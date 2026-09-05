"""Pure, dependency-injected feedback-round state machine for PIT optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Literal, Protocol, runtime_checkable

from core.pit_optimizer_v5.artifacts import (
    PersistedRoleInvocationV5,
    RepositoryCheckpointV5,
)
from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    PreValidationInvalidExperimentIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    StructuralTemplateV5,
    derive_experiment_identity_v5,
    derive_pre_validation_invalid_experiment_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignEvidenceV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    CriticArtifactV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    InvestigatorArtifactV5,
    PanelEvaluationV5,
    ValidationResultV5,
    canonical_sha256_v5,
    selected_scenario,
    validate_campaign_evidence_v5,
    validate_campaign_manifest_bindings_v5,
)
from core.pit_optimizer_v5.memory import (
    ArchiveReducerV5,
    CleanupResultPayloadV5,
    CriticUnavailableAuthorityV5,
    EpisodeEvidencePayloadV5,
    ExperimentRecordV5,
    NoNovelHypothesisAuthorityV5,
    NoveltyExhaustedAuthorityV5,
    PreCriticEvidenceIdentityV5,
    QuickEvidencePayloadV5,
    RenderedVariantPayloadV5,
    ResourceLeasePayloadV5,
    RoleCompletionPayloadV5,
    RoundEventPayloadV5,
    RoundEventV5,
    RoundIntentPayloadV5,
    RoundOutcomePayloadV5,
    StoredExperimentRecordV5,
    is_testable_experiment_status_v5,
)
from core.pit_optimizer_v5.probes import (
    BEHAVIORAL_EQUIVALENT_ON_SUITE_V1,
    SemanticFingerprintV5,
    classify_semantic_fingerprints_v5,
)
from core.pit_optimizer_v5.provider import (
    ExistingPersistedRoleRequestV5,
    FreshPersistedRoleRequestV5,
    PersistedRoleRequestV5,
    RecoverableRoleInvokerV5,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    RoleNameV5,
    RoleReconciliationFailureV5,
    RoleRequestV5,
)
from core.pit_optimizer_v5.rendering import render_variants
from core.pit_optimizer_v5.search import (
    ArchiveRecordAuthorityV5,
    BaselineParentAuthorityV5,
    ParentCandidateV5,
    SearchStateV5,
    campaign_cagr_pct,
)
from core.pit_optimizer_v5.selection import (
    ScheduledHypothesisV5,
    canonicalize_discovery_evidence_v5,
    discovery_episode_execution_order_v5,
    record_novelty_attempt_v5,
    select_parent_v5,
    select_manifest_discovery_survivors_v5,
)
from core.pit_optimizer_v5.selection import QuickScreenCandidateV5


RuntimeStageV5 = Literal[
    "recovery",
    "parent_selection",
    "investigator",
    "novelty",
    "author",
    "rendering",
    "materialization",
    "validation",
    "semantic_probe",
    "quick_evaluation",
    "survivor_selection",
    "discovery_evaluation",
    "critic",
    "finalization",
    "checkpoint",
    "cleanup",
]
RuntimeFailureCodeV5 = Literal[
    "cancelled",
    "deadline_exceeded",
    "invalid_dependency_result",
    "stage_failed",
    "role_unrecoverable",
    "role_rejected",
    "no_testable_experiments",
    "foreign_lease",
    "cleanup_failed",
]
RuntimeStatusV5 = Literal[
    "completed",
    "no_novel_hypothesis",
    "novelty_exhausted",
    "critic_unavailable",
    "failed",
]
CandidateRuntimeStatusV5 = Literal[
    "invalid",
    "exact_duplicate",
    "behavioral_equivalent",
    "quick_ready",
    "quick_rejected",
    "zero_trade",
    "timed_out",
    "cancelled",
    "evaluation_failed",
    "evaluated",
]

_RUNTIME_STAGES = frozenset(
    {
        "recovery",
        "parent_selection",
        "investigator",
        "novelty",
        "author",
        "rendering",
        "materialization",
        "validation",
        "semantic_probe",
        "quick_evaluation",
        "survivor_selection",
        "discovery_evaluation",
        "critic",
        "finalization",
        "checkpoint",
        "cleanup",
    }
)
_FAILURE_CODES = frozenset(
    {
        "cancelled",
        "deadline_exceeded",
        "invalid_dependency_result",
        "stage_failed",
        "role_unrecoverable",
        "role_rejected",
        "no_testable_experiments",
        "foreign_lease",
        "cleanup_failed",
    }
)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class StageDeadlineV5:
    stage: RuntimeStageV5
    expires_at_monotonic: float

    def __post_init__(self) -> None:
        if self.stage not in _RUNTIME_STAGES:
            raise ValueError("runtime deadline stage is invalid")
        if type(self.expires_at_monotonic) is not float or not math.isfinite(self.expires_at_monotonic):
            raise ValueError("runtime deadline must be a finite monotonic value")


@dataclass(frozen=True, slots=True)
class RuntimeFailureV5:
    stage: RuntimeStageV5
    code: RuntimeFailureCodeV5
    role: RoleNameV5 | None = None
    experiment_id: str | None = None

    def __post_init__(self) -> None:
        if self.stage not in _RUNTIME_STAGES or self.code not in _FAILURE_CODES:
            raise ValueError("runtime failure is outside the closed V5 taxonomy")
        if self.role is not None and self.role not in {"investigator", "author", "critic"}:
            raise ValueError("runtime failure role is invalid")
        if self.experiment_id is not None:
            _digest(self.experiment_id, "runtime failure experiment")


@dataclass(frozen=True, slots=True)
class OwnedLeaseV5:
    payload: ResourceLeasePayloadV5
    round_index: int
    opaque_handle: object

    def __post_init__(self) -> None:
        if type(self.payload) is not ResourceLeasePayloadV5:
            raise ValueError("owned lease payload is invalid")
        _positive(self.round_index, "owned lease round")
        if self.opaque_handle is None:
            raise ValueError("owned lease requires an opaque handle")


@dataclass(frozen=True, slots=True)
class MaterializedVariantV5:
    variant: RenderedVariantV5
    source_bundle_ref: ArtifactRefV5
    leases: tuple[OwnedLeaseV5, ...]
    opaque_candidate: object

    def __post_init__(self) -> None:
        if type(self.variant) is not RenderedVariantV5 or type(self.source_bundle_ref) is not ArtifactRefV5:
            raise ValueError("materialized variant authority is invalid")
        if self.source_bundle_ref.sha256 != self.variant.source_bundle.sha256:
            raise ValueError("materialized source reference differs from rendered bytes")
        if type(self.leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in self.leases):
            raise ValueError("materialized variant leases are invalid")
        lease_ids = tuple(item.payload.lease_id for item in self.leases)
        if len(set(lease_ids)) != len(lease_ids):
            raise ValueError("materialized variant leases must be unique")
        if self.opaque_candidate is None:
            raise ValueError("materialized variant requires an opaque candidate handle")


ExperimentIdentityLikeV5 = ExperimentIdentityV5 | PreValidationInvalidExperimentIdentityV5


@dataclass(frozen=True, slots=True)
class CandidateEvidenceV5:
    experiment_identity: ExperimentIdentityLikeV5
    template: StructuralTemplateV5
    materialized: MaterializedVariantV5
    validation: ValidationResultV5
    semantic_fingerprint: SemanticFingerprintV5 | None
    quick_evidence: PanelEvaluationV5 | None
    discovery_episodes: tuple[EpisodeEvaluationV5, ...]
    campaign_evidence: CampaignEvidenceV5 | None
    status: CandidateRuntimeStatusV5
    failure_code: str | None
    artifact_refs: tuple[ArtifactRefV5, ...]

    def __post_init__(self) -> None:
        if type(self.experiment_identity) not in {
            ExperimentIdentityV5,
            PreValidationInvalidExperimentIdentityV5,
        }:
            raise ValueError("candidate evidence identity is invalid")
        if type(self.template) is not StructuralTemplateV5 or type(self.materialized) is not MaterializedVariantV5:
            raise ValueError("candidate evidence template or materialization is invalid")
        if type(self.validation) is not ValidationResultV5:
            raise ValueError("candidate evidence validation is invalid")
        if self.semantic_fingerprint is not None and type(self.semantic_fingerprint) is not SemanticFingerprintV5:
            raise ValueError("candidate semantic fingerprint is invalid")
        if self.quick_evidence is not None and type(self.quick_evidence) is not PanelEvaluationV5:
            raise ValueError("candidate quick evidence is invalid")
        if type(self.discovery_episodes) is not tuple or any(
            type(item) is not EpisodeEvaluationV5 for item in self.discovery_episodes
        ):
            raise ValueError("candidate discovery evidence is invalid")
        if self.campaign_evidence is not None and type(self.campaign_evidence) is not CampaignEvidenceV5:
            raise ValueError("candidate campaign evidence is invalid")
        if self.status not in {
            "invalid",
            "exact_duplicate",
            "behavioral_equivalent",
            "quick_ready",
            "quick_rejected",
            "zero_trade",
            "timed_out",
            "cancelled",
            "evaluation_failed",
            "evaluated",
        }:
            raise ValueError("candidate evidence status is invalid")
        if self.failure_code is not None and (type(self.failure_code) is not str or not self.failure_code):
            raise ValueError("candidate failure code is invalid")
        if type(self.artifact_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.artifact_refs):
            raise ValueError("candidate artifact references are invalid")
        if len(set(self.artifact_refs)) != len(self.artifact_refs):
            raise ValueError("candidate artifact references must be unique")

    @property
    def experiment_id(self) -> str:
        return self.experiment_identity.sha256


@dataclass(frozen=True, slots=True)
class SearchProjectionV5:
    checkpoint: RepositoryCheckpointV5 | None
    state: SearchStateV5
    stored_records: tuple[StoredExperimentRecordV5, ...]

    def __post_init__(self) -> None:
        if self.checkpoint is not None and type(self.checkpoint) is not RepositoryCheckpointV5:
            raise ValueError("search projection checkpoint is invalid")
        if type(self.state) is not SearchStateV5:
            raise ValueError("search projection state is invalid")
        if type(self.stored_records) is not tuple or any(
            type(item) is not StoredExperimentRecordV5 for item in self.stored_records
        ):
            raise ValueError("search projection records are invalid")
        references = tuple(item.reference for item in self.stored_records)
        if self.checkpoint is None:
            if references:
                raise ValueError("uncheckpointed search projection cannot promote records")
        elif references != self.checkpoint.record_refs:
            raise ValueError("search projection records differ from checkpoint authority")


@dataclass(frozen=True, slots=True)
class FeedbackRoundInputV5:
    manifest: CampaignManifestV5
    panel_plan: CampaignPanelPlanV5
    evaluator_contract: EvaluatorContractV5
    baseline: BaselineParentAuthorityV5
    round_index: int
    owner_token_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.manifest) is not CampaignManifestV5
            or type(self.panel_plan) is not CampaignPanelPlanV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.baseline) is not BaselineParentAuthorityV5
        ):
            raise ValueError("feedback-round authority is invalid")
        _positive(self.round_index, "feedback round")
        _digest(self.owner_token_sha256, "feedback-round owner token")
        if self.round_index > self.manifest.search.max_feedback_rounds:
            raise ValueError("feedback round exceeds manifest capacity")
        validate_campaign_manifest_bindings_v5(
            self.manifest,
            panel_plan=self.panel_plan,
            evaluator_contract=self.evaluator_contract,
        )

    @property
    def campaign_id(self) -> str:
        return self.manifest.campaign_id


@dataclass(frozen=True, slots=True)
class FeedbackRoundResultV5:
    status: RuntimeStatusV5
    campaign_id: str
    round_index: int
    parent: ParentCandidateV5 | None
    terminal_outcome: RoundOutcomePayloadV5 | None
    record_refs: tuple[ArtifactRefV5, ...]
    checkpoint: RepositoryCheckpointV5 | None
    cleanup: CleanupResultPayloadV5 | None
    failure: RuntimeFailureV5 | None

    def __post_init__(self) -> None:
        if self.status not in {
            "completed",
            "no_novel_hypothesis",
            "novelty_exhausted",
            "critic_unavailable",
            "failed",
        }:
            raise ValueError("feedback-round result status is invalid")
        if type(self.campaign_id) is not str or not self.campaign_id:
            raise ValueError("feedback-round result campaign is invalid")
        _positive(self.round_index, "feedback-round result round")
        if self.parent is not None and type(self.parent) is not ParentCandidateV5:
            raise ValueError("feedback-round result parent is invalid")
        if self.terminal_outcome is not None and type(self.terminal_outcome) is not RoundOutcomePayloadV5:
            raise ValueError("feedback-round terminal outcome is invalid")
        if type(self.record_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.record_refs):
            raise ValueError("feedback-round record references are invalid")
        if self.checkpoint is not None and type(self.checkpoint) is not RepositoryCheckpointV5:
            raise ValueError("feedback-round checkpoint is invalid")
        if self.cleanup is not None and type(self.cleanup) is not CleanupResultPayloadV5:
            raise ValueError("feedback-round cleanup result is invalid")
        if self.failure is not None and type(self.failure) is not RuntimeFailureV5:
            raise ValueError("feedback-round failure is invalid")
        if self.status == "failed":
            if self.failure is None or self.terminal_outcome is not None:
                raise ValueError("failed feedback round requires only a typed failure")
        elif self.failure is not None:
            raise ValueError("successful or terminal feedback round cannot carry a failure")
        if self.status in {"no_novel_hypothesis", "novelty_exhausted", "critic_unavailable"}:
            if self.terminal_outcome is None or self.terminal_outcome.outcome != self.status:
                raise ValueError("terminal feedback round differs from its durable outcome")
            if self.record_refs or self.checkpoint is not None:
                raise ValueError("terminal non-promoting feedback round cannot carry promotion authority")
        elif self.terminal_outcome is not None:
            raise ValueError("nonterminal feedback round cannot carry a terminal outcome")
        if self.status == "completed" and self.checkpoint is None:
            raise ValueError("completed feedback round requires its checkpoint")


NoveltyResolutionV5 = ScheduledHypothesisV5 | NoNovelHypothesisAuthorityV5 | NoveltyExhaustedAuthorityV5


@runtime_checkable
class RoundPersistenceV5(Protocol):
    def recover_projection(
        self, reducer: ArchiveReducerV5[SearchStateV5]
    ) -> tuple[RepositoryCheckpointV5 | None, SearchStateV5]: ...

    def load_experiment(self, reference: ArtifactRefV5) -> ExperimentRecordV5: ...

    def load_round_events(self, *, campaign_id: str, round_index: int) -> tuple[RoundEventV5, ...]: ...

    def load_round_payload(self, reference: ArtifactRefV5, *, expected_kind: str) -> RoundEventPayloadV5: ...

    def append_role_request(self, *, call: RoleCallKeyV5, request: RoleRequestV5) -> PersistedRoleRequestV5: ...

    def load_role_invocation(self, payload: RoleCompletionPayloadV5) -> RoleInvocationPackageV5: ...

    def persist_role_invocation(
        self,
        *,
        call: RoleCallKeyV5,
        request_ref: ArtifactRefV5,
        package: RoleInvocationPackageV5,
    ) -> PersistedRoleInvocationV5: ...

    def append_input(self, *, artifact_kind: str, value: object) -> ArtifactRefV5: ...

    def append_round_payload(self, payload: RoundEventPayloadV5) -> ArtifactRefV5: ...

    def append_round_event(self, event: RoundEventV5) -> ArtifactRefV5: ...

    def append_critic(self, artifact: CriticArtifactV5) -> ArtifactRefV5: ...

    def append_experiment(self, record: ExperimentRecordV5) -> ArtifactRefV5: ...

    def publish_projection(
        self,
        *,
        record_refs: tuple[ArtifactRefV5, ...],
        reducer: ArchiveReducerV5[SearchStateV5],
        generation: int,
    ) -> RepositoryCheckpointV5: ...


@runtime_checkable
class RuntimeClockV5(Protocol):
    def monotonic(self) -> float: ...


@runtime_checkable
class RuntimeCancellationV5(Protocol):
    def is_cancelled(self) -> bool: ...


@runtime_checkable
class RoleRequestFactoryV5(Protocol):
    def investigator_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
    ) -> RoleRequestV5: ...

    def author_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        decision: ScheduledHypothesisV5,
        investigator: InvestigatorArtifactV5,
    ) -> RoleRequestV5: ...

    def critic_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        decision: ScheduledHypothesisV5,
        candidates: tuple[CandidateEvidenceV5, ...],
    ) -> RoleRequestV5: ...


@runtime_checkable
class NoveltyResolverV5(Protocol):
    def resolve(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
        request: RoleRequestV5,
        package: RoleInvocationPackageV5,
        artifact: InvestigatorArtifactV5,
    ) -> NoveltyResolutionV5: ...


@runtime_checkable
class CandidateRuntimeV5(Protocol):
    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5: ...

    def materialize(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5: ...

    def recover_materialized(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        leases: tuple[OwnedLeaseV5, ...],
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5: ...

    def validate(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> ValidationResultV5: ...

    def fingerprint(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> SemanticFingerprintV5: ...

    def evaluate_quick(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> PanelEvaluationV5: ...

    def evaluate_episode(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
    ) -> EpisodeEvaluationV5: ...


@runtime_checkable
class ExperimentRecordFactoryV5(Protocol):
    def build_records(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        parent: ParentCandidateV5,
        decision: ScheduledHypothesisV5,
        template: StructuralTemplateV5,
        candidates: tuple[CandidateEvidenceV5, ...],
        critic: CriticArtifactV5,
        critic_ref: ArtifactRefV5,
    ) -> tuple[ExperimentRecordV5, ...]: ...


@runtime_checkable
class ArchiveReducerFactoryV5(Protocol):
    def recovery_reducer(self, inputs: FeedbackRoundInputV5) -> ArchiveReducerV5[SearchStateV5]: ...

    def publication_reducer(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        prior: SearchProjectionV5,
        records: tuple[StoredExperimentRecordV5, ...],
        source_authorities: tuple[ArchiveRecordAuthorityV5, ...],
    ) -> ArchiveReducerV5[SearchStateV5]: ...


@runtime_checkable
class OwnedCleanupV5(Protocol):
    def recover_lease(
        self,
        payload: ResourceLeasePayloadV5,
        *,
        round_index: int,
    ) -> OwnedLeaseV5: ...

    def cleanup(
        self,
        leases: tuple[OwnedLeaseV5, ...],
        *,
        deadline: StageDeadlineV5,
    ) -> CleanupResultPayloadV5: ...


@dataclass(frozen=True, slots=True)
class FeedbackRoundDependenciesV5:
    persistence: RoundPersistenceV5
    invoker: RecoverableRoleInvokerV5
    requests: RoleRequestFactoryV5
    novelty: NoveltyResolverV5
    candidates: CandidateRuntimeV5
    records: ExperimentRecordFactoryV5
    archive_reducers: ArchiveReducerFactoryV5
    clock: RuntimeClockV5
    cancellation: RuntimeCancellationV5
    cleanup: OwnedCleanupV5

    def __post_init__(self) -> None:
        protocols = (
            (self.persistence, RoundPersistenceV5),
            (self.invoker, RecoverableRoleInvokerV5),
            (self.requests, RoleRequestFactoryV5),
            (self.novelty, NoveltyResolverV5),
            (self.candidates, CandidateRuntimeV5),
            (self.records, ExperimentRecordFactoryV5),
            (self.archive_reducers, ArchiveReducerFactoryV5),
            (self.clock, RuntimeClockV5),
            (self.cancellation, RuntimeCancellationV5),
            (self.cleanup, OwnedCleanupV5),
        )
        if any(not isinstance(value, protocol) for value, protocol in protocols):
            raise ValueError("feedback-round dependency does not implement its V5 protocol")


class _RuntimeAbort(RuntimeError):
    def __init__(self, failure: RuntimeFailureV5) -> None:
        super().__init__(failure.code)
        self.failure = failure


class _Journal:
    def __init__(self, inputs: FeedbackRoundInputV5, persistence: RoundPersistenceV5) -> None:
        self._inputs = inputs
        self._persistence = persistence
        self.events = list(
            persistence.load_round_events(
                campaign_id=inputs.campaign_id,
                round_index=inputs.round_index,
            )
        )
        self.payloads = [
            persistence.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in self.events
        ]

    def role_completion(self, role: RoleNameV5) -> RoleCompletionPayloadV5 | None:
        matches = tuple(
            payload
            for payload in self.payloads
            if isinstance(payload, RoleCompletionPayloadV5) and payload.role == role
        )
        if len(matches) > 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", role=role))
        return None if not matches else matches[0]

    def find_experiment_payloads(self, experiment_id: str) -> tuple[RoundEventPayloadV5, ...]:
        result: list[RoundEventPayloadV5] = []
        for event, payload in zip(self.events, self.payloads, strict=True):
            if event.experiment_id == experiment_id:
                result.append(payload)
        return tuple(result)

    def payload_reference(self, payload: RoundEventPayloadV5) -> ArtifactRefV5:
        matches = tuple(
            event.payload_ref for event, item in zip(self.events, self.payloads, strict=True) if item == payload
        )
        if len(matches) != 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return matches[0]

    def lease_payloads(self) -> tuple[ResourceLeasePayloadV5, ...]:
        return tuple(payload for payload in self.payloads if isinstance(payload, ResourceLeasePayloadV5))

    def cleanup_payload(self) -> CleanupResultPayloadV5 | None:
        matches = tuple(payload for payload in self.payloads if isinstance(payload, CleanupResultPayloadV5))
        if len(matches) > 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return None if not matches else matches[0]

    def terminal_payload(self) -> RoundOutcomePayloadV5 | None:
        matches = tuple(payload for payload in self.payloads if isinstance(payload, RoundOutcomePayloadV5))
        if len(matches) > 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return None if not matches else matches[0]

    def append(self, payload: RoundEventPayloadV5, *, experiment_id: str | None = None) -> RoundEventV5:
        payload_ref = self._persistence.append_round_payload(payload)
        prior_sha256 = None if not self.events else self.events[-1].sha256
        event = RoundEventV5(
            campaign_id=self._inputs.campaign_id,
            round_index=self._inputs.round_index,
            sequence=len(self.events),
            prior_event_sha256=prior_sha256,
            event_kind={
                RoleCompletionPayloadV5: "role_completion",
                RoundIntentPayloadV5: "round_intent",
                RenderedVariantPayloadV5: "rendered_variant",
                QuickEvidencePayloadV5: "quick_evidence",
                EpisodeEvidencePayloadV5: "episode_evidence",
                ResourceLeasePayloadV5: "resource_lease",
                CleanupResultPayloadV5: "cleanup_result",
                RoundOutcomePayloadV5: "round_outcome",
            }[type(payload)],
            experiment_id=experiment_id,
            payload_ref=payload_ref,
        )
        self._persistence.append_round_event(event)
        self.events.append(event)
        self.payloads.append(payload)
        return event


class _Runtime:
    def __init__(self, inputs: FeedbackRoundInputV5, dependencies: FeedbackRoundDependenciesV5) -> None:
        self.inputs = inputs
        self.dependencies = dependencies
        started = dependencies.clock.monotonic()
        if type(started) is not float or not math.isfinite(started):
            raise ValueError("runtime clock returned a non-finite monotonic value")
        self.round_deadline = started + inputs.manifest.resources.round_wall_timeout_seconds
        self.journal = _Journal(inputs, dependencies.persistence)
        self.parent: ParentCandidateV5 | None = None
        self.projection: SearchProjectionV5 | None = None
        self.record_refs: tuple[ArtifactRefV5, ...] = ()
        self.checkpoint: RepositoryCheckpointV5 | None = None
        self._owned_leases: dict[str, OwnedLeaseV5] = {}

    def _deadline(self, stage: RuntimeStageV5, seconds: int) -> StageDeadlineV5:
        if self.dependencies.cancellation.is_cancelled():
            raise _RuntimeAbort(RuntimeFailureV5(stage, "cancelled"))
        now = self.dependencies.clock.monotonic()
        if type(now) is not float or not math.isfinite(now):
            raise _RuntimeAbort(RuntimeFailureV5(stage, "invalid_dependency_result"))
        expires = min(self.round_deadline, now + seconds)
        if now >= expires:
            raise _RuntimeAbort(RuntimeFailureV5(stage, "deadline_exceeded"))
        return StageDeadlineV5(stage=stage, expires_at_monotonic=float(expires))

    def _check_finished(self, deadline: StageDeadlineV5, *, experiment_id: str | None = None) -> None:
        now = self.dependencies.clock.monotonic()
        if type(now) is not float or not math.isfinite(now):
            raise _RuntimeAbort(
                RuntimeFailureV5(deadline.stage, "invalid_dependency_result", experiment_id=experiment_id)
            )
        if now >= deadline.expires_at_monotonic:
            raise _RuntimeAbort(RuntimeFailureV5(deadline.stage, "deadline_exceeded", experiment_id=experiment_id))

    def _validate_role_request(
        self,
        request: RoleRequestV5,
        *,
        role: RoleNameV5,
        parent: ParentCandidateV5,
        hypothesis_id: str | None,
        experiment_ids: tuple[str, ...],
    ) -> None:
        if type(request) is not RoleRequestV5 or request.role != role:
            raise _RuntimeAbort(RuntimeFailureV5(role, "invalid_dependency_result", role=role))  # type: ignore[arg-type]
        binding = request.expected_binding
        if (
            binding.parent_revision_sha256 != parent.policy_identity_sha256
            or binding.hypothesis_id != hypothesis_id
            or binding.experiment_ids != experiment_ids
            or binding.discovery_plan_sha256 != self.inputs.panel_plan.discovery_plan_sha256
        ):
            raise _RuntimeAbort(RuntimeFailureV5(role, "invalid_dependency_result", role=role))  # type: ignore[arg-type]

    def _complete_role(
        self,
        *,
        request: RoleRequestV5,
        role: RoleNameV5,
        parent: ParentCandidateV5,
        hypothesis_id: str | None,
        experiment_ids: tuple[str, ...],
    ) -> RoleInvocationPackageV5:
        self._validate_role_request(
            request,
            role=role,
            parent=parent,
            hypothesis_id=hypothesis_id,
            experiment_ids=experiment_ids,
        )
        deadline = self._deadline(role, self.inputs.manifest.resources.role_call_timeout_seconds)  # type: ignore[arg-type]
        call = RoleCallKeyV5(
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            role=role,
            role_position={"investigator": 1, "author": 2, "critic": 3}[role],
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=request.sha256,
        )
        completed = self.journal.role_completion(role)
        if completed is not None:
            if completed.call_key_sha256 != call.sha256 or completed.request_sha256 != request.sha256:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", role=role))
            package = self.dependencies.persistence.load_role_invocation(completed)
            if package.call != call or package.request != request:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", role=role))
            return package

        persisted = self.dependencies.persistence.append_role_request(call=call, request=request)
        try:
            if type(persisted) is FreshPersistedRoleRequestV5:
                package = self.dependencies.invoker.invoke_once(persisted)
            elif type(persisted) is ExistingPersistedRoleRequestV5:
                reconciled = self.dependencies.invoker.reconcile_once(persisted)
                if type(reconciled) is RoleReconciliationFailureV5:
                    raise _RuntimeAbort(RuntimeFailureV5(role, "role_unrecoverable", role=role))  # type: ignore[arg-type]
                package = reconciled
            else:
                raise _RuntimeAbort(RuntimeFailureV5(role, "invalid_dependency_result", role=role))  # type: ignore[arg-type]
        except _RuntimeAbort:
            raise
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5(role, "role_unrecoverable", role=role)) from None  # type: ignore[arg-type]
        if type(package) is not RoleInvocationPackageV5 or package.call != call or package.request != request:
            raise _RuntimeAbort(RuntimeFailureV5(role, "invalid_dependency_result", role=role))  # type: ignore[arg-type]
        persisted_invocation = self.dependencies.persistence.persist_role_invocation(
            call=call,
            request_ref=persisted.reference,
            package=package,
        )
        self.journal.append(persisted_invocation.payload)
        self._check_finished(deadline)
        return package

    def _recover_projection(self) -> SearchProjectionV5:
        deadline = self._deadline("recovery", self.inputs.manifest.resources.round_wall_timeout_seconds)
        reducer = self.dependencies.archive_reducers.recovery_reducer(self.inputs)
        checkpoint, state = self.dependencies.persistence.recover_projection(reducer)
        if type(state) is not SearchStateV5 or state.archive.capacity != self.inputs.manifest.search.archive_capacity:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        refs = () if checkpoint is None else checkpoint.record_refs
        stored = tuple(
            StoredExperimentRecordV5(
                reference=reference,
                record=self.dependencies.persistence.load_experiment(reference),
            )
            for reference in refs
        )
        projection = SearchProjectionV5(checkpoint=checkpoint, state=state, stored_records=stored)
        self._check_finished(deadline)
        self.projection = projection
        return projection

    def _recover_owned_leases(self) -> tuple[OwnedLeaseV5, ...]:
        if self._owned_leases:
            return tuple(self._owned_leases[key] for key in sorted(self._owned_leases))
        for payload in self.journal.lease_payloads():
            if (
                payload.owner_campaign_id != self.inputs.campaign_id
                or payload.owner_token_sha256 != self.inputs.owner_token_sha256
            ):
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "foreign_lease"))
            try:
                lease = self.dependencies.cleanup.recover_lease(payload, round_index=self.inputs.round_index)
            except BaseException:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "foreign_lease")) from None
            if (
                type(lease) is not OwnedLeaseV5
                or lease.payload != payload
                or lease.round_index != self.inputs.round_index
            ):
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "foreign_lease"))
            if payload.lease_id in self._owned_leases:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
            self._owned_leases[payload.lease_id] = lease
        return tuple(self._owned_leases[key] for key in sorted(self._owned_leases))

    def _register_leases(self, leases: tuple[OwnedLeaseV5, ...]) -> None:
        if type(leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in leases):
            raise _RuntimeAbort(RuntimeFailureV5("materialization", "invalid_dependency_result"))
        for lease in leases:
            payload = lease.payload
            if (
                lease.round_index != self.inputs.round_index
                or payload.owner_campaign_id != self.inputs.campaign_id
                or payload.owner_token_sha256 != self.inputs.owner_token_sha256
            ):
                raise _RuntimeAbort(RuntimeFailureV5("materialization", "foreign_lease"))
            prior = self._owned_leases.get(payload.lease_id)
            if prior is not None:
                if prior.payload != payload:
                    raise _RuntimeAbort(RuntimeFailureV5("materialization", "foreign_lease"))
                continue
            if any(item.lease_id == payload.lease_id for item in self.journal.lease_payloads()):
                raise _RuntimeAbort(RuntimeFailureV5("materialization", "invalid_dependency_result"))
            self.journal.append(payload)
            self._owned_leases[payload.lease_id] = lease

    def _cleanup(self) -> CleanupResultPayloadV5:
        prior = self.journal.cleanup_payload()
        if prior is not None:
            return prior
        leases = self._recover_owned_leases()
        now = self.dependencies.clock.monotonic()
        if type(now) is not float or not math.isfinite(now):
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "invalid_dependency_result"))
        deadline = StageDeadlineV5(
            stage="cleanup",
            expires_at_monotonic=float(now + self.inputs.manifest.resources.cleanup_timeout_seconds),
        )
        try:
            result = self.dependencies.cleanup.cleanup(leases, deadline=deadline)
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "cleanup_failed")) from None
        expected = {
            "workspace": sum(item.payload.resource_kind == "workspace" for item in leases),
            "policy_worker": sum(item.payload.resource_kind == "policy_worker" for item in leases),
            "evaluator_process": sum(item.payload.resource_kind == "evaluator_process" for item in leases),
            "container": sum(item.payload.resource_kind == "container" for item in leases),
        }
        if type(result) is not CleanupResultPayloadV5 or (
            result.owned_workspaces,
            result.owned_policy_workers,
            result.owned_evaluators,
            result.owned_containers,
        ) != (
            expected["workspace"],
            expected["policy_worker"],
            expected["evaluator_process"],
            expected["container"],
        ):
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "invalid_dependency_result"))
        self.journal.append(result)
        if not result.cleanup_complete:
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "cleanup_failed"))
        return result

    def _terminal_result(
        self,
        authority: NoNovelHypothesisAuthorityV5 | NoveltyExhaustedAuthorityV5 | CriticUnavailableAuthorityV5,
    ) -> FeedbackRoundResultV5:
        cleanup = self._cleanup()
        payload = RoundOutcomePayloadV5(
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            authority=authority,
        )
        prior = self.journal.terminal_payload()
        if prior is None:
            self.journal.append(payload)
        elif prior != payload:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return FeedbackRoundResultV5(
            status=payload.outcome,
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            parent=self.parent,
            terminal_outcome=payload,
            record_refs=(),
            checkpoint=None,
            cleanup=cleanup,
            failure=None,
        )

    def _validate_terminal_novelty(
        self,
        authority: NoNovelHypothesisAuthorityV5 | NoveltyExhaustedAuthorityV5,
        *,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
        request: RoleRequestV5,
        package: RoleInvocationPackageV5,
        artifact: InvestigatorArtifactV5,
    ) -> None:
        state_sha256 = canonical_sha256_v5(projection.state.to_primitive())
        matches = (authority,) if type(authority) is NoNovelHypothesisAuthorityV5 else authority.parent_outcomes
        current = tuple(item for item in matches if item.parent_revision_sha256 == parent.policy_identity_sha256)
        if (
            authority.discovery_plan_sha256 != self.inputs.panel_plan.discovery_plan_sha256
            or authority.search_state_before_sha256 != state_sha256
            or len(current) != 1
            or current[0].investigator_request_sha256 != request.sha256
            or current[0].investigator_evidence_sha256 != request.role_evidence.sha256
            or current[0].investigator_attempt_sha256s != (package.attempt.sha256,)
            or current[0].investigator_artifact_sha256 != canonical_sha256_v5(artifact)
        ):
            raise _RuntimeAbort(RuntimeFailureV5("novelty", "invalid_dependency_result"))

    def _existing_payload(
        self,
        experiment_id: str,
        payload_type: type[RenderedVariantPayloadV5] | type[QuickEvidencePayloadV5] | type[EpisodeEvidencePayloadV5],
        *,
        episode_ordinal: int | None = None,
    ) -> RoundEventPayloadV5 | None:
        matches = tuple(
            item
            for item in self.journal.find_experiment_payloads(experiment_id)
            if type(item) is payload_type
            and (
                episode_ordinal is None
                or (type(item) is EpisodeEvidencePayloadV5 and item.episode.episode_ordinal == episode_ordinal)
            )
        )
        if len(matches) > 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=experiment_id))
        return None if not matches else matches[0]

    def _validate_quick(self, evaluation: PanelEvaluationV5, identity: ExperimentIdentityV5) -> None:
        plan = self.inputs.panel_plan.quick
        if (
            type(evaluation) is not PanelEvaluationV5
            or evaluation.evaluator_contract_sha256 != self.inputs.evaluator_contract.sha256
            or evaluation.sandbox_profile_sha256 != self.inputs.evaluator_contract.sandbox_profile_sha256
            or evaluation.panel_sha256 != plan.panel_ref.sha256
            or evaluation.policy_identity_sha256 != identity.policy_revision_sha256
            or evaluation.start_date != plan.start_date
            or evaluation.end_date != plan.end_date
            or evaluation.selection_scenario_id != "base"
            or tuple(item.scenario_id for item in evaluation.scenarios) != ("base",)
        ):
            raise ValueError("quick evidence authority mismatch")

    def _validate_episode(
        self,
        episode: EpisodeEvaluationV5,
        plan: EpisodePlanV5,
        identity: ExperimentIdentityV5,
    ) -> None:
        evaluation = episode.evaluation
        expected_scenarios = tuple(item.scenario_id for item in self.inputs.evaluator_contract.friction_grid)
        if (
            type(episode) is not EpisodeEvaluationV5
            or episode.episode_id != plan.episode_id
            or episode.episode_ordinal != plan.episode_ordinal
            or episode.start_date != plan.start_date
            or episode.end_date != plan.end_date
            or evaluation.panel_sha256 != plan.panel_ref.sha256
            or evaluation.evaluator_contract_sha256 != self.inputs.evaluator_contract.sha256
            or evaluation.sandbox_profile_sha256 != self.inputs.evaluator_contract.sandbox_profile_sha256
            or evaluation.policy_identity_sha256 != identity.policy_revision_sha256
            or evaluation.selection_scenario_id != self.inputs.evaluator_contract.selection_scenario_id
            or tuple(item.scenario_id for item in evaluation.scenarios) != expected_scenarios
        ):
            raise ValueError("episode evidence authority mismatch")

    def _failed_candidate(
        self,
        candidate: CandidateEvidenceV5,
        *,
        code: str,
        stage: RuntimeStageV5,
    ) -> CandidateEvidenceV5:
        reference = self.dependencies.persistence.append_input(
            artifact_kind="typed_failure",
            value={"code": code, "stage": stage, "experiment_id": candidate.experiment_id},
        )
        return replace(
            candidate,
            status="evaluation_failed",
            failure_code=code,
            artifact_refs=(*candidate.artifact_refs, reference),
        )

    def _materialized_variant(
        self,
        *,
        variant: RenderedVariantV5,
        identity: ExperimentIdentityV5,
        existed: bool,
    ) -> MaterializedVariantV5:
        deadline = self._deadline(
            "materialization",
            self.inputs.manifest.resources.worker_startup_timeout_seconds,
        )
        try:
            if existed:
                materialized = self.dependencies.candidates.recover_materialized(
                    inputs=self.inputs,
                    experiment_identity=identity,
                    variant=variant,
                    leases=self._recover_owned_leases(),
                    deadline=deadline,
                )
            else:
                materialized = self.dependencies.candidates.materialize(
                    inputs=self.inputs,
                    experiment_identity=identity,
                    variant=variant,
                    deadline=deadline,
                )
        except _RuntimeAbort:
            raise
        except BaseException:
            raise _RuntimeAbort(
                RuntimeFailureV5("materialization", "stage_failed", experiment_id=identity.sha256)
            ) from None
        if type(materialized) is not MaterializedVariantV5 or materialized.variant != variant:
            raise _RuntimeAbort(
                RuntimeFailureV5("materialization", "invalid_dependency_result", experiment_id=identity.sha256)
            )
        self._register_leases(materialized.leases)
        self._check_finished(deadline, experiment_id=identity.sha256)
        return materialized

    def _initial_candidate(
        self,
        *,
        template: StructuralTemplateV5,
        variant: RenderedVariantV5,
        parent: ParentCandidateV5,
        decision: ScheduledHypothesisV5,
    ) -> CandidateEvidenceV5:
        identity = derive_experiment_identity_v5(
            policy_revision=variant.policy_revision,
            parent_revision_sha256=parent.policy_identity_sha256,
            hypothesis=decision.hypothesis,
            template=template,
            assignment=variant.assignment,
            round_index=self.inputs.round_index,
            discovery_plan_sha256=self.inputs.panel_plan.discovery_plan_sha256,
        )
        existing_render = self._existing_payload(identity.sha256, RenderedVariantPayloadV5)
        if existing_render is not None:
            if type(existing_render) is not RenderedVariantPayloadV5 or existing_render.variant != variant:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            render_ref = self.journal.payload_reference(existing_render)
            existed = True
        else:
            event = self.journal.append(RenderedVariantPayloadV5(variant=variant), experiment_id=identity.sha256)
            render_ref = event.payload_ref
            existed = False

        materialized = self._materialized_variant(variant=variant, identity=identity, existed=existed)
        deadline = self._deadline("validation", self.inputs.manifest.resources.mechanics_timeout_seconds)
        try:
            validation = self.dependencies.candidates.validate(materialized, deadline=deadline)
        except BaseException:
            validation = ValidationResultV5(valid=False, failure_code="validation_execution_failed", changed_symbols=())
        if type(validation) is not ValidationResultV5:
            raise _RuntimeAbort(
                RuntimeFailureV5("validation", "invalid_dependency_result", experiment_id=identity.sha256)
            )
        validation_ref = self.dependencies.persistence.append_input(
            artifact_kind="validation",
            value=validation,
        )
        references = tuple(
            sorted(
                {render_ref, materialized.source_bundle_ref, validation_ref},
                key=lambda item: (item.relative_path, item.sha256),
            )
        )
        if not validation.valid:
            invalid_identity = derive_pre_validation_invalid_experiment_identity_v5(
                parent_revision_sha256=parent.policy_identity_sha256,
                hypothesis=decision.hypothesis,
                template=template,
                assignment=variant.assignment,
                round_index=self.inputs.round_index,
                discovery_plan_sha256=self.inputs.panel_plan.discovery_plan_sha256,
            )
            return CandidateEvidenceV5(
                experiment_identity=invalid_identity,
                template=template,
                materialized=materialized,
                validation=validation,
                semantic_fingerprint=None,
                quick_evidence=None,
                discovery_episodes=(),
                campaign_evidence=None,
                status="invalid",
                failure_code=validation.failure_code,
                artifact_refs=references,
            )
        if validation.changed_symbols != template.changed_symbols:
            raise _RuntimeAbort(
                RuntimeFailureV5("validation", "invalid_dependency_result", experiment_id=identity.sha256)
            )
        candidate = CandidateEvidenceV5(
            experiment_identity=identity,
            template=template,
            materialized=materialized,
            validation=validation,
            semantic_fingerprint=None,
            quick_evidence=None,
            discovery_episodes=(),
            campaign_evidence=None,
            status="exact_duplicate"
            if identity.policy_revision_sha256 == parent.policy_identity_sha256
            else "quick_ready",
            failure_code=None,
            artifact_refs=references,
        )
        if candidate.status == "exact_duplicate":
            return candidate

        existing_quick = self._existing_payload(identity.sha256, QuickEvidencePayloadV5)
        if existing_quick is not None:
            if type(existing_quick) is not QuickEvidencePayloadV5 or existing_quick.experiment_id != identity.sha256:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            try:
                self._validate_quick(existing_quick.evaluation, identity)
            except ValueError:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                ) from None
            if existing_quick.semantic_fingerprint.fingerprint_sha256 == parent.semantic_fingerprint_sha256:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            return replace(
                candidate,
                semantic_fingerprint=existing_quick.semantic_fingerprint,
                quick_evidence=existing_quick.evaluation,
                artifact_refs=tuple(
                    sorted(
                        {*candidate.artifact_refs, self.journal.payload_reference(existing_quick)},
                        key=lambda item: (item.relative_path, item.sha256),
                    )
                ),
            )

        probe_deadline = self._deadline("semantic_probe", self.inputs.manifest.resources.mechanics_timeout_seconds)
        try:
            fingerprint = self.dependencies.candidates.fingerprint(materialized, deadline=probe_deadline)
            if type(fingerprint) is not SemanticFingerprintV5:
                raise TypeError
            comparison = classify_semantic_fingerprints_v5(
                self.inputs.baseline.semantic_fingerprint
                if parent.origin == "baseline"
                else self._parent_fingerprint(parent, materialized, probe_deadline),
                fingerprint,
            )
            self._check_finished(probe_deadline, experiment_id=identity.sha256)
        except _RuntimeAbort:
            raise
        except BaseException:
            return self._failed_candidate(candidate, code="semantic_probe_failed", stage="semantic_probe")
        if comparison.classification == BEHAVIORAL_EQUIVALENT_ON_SUITE_V1:
            fingerprint_ref = self.dependencies.persistence.append_input(
                artifact_kind="semantic_fingerprint",
                value=fingerprint,
            )
            return replace(
                candidate,
                semantic_fingerprint=fingerprint,
                status="behavioral_equivalent",
                artifact_refs=tuple(
                    sorted(
                        {*candidate.artifact_refs, fingerprint_ref},
                        key=lambda item: (item.relative_path, item.sha256),
                    )
                ),
            )

        quick_deadline = self._deadline("quick_evaluation", self.inputs.manifest.resources.quick_timeout_seconds)
        try:
            quick = self.dependencies.candidates.evaluate_quick(materialized, deadline=quick_deadline)
            self._validate_quick(quick, identity)
            self._check_finished(quick_deadline, experiment_id=identity.sha256)
        except _RuntimeAbort:
            raise
        except BaseException:
            fingerprint_ref = self.dependencies.persistence.append_input(
                artifact_kind="semantic_fingerprint",
                value=fingerprint,
            )
            return self._failed_candidate(
                replace(
                    candidate,
                    semantic_fingerprint=fingerprint,
                    artifact_refs=tuple(
                        sorted(
                            {*candidate.artifact_refs, fingerprint_ref},
                            key=lambda item: (item.relative_path, item.sha256),
                        )
                    ),
                ),
                code="quick_evaluation_failed",
                stage="quick_evaluation",
            )
        quick_event = self.journal.append(
            QuickEvidencePayloadV5(
                experiment_id=identity.sha256,
                semantic_fingerprint=fingerprint,
                evaluation=quick,
            ),
            experiment_id=identity.sha256,
        )
        return replace(
            candidate,
            semantic_fingerprint=fingerprint,
            quick_evidence=quick,
            artifact_refs=tuple(
                sorted(
                    {*candidate.artifact_refs, quick_event.payload_ref},
                    key=lambda item: (item.relative_path, item.sha256),
                )
            ),
        )

    def _parent_fingerprint(
        self,
        parent: ParentCandidateV5,
        materialized: MaterializedVariantV5,
        deadline: StageDeadlineV5,
    ) -> SemanticFingerprintV5:
        if self.projection is None or parent.experiment_record_ref is None:
            raise ValueError("archive parent fingerprint authority is unavailable")
        record = next(
            (item.record for item in self.projection.stored_records if item.reference == parent.experiment_record_ref),
            None,
        )
        if record is None or record.semantic_fingerprint is None:
            raise ValueError("archive parent fingerprint authority is incomplete")
        if record.semantic_fingerprint.fingerprint_sha256 != parent.semantic_fingerprint_sha256:
            raise ValueError("archive parent fingerprint authority differs")
        del materialized, deadline
        return record.semantic_fingerprint

    def _render_and_quick_screen(
        self,
        *,
        parent: ParentCandidateV5,
        decision: ScheduledHypothesisV5,
        template: StructuralTemplateV5,
    ) -> tuple[CandidateEvidenceV5, ...]:
        deadline = self._deadline("rendering", self.inputs.manifest.resources.round_wall_timeout_seconds)
        parent_source = self.dependencies.candidates.load_parent_source(parent)
        if type(parent_source) is not SourceBundleV5 or parent_source.sha256 != parent.source_bundle_ref.sha256:
            raise _RuntimeAbort(RuntimeFailureV5("rendering", "invalid_dependency_result"))
        try:
            variants = render_variants(
                parent=parent_source,
                parent_revision=parent.policy_revision,
                template=template,
                maximum=self.inputs.manifest.search.max_variants_per_template,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("rendering", "stage_failed")) from None
        if not variants or len(variants) > self.inputs.manifest.search.max_variants_per_template:
            raise _RuntimeAbort(RuntimeFailureV5("rendering", "invalid_dependency_result"))
        self._check_finished(deadline)
        candidates = tuple(
            self._initial_candidate(
                template=template,
                variant=variant,
                parent=parent,
                decision=decision,
            )
            for variant in variants
        )
        quick_candidates = tuple(
            QuickScreenCandidateV5(
                template=template,
                variant=item.materialized.variant,
                validation=item.validation,
                semantic_fingerprint=item.semantic_fingerprint,
                parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
                quick_evidence=item.quick_evidence,
            )
            for item in candidates
            if item.status == "quick_ready"
        )
        deadline = self._deadline("survivor_selection", self.inputs.manifest.resources.round_wall_timeout_seconds)
        try:
            survivors = select_manifest_discovery_survivors_v5(
                candidates=quick_candidates,
                capabilities=self.inputs.manifest.search,
                panel_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("survivor_selection", "invalid_dependency_result")) from None
        self._check_finished(deadline)
        survivor_revisions = {item.policy_identity_sha256 for item in survivors}
        return tuple(
            item
            if item.status != "quick_ready" or item.materialized.variant.policy_revision.sha256 in survivor_revisions
            else replace(item, status="quick_rejected")
            for item in candidates
        )

    def _evaluate_discovery(
        self,
        candidates: tuple[CandidateEvidenceV5, ...],
    ) -> tuple[CandidateEvidenceV5, ...]:
        episode_order = discovery_episode_execution_order_v5(
            discovery_plan=self.inputs.panel_plan,
            round_index=self.inputs.round_index,
        )
        result: list[CandidateEvidenceV5] = []
        for candidate in candidates:
            if candidate.status != "quick_ready":
                result.append(candidate)
                continue
            identity = candidate.experiment_identity
            assert type(identity) is ExperimentIdentityV5
            episodes: list[EpisodeEvaluationV5] = []
            references = set(candidate.artifact_refs)
            failed = False
            for plan in episode_order:
                existing = self._existing_payload(
                    identity.sha256,
                    EpisodeEvidencePayloadV5,
                    episode_ordinal=plan.episode_ordinal,
                )
                if existing is not None:
                    assert type(existing) is EpisodeEvidencePayloadV5
                    try:
                        self._validate_episode(existing.episode, plan, identity)
                    except ValueError:
                        raise _RuntimeAbort(
                            RuntimeFailureV5(
                                "recovery",
                                "invalid_dependency_result",
                                experiment_id=identity.sha256,
                            )
                        ) from None
                    episode = existing.episode
                    references.add(self.journal.payload_reference(existing))
                else:
                    deadline = self._deadline(
                        "discovery_evaluation",
                        self.inputs.manifest.resources.discovery_episode_timeout_seconds,
                    )
                    try:
                        episode = self.dependencies.candidates.evaluate_episode(
                            candidate.materialized,
                            plan,
                            deadline=deadline,
                        )
                        self._validate_episode(episode, plan, identity)
                        self._check_finished(deadline, experiment_id=identity.sha256)
                    except _RuntimeAbort:
                        raise
                    except BaseException:
                        failed = True
                        break
                    event = self.journal.append(
                        EpisodeEvidencePayloadV5(experiment_id=identity.sha256, episode=episode),
                        experiment_id=identity.sha256,
                    )
                    references.add(event.payload_ref)
                episodes.append(episode)
            if failed:
                partial = replace(
                    candidate,
                    discovery_episodes=tuple(sorted(episodes, key=lambda item: item.episode_ordinal)),
                    artifact_refs=tuple(sorted(references, key=lambda item: (item.relative_path, item.sha256))),
                )
                result.append(
                    self._failed_candidate(
                        partial,
                        code="discovery_evaluation_failed",
                        stage="discovery_evaluation",
                    )
                )
                continue
            canonical = canonicalize_discovery_evidence_v5(tuple(episodes))
            score = campaign_cagr_pct(
                episodes=canonical,
                discovery_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
            )
            campaign = CampaignEvidenceV5(
                discovery_plan_sha256=self.inputs.panel_plan.discovery_plan_sha256,
                episodes=canonical,
                campaign_cagr_pct=score,
                closed_trades=sum(selected_scenario(item.evaluation).report.closed_trades for item in canonical),
            )
            validate_campaign_evidence_v5(
                campaign,
                panel_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
                policy_identity_sha256=identity.policy_revision_sha256,
            )
            result.append(
                replace(
                    candidate,
                    discovery_episodes=canonical,
                    campaign_evidence=campaign,
                    status="zero_trade" if campaign.closed_trades == 0 else "evaluated",
                    artifact_refs=tuple(sorted(references, key=lambda item: (item.relative_path, item.sha256))),
                )
            )
        return tuple(result)

    def _precritic_evidence(
        self,
        candidates: tuple[CandidateEvidenceV5, ...],
    ) -> tuple[PreCriticEvidenceIdentityV5, ...]:
        evidence: list[PreCriticEvidenceIdentityV5] = []
        for candidate in candidates:
            experiment_id = candidate.experiment_id
            evidence.append(
                PreCriticEvidenceIdentityV5(
                    experiment_id=experiment_id,
                    evidence_kind="rendered_variant",
                    evidence_sha256=candidate.materialized.variant.sha256,
                )
            )
            evidence.append(
                PreCriticEvidenceIdentityV5(
                    experiment_id=experiment_id,
                    evidence_kind="validation",
                    evidence_sha256=canonical_sha256_v5(candidate.validation),
                )
            )
            if candidate.semantic_fingerprint is not None:
                evidence.append(
                    PreCriticEvidenceIdentityV5(
                        experiment_id=experiment_id,
                        evidence_kind="semantic_fingerprint",
                        evidence_sha256=candidate.semantic_fingerprint.fingerprint_sha256,
                    )
                )
            if candidate.quick_evidence is not None:
                evidence.append(
                    PreCriticEvidenceIdentityV5(
                        experiment_id=experiment_id,
                        evidence_kind="quick_evaluation",
                        evidence_sha256=candidate.quick_evidence.sha256,
                    )
                )
            evidence.extend(
                PreCriticEvidenceIdentityV5(
                    experiment_id=experiment_id,
                    evidence_kind="episode_evaluation",
                    evidence_sha256=canonical_sha256_v5(episode),
                    episode_ordinal=episode.episode_ordinal,
                )
                for episode in candidate.discovery_episodes
            )
            if candidate.failure_code is not None:
                evidence.append(
                    PreCriticEvidenceIdentityV5(
                        experiment_id=experiment_id,
                        evidence_kind="typed_failure",
                        evidence_sha256=canonical_sha256_v5(
                            {"code": candidate.failure_code, "experiment_id": experiment_id}
                        ),
                    )
                )
        return tuple(evidence)

    def _round_intent(self, payload: RoundIntentPayloadV5) -> None:
        matches = tuple(item for item in self.journal.payloads if type(item) is RoundIntentPayloadV5)
        if len(matches) > 1 or (matches and matches[0] != payload):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        if not matches:
            self.journal.append(payload)

    def _publish(
        self,
        *,
        projection: SearchProjectionV5,
        candidates: tuple[CandidateEvidenceV5, ...],
        records: tuple[ExperimentRecordV5, ...],
    ) -> tuple[tuple[ArtifactRefV5, ...], RepositoryCheckpointV5]:
        refs = tuple(self.dependencies.persistence.append_experiment(record) for record in records)
        stored = tuple(
            StoredExperimentRecordV5(reference=reference, record=record)
            for reference, record in zip(refs, records, strict=True)
        )
        candidates_by_id = {item.experiment_id: item for item in candidates}
        authorities = tuple(
            sorted(
                (
                    ArchiveRecordAuthorityV5(
                        experiment_id=item.record.experiment_id,
                        experiment_record_ref=item.reference,
                        source_bundle=candidates_by_id[item.record.experiment_id].materialized.variant.source_bundle,
                        source_bundle_ref=candidates_by_id[item.record.experiment_id].materialized.source_bundle_ref,
                    )
                    for item in stored
                    if item.record.status in {"evaluated", "zero_trade"}
                ),
                key=lambda item: item.experiment_id,
            )
        )
        reducer = self.dependencies.archive_reducers.publication_reducer(
            inputs=self.inputs,
            prior=projection,
            records=stored,
            source_authorities=authorities,
        )
        all_refs = tuple(
            sorted(
                {*(projection.checkpoint.record_refs if projection.checkpoint is not None else ()), *refs},
                key=lambda item: (item.relative_path, item.sha256),
            )
        )
        deadline = self._deadline("checkpoint", self.inputs.manifest.resources.round_wall_timeout_seconds)
        checkpoint = self.dependencies.persistence.publish_projection(
            record_refs=all_refs,
            reducer=reducer,
            generation=1 if projection.checkpoint is None else projection.checkpoint.generation + 1,
        )
        if type(checkpoint) is not RepositoryCheckpointV5 or checkpoint.record_refs != all_refs:
            raise _RuntimeAbort(RuntimeFailureV5("checkpoint", "invalid_dependency_result"))
        self._check_finished(deadline)
        self.record_refs = refs
        self.checkpoint = checkpoint
        return refs, checkpoint

    def run(self) -> FeedbackRoundResultV5:
        projection = self._recover_projection()
        self._recover_owned_leases()

        terminal = self.journal.terminal_payload()
        if terminal is not None:
            try:
                self.parent = select_parent_v5(
                    state=projection.state,
                    baseline=self.inputs.baseline,
                    discovery_plan=self.inputs.panel_plan,
                    evaluator_contract=self.inputs.evaluator_contract,
                    stored_records=projection.stored_records,
                )
            except BaseException:
                self.parent = None
            return self._terminal_result(terminal.authority)

        if projection.state.next_round_index == self.inputs.round_index + 1:
            if projection.checkpoint is None:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
            current = tuple(
                item.reference
                for item in projection.stored_records
                if item.record.round_index == self.inputs.round_index
            )
            if not current:
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
            self.record_refs = current
            self.checkpoint = projection.checkpoint
            cleanup = self._cleanup()
            return FeedbackRoundResultV5(
                status="completed",
                campaign_id=self.inputs.campaign_id,
                round_index=self.inputs.round_index,
                parent=None,
                terminal_outcome=None,
                record_refs=current,
                checkpoint=projection.checkpoint,
                cleanup=cleanup,
                failure=None,
            )
        if projection.state.next_round_index != self.inputs.round_index:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))

        deadline = self._deadline("parent_selection", self.inputs.manifest.resources.round_wall_timeout_seconds)
        try:
            parent = select_parent_v5(
                state=projection.state,
                baseline=self.inputs.baseline,
                discovery_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
                stored_records=projection.stored_records,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("parent_selection", "stage_failed")) from None
        self.parent = parent
        self._check_finished(deadline)

        investigator_request = self.dependencies.requests.investigator_request(self.inputs, projection, parent)
        investigator_package = self._complete_role(
            request=investigator_request,
            role="investigator",
            parent=parent,
            hypothesis_id=None,
            experiment_ids=(),
        )
        if not investigator_package.accepted or type(investigator_package.artifact) is not InvestigatorArtifactV5:
            raise _RuntimeAbort(RuntimeFailureV5("investigator", "role_rejected", role="investigator"))
        investigator = investigator_package.artifact

        deadline = self._deadline("novelty", self.inputs.manifest.resources.round_wall_timeout_seconds)
        try:
            novelty = self.dependencies.novelty.resolve(
                inputs=self.inputs,
                projection=projection,
                parent=parent,
                request=investigator_request,
                package=investigator_package,
                artifact=investigator,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("novelty", "stage_failed")) from None
        self._check_finished(deadline)
        if type(novelty) in {NoNovelHypothesisAuthorityV5, NoveltyExhaustedAuthorityV5}:
            assert type(novelty) in {NoNovelHypothesisAuthorityV5, NoveltyExhaustedAuthorityV5}
            self._validate_terminal_novelty(
                novelty,
                projection=projection,
                parent=parent,
                request=investigator_request,
                package=investigator_package,
                artifact=investigator,
            )
            return self._terminal_result(novelty)
        if type(novelty) is not ScheduledHypothesisV5 or novelty.parent != parent:
            raise _RuntimeAbort(RuntimeFailureV5("novelty", "invalid_dependency_result"))
        decision = novelty
        state_after_novelty = record_novelty_attempt_v5(projection.state, decision)
        self._round_intent(
            RoundIntentPayloadV5(
                parent_revision_sha256=parent.policy_identity_sha256,
                parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
                hypothesis=decision.hypothesis,
                discovery_plan_sha256=self.inputs.panel_plan.discovery_plan_sha256,
            )
        )

        author_request = self.dependencies.requests.author_request(
            self.inputs,
            projection,
            decision,
            investigator,
        )
        author_package = self._complete_role(
            request=author_request,
            role="author",
            parent=parent,
            hypothesis_id=decision.hypothesis.hypothesis_id,
            experiment_ids=(),
        )
        if not author_package.accepted or type(author_package.artifact) is not StructuralTemplateV5:
            raise _RuntimeAbort(RuntimeFailureV5("author", "role_rejected", role="author"))
        template = author_package.artifact
        if (
            template.parent_revision_sha256 != parent.policy_identity_sha256
            or template.hypothesis_id != decision.hypothesis.hypothesis_id
            or len(template.axes) > self.inputs.manifest.search.max_tunable_axes
            or (template.full_source_escape is not None and not self.inputs.manifest.search.allow_full_source_escape)
        ):
            raise _RuntimeAbort(RuntimeFailureV5("author", "invalid_dependency_result", role="author"))

        candidates = self._render_and_quick_screen(parent=parent, decision=decision, template=template)
        candidates = self._evaluate_discovery(candidates)
        if any(item.status == "quick_ready" for item in candidates):
            raise _RuntimeAbort(RuntimeFailureV5("discovery_evaluation", "invalid_dependency_result"))
        critic_candidates = tuple(
            item
            for item in candidates
            if item.status != "quick_ready" and is_testable_experiment_status_v5(item.status)
        )
        if not critic_candidates:
            raise _RuntimeAbort(RuntimeFailureV5("critic", "no_testable_experiments", role="critic"))
        experiment_ids = tuple(item.experiment_id for item in critic_candidates)
        critic_request = self.dependencies.requests.critic_request(
            self.inputs,
            projection,
            decision,
            critic_candidates,
        )
        critic_package = self._complete_role(
            request=critic_request,
            role="critic",
            parent=parent,
            hypothesis_id=decision.hypothesis.hypothesis_id,
            experiment_ids=experiment_ids,
        )
        if not critic_package.accepted:
            authority = CriticUnavailableAuthorityV5(
                outcome="critic_unavailable",
                discovery_plan_sha256=self.inputs.panel_plan.discovery_plan_sha256,
                search_state_before_sha256=canonical_sha256_v5(projection.state.to_primitive()),
                search_state_after_sha256=canonical_sha256_v5(state_after_novelty.to_primitive()),
                parent_revision_sha256=parent.policy_identity_sha256,
                hypothesis_id=decision.hypothesis.hypothesis_id,
                critic_request_sha256=critic_request.sha256,
                critic_evidence_sha256=critic_request.role_evidence.sha256,
                critic_attempt_sha256s=(critic_package.attempt.sha256,),
                experiment_ids=experiment_ids,
                precritic_evidence=self._precritic_evidence(critic_candidates),
            )
            return self._terminal_result(authority)
        if type(critic_package.artifact) is not CriticArtifactV5:
            raise _RuntimeAbort(RuntimeFailureV5("critic", "invalid_dependency_result", role="critic"))
        critic = critic_package.artifact
        if tuple(item.experiment_id for item in critic.reviews) != experiment_ids:
            raise _RuntimeAbort(RuntimeFailureV5("critic", "invalid_dependency_result", role="critic"))
        critic_ref = self.dependencies.persistence.append_critic(critic)

        deadline = self._deadline("finalization", self.inputs.manifest.resources.round_wall_timeout_seconds)
        records = self.dependencies.records.build_records(
            inputs=self.inputs,
            parent=parent,
            decision=decision,
            template=template,
            candidates=candidates,
            critic=critic,
            critic_ref=critic_ref,
        )
        if (
            type(records) is not tuple
            or any(type(item) is not ExperimentRecordV5 for item in records)
            or tuple(item.experiment_id for item in records) != tuple(item.experiment_id for item in candidates)
            or any(
                item.round_index != self.inputs.round_index
                or item.parent_revision_sha256 != parent.policy_identity_sha256
                or item.hypothesis != decision.hypothesis
                or item.template != template
                for item in records
            )
        ):
            raise _RuntimeAbort(RuntimeFailureV5("finalization", "invalid_dependency_result"))
        self._check_finished(deadline)
        refs, checkpoint = self._publish(
            projection=projection,
            candidates=candidates,
            records=records,
        )
        cleanup = self._cleanup()
        return FeedbackRoundResultV5(
            status="completed",
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            parent=parent,
            terminal_outcome=None,
            record_refs=refs,
            checkpoint=checkpoint,
            cleanup=cleanup,
            failure=None,
        )


def run_feedback_round_v5(
    inputs: FeedbackRoundInputV5,
    dependencies: FeedbackRoundDependenciesV5,
) -> FeedbackRoundResultV5:
    """Execute or resume one feedback round without concrete external adapters."""

    if type(inputs) is not FeedbackRoundInputV5 or type(dependencies) is not FeedbackRoundDependenciesV5:
        raise ValueError("feedback-round entry requires exact V5 input and dependencies")
    runtime = _Runtime(inputs, dependencies)

    def failed(failure: RuntimeFailureV5) -> FeedbackRoundResultV5:
        cleanup = runtime.journal.cleanup_payload()
        if cleanup is None and (runtime.journal.lease_payloads() or runtime.checkpoint is not None):
            try:
                cleanup = runtime._cleanup()
            except _RuntimeAbort as cleanup_abort:
                failure = cleanup_abort.failure
        return FeedbackRoundResultV5(
            status="failed",
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
            parent=runtime.parent,
            terminal_outcome=None,
            record_refs=runtime.record_refs,
            checkpoint=runtime.checkpoint,
            cleanup=cleanup,
            failure=failure,
        )

    try:
        return runtime.run()
    except _RuntimeAbort as abort:
        return failed(abort.failure)
    except BaseException:
        return failed(RuntimeFailureV5("recovery", "stage_failed"))


__all__ = [
    "ArchiveReducerFactoryV5",
    "CandidateEvidenceV5",
    "CandidateRuntimeV5",
    "ExperimentRecordFactoryV5",
    "FeedbackRoundDependenciesV5",
    "FeedbackRoundInputV5",
    "FeedbackRoundResultV5",
    "MaterializedVariantV5",
    "NoveltyResolutionV5",
    "NoveltyResolverV5",
    "OwnedCleanupV5",
    "OwnedLeaseV5",
    "RoleRequestFactoryV5",
    "RoundPersistenceV5",
    "RuntimeCancellationV5",
    "RuntimeClockV5",
    "RuntimeFailureCodeV5",
    "RuntimeFailureV5",
    "RuntimeStageV5",
    "RuntimeStatusV5",
    "SearchProjectionV5",
    "StageDeadlineV5",
    "run_feedback_round_v5",
]
