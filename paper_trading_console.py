"""Operator console for paper-trading deployment and observation.

This script is the day-to-day control surface for supervised operation in an
Alpaca paper account. Live-account trading is out of scope.

Examples:
    python paper_trading_console.py doctor
    python paper_trading_console.py status --limit 10
    python paper_trading_console.py run-now
    python scheduler.py --enable-orders --now
    python paper_trading_console.py install-task
    python paper_trading_console.py task-status
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from auto_trader import _rank_entry_candidates, run_auto_trader
from config import settings
from core.execution_store import get_execution_store
from core.gmail_oauth import (
    GmailOAuthError,
    authorize_gmail,
    revoke_gmail_authorization,
)
from core.notifier import _is_configured as notify_configured
from core.notifier import send_email
from core.order_execution import (
    _get_trading_client,
    _is_paper_mode,
    get_open_orders,
    get_open_positions,
)
from setup_windows_task import LOG_FILE as SCHEDULER_LOG
from setup_windows_task import register_task, show_status


PROJECT_DIR = Path(__file__).resolve().parent
SCAN_RESULTS_DIR = PROJECT_DIR / settings.RESULTS_DIR


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    severity: str = "ok"


def run_doctor() -> int:
    """Validate the local paper-trading deployment configuration."""
    checks = [
        _check_paper_mode(),
        _check_api_keys_present(),
        _check_execution_store_path(),
        _check_scan_results_dir(),
        _check_email_configuration(),
        _check_alpaca_connectivity(),
    ]

    print("=" * 60)
    print("PAPER TRADING DOCTOR")
    print("=" * 60)
    failures = 0
    for check in checks:
        marker = "OK" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")
        if not check.ok:
            failures += 1

    print("-" * 60)
    if failures:
        print(f"Doctor finished with {failures} failing check(s).")
        return 1

    print("Doctor finished cleanly. Paper deployment is ready.")
    return 0


def run_checklist(limit: int = 10) -> int:
    """Run a single pre-paper-trading checklist across config, broker, store, and signals."""
    checks = [
        _check_paper_mode(),
        _check_api_keys_present(),
        _check_execution_store_path(),
        _check_execution_store_health(limit=limit),
        _check_scan_results_dir(),
        _check_recent_signal_quality(limit=limit),
        _check_email_configuration(),
        _check_alpaca_connectivity(),
    ]

    print("=" * 60)
    print("PAPER TRADING CHECKLIST")
    print("=" * 60)

    failures = 0
    warnings = 0
    for check in checks:
        marker = _format_check_marker(check)
        print(f"[{marker}] {check.name}: {check.detail}")
        if not check.ok or check.severity == "fail":
            failures += 1
        elif check.severity == "warn":
            warnings += 1

    print("-" * 60)
    if failures:
        print(
            f"Checklist finished with {failures} failure(s) and {warnings} warning(s). "
            "Do not enable paper automation yet."
        )
        return 1

    if warnings:
        print(
            f"Checklist finished with {warnings} warning(s) and no hard failures. "
            "Safe for supervised paper testing, but review the warnings first."
        )
        return 0

    print("Checklist finished cleanly. Safe to begin supervised paper trading.")
    return 0


def print_status(limit: int = 10) -> int:
    """Print current paper-trading status, signals, and execution activity."""
    print("=" * 60)
    print("PAPER TRADING STATUS")
    print("=" * 60)
    print(f"Paper mode: {_is_paper_mode()}")
    print(f"Execution store: {settings.EXECUTION_STORE_DB_PATH}")
    print(f"Scheduler log: {SCHEDULER_LOG}")

    positions = get_open_positions()
    orders = get_open_orders()

    _print_account_summary()
    _print_positions(positions)
    _print_open_orders(orders)
    _print_recent_workflows(limit=limit)
    _print_latest_scan_summary(positions=positions, orders=orders, limit=limit)

    return 0


def _print_account_summary() -> None:
    try:
        client = _get_trading_client()
        account = client.get_account()
        clock = client.get_clock()
        print("-" * 60)
        print("Account")
        print(f"Equity: ${float(account.equity):,.2f}")
        print(f"Buying power: ${float(account.buying_power):,.2f}")
        print(f"Market open: {bool(clock.is_open)}")
    except Exception as exc:  # noqa: BLE001
        print("-" * 60)
        print(f"Account: unavailable ({exc})")


def _print_positions(positions: list[object]) -> None:
    print("-" * 60)
    print(f"Open positions: {len(positions)}")
    for position in positions:
        print(
            f"{position.symbol}: qty={position.qty} avg=${position.avg_entry_price:.2f} "
            f"last=${position.current_price:.2f} pnl={position.unrealized_pl_pct * 100:.2f}%"
        )


def _print_open_orders(orders: list[object]) -> None:
    print("-" * 60)
    print(f"Open orders: {len(orders)}")
    for order in orders[:20]:
        side = str(getattr(order, "side", "")).split(".")[-1]
        order_type = str(getattr(order, "type", "")).split(".")[-1]
        symbol = str(getattr(order, "symbol", ""))
        qty = getattr(order, "qty", "")
        client_order_id = str(getattr(order, "client_order_id", "") or "")
        print(
            f"{symbol}: {side} {qty} type={order_type} client_order_id={client_order_id or 'n/a'}"
        )


def _print_recent_workflows(limit: int) -> None:
    rows = get_execution_store().list_recent_workflows(limit=limit)
    print("-" * 60)
    print(f"Recent execution workflows: {len(rows)}")
    for row in rows:
        entry_plan = row.get("entry_plan") or {}
        entry_price = entry_plan.get("entry_price")
        qty = entry_plan.get("qty")
        entry_text = ""
        if entry_price is not None and qty is not None:
            entry_text = f" | qty={qty} entry=${float(entry_price):.2f}"
        print(
            f"{row['updated_at_utc']} | {row['symbol']} | {row['state'] or 'n/a'} "
            f"| wf={row['workflow_id']}{entry_text}"
        )


def _print_latest_scan_summary(*, positions: list[object], orders: list[object], limit: int) -> None:
    latest_scan = _find_latest_scan_file()
    print("-" * 60)
    if latest_scan is None:
        print("Latest scan: none found")
        return

    print(f"Latest scan: {latest_scan.name}")
    try:
        df = pd.read_csv(latest_scan)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read latest scan CSV: {exc}")
        return

    actionable = df[df.get("Scanner_Category") == "actionable_buy"] if "Scanner_Category" in df else pd.DataFrame()
    watchlist = (
        df[df.get("Scanner_Category") == "watchlist_candidate"]
        if "Scanner_Category" in df
        else pd.DataFrame()
    )
    print(f"Actionable buys: {len(actionable)} | Watchlist candidates: {len(watchlist)}")
    _print_execution_shortlist(actionable=actionable, positions=positions, orders=orders, limit=limit)
    if not actionable.empty:
        print("Top actionable buys:")
        _print_signal_rows(actionable.head(limit))
    elif not watchlist.empty:
        print("Top watchlist candidates:")
        _print_signal_rows(watchlist.head(limit))


def _print_signal_rows(rows: pd.DataFrame) -> None:
    for _, row in rows.iterrows():
        symbol = row.get("Symbol", "n/a")
        rs_score = row.get("RS_Score", "n/a")
        canslim_score = row.get("CANSLIM_Score", "n/a")
        notes = row.get("Scanner_Notes", "")
        print(f"{symbol}: RS={rs_score} CANSLIM={canslim_score} notes={notes}")


def _print_execution_shortlist(*, actionable: pd.DataFrame, positions: list[object], orders: list[object], limit: int) -> None:
    """Show which actionable buys would actually route into live/paper execution."""
    held_symbols = {str(getattr(position, "symbol", "")) for position in positions if getattr(position, "symbol", "")}
    open_order_symbols = {str(getattr(order, "symbol", "")) for order in orders if getattr(order, "symbol", "")}
    pending_entry_symbols = {
        str(getattr(order, "symbol", ""))
        for order in orders
        if getattr(order, "symbol", "") and str(getattr(order, "side", "")).split(".")[-1].strip().lower() == "buy"
    }
    shortlisted, deprioritized, skipped_active, execution_slots = _compute_execution_shortlist_from_scan(
        actionable=actionable,
        held_symbols=held_symbols,
        open_order_symbols=open_order_symbols,
        pending_entry_symbols=pending_entry_symbols,
        max_new_entries=settings.MAX_NEW_ENTRIES_PER_CYCLE,
        max_open_positions=settings.MAX_OPEN_POSITIONS,
    )

    print(f"Execution shortlist capacity this cycle: {execution_slots}")
    if shortlisted:
        print("Would route into execution:")
        _print_shortlist_rows(shortlisted[:limit])
    if deprioritized:
        print("Deprioritized actionable buys:")
        _print_shortlist_rows(deprioritized[:limit])
    if skipped_active:
        print(f"Already active, so skipped: {', '.join(skipped_active[:limit])}")


def _print_shortlist_rows(rows: list[dict]) -> None:
    for row in rows:
        print(
            f"{row['symbol']}: RS={row['rs_score']:.1f} "
            f"CANSLIM={row['total_score']:.1f} surge={row['has_volume_surge']}"
        )


def _compute_execution_shortlist_from_scan(
    *,
    actionable: pd.DataFrame,
    held_symbols: set[str],
    open_order_symbols: set[str],
    pending_entry_symbols: set[str],
    max_new_entries: int,
    max_open_positions: int,
) -> tuple[list[dict], list[dict], list[str], int]:
    """Mirror live execution ranking on the latest scan export for operator visibility."""
    active_symbols = {symbol for symbol in (held_symbols | open_order_symbols) if symbol}
    active_slot_count = len({symbol for symbol in (held_symbols | pending_entry_symbols) if symbol})
    available_slots = max(0, max_open_positions - active_slot_count)
    execution_slots = min(available_slots, max(1, int(max_new_entries)))
    actionable_rows = [_scan_row_to_candidate(row) for _, row in actionable.iterrows()]
    ranked = _rank_entry_candidates(actionable_rows)

    shortlisted: list[dict] = []
    deprioritized: list[dict] = []
    skipped_active: list[str] = []
    for candidate in ranked:
        symbol = candidate["symbol"]
        if symbol in active_symbols:
            skipped_active.append(symbol)
            continue
        if len(shortlisted) < execution_slots:
            shortlisted.append(candidate)
        else:
            deprioritized.append(candidate)

    return shortlisted, deprioritized, skipped_active, execution_slots


def _scan_row_to_candidate(row: pd.Series) -> dict:
    """Convert one exported scan row into the ranking shape used by live execution."""
    return {
        "symbol": str(row.get("Symbol", "")),
        "total_score": float(row.get("CANSLIM_Score", 0.0) or 0.0),
        "rs_score": float(row.get("RS_Score", 0.0) or 0.0),
        "has_volume_surge": bool(row.get("Has_Volume_Surge", False)),
        "metrics": {
            "current_growth": _safe_float(row.get("Current_Growth")),
            "annual_growth": _safe_float(row.get("Annual_Growth")),
            "proximity_to_high": _safe_float(row.get("Proximity_to_High")),
        },
    }


def _safe_float(value: object) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_latest_scan_file() -> Optional[Path]:
    if not SCAN_RESULTS_DIR.exists():
        return None
    candidates = sorted(SCAN_RESULTS_DIR.glob("*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _check_paper_mode() -> CheckResult:
    if _is_paper_mode():
        return CheckResult("Paper mode", True, "ALPACA_PAPER=true")
    return CheckResult("Paper mode", False, "ALPACA_PAPER is false; live trading is blocked for this workflow")


def _check_api_keys_present() -> CheckResult:
    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", settings.ALPACA_API_KEY),
            ("ALPACA_SECRET_KEY", settings.ALPACA_SECRET_KEY),
            ("FMP_API_KEY", settings.FMP_API_KEY),
        )
        if not value
    ]
    if missing:
        return CheckResult("API keys", False, f"Missing: {', '.join(missing)}")
    return CheckResult("API keys", True, "Alpaca and FMP keys present")


def _check_execution_store_path() -> CheckResult:
    try:
        db_path = Path(settings.EXECUTION_STORE_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return CheckResult("Execution store", True, str(db_path))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Execution store", False, str(exc))


def _check_scan_results_dir() -> CheckResult:
    try:
        SCAN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        return CheckResult("Scan results directory", True, str(SCAN_RESULTS_DIR))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Scan results directory", False, str(exc))


def _check_email_configuration() -> CheckResult:
    if notify_configured():
        return CheckResult("Email notifications", True, "configured")
    return CheckResult(
        "Email notifications",
        True,
        "not configured (observability will rely on logs/status)",
        severity="warn",
    )


def _check_alpaca_connectivity() -> CheckResult:
    try:
        client = _get_trading_client()
        account = client.get_account()
        return CheckResult("Alpaca connectivity", True, f"connected; equity=${float(account.equity):,.2f}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Alpaca connectivity", False, str(exc))


def _check_execution_store_health(limit: int) -> CheckResult:
    try:
        rows = get_execution_store().list_recent_workflows(limit=max(1, int(limit)))
        db_path = Path(settings.EXECUTION_STORE_DB_PATH)
        detail = f"reachable at {db_path} | recent workflows={len(rows)}"
        if not rows:
            detail += " (expected for a brand-new paper deployment)"
            return CheckResult("Execution store health", True, detail, severity="warn")
        return CheckResult("Execution store health", True, detail)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Execution store health", False, str(exc), severity="fail")


def _check_recent_signal_quality(limit: int) -> CheckResult:
    latest_scan = _find_latest_scan_file()
    if latest_scan is None:
        return CheckResult(
            "Recent signal quality",
            True,
            "no scan export found yet",
            severity="warn",
        )

    try:
        df = pd.read_csv(latest_scan)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Recent signal quality", False, f"could not read {latest_scan.name}: {exc}", severity="fail")

    required_columns = {"Symbol", "Scanner_Category"}
    missing = sorted(required_columns - set(df.columns))
    if missing:
        return CheckResult(
            "Recent signal quality",
            False,
            f"{latest_scan.name} missing columns: {', '.join(missing)}",
            severity="fail",
        )

    actionable = df[df["Scanner_Category"] == "actionable_buy"]
    watchlist = df[df["Scanner_Category"] == "watchlist_candidate"]
    latest_scan_age = _format_scan_age(latest_scan)
    shortlist, _, _, execution_slots = _compute_execution_shortlist_from_scan(
        actionable=actionable,
        held_symbols=set(),
        open_order_symbols=set(),
        pending_entry_symbols=set(),
        max_new_entries=settings.MAX_NEW_ENTRIES_PER_CYCLE,
        max_open_positions=settings.MAX_OPEN_POSITIONS,
    )
    top_symbols = ", ".join(row["symbol"] for row in shortlist[: max(1, min(limit, 3))]) or "none"
    detail = (
        f"{latest_scan.name} ({latest_scan_age}) | actionable={len(actionable)} "
        f"| watchlist={len(watchlist)} | executable_top={len(shortlist)}/{execution_slots} "
        f"| top={top_symbols}"
    )

    if len(actionable) == 0:
        return CheckResult("Recent signal quality", True, detail, severity="warn")
    return CheckResult("Recent signal quality", True, detail)


def _format_scan_age(latest_scan: Path) -> str:
    modified_at = datetime.fromtimestamp(latest_scan.stat().st_mtime, tz=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    age = now_utc - modified_at
    if age <= timedelta(hours=24):
        hours = max(0, int(age.total_seconds() // 3600))
        return f"{hours}h old"
    days = max(1, int(age.total_seconds() // 86400))
    return f"{days}d old"


def _format_check_marker(check: CheckResult) -> str:
    if check.severity == "fail" or not check.ok:
        return "FAIL"
    if check.severity == "warn":
        return "WARN"
    return "OK"


def run_now(*, dry_run: bool) -> int:
    """Run the trading cycle immediately."""
    run_auto_trader(dry_run=dry_run)
    return 0


def _notification_email(email: str | None) -> str:
    value = str(email or settings.NOTIFY_EMAIL_FROM or "").strip()
    if not value:
        raise GmailOAuthError(
            "Gmail address is required; set NOTIFY_EMAIL_FROM or pass --email"
        )
    return value


def run_email_auth(*, client_secrets: Path, email: str | None) -> int:
    """Open Google's desktop consent flow and store the grant in the OS credential vault."""
    try:
        result = authorize_gmail(_notification_email(email), client_secrets)
    except GmailOAuthError as exc:
        print(f"Gmail authorization failed: {exc}")
        return 1
    print(f"Gmail OAuth authorized for {result.email}; credential stored in Windows Credential Manager.")
    return 0


