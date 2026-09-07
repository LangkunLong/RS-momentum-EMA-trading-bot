"""Bounded synthetic, provider-free composition through the normal V5 runtime.

The fixture entry constant is an explicit synthetic baseline contract. This
adapter never imports or executes candidate source, materializes Git, or reads
market rows. Its metrics are synthetic and cannot authorize later stages.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    SourceBundleV5,
    SourceOperationV5,
    StructuralTemplateV5,
    derive_changed_symbols_v5,
)
from core.pit_optimizer_v5.contracts import (
    CampaignManifestV5,
    CriticArtifactV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
    PanelEvaluationV5,
    ScenarioPanelEvaluationV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    validate_episode_plan_panel_v5,
)
from core.pit_optimizer_v5.manifest import AuthenticatedCampaignManifestV5
from core.pit_optimizer_v5.memory import CleanupResultPayloadV5, ResourceLeasePayloadV5, RoleCompletionPayloadV5
from core.pit_optimizer_v5.probes import ProbeObservationV5, SemanticFingerprintV5
from core.pit_optimizer_v5.production_runtime import (
    CanonicalExperimentRecordFactoryV5,
    ControllerCancellationV5,
    LocalArchiveReducerFactoryV5,
    LocalRoleRequestFactoryV5,
    SelectionNoveltyResolverV5,
    SystemMonotonicClockV5,
)
from core.pit_optimizer_v5.provider import (
    ExistingPersistedRoleRequestV5,
    FixtureRoleRunnerV5,
    FixtureRoleTerminalAuthorityV5,
    FreshPersistedRoleRequestV5,
    RoleFailureV5,
    RoleInvocationPackageV5,
    RoleRunnerV5,
    canonical_role_response_sha256_v5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundDependenciesV5,
    FeedbackRoundInputV5,
    MaterializedVariantV5,
    OwnedLeaseV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.search import annualized_return_pct
from core.strategy_policy.contracts import EntryDecision


FIXTURE_ENTRY_CONSTANT_V5 = "FIXTURE_ENTRY_THRESHOLD"
FIXTURE_VARIANT_VALUES_V5 = (1, 2, 3)
FIXTURE_CRITIC_EXPLANATION_V5 = "Synthetic entry thresholds produced distinct decisions across the fixed probes."
FIXTURE_NEXT_DIRECTION_V5 = "Next investigate entry selectivity against the restored synthetic exposure evidence."
_ENTRY_PATH = "core/strategy_policy/v3/entry.py"
_SYMBOL = "core.strategy_policy.v3.entry.FIXTURE_ENTRY_THRESHOLD"


def _entry_value(source: SourceBundleV5) -> int:
    return _entry_source_value(source.files[0].source)


def _entry_source_value(source: str) -> int:
    tree = ast.parse(source)
    matches = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == FIXTURE_ENTRY_CONSTANT_V5
    ]
    if len(matches) != 1 or not isinstance(matches[0], ast.Constant) or type(matches[0].value) is not int:
        raise ValueError("fixture baseline requires its explicit integer entry constant")
    return matches[0].value


def _response(request):
    """Return canonical local content bound to the exact issued role evidence."""
    ids = (request.role_evidence.items[0].evidence_id,)
    if request.role == "investigator":
        artifact = InvestigatorArtifactV5(
            tuple(
                HypothesisV5(
                    f"fixture-entry-{rank}",
                    rank,
                    "entry",
                    f"Synthetic entry selectivity mechanism {rank} changes exposure.",
                    (MetricPredictionV5("average_exposure_pct", "increase", "Synthetic threshold response."),),
                    ids,
                    "Replace the declared fixture entry constant using its three values.",
                )
                for rank in (1, 2, 3)
            )
        )
    elif request.role == "author":
        prior_value = _entry_source_value(request.role_input.editable_sources[0].source)
        values = tuple(prior_value + value for value in FIXTURE_VARIANT_VALUES_V5)
        artifact = StructuralTemplateV5(
            request.expected_binding.hypothesis_id,
            request.expected_binding.parent_revision_sha256,
            (_SYMBOL,),
            (
                SourceOperationV5(
                    _ENTRY_PATH,
                    FIXTURE_ENTRY_CONSTANT_V5,
                    "replace_constant",
                    'FIXTURE_ENTRY_THRESHOLD = PIT_AXIS("entry_threshold")\n',
                ),
            ),
            (LiteralAxisV5("entry_threshold", values[0], values),),
            None,
        )
    else:
        artifact = CriticArtifactV5(
            tuple(
                CriticReviewV5(
                    experiment,
                    "Synthetic activity follows the declared threshold.",
                    FIXTURE_CRITIC_EXPLANATION_V5,
                    ids,
                    "refine",
                    FIXTURE_NEXT_DIRECTION_V5,
                )
                for experiment in request.expected_binding.experiment_ids
            ),
            FIXTURE_CRITIC_EXPLANATION_V5,
            FIXTURE_NEXT_DIRECTION_V5,
            ids,
        )
    if request.role == "author":
        artifact = artifact.to_primitive()
        artifact["axes"] = (
            {
                "name": "entry_threshold",
                "default": {"kind": "int", "value": values[0]},
                "values": tuple({"kind": "int", "value": value} for value in values),
            },
        )
    return canonical_json_bytes_v5({"binding": request.expected_binding.to_primitive(), "artifact": artifact}).decode()


class FixtureRoleInvokerV5:
    """Adapt a deterministic RoleRunnerV5 to the persisted runtime role boundary."""

    def __init__(self, manifest):
        if (
            type(manifest) is not CampaignManifestV5
            or manifest.provider is not None
            or manifest.pit_data_scope != "production"
        ):
            raise ValueError("fixture roles require exact provider-free authority")
        self.runner: RoleRunnerV5 = FixtureRoleRunnerV5(responses={}, campaign_fixture=True)
        self._manifest = manifest

    def _invoke(self, persisted):
        if (
            persisted.call.campaign_id != self._manifest.campaign_id
            or persisted.call.attempt_kind != "primary"
            or persisted.call.attempt_index != 1
            or persisted.call.round_index not in (1, 2)
        ):
            raise ValueError("fixture role campaign differs")
        # One primary slot per role in each round. Reconciliation constructs a
        # fresh deterministic runner, so it never consumes an extra role slot.
        runner = self.runner
        artifact = None
        try:
            artifact = runner.invoke_once(persisted.request)
        except RoleFailureV5:
            pass
        attempt = runner.attempts[-1]
        attempt = replace(attempt, attempt_index=persisted.call.attempt_index)
        terminal = FixtureRoleTerminalAuthorityV5(
            "fixture-v5",
            persisted.call.sha256,
            persisted.request.sha256,
            attempt.sha256,
            attempt.artifact_sha256,
        )
        return RoleInvocationPackageV5(persisted.call, persisted.request, attempt, terminal, artifact)

    def invoke_once(self, persisted_request, *, deadline_monotonic):
        if type(persisted_request) is not FreshPersistedRoleRequestV5:
            raise ValueError("fixture invocation requires a fresh persisted request")
        return self._invoke(persisted_request)

    def reconcile_once(self, persisted_request):
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise ValueError("fixture recovery requires an existing persisted request")
        return FixtureRoleInvokerV5(self._manifest)._invoke(persisted_request)


class SyntheticCandidateRuntimeV5:
    """Pure candidate/evaluator fake with explicit synthetic workspace ownership."""

    def __init__(self, repository, inputs):
        self.repository, self.inputs = repository, inputs
        self.active = {}
        self.evaluations = []
        self.mechanics = []
        self.parent_source = None

    def load_parent_source(self, parent):
        self.parent_source = self.repository.load_typed_artifact(parent.source_bundle_ref, value_type=SourceBundleV5)
        return self.parent_source

    def materialize(self, *, inputs, experiment_identity, variant, deadline):
        if inputs != self.inputs:
            raise ValueError("fixture materialization authority differs")
        ref = self.repository.append_typed_state(
            namespace="policy-source", key=variant.policy_revision.sha256, value=variant.source_bundle
        )
        payload = ResourceLeasePayloadV5(
            "fixture-" + experiment_identity.sha256, "workspace", inputs.campaign_id, inputs.owner_token_sha256
        )
        lease = OwnedLeaseV5(payload, inputs.round_index, payload.lease_id)
        self.active[payload.lease_id] = lease
        return MaterializedVariantV5(variant, ref, (lease,), variant.source_bundle.sha256)

    def recover_materialized(self, *, inputs, experiment_identity, variant, leases, deadline):
        materialized = self.materialize(
            inputs=inputs, experiment_identity=experiment_identity, variant=variant, deadline=deadline
        )
        if materialized.leases != leases:
            raise ValueError("fixture recovered leases differ")
        return materialized

    def validate(self, materialized, *, deadline):
        _entry_value(materialized.variant.source_bundle)
        changed = derive_changed_symbols_v5(before=self.parent_source, after=materialized.variant.source_bundle)
        self.mechanics.append(
            (materialized.variant.policy_revision.sha256, self.inputs.panel_plan.mechanics.panel_ref.sha256)
        )
        return ValidationResultV5(True, None, changed)

    def fingerprint(self, materialized, *, deadline):
        value = _entry_value(materialized.variant.source_bundle)
        observations = []
        baseline = self.inputs.baseline.semantic_fingerprint
        for item in baseline.observations:
            if item.method == "evaluate_entry":
                decision = EntryDecision.from_canonical_json(item.decision_json.decode())
                decision = replace(decision, rank=(float(value), decision.rank[1]))
                item = ProbeObservationV5(
                    item.probe_id, item.method, item.input_sha256, decision.to_canonical_json().encode()
                )
            observations.append(item)
        observations = tuple(observations)
        digest = hashlib.sha256(
            canonical_json_bytes_v5(
                {
                    "suite_id": baseline.suite_id,
                    "observations": [item.to_primitive() for item in observations],
                }
            )
        ).hexdigest()
        return SemanticFingerprintV5(baseline.suite_id, observations, digest)

    def _evaluate(self, materialized, episode, scenarios):
        value = _entry_value(materialized.variant.source_bundle)
        days = (date.fromisoformat(episode.end_date) - date.fromisoformat(episode.start_date)).days
        baseline_report = self.inputs.baseline.campaign.episodes[0].evaluation.scenarios[0].report
        evidence = []
        for scenario in scenarios:
            total = Decimal(5 + value - {"gross": 0, "base": 1, "stress": 2}[scenario])
            start, end = Decimal(100), Decimal(100) + total
            report = replace(
                baseline_report,
                portfolio_total_return_pct=total,
                portfolio_annualized_return_pct=annualized_return_pct(
                    starting_equity=start, ending_equity=end, days=days
                ),
                closed_trades=value + 1,
                average_exposure_pct=Decimal(20 + value),
                average_cash_pct=Decimal(80 - value),
            )
            evidence.append(ScenarioPanelEvaluationV5(scenario, start, end, report))
            self.evaluations.append((materialized.variant.policy_revision.sha256, episode.episode_id, scenario))
        return PanelEvaluationV5(
            self.inputs.evaluator_contract.sha256,
            self.inputs.evaluator_contract.sandbox_profile_sha256,
            episode.panel_ref.sha256,
            materialized.variant.policy_revision.sha256,
            episode.start_date,
            episode.end_date,
            days,
            "base",
            tuple(evidence),
        )

    def evaluate_quick(self, materialized, *, deadline):
        return self._evaluate(materialized, self.inputs.panel_plan.quick, ("base",))

    def evaluate_episode(self, materialized, episode, *, deadline):
        return EpisodeEvaluationV5(
            episode.episode_id,
            episode.episode_ordinal,
            episode.start_date,
            episode.end_date,
            self._evaluate(materialized, episode, ("gross", "base", "stress")),
        )

    def recover_lease(self, payload, *, round_index):
        if (
            payload.owner_campaign_id != self.inputs.campaign_id
            or payload.owner_token_sha256 != self.inputs.owner_token_sha256
        ):
            raise ValueError("fixture lease is foreign")
        lease = OwnedLeaseV5(payload, round_index, payload.lease_id)
        self.active[payload.lease_id] = lease
        return lease

    def cleanup(self, leases, *, deadline):
        for lease in leases:
            if self.active.get(lease.payload.lease_id) != lease:
                raise ValueError("fixture cleanup lease is foreign")
            del self.active[lease.payload.lease_id]
        return CleanupResultPayloadV5(len(leases), 0, 0, 0, not self.active)


def compose_fixture_round_v5(*, repository, authorities, round_index):
    """Compose only the authenticated provider-free synthetic fixture contract."""
    if type(repository) is not LocalArtifactRepositoryV5 or type(authorities) is not AuthenticatedCampaignManifestV5:
        raise ValueError("fixture composition requires authenticated V5 authorities")
    manifest = authorities.manifest
    if (
        manifest.provider is not None
        or manifest.pit_data_scope != "production"
        or manifest.sha256 != authorities.manifest_ref.sha256
        or manifest.search.hypotheses_per_investigator != 3
        or manifest.search.max_variants_per_template < 3
        or manifest.search.max_discovery_survivors_per_template < 3
        or manifest.search.max_feedback_rounds < 2
        or round_index not in (1, 2)
    ):
        raise ValueError("fixture manifest authority is incompatible")
    if _entry_value(authorities.baseline_authority.source_bundle) != 0:
        raise ValueError("fixture baseline entry constant must be zero")
    for episode in (authorities.panel_plan.mechanics, authorities.panel_plan.quick, *authorities.panel_plan.discovery):
        validate_episode_plan_panel_v5(episode, repository.load_evaluation_panel_spec(episode.panel_ref))
    for reference, value in (
        (authorities.manifest_ref, manifest),
        (manifest.panel_plan_ref, authorities.panel_plan),
        (manifest.execution_profile_ref, authorities.execution_profile),
        (manifest.evaluator_contract_ref, authorities.evaluator_contract),
        (manifest.baseline_authority_ref, authorities.baseline_authority),
        (manifest.baseline_policy_revision_ref, authorities.baseline_policy_revision),
        (manifest.policy_scope_ref, authorities.policy_scope),
        (manifest.sandbox_profile_ref, authorities.sandbox_profile),
    ):
        if repository.load_typed_artifact(reference, value_type=type(value)) != value:
            raise ValueError("fixture authority differs from its authenticated reference")
    inputs = FeedbackRoundInputV5(
        manifest,
        authorities.panel_plan,
        authorities.evaluator_contract,
        authorities.baseline_authority,
        round_index,
        canonical_sha256_v5({"fixture": manifest.sha256, "round": round_index}),
    )
    candidates = SyntheticCandidateRuntimeV5(repository, inputs)
    dependencies = FeedbackRoundDependenciesV5(
        repository,
        FixtureRoleInvokerV5(manifest),
        LocalRoleRequestFactoryV5(repository=repository, manifest=manifest),
        SelectionNoveltyResolverV5(),
        candidates,
        CanonicalExperimentRecordFactoryV5(),
        LocalArchiveReducerFactoryV5(repository),
        SystemMonotonicClockV5(),
        ControllerCancellationV5(),
        candidates,
    )
    return inputs, dependencies


def verify_fixture_run_v5(*, repository, manifest):
    """Authenticate synthetic role/lease history without production adapter configuration."""
    if manifest.provider is not None or manifest.pit_data_scope != "production":
        raise ValueError("fixture history requires provider-free authority")
    for index in range(1, manifest.search.max_feedback_rounds + 1):
        events = repository.load_round_events(campaign_id=manifest.campaign_id, round_index=index)
        payloads = tuple(
            repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in events
        )
        leases = tuple(item for item in payloads if type(item) is ResourceLeasePayloadV5)
        expected_token = canonical_sha256_v5({"fixture": manifest.sha256, "round": index})
        if repository.load_candidate_executions(campaign_id=manifest.campaign_id, round_index=index):
            raise ValueError("fixture history contains external evaluator ownership")
        if any(
            item.resource_kind != "workspace"
            or item.owner_campaign_id != manifest.campaign_id
            or item.owner_token_sha256 != expected_token
            or not item.lease_id.startswith("fixture-")
            for item in leases
        ):
            raise ValueError("fixture resource history differs from synthetic ownership")
        for payload in payloads:
            if type(payload) is RoleCompletionPayloadV5:
                package = repository.load_role_invocation(payload)
                if (
                    type(package.terminal_authority) is not FixtureRoleTerminalAuthorityV5
                    or package.attempt.response_sha256 != canonical_role_response_sha256_v5(_response(package.request))
                ):
                    raise ValueError("fixture role history differs from deterministic responses")
            elif type(payload) is CleanupResultPayloadV5 and payload.cleanup_complete:
                if (
                    payload.owned_workspaces,
                    payload.owned_policy_workers,
                    payload.owned_evaluators,
                    payload.owned_containers,
                ) != (len(leases), 0, 0, 0):
                    raise ValueError("fixture cleanup differs from its owned leases")
    return True


def run_fixture_campaign_v5(*, repository, authorities):
    """Run two bounded rounds, reconstructing dependencies at the checkpoint boundary."""
    original = authorities.baseline_authority.source_bundle.sha256
    results = []
    for round_index in (1, 2):
        inputs, dependencies = compose_fixture_round_v5(
            repository=repository, authorities=authorities, round_index=round_index
        )
        result = run_feedback_round_v5(inputs, dependencies)
        results.append(result)
        if result.status != "completed" or result.cleanup is None or not result.cleanup.cleanup_complete:
            break
    if authorities.baseline_authority.source_bundle.sha256 != original:
        raise ValueError("fixture baseline source changed")
    return tuple(results)


__all__ = [
    "FixtureRoleInvokerV5",
    "SyntheticCandidateRuntimeV5",
    "compose_fixture_round_v5",
    "run_fixture_campaign_v5",
    "verify_fixture_run_v5",
]
