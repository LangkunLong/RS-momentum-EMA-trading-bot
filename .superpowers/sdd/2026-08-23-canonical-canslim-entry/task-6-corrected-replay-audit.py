#!/usr/bin/env python3
"""Read-only, fail-closed audit for the corrected Task 6 PIT replay.

The program intentionally performs no replay, resume, repair, or publication.
It is a post-run verifier: a directory without its manifest-last completion
marker is rejected before any artifact content is interpreted.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


_BUNDLE_SHA256 = "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb"
_REGENERATION_GIT_SHA = "d555f7f4c7727d9c6a440bba50cced0fbe9f3095"
_REGENERATION_AUDIT_SCHEMA = 3
_START = "2021-01-01"
_END = "2025-12-31"
_WARMUP = "2020-01-01"
_BENCHMARK = "SPY"
_SESSION_COUNT = 1255
_OUTCOME_SCHEMA_VERSION = 1
_CHECKPOINT_SCHEMA_VERSION = 3
_FUNDAMENTALS_EXCEPTION = "evaluated_pit_quarterly_and_annual_at_least_90_pct"
_PUBLIC_DATE_RULE = (
    "first supplied SPY trading day strictly after SEC acceptance calendar date; "
    "filed date fallback only"
)
_MASTER_COLUMNS = (
    "ticker", "cik", "company_name", "first_membership_date",
    "last_membership_date", "mapping_basis",
)
_EXCLUSION_COLUMNS = (
    "ticker", "company_name", "first_membership_date", "last_membership_date",
    "reason", "details",
)
_SAME_ISSUER_KINDS = {
    "same_issuer_rename", "same_issuer_ticker_reuse", "legacy_survivor_rename",
    "accounting_acquirer_rename",
}

_FRAME_ARTIFACTS = {
    "five_year_leaders.csv",
    "rolling_leader_labels.csv",
    "canslim_signals.csv",
    "entry_attempt_outcomes.csv",
    "daily_entry_funnel.csv",
    "transactions.csv",
    "weekly_holdings.csv",
    "equity_curve.csv",
    "leader_basket_holdings.csv",
    "leader_basket_transactions.csv",
    "leader_basket_equity.csv",
    "leader_recall.csv",
}
_JSON_TEXT_ARTIFACTS = {"coverage.json", "summary.json", "report.md"}
_MANIFEST_ARTIFACTS = _FRAME_ARTIFACTS | _JSON_TEXT_ARTIFACTS
_STATE_FILES = {
    "portfolio_checkpoint.json",
    "portfolio_progress.jsonl",
    "portfolio_state.jsonl",
}
_RUN_FILES = _STATE_FILES | {
    "run_manifest.json",
}
_HASHED_ARTIFACTS = _MANIFEST_ARTIFACTS | (_RUN_FILES - {"run_manifest.json"})
_ENTRY_OUTCOME_COLUMNS = (
    "symbol",
    "signal_date",
    "entry_date",
    "pivot",
    "buy_zone_lower",
    "buy_zone_upper",
    "entry_open",
    "outcome",
)
_FUNNEL_COLUMNS = (
    "signal_date",
    "evaluated_count",
    "qualified_count",
    "attempted_count",
    "executed_count",
    "rejected_count",
)
_SIGNAL_COLUMNS = {
    "symbol", "signal_date", "close", "buy_signal", "buy_signal_without_market",
    "c_score", "a_score", "n_score", "s_score", "i_score", "m_score",
    "current_growth", "annual_growth",
    "rs_score", "has_breakout", "has_volume_surge", "in_buy_zone", "canslim_score",
    "entry_composite_score", "technical_score", "entry_contract_eligible",
    "entry_blocking_reasons", "market_is_bullish", "market_regime_is_bullish",
    "has_peg_today", "signal_reason", "technical_only",
    "pivot", "prior_close", "event_volume", "prior_average_volume_50",
    "entry_volume_ratio", "entry_extension", "price_advanced",
    "technical_setup_eligible", "technical_blocking_reasons",
}
_REQUIRED_DIAGNOSTICS = {
    "signal_days", "entries_allowed_days", "blocked_by_regime_days",
    "blocked_by_market_days", "cash_deployment_override_days", "buy_signal_rows",
    "potential_buy_signal_rows", "potential_buy_signal_rows_blocked_by_market",
    "buy_signal_rows_when_entries_allowed", "buy_signal_rows_blocked_by_regime",
    "buy_signal_rows_blocked_by_market", "buy_signal_rows_blocked_by_both",
    "buy_signal_rows_when_cash_override", "capacity_truncated_signals",
    "entry_attempts", "entries_executed", "entry_rejected_already_open",
    "entry_rejected_capacity", "entry_rejected_missing_data",
    "entry_rejected_invalid_price", "entry_rejected_next_open_buy_zone",
    "entry_rejected_invalid_risk", "entry_rejected_no_cash", "eviction_attempts",
    "evictions_executed", "eviction_rejections",
}
_REJECTION_NAMES = (
    "entry_rejected_already_open", "entry_rejected_capacity",
    "entry_rejected_missing_data", "entry_rejected_invalid_price",
    "entry_rejected_next_open_buy_zone", "entry_rejected_invalid_risk",
    "entry_rejected_no_cash",
)
_QUANTITY_QUANTUM = Decimal("0.000001")
_QUANTITY_ROUNDING_ERROR = Decimal("0.0000005")
_FISCAL_TOLERANCE_DAYS = 28
_CAH_STATEMENT_COUNTS = {
    "quarterly": 70,
    "annual": 30,
    "balance": 157,
}
_BUILTIN_STRATEGY_IDENTITY = {
    "kind": "built_in",
    "module": "core.backtest_engine",
    "qualname": "CanslimStrategy",
    "version": 1,
}


def _fail(message: str) -> None:
    raise AssertionError(message)


def _regular_file(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    if not path.is_file() or path.is_symlink():
        _fail(f"{label} must be a regular, non-link file: {path}")
    return path.resolve()


def _directory(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    if not path.is_dir() or path.is_symlink():
        _fail(f"{label} must be a directory, not a link: {path}")
    return path.resolve()


@dataclass(frozen=True)
class _StateLayout:
    """The journal/checkpoint evidence paired with a completed publication."""

    state_dir: Path
    checkpoint_path: Path
    state_path: Path
    progress_path: Path


def _state_layout(
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> _StateLayout:
    """Bind a fresh replay's journals to its one immutable run directory."""
    arguments = manifest.get("arguments")
    if not isinstance(arguments, Mapping):
        _fail("manifest arguments are missing for state-layout binding")
    if arguments.get("resume_checkpoint") is not None:
        _fail("Task 6 requires a fresh single-directory replay with no resume checkpoint")
    state_dir = run_dir
    checkpoint_path = _regular_file(
        state_dir / "portfolio_checkpoint.json", label="portfolio checkpoint"
    )
    state_path = _regular_file(
        state_dir / "portfolio_state.jsonl", label="portfolio state log"
    )
    progress_path = _regular_file(
        state_dir / "portfolio_progress.jsonl", label="portfolio progress log"
    )
    if checkpoint_path.parent != state_dir or state_path.parent != state_dir or progress_path.parent != state_dir:
        _fail("state evidence paths do not have the exact bound parent")
    if checkpoint_path in {state_path, progress_path}:
        _fail("manifest resume checkpoint collides with a required state journal")
    return _StateLayout(
        state_dir=state_dir,
        checkpoint_path=checkpoint_path,
        state_path=state_path,
        progress_path=progress_path,
    )


def _state_hashes(layout: _StateLayout) -> dict[str, str]:
    """Hash exactly the three regular state artifacts used by the audit."""
    paths = {
        "checkpoint": layout.checkpoint_path,
        "portfolio_progress.jsonl": layout.progress_path,
        "portfolio_state.jsonl": layout.state_path,
    }
    return {name: _sha256(path) for name, path in paths.items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def _jsonl_records(path: Path, *, label: str):
    """Yield JSONL objects without retaining the journal in memory."""
    seen = False
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, start=1):
            seen = True
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise AssertionError(f"{label} line {number} is invalid JSON") from exc
            if not isinstance(value, dict):
                _fail(f"{label} line {number} is not an object")
            yield number, value
    if not seen:
        _fail(f"{label} is empty")


def _as_date_series(frame: pd.DataFrame, column: str, *, label: str) -> pd.Series:
    try:
        values = pd.to_datetime(frame[column], errors="raise").dt.normalize()
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError(f"{label} has invalid {column} dates") from exc
    if values.isna().any():
        _fail(f"{label} has null {column} dates")
    return values


def _strict_bool(frame: pd.DataFrame, column: str, *, label: str) -> pd.Series:
    raw = frame[column]
    if raw.isna().any() or not raw.map(
        lambda value: isinstance(value, bool) or value.__class__.__name__ == "bool_"
    ).all():
        _fail(f"{label}.{column} must contain only JSON booleans")
    return raw.astype(bool)


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        _fail(f"{label} must be a nonnegative integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssertionError(f"{label} must be a nonnegative integer") from exc
    if number != value or number < 0:
        _fail(f"{label} must be a nonnegative integer")
    return number


def _finite_optional(frame: pd.DataFrame, columns: Sequence[str], *, label: str) -> None:
    for column in columns:
        if column not in frame:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        nonblank = frame[column].notna() & frame[column].astype(str).ne("")
        if (nonblank & values.isna()).any() or not values.dropna().map(math.isfinite).all():
            _fail(f"{label}.{column} contains an invalid/non-finite numeric value")


def _same_json(left: object, right: object, *, label: str) -> None:
    if left != right:
        _fail(f"{label} differs")


def _same_serialized_json(left: object, right: object, *, label: str) -> None:
    try:
        left_bytes = json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False)
        right_bytes = json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label} is not stable JSON") from exc
    if left_bytes != right_bytes:
        _fail(f"{label} differs")


