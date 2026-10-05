"""Daily market-hours scheduler for the CANSLIM auto-trader.

Runs as a long-lived process.  Each weekday:

  09:31 ET  — Full CANSLIM scan + bracket buy entries (once per day)
  Each hour (10:01, 11:01, …, 16:01 ET)
            — Hourly exit check: 21-period hourly EMA + stop-loss vs live P&L
  Every 30 min (09:30–16:05 ET)
            — Daily exit check (fallback): 21-day EMA + Alpaca unrealised P&L
  All day   — Fill-monitor WebSocket in background daemon thread

Usage:
    python scheduler.py                         # dry run (safe default)
    python scheduler.py --enable-orders         # submit Alpaca paper orders
    python scheduler.py --now                    # dry-run scan immediately
    python scheduler.py --enable-orders --now    # order-enabled scan immediately

Hourly monitoring catches MA violations and stop-loss breaches faster than
the daily check, because a 21-period hourly EMA tracks roughly 2.6 trading
days of intraday structure instead of 21 calendar days of daily closes.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import traceback
from uuid import uuid4
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Iterable
from zoneinfo import ZoneInfo

from config import settings
from auto_trader import (
    ExecutionReadinessCheck,
    monitor_and_exit_positions,
    monitor_exits_hourly,
    run_auto_trader,
)
from core.notifier import notify_cycle_summary
from core.alpaca_client_policy import (
    alpaca_http_request_budget,
    alpaca_http_request_snapshot,
    single_attempt_alpaca_requests,
)
from core.data_client import (
    fmp_observation_request_limit,
    fmp_request_budget,
    fmp_request_budget_snapshot,
)
from core.order_execution import _get_trading_client, _is_paper_mode, require_paper_mode
from core.order_manager import OrderManager
from core.bounded_trading_stream import ReadOnlyTradeUpdateObserver
from core.operation_limits import (
    OperationBudget,
    OperationCapExceeded,
    OperationManifestError,
    activate_operation_budget,
    budgeted_standard_output,
    consume_operation_dimension,
    current_operation_budget,
    enforce_manifest_session_window,
    load_operation_manifest,
    validate_runtime_binding,
)
from core.scheduler_observation import (
    SchedulerObservation,
    activate_scheduler_observation,
    current_scheduler_observation,
)
from fill_monitor import FillMonitor

_ET = ZoneInfo("America/New_York")

# Market session window (ET)
_MARKET_OPEN = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)
_SCAN_TIME = dtime(9, 31)           # Run full scan 1 min after open

# Extend the monitoring window 5 minutes past close so the 15:00-16:00 hourly
# bar (the last bar of the regular session) is always evaluated at 16:01.
_EXIT_MONITOR_CLOSE = dtime(16, 5)

# Hourly bar close times: we run the check at :01 past each full hour so the
# bar is fully closed.  Range 10–16 covers 10:01 through 16:01 ET.
_HOURLY_CHECK_HOURS = frozenset(range(10, 17))  # 10 through 16 inclusive

_DAILY_EXIT_INTERVAL_SECS = 30 * 60    # Secondary safety-net: every 30 minutes
_LOOP_SLEEP_SECS = 30                   # Main loop tick
_QUIET_LOG_INTERVAL_SECS = 60 * 60     # Log "waiting" at most once per hour when closed
_MONITOR_CONNECT_TIMEOUT_SECS = 15.0
_MONITOR_CONNECT_POLL_SECS = 0.1
_SCHEDULER_LOCK_PATH = Path(tempfile.gettempdir()) / "canslim-paper-scheduler.lock"


def _run_observed_work(
    kind: str,
    key: str,
    scheduled_at: datetime,
    callback,
    *,
    missed_due_keys: set[tuple[str, str, str]] | None = None,
):
    """Record one due scheduler action while preserving its ordinary callback."""
    observation = current_scheduler_observation()
    if observation is None:
        return callback()
    observation.record_event(
        "scheduler_work",
        f"{kind}:{key}",
        "due",
        {"scheduled_at": scheduled_at.isoformat()},
    )
    started_at = _now_et()
    observation.begin_work(kind, key, scheduled_at)
    if (started_at - scheduled_at).total_seconds() > _LOOP_SLEEP_SECS:
        _record_missed_observed_work(
            observation,
            kind,
            key,
            scheduled_at,
            started_at,
            missed_due_keys,
        )
    if not observation.try_admit_work(kind, key, _now_et):
        return None
    try:
        result = callback()
    except Exception as exc:
        observation.finish_work(kind, key)
        details = {"error_type": type(exc).__name__}
        observation.record_event(
            "scheduler_work", f"{kind}:{key}", "failed", details
        )
        observation.latch_service_issue(f"{kind}_work_failed", details)
        raise
    observation.finish_work(kind, key)
    observation.record_event(
        "scheduler_work",
        f"{kind}:{key}",
        "completed",
        {},
    )
    return result


def _record_missed_observed_work(
    observation: SchedulerObservation,
    kind: str,
    key: str,
    scheduled_at: datetime,
    observed_at: datetime,
    missed_due_keys: set[tuple[str, str, str]] | None = None,
) -> None:
    """Latch one due scheduler slot that passed beyond the loop tolerance."""
    identity = (kind, key, scheduled_at.isoformat())
    if missed_due_keys is not None:
        if identity in missed_due_keys:
            return
        missed_due_keys.add(identity)
    details = {
        "scheduled_at": scheduled_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "late_seconds": max(0, int((observed_at - scheduled_at).total_seconds())),
    }
    observation.record_event(
        "scheduler_work", f"{kind}:{key}", "missed", details
    )
    observation.latch_service_issue(
        "scheduled_work_missed", {"kind": kind, "key": key, **details}, unverified=True
    )


def _observed_hourly_due_at(now: datetime, session_started_at: datetime) -> datetime:
    """Use session start for a current-hour catch-up whose nominal due predates observation."""
    due = now.replace(minute=1, second=0, microsecond=0)
    return max(due, session_started_at)


def _reconcile_missed_observed_work(
    observation: SchedulerObservation,
    now: datetime,
    session_started_at: datetime,
    attempted_hourly_due: set[datetime],
    last_daily_exit: datetime,
    missed_due_keys: set[tuple[str, str, str]],
    observation_end_at: datetime | None = None,
) -> None:
    """Record hourly and daily slots that elapsed during long scheduler work."""
    cutoff = now - timedelta(seconds=_LOOP_SLEEP_SECS)
    if observation_end_at is not None:
        cutoff = min(cutoff, observation_end_at)
    if now.date() == session_started_at.date():
        for hour in sorted(_HOURLY_CHECK_HOURS):
            due = now.replace(hour=hour, minute=1, second=0, microsecond=0)
            if session_started_at <= due <= cutoff and due not in attempted_hourly_due:
                _record_missed_observed_work(
                    observation,
                    "exit_check",
                    "hourly",
                    due,
                    now,
                    missed_due_keys,
                )

    due = (
        session_started_at
        if last_daily_exit.year == 1
        else last_daily_exit + timedelta(seconds=_DAILY_EXIT_INTERVAL_SECS)
    )
    while due <= cutoff:
        _record_missed_observed_work(
            observation,
            "exit_check",
            "daily_fallback",
            due,
            now,
            missed_due_keys,
        )
        due += timedelta(seconds=_DAILY_EXIT_INTERVAL_SECS)


def _record_observation_resource_snapshots(observation: SchedulerObservation) -> None:
    observation.record_event(
        "resource_snapshot", "alpaca", "captured", alpaca_http_request_snapshot()
    )
    observation.record_event(
        "resource_snapshot", "fmp", "captured", fmp_request_budget_snapshot()
    )


def _observer_exit_code(
    observation: SchedulerObservation,
    budget: OperationBudget,
) -> int:
    """Fail on any incomplete budget, even when the final receipt cannot print."""
    budget_state = budget.snapshot()
    if str(budget_state["incomplete_reason"]).startswith("output_bytes:"):
        existing = observation.to_receipt()["issues"]
        if not any(issue.get("code") == "operation_output_cap_denied" for issue in existing):
            observation.latch_service_issue(
                "operation_output_cap_denied",
                {"reason": budget_state["incomplete_reason"]},
            )
    receipt = observation.to_receipt()
    budget_state = budget.snapshot()
    return (
        1
        if not budget_state["evidence_complete"]
        or receipt["service_health"] != "healthy"
        or receipt["overall_readiness"] == "fail"
        else 0
    )


class SchedulerAlreadyRunningError(RuntimeError):
    """Raised when another scheduler owns the host-wide paper-trading lock."""


class SchedulerInstanceLock:
    """Cross-process lock that is released automatically when the process exits."""

    def __init__(self, path: Path = _SCHEDULER_LOCK_PATH) -> None:
        self._path = Path(path)
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "SchedulerInstanceLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SchedulerAlreadyRunningError(
                f"CANSLIM scheduler is already running (lock: {self._path})"
            ) from exc

        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _now_et() -> datetime:
    return datetime.now(tz=_ET)


def _is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon=0 … Fri=4


def _is_market_hours(dt: datetime) -> bool:
    """True during the normal trading session (09:30–16:00 ET, weekdays)."""
    return _is_weekday(dt) and _MARKET_OPEN <= dt.time() <= _MARKET_CLOSE


def _is_exit_monitor_window(dt: datetime) -> bool:
    """True during 09:30–16:05 ET — includes 5-min grace period for last hourly bar."""
    return _is_weekday(dt) and _MARKET_OPEN <= dt.time() <= _EXIT_MONITOR_CLOSE


def _session_is_complete(now: datetime, session_date: date) -> bool:
    """Return True once a bounded weekday session must no longer run."""
    return (
        now.date() != session_date
        or not _is_weekday(now)
        or now.time() > _EXIT_MONITOR_CLOSE
    )


def _market_clock_is_open() -> bool | None:
    """Return Alpaca's clock state when available, else None.

    ``None`` means the market clock could not be queried, so the scheduler
    should fall back to its local time-based heuristics instead of assuming
    the market is closed.
    """
    try:
        client = _get_trading_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception:
        return None


def _validate_observer_run_admission(
    observation_stop_at: datetime,
    observation_hard_deadline_at: datetime,
) -> None:
    """Repeat observer admission at the public Python entry before provider I/O."""
    budget = current_operation_budget()
    if budget is None:
        raise OperationManifestError(
            "observe_health requires an active admitted observer operation budget"
        )
    manifest = budget.manifest
    manifest.require_admitted()
    if manifest.operation != "observer":
        raise OperationManifestError("observe_health requires an observer manifest")
    observation = current_scheduler_observation()
    if observation is None:
        raise OperationManifestError(
            "observe_health requires an active scheduler observation context"
        )
    enforce_manifest_session_window(manifest, now=datetime.now(_ET))
    bound_stop = datetime.fromisoformat(
        str(manifest.binding["session_stop"])
    ).astimezone(_ET)
    bound_start = datetime.fromisoformat(
        str(manifest.binding["session_start"])
    ).astimezone(_ET)
    expected_hard_deadline = bound_stop + timedelta(
        seconds=manifest.deadlines_seconds["hard_deadline_grace_seconds"]
    )
    if (
        observation_stop_at != bound_stop
        or observation_hard_deadline_at != expected_hard_deadline
    ):
        raise OperationManifestError(
            "scheduler deadlines do not match the admitted observer manifest"
        )
    if (bound_stop - bound_start).total_seconds() > manifest.deadlines_seconds[
        "session_seconds"
    ]:
        raise OperationManifestError(
            "manifest session_seconds cannot cover the bound session window"
        )
    budget.ensure_complete()
    validate_runtime_binding(manifest, Path(__file__).resolve().parent)
    settings.load_runtime_credentials()

    account = _get_trading_client().get_account()
    account_id = str(getattr(account, "id", "") or "").strip()
    if not account_id or account_id != str(manifest.binding["account_id"]):
        observation.latch_service_issue(
            "observer_account_binding_mismatch", {}
        )
        raise OperationManifestError(
            "connected account does not match the admitted observer account"
        )


def run_scheduler(
    dry_run: bool = True,
    run_now: bool = False,
    stop_after_session: bool = False,
    observe_health: bool = False,
    observation_stop_at: datetime | None = None,
    observation_hard_deadline_at: datetime | None = None,
) -> None:
    """Start the daily trading loop.

    Args:
        dry_run: When True, all order functions print their intent but submit nothing.
        run_now: When True, run the full scan immediately at startup instead of
            waiting for 09:31 ET.  Useful for manual testing during market hours.
        stop_after_session: Exit after the 16:05 ET monitoring window. Intended
            for one weekday invocation from Windows Task Scheduler.
    """
    require_paper_mode()
    if observe_health and (not dry_run or not run_now or not stop_after_session):
        raise ValueError(
            "Health observation requires dry-run, --now, and --session"
        )
    if observe_health and (
        observation_stop_at is None or observation_hard_deadline_at is None
    ):
        raise ValueError(
            "Health observation requires an explicit stop time and hard deadline"
        )
    if observe_health:
        assert observation_stop_at is not None
        assert observation_hard_deadline_at is not None
        _validate_observer_run_admission(
            observation_stop_at,
            observation_hard_deadline_at,
        )
    if not observe_health and (
        observation_stop_at is not None or observation_hard_deadline_at is not None
    ):
        raise ValueError("Observation deadlines are only valid with --observe-health")
    with SchedulerInstanceLock():
        _run_scheduler_locked(
            dry_run=dry_run,
            run_now=run_now,
            stop_after_session=stop_after_session,
            observe_health=observe_health,
            observation_stop_at=observation_stop_at,
            observation_hard_deadline_at=observation_hard_deadline_at,
        )


def _run_scheduler_locked(
    *,
    dry_run: bool,
    run_now: bool,
    stop_after_session: bool,
    observe_health: bool = False,
    observation_stop_at: datetime | None = None,
    observation_hard_deadline_at: datetime | None = None,
) -> None:
    """Run one scheduler process after the singleton has been acquired."""
    mode = "DRY RUN" if dry_run else "paper"
    print(f"[SCHEDULER] Starting CANSLIM scheduler [{mode}]")
    print("[SCHEDULER] Press Ctrl-C to stop.")

    monitor: FillMonitor | ReadOnlyTradeUpdateObserver | None = None

    def live_execution_ready() -> bool:
        """Read monitor health at the instant an order may submit."""
        return monitor is not None and monitor.is_connected()

    try:
        session_date: date | None = None
        observation_session_started_at: datetime | None = None
        if stop_after_session:
            started_at = _now_et()
            session_date = started_at.date()
            if observe_health:
                observation_session_started_at = started_at
                observation = current_scheduler_observation()
                if observation is None:
                    raise RuntimeError("Health observation context is not active")
                assert observation_stop_at is not None
                assert observation_hard_deadline_at is not None
                observation.configure_execution_window(
                    requested_stop_at=observation_stop_at,
                    hard_deadline_at=observation_hard_deadline_at,
                )
            unavailable = (
                _session_is_complete(started_at, session_date)
                if not observe_health
                else (
                    not _is_weekday(started_at)
                    or started_at.date() != observation_stop_at.date()
                    or started_at >= observation_stop_at
                    or started_at >= observation_hard_deadline_at
                )
            )
            if unavailable:
                print(
                    f"[SCHEDULER] {started_at.strftime('%Y-%m-%d %H:%M ET')} — "
                    "no bounded weekday session remains to run."
                )
                if observe_health:
                    observation = current_scheduler_observation()
                    if observation is not None:
                        observation.record_event(
                            "scheduler",
                            "bounded_session",
                            "unavailable",
                            {"started_at": started_at.isoformat()},
                        )
                        observation.latch_service_issue(
                            "bounded_session_unavailable",
                            {"started_at": started_at.isoformat()},
                            unverified=True,
                        )
                return

        if observe_health:
            observation = current_scheduler_observation()
            if observation is None:
                raise RuntimeError("Health observation context is not active")
            observation.record_scheduler_started(_now_et())
            clock_open = _market_clock_is_open()
            status = "open" if clock_open is True else "closed" if clock_open is False else "unknown"
            observation.record_event("market_clock", "startup_preflight", status, {})
            if clock_open is not True:
                unknown = clock_open is None
                code = "market_clock_unknown" if unknown else "market_clock_closed"
                observation.latch_service_issue(code, {}, unverified=unknown)
                raise RuntimeError(
                    "Observation requires an authoritative open Alpaca clock"
                )

        if observe_health:
            budget = current_operation_budget()
            if budget is None or budget.manifest.operation != "observer":
                raise RuntimeError("Observer operation budget is not active")
            monitor = ReadOnlyTradeUpdateObserver(budget)
            monitor.start()
            _wait_for_fill_monitor_connection(
                monitor,
                timeout_seconds=budget.manifest.deadlines_seconds[
                    "stream_connect_seconds"
                ],
            )
            observation = current_scheduler_observation()
            if observation is not None:
                observation.record_event(
                    "trade_update_stream", "observer", "connected", {}
                )
        elif dry_run:
            print("[SCHEDULER] Fill monitor disabled in dry-run mode.")
        else:
            monitor = _start_live_monitor()

        last_scan_date: date | None = None
        last_daily_exit: datetime = datetime.min.replace(tzinfo=_ET)
        last_hourly_exit_hour: int = -1
        last_quiet_log: datetime = datetime.min.replace(tzinfo=_ET)
        session_seen_open_date: date | None = None
        attempted_hourly_due: set[datetime] = set()
        missed_due_keys: set[tuple[str, str, str]] = set()

        # Optional immediate scan at startup.
        if run_now:
            immediate_scan_at = _now_et()
            if (
                observe_health
                and observation_stop_at is not None
                and immediate_scan_at >= observation_stop_at
            ):
                observation = current_scheduler_observation()
                if observation is not None:
                    observation.request_stop(immediate_scan_at)
                    observation.latch_service_issue(
                        "observation_window_closed_before_scan",
                        {"actual_at": immediate_scan_at.isoformat()},
                        unverified=True,
                    )
                return
            if not dry_run and _market_clock_is_open() is not True:
                raise RuntimeError(
                    "Order-enabled --now requires an authoritative open Alpaca clock"
                )
            print(
                f"\n[SCHEDULER] --now flag: running scan immediately at "
                f"{_now_et().strftime('%H:%M ET')}"
            )
            try:
                if dry_run:
                    if observe_health:
                        _run_observed_work(
                            "scan",
                            "startup",
                            _now_et(),
                            lambda: _run_observer_scan(
                                list(current_operation_budget().manifest.scope["symbols"])
                            ),
                        )
                    else:
                        _run_cycle(dry_run)
                else:
                    _run_cycle(
                        dry_run,
                        execution_ready=live_execution_ready,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[SCHEDULER ERROR] Immediate scan failed: {exc}")
            finally:
                # Always mark today as scanned so we don't retry on the next tick.
                last_scan_date = _now_et().date()

        while True:
            if observe_health:
                budget = current_operation_budget()
                if budget is None:
                    raise RuntimeError("Observer operation budget is not active")
                budget.ensure_complete()
                consume_operation_dimension("scheduler_ticks")
            execution_armed = dry_run
            if monitor is not None:
                if observe_health:
                    if not monitor.is_connected():
                        observation = current_scheduler_observation()
                        if observation is not None:
                            observation.latch_service_issue(
                                "observer_trade_update_stream_disconnected",
                                {},
                                unverified=True,
                            )
                        raise RuntimeError("Read-only trade-update observer disconnected")
                else:
                    try:
                        monitor = _ensure_fill_monitor_running(monitor, dry_run=False)
                        execution_armed = True
                    except Exception as exc:  # noqa: BLE001
                        execution_armed = False
                        print(f"[SCHEDULER ERROR] Live execution disarmed: {exc}")

            now = _now_et()
            today = now.date()

            observation = current_scheduler_observation() if observe_health else None
            if observation is not None and observation_session_started_at is not None:
                _reconcile_missed_observed_work(
                    observation,
                    now,
                    observation_session_started_at,
                    attempted_hourly_due,
                    last_daily_exit,
                    missed_due_keys,
                    observation_stop_at,
                )

            if (
                observation is not None
                and observation_stop_at is not None
                and now >= observation_stop_at
            ):
                observation.request_stop(now)
                break

            if session_date is not None and _session_is_complete(now, session_date):
                print(
                    f"[SCHEDULER] {now.strftime('%Y-%m-%d %H:%M ET')} — "
                    "weekday session complete."
                )
                break

            if session_seen_open_date != today:
                session_seen_open_date = None

            in_market_hours = _is_market_hours(now)
            in_exit_window = _is_exit_monitor_window(now)
            market_clock_open = _market_clock_is_open() if (in_market_hours or in_exit_window) else None

            if observe_health and (in_market_hours or in_exit_window):
                observation = current_scheduler_observation()
                if observation is not None:
                    clock_status = (
                        "open"
                        if market_clock_open is True
                        else "closed"
                        if market_clock_open is False
                        else "unknown"
                    )
                    observation.record_event(
                        "market_clock", "scheduler_tick", clock_status, {}
                    )
                    if market_clock_open is None:
                        observation.latch_service_issue(
                            "market_clock_unknown", {}, unverified=True
                        )

            if market_clock_open and in_market_hours:
                session_seen_open_date = today

            if observe_health:
                market_session_live = in_market_hours and market_clock_open is True
            elif dry_run:
                market_session_live = in_market_hours and (
                    market_clock_open if market_clock_open is not None else True
                )
            else:
                market_session_live = (
                    execution_armed and in_market_hours and market_clock_open is True
                )

            exit_session_live = False
            if not observe_health and in_exit_window and (dry_run or execution_armed):
                if market_clock_open is None:
                    exit_session_live = dry_run and not observe_health
                elif in_market_hours:
                    exit_session_live = market_clock_open
                else:
                    exit_session_live = session_seen_open_date == today or last_scan_date == today

            if market_session_live:
                if observe_health:
                    now = _now_et()
                    today = now.date()
                    if observation_stop_at is not None and now >= observation_stop_at:
                        observation = current_scheduler_observation()
                        if observation is not None:
                            observation.request_stop(now)
                        continue
                    market_session_live = _is_market_hours(now) and market_clock_open is True
                current_hour = now.hour

                # ── Daily scan at 09:31 ────────────────────────────────────────
                # Always update last_scan_date after the attempt (success or
                # early-return) so the scheduler does not retry every 30s when
                # run_auto_trader exits early due to the Alpaca market clock.
                if market_session_live and now.time() >= _SCAN_TIME and last_scan_date != today:
                    print(f"\n[SCHEDULER] {now.strftime('%H:%M ET')} — Running daily scan + entries")
                    last_scan_date = today   # set BEFORE the call to prevent retry loops
                    try:
                        if dry_run:
                            if observe_health:
                                _run_observed_work(
                                    "scan",
                                    "scheduled",
                                    now.replace(hour=9, minute=31, second=0, microsecond=0),
                                    lambda: _run_observer_scan(
                                        list(current_operation_budget().manifest.scope["symbols"])
                                    ),
                                )
                            else:
                                _run_cycle(dry_run)
                        else:
                            _run_cycle(
                                dry_run,
                                execution_ready=live_execution_ready,
                            )
                    except Exception as exc:  # noqa: BLE001
                        print(f"[SCHEDULER ERROR] Daily scan failed: {exc}")

            # ── Hourly exit check (09:30–16:05 window) ────────────────────────
            # Runs once per clock hour at :01 past.  The extended window to
            # 16:05 ensures the 15:00–16:00 bar is always evaluated at 16:01.
            if exit_session_live:
                if observe_health:
                    now = _now_et()
                    today = now.date()
                    if observation_stop_at is not None and now >= observation_stop_at:
                        observation = current_scheduler_observation()
                        if observation is not None:
                            observation.request_stop(now)
                        continue
                    current_hour = now.hour
                current_hour = now.hour
                if (
                    current_hour in _HOURLY_CHECK_HOURS
                    and now.minute >= 1
                    and current_hour != last_hourly_exit_hour
                ):
                    print(f"\n[SCHEDULER] {now.strftime('%H:%M ET')} — Hourly exit check")
                    if observe_health:
                        # Claim the due slot before work starts so a failed
                        # callback is reported once instead of retried every tick.
                        last_hourly_exit_hour = current_hour
                    try:
                        def run_hourly_check():
                            if dry_run:
                                return monitor_exits_hourly(dry_run=dry_run)
                            return monitor_exits_hourly(
                                dry_run=dry_run,
                                execution_ready=live_execution_ready,
                            )

                        if observe_health:
                            scheduled_at = _observed_hourly_due_at(
                                now, observation_session_started_at or now
                            )
                            attempted_hourly_due.add(scheduled_at)
                            exited = _run_observed_work(
                                "exit_check",
                                "hourly",
                                scheduled_at,
                                run_hourly_check,
                                missed_due_keys=missed_due_keys,
                            )
                        else:
                            exited = run_hourly_check()
                        if exited:
                            print(f"[SCHEDULER] Hourly exits: {', '.join(exited)}")
                            observation = current_scheduler_observation()
                            if observation is None:
                                notify_cycle_summary(
                                    entered=[], exited=exited, paper=_is_paper_mode()
                                )
                            else:
                                observation.record_event(
                                    "notification", "cycle_summary", "suppressed_observation", {}
                                )
                        last_hourly_exit_hour = current_hour
                    except Exception as exc:  # noqa: BLE001
                        print(f"[SCHEDULER ERROR] Hourly exit check failed: {exc}")

                # ── 30-min daily fallback exit check ──────────────────────────
                elapsed = (now - last_daily_exit).total_seconds()
                if elapsed >= _DAILY_EXIT_INTERVAL_SECS:
                    if observe_health:
                        now = _now_et()
                        if observation_stop_at is not None and now >= observation_stop_at:
                            observation = current_scheduler_observation()
                            if observation is not None:
                                observation.request_stop(now)
                            continue
                        elapsed = (now - last_daily_exit).total_seconds()
                    if elapsed < _DAILY_EXIT_INTERVAL_SECS:
                        continue
                    if observe_health:
                        due_at = (
                            observation_session_started_at or now
                            if last_daily_exit.year == 1
                            else last_daily_exit
                            + timedelta(seconds=_DAILY_EXIT_INTERVAL_SECS)
                        )
                        # Claim the fallback slot before work starts. A failed
                        # observation must not retry its broker reads next tick.
                        last_daily_exit = now
                    try:
                        def run_daily_check():
                            if dry_run:
                                return monitor_and_exit_positions(dry_run=dry_run)
                            return monitor_and_exit_positions(
                                dry_run=dry_run,
                                execution_ready=live_execution_ready,
                            )

                        if observe_health:
                            exited = _run_observed_work(
                                "exit_check",
                                "daily_fallback",
                                due_at,
                                run_daily_check,
                                missed_due_keys=missed_due_keys,
                            )
                        else:
                            exited = run_daily_check()
                        if exited:
                            print(f"[SCHEDULER] Daily fallback exits: {', '.join(exited)}")
                        if not observe_health:
                            last_daily_exit = now
                    except Exception as exc:  # noqa: BLE001
                        print(f"[SCHEDULER ERROR] Daily fallback exit failed: {exc}")

            elif not market_session_live:
                # Log "waiting" at most once per hour when market is fully closed
                elapsed_quiet = (now - last_quiet_log).total_seconds()
                if elapsed_quiet >= _QUIET_LOG_INTERVAL_SECS:
                    day_name = now.strftime("%A")
                    print(
                        f"[SCHEDULER] {now.strftime('%Y-%m-%d %H:%M ET')} — "
                        f"market closed ({day_name}), waiting…"
                    )
                    last_quiet_log = now
                # Reset hourly counter at end of session so it fires fresh next day
                if now.time() > _EXIT_MONITOR_CLOSE:
                    last_hourly_exit_hour = -1

            if observe_health and observation_stop_at is not None:
                seconds_to_stop = (observation_stop_at - _now_et()).total_seconds()
                if seconds_to_stop <= 0:
                    continue
                time.sleep(min(_LOOP_SLEEP_SECS, seconds_to_stop))
            else:
                time.sleep(_LOOP_SLEEP_SECS)

    except KeyboardInterrupt:
        if observe_health:
            observation = current_scheduler_observation()
            if observation is not None:
                observation.mark_interrupted(_now_et())
        print("\n[SCHEDULER] Shutdown requested.")
    finally:
        if monitor is not None:
            stopped = monitor.stop()
            if not stopped and observe_health:
                observation = current_scheduler_observation()
                if observation is not None:
                    observation.latch_service_issue(
                        "observer_trade_update_stream_stop_incomplete",
                        {},
                        unverified=True,
                    )
            print("[SCHEDULER] Fill monitor stopped. Goodbye.")
        else:
            print("[SCHEDULER] Dry-run scheduler stopped. Goodbye.")


def _wait_for_fill_monitor_connection(
    monitor: FillMonitor | ReadOnlyTradeUpdateObserver,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float = _MONITOR_CONNECT_POLL_SECS,
) -> None:
    """Wait for authenticated stream readiness or fail closed."""
    timeout = _MONITOR_CONNECT_TIMEOUT_SECS if timeout_seconds is None else timeout_seconds
    budget = current_operation_budget()
    if budget is not None:
        timeout = min(
            timeout,
            budget.manifest.deadlines_seconds["stream_connect_seconds"],
        )
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if monitor.is_connected():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Fill monitor did not become connected")
        time.sleep(max(0.0, poll_seconds))


def _start_live_monitor() -> FillMonitor:
    """Start a healthy monitor and reconcile broker safety before returning."""
    monitor = FillMonitor()
    monitor.start()
    try:
        _wait_for_fill_monitor_connection(monitor)
        _run_startup_stop_reconciliation()
    except BaseException:
        monitor.stop()
        raise
    return monitor


def _ensure_fill_monitor_running(monitor: FillMonitor, *, dry_run: bool) -> FillMonitor:
    """Return a connected monitor, or replace and reconcile it before use."""
    if dry_run or monitor.is_connected():
        return monitor

    print("[SCHEDULER] Fill monitor is unhealthy. Restarting trade-update stream.")
    if not monitor.stop():
        raise RuntimeError(
            "Unhealthy fill monitor did not terminate; refusing replacement"
        )

    replacement = FillMonitor()
    replacement.start()
    try:
        _wait_for_fill_monitor_connection(replacement)
        _run_startup_stop_reconciliation()
    except BaseException:
        replacement.stop()
        raise

    return replacement


def _run_startup_stop_reconciliation() -> None:
    """Repair missing protective stops for existing positions at process startup."""
    try:
        results = OrderManager(paper=_is_paper_mode()).reconcile_startup_stops()
    except Exception as exc:  # noqa: BLE001
        print(f"[SCHEDULER ERROR] Startup stop reconciliation failed: {exc}")
        raise RuntimeError("Startup stop reconciliation failed") from exc

    if not results:
        print("[SCHEDULER] Startup stop reconciliation: no open positions.")
        return

    repaired = [result.symbol for result in results if result.action in {"submitted", "replaced"} and result.success]
    reused = [result.symbol for result in results if result.action in {"reused", "cleaned"} and result.success]
    skipped = [result.symbol for result in results if result.action == "skipped_pending_exit"]
    failed = [result.symbol for result in results if not result.success]

    print(
        "[SCHEDULER] Startup stop reconciliation complete: "
        f"repaired={len(repaired)} reused={len(reused)} "
        f"skipped={len(skipped)} failed={len(failed)}"
    )
    if repaired:
        print(f"[SCHEDULER] Repaired stops: {', '.join(repaired)}")
    if skipped:
        print(f"[SCHEDULER] Skipped pending exits: {', '.join(skipped)}")
    if failed:
        print(f"[SCHEDULER] Failed stop repairs: {', '.join(failed)}")
        raise RuntimeError(
            "Startup stop reconciliation left safety unproven for: "
            + ", ".join(failed)
        )


def _run_cycle(
    dry_run: bool,
    *,
    execution_ready: ExecutionReadinessCheck | None = None,
) -> None:
    """Run the full auto-trader cycle and send a cycle summary email.

    Wraps run_auto_trader() and fires notify_cycle_summary() so the user
    gets a daily email summary of what was entered/exited.  The auto-trader
    returns the exact symbols acted on so reporting stays consistent with the
    execution cycle.
    """
    # run_auto_trader handles its own market-clock guard and prints everything.
    # The cycle summary email is best-effort — notification failure must not
    # prevent the trading cycle from completing.
    if execution_ready is None:
        result = run_auto_trader(dry_run=dry_run)
    else:
        result = run_auto_trader(
            dry_run=dry_run,
            execution_ready=execution_ready,
        )

    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_event(
            "notification", "cycle_summary", "suppressed_observation", {}
        )
        return

    # Send a lightweight "cycle ran" notification.  Full per-fill notifications
    # come from FillMonitor when orders are actually filled by Alpaca.
    try:
        notify_cycle_summary(
            entered=result.entered,
            exited=result.exited,
            paper=_is_paper_mode(),
        )
    except Exception:  # noqa: BLE001
        pass  # notification failure is non-fatal


def _run_observer_scan(symbols: list[str]) -> None:
    """Scan only the manifest's explicit symbols, with both trade phases off."""
    for symbol in symbols:
        consume_operation_dimension("scan_cycles")
        run_auto_trader(
            dry_run=True,
            skip_entries=True,
            skip_exits=True,
            symbol=symbol,
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the safe-by-default scheduler CLI."""
    parser = argparse.ArgumentParser(description="CANSLIM Daily Scheduler")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended orders without submitting (the default)",
    )
    mode.add_argument(
        "--enable-orders",
        action="store_true",
        help="Explicitly enable Alpaca paper-order submission",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        default=False,
        help="Run the full scan immediately at startup, then follow the normal schedule",
    )
    parser.add_argument(
        "--session",
        action="store_true",
        default=False,
        help="Exit after the 16:05 ET monitoring window",
    )
    parser.add_argument(
        "--observe-health",
        action="store_true",
        default=False,
        help="Record bounded, offline-reviewable outcomes for one dry-run session",
    )
    parser.add_argument(
        "--operation-manifest",
        default="",
        help="path to the separately admitted observer limits manifest",
    )
    parser.add_argument(
        "--observe-stop-at",
        type=_observation_datetime_argument,
        default=None,
        metavar="ISO_DATETIME",
        help="Stop admitting observation work at this timezone-aware timestamp",
    )
    parser.add_argument(
        "--observe-hard-deadline-at",
        type=_observation_datetime_argument,
        default=None,
        metavar="ISO_DATETIME",
        help="Fixed process deadline enforced by the one-shot launcher",
    )
    parser.add_argument(
        "--fmp-daily-budget",
        type=_fmp_budget_argument,
        default=None,
        help=(
            "Override the process-local FMP request budget "
            f"(0-{settings.FMP_FREE_DAILY_REQUEST_BUDGET_CAP})"
        ),
    )
    parser.add_argument(
        "--task-log",
        default="",
        help="Append stdout/stderr to this log path for Task Scheduler",
    )
    return parser


def _fmp_budget_argument(value: str) -> int:
    """Parse a bounded FMP budget for one scheduler process."""
    try:
        budget = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("FMP budget must be an integer") from exc
    cap = settings.FMP_FREE_DAILY_REQUEST_BUDGET_CAP
    if not 0 <= budget <= cap:
        raise argparse.ArgumentTypeError(
            f"FMP budget must be between 0 and {cap}"
        )
    return budget


def _observation_datetime_argument(value: str) -> datetime:
    """Parse an explicit ISO-8601 deadline and normalize it to Eastern time."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("deadline must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("deadline must include a UTC offset")
    return parsed.astimezone(_ET)


def _run_cli_args(args: argparse.Namespace) -> int:
    """Apply process-local controls and invoke the scheduler."""
    if args.observe_health:
        if not args.dry_run or args.enable_orders or not args.now or not args.session:
            print(
                "[SCHEDULER] --observe-health requires explicit --dry-run, --now, and --session."
            )
            return 2
        stop_at = args.observe_stop_at
        hard_deadline_at = args.observe_hard_deadline_at
        if stop_at is None or hard_deadline_at is None:
            print(
                "[SCHEDULER] --observe-health requires --observe-stop-at and "
                "--observe-hard-deadline-at."
            )
            return 2
        if not args.operation_manifest:
            print(
                "[SCHEDULER] --observe-health requires an admitted --operation-manifest."
            )
            return 2
        try:
            manifest = load_operation_manifest(
                args.operation_manifest, expected_operation="observer"
            )
            enforce_manifest_session_window(manifest, now=_now_et())
            validate_runtime_binding(manifest, Path(__file__).resolve().parent)
            bound_start = datetime.fromisoformat(
                str(manifest.binding["session_start"])
            ).astimezone(_ET)
            bound_stop = datetime.fromisoformat(
                str(manifest.binding["session_stop"])
            ).astimezone(_ET)
            expected_hard_deadline = bound_stop + timedelta(
                seconds=manifest.deadlines_seconds["hard_deadline_grace_seconds"]
            )
            if stop_at != bound_stop or hard_deadline_at != expected_hard_deadline:
                raise OperationManifestError(
                    "CLI stop/deadline values do not match the admitted manifest"
                )
            if (bound_stop - bound_start).total_seconds() > manifest.deadlines_seconds[
                "session_seconds"
            ]:
                raise OperationManifestError(
                    "manifest session_seconds cannot cover the bound session window"
                )
            budget = OperationBudget(manifest)
            settings.load_runtime_credentials()
        except OperationManifestError as exc:
            print(f"[SCHEDULER] Observer contract rejected before client construction: {exc}")
            return 2
        notification_settings = (
            "NOTIFY_EMAIL_FROM",
            "NOTIFY_EMAIL_TO",
            "NOTIFY_EMAIL_PASSWORD",
        )
        nonblank_notifications = [
            name for name in notification_settings if str(getattr(settings, name, "")).strip()
        ]
        if nonblank_notifications:
            print(
                "[SCHEDULER] --observe-health requires blank notification "
                f"credentials; got configured values for {nonblank_notifications}."
            )
            return 2
    if args.fmp_daily_budget is not None:
        if args.observe_health:
            settings.FMP_DAILY_REQUEST_BUDGET = min(
                int(settings.FMP_DAILY_REQUEST_BUDGET), args.fmp_daily_budget
            )
        else:
            settings.FMP_DAILY_REQUEST_BUDGET = args.fmp_daily_budget
    if args.observe_health:
        observation = SchedulerObservation(
            run_id=f"scheduler-{uuid4().hex}",
            snapshot_path=os.environ.get("SCHEDULER_OBSERVATION_SNAPSHOT_PATH") or None,
        )
        with (
            activate_scheduler_observation(observation),
            activate_operation_budget(budget),
            budgeted_standard_output(budget),
        ):
            fmp_limit = fmp_observation_request_limit(
                manifest.http_category_limits["fmp_read"]
            )
            missing_ledger = any(
                issue["code"] in {"fmp_ledger_missing", "fmp_ledger_unreadable"}
                for issue in observation.to_receipt()["issues"]
            )
            if missing_ledger:
                _record_observation_resource_snapshots(observation)
            else:
                try:
                    with single_attempt_alpaca_requests(), alpaca_http_request_budget(
                        manifest.total_http_attempts
                    ), fmp_request_budget(fmp_limit):
                        try:
                            run_scheduler(
                                dry_run=True,
                                run_now=True,
                                stop_after_session=True,
                                observe_health=True,
                                observation_stop_at=stop_at,
                                observation_hard_deadline_at=hard_deadline_at,
                            )
                        except SchedulerAlreadyRunningError as exc:
                            print(f"[SCHEDULER] {exc}; observation did not start.")
                            observation.latch_service_issue(
                                "scheduler_already_running", {}, unverified=True
                            )
                        except OperationCapExceeded as exc:
                            print(f"[SCHEDULER] Observer contract stopped the run: {exc}")
                        except Exception as exc:  # noqa: BLE001
                            print(f"[SCHEDULER ERROR] Observation failed: {exc}")
                            try:
                                observation.record_event(
                                    "scheduler", "observation", "failed",
                                    {"error_type": type(exc).__name__},
                                )
                            except OperationCapExceeded:
                                pass
                            if observation.to_receipt()["service_health"] == "healthy":
                                observation.latch_service_issue(
                                    "scheduler_observation_failed",
                                    {"error_type": type(exc).__name__},
                                )
                        finally:
                            try:
                                _record_observation_resource_snapshots(observation)
                            except OperationCapExceeded:
                                observation.latch_service_issue(
                                    "observer_resource_snapshot_cap_denied", {},
                                )
                except RuntimeError as exc:
                    print(f"[SCHEDULER ERROR] Could not activate observation budgets: {exc}")
                    observation.latch_service_issue(
                        "observation_budget_activation_failed",
                        {"error_type": type(exc).__name__},
                    )
        actual_stop_at = _now_et()
        window = observation.to_receipt()["execution_window"]
        admitted_at = window.get("scheduler_started_at")
        deadline_exceeded = bool(
            admitted_at
            and datetime.fromisoformat(admitted_at) < hard_deadline_at
            and actual_stop_at >= hard_deadline_at
        )
        with activate_scheduler_observation(observation), activate_operation_budget(budget):
            try:
                observation.mark_stopped(
                    actual_stop_at,
                    deadline_exceeded=deadline_exceeded,
                )
            except OperationCapExceeded as exc:
                observation.latch_service_issue(
                    "observer_final_event_cap_denied", {"reason": str(exc)}
                )
            if str(budget.snapshot()["incomplete_reason"]).startswith("output_bytes:"):
                _observer_exit_code(observation, budget)

            prefix = "SCHEDULER_OBSERVATION_RECEIPT="
            line = ""
            receipt: dict[str, Any] = {}
            try:
                charged = 0
                for _ in range(3):
                    budget_state = budget.snapshot()
                    receipt = observation.to_receipt()
                    receipt["evidence_complete"] = budget_state["evidence_complete"]
                    receipt["operation_budget"] = budget_state
                    line = prefix + json.dumps(receipt, sort_keys=True)
                    required = len((line + "\n").encode("utf-8"))
                    if required <= charged:
                        break
                    budget.consume_output(required - charged)
                    charged = required
                else:
                    raise OperationCapExceeded(
                        "final observer receipt byte count did not stabilize"
                    )
                print(line)
            except OperationCapExceeded:
                return _observer_exit_code(observation, budget)
        return _observer_exit_code(observation, budget)
    try:
        run_scheduler(
            dry_run=not args.enable_orders,
            run_now=args.now,
            stop_after_session=args.session,
        )
    except SchedulerAlreadyRunningError as exc:
        print(f"[SCHEDULER] {exc}; duplicate start ignored.")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """Run the safe scheduler CLI and make duplicate task starts a no-op."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.task_log:
        return _run_cli_args(args)

    log_path = Path(args.task_log)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                return _run_cli_args(args)
            except BaseException:  # noqa: BLE001
                traceback.print_exc()
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
