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
import hashlib
import importlib.metadata
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd

from auto_trader import _rank_entry_candidates, run_auto_trader
from config import settings
from core.data_client import _fmp_get
from core.execution_store import get_execution_store
from core.gmail_oauth import (
    GmailOAuthError,
    authorize_gmail,
    revoke_gmail_authorization,
)
from core.notifier import _configured_backend as notify_configured_backend
from core.notifier import _is_configured as notify_configured
from core.notifier import notification_configuration_error
from core.notifier import send_email
from core.order_execution import (
    _get_trading_client,
    _is_paper_mode,
    get_open_orders,
    get_open_positions,
)
from setup_windows_task import LOG_FILE as SCHEDULER_LOG
from setup_windows_task import TASK_NAME, _schtasks, register_task, show_status


PROJECT_DIR = Path(__file__).resolve().parent
SCAN_RESULTS_DIR = PROJECT_DIR / settings.RESULTS_DIR


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    severity: str = "ok"


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: EvidenceStatus
    detail: str


def run_doctor(*, probe_external: bool = False) -> int:
    """Report readiness evidence without contacting providers unless explicitly requested."""
    return _print_readiness_report(
        "PAPER TRADING READINESS",
        _collect_readiness_checks(probe_external=probe_external),
    )


def run_checklist(limit: int = 10, *, probe_external: bool = False) -> int:
    """Show readiness evidence plus recent scan observations; never imply approval."""
    checks = _collect_readiness_checks(probe_external=probe_external)
    signal_check = _check_recent_signal_quality(limit=limit)
    checks.append(_readiness_from_legacy_check(signal_check))
    return _print_readiness_report("PAPER TRADING CHECKLIST", checks)


def _collect_readiness_checks(*, probe_external: bool) -> list[ReadinessCheck]:
    """Collect local evidence and explicitly label checks that need an observed run."""
    required_keys = bool(settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY and settings.FMP_API_KEY)
    if _is_paper_mode() and required_keys:
        config = ReadinessCheck("Configuration", EvidenceStatus.PASS, "paper mode selected; required key settings are present")
    elif not _is_paper_mode():
        config = ReadinessCheck("Configuration", EvidenceStatus.FAIL, "paper mode is not selected")
    else:
        config = ReadinessCheck("Configuration", EvidenceStatus.FAIL, "one or more required key settings are missing")

    checks = [config]

    if probe_external:
        checks.extend(
            _check_external_access(
                alpaca_account_reader=lambda: _get_trading_client().get_account(),
                fmp_get=_fmp_get,
                paper_mode=_is_paper_mode(),
                fmp_key_present=bool(settings.FMP_API_KEY),
            )
        )
    else:
        checks.extend(
            [
                ReadinessCheck("Alpaca connectivity", EvidenceStatus.UNVERIFIED, "not probed; pass --probe-external for one paper-account read"),
                ReadinessCheck("Paper account identity", EvidenceStatus.UNVERIFIED, "account fingerprint not observed"),
                ReadinessCheck("FMP statement entitlement", EvidenceStatus.UNVERIFIED, "not probed; pass --probe-external for one statement request"),
                ReadinessCheck("FMP price entitlement", EvidenceStatus.UNVERIFIED, "not probed; pass --probe-external for one price request"),
            ]
        )

    checks.extend(
        [
            _check_execution_store_read_only(),
            _check_scheduler_installation(),
            ReadinessCheck(
                "Service health",
                EvidenceStatus.UNVERIFIED,
                "task installation does not establish a healthy process, heartbeat, stream, or reconciliation state",
            ),
            ReadinessCheck(
                "Strategy dry run",
                EvidenceStatus.UNVERIFIED,
                "no fresh no-order strategy-run evidence was supplied to this report",
            ),
        ]
    )
    account_identity = next(check for check in checks if check.name == "Paper account identity")
    account_fingerprint = "unverified"
    if account_identity.status is EvidenceStatus.PASS and "paper account fingerprint=" in account_identity.detail:
        account_fingerprint = account_identity.detail.partition("paper account fingerprint=")[2]
    identity = _runtime_identity_record(account_fingerprint=account_fingerprint)
    checks.insert(
        0,
        ReadinessCheck(
            "Runtime identity",
            EvidenceStatus.UNVERIFIED,
            json.dumps(identity, sort_keys=True, separators=(",", ":"))
            + "; canonical deployment selection/approval is not recorded",
        ),
    )
    return checks


