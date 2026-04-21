"""Comprehensive CANSLIM historical backtesting engine."""

from __future__ import annotations

import argparse
import os
import pickle
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from config import settings
from backtest import (
    _calculate_rs_at_date,
    _compute_canslim_score,
    _download_bulk_closes,
    _download_price_data,
    _evaluate_fundamentals_at_date,
    _evaluate_market_at_date,
    _evaluate_technical_at_date,
)
from core.data_client import clear_session_cache, fetch_bulk_ohlcv
from core.index_ticker_fetcher import get_all_index_tickers, get_sp500_tickers
from core.momentum_analysis import calculate_weighted_performance

sys.stdout.reconfigure(line_buffering=True)


DEFAULT_TICKERS = ["CRWD", "NVDA", "MU", "GEV", "VRT", "VST"]
DEFAULT_CAPITAL = 100_000.0
DEFAULT_LOOKBACK_WEEKS = 156
DEFAULT_SIGNAL_EVERY_N_DAYS = 5
DEFAULT_MA_EXIT_PERIOD = 21
DEFAULT_MA_CONSECUTIVE = 2
DEFAULT_START_DATE = "2023-04-01"
DEFAULT_END_DATE = "2026-04-01"
DEFAULT_TAKE_PROFIT_PCT = 0.40
DEFAULT_SCALE_OUT_FRACTION = 0.50
DEFAULT_STAGNATION_DAYS = 20
DEFAULT_STAGNATION_THRESHOLD_PCT = 0.05
DEFAULT_BREAKEVEN_TRIGGER_PCT = 0.08
DEFAULT_POSITION_SIZE_PCT = settings.POSITION_SIZE_PCT
DEFAULT_POSITION_RISK_PCT = settings.POSITION_RISK_PCT  # 1% portfolio risk per trade
DEFAULT_MIN_RS_SCORE = float(settings.MIN_RS_SCORE)
DEFAULT_MIN_C_A_GROWTH = 0.25
DEFAULT_MIN_TECHNICAL_SCORE = 70.0
DEFAULT_BULK_PRICE_FETCH_THRESHOLD = 25
BENCHMARK = "SPY"


def _period_for_date_range(start_date: pd.Timestamp, end_date: pd.Timestamp, buffer_days: int = 120) -> str:
    days = max((end_date - start_date).days + buffer_days, 35)
    if days <= 35:
        return "1mo"
    if days <= 100:
        return "3mo"
    if days <= 200:
        return "6mo"
    if days <= 370:
        return "1y"
    if days <= 435:
        return "14mo"
    if days <= 740:
        return "2y"
    if days <= 1100:
        return "3y"
    return "5y"


def _resolve_window(
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    lookback_weeks: Optional[int] = None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    end_ts = pd.Timestamp(end_date or DEFAULT_END_DATE)
    today = pd.Timestamp(datetime.now().date())
    if end_ts > today:
        end_ts = today

    if start_date:
        start_ts = pd.Timestamp(start_date)
    else:
        weeks = lookback_weeks if lookback_weeks is not None else DEFAULT_LOOKBACK_WEEKS
        start_ts = end_ts - pd.Timedelta(weeks=weeks)

    return start_ts.normalize(), end_ts.normalize()


def _calculate_rs_snapshot(all_closes: pd.DataFrame, eval_date: pd.Timestamp) -> Dict[str, float]:
    """Calculate RS scores for the full universe once as-of a specific date."""
    sliced = all_closes.loc[:eval_date].dropna(axis=1, how="all")
    if sliced.empty:
        return {}

    perfs: dict[str, float] = {}
    for ticker in sliced.columns:
        series = sliced[ticker].dropna()
        if len(series) < 60:
            continue

        wp = calculate_weighted_performance(series)
        if wp is None and len(series) >= 60:
            raw_return = (series.iloc[-1] - series.iloc[0]) / series.iloc[0]
            trading_days = len(series)
            wp = (1 + raw_return) ** (252 / trading_days) - 1

        if wp is not None:
            perfs[str(ticker)] = float(wp)

    if len(perfs) < 10:
        return {}

    perf_series = pd.Series(perfs)
    ranks = perf_series.rank(pct=True)
    rs_scores = ranks * settings.RS_PERCENTILE_MULTIPLIER + settings.RS_PERCENTILE_MIN
    return {str(symbol): float(score) for symbol, score in rs_scores.items()}


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    qty: float
    stop_price: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    canslim_score: float = 0.0
    rs_score: float = 0.0
    entry_reason: str = "Signal"
    remaining_qty: Optional[float] = None
    realized_pnl: float = 0.0
    scale_out_price: Optional[float] = None
    scaled_out_qty: float = 0.0
    peak_close: Optional[float] = None
    days_held: int = 0
    breakeven_armed: bool = False
    ema_trailing_active: bool = False
    scale_out_tier: int = 0
    eight_week_hold: bool = False

    def __post_init__(self) -> None:
        if self.remaining_qty is None:
            self.remaining_qty = self.qty
        if self.peak_close is None:
            self.peak_close = self.entry_price

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        remaining_qty = self.remaining_qty or 0.0
        return self.realized_pnl + (self.exit_price - self.entry_price) * remaining_qty

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None or self.cost_basis == 0:
            return 0.0
        return self.pnl / self.cost_basis

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.qty


@dataclass
class SimulationResult:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    benchmark_curve: pd.Series = field(default_factory=pd.Series)
    initial_capital: float = DEFAULT_CAPITAL
    config: dict = field(default_factory=dict)
    transaction_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    weekly_holdings: pd.DataFrame = field(default_factory=pd.DataFrame)
    signal_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    benchmark_symbol: str = BENCHMARK

    @property
    def closed_trades(self) -> List[Trade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def open_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.is_open]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    @property
    def total_return_pct(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        return float((self.equity_curve.iloc[-1] / self.equity_curve.iloc[0] - 1) * 100)

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pnl > 0)
        return wins / len(closed) * 100

    @property
    def avg_win_pct(self) -> float:
        wins = [t.pnl_pct for t in self.closed_trades if t.pnl > 0]
        return float(np.mean(wins) * 100) if wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.pnl_pct for t in self.closed_trades if t.pnl <= 0]
        return float(np.mean(losses) * 100) if losses else 0.0

    @property
    def exit_reason_counts(self) -> dict:
        counts: dict = {}
        for t in self.closed_trades:
            reason = t.exit_reason or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    @property
    def annualized_return_pct(self) -> float:
        return PerformanceReport.compute_metrics(self.equity_curve).get("annualized_return_pct", 0.0)

    @property
    def max_drawdown_pct(self) -> float:
        return PerformanceReport.compute_metrics(self.equity_curve).get("max_drawdown_pct", 0.0)

    @property
    def sharpe_ratio(self) -> float:
        return PerformanceReport.compute_metrics(self.equity_curve).get("sharpe_ratio", 0.0)

    @property
    def benchmark_return_pct(self) -> float:
        return PerformanceReport.compute_metrics(self.benchmark_curve).get("total_return_pct", 0.0)

    @property
    def benchmark_annualized_return_pct(self) -> float:
        return PerformanceReport.compute_metrics(self.benchmark_curve).get("annualized_return_pct", 0.0)

    @property
    def benchmark_max_drawdown_pct(self) -> float:
        return PerformanceReport.compute_metrics(self.benchmark_curve).get("max_drawdown_pct", 0.0)

    @property
    def benchmark_sharpe_ratio(self) -> float:
        return PerformanceReport.compute_metrics(self.benchmark_curve).get("sharpe_ratio", 0.0)


