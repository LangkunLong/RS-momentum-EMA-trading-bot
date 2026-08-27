"""Aggregate-only C/A provenance diagnosis for the sealed Task 11 replay.

The diagnostic is deliberately narrower than a backtest.  It authenticates the
published signal ledger and its local PIT source chain, reconstructs only the
visible fundamental states needed by the fixed technical cohort, and publishes
counts rather than security or filing identities.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, BinaryIO, Iterator, Mapping

from core.canslim.a_annual_earnings import evaluate_a_with_trace
from core.canslim.c_current_earnings import evaluate_c_with_trace
from core.canslim.earnings_trace import ATrace, CTrace, MetricFamily, TraceReason
from core.canslim.entry_contract import MIN_ANNUAL_GROWTH, MIN_CURRENT_GROWTH
from core.pit_data import PITDataBundle

from .baseline import (
    STRICT_PROPER_BASE_TASK11_PROFILE_ID,
    BaselineAuthority,
    BaselineAuthorityProfile,
    BaselineSnapshot,
    resolve_baseline_authority_profile,
    verify_baseline_run,
)


TASK11_CA_PROVENANCE_SCHEMA_VERSION = 1
TASK11_PROVENANCE_SHA256 = (
    "52fb676f64f279f6f7b6f119df1fd367e569658b98e313b450b0dee1f5aeb0b6"
)
TASK11_FUNDAMENTALS_SHA256 = (
    "527769d437b1f29b8aac46543fd65c8017d798f9ee6a27a350c448cfe1242b00"
)
TASK11_FUNDAMENTALS_AUDIT_SHA256 = (
    "2983e2538de39392e25df99fea93a26073f6bf5d21d87e5411847569b38619b1"
)
TASK11_BUNDLE_SHA256 = (
    "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb"
)

_WINDOW_START = date(2023, 1, 1)
_WINDOW_END = date(2025, 12, 31)
_EXPECTED_COHORT_SIZE = 8_439
_EXPECTED_COUNTS = {
    "c": {"pass": 1_926, "finite_below_threshold": 5_712, "unavailable": 801},
    "a": {"pass": 627, "finite_below_threshold": 1_136, "unavailable": 163},
}
_REQUIRED_SIGNAL_COLUMNS = frozenset(
    {
        "symbol",
        "signal_date",
        "technical_setup_eligible",
        "c_score",
        "current_growth",
        "a_score",
        "annual_growth",
    }
)
_OUTCOMES = ("pass", "finite_below_threshold", "unavailable")
_VISIBILITY = ("both_visible", "current_only", "prior_only", "neither_visible")
_METRIC_FAMILIES = frozenset(MetricFamily)
_TRACE_REASONS = frozenset(TraceReason)
_SPOOL_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024
_FLOAT_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class _SealedSource:
    name: str
    path: Path
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class _SignalRow:
    symbol: str
    signal_date: date
    c_score: float
    current_growth: float | None
    a_score: float
    annual_growth: float | None


def diagnose_task11_ca_provenance(
    run_dir: Path, profile: BaselineAuthorityProfile
) -> dict[str, object]:
    """Reconcile sealed Task 11 C/A scalars to their PIT-visible provenance."""

    if type(profile) is not BaselineAuthorityProfile:
        raise ValueError("Task 11 C/A diagnosis requires a baseline authority profile")
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
            "Task 11 C/A diagnosis requires the exact canonical "
            "strict-proper-base-task11 authority profile"
        )

    authority = canonical_profile.authority
    snapshot = verify_baseline_run(Path(run_dir), authority)
    _require_snapshot_authority_match(snapshot, authority)

    manifest_source = _SealedSource(
        "run_manifest.json",
        snapshot.run_dir / "run_manifest.json",
        authority.manifest_sha256,
    )
    signal_source = _SealedSource(
        "canslim_signals.csv",
        snapshot.run_dir / "canslim_signals.csv",
        authority.artifact_sha256["canslim_signals.csv"],
    )
    manifest = _load_json(manifest_source)
    bundle_source, provenance_source = _manifest_sources(manifest, authority)
    provenance = _load_json(provenance_source)
    fundamentals_source, audit_source = _provenance_sources(
        provenance_source, provenance
    )

    # Authenticate the sidecar bytes even though their private rows are never
    # parsed or returned.  They bind the record-level SEC chain used by the PIT
    # bundle without opening either upstream bulk archive.
    _verify_snapshot(fundamentals_source)
    _verify_snapshot(audit_source)
    _regular_nonreparse_file(signal_source.path, "sealed Task 11 signal ledger")

    verified_sources = {
        source.name: source
        for source in (
            manifest_source,
            signal_source,
            bundle_source,
            provenance_source,
            fundamentals_source,
            audit_source,
        )
    }

    bundle_bytes = _authenticated_bundle_bytes(bundle_source)
    bundle = PITDataBundle.from_authenticated_bytes(
        bundle_bytes, expected_sha256=authority.bundle_sha256
    )
    del bundle_bytes
    with bundle:
        _require_bundle_source_chain(bundle)
        rows_by_symbol = _read_fixed_cohort(signal_source)
        date_bounds = {
            symbol: (
                min(row.signal_date for row in rows),
                max(row.signal_date for row in rows),
            )
            for symbol, rows in rows_by_symbol.items()
        }
        c_aggregates = _new_gate_aggregates()
        a_aggregates = _new_gate_aggregates()
        reconciliation = _reconcile_boundaries(
            bundle,
            date_bounds,
            rows_by_symbol,
            c_aggregates,
            a_aggregates,
        )

    _require_expected_counts(c_aggregates, a_aggregates, reconciliation)
    payload: dict[str, object] = {
        "schema_version": TASK11_CA_PROVENANCE_SCHEMA_VERSION,
        "diagnosis_scope": "task11_ca_provenance_not_strategy_optimization",
        "profile": _render_profile(canonical_profile),
        "source_chain": _render_source_chain(verified_sources),
        "window": {"start": _WINDOW_START.isoformat(), "end": _WINDOW_END.isoformat()},
        "reconciliation": _render_reconciliation(reconciliation),
        "c": _render_gate_aggregates(c_aggregates),
        "a": _render_gate_aggregates(a_aggregates),
    }
    _require_aggregate_only_payload(payload)
    return payload


def _require_snapshot_authority_match(
    snapshot: BaselineSnapshot, authority: BaselineAuthority
) -> None:
    if snapshot.manifest_sha256 != authority.manifest_sha256:
        raise ValueError("verified Task 11 manifest differs from canonical authority")
    if snapshot.bundle_sha256 != authority.bundle_sha256:
        raise ValueError("verified Task 11 bundle differs from canonical authority")
    if dict(snapshot.artifact_sha256) != dict(authority.artifact_sha256):
        raise ValueError("verified Task 11 artifacts differ from canonical authority")


def _manifest_sources(
    manifest: Mapping[str, Any], authority: BaselineAuthority
) -> tuple[_SealedSource, _SealedSource]:
    arguments = _required_mapping(manifest, "arguments", "verified run manifest")
    input_sha256 = _required_mapping(
        manifest, "input_sha256", "verified run manifest"
    )
    bundle_metadata = _required_mapping(
        manifest, "bundle_metadata", "verified run manifest"
    )
    if input_sha256.get("pit_bundle") != authority.bundle_sha256:
        raise ValueError("verified Task 11 input identity does not bind the canonical bundle")
    if input_sha256.get("fundamentals_provenance") != TASK11_PROVENANCE_SHA256:
        raise ValueError("verified Task 11 input identity does not bind provenance")
    if bundle_metadata.get("fundamentals_provenance_sha256") != TASK11_PROVENANCE_SHA256:
        raise ValueError("verified Task 11 bundle metadata does not bind provenance")
    if bundle_metadata.get("fundamentals_source_sha256") != TASK11_FUNDAMENTALS_SHA256:
        raise ValueError("verified Task 11 bundle metadata does not bind fundamentals")

    bundle_path = _location(arguments, "pit_bundle", "verified run manifest")
    provenance_path = _location(
        arguments, "fundamentals_provenance", "verified run manifest"
    )
    bundle_source = _SealedSource("pit_bundle", bundle_path, authority.bundle_sha256)
    provenance_source = _SealedSource(
        "fundamentals_provenance.json",
        provenance_path,
        TASK11_PROVENANCE_SHA256,
    )
    _regular_nonreparse_file(bundle_source.path, "sealed Task 11 PIT bundle")
    _regular_nonreparse_file(
        provenance_source.path, "sealed Task 11 fundamentals provenance"
    )
    return bundle_source, provenance_source


def _provenance_sources(
    provenance_source: _SealedSource, provenance: Mapping[str, Any]
) -> tuple[_SealedSource, _SealedSource]:
    fundamentals_sha256 = _required_digest(
        provenance, "fundamentals_sha256", "verified fundamentals provenance"
    )
    audit_sha256 = _required_digest(
        provenance, "fundamentals_audit_sha256", "verified fundamentals provenance"
    )
    if fundamentals_sha256 != TASK11_FUNDAMENTALS_SHA256:
        raise ValueError("Task 11 fundamentals identity differs from the sealed identity")
    if audit_sha256 != TASK11_FUNDAMENTALS_AUDIT_SHA256:
        raise ValueError("Task 11 fundamentals audit identity differs from the sealed identity")
    parent = provenance_source.path.parent
    fundamentals_source = _SealedSource(
        "fundamentals.csv", parent / "fundamentals.csv", fundamentals_sha256
    )
    audit_source = _SealedSource(
        "fundamentals_audit.csv", parent / "fundamentals_audit.csv", audit_sha256
    )
    _regular_nonreparse_file(fundamentals_source.path, "sealed Task 11 fundamentals CSV")
    _regular_nonreparse_file(audit_source.path, "sealed Task 11 fundamentals audit CSV")
    return fundamentals_source, audit_source


def _require_bundle_source_chain(bundle: PITDataBundle) -> None:
    if bundle.sha256 != TASK11_BUNDLE_SHA256:
        raise ValueError("opened PIT bundle differs from the sealed Task 11 bundle")
    if bundle.metadata.get("fundamentals_provenance_sha256") != TASK11_PROVENANCE_SHA256:
        raise ValueError("PIT bundle metadata differs from sealed provenance")
    if bundle.metadata.get("fundamentals_source_sha256") != TASK11_FUNDAMENTALS_SHA256:
        raise ValueError("PIT bundle metadata differs from sealed fundamentals")


def _read_fixed_cohort(source: _SealedSource) -> dict[str, list[_SignalRow]]:
    rows_by_symbol: dict[str, list[_SignalRow]] = {}
    seen: set[tuple[str, date]] = set()
    cohort_size = 0
    with _verified_csv_reader(source) as reader:
        _require_columns(reader.fieldnames, _REQUIRED_SIGNAL_COLUMNS, source.name)
        for raw in reader:
            signal_date = _strict_date(raw.get("signal_date"), source.name)
            technical_eligible = _strict_bool(
                raw.get("technical_setup_eligible"), source.name
            )
            if not (_WINDOW_START <= signal_date <= _WINDOW_END and technical_eligible):
                continue
            symbol = _strict_symbol(raw.get("symbol"), source.name)
            key = (symbol, signal_date)
            if key in seen:
                raise ValueError("Task 11 technical cohort contains a duplicate symbol-date")
            seen.add(key)
            row = _SignalRow(
                symbol=symbol,
                signal_date=signal_date,
                c_score=_required_finite_csv(raw.get("c_score"), "c_score"),
                current_growth=_optional_finite_csv(
                    raw.get("current_growth"), "current_growth"
                ),
                a_score=_required_finite_csv(raw.get("a_score"), "a_score"),
                annual_growth=_optional_finite_csv(
                    raw.get("annual_growth"), "annual_growth"
                ),
            )
            rows_by_symbol.setdefault(symbol, []).append(row)
            cohort_size += 1
    if cohort_size != _EXPECTED_COHORT_SIZE:
        raise ValueError(
            f"Task 11 technical cohort has {cohort_size} rows; expected {_EXPECTED_COHORT_SIZE}"
        )
    for rows in rows_by_symbol.values():
        rows.sort(key=lambda row: row.signal_date)
    return dict(sorted(rows_by_symbol.items()))


def _reconcile_boundaries(
    bundle: PITDataBundle,
    date_bounds: Mapping[str, tuple[date, date]],
    rows_by_symbol: Mapping[str, list[_SignalRow]],
    c_aggregates: dict[str, object],
    a_aggregates: dict[str, object],
) -> dict[str, int | bool]:
    reconciled = 0
    a_cohort_size = 0
    seen_symbols: set[str] = set()
    current_symbol: str | None = None
    state_dates: list[date] = []
    state_snapshots: list[dict[str, Any]] = []

    def reconcile_symbol() -> None:
        nonlocal reconciled, a_cohort_size
        if current_symbol is None:
            return
        rows = rows_by_symbol.get(current_symbol)
        if rows is None or current_symbol in seen_symbols:
            raise ValueError("fundamental state stream contains an unexpected symbol")
        seen_symbols.add(current_symbol)
        if not state_dates or state_dates[0] != date_bounds[current_symbol][0]:
            raise ValueError("fundamental state stream omits the inclusive start boundary")
        if any(
            left >= right
            for left, right in zip(state_dates, state_dates[1:], strict=False)
        ):
            raise ValueError("fundamental state boundaries are not strictly ordered")
        for row in rows:
            state_index = bisect_right(state_dates, row.signal_date) - 1
            if state_index < 0:
                raise ValueError("signal date precedes its first fundamental state boundary")
            facts = state_snapshots[state_index]
            c_trace = evaluate_c_with_trace(facts["quarterly_income"])
            _validate_trace(c_trace, row.signal_date, "C")
            _reconcile_scalar(c_trace.score, row.c_score, "C score")
            _reconcile_optional(
                c_trace.current_growth, row.current_growth, "C current growth"
            )
            c_outcome = _outcome(c_trace.current_growth, MIN_CURRENT_GROWTH)
            _add_gate_aggregate(c_aggregates, row.signal_date, c_trace, c_outcome)

            if c_outcome == "pass":
                a_cohort_size += 1
                a_trace = evaluate_a_with_trace(
                    facts["annual_income"], balance_sheet=facts["balance_sheet"]
                )
                _validate_trace(a_trace, row.signal_date, "A")
                _reconcile_scalar(a_trace.score, row.a_score, "A score")
                _reconcile_optional(
                    a_trace.annual_growth, row.annual_growth, "A annual growth"
                )
                a_outcome = _outcome(a_trace.annual_growth, MIN_ANNUAL_GROWTH)
                _add_gate_aggregate(a_aggregates, row.signal_date, a_trace, a_outcome)

            reconciled += 1
            if reconciled % 500 == 0:
                print(
                    f"task11-ca-provenance: reconciled {reconciled}/{_EXPECTED_COHORT_SIZE} technical rows",
                    file=sys.stderr,
                )

    for symbol, boundary, facts in bundle.iter_fundamental_state_boundaries(
        date_bounds, include_provenance=True
    ):
        if current_symbol is None:
            current_symbol = symbol
        elif symbol != current_symbol:
            reconcile_symbol()
            current_symbol = symbol
            state_dates = []
            state_snapshots = []
        if symbol not in date_bounds:
            raise ValueError("fundamental state stream escaped the fixed technical cohort")
        if not date_bounds[symbol][0] <= boundary <= date_bounds[symbol][1]:
            raise ValueError("fundamental state boundary is outside the cohort date bounds")
        state_dates.append(boundary)
        state_snapshots.append(facts)
    reconcile_symbol()
    if seen_symbols != set(rows_by_symbol):
        raise ValueError("fundamental state stream omitted a technical-cohort symbol")
    if reconciled != _EXPECTED_COHORT_SIZE:
        raise ValueError("not every Task 11 technical row was reconciled")
    return {
        "c_cohort_size": reconciled,
        "a_cohort_size": a_cohort_size,
        "c_scalar_rows_checked": reconciled,
        "a_scalar_rows_checked": a_cohort_size,
        "c_availability_rows_checked": reconciled,
        "a_availability_rows_checked": a_cohort_size,
        "mismatch_count": 0,
        "passed": True,
    }


def _new_gate_aggregates() -> dict[str, object]:
    return {
        "cohort_size": 0,
        "outcomes": Counter(),
        "by_year": {str(year): Counter() for year in range(2023, 2026)},
        "metric_family_by_outcome": {outcome: Counter() for outcome in _OUTCOMES},
        "unavailable_terminal_reasons": Counter(),
        "public_date_pair_visibility": Counter(),
    }


def _add_gate_aggregate(
    aggregates: dict[str, object],
    signal_date: date,
    trace: CTrace | ATrace,
    outcome: str,
) -> None:
    aggregates["cohort_size"] = int(aggregates["cohort_size"]) + 1
    outcomes = aggregates["outcomes"]
    by_year = aggregates["by_year"]
    metric_family = aggregates["metric_family_by_outcome"]
    unavailable_reasons = aggregates["unavailable_terminal_reasons"]
    visibility = aggregates["public_date_pair_visibility"]
    if not isinstance(outcomes, Counter) or not isinstance(by_year, dict):
        raise AssertionError("gate accumulator is malformed")
    if not isinstance(metric_family, dict) or not isinstance(unavailable_reasons, Counter):
        raise AssertionError("gate accumulator is malformed")
    if not isinstance(visibility, Counter):
        raise AssertionError("gate accumulator is malformed")
    outcomes[outcome] += 1
    year_counts = by_year[str(signal_date.year)]
    year_counts["cohort_size"] += 1
    year_counts[outcome] += 1
    metric_family[outcome][trace.metric_family.value] += 1
    if outcome == "unavailable":
        if trace.terminal_reason is TraceReason.COMPLETE:
            raise ValueError("unavailable trace has a complete terminal reason")
        unavailable_reasons[trace.terminal_reason.value] += 1
    elif trace.terminal_reason is not TraceReason.COMPLETE:
        raise ValueError("available trace has a non-complete terminal reason")
    visibility[_visibility_category(trace)] += 1


def _validate_trace(trace: CTrace | ATrace, signal_date: date, label: str) -> None:
    if (label == "C" and type(trace) is not CTrace) or (
        label == "A" and type(trace) is not ATrace
    ):
        raise ValueError(f"{label} evaluator returned an unexpected trace type")
    if (
        type(trace.metric_family) is not MetricFamily
        or trace.metric_family not in _METRIC_FAMILIES
    ):
        raise ValueError(f"{label} trace has an unknown metric family")
    if (
        type(trace.terminal_reason) is not TraceReason
        or trace.terminal_reason not in _TRACE_REASONS
    ):
        raise ValueError(f"{label} trace has an unknown terminal reason")
    _finite_number(trace.score, f"{label} trace score")
    growth = trace.current_growth if type(trace) is CTrace else trace.annual_growth
    if growth is not None:
        _finite_number(growth, f"{label} trace growth")
    for field in (
        "current_period_end",
        "prior_period_end",
        "current_public_date",
        "prior_public_date",
    ):
        value = getattr(trace, field)
        if value is not None and type(value) is not date:
            raise ValueError(f"{label} trace {field} is not a date")
    for prefix in ("current", "prior"):
        period = getattr(trace, f"{prefix}_period_end")
        public = getattr(trace, f"{prefix}_public_date")
        if (period is None) != (public is None):
            raise ValueError(f"{label} trace has incomplete {prefix} provenance")
        if public is not None and public > signal_date:
            raise ValueError(f"{label} trace uses a filing after the signal date")
    for field in ("current_value", "prior_value"):
        value = getattr(trace, field)
        if value is not None:
            _finite_number(value, f"{label} trace {field}")


def _reconcile_scalar(actual: object, expected: object, label: str) -> None:
    actual_number = _finite_number(actual, f"reconstructed {label}")
    expected_number = _finite_number(expected, f"sealed {label}")
    if not math.isclose(
        actual_number,
        expected_number,
        rel_tol=0.0,
        abs_tol=_FLOAT_ABS_TOLERANCE,
    ):
        raise ValueError(f"{label} differs from the sealed signal ledger")


def _reconcile_optional(actual: object, expected: object, label: str) -> None:
    if (actual is None) != (expected is None):
        raise ValueError(f"{label} availability differs from the sealed signal ledger")
    if actual is not None:
        _reconcile_scalar(actual, expected, label)


def _outcome(growth: float | None, threshold: float) -> str:
    if growth is None:
        return "unavailable"
    value = _finite_number(growth, "trace growth")
    return "pass" if value >= threshold else "finite_below_threshold"


def _visibility_category(trace: CTrace | ATrace) -> str:
    current = trace.current_public_date is not None
    prior = trace.prior_public_date is not None
    if current and prior:
        return "both_visible"
    if current:
        return "current_only"
    if prior:
        return "prior_only"
    return "neither_visible"


def _require_expected_counts(
    c_aggregates: Mapping[str, object],
    a_aggregates: Mapping[str, object],
    reconciliation: Mapping[str, object],
) -> None:
    c_outcomes = c_aggregates.get("outcomes")
    a_outcomes = a_aggregates.get("outcomes")
    if not isinstance(c_outcomes, Counter) or not isinstance(a_outcomes, Counter):
        raise AssertionError("gate outcome accumulators are malformed")
    if {name: int(c_outcomes[name]) for name in _OUTCOMES} != _EXPECTED_COUNTS["c"]:
        raise ValueError("Task 11 C outcome counts differ from the sealed cohort")
    if {name: int(a_outcomes[name]) for name in _OUTCOMES} != _EXPECTED_COUNTS["a"]:
        raise ValueError("Task 11 A outcome counts differ from the sealed conditional cohort")
    if reconciliation.get("a_cohort_size") != _EXPECTED_COUNTS["c"]["pass"]:
        raise ValueError("Task 11 conditional A cohort differs from reconciled C passes")


def _render_profile(profile: BaselineAuthorityProfile) -> dict[str, object]:
    authority = profile.authority
    return {
        "profile_id": profile.profile_id,
        "scope": profile.scope,
        "fidelity_label": profile.fidelity_label,
        "fidelity_reason": profile.fidelity_reason,
        "manifest_sha256": authority.manifest_sha256,
        "bundle_sha256": authority.bundle_sha256,
        "replay_git_head": authority.replay_git_head,
        "date_contract": dict(authority.date_contract),
    }


def _render_source_chain(
    verified_sources: Mapping[str, _SealedSource]
) -> dict[str, dict[str, object]]:
    expected_names = {
        "run_manifest.json",
        "canslim_signals.csv",
        "pit_bundle",
        "fundamentals_provenance.json",
        "fundamentals.csv",
        "fundamentals_audit.csv",
    }
    if set(verified_sources) != expected_names:
        raise AssertionError("verified Task 11 source chain is not closed")
    return {
        name: {"sha256": verified_sources[name].expected_sha256, "verified": True}
        for name in sorted(expected_names)
    }


def _render_reconciliation(
    reconciliation: Mapping[str, object]
) -> dict[str, object]:
    return {
        "c_cohort_size": int(reconciliation["c_cohort_size"]),
        "a_cohort_size": int(reconciliation["a_cohort_size"]),
        "scalar_rows_checked": {
            "c": int(reconciliation["c_scalar_rows_checked"]),
            "a": int(reconciliation["a_scalar_rows_checked"]),
        },
        "availability_rows_checked": {
            "c": int(reconciliation["c_availability_rows_checked"]),
            "a": int(reconciliation["a_availability_rows_checked"]),
        },
        "mismatch_count": int(reconciliation["mismatch_count"]),
        "passed": bool(reconciliation["passed"]),
    }


def _render_gate_aggregates(aggregates: Mapping[str, object]) -> dict[str, object]:
    outcomes = aggregates["outcomes"]
    by_year = aggregates["by_year"]
    metric_family = aggregates["metric_family_by_outcome"]
    unavailable_reasons = aggregates["unavailable_terminal_reasons"]
    visibility = aggregates["public_date_pair_visibility"]
    if not isinstance(outcomes, Counter) or not isinstance(by_year, Mapping):
        raise AssertionError("gate aggregates are malformed")
    if not isinstance(metric_family, Mapping) or not isinstance(unavailable_reasons, Counter):
        raise AssertionError("gate aggregates are malformed")
    if not isinstance(visibility, Counter):
        raise AssertionError("gate aggregates are malformed")
    return {
        "cohort_size": int(aggregates["cohort_size"]),
        "pass_count": int(outcomes["pass"]),
        "finite_below_threshold_count": int(outcomes["finite_below_threshold"]),
        "unavailable_count": int(outcomes["unavailable"]),
        "by_year": {
            year: {
                "cohort_size": int(by_year[year]["cohort_size"]),
                "pass_count": int(by_year[year]["pass"]),
                "finite_below_threshold_count": int(
                    by_year[year]["finite_below_threshold"]
                ),
                "unavailable_count": int(by_year[year]["unavailable"]),
            }
            for year in ("2023", "2024", "2025")
        },
        "metric_family_by_outcome_counts": {
            outcome: {
                family.value: int(metric_family[outcome][family.value])
                for family in MetricFamily
            }
            for outcome in _OUTCOMES
        },
        "unavailable_terminal_reason_counts": {
            reason.value: int(unavailable_reasons[reason.value])
            for reason in TraceReason
            if reason is not TraceReason.COMPLETE
        },
        "public_date_pair_visibility_counts": {
            category: int(visibility[category]) for category in _VISIBILITY
        },
    }


def _require_aggregate_only_payload(payload: Mapping[str, object]) -> None:
    forbidden = {"symbol", "ticker", "cik", "accession", "path", "url"}

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() in forbidden:
                    raise AssertionError("Task 11 C/A payload contains a private field")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, float) and not math.isfinite(value):
            raise AssertionError("Task 11 C/A payload contains a non-finite number")

    walk(payload)


def _required_mapping(
    mapping: Mapping[str, Any], key: str, label: str
) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} {key} must be a JSON object")
    return value


def _location(mapping: Mapping[str, Any], key: str, label: str) -> Path:
    value = mapping.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} {key} must be a non-empty file location")
    # ``abspath`` normalizes a lexical location without dereferencing symlinks or
    # junctions.  The subsequent component walk must see the location exactly as
    # the authenticated manifest named it.
    return Path(os.path.abspath(value))


def _required_digest(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} {key} must be a lowercase SHA-256 digest")
    return value


def _strict_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} has a non-text signal date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid signal date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} signal date is not canonical ISO format")
    return parsed


def _strict_bool(value: object, label: str) -> bool:
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"{label} has a non-canonical technical eligibility value")


def _strict_symbol(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value != value.upper()
    ):
        raise ValueError(f"{label} has an invalid private symbol identity")
    return value


def _required_finite_csv(value: object, field: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sealed signal ledger has no {field}")
    return _finite_number(value, f"sealed signal ledger {field}")


def _optional_finite_csv(value: object, field: str) -> float | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"sealed signal ledger has an invalid {field}")
    return _finite_number(value, f"sealed signal ledger {field}")


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} is not a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is not a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} is not a finite number")
    return number


def _require_columns(
    fieldnames: list[str] | None, required: frozenset[str], label: str
) -> None:
    if fieldnames is None or len(fieldnames) != len(set(fieldnames)):
        raise ValueError(f"{label} has an invalid CSV header")
    if not required.issubset(fieldnames):
        raise ValueError(f"{label} omits required C/A reconciliation fields")


@contextmanager
def _verified_byte_snapshot(source: _SealedSource) -> Iterator[BinaryIO]:
    digest = hashlib.sha256()
    try:
        with _bound_regular_input(
            source.path, f"sealed Task 11 {source.name}"
        ) as input_handle:
            with tempfile.SpooledTemporaryFile(
                max_size=_SPOOL_MEMORY_LIMIT_BYTES, mode="w+b"
            ) as snapshot_handle:
                while block := input_handle.read(1024 * 1024):
                    digest.update(block)
                    snapshot_handle.write(block)
                if digest.hexdigest() != source.expected_sha256:
                    raise ValueError(f"sealed Task 11 {source.name} changed before parsing")
                snapshot_handle.seek(0)
                yield snapshot_handle
    except OSError as exc:
        raise ValueError(f"sealed Task 11 {source.name} cannot be snapshotted") from exc


def _authenticated_bundle_bytes(source: _SealedSource) -> bytes:
    """Return the bundle's authenticated snapshot as immutable bytes."""

    with _verified_byte_snapshot(source) as snapshot_handle:
        data = snapshot_handle.read()
    if type(data) is not bytes:
        raise AssertionError("authenticated Task 11 bundle snapshot is not immutable bytes")
    return data


