"""Unit tests for hourly exit monitoring and fetch_hourly_ohlcv."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hourly_bars(n: int = 60, trend: str = "flat", base: float = 100.0) -> pd.DataFrame:
    """Generate synthetic 1H OHLCV bars.

    Args:
        n: Number of hourly bars.
        trend: 'flat', 'rising', or 'falling'.
        base: Starting close price.
    """
    if trend == "rising":
        closes = [base * (1 + i * 0.002) for i in range(n)]
    elif trend == "falling":
        closes = [base * (1 - i * 0.002) for i in range(n)]
    else:
        closes = [base] * n

    index = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame({
        "Open":   closes,
        "High":   [c * 1.001 for c in closes],
        "Low":    [c * 0.999 for c in closes],
        "Close":  closes,
        "Volume": [1_000_000] * n,
    }, index=index)


def _make_position(symbol: str = "NVDA", unrealized_pl_pct: float = -0.01) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        qty=10.0,
        avg_entry_price=100.0,
        current_price=100.0 * (1 + unrealized_pl_pct),
        unrealized_pl_pct=unrealized_pl_pct,
    )


# ---------------------------------------------------------------------------
# fetch_hourly_ohlcv
# ---------------------------------------------------------------------------


class TestFetchHourlyOhlcv:
    """Tests for core.data_client.fetch_hourly_ohlcv."""

    def test_returns_dataframe_with_correct_columns(self):
        """Mocked Alpaca call returns a properly structured DataFrame."""
        from core.data_client import fetch_hourly_ohlcv

        index = pd.DatetimeIndex(
            [
                pd.Timestamp("2024-01-02 09:30", tz="America/New_York"),
                pd.Timestamp("2024-01-02 10:30", tz="America/New_York"),
                pd.Timestamp("2024-01-02 11:30", tz="America/New_York"),
            ]
        ).tz_convert("UTC")
        mock_bars = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0],
                "High": [100.5, 101.5, 102.5],
                "Low": [99.5, 100.5, 101.5],
                "Close": [100.2, 101.2, 102.2],
                "Volume": [1_000_000, 1_100_000, 1_200_000],
            },
            index=index,
        )
        # Rename to Alpaca lowercase convention to test renaming logic
        mock_bars_lc = mock_bars.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })

        mock_client = MagicMock()
        mock_barset = MagicMock()
        mock_barset.df = mock_bars_lc
        mock_client.get_stock_bars.return_value = mock_barset

        with patch("core.data_client._get_alpaca_client", return_value=mock_client), \
             patch("core.data_client._cache_get", return_value=None), \
             patch("core.data_client._cache_set"):
            result = fetch_hourly_ohlcv("NVDA", days=10)

        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert len(result) == 3

    def test_returns_empty_dataframe_on_api_error(self):
        from core.data_client import fetch_hourly_ohlcv

        mock_client = MagicMock()
        mock_client.get_stock_bars.side_effect = RuntimeError("API down")

        with patch("core.data_client._get_alpaca_client", return_value=mock_client), \
             patch("core.data_client._cache_get", return_value=None):
            result = fetch_hourly_ohlcv("FAIL", days=5)

        assert result.empty

    def test_returns_cached_result_without_api_call(self):
        from core.data_client import fetch_hourly_ohlcv

        cached = _make_hourly_bars(30)

        with patch("core.data_client._cache_get", return_value=cached), \
             patch("core.data_client._get_alpaca_client") as mock_api:
            result = fetch_hourly_ohlcv("SPY", days=10)

        mock_api.assert_not_called()
        assert len(result) == 30

    def test_drops_timezone_from_index(self):
        from core.data_client import fetch_hourly_ohlcv

        mock_bars = _make_hourly_bars(20)
        # Simulate tz-aware index (Alpaca returns UTC)
        mock_bars.index = mock_bars.index.tz_localize("UTC")
        mock_bars_lc = mock_bars.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })

        mock_client = MagicMock()
        mock_barset = MagicMock()
        mock_barset.df = mock_bars_lc
        mock_client.get_stock_bars.return_value = mock_barset

        with patch("core.data_client._get_alpaca_client", return_value=mock_client), \
             patch("core.data_client._cache_get", return_value=None), \
             patch("core.data_client._cache_set"):
            result = fetch_hourly_ohlcv("AAPL", days=5)

        assert result.index.tz is None

    def test_filters_extended_hours_bars(self):
        from core.data_client import fetch_hourly_ohlcv

        eastern = "America/New_York"
        idx_et = pd.DatetimeIndex(
            [
                pd.Timestamp("2024-01-02 08:30", tz=eastern),
                pd.Timestamp("2024-01-02 09:30", tz=eastern),
                pd.Timestamp("2024-01-02 10:30", tz=eastern),
                pd.Timestamp("2024-01-02 16:30", tz=eastern),
            ]
        )
        mock_bars = pd.DataFrame(
            {
                "open": [1.0, 2.0, 3.0, 4.0],
                "high": [1.0, 2.0, 3.0, 4.0],
                "low": [1.0, 2.0, 3.0, 4.0],
                "close": [1.0, 2.0, 3.0, 4.0],
                "volume": [100, 200, 300, 400],
            },
            index=idx_et.tz_convert("UTC"),
        )

        mock_client = MagicMock()
        mock_barset = MagicMock()
        mock_barset.df = mock_bars
        mock_client.get_stock_bars.return_value = mock_barset

        with patch("core.data_client._get_alpaca_client", return_value=mock_client), \
             patch("core.data_client._cache_get", return_value=None), \
             patch("core.data_client._cache_set"):
            result = fetch_hourly_ohlcv("AAPL", days=5)

        assert list(result["Close"]) == [2.0, 3.0]
        assert [ts.strftime("%H:%M") for ts in result.index] == ["09:30", "10:30"]


class TestFetchLatestIntradayPrice:
    def test_returns_latest_regular_session_minute_close(self):
        from core.data_client import fetch_latest_intraday_price

        eastern = "America/New_York"
        idx_et = pd.DatetimeIndex(
            [
                pd.Timestamp("2024-01-02 08:15", tz=eastern),
                pd.Timestamp("2024-01-02 09:30", tz=eastern),
                pd.Timestamp("2024-01-02 09:31", tz=eastern),
                pd.Timestamp("2024-01-02 16:10", tz=eastern),
            ]
        )
        mock_bars = pd.DataFrame(
            {"close": [98.0, 100.0, 101.5, 99.0]},
            index=idx_et.tz_convert("UTC"),
        )

        mock_client = MagicMock()
        mock_barset = MagicMock()
        mock_barset.df = mock_bars
        mock_client.get_stock_bars.return_value = mock_barset

        with patch("core.data_client._get_alpaca_client", return_value=mock_client):
            result = fetch_latest_intraday_price("NVDA")

        assert result == pytest.approx(101.5)


# ---------------------------------------------------------------------------
# monitor_exits_hourly
# ---------------------------------------------------------------------------


class TestMonitorExitsHourly:
    """Tests for auto_trader.monitor_exits_hourly."""

    def _patch_positions(self, positions):
        return patch("auto_trader.get_open_positions", return_value=positions)

    def _patch_hourly_bars(self, bars):
        return patch("auto_trader.fetch_hourly_ohlcv", return_value=bars)

    def test_returns_empty_list_when_no_positions(self):
        from auto_trader import monitor_exits_hourly

        with self._patch_positions([]):
            result = monitor_exits_hourly()

        assert result == []

    def test_triggers_stop_loss_on_large_loss(self):
        from auto_trader import monitor_exits_hourly

        pos = _make_position("NVDA", unrealized_pl_pct=-0.08)  # 8% loss > 7% threshold
        bars = _make_hourly_bars(40, trend="flat")
        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars), \
             patch("auto_trader.check_exit_signals", return_value=[pos]), \
             patch("auto_trader.OrderManager", return_value=manager):
            result = monitor_exits_hourly()

        assert "NVDA" in result

    def test_no_exit_when_loss_is_below_threshold(self):
        from auto_trader import monitor_exits_hourly

        pos = _make_position("NVDA", unrealized_pl_pct=-0.03)  # only 3% — no stop
        bars = _make_hourly_bars(40, trend="rising")

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.OrderManager") as mock_manager_cls:
            result = monitor_exits_hourly()

        mock_manager_cls.return_value.submit_exit.assert_not_called()
        assert result == []

    def test_hourly_ma_violation_triggers_exit(self):
        """Two consecutive hourly closes below 21-period EMA triggers sell."""
        from auto_trader import monitor_exits_hourly

        pos = _make_position("AAPL", unrealized_pl_pct=-0.03)

        # 30 bars rising, then last 5 bars sharply falling below EMA
        bars_up = _make_hourly_bars(30, trend="rising", base=100.0)
        # Force the last 2 bars to be well below the EMA of the rising series
        ema_val = float(bars_up["Close"].ewm(span=21, adjust=False).mean().iloc[-1])
        bars_up.iloc[-1, bars_up.columns.get_loc("Close")] = ema_val * 0.95
        bars_up.iloc[-2, bars_up.columns.get_loc("Close")] = ema_val * 0.95
        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars_up), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.OrderManager", return_value=manager):
            result = monitor_exits_hourly(ema_period=21, consecutive=2)

        assert "AAPL" in result

    def test_hourly_ma_no_exit_when_above_ema(self):
        """Bars above EMA do not trigger exit."""
        from auto_trader import monitor_exits_hourly

        pos = _make_position("CRWD", unrealized_pl_pct=0.05)
        bars = _make_hourly_bars(40, trend="rising", base=100.0)

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.OrderManager") as mock_manager_cls:
            result = monitor_exits_hourly()

        mock_manager_cls.return_value.submit_exit.assert_not_called()
        assert result == []

    def test_skips_symbol_if_insufficient_bars(self):
        """Symbols with fewer bars than ema_period are skipped gracefully."""
        from auto_trader import monitor_exits_hourly

        pos = _make_position("NEW", unrealized_pl_pct=-0.02)
        # Only 5 bars — not enough for 21-period EMA
        bars = _make_hourly_bars(5, trend="flat")

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars), \
             patch("auto_trader.check_exit_signals", return_value=[]), \
             patch("auto_trader.OrderManager") as mock_manager_cls:
            result = monitor_exits_hourly(ema_period=21)

        mock_manager_cls.return_value.submit_exit.assert_not_called()
        assert result == []

    def test_does_not_double_exit_stop_loss_symbol(self):
        """A symbol already exited via stop-loss is not re-processed for MA."""
        from auto_trader import monitor_exits_hourly

        pos = _make_position("NVDA", unrealized_pl_pct=-0.08)
        # Even though MA would also trigger, stop-loss exits first
        bars = _make_hourly_bars(30, trend="falling", base=100.0)
        manager = MagicMock()
        manager.submit_exit.return_value = SimpleNamespace(success=True)

        with self._patch_positions([pos]), \
             self._patch_hourly_bars(bars), \
             patch("auto_trader.check_exit_signals", return_value=[pos]), \
             patch("auto_trader.OrderManager", return_value=manager):
            result = monitor_exits_hourly()

        assert manager.submit_exit.call_count == 1
        assert result == ["NVDA"]


# ---------------------------------------------------------------------------
# Backtest per-year breakdown
# ---------------------------------------------------------------------------


class TestAnnualBreakdown:
    """Test that print_pnl_report outputs annual returns when multi-year data exists."""

    def test_annual_breakdown_printed_for_multi_year_equity_curve(self, capsys):
        from backtest_pnl import SimulationResult, print_pnl_report

        # Build a 2-year equity curve with known returns
        dates_2023 = pd.date_range("2023-01-02", "2023-12-29", freq="B")
        dates_2024 = pd.date_range("2024-01-02", "2024-12-27", freq="B")
        dates = dates_2023.append(dates_2024)

        equity = pd.Series(
            [100_000] * len(dates_2023) + [120_000] * len(dates_2024),
            index=dates.astype(str),
        )
        spy = pd.Series(
            [100_000] * len(dates_2023) + [115_000] * len(dates_2024),
            index=dates.astype(str),
        )

        result = SimulationResult(
            equity_curve=equity,
            benchmark_curve=spy,
            initial_capital=100_000,
            config={"tickers": ["NVDA"], "stop_loss_pct": 0.07,
                    "ma_exit_period": 21, "max_positions": 5,
                    "min_canslim_score": 40, "min_rs_score": 75},
        )

        print_pnl_report(result)
        captured = capsys.readouterr().out

        assert "Annual Returns" in captured
        assert "2023" in captured
        assert "2024" in captured

    def test_no_annual_breakdown_for_single_year(self, capsys):
        from backtest_pnl import SimulationResult, print_pnl_report

        dates = pd.date_range("2024-01-02", "2024-06-28", freq="B")
        equity = pd.Series([100_000] * len(dates), index=dates.astype(str))

        result = SimulationResult(
            equity_curve=equity,
            benchmark_curve=pd.Series(dtype=float),
            initial_capital=100_000,
            config={"tickers": [], "stop_loss_pct": 0.07,
                    "ma_exit_period": 21, "max_positions": 5,
                    "min_canslim_score": 40, "min_rs_score": 75},
        )

        print_pnl_report(result)
        captured = capsys.readouterr().out

        assert "Annual Returns" not in captured
