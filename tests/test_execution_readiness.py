"""Execution-readiness regressions for broker mutation boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from auto_trader import (
    execute_entries,
    monitor_and_exit_positions,
    monitor_exits_hourly,
    run_auto_trader,
)
from core.order_execution import PositionSummary


def _position(*, loss_pct: float = 0.0) -> PositionSummary:
    return PositionSummary(
        symbol="NVDA",
        qty=1.0,
        avg_entry_price=100.0,
        current_price=100.0 * (1 + loss_pct),
        unrealized_pl_pct=loss_pct,
    )


def _falling_bars(count: int = 30) -> pd.DataFrame:
    closes = [130.0 - index for index in range(count)]
    return pd.DataFrame({"Close": closes})


def _entry_signal() -> dict[str, object]:
    return {
        "symbol": "NVDA",
        "total_score": 90.0,
        "rs_score": 95.0,
        "is_breakout": True,
        "has_volume_surge": True,
        "buy_point": 100.0,
    }


def _entry_plan() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="NVDA",
        entry_price=101.0,
        price_source="intraday_minute_close",
        stop_price=93.93,
        stop_loss_pct=0.07,
        position_value=10_000.0,
        risk_amount=700.0,
        risk_per_share=7.07,
        qty=99.0099,
        canslim_score=90.0,
        rs_score=95.0,
        is_breakout=True,
        has_volume_surge=True,
    )


def test_live_entry_rechecks_readiness_after_price_fetch() -> None:
    state = {"ready": True}
    manager = MagicMock()

    def build_plan(**_kwargs):
        state["ready"] = False
        return _entry_plan()

    with (
        patch("auto_trader._get_account_equity", return_value=100_000.0),
        patch("auto_trader._is_market_open", return_value=True),
        patch("auto_trader.get_open_positions", return_value=[]),
        patch("auto_trader.get_open_orders", return_value=[]),
        patch("auto_trader._build_entry_execution_plan", side_effect=build_plan),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="readiness"):
            execute_entries(
                [_entry_signal()],
                dry_run=False,
                execution_ready=lambda: state["ready"],
            )

    manager.submit_entry.assert_not_called()


def test_live_entry_rechecks_market_clock_after_price_fetch() -> None:
    manager = MagicMock()

    with (
        patch("auto_trader._get_account_equity", return_value=100_000.0),
        patch("auto_trader._is_market_open", side_effect=[True, False]),
        patch("auto_trader.get_open_positions", return_value=[]),
        patch("auto_trader.get_open_orders", return_value=[]),
        patch("auto_trader._build_entry_execution_plan", return_value=_entry_plan()),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="market clock"):
            execute_entries(
                [_entry_signal()],
                dry_run=False,
                execution_ready=lambda: True,
            )

    manager.submit_entry.assert_not_called()


def test_daily_exit_rechecks_readiness_after_data_fetch() -> None:
    state = {"ready": True}
    manager = MagicMock()

    def fetch_bars(*_args, **_kwargs):
        state["ready"] = False
        return _falling_bars()

    with (
        patch("auto_trader.get_open_positions", return_value=[_position()]),
        patch("auto_trader.check_exit_signals", return_value=[]),
        patch("auto_trader.fetch_ohlcv", side_effect=fetch_bars),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="readiness"):
            monitor_and_exit_positions(
                dry_run=False,
                execution_ready=lambda: state["ready"],
            )

    manager.submit_exit.assert_not_called()


def test_hourly_exit_rechecks_readiness_after_data_fetch() -> None:
    state = {"ready": True}
    manager = MagicMock()

    def fetch_bars(*_args, **_kwargs):
        state["ready"] = False
        return _falling_bars()

    with (
        patch("auto_trader.get_open_positions", return_value=[_position()]),
        patch("auto_trader.check_exit_signals", return_value=[]),
        patch("auto_trader.fetch_hourly_ohlcv", side_effect=fetch_bars),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="readiness"):
            monitor_exits_hourly(
                dry_run=False,
                execution_ready=lambda: state["ready"],
            )

    manager.submit_exit.assert_not_called()


def test_full_cycle_rechecks_readiness_after_long_scan() -> None:
    state = {"ready": True}
    manager = MagicMock()

    def scan():
        state["ready"] = False
        return [_entry_signal()], [], "uptrend"

    with (
        patch("auto_trader._is_market_open", return_value=True),
        patch("auto_trader.scan_for_canslim_stocks", side_effect=scan),
        patch("auto_trader._get_account_equity", return_value=100_000.0),
        patch("auto_trader.get_open_positions", return_value=[]),
        patch("auto_trader.get_open_orders", return_value=[]),
        patch("auto_trader._build_entry_execution_plan", return_value=_entry_plan()),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="readiness"):
            run_auto_trader(
                dry_run=False,
                skip_exits=True,
                execution_ready=lambda: state["ready"],
            )

    manager.submit_entry.assert_not_called()


def test_live_direct_auto_trader_requires_readiness_callback() -> None:
    with (
        patch("auto_trader._is_market_open", return_value=True) as market_open,
        patch("auto_trader.monitor_and_exit_positions", return_value=[]),
        patch(
            "auto_trader.scan_for_canslim_stocks",
            return_value=([], [], "uptrend"),
        ) as scan,
    ):
        with pytest.raises(RuntimeError, match="readiness callback"):
            run_auto_trader(dry_run=False)

    market_open.assert_called_once_with()
    scan.assert_not_called()


def test_dry_run_does_not_consult_live_readiness() -> None:
    readiness = MagicMock(side_effect=AssertionError("dry run consulted live readiness"))
    manager = MagicMock()

    with (
        patch("auto_trader.get_open_positions", return_value=[_position(loss_pct=-0.10)]),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        exited = monitor_and_exit_positions(
            dry_run=True,
            execution_ready=readiness,
        )

    assert exited == ["NVDA"]
    readiness.assert_not_called()
    manager.submit_exit.assert_not_called()


@pytest.mark.parametrize("monitor_name", ["daily", "hourly"])
def test_hard_stop_rechecks_readiness_after_broker_signal_check(
    monitor_name: str,
) -> None:
    state = {"ready": True}
    manager = MagicMock()

    def check_signals(*_args, **_kwargs):
        state["ready"] = False
        return [_position(loss_pct=-0.10)]

    monitor = (
        monitor_and_exit_positions
        if monitor_name == "daily"
        else monitor_exits_hourly
    )
    with (
        patch("auto_trader.get_open_positions", return_value=[_position(loss_pct=-0.10)]),
        patch("auto_trader.check_exit_signals", side_effect=check_signals),
        patch("auto_trader.OrderManager", return_value=manager),
    ):
        with pytest.raises(RuntimeError, match="readiness"):
            monitor(
                dry_run=False,
                execution_ready=lambda: state["ready"],
            )

    manager.submit_exit.assert_not_called()
