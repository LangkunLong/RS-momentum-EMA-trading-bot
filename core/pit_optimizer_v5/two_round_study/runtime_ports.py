"""Provider-free V5 runtime composition for the two-round study fixture.

This module is intentionally a thin composition layer.  The legacy runtime,
role factories, renderer, registry and mechanism sidecar remain authoritative;
the classes below only bind those ports to the study's authenticated fixture
and to the ordinary V5 role protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hashlib
import inspect
import json
import time

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    LiteralAxisV5,
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    StructuralTemplateV5,
    derive_changed_symbols_v5,
)
from core.pit_optimizer_v5.contracts import (
    CampaignEvidenceV5,
    CampaignManifestV5,
    CriticArtifactV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluationReportV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
    PanelEvaluationV5,
    ScenarioPanelEvaluationV5,
    ValidationResultV5,
    canonical_sha256_v5,
    canonical_json_bytes_v5,
    selected_scenario,
)
from core.pit_optimizer_v5.manifest import AuthenticatedCampaignManifestV5
from core.pit_optimizer_v5.mechanism_artifacts import (
    MECHANISM_ARTIFACT_NAMESPACE_V1,
    MechanismArtifactRepositoryV5,
    MechanismBoundCandidateV1,
    MechanismCapabilityError,
    MechanismExtensionCapabilityV1,
    MechanismPrecommitmentIndexV1,
    MechanismRuntimeExtensionV1,
    MechanismWorkerFactoryV1,
    explicit_mechanism_fixture_opt_in_v1,
    mechanism_extension_source_sha256_v1,
    manifest_source_identity_sha256_v1,
    round_intent_sha256_v1,
)
from core.pit_optimizer_v5.mechanism_contracts import (
    MECHANISM_ALLOWED_SYMBOLS_V1,
    MechanismControlV1,
    MechanismDisconfirmingObservationV1,
    MechanismEvidenceReportV1,
    MechanismExperimentSpecV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismObservationRunV1,
    MechanismWorkerPortV1,
    MechanismWorkerRegistrationV1,
    SyntheticFixtureWorkerV1,
    build_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.memory import (
    CleanupResultPayloadV5,
    NoNovelHypothesisAuthorityV5,
    NoveltyExhaustedAuthorityV5,
    ResourceLeasePayloadV5,
    RoundOutcomePayloadV5,
    RoleCompletionPayloadV5,
    RuntimeFailureAuthorityV5,
    RoundIntentPayloadV5,
    StoredExperimentRecordV5,
    is_testable_experiment_status_v5,
)
from core.pit_optimizer_v5.probes import fingerprint_policy_client_v5
from core.pit_optimizer_v5.production_runtime import (
    CanonicalExperimentRecordFactoryV5,
    ControllerCancellationV5,
    LocalArchiveReducerFactoryV5,
    LocalRoleRequestFactoryV5,
    MechanismRoleRequestAdapterV1,
    SelectionNoveltyResolverV5,
    SystemMonotonicClockV5,
)
from core.pit_optimizer_v5.provider import (
    AuthorRoleInputV5,
    ExistingPersistedRoleRequestV5,
    FixtureRoleRunnerV5,
    FixtureRoleTerminalAuthorityV5,
    FreshPersistedRoleRequestV5,
    MechanismRoleInputV1,
    RecoverableRoleInvokerV5,
    RoleInvocationPackageV5,
    RoleRequestV5,
    parsed_role_artifact_primitive_v5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundDependenciesV5,
    FeedbackRoundInputV5,
    MaterializedVariantV5,
    OwnedLeaseV5,
    StageDeadlineV5,
)
from core.pit_optimizer_v5.search import (
    ParentCandidateV5,
    archive_parent_from_record_v5,
    baseline_parent_candidate_v5,
)
from core.pit_optimizer_v5.selection import select_parent_v5
from core.pit_optimizer_v5.two_round_study.compiler import (
    CompiledStudyExperimentV1,
    StudyCommitmentIndexV1,
    StudyDraftBindingV1,
    compile_study_experiment_v1,
)
from core.pit_optimizer_v5.two_round_study.contracts import StudyArmV1
from core.pit_optimizer_v5.two_round_study.contrast import StudyContrastV1
from core.pit_optimizer_v5.two_round_study.fixtures import StudyFixtureV1
from core.pit_optimizer_v5.two_round_study.imports import (
    StudyImportInvokerV1,
    StudyImportV1,
    verify_imported_package_v1,
)
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
from core.pit_optimizer_v5.two_round_study.registry import (
    FrozenBehaviorRegistryV1,
    RegistryConfigurationV1,
    registry_client_v1,
    registry_decision_v1,
    reviewed_source_template_v1,
    verify_registry_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1


_AXIS_BY_CONFIGURATION = {
    "P0": 0,
    "A": 1,
    "S": 2,
    "S-inert": 3,
    "S-always-on": 4,
    "S-gte-0.05": 5,
    "S-gte-0.50": 6,
}
_MECHANISM_METRICS = (
    MechanismMetricSpecV1(
        metric_id="exit.decision_changed_count",
        unit="count",
        direction="increase",
        tolerance=Decimal("0"),
        denominator="relevant_cases",
    ),
    MechanismMetricSpecV1(
        metric_id="exit.protected_control_unchanged_count",
        unit="count",
        direction="unchanged",
        tolerance=Decimal("0"),
        denominator="control_cases",
    ),
)
_MECHANISM_CONTROL = MechanismControlV1(control_id="protected_next_stop_price")
_MECHANISM_RECIPE = MechanismRecipeV1(
    recipe_id="evaluate_exit_atr20_fraction_v1",
    input_field="features.atr_20_fraction",
    input_values=(Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None),
)
_MECHANISM_DISCONFIRMING = MechanismDisconfirmingObservationV1(
    observation_id="protected_control_changed",
    metric_id="exit.protected_control_unchanged_count",
)

_ACCEPTED_INVESTIGATOR_FAILURE_CODES_BY_STAGE = {
    "investigator": frozenset({"deadline_exceeded", "invalid_dependency_result"}),
    "novelty": frozenset({"cancelled", "deadline_exceeded", "invalid_dependency_result", "stage_failed"}),
}
_FIXTURE_ROLE_TERMINAL_ID = "study-runtime-v1"


@dataclass(frozen=True, slots=True)
class ScriptedResponseAuthorityV1:
    """Portable create-only identity for the deterministic fixture role logic."""

    schema_version: int
    campaign_id: str
    round_index: int
    manifest_sha256: str
    registry_sha256: str
    response_logic_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("scripted response authority schema is unsupported")
        if type(self.campaign_id) is not str or not self.campaign_id:
            raise ValueError("scripted response authority campaign is invalid")
        if type(self.round_index) is not int or self.round_index not in {1, 2}:
            raise ValueError("scripted response authority round is invalid")
        for value, label in (
            (self.manifest_sha256, "scripted response manifest"),
            (self.registry_sha256, "scripted response registry"),
            (self.response_logic_sha256, "scripted response logic"),
        ):
            if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{label} identity is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


_SCRIPTED_RESPONSE_FUNCTION_NAMES = (
    "_base_citations",
    "_author_template",
    "_investigator_artifact",
    "_critic_artifact",
    "scripted_response_v1",
    "parsed_role_artifact_primitive_v5",
    "canonical_json_bytes_v5",
)
_SCRIPTED_REGISTRY_FUNCTION_NAMES = (
    "reviewed_source_template_v1",
    "registry_client_v1",
    "registry_decision_v1",
    "verify_registry_v1",
)


def _source_identity(function: object) -> str:
    if not callable(function):
        raise ValueError("scripted response helper is not callable")
    try:
        source = inspect.getsource(function).replace("\r\n", "\n").encode("utf-8")
    except (OSError, TypeError, UnicodeError) as exc:
        raise ValueError("scripted response helper source is unavailable") from exc
    return hashlib.sha256(source).hexdigest()


def scripted_response_logic_sha256_v1(registry: FrozenBehaviorRegistryV1) -> str:
    """Hash the actual response helpers plus the frozen registry dependencies."""

    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("scripted response registry authority is invalid")
    components = {
        "response_helpers": {
            name: _source_identity(globals().get(name)) for name in _SCRIPTED_RESPONSE_FUNCTION_NAMES
        },
        "registry_helpers": {
            name: _source_identity(globals().get(name)) for name in _SCRIPTED_REGISTRY_FUNCTION_NAMES
        },
        "axis_by_configuration": _AXIS_BY_CONFIGURATION,
        "registry_sha256": registry.sha256,
    }
    return canonical_sha256_v5(components)


def scripted_response_authority_v1(
    *,
    manifest,
    registry: FrozenBehaviorRegistryV1,
    round_index: int,
) -> ScriptedResponseAuthorityV1:
    if round_index not in {1, 2} or type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("scripted response authority inputs are invalid")
    if type(manifest) is AuthenticatedCampaignManifestV5:
        manifest = manifest.manifest
    if type(manifest) is not CampaignManifestV5:
        raise ValueError("scripted response authority manifest is invalid")
    return ScriptedResponseAuthorityV1(
        schema_version=1,
        campaign_id=manifest.campaign_id,
        round_index=round_index,
        manifest_sha256=manifest.sha256,
        registry_sha256=registry.sha256,
        response_logic_sha256=scripted_response_logic_sha256_v1(registry),
    )


def _scripted_authority_reference(authority: ScriptedResponseAuthorityV1) -> ArtifactRefV5:
    key = f"{authority.campaign_id}-{authority.round_index}"
    raw = canonical_json_bytes_v5(authority)
    return ArtifactRefV5(
        f"adapter-state/study-scripted-role/{key}.json",
        hashlib.sha256(raw).hexdigest(),
    )


def verify_scripted_role_authority_v1(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest,
    registry: FrozenBehaviorRegistryV1,
    round_index: int,
) -> ArtifactRefV5:
    """Authenticate the immutable scripted source pin without creating bytes."""

    if type(repository) is not LocalArtifactRepositoryV5:
        raise ValueError("scripted response authority repository is invalid")
    authority = scripted_response_authority_v1(
        manifest=manifest,
        registry=registry,
        round_index=round_index,
    )
    reference = _scripted_authority_reference(authority)
    stored = repository.load_typed_state(
        namespace="study-scripted-role",
        key=f"{authority.campaign_id}-{authority.round_index}",
        value_type=ScriptedResponseAuthorityV1,
        repair=False,
    )
    if stored is None:
        raise ValueError("scripted response authority create-only pin is missing")
    if stored != authority:
        raise ValueError("scripted response authority differs from its create-only pin")
    return reference


def prepare_scripted_role_authority_v1(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest,
    registry: FrozenBehaviorRegistryV1,
    round_index: int,
) -> ArtifactRefV5:
    """Create the scripted source pin before a role request is journaled.

    This is the study-side boundary used by the controller preflight.  A slot
    is fresh only when neither authenticated role requests nor round events
    exist; historical recovery therefore cannot recreate an authority index.
    """

    if type(repository) is not LocalArtifactRepositoryV5:
        raise ValueError("scripted response authority repository is invalid")
    authority = scripted_response_authority_v1(
        manifest=manifest,
        registry=registry,
        round_index=round_index,
    )
    requests = repository.load_authenticated_role_requests(
        campaign_id=authority.campaign_id,
        round_index=authority.round_index,
    )
    events = repository.load_round_events(
        campaign_id=authority.campaign_id,
        round_index=authority.round_index,
    )
    if requests or events:
        raise ValueError("scripted response authority must be prepared before historical requests or events")
    try:
        return repository.append_typed_state(
            namespace="study-scripted-role",
            key=f"{authority.campaign_id}-{authority.round_index}",
            value=authority,
        )
    except ValueError as exc:
        raise ValueError("scripted response authority conflicts with its create-only pin") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _configuration_for_variant(
    registry: FrozenBehaviorRegistryV1,
    variant: RenderedVariantV5,
) -> RegistryConfigurationV1:
    matches = tuple(
        configuration
        for configuration in registry.configurations
        if configuration.source_bundle == variant.source_bundle
        and configuration.policy_revision == variant.policy_revision
    )
    if len(matches) != 1:
        raise ValueError("rendered candidate is outside the frozen registry")
    axis = dict(variant.assignment.values).get("configuration_axis")
    if axis != _AXIS_BY_CONFIGURATION[matches[0].configuration_id]:
        raise ValueError("rendered candidate assignment differs from its registry configuration")
    return matches[0]


def _configuration_for_source(
    registry: FrozenBehaviorRegistryV1,
    source: SourceBundleV5,
    revision: PolicyRevisionIdentityV5,
) -> RegistryConfigurationV1:
    matches = tuple(
        configuration
        for configuration in registry.configurations
        if configuration.source_bundle == source and configuration.policy_revision == revision
    )
    if len(matches) != 1:
        raise ValueError("source authority is outside the frozen registry")
    return matches[0]


def _deadline_check(deadline: StageDeadlineV5) -> None:
    if type(deadline) is not StageDeadlineV5 or time.monotonic() >= deadline.expires_at_monotonic:
        raise TimeoutError("study synthetic runtime deadline expired")


@dataclass(slots=True)
class StudyRequestRouterV1:
    """Select the arm's investigator/author factory and always augment critics."""

    arm: StudyArmV1
    base: LocalRoleRequestFactoryV5
    enabled: LocalRoleRequestFactoryV5

    def __post_init__(self) -> None:
        if self.arm not in {"primary", "withheld"}:
            raise ValueError("study arm is invalid")
        if type(self.base) is not LocalRoleRequestFactoryV5 or type(self.enabled) is not LocalRoleRequestFactoryV5:
            raise ValueError("study request router requires local role factories")
        if self.base.repository is not self.enabled.repository or self.base.manifest != self.enabled.manifest:
            raise ValueError("study role factories must share repository and manifest")
        if self.base.maximum_output_tokens != self.enabled.maximum_output_tokens:
            raise ValueError("study role factories must share output cap")
        if self.enabled.mechanism_adapter is None:
            raise ValueError("enabled study factory requires its mechanism adapter")

    def _arm_factory(self) -> LocalRoleRequestFactoryV5:
        return self.enabled if self.arm == "primary" else self.base

    def investigator_request(self, inputs, projection, parent):
        return self._arm_factory().investigator_request(inputs, projection, parent)

    def author_request(self, inputs, projection, decision, investigator):
        return self._arm_factory().author_request(inputs, projection, decision, investigator)

    def critic_request(self, inputs, projection, decision, candidates):
        return self.enabled.critic_request(inputs, projection, decision, candidates)

    def critic_request_with_mechanism(
        self,
        inputs,
        projection,
        decision,
        candidates,
        *,
        mechanism_evidence: tuple[object, ...],
    ):
        if type(mechanism_evidence) is not tuple or len(mechanism_evidence) != len(candidates):
            raise MechanismCapabilityError("study current critic bundle count differs")
        expected_authorization = explicit_mechanism_fixture_opt_in_v1(
            f"study-runtime:{self.arm}:{inputs.campaign_id}:{inputs.round_index}:{decision.hypothesis.sha256}"
        )
        for bundle in mechanism_evidence:
            if type(bundle) is not tuple or len(bundle) != 5:
                raise MechanismCapabilityError("study current critic bundle shape is invalid")
            capability, bound, run, report, intent = bundle
            if (
                type(capability) is not MechanismExtensionCapabilityV1
                or type(bound) is not MechanismBoundCandidateV1
                or type(run) is not MechanismObservationRunV1
                or type(report) is not MechanismEvidenceReportV1
                or type(intent) is not RoundIntentPayloadV5
                or capability.authorization != expected_authorization
                or capability.authenticated_manifest.manifest != inputs.manifest
                or capability.round_index != inputs.round_index
                or intent.hypothesis != decision.hypothesis
                or intent != capability.round_intent
                or bound.capability != capability
                or run.binding != bound.binding
                or report.binding != bound.binding
            ):
                raise MechanismCapabilityError("study current critic bundle is foreign to this arm and round")
        return self.enabled.critic_request_with_mechanism(
            inputs,
            projection,
            decision,
            candidates,
            mechanism_evidence=mechanism_evidence,
        )