class PerformanceReport:
    """Portfolio and benchmark metric helpers."""

    @staticmethod
    def compute_metrics(curve: pd.Series) -> dict[str, float]:
        if curve is None or len(curve) < 2:
            return {
                "total_return_pct": 0.0,
                "annualized_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "sharpe_ratio": 0.0,
            }

        series = curve.astype(float)
        total_return = float((series.iloc[-1] / series.iloc[0] - 1) * 100)
        returns = series.pct_change().dropna()
        if returns.empty:
            annualized = 0.0
            sharpe = 0.0
        else:
            days = max((pd.to_datetime(series.index[-1]) - pd.to_datetime(series.index[0])).days, 1)
            annualized = float(((series.iloc[-1] / series.iloc[0]) ** (365 / days) - 1) * 100)
            sharpe = 0.0 if returns.std() == 0 else float((returns.mean() / returns.std()) * np.sqrt(252))

        rolling_max = series.cummax()
        drawdown = ((series - rolling_max) / rolling_max).min()
        max_drawdown = float(drawdown * 100) if pd.notna(drawdown) else 0.0

        return {
            "total_return_pct": total_return,
            "annualized_return_pct": annualized,
            "max_drawdown_pct": max_drawdown,
            "sharpe_ratio": sharpe,
        }


