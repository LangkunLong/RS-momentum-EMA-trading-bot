"""Immutable fold and aggregate contracts for the PIT optimizer."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import InitVar, asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
import os
from pathlib import Path
import re
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
    """Build one provider-free schema-v3 subset manifest and prepare command."""
    namespace = _manifest_cli_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
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