def _base_citations(request: RoleRequestV5) -> tuple[str, ...]:
    role_input = request.role_input
    if isinstance(role_input, MechanismRoleInputV1):
        extension_ids = {
            evidence_id
            for projection in role_input.projections
            for row in projection.rows
            for evidence_id in row.evidence_ids
        }
        return tuple(item.evidence_id for item in request.role_evidence.items if item.evidence_id not in extension_ids)
    return tuple(item.evidence_id for item in request.role_evidence.items)


def _author_template(request: RoleRequestV5, selected: CompiledStudyExperimentV1 | None, round_index: int) -> StructuralTemplateV5:
    if not isinstance(request.role_input, AuthorRoleInputV5):
        raise ValueError("study scripted author requires ordinary AuthorRoleInputV5")
    template = reviewed_source_template_v1()
    hypothesis_id = request.expected_binding.hypothesis_id
    if hypothesis_id is None:
        raise ValueError("author request lacks the selected hypothesis")
    if round_index == 1:
        values = (1, 2)
        default = 1
    else:
        if selected is None:
            raise ValueError("round-two author has no authenticated compiled draft")
        value = _AXIS_BY_CONFIGURATION.get(selected.selected_configuration_id)
        if value is None:
            raise ValueError("compiled study selected an unknown configuration")
        values = (value,)
        default = value
    return replace(
        template,
        hypothesis_id=hypothesis_id,
        parent_revision_sha256=request.expected_binding.parent_revision_sha256,
        axes=(LiteralAxisV5("configuration_axis", default, values),),
    )


def _investigator_artifact(request: RoleRequestV5) -> InvestigatorArtifactV5:
    evidence_ids = tuple(item.evidence_id for item in request.role_evidence.items)
    if not evidence_ids:
        raise ValueError("study investigator request has no issued evidence")
    hypothesis = HypothesisV5(
        hypothesis_id="study-exit-v1",
        rank=1,
        primary_mechanism="exit",
        # Keep the frozen fixture claim explicit but compact: round-one
        # memory must retain S's full feedback and A's summary under the
        # production 3072-byte cap while A's full feedback still compacts.
        causal_claim="Exit threshold changes decisions; protected control holds.",
        predicted_changes=(
            MetricPredictionV5(
                "exit.decision_changed_count",
                "increase",
                "Change.",
            ),
            MetricPredictionV5(
                "exit.protected_control_unchanged_count",
                "unchanged",
                "Control holds.",
            ),
        ),
        evidence_ids=evidence_ids[:1],
        author_instructions="Bounded exit edit",
        authoring_mode="symbol_edits",
    )
    return InvestigatorArtifactV5((hypothesis,))


