"""Detached, provider-free qualification through a sealed one-use attempt.

Only attempt construction reauthenticates the discovery reducer. Execution
consumes the frozen confirmation graph and never loads discovery search state.
The confirmation sandbox stage is reused as an internal typed held-out adapter;
qualification owns distinct locks, ledger events, workspaces and recovery records.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import json
from pathlib import Path
import time

from core.pit_optimizer_v5 import confirmation as confirmed
from core.pit_optimizer_v5.candidate_ir import RenderedVariantV5, VariantAssignmentV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    ConfirmationAttemptCommitmentV5,
    ConfirmationOutcomeV5,
    ConfirmationPanelPlanV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    QualificationAttemptCommitmentV5,
    QualificationOutcomeV5,
    QualificationPanelPlanV5,
    RetirementLedgerLocatorV5,
    SandboxProfileV5,
    StageOutcomeStatusV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    selected_scenario,
    validate_episode_plan_panel_v5,
    validate_sandbox_profile_resources_v5,
)
from typing import Literal

_load = confirmed._load
_walk = confirmed._walk
_evidence = confirmed._evidence


def _check_terminal(ledger, terminal):
    confirmed._check_terminal(ledger, terminal)
    cleanup_type = (
        QualificationCleanupV5 if type(terminal) is QualificationRetirementV5 else confirmed.ConfirmationCleanupV5
    )
    cleanup = _load(ledger.repository, terminal.cleanup_evidence_ref, cleanup_type)
    if cleanup.attempt_ref != ledger.attempt_ref:
        raise ValueError("retirement cleanup belongs to another attempt")
    if terminal.status == "completed" and (not cleanup.source_unchanged or not cleanup.cleanup_complete):
        raise ValueError("completed retirement has unverified cleanup")


@dataclass(frozen=True, slots=True)
class QualificationCleanupV5:
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
            raise ValueError("qualification cleanup evidence is invalid")


@dataclass(frozen=True, slots=True)
class QualificationRetirementV5:
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
            raise ValueError("qualification retirement is invalid")
        ArtifactRefV5("identity", self.prior_state_sha256)
        for item in (self.attempt_ref, self.preopen_snapshot_ref, self.cleanup_evidence_ref):
            if type(item) is not ArtifactRefV5:
                raise ValueError("qualification retirement authority is absent")
        if self.status == "completed":
            if (
                type(self.baseline_evidence_ref) is not ArtifactRefV5
                or type(self.candidate_evidence_ref) is not ArtifactRefV5
            ):
                raise ValueError("completed retirement requires both evidence references")
        elif self.baseline_evidence_ref is not None or self.candidate_evidence_ref is not None:
            raise ValueError("failed retirement cannot carry evaluation metrics")


def _domain(attempt):
    return canonical_sha256_v5(
        {
            "schema_version": 5,
            "domain": "pit-optimizer-v5-stage-retirement-v1",
            "stage": "qualification",
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
        snapshot.stage != "qualification"
        or snapshot.record_count != 1
        or snapshot.retired_security_lineage_ids
        or snapshot.ledger_head_sha256 != value["record_sha256"]
    ):
        raise ValueError("qualification ledger snapshot is not unused genesis")
    return canonical_json_bytes_v5(value) + b"\n"


class QualificationLedgerV5:
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
                    "record_type": "qualification_opened",
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
            raise ValueError("qualification ledger changed or belongs to another attempt")
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
                or value["record_type"] != "qualification_terminal"
                or value["prior_state_sha256"] != hashlib.sha256(prefix).hexdigest()
                or canonical_json_bytes_v5(value) + b"\n" != suffix
            ):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ValueError("qualification ledger terminal chain is invalid") from None
        return "retired", ref

    def open(self):
        if self.state()[0] != "unused":
            raise ValueError("qualification domain has already been opened")
        self.repository.append_stage_ledger(
            self.attempt.retirement_ledger.relative_path,
            prior=self.initial,
            event=self.opened,
        )

    def retire(self, terminal_ref):
        state, existing = self.state()
        if state == "retired":
            if existing != terminal_ref:
                raise ValueError("qualification domain already has another terminal")
            return
        if state != "opened":
            raise ValueError("qualification cannot retire without an opening")
        prefix = self.initial + self.opened
        event = (
            canonical_json_bytes_v5(
                {
                    "schema_version": 5,
                    "record_type": "qualification_terminal",
                    "terminal_ref": terminal_ref,
                    "prior_state_sha256": hashlib.sha256(prefix).hexdigest(),
                }
            )
            + b"\n"
        )
        self.repository.append_stage_ledger(self.attempt.retirement_ledger.relative_path, prior=prefix, event=event)


@dataclass(frozen=True, slots=True)
class QualificationInputsV5(confirmed.ConfirmationInputsV5):
    attempt: QualificationAttemptCommitmentV5
    confirmation_plan: ConfirmationPanelPlanV5


def _confirmed_outcome(repository, outcome_ref):
    """Recompute eligibility and require the exact permanent confirmation chain."""
    from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5

    outcome = _load(repository, outcome_ref, ConfirmationOutcomeV5)
    if outcome.status != "completed" or not outcome.eligible_to_request_qualification:
        raise ValueError("qualification requires eligible completed confirmation")
    attempt = _load(repository, outcome.attempt_ref, ConfirmationAttemptCommitmentV5)
    inputs = confirmed._inputs(repository, outcome.attempt_ref, attempt)
    snapshot = _load(repository, attempt.retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    if (
        snapshot.retirement_domain_id != attempt.retirement_domain_id
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
    ):
        raise ValueError("confirmation snapshot differs from its attempt")
    ledger = confirmed.ConfirmationLedgerV5(repository, outcome.attempt_ref, attempt, snapshot)
    state, terminal_ref = ledger.state()
    if state != "retired" or terminal_ref != outcome.retirement_terminal_ref:
        raise ValueError("confirmation has no matching permanent retirement")
    terminal = _load(repository, terminal_ref, confirmed.ConfirmationRetirementV5)
    _check_terminal(ledger, terminal)
    plan = confirmed._panel(repository, inputs, snapshot)
    recomputed = confirmed._result(repository, inputs, terminal_ref, terminal, plan)
    if outcome != recomputed or not recomputed.eligible_to_request_qualification:
        raise ValueError("confirmation eligibility differs from authenticated evidence")
    return inputs, plan


def _bindings(attempt, original):
    for name in (
        "pit_bundle_ref",
        "prices_provenance_ref",
        "execution_profile_ref",
        "evaluator_contract_ref",
        "scenario_grid_ref",
        "baseline_authority_ref",
        "sandbox_profile_ref",
    ):
        if getattr(attempt, name) != getattr(original.attempt, name):
            raise ValueError("qualification shared identity differs from confirmation")
    if (
        attempt.confirmed_policy_ref != original.selection.policy_ref
        or attempt.qualification_plan_ref.sha256 != original.discovery.qualification_plan_sha256
        or attempt.target != original.manifest.target
        or _domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("qualification candidate, plan, target or domain differs")


def build_qualification_attempt(
    *,
    repository,
    confirmation_outcome_ref: ArtifactRefV5,
    qualification_plan_ref: ArtifactRefV5,
    retirement_ledger: RetirementLedgerLocatorV5,
    attempt_id: str,
    output_path: str,
    operator_approved: bool,
) -> ArtifactRefV5:
    """Commit the exact eligible candidate while qualification bytes remain opaque."""
    from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
    from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5

    if operator_approved is not True:
        raise ValueError("qualification requires a separate affirmative operator decision")
    original, _ = _confirmed_outcome(repository, confirmation_outcome_ref)
    # As in Task 6, a checkpoint counter is not discovery closure authority.
    authorities = authenticate_campaign_manifest_v5(
        repository=repository, manifest_ref=original.attempt.discovery_manifest_ref
    )
    champion, _ = repository.load_confirmation_champion(
        checkpoint_ref=original.selection.checkpoint_ref,
        archive_ref=original.selection.archive_ref,
        finalized_campaign_ref=original.selection.finalized_campaign_ref,
        discovery_manifest_ref=original.attempt.discovery_manifest_ref,
    )
    if (
        champion.policy_revision != original.candidate_policy
        or champion.source_bundle_ref != original.selection.source_ref
        or champion.experiment_record_ref != original.selection.experiment_ref
        or authorities.manifest != original.manifest
    ):
        raise ValueError("confirmation candidate differs from reducer-authenticated final champion")
    snapshot = _load(repository, retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    source = original.attempt
    attempt = QualificationAttemptCommitmentV5(
        5,
        attempt_id,
        qualification_plan_ref,
        confirmation_outcome_ref,
        original.selection.policy_ref,
        source.pit_bundle_ref,
        source.prices_provenance_ref,
        source.execution_profile_ref,
        source.evaluator_contract_ref,
        source.scenario_grid_ref,
        source.baseline_authority_ref,
        source.sandbox_profile_ref,
        snapshot.retirement_domain_id,
        retirement_ledger,
        original.manifest.target,
    )
    _bindings(attempt, original)
    # Authenticate the sealed bytes without asking the repository to parse them.
    repository.authenticate_raw_artifact(qualification_plan_ref)
    if snapshot.ledger_relative_path != retirement_ledger.relative_path:
        raise ValueError("qualification ledger locator differs from immutable snapshot")
    with repository.adapter_state_transition(namespace="qualification", key=attempt.retirement_domain_id):
        if repository.read_stage_ledger(retirement_ledger.relative_path) != _genesis(snapshot):
            raise ValueError("qualification ledger changed or was already used")
        return repository.create_typed_artifact(output_path, attempt)


def _inputs(repository, attempt_ref, attempt):
    original, confirmation_plan = _confirmed_outcome(repository, attempt.confirmation_outcome_ref)
    _bindings(attempt, original)
    selection = original.selection
    _walk(
        repository,
        (attempt_ref,),
        opaque=(
            attempt.qualification_plan_ref,
            original.attempt.confirmation_plan_ref,
            selection.checkpoint_ref,
            selection.archive_ref,
            selection.experiment_ref,
            selection.finalized_campaign_ref,
        ),
        raw=(attempt.pit_bundle_ref, attempt.prices_provenance_ref),
    )
    values = {item.name: getattr(original, item.name) for item in fields(original)}
    values.update(attempt_ref=attempt_ref, attempt=attempt, confirmation_plan=confirmation_plan)
    return QualificationInputsV5(**values)


def _panel(repository, inputs, snapshot):
    plan = _load(repository, inputs.attempt.qualification_plan_ref, QualificationPanelPlanV5)
    # Now that the ledger is durably open, authenticate every newly visible child.
    _walk(
        repository,
        (inputs.attempt.qualification_plan_ref,),
        raw=(inputs.attempt.pit_bundle_ref, inputs.attempt.prices_provenance_ref),
    )
    panel = repository.load_evaluation_panel_spec(plan.episode.panel_ref)
    validate_episode_plan_panel_v5(plan.episode, panel)
    previous = (
        inputs.discovery.mechanics,
        inputs.discovery.quick,
        *inputs.discovery.discovery,
        inputs.confirmation_plan.episode,
    )
    if (
        plan.pit_bundle_sha256 != inputs.attempt.pit_bundle_ref.sha256
        or plan.partition_seed_sha256 != inputs.discovery.partition_seed_sha256
        or plan.target != inputs.attempt.target
        or plan.qualification_retirement_domain_id != inputs.attempt.retirement_domain_id
        or plan.qualification_ledger_snapshot_sha256 != snapshot.ledger_head_sha256
        or plan.episode.purpose != "qualification"
        or panel.purpose != "qualification"
        or {affiliation for lineage in panel.lineages for affiliation in lineage.source_affiliations}
        != {"sp500", "nasdaq100", "russell2000"}
        or max(item.end_date for item in previous) >= plan.episode.start_date
        or any(set(item.lineage_ids) & set(plan.episode.lineage_ids) for item in previous)
    ):
        raise ValueError("opened qualification differs from its sealed owner or separation")
    return plan


def _result(repository, inputs, terminal_ref, terminal, plan):
    cleanup = _load(repository, terminal.cleanup_evidence_ref, QualificationCleanupV5)
    if cleanup.attempt_ref != inputs.attempt_ref or terminal.attempt_ref != inputs.attempt_ref:
        raise ValueError("qualification terminal has foreign authority")
    if terminal.status != "completed":
        return _failed_outcome(inputs.attempt_ref, inputs.attempt, terminal_ref, terminal)
    if not cleanup.source_unchanged or not cleanup.cleanup_complete:
        raise ValueError("completed qualification lacks verified source and cleanup")
    baseline = _load(repository, terminal.baseline_evidence_ref, PanelEvaluationV5)
    candidate = _load(repository, terminal.candidate_evidence_ref, PanelEvaluationV5)
    baseline_cagr = _evidence(inputs, plan, baseline, inputs.baseline_policy)
    candidate_cagr = _evidence(inputs, plan, candidate, inputs.candidate_policy)
    if selected_scenario(candidate).report.closed_trades <= 0:
        raise ValueError("completed qualification candidate is behaviorally inactive")
    target_reached = candidate_cagr >= inputs.attempt.target.target_pct
    baseline_beaten = candidate_cagr > baseline_cagr
    return QualificationOutcomeV5(
        5,
        inputs.attempt_ref,
        "completed",
        inputs.attempt.confirmed_policy_ref,
        terminal.baseline_evidence_ref,
        terminal.candidate_evidence_ref,
        inputs.attempt.target.target_pct,
        baseline_cagr,
        candidate_cagr,
        candidate_cagr - baseline_cagr,
        target_reached,
        baseline_beaten,
        target_reached and baseline_beaten,
        terminal_ref,
        terminal.cleanup_evidence_ref,
        0,
    )


def _failed_outcome(attempt_ref, attempt, terminal_ref, terminal):
    if terminal.status == "completed":
        raise ValueError("completed qualification requires its authenticated evidence graph")
    return QualificationOutcomeV5(
        5,
        attempt_ref,
        terminal.status,
        attempt.confirmed_policy_ref,
        None,
        None,
        attempt.target.target_pct,
        None,
        None,
        None,
        False,
        False,
        False,
        terminal_ref,
        terminal.cleanup_evidence_ref,
        0,
    )


@dataclass(frozen=True, slots=True)
class QualificationRecoveryInputsV5:
    """Only immutable ownership/configuration; no panel or eligibility evidence."""

    attempt_ref: ArtifactRefV5
    attempt: QualificationAttemptCommitmentV5
    manifest: CampaignManifestV5
    evaluator: EvaluatorContractV5
    sandbox: SandboxProfileV5
    adapter: confirmed.ConfirmationAdapterConfigV5


def _recovery_inputs(repository, attempt_ref, attempt):
    # This ancestry supplies host ownership, not permission to evaluate. Never
    # parse panels, candidate source, discovery memory or terminal metric evidence.
    outcome = _load(repository, attempt.confirmation_outcome_ref, ConfirmationOutcomeV5)
    prior = _load(repository, outcome.attempt_ref, ConfirmationAttemptCommitmentV5)
    manifest = _load(repository, prior.discovery_manifest_ref, CampaignManifestV5)
    adapter = _load(repository, prior.execution_adapter_ref, confirmed.ConfirmationAdapterConfigV5)
    evaluator = _load(repository, attempt.evaluator_contract_ref, EvaluatorContractV5)
    sandbox = _load(repository, attempt.sandbox_profile_ref, SandboxProfileV5)
    for name in (
        "pit_bundle_ref",
        "prices_provenance_ref",
        "execution_profile_ref",
        "evaluator_contract_ref",
        "scenario_grid_ref",
        "baseline_authority_ref",
        "sandbox_profile_ref",
    ):
        if getattr(attempt, name) != getattr(prior, name):
            raise ValueError("qualification recovery has foreign shared ownership")
    if (
        outcome.confirmed_policy_ref != attempt.confirmed_policy_ref
        or prior.discovery_champion_policy_ref != attempt.confirmed_policy_ref
        or manifest.evaluator_contract_ref != attempt.evaluator_contract_ref
        or manifest.sandbox_profile_ref != attempt.sandbox_profile_ref
        or manifest.execution_profile_ref != attempt.execution_profile_ref
        or manifest.baseline_authority_ref != attempt.baseline_authority_ref
        or manifest.target != attempt.target
        or evaluator.sandbox_profile_sha256 != sandbox.sha256
        or evaluator.execution_profile_sha256 != attempt.execution_profile_ref.sha256
        or evaluator.pit_bundle_sha256 != attempt.pit_bundle_ref.sha256
        or evaluator.prices_provenance_sha256 != attempt.prices_provenance_ref.sha256
        or adapter.discovery_manifest_ref != prior.discovery_manifest_ref
        or adapter.repository_root_identity_sha256 != repository.root_identity_sha256
        or _domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("qualification recovery ownership is inconsistent")
    validate_sandbox_profile_resources_v5(sandbox, manifest.resources)
    return QualificationRecoveryInputsV5(attempt_ref, attempt, manifest, evaluator, sandbox, adapter)


def _recover_cleanup(repository, attempt_ref, attempt, recovery_factory):
    try:
        inputs = _recovery_inputs(repository, attempt_ref, attempt)
        factory = LocalQualificationRecoveryV5 if recovery_factory is None else recovery_factory
        cleanup = factory(repository, inputs).close(inputs, recovered=True)
        if type(cleanup) is not QualificationCleanupV5 or cleanup.attempt_ref != attempt_ref or not cleanup.recovered:
            raise ValueError("qualification recovery cleanup has foreign authority")
        return cleanup
    except BaseException:
        return QualificationCleanupV5(5, attempt_ref, False, False, True)


def _retry_retired_cleanup(repository, attempt_ref, attempt, cleanup, recovery_factory):
    """Preserve terminal evidence and publish successful cleanup separately."""
    if cleanup.attempt_ref != attempt_ref:
        raise ValueError("qualification cleanup belongs to another attempt")
    if cleanup.cleanup_complete:
        return cleanup
    path = f"qualification/{attempt.sha256}/recovery-cleanup.json"
    previous = repository.load_qualification_record(path, value_type=QualificationCleanupV5)
    if previous is not None:
        recovered = previous[1]
        if (
            recovered.attempt_ref != attempt_ref
            or not recovered.cleanup_complete
            or not recovered.recovered
            or (recovered.source_unchanged and not cleanup.source_unchanged)
        ):
            raise ValueError("qualification supplemental cleanup is invalid")
        return recovered
    recovered = _recover_cleanup(repository, attempt_ref, attempt, recovery_factory)
    recovered = replace(recovered, source_unchanged=cleanup.source_unchanged and recovered.source_unchanged)
    if recovered.cleanup_complete:
        repository.create_typed_artifact(path, recovered)
    return recovered


def qualification_cleanup_evidence_v5(repository, outcome):
    """Read current cleanup proof without rewriting the immutable outcome."""
    cleanup = _load(repository, outcome.cleanup_evidence_ref, QualificationCleanupV5)
    if cleanup.attempt_ref != outcome.attempt_ref:
        raise ValueError("qualification outcome has foreign cleanup")
    if cleanup.cleanup_complete:
        return cleanup
    attempt = _load(repository, outcome.attempt_ref, QualificationAttemptCommitmentV5)
    previous = repository.load_qualification_record(
        f"qualification/{attempt.sha256}/recovery-cleanup.json", value_type=QualificationCleanupV5
    )
    if previous is None:
        return cleanup
    recovered = previous[1]
    if (
        recovered.attempt_ref != outcome.attempt_ref
        or not recovered.cleanup_complete
        or not recovered.recovered
        or (recovered.source_unchanged and not cleanup.source_unchanged)
    ):
        raise ValueError("qualification supplemental cleanup is invalid")
    return recovered


def run_qualification(
    *, repository, attempt_ref: ArtifactRefV5, worker_factory=None, recovery_factory=None
) -> ArtifactRefV5:
    """Open once, retire every terminal path, and recover crashes without reevaluation."""
    from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5

    attempt = _load(repository, attempt_ref, QualificationAttemptCommitmentV5)
    snapshot = _load(repository, attempt.retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    if (
        snapshot.retirement_domain_id != attempt.retirement_domain_id
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
        or _domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("qualification attempt ledger authority is inconsistent")
    prefix = f"qualification/{attempt.sha256}"
    # This lock is retained through evaluation. A second controller cannot
    # mistake a live opened attempt for an abandoned controller's work.
    with repository.adapter_state_transition(namespace="qualification", key=attempt.retirement_domain_id):
        ledger = QualificationLedgerV5(repository, attempt_ref, attempt, snapshot)
        state, terminal_ref = ledger.state()
        if state == "retired":
            terminal = _load(repository, terminal_ref, QualificationRetirementV5)
            _check_terminal(ledger, terminal)
            if terminal.status == "completed":
                inputs = _inputs(repository, attempt_ref, attempt)
                plan = _panel(repository, inputs, snapshot)
                outcome = _result(repository, inputs, terminal_ref, terminal, plan)
            else:
                cleanup = _load(repository, terminal.cleanup_evidence_ref, QualificationCleanupV5)
                _retry_retired_cleanup(repository, attempt_ref, attempt, cleanup, recovery_factory)
                outcome = _failed_outcome(attempt_ref, attempt, terminal_ref, terminal)
            return repository.create_typed_artifact(prefix + "/outcome.json", outcome)
        recovered = state == "opened"
        inputs = None
        if not recovered:
            inputs = _inputs(repository, attempt_ref, attempt)
            ledger.open()
        else:
            prepared = repository.load_qualification_record(
                prefix + "/terminal.json", value_type=QualificationRetirementV5
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
                    cleanup = _load(repository, terminal.cleanup_evidence_ref, QualificationCleanupV5)
                    _retry_retired_cleanup(repository, attempt_ref, attempt, cleanup, recovery_factory)
                    outcome = _failed_outcome(attempt_ref, attempt, terminal_ref, terminal)
                return repository.create_typed_artifact(prefix + "/outcome.json", outcome)
        worker, plan = None, None
        status, baseline_ref, candidate_ref = "failed", None, None
        cleanup = QualificationCleanupV5(5, attempt_ref, False, False, recovered)
        try:
            if recovered:
                cleanup = _recover_cleanup(repository, attempt_ref, attempt, recovery_factory)
            else:
                worker = (LocalQualificationWorkerV5 if worker_factory is None else worker_factory)(repository, inputs)
                plan = _panel(repository, inputs, snapshot)
                baseline = worker.evaluate(inputs, plan, baseline=True)
                _evidence(inputs, plan, baseline, inputs.baseline_policy)
                baseline_ref = repository.create_typed_artifact(prefix + "/baseline.json", baseline)
                candidate = worker.evaluate(inputs, plan, baseline=False)
                _evidence(inputs, plan, candidate, inputs.candidate_policy)
                candidate_ref = repository.create_typed_artifact(prefix + "/candidate.json", candidate)
                if selected_scenario(candidate).report.closed_trades <= 0:
                    raise ValueError("qualification candidate is behaviorally inactive")
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
                    if type(cleanup) is not QualificationCleanupV5 or cleanup.attempt_ref != attempt_ref:
                        raise ValueError("qualification cleanup has foreign authority")
                except BaseException:
                    cleanup = QualificationCleanupV5(5, attempt_ref, False, False, recovered)
            elif not recovered:
                # Constructor failures must not disable cleanup of a previously
                # reserved deterministic slot.
                cleanup = _recover_cleanup(repository, attempt_ref, attempt, recovery_factory)
            if not cleanup.source_unchanged or not cleanup.cleanup_complete:
                status = "failed"
            if status != "completed":
                baseline_ref = candidate_ref = None
            previous_cleanup = repository.load_qualification_record(
                prefix + "/cleanup.json", value_type=QualificationCleanupV5
            )
            if previous_cleanup is None:
                cleanup_ref = repository.create_typed_artifact(prefix + "/cleanup.json", cleanup)
            else:
                cleanup_ref, cleanup = previous_cleanup
                if cleanup.attempt_ref != attempt_ref:
                    raise ValueError("recovered cleanup is foreign")
            terminal = QualificationRetirementV5(
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


class LocalQualificationWorkerV5:
    """Concrete disposable Git worker plus the existing network-absent Docker evaluator."""

    def __init__(self, repository, inputs):
        from core.pit_optimizer_v5.manifest import capture_clean_policy_snapshot_v5
        from core.pit_optimizer_v5.sandbox import DockerPanelEvaluatorV5

        config = inputs.adapter
        self.source_snapshot = capture_clean_policy_snapshot_v5(
            source_root=Path(config.source_root),
            git_executable=Path(config.git_executable),
            expected_source_commit=inputs.manifest.source_commit,
        )
        self._initialize_resources(repository, inputs)
        self.evaluator = DockerPanelEvaluatorV5(executor=self.executor, clock=self)
        self.before = self.materializer._read_source_bundle()
        if self.before != inputs.baseline_source:
            raise ValueError("qualification source checkout differs from unchanged baseline")

    def _initialize_resources(self, repository, inputs):
        """Reconstruct authenticated resource handles without inspecting source cleanliness."""
        from core.pit_optimizer_v5.production_workspace import LocalGitWorkspaceDriverV5
        from core.pit_optimizer_v5.production_sandbox import LocalSandboxMountFactoryV5, LocalContainerExecutorV5
        from core.pit_optimizer_v5.workspace import WorkspaceOwnerV5, WorkspaceRootsV5, GitCandidateMaterializerV5

        self.repository, self.inputs = repository, inputs
        config = inputs.adapter
        self.owner = WorkspaceOwnerV5(
            inputs.manifest.campaign_id,
            inputs.manifest.search.max_feedback_rounds,
            config.owner_token_sha256,
            "pit-v5-qualify-" + inputs.attempt.sha256,
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
        self.leases, self.workspace_leases = [], []
        self.role = None

    @staticmethod
    def monotonic():
        return time.monotonic()

    def _token(self):
        if self.role not in {"baseline", "candidate"}:
            raise ValueError("qualification worker policy is not frozen")
        return canonical_sha256_v5((self.inputs.attempt.sha256, self.role))

    def register_execution(self, authority, leases):
        self.leases.extend(leases)
        self.repository.create_typed_artifact(
            f"qualification/{self.inputs.attempt.sha256}/{self.role}-execution.json", authority
        )

    def evaluate(self, inputs, plan, *, baseline):
        from core.pit_optimizer_v5.memory import CandidateExecutionKeyV5
        from core.pit_optimizer_v5.runtime import StageDeadlineV5
        from core.pit_optimizer_v5.sandbox import DockerPanelRequestV5

        if inputs is not self.inputs:
            raise ValueError("qualification worker inputs changed")
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
            raise ValueError("qualification sandbox evaluation failed")
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
                record = self.repository.load_qualification_record(
                    f"qualification/{inputs.attempt.sha256}/{role}-execution.json",
                    value_type=CandidateExecutionAuthorityV5,
                )
                if recovered and record is not None:
                    authority = record[1]
                    if (
                        authority.key.stage != "confirmation_evaluation"
                        or authority.key.episode_ordinal is not None
                        or authority.key.experiment_id != canonical_sha256_v5((inputs.attempt.sha256, role))
                    ):
                        raise ValueError("qualification cleanup execution belongs to another policy slot")
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
                    raise TimeoutError("qualification cleanup deadline exceeded")
                self.materializer.cleanup(owner=self.owner, lease=lease)
            except BaseException:
                complete = False
        try:
            source = self.materializer._read_source_bundle()
            snapshot = capture_clean_policy_snapshot_v5(
                source_root=Path(inputs.adapter.source_root),
                git_executable=Path(inputs.adapter.git_executable),
                expected_source_commit=inputs.manifest.source_commit,
            )
            unchanged = source.sha256 == inputs.evaluator.baseline_source_bundle_sha256 and (
                self.source_snapshot is None or snapshot == self.source_snapshot
            )
        except BaseException:
            unchanged = False
        return QualificationCleanupV5(
            5, inputs.attempt_ref, unchanged, complete and time.monotonic() < cleanup_deadline, recovered
        )


class LocalQualificationRecoveryV5(LocalQualificationWorkerV5):
    """Cleanup-only handles: no evaluator, materialization or clean-source prerequisite."""

    def __init__(self, repository, inputs):
        self.source_snapshot = None
        self._initialize_resources(repository, inputs)

    def evaluate(self, inputs, plan, *, baseline):
        raise RuntimeError("qualification recovery cannot evaluate")


def qualification_evidence_summary_v5(repository, outcome):
    """Project aggregate evidence only; no lineage IDs, source, paths or raw content."""
    if type(outcome) is not QualificationOutcomeV5:
        raise ValueError("qualification summary requires a closed outcome")
    if outcome.status != "completed":
        return {}
    result = {}
    for role, ref in (("baseline", outcome.baseline_evidence_ref), ("candidate", outcome.candidate_evidence_ref)):
        evaluation = _load(repository, ref, PanelEvaluationV5)
        report = selected_scenario(evaluation).report
        result[role] = {
            "closed_trades": report.closed_trades,
            "average_exposure_pct": report.average_exposure_pct,
            "max_drawdown_pct": report.max_drawdown_pct,
            "total_friction_usd": report.total_friction_usd,
            "friction_drag_pct": report.friction_drag_pct,
            "episode_metrics": tuple(item.metrics for item in report.episode_slices),
            "calendar_year_metrics": tuple(item.metrics for item in report.calendar_year_slices),
        }
    return result


__all__ = ["build_qualification_attempt", "run_qualification", "qualification_evidence_summary_v5"]
