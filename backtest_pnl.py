"""CANSLIM Auto-Trader PnL Backtest.

Simulates the full auto_trader entry/exit cycle against historical price data
to measure realized P&L, win rate, drawdown, and Sharpe ratio.

Simulation rules mirror auto_trader.py exactly:
  Entry  — next-day open after a CANSLIM buy signal fires (no look-ahead bias)
  Stop   — 7% below entry price; triggered when daily LOW crosses the stop price
  MA exit— 2 consecutive daily closes below the 21-day EMA of the position
  EOT    — positions still open at the end of the test are closed at the final close

Position sizing, max-positions cap, and stop-loss percentage come from
config/settings.py (POSITION_SIZE_PCT, MAX_OPEN_POSITIONS, STOP_LOSS_PCT).

Signal frequency is configurable (default: weekly = every 5 trading days) to
keep FMP fundamental API calls within free-tier limits.

Usage:
    python backtest_pnl.py
    python backtest_pnl.py --tickers NVDA CRWD MU --weeks 52 --capital 100000
"""

from __future__ import annotations

import sys
import os
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from core.data_client import clear_session_cache
from core.index_ticker_fetcher import get_sp500_tickers


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_TICKERS = ["CRWD", "NVDA", "MU", "GEV", "VRT", "VST"]
DEFAULT_CAPITAL = 100_000.0
DEFAULT_LOOKBACK_WEEKS = 104  # ~2 years
DEFAULT_SIGNAL_EVERY_N_DAYS = 5  # evaluate CANSLIM weekly
DEFAULT_MA_EXIT_PERIOD = 21  # 21-day EMA for MA-violation exit
DEFAULT_MA_CONSECUTIVE = 2  # consecutive closes below EMA to trigger exit
BENCHMARK = "SPY"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    """Record of a single completed (or open) position."""

    symbol: str
    entry_date: str
    entry_price: float
    qty: float
    stop_price: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "stop_loss" | "ma_violation" | "end_of_test"
    canslim_score: float = 0.0
    rs_score: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.qty


@dataclass
class SimulationResult:
    """Full results from a PnL simulation run."""

    trades: List[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    benchmark_curve: pd.Series = field(default_factory=pd.Series)
    initial_capital: float = DEFAULT_CAPITAL
    config: dict = field(default_factory=dict)

    # --- Computed properties ---

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
        if not self.equity_curve.empty:
            return (self.equity_curve.iloc[-1] / self.equity_curve.iloc[0] - 1) * 100
        return 0.0

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
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        rolling_max = self.equity_curve.cummax()
        drawdown = (self.equity_curve - rolling_max) / rolling_max
        return float(drawdown.min() * 100)

    @property
    def sharpe_ratio(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        daily_returns = self.equity_curve.pct_change().dropna()
        if daily_returns.std() == 0:
            return 0.0
        return float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))

    @property
    def benchmark_return_pct(self) -> float:
        if len(self.benchmark_curve) < 2:
            return 0.0
        return (self.benchmark_curve.iloc[-1] / self.benchmark_curve.iloc[0] - 1) * 100

    @property
    def exit_reason_counts(self) -> dict:
        counts: dict = {}
        for t in self.closed_trades:
            reason = t.exit_reason or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Portfolio simulator
# ---------------------------------------------------------------------------


