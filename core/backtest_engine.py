"""Comprehensive CANSLIM historical backtesting engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from config import settings
from backtest import (
    _calculate_rs_at_date,
    _compute_canslim_score,
    _compute_entry_composite_score,
    _download_bulk_closes,
    _download_price_data,
    _evaluate_fundamentals_at_date,
    _evaluate_market_at_date,
    _evaluate_technical_at_date,
)
from core.canslim.entry_contract import (
    MAX_BUY_ZONE_EXTENSION,
    MIN_ANNUAL_GROWTH,
    MIN_COMPOSITE_SCORE,
    MIN_CURRENT_GROWTH,
    MIN_RS_SCORE,
    MIN_VOLUME_RATIO,
    CanslimEntryFacts,
    build_entry_facts,
    evaluate_entry_contract,
)
from core.canslim.m_market_direction import MarketRegime, MarketRegimeTracker
from core.canslim.a_annual_earnings import evaluate_a
from core.canslim.c_current_earnings import evaluate_c
from core.canslim.i_institutional import evaluate_i
from core.industry_group import get_top_groups, load_industry_map
from core.data_client import clear_session_cache, fetch_bulk_ohlcv
from core.index_ticker_fetcher import get_all_index_tickers, get_sp500_tickers
from core.momentum_analysis import calculate_rs_snapshot
from core.pit_data import PITDataBundle, PriceIdentityTransitionContract
from core.trading_sessions import exact_session_row, history_through_exact_session

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
DEFAULT_MIN_RS_SCORE = MIN_RS_SCORE
DEFAULT_MIN_CANSLIM_SCORE = MIN_COMPOSITE_SCORE
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


def _calculate_rs_snapshot(
    all_closes: pd.DataFrame,
    eval_date: pd.Timestamp,
    eligible_tickers: Optional[Iterable[str]] = None,
) -> Dict[str, float]:
    """Compatibility delegate for the public causal RS snapshot."""
    return calculate_rs_snapshot(all_closes, eval_date, eligible_tickers)


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


ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION = 1
_ENTRY_OUTCOME_FIELDS = (
    "symbol",
    "signal_date",
    "entry_date",
    "pivot",
    "buy_zone_lower",
    "buy_zone_upper",
    "entry_open",
    "outcome",
)
_ENTRY_REJECTION_OUTCOMES = (
    "entry_rejected_already_open",
    "entry_rejected_capacity",
    "entry_rejected_missing_data",
    "entry_rejected_invalid_price",
    "entry_rejected_next_open_buy_zone",
    "entry_rejected_invalid_risk",
    "entry_rejected_no_cash",
)
_ENTRY_TERMINAL_OUTCOMES = ("entries_executed", *_ENTRY_REJECTION_OUTCOMES)


@dataclass(frozen=True, slots=True)
class EntryAttemptOutcome:
    """Immutable terminal result for one queued entry attempt."""

    symbol: str
    signal_date: str
    entry_date: str
    pivot: float | None
    buy_zone_lower: float | None
    buy_zone_upper: float | None
    entry_open: float | None
    outcome: str

    def to_primitive(self) -> dict[str, str | float | None]:
        """Return the stable JSON/CSV representation in schema field order."""
        return {
            "symbol": self.symbol,
            "signal_date": self.signal_date,
            "entry_date": self.entry_date,
            "pivot": self.pivot,
            "buy_zone_lower": self.buy_zone_lower,
            "buy_zone_upper": self.buy_zone_upper,
            "entry_open": self.entry_open,
            "outcome": self.outcome,
        }

    @classmethod
    def from_primitive(cls, value: object) -> "EntryAttemptOutcome":
        """Validate and restore one schema-v1 primitive outcome."""
        if (
            not isinstance(value, dict)
            or len(value) != len(_ENTRY_OUTCOME_FIELDS)
            or set(value) != set(_ENTRY_OUTCOME_FIELDS)
        ):
            raise ValueError("portfolio checkpoint entry outcome schema is invalid")
        symbol = value["symbol"]
        signal_date = value["signal_date"]
        entry_date = value["entry_date"]
        outcome = value["outcome"]
        if not all(isinstance(item, str) and item for item in (symbol, signal_date, entry_date)):
            raise ValueError("portfolio checkpoint entry outcome identity is invalid")
        if outcome not in _ENTRY_TERMINAL_OUTCOMES:
            raise ValueError("portfolio checkpoint entry outcome value is invalid")

        def optional_finite(name: str) -> float | None:
            raw = value[name]
            if raw is None:
                return None
            if isinstance(raw, bool):
                raise ValueError("portfolio checkpoint entry outcome number is invalid")
            try:
                number = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("portfolio checkpoint entry outcome number is invalid") from exc
            if not math.isfinite(number):
                raise ValueError("portfolio checkpoint entry outcome number is invalid")
            return number

        try:
            signal_date = str(pd.Timestamp(signal_date).date())
            entry_date = str(pd.Timestamp(entry_date).date())
        except (TypeError, ValueError) as exc:
            raise ValueError("portfolio checkpoint entry outcome date is invalid") from exc
        return cls(
            symbol=symbol.upper(),
            signal_date=signal_date,
            entry_date=entry_date,
            pivot=optional_finite("pivot"),
            buy_zone_lower=optional_finite("buy_zone_lower"),
            buy_zone_upper=optional_finite("buy_zone_upper"),
            entry_open=optional_finite("entry_open"),
            outcome=outcome,
        )


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
    execution_diagnostics: dict[str, int] = field(default_factory=dict)
    benchmark_symbol: str = BENCHMARK
    entry_outcomes: tuple[EntryAttemptOutcome, ...] = ()

    @property
    def signal_funnel(self) -> dict[str, int]:
        """Return bounded, content-free counts for each CANSLIM signal gate.

        The funnel is deliberately derived from ``signal_log`` rather than from
        trade outcomes.  That lets a replay distinguish an empty candidate
        universe, weak RS, market gating, breakout/volume gating, and portfolio
        capacity without exposing symbols, prices, or provider output.
        """
        columns = self.signal_log.columns

        def count_true(name: str) -> int:
            if name not in columns:
                return 0
            return int(self.signal_log[name].fillna(False).astype(bool).sum())

        def count_threshold(name: str, threshold: float) -> int:
            if name not in columns:
                return 0
            values = pd.to_numeric(self.signal_log[name], errors="coerce")
            return int((values >= threshold).fillna(False).sum())

        return {
            "evaluated_rows": int(len(self.signal_log)),
            "signal_days": int(
                self.signal_log["signal_date"].nunique()
                if "signal_date" in columns
                else 0
            ),
            "symbols_evaluated": int(
                self.signal_log["symbol"].nunique()
                if "symbol" in columns
                else 0
            ),
            "rs_pass": count_threshold("rs_score", MIN_RS_SCORE),
            "market_pass": count_true("market_is_bullish"),
            "breakout_pass": count_true("has_breakout"),
            "volume_surge_pass": count_true("has_volume_surge"),
            "buy_zone_pass": count_true("in_buy_zone"),
            "peg_pass": count_true("has_peg_today"),
            "technical_score_pass": count_threshold(
                "technical_score", self.config.get("min_technical_score", DEFAULT_MIN_TECHNICAL_SCORE)
            ),
            "buy_signal_count": count_true("buy_signal"),
            "candidate_universe_count": int(
                self.config.get("candidate_universe_count", len(self.config.get("tickers", [])))
            ),
            "rs_universe_count": int(self.config.get("rs_universe_count", 0)),
        }

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one transactional cache connection and always close it."""
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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


def _finite_signal_number(value: object) -> float | None:
    """Return a JSON-safe built-in float, or ``None`` when unavailable."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _causal_open_price(ohlcv: pd.DataFrame, eval_date: pd.Timestamp) -> float | None:
    """Return the price knowable at the session open without using that session's close."""
    current = exact_session_row(ohlcv, eval_date)
    if current is not None and "Open" in current.index:
        open_price = _finite_signal_number(current["Open"])
        if open_price is not None and open_price > 0:
            return open_price

    if "Close" not in ohlcv.columns:
        return None
    prior = ohlcv.loc[ohlcv.index < eval_date, "Close"]
    for value in prior.iloc[::-1]:
        prior_close = _finite_signal_number(value)
        if prior_close is not None and prior_close > 0:
            return prior_close
    return None


