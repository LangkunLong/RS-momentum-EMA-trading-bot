"""High-impact runtime gates for the paper-trading scheduler."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import scheduler
from config import settings


_ET = ZoneInfo("America/New_York")


def test_scheduler_instance_lock_rejects_a_second_process(tmp_path) -> None:
    lock_path = tmp_path / "scheduler.lock"

    with scheduler.SchedulerInstanceLock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with scheduler.SchedulerInstanceLock(lock_path):
                pass

    with scheduler.SchedulerInstanceLock(lock_path):
        pass


def test_monitor_connection_wait_fails_closed() -> None:
    monitor = MagicMock()
    monitor.is_connected.return_value = False

    with pytest.raises(RuntimeError, match="did not become connected"):
        scheduler._wait_for_fill_monitor_connection(
            monitor,
            timeout_seconds=0,
            poll_seconds=0,
        )


def test_unhealthy_monitor_must_stop_before_replacement() -> None:
    monitor = MagicMock()
    monitor.is_connected.return_value = False
    monitor.stop.return_value = False

    with (
        patch("scheduler.FillMonitor") as replacement,
        patch("scheduler._run_startup_stop_reconciliation"),
    ):
        with pytest.raises(
            RuntimeError,
            match="Unhealthy fill monitor did not terminate; refusing replacement",
        ):
            scheduler._ensure_fill_monitor_running(monitor, dry_run=False)

    replacement.assert_not_called()


def test_failed_startup_reconciliation_raises() -> None:
    failed = SimpleNamespace(
        symbol="SPY",
        success=False,
        action="submit_failed",
        error="broker unavailable",
    )
    manager = MagicMock()
    manager.reconcile_startup_stops.return_value = [failed]

    with patch("scheduler.OrderManager", return_value=manager):
        with pytest.raises(RuntimeError, match="SPY"):
            scheduler._run_startup_stop_reconciliation()


def test_live_scheduler_does_not_trade_when_broker_clock_is_unavailable() -> None:
    monitor = MagicMock()
    monitor.is_running.return_value = True
    monitor.is_connected.return_value = True

    with (
        patch(
            "scheduler._now_et",
            return_value=datetime(2026, 8, 17, 10, 1, tzinfo=_ET),
        ),
        patch("scheduler._market_clock_is_open", return_value=None),
        patch("scheduler.time.sleep", side_effect=KeyboardInterrupt),
        patch("scheduler._run_cycle") as cycle,
        patch("scheduler.monitor_exits_hourly") as hourly,
        patch("scheduler.monitor_and_exit_positions") as daily,
        patch("scheduler._run_startup_stop_reconciliation"),
        patch("scheduler.FillMonitor", return_value=monitor),
    ):
        scheduler.run_scheduler(dry_run=False)

    cycle.assert_not_called()
    hourly.assert_not_called()
    daily.assert_not_called()
    monitor.stop.assert_called_once()


def test_live_now_cycle_receives_dynamic_monitor_readiness() -> None:
    state = {"ready": True}
    observed: list[bool | None] = []
    monitor = MagicMock()
    monitor.is_connected.side_effect = lambda: state["ready"]
    monitor.stop.return_value = True

    def cycle(_dry_run: bool, *, execution_ready=None) -> None:
        state["ready"] = False
        observed.append(execution_ready() if execution_ready is not None else None)
        raise KeyboardInterrupt

    with (
        patch(
            "scheduler._now_et",
            return_value=datetime(2026, 8, 17, 10, 1, tzinfo=_ET),
        ),
        patch("scheduler._market_clock_is_open", return_value=True),
        patch("scheduler._run_cycle", side_effect=cycle),
        patch("scheduler._run_startup_stop_reconciliation"),
        patch("scheduler.FillMonitor", return_value=monitor),
    ):
        scheduler.run_scheduler(dry_run=False, run_now=True)

    assert observed == [False]


def test_scheduled_cycle_receives_dynamic_monitor_readiness() -> None:
    state = {"ready": True}
    observed: list[bool | None] = []
    monitor = MagicMock()
    monitor.is_connected.side_effect = lambda: state["ready"]
    monitor.stop.return_value = True

    def cycle(_dry_run: bool, *, execution_ready=None) -> None:
        state["ready"] = False
        observed.append(execution_ready() if execution_ready is not None else None)

    with (
        patch(
            "scheduler._now_et",
            return_value=datetime(2026, 8, 17, 10, 1, tzinfo=_ET),
        ),
        patch("scheduler._market_clock_is_open", return_value=True),
        patch("scheduler._run_cycle", side_effect=cycle),
        patch("scheduler.monitor_exits_hourly", return_value=[]),
        patch("scheduler.monitor_and_exit_positions", return_value=[]),
        patch("scheduler.time.sleep", side_effect=KeyboardInterrupt),
        patch("scheduler._run_startup_stop_reconciliation"),
        patch("scheduler.FillMonitor", return_value=monitor),
    ):
        scheduler.run_scheduler(dry_run=False)

    assert observed == [False]


def test_session_mode_exits_after_monitoring_window_without_sleeping() -> None:
    with (
        patch(
            "scheduler._now_et",
            return_value=datetime(2026, 8, 17, 16, 6, tzinfo=_ET),
        ),
        patch("scheduler.time.sleep") as sleep,
        patch("scheduler._run_cycle") as cycle,
    ):
        scheduler.run_scheduler(dry_run=True, stop_after_session=True)

    sleep.assert_not_called()
    cycle.assert_not_called()


def test_session_completion_detects_date_rollover_and_weekends() -> None:
    session_date = date(2026, 8, 17)

    assert scheduler._session_is_complete(
        datetime(2026, 8, 18, 9, 0, tzinfo=_ET),
        session_date,
    )
    assert scheduler._session_is_complete(
        datetime(2026, 8, 22, 10, 0, tzinfo=_ET),
        date(2026, 8, 22),
    )
    assert not scheduler._session_is_complete(
        datetime(2026, 8, 17, 16, 5, tzinfo=_ET),
        session_date,
    )


def test_after_close_session_exits_before_starting_live_monitor() -> None:
    with (
        patch(
            "scheduler._now_et",
            return_value=datetime(2026, 8, 17, 16, 6, tzinfo=_ET),
        ),
        patch("scheduler.FillMonitor") as monitor,
    ):
        scheduler.run_scheduler(dry_run=False, stop_after_session=True)

    monitor.assert_not_called()


def test_scheduler_cli_defaults_to_dry_run_and_requires_enable_orders() -> None:
    parser = scheduler.build_parser()

    default_args = parser.parse_args([])
    enabled_args = parser.parse_args(["--enable-orders"])

    assert default_args.enable_orders is False
    assert enabled_args.enable_orders is True


def test_scheduler_cli_rejects_fmp_budget_above_free_plan_cap() -> None:
    parser = scheduler.build_parser()
    cap = settings.FMP_FREE_DAILY_REQUEST_BUDGET_CAP

    assert parser.parse_args(["--fmp-daily-budget", str(cap)]).fmp_daily_budget == cap
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--fmp-daily-budget", str(cap + 1)])

    assert exc_info.value.code == 2


def test_cli_treats_an_existing_scheduler_as_a_safe_noop(capsys) -> None:
    with patch(
        "scheduler.run_scheduler",
        side_effect=scheduler.SchedulerAlreadyRunningError("already running"),
    ):
        assert scheduler.main([]) == 0

    assert "already running" in capsys.readouterr().out


def test_task_cli_applies_zero_budget_and_writes_its_log(
    tmp_path,
    monkeypatch,
) -> None:
    log_path = tmp_path / "scheduler.log"
    monkeypatch.setattr(settings, "FMP_DAILY_REQUEST_BUDGET", 99)

    def fake_run_scheduler(**kwargs) -> None:
        print(f"task dry_run={kwargs['dry_run']}")

    with patch("scheduler.run_scheduler", side_effect=fake_run_scheduler):
        rc = scheduler.main(
            [
                "--dry-run",
                "--session",
                "--fmp-daily-budget",
                "0",
                "--task-log",
                str(log_path),
            ]
        )

    assert rc == 0
    assert settings.FMP_DAILY_REQUEST_BUDGET == 0
    assert "task dry_run=True" in log_path.read_text(encoding="utf-8")
