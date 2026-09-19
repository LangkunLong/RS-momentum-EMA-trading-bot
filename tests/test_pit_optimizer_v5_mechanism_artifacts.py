"""Focused offline persistence checks for the V5 mechanism extension."""

from __future__ import annotations

from decimal import Decimal
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.campaign_admission import (
    CampaignAdmissionPolicyV5,
    require_role_envelope_v5,
)
from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    SourceFileV5,
    SourceOperationV5,
    StructuralTemplateV5,
    VariantAssignmentV5,
    derive_experiment_identity_v5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    CampaignEvidenceV5,
    CampaignPanelPlanV5,
    CriticArtifactV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    EvaluationReportV5,
    InvestigatorArtifactV5,
    ModelPriceUpperBoundV5,
    PanelEvaluationV5,
    ProviderCapabilitiesV5,
    ResourceCapabilitiesV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    SandboxProfileV5,
    ScenarioPanelEvaluationV5,
    SearchCapabilitiesV5,
    ValidationResultV5,
    canonical_sha256_v5,
    canonical_json_bytes_v5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    PolicySourceSnapshotV5,
    build_campaign_manifest_v5,
)
from core.pit_optimizer_v5.memory import (
    ExperimentRecordV5,
    RoleCompletionPayloadV5,
    RoundEventV5,
    RoundIntentPayloadV5,
    StoredExperimentRecordV5,
    event_kind_for_payload_v5,
    project_investigator_memory_v5,
)
from core.pit_optimizer_v5.mechanism_artifacts import (
    MechanismArtifactCorrupt,
    MechanismArtifactRepositoryV5,
    MechanismCapabilityError,
    MechanismChronologyError,
    MechanismExtensionCapabilityV1,
    MechanismExperimentIndexV1,
    MechanismRuntimeExtensionV1,
    explicit_mechanism_fixture_opt_in_v1,
    round_intent_sha256_v1,
)
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismControlV1,
    MechanismCoverageV1,
    MechanismExecutionV1,
    MechanismExperimentSpecV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismPredictionResultV1,
    MechanismResourceBudgetV1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismWorkerRegistrationV1,
    SyntheticFixtureWorkerV1,
    build_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.fixture_runtime import (
    FixtureRoleInvokerV5,
    SyntheticCandidateRuntimeV5,
    compose_fixture_round_v5,
)
from core.pit_optimizer_v5.probes import policy_probe_suite_v1
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.production_runtime import (
    LocalArchiveReducerFactoryV5,
    LocalRoleRequestFactoryV5,
    MechanismRoleRequestAdapterV1,
)
from core.pit_optimizer_v5.runtime import (
    CandidateEvidenceV5,
    FeedbackRoundInputV5,
    MaterializedVariantV5,
    SearchProjectionV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.search import (
    ArchiveRecordAuthorityV5,
    ArchiveEntryV5,
    BaselineParentAuthorityV5,
    CandidateArchiveV5,
    SearchStateV5,
    campaign_cagr_pct,
    archive_parent_from_record_v5,
    baseline_parent_candidate_v5,
    expected_target_gap_pct_v5,
)
from core.pit_optimizer_v5.selection import ScheduledHypothesisV5, hypothesis_novelty_key_v5, select_parent_v5
from core.pit_optimizer_v5.contracts import HypothesisV5, MetricPredictionV5
from core.strategy_policy.contracts import (
    AllocationDecision,
    CapacityDecision,
    EntryDecision,
    EvictionDecision,
    ExitDecision,
)
from core.strategy_policy.contracts_v3 import AddOnDecisionV3
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    ProbeObservationV5,
    SemanticFingerprintV5,
)
from core.pit_optimizer_v5.provider import (
    AuthorPolicyContractsV5,
    AuthorRoleInputV5,
    InvestigatorRoleInputV5,
    MechanismEvidenceRowV1,
    MechanismMemoryDispositionV1,
    MechanismRoleInputV1,
    MechanismRoleProjectionV1,
    RoleBindingV5,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    FixtureRoleRunnerV5,
    FixtureRoleTerminalAuthorityV5,
    _role_citation_sequence,
    _validate_mechanism_role_projection,
    build_role_request_v5,
    parsed_role_artifact_primitive_v5,
    prospective_role_usage_v5,
    role_schema_authority_from_manifest_v5,
    wire_role_messages_v5,
    wire_role_schema_v5,
)

from tests.task4_legacy_fixture import (
    LEGACY_GOLDEN_JSON_SHA256_V5,
    _run as run_legacy_fixture,
)


def _unlink_fixture_path(path: Path) -> None:
    """Delete one synthetic fixture artifact, including long Windows paths."""

    raw = str(path)
    if os.name == "nt" and not raw.startswith("\\\\?\\"):
        raw = "\\\\?\\" + raw
    os.unlink(raw)


def _hypothesis() -> HypothesisV5:
    return HypothesisV5(
        hypothesis_id="hyp.exit.atr",
        rank=1,
        primary_mechanism="exit",
        causal_claim="ATR-aware exit behavior changes exit decisions.",
        predicted_changes=(
            MetricPredictionV5(
                metric_id="exit.decision_changed_count",
                direction="increase",
                rationale="Applicable cases should change.",
            ),
        ),
        evidence_ids=("v5.fixture.evidence",),
        author_instructions="Use the registered exit mechanism recipe.",
    )


def _source_bundle(suffix: str = "", *, fixture_entry: bool = False) -> SourceBundleV5:
    root = Path(__file__).resolve().parents[1]
    files = []
    for path in EDITABLE_POLICY_PATHS_V5:
        content = (root / path).read_text(encoding="utf-8")
        if fixture_entry and path == EDITABLE_POLICY_PATHS_V5[0]:
            content = content.replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n\nFIXTURE_ENTRY_THRESHOLD = 0\n",
                1,
            )
        if suffix and path == EDITABLE_POLICY_PATHS_V5[-1]:
            content = content.rstrip("\n") + f"\n# {suffix}\n"
        files.append(SourceFileV5(path=path, source=content))
    return SourceBundleV5(files=tuple(files))


def _seed_snapshot() -> object:
    return next(case.snapshot for case in policy_probe_suite_v1() if case.method == "evaluate_exit")


def _spec(
    *,
    parent_revision_sha256: str,
    round_intent_sha256: str,
    hypothesis: HypothesisV5 | None = None,
) -> MechanismExperimentSpecV1:
    from core.pit_optimizer_v5.mechanism_contracts import (
        MechanismControlV1,
        MechanismDisconfirmingObservationV1,
        MechanismDiagnosticSelectorV1,
        MechanismMetricSpecV1,
        MechanismPredicateV1,
        MechanismRecipeV1,
    )

    hypothesis = _hypothesis() if hypothesis is None else hypothesis
    return MechanismExperimentSpecV1(
        precommitment_id="v5.precommit.exit.atr",
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=hypothesis.sha256,
        parent_revision_sha256=parent_revision_sha256,
        round_intent_sha256=round_intent_sha256,
        target_method="evaluate_exit",
        allowed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="is_present",
            value=None,
        ),
        controls=(MechanismControlV1(control_id="protected_next_stop_price"),),
        metrics=(
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
                denominator="relevant_cases",
            ),
            MechanismMetricSpecV1(
                metric_id="evaluator.exit_attribution_count",
                unit="count",
                direction="increase",
                tolerance=Decimal("0"),
                denominator="evaluator_cases",
                selector=MechanismDiagnosticSelectorV1(section="exit_attribution", metric_id="ma_violation"),
            ),
        ),
        aggregation="paired_case_delta",
        minimum_relevant_cases=2,
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None),
        ),
        disconfirming_observations=(
            MechanismDisconfirmingObservationV1(
                observation_id="protected_control_changed",
                metric_id="exit.protected_control_unchanged_count",
            ),
        ),
        provenance="authorized_discovery",
    )


def _static_fingerprint(*, distinct: bool = False, variant: int = 0) -> SemanticFingerprintV5:
    """Build the fixed semantic authority without executing policy source."""

    observations = []
    for case in policy_probe_suite_v1():
        if case.method == "evaluate_entry":
            decision = EntryDecision(False, True, (None, None), ())
        elif case.method == "recommend_capacity":
            decision = CapacityDecision(None, False)
        elif case.method == "recommend_allocation":
            decision = AllocationDecision(0.01, 0.01, None)
        elif case.method == "select_eviction":
            decision = EvictionDecision(None)
        elif case.method == "evaluate_add_on":
            decision = AddOnDecisionV3(False, 0.0, None, "hold")
        else:
            stop = None
            if distinct and case.probe_id == "exit_winner_scale_boundary":
                candidates = case.snapshot.base.protective_stop_candidates
                if candidates:
                    stop = candidates[-1 - (variant % len(candidates))]
                early_winner_hold = variant >= 1
            else:
                early_winner_hold = case.snapshot.base.early_winner_hold
            decision = ExitDecision(
                actions=(),
                next_stop_price=stop,
                early_winner_hold=early_winner_hold,
                scale_out_tier=case.snapshot.base.scale_out_tier,
                breakeven_armed=case.snapshot.base.breakeven_armed,
                ema_trailing_active=case.snapshot.base.ema_trailing_active,
            )
        observations.append(
            ProbeObservationV5(
                case.probe_id,
                case.method,
                case.input_sha256,
                decision.to_canonical_json().encode("utf-8"),
            )
        )
    return SemanticFingerprintV5(
        suite_id=PROBE_SUITE_ID_V5,
        observations=tuple(observations),
        fingerprint_sha256=canonical_sha256_v5(
            {
                "suite_id": PROBE_SUITE_ID_V5,
                "observations": tuple(item.to_primitive() for item in observations),
            }
        ),
    )


def _zero_report() -> EvaluationReportV5:
    return EvaluationReportV5(
        portfolio_annualized_return_pct=Decimal("0"),
        portfolio_total_return_pct=Decimal("0"),
        gross_annualized_return_pct=Decimal("0"),
        benchmark_annualized_return_pct=Decimal("0"),
        benchmark_total_return_pct=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        sharpe_ratio=Decimal("0"),
        closed_trades=0,
        average_exposure_pct=Decimal("0"),
        average_cash_pct=Decimal("0"),
        turnover_pct=Decimal("0"),
        total_friction_usd=Decimal("0"),
        friction_drag_pct=Decimal("0"),
        win_rate_pct=None,
        average_win_pct=None,
        average_loss_pct=None,
        payoff_ratio=None,
        expectancy_pct=None,
        median_holding_sessions=None,
        invested_sleeve_annualized_return_pct=None,
        estimated_idle_cash_drag_pct=Decimal("0"),
        stop_gap_shortfall_usd=Decimal("0"),
        identity_transition_count=0,
        scale_out_opportunity_cost_pct=None,
        maximum_favorable_excursion_pct=None,
        maximum_adverse_excursion_pct=None,
        entry_funnel=(),
        exit_attribution=(),
        policy_intent_outcomes=(),
        regime_slices=(),
        episode_slices=(),
        calendar_year_slices=(),
        rolling_returns=(),
    )


