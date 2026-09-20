"""Fresh synthetic authority graph used by the V5 two-round study."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from datetime import date
import hashlib
from pathlib import Path

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    CampaignEvidenceV5,
    CampaignPanelPlanV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    EvaluationReportV5,
    PanelEvaluationV5,
    ResourceCapabilitiesV5,
    SandboxProfileV5,
    ScenarioPanelEvaluationV5,
    initial_friction_grid_v5,
    SearchCapabilitiesV5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    PolicySourceSnapshotV5,
    authenticate_campaign_manifest_v5,
    build_campaign_manifest_v5,
)
from core.pit_optimizer_v5.mechanism_contracts import MechanismResourceBudgetV1
from core.pit_optimizer_v5.search import annualized_return_pct, BaselineParentAuthorityV5

from .registry import (
    FrozenBehaviorRegistryV1,
    SyntheticPortfolioEvaluatorInputV1,
    build_study_registry_v1,
)


_SOURCE_COMMIT = "a" * 40
_FIXTURE_NAMESPACE = "study-fixture"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class StudyReturnInputV1:
    """Supplied synthetic portfolio endpoints and derived CAGR values."""

    configuration_id: str
    starting_equity: Decimal
    ending_equity: Decimal
    gross_ending_equity: Decimal
    stress_ending_equity: Decimal
    days: int
    base_annualized_return_pct: Decimal
    gross_annualized_return_pct: Decimal
    stress_annualized_return_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.configuration_id) is not str or not self.configuration_id:
            raise ValueError("synthetic return configuration is invalid")
        values = (
            self.starting_equity,
            self.ending_equity,
            self.gross_ending_equity,
            self.stress_ending_equity,
        )
        if any(type(value) is not Decimal or not value.is_finite() or value <= 0 for value in values):
            raise ValueError("synthetic return endpoints are invalid")
        if type(self.days) is not int or self.days <= 0:
            raise ValueError("synthetic return duration is invalid")
        expected = (
            annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.ending_equity,
                days=self.days,
            ),
            annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.gross_ending_equity,
                days=self.days,
            ),
            annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.stress_ending_equity,
                days=self.days,
            ),
        )
        supplied = (
            self.base_annualized_return_pct,
            self.gross_annualized_return_pct,
            self.stress_annualized_return_pct,
        )
        if supplied != expected:
            raise ValueError("synthetic return CAGR values do not derive from supplied endpoints")

    @classmethod
    def from_registry_input(cls, value: SyntheticPortfolioEvaluatorInputV1) -> "StudyReturnInputV1":
        if value.round_index != 1:
            raise ValueError("round-one portfolio input is required")
        return cls(
            configuration_id=value.configuration_id,
            starting_equity=value.starting_equity,
            ending_equity=value.ending_equity,
            gross_ending_equity=value.gross_ending_equity,
            stress_ending_equity=value.stress_ending_equity,
            days=value.days,
            base_annualized_return_pct=value.base_annualized_return_pct,
            gross_annualized_return_pct=value.gross_annualized_return_pct,
            stress_annualized_return_pct=value.stress_annualized_return_pct,
        )


@dataclass(frozen=True, slots=True)
class StudyRoundOutcomeV1:
    """Frozen supplied round-two portfolio inputs, without mechanism metrics."""

    round_index: int
    configuration_id: str
    starting_equity: Decimal
    ending_equity: Decimal
    gross_ending_equity: Decimal
    stress_ending_equity: Decimal
    days: int
    base_annualized_return_pct: Decimal
    gross_annualized_return_pct: Decimal
    stress_annualized_return_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.round_index) is not int or self.round_index != 2:
            raise ValueError("round outcome index is invalid")
        SyntheticPortfolioEvaluatorInputV1(
            round_index=2,
            configuration_id=self.configuration_id,
            starting_equity=self.starting_equity,
            ending_equity=self.ending_equity,
            gross_ending_equity=self.gross_ending_equity,
            stress_ending_equity=self.stress_ending_equity,
            days=self.days,
            base_annualized_return_pct=self.base_annualized_return_pct,
            gross_annualized_return_pct=self.gross_annualized_return_pct,
            stress_annualized_return_pct=self.stress_annualized_return_pct,
        )

    @classmethod
    def from_registry_input(cls, value: SyntheticPortfolioEvaluatorInputV1) -> "StudyRoundOutcomeV1":
        if value.round_index != 2:
            raise ValueError("round-two portfolio input is required")
        return cls(
            round_index=2,
            configuration_id=value.configuration_id,
            starting_equity=value.starting_equity,
            ending_equity=value.ending_equity,
            gross_ending_equity=value.gross_ending_equity,
            stress_ending_equity=value.stress_ending_equity,
            days=value.days,
            base_annualized_return_pct=value.base_annualized_return_pct,
            gross_annualized_return_pct=value.gross_annualized_return_pct,
            stress_annualized_return_pct=value.stress_annualized_return_pct,
        )

    @property
    def cagr_pct(self) -> Decimal:
        return self.base_annualized_return_pct


@dataclass(frozen=True, slots=True)
class StudyFixtureV1:
    """The four authorities required by the later two-round runtime."""

    repository: LocalArtifactRepositoryV5
    manifest: AuthenticatedCampaignManifestV5
    registry: FrozenBehaviorRegistryV1
    baseline_configuration_id: str
    resource_budget: MechanismResourceBudgetV1
    synthetic_return_inputs: tuple[StudyReturnInputV1, ...] = ()
    round_two_outcomes: tuple[StudyRoundOutcomeV1, ...] = ()

    def __post_init__(self) -> None:
        if type(self.repository) is not LocalArtifactRepositoryV5:
            raise ValueError("study fixture repository is invalid")
        if type(self.manifest) is not AuthenticatedCampaignManifestV5:
            raise ValueError("study fixture manifest is invalid")
        if type(self.registry) is not FrozenBehaviorRegistryV1:
            raise ValueError("study fixture registry is invalid")
        from .registry import verify_registry_v1

        verify_registry_v1(self.registry)
        authenticated = authenticate_campaign_manifest_v5(
            repository=self.repository,
            manifest_ref=self.manifest.manifest_ref,
        )
        if authenticated != self.manifest:
            raise ValueError("study fixture manifest is not authenticated by its repository")
        if self.resource_budget != study_resource_budget_v1():
            raise ValueError("study fixture resource budget differs from the frozen bound")
        _validate_fixture_authority(
            manifest=self.manifest,
            registry=self.registry,
            baseline_configuration_id=self.baseline_configuration_id,
        )
        self.registry.configuration(self.baseline_configuration_id)
        if type(self.synthetic_return_inputs) is not tuple or any(
            type(item) is not StudyReturnInputV1 for item in self.synthetic_return_inputs
        ):
            raise ValueError("study fixture synthetic return inputs are invalid")
        if type(self.round_two_outcomes) is not tuple or any(
            type(item) is not StudyRoundOutcomeV1 for item in self.round_two_outcomes
        ):
            raise ValueError("study fixture round outcomes are invalid")
        expected_ids = tuple(item.configuration_id for item in self.registry.configurations)
        if tuple(item.configuration_id for item in self.synthetic_return_inputs) != expected_ids:
            raise ValueError("study fixture round-one portfolio inputs differ from the registry")
        if tuple(item.configuration_id for item in self.round_two_outcomes) != expected_ids:
            raise ValueError("study fixture round-two portfolio inputs differ from the registry")
        for configuration, round_one, round_two in zip(
            self.registry.configurations,
            self.synthetic_return_inputs,
            self.round_two_outcomes,
            strict=True,
        ):
            expected_round_one, expected_round_two = configuration.portfolio_evaluator_inputs
            if (
                StudyReturnInputV1.from_registry_input(expected_round_one) != round_one
                or StudyRoundOutcomeV1.from_registry_input(expected_round_two) != round_two
            ):
                raise ValueError("study fixture portfolio inputs differ from registry authority")

    @property
    def manifest_ref(self) -> ArtifactRefV5:
        return self.manifest.manifest_ref

    @property
    def authenticated_manifest(self) -> AuthenticatedCampaignManifestV5:
        return self.manifest

    @property
    def baseline(self) -> str:
        return self.baseline_configuration_id

    @property
    def evaluator_inputs(self) -> tuple[StudyReturnInputV1, ...]:
        return self.synthetic_return_inputs

def study_resource_budget_v1() -> MechanismResourceBudgetV1:
    """Return the declared synthetic mechanism bounds frozen by the study."""

    return MechanismResourceBudgetV1(
        max_cases=8,
        max_repetitions=2,
        timeout_ms=1000,
        cpu_seconds=Decimal("1"),
        memory_mib=128,
        output_bytes=65536,
    )


def _study_search_capabilities() -> SearchCapabilitiesV5:
    return SearchCapabilitiesV5(
        hypotheses_per_investigator=1,
        max_tunable_axes=1,
        max_variants_per_template=2,
        max_discovery_survivors_per_template=2,
        archive_capacity=1,
        max_feedback_rounds=2,
        allow_full_source_escape=False,
        investigator_memory_max_bytes=3072,
    )


def _study_resource_capabilities() -> ResourceCapabilitiesV5:
    return ResourceCapabilitiesV5(
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


def _validate_fixture_authority(
    *,
    manifest: AuthenticatedCampaignManifestV5,
    registry: FrozenBehaviorRegistryV1,
    baseline_configuration_id: str,
) -> None:
    if type(manifest) is not AuthenticatedCampaignManifestV5:
        raise ValueError("study fixture manifest is invalid")
    if baseline_configuration_id != "P0":
        raise ValueError("study fixture baseline must be P0")
    baseline = registry.configuration(baseline_configuration_id)
    campaign_manifest = manifest.manifest
    baseline_authority = manifest.baseline_authority
    if (
        campaign_manifest.search != _study_search_capabilities()
        or campaign_manifest.resources != _study_resource_capabilities()
        or campaign_manifest.provider is not None
        or campaign_manifest.pit_data_scope != "production"
        or campaign_manifest.semantic_mode != "required"
        or baseline_authority.policy_revision != baseline.policy_revision
        or baseline_authority.source_bundle != baseline.source_bundle
        or baseline_authority.semantic_fingerprint != baseline.fixed_suite
        or baseline_authority.pit_data_scope != "production"
        or baseline_authority.semantic_mode != "required"
    ):
        raise ValueError("study fixture manifest differs from the frozen authority graph")


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


def _return_inputs(registry: FrozenBehaviorRegistryV1) -> tuple[StudyReturnInputV1, ...]:
    return tuple(
        StudyReturnInputV1.from_registry_input(configuration.portfolio_evaluator_inputs[0])
        for configuration in registry.configurations
    )


def _round_two_inputs(registry: FrozenBehaviorRegistryV1) -> tuple[StudyRoundOutcomeV1, ...]:
    return tuple(
        StudyRoundOutcomeV1.from_registry_input(configuration.portfolio_evaluator_inputs[1])
        for configuration in registry.configurations
    )


def _validate_new_root(root: Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ValueError("study fixture root must be absolute")
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_dir() or any(candidate.iterdir()):
            raise ValueError("study fixture root must be a fresh empty directory")
    else:
        candidate.mkdir(parents=True)
    return Path(candidate.resolve(strict=True))


def _build_manifest(
    *,
    repository: LocalArtifactRepositoryV5,
    registry: FrozenBehaviorRegistryV1,
    search: SearchCapabilitiesV5 | None = None,
    resources: ResourceCapabilitiesV5 | None = None,
) -> AuthenticatedCampaignManifestV5:
    baseline_configuration = registry.configuration("P0")
    selected_search = _study_search_capabilities() if search is None else search
    selected_resources = _study_resource_capabilities() if resources is None else resources
    pit_ref = repository.append_binary_state(
        namespace=_FIXTURE_NAMESPACE,
        key="pit-bundle",
        content=b"v5 study synthetic PIT bundle",
    )
    prices_ref = repository.append_binary_state(
        namespace=_FIXTURE_NAMESPACE,
        key="prices",
        content=b"v5 study synthetic price provenance",
    )
    transition_ref = repository.append_binary_state(
        namespace=_FIXTURE_NAMESPACE,
        key="transition",
        content=b"v5 study synthetic identity transition",
    )
    lineage = PanelSecurityLineage("study-fixture-lineage", ("SPY",), ("sp500",))
    panel_specs = {
        "quick": EvaluationPanelSpec.from_lineages(
            purpose="quick", sessions=("2025-01-01", "2026-01-01"), lineages=(lineage,)
        ),
        "discovery-1": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2026-02-01", "2027-02-01"), lineages=(lineage,)
        ),
        "discovery-2": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2027-03-01", "2028-02-29"), lineages=(lineage,)
        ),
        "discovery-3": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2029-04-01", "2030-04-01"), lineages=(lineage,)
        ),
        "discovery-4": EvaluationPanelSpec.from_lineages(
            purpose="discovery", sessions=("2030-05-01", "2031-05-01"), lineages=(lineage,)
        ),
    }
    panel_refs = {
        name: repository.create_evaluation_panel_spec(f"panels/study-{name}.json", panel)
        for name, panel in panel_specs.items()
    }

    def episode(name: str, panel_name: str, ordinal: int | None = None) -> EpisodePlanV5:
        panel = panel_specs[panel_name]
        return EpisodePlanV5(
            episode_id=f"study-{name}",
            episode_ordinal=ordinal,
            purpose=panel.purpose,
            start_date=panel.start_date,
            end_date=panel.end_date,
            lineage_ids=tuple(item.security_lineage_id for item in panel.lineages),
            panel_ref=panel_refs[panel_name],
        )

    target = AnnualizedReturnTargetV5(Decimal("10.00"))
    panel_plan = CampaignPanelPlanV5(
        schema_version=5,
        pit_bundle_ref=pit_ref,
        prices_provenance_ref=prices_ref,
        partition_seed_sha256="1" * 64,
        target_sha256=target.sha256,
        mechanics=episode("mechanics", "quick"),
        quick=episode("quick", "quick"),
        discovery=tuple(
            episode(f"discovery-{index}", f"discovery-{index}", index) for index in range(1, 5)
        ),
        confirmation_plan_sha256="2" * 64,
        qualification_plan_sha256="3" * 64,
    )
    panel_plan_ref = repository.create_typed_artifact("panels/study-plan.json", panel_plan)

    source_ref = repository.create_typed_artifact("evaluator/study-source.json", baseline_configuration.source_bundle)
    revision_ref = repository.create_typed_artifact("evaluator/study-revision.json", baseline_configuration.policy_revision)
    execution = ExecutionProfileV5(
        5,
        "next_open",
        "open_then_stop",
        "last_session_close",
        "half_spread_plus_market_impact_plus_commission_bps",
    )
    execution_ref = repository.create_typed_artifact("evaluator/study-execution.json", execution)
    evaluator_source_ref = repository._create_only(
        "evaluator/study-runtime-source.json", {"fixture": "v5-two-round-study"}
    )
    sandbox = SandboxProfileV5(
        schema_version=5,
        image_name="v5-two-round-study",
        image_digest="sha256:" + "4" * 64,
        runtime_source_sha256=evaluator_source_ref.sha256,
        network_mode="none",
        root_filesystem="read_only",
        source_mount_mode="read_only",
        data_mount_mode="read_only",
        output_mode="bounded_write_only",
        cpu_limit=selected_resources.evaluation_cpu_limit,
        memory_limit_mib=selected_resources.evaluation_memory_mib,
        output_limit_bytes=selected_resources.evaluation_output_limit_bytes,
        pid_limit=selected_resources.evaluation_pid_limit,
    )
    sandbox_ref = repository.create_typed_artifact("evaluator/study-sandbox.json", sandbox)
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
    evaluator_ref = repository.create_typed_artifact("evaluator/study-evaluator.json", evaluator)

    portfolio_input = baseline_configuration.portfolio_evaluator_inputs[0]
    report = _zero_report()
    scenario_endpoints = {
        "base": (portfolio_input.ending_equity, portfolio_input.base_annualized_return_pct),
        "gross": (portfolio_input.gross_ending_equity, portfolio_input.gross_annualized_return_pct),
        "stress": (portfolio_input.stress_ending_equity, portfolio_input.stress_annualized_return_pct),
    }
    episodes: list[EpisodeEvaluationV5] = []
    for index in range(1, 5):
        panel = panel_specs[f"discovery-{index}"]
        scenarios = tuple(
            ScenarioPanelEvaluationV5(
                scenario.scenario_id,
                portfolio_input.starting_equity,
                scenario_endpoints[scenario.scenario_id][0],
                replace(
                    report,
                    portfolio_total_return_pct=(
                        (scenario_endpoints[scenario.scenario_id][0] / portfolio_input.starting_equity)
                        - Decimal("1")
                    )
                    * Decimal("100"),
                    portfolio_annualized_return_pct=scenario_endpoints[scenario.scenario_id][1],
                    gross_annualized_return_pct=portfolio_input.gross_annualized_return_pct,
                ),
            )
            for scenario in initial_friction_grid_v5()
        )
        evaluation = PanelEvaluationV5(
            evaluator_contract_sha256=evaluator.sha256,
            sandbox_profile_sha256=sandbox.sha256,
            panel_sha256=panel.sha256,
            policy_identity_sha256=baseline_configuration.policy_revision.sha256,
            start_date=panel.start_date,
            end_date=panel.end_date,
            elapsed_calendar_days=(date.fromisoformat(panel.end_date) - date.fromisoformat(panel.start_date)).days,
            selection_scenario_id="base",
            scenarios=scenarios,
        )
        episodes.append(
            EpisodeEvaluationV5(
                episode_id=f"study-discovery-{index}",
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
        policy_revision=baseline_configuration.policy_revision,
        policy_revision_ref=revision_ref,
        semantic_fingerprint=baseline_configuration.fixed_suite,
        campaign=campaign,
        source_bundle=baseline_configuration.source_bundle,
        source_bundle_ref=source_ref,
        pit_data_scope="production",
        semantic_mode="required",
    )
    baseline_ref = repository.create_typed_artifact("evaluator/study-baseline.json", baseline)
    return build_campaign_manifest_v5(
        repository=repository,
        campaign_id="study-fixture-campaign",
        target=target,
        source_snapshot=PolicySourceSnapshotV5(
            source_commit=_SOURCE_COMMIT,
            editable_source_sha256=baseline_configuration.policy_revision.editable_source_sha256,
        ),
        execution_profile_ref=execution_ref,
        evaluator_contract_ref=evaluator_ref,
        baseline_authority_ref=baseline_ref,
        panel_plan_ref=panel_plan_ref,
        sandbox_profile_ref=sandbox_ref,
        policy_scope_path="evaluator/study-policy-scope.json",
        manifest_path="evaluator/study-manifest.json",
        search=selected_search,
        provider=None,
        resources=selected_resources,
        pit_data_scope="production",
        semantic_mode="required",
    )


def create_study_fixture_v1(
    *,
    root: Path,
    registry: FrozenBehaviorRegistryV1 | None = None,
) -> StudyFixtureV1:
    """Create one fresh synthetic authority graph under an empty root."""

    selected_registry = build_study_registry_v1() if registry is None else registry
    if type(selected_registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("study fixture registry is invalid")
    from .registry import verify_registry_v1

    verify_registry_v1(selected_registry)
    resolved_root = _validate_new_root(root)
    repository = LocalArtifactRepositoryV5(resolved_root)
    manifest = _build_manifest(repository=repository, registry=selected_registry)
    return StudyFixtureV1(
        repository=repository,
        manifest=manifest,
        registry=selected_registry,
        baseline_configuration_id="P0",
        resource_budget=study_resource_budget_v1(),
        synthetic_return_inputs=_return_inputs(selected_registry),
        round_two_outcomes=_round_two_inputs(selected_registry),
    )


def reopen_study_fixture_v1(
    *,
    root: Path,
    manifest_ref: ArtifactRefV5,
    registry: FrozenBehaviorRegistryV1,
) -> StudyFixtureV1:
    """Authenticate an existing graph without creating or repairing artifacts."""

    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("study fixture registry is invalid")
    from .registry import verify_registry_v1

    verify_registry_v1(registry)
    resolved_root = Path(root)
    if not resolved_root.is_absolute() or resolved_root.is_symlink() or not resolved_root.is_dir():
        raise ValueError("study fixture root is invalid")
    repository = LocalArtifactRepositoryV5(resolved_root.resolve(strict=True))
    manifest = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=manifest_ref)
    _validate_fixture_authority(
        manifest=manifest,
        registry=registry,
        baseline_configuration_id="P0",
    )
    return StudyFixtureV1(
        repository=repository,
        manifest=manifest,
        registry=registry,
        baseline_configuration_id="P0",
        resource_budget=study_resource_budget_v1(),
        synthetic_return_inputs=_return_inputs(registry),
        round_two_outcomes=_round_two_inputs(registry),
    )


__all__ = [
    "StudyFixtureV1",
    "StudyReturnInputV1",
    "StudyRoundOutcomeV1",
    "create_study_fixture_v1",
    "reopen_study_fixture_v1",
    "study_resource_budget_v1",
]
