"""Unit tests for backtest_pnl.py — PortfolioSimulator and SimulationResult.

All tests use synthetic price DataFrames (no Alpaca or FMP API calls).
Tests verify:

  Trade dataclass
  ├── pnl, pnl_pct, cost_basis, is_open properties
  └── closed vs open state

  SimulationResult
  ├── win_rate, avg_win_pct, avg_loss_pct
  ├── max_drawdown_pct
  ├── sharpe_ratio
  ├── benchmark_return_pct
  └── exit_reason_counts

  PortfolioSimulator._enter_position
  ├── opens position at next-day open price
  ├── deducts cost from equity
  ├── sets stop 7% below entry
  └── skips when position already held or limit reached

  PortfolioSimulator._check_exits
  ├── hard stop fires when daily low <= stop price
  ├── MA violation fires after 2 consecutive closes below EMA
  └── healthy position is not exited

  PortfolioSimulator._close_trade
  ├── records exit price, date, reason
  └── returns proceeds to equity
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import patch

import pandas as pd
import pytest

from backtest_pnl import (
    PortfolioSimulator,
    SimulationResult,
    Trade,
)


# ---------------------------------------------------------------------------
# Synthetic price data helpers
# ---------------------------------------------------------------------------

_FIXTURE_END = pd.Timestamp("2026-07-31")


def _business_dates(periods: int) -> pd.DatetimeIndex:
    """Return an exact, wall-clock-independent number of business dates."""
    return pd.bdate_range(end=_FIXTURE_END, periods=periods)


def _make_ohlcv(
    n: int = 60,
    start_price: float = 100.0,
    trend: float = 0.0,
    low_override: Dict[int, float] | None = None,
) -> pd.DataFrame:
    """Return a synthetic OHLCV DataFrame with n bars.

    Args:
        n: Number of daily bars.
        start_price: Starting close price.
        trend: Daily drift applied to close (e.g. 0.001 = +0.1%/day).
        low_override: Map of {bar_index: low_price} to force specific lows.
    """
    dates = _business_dates(n)
    closes = [start_price * (1 + trend) ** i for i in range(n)]
    lows = [c * 0.99 for c in closes]
    highs = [c * 1.01 for c in closes]
    opens = [c * 0.995 for c in closes]

    if low_override:
        for idx, low in low_override.items():
            lows[idx] = low

    df = pd.DataFrame({
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": [1_000_000] * n,
    }, index=dates)
    return df


def _make_declining_ohlcv(n: int = 60, start_price: float = 100.0) -> pd.DataFrame:
    """OHLCV where close drops steadily so last 2 bars are well below 21-EMA."""
    dates = _business_dates(n)
    # First 50 bars: flat at start_price so EMA anchors there
    closes = [start_price] * (n - 10) + [start_price * 0.85] * 10
    lows = [c * 0.99 for c in closes]
    highs = [c * 1.01 for c in closes]
    opens = [c * 0.995 for c in closes]
    return pd.DataFrame({
        "Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1_000_000] * n,
    }, index=dates)


def _make_simulator(**kwargs) -> PortfolioSimulator:
    """Return a PortfolioSimulator with sensible test defaults."""
    defaults = dict(
        initial_capital=100_000.0,
        max_positions=5,
        position_size_pct=0.10,
        stop_loss_pct=0.07,
        ma_exit_period=21,
        ma_consecutive=2,
        signal_every_n_days=5,
        min_canslim_score=40.0,
        min_rs_score=70.0,
    )
    defaults.update(kwargs)
    return PortfolioSimulator(**defaults)


def _make_trade(
    symbol: str = "TEST",
    entry_price: float = 100.0,
    qty: float = 10.0,
    stop_price: float = 93.0,
    exit_price: float | None = None,
    exit_reason: str | None = None,
) -> Trade:
    t = Trade(
        symbol=symbol,
        entry_date="2024-01-01",
        entry_price=entry_price,
        qty=qty,
        stop_price=stop_price,
        exit_price=exit_price,
        exit_date="2024-02-01" if exit_price else None,
        exit_reason=exit_reason,
    )
    return t


# ===========================================================================
# Trade dataclass
# ===========================================================================


class TestTrade:
    def test_is_open_when_no_exit_price(self) -> None:
        t = _make_trade(exit_price=None)
        assert t.is_open is True

    def test_is_closed_when_exit_price_set(self) -> None:
        t = _make_trade(exit_price=110.0)
        assert t.is_open is False

    def test_pnl_is_zero_when_open(self) -> None:
        t = _make_trade(exit_price=None)
        assert t.pnl == 0.0

    def test_pnl_profit(self) -> None:
        t = _make_trade(entry_price=100.0, qty=10.0, exit_price=115.0)
        assert t.pnl == pytest.approx(150.0)  # (115-100)*10

    def test_pnl_loss(self) -> None:
        t = _make_trade(entry_price=100.0, qty=10.0, exit_price=93.0)
        assert t.pnl == pytest.approx(-70.0)  # (93-100)*10

    def test_pnl_pct_profit(self) -> None:
        t = _make_trade(entry_price=100.0, exit_price=115.0)
        assert t.pnl_pct == pytest.approx(0.15)

    def test_pnl_pct_loss(self) -> None:
        t = _make_trade(entry_price=100.0, exit_price=93.0)
        assert t.pnl_pct == pytest.approx(-0.07)

    def test_cost_basis(self) -> None:
        t = _make_trade(entry_price=200.0, qty=5.0)
        assert t.cost_basis == pytest.approx(1000.0)


# ===========================================================================
# SimulationResult computed properties
# ===========================================================================


class TestSimulationResult:
    def _make_result(self, trades=None, equity=None, benchmark=None) -> SimulationResult:
        if equity is None:
            equity = pd.Series([100_000.0, 105_000.0, 103_000.0, 108_000.0])
        if benchmark is None:
            benchmark = pd.Series([100_000.0, 102_000.0, 101_000.0, 104_000.0])
        return SimulationResult(
            trades=trades or [],
            equity_curve=equity,
            benchmark_curve=benchmark,
            initial_capital=100_000.0,
        )

    def test_total_return_pct(self) -> None:
        r = self._make_result(equity=pd.Series([100_000.0, 110_000.0]))
        assert r.total_return_pct == pytest.approx(10.0)

    def test_benchmark_return_pct(self) -> None:
        r = self._make_result(benchmark=pd.Series([100_000.0, 115_000.0]))
        assert r.benchmark_return_pct == pytest.approx(15.0)

    def test_win_rate_50pct(self) -> None:
        trades = [
            _make_trade("A", exit_price=110.0, exit_reason="ma_violation"),
            _make_trade("B", exit_price=93.0, exit_reason="stop_loss"),
        ]
        r = self._make_result(trades=trades)
        assert r.win_rate == pytest.approx(50.0)

    def test_win_rate_100pct(self) -> None:
        trades = [_make_trade("A", exit_price=120.0, exit_reason="ma_violation")]
        r = self._make_result(trades=trades)
        assert r.win_rate == pytest.approx(100.0)

    def test_win_rate_zero_when_no_closed_trades(self) -> None:
        r = self._make_result(trades=[])
        assert r.win_rate == 0.0

    def test_avg_win_pct(self) -> None:
        trades = [
            _make_trade("A", entry_price=100.0, qty=1.0, exit_price=120.0, exit_reason="x"),  # +20%
            _make_trade("B", entry_price=100.0, qty=1.0, exit_price=110.0, exit_reason="x"),  # +10%
        ]
        r = self._make_result(trades=trades)
        assert r.avg_win_pct == pytest.approx(15.0)

    def test_avg_loss_pct(self) -> None:
        trades = [
            _make_trade("A", entry_price=100.0, qty=1.0, exit_price=93.0, exit_reason="stop_loss"),  # -7%
            _make_trade("B", entry_price=100.0, qty=1.0, exit_price=97.0, exit_reason="stop_loss"),  # -3%
        ]
        r = self._make_result(trades=trades)
        assert r.avg_loss_pct == pytest.approx(-5.0)

    def test_max_drawdown_pct(self) -> None:
        # Equity: 100k → 90k → 95k → 85k → 100k
        equity = pd.Series([100_000.0, 90_000.0, 95_000.0, 85_000.0, 100_000.0])
        r = self._make_result(equity=equity)
        # Peak was 100k, trough was 85k → drawdown = -15%
        assert r.max_drawdown_pct == pytest.approx(-15.0)

    def test_sharpe_positive_trend(self) -> None:
        # Monotonically rising equity → positive Sharpe
        equity = pd.Series([100_000.0 * (1.0005 ** i) for i in range(252)])
        r = self._make_result(equity=equity)
        assert r.sharpe_ratio > 0

    def test_exit_reason_counts(self) -> None:
        trades = [
            _make_trade("A", exit_price=93.0, exit_reason="stop_loss"),
            _make_trade("B", exit_price=93.0, exit_reason="stop_loss"),
            _make_trade("C", exit_price=110.0, exit_reason="ma_violation"),
        ]
        r = self._make_result(trades=trades)
        assert r.exit_reason_counts == {"stop_loss": 2, "ma_violation": 1}

    def test_closed_vs_open_split(self) -> None:
        closed = _make_trade("A", exit_price=110.0, exit_reason="x")
        still_open = _make_trade("B", exit_price=None)
        r = self._make_result(trades=[closed, still_open])
        assert len(r.closed_trades) == 1
        assert len(r.open_trades) == 1


# ===========================================================================
# PortfolioSimulator._enter_position
# ===========================================================================


class TestEnterPosition:
    def test_opens_position_at_open_price(self) -> None:
        sim = _make_simulator(initial_capital=100_000.0, stop_loss_pct=0.07)
        ohlcv = _make_ohlcv(n=30, start_price=200.0)
        entry_date = ohlcv.index[-1]

        signal = {"symbol": "TEST", "signal_date": "2024-01-01", "canslim_score": 75.0, "rs_score": 85.0}
        ticker_ohlcv = {"TEST": ohlcv}

        sim._enter_position(signal, ticker_ohlcv, entry_date)

        assert "TEST" in sim._open_positions
        trade = sim._open_positions["TEST"]
        # Risk-based: position_value = portfolio * risk_pct / stop_pct
        # qty = position_value / entry_price = portfolio * risk_pct / (stop_pct * entry_price)
        position_risk_pct = 0.01  # settings.POSITION_RISK_PCT default
        expected_qty = (100_000.0 * position_risk_pct / 0.07) / trade.entry_price
        assert trade.qty == pytest.approx(expected_qty)

    def test_stop_price_is_7pct_below_entry(self) -> None:
        sim = _make_simulator(stop_loss_pct=0.07)
        ohlcv = _make_ohlcv(n=30, start_price=100.0)
        entry_date = ohlcv.index[-1]
        signal = {"symbol": "TEST", "signal_date": "2024-01-01", "canslim_score": 75.0, "rs_score": 85.0}

        sim._enter_position(signal, {"TEST": ohlcv}, entry_date)

        trade = sim._open_positions["TEST"]
        # `1 - 0.07` vs literal `0.93` may differ by 1 ULP in float; use abs tolerance
        assert trade.stop_price == pytest.approx(trade.entry_price * 0.93, abs=0.02)

    def test_cash_equity_reduced_by_position_value(self) -> None:
        sim = _make_simulator(initial_capital=100_000.0, stop_loss_pct=0.07)
        ohlcv = _make_ohlcv(n=30, start_price=100.0)
        entry_date = ohlcv.index[-1]
        signal = {"symbol": "TEST", "signal_date": "2024-01-01", "canslim_score": 75.0, "rs_score": 85.0}

        sim._enter_position(signal, {"TEST": ohlcv}, entry_date)

        # Risk-based: position_value = portfolio * risk_pct / stop_pct = 100_000 * 0.01 / 0.07
        position_risk_pct = 0.01
        position_value = 100_000.0 * position_risk_pct / 0.07
        assert sim._equity == pytest.approx(100_000.0 - position_value, abs=1.0)

    def test_skips_if_already_holding_symbol(self) -> None:
        sim = _make_simulator()
        ohlcv = _make_ohlcv(n=30)
        entry_date = ohlcv.index[-1]
        # Pre-populate as if already held
        sim._open_positions["TEST"] = _make_trade("TEST")
        equity_before = sim._equity
        signal = {"symbol": "TEST", "signal_date": "2024-01-01", "canslim_score": 75.0, "rs_score": 85.0}

        sim._enter_position(signal, {"TEST": ohlcv}, entry_date)

        # Equity unchanged — no new position opened
        assert sim._equity == equity_before
        assert len(sim._open_positions) == 1

    def test_skips_when_max_positions_reached(self) -> None:
        sim = _make_simulator(max_positions=2)
        ohlcv = _make_ohlcv(n=30)
        entry_date = ohlcv.index[-1]
        # Fill up positions
        sim._open_positions["A"] = _make_trade("A")
        sim._open_positions["B"] = _make_trade("B")
        equity_before = sim._equity
        signal = {"symbol": "NEW", "signal_date": "2024-01-01", "canslim_score": 75.0, "rs_score": 85.0}

        sim._enter_position(signal, {"NEW": ohlcv}, entry_date)

        assert "NEW" not in sim._open_positions
        assert sim._equity == equity_before


# ===========================================================================
# PortfolioSimulator._check_exits — stop loss
# ===========================================================================


class TestStopLossExit:
    def test_stop_triggered_when_low_touches_stop_price(self) -> None:
        sim = _make_simulator(stop_loss_pct=0.07)
        entry_price = 100.0
        stop_price = round(entry_price * 0.93, 2)
        trade = _make_trade("TEST", entry_price=entry_price, stop_price=stop_price, exit_price=None)
        sim._open_positions["TEST"] = trade
        sim._equity = 90_000.0

        # Build OHLCV where today's low dips below stop
        ohlcv = _make_ohlcv(n=40, start_price=entry_price, low_override={39: stop_price - 0.01})
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        # Position should be closed
        assert "TEST" not in sim._open_positions
        closed = [t for t in sim._trades if t.symbol == "TEST"]
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"
        assert closed[0].exit_price == pytest.approx(stop_price)

    def test_no_exit_when_low_above_stop_price(self) -> None:
        sim = _make_simulator(stop_loss_pct=0.07)
        entry_price = 100.0
        stop_price = round(entry_price * 0.93, 2)
        trade = _make_trade("TEST", entry_price=entry_price, stop_price=stop_price, exit_price=None)
        sim._open_positions["TEST"] = trade

        # All lows stay above stop (close 100 → low ~99, stop = 93)
        ohlcv = _make_ohlcv(n=40, start_price=entry_price)
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        assert "TEST" in sim._open_positions

    def test_exit_price_is_stop_price_not_the_low(self) -> None:
        """Fill at the stop price, not the intraday low (conservative fill)."""
        sim = _make_simulator(stop_loss_pct=0.07)
        stop_price = 93.0
        trade = _make_trade("TEST", entry_price=100.0, stop_price=stop_price, exit_price=None)
        sim._open_positions["TEST"] = trade
        sim._equity = 90_000.0

        # Low at 85 (gaps through stop)
        ohlcv = _make_ohlcv(n=40, low_override={39: 85.0})
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        closed = [t for t in sim._trades if t.symbol == "TEST"]
        assert closed[0].exit_price == pytest.approx(stop_price)


# ===========================================================================
# PortfolioSimulator._check_exits — MA violation
# ===========================================================================


class TestMAViolationExit:
    def test_ma_violation_triggers_after_two_consecutive_closes_below_ema(self) -> None:
        # Stop at 93 — decline must stay ABOVE 93 so stop doesn't fire first.
        # Use a 5% decline (to 95) instead of 15% so lows (95*0.99=94.05) > stop=93.
        sim = _make_simulator(ma_exit_period=21, ma_consecutive=2)
        trade = _make_trade("TEST", entry_price=100.0, stop_price=93.0, exit_price=None)
        sim._open_positions["TEST"] = trade
        sim._equity = 90_000.0

        # Build: first 50 bars flat at 100 (EMA anchors near 100), last 10 close at 95.
        # EMA will still be near 100 → 95 < EMA → MA violation fires. Stop=93 not hit.
        n = 60
        dates = _business_dates(n)
        closes = [100.0] * 50 + [95.0] * 10
        lows = [c * 0.99 for c in closes]  # min low = 95*0.99 = 94.05 > stop=93
        highs = [c * 1.01 for c in closes]
        opens = [c * 0.995 for c in closes]
        ohlcv = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1_000_000] * n,
        }, index=dates)
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        # Should have been exited by MA violation (not stop)
        closed = [t for t in sim._trades if t.symbol == "TEST"]
        assert len(closed) == 1
        assert closed[0].exit_reason == "ma_violation"

    def test_no_ma_exit_when_price_above_ema(self) -> None:
        sim = _make_simulator(ma_exit_period=21, ma_consecutive=2)
        trade = _make_trade("TEST", entry_price=100.0, stop_price=93.0, exit_price=None)
        sim._open_positions["TEST"] = trade

        # Rising price stays above EMA
        ohlcv = _make_ohlcv(n=60, start_price=100.0, trend=0.003)
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        assert "TEST" in sim._open_positions

    def test_stop_takes_priority_over_ma_violation(self) -> None:
        """When both stop and MA trigger on the same bar, stop fires first."""
        sim = _make_simulator(stop_loss_pct=0.07, ma_exit_period=21, ma_consecutive=2)
        entry_price = 100.0
        stop_price = round(entry_price * 0.93, 2)
        trade = _make_trade("TEST", entry_price=entry_price, stop_price=stop_price, exit_price=None)
        sim._open_positions["TEST"] = trade
        sim._equity = 90_000.0

        # Declining OHLCV where low also crosses stop
        ohlcv = _make_declining_ohlcv(n=60, start_price=entry_price)
        # Force the last bar's low to also cross the stop
        ohlcv.iloc[-1, ohlcv.columns.get_loc("Low")] = stop_price - 1
        eval_date = ohlcv.index[-1]

        sim._check_exits("TEST", ohlcv, eval_date, [], {})

        closed = [t for t in sim._trades if t.symbol == "TEST"]
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"


# ===========================================================================
# PortfolioSimulator._close_trade
# ===========================================================================


class TestCloseTrade:
    def test_close_trade_records_exit(self) -> None:
        sim = _make_simulator(initial_capital=90_000.0)
        trade = _make_trade("AAPL", entry_price=100.0, qty=10.0, exit_price=None)
        sim._open_positions["AAPL"] = trade

        sim._close_trade("AAPL", exit_price=115.0, reason="ma_violation", date_str="2024-06-01")

        assert "AAPL" not in sim._open_positions
        assert len(sim._trades) == 1
        closed = sim._trades[0]
        assert closed.exit_price == 115.0
        assert closed.exit_reason == "ma_violation"
        assert closed.exit_date == "2024-06-01"

    def test_close_trade_returns_proceeds_to_equity(self) -> None:
        sim = _make_simulator(initial_capital=90_000.0)
        trade = _make_trade("AAPL", entry_price=100.0, qty=10.0, exit_price=None)
        sim._open_positions["AAPL"] = trade
        initial_equity = sim._equity

        sim._close_trade("AAPL", exit_price=115.0, reason="ma_violation", date_str="2024-06-01")

        expected = initial_equity + (115.0 * 10.0)
        assert sim._equity == pytest.approx(expected)

    def test_close_trade_is_noop_for_unknown_symbol(self) -> None:
        sim = _make_simulator()
        initial_equity = sim._equity

        sim._close_trade("NONE", exit_price=100.0, reason="stop_loss", date_str="2024-01-01")

        assert sim._equity == initial_equity
        assert len(sim._trades) == 0


# ===========================================================================
# PortfolioSimulator.run() integration (fully mocked)
# ===========================================================================


class TestPortfolioSimulatorRun:
    """Integration tests: run() with mocked data download and CANSLIM evaluation."""

    def _make_full_signal_ticker_ohlcv(self, symbol: str, n: int = 150) -> Dict[str, pd.DataFrame]:
        """Return ticker_ohlcv dict with SPY + one ticker that rises +20% over n bars."""
        dates = _business_dates(n)
        closes = [100.0 * (1.001 ** i) for i in range(n)]
        lows = [c * 0.99 for c in closes]
        highs = [c * 1.01 for c in closes]
        opens = [c * 0.995 for c in closes]
        df = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [2_000_000]*n
        }, index=dates)
        spy = df.copy()
        return {symbol: df, "SPY": spy}

    def test_run_returns_simulation_result(self) -> None:
        sim = _make_simulator()

        # n=150 >> lookback_weeks=8 (40 business days) — generous padding
        ticker_ohlcv = self._make_full_signal_ticker_ohlcv("NVDA", n=150)
        all_closes = pd.DataFrame({
            "NVDA": [100.0 * (1.001**i) for i in range(150)],
        }, index=ticker_ohlcv["NVDA"].index)

        with (
            patch("backtest_pnl._download_price_data", return_value=ticker_ohlcv),
            patch("backtest_pnl._download_bulk_closes", return_value=all_closes),
            patch("backtest_pnl.get_sp500_tickers", return_value=[]),
            patch("backtest_pnl._calculate_rs_at_date", return_value=90.0),
            patch("backtest_pnl._evaluate_market_at_date", return_value=(0.8, True, 0, True)),
            patch("backtest_pnl._evaluate_fundamentals_at_date", return_value={
                "c_score": 0.8, "a_score": 0.7, "i_score": 0.6,
                "current_growth": 0.30, "annual_growth": 0.25, "roe": 0.20, "shares_outstanding": None,
            }),
            patch("backtest_pnl._evaluate_technical_at_date", return_value={
                "n_score": 0.9, "s_score": 0.8, "proximity": 0.99, "close": 110.0,
                "high_52": 112.0, "avg_vol_50": 2_000_000,
                "is_breakout": True, "has_volume_surge": True,
                "has_power_gap": False, "power_gap_details": {},
            }),
            patch("backtest_pnl._compute_canslim_score", return_value=75.0),
            patch("backtest_pnl.clear_session_cache"),
        ):
            # lookback_weeks=8 → 40 business days, well within 150 bars
            result = sim.run(
                tickers=["NVDA"],
                lookback_weeks=8,
                end_date=str(_FIXTURE_END.date()),
            )

        assert isinstance(result, SimulationResult)
        assert not result.equity_curve.empty

    def test_run_enters_position_on_buy_signal(self) -> None:
        sim = _make_simulator(min_canslim_score=40.0, min_rs_score=70.0, signal_every_n_days=1)

        # n=150 bars >> lookback_weeks=8 (40 business days)
        ticker_ohlcv = self._make_full_signal_ticker_ohlcv("NVDA", n=150)
        all_closes = pd.DataFrame({
            "NVDA": [100.0 * (1.001**i) for i in range(150)],
        }, index=ticker_ohlcv["NVDA"].index)

        with (
            patch("backtest_pnl._download_price_data", return_value=ticker_ohlcv),
            patch("backtest_pnl._download_bulk_closes", return_value=all_closes),
            patch("backtest_pnl.get_sp500_tickers", return_value=[]),
            patch("backtest_pnl._calculate_rs_at_date", return_value=90.0),
            patch("backtest_pnl._evaluate_market_at_date", return_value=(0.8, True, 0, True)),
            patch("backtest_pnl._evaluate_fundamentals_at_date", return_value={
                "c_score": 0.8, "a_score": 0.7, "i_score": 0.6,
                "current_growth": 0.30, "annual_growth": 0.25, "roe": 0.20, "shares_outstanding": None,
            }),
            patch("backtest_pnl._evaluate_technical_at_date", return_value={
                "n_score": 0.9, "s_score": 0.8, "proximity": 0.99, "close": 110.0,
                "high_52": 112.0, "avg_vol_50": 2_000_000,
                "is_breakout": True, "has_volume_surge": True,
                "has_power_gap": False, "power_gap_details": {},
            }),
            patch("backtest_pnl._compute_canslim_score", return_value=75.0),
            patch("backtest_pnl.clear_session_cache"),
            patch("core.backtest_engine.load_industry_map", return_value={}),
        ):
            result = sim.run(
                tickers=["NVDA"],
                lookback_weeks=8,
                end_date=str(_FIXTURE_END.date()),
            )

        # At least one trade should have been recorded
        assert len(result.trades) >= 1
        symbols = {t.symbol for t in result.trades}
        assert "NVDA" in symbols

    def test_run_applies_stop_loss_when_price_drops(self) -> None:
        from unittest.mock import MagicMock

        # 150 bars: first 110 flat at 100 (outside 8-week lookback window),
        # then 10 bars at 105 (signal fires in lookback, position opens),
        # then 30 bars crash to 80 (below stop of 105*0.93≈97.65 → stop triggers).
        n = 150
        dates = _business_dates(n)
        closes = [100.0] * 110 + [105.0] * 10 + [80.0] * 30
        lows = [c * 0.99 for c in closes[:120]] + [c * 0.98 for c in closes[120:]]
        highs = [c * 1.01 for c in closes]
        opens = [c * 0.995 for c in closes]
        crash_df = pd.DataFrame({
            "Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [2_000_000] * n,
        }, index=dates)
        ticker_ohlcv = {"NVDA": crash_df, "SPY": crash_df.copy()}
        all_closes = pd.DataFrame({"NVDA": closes}, index=dates)

        # Inject a mock DataFetcher to bypass the SQLite cache, ensuring synthetic
        # data is used regardless of any cached real historical data.
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_price_data.return_value = ticker_ohlcv
        mock_fetcher.fetch_rs_universe_closes.return_value = all_closes

        sim = _make_simulator(
            stop_loss_pct=0.07,
            signal_every_n_days=1,
            min_canslim_score=40.0,
            min_rs_score=70.0,
            data_fetcher=mock_fetcher,
        )

        with (
            patch("backtest_pnl.get_sp500_tickers", return_value=[]),
            patch("backtest_pnl._calculate_rs_at_date", return_value=90.0),
            patch("backtest_pnl._evaluate_market_at_date", return_value=(0.8, True, 0, True)),
            patch("backtest_pnl._evaluate_fundamentals_at_date", return_value={
                "c_score": 0.8, "a_score": 0.7, "i_score": 0.6,
                "current_growth": 0.30, "annual_growth": 0.25, "roe": 0.20, "shares_outstanding": None,
            }),
            patch("backtest_pnl._evaluate_technical_at_date", return_value={
                "n_score": 0.9, "s_score": 0.8, "proximity": 0.99, "close": 105.0,
                "high_52": 107.0, "avg_vol_50": 2_000_000,
                "is_breakout": True, "has_volume_surge": True,
                "has_power_gap": False, "power_gap_details": {},
            }),
            patch("backtest_pnl._compute_canslim_score", return_value=75.0),
            patch("backtest_pnl.clear_session_cache"),
            patch(
                "core.canslim.m_market_direction.MarketRegimeTracker.allows_entries",
                new_callable=lambda: property(lambda self: True),
            ),
            patch("core.backtest_engine.load_industry_map", return_value={}),
        ):
            # 8 weeks = 40 business days, covers the last 40 bars (crash included)
            result = sim.run(
                tickers=["NVDA"],
                lookback_weeks=8,
                end_date=str(_FIXTURE_END.date()),
            )

        stop_exits = [t for t in result.trades if t.exit_reason == "stop_loss"]
        # The crash should have triggered at least one stop-loss exit
        assert len(stop_exits) >= 1, [
            (trade.entry_date, trade.entry_price, trade.exit_date, trade.exit_price, trade.exit_reason)
            for trade in result.trades
        ]