def _authenticated_fixture(
    *,
    repository: LocalArtifactRepositoryV5,
    parent_bundle: SourceBundleV5,
    parent_revision: PolicyRevisionIdentityV5,
    semantic_mode: str = "required",
    investigator_memory_max_bytes: int = 96 * 1024,
    archive_capacity: int = 2,
    hypotheses_per_investigator: int = 1,
    max_variants_per_template: int = 2,
    max_discovery_survivors_per_template: int = 1,
    fixture_entry: bool = False,
) -> AuthenticatedCampaignManifestV5:
    """Compose and authenticate a complete fresh temporary authority graph."""

    pit_ref = repository.append_binary_state(
        namespace="task4-fixture", key="pit-bundle", content=b"task4 synthetic PIT bytes"
    )
    prices_ref = repository.append_binary_state(
        namespace="task4-fixture", key="prices", content=b"task4 synthetic price provenance"
    )
    transition_ref = repository.append_binary_state(
        namespace="task4-fixture", key="transition", content=b"task4 synthetic identity transition"
    )
    lineage = PanelSecurityLineage("fixture-lineage", ("SPY",), ("sp500",))
    panel_specs = {
        "quick": EvaluationPanelSpec.from_lineages(
            purpose="quick", sessions=("2025-01-01", "2025-01-02"), lineages=(lineage,)
        ),
        "discovery-1": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2025-02-01", "2025-02-02"), lineages=(lineage,)
        ),
        "discovery-2": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2025-03-01", "2025-03-02"), lineages=(lineage,)
        ),
        "discovery-3": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2025-04-01", "2025-04-02"), lineages=(lineage,)
        ),
        "discovery-4": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2025-05-01", "2025-05-02"), lineages=(lineage,)
        ),
    }
    panel_refs = {
        name: repository.create_evaluation_panel_spec(f"panels/task4-{name}.json", panel)
        for name, panel in panel_specs.items()
    }
    target = AnnualizedReturnTargetV5(Decimal("10.00"))

    def episode(name: str, panel_name: str, ordinal: int | None = None) -> EpisodePlanV5:
        panel = panel_specs[panel_name]
        return EpisodePlanV5(
            episode_id=f"task4-{name}",
            episode_ordinal=ordinal,
            purpose=panel.purpose,
            start_date=panel.start_date,
            end_date=panel.end_date,
            lineage_ids=tuple(item.security_lineage_id for item in panel.lineages),
            panel_ref=panel_refs[panel_name],
        )

    panel_plan = CampaignPanelPlanV5(
        schema_version=5,
        pit_bundle_ref=pit_ref,
        prices_provenance_ref=prices_ref,
        partition_seed_sha256="1" * 64,
        target_sha256=target.sha256,
        mechanics=episode("mechanics", "quick"),
        quick=episode("quick", "quick"),
        discovery=tuple(episode(f"discovery-{index}", f"discovery-{index}", index) for index in range(1, 5)),
        confirmation_plan_sha256="2" * 64,
        qualification_plan_sha256="3" * 64,
    )
    panel_plan_ref = repository.create_typed_artifact("panels/task4-plan.json", panel_plan)

    source_ref = repository.create_typed_artifact("evaluator/task4-source.json", parent_bundle)
    revision_ref = repository.create_typed_artifact("evaluator/task4-revision.json", parent_revision)
    execution = ExecutionProfileV5(
        5,
        "next_open",
        "open_then_stop",
        "last_session_close",
        "half_spread_plus_market_impact_plus_commission_bps",
    )
    execution_ref = repository.create_typed_artifact("evaluator/task4-execution.json", execution)
    evaluator_source_ref = repository._create_only(
        "evaluator/task4-runtime-source.json", {"fixture": "task4-runtime-source"}
    )
    resources = ResourceCapabilitiesV5(
        max_parallel_evaluations=1,
        evaluation_cpu_limit=Decimal("1"),
        evaluation_memory_mib=128,
        evaluation_output_limit_bytes=64 * 1024,
        mechanics_timeout_seconds=60,
        quick_timeout_seconds=60,
        discovery_episode_timeout_seconds=60,
        round_wall_timeout_seconds=120,
        campaign_wall_timeout_seconds=240,
    )
    sandbox = SandboxProfileV5(
        schema_version=5,
        image_name="task4-fixture",
        image_digest="sha256:" + "4" * 64,
        runtime_source_sha256=evaluator_source_ref.sha256,
        network_mode="none",
        root_filesystem="read_only",
        source_mount_mode="read_only",
        data_mount_mode="read_only",
        output_mode="bounded_write_only",
        cpu_limit=resources.evaluation_cpu_limit,
        memory_limit_mib=resources.evaluation_memory_mib,
        output_limit_bytes=resources.evaluation_output_limit_bytes,
        pid_limit=resources.evaluation_pid_limit,
    )
    sandbox_ref = repository.create_typed_artifact("evaluator/task4-sandbox.json", sandbox)
    evaluator = EvaluatorContractV5(
        schema_version=5,
        execution_profile_sha256=execution_ref.sha256,
        sandbox_profile_sha256=sandbox_ref.sha256,
        evaluator_source_sha256=evaluator_source_ref.sha256,
        pit_bundle_sha256=pit_ref.sha256,
        prices_provenance_sha256=prices_ref.sha256,
        identity_transition_contract_sha256=transition_ref.sha256,
        baseline_source_bundle_sha256=source_ref.sha256,
        baseline_policy_revision_sha256=revision_ref.sha256,
        friction_grid=initial_friction_grid_v5(),
        selection_scenario_id="base",
    )
    evaluator_ref = repository.create_typed_artifact("evaluator/task4-evaluator.json", evaluator)

    report = _zero_report()
    episodes = []
    for index, name in enumerate(("discovery-1", "discovery-2", "discovery-3", "discovery-4"), 1):
        panel = panel_specs[name]
        scenarios = tuple(
            ScenarioPanelEvaluationV5(scenario.scenario_id, Decimal("100"), Decimal("100"), report)
            for scenario in initial_friction_grid_v5()
        )
        evaluation = PanelEvaluationV5(
            evaluator_contract_sha256=evaluator.sha256,
            sandbox_profile_sha256=sandbox.sha256,
            panel_sha256=panel.sha256,
            policy_identity_sha256=parent_revision.sha256,
            start_date=panel.start_date,
            end_date=panel.end_date,
            elapsed_calendar_days=1,
            selection_scenario_id="base",
            scenarios=scenarios,
        )
        episodes.append(
            EpisodeEvaluationV5(
                episode_id=f"task4-discovery-{index}",
                episode_ordinal=index,
                start_date=panel.start_date,
                end_date=panel.end_date,
                evaluation=evaluation,
            )
        )
    campaign = CampaignEvidenceV5(
        discovery_plan_sha256=panel_plan.discovery_plan_sha256,
        episodes=tuple(episodes),
        campaign_cagr_pct=Decimal("0"),
        closed_trades=0,
    )
    baseline = BaselineParentAuthorityV5(
        policy_revision=parent_revision,
        policy_revision_ref=revision_ref,
        semantic_fingerprint=(None if semantic_mode == "disabled_development" else _static_fingerprint()),
        campaign=campaign,
        source_bundle=parent_bundle,
        source_bundle_ref=source_ref,
        pit_data_scope=("development_sp500_v2" if semantic_mode == "disabled_development" else "production"),
        semantic_mode=semantic_mode,
    )
    baseline_ref = repository.create_typed_artifact("evaluator/task4-baseline.json", baseline)
    return build_campaign_manifest_v5(
        repository=repository,
        campaign_id="fixture-campaign",
        target=target,
        source_snapshot=PolicySourceSnapshotV5(
            source_commit="a" * 40,
            editable_source_sha256=parent_revision.editable_source_sha256,
        ),
        execution_profile_ref=execution_ref,
        evaluator_contract_ref=evaluator_ref,
        baseline_authority_ref=baseline_ref,
        panel_plan_ref=panel_plan_ref,
        sandbox_profile_ref=sandbox_ref,
        policy_scope_path="evaluator/task4-policy-scope.json",
        manifest_path="evaluator/task4-manifest.json",
        search=SearchCapabilitiesV5(
            hypotheses_per_investigator=hypotheses_per_investigator,
            max_tunable_axes=1,
            max_variants_per_template=max_variants_per_template,
            max_discovery_survivors_per_template=max_discovery_survivors_per_template,
            archive_capacity=archive_capacity,
            max_feedback_rounds=2,
            allow_full_source_escape=False,
            investigator_memory_max_bytes=investigator_memory_max_bytes,
        ),
        resources=resources,
        provider=None,
        pit_data_scope=("development_sp500_v2" if semantic_mode == "disabled_development" else "production"),
        semantic_mode=semantic_mode,
    )


