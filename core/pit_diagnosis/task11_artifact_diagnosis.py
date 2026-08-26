"""Read-only aggregate diagnosis of the sealed Task 11 replay.

This module deliberately reads the ledgers emitted by the completed Task 11
backtest only after :func:`verify_baseline_run` has authenticated the immutable
publication.  It is not a strategy runner and never reconstructs entry logic
from prices, fundamentals, or the cached diagnosis fact store.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import date
import io
import json
import math
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .baseline import (
    STRICT_PROPER_BASE_TASK11_PROFILE_ID,
    BaselineAuthority,
    BaselineAuthorityProfile,
    BaselineSnapshot,
    _artifact_byte_limit,
    _authenticated_byte_snapshot,
    _regular_directory,
    _regular_file,
    resolve_baseline_authority_profile,
    verify_baseline_run,
)


TASK11_ARTIFACT_DIAGNOSIS_SCHEMA_VERSION = 1
_FULL_WINDOW = ("full_2021_2025", date(2021, 1, 1), date(2025, 12, 31))
_FOCUS_WINDOW = ("focus_2023_2025", date(2023, 1, 1), date(2025, 12, 31))
_WINDOWS = (_FULL_WINDOW, _FOCUS_WINDOW)
_ENTRY_OUTCOME_EXECUTED = "entries_executed"
_ENTRY_OUTCOME_REJECTIONS = (
    "entry_rejected_already_open",
    "entry_rejected_capacity",
    "entry_rejected_invalid_price",
    "entry_rejected_invalid_risk",
    "entry_rejected_missing_data",
    "entry_rejected_next_open_buy_zone",
    "entry_rejected_no_cash",
)
_ALLOWED_ENTRY_OUTCOMES = frozenset(
    (_ENTRY_OUTCOME_EXECUTED, *_ENTRY_OUTCOME_REJECTIONS)
)
_SIGNAL_COLUMNS = frozenset(
    {
        "signal_date",
        "technical_setup_eligible",
        "current_growth",
        "annual_growth",
        "rs_score",
        "entry_composite_score",
        "entry_contract_eligible",
    }
)
_DAILY_FUNNEL_COLUMNS = frozenset(
    {
        "signal_date",
        "evaluated_count",
        "qualified_count",
        "attempted_count",
        "executed_count",
        "rejected_count",
    }
)
_OUTCOME_COLUMNS = frozenset({"signal_date", "entry_date", "outcome"})
_WEEKLY_HOLDINGS_COLUMNS = frozenset(
    {"Week_Ending", "Holding_Count", "Cash", "Total_Equity"}
)
_TRANSACTION_COLUMNS = frozenset({"Date", "Action", "Reason"})
_CONSUMED_ARTIFACT_NAMES = (
    "summary.json",
    "canslim_signals.csv",
    "daily_entry_funnel.csv",
    "entry_attempt_outcomes.csv",
    "weekly_holdings.csv",
    "transactions.csv",
)


@dataclass(frozen=True)
class _SealedSource:
    """One static Task 11 source that must remain digest-bound through parsing."""

    name: str
    path: Path
    expected_sha256: str


def diagnose_task11_artifacts(
    run_dir: Path, profile: BaselineAuthorityProfile
) -> dict[str, object]:
    """Return aggregate-only evidence from the sealed Task 11 replay.

    Args:
        run_dir: Candidate completed replay directory.  It must match the
            immutable Task 11 authority exactly.
        profile: The selected closed baseline authority profile.

    Returns:
        A JSON-serializable, deterministic engine/contract diagnosis.

    Raises:
        ValueError: If the profile is not Task 11 or any sealed artifact fails
            verification or internal reconciliation.
    """

    if type(profile) is not BaselineAuthorityProfile:
        raise ValueError("Task 11 artifact diagnosis requires a baseline authority profile")
    canonical_profile = resolve_baseline_authority_profile(
        STRICT_PROPER_BASE_TASK11_PROFILE_ID
    )
    if type(profile.authority) is not BaselineAuthority or (
        profile.profile_id != canonical_profile.profile_id
        or profile.scope != canonical_profile.scope
        or profile.fidelity_label != canonical_profile.fidelity_label
        or profile.fidelity_reason != canonical_profile.fidelity_reason
        or profile.authority != canonical_profile.authority
    ):
        raise ValueError(
            "Task 11 artifact diagnosis requires the exact canonical "
            "strict-proper-base-task11 authority profile"
        )

    # The caller-supplied object is a boundary proof only.  All subsequent reads,
    # verification, source bindings, and published labels come from the resolver's
    # closed canonical profile rather than the caller's instance.
    authority = canonical_profile.authority

    # This is intentionally first: subsequent paths are fixed filenames under the
    # hash-verified run directory, never paths named by an artifact's CSV contents.
    snapshot = verify_baseline_run(Path(run_dir), authority)
    _require_snapshot_authority_match(snapshot, authority)
    sources = _consumed_sources(snapshot, authority)
    manifest = _load_json(sources["run_manifest.json"])
    config = _mapping_value(manifest, "canslim_config", "verified run manifest")
    contract = _contract_from_manifest(config)

    signal_windows = _diagnose_signals(
        sources["canslim_signals.csv"], contract["entry_thresholds"]
    )
    daily_windows = _diagnose_daily_funnel(sources["daily_entry_funnel.csv"])
    outcome_windows = _diagnose_entry_outcomes(sources["entry_attempt_outcomes.csv"])
    weekly_windows = _diagnose_weekly_holdings(sources["weekly_holdings.csv"])
    transaction_windows = _diagnose_transactions(sources["transactions.csv"])
    summary = _load_json(sources["summary.json"])

    for name, _, _ in _WINDOWS:
        _reconcile_window(
            name,
            signal_windows[name],
            daily_windows[name],
            outcome_windows[name],
        )
    rejection_counts = _reconcile_full_summary(
        summary,
        daily_windows[_FULL_WINDOW[0]],
        outcome_windows[_FULL_WINDOW[0]],
        weekly_windows[_FULL_WINDOW[0]],
    )

    windows = {
        name: _render_window(
            name,
            start,
            end,
            signal_windows[name],
            daily_windows[name],
            outcome_windows[name],
            weekly_windows[name],
            transaction_windows[name],
        )
        for name, start, end in _WINDOWS
    }
    full_daily = daily_windows[_FULL_WINDOW[0]]
    full_outcomes = outcome_windows[_FULL_WINDOW[0]]
    payload: dict[str, object] = {
        "schema_version": TASK11_ARTIFACT_DIAGNOSIS_SCHEMA_VERSION,
        "diagnosis_scope": "engine_contract_diagnosis_not_strategy_optimization",
        "profile": {
            "profile_id": canonical_profile.profile_id,
            "scope": canonical_profile.scope,
            "fidelity_label": canonical_profile.fidelity_label,
            "fidelity_reason": canonical_profile.fidelity_reason,
            "manifest_sha256": authority.manifest_sha256,
            "bundle_sha256": authority.bundle_sha256,
            "replay_git_head": authority.replay_git_head,
            "date_contract": dict(authority.date_contract),
        },
        "contract": contract,
        "execution_verdict": {
            "source": "entry_attempt_outcomes.csv",
            "qualified_signals": full_daily["qualified"],
            "attempted_signals": full_outcomes["attempted"],
            "executed_attempts": full_outcomes["executed"],
            "next_open_buy_zone_rejections": full_outcomes["rejection_counts"][
                "entry_rejected_next_open_buy_zone"
            ],
            "rejection_counts": rejection_counts,
            "execution_timing": "next_open_with_buy_zone_validation",
            "qualified_to_attempted_reconciled": True,
        },
        "windows": windows,
    }
    return payload


def _require_snapshot_authority_match(
    snapshot: BaselineSnapshot, authority: BaselineAuthority
) -> None:
    """Reject parsed verifier metadata unless it agrees with the closed authority."""

    if snapshot.manifest_sha256 != authority.manifest_sha256:
        raise ValueError("verified Task 11 manifest identity differs from canonical authority")
    if dict(snapshot.artifact_sha256) != dict(authority.artifact_sha256):
        raise ValueError("verified Task 11 artifact identities differ from canonical authority")


def _consumed_sources(
    snapshot: BaselineSnapshot, authority: BaselineAuthority
) -> dict[str, _SealedSource]:
    """Pin the fixed source set used by this reader to verified static paths."""

    _regular_directory(snapshot.run_dir, "verified Task 11 run directory")
    sources = {
        "run_manifest.json": _SealedSource(
            name="run_manifest.json",
            path=snapshot.run_dir / "run_manifest.json",
            expected_sha256=authority.manifest_sha256,
        )
    }
    for name in _CONSUMED_ARTIFACT_NAMES:
        expected = authority.artifact_sha256.get(name)
        if not isinstance(expected, str):
            raise ValueError(f"verified Task 11 publication does not bind {name}")
        sources[name] = _SealedSource(
            name=name,
            path=snapshot.run_dir / name,
            expected_sha256=expected,
        )
    for source in sources.values():
        _regular_file(source.path, f"sealed Task 11 {source.name}")
    return sources


@contextmanager
def _verified_byte_snapshot(source: _SealedSource) -> Iterator[BinaryIO]:
    """Yield an ephemeral copy only after its exact sealed bytes are authenticated."""

    with _authenticated_byte_snapshot(
        source.path,
        f"sealed Task 11 {source.name}",
        source.expected_sha256,
        _artifact_byte_limit(source.name),
    ) as snapshot_handle:
        yield snapshot_handle


def _load_json(source: _SealedSource) -> Mapping[str, Any]:
    """Load a hash-bound JSON mapping without accepting arbitrary shapes."""

    try:
        with _verified_byte_snapshot(source) as snapshot_handle:
            value = json.loads(snapshot_handle.read())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sealed Task 11 {source.name} cannot be read as JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"sealed Task 11 {source.name} must be a JSON object")
    return value


@contextmanager
def _verified_csv_reader(source: _SealedSource) -> Iterator[csv.DictReader]:
    """Yield a CSV reader over verified ephemeral bytes, never over a live path."""

    with _verified_byte_snapshot(source) as snapshot_handle:
        text_handle = io.TextIOWrapper(snapshot_handle, encoding="utf-8", newline="")
        try:
            yield csv.DictReader(text_handle)
        except UnicodeDecodeError as exc:
            raise ValueError(f"sealed Task 11 {source.name} cannot be decoded as CSV") from exc
        finally:
            text_handle.detach()


def _mapping_value(
    mapping: Mapping[str, Any], key: str, label: str
) -> Mapping[str, Any]:
    """Return a required JSON object from a verified mapping."""

    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} {key} must be a JSON object")
    return value


def _contract_from_manifest(config: Mapping[str, Any]) -> dict[str, object]:
    """Extract only contract fields emitted by the sealed engine manifest."""

    thresholds = {
        "current_growth": _required_finite_number(
            config, "entry_contract_min_current_growth", "canslim_config"
        ),
        "annual_growth": _required_finite_number(
            config, "entry_contract_min_annual_growth", "canslim_config"
        ),
        "rs_score": _required_finite_number(
            config, "entry_contract_min_rs_score", "canslim_config"
        ),
        "entry_composite_score": _required_finite_number(
            config, "entry_contract_min_composite_score", "canslim_config"
        ),
    }
    return {
        "source": "run_manifest.json.canslim_config",
        "entry_thresholds": thresholds,
        "daily_evaluation": {
            "signal_every_n_days": _required_nonnegative_integer(
                config, "signal_every_n_days", "canslim_config"
            ),
            "require_bullish_market": _required_boolean(
                config, "require_bullish_market", "canslim_config"
            ),
            "use_stateful_regime_gate": _required_boolean(
                config, "use_stateful_regime_gate", "canslim_config"
            ),
        },
        "sizing_and_capacity": {
            "max_positions": _optional_nonnegative_integer(
                config, "max_positions", "canslim_config"
            ),
            "cash_deployment_threshold_pct": _optional_finite_number(
                config, "cash_deployment_threshold_pct", "canslim_config"
            ),
            "position_risk_pct": _required_finite_number(
                config, "position_risk_pct", "canslim_config"
            ),
            "position_size_pct": _required_finite_number(
                config, "position_size_pct", "canslim_config"
            ),
            "stop_loss_pct": _required_finite_number(
                config, "stop_loss_pct", "canslim_config"
            ),
            "take_profit_pct": _required_finite_number(
                config, "take_profit_pct", "canslim_config"
            ),
        },
    }


def _diagnose_signals(
    source: _SealedSource, thresholds: object
) -> dict[str, dict[str, object]]:
    """Stream aggregate funnel counts from the actual signal ledger."""

    if not isinstance(thresholds, Mapping):
        raise ValueError("Task 11 entry thresholds are malformed")
    signal_thresholds = {
        key: _required_finite_number(thresholds, key, "Task 11 entry thresholds")
        for key in ("current_growth", "annual_growth", "rs_score", "entry_composite_score")
    }
    windows = {name: _new_signal_stats(signal_thresholds) for name, _, _ in _WINDOWS}
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _SIGNAL_COLUMNS, "canslim_signals.csv")
        for row in reader:
            day = _row_date(row, "signal_date", "canslim_signals.csv")
            for name in _window_names(day, "canslim_signals.csv"):
                _add_signal_row(windows[name], row)
    return windows


def _new_signal_stats(thresholds: Mapping[str, float]) -> dict[str, object]:
    """Create one empty sequential signal-funnel accumulator."""

    return {
        "evaluated_symbol_days": 0,
        "technical_setup_eligible": 0,
        "current_growth_gate": _new_gate_stats(thresholds["current_growth"]),
        "annual_growth_gate": _new_gate_stats(thresholds["annual_growth"]),
        "rs_score_gate": _new_gate_stats(thresholds["rs_score"]),
        "entry_composite_score_gate": _new_gate_stats(
            thresholds["entry_composite_score"]
        ),
        "entry_contract_qualified": 0,
    }


def _new_gate_stats(threshold: float) -> dict[str, object]:
    """Create one gate accumulator without collapsing missing into failures."""

    return {
        "threshold": threshold,
        "evaluated_after_prior_gate": 0,
        "passed": 0,
        "below_threshold": 0,
        "unavailable": 0,
    }


def _add_signal_row(stats: dict[str, object], row: Mapping[str, str | None]) -> None:
    """Account one emitted signal row through its recorded gate fields."""

    stats["evaluated_symbol_days"] = int(stats["evaluated_symbol_days"]) + 1
    qualified = _row_boolean(row, "entry_contract_eligible", "canslim_signals.csv")
    if qualified:
        stats["entry_contract_qualified"] = int(stats["entry_contract_qualified"]) + 1
    if not _row_boolean(row, "technical_setup_eligible", "canslim_signals.csv"):
        return

    stats["technical_setup_eligible"] = int(stats["technical_setup_eligible"]) + 1
    if not _gate_passed_or_recorded(
        stats["current_growth_gate"],
        _row_optional_number(row, "current_growth", "canslim_signals.csv"),
    ):
        return
    if not _gate_passed_or_recorded(
        stats["annual_growth_gate"],
        _row_optional_number(row, "annual_growth", "canslim_signals.csv"),
    ):
        return
    if not _gate_passed_or_recorded(
        stats["rs_score_gate"],
        _row_optional_number(row, "rs_score", "canslim_signals.csv"),
    ):
        return
    _gate_passed_or_recorded(
        stats["entry_composite_score_gate"],
        _row_optional_number(row, "entry_composite_score", "canslim_signals.csv"),
    )


def _gate_passed_or_recorded(gate: object, value: float | None) -> bool:
    """Record an emitted scalar as pass, below threshold, or unavailable."""

    if not isinstance(gate, dict):
        raise ValueError("Task 11 signal gate accumulator is malformed")
    gate["evaluated_after_prior_gate"] = int(gate["evaluated_after_prior_gate"]) + 1
    if value is None:
        gate["unavailable"] = int(gate["unavailable"]) + 1
        return False
    threshold = gate.get("threshold")
    if not isinstance(threshold, (float, int)) or isinstance(threshold, bool):
        raise ValueError("Task 11 signal gate threshold is malformed")
    if value >= float(threshold):
        gate["passed"] = int(gate["passed"]) + 1
        return True
    gate["below_threshold"] = int(gate["below_threshold"]) + 1
    return False


def _diagnose_daily_funnel(source: _SealedSource) -> dict[str, dict[str, object]]:
    """Read daily engine counts, including per-year signal-day concentration."""

    windows = {name: _new_daily_stats() for name, _, _ in _WINDOWS}
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _DAILY_FUNNEL_COLUMNS, "daily_entry_funnel.csv")
        for row in reader:
            day = _row_date(row, "signal_date", "daily_entry_funnel.csv")
            counts = {
                "evaluated": _row_nonnegative_integer(
                    row, "evaluated_count", "daily_entry_funnel.csv"
                ),
                "qualified": _row_nonnegative_integer(
                    row, "qualified_count", "daily_entry_funnel.csv"
                ),
                "attempted": _row_nonnegative_integer(
                    row, "attempted_count", "daily_entry_funnel.csv"
                ),
                "executed": _row_nonnegative_integer(
                    row, "executed_count", "daily_entry_funnel.csv"
                ),
                "rejected": _row_nonnegative_integer(
                    row, "rejected_count", "daily_entry_funnel.csv"
                ),
            }
            for name in _window_names(day, "daily_entry_funnel.csv"):
                _add_daily_counts(windows[name], day, counts)
    return windows


def _new_daily_stats() -> dict[str, object]:
    """Create one daily engine-funnel accumulator."""

    return {
        "session_count": 0,
        "evaluated": 0,
        "qualified": 0,
        "attempted": 0,
        "executed": 0,
        "rejected": 0,
        "active_signal_days": 0,
        "max_qualified_per_active_day": 0,
        "per_year": {},
    }


def _add_daily_counts(
    stats: dict[str, object], day: date, counts: Mapping[str, int]
) -> None:
    """Add one emitted daily funnel row to a window accumulator."""

    stats["session_count"] = int(stats["session_count"]) + 1
    for key, value in counts.items():
        stats[key] = int(stats[key]) + value
    if counts["qualified"] > 0:
        stats["active_signal_days"] = int(stats["active_signal_days"]) + 1
        stats["max_qualified_per_active_day"] = max(
            int(stats["max_qualified_per_active_day"]), counts["qualified"]
        )
    per_year = stats["per_year"]
    if not isinstance(per_year, dict):
        raise ValueError("Task 11 daily per-year accumulator is malformed")
    year_counts = per_year.setdefault(
        str(day.year),
        {"qualified": 0, "attempted": 0, "executed": 0, "rejected": 0},
    )
    for key in ("qualified", "attempted", "executed", "rejected"):
        year_counts[key] += counts[key]


def _diagnose_entry_outcomes(source: _SealedSource) -> dict[str, dict[str, object]]:
    """Read the actual next-open outcome ledger without inferring fill outcomes."""

    windows = {name: _new_outcome_stats() for name, _, _ in _WINDOWS}
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _OUTCOME_COLUMNS, "entry_attempt_outcomes.csv")
        for row in reader:
            day = _row_date(row, "signal_date", "entry_attempt_outcomes.csv")
            outcome = _row_text(row, "outcome", "entry_attempt_outcomes.csv")
            if outcome not in _ALLOWED_ENTRY_OUTCOMES:
                raise ValueError(f"entry_attempt_outcomes.csv has unknown outcome {outcome!r}")
            for name in _window_names(day, "entry_attempt_outcomes.csv"):
                _add_outcome(windows[name], day, outcome)
    return windows


def _new_outcome_stats() -> dict[str, object]:
    """Create one outcome-ledger accumulator."""

    return {
        "attempted": 0,
        "executed": 0,
        "rejected": 0,
        "rejection_counts": {name: 0 for name in _ENTRY_OUTCOME_REJECTIONS},
        "per_year": {},
    }


def _add_outcome(stats: dict[str, object], day: date, outcome: str) -> None:
    """Add one actual outcome to aggregate counters."""

    stats["attempted"] = int(stats["attempted"]) + 1
    per_year = stats["per_year"]
    if not isinstance(per_year, dict):
        raise ValueError("Task 11 outcome per-year accumulator is malformed")
    year_counts = per_year.setdefault(
        str(day.year),
        {
            "attempted": 0,
            "executed": 0,
            "rejected": 0,
            "next_open_buy_zone_rejected": 0,
        },
    )
    year_counts["attempted"] += 1
    if outcome == _ENTRY_OUTCOME_EXECUTED:
        stats["executed"] = int(stats["executed"]) + 1
        year_counts["executed"] += 1
        return
    stats["rejected"] = int(stats["rejected"]) + 1
    year_counts["rejected"] += 1
    rejection_counts = stats["rejection_counts"]
    if not isinstance(rejection_counts, dict):
        raise ValueError("Task 11 outcome rejection accumulator is malformed")
    rejection_counts[outcome] = int(rejection_counts[outcome]) + 1
    if outcome == "entry_rejected_next_open_buy_zone":
        year_counts["next_open_buy_zone_rejected"] += 1


def _diagnose_weekly_holdings(source: _SealedSource) -> dict[str, dict[str, object]]:
    """Read aggregate open-holdings and cash percentages from weekly snapshots."""

    windows = {name: _new_weekly_stats() for name, _, _ in _WINDOWS}
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _WEEKLY_HOLDINGS_COLUMNS, "weekly_holdings.csv")
        for row in reader:
            day = _row_date(row, "Week_Ending", "weekly_holdings.csv")
            holding_count = _row_nonnegative_integer(
                row, "Holding_Count", "weekly_holdings.csv"
            )
            cash = _row_required_number(row, "Cash", "weekly_holdings.csv")
            total_equity = _row_required_number(row, "Total_Equity", "weekly_holdings.csv")
            if total_equity <= 0:
                raise ValueError("weekly_holdings.csv contains non-positive total equity")
            for name in _window_names(day, "weekly_holdings.csv"):
                _add_weekly_snapshot(windows[name], holding_count, cash, total_equity)
    return windows


def _new_weekly_stats() -> dict[str, object]:
    """Create one weekly aggregate accumulator."""

    return {
        "week_count": 0,
        "open_holding_count_sum": 0,
        "max_open_holdings": 0,
        "weeks_with_open_holdings": 0,
        "cash_pct_sum": 0.0,
    }


def _add_weekly_snapshot(
    stats: dict[str, object], holding_count: int, cash: float, total_equity: float
) -> None:
    """Add one weekly snapshot without reconstructing position values."""

    stats["week_count"] = int(stats["week_count"]) + 1
    stats["open_holding_count_sum"] = int(stats["open_holding_count_sum"]) + holding_count
    stats["max_open_holdings"] = max(int(stats["max_open_holdings"]), holding_count)
    if holding_count > 0:
        stats["weeks_with_open_holdings"] = int(stats["weeks_with_open_holdings"]) + 1
    stats["cash_pct_sum"] = float(stats["cash_pct_sum"]) + (100.0 * cash / total_equity)


def _diagnose_transactions(source: _SealedSource) -> dict[str, dict[str, object]]:
    """Read actual exit transaction reasons from the engine transaction ledger."""

    windows = {name: _new_transaction_stats() for name, _, _ in _WINDOWS}
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _TRANSACTION_COLUMNS, "transactions.csv")
        for row in reader:
            day = _row_date(row, "Date", "transactions.csv")
            action = _row_text(row, "Action", "transactions.csv")
            if action not in {"BUY", "SELL"}:
                raise ValueError(f"transactions.csv has unsupported action {action!r}")
            reason = _row_text(row, "Reason", "transactions.csv")
            for name in _window_names(day, "transactions.csv"):
                _add_transaction(windows[name], action, reason)
    return windows


def _new_transaction_stats() -> dict[str, object]:
    """Create one transaction-ledger accumulator."""

    return {"buy_transaction_count": 0, "exit_transaction_count": 0, "exit_reasons": Counter()}


def _add_transaction(stats: dict[str, object], action: str, reason: str) -> None:
    """Record one engine transaction without combining scale-outs into trades."""

    if action == "BUY":
        stats["buy_transaction_count"] = int(stats["buy_transaction_count"]) + 1
        return
    if not reason:
        raise ValueError("transactions.csv has a SELL row without an exit reason")
    stats["exit_transaction_count"] = int(stats["exit_transaction_count"]) + 1
    exit_reasons = stats["exit_reasons"]
    if not isinstance(exit_reasons, Counter):
        raise ValueError("Task 11 exit-reason accumulator is malformed")
    exit_reasons[reason] += 1


def _reconcile_window(
    name: str,
    signals: Mapping[str, object],
    daily: Mapping[str, object],
    outcomes: Mapping[str, object],
) -> None:
    """Require independent Task 11 signal, daily, and outcome ledgers to agree."""

    for signal_key, daily_key in (
        ("evaluated_symbol_days", "evaluated"),
        ("entry_contract_qualified", "qualified"),
    ):
        if signals[signal_key] != daily[daily_key]:
            raise ValueError(f"{name} signal and daily funnel counts do not reconcile")
    if daily["qualified"] != daily["attempted"]:
        raise ValueError(f"{name} qualified signals did not all reach an entry attempt")
    for daily_key, outcome_key in (
        ("attempted", "attempted"),
        ("executed", "executed"),
        ("rejected", "rejected"),
    ):
        if daily[daily_key] != outcomes[outcome_key]:
            raise ValueError(f"{name} daily funnel and outcome ledger counts do not reconcile")
    if int(outcomes["executed"]) + int(outcomes["rejected"]) != int(outcomes["attempted"]):
        raise ValueError(f"{name} outcome ledger is not complete")
    _reconcile_per_year(name, daily, outcomes)


def _reconcile_per_year(
    name: str, daily: Mapping[str, object], outcomes: Mapping[str, object]
) -> None:
    """Check per-year engine totals before publishing them."""

    daily_years = daily["per_year"]
    outcome_years = outcomes["per_year"]
    if not isinstance(daily_years, Mapping) or not isinstance(outcome_years, Mapping):
        raise ValueError(f"{name} per-year ledgers are malformed")
    if set(daily_years) != set(outcome_years):
        raise ValueError(f"{name} daily and outcome years do not reconcile")
    for year, daily_counts in daily_years.items():
        outcome_counts = outcome_years[year]
        if not isinstance(daily_counts, Mapping) or not isinstance(outcome_counts, Mapping):
            raise ValueError(f"{name} per-year counts are malformed")
        for key in ("attempted", "executed", "rejected"):
            if daily_counts[key] != outcome_counts[key]:
                raise ValueError(f"{name} {year} daily and outcome totals do not reconcile")


def _reconcile_full_summary(
    summary: Mapping[str, Any],
    daily: Mapping[str, object],
    outcomes: Mapping[str, object],
    weekly: Mapping[str, object],
) -> dict[str, int]:
    """Cross-check aggregate ledger counts against the sealed engine summary."""

    entry_contract = _mapping_value(summary, "entry_contract", "summary.json")
    for summary_key, ledger_key in (
        ("evaluated_symbol_days", "evaluated"),
        ("qualified_signals", "qualified"),
        ("attempted_signals", "attempted"),
        ("executed_attempts", "executed"),
        ("rejected_attempts", "rejected"),
    ):
        if _required_nonnegative_integer(entry_contract, summary_key, "summary.entry_contract") != int(
            daily[ledger_key]
        ):
            raise ValueError("summary.entry_contract does not reconcile to the daily funnel")
    if _required_nonnegative_integer(
        entry_contract, "next_open_buy_zone_rejections", "summary.entry_contract"
    ) != int(outcomes["rejection_counts"]["entry_rejected_next_open_buy_zone"]):
        raise ValueError("summary next-open buy-zone rejections do not reconcile to outcomes")
    summary_rejections = _mapping_value(
        entry_contract, "rejection_counts", "summary.entry_contract"
    )
    rejection_counts = outcomes["rejection_counts"]
    if not isinstance(rejection_counts, Mapping):
        raise ValueError("Task 11 outcome rejection counts are malformed")
    resolved = {
        name: _required_nonnegative_integer(
            summary_rejections, name, "summary.entry_contract.rejection_counts"
        )
        for name in _ENTRY_OUTCOME_REJECTIONS
    }
    if resolved != dict(rejection_counts):
        raise ValueError("summary rejection counts do not reconcile to the outcome ledger")

    canslim = _mapping_value(summary, "canslim", "summary.json")
    week_count = int(weekly["week_count"])
    if week_count <= 0:
        raise ValueError("Task 11 weekly holdings ledger is empty")
    weekly_cash_pct = float(weekly["cash_pct_sum"]) / week_count
    summary_cash_pct = _required_finite_number(canslim, "average_cash_pct", "summary.canslim")
    if not math.isclose(weekly_cash_pct, summary_cash_pct, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("summary average cash does not reconcile to weekly holdings")
    return dict(sorted(resolved.items()))


def _render_window(
    name: str,
    start: date,
    end: date,
    signals: Mapping[str, object],
    daily: Mapping[str, object],
    outcomes: Mapping[str, object],
    weekly: Mapping[str, object],
    transactions: Mapping[str, object],
) -> dict[str, object]:
    """Render one verified aggregate analysis window."""

    active_days = int(daily["active_signal_days"])
    qualified = int(daily["qualified"])
    week_count = int(weekly["week_count"])
    if active_days <= 0 or week_count <= 0:
        raise ValueError(f"{name} lacks required signal-day or weekly holdings evidence")
    exits = transactions["exit_reasons"]
    if not isinstance(exits, Counter):
        raise ValueError(f"{name} exit reason counts are malformed")
    outcome_rejections = outcomes["rejection_counts"]
    if not isinstance(outcome_rejections, Mapping):
        raise ValueError(f"{name} outcome rejection counts are malformed")
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "reconciled": True,
        "funnel": {
            "evaluated_symbol_days": signals["evaluated_symbol_days"],
            "technical_setup_eligible": signals["technical_setup_eligible"],
            "current_growth_gate": signals["current_growth_gate"],
            "annual_growth_gate": signals["annual_growth_gate"],
            "rs_score_gate": signals["rs_score_gate"],
            "entry_composite_score_gate": signals["entry_composite_score_gate"],
            "qualified": qualified,
            "attempted": outcomes["attempted"],
            "executed": outcomes["executed"],
            "next_open_buy_zone_rejections": outcome_rejections[
                "entry_rejected_next_open_buy_zone"
            ],
        },
        "per_year": _render_per_year(daily, outcomes),
        "signal_day_concentration": {
            "active_days": active_days,
            "mean_qualified_per_active_day": qualified / active_days,
            "maximum_qualified_per_active_day": daily["max_qualified_per_active_day"],
        },
        "weekly_open_holdings_and_cash": {
            "week_count": week_count,
            "average_open_holdings": int(weekly["open_holding_count_sum"]) / week_count,
            "maximum_open_holdings": weekly["max_open_holdings"],
            "weeks_with_open_holdings": weekly["weeks_with_open_holdings"],
            "average_cash_pct": float(weekly["cash_pct_sum"]) / week_count,
        },
        "transactions": {
            "buy_transaction_count": transactions["buy_transaction_count"],
            "exit_transaction_count": transactions["exit_transaction_count"],
            "exit_reason_counts": dict(sorted(exits.items())),
        },
    }


def _render_per_year(
    daily: Mapping[str, object], outcomes: Mapping[str, object]
) -> dict[str, dict[str, int]]:
    """Publish only aggregate yearly counts from reconciled engine ledgers."""

    daily_years = daily["per_year"]
    outcome_years = outcomes["per_year"]
    if not isinstance(daily_years, Mapping) or not isinstance(outcome_years, Mapping):
        raise ValueError("Task 11 per-year count render inputs are malformed")
    result: dict[str, dict[str, int]] = {}
    for year in sorted(daily_years):
        daily_counts = daily_years[year]
        outcome_counts = outcome_years[year]
        if not isinstance(daily_counts, Mapping) or not isinstance(outcome_counts, Mapping):
            raise ValueError("Task 11 per-year count render value is malformed")
        result[year] = {
            "qualified": int(daily_counts["qualified"]),
            "attempted": int(daily_counts["attempted"]),
            "executed": int(daily_counts["executed"]),
            "next_open_buy_zone_rejected": int(
                outcome_counts["next_open_buy_zone_rejected"]
            ),
        }
    return result


def _window_names(day: date, label: str) -> tuple[str, ...]:
    """Return every fixed reporting window containing *day*."""

    if not (_FULL_WINDOW[1] <= day <= _FULL_WINDOW[2]):
        raise ValueError(f"{label} has a date outside the Task 11 evaluation contract")
    return tuple(name for name, start, end in _WINDOWS if start <= day <= end)


def _require_columns(
    fieldnames: list[str] | None, required: frozenset[str], label: str
) -> None:
    """Fail closed on an unexpected missing column in a bound ledger."""

    if fieldnames is None or not required.issubset(fieldnames):
        raise ValueError(f"{label} does not contain its required diagnostic columns")


def _row_date(row: Mapping[str, str | None], key: str, label: str) -> date:
    """Parse one emitted ISO date field."""

    value = _row_text(row, key, label)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} {key} must be an ISO date") from exc


def _row_text(row: Mapping[str, str | None], key: str, label: str) -> str:
    """Return a required non-empty CSV field."""

    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} {key} is missing")
    return value.strip()


def _row_boolean(row: Mapping[str, str | None], key: str, label: str) -> bool:
    """Parse the exact boolean spellings emitted by the replay."""

    value = _row_text(row, key, label)
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"{label} {key} must be True or False")


def _row_optional_number(
    row: Mapping[str, str | None], key: str, label: str
) -> float | None:
    """Parse a nullable emitted scalar; blank/non-finite means unavailable."""

    value = row.get(key)
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} {key} must be numeric or unavailable") from exc
    return parsed if math.isfinite(parsed) else None


def _row_required_number(row: Mapping[str, str | None], key: str, label: str) -> float:
    """Parse one required finite number from a ledger row."""

    value = _row_optional_number(row, key, label)
    if value is None:
        raise ValueError(f"{label} {key} must be a finite number")
    return value


def _row_nonnegative_integer(
    row: Mapping[str, str | None], key: str, label: str
) -> int:
    """Parse one non-negative integer emitted by the engine."""

    value = _row_text(row, key, label)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} {key} must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != value:
        raise ValueError(f"{label} {key} must be a non-negative integer")
    return parsed


def _required_finite_number(mapping: Mapping[str, Any], key: str, label: str) -> float:
    """Return one required finite numeric manifest/summary field."""

    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label} {key} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} {key} must be a finite number")
    return parsed


def _optional_finite_number(
    mapping: Mapping[str, Any], key: str, label: str
) -> float | None:
    """Return a nullable finite numeric manifest field."""

    value = mapping.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label} {key} must be null or a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} {key} must be null or a finite number")
    return parsed


def _required_nonnegative_integer(
    mapping: Mapping[str, Any], key: str, label: str
) -> int:
    """Return one required non-negative integer from a JSON mapping."""

    value = mapping.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} {key} must be a non-negative integer")
    return value


def _optional_nonnegative_integer(
    mapping: Mapping[str, Any], key: str, label: str
) -> int | None:
    """Return a nullable non-negative integer from a JSON mapping."""

    value = mapping.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} {key} must be null or a non-negative integer")
    return value


def _required_boolean(mapping: Mapping[str, Any], key: str, label: str) -> bool:
    """Return one required JSON boolean."""

    value = mapping.get(key)
    if type(value) is not bool:
        raise ValueError(f"{label} {key} must be boolean")
    return value