class PortfolioSimulator:
    """Simulates the auto_trader entry/exit cycle against historical price data."""

    def __init__(
        self,
        initial_capital: float = DEFAULT_CAPITAL,
        max_positions: int = settings.MAX_OPEN_POSITIONS,
        position_size_pct: float = settings.POSITION_SIZE_PCT,
        stop_loss_pct: float = settings.STOP_LOSS_PCT,
        ma_exit_period: int = DEFAULT_MA_EXIT_PERIOD,
        ma_consecutive: int = DEFAULT_MA_CONSECUTIVE,
        signal_every_n_days: int = DEFAULT_SIGNAL_EVERY_N_DAYS,
        min_canslim_score: float = 40.0,
        min_rs_score: float = float(settings.MIN_RS_SCORE),
        require_bullish_market: bool = False,
    ) -> None:
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.stop_loss_pct = stop_loss_pct
        self.ma_exit_period = ma_exit_period
        self.ma_consecutive = ma_consecutive
        self.signal_every_n_days = signal_every_n_days
        self.min_canslim_score = min_canslim_score
        self.min_rs_score = min_rs_score
        self.require_bullish_market = require_bullish_market

        # Runtime state — reset on each run()
        self._equity: float = initial_capital
        self._open_positions: Dict[str, Trade] = {}
        self._trades: List[Trade] = []

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def run(
        self,
        tickers: List[str],
        lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    ) -> SimulationResult:
        """Run the full PnL simulation.

        Args:
            tickers: Symbols to evaluate for CANSLIM signals.
            lookback_weeks: How many weeks of history to simulate.

        Returns:
            SimulationResult with trades, equity curve, and statistics.
        """
        self._equity = self.initial_capital
        self._open_positions = {}
        self._trades = []

        clear_session_cache()

        period = f"{max(lookback_weeks // 4 + 6, 18)}mo"
        print(f"Downloading price data (period={period})...")
        all_tickers = list(set(tickers + [BENCHMARK]))
        ticker_ohlcv = _download_price_data(all_tickers, period=period)

        if BENCHMARK not in ticker_ohlcv:
            print("FATAL: Could not download SPY benchmark data.")
            return SimulationResult()

        print("Downloading S&P 500 closes for RS ranking...")
        sp500 = get_sp500_tickers()
        all_rs_tickers = list(set(tickers + sp500))
        all_closes = _download_bulk_closes(all_rs_tickers, period=period)
        print(f"  RS universe: {len(all_closes.columns)} tickers")

        spy_df = ticker_ohlcv[BENCHMARK]
        end_date = datetime.now()
        start_date = end_date - timedelta(weeks=lookback_weeks)

        trading_days = spy_df.loc[start_date:end_date].index
        if len(trading_days) < 30:
            print("ERROR: Not enough trading days in range.")
            return SimulationResult()

        print(f"\nSimulating {len(trading_days)} trading days "
              f"({trading_days[0].date()} to {trading_days[-1].date()})...")

        # Equity curve: one entry per trading day
        equity_series: Dict[str, float] = {}
        benchmark_series: Dict[str, float] = {}
        spy_start_price: Optional[float] = None

        for day_idx, eval_date in enumerate(trading_days):
            date_str = str(eval_date.date())

            # --- 1. Check exits on open positions ---
            for symbol in list(self._open_positions.keys()):
                if symbol not in ticker_ohlcv:
                    continue
                ohlcv = ticker_ohlcv[symbol]
                self._check_exits(symbol, ohlcv, eval_date, all_tickers, ticker_ohlcv)

            # --- 2. Check for pending entries (signal from previous day) ---
            if hasattr(self, "_pending_entries"):
                for pending in self._pending_entries:
                    self._enter_position(pending, ticker_ohlcv, eval_date)
                del self._pending_entries

            # --- 3. Evaluate CANSLIM signals on signal days ---
            is_signal_day = (day_idx % self.signal_every_n_days == 0)
            if is_signal_day:
                self._pending_entries = self._evaluate_signals(
                    tickers, ticker_ohlcv, all_closes, eval_date
                )

            # --- 4. Mark equity (cash + open position market value) ---
            market_value = self._equity
            for symbol, trade in self._open_positions.items():
                if symbol in ticker_ohlcv:
                    ohlcv = ticker_ohlcv[symbol]
                    bar = ohlcv.loc[:eval_date]
                    if not bar.empty:
                        current_price = float(bar["Close"].iloc[-1])
                        market_value += current_price * trade.qty - trade.cost_basis
            equity_series[date_str] = market_value

            # Benchmark: normalize SPY to starting capital
            spy_bar = spy_df.loc[:eval_date]
            if not spy_bar.empty:
                spy_price = float(spy_bar["Close"].iloc[-1])
                if spy_start_price is None:
                    spy_start_price = spy_price
                benchmark_series[date_str] = (spy_price / spy_start_price) * self.initial_capital

        # --- 4. Force-close any remaining open positions at last close ---
        last_date = pd.Timestamp(trading_days[-1])
        for symbol in list(self._open_positions.keys()):
            trade = self._open_positions[symbol]
            if symbol in ticker_ohlcv:
                ohlcv = ticker_ohlcv[symbol]
                bar = ohlcv.loc[:last_date]
                if not bar.empty:
                    exit_price = float(bar["Close"].iloc[-1])
                    self._close_trade(symbol, exit_price, "end_of_test",
                                      str(last_date.date()))

        equity_curve = pd.Series(equity_series)
        benchmark_curve = pd.Series(benchmark_series)

        return SimulationResult(
            trades=self._trades,
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            initial_capital=self.initial_capital,
            config={
                "tickers": tickers,
                "lookback_weeks": lookback_weeks,
                "max_positions": self.max_positions,
                "position_size_pct": self.position_size_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "ma_exit_period": self.ma_exit_period,
                "min_canslim_score": self.min_canslim_score,
                "min_rs_score": self.min_rs_score,
            },
        )

    # -----------------------------------------------------------------------
    # Signal evaluation
    # -----------------------------------------------------------------------

    def _evaluate_signals(
        self,
        tickers: List[str],
        ticker_ohlcv: Dict[str, pd.DataFrame],
        all_closes: pd.DataFrame,
        eval_date: pd.Timestamp,
    ) -> List[dict]:
        """Return list of pending entry dicts for tickers that fired a buy signal."""
        spy_df = ticker_ohlcv.get(BENCHMARK, pd.DataFrame())
        if spy_df.empty:
            return []

        m_score, m_bullish, _, _ = _evaluate_market_at_date(spy_df, eval_date)

        if self.require_bullish_market and not m_bullish:
            return []

        signals = []
        for ticker in tickers:
            if ticker in self._open_positions:
                continue  # already in position
            if ticker not in ticker_ohlcv:
                continue

            tdata = ticker_ohlcv[ticker]
            available = tdata.loc[:eval_date]
            if len(available) < 60:
                continue

            rs_score = _calculate_rs_at_date(all_closes, ticker, eval_date)
            if rs_score < self.min_rs_score:
                continue

            l_score = rs_score / 100.0
            fund = _evaluate_fundamentals_at_date(ticker, eval_date)
            tech = _evaluate_technical_at_date(tdata, eval_date, fund.get("shares_outstanding"))

            c_score = fund.get("c_score", 0.0)
            a_score = fund.get("a_score", 0.0)
            i_score = fund.get("i_score", 0.5)
            has_fundamentals = (
                fund.get("current_growth") is not None or fund.get("annual_growth") is not None
            )

            total = _compute_canslim_score(
                c=c_score,
                a=a_score,
                n=tech["n_score"],
                s=tech["s_score"],
                l_score=l_score,
                i=i_score,
                m=m_score,
                has_fundamentals=has_fundamentals,
                institutional_data_available=bool(fund.get("institutional_data_available", False)),
            )

            peg_details = tech.get("power_gap_details") or {}
            has_peg_today = bool(tech.get("has_power_gap")) and peg_details.get("days_ago") == 0
            has_breakout = bool(tech.get("is_breakout"))
            has_surge = bool(tech.get("has_volume_surge"))

            buy_signal = (
                total >= self.min_canslim_score
                and rs_score >= self.min_rs_score
                and ((has_breakout and has_surge) or has_peg_today)
            )

            if buy_signal:
                signals.append({
                    "symbol": ticker,
                    "signal_date": str(eval_date.date()),
                    "canslim_score": total,
                    "rs_score": rs_score,
                })

        return signals

    # -----------------------------------------------------------------------
    # Entry / exit helpers
    # -----------------------------------------------------------------------

    def _enter_position(
        self,
        signal: dict,
        ticker_ohlcv: Dict[str, pd.DataFrame],
        entry_date: pd.Timestamp,
    ) -> None:
        """Open a new position at the next-day open price."""
        symbol = signal["symbol"]
        if symbol in self._open_positions:
            return
        if len(self._open_positions) >= self.max_positions:
            return

        ohlcv = ticker_ohlcv.get(symbol)
        if ohlcv is None:
            return

        # Use the open price of the entry date as the fill price
        bar = ohlcv.loc[entry_date:entry_date]
        if bar.empty:
            # Fallback: use previous close
            prev = ohlcv.loc[:entry_date]
            if prev.empty:
                return
            entry_price = float(prev["Close"].iloc[-1])
        else:
            entry_price = float(bar["Open"].iloc[0]) if "Open" in bar.columns else float(bar["Close"].iloc[0])

        if entry_price <= 0:
            return

        position_value = self._equity * self.position_size_pct
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
        )
        self._open_positions[symbol] = trade
        self._equity -= position_value
        print(
            f"  [ENTER] {symbol} @ ${entry_price:.2f} | qty={qty:.2f} | "
            f"stop=${stop_price:.2f} | CANSLIM={signal.get('canslim_score', 0):.1f}"
        )

    def _check_exits(
        self,
        symbol: str,
        ohlcv: pd.DataFrame,
        eval_date: pd.Timestamp,
        all_tickers: List[str],
        ticker_ohlcv: Dict[str, pd.DataFrame],
    ) -> None:
        """Check stop-loss and MA-violation exit conditions for an open position."""
        trade = self._open_positions.get(symbol)
        if trade is None:
            return

        bar = ohlcv.loc[eval_date:eval_date]
        if bar.empty:
            return

        low = float(bar["Low"].iloc[0]) if "Low" in bar.columns else float(bar["Close"].iloc[0])
        close = float(bar["Close"].iloc[0])
        date_str = str(eval_date.date())

        # --- Hard stop: daily low touches stop price ---
        if low <= trade.stop_price:
            # Fill at stop price (not the low — conservative but fair)
            exit_price = trade.stop_price
            self._close_trade(symbol, exit_price, "stop_loss", date_str)
            return

        # --- MA violation: two consecutive closes below EMA ---
        history = ohlcv.loc[:eval_date]
        if len(history) >= self.ma_exit_period + self.ma_consecutive:
            ema = history["Close"].ewm(span=self.ma_exit_period, adjust=False).mean()
            last_closes = history["Close"].iloc[-self.ma_consecutive:]
            last_ema = ema.iloc[-self.ma_consecutive:]
            if (last_closes.values < last_ema.values).all():
                self._close_trade(symbol, close, "ma_violation", date_str)

    def _close_trade(
        self,
        symbol: str,
        exit_price: float,
        reason: str,
        date_str: str,
    ) -> None:
        """Record a closed trade and update cash equity."""
        trade = self._open_positions.pop(symbol, None)
        if trade is None:
            return

        trade.exit_price = exit_price
        trade.exit_date = date_str
        trade.exit_reason = reason

        proceeds = exit_price * trade.qty
        self._equity += proceeds

        sign = "+" if trade.pnl >= 0 else ""
        print(
            f"  [EXIT/{reason}] {symbol} @ ${exit_price:.2f} | "
            f"entry=${trade.entry_price:.2f} | P&L={sign}${trade.pnl:.0f} ({sign}{trade.pnl_pct*100:.1f}%)"
        )
        self._trades.append(trade)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_pnl_report(result: SimulationResult) -> None:
    """Print a comprehensive P&L summary for the simulation."""
    print("\n" + "=" * 70)
    print("AUTO-TRADER PNL BACKTEST RESULTS")
    print("=" * 70)

    cfg = result.config
    print(f"Capital:        ${result.initial_capital:,.0f}")
    print(f"Tickers:        {', '.join(cfg.get('tickers', []))}")
    print(f"Stop-loss:      {cfg.get('stop_loss_pct', 0)*100:.0f}%")
    print(f"MA exit:        {cfg.get('ma_exit_period', 21)}-day EMA")
    print(f"Max positions:  {cfg.get('max_positions', 5)}")
    print(f"CANSLIM floor:  {cfg.get('min_canslim_score', 40)}")
    print(f"RS floor:       {cfg.get('min_rs_score', 75)}")

    print("\n--- Portfolio Performance ---")
    print(f"Final equity:   ${result.equity_curve.iloc[-1]:,.0f}" if not result.equity_curve.empty else "")
    print(f"Total return:   {result.total_return_pct:+.1f}%")
    print(f"Benchmark(SPY): {result.benchmark_return_pct:+.1f}%")
    print(f"Alpha:          {result.total_return_pct - result.benchmark_return_pct:+.1f}%")
    print(f"Max drawdown:   {result.max_drawdown_pct:.1f}%")
    print(f"Sharpe ratio:   {result.sharpe_ratio:.2f}")

    closed = result.closed_trades
    print("\n--- Trade Statistics ---")
    print(f"Total trades:   {len(result.trades)}")
    print(f"Closed:         {len(closed)}")
    print(f"Still open:     {len(result.open_trades)}")
    if closed:
        print(f"Win rate:       {result.win_rate:.1f}%")
        print(f"Avg win:        +{result.avg_win_pct:.1f}%")
        print(f"Avg loss:       {result.avg_loss_pct:.1f}%")
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        if wins:
            best = max(wins, key=lambda t: t.pnl_pct)
            print(f"Best trade:     {best.symbol} {best.pnl_pct*100:+.1f}%  ({best.entry_date} -> {best.exit_date})")
        if losses:
            worst = min(losses, key=lambda t: t.pnl_pct)
            print(f"Worst trade:    {worst.symbol} {worst.pnl_pct*100:+.1f}%  ({worst.entry_date} -> {worst.exit_date})")
        print(f"Exit reasons:   {result.exit_reason_counts}")

    if closed:
        print("\n--- Closed Trades ---")
        header = f"{'Symbol':<6}  {'Entry Date':<12} {'Entry':>8}  {'Exit Date':<12} {'Exit':>8}  {'P&L $':>9}  {'P&L %':>7}  {'Reason'}"
        print(header)
        print("-" * len(header))
        for t in sorted(closed, key=lambda t: t.entry_date):
            sign = "+" if t.pnl >= 0 else ""
            print(
                f"{t.symbol:<6}  {t.entry_date:<12} ${t.entry_price:>7.2f}  "
                f"{t.exit_date:<12} ${t.exit_price:>7.2f}  "
                f"{sign}${t.pnl:>8.0f}  {sign}{t.pnl_pct*100:>6.1f}%  {t.exit_reason}"
            )

    if result.open_trades:
        print("\n--- Still Open at End of Test ---")
        for t in result.open_trades:
            print(f"  {t.symbol}: entered {t.entry_date} @ ${t.entry_price:.2f} | stop=${t.stop_price:.2f}")

    # Per-year breakdown from equity curve
    if not result.equity_curve.empty:
        idx = pd.to_datetime(result.equity_curve.index)
        years = sorted(idx.year.unique())
        if len(years) > 1:
            print("\n--- Annual Returns ---")
            for yr in years:
                yr_vals = result.equity_curve[idx.year == yr]
                yr_start = yr_vals.iloc[0]
                yr_end = yr_vals.iloc[-1]
                yr_ret = (yr_end / yr_start - 1) * 100
                # Compare to SPY for same period
                if not result.benchmark_curve.empty:
                    spy_idx = pd.to_datetime(result.benchmark_curve.index)
                    spy_yr = result.benchmark_curve[spy_idx.year == yr]
                    if len(spy_yr) >= 2:
                        spy_ret = (spy_yr.iloc[-1] / spy_yr.iloc[0] - 1) * 100
                        alpha_str = f"  alpha {yr_ret - spy_ret:+.1f}%  (SPY {spy_ret:+.1f}%)"
                    else:
                        alpha_str = ""
                else:
                    alpha_str = ""
                print(f"  {yr}: {yr_ret:+.1f}%{alpha_str}")