def _capability(
    tmp_path: Path,
    *,
    semantic_mode: str = "required",
    investigator_memory_max_bytes: int = 96 * 1024,
    archive_capacity: int = 2,
    hypotheses_per_investigator: int = 1,
    max_variants_per_template: int = 2,
    max_discovery_survivors_per_template: int = 1,
    fixture_entry: bool = False,
) -> tuple[
    MechanismArtifactRepositoryV5,
    MechanismExtensionCapabilityV1,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
]:
    parent_bundle = _source_bundle(fixture_entry=fixture_entry)
    candidate_bundle = _source_bundle("candidate fixture")
    parent_revision = derive_policy_revision_identity_v5(
        source_bundle=parent_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    candidate_revision = derive_policy_revision_identity_v5(
        source_bundle=candidate_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    artifact_repository = LocalArtifactRepositoryV5(tmp_path)
    authenticated = _authenticated_fixture(
        repository=artifact_repository,
        parent_bundle=parent_bundle,
        parent_revision=parent_revision,
        semantic_mode=semantic_mode,
        investigator_memory_max_bytes=investigator_memory_max_bytes,
        archive_capacity=archive_capacity,
        hypotheses_per_investigator=hypotheses_per_investigator,
        max_variants_per_template=max_variants_per_template,
        max_discovery_survivors_per_template=max_discovery_survivors_per_template,
    )
    intent = RoundIntentPayloadV5(
        parent_revision_sha256=parent_revision.sha256,
        parent_semantic_fingerprint_sha256=(
            None
            if semantic_mode == "disabled_development"
            else authenticated.baseline_authority.semantic_fingerprint.fingerprint_sha256
        ),
        hypothesis=_hypothesis(),
        discovery_plan_sha256=authenticated.panel_plan.discovery_plan_sha256,
        pit_data_scope="development_sp500_v2" if semantic_mode == "disabled_development" else "production",
        semantic_mode=semantic_mode,
    )
    spec = _spec(parent_revision_sha256=parent_revision.sha256, round_intent_sha256=round_intent_sha256_v1(intent))
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot())
    capability = MechanismExtensionCapabilityV1(
        authenticated_manifest=authenticated,
        round_intent=intent,
        spec=spec,
        corpus=corpus,
        parent_candidate=baseline_parent_candidate_v5(
            authority=authenticated.baseline_authority,
            discovery_plan=authenticated.panel_plan,
            evaluator_contract=authenticated.evaluator_contract,
            pit_data_scope=authenticated.manifest.pit_data_scope,
        ),
        parent_revision=parent_revision,
        parent_revision_ref=authenticated.baseline_authority.policy_revision_ref,
        parent_source_bundle=parent_bundle,
        parent_source_bundle_ref=authenticated.baseline_authority.source_bundle_ref,
        resource_budget=MechanismResourceBudgetV1(
            max_cases=16,
            max_repetitions=1,
            timeout_ms=1000,
            cpu_seconds=Decimal("2"),
            memory_mib=128,
            output_bytes=64 * 1024,
        ),
        authorization=explicit_mechanism_fixture_opt_in_v1("task4-test-opt-in"),
        round_index=1,
    )
    return MechanismArtifactRepositoryV5(artifact_repository), capability, candidate_bundle, candidate_revision


def _legacy_golden_candidate(
    *,
    authenticated: AuthenticatedCampaignManifestV5,
    parent,
    hypothesis: HypothesisV5,
    discovery_plan_sha256: str,
    candidate_suffix: str,
    cagr: str,
) -> CandidateEvidenceV5:
    """Build the same pure typed candidate shape used by the recorded baseline."""

    source_bundle = _source_bundle(candidate_suffix)
    policy_revision = derive_policy_revision_identity_v5(
        source_bundle=source_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    operation = SourceOperationV5(
        path="core/strategy_policy/v3/exit.py",
        symbol="evaluate_exit",
        kind="replace_function",
        replacement_source="def evaluate_exit(*args, **kwargs):\n    return None\n",
    )
    template = StructuralTemplateV5(
        hypothesis_id=hypothesis.hypothesis_id,
        parent_revision_sha256=parent.policy_identity_sha256,
        changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
        source_operations=(operation,),
        axes=(LiteralAxisV5("atr_20_fraction", 0.20, (0.20,)),),
        full_source_escape=None,
    )
    assignment = VariantAssignmentV5((("atr_20_fraction", 0.20),))
    variant = RenderedVariantV5(
        assignment=assignment,
        source_bundle=source_bundle,
        policy_revision=policy_revision,
    )
    identity = derive_experiment_identity_v5(
        policy_revision=policy_revision,
        parent_revision_sha256=parent.policy_identity_sha256,
        hypothesis=hypothesis,
        template=template,
        assignment=assignment,
        round_index=1,
        discovery_plan_sha256=discovery_plan_sha256,
    )
    materialized = MaterializedVariantV5(
        variant=variant,
        source_bundle_ref=ArtifactRefV5(f"scratch/{candidate_suffix}.json", source_bundle.sha256),
        leases=(),
        opaque_candidate=f"{candidate_suffix}-handle",
    )
    parent_episodes = authenticated.baseline_authority.campaign.episodes
    candidate_episodes = tuple(
        replace(
            episode,
            evaluation=replace(episode.evaluation, policy_identity_sha256=policy_revision.sha256),
        )
        for episode in parent_episodes
    )
    campaign = replace(
        authenticated.baseline_authority.campaign,
        episodes=candidate_episodes,
        campaign_cagr_pct=Decimal(cagr),
    )
    quick_parent = parent_episodes[0].evaluation
    quick = replace(
        quick_parent,
        panel_sha256=authenticated.panel_plan.quick.panel_ref.sha256,
        policy_identity_sha256=policy_revision.sha256,
        start_date=authenticated.panel_plan.quick.start_date,
        end_date=authenticated.panel_plan.quick.end_date,
    )
    return CandidateEvidenceV5(
        experiment_identity=identity,
        template=template,
        materialized=materialized,
        validation=ValidationResultV5(True, None, template.changed_symbols),
        semantic_fingerprint=_static_fingerprint(distinct=True),
        quick_evidence=quick,
        discovery_episodes=candidate_episodes,
        campaign_evidence=campaign,
        status="evaluated",
        failure_code=None,
        artifact_refs=(),
    )


def _persist_evaluated_record(
    repository: MechanismArtifactRepositoryV5,
    capability: MechanismExtensionCapabilityV1,
    candidate_bundle: SourceBundleV5,
    candidate_revision: PolicyRevisionIdentityV5,
    candidate_suffix: str = "candidate fixture",
    campaign_ratio: Decimal = Decimal("1"),
    semantic_fingerprint: SemanticFingerprintV5 | None = None,
    critic_artifact: CriticArtifactV5 | None = None,
    publish: bool = True,
) -> StoredExperimentRecordV5:
    """Persist one complete typed candidate checkpoint for archive/restart tests."""

    result = run_legacy_fixture(
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        candidate_suffix=candidate_suffix,
        return_candidate=True,
    )
    candidate = result["_candidate"]
    assert candidate.materialized.variant.source_bundle == candidate_bundle
    assert candidate.materialized.variant.policy_revision == candidate_revision
    candidate = replace(
        candidate,
        experiment_identity=derive_experiment_identity_v5(
            policy_revision=candidate_revision,
            parent_revision_sha256=capability.parent_revision.sha256,
            hypothesis=_hypothesis(),
            template=candidate.template,
            assignment=candidate.materialized.variant.assignment,
            round_index=capability.round_index,
            discovery_plan_sha256=capability.round_intent.discovery_plan_sha256,
        ),
    )
    if semantic_fingerprint is not None:
        candidate = replace(candidate, semantic_fingerprint=semantic_fingerprint)

    repository.repository.append_typed_state(
        namespace="policy-source",
        key=candidate_revision.sha256,
        value=candidate_bundle,
    )
    source_ref = ArtifactRefV5(
        f"adapter-state/policy-source/{candidate_revision.sha256}.json",
        candidate_bundle.sha256,
    )
    revision_ref = repository.repository.create_typed_artifact(
        f"evaluator/task4-candidate-revision-{candidate_revision.sha256}.json",
        candidate_revision,
    )
    positive_report = replace(
        _zero_report(),
        closed_trades=1,
        portfolio_total_return_pct=(campaign_ratio - Decimal("1")) * Decimal("100"),
    )
    parent_campaign = capability.authenticated_manifest.baseline_authority.campaign
    episodes = tuple(
        replace(
            episode,
            evaluation=replace(
                episode.evaluation,
                policy_identity_sha256=candidate_revision.sha256,
                scenarios=tuple(
                    replace(
                        scenario,
                        starting_equity=Decimal("100"),
                        ending_equity=Decimal("100") * campaign_ratio,
                        report=positive_report,
                    )
                    for scenario in episode.evaluation.scenarios
                ),
            ),
        )
        for episode in parent_campaign.episodes
    )
    campaign = CampaignEvidenceV5(
        discovery_plan_sha256=capability.authenticated_manifest.panel_plan.discovery_plan_sha256,
        episodes=episodes,
        campaign_cagr_pct=campaign_cagr_pct(
            episodes=episodes,
            discovery_plan=capability.authenticated_manifest.panel_plan,
            evaluator_contract=capability.authenticated_manifest.evaluator_contract,
        ),
        closed_trades=4,
    )
    review = (
        next(item for item in critic_artifact.reviews if item.experiment_id == candidate.experiment_id)
        if critic_artifact is not None
        else CriticReviewV5(
            experiment_id=candidate.experiment_id,
            prediction_vs_observation="fixture candidate matched the declared mechanism.",
            causal_explanation="fixture evidence is complete and deterministic.",
            evidence_ids=("v5.fixture.evidence",),
            disposition="promote",
            next_direction="fixture follow-up",
        )
    )
    critic_ref = repository.repository.append_critic(
        critic_artifact
        if critic_artifact is not None
        else CriticArtifactV5(
            reviews=(review,),
            comparative_assessment="fixture comparison",
            next_campaign_direction="fixture follow-up",
            evidence_ids=("v5.fixture.evidence",),
        )
    )
    refs = tuple(sorted((critic_ref, revision_ref, source_ref), key=lambda item: (item.relative_path, item.sha256)))
    record = ExperimentRecordV5(
        experiment_id=candidate.experiment_id,
        experiment_identity=candidate.experiment_identity,
        round_index=capability.round_index,
        parent_revision_sha256=capability.parent_revision.sha256,
        parent_semantic_fingerprint_sha256=capability.parent_candidate.semantic_fingerprint_sha256,
        hypothesis=_hypothesis(),
        template=candidate.template,
        template_sha256=candidate.template.sha256,
        variant_assignment=candidate.materialized.variant.assignment,
        policy_revision=candidate_revision,
        semantic_fingerprint=candidate.semantic_fingerprint,
        status="evaluated",
        validation=candidate.validation,
        quick_evidence=candidate.quick_evidence,
        discovery_episodes=episodes,
        campaign_evidence=campaign,
        target_gap_pct=expected_target_gap_pct_v5(
            target=capability.authenticated_manifest.manifest.target,
            campaign_cagr=campaign.campaign_cagr_pct,
        ),
        critic_review=review,
        artifact_refs=refs,
        critic_artifact_ref=critic_ref,
        pit_data_scope=capability.round_intent.pit_data_scope,
        semantic_mode=capability.round_intent.semantic_mode,
    )
    record_ref = repository.repository.append_experiment(record)
    inputs = FeedbackRoundInputV5(
        manifest=capability.authenticated_manifest.manifest,
        panel_plan=capability.authenticated_manifest.panel_plan,
        evaluator_contract=capability.authenticated_manifest.evaluator_contract,
        baseline=capability.authenticated_manifest.baseline_authority,
        round_index=capability.round_index,
        owner_token_sha256="d" * 64,
    )
    prior = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=inputs.manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    authority = ArchiveRecordAuthorityV5(
        experiment_id=record.experiment_id,
        experiment_record_ref=record_ref,
        source_bundle=candidate_bundle,
        source_bundle_ref=source_ref,
    )
    stored = StoredExperimentRecordV5(reference=record_ref, record=record)
    if publish:
        reducer = LocalArchiveReducerFactoryV5(repository.repository).publication_reducer(
            inputs=inputs,
            prior=prior,
            records=(stored,),
            source_authorities=(authority,),
        )
        repository.repository.publish_projection(
            record_refs=(record_ref,),
            reducer=reducer,
            generation=1,
        )
    return stored


def _post_evaluation_candidate(
    candidate: object,
    capability: MechanismExtensionCapabilityV1,
    bound: object,
) -> object:
    parent_campaign = capability.selected_parent_campaign_evidence
    assert parent_campaign is not None
    return replace(
        candidate,
        discovery_episodes=tuple(
            replace(
                episode,
                evaluation=replace(
                    episode.evaluation,
                    policy_identity_sha256=bound.candidate_revision.sha256,
                ),
            )
            for episode in parent_campaign.episodes
        ),
        status="evaluated",
    )


def _append_round_event(repository: LocalArtifactRepositoryV5, payload: object) -> ArtifactRefV5:
    """Append one authenticated payload/event pair with the real journal chain."""

    payload_ref = repository.append_round_payload(payload)  # type: ignore[arg-type]
    events = repository.load_round_events(campaign_id="fixture-campaign", round_index=1)
    prior = None if not events else events[-1].sha256
    event = RoundEventV5(
        campaign_id="fixture-campaign",
        round_index=1,
        sequence=len(events),
        prior_event_sha256=prior,
        event_kind=event_kind_for_payload_v5(payload),  # type: ignore[arg-type]
        experiment_id=None,
        payload_ref=payload_ref,
    )
    return repository.append_round_event(event)


def _assert_fixture_role_chronology(
    repository: LocalArtifactRepositoryV5,
    *,
    campaign_id: str,
    round_index: int,
) -> None:
    events = repository.load_round_events(campaign_id=campaign_id, round_index=round_index)
    payloads = tuple(
        repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in events
    )
    role_positions = {
        payload.role: position for position, payload in enumerate(payloads) if type(payload) is RoleCompletionPayloadV5
    }
    intent_positions = tuple(
        position for position, payload in enumerate(payloads) if type(payload) is RoundIntentPayloadV5
    )
    assert len(intent_positions) == 1
    assert set(role_positions) == {"investigator", "author", "critic"}
    assert role_positions["investigator"] < intent_positions[0] < role_positions["author"] < role_positions["critic"]


def _persist_fixture_role(
    repository: LocalArtifactRepositoryV5,
    *,
    campaign_id: str,
    round_index: int,
    role_position: int,
    request: object,
    artifact: object,
) -> None:
    """Persist one accepted fixture role through the request and invocation authorities."""

    response = canonical_json_bytes_v5(
        {
            "binding": request.expected_binding.to_primitive(),  # type: ignore[union-attr]
            "artifact": parsed_role_artifact_primitive_v5(artifact)["artifact"],  # type: ignore[arg-type]
        }
    ).decode()
    runner = FixtureRoleRunnerV5(responses={request.role: (response,)})  # type: ignore[union-attr]
    parsed = runner.invoke_once(request)  # type: ignore[arg-type]
    attempt = runner.attempts[-1]
    call = RoleCallKeyV5(
        campaign_id=campaign_id,
        round_index=round_index,
        role=request.role,  # type: ignore[union-attr]
        role_position=role_position,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,  # type: ignore[union-attr]
    )
    package = RoleInvocationPackageV5(
        call=call,
        request=request,  # type: ignore[arg-type]
        attempt=attempt,
        terminal_authority=FixtureRoleTerminalAuthorityV5(
            "task5-history",
            call.sha256,
            request.sha256,  # type: ignore[union-attr]
            attempt.sha256,
            attempt.artifact_sha256,
        ),
        artifact=parsed,
    )
    persisted_request = repository.append_role_request(call=call, request=request)  # type: ignore[arg-type]
    persisted = repository.persist_role_invocation(
        call=call,
        request_ref=persisted_request.reference,
        package=package,
    )
    _append_round_event(repository, persisted.payload)


def _append_full_fixture_role_history(
    repository: MechanismArtifactRepositoryV5,
    capability: MechanismExtensionCapabilityV1,
    *,
    target_candidate: object,
    sibling_candidate: object,
) -> CriticArtifactV5:
    """Persist investigator, author, and shared critic history for the sibling batch."""

    raw = repository.repository
    manifest = capability.authenticated_manifest.manifest
    baseline = capability.parent_candidate
    inputs = FeedbackRoundInputV5(
        manifest=manifest,
        panel_plan=capability.authenticated_manifest.panel_plan,
        evaluator_contract=capability.authenticated_manifest.evaluator_contract,
        baseline=capability.authenticated_manifest.baseline_authority,
        round_index=1,
        owner_token_sha256="d" * 64,
    )
    parent_campaign = capability.authenticated_manifest.baseline_authority.campaign

    def evaluated_candidate(candidate: object) -> object:
        revision = candidate.materialized.variant.policy_revision  # type: ignore[union-attr]
        episodes = tuple(
            replace(
                episode,
                evaluation=replace(episode.evaluation, policy_identity_sha256=revision.sha256),
            )
            for episode in parent_campaign.episodes
        )
        return replace(candidate, status="evaluated", discovery_episodes=episodes)

    target_candidate = evaluated_candidate(target_candidate)
    sibling_candidate = evaluated_candidate(sibling_candidate)
    decision = ScheduledHypothesisV5(
        baseline,
        _hypothesis(),
        hypothesis_novelty_key_v5(parent_revision_sha256=baseline.policy_identity_sha256, hypothesis=_hypothesis()),
    )
    empty_projection = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    evidence = RoleEvidenceV5(
        5,
        (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
    )
    investigator_request = build_role_request_v5(
        role="investigator",
        role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
        issued_evidence=evidence,
        expected_binding=RoleBindingV5(
            baseline.policy_identity_sha256,
            None,
            (),
            inputs.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(role="investigator", manifest=manifest),
        max_output_tokens=4096,
    )
    _persist_fixture_role(
        raw,
        campaign_id=capability.campaign_id,
        round_index=1,
        role_position=1,
        request=investigator_request,
        artifact=InvestigatorArtifactV5((_hypothesis(),)),
    )
    _append_round_event(raw, capability.round_intent)

    parent_source = raw.load_typed_artifact(baseline.source_bundle_ref, value_type=SourceBundleV5)
    exit_path = EDITABLE_POLICY_PATHS_V5[-1]
    author_request = build_role_request_v5(
        role="author",
        role_input=AuthorRoleInputV5(
            tuple(item for item in parent_source.files if item.path == exit_path),
            False,
            _hypothesis(),
            AuthorPolicyContractsV5(baseline.policy_revision, manifest.policy_scope_ref.sha256),
        ),
        issued_evidence=evidence,
        expected_binding=RoleBindingV5(
            baseline.policy_identity_sha256,
            _hypothesis().hypothesis_id,
            (),
            inputs.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="author", manifest=manifest, author_policy_paths=(exit_path,)
        ),
        max_output_tokens=4096,
    )
    _persist_fixture_role(
        raw,
        campaign_id=capability.campaign_id,
        round_index=1,
        role_position=2,
        request=author_request,
        artifact=target_candidate.template,
    )

    history_factory = LocalRoleRequestFactoryV5(repository=raw, manifest=manifest)
    critic_request = history_factory.critic_request(
        inputs,
        empty_projection,
        decision,
        (target_candidate, sibling_candidate),
    )
    cited = critic_request.role_evidence.items[0].evidence_id
    shared = CriticArtifactV5(
        reviews=tuple(
            CriticReviewV5(
                experiment_id=item.experiment_id,
                prediction_vs_observation="fixture candidate contradicted the declared mechanism.",
                causal_explanation="the paired fixture measurement is deterministic.",
                evidence_ids=(cited,),
                disposition=("refine" if item is target_candidate else "promote"),
                next_direction="retain the negative finding in the next investigation",
            )
            for item in (target_candidate, sibling_candidate)
        ),
        comparative_assessment="higher-CAGR sibling wins while target mechanism remains measured.",
        next_campaign_direction="retain the negative finding in the next investigation",
        evidence_ids=(cited,),
    )
    _persist_fixture_role(
        raw,
        campaign_id=capability.campaign_id,
        round_index=1,
        role_position=3,
        request=critic_request,
        artifact=shared,
    )
    return shared


def _fixture_workers(bound):
    from core.strategy_policy.contracts import ExitDecision

    def registration(role: str) -> MechanismWorkerRegistrationV1:
        return MechanismWorkerRegistrationV1(
            experiment_id=bound.experiment_id,
            spec_sha256=bound.binding.spec_sha256,
            role=role,
            parent_revision_sha256=bound.binding.parent_revision_sha256,
            candidate_bytes_sha256=bound.binding.candidate_bytes_sha256,
            corpus_sha256=bound.binding.corpus_sha256,
            resource_budget=bound.binding.resource_budget,
            execution_kind="synthetic_fixture",
            reset_semantics="reset_per_case",
            cpu_memory_enforced=False,
        )

    parent = {}
    candidate = {}
    for case in bound.capability.corpus.cases:
        neutral = ExitDecision(
            actions=(),
            next_stop_price=None,
            early_winner_hold=case.snapshot.base.early_winner_hold,
            scale_out_tier=case.snapshot.base.scale_out_tier,
            breakeven_armed=case.snapshot.base.breakeven_armed,
            ema_trailing_active=case.snapshot.base.ema_trailing_active,
        )
        parent[case.input_identity_sha256] = neutral
        candidate[case.input_identity_sha256] = neutral
    return (
        SyntheticFixtureWorkerV1(registration=registration("parent"), decisions_by_input_identity=parent),
        SyntheticFixtureWorkerV1(registration=registration("candidate"), decisions_by_input_identity=candidate),
    )


class _MechanismFixtureRoleInvoker(FixtureRoleInvokerV5):
    """Use the real fixture invoker with an authenticated exit hypothesis."""

    def _invoke(self, persisted):
        if persisted.request.role not in {"investigator", "author"}:
            return super()._invoke(persisted)
        original = self.runner
        if persisted.request.role == "investigator":
            evidence_id = persisted.request.role_evidence.items[0].evidence_id
            hypothesis = _hypothesis()
            hypotheses = tuple(
                replace(
                    hypothesis,
                    hypothesis_id=(hypothesis.hypothesis_id if rank == 1 else f"{hypothesis.hypothesis_id}.{rank}"),
                    rank=rank,
                    evidence_ids=(evidence_id,),
                )
                for rank in (1, 2, 3)
            )
            artifact = InvestigatorArtifactV5(hypotheses)
            response = canonical_json_bytes_v5(
                {
                    "binding": persisted.request.expected_binding.to_primitive(),
                    "artifact": parsed_role_artifact_primitive_v5(artifact)["artifact"],
                }
            ).decode()
            responses = {"investigator": (response,)}
        else:
            template = StructuralTemplateV5(
                hypothesis_id=persisted.request.expected_binding.hypothesis_id,
                parent_revision_sha256=persisted.request.expected_binding.parent_revision_sha256,
                changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
                source_operations=(
                    SourceOperationV5(
                        path="core/strategy_policy/v3/exit.py",
                        symbol="evaluate_exit",
                        kind="replace_function",
                        replacement_source=(
                            "def evaluate_exit(snapshot: ExitSnapshotV3) -> ExitDecision:\n"
                            '    return PIT_AXIS("atr_20_fraction")\n'
                        ),
                    ),
                ),
                axes=(LiteralAxisV5("atr_20_fraction", 0.20, (0.20, 0.40, 0.60)),),
                full_source_escape=None,
            )
            artifact = parsed_role_artifact_primitive_v5(template)["artifact"]
            response = canonical_json_bytes_v5(
                {"binding": persisted.request.expected_binding.to_primitive(), "artifact": artifact}
            ).decode()
            responses = {"author": (response,)}
        self.runner = FixtureRoleRunnerV5(responses=responses)
        try:
            return super()._invoke(persisted)
        finally:
            self.runner = original


class _MechanismSyntheticCandidateRuntime(SyntheticCandidateRuntimeV5):
    """Vary only the typed fixed-suite fingerprint for exit-axis variants."""

    def fingerprint(self, materialized, *, deadline):
        assignment = dict(materialized.variant.assignment.values)
        value = assignment["atr_20_fraction"]
        variant = {0.20: 0, 0.40: 1, 0.60: 2}[value]
        return _static_fingerprint(distinct=True, variant=variant)


class _FailingDiscoveryMechanismCandidateRuntime(_MechanismSyntheticCandidateRuntime):
    """Keep the supplied-port path real while failing discovery evaluation."""

    def evaluate_episode(self, materialized, episode, *, deadline):
        raise RuntimeError("fixture discovery failure")


class _LazyFixtureMechanismExtension:
    """Compose the real extension from the runtime's authenticated intent.

    The investigator fixture reissues its first request-local evidence ID in
    the selected hypothesis.  The capability therefore cannot be frozen from
    the helper's provisional hypothesis before the role response exists.  This
    narrow supplied port waits for the journaled intent, verifies that only
    evidence IDs changed from the operator-declared hypothesis, builds a fresh
    spec/corpus for that exact intent, and delegates every operation to the
    production ``MechanismRuntimeExtensionV1``.
    """

    def __init__(
        self,
        repository: MechanismArtifactRepositoryV5,
        seed_capability: MechanismExtensionCapabilityV1,
        *,
        worker_factory,
    ) -> None:
        self.repository = repository
        self.seed_capability = seed_capability
        self.worker_factory = worker_factory
        self._delegate: MechanismRuntimeExtensionV1 | None = None

    def _compose_from_saved_intent(self, round_intent: RoundIntentPayloadV5) -> MechanismRuntimeExtensionV1:
        raw = self.repository.repository
        payloads = tuple(
            raw.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            for event in raw.load_round_events(
                campaign_id=self.seed_capability.campaign_id,
                round_index=self.seed_capability.round_index,
            )
        )
        saved = tuple(item for item in payloads if type(item) is RoundIntentPayloadV5)
        if saved != (round_intent,):
            raise AssertionError("fixture extension did not compose from the saved round intent")
        seed_intent = self.seed_capability.round_intent
        semantic_hypothesis = replace(round_intent.hypothesis, evidence_ids=seed_intent.hypothesis.evidence_ids)
        if semantic_hypothesis != seed_intent.hypothesis:
            raise AssertionError("fixture reissuance changed the declared hypothesis")
        if replace(round_intent, hypothesis=semantic_hypothesis) != seed_intent:
            raise AssertionError("fixture reissuance changed round-intent authority")
        spec = _spec(
            parent_revision_sha256=self.seed_capability.parent_revision_sha256,
            round_intent_sha256=round_intent_sha256_v1(round_intent),
            hypothesis=round_intent.hypothesis,
        )
        corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot())
        capability = replace(
            self.seed_capability,
            round_intent=round_intent,
            spec=spec,
            corpus=corpus,
        )
        return MechanismRuntimeExtensionV1(
            self.repository,
            capability,
            worker_factory=self.worker_factory,
        )

    def before_authoring(self, *, round_intent: RoundIntentPayloadV5, **kwargs) -> None:
        self._delegate = self._compose_from_saved_intent(round_intent)
        self._delegate.before_authoring(round_intent=round_intent, **kwargs)

    def _require_delegate(self) -> MechanismRuntimeExtensionV1:
        if self._delegate is None:
            raise AssertionError("fixture mechanism extension was used before authoring")
        return self._delegate

    def observe_candidate(self, **kwargs):
        return self._require_delegate().observe_candidate(**kwargs)

    def finalize_report(self, **kwargs):
        return self._require_delegate().finalize_report(**kwargs)

    def role_request_evidence(self, experiment_id: str):
        return self._require_delegate().role_request_evidence(experiment_id)

    def __getattr__(self, name: str):
        return getattr(self._require_delegate(), name)


def _counting_fixture_factory(counter: dict[str, int]):
    def factory(bound):
        counter["factory"] += 1
        return _fixture_workers(bound)

    return factory


def _append_author_request(
    repository: MechanismArtifactRepositoryV5,
    capability: MechanismExtensionCapabilityV1,
) -> None:
    schema = role_schema_authority_from_manifest_v5(
        role="author",
        manifest=capability.authenticated_manifest.manifest,
        author_policy_paths=EDITABLE_POLICY_PATHS_V5,
    )
    request = build_role_request_v5(
        role="author",
        role_input=AuthorRoleInputV5(
            editable_sources=tuple(capability.parent_source_bundle.files),
            full_source_escape=False,
            hypothesis=capability.round_intent.hypothesis,
            policy_contracts=AuthorPolicyContractsV5(
                parent_revision=capability.parent_revision,
                policy_scope_sha256=schema.policy_scope_sha256,
            ),
        ),
        issued_evidence=RoleEvidenceV5(
            schema_version=5,
            items=(RoleEvidenceItemV5("v5.fixture.evidence", "semantic.hypothesis_evidence", 1),),
        ),
        expected_binding=RoleBindingV5(
            parent_revision_sha256=capability.parent_revision_sha256,
            hypothesis_id=capability.round_intent.hypothesis.hypothesis_id,
            experiment_ids=(),
            discovery_plan_sha256=capability.round_intent.discovery_plan_sha256,
        ),
        schema_authority=schema,
        max_output_tokens=128,
    )
    call = RoleCallKeyV5(
        campaign_id=capability.campaign_id,
        round_index=capability.round_index,
        role="author",
        role_position=2,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    repository.repository.append_role_request(call=call, request=request)


def test_precommitment_and_exact_restart_round_trip(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    persisted = repository.append_precommitment(capability)
    recovered = repository.load_precommitment(capability)
    assert recovered is not None
    assert recovered.index == persisted.index
    assert recovered.spec == capability.spec
    assert recovered.corpus == capability.corpus
    assert any(path.name.endswith("-spec.bin") for path in tmp_path.rglob("*"))
    assert any(path.name.endswith("-corpus.bin") for path in tmp_path.rglob("*"))
    assert any(path.name == "fixture-campaign-0001.json" for path in tmp_path.rglob("*"))


def test_precommitment_reuses_existing_authorized_spec_but_rejects_late_freeze(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    persisted = repository.append_precommitment(capability)
    _append_author_request(repository, capability)

    recovered = repository.append_precommitment(capability)
    assert recovered.index == persisted.index

    late_root = tmp_path / "late"
    late_root.mkdir()
    late_repository, late_capability, _, _ = _capability(late_root)
    _append_author_request(late_repository, late_capability)
    with pytest.raises(MechanismChronologyError):
        late_repository.append_precommitment(late_capability)


def test_precommitment_rejects_foreign_parent_digest_and_unsafe_path(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    persisted = repository.append_precommitment(capability)
    with pytest.raises(ValueError):
        ArtifactRefV5("../outside.json", "a" * 64)
    foreign_index = replace(
        persisted.index,
        parent_revision_sha256="f" * 64,
        parent_revision_ref=ArtifactRefV5(
            persisted.index.parent_revision_ref.relative_path,
            "f" * 64,
        ),
    )
    with pytest.raises(MechanismCapabilityError):
        repository._validate_precommitment_index(capability, foreign_index)


def test_capability_rejects_reassembled_manifest_without_authenticated_graph(tmp_path: Path) -> None:
    _, capability, _, _ = _capability(tmp_path)
    tampered_manifest = replace(
        capability.authenticated_manifest.manifest,
        pit_data_scope="development_sp500_v2",
    )
    tampered_authenticated = replace(capability.authenticated_manifest, manifest=tampered_manifest)
    with pytest.raises(MechanismCapabilityError):
        replace(capability, authenticated_manifest=tampered_authenticated)


def test_required_fixture_collects_and_reuses_completed_run(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    repository.append_precommitment(capability)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.before_authoring(round_intent=capability.round_intent)

    def workers(bound):
        return _fixture_workers(bound)

    extension.worker_factory = workers
    experiment_id = "1" * 64
    run = extension.observe_candidate(
        experiment_id=experiment_id,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        semantic_outcome="behaviorally_distinct",
        semantic_recovered=False,
        deadline_monotonic=None,
    )
    assert run is not None and run.execution.status == "completed"
    second = extension.observe_candidate(
        experiment_id=experiment_id,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        semantic_outcome="behaviorally_distinct",
        semantic_recovered=True,
        deadline_monotonic=None,
    )
    assert second == run


def test_selected_nonbaseline_parent_binds_persisted_record_and_source_authority(tmp_path: Path) -> None:
    repository, baseline_capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    stored = _persist_evaluated_record(repository, baseline_capability, candidate_bundle, candidate_revision)
    parent_campaign = stored.record.campaign_evidence
    assert parent_campaign is not None
    source_ref = next(
        ref for ref in stored.record.artifact_refs if ref.relative_path.startswith("adapter-state/policy-source/")
    )
    revision_ref = next(
        ref
        for ref in stored.record.artifact_refs
        if ref.relative_path.startswith("evaluator/task4-candidate-revision-")
    )
    entry = ArchiveEntryV5(
        policy_revision=candidate_revision,
        primary_mechanism=stored.record.hypothesis.primary_mechanism,
        admitted_round=stored.record.round_index,
        campaign=stored.record.campaign_evidence,
        source_bundle_ref=source_ref,
        experiment_record_ref=stored.reference,
    )
    parent = archive_parent_from_record_v5(entry=entry, stored_record=stored)
    intent = RoundIntentPayloadV5(
        parent_revision_sha256=candidate_revision.sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        hypothesis=_hypothesis(),
        discovery_plan_sha256=baseline_capability.round_intent.discovery_plan_sha256,
        pit_data_scope=baseline_capability.round_intent.pit_data_scope,
        semantic_mode=baseline_capability.round_intent.semantic_mode,
    )
    spec = _spec(
        parent_revision_sha256=candidate_revision.sha256,
        round_intent_sha256=round_intent_sha256_v1(intent),
    )
    corpus = build_mechanism_observation_corpus_v1(spec, seed_snapshot=_seed_snapshot())
    capability = MechanismExtensionCapabilityV1(
        authenticated_manifest=baseline_capability.authenticated_manifest,
        round_intent=intent,
        parent_candidate=parent,
        parent_revision=candidate_revision,
        parent_revision_ref=revision_ref,
        parent_source_bundle=candidate_bundle,
        parent_source_bundle_ref=source_ref,
        spec=spec,
        corpus=corpus,
        resource_budget=baseline_capability.resource_budget,
        authorization=baseline_capability.authorization,
        round_index=baseline_capability.round_index + 1,
        parent_campaign_evidence=parent_campaign,
        parent_record=stored,
    )
    persisted = repository.append_precommitment(capability)
    assert persisted.index.parent_revision_ref == revision_ref
    assert persisted.index.parent_source_bundle_ref == source_ref
    _unlink_fixture_path(repository.repository.root / "checkpoint.json")
    with pytest.raises(MechanismCapabilityError):
        repository.append_precommitment(capability)


def test_complete_evidence_restarts_through_read_only_record_loader(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.worker_factory = _fixture_workers
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    experiment_id = result["decision"]["experiment_id"]
    bound = extension._bound[experiment_id]
    run = extension._runs[experiment_id]
    assert run is not None and run.execution.status == "completed"
    candidate_evidence = _post_evaluation_candidate(result["_candidate"], capability, bound)
    report = extension.finalize_report(experiment_id=experiment_id, candidate_evidence=candidate_evidence)
    assert report is not None
    stored = _persist_evaluated_record(repository, capability, candidate_bundle, candidate_revision)
    candidate_source_ref = next(
        ref for ref in stored.record.artifact_refs if ref.relative_path.startswith("adapter-state/policy-source/")
    )
    assert stored.record.experiment_id == experiment_id

    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    recovered = restarted.load_existing_evidence(
        campaign_id=capability.campaign_id,
        round_index=capability.round_index,
        experiment_id=experiment_id,
        manifest_ref=capability.manifest_ref,
        manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
        precommitment_id=capability.precommitment_id,
        expected_parent_revision_sha256=capability.parent_revision_sha256,
        expected_parent_revision_ref=capability.parent_revision_ref,
        expected_parent_source_bundle_sha256=capability.parent_source_bundle.sha256,
        expected_spec_sha256=capability.spec.sha256,
        expected_corpus_sha256=capability.corpus.sha256,
        expected_candidate_record_ref=stored.reference,
        expected_candidate_revision_sha256=candidate_revision.sha256,
        expected_candidate_source_bundle_sha256=candidate_bundle.sha256,
        expected_candidate_source_bundle_ref=candidate_source_ref,
        expected_binding_sha256=bound.binding.sha256,
    )
    assert recovered is not None
    assert recovered.run == run
    assert recovered.report == report
    with pytest.raises(MechanismCapabilityError):
        restarted.load_existing_evidence(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            experiment_id=experiment_id,
            manifest_ref=capability.manifest_ref,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            precommitment_id=capability.precommitment_id,
            expected_parent_revision_sha256=capability.parent_revision_sha256,
            expected_parent_revision_ref=capability.parent_revision_ref,
            expected_parent_source_bundle_sha256=capability.parent_source_bundle.sha256,
            expected_spec_sha256=capability.spec.sha256,
            expected_corpus_sha256=capability.corpus.sha256,
            expected_candidate_record_ref=stored.reference,
            expected_candidate_revision_sha256="f" * 64,
            expected_candidate_source_bundle_sha256=candidate_bundle.sha256,
            expected_candidate_source_bundle_ref=candidate_source_ref,
            expected_binding_sha256=bound.binding.sha256,
        )
    with pytest.raises(MechanismCapabilityError):
        restarted.load_existing_evidence(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            experiment_id=experiment_id,
            manifest_ref=capability.manifest_ref,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            precommitment_id=capability.precommitment_id,
            expected_parent_revision_sha256=capability.parent_revision_sha256,
            expected_parent_revision_ref=ArtifactRefV5(
                capability.parent_revision_ref.relative_path,
                "f" * 64,
            ),
            expected_parent_source_bundle_sha256=capability.parent_source_bundle.sha256,
            expected_spec_sha256=capability.spec.sha256,
            expected_corpus_sha256=capability.corpus.sha256,
            expected_candidate_record_ref=stored.reference,
            expected_candidate_revision_sha256=candidate_revision.sha256,
            expected_candidate_source_bundle_sha256=candidate_bundle.sha256,
            expected_candidate_source_bundle_ref=candidate_source_ref,
            expected_binding_sha256=bound.binding.sha256,
        )
    _unlink_fixture_path(repository.repository.root / "checkpoint.json")
    with pytest.raises(MechanismCapabilityError):
        restarted.load_existing_evidence(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            experiment_id=experiment_id,
            manifest_ref=capability.manifest_ref,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            precommitment_id=capability.precommitment_id,
            expected_parent_revision_sha256=capability.parent_revision_sha256,
            expected_parent_revision_ref=capability.parent_revision_ref,
            expected_parent_source_bundle_sha256=capability.parent_source_bundle.sha256,
            expected_spec_sha256=capability.spec.sha256,
            expected_corpus_sha256=capability.corpus.sha256,
            expected_candidate_record_ref=stored.reference,
            expected_candidate_revision_sha256=candidate_revision.sha256,
            expected_candidate_source_bundle_sha256=candidate_bundle.sha256,
            expected_candidate_source_bundle_ref=candidate_source_ref,
            expected_binding_sha256=bound.binding.sha256,
        )


def test_read_only_record_discovery_rehydrates_intent_and_existing_evidence(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.worker_factory = _fixture_workers
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    experiment_id = result["decision"]["experiment_id"]
    bound = extension._bound[experiment_id]
    candidate_evidence = _post_evaluation_candidate(result["_candidate"], capability, bound)
    report = extension.finalize_report(experiment_id=experiment_id, candidate_evidence=candidate_evidence)
    assert report is not None
    stored = _persist_evaluated_record(repository, capability, candidate_bundle, candidate_revision)
    intent_ref = repository.repository.append_round_payload(capability.round_intent)
    repository.repository.append_round_event(
        RoundEventV5(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            sequence=0,
            prior_event_sha256=None,
            event_kind="round_intent",
            experiment_id=None,
            payload_ref=intent_ref,
        )
    )
    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    before = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    recovered, recovered_intent = restarted.load_existing_evidence_for_record(
        campaign_id=capability.campaign_id,
        round_index=capability.round_index,
        manifest_ref=capability.manifest_ref,
        manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
        stored_record=stored,
    )
    assert recovered is not None
    assert recovered.report == report
    assert recovered_intent == capability.round_intent
    after = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_read_only_record_discovery_fails_closed_without_precommitment_index(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    stored = _persist_evaluated_record(
        repository,
        capability,
        candidate_bundle,
        candidate_revision,
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    issued: list[tuple[str, object]] = []

    def issue(metric_id: str, value: object) -> str:
        issued.append((metric_id, value))
        return f"v5.investigator.{len(issued)}.{metric_id.replace('.', '-')}.readonly"

    adapter = MechanismRoleRequestAdapterV1(
        repository,
        authenticated_manifest=capability.authenticated_manifest,
    )
    assert (
        adapter.project_persisted_record(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            manifest_ref=capability.manifest_ref,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            stored_record=stored,
            issue_evidence=issue,
        )
        is None
    )
    assert issued == []
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_read_only_record_discovery_damaged_index_does_not_repair(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability, worker_factory=_fixture_workers)
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    experiment_id = result["decision"]["experiment_id"]
    bound = extension._bound[experiment_id]
    report = extension.finalize_report(
        experiment_id=experiment_id,
        candidate_evidence=_post_evaluation_candidate(result["_candidate"], capability, bound),
    )
    assert report is not None
    stored = _persist_evaluated_record(
        repository,
        capability,
        candidate_bundle,
        candidate_revision,
    )
    raw = repository.repository
    authority_path = raw.root / "adapter-state-authority" / "mechanism-v5" / "fixture-campaign-0001.json"
    value_path = raw.root / "adapter-state" / "mechanism-v5" / "fixture-campaign-0001.json"
    assert authority_path.is_file() and value_path.is_file()
    value_bytes = value_path.read_bytes()
    _unlink_fixture_path(authority_path)
    damaged_before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    issued: list[tuple[str, object]] = []

    def issue(metric_id: str, value: object) -> str:
        issued.append((metric_id, value))
        return f"v5.investigator.{len(issued)}.{metric_id.replace('.', '-')}.damaged"

    adapter = MechanismRoleRequestAdapterV1(
        MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path)),
        authenticated_manifest=capability.authenticated_manifest,
    )
    with pytest.raises(MechanismArtifactCorrupt):
        adapter.project_persisted_record(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            manifest_ref=capability.manifest_ref,
            manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
            stored_record=stored,
            issue_evidence=issue,
        )
    assert issued == []
    assert value_path.read_bytes() == value_bytes
    damaged_after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    assert damaged_after == damaged_before


def test_retained_non_testable_record_is_visible_as_omitted_memory(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(
        tmp_path,
        investigator_memory_max_bytes=1000,
    )
    stored = _persist_evaluated_record(
        repository,
        capability,
        candidate_bundle,
        candidate_revision,
        publish=False,
    )
    record = stored.record
    assert record.quick_evidence is not None
    assert record.critic_artifact_ref is not None
    parent_fingerprint = capability.authenticated_manifest.baseline_authority.semantic_fingerprint
    assert parent_fingerprint is not None
    untestable = replace(
        record,
        status="behavioral_equivalent",
        semantic_fingerprint=parent_fingerprint,
        quick_evidence=None,
        discovery_episodes=(),
        campaign_evidence=None,
        target_gap_pct=None,
        critic_review=None,
        artifact_refs=tuple(ref for ref in record.artifact_refs if ref != record.critic_artifact_ref),
        critic_artifact_ref=None,
    )
    _unlink_fixture_path(repository.repository.root / stored.reference.relative_path)
    untestable_ref = repository.repository.append_experiment(untestable)
    inputs = FeedbackRoundInputV5(
        manifest=capability.authenticated_manifest.manifest,
        panel_plan=capability.authenticated_manifest.panel_plan,
        evaluator_contract=capability.authenticated_manifest.evaluator_contract,
        baseline=capability.authenticated_manifest.baseline_authority,
        round_index=2,
        owner_token_sha256="d" * 64,
    )
    prior = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=inputs.manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    reducer = LocalArchiveReducerFactoryV5(repository.repository).publication_reducer(
        inputs=inputs,
        prior=prior,
        records=(StoredExperimentRecordV5(untestable_ref, untestable),),
        source_authorities=(),
    )
    checkpoint = repository.repository.publish_projection(
        record_refs=(untestable_ref,),
        reducer=reducer,
        generation=1,
    )
    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    recovery_reducer = LocalArchiveReducerFactoryV5(restarted.repository).recovery_reducer(inputs)
    recovered_checkpoint, state = restarted.repository.recover_projection(recovery_reducer)
    assert recovered_checkpoint == checkpoint
    recovered = StoredExperimentRecordV5(
        untestable_ref,
        restarted.repository.load_experiment(untestable_ref),
    )
    projection = SearchProjectionV5(
        checkpoint=recovered_checkpoint,
        state=state,
        stored_records=(recovered,),
    )
    parent = select_parent_v5(
        state=state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=(recovered,),
    )
    factory = LocalRoleRequestFactoryV5(
        repository=restarted.repository,
        manifest=inputs.manifest,
        mechanism_adapter=MechanismRoleRequestAdapterV1(
            restarted,
            authenticated_manifest=capability.authenticated_manifest,
        ),
    )
    request = factory.investigator_request(inputs, projection, parent)
    assert type(request.role_input) is MechanismRoleInputV1
    assert request.role_input.projections == ()
    assert request.role_input.omitted == (
        MechanismMemoryDispositionV1(
            experiment_id=untestable.experiment_id,
            disposition="omitted",
            reason="not_authenticated",
        ),
    )


def test_mechanism_request_adapter_projects_authenticated_finding_with_fresh_ids(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.worker_factory = _fixture_workers
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    experiment_id = result["decision"]["experiment_id"]
    bound = extension._bound[experiment_id]
    report = extension.finalize_report(
        experiment_id=experiment_id,
        candidate_evidence=_post_evaluation_candidate(result["_candidate"], capability, bound),
    )
    assert report is not None
    stored = _persist_evaluated_record(repository, capability, candidate_bundle, candidate_revision)
    intent_ref = repository.repository.append_round_payload(capability.round_intent)
    repository.repository.append_round_event(
        RoundEventV5(
            campaign_id=capability.campaign_id,
            round_index=capability.round_index,
            sequence=0,
            prior_event_sha256=None,
            event_kind="round_intent",
            experiment_id=None,
            payload_ref=intent_ref,
        )
    )
    issued: list[tuple[str, object]] = []

    def issue(metric_id: str, value: object) -> str:
        evidence_id = f"v5.investigator.{len(issued) + 1}.{metric_id.replace('.', '-')}.fresh"
        issued.append((metric_id, value))
        return evidence_id

    projection = MechanismRoleRequestAdapterV1(repository).project_persisted_record(
        campaign_id=capability.campaign_id,
        round_index=capability.round_index,
        manifest_ref=capability.manifest_ref,
        manifest_source_identity_sha256=capability.manifest_source_identity_sha256,
        stored_record=stored,
        issue_evidence=issue,
    )
    assert projection.experiment_id == stored.record.experiment_id
    assert projection.hypothesis_claim == capability.round_intent.hypothesis.causal_claim
    assert projection.rows
    assert projection.rows[0].evidence_ids
    assert all("fresh" in evidence_id for row in projection.rows for evidence_id in row.evidence_ids)
    assert projection.rows[0].prediction.metric_id == "exit.decision_changed_count"
    assert any(metric_id.endswith(".delta") for metric_id, _value in issued)
    assert all("v5.fixture.evidence" not in evidence_id for row in projection.rows for evidence_id in row.evidence_ids)


def test_factory_restarts_real_history_and_projects_summary_mechanism_finding(tmp_path: Path) -> None:
    """A restarted factory carries a measured target from bounded summary memory."""

    repository, capability, target_bundle, target_revision = _capability(
        tmp_path,
        investigator_memory_max_bytes=3072,
        archive_capacity=1,
    )
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.worker_factory = _fixture_workers
    target_result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    target_experiment_id = target_result["decision"]["experiment_id"]
    target_bound = extension._bound[target_experiment_id]
    target_report = extension.finalize_report(
        experiment_id=target_experiment_id,
        candidate_evidence=_post_evaluation_candidate(target_result["_candidate"], capability, target_bound),
    )
    assert target_report is not None
    sibling_suffix = "higher sibling"
    sibling_result = run_legacy_fixture(
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        candidate_suffix=sibling_suffix,
        return_candidate=True,
    )
    target_candidate = target_result["_candidate"]
    sibling_candidate = sibling_result["_candidate"]
    assert target_candidate.experiment_id != sibling_candidate.experiment_id
    sibling_bundle = sibling_candidate.materialized.variant.source_bundle
    sibling_revision = sibling_candidate.materialized.variant.policy_revision

    shared_critic = _append_full_fixture_role_history(
        repository,
        capability,
        target_candidate=target_candidate,
        sibling_candidate=sibling_candidate,
    )
    _assert_fixture_role_chronology(
        repository.repository,
        campaign_id=capability.campaign_id,
        round_index=1,
    )
    target_stored = _persist_evaluated_record(
        repository,
        capability,
        target_bundle,
        target_revision,
        campaign_ratio=Decimal("1.005"),
        semantic_fingerprint=_static_fingerprint(distinct=True, variant=0),
        critic_artifact=shared_critic,
        publish=False,
    )
    sibling_stored = _persist_evaluated_record(
        repository,
        capability,
        sibling_bundle,
        sibling_revision,
        candidate_suffix=sibling_suffix,
        campaign_ratio=Decimal("1.01"),
        semantic_fingerprint=_static_fingerprint(distinct=True, variant=1),
        critic_artifact=shared_critic,
        publish=False,
    )
    assert target_stored.record.semantic_fingerprint != sibling_stored.record.semantic_fingerprint
    assert target_stored.record.critic_artifact_ref == sibling_stored.record.critic_artifact_ref
    assert sibling_stored.record.policy_revision == sibling_revision, (
        sibling_stored.record.policy_revision.sha256,
        sibling_revision.sha256,
    )
    assert sibling_stored.record.experiment_identity.policy_revision_sha256 == sibling_revision.sha256, (
        sibling_stored.record.experiment_identity.policy_revision_sha256,
        sibling_revision.sha256,
    )

    inputs = FeedbackRoundInputV5(
        manifest=capability.authenticated_manifest.manifest,
        panel_plan=capability.authenticated_manifest.panel_plan,
        evaluator_contract=capability.authenticated_manifest.evaluator_contract,
        baseline=capability.authenticated_manifest.baseline_authority,
        round_index=2,
        owner_token_sha256="e" * 64,
    )
    prior = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=inputs.manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    authorities = tuple(
        sorted(
            (
                ArchiveRecordAuthorityV5(
                    experiment_id=item.record.experiment_id,
                    experiment_record_ref=item.reference,
                    source_bundle=(target_bundle if item is target_stored else sibling_bundle),
                    source_bundle_ref=next(
                        ref
                        for ref in item.record.artifact_refs
                        if ref.relative_path == f"adapter-state/policy-source/{item.record.policy_revision.sha256}.json"
                    ),
                )
                for item in (target_stored, sibling_stored)
            ),
            key=lambda item: item.experiment_id,
        )
    )
    reducer = LocalArchiveReducerFactoryV5(repository.repository).publication_reducer(
        inputs=inputs,
        prior=prior,
        records=(target_stored, sibling_stored),
        source_authorities=authorities,
    )
    record_refs = tuple(sorted((target_stored.reference, sibling_stored.reference), key=lambda ref: ref.relative_path))
    checkpoint = repository.repository.publish_projection(
        record_refs=record_refs,
        reducer=reducer,
        generation=1,
    )
    assert checkpoint.record_refs == record_refs

    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    recovery_reducer = LocalArchiveReducerFactoryV5(restarted.repository).recovery_reducer(inputs)
    recovered_checkpoint, state = restarted.repository.recover_projection(recovery_reducer)
    assert recovered_checkpoint == checkpoint
    stored_records = tuple(
        StoredExperimentRecordV5(reference=ref, record=restarted.repository.load_experiment(ref))
        for ref in recovered_checkpoint.record_refs
    )
    projection = SearchProjectionV5(
        checkpoint=recovered_checkpoint,
        state=state,
        stored_records=stored_records,
    )
    assert tuple(
        sorted(
            (
                item.record.experiment_id,
                item.record.policy_revision.sha256,
                item.record.experiment_identity.policy_revision_sha256,
            )
            for item in stored_records
        )
    ) == tuple(
        sorted(
            (
                item.record.experiment_id,
                item.record.policy_revision.sha256,
                item.record.experiment_identity.policy_revision_sha256,
            )
            for item in (target_stored, sibling_stored)
        )
    )
    assert state.archive.entries[0].policy_identity_sha256 == sibling_revision.sha256, (
        repr(state.archive.entries),
        repr(sibling_revision.sha256),
    )
    parent = select_parent_v5(
        state=state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=stored_records,
    )
    assert parent.policy_identity_sha256 == sibling_revision.sha256, (
        parent.policy_identity_sha256,
        target_revision.sha256,
        sibling_revision.sha256,
        tuple((entry.policy_identity_sha256, entry.campaign_cagr_pct) for entry in state.archive.entries),
    )
    memory = project_investigator_memory_v5(
        stored_records=stored_records,
        selected_parent_revision_sha256=parent.policy_identity_sha256,
        selected_parent_record_ref=parent.selected_parent_record_ref,
        relevant_mechanism="exit",
        maximum_bytes=inputs.manifest.search.investigator_memory_max_bytes,
    )
    assert any(item.experiment_id == target_experiment_id for item in memory.summaries)
    assert all(item.experiment_id != target_experiment_id for item in memory.complete_feedback)

    adapter = MechanismRoleRequestAdapterV1(
        restarted,
        authenticated_manifest=capability.authenticated_manifest,
    )
    factory = LocalRoleRequestFactoryV5(
        repository=restarted.repository,
        manifest=inputs.manifest,
        mechanism_adapter=adapter,
    )
    request = factory.investigator_request(inputs, projection, parent)
    investigator_input_bound, investigator_cost_bound = _assert_synthetic_role_admission(
        request,
        inputs.manifest,
    )
    assert investigator_input_bound > 0
    assert investigator_cost_bound > 0
    assert type(request.role_input) is MechanismRoleInputV1
    assert any(
        item.experiment_id == target_experiment_id and item.memory_selection == "summary"
        for item in request.role_input.projections
    )
    target_rows = tuple(
        row
        for item in request.role_input.projections
        if item.experiment_id == target_experiment_id
        for row in item.rows
    )
    assert target_rows
    assert all(evidence_id not in {"v5.fixture.evidence"} for row in target_rows for evidence_id in row.evidence_ids)
    assert any(item.metric_id.endswith(".delta") and item.value is not None for item in request.role_evidence.items)
    target_projection = next(
        item for item in request.role_input.projections if item.experiment_id == target_experiment_id
    )
    target_report_row = next(
        row
        for row in target_rows
        if row.stage == "report" and row.prediction.metric_id == "exit.decision_changed_count"
    )
    target_prediction = target_report_row.prediction
    assert target_projection.hypothesis_claim == _hypothesis().causal_claim
    assert target_projection.predicted_changes == _hypothesis().predicted_changes
    assert target_projection.execution == MechanismExecutionV1("completed", "completed")
    assert target_projection.applicability == MechanismPredicateV1("features.atr_20_fraction", "is_present", None)
    assert target_projection.coverage == MechanismCoverageV1(4, 3, 0, 4, 4, 0)
    assert (
        target_prediction.parent_value,
        target_prediction.candidate_value,
        target_prediction.numerator,
        target_prediction.denominator,
        target_prediction.paired_delta,
        target_prediction.assessment,
        target_prediction.availability,
        target_prediction.unavailable_reason,
    ) == (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        3,
        Decimal("0"),
        "contradicted_on_cases",
        "measured",
        None,
    )
    assert "Processor time and peak memory were not measured or enforced by the synthetic fixture port." in (
        target_projection.limitations
    )
    assert "memory.selection=summary; sidecar restored from authenticated history." in target_projection.limitations
    assert any(
        row.prediction.metric_id == "evaluator.exit_attribution_count"
        and row.prediction.availability == "unavailable"
        and row.prediction.unavailable_reason == "evaluator_metric_missing"
        and row.prediction.denominator == 0
        for row in target_rows
    )

    target_return = target_stored.record.campaign_evidence.campaign_cagr_pct  # type: ignore[union-attr]
    sibling_return = sibling_stored.record.campaign_evidence.campaign_cagr_pct  # type: ignore[union-attr]
    parent_return = capability.authenticated_manifest.baseline_authority.campaign.campaign_cagr_pct
    assert parent_return < target_return < sibling_return
    mechanism_ids = {evidence_id for row in target_rows for evidence_id in row.evidence_ids}
    assert mechanism_ids.isdisjoint(shared_critic.evidence_ids)
    assert target_stored.record.critic_review is not None
    assert mechanism_ids.isdisjoint(target_stored.record.critic_review.evidence_ids)
    assert "exit.decision_changed_count" not in repr(shared_critic)

    # The next author request still uses the unchanged AuthorRoleInputV5, but
    # its hypothesis cites a real mechanism ID from this restarted request.
    mechanism_id = target_report_row.evidence_ids[0]
    next_hypothesis = replace(_hypothesis(), evidence_ids=(mechanism_id,))
    next_decision = ScheduledHypothesisV5(
        parent,
        next_hypothesis,
        hypothesis_novelty_key_v5(
            parent_revision_sha256=parent.policy_identity_sha256,
            hypothesis=next_hypothesis,
        ),
    )
    author_request = factory.author_request(
        inputs,
        projection,
        next_decision,
        InvestigatorArtifactV5((next_hypothesis,)),
    )
    assert type(author_request.role_input) is AuthorRoleInputV5
    assert author_request.role_input.hypothesis.evidence_ids == (mechanism_id,)
    assert author_request.role_evidence.items[0].evidence_id == mechanism_id
    assert author_request.role_evidence.items[0].metric_id == "exit.decision_changed_count.parent"
    assert author_request.role_evidence.items[0].value == target_prediction.parent_value

    # Reopen the repository a second time and require byte-identical request
    # and message reconstruction from the same checkpoint and journal.
    reopened = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    reopened_reducer = LocalArchiveReducerFactoryV5(reopened.repository).recovery_reducer(inputs)
    reopened_checkpoint, reopened_state = reopened.repository.recover_projection(reopened_reducer)
    reopened_records = tuple(
        StoredExperimentRecordV5(reference=ref, record=reopened.repository.load_experiment(ref))
        for ref in reopened_checkpoint.record_refs
    )
    reopened_projection = SearchProjectionV5(
        checkpoint=reopened_checkpoint,
        state=reopened_state,
        stored_records=reopened_records,
    )
    reopened_parent = select_parent_v5(
        state=reopened_state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=reopened_records,
    )
    reopened_factory = LocalRoleRequestFactoryV5(
        repository=reopened.repository,
        manifest=inputs.manifest,
        mechanism_adapter=MechanismRoleRequestAdapterV1(
            reopened,
            authenticated_manifest=capability.authenticated_manifest,
        ),
    )
    reopened_request = reopened_factory.investigator_request(inputs, reopened_projection, reopened_parent)
    assert reopened_request.sha256 == request.sha256
    assert canonical_json_bytes_v5(reopened_request.to_primitive()) == canonical_json_bytes_v5(request.to_primitive())
    assert reopened_request.messages == request.messages


def _assert_synthetic_role_admission(request, manifest) -> tuple[int, Decimal]:
    """Run the real wire/schema/overhead predicates on one final request."""

    provider_seed = ProviderCapabilitiesV5(
        model="synthetic-mechanism-budget",
        maximum_role_calls=3,
        maximum_total_tokens=10_000_000,
        maximum_output_tokens_per_role=request.max_output_tokens,
        maximum_usd=Decimal("10"),
        price_upper_bound=ModelPriceUpperBoundV5(
            model="synthetic-mechanism-budget",
            input_usd_per_million_tokens=Decimal("0.01"),
            output_usd_per_million_tokens=Decimal("0.01"),
        ),
        input_token_overhead_upper_bound=4096,
    )
    input_bound, cost_bound = prospective_role_usage_v5(request, provider_seed)
    wire_bytes = len(canonical_json_bytes_v5(wire_role_messages_v5(request.messages)))
    schema_bytes = max(
        len(request.schema_authority.canonical_schema_json),
        len(canonical_json_bytes_v5(wire_role_schema_v5(json.loads(request.schema_authority.canonical_schema_json)))),
    )
    assert input_bound == wire_bytes + schema_bytes + provider_seed.input_token_overhead_upper_bound
    assert input_bound > wire_bytes + schema_bytes
    assert cost_bound is not None and cost_bound > 0
    prospective_tokens = input_bound + request.max_output_tokens
    provider = replace(
        provider_seed,
        maximum_total_tokens=provider_seed.maximum_role_calls * prospective_tokens,
    )
    budget_manifest = replace(
        manifest,
        search=replace(
            manifest.search,
            hypotheses_per_investigator=1,
            max_tunable_axes=1,
            max_variants_per_template=1,
            max_discovery_survivors_per_template=1,
            allow_full_source_escape=False,
        ),
        provider=provider,
        pit_data_scope="development_sp500_v2",
        semantic_mode="disabled_development",
    )
    policy = CampaignAdmissionPolicyV5(
        schema_version=5,
        policy_revision=1,
        manifest_ref=ArtifactRefV5("synthetic/admission-manifest.json", budget_manifest.sha256),
        adapter_config_ref=ArtifactRefV5("synthetic/admission-config.json", "f" * 64),
        owner_token_sha256="e" * 64,
        repository_root_identity_sha256="d" * 64,
        target_evaluated_feedback_rounds=1,
        prospective_total_tokens_per_role=prospective_tokens,
    )
    require_role_envelope_v5(policy=policy, manifest=budget_manifest, request=request)
    with pytest.raises(ValueError, match="exceeds the campaign admission envelope"):
        require_role_envelope_v5(
            policy=replace(policy, prospective_total_tokens_per_role=prospective_tokens - 1),
            manifest=budget_manifest,
            request=request,
        )
    overhead_provider = replace(
        provider,
        input_token_overhead_upper_bound=provider.input_token_overhead_upper_bound + 1,
    )
    overhead_manifest = replace(budget_manifest, provider=overhead_provider)
    overhead_policy = replace(
        policy,
        manifest_ref=ArtifactRefV5(policy.manifest_ref.relative_path, overhead_manifest.sha256),
    )
    larger_input_bound, _ = prospective_role_usage_v5(request, overhead_provider)
    assert larger_input_bound == input_bound + 1
    with pytest.raises(ValueError, match="exceeds the campaign admission envelope"):
        require_role_envelope_v5(policy=overhead_policy, manifest=overhead_manifest, request=request)
    return input_bound, cost_bound


def test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supplied-port runtime runs the real role/checkpoint chronology once."""

    repository, capability, _, _ = _capability(
        tmp_path,
        hypotheses_per_investigator=3,
        max_variants_per_template=3,
        max_discovery_survivors_per_template=3,
        fixture_entry=True,
    )
    authenticated = capability.authenticated_manifest
    raw = repository.repository
    inputs, composed = compose_fixture_round_v5(
        repository=raw,
        authorities=authenticated,
        round_index=1,
    )
    extension = _LazyFixtureMechanismExtension(repository, capability, worker_factory=_fixture_workers)
    adapter = MechanismRoleRequestAdapterV1(repository, authenticated_manifest=authenticated)
    requests = LocalRoleRequestFactoryV5(
        repository=raw,
        manifest=authenticated.manifest,
        mechanism_adapter=adapter,
    )
    guarded_requests = []
    original_guard = requests._guarded_request

    def observe_guard(inputs, request):
        guarded_requests.append(request)
        return original_guard(inputs, request)

    monkeypatch.setattr(requests, "_guarded_request", observe_guard)
    candidates = _MechanismSyntheticCandidateRuntime(raw, inputs)
    dependencies = replace(
        composed,
        invoker=_MechanismFixtureRoleInvoker(authenticated.manifest),
        requests=requests,
        candidates=candidates,
        cleanup=candidates,
        mechanism=extension,
    )
    result = run_feedback_round_v5(inputs, dependencies)
    assert result.status == "completed", result.failure
    assert result.checkpoint is not None
    assert result.cleanup is not None and result.cleanup.cleanup_complete
    assert len(extension._reports) == 3
    assert all(run.execution.status == "completed" for run in extension._runs.values())
    assert all(run.coverage.total_cases > 0 for run in extension._runs.values())

    events = raw.load_round_events(campaign_id=inputs.campaign_id, round_index=inputs.round_index)
    payloads = tuple(raw.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in events)
    role_completions = tuple(item for item in payloads if type(item) is RoleCompletionPayloadV5)
    assert tuple(item.role for item in role_completions) == ("investigator", "author", "critic")
    _assert_fixture_role_chronology(
        raw,
        campaign_id=inputs.campaign_id,
        round_index=inputs.round_index,
    )
    critic_guards = tuple(item for item in guarded_requests if item.role == "critic")
    assert len(critic_guards) == 1
    assert type(critic_guards[0].role_input) is MechanismRoleInputV1
    intent_positions = tuple(index for index, item in enumerate(payloads) if type(item) is RoundIntentPayloadV5)
    assert intent_positions and intent_positions[0] < tuple(payloads).index(role_completions[1])
    investigator_package = raw.load_role_invocation(role_completions[0])
    author_package = raw.load_role_invocation(role_completions[1])
    assert type(author_package.request.role_input) is AuthorRoleInputV5
    assert author_package.request.role_input.hypothesis.evidence_ids == (
        investigator_package.request.role_evidence.items[0].evidence_id,
    )
    critic_package = raw.load_role_invocation(role_completions[-1])
    assert type(critic_package.request.role_input) is MechanismRoleInputV1
    wrapper = critic_package.request.role_input
    assert len(wrapper.projections) == 3
    assert {item.experiment_id for item in wrapper.projections} == set(extension._reports)
    issued = {item.evidence_id for item in critic_package.request.role_evidence.items}
    assert all(evidence_id in issued for item in wrapper.projections for evidence_id in item.evidence_ids)
    assert all(
        item.report_sha256 in {report.sha256 for report in extension._reports.values()} for item in wrapper.projections
    )
    assert all(item.memory_selection == "complete" for item in wrapper.projections)
    assert all(item.limitations for item in wrapper.projections)
    context_rows = tuple(
        row
        for item in wrapper.projections
        for row in item.rows
        if row.prediction.metric_id == "evaluator.exit_attribution_count"
    )
    assert len(context_rows) >= 2
    assert len(
        {
            (item.experiment_id, row.stage, row.episode_id, row.episode_ordinal, row.scenario_id)
            for item in wrapper.projections
            for row in item.rows
            if row.prediction.metric_id == "evaluator.exit_attribution_count"
        }
    ) == len(context_rows)

    # Exercise the unchanged accounting predicates against the actual final
    # augmented critic request.  The fixture is provider-free; the helper's
    # paid metadata is fresh typed authority for this pure check only.
    input_bound, cost_bound = _assert_synthetic_role_admission(
        critic_package.request,
        authenticated.manifest,
    )
    assert input_bound > len(canonical_json_bytes_v5(critic_package.request.messages))
    assert cost_bound > 0


def test_disabled_development_never_opens_supplied_workers(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(
        tmp_path, semantic_mode="disabled_development"
    )
    repository.append_precommitment(capability)
    calls = {"factory": 0}

    def factory(bound):
        calls["factory"] += 1
        raise AssertionError("disabled development must not request workers")

    extension = MechanismRuntimeExtensionV1(repository, capability, worker_factory=factory)
    extension.before_authoring(round_intent=capability.round_intent)
    run = extension.observe_candidate(
        experiment_id="2" * 64,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        semantic_outcome="behaviorally_distinct",
        semantic_recovered=False,
        deadline_monotonic=None,
    )
    assert run is not None
    assert run.execution.status == "not_run"
    assert run.execution.reason == "disabled_development"
    assert calls["factory"] == 0


def test_runtime_disabled_development_emits_not_run_without_workers(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path, semantic_mode="disabled_development")
    calls = {"factory": 0}
    extension = MechanismRuntimeExtensionV1(
        repository,
        capability,
        worker_factory=_counting_fixture_factory(calls),
    )
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
    )
    run = extension._runs[result["decision"]["experiment_id"]]
    assert result["decision"]["status"] == "quick_ready"
    assert run.execution.status == "not_run"
    assert run.execution.reason == "disabled_development"
    assert calls["factory"] == 0


def test_corrupt_run_sidecar_fails_closed(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    repository.append_precommitment(capability)
    extension = MechanismRuntimeExtensionV1(
        repository, capability, worker_factory=lambda bound: (_ for _ in ()).throw(AssertionError())
    )
    extension.before_authoring(round_intent=capability.round_intent)
    bound = capability.bind_candidate(
        experiment_id="3" * 64,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )
    repository.append_binding(bound)
    raw = b"partial"
    # An index-authenticated reference to the wrong bytes is the interrupted
    # write case: recovery must reject it before any worker factory is used.
    run_ref = repository.repository.append_binary_state(
        namespace="mechanism-v5",
        key=f"{bound.experiment_id}-run",
        content=raw,
    )
    binding_index = repository._load_experiment_index(bound.experiment_id, "binding")
    assert binding_index is not None
    run_index = repository._candidate_index(bound, "run", binding_index.binding_ref, run_ref)
    repository.repository.append_typed_state(
        namespace="mechanism-v5",
        key=f"{bound.experiment_id}-run",
        value=run_index,
    )
    with pytest.raises(MechanismArtifactCorrupt):
        repository.load_run(bound)


def test_runtime_finalization_matches_authenticated_parent_discovery_context(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability, worker_factory=_fixture_workers)
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    candidate = result["_candidate"]
    parent_campaign = capability.selected_parent_campaign_evidence
    assert parent_campaign is not None
    bound = extension._bound[result["decision"]["experiment_id"]]
    candidate_evidence = replace(
        candidate,
        discovery_episodes=tuple(
            replace(
                episode, evaluation=replace(episode.evaluation, policy_identity_sha256=bound.candidate_revision.sha256)
            )
            for episode in parent_campaign.episodes
        ),
        status="evaluated",
    )
    mismatch = replace(
        candidate_evidence,
        discovery_episodes=tuple(
            replace(episode, evaluation=replace(episode.evaluation, panel_sha256="f" * 64))
            for episode in candidate_evidence.discovery_episodes
        ),
    )
    assert extension._matched_evaluations_for_candidate(bound, mismatch) == ()
    report = extension.finalize_report(
        experiment_id=result["decision"]["experiment_id"],
        candidate_evidence=candidate_evidence,
    )
    assert report is not None
    assert len(report.consequence_contexts) == 4
    assert all(
        item.context.parent_policy_identity_sha256 == capability.parent_revision.sha256
        for item in report.consequence_contexts
    )
    assert all(
        item.context.candidate_policy_identity_sha256 == bound.candidate_revision.sha256
        for item in report.consequence_contexts
    )
    complete_path = (
        repository.repository.root / "adapter-state" / "mechanism-v5" / f"{bound.experiment_id}-complete.json"
    )
    complete_authority_path = (
        repository.repository.root / "adapter-state-authority" / "mechanism-v5" / f"{bound.experiment_id}-complete.json"
    )
    _unlink_fixture_path(complete_path)
    _unlink_fixture_path(complete_authority_path)
    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    assert restarted.load_report(bound) == report
    assert any(path.name == complete_path.name for path in repository.repository.root.rglob("*.json"))


def test_runtime_extension_is_called_after_render_and_finalized_separately(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(
        repository,
        capability,
        worker_factory=_fixture_workers,
    )

    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )

    assert result["decision"]["status"] == "quick_ready"
    assert result["call_counts"] == {
        "materialize": 1,
        "validate": 1,
        "fingerprint": 1,
        "evaluate_quick": 1,
    }
    experiment_id = result["decision"]["experiment_id"]
    assert extension._runs[experiment_id].execution.status == "completed"
    bound = extension._bound[experiment_id]
    candidate_evidence = _post_evaluation_candidate(result["_candidate"], capability, bound)
    report = extension.finalize_report(experiment_id=experiment_id, candidate_evidence=candidate_evidence)
    assert report is not None
    assert any(path.name.endswith("-spec.bin") for path in tmp_path.rglob("*"))
    assert any(path.name.endswith("-corpus.bin") for path in tmp_path.rglob("*"))
    assert any(path.name.endswith("-binding.bin") for path in tmp_path.rglob("*"))
    assert any(path.name.endswith("-run.bin") for path in tmp_path.rglob("*"))
    assert any(path.name.endswith("-report.bin") for path in tmp_path.rglob("*"))


@pytest.mark.parametrize("status", ("quick_rejected", "timed_out", "cancelled", "evaluation_failed"))
def test_noncomplete_candidate_status_keeps_local_report_and_evaluator_absence(
    tmp_path: Path,
    status: str,
) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability, worker_factory=_fixture_workers)
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    experiment_id = result["decision"]["experiment_id"]
    bound = extension._bound[experiment_id]
    candidate = replace(
        result["_candidate"],
        status=status,
        discovery_episodes=(),
        campaign_evidence=None,
        failure_code=(None if status == "quick_rejected" else f"{status}_fixture"),
    )

    report = extension.finalize_report(experiment_id=experiment_id, candidate_evidence=candidate)
    assert report is not None
    local = next(item for item in report.predictions if item.metric_id == "exit.decision_changed_count")
    evaluator = next(item for item in report.predictions if item.metric_id == "evaluator.exit_attribution_count")
    assert local.availability == "measured"
    assert local.assessment == "contradicted_on_cases"
    assert evaluator.availability == "unavailable"
    assert evaluator.unavailable_reason == "evaluator_metric_missing"
    assert all(item.unavailable_reason != "execution_failed" for item in report.predictions)

    capability_out, bound_out, run_out, report_out, intent_out = extension.role_request_evidence(experiment_id)
    assert capability_out == capability
    assert bound_out == bound
    assert run_out.execution.status == "completed"
    assert report_out == report
    issued: list[tuple[str, object]] = []
    projection = MechanismRoleRequestAdapterV1(repository).project_current_evidence(
        candidate=candidate,
        intent=intent_out,
        capability=capability_out,
        bound=bound_out,
        run=run_out,
        report=report_out,
        issue_evidence=lambda metric_id, value: (
            issued.append((metric_id, value)) or f"v5.final-fix.{len(issued)}"
        ),
    )
    assert projection.execution == report.execution
    assert any(
        row.prediction.metric_id == "exit.decision_changed_count" and row.prediction.availability == "measured"
        for row in projection.rows
    )
    assert any(
        row.prediction.metric_id == "evaluator.exit_attribution_count"
        and row.prediction.availability == "unavailable"
        and row.prediction.unavailable_reason == "evaluator_metric_missing"
        for row in projection.rows
    )


def test_supplied_port_quick_rejections_reach_mixed_current_critic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid survivor rejection stays in the critic batch with local evidence."""

    import core.pit_optimizer_v5.runtime as runtime_module

    repository, capability, _, _ = _capability(
        tmp_path,
        hypotheses_per_investigator=3,
        max_variants_per_template=3,
        max_discovery_survivors_per_template=3,
        fixture_entry=True,
    )
    authenticated = capability.authenticated_manifest
    raw = repository.repository
    inputs, composed = compose_fixture_round_v5(repository=raw, authorities=authenticated, round_index=1)
    monkeypatch.setattr(
        runtime_module,
        "select_manifest_discovery_survivors_v5",
        lambda **kwargs: tuple(kwargs["candidates"][:1]),
    )
    calls = {"factory": 0}
    extension = _LazyFixtureMechanismExtension(
        repository,
        capability,
        worker_factory=_counting_fixture_factory(calls),
    )
    adapter = MechanismRoleRequestAdapterV1(repository, authenticated_manifest=authenticated)
    requests = LocalRoleRequestFactoryV5(repository=raw, manifest=authenticated.manifest, mechanism_adapter=adapter)
    candidates = _MechanismSyntheticCandidateRuntime(raw, inputs)
    dependencies = replace(
        composed,
        invoker=_MechanismFixtureRoleInvoker(authenticated.manifest),
        requests=requests,
        candidates=candidates,
        cleanup=candidates,
        mechanism=extension,
    )

    result = run_feedback_round_v5(inputs, dependencies)
    assert result.status == "completed", result.failure
    assert result.checkpoint is not None
    assert len(extension._reports) == 3
    assert len(extension._reports) == len(extension._runs)
    assert calls["factory"] == 3
    critic_events = tuple(
        item
        for event in raw.load_round_events(campaign_id=inputs.campaign_id, round_index=inputs.round_index)
        for item in (raw.load_round_payload(event.payload_ref, expected_kind=event.event_kind),)
        if type(item) is RoleCompletionPayloadV5 and item.role == "critic"
    )
    assert len(critic_events) == 1
    critic = raw.load_role_invocation(critic_events[0])
    assert type(critic.request.role_input) is MechanismRoleInputV1
    assert {item.experiment_id for item in critic.request.role_input.projections} == set(extension._reports)
    assert any(
        item.status == "quick_rejected"
        for ref in result.record_refs
        for item in (StoredExperimentRecordV5(reference=ref, record=raw.load_experiment(ref)).record,)
    )


def test_supplied_port_discovery_failure_keeps_local_report_and_completes_round(
    tmp_path: Path,
) -> None:
    repository, capability, _, _ = _capability(
        tmp_path,
        hypotheses_per_investigator=3,
        max_variants_per_template=3,
        max_discovery_survivors_per_template=3,
        fixture_entry=True,
    )
    authenticated = capability.authenticated_manifest
    raw = repository.repository
    inputs, composed = compose_fixture_round_v5(repository=raw, authorities=authenticated, round_index=1)
    calls = {"factory": 0}
    extension = _LazyFixtureMechanismExtension(
        repository,
        capability,
        worker_factory=_counting_fixture_factory(calls),
    )
    adapter = MechanismRoleRequestAdapterV1(repository, authenticated_manifest=authenticated)
    requests = LocalRoleRequestFactoryV5(repository=raw, manifest=authenticated.manifest, mechanism_adapter=adapter)
    candidates = _FailingDiscoveryMechanismCandidateRuntime(raw, inputs)
    dependencies = replace(
        composed,
        invoker=_MechanismFixtureRoleInvoker(authenticated.manifest),
        requests=requests,
        candidates=candidates,
        cleanup=candidates,
        mechanism=extension,
    )

    result = run_feedback_round_v5(inputs, dependencies)
    assert result.status == "completed", result.failure
    assert result.checkpoint is not None
    assert len(extension._reports) == 3
    assert calls["factory"] == 3
    assert all(
        next(item for item in report.predictions if item.metric_id == "exit.decision_changed_count").availability
        == "measured"
        for report in extension._reports.values()
    )
    records = tuple(raw.load_experiment(ref) for ref in result.record_refs)
    assert records and all(item.status == "evaluation_failed" for item in records)

    restarted = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(tmp_path))
    recovered_checkpoint, recovered_state = restarted.repository.recover_projection(
        LocalArchiveReducerFactoryV5(restarted.repository).recovery_reducer(inputs)
    )
    recovered_records = tuple(
        StoredExperimentRecordV5(reference=ref, record=restarted.repository.load_experiment(ref))
        for ref in recovered_checkpoint.record_refs
    )
    recovered_projection = SearchProjectionV5(
        checkpoint=recovered_checkpoint,
        state=recovered_state,
        stored_records=recovered_records,
    )
    recovered_parent = select_parent_v5(
        state=recovered_state,
        baseline=inputs.baseline,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        stored_records=recovered_records,
    )
    restarted_factory = LocalRoleRequestFactoryV5(
        repository=restarted.repository,
        manifest=inputs.manifest,
        mechanism_adapter=MechanismRoleRequestAdapterV1(
            restarted,
            authenticated_manifest=authenticated,
        ),
    )
    restarted_inputs = replace(inputs, round_index=2)
    investigator_request = restarted_factory.investigator_request(
        restarted_inputs,
        recovered_projection,
        recovered_parent,
    )
    assert type(investigator_request.role_input) is MechanismRoleInputV1
    assert {item.experiment_id for item in investigator_request.role_input.projections} == {
        item.record.experiment_id for item in recovered_records
    }
    assert investigator_request.role_input.omitted == ()
    assert all(
        any(
            row.prediction.metric_id == "exit.decision_changed_count"
            and row.prediction.availability == "measured"
            for row in item.rows
        )
        for item in investigator_request.role_input.projections
    )


def test_runtime_rejected_candidates_do_not_open_supplemental_workers(tmp_path: Path) -> None:
    cases = (
        ("invalid", {"validation_valid": False}, "invalid"),
        ("exact", {"candidate_suffix": ""}, "exact_duplicate"),
        ("equivalent", {"semantic_kind": "equivalent"}, "behavioral_equivalent"),
        ("sibling", {"semantic_kind": "sibling"}, "sibling_equivalent"),
    )
    for name, options, expected_status in cases:
        case_root = tmp_path / name
        case_root.mkdir()
        repository, capability, _, _ = _capability(case_root)
        calls = {"factory": 0}
        extension = MechanismRuntimeExtensionV1(
            repository,
            capability,
            worker_factory=_counting_fixture_factory(calls),
        )
        result = run_legacy_fixture(
            mechanism=extension,
            round_intent=capability.round_intent,
            parent_candidate=capability.parent_candidate,
            authenticated_manifest=capability.authenticated_manifest,
            **options,
        )

        assert result["decision"]["status"] == expected_status
        assert calls["factory"] == 0
        assert not any(path.name.endswith("-binding.bin") for path in case_root.rglob("*"))
        assert not any(path.name.endswith("-run.bin") for path in case_root.rglob("*"))


def test_runtime_recovered_semantic_stage_without_complete_observation_is_not_run(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    calls = {"factory": 0}

    def factory(bound):
        calls["factory"] += 1
        return _fixture_workers(bound)

    extension = MechanismRuntimeExtensionV1(repository, capability, worker_factory=factory)
    result = run_legacy_fixture(
        mechanism=extension,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        recovered_semantic=True,
        authenticated_manifest=capability.authenticated_manifest,
    )

    experiment_id = result["decision"]["experiment_id"]
    recovered_run = extension._runs[experiment_id]
    assert result["decision"]["status"] == "quick_ready"
    assert recovered_run.execution.status == "not_run"
    assert recovered_run.execution.reason == "worker_unavailable"
    assert calls["factory"] == 0


def test_runtime_missing_executor_and_expired_deadline_preserve_candidate_outcome(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    missing = MechanismRuntimeExtensionV1(repository, capability)
    missing_result = run_legacy_fixture(
        mechanism=missing,
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
    )
    missing_run = missing._runs[missing_result["decision"]["experiment_id"]]
    assert missing_result["decision"]["status"] == "quick_ready"
    assert missing_run.execution.status == "not_run"
    assert missing_run.execution.reason == "worker_unavailable"

    deadline_root = tmp_path / "expired"
    deadline_root.mkdir()
    deadline_repository, deadline_capability, _, _ = _capability(deadline_root)
    calls = {"factory": 0}

    def factory(bound):
        calls["factory"] += 1
        return _fixture_workers(bound)

    deadline_extension = MechanismRuntimeExtensionV1(
        deadline_repository,
        deadline_capability,
        worker_factory=factory,
    )
    deadline_result = run_legacy_fixture(
        mechanism=deadline_extension,
        round_intent=deadline_capability.round_intent,
        parent_candidate=deadline_capability.parent_candidate,
        clock_value=1.0,
        authenticated_manifest=deadline_capability.authenticated_manifest,
    )
    deadline_run = deadline_extension._runs[deadline_result["decision"]["experiment_id"]]
    assert deadline_result["decision"]["status"] == "quick_ready"
    assert deadline_run.execution.status == "failed"
    assert deadline_run.execution.reason == "timeout"
    assert calls["factory"] == 1


def test_new_dto_schema_versions_reject_bool(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    with pytest.raises(MechanismCapabilityError):
        replace(capability, schema_version=True)
    with pytest.raises(ValueError):
        replace(capability.authorization, schema_version=True)

    repository.append_precommitment(capability)
    bound = capability.bind_candidate(
        experiment_id="5" * 64,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )
    repository.append_binding(bound)
    index = repository._load_experiment_index(bound.experiment_id, "binding")
    assert isinstance(index, MechanismExperimentIndexV1)
    with pytest.raises(ValueError):
        replace(index, schema_version=True)


def test_extension_absent_fixture_shape_is_stable() -> None:
    value = run_legacy_fixture()
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == LEGACY_GOLDEN_JSON_SHA256_V5


def test_extension_off_factory_requests_and_messages_match_recorded_baseline(tmp_path: Path) -> None:
    """The opt-in wrapper leaves all three legacy factory wires byte stable."""

    parent_bundle = _source_bundle()
    parent_revision = derive_policy_revision_identity_v5(
        source_bundle=parent_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    repository = LocalArtifactRepositoryV5(tmp_path)
    authenticated = _authenticated_fixture(
        repository=repository,
        parent_bundle=parent_bundle,
        parent_revision=parent_revision,
        semantic_mode="required",
    )
    manifest = authenticated.manifest
    inputs = FeedbackRoundInputV5(
        manifest=manifest,
        panel_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        baseline=authenticated.baseline_authority,
        round_index=1,
        owner_token_sha256=canonical_sha256_v5({"fixture": manifest.sha256, "round": 1}),
    )
    checkpoint, state = repository.recover_projection(LocalArchiveReducerFactoryV5(repository).recovery_reducer(inputs))
    projection = SearchProjectionV5(checkpoint=checkpoint, state=state, stored_records=())
    parent = baseline_parent_candidate_v5(
        authority=authenticated.baseline_authority,
        discovery_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        pit_data_scope=manifest.pit_data_scope,
    )
    factory = LocalRoleRequestFactoryV5(repository=repository, manifest=manifest)
    investigator = factory.investigator_request(inputs, projection, parent)
    hypothesis = replace(
        _hypothesis(),
        evidence_ids=(investigator.role_evidence.items[0].evidence_id,),
    )
    investigator_artifact = InvestigatorArtifactV5((hypothesis,))
    decision = ScheduledHypothesisV5(
        parent,
        hypothesis,
        hypothesis_novelty_key_v5(
            parent_revision_sha256=parent.policy_identity_sha256,
            hypothesis=hypothesis,
        ),
    )
    author = factory.author_request(inputs, projection, decision, investigator_artifact)
    candidate_a = _legacy_golden_candidate(
        authenticated=authenticated,
        parent=parent,
        hypothesis=hypothesis,
        discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
        candidate_suffix="baseline-candidate-a",
        cagr="1.00",
    )
    candidate_b = _legacy_golden_candidate(
        authenticated=authenticated,
        parent=parent,
        hypothesis=hypothesis,
        discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
        candidate_suffix="baseline-candidate-b",
        cagr="2.00",
    )
    critic = factory.critic_request(inputs, projection, decision, (candidate_a, candidate_b))

    expected = {
        "investigator": (
            "a766155d672fa59bf104a7257608a958607c1e13aa1f3eea97b3e2183b53cb02",
            6985,
            "ca912c183de58a2ed669bfb91b43bee1b95449b7b9c1f5c7d7168847f115f3d3",
            3844,
            "1de79d44ac4755db99b3212f1d841f10cf42e7ec5d4fa6ce683ae945e601746b",
            16,
        ),
        "author": (
            "ad1ed91033ba04ce30ff28044427724b275b83b4029d641e8f5483e77f9249f7",
            14587,
            "d193ee5550e5692f5268448d0f868e5341ddd886c0177b8846549beff11a6113",
            13898,
            "ee84efcb09011f876185d472be042165f25f2d97bd34b7a00b08399da98ce43e",
            1,
        ),
        "critic": (
            "d1c35a4616737a0021a6d2668c7e859bf147aa2c01887df1bb847e192de8a535",
            130134,
            "c3961b57625bf3a11543416781ba85f6ebcbf0bec52be703a5c76e2a33874b89",
            75405,
            "3a5ea7a9487d4cd1fd1e8995d948b3d0aa430f3e3111fbb97d3920bf9c7861d7",
            340,
        ),
    }
    for name, request in (("investigator", investigator), ("author", author), ("critic", critic)):
        request_bytes = canonical_json_bytes_v5(request.to_primitive())
        message_bytes = canonical_json_bytes_v5(request.messages)
        request_sha, request_size, messages_sha, messages_size, schema_sha, evidence_count = expected[name]
        assert request.sha256 == request_sha
        assert hashlib.sha256(request_bytes).hexdigest() == request_sha
        assert len(request_bytes) == request_size
        assert hashlib.sha256(message_bytes).hexdigest() == messages_sha
        assert len(message_bytes) == messages_size
        assert request.response_schema_sha256 == schema_sha
        assert len(request.role_evidence.items) == evidence_count


def test_mechanism_role_wrapper_keeps_typed_row_ids_and_rejects_unissued_ids() -> None:
    metric = MechanismMetricSpecV1(
        metric_id="exit.decision_changed_count",
        unit="count",
        direction="increase",
        tolerance=Decimal(0),
        denominator="relevant_cases",
    )
    prediction = MechanismPredictionResultV1.from_measurement(
        metric,
        parent_value=Decimal(0),
        candidate_value=Decimal(1),
        numerator=Decimal(1),
        denominator=2,
        minimum_relevant_cases=1,
    )
    hashes = {
        "report": "1" * 64,
        "parent": "2" * 64,
        "candidate": "3" * 64,
        "parent_source": "4" * 64,
        "candidate_source": "5" * 64,
        "evaluator": "6" * 64,
        "corpus": "7" * 64,
    }
    ids = tuple(
        f"v5.investigator.{ordinal}.exit.decision_changed_count.{suffix}"
        for ordinal, suffix in enumerate(("parent", "candidate", "numerator", "denominator", "delta"), 1)
    )
    row = MechanismEvidenceRowV1(
        stage="report",
        episode_id=None,
        episode_ordinal=None,
        scenario_id=None,
        report_sha256=hashes["report"],
        hypothesis_id="hyp.exit.atr",
        experiment_id="8" * 64,
        parent_revision_sha256=hashes["parent"],
        candidate_revision_sha256=hashes["candidate"],
        parent_source_bundle_sha256=hashes["parent_source"],
        candidate_source_bundle_sha256=hashes["candidate_source"],
        evaluator_contract_sha256=hashes["evaluator"],
        corpus_sha256=hashes["corpus"],
        prediction=prediction,
        evidence_ids=ids,
    )
    projection = MechanismRoleProjectionV1(
        experiment_id="8" * 64,
        hypothesis_id="hyp.exit.atr",
        hypothesis_claim="ATR-aware exit behavior changes exit decisions.",
        predicted_changes=_hypothesis().predicted_changes,
        report_sha256=hashes["report"],
        parent_revision_sha256=hashes["parent"],
        candidate_revision_sha256=hashes["candidate"],
        parent_source_bundle_sha256=hashes["parent_source"],
        candidate_source_bundle_sha256=hashes["candidate_source"],
        evaluator_contract_sha256=hashes["evaluator"],
        corpus_sha256=hashes["corpus"],
        execution=MechanismExecutionV1("completed", "completed"),
        controls=(MechanismControlV1("protected_next_stop_price"),),
        applicability=MechanismPredicateV1("features.atr_20_fraction", "is_present", None),
        coverage=MechanismCoverageV1(2, 2, 1, 2, 2, 0),
        rows=(row,),
        limitations=("Synthetic fixture report.",),
    )
    issued = RoleEvidenceV5(
        5,
        tuple(
            RoleEvidenceItemV5(evidence_id, metric_id, value)
            for evidence_id, metric_id, value in zip(
                ids,
                (
                    "exit.decision_changed_count.parent",
                    "exit.decision_changed_count.candidate",
                    "exit.decision_changed_count.numerator",
                    "exit.decision_changed_count.denominator",
                    "exit.decision_changed_count.delta",
                ),
                (0, 1, 1, 2, 1),
                strict=True,
            )
        ),
    )
    base = InvestigatorRoleInputV5(("v5.investigator.base",), (), ())
    wrapped = MechanismRoleInputV1("investigator", base, projection)

    _validate_mechanism_role_projection(projection, issued_evidence=issued)
    assert wrapped.base is base
    assert _role_citation_sequence(wrapped) == ("v5.investigator.base", *ids)
    with pytest.raises(ValueError, match="outside this request"):
        _validate_mechanism_role_projection(
            replace(projection, rows=(replace(row, evidence_ids=(*ids[:-1], "v5.investigator.foreign")),)),
            issued_evidence=issued,
        )
    wrong_value = RoleEvidenceV5(
        5,
        tuple(
            RoleEvidenceItemV5(
                item.evidence_id, item.metric_id, 99 if item.metric_id.endswith(".delta") else item.value
            )
            for item in issued.items
        ),
    )
    with pytest.raises(ValueError, match="value differs"):
        _validate_mechanism_role_projection(projection, issued_evidence=wrong_value)
    with pytest.raises(ValueError, match="declared prediction"):
        _validate_mechanism_role_projection(
            replace(
                projection,
                predicted_changes=(
                    *_hypothesis().predicted_changes,
                    MetricPredictionV5(
                        metric_id="exit.protected_control_unchanged_count",
                        direction="unchanged",
                        rationale="The protected control must remain unchanged.",
                    ),
                ),
            ),
            issued_evidence=issued,
        )
    with pytest.raises(ValueError, match="execution status"):
        _validate_mechanism_role_projection(
            replace(projection, execution=MechanismExecutionV1("failed", "timeout")),
            issued_evidence=issued,
        )
    with pytest.raises(ValueError, match="evidence IDs"):
        _validate_mechanism_role_projection(
            replace(projection, rows=(replace(row, evidence_ids=()),)),
            issued_evidence=issued,
        )
    wide_projections = tuple(
        replace(
            projection,
            experiment_id=f"{ordinal:064x}",
            rows=(replace(row, experiment_id=f"{ordinal:064x}"),),
        )
        for ordinal in range(1, 10)
    )
    wide_wrapper = MechanismRoleInputV1("investigator", base, wide_projections)
    assert len(wide_wrapper.projections) == 9


def test_precommitment_reauthenticates_manifest_with_current_repository(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    foreign_root = tmp_path / "foreign"
    source_root.mkdir()
    foreign_root.mkdir()
    repository, capability, _, _ = _capability(source_root)
    foreign = MechanismArtifactRepositoryV5(LocalArtifactRepositoryV5(foreign_root))

    with pytest.raises(MechanismCapabilityError):
        foreign.append_precommitment(capability)

    mismatched_intent = replace(capability.round_intent, pit_data_scope="development_sp500_v2")
    mismatched_spec = replace(
        capability.spec,
        round_intent_sha256=round_intent_sha256_v1(mismatched_intent),
    )
    object.__setattr__(capability, "round_intent", mismatched_intent)
    object.__setattr__(capability, "spec", mismatched_spec)
    with pytest.raises(MechanismCapabilityError):
        repository.append_precommitment(capability)


def test_registered_sandbox_rejects_synthetic_factory_before_invocation(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    registered = replace(capability, execution_kind="registered_sandbox")
    calls = {"factory": 0}

    def factory(bound):
        calls["factory"] += 1
        return _fixture_workers(bound)

    extension = MechanismRuntimeExtensionV1(repository, registered, worker_factory=factory)
    extension.before_authoring(round_intent=registered.round_intent)
    run = extension.observe_candidate(
        experiment_id="6" * 64,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
        semantic_outcome="behaviorally_distinct",
        semantic_recovered=False,
        deadline_monotonic=None,
    )
    assert run is not None
    assert run.execution.status == "not_run"
    assert run.execution.reason == "worker_unavailable"
    assert calls["factory"] == 0


def test_orphan_run_blob_fails_before_worker_factory(tmp_path: Path) -> None:
    repository, capability, candidate_bundle, candidate_revision = _capability(tmp_path)
    repository.append_precommitment(capability)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    extension.before_authoring(round_intent=capability.round_intent)
    bound = capability.bind_candidate(
        experiment_id="7" * 64,
        candidate_revision=candidate_revision,
        candidate_source_bundle=candidate_bundle,
    )
    repository.append_binding(bound)
    repository.repository.append_binary_state(
        namespace="mechanism-v5",
        key=f"{bound.experiment_id}-run",
        content=b"orphan",
    )
    calls = {"factory": 0}

    def factory(bound):
        calls["factory"] += 1
        return _fixture_workers(bound)

    extension.worker_factory = factory
    with pytest.raises(MechanismArtifactCorrupt):
        extension.observe_candidate(
            experiment_id=bound.experiment_id,
            candidate_revision=candidate_revision,
            candidate_source_bundle=candidate_bundle,
            semantic_outcome="behaviorally_distinct",
            semantic_recovered=False,
            deadline_monotonic=None,
        )
    assert calls["factory"] == 0


def test_finalization_has_no_caller_supplied_match_override(tmp_path: Path) -> None:
    repository, capability, _, _ = _capability(tmp_path)
    extension = MechanismRuntimeExtensionV1(repository, capability)
    with pytest.raises(TypeError):
        extension.finalize_report(
            experiment_id="8" * 64,
            candidate_evidence=None,
            matched_evaluations=(),
        )
