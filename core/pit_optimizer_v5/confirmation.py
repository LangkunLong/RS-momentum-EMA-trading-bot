"""Provider-free, one-use confirmation, detached from discovery search state.

Only the builder reads the final discovery projection. Execution consumes its
immutable selection and attempt graph; no role runner or search reducer exists
in this composition. The mutable ledger retains its original genesis bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import ntpath
from pathlib import Path, PureWindowsPath
import time
from typing import Literal, Protocol

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    SourceBundleV5,
    RenderedVariantV5,
    VariantAssignmentV5,
)
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    ConfirmationAttemptCommitmentV5,
    ConfirmationOutcomeV5,
    ConfirmationPanelPlanV5,
    EvaluatorContractV5,
    FinalizedDiscoveryCampaignV5,
    PanelEvaluationV5,
    RetirementLedgerLocatorV5,
    SandboxProfileV5,
    ScenarioGridV5,
    StageOutcomeStatusV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    selected_scenario,
    validate_campaign_manifest_bindings_v5,
    validate_episode_plan_panel_v5,
    validate_sandbox_profile_resources_v5,
)


@dataclass(frozen=True, slots=True)
class ConfirmationAdapterConfigV5:
    """Explicit local host authority with no provider capability or secret handle."""

    schema_version: Literal[5]
    discovery_manifest_ref: ArtifactRefV5
    repository_root_identity_sha256: str
    source_root: str
    workspace_root: str
    data_root: str
    output_root: str
    control_root: str
    git_executable: str
    docker_executable: str
    owner_token_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("confirmation adapter schema is invalid")
        if type(self.discovery_manifest_ref) is not ArtifactRefV5:
            raise ValueError("confirmation adapter manifest is absent")
        for digest in (self.repository_root_identity_sha256, self.owner_token_sha256):
            ArtifactRefV5("identity", digest)
        paths = (self.source_root, self.workspace_root, self.data_root, self.output_root, self.control_root)
        for path in (*paths, self.git_executable, self.docker_executable):
            if (
                type(path) is not str
                or "/" in path
                or not ntpath.isabs(path)
                or ntpath.normpath(path) != path
                or any(part.endswith((".", " ")) for part in PureWindowsPath(path).parts[1:])
            ):
                raise ValueError("confirmation adapter requires canonical absolute Windows paths")
        for index, first in enumerate(paths):
            for second in paths[index + 1 :]:
                try:
                    common = ntpath.commonpath((ntpath.normcase(first), ntpath.normcase(second)))
                except ValueError:
                    continue
                if common in {ntpath.normcase(first), ntpath.normcase(second)}:
                    raise ValueError("confirmation adapter roots overlap")


@dataclass(frozen=True, slots=True)
class FrozenConfirmationSelectionV5:
    schema_version: Literal[5]
    discovery_manifest_ref: ArtifactRefV5
    checkpoint_ref: ArtifactRefV5
    archive_ref: ArtifactRefV5
    experiment_ref: ArtifactRefV5
    policy_ref: ArtifactRefV5
    source_ref: ArtifactRefV5
    baseline_policy_ref: ArtifactRefV5
    baseline_source_ref: ArtifactRefV5
    final_round: int
    finalized_campaign_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if self.schema_version != 5 or type(self.final_round) is not int or self.final_round < 1:
            raise ValueError("frozen confirmation selection is invalid")
        for reference in (
            self.discovery_manifest_ref,
            self.checkpoint_ref,
            self.archive_ref,
            self.experiment_ref,
            self.policy_ref,
            self.source_ref,
            self.baseline_policy_ref,
            self.baseline_source_ref,
            self.finalized_campaign_ref,
        ):
            if type(reference) is not ArtifactRefV5:
                raise ValueError("frozen selection requires complete authenticated edges")


@dataclass(frozen=True, slots=True)
class ConfirmationCleanupV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    source_unchanged: bool
    cleanup_complete: bool
    recovered: bool
    provider_calls: Literal[0] = 0

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or type(self.attempt_ref) is not ArtifactRefV5
            or any(type(item) is not bool for item in (self.source_unchanged, self.cleanup_complete, self.recovered))
            or type(self.provider_calls) is not int
            or self.provider_calls != 0
        ):
            raise ValueError("confirmation cleanup evidence is invalid")


@dataclass(frozen=True, slots=True)
class ConfirmationRetirementV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    preopen_snapshot_ref: ArtifactRefV5
    prior_state_sha256: str
    status: StageOutcomeStatusV5
    cleanup_evidence_ref: ArtifactRefV5
    baseline_evidence_ref: ArtifactRefV5 | None
    candidate_evidence_ref: ArtifactRefV5 | None

    def __post_init__(self) -> None:
        if self.schema_version != 5 or self.status not in {"completed", "failed", "timed_out", "cancelled"}:
            raise ValueError("confirmation retirement is invalid")
        ArtifactRefV5("identity", self.prior_state_sha256)
        for item in (self.attempt_ref, self.preopen_snapshot_ref, self.cleanup_evidence_ref):
            if type(item) is not ArtifactRefV5:
                raise ValueError("confirmation retirement authority is absent")
        if self.status == "completed":
            if (
                type(self.baseline_evidence_ref) is not ArtifactRefV5
                or type(self.candidate_evidence_ref) is not ArtifactRefV5
            ):
                raise ValueError("completed retirement requires both evidence references")
        elif self.baseline_evidence_ref is not None or self.candidate_evidence_ref is not None:
            raise ValueError("failed retirement cannot carry evaluation metrics")


def _load(repository, reference, value_type):
    value = repository.load_typed_artifact(reference, value_type=value_type)
    if type(value) is not value_type or canonical_sha256_v5(value) != reference.sha256:
        raise ValueError("confirmation typed artifact authentication failed")
    return value


def _walk(repository, roots, *, opaque=(), raw=()):
    """Authenticate all graph edges; sealed plan bytes never reach a JSON parser."""
    done, active = set(), set()
    by_path = {}

    def visit(reference):
        if reference in active:
            raise ValueError("confirmation artifact graph contains a cycle")
        previous = by_path.setdefault(reference.relative_path, reference.sha256)
        if previous != reference.sha256:
            raise ValueError("confirmation graph has conflicting path authority")
        if reference in done:
            return
        active.add(reference)
        if reference in opaque or reference in raw:
            repository.authenticate_raw_artifact(reference)
        else:
            item = repository.authenticate(reference)
            if item.reference != reference or hashlib.sha256(item.content).hexdigest() != reference.sha256:
                raise ValueError("confirmation graph authentication failed")
            for child in item.child_references:
                visit(child)
        active.remove(reference)
        done.add(reference)

    for root in roots:
        visit(root)


def _domain(attempt):
    return canonical_sha256_v5(
        {
            "schema_version": 5,
            "domain": "pit-optimizer-v5-stage-retirement-v1",
            "stage": "confirmation",
            "ledger_relative_path": attempt.retirement_ledger.relative_path,
            "pit_bundle_sha256": attempt.pit_bundle_ref.sha256,
            "prices_provenance_sha256": attempt.prices_provenance_ref.sha256,
        }
    )


def _genesis(snapshot):
    value = {
        "schema_version": 4,
        "ledger_kind": "qualification_retirement",
        "qualification_retirement_domain_id": snapshot.retirement_domain_id,
        "record_type": "genesis",
        "sequence": 0,
        "previous_record_sha256": None,
    }
    # Existing stage-ledger genesis uses newline-bearing canonical JSON.
    value["record_sha256"] = hashlib.sha256(canonical_json_bytes_v5(value) + b"\n").hexdigest()
    if (
        snapshot.stage != "confirmation"
        or snapshot.record_count != 1
        or snapshot.retired_security_lineage_ids
        or snapshot.ledger_head_sha256 != value["record_sha256"]
    ):
        raise ValueError("confirmation ledger snapshot is not unused genesis")
    return canonical_json_bytes_v5(value) + b"\n"


class ConfirmationLedgerV5:
    """V5 content-free stage records appended atomically after the legacy genesis.

    The original ledger prefix is preserved exactly. Legacy readers reject the
    V5 extension, so they cannot mistake this retired domain for unused state.
    The caller holds the domain's repository transition lock for the full run.
    """

    def __init__(self, repository, attempt_ref, attempt, snapshot):
        self.repository, self.attempt_ref, self.attempt, self.snapshot = repository, attempt_ref, attempt, snapshot
        self.initial = _genesis(snapshot)
        self.opened = (
            canonical_json_bytes_v5(
                {
                    "schema_version": 5,
                    "record_type": "confirmation_opened",
                    "attempt_ref": attempt_ref,
                    "preopen_snapshot_ref": attempt.retirement_ledger.preopen_snapshot_ref,
                    "retirement_domain_id": attempt.retirement_domain_id,
                    "prior_state_sha256": hashlib.sha256(self.initial).hexdigest(),
                }
            )
            + b"\n"
        )

    def state(self):
        raw = self.repository.read_stage_ledger(self.attempt.retirement_ledger.relative_path)
        if raw == self.initial:
            return "unused", None
        prefix = self.initial + self.opened
        if not raw.startswith(prefix):
            raise ValueError("confirmation ledger changed or belongs to another attempt")
        suffix = raw[len(prefix) :]
        if not suffix:
            return "opened", None
        try:
            value = json.loads(suffix)
            if set(value) != {"schema_version", "record_type", "terminal_ref", "prior_state_sha256"}:
                raise ValueError
            ref = ArtifactRefV5(**value["terminal_ref"])
            if (
                value["schema_version"] != 5
                or value["record_type"] != "confirmation_terminal"
                or value["prior_state_sha256"] != hashlib.sha256(prefix).hexdigest()
                or canonical_json_bytes_v5(value) + b"\n" != suffix
            ):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ValueError("confirmation ledger terminal chain is invalid") from None
        return "retired", ref

    def open(self):
        if self.state()[0] != "unused":
            raise ValueError("confirmation domain has already been opened")
        self.repository.append_stage_ledger(
            self.attempt.retirement_ledger.relative_path,
            prior=self.initial,
            event=self.opened,
        )

    def retire(self, terminal_ref):
        state, existing = self.state()
        if state == "retired":
            if existing != terminal_ref:
                raise ValueError("confirmation domain already has another terminal")
            return
        if state != "opened":
            raise ValueError("confirmation cannot retire without an opening")
        prefix = self.initial + self.opened
        event = (
            canonical_json_bytes_v5(
                {
                    "schema_version": 5,
                    "record_type": "confirmation_terminal",
                    "terminal_ref": terminal_ref,
                    "prior_state_sha256": hashlib.sha256(prefix).hexdigest(),
                }
            )
            + b"\n"
        )
        self.repository.append_stage_ledger(self.attempt.retirement_ledger.relative_path, prior=prefix, event=event)


def build_confirmation_attempt(
    *,
    repository,
    discovery_manifest_ref: ArtifactRefV5,
    discovery_checkpoint_ref: ArtifactRefV5,
    discovery_archive_ref: ArtifactRefV5,
    finalized_campaign_ref: ArtifactRefV5,
    confirmation_plan_ref: ArtifactRefV5,
    scenario_grid_ref: ArtifactRefV5,
    retirement_ledger: RetirementLedgerLocatorV5,
    execution_adapter_ref: ArtifactRefV5,
    attempt_id: str,
    output_path: str,
) -> ArtifactRefV5:
    """Freeze the authenticated final champion without parsing held-out bytes.

    Every completed round must be covered by explicit finalized-campaign
    authority. Source-only work cannot synthesize closure from checkpoint
    counters; absent or unauthentic closure fails before any attempt is written.
    """
    from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
    from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5
    from core.pit_optimizer_v5.search import verified_campaign_cagr_pct

    authorities = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=discovery_manifest_ref)
    manifest, plan = authorities.manifest, authorities.panel_plan
    if confirmation_plan_ref.sha256 != plan.confirmation_plan_sha256:
        raise ValueError("confirmation plan is not the discovery commitment")
    adapter = _load(repository, execution_adapter_ref, ConfirmationAdapterConfigV5)
    if (
        adapter.discovery_manifest_ref != discovery_manifest_ref
        or adapter.repository_root_identity_sha256 != repository.root_identity_sha256
    ):
        raise ValueError("confirmation host adapter differs from discovery authority")
    _walk(
        repository,
        (
            discovery_checkpoint_ref,
            discovery_archive_ref,
            finalized_campaign_ref,
            execution_adapter_ref,
            scenario_grid_ref,
            confirmation_plan_ref,
            retirement_ledger.preopen_snapshot_ref,
        ),
        opaque=(confirmation_plan_ref,),
        raw=(plan.pit_bundle_ref, plan.prices_provenance_ref),
    )
    champion, experiment = repository.load_confirmation_champion(
        checkpoint_ref=discovery_checkpoint_ref,
        archive_ref=discovery_archive_ref,
        finalized_campaign_ref=finalized_campaign_ref,
        discovery_manifest_ref=discovery_manifest_ref,
    )
    score = verified_campaign_cagr_pct(
        campaign=champion.campaign,
        discovery_plan=plan,
        evaluator_contract=authorities.evaluator_contract,
        policy_identity_sha256=champion.policy_revision.sha256,
    )
    if score <= authorities.baseline_authority.campaign.campaign_cagr_pct:
        raise ValueError("final discovery champion did not beat the unchanged baseline")
    policy_refs = tuple(ref for ref in experiment.artifact_refs if ref.sha256 == champion.policy_revision.sha256)
    if len(policy_refs) > 1:
        raise ValueError("champion contains ambiguous immutable policy references")
    policy_ref = (
        policy_refs[0] if policy_refs else ArtifactRefV5(output_path + ".policy.json", champion.policy_revision.sha256)
    )
    selection = FrozenConfirmationSelectionV5(
        5,
        discovery_manifest_ref,
        discovery_checkpoint_ref,
        discovery_archive_ref,
        champion.experiment_record_ref,
        policy_ref,
        champion.source_bundle_ref,
        manifest.baseline_policy_revision_ref,
        authorities.baseline_authority.source_bundle_ref,
        manifest.search.max_feedback_rounds,
        finalized_campaign_ref,
    )
    source = _load(repository, selection.source_ref, SourceBundleV5)
    policy = (
        _load(repository, selection.policy_ref, PolicyRevisionIdentityV5) if policy_refs else champion.policy_revision
    )
    if (
        policy != champion.policy_revision
        or tuple((item.path, item.sha256) for item in source.files) != policy.editable_source_sha256
    ):
        raise ValueError("frozen champion source differs from its immutable policy")
    grid = _load(repository, scenario_grid_ref, ScenarioGridV5)
    if grid.scenarios != authorities.evaluator_contract.friction_grid:
        raise ValueError("confirmation friction grid differs from discovery evaluator")
    snapshot = _load(repository, retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    selection_path = output_path + ".selection.json"
    selection_ref = ArtifactRefV5(selection_path, canonical_sha256_v5(selection))
    attempt = ConfirmationAttemptCommitmentV5(
        5,
        attempt_id,
        confirmation_plan_ref,
        discovery_manifest_ref,
        selection.policy_ref,
        selection.experiment_ref,
        plan.pit_bundle_ref,
        plan.prices_provenance_ref,
        manifest.execution_profile_ref,
        manifest.evaluator_contract_ref,
        scenario_grid_ref,
        manifest.baseline_authority_ref,
        manifest.sandbox_profile_ref,
        snapshot.retirement_domain_id,
        retirement_ledger,
        selection_ref,
        execution_adapter_ref,
    )
    if (
        snapshot.ledger_relative_path != retirement_ledger.relative_path
        or _domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("confirmation retirement domain differs from its data authority")
    with repository.adapter_state_transition(namespace="confirmation", key=attempt.retirement_domain_id):
        if repository.read_stage_ledger(retirement_ledger.relative_path) != _genesis(snapshot):
            raise ValueError("confirmation ledger changed or was already used")
        if not policy_refs:
            repository.create_typed_artifact(policy_ref.relative_path, policy)
        repository.create_typed_artifact(selection_path, selection)
        return repository.create_typed_artifact(output_path, attempt)


@dataclass(frozen=True, slots=True)
class ConfirmationInputsV5:
    attempt_ref: ArtifactRefV5
    attempt: ConfirmationAttemptCommitmentV5
    selection: FrozenConfirmationSelectionV5
    manifest: CampaignManifestV5
    discovery: CampaignPanelPlanV5
    evaluator: EvaluatorContractV5
    execution: ExecutionProfileV5
    sandbox: SandboxProfileV5
    grid: ScenarioGridV5
    adapter: ConfirmationAdapterConfigV5
    baseline_policy: PolicyRevisionIdentityV5
    baseline_source: SourceBundleV5
    candidate_policy: PolicyRevisionIdentityV5
    candidate_source: SourceBundleV5


def _inputs(repository, attempt_ref, attempt):
    # Discovery archive and checkpoint are opaque byte commitments here. The
    # builder is the sole consumer of their projection and experiment records.
    selection = _load(repository, attempt.frozen_selection_ref, FrozenConfirmationSelectionV5)
    finalization = _load(repository, selection.finalized_campaign_ref, FinalizedDiscoveryCampaignV5)
    manifest = _load(repository, attempt.discovery_manifest_ref, CampaignManifestV5)
    discovery = _load(repository, manifest.panel_plan_ref, CampaignPanelPlanV5)
    _walk(
        repository,
        (attempt_ref,),
        opaque=(
            attempt.confirmation_plan_ref,
            selection.checkpoint_ref,
            selection.archive_ref,
            selection.experiment_ref,
            selection.finalized_campaign_ref,
        ),
        raw=(attempt.pit_bundle_ref, attempt.prices_provenance_ref),
    )
    evaluator = _load(repository, attempt.evaluator_contract_ref, EvaluatorContractV5)
    execution = _load(repository, attempt.execution_profile_ref, ExecutionProfileV5)
    sandbox = _load(repository, attempt.sandbox_profile_ref, SandboxProfileV5)
    grid = _load(repository, attempt.scenario_grid_ref, ScenarioGridV5)
    adapter = _load(repository, attempt.execution_adapter_ref, ConfirmationAdapterConfigV5)
    baseline_policy = _load(repository, selection.baseline_policy_ref, PolicyRevisionIdentityV5)
    baseline_source = _load(repository, selection.baseline_source_ref, SourceBundleV5)
    candidate_policy = _load(repository, selection.policy_ref, PolicyRevisionIdentityV5)
    candidate_source = _load(repository, selection.source_ref, SourceBundleV5)
    validate_campaign_manifest_bindings_v5(manifest, panel_plan=discovery, evaluator_contract=evaluator)
    validate_sandbox_profile_resources_v5(sandbox, manifest.resources)
    if (
        selection.discovery_manifest_ref != attempt.discovery_manifest_ref
        or finalization.discovery_manifest_ref != attempt.discovery_manifest_ref
        or finalization.checkpoint_ref != selection.checkpoint_ref
        or finalization.archive_ref != selection.archive_ref
        or len(finalization.rounds) != selection.final_round
        or selection.policy_ref != attempt.discovery_champion_policy_ref
        or selection.experiment_ref != attempt.discovery_champion_experiment_ref
        or selection.final_round != manifest.search.max_feedback_rounds
        or selection.baseline_policy_ref != manifest.baseline_policy_revision_ref
        or attempt.confirmation_plan_ref.sha256 != discovery.confirmation_plan_sha256
        or attempt.pit_bundle_ref != discovery.pit_bundle_ref
        or attempt.prices_provenance_ref != discovery.prices_provenance_ref
        or attempt.execution_profile_ref != manifest.execution_profile_ref
        or attempt.evaluator_contract_ref != manifest.evaluator_contract_ref
        or attempt.sandbox_profile_ref != manifest.sandbox_profile_ref
        or attempt.baseline_authority_ref != manifest.baseline_authority_ref
        or execution.sha256 != evaluator.execution_profile_sha256
        or sandbox.sha256 != evaluator.sandbox_profile_sha256
        or grid.scenarios != evaluator.friction_grid
        or baseline_policy.sha256 != evaluator.baseline_policy_revision_sha256
        or baseline_source.sha256 != evaluator.baseline_source_bundle_sha256
        or adapter.discovery_manifest_ref != attempt.discovery_manifest_ref
        or adapter.repository_root_identity_sha256 != repository.root_identity_sha256
        or _domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("confirmation attempt graph has inconsistent authorities")
    for source, policy in ((baseline_source, baseline_policy), (candidate_source, candidate_policy)):
        if (
            tuple((item.path, item.sha256) for item in source.files) != policy.editable_source_sha256
            or policy.trusted_policy_runtime_sha256 != baseline_policy.trusted_policy_runtime_sha256
            or policy.immutable_constraints_sha256 != baseline_policy.immutable_constraints_sha256
        ):
            raise ValueError("confirmation policy source authority is inconsistent")
    return ConfirmationInputsV5(
        attempt_ref,
        attempt,
        selection,
        manifest,
        discovery,
        evaluator,
        execution,
        sandbox,
        grid,
        adapter,
        baseline_policy,
        baseline_source,
        candidate_policy,
        candidate_source,
    )


def _panel(repository, inputs, snapshot):
    plan = _load(repository, inputs.attempt.confirmation_plan_ref, ConfirmationPanelPlanV5)
    panel = repository.load_evaluation_panel_spec(plan.episode.panel_ref)
    validate_episode_plan_panel_v5(plan.episode, panel)
    if (
        plan.pit_bundle_sha256 != inputs.attempt.pit_bundle_ref.sha256
        or plan.partition_seed_sha256 != inputs.discovery.partition_seed_sha256
        or plan.target_sha256 != inputs.manifest.target.sha256
        or plan.confirmation_retirement_domain_id != inputs.attempt.retirement_domain_id
        or plan.confirmation_ledger_snapshot_sha256 != snapshot.ledger_head_sha256
        or plan.episode.purpose != "qualification"
        or panel.purpose != "qualification"
        or max(item.end_date for item in inputs.discovery.discovery) >= plan.episode.start_date
        or any(
            set(item.lineage_ids) & set(plan.episode.lineage_ids)
            for item in (inputs.discovery.mechanics, inputs.discovery.quick, *inputs.discovery.discovery)
        )
    ):
        raise ValueError("opened confirmation owner differs from its sealed commitments")
    return plan


def _cagr(evaluation):
    scenario = selected_scenario(evaluation)
    with localcontext() as context:
        context.prec, context.rounding = 40, ROUND_HALF_EVEN
        value = (
            (scenario.ending_equity / scenario.starting_equity)
            ** (Decimal(365) / Decimal(evaluation.elapsed_calendar_days))
            - 1
        ) * 100
        value = value.quantize(Decimal("0.000001"))
    if value != scenario.report.portfolio_annualized_return_pct:
        raise ValueError("confirmation CAGR differs from authenticated equity endpoints")
    return value


def _evidence(inputs, plan, value, policy):
    if type(value) is not PanelEvaluationV5 or (
        value.policy_identity_sha256 != policy.sha256
        or value.evaluator_contract_sha256 != inputs.evaluator.sha256
        or value.sandbox_profile_sha256 != inputs.sandbox.sha256
        or value.panel_sha256 != plan.episode.panel_ref.sha256
        or (value.start_date, value.end_date) != (plan.episode.start_date, plan.episode.end_date)
        or value.selection_scenario_id != inputs.evaluator.selection_scenario_id
        or tuple(item.scenario_id for item in value.scenarios)
        != tuple(item.scenario_id for item in inputs.grid.scenarios)
    ):
        raise ValueError("confirmation evidence differs from shared evaluation authority")
    return _cagr(value)


class ConfirmationWorkerV5(Protocol):
    def evaluate(
        self, inputs: ConfirmationInputsV5, plan: ConfirmationPanelPlanV5, *, baseline: bool
    ) -> PanelEvaluationV5: ...
    def close(self, inputs: ConfirmationInputsV5, *, recovered: bool) -> ConfirmationCleanupV5: ...


def _result(repository, inputs, terminal_ref, terminal, plan):
    cleanup = _load(repository, terminal.cleanup_evidence_ref, ConfirmationCleanupV5)
    if cleanup.attempt_ref != inputs.attempt_ref or terminal.attempt_ref != inputs.attempt_ref:
        raise ValueError("confirmation terminal has foreign authority")
    baseline_cagr = candidate_cagr = active = None
    if terminal.status == "completed":
        if not cleanup.source_unchanged or not cleanup.cleanup_complete:
            raise ValueError("completed confirmation lacks verified source and cleanup")
        baseline = _load(repository, terminal.baseline_evidence_ref, PanelEvaluationV5)
        candidate = _load(repository, terminal.candidate_evidence_ref, PanelEvaluationV5)
        baseline_cagr = _evidence(inputs, plan, baseline, inputs.baseline_policy)
        candidate_cagr = _evidence(inputs, plan, candidate, inputs.candidate_policy)
        active = selected_scenario(candidate).report.closed_trades > 0
    return ConfirmationOutcomeV5(
        5,
        inputs.attempt_ref,
        terminal.status,
        inputs.attempt.discovery_champion_policy_ref,
        terminal.baseline_evidence_ref,
        terminal.candidate_evidence_ref,
        baseline_cagr,
        candidate_cagr,
        None if baseline_cagr is None else candidate_cagr - baseline_cagr,
        active,
        bool(active and candidate_cagr > baseline_cagr),
        terminal_ref,
        terminal.cleanup_evidence_ref,
        0,
    )


def _failed_outcome(attempt_ref, attempt, terminal_ref, terminal):
    if terminal.status == "completed":
        raise ValueError("completed confirmation requires its authenticated evidence graph")
    return ConfirmationOutcomeV5(
        5,
        attempt_ref,
        terminal.status,
        attempt.discovery_champion_policy_ref,
        None,
        None,
        None,
        None,
        None,
        None,
        False,
        terminal_ref,
        terminal.cleanup_evidence_ref,
        0,
    )


def _check_terminal(ledger, terminal):
    if (
        terminal.attempt_ref != ledger.attempt_ref
        or terminal.preopen_snapshot_ref != ledger.attempt.retirement_ledger.preopen_snapshot_ref
        or terminal.prior_state_sha256 != hashlib.sha256(ledger.initial + ledger.opened).hexdigest()
    ):
        raise ValueError("confirmation retirement differs from its ledger chain")


def run_confirmation(*, repository, attempt_ref: ArtifactRefV5, worker_factory=None) -> ArtifactRefV5:
    """Open once, retire every terminal path, and recover crashes without reevaluation."""
    from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5

    attempt = _load(repository, attempt_ref, ConfirmationAttemptCommitmentV5)
    snapshot = _load(repository, attempt.retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    if (
        snapshot.retirement_domain_id != attempt.retirement_domain_id
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
    ):
        raise ValueError("confirmation attempt ledger authority is inconsistent")
    prefix = f"confirmation/{attempt.sha256}"
    # This lock is retained through evaluation. A second controller cannot
    # mistake a live opened attempt for an abandoned controller's work.
    with repository.adapter_state_transition(namespace="confirmation", key=attempt.retirement_domain_id):
        ledger = ConfirmationLedgerV5(repository, attempt_ref, attempt, snapshot)
        state, terminal_ref = ledger.state()
        if state == "retired":
            terminal = _load(repository, terminal_ref, ConfirmationRetirementV5)
            _check_terminal(ledger, terminal)
            if terminal.status == "completed":
                inputs = _inputs(repository, attempt_ref, attempt)
                plan = _panel(repository, inputs, snapshot)
                outcome = _result(repository, inputs, terminal_ref, terminal, plan)
            else:
                _load(repository, terminal.cleanup_evidence_ref, ConfirmationCleanupV5)
                outcome = _failed_outcome(attempt_ref, attempt, terminal_ref, terminal)
            return repository.create_typed_artifact(prefix + "/outcome.json", outcome)
        recovered = state == "opened"
        inputs = None
        if not recovered:
            inputs = _inputs(repository, attempt_ref, attempt)
            ledger.open()
        else:
            prepared = repository.load_confirmation_record(
                prefix + "/terminal.json", value_type=ConfirmationRetirementV5
            )
            if prepared is not None:
                terminal_ref, terminal = prepared
                _check_terminal(ledger, terminal)
                ledger.retire(terminal_ref)
                if terminal.status == "completed":
                    inputs = _inputs(repository, attempt_ref, attempt)
                    plan = _panel(repository, inputs, snapshot)
                    outcome = _result(repository, inputs, terminal_ref, terminal, plan)
                else:
                    outcome = _failed_outcome(attempt_ref, attempt, terminal_ref, terminal)
                return repository.create_typed_artifact(prefix + "/outcome.json", outcome)
        worker, plan = None, None
        status, baseline_ref, candidate_ref = "failed", None, None
        cleanup = ConfirmationCleanupV5(5, attempt_ref, False, False, recovered)
        try:
            if inputs is None:
                inputs = _inputs(repository, attempt_ref, attempt)
            worker = (LocalConfirmationWorkerV5 if worker_factory is None else worker_factory)(repository, inputs)
            if not recovered:
                plan = _panel(repository, inputs, snapshot)
                baseline = worker.evaluate(inputs, plan, baseline=True)
                _evidence(inputs, plan, baseline, inputs.baseline_policy)
                baseline_ref = repository.create_typed_artifact(prefix + "/baseline.json", baseline)
                candidate = worker.evaluate(inputs, plan, baseline=False)
                _evidence(inputs, plan, candidate, inputs.candidate_policy)
                candidate_ref = repository.create_typed_artifact(prefix + "/candidate.json", candidate)
                status = "completed"
        except TimeoutError:
            status = "timed_out"
        except (KeyboardInterrupt, SystemExit):
            status = "cancelled"
        except BaseException:
            status = "failed"
        finally:
            if worker is not None:
                try:
                    cleanup = worker.close(inputs, recovered=recovered)
                    if type(cleanup) is not ConfirmationCleanupV5 or cleanup.attempt_ref != attempt_ref:
                        raise ValueError("confirmation cleanup has foreign authority")
                except BaseException:
                    cleanup = ConfirmationCleanupV5(5, attempt_ref, False, False, recovered)
            if not cleanup.source_unchanged or not cleanup.cleanup_complete:
                status = "failed"
            if status != "completed":
                baseline_ref = candidate_ref = None
            previous_cleanup = repository.load_confirmation_record(
                prefix + "/cleanup.json", value_type=ConfirmationCleanupV5
            )
            if previous_cleanup is None:
                cleanup_ref = repository.create_typed_artifact(prefix + "/cleanup.json", cleanup)
            else:
                cleanup_ref, cleanup = previous_cleanup
                if cleanup.attempt_ref != attempt_ref:
                    raise ValueError("recovered cleanup is foreign")
            terminal = ConfirmationRetirementV5(
                5,
                attempt_ref,
                attempt.retirement_ledger.preopen_snapshot_ref,
                hashlib.sha256(ledger.initial + ledger.opened).hexdigest(),
                status,
                cleanup_ref,
                baseline_ref,
                candidate_ref,
            )
            terminal_ref = repository.create_typed_artifact(prefix + "/terminal.json", terminal)
            ledger.retire(terminal_ref)
        outcome = (
            _result(repository, inputs, terminal_ref, terminal, plan)
            if status == "completed"
            else _failed_outcome(attempt_ref, attempt, terminal_ref, terminal)
        )
        return repository.create_typed_artifact(prefix + "/outcome.json", outcome)


class LocalConfirmationWorkerV5:
    """Concrete disposable Git worker plus the existing network-absent Docker evaluator."""

    def __init__(self, repository, inputs):
        from core.pit_optimizer_v5.manifest import capture_clean_policy_snapshot_v5
        from core.pit_optimizer_v5.production_workspace import LocalGitWorkspaceDriverV5
        from core.pit_optimizer_v5.production_sandbox import LocalSandboxMountFactoryV5, LocalContainerExecutorV5
        from core.pit_optimizer_v5.sandbox import DockerPanelEvaluatorV5
        from core.pit_optimizer_v5.workspace import WorkspaceOwnerV5, WorkspaceRootsV5, GitCandidateMaterializerV5

        self.repository, self.inputs = repository, inputs
        config = inputs.adapter
        self.source_snapshot = capture_clean_policy_snapshot_v5(
            source_root=Path(config.source_root),
            git_executable=Path(config.git_executable),
            expected_source_commit=inputs.manifest.source_commit,
        )
        self.owner = WorkspaceOwnerV5(
            inputs.manifest.campaign_id,
            inputs.selection.final_round,
            config.owner_token_sha256,
            "pit-v5-confirm-" + inputs.attempt.sha256,
        )
        roots = WorkspaceRootsV5(config.source_root, config.workspace_root)
        self.driver = LocalGitWorkspaceDriverV5(
            roots=roots,
            source_commit=inputs.manifest.source_commit,
            git_executable=Path(config.git_executable),
            repository=repository,
            owner=self.owner,
        )
        self.materializer = GitCandidateMaterializerV5(roots=roots, driver=self.driver, token_factory=self._token)
        self.mounts = LocalSandboxMountFactoryV5(
            manifest=inputs.manifest,
            evaluator_contract=inputs.evaluator,
            sandbox_profile=inputs.sandbox,
            owner=self.owner,
            workspace_driver=self.driver,
            data_root=Path(config.data_root),
            output_root=Path(config.output_root),
            repository=repository,
        )
        self.executor = LocalContainerExecutorV5(
            manifest=inputs.manifest,
            sandbox_profile=inputs.sandbox,
            owner=self.owner,
            mount_factory=self.mounts,
            docker_executable=Path(config.docker_executable),
            control_root=Path(config.control_root),
            repository=repository,
        )
        self.evaluator = DockerPanelEvaluatorV5(executor=self.executor, clock=self)
        self.leases, self.workspace_leases = [], []
        self.role = None
        self.before = self.materializer._read_source_bundle()
        if self.before != inputs.baseline_source:
            raise ValueError("confirmation source checkout differs from unchanged baseline")

    @staticmethod
    def monotonic():
        return time.monotonic()

    def _token(self):
        if self.role not in {"baseline", "candidate"}:
            raise ValueError("confirmation worker policy is not frozen")
        return canonical_sha256_v5((self.inputs.attempt.sha256, self.role))

    def register_execution(self, authority, leases):
        self.leases.extend(leases)
        self.repository.create_typed_artifact(
            f"confirmation/{self.inputs.attempt.sha256}/{self.role}-execution.json", authority
        )

    def evaluate(self, inputs, plan, *, baseline):
        from core.pit_optimizer_v5.memory import CandidateExecutionKeyV5
        from core.pit_optimizer_v5.runtime import StageDeadlineV5
        from core.pit_optimizer_v5.sandbox import DockerPanelRequestV5

        if inputs is not self.inputs:
            raise ValueError("confirmation worker inputs changed")
        self.role = "baseline" if baseline else "candidate"
        policy = inputs.baseline_policy if baseline else inputs.candidate_policy
        source = inputs.baseline_source if baseline else inputs.candidate_source
        variant = RenderedVariantV5(VariantAssignmentV5(()), source, policy)
        workspace = self.materializer.materialize(
            owner=self.owner, parent_revision=inputs.baseline_policy, variant=variant
        )
        self.workspace_leases.append(workspace.lease)
        materialized = workspace.as_runtime_materialization(
            inputs.selection.baseline_source_ref if baseline else inputs.selection.source_ref
        )
        key = CandidateExecutionKeyV5(
            canonical_sha256_v5((inputs.attempt.sha256, self.role)), "confirmation_evaluation", None
        )
        scenarios = tuple(item.scenario_id for item in inputs.grid.scenarios)
        mounts = self.mounts.mounts_for(
            materialized=materialized, panel=plan.episode, scenario_ids=scenarios, execution_key=key
        )
        request = DockerPanelRequestV5(
            self.owner,
            inputs.manifest,
            key,
            policy,
            inputs.evaluator,
            inputs.sandbox,
            plan.episode,
            scenarios,
            *mounts,
            self.mounts.panel_for(plan.episode),
        )
        deadline = StageDeadlineV5(
            "confirmation_evaluation", time.monotonic() + inputs.manifest.resources.discovery_episode_timeout_seconds
        )
        outcome = self.evaluator.evaluate(request, deadline=deadline, registrar=self)
        if outcome.failure is not None:
            if outcome.failure.code == "timed_out":
                raise TimeoutError
            raise ValueError("confirmation sandbox evaluation failed")
        return outcome.evaluation

    def close(self, inputs, *, recovered):
        from core.pit_optimizer_v5.manifest import capture_clean_policy_snapshot_v5
        from core.pit_optimizer_v5.memory import CandidateExecutionAuthorityV5

        complete = True
        cleanup_deadline = time.monotonic() + inputs.manifest.resources.cleanup_timeout_seconds
        # Reconcile exact deterministic slots even on ordinary failure: a
        # materializer can fail after reservation but before returning its lease.
        # No resource enumeration, discovery journal, or replay is permitted.
        for role in ("baseline", "candidate"):
            self.role = role
            record = None
            try:
                record = self.repository.load_confirmation_record(
                    f"confirmation/{inputs.attempt.sha256}/{role}-execution.json",
                    value_type=CandidateExecutionAuthorityV5,
                )
                if recovered and record is not None:
                    authority = record[1]
                    if (
                        authority.key.stage != "confirmation_evaluation"
                        or authority.key.episode_ordinal is not None
                        or authority.key.experiment_id != canonical_sha256_v5((inputs.attempt.sha256, role))
                    ):
                        raise ValueError("confirmation cleanup execution belongs to another policy slot")
                    self.leases.extend(
                        self.executor.recover_lease_from_authority(
                            payload, authority=authority, round_index=self.owner.round_index
                        )
                        for payload in authority.lease_payloads
                    )
            except BaseException:
                complete = False
            # Workspace cleanup must still run if container authority recovery
            # failed. These are separately owned deterministic resources.
            try:
                lease = self.driver.load_lease("workspace." + hashlib.sha256(self._token().encode()).hexdigest())
                if lease is not None:
                    if lease not in self.workspace_leases:
                        self.workspace_leases.append(lease)
                    if record is None:
                        # Mount issuance can precede execution registration.
                        # Without its authority, output absence is unproven.
                        complete = False
            except BaseException:
                complete = False
        try:
            result = self.executor.cleanup_many(owner=self.owner, leases=tuple(self.leases))
            complete = complete and result.cleanup_complete
        except BaseException:
            complete = False
        for lease in self.workspace_leases:
            try:
                if time.monotonic() >= cleanup_deadline:
                    raise TimeoutError("confirmation cleanup deadline exceeded")
                self.materializer.cleanup(owner=self.owner, lease=lease)
            except BaseException:
                complete = False
        try:
            unchanged = (
                self.materializer._read_source_bundle() == inputs.baseline_source
                and capture_clean_policy_snapshot_v5(
                    source_root=Path(inputs.adapter.source_root),
                    git_executable=Path(inputs.adapter.git_executable),
                    expected_source_commit=inputs.manifest.source_commit,
                )
                == self.source_snapshot
            )
        except BaseException:
            unchanged = False
        return ConfirmationCleanupV5(
            5, inputs.attempt_ref, unchanged, complete and time.monotonic() < cleanup_deadline, recovered
        )


__all__ = ["build_confirmation_attempt", "run_confirmation", "ConfirmationAdapterConfigV5"]
