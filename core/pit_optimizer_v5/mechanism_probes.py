"""Bounded paired observations for the registered V5 mechanism slice.

This module owns only deterministic, controller-authored observation records.
It never imports candidate source or invokes a policy fallback.  A caller must
inject two explicitly registered worker ports.  The included fixture port is a
small in-process table of already-typed decisions for focused synthetic tests;
it does not represent a sandbox or resource-isolated candidate execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import math
import re
import time
from typing import Literal, Protocol, runtime_checkable

from core.strategy_policy.contracts import ExitDecision, validate_exit_decision
from core.strategy_policy.contracts_v3 import ExitSnapshotV3

from .mechanism_contracts import (
    MechanismCoverageV1,
    MechanismExecutionV1,
    MechanismExperimentSpecV1,
    MechanismObservationBindingV1,
    MechanismResourceBudgetV1,
    validate_mechanism_observation_binding_v1,
)
from .probes import canonical_probe_json_v5


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CASES_V1 = 100_000
_MAX_REPETITIONS_V1 = 64
_WORKER_PORT_VERSION_V1 = "mechanism-worker-port-v1"

MechanismWorkerRoleV1 = Literal["parent", "candidate"]
MechanismWorkerKindV1 = Literal["synthetic_fixture", "registered_sandbox"]
MechanismResetSemanticsV1 = Literal["reset_per_case"]


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: object, label: str) -> bytes:
    to_json = getattr(value, "to_canonical_json", None)
    if not callable(to_json):
        raise ValueError(f"{label} cannot be canonically encoded")
    try:
        raw = to_json()
        encoded = raw.encode("utf-8")
    except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} cannot be canonically encoded") from exc
    if not encoded or b"\n" in encoded or b"\r" in encoded:
        raise ValueError(f"{label} canonical encoding is invalid")
    return encoded


def _decimal_from_snapshot_value(value: float | None) -> Decimal | None:
    """Convert a supplied V3 float for the frozen Decimal predicate.

    ``str(float)`` is intentional: it uses the V3 snapshot's displayed finite
    value without importing binary floating-point tail digits into the
    precommitted Decimal predicate.  The original recipe Decimal remains the
    case identity, so this conversion cannot merge distinct cases silently.
    """

    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("snapshot ATR fraction must be a finite float or None")
    return Decimal(str(value))


def _validate_snapshot(snapshot: object) -> tuple[ExitSnapshotV3, bytes, str]:
    if type(snapshot) is not ExitSnapshotV3:
        raise ValueError("mechanism observation snapshot is invalid")
    encoded = _canonical_json_bytes(snapshot, "mechanism observation snapshot")
    try:
        decoded = ExitSnapshotV3.from_canonical_json(encoded.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("mechanism observation snapshot is not canonical") from exc
    if decoded != snapshot or _canonical_json_bytes(decoded, "mechanism observation snapshot") != encoded:
        raise ValueError("mechanism observation snapshot is not canonical")
    return snapshot, encoded, _sha256(encoded)


def _validate_decision(snapshot: ExitSnapshotV3, decision: object) -> tuple[ExitDecision, bytes, str]:
    if type(decision) is not ExitDecision:
        raise ValueError("worker returned a non-exit decision")
    encoded = _canonical_json_bytes(decision, "worker exit decision")
    try:
        decoded = ExitDecision.from_canonical_json(encoded.decode("utf-8"))
        validate_exit_decision(snapshot.base, decoded)
    except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("worker returned an invalid exit decision") from exc
    if decoded != decision or _canonical_json_bytes(decoded, "worker exit decision") != encoded:
        raise ValueError("worker returned a non-canonical exit decision")
    return decoded, encoded, _sha256(encoded)


@dataclass(frozen=True, slots=True)
class MechanismPairedCaseV1:
    """One recipe input rendered into one trusted V3 exit snapshot."""

    order: int
    input_value: Decimal | None
    input_canonical_bytes: bytes
    input_identity_sha256: str
    converted_value: float | None
    snapshot: ExitSnapshotV3
    snapshot_canonical_json: bytes
    snapshot_sha256: str
    applicable: bool

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("mechanism case order is invalid")
        if self.input_value is not None:
            if type(self.input_value) is not Decimal or not self.input_value.is_finite():
                raise ValueError("mechanism case input is invalid")
            if self.input_value < 0 or self.input_value > 1:
                raise ValueError("mechanism case input is out of bounds")
        if type(self.input_canonical_bytes) is not bytes or not self.input_canonical_bytes:
            raise ValueError("mechanism case input canonical bytes are invalid")
        expected_input = canonical_probe_json_v5(self.input_value)
        if self.input_canonical_bytes != expected_input:
            raise ValueError("mechanism case input identity is not canonical")
        if self.input_identity_sha256 != _sha256(self.input_canonical_bytes):
            raise ValueError("mechanism case input identity does not reconcile")
        if self.converted_value is None:
            if self.input_value is not None:
                raise ValueError("missing converted input does not reconcile")
        else:
            if type(self.converted_value) is not float or not math.isfinite(self.converted_value):
                raise ValueError("mechanism case converted input is invalid")
            if self.converted_value < 0 or self.converted_value > 1:
                raise ValueError("mechanism case converted input is out of bounds")
            if self.input_value is None or float(self.input_value) != self.converted_value:
                raise ValueError("mechanism case converted input does not reconcile")
        snapshot, encoded, snapshot_sha256 = _validate_snapshot(self.snapshot)
        if self.snapshot_canonical_json != encoded or self.snapshot_sha256 != snapshot_sha256:
            raise ValueError("mechanism case snapshot identity does not reconcile")
        observed = snapshot.features.atr_20_fraction
        if observed != self.converted_value:
            raise ValueError("mechanism case snapshot input does not reconcile")
        if type(self.applicable) is not bool:
            raise ValueError("mechanism case applicability is invalid")


@dataclass(frozen=True, slots=True)
class MechanismObservationCorpusV1:
    """Ordered, immutable recipe cases bound to the registered input recipe."""

    recipe_sha256: str
    cases: tuple[MechanismPairedCaseV1, ...]

    def __post_init__(self) -> None:
        _digest(self.recipe_sha256, "observation corpus recipe")
        if type(self.cases) is not tuple or not self.cases or len(self.cases) > _MAX_CASES_V1:
            raise ValueError("observation corpus cases are invalid")
        if any(type(case) is not MechanismPairedCaseV1 for case in self.cases):
            raise ValueError("observation corpus case is invalid")
        if tuple(case.order for case in self.cases) != tuple(range(len(self.cases))):
            raise ValueError("observation corpus case order is not contiguous")
        if len({case.input_identity_sha256 for case in self.cases}) != len(self.cases):
            raise ValueError("observation corpus input identities must be unique")
        if len({case.snapshot_sha256 for case in self.cases}) != len(self.cases):
            raise ValueError("observation corpus converted inputs must be unique")

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_probe_json_v5(
            (
                self.recipe_sha256,
                tuple(
                    (
                        case.order,
                        case.input_canonical_bytes,
                        case.input_identity_sha256,
                        case.converted_value,
                        case.snapshot_canonical_json,
                        case.snapshot_sha256,
                        case.applicable,
                    )
                    for case in self.cases
                ),
            )
        )

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes)


def build_mechanism_observation_corpus_v1(
    spec: MechanismExperimentSpecV1,
    *,
    seed_snapshot: ExitSnapshotV3,
) -> MechanismObservationCorpusV1:
    """Render only the frozen ATR recipe against a trusted V3 exit seed."""

    if type(spec) is not MechanismExperimentSpecV1:
        raise ValueError("mechanism observation spec is invalid")
    if spec.target_method != "evaluate_exit" or spec.recipe.recipe_id != "evaluate_exit_atr20_fraction_v1":
        raise ValueError("mechanism observation recipe is unsupported")
    seed, _seed_json, _seed_sha256 = _validate_snapshot(seed_snapshot)
    cases: list[MechanismPairedCaseV1] = []
    seen_snapshots: set[str] = set()
    for order, input_value in enumerate(spec.recipe.input_values):
        converted = None if input_value is None else float(input_value)
        if converted is not None and not math.isfinite(converted):
            raise ValueError("recipe input converts to a non-finite float")
        features = seed.features
        features = type(features)(
            current_rs_score=features.current_rs_score,
            industry_group_rs=features.industry_group_rs,
            atr_20_fraction=converted,
            volume_ratio=features.volume_ratio,
        )
        snapshot = type(seed)(
            base=seed.base,
            features=features,
            unrealized_return_fraction=seed.unrealized_return_fraction,
            maximum_favorable_excursion_fraction=seed.maximum_favorable_excursion_fraction,
            maximum_adverse_excursion_fraction=seed.maximum_adverse_excursion_fraction,
            position_notional_fraction=seed.position_notional_fraction,
        )
        snapshot, snapshot_json, snapshot_sha256 = _validate_snapshot(snapshot)
        if snapshot_sha256 in seen_snapshots:
            raise ValueError("distinct recipe inputs alias the same converted float")
        seen_snapshots.add(snapshot_sha256)
        input_json = canonical_probe_json_v5(input_value)
        applicable = spec.applicability.matches(_decimal_from_snapshot_value(converted))
        cases.append(
            MechanismPairedCaseV1(
                order=order,
                input_value=input_value,
                input_canonical_bytes=input_json,
                input_identity_sha256=_sha256(input_json),
                converted_value=converted,
                snapshot=snapshot,
                snapshot_canonical_json=snapshot_json,
                snapshot_sha256=snapshot_sha256,
                applicable=applicable,
            )
        )
    return MechanismObservationCorpusV1(recipe_sha256=spec.recipe.sha256, cases=tuple(cases))


def validate_mechanism_observation_corpus_v1(
    spec: MechanismExperimentSpecV1,
    binding: MechanismObservationBindingV1,
    corpus: MechanismObservationCorpusV1,
) -> None:
    """Validate a rendered corpus against the frozen spec before reduction or execution."""

    if type(spec) is not MechanismExperimentSpecV1 or type(binding) is not MechanismObservationBindingV1:
        raise ValueError("mechanism observation spec/binding is invalid")
    if type(corpus) is not MechanismObservationCorpusV1:
        raise ValueError("mechanism observation corpus is invalid")
    validate_mechanism_observation_binding_v1(spec, binding)
    if corpus.recipe_sha256 != spec.recipe.sha256:
        raise ValueError("mechanism observation corpus recipe differs from the frozen recipe")
    if corpus.sha256 != binding.corpus_sha256:
        raise ValueError("mechanism observation corpus differs from the exact binding")
    expected_input_bytes = tuple(canonical_probe_json_v5(value) for value in spec.recipe.input_values)
    if len(corpus.cases) != len(expected_input_bytes):
        raise ValueError("mechanism observation corpus input count differs from the frozen recipe")
    for case, expected_bytes in zip(corpus.cases, expected_input_bytes, strict=True):
        expected_identity = _sha256(expected_bytes)
        if case.input_canonical_bytes != expected_bytes or case.input_identity_sha256 != expected_identity:
            raise ValueError(
                "mechanism observation corpus canonical recipe input identity differs from the frozen recipe"
            )
    actual_inputs = tuple(case.input_value for case in corpus.cases)
    if actual_inputs != spec.recipe.input_values:
        raise ValueError("mechanism observation corpus inputs differ from the frozen recipe order")
    for case in corpus.cases:
        actual_snapshot_value = case.snapshot.features.atr_20_fraction
        expected_snapshot_value = None if case.input_value is None else float(case.input_value)
        if actual_snapshot_value != expected_snapshot_value or case.converted_value != actual_snapshot_value:
            raise ValueError("mechanism observation corpus converted snapshot differs from the recipe")
        expected_applicability = spec.applicability.matches(_decimal_from_snapshot_value(actual_snapshot_value))
        if case.applicable != expected_applicability:
            raise ValueError("mechanism observation corpus applicability differs from the frozen spec")


@dataclass(frozen=True, slots=True)
class MechanismWorkerRegistrationV1:
    """Identity and bounds asserted by one injected worker port."""

    experiment_id: str
    spec_sha256: str
    role: MechanismWorkerRoleV1
    parent_revision_sha256: str
    candidate_bytes_sha256: str
    corpus_sha256: str
    resource_budget: MechanismResourceBudgetV1
    execution_kind: MechanismWorkerKindV1
    reset_semantics: MechanismResetSemanticsV1
    cpu_memory_enforced: bool

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "worker experiment ID")
        _digest(self.spec_sha256, "worker spec SHA-256")
        if type(self.role) is not str or self.role not in {"parent", "candidate"}:
            raise ValueError("worker role is invalid")
        _digest(self.parent_revision_sha256, "worker parent revision SHA-256")
        _digest(self.candidate_bytes_sha256, "worker candidate bytes SHA-256")
        _digest(self.corpus_sha256, "worker corpus SHA-256")
        if type(self.resource_budget) is not MechanismResourceBudgetV1:
            raise ValueError("worker resource budget is invalid")
        if type(self.execution_kind) is not str or self.execution_kind not in {
            "synthetic_fixture",
            "registered_sandbox",
        }:
            raise ValueError("worker execution kind is unsupported")
        if type(self.reset_semantics) is not str or self.reset_semantics != "reset_per_case":
            raise ValueError("worker reset semantics are unsupported")
        if type(self.cpu_memory_enforced) is not bool:
            raise ValueError("worker CPU and memory enforcement flag is invalid")


@runtime_checkable
class MechanismWorkerPortV1(Protocol):
    """The only execution surface accepted by the paired observer."""

    registration: MechanismWorkerRegistrationV1

    def open(self) -> object: ...

    def reset(self, session: object, case: MechanismPairedCaseV1) -> None: ...

    def evaluate(
        self,
        session: object,
        case: MechanismPairedCaseV1,
        *,
        deadline_monotonic: float | None,
    ) -> object: ...

    def close(self, session: object) -> None: ...


class SyntheticFixtureWorkerV1:
    """A typed decision table for deterministic local fixture tests."""

    def __init__(
        self,
        *,
        registration: MechanismWorkerRegistrationV1,
        decisions_by_input_identity: Mapping[str, object],
    ) -> None:
        if type(registration) is not MechanismWorkerRegistrationV1:
            raise ValueError("synthetic fixture registration is invalid")
        if not isinstance(decisions_by_input_identity, Mapping):
            raise ValueError("synthetic fixture decisions are invalid")
        self.registration = registration
        self.decisions_by_input_identity = dict(decisions_by_input_identity)
        self.failure: BaseException | None = None
        self.cleanup_failure: BaseException | None = None
        self.opened = False
        self.closed = False
        self.call_count = 0
        self.reset_count = 0

    def open(self) -> object:
        if self.opened and not self.closed:
            raise RuntimeError("synthetic fixture worker is already open")
        self.opened = True
        self.closed = False
        return self

    def reset(self, session: object, case: MechanismPairedCaseV1) -> None:
        if session is not self or not self.opened or self.closed:
            raise RuntimeError("synthetic fixture worker session is invalid")
        if type(case) is not MechanismPairedCaseV1:
            raise ValueError("synthetic fixture case is invalid")
        self.reset_count += 1
        if self.failure is not None:
            raise self.failure

    def evaluate(
        self,
        session: object,
        case: MechanismPairedCaseV1,
        *,
        deadline_monotonic: float | None,
    ) -> object:
        if session is not self or not self.opened or self.closed:
            raise RuntimeError("synthetic fixture worker session is invalid")
        if type(case) is not MechanismPairedCaseV1:
            raise ValueError("synthetic fixture case is invalid")
        self.call_count += 1
        if self.failure is not None:
            raise self.failure
        try:
            return self.decisions_by_input_identity[case.input_identity_sha256]
        except KeyError as exc:
            raise ValueError("synthetic fixture has no decision for the bound input") from exc

    def close(self, session: object) -> None:
        if session is not self:
            raise RuntimeError("synthetic fixture worker session is invalid")
        self.closed = True
        if self.cleanup_failure is not None:
            raise self.cleanup_failure


@dataclass(frozen=True, slots=True)
class MechanismPairedObservationV1:
    """One stable parent/candidate observation with its canonical bytes retained."""

    corpus_sha256: str
    case_order: int
    input_value: Decimal | None
    input_canonical_bytes: bytes
    input_identity_sha256: str
    converted_value: float | None
    case_snapshot_json: bytes
    snapshot_sha256: str
    applicable: bool
    repetition: int
    parent_decision_json: bytes
    parent_decision_sha256: str
    candidate_decision_json: bytes
    candidate_decision_sha256: str
    decision_changed: bool
    protected_control_unchanged: bool

    def __post_init__(self) -> None:
        _digest(self.corpus_sha256, "paired observation corpus SHA-256")
        if type(self.case_order) is not int or self.case_order < 0:
            raise ValueError("paired observation case order is invalid")
        if type(self.repetition) is not int or self.repetition < 0:
            raise ValueError("paired observation repetition is invalid")
        if type(self.case_snapshot_json) is not bytes or not self.case_snapshot_json:
            raise ValueError("paired observation snapshot bytes are invalid")
        if type(self.parent_decision_json) is not bytes or not self.parent_decision_json:
            raise ValueError("paired observation parent decision bytes are invalid")
        if type(self.candidate_decision_json) is not bytes or not self.candidate_decision_json:
            raise ValueError("paired observation candidate decision bytes are invalid")
        try:
            snapshot = ExitSnapshotV3.from_canonical_json(self.case_snapshot_json.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("paired observation snapshot bytes are not canonical") from exc
        case = MechanismPairedCaseV1(
            order=self.case_order,
            input_value=self.input_value,
            input_canonical_bytes=self.input_canonical_bytes,
            input_identity_sha256=self.input_identity_sha256,
            converted_value=self.converted_value,
            snapshot=snapshot,
            snapshot_canonical_json=self.case_snapshot_json,
            snapshot_sha256=self.snapshot_sha256,
            applicable=self.applicable,
        )
        try:
            parent_decision = ExitDecision.from_canonical_json(self.parent_decision_json.decode("utf-8"))
            candidate_decision = ExitDecision.from_canonical_json(self.candidate_decision_json.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("paired observation decision bytes are not canonical") from exc
        _parent, parent_json, parent_sha256 = _validate_decision(case.snapshot, parent_decision)
        _candidate, candidate_json, candidate_sha256 = _validate_decision(case.snapshot, candidate_decision)
        if parent_json != self.parent_decision_json or parent_sha256 != self.parent_decision_sha256:
            raise ValueError("paired observation parent decision identity does not reconcile")
        if candidate_json != self.candidate_decision_json or candidate_sha256 != self.candidate_decision_sha256:
            raise ValueError("paired observation candidate decision identity does not reconcile")
        if type(self.decision_changed) is not bool or self.decision_changed != (parent_json != candidate_json):
            raise ValueError("paired observation decision change does not reconcile")
        if type(self.protected_control_unchanged) is not bool:
            raise ValueError("paired observation control flag is invalid")
        if self.protected_control_unchanged != (_parent.next_stop_price == _candidate.next_stop_price):
            raise ValueError("paired observation protected control flag does not reconcile")

    @property
    def action_changed(self) -> bool:
        parent = ExitDecision.from_canonical_json(self.parent_decision_json.decode("utf-8"))
        candidate = ExitDecision.from_canonical_json(self.candidate_decision_json.decode("utf-8"))
        return parent.actions != candidate.actions

    @property
    def next_stop_price_changed(self) -> bool:
        parent = ExitDecision.from_canonical_json(self.parent_decision_json.decode("utf-8"))
        candidate = ExitDecision.from_canonical_json(self.candidate_decision_json.decode("utf-8"))
        return parent.next_stop_price != candidate.next_stop_price


@dataclass(frozen=True, slots=True)
class MechanismObservationRunV1:
    """Bounded observation output consumed later by the Task 3 reducer."""

    binding: MechanismObservationBindingV1
    corpus: MechanismObservationCorpusV1
    execution: MechanismExecutionV1
    coverage: MechanismCoverageV1
    observations: tuple[MechanismPairedObservationV1, ...]
    repetitions: int
    reset_semantics: MechanismResetSemanticsV1
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not MechanismObservationBindingV1:
            raise ValueError("observation run binding is invalid")
        if type(self.corpus) is not MechanismObservationCorpusV1:
            raise ValueError("observation run corpus is invalid")
        if self.binding.corpus_sha256 != self.corpus.sha256:
            raise ValueError("observation run corpus differs from its binding")
        if type(self.execution) is not MechanismExecutionV1:
            raise ValueError("observation run execution is invalid")
        if type(self.coverage) is not MechanismCoverageV1:
            raise ValueError("observation run coverage is invalid")
        if type(self.repetitions) is not int or not 1 <= self.repetitions <= _MAX_REPETITIONS_V1:
            raise ValueError("observation run repetitions are invalid")
        if self.repetitions > self.binding.resource_budget.max_repetitions and not (
            self.execution.status == "failed" and self.execution.reason == "resource_limit"
        ):
            raise ValueError("observation run repetitions exceed its binding")
        if self.reset_semantics != "reset_per_case":
            raise ValueError("observation run reset semantics are unsupported")
        if type(self.observations) is not tuple or len(self.observations) > len(self.corpus.cases) * self.repetitions:
            raise ValueError("observation run observations are invalid")
        if any(type(item) is not MechanismPairedObservationV1 for item in self.observations):
            raise ValueError("observation run observation is invalid")
        actual_lattice = tuple((item.repetition, item.case_order) for item in self.observations)
        expected_lattice = tuple(
            (repetition, case_order)
            for repetition in range(self.repetitions)
            for case_order in range(len(self.corpus.cases))
        )
        if self.execution.status == "completed":
            if actual_lattice != expected_lattice:
                raise ValueError("completed observation run does not match the exact repetition/case lattice")
        elif self.execution.status == "failed":
            if actual_lattice != expected_lattice[: len(actual_lattice)]:
                raise ValueError("failed observation run is not a prefix of the repetition/case lattice")
        elif self.observations:
            raise ValueError("not_run observation run must not contain observations")
        if any(item.corpus_sha256 != self.corpus.sha256 for item in self.observations):
            raise ValueError("observation run contains a foreign corpus observation")
        if len(set(actual_lattice)) != len(actual_lattice):
            raise ValueError("observation run repeats a case observation")
        first_by_case_decisions: dict[int, tuple[bytes, bytes]] = {}
        for item in self.observations:
            if item.case_order >= len(self.corpus.cases):
                raise ValueError("observation run case order is outside its corpus")
            expected = self.corpus.cases[item.case_order]
            if (
                item.input_value != expected.input_value
                or item.input_canonical_bytes != expected.input_canonical_bytes
                or item.input_identity_sha256 != expected.input_identity_sha256
                or item.converted_value != expected.converted_value
                or item.case_snapshot_json != expected.snapshot_canonical_json
                or item.snapshot_sha256 != expected.snapshot_sha256
                or item.applicable != expected.applicable
            ):
                raise ValueError("observation run case identity differs from its corpus")
            prior_decisions = first_by_case_decisions.setdefault(
                item.case_order,
                (item.parent_decision_json, item.candidate_decision_json),
            )
            if prior_decisions != (item.parent_decision_json, item.candidate_decision_json):
                raise ValueError("repeated observation decisions are nondeterministic")
        if self.execution.status == "not_run":
            expected_coverage = _zero_coverage()
        else:
            expected_coverage = _coverage(self.corpus, self.observations)
        if self.coverage != expected_coverage:
            raise ValueError("observation run coverage does not reconcile with its observations")
        if type(self.limitations) is not tuple or any(
            type(item) is not str or not item.strip() or len(item) > 512 for item in self.limitations
        ):
            raise ValueError("observation run limitations are invalid")

    @property
    def relevant_case_count(self) -> int:
        return self.coverage.relevant_cases

    @property
    def decision_changed_count(self) -> int:
        first_by_case: dict[int, MechanismPairedObservationV1] = {}
        for item in self.observations:
            first_by_case.setdefault(item.case_order, item)
        return sum(item.decision_changed for item in first_by_case.values() if item.applicable)

    @property
    def protected_control_unchanged_count(self) -> int:
        first_by_case: dict[int, MechanismPairedObservationV1] = {}
        for item in self.observations:
            first_by_case.setdefault(item.case_order, item)
        return sum(item.protected_control_unchanged for item in first_by_case.values())

    @property
    def action_changed_count(self) -> int:
        return sum(item.action_changed for item in self.observations if item.repetition == 0 and item.applicable)


class _ObservationFailure(Exception):
    def __init__(
        self, reason: Literal["resource_limit", "timeout", "protocol_failure", "execution_failed"], message: str
    ):
        super().__init__(message)
        self.reason = reason


def _validate_worker_port(
    worker: object, *, binding: MechanismObservationBindingV1, role: MechanismWorkerRoleV1
) -> None:
    registration = getattr(worker, "registration", None)
    if type(registration) is not MechanismWorkerRegistrationV1:
        raise ValueError("worker port registration is invalid")
    if registration.experiment_id != binding.experiment_id:
        raise ValueError("worker port experiment binding differs")
    if registration.spec_sha256 != binding.spec_sha256:
        raise ValueError("worker port spec binding differs")
    if registration.role != role:
        raise ValueError("worker port role binding differs")
    if registration.parent_revision_sha256 != binding.parent_revision_sha256:
        raise ValueError("worker port parent binding differs")
    if registration.candidate_bytes_sha256 != binding.candidate_bytes_sha256:
        raise ValueError("worker port candidate binding differs")
    if registration.corpus_sha256 != binding.corpus_sha256:
        raise ValueError("worker port corpus binding differs")
    if registration.resource_budget != binding.resource_budget:
        raise ValueError("worker port resource binding differs")
    for method in ("open", "reset", "evaluate", "close"):
        if not callable(getattr(worker, method, None)):
            raise ValueError("worker port is missing a bounded operation")


def _coverage(
    corpus: MechanismObservationCorpusV1, observations: tuple[MechanismPairedObservationV1, ...]
) -> MechanismCoverageV1:
    first_by_case: dict[int, MechanismPairedObservationV1] = {}
    for observation in observations:
        first_by_case.setdefault(observation.case_order, observation)
    return MechanismCoverageV1(
        total_cases=len(first_by_case),
        relevant_cases=sum(item.applicable for item in first_by_case.values()),
        decision_changed_cases=sum(item.decision_changed for item in first_by_case.values() if item.applicable),
        protected_control_cases=len(first_by_case),
        protected_control_unchanged_cases=sum(item.protected_control_unchanged for item in first_by_case.values()),
        unsupported_cases=0,
        branch_coverage="unavailable",
    )


def _zero_coverage() -> MechanismCoverageV1:
    return MechanismCoverageV1(
        total_cases=0,
        relevant_cases=0,
        decision_changed_cases=0,
        protected_control_cases=0,
        protected_control_unchanged_cases=0,
        unsupported_cases=0,
        branch_coverage="unavailable",
    )


def _run(
    *,
    binding: MechanismObservationBindingV1,
    corpus: MechanismObservationCorpusV1,
    execution: MechanismExecutionV1,
    observations: tuple[MechanismPairedObservationV1, ...],
    repetitions: int,
    limitations: tuple[str, ...],
) -> MechanismObservationRunV1:
    return MechanismObservationRunV1(
        binding=binding,
        corpus=corpus,
        execution=execution,
        coverage=_coverage(corpus, observations),
        observations=observations,
        repetitions=repetitions,
        reset_semantics="reset_per_case",
        limitations=limitations,
    )


def collect_mechanism_observations_v1(
    spec: MechanismExperimentSpecV1,
    binding: MechanismObservationBindingV1,
    corpus: MechanismObservationCorpusV1,
    *,
    parent_worker: MechanismWorkerPortV1,
    candidate_worker: MechanismWorkerPortV1,
    repetitions: int = 1,
    deadline_monotonic: float | None = None,
) -> MechanismObservationRunV1:
    """Collect paired observations through two exact, bounded worker ports."""

    if type(spec) is not MechanismExperimentSpecV1 or type(binding) is not MechanismObservationBindingV1:
        raise ValueError("mechanism observation spec/binding is invalid")
    if type(corpus) is not MechanismObservationCorpusV1:
        raise ValueError("mechanism observation corpus is invalid")
    validate_mechanism_observation_corpus_v1(spec, binding, corpus)
    if type(repetitions) is not int or not 1 <= repetitions <= _MAX_REPETITIONS_V1:
        raise ValueError("observation repetitions are invalid")
    if deadline_monotonic is not None and (
        type(deadline_monotonic) is not float or not math.isfinite(deadline_monotonic)
    ):
        raise ValueError("observation deadline is invalid")
    if parent_worker is candidate_worker:
        raise ValueError("parent and candidate worker ports must be distinct")
    _validate_worker_port(parent_worker, binding=binding, role="parent")
    _validate_worker_port(candidate_worker, binding=binding, role="candidate")
    budget = binding.resource_budget
    if len(corpus.cases) > budget.max_cases or repetitions > budget.max_repetitions:
        return _run(
            binding=binding,
            corpus=corpus,
            execution=MechanismExecutionV1(status="failed", reason="resource_limit"),
            observations=(),
            repetitions=repetitions,
            limitations=("The bound case or repetition count exceeds the registered resource budget.",),
        )
    if (
        parent_worker.registration.execution_kind != "synthetic_fixture"
        or candidate_worker.registration.execution_kind != "synthetic_fixture"
    ):
        return _run(
            binding=binding,
            corpus=corpus,
            execution=MechanismExecutionV1(status="not_run", reason="worker_unavailable"),
            observations=(),
            repetitions=repetitions,
            limitations=("No registered sandbox adapter is available for nonfixture execution.",),
        )
    if parent_worker.registration.cpu_memory_enforced or candidate_worker.registration.cpu_memory_enforced:
        return _run(
            binding=binding,
            corpus=corpus,
            execution=MechanismExecutionV1(status="not_run", reason="worker_unavailable"),
            observations=(),
            repetitions=repetitions,
            limitations=("The synthetic fixture port cannot assert CPU or memory enforcement.",),
        )

    observations: list[MechanismPairedObservationV1] = []
    sessions: list[tuple[object, object]] = []
    execution_status: Literal["completed", "failed"] = "completed"
    execution_reason: Literal["completed", "resource_limit", "timeout", "protocol_failure", "execution_failed"] = (
        "completed"
    )
    limitations: list[str] = [
        "Synthetic fixture execution supplied typed decisions; it does not prove sandbox execution or resource isolation.",
        "CPU and peak memory were not measured or enforced by the synthetic fixture port.",
        "Branch coverage is unavailable without an independently instrumented worker.",
    ]

    def check_deadline() -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise _ObservationFailure("timeout", "caller-owned observation deadline expired")

    try:
        check_deadline()
        parent_session = parent_worker.open()
        sessions.append((parent_worker, parent_session))
        check_deadline()
        candidate_session = candidate_worker.open()
        sessions.append((candidate_worker, candidate_session))
        output_bytes = 0
        first_by_case: dict[int, MechanismPairedObservationV1] = {}
        for repetition in range(repetitions):
            for case in corpus.cases:
                check_deadline()
                try:
                    parent_worker.reset(parent_session, case)
                    candidate_worker.reset(candidate_session, case)
                    parent_raw = parent_worker.evaluate(
                        parent_session,
                        case,
                        deadline_monotonic=deadline_monotonic,
                    )
                    candidate_raw = candidate_worker.evaluate(
                        candidate_session,
                        case,
                        deadline_monotonic=deadline_monotonic,
                    )
                    parent, parent_json, parent_sha256 = _validate_decision(case.snapshot, parent_raw)
                    candidate, candidate_json, candidate_sha256 = _validate_decision(case.snapshot, candidate_raw)
                except _ObservationFailure:
                    raise
                except TimeoutError as exc:
                    raise _ObservationFailure("timeout", "bounded worker timed out") from exc
                except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
                    raise _ObservationFailure(
                        "protocol_failure", "bounded worker returned an invalid protocol value"
                    ) from exc
                except BaseException as exc:
                    raise _ObservationFailure("execution_failed", "bounded worker execution failed") from exc
                output_bytes += len(parent_json) + len(candidate_json)
                if output_bytes > budget.output_bytes:
                    raise _ObservationFailure("resource_limit", "bounded worker output exceeded its registered limit")
                observation = MechanismPairedObservationV1(
                    corpus_sha256=corpus.sha256,
                    case_order=case.order,
                    input_value=case.input_value,
                    input_canonical_bytes=case.input_canonical_bytes,
                    input_identity_sha256=case.input_identity_sha256,
                    converted_value=case.converted_value,
                    case_snapshot_json=case.snapshot_canonical_json,
                    snapshot_sha256=case.snapshot_sha256,
                    applicable=case.applicable,
                    repetition=repetition,
                    parent_decision_json=parent_json,
                    parent_decision_sha256=parent_sha256,
                    candidate_decision_json=candidate_json,
                    candidate_decision_sha256=candidate_sha256,
                    decision_changed=parent_json != candidate_json,
                    protected_control_unchanged=parent.next_stop_price == candidate.next_stop_price,
                )
                if repetition > 0:
                    prior = first_by_case.get(case.order)
                    if prior is None or (
                        prior.parent_decision_json != observation.parent_decision_json
                        or prior.candidate_decision_json != observation.candidate_decision_json
                    ):
                        raise _ObservationFailure(
                            "execution_failed", "repeated paired observation was nondeterministic"
                        )
                else:
                    first_by_case[case.order] = observation
                observations.append(observation)
    except _ObservationFailure as exc:
        execution_status = "failed"
        execution_reason = exc.reason
        limitations.append(str(exc))
    except TimeoutError as exc:
        execution_status = "failed"
        execution_reason = "timeout"
        limitations.append(str(exc))
    except BaseException:
        execution_status = "failed"
        execution_reason = "execution_failed"
        limitations.append("A bounded worker operation raised an unexpected execution failure.")
    finally:
        cleanup_failed = False
        for worker, session in reversed(sessions):
            try:
                worker.close(session)
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            execution_status = "failed"
            execution_reason = "execution_failed"
            limitations.append("Worker cleanup failed after the observation attempt.")

    execution = MechanismExecutionV1(status=execution_status, reason=execution_reason)
    return _run(
        binding=binding,
        corpus=corpus,
        execution=execution,
        observations=tuple(observations),
        repetitions=repetitions,
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "MechanismObservationCorpusV1",
    "MechanismObservationRunV1",
    "MechanismPairedCaseV1",
    "MechanismPairedObservationV1",
    "MechanismWorkerPortV1",
    "MechanismWorkerRegistrationV1",
    "SyntheticFixtureWorkerV1",
    "build_mechanism_observation_corpus_v1",
    "collect_mechanism_observations_v1",
    "validate_mechanism_observation_corpus_v1",
]