def _git_identity(repo_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
            capture_output=True, text=True, timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root, check=True, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError("could not read repository Git identity") from exc
    if status.stdout != "":
        _fail("repository must have exact empty git status --porcelain --untracked-files=all")
    return head.stdout.strip().lower()


def _digest(value: object, *, label: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        _fail(f"{label} must be a lowercase SHA-256")
    return text


def _close_number(left: object, right: object, *, label: str) -> None:
    try:
        lhs = float(left)
        rhs = float(right)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssertionError(f"{label} is not numeric") from exc
    if not math.isfinite(lhs) or not math.isfinite(rhs) or not math.isclose(
        lhs, rhs, rel_tol=1e-12, abs_tol=1e-8
    ):
        _fail(f"{label} differs: {lhs!r} != {rhs!r}")



def _stable_digest(value: object) -> str:
    try:
        payload = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssertionError("fingerprint payload is not stable JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _require_builtin_strategy_identity(value: object) -> None:
    if value != _BUILTIN_STRATEGY_IDENTITY:
        _fail("checkpoint strategy identity is not the exact built-in CANSLIM identity")


def _require_regeneration_binding(
    regeneration: Mapping[str, Any],
    bundle_manifest: Mapping[str, Any],
    *,
    actual_bundle_sha256: str,
    actual_bundle_manifest_sha256: str,
) -> None:
    """Prove the rebuilt bundle, manifest, and Task-4 audit are one publication."""

    if regeneration.get("schema_version") != _REGENERATION_AUDIT_SCHEMA:
        _fail("Task-4 regeneration audit schema is not 3")
    if regeneration.get("status") != "complete":
        _fail("Task-4 regeneration audit is not complete")
    if regeneration.get("correction_git_head") != _REGENERATION_GIT_SHA:
        _fail("Task-4 regeneration producer Git SHA differs")
    bundle_values = {
        str(actual_bundle_sha256).lower(),
        str(regeneration.get("bundle_sha256", "")).lower(),
        str(bundle_manifest.get("bundle_sha256", "")).lower(),
    }
    if bundle_values != {_BUNDLE_SHA256}:
        _fail("corrected bundle SHA does not agree across file, manifest, and audit")
    if regeneration.get("bundle_manifest_sha256") != actual_bundle_manifest_sha256:
        _fail("Task-4 audit does not bind the exact bundle manifest SHA")
    date_contract = {
        "warmup_start": _WARMUP,
        "evaluation_start": _START,
        "data_cutoff": _END,
    }
    if regeneration.get("date_contract") != date_contract:
        _fail("Task-4 regeneration date contract differs")
    metadata = bundle_manifest.get("metadata")
    if not isinstance(metadata, Mapping):
        _fail("bundle manifest metadata is missing")
    if any(metadata.get(key) != value for key, value in date_contract.items()):
        _fail("bundle manifest metadata date contract differs")
    archives = regeneration.get("source_archives_sha256")
    normalized = regeneration.get("normalized_files_sha256")
    if not isinstance(archives, Mapping) or not isinstance(normalized, Mapping):
        _fail("Task-4 audit source hash maps are missing")
    cross_hashes = {
        "fundamentals_submissions_archive_sha256": archives.get("submissions"),
        "fundamentals_companyfacts_archive_sha256": archives.get("companyfacts"),
        "fundamentals_identity_manifest_csv_sha256": regeneration.get(
            "correction_producer_identity_manifest_csv_sha256"
        ),
        "fundamentals_source_sha256": normalized.get("fundamentals.csv"),
        "fundamentals_provenance_sha256": normalized.get(
            "fundamentals_provenance.json"
        ),
    }
    for key, expected in cross_hashes.items():
        if metadata.get(key) != expected:
            _fail(f"Task-4 audit and bundle manifest source SHA differ: {key}")
    validations = regeneration.get("validations")
    counts = regeneration.get("validated_counts")
    if not isinstance(validations, Mapping) or not isinstance(counts, Mapping):
        _fail("Task-4 audit validation facts are missing")
    if validations.get("xom_reviewed_cik") != "0000034088":
        _fail("Task-4 audit does not bind XOM to reviewed CIK 0000034088")
    if validations.get("xom_mapping_basis") != "reviewed_baseline_cik":
        _fail("Task-4 audit XOM mapping basis differs")
    if validations.get("bundle") != "verify_pit_bundle_passed":
        _fail("Task-4 audit does not record successful bundle verification")
    for key, expected in {
        "xom": 209,
        "xom_quarterly": 71,
        "xom_annual": 30,
        "xom_balance": 108,
    }.items():
        if counts.get(key) != expected:
            _fail(f"Task-4 audit XOM row count differs: {key}")


def _require_exact_next_open_outcome(
    outcome: Mapping[str, Any],
    *,
    expected_entry_date: pd.Timestamp,
    exact_bundle_open: object,
    buy_transaction_price: object | None,
) -> None:
    try:
        actual_date = pd.Timestamp(outcome.get("entry_date")).normalize()
    except (TypeError, ValueError) as exc:
        raise AssertionError("entry outcome has an invalid entry date") from exc
    if actual_date != pd.Timestamp(expected_entry_date).normalize():
        _fail("entry outcome is not on the exact next benchmark session")
    actual_open = _finite(outcome.get("entry_open"))
    expected_open = _finite(exact_bundle_open)
    if actual_open is None or expected_open is None or not math.isclose(
        actual_open, expected_open, rel_tol=1e-12, abs_tol=1e-9
    ):
        _fail("entry outcome does not carry the exact next-session Open")
    if outcome.get("outcome") == "entries_executed":
        buy_price = _finite(buy_transaction_price)
        if buy_price is None or not math.isclose(
            actual_open, buy_price, rel_tol=1e-12, abs_tol=1e-9
        ):
            _fail("executed outcome Open differs from the exact BUY transaction price")
    elif buy_transaction_price is not None:
        _fail("rejected entry outcome has an impossible BUY transaction")


def _finite(value: object) -> float | None:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _safe_growth(current: object, previous: object) -> float | None:
    current_number = _finite(current)
    previous_number = _finite(previous)
    if (
        current_number is None
        or previous_number is None
        or previous_number <= 0.0
        or bool(np.isclose(previous_number, 0.0))
    ):
        return None
    growth = (current_number - previous_number) / abs(previous_number)
    return growth if math.isfinite(growth) else None


def _period_date(value: object) -> date | None:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return None if pd.isna(timestamp) else timestamp.date()


def _same_scalar(left: object, right: object) -> bool:
    left_number = _finite(left)
    right_number = _finite(right)
    if left_number is not None or right_number is not None:
        return left_number is not None and right_number is not None and left_number == right_number
    try:
        return bool(pd.isna(left)) and bool(pd.isna(right))
    except (TypeError, ValueError):
        return left == right


def _independent_yoy_matches(
    values: pd.Series,
) -> tuple[tuple[date, object, date | None, object | None], ...]:
    """Match fiscal quarters without importing the production matcher."""

    grouped: dict[date, list[object]] = {}
    for label, value in values.items():
        period = _period_date(label)
        if period is not None:
            grouped.setdefault(period, []).append(value)
    validated: dict[date, tuple[bool, object | None]] = {}
    for period, duplicates in grouped.items():
        first = duplicates[0]
        valid = all(_same_scalar(first, item) for item in duplicates[1:])
        validated[period] = (valid, first if valid else None)

    result: list[tuple[date, object, date | None, object | None]] = []
    periods = sorted(validated, reverse=True)
    for current_period in periods:
        current_valid, current_value = validated[current_period]
        if not current_valid or current_period.year <= 1:
            result.append((current_period, current_value, None, None))
            continue
        prior_year = current_period.year - 1
        target = date(
            prior_year,
            current_period.month,
            min(current_period.day, calendar.monthrange(prior_year, current_period.month)[1]),
        )
        candidates = [
            period
            for period in periods
            if period < current_period
            and abs((period - target).days) <= _FISCAL_TOLERANCE_DAYS
        ]
        if not candidates:
            result.append((current_period, current_value, None, None))
            continue
        distance = min(abs((period - target).days) for period in candidates)
        closest = [
            period for period in candidates if abs((period - target).days) == distance
        ]
        if len(closest) != 1 or not validated[closest[0]][0]:
            result.append((current_period, current_value, None, None))
            continue
        prior_period = closest[0]
        result.append(
            (current_period, current_value, prior_period, validated[prior_period][1])
        )
    return tuple(result)


def _independent_latest_yoy_pair(values: pd.Series) -> tuple[object, object] | None:
    matches = _independent_yoy_matches(values)
    if not matches or matches[0][2] is None:
        return None
    return matches[0][1], matches[0][3]


def _independent_earnings_series(frame: pd.DataFrame) -> pd.Series:
    labels = [str(label) for label in frame.index]
    for needle in ("diluted eps", "basic eps", "net income"):
        for position, label in enumerate(labels):
            if needle in label.casefold():
                selected = frame.iloc[position]
                selected.name = frame.index[position]
                return selected
    return pd.Series(dtype=float)


def _independent_evaluate_c(
    quarterly_income: pd.DataFrame,
) -> tuple[float, float | None]:
    earnings = _independent_earnings_series(quarterly_income)
    if earnings.empty:
        return 0.0, None
    matches = _independent_yoy_matches(earnings)
    growths = [
        _safe_growth(current, previous) if prior_period is not None else None
        for _current_period, current, prior_period, previous in matches
    ]
    if not growths or growths[0] is None:
        return 0.0, None
    current_growth = growths[0]
    growth_score = float(np.clip(current_growth / 0.25, 0, 2) / 2)
    recent = [value for value in growths[:3] if value is not None]
    consistency = (
        sum(value >= 0.25 for value in recent) / len(recent) if recent else 0.0
    )
    valid = [value for value in growths[:4] if value is not None]
    acceleration = (
        sum(valid[index] > valid[index + 1] for index in range(len(valid) - 1))
        / (len(valid) - 1)
        if len(valid) >= 2
        else 0.5
    )
    score = float(np.clip(0.60 * growth_score + 0.20 * consistency + 0.20 * acceleration, 0, 1))
    return score, current_growth


def _independent_evaluate_a(
    annual_income: pd.DataFrame,
    balance_sheet: pd.DataFrame,
) -> tuple[float, float | None, float | None]:
    """Evaluate A independently with explicit Diluted/Basic/Net Income priority."""

    earnings = _independent_earnings_series(annual_income).sort_index()
    if len(earnings) < 2:
        return 0.0, None, None
    growths = [
        _safe_growth(earnings.iloc[index], earnings.iloc[index - 1])
        for index in range(len(earnings) - 1, 0, -1)
    ]
    if not growths or growths[0] is None:
        return 0.0, None, None
    annual_growth = growths[0]
    growth_score = float(np.clip(annual_growth / 0.25, 0, 2) / 2)
    recent = [value for value in growths[:3] if value is not None]
    consistency = (
        sum(value >= 0.25 for value in recent) / len(recent) if recent else 0.0
    )
    roe: float | None = None
    if not annual_income.empty and not balance_sheet.empty:
        net_income = pd.Series(dtype=float)
        for position, label in enumerate(annual_income.index.astype(str)):
            if "net income" in label.casefold():
                net_income = pd.to_numeric(
                    annual_income.iloc[position], errors="coerce"
                ).dropna().sort_index()
                break
        equity = pd.Series(dtype=float)
        equity_needles = (
            "stockholders equity",
            "stockholders' equity",
            "shareholders equity",
            "shareholders' equity",
            "total equity",
            "common stock equity",
        )
        for position, label in enumerate(balance_sheet.index.astype(str)):
            normalized = label.casefold().replace("’", "'")
            if any(needle in normalized for needle in equity_needles):
                equity = pd.to_numeric(
                    balance_sheet.iloc[position], errors="coerce"
                ).dropna().sort_index()
                break
        if not net_income.empty and not equity.empty:
            latest_net = _finite(net_income.iloc[-1])
            latest_equity = _finite(equity.iloc[-1])
            if latest_net is not None and latest_equity is not None and latest_equity > 0:
                candidate = latest_net / latest_equity
                roe = candidate if math.isfinite(candidate) else None
    roe_score = float(np.clip(roe / 0.17, 0, 2) / 2) if roe is not None else 0.0
    score = float(
        np.clip(
            0.50 * growth_score + 0.30 * consistency + 0.20 * roe_score,
            0,
            1,
        )
    )
    return score, annual_growth, roe


def _require_reviewed_cah_invariant(
    cah_rows: pd.DataFrame,
    cah_as_of: Mapping[str, Any],
) -> None:
    """Prove the reviewed CAH correction is present rather than vacuously absent."""

    if not isinstance(cah_rows, pd.DataFrame) or "statement_type" not in cah_rows:
        _fail("CAH corrected fundamental rows are unavailable")
    counts = cah_rows["statement_type"].value_counts().to_dict()
    if len(cah_rows) != sum(_CAH_STATEMENT_COUNTS.values()) or counts != _CAH_STATEMENT_COUNTS:
        _fail("CAH corrected fundamental rows do not have the reviewed statement split")
    frames: dict[str, pd.DataFrame] = {}
    for key in ("quarterly_income", "annual_income", "balance_sheet"):
        frame = cah_as_of.get(key)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            _fail(f"CAH 2021-05-13 lacks reviewed {key} facts")
        frames[key] = frame
    quarterly_earnings = _independent_earnings_series(frames["quarterly_income"])
    annual_earnings = _independent_earnings_series(frames["annual_income"])
    if not any(_finite(value) is not None for value in quarterly_earnings) or not any(
        _finite(value) is not None for value in annual_earnings
    ):
        _fail("CAH 2021-05-13 lacks reviewed finite earnings facts")

    c_score, c_growth = _independent_evaluate_c(frames["quarterly_income"])
    a_score, a_growth, roe = _independent_evaluate_a(
        frames["annual_income"], frames["balance_sheet"]
    )
    if _finite(c_score) is None or _finite(a_score) is None:
        _fail("CAH 2021-05-13 C/A scores are not finite")
    if c_growth is not None and _finite(c_growth) is None:
        _fail("CAH 2021-05-13 current earnings growth is not finite-or-missing")
    if _finite(a_growth) is None or _finite(roe) is None:
        _fail("CAH 2021-05-13 reviewed annual growth/ROE facts are unavailable or non-finite")


@dataclass(frozen=True)
class _IndependentEntryFacts:
    event_close: float | None
    prior_close: float | None
    event_volume: float | None
    prior_average_volume_50: float | None
    pivot: float | None
    volume_ratio: float | None
    extension: float | None
    price_advanced: bool
    has_volume_surge: bool
    in_buy_zone: bool
    eligible: bool
    blocking_reasons: tuple[str, ...]


def _normalized_unique_index(frame: pd.DataFrame, *, label: str) -> pd.DatetimeIndex:
    try:
        index = pd.DatetimeIndex(frame.index).normalize()
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label} has an invalid session index") from exc
    if index.has_duplicates or not index.is_monotonic_increasing:
        _fail(f"{label} session index must be unique and increasing")
    return index


def _independent_entry_facts(
    history: pd.DataFrame,
    event_session: object,
) -> _IndependentEntryFacts:
    """Build the entry facts from a required exact event-session bar."""

    if not isinstance(history, pd.DataFrame) or not {"Close", "Volume"}.issubset(history):
        _fail("price history lacks Close/Volume for independent entry facts")
    index = _normalized_unique_index(history, label="price history")
    when = pd.Timestamp(event_session).normalize()
    positions = np.flatnonzero(index == when)
    if len(positions) != 1:
        _fail("price history lacks the exact completed-session bar")
    return _independent_entry_facts_at_position(history, int(positions[0]))


def _independent_entry_facts_at_position(
    history: pd.DataFrame,
    position: int,
) -> _IndependentEntryFacts:
    close_count = position + 1
    volume_count = position + 1
    close_start = max(0, close_count - 253)
    volume_start = max(0, volume_count - 51)
    closes = [
        _finite(value)
        for value in history["Close"].iloc[close_start : position + 1]
    ]
    volumes = [
        _finite(value)
        for value in history["Volume"].iloc[volume_start : position + 1]
    ]
    reasons: list[str] = []
    event_close = closes[-1] if closes else None
    prior_close = closes[-2] if len(closes) >= 2 else None
    event_volume = volumes[-1] if volumes else None
    pivot: float | None = None
    prior_average: float | None = None
    volume_ratio: float | None = None
    extension: float | None = None

    if close_count < 2:
        reasons.append("insufficient_close_history")
    else:
        relevant = closes[-253:]
        if any(value is None for value in relevant):
            reasons.append("non_finite_close_input")
        else:
            finite_closes = [float(value) for value in relevant if value is not None]
            pivot = max(finite_closes[:-1])
            event_close = finite_closes[-1]
            prior_close = finite_closes[-2]
            if pivot <= 0:
                reasons.append("non_positive_pivot")
    if volume_count < 51:
        reasons.append("insufficient_prior_volume_history")
    else:
        relevant_volumes = volumes[-51:]
        if any(value is None for value in relevant_volumes):
            reasons.append("non_finite_volume_input")
        else:
            finite_volumes = [
                float(value) for value in relevant_volumes if value is not None
            ]
            prior_average = sum(finite_volumes[:-1]) / 50.0
            event_volume = finite_volumes[-1]
            if prior_average <= 0:
                reasons.append("non_positive_prior_average_volume")
            else:
                volume_ratio = event_volume / prior_average

    price_advanced = bool(
        event_close is not None and prior_close is not None and event_close > prior_close
    )
    has_volume_surge = bool(volume_ratio is not None and volume_ratio >= 1.30)
    in_buy_zone = bool(
        event_close is not None
        and pivot is not None
        and pivot > 0
        and pivot <= event_close <= pivot * 1.05
    )
    if event_close is not None and prior_close is not None and not price_advanced:
        reasons.append("close_not_above_prior_close")
    if volume_ratio is not None and not has_volume_surge:
        reasons.append("volume_ratio_below_threshold")
    if event_close is not None and pivot is not None and pivot > 0:
        extension = event_close / pivot - 1.0
        if event_close < pivot:
            reasons.append("close_below_pivot")
        elif event_close > pivot * 1.05:
            reasons.append("close_above_buy_zone")
    blockers = tuple(reasons)
    return _IndependentEntryFacts(
        event_close,
        prior_close,
        event_volume,
        prior_average,
        pivot,
        volume_ratio,
        extension,
        price_advanced,
        has_volume_surge,
        in_buy_zone,
        not blockers,
        blockers,
    )


def _independent_rs_snapshot(
    all_closes: pd.DataFrame,
    event_session: object,
    *,
    eligible_tickers: set[str],
) -> dict[str, float]:
    """Rank only peers with a finite close on the exact evaluation session."""

    index = _normalized_unique_index(all_closes, label="RS close matrix")
    when = pd.Timestamp(event_session).normalize()
    positions = np.flatnonzero(index == when)
    if len(positions) != 1:
        _fail("RS close matrix lacks the exact completed-session row")
    position = int(positions[0])
    frame = all_closes.iloc[: position + 1]
    event = frame.iloc[-1]
    performance: dict[str, float] = {}
    for raw_symbol in frame.columns:
        symbol = str(raw_symbol).upper()
        if symbol not in eligible_tickers or _finite(event[raw_symbol]) is None:
            continue
        series = pd.to_numeric(frame[raw_symbol], errors="coerce").dropna()
        if len(series) >= 260:
            anchors = (series.iloc[-1], series.iloc[-65], series.iloc[-130], series.iloc[-195], series.iloc[-260])
            if any(_finite(value) is None or float(value) <= 0 for value in anchors[1:]):
                continue
            weighted = (
                0.40 * (anchors[0] / anchors[1] - 1.0)
                + 0.20 * (anchors[1] / anchors[2] - 1.0)
                + 0.20 * (anchors[2] / anchors[3] - 1.0)
                + 0.20 * (anchors[3] / anchors[4] - 1.0)
            )
        elif len(series) >= 60 and float(series.iloc[0]) > 0:
            raw_return = (float(series.iloc[-1]) - float(series.iloc[0])) / float(
                series.iloc[0]
            )
            base = 1.0 + raw_return
            if base <= 0:
                continue
            weighted = base ** (252.0 / len(series)) - 1.0
        else:
            continue
        if math.isfinite(float(weighted)):
            performance[symbol] = float(weighted)
    if not performance:
        return {}
    values = pd.Series(performance, dtype=float)
    ranks = values.rank(pct=True)
    return {
        str(symbol): float(rank * 98.0 + 1.0)
        for symbol, rank in ranks.items()
    }


def _independent_evaluate_n(
    quarterly_income: pd.DataFrame,
    proximity_to_high: object,
) -> tuple[float, float | None]:
    revenue_growth: float | None = None
    if isinstance(quarterly_income, pd.DataFrame) and not quarterly_income.empty:
        for position, label in enumerate(quarterly_income.index.astype(str)):
            if "revenue" in label.casefold():
                pair = _independent_latest_yoy_pair(quarterly_income.iloc[position])
                if pair is not None:
                    revenue_growth = _safe_growth(*pair)
                break
    proximity = _finite(proximity_to_high)
    if proximity is not None and proximity > 0:
        if proximity >= 0.98:
            proximity_score = 1.0
        elif proximity >= 0.90:
            proximity_score = (proximity - 0.90) / 0.08
        elif proximity >= 0.75:
            proximity_score = (proximity - 0.75) / 0.15 * 0.3
        else:
            proximity_score = 0.0
    else:
        proximity_score = 0.0
    components: list[tuple[float, float]] = []
    if revenue_growth is not None:
        revenue_score = float(np.clip(revenue_growth / 0.25, 0, 2) / 2)
        components.append((0.5, revenue_score))
    if proximity is not None and proximity > 0:
        components.append((0.5, proximity_score))
    if not components:
        return 0.0, revenue_growth
    weight = sum(item[0] for item in components)
    score = sum(component_weight / weight * value for component_weight, value in components)
    return float(np.clip(score, 0, 1)), revenue_growth


def _independent_n_score(
    revenue_growth: float | None,
    proximity: float,
) -> float:
    if proximity >= 0.98:
        proximity_score = 1.0
    elif proximity >= 0.90:
        proximity_score = (proximity - 0.90) / 0.08
    elif proximity >= 0.75:
        proximity_score = (proximity - 0.75) / 0.15 * 0.3
    else:
        proximity_score = 0.0
    if revenue_growth is None:
        return float(np.clip(proximity_score, 0, 1))
    revenue_score = float(np.clip(revenue_growth / 0.25, 0, 2) / 2)
    return float(np.clip(0.5 * revenue_score + 0.5 * proximity_score, 0, 1))


def _independent_power_gap(
    price_history: pd.DataFrame,
    *,
    lookback_days: int = 10,
) -> tuple[bool, dict[str, float]]:
    if len(price_history) < lookback_days + 50:
        return False, {}
    required = {"Open", "Close", "Volume"}
    if not required.issubset(price_history):
        return False, {}
    bounded = price_history.tail(lookback_days + 50)
    recent = bounded.tail(lookback_days + 1)
    volumes = pd.to_numeric(bounded["Volume"], errors="coerce")
    average = _finite(volumes.iloc[: -(lookback_days + 1)].mean())
    if average is None or average <= 0:
        return False, {}
    opens = pd.to_numeric(recent["Open"], errors="coerce")
    closes = pd.to_numeric(recent["Close"], errors="coerce")
    recent_volumes = pd.to_numeric(recent["Volume"], errors="coerce")
    for index in range(len(recent) - 1, 0, -1):
        prior_close = _finite(closes.iloc[index - 1])
        event_open = _finite(opens.iloc[index])
        event_volume = _finite(recent_volumes.iloc[index])
        if prior_close is None or prior_close <= 0 or event_open is None or event_volume is None:
            continue
        gap = (event_open - prior_close) / prior_close
        ratio = event_volume / average
        if gap >= 0.02 and ratio >= 1.5:
            return True, {
                "gap_size": gap,
                "volume_ratio": ratio,
                "gap_price": event_open,
                "days_ago": len(recent) - index - 1,
            }
    return False, {}


def _independent_evaluate_s(
    price_history: pd.DataFrame,
    *,
    prior_average_volume_50: object,
    shares_outstanding: object,
) -> tuple[float, bool, dict[str, float]]:
    """Recompute S and its diagnostic PEG from the exact history slice."""

    if not {"Close", "Volume"}.issubset(price_history) or price_history.empty:
        return 0.0, False, {}
    closes = pd.to_numeric(price_history["Close"].tail(252), errors="coerce")
    volumes = pd.to_numeric(price_history["Volume"].tail(50), errors="coerce")
    current = _finite(closes.iloc[-1])
    event_volume = _finite(volumes.iloc[-1])
    average = _finite(prior_average_volume_50)
    if current is None or event_volume is None:
        return 0.0, False, {}
    high = _finite(closes.max())
    proximity = current / high if high is not None and high > 0 else 0.0
    shares = _finite(shares_outstanding)
    if shares is None or shares <= 0:
        float_score = 0.5
    else:
        millions = shares / 1_000_000.0
        if millions < 50:
            float_score = 1.0
        elif millions < 200:
            float_score = 0.85
        elif millions < 500:
            float_score = 0.65
        elif millions < 1000:
            float_score = 0.4
        else:
            float_score = 0.2
    recent = price_history.tail(min(50, len(price_history)))
    recent_closes = pd.to_numeric(recent["Close"], errors="coerce")
    recent_volumes = pd.to_numeric(recent["Volume"], errors="coerce")
    changes = recent_closes.diff()
    up_volume = _finite(recent_volumes.loc[changes > 0].mean()) or 0.0
    down_volume = _finite(recent_volumes.loc[changes < 0].mean())
    if down_volume is None:
        down_volume = 1.0
    ratio = 2.0 if down_volume == 0 else min(up_volume / down_volume, 3.0)
    if ratio >= 1.5:
        up_down_score = 1.0
    elif ratio >= 1.0:
        up_down_score = (ratio - 1.0) / 0.5
    else:
        up_down_score = max(ratio - 0.5, 0.0) / 0.5 * 0.3
    price_up = len(closes) >= 2 and current > float(closes.iloc[-2])
    volume_ratio = event_volume / average if average is not None and average > 0 else 0.0
    volume_score = min(volume_ratio / 1.3, 1.0) if average is not None and average > 0 else 0.0
    is_breakout = proximity >= 0.95
    proximity_score = float(np.clip((proximity - 0.85) / 0.10, 0, 1))
    breakout_score = 1.0 if is_breakout else proximity_score
    surge_breakout = 0.5 * volume_score + 0.5 * breakout_score
    has_gap, gap_details = _independent_power_gap(price_history)
    if has_gap and proximity < 0.85:
        has_gap, gap_details = False, {}
    score = (
        0.25 * float_score
        + 0.25 * up_down_score
        + 0.30 * surge_breakout
        + 0.20 * (1.0 if has_gap else 0.0)
    )
    # The production S score counts volume strength even on a down day, but
    # its reported surge boolean requires an advancing close. The canonical
    # entry facts enforce the advancing-session requirement independently.
    del price_up
    return float(np.clip(score, 0, 1)), has_gap, gap_details


def _independent_evaluate_i(
    company_info: Mapping[str, Any],
) -> tuple[float, bool]:
    held = _finite(company_info.get("held_percent_institutions"))
    current = company_info.get("institution_count")
    previous = company_info.get("prev_institution_count")
    if (current is None) != (previous is None):
        _fail("institutional current/prior holder counts must be an atomic pair")
    components: list[tuple[float, float]] = []
    if held is not None:
        if held < 0.10:
            level = held / 0.10 * 0.3
        elif held < 0.30:
            level = 0.3 + (held - 0.10) / 0.20 * 0.4
        elif held < 0.60:
            level = 0.7 + (held - 0.30) / 0.30 * 0.3
        elif held < 0.80:
            level = 1.0 - (held - 0.60) / 0.20 * 0.15
        elif held < 0.90:
            level = 0.85 - (held - 0.80) / 0.10 * 0.25
        else:
            level = max(0.6 - (held - 0.90) / 0.10 * 0.3, 0.3)
        components.append((0.6, level))
    if current is not None and previous is not None:
        current_number = _finite(current)
        previous_number = _finite(previous)
        if current_number is None or previous_number is None:
            _fail("institutional holder counts must be finite")
        if previous_number <= 0:
            trend = 0.5
        else:
            change = (current_number - previous_number) / previous_number
            if change >= 0.10:
                trend = 1.0
            elif change >= 0.03:
                trend = 0.7 + (change - 0.03) / 0.07 * 0.3
            elif change >= 0:
                trend = 0.5 + change / 0.03 * 0.2
            elif change >= -0.05:
                trend = 0.5 + change / 0.05 * 0.2
            else:
                trend = max(0.3 + (change + 0.05) / 0.15 * 0.2, 0.1)
        components.append((0.4, trend))
    if not components:
        return 0.5, False
    total_weight = sum(weight for weight, _value in components)
    score = sum(weight / total_weight * value for weight, value in components)
    return float(np.clip(score, 0, 1)), True


def _independent_entry_decision(
    facts: _IndependentEntryFacts,
    *,
    current_growth: object,
    annual_growth: object,
    rs_score: object,
    composite_score: object,
) -> tuple[bool, tuple[str, ...], float | None, float | None, float | None, float | None]:
    values = (
        _finite(current_growth),
        _finite(annual_growth),
        _finite(rs_score),
        _finite(composite_score),
    )
    reasons = list(facts.blocking_reasons)
    for value, threshold, unavailable, below in (
        (values[0], 0.25, "current_growth_unavailable", "current_growth_below_threshold"),
        (values[1], 0.25, "annual_growth_unavailable", "annual_growth_below_threshold"),
        (values[2], 80.0, "rs_score_unavailable", "rs_score_below_threshold"),
        (values[3], 70.0, "composite_score_unavailable", "composite_score_below_threshold"),
    ):
        if value is None:
            reasons.append(unavailable)
        elif value < threshold:
            reasons.append(below)
    blockers = tuple(reasons)
    return (not blockers, blockers, *values)


def _independent_composite_scores(
    *,
    c_score: float,
    a_score: float,
    n_score: float,
    s_score: float,
    l_score: float,
    i_score: float,
    m_score: float,
    institutional_data_available: bool,
) -> tuple[float, float, float]:
    weights = {"C": 0.20, "A": 0.15, "N": 0.10, "S": 0.10, "L": 0.20, "I": 0.10, "M": 0.15}
    if not institutional_data_available:
        weights["I"] = 0.0
        for key in weights:
            if key != "I":
                weights[key] /= 0.90
    components = {
        "C": c_score,
        "A": a_score,
        "N": n_score,
        "S": s_score,
        "L": l_score,
        "I": i_score,
        "M": m_score,
    }
    total = sum(weights[key] * components[key] for key in weights) * 100.0
    entry_weight = sum(weight for key, weight in weights.items() if key != "M")
    entry = (
        sum(weights[key] * components[key] for key in weights if key != "M")
        * 100.0
        / entry_weight
    )
    technical_weight = 0.10 + 0.10 + 0.20 + 0.15
    technical = (
        0.10 * n_score + 0.10 * s_score + 0.20 * l_score + 0.15 * m_score
    ) / technical_weight * 100.0
    return float(total), float(entry), float(technical)



def _add_repo_to_path(repo_root: Path) -> None:
    text = str(repo_root)
    if text not in sys.path:
        sys.path.insert(0, text)


def _audit_task2_bindings(bundle: Any, args: argparse.Namespace) -> dict[str, Any]:
    provenance = _json(args.fundamentals_provenance, label="fundamentals provenance")
    source_coverage = _json(args.fundamentals_coverage, label="fundamentals coverage")
    if _sha256(args.fundamentals_provenance) != bundle.metadata["fundamentals_provenance_sha256"]:
        _fail("fundamentals provenance does not match the PIT bundle")
    bindings = {
        "fundamentals_coverage_sha256": args.fundamentals_coverage,
        "security_master_sha256": args.security_master,
        "security_master_exclusions_sha256": args.security_master_exclusions,
    }
    for field, path in bindings.items():
        if _digest(provenance.get(field), label=field) != _sha256(path):
            _fail(f"fundamentals provenance does not bind {field}")
    if provenance.get("start_date") != _WARMUP or provenance.get("end_date") != _END:
        _fail("fundamentals provenance date contract differs")
    if provenance.get("source") != "SEC EDGAR official bulk archives":
        _fail("fundamentals provenance source differs")
    if provenance.get("public_date_rule") != _PUBLIC_DATE_RULE:
        _fail("fundamentals provenance public-date rule differs")
    if not isinstance(provenance.get("archive_manifest"), dict) or not provenance["archive_manifest"]:
        _fail("fundamentals provenance lacks archive retrieval facts")
    for field in (
        "submissions_archive_sha256", "companyfacts_archive_sha256",
        "identity_manifest_csv_sha256",
    ):
        if bundle.metadata[f"fundamentals_{field}"] != _digest(
            provenance.get(field), label=field
        ):
            _fail(f"fundamentals source digest differs from bundle: {field}")

    master = pd.read_csv(args.security_master, dtype=str, keep_default_na=False)
    exclusions = pd.read_csv(args.security_master_exclusions, dtype=str, keep_default_na=False)
    if tuple(master.columns) != _MASTER_COLUMNS or tuple(exclusions.columns) != _EXCLUSION_COLUMNS:
        _fail("security-master/exclusion header differs from production schema")
    master["ticker"] = master["ticker"].str.upper()
    exclusions["ticker"] = exclusions["ticker"].str.upper()
    if master.empty or master[["ticker", "cik", "mapping_basis"]].eq("").any().any():
        _fail("security master has blank required fields")
    if exclusions[["ticker", "reason"]].eq("").any().any():
        _fail("security-master exclusions have blank closed reasons")
    if master.duplicated(["ticker", "cik", "first_membership_date", "last_membership_date"]).any():
        _fail("security master has duplicate interval identities")
    if any(len(cik) != 10 or not cik.isdigit() for cik in master["cik"]):
        _fail("security master has an invalid CIK")
    xom_master = master.loc[master["ticker"].eq("XOM")]
    if len(xom_master) != 1 or xom_master.iloc[0]["cik"] != "0000034088":
        _fail("security master does not bind XOM exactly once to reviewed CIK 0000034088")
    if xom_master.iloc[0]["mapping_basis"] != "reviewed_baseline_cik":
        _fail("security master XOM mapping basis is not reviewed_baseline_cik")
    selected_fundamentals = pd.read_csv(
        args.fundamentals_csv,
        dtype={"ticker": str, "statement_type": str},
        keep_default_na=True,
    )
    selected_fundamentals = selected_fundamentals.loc[
        selected_fundamentals["ticker"].isin(["CAH", "XOM"])
    ]
    xom = selected_fundamentals.loc[selected_fundamentals["ticker"].eq("XOM")]
    xom_counts = xom["statement_type"].value_counts().to_dict()
    if len(xom) != 209 or xom_counts != {
        "quarterly": 71,
        "annual": 30,
        "balance": 108,
    }:
        _fail("corrected XOM fundamental rows do not have the reviewed 71/30/108 split")
    numeric_columns = [
        column
        for column in selected_fundamentals.columns
        if column
        not in {"ticker", "statement_type", "period_end", "public_date"}
    ]
    for column in numeric_columns:
        values = pd.to_numeric(selected_fundamentals[column], errors="coerce")
        present = selected_fundamentals[column].notna()
        if values.loc[present].isna().any() or (~np.isfinite(values.loc[present])).any():
            _fail(f"CAH/XOM normalized fundamentals contain a non-finite value: {column}")
    cah_as_of = bundle.fundamentals_as_of("CAH", pd.Timestamp("2021-05-13"))
    _require_reviewed_cah_invariant(
        selected_fundamentals.loc[selected_fundamentals["ticker"].eq("CAH")],
        cah_as_of,
    )
    union = {event.ticker for event in bundle.membership.events}
    resolved = set(master["ticker"])
    excluded = set(exclusions["ticker"])
    if resolved & excluded or resolved | excluded != union:
        _fail("security-master resolved/excluded accounting differs from bundle membership")
    expected_counts = {
        "membership_union_symbol_count": len(union),
        "resolved_symbol_count": len(resolved),
        "explicitly_excluded_symbol_count": len(excluded),
    }
    for field, expected in expected_counts.items():
        if _nonnegative_int(source_coverage.get(field), label=f"coverage {field}") != expected:
            _fail(f"fundamentals coverage count differs: {field}")
    accounted_pct = len(resolved | excluded) / len(union) * 100.0
    resolved_pct = len(resolved) / len(union) * 100.0
    _close_number(
        source_coverage.get("resolved_or_closed_exclusion_percentage"), accounted_pct,
        label="accounted CIK coverage",
    )
    _close_number(
        source_coverage.get("resolved_cik_percentage"), resolved_pct,
        label="resolved CIK coverage",
    )
    return {
        **expected_counts,
        "resolved_cik_percentage": resolved_pct,
        "resolved_or_closed_exclusion_percentage": accounted_pct,
        "security_master": master.to_dict(orient="records"),
        "exclusions": exclusions.to_dict(orient="records"),
        "filed_date_fallback_count": int(source_coverage.get("filed_date_fallback_count", 0)),
        "filed_date_fallback_unique_count": int(
            source_coverage.get("filed_date_fallback_unique_count", 0)
        ),
        "task2_coverage": source_coverage,
        "task2_artifact_sha256": {
            field: _sha256(path) for field, path in bindings.items()
        },
    }


def _expected_result_config(
    bundle: Any,
    bundle_manifest: Mapping[str, Any],
    *,
    rs_universe_count: int,
) -> dict[str, Any]:
    tickers = [symbol for symbol in bundle.symbols() if symbol != _BENCHMARK]
    return {
        "tickers": tickers,
        "candidate_universe_count": len(tickers),
        "rs_universe_count": rs_universe_count,
        "benchmark_symbol": _BENCHMARK,
        "max_positions": None,
        "require_bullish_market": False,
        "use_stateful_regime_gate": False,
        "cash_deployment_threshold_pct": None,
        "position_size_pct": 0.125,
        "position_risk_pct": 0.01,
        "stop_loss_pct": 0.08,
        "ma_exit_period": 21,
        "ma_consecutive": 2,
        "signal_every_n_days": 1,
        "min_canslim_score": 70.0,
        "min_rs_score": 80.0,
        "requested_min_canslim_score": 70.0,
        "requested_min_rs_score": 80.0,
        "entry_threshold_requests_advisory_only": True,
        "min_technical_score": 70.0,
        "entry_contract_min_current_growth": 0.25,
        "entry_contract_min_annual_growth": 0.25,
        "entry_contract_min_rs_score": 80.0,
        "entry_contract_min_composite_score": 70.0,
        "entry_contract_min_volume_ratio": 1.3,
        "entry_contract_max_buy_zone_extension": 0.05,
        "technical_only": False,
        "data_mode": "point_in_time",
        "pit_bundle_sha256": bundle.sha256,
        "pit_data_cutoff": _END,
        "pit_manifest": dict(bundle_manifest),
        "take_profit_pct": 0.4,
        "scale_out_fraction": 0.5,
        "stagnation_days": 20,
        "stagnation_threshold_pct": 0.05,
        "breakeven_trigger_pct": 0.08,
        "industry_group_top_n": 20,
        "start_date": _START,
        "end_date": _END,
    }


def _audit_bundle(args: argparse.Namespace) -> dict[str, Any]:
    """Use the production read-only bundle reader and exact manifest comparer."""
    _add_repo_to_path(args.repo_root)
    from core.pit_data import PITDataBundle, sha256_file  # pylint: disable=import-outside-toplevel
    from config import settings  # pylint: disable=import-outside-toplevel

    inputs = {
        "membership_csv": args.membership_csv,
        "prices_csv": args.prices_csv,
        "fundamentals_csv": args.fundamentals_csv,
        "membership_provenance": args.membership_provenance,
        "prices_provenance": args.prices_provenance,
        "fundamentals_provenance": args.fundamentals_provenance,
    }
    with PITDataBundle(args.pit_bundle, expected_sha256=args.expected_bundle_sha256) as bundle:
        actual = bundle.manifest()
        for name, path in inputs.items():
            expected_key = f"{name}_sha256".replace("_csv_sha256", "_source_sha256")
            if name.endswith("_provenance"):
                expected_key = f"{name}_sha256"
            if sha256_file(path) != bundle.metadata[expected_key]:
                _fail(f"bundle metadata hash mismatch for {name}")
        identity_transition_contract = bundle.load_price_identity_transition_contract(
            args.prices_provenance
        )
        task2 = _audit_task2_bindings(bundle, args)
        all_closes = bundle.fetch_closes(
            bundle.symbols(), pd.Timestamp(_START), pd.Timestamp(_END)
        )
        spy = all_closes[_BENCHMARK].dropna()
        sessions = pd.DatetimeIndex(spy.index).normalize()
        if len(sessions) != _SESSION_COUNT or sessions.has_duplicates or not sessions.is_monotonic_increasing:
            _fail("immutable bundle does not contain the exact 1255 ordered SPY sessions")
        expected_config = _expected_result_config(
            bundle, actual, rs_universe_count=len(all_closes.columns)
        )
    expected = _json(args.bundle_manifest, label="bundle manifest")
    normalized = dict(actual)
    try:
        normalized["symbols"] = normalized.pop("symbol_count")
    except KeyError as exc:
        raise AssertionError("production bundle manifest lacks symbol_count") from exc
    _same_json(expected, normalized, label="bundle manifest")
    regeneration = _json(args.regeneration_audit, label="Task-4 regeneration audit")
    _require_regeneration_binding(
        regeneration,
        expected,
        actual_bundle_sha256=_sha256(args.pit_bundle),
        actual_bundle_manifest_sha256=_sha256(args.bundle_manifest),
    )
    normalized_paths = {
        "security_master.csv": args.security_master,
        "security_master_exclusions.csv": args.security_master_exclusions,
        "fundamentals.csv": args.fundamentals_csv,
        "fundamentals_provenance.json": args.fundamentals_provenance,
        "fundamentals_coverage.json": args.fundamentals_coverage,
    }
    normalized_hashes = regeneration.get("normalized_files_sha256")
    if not isinstance(normalized_hashes, Mapping):
        _fail("Task-4 regeneration audit normalized hashes are missing")
    for name, path in normalized_paths.items():
        if normalized_hashes.get(name) != _sha256(path):
            _fail(f"Task-4 regeneration audit normalized SHA differs: {name}")
    producer_sources = {
        "fetch_sec_pit_fundamentals_py_sha256": args.repo_root
        / "fetch_sec_pit_fundamentals.py",
        "build_pit_bundle_py_sha256": args.repo_root / "build_pit_bundle.py",
        "verify_pit_bundle_py_sha256": args.repo_root / "verify_pit_bundle.py",
        "regeneration_driver_ps1_sha256": args.repo_root
        / ".superpowers"
        / "sdd"
        / "2026-08-23-canonical-canslim-entry"
        / "task-4-regenerate.ps1",
    }
    for field, path in producer_sources.items():
        if regeneration.get(field) != _sha256(
            _regular_file(path, label=f"Task-4 producer source {field}")
        ):
            _fail(f"Task-4 regeneration producer source SHA differs: {field}")
    if type(settings.ENABLE_EVICTION) is not bool:
        _fail("bound engine enable_eviction setting is not a boolean")
    return {
        "manifest": actual,
        "sessions": sessions,
        "all_closes": all_closes,
        "task2": task2,
        "expected_config": expected_config,
        "prices_provenance": _json(args.prices_provenance, label="prices provenance"),
        "identity_transition_contract": identity_transition_contract,
        "enable_eviction": settings.ENABLE_EVICTION,
        "regeneration_audit": regeneration,
    }


def _expected_checkpoint_fingerprint(
    manifest: Mapping[str, Any],
    bundle_context: Mapping[str, Any],
) -> str:
    """Independently serialize the exact engine-v3 fingerprint payload."""
    config = bundle_context["expected_config"]
    contract = bundle_context["identity_transition_contract"]
    candidates = config["tickers"]
    if not isinstance(candidates, list) or any(not isinstance(item, str) for item in candidates):
        _fail("bound result configuration has invalid candidate tickers")
    fingerprint_payload = {
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "bundle_sha256": bundle_context["manifest"]["bundle_sha256"],
        "code_identity": manifest["git_head"],
        "strategy_identity": _BUILTIN_STRATEGY_IDENTITY,
        "identity_prices_provenance_sha256": contract.prices_provenance_sha256,
        "identity_request_contracts_sha256": contract.request_contracts_sha256,
        "start_date": _START,
        "end_date": _END,
        "benchmark": _BENCHMARK,
        "universe": sorted(str(item).upper() for item in [*candidates, _BENCHMARK]),
        "initial_capital": 100_000.0,
        "max_positions": config["max_positions"],
        "position_size_pct": config["position_size_pct"],
        "position_risk_pct": config["position_risk_pct"],
        "stop_loss_pct": config["stop_loss_pct"],
        "ma_exit_period": config["ma_exit_period"],
        "ma_consecutive": config["ma_consecutive"],
        "signal_every_n_days": config["signal_every_n_days"],
        "min_canslim_score": config["min_canslim_score"],
        "min_rs_score": config["min_rs_score"],
        "min_technical_score": config["min_technical_score"],
        "require_bullish_market": config["require_bullish_market"],
        "use_stateful_regime_gate": config["use_stateful_regime_gate"],
        "cash_deployment_threshold_pct": config["cash_deployment_threshold_pct"],
        "technical_only": config["technical_only"],
        "take_profit_pct": config["take_profit_pct"],
        "scale_out_fraction": config["scale_out_fraction"],
        "stagnation_days": config["stagnation_days"],
        "stagnation_threshold_pct": config["stagnation_threshold_pct"],
        "breakeven_trigger_pct": config["breakeven_trigger_pct"],
        "enable_eviction": bundle_context["enable_eviction"],
    }
    try:
        payload = json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise AssertionError("bound engine fingerprint payload is not stable JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _audit_checkpoint(
    layout: _StateLayout,
    manifest: Mapping[str, Any],
    bundle_context: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = _json(layout.checkpoint_path, label="portfolio checkpoint")
    expected_fields = {
        "schema_version", "fingerprint", "code_identity", "strategy_identity",
        "next_day_index",
        "total_days", "state_log_offset", "completed", "equity", "open_positions",
        "trades", "execution_diagnostics", "entry_outcome_schema_version",
        "entry_outcomes", "pending_entries", "benchmark_start_price", "regime",
        "origin_requested_min_rs_score", "origin_requested_min_canslim_score",
        "result_config",
    }
    if set(checkpoint) != expected_fields:
        _fail("completed checkpoint schema is not exact")
    if checkpoint.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
        _fail("portfolio checkpoint schema must be v3")
    if checkpoint.get("entry_outcome_schema_version") != _OUTCOME_SCHEMA_VERSION:
        _fail("portfolio checkpoint outcome schema must be v1")
    if checkpoint.get("completed") is not True:
        _fail("run is incomplete: portfolio checkpoint is not completed")
    if checkpoint.get("code_identity") != manifest.get("git_head"):
        _fail("checkpoint code identity does not match manifest Git identity")
    _require_builtin_strategy_identity(checkpoint.get("strategy_identity"))
    if (
        checkpoint.get("origin_requested_min_rs_score") != 80.0
        or checkpoint.get("origin_requested_min_canslim_score") != 70.0
    ):
        _fail("checkpoint origin advisory threshold requests differ")
    _digest(checkpoint.get("fingerprint"), label="checkpoint fingerprint")
    if checkpoint["fingerprint"] != _expected_checkpoint_fingerprint(manifest, bundle_context):
        _fail("checkpoint fingerprint differs from the exact bound engine inputs/config")
    total = _nonnegative_int(checkpoint.get("total_days"), label="checkpoint total_days")
    next_index = _nonnegative_int(checkpoint.get("next_day_index"), label="checkpoint next_day_index")
    if (total, next_index) != (_SESSION_COUNT, _SESSION_COUNT):
        _fail("checkpoint must complete exactly 1255 daily sessions")
    if checkpoint.get("pending_entries") != []:
        _fail("completed checkpoint must not retain pending entries")
    if checkpoint.get("open_positions") != {}:
        _fail("completed checkpoint must not retain open positions")
    if not isinstance(checkpoint.get("result_config"), dict):
        _fail("completed checkpoint lacks result_config")
    if not isinstance(checkpoint.get("execution_diagnostics"), dict):
        _fail("checkpoint lacks execution diagnostics")
    if not isinstance(checkpoint.get("entry_outcomes"), list):
        _fail("checkpoint entry_outcomes must be a list")
    if not isinstance(checkpoint.get("trades"), list) or not isinstance(checkpoint.get("regime"), dict):
        _fail("checkpoint trades/regime state is malformed")
    offset = checkpoint.get("state_log_offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
        _fail("checkpoint state_log_offset must be a positive integer")
    if layout.state_path.stat().st_size != offset:
        _fail("completed checkpoint offset must equal the full state-log size")
    return checkpoint


def _audit_physical_set(
    run_dir: Path,
    manifest: Mapping[str, Any],
    layout: _StateLayout,
) -> None:
    children = list(run_dir.iterdir())
    for item in children:
        if item.is_symlink() or not item.is_file():
            _fail(f"unexpected non-regular run child: {item.name}")
    actual = {item.name for item in children}
    expected_files = _MANIFEST_ARTIFACTS | _RUN_FILES
    if actual != expected_files:
        _fail(f"unexpected physical run artifact set: {sorted(actual)}")
    hashes = manifest.get("artifacts")
    expected_hashes = _HASHED_ARTIFACTS
    if not isinstance(hashes, dict) or set(hashes) != expected_hashes:
        _fail("manifest artifact set is not the exact Task 6 publication set")
    for name, expected in hashes.items():
        if not isinstance(expected, str) or len(expected) != 64:
            _fail(f"manifest hash is invalid: {name}")
        if _sha256(run_dir / name) != expected:
            _fail(f"artifact SHA-256 mismatch: {name}")
    manifest_time = (run_dir / "run_manifest.json").stat().st_mtime_ns
    if any((run_dir / name).stat().st_mtime_ns > manifest_time for name in actual - {"run_manifest.json"}):
        _fail("run_manifest.json is not the manifest-last completion marker")


def _final_revalidate_run(
    run_dir: Path,
    manifest: Mapping[str, Any],
    layout: _StateLayout,
    *,
    expected_manifest_sha256: str,
) -> None:
    """Close the long-audit TOCTOU window before publishing success."""

    _audit_physical_set(run_dir, manifest, layout)
    if _sha256(run_dir / "run_manifest.json") != expected_manifest_sha256:
        _fail("final run manifest changed during audit")


def _audit_manifest(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "run_manifest.json"
    if (run_dir / "run_failed.json").exists():
        _fail("run has a failure marker and cannot be audited as complete")
    if not manifest_path.exists():
        _fail("run is incomplete: manifest-last run_manifest.json is absent")
    manifest = _json(manifest_path, label="run manifest")
    if manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        _fail("run manifest is not a completed schema-v1 publication")
    expected_head = args.expected_replay_git_sha.lower()
    if manifest.get("git_head") != expected_head:
        _fail("manifest Git identity differs from --expected-replay-git-sha")
    if _git_identity(args.repo_root) != expected_head:
        _fail("repository HEAD differs from the replay's exact Git identity")
    if manifest.get("bundle_sha256") != args.expected_bundle_sha256:
        _fail("manifest bundle SHA differs from expected preserved bundle")
    if manifest.get("date_contract") != {
        "warmup_start": _WARMUP, "evaluation_start": _START, "data_cutoff": _END,
    }:
        _fail("manifest date contract is not the fixed 2020/2021-2025 baseline")
    arguments = manifest.get("arguments")
    if not isinstance(arguments, dict):
        _fail("manifest arguments are missing")
    exact_arguments = {
        "start_date": _START, "end_date": _END, "benchmark": _BENCHMARK,
        "leader_count": 100, "rebalance_days": 20,
        "allow_incomplete_fundamentals": True, "bundle_sha256": _BUNDLE_SHA256,
    }
    for key, expected in exact_arguments.items():
        if arguments.get(key) != expected:
            _fail(f"manifest argument mismatch: {key}")
    if arguments.get("resume_checkpoint") is not None:
        _fail("Task 6 requires a fresh single-directory replay")
    if arguments.get("checkpoint_every_days") != 20:
        _fail("Task 6 checkpoint cadence must be 20 days")
    if _normalized_argument(arguments.get("output_root")) != run_dir.parent:
        _fail("manifest output_root is not the audited run directory parent")
    for key, path in _input_paths(args).items():
        if _normalized_argument(arguments.get(key)) != path:
            _fail(f"manifest input path does not identify the audited {key}")
    hashes = manifest.get("input_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(_replay_input_paths(args)):
        _fail("manifest input hash schema is incomplete")
    for key, path in _replay_input_paths(args).items():
        if hashes.get(key) != _sha256(path):
            _fail(f"post-run source hash differs from manifest input hash: {key}")
    expected_sources = {
        "membership": _json(args.membership_provenance, label="membership provenance"),
        "prices": _json(args.prices_provenance, label="prices provenance"),
        "fundamentals": _json(args.fundamentals_provenance, label="fundamentals provenance"),
    }
    _same_json(manifest.get("source_provenance"), expected_sources, label="manifest source provenance")
    return manifest


def _normalized_argument(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    return Path(value).resolve()


def _input_paths(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "pit_bundle": args.pit_bundle,
        "membership_provenance": args.membership_provenance,
        "prices_provenance": args.prices_provenance,
        "fundamentals_provenance": args.fundamentals_provenance,
        "fundamentals_coverage": args.fundamentals_coverage,
        "security_master": args.security_master,
        "security_master_exclusions": args.security_master_exclusions,
    }


def _replay_input_paths(args: argparse.Namespace) -> dict[str, Path]:
    return _input_paths(args)


def _audited_source_paths(args: argparse.Namespace) -> dict[str, Path]:
    return {
        **_input_paths(args),
        "bundle_manifest": args.bundle_manifest,
        "regeneration_audit": args.regeneration_audit,
        "membership_csv": args.membership_csv,
        "prices_csv": args.prices_csv,
        "fundamentals_csv": args.fundamentals_csv,
    }


def _audit_coverage(
    run_dir: Path,
    manifest: Mapping[str, Any],
    bundle_context: Mapping[str, Any],
    *,
    signals: pd.DataFrame,
    factual_coverage_counts: Mapping[str, int],
) -> dict[str, Any]:
    coverage = _json(run_dir / "coverage.json", label="coverage artifact")
    if coverage.get("schema_version") != 1 or coverage.get("date_contract") != {
        "warmup_start": _WARMUP, "evaluation_start": _START, "data_cutoff": _END,
    }:
        _fail("coverage schema/date contract differs")
    if set(coverage) != {
        "schema_version", "date_contract", "membership", "prices",
        "cik_and_exclusions", "evaluated_fundamentals", "gates",
        "all_gates_passed", "bundle", "non_blocking_failed_gates",
        "baseline_publishable",
    }:
        _fail("coverage top-level schema is not exact")
    status = manifest.get("coverage_status")
    if not isinstance(status, dict):
        _fail("manifest lacks coverage status")
    for key in ("all_gates_passed", "baseline_publishable", "non_blocking_failed_gates"):
        if status.get(key) != coverage.get(key):
            _fail(f"coverage status mismatch: {key}")
    gates = coverage.get("gates")
    gate_fields = {
        "membership_495_through_510": {"passed", "minimum", "maximum"},
        "spy_complete_2020_through_2025": {"passed", "first_date", "last_date"},
        "member_price_coverage_at_least_98_pct": {"passed", "value_pct", "threshold_pct"},
        "cik_resolved_or_closed_exclusion_at_least_95_pct": {
            "passed", "value_pct", "threshold_pct",
        },
        _FUNDAMENTALS_EXCEPTION: {"passed", "value_pct", "threshold_pct"},
    }
    if not isinstance(gates, dict) or set(gates) != set(gate_fields):
        _fail("coverage must contain exactly the five production gates")
    for name, fields in gate_fields.items():
        if not isinstance(gates[name], dict) or set(gates[name]) != fields:
            _fail(f"coverage gate schema differs: {name}")

    bundle_manifest = bundle_context["manifest"]
    coverage_membership = coverage.get("membership")
    if not isinstance(coverage_membership, dict) or set(coverage_membership) != {
        "event_count", "evaluation_session_count", "evaluation_session_min_members",
        "evaluation_session_max_members", "union_symbol_count",
    }:
        _fail("coverage membership facts are missing")
    minimum = int(coverage_membership.get("evaluation_session_min_members", -1))
    maximum = int(coverage_membership.get("evaluation_session_max_members", -1))
    manifest_membership = bundle_manifest["coverage"]["membership"]
    if (minimum, maximum) != (
        int(manifest_membership["evaluation_session_min_members"]),
        int(manifest_membership["evaluation_session_max_members"]),
    ):
        _fail("coverage membership extrema differ from immutable bundle")
    expected_membership = {
        "event_count": bundle_manifest["membership_events"],
        "evaluation_session_count": _SESSION_COUNT,
        "evaluation_session_min_members": minimum,
        "evaluation_session_max_members": maximum,
        "union_symbol_count": bundle_context["task2"]["membership_union_symbol_count"],
    }
    _same_serialized_json(
        coverage_membership, expected_membership, label="coverage membership facts"
    )
    expected_membership_gate = {
        "passed": minimum >= 495 and maximum <= 510,
        "minimum": minimum,
        "maximum": maximum,
    }
    _same_serialized_json(
        gates["membership_495_through_510"], expected_membership_gate,
        label="membership coverage gate",
    )
    prices = bundle_context["prices_provenance"]
    price_manifest = bundle_manifest["coverage"]["price"]
    expected_spy_gate = {
        "passed": (
            str(prices.get("spy_first_date")) <= "2020-01-02"
            and str(prices.get("spy_last_date")) == _END
            and str(price_manifest["first_date"]) <= "2020-01-02"
            and str(price_manifest["last_date"]) == _END
        ),
        "first_date": prices.get("spy_first_date"),
        "last_date": prices.get("spy_last_date"),
    }
    _same_serialized_json(
        gates["spy_complete_2020_through_2025"], expected_spy_gate,
        label="SPY coverage gate",
    )
    price_pct = float(prices.get("coverage_pct"))
    if not isinstance(coverage.get("prices"), dict) or set(coverage["prices"]) != {
        "coverage_pct", "member_trading_day_pairs", "covered_member_trading_day_pairs",
        "remaining_member_pair_gap_count", "symbols_with_no_prices",
        "symbols_with_partial_prices", "spy_first_date", "spy_last_date",
    }:
        _fail("coverage price facts are missing")
    expected_prices = {
        field: prices.get(field) for field in coverage["prices"]
    }
    expected_prices["coverage_pct"] = price_pct
    expected_prices["symbols_with_no_prices"] = prices.get("symbols_with_no_prices", [])
    expected_prices["symbols_with_partial_prices"] = prices.get(
        "symbols_with_partial_prices", []
    )
    _same_serialized_json(coverage["prices"], expected_prices, label="coverage price facts")
    cik_pct = float(bundle_context["task2"]["resolved_or_closed_exclusion_percentage"])
    evaluated = coverage.get("evaluated_fundamentals")
    if not isinstance(evaluated, dict) or set(evaluated) != {
        "evaluated_symbol_date_count", "usable_current_quarterly_count",
        "usable_annual_count", "usable_current_quarterly_and_annual_count",
        "current_quarterly_and_annual_pct", "coverage_basis",
    }:
        _fail("evaluated-fundamentals coverage facts are missing")
    if evaluated.get("coverage_basis") != (
        "unique strict-PIT signal-log symbol/date rows independently recomputed from "
        "hash-bound as-of quarterly/annual frames with fiscal-date-matched "
        "evaluate_c and unchanged evaluate_a"
    ):
        _fail("evaluated-fundamentals coverage basis differs")
    evaluated_rows = len(signals)
    if _nonnegative_int(
        evaluated.get("evaluated_symbol_date_count"), label="evaluated coverage denominator"
    ) != evaluated_rows:
        _fail("evaluated-fundamentals denominator differs from signal artifact")
    current_count = int(factual_coverage_counts["current"])
    annual_count = int(factual_coverage_counts["annual"])
    both_count = int(factual_coverage_counts["both"])
    if _nonnegative_int(
        evaluated.get("usable_current_quarterly_count"), label="current coverage numerator"
    ) != current_count or _nonnegative_int(
        evaluated.get("usable_annual_count"), label="annual coverage numerator"
    ) != annual_count:
        _fail("evaluated-fundamentals component counts differ from signal artifact")
    both = _nonnegative_int(
        evaluated.get("usable_current_quarterly_and_annual_count"),
        label="evaluated coverage numerator",
    )
    if both != both_count:
        _fail("evaluated-fundamentals joint count differs from signal artifact")
    fundamentals_pct = both / evaluated_rows * 100.0
    _close_number(
        evaluated.get("current_quarterly_and_annual_pct"), fundamentals_pct,
        label="evaluated-fundamentals percentage",
    )
    expected_threshold_gates = {
        "member_price_coverage_at_least_98_pct": (price_pct, 98.0),
        "cik_resolved_or_closed_exclusion_at_least_95_pct": (cik_pct, 95.0),
        _FUNDAMENTALS_EXCEPTION: (fundamentals_pct, 90.0),
    }
    for name, (value, threshold) in expected_threshold_gates.items():
        expected_gate = {
            "passed": value >= threshold,
            "value_pct": value,
            "threshold_pct": threshold,
        }
        _same_serialized_json(gates[name], expected_gate, label=f"coverage gate {name}")
    _same_json(coverage.get("cik_and_exclusions"), bundle_context["task2"],
               label="coverage Task 2 bindings")
    _same_json(coverage.get("bundle"), bundle_manifest, label="coverage bundle manifest")
    failed = sorted(name for name, gate in gates.items() if not isinstance(gate, dict) or gate.get("passed") is not True)
    if coverage.get("all_gates_passed") != (not failed):
        _fail("coverage all_gates_passed is inconsistent")
    non_blocking = coverage.get("non_blocking_failed_gates")
    if coverage.get("all_gates_passed"):
        if non_blocking != [] or coverage.get("baseline_publishable") is not True:
            _fail("passing coverage must be publishable with no exceptions")
    elif (
        failed != [_FUNDAMENTALS_EXCEPTION]
        or non_blocking != [_FUNDAMENTALS_EXCEPTION]
        or coverage.get("baseline_publishable") is not True
    ):
        _fail("only the explicit evaluated-fundamentals coverage exception is permitted")
    return coverage


def _optional_number(value: object) -> float | None:
    if pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _reason_tuple(value: object) -> tuple[str, ...]:
    if pd.isna(value) or value == "":
        return ()
    if not isinstance(value, str):
        _fail("blocking reasons must be comma-separated text")
    return tuple(value.split(","))


def _assert_optional_number(actual: object, expected: object, *, label: str) -> None:
    actual_number = _optional_number(actual)
    expected_number = _optional_number(expected)
    if actual_number is None or expected_number is None:
        if actual_number is not expected_number:
            _fail(f"{label} availability differs")
    else:
        _close_number(actual_number, expected_number, label=label)


def _configured_candidate_tickers(checkpoint: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the exact post-filter candidate order stored by the engine."""
    config = checkpoint.get("result_config")
    if not isinstance(config, Mapping):
        _fail("completed checkpoint lacks a result configuration")
    raw_tickers = config.get("tickers")
    if not isinstance(raw_tickers, list) or not raw_tickers:
        _fail("result configuration lacks the configured candidate universe")
    tickers: list[str] = []
    for value in raw_tickers:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or value != value.upper()
            or value == _BENCHMARK
        ):
            _fail("result configuration contains an invalid candidate ticker")
        tickers.append(value)
    if len(set(tickers)) != len(tickers):
        _fail("result configuration contains duplicate candidate tickers")
    if config.get("technical_only") is not False or config.get("data_mode") != "point_in_time":
        _fail("expected signal-key audit requires the fixed full PIT mode")
    return tuple(tickers)


def _journal_symbol(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value != value.upper()
    ):
        _fail(f"{label} is not an uppercase nonblank ticker")
    return value


def _journal_quantity(value: object, *, label: str) -> Decimal:
    """Return one engine-serialized six-decimal quantity.

    ``_record_transaction`` and identity transfers both use ``round(quantity,
    6)``.  Parsing via ``Decimal(str(...))`` retains the emitted decimal value
    instead of adding binary-float comparison error to the audit budget.
    """
    if isinstance(value, bool):
        _fail(f"{label} is not a finite nonnegative six-decimal quantity")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AssertionError(f"{label} is not a finite nonnegative six-decimal quantity") from exc
    if not quantity.is_finite() or quantity < 0:
        _fail(f"{label} is not a finite nonnegative six-decimal quantity")
    try:
        rounded = quantity.quantize(_QUANTITY_QUANTUM)
    except InvalidOperation as exc:
        raise AssertionError(f"{label} cannot be represented at six decimals") from exc
    if quantity != rounded:
        _fail(f"{label} has more than six serialized decimal places")
    return quantity


@dataclass(frozen=True)
class _SerializedPosition:
    """Observed six-decimal quantity plus its bounded source-rounding error."""

    quantity: Decimal
    error_budget: Decimal


def _replay_serialized_position_records(
    open_positions: dict[str, _SerializedPosition],
    records: Sequence[object],
    *,
    label: str,
) -> None:
    """Apply one ordered journal transaction batch with a bounded rounding ledger.

    The engine keeps unrounded position quantities but writes each BUY, SELL,
    and TRANSFER quantity rounded independently to six decimal places.  A
    position begins with at most half a micro-share of error.  Every serialized
    SELL can increase its residual error by one further half micro-share.
    TRANSFER compares the accumulated predecessor bound plus its own rounding
    error, then resets the successor to that directly serialized value.  The
    budget is used only for zero-vs-open and transfer-continuity decisions;
    ticker/action order validation remains exact.
    """
    for transaction_index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            _fail(f"{label} transaction is not an object")
        ticker = _journal_symbol(
            record.get("Ticker"), label=f"{label} transaction {transaction_index} ticker"
        )
        action = record.get("Action")
        if not isinstance(action, str):
            _fail(f"{label} transaction action is invalid")
        action = action.upper()
        quantity = _journal_quantity(
            record.get("Quantity"), label=f"{label} transaction {transaction_index} quantity"
        )

        if action == "BUY":
            if quantity <= 0:
                _fail(f"{label} BUY quantity is not positive")
            if ticker in open_positions:
                _fail(f"{label} BUY duplicates an already-open ticker")
            open_positions[ticker] = _SerializedPosition(quantity, _QUANTITY_ROUNDING_ERROR)
            continue

        if action == "SELL":
            held = open_positions.get(ticker)
            if held is None:
                _fail(f"{label} SELL has no open ticker")
            # The recorded SELL itself can differ from the engine's unrounded
            # sale by half a micro-share, in addition to accumulated prior rows.
            next_budget = held.error_budget + _QUANTITY_ROUNDING_ERROR
            if quantity > held.quantity + next_budget:
                _fail(f"{label} SELL exceeds the open quantity beyond the six-decimal budget")
            remaining = held.quantity - quantity
            if abs(remaining) <= next_budget:
                # This is the only deliberate close decision relaxation: the
                # residual is fully explainable by independently rounded rows.
                open_positions.pop(ticker)
            else:
                open_positions[ticker] = _SerializedPosition(remaining, next_budget)
            continue

        if action == "TRANSFER":
            if quantity <= 0:
                _fail(f"{label} TRANSFER quantity is not positive")
            predecessor = _journal_symbol(
                record.get("FromTicker"),
                label=f"{label} transaction {transaction_index} predecessor",
            )
            held = open_positions.get(predecessor)
            if held is None or ticker in open_positions:
                _fail(f"{label} identity transfer has invalid open-position state")
            transfer_budget = held.error_budget + _QUANTITY_ROUNDING_ERROR
            if abs(quantity - held.quantity) > transfer_budget:
                _fail(
                    f"{label} identity transfer quantity differs from the predecessor "
                    "beyond the six-decimal budget"
                )
            # Transfer's serialized quantity is a fresh direct observation of
            # the unrounded remaining engine position, so prior drift does not
            # carry into the successor ledger.
            open_positions.pop(predecessor)
            open_positions[ticker] = _SerializedPosition(
                quantity, _QUANTITY_ROUNDING_ERROR
            )
            continue

        _fail(f"{label} transaction action is unsupported")


def _open_symbols_at_signal_time(
    state_path: Path,
    sessions: pd.DatetimeIndex,
) -> tuple[frozenset[str], ...]:
    """Replay daily transaction order through the point immediately before screening.

    A day journal contains exits/identity transfers and queued next-open entries
    before that day's call to ``_evaluate_signals``.  The terminal ``final``
    journal record is deliberately ignored: its end-of-test liquidations occur
    after the last signal evaluation.
    """
    open_quantities: dict[str, _SerializedPosition] = {}
    open_by_session: list[frozenset[str]] = []
    expected_index = pd.DatetimeIndex(sessions).normalize()
    next_day_index = 0
    final_seen = False

    for _line, event in _jsonl_records(state_path, label="portfolio state log"):
        kind = event.get("kind")
        if kind == "final":
            if final_seen or next_day_index != len(expected_index):
                _fail("state journal final record is misplaced")
            final_seen = True
            continue
        if kind != "day" or final_seen:
            _fail("state journal has an unexpected record while replaying positions")
        if next_day_index >= len(expected_index):
            _fail("state journal has too many day records")
        if event.get("day_index") != next_day_index:
            _fail("state journal day index differs while replaying positions")
        try:
            journal_date = pd.Timestamp(event.get("date")).normalize()
        except (TypeError, ValueError) as exc:
            raise AssertionError("state journal day has an invalid date") from exc
        if journal_date != expected_index[next_day_index]:
            _fail("state journal day date differs while replaying positions")
        records = event.get("transactions")
        if not isinstance(records, list):
            _fail("state journal day transactions are not a list")

        for record in records:
            if not isinstance(record, Mapping):
                _fail("state journal transaction is not an object")
            try:
                transaction_date = pd.Timestamp(record.get("Date")).normalize()
            except (TypeError, ValueError) as exc:
                raise AssertionError("state journal transaction has an invalid date") from exc
            if transaction_date != journal_date:
                _fail("state journal transaction is not on its journal day")
        _replay_serialized_position_records(open_quantities, records, label="state journal")

        open_by_session.append(frozenset(open_quantities))
        next_day_index += 1

    if next_day_index != len(expected_index) or not final_seen:
        _fail("state journal is incomplete while replaying open positions")
    return tuple(open_by_session)


def _evaluation_history_counts(
    history: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    symbol: str,
) -> np.ndarray:
    """Count bars visible to the strategy from its no-warmup price slice."""
    if not isinstance(history, pd.DataFrame):
        _fail(f"immutable history is not a frame: {symbol}")
    index = pd.DatetimeIndex(history.index).normalize()
    if not index.is_monotonic_increasing:
        _fail(f"immutable history is not sorted: {symbol}")
    start = pd.Timestamp(_START)
    end = pd.Timestamp(_END)
    if len(index) and (index[0] < start or index[-1] > end):
        _fail(f"immutable history is not the engine's evaluation-only slice: {symbol}")
    index_values = index.as_unit("ns").asi8
    session_values = pd.DatetimeIndex(sessions).as_unit("ns").asi8
    return np.searchsorted(index_values, session_values, side="right")


def _evaluation_exact_session_mask(
    history: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    symbol: str,
) -> np.ndarray:
    index = _normalized_unique_index(history, label=f"immutable history {symbol}")
    return np.asarray(sessions.isin(index), dtype=bool)


def _expected_signal_keys(
    args: argparse.Namespace,
    state_path: Path,
    checkpoint: Mapping[str, Any],
    bundle_context: Mapping[str, Any],
) -> tuple[tuple[str, pd.Timestamp], ...]:
    """Derive every full-mode ``evaluate_symbol`` row independently of its CSV.

    This mirrors the configured PIT loop, not merely the emitted signal file:
    candidate order comes from the result config, membership is evaluated for
    every SPY session, price observations begin at the evaluation start (no
    warmup), and daily journal transactions are replayed through queued opens,
    exits, and identity transfers before each signal pass.
    """
    _add_repo_to_path(args.repo_root)
    from core.pit_data import PITDataBundle  # pylint: disable=import-outside-toplevel

    config = checkpoint["result_config"]
    candidates = _configured_candidate_tickers(checkpoint)
    cadence = config.get("signal_every_n_days")
    if isinstance(cadence, bool):
        _fail("result configuration signal cadence is invalid")
    try:
        cadence = int(cadence)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssertionError("result configuration signal cadence is invalid") from exc
    if cadence < 1:
        _fail("result configuration signal cadence is invalid")
    sessions = pd.DatetimeIndex(bundle_context["sessions"]).normalize()
    open_by_session = _open_symbols_at_signal_time(state_path, sessions)
    if len(open_by_session) != len(sessions):
        _fail("open-position replay does not cover every SPY session")

    with PITDataBundle(args.pit_bundle, expected_sha256=args.expected_bundle_sha256) as bundle:
        histories = bundle.fetch_price_data(
            [*candidates, _BENCHMARK], pd.Timestamp(_START), pd.Timestamp(_END)
        )
        if _BENCHMARK not in histories:
            _fail("immutable bundle lacks SPY OHLCV for expected-key audit")
        history_counts = {
            ticker: _evaluation_history_counts(history, sessions, symbol=ticker)
            for ticker, history in histories.items()
            if ticker in candidates
        }
        exact_session_masks = {
            ticker: _evaluation_exact_session_mask(
                history, sessions, symbol=ticker
            )
            for ticker, history in histories.items()
            if ticker in candidates
        }
        members_by_session = tuple(bundle.members_at(when) for when in sessions)

    expected: list[tuple[str, pd.Timestamp]] = []
    for day_index, when in enumerate(sessions):
        if day_index % cadence != 0:
            continue
        members = members_by_session[day_index]
        open_symbols = open_by_session[day_index]
        for ticker in candidates:
            # This is the full-mode PIT branch in PortfolioSimulator.run and
            # _evaluate_signals: industry filtering is inert because PIT mode
            # supplies an empty industry map.
            if ticker not in members or ticker in open_symbols:
                continue
            observations = history_counts.get(ticker)
            exact_mask = exact_session_masks.get(ticker)
            if (
                observations is None
                or exact_mask is None
                or not bool(exact_mask[day_index])
                or int(observations[day_index]) < 60
            ):
                continue
            expected.append((ticker, when))
    return tuple(expected)


def _require_exact_signal_keys(
    actual: Sequence[tuple[str, pd.Timestamp]],
    expected: Sequence[tuple[str, pd.Timestamp]],
) -> None:
    """Fail closed on dropped, injected, or reordered strategy rows."""
    actual_set = set(actual)
    expected_set = set(expected)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)[:5]
        extra = sorted(actual_set - expected_set)[:5]
        _fail(
            "emitted signal keys differ from the independent full-mode loop "
            f"(missing={len(expected_set - actual_set)} {missing}, "
            f"extra={len(actual_set - expected_set)} {extra})"
        )
    if tuple(actual) != tuple(expected):
        _fail("emitted signal-key order differs from the configured PIT candidate loop")


def _audit_all_signal_rows(
    args: argparse.Namespace,
    signals: pd.DataFrame,
    signal_dates: pd.Series,
    bundle_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild every signal from exact sessions and independent CANSLIM facts."""

    _add_repo_to_path(args.repo_root)
    from backtest import _evaluate_market_at_date  # pylint: disable=import-outside-toplevel
    from core.pit_data import PITDataBundle  # pylint: disable=import-outside-toplevel

    bool_names = (
        "buy_signal", "buy_signal_without_market", "has_breakout",
        "has_volume_surge", "in_buy_zone", "price_advanced",
        "technical_setup_eligible", "entry_contract_eligible", "market_is_bullish",
        "market_regime_is_bullish", "has_peg_today", "technical_only",
    )
    for name in bool_names:
        _strict_bool(signals, name, label="signals")

    symbols = sorted(set(signals["symbol"]))
    all_closes = bundle_context["all_closes"]
    independent_eligible: dict[int, bool] = {}
    coverage_counts = {"current": 0, "annual": 0, "both": 0}
    with PITDataBundle(args.pit_bundle, expected_sha256=args.expected_bundle_sha256) as bundle:
        histories = bundle.fetch_price_data(
            [*symbols, _BENCHMARK], pd.Timestamp(_START), pd.Timestamp(_END)
        )
        if _BENCHMARK not in histories:
            _fail("immutable bundle lacks SPY OHLCV for factual signal audit")
        history_positions: dict[str, dict[pd.Timestamp, int]] = {}
        for history_symbol, history_frame in histories.items():
            history_index = _normalized_unique_index(
                history_frame, label=f"immutable history {history_symbol}"
            )
            history_positions[str(history_symbol).upper()] = {
                pd.Timestamp(timestamp): position
                for position, timestamp in enumerate(history_index)
            }

        fundamental_index = pd.read_csv(
            args.fundamentals_csv,
            usecols=["ticker", "public_date"],
            dtype=str,
            keep_default_na=False,
        ).drop_duplicates()
        if fundamental_index.empty or fundamental_index.eq("").any().any():
            _fail("hash-bound fundamental source lacks a usable public-date index")
        public_dates: dict[str, np.ndarray] = {
            str(ticker): pd.DatetimeIndex(
                pd.to_datetime(group["public_date"], errors="raise").sort_values()
            ).as_unit("ns").asi8
            for ticker, group in fundamental_index.groupby("ticker", sort=False)
        }
        fundamental_cache: dict[tuple[str, int | None], dict[str, Any]] = {}
        session_groups = signals.groupby(signal_dates, sort=False).groups
        for raw_when, indexes in session_groups.items():
            when = pd.Timestamp(raw_when).normalize()
            members = bundle.members_at(when)
            rs_snapshot = _independent_rs_snapshot(
                all_closes,
                when,
                eligible_tickers={str(symbol).upper() for symbol in members - {_BENCHMARK}},
            )
            m_score_raw, market_bullish, _distribution_days, _follow_through = (
                _evaluate_market_at_date(histories[_BENCHMARK], when)
            )
            m_score = _optional_number(m_score_raw)
            if m_score is None:
                _fail(f"market score is unavailable on exact session {when.date()}")

            for row in signals.loc[indexes].itertuples(index=True):
                index = int(row.Index)
                symbol = str(row.symbol)
                label = f"signal {symbol}/{when.date()}"
                if symbol not in members:
                    _fail(f"{label} is outside strict PIT membership")
                history = histories.get(symbol)
                if history is None:
                    _fail(f"{label} has no immutable price history")
                exact_position = history_positions.get(symbol, {}).get(when)
                if exact_position is None:
                    _fail(f"{label} lacks an exact completed-session price bar")
                facts = _independent_entry_facts_at_position(history, exact_position)
                available = history.iloc[: exact_position + 1]
                close_values = pd.to_numeric(available["Close"], errors="coerce")
                high_52 = _finite(close_values.iloc[-min(252, len(close_values)) :].max())
                if facts.event_close is None or high_52 is None or high_52 <= 0:
                    _fail(f"{label} lacks finite price facts for N/S")
                proximity = facts.event_close / high_52

                dates = public_dates.get(symbol, np.asarray([], dtype=np.int64))
                when_ns = int(when.value)
                public_position = int(np.searchsorted(dates, when_ns, side="right") - 1)
                public_state = int(dates[public_position]) if public_position >= 0 else None
                cache_key = (symbol, public_state)
                fundamental = fundamental_cache.get(cache_key)
                if fundamental is None:
                    as_of = pd.Timestamp(public_state) if public_state is not None else when
                    pit = bundle.fundamentals_as_of(symbol, as_of)
                    c_score, current = _independent_evaluate_c(pit["quarterly_income"])
                    a_score, annual, _roe = _independent_evaluate_a(
                        pit["annual_income"], pit["balance_sheet"]
                    )
                    i_score, institutional_available = _independent_evaluate_i(
                        pit["company_info"]
                    )
                    _n_without_price, revenue_growth = _independent_evaluate_n(
                        pit["quarterly_income"], None
                    )
                    fundamental = {
                        "c_score": _optional_number(c_score),
                        "a_score": _optional_number(a_score),
                        "current_growth": _optional_number(current),
                        "annual_growth": _optional_number(annual),
                        "i_score": _optional_number(i_score),
                        "institutional_data_available": institutional_available,
                        "shares_outstanding": pit["company_info"].get("shares_outstanding"),
                        "revenue_growth": _optional_number(revenue_growth),
                    }
                    fundamental_cache[cache_key] = fundamental

                current = fundamental["current_growth"]
                annual = fundamental["annual_growth"]
                coverage_counts["current"] += int(current is not None)
                coverage_counts["annual"] += int(annual is not None)
                coverage_counts["both"] += int(current is not None and annual is not None)

                rs_score = _optional_number(rs_snapshot.get(symbol))
                l_score = rs_score / 100.0 if rs_score is not None else math.nan
                n_score = _independent_n_score(
                    fundamental["revenue_growth"], proximity
                )
                s_score, has_power_gap, gap_details = _independent_evaluate_s(
                    available,
                    prior_average_volume_50=facts.prior_average_volume_50,
                    shares_outstanding=fundamental["shares_outstanding"],
                )
                has_peg_today = bool(has_power_gap and gap_details.get("days_ago") == 0)
                c_score = _optional_number(fundamental["c_score"])
                a_score = _optional_number(fundamental["a_score"])
                i_score = _optional_number(fundamental["i_score"])
                if c_score is None or a_score is None or i_score is None:
                    _fail(f"{label} has a non-finite independent component score")
                canslim_score, entry_composite, technical_score = (
                    _independent_composite_scores(
                        c_score=c_score,
                        a_score=a_score,
                        n_score=n_score,
                        s_score=s_score,
                        l_score=l_score,
                        i_score=i_score,
                        m_score=m_score,
                        institutional_data_available=bool(
                            fundamental["institutional_data_available"]
                        ),
                    )
                )
                (
                    eligible,
                    entry_blockers,
                    current,
                    annual,
                    rs_score,
                    entry_composite,
                ) = _independent_entry_decision(
                    facts,
                    current_growth=current,
                    annual_growth=annual,
                    rs_score=rs_score,
                    composite_score=entry_composite,
                )
                independent_eligible[index] = eligible

                expected_numbers = {
                    "close": facts.event_close,
                    "c_score": c_score,
                    "a_score": a_score,
                    "n_score": n_score,
                    "s_score": s_score,
                    "i_score": i_score,
                    "m_score": m_score,
                    "current_growth": current,
                    "annual_growth": annual,
                    "rs_score": rs_score,
                    "canslim_score": canslim_score,
                    "entry_composite_score": entry_composite,
                    "technical_score": technical_score,
                    "pivot": facts.pivot,
                    "prior_close": facts.prior_close,
                    "event_volume": facts.event_volume,
                    "prior_average_volume_50": facts.prior_average_volume_50,
                    "entry_volume_ratio": facts.volume_ratio,
                    "entry_extension": facts.extension,
                }
                for field, expected in expected_numbers.items():
                    _assert_optional_number(
                        getattr(row, field), expected, label=f"{label} {field}"
                    )
                expected_bools = {
                    "market_is_bullish": True,
                    "market_regime_is_bullish": bool(market_bullish),
                    "buy_signal_without_market": eligible,
                    "has_breakout": facts.in_buy_zone,
                    "has_volume_surge": facts.has_volume_surge,
                    "has_peg_today": has_peg_today,
                    "price_advanced": facts.price_advanced,
                    "in_buy_zone": facts.in_buy_zone,
                    "technical_setup_eligible": facts.eligible,
                    "entry_contract_eligible": eligible,
                    "buy_signal": eligible,
                    "technical_only": False,
                }
                for field, expected in expected_bools.items():
                    if bool(getattr(row, field)) != expected:
                        _fail(f"{label} {field} differs from independent decision")
                if _reason_tuple(row.technical_blocking_reasons) != facts.blocking_reasons:
                    _fail(f"{label} technical blockers differ")
                if _reason_tuple(row.entry_blocking_reasons) != entry_blockers:
                    _fail(f"{label} entry blockers differ")
                expected_reason = "Volume Breakout" if facts.eligible else "No Breakout"
                if row.signal_reason != expected_reason:
                    _fail(f"{label} signal_reason differs")

    eligibility = pd.Series(independent_eligible).reindex(signals.index)
    if eligibility.isna().any():
        _fail("independent signal audit did not cover every row")
    return {
        "eligible": eligibility.astype(bool),
        "coverage_counts": coverage_counts,
    }


def _audit_exact_next_opens(
    args: argparse.Namespace,
    outcomes: pd.DataFrame,
    transactions: pd.DataFrame,
    bundle_context: Mapping[str, Any],
) -> None:
    """Bind every attempt to the immutable exact next-session Open."""

    if outcomes.empty:
        return
    _add_repo_to_path(args.repo_root)
    from core.pit_data import PITDataBundle  # pylint: disable=import-outside-toplevel

    sessions = pd.DatetimeIndex(bundle_context["sessions"]).normalize()
    next_session = {
        current: following
        for current, following in zip(sessions, sessions[1:], strict=False)
    }
    buy_rows = transactions.loc[
        transactions["Action"].astype(str).str.upper().eq("BUY")
    ].copy()
    if not buy_rows.empty:
        buy_rows["Date"] = pd.to_datetime(buy_rows["Date"], errors="raise").dt.normalize()
        buy_rows["Ticker"] = buy_rows["Ticker"].astype(str).str.upper()
        if buy_rows.duplicated(["Ticker", "Date"]).any():
            _fail("BUY transactions are not unique by ticker/session")
    buy_prices = {
        (str(row.Ticker), pd.Timestamp(row.Date).normalize()): row.Price
        for row in buy_rows.itertuples(index=False)
    }
    symbols = sorted(set(outcomes["symbol"].astype(str).str.upper()))
    with PITDataBundle(args.pit_bundle, expected_sha256=args.expected_bundle_sha256) as bundle:
        histories = bundle.fetch_price_data(
            symbols, pd.Timestamp(_START), pd.Timestamp(_END)
        )
    for record in outcomes.where(pd.notna(outcomes), None).to_dict("records"):
        symbol = str(record["symbol"]).upper()
        signal_date = pd.Timestamp(record["signal_date"]).normalize()
        expected_entry = next_session.get(signal_date)
        if expected_entry is None:
            _fail("non-final entry outcome has no next benchmark session")
        history = histories.get(symbol)
        if history is None:
            _fail(f"entry outcome has no immutable price history: {symbol}")
        index = _normalized_unique_index(history, label=f"entry history {symbol}")
        positions = np.flatnonzero(index == expected_entry)
        if len(positions) != 1 or "Open" not in history.columns:
            _fail(f"entry outcome lacks an exact next-session Open: {symbol}")
        exact_open = history.iloc[int(positions[0])]["Open"]
        buy_price = buy_prices.get((symbol, expected_entry))
        _require_exact_next_open_outcome(
            record,
            expected_entry_date=expected_entry,
            exact_bundle_open=exact_open,
            buy_transaction_price=buy_price,
        )

def _audit_frames(
    args: argparse.Namespace,
    run_dir: Path,
    layout: _StateLayout,
    checkpoint: Mapping[str, Any],
    manifest: Mapping[str, Any],
    bundle_context: Mapping[str, Any],
) -> dict[str, Any]:
    signals = pd.read_csv(run_dir / "canslim_signals.csv")
    outcomes = pd.read_csv(run_dir / "entry_attempt_outcomes.csv")
    transactions = pd.read_csv(run_dir / "transactions.csv")
    funnel = pd.read_csv(run_dir / "daily_entry_funnel.csv")
    equity = pd.read_csv(run_dir / "equity_curve.csv")
    weekly = pd.read_csv(run_dir / "weekly_holdings.csv")
    if set(signals.columns) != _SIGNAL_COLUMNS:
        _fail("signal artifact differs from the exact producer schema")
    if tuple(outcomes.columns) != _ENTRY_OUTCOME_COLUMNS:
        _fail("entry outcome CSV schema differs from v1")
    if tuple(funnel.columns) != _FUNNEL_COLUMNS:
        _fail("daily funnel CSV schema differs from v1")
    if not {"date", "portfolio", "benchmark"}.issubset(equity):
        _fail("equity curve lacks baseline calendar fields")
    sessions = pd.Series(bundle_context["sessions"], name="date")
    equity_dates = _as_date_series(equity, "date", label="equity curve")
    if not equity_dates.reset_index(drop=True).equals(sessions.reset_index(drop=True)):
        _fail("equity curve dates differ from immutable-bundle SPY sessions")
    signal_dates = _as_date_series(signals, "signal_date", label="signals")
    unique_signal_dates = pd.DatetimeIndex(signal_dates.drop_duplicates().sort_values())
    if not unique_signal_dates.isin(pd.DatetimeIndex(sessions)).all():
        _fail("signal dates contain a value outside immutable-bundle SPY sessions")
    if signals["symbol"].isna().any() or not signals["symbol"].map(
        lambda value: isinstance(value, str) and value == value.strip() and value == value.upper() and bool(value)
    ).all():
        _fail("signals contain an invalid symbol")
    if pd.DataFrame({"symbol": signals["symbol"], "date": signal_dates}).duplicated().any():
        _fail("signals are not unique by symbol/session")
    actual_signal_keys = tuple(zip(
        signals["symbol"].tolist(), signal_dates.tolist(), strict=True,
    ))
    expected_signal_keys = _expected_signal_keys(
        args, layout.state_path, checkpoint, bundle_context
    )
    _require_exact_signal_keys(actual_signal_keys, expected_signal_keys)
    if signals.empty:
        _fail("signal artifact is empty")
    buy = _strict_bool(signals, "buy_signal", label="signals")
    serialized_eligible = _strict_bool(signals, "entry_contract_eligible", label="signals")
    technical = _strict_bool(signals, "technical_setup_eligible", label="signals")
    if not buy.equals(serialized_eligible):
        _fail("fixed no-market-gate buy_signal does not equal entry eligibility")
    if bool((serialized_eligible & ~technical).any()):
        _fail("entry-qualified signal lacks technical eligibility")
    _finite_optional(
        signals,
        (
            "close", "c_score", "a_score", "n_score", "s_score", "i_score", "m_score",
            "current_growth", "annual_growth", "rs_score", "canslim_score",
            "entry_composite_score", "technical_score", "pivot", "prior_close",
            "event_volume", "prior_average_volume_50", "entry_volume_ratio",
            "entry_extension",
        ),
        label="signals",
    )
    factual = _audit_all_signal_rows(
        args, signals, signal_dates, bundle_context
    )
    eligible = factual["eligible"]
    if not serialized_eligible.equals(eligible):
        _fail("serialized eligibility differs from independently rebuilt decisions")
    if not {"Ticker", "Date", "Action", "Price", "Quantity", "Reason"}.issubset(transactions):
        _fail("transaction artifact lacks reconciliation fields")
    transaction_dates = _as_date_series(transactions, "Date", label="transactions") if not transactions.empty else pd.Series(dtype="datetime64[ns]")
    if not transactions.empty and not set(transaction_dates).issubset(set(sessions)):
        _fail("transactions contain an off-calendar date")
    actions = transactions["Action"].astype(str).str.upper() if not transactions.empty else pd.Series(dtype=str)
    if not actions.isin({"BUY", "SELL", "TRANSFER"}).all():
        _fail("transactions contain an unsupported action")
    if not transactions.empty:
        buy_sell = actions.isin({"BUY", "SELL"})
        prices = pd.to_numeric(transactions.loc[buy_sell, "Price"], errors="coerce")
        if prices.isna().any() or (prices <= 0).any() or not prices.map(math.isfinite).all():
            _fail("BUY/SELL transaction prices must be finite and positive")
        quantities = pd.to_numeric(transactions["Quantity"], errors="coerce")
        if quantities.isna().any() or (quantities <= 0).any() or not quantities.map(math.isfinite).all():
            _fail("transaction quantities must be finite and positive")
    _audit_exact_next_opens(args, outcomes, transactions, bundle_context)
    if len(outcomes) != len(checkpoint["entry_outcomes"]):
        _fail("outcome CSV count differs from completed checkpoint")
    if manifest.get("entry_attempt_outcome_schema_version") != _OUTCOME_SCHEMA_VERSION or (
        manifest.get("entry_attempt_outcome_count") != len(outcomes)
    ):
        _fail("manifest outcome schema/count differs from outcome artifact")
    _compare_record_sequence_to_frame(
        checkpoint["entry_outcomes"], outcomes, label="checkpoint entry outcomes"
    )
    funnel_dates = _as_date_series(funnel, "signal_date", label="daily funnel")
    if not funnel_dates.reset_index(drop=True).equals(sessions.reset_index(drop=True)):
        _fail("daily funnel dates differ from immutable-bundle SPY sessions")
    for column in _FUNNEL_COLUMNS[1:]:
        values = funnel[column].map(
            lambda value, name=column: _nonnegative_int(
                value, label=f"daily funnel {name}"
            )
        )
        if not values.equals(funnel[column]):
            _fail(f"daily funnel {column} is not an integer column")
    session_index = pd.DatetimeIndex(sessions)
    derived = pd.DataFrame({"signal_date": session_index})
    derived["evaluated_count"] = signal_dates.value_counts().reindex(session_index, fill_value=0).to_numpy()
    derived["qualified_count"] = signal_dates[eligible].value_counts().reindex(
        session_index, fill_value=0
    ).to_numpy()
    if not outcomes.empty:
        outcome_dates = _as_date_series(outcomes, "signal_date", label="outcomes")
        derived["attempted_count"] = outcome_dates.value_counts().reindex(
            session_index, fill_value=0
        ).to_numpy()
        derived["executed_count"] = outcome_dates.loc[
            outcomes["outcome"].eq("entries_executed")
        ].value_counts().reindex(session_index, fill_value=0).to_numpy()
    else:
        derived["attempted_count"] = 0
        derived["executed_count"] = 0
    derived["rejected_count"] = derived["attempted_count"] - derived["executed_count"]
    for column in _FUNNEL_COLUMNS[1:]:
        if not (funnel[column].astype(int).to_numpy() == derived[column].to_numpy()).all():
            _fail(f"daily funnel {column} does not match its source ledger")
    expected_signal_dates = pd.DatetimeIndex(np.repeat(
        session_index.to_numpy(), funnel["evaluated_count"].astype(int).to_numpy()
    ))
    if not pd.DatetimeIndex(signal_dates).equals(expected_signal_dates):
        _fail("ordered signal dates do not exactly match bundle sessions/funnel counts")
    if (funnel["executed_count"] > funnel["attempted_count"]).any() or (
        funnel["rejected_count"] > funnel["attempted_count"]
    ).any() or (funnel["attempted_count"] > funnel["qualified_count"]).any():
        _fail("daily funnel violates executed/rejected/attempted/qualified bounds")
    qualified_keys = set(zip(
        signals.loc[eligible, "symbol"], signal_dates.loc[eligible], strict=True,
    ))
    if outcomes.duplicated(["symbol", "signal_date"]).any():
        _fail("entry outcomes are not unique by symbol/session")
    outcome_symbols = outcomes["symbol"] if not outcomes.empty else pd.Series(dtype=str)
    if not outcome_symbols.empty and not outcome_symbols.map(
        lambda value: isinstance(value, str) and value == value.strip().upper() and bool(value)
    ).all():
        _fail("entry outcomes contain an invalid symbol")
    outcome_dates = (
        _as_date_series(outcomes, "signal_date", label="outcomes")
        if not outcomes.empty else pd.Series(dtype="datetime64[ns]")
    )
    outcome_keys = set(zip(outcome_symbols, outcome_dates, strict=True))
    expected_outcome_keys = {key for key in qualified_keys if key[1] != session_index[-1]}
    if outcome_keys != expected_outcome_keys:
        _fail("outcome keys do not exactly equal all non-final qualified signal keys")
    diagnostics = checkpoint["execution_diagnostics"]
    if set(diagnostics) != _REQUIRED_DIAGNOSTICS:
        _fail("checkpoint diagnostics schema is not exact")
    for key in _REQUIRED_DIAGNOSTICS:
        _nonnegative_int(diagnostics[key], label=f"diagnostic {key}")
    rejection_counts = outcomes["outcome"].value_counts() if not outcomes.empty else pd.Series(dtype=int)
    qualified_count = int(eligible.sum())
    expected_diagnostics = {
        "signal_days": _SESSION_COUNT,
        "entries_allowed_days": _SESSION_COUNT,
        "blocked_by_regime_days": 0,
        "blocked_by_market_days": 0,
        "cash_deployment_override_days": 0,
        "buy_signal_rows": qualified_count,
        "potential_buy_signal_rows": qualified_count,
        "potential_buy_signal_rows_blocked_by_market": 0,
        "buy_signal_rows_when_entries_allowed": qualified_count,
        "buy_signal_rows_blocked_by_regime": 0,
        "buy_signal_rows_blocked_by_market": 0,
        "buy_signal_rows_blocked_by_both": 0,
        "buy_signal_rows_when_cash_override": 0,
        "capacity_truncated_signals": 0,
        "entry_attempts": len(outcomes),
        "entries_executed": int(rejection_counts.get("entries_executed", 0)),
        **{name: int(rejection_counts.get(name, 0)) for name in _REJECTION_NAMES},
        "eviction_attempts": 0,
        "evictions_executed": 0,
        "eviction_rejections": 0,
    }
    _same_json(diagnostics, expected_diagnostics, label="recomputed execution diagnostics")
    if diagnostics != manifest.get("execution_diagnostics"):
        _fail("manifest diagnostics differ from completed checkpoint")
    _same_serialized_json(
        checkpoint["result_config"], manifest.get("canslim_config"),
        label="manifest/checkpoint CANSLIM config",
    )
    config = checkpoint["result_config"]
    _same_serialized_json(
        config, bundle_context["expected_config"], label="full stable result_config"
    )
    return {
        "signals": signals, "outcomes": outcomes, "transactions": transactions,
        "weekly": weekly, "equity": equity, "funnel": funnel,
        "sessions": session_index, "diagnostics": diagnostics,
        "factual_coverage_counts": factual["coverage_counts"],
    }


def _audit_reconciliation(args: argparse.Namespace, frames: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    _add_repo_to_path(args.repo_root)
    from core.pit_baseline_report import reconcile_signals_to_transactions  # pylint: disable=import-outside-toplevel

    actual = reconcile_signals_to_transactions(
        frames["signals"], frames["transactions"], frames["diagnostics"],
        entry_outcomes=frames["outcomes"].where(
            pd.notna(frames["outcomes"]), None
        ).to_dict("records"),
        trading_days=frames["sessions"],
    )
    _same_json(actual, manifest.get("execution_reconciliation"), label="public reconciliation")
    if actual["unattributed_rejection_count"] != 0 or actual["unattributed_cash_capacity_count"] != 0:
        _fail("reconciliation contains an unattributed rejection")
    if actual["capacity_blocked_count"] != 0:
        _fail("uncapped Task 6 baseline contains a capacity-blocked outcome")
    return actual


def _equity_metrics(frame: pd.DataFrame, column: str) -> dict[str, float]:
    dates = _as_date_series(frame, "date", label=f"{column} equity")
    values = pd.to_numeric(frame[column], errors="coerce").astype(float)
    if values.isna().any() or (~np.isfinite(values)).any() or (values <= 0).any():
        _fail(f"{column} equity contains invalid values")
    returns = values.pct_change().dropna()
    days = max((dates.iloc[-1] - dates.iloc[0]).days, 1)
    total = float((values.iloc[-1] / values.iloc[0] - 1.0) * 100.0)
    annualized = float(((values.iloc[-1] / values.iloc[0]) ** (365.0 / days) - 1.0) * 100.0)
    sharpe = 0.0 if returns.empty or returns.std() == 0 else float(
        returns.mean() / returns.std() * np.sqrt(252)
    )
    drawdown = float(((values - values.cummax()) / values.cummax()).min() * 100.0)
    return {
        "total_return_pct": total,
        "annualized_return_pct": annualized,
        "max_drawdown_pct": drawdown,
        "sharpe_ratio": sharpe,
    }


def _average_cash_pct(weekly: pd.DataFrame) -> float:
    required = {"Week_Ending", "Cash", "Total_Equity"}
    if weekly.empty or not required.issubset(weekly):
        _fail("weekly holdings lack cash/equity observations")
    cash = pd.to_numeric(weekly["Cash"], errors="coerce").astype(float)
    equity = pd.to_numeric(weekly["Total_Equity"], errors="coerce").astype(float)
    if cash.isna().any() or equity.isna().any() or (~np.isfinite(cash)).any() or (
        ~np.isfinite(equity)
    ).any() or (cash < 0).any() or (equity <= 0).any():
        _fail("weekly holdings contain invalid cash/equity values")
    return float((cash / equity).mean() * 100.0)


def _identity_key(contract: Any, ticker: str) -> str:
    identity = contract.identities.get(str(ticker).upper())
    if identity is None or identity.continuity_kind not in _SAME_ISSUER_KINDS:
        return f"ticker:{str(ticker).upper()}"
    return f"chain:{identity.chain_id}"


def _normalized_value(value: object) -> object:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, str):
        return None if value == "" else value
    if isinstance(value, bool) or value.__class__.__name__ == "bool_":
        return ("bool", bool(value))
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return ("number", format(number, ".17g"))
    if pd.isna(value):
        return None
    return str(value)


def _normalized_frame_iterator(frame: pd.DataFrame):
    for row in frame.itertuples(index=False, name=None):
        yield tuple(_normalized_value(value) for value in row)


def _normalized_record_tuple(record: Mapping[str, Any], columns: Sequence[str]) -> tuple[object, ...]:
    if not set(record).issubset(columns):
        _fail("journal/checkpoint record has fields absent from its CSV schema")
    return tuple(_normalized_value(record.get(column)) for column in columns)


def _compare_record_sequence_to_frame(
    records: Sequence[Mapping[str, Any]],
    frame: pd.DataFrame,
    *,
    label: str,
) -> None:
    if len(records) != len(frame):
        _fail(f"{label} count differs from CSV")
    expected_rows = _normalized_frame_iterator(frame)
    for number, (record, expected) in enumerate(
        zip(records, expected_rows, strict=True), start=1
    ):
        if not isinstance(record, Mapping):
            _fail(f"{label} row {number} is not an object")
        if _normalized_record_tuple(record, tuple(frame.columns)) != expected:
            _fail(f"{label} differs from CSV at row {number}")


def _compare_frames(actual: pd.DataFrame, expected: pd.DataFrame, *, label: str) -> None:
    if tuple(actual.columns) != tuple(expected.columns) or len(actual) != len(expected):
        _fail(f"{label} schema/count differs from regenerated artifact")
    actual_rows = _normalized_frame_iterator(actual)
    expected_rows = _normalized_frame_iterator(expected)
    for number, (actual_row, expected_row) in enumerate(
        zip(actual_rows, expected_rows, strict=True), start=1
    ):
        if actual_row != expected_row:
            _fail(f"{label} differs from regenerated artifact at row {number}")


def _regenerate_leader_artifacts(
    args: argparse.Namespace,
    run_dir: Path,
    bundle_context: Mapping[str, Any],
) -> dict[str, Any]:
    _add_repo_to_path(args.repo_root)
    from core.leader_evaluation import (  # pylint: disable=import-outside-toplevel
        LeaderIdentityContract,
        label_five_year_leaders,
        label_rolling_leaders,
    )
    from core.pit_baseline_report import (  # pylint: disable=import-outside-toplevel
        five_year_leaders_frame,
        rolling_leaders_frame,
    )
    from core.pit_data import PITDataBundle  # pylint: disable=import-outside-toplevel

    expected_identity_sha = bundle_context["manifest"]["metadata"][
        "prices_provenance_sha256"
    ]
    contract = LeaderIdentityContract.from_prices_provenance(
        args.prices_provenance, expected_sha256=expected_identity_sha
    )
    with PITDataBundle(args.pit_bundle, expected_sha256=args.expected_bundle_sha256) as bundle:
        leaders = label_five_year_leaders(
            bundle_context["all_closes"], bundle.membership,
            start_date=_START, end_date=_END, identity_contract=contract, top_n=100,
        )
        rolling = label_rolling_leaders(
            bundle_context["all_closes"], bundle.membership,
            start_date=_START, end_date=_END, identity_contract=contract, top_n=100,
        )
    expected_leaders = five_year_leaders_frame(leaders)
    expected_rolling = rolling_leaders_frame(rolling)
    actual_leaders = pd.read_csv(run_dir / "five_year_leaders.csv")
    actual_rolling = pd.read_csv(run_dir / "rolling_leader_labels.csv")
    _compare_frames(actual_leaders, expected_leaders, label="five-year leaders")
    _compare_frames(actual_rolling, expected_rolling, label="rolling leader labels")
    return {
        "leaders": expected_leaders,
        "rolling": expected_rolling,
        "identity_contract": contract,
    }


def _recompute_recall(
    args: argparse.Namespace,
    run_dir: Path,
    frames: Mapping[str, Any],
    leader_context: Mapping[str, Any],
) -> dict[str, Any]:
    del args
    leaders = leader_context["leaders"].copy()
    rolling = leader_context["rolling"].copy()
    recall = pd.read_csv(run_dir / "leader_recall.csv")
    if len(leaders) != 100 or leaders["ticker"].duplicated().any() or set(leaders["rank"]) != set(range(1, 101)):
        _fail("five-year leader artifact is not the exact top 100")
    if len(rolling) != 4_800:
        _fail("rolling leader artifact does not contain 4,800 labels")
    if set(recall["ticker"]) != set(leaders["ticker"]) or len(recall) != len(leaders):
        _fail("leader recall artifact does not exactly cover five-year leaders")
    member_at_start = _strict_bool(leaders, "member_at_start", label="five-year leaders")
    member_at_evaluation = _strict_bool(
        rolling, "member_at_evaluation", label="rolling leaders"
    )
    contract = leader_context["identity_contract"]
    signal_keys = {
        _identity_key(contract, symbol)
        for symbol in frames["signals"].loc[
            _strict_bool(frames["signals"], "buy_signal", label="signals"), "symbol"
        ]
    }
    buy_transactions = frames["transactions"].loc[
        frames["transactions"]["Action"].astype(str).str.upper().eq("BUY")
    ]
    execution_keys = {
        _identity_key(contract, symbol) for symbol in buy_transactions["Ticker"]
    }
    leader_keys = leaders["ticker"].map(lambda value: _identity_key(contract, value))
    signaled = leader_keys.isin(signal_keys)
    executed = leader_keys.isin(execution_keys)
    raw_denominator = len(leaders)
    exposed_denominator = int(member_at_start.sum())

    signal_rows = frames["signals"].loc[
        _strict_bool(frames["signals"], "buy_signal", label="signals"),
        ["symbol", "signal_date"],
    ].copy()
    signal_rows["key"] = signal_rows["symbol"].map(lambda value: _identity_key(contract, value))
    signal_rows["signal_date"] = pd.to_datetime(signal_rows["signal_date"], errors="raise").dt.normalize()
    dates_by_key = {
        key: np.sort(
            pd.DatetimeIndex(group["signal_date"]).as_unit("ns").asi8
        )
        for key, group in signal_rows.groupby("key", sort=False)
    }
    rolling_eval = pd.to_datetime(rolling["evaluation_date"], errors="raise").dt.normalize()
    rolling_keys = rolling["ticker"].map(lambda value: _identity_key(contract, value))
    rolling_recalled = pd.Series(False, index=rolling.index)
    window_ns = int(timedelta(days=20).total_seconds() * 1_000_000_000)
    for key, indexes in rolling.groupby(rolling_keys, sort=False).groups.items():
        signal_dates = dates_by_key.get(key)
        if signal_dates is None or len(signal_dates) == 0:
            continue
        positions = np.asarray(list(indexes), dtype=int)
        evaluation_values = (
            pd.DatetimeIndex(rolling_eval.iloc[positions]).as_unit("ns").asi8
        )
        prior = np.searchsorted(signal_dates, evaluation_values, side="right") - 1
        found = prior >= 0
        recalled = np.zeros(len(positions), dtype=bool)
        recalled[found] = evaluation_values[found] - signal_dates[prior[found]] <= window_ns
        rolling_recalled.iloc[positions] = recalled

    def pct(numerator: int, denominator: int) -> float:
        return numerator / denominator * 100.0 if denominator else 0.0

    return {
        "five_year": {
            "raw_denominator": raw_denominator,
            "raw_signaled_numerator": int(signaled.sum()),
            "raw_executed_numerator": int(executed.sum()),
            "raw_signal_recall_pct": pct(int(signaled.sum()), raw_denominator),
            "raw_execution_recall_pct": pct(int(executed.sum()), raw_denominator),
            "pit_exposed_denominator": exposed_denominator,
            "pit_exposed_signaled_numerator": int((signaled & member_at_start).sum()),
            "pit_exposed_executed_numerator": int((executed & member_at_start).sum()),
            "pit_exposed_signal_recall_pct": pct(
                int((signaled & member_at_start).sum()), exposed_denominator
            ),
            "pit_exposed_execution_recall_pct": pct(
                int((executed & member_at_start).sum()), exposed_denominator
            ),
        },
        "rolling": {
            "raw_denominator": len(rolling),
            "raw_recalled_numerator": int(rolling_recalled.sum()),
            "raw_recall_pct": pct(int(rolling_recalled.sum()), len(rolling)),
            "pit_exposed_denominator": int(member_at_evaluation.sum()),
            "pit_exposed_recalled_numerator": int(
                (rolling_recalled & member_at_evaluation).sum()
            ),
            "pit_exposed_recall_pct": pct(
                int((rolling_recalled & member_at_evaluation).sum()),
                int(member_at_evaluation.sum()),
            ),
        },
    }


def _audit_journals(
    layout: _StateLayout,
    checkpoint: Mapping[str, Any],
    frames: Mapping[str, Any],
) -> None:
    indices = {"signals": 0, "entry_outcomes": 0, "transactions": 0, "weekly": 0}
    frame_names = {
        "signals": "signals", "entry_outcomes": "outcomes",
        "transactions": "transactions", "weekly": "weekly",
    }
    frame_iterators = {
        field: iter(_normalized_frame_iterator(frames[frame_name]))
        for field, frame_name in frame_names.items()
    }
    frame_columns = {
        field: tuple(frames[frame_name].columns)
        for field, frame_name in frame_names.items()
    }
    equity_rows = iter(frames["equity"][["date", "portfolio", "benchmark"]].itertuples(
        index=False, name=None
    ))

    def consume(field: str, record: Mapping[str, Any]) -> None:
        try:
            expected = next(frame_iterators[field])
        except StopIteration as exc:
            raise AssertionError(f"state journal has excess {field} rows") from exc
        actual = _normalized_record_tuple(record, frame_columns[field])
        if actual != expected:
            _fail(f"state journal {field} differs from CSV at row {indices[field] + 1}")
        indices[field] += 1

    checkpoints: dict[int, tuple[int, int]] = {}
    final_seen = False
    day_count = 0
    last_equity: float | None = None
    for _line, event in _jsonl_records(layout.state_path, label="portfolio state log"):
        if event.get("kind") == "day":
            if final_seen or set(event) != {
                "kind", "day_index", "date", "equity", "benchmark", "signals",
                "entry_outcomes", "transactions", "weekly",
            }:
                _fail("state journal day schema/order is invalid")
            if event["day_index"] != day_count:
                _fail("state journal day indexes are not contiguous")
            try:
                equity_date, expected_equity, expected_benchmark = next(equity_rows)
            except StopIteration as exc:
                raise AssertionError("state journal has excess day rows") from exc
            expected_date = str(frames["sessions"][day_count].date())
            if str(equity_date) != expected_date:
                _fail("equity iterator date differs from immutable SPY session")
            if event["date"] != expected_date:
                _fail("state journal date differs from immutable SPY session")
            _close_number(event["equity"], expected_equity, label="state journal equity")
            _close_number(event["benchmark"], expected_benchmark,
                          label="state journal benchmark")
            last_equity = float(expected_equity)
            for field in frame_names:
                records = event[field]
                if not isinstance(records, list):
                    _fail(f"state journal {field} is not a list")
                for record in records:
                    if not isinstance(record, dict):
                        _fail(f"state journal {field} contains a non-object")
                    consume(field, record)
            if (day_count + 1) % 20 == 0 or day_count == _SESSION_COUNT - 1:
                checkpoints[day_count] = (indices["signals"], indices["transactions"])
            day_count += 1
        elif event.get("kind") == "final":
            if final_seen or day_count != _SESSION_COUNT or set(event) != {"kind", "transactions"}:
                _fail("state journal final record is misplaced or malformed")
            final_seen = True
            if not isinstance(event["transactions"], list):
                _fail("state journal final transactions are not a list")
            for record in event["transactions"]:
                if not isinstance(record, dict):
                    _fail("state journal final transaction is not an object")
                consume("transactions", record)
        else:
            _fail("state journal contains an unknown event")
    if day_count != _SESSION_COUNT or not final_seen:
        _fail("state journal lacks 1255 days and one terminal final event")
    for field, frame_name in frame_names.items():
        if indices[field] != len(frames[frame_name]):
            _fail(f"state journal {field} count differs from CSV")
        try:
            next(frame_iterators[field])
        except StopIteration:
            pass
        else:
            _fail(f"state journal omits {field} CSV rows")
    try:
        next(equity_rows)
    except StopIteration:
        pass
    else:
        _fail("state journal omits equity CSV rows")
    if last_equity is None:
        _fail("state journal has no terminal equity")
    _close_number(checkpoint.get("equity"), last_equity,
                  label="terminal checkpoint equity")

    expected_days = list(range(19, _SESSION_COUNT, 20))
    if expected_days[-1] != _SESSION_COUNT - 1:
        expected_days.append(_SESSION_COUNT - 1)
    progress = iter(_jsonl_records(
        layout.progress_path, label="portfolio progress"
    ))
    universe_count = len(checkpoint["result_config"]["tickers"]) + 1
    try:
        started = next(progress)[1]
    except StopIteration as exc:
        raise AssertionError("progress journal lacks started event") from exc
    if set(started) != {"phase", "fingerprint", "start_date", "end_date", "universe_count"}:
        _fail("progress started event schema differs")
    if started != {
        "phase": "started", "fingerprint": checkpoint.get("fingerprint"),
        "start_date": _START, "end_date": _END,
        "universe_count": universe_count,
    }:
        _fail("progress started event differs from fixed replay")

    def next_progress_event(*, label: str) -> Mapping[str, Any]:
        try:
            return next(progress)[1]
        except StopIteration as exc:
            raise AssertionError(f"progress journal lacks {label}") from exc

    for day_index in expected_days:
        event = next_progress_event(label="a periodic checkpoint")
        if set(event) != {
            "phase", "day_index", "date", "next_day_index", "total_days", "percent",
            "elapsed_seconds", "open_positions", "signal_rows", "transactions",
        }:
            _fail("progress checkpoint event schema differs")
        expected_signal_rows, expected_transactions = checkpoints[day_index]
        if (
            event["phase"] != "checkpoint" or event["day_index"] != day_index
            or event["date"] != str(frames["sessions"][day_index].date())
            or event["next_day_index"] != day_index + 1
            or event["total_days"] != _SESSION_COUNT
            or event["percent"] != round((day_index + 1) * 100.0 / _SESSION_COUNT, 3)
            or event["signal_rows"] != expected_signal_rows
            or event["transactions"] != expected_transactions
        ):
            _fail("progress checkpoint counters differ from streamed state")
        _nonnegative_int(event["open_positions"], label="progress open positions")
        if float(event["elapsed_seconds"]) < 0 or not math.isfinite(float(event["elapsed_seconds"])):
            _fail("progress elapsed_seconds is invalid")
    completed = next_progress_event(label="a completed event")
    if set(completed) != {
        "phase", "next_day_index", "total_days", "percent", "elapsed_seconds",
        "open_positions", "signal_rows", "transactions",
    }:
        _fail("progress completed event schema differs")
    if (
        completed["phase"] != "completed" or completed["next_day_index"] != _SESSION_COUNT
        or completed["total_days"] != _SESSION_COUNT or completed["percent"] != 100.0
        or completed["open_positions"] != 0
        or completed["signal_rows"] != len(frames["signals"])
        or completed["transactions"] != len(frames["transactions"])
    ):
        _fail("progress completed event does not reconcile terminal state")
    if float(completed["elapsed_seconds"]) < 0 or not math.isfinite(
        float(completed["elapsed_seconds"])
    ):
        _fail("completed progress elapsed_seconds is invalid")
    try:
        next(progress)
    except StopIteration:
        pass
    else:
        _fail("fresh replay progress journal contains an event after completion")


def _expected_leader_recall_publication(
    recomputed: Mapping[str, Any],
) -> dict[str, Any]:
    five = recomputed["five_year"]
    rolling = recomputed["rolling"]
    raw_five = {
        "denominator_count": five["raw_denominator"],
        "signaled_count": five["raw_signaled_numerator"],
        "signal_recall_pct": five["raw_signal_recall_pct"],
        "executed_count": five["raw_executed_numerator"],
        "execution_recall_pct": five["raw_execution_recall_pct"],
    }
    exposed_five = {
        "denominator_count": five["pit_exposed_denominator"],
        "signaled_count": five["pit_exposed_signaled_numerator"],
        "signal_recall_pct": five["pit_exposed_signal_recall_pct"],
        "executed_count": five["pit_exposed_executed_numerator"],
        "execution_recall_pct": five["pit_exposed_execution_recall_pct"],
    }
    raw_rolling = {
        "denominator_count": rolling["raw_denominator"],
        "signaled_count": rolling["raw_recalled_numerator"],
        "signal_recall_pct": rolling["raw_recall_pct"],
    }
    exposed_rolling = {
        "denominator_count": rolling["pit_exposed_denominator"],
        "signaled_count": rolling["pit_exposed_recalled_numerator"],
        "signal_recall_pct": rolling["pit_exposed_recall_pct"],
    }
    return {
        "five_year": {
            "raw_all": raw_five,
            "pit_exposed_member_at_start": exposed_five,
        },
        "rolling": {
            "raw_all": raw_rolling,
            "pit_exposed_member_at_evaluation": exposed_rolling,
        },
        "top100_signaled": raw_five["signaled_count"],
        "top100_executed": raw_five["executed_count"],
        "signal_recall_pct": raw_five["signal_recall_pct"],
        "execution_recall_pct": raw_five["execution_recall_pct"],
        "rolling_label_recall_pct": raw_rolling["signal_recall_pct"],
        "deprecated_raw_count_aliases": {
            "top100_signaled": "five_year.raw_all.signaled_count",
            "top100_executed": "five_year.raw_all.executed_count",
        },
        "compatibility_aliases": {
            "signal_recall_pct": "five_year.raw_all.signal_recall_pct",
            "execution_recall_pct": "five_year.raw_all.execution_recall_pct",
            "rolling_label_recall_pct": "rolling.raw_all.signal_recall_pct",
        },
    }


def _expected_recall_report_text(recall: Mapping[str, Any]) -> str:
    five_raw = recall["five_year"]["raw_all"]
    five_pit = recall["five_year"]["pit_exposed_member_at_start"]
    rolling_raw = recall["rolling"]["raw_all"]
    rolling_pit = recall["rolling"]["pit_exposed_member_at_evaluation"]
    return "\n".join(
        (
            "- Five-year raw/all: "
            f"{five_raw['signaled_count']}/{five_raw['denominator_count']} signaled "
            f"({five_raw['signal_recall_pct']:.2f}%); "
            f"{five_raw['executed_count']}/{five_raw['denominator_count']} executed "
            f"({five_raw['execution_recall_pct']:.2f}%).",
            "- Five-year PIT-exposed (`member_at_start=True`): "
            f"{five_pit['signaled_count']}/{five_pit['denominator_count']} signaled "
            f"({five_pit['signal_recall_pct']:.2f}%); "
            f"{five_pit['executed_count']}/{five_pit['denominator_count']} executed "
            f"({five_pit['execution_recall_pct']:.2f}%).",
            "- Rolling raw/all signal recall: "
            f"{rolling_raw['signaled_count']}/{rolling_raw['denominator_count']} "
            f"({rolling_raw['signal_recall_pct']:.2f}%).",
            "- Rolling PIT-exposed (`member_at_evaluation=True`) signal recall: "
            f"{rolling_pit['signaled_count']}/{rolling_pit['denominator_count']} "
            f"({rolling_pit['signal_recall_pct']:.2f}%).",
        )
    )


def _require_recall_publication(
    *,
    recomputed: Mapping[str, Any],
    summary_recall: object,
    manifest_recall: object,
    report: str,
) -> dict[str, Any]:
    expected = _expected_leader_recall_publication(recomputed)
    _same_json(summary_recall, expected, label="summary leader recall")
    _same_json(manifest_recall, expected, label="manifest leader recall")
    for line in _expected_recall_report_text(expected).splitlines():
        if report.count(line) != 1:
            _fail("report recall does not contain each exact recomputed denominator line")
    return expected


def _summary(
    args: argparse.Namespace,
    run_dir: Path,
    manifest: Mapping[str, Any],
    frames: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    leader_context: Mapping[str, Any],
) -> dict[str, Any]:
    summary = _json(run_dir / "summary.json", label="summary artifact")
    contract = summary.get("entry_contract")
    if not isinstance(contract, dict):
        _fail("summary lacks entry_contract metrics")
    expected_contract = {
        "outcome_schema_version": _OUTCOME_SCHEMA_VERSION,
        "daily_session_count": _SESSION_COUNT,
        "evaluated_symbol_days": int(frames["funnel"]["evaluated_count"].sum()),
        "qualified_signals": int(frames["funnel"]["qualified_count"].sum()),
        "attempted_signals": int(frames["funnel"]["attempted_count"].sum()),
        "executed_attempts": int(frames["funnel"]["executed_count"].sum()),
        "rejected_attempts": int(frames["funnel"]["rejected_count"].sum()),
        "next_open_buy_zone_rejections": reconciliation["next_open_buy_zone_rejected_count"],
        "rejection_counts": reconciliation["rejection_counts"],
    }
    _same_json(contract, expected_contract, label="summary entry-contract metrics")
    portfolio_metrics = _equity_metrics(frames["equity"], "portfolio")
    benchmark_metrics = _equity_metrics(frames["equity"], "benchmark")
    average_cash = _average_cash_pct(frames["weekly"])
    canslim_summary = summary.get("canslim")
    if not isinstance(canslim_summary, dict):
        _fail("summary lacks CANSLIM metrics")
    for key, expected in portfolio_metrics.items():
        _close_number(canslim_summary.get(key), expected, label=f"summary CANSLIM {key}")
    _close_number(canslim_summary.get("average_cash_pct"), average_cash,
                  label="summary average cash")
    _close_number(summary.get("spy", {}).get("total_return_pct"),
                  benchmark_metrics["total_return_pct"], label="summary SPY return")
    closed_trades = [trade for trade in checkpoint.get("trades", []) if trade.get("exit_price") is not None]
    wins = 0
    for trade in closed_trades:
        pnl = float(trade.get("realized_pnl", 0.0)) + (
            float(trade["exit_price"]) - float(trade["entry_price"])
        ) * float(trade.get("remaining_qty") or 0.0)
        wins += int(pnl > 0)
    expected_win_rate = wins / len(closed_trades) * 100.0 if closed_trades else 0.0
    if canslim_summary.get("closed_trades") != len(closed_trades):
        _fail("summary closed-trade count differs from checkpoint trades")
    _close_number(canslim_summary.get("win_rate_pct"), expected_win_rate,
                  label="summary win rate")
    recall = _recompute_recall(args, run_dir, frames, leader_context)
    recall_summary = summary.get("leader_recall")
    if not isinstance(recall_summary, dict):
        _fail("summary lacks leader recall")
    _require_recall_publication(
        recomputed=recall,
        summary_recall=recall_summary,
        manifest_recall=manifest.get("leader_recall"),
        report=(run_dir / "report.md").read_text(encoding="utf-8"),
    )
    return {
        "git_head": manifest["git_head"],
        "bundle_sha256": manifest["bundle_sha256"],
        "daily_sessions": _SESSION_COUNT,
        "evaluated_symbol_days": expected_contract["evaluated_symbol_days"],
        "qualified_signals": expected_contract["qualified_signals"],
        "attempts": expected_contract["attempted_signals"],
        "executions": expected_contract["executed_attempts"],
        "rejections": expected_contract["rejected_attempts"],
        "rejection_counts": {
            name: int(frames["diagnostics"][name]) for name in _REJECTION_NAMES
        },
        "next_open_buy_zone_rejections": expected_contract["next_open_buy_zone_rejections"],
        "cash_rejections": reconciliation["cash_blocked_count"],
        "final_pending": reconciliation["final_pending_count"],
        "recomputed_portfolio_metrics": portfolio_metrics,
        "recomputed_benchmark_metrics": benchmark_metrics,
        "recomputed_average_cash_pct": average_cash,
        "recomputed_leader_recall": recall,
        "legacy_count_aliases": {
            "signal_recall_pct": recall_summary.get("signal_recall_pct"),
            "execution_recall_pct": recall_summary.get("execution_recall_pct"),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Task 6 corrected-replay audit")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--expected-replay-git-sha", required=True)
    parser.add_argument("--expected-bundle-sha256", default=_BUNDLE_SHA256)
    parser.add_argument("--regeneration-audit", required=True)
    parser.add_argument("--pit-bundle", required=True)
    parser.add_argument("--bundle-manifest", required=True)
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--prices-csv", required=True)
    parser.add_argument("--fundamentals-csv", required=True)
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--fundamentals-provenance", required=True)
    parser.add_argument("--fundamentals-coverage", required=True)
    parser.add_argument("--security-master", required=True)
    parser.add_argument("--security-master-exclusions", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.run_dir = _directory(args.run_dir, label="run directory")
    args.repo_root = _directory(args.repo_root, label="repository root")
    for key in (
        "pit_bundle", "bundle_manifest", "regeneration_audit", "membership_csv",
        "prices_csv", "fundamentals_csv",
        "membership_provenance", "prices_provenance", "fundamentals_provenance",
        "fundamentals_coverage", "security_master", "security_master_exclusions",
    ):
        setattr(args, key, _regular_file(getattr(args, key), label=key.replace("_", " ")))
    args.expected_bundle_sha256 = args.expected_bundle_sha256.lower()
    if args.expected_bundle_sha256 != _BUNDLE_SHA256:
        _fail("Task 6 auditor is bound to the corrected rebuilt-bundle SHA-256")
    if len(args.expected_replay_git_sha) != 40 or any(
        char not in "0123456789abcdefABCDEF"
        for char in args.expected_replay_git_sha
    ):
        _fail("--expected-replay-git-sha must be a full 40-character commit SHA")
    args.expected_replay_git_sha = args.expected_replay_git_sha.lower()
    if args.expected_replay_git_sha != _REGENERATION_GIT_SHA:
        _fail("Task 6 replay must use the reviewed correction producer Git SHA")
    audited_source_hashes = {
        name: _sha256(path) for name, path in _audited_source_paths(args).items()
    }
    manifest = _audit_manifest(args, args.run_dir)
    layout = _state_layout(run_dir=args.run_dir, manifest=manifest)
    _audit_physical_set(args.run_dir, manifest, layout)
    final_manifest_sha256 = _sha256(args.run_dir / "run_manifest.json")
    bundle_context = _audit_bundle(args)
    _same_json(
        manifest.get("bundle_metadata"), bundle_context["manifest"]["metadata"],
        label="manifest bundle metadata",
    )
    checkpoint = _audit_checkpoint(layout, manifest, bundle_context)
    frames = _audit_frames(
        args, args.run_dir, layout, checkpoint, manifest, bundle_context
    )
    coverage = _audit_coverage(
        args.run_dir, manifest, bundle_context, signals=frames["signals"],
        factual_coverage_counts=frames["factual_coverage_counts"],
    )
    _audit_journals(layout, checkpoint, frames)
    reconciliation = _audit_reconciliation(args, frames, manifest)
    leader_context = _regenerate_leader_artifacts(
        args, args.run_dir, bundle_context
    )
    measured = _summary(
        args, args.run_dir, manifest, frames, reconciliation, checkpoint,
        leader_context,
    )
    measured["coverage_exception"] = coverage["non_blocking_failed_gates"]
    for key, path in _replay_input_paths(args).items():
        if _sha256(path) != manifest["input_sha256"][key]:
            _fail(f"hash-bound input changed during audit: {key}")
    for key, path in _audited_source_paths(args).items():
        if _sha256(path) != audited_source_hashes[key]:
            _fail(f"audited source changed during audit: {key}")
    if _git_identity(args.repo_root) != manifest["git_head"]:
        _fail("repository identity changed during audit")
    _final_revalidate_run(
        args.run_dir,
        manifest,
        layout,
        expected_manifest_sha256=final_manifest_sha256,
    )
    measured.update({
        "fresh_single_directory_state": True,
        "final_run_dir": str(args.run_dir),
        "journal_run_dir": str(layout.state_dir),
        "checkpoint_path": str(layout.checkpoint_path),
        "final_manifest_sha256": final_manifest_sha256,
        "state_sha256": _state_hashes(layout),
        "checkpoint_fingerprint": checkpoint["fingerprint"],
        "state_result_equivalence": True,
    })
    print(json.dumps(measured, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"Task 6 audit failed closed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
