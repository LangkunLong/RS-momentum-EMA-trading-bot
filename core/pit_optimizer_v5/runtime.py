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
    CandidateExecutionAuthorityV5,
    CandidateExecutionKeyV5,
    CandidateExecutionStageV5,
    CandidateStageFailureCodeV5,
    CandidateStageOutcomeV5,
    CandidateStageResultPayloadV5,
    CandidateStageV5,
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
    RuntimeFailureAuthorityV5,
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
    expected_target_gap_pct_v5,
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
    cleanup_failure: RuntimeFailureV5 | None = None

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
        if self.cleanup_failure is not None and (
            type(self.cleanup_failure) is not RuntimeFailureV5
            or self.cleanup_failure.stage != "cleanup"
            or self.cleanup_failure.code not in {"cleanup_failed", "deadline_exceeded", "invalid_dependency_result"}
        ):
            raise ValueError("feedback-round cleanup failure is invalid")
        if self.status == "failed":
            if self.failure is None:
                raise ValueError("failed feedback round requires its typed primary failure")
            if self.terminal_outcome is not None and (
                type(self.terminal_outcome.authority) is not RuntimeFailureAuthorityV5
                or self.terminal_outcome.authority.stage != self.failure.stage
                or self.terminal_outcome.authority.failure_code != self.failure.code
                or self.terminal_outcome.authority.role != self.failure.role
                or self.terminal_outcome.authority.experiment_id != self.failure.experiment_id
            ):
                raise ValueError("failed feedback round differs from its durable primary failure")
        elif self.failure is not None:
            raise ValueError("successful or terminal feedback round cannot carry a failure")
        if self.status in {"no_novel_hypothesis", "novelty_exhausted", "critic_unavailable"}:
            if self.terminal_outcome is None or self.terminal_outcome.outcome != self.status:
                raise ValueError("terminal feedback round differs from its durable outcome")
            if self.record_refs or self.checkpoint is not None:
                raise ValueError("terminal non-promoting feedback round cannot carry promotion authority")
        elif self.status != "failed" and self.terminal_outcome is not None:
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
class CandidateExecutionPersistenceV5(Protocol):
    """Atomic side journal for exact evaluator ownership and crash recovery."""

    def load_candidate_executions(
        self, *, campaign_id: str, round_index: int
    ) -> tuple[CandidateExecutionAuthorityV5, ...]: ...

    def load_candidate_execution(
        self,
        *,
        campaign_id: str,
        round_index: int,
        key: CandidateExecutionKeyV5,
    ) -> tuple[CandidateExecutionAuthorityV5, ArtifactRefV5] | None: ...

    def append_candidate_execution(self, authority: CandidateExecutionAuthorityV5) -> ArtifactRefV5: ...


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