def run_email_test() -> int:
    """Send one notification through the same backend used by trading workflows."""
    sent = send_email(
        "[CANSLIM] Gmail OAuth notification test",
        "This test was sent through the browser-authorized Gmail API notification backend.",
    )
    if sent:
        print("Gmail OAuth test notification sent successfully.")
        return 0
    print("Gmail OAuth test notification failed; review notification configuration and logs.")
    return 1


def run_email_revoke(*, email: str | None) -> int:
    """Revoke the Google grant and remove its local Windows credential."""
    try:
        result = revoke_gmail_authorization(_notification_email(email))
    except GmailOAuthError as exc:
        print(f"Gmail authorization revocation failed: {exc}")
        return 1
    print(f"Gmail OAuth authorization revoked for {result.email}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper trading deployment and observation console")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Validate paper-trading deployment prerequisites")
    checklist_parser = subparsers.add_parser("checklist", help="Run the full pre-paper-trading checklist")
    checklist_parser.add_argument("--limit", type=int, default=10, help="Rows to inspect in store/signal checks")

    status_parser = subparsers.add_parser("status", help="Show paper account, signals, and workflow status")
    status_parser.add_argument("--limit", type=int, default=10, help="Rows to show in status sections")

    run_now_parser = subparsers.add_parser(
        "run-now",
        help="Run one cycle immediately (dry run by default)",
    )
    run_now_mode = run_now_parser.add_mutually_exclusive_group()
    run_now_mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Observe signals without submitting paper orders (default)",
    )
    run_now_mode.add_argument(
        "--enable-orders",
        dest="dry_run",
        action="store_false",
        help="Refused here; use scheduler.py --enable-orders --now",
    )

    install_parser = subparsers.add_parser(
        "install-task",
        help="Register the Windows scheduler task (dry-run by default)",
    )
    install_parser.add_argument(
        "--enable-orders",
        action="store_true",
        help="Register the order-enabled paper scheduler after validation and approval",
    )
    subparsers.add_parser("task-status", help="Show Windows task scheduler status")

    email_auth_parser = subparsers.add_parser(
        "email-auth",
        help="Authorize Gmail notifications in a browser and store the grant in Windows Credential Manager",
    )
    email_auth_parser.add_argument(
        "--client-secrets",
        type=Path,
        required=True,
        help="Google Desktop OAuth client JSON downloaded from Google Cloud",
    )
    email_auth_parser.add_argument(
        "--email",
        help="Gmail account to authorize (defaults to NOTIFY_EMAIL_FROM)",
    )
    subparsers.add_parser(
        "email-test",
        help="Send one test through the configured notification backend",
    )
    email_revoke_parser = subparsers.add_parser(
        "email-revoke",
        help="Revoke Gmail OAuth and remove the local Windows credential",
    )
    email_revoke_parser.add_argument(
        "--email",
        help="Gmail account to revoke (defaults to NOTIFY_EMAIL_FROM)",
    )

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "doctor":
        return run_doctor()
    if args.command == "checklist":
        return run_checklist(limit=args.limit)
    if args.command == "status":
        return print_status(limit=args.limit)
    if args.command == "run-now":
        if not args.dry_run:
            parser.error(
                "paper_trading_console.py run-now is dry-run only; "
                "use `python scheduler.py --enable-orders --now` for the canonical order path"
            )
        return run_now(dry_run=True)
    if args.command == "install-task":
        return register_task(dry_run=not args.enable_orders)
    if args.command == "task-status":
        return show_status()
    if args.command == "email-auth":
        return run_email_auth(client_secrets=args.client_secrets, email=args.email)
    if args.command == "email-test":
        return run_email_test()
    if args.command == "email-revoke":
        return run_email_revoke(email=args.email)

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
