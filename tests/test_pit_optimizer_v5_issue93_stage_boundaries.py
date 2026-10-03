"""Synthetic public-API evidence for Research-14 / issue #93 stage boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5 import confirmation as confirmation_module
from core.pit_optimizer_v5 import qualification as qualification_module
from core.pit_optimizer_v5 import readiness as readiness_module
from core.pit_optimizer_v5.artifacts import (
    ArtifactRefV5,
    LocalArtifactRepositoryV5,
    RepositoryCheckpointV5,
)
from core.pit_optimizer_v5.candidate_ir import (
    SourceBundleV5,
    SourceFileV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.confirmation import (
    ConfirmationAdapterConfigV5,
    ConfirmationCleanupV5,
    ConfirmationLedgerV5,
    build_confirmation_attempt,
    run_confirmation,
)
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    CampaignEvidenceV5,
    CampaignPanelPlanV5,
    ConfirmationAttemptCommitmentV5,
    ConfirmationOutcomeV5,
    ConfirmationPanelPlanV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    EvaluationReportV5,
    FinalizedDiscoveryCampaignV5,
    FinalizedDiscoveryRoundV5,
    PanelEvaluationV5,
    QualificationAttemptCommitmentV5,
    QualificationPanelPlanV5,
    QualificationOutcomeV5,
    RetirementLedgerLocatorV5,
    ResourceCapabilitiesV5,
    ScenarioGridV5,
    ScenarioPanelEvaluationV5,
    SandboxProfileV5,
    SearchCapabilitiesV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.fixture_runtime import run_fixture_campaign_v5, verify_fixture_run_v5
from core.pit_optimizer_v5.manifest import (
    PolicySourceSnapshotV5,
    build_campaign_manifest_v5,
)
from core.pit_optimizer_v5.panels import (
    CANONICAL_STAGE_LEDGER_PATHS_V5,
    StageRetirementSnapshotV5,
    initialize_stage_ledgers_v5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    ProbeObservationV5,
    SemanticFingerprintV5,
    policy_probe_suite_v1,
)
from core.pit_optimizer_v5.qualification import (
    QualificationCleanupV5,
    build_qualification_attempt,
    qualification_cleanup_evidence_v5,
    run_qualification,
)
from core.pit_optimizer_v5.readiness import (
    FullReplayReadinessV5,
    ReadinessGitAuthorityV5,
    full_replay_readiness,
)
from core.pit_optimizer_v5.search import (
    BaselineParentAuthorityV5,
    annualized_return_pct,
    campaign_cagr_pct,
)
from core.strategy_policy.contracts import (
    AllocationDecision,
    CapacityDecision,
    EntryDecision,
    EvictionDecision,
    ExitDecision,
)
from core.strategy_policy.contracts_v3 import AddOnDecisionV3


@dataclass(frozen=True)
class _StageFixture:
    repository: LocalArtifactRepositoryV5
    manifest_ref: ArtifactRefV5
    discovery_plan: CampaignPanelPlanV5
    discovery_checkpoint_ref: ArtifactRefV5
    discovery_archive_ref: ArtifactRefV5
    finalized_campaign_ref: ArtifactRefV5
    confirmation_plan_ref: ArtifactRefV5
    confirmation_panel_ref: ArtifactRefV5
    qualification_plan_ref: ArtifactRefV5
    scenario_grid_ref: ArtifactRefV5
    confirmation_ledger: RetirementLedgerLocatorV5
    qualification_ledger: RetirementLedgerLocatorV5
    adapter_ref: ArtifactRefV5
    baseline_policy_ref: ArtifactRefV5
    git_authority: ReadinessGitAuthorityV5
    source_commit: str
    source_root: Path
    pit_bundle_ref: ArtifactRefV5
    prices_ref: ArtifactRefV5


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


def _baseline_fingerprint() -> SemanticFingerprintV5:
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
            base = case.snapshot.base
            decision = ExitDecision(
                actions=(),
                next_stop_price=None,
                early_winner_hold=base.early_winner_hold,
                scale_out_tier=base.scale_out_tier,
                breakeven_armed=base.breakeven_armed,
                ema_trailing_active=base.ema_trailing_active,
            )
        observations.append(
            ProbeObservationV5(
                case.probe_id,
                case.method,
                case.input_sha256,
                decision.to_canonical_json().encode("utf-8"),
            )
        )
    frozen = tuple(observations)
    return SemanticFingerprintV5(
        PROBE_SUITE_ID_V5,
        frozen,
        canonical_sha256_v5(
            {"suite_id": PROBE_SUITE_ID_V5, "observations": tuple(item.to_primitive() for item in frozen)}
        ),
    )


def _source_bundle() -> SourceBundleV5:
    root = Path(__file__).resolve().parents[1]
    files = []
    for path in EDITABLE_POLICY_PATHS_V5:
        source = (root / path).read_text(encoding="utf-8")
        if path == EDITABLE_POLICY_PATHS_V5[0]:
            source = source.replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n\nFIXTURE_ENTRY_THRESHOLD = 0\n",
                1,
            )
        files.append(SourceFileV5(path, source))
    return SourceBundleV5(tuple(files))


def _commit_source(source_root: Path, bundle: SourceBundleV5, git: str) -> str:
    source_root.mkdir(parents=True)
    for item in bundle.files:
        path = source_root.joinpath(*Path(item.path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.source.encode("utf-8"))

    def git_run(*args: str) -> str:
        result = subprocess.run(
            (git, *args),
            cwd=source_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    git_run("init", "--quiet")
    git_run("config", "user.name", "Issue 93 Synthetic Fixture")
    git_run("config", "user.email", "issue93-fixture@example.invalid")
    git_run("config", "core.autocrlf", "false")
    git_run("add", "--", *EDITABLE_POLICY_PATHS_V5)
    git_run("commit", "--quiet", "-m", "synthetic frozen policy fixture")
    return git_run("rev-parse", "HEAD")


def _panel_episode(
    repository: LocalArtifactRepositoryV5,
    *,
    name: str,
    purpose: str,
    ordinal: int | None,
    start: str,
    end: str,
    affiliations: tuple[str, ...],
) -> EpisodePlanV5:
    ticker = "S" + str(int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:5], 16)).zfill(5)
    lineage = PanelSecurityLineage(name + "-lineage", (ticker,), affiliations)
    panel = EvaluationPanelSpec.from_lineages(
        purpose=purpose,
        sessions=(start, end),
        lineages=(lineage,),
    )
    reference = repository.create_evaluation_panel_spec(f"panels/specs/{name}.json", panel)
    return EpisodePlanV5(
        episode_id=name,
        episode_ordinal=ordinal,
        purpose=purpose,
        start_date=start,
        end_date=end,
        lineage_ids=(lineage.security_lineage_id,),
        panel_ref=reference,
    )


def _stage_fixture(tmp_path: Path) -> _StageFixture:
    artifact_root = tmp_path / "r"
    source_root = tmp_path / "s"
    work_roots = tuple(str((tmp_path / name).resolve()) for name in ("workspace", "data", "output", "control"))
    artifact_root.mkdir(parents=True)

    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Git is required for the source-bound synthetic readiness fixture")
    git_command = Path(git).resolve(strict=True)
    candidates = (
        git_command.parent.parent / "bin" / "git.exe",
        git_command,
    )
    git_path = next(
        str(candidate.resolve(strict=True))
        for candidate in candidates
        if candidate.is_file() and os.stat(candidate).st_nlink == 1
    )
    bundle = _source_bundle()
    source_commit = _commit_source(source_root.resolve(), bundle, git_path)
    git_sha256 = hashlib.sha256(Path(git_path).read_bytes()).hexdigest()
    git_authority = ReadinessGitAuthorityV5(git_path, git_sha256)

    repository = LocalArtifactRepositoryV5(artifact_root)
    pit_ref = repository.append_binary_state(
        namespace="issue93-synthetic", key="pit-bundle", content=b"synthetic PIT bytes; no market rows"
    )
    prices_ref = repository.append_binary_state(
        namespace="issue93-synthetic", key="prices", content=b"synthetic price provenance bytes"
    )
    transition_ref = repository.append_binary_state(
        namespace="issue93-synthetic", key="transition", content=b"synthetic identity transition bytes"
    )
    snapshots = initialize_stage_ledgers_v5(
        repository=repository,
        pit_bundle_ref=pit_ref,
        prices_provenance_ref=prices_ref,
        confirmation_ledger_path=CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"],
        qualification_ledger_path=CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"],
    )
    stage_snapshots = {}
    for stage in ("confirmation", "qualification"):
        metadata = snapshots[stage]
        snapshot_ref = ArtifactRefV5(
            f"panels/retirement-snapshots/{stage}-{metadata['retirement_domain_id']}.json",
            metadata["snapshot_sha256"],
        )
        stage_snapshots[stage] = (
            snapshot_ref,
            repository.load_typed_artifact(snapshot_ref, value_type=StageRetirementSnapshotV5),
        )

    target = AnnualizedReturnTargetV5(Decimal("10.00"))
    seed = "issue-93-synthetic-partition-v1"
    partition_seed_sha256 = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    mechanics = _panel_episode(
        repository, name="mechanics", purpose="quick", ordinal=None,
        start="2020-01-01", end="2020-12-31", affiliations=("sp500",),
    )
    quick = _panel_episode(
        repository, name="quick", purpose="quick", ordinal=None,
        start="2020-01-01", end="2020-12-31", affiliations=("sp500",),
    )
    discovery = tuple(
        _panel_episode(
            repository,
            name=f"discovery-{index}",
            purpose="discovery",
            ordinal=index,
            start=f"{2020 + index}-01-01",
            end=f"{2020 + index}-12-31",
            affiliations=("sp500",),
        )
        for index in range(1, 5)
    )
    confirmation_episode = _panel_episode(
        repository, name="confirmation", purpose="qualification", ordinal=None,
        start="2025-01-01", end="2025-12-31", affiliations=("sp500", "nasdaq100"),
    )
    qualification_episode = _panel_episode(
        repository, name="qualification", purpose="qualification", ordinal=None,
        start="2026-01-01", end="2026-12-31",
        affiliations=("sp500", "nasdaq100", "russell2000"),
    )
    confirmation_plan = ConfirmationPanelPlanV5(
        5,
        pit_ref.sha256,
        partition_seed_sha256,
        target.sha256,
        stage_snapshots["confirmation"][1].retirement_domain_id,
        stage_snapshots["confirmation"][1].ledger_head_sha256,
        confirmation_episode,
    )
    qualification_plan = QualificationPanelPlanV5(
        5,
        pit_ref.sha256,
        partition_seed_sha256,
        target,
        stage_snapshots["qualification"][1].retirement_domain_id,
        stage_snapshots["qualification"][1].ledger_head_sha256,
        qualification_episode,
    )
    confirmation_plan_ref = repository.create_typed_artifact("panels/confirmation-owner.json", confirmation_plan)
    qualification_plan_ref = repository.create_typed_artifact("panels/qualification-owner.json", qualification_plan)
    discovery_plan = CampaignPanelPlanV5(
        5,
        pit_ref,
        prices_ref,
        partition_seed_sha256,
        target.sha256,
        mechanics,
        quick,
        discovery,
        confirmation_plan.sha256,
        qualification_plan.sha256,
    )
    discovery_plan_ref = repository.create_typed_artifact("panels/discovery-owner.json", discovery_plan)

    baseline_source_ref = repository.create_typed_artifact("baseline/source.json", bundle)
    baseline_policy = derive_policy_revision_identity_v5(
        source_bundle=bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    baseline_policy_ref = repository.create_typed_artifact("baseline/policy.json", baseline_policy)
    execution = ExecutionProfileV5(
        5,
        "next_open",
        "open_then_stop",
        "last_session_close",
        "half_spread_plus_market_impact_plus_commission_bps",
    )
    execution_ref = repository.create_typed_artifact("evaluator/execution.json", execution)
    runtime_ref = repository.append_binary_state(
        namespace="issue93-synthetic", key="runtime-source", content=b"synthetic runtime identity bytes"
    )
    resources = ResourceCapabilitiesV5(
        max_parallel_evaluations=1,
        evaluation_memory_mib=128,
        evaluation_output_limit_bytes=65536,
        mechanics_timeout_seconds=60,
        quick_timeout_seconds=60,
        discovery_episode_timeout_seconds=60,
        round_wall_timeout_seconds=120,
        campaign_wall_timeout_seconds=240,
    )
    sandbox = SandboxProfileV5(
        5,
        "issue-93-synthetic",
        "sha256:" + "4" * 64,
        runtime_ref.sha256,
        "none",
        "read_only",
        "read_only",
        "read_only",
        "bounded_write_only",
        resources.evaluation_cpu_limit,
        resources.evaluation_memory_mib,
        resources.evaluation_output_limit_bytes,
        resources.evaluation_pid_limit,
    )
    sandbox_ref = repository.create_typed_artifact("evaluator/sandbox.json", sandbox)
    evaluator = EvaluatorContractV5(
        5,
        execution.sha256,
        sandbox.sha256,
        runtime_ref.sha256,
        pit_ref.sha256,
        prices_ref.sha256,
        transition_ref.sha256,
        bundle.sha256,
        baseline_policy.sha256,
        initial_friction_grid_v5(),
        "base",
    )
    evaluator_ref = repository.create_typed_artifact("evaluator/contract.json", evaluator)
    report = _zero_report()
    baseline_episodes = []
    for episode in discovery:
        days = (date.fromisoformat(episode.end_date) - date.fromisoformat(episode.start_date)).days
        evaluation = PanelEvaluationV5(
            evaluator.sha256,
            sandbox.sha256,
            episode.panel_ref.sha256,
            baseline_policy.sha256,
            episode.start_date,
            episode.end_date,
            days,
            "base",
            tuple(
                ScenarioPanelEvaluationV5(item.scenario_id, Decimal("100"), Decimal("100"), report)
                for item in initial_friction_grid_v5()
            ),
        )
        baseline_episodes.append(EpisodeEvaluationV5(episode.episode_id, episode.episode_ordinal, episode.start_date, episode.end_date, evaluation))
    baseline_campaign = CampaignEvidenceV5(
        discovery_plan.discovery_plan_sha256,
        tuple(baseline_episodes),
        campaign_cagr_pct(
            episodes=tuple(baseline_episodes),
            discovery_plan=discovery_plan,
            evaluator_contract=evaluator,
        ),
        0,
    )
    baseline = BaselineParentAuthorityV5(
        baseline_policy,
        baseline_policy_ref,
        _baseline_fingerprint(),
        baseline_campaign,
        bundle,
        baseline_source_ref,
    )
    baseline_ref = repository.create_typed_artifact("baseline/authority.json", baseline)
    search = SearchCapabilitiesV5(
        hypotheses_per_investigator=3,
        max_tunable_axes=1,
        max_variants_per_template=3,
        max_discovery_survivors_per_template=3,
        archive_capacity=2,
        max_feedback_rounds=2,
        allow_full_source_escape=False,
        investigator_memory_max_bytes=96 * 1024,
    )
    authorities = build_campaign_manifest_v5(
        repository=repository,
        campaign_id="issue93-synthetic-campaign",
        target=target,
        source_snapshot=PolicySourceSnapshotV5.from_tracked_bytes(
            source_commit=source_commit,
            source_by_path={item.path: item.source.encode("utf-8") for item in bundle.files},
        ),
        execution_profile_ref=execution_ref,
        evaluator_contract_ref=evaluator_ref,
        baseline_authority_ref=baseline_ref,
        panel_plan_ref=discovery_plan_ref,
        sandbox_profile_ref=sandbox_ref,
        policy_scope_path="campaign/policy-scope.json",
        manifest_path="campaign/manifest.json",
        search=search,
        resources=resources,
        provider=None,
        pit_data_scope="production",
        semantic_mode="required",
    )

    results = run_fixture_campaign_v5(repository=repository, authorities=authorities)
    if len(results) != 2 or any(item.status != "completed" or item.cleanup is None or not item.cleanup.cleanup_complete for item in results):
        raise AssertionError(f"synthetic public discovery fixture did not complete: {results!r}")
    assert verify_fixture_run_v5(repository=repository, manifest=authorities.manifest)
    checkpoint = repository.load_checkpoint()
    assert type(checkpoint) is RepositoryCheckpointV5 and checkpoint.generation == 2
    checkpoint_raw = canonical_json_bytes_v5(checkpoint.to_primitive())
    checkpoint_ref = ArtifactRefV5("checkpoint.json", hashlib.sha256(checkpoint_raw).hexdigest())
    archive_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    repository.authenticate(checkpoint_ref)
    repository.authenticate(archive_ref)
    finalized_rounds = []
    for round_index in (1, 2):
        events = repository.load_round_events(campaign_id=authorities.manifest.campaign_id, round_index=round_index)
        event_refs = tuple(
            ArtifactRefV5(
                f"events/{authorities.manifest.campaign_id}/{round_index:04d}/{event.sequence:06d}.json",
                event.sha256,
            )
            for event in events
        )
        records = tuple(
            sorted(
                (
                    reference
                    for reference in checkpoint.record_refs
                    if repository.load_experiment(reference).round_index == round_index
                ),
                key=lambda reference: (reference.relative_path, reference.sha256),
            )
        )
        finalized_rounds.append(FinalizedDiscoveryRoundV5(round_index, event_refs, records))
    finalized = FinalizedDiscoveryCampaignV5(
        5,
        authorities.manifest_ref,
        checkpoint_ref,
        archive_ref,
        tuple(finalized_rounds),
        "completed",
    )
    finalized_ref = repository.create_typed_artifact("discovery/finalized-campaign.json", finalized)
    repository.load_confirmation_champion(
        checkpoint_ref=checkpoint_ref,
        archive_ref=archive_ref,
        finalized_campaign_ref=finalized_ref,
        discovery_manifest_ref=authorities.manifest_ref,
    )
    scenario_grid_ref = repository.create_typed_artifact(
        "evaluator/scenario-grid.json", ScenarioGridV5(5, initial_friction_grid_v5())
    )
    adapter = ConfirmationAdapterConfigV5(
        5,
        authorities.manifest_ref,
        repository.root_identity_sha256,
        str(source_root.resolve()),
        work_roots[0],
        work_roots[1],
        work_roots[2],
        work_roots[3],
        git_path,
        r"C:\offline-tools\docker.exe",
        "5" * 64,
    )
    adapter_ref = repository.create_typed_artifact("stages/execution-adapter.json", adapter)
    return _StageFixture(
        repository,
        authorities.manifest_ref,
        discovery_plan,
        checkpoint_ref,
        archive_ref,
        finalized_ref,
        confirmation_plan_ref,
        confirmation_episode.panel_ref,
        qualification_plan_ref,
        scenario_grid_ref,
        RetirementLedgerLocatorV5(
            CANONICAL_STAGE_LEDGER_PATHS_V5["confirmation"], stage_snapshots["confirmation"][0]
        ),
        RetirementLedgerLocatorV5(
            CANONICAL_STAGE_LEDGER_PATHS_V5["qualification"], stage_snapshots["qualification"][0]
        ),
        adapter_ref,
        baseline_policy_ref,
        git_authority,
        source_commit,
        source_root.resolve(),
        pit_ref,
        prices_ref,
    )


@pytest.fixture(scope="module")
def stage_fixture() -> _StageFixture:
    root = Path(__file__).resolve().parents[1] / ".artifacts" / "pytest" / "tmp_test_roots"
    root.mkdir(parents=True, exist_ok=True)
    base = root / f"i93-{uuid4().hex[:8]}"
    base.mkdir()
    return _stage_fixture(base)


def _fork_stage_fixture(template: _StageFixture, case_name: str, root: Path) -> _StageFixture:
    evidence_root_value = {
        "positive": os.environ.get("ISSUE93_EVIDENCE_ROOT"),
        "heldout-decode-interruption": os.environ.get("ISSUE93_DECODE_EVIDENCE_ROOT"),
    }.get(case_name)
    persistent = evidence_root_value is not None
    if persistent:
        root = Path(evidence_root_value).resolve()
    artifact_root = root / "r"
    source_root = root / "s"
    if persistent and root.exists() and any(root.iterdir()):
        raise ValueError(f"issue #93 retained evidence path must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template.repository.root, artifact_root)
    shutil.copytree(template.source_root, source_root)
    repository = LocalArtifactRepositoryV5(artifact_root)
    original = repository.load_typed_artifact(template.adapter_ref, value_type=ConfirmationAdapterConfigV5)
    work_roots = tuple(str((root / name).resolve()) for name in ("workspace", "data", "output", "control"))
    adapter = replace(
        original,
        repository_root_identity_sha256=repository.root_identity_sha256,
        source_root=str(source_root.resolve()),
        workspace_root=work_roots[0],
        data_root=work_roots[1],
        output_root=work_roots[2],
        control_root=work_roots[3],
    )
    adapter_ref = repository.create_typed_artifact(f"stages/adapters/{case_name}.json", adapter)
    return replace(template, repository=repository, adapter_ref=adapter_ref, source_root=source_root.resolve())


def _confirmation_attempt(fixture: _StageFixture) -> ArtifactRefV5:
    return build_confirmation_attempt(
        repository=fixture.repository,
        discovery_manifest_ref=fixture.manifest_ref,
        discovery_checkpoint_ref=fixture.discovery_checkpoint_ref,
        discovery_archive_ref=fixture.discovery_archive_ref,
        finalized_campaign_ref=fixture.finalized_campaign_ref,
        confirmation_plan_ref=fixture.confirmation_plan_ref,
        scenario_grid_ref=fixture.scenario_grid_ref,
        retirement_ledger=fixture.confirmation_ledger,
        execution_adapter_ref=fixture.adapter_ref,
        attempt_id="issue93-confirmation-attempt",
        output_path="stages/confirmation-attempt.json",
    )


def _qualification_attempt(fixture: _StageFixture, confirmation_outcome_ref: ArtifactRefV5) -> ArtifactRefV5:
    return build_qualification_attempt(
        repository=fixture.repository,
        confirmation_outcome_ref=confirmation_outcome_ref,
        qualification_plan_ref=fixture.qualification_plan_ref,
        retirement_ledger=fixture.qualification_ledger,
        attempt_id="issue93-qualification-attempt",
        output_path="stages/qualification-attempt.json",
        operator_approved=True,
    )


class _SyntheticStageWorker:
    def __init__(self, repository, inputs, *, tamper_identity: bool = False, inactive: bool = False):
        self.repository = repository
        self.inputs = inputs
        self.tamper_identity = tamper_identity
        self.inactive = inactive
        self.evaluations = []
        self.closed = []

    def evaluate(self, inputs, plan, *, baseline: bool) -> PanelEvaluationV5:
        assert inputs is self.inputs
        self.evaluations.append(("baseline" if baseline else "candidate", plan.episode.panel_ref.sha256))
        start = Decimal("100")
        end = Decimal("100") if baseline else Decimal("140")
        days = (date.fromisoformat(plan.episode.end_date) - date.fromisoformat(plan.episode.start_date)).days
        cagr = annualized_return_pct(starting_equity=start, ending_equity=end, days=days)
        report = replace(
            _zero_report(),
            portfolio_annualized_return_pct=cagr,
            portfolio_total_return_pct=(end / start - Decimal("1")) * Decimal("100"),
            closed_trades=0 if self.inactive and not baseline else (1 if baseline else 6),
        )
        result = PanelEvaluationV5(
            inputs.evaluator.sha256,
            inputs.sandbox.sha256,
            plan.episode.panel_ref.sha256,
            (inputs.baseline_policy if baseline else inputs.candidate_policy).sha256,
            plan.episode.start_date,
            plan.episode.end_date,
            days,
            inputs.evaluator.selection_scenario_id,
            tuple(
                ScenarioPanelEvaluationV5(item.scenario_id, start, end, report)
                for item in inputs.grid.scenarios
            ),
        )
        if self.tamper_identity:
            result = replace(
                result,
                evaluator_contract_sha256="a" * 64,
                panel_sha256="b" * 64,
                policy_identity_sha256="c" * 64,
            )
        return result

    def close(self, inputs, *, recovered: bool):
        self.closed.append(recovered)
        return ConfirmationCleanupV5(5, inputs.attempt_ref, True, True, recovered)


class _SyntheticQualificationWorker(_SyntheticStageWorker):
    def __init__(self, repository, inputs, *, incomplete_cleanup: bool = False):
        super().__init__(repository, inputs)
        self.incomplete_cleanup = incomplete_cleanup

    def close(self, inputs, *, recovered: bool):
        self.closed.append(recovered)
        return QualificationCleanupV5(5, inputs.attempt_ref, True, not self.incomplete_cleanup, recovered)


class _RecoveredQualificationCleanup:
    def __init__(self, repository, inputs):
        self.inputs = inputs

    def close(self, inputs, *, recovered: bool):
        assert inputs is self.inputs
        return QualificationCleanupV5(5, inputs.attempt_ref, True, True, True)


def test_public_stage_chain_binds_policy_panels_and_non_executable_readiness(
    stage_fixture: _StageFixture, tmp_path: Path
) -> None:
    fixture = _fork_stage_fixture(stage_fixture, "positive", tmp_path)
    confirmation_attempt_ref = _confirmation_attempt(fixture)
    confirmation_attempt = fixture.repository.load_typed_artifact(
        confirmation_attempt_ref, value_type=ConfirmationAttemptCommitmentV5
    )

    # A changed champion identity must fail before opening the canonical ledger.
    tampered_attempt = replace(
        confirmation_attempt,
        discovery_champion_policy_ref=fixture.baseline_policy_ref,
    )
    tampered_ref = fixture.repository.create_typed_artifact("stages/tampered-policy-attempt.json", tampered_attempt)
    with pytest.raises(ValueError):
        run_confirmation(repository=fixture.repository, attempt_ref=tampered_ref)

    worker_instances = []

    def confirmation_factory(repository, inputs):
        worker = _SyntheticStageWorker(repository, inputs)
        worker_instances.append(worker)
        return worker

    confirmation_outcome_ref = run_confirmation(
        repository=fixture.repository,
        attempt_ref=confirmation_attempt_ref,
        worker_factory=confirmation_factory,
    )
    confirmation_outcome = fixture.repository.load_typed_artifact(
        confirmation_outcome_ref,
        value_type=ConfirmationOutcomeV5,
    )
    assert confirmation_outcome.status == "completed"
    assert confirmation_outcome.eligible_to_request_qualification is True
    assert len(worker_instances[0].evaluations) == 2
    assert worker_instances[0].closed == [False]
    assert fixture.repository.load_typed_artifact(
        confirmation_attempt.confirmation_plan_ref, value_type=ConfirmationPanelPlanV5
    ).episode.end_date < "2026-01-01"
    assert set(
        fixture.repository.load_evaluation_panel_spec(
            fixture.repository.load_typed_artifact(
                confirmation_attempt.confirmation_plan_ref, value_type=ConfirmationPanelPlanV5
            ).episode.panel_ref
        ).lineages[0].executable_tickers
    ).isdisjoint(
        {
            ticker
            for episode in (fixture.discovery_plan.mechanics, fixture.discovery_plan.quick, *fixture.discovery_plan.discovery)
            for lineage in fixture.repository.load_evaluation_panel_spec(episode.panel_ref).lineages
            for ticker in lineage.executable_tickers
        }
    )

    # The discovery finalization is not qualification evidence.
    with pytest.raises((TypeError, ValueError)):
        _qualification_attempt(fixture, fixture.finalized_campaign_ref)
    qualification_attempt_ref = _qualification_attempt(fixture, confirmation_outcome_ref)
    qualification_workers = []

    def qualification_factory(repository, inputs):
        worker = _SyntheticQualificationWorker(repository, inputs)
        qualification_workers.append(worker)
        return worker

    qualification_outcome_ref = run_qualification(
        repository=fixture.repository,
        attempt_ref=qualification_attempt_ref,
        worker_factory=qualification_factory,
    )
    qualification_outcome = fixture.repository.load_typed_artifact(
        qualification_outcome_ref,
        value_type=QualificationOutcomeV5,
    )
    assert qualification_outcome.status == "completed" and qualification_outcome.qualified is True
    assert len(qualification_workers[0].evaluations) == 2
    assert qualification_workers[0].closed == [False]

    prior = fixture.repository.load_typed_artifact(confirmation_attempt_ref, value_type=ConfirmationAttemptCommitmentV5)
    _, confirmation_terminal, _ = readiness_module._retired(
        fixture.repository, confirmation_outcome, prior, "confirmation"
    )
    _, qualification_terminal, _ = readiness_module._retired(
        fixture.repository,
        qualification_outcome,
        fixture.repository.load_typed_artifact(qualification_attempt_ref, value_type=QualificationAttemptCommitmentV5),
        "qualification",
    )
    identity_inputs = readiness_module._identities(
        fixture.repository,
        fixture.repository.load_typed_artifact(qualification_attempt_ref, value_type=QualificationAttemptCommitmentV5),
        prior,
        confirmation_outcome,
    )
    _, prior_plan = readiness_module._owner(
        fixture.repository, prior.confirmation_plan_ref, ConfirmationPanelPlanV5
    )
    _, qualification_plan = readiness_module._owner(
        fixture.repository,
        fixture.repository.load_typed_artifact(qualification_attempt_ref, value_type=QualificationAttemptCommitmentV5).qualification_plan_ref,
        QualificationPanelPlanV5,
    )
    identity_inputs.attempt_ref, identity_inputs.attempt = confirmation_attempt_ref, prior
    assert confirmation_module._result(
        fixture.repository,
        identity_inputs,
        confirmation_outcome.retirement_terminal_ref,
        confirmation_terminal,
        prior_plan,
    ) == confirmation_outcome
    qualification_attempt = fixture.repository.load_typed_artifact(
        qualification_attempt_ref, value_type=QualificationAttemptCommitmentV5
    )
    identity_inputs.attempt_ref, identity_inputs.attempt = qualification_attempt_ref, qualification_attempt
    assert qualification_module._result(
        fixture.repository,
        identity_inputs,
        qualification_outcome.retirement_terminal_ref,
        qualification_terminal,
        qualification_plan,
    ) == qualification_outcome

    readiness_ref = full_replay_readiness(
        repository=fixture.repository,
        qualification_outcome_ref=qualification_outcome_ref,
        output_path="full-replay-readiness.json",
        git_authority=fixture.git_authority,
    )
    readiness = fixture.repository.load_typed_artifact(readiness_ref, value_type=FullReplayReadinessV5)
    assert readiness.source_clean and readiness.cleanup_complete and readiness.permanently_retired
    assert readiness.executable is False and readiness.replay_started is False and readiness.provider_calls == 0
    assert readiness.projection == "local_full_replay_requires_separate_explicit_decision"

    # Rerunning a retired attempt reads the durable outcome and never constructs a worker.
    assert run_qualification(
        repository=fixture.repository,
        attempt_ref=qualification_attempt_ref,
        worker_factory=lambda *_: pytest.fail("retired qualification must not execute again"),
    ) == qualification_outcome_ref
    with pytest.raises(ValueError):
        build_qualification_attempt(
            repository=fixture.repository,
            confirmation_outcome_ref=confirmation_outcome_ref,
            qualification_plan_ref=fixture.qualification_plan_ref,
            retirement_ledger=fixture.qualification_ledger,
            attempt_id="issue93-qualification-reuse",
            output_path="stages/reused-qualification-attempt.json",
            operator_approved=True,
        )

    if os.environ.get("ISSUE93_EVIDENCE_ROOT"):
        evidence_root = Path(os.environ["ISSUE93_EVIDENCE_ROOT"]).resolve()
        summary = {
            "schema_version": 1,
            "evaluation_mode": "offline_fixture",
            "provider_calls": 0,
            "broker_calls": 0,
            "source_revision": subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "source_commit_for_readiness": fixture.source_commit,
            "policy_artifact_sha256": confirmation_outcome.confirmed_policy_ref.sha256,
            "policy_source_bundle_sha256": readiness.candidate_source_sha256,
            "pit_bundle_sha256": fixture.pit_bundle_ref.sha256,
            "prices_provenance_sha256": fixture.prices_ref.sha256,
            "discovery_manifest_sha256": fixture.manifest_ref.sha256,
            "discovery_checkpoint_sha256": fixture.discovery_checkpoint_ref.sha256,
            "discovery_archive_sha256": fixture.discovery_archive_ref.sha256,
            "finalized_discovery_sha256": fixture.finalized_campaign_ref.sha256,
            "confirmation_plan_sha256": fixture.confirmation_plan_ref.sha256,
            "qualification_plan_sha256": fixture.qualification_plan_ref.sha256,
            "confirmation_attempt_sha256": confirmation_attempt_ref.sha256,
            "confirmation_outcome_sha256": confirmation_outcome_ref.sha256,
            "qualification_attempt_sha256": qualification_attempt_ref.sha256,
            "qualification_outcome_sha256": qualification_outcome_ref.sha256,
            "readiness_sha256": readiness_ref.sha256,
            "readiness_flags": {
                "executable": readiness.executable,
                "replay_started": readiness.replay_started,
                "provider_calls": readiness.provider_calls,
            },
            "artifact_root_identity_sha256": fixture.repository.root_identity_sha256,
            "artifact_root_path": str(fixture.repository.root),
            "source_root_path": str(fixture.source_root),
        }
        (evidence_root / "stage-chain-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def test_confirmation_keeps_heldout_plan_opaque_until_open_and_retires_decode_interruption(
    stage_fixture: _StageFixture, tmp_path: Path
) -> None:
    fixture = _fork_stage_fixture(stage_fixture, "heldout-decode-interruption", tmp_path)
    repository = fixture.repository
    phase = "builder"
    ledger = None
    raw_plan_auth_phases = []
    raw_plan_auth_states = []
    plan_decode_states = []
    panel_decode_states = []
    interrupted = False
    authenticate_raw = repository.authenticate_raw_artifact
    load_typed = repository.load_typed_artifact
    load_panel = repository.load_evaluation_panel_spec

    def observe_raw_authentication(reference):
        if reference == fixture.confirmation_plan_ref:
            raw_plan_auth_phases.append(phase)
            if ledger is not None:
                state = ledger.state()[0]
                raw_plan_auth_states.append((phase, state))
                if phase == "runner":
                    assert state == "unused"
        return authenticate_raw(reference)

    def observe_typed_load(reference, *, value_type):
        if value_type is ConfirmationPanelPlanV5:
            if ledger is None:
                pytest.fail("attempt construction must keep the held-out plan opaque")
            state = ledger.state()[0]
            plan_decode_states.append((phase, state))
            assert phase == "runner" and state == "opened"
        return load_typed(reference, value_type=value_type)

    def interrupt_after_panel_decode(reference):
        nonlocal interrupted
        if reference == fixture.confirmation_panel_ref and not interrupted:
            state = ledger.state()[0]
            panel_decode_states.append(state)
            assert state == "opened"
            load_panel(reference)
            interrupted = True
            raise KeyboardInterrupt("synthetic interruption after held-out panel decode")
        return load_panel(reference)

    repository.authenticate_raw_artifact = observe_raw_authentication
    repository.load_typed_artifact = observe_typed_load
    repository.load_evaluation_panel_spec = interrupt_after_panel_decode

    attempt_ref = _confirmation_attempt(fixture)
    assert raw_plan_auth_phases == ["builder"]
    attempt = load_typed(attempt_ref, value_type=ConfirmationAttemptCommitmentV5)
    snapshot = load_typed(
        attempt.retirement_ledger.preopen_snapshot_ref,
        value_type=StageRetirementSnapshotV5,
    )
    ledger = ConfirmationLedgerV5(repository, attempt_ref, attempt, snapshot)
    assert ledger.state()[0] == "unused"

    workers = []

    def worker_factory(current_repository, inputs):
        worker = _SyntheticStageWorker(current_repository, inputs)
        workers.append(worker)
        return worker

    phase = "runner"
    outcome_ref = run_confirmation(repository=repository, attempt_ref=attempt_ref, worker_factory=worker_factory)
    outcome = repository.load_typed_artifact(outcome_ref, value_type=ConfirmationOutcomeV5)
    assert interrupted and raw_plan_auth_phases == ["builder", "runner"]
    assert raw_plan_auth_states == [("runner", "unused")]
    assert plan_decode_states == [("runner", "opened")]
    assert panel_decode_states == ["opened"]
    assert outcome.status == "cancelled" and outcome.candidate_evidence_ref is None
    assert ledger.state()[0] == "retired"
    assert len(workers) == 1 and workers[0].evaluations == [] and workers[0].closed == [False]

    recovered_ref = run_confirmation(
        repository=repository,
        attempt_ref=attempt_ref,
        worker_factory=lambda *_: pytest.fail("retired decode interruption must not evaluate again"),
    )
    assert recovered_ref == outcome_ref and ledger.state()[0] == "retired"
    assert len(workers[0].evaluations) == 0 and plan_decode_states == [("runner", "opened")]

    evidence_root_value = os.environ.get("ISSUE93_DECODE_EVIDENCE_ROOT")
    if evidence_root_value is not None:
        evidence_root = Path(evidence_root_value).resolve()
        summary = {
            "schema_version": 1,
            "source_revision": subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "evaluation_mode": "offline_fixture",
            "provider_calls": 0,
            "pit_bundle_sha256": fixture.pit_bundle_ref.sha256,
            "discovery_manifest_sha256": fixture.manifest_ref.sha256,
            "confirmation_plan_sha256": fixture.confirmation_plan_ref.sha256,
            "confirmation_panel_sha256": fixture.confirmation_panel_ref.sha256,
            "confirmation_attempt_sha256": attempt_ref.sha256,
            "confirmation_outcome_sha256": outcome_ref.sha256,
            "retirement_terminal_sha256": outcome.retirement_terminal_ref.sha256,
            "retirement_ledger_sha256": hashlib.sha256(
                repository.read_stage_ledger(attempt.retirement_ledger.relative_path)
            ).hexdigest(),
            "raw_plan_authentication_phases": raw_plan_auth_phases,
            "raw_plan_authentication_states": raw_plan_auth_states,
            "typed_plan_decode_phases_and_ledger_states": plan_decode_states,
            "panel_decode_ledger_states": panel_decode_states,
            "final_ledger_state": ledger.state()[0],
            "worker_evaluations": len(workers[0].evaluations),
            "artifact_root_identity_sha256": repository.root_identity_sha256,
        }
        (evidence_root / "heldout-ordering-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def test_confirmation_terminal_interruption_recovers_without_reevaluation(
    stage_fixture: _StageFixture, tmp_path: Path
) -> None:
    fixture = _fork_stage_fixture(stage_fixture, "confirmation-recovery", tmp_path)
    attempt_ref = _confirmation_attempt(fixture)
    workers = []

    def worker_factory(repository, inputs):
        worker = _SyntheticStageWorker(repository, inputs)
        workers.append(worker)
        return worker

    create = fixture.repository.create_typed_artifact
    interrupted = False

    def crash_after_terminal_is_durable(path, value):
        nonlocal interrupted
        reference = create(path, value)
        if path.endswith("/terminal.json") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("synthetic interruption after durable terminal write")
        return reference

    fixture.repository.create_typed_artifact = crash_after_terminal_is_durable
    with pytest.raises(KeyboardInterrupt):
        run_confirmation(repository=fixture.repository, attempt_ref=attempt_ref, worker_factory=worker_factory)
    fixture.repository.create_typed_artifact = create

    outcome_ref = run_confirmation(
        repository=fixture.repository,
        attempt_ref=attempt_ref,
        worker_factory=lambda *_: pytest.fail("prepared terminal recovery must not evaluate again"),
    )
    outcome = fixture.repository.load_typed_artifact(
        outcome_ref,
        value_type=__import__("core.pit_optimizer_v5.contracts", fromlist=["ConfirmationOutcomeV5"]).ConfirmationOutcomeV5,
    )
    cleanup = fixture.repository.load_typed_artifact(outcome.cleanup_evidence_ref, value_type=ConfirmationCleanupV5)
    assert outcome.status == "completed" and outcome.eligible_to_request_qualification
    assert cleanup.cleanup_complete and cleanup.source_unchanged and cleanup.recovered is False
    assert len(workers) == 1 and len(workers[0].evaluations) == 2
    assert run_confirmation(
        repository=fixture.repository,
        attempt_ref=attempt_ref,
        worker_factory=lambda *_: pytest.fail("retired confirmation must not execute again"),
    ) == outcome_ref


def test_tampered_panel_report_identity_retires_ineligible_and_cannot_unlock_qualification(
    stage_fixture: _StageFixture, tmp_path: Path,
) -> None:
    fixture = _fork_stage_fixture(stage_fixture, "tampered-report", tmp_path)
    attempt_ref = _confirmation_attempt(fixture)
    workers = []

    def worker_factory(repository, inputs):
        worker = _SyntheticStageWorker(repository, inputs, tamper_identity=True)
        workers.append(worker)
        return worker

    outcome_ref = run_confirmation(
        repository=fixture.repository,
        attempt_ref=attempt_ref,
        worker_factory=worker_factory,
    )
    from core.pit_optimizer_v5.contracts import ConfirmationOutcomeV5

    outcome = fixture.repository.load_typed_artifact(outcome_ref, value_type=ConfirmationOutcomeV5)
    cleanup = fixture.repository.load_typed_artifact(outcome.cleanup_evidence_ref, value_type=ConfirmationCleanupV5)
    assert outcome.status == "failed" and outcome.eligible_to_request_qualification is False
    assert outcome.candidate_evidence_ref is None and cleanup.cleanup_complete and cleanup.source_unchanged
    assert len(workers) == 1 and workers[0].evaluations == [("baseline", workers[0].evaluations[0][1])]
    with pytest.raises(ValueError):
        _qualification_attempt(fixture, outcome_ref)


def test_qualification_cleanup_recovery_preserves_failed_outcome_and_does_not_rerun(
    stage_fixture: _StageFixture, tmp_path: Path,
) -> None:
    fixture = _fork_stage_fixture(stage_fixture, "qualification-recovery", tmp_path)
    confirmation_attempt_ref = _confirmation_attempt(fixture)
    confirmation_outcome_ref = run_confirmation(
        repository=fixture.repository,
        attempt_ref=confirmation_attempt_ref,
        worker_factory=lambda repository, inputs: _SyntheticStageWorker(repository, inputs),
    )
    attempt_ref = _qualification_attempt(fixture, confirmation_outcome_ref)
    workers = []

    def worker_factory(repository, inputs):
        worker = _SyntheticQualificationWorker(repository, inputs, incomplete_cleanup=True)
        workers.append(worker)
        return worker

    outcome_ref = run_qualification(
        repository=fixture.repository,
        attempt_ref=attempt_ref,
        worker_factory=worker_factory,
    )
    from core.pit_optimizer_v5.contracts import QualificationOutcomeV5

    original = fixture.repository.load_typed_artifact(outcome_ref, value_type=QualificationOutcomeV5)
    assert original.status == "failed" and original.qualified is False
    assert len(workers) == 1 and len(workers[0].evaluations) == 2

    recovered_ref = run_qualification(
        repository=fixture.repository,
        attempt_ref=attempt_ref,
        worker_factory=lambda *_: pytest.fail("retired qualification recovery must not reexecute"),
        recovery_factory=_RecoveredQualificationCleanup,
    )
    recovered_outcome = fixture.repository.load_typed_artifact(recovered_ref, value_type=QualificationOutcomeV5)
    cleanup = qualification_cleanup_evidence_v5(fixture.repository, recovered_outcome)
    assert recovered_ref == outcome_ref
    assert recovered_outcome == original and recovered_outcome.qualified is False
    assert cleanup.cleanup_complete and cleanup.source_unchanged and cleanup.recovered is True
    assert len(workers[0].evaluations) == 2
