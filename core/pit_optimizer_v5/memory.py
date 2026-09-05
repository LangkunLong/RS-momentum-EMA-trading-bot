"""Pure immutable memory and recovery contracts for PIT optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, TypeVar

from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    PolicyRevisionIdentityV5,
    PreValidationInvalidExperimentIdentityV5,
    RenderedVariantV5,
    StructuralTemplateV5,
    VariantAssignmentV5,
    derive_experiment_identity_v5,
    derive_pre_validation_invalid_experiment_identity_v5,
    validate_post_validation_experiment_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignEvidenceV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    HypothesisV5,
    PanelEvaluationV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.probes import SemanticFingerprintV5
from core.pit_optimizer_v5.provider import RoleNameV5, RoleOutcomeV5


ExperimentStatusV5 = Literal[
    "invalid",
    "exact_duplicate",
    "behavioral_equivalent",
    "zero_trade",
    "quick_rejected",
    "timed_out",
    "cancelled",
    "evaluation_failed",
    "evaluated",
]
ExperimentIdentityLikeV5 = ExperimentIdentityV5 | PreValidationInvalidExperimentIdentityV5
RoundEventKindV5 = Literal[
    "round_intent",
    "role_completion",
    "rendered_variant",
    "candidate_stage_result",
    "quick_evidence",
    "episode_evidence",
    "resource_lease",
    "cleanup_result",
    "round_outcome",
]
RoundOutcomeKindV5 = Literal[
    "no_novel_hypothesis",
    "novelty_exhausted",
    "critic_unavailable",
    "runtime_failed",
]
CandidateStageV5 = Literal["validation", "semantic_probe", "quick_evaluation", "discovery_evaluation"]
CandidateStageOutcomeV5 = Literal[
    "validation_valid",
    "validation_invalid",
    "validation_failed",
    "exact_duplicate",
    "behavioral_equivalent",
    "behaviorally_distinct",
    "semantic_probe_failed",
    "quick_evaluation_failed",
    "discovery_evaluation_failed",
]
CandidateStageFailureCodeV5 = Literal[
    "validation_execution_failed",
    "semantic_probe_failed",
    "quick_evaluation_failed",
    "discovery_evaluation_failed",
]
RoundFailureStageV5 = Literal[
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
RoundFailureCodeV5 = Literal[
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

_STATUSES = frozenset(
    {
        "invalid",
        "exact_duplicate",
        "behavioral_equivalent",
        "zero_trade",
        "quick_rejected",
        "timed_out",
        "cancelled",
        "evaluation_failed",
        "evaluated",
    }
)
_TESTABLE_STATUSES = frozenset(
    {
        "zero_trade",
        "quick_rejected",
        "timed_out",
        "cancelled",
        "evaluation_failed",
        "evaluated",
    }
)
_EVENT_KINDS = frozenset(
    {
        "round_intent",
        "role_completion",
        "rendered_variant",
        "candidate_stage_result",
        "quick_evidence",
        "episode_evidence",
        "resource_lease",
        "cleanup_result",
        "round_outcome",
    }
)
_CANDIDATE_STAGE_INDEX = {
    "validation": 1,
    "semantic_probe": 2,
    "quick_evaluation": 3,
    "discovery_evaluation": 4,
}
_CANDIDATE_STAGE_OUTCOMES = frozenset(
    {
        "validation_valid",
        "validation_invalid",
        "validation_failed",
        "exact_duplicate",
        "behavioral_equivalent",
        "behaviorally_distinct",
        "semantic_probe_failed",
        "quick_evaluation_failed",
        "discovery_evaluation_failed",
    }
)
_CANDIDATE_STAGE_FAILURE_CODES = frozenset(
    {
        "validation_execution_failed",
        "semantic_probe_failed",
        "quick_evaluation_failed",
        "discovery_evaluation_failed",
    }
)
_ROUND_FAILURE_STAGES = frozenset(
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
_ROUND_FAILURE_CODES = frozenset(
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
_PRECRITIC_EVIDENCE_KINDS = frozenset(
    {
        "rendered_variant",
        "validation",
        "semantic_fingerprint",
        "quick_evaluation",
        "episode_evaluation",
        "typed_failure",
    }
)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _digest_tuple(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise ValueError(f"{label} must be an immutable digest tuple")
    if required and not value:
        raise ValueError(f"{label} must not be empty")
    for item in value:
        _digest(item, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")
    return value


def is_testable_experiment_status_v5(status: ExperimentStatusV5) -> bool:
    """Return whether a final status belongs in the complete critic batch."""

    if type(status) is not str or status not in _STATUSES:
        raise ValueError("experiment status is invalid")
    return status in _TESTABLE_STATUSES


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _count(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(f"{label} must be an integer in range")
    return value


def _optional_decimal(value: object, label: str) -> Decimal | None:
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    return value


def _ordered_unique_refs(value: object, label: str) -> tuple[ArtifactRefV5, ...]:
    if type(value) is not tuple or any(type(item) is not ArtifactRefV5 for item in value):
        raise ValueError(f"{label} must contain V5 artifact references")
    keys = tuple((item.relative_path, item.sha256) for item in value)
    if keys != tuple(sorted(set(keys))):
        raise ValueError(f"{label} must be unique and canonically ordered")
    return value


def _evidence_policy_digests(
    episodes: tuple[EpisodeEvaluationV5, ...],
) -> set[str]:
    return {item.evaluation.policy_identity_sha256 for item in episodes}


def _literal_artifact_primitive(value: object) -> dict[str, object]:
    if type(value) is bool:
        kind = "bool"
        primitive: object = value
    elif type(value) is int:
        kind = "int"
        primitive = value
    elif type(value) is str:
        kind = "str"
        primitive = value
    elif type(value) is float:
        if not (value == value and abs(value) != float("inf")):
            raise ValueError("variant float literal must be finite")
        kind = "float"
        primitive = value
    else:
        raise ValueError("variant literal is outside the closed exact-type set")
    return {"kind": kind, "value": primitive}


def _assignment_artifact_primitive(
    assignment: VariantAssignmentV5,
) -> dict[str, object]:
    return {
        "values": [{"name": name, "value": _literal_artifact_primitive(value)} for name, value in assignment.values]
    }


def _template_artifact_primitive(template: StructuralTemplateV5) -> dict[str, object]:
    return {
        "hypothesis_id": template.hypothesis_id,
        "parent_revision_sha256": template.parent_revision_sha256,
        "changed_symbols": list(template.changed_symbols),
        "source_operations": [item.to_primitive() for item in template.source_operations],
        "axes": [
            {
                "name": axis.name,
                "default": _literal_artifact_primitive(axis.default),
                "values": [_literal_artifact_primitive(value) for value in axis.values],
            }
            for axis in template.axes
        ],
        "full_source_escape": (
            None
            if template.full_source_escape is None
            else [item.to_primitive() for item in template.full_source_escape]
        ),
    }


@dataclass(frozen=True, slots=True)
class ExperimentRecordV5:
    """One final, immutable outcome for every intended local experiment."""

    experiment_id: str
    experiment_identity: ExperimentIdentityLikeV5
    round_index: int
    parent_revision_sha256: str
    parent_semantic_fingerprint_sha256: str
    hypothesis: HypothesisV5
    template: StructuralTemplateV5
    template_sha256: str
    variant_assignment: VariantAssignmentV5
    policy_revision: PolicyRevisionIdentityV5 | None
    semantic_fingerprint: SemanticFingerprintV5 | None
    status: ExperimentStatusV5
    validation: ValidationResultV5
    quick_evidence: PanelEvaluationV5 | None
    discovery_episodes: tuple[EpisodeEvaluationV5, ...]
    campaign_evidence: CampaignEvidenceV5 | None
    target_gap_pct: Decimal | None
    critic_review: CriticReviewV5 | None
    artifact_refs: tuple[ArtifactRefV5, ...]
    critic_artifact_ref: ArtifactRefV5 | None = None

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "experiment ID")
        _count(self.round_index, "experiment round", positive=True)
        _digest(self.parent_revision_sha256, "parent revision SHA-256")
        _digest(
            self.parent_semantic_fingerprint_sha256,
            "parent semantic fingerprint SHA-256",
        )
        if type(self.hypothesis) is not HypothesisV5:
            raise ValueError("experiment hypothesis must use the V5 schema")
        if type(self.template) is not StructuralTemplateV5:
            raise ValueError("experiment template must use the V5 schema")
        if type(self.variant_assignment) is not VariantAssignmentV5:
            raise ValueError("experiment assignment must use the V5 schema")
        _digest(self.template_sha256, "experiment template SHA-256")
        if self.template_sha256 != self.template.sha256:
            raise ValueError("experiment template digest differs from its contents")
        if self.template.parent_revision_sha256 != self.parent_revision_sha256:
            raise ValueError("experiment template differs from its parent revision")
        if self.template.hypothesis_id != self.hypothesis.hypothesis_id:
            raise ValueError("experiment template differs from its hypothesis")
        if self.status not in _STATUSES:
            raise ValueError("experiment status is invalid")
        if type(self.validation) is not ValidationResultV5:
            raise ValueError("experiment validation must use the V5 schema")
        if self.semantic_fingerprint is not None and type(self.semantic_fingerprint) is not SemanticFingerprintV5:
            raise ValueError("experiment semantic fingerprint is invalid")
        if self.quick_evidence is not None and type(self.quick_evidence) is not PanelEvaluationV5:
            raise ValueError("experiment quick evidence is invalid")
        if type(self.discovery_episodes) is not tuple or any(
            type(item) is not EpisodeEvaluationV5 for item in self.discovery_episodes
        ):
            raise ValueError("experiment discovery evidence is invalid")
        episode_keys = tuple((item.episode_ordinal, item.episode_id) for item in self.discovery_episodes)
        if episode_keys != tuple(sorted(set(episode_keys))):
            raise ValueError("experiment discovery evidence must be canonical and unique")
        if self.campaign_evidence is not None and type(self.campaign_evidence) is not CampaignEvidenceV5:
            raise ValueError("experiment campaign evidence is invalid")
        _optional_decimal(self.target_gap_pct, "experiment target gap")
        if self.critic_review is not None and type(self.critic_review) is not CriticReviewV5:
            raise ValueError("experiment critic review is invalid")
        _ordered_unique_refs(self.artifact_refs, "experiment artifact references")
        if self.critic_artifact_ref is not None and type(self.critic_artifact_ref) is not ArtifactRefV5:
            raise ValueError("experiment critic artifact reference is invalid")
        if is_testable_experiment_status_v5(self.status):
            if self.critic_review is None or self.critic_artifact_ref is None:
                raise ValueError("testable experiment requires its complete critic binding")
        elif self.critic_review is not None or self.critic_artifact_ref is not None:
            raise ValueError("untestable experiment cannot carry critic fields")

        if self.experiment_id != self.experiment_identity.sha256:
            raise ValueError("experiment ID differs from its identity")
        if self.status == "invalid":
            self._validate_invalid_identity()
        else:
            self._validate_post_validation_identity()
        self._validate_status_evidence()
        self._validate_bound_evidence()

    def _validate_invalid_identity(self) -> None:
        if type(self.experiment_identity) is not PreValidationInvalidExperimentIdentityV5:
            raise ValueError("invalid experiment requires the pre-validation identity")
        if self.policy_revision is not None or self.validation.valid:
            raise ValueError("invalid experiment cannot carry a validated policy revision")
        expected = derive_pre_validation_invalid_experiment_identity_v5(
            parent_revision_sha256=self.parent_revision_sha256,
            hypothesis=self.hypothesis,
            template=self.template,
            assignment=self.variant_assignment,
            round_index=self.round_index,
            discovery_plan_sha256=self.experiment_identity.discovery_plan_sha256,
        )
        if expected != self.experiment_identity:
            raise ValueError("invalid experiment identity bindings differ")
        if (
            any(
                item is not None
                for item in (
                    self.semantic_fingerprint,
                    self.quick_evidence,
                    self.campaign_evidence,
                    self.target_gap_pct,
                    self.critic_review,
                    self.critic_artifact_ref,
                )
            )
            or self.discovery_episodes
        ):
            raise ValueError("invalid experiment cannot carry downstream evidence")

    def _validate_post_validation_identity(self) -> None:
        if type(self.experiment_identity) is not ExperimentIdentityV5:
            raise ValueError("validated experiment requires the post-validation identity")
        if self.policy_revision is None or not self.validation.valid:
            raise ValueError("validated experiment requires a valid policy revision")
        if self.validation.changed_symbols != self.template.changed_symbols:
            raise ValueError("validation changed symbols differ from the template")
        validate_post_validation_experiment_identity_v5(
            self.experiment_identity,
            policy_revision=self.policy_revision,
        )
        expected = derive_experiment_identity_v5(
            policy_revision=self.policy_revision,
            parent_revision_sha256=self.parent_revision_sha256,
            hypothesis=self.hypothesis,
            template=self.template,
            assignment=self.variant_assignment,
            round_index=self.round_index,
            discovery_plan_sha256=self.experiment_identity.discovery_plan_sha256,
        )
        if expected != self.experiment_identity:
            raise ValueError("validated experiment identity bindings differ")

    def _validate_status_evidence(self) -> None:
        if self.status == "invalid":
            return
        if self.status == "exact_duplicate":
            if (
                self.policy_revision is None
                or self.policy_revision.sha256 != self.parent_revision_sha256
                or self.semantic_fingerprint is not None
                or self.quick_evidence is not None
                or self.discovery_episodes
                or self.campaign_evidence is not None
                or self.target_gap_pct is not None
            ):
                raise ValueError("exact duplicate experiment evidence is inconsistent")
        elif self.status == "behavioral_equivalent":
            if (
                self.semantic_fingerprint is None
                or self.semantic_fingerprint.fingerprint_sha256 != self.parent_semantic_fingerprint_sha256
                or self.quick_evidence is not None
                or self.discovery_episodes
                or self.campaign_evidence is not None
                or self.target_gap_pct is not None
            ):
                raise ValueError("behaviorally equivalent experiment evidence is inconsistent")
        elif self.status == "quick_rejected":
            if (
                self.semantic_fingerprint is None
                or self.quick_evidence is None
                or self.discovery_episodes
                or self.campaign_evidence is not None
                or self.target_gap_pct is not None
            ):
                raise ValueError("quick-rejected experiment evidence is inconsistent")
        elif self.status == "zero_trade":
            if (
                self.semantic_fingerprint is None
                or self.quick_evidence is None
                or len(self.discovery_episodes) != 4
                or self.campaign_evidence is None
                or self.campaign_evidence.closed_trades != 0
                or self.target_gap_pct is None
            ):
                raise ValueError("zero-trade experiment requires complete zero-activity evidence")
        elif self.status == "evaluated":
            if (
                self.semantic_fingerprint is None
                or self.quick_evidence is None
                or len(self.discovery_episodes) != 4
                or self.campaign_evidence is None
                or self.target_gap_pct is None
            ):
                raise ValueError("evaluated experiment requires complete campaign evidence")
        elif self.status in {"timed_out", "cancelled", "evaluation_failed"}:
            if self.campaign_evidence is not None or self.target_gap_pct is not None:
                raise ValueError("interrupted experiment cannot carry completed campaign scoring")
        if (
            self.semantic_fingerprint is not None
            and self.status != "behavioral_equivalent"
            and self.semantic_fingerprint.fingerprint_sha256 == self.parent_semantic_fingerprint_sha256
        ):
            raise ValueError("behaviorally distinct experiment status carries the parent fingerprint")

    def _validate_bound_evidence(self) -> None:
        if self.policy_revision is None:
            return
        revision_sha256 = self.policy_revision.sha256
        if self.quick_evidence is not None and self.quick_evidence.policy_identity_sha256 != revision_sha256:
            raise ValueError("quick evidence differs from the experiment policy")
        policy_digests = _evidence_policy_digests(self.discovery_episodes)
        if policy_digests and policy_digests != {revision_sha256}:
            raise ValueError("discovery evidence differs from the experiment policy")
        if self.quick_evidence is not None and self.discovery_episodes:
            if any(
                item.evaluation.evaluator_contract_sha256 != self.quick_evidence.evaluator_contract_sha256
                or item.evaluation.sandbox_profile_sha256 != self.quick_evidence.sandbox_profile_sha256
                for item in self.discovery_episodes
            ):
                raise ValueError("quick and discovery evaluator identities differ")
        if self.campaign_evidence is not None:
            if self.campaign_evidence.episodes != self.discovery_episodes:
                raise ValueError("campaign evidence differs from the experiment episodes")
            if (
                self.campaign_evidence.policy_identity_sha256 != revision_sha256
                or self.campaign_evidence.discovery_plan_sha256 != self.experiment_identity.discovery_plan_sha256
            ):
                raise ValueError("campaign evidence identity bindings differ")
        if self.critic_review is not None and (self.critic_review.experiment_id != self.experiment_id):
            raise ValueError("critic review differs from the experiment identity")

    def to_primitive(self) -> dict[str, object]:
        identity_kind = (
            "pre_validation_invalid"
            if type(self.experiment_identity) is PreValidationInvalidExperimentIdentityV5
            else "post_validation"
        )
        return {
            "schema_version": 5,
            "experiment_id": self.experiment_id,
            "experiment_identity_kind": identity_kind,
            "experiment_identity": self.experiment_identity.to_primitive(),
            "round_index": self.round_index,
            "parent_revision_sha256": self.parent_revision_sha256,
            "parent_semantic_fingerprint_sha256": self.parent_semantic_fingerprint_sha256,
            "hypothesis": canonical_primitive_v5(self.hypothesis),
            "template": _template_artifact_primitive(self.template),
            "template_sha256": self.template_sha256,
            "variant_assignment": _assignment_artifact_primitive(self.variant_assignment),
            "policy_revision": (None if self.policy_revision is None else self.policy_revision.to_primitive()),
            "semantic_fingerprint": (
                None if self.semantic_fingerprint is None else self.semantic_fingerprint.to_primitive()
            ),
            "status": self.status,
            "validation": canonical_primitive_v5(self.validation),
            "quick_evidence": canonical_primitive_v5(self.quick_evidence),
            "discovery_episodes": canonical_primitive_v5(self.discovery_episodes),
            "campaign_evidence": canonical_primitive_v5(self.campaign_evidence),
            "target_gap_pct": canonical_primitive_v5(self.target_gap_pct),
            "critic_review": canonical_primitive_v5(self.critic_review),
            "artifact_refs": [item.to_primitive() for item in self.artifact_refs],
            "critic_artifact_ref": (
                None if self.critic_artifact_ref is None else self.critic_artifact_ref.to_primitive()
            ),
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self.to_primitive())


@dataclass(frozen=True, slots=True)
class RoundIntentPayloadV5:
    parent_revision_sha256: str
    parent_semantic_fingerprint_sha256: str
    hypothesis: HypothesisV5
    discovery_plan_sha256: str

    def __post_init__(self) -> None:
        _digest(self.parent_revision_sha256, "round-intent parent revision")
        _digest(
            self.parent_semantic_fingerprint_sha256,
            "round-intent parent fingerprint",
        )
        if type(self.hypothesis) is not HypothesisV5:
            raise ValueError("round-intent hypothesis is invalid")
        _digest(self.discovery_plan_sha256, "round-intent discovery plan")


@dataclass(frozen=True, slots=True)
class RoleCompletionPayloadV5:
    """Authenticated durable terminal package for one fixed round role."""

    campaign_id: str
    round_index: int
    role: RoleNameV5
    role_position: int
    outcome: RoleOutcomeV5
    call_key_sha256: str
    request_sha256: str
    binding_sha256: str
    attempt_sha256: str
    terminal_authority_sha256: str
    artifact_sha256: str | None
    package_sha256: str
    request_ref: ArtifactRefV5
    attempt_ref: ArtifactRefV5
    terminal_authority_ref: ArtifactRefV5
    artifact_ref: ArtifactRefV5 | None

    def __post_init__(self) -> None:
        _text(self.campaign_id, "role-completion campaign ID")
        _count(self.round_index, "role-completion round", positive=True)
        expected_position = {"investigator": 1, "author": 2, "critic": 3}.get(self.role)
        if type(self.role_position) is not int or self.role_position != expected_position:
            raise ValueError("role completion differs from its fixed round position")
        if self.outcome not in {
            "accepted",
            "transport_failure",
            "response_schema_failure",
            "evidence_binding_failure",
            "authorization_failure",
            "accounting_failure",
        }:
            raise ValueError("role completion outcome is invalid")
        for value, label in (
            (self.call_key_sha256, "role-completion call key"),
            (self.request_sha256, "role-completion request"),
            (self.binding_sha256, "role-completion binding"),
            (self.attempt_sha256, "role-completion attempt"),
            (self.terminal_authority_sha256, "role-completion terminal authority"),
            (self.package_sha256, "role-completion package"),
        ):
            _digest(value, label)
        if self.artifact_sha256 is not None:
            _digest(self.artifact_sha256, "role-completion artifact")
        refs = (
            (self.request_ref, f"roles/requests/{self.call_key_sha256}.json"),
            (self.attempt_ref, f"roles/attempts/{self.role}/{self.attempt_sha256}.json"),
            (
                self.terminal_authority_ref,
                f"roles/terminals/{self.role}/{self.terminal_authority_sha256}.json",
            ),
        )
        for reference, expected_path in refs:
            if type(reference) is not ArtifactRefV5 or reference.relative_path != expected_path:
                raise ValueError("role completion reference differs from its authority")
        if self.outcome == "accepted":
            if self.artifact_sha256 is None or type(self.artifact_ref) is not ArtifactRefV5:
                raise ValueError("accepted role completion requires its exact artifact")
            if self.artifact_ref.relative_path != f"roles/artifacts/{self.role}/{self.artifact_sha256}.json":
                raise ValueError("role completion artifact path differs from its authority")
        elif self.artifact_sha256 is not None or self.artifact_ref is not None:
            raise ValueError("failed role completion cannot carry an artifact")


@dataclass(frozen=True, slots=True)
class RenderedVariantPayloadV5:
    variant: RenderedVariantV5

    def __post_init__(self) -> None:
        if type(self.variant) is not RenderedVariantV5:
            raise ValueError("rendered-variant payload is invalid")


@dataclass(frozen=True, slots=True)
class CandidateStageResultPayloadV5:
    """One indexed, durable local candidate-stage result."""

    experiment_id: str
    stage: CandidateStageV5
    stage_index: int
    outcome: CandidateStageOutcomeV5
    validation: ValidationResultV5 | None = None
    semantic_fingerprint: SemanticFingerprintV5 | None = None
    failure_code: CandidateStageFailureCodeV5 | None = None
    failure_ref: ArtifactRefV5 | None = None
    episode_ordinal: int | None = None

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "candidate-stage experiment ID")
        expected_index = _CANDIDATE_STAGE_INDEX.get(self.stage)
        if type(self.stage_index) is not int or self.stage_index != expected_index:
            raise ValueError("candidate-stage index differs from its stage")
        if self.outcome not in _CANDIDATE_STAGE_OUTCOMES:
            raise ValueError("candidate-stage outcome is invalid")
        expected_stage = {
            "validation_valid": "validation",
            "validation_invalid": "validation",
            "validation_failed": "validation",
            "exact_duplicate": "semantic_probe",
            "behavioral_equivalent": "semantic_probe",
            "behaviorally_distinct": "semantic_probe",
            "semantic_probe_failed": "semantic_probe",
            "quick_evaluation_failed": "quick_evaluation",
            "discovery_evaluation_failed": "discovery_evaluation",
        }[self.outcome]
        if self.stage != expected_stage:
            raise ValueError("candidate-stage outcome differs from its stage")

        validation_outcomes = {"validation_valid", "validation_invalid", "validation_failed"}
        semantic_outcomes = {"behavioral_equivalent", "behaviorally_distinct"}
        failure_outcomes = {
            "validation_failed": "validation_execution_failed",
            "semantic_probe_failed": "semantic_probe_failed",
            "quick_evaluation_failed": "quick_evaluation_failed",
            "discovery_evaluation_failed": "discovery_evaluation_failed",
        }
        if self.outcome in validation_outcomes:
            if type(self.validation) is not ValidationResultV5:
                raise ValueError("validation stage requires its exact result")
            expected_valid = self.outcome == "validation_valid"
            if self.validation.valid != expected_valid:
                raise ValueError("validation stage outcome differs from its result")
        elif self.validation is not None:
            raise ValueError("non-validation stage cannot carry validation evidence")
        if self.outcome in semantic_outcomes:
            if type(self.semantic_fingerprint) is not SemanticFingerprintV5:
                raise ValueError("semantic stage requires its exact fingerprint")
        elif self.semantic_fingerprint is not None:
            raise ValueError("non-semantic result cannot carry a fingerprint")

        expected_failure = failure_outcomes.get(self.outcome)
        if expected_failure is None:
            if self.failure_code is not None or self.failure_ref is not None:
                raise ValueError("successful candidate stage cannot carry failure authority")
        elif (
            self.failure_code != expected_failure
            or self.failure_code not in _CANDIDATE_STAGE_FAILURE_CODES
            or type(self.failure_ref) is not ArtifactRefV5
            or self.failure_ref.relative_path != f"inputs/typed_failure/{self.failure_ref.sha256}.json"
        ):
            raise ValueError("failed candidate stage lacks its exact typed failure reference")
        if self.outcome == "discovery_evaluation_failed":
            ordinal = _count(self.episode_ordinal, "failed discovery episode ordinal", positive=True)
            if ordinal > 4:
                raise ValueError("failed discovery episode ordinal is outside the fixed panel")
        elif self.episode_ordinal is not None:
            raise ValueError("only discovery failure can carry an episode ordinal")


@dataclass(frozen=True, slots=True)
class QuickEvidencePayloadV5:
    experiment_id: str
    semantic_fingerprint: SemanticFingerprintV5
    evaluation: PanelEvaluationV5

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "quick-evidence experiment ID")
        if type(self.semantic_fingerprint) is not SemanticFingerprintV5:
            raise ValueError("quick-evidence semantic fingerprint is invalid")
        if type(self.evaluation) is not PanelEvaluationV5:
            raise ValueError("quick-evidence panel is invalid")


@dataclass(frozen=True, slots=True)
class EpisodeEvidencePayloadV5:
    experiment_id: str
    episode: EpisodeEvaluationV5

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "episode-evidence experiment ID")
        if type(self.episode) is not EpisodeEvaluationV5:
            raise ValueError("episode-evidence payload is invalid")


@dataclass(frozen=True, slots=True)
class ResourceLeasePayloadV5:
    lease_id: str
    resource_kind: Literal["workspace", "policy_worker", "evaluator_process", "container"]
    owner_campaign_id: str
    owner_token_sha256: str

    def __post_init__(self) -> None:
        _text(self.lease_id, "resource lease ID")
        if self.resource_kind not in {
            "workspace",
            "policy_worker",
            "evaluator_process",
            "container",
        }:
            raise ValueError("resource lease kind is invalid")
        _text(self.owner_campaign_id, "resource lease campaign")
        _digest(self.owner_token_sha256, "resource lease owner token")


@dataclass(frozen=True, slots=True)
class CleanupResultPayloadV5:
    owned_workspaces: int
    owned_policy_workers: int
    owned_evaluators: int
    owned_containers: int
    cleanup_complete: bool
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.owned_workspaces, "owned workspaces"),
            (self.owned_policy_workers, "owned policy workers"),
            (self.owned_evaluators, "owned evaluators"),
            (self.owned_containers, "owned containers"),
        ):
            _count(value, label)
        if type(self.cleanup_complete) is not bool:
            raise ValueError("cleanup completion flag is invalid")
        if self.cleanup_complete:
            if self.failure_code is not None:
                raise ValueError("complete cleanup cannot carry a failure code")
        else:
            _text(self.failure_code, "cleanup failure code")


@dataclass(frozen=True, slots=True)
class NoNovelHypothesisAuthorityV5:
    """Exact controller and investigator facts for one exhausted parent."""

    outcome: Literal["no_novel_hypothesis"]
    discovery_plan_sha256: str
    search_state_before_sha256: str
    search_state_after_sha256: str
    parent_revision_sha256: str
    investigator_request_sha256: str
    investigator_evidence_sha256: str
    investigator_attempt_sha256s: tuple[str, ...]
    investigator_artifact_sha256: str
    selection_outcome_sha256: str

    def __post_init__(self) -> None:
        if self.outcome != "no_novel_hypothesis":
            raise ValueError("no-novel authority kind is invalid")
        for value, label in (
            (self.discovery_plan_sha256, "no-novel discovery plan"),
            (self.search_state_before_sha256, "no-novel prior search state"),
            (self.search_state_after_sha256, "no-novel resulting search state"),
            (self.parent_revision_sha256, "no-novel parent revision"),
            (self.investigator_request_sha256, "no-novel investigator request"),
            (self.investigator_evidence_sha256, "no-novel investigator evidence"),
            (self.investigator_artifact_sha256, "no-novel investigator artifact"),
            (self.selection_outcome_sha256, "no-novel selection outcome"),
        ):
            _digest(value, label)
        _digest_tuple(
            self.investigator_attempt_sha256s,
            "no-novel investigator attempts",
            required=True,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class NoveltyExhaustedAuthorityV5:
    """Exact ordered per-parent outcomes proving campaign novelty exhaustion."""

    outcome: Literal["novelty_exhausted"]
    discovery_plan_sha256: str
    search_state_before_sha256: str
    search_state_after_sha256: str
    parent_outcomes: tuple[NoNovelHypothesisAuthorityV5, ...]

    def __post_init__(self) -> None:
        if self.outcome != "novelty_exhausted":
            raise ValueError("novelty-exhausted authority kind is invalid")
        for value, label in (
            (self.discovery_plan_sha256, "novelty-exhausted discovery plan"),
            (self.search_state_before_sha256, "novelty-exhausted prior search state"),
            (self.search_state_after_sha256, "novelty-exhausted resulting search state"),
        ):
            _digest(value, label)
        if (
            type(self.parent_outcomes) is not tuple
            or not self.parent_outcomes
            or any(type(item) is not NoNovelHypothesisAuthorityV5 for item in self.parent_outcomes)
        ):
            raise ValueError("novelty-exhausted parent outcomes are invalid")
        if any(item.discovery_plan_sha256 != self.discovery_plan_sha256 for item in self.parent_outcomes):
            raise ValueError("novelty exhaustion spans discovery plans")
        if len({item.parent_revision_sha256 for item in self.parent_outcomes}) != len(self.parent_outcomes):
            raise ValueError("novelty exhaustion contains a duplicate parent")
        state_chain = (
            self.search_state_before_sha256,
            *(item.search_state_after_sha256 for item in self.parent_outcomes),
        )
        expected_chain = (
            *(item.search_state_before_sha256 for item in self.parent_outcomes),
            self.search_state_after_sha256,
        )
        if state_chain != expected_chain:
            raise ValueError("novelty exhaustion has a broken search-state chain")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


PreCriticEvidenceKindV5 = Literal[
    "rendered_variant",
    "validation",
    "semantic_fingerprint",
    "quick_evaluation",
    "episode_evaluation",
    "typed_failure",
]


@dataclass(frozen=True, slots=True)
class PreCriticEvidenceIdentityV5:
    """One typed pre-critic fact retained when no critic slot is available."""

    experiment_id: str
    evidence_kind: PreCriticEvidenceKindV5
    evidence_sha256: str
    episode_ordinal: int | None = None

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "pre-critic evidence experiment")
        if type(self.evidence_kind) is not str or self.evidence_kind not in _PRECRITIC_EVIDENCE_KINDS:
            raise ValueError("pre-critic evidence kind is invalid")
        _digest(self.evidence_sha256, "pre-critic evidence identity")
        if self.evidence_kind == "episode_evaluation":
            _count(self.episode_ordinal, "pre-critic episode ordinal", positive=True)
        elif self.episode_ordinal is not None:
            raise ValueError("only episode evidence may carry an ordinal")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class CriticUnavailableAuthorityV5:
    """Exact failed critic role plus the complete retained pre-critic batch."""

    outcome: Literal["critic_unavailable"]
    discovery_plan_sha256: str
    search_state_before_sha256: str
    search_state_after_sha256: str
    parent_revision_sha256: str
    hypothesis_id: str
    critic_request_sha256: str
    critic_evidence_sha256: str
    critic_attempt_sha256s: tuple[str, ...]
    experiment_ids: tuple[str, ...]
    precritic_evidence: tuple[PreCriticEvidenceIdentityV5, ...]

    def __post_init__(self) -> None:
        if self.outcome != "critic_unavailable":
            raise ValueError("critic-unavailable authority kind is invalid")
        for value, label in (
            (self.discovery_plan_sha256, "critic-unavailable discovery plan"),
            (self.search_state_before_sha256, "critic-unavailable prior search state"),
            (self.search_state_after_sha256, "critic-unavailable resulting search state"),
            (self.parent_revision_sha256, "critic-unavailable parent revision"),
            (self.critic_request_sha256, "critic-unavailable request"),
            (self.critic_evidence_sha256, "critic-unavailable evidence"),
        ):
            _digest(value, label)
        _text(self.hypothesis_id, "critic-unavailable hypothesis ID")
        _digest_tuple(
            self.critic_attempt_sha256s,
            "critic-unavailable attempts",
            required=True,
        )
        _digest_tuple(
            self.experiment_ids,
            "critic-unavailable experiments",
            required=True,
        )
        if (
            type(self.precritic_evidence) is not tuple
            or not self.precritic_evidence
            or any(type(item) is not PreCriticEvidenceIdentityV5 for item in self.precritic_evidence)
        ):
            raise ValueError("critic-unavailable pre-critic evidence is invalid")
        evidence_keys = tuple(
            (item.experiment_id, item.evidence_kind, item.episode_ordinal) for item in self.precritic_evidence
        )
        if len(set(evidence_keys)) != len(evidence_keys):
            raise ValueError("critic-unavailable pre-critic evidence is duplicated")
        evidence_experiment_ids = {item.experiment_id for item in self.precritic_evidence}
        if evidence_experiment_ids != set(self.experiment_ids):
            raise ValueError("critic-unavailable evidence differs from its experiment batch")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class RuntimeFailureAuthorityV5:
    """Sanitized durable primary failure recorded before owned cleanup."""

    outcome: Literal["runtime_failed"]
    stage: RoundFailureStageV5
    failure_code: RoundFailureCodeV5
    role: RoleNameV5 | None = None
    experiment_id: str | None = None

    def __post_init__(self) -> None:
        if self.outcome != "runtime_failed":
            raise ValueError("runtime-failure authority kind is invalid")
        if self.stage not in _ROUND_FAILURE_STAGES or self.failure_code not in _ROUND_FAILURE_CODES:
            raise ValueError("runtime-failure authority is outside the closed taxonomy")
        if self.role is not None and self.role not in {"investigator", "author", "critic"}:
            raise ValueError("runtime-failure role is invalid")
        if self.experiment_id is not None:
            _digest(self.experiment_id, "runtime-failure experiment")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


RoundOutcomeAuthorityV5 = (
    NoNovelHypothesisAuthorityV5
    | NoveltyExhaustedAuthorityV5
    | CriticUnavailableAuthorityV5
    | RuntimeFailureAuthorityV5
)


@dataclass(frozen=True, slots=True)
class RoundOutcomePayloadV5:
    """Authenticated terminal round fact that does not fabricate an experiment."""

    campaign_id: str
    round_index: int
    authority: RoundOutcomeAuthorityV5

    def __post_init__(self) -> None:
        _text(self.campaign_id, "round-outcome campaign ID")
        _count(self.round_index, "round-outcome round", positive=True)
        if type(self.authority) not in {
            NoNovelHypothesisAuthorityV5,
            NoveltyExhaustedAuthorityV5,
            CriticUnavailableAuthorityV5,
            RuntimeFailureAuthorityV5,
        }:
            raise ValueError("round-outcome authority is outside the closed V5 union")

    @property
    def outcome(self) -> RoundOutcomeKindV5:
        return self.authority.outcome

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


RoundEventPayloadV5 = (
    RoundIntentPayloadV5
    | RoleCompletionPayloadV5
    | RenderedVariantPayloadV5
    | CandidateStageResultPayloadV5
    | QuickEvidencePayloadV5
    | EpisodeEvidencePayloadV5
    | ResourceLeasePayloadV5
    | CleanupResultPayloadV5
    | RoundOutcomePayloadV5
)

_PAYLOAD_TYPES: dict[str, type[object]] = {
    "round_intent": RoundIntentPayloadV5,
    "role_completion": RoleCompletionPayloadV5,
    "rendered_variant": RenderedVariantPayloadV5,
    "candidate_stage_result": CandidateStageResultPayloadV5,
    "quick_evidence": QuickEvidencePayloadV5,
    "episode_evidence": EpisodeEvidencePayloadV5,
    "resource_lease": ResourceLeasePayloadV5,
    "cleanup_result": CleanupResultPayloadV5,
    "round_outcome": RoundOutcomePayloadV5,
}


def event_kind_for_payload_v5(payload: RoundEventPayloadV5) -> RoundEventKindV5:
    for event_kind, payload_type in _PAYLOAD_TYPES.items():
        if type(payload) is payload_type:
            return event_kind  # type: ignore[return-value]
    raise ValueError("round-event payload is outside the closed V5 union")


def round_event_payload_primitive_v5(
    payload: RoundEventPayloadV5,
) -> dict[str, object]:
    """Encode the closed payload union without losing strict literal types."""

    event_kind = event_kind_for_payload_v5(payload)
    if isinstance(payload, RoundIntentPayloadV5):
        body = canonical_primitive_v5(payload)
    elif isinstance(payload, RenderedVariantPayloadV5):
        body = {
            "variant": {
                "assignment": _assignment_artifact_primitive(payload.variant.assignment),
                "source_bundle": payload.variant.source_bundle.to_primitive(),
                "policy_revision": payload.variant.policy_revision.to_primitive(),
            }
        }
    elif isinstance(payload, CandidateStageResultPayloadV5):
        body = {
            "experiment_id": payload.experiment_id,
            "stage": payload.stage,
            "stage_index": payload.stage_index,
            "outcome": payload.outcome,
            "validation": canonical_primitive_v5(payload.validation),
            "semantic_fingerprint": (
                None if payload.semantic_fingerprint is None else payload.semantic_fingerprint.to_primitive()
            ),
            "failure_code": payload.failure_code,
            "failure_ref": None if payload.failure_ref is None else payload.failure_ref.to_primitive(),
            "episode_ordinal": payload.episode_ordinal,
        }
    elif isinstance(payload, QuickEvidencePayloadV5):
        body = {
            "experiment_id": payload.experiment_id,
            "semantic_fingerprint": payload.semantic_fingerprint.to_primitive(),
            "evaluation": canonical_primitive_v5(payload.evaluation),
        }
    elif isinstance(payload, EpisodeEvidencePayloadV5):
        body = {
            "experiment_id": payload.experiment_id,
            "episode": canonical_primitive_v5(payload.episode),
        }
    else:
        body = canonical_primitive_v5(payload)
    if not isinstance(body, dict):
        raise ValueError("round-event payload primitive is invalid")
    return {
        "schema_version": 5,
        "payload_kind": event_kind,
        "payload": body,
    }


@dataclass(frozen=True, slots=True)
class RoundEventV5:
    campaign_id: str
    round_index: int
    sequence: int
    prior_event_sha256: str | None
    event_kind: RoundEventKindV5
    experiment_id: str | None
    payload_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        _text(self.campaign_id, "round-event campaign ID")
        _count(self.round_index, "round-event round", positive=True)
        _count(self.sequence, "round-event sequence")
        if self.sequence == 0:
            if self.prior_event_sha256 is not None:
                raise ValueError("first round event cannot name a predecessor")
        else:
            _digest(self.prior_event_sha256, "prior round-event SHA-256")
        if self.event_kind not in _EVENT_KINDS:
            raise ValueError("round-event kind is invalid")
        if self.experiment_id is not None:
            _digest(self.experiment_id, "round-event experiment ID")
        if self.event_kind in {
            "rendered_variant",
            "candidate_stage_result",
            "quick_evidence",
            "episode_evidence",
        }:
            if self.experiment_id is None:
                raise ValueError("experiment-local event requires an experiment ID")
        elif self.event_kind in {
            "round_intent",
            "role_completion",
            "resource_lease",
            "cleanup_result",
            "round_outcome",
        }:
            if self.experiment_id is not None:
                raise ValueError("campaign-level event cannot name an experiment")
        if type(self.payload_ref) is not ArtifactRefV5:
            raise ValueError("round-event payload reference is invalid")
        prefix = f"payloads/{self.event_kind}/"
        if not self.payload_ref.relative_path.startswith(prefix):
            raise ValueError("round-event payload path differs from its kind")

    def validate_payload(self, payload: RoundEventPayloadV5) -> None:
        if type(payload) is not _PAYLOAD_TYPES[self.event_kind]:
            raise ValueError("round-event kind differs from its decoded payload schema")
        if isinstance(payload, (CandidateStageResultPayloadV5, QuickEvidencePayloadV5, EpisodeEvidencePayloadV5)):
            if payload.experiment_id != self.experiment_id:
                raise ValueError("round-event experiment differs from its payload")
        if isinstance(payload, ResourceLeasePayloadV5):
            if payload.owner_campaign_id != self.campaign_id:
                raise ValueError("resource lease differs from its campaign")
        if isinstance(payload, RoleCompletionPayloadV5):
            if payload.campaign_id != self.campaign_id or payload.round_index != self.round_index:
                raise ValueError("role completion differs from its event authority")
        if isinstance(payload, RoundOutcomePayloadV5):
            if payload.campaign_id != self.campaign_id or payload.round_index != self.round_index:
                raise ValueError("round outcome differs from its event authority")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": 5,
            "campaign_id": self.campaign_id,
            "round_index": self.round_index,
            "sequence": self.sequence,
            "prior_event_sha256": self.prior_event_sha256,
            "event_kind": self.event_kind,
            "experiment_id": self.experiment_id,
            "payload_ref": self.payload_ref.to_primitive(),
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self.to_primitive())


@dataclass(frozen=True, slots=True)
class RecoveryStepV5:
    event_kind: RoundEventKindV5
    experiment_id: str | None = None
    episode_ordinal: int | None = None
    resource_kind: str | None = None
    resource_lease_id: str | None = None
    role: RoleNameV5 | None = None
    role_position: int | None = None

    def __post_init__(self) -> None:
        if self.event_kind not in _EVENT_KINDS:
            raise ValueError("recovery step kind is invalid")
        if self.experiment_id is not None:
            _digest(self.experiment_id, "recovery experiment ID")
        if self.event_kind == "episode_evidence":
            _count(self.episode_ordinal, "recovery episode ordinal", positive=True)
        elif self.episode_ordinal is not None:
            raise ValueError("only episode recovery steps carry an ordinal")
        if self.event_kind == "resource_lease":
            if self.resource_kind not in {
                "workspace",
                "policy_worker",
                "evaluator_process",
                "container",
            }:
                raise ValueError("resource recovery step kind is invalid")
            _text(self.resource_lease_id, "resource recovery lease ID")
        elif self.resource_kind is not None or self.resource_lease_id is not None:
            raise ValueError("only resource recovery steps carry lease bindings")
        if self.event_kind == "role_completion":
            expected_position = {"investigator": 1, "author": 2, "critic": 3}.get(self.role)
            if type(self.role_position) is not int or self.role_position != expected_position:
                raise ValueError("role recovery step differs from its fixed round position")
        elif self.role is not None or self.role_position is not None:
            raise ValueError("only role recovery steps carry role bindings")
        if self.event_kind in {
            "rendered_variant",
            "candidate_stage_result",
            "quick_evidence",
            "episode_evidence",
        }:
            if self.experiment_id is None:
                raise ValueError("experiment recovery step requires an experiment ID")
        elif self.experiment_id is not None:
            raise ValueError("campaign recovery step cannot name an experiment")


@dataclass(frozen=True, slots=True)
class RoundRecoveryV5:
    events: tuple[RoundEventV5, ...]
    payloads: tuple[RoundEventPayloadV5, ...]
    missing_steps: tuple[RecoveryStepV5, ...]

    def __post_init__(self) -> None:
        if len(self.events) != len(self.payloads):
            raise ValueError("recovery events and payloads differ")
        for event, payload in zip(self.events, self.payloads, strict=True):
            event.validate_payload(payload)
        if type(self.missing_steps) is not tuple or any(
            type(item) is not RecoveryStepV5 for item in self.missing_steps
        ):
            raise ValueError("recovery missing steps are invalid")
        terminal_positions = tuple(
            index for index, payload in enumerate(self.payloads) if isinstance(payload, RoundOutcomePayloadV5)
        )
        if len(terminal_positions) > 1:
            raise ValueError("round recovery contains multiple terminal outcomes")
        if terminal_positions and any(
            not isinstance(payload, CleanupResultPayloadV5) for payload in self.payloads[terminal_positions[0] + 1 :]
        ):
            raise ValueError("only cleanup may follow a primary round outcome")
        if terminal_positions and self.missing_steps:
            raise ValueError("terminal round recovery cannot schedule more local steps")

    @property
    def terminal_outcome(self) -> RoundOutcomePayloadV5 | None:
        return next((item for item in self.payloads if isinstance(item, RoundOutcomePayloadV5)), None)


def _validate_candidate_stage_chain_v5(
    events: tuple[RoundEventV5, ...],
    payloads: tuple[RoundEventPayloadV5, ...],
) -> None:
    states: dict[str, str] = {}
    fingerprints: dict[str, SemanticFingerprintV5] = {}
    episode_ordinals: dict[str, set[int]] = {}
    critic_complete = False
    for event, payload in zip(events, payloads, strict=True):
        if isinstance(payload, RoleCompletionPayloadV5) and payload.role == "critic":
            critic_complete = True
            continue
        if (
            isinstance(
                payload,
                (
                    RenderedVariantPayloadV5,
                    CandidateStageResultPayloadV5,
                    QuickEvidencePayloadV5,
                    EpisodeEvidencePayloadV5,
                ),
            )
            and critic_complete
        ):
            raise ValueError("candidate work cannot follow critic completion")
        experiment_id = event.experiment_id
        if isinstance(payload, RenderedVariantPayloadV5):
            assert experiment_id is not None
            if experiment_id in states:
                raise ValueError("rendered candidate is duplicated")
            states[experiment_id] = "rendered"
            episode_ordinals[experiment_id] = set()
            continue
        if isinstance(payload, CandidateStageResultPayloadV5):
            assert experiment_id is not None
            state = states.get(experiment_id)
            if payload.stage == "validation":
                if state != "rendered":
                    raise ValueError("candidate validation is duplicated or out of order")
                states[experiment_id] = "validated" if payload.outcome == "validation_valid" else "terminal"
            elif payload.stage == "semantic_probe":
                if state != "validated":
                    raise ValueError("candidate semantic result is duplicated or out of order")
                if payload.outcome == "behaviorally_distinct":
                    assert payload.semantic_fingerprint is not None
                    fingerprints[experiment_id] = payload.semantic_fingerprint
                    states[experiment_id] = "distinct"
                else:
                    states[experiment_id] = "terminal"
            elif payload.stage == "quick_evaluation":
                if state != "distinct":
                    raise ValueError("candidate quick failure is duplicated or out of order")
                states[experiment_id] = "terminal"
            elif payload.stage == "discovery_evaluation":
                if state not in {"quick", "episodes"}:
                    raise ValueError("candidate discovery failure is duplicated or out of order")
                assert payload.episode_ordinal is not None
                if payload.episode_ordinal in episode_ordinals[experiment_id]:
                    raise ValueError("candidate discovery failure duplicates completed evidence")
                states[experiment_id] = "terminal"
            continue
        if isinstance(payload, QuickEvidencePayloadV5):
            assert experiment_id is not None
            if states.get(experiment_id) != "distinct":
                raise ValueError("candidate quick evidence is duplicated or out of order")
            if payload.semantic_fingerprint != fingerprints.get(experiment_id):
                raise ValueError("candidate quick evidence differs from its durable semantic result")
            states[experiment_id] = "quick"
            continue
        if isinstance(payload, EpisodeEvidencePayloadV5):
            assert experiment_id is not None
            if states.get(experiment_id) not in {"quick", "episodes"}:
                raise ValueError("candidate episode evidence is duplicated or out of order")
            ordinal = payload.episode.episode_ordinal
            if ordinal in episode_ordinals[experiment_id] or len(episode_ordinals[experiment_id]) >= 4:
                raise ValueError("candidate episode evidence is duplicated or exceeds the fixed panel")
            episode_ordinals[experiment_id].add(ordinal)
            states[experiment_id] = "episodes"


def fold_round_events_v5(
    *,
    events: tuple[RoundEventV5, ...],
    payloads: tuple[RoundEventPayloadV5, ...],
    expected_steps: tuple[RecoveryStepV5, ...] = (),
) -> RoundRecoveryV5:
    """Authenticate one contiguous chain and return only absent local steps."""

    if type(events) is not tuple or type(payloads) is not tuple:
        raise ValueError("round recovery requires immutable event and payload tuples")
    if len(events) != len(payloads):
        raise ValueError("round recovery events and payloads differ")
    if events:
        campaign_id = events[0].campaign_id
        round_index = events[0].round_index
        for position, (event, payload) in enumerate(zip(events, payloads, strict=True)):
            if type(event) is not RoundEventV5:
                raise ValueError("round recovery event is invalid")
            if event.campaign_id != campaign_id or event.round_index != round_index:
                raise ValueError("round recovery stream spans campaigns or rounds")
            if event.sequence != position:
                raise ValueError("round recovery stream has a sequence gap")
            expected_prior = None if position == 0 else events[position - 1].sha256
            if event.prior_event_sha256 != expected_prior:
                raise ValueError("round recovery stream has a broken predecessor link")
            event.validate_payload(payload)
    if type(expected_steps) is not tuple or any(type(item) is not RecoveryStepV5 for item in expected_steps):
        raise ValueError("expected recovery steps are invalid")
    if len(set(expected_steps)) != len(expected_steps):
        raise ValueError("expected recovery steps must be unique")

    terminal_positions = tuple(
        index for index, payload in enumerate(payloads) if isinstance(payload, RoundOutcomePayloadV5)
    )
    if len(terminal_positions) > 1:
        raise ValueError("round recovery contains multiple primary outcomes")
    terminal = None if not terminal_positions else payloads[terminal_positions[0]]
    if terminal_positions:
        terminal_position = terminal_positions[0]
        if any(isinstance(item, CleanupResultPayloadV5) for item in payloads[:terminal_position]):
            raise ValueError("cleanup cannot precede a primary round outcome")
        if any(not isinstance(item, CleanupResultPayloadV5) for item in payloads[terminal_position + 1 :]):
            raise ValueError("only cleanup may follow a primary round outcome")

    cleanup_positions = tuple(
        index for index, payload in enumerate(payloads) if isinstance(payload, CleanupResultPayloadV5)
    )
    complete_cleanup_positions = tuple(
        index
        for index in cleanup_positions
        if isinstance(payloads[index], CleanupResultPayloadV5) and payloads[index].cleanup_complete
    )
    if len(complete_cleanup_positions) > 1 or (
        complete_cleanup_positions and complete_cleanup_positions[0] != len(payloads) - 1
    ):
        raise ValueError("complete cleanup must close the durable round chain")

    role_completions = tuple(payload for payload in payloads if isinstance(payload, RoleCompletionPayloadV5))
    if tuple(item.role_position for item in role_completions) != tuple(range(1, len(role_completions) + 1)):
        raise ValueError("durable role completions are not the canonical round prefix")
    _validate_candidate_stage_chain_v5(events, payloads)

    completed: set[RecoveryStepV5] = set()
    for event, payload in zip(events, payloads, strict=True):
        episode_ordinal = payload.episode.episode_ordinal if isinstance(payload, EpisodeEvidencePayloadV5) else None
        completed.add(
            RecoveryStepV5(
                event_kind=event.event_kind,
                experiment_id=event.experiment_id,
                episode_ordinal=episode_ordinal,
                resource_kind=(payload.resource_kind if isinstance(payload, ResourceLeasePayloadV5) else None),
                resource_lease_id=(payload.lease_id if isinstance(payload, ResourceLeasePayloadV5) else None),
                role=(payload.role if isinstance(payload, RoleCompletionPayloadV5) else None),
                role_position=(payload.role_position if isinstance(payload, RoleCompletionPayloadV5) else None),
            )
        )
    return RoundRecoveryV5(
        events=events,
        payloads=payloads,
        missing_steps=(() if terminal is not None else tuple(step for step in expected_steps if step not in completed)),
    )


StateT = TypeVar("StateT")


class ArchiveReducerV5(Protocol[StateT]):
    """Task-6-owned archive semantics injected into journal reconstruction."""

    def initial(self) -> StateT: ...

    def apply(self, state: StateT, record: ExperimentRecordV5) -> StateT: ...

    def to_primitive(self, state: StateT) -> object: ...


def reduce_experiment_journal_v5(records: tuple[ExperimentRecordV5, ...], reducer: ArchiveReducerV5[StateT]) -> StateT:
    """Fold final records without embedding a second archive policy in memory."""

    if type(records) is not tuple or any(type(item) is not ExperimentRecordV5 for item in records):
        raise ValueError("experiment journal records are invalid")
    state = reducer.initial()
    for record in sorted(records, key=lambda item: (item.round_index, item.experiment_id)):
        state = reducer.apply(state, record)
    return state


@dataclass(frozen=True, slots=True)
class StoredExperimentRecordV5:
    reference: ArtifactRefV5
    record: ExperimentRecordV5

    def __post_init__(self) -> None:
        if type(self.reference) is not ArtifactRefV5 or type(self.record) is not ExperimentRecordV5:
            raise ValueError("stored experiment record is invalid")
        if self.reference.sha256 != self.record.sha256:
            raise ValueError("stored experiment reference differs from its record")
        if self.reference.relative_path != f"records/{self.record.experiment_id}.json":
            raise ValueError("stored experiment path differs from its identity")


@dataclass(frozen=True, slots=True)
class ExperimentFeedbackV5:
    record_ref: ArtifactRefV5
    record_sha256: str
    experiment_id: str
    round_index: int
    parent_revision_sha256: str
    policy_revision_sha256: str | None
    primary_mechanism: str
    hypothesis: HypothesisV5
    status: ExperimentStatusV5
    validation: ValidationResultV5
    semantic_fingerprint_sha256: str | None
    quick_evidence_sha256: str | None
    campaign_evidence_sha256: str | None
    target_gap_pct: Decimal | None
    critic_review: CriticReviewV5 | None
    critic_artifact_ref: ArtifactRefV5 | None

    def __post_init__(self) -> None:
        if type(self.record_ref) is not ArtifactRefV5:
            raise ValueError("complete feedback record reference is invalid")
        _digest(self.record_sha256, "complete feedback record SHA-256")
        _digest(self.experiment_id, "complete feedback experiment ID")
        if self.record_ref.sha256 != self.record_sha256:
            raise ValueError("complete feedback record digest differs")
        _count(self.round_index, "complete feedback round", positive=True)
        _digest(self.parent_revision_sha256, "complete feedback parent revision")
        if self.policy_revision_sha256 is not None:
            _digest(self.policy_revision_sha256, "complete feedback policy revision")
        _text(self.primary_mechanism, "complete feedback mechanism")
        if type(self.hypothesis) is not HypothesisV5 or self.hypothesis.primary_mechanism != self.primary_mechanism:
            raise ValueError("complete feedback hypothesis binding is invalid")
        if self.status not in _STATUSES or type(self.validation) is not ValidationResultV5:
            raise ValueError("complete feedback status or validation is invalid")
        for digest, label in (
            (self.semantic_fingerprint_sha256, "complete feedback fingerprint"),
            (self.quick_evidence_sha256, "complete feedback quick evidence"),
            (self.campaign_evidence_sha256, "complete feedback campaign evidence"),
        ):
            if digest is not None:
                _digest(digest, label)
        _optional_decimal(self.target_gap_pct, "complete feedback target gap")
        if self.critic_review is not None and (
            type(self.critic_review) is not CriticReviewV5 or self.critic_review.experiment_id != self.experiment_id
        ):
            raise ValueError("complete feedback critic review is invalid")
        if (self.critic_review is None) != (self.critic_artifact_ref is None):
            raise ValueError("complete feedback critic bindings differ")
        if is_testable_experiment_status_v5(self.status):
            if self.critic_review is None:
                raise ValueError("testable complete feedback requires critic fields")
        elif self.critic_review is not None:
            raise ValueError("untestable complete feedback cannot carry critic fields")

    @classmethod
    def from_stored(cls, stored: StoredExperimentRecordV5) -> "ExperimentFeedbackV5":
        record = stored.record
        return cls(
            record_ref=stored.reference,
            record_sha256=record.sha256,
            experiment_id=record.experiment_id,
            round_index=record.round_index,
            parent_revision_sha256=record.parent_revision_sha256,
            policy_revision_sha256=(None if record.policy_revision is None else record.policy_revision.sha256),
            primary_mechanism=record.hypothesis.primary_mechanism,
            hypothesis=record.hypothesis,
            status=record.status,
            validation=record.validation,
            semantic_fingerprint_sha256=(
                None if record.semantic_fingerprint is None else record.semantic_fingerprint.fingerprint_sha256
            ),
            quick_evidence_sha256=(None if record.quick_evidence is None else record.quick_evidence.sha256),
            campaign_evidence_sha256=(
                None if record.campaign_evidence is None else canonical_sha256_v5(record.campaign_evidence)
            ),
            target_gap_pct=record.target_gap_pct,
            critic_review=record.critic_review,
            critic_artifact_ref=record.critic_artifact_ref,
        )


@dataclass(frozen=True, slots=True)
class ExperimentMemorySummaryV5:
    record_ref: ArtifactRefV5
    record_sha256: str
    experiment_id: str
    round_index: int
    parent_revision_sha256: str
    policy_revision_sha256: str | None
    primary_mechanism: str
    hypothesis_sha256: str
    status: ExperimentStatusV5
    target_gap_pct: Decimal | None
    critic_review_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.record_ref) is not ArtifactRefV5:
            raise ValueError("memory summary record reference is invalid")
        for digest, label in (
            (self.record_sha256, "memory summary record"),
            (self.experiment_id, "memory summary experiment"),
            (self.parent_revision_sha256, "memory summary parent"),
            (self.hypothesis_sha256, "memory summary hypothesis"),
        ):
            _digest(digest, label)
        if self.record_ref.sha256 != self.record_sha256:
            raise ValueError("memory summary record digest differs")
        _count(self.round_index, "memory summary round", positive=True)
        if self.policy_revision_sha256 is not None:
            _digest(self.policy_revision_sha256, "memory summary policy revision")
        _text(self.primary_mechanism, "memory summary mechanism")
        if self.status not in _STATUSES:
            raise ValueError("memory summary status is invalid")
        _optional_decimal(self.target_gap_pct, "memory summary target gap")
        if self.critic_review_sha256 is not None:
            _digest(self.critic_review_sha256, "memory summary critic review")
        if is_testable_experiment_status_v5(self.status) != (self.critic_review_sha256 is not None):
            raise ValueError("memory summary critic digest differs from status testability")

    @classmethod
    def from_stored(cls, stored: StoredExperimentRecordV5) -> "ExperimentMemorySummaryV5":
        record = stored.record
        return cls(
            record_ref=stored.reference,
            record_sha256=record.sha256,
            experiment_id=record.experiment_id,
            round_index=record.round_index,
            parent_revision_sha256=record.parent_revision_sha256,
            policy_revision_sha256=(None if record.policy_revision is None else record.policy_revision.sha256),
            primary_mechanism=record.hypothesis.primary_mechanism,
            hypothesis_sha256=record.hypothesis.sha256,
            status=record.status,
            target_gap_pct=record.target_gap_pct,
            critic_review_sha256=(None if record.critic_review is None else record.critic_review.sha256),
        )


@dataclass(frozen=True, slots=True)
class InvestigatorMemoryProjectionV5:
    selected_parent_revision_sha256: str
    relevant_mechanism: str
    complete_feedback: tuple[ExperimentFeedbackV5, ...]
    summaries: tuple[ExperimentMemorySummaryV5, ...]

    def __post_init__(self) -> None:
        _digest(self.selected_parent_revision_sha256, "projection selected parent")
        _text(self.relevant_mechanism, "projection relevant mechanism")
        if type(self.complete_feedback) is not tuple or any(
            type(item) is not ExperimentFeedbackV5 for item in self.complete_feedback
        ):
            raise ValueError("projection complete feedback is invalid")
        if type(self.summaries) is not tuple or any(
            type(item) is not ExperimentMemorySummaryV5 for item in self.summaries
        ):
            raise ValueError("projection summaries are invalid")
        ids = tuple(item.experiment_id for item in (*self.complete_feedback, *self.summaries))
        if len(set(ids)) != len(ids):
            raise ValueError("projection experiments must be unique")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": 5,
            "selected_parent_revision_sha256": self.selected_parent_revision_sha256,
            "relevant_mechanism": self.relevant_mechanism,
            "complete_feedback": canonical_primitive_v5(self.complete_feedback),
            "summaries": canonical_primitive_v5(self.summaries),
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())


class ProjectionBudgetTooSmallV5(ValueError):
    """The mandatory selected-parent lineage cannot fit the declared role budget."""


ProjectionLineageAmbiguityCodeV5 = Literal[
    "conflicting_parent_lineage",
    "conflicting_policy_identity",
    "lineage_cycle",
]


class ProjectionLineageAmbiguityV5(ValueError):
    """Selected-revision ancestry has no single authenticated interpretation."""

    def __init__(self, code: ProjectionLineageAmbiguityCodeV5, revision_sha256: str) -> None:
        if code not in {
            "conflicting_parent_lineage",
            "conflicting_policy_identity",
            "lineage_cycle",
        }:
            raise ValueError("projection lineage ambiguity code is invalid")
        self.code = code
        self.revision_sha256 = _digest(revision_sha256, "ambiguous lineage revision")
        super().__init__(f"selected-parent lineage is ambiguous: {code}")


class ProjectionLineageAuthorityV5(ValueError):
    """Selected-parent record is absent from or inconsistent with checkpoint authority."""


def _selected_parent_lineage_ids(
    stored_records: tuple[StoredExperimentRecordV5, ...],
    selected_parent_revision_sha256: str,
    selected_parent_experiment_id: str,
) -> tuple[str, ...]:
    by_revision: dict[str, list[StoredExperimentRecordV5]] = {}
    for item in stored_records:
        policy_revision = item.record.policy_revision
        if policy_revision is not None:
            by_revision.setdefault(policy_revision.sha256, []).append(item)

    lineage_groups: list[tuple[StoredExperimentRecordV5, ...]] = []
    seen_revisions: set[str] = set()
    revision = selected_parent_revision_sha256
    while revision in by_revision:
        if revision in seen_revisions:
            raise ProjectionLineageAmbiguityV5("lineage_cycle", revision)
        seen_revisions.add(revision)
        revision_records = tuple(by_revision[revision])
        producer_records = tuple(
            item
            for item in revision_records
            if item.record.status != "exact_duplicate"
            and item.record.policy_revision is not None
            and item.record.policy_revision.sha256 != item.record.parent_revision_sha256
        )
        if not producer_records:
            break

        expected_identity = producer_records[0].record.policy_revision
        if any(item.record.policy_revision != expected_identity for item in producer_records[1:]):
            raise ProjectionLineageAmbiguityV5("conflicting_policy_identity", revision)
        parents = {item.record.parent_revision_sha256 for item in producer_records}
        if len(parents) != 1:
            raise ProjectionLineageAmbiguityV5("conflicting_parent_lineage", revision)

        # A terminal evaluated record can anchor an archive-selected revision. Other
        # attempts on the same bytes remain complete feedback, but cannot advance
        # ancestry on their own.
        if not any(item.record.status == "evaluated" for item in producer_records):
            break
        if not lineage_groups and all(
            item.record.experiment_id != selected_parent_experiment_id for item in producer_records
        ):
            raise ProjectionLineageAuthorityV5("selected-parent record is not a producer of its revision")
        lineage_groups.append(
            tuple(sorted(revision_records, key=lambda item: (item.record.round_index, item.record.experiment_id)))
        )
        revision = next(iter(parents))

    return tuple(item.record.experiment_id for group in reversed(lineage_groups) for item in group)


def project_investigator_memory_v5(
    *,
    stored_records: tuple[StoredExperimentRecordV5, ...],
    selected_parent_revision_sha256: str,
    selected_parent_record_ref: ArtifactRefV5 | None,
    relevant_mechanism: str,
    maximum_bytes: int,
) -> InvestigatorMemoryProjectionV5:
    """Build a bounded view from checkpoint-authorized records without truncating feedback."""

    if type(stored_records) is not tuple or any(type(item) is not StoredExperimentRecordV5 for item in stored_records):
        raise ValueError("projection stored records are invalid")
    _digest(selected_parent_revision_sha256, "projection selected parent")
    _text(relevant_mechanism, "projection relevant mechanism")
    _count(maximum_bytes, "projection byte budget", positive=True)
    ids = tuple(item.record.experiment_id for item in stored_records)
    if len(set(ids)) != len(ids):
        raise ValueError("projection stored experiments must be unique")
    canonical_records = tuple(
        sorted(stored_records, key=lambda item: (item.record.round_index, item.record.experiment_id))
    )
    if selected_parent_record_ref is None:
        lineage_id_order: tuple[str, ...] = ()
    else:
        if type(selected_parent_record_ref) is not ArtifactRefV5:
            raise ValueError("projection selected-parent record reference is invalid")
        authoritative = tuple(item for item in canonical_records if item.reference == selected_parent_record_ref)
        if len(authoritative) != 1:
            raise ProjectionLineageAuthorityV5("selected-parent record is not checkpoint-authorized")
        selected_record = authoritative[0].record
        if (
            selected_record.status != "evaluated"
            or selected_record.policy_revision is None
            or selected_record.policy_revision.sha256 != selected_parent_revision_sha256
            or selected_record.policy_revision.sha256 == selected_record.parent_revision_sha256
        ):
            raise ProjectionLineageAuthorityV5("selected-parent record does not bind an evaluated revision")
        lineage_id_order = _selected_parent_lineage_ids(
            canonical_records,
            selected_parent_revision_sha256,
            selected_record.experiment_id,
        )
    lineage_ids = set(lineage_id_order)
    records_by_id = {item.record.experiment_id: item for item in canonical_records}
    lineage = tuple(records_by_id[experiment_id] for experiment_id in lineage_id_order)
    relevant = tuple(
        item
        for item in canonical_records
        if item.record.experiment_id not in lineage_ids
        and item.record.hypothesis.primary_mechanism == relevant_mechanism
    )
    remaining = tuple(
        item
        for item in canonical_records
        if item.record.experiment_id not in lineage_ids
        and item.record.hypothesis.primary_mechanism != relevant_mechanism
    )

    complete: list[ExperimentFeedbackV5] = [ExperimentFeedbackV5.from_stored(item) for item in lineage]
    summaries: list[ExperimentMemorySummaryV5] = []

    def build() -> InvestigatorMemoryProjectionV5:
        return InvestigatorMemoryProjectionV5(
            selected_parent_revision_sha256=selected_parent_revision_sha256,
            relevant_mechanism=relevant_mechanism,
            complete_feedback=tuple(complete),
            summaries=tuple(summaries),
        )

    if len(build().canonical_json_bytes()) > maximum_bytes:
        raise ProjectionBudgetTooSmallV5("selected-parent lineage exceeds the investigator byte budget")
    for item in relevant:
        feedback = ExperimentFeedbackV5.from_stored(item)
        complete.append(feedback)
        if len(build().canonical_json_bytes()) > maximum_bytes:
            complete.pop()
            summary = ExperimentMemorySummaryV5.from_stored(item)
            summaries.append(summary)
            if len(build().canonical_json_bytes()) > maximum_bytes:
                summaries.pop()
    for item in remaining:
        summary = ExperimentMemorySummaryV5.from_stored(item)
        summaries.append(summary)
        if len(build().canonical_json_bytes()) > maximum_bytes:
            summaries.pop()
    return build()


__all__ = [
    "ArchiveReducerV5",
    "CandidateStageFailureCodeV5",
    "CandidateStageOutcomeV5",
    "CandidateStageResultPayloadV5",
    "CandidateStageV5",
    "CleanupResultPayloadV5",
    "CriticUnavailableAuthorityV5",
    "EpisodeEvidencePayloadV5",
    "ExperimentFeedbackV5",
    "ExperimentIdentityLikeV5",
    "ExperimentMemorySummaryV5",
    "ExperimentRecordV5",
    "ExperimentStatusV5",
    "InvestigatorMemoryProjectionV5",
    "NoNovelHypothesisAuthorityV5",
    "NoveltyExhaustedAuthorityV5",
    "PreCriticEvidenceIdentityV5",
    "PreCriticEvidenceKindV5",
    "ProjectionBudgetTooSmallV5",
    "ProjectionLineageAmbiguityCodeV5",
    "ProjectionLineageAmbiguityV5",
    "ProjectionLineageAuthorityV5",
    "QuickEvidencePayloadV5",
    "RecoveryStepV5",
    "RenderedVariantPayloadV5",
    "ResourceLeasePayloadV5",
    "RoleCompletionPayloadV5",
    "RoundEventKindV5",
    "RoundEventPayloadV5",
    "RoundEventV5",
    "RoundIntentPayloadV5",
    "RoundOutcomeKindV5",
    "RoundOutcomeAuthorityV5",
    "RoundOutcomePayloadV5",
    "RuntimeFailureAuthorityV5",
    "RoundRecoveryV5",
    "StoredExperimentRecordV5",
    "event_kind_for_payload_v5",
    "fold_round_events_v5",
    "is_testable_experiment_status_v5",
    "project_investigator_memory_v5",
    "reduce_experiment_journal_v5",
    "round_event_payload_primitive_v5",
]
