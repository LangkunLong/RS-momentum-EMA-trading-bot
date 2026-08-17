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
import os
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time as dtime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Iterable
from zoneinfo import ZoneInfo

from config import settings
from auto_trader import (
    ExecutionReadinessCheck,
    monitor_and_exit_positions,
    monitor_exits_hourly,
    run_auto_trader,
)
from core.notifier import notify_cycle_summary
from core.order_execution import _get_trading_client, _is_paper_mode, require_paper_mode
from core.order_manager import OrderManager
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


def run_scheduler(
    dry_run: bool = True,
    run_now: bool = False,
    stop_after_session: bool = False,
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
    with SchedulerInstanceLock():
        _run_scheduler_locked(
            dry_run=dry_run,
            run_now=run_now,
            stop_after_session=stop_after_session,
        )


def _run_scheduler_locked(
    *,
    dry_run: bool,
    run_now: bool,
    stop_after_session: bool,
) -> None:
    """Run one scheduler process after the singleton has been acquired."""
    mode = "DRY RUN" if dry_run else "paper"
    print(f"[SCHEDULER] Starting CANSLIM scheduler [{mode}]")
    print("[SCHEDULER] Press Ctrl-C to stop.")

    monitor: FillMonitor | None = None

    def live_execution_ready() -> bool:
        """Read monitor health at the instant an order may submit."""
        return monitor is not None and monitor.is_connected()

    try:
        session_date: date | None = None
        if stop_after_session:
            started_at = _now_et()
            session_date = started_at.date()
            if _session_is_complete(started_at, session_date):
                print(
                    f"[SCHEDULER] {started_at.strftime('%Y-%m-%d %H:%M ET')} — "
                    "no bounded weekday session remains to run."
                )
                return

        if dry_run:
            print("[SCHEDULER] Fill monitor disabled in dry-run mode.")
        else:
            monitor = _start_live_monitor()

        last_scan_date: date | None = None
        last_daily_exit: datetime = datetime.min.replace(tzinfo=_ET)
        last_hourly_exit_hour: int = -1
        last_quiet_log: datetime = datetime.min.replace(tzinfo=_ET)
        session_seen_open_date: date | None = None

        # Optional immediate scan at startup.
        if run_now:
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
            execution_armed = dry_run
            if monitor is not None:
                try:
                    monitor = _ensure_fill_monitor_running(monitor, dry_run=False)
                    execution_armed = True
                except Exception as exc:  # noqa: BLE001
                    execution_armed = False
                    print(f"[SCHEDULER ERROR] Live execution disarmed: {exc}")

            now = _now_et()
            today = now.date()

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

            if market_clock_open and in_market_hours:
                session_seen_open_date = today

            if dry_run:
                market_session_live = in_market_hours and (
                    market_clock_open if market_clock_open is not None else True
                )
            else:
                market_session_live = (
                    execution_armed and in_market_hours and market_clock_open is True
                )

            exit_session_live = False
            if in_exit_window and (dry_run or execution_armed):
                if market_clock_open is None:
                    exit_session_live = dry_run
                elif in_market_hours:
                    exit_session_live = market_clock_open
                else:
                    exit_session_live = session_seen_open_date == today or last_scan_date == today

            if market_session_live:
                current_hour = now.hour

                # ── Daily scan at 09:31 ────────────────────────────────────────
                # Always update last_scan_date after the attempt (success or
                # early-return) so the scheduler does not retry every 30s when
                # run_auto_trader exits early due to the Alpaca market clock.
                if now.time() >= _SCAN_TIME and last_scan_date != today:
                    print(f"\n[SCHEDULER] {now.strftime('%H:%M ET')} — Running daily scan + entries")
                    last_scan_date = today   # set BEFORE the call to prevent retry loops
                    try:
                        if dry_run:
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
                current_hour = now.hour
                if (
                    current_hour in _HOURLY_CHECK_HOURS
                    and now.minute >= 1
                    and current_hour != last_hourly_exit_hour
                ):
                    print(f"\n[SCHEDULER] {now.strftime('%H:%M ET')} — Hourly exit check")
                    try:
                        if dry_run:
                            exited = monitor_exits_hourly(dry_run=dry_run)
                        else:
                            exited = monitor_exits_hourly(
                                dry_run=dry_run,
                                execution_ready=live_execution_ready,
                            )
                        if exited:
                            print(f"[SCHEDULER] Hourly exits: {', '.join(exited)}")
                            notify_cycle_summary(entered=[], exited=exited, paper=_is_paper_mode())
                        last_hourly_exit_hour = current_hour
                    except Exception as exc:  # noqa: BLE001
                        print(f"[SCHEDULER ERROR] Hourly exit check failed: {exc}")

                # ── 30-min daily fallback exit check ──────────────────────────
                elapsed = (now - last_daily_exit).total_seconds()
                if elapsed >= _DAILY_EXIT_INTERVAL_SECS:
                    try:
                        if dry_run:
                            exited = monitor_and_exit_positions(dry_run=dry_run)
                        else:
                            exited = monitor_and_exit_positions(
                                dry_run=dry_run,
                                execution_ready=live_execution_ready,
                            )
                        if exited:
                            print(f"[SCHEDULER] Daily fallback exits: {', '.join(exited)}")
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

            time.sleep(_LOOP_SLEEP_SECS)

    except KeyboardInterrupt:
        print("\n[SCHEDULER] Shutdown requested.")
    finally:
        if monitor is not None:
            monitor.stop()
            print("[SCHEDULER] Fill monitor stopped. Goodbye.")
        else:
            print("[SCHEDULER] Dry-run scheduler stopped. Goodbye.")


def _wait_for_fill_monitor_connection(
    monitor: FillMonitor,
    *,
    timeout_seconds: float = _MONITOR_CONNECT_TIMEOUT_SECS,
    poll_seconds: float = _MONITOR_CONNECT_POLL_SECS,
) -> None:
    """Wait for authenticated stream readiness or fail closed."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
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


def _run_cli_args(args: argparse.Namespace) -> int:
    """Apply process-local controls and invoke the scheduler."""
    if args.fmp_daily_budget is not None:
        settings.FMP_DAILY_REQUEST_BUDGET = args.fmp_daily_budget
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
