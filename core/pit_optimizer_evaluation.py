"""Immutable legacy-fold and continuous-panel contracts for the PIT optimizer."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import InitVar, asdict, dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
from itertools import pairwise
import sys
from typing import Iterator, Mapping, Sequence


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CLOSED_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_EXPOSURE_KINDS = {
    "candidate_validation",
    "provider_context",
    "hidden_validation",
}
VALIDATION_OUTCOME_FAILURE_CODES = frozenset(
    {
        "accounting_failed",
        "candidate_exception",
        "candidate_timeout",
        "integrity_failed",
        "not_attempted",
        "replay_failed",
        "worker_failed",
    }
)
_DISCOVERY_EXPOSURE_SEAL = object()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _date(value: str, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} is not canonical")
    return parsed


def _finite(value: float | int, label: str) -> None:
    if isinstance(value, bool) or type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")


@dataclass(frozen=True, slots=True)
class FoldSpec:
    fold_id: str
    purpose: str
    start_date: str
    end_date: str
    sessions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("fold_id is invalid")
        if self.purpose not in {"discovery", "hidden"}:
            raise ValueError("fold purpose is invalid")
        if (
            type(self.sessions) is not tuple
            or not 20 <= len(self.sessions) <= 252
        ):
            raise ValueError("fold must contain 20 through 252 sessions")
        parsed = tuple(_date(value, "fold session") for value in self.sessions)
        if len(set(parsed)) != len(parsed) or any(left >= right for left, right in pairwise(parsed)):
            raise ValueError("fold sessions must be unique and chronological")
        start = _date(self.start_date, "fold start_date")
        end = _date(self.end_date, "fold end_date")
        if start != parsed[0] or end != parsed[-1]:
            raise ValueError("fold boundaries must match sessions")


@dataclass(frozen=True, slots=True)
class FoldManifest:
    data_identity_sha256: str
    universe_sha256: str
    benchmark: str
    warmup_start_date: str
    discovery_folds: tuple[FoldSpec, ...]
    hidden_fold: FoldSpec

    def __post_init__(self) -> None:
        for name in ("data_identity_sha256", "universe_sha256"):
            if _SHA256_RE.fullmatch(getattr(self, name) or "") is None:
                raise ValueError(f"{name} is invalid")
        if not isinstance(self.benchmark, str) or not self.benchmark or self.benchmark != self.benchmark.upper():
            raise ValueError("benchmark is invalid")
        warmup = _date(self.warmup_start_date, "warmup_start_date")
        if type(self.discovery_folds) is not tuple or len(self.discovery_folds) != 2:
            raise ValueError("fold manifest requires exactly two discovery folds")
        if any(not isinstance(fold, FoldSpec) or fold.purpose != "discovery" for fold in self.discovery_folds):
            raise ValueError("discovery fold purpose is invalid")
        if not isinstance(self.hidden_fold, FoldSpec) or self.hidden_fold.purpose != "hidden":
            raise ValueError("hidden fold purpose is invalid")
        folds = (*self.discovery_folds, self.hidden_fold)
        if len({fold.fold_id for fold in folds}) != len(folds):
            raise ValueError("fold IDs must be unique")
        if len({len(fold.sessions) for fold in folds}) != 1:
            raise ValueError("folds must contain the same number of sessions")
        seen: set[str] = set()
        for fold in folds:
            overlap = seen.intersection(fold.sessions)
            if overlap:
                raise ValueError("fold sessions overlap")
            seen.update(fold.sessions)
        if warmup >= _date(self.discovery_folds[0].start_date, "first discovery start"):
            raise ValueError("warmup must precede discovery")
        if any(
            _date(left.end_date, "fold end") >= _date(right.start_date, "fold start") for left, right in pairwise(folds)
        ):
            raise ValueError("folds must be chronological with hidden strictly after discovery")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(asdict(self))).hexdigest()


@dataclass(frozen=True, slots=True)
class AggregateMetric:
    metric_id: str
    value: float | int

    def __post_init__(self) -> None:
        if not isinstance(self.metric_id, str) or not self.metric_id:
            raise ValueError("aggregate metric ID is invalid")
        _finite(self.value, "aggregate metric value")


@dataclass(frozen=True, slots=True)
class FoldAggregateSummary:
    fold_id: str
    total_return_pct: float
    excess_total_return_pp: float | None
    max_drawdown_pct: float
    sharpe_ratio: float
    closed_trades: int
    turnover_pct: float
    average_exposure_pct: float
    entry_funnel: tuple[AggregateMetric, ...]
    exit_attribution: tuple[AggregateMetric, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("aggregate fold_id is invalid")
        for name in (
            "total_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "turnover_pct",
            "average_exposure_pct",
        ):
            _finite(getattr(self, name), name)
        if self.excess_total_return_pp is not None:
            _finite(self.excess_total_return_pp, "excess_total_return_pp")
        if type(self.closed_trades) is not int or self.closed_trades < 0:
            raise ValueError("closed_trades is invalid")
        for name in ("entry_funnel", "exit_attribution"):
            metrics = getattr(self, name)
            if type(metrics) is not tuple or any(not isinstance(metric, AggregateMetric) for metric in metrics):
                raise ValueError(f"{name} is invalid")
            ids = tuple(metric.metric_id for metric in metrics)
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} metric IDs must be unique")


_OBJECTIVE_QUANTUM = Decimal("0.01")


def _objective_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    return number.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN)


_PANEL_PURPOSES = ("quick", "discovery", "qualification")
_SOURCE_AFFILIATIONS = ("sp500", "nasdaq100", "russell2000")
_AFFILIATION_BITSET_ORDER = (
    ("sp500",),
    ("nasdaq100",),
    ("sp500", "nasdaq100"),
    ("russell2000",),
    ("sp500", "russell2000"),
    ("nasdaq100", "russell2000"),
    ("sp500", "nasdaq100", "russell2000"),
)


@dataclass(frozen=True, slots=True)
class AnnualizedReturnTarget:
    metric_id: str
    formula_id: str
    basis: str
    target_pct: Decimal
    milestones_pct: tuple[Decimal, ...]
    precision_pct: Decimal

    def __post_init__(self) -> None:
        if self.metric_id != "portfolio_annualized_return_pct":
            raise ValueError("annualized return target metric is invalid")
        if self.formula_id != "production_equity_cagr_365_calendar_days_v1":
            raise ValueError("annualized return target formula is invalid")
        if self.basis != "absolute":
            raise ValueError("annualized return target basis is invalid")
        if (
            not isinstance(self.target_pct, Decimal)
            or not self.target_pct.is_finite()
            or self.target_pct <= 0
            or self.target_pct
            != self.target_pct.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN)
        ):
            raise ValueError("annualized return target is invalid")
        if (
            type(self.milestones_pct) is not tuple
            or self.milestones_pct != tuple(sorted(set(self.milestones_pct)))
            or not {
                Decimal("10.00"),
                Decimal("20.00"),
                Decimal("50.00"),
            }.issubset(self.milestones_pct)
            or self.target_pct not in self.milestones_pct
            or any(
                not isinstance(item, Decimal)
                or not item.is_finite()
                or item <= 0
                or item
                != item.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN)
                for item in self.milestones_pct
            )
        ):
            raise ValueError("annualized return milestones are invalid")
        if self.precision_pct != _OBJECTIVE_QUANTUM:
            raise ValueError("annualized return comparison precision is invalid")

    @classmethod
    def production(cls) -> "AnnualizedReturnTarget":
        return cls(
            metric_id="portfolio_annualized_return_pct",
            formula_id="production_equity_cagr_365_calendar_days_v1",
            basis="absolute",
            target_pct=Decimal("10.00"),
            milestones_pct=(
                Decimal("10.00"),
                Decimal("20.00"),
                Decimal("50.00"),
            ),
            precision_pct=_OBJECTIVE_QUANTUM,
        )


@dataclass(frozen=True, slots=True)
class PanelSecurityLineage:
    security_lineage_id: str
    executable_tickers: tuple[str, ...]
    source_affiliations: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.security_lineage_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", self.security_lineage_id)
            is None
        ):
            raise ValueError("panel security lineage ID is invalid")
        if (
            type(self.executable_tickers) is not tuple
            or not self.executable_tickers
            or len(set(self.executable_tickers)) != len(self.executable_tickers)
            or any(
                not isinstance(ticker, str)
                or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", ticker) is None
                for ticker in self.executable_tickers
            )
        ):
            raise ValueError("panel executable ticker history is invalid")
        if (
            type(self.source_affiliations) is not tuple
            or self.source_affiliations not in _AFFILIATION_BITSET_ORDER
        ):
            raise ValueError("panel source affiliations are invalid")


@dataclass(frozen=True, slots=True)
class PanelStratumAllocation:
    source_affiliations: tuple[str, ...]
    eligible_count: int
    quick_count: int
    discovery_count: int
    qualification_count: int
    unallocated_count: int

    def __post_init__(self) -> None:
        if self.source_affiliations not in _AFFILIATION_BITSET_ORDER:
            raise ValueError("panel allocation affiliation stratum is invalid")
        counts = (
            self.eligible_count,
            self.quick_count,
            self.discovery_count,
            self.qualification_count,
            self.unallocated_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("panel allocation counts are invalid")
        if sum(counts[1:]) != self.eligible_count:
            raise ValueError("panel allocation exceeds eligible stratum capacity")


@dataclass(frozen=True, slots=True)
class PanelAllocationRuleV1:
    quick_count: int
    discovery_count: int
    qualification_count: int
    algorithm_id: str = "sha256_lineage_stratified_v1"
    remainder: str = "unallocated"
    affiliation_bitset_order: tuple[tuple[str, ...], ...] = _AFFILIATION_BITSET_ORDER
    panel_allocation_order: tuple[str, ...] = (
        "qualification",
        "discovery",
        "quick",
    )
    apportionment_id: str = "largest_remainder_residual_capacity_v1"

    def __post_init__(self) -> None:
        counts = (self.quick_count, self.discovery_count, self.qualification_count)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("panel requested counts are invalid")
        if sum(counts) <= 0:
            raise ValueError("panel allocation must request at least one lineage")
        if self.algorithm_id != "sha256_lineage_stratified_v1":
            raise ValueError("panel allocation algorithm is invalid")
        if self.remainder != "unallocated":
            raise ValueError("panel allocation remainder policy is invalid")
        if self.affiliation_bitset_order != _AFFILIATION_BITSET_ORDER:
            raise ValueError("panel affiliation-bitset order is invalid")
        if self.panel_allocation_order != ("qualification", "discovery", "quick"):
            raise ValueError("panel allocation order is invalid")
        if self.apportionment_id != "largest_remainder_residual_capacity_v1":
            raise ValueError("panel apportionment algorithm is invalid")


def _validate_panel_lineages(
    lineages: tuple[PanelSecurityLineage, ...],
) -> None:
    if type(lineages) is not tuple or any(
        not isinstance(item, PanelSecurityLineage) for item in lineages
    ):
        raise ValueError("panel lineages are invalid")
    lineage_ids = tuple(item.security_lineage_id for item in lineages)
    if len(set(lineage_ids)) != len(lineage_ids):
        raise ValueError("panel security lineage IDs must be unique")
    ticker_owner: dict[str, str] = {}
    for lineage in lineages:
        for ticker in lineage.executable_tickers:
            owner = ticker_owner.setdefault(ticker, lineage.security_lineage_id)
            if owner != lineage.security_lineage_id:
                raise ValueError("ticker alias crosses panel security lineages")


def _panel_quota_counts(
    residual: Mapping[tuple[str, ...], int],
    demand: int,
) -> dict[tuple[str, ...], int]:
    total_residual = sum(residual.values())
    if demand > total_residual:
        raise ValueError("panel demand exceeds eligible capacity")
    if demand == 0:
        return {key: 0 for key in residual}
    quotas = {
        key: Fraction(demand * capacity, total_residual)
        for key, capacity in residual.items()
    }
    assigned = {
        key: min(quota.numerator // quota.denominator, residual[key])
        for key, quota in quotas.items()
    }
    remaining = demand - sum(assigned.values())
    tie_order = {key: index for index, key in enumerate(_AFFILIATION_BITSET_ORDER)}
    candidates = sorted(
        residual,
        key=lambda key: (
            -(quotas[key] - (quotas[key].numerator // quotas[key].denominator)),
            tie_order[key],
        ),
    )
    for key in candidates:
        if remaining == 0:
            break
        if assigned[key] < residual[key]:
            assigned[key] += 1
            remaining -= 1
    if remaining != 0 or any(assigned[key] > residual[key] for key in residual):
        raise ValueError("panel apportionment exceeds residual capacity")
    return assigned


def sha256_lineage_stratified_v1(
    *,
    lineages: tuple[PanelSecurityLineage, ...],
    partition_seed_sha256: str,
    rule: PanelAllocationRuleV1,
) -> tuple[
    tuple[PanelSecurityLineage, ...],
    tuple[PanelSecurityLineage, ...],
    tuple[PanelSecurityLineage, ...],
    tuple[PanelStratumAllocation, ...],
]:
    """Allocate closed lineage strata using residual-capacity largest remainder."""

    _require_digest(partition_seed_sha256, "panel partition seed SHA-256")
    if not isinstance(rule, PanelAllocationRuleV1):
        raise ValueError("panel allocation rule is invalid")
    _validate_panel_lineages(lineages)
    if rule.quick_count + rule.discovery_count + rule.qualification_count > len(
        lineages
    ):
        raise ValueError("panel demand exceeds eligible capacity")
    strata: dict[tuple[str, ...], list[PanelSecurityLineage]] = {
        key: [] for key in _AFFILIATION_BITSET_ORDER
    }
    for lineage in lineages:
        strata[lineage.source_affiliations].append(lineage)
    for values in strata.values():
        values.sort(
            key=lambda item: (
                hashlib.sha256(
                    (partition_seed_sha256 + item.security_lineage_id).encode("utf-8")
                ).hexdigest(),
                item.security_lineage_id,
            )
        )
    residual = {key: len(strata[key]) for key in _AFFILIATION_BITSET_ORDER}
    per_panel: dict[str, dict[tuple[str, ...], int]] = {}
    for purpose in rule.panel_allocation_order:
        counts = _panel_quota_counts(residual, getattr(rule, f"{purpose}_count"))
        per_panel[purpose] = counts
        for key, count in counts.items():
            residual[key] -= count
            if residual[key] < 0:
                raise ValueError("panel allocation exceeds eligible stratum capacity")
    cursors = {key: 0 for key in _AFFILIATION_BITSET_ORDER}
    selected: dict[str, list[PanelSecurityLineage]] = {
        purpose: [] for purpose in rule.panel_allocation_order
    }
    for purpose in rule.panel_allocation_order:
        for key in _AFFILIATION_BITSET_ORDER:
            start = cursors[key]
            end = start + per_panel[purpose][key]
            selected[purpose].extend(strata[key][start:end])
            cursors[key] = end
    allocations = tuple(
        PanelStratumAllocation(
            source_affiliations=key,
            eligible_count=len(strata[key]),
            quick_count=per_panel["quick"][key],
            discovery_count=per_panel["discovery"][key],
            qualification_count=per_panel["qualification"][key],
            unallocated_count=residual[key],
        )
        for key in _AFFILIATION_BITSET_ORDER
    )
    result = tuple(
        tuple(sorted(selected[purpose], key=lambda item: item.security_lineage_id))
        for purpose in _PANEL_PURPOSES
    )
    assigned = (*result[0], *result[1], *result[2])
    if len({item.security_lineage_id for item in assigned}) != len(assigned):
        raise ValueError("security lineage alias crosses panels")
    return result[0], result[1], result[2], allocations


def _panel_json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _panel_json_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, tuple):
        return [_panel_json_value(item) for item in value]
    if isinstance(value, list):
        return [_panel_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _panel_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


@dataclass(frozen=True, slots=True)
class EvaluationPanelSpec:
    purpose: str
    start_date: str
    end_date: str
    sessions: tuple[str, ...]
    sessions_sha256: str
    lineages: tuple[PanelSecurityLineage, ...]

    def __post_init__(self) -> None:
        if self.purpose not in _PANEL_PURPOSES:
            raise ValueError("evaluation panel purpose is invalid")
        if type(self.sessions) is not tuple or len(self.sessions) < 2:
            raise ValueError("evaluation panel requires continuous sessions")
        parsed = tuple(_date(value, "evaluation panel session") for value in self.sessions)
        if len(set(parsed)) != len(parsed) or any(
            left >= right for left, right in pairwise(parsed)
        ):
            raise ValueError("evaluation panel sessions must be unique and chronological")
        if self.start_date != self.sessions[0] or self.end_date != self.sessions[-1]:
            raise ValueError("evaluation panel bounds differ from its sessions")
        expected_sessions_sha256 = hashlib.sha256(
            _canonical_json_bytes(list(self.sessions))
        ).hexdigest()
        if self.sessions_sha256 != expected_sessions_sha256:
            raise ValueError("evaluation panel session digest differs")
        _validate_panel_lineages(self.lineages)
        if not self.lineages or self.lineages != tuple(
            sorted(self.lineages, key=lambda item: item.security_lineage_id)
        ):
            raise ValueError("evaluation panel lineages must be sorted and nonempty")

    @classmethod
    def from_lineages(
        cls,
        *,
        purpose: str,
        sessions: tuple[str, ...],
        lineages: tuple[PanelSecurityLineage, ...],
    ) -> "EvaluationPanelSpec":
        if not sessions:
            raise ValueError("evaluation panel sessions are absent")
        canonical_lineages = tuple(
            sorted(lineages, key=lambda item: item.security_lineage_id)
        )
        return cls(
            purpose=purpose,
            start_date=sessions[0],
            end_date=sessions[-1],
            sessions=sessions,
            sessions_sha256=hashlib.sha256(
                _canonical_json_bytes(list(sessions))
            ).hexdigest(),
            lineages=canonical_lineages,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(_panel_json_value(self))
        ).hexdigest()


def _validate_panel_plan_common(
    *,
    schema_version: int,
    pit_bundle_sha256: str,
    prices_provenance_sha256: str,
    partition_seed_sha256: str,
    qualification_retirement_domain_id: str,
    qualification_ledger_snapshot_sha256: str,
    target: AnnualizedReturnTarget,
    allocation_rule: PanelAllocationRuleV1,
    stratum_allocations: tuple[PanelStratumAllocation, ...],
) -> None:
    if schema_version != 4:
        raise ValueError("panel plan schema is unsupported")
    for value, label in (
        (pit_bundle_sha256, "panel PIT bundle SHA-256"),
        (prices_provenance_sha256, "panel prices provenance SHA-256"),
        (partition_seed_sha256, "panel partition seed SHA-256"),
        (
            qualification_retirement_domain_id,
            "panel qualification retirement domain ID",
        ),
        (
            qualification_ledger_snapshot_sha256,
            "panel qualification ledger snapshot SHA-256",
        ),
    ):
        _require_digest(value, label)
    if not isinstance(target, AnnualizedReturnTarget):
        raise ValueError("panel annualized return target is invalid")
    if not isinstance(allocation_rule, PanelAllocationRuleV1):
        raise ValueError("panel allocation rule is invalid")
    if (
        type(stratum_allocations) is not tuple
        or tuple(item.source_affiliations for item in stratum_allocations)
        != _AFFILIATION_BITSET_ORDER
        or any(
            not isinstance(item, PanelStratumAllocation)
            for item in stratum_allocations
        )
    ):
        raise ValueError("panel stratum allocation proof is invalid")
    for purpose in _PANEL_PURPOSES:
        if sum(getattr(item, f"{purpose}_count") for item in stratum_allocations) != getattr(
            allocation_rule,
            f"{purpose}_count",
        ):
            raise ValueError("panel stratum allocation totals differ from the rule")


@dataclass(frozen=True, slots=True)
class QualificationPanelPlan:
    schema_version: int
    plan_kind: str
    pit_bundle_sha256: str
    prices_provenance_sha256: str
    partition_seed_sha256: str
    qualification_retirement_domain_id: str
    qualification_ledger_snapshot_sha256: str
    target: AnnualizedReturnTarget
    allocation_rule: PanelAllocationRuleV1
    stratum_allocations: tuple[PanelStratumAllocation, ...]
    qualification_panel: EvaluationPanelSpec

    def __post_init__(self) -> None:
        if self.plan_kind != "qualification_panel_plan":
            raise ValueError("qualification panel plan kind is invalid")
        _validate_panel_plan_common(
            schema_version=self.schema_version,
            pit_bundle_sha256=self.pit_bundle_sha256,
            prices_provenance_sha256=self.prices_provenance_sha256,
            partition_seed_sha256=self.partition_seed_sha256,
            qualification_retirement_domain_id=self.qualification_retirement_domain_id,
            qualification_ledger_snapshot_sha256=self.qualification_ledger_snapshot_sha256,
            target=self.target,
            allocation_rule=self.allocation_rule,
            stratum_allocations=self.stratum_allocations,
        )
        if (
            not isinstance(self.qualification_panel, EvaluationPanelSpec)
            or self.qualification_panel.purpose != "qualification"
            or len(self.qualification_panel.lineages)
            != self.allocation_rule.qualification_count
        ):
            raise ValueError("qualification panel plan contents are invalid")

    def to_primitive(self) -> dict[str, object]:
        primitive = _panel_json_value(self)
        if not isinstance(primitive, dict):
            raise AssertionError("qualification panel primitive is not an object")
        return primitive

    @property
    def sha256(self) -> str:
        return hashlib.sha256(panel_plan_bytes(self)).hexdigest()


@dataclass(frozen=True, slots=True)
class DiscoveryPanelPlan:
    schema_version: int
    plan_kind: str
    pit_bundle_sha256: str
    prices_provenance_sha256: str
    partition_seed_sha256: str
    qualification_retirement_domain_id: str
    qualification_ledger_snapshot_sha256: str
    target: AnnualizedReturnTarget
    allocation_rule: PanelAllocationRuleV1
    stratum_allocations: tuple[PanelStratumAllocation, ...]
    quick_panel: EvaluationPanelSpec
    discovery_panel: EvaluationPanelSpec
    qualification_plan_sha256: str

    def __post_init__(self) -> None:
        if self.plan_kind != "discovery_panel_plan":
            raise ValueError("discovery panel plan kind is invalid")
        _validate_panel_plan_common(
            schema_version=self.schema_version,
            pit_bundle_sha256=self.pit_bundle_sha256,
            prices_provenance_sha256=self.prices_provenance_sha256,
            partition_seed_sha256=self.partition_seed_sha256,
            qualification_retirement_domain_id=self.qualification_retirement_domain_id,
            qualification_ledger_snapshot_sha256=self.qualification_ledger_snapshot_sha256,
            target=self.target,
            allocation_rule=self.allocation_rule,
            stratum_allocations=self.stratum_allocations,
        )
        _require_digest(
            self.qualification_plan_sha256,
            "discovery qualification plan commitment",
        )
        if (
            not isinstance(self.quick_panel, EvaluationPanelSpec)
            or self.quick_panel.purpose != "quick"
            or len(self.quick_panel.lineages) != self.allocation_rule.quick_count
            or not isinstance(self.discovery_panel, EvaluationPanelSpec)
            or self.discovery_panel.purpose != "discovery"
            or len(self.discovery_panel.lineages)
            != self.allocation_rule.discovery_count
        ):
            raise ValueError("discovery panel plan contents are invalid")
        combined = (*self.quick_panel.lineages, *self.discovery_panel.lineages)
        _validate_panel_lineages(combined)

    def to_primitive(self) -> dict[str, object]:
        primitive = _panel_json_value(self)
        if not isinstance(primitive, dict):
            raise AssertionError("discovery panel primitive is not an object")
        return primitive

    @property
    def sha256(self) -> str:
        return hashlib.sha256(panel_plan_bytes(self)).hexdigest()


def panel_plan_bytes(plan: DiscoveryPanelPlan | QualificationPanelPlan) -> bytes:
    if not isinstance(plan, (DiscoveryPanelPlan, QualificationPanelPlan)):
        raise ValueError("panel plan type is invalid")
    return _canonical_json_bytes(plan.to_primitive())


def compose_panel_plans(
    *,
    lineages: tuple[PanelSecurityLineage, ...],
    sessions: tuple[str, ...],
    pit_bundle_sha256: str,
    prices_provenance_sha256: str,
    partition_seed_sha256: str,
    target: AnnualizedReturnTarget,
    rule: PanelAllocationRuleV1,
    ledger_snapshot: "QualificationRetirementSnapshot",
) -> tuple[QualificationPanelPlan, DiscoveryPanelPlan]:
    if not isinstance(ledger_snapshot, QualificationRetirementSnapshot):
        raise ValueError("qualification retirement snapshot is invalid")
    retired = frozenset(ledger_snapshot.retired_security_lineage_ids)
    eligible = tuple(
        item for item in lineages if item.security_lineage_id not in retired
    )
    quick, discovery, qualification, allocations = sha256_lineage_stratified_v1(
        lineages=eligible,
        partition_seed_sha256=partition_seed_sha256,
        rule=rule,
    )
    common: dict[str, object] = {
        "schema_version": 4,
        "pit_bundle_sha256": pit_bundle_sha256,
        "prices_provenance_sha256": prices_provenance_sha256,
        "partition_seed_sha256": partition_seed_sha256,
        "qualification_retirement_domain_id": ledger_snapshot.qualification_retirement_domain_id,
        "qualification_ledger_snapshot_sha256": ledger_snapshot.snapshot_sha256,
        "target": target,
        "allocation_rule": rule,
        "stratum_allocations": allocations,
    }
    qualification_plan = QualificationPanelPlan(
        **common,  # type: ignore[arg-type]
        plan_kind="qualification_panel_plan",
        qualification_panel=EvaluationPanelSpec.from_lineages(
            purpose="qualification",
            sessions=sessions,
            lineages=qualification,
        ),
    )
    discovery_plan = DiscoveryPanelPlan(
        **common,  # type: ignore[arg-type]
        plan_kind="discovery_panel_plan",
        quick_panel=EvaluationPanelSpec.from_lineages(
            purpose="quick",
            sessions=sessions,
            lineages=quick,
        ),
        discovery_panel=EvaluationPanelSpec.from_lineages(
            purpose="discovery",
            sessions=sessions,
            lineages=discovery,
        ),
        qualification_plan_sha256=qualification_plan.sha256,
    )
    return qualification_plan, discovery_plan


def _panel_plan_pair_is_consistent(
    qualification_plan: QualificationPanelPlan,
    discovery_plan: DiscoveryPanelPlan,
) -> None:
    if discovery_plan.qualification_plan_sha256 != qualification_plan.sha256:
        raise ValueError("discovery qualification commitment differs")
    common_fields = (
        "schema_version",
        "pit_bundle_sha256",
        "prices_provenance_sha256",
        "partition_seed_sha256",
        "qualification_retirement_domain_id",
        "qualification_ledger_snapshot_sha256",
        "target",
        "allocation_rule",
        "stratum_allocations",
    )
    if any(
        getattr(qualification_plan, name) != getattr(discovery_plan, name)
        for name in common_fields
    ):
        raise ValueError("panel plan common identities differ")
    all_lineages = (
        *discovery_plan.quick_panel.lineages,
        *discovery_plan.discovery_panel.lineages,
        *qualification_plan.qualification_panel.lineages,
    )
    _validate_panel_lineages(all_lineages)


def _install_or_authenticate_panel_file(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValueError("conflicting partial panel publication")
        return
    staging = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    try:
        with staging.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ValueError("conflicting partial panel publication") from None
    finally:
        staging.unlink(missing_ok=True)


def publish_panel_plans(
    *,
    output_root: Path,
    qualification_plan: QualificationPanelPlan,
    discovery_plan: DiscoveryPanelPlan,
) -> dict[str, object]:
    _panel_plan_pair_is_consistent(qualification_plan, discovery_plan)
    root = Path(output_root).resolve(strict=False)
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.parent.is_symlink() or root.is_symlink():
        raise ValueError("panel publication root is invalid")
    if root.exists() and not root.is_dir():
        raise ValueError("panel publication root is not a directory")
    if not root.exists():
        root.mkdir()
    marker = root / "publication.json"
    if marker.exists():
        raise FileExistsError("completed panel publication is immutable")
    allowed = {"qualification-plan.json", "discovery-plan.json"}
    unexpected = {path.name for path in root.iterdir()} - allowed
    if unexpected:
        raise ValueError("panel publication root contains unexpected partial files")
    qualification_payload = panel_plan_bytes(qualification_plan)
    discovery_payload = panel_plan_bytes(discovery_plan)
    publication: dict[str, object] = {
        "schema_version": 4,
        "publication_kind": "panel_plans",
        "qualification_plan_sha256": hashlib.sha256(
            qualification_payload
        ).hexdigest(),
        "discovery_plan_sha256": hashlib.sha256(discovery_payload).hexdigest(),
        "qualification_ledger_snapshot_sha256": discovery_plan.qualification_ledger_snapshot_sha256,
        "quick_count": discovery_plan.allocation_rule.quick_count,
        "discovery_count": discovery_plan.allocation_rule.discovery_count,
        "qualification_count": discovery_plan.allocation_rule.qualification_count,
    }
    staging = root.parent / f".panel-staging-{secrets.token_hex(12)}"
    staging.mkdir()
    if os.name != "nt":
        os.chmod(staging, 0o700)
    try:
        (staging / "qualification-plan.json").write_bytes(qualification_payload)
        (staging / "discovery-plan.json").write_bytes(discovery_payload)
        (staging / "publication.json").write_bytes(
            _canonical_json_bytes(publication)
        )
        _install_or_authenticate_panel_file(
            root / "qualification-plan.json",
            (staging / "qualification-plan.json").read_bytes(),
        )
        _install_or_authenticate_panel_file(
            root / "discovery-plan.json",
            (staging / "discovery-plan.json").read_bytes(),
        )
        _install_or_authenticate_panel_file(
            marker,
            (staging / "publication.json").read_bytes(),
        )
    finally:
        shutil.rmtree(staging)
    return publication


def _panel_lineage_from_primitive(value: object) -> PanelSecurityLineage:
    if not isinstance(value, dict) or set(value) != {
        "security_lineage_id",
        "executable_tickers",
        "source_affiliations",
    }:
        raise ValueError("panel lineage artifact is invalid")
    return PanelSecurityLineage(
        security_lineage_id=value["security_lineage_id"],  # type: ignore[arg-type]
        executable_tickers=tuple(value["executable_tickers"]),  # type: ignore[arg-type]
        source_affiliations=tuple(value["source_affiliations"]),  # type: ignore[arg-type]
    )


def _evaluation_panel_from_primitive(value: object) -> EvaluationPanelSpec:
    if not isinstance(value, dict) or set(value) != {
        "purpose",
        "start_date",
        "end_date",
        "sessions",
        "sessions_sha256",
        "lineages",
    }:
        raise ValueError("evaluation panel artifact is invalid")
    return EvaluationPanelSpec(
        purpose=value["purpose"],  # type: ignore[arg-type]
        start_date=value["start_date"],  # type: ignore[arg-type]
        end_date=value["end_date"],  # type: ignore[arg-type]
        sessions=tuple(value["sessions"]),  # type: ignore[arg-type]
        sessions_sha256=value["sessions_sha256"],  # type: ignore[arg-type]
        lineages=tuple(
            _panel_lineage_from_primitive(item) for item in value["lineages"]  # type: ignore[union-attr]
        ),
    )


def annualized_return_target_from_primitive(
    value: object,
) -> AnnualizedReturnTarget:
    """Decode the closed, canonical annualized-return target contract."""

    if not isinstance(value, dict) or set(value) != {
        "metric_id",
        "formula_id",
        "basis",
        "target_pct",
        "milestones_pct",
        "precision_pct",
    }:
        raise ValueError("annualized return target artifact is invalid")
    return AnnualizedReturnTarget(
        metric_id=value["metric_id"],  # type: ignore[arg-type]
        formula_id=value["formula_id"],  # type: ignore[arg-type]
        basis=value["basis"],  # type: ignore[arg-type]
        target_pct=Decimal(value["target_pct"]),  # type: ignore[arg-type]
        milestones_pct=tuple(Decimal(item) for item in value["milestones_pct"]),  # type: ignore[union-attr]
        precision_pct=Decimal(value["precision_pct"]),  # type: ignore[arg-type]
    )


def _target_from_primitive(value: object) -> AnnualizedReturnTarget:
    return annualized_return_target_from_primitive(value)


def _allocation_rule_from_primitive(value: object) -> PanelAllocationRuleV1:
    expected_keys = {
        "quick_count",
        "discovery_count",
        "qualification_count",
        "algorithm_id",
        "remainder",
        "affiliation_bitset_order",
        "panel_allocation_order",
        "apportionment_id",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("panel allocation rule artifact keys are invalid")
    primitive = dict(value)
    primitive["affiliation_bitset_order"] = tuple(
        tuple(item) for item in primitive.get("affiliation_bitset_order", ())
    )
    primitive["panel_allocation_order"] = tuple(
        primitive.get("panel_allocation_order", ())
    )
    return PanelAllocationRuleV1(**primitive)  # type: ignore[arg-type]


def _allocation_from_primitive(value: object) -> PanelStratumAllocation:
    expected_keys = {
        "source_affiliations",
        "eligible_count",
        "quick_count",
        "discovery_count",
        "qualification_count",
        "unallocated_count",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("panel stratum allocation artifact keys are invalid")
    primitive = dict(value)
    primitive["source_affiliations"] = tuple(
        primitive.get("source_affiliations", ())
    )
    return PanelStratumAllocation(**primitive)  # type: ignore[arg-type]


def _discovery_panel_plan_from_primitive(value: object) -> DiscoveryPanelPlan:
    expected_keys = {
        "schema_version",
        "plan_kind",
        "pit_bundle_sha256",
        "prices_provenance_sha256",
        "partition_seed_sha256",
        "qualification_retirement_domain_id",
        "qualification_ledger_snapshot_sha256",
        "target",
        "allocation_rule",
        "stratum_allocations",
        "quick_panel",
        "discovery_panel",
        "qualification_plan_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("discovery panel plan keys are invalid")
    if value.get("schema_version") != 4:
        raise ValueError("discovery panel plan is not schema-v4")
    primitive = dict(value)
    primitive["target"] = _target_from_primitive(primitive.get("target"))
    primitive["allocation_rule"] = _allocation_rule_from_primitive(
        primitive.get("allocation_rule")
    )
    primitive["stratum_allocations"] = tuple(
        _allocation_from_primitive(item)
        for item in primitive.get("stratum_allocations", ())
    )
    primitive["quick_panel"] = _evaluation_panel_from_primitive(
        primitive.get("quick_panel")
    )
    primitive["discovery_panel"] = _evaluation_panel_from_primitive(
        primitive.get("discovery_panel")
    )
    return DiscoveryPanelPlan(**primitive)  # type: ignore[arg-type]


def _qualification_panel_plan_from_primitive(
    value: object,
) -> QualificationPanelPlan:
    expected_keys = {
        "schema_version",
        "plan_kind",
        "pit_bundle_sha256",
        "prices_provenance_sha256",
        "partition_seed_sha256",
        "qualification_retirement_domain_id",
        "qualification_ledger_snapshot_sha256",
        "target",
        "allocation_rule",
        "stratum_allocations",
        "qualification_panel",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("qualification panel plan keys are invalid")
    if value.get("schema_version") != 4:
        raise ValueError("qualification panel plan is not schema-v4")
    primitive = dict(value)
    primitive["target"] = _target_from_primitive(primitive.get("target"))
    primitive["allocation_rule"] = _allocation_rule_from_primitive(
        primitive.get("allocation_rule")
    )
    primitive["stratum_allocations"] = tuple(
        _allocation_from_primitive(item)
        for item in primitive.get("stratum_allocations", ())
    )
    primitive["qualification_panel"] = _evaluation_panel_from_primitive(
        primitive.get("qualification_panel")
    )
    return QualificationPanelPlan(**primitive)  # type: ignore[arg-type]


_PANEL_PUBLICATION_KEYS = {
    "schema_version",
    "publication_kind",
    "qualification_plan_sha256",
    "discovery_plan_sha256",
    "qualification_ledger_snapshot_sha256",
    "quick_count",
    "discovery_count",
    "qualification_count",
}


def load_discovery_panel_plan(
    path: Path,
    *,
    require_publication: bool = True,
) -> DiscoveryPanelPlan:
    resolved = Path(path).resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("discovery panel plan must be a regular file")
    raw = resolved.read_bytes()
    try:
        primitive = json.loads(raw, object_pairs_hook=_reject_duplicate_record_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("discovery panel plan JSON is invalid") from exc
    if not isinstance(primitive, dict) or raw != _canonical_json_bytes(primitive):
        raise ValueError("discovery panel plan is not canonical JSON")
    plan = _discovery_panel_plan_from_primitive(primitive)
    if require_publication:
        marker = resolved.parent / "publication.json"
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("panel publication commit marker is absent")
        marker_raw = marker.read_bytes()
        try:
            publication = json.loads(
                marker_raw,
                object_pairs_hook=_reject_duplicate_record_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("panel publication marker JSON is invalid") from exc
        if (
            not isinstance(publication, dict)
            or marker_raw != _canonical_json_bytes(publication)
            or set(publication) != _PANEL_PUBLICATION_KEYS
            or publication.get("schema_version") != 4
            or publication.get("publication_kind") != "panel_plans"
            or publication.get("discovery_plan_sha256")
            != hashlib.sha256(raw).hexdigest()
            or publication.get("qualification_plan_sha256")
            != plan.qualification_plan_sha256
            or publication.get("qualification_ledger_snapshot_sha256")
            != plan.qualification_ledger_snapshot_sha256
            or publication.get("quick_count")
            != plan.allocation_rule.quick_count
            or publication.get("discovery_count")
            != plan.allocation_rule.discovery_count
            or publication.get("qualification_count")
            != plan.allocation_rule.qualification_count
        ):
            raise ValueError("panel publication marker identity differs")
    return plan


def load_qualification_panel_plan(
    path: Path,
    *,
    require_publication: bool = True,
) -> QualificationPanelPlan:
    resolved = Path(path).resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("qualification panel plan must be a regular file")
    raw = resolved.read_bytes()
    try:
        primitive = json.loads(raw, object_pairs_hook=_reject_duplicate_record_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification panel plan JSON is invalid") from exc
    if not isinstance(primitive, dict) or raw != _canonical_json_bytes(primitive):
        raise ValueError("qualification panel plan is not canonical JSON")
    plan = _qualification_panel_plan_from_primitive(primitive)
    if require_publication:
        marker = resolved.parent / "publication.json"
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("panel publication commit marker is absent")
        marker_raw = marker.read_bytes()
        try:
            publication = json.loads(
                marker_raw,
                object_pairs_hook=_reject_duplicate_record_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("panel publication marker JSON is invalid") from exc
        if (
            not isinstance(publication, dict)
            or marker_raw != _canonical_json_bytes(publication)
            or set(publication) != _PANEL_PUBLICATION_KEYS
            or publication.get("schema_version") != 4
            or publication.get("publication_kind") != "panel_plans"
            or publication.get("qualification_plan_sha256")
            != hashlib.sha256(raw).hexdigest()
            or publication.get("qualification_ledger_snapshot_sha256")
            != plan.qualification_ledger_snapshot_sha256
            or publication.get("quick_count")
            != plan.allocation_rule.quick_count
            or publication.get("discovery_count")
            != plan.allocation_rule.discovery_count
            or publication.get("qualification_count")
            != plan.allocation_rule.qualification_count
            or not isinstance(publication.get("discovery_plan_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                publication["discovery_plan_sha256"],
            )
            is None
        ):
            raise ValueError("panel publication marker identity differs")
    return plan


@dataclass(frozen=True, slots=True)
class PanelAggregateSummary:
    panel_id: str
    panel_sha256: str
    starting_equity: float
    ending_equity: float
    elapsed_calendar_days: int
    portfolio_annualized_return_pct: Decimal
    total_return_pct: float
    benchmark_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    closed_trades: int
    turnover_pct: float
    average_exposure_pct: float
    entry_funnel: tuple[AggregateMetric, ...]
    exit_attribution: tuple[AggregateMetric, ...]

    def __post_init__(self) -> None:
        if self.panel_id not in _PANEL_PURPOSES:
            raise ValueError("panel aggregate identity is invalid")
        _require_digest(self.panel_sha256, "panel aggregate panel SHA-256")
        for name in (
            "starting_equity",
            "ending_equity",
            "total_return_pct",
            "benchmark_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "turnover_pct",
            "average_exposure_pct",
        ):
            _finite(getattr(self, name), f"panel {name}")
        if self.starting_equity <= 0.0 or self.ending_equity <= 0.0:
            raise ValueError("panel equity endpoints must be positive")
        if type(self.elapsed_calendar_days) is not int or self.elapsed_calendar_days <= 0:
            raise ValueError("panel elapsed calendar days are invalid")
        if type(self.closed_trades) is not int or self.closed_trades < 0:
            raise ValueError("panel closed trades are invalid")
        for name in ("entry_funnel", "exit_attribution"):
            metrics = getattr(self, name)
            if type(metrics) is not tuple or any(
                not isinstance(metric, AggregateMetric) for metric in metrics
            ):
                raise ValueError(f"panel {name} is invalid")
            ids = tuple(metric.metric_id for metric in metrics)
            if len(set(ids)) != len(ids):
                raise ValueError(f"panel {name} metric IDs must be unique")
        from core.pit_optimization import production_equity_cagr_pct

        expected = _objective_decimal(
            production_equity_cagr_pct(
                self.starting_equity,
                self.ending_equity,
                self.elapsed_calendar_days,
            ),
            "panel production annualized return",
        )
        supplied = _objective_decimal(
            self.portfolio_annualized_return_pct,
            "panel annualized return",
        )
        if supplied != self.portfolio_annualized_return_pct or supplied != expected:
            raise ValueError("panel annualized return differs from production CAGR")


def _aggregate_metric_from_primitive(value: object) -> AggregateMetric:
    if not isinstance(value, dict) or set(value) != {"metric_id", "value"}:
        raise ValueError("panel aggregate metric artifact is invalid")
    return AggregateMetric(
        metric_id=value["metric_id"],  # type: ignore[arg-type]
        value=value["value"],  # type: ignore[arg-type]
    )


def panel_aggregate_summary_from_primitive(value: object) -> PanelAggregateSummary:
    """Decode closed quick/discovery aggregate evidence for schema-v4 roles."""

    expected_keys = {field.name for field in fields(PanelAggregateSummary)}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("panel aggregate summary artifact is invalid")
    try:
        return PanelAggregateSummary(
            panel_id=value["panel_id"],  # type: ignore[arg-type]
            panel_sha256=value["panel_sha256"],  # type: ignore[arg-type]
            starting_equity=value["starting_equity"],  # type: ignore[arg-type]
            ending_equity=value["ending_equity"],  # type: ignore[arg-type]
            elapsed_calendar_days=value["elapsed_calendar_days"],  # type: ignore[arg-type]
            portfolio_annualized_return_pct=Decimal(
                value["portfolio_annualized_return_pct"]  # type: ignore[arg-type]
            ),
            total_return_pct=value["total_return_pct"],  # type: ignore[arg-type]
            benchmark_return_pct=value["benchmark_return_pct"],  # type: ignore[arg-type]
            max_drawdown_pct=value["max_drawdown_pct"],  # type: ignore[arg-type]
            sharpe_ratio=value["sharpe_ratio"],  # type: ignore[arg-type]
            closed_trades=value["closed_trades"],  # type: ignore[arg-type]
            turnover_pct=value["turnover_pct"],  # type: ignore[arg-type]
            average_exposure_pct=value["average_exposure_pct"],  # type: ignore[arg-type]
            entry_funnel=tuple(
                _aggregate_metric_from_primitive(item)
                for item in value["entry_funnel"]  # type: ignore[union-attr]
            ),
            exit_attribution=tuple(
                _aggregate_metric_from_primitive(item)
                for item in value["exit_attribution"]  # type: ignore[union-attr]
            ),
        )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("panel aggregate summary closed contract is invalid") from exc


@dataclass(frozen=True, slots=True)
class DiscoveryPanelComparison:
    candidate_cagr_pct: Decimal
    fixed_baseline_cagr_pct: Decimal
    candidate_vs_fixed_baseline_delta_pp: Decimal
    target_gap_pp: Decimal
    strictly_improves_champion: bool

    def __post_init__(self) -> None:
        for name in (
            "candidate_cagr_pct",
            "fixed_baseline_cagr_pct",
            "candidate_vs_fixed_baseline_delta_pp",
            "target_gap_pp",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value != value.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN)
            ):
                raise ValueError(f"discovery panel {name} is invalid")
        if self.candidate_vs_fixed_baseline_delta_pp != (
            self.candidate_cagr_pct - self.fixed_baseline_cagr_pct
        ).quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN):
            raise ValueError("discovery panel baseline delta differs")
        if self.target_gap_pp != (
            AnnualizedReturnTarget.production().target_pct - self.candidate_cagr_pct
        ).quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN):
            raise ValueError("discovery panel target gap differs")
        if type(self.strictly_improves_champion) is not bool:
            raise ValueError("discovery panel champion comparison is invalid")

    @classmethod
    def from_result(
        cls,
        *,
        candidate_cagr_pct: object,
        fixed_baseline_cagr_pct: object,
        current_champion_cagr_pct: object,
        target: AnnualizedReturnTarget,
    ) -> "DiscoveryPanelComparison":
        if not isinstance(target, AnnualizedReturnTarget):
            raise ValueError("discovery panel target is invalid")
        candidate = _objective_decimal(candidate_cagr_pct, "candidate panel CAGR")
        baseline = _objective_decimal(fixed_baseline_cagr_pct, "baseline panel CAGR")
        champion = _objective_decimal(current_champion_cagr_pct, "champion panel CAGR")
        return cls(
            candidate_cagr_pct=candidate,
            fixed_baseline_cagr_pct=baseline,
            candidate_vs_fixed_baseline_delta_pp=(candidate - baseline).quantize(
                _OBJECTIVE_QUANTUM,
                rounding=ROUND_HALF_EVEN,
            ),
            target_gap_pp=(target.target_pct - candidate).quantize(
                _OBJECTIVE_QUANTUM,
                rounding=ROUND_HALF_EVEN,
            ),
            strictly_improves_champion=candidate > champion,
        )


@dataclass(frozen=True, slots=True)
class QualificationDecision:
    qualification_panel_sha256: str
    candidate_cagr_pct: Decimal
    baseline_cagr_pct: Decimal
    target_pct: Decimal
    evaluation_complete: bool
    integrity_complete: bool
    qualified: bool

    def __post_init__(self) -> None:
        _require_digest(
            self.qualification_panel_sha256,
            "qualification panel SHA-256",
        )
        for name in ("candidate_cagr_pct", "baseline_cagr_pct", "target_pct"):
            value = getattr(self, name)
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value != value.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN)
            ):
                raise ValueError(f"qualification {name} is invalid")
        if self.target_pct <= 0:
            raise ValueError("qualification target must be positive")
        if type(self.evaluation_complete) is not bool or type(self.integrity_complete) is not bool:
            raise ValueError("qualification completion flags are invalid")
        expected = (
            self.evaluation_complete
            and self.integrity_complete
            and self.candidate_cagr_pct >= self.target_pct
            and self.candidate_cagr_pct > self.baseline_cagr_pct
        )
        if self.qualified is not expected:
            raise ValueError("qualification decision differs from the closed return gate")

    @classmethod
    def from_result(
        cls,
        *,
        candidate_evidence: PanelAggregateSummary,
        baseline_evidence: PanelAggregateSummary,
        qualification_panel: EvaluationPanelSpec,
        target: AnnualizedReturnTarget,
        evaluation_complete: bool,
        integrity_complete: bool,
    ) -> "QualificationDecision":
        if not isinstance(target, AnnualizedReturnTarget):
            raise ValueError("qualification target is invalid")
        if (
            not isinstance(candidate_evidence, PanelAggregateSummary)
            or not isinstance(baseline_evidence, PanelAggregateSummary)
            or not isinstance(qualification_panel, EvaluationPanelSpec)
            or qualification_panel.purpose != "qualification"
            or candidate_evidence.panel_id != "qualification"
            or baseline_evidence.panel_id != "qualification"
            or candidate_evidence.panel_sha256 != qualification_panel.sha256
            or baseline_evidence.panel_sha256 != qualification_panel.sha256
        ):
            raise ValueError(
                "candidate and baseline evidence must bind the same qualification panel"
            )
        candidate = candidate_evidence.portfolio_annualized_return_pct
        baseline = baseline_evidence.portfolio_annualized_return_pct
        qualified = (
            evaluation_complete is True
            and integrity_complete is True
            and candidate >= target.target_pct
            and candidate > baseline
        )
        return cls(
            qualification_panel_sha256=candidate_evidence.panel_sha256,
            candidate_cagr_pct=candidate,
            baseline_cagr_pct=baseline,
            target_pct=target.target_pct,
            evaluation_complete=evaluation_complete,
            integrity_complete=integrity_complete,
            qualified=qualified,
        )


@dataclass(frozen=True, slots=True)
class DiscoveryScore:
    median_excess_return_pp: Decimal
    worst_excess_return_pp: Decimal
    max_drawdown_magnitude_pp: Decimal

    def __post_init__(self) -> None:
        for name in (
            "median_excess_return_pp",
            "worst_excess_return_pp",
            "max_drawdown_magnitude_pp",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value != value.quantize(_OBJECTIVE_QUANTUM, rounding=ROUND_HALF_EVEN):
                raise ValueError(f"{name} must be quantized to 0.01")
        if self.max_drawdown_magnitude_pp < 0:
            raise ValueError("max_drawdown_magnitude_pp cannot be negative")

    @property
    def ordering_key(self) -> tuple[Decimal, Decimal, Decimal]:
        return (
            self.median_excess_return_pp,
            self.worst_excess_return_pp,
            -self.max_drawdown_magnitude_pp,
        )


def discovery_score_from_folds(
    candidate_folds: tuple[FoldAggregateSummary, ...],
    original_baseline_folds: tuple[FoldAggregateSummary, ...],
    *,
    original_baseline_sha256: str,
    expected_original_baseline_sha256: str,
) -> DiscoveryScore:
    for folds, label in (
        (candidate_folds, "candidate"),
        (original_baseline_folds, "original baseline"),
    ):
        if (
            type(folds) is not tuple
            or len(folds) != 2
            or any(not isinstance(item, FoldAggregateSummary) for item in folds)
        ):
            raise ValueError(
                f"discovery objective requires exactly two {label} fold summaries"
            )
        if tuple(item.fold_id for item in folds) != (
            "discovery_1",
            "discovery_2",
        ):
            raise ValueError("discovery objective fold identities are invalid")
    _require_digest(
        original_baseline_sha256,
        "discovery original baseline SHA-256",
    )
    _require_digest(
        expected_original_baseline_sha256,
        "discovery expected original baseline SHA-256",
    )
    actual_baseline_sha256 = hashlib.sha256(
        _canonical_json_bytes([asdict(item) for item in original_baseline_folds])
    ).hexdigest()
    if (
        original_baseline_sha256 != expected_original_baseline_sha256
        or actual_baseline_sha256 != original_baseline_sha256
    ):
        raise ValueError("discovery fixed baseline identity differs")
    # Sparse subset folds are deliberately short and the sealed baseline can
    # legitimately have no trades in one fold.  Require evidence of trading
    # somewhere in the fixed discovery window instead of forcing every small
    # fold to contain a close.  The objective still scores both fold returns
    # and the worst drawdown, so an inactive fold remains visible and cannot
    # manufacture an improvement by itself.
    if sum(item.closed_trades for item in candidate_folds) < 1:
        raise ValueError("discovery window requires at least one closed trade")
    excess = tuple(
        _objective_decimal(
            Decimal(str(candidate.total_return_pct))
            - Decimal(str(original_baseline.total_return_pct)),
            "fold excess return",
        )
        for candidate, original_baseline in zip(
            candidate_folds,
            original_baseline_folds,
            strict=True,
        )
    )
    ordered = tuple(sorted(excess))
    median = ((ordered[0] + ordered[1]) / Decimal(2)).quantize(
        _OBJECTIVE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    drawdowns = tuple(
        _objective_decimal(
            abs(min(Decimal(str(item.max_drawdown_pct)), Decimal(0))),
            "fold drawdown magnitude",
        )
        for item in candidate_folds
    )
    return DiscoveryScore(
        median_excess_return_pp=median,
        worst_excess_return_pp=ordered[0],
        max_drawdown_magnitude_pp=max(drawdowns),
    )


def strictly_improves_discovery(
    candidate: DiscoveryScore,
    incumbent: DiscoveryScore,
) -> bool:
    if not isinstance(candidate, DiscoveryScore) or not isinstance(
        incumbent, DiscoveryScore
    ):
        raise ValueError("discovery comparison requires closed scores")
    return candidate.ordering_key > incumbent.ordering_key


@dataclass(frozen=True, slots=True)
class HoldoutDecision:
    excess_total_return_pp: Decimal
    closed_trades: int
    safety_complete: bool
    integrity_complete: bool
    accounting_complete: bool
    long_replay_eligible: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.excess_total_return_pp, Decimal)
            or not self.excess_total_return_pp.is_finite()
            or self.excess_total_return_pp
            != self.excess_total_return_pp.quantize(
                _OBJECTIVE_QUANTUM,
                rounding=ROUND_HALF_EVEN,
            )
        ):
            raise ValueError("holdout excess return must be a quantized finite Decimal")
        if type(self.closed_trades) is not int or self.closed_trades < 0:
            raise ValueError("holdout closed trades are invalid")
        for name in (
            "safety_complete",
            "integrity_complete",
            "accounting_complete",
            "long_replay_eligible",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"holdout {name} must be boolean")
        expected = (
            self.excess_total_return_pp >= Decimal("0.10")
            and self.closed_trades >= 3
            and self.safety_complete
            and self.integrity_complete
            and self.accounting_complete
        )
        if self.long_replay_eligible is not expected:
            raise ValueError("holdout eligibility differs from the closed gate")

    @classmethod
    def from_result(
        cls,
        *,
        excess_total_return_pp: float | int | Decimal,
        closed_trades: int,
        safety_complete: bool,
        integrity_complete: bool,
        accounting_complete: bool,
    ) -> "HoldoutDecision":
        excess = _objective_decimal(
            excess_total_return_pp,
            "holdout excess return",
        )
        eligible = (
            excess >= Decimal("0.10")
            and type(closed_trades) is int
            and closed_trades >= 3
            and safety_complete is True
            and integrity_complete is True
            and accounting_complete is True
        )
        return cls(
            excess_total_return_pp=excess,
            closed_trades=closed_trades,
            safety_complete=safety_complete,
            integrity_complete=integrity_complete,
            accounting_complete=accounting_complete,
            long_replay_eligible=eligible,
        )


@dataclass(frozen=True, slots=True)
class FoldEvaluationResult:
    fold_id: str
    engine_policy_sha256: str
    candidate_identity_sha256: str
    evidence_sha256: str
    aggregate_metrics: FoldAggregateSummary

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("fold evaluation ID is invalid")
        _require_digest(self.engine_policy_sha256, "fold engine policy SHA-256")
        _require_digest(
            self.candidate_identity_sha256,
            "fold candidate identity SHA-256",
        )
        _require_digest(self.evidence_sha256, "fold evidence SHA-256")
        if not isinstance(self.aggregate_metrics, FoldAggregateSummary):
            raise ValueError("fold evaluation aggregate is invalid")
        if self.aggregate_metrics.fold_id != self.fold_id:
            raise ValueError("fold evaluation aggregate identity differs")


@dataclass(frozen=True, slots=True)
class DiscoveryComparison:
    candidate_vs_fixed_baseline: DiscoveryScore
    candidate_vs_incumbent_diagnostics: DiscoveryScore
    rankable: bool
    strictly_improves_incumbent: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_vs_fixed_baseline, DiscoveryScore) or not isinstance(
            self.candidate_vs_incumbent_diagnostics,
            DiscoveryScore,
        ):
            raise ValueError("discovery comparison scores are invalid")
        if type(self.rankable) is not bool or type(self.strictly_improves_incumbent) is not bool:
            raise ValueError("discovery comparison flags are invalid")
        if self.strictly_improves_incumbent and not self.rankable:
            raise ValueError("an unrankable discovery candidate cannot improve")


@dataclass(frozen=True, slots=True)
class DiscoveryEvaluation:
    folds: tuple[FoldEvaluationResult, ...]
    comparison: DiscoveryComparison

    def __post_init__(self) -> None:
        if (
            type(self.folds) is not tuple
            or len(self.folds) != 2
            or any(not isinstance(item, FoldEvaluationResult) for item in self.folds)
        ):
            raise ValueError("discovery evaluation folds are invalid")
        if tuple(item.fold_id for item in self.folds) != (
            "discovery_1",
            "discovery_2",
        ):
            raise ValueError("discovery evaluation fold order is invalid")
        if not isinstance(self.comparison, DiscoveryComparison):
            raise ValueError("discovery evaluation comparison is invalid")
        if len({item.engine_policy_sha256 for item in self.folds}) != 1 or len(
            {item.candidate_identity_sha256 for item in self.folds}
        ) != 1:
            raise ValueError("discovery evaluation fold identities differ")


@dataclass(frozen=True, slots=True)
class DeterminismAttestation:
    fold_id: str
    expected_evidence_sha256: str
    repeated_evidence_sha256: str
    matched: bool

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("determinism fold ID is invalid")
        _require_digest(
            self.expected_evidence_sha256,
            "determinism expected evidence SHA-256",
        )
        _require_digest(
            self.repeated_evidence_sha256,
            "determinism repeated evidence SHA-256",
        )
        if type(self.matched) is not bool:
            raise ValueError("determinism match flag is invalid")
        if self.matched is not (
            self.expected_evidence_sha256 == self.repeated_evidence_sha256
        ):
            raise ValueError("determinism match flag differs from evidence")


@dataclass(frozen=True, slots=True)
class HiddenEvaluation:
    baseline_aggregate: FoldAggregateSummary
    candidate_aggregate: FoldAggregateSummary
    decision: HoldoutDecision

    def __post_init__(self) -> None:
        if not isinstance(self.baseline_aggregate, FoldAggregateSummary) or not isinstance(
            self.candidate_aggregate,
            FoldAggregateSummary,
        ):
            raise ValueError("hidden evaluation aggregates are invalid")
        if self.baseline_aggregate.fold_id != self.candidate_aggregate.fold_id:
            raise ValueError("hidden evaluation fold identities differ")
        if not isinstance(self.decision, HoldoutDecision):
            raise ValueError("hidden evaluation decision is invalid")


@dataclass(frozen=True, slots=True)
class HiddenResetReceipt:
    """Content-free proof that one hidden subject began from a fresh reset."""

    fold_id: str
    subject: str
    subject_identity_sha256: str
    reset_receipt_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("hidden reset fold ID is invalid")
        if self.subject not in {"baseline", "candidate"}:
            raise ValueError("hidden reset subject is invalid")
        _require_digest(
            self.subject_identity_sha256,
            "hidden reset subject identity SHA-256",
        )
        _require_digest(self.reset_receipt_sha256, "hidden reset receipt SHA-256")


def _hidden_attestation_digest(values: dict[str, object]) -> str:
    evaluation = values["evaluation"]
    assert isinstance(evaluation, HiddenEvaluation)
    decision = evaluation.decision
    primitive = {
        key: (
            asdict(value)
            if isinstance(value, HiddenResetReceipt)
            else value
        )
        for key, value in values.items()
        if key != "evaluation"
    }
    primitive["evaluation"] = {
        "baseline_aggregate": asdict(evaluation.baseline_aggregate),
        "candidate_aggregate": asdict(evaluation.candidate_aggregate),
        "decision": {
            "excess_total_return_pp": str(decision.excess_total_return_pp),
            "closed_trades": decision.closed_trades,
            "safety_complete": decision.safety_complete,
            "integrity_complete": decision.integrity_complete,
            "accounting_complete": decision.accounting_complete,
            "long_replay_eligible": decision.long_replay_eligible,
        },
    }
    return hashlib.sha256(_canonical_json_bytes(primitive)).hexdigest()


@dataclass(frozen=True, slots=True)
class HiddenEvaluationAttestation:
    """Authenticated binding for one independently reset hidden comparison."""

    reservation_record_sha256: str
    source_head: str
    source_fingerprint_sha256: str
    baseline_policy_sha256: str
    candidate_identity_sha256: str
    fold_id: str
    baseline_reset: HiddenResetReceipt
    candidate_reset: HiddenResetReceipt
    evaluation: HiddenEvaluation
    attestation_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        reservation_record_sha256: str,
        source_head: str,
        source_fingerprint_sha256: str,
        baseline_policy_sha256: str,
        candidate_identity_sha256: str,
        fold_id: str,
        baseline_reset: HiddenResetReceipt,
        candidate_reset: HiddenResetReceipt,
        evaluation: HiddenEvaluation,
    ) -> "HiddenEvaluationAttestation":
        values: dict[str, object] = {
            "reservation_record_sha256": reservation_record_sha256,
            "source_head": source_head,
            "source_fingerprint_sha256": source_fingerprint_sha256,
            "baseline_policy_sha256": baseline_policy_sha256,
            "candidate_identity_sha256": candidate_identity_sha256,
            "fold_id": fold_id,
            "baseline_reset": baseline_reset,
            "candidate_reset": candidate_reset,
            "evaluation": evaluation,
        }
        return cls(**values, attestation_sha256=_hidden_attestation_digest(values))

    def __post_init__(self) -> None:
        _require_digest(
            self.reservation_record_sha256,
            "hidden attestation reservation SHA-256",
        )
        if not isinstance(self.source_head, str) or re.fullmatch(
            r"[0-9a-f]{40}", self.source_head
        ) is None:
            raise ValueError("hidden attestation source HEAD is invalid")
        for value, label in (
            (self.source_fingerprint_sha256, "source fingerprint"),
            (self.baseline_policy_sha256, "baseline policy"),
            (self.candidate_identity_sha256, "candidate identity"),
            (self.attestation_sha256, "attestation"),
        ):
            _require_digest(value, f"hidden {label} SHA-256")
        if not isinstance(self.fold_id, str) or not self.fold_id:
            raise ValueError("hidden attestation fold ID is invalid")
        if not isinstance(self.baseline_reset, HiddenResetReceipt) or not isinstance(
            self.candidate_reset, HiddenResetReceipt
        ):
            raise ValueError("hidden attestation reset receipts are invalid")
        if not isinstance(self.evaluation, HiddenEvaluation):
            raise ValueError("hidden attestation evaluation is invalid")
        values: dict[str, object] = {
            "reservation_record_sha256": self.reservation_record_sha256,
            "source_head": self.source_head,
            "source_fingerprint_sha256": self.source_fingerprint_sha256,
            "baseline_policy_sha256": self.baseline_policy_sha256,
            "candidate_identity_sha256": self.candidate_identity_sha256,
            "fold_id": self.fold_id,
            "baseline_reset": self.baseline_reset,
            "candidate_reset": self.candidate_reset,
            "evaluation": self.evaluation,
        }
        if self.attestation_sha256 != _hidden_attestation_digest(values):
            raise ValueError("hidden attestation digest differs")


@dataclass(frozen=True, slots=True)
class PitOptimizerCleanup:
    candidate_removed: bool
    worker_stopped: bool
    source_modified: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.candidate_removed,
                self.worker_stopped,
                self.source_modified,
            )
        ):
            raise ValueError("optimizer cleanup flags are invalid")


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _require_closed_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _CLOSED_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ValidationWindowIdentity:
    pit_bundle_sha256: str
    universe_sha256: str
    benchmark: str
    warmup_contract_sha256: str
    sessions_sha256: str
    session_count: int
    first_session: str
    last_session: str

    def __post_init__(self) -> None:
        for name in (
            "pit_bundle_sha256",
            "universe_sha256",
            "warmup_contract_sha256",
            "sessions_sha256",
        ):
            _require_digest(getattr(self, name), f"validation {name}")
        if (
            not isinstance(self.benchmark, str)
            or not self.benchmark
            or self.benchmark != self.benchmark.upper()
        ):
            raise ValueError("validation benchmark is invalid")
        if type(self.session_count) is not int or self.session_count <= 0:
            raise ValueError("validation session count is invalid")
        first = _date(self.first_session, "validation first session")
        last = _date(self.last_session, "validation last session")
        if first > last:
            raise ValueError("validation session bounds are invalid")


@dataclass(frozen=True, slots=True)
class ValidationExposureMetadata:
    run_id: str
    source_head: str
    baseline_policy_sha256: str
    candidate_identity_sha256: str | None
    exposure_kind: str

    def __post_init__(self) -> None:
        _require_closed_id(self.run_id, "validation run ID")
        if not isinstance(self.source_head, str) or re.fullmatch(
            r"[0-9a-f]{40}", self.source_head
        ) is None:
            raise ValueError("validation source HEAD is invalid")
        _require_digest(
            self.baseline_policy_sha256,
            "validation baseline policy SHA-256",
        )
        if self.candidate_identity_sha256 is not None:
            _require_digest(
                self.candidate_identity_sha256,
                "validation candidate identity SHA-256",
            )
        if self.exposure_kind not in _EXPOSURE_KINDS:
            raise ValueError("validation exposure kind is invalid")


@dataclass(frozen=True, slots=True)
class ValidationReservation:
    consumption_key_sha256: str
    reservation_record_sha256: str

    def __post_init__(self) -> None:
        _require_digest(
            self.consumption_key_sha256,
            "validation consumption key SHA-256",
        )
        _require_digest(
            self.reservation_record_sha256,
            "validation reservation record SHA-256",
        )


@dataclass(frozen=True, slots=True)
class ValidationOutcomeProof:
    """Content-free durable completion proof for one consumed reservation."""

    reservation_record_sha256: str
    attempted: bool
    completed: bool
    failure_code: str | None
    outcome_record_sha256: str
    ledger_head_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.reservation_record_sha256, "reservation"),
            (self.outcome_record_sha256, "outcome record"),
            (self.ledger_head_sha256, "ledger head"),
        ):
            _require_digest(value, f"validation outcome {label} SHA-256")
        ValidationLedger._validate_outcome_fields(
            attempted=self.attempted,
            completed=self.completed,
            failure_code=self.failure_code,
        )


@dataclass(frozen=True, slots=True)
class DiscoveryExposureProof:
    fold_ids: tuple[str, str]
    window_identities: tuple[ValidationWindowIdentity, ValidationWindowIdentity]
    metadata: ValidationExposureMetadata
    reservation_record_sha256s: tuple[str, str]
    ledger_head_sha256: str
    _controller_seal: InitVar[object] = None

    def __post_init__(self, _controller_seal: object) -> None:
        if _controller_seal is not _DISCOVERY_EXPOSURE_SEAL:
            raise ValueError("discovery exposure proof must be ledger derived")
        if self.fold_ids != ("discovery_1", "discovery_2"):
            raise ValueError("discovery exposure proof fold IDs are invalid")
        if (
            type(self.window_identities) is not tuple
            or len(self.window_identities) != 2
            or any(
                not isinstance(item, ValidationWindowIdentity)
                for item in self.window_identities
            )
        ):
            raise ValueError("discovery exposure proof window identities are invalid")
        if not isinstance(self.metadata, ValidationExposureMetadata):
            raise ValueError("discovery exposure proof metadata is invalid")
        left, right = self.window_identities
        if (
            left.pit_bundle_sha256,
            left.universe_sha256,
            left.benchmark,
            left.warmup_contract_sha256,
        ) != (
            right.pit_bundle_sha256,
            right.universe_sha256,
            right.benchmark,
            right.warmup_contract_sha256,
        ):
            raise ValueError("discovery exposure proof window lineage is inconsistent")
        if type(self.reservation_record_sha256s) is not tuple:
            raise ValueError("discovery exposure proof reservations are invalid")
        for digest in (*self.reservation_record_sha256s, self.ledger_head_sha256):
            _require_digest(digest, "discovery exposure proof SHA-256")


def _reject_duplicate_record_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("validation ledger contains duplicate JSON keys")
        value[key] = item
    return value


@contextmanager
def _validation_file_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class QualificationPanelIdentity:
    """One exact retrospective panel exposure retired before evaluation."""

    schema_version: int
    qualification_retirement_domain_id: str
    pit_bundle_sha256: str
    qualification_plan_sha256: str
    qualification_panel_sha256: str
    security_lineage_ids: tuple[str, ...]
    sessions_sha256: str
    session_count: int
    first_session: str
    last_session: str
    warmup_contract_sha256: str
    engine_policy_sha256: str
    target_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("qualification panel identity schema is invalid")
        for name in (
            "qualification_retirement_domain_id",
            "pit_bundle_sha256",
            "qualification_plan_sha256",
            "qualification_panel_sha256",
            "sessions_sha256",
            "warmup_contract_sha256",
            "engine_policy_sha256",
            "target_sha256",
        ):
            _require_digest(getattr(self, name), f"qualification identity {name}")
        if (
            type(self.security_lineage_ids) is not tuple
            or not self.security_lineage_ids
            or self.security_lineage_ids
            != tuple(sorted(set(self.security_lineage_ids)))
            or any(
                re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", item) is None
                for item in self.security_lineage_ids
            )
        ):
            raise ValueError("qualification identity lineage set is invalid")
        if type(self.session_count) is not int or self.session_count <= 1:
            raise ValueError("qualification identity session count is invalid")
        first = _date(self.first_session, "qualification first session")
        last = _date(self.last_session, "qualification last session")
        if first >= last:
            raise ValueError("qualification identity session bounds are invalid")

    @classmethod
    def from_plan(
        cls,
        plan: QualificationPanelPlan,
        *,
        warmup_contract_sha256: str,
        engine_policy_sha256: str,
    ) -> "QualificationPanelIdentity":
        if not isinstance(plan, QualificationPanelPlan):
            raise ValueError("qualification identity requires a closed plan")
        panel = plan.qualification_panel
        target_sha256 = hashlib.sha256(
            _canonical_json_bytes(_panel_json_value(plan.target))
        ).hexdigest()
        return cls(
            schema_version=4,
            qualification_retirement_domain_id=(
                plan.qualification_retirement_domain_id
            ),
            pit_bundle_sha256=plan.pit_bundle_sha256,
            qualification_plan_sha256=plan.sha256,
            qualification_panel_sha256=panel.sha256,
            security_lineage_ids=tuple(
                sorted(item.security_lineage_id for item in panel.lineages)
            ),
            sessions_sha256=panel.sessions_sha256,
            session_count=len(panel.sessions),
            first_session=panel.start_date,
            last_session=panel.end_date,
            warmup_contract_sha256=warmup_contract_sha256,
            engine_policy_sha256=engine_policy_sha256,
            target_sha256=target_sha256,
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "qualification_retirement_domain_id": (
                self.qualification_retirement_domain_id
            ),
            "pit_bundle_sha256": self.pit_bundle_sha256,
            "qualification_plan_sha256": self.qualification_plan_sha256,
            "qualification_panel_sha256": self.qualification_panel_sha256,
            "security_lineage_ids": list(self.security_lineage_ids),
            "sessions_sha256": self.sessions_sha256,
            "session_count": self.session_count,
            "first_session": self.first_session,
            "last_session": self.last_session,
            "warmup_contract_sha256": self.warmup_contract_sha256,
            "engine_policy_sha256": self.engine_policy_sha256,
            "target_sha256": self.target_sha256,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.to_primitive())).hexdigest()


@dataclass(frozen=True, slots=True)
class QualificationReservation:
    qualification_identity_sha256: str
    reservation_record_sha256: str
    retired_security_lineage_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(
            self.qualification_identity_sha256,
            "qualification reservation identity SHA-256",
        )
        _require_digest(
            self.reservation_record_sha256,
            "qualification reservation record SHA-256",
        )
        if (
            not self.retired_security_lineage_ids
            or self.retired_security_lineage_ids
            != tuple(sorted(set(self.retired_security_lineage_ids)))
        ):
            raise ValueError("qualification reservation lineages are invalid")


@dataclass(frozen=True, slots=True)
class QualificationOutcomeProof:
    reservation_record_sha256: str
    attempted: bool
    completed: bool
    terminal_code: str
    qualified: bool | None
    decision_sha256: str | None
    outcome_record_sha256: str
    ledger_head_sha256: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.reservation_record_sha256, "reservation"),
            (self.outcome_record_sha256, "outcome"),
            (self.ledger_head_sha256, "ledger head"),
        ):
            _require_digest(value, f"qualification outcome {label} SHA-256")
        if (
            type(self.attempted) is not bool
            or type(self.completed) is not bool
            or self.completed and not self.attempted
            or _CLOSED_ID_RE.fullmatch(self.terminal_code or "") is None
        ):
            raise ValueError("qualification outcome terminal facts are invalid")
        if self.completed:
            if type(self.qualified) is not bool or self.decision_sha256 is None:
                raise ValueError("completed qualification outcome is incomplete")
            _require_digest(
                self.decision_sha256,
                "qualification outcome decision SHA-256",
            )
        elif self.qualified is not None or self.decision_sha256 is not None:
            raise ValueError("failed qualification outcome claims a decision")


@dataclass(frozen=True, slots=True)
class QualificationRetirementSnapshot:
    schema_version: int
    qualification_retirement_domain_id: str
    ledger_head_sha256: str
    record_count: int
    retired_security_lineage_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("qualification retirement snapshot schema is invalid")
        _require_digest(
            self.qualification_retirement_domain_id,
            "qualification retirement domain ID",
        )
        _require_digest(self.ledger_head_sha256, "qualification ledger head")
        if type(self.record_count) is not int or self.record_count < 1:
            raise ValueError("qualification retirement snapshot record count is invalid")
        if (
            type(self.retired_security_lineage_ids) is not tuple
            or self.retired_security_lineage_ids
            != tuple(sorted(set(self.retired_security_lineage_ids)))
            or any(
                re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", item) is None
                for item in self.retired_security_lineage_ids
            )
        ):
            raise ValueError("qualification retired lineage snapshot is invalid")

    @property
    def snapshot_sha256(self) -> str:
        """Return the append-only ledger head used as the snapshot commitment."""

        return self.ledger_head_sha256


class QualificationRetirementLedger:
    """Permanent schema-v4 hash chain for one-use qualification lineages."""

    def __init__(
        self,
        path: Path,
        qualification_retirement_domain_id: str,
    ) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("qualification retirement ledger path must be absolute")
        _require_digest(
            qualification_retirement_domain_id,
            "qualification retirement domain ID",
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if candidate.parent.is_symlink() or not candidate.parent.is_dir() or candidate.is_symlink():
            raise ValueError("qualification retirement ledger path is invalid")
        self._path = candidate.resolve(strict=False)
        self._domain_id = qualification_retirement_domain_id
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with _validation_file_lock(self._lock_path):
            if not self._path.exists():
                self._initialize_unlocked()
            self._read_records_unlocked()

    @staticmethod
    def _record_digest(record: Mapping[str, object]) -> str:
        preimage = dict(record)
        preimage.pop("record_sha256", None)
        return hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()

    def _initialize_unlocked(self) -> None:
        record: dict[str, object] = {
            "schema_version": 4,
            "ledger_kind": "qualification_retirement",
            "qualification_retirement_domain_id": self._domain_id,
            "record_type": "genesis",
            "sequence": 0,
            "previous_record_sha256": None,
        }
        record["record_sha256"] = self._record_digest(record)
        payload = _canonical_json_bytes(record)
        staging = self._path.parent / (
            f".{self._path.name}.{secrets.token_hex(12)}.tmp"
        )
        try:
            with staging.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staging, self._path)
            except FileExistsError:
                return
        finally:
            staging.unlink(missing_ok=True)

    def _read_records_unlocked(self) -> list[dict[str, object]]:
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError("qualification retirement ledger is not a regular file")
        raw = self._path.read_bytes()
        if not raw or not raw.endswith(b"\n"):
            raise ValueError("qualification retirement ledger is not canonical JSONL")
        records: list[dict[str, object]] = []
        previous: str | None = None
        for sequence, line in enumerate(raw.splitlines(keepends=True)):
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_record_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("qualification retirement ledger JSON is invalid") from exc
            if not isinstance(record, dict) or line != _canonical_json_bytes(record):
                raise ValueError("qualification retirement ledger record is not canonical")
            digest = record.get("record_sha256")
            if (
                record.get("schema_version") != 4
                or record.get("ledger_kind") != "qualification_retirement"
                or record.get("qualification_retirement_domain_id") != self._domain_id
                or record.get("sequence") != sequence
                or record.get("previous_record_sha256") != previous
                or not isinstance(digest, str)
                or digest != self._record_digest(record)
            ):
                raise ValueError("qualification retirement ledger hash chain is invalid")
            record_type = record.get("record_type")
            if sequence == 0:
                if set(record) != {
                    "schema_version",
                    "ledger_kind",
                    "qualification_retirement_domain_id",
                    "record_type",
                    "sequence",
                    "previous_record_sha256",
                    "record_sha256",
                } or record_type != "genesis":
                    raise ValueError("qualification retirement ledger genesis is invalid")
            elif record_type == "reservation":
                expected_keys = {
                    "schema_version",
                    "ledger_kind",
                    "qualification_retirement_domain_id",
                    "record_type",
                    "sequence",
                    "previous_record_sha256",
                    "qualification_identity_sha256",
                    "candidate_identity_sha256",
                    "identity",
                    "retired_security_lineage_ids",
                    "record_sha256",
                }
                identity_value = record.get("identity")
                if not isinstance(identity_value, dict):
                    raise ValueError("qualification reservation identity is invalid")
                identity_value = dict(identity_value)
                lineage_values = identity_value.get("security_lineage_ids")
                if not isinstance(lineage_values, list):
                    raise ValueError("qualification reservation lineages are invalid")
                identity_value["security_lineage_ids"] = tuple(lineage_values)
                identity = QualificationPanelIdentity(**identity_value)  # type: ignore[arg-type]
                retired = record.get("retired_security_lineage_ids")
                if (
                    set(record) != expected_keys
                    or record.get("qualification_identity_sha256") != identity.sha256
                    or record.get("candidate_identity_sha256") is None
                    or _SHA256_RE.fullmatch(
                        str(record.get("candidate_identity_sha256"))
                    )
                    is None
                    or retired != list(identity.security_lineage_ids)
                ):
                    raise ValueError("qualification reservation record is invalid")
            elif record_type == "outcome":
                expected_keys = {
                    "schema_version",
                    "ledger_kind",
                    "qualification_retirement_domain_id",
                    "record_type",
                    "sequence",
                    "previous_record_sha256",
                    "reservation_record_sha256",
                    "attempted",
                    "completed",
                    "terminal_code",
                    "qualified",
                    "decision_sha256",
                    "record_sha256",
                }
                reservation = record.get("reservation_record_sha256")
                if (
                    set(record) != expected_keys
                    or not isinstance(reservation, str)
                    or not any(
                        item.get("record_type") == "reservation"
                        and item.get("record_sha256") == reservation
                        for item in records
                    )
                    or any(
                        item.get("record_type") == "outcome"
                        and item.get("reservation_record_sha256") == reservation
                        for item in records
                    )
                ):
                    raise ValueError("qualification outcome record is invalid")
                QualificationOutcomeProof(
                    reservation_record_sha256=reservation,
                    attempted=record.get("attempted"),  # type: ignore[arg-type]
                    completed=record.get("completed"),  # type: ignore[arg-type]
                    terminal_code=record.get("terminal_code"),  # type: ignore[arg-type]
                    qualified=record.get("qualified"),  # type: ignore[arg-type]
                    decision_sha256=record.get("decision_sha256"),  # type: ignore[arg-type]
                    outcome_record_sha256=str(record["record_sha256"]),
                    ledger_head_sha256=str(record["record_sha256"]),
                )
            elif record_type == "retirement":
                raise ValueError("legacy qualification retirement records are unsupported")
            else:
                raise ValueError("qualification retirement ledger record type is invalid")
            records.append(record)
            previous = digest
        return records

    @staticmethod
    def _retired_lineages(records: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
        retired: set[str] = set()
        for record in records[1:]:
            values = record.get("retired_security_lineage_ids")
            if values is None and record.get("record_type") == "reservation":
                values = record.get("security_lineage_ids")
            if values is None and record.get("record_type") in {"reservation", "retirement"}:
                lineage = record.get("security_lineage_id")
                values = [] if lineage is None else [lineage]
            if values is None:
                continue
            if (
                not isinstance(values, list)
                or values != sorted(set(values))
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", item) is None
                    for item in values
                )
            ):
                raise ValueError("qualification retirement lineage record is invalid")
            retired.update(values)
        return tuple(sorted(retired))

    def _snapshot_unlocked(self) -> QualificationRetirementSnapshot:
        records = self._read_records_unlocked()
        return QualificationRetirementSnapshot(
            schema_version=4,
            qualification_retirement_domain_id=self._domain_id,
            ledger_head_sha256=str(records[-1]["record_sha256"]),
            record_count=len(records),
            retired_security_lineage_ids=self._retired_lineages(records),
        )

    def snapshot(self) -> QualificationRetirementSnapshot:
        with _validation_file_lock(self._lock_path):
            return self._snapshot_unlocked()

    def authenticate_ancestor(
        self,
        ledger_head_sha256: str,
    ) -> QualificationRetirementSnapshot:
        """Authenticate a sealed historical head as an ancestor of current state."""

        _require_digest(ledger_head_sha256, "qualification ledger ancestor head")
        with _validation_file_lock(self._lock_path):
            records = self._read_records_unlocked()
            if not any(
                record.get("record_sha256") == ledger_head_sha256
                for record in records
            ):
                raise ValueError("qualification ledger snapshot is not an ancestor")
            return self._snapshot_unlocked()

    def _append_record_unlocked(
        self,
        records: Sequence[Mapping[str, object]],
        primitive: Mapping[str, object],
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 4,
            "ledger_kind": "qualification_retirement",
            "qualification_retirement_domain_id": self._domain_id,
            "sequence": len(records),
            "previous_record_sha256": records[-1]["record_sha256"],
            **primitive,
        }
        record["record_sha256"] = self._record_digest(record)
        with self._path.open("ab") as handle:
            handle.write(_canonical_json_bytes(record))
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def reserve_qualification(
        self,
        identity: QualificationPanelIdentity,
        *,
        candidate_identity_sha256: str,
    ) -> QualificationReservation:
        """Permanently retire the exact panel lineages before any evaluation."""

        if (
            not isinstance(identity, QualificationPanelIdentity)
            or identity.qualification_retirement_domain_id != self._domain_id
        ):
            raise ValueError("qualification reservation identity differs from ledger")
        _require_digest(
            candidate_identity_sha256,
            "qualification reservation candidate identity SHA-256",
        )
        with _validation_file_lock(self._lock_path):
            records = self._read_records_unlocked()
            retired = set(self._retired_lineages(records))
            requested = set(identity.security_lineage_ids)
            if retired.intersection(requested):
                raise ValueError("qualification panel contains retired lineages")
            record = self._append_record_unlocked(
                records,
                {
                    "record_type": "reservation",
                    "qualification_identity_sha256": identity.sha256,
                    "candidate_identity_sha256": candidate_identity_sha256,
                    "identity": identity.to_primitive(),
                    "retired_security_lineage_ids": list(
                        identity.security_lineage_ids
                    ),
                },
            )
        return QualificationReservation(
            qualification_identity_sha256=identity.sha256,
            reservation_record_sha256=str(record["record_sha256"]),
            retired_security_lineage_ids=identity.security_lineage_ids,
        )

    def record_qualification_outcome(
        self,
        reservation: QualificationReservation,
        *,
        attempted: bool,
        completed: bool,
        terminal_code: str,
        decision: QualificationDecision | None,
    ) -> QualificationOutcomeProof:
        """Append one terminal result for a reservation, including failures."""

        if not isinstance(reservation, QualificationReservation):
            raise ValueError("qualification outcome reservation is invalid")
        qualified = None if decision is None else decision.qualified
        decision_sha256 = (
            None
            if decision is None
            else hashlib.sha256(
                _canonical_json_bytes(_panel_json_value(decision))
            ).hexdigest()
        )
        provisional = QualificationOutcomeProof(
            reservation_record_sha256=reservation.reservation_record_sha256,
            attempted=attempted,
            completed=completed,
            terminal_code=terminal_code,
            qualified=qualified,
            decision_sha256=decision_sha256,
            outcome_record_sha256="0" * 64,
            ledger_head_sha256="0" * 64,
        )
        with _validation_file_lock(self._lock_path):
            records = self._read_records_unlocked()
            matching = next(
                (
                    item
                    for item in records
                    if item.get("record_type") == "reservation"
                    and item.get("record_sha256")
                    == reservation.reservation_record_sha256
                    and item.get("qualification_identity_sha256")
                    == reservation.qualification_identity_sha256
                    and item.get("retired_security_lineage_ids")
                    == list(reservation.retired_security_lineage_ids)
                ),
                None,
            )
            if matching is None:
                raise ValueError("qualification reservation is absent from ledger")
            if any(
                item.get("record_type") == "outcome"
                and item.get("reservation_record_sha256")
                == reservation.reservation_record_sha256
                for item in records
            ):
                raise ValueError("qualification reservation outcome already exists")
            record = self._append_record_unlocked(
                records,
                {
                    "record_type": "outcome",
                    "reservation_record_sha256": (
                        provisional.reservation_record_sha256
                    ),
                    "attempted": provisional.attempted,
                    "completed": provisional.completed,
                    "terminal_code": provisional.terminal_code,
                    "qualified": provisional.qualified,
                    "decision_sha256": provisional.decision_sha256,
                },
            )
        digest = str(record["record_sha256"])
        return QualificationOutcomeProof(
            reservation_record_sha256=reservation.reservation_record_sha256,
            attempted=attempted,
            completed=completed,
            terminal_code=terminal_code,
            qualified=qualified,
            decision_sha256=decision_sha256,
            outcome_record_sha256=digest,
            ledger_head_sha256=digest,
        )

    @contextmanager
    def locked_snapshot(self) -> Iterator[QualificationRetirementSnapshot]:
        with _validation_file_lock(self._lock_path):
            yield self._snapshot_unlocked()


class ValidationLedger:
    """Permanent, hash-chained consumption ledger for validation windows."""

    def __init__(self, path: Path) -> None:
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or candidate.name != "pit_optimizer_validation_ledger.jsonl"
            or not candidate.parent.is_dir()
            or candidate.parent.is_symlink()
            or candidate.is_symlink()
        ):
            raise ValueError("validation ledger path is invalid")
        self._path = candidate.resolve()
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with _validation_file_lock(self._lock_path):
            self._read_records()

    @staticmethod
    def _consumption_key(identity: ValidationWindowIdentity) -> str:
        return hashlib.sha256(_canonical_json_bytes(asdict(identity))).hexdigest()

    @staticmethod
    def _record_digest(record: dict[str, object]) -> str:
        preimage = dict(record)
        preimage.pop("record_sha256", None)
        return hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()

    def _read_records(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError("validation ledger must be a regular non-link file")
        raw = self._path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("validation ledger has a partial record")
        records: list[dict[str, object]] = []
        previous = "0" * 64
        consumed: set[str] = set()
        reservations: set[str] = set()
        outcomes: set[str] = set()
        for index, line in enumerate(raw.splitlines(keepends=True), start=1):
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_record_keys,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("validation ledger contains invalid JSON") from exc
            if not isinstance(value, dict) or line != _canonical_json_bytes(value):
                raise ValueError("validation ledger record is not canonical JSON")
            if value.get("schema_version") != 2 or value.get("record_index") != index:
                raise ValueError("validation ledger record index is invalid")
            if value.get("previous_record_sha256") != previous:
                raise ValueError("validation ledger hash chain is broken")
            record_sha256 = value.get("record_sha256")
            _require_digest(record_sha256, "validation ledger record SHA-256")
            if record_sha256 != self._record_digest(value):
                raise ValueError("validation ledger record digest differs")
            record_type = value.get("record_type")
            if record_type == "consumption":
                expected_keys = {
                    "schema_version",
                    "record_type",
                    "record_index",
                    "previous_record_sha256",
                    "consumption_key_sha256",
                    "identity",
                    "metadata",
                    "record_sha256",
                }
                if set(value) != expected_keys:
                    raise ValueError("validation consumption record keys are invalid")
                key = value.get("consumption_key_sha256")
                _require_digest(key, "validation consumption key SHA-256")
                if key in consumed:
                    raise ValueError("validation ledger repeats a consumed identity")
                identity = value.get("identity")
                metadata = value.get("metadata")
                if not isinstance(identity, dict) or not isinstance(metadata, dict):
                    raise ValueError("validation consumption record is malformed")
                closed_identity = ValidationWindowIdentity(**identity)
                ValidationExposureMetadata(**metadata)
                if key != self._consumption_key(closed_identity):
                    raise ValueError("validation consumption key differs from identity")
                consumed.add(key)
                reservations.add(str(record_sha256))
            elif record_type == "outcome":
                expected_keys = {
                    "schema_version",
                    "record_type",
                    "record_index",
                    "previous_record_sha256",
                    "reservation_record_sha256",
                    "attempted",
                    "completed",
                    "failure_code",
                    "record_sha256",
                }
                if set(value) != expected_keys:
                    raise ValueError("validation outcome record keys are invalid")
                reservation = value.get("reservation_record_sha256")
                if reservation not in reservations or reservation in outcomes:
                    raise ValueError("validation outcome reservation is invalid")
                self._validate_outcome_fields(
                    attempted=value.get("attempted"),
                    completed=value.get("completed"),
                    failure_code=value.get("failure_code"),
                )
                outcomes.add(str(reservation))
            else:
                raise ValueError("validation ledger record type is invalid")
            previous = str(record_sha256)
            records.append(value)
        return records

    @staticmethod
    def _validate_outcome_fields(
        *,
        attempted: object,
        completed: object,
        failure_code: object,
    ) -> None:
        if type(attempted) is not bool or type(completed) is not bool:
            raise ValueError("validation outcome flags must be boolean")
        if completed and not attempted:
            raise ValueError("completed validation must have been attempted")
        if completed:
            if failure_code is not None:
                raise ValueError("completed validation cannot have a failure code")
        else:
            if failure_code not in VALIDATION_OUTCOME_FAILURE_CODES:
                raise ValueError("validation outcome failure code is not closed")
            if attempted is False and failure_code != "not_attempted":
                raise ValueError("unattempted validation outcome code is inconsistent")
            if attempted is True and failure_code == "not_attempted":
                raise ValueError("attempted validation outcome code is inconsistent")

    def _append_record(
        self,
        records: list[dict[str, object]],
        primitive: dict[str, object],
    ) -> dict[str, object]:
        record = {
            "schema_version": 2,
            "record_index": len(records) + 1,
            "previous_record_sha256": (
                "0" * 64 if not records else records[-1]["record_sha256"]
            ),
            **primitive,
        }
        record["record_sha256"] = self._record_digest(record)
        payload = _canonical_json_bytes(record)
        with self._path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def _reserve(
        self,
        identity: ValidationWindowIdentity,
        metadata: ValidationExposureMetadata,
    ) -> ValidationReservation:
        if not isinstance(identity, ValidationWindowIdentity) or not isinstance(
            metadata, ValidationExposureMetadata
        ):
            raise ValueError("validation reservation requires closed contracts")
        key = self._consumption_key(identity)
        with _validation_file_lock(self._lock_path):
            records = self._read_records()
            if any(
                record.get("record_type") == "consumption"
                and record.get("consumption_key_sha256") == key
                for record in records
            ):
                raise ValueError("validation window identity is permanently consumed")
            record = self._append_record(
                records,
                {
                    "record_type": "consumption",
                    "consumption_key_sha256": key,
                    "identity": asdict(identity),
                    "metadata": asdict(metadata),
                },
            )
        return ValidationReservation(key, str(record["record_sha256"]))

    def mark_discovery(
        self,
        identity: ValidationWindowIdentity,
        metadata: ValidationExposureMetadata,
    ) -> ValidationReservation:
        if not isinstance(metadata, ValidationExposureMetadata) or metadata.exposure_kind not in {
            "candidate_validation",
            "provider_context",
        }:
            raise ValueError("discovery exposure kind is invalid")
        return self._reserve(identity, metadata)

    def reserve_hidden(
        self,
        identity: ValidationWindowIdentity,
        metadata: ValidationExposureMetadata,
    ) -> ValidationReservation:
        if not isinstance(metadata, ValidationExposureMetadata) or metadata.exposure_kind != "hidden_validation":
            raise ValueError("hidden exposure kind is invalid")
        return self._reserve(identity, metadata)

    def seal_discovery_folds(
        self,
        fold_manifest: FoldManifest,
        reservations: tuple[ValidationReservation, ValidationReservation],
    ) -> DiscoveryExposureProof:
        if not isinstance(fold_manifest, FoldManifest):
            raise ValueError("discovery exposure fold manifest is invalid")
        if (
            type(reservations) is not tuple
            or len(reservations) != 2
            or any(not isinstance(item, ValidationReservation) for item in reservations)
        ):
            raise ValueError("discovery exposure reservations are invalid")
        with _validation_file_lock(self._lock_path):
            records = self._read_records()
            selected: list[dict[str, object]] = []
            selected_identities: list[ValidationWindowIdentity] = []
            selected_metadata: list[ValidationExposureMetadata] = []
            for fold, reservation in zip(
                fold_manifest.discovery_folds,
                reservations,
                strict=True,
            ):
                record = next(
                    (
                        item
                        for item in records
                        if item.get("record_type") == "consumption"
                        and item.get("record_sha256")
                        == reservation.reservation_record_sha256
                        and item.get("consumption_key_sha256")
                        == reservation.consumption_key_sha256
                    ),
                    None,
                )
                if record is None:
                    raise ValueError("discovery exposure reservation is absent")
                metadata = record.get("metadata")
                identity = record.get("identity")
                expected_sessions_sha256 = hashlib.sha256(
                    _canonical_json_bytes(list(fold.sessions))
                ).hexdigest()
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("exposure_kind")
                    not in {"candidate_validation", "provider_context"}
                    or not isinstance(identity, dict)
                    or identity.get("pit_bundle_sha256")
                    != fold_manifest.data_identity_sha256
                    or identity.get("universe_sha256")
                    != fold_manifest.universe_sha256
                    or identity.get("benchmark") != fold_manifest.benchmark
                    or identity.get("sessions_sha256") != expected_sessions_sha256
                    or identity.get("session_count") != len(fold.sessions)
                    or identity.get("first_session") != fold.start_date
                    or identity.get("last_session") != fold.end_date
                ):
                    raise ValueError("discovery exposure reservation differs from fold")
                closed_identity = ValidationWindowIdentity(**identity)
                closed_metadata = ValidationExposureMetadata(**metadata)
                selected.append(record)
                selected_identities.append(closed_identity)
                selected_metadata.append(closed_metadata)
            if not records:
                raise ValueError("discovery exposure ledger is empty")
            left, right = selected_identities
            if (
                left.pit_bundle_sha256,
                left.universe_sha256,
                left.benchmark,
                left.warmup_contract_sha256,
            ) != (
                right.pit_bundle_sha256,
                right.universe_sha256,
                right.benchmark,
                right.warmup_contract_sha256,
            ):
                raise ValueError("discovery exposure window lineage differs")
            if selected_metadata[0] != selected_metadata[1]:
                raise ValueError("discovery exposure metadata lineage differs")
            return DiscoveryExposureProof(
                fold_ids=tuple(
                    fold.fold_id for fold in fold_manifest.discovery_folds
                ),
                window_identities=tuple(selected_identities),
                metadata=selected_metadata[0],
                reservation_record_sha256s=tuple(
                    str(item["record_sha256"]) for item in selected
                ),
                ledger_head_sha256=str(records[-1]["record_sha256"]),
                _controller_seal=_DISCOVERY_EXPOSURE_SEAL,
            )

    def record_outcome(
        self,
        reservation: ValidationReservation,
        *,
        attempted: bool,
        completed: bool,
        failure_code: str | None,
    ) -> ValidationOutcomeProof:
        if not isinstance(reservation, ValidationReservation):
            raise ValueError("validation outcome requires a closed reservation")
        self._validate_outcome_fields(
            attempted=attempted,
            completed=completed,
            failure_code=failure_code,
        )
        with _validation_file_lock(self._lock_path):
            records = self._read_records()
            consumption = next(
                (
                    record
                    for record in records
                    if record.get("record_type") == "consumption"
                    and record.get("record_sha256")
                    == reservation.reservation_record_sha256
                    and record.get("consumption_key_sha256")
                    == reservation.consumption_key_sha256
                ),
                None,
            )
            if consumption is None:
                raise ValueError("validation reservation is not present in this ledger")
            if any(
                record.get("record_type") == "outcome"
                and record.get("reservation_record_sha256")
                == reservation.reservation_record_sha256
                for record in records
            ):
                raise ValueError("validation reservation outcome is already recorded")
            record = self._append_record(
                records,
                {
                    "record_type": "outcome",
                    "reservation_record_sha256": reservation.reservation_record_sha256,
                    "attempted": attempted,
                    "completed": completed,
                    "failure_code": failure_code,
                },
            )
            record_sha256 = str(record["record_sha256"])
            return ValidationOutcomeProof(
                reservation_record_sha256=reservation.reservation_record_sha256,
                attempted=attempted,
                completed=completed,
                failure_code=failure_code,
                outcome_record_sha256=record_sha256,
                ledger_head_sha256=record_sha256,
            )


def _absolute_cli_path(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an explicit absolute path")
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    return candidate.resolve(strict=False)


def _read_canonical_mapping(path: Path, label: str) -> Mapping[str, object]:
    candidate = _absolute_cli_path(path, label)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be an existing regular non-link file")
    raw = candidate.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json_bytes(value):
        raise ValueError(f"{label} is not canonical JSON")
    return value


def qualification_retirement_domain_id(
    *,
    prices_provenance_sha256: str,
    lineages: tuple[PanelSecurityLineage, ...],
) -> str:
    """Bind the permanent retirement domain only to stable lineage provenance."""

    _require_digest(
        prices_provenance_sha256,
        "qualification retirement prices provenance SHA-256",
    )
    _validate_panel_lineages(lineages)
    primitive = {
        "schema_version": 4,
        "namespace_kind": "authenticated_price_identity_security_lineages_v1",
        "prices_provenance_sha256": prices_provenance_sha256,
        "lineages": [
            _panel_json_value(item)
            for item in sorted(lineages, key=lambda value: value.security_lineage_id)
        ],
    }
    return hashlib.sha256(_canonical_json_bytes(primitive)).hexdigest()


def _lineages_from_authenticated_bundle(
    bundle: object,
    transition_contract: object,
) -> tuple[PanelSecurityLineage, ...]:
    metadata = getattr(bundle, "metadata", None)
    if not isinstance(metadata, Mapping) or metadata.get("schema_version") != "2":
        raise ValueError("panel allocation requires a schema-v2 PIT bundle")
    source_universe = metadata.get("source_universe")
    if source_universe not in _SOURCE_AFFILIATIONS:
        raise ValueError("panel bundle source affiliation is invalid")
    raw_security_lineages = bundle.security_lineage_ids()
    identities = getattr(transition_contract, "identities", None)
    if not isinstance(raw_security_lineages, Mapping) or not isinstance(
        identities,
        Mapping,
    ):
        raise ValueError("panel bundle lineage identities are absent")
    grouped: dict[str, list[str]] = {}
    for ticker, lineage_id in raw_security_lineages.items():
        if not isinstance(ticker, str) or not isinstance(lineage_id, str):
            raise ValueError("panel bundle lineage binding is invalid")
        grouped.setdefault(lineage_id, []).append(ticker)
    result: list[PanelSecurityLineage] = []
    for lineage_id, tickers in grouped.items():
        ordered = tuple(
            sorted(
                tickers,
                key=lambda ticker: (
                    str(identities[ticker]["admitted_start"]),
                    str(identities[ticker]["admitted_end"]),
                    ticker,
                ),
            )
        )
        result.append(
            PanelSecurityLineage(
                security_lineage_id=lineage_id,
                executable_tickers=ordered,
                source_affiliations=(str(source_universe),),
            )
        )
    lineages = tuple(sorted(result, key=lambda item: item.security_lineage_id))
    _validate_panel_lineages(lineages)
    return lineages


def build_and_publish_panel_plans(
    *,
    pit_bundle: Path,
    pit_bundle_sha256: str,
    prices_provenance: Path,
    start_date: str,
    end_date: str,
    partition_seed: str,
    rule: PanelAllocationRuleV1,
    target: AnnualizedReturnTarget,
    qualification_ledger: Path,
    output_root: Path,
) -> dict[str, object]:
    """Authenticate local inputs and publish one immutable schema-v4 panel pair."""

    _require_digest(pit_bundle_sha256, "panel PIT bundle SHA-256")
    if (
        not isinstance(partition_seed, str)
        or not partition_seed.strip()
        or partition_seed != partition_seed.strip()
        or len(partition_seed.encode("utf-8")) > 256
    ):
        raise ValueError("panel partition seed is invalid")
    start = _date(start_date, "panel start date")
    end = _date(end_date, "panel end date")
    if start >= end:
        raise ValueError("panel date range is invalid")
    bundle_path = Path(pit_bundle).resolve()
    provenance_path = Path(prices_provenance).resolve()
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise ValueError("panel PIT bundle must be a regular non-link file")
    if provenance_path.is_symlink() or not provenance_path.is_file():
        raise ValueError("panel prices provenance must be a regular non-link file")
    from core.pit_data import PITDataBundle

    with PITDataBundle(bundle_path, expected_sha256=pit_bundle_sha256) as bundle:
        transition_contract = bundle.load_price_identity_transition_contract(
            provenance_path
        )
        prices_provenance_sha256 = transition_contract.prices_provenance_sha256
        lineages = _lineages_from_authenticated_bundle(bundle, transition_contract)
        import pandas as pd

        reference = bundle.fetch_price_data(
            ("SPY",),
            pd.Timestamp(start_date),
            pd.Timestamp(end_date),
        ).get("SPY")
        if reference is None or reference.empty:
            raise ValueError("panel reference sessions are absent")
        sessions = tuple(index.date().isoformat() for index in reference.index)
        if sessions[0] != start_date or sessions[-1] != end_date:
            raise ValueError("panel bounds must be exact market sessions")
    domain_id = qualification_retirement_domain_id(
        prices_provenance_sha256=prices_provenance_sha256,
        lineages=lineages,
    )
    ledger = QualificationRetirementLedger(
        Path(qualification_ledger).resolve(strict=False),
        domain_id,
    )
    partition_seed_sha256 = hashlib.sha256(
        partition_seed.encode("utf-8")
    ).hexdigest()
    with ledger.locked_snapshot() as snapshot:
        qualification_plan, discovery_plan = compose_panel_plans(
            lineages=lineages,
            sessions=sessions,
            pit_bundle_sha256=pit_bundle_sha256,
            prices_provenance_sha256=prices_provenance_sha256,
            partition_seed_sha256=partition_seed_sha256,
            target=target,
            rule=rule,
            ledger_snapshot=snapshot,
        )
        return publish_panel_plans(
            output_root=Path(output_root),
            qualification_plan=qualification_plan,
            discovery_plan=discovery_plan,
        )


def _manifest_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.pit_optimizer_evaluation",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-subset-manifest", allow_abbrev=False)
    for flag in (
        "readiness",
        "verified-parity",
        "pit-bundle",
        "baseline-run",
        "source-root",
        "permanent-runtime-root",
        "controller-temp-parent",
        "artifact-root",
        "git-executable",
        "docker-executable",
        "output",
    ):
        build.add_argument(f"--{flag}", type=Path, required=True)
    build.add_argument("--parity-reference", type=Path)
    build.add_argument("--sandbox-image", required=True)
    build.add_argument("--iterations", type=int, required=True)
    for role in ("investigator", "author", "critic"):
        for suffix in (
            "static-bytes",
            "dynamic-bytes",
            "input-tokens",
            "output-tokens",
            "response-bytes",
        ):
            build.add_argument(f"--{role}-{suffix}", type=int, required=True)
    build.add_argument("--max-files", type=int, required=True)
    build.add_argument("--max-hunks", type=int, required=True)
    build.add_argument("--max-changed-lines", type=int, required=True)
    build.add_argument("--max-diff-bytes", type=int, required=True)
    panels = commands.add_parser("build-panel-plans", allow_abbrev=False)
    panels.add_argument("--pit-bundle", type=Path, required=True)
    panels.add_argument("--pit-bundle-sha256", required=True)
    panels.add_argument("--prices-provenance", type=Path, required=True)
    panels.add_argument("--start-date", required=True)
    panels.add_argument("--end-date", required=True)
    panels.add_argument("--partition-seed", required=True)
    panels.add_argument("--quick-count", type=int, required=True)
    panels.add_argument("--discovery-count", type=int, required=True)
    panels.add_argument("--qualification-count", type=int, required=True)
    panels.add_argument("--target-pct", required=True)
    panels.add_argument("--qualification-ledger", type=Path, required=True)
    panels.add_argument("--output-root", type=Path, required=True)
    return parser


def _call_budgets_from_namespace(namespace: argparse.Namespace) -> tuple[object, ...]:
    from core.pit_optimization_contract import (
        OPTIMIZER_V2_ROLES,
        PIT_OPTIMIZER_R1_MODEL,
        PitOptimizerCallBudget,
    )

    budgets = []
    for iteration in range(1, namespace.iterations + 1):
        for ordinal, role in enumerate(OPTIMIZER_V2_ROLES, start=1):
            prefix = role.replace("-", "_")
            budgets.append(
                PitOptimizerCallBudget(
                    call_index=(iteration - 1) * 3 + ordinal,
                    iteration=iteration,
                    role=role,
                    model=PIT_OPTIMIZER_R1_MODEL,
                    max_static_input_bytes=getattr(
                        namespace,
                        f"{prefix}_static_bytes",
                    ),
                    max_dynamic_input_bytes=getattr(
                        namespace,
                        f"{prefix}_dynamic_bytes",
                    ),
                    max_input_tokens=getattr(
                        namespace,
                        f"{prefix}_input_tokens",
                    ),
                    max_output_tokens=getattr(
                        namespace,
                        f"{prefix}_output_tokens",
                    ),
                    max_response_bytes=getattr(
                        namespace,
                        f"{prefix}_response_bytes",
                    ),
                )
            )
    return tuple(budgets)


def main(argv: Sequence[str] | None = None) -> int:
    """Build provider-free schema-v3 audit or schema-v4 panel artifacts."""
    namespace = _manifest_cli_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    if namespace.command == "build-panel-plans":
        target = AnnualizedReturnTarget(
            metric_id="portfolio_annualized_return_pct",
            formula_id="production_equity_cagr_365_calendar_days_v1",
            basis="absolute",
            target_pct=Decimal(namespace.target_pct),
            milestones_pct=(
                Decimal("10.00"),
                Decimal("20.00"),
                Decimal("50.00"),
            ),
            precision_pct=Decimal("0.01"),
        )
        publication = build_and_publish_panel_plans(
            pit_bundle=namespace.pit_bundle,
            pit_bundle_sha256=namespace.pit_bundle_sha256,
            prices_provenance=namespace.prices_provenance,
            start_date=namespace.start_date,
            end_date=namespace.end_date,
            partition_seed=namespace.partition_seed,
            rule=PanelAllocationRuleV1(
                quick_count=namespace.quick_count,
                discovery_count=namespace.discovery_count,
                qualification_count=namespace.qualification_count,
            ),
            target=target,
            qualification_ledger=namespace.qualification_ledger,
            output_root=namespace.output_root,
        )
        print(
            "PIT_OPTIMIZER_PANEL_PLANS="
            + json.dumps(
                publication,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return 0
    if namespace.command != "build-subset-manifest":
        raise ValueError("unknown PIT optimizer evaluation command")
    from core.pit_optimization_contract import (
        PatchBounds,
        build_prepare_command,
        build_subset_manifest,
        write_optimizer_manifest,
    )
    from core.pit_optimizer_controller import _load_parity
    from core.pit_policy_parity import load_parity_reference

    readiness_path = _absolute_cli_path(namespace.readiness, "readiness artifact")
    parity_path = _absolute_cli_path(
        namespace.verified_parity,
        "verified parity artifact",
    )
    parity_reference = (
        None
        if namespace.parity_reference is None
        else load_parity_reference(
            _absolute_cli_path(
                namespace.parity_reference,
                "parity reference artifact",
            )
        )
    )
    readiness = _read_canonical_mapping(readiness_path, "readiness artifact")
    parity = _load_parity(parity_path)
    manifest = build_subset_manifest(
        legacy_readiness=readiness,
        legacy_readiness_path=readiness_path,
        parity_attestation=parity,
        verified_parity_path=parity_path,
        pit_bundle=_absolute_cli_path(namespace.pit_bundle, "PIT bundle"),
        baseline_run=_absolute_cli_path(namespace.baseline_run, "baseline run"),
        source_root=_absolute_cli_path(namespace.source_root, "source root"),
        permanent_runtime_root=_absolute_cli_path(
            namespace.permanent_runtime_root,
            "permanent runtime root",
        ),
        controller_temp_parent=_absolute_cli_path(
            namespace.controller_temp_parent,
            "controller temporary parent",
        ),
        artifact_root=_absolute_cli_path(namespace.artifact_root, "artifact root"),
        sandbox_image=namespace.sandbox_image,
        call_budgets=_call_budgets_from_namespace(namespace),
        candidate_bounds=PatchBounds(
            namespace.max_files,
            namespace.max_hunks,
            namespace.max_changed_lines,
            namespace.max_diff_bytes,
        ),
        max_iterations=namespace.iterations,
        parity_reference=parity_reference,
    )
    output = _absolute_cli_path(namespace.output, "optimizer manifest output")
    written, digest = write_optimizer_manifest(manifest, output)
    command = build_prepare_command(
        manifest,
        manifest_path=written,
        legacy_readiness_path=readiness_path,
        verified_parity_path=parity_path,
        pit_bundle_path=_absolute_cli_path(namespace.pit_bundle, "PIT bundle"),
        baseline_run_path=_absolute_cli_path(namespace.baseline_run, "baseline run"),
        repo_root=_absolute_cli_path(namespace.source_root, "source root"),
        permanent_runtime_root=_absolute_cli_path(
            namespace.permanent_runtime_root,
            "permanent runtime root",
        ),
        controller_temp_parent=_absolute_cli_path(
            namespace.controller_temp_parent,
            "controller temporary parent",
        ),
        artifact_root=_absolute_cli_path(namespace.artifact_root, "artifact root"),
        git_executable=_absolute_cli_path(namespace.git_executable, "Git executable"),
        docker_executable=_absolute_cli_path(
            namespace.docker_executable,
            "Docker executable",
        ),
        sandbox_image=namespace.sandbox_image,
    )
    print(
        "PIT_OPTIMIZER_MANIFEST="
        + json.dumps(
            {
                "artifact": str(written),
                "sha256": digest,
                "manifest": manifest.to_primitive(),
                "authorization": {
                    "max_calls": manifest.authorization_requirement.max_calls,
                    "max_tokens": manifest.authorization_requirement.max_tokens,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    print("PIT_OPTIMIZER_PREPARE_COMMAND=" + command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