def export_equity_curve(result: SimulationResult, filename: Optional[str] = None) -> str:
    """Export equity curve and benchmark to CSV for plotting."""
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"equity_curve_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    df = pd.DataFrame({
        "Date": result.equity_curve.index,
        "Portfolio": result.equity_curve.values,
        "Benchmark_SPY": result.benchmark_curve.reindex(result.equity_curve.index).values,
    })
    df.to_csv(filename, index=False)
    print(f"Equity curve saved to {filename}")
    return filename


def export_pnl_to_csv(result: SimulationResult, filename: Optional[str] = None) -> str:
    """Export trade log to CSV and return the file path."""
    if filename is None:
        os.makedirs(settings.BACKTEST_RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.BACKTEST_RESULTS_DIR,
            f"backtest_pnl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    rows = []
    for t in result.trades:
        rows.append({
            "Symbol": t.symbol,
            "Entry_Date": t.entry_date,
            "Entry_Price": round(t.entry_price, 2),
            "Qty": round(t.qty, 4),
            "Stop_Price": round(t.stop_price, 2),
            "Exit_Date": t.exit_date,
            "Exit_Price": round(t.exit_price, 2) if t.exit_price else None,
            "Exit_Reason": t.exit_reason,
            "PnL_USD": round(t.pnl, 2),
            "PnL_Pct": round(t.pnl_pct * 100, 2),
            "CANSLIM_Score": round(t.canslim_score, 1),
            "RS_Score": round(t.rs_score, 1),
        })
    pd.DataFrame(rows).to_csv(filename, index=False)
    print(f"\nTrade log saved to {filename}")
    return filename


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if False and __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CANSLIM Auto-Trader PnL Backtest")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS,
                        help="Tickers to backtest (default: %(default)s)")
    parser.add_argument("--weeks", type=int, default=DEFAULT_LOOKBACK_WEEKS,
                        help="Lookback in weeks (default: %(default)s = ~2 years)")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                        help="Starting capital in USD (default: %(default)s)")
    parser.add_argument("--stop", type=float, default=settings.STOP_LOSS_PCT,
                        help="Stop-loss fraction (default: %(default)s = 7%%)")
    parser.add_argument("--ma-period", type=int, default=DEFAULT_MA_EXIT_PERIOD,
                        help="EMA period for MA-violation exit (default: %(default)s)")
    parser.add_argument("--signal-days", type=int, default=DEFAULT_SIGNAL_EVERY_N_DAYS,
                        help="Re-evaluate CANSLIM every N trading days (default: %(default)s)")
    parser.add_argument("--min-canslim", type=float, default=40.0,
                        help="Minimum CANSLIM score to enter (default: %(default)s)")
    parser.add_argument("--min-rs", type=float, default=float(settings.MIN_RS_SCORE),
                        help="Minimum RS score to enter (default: %(default)s)")
    parser.add_argument("--bullish-only", action="store_true",
                        help="Only enter when market is bullish (O'Neil strict mode)")
    parser.add_argument("--no-csv", action="store_true",
                        help="Skip trade-log CSV export")
    parser.add_argument("--export-equity", action="store_true",
                        help="Also export equity curve to CSV (for charting)")
    parser.add_argument("--universe", choices=["default", "sp500", "large_cap"],
                        default="default",
                        help="Ticker universe: default=hardcoded list, sp500=live S&P 500, "
                             "large_cap=S&P500+Nasdaq100 (default: %(default)s)")
    args = parser.parse_args()

    # Resolve ticker universe
    tickers = args.tickers
    if args.universe == "sp500":
        print("Fetching S&P 500 tickers from iShares ETF…")
        from core.index_ticker_fetcher import get_sp500_tickers
        tickers = get_sp500_tickers() or tickers
        print(f"  → {len(tickers)} tickers")
    elif args.universe == "large_cap":
        print("Fetching large-cap tickers (S&P 500 + Nasdaq 100)…")
        from core.index_ticker_fetcher import get_all_index_tickers
        tickers = get_all_index_tickers(indices=["sp500", "nasdaq100"]) or tickers
        print(f"  → {len(tickers)} tickers")

    # Always include EXTRA_SYMBOLS
    extra = [s for s in settings.EXTRA_SYMBOLS if s not in tickers]
    if extra:
        tickers = list(tickers) + extra
        print(f"  + {len(extra)} EXTRA_SYMBOLS: {extra}")

    print("=" * 70)
    print("CANSLIM AUTO-TRADER PNL BACKTEST")
    print("=" * 70)
    print(f"Universe:      {args.universe} ({len(tickers)} tickers)")
    print(f"Lookback:      {args.weeks} weeks")
    print(f"Capital:       ${args.capital:,.0f}")
    print(f"Stop-loss:     {args.stop*100:.0f}%")
    print(f"MA exit:       {args.ma_period}-day EMA")
    print(f"Signal freq:   every {args.signal_days} trading days")
    print(f"CANSLIM floor: {args.min_canslim}")
    print(f"RS floor:      {args.min_rs}")
    print(f"Bullish gate:  {'ON' if args.bullish_only else 'off'}")
    print()

    sim = PortfolioSimulator(
        initial_capital=args.capital,
        max_positions=settings.MAX_OPEN_POSITIONS,
        position_size_pct=settings.POSITION_SIZE_PCT,
        stop_loss_pct=args.stop,
        ma_exit_period=args.ma_period,
        signal_every_n_days=args.signal_days,
        min_canslim_score=args.min_canslim,
        min_rs_score=args.min_rs,
        require_bullish_market=args.bullish_only,
    )

    result = sim.run(tickers=tickers, lookback_weeks=args.weeks)
    print_pnl_report(result)

    if not args.no_csv and result.trades:
        export_pnl_to_csv(result)

    if args.export_equity and not result.equity_curve.empty:
        export_equity_curve(result)