def _verify_snapshot(source: _SealedSource) -> None:
    with _verified_byte_snapshot(source):
        pass


def _load_json(source: _SealedSource) -> Mapping[str, Any]:
    try:
        with _verified_byte_snapshot(source) as snapshot_handle:
            value = json.loads(
                snapshot_handle.read(),
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON")
                ),
            )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sealed Task 11 {source.name} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"sealed Task 11 {source.name} must be a JSON object")
    return value


@contextmanager
def _verified_csv_reader(source: _SealedSource) -> Iterator[csv.DictReader]:
    with _verified_byte_snapshot(source) as snapshot_handle:
        text_handle = io.TextIOWrapper(snapshot_handle, encoding="utf-8", newline="")
        try:
            yield csv.DictReader(text_handle)
        except UnicodeDecodeError as exc:
            raise ValueError(f"sealed Task 11 {source.name} is not valid UTF-8 CSV") from exc
        finally:
            text_handle.detach()


def _regular_nonreparse_file(path: Path, label: str) -> Path:
    value = Path(os.path.abspath(path))
    metadata = _nonreparse_path_metadata(value, label)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular non-reparse file")
    return value


def _nonreparse_path_metadata(path: Path, label: str) -> os.stat_result:
    """Validate every lexical component without dereferencing a reparse point."""

    if not path.is_absolute():
        raise ValueError(f"{label} must have an absolute lexical location")
    parts = path.parts
    if not parts:
        raise ValueError(f"{label} must have a valid lexical location")
    current = Path(parts[0])
    final_metadata: os.stat_result | None = None
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for index, part in enumerate(parts):
        if index:
            current /= part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ValueError(f"{label} must have regular non-reparse components") from exc
        is_reparse = bool(
            reparse_flag
            and int(getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
        )
        if stat.S_ISLNK(metadata.st_mode) or is_reparse:
            raise ValueError(f"{label} must have regular non-reparse components")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} parent components must be directories")
        final_metadata = metadata
    if final_metadata is None:
        raise ValueError(f"{label} must have a valid lexical location")
    return final_metadata


@contextmanager
def _bound_regular_input(path: Path, label: str) -> Iterator[BinaryIO]:
    """Open a lexical regular file and prove the handle still names that file."""

    value = _regular_nonreparse_file(path, label)
    try:
        with value.open("rb") as handle:
            opened_metadata = os.fstat(handle.fileno())
            path_metadata = _nonreparse_path_metadata(value, label)
            if not stat.S_ISREG(opened_metadata.st_mode) or not os.path.samestat(
                opened_metadata, path_metadata
            ):
                raise ValueError(f"{label} changed while it was opened")
            yield handle
            final_opened_metadata = os.fstat(handle.fileno())
            final_path_metadata = _nonreparse_path_metadata(value, label)
            if (
                not os.path.samestat(final_opened_metadata, final_path_metadata)
                or opened_metadata.st_size != final_opened_metadata.st_size
                or opened_metadata.st_mtime_ns != final_opened_metadata.st_mtime_ns
            ):
                raise ValueError(f"{label} changed while it was read")
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened as a bound regular file") from exc
