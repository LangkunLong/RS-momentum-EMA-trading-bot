"""Concrete controller dependencies for the PIT optimizer V5 runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import threading
import time
from typing import Literal

from core.pit_optimizer_v5.artifacts import ArchiveSnapshotV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    StructuralTemplateV5,
    derive_changed_symbols_v5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    CriticArtifactV5,
    EvaluatorContractV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
    PanelEvaluationV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    SandboxProfileV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    selected_scenario,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.memory import (
    CleanupResultPayloadV5,
    ExperimentRecordV5,
    NoNovelHypothesisAuthorityV5,
    PreValidationInvalidExperimentIdentityV5,
    ResourceLeasePayloadV5,
    StoredExperimentRecordV5,
    is_testable_experiment_status_v5,
    reduce_experiment_journal_v5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    SemanticFingerprintV5,
    classify_semantic_fingerprints_v5,
)
from core.pit_optimizer_v5.production_sandbox import (
    LocalContainerExecutorV5,
    LocalSandboxMountFactoryV5,
)
from core.pit_optimizer_v5.provider import (
    ArchiveFamilyAggregateV5,
    AuthorPolicyContractsV5,
    AuthorRoleInputV5,
    CriticDirectionAggregateV5,
    CriticRoleInputV5,
    ExperimentEvaluationAggregateV5,
    ExperimentFailureAggregateV5,
    ExperimentPredictionAggregateV5,
    ExperimentSemanticDifferenceAggregateV5,
    InvestigatorRoleInputV5,
    RoleBindingV5,
    RoleInvocationPackageV5,
    RoleRequestV5,
    ScenarioAggregateV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.runtime import (
    CandidateEvidenceV5,
    FeedbackRoundInputV5,
    MaterializedVariantV5,
    OwnedLeaseV5,
    SearchProjectionV5,
    StageDeadlineV5,
)
from core.pit_optimizer_v5.sandbox import RuntimeDockerPanelEvaluatorV5
from core.pit_optimizer_v5.search import (
    ArchiveRecordAuthorityV5,
    BaselineParentAuthorityV5,
    CandidateArchiveReducerV5,
    ParentCandidateV5,
    PRIMARY_MECHANISMS_V5,
    expected_target_gap_pct_v5,
)
from core.pit_optimizer_v5.selection import (
    ScheduledHypothesisV5,
    advance_no_novel_parent_v5,
    select_novel_hypothesis_v5,
)
from core.pit_optimizer_v5.workspace import (
    GitCandidateMaterializerV5,
    MaterializedWorkspaceV5,
    WorkspaceLeaseV5,
    WorkspaceOwnerV5,
)


class _EvidenceBuilderV5:
    def __init__(self, role: Literal["investigator", "author", "critic"]) -> None:
        self._role = role
        self._items: list[RoleEvidenceItemV5] = []

    def add(self, metric_id: str, value: object) -> str:
        if type(metric_id) is not str:
            raise ValueError("role evidence metric ID is invalid")
        ordinal = len(self._items) + 1
        slug = re.sub(r"[^a-z0-9_.-]+", "-", metric_id.lower()).strip(".-")
        suffix = hashlib.sha256(f"{self._role}:{ordinal}:{metric_id}".encode()).hexdigest()[:12]
        evidence_id = f"v5.{self._role}.{ordinal}.{slug[:72]}.{suffix}"
        item = RoleEvidenceItemV5(evidence_id, metric_id, value)  # type: ignore[arg-type]
        self._items.append(item)
        return evidence_id

    def build(self) -> RoleEvidenceV5:
        return RoleEvidenceV5(5, tuple(self._items))


def _parent_campaign(
    inputs: FeedbackRoundInputV5,
    projection: SearchProjectionV5,
    parent: ParentCandidateV5,
):
    if parent.origin == "baseline":
        if parent.policy_revision != inputs.baseline.policy_revision:
            raise ValueError("baseline parent differs from baseline authority")
        return inputs.baseline.campaign
    matches = tuple(
        stored.record.campaign_evidence
        for stored in projection.stored_records
        if stored.reference == parent.experiment_record_ref and stored.record.policy_revision == parent.policy_revision
    )
    if len(matches) != 1 or matches[0] is None:
        raise ValueError("archive parent campaign authority is unavailable")
    return matches[0]


def _parent_fingerprint(
    inputs: FeedbackRoundInputV5,
    projection: SearchProjectionV5,
    parent: ParentCandidateV5,
):
    if parent.origin == "baseline":
        fingerprint = inputs.baseline.semantic_fingerprint
    else:
        matches = tuple(
            stored.record.semantic_fingerprint
            for stored in projection.stored_records
            if stored.reference == parent.experiment_record_ref
            and stored.record.policy_revision == parent.policy_revision
        )
        if len(matches) != 1 or matches[0] is None:
            raise ValueError("archive parent semantic authority is unavailable")
        fingerprint = matches[0]
    if fingerprint.fingerprint_sha256 != parent.semantic_fingerprint_sha256:
        raise ValueError("parent semantic authority differs from scheduling identity")
    return fingerprint


class LocalCandidateBaseOperationsV5:
    """Concrete authenticated source, materialization, and validation boundary."""

    def __init__(
        self,
        *,
        manifest: CampaignManifestV5,
        panel_plan: CampaignPanelPlanV5,
        evaluator_contract: EvaluatorContractV5,
        sandbox_profile: SandboxProfileV5,
        baseline: BaselineParentAuthorityV5,
        owner: WorkspaceOwnerV5,
        repository: LocalArtifactRepositoryV5,
        materializer: GitCandidateMaterializerV5,
        mount_factory: LocalSandboxMountFactoryV5,
        probe_evaluator: RuntimeDockerPanelEvaluatorV5,
        clock: "SystemMonotonicClockV5",
    ) -> None:
        if (
            type(manifest) is not CampaignManifestV5
            or type(panel_plan) is not CampaignPanelPlanV5
            or type(evaluator_contract) is not EvaluatorContractV5
            or type(sandbox_profile) is not SandboxProfileV5
            or type(baseline) is not BaselineParentAuthorityV5
            or type(owner) is not WorkspaceOwnerV5
            or type(repository) is not LocalArtifactRepositoryV5
            or type(materializer) is not GitCandidateMaterializerV5
            or type(mount_factory) is not LocalSandboxMountFactoryV5
            or type(probe_evaluator) is not RuntimeDockerPanelEvaluatorV5
            or type(clock) is not SystemMonotonicClockV5
        ):
            raise ValueError("local candidate-base authority is invalid")
        if (
            owner.campaign_id != manifest.campaign_id
            or owner.round_index > manifest.search.max_feedback_rounds
            or manifest.panel_plan_ref.sha256 != panel_plan.sha256
            or manifest.evaluator_contract_ref.sha256 != evaluator_contract.sha256
            or manifest.sandbox_profile_ref.sha256 != sandbox_profile.sha256
            or manifest.baseline_authority_ref.sha256 != canonical_sha256_v5(baseline)
            or evaluator_contract.sandbox_profile_sha256 != sandbox_profile.sha256
            or materializer.driver is not mount_factory.workspace_driver
            or type(probe_evaluator.evaluator.executor) is not LocalContainerExecutorV5
            or probe_evaluator.evaluator.executor.mount_factory is not mount_factory
        ):
            raise ValueError("local candidate-base authority graph is inconsistent")
        self._manifest = manifest
        self._panel_plan = panel_plan
        self._contract = evaluator_contract
        self._profile = sandbox_profile
        self._baseline = baseline
        self._owner = owner
        self._repository = repository
        self._materializer = materializer
        self._mount_factory = mount_factory
        self._probe_evaluator = probe_evaluator
        self._clock = clock
        self._base_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-local-candidate-base-v1",
                "manifest_sha256": manifest.sha256,
                "panel_plan_sha256": panel_plan.sha256,
                "evaluator_contract_sha256": evaluator_contract.sha256,
                "sandbox_profile_sha256": sandbox_profile.sha256,
                "baseline_authority_sha256": canonical_sha256_v5(baseline),
                "owner_sha256": owner.sha256,
                "repository_root_identity_sha256": repository.root_identity_sha256,
                "workspace_driver_sha256": materializer.driver.driver_identity_sha256,
                "mount_factory_sha256": mount_factory.mount_identity_sha256,
                "container_executor_sha256": (probe_evaluator.evaluator.executor.executor_identity_sha256),
                "probe_suite_id": PROBE_SUITE_ID_V5,
            }
        )

    @property
    def base_identity_sha256(self) -> str:
        return self._base_identity_sha256

    @property
    def manifest(self) -> CampaignManifestV5:
        return self._manifest

    @property
    def panel_plan(self) -> CampaignPanelPlanV5:
        return self._panel_plan

    @property
    def evaluator_contract(self) -> EvaluatorContractV5:
        return self._contract

    @property
    def sandbox_profile(self) -> SandboxProfileV5:
        return self._profile

    @property
    def baseline(self) -> BaselineParentAuthorityV5:
        return self._baseline

    @property
    def owner(self) -> WorkspaceOwnerV5:
        return self._owner

    @property
    def repository(self) -> LocalArtifactRepositoryV5:
        return self._repository

    @property
    def materializer(self) -> GitCandidateMaterializerV5:
        return self._materializer

    @property
    def mount_factory(self) -> LocalSandboxMountFactoryV5:
        return self._mount_factory

    @property
    def probe_evaluator(self) -> RuntimeDockerPanelEvaluatorV5:
        return self._probe_evaluator

    @property
    def clock(self) -> "SystemMonotonicClockV5":
        return self._clock

    def _check_deadline(self, deadline: StageDeadlineV5, stage: str) -> None:
        now = self._clock.monotonic()
        if (
            type(deadline) is not StageDeadlineV5
            or deadline.stage != stage
            or type(now) is not float
            or not math.isfinite(now)
            or now >= deadline.expires_at_monotonic
        ):
            raise TimeoutError(f"V5 {stage} deadline exceeded")

    def _require_inputs(self, inputs: FeedbackRoundInputV5) -> None:
        if (
            type(inputs) is not FeedbackRoundInputV5
            or inputs.manifest != self._manifest
            or inputs.panel_plan != self._panel_plan
            or inputs.evaluator_contract != self._contract
            or inputs.baseline != self._baseline
            or inputs.round_index != self._owner.round_index
            or inputs.owner_token_sha256 != self._owner.owner_token_sha256
        ):
            raise ValueError("candidate-base round authority differs")

    @staticmethod
    def _source_matches_revision(
        source: SourceBundleV5,
        revision: PolicyRevisionIdentityV5,
    ) -> bool:
        return tuple((item.path, item.sha256) for item in source.files) == (revision.editable_source_sha256)

    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5:
        if type(parent) is not ParentCandidateV5:
            raise ValueError("candidate parent is invalid")
        source = self._repository.load_typed_artifact(
            parent.source_bundle_ref,
            value_type=SourceBundleV5,
        )
        if source.sha256 != parent.source_bundle_ref.sha256 or not self._source_matches_revision(
            source, parent.policy_revision
        ):
            raise ValueError("candidate parent source authority differs")
        if parent.origin == "baseline":
            if (
                parent.policy_revision != self._baseline.policy_revision
                or parent.source_bundle_ref != self._baseline.source_bundle_ref
                or source != self._baseline.source_bundle
            ):
                raise ValueError("candidate baseline parent authority differs")
        else:
            durable = self._repository.load_typed_state(
                namespace="policy-source",
                key=parent.policy_revision.sha256,
                value_type=SourceBundleV5,
            )
            if durable != source:
                raise ValueError("candidate archive parent source is not durable")
        return source

    def _parent_source_and_revision(
        self,
        identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
    ) -> tuple[SourceBundleV5, PolicyRevisionIdentityV5]:
        if identity.parent_revision_sha256 == self._baseline.policy_revision.sha256:
            return self._baseline.source_bundle, self._baseline.policy_revision
        source = self._repository.load_typed_state(
            namespace="policy-source",
            key=identity.parent_revision_sha256,
            value_type=SourceBundleV5,
        )
        if source is None:
            raise ValueError("candidate archive parent source is unavailable")
        revision = derive_policy_revision_identity_v5(
            source_bundle=source,
            trusted_policy_runtime_sha256=(variant.policy_revision.trusted_policy_runtime_sha256),
            immutable_constraints_sha256=(variant.policy_revision.immutable_constraints_sha256),
        )
        if revision.sha256 != identity.parent_revision_sha256:
            raise ValueError("candidate archive parent revision differs")
        return source, revision

    def _require_identity(
        self,
        inputs: FeedbackRoundInputV5,
        identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
    ) -> tuple[SourceBundleV5, PolicyRevisionIdentityV5]:
        self._require_inputs(inputs)
        if (
            type(identity) is not ExperimentIdentityV5
            or type(variant) is not RenderedVariantV5
            or identity.policy_revision_sha256 != variant.policy_revision.sha256
            or identity.assignment_sha256 != variant.assignment.sha256
            or identity.round_index != self._owner.round_index
            or identity.discovery_plan_sha256 != self._panel_plan.discovery_plan_sha256
        ):
            raise ValueError("candidate experiment identity differs")
        return self._parent_source_and_revision(identity, variant)

    def _source_reference(self, variant: RenderedVariantV5) -> ArtifactRefV5:
        reference = self._repository.append_typed_state(
            namespace="policy-source",
            key=variant.policy_revision.sha256,
            value=variant.source_bundle,
        )
        if reference.sha256 != variant.source_bundle.sha256:
            raise ValueError("candidate source persistence differs")
        return reference

    def materialize(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        self._check_deadline(deadline, "materialization")
        parent_source, parent_revision = self._require_identity(
            inputs,
            experiment_identity,
            variant,
        )
        source_ref = self._source_reference(variant)
        materialized = (
            self._materializer.materialize(
                owner=self._owner,
                parent_revision=parent_revision,
                variant=variant,
            )
            if parent_revision == self._baseline.policy_revision
            else self._materializer.materialize_from_parent_source(
                owner=self._owner,
                source_revision=self._baseline.policy_revision,
                parent_revision=parent_revision,
                parent_source=parent_source,
                variant=variant,
            )
        )
        self._check_deadline(deadline, "materialization")
        return materialized.as_runtime_materialization(source_ref)

    def recover_materialized(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        leases: tuple[OwnedLeaseV5, ...],
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        self._check_deadline(deadline, "materialization")
        _parent_source, parent_revision = self._require_identity(
            inputs,
            experiment_identity,
            variant,
        )
        source = self._repository.load_typed_state(
            namespace="policy-source",
            key=variant.policy_revision.sha256,
            value_type=SourceBundleV5,
        )
        if source != variant.source_bundle:
            raise ValueError("candidate durable source differs")
        matching = tuple(
            item
            for item in leases
            if type(item) is OwnedLeaseV5
            and type(item.opaque_handle) is WorkspaceLeaseV5
            and item.opaque_handle.parent_revision_sha256 == parent_revision.sha256
            and item.opaque_handle.child_revision_sha256 == variant.policy_revision.sha256
        )
        if len(matching) != 1:
            raise ValueError("candidate workspace recovery authority is ambiguous")
        lease = matching[0].opaque_handle
        materialized = self._materializer.recover_materialized(
            owner=self._owner,
            source_revision=self._baseline.policy_revision,
            parent_revision=parent_revision,
            variant=variant,
            lease=lease,
        )
        self._check_deadline(deadline, "materialization")
        source_ref = ArtifactRefV5(
            f"adapter-state/policy-source/{variant.policy_revision.sha256}.json",
            source.sha256,
        )
        return materialized.as_runtime_materialization(source_ref)

    def validate(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> ValidationResultV5:
        self._check_deadline(deadline, "validation")
        if (
            type(materialized) is not MaterializedVariantV5
            or type(materialized.opaque_candidate) is not MaterializedWorkspaceV5
        ):
            raise ValueError("candidate materialization is invalid")
        workspace = materialized.opaque_candidate
        child_source = self._materializer.read_materialized_source(workspace)
        parent_source = (
            self._baseline.source_bundle
            if workspace.parent_revision == self._baseline.policy_revision
            else self._repository.load_typed_state(
                namespace="policy-source",
                key=workspace.parent_revision.sha256,
                value_type=SourceBundleV5,
            )
        )
        if parent_source is None or not self._source_matches_revision(
            parent_source,
            workspace.parent_revision,
        ):
            raise ValueError("candidate validation parent source differs")
        changed_symbols = derive_changed_symbols_v5(
            before=parent_source,
            after=child_source,
        )
        self._check_deadline(deadline, "validation")
        return ValidationResultV5(True, None, changed_symbols)

    def fingerprint(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> SemanticFingerprintV5:
        del materialized, deadline
        raise RuntimeError("semantic_probe_requires_registered_sandbox_execution")


class LocalRoleRequestFactoryV5:
    """Build exact bounded role requests from authenticated runtime projections."""

    def __init__(self, *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5) -> None:
        if (
            type(repository) is not LocalArtifactRepositoryV5
            or type(manifest) is not CampaignManifestV5
            or manifest.provider is None
        ):
            raise ValueError("local role-request factory authority is invalid")
        self._repository = repository
        self._manifest = manifest

    @property
    def repository(self) -> LocalArtifactRepositoryV5:
        return self._repository

    @property
    def manifest(self) -> CampaignManifestV5:
        return self._manifest

    def _investigator_parts(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
    ) -> tuple[InvestigatorRoleInputV5, RoleEvidenceV5]:
        if inputs.manifest != self._manifest:
            raise ValueError("investigator request manifest differs")
        evidence = _EvidenceBuilderV5("investigator")
        evaluator_ids: list[str] = []
        campaign = _parent_campaign(inputs, projection, parent)
        for episode in campaign.episodes:
            report = selected_scenario(episode.evaluation).report
            prefix = f"episode.{episode.episode_ordinal}"
            evaluator_ids.extend(
                (
                    evidence.add(
                        f"{prefix}.portfolio_annualized_return_pct",
                        report.portfolio_annualized_return_pct,
                    ),
                    evidence.add(f"{prefix}.max_drawdown_pct", report.max_drawdown_pct),
                    evidence.add(f"{prefix}.closed_trades", report.closed_trades),
                    evidence.add(f"{prefix}.average_exposure_pct", report.average_exposure_pct),
                )
            )

        families: list[ArchiveFamilyAggregateV5] = []
        for mechanism in PRIMARY_MECHANISMS_V5:
            entries = tuple(item for item in projection.state.archive.entries if item.primary_mechanism == mechanism)
            if not entries:
                continue
            leader = entries[0]
            ids = (
                evidence.add("archive.family.candidate_count", len(entries)),
                evidence.add("archive.family.leader_cagr_pct", leader.campaign_cagr_pct),
            )
            families.append(
                ArchiveFamilyAggregateV5(
                    mechanism,
                    len(entries),
                    leader.policy_identity_sha256,
                    ids,
                )
            )

        directions: list[CriticDirectionAggregateV5] = []
        completed = tuple(stored for stored in projection.stored_records if stored.record.critic_review is not None)[
            -12:
        ]
        for stored in completed:
            record = stored.record
            review = record.critic_review
            assert review is not None
            evidence_id = evidence.add("archive.critic.target_gap_pct", record.target_gap_pct)
            directions.append(
                CriticDirectionAggregateV5(
                    record.experiment_id,
                    record.hypothesis.primary_mechanism,
                    review.prediction_vs_observation,
                    review.causal_explanation,
                    review.disposition,
                    review.next_direction,
                    (evidence_id,),
                )
            )
        role_input = InvestigatorRoleInputV5(
            tuple(evaluator_ids),
            tuple(families),
            tuple(directions),
        )
        return role_input, evidence.build()

    def investigator_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
    ) -> RoleRequestV5:
        role_input, evidence = self._investigator_parts(inputs, projection, parent)
        return build_role_request_v5(
            role="investigator",
            role_input=role_input,
            issued_evidence=evidence,
            expected_binding=RoleBindingV5(
                parent.policy_identity_sha256,
                None,
                (),
                inputs.panel_plan.discovery_plan_sha256,
            ),
            schema_authority=role_schema_authority_from_manifest_v5(
                role="investigator",
                manifest=inputs.manifest,
            ),
            max_output_tokens=inputs.manifest.provider.maximum_output_tokens_per_role,  # type: ignore[union-attr]
        )

    def author_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        decision: ScheduledHypothesisV5,
        investigator: InvestigatorArtifactV5,
    ) -> RoleRequestV5:
        if decision.hypothesis not in investigator.hypotheses:
            raise ValueError("author decision is absent from investigator authority")
        _investigator_input, investigator_evidence = self._investigator_parts(
            inputs,
            projection,
            decision.parent,
        )
        by_id = {item.evidence_id: item for item in investigator_evidence.items}
        try:
            cited = tuple(by_id[item] for item in decision.hypothesis.evidence_ids)
        except KeyError:
            raise ValueError("author hypothesis cites unavailable evidence") from None
        source = self._repository.load_typed_artifact(
            decision.parent.source_bundle_ref,
            value_type=SourceBundleV5,
        )
        if tuple((item.path, item.sha256) for item in source.files) != (
            decision.parent.policy_revision.editable_source_sha256
        ):
            raise ValueError("author source differs from parent revision")
        full_source_allowed = inputs.manifest.search.allow_full_source_escape
        if full_source_allowed or decision.hypothesis.primary_mechanism == "cross_policy":
            paths = EDITABLE_POLICY_PATHS_V5
        else:
            path_by_mechanism = {
                "entry": EDITABLE_POLICY_PATHS_V5[0],
                "risk_sizing": EDITABLE_POLICY_PATHS_V5[1],
                "position_management": EDITABLE_POLICY_PATHS_V5[2],
                "exit": EDITABLE_POLICY_PATHS_V5[3],
            }
            paths = (path_by_mechanism[decision.hypothesis.primary_mechanism],)
        sources = tuple(item for item in source.files if item.path in paths)
        return build_role_request_v5(
            role="author",
            role_input=AuthorRoleInputV5(
                sources,
                full_source_allowed,
                decision.hypothesis,
                AuthorPolicyContractsV5(
                    decision.parent.policy_revision,
                    inputs.manifest.policy_scope_ref.sha256,
                ),
            ),
            issued_evidence=RoleEvidenceV5(5, cited),
            expected_binding=RoleBindingV5(
                decision.parent.policy_identity_sha256,
                decision.hypothesis.hypothesis_id,
                (),
                inputs.panel_plan.discovery_plan_sha256,
            ),
            schema_authority=role_schema_authority_from_manifest_v5(
                role="author",
                manifest=inputs.manifest,
                author_policy_paths=paths,
                full_source_escape=full_source_allowed,
            ),
            max_output_tokens=inputs.manifest.provider.maximum_output_tokens_per_role,  # type: ignore[union-attr]
        )

    @staticmethod
    def _ordered_scenarios(panel: PanelEvaluationV5):
        by_id = {item.scenario_id: item for item in panel.scenarios}
        try:
            return tuple(by_id[item] for item in ("gross", "base", "stress"))
        except KeyError:
            raise ValueError("critic panel lacks the canonical scenario grid") from None

    def critic_request(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        decision: ScheduledHypothesisV5,
        candidates: tuple[CandidateEvidenceV5, ...],
    ) -> RoleRequestV5:
        if not candidates:
            raise ValueError("critic request requires testable candidates")
        experiment_ids = tuple(item.experiment_id for item in candidates)
        if len(set(experiment_ids)) != len(experiment_ids):
            raise ValueError("critic candidate identities are not unique")
        parent_fingerprint = _parent_fingerprint(inputs, projection, decision.parent)
        evidence = _EvidenceBuilderV5("critic")
        evaluations: list[ExperimentEvaluationAggregateV5] = []
        semantic_rows: list[ExperimentSemanticDifferenceAggregateV5] = []
        failures: list[ExperimentFailureAggregateV5] = []
        for candidate in candidates:
            scenarios: list[ScenarioAggregateV5] = []
            if candidate.quick_evidence is not None:
                scenario = selected_scenario(candidate.quick_evidence)
                evidence_id = evidence.add(
                    "quick.portfolio_annualized_return_pct",
                    scenario.report.portfolio_annualized_return_pct,
                )
                scenarios.append(
                    ScenarioAggregateV5(
                        "quick",
                        None,
                        scenario.scenario_id,
                        (evidence_id,),
                    )
                )
            for episode in candidate.discovery_episodes:
                for scenario in self._ordered_scenarios(episode.evaluation):
                    evidence_id = evidence.add(
                        "episode.portfolio_annualized_return_pct",
                        scenario.report.portfolio_annualized_return_pct,
                    )
                    scenarios.append(
                        ScenarioAggregateV5(
                            "discovery_episode",
                            episode.episode_ordinal,
                            scenario.scenario_id,
                            (evidence_id,),
                        )
                    )
            evaluations.append(
                ExperimentEvaluationAggregateV5(
                    candidate.experiment_id,
                    candidate.status,  # type: ignore[arg-type]
                    tuple(scenarios),
                )
            )

        for candidate in candidates:
            fingerprint = candidate.semantic_fingerprint
            if fingerprint is None:
                raise ValueError("testable critic candidate lacks semantic evidence")
            comparison = classify_semantic_fingerprints_v5(parent_fingerprint, fingerprint)
            evidence_id = evidence.add(
                "semantic.differing_decision_count",
                len(comparison.differing_probe_ids),
            )
            semantic_rows.append(
                ExperimentSemanticDifferenceAggregateV5(
                    candidate.experiment_id,
                    parent_fingerprint.fingerprint_sha256,
                    fingerprint.fingerprint_sha256,
                    comparison.classification,
                    len(comparison.differing_probe_ids),
                    (evidence_id,),
                )
            )

        for candidate in candidates:
            if candidate.status in {"evaluated", "zero_trade", "quick_rejected"}:
                stage = "none"
                failure_code = None
            elif candidate.quick_evidence is None:
                stage = "quick_evaluation"
                failure_code = candidate.failure_code or candidate.status
            else:
                stage = "campaign_evaluation"
                failure_code = candidate.failure_code or candidate.status
            evidence_id = evidence.add(f"failure.{stage}.count", 0 if stage == "none" else 1)
            failures.append(
                ExperimentFailureAggregateV5(
                    candidate.experiment_id,
                    stage,  # type: ignore[arg-type]
                    failure_code,
                    (evidence_id,),
                )
            )

        predictions = tuple(
            ExperimentPredictionAggregateV5(
                candidate.experiment_id,
                tuple(
                    MetricPredictionV5(item.metric_id, item.direction, item.rationale)
                    for item in decision.hypothesis.predicted_changes
                ),
            )
            for candidate in candidates
        )
        return build_role_request_v5(
            role="critic",
            role_input=CriticRoleInputV5(
                tuple(evaluations),
                predictions,
                tuple(semantic_rows),
                tuple(failures),
            ),
            issued_evidence=evidence.build(),
            expected_binding=RoleBindingV5(
                decision.parent.policy_identity_sha256,
                decision.hypothesis.hypothesis_id,
                experiment_ids,
                inputs.panel_plan.discovery_plan_sha256,
            ),
            schema_authority=role_schema_authority_from_manifest_v5(
                role="critic",
                manifest=inputs.manifest,
            ),
            max_output_tokens=inputs.manifest.provider.maximum_output_tokens_per_role,  # type: ignore[union-attr]
        )


class SelectionNoveltyResolverV5:
    def resolve(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
        parent: ParentCandidateV5,
        request: RoleRequestV5,
        package: RoleInvocationPackageV5,
        artifact: InvestigatorArtifactV5,
    ):
        outcome = select_novel_hypothesis_v5(
            state=projection.state,
            parent=parent,
            artifact=artifact,
            capabilities=inputs.manifest.search,
        )
        if type(outcome) is ScheduledHypothesisV5:
            return outcome
        after = advance_no_novel_parent_v5(
            projection.state,
            outcome,
            artifact=artifact,
            capabilities=inputs.manifest.search,
        )
        return NoNovelHypothesisAuthorityV5(
            "no_novel_hypothesis",
            inputs.panel_plan.discovery_plan_sha256,
            canonical_sha256_v5(projection.state.to_primitive()),
            canonical_sha256_v5(after.to_primitive()),
            parent.policy_identity_sha256,
            request.sha256,
            request.role_evidence.sha256,
            (package.attempt.sha256,),
            canonical_sha256_v5(artifact),
            canonical_sha256_v5(outcome.to_primitive()),
        )


class CanonicalExperimentRecordFactoryV5:
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
    ) -> tuple[ExperimentRecordV5, ...]:
        reviews = {item.experiment_id: item for item in critic.reviews}
        expected_ids = tuple(item.experiment_id for item in candidates if is_testable_experiment_status_v5(item.status))
        if tuple(reviews) != expected_ids:
            raise ValueError("critic reviews differ from finalized experiment order")
        records: list[ExperimentRecordV5] = []
        for candidate in candidates:
            testable = is_testable_experiment_status_v5(candidate.status)
            campaign = candidate.campaign_evidence
            records.append(
                ExperimentRecordV5(
                    candidate.experiment_id,
                    candidate.experiment_identity,
                    inputs.round_index,
                    parent.policy_identity_sha256,
                    parent.semantic_fingerprint_sha256,
                    decision.hypothesis,
                    template,
                    template.sha256,
                    candidate.materialized.variant.assignment,
                    (
                        None
                        if type(candidate.experiment_identity) is PreValidationInvalidExperimentIdentityV5
                        else candidate.materialized.variant.policy_revision
                    ),
                    candidate.semantic_fingerprint,
                    candidate.status,  # type: ignore[arg-type]
                    candidate.validation,
                    candidate.quick_evidence,
                    candidate.discovery_episodes,
                    campaign,
                    (
                        None
                        if campaign is None
                        else expected_target_gap_pct_v5(
                            target=inputs.manifest.target,
                            campaign_cagr=campaign.campaign_cagr_pct,
                        )
                    ),
                    reviews[candidate.experiment_id] if testable else None,
                    candidate.artifact_refs,
                    critic_ref if testable else None,
                )
            )
        return tuple(records)


class LocalArchiveReducerFactoryV5:
    def __init__(self, repository: LocalArtifactRepositoryV5) -> None:
        if type(repository) is not LocalArtifactRepositoryV5:
            raise ValueError("archive reducer repository is invalid")
        self._repository = repository

    @property
    def repository(self) -> LocalArtifactRepositoryV5:
        return self._repository

    def _authorities(
        self,
        records: tuple[StoredExperimentRecordV5, ...],
    ) -> tuple[ArchiveRecordAuthorityV5, ...]:
        result: list[ArchiveRecordAuthorityV5] = []
        for stored in records:
            record = stored.record
            if record.status not in {"evaluated", "zero_trade"}:
                continue
            if record.policy_revision is None:
                raise ValueError("archive-eligible record lacks a policy revision")
            source = self._repository.load_typed_state(
                namespace="policy-source",
                key=record.policy_revision.sha256,
                value_type=SourceBundleV5,
            )
            if source is None:
                raise ValueError("archive-eligible source authority is unavailable")
            source_ref = ArtifactRefV5(
                f"adapter-state/policy-source/{record.policy_revision.sha256}.json",
                source.sha256,
            )
            if (
                source_ref not in record.artifact_refs
                or tuple((item.path, item.sha256) for item in source.files)
                != record.policy_revision.editable_source_sha256
            ):
                raise ValueError("archive source differs from its experiment record")
            result.append(
                ArchiveRecordAuthorityV5(
                    record.experiment_id,
                    stored.reference,
                    source,
                    source_ref,
                )
            )
        return tuple(sorted(result, key=lambda item: item.experiment_id))

    def recovery_reducer(self, inputs: FeedbackRoundInputV5) -> CandidateArchiveReducerV5:
        checkpoint = self._repository.load_checkpoint()
        records = (
            ()
            if checkpoint is None
            else tuple(
                StoredExperimentRecordV5(reference, self._repository.load_experiment(reference))
                for reference in checkpoint.record_refs
            )
        )
        return CandidateArchiveReducerV5(
            inputs.panel_plan,
            inputs.evaluator_contract,
            inputs.manifest.target,
            self._authorities(records),
            inputs.manifest.search.archive_capacity,
        )

    def verify_projection(
        self,
        *,
        manifest: CampaignManifestV5,
        panel_plan: CampaignPanelPlanV5,
        evaluator_contract: EvaluatorContractV5,
    ) -> None:
        """Authenticate the committed checkpoint/archive without repairing either file."""

        if (
            type(manifest) is not CampaignManifestV5
            or type(panel_plan) is not CampaignPanelPlanV5
            or type(evaluator_contract) is not EvaluatorContractV5
            or manifest.panel_plan_ref.sha256 != panel_plan.sha256
            or manifest.evaluator_contract_ref.sha256 != evaluator_contract.sha256
        ):
            raise ValueError("archive verification authority is invalid")
        checkpoint = self._repository.load_checkpoint()
        if checkpoint is None:
            return
        records = tuple(
            StoredExperimentRecordV5(
                reference,
                self._repository.load_experiment(reference),
            )
            for reference in checkpoint.record_refs
        )
        reducer = CandidateArchiveReducerV5(
            panel_plan,
            evaluator_contract,
            manifest.target,
            self._authorities(records),
            manifest.search.archive_capacity,
        )
        state = reduce_experiment_journal_v5(
            tuple(item.record for item in records),
            reducer,
        )
        snapshot = ArchiveSnapshotV5(
            checkpoint.generation,
            checkpoint.record_refs,
            reducer.to_primitive(state),
        )
        raw = canonical_json_bytes_v5(snapshot.to_primitive())
        archive_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
        authenticated = self._repository.authenticate(archive_ref)
        if authenticated.content != raw:
            raise ValueError("archive projection differs from checkpoint authority")

    def publication_reducer(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        prior: SearchProjectionV5,
        records: tuple[StoredExperimentRecordV5, ...],
        source_authorities: tuple[ArchiveRecordAuthorityV5, ...],
    ) -> CandidateArchiveReducerV5:
        expected_new = self._authorities(records)
        if expected_new != source_authorities:
            raise ValueError("publication source authorities differ from stored records")
        authorities = tuple(
            sorted(
                {
                    item.experiment_id: item for item in (*self._authorities(prior.stored_records), *source_authorities)
                }.values(),
                key=lambda item: item.experiment_id,
            )
        )
        return CandidateArchiveReducerV5(
            inputs.panel_plan,
            inputs.evaluator_contract,
            inputs.manifest.target,
            authorities,
            inputs.manifest.search.archive_capacity,
        )


class SystemMonotonicClockV5:
    def monotonic(self) -> float:
        return float(time.monotonic())


class ControllerCancellationV5:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class CompositeOwnedCleanupV5:
    owner: WorkspaceOwnerV5
    materializer: GitCandidateMaterializerV5
    executor: LocalContainerExecutorV5
    clock: SystemMonotonicClockV5

    def __post_init__(self) -> None:
        if (
            type(self.owner) is not WorkspaceOwnerV5
            or type(self.materializer) is not GitCandidateMaterializerV5
            or type(self.executor) is not LocalContainerExecutorV5
            or type(self.clock) is not SystemMonotonicClockV5
        ):
            raise ValueError("composite cleanup authority is invalid")

    def recover_lease(
        self,
        payload: ResourceLeasePayloadV5,
        *,
        round_index: int,
    ) -> OwnedLeaseV5:
        if payload.resource_kind == "workspace":
            lease = self.materializer.driver.load_lease(payload.lease_id)
            if (
                type(lease) is not WorkspaceLeaseV5
                or lease.payload != payload
                or lease.owner != self.owner
                or round_index != self.owner.round_index
            ):
                raise ValueError("workspace cleanup lease is absent or foreign")
            return lease.as_owned_lease()
        if payload.resource_kind in {"evaluator_process", "container"}:
            return self.executor.recover_lease(payload, round_index=round_index)
        raise ValueError("unsupported V5 cleanup lease kind")

    def _check_deadline(self, deadline: StageDeadlineV5) -> None:
        now = self.clock.monotonic()
        if (
            type(deadline) is not StageDeadlineV5
            or deadline.stage != "cleanup"
            or not math.isfinite(now)
            or now >= deadline.expires_at_monotonic
        ):
            raise TimeoutError("V5 cleanup deadline exceeded")

    def cleanup(
        self,
        leases: tuple[OwnedLeaseV5, ...],
        *,
        deadline: StageDeadlineV5,
    ) -> CleanupResultPayloadV5:
        if type(leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in leases):
            raise ValueError("composite cleanup leases are invalid")
        self._check_deadline(deadline)
        container_leases = tuple(
            item for item in leases if item.payload.resource_kind in {"evaluator_process", "container"}
        )
        workspace_leases = tuple(item for item in leases if item.payload.resource_kind == "workspace")
        if len(container_leases) + len(workspace_leases) != len(leases):
            raise ValueError("composite cleanup contains an unsupported lease")
        container_result = self.executor.cleanup_many(owner=self.owner, leases=container_leases)
        self._check_deadline(deadline)
        workspace_count = 0
        for owned in workspace_leases:
            lease = owned.opaque_handle
            if type(lease) is not WorkspaceLeaseV5 or lease.payload != owned.payload:
                raise ValueError("workspace cleanup capability is foreign")
            self.materializer.cleanup(owner=self.owner, lease=lease)
            workspace_count += 1
            self._check_deadline(deadline)
        return CleanupResultPayloadV5(
            workspace_count,
            0,
            container_result.owned_evaluators,
            container_result.owned_containers,
            container_result.cleanup_complete,
            container_result.failure_code,
        )


__all__ = [
    "CanonicalExperimentRecordFactoryV5",
    "CompositeOwnedCleanupV5",
    "ControllerCancellationV5",
    "LocalArchiveReducerFactoryV5",
    "LocalCandidateBaseOperationsV5",
    "LocalRoleRequestFactoryV5",
    "SelectionNoveltyResolverV5",
    "SystemMonotonicClockV5",
]