class DataFetcher:
    """Backtest data loader with persistent SQLite cache."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.BACKTEST_DATA_CACHE_DB_PATH
        self._db_available = False
        self._memory_cache: dict[str, Any] = {}
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._ensure_schema()
            self._db_available = True
        except (OSError, sqlite3.Error):
            self._db_available = False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_cache (
                    cache_key TEXT PRIMARY KEY,
                    cache_kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload BLOB NOT NULL
                )
                """
            )

    def _load_cached(self, cache_key: str) -> Any | None:
        if not self._db_available:
            return self._memory_cache.get(cache_key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM dataset_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return pickle.loads(row[0])

    def _store_cached(self, cache_key: str, cache_kind: str, payload: Any) -> None:
        if not self._db_available:
            self._memory_cache[cache_key] = payload
            return
        blob = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dataset_cache(cache_key, cache_kind, created_at, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cache_key, cache_kind, datetime.now().isoformat(), blob),
                )
        except sqlite3.Error:
            self._db_available = False
            self._memory_cache[cache_key] = payload

    def fetch_price_data(
        self,
        tickers: List[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> Dict[str, pd.DataFrame]:
        period = _period_for_date_range(start_date, end_date)
        cache_key = f"price::{period}::{start_date.date()}::{end_date.date()}::{','.join(sorted(tickers))}"
        cached = self._load_cached(cache_key)
        if cached is not None:
            return cached

        if len(tickers) >= DEFAULT_BULK_PRICE_FETCH_THRESHOLD:
            data = fetch_bulk_ohlcv(tickers, period=period, chunk_size=settings.CHUNK_SIZE)
        else:
            data = _download_price_data(tickers, period=period)
        result = {symbol: df.copy() for symbol, df in data.items() if not df.empty}
        self._store_cached(cache_key, "price", result)
        return result

    def fetch_rs_universe_closes(
        self,
        tickers: List[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        period = _period_for_date_range(start_date, end_date)
        cache_key = f"closes::{period}::{start_date.date()}::{end_date.date()}::{','.join(sorted(tickers))}"
        cached = self._load_cached(cache_key)
        if cached is not None:
            return cached

        closes = _download_bulk_closes(tickers, period=period)
        self._store_cached(cache_key, "closes", closes)
        return closes


class CanslimStrategy:
    """Modular CANSLIM signal evaluation."""

    def __init__(
        self,
        *,
        min_c_a_growth: float = DEFAULT_MIN_C_A_GROWTH,
        min_rs_score: float = DEFAULT_MIN_RS_SCORE,
        min_canslim_score: float = float(settings.MIN_CANSLIM_SCORE),
        min_technical_score: float = DEFAULT_MIN_TECHNICAL_SCORE,
        technical_only: bool = False,
    ) -> None:
        self.min_c_a_growth = min_c_a_growth
        self.min_rs_score = min_rs_score
        self.min_canslim_score = min_canslim_score
        self.min_technical_score = min_technical_score
        self.technical_only = technical_only

    @staticmethod
    def _compute_technical_score(
        *,
        n_score: float,
        s_score: float,
        l_score: float,
        m_score: float,
    ) -> float:
        raw = (
            settings.CANSLIM_WEIGHT_N * n_score
            + settings.CANSLIM_WEIGHT_S * s_score
            + settings.CANSLIM_WEIGHT_L * l_score
            + settings.CANSLIM_WEIGHT_M * m_score
        )
        technical_weight = (
            settings.CANSLIM_WEIGHT_N
            + settings.CANSLIM_WEIGHT_S
            + settings.CANSLIM_WEIGHT_L
            + settings.CANSLIM_WEIGHT_M
        )
        if technical_weight <= 0:
            return 0.0
        return float((raw / technical_weight) * 100)

    def evaluate_market(self, spy_df: pd.DataFrame, eval_date: pd.Timestamp) -> dict:
        m_score, is_bullish, dist_days, ftd = _evaluate_market_at_date(spy_df, eval_date)
        return {
            "m_score": m_score,
            "market_is_bullish": bool(is_bullish),
            "distribution_days": dist_days,
            "follow_through": bool(ftd),
        }

    def evaluate_symbol(
        self,
        *,
        ticker: str,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        all_closes: pd.DataFrame,
        eval_date: pd.Timestamp,
        market_state: dict,
        rs_score: Optional[float] = None,
    ) -> Optional[dict]:
        tdata = ticker_ohlcv.get(ticker)
        if tdata is None:
            return None

        available = tdata.loc[:eval_date]
        if len(available) < 60:
            return None

        rs_score = float(rs_score) if rs_score is not None else _calculate_rs_at_date(all_closes, ticker, eval_date)
        l_score = rs_score / 100.0
        if self.technical_only:
            fund = {
                "current_growth": None,
                "annual_growth": None,
                "c_score": 0.0,
                "a_score": 0.0,
                "i_score": 0.5,
                "institutional_data_available": False,
                "shares_outstanding": None,
            }
        else:
            fund = _evaluate_fundamentals_at_date(ticker, eval_date)
        tech = _evaluate_technical_at_date(tdata, eval_date, fund.get("shares_outstanding"))

        c_growth = fund.get("current_growth")
        a_growth = fund.get("annual_growth")
        c_score = fund.get("c_score", 0.0)
        a_score = fund.get("a_score", 0.0)
        i_score = fund.get("i_score", 0.5)
        has_fundamentals = c_growth is not None or a_growth is not None

        total_score = _compute_canslim_score(
            c=c_score,
            a=a_score,
            n=tech["n_score"],
            s=tech["s_score"],
            l_score=l_score,
            i=i_score,
            m=market_state["m_score"],
            has_fundamentals=has_fundamentals,
            institutional_data_available=bool(fund.get("institutional_data_available", False)),
        )

        peg_details = tech.get("power_gap_details") or {}
        has_peg_today = bool(tech.get("has_power_gap")) and peg_details.get("days_ago") == 0
        has_breakout = bool(tech.get("is_breakout"))
        has_surge = bool(tech.get("has_volume_surge"))
        technical_score = self._compute_technical_score(
            n_score=tech["n_score"],
            s_score=tech["s_score"],
            l_score=l_score,
            m_score=market_state["m_score"],
        )

        c_pass = c_growth is not None and c_growth >= self.min_c_a_growth
        a_pass = a_growth is not None and a_growth >= self.min_c_a_growth
        l_pass = rs_score >= self.min_rs_score
        m_pass = bool(market_state["market_is_bullish"])
        tech_pass = (has_breakout and has_surge) or has_peg_today
        composite_pass = total_score >= self.min_canslim_score
        technical_composite_pass = technical_score >= self.min_technical_score
        if self.technical_only:
            buy_signal = all([l_pass, m_pass, tech_pass, technical_composite_pass])
        else:
            buy_signal = all([c_pass, a_pass, l_pass, m_pass, tech_pass, composite_pass])

        if has_peg_today:
            signal_reason = "PEG Breakout"
        elif has_breakout and has_surge:
            signal_reason = "Volume Breakout"
        else:
            signal_reason = "No Breakout"

        return {
            "symbol": ticker,
            "signal_date": str(eval_date.date()),
            "close": tech.get("close"),
            "c_score": c_score,
            "a_score": a_score,
            "n_score": tech.get("n_score", 0.0),
            "s_score": tech.get("s_score", 0.0),
            "i_score": i_score,
            "m_score": market_state["m_score"],
            "current_growth": c_growth,
            "annual_growth": a_growth,
            "rs_score": rs_score,
            "canslim_score": total_score,
            "technical_score": technical_score,
            "market_is_bullish": m_pass,
            "has_breakout": has_breakout,
            "has_volume_surge": has_surge,
            "has_peg_today": has_peg_today,
            "buy_signal": buy_signal,
            "signal_reason": signal_reason,
            "technical_only": self.technical_only,
        }


class PortfolioSimulator:
    """Simulates the full CANSLIM portfolio lifecycle."""

    def __init__(
        self,
        initial_capital: float = DEFAULT_CAPITAL,
        max_positions: int = settings.MAX_OPEN_POSITIONS,
        position_size_pct: float = DEFAULT_POSITION_SIZE_PCT,
        position_risk_pct: float = DEFAULT_POSITION_RISK_PCT,
        stop_loss_pct: float = settings.STOP_LOSS_PCT,
        ma_exit_period: int = DEFAULT_MA_EXIT_PERIOD,
        ma_consecutive: int = DEFAULT_MA_CONSECUTIVE,
        signal_every_n_days: int = DEFAULT_SIGNAL_EVERY_N_DAYS,
        min_canslim_score: float = float(settings.MIN_CANSLIM_SCORE),
        min_rs_score: float = DEFAULT_MIN_RS_SCORE,
        min_technical_score: float = DEFAULT_MIN_TECHNICAL_SCORE,
        require_bullish_market: bool = True,
        technical_only: bool = False,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        scale_out_fraction: float = DEFAULT_SCALE_OUT_FRACTION,
        stagnation_days: int = DEFAULT_STAGNATION_DAYS,
        stagnation_threshold_pct: float = DEFAULT_STAGNATION_THRESHOLD_PCT,
        breakeven_trigger_pct: float = DEFAULT_BREAKEVEN_TRIGGER_PCT,
        data_fetcher: Optional[DataFetcher] = None,
        strategy: Optional[CanslimStrategy] = None,
        benchmark_symbol: str = BENCHMARK,
    ) -> None:
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.position_risk_pct = position_risk_pct
        self.stop_loss_pct = stop_loss_pct
        self.ma_exit_period = ma_exit_period
        self.ma_consecutive = ma_consecutive
        self.signal_every_n_days = signal_every_n_days
        self.min_canslim_score = min_canslim_score
        self.min_rs_score = min_rs_score
        self.min_technical_score = min_technical_score
        self.require_bullish_market = require_bullish_market
        self.technical_only = technical_only
        self.take_profit_pct = take_profit_pct
        self.scale_out_fraction = scale_out_fraction
        self.stagnation_days = stagnation_days
        self.stagnation_threshold_pct = stagnation_threshold_pct
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.data_fetcher = data_fetcher or DataFetcher()
        self.strategy = strategy or CanslimStrategy(
            min_rs_score=min_rs_score,
            min_canslim_score=min_canslim_score,
            min_technical_score=min_technical_score,
            technical_only=technical_only,
        )
        self.benchmark_symbol = benchmark_symbol

        self._equity: float = initial_capital
        self._open_positions: Dict[str, Trade] = {}
        self._trades: List[Trade] = []
        self._transactions: List[dict] = []
        self._weekly_snapshots: List[dict] = []
        self._signal_rows: List[dict] = []

    def run(
        self,
        tickers: List[str],
        lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        benchmark_symbol: Optional[str] = None,
    ) -> SimulationResult:
        self._equity = self.initial_capital
        self._open_positions = {}
        self._trades = []
        self._transactions = []
        self._weekly_snapshots = []
        self._signal_rows = []

        clear_session_cache()
        benchmark = benchmark_symbol or self.benchmark_symbol
        start_ts, end_ts = _resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_weeks=lookback_weeks,
        )

        all_tickers = list(dict.fromkeys([*tickers, benchmark]))
        print(f"Downloading price data for {len(all_tickers)} tickers...")
        ticker_ohlcv = self.data_fetcher.fetch_price_data(all_tickers, start_ts, end_ts)
        if benchmark not in ticker_ohlcv:
            print(f"FATAL: Could not download {benchmark} benchmark data.")
            return SimulationResult()

        universe = list(dict.fromkeys([*tickers, *get_sp500_tickers()]))
        print(f"Downloading RS universe closes for {len(universe)} tickers...")
        all_closes = self.data_fetcher.fetch_rs_universe_closes(universe, start_ts, end_ts)

        benchmark_df = ticker_ohlcv[benchmark]
        trading_days = benchmark_df.loc[start_ts:end_ts].index
        if len(trading_days) < 30:
            print("ERROR: Not enough trading days in range.")
            return SimulationResult()

        equity_series: Dict[str, float] = {}
        benchmark_series: Dict[str, float] = {}
        benchmark_start_price: Optional[float] = None
        pending_entries: List[dict] = []

        for day_idx, eval_date in enumerate(trading_days):
            date_str = str(eval_date.date())

            for symbol in list(self._open_positions.keys()):
                ohlcv = ticker_ohlcv.get(symbol)
                if ohlcv is not None:
                    self._check_exits(symbol, ohlcv, eval_date)

            for pending in pending_entries:
                self._enter_position(pending, ticker_ohlcv, eval_date)
            pending_entries = []

            is_signal_day = day_idx % self.signal_every_n_days == 0
            market_state = self.strategy.evaluate_market(benchmark_df, eval_date)
            if is_signal_day:
                pending_entries = self._evaluate_signals(
                    tickers=tickers,
                    ticker_ohlcv=ticker_ohlcv,
                    all_closes=all_closes,
                    eval_date=eval_date,
                    market_state=market_state,
                )

            equity_series[date_str] = self._mark_equity(ticker_ohlcv, eval_date)

            benchmark_bar = benchmark_df.loc[:eval_date]
            if not benchmark_bar.empty:
                benchmark_price = float(benchmark_bar["Close"].iloc[-1])
                if benchmark_start_price is None:
                    benchmark_start_price = benchmark_price
                benchmark_series[date_str] = (benchmark_price / benchmark_start_price) * self.initial_capital

            self._record_weekly_holdings(ticker_ohlcv, eval_date, trading_days)

        last_date = pd.Timestamp(trading_days[-1])
        for symbol in list(self._open_positions.keys()):
            ohlcv = ticker_ohlcv.get(symbol)
            if ohlcv is None:
                continue
            bar = ohlcv.loc[:last_date]
            if not bar.empty:
                exit_price = float(bar["Close"].iloc[-1])
                self._close_trade(symbol, exit_price, "end_of_test", str(last_date.date()))

        return SimulationResult(
            trades=self._trades,
            equity_curve=pd.Series(equity_series),
            benchmark_curve=pd.Series(benchmark_series),
            initial_capital=self.initial_capital,
            config={
                "tickers": tickers,
                "benchmark_symbol": benchmark,
                "max_positions": self.max_positions,
                "position_size_pct": self.position_size_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "ma_exit_period": self.ma_exit_period,
                "ma_consecutive": self.ma_consecutive,
                "signal_every_n_days": self.signal_every_n_days,
                "min_canslim_score": self.min_canslim_score,
                "min_rs_score": self.min_rs_score,
                "min_technical_score": self.min_technical_score,
                "technical_only": self.technical_only,
                "take_profit_pct": self.take_profit_pct,
                "scale_out_fraction": self.scale_out_fraction,
                "stagnation_days": self.stagnation_days,
                "stagnation_threshold_pct": self.stagnation_threshold_pct,
                "breakeven_trigger_pct": self.breakeven_trigger_pct,
                "start_date": str(start_ts.date()),
                "end_date": str(end_ts.date()),
            },
            transaction_log=pd.DataFrame(self._transactions),
            weekly_holdings=pd.DataFrame(self._weekly_snapshots),
            signal_log=pd.DataFrame(self._signal_rows),
            benchmark_symbol=benchmark,
        )

    def _evaluate_signals(
        self,
        *,
        tickers: List[str],
        ticker_ohlcv: Dict[str, pd.DataFrame],
        all_closes: pd.DataFrame,
        eval_date: pd.Timestamp,
        market_state: dict,
    ) -> List[dict]:
        if self.require_bullish_market and not market_state["market_is_bullish"]:
            return []

        signals: List[dict] = []
        rs_snapshot = _calculate_rs_snapshot(all_closes, eval_date)
        for ticker in tickers:
            if ticker in self._open_positions or ticker not in ticker_ohlcv:
                continue

            row = self.strategy.evaluate_symbol(
                ticker=ticker,
                ticker_ohlcv=ticker_ohlcv,
                all_closes=all_closes,
                eval_date=eval_date,
                market_state=market_state,
                rs_score=rs_snapshot.get(ticker),
            )
            if row is None:
                continue
            self._signal_rows.append(row)
            if row["buy_signal"]:
                signals.append(row)

        signals.sort(key=lambda item: (item["canslim_score"], item["rs_score"]), reverse=True)
        open_slots = max(self.max_positions - len(self._open_positions), 0)
        return signals[:open_slots]

    def _enter_position(
        self,
        signal: dict,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        entry_date: pd.Timestamp,
    ) -> None:
        symbol = signal["symbol"]
        if symbol in self._open_positions or len(self._open_positions) >= self.max_positions:
            return

        ohlcv = ticker_ohlcv.get(symbol)
        if ohlcv is None:
            return

        bar = ohlcv.loc[entry_date:entry_date]
        if bar.empty:
            prev = ohlcv.loc[:entry_date]
            if prev.empty:
                return
            entry_price = float(prev["Close"].iloc[-1])
        else:
            entry_price = float(bar["Open"].iloc[0]) if "Open" in bar.columns else float(bar["Close"].iloc[0])

        if entry_price <= 0:
            return

        total_portfolio_value = self._mark_equity(ticker_ohlcv, entry_date)
        # Risk-based sizing: risk exactly position_risk_pct of portfolio per trade.
        # shares = (portfolio * risk_pct) / (entry * stop_pct)
        # position_value = shares * entry = portfolio * risk_pct / stop_pct
        risk_amount = total_portfolio_value * self.position_risk_pct
        risk_per_share = entry_price * self.stop_loss_pct
        if risk_per_share <= 0:
            return
        position_value = min(self._equity, risk_amount / risk_per_share * entry_price)
        if position_value <= 0:
            return

        qty = position_value / entry_price
        stop_price = round(entry_price * (1 - self.stop_loss_pct), 2)
        trade = Trade(
            symbol=symbol,
            entry_date=str(entry_date.date()),
            entry_price=entry_price,
            qty=qty,
            stop_price=stop_price,
            canslim_score=signal.get("canslim_score", 0.0),
            rs_score=signal.get("rs_score", 0.0),
            entry_reason=signal.get("signal_reason", "Signal"),
        )
        self._open_positions[symbol] = trade
        self._equity -= position_value
        self._record_transaction(
            date=str(entry_date.date()),
            ticker=symbol,
            action="BUY",
            price=entry_price,
            quantity=qty,
            reason=signal.get("signal_reason", "Signal"),
        )

    def _check_exits(
        self,
        symbol: str,
        ohlcv: pd.DataFrame,
        eval_date: pd.Timestamp,
        *_unused_args,
        **_unused_kwargs,
    ) -> None:
        trade = self._open_positions.get(symbol)
        if trade is None:
            return

        bar = ohlcv.loc[eval_date:eval_date]
        if bar.empty:
            return

        low = float(bar["Low"].iloc[0]) if "Low" in bar.columns else float(bar["Close"].iloc[0])
        high = float(bar["High"].iloc[0]) if "High" in bar.columns else float(bar["Close"].iloc[0])
        close = float(bar["Close"].iloc[0])
        date_str = str(eval_date.date())

        trade.days_held += 1
        trade.peak_close = max(trade.peak_close or trade.entry_price, close)

        if low <= trade.stop_price:
            self._close_trade(symbol, trade.stop_price, "stop_loss", date_str)
            return

        target_price = trade.entry_price * (1 + self.take_profit_pct)
        if (
            trade.scale_out_price is None
            and trade.remaining_qty
            and trade.remaining_qty > 0
            and high >= target_price
        ):
            self._scale_out_trade(symbol, target_price, date_str, "take_profit_scale_out")
            trade = self._open_positions.get(symbol)
            if trade is None:
                return

        if (
            trade.days_held >= self.stagnation_days
            and (trade.peak_close or trade.entry_price) < trade.entry_price * (1 + self.stagnation_threshold_pct)
        ):
            self._close_trade(symbol, close, "time_stop", date_str)
            return

        history = ohlcv.loc[:eval_date]
        self._update_protective_stop(trade, history, high)

        if len(history) >= self.ma_exit_period + self.ma_consecutive:
            ema = history["Close"].ewm(span=self.ma_exit_period, adjust=False).mean()
            last_closes = history["Close"].iloc[-self.ma_consecutive:]
            last_ema = ema.iloc[-self.ma_consecutive:]
            if (last_closes.values < last_ema.values).all():
                self._close_trade(symbol, close, "ma_violation", date_str)

    def _update_protective_stop(self, trade: Trade, history: pd.DataFrame, high: float) -> None:
        """Ratchet stops using only end-of-bar information.

        We intentionally apply stop upgrades after processing the current bar's stop
        check. With daily bars, this avoids assuming the intraday high happened
        before an intraday stop breach on the same candle.
        """
        next_stop = trade.stop_price
        breakeven_trigger = trade.entry_price * (1 + self.breakeven_trigger_pct)

        if not trade.breakeven_armed and high >= breakeven_trigger:
            trade.breakeven_armed = True
            next_stop = max(next_stop, trade.entry_price)

        if trade.breakeven_armed and len(history) >= self.ma_exit_period:
            ema_today = history["Close"].ewm(span=self.ma_exit_period, adjust=False).mean().iloc[-1]
            if pd.notna(ema_today):
                trade.ema_trailing_active = True
                next_stop = max(next_stop, float(ema_today))

        if next_stop > trade.stop_price:
            trade.stop_price = round(next_stop, 2)

    def _scale_out_trade(
        self,
        symbol: str,
        exit_price: float,
        date_str: str,
        reason: str,
        sell_qty: Optional[float] = None,
    ) -> None:
        trade = self._open_positions.get(symbol)
        if trade is None or not trade.remaining_qty:
            return

        scale_qty = sell_qty if sell_qty is not None else trade.remaining_qty * self.scale_out_fraction
        if scale_qty <= 0:
            return
        trade.remaining_qty = (trade.remaining_qty or 0.0) - scale_qty
        trade.scaled_out_qty += scale_qty
        trade.scale_out_price = exit_price
        proceeds = exit_price * scale_qty
        trade.realized_pnl += (exit_price - trade.entry_price) * scale_qty
        self._equity += proceeds
        self._record_transaction(
            date=date_str,
            ticker=symbol,
            action="SELL",
            price=exit_price,
            quantity=scale_qty,
            reason=reason,
        )

    def _close_trade(self, symbol: str, exit_price: float, reason: str, date_str: str) -> None:
        trade = self._open_positions.pop(symbol, None)
        if trade is None:
            return

        remaining_qty = trade.remaining_qty or 0.0
        proceeds = exit_price * remaining_qty
        self._equity += proceeds
        self._record_transaction(
            date=date_str,
            ticker=symbol,
            action="SELL",
            price=exit_price,
            quantity=remaining_qty,
            reason=reason,
        )

        trade.exit_price = exit_price
        trade.exit_date = date_str
        trade.exit_reason = reason
        self._trades.append(trade)

    def _mark_equity(self, ticker_ohlcv: Dict[str, pd.DataFrame], eval_date: pd.Timestamp) -> float:
        market_value = self._equity
        for symbol, trade in self._open_positions.items():
            ohlcv = ticker_ohlcv.get(symbol)
            if ohlcv is None:
                continue
            bar = ohlcv.loc[:eval_date]
            if bar.empty:
                continue
            current_price = float(bar["Close"].iloc[-1])
            market_value += current_price * (trade.remaining_qty or 0.0)
        return market_value

    def _record_transaction(
        self,
        *,
        date: str,
        ticker: str,
        action: str,
        price: float,
        quantity: float,
        reason: str,
    ) -> None:
        self._transactions.append(
            {
                "Date": date,
                "Ticker": ticker,
                "Action": action,
                "Price": round(price, 4),
                "Quantity": round(quantity, 6),
                "Value": round(price * quantity, 2),
                "Reason": reason,
            }
        )

    def _record_weekly_holdings(
        self,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
        trading_days: pd.Index,
    ) -> None:
        current_idx = trading_days.get_loc(eval_date)
        next_day = trading_days[current_idx + 1] if current_idx + 1 < len(trading_days) else None
        is_week_end = eval_date.weekday() == 4 or next_day is None or next_day.weekday() < eval_date.weekday()
        if not is_week_end:
            return

        symbols = sorted(self._open_positions.keys())
        market_value = 0.0
        for symbol in symbols:
            ohlcv = ticker_ohlcv.get(symbol)
            if ohlcv is None:
                continue
            bar = ohlcv.loc[:eval_date]
            if bar.empty:
                continue
            price = float(bar["Close"].iloc[-1])
            market_value += price * (self._open_positions[symbol].remaining_qty or 0.0)

        self._weekly_snapshots.append(
            {
                "Week_Ending": str(eval_date.date()),
                "Holdings": ",".join(symbols),
                "Holding_Count": len(symbols),
                "Cash": round(self._equity, 2),
                "Market_Value": round(market_value, 2),
                "Total_Equity": round(self._equity + market_value, 2),
            }
        )


def print_pnl_report(result: SimulationResult) -> None:
    print("\n" + "=" * 72)
    print("COMPREHENSIVE CANSLIM BACKTEST")
    print("=" * 72)

    cfg = result.config
    print(f"Capital:          ${result.initial_capital:,.0f}")
    print(f"Universe size:    {len(cfg.get('tickers', []))}")
    print(f"Benchmark:        {result.benchmark_symbol}")
    print(f"Window:           {cfg.get('start_date', '?')} -> {cfg.get('end_date', '?')}")
    print(f"Signal cadence:   every {cfg.get('signal_every_n_days', DEFAULT_SIGNAL_EVERY_N_DAYS)} trading days")
    print(f"Stop-loss:        {cfg.get('stop_loss_pct', settings.STOP_LOSS_PCT) * 100:.1f}%")
    print(f"Take-profit:      {cfg.get('take_profit_pct', DEFAULT_TAKE_PROFIT_PCT) * 100:.1f}%")
    print(f"Time stop:        {cfg.get('stagnation_days', DEFAULT_STAGNATION_DAYS)} days")
    print(f"Breakeven trigger:{cfg.get('breakeven_trigger_pct', DEFAULT_BREAKEVEN_TRIGGER_PCT) * 100:>11.1f}%")
    print(f"RS floor:         {cfg.get('min_rs_score', DEFAULT_MIN_RS_SCORE)}")
    if cfg.get("technical_only"):
        print("Mode:             technical-only")
        print(f"Technical floor:  {cfg.get('min_technical_score', DEFAULT_MIN_TECHNICAL_SCORE)}")
    else:
        print("Mode:             full CANSLIM")
        print(f"CANSLIM floor:    {cfg.get('min_canslim_score', settings.MIN_CANSLIM_SCORE)}")

    print("\n--- Portfolio vs Benchmark ---")
    print(f"{'Metric':<22} {'Strategy':>12} {'Benchmark':>12}")
    print(f"{'Total Return':<22} {result.total_return_pct:>11.1f}% {result.benchmark_return_pct:>11.1f}%")
    print(f"{'Annualized Return':<22} {result.annualized_return_pct:>11.1f}% {result.benchmark_annualized_return_pct:>11.1f}%")
    print(f"{'Max Drawdown':<22} {result.max_drawdown_pct:>11.1f}% {result.benchmark_max_drawdown_pct:>11.1f}%")
    print(f"{'Sharpe Ratio':<22} {result.sharpe_ratio:>12.2f} {result.benchmark_sharpe_ratio:>12.2f}")

    print("\n--- Trade Statistics ---")
    print(f"Closed trades:     {len(result.closed_trades)}")
    print(f"Win rate:          {result.win_rate:.1f}%")
    print(f"Average win:       {result.avg_win_pct:+.1f}%")
    print(f"Average loss:      {result.avg_loss_pct:+.1f}%")
    print(f"Exit reasons:      {result.exit_reason_counts}")
    if not result.transaction_log.empty:
        print(f"Transactions:      {len(result.transaction_log)}")
    if not result.weekly_holdings.empty:
        print(f"Weekly snapshots:  {len(result.weekly_holdings)}")

    warnings: list[str] = []
    if result.signal_log.empty:
        warnings.append("No signal rows were evaluated; check universe and benchmark data availability.")
    else:
        if (
            not cfg.get("technical_only")
            and int(result.signal_log["current_growth"].notna().sum()) == 0
            and int(result.signal_log["annual_growth"].notna().sum()) == 0
        ):
            warnings.append("No C/A fundamental growth data was available. Strict CANSLIM buy gates will block every trade.")
        if len(result.config.get("tickers", [])) < 50:
            warnings.append(
                f"Universe contains only {len(result.config.get('tickers', []))} tickers. "
                "This is far smaller than a real S&P 500 backtest and may indicate fallback or degraded index data."
            )
        if len(result.closed_trades) == 0 and int(result.signal_log["buy_signal"].sum()) == 0:
            warnings.append("No buy signals passed all gates during the run. Review signal_log output before trusting the zero-trade result.")

    if warnings:
        print("\n--- Warnings ---")
        for warning in warnings:
            print(f"  - {warning}")

    idx = pd.to_datetime(result.equity_curve.index) if not result.equity_curve.empty else pd.Index([])
    years = sorted(idx.year.unique()) if len(idx) else []
    if len(years) > 1:
        print("\n--- Annual Returns ---")
        for year in years:
            strat_year = result.equity_curve[idx.year == year]
            bench_idx = pd.to_datetime(result.benchmark_curve.index)
            bench_year = result.benchmark_curve[bench_idx.year == year] if not result.benchmark_curve.empty else pd.Series(dtype=float)
            strat_ret = (strat_year.iloc[-1] / strat_year.iloc[0] - 1) * 100 if len(strat_year) > 1 else 0.0
            bench_ret = (bench_year.iloc[-1] / bench_year.iloc[0] - 1) * 100 if len(bench_year) > 1 else 0.0
            print(f"  {year}: {strat_ret:+.1f}%  ({result.benchmark_symbol} {bench_ret:+.1f}%)")


def export_equity_curve(result: SimulationResult, filename: Optional[str] = None) -> str:
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"equity_curve_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    pd.DataFrame(
        {
            "Date": result.equity_curve.index,
            "Portfolio": result.equity_curve.values,
            "Benchmark": result.benchmark_curve.reindex(result.equity_curve.index).values,
        }
    ).to_csv(filename, index=False)
    return filename


def export_pnl_to_csv(result: SimulationResult, filename: Optional[str] = None) -> str:
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"transaction_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    result.transaction_log.to_csv(filename, index=False)
    return filename


def export_weekly_holdings(result: SimulationResult, filename: Optional[str] = None) -> str:
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"weekly_holdings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    result.weekly_holdings.to_csv(filename, index=False)
    return filename


def export_signal_log(result: SimulationResult, filename: Optional[str] = None) -> str:
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"signal_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    result.signal_log.to_csv(filename, index=False)
    return filename


def export_trade_charts(
    result: SimulationResult,
    *,
    output_dir: Optional[str] = None,
    data_fetcher: Optional[DataFetcher] = None,
) -> List[str]:
    if result.transaction_log.empty:
        return []

    output_dir = output_dir or os.path.join(settings.BACKTEST_RESULTS_DIR, "charts")
    os.makedirs(output_dir, exist_ok=True)
    fetcher = data_fetcher or DataFetcher()

    start_date = pd.Timestamp(result.config.get("start_date", DEFAULT_START_DATE))
    end_date = pd.Timestamp(result.config.get("end_date", DEFAULT_END_DATE))
    symbols = sorted(result.transaction_log["Ticker"].dropna().unique())
    price_data = fetcher.fetch_price_data(symbols, start_date, end_date)

    files: List[str] = []
    for symbol in symbols:
        ohlcv = price_data.get(symbol)
        if ohlcv is None or ohlcv.empty:
            continue

        chart_df = ohlcv.loc[start_date:end_date].copy()
        if chart_df.empty:
            continue
        chart_df = chart_df.copy()
        chart_df["EMA21"] = chart_df["Close"].ewm(span=21, adjust=False).mean()

        tx = result.transaction_log[result.transaction_log["Ticker"] == symbol].copy()
        tx["Date"] = pd.to_datetime(tx["Date"])
        buys = tx[tx["Action"] == "BUY"]
        sells = tx[tx["Action"] == "SELL"]

        fig = go.Figure()
        fig.add_trace(
            go.Candlestick(
                x=chart_df.index,
                open=chart_df["Open"],
                high=chart_df["High"],
                low=chart_df["Low"],
                close=chart_df["Close"],
                name=f"{symbol} Price",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=chart_df.index,
                y=chart_df["EMA21"],
                mode="lines",
                name="21 EMA",
                line=dict(color="#ffb703", width=1.5),
            )
        )
        if not buys.empty:
            fig.add_trace(
                go.Scatter(
                    x=buys["Date"],
                    y=buys["Price"],
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=13, color="#2ecc71", line=dict(color="black", width=1)),
                    name="Buy",
                    text=buys["Reason"],
                )
            )
        if not sells.empty:
            fig.add_trace(
                go.Scatter(
                    x=sells["Date"],
                    y=sells["Price"],
                    mode="markers",
                    marker=dict(symbol="triangle-down", size=13, color="#e63946", line=dict(color="black", width=1)),
                    name="Sell",
                    text=sells["Reason"],
                )
            )
        fig.update_layout(
            title=f"{symbol} Backtest Trades ({'technical-only' if result.config.get('technical_only') else 'full CANSLIM'})",
            template="plotly_dark",
            xaxis_title="Date",
            yaxis_title="Price",
            xaxis_rangeslider_visible=False,
        )
        file_path = os.path.join(output_dir, f"{symbol}_trade_chart.html")
        fig.write_html(file_path, include_plotlyjs="cdn")
        files.append(file_path)

    return files


def _resolve_universe(universe: str, tickers: Optional[Iterable[str]] = None) -> List[str]:
    if tickers:
        return list(dict.fromkeys(tickers))
    if universe == "sp500":
        return get_sp500_tickers(force_refresh=True)
    if universe == "nasdaq100":
        return get_all_index_tickers(indices=["nasdaq100"], force_refresh=True)
    if universe == "russell2000":
        return get_all_index_tickers(indices=["russell2000"], force_refresh=True)
    if universe == "large_cap":
        return get_all_index_tickers(indices=["sp500", "nasdaq100"], force_refresh=True)
    return DEFAULT_TICKERS


def run_cli(argv: Optional[List[str]] = None) -> SimulationResult:
    parser = argparse.ArgumentParser(description="Comprehensive CANSLIM historical backtest")
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument(
        "--universe",
        choices=["default", "sp500", "nasdaq100", "russell2000", "large_cap"],
        default="default",
    )
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--weeks", type=int, default=DEFAULT_LOOKBACK_WEEKS)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--stop", type=float, default=settings.STOP_LOSS_PCT)
    parser.add_argument("--position-size-pct", type=float, default=DEFAULT_POSITION_SIZE_PCT)
    parser.add_argument("--position-risk-pct", type=float, default=DEFAULT_POSITION_RISK_PCT)
    parser.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT_PCT)
    parser.add_argument("--scale-out-fraction", type=float, default=DEFAULT_SCALE_OUT_FRACTION)
    parser.add_argument("--stagnation-days", type=int, default=DEFAULT_STAGNATION_DAYS)
    parser.add_argument("--stagnation-threshold", type=float, default=DEFAULT_STAGNATION_THRESHOLD_PCT)
    parser.add_argument("--breakeven-trigger", type=float, default=DEFAULT_BREAKEVEN_TRIGGER_PCT)
    parser.add_argument("--signal-days", type=int, default=DEFAULT_SIGNAL_EVERY_N_DAYS)
    parser.add_argument("--min-canslim", type=float, default=float(settings.MIN_CANSLIM_SCORE))
    parser.add_argument("--min-rs", type=float, default=DEFAULT_MIN_RS_SCORE)
    parser.add_argument("--min-technical-score", type=float, default=DEFAULT_MIN_TECHNICAL_SCORE)
    parser.add_argument("--benchmark", default=BENCHMARK)
    parser.add_argument("--technical-only", action="store_true")
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument("--export-equity", action="store_true")
    parser.add_argument("--export-holdings", action="store_true")
    parser.add_argument("--export-charts", action="store_true")
    args = parser.parse_args(argv)

    tickers = _resolve_universe(args.universe, args.tickers)
    extra = [s for s in settings.EXTRA_SYMBOLS if s not in tickers]
    if extra:
        tickers.extend(extra)

    simulator = PortfolioSimulator(
        initial_capital=args.capital,
        max_positions=settings.MAX_OPEN_POSITIONS,
        position_size_pct=args.position_size_pct,
        position_risk_pct=args.position_risk_pct,
        stop_loss_pct=args.stop,
        signal_every_n_days=args.signal_days,
        min_canslim_score=args.min_canslim,
        min_rs_score=args.min_rs,
        min_technical_score=args.min_technical_score,
        require_bullish_market=True,
        technical_only=args.technical_only,
        take_profit_pct=args.take_profit,
        scale_out_fraction=args.scale_out_fraction,
        stagnation_days=args.stagnation_days,
        stagnation_threshold_pct=args.stagnation_threshold,
        breakeven_trigger_pct=args.breakeven_trigger,
        benchmark_symbol=args.benchmark,
    )

    result = simulator.run(
        tickers=tickers,
        lookback_weeks=args.weeks,
        start_date=args.start_date,
        end_date=args.end_date,
        benchmark_symbol=args.benchmark,
    )
    print_pnl_report(result)

    if not args.no_csv:
        trade_file = export_pnl_to_csv(result)
        print(f"Transaction log saved to {trade_file}")
        signal_file = export_signal_log(result)
        print(f"Signal log saved to {signal_file}")
        if args.export_holdings:
            holdings_file = export_weekly_holdings(result)
            print(f"Weekly holdings saved to {holdings_file}")
        if args.export_charts:
            chart_files = export_trade_charts(result)
            print(f"Trade charts exported: {len(chart_files)}")
    if args.export_equity:
        equity_file = export_equity_curve(result)
        print(f"Equity curve saved to {equity_file}")

    return result