class CanslimStrategy:
    """Modular CANSLIM signal evaluation under the fixed entry contract.

    ``min_rs_score`` and ``min_canslim_score`` remain constructor-compatible
    advisory requests.  Effective qualification always uses canonical 80/70.
    """

    def __init__(
        self,
        *,
        min_c_a_growth: float = DEFAULT_MIN_C_A_GROWTH,
        min_rs_score: float = DEFAULT_MIN_RS_SCORE,
        min_canslim_score: float = DEFAULT_MIN_CANSLIM_SCORE,
        min_technical_score: float = DEFAULT_MIN_TECHNICAL_SCORE,
        require_bullish_market: bool = False,
        technical_only: bool = False,
        fundamental_provider: Optional[Callable[[str, pd.Timestamp], dict[str, Any]]] = None,
        require_proper_base: bool = False,
    ) -> None:
        self.min_c_a_growth = min_c_a_growth
        self.requested_min_rs_score = _finite_signal_number(min_rs_score)
        self.requested_min_canslim_score = _finite_signal_number(min_canslim_score)
        self.min_rs_score = MIN_RS_SCORE
        self.min_canslim_score = MIN_COMPOSITE_SCORE
        self.entry_threshold_requests_advisory_only = True
        self.min_technical_score = min_technical_score
        self.require_bullish_market = require_bullish_market
        self.technical_only = technical_only
        self.fundamental_provider = fundamental_provider
        self.require_proper_base = require_proper_base

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

        available = history_through_exact_session(tdata, eval_date)
        if available is None or len(available) < 60:
            return None

        raw_rs_score = (
            rs_score
            if rs_score is not None
            else _calculate_rs_at_date(all_closes, ticker, eval_date)
        )
        normalized_rs_score = _finite_signal_number(raw_rs_score)
        l_score = (
            normalized_rs_score / 100.0
            if normalized_rs_score is not None
            else math.nan
        )
        if self.technical_only:
            fund = {
                "current_growth": None,
                "annual_growth": None,
                "c_score": 0.0,
                "a_score": 0.0,
                "i_score": 0.5,
                "institutional_data_available": False,
                "shares_outstanding": None,
                "quarterly_income": pd.DataFrame(),
            }
        elif self.fundamental_provider is None:
            # Preserve the legacy provider's already-scored contract.  PIT
            # mode supplies raw, as-of frames through ``fundamental_provider``
            # and is scored locally below.
            fund = _evaluate_fundamentals_at_date(ticker, eval_date)
        else:
            fund_data = self.fundamental_provider(ticker, eval_date)
            qi = fund_data["quarterly_income"]
            ai = fund_data["annual_income"]
            bs = fund_data["balance_sheet"]
            info = fund_data["company_info"]
            c_score, current_growth = evaluate_c(qi)
            a_score, annual_growth, roe = evaluate_a(ai, balance_sheet=bs)
            held_pct = info.get("held_percent_institutions")
            holder_count = info.get("institution_count")
            prev_holder_count = info.get("prev_institution_count")
            i_score = evaluate_i(
                held_pct,
                num_institutional_holders=holder_count,
                prev_num_institutional_holders=prev_holder_count,
            )
            fund = {
                "c_score": c_score,
                "a_score": a_score,
                "i_score": i_score,
                "current_growth": current_growth,
                "annual_growth": annual_growth,
                "roe": roe,
                "shares_outstanding": info.get("shares_outstanding"),
                "institutional_data_available": held_pct is not None
                or (holder_count is not None and prev_holder_count is not None),
                "quarterly_income": qi,
            }
        tech = _evaluate_technical_at_date(
            available,
            eval_date,
            fund.get("shares_outstanding"),
            quarterly_income=fund.get("quarterly_income"),
            require_proper_base=self.require_proper_base,
        )
        if tech is None:
            return None

        c_growth = _finite_signal_number(fund.get("current_growth"))
        a_growth = _finite_signal_number(fund.get("annual_growth"))
        c_score = _finite_signal_number(fund.get("c_score", 0.0))
        a_score = _finite_signal_number(fund.get("a_score", 0.0))
        i_score = _finite_signal_number(fund.get("i_score", 0.5))
        n_score = _finite_signal_number(tech.get("n_score", 0.0))
        s_score = _finite_signal_number(tech.get("s_score", 0.0))
        m_score = _finite_signal_number(market_state.get("m_score"))

        def score_value(value: float | None) -> float:
            return value if value is not None else math.nan

        total_score = _compute_canslim_score(
            c=score_value(c_score),
            a=score_value(a_score),
            n=score_value(n_score),
            s=score_value(s_score),
            l_score=l_score,
            i=score_value(i_score),
            m=score_value(m_score),
            institutional_data_available=bool(fund.get("institutional_data_available", False)),
        )
        entry_composite_score = _compute_entry_composite_score(
            c=score_value(c_score),
            a=score_value(a_score),
            n=score_value(n_score),
            s=score_value(s_score),
            l_score=l_score,
            i=score_value(i_score),
            institutional_data_available=bool(fund.get("institutional_data_available", False)),
        )

        peg_details = tech.get("power_gap_details") or {}
        has_peg_today = bool(tech.get("has_power_gap")) and peg_details.get("days_ago") == 0
        entry_facts = tech.get("entry_facts")
        if not isinstance(entry_facts, CanslimEntryFacts):
            raise ValueError("technical evaluator did not return canonical entry facts")
        has_breakout = entry_facts.in_buy_zone
        has_surge = entry_facts.has_volume_surge
        in_buy_zone = entry_facts.in_buy_zone
        technical_score = self._compute_technical_score(
            n_score=score_value(n_score),
            s_score=score_value(s_score),
            l_score=l_score,
            m_score=score_value(m_score),
        )

        m_pass = bool(
            not self.require_bullish_market
            or market_state["market_is_bullish"]
            or market_state.get("cash_deployment_override", False)
        )
        if self.technical_only:
            entry_contract_eligible = entry_facts.eligible
            entry_blocking_reasons = entry_facts.blocking_reasons
        else:
            decision = evaluate_entry_contract(
                entry_facts,
                current_growth=c_growth,
                annual_growth=a_growth,
                rs_score=normalized_rs_score,
                composite_score=entry_composite_score,
            )
            c_growth = decision.current_growth
            a_growth = decision.annual_growth
            normalized_rs_score = decision.rs_score
            entry_composite_score = decision.composite_score
            entry_contract_eligible = decision.eligible
            entry_blocking_reasons = decision.blocking_reasons
        buy_signal_without_market = entry_contract_eligible
        buy_signal = bool(buy_signal_without_market and m_pass)

        if entry_facts.eligible:
            signal_reason = "Volume Breakout"
        else:
            signal_reason = "No Breakout"

        return {
            "symbol": str(ticker).upper(),
            "signal_date": str(eval_date.date()),
            "close": _finite_signal_number(tech.get("close")),
            "c_score": c_score,
            "a_score": a_score,
            "n_score": n_score,
            "s_score": s_score,
            "i_score": i_score,
            "m_score": m_score,
            "current_growth": c_growth,
            "annual_growth": a_growth,
            "rs_score": normalized_rs_score,
            "canslim_score": _finite_signal_number(total_score),
            "entry_composite_score": _finite_signal_number(entry_composite_score),
            "technical_score": _finite_signal_number(technical_score),
            "market_is_bullish": m_pass,
            "market_regime_is_bullish": bool(market_state["market_is_bullish"]),
            "buy_signal_without_market": bool(buy_signal_without_market),
            "has_breakout": has_breakout,
            "has_volume_surge": has_surge,
            "has_peg_today": has_peg_today,
            "pivot": _finite_signal_number(entry_facts.pivot),
            "prior_close": _finite_signal_number(entry_facts.prior_close),
            "event_volume": _finite_signal_number(entry_facts.event_volume),
            "prior_average_volume_50": _finite_signal_number(
                entry_facts.prior_average_volume_50
            ),
            "entry_volume_ratio": _finite_signal_number(entry_facts.volume_ratio),
            "entry_extension": _finite_signal_number(entry_facts.extension),
            "price_advanced": entry_facts.price_advanced,
            "in_buy_zone": in_buy_zone,
            "technical_setup_eligible": entry_facts.eligible,
            "technical_blocking_reasons": ",".join(entry_facts.blocking_reasons),
            "entry_contract_eligible": entry_contract_eligible,
            "entry_blocking_reasons": ",".join(entry_blocking_reasons),
            "buy_signal": buy_signal,
            "signal_reason": signal_reason,
            "technical_only": self.technical_only,
        }


def _new_execution_diagnostics() -> dict[str, int]:
    return {
        "signal_days": 0,
        "entries_allowed_days": 0,
        "blocked_by_regime_days": 0,
        "blocked_by_market_days": 0,
        "cash_deployment_override_days": 0,
        "buy_signal_rows": 0,
        "potential_buy_signal_rows": 0,
        "potential_buy_signal_rows_blocked_by_market": 0,
        "buy_signal_rows_when_entries_allowed": 0,
        "buy_signal_rows_blocked_by_regime": 0,
        "buy_signal_rows_blocked_by_market": 0,
        "buy_signal_rows_blocked_by_both": 0,
        "buy_signal_rows_when_cash_override": 0,
        "capacity_truncated_signals": 0,
        "entry_attempts": 0,
        "entries_executed": 0,
        "entry_rejected_already_open": 0,
        "entry_rejected_capacity": 0,
        "entry_rejected_missing_data": 0,
        "entry_rejected_invalid_price": 0,
        "entry_rejected_next_open_buy_zone": 0,
        "entry_rejected_invalid_risk": 0,
        "entry_rejected_no_cash": 0,
        "eviction_attempts": 0,
        "evictions_executed": 0,
        "eviction_rejections": 0,
    }


_PORTFOLIO_CHECKPOINT_SCHEMA = 3
_BUILTIN_CANSLIM_STRATEGY_CHECKPOINT_VERSION = 1
_MISSING_CHECKPOINT_IDENTITY = object()


