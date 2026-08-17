"""Regression tests for all 6 bugs found and fixed in the scheduler/auto_trader audit.

Each test class is named after the bug it guards against to make failures
self-documenting in CI output.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Bug 1 — _latest_close used bars["close"] (lowercase) → always returned None
# Impact: execute_entries() skipped every ticker with "could not fetch price"
# ---------------------------------------------------------------------------


class TestBug1LatestCloseColumnName:
    """_latest_close must return the last Close value, not None."""

    def _make_bars(self) -> pd.DataFrame:
        """Simulate what fetch_ohlcv returns — capitalized columns."""
        return pd.DataFrame(
            {"Open": [99.0, 100.0], "High": [101.0, 102.0],
             "Low": [98.0, 99.0], "Close": [100.5, 101.0], "Volume": [1e6, 1e6]}
        )

    def test_latest_close_returns_float_not_none(self):
        from auto_trader import _latest_close

        with patch("auto_trader.fetch_ohlcv", return_value=self._make_bars()):
            result = _latest_close("NVDA")

        assert result is not None, "_latest_close returned None — lowercase column bug is back"
        assert isinstance(result, float)
        assert result == pytest.approx(101.0)

    def test_execute_entries_uses_price_correctly(self):
        """execute_entries must not print 'could not fetch price' for valid bars."""
        from auto_trader import execute_entries

        logged = []

        opp = {
            "symbol": "NVDA",
            "total_score": 75.0,
            "rs_score": 88.0,
            "is_breakout": True,
            "has_volume_surge": True,
            "buy_point": 100.0,
        }

        with patch("auto_trader._get_account_equity", return_value=100_000.0), \
             patch("auto_trader.get_open_positions", return_value=[]), \
             patch("auto_trader.get_open_orders", return_value=[]), \
             patch("auto_trader.fetch_ohlcv", return_value=self._make_bars()), \
             patch("auto_trader.OrderManager") as mock_manager_cls, \
             patch("builtins.print", side_effect=lambda *a: logged.append(" ".join(str(x) for x in a))):
            mock_manager_cls.return_value.submit_entry.return_value = SimpleNamespace(
                success=True,
                dry_run=False,
                workflow_id="wf-nvda-regression",
            )
            execute_entries([opp])

        # The bug caused: "could not fetch price — skipping"
        price_skip = [m for m in logged if "could not fetch price" in m]
        assert not price_skip, f"Got 'could not fetch price' despite valid bars: {price_skip}"
        mock_manager_cls.return_value.submit_entry.assert_called_once()

    def test_latest_close_prefers_intraday_price_when_market_is_open(self):
        from auto_trader import _latest_close

        with patch("auto_trader._is_market_open", return_value=True), \
             patch("auto_trader.fetch_latest_intraday_price", return_value=101.25) as mock_intraday, \
             patch("auto_trader.fetch_ohlcv") as mock_daily:
            result = _latest_close("NVDA")

        assert result == pytest.approx(101.25)
        mock_intraday.assert_called_once_with("NVDA")
        mock_daily.assert_not_called()

    def test_latest_close_falls_back_to_daily_close_when_intraday_missing(self):
        from auto_trader import _latest_close

        bars = self._make_bars()
        with patch("auto_trader._is_market_open", return_value=True), \
             patch("auto_trader.fetch_latest_intraday_price", return_value=None), \
             patch("auto_trader.fetch_ohlcv", return_value=bars):
            result = _latest_close("NVDA")

        assert result == pytest.approx(101.0)


# ---------------------------------------------------------------------------
# Bug 2 — backtest_pnl --universe large_cap referenced get_large_cap_tickers
#          which does not exist → ImportError at runtime
# ---------------------------------------------------------------------------


class TestBug2LargeCapUniverse:
    """--universe large_cap must resolve via get_all_index_tickers, not a missing function."""

    def test_get_large_cap_tickers_does_not_exist_in_fetcher(self):
        """Confirm the old function was never added so this test stays meaningful."""
        import core.index_ticker_fetcher as fetcher
        assert not hasattr(fetcher, "get_large_cap_tickers"), (
            "get_large_cap_tickers was added — update backtest_pnl.py if intentional"
        )

    def test_get_all_index_tickers_accepts_sp500_nasdaq100(self):
        """get_all_index_tickers(indices=['sp500','nasdaq100']) must not raise."""
        from core.index_ticker_fetcher import get_all_index_tickers

        with patch("core.index_ticker_fetcher.IndexTickerFetcher.get_all_tickers",
                   return_value=["AAPL", "NVDA", "MSFT"]):
            result = get_all_index_tickers(indices=["sp500", "nasdaq100"])

        assert isinstance(result, list)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Bug 3 — scheduler retried scan every 30s when auto_trader returned early
#          because last_scan_date was only set inside the try block
# ---------------------------------------------------------------------------


class TestBug3SchedulerNoRetryLoop:
    """last_scan_date must be set even when _run_cycle raises or returns early."""

    def test_last_scan_date_set_before_run_cycle_call(self):
        """Simulate: _run_cycle raises. last_scan_date must still be updated."""
        from scheduler import run_scheduler

        call_count = {"n": 0}

        def _failing_cycle(_dry_run):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Alpaca down")
            # Second call should not happen — the date was already marked

        tick = {"count": 0}

        def _mock_now():
            tick["count"] += 1
            # Ticks: market-open at 09:32, then 09:33 (should NOT re-trigger scan)
            base = datetime(2024, 4, 15, 9, 32, 0, tzinfo=_ET)
            from datetime import timedelta
            return base + timedelta(seconds=(tick["count"] - 1) * 30)

        def _mock_sleep(_secs):
            if tick["count"] >= 3:
                raise KeyboardInterrupt  # stop the loop after 2 ticks

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler._run_cycle", side_effect=_failing_cycle), \
             patch("scheduler.FillMonitor") as mock_fm, \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]):
            mock_fm.return_value.start = MagicMock()
            mock_fm.return_value.stop = MagicMock()
            run_scheduler(dry_run=True)

        # _run_cycle must have been called exactly once, not retried
        assert call_count["n"] == 1, (
            f"_run_cycle was called {call_count['n']} times — retry loop bug is back"
        )


# ---------------------------------------------------------------------------
# Bug 4 — 16:01 ET hourly check never fired because _is_market_hours(16:01) = False
#          The 15:00–16:00 bar (last of the day) was never evaluated
# ---------------------------------------------------------------------------


class TestBug4LastHourlyCheckFires:
    """The hourly exit check must fire at 16:01 ET (last bar of the day)."""

    def test_16_01_is_in_exit_monitor_window(self):
        from scheduler import _is_exit_monitor_window

        dt_16_01 = datetime(2024, 4, 15, 16, 1, 0, tzinfo=_ET)  # Monday
        assert _is_exit_monitor_window(dt_16_01), (
            "16:01 is outside exit monitor window — last hourly bar will never be checked"
        )

    def test_16_06_is_outside_exit_monitor_window(self):
        from scheduler import _is_exit_monitor_window

        dt_16_06 = datetime(2024, 4, 15, 16, 6, 0, tzinfo=_ET)
        assert not _is_exit_monitor_window(dt_16_06)

    def test_16_01_not_in_regular_market_hours(self):
        """_is_market_hours should still return False at 16:01 (entries gate)."""
        from scheduler import _is_market_hours

        dt_16_01 = datetime(2024, 4, 15, 16, 1, 0, tzinfo=_ET)
        assert not _is_market_hours(dt_16_01)

    def test_hourly_check_triggers_at_16_01(self):
        """Simulate scheduler tick at 16:01 — monitor_exits_hourly must be called."""
        from scheduler import run_scheduler

        hourly_call_times = []

        tick = {"count": 0}

        def _mock_now():
            tick["count"] += 1
            # Tick 1: 16:01 (exit monitor window, hourly check should fire)
            # Tick 2: stop
            return datetime(2024, 4, 15, 16, 1, 30, tzinfo=_ET)

        def _mock_sleep(_secs):
            if tick["count"] >= 2:
                raise KeyboardInterrupt

        def _mock_hourly():
            hourly_call_times.append(True)
            return []

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler.monitor_exits_hourly", side_effect=_mock_hourly), \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.FillMonitor") as mock_fm:
            mock_fm.return_value.start = MagicMock()
            mock_fm.return_value.stop = MagicMock()
            run_scheduler(dry_run=True)

        assert hourly_call_times, "monitor_exits_hourly not called at 16:01 — last bar missed"


class TestSchedulerSkipsHolidayWhenClockClosed:
    """A weekday holiday should not trigger scan or exit work when Alpaca says closed."""

    def test_holiday_clock_closed_skips_scan_and_exit_checks(self):
        from scheduler import run_scheduler

        tick = {"count": 0}

        def _mock_now():
            tick["count"] += 1
            return datetime(2024, 1, 15, 10, 1, 0, tzinfo=_ET)  # MLK Day, Monday

        def _mock_sleep(_secs):
            if tick["count"] >= 2:
                raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler._market_clock_is_open", return_value=False), \
             patch("scheduler._run_cycle") as mock_cycle, \
             patch("scheduler.monitor_exits_hourly", return_value=[]) as mock_hourly, \
             patch("scheduler.monitor_and_exit_positions", return_value=[]) as mock_daily, \
             patch("scheduler.FillMonitor") as mock_fm:
            mock_fm.return_value.start = MagicMock()
            mock_fm.return_value.stop = MagicMock()
            run_scheduler(dry_run=True)

        mock_cycle.assert_not_called()
        mock_hourly.assert_not_called()
        mock_daily.assert_not_called()


# ---------------------------------------------------------------------------
# Startup reconciliation — existing positions must be re-protected if the
# websocket was down during a prior fill.
# ---------------------------------------------------------------------------


class TestStartupStopReconciliation:
    def test_live_scheduler_runs_startup_reconciliation_after_monitor_start(self):
        from scheduler import run_scheduler

        call_order: list[str] = []

        def _mock_now():
            return datetime(2024, 4, 15, 9, 20, 0, tzinfo=_ET)

        def _mock_sleep(_secs):
            raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler._run_startup_stop_reconciliation",
                   side_effect=lambda: call_order.append("reconcile")), \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]), \
             patch("scheduler.FillMonitor") as mock_fm:
            mock_fm.return_value.start.side_effect = lambda: call_order.append("monitor_start")
            mock_fm.return_value.stop = MagicMock()
            run_scheduler(dry_run=False)

        assert call_order == ["monitor_start", "reconcile"]

    def test_dry_run_scheduler_skips_startup_reconciliation(self):
        from scheduler import run_scheduler

        def _mock_now():
            return datetime(2024, 4, 15, 9, 20, 0, tzinfo=_ET)

        def _mock_sleep(_secs):
            raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler._run_startup_stop_reconciliation") as mock_reconcile, \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]), \
             patch("scheduler.FillMonitor") as mock_fm:
            mock_fm.return_value.start = MagicMock()
            mock_fm.return_value.stop = MagicMock()
            run_scheduler(dry_run=True)

        mock_reconcile.assert_not_called()


class TestFillMonitorRecovery:
    """The scheduler must restart a dead Alpaca trade-update stream."""

    def test_scheduler_restarts_dead_fill_monitor(self):
        from scheduler import run_scheduler

        first_monitor = MagicMock()
        first_monitor.is_running.return_value = False
        first_monitor.start = MagicMock()
        first_monitor.stop = MagicMock()

        replacement_monitor = MagicMock()
        replacement_monitor.is_running.return_value = True
        replacement_monitor.start = MagicMock()
        replacement_monitor.stop = MagicMock()

        tick = {"count": 0}

        def _mock_now():
            tick["count"] += 1
            return datetime(2024, 4, 15, 9, 20, 0, tzinfo=_ET)

        def _mock_sleep(_secs):
            raise KeyboardInterrupt

        with patch("scheduler._now_et", side_effect=_mock_now), \
             patch("scheduler.time.sleep", side_effect=_mock_sleep), \
             patch("scheduler.monitor_and_exit_positions", return_value=[]), \
             patch("scheduler.monitor_exits_hourly", return_value=[]), \
             patch("scheduler.FillMonitor", side_effect=[first_monitor, replacement_monitor]):
            run_scheduler(dry_run=True)

        first_monitor.start.assert_called_once()
        first_monitor.stop.assert_called_once()
        replacement_monitor.start.assert_called_once()
        replacement_monitor.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Bug 5 — verify_paper_trading used bars["close"] → unhandled KeyError crash
#          leaving FillMonitor running without cleanup
# ---------------------------------------------------------------------------


class TestBug5VerifyPaperTradingColumnName:
    """verify_paper_trading must use bars['Close'], not bars['close']."""

    def test_verify_script_uses_capital_close(self):
        """Grep the source for the correct column name."""
        with open("verify_paper_trading.py") as f:
            src = f.read()

        # Should not contain the lowercase version in a price-access context
        assert 'bars["close"]' not in src and "bars['close']" not in src, (
            "verify_paper_trading.py still uses bars['close'] — will crash at runtime"
        )
        assert 'bars["Close"]' in src or "bars['Close']" in src, (
            "verify_paper_trading.py does not access bars['Close'] at all — unexpected"
        )

    def test_verify_script_prefers_intraday_price_when_available(self):
        with open("verify_paper_trading.py") as f:
            src = f.read()

        assert "fetch_latest_intraday_price" in src, (
            "verify_paper_trading.py no longer prefers an intraday price reference"
        )


# ---------------------------------------------------------------------------
# Bug 6 — cycle summary email was never sent (_run_cycle had empty body)
# ---------------------------------------------------------------------------


class TestAutoTraderCycleResult:
    """The auto-trader must return exact outcomes for scheduler reporting."""

    def test_run_auto_trader_returns_entered_and_exited_symbols(self):
        from auto_trader import AutoTraderCycleResult, run_auto_trader

        with (
            patch("auto_trader.monitor_and_exit_positions", return_value=["AAPL"]),
            patch("auto_trader.scan_for_canslim_stocks", return_value=([{"symbol": "NVDA"}], [], "uptrend")),
            patch("auto_trader.execute_entries", return_value=["NVDA"]),
        ):
            result = run_auto_trader(dry_run=True)

        assert result == AutoTraderCycleResult(entered=("NVDA",), exited=("AAPL",))

    def test_run_auto_trader_skip_flags_return_empty_outcomes(self):
        from auto_trader import AutoTraderCycleResult, run_auto_trader

        with patch("auto_trader.scan_for_canslim_stocks", return_value=([], [], "uptrend")):
            result = run_auto_trader(dry_run=True, skip_entries=True, skip_exits=True)

        assert result == AutoTraderCycleResult()

    def test_market_closed_guard_returns_empty_outcomes(self):
        from auto_trader import AutoTraderCycleResult, run_auto_trader

        with (
            patch("auto_trader.settings.ENTRY_MARKET_HOURS_ONLY", True),
            patch("auto_trader._is_market_open", return_value=False),
        ):
            result = run_auto_trader(dry_run=False)

        assert result == AutoTraderCycleResult()


class TestBug6CycleSummaryEmail:
    """_run_cycle must call notify_cycle_summary after run_auto_trader completes."""

    def test_run_cycle_calls_notify_cycle_summary(self):
        from scheduler import _run_cycle

        with patch("scheduler.run_auto_trader") as mock_trader, \
             patch("scheduler.notify_cycle_summary") as mock_notify, \
             patch("scheduler._is_paper_mode", return_value=True):
            _run_cycle(dry_run=True)

        mock_trader.assert_called_once_with(dry_run=True)
        mock_notify.assert_called_once()

    def test_run_cycle_does_not_crash_if_notify_fails(self):
        """notify_cycle_summary failure must be swallowed — trading must not be blocked."""
        from scheduler import _run_cycle

        with patch("scheduler.run_auto_trader"), \
             patch("scheduler.notify_cycle_summary", side_effect=RuntimeError("SMTP down")), \
             patch("scheduler._is_paper_mode", return_value=True):
            # Must not raise
            _run_cycle(dry_run=True)
