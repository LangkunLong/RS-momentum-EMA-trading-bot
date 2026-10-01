"""High-impact runtime gates for the paper-trading scheduler."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

import scheduler
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation
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


def _observation_args() -> list[str]:
    return ["--dry-run", "--now", "--session", "--observe-health"]


def _receipt_from_output(output: str) -> dict:
    prefix = "SCHEDULER_OBSERVATION_RECEIPT="
    line = next(line for line in output.splitlines() if line.startswith(prefix))
    return json.loads(line[len(prefix) :])


def _set_observation_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALPACA_HTTP_TIMEOUT_SECONDS", 15)
    monkeypatch.setattr(settings, "FMP_HTTP_TIMEOUT_SECONDS", 15)
    monkeypatch.setattr(settings, "INDEX_TICKER_HTTP_TIMEOUT_SECONDS", 15)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_FROM", "")
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_TO", "")
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_PASSWORD", "")


@pytest.mark.parametrize(
    "argv",
    [
        ["--observe-health"],
        ["--observe-health", "--now", "--session"],
        ["--enable-orders", "--now", "--session", "--observe-health"],
    ],
)
def test_observation_cli_requires_explicit_bounded_dry_run(argv: list[str]) -> None:
    with patch("scheduler._market_clock_is_open") as clock:
        assert scheduler.main(argv) == 2

    clock.assert_not_called()


@pytest.mark.parametrize(
    ("setting_name", "bad_value"),
    [
        ("ALPACA_HTTP_TIMEOUT_SECONDS", 14),
        ("FMP_HTTP_TIMEOUT_SECONDS", 14),
        ("INDEX_TICKER_HTTP_TIMEOUT_SECONDS", 14),
        ("ALPACA_SDK_RETRY_ATTEMPTS", 1),
    ],
)
def test_observation_cli_rejects_noncanonical_request_settings(
    setting_name: str,
    bad_value: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, setting_name, bad_value)
    with patch("scheduler.run_scheduler") as run:
        assert scheduler.main(_observation_args()) == 2

    run.assert_not_called()


def test_observation_budget_override_cannot_raise_configured_fmp_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_observation_settings(monkeypatch)
    monkeypatch.setattr(settings, "FMP_DAILY_REQUEST_BUDGET", 7)
    captured: list[int] = []

    def request_limit(_cap: int) -> int:
        captured.append(settings.FMP_DAILY_REQUEST_BUDGET)
        return 0

    with (
        patch("scheduler.fmp_observation_request_limit", side_effect=request_limit),
        patch("scheduler.run_scheduler"),
    ):
        assert scheduler.main(_observation_args() + ["--fmp-daily-budget", "198"]) == 0

    assert captured == [7]


def test_missing_fmp_ledger_stops_before_any_provider_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _set_observation_settings(monkeypatch)
    monkeypatch.setattr(
        settings,
        "FMP_REQUEST_LEDGER_PATH",
        str(tmp_path / "missing-ledger.json"),
    )
    with (
        patch("scheduler._market_clock_is_open") as clock,
        patch("scheduler.run_scheduler") as run,
    ):
        result = scheduler.main(_observation_args())

    assert result == 1
    clock.assert_not_called()
    run.assert_not_called()
    receipt = _receipt_from_output(capsys.readouterr().out)
    assert receipt["issues"][0]["code"] == "fmp_ledger_missing"


def test_observation_clock_preflight_runs_inside_budget_before_scan(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _set_observation_settings(monkeypatch)
    events: list[str] = []
    cycle_done = False
    session_start = datetime(2026, 10, 1, 10, 1, tzinfo=_ET)
    session_end = datetime(2026, 10, 1, 16, 6, tzinfo=_ET)

    def fake_now() -> datetime:
        return session_end if cycle_done else session_start

    def clock() -> bool:
        events.append("clock")
        assert scheduler.alpaca_http_request_snapshot()["cap"] == 256
        return True

    def cycle(*_args, **_kwargs) -> None:
        nonlocal cycle_done
        events.append("cycle")
        cycle_done = True

    monkeypatch.setattr(scheduler, "_now_et", fake_now)
    with (
        patch("scheduler.SchedulerInstanceLock", return_value=MagicMock(__enter__=lambda self: self, __exit__=lambda *args: None)),
        patch("scheduler.require_paper_mode"),
        patch("scheduler._market_clock_is_open", side_effect=clock),
        patch("scheduler._run_cycle", side_effect=cycle),
        patch("scheduler.fmp_observation_request_limit", return_value=12),
    ):
        result = scheduler.main(_observation_args())

    assert result == 1
    assert events[:2] == ["clock", "cycle"]
    receipt = _receipt_from_output(capsys.readouterr().out)
    assert receipt["service_health"] == "unverified"
    assert receipt["overall_readiness"] == "unverified"
    assert any(issue["code"] == "scheduled_work_missed" for issue in receipt["issues"])


def test_unknown_observation_clock_prevents_immediate_scan(capsys, monkeypatch) -> None:
    _set_observation_settings(monkeypatch)
    now = datetime(2026, 10, 1, 10, 1, tzinfo=_ET)
    monkeypatch.setattr(scheduler, "_now_et", lambda: now)
    with (
        patch("scheduler.SchedulerInstanceLock", return_value=MagicMock(__enter__=lambda self: self, __exit__=lambda *args: None)),
        patch("scheduler.require_paper_mode"),
        patch("scheduler._market_clock_is_open", return_value=None),
        patch("scheduler._run_cycle") as cycle,
        patch("scheduler.fmp_observation_request_limit", return_value=12),
    ):
        result = scheduler.main(_observation_args())

    assert result == 1
    cycle.assert_not_called()
    receipt = _receipt_from_output(capsys.readouterr().out)
    assert receipt["service_health"] == "unverified"
    assert receipt["issues"][-1]["code"] == "market_clock_unknown"


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 10, 1, 16, 6, tzinfo=_ET),
        datetime(2026, 10, 3, 10, 0, tzinfo=_ET),
    ],
)
def test_observation_with_no_remaining_session_is_unverified(
    start: datetime,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    _set_observation_settings(monkeypatch)
    monkeypatch.setattr(scheduler, "_now_et", lambda: start)
    with (
        patch("scheduler.SchedulerInstanceLock", return_value=MagicMock(__enter__=lambda self: self, __exit__=lambda *args: None)),
        patch("scheduler.require_paper_mode"),
        patch("scheduler._market_clock_is_open") as clock,
        patch("scheduler._run_cycle") as cycle,
        patch("scheduler.fmp_observation_request_limit", return_value=12),
    ):
        result = scheduler.main(_observation_args())

    assert result == 1
    clock.assert_not_called()
    cycle.assert_not_called()
    receipt = _receipt_from_output(capsys.readouterr().out)
    assert receipt["service_health"] == "unverified"
    assert receipt["issues"][-1]["code"] == "bounded_session_unavailable"


def test_full_large_cap_scan_spanning_exit_slots_records_missed_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys,
) -> None:
    _set_observation_settings(monkeypatch)
    start = datetime(2026, 10, 1, 10, 1, tzinfo=_ET)
    scan_finished = datetime(2026, 10, 1, 15, 5, tzinfo=_ET)
    clock_state = {"now": start}
    universe = [f"S{i:03d}" for i in range(25)]
    extra_symbols = [universe[0], "EXTRA"]
    context_symbols = [f"MKT{i}" for i in range(5)]
    validations: list[list[str]] = []
    context_tickers_requested: set[str] = set()
    market = SimpleNamespace(
        is_bullish=True,
        score=1.0,
        distribution_days=0,
        follow_through=True,
        as_of_session=pd.Timestamp("2026-10-01"),
    )
    dates = pd.bdate_range(end="2026-10-01", periods=280)
    synthetic_bars = pd.DataFrame(
        {
            symbol: pd.Series(range(100, 100 + len(dates)), index=dates, dtype=float)
            for symbol in context_symbols
        },
        index=dates,
    )
    store_path = tmp_path / "scheduler-observation.sqlite3"

    def now_et() -> datetime:
        return clock_state["now"]

    def fetch_synthetic_bars(tickers: list[str], **_kwargs) -> pd.DataFrame:
        clock_state["now"] = scan_finished
        context_tickers_requested.update(tickers)
        return synthetic_bars.copy()

    def record_validation(symbols: list[str], **_kwargs) -> list[str]:
        validations.append(list(symbols))
        return list(symbols)

    next_ticks = iter(
        [
            datetime(2026, 10, 1, 15, 35, tzinfo=_ET),
            datetime(2026, 10, 1, 16, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 16, 5, tzinfo=_ET),
            datetime(2026, 10, 1, 16, 6, tzinfo=_ET),
        ]
    )

    def finish_session(_seconds: float) -> None:
        clock_state["now"] = next(next_ticks)

    def store_snapshot() -> dict[str, tuple[int, str]]:
        table_names = (
            "workflow_snapshots",
            "workflow_transitions",
            "workflow_order_refs",
            "active_positions",
            "workflow_notification_claims",
        )
        result: dict[str, tuple[int, str]] = {}
        with sqlite3.connect(store_path) as connection:
            for table in table_names:
                rows = connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                encoded = json.dumps(rows, separators=(",", ":"), default=str).encode()
                result[table] = (len(rows), hashlib.sha256(encoded).hexdigest())
        return result

    monkeypatch.setattr(scheduler, "_now_et", now_et)
    monkeypatch.setattr(settings, "SECTORS", "large_cap")
    monkeypatch.setattr(settings, "EXTRA_SYMBOLS", extra_symbols)
    monkeypatch.setattr(settings, "RS_CACHE_DIR", str(tmp_path / "rs-cache"))
    monkeypatch.setattr(settings, "RS_CACHE_FILE", "rs.csv")
    monkeypatch.setattr(settings, "EXECUTION_STORE_DB_PATH", str(store_path))
    from core.execution_store import ExecutionStore

    ExecutionStore(str(store_path))
    before_store = store_snapshot()
    with (
        patch(
            "scheduler.SchedulerInstanceLock",
            return_value=MagicMock(__enter__=lambda self: self, __exit__=lambda *args: None),
        ),
        patch("scheduler.require_paper_mode"),
        patch("scheduler._market_clock_is_open", return_value=True),
        patch("scheduler.fmp_observation_request_limit", return_value=12),
        patch("scheduler.time.sleep", side_effect=finish_session),
        patch("enhanced_scanner.get_index_tickers", return_value=universe) as index_fetch,
        patch("enhanced_scanner.validate_tickers_bulk", side_effect=record_validation),
        patch("core.stock_screening.evaluate_market_direction", return_value=market),
        patch("core.momentum_analysis.get_sp500_tickers", return_value=context_symbols),
        patch("core.momentum_analysis.fetch_bulk_close_prices", side_effect=fetch_synthetic_bars) as fetch_bars,
        patch("auto_trader.get_open_positions", return_value=[]),
        patch("scheduler.notify_cycle_summary") as cycle_notification,
        patch("core.notifier.smtplib.SMTP") as smtp,
        patch("core.notifier.send_gmail_email") as gmail,
        patch("requests.sessions.Session.request", side_effect=AssertionError("network is disabled")) as http_request,
    ):
        result = scheduler.main(_observation_args())
    after_store = store_snapshot()

    assert result == 1
    index_fetch.assert_called_once_with(index_name="large_cap")
    expected_universe = universe + ["EXTRA"]
    assert validations == [expected_universe]
    fetch_bars.assert_called_once()
    assert set(expected_universe + context_symbols).issubset(context_tickers_requested)
    cycle_notification.assert_not_called()
    smtp.assert_not_called()
    gmail.assert_not_called()
    http_request.assert_not_called()
    assert before_store == after_store
    receipt = _receipt_from_output(capsys.readouterr().out)
    assert receipt["scan_coverage"]["requested"] == len(expected_universe)
    assert receipt["scan_coverage"]["validated"] == len(expected_universe)
    assert receipt["required_input_coverage"] == "complete"
    assert receipt["overall_readiness"] != "pass"
    work = [event for event in receipt["events"] if event["kind"] == "scheduler_work"]
    assert any(event["key"] == "scan:startup" and event["status"] == "completed" for event in work)
    assert any(event["key"] == "exit_check:scan_phase" and event["status"] == "completed" for event in work)
    assert any(event["key"] == "exit_check:hourly" and event["status"] == "missed" for event in work)
    assert any(
        event["key"] == "exit_check:hourly"
        and event["status"] == "missed"
        and event["details"].get("scheduled_at") == "2026-10-01T10:01:00-04:00"
        for event in work
    )
    assert sum(event["key"] == "exit_check:hourly" and event["status"] == "completed" for event in work) == 2
    assert any(event["key"] == "exit_check:daily_fallback" and event["status"] == "missed" for event in work)
    daily_missed = [
        event
        for event in work
        if event["key"] == "exit_check:daily_fallback" and event["status"] == "missed"
    ]
    assert [event["details"].get("scheduled_at") for event in daily_missed] == [
        "2026-10-01T10:01:00-04:00"
    ]
    assert sum(event["key"] == "exit_check:daily_fallback" and event["status"] == "completed" for event in work) == 3
    due_at = {
        (event["key"], event["details"].get("scheduled_at"))
        for event in work
        if event["status"] == "due"
    }
    assert ("exit_check:hourly", "2026-10-01T15:01:00-04:00") in due_at
    assert ("exit_check:hourly", "2026-10-01T16:01:00-04:00") in due_at
    assert ("exit_check:daily_fallback", "2026-10-01T15:35:00-04:00") in due_at
    assert ("exit_check:daily_fallback", "2026-10-01T16:05:00-04:00") in due_at
    assert any(issue["code"] == "scheduled_work_missed" for issue in receipt["issues"])
    assert receipt["service_health"] == "unverified"
    assert receipt["resource_counters"]["workflow_transition_writes"] == 0
    assert receipt["resource_counters"]["workflow_snapshot_writes"] == 0


def test_observed_scheduler_suppresses_hourly_exit_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_observation_settings(monkeypatch)
    times = iter(
        [
            datetime(2026, 10, 1, 10, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 10, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 10, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 10, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 10, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 15, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 15, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 15, 1, tzinfo=_ET),
            datetime(2026, 10, 1, 16, 6, tzinfo=_ET),
        ]
    )
    monkeypatch.setattr(scheduler, "_now_et", lambda: next(times))
    with (
        patch("scheduler.SchedulerInstanceLock", return_value=MagicMock(__enter__=lambda self: self, __exit__=lambda *args: None)),
        patch("scheduler.require_paper_mode"),
        patch("scheduler._market_clock_is_open", return_value=True),
        patch("scheduler._run_cycle"),
        patch("scheduler.monitor_exits_hourly", return_value=["AAPL"]),
        patch("scheduler.monitor_and_exit_positions", return_value=[]),
        patch("scheduler.notify_cycle_summary") as notify,
        patch("scheduler.fmp_observation_request_limit", return_value=12),
        patch("scheduler.time.sleep"),
    ):
        assert scheduler.main(_observation_args()) == 1

    notify.assert_not_called()


def test_run_cycle_suppresses_summary_even_with_parent_notification_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_FROM", "sender@example.com")
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_TO", "recipient@example.com")
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_PASSWORD", "parent-secret")
    observation = SchedulerObservation("notification-suppression")
    result = SimpleNamespace(entered=[], exited=[])

    with (
        activate_scheduler_observation(observation),
        patch("scheduler.run_auto_trader", return_value=result),
        patch("scheduler.notify_cycle_summary") as notify,
    ):
        scheduler._run_cycle(dry_run=True)

    notify.assert_not_called()
    assert observation.to_receipt()["events"][-1]["status"] == "suppressed_observation"


def test_observed_work_latches_a_caught_expected_work_failure() -> None:
    observation = SchedulerObservation("work-failure")
    scheduled_at = datetime(2026, 10, 1, 9, 31, tzinfo=_ET)
    with activate_scheduler_observation(observation), pytest.raises(RuntimeError, match="scan failed"):
        scheduler._run_observed_work(
            "scan",
            "startup",
            scheduled_at,
            lambda: (_ for _ in ()).throw(RuntimeError("scan failed")),
        )

    receipt = observation.to_receipt()
    assert receipt["service_health"] == "failed"
    assert any(
        event["kind"] == "scheduler_work" and event["status"] == "failed"
        for event in receipt["events"]
    )