def _checkpoint_json_safe(value: Any) -> Any:
    """Convert simulator state to JSON without serializing executable objects."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return _checkpoint_json_safe(value.item())
    if isinstance(value, np.ndarray):
        return [_checkpoint_json_safe(item) for item in value.tolist()]
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _checkpoint_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_checkpoint_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    raise ValueError(f"unsupported checkpoint value type: {type(value).__name__}")


def _checkpoint_bytes(value: Any) -> bytes:
    return (
        json.dumps(_checkpoint_json_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _append_checkpoint_jsonl(path: Path, value: Any, *, stream: Any = None, sync: bool = False) -> int:
    payload = _checkpoint_bytes(value)
    if stream is None:
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
            return handle.tell()
    stream.write(payload)
    stream.flush()
    if sync:
        os.fsync(stream.fileno())
    return stream.tell()


def _write_checkpoint_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = _checkpoint_bytes(value)
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_checkpoint_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("portfolio checkpoint must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("portfolio checkpoint is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != _PORTFOLIO_CHECKPOINT_SCHEMA:
        raise ValueError("portfolio checkpoint schema is unsupported")
    return value


def _checkpoint_origin_advisory_request(
    checkpoint: dict[str, Any],
    field: str,
) -> float | None:
    """Return one normalized origin request from a schema-v3 checkpoint."""
    if field not in checkpoint:
        raise ValueError("portfolio checkpoint origin advisory request is missing")
    raw = checkpoint[field]
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("portfolio checkpoint origin advisory request is invalid")
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError("portfolio checkpoint origin advisory request is invalid")
    return number


def _strategy_checkpoint_identity(
    simulator: "PortfolioSimulator",
    *,
    require_explicit_custom: bool,
) -> dict[str, Any]:
    """Return stable strategy provenance without serializing executable state."""

    strategy = simulator.strategy
    strategy_type = type(strategy)
    module = str(strategy_type.__module__)
    qualname = str(strategy_type.__qualname__)
    if not simulator._strategy_was_injected and strategy_type is CanslimStrategy:
        return {
            "kind": "built_in",
            "module": module,
            "qualname": qualname,
            "version": _BUILTIN_CANSLIM_STRATEGY_CHECKPOINT_VERSION,
        }
    if not require_explicit_custom:
        return {
            "kind": "custom",
            "module": module,
            "qualname": qualname,
        }

    explicit = getattr(strategy, "checkpoint_identity", _MISSING_CHECKPOINT_IDENTITY)
    if explicit is not _MISSING_CHECKPOINT_IDENTITY and callable(explicit):
        try:
            explicit = explicit()
        except Exception as exc:
            raise ValueError(
                "custom strategy checkpoint_identity could not be evaluated"
            ) from exc
    if explicit is _MISSING_CHECKPOINT_IDENTITY or explicit is None:
        raise ValueError(
            "custom strategy checkpointing requires an explicit JSON-safe "
            "checkpoint_identity"
        )
    else:
        try:
            encoded = json.dumps(
                explicit,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            normalized_identity = json.loads(encoded)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "custom strategy checkpoint_identity must be JSON-safe"
            ) from exc

    return {
        "kind": "custom",
        "module": module,
        "qualname": qualname,
        "checkpoint_identity": normalized_identity,
    }


def _read_checkpoint_state(path: Path, *, offset: int, next_day_index: int) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("portfolio state log is missing")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("portfolio state log offset is invalid")
    outputs: dict[str, list[Any]] = {
        "equity": [], "benchmark": [], "transactions": [], "weekly": [], "signals": [],
        "entry_outcomes": [],
    }
    expected_day = 0
    consumed = 0
    with path.open("rb") as stream:
        while consumed < offset:
            line = stream.readline()
            if not line:
                raise ValueError("portfolio state log is shorter than its checkpoint")
            consumed += len(line)
            if consumed > offset:
                raise ValueError("portfolio checkpoint splits a state log record")
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("portfolio state log contains invalid JSON") from exc
            if not isinstance(event, dict):
                raise ValueError("portfolio state log record is not an object")
            kind = event.get("kind")
            if kind == "day":
                day_idx = event.get("day_index")
                if day_idx != expected_day or day_idx >= next_day_index:
                    raise ValueError("portfolio state log day sequence does not match checkpoint")
                expected_day += 1
                outputs["equity"].append({"date": event["date"], "equity": event["equity"]})
                benchmark = event.get("benchmark")
                if benchmark is not None:
                    outputs["benchmark"].append({"date": event["date"], "equity": benchmark})
                outputs["transactions"].extend(event.get("transactions", []))
                outputs["weekly"].extend(event.get("weekly", []))
                outputs["signals"].extend(event.get("signals", []))
                outputs["entry_outcomes"].extend(event.get("entry_outcomes", []))
            elif kind == "final":
                outputs["transactions"].extend(event.get("transactions", []))
            else:
                raise ValueError("portfolio state log contains an unknown record")
    if expected_day != next_day_index:
        raise ValueError("portfolio state log is missing checkpointed days")
    return outputs


def _portfolio_checkpoint_fingerprint(
    *,
    bundle_sha256: Optional[str],
    code_identity: Optional[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    benchmark: str,
    universe: Iterable[str],
    simulator: "PortfolioSimulator",
    strategy_identity: Optional[dict[str, Any]] = None,
) -> str:
    config = {
        "schema_version": _PORTFOLIO_CHECKPOINT_SCHEMA,
        "bundle_sha256": bundle_sha256,
        "code_identity": code_identity,
        "strategy_identity": (
            strategy_identity
            if strategy_identity is not None
            else _strategy_checkpoint_identity(
                simulator,
                require_explicit_custom=False,
            )
        ),
        "identity_prices_provenance_sha256": (
            simulator.identity_transition_contract.prices_provenance_sha256
            if simulator.identity_transition_contract is not None else None
        ),
        "identity_request_contracts_sha256": (
            simulator.identity_transition_contract.request_contracts_sha256
            if simulator.identity_transition_contract is not None else None
        ),
        "start_date": str(start_date.date()),
        "end_date": str(end_date.date()),
        "benchmark": benchmark,
        "universe": sorted(str(item).upper() for item in universe),
        "initial_capital": simulator.initial_capital,
        "max_positions": simulator.max_positions,
        "position_size_pct": simulator.position_size_pct,
        "position_risk_pct": simulator.position_risk_pct,
        "stop_loss_pct": simulator.stop_loss_pct,
        "ma_exit_period": simulator.ma_exit_period,
        "ma_consecutive": simulator.ma_consecutive,
        "signal_every_n_days": simulator.signal_every_n_days,
        "min_canslim_score": MIN_COMPOSITE_SCORE,
        "min_rs_score": MIN_RS_SCORE,
        "min_technical_score": simulator.min_technical_score,
        "require_bullish_market": simulator.require_bullish_market,
        "use_stateful_regime_gate": simulator.use_stateful_regime_gate,
        "cash_deployment_threshold_pct": simulator.cash_deployment_threshold_pct,
        "technical_only": simulator.technical_only,
        "take_profit_pct": simulator.take_profit_pct,
        "scale_out_fraction": simulator.scale_out_fraction,
        "stagnation_days": simulator.stagnation_days,
        "stagnation_threshold_pct": simulator.stagnation_threshold_pct,
        "breakeven_trigger_pct": simulator.breakeven_trigger_pct,
        "enable_eviction": simulator.enable_eviction,
    }
    return hashlib.sha256(_checkpoint_bytes(config)).hexdigest()


def _regime_checkpoint_state(tracker: MarketRegimeTracker) -> dict[str, Any]:
    return {
        "regime": tracker.regime.value,
        "dist_day_bars": list(tracker._dist_day_bars),
        "bar_count": tracker._bar_count,
        "rally_day_count": tracker._rally_day_count,
        "rally_active": tracker._rally_active,
        "correction_low": tracker._correction_low,
    }


def _restore_regime_checkpoint(state: dict[str, Any]) -> MarketRegimeTracker:
    try:
        tracker = MarketRegimeTracker()
        tracker.regime = MarketRegime(str(state["regime"]))
        tracker._dist_day_bars = [int(item) for item in state["dist_day_bars"]]
        tracker._bar_count = int(state["bar_count"])
        tracker._rally_day_count = int(state["rally_day_count"])
        tracker._rally_active = bool(state["rally_active"])
        correction_low = state["correction_low"]
        tracker._correction_low = float("inf") if correction_low is None else float(correction_low)
        return tracker
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("portfolio checkpoint regime state is invalid") from exc


def _trade_checkpoint_dict(trade: Trade) -> dict[str, Any]:
    return _checkpoint_json_safe(trade.__dict__)


def _trade_from_checkpoint(value: Any) -> Trade:
    if not isinstance(value, dict):
        raise ValueError("portfolio checkpoint trade is invalid")
    try:
        return Trade(**value)
    except (TypeError, ValueError) as exc:
        raise ValueError("portfolio checkpoint trade fields are invalid") from exc


class PortfolioSimulator:
    """Simulate the full CANSLIM portfolio lifecycle.

    Legacy RS/composite request arguments are retained as JSON-safe advisory
    metadata and cannot alter signals, results, or checkpoint identity.
    """

    def __init__(
        self,
        initial_capital: float = DEFAULT_CAPITAL,
        # Backtests should evaluate every eligible signal by default.  The
        # live execution guard in config.settings remains unchanged; callers
        # can still pass an explicit cap for a concentration experiment.
        max_positions: Optional[int] = None,
        position_size_pct: float = DEFAULT_POSITION_SIZE_PCT,
        position_risk_pct: float = DEFAULT_POSITION_RISK_PCT,
        stop_loss_pct: float = settings.STOP_LOSS_PCT,
        ma_exit_period: int = DEFAULT_MA_EXIT_PERIOD,
        ma_consecutive: int = DEFAULT_MA_CONSECUTIVE,
        signal_every_n_days: int = DEFAULT_SIGNAL_EVERY_N_DAYS,
        min_canslim_score: float = DEFAULT_MIN_CANSLIM_SCORE,
        min_rs_score: float = DEFAULT_MIN_RS_SCORE,
        min_technical_score: float = DEFAULT_MIN_TECHNICAL_SCORE,
        require_bullish_market: bool = False,
        use_stateful_regime_gate: bool = False,
        cash_deployment_threshold_pct: Optional[float] = None,
        technical_only: bool = False,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        scale_out_fraction: float = DEFAULT_SCALE_OUT_FRACTION,
        stagnation_days: int = DEFAULT_STAGNATION_DAYS,
        stagnation_threshold_pct: float = DEFAULT_STAGNATION_THRESHOLD_PCT,
        breakeven_trigger_pct: float = DEFAULT_BREAKEVEN_TRIGGER_PCT,
        data_fetcher: Optional[DataFetcher] = None,
        strategy: Optional[CanslimStrategy] = None,
        benchmark_symbol: str = BENCHMARK,
        enable_eviction: bool = settings.ENABLE_EVICTION,
        pit_bundle: Optional[PITDataBundle] = None,
        identity_transition_contract: Optional[PriceIdentityTransitionContract] = None,
    ) -> None:
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.position_risk_pct = position_risk_pct
        self.stop_loss_pct = stop_loss_pct
        self.ma_exit_period = ma_exit_period
        self.ma_consecutive = ma_consecutive
        self.signal_every_n_days = signal_every_n_days
        self.requested_min_canslim_score = _finite_signal_number(min_canslim_score)
        self.requested_min_rs_score = _finite_signal_number(min_rs_score)
        self.min_canslim_score = MIN_COMPOSITE_SCORE
        self.min_rs_score = MIN_RS_SCORE
        self.entry_threshold_requests_advisory_only = True
        self.min_technical_score = min_technical_score
        self.require_bullish_market = require_bullish_market
        self.use_stateful_regime_gate = use_stateful_regime_gate
        if cash_deployment_threshold_pct is not None and not 0.0 <= cash_deployment_threshold_pct <= 1.0:
            raise ValueError("cash_deployment_threshold_pct must be between 0 and 1")
        self.cash_deployment_threshold_pct = cash_deployment_threshold_pct
        self.technical_only = technical_only
        self.pit_bundle = pit_bundle
        self.require_proper_base = bool(pit_bundle is not None and not technical_only)
        self.identity_transition_contract = identity_transition_contract
        self.take_profit_pct = take_profit_pct
        self.scale_out_fraction = scale_out_fraction
        self.stagnation_days = stagnation_days
        self.stagnation_threshold_pct = stagnation_threshold_pct
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.data_fetcher = data_fetcher or DataFetcher()
        self._strategy_was_injected = strategy is not None
        self.strategy = (
            strategy
            if strategy is not None
            else CanslimStrategy(
                min_rs_score=min_rs_score,
                min_canslim_score=min_canslim_score,
                min_technical_score=min_technical_score,
                require_bullish_market=require_bullish_market,
                technical_only=technical_only,
                fundamental_provider=(
                    pit_bundle.fundamentals_provider if pit_bundle is not None else None
                ),
                require_proper_base=self.require_proper_base,
            )
        )
        try:
            self.strategy.min_rs_score = MIN_RS_SCORE
            self.strategy.min_canslim_score = MIN_COMPOSITE_SCORE
            self.strategy.entry_threshold_requests_advisory_only = True
            self.strategy.require_proper_base = self.require_proper_base
        except Exception as exc:
            raise ValueError(
                "supplied strategy cannot honor the fixed canonical entry thresholds"
            ) from exc
        if pit_bundle is not None and not technical_only:
            # A custom strategy is still bound to the immutable bundle.  This
            # prevents a caller from silently falling back to today's provider.
            self.strategy.fundamental_provider = pit_bundle.fundamentals_provider
        self.benchmark_symbol = benchmark_symbol
        self.enable_eviction = enable_eviction

        self._equity: float = initial_capital
        self._open_positions: Dict[str, Trade] = {}
        self._trades: List[Trade] = []
        self._transactions: List[dict] = []
        self._weekly_snapshots: List[dict] = []
        self._signal_rows: List[dict] = []
        self._entry_outcomes: List[EntryAttemptOutcome] = []
        self._ticker_industry: Dict[str, str] = {}
        self._execution_diagnostics = _new_execution_diagnostics()
        self._pending_entries_remaining = 0

    def run(
        self,
        tickers: List[str],
        lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        benchmark_symbol: Optional[str] = None,
        checkpoint_path: Optional[str | Path] = None,
        progress_log_path: Optional[str | Path] = None,
        resume: bool = False,
        checkpoint_every_days: int = 20,
        checkpoint_code_identity: Optional[str] = None,
    ) -> SimulationResult:
        if checkpoint_every_days < 1:
            raise ValueError("checkpoint_every_days must be positive")
        if resume and checkpoint_path is None:
            raise ValueError("resume requires checkpoint_path")
        self._equity = self.initial_capital
        self._open_positions = {}
        self._trades = []
        self._transactions = []
        self._weekly_snapshots = []
        self._signal_rows = []
        self._entry_outcomes = []
        self._execution_diagnostics = _new_execution_diagnostics()
        self._pending_entries_remaining = 0

        clear_session_cache()
        benchmark = str(benchmark_symbol or self.benchmark_symbol).upper()
        start_ts, end_ts = _resolve_window(
            start_date=start_date,
            end_date=end_date,
            lookback_weeks=lookback_weeks,
        )

        if self.pit_bundle is not None:
            bundle_symbols = set(self.pit_bundle.symbols())
            requested = [str(ticker).upper() for ticker in tickers] or list(self.pit_bundle.symbols())
            tickers = list(
                dict.fromkeys(
                    ticker for ticker in requested if ticker in bundle_symbols and ticker != benchmark
                )
            )
            if not tickers:
                raise ValueError("point-in-time bundle has no requested candidate symbols")
            all_tickers = list(dict.fromkeys([*tickers, benchmark]))
        else:
            all_tickers = list(dict.fromkeys([*tickers, benchmark]))
        universe = list(self.pit_bundle.symbols()) if self.pit_bundle is not None else list(dict.fromkeys([*tickers, *get_sp500_tickers()]))
        checkpoint = Path(checkpoint_path).resolve() if checkpoint_path is not None else None
        progress = Path(progress_log_path).resolve() if progress_log_path is not None else None
        state_log = checkpoint.with_name("portfolio_state.jsonl") if checkpoint is not None else None
        strategy_identity = _strategy_checkpoint_identity(
            self,
            require_explicit_custom=checkpoint is not None,
        )
        fingerprint = _portfolio_checkpoint_fingerprint(
            bundle_sha256=self.pit_bundle.sha256 if self.pit_bundle is not None else None,
            code_identity=checkpoint_code_identity,
            start_date=start_ts,
            end_date=end_ts,
            benchmark=benchmark,
            universe=all_tickers,
            simulator=self,
            strategy_identity=strategy_identity,
        )
        checkpoint_state: Optional[dict[str, Any]] = None
        origin_requested_min_rs_score = self.requested_min_rs_score
        origin_requested_min_canslim_score = self.requested_min_canslim_score
        restored_outputs: dict[str, list[Any]] = {
            "equity": [], "benchmark": [], "transactions": [], "weekly": [], "signals": [],
            "entry_outcomes": [],
        }
        if checkpoint is not None and resume:
            checkpoint_state = _load_checkpoint_json(checkpoint)
            if (
                checkpoint_state.get("entry_outcome_schema_version")
                != ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION
            ):
                raise ValueError("portfolio checkpoint entry outcome schema is unsupported")
            if checkpoint_state.get("strategy_identity") != strategy_identity:
                raise ValueError(
                    "portfolio checkpoint was produced by a different strategy identity"
                )
            if checkpoint_state.get("fingerprint") != fingerprint:
                raise ValueError("portfolio checkpoint does not match the requested run")
            if checkpoint_state.get("code_identity") != checkpoint_code_identity:
                raise ValueError("portfolio checkpoint was produced by a different code revision")
            origin_requested_min_rs_score = _checkpoint_origin_advisory_request(
                checkpoint_state, "origin_requested_min_rs_score"
            )
            origin_requested_min_canslim_score = _checkpoint_origin_advisory_request(
                checkpoint_state, "origin_requested_min_canslim_score"
            )
            if state_log is None:
                raise ValueError("portfolio checkpoint state log is not configured")
            restored_outputs = _read_checkpoint_state(
                state_log,
                offset=int(checkpoint_state["state_log_offset"]),
                next_day_index=int(checkpoint_state["next_day_index"]),
            )
            if checkpoint_state.get("completed"):
                _append_checkpoint_jsonl(progress or checkpoint.with_name("portfolio_progress.jsonl"), {
                    "phase": "resume_cached_result",
                    "next_day_index": checkpoint_state["next_day_index"],
                    "total_days": checkpoint_state["total_days"],
                    "fingerprint": fingerprint,
                }, sync=True)
                return self._result_from_checkpoint(checkpoint_state, restored_outputs, benchmark)
        elif checkpoint is not None and checkpoint.exists():
            raise ValueError("checkpoint already exists; pass resume=True to continue it")

        if state_log is not None:
            state_log.parent.mkdir(parents=True, exist_ok=True)
            if resume:
                with state_log.open("r+b") as handle:
                    handle.truncate(int(checkpoint_state["state_log_offset"]))
            else:
                state_log.touch(exist_ok=False)
        state_stream = state_log.open("ab") if state_log is not None else None
        started_at = datetime.now().timestamp()
        if progress is not None:
            _append_checkpoint_jsonl(progress, {
                "phase": "resumed" if resume else "started",
                "fingerprint": fingerprint,
                "start_date": str(start_ts.date()),
                "end_date": str(end_ts.date()),
                "universe_count": len(universe),
            }, sync=True)

        if self.pit_bundle is not None:
            print(f"Reading point-in-time price data for {len(all_tickers)} tickers...")
            ticker_ohlcv = self.pit_bundle.fetch_price_data(all_tickers, start_ts, end_ts)
        else:
            print(f"Downloading price data for {len(all_tickers)} tickers...")
            ticker_ohlcv = self.data_fetcher.fetch_price_data(all_tickers, start_ts, end_ts)
        if benchmark not in ticker_ohlcv:
            if state_stream is not None:
                state_stream.close()
            print(f"FATAL: Could not download {benchmark} benchmark data.")
            return SimulationResult()

        if self.pit_bundle is not None:
            print(f"Reading point-in-time RS closes for {len(universe)} symbols...")
            all_closes = self.pit_bundle.fetch_closes(universe, start_ts, end_ts)
        else:
            print(f"Downloading RS universe closes for {len(universe)} tickers...")
            all_closes = self.data_fetcher.fetch_rs_universe_closes(universe, start_ts, end_ts)

        benchmark_df = ticker_ohlcv[benchmark]
        trading_days = benchmark_df.loc[start_ts:end_ts].index
        if len(trading_days) < 30:
            if state_stream is not None:
                state_stream.close()
            print("ERROR: Not enough trading days in range.")
            return SimulationResult()

        self._ticker_industry = {} if self.technical_only or self.pit_bundle is not None else load_industry_map(tickers)
        if checkpoint_state is not None:
            next_day_index = int(checkpoint_state["next_day_index"])
            if next_day_index < 0 or next_day_index > len(trading_days):
                raise ValueError("portfolio checkpoint day index is outside the requested window")
            regime_tracker = _restore_regime_checkpoint(checkpoint_state["regime"])
            self._regime_tracker = regime_tracker
            self._equity = float(checkpoint_state["equity"])
            self._open_positions = {
                str(symbol): _trade_from_checkpoint(value)
                for symbol, value in checkpoint_state["open_positions"].items()
            }
            self._trades = [_trade_from_checkpoint(value) for value in checkpoint_state["trades"]]
            self._transactions = list(restored_outputs["transactions"])
            self._weekly_snapshots = list(restored_outputs["weekly"])
            self._signal_rows = list(restored_outputs["signals"])
            restored_entry_outcomes = tuple(
                EntryAttemptOutcome.from_primitive(value)
                for value in restored_outputs["entry_outcomes"]
            )
            checkpoint_entry_outcomes = tuple(
                EntryAttemptOutcome.from_primitive(value)
                for value in checkpoint_state["entry_outcomes"]
            )
            if restored_entry_outcomes != checkpoint_entry_outcomes:
                raise ValueError("portfolio checkpoint entry outcomes disagree with state log")
            self._entry_outcomes = list(restored_entry_outcomes)
            self._execution_diagnostics = {
                str(key): int(value)
                for key, value in checkpoint_state["execution_diagnostics"].items()
            }
            benchmark_start_price = checkpoint_state.get("benchmark_start_price")
            benchmark_start_price = (
                float(benchmark_start_price) if benchmark_start_price is not None else None
            )
            pending_entries = list(checkpoint_state["pending_entries"])
            equity_series = {
                str(row["date"]): float(row["equity"])
                for row in restored_outputs["equity"]
            }
            benchmark_series = {
                str(row["date"]): float(row["equity"])
                for row in restored_outputs["benchmark"]
            }
        else:
            regime_tracker = MarketRegimeTracker()
            regime_tracker.bootstrap(benchmark_df, start_ts)
            self._regime_tracker = regime_tracker
            next_day_index = 0
            equity_series = {}
            benchmark_series = {}
            benchmark_start_price = None
            pending_entries = []

        total_days = len(trading_days)
        for day_idx, eval_date in enumerate(trading_days[next_day_index:], start=next_day_index):
            signal_start = len(self._signal_rows)
            outcome_start = len(self._entry_outcomes)
            transaction_start = len(self._transactions)
            weekly_start = len(self._weekly_snapshots)
            if day_idx > 0:
                hist = benchmark_df.loc[:eval_date]
                if len(hist) >= 2:
                    prev_bar = hist.iloc[-2]
                    curr_bar = hist.iloc[-1]
                    regime_tracker.update(
                        date=eval_date,
                        close=float(curr_bar["Close"]),
                        prev_close=float(prev_bar["Close"]),
                        volume=float(curr_bar["Volume"]),
                        prev_volume=float(prev_bar["Volume"]),
                    )

            date_str = str(eval_date.date())

            self._apply_identity_transitions(ticker_ohlcv, eval_date)

            for pending_idx, pending in enumerate(pending_entries):
                self._pending_entries_remaining = len(pending_entries) - pending_idx
                self._enter_position(pending, ticker_ohlcv, eval_date)
            self._pending_entries_remaining = 0
            pending_entries = []

            for symbol in list(self._open_positions.keys()):
                ohlcv = ticker_ohlcv.get(symbol)
                if ohlcv is not None:
                    self._check_exits(symbol, ohlcv, eval_date)

            is_signal_day = day_idx % self.signal_every_n_days == 0
            market_state = self.strategy.evaluate_market(benchmark_df, eval_date)
            if is_signal_day:
                active_tickers = tickers
                if self.pit_bundle is not None:
                    active_members = self.pit_bundle.members_at(eval_date)
                    active_tickers = [ticker for ticker in tickers if ticker in active_members]
                pending_entries = self._evaluate_signals(
                    tickers=active_tickers,
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

            if state_stream is not None:
                date_key = str(eval_date.date())
                offset = _append_checkpoint_jsonl(state_log, {
                    "kind": "day",
                    "day_index": day_idx,
                    "date": date_key,
                    "equity": equity_series[date_key],
                    "benchmark": benchmark_series.get(date_key),
                    "signals": self._signal_rows[signal_start:],
                    "entry_outcomes": [
                        outcome.to_primitive()
                        for outcome in self._entry_outcomes[outcome_start:]
                    ],
                    "transactions": self._transactions[transaction_start:],
                    "weekly": self._weekly_snapshots[weekly_start:],
                }, stream=state_stream, sync=False)
                checkpoint_due = (
                    (day_idx + 1) % checkpoint_every_days == 0
                    or day_idx == total_days - 1
                )
                if checkpoint_due:
                    state_stream.flush()
                    os.fsync(state_stream.fileno())
                    checkpoint_payload = self._checkpoint_payload(
                        fingerprint=fingerprint,
                        code_identity=checkpoint_code_identity,
                        strategy_identity=strategy_identity,
                        next_day_index=day_idx + 1,
                        total_days=total_days,
                        state_log_offset=offset,
                        regime_tracker=regime_tracker,
                        pending_entries=pending_entries,
                        benchmark_start_price=benchmark_start_price,
                        origin_requested_min_rs_score=origin_requested_min_rs_score,
                        origin_requested_min_canslim_score=(
                            origin_requested_min_canslim_score
                        ),
                    )
                    _write_checkpoint_json(checkpoint, checkpoint_payload)
                    if progress is not None:
                        _append_checkpoint_jsonl(progress, {
                            "phase": "checkpoint",
                            "day_index": day_idx,
                            "date": date_key,
                            "next_day_index": day_idx + 1,
                            "total_days": total_days,
                            "percent": round((day_idx + 1) * 100.0 / total_days, 3),
                            "elapsed_seconds": round(datetime.now().timestamp() - started_at, 3),
                            "open_positions": len(self._open_positions),
                            "signal_rows": len(self._signal_rows),
                            "transactions": len(self._transactions),
                        }, sync=True)

        last_date = pd.Timestamp(trading_days[-1])
        final_transaction_start = len(self._transactions)
        for symbol in list(self._open_positions.keys()):
            ohlcv = ticker_ohlcv.get(symbol)
            if ohlcv is None:
                continue
            bar = ohlcv.loc[:last_date]
            if not bar.empty:
                exit_price = float(bar["Close"].iloc[-1])
                self._close_trade(symbol, exit_price, "end_of_test", str(last_date.date()))

        if state_stream is not None:
            final_offset = _append_checkpoint_jsonl(state_log, {
                "kind": "final",
                "transactions": self._transactions[final_transaction_start:],
            }, stream=state_stream, sync=True)
        else:
            final_offset = 0
        result_config = self._result_config(
            tickers=tickers,
            benchmark=benchmark,
            all_closes=all_closes,
            start_ts=start_ts,
            end_ts=end_ts,
            requested_entry_floors=(
                origin_requested_min_rs_score,
                origin_requested_min_canslim_score,
            ),
        )
        result = SimulationResult(
            trades=self._trades,
            equity_curve=pd.Series(equity_series),
            benchmark_curve=pd.Series(benchmark_series),
            initial_capital=self.initial_capital,
            config=result_config,
            transaction_log=pd.DataFrame(self._transactions),
            weekly_holdings=pd.DataFrame(self._weekly_snapshots),
            signal_log=pd.DataFrame(self._signal_rows),
            execution_diagnostics=dict(self._execution_diagnostics),
            entry_outcomes=tuple(self._entry_outcomes),
            benchmark_symbol=benchmark,
        )
        if checkpoint is not None:
            _write_checkpoint_json(
                checkpoint,
                self._checkpoint_payload(
                    fingerprint=fingerprint,
                    code_identity=checkpoint_code_identity,
                    strategy_identity=strategy_identity,
                    next_day_index=total_days,
                    total_days=total_days,
                    state_log_offset=final_offset,
                    regime_tracker=regime_tracker,
                    pending_entries=[],
                    benchmark_start_price=benchmark_start_price,
                    origin_requested_min_rs_score=origin_requested_min_rs_score,
                    origin_requested_min_canslim_score=(
                        origin_requested_min_canslim_score
                    ),
                    completed=True,
                    result_config=result_config,
                ),
            )
            if progress is not None:
                _append_checkpoint_jsonl(progress, {
                    "phase": "completed",
                    "next_day_index": total_days,
                    "total_days": total_days,
                    "percent": 100.0,
                    "elapsed_seconds": round(datetime.now().timestamp() - started_at, 3),
                    "open_positions": len(self._open_positions),
                    "signal_rows": len(self._signal_rows),
                    "transactions": len(self._transactions),
                }, sync=True)
        if state_stream is not None:
            state_stream.close()
        return result

    def _result_config(
        self,
        *,
        tickers: list[str],
        benchmark: str,
        all_closes: pd.DataFrame,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        requested_entry_floors: tuple[float | None, float | None] | None = None,
    ) -> dict[str, Any]:
        requested_min_rs_score, requested_min_canslim_score = (
            requested_entry_floors
            if requested_entry_floors is not None
            else (
                self.requested_min_rs_score,
                self.requested_min_canslim_score,
            )
        )
        return {
            "tickers": tickers,
            "candidate_universe_count": len(tickers),
            "rs_universe_count": len(all_closes.columns),
            "benchmark_symbol": benchmark,
            "max_positions": self.max_positions,
            "require_bullish_market": self.require_bullish_market,
            "use_stateful_regime_gate": self.use_stateful_regime_gate,
            "cash_deployment_threshold_pct": self.cash_deployment_threshold_pct,
            "position_size_pct": self.position_size_pct,
            "position_risk_pct": self.position_risk_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "ma_exit_period": self.ma_exit_period,
            "ma_consecutive": self.ma_consecutive,
            "signal_every_n_days": self.signal_every_n_days,
            "min_canslim_score": self.min_canslim_score,
            "min_rs_score": self.min_rs_score,
            "requested_min_canslim_score": requested_min_canslim_score,
            "requested_min_rs_score": requested_min_rs_score,
            "entry_threshold_requests_advisory_only": (
                self.entry_threshold_requests_advisory_only
            ),
            "min_technical_score": self.min_technical_score,
            "entry_contract_min_current_growth": MIN_CURRENT_GROWTH,
            "entry_contract_min_annual_growth": MIN_ANNUAL_GROWTH,
            "entry_contract_min_rs_score": MIN_RS_SCORE,
            "entry_contract_min_composite_score": MIN_COMPOSITE_SCORE,
            "entry_contract_min_volume_ratio": MIN_VOLUME_RATIO,
            "entry_contract_max_buy_zone_extension": MAX_BUY_ZONE_EXTENSION,
            "technical_only": self.technical_only,
            "data_mode": "point_in_time" if self.pit_bundle is not None else "provider_cache",
            "pit_bundle_sha256": self.pit_bundle.sha256 if self.pit_bundle is not None else None,
            "pit_data_cutoff": str(self.pit_bundle.data_cutoff.date()) if self.pit_bundle is not None else None,
            "pit_manifest": self.pit_bundle.manifest() if self.pit_bundle is not None else None,
            "take_profit_pct": self.take_profit_pct,
            "scale_out_fraction": self.scale_out_fraction,
            "stagnation_days": self.stagnation_days,
            "stagnation_threshold_pct": self.stagnation_threshold_pct,
            "breakeven_trigger_pct": self.breakeven_trigger_pct,
            "industry_group_top_n": settings.INDUSTRY_GROUP_TOP_N,
            "start_date": str(start_ts.date()),
            "end_date": str(end_ts.date()),
        }

    def _checkpoint_payload(
        self,
        *,
        fingerprint: str,
        code_identity: Optional[str],
        strategy_identity: dict[str, Any],
        next_day_index: int,
        total_days: int,
        state_log_offset: int,
        regime_tracker: MarketRegimeTracker,
        pending_entries: list[dict],
        benchmark_start_price: Optional[float],
        origin_requested_min_rs_score: float | None,
        origin_requested_min_canslim_score: float | None,
        completed: bool = False,
        result_config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _PORTFOLIO_CHECKPOINT_SCHEMA,
            "fingerprint": fingerprint,
            "code_identity": code_identity,
            "strategy_identity": strategy_identity,
            "next_day_index": next_day_index,
            "total_days": total_days,
            "state_log_offset": state_log_offset,
            "completed": completed,
            "equity": self._equity,
            "open_positions": {
                symbol: _trade_checkpoint_dict(trade)
                for symbol, trade in self._open_positions.items()
            },
            "trades": [_trade_checkpoint_dict(trade) for trade in self._trades],
            "execution_diagnostics": self._execution_diagnostics,
            "entry_outcome_schema_version": ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION,
            "entry_outcomes": [outcome.to_primitive() for outcome in self._entry_outcomes],
            "pending_entries": pending_entries,
            "benchmark_start_price": benchmark_start_price,
            "origin_requested_min_rs_score": origin_requested_min_rs_score,
            "origin_requested_min_canslim_score": (
                origin_requested_min_canslim_score
            ),
            "regime": _regime_checkpoint_state(regime_tracker),
        }
        if result_config is not None:
            payload["result_config"] = result_config
        return _checkpoint_json_safe(payload)

    def _result_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
        outputs: dict[str, list[Any]],
        benchmark: str,
    ) -> SimulationResult:
        if not checkpoint.get("completed") or "result_config" not in checkpoint:
            raise ValueError("portfolio checkpoint does not contain a completed result")
        if checkpoint.get("entry_outcome_schema_version") != ENTRY_ATTEMPT_OUTCOME_SCHEMA_VERSION:
            raise ValueError("portfolio checkpoint entry outcome schema is unsupported")
        origin_requested_min_rs_score = _checkpoint_origin_advisory_request(
            checkpoint, "origin_requested_min_rs_score"
        )
        origin_requested_min_canslim_score = _checkpoint_origin_advisory_request(
            checkpoint, "origin_requested_min_canslim_score"
        )
        result_config = checkpoint["result_config"]
        if (
            result_config.get("requested_min_rs_score")
            != origin_requested_min_rs_score
            or result_config.get("requested_min_canslim_score")
            != origin_requested_min_canslim_score
        ):
            raise ValueError(
                "completed checkpoint result config disagrees with origin advisory requests"
            )
        checkpoint_outcomes = tuple(
            EntryAttemptOutcome.from_primitive(value)
            for value in checkpoint["entry_outcomes"]
        )
        journal_outcomes = tuple(
            EntryAttemptOutcome.from_primitive(value)
            for value in outputs["entry_outcomes"]
        )
        if checkpoint_outcomes != journal_outcomes:
            raise ValueError("completed checkpoint entry outcomes disagree with state log")
        equity = pd.Series(
            [float(row["equity"]) for row in outputs["equity"]],
            index=pd.to_datetime([row["date"] for row in outputs["equity"]]),
            dtype=float,
        )
        benchmark_curve = pd.Series(
            [float(row["equity"]) for row in outputs["benchmark"]],
            index=pd.to_datetime([row["date"] for row in outputs["benchmark"]]),
            dtype=float,
        )
        return SimulationResult(
            trades=[_trade_from_checkpoint(value) for value in checkpoint["trades"]],
            equity_curve=equity,
            benchmark_curve=benchmark_curve,
            initial_capital=self.initial_capital,
            config=result_config,
            transaction_log=pd.DataFrame(outputs["transactions"]),
            weekly_holdings=pd.DataFrame(outputs["weekly"]),
            signal_log=pd.DataFrame(outputs["signals"]),
            execution_diagnostics={
                str(key): int(value)
                for key, value in checkpoint["execution_diagnostics"].items()
            },
            entry_outcomes=checkpoint_outcomes,
            benchmark_symbol=benchmark,
        )

    def _canonicalize_signal_row(
        self,
        *,
        row: dict[str, Any],
        ticker: str,
        ticker_history: pd.DataFrame,
        eval_date: pd.Timestamp,
        market_allowed: bool,
        market_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Rebuild the authoritative entry decision at the execution boundary."""

        available = history_through_exact_session(ticker_history, eval_date)
        if available is None:
            closes: Iterable[object] = ()
            volumes: Iterable[object] = ()
        else:
            closes = available["Close"] if "Close" in available.columns else ()
            volumes = available["Volume"] if "Volume" in available.columns else ()
        if self.require_proper_base:
            facts = build_entry_facts(
                closes,
                volumes,
                history_before_event=available.iloc[:-1] if available is not None else None,
                event_session=eval_date,
                require_proper_base=True,
            )
        else:
            facts = build_entry_facts(closes, volumes)

        canonical = dict(row)
        if self.technical_only:
            current_growth = _finite_signal_number(canonical.get("current_growth"))
            annual_growth = _finite_signal_number(canonical.get("annual_growth"))
            rs_score = _finite_signal_number(canonical.get("rs_score"))
            composite_score = _finite_signal_number(
                canonical.get("entry_composite_score")
            )
            entry_eligible = facts.eligible
            entry_blocking_reasons = facts.blocking_reasons
        else:
            decision = evaluate_entry_contract(
                facts,
                current_growth=canonical.get("current_growth"),
                annual_growth=canonical.get("annual_growth"),
                rs_score=canonical.get("rs_score"),
                composite_score=canonical.get("entry_composite_score"),
            )
            current_growth = decision.current_growth
            annual_growth = decision.annual_growth
            rs_score = decision.rs_score
            composite_score = decision.composite_score
            entry_eligible = decision.eligible
            entry_blocking_reasons = decision.blocking_reasons

        canonical.update(
            {
                "symbol": str(ticker).upper(),
                "signal_date": str(eval_date.date()),
                "close": _finite_signal_number(facts.event_close),
                "current_growth": current_growth,
                "annual_growth": annual_growth,
                "rs_score": rs_score,
                "entry_composite_score": composite_score,
                "market_is_bullish": bool(market_allowed),
                "market_regime_is_bullish": bool(
                    market_state.get("market_is_bullish", False)
                ),
                "buy_signal_without_market": bool(entry_eligible),
                "has_breakout": facts.in_buy_zone,
                "has_volume_surge": facts.has_volume_surge,
                "pivot": _finite_signal_number(facts.pivot),
                "prior_close": _finite_signal_number(facts.prior_close),
                "event_volume": _finite_signal_number(facts.event_volume),
                "prior_average_volume_50": _finite_signal_number(
                    facts.prior_average_volume_50
                ),
                "entry_volume_ratio": _finite_signal_number(facts.volume_ratio),
                "entry_extension": _finite_signal_number(facts.extension),
                "price_advanced": facts.price_advanced,
                "in_buy_zone": facts.in_buy_zone,
                "technical_setup_eligible": facts.eligible,
                "technical_blocking_reasons": ",".join(facts.blocking_reasons),
                "entry_contract_eligible": bool(entry_eligible),
                "entry_blocking_reasons": ",".join(entry_blocking_reasons),
                "buy_signal": bool(entry_eligible and market_allowed),
                "technical_only": self.technical_only,
            }
        )
        canonical["signal_reason"] = (
            "Volume Breakout" if facts.eligible else "No Breakout"
        )
        return canonical

    def _evaluate_signals(
        self,
        *,
        tickers: List[str],
        ticker_ohlcv: Dict[str, pd.DataFrame],
        all_closes: pd.DataFrame,
        eval_date: pd.Timestamp,
        market_state: dict,
    ) -> List[dict]:
        self._execution_diagnostics["signal_days"] += 1
        regime_allowed = bool(self._regime_tracker.allows_entries)
        cash_override = False
        if (
            self.require_bullish_market
            and
            self.cash_deployment_threshold_pct is not None
            and not market_state["market_is_bullish"]
        ):
            total_equity = self._mark_equity(ticker_ohlcv, eval_date)
            cash_ratio = self._equity / total_equity if total_equity > 0 else 0.0
            cash_override = cash_ratio >= self.cash_deployment_threshold_pct
            if cash_override:
                self._execution_diagnostics["cash_deployment_override_days"] += 1
        effective_market_state = dict(market_state)
        effective_market_state["cash_deployment_override"] = cash_override
        market_allowed = bool(
            not self.require_bullish_market
            or market_state["market_is_bullish"]
            or cash_override
        )
        if self.use_stateful_regime_gate and not regime_allowed:
            self._execution_diagnostics["blocked_by_regime_days"] += 1
        if not market_allowed:
            self._execution_diagnostics["blocked_by_market_days"] += 1
        entries_allowed = market_allowed and (
            not self.use_stateful_regime_gate or regime_allowed
        )
        if entries_allowed:
            self._execution_diagnostics["entries_allowed_days"] += 1

        signals: List[dict] = []
        rs_eligible = None
        if self.pit_bundle is not None:
            rs_eligible = self.pit_bundle.members_at(eval_date) - {self.benchmark_symbol.upper()}
        rs_snapshot = _calculate_rs_snapshot(all_closes, eval_date, eligible_tickers=rs_eligible)
        top_groups = get_top_groups(rs_snapshot, self._ticker_industry)
        for ticker in tickers:
            if ticker in self._open_positions or ticker not in ticker_ohlcv:
                continue
            ticker_group = self._ticker_industry.get(ticker)
            if top_groups and ticker_group is not None and ticker_group not in top_groups:
                continue

            row = self.strategy.evaluate_symbol(
                ticker=ticker,
                ticker_ohlcv=ticker_ohlcv,
                all_closes=all_closes,
                eval_date=eval_date,
                market_state=effective_market_state,
                rs_score=rs_snapshot.get(ticker),
            )
            if row is None:
                continue
            row = self._canonicalize_signal_row(
                row=row,
                ticker=ticker,
                ticker_history=ticker_ohlcv[ticker],
                eval_date=eval_date,
                market_allowed=market_allowed,
                market_state=market_state,
            )
            self._signal_rows.append(row)
            if row.get("buy_signal_without_market", row.get("buy_signal", False)):
                self._execution_diagnostics["potential_buy_signal_rows"] += 1
                if (
                    self.require_bullish_market
                    and not row.get("market_regime_is_bullish", market_state["market_is_bullish"])
                    and not cash_override
                ):
                    self._execution_diagnostics["potential_buy_signal_rows_blocked_by_market"] += 1
            if row["buy_signal"]:
                self._execution_diagnostics["buy_signal_rows"] += 1
                if entries_allowed:
                    self._execution_diagnostics["buy_signal_rows_when_entries_allowed"] += 1
                    if cash_override:
                        self._execution_diagnostics["buy_signal_rows_when_cash_override"] += 1
                elif not regime_allowed and not market_allowed:
                    self._execution_diagnostics["buy_signal_rows_blocked_by_both"] += 1
                elif not regime_allowed:
                    self._execution_diagnostics["buy_signal_rows_blocked_by_regime"] += 1
                elif not market_allowed:
                    self._execution_diagnostics["buy_signal_rows_blocked_by_market"] += 1
            if entries_allowed and row["buy_signal"]:
                signals.append(row)

        if not entries_allowed:
            return []

        signals.sort(
            key=lambda item: (
                _finite_signal_number(item.get("canslim_score")) or -math.inf,
                _finite_signal_number(item.get("rs_score")) or -math.inf,
            ),
            reverse=True,
        )
        if self.max_positions is None:
            candidate_limit = len(signals)
        else:
            open_slots = max(self.max_positions - len(self._open_positions), 0)
            candidate_limit = open_slots
            if candidate_limit == 0 and self.enable_eviction and self.max_positions > 0:
                candidate_limit = 1
        self._execution_diagnostics["capacity_truncated_signals"] += max(
            len(signals) - candidate_limit,
            0,
        )
        return signals[:candidate_limit]

    def _try_evict(
        self,
        new_signal: dict,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
    ) -> bool:
        """Two-pass eviction: free a slot for a higher-RS new signal.

        Pass 1: evict an underwater position (open-time price < entry) with lower RS.
        Pass 2: evict any position with lower RS if pass 1 finds nothing.
        Returns True if a position was evicted.
        """
        if not self.enable_eviction:
            return False

        self._execution_diagnostics["eviction_attempts"] += 1

        new_rs = new_signal.get("rs_score", 0.0)

        losers: list = []
        fallback: list = []
        for sym, trade in self._open_positions.items():
            if trade.rs_score >= new_rs:
                continue
            ohlcv = ticker_ohlcv.get(sym)
            open_price = _causal_open_price(ohlcv, eval_date) if ohlcv is not None else None
            if open_price is None:
                continue  # data gap guard
            fallback.append((sym, trade, open_price))
            if open_price < trade.entry_price:
                losers.append((sym, trade, open_price))

        pool = losers if losers else fallback
        if not pool:
            self._execution_diagnostics["eviction_rejections"] += 1
            return False

        evict_sym, _, evict_price = min(pool, key=lambda x: x[1].rs_score)
        self._close_trade(evict_sym, evict_price, "evicted", str(eval_date.date()))
        self._execution_diagnostics["evictions_executed"] += 1
        return True

    def _enter_position(
        self,
        signal: dict,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        entry_date: pd.Timestamp,
    ) -> None:
        self._execution_diagnostics["entry_attempts"] += 1
        symbol = str(signal["symbol"]).upper()
        entry_date_text = str(entry_date.date())
        raw_signal_date = signal.get("signal_date", entry_date)
        try:
            signal_date = str(pd.Timestamp(raw_signal_date).date())
        except (TypeError, ValueError):
            # Direct callers may omit the diagnostic signal date; execution is
            # still causally bound to the supplied entry session.
            signal_date = entry_date_text
        pivot: float | None = None
        raw_pivot = signal.get("pivot")
        try:
            candidate_pivot = float(raw_pivot)
        except (TypeError, ValueError, OverflowError):
            candidate_pivot = math.nan
        if math.isfinite(candidate_pivot) and candidate_pivot > 0:
            pivot = candidate_pivot
        buy_zone_lower = pivot
        buy_zone_upper = (
            pivot * (1 + MAX_BUY_ZONE_EXTENSION) if pivot is not None else None
        )
        entry_open: float | None = None

        def finish(outcome: str) -> None:
            self._execution_diagnostics[outcome] += 1
            self._entry_outcomes.append(
                EntryAttemptOutcome(
                    symbol=symbol,
                    signal_date=signal_date,
                    entry_date=entry_date_text,
                    pivot=pivot,
                    buy_zone_lower=buy_zone_lower,
                    buy_zone_upper=buy_zone_upper,
                    entry_open=entry_open,
                    outcome=outcome,
                )
            )

        if symbol in self._open_positions:
            finish("entry_rejected_already_open")
            return

        ohlcv = ticker_ohlcv.get(symbol)
        if ohlcv is None:
            finish("entry_rejected_missing_data")
            return

        bar = exact_session_row(ohlcv, entry_date)
        if bar is None or "Open" not in bar.index:
            finish("entry_rejected_missing_data")
            return
        raw_entry_price = bar["Open"]
        if raw_entry_price is None or bool(pd.isna(raw_entry_price)):
            finish("entry_rejected_missing_data")
            return

        try:
            entry_price = float(raw_entry_price)
        except (TypeError, ValueError, OverflowError):
            finish("entry_rejected_invalid_price")
            return
        if not math.isfinite(entry_price) or entry_price <= 0:
            finish("entry_rejected_invalid_price")
            return
        entry_open = entry_price

        if pivot is not None and not (pivot <= entry_price <= buy_zone_upper):
            finish("entry_rejected_next_open_buy_zone")
            return

        needs_capacity_eviction = (
            self.max_positions is not None
            and len(self._open_positions) >= self.max_positions
        )
        if not needs_capacity_eviction and self._equity <= 0:
            finish("entry_rejected_no_cash")
            return

        total_portfolio_value = self._mark_open_equity(ticker_ohlcv, entry_date)
        # Risk-based sizing: risk exactly position_risk_pct of portfolio per trade.
        # shares = (portfolio * risk_pct) / (entry * stop_pct)
        # position_value = shares * entry = portfolio * risk_pct / stop_pct
        risk_amount = total_portfolio_value * self.position_risk_pct
        risk_per_share = entry_price * self.stop_loss_pct
        if not math.isfinite(risk_per_share) or risk_per_share <= 0:
            finish("entry_rejected_invalid_risk")
            return
        target_position_value = risk_amount / risk_per_share * entry_price
        if not math.isfinite(target_position_value) or target_position_value <= 0:
            finish("entry_rejected_invalid_risk")
            return

        # Validate the replacement's price and risk before an eviction can
        # mutate the portfolio. A fully invested portfolio may still rotate a
        # valid replacement and use the cash released by that eviction.
        if needs_capacity_eviction and not self._try_evict(
            signal, ticker_ohlcv, entry_date
        ):
            finish("entry_rejected_capacity")
            return

        if self._equity <= 0:
            finish("entry_rejected_no_cash")
            return

        if self.max_positions is None and self._pending_entries_remaining > 1:
            # In uncapped backtests, do not let early-ranked signals consume
            # all cash and starve valid same-day signals.  Spread available
            # cash over the pending batch without using leverage; each trade
            # remains at or below its configured risk target.
            batch_allocation = self._equity / self._pending_entries_remaining
            position_value = min(self._equity, target_position_value, batch_allocation)
        else:
            position_value = min(self._equity, target_position_value)
        if not math.isfinite(position_value) or position_value <= 0:
            finish("entry_rejected_invalid_risk")
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
        finish("entries_executed")

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

        gain_pct = (close - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # Release 8-week hold after 40 trading days
        if trade.eight_week_hold and trade.days_held >= 40:
            trade.eight_week_hold = False
            trade.scale_out_tier = 0

        # Detect super-winner: 20%+ gain within first 3 weeks (15 trading days)
        if not trade.eight_week_hold and trade.days_held <= 15 and gain_pct >= 0.20:
            trade.eight_week_hold = True

        if low <= trade.stop_price:
            self._close_trade(symbol, trade.stop_price, "stop_loss", date_str)
            return

        if not trade.eight_week_hold and (trade.remaining_qty or 0.0) > 0:
            tiers = settings.SCALE_OUT_TIERS
            while trade.scale_out_tier < len(tiers):
                gain_target, fraction = tiers[trade.scale_out_tier]
                tier_price = trade.entry_price * (1 + gain_target)
                if high < tier_price:
                    break
                sell_qty = trade.qty * fraction
                if sell_qty > 0 and (trade.remaining_qty or 0.0) >= sell_qty:
                    self._scale_out_trade(symbol, tier_price, date_str, "take_profit_scale_out", sell_qty=sell_qty)
                trade.scale_out_tier += 1
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

    def _apply_identity_transitions(
        self,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
    ) -> None:
        contract = self.identity_transition_contract
        if contract is None:
            return
        effective = eval_date.date()
        for transition in contract.transitions:
            if transition.effective_date != effective:
                continue
            trade = self._open_positions.pop(transition.predecessor, None)
            if trade is None:
                continue
            if transition.successor in self._open_positions:
                raise ValueError("identity transition successor is already open")
            successor_frame = ticker_ohlcv.get(transition.successor)
            if successor_frame is None:
                raise ValueError("identity transition successor has no price data")
            successor_bar = successor_frame.loc[eval_date:eval_date]
            if successor_bar.empty or float(successor_bar["Open"].iloc[0]) <= 0:
                raise ValueError("identity transition successor lacks a valid transition bar")
            trade.symbol = transition.successor
            self._open_positions[transition.successor] = trade
            quantity = float(trade.remaining_qty or 0.0)
            if quantity > 1e-12:
                self._transactions.append({
                    "Date": str(eval_date.date()),
                    "Ticker": transition.successor,
                    "FromTicker": transition.predecessor,
                    "Action": "TRANSFER",
                    "Price": round(float(successor_bar["Open"].iloc[0]), 4),
                    "Quantity": round(quantity, 6),
                    "Value": 0.0,
                    "Reason": "pit_identity_transfer",
                })

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

        remaining_qty = max(float(trade.remaining_qty or 0.0), 0.0)
        proceeds = exit_price * remaining_qty
        self._equity += proceeds
        if remaining_qty > 1e-12:
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

    def _mark_open_equity(
        self,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
    ) -> float:
        market_value = self._equity
        for symbol, trade in self._open_positions.items():
            ohlcv = ticker_ohlcv.get(symbol)
            price = _causal_open_price(ohlcv, eval_date) if ohlcv is not None else None
            if price is not None:
                market_value += price * (trade.remaining_qty or 0.0)
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
    print(
        "M-gate:           "
        + ("required" if cfg.get("require_bullish_market", False) else "diagnostic only")
    )
    print(f"Stop-loss:        {cfg.get('stop_loss_pct', settings.STOP_LOSS_PCT) * 100:.1f}%")
    print(f"Take-profit:      {cfg.get('take_profit_pct', DEFAULT_TAKE_PROFIT_PCT) * 100:.1f}%")
    print(f"Time stop:        {cfg.get('stagnation_days', DEFAULT_STAGNATION_DAYS)} days")
    print(f"Breakeven trigger:{cfg.get('breakeven_trigger_pct', DEFAULT_BREAKEVEN_TRIGGER_PCT) * 100:>11.1f}%")
    print(f"RS floor (fixed): {cfg.get('min_rs_score', MIN_RS_SCORE)}")
    if cfg.get("technical_only"):
        print("Mode:             technical-only")
        print(f"Technical floor:  {cfg.get('min_technical_score', DEFAULT_MIN_TECHNICAL_SCORE)}")
    else:
        print("Mode:             full CANSLIM")
        print(
            "CANSLIM floor (fixed): "
            f"{cfg.get('min_canslim_score', MIN_COMPOSITE_SCORE)}"
        )
    if cfg.get("entry_threshold_requests_advisory_only"):
        print(
            "Legacy floor requests (ignored): "
            f"RS={cfg.get('requested_min_rs_score')}, "
            f"composite={cfg.get('requested_min_canslim_score')}"
        )

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

    funnel = result.signal_funnel
    print("\n--- Signal Funnel ---")
    print(
        "Evaluated rows:    "
        f"{funnel['evaluated_rows']} across {funnel['symbols_evaluated']} symbols / "
        f"{funnel['signal_days']} signal days"
    )
    print(
        "RS universe:       "
        f"{funnel['rs_universe_count']} | candidate universe: "
        f"{funnel['candidate_universe_count']}"
    )
    print(
        "RS / market / breakout / volume: "
        f"{funnel['rs_pass']} / {funnel['market_pass']} / "
        f"{funnel['breakout_pass']} / {funnel['volume_surge_pass']}"
    )
    print(
        "Buy-zone / PEG / technical / buy: "
        f"{funnel['buy_zone_pass']} / {funnel['peg_pass']} / "
        f"{funnel['technical_score_pass']} / {funnel['buy_signal_count']}"
    )
    execution = result.execution_diagnostics
    if execution:
        print("\n--- Execution Diagnostics ---")
        print(
            "Signal days / entry-allowed days: "
            f"{execution.get('signal_days', 0)} / "
            f"{execution.get('entries_allowed_days', 0)}"
        )
        print(
            "Buy signals / entry-eligible signals: "
            f"{execution.get('buy_signal_rows', 0)} / "
            f"{execution.get('buy_signal_rows_when_entries_allowed', 0)}"
        )
        print(
            "Potential buys before M gate / blocked by M gate: "
            f"{execution.get('potential_buy_signal_rows', 0)} / "
            f"{execution.get('potential_buy_signal_rows_blocked_by_market', 0)}"
        )
        if cfg.get("cash_deployment_threshold_pct") is not None:
            print(
                "Cash override days / admitted signals: "
                f"{execution.get('cash_deployment_override_days', 0)} / "
                f"{execution.get('buy_signal_rows_when_cash_override', 0)}"
            )
        print(
            "Entry attempts / executed: "
            f"{execution.get('entry_attempts', 0)} / "
            f"{execution.get('entries_executed', 0)}"
        )
        print(
            "Capacity-truncated / capacity-rejected: "
            f"{execution.get('capacity_truncated_signals', 0)} / "
            f"{execution.get('entry_rejected_capacity', 0)}"
        )
        print(
            "No-cash / invalid-risk rejections: "
            f"{execution.get('entry_rejected_no_cash', 0)} / "
            f"{execution.get('entry_rejected_invalid_risk', 0)}"
        )

    entry_outcome_counts: dict[str, int] = {}
    for entry_outcome in result.entry_outcomes:
        entry_outcome_counts[entry_outcome.outcome] = (
            entry_outcome_counts.get(entry_outcome.outcome, 0) + 1
        )
    print("\n--- Entry Attempt Outcomes ---")
    if entry_outcome_counts:
        for outcome, count in sorted(entry_outcome_counts.items()):
            print(f"{outcome}: {count}")
    else:
        print("No entry attempts recorded.")

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
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help="maximum simultaneous positions; omit for uncapped backtests",
    )
    parser.add_argument("--stop", type=float, default=settings.STOP_LOSS_PCT)
    parser.add_argument("--position-size-pct", type=float, default=DEFAULT_POSITION_SIZE_PCT)
    parser.add_argument("--position-risk-pct", type=float, default=DEFAULT_POSITION_RISK_PCT)
    parser.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT_PCT)
    parser.add_argument("--scale-out-fraction", type=float, default=DEFAULT_SCALE_OUT_FRACTION)
    parser.add_argument("--stagnation-days", type=int, default=DEFAULT_STAGNATION_DAYS)
    parser.add_argument("--stagnation-threshold", type=float, default=DEFAULT_STAGNATION_THRESHOLD_PCT)
    parser.add_argument("--breakeven-trigger", type=float, default=DEFAULT_BREAKEVEN_TRIGGER_PCT)
    parser.add_argument("--signal-days", type=int, default=DEFAULT_SIGNAL_EVERY_N_DAYS)
    parser.add_argument(
        "--stateful-regime-gate",
        action="store_true",
        help="also require the stateful O'Neil correction tracker to allow entries",
    )
    parser.add_argument(
        "--require-bullish-market",
        action="store_true",
        help="opt in to the O'Neil M-gate; otherwise valid entries execute when cash is available",
    )
    parser.add_argument(
        "--allow-non-bullish-entries",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--cash-deployment-threshold",
        type=float,
        default=None,
        help="fraction of cash above which non-bullish signals may be admitted (0-1)",
    )
    parser.add_argument(
        "--min-canslim",
        type=float,
        default=DEFAULT_MIN_CANSLIM_SCORE,
        help=(
            "deprecated advisory; ignored for entry qualification, which uses "
            "fixed canonical composite 70"
        ),
    )
    parser.add_argument(
        "--min-rs",
        type=float,
        default=DEFAULT_MIN_RS_SCORE,
        help=(
            "deprecated advisory; ignored for entry qualification, which uses "
            "fixed canonical RS 80"
        ),
    )
    parser.add_argument("--min-technical-score", type=float, default=DEFAULT_MIN_TECHNICAL_SCORE)
    parser.add_argument("--benchmark", default=BENCHMARK)
    parser.add_argument("--technical-only", action="store_true")
    parser.add_argument(
        "--pit-bundle",
        default=None,
        help="validated SQLite point-in-time bundle for offline price data (and fundamentals when not technical-only)",
    )
    parser.add_argument(
        "--pit-bundle-sha256",
        default=None,
        help="lowercase SHA-256 digest for --pit-bundle",
    )
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument("--export-equity", action="store_true")
    parser.add_argument("--export-holdings", action="store_true")
    parser.add_argument("--export-charts", action="store_true")
    args = parser.parse_args(argv)

    pit_bundle: Optional[PITDataBundle] = None
    if args.pit_bundle:
        if not args.pit_bundle_sha256:
            parser.error("--pit-bundle-sha256 is required with --pit-bundle")
        pit_bundle = PITDataBundle(args.pit_bundle, expected_sha256=args.pit_bundle_sha256)
        tickers = list(args.tickers or pit_bundle.symbols())
    else:
        tickers = _resolve_universe(args.universe, args.tickers)
        extra = [s for s in settings.EXTRA_SYMBOLS if s not in tickers]
        if extra:
            tickers.extend(extra)

    simulator = PortfolioSimulator(
        initial_capital=args.capital,
        max_positions=args.max_positions,
        position_size_pct=args.position_size_pct,
        position_risk_pct=args.position_risk_pct,
        stop_loss_pct=args.stop,
        signal_every_n_days=args.signal_days,
        min_canslim_score=args.min_canslim,
        min_rs_score=args.min_rs,
        min_technical_score=args.min_technical_score,
        require_bullish_market=args.require_bullish_market and not args.allow_non_bullish_entries,
        use_stateful_regime_gate=args.stateful_regime_gate,
        cash_deployment_threshold_pct=args.cash_deployment_threshold,
        technical_only=args.technical_only,
        take_profit_pct=args.take_profit,
        scale_out_fraction=args.scale_out_fraction,
        stagnation_days=args.stagnation_days,
        stagnation_threshold_pct=args.stagnation_threshold,
        breakeven_trigger_pct=args.breakeven_trigger,
        benchmark_symbol=args.benchmark,
        pit_bundle=pit_bundle,
    )

    try:
        result = simulator.run(
            tickers=tickers,
            lookback_weeks=args.weeks,
            start_date=args.start_date,
            end_date=args.end_date,
            benchmark_symbol=args.benchmark,
        )
    finally:
        if pit_bundle is not None:
            pit_bundle.close()
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
