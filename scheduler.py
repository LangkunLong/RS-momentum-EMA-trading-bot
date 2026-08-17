"""Daily market-hours scheduler for the CANSLIM auto-trader.

Runs as a long-lived process.  Each weekday:

  09:31 ET  — Full CANSLIM scan + bracket buy entries (once per day)
  Each hour (10:01, 11:01, …, 16:01 ET)
            — Hourly exit check: 21-period hourly EMA + stop-loss vs live P&L
  Every 30 min (09:30–16:05 ET)
            — Daily exit check (fallback): 21-day EMA + Alpaca unrealised P&L
  All day   — Fill-monitor WebSocket in background daemon thread

Usage:
    python scheduler.py               # live paper trading
    python scheduler.py --dry-run     # no real orders, logs what would happen
    python scheduler.py --now         # run scan immediately, then follow normal schedule

Hourly monitoring catches MA violations and stop-loss breaches faster than
the daily check, because a 21-period hourly EMA tracks roughly 2.6 trading
days of intraday structure instead of 21 calendar days of daily closes.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from auto_trader import (
    monitor_and_exit_positions,
    monitor_exits_hourly,
    run_auto_trader,
)
from core.notifier import notify_cycle_summary
from core.order_execution import _get_trading_client, _is_paper_mode
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


def run_scheduler(dry_run: bool = False, run_now: bool = False) -> None:
    """Start the daily trading loop.

    Args:
        dry_run: When True, all order functions print their intent but submit nothing.
        run_now: When True, run the full scan immediately at startup instead of
            waiting for 09:31 ET.  Useful for manual testing during market hours.
    """
    mode = "DRY RUN" if dry_run else ("paper" if _is_paper_mode() else "LIVE")
    print(f"[SCHEDULER] Starting CANSLIM scheduler [{mode}]")
    print("[SCHEDULER] Press Ctrl-C to stop.")

    monitor: FillMonitor | None = None
    if dry_run:
        print("[SCHEDULER] Fill monitor disabled in dry-run mode.")
    else:
        monitor = FillMonitor()
        monitor.start()
        _run_startup_stop_reconciliation()

    last_scan_date: date | None = None
    last_daily_exit: datetime = datetime.min.replace(tzinfo=_ET)
    last_hourly_exit_hour: int = -1
    last_quiet_log: datetime = datetime.min.replace(tzinfo=_ET)
    session_seen_open_date: date | None = None

    # Optional immediate scan at startup
    if run_now:
        print(f"\n[SCHEDULER] --now flag: running scan immediately at {_now_et().strftime('%H:%M ET')}")
        try:
            _run_cycle(dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"[SCHEDULER ERROR] Immediate scan failed: {exc}")
        finally:
            # Always mark today as scanned so we don't retry on the next tick
            last_scan_date = _now_et().date()

    try:
        while True:
            if monitor is not None:
                monitor = _ensure_fill_monitor_running(monitor, dry_run=False)
            now = _now_et()
            today = now.date()

            if session_seen_open_date != today:
                session_seen_open_date = None

            in_market_hours = _is_market_hours(now)
            in_exit_window = _is_exit_monitor_window(now)
            market_clock_open = _market_clock_is_open() if (in_market_hours or in_exit_window) else None

            if market_clock_open and in_market_hours:
                session_seen_open_date = today

            market_session_live = in_market_hours and (
                market_clock_open if market_clock_open is not None else True
            )
            exit_session_live = False
            if in_exit_window:
                if market_clock_open is None:
                    exit_session_live = True
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
                        _run_cycle(dry_run)
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
                        exited = monitor_exits_hourly(dry_run=dry_run)
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
                        exited = monitor_and_exit_positions(dry_run=dry_run)
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


def _ensure_fill_monitor_running(monitor: FillMonitor, *, dry_run: bool) -> FillMonitor:
    """Restart the fill monitor if its background stream has died."""
    if monitor.is_running():
        return monitor

    print("[SCHEDULER] Fill monitor is not running. Restarting trade-update stream.")
    try:
        monitor.stop()
    except Exception:  # noqa: BLE001
        pass

    replacement = FillMonitor()
    replacement.start()

    if not dry_run:
        _run_startup_stop_reconciliation()

    return replacement


def _run_startup_stop_reconciliation() -> None:
    """Repair missing protective stops for existing positions at process startup."""
    try:
        results = OrderManager(paper=_is_paper_mode()).reconcile_startup_stops()
    except Exception as exc:  # noqa: BLE001
        print(f"[SCHEDULER ERROR] Startup stop reconciliation failed: {exc}")
        return

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


def _run_cycle(dry_run: bool) -> None:
    """Run the full auto-trader cycle and send a cycle summary email.

    Wraps run_auto_trader() and fires notify_cycle_summary() so the user
    gets a daily email summary of what was entered/exited.  The auto-trader
    returns the exact symbols acted on so reporting stays consistent with the
    execution cycle.
    """
    # run_auto_trader handles its own market-clock guard and prints everything.
    # The cycle summary email is best-effort — notification failure must not
    # prevent the trading cycle from completing.
    result = run_auto_trader(dry_run=dry_run)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CANSLIM Daily Scheduler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print intended orders without submitting (safe for testing schedule timing)",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        default=False,
        help="Run the full scan immediately at startup, then follow the normal schedule",
    )
    args = parser.parse_args()
    run_scheduler(dry_run=args.dry_run, run_now=args.now)