def _critic_artifact(request: RoleRequestV5, *, round_index: int) -> CriticArtifactV5:
    projection_by_experiment: dict[str, object] = {}
    if type(request.role_input) is MechanismRoleInputV1:
        base_ids = _base_citations(request)
        citations = tuple(item.evidence_id for item in request.role_evidence.items)
        if not citations:
            raise ValueError("study critic request has no issued evidence")
        # Round one intentionally omits supplemental mechanism findings in
        # both its prose and its citations. Round two can cite its own current
        # mechanism bundle after the fresh arm-specific precommitment.
        projection_by_experiment = {item.experiment_id: item for item in request.role_input.projections}
        evidence_ids = base_ids if round_index == 1 else citations
        explanation = (
            "Synthetic comparison."
            if round_index == 1
            else "Synthetic panels support the comparative result."
        )
    else:
        base_ids = _base_citations(request)
        evidence_ids = base_ids
        explanation = "The synthetic portfolio panels provide the declared comparative result."
    if not base_ids:
        base_ids = evidence_ids[:1]
    reviews = []
    local_summaries: list[str] = []
    for experiment_id in request.expected_binding.experiment_ids:
        review_evidence = tuple(base_ids[:1])
        review_explanation = explanation
        disposition = "promote"
        next_direction = (
            "Repeat."
            if round_index == 1
            else "Repeat the paired local check on the next bounded campaign."
        )
        if round_index == 2 and experiment_id in projection_by_experiment:
            projection = projection_by_experiment[experiment_id]
            mechanism_ids = tuple(projection.evidence_ids)
            if mechanism_ids:
                review_evidence = tuple((*base_ids[:1], mechanism_ids[0]))
            execution = projection.execution
            if execution.status != "completed":
                reason = execution.reason or execution.status
                local_summary = f"Local mechanism observation unavailable ({reason}); portfolio result is separate."
                disposition = "refine"
                next_direction = "Repeat the local mechanism check when observation is available."
            else:
                assessments = {row.prediction.assessment for row in projection.rows}
                if "contradicted_on_cases" in assessments:
                    local_summary = "Local mechanism observation contradicts the declared direction; portfolio result is separate."
                    disposition = "refine"
                    next_direction = "Refine the mechanism claim and repeat the bounded local check."
                elif assessments == {"supported_on_cases"}:
                    local_summary = "Local mechanism observation supports the declared direction; portfolio result is separate."
                else:
                    local_summary = "Local mechanism observation is inconclusive; portfolio result is separate."
                    disposition = "refine"
                    next_direction = "Repeat the bounded local check with sufficient coverage."
            review_explanation = local_summary
            local_summaries.append(local_summary)
        reviews.append(
            CriticReviewV5(
                experiment_id,
                "Synthetic result." if round_index == 1 else "Synthetic portfolio result is as supplied.",
                review_explanation,
                review_evidence,
                disposition,
                next_direction,
            )
        )
    if round_index == 2 and projection_by_experiment:
        explanation = "Synthetic portfolio panels provide the portfolio comparison; " + " ".join(local_summaries)
        evidence_ids = tuple(
            evidence_id
            for experiment_id in request.expected_binding.experiment_ids
            for evidence_id in tuple(projection_by_experiment[experiment_id].evidence_ids)[:1]
        ) or evidence_ids
    return CriticArtifactV5(
        tuple(reviews),
        explanation if round_index == 2 else "Synthetic comparison.",
        "Repeat." if round_index == 1 else "Repeat the paired local check on the next bounded campaign.",
        tuple(evidence_ids[:1]),
    )


def scripted_response_v1(
    *,
    request: RoleRequestV5,
    round_index: int,
    registry: FrozenBehaviorRegistryV1,
    selected: CompiledStudyExperimentV1 | None,
) -> str:
    """Return a deterministic ordinary role response bound to ``request``."""

    if type(request) is not RoleRequestV5 or round_index not in {1, 2}:
        raise ValueError("scripted study request is invalid")
    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("scripted study registry authority is invalid")
    verify_registry_v1(registry)
    if request.role == "investigator":
        if round_index == 2:
            raise ValueError("round-two investigator authority must come from the authenticated import")
        artifact = _investigator_artifact(request)
    elif request.role == "author":
        artifact = _author_template(request, selected, round_index)
    else:
        artifact = _critic_artifact(request, round_index=round_index)
    primitive = parsed_role_artifact_primitive_v5(artifact)["artifact"]
    return canonical_json_bytes_v5(
        {"binding": request.expected_binding.to_primitive(), "artifact": primitive}
    ).decode("utf-8")


class StudyScriptedRoleInvokerV1:
    """Crash-safe fixture role invoker with request-derived response bytes."""

    def __init__(
        self,
        manifest,
        registry: FrozenBehaviorRegistryV1,
        round_index: int,
        repository: LocalArtifactRepositoryV5,
        selected: CompiledStudyExperimentV1 | None = None,
    ) -> None:
        if type(registry) is not FrozenBehaviorRegistryV1 or round_index not in {1, 2}:
            raise ValueError("scripted study authority is invalid")
        if type(repository) is not LocalArtifactRepositoryV5:
            raise ValueError("scripted study authority repository is invalid")
        verify_registry_v1(registry)
        if manifest.provider is not None or manifest.pit_data_scope != "production":
            raise ValueError("scripted study roles require the provider-free fixture manifest")
        self.manifest = manifest
        self.registry = registry
        self.round_index = round_index
        self.repository = repository
        self.selected = selected
        self.response_authority = scripted_response_authority_v1(
            manifest=manifest,
            registry=registry,
            round_index=round_index,
        )
        self.response_authority_ref: ArtifactRefV5 = verify_scripted_role_authority_v1(
            repository=repository,
            manifest=manifest,
            registry=registry,
            round_index=round_index,
        )

    def set_selected(self, selected: CompiledStudyExperimentV1) -> None:
        if type(selected) is not CompiledStudyExperimentV1:
            raise ValueError("selected compiled study is invalid")
        self.selected = selected

    def _verify_response_authority(self) -> None:
        current = scripted_response_authority_v1(
            manifest=self.manifest,
            registry=self.registry,
            round_index=self.round_index,
        )
        stored = self.repository.load_typed_state(
            namespace="study-scripted-role",
            key=f"{current.campaign_id}-{current.round_index}",
            value_type=ScriptedResponseAuthorityV1,
            repair=False,
        )
        if stored is None or stored != current or stored != self.response_authority:
            raise ValueError("scripted response authority differs from its persisted identity")

    @staticmethod
    def _package(persisted, response: str) -> RoleInvocationPackageV5:
        runner = FixtureRoleRunnerV5(responses={persisted.call.role: (response,)})
        artifact = runner.invoke_once(persisted.request)
        attempt = runner.attempts[-1]
        if attempt.attempt_index != persisted.call.attempt_index:
            attempt = replace(attempt, attempt_index=persisted.call.attempt_index)
        terminal = FixtureRoleTerminalAuthorityV5(
            "study-runtime-v1",
            persisted.call.sha256,
            persisted.request.sha256,
            attempt.sha256,
            attempt.artifact_sha256,
        )
        return RoleInvocationPackageV5(persisted.call, persisted.request, attempt, terminal, artifact)

    def _invoke(self, persisted):
        self._verify_response_authority()
        if persisted.call.campaign_id != self.manifest.campaign_id or persisted.call.round_index != self.round_index:
            raise ValueError("scripted study request differs from its manifest slot")
        response = scripted_response_v1(
            request=persisted.request,
            round_index=self.round_index,
            registry=self.registry,
            selected=self.selected,
        )
        return self._package(persisted, response)

    def invoke_once(self, persisted_request: FreshPersistedRoleRequestV5, *, deadline_monotonic: float):
        if type(persisted_request) is not FreshPersistedRoleRequestV5:
            raise ValueError("scripted study invocation requires a fresh request")
        if type(deadline_monotonic) is not float:
            raise ValueError("scripted study deadline is invalid")
        return self._invoke(persisted_request)

    def reconcile_once(self, persisted_request: ExistingPersistedRoleRequestV5):
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise ValueError("scripted study recovery requires an existing request")
        return self._invoke(persisted_request)

    def authenticate_existing_round(self) -> bool:
        """Authenticate persisted scripted packages before a runtime fast return."""

        self._verify_response_authority()
        events = self.repository.load_round_events(
            campaign_id=self.manifest.campaign_id,
            round_index=self.round_index,
        )
        if not events:
            return False
        for event in events:
            payload = self.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            if type(payload) is not RoleCompletionPayloadV5:
                continue
            package = self.repository.load_role_invocation(payload)
            if package.call.role == "investigator" and self.round_index == 2:
                continue
            if not package.accepted:
                if (
                    package.artifact is not None
                    or package.attempt.failure_code is None
                    or type(package.terminal_authority) is not FixtureRoleTerminalAuthorityV5
                ):
                    raise ValueError("persisted scripted role failure is not an authenticated fixture terminal")
                continue
            persisted = ExistingPersistedRoleRequestV5(
                payload.request_ref,
                package.call,
                package.request,
            )
            expected = self._invoke(persisted)
            if expected != package:
                raise ValueError("persisted scripted role package differs from its response authority")
        return True