def _runtime_identity_record(*, account_fingerprint: str = "unverified") -> dict[str, object]:
    """Build safe execution identity facts; credentials and raw account IDs are excluded."""
    source_revision = "unknown"
    source_tree_dirty: bool | None = None
    try:
        revision_result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
        source_revision = revision_result.stdout.strip() or "unknown"
        status_result = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "status", "--porcelain"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
        source_tree_dirty = bool(status_result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    installed_versions: dict[str, str] = {}
    for package in ("alpaca-py", "pandas", "requests"):
        try:
            installed_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed_versions[package] = "missing"

    paper_mode = _is_paper_mode()
    return {
        "source_revision": source_revision,
        "runtime_identity": {
            "checkout_path": str(PROJECT_DIR),
            "source_tree_dirty": source_tree_dirty,
            "interpreter_path": sys.executable,
            "interpreter_version": sys.version.split()[0],
            "dependency_profile": {
                "manifest": "pyproject.toml",
                "manifest_sha256": _sha256_file_or_unavailable(PROJECT_DIR / "pyproject.toml"),
                "lock_manifest": "requirements-lock.txt",
                "lock_manifest_sha256": _sha256_file_or_unavailable(PROJECT_DIR / "requirements-lock.txt"),
                "installed_versions": installed_versions,
            },
            "configuration_profile": {
                "paper_mode": paper_mode,
                "alpaca_stock_feed": str(settings.ALPACA_STOCK_FEED),
                "fmp_plan": str(settings.FMP_PLAN),
                "price_source": "Alpaca",
                "statement_source": "Financial Modeling Prep",
            },
            "persistent_store_path": str(settings.EXECUTION_STORE_DB_PATH),
            "paper_environment": {
                "broker": "Alpaca",
                "mode": "paper" if paper_mode else "not-paper",
                "account_fingerprint": account_fingerprint,
            },
        },
    }


def _sha256_file_or_unavailable(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _check_external_access(
    *,
    alpaca_account_reader: Callable[[], object],
    fmp_get: Callable[[str, Optional[dict]], object],
    paper_mode: bool,
    fmp_key_present: bool,
    symbol: str = "AAPL",
    today: date | None = None,
) -> list[ReadinessCheck]:
    """Check three provider claims independently; callers supply external adapters."""
    if not paper_mode:
        alpaca = ReadinessCheck("Alpaca connectivity", EvidenceStatus.FAIL, "paper mode is false; account probe was refused")
        account_identity = ReadinessCheck("Paper account identity", EvidenceStatus.UNVERIFIED, "account probe was refused")
    else:
        try:
            account = alpaca_account_reader()
            if account is None:
                raise ValueError("empty account response")
            alpaca = ReadinessCheck("Alpaca connectivity", EvidenceStatus.PASS, "paper account endpoint returned a response")
            raw_account_id = str(getattr(account, "id", "") or "").strip()
            if raw_account_id:
                fingerprint = hashlib.sha256(raw_account_id.encode("utf-8")).hexdigest()[:16]
                account_identity = ReadinessCheck(
                    "Paper account identity",
                    EvidenceStatus.PASS,
                    f"paper account fingerprint=sha256:{fingerprint}",
                )
            else:
                account_identity = ReadinessCheck(
                    "Paper account identity",
                    EvidenceStatus.UNVERIFIED,
                    "account endpoint replied but returned no account identifier to fingerprint",
                )
        except Exception as exc:  # noqa: BLE001
            alpaca = ReadinessCheck(
                "Alpaca connectivity",
                EvidenceStatus.FAIL,
                f"paper account request failed ({type(exc).__name__})",
            )
            account_identity = ReadinessCheck("Paper account identity", EvidenceStatus.UNVERIFIED, "account identity was not returned")

    statement = _check_fmp_records(
        "FMP statement entitlement",
        endpoint="income-statement",
        params={"symbol": symbol, "period": "quarter", "limit": 1},
        fmp_get=fmp_get,
        key_present=fmp_key_present,
    )
    day = today or date.today()
    prices = _check_fmp_records(
        "FMP price entitlement",
        endpoint="historical-price-eod/full",
        params={"symbol": symbol, "from": (day - timedelta(days=10)).isoformat(), "to": day.isoformat()},
        fmp_get=fmp_get,
        key_present=fmp_key_present,
    )
    return [alpaca, account_identity, statement, prices]


def _check_fmp_records(
    name: str,
    *,
    endpoint: str,
    params: dict,
    fmp_get: Callable[[str, Optional[dict]], object],
    key_present: bool,
) -> ReadinessCheck:
    if not key_present:
        return ReadinessCheck(name, EvidenceStatus.UNVERIFIED, "FMP key setting is missing; endpoint was not probed")
    try:
        response = fmp_get(endpoint, params)
    except Exception as exc:  # noqa: BLE001
        return ReadinessCheck(name, EvidenceStatus.UNVERIFIED, f"request did not establish entitlement ({type(exc).__name__})")
    if isinstance(response, (list, tuple)) and response and isinstance(response[0], dict):
        record = response[0]
        required_fields = {"symbol", "date", "close"} if endpoint == "historical-price-eod/full" else {"symbol", "date", "revenue"}
        missing_fields = sorted(field for field in required_fields if record.get(field) in (None, ""))
        if str(record.get("symbol", "")).upper() != str(params.get("symbol", "")).upper():
            missing_fields.append("matching_symbol")
        if missing_fields:
            return ReadinessCheck(
                name,
                EvidenceStatus.UNVERIFIED,
                f"endpoint response did not establish required sample fields: {', '.join(missing_fields)}",
            )
        detail = f"endpoint returned {len(response)} usable record(s) for the representative probe"
        if endpoint == "historical-price-eod/full":
            detail += "; this entitlement probe is separate from the app's Alpaca price source"
        return ReadinessCheck(name, EvidenceStatus.PASS, detail)
    return ReadinessCheck(
        name,
        EvidenceStatus.UNVERIFIED,
        "endpoint returned no usable records; entitlement is not established (empty data and access failures are indistinguishable)",
    )


def _check_execution_store_read_only() -> ReadinessCheck:
    """Inspect an existing execution store without creating, migrating, or writing it."""
    db_path = Path(settings.EXECUTION_STORE_DB_PATH)
    if not db_path.exists():
        return ReadinessCheck("Persistence", EvidenceStatus.UNVERIFIED, f"store does not exist at {db_path}; no file was created")
    if not db_path.is_file():
        return ReadinessCheck("Persistence", EvidenceStatus.FAIL, f"configured store path is not a file: {db_path}")

    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
    except sqlite3.Error as exc:
        return ReadinessCheck("Persistence", EvidenceStatus.FAIL, f"read-only store inspection failed ({type(exc).__name__})")

    required_tables = {"workflow_snapshots", "workflow_transitions", "workflow_order_refs", "active_positions"}
    if not integrity or integrity[0] != "ok":
        return ReadinessCheck("Persistence", EvidenceStatus.FAIL, "read-only SQLite integrity check failed")
    if not required_tables.issubset(tables):
        return ReadinessCheck("Persistence", EvidenceStatus.FAIL, "existing SQLite file lacks the expected execution-store schema")
    return ReadinessCheck("Persistence", EvidenceStatus.PASS, f"existing execution store passed read-only checks at {db_path}")


def _check_scheduler_installation() -> ReadinessCheck:
    try:
        return_code, output = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    except OSError as exc:
        return ReadinessCheck("Scheduler installation", EvidenceStatus.UNVERIFIED, f"read-only task query unavailable ({type(exc).__name__})")
    if return_code == 0:
        return ReadinessCheck("Scheduler installation", EvidenceStatus.PASS, f"task {TASK_NAME!r} is registered; action details still require identity review")
    lowered = output.lower()
    if "cannot find" in lowered or "does not exist" in lowered or "not found" in lowered:
        return ReadinessCheck("Scheduler installation", EvidenceStatus.FAIL, f"task {TASK_NAME!r} was not found by the read-only query")
    return ReadinessCheck("Scheduler installation", EvidenceStatus.UNVERIFIED, "read-only task query did not establish installation")


def _readiness_from_legacy_check(check: CheckResult) -> ReadinessCheck:
    if not check.ok or check.severity == "fail":
        status = EvidenceStatus.FAIL
    elif check.severity == "warn":
        status = EvidenceStatus.UNVERIFIED
    else:
        status = EvidenceStatus.PASS
    return ReadinessCheck(check.name, status, check.detail)


def _print_readiness_report(heading: str, checks: list[ReadinessCheck]) -> int:
    print("=" * 60)
    print(heading)
    print("=" * 60)
    for check in checks:
        print(f"[{check.status.value}] {check.name}: {check.detail}")
    failures = sum(check.status is EvidenceStatus.FAIL for check in checks)
    unverified = sum(check.status is EvidenceStatus.UNVERIFIED for check in checks)
    print("-" * 60)
    print(f"Evidence summary: passed={len(checks) - failures - unverified}, failed={failures}, unverified={unverified}")
    if failures:
        print("Readiness is incomplete because one or more checks failed. This report does not authorize order submission.")
        return 1
    if unverified:
        print("Readiness is incomplete because required evidence remains unverified. This report does not authorize order submission.")
        return 2
    print("All reported checks passed. Confirm deployment identity and policy compatibility separately before activation.")
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
    db_path = Path(settings.EXECUTION_STORE_DB_PATH)
    if db_path.exists() and not db_path.is_file():
        return CheckResult("Execution store", False, f"configured path is not a file: {db_path}")
    if not db_path.exists():
        return CheckResult("Execution store", True, f"configured path does not exist; left untouched: {db_path}", severity="warn")
    return CheckResult("Execution store", True, f"configured existing store: {db_path}")


def _check_scan_results_dir() -> CheckResult:
    if not SCAN_RESULTS_DIR.exists():
        return CheckResult("Scan results directory", True, f"not present; left untouched: {SCAN_RESULTS_DIR}", severity="warn")
    if not SCAN_RESULTS_DIR.is_dir():
        return CheckResult("Scan results directory", False, f"configured path is not a directory: {SCAN_RESULTS_DIR}")
    return CheckResult("Scan results directory", True, str(SCAN_RESULTS_DIR))


def _check_email_configuration() -> CheckResult:
    configuration_error = notification_configuration_error()
    if configuration_error is not None:
        return CheckResult("Email notifications", False, configuration_error, severity="fail")
    if notify_configured():
        return CheckResult("Email notifications", True, "configured")
    if str(settings.NOTIFY_EMAIL_PROVIDER or "auto").strip().lower() == "gmail_oauth":
        return CheckResult(
            "Email notifications",
            False,
            "Gmail OAuth is selected but unavailable; run `email-auth` again",
            severity="fail",
        )
    return CheckResult(
        "Email notifications",
        True,
        "not configured (observability will rely on logs/status)",
        severity="warn",
    )


def _check_execution_store_health(limit: int) -> CheckResult:
    del limit  # Compatibility argument; health inspection deliberately does not load workflows.
    check = _check_execution_store_read_only()
    severity = "ok" if check.status is EvidenceStatus.PASS else "warn" if check.status is EvidenceStatus.UNVERIFIED else "fail"
    return CheckResult("Execution store health", check.status is not EvidenceStatus.FAIL, check.detail, severity=severity)


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
    if notify_configured_backend() != "gmail_oauth":
        print("Gmail OAuth test requires NOTIFY_EMAIL_PROVIDER=gmail_oauth with authorization.")
        return 1
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

    doctor_parser = subparsers.add_parser("doctor", help="Report paper-trading readiness evidence")
    doctor_parser.add_argument(
        "--probe-external",
        action="store_true",
        help="Make one Alpaca account read and one each of the FMP statement and price endpoints",
    )
    checklist_parser = subparsers.add_parser("checklist", help="Run the full pre-paper-trading checklist")
    checklist_parser.add_argument("--limit", type=int, default=10, help="Rows to inspect in store/signal checks")
    checklist_parser.add_argument(
        "--probe-external",
        action="store_true",
        help="Make one Alpaca account read and one each of the FMP statement and price endpoints",
    )

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
        return run_doctor(probe_external=args.probe_external)
    if args.command == "checklist":
        return run_checklist(limit=args.limit, probe_external=args.probe_external)
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
