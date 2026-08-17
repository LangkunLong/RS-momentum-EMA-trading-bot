"""Regression tests proving dry-run mode cannot submit exit orders."""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from auto_trader import (
    monitor_and_exit_positions,
    monitor_exits_hourly,
    run_auto_trader,
)
from core.order_execution import PositionSummary
from scheduler import run_scheduler


_ET = ZoneInfo("America/New_York")


def _breached_position() -> PositionSummary:
    return PositionSummary(
        symbol="NVDA",
        qty=2.0,
        avg_entry_price=100.0,
        current_price=90.0,
        unrealized_pl_pct=-0.10,
    )


def test_daily_exit_monitor_dry_run_does_not_submit() -> None:
    with (
        patch("auto_trader.get_open_positions", return_value=[_breached_position()]),
        patch("auto_trader.OrderManager") as manager_cls,
    ):
        exited = monitor_and_exit_positions(dry_run=True)

    assert exited == ["NVDA"]
    manager_cls.return_value.submit_exit.assert_not_called()


def test_hourly_exit_monitor_dry_run_does_not_submit() -> None:
    with (
        patch("auto_trader.get_open_positions", return_value=[_breached_position()]),
        patch("auto_trader.OrderManager") as manager_cls,
    ):
        exited = monitor_exits_hourly(dry_run=True)

    assert exited == ["NVDA"]
    manager_cls.return_value.submit_exit.assert_not_called()


def test_auto_trader_propagates_dry_run_to_exit_monitor() -> None:
    with (
        patch("auto_trader.monitor_and_exit_positions", return_value=[]) as monitor,
        patch("auto_trader.scan_for_canslim_stocks", return_value=([], [], "uptrend")),
    ):
        run_auto_trader(dry_run=True)

    monitor.assert_called_once_with(dry_run=True)


def test_scheduler_propagates_dry_run_to_all_exit_monitors() -> None:
    monitor = MagicMock()
    monitor.is_running.return_value = True

    with (
        patch("scheduler._now_et", return_value=datetime(2026, 8, 17, 10, 1, tzinfo=_ET)),
        patch("scheduler._market_clock_is_open", return_value=True),
        patch("scheduler.time.sleep", side_effect=KeyboardInterrupt),
        patch("scheduler._run_cycle"),
        patch("scheduler.monitor_exits_hourly", return_value=[]) as hourly,
        patch("scheduler.monitor_and_exit_positions", return_value=[]) as daily,
        patch("scheduler.FillMonitor", return_value=monitor),
    ):
        run_scheduler(dry_run=True)

    hourly.assert_called_once_with(dry_run=True)
    daily.assert_called_once_with(dry_run=True)


def test_dry_run_scheduler_does_not_start_fill_monitor() -> None:
    with (
        patch("scheduler._now_et", return_value=datetime(2026, 8, 17, 8, 0, tzinfo=_ET)),
        patch("scheduler.time.sleep", side_effect=KeyboardInterrupt),
        patch("scheduler.FillMonitor") as monitor_cls,
    ):
        run_scheduler(dry_run=True)

    monitor_cls.assert_not_called()


def test_entry_cycle_aborts_when_broker_state_cannot_be_inspected() -> None:
    opportunity = {
        "symbol": "NVDA",
        "total_score": 90.0,
        "rs_score": 95.0,
        "is_breakout": True,
        "has_volume_surge": True,
        "buy_point": 100.0,
    }

    with (
        patch("auto_trader._get_account_equity", return_value=100_000.0),
        patch("auto_trader._is_market_open", return_value=True),
        patch(
            "auto_trader.get_open_positions",
            side_effect=RuntimeError("broker state unavailable"),
        ) as positions,
        patch("auto_trader.get_open_orders") as orders,
        patch("auto_trader.OrderManager") as manager_cls,
    ):
        from auto_trader import execute_entries

        entered = execute_entries([opportunity], dry_run=False)

    assert entered == []
    positions.assert_called_once_with(raise_on_error=True)
    orders.assert_not_called()
    manager_cls.return_value.submit_entry.assert_not_called()