class StudyCandidateRuntimeV1:
    """Synthetic candidate/evaluator ports backed only by the frozen registry."""

    def __init__(self, repository: LocalArtifactRepositoryV5, registry: FrozenBehaviorRegistryV1) -> None:
        if type(repository) is not LocalArtifactRepositoryV5 or type(registry) is not FrozenBehaviorRegistryV1:
            raise ValueError("study candidate runtime authority is invalid")
        self.repository = repository
        self.registry = registry
        self.parent_source: SourceBundleV5 | None = None
        self.inputs: FeedbackRoundInputV5 | None = None
        self.active: dict[str, OwnedLeaseV5] = {}
        self.opened_workers: list[tuple[str, str]] = []

    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5:
        if type(parent) is not ParentCandidateV5:
            raise ValueError("candidate parent is invalid")
        source = self.repository.load_typed_artifact(parent.source_bundle_ref, value_type=SourceBundleV5)
        if source.sha256 != parent.source_bundle_ref.sha256 or tuple(
            (item.path, item.sha256) for item in source.files
        ) != parent.policy_revision.editable_source_sha256:
            raise ValueError("candidate parent source differs from its revision")
        _configuration_for_source(self.registry, source, parent.policy_revision)
        self.parent_source = source
        return source

    def _check_inputs(self, inputs: FeedbackRoundInputV5) -> None:
        if type(inputs) is not FeedbackRoundInputV5:
            raise ValueError("candidate inputs are invalid")
        if self.inputs is None:
            self.inputs = inputs
        elif self.inputs != inputs:
            raise ValueError("candidate inputs differ from their first authority")
        if self.parent_source is None:
            raise ValueError("candidate parent source was not loaded")

    def _variant_configuration(self, variant: RenderedVariantV5) -> RegistryConfigurationV1:
        return _configuration_for_variant(self.registry, variant)

    def materialize(self, *, inputs, experiment_identity, variant, deadline):
        _deadline_check(deadline)
        if type(experiment_identity) is not ExperimentIdentityV5 or type(variant) is not RenderedVariantV5:
            raise ValueError("candidate materialization identity is invalid")
        self._check_inputs(inputs)
        if experiment_identity.policy_revision_sha256 != variant.policy_revision.sha256:
            raise ValueError("candidate identity differs from rendered revision")
        self._variant_configuration(variant)
        ref = self.repository.append_typed_state(
            namespace="policy-source",
            key=variant.policy_revision.sha256,
            value=variant.source_bundle,
        )
        payload = ResourceLeasePayloadV5(
            "fixture-" + experiment_identity.sha256,
            "workspace",
            inputs.campaign_id,
            inputs.owner_token_sha256,
        )
        lease = OwnedLeaseV5(payload, inputs.round_index, payload.lease_id)
        self.active[payload.lease_id] = lease
        return MaterializedVariantV5(variant, ref, (lease,), variant.source_bundle.sha256)

    def recover_materialized(self, *, inputs, experiment_identity, variant, leases, deadline):
        _deadline_check(deadline)
        self._check_inputs(inputs)
        if type(leases) is not tuple:
            raise ValueError("candidate recovery requires one owned lease")
        expected_lease_id = "fixture-" + experiment_identity.sha256
        candidate_leases = tuple(item for item in leases if item.payload.lease_id == expected_lease_id)
        if len(candidate_leases) != 1:
            raise ValueError("candidate recovery requires its one owned lease")
        materialized = self.materialize(
            inputs=inputs,
            experiment_identity=experiment_identity,
            variant=variant,
            deadline=deadline,
        )
        if materialized.leases != candidate_leases:
            raise ValueError("candidate recovered lease differs")
        return materialized

    def _configuration_for_materialized(self, materialized: MaterializedVariantV5) -> RegistryConfigurationV1:
        if type(materialized) is not MaterializedVariantV5:
            raise ValueError("candidate materialization is invalid")
        configuration = self._variant_configuration(materialized.variant)
        if materialized.opaque_candidate != materialized.variant.source_bundle.sha256:
            raise ValueError("candidate source handle differs")
        for lease in materialized.leases:
            if self.active.get(lease.payload.lease_id) != lease:
                raise ValueError("candidate lease is not owned")
        return configuration

    def validate(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> ValidationResultV5:
        _deadline_check(deadline)
        self._configuration_for_materialized(materialized)
        assert self.parent_source is not None
        changed = derive_changed_symbols_v5(before=self.parent_source, after=materialized.variant.source_bundle)
        return ValidationResultV5(True, None, changed)

    def fingerprint(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5):
        _deadline_check(deadline)
        configuration = self._configuration_for_materialized(materialized)
        fingerprint = fingerprint_policy_client_v5(registry_client_v1(self.registry, configuration.configuration_id))
        if fingerprint != configuration.fixed_suite:
            raise ValueError("registry fingerprint differs from its frozen suite")
        return fingerprint

    def _report(
        self,
        configuration: RegistryConfigurationV1,
        outcome,
        *,
        scenario_id: str,
        base_report: EvaluationReportV5,
    ) -> EvaluationReportV5:
        endpoint = {
            "base": (outcome.ending_equity, outcome.base_annualized_return_pct),
            "gross": (outcome.gross_ending_equity, outcome.gross_annualized_return_pct),
            "stress": (outcome.stress_ending_equity, outcome.stress_annualized_return_pct),
        }.get(scenario_id)
        if endpoint is None:
            raise ValueError("synthetic scenario is outside the frozen endpoint set")
        ending_equity, annualized_return = endpoint
        total = ((ending_equity / outcome.starting_equity) - Decimal("1")) * Decimal("100")
        gross_total = ((outcome.gross_ending_equity / outcome.starting_equity) - Decimal("1")) * Decimal("100")
        return replace(
            base_report,
            portfolio_annualized_return_pct=annualized_return,
            portfolio_total_return_pct=total,
            gross_annualized_return_pct=outcome.gross_annualized_return_pct,
            closed_trades=(0 if configuration.configuration_id == "P0" else 1),
            average_exposure_pct=Decimal("50"),
            average_cash_pct=Decimal("50"),
            turnover_pct=Decimal("0"),
            total_friction_usd=Decimal("0"),
            friction_drag_pct=gross_total - total,
        )

    def _panel(self, materialized: MaterializedVariantV5, episode: EpisodePlanV5, *, discovery: bool) -> PanelEvaluationV5:
        configuration = self._configuration_for_materialized(materialized)
        assert self.inputs is not None
        outcome = next(
            item
            for item in configuration.portfolio_evaluator_inputs
            if item.round_index == self.inputs.round_index
        )
        days = (date.fromisoformat(episode.end_date) - date.fromisoformat(episode.start_date)).days
        if days != outcome.days:
            raise ValueError("synthetic panel calendar differs from frozen evaluator input")
        baseline_report = selected_scenario(self.inputs.baseline.campaign.episodes[0].evaluation).report
        scenario_ids = ("gross", "base", "stress") if discovery else ("base",)
        scenarios = []
        for scenario_id in scenario_ids:
            ending = {
                "gross": outcome.gross_ending_equity,
                "base": outcome.ending_equity,
                "stress": outcome.stress_ending_equity,
            }[scenario_id]
            report = self._report(configuration, outcome, scenario_id=scenario_id, base_report=baseline_report)
            scenarios.append(ScenarioPanelEvaluationV5(scenario_id, outcome.starting_equity, ending, report))
        return PanelEvaluationV5(
            self.inputs.evaluator_contract.sha256,
            self.inputs.evaluator_contract.sandbox_profile_sha256,
            episode.panel_ref.sha256,
            configuration.policy_revision.sha256,
            episode.start_date,
            episode.end_date,
            days,
            "base",
            tuple(scenarios),
        )

    def evaluate_quick(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5):
        _deadline_check(deadline)
        assert self.inputs is not None
        return self._panel(materialized, self.inputs.panel_plan.quick, discovery=False)

    def evaluate_episode(self, materialized: MaterializedVariantV5, episode: EpisodePlanV5, *, deadline: StageDeadlineV5):
        _deadline_check(deadline)
        return EpisodeEvaluationV5(
            episode.episode_id,
            episode.episode_ordinal or 0,
            episode.start_date,
            episode.end_date,
            self._panel(materialized, episode, discovery=True),
        )

    def recover_lease(self, payload: ResourceLeasePayloadV5, *, round_index: int) -> OwnedLeaseV5:
        if self.inputs is not None and (
            payload.owner_campaign_id != self.inputs.campaign_id
            or payload.owner_token_sha256 != self.inputs.owner_token_sha256
        ):
            raise ValueError("study lease is foreign")
        lease = OwnedLeaseV5(payload, round_index, payload.lease_id)
        self.active[payload.lease_id] = lease
        return lease

    def cleanup(self, leases: tuple[OwnedLeaseV5, ...], *, deadline: StageDeadlineV5) -> CleanupResultPayloadV5:
        _deadline_check(deadline)
        if type(leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in leases):
            raise ValueError("study cleanup leases are invalid")
        for lease in leases:
            if self.active.get(lease.payload.lease_id) != lease:
                raise ValueError("study cleanup lease is foreign")
        for lease in leases:
            del self.active[lease.payload.lease_id]
        return CleanupResultPayloadV5(len(leases), 0, 0, 0, not self.active)


def study_workers_v1(
    *,
    bound: MechanismBoundCandidateV1,
    registry: FrozenBehaviorRegistryV1,
) -> tuple[MechanismWorkerPortV1, MechanismWorkerPortV1]:
    if type(bound) is not MechanismBoundCandidateV1 or type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("study worker authority is invalid")
    parent = _configuration_for_source(registry, bound.capability.parent_source_bundle, bound.capability.parent_revision)
    candidate = _configuration_for_source(registry, bound.candidate_source_bundle, bound.candidate_revision)
    parent_registration = MechanismWorkerRegistrationV1(
        experiment_id=bound.experiment_id,
        spec_sha256=bound.capability.spec.sha256,
        role="parent",
        parent_revision_sha256=bound.capability.parent_revision_sha256,
        candidate_bytes_sha256=bound.candidate_source_bundle.sha256,
        corpus_sha256=bound.capability.corpus.sha256,
        resource_budget=bound.capability.resource_budget,
        execution_kind="synthetic_fixture",
        reset_semantics="reset_per_case",
        cpu_memory_enforced=False,
    )
    candidate_registration = replace(parent_registration, role="candidate")
    parent_decisions = {}
    candidate_decisions = {}
    for case in bound.capability.corpus.cases:
        parent_decisions[case.input_identity_sha256] = registry_decision_v1(
            registry=registry,
            configuration_id=parent.configuration_id,
            method="evaluate_exit",
            snapshot=case.snapshot,
        )
        candidate_decisions[case.input_identity_sha256] = registry_decision_v1(
            registry=registry,
            configuration_id=candidate.configuration_id,
            method="evaluate_exit",
            snapshot=case.snapshot,
        )
    return (
        SyntheticFixtureWorkerV1(registration=parent_registration, decisions_by_input_identity=parent_decisions),
        SyntheticFixtureWorkerV1(registration=candidate_registration, decisions_by_input_identity=candidate_decisions),
    )


def _seed_snapshot(registry: FrozenBehaviorRegistryV1):
    from core.strategy_policy.contracts_v3 import ExitSnapshotV3

    raw = registry.configuration("P0").synthetic_evaluator_inputs[0].snapshot_json
    return ExitSnapshotV3.from_canonical_json(raw.decode("utf-8"))


def _round_one_spec(intent: RoundIntentPayloadV5, parent: ParentCandidateV5) -> MechanismExperimentSpecV1:
    seed = canonical_json_bytes_v5(
        {"round": 1, "intent": round_intent_sha256_v1(intent), "parent": parent.policy_identity_sha256}
    )
    return MechanismExperimentSpecV1(
        precommitment_id="v5.study." + _sha256(seed),
        hypothesis_id=intent.hypothesis.hypothesis_id,
        hypothesis_sha256=intent.hypothesis.sha256,
        parent_revision_sha256=parent.policy_identity_sha256,
        round_intent_sha256=round_intent_sha256_v1(intent),
        target_method="evaluate_exit",
        allowed_symbols=MECHANISM_ALLOWED_SYMBOLS_V1,
        applicability=MechanismPredicateV1("features.atr_20_fraction", "is_present", None),
        controls=(_MECHANISM_CONTROL,),
        metrics=_MECHANISM_METRICS,
        aggregation="paired_case_delta",
        minimum_relevant_cases=2,
        recipe=_MECHANISM_RECIPE,
        disconfirming_observations=(_MECHANISM_DISCONFIRMING,),
        provenance="synthetic_offline",
        frozen_before_authoring=True,
    )


class StudyMechanismComposerV1:
    """Late-bound mechanism capability for one concrete study round."""

    def __init__(
        self,
        *,
        fixture: StudyFixtureV1,
        inputs: FeedbackRoundInputV5,
        arm: StudyArmV1,
        store: StudyStoreV1,
        ledger: StudyLedgerV1 | None,
        imported: StudyImportV1 | None,
        scripted: StudyScriptedRoleInvokerV1,
        registered_worker_factory: MechanismWorkerFactoryV1 | None = None,
    ) -> None:
        if type(fixture) is not StudyFixtureV1 or type(inputs) is not FeedbackRoundInputV5:
            raise ValueError("study mechanism composer authority is invalid")
        if arm not in {"primary", "withheld"} or type(store) is not StudyStoreV1:
            raise ValueError("study mechanism composer arm/store is invalid")
        if type(scripted) is not StudyScriptedRoleInvokerV1:
            raise ValueError("study mechanism composer role delegate is invalid")
        if registered_worker_factory is not None and not callable(registered_worker_factory):
            raise ValueError("study registered mechanism worker factory is invalid")
        if registered_worker_factory is not None:
            from core.pit_optimizer_v5.mechanism_docker import MechanismDockerCaseWorkerFactoryV1

            if type(registered_worker_factory) is not MechanismDockerCaseWorkerFactoryV1:
                raise ValueError("study registered mechanism factory must be the bounded Docker case factory")
        self.fixture = fixture
        self.inputs = inputs
        self.arm = arm
        self.store = store
        self.ledger = ledger
        self.imported = imported
        self.scripted = scripted
        self.registered_worker_factory = registered_worker_factory
        self.mechanism_repository = MechanismArtifactRepositoryV5(fixture.repository)
        self._extension: MechanismRuntimeExtensionV1 | None = None
        self._capability: MechanismExtensionCapabilityV1 | None = None
        self._link_ref: ArtifactRefV5 | None = None

    def _saved_investigator(self) -> tuple[RoleInvocationPackageV5, RoundIntentPayloadV5 | None]:
        events = self.fixture.repository.load_round_events(
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
        )
        payloads = tuple(
            self.fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            for event in events
        )
        completions = tuple(item for item in payloads if type(item) is RoleCompletionPayloadV5 and item.role == "investigator")
        intents = tuple(item for item in payloads if type(item) is RoundIntentPayloadV5)
        if len(completions) != 1 or len(intents) > 1:
            raise MechanismCapabilityError("saved investigator or round intent is not unique")
        package = self.fixture.repository.load_role_invocation(completions[0])
        if package.accepted:
            if not isinstance(package.artifact, InvestigatorArtifactV5):
                raise MechanismCapabilityError("saved investigator completion is not an investigator artifact")
        elif (
            package.artifact is not None
            or package.attempt.failure_code is None
            or type(package.terminal_authority) is not FixtureRoleTerminalAuthorityV5
        ):
            raise MechanismCapabilityError("saved investigator completion is not an authenticated fixture terminal")
        if not package.accepted:
            expected_terminal = FixtureRoleTerminalAuthorityV5(
                _FIXTURE_ROLE_TERMINAL_ID,
                package.call.sha256,
                package.request.sha256,
                package.attempt.sha256,
                None,
            )
            if package.terminal_authority != expected_terminal:
                raise MechanismCapabilityError("saved investigator fixture terminal differs from its script authority")
        intent = None if not intents else intents[0]
        if intent is not None and not package.accepted:
            raise MechanismCapabilityError("failed investigator completion cannot carry a round intent")
        if intent is not None:
            matching_hypotheses = tuple(
                hypothesis
                for hypothesis in package.artifact.hypotheses
                if hypothesis == intent.hypothesis
            )
            if len(matching_hypotheses) != 1:
                raise MechanismCapabilityError("saved investigator hypothesis differs from round intent")
        return package, intent

    def _import_authority(self, package: RoleInvocationPackageV5) -> None:
        if self.imported is None or self.ledger is None:
            raise MechanismCapabilityError("round-two mechanism requires an authenticated import and ledger")
        invoker = StudyImportInvokerV1(
            manifest=self.fixture.manifest,
            imported=self.imported,
            store=self.store,
            ledger=self.ledger,
            delegate=self.scripted,
        )
        repository, _preflight, request, call, _manifest = invoker._fixture_authority()
        import_events = repository.load_round_events(campaign_id=call.campaign_id, round_index=call.round_index)
        import_payloads = tuple(
            repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in import_events
        )
        import_completions = tuple(
            item for item in import_payloads if type(item) is RoleCompletionPayloadV5 and item.role == "investigator"
        )
        if len(import_completions) != 1:
            raise MechanismCapabilityError("import fixture lacks one investigator completion")
        imported_package = repository.load_role_invocation(import_completions[0])
        if imported_package != package or request != package.request or call != package.call:
            raise MechanismCapabilityError("runtime investigator package differs from authenticated import")
        verify_imported_package_v1(
            package=imported_package,
            record=self.imported,
            store=self.store,
            ledger=self.ledger,
            fixture_repository=repository,
        )

    def _parent_authority(
        self,
        parent: ParentCandidateV5,
    ) -> tuple[PolicyRevisionIdentityV5, ArtifactRefV5, SourceBundleV5, StoredExperimentRecordV5 | None, CampaignEvidenceV5 | None, object | None]:
        if parent.origin == "baseline":
            baseline = self.fixture.manifest.baseline_authority
            return (
                baseline.policy_revision,
                baseline.policy_revision_ref,
                baseline.source_bundle,
                None,
                None,
                None,
            )
        if parent.experiment_record_ref is None:
            raise MechanismCapabilityError("archived parent lacks its record reference")
        record = self.fixture.repository.load_experiment(parent.experiment_record_ref)
        stored = StoredExperimentRecordV5(parent.experiment_record_ref, record)
        if record.policy_revision is None or record.campaign_evidence is None:
            raise MechanismCapabilityError("archived parent lacks policy or campaign evidence")
        source_refs = tuple(
            item
            for item in record.artifact_refs
            if item.relative_path == f"adapter-state/policy-source/{record.policy_revision.sha256}.json"
        )
        if len(source_refs) != 1:
            raise MechanismCapabilityError("archived parent lacks its persisted source bundle")
        source = self.fixture.repository.load_typed_artifact(source_refs[0], value_type=SourceBundleV5)
        if source != _configuration_for_source(self.fixture.registry, source, record.policy_revision).source_bundle:
            raise MechanismCapabilityError("archived parent source differs from the registry")
        revision_ref = self.fixture.repository.append_typed_state(
            namespace="policy-revision",
            key=record.policy_revision.sha256,
            value=record.policy_revision,
        )
        return record.policy_revision, revision_ref, source, stored, record.campaign_evidence, record.quick_evidence

    def _persist_round_two_link(
        self,
        *,
        parent: ParentCandidateV5,
        intent: RoundIntentPayloadV5,
        compiled: CompiledStudyExperimentV1,
        capability: MechanismExtensionCapabilityV1,
    ) -> None:
        persisted = self.mechanism_repository.load_precommitment(capability)
        if persisted is None:
            raise MechanismCapabilityError("round-two mechanism precommitment is missing")
        payload = canonical_json_bytes_v5(
            {
                "schema_version": 1,
                "arm": self.arm,
                "campaign_id": self.inputs.campaign_id,
                "round_index": self.inputs.round_index,
                "import_sha256": self.imported.sha256 if self.imported is not None else None,
                "round_intent_sha256": round_intent_sha256_v1(intent),
                "parent_revision_sha256": parent.policy_identity_sha256,
                "precommitment_id": compiled.spec.precommitment_id,
                "spec_sha256": compiled.spec.sha256,
                "corpus_sha256": compiled.corpus.sha256,
                "contrast_sha256": compiled.contrast.sha256,
                "commitment_index_ref": compiled.draft_binding.storage_ref.to_primitive(),
                "mechanism_index_ref": persisted.index.spec_ref.to_primitive(),
                "mechanism_corpus_ref": persisted.index.corpus_ref.to_primitive(),
                "extension_source_sha256": mechanism_extension_source_sha256_v1(),
            }
        )
        key = f"{self.arm}-{self.inputs.campaign_id}-{self.inputs.round_index}"
        self._link_ref = self.store.put(kind="mechanism-links", key=key, content=payload)

    def _existing_round_parent(self) -> ParentCandidateV5:
        reducer = LocalArchiveReducerFactoryV5(self.fixture.repository).recovery_reducer(self.inputs)
        checkpoint, state = self.fixture.repository.recover_projection(reducer)
        stored = tuple(
            StoredExperimentRecordV5(reference, self.fixture.repository.load_experiment(reference))
            for reference in checkpoint.record_refs
        )
        return select_parent_v5(
            state=state,
            baseline=self.inputs.baseline,
            discovery_plan=self.inputs.panel_plan,
            evaluator_contract=self.inputs.evaluator_contract,
            stored_records=stored,
        )

    def _round_one_parent(self) -> ParentCandidateV5:
        """Reconstruct the immutable baseline parent without archive writes."""

        try:
            parent = baseline_parent_candidate_v5(
                authority=self.fixture.manifest.baseline_authority,
                discovery_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
                pit_data_scope=self.fixture.manifest.manifest.pit_data_scope,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-one baseline parent authority is unavailable") from exc
        if (
            parent.source_bundle_ref != self.fixture.manifest.baseline_authority.source_bundle_ref
            or parent.policy_identity_sha256 != self.fixture.manifest.baseline_authority.policy_revision.sha256
        ):
            raise MechanismCapabilityError("round-one baseline parent differs from campaign authority")
        return parent

    def _round_one_capability(
        self,
        *,
        intent: RoundIntentPayloadV5,
        parent: ParentCandidateV5,
    ) -> MechanismExtensionCapabilityV1:
        baseline = self.fixture.manifest.baseline_authority
        if (
            intent.parent_revision_sha256 != parent.policy_identity_sha256
            or intent.parent_semantic_fingerprint_sha256 != parent.semantic_fingerprint_sha256
            or intent.discovery_plan_sha256 != self.inputs.panel_plan.discovery_plan_sha256
            or intent.pit_data_scope != self.fixture.manifest.manifest.pit_data_scope
            or intent.semantic_mode != self.fixture.manifest.manifest.semantic_mode
        ):
            raise MechanismCapabilityError("round-one intent differs from the authenticated baseline parent")
        spec = _round_one_spec(intent, parent)
        corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot(self.fixture.registry))
        return MechanismExtensionCapabilityV1(
            authenticated_manifest=self.fixture.manifest,
            round_intent=intent,
            parent_candidate=parent,
            parent_revision=baseline.policy_revision,
            parent_revision_ref=baseline.policy_revision_ref,
            parent_source_bundle=baseline.source_bundle,
            parent_source_bundle_ref=baseline.source_bundle_ref,
            spec=spec,
            corpus=corpus,
            resource_budget=self.fixture.resource_budget,
            authorization=explicit_mechanism_fixture_opt_in_v1(
                f"study-runtime:{self.arm}:{self.inputs.campaign_id}:{self.inputs.round_index}:{intent.hypothesis.sha256}"
            ),
            round_index=1,
            execution_kind=(
                "registered_sandbox" if self.registered_worker_factory is not None else "synthetic_fixture"
            ),
            extension_source_sha256=mechanism_extension_source_sha256_v1(),
            parent_campaign_evidence=baseline.campaign,
            parent_quick_evidence=None,
            parent_record=None,
        )

    def _strict_round_one_precommitment(
        self,
        capability: MechanismExtensionCapabilityV1,
    ):
        """Load the round-one graph through a no-repair authority boundary."""

        try:
            index = self.fixture.repository.load_typed_state(
                namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
                key=f"{self.inputs.campaign_id}-{self.inputs.round_index:04d}",
                value_type=MechanismPrecommitmentIndexV1,
                repair=False,
            )
        except (ValueError, TypeError) as exc:
            raise MechanismCapabilityError("round-one mechanism precommitment authority is unreadable") from exc
        if index is None:
            return None
        try:
            persisted = self.mechanism_repository.load_precommitment(capability)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-one mechanism precommitment graph is invalid") from exc
        if persisted is None:
            raise MechanismCapabilityError("round-one mechanism precommitment graph is missing")
        return persisted

    def _authenticate_round_one_history(
        self,
        *,
        intent: RoundIntentPayloadV5 | None,
        role_names: set[str],
        payloads: tuple[object, ...],
    ) -> bool:
        """Authenticate F, baseline ancestry, and the persisted round-one graph."""

        if "investigator" not in role_names:
            raise MechanismCapabilityError("round-one role completion prefix is invalid")
        if "critic" in role_names and "author" not in role_names:
            raise MechanismCapabilityError("round-one critic completion lacks its author")
        if intent is None:
            return self._authenticate_no_intent_history(
                package=self._saved_investigator()[0],
                role_names=role_names,
                payloads=payloads,
                message="round-one intent is missing for a later role completion",
            )

        parent = self._round_one_parent()
        capability = self._round_one_capability(intent=intent, parent=parent)
        persisted = self._strict_round_one_precommitment(capability)
        if persisted is None:
            if "author" in role_names or "critic" in role_names:
                raise MechanismCapabilityError("completed round-one mechanism precommitment is missing")
            return False
        if "author" not in role_names:
            # The public precommitment is lawful after F/intent and before the
            # author request; leave the legacy runtime to resume that phase.
            return False

        try:
            _state = LocalArchiveReducerFactoryV5(self.fixture.repository).verify_projection(
                manifest=self.fixture.manifest.manifest,
                panel_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
                repair=False,
            )
            checkpoint = self.fixture.repository.load_checkpoint()
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-one checkpoint authority is unavailable") from exc
        if checkpoint is None:
            # Authoring and all candidate/critic work may have completed in
            # memory before publication.  The durable role packages and event
            # chain are still valid resume authority; no checkpoint is required
            # until publication reaches its atomic boundary.
            return False
        records = tuple(
            StoredExperimentRecordV5(reference, self.fixture.repository.load_experiment(reference))
            for reference in checkpoint.record_refs
        )
        round_one_records = tuple(
            item
            for item in records
            if item.record.round_index == 1 and is_testable_experiment_status_v5(item.record.status)
        )
        current_records = tuple(item for item in records if item.record.round_index == 1)
        if not current_records:
            raise MechanismCapabilityError("round-one checkpoint lacks its current experiment records")
        journal_records = self.fixture.repository.load_experiment_journal()
        checkpoint_refs = set(checkpoint.record_refs)
        orphan_current = tuple(
            item
            for item in journal_records
            if item.record.round_index == 1 and item.reference not in checkpoint_refs
        )
        if orphan_current:
            raise MechanismCapabilityError("round-one checkpoint omits a persisted experiment record")
        if "author" not in role_names:
            raise MechanismCapabilityError("round-one checkpoint lacks its author completion")
        if not round_one_records:
            if "critic" in role_names:
                raise MechanismCapabilityError("round-one critic completion lacks testable records")
            return True
        if "critic" not in role_names:
            raise MechanismCapabilityError("round-one checkpoint lacks its critic completion")
        manifest_source_identity = manifest_source_identity_sha256_v1(self.fixture.manifest)
        for stored_record in current_records:
            if (
                stored_record.record.parent_revision_sha256 != intent.parent_revision_sha256
                or stored_record.record.hypothesis != intent.hypothesis
                or stored_record.record.pit_data_scope != intent.pit_data_scope
                or stored_record.record.semantic_mode != intent.semantic_mode
            ):
                raise MechanismCapabilityError("round-one checkpoint record differs from the authenticated intent")
        for stored_record in round_one_records:
            try:
                loaded = self.mechanism_repository.load_existing_evidence_for_record(
                    campaign_id=self.inputs.campaign_id,
                    round_index=1,
                    manifest_ref=self.fixture.manifest.manifest_ref,
                    manifest_source_identity_sha256=manifest_source_identity,
                    stored_record=stored_record,
                )
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise MechanismCapabilityError("round-one mechanism evidence graph is invalid") from exc
            if loaded is None:
                raise MechanismCapabilityError(
                    f"round-one mechanism evidence is missing for {stored_record.record.experiment_id}"
                )
            _evidence, saved_intent = loaded
            if saved_intent != intent:
                raise MechanismCapabilityError("round-one mechanism evidence intent differs from F")
        return True

    def _authenticate_no_intent_history(
        self,
        *,
        package: RoleInvocationPackageV5,
        role_names: set[str],
        payloads: tuple[object, ...],
        message: str,
    ) -> bool:
        """Authenticate a durable pre-intent prefix or terminal history.

        The runtime can append a terminal outcome after investigator F and
        before the round intent.  Keep that outcome durable and let the
        runtime's existing recovery validation return it; only admit the
        closed payload sequence that the runtime can produce at this phase.
        """

        investigator_completions = tuple(
            item
            for item in payloads
            if type(item) is RoleCompletionPayloadV5 and item.role == "investigator"
        )
        if role_names != {"investigator"} or len(investigator_completions) != 1:
            raise MechanismCapabilityError(message)
        terminal_items = tuple(item for item in payloads if type(item) is RoundOutcomePayloadV5)
        if len(terminal_items) > 1:
            raise MechanismCapabilityError("durable terminal outcome is not unique")
        if not terminal_items:
            if any(type(item) is not RoleCompletionPayloadV5 for item in payloads):
                raise MechanismCapabilityError(message)
            return False

        terminal = terminal_items[0]
        if (
            terminal.campaign_id != self.inputs.campaign_id
            or terminal.round_index != self.inputs.round_index
            or any(
                type(item) not in {RoleCompletionPayloadV5, RoundOutcomePayloadV5, CleanupResultPayloadV5}
                for item in payloads
            )
        ):
            raise MechanismCapabilityError("durable terminal history contains an unsupported payload")
        terminal_index = payloads.index(terminal)
        if terminal_index < payloads.index(investigator_completions[0]) or any(
            type(item) is CleanupResultPayloadV5
            for item in payloads[:terminal_index]
        ):
            raise MechanismCapabilityError("durable terminal history has an invalid phase order")
        authority = terminal.authority
        if package.accepted:
            if type(authority) is RuntimeFailureAuthorityV5:
                allowed_codes = _ACCEPTED_INVESTIGATOR_FAILURE_CODES_BY_STAGE.get(authority.stage, frozenset())
                if (
                    authority.failure_code not in allowed_codes
                    or authority.role is not None
                    or authority.experiment_id is not None
                ):
                    raise MechanismCapabilityError("accepted investigator terminal authority is out of phase")
            elif type(authority) in {NoNovelHypothesisAuthorityV5, NoveltyExhaustedAuthorityV5}:
                try:
                    # verify_projection's scheduling-history fold reconstructs
                    # the state before this terminal, then binds the exact F
                    # request/evidence/attempt/artifact, selection transition,
                    # cursor and exhaustion chain.  Keep repair disabled: this
                    # gate only authenticates existing bytes before runtime's
                    # completed-terminal fast return.
                    LocalArchiveReducerFactoryV5(self.fixture.repository).verify_projection(
                        manifest=self.fixture.manifest.manifest,
                        panel_plan=self.inputs.panel_plan,
                        evaluator_contract=self.inputs.evaluator_contract,
                        repair=False,
                    )
                except (TypeError, ValueError, ArithmeticError, OSError) as exc:
                    raise MechanismCapabilityError("durable novelty terminal authority is invalid") from exc
            else:
                raise MechanismCapabilityError("accepted investigator terminal authority is invalid")
        elif (
            type(authority) is not RuntimeFailureAuthorityV5
            or authority.stage != "investigator"
            or authority.failure_code != "role_rejected"
            or authority.role != "investigator"
            or authority.experiment_id is not None
        ):
            raise MechanismCapabilityError("rejected investigator terminal authority is invalid")
        return False

    def _load_persisted_round_two_graph(
        self,
        *,
        package: RoleInvocationPackageV5,
        intent: RoundIntentPayloadV5,
    ) -> tuple[CompiledStudyExperimentV1, ParentCandidateV5]:
        """Load the create-only round-two graph and its historical parent.

        A completed fixture round has advanced ``checkpoint.json``.  Re-running
        the public compiler against that mutable edge would therefore reject a
        valid historical commitment.  The compiler has already authenticated
        and persisted this graph before authoring; completed-round auth reads
        those exact bytes and verifies their original P1 ancestry instead of
        selecting or regenerating a new graph.
        """

        if self.imported is None or self.ledger is None:
            raise MechanismCapabilityError("round-two graph requires an authenticated import and ledger")
        invoker = StudyImportInvokerV1(
            manifest=self.fixture.manifest,
            imported=self.imported,
            store=self.store,
            ledger=self.ledger,
            delegate=self.scripted,
        )
        repository, preflight, request, call, _manifest = invoker._fixture_authority()
        if request != package.request or call != package.call:
            raise MechanismCapabilityError("persisted round-two F package differs from its import authority")

        commitment_slot_hash = canonical_sha256_v5(
            (self.arm, self.inputs.campaign_id, self.inputs.round_index)
        )
        commitment_path = (
            f"adapter-blobs/study-v1-commitments/{self.arm}-{commitment_slot_hash}.bin"
        )
        commitment_refs = tuple(
            reference
            for reference in self.store.list_refs(kind="commitments")
            if reference.relative_path == commitment_path
        )
        matches: list[tuple[ArtifactRefV5, StudyCommitmentIndexV1]] = []
        for reference in commitment_refs:
            try:
                index = StudyCommitmentIndexV1.from_canonical_json(self.store.read(reference))
            except (UnicodeDecodeError, TypeError, ValueError, ArithmeticError) as exc:
                raise MechanismCapabilityError("round-two commitment index is not canonical") from exc
            if index.storage_ref != reference:
                raise MechanismCapabilityError("round-two commitment index reference differs from its bytes")
            if (
                index.arm == self.arm
                and index.campaign_id == self.inputs.campaign_id
                and index.round_index == self.inputs.round_index
            ):
                matches.append((reference, index))
        if len(matches) != 1:
            raise MechanismCapabilityError("round-two commitment index is not unique")
        _commitment_ref, index = matches[0]
        if (
            index.import_ref != self.imported.storage_ref
            or index.round_intent_ref.sha256 != round_intent_sha256_v1(intent)
            or index.parent_revision_sha256 == "0" * 64
        ):
            raise MechanismCapabilityError("round-two commitment index differs from its immutable import")

        def read(reference: ArtifactRefV5) -> bytes:
            raw = self.store.read(reference)
            if hashlib.sha256(raw).hexdigest() != reference.sha256:
                raise MechanismCapabilityError("round-two graph bytes differ from their references")
            return raw

        try:
            binding = StudyDraftBindingV1.from_canonical_json(read(index.binding_ref))
            spec = MechanismExperimentSpecV1.from_canonical_json(read(index.spec_ref).decode("utf-8"))
            contrast = StudyContrastV1.from_canonical_json(read(index.contrast_ref))
        except (UnicodeDecodeError, TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-two committed graph is not canonical") from exc
        if (
            binding.storage_ref != index.binding_ref
            or binding.import_ref != self.imported.storage_ref
            or binding.draft_ref != index.draft_ref
            or binding.round_intent_ref != index.round_intent_ref
            or binding.round_intent_sha256 != round_intent_sha256_v1(intent)
            or binding.parent_revision_sha256 != index.parent_revision_sha256
            or binding.parent_source_bundle_ref != index.parent_source_bundle_ref
            or binding.checkpoint_ref != preflight.checkpoint_ref
            or binding.snapshot_ref != preflight.snapshot_ref
            or binding.fixture_manifest_ref != self.imported.fixture_manifest_ref
            or binding.fixture_root_identity_sha256 != self.imported.fixture_root_identity_sha256
            or binding.fixture_role_package_sha256 != package.sha256
            or binding.journal_campaign_id != self.inputs.campaign_id
            or binding.journal_round_index != self.inputs.round_index
            or spec.sha256 != index.spec_ref.sha256
            or contrast.sha256 != index.contrast_ref.sha256
            or contrast.spec_sha256 != spec.sha256
            or contrast.corpus_sha256 != index.corpus_ref.sha256
            or contrast.draft_binding_sha256 != binding.sha256
        ):
            raise MechanismCapabilityError("round-two committed graph differs from its original authority")

        persisted_corpus = read(index.corpus_ref)
        corpus = build_mechanism_observation_corpus_v1(
            spec,
            seed_snapshot=_seed_snapshot(self.fixture.registry),
        )
        if persisted_corpus != corpus.canonical_bytes or corpus.sha256 != index.corpus_ref.sha256:
            raise MechanismCapabilityError("round-two committed corpus differs from its frozen registry inputs")
        try:
            compiled = CompiledStudyExperimentV1(
                spec=spec,
                corpus=corpus,
                contrast=contrast,
                draft_binding=binding,
                selected_configuration_id=index.selected_configuration_id,
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-two committed graph is internally inconsistent") from exc

        if binding.parent_experiment_record_ref is None:
            raise MechanismCapabilityError("round-two commitment does not retain its archived P1")
        try:
            state = LocalArchiveReducerFactoryV5(repository).verify_projection(
                manifest=self.fixture.manifest.manifest,
                panel_plan=self.inputs.panel_plan,
                evaluator_contract=self.inputs.evaluator_contract,
                repair=False,
            )
            checkpoint = repository.load_checkpoint()
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-two checkpoint authority is unavailable") from exc
        if checkpoint is None:
            raise MechanismCapabilityError("round-two historical P1 lacks a checkpoint")
        entries = tuple(
            entry
            for entry in state.archive.entries
            if entry.experiment_record_ref == binding.parent_experiment_record_ref
        )
        if len(entries) != 1:
            raise MechanismCapabilityError("round-two historical P1 is not checkpoint-authorized")
        record = repository.load_experiment(binding.parent_experiment_record_ref)
        stored = StoredExperimentRecordV5(binding.parent_experiment_record_ref, record)
        try:
            parent = archive_parent_from_record_v5(entry=entries[0], stored_record=stored)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise MechanismCapabilityError("round-two historical P1 is not authenticated") from exc
        if (
            parent.admitted_round != 1
            or parent.policy_identity_sha256 != binding.parent_revision_sha256
            or parent.source_bundle_ref != binding.parent_source_bundle_ref
            or parent.experiment_record_ref != binding.parent_experiment_record_ref
        ):
            raise MechanismCapabilityError("round-two commitment replaced its original archived P1")
        configuration = self.fixture.registry.configuration(compiled.selected_configuration_id)
        expected_parent = (
            configuration
            if configuration.parent_configuration_id is None
            else self.fixture.registry.configuration(configuration.parent_configuration_id)
        )
        if (
            expected_parent.policy_revision.sha256 != parent.policy_identity_sha256
            or expected_parent.source_bundle.sha256 != parent.source_bundle_ref.sha256
        ):
            raise MechanismCapabilityError("round-two selected configuration differs from historical P1")

        mechanism_index = repository.load_typed_state(
            namespace=MECHANISM_ARTIFACT_NAMESPACE_V1,
            key=f"{self.inputs.campaign_id}-{self.inputs.round_index:04d}",
            value_type=MechanismPrecommitmentIndexV1,
            repair=False,
        )
        if mechanism_index is None:
            raise MechanismCapabilityError("round-two mechanism precommitment is missing")
        expected_link_path = (
            f"adapter-blobs/study-v1-mechanism-links/"
            f"{self.arm}-{self.inputs.campaign_id}-{self.inputs.round_index}.bin"
        )
        link_refs = tuple(ref for ref in self.store.list_refs(kind="mechanism-links") if ref.relative_path == expected_link_path)
        if len(link_refs) != 1:
            raise MechanismCapabilityError("round-two mechanism link is not unique")
        link_raw = self.store.read(link_refs[0])
        try:
            link = json.loads(link_raw.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise MechanismCapabilityError("round-two mechanism link is not canonical") from exc
        expected_link = {
            "schema_version": 1,
            "arm": self.arm,
            "campaign_id": self.inputs.campaign_id,
            "round_index": self.inputs.round_index,
            "import_sha256": self.imported.sha256,
            "round_intent_sha256": round_intent_sha256_v1(intent),
            "parent_revision_sha256": parent.policy_identity_sha256,
            "precommitment_id": compiled.spec.precommitment_id,
            "spec_sha256": compiled.spec.sha256,
            "corpus_sha256": compiled.corpus.sha256,
            "contrast_sha256": compiled.contrast.sha256,
            "commitment_index_ref": compiled.draft_binding.storage_ref.to_primitive(),
            "mechanism_index_ref": mechanism_index.spec_ref.to_primitive(),
            "mechanism_corpus_ref": mechanism_index.corpus_ref.to_primitive(),
            "extension_source_sha256": mechanism_extension_source_sha256_v1(),
        }
        if canonical_json_bytes_v5(link) != link_raw or link != expected_link:
            raise MechanismCapabilityError("round-two mechanism link differs from its create-only authority")
        return compiled, parent

    def authenticate_existing_round(self) -> bool:
        """Authenticate persisted study bytes before legacy completed-round return."""

        events = self.fixture.repository.load_round_events(
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
        )
        if not events:
            return False
        if self.inputs.round_index == 1:
            if not self.scripted.authenticate_existing_round():
                raise MechanismCapabilityError("existing round-one authority is unavailable")
            _package, intent = self._saved_investigator()
            payloads = tuple(
                self.fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
                for event in events
            )
            role_names = {item.role for item in payloads if type(item) is RoleCompletionPayloadV5}
            return self._authenticate_round_one_history(
                intent=intent,
                role_names=role_names,
                payloads=payloads,
            )
        package, intent = self._saved_investigator()
        self._import_authority(package)
        payloads = tuple(
            self.fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            for event in events
        )
        role_names = {item.role for item in payloads if type(item) is RoleCompletionPayloadV5}
        if intent is None:
            return self._authenticate_no_intent_history(
                package=package,
                role_names=role_names,
                payloads=payloads,
                message="round-two intent is missing for a later role completion",
            )
        if "author" not in role_names:
            # A crash after F/intent (or after the public compiler but before
            # author admission) must be allowed to resume through the legacy
            # runtime.  Authenticate the source pin, reuse a committed graph
            # when one exists, and never manufacture a missing commitment.
            verify_scripted_role_authority_v1(
                repository=self.fixture.repository,
                manifest=self.fixture.manifest,
                registry=self.fixture.registry,
                round_index=self.inputs.round_index,
            )
            commitment_slot_hash = canonical_sha256_v5(
                (self.arm, self.inputs.campaign_id, self.inputs.round_index)
            )
            commitment_path = (
                f"adapter-blobs/study-v1-commitments/{self.arm}-{commitment_slot_hash}.bin"
            )
            commitment_refs = tuple(
                reference
                for reference in self.store.list_refs(kind="commitments")
                if reference.relative_path == commitment_path
            )
            if not commitment_refs:
                return False
            compiled, _parent = self._load_persisted_round_two_graph(package=package, intent=intent)
            self.scripted.set_selected(compiled)
            return False
        compiled, _parent = self._load_persisted_round_two_graph(package=package, intent=intent)
        self.scripted.set_selected(compiled)
        if not self.scripted.authenticate_existing_round():
            raise MechanismCapabilityError("existing round-two scripted authority is unavailable")
        link_key = f"{self.arm}-{self.inputs.campaign_id}-{self.inputs.round_index}"
        link_suffix = f"adapter-blobs/study-v1-mechanism-links/{link_key}.bin"
        if not any(reference.relative_path == link_suffix for reference in self.store.list_refs(kind="mechanism-links")):
            raise MechanismCapabilityError("existing round-two mechanism link is missing")
        return True

    def before_authoring(
        self,
        *,
        round_intent: RoundIntentPayloadV5,
        campaign_id: str | None = None,
        round_index: int | None = None,
        parent_candidate: object | None = None,
    ) -> None:
        if campaign_id is not None and campaign_id != self.inputs.campaign_id:
            raise MechanismCapabilityError("study mechanism campaign differs")
        if round_index is not None and round_index != self.inputs.round_index:
            raise MechanismCapabilityError("study mechanism round differs")
        if type(parent_candidate) is not ParentCandidateV5:
            raise MechanismCapabilityError("study mechanism parent is invalid")
        package, saved_intent = self._saved_investigator()
        if saved_intent != round_intent or package.artifact is None or saved_intent.hypothesis not in package.artifact.hypotheses:
            raise MechanismCapabilityError("study mechanism intent is not the saved investigator authority")
        if self.inputs.round_index == 2:
            self._import_authority(package)
        parent_revision, parent_revision_ref, parent_source, parent_record, parent_campaign, parent_quick = self._parent_authority(
            parent_candidate
        )
        if self.inputs.round_index == 2:
            if self.imported is None:
                raise MechanismCapabilityError("round-two study import is missing")
            compiled = compile_study_experiment_v1(
                store=self.store,
                imported=self.imported,
                request=package.request,
                round_intent=round_intent,
                parent=parent_candidate,
                seed_snapshot=_seed_snapshot(self.fixture.registry),
                registry=self.fixture.registry,
            )
            self.scripted.set_selected(compiled)
            spec, corpus = compiled.spec, compiled.corpus
        else:
            spec = _round_one_spec(round_intent, parent_candidate)
            corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot(self.fixture.registry))
        capability = MechanismExtensionCapabilityV1(
            authenticated_manifest=self.fixture.manifest,
            round_intent=round_intent,
            parent_candidate=parent_candidate,
            parent_revision=parent_revision,
            parent_revision_ref=parent_revision_ref,
            parent_source_bundle=parent_source,
            parent_source_bundle_ref=parent_candidate.source_bundle_ref,
            spec=spec,
            corpus=corpus,
            resource_budget=self.fixture.resource_budget,
            authorization=explicit_mechanism_fixture_opt_in_v1(
                f"study-runtime:{self.arm}:{self.inputs.campaign_id}:{self.inputs.round_index}:{round_intent.hypothesis.sha256}"
            ),
            round_index=self.inputs.round_index,
            execution_kind=(
                "registered_sandbox" if self.registered_worker_factory is not None else "synthetic_fixture"
            ),
            extension_source_sha256=mechanism_extension_source_sha256_v1(),
            parent_campaign_evidence=parent_campaign,
            parent_quick_evidence=parent_quick,
            parent_record=parent_record,
        )
        worker_factory = (
            self.registered_worker_factory
            if self.registered_worker_factory is not None
            else lambda bound: study_workers_v1(bound=bound, registry=self.fixture.registry)
        )
        extension = MechanismRuntimeExtensionV1(
            self.mechanism_repository,
            capability,
            worker_factory=worker_factory,
        )
        extension.before_authoring(
            round_intent=round_intent,
            campaign_id=self.inputs.campaign_id,
            round_index=self.inputs.round_index,
            parent_candidate=parent_candidate,
        )
        self._capability = capability
        self._extension = extension
        if self.inputs.round_index == 2:
            assert compiled is not None
            self._persist_round_two_link(
                parent=parent_candidate,
                intent=round_intent,
                compiled=compiled,
                capability=capability,
            )

    def _require_extension(self) -> MechanismRuntimeExtensionV1:
        if self._extension is None:
            raise MechanismCapabilityError("study mechanism extension is not initialized")
        return self._extension

    def observe_candidate(self, **kwargs):
        return self._require_extension().observe_candidate(**kwargs)

    def finalize_report(self, **kwargs):
        return self._require_extension().finalize_report(**kwargs)

    def role_request_evidence(self, experiment_id: str):
        return self._require_extension().role_request_evidence(experiment_id)


def compose_study_round_v1(
    *,
    fixture: StudyFixtureV1,
    round_index: int,
    arm: StudyArmV1,
    store: StudyStoreV1,
    ledger: StudyLedgerV1 | None,
    imported: StudyImportV1 | None,
    registered_worker_factory: MechanismWorkerFactoryV1 | None = None,
) -> tuple[FeedbackRoundInputV5, FeedbackRoundDependenciesV5]:
    if type(fixture) is not StudyFixtureV1 or type(store) is not StudyStoreV1:
        raise ValueError("study round composition authority is invalid")
    if round_index not in {1, 2} or arm not in {"primary", "withheld"}:
        raise ValueError("study round or arm is invalid")
    if round_index == 1 and (ledger is not None or imported is not None):
        raise ValueError("round one does not accept study import authority")
    if round_index == 2:
        if type(ledger) is not StudyLedgerV1 or type(imported) is not StudyImportV1:
            raise ValueError("round two requires its import and ledger")
        if imported.arm != arm or imported.study_id != ledger.study_id:
            raise ValueError("round-two import differs from its arm or ledger")
    if registered_worker_factory is not None and not callable(registered_worker_factory):
        raise ValueError("study registered mechanism worker factory is invalid")
    authenticated = fixture.manifest
    manifest = authenticated.manifest
    owner_token = canonical_sha256_v5({"fixture": manifest.sha256, "round": round_index})
    inputs = FeedbackRoundInputV5(
        manifest,
        authenticated.panel_plan,
        authenticated.evaluator_contract,
        authenticated.baseline_authority,
        round_index,
        owner_token,
    )
    existing_requests = fixture.repository.load_authenticated_role_requests(
        campaign_id=manifest.campaign_id,
        round_index=round_index,
    )
    existing_events = fixture.repository.load_round_events(
        campaign_id=manifest.campaign_id,
        round_index=round_index,
    )
    if existing_requests or existing_events:
        verify_scripted_role_authority_v1(
            repository=fixture.repository,
            manifest=manifest,
            registry=fixture.registry,
            round_index=round_index,
        )
    else:
        prepare_scripted_role_authority_v1(
            repository=fixture.repository,
            manifest=manifest,
            registry=fixture.registry,
            round_index=round_index,
        )
    scripted = StudyScriptedRoleInvokerV1(
        manifest,
        fixture.registry,
        round_index,
        repository=fixture.repository,
    )
    mechanism_repository = MechanismArtifactRepositoryV5(fixture.repository)
    base_factory = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=manifest)
    enabled_factory = LocalRoleRequestFactoryV5(
        repository=fixture.repository,
        manifest=manifest,
        mechanism_adapter=MechanismRoleRequestAdapterV1(mechanism_repository, authenticated),
    )
    requests = StudyRequestRouterV1(arm, base_factory, enabled_factory)
    invoker: RecoverableRoleInvokerV5
    if round_index == 1:
        invoker = scripted
    else:
        invoker = StudyImportInvokerV1(
            manifest=authenticated,
            imported=imported,
            store=store,
            ledger=ledger,
            delegate=scripted,
        )
    candidates = StudyCandidateRuntimeV1(fixture.repository, fixture.registry)
    # Recovery asks the candidate port to authenticate leases before the first
    # materialization call can seed its input authority.  Bind the immutable
    # round inputs at composition time so a dependency cannot register a lease
    # with a foreign owner token and leave it in its active set before the
    # runtime validates the returned handle.
    candidates.inputs = inputs
    mechanism = StudyMechanismComposerV1(
        fixture=fixture,
        inputs=inputs,
        arm=arm,
        store=store,
        ledger=ledger,
        imported=imported,
        scripted=scripted,
        registered_worker_factory=registered_worker_factory,
    )
    dependencies = FeedbackRoundDependenciesV5(
        persistence=fixture.repository,
        invoker=invoker,
        requests=requests,
        novelty=SelectionNoveltyResolverV5(),
        candidates=candidates,
        records=CanonicalExperimentRecordFactoryV5(),
        archive_reducers=LocalArchiveReducerFactoryV5(fixture.repository),
        clock=SystemMonotonicClockV5(),
        cancellation=ControllerCancellationV5(),
        cleanup=candidates,
        mechanism=mechanism,
    )
    mechanism.authenticate_existing_round()
    return inputs, dependencies


__all__ = [
    "ScriptedResponseAuthorityV1",
    "StudyCandidateRuntimeV1",
    "StudyMechanismComposerV1",
    "StudyRequestRouterV1",
    "StudyScriptedRoleInvokerV1",
    "compose_study_round_v1",
    "prepare_scripted_role_authority_v1",
    "scripted_response_authority_v1",
    "scripted_response_logic_sha256_v1",
    "scripted_response_v1",
    "study_workers_v1",
    "verify_scripted_role_authority_v1",
]