from core import backtest_engine as _engine

DataFetcher = _engine.DataFetcher
CanslimStrategy = _engine.CanslimStrategy
PerformanceReport = _engine.PerformanceReport
Trade = _engine.Trade
SimulationResult = _engine.SimulationResult
print_pnl_report = _engine.print_pnl_report
export_equity_curve = _engine.export_equity_curve
export_pnl_to_csv = _engine.export_pnl_to_csv
export_weekly_holdings = _engine.export_weekly_holdings
export_trade_charts = _engine.export_trade_charts
run_cli = _engine.run_cli
DEFAULT_TICKERS = _engine.DEFAULT_TICKERS
DEFAULT_CAPITAL = _engine.DEFAULT_CAPITAL
DEFAULT_LOOKBACK_WEEKS = _engine.DEFAULT_LOOKBACK_WEEKS
DEFAULT_SIGNAL_EVERY_N_DAYS = _engine.DEFAULT_SIGNAL_EVERY_N_DAYS
DEFAULT_MA_EXIT_PERIOD = _engine.DEFAULT_MA_EXIT_PERIOD
DEFAULT_MA_CONSECUTIVE = _engine.DEFAULT_MA_CONSECUTIVE


class PortfolioSimulator(_engine.PortfolioSimulator):
    """Compatibility wrapper that preserves patch points in this module."""

    def run(self, *args, **kwargs):  # type: ignore[override]
        from core.index_ticker_fetcher import get_all_index_tickers as _get_all_index_tickers

        _engine._download_price_data = _download_price_data
        _engine._download_bulk_closes = _download_bulk_closes
        _engine._calculate_rs_at_date = _calculate_rs_at_date
        _engine._evaluate_market_at_date = _evaluate_market_at_date
        _engine._evaluate_fundamentals_at_date = _evaluate_fundamentals_at_date
        _engine._evaluate_technical_at_date = _evaluate_technical_at_date
        _engine._compute_canslim_score = _compute_canslim_score
        _engine.clear_session_cache = clear_session_cache
        _engine.get_sp500_tickers = get_sp500_tickers
        _engine.get_all_index_tickers = _get_all_index_tickers
        return super().run(*args, **kwargs)


if __name__ == "__main__":
    run_cli()