class CandidateExecutionRegistrarV5(Protocol):
    def __call__(
        self,
        authority: CandidateExecutionAuthorityV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> None: ...


@runtime_checkable
class LeaseAwareCandidateRuntimeV5(Protocol):
    """Candidate evaluator that durably registers owned leases before evidence use."""

    def evaluate_quick_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        register_execution: CandidateExecutionRegistrarV5,
    ) -> PanelEvaluationV5: ...

    def recover_quick_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> PanelEvaluationV5: ...

    def evaluate_episode_registered(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        register_execution: CandidateExecutionRegistrarV5,
    ) -> EpisodeEvaluationV5: ...

    def recover_episode_registered(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
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
        return None if not matches else matches[-1]

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
        if any(item.cleanup_complete for item in matches[:-1]):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return None if not matches else matches[-1]

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
                CandidateStageResultPayloadV5: "candidate_stage_result",
                QuickEvidencePayloadV5: "quick_evidence",
                EpisodeEvidencePayloadV5: "episode_evidence",
                CandidateExecutionAuthorityV5: "candidate_execution",
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
        self.projection = projection
        if not self._adopt_completed_projection(projection):
            self._deadline("recovery", self.inputs.manifest.resources.round_wall_timeout_seconds)
        return projection

    def _adopt_completed_projection(self, projection: SearchProjectionV5) -> bool:
        if projection.state.next_round_index != self.inputs.round_index + 1:
            return False
        checkpoint = projection.checkpoint
        if type(checkpoint) is not RepositoryCheckpointV5:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        completed_rounds = tuple(sorted({item.record.round_index for item in projection.stored_records}))
        current = tuple(
            item.reference for item in projection.stored_records if item.record.round_index == self.inputs.round_index
        )
        if (
            not current
            or not completed_rounds
            or completed_rounds[-1] != self.inputs.round_index
            or checkpoint.generation != len(completed_rounds)
            or checkpoint.record_refs != tuple(item.reference for item in projection.stored_records)
        ):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        self.record_refs = current
        self.checkpoint = checkpoint
        return True

    def _recover_owned_leases(self) -> tuple[OwnedLeaseV5, ...]:
        execution_payloads = tuple(
            payload for authority in self._execution_authorities() for payload in authority.lease_payloads
        )
        for payload in (*self.journal.lease_payloads(), *execution_payloads):
            if (
                payload.owner_campaign_id != self.inputs.campaign_id
                or payload.owner_token_sha256 != self.inputs.owner_token_sha256
            ):
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "foreign_lease"))
            prior = self._owned_leases.get(payload.lease_id)
            if prior is not None:
                if prior.payload != payload:
                    raise _RuntimeAbort(RuntimeFailureV5("recovery", "foreign_lease"))
                continue
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
            self._owned_leases[payload.lease_id] = lease
        return tuple(self._owned_leases[key] for key in sorted(self._owned_leases))

    def _execution_authorities(self) -> tuple[CandidateExecutionAuthorityV5, ...]:
        persistence = self.dependencies.persistence
        if not isinstance(persistence, CandidateExecutionPersistenceV5):
            if isinstance(self.dependencies.candidates, LeaseAwareCandidateRuntimeV5):
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
            return ()
        try:
            authorities = persistence.load_candidate_executions(
                campaign_id=self.inputs.campaign_id,
                round_index=self.inputs.round_index,
            )
        except _RuntimeAbort:
            raise
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "stage_failed")) from None
        if type(authorities) is not tuple or any(
            type(item) is not CandidateExecutionAuthorityV5
            or item.campaign_id != self.inputs.campaign_id
            or item.round_index != self.inputs.round_index
            or item.owner_token_sha256 != self.inputs.owner_token_sha256
            for item in authorities
        ):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        keys = tuple(item.key for item in authorities)
        lease_ids = tuple(payload.lease_id for item in authorities for payload in item.lease_payloads)
        if len(set(keys)) != len(keys) or len(set(lease_ids)) != len(lease_ids):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return authorities

    def _existing_execution(self, key: CandidateExecutionKeyV5) -> CandidateExecutionAuthorityV5 | None:
        if type(key) is not CandidateExecutionKeyV5:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        persistence = self.dependencies.persistence
        if not isinstance(persistence, CandidateExecutionPersistenceV5):
            if isinstance(self.dependencies.candidates, LeaseAwareCandidateRuntimeV5):
                raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
            return None
        try:
            match = persistence.load_candidate_execution(
                campaign_id=self.inputs.campaign_id,
                round_index=self.inputs.round_index,
                key=key,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "stage_failed")) from None
        if match is None:
            return None
        if (
            type(match) is not tuple
            or len(match) != 2
            or type(match[0]) is not CandidateExecutionAuthorityV5
            or type(match[1]) is not ArtifactRefV5
            or match[0].key != key
            or match[0].campaign_id != self.inputs.campaign_id
            or match[0].round_index != self.inputs.round_index
            or match[0].owner_token_sha256 != self.inputs.owner_token_sha256
        ):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        return match[0]

    def _register_execution(
        self,
        authority: CandidateExecutionAuthorityV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> None:
        if (
            type(authority) is not CandidateExecutionAuthorityV5
            or type(leases) is not tuple
            or tuple(item.payload for item in leases) != authority.lease_payloads
            or any(type(item) is not OwnedLeaseV5 for item in leases)
        ):
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        if (
            authority.campaign_id != self.inputs.campaign_id
            or authority.round_index != self.inputs.round_index
            or authority.owner_token_sha256 != self.inputs.owner_token_sha256
            or any(item.round_index != self.inputs.round_index for item in leases)
        ):
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "foreign_lease",
                    experiment_id=authority.key.experiment_id,
                )
            )
        existing_authorities = self._execution_authorities()
        if any(item.key == authority.key for item in existing_authorities):
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "invalid_dependency_result",
                    experiment_id=authority.key.experiment_id,
                )
            )
        durable_ids = {payload.lease_id for item in existing_authorities for payload in item.lease_payloads} | {
            item.lease_id for item in self.journal.lease_payloads()
        }
        for lease in leases:
            if lease.payload.lease_id in self._owned_leases or lease.payload.lease_id in durable_ids:
                raise _RuntimeAbort(
                    RuntimeFailureV5(
                        authority.key.stage,
                        "foreign_lease",
                        experiment_id=authority.key.experiment_id,
                    )
                )
        self._owned_leases.update({item.payload.lease_id: item for item in leases})
        persistence = self.dependencies.persistence
        if not isinstance(persistence, CandidateExecutionPersistenceV5):
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "invalid_dependency_result",
                    experiment_id=authority.key.experiment_id,
                )
            )
        try:
            reference = persistence.append_candidate_execution(authority)
        except _RuntimeAbort:
            raise
        except BaseException:
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "stage_failed",
                    experiment_id=authority.key.experiment_id,
                )
            ) from None
        try:
            refreshed = _Journal(self.inputs, self.dependencies.persistence)
        except _RuntimeAbort:
            raise
        except BaseException:
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "stage_failed",
                    experiment_id=authority.key.experiment_id,
                )
            ) from None
        self.journal = refreshed
        if type(reference) is not ArtifactRefV5:
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "invalid_dependency_result",
                    experiment_id=authority.key.experiment_id,
                )
            )
        try:
            reloaded = persistence.load_candidate_execution(
                campaign_id=self.inputs.campaign_id,
                round_index=self.inputs.round_index,
                key=authority.key,
            )
        except BaseException:
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "stage_failed",
                    experiment_id=authority.key.experiment_id,
                )
            ) from None
        if (
            type(reloaded) is not tuple
            or len(reloaded) != 2
            or type(reloaded[0]) is not CandidateExecutionAuthorityV5
            or type(reloaded[1]) is not ArtifactRefV5
            or reloaded != (authority, reference)
        ):
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "invalid_dependency_result",
                    experiment_id=authority.key.experiment_id,
                )
            )
        durable = tuple(
            payload
            for event, payload in zip(
                refreshed.events,
                refreshed.payloads,
                strict=True,
            )
            if payload == authority and event.payload_ref == reference
        )
        if durable != (authority,):
            raise _RuntimeAbort(
                RuntimeFailureV5(
                    authority.key.stage,
                    "invalid_dependency_result",
                    experiment_id=authority.key.experiment_id,
                )
            )

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
        if prior is not None and prior.cleanup_complete:
            return prior
        leases = self._recover_owned_leases()
        expected = {
            "workspace": sum(item.payload.resource_kind == "workspace" for item in leases),
            "policy_worker": sum(item.payload.resource_kind == "policy_worker" for item in leases),
            "evaluator_process": sum(item.payload.resource_kind == "evaluator_process" for item in leases),
            "container": sum(item.payload.resource_kind == "container" for item in leases),
        }
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
            self.journal.append(
                CleanupResultPayloadV5(
                    owned_workspaces=expected["workspace"],
                    owned_policy_workers=expected["policy_worker"],
                    owned_evaluators=expected["evaluator_process"],
                    owned_containers=expected["container"],
                    cleanup_complete=False,
                    failure_code="cleanup_execution_failed",
                )
            )
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "cleanup_failed")) from None
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
            self.journal.append(
                CleanupResultPayloadV5(
                    owned_workspaces=expected["workspace"],
                    owned_policy_workers=expected["policy_worker"],
                    owned_evaluators=expected["evaluator_process"],
                    owned_containers=expected["container"],
                    cleanup_complete=False,
                    failure_code="invalid_dependency_result",
                )
            )
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "invalid_dependency_result"))
        self.journal.append(result)
        self._check_finished(deadline)
        if not result.cleanup_complete:
            raise _RuntimeAbort(RuntimeFailureV5("cleanup", "cleanup_failed"))
        return result

    def _completed_result(self, *, parent: ParentCandidateV5 | None) -> FeedbackRoundResultV5:
        if type(self.checkpoint) is not RepositoryCheckpointV5 or not self.record_refs:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        cleanup_failure: RuntimeFailureV5 | None = None
        try:
            cleanup = self._cleanup()
        except _RuntimeAbort as abort:
            cleanup_failure = abort.failure
            cleanup = self.journal.cleanup_payload()
        return FeedbackRoundResultV5(
            status="completed",
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            parent=parent,
            terminal_outcome=None,
            record_refs=self.record_refs,
            checkpoint=self.checkpoint,
            cleanup=cleanup,
            failure=None,
            cleanup_failure=cleanup_failure,
        )

    def _terminal_result(
        self,
        authority: NoNovelHypothesisAuthorityV5 | NoveltyExhaustedAuthorityV5 | CriticUnavailableAuthorityV5,
    ) -> FeedbackRoundResultV5:
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
        cleanup_failure: RuntimeFailureV5 | None = None
        try:
            cleanup = self._cleanup()
        except _RuntimeAbort as abort:
            cleanup_failure = abort.failure
            cleanup = self.journal.cleanup_payload()
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
            cleanup_failure=cleanup_failure,
        )

    def _failure_result(self, failure: RuntimeFailureV5) -> FeedbackRoundResultV5:
        if self.checkpoint is not None:
            return self._completed_result(parent=self.parent)
        payload = RoundOutcomePayloadV5(
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            authority=RuntimeFailureAuthorityV5(
                outcome="runtime_failed",
                stage=failure.stage,
                failure_code=failure.code,
                role=failure.role,
                experiment_id=failure.experiment_id,
            ),
        )
        prior = self.journal.terminal_payload()
        if prior is None:
            self.journal.append(payload)
        elif type(prior.authority) is RuntimeFailureAuthorityV5:
            payload = prior
            failure = RuntimeFailureV5(
                stage=prior.authority.stage,
                code=prior.authority.failure_code,
                role=prior.authority.role,
                experiment_id=prior.authority.experiment_id,
            )
        else:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result"))
        cleanup_failure: RuntimeFailureV5 | None = None
        try:
            cleanup = self._cleanup()
        except _RuntimeAbort as abort:
            cleanup_failure = abort.failure
            cleanup = self.journal.cleanup_payload()
        return FeedbackRoundResultV5(
            status="failed",
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            parent=self.parent,
            terminal_outcome=payload,
            record_refs=self.record_refs,
            checkpoint=self.checkpoint,
            cleanup=cleanup,
            failure=failure,
            cleanup_failure=cleanup_failure,
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
        payload_type: (
            type[RenderedVariantPayloadV5]
            | type[CandidateStageResultPayloadV5]
            | type[QuickEvidencePayloadV5]
            | type[EpisodeEvidencePayloadV5]
        ),
        *,
        episode_ordinal: int | None = None,
        candidate_stage: CandidateStageV5 | None = None,
    ) -> RoundEventPayloadV5 | None:
        matches = tuple(
            item
            for item in self.journal.find_experiment_payloads(experiment_id)
            if type(item) is payload_type
            and (
                episode_ordinal is None
                or (type(item) is EpisodeEvidencePayloadV5 and item.episode.episode_ordinal == episode_ordinal)
            )
            and (
                candidate_stage is None
                or (type(item) is CandidateStageResultPayloadV5 and item.stage == candidate_stage)
            )
        )
        if len(matches) > 1:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=experiment_id))
        return None if not matches else matches[0]

    @staticmethod
    def _artifact_refs(*groups: tuple[ArtifactRefV5, ...]) -> tuple[ArtifactRefV5, ...]:
        return tuple(
            sorted(
                {reference for group in groups for reference in group},
                key=lambda item: (item.relative_path, item.sha256),
            )
        )

    def _candidate_stage(
        self,
        experiment_id: str,
        stage: CandidateStageV5,
    ) -> CandidateStageResultPayloadV5 | None:
        payload = self._existing_payload(
            experiment_id,
            CandidateStageResultPayloadV5,
            candidate_stage=stage,
        )
        if payload is not None and type(payload) is not CandidateStageResultPayloadV5:
            raise _RuntimeAbort(RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=experiment_id))
        return payload

    def _record_candidate_failure(
        self,
        candidate: CandidateEvidenceV5,
        *,
        stage: CandidateStageV5,
        outcome: CandidateStageOutcomeV5,
        code: CandidateStageFailureCodeV5,
        episode_ordinal: int | None = None,
    ) -> CandidateEvidenceV5:
        reference = self.dependencies.persistence.append_input(
            artifact_kind="typed_failure",
            value={"code": code, "stage": stage, "experiment_id": candidate.experiment_id},
        )
        payload = CandidateStageResultPayloadV5(
            experiment_id=candidate.experiment_id,
            stage=stage,
            stage_index={
                "validation": 1,
                "semantic_probe": 2,
                "quick_evaluation": 3,
                "discovery_evaluation": 4,
            }[stage],
            outcome=outcome,
            failure_code=code,
            failure_ref=reference,
            episode_ordinal=episode_ordinal,
        )
        event = self.journal.append(payload, experiment_id=candidate.experiment_id)
        return replace(
            candidate,
            status="evaluation_failed",
            failure_code=code,
            artifact_refs=self._artifact_refs(candidate.artifact_refs, (reference, event.payload_ref)),
        )

    def _recover_candidate_failure(
        self,
        candidate: CandidateEvidenceV5,
        payload: CandidateStageResultPayloadV5,
    ) -> CandidateEvidenceV5:
        if payload.failure_code is None or payload.failure_ref is None:
            raise _RuntimeAbort(
                RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=candidate.experiment_id)
            )
        return replace(
            candidate,
            status="evaluation_failed",
            failure_code=payload.failure_code,
            artifact_refs=self._artifact_refs(
                candidate.artifact_refs,
                (payload.failure_ref, self.journal.payload_reference(payload)),
            ),
        )

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
        validation_stage = self._candidate_stage(identity.sha256, "validation")
        if validation_stage is None:
            deadline = self._deadline("validation", self.inputs.manifest.resources.mechanics_timeout_seconds)
            failure_ref: ArtifactRefV5 | None = None
            try:
                validation = self.dependencies.candidates.validate(materialized, deadline=deadline)
                if type(validation) is not ValidationResultV5:
                    raise TypeError
                validation_outcome: CandidateStageOutcomeV5 = (
                    "validation_valid" if validation.valid else "validation_invalid"
                )
            except _RuntimeAbort:
                raise
            except BaseException:
                validation = ValidationResultV5(
                    valid=False,
                    failure_code="validation_execution_failed",
                    changed_symbols=(),
                )
                validation_outcome = "validation_failed"
                failure_ref = self.dependencies.persistence.append_input(
                    artifact_kind="typed_failure",
                    value={
                        "code": "validation_execution_failed",
                        "stage": "validation",
                        "experiment_id": identity.sha256,
                    },
                )
            validation_stage = CandidateStageResultPayloadV5(
                experiment_id=identity.sha256,
                stage="validation",
                stage_index=1,
                outcome=validation_outcome,
                validation=validation,
                failure_code=("validation_execution_failed" if failure_ref is not None else None),
                failure_ref=failure_ref,
            )
            validation_event = self.journal.append(validation_stage, experiment_id=identity.sha256)
            validation_stage_ref = validation_event.payload_ref
            self._check_finished(deadline, experiment_id=identity.sha256)
        else:
            assert validation_stage.validation is not None
            validation = validation_stage.validation
            validation_stage_ref = self.journal.payload_reference(validation_stage)
            failure_ref = validation_stage.failure_ref
        references = self._artifact_refs(
            (render_ref, materialized.source_bundle_ref, validation_stage_ref),
            (() if failure_ref is None else (failure_ref,)),
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
            semantic_stage = self._candidate_stage(identity.sha256, "semantic_probe")
            if semantic_stage is None:
                semantic_event = self.journal.append(
                    CandidateStageResultPayloadV5(
                        experiment_id=identity.sha256,
                        stage="semantic_probe",
                        stage_index=2,
                        outcome="exact_duplicate",
                    ),
                    experiment_id=identity.sha256,
                )
                semantic_ref = semantic_event.payload_ref
            elif semantic_stage.outcome == "exact_duplicate":
                semantic_ref = self.journal.payload_reference(semantic_stage)
            else:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            return replace(candidate, artifact_refs=self._artifact_refs(candidate.artifact_refs, (semantic_ref,)))

        semantic_stage = self._candidate_stage(identity.sha256, "semantic_probe")
        if semantic_stage is not None:
            semantic_ref = self.journal.payload_reference(semantic_stage)
            if semantic_stage.outcome == "semantic_probe_failed":
                return self._recover_candidate_failure(candidate, semantic_stage)
            if semantic_stage.outcome not in {"behavioral_equivalent", "behaviorally_distinct"}:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            assert semantic_stage.semantic_fingerprint is not None
            fingerprint = semantic_stage.semantic_fingerprint
            candidate = replace(
                candidate,
                semantic_fingerprint=fingerprint,
                status=(
                    "behavioral_equivalent" if semantic_stage.outcome == "behavioral_equivalent" else "quick_ready"
                ),
                artifact_refs=self._artifact_refs(candidate.artifact_refs, (semantic_ref,)),
            )
            if candidate.status == "behavioral_equivalent":
                return candidate
        else:
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
            except _RuntimeAbort:
                raise
            except BaseException:
                failed = self._record_candidate_failure(
                    candidate,
                    stage="semantic_probe",
                    outcome="semantic_probe_failed",
                    code="semantic_probe_failed",
                )
                self._check_finished(probe_deadline, experiment_id=identity.sha256)
                return failed
            semantic_outcome: CandidateStageOutcomeV5 = (
                "behavioral_equivalent"
                if comparison.classification == BEHAVIORAL_EQUIVALENT_ON_SUITE_V1
                else "behaviorally_distinct"
            )
            semantic_event = self.journal.append(
                CandidateStageResultPayloadV5(
                    experiment_id=identity.sha256,
                    stage="semantic_probe",
                    stage_index=2,
                    outcome=semantic_outcome,
                    semantic_fingerprint=fingerprint,
                ),
                experiment_id=identity.sha256,
            )
            candidate = replace(
                candidate,
                semantic_fingerprint=fingerprint,
                status=("behavioral_equivalent" if semantic_outcome == "behavioral_equivalent" else "quick_ready"),
                artifact_refs=self._artifact_refs(candidate.artifact_refs, (semantic_event.payload_ref,)),
            )
            self._check_finished(probe_deadline, experiment_id=identity.sha256)
            if candidate.status == "behavioral_equivalent":
                return candidate

        quick_failure = self._candidate_stage(identity.sha256, "quick_evaluation")
        if quick_failure is not None:
            if quick_failure.outcome != "quick_evaluation_failed":
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            return self._recover_candidate_failure(candidate, quick_failure)
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
            if existing_quick.semantic_fingerprint != candidate.semantic_fingerprint:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            return replace(
                candidate,
                quick_evidence=existing_quick.evaluation,
                artifact_refs=self._artifact_refs(
                    candidate.artifact_refs,
                    (self.journal.payload_reference(existing_quick),),
                ),
            )

        quick_deadline = self._deadline("quick_evaluation", self.inputs.manifest.resources.quick_timeout_seconds)
        try:
            if isinstance(self.dependencies.candidates, LeaseAwareCandidateRuntimeV5):
                execution_key = CandidateExecutionKeyV5(
                    identity.sha256,
                    "quick_evaluation",
                    None,
                )
                execution = self._existing_execution(execution_key)
                if execution is None:
                    quick = self.dependencies.candidates.evaluate_quick_registered(
                        materialized,
                        deadline=quick_deadline,
                        execution_key=execution_key,
                        register_execution=self._register_execution,
                    )
                else:
                    quick = self.dependencies.candidates.recover_quick_registered(
                        materialized,
                        deadline=quick_deadline,
                        authority=execution,
                    )
            else:
                quick = self.dependencies.candidates.evaluate_quick(materialized, deadline=quick_deadline)
            self._validate_quick(quick, identity)
        except _RuntimeAbort:
            raise
        except BaseException:
            failed = self._record_candidate_failure(
                candidate,
                stage="quick_evaluation",
                outcome="quick_evaluation_failed",
                code="quick_evaluation_failed",
            )
            self._check_finished(quick_deadline, experiment_id=identity.sha256)
            return failed
        assert candidate.semantic_fingerprint is not None
        quick_event = self.journal.append(
            QuickEvidencePayloadV5(
                experiment_id=identity.sha256,
                semantic_fingerprint=candidate.semantic_fingerprint,
                evaluation=quick,
            ),
            experiment_id=identity.sha256,
        )
        self._check_finished(quick_deadline, experiment_id=identity.sha256)
        return replace(
            candidate,
            quick_evidence=quick,
            artifact_refs=self._artifact_refs(candidate.artifact_refs, (quick_event.payload_ref,)),
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
            durable_failure = self._candidate_stage(identity.sha256, "discovery_evaluation")
            if durable_failure is not None and durable_failure.outcome != "discovery_evaluation_failed":
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
            episodes: list[EpisodeEvaluationV5] = []
            references = set(candidate.artifact_refs)
            recovered_failure = False
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
                    if durable_failure is not None:
                        if durable_failure.episode_ordinal != plan.episode_ordinal:
                            raise _RuntimeAbort(
                                RuntimeFailureV5(
                                    "recovery",
                                    "invalid_dependency_result",
                                    experiment_id=identity.sha256,
                                )
                            )
                        partial = replace(
                            candidate,
                            discovery_episodes=tuple(sorted(episodes, key=lambda item: item.episode_ordinal)),
                            artifact_refs=self._artifact_refs(
                                tuple(references),
                                (self.journal.payload_reference(durable_failure),),
                            ),
                        )
                        result.append(self._recover_candidate_failure(partial, durable_failure))
                        recovered_failure = True
                        break
                    deadline = self._deadline(
                        "discovery_evaluation",
                        self.inputs.manifest.resources.discovery_episode_timeout_seconds,
                    )
                    try:
                        if isinstance(self.dependencies.candidates, LeaseAwareCandidateRuntimeV5):
                            execution_key = CandidateExecutionKeyV5(
                                identity.sha256,
                                "discovery_evaluation",
                                plan.episode_ordinal,
                            )
                            execution = self._existing_execution(execution_key)
                            if execution is None:
                                episode = self.dependencies.candidates.evaluate_episode_registered(
                                    candidate.materialized,
                                    plan,
                                    deadline=deadline,
                                    execution_key=execution_key,
                                    register_execution=self._register_execution,
                                )
                            else:
                                episode = self.dependencies.candidates.recover_episode_registered(
                                    candidate.materialized,
                                    plan,
                                    deadline=deadline,
                                    authority=execution,
                                )
                        else:
                            episode = self.dependencies.candidates.evaluate_episode(
                                candidate.materialized,
                                plan,
                                deadline=deadline,
                            )
                        self._validate_episode(episode, plan, identity)
                    except _RuntimeAbort:
                        raise
                    except BaseException:
                        partial = replace(
                            candidate,
                            discovery_episodes=tuple(sorted(episodes, key=lambda item: item.episode_ordinal)),
                            artifact_refs=tuple(sorted(references, key=lambda item: (item.relative_path, item.sha256))),
                        )
                        result.append(
                            self._record_candidate_failure(
                                partial,
                                stage="discovery_evaluation",
                                outcome="discovery_evaluation_failed",
                                code="discovery_evaluation_failed",
                                episode_ordinal=plan.episode_ordinal,
                            )
                        )
                        self._check_finished(deadline, experiment_id=identity.sha256)
                        recovered_failure = True
                        break
                    event = self.journal.append(
                        EpisodeEvidencePayloadV5(experiment_id=identity.sha256, episode=episode),
                        experiment_id=identity.sha256,
                    )
                    references.add(event.payload_ref)
                    self._check_finished(deadline, experiment_id=identity.sha256)
                episodes.append(episode)
            if recovered_failure:
                continue
            if durable_failure is not None:
                raise _RuntimeAbort(
                    RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=identity.sha256)
                )
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
                failure_payloads = tuple(
                    item
                    for item in self.journal.find_experiment_payloads(experiment_id)
                    if type(item) is CandidateStageResultPayloadV5
                    and item.failure_code == candidate.failure_code
                    and item.failure_ref is not None
                )
                if len(failure_payloads) != 1:
                    raise _RuntimeAbort(
                        RuntimeFailureV5("recovery", "invalid_dependency_result", experiment_id=experiment_id)
                    )
                failure_ref = failure_payloads[0].failure_ref
                assert failure_ref is not None
                evidence.append(
                    PreCriticEvidenceIdentityV5(
                        experiment_id=experiment_id,
                        evidence_kind="typed_failure",
                        evidence_sha256=failure_ref.sha256,
                    )
                )
        return tuple(evidence)

    def _expected_records(
        self,
        *,
        parent: ParentCandidateV5,
        decision: ScheduledHypothesisV5,
        template: StructuralTemplateV5,
        candidates: tuple[CandidateEvidenceV5, ...],
        critic: CriticArtifactV5,
        critic_ref: ArtifactRefV5,
    ) -> tuple[ExperimentRecordV5, ...]:
        reviews = {review.experiment_id: review for review in critic.reviews}
        expected_review_ids = tuple(
            candidate.experiment_id for candidate in candidates if is_testable_experiment_status_v5(candidate.status)
        )
        if tuple(reviews) != expected_review_ids:
            raise _RuntimeAbort(RuntimeFailureV5("finalization", "invalid_dependency_result"))
        records: list[ExperimentRecordV5] = []
        try:
            for candidate in candidates:
                testable = is_testable_experiment_status_v5(candidate.status)
                campaign = candidate.campaign_evidence
                records.append(
                    ExperimentRecordV5(
                        experiment_id=candidate.experiment_id,
                        experiment_identity=candidate.experiment_identity,
                        round_index=self.inputs.round_index,
                        parent_revision_sha256=parent.policy_identity_sha256,
                        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
                        hypothesis=decision.hypothesis,
                        template=template,
                        template_sha256=template.sha256,
                        variant_assignment=candidate.materialized.variant.assignment,
                        policy_revision=(
                            None
                            if type(candidate.experiment_identity) is PreValidationInvalidExperimentIdentityV5
                            else candidate.materialized.variant.policy_revision
                        ),
                        semantic_fingerprint=candidate.semantic_fingerprint,
                        status=candidate.status,  # type: ignore[arg-type]
                        validation=candidate.validation,
                        quick_evidence=candidate.quick_evidence,
                        discovery_episodes=candidate.discovery_episodes,
                        campaign_evidence=campaign,
                        target_gap_pct=(
                            None
                            if campaign is None
                            else expected_target_gap_pct_v5(
                                target=self.inputs.manifest.target,
                                campaign_cagr=campaign.campaign_cagr_pct,
                            )
                        ),
                        critic_review=(reviews[candidate.experiment_id] if testable else None),
                        artifact_refs=candidate.artifact_refs,
                        critic_artifact_ref=(critic_ref if testable else None),
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise _RuntimeAbort(RuntimeFailureV5("finalization", "invalid_dependency_result")) from None
        return tuple(records)

    @staticmethod
    def _require_exact_records(
        records: object,
        expected_records: tuple[ExperimentRecordV5, ...],
    ) -> tuple[ExperimentRecordV5, ...]:
        if type(records) is not tuple or records != expected_records:
            raise _RuntimeAbort(RuntimeFailureV5("finalization", "invalid_dependency_result"))
        return records

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
        self._deadline("checkpoint", self.inputs.manifest.resources.round_wall_timeout_seconds)
        expected_generation = 1 if projection.checkpoint is None else projection.checkpoint.generation + 1
        checkpoint = self.dependencies.persistence.publish_projection(
            record_refs=all_refs,
            reducer=reducer,
            generation=expected_generation,
        )
        if (
            type(checkpoint) is not RepositoryCheckpointV5
            or checkpoint.generation != expected_generation
            or checkpoint.record_refs != all_refs
        ):
            raise _RuntimeAbort(RuntimeFailureV5("checkpoint", "invalid_dependency_result"))
        current_refs = tuple(sorted(refs, key=lambda item: (item.relative_path, item.sha256)))
        self.record_refs = current_refs
        self.checkpoint = checkpoint
        return current_refs, checkpoint

    def run(self) -> FeedbackRoundResultV5:
        projection = self._recover_projection()
        if self.checkpoint is not None:
            return self._completed_result(parent=None)
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
            if type(terminal.authority) is RuntimeFailureAuthorityV5:
                return self._failure_result(
                    RuntimeFailureV5(
                        stage=terminal.authority.stage,
                        code=terminal.authority.failure_code,
                        role=terminal.authority.role,
                        experiment_id=terminal.authority.experiment_id,
                    )
                )
            return self._terminal_result(terminal.authority)

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
        expected_records = self._expected_records(
            parent=parent,
            decision=decision,
            template=template,
            candidates=candidates,
            critic=critic,
            critic_ref=critic_ref,
        )
        try:
            records = self.dependencies.records.build_records(
                inputs=self.inputs,
                parent=parent,
                decision=decision,
                template=template,
                candidates=candidates,
                critic=critic,
                critic_ref=critic_ref,
            )
        except BaseException:
            raise _RuntimeAbort(RuntimeFailureV5("finalization", "stage_failed")) from None
        records = self._require_exact_records(records, expected_records)
        self._check_finished(deadline)
        self._publish(
            projection=projection,
            candidates=candidates,
            records=records,
        )
        return self._completed_result(parent=parent)


def run_feedback_round_v5(
    inputs: FeedbackRoundInputV5,
    dependencies: FeedbackRoundDependenciesV5,
) -> FeedbackRoundResultV5:
    """Execute or resume one feedback round without concrete external adapters."""

    if type(inputs) is not FeedbackRoundInputV5 or type(dependencies) is not FeedbackRoundDependenciesV5:
        raise ValueError("feedback-round entry requires exact V5 input and dependencies")
    runtime: _Runtime | None = None

    def bare_failure(failure: RuntimeFailureV5) -> FeedbackRoundResultV5:
        return FeedbackRoundResultV5(
            status="failed",
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
            parent=(None if runtime is None else runtime.parent),
            terminal_outcome=None,
            record_refs=(() if runtime is None else runtime.record_refs),
            checkpoint=(None if runtime is None else runtime.checkpoint),
            cleanup=None,
            failure=failure,
        )

    def committed_result() -> FeedbackRoundResultV5:
        assert runtime is not None and type(runtime.checkpoint) is RepositoryCheckpointV5
        try:
            return runtime._completed_result(parent=runtime.parent)
        except BaseException:
            try:
                cleanup = runtime.journal.cleanup_payload()
            except BaseException:
                cleanup = None
            cleanup_failure = (
                None
                if cleanup is not None and cleanup.cleanup_complete
                else RuntimeFailureV5("cleanup", "cleanup_failed")
            )
            return FeedbackRoundResultV5(
                status="completed",
                campaign_id=inputs.campaign_id,
                round_index=inputs.round_index,
                parent=runtime.parent,
                terminal_outcome=None,
                record_refs=runtime.record_refs,
                checkpoint=runtime.checkpoint,
                cleanup=cleanup,
                failure=None,
                cleanup_failure=cleanup_failure,
            )

    try:
        runtime = _Runtime(inputs, dependencies)
        return runtime.run()
    except _RuntimeAbort as abort:
        if runtime is None:
            return bare_failure(abort.failure)
        if runtime.checkpoint is not None:
            return committed_result()
        try:
            return runtime._failure_result(abort.failure)
        except BaseException:
            return bare_failure(abort.failure)
    except BaseException:
        failure = RuntimeFailureV5("recovery", "stage_failed")
        if runtime is None:
            return bare_failure(failure)
        if runtime.checkpoint is not None:
            return committed_result()
        try:
            return runtime._failure_result(failure)
        except BaseException:
            return bare_failure(failure)


__all__ = [
    "ArchiveReducerFactoryV5",
    "CandidateEvidenceV5",
    "CandidateExecutionAuthorityV5",
    "CandidateExecutionKeyV5",
    "CandidateExecutionPersistenceV5",
    "CandidateExecutionRegistrarV5",
    "CandidateExecutionStageV5",
    "CandidateRuntimeV5",
    "ExperimentRecordFactoryV5",
    "FeedbackRoundDependenciesV5",
    "FeedbackRoundInputV5",
    "FeedbackRoundResultV5",
    "MaterializedVariantV5",
    "LeaseAwareCandidateRuntimeV5",
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
