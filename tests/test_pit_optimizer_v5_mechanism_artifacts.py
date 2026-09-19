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
from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    SourceFileV5,
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
    PanelEvaluationV5,
    ResourceCapabilitiesV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    SandboxProfileV5,
    ScenarioPanelEvaluationV5,
    SearchCapabilitiesV5,
    canonical_sha256_v5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    PolicySourceSnapshotV5,
    build_campaign_manifest_v5,
)
from core.pit_optimizer_v5.memory import ExperimentRecordV5, RoundIntentPayloadV5, StoredExperimentRecordV5
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
    MechanismExperimentSpecV1,
    MechanismResourceBudgetV1,
)
from core.pit_optimizer_v5.mechanism_probes import (
    MechanismWorkerRegistrationV1,
    SyntheticFixtureWorkerV1,
    build_mechanism_observation_corpus_v1,
)
from core.pit_optimizer_v5.probes import policy_probe_suite_v1
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5
from core.pit_optimizer_v5.runtime import FeedbackRoundInputV5, SearchProjectionV5
from core.pit_optimizer_v5.search import (
    ArchiveRecordAuthorityV5,
    ArchiveEntryV5,
    BaselineParentAuthorityV5,
    CandidateArchiveV5,
    SearchStateV5,
    archive_parent_from_record_v5,
    baseline_parent_candidate_v5,
    expected_target_gap_pct_v5,
)
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
    RoleBindingV5,
    RoleCallKeyV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
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


def _source_bundle(suffix: str = "") -> SourceBundleV5:
    root = Path(__file__).resolve().parents[1]
    files = []
    for path in EDITABLE_POLICY_PATHS_V5:
        content = (root / path).read_text(encoding="utf-8")
        if suffix and path == EDITABLE_POLICY_PATHS_V5[-1]:
            content = content.rstrip("\n") + f"\n# {suffix}\n"
        files.append(SourceFileV5(path=path, source=content))
    return SourceBundleV5(files=tuple(files))


def _seed_snapshot() -> object:
    return next(case.snapshot for case in policy_probe_suite_v1() if case.method == "evaluate_exit")


def _spec(*, parent_revision_sha256: str, round_intent_sha256: str) -> MechanismExperimentSpecV1:
    from core.pit_optimizer_v5.mechanism_contracts import (
        MechanismControlV1,
        MechanismDisconfirmingObservationV1,
        MechanismDiagnosticSelectorV1,
        MechanismMetricSpecV1,
        MechanismPredicateV1,
        MechanismRecipeV1,
    )

    hypothesis = _hypothesis()
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


def _static_fingerprint(*, distinct: bool = False) -> SemanticFingerprintV5:
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
                stop = case.snapshot.base.protective_stop_candidates[-1]
            decision = ExitDecision(
                actions=(),
                next_stop_price=stop,
                early_winner_hold=case.snapshot.base.early_winner_hold,
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
            hypotheses_per_investigator=1,
            max_tunable_axes=1,
            max_variants_per_template=2,
            max_discovery_survivors_per_template=1,
            archive_capacity=2,
            max_feedback_rounds=2,
            allow_full_source_escape=False,
        ),
        resources=resources,
        provider=None,
        pit_data_scope=("development_sp500_v2" if semantic_mode == "disabled_development" else "production"),
        semantic_mode=semantic_mode,
    )


def _capability(
    tmp_path: Path, *, semantic_mode: str = "required"
) -> tuple[
    MechanismArtifactRepositoryV5,
    MechanismExtensionCapabilityV1,
    SourceBundleV5,
    PolicyRevisionIdentityV5,
]:
    parent_bundle = _source_bundle()
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


def _persist_evaluated_record(
    repository: MechanismArtifactRepositoryV5,
    capability: MechanismExtensionCapabilityV1,
    candidate_bundle: SourceBundleV5,
    candidate_revision: PolicyRevisionIdentityV5,
) -> StoredExperimentRecordV5:
    """Persist one complete typed candidate checkpoint for archive/restart tests."""

    result = run_legacy_fixture(
        round_intent=capability.round_intent,
        parent_candidate=capability.parent_candidate,
        authenticated_manifest=capability.authenticated_manifest,
        return_candidate=True,
    )
    candidate = result["_candidate"]
    assert candidate.materialized.variant.source_bundle == candidate_bundle
    assert candidate.materialized.variant.policy_revision == candidate_revision

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
    positive_report = replace(_zero_report(), closed_trades=1)
    parent_campaign = capability.authenticated_manifest.baseline_authority.campaign
    episodes = tuple(
        replace(
            episode,
            evaluation=replace(
                episode.evaluation,
                policy_identity_sha256=candidate_revision.sha256,
                scenarios=tuple(replace(scenario, report=positive_report) for scenario in episode.evaluation.scenarios),
            ),
        )
        for episode in parent_campaign.episodes
    )
    campaign = CampaignEvidenceV5(
        discovery_plan_sha256=capability.authenticated_manifest.panel_plan.discovery_plan_sha256,
        episodes=episodes,
        campaign_cagr_pct=Decimal("0"),
        closed_trades=4,
    )
    review = CriticReviewV5(
        experiment_id=candidate.experiment_id,
        prediction_vs_observation="fixture candidate matched the declared mechanism.",
        causal_explanation="fixture evidence is complete and deterministic.",
        evidence_ids=("v5.fixture.evidence",),
        disposition="promote",
        next_direction="fixture follow-up",
    )
    critic_ref = repository.repository.append_critic(
        CriticArtifactV5(
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
    reducer = LocalArchiveReducerFactoryV5(repository.repository).publication_reducer(
        inputs=inputs,
        prior=prior,
        records=(StoredExperimentRecordV5(reference=record_ref, record=record),),
        source_authorities=(authority,),
    )
    repository.repository.publish_projection(
        record_refs=(record_ref,),
        reducer=reducer,
        generation=1,
    )
    return StoredExperimentRecordV5(reference=record_ref, record=record)


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
