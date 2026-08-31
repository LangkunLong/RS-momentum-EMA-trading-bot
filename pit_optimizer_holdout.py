"""Run one provider-free PIT optimizer holdout from an authenticated discovery winner."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import replace
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Callable, Mapping, Sequence

from core.pit_optimizer_holdout import HoldoutProgressJournal, load_discovery_winner_evidence


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_POLICY_WORKER_SESSION_OUTPUT_BYTES = 32 * 1024 * 1024
_POLICY_WORKER_BASE_FOLD_SESSIONS = 60


class HoldoutPreflightError(RuntimeError):
    """A closed holdout precondition failed before or during local evaluation."""


def _hidden_validation_was_opened(state: object | None) -> bool:
    """Read the controller's one-shot exposure flag after an interrupted evaluation."""
    return bool(getattr(state, "hidden_validation_opened", False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pit_optimizer_holdout.py",
        description=(
            "Authenticate one completed local PIT discovery canary and run its "
            "provider-free hidden evaluation in a disposable Docker worker."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--permanent-runtime-root", type=Path, required=True)
    parser.add_argument("--controller-temp-parent", type=Path, required=True)
    parser.add_argument("--holdout-artifact-root", type=Path, required=True)
    parser.add_argument("--git-executable", type=Path, required=True)
    parser.add_argument("--docker-executable", type=Path, required=True)
    parser.add_argument("--optimizer-manifest", type=Path, required=True)
    parser.add_argument("--verified-parity", type=Path, required=True)
    parser.add_argument("--pit-bundle", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--discovery-artifact-root", type=Path, required=True)
    parser.add_argument("--discovery-summary-sha256", required=True)
    parser.add_argument("--child-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--wall-timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--output-limit-bytes", type=int, default=4 * 1024 * 1024)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--preflight",
        action="store_true",
        help="Authenticate and reconstruct the candidate without opening hidden validation.",
    )
    action.add_argument(
        "--execute",
        action="store_true",
        help="Open exactly one hidden validation after successful local preflight.",
    )
    return parser


def _regular_file(path: Path, label: str) -> Path:
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise HoldoutPreflightError(f"{label}_missing") from exc
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or reparse
        or not stat.S_ISREG(info.st_mode)
    ):
        raise HoldoutPreflightError(f"{label}_invalid")
    return candidate.resolve(strict=True)


def _directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise HoldoutPreflightError(f"{label}_missing") from exc
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or reparse
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise HoldoutPreflightError(f"{label}_invalid")
    return candidate.resolve(strict=True)


def _new_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.exists() or candidate.is_symlink():
        raise HoldoutPreflightError(f"{label}_already_exists")
    parent = _directory(candidate.parent, f"{label}_parent")
    candidate.mkdir(mode=0o700)
    resolved = _directory(candidate, label)
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise HoldoutPreflightError(f"{label}_escaped_parent") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_readiness(namespace: argparse.Namespace) -> object:
    """Recreate the prior canary's provider-free readiness identity without a gateway."""

    from core.pit_optimization import load_pit_optimizer_v3_readiness
    from core.pit_optimization_contract import PitOptimizerGateConfig
    from core.pit_optimizer_controller import _load_manifest

    manifest_path = _regular_file(namespace.optimizer_manifest, "optimizer_manifest")
    manifest = _load_manifest(manifest_path)
    discovery_root = _directory(namespace.discovery_artifact_root, "discovery_artifact_root")
    readiness_path = _regular_file(
        discovery_root.parent / f"{manifest.run_id}.readiness.json",
        "optimizer_readiness",
    )
    baseline_run = _directory(namespace.baseline_run, "baseline_run")
    baseline_manifest = _regular_file(
        baseline_run / "run_manifest.json", "baseline_manifest"
    )
    pit_bundle = _regular_file(namespace.pit_bundle, "pit_bundle")
    parity = _regular_file(namespace.verified_parity, "verified_parity")
    config = PitOptimizerGateConfig(
        phase="canary",
        baseline_run=baseline_run,
        baseline_manifest_sha256=_sha256_file(baseline_manifest),
        pit_bundle=pit_bundle,
        pit_bundle_sha256=_sha256_file(pit_bundle),
        effective_policy_sha256=manifest.effective_policy_sha256,
        optimizer_manifest=manifest_path,
        optimizer_manifest_sha256=manifest.sha256,
        verified_parity_artifact=parity,
        verified_parity_sha256=_sha256_file(parity),
        readiness_artifact=readiness_path,
        readiness_sha256=_sha256_file(readiness_path),
        authorization_window_id=manifest.authorization_requirement.window_id,
        authorization_requirement_sha256=manifest.authorization_requirement.sha256,
        # The existing canary identity records a past authorized transmission.
        # This local-only runner never creates a provider gateway or request.
        source_transmission_authorized=True,
        max_api_calls=manifest.authorization_requirement.max_calls,
        max_tokens=manifest.authorization_requirement.max_tokens,
        max_iterations=manifest.max_iterations,
        apply=False,
    )
    try:
        readiness = load_pit_optimizer_v3_readiness(config)
    except ValueError as exc:
        raise HoldoutPreflightError("optimizer_readiness_invalid") from exc
    if readiness.manifest.sha256 != manifest.sha256:
        raise HoldoutPreflightError("optimizer_manifest_identity_invalid")
    return readiness


def _authenticate_source(*, readiness: object, repo_root: Path, source_state: object, git: object) -> None:
    from agent_loop import _pit_optimizer_source_identity, recheck_source_unchanged
    from core.pit_optimization_contract import _committed_policy_source_text

    manifest = getattr(readiness, "manifest", None)
    if manifest is None:
        raise HoldoutPreflightError("optimizer_readiness_invalid")
    source_identity = _pit_optimizer_source_identity(repo_root, git)
    if (
        getattr(source_state, "head", None) != manifest.source_head
        or source_identity
        != (manifest.source_head, manifest.source_fingerprint_sha256)
        or recheck_source_unchanged(source_state).source_modified
    ):
        raise HoldoutPreflightError("source_identity_drift")
    for relative, expected in manifest.policy_source_sha256s:
        source_text = _committed_policy_source_text(repo_root, relative)
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != expected:
            raise HoldoutPreflightError("source_policy_identity_drift")


def _build_hidden_evaluator(
    *,
    readiness: object,
    repo_root: Path,
    pit_bundle: Path,
    baseline_run: Path,
    docker_executable: Path,
    controller_root: Path,
    permanent_runtime_root: Path,
    child_timeout_seconds: float,
    output_limit_bytes: int,
    wall_deadline: float,
    progress: HoldoutProgressJournal,
) -> Callable[[object, object, object], object]:
    from agent_loop import PolicyWorkerRunner, configure_docker_executable, _canonical_json_bytes
    from core.backtest_engine import PortfolioSimulator
    from core.pit_data import PITDataBundle
    from core.pit_optimization import _build_verification_scope
    from core.pit_optimizer_evaluation import (
        HiddenEvaluation,
        HiddenEvaluationAttestation,
        HiddenResetReceipt,
        HoldoutDecision,
    )
    from core.pit_policy_parity import ParityFoldEvidence, build_fold_evidence
    from core.strategy_policy.contracts import CapacitySnapshot
    from core.strategy_policy.worker import PolicyDeterminismProbe

    manifest = getattr(readiness, "manifest", None)
    if manifest is None:
        raise HoldoutPreflightError("optimizer_readiness_invalid")
    with PITDataBundle(pit_bundle, expected_sha256=manifest.pit_bundle_sha256) as bundle:
        scope = _build_verification_scope(bundle, baseline_run)
    universe = tuple(scope["symbols"])
    probes = (
        PolicyDeterminismProbe(
            "recommend_capacity",
            CapacitySnapshot(None, 25, 0, 3, 1.0, False),
            CapacitySnapshot(5, 25, 2, 1, 0.5, True),
        ),
    )
    docker = configure_docker_executable(
        docker_executable,
        source_root=repo_root,
        controller_root=controller_root,
        permanent_runtime_root=permanent_runtime_root,
    )
    hidden_fold_sessions = len(manifest.fold_manifest.hidden_fold.sessions)
    fold_scale = max(
        1,
        math.ceil(hidden_fold_sessions / _POLICY_WORKER_BASE_FOLD_SESSIONS),
    )
    session_output_limit_bytes = min(
        _MAX_POLICY_WORKER_SESSION_OUTPUT_BYTES,
        output_limit_bytes * fold_scale,
    )
    runner = PolicyWorkerRunner(
        image=manifest.sandbox_image,
        engine=docker,
        temp_parent=controller_root,
        fold_timeout_seconds=child_timeout_seconds,
        output_limit_bytes=session_output_limit_bytes,
        wall_deadline=wall_deadline,
    )
    worker_sequence = 0

    def require_wall_time() -> None:
        if time.monotonic() >= wall_deadline:
            raise TimeoutError("holdout evaluator wall deadline reached")

    def validate_evidence(evidence: object, fold: object) -> object:
        if (
            not isinstance(evidence, ParityFoldEvidence)
            or evidence.fold_id != getattr(fold, "fold_id", None)
            or evidence.effective_policy_sha256 != manifest.effective_policy_sha256
        ):
            raise HoldoutPreflightError("holdout_evidence_identity_invalid")
        return evidence

    def evaluate_baseline(fold: object) -> object:
        progress.publish("baseline_replay")
        require_wall_time()
        with PITDataBundle(pit_bundle, expected_sha256=manifest.pit_bundle_sha256) as bundle:
            simulator = PortfolioSimulator(
                pit_bundle=bundle,
                benchmark_symbol=manifest.fold_manifest.benchmark,
                signal_every_n_days=1,
            )
            result = simulator.run(
                list(universe),
                start_date=fold.start_date,
                end_date=fold.end_date,
                history_start_date=manifest.fold_manifest.warmup_start_date,
                benchmark_symbol=manifest.fold_manifest.benchmark,
            )
        require_wall_time()
        return validate_evidence(build_fold_evidence(fold=fold, result=result), fold)

    def evaluate_candidate(candidate_root: Path, fold: object, identity_sha256: str) -> object:
        nonlocal worker_sequence
        progress.publish("candidate_replay")
        require_wall_time()
        worker_sequence += 1
        factory = runner.client_factory(
            candidate_root=candidate_root,
            interface_version=manifest.policy_interface_version,
            fold_run_id=f"{fold.fold_id}-{worker_sequence:02d}",
            determinism_probes=probes,
        )
        with PITDataBundle(pit_bundle, expected_sha256=manifest.pit_bundle_sha256) as bundle:
            simulator = PortfolioSimulator(
                pit_bundle=bundle,
                benchmark_symbol=manifest.fold_manifest.benchmark,
                signal_every_n_days=1,
                policy_client_factory=factory,
            )
            result = simulator.run(
                list(universe),
                start_date=fold.start_date,
                end_date=fold.end_date,
                history_start_date=manifest.fold_manifest.warmup_start_date,
                benchmark_symbol=manifest.fold_manifest.benchmark,
            )
        require_wall_time()
        return validate_evidence(build_fold_evidence(fold=fold, result=result), fold)

    def reset_receipt(subject: str, identity_sha256: str) -> object:
        fold = manifest.fold_manifest.hidden_fold
        digest = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "fold_id": fold.fold_id,
                    "subject": subject,
                    "subject_identity_sha256": identity_sha256,
                    "reset": "fresh_simulator_and_policy_session",
                }
            )
        ).hexdigest()
        return HiddenResetReceipt(
            fold_id=fold.fold_id,
            subject=subject,
            subject_identity_sha256=identity_sha256,
            reset_receipt_sha256=digest,
        )

    def evaluate_hidden(workspace: object, identity: object, reservation: object) -> object:
        fold = manifest.fold_manifest.hidden_fold
        baseline = evaluate_baseline(fold)
        candidate = evaluate_candidate(
            workspace.root, fold, identity.identity_sha256
        )
        hidden_excess = candidate.aggregate.total_return_pct - baseline.aggregate.total_return_pct
        candidate_aggregate = replace(
            candidate.aggregate,
            excess_total_return_pp=hidden_excess,
        )
        decision = HoldoutDecision.from_result(
            excess_total_return_pp=hidden_excess,
            closed_trades=candidate_aggregate.closed_trades,
            safety_complete=True,
            integrity_complete=True,
            accounting_complete=True,
        )
        evaluation = HiddenEvaluation(
            baseline_aggregate=baseline.aggregate,
            candidate_aggregate=candidate_aggregate,
            decision=decision,
        )
        return HiddenEvaluationAttestation.issue(
            reservation_record_sha256=reservation.reservation_record_sha256,
            source_head=manifest.source_head,
            source_fingerprint_sha256=manifest.source_fingerprint_sha256,
            baseline_policy_sha256=manifest.effective_policy_sha256,
            candidate_identity_sha256=identity.identity_sha256,
            fold_id=fold.fold_id,
            baseline_reset=reset_receipt("baseline", manifest.effective_policy_sha256),
            candidate_reset=reset_receipt(
                "candidate", identity.identity_sha256
            ),
            evaluation=evaluation,
        )

    return evaluate_hidden


def _remove_empty(path: Path, *, parent: Path) -> bool:
    try:
        if path.parent != parent or path.is_symlink():
            return False
        if path.exists():
            path.rmdir()
        return not path.exists()
    except OSError:
        return False


def _write_summary(
    *,
    store: object,
    status: str,
    terminal: str,
    hidden: Mapping[str, object],
    candidate_reconstructed: bool,
    candidate_removed: bool,
    source_modified: bool,
    controller_resources_removed: bool,
    failure_stage: str | None,
    failure_kind: str | None,
) -> None:
    store.write_json_artifact(
        "summary.json",
        {
            "schema_version": 3,
            "stage": "provider_free_holdout",
            "status": status,
            "terminal": {"code": terminal, "exit_code": 0 if status != "aborted" else 1},
            "provider": {"api_calls": 0, "network": "disabled"},
            "candidate": {
                "reconstructed": candidate_reconstructed,
                "removed": candidate_removed,
            },
            "hidden_outcome": dict(hidden),
            "cleanup": {
                "complete": candidate_removed
                and not source_modified
                and controller_resources_removed,
                "source_modified": source_modified,
                "controller_resources_removed": controller_resources_removed,
            },
            "failure": (
                None
                if status != "aborted"
                else {"stage": failure_stage, "kind": failure_kind}
            ),
        },
    )


def _holdout_failure_kind(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, HoldoutPreflightError):
        return "preflight"
    names = {
        "AuditFailure": "audit",
        "EvidenceTampering": "integrity",
        "IdentityDrift": "identity",
        "SandboxError": "sandbox",
        "SandboxIntegrityFailure": "sandbox",
    }
    return names.get(type(error).__name__, "runtime")


def _holdout_failure_stage(journal: HoldoutProgressJournal | None) -> str:
    if journal is None:
        return "preflight"
    try:
        state = journal.read().get("state")
    except BaseException:
        return "preflight"
    if state in {"preflight", "baseline_replay", "candidate_replay", "finalizing"}:
        return str(state)
    return "preflight"


def _validate_limits(namespace: argparse.Namespace) -> None:
    if (
        not isinstance(namespace.discovery_summary_sha256, str)
        or _SHA256_RE.fullmatch(namespace.discovery_summary_sha256) is None
        or not isinstance(namespace.child_timeout_seconds, float)
        or not 0 < namespace.child_timeout_seconds <= 900.0
        or not isinstance(namespace.wall_timeout_seconds, float)
        or not namespace.child_timeout_seconds <= namespace.wall_timeout_seconds <= 7200.0
        or type(namespace.output_limit_bytes) is not int
        or not 1 <= namespace.output_limit_bytes <= 4 * 1024 * 1024
    ):
        raise HoldoutPreflightError("holdout_limits_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    from agent_loop import (
        configure_git_executable,
        dispose_candidate,
        export_candidate,
        preflight_source,
        recheck_source_unchanged,
    )
    from core.pit_optimizer_artifacts import IncrementalArtifactStore
    from core.pit_optimizer_candidate import validate_candidate_diff
    from core.pit_optimizer_controller import (
        CandidateWorkspace,
        PitOptimizerServices,
        _new_run_state,
        _record_artifact,
        _run_hidden_once,
        _window_identity,
    )
    from core.pit_optimizer_evaluation import ValidationExposureMetadata, ValidationLedger

    parser = build_parser()
    namespace = parser.parse_args(tuple(sys.argv[1:] if argv is None else argv))
    artifact_root: Path | None = None
    journal: HoldoutProgressJournal | None = None
    store: object | None = None
    source_state: object | None = None
    candidate: object | None = None
    state: object | None = None
    controller_root: Path | None = None
    candidate_parent: Path | None = None
    candidate_reconstructed = False
    candidate_removed = False
    source_modified = True
    controller_resources_removed = False
    hidden: dict[str, object] = {
        "opened": False,
        "completed": False,
        "long_replay_eligible": None,
    }
    status = "aborted"
    terminal = "preflight_failed"
    failure_stage: str | None = None
    failure_kind: str | None = None
    failure = False
    try:
        _validate_limits(namespace)
        repo_root = _directory(namespace.repo_root, "repo_root")
        runtime_root = _directory(
            namespace.permanent_runtime_root, "permanent_runtime_root"
        )
        temp_parent = _directory(
            namespace.controller_temp_parent, "controller_temp_parent"
        )
        artifact_root = _new_directory(namespace.holdout_artifact_root, "holdout_artifact_root")
        store = IncrementalArtifactStore(artifact_root)
        journal = HoldoutProgressJournal(root=artifact_root, controller_pid=os.getpid())
        journal.publish("preflight")
        nonce = secrets.token_hex(8)
        controller_root = _new_directory(
            temp_parent / f"pit-optimizer-holdout-{nonce}", "controller_root"
        )
        candidate_parent = _new_directory(
            controller_root / "candidates", "candidate_parent"
        )
        readiness = _load_readiness(namespace)
        manifest = readiness.manifest
        evidence = load_discovery_winner_evidence(
            root=_directory(namespace.discovery_artifact_root, "discovery_artifact_root"),
            expected_summary_sha256=namespace.discovery_summary_sha256,
            expected_role_calls=len(manifest.call_budgets),
        )
        if (
            evidence.manifest_sha256 != manifest.sha256
            or evidence.readiness_sha256 != readiness.readiness_sha256
        ):
            raise HoldoutPreflightError("discovery_artifact_identity_drift")
        git = configure_git_executable(_regular_file(namespace.git_executable, "git_executable"))
        source_state = preflight_source(
            repo_root,
            permanent_runtime_root=runtime_root,
            controller_temp_parent=candidate_parent,
            git=git,
        )
        _authenticate_source(
            readiness=readiness,
            repo_root=repo_root,
            source_state=source_state,
            git=git,
        )
        candidate = export_candidate(source_state, destination_parent=candidate_parent)
        candidate_reconstructed = True
        identity, cumulative_diff = validate_candidate_diff(
            authenticated_base_root=repo_root,
            candidate_root=candidate.root,
            incremental_diff=evidence.cumulative_diff,
            git=git,
            bounds=manifest.candidate_bounds,
            source_commit=manifest.source_head,
            policy_interface_version=manifest.policy_interface_version,
            immutable_constraints_sha256=manifest.immutable_constraints_sha256,
            discovery_manifest_sha256=manifest.fold_manifest.sha256,
        )
        if (
            json.loads(identity.to_canonical_json()) != evidence.incumbent
            or cumulative_diff != evidence.cumulative_diff
        ):
            raise HoldoutPreflightError("candidate_identity_reconstruction_mismatch")
        workspace = CandidateWorkspace(f"holdout_{nonce}", candidate.root)
        _record_artifact(
            _new_run_state(readiness),
            store.write_json_artifact(
                "run.json",
                {
                    "schema_version": 3,
                    "stage": "provider_free_holdout",
                    "provider_calls": 0,
                    "apply": False,
                    "discovery_run_id": evidence.run_id,
                },
            ),
        )
        if namespace.preflight:
            status = "preflight_completed"
            terminal = "preflight_completed"
        else:
            ledger = ValidationLedger(runtime_root / manifest.validation_ledger_name)
            deadline = time.monotonic() + namespace.wall_timeout_seconds
            evaluate_hidden = _build_hidden_evaluator(
                readiness=readiness,
                repo_root=repo_root,
                pit_bundle=_regular_file(namespace.pit_bundle, "pit_bundle"),
                baseline_run=_directory(namespace.baseline_run, "baseline_run"),
                docker_executable=_regular_file(namespace.docker_executable, "docker_executable"),
                controller_root=candidate_parent,
                permanent_runtime_root=runtime_root,
                child_timeout_seconds=namespace.child_timeout_seconds,
                output_limit_bytes=namespace.output_limit_bytes,
                wall_deadline=deadline,
                progress=journal,
            )

            def forbidden(*_args: object, **_kwargs: object) -> object:
                raise RuntimeError("provider_or_discovery_path_forbidden")

            state = _new_run_state(readiness)
            state.incumbent_workspace = workspace
            state.incumbent_identity = identity
            state.incumbent_cumulative_diff = cumulative_diff
            services = PitOptimizerServices(
                freeze_pricing=forbidden,
                open_run_lease=forbidden,
                close_run_lease=forbidden,
                call_role=forbidden,
                recover_role_attempt=forbidden,
                create_candidate=forbidden,
                validate_and_apply=forbidden,
                evaluate_discovery=forbidden,
                confirm_discovery=forbidden,
                reserve_hidden_validation=lambda incumbent: ledger.reserve_hidden(
                    _window_identity(manifest, 2),
                    ValidationExposureMetadata(
                        run_id=manifest.run_id,
                        source_head=manifest.source_head,
                        baseline_policy_sha256=manifest.effective_policy_sha256,
                        candidate_identity_sha256=incumbent.identity_sha256,
                        exposure_kind="hidden_validation",
                    ),
                ),
                evaluate_hidden=evaluate_hidden,
                record_hidden_outcome=lambda reservation, attempted, completed, failure_code: ledger.record_outcome(
                    reservation,
                    attempted=attempted,
                    completed=completed,
                    failure_code=failure_code,
                ),
                dispose_candidate=forbidden,
                verify_inputs=forbidden,
                cancellation_requested=lambda: False,
                prepare_iteration_artifacts=forbidden,
                write_json_artifact=store.write_json_artifact,
                write_diff_artifact=forbidden,
            )
            result = _run_hidden_once(readiness, state, services)
            journal.publish("finalizing")
            hidden = {
                "opened": True,
                "completed": True,
                "excess_total_return_pp": str(result.decision.excess_total_return_pp),
                "closed_trades": result.decision.closed_trades,
                "long_replay_eligible": result.decision.long_replay_eligible,
            }
            status = "holdout_completed"
            terminal = "holdout_completed"
    except BaseException as exc:
        failure = True
        status = "aborted"
        failure_stage = _holdout_failure_stage(journal)
        failure_kind = _holdout_failure_kind(exc)
        if _hidden_validation_was_opened(state):
            hidden["opened"] = True
        terminal = "hidden_validation_failed" if hidden["opened"] else "preflight_failed"
    finally:
        if candidate is not None:
            try:
                dispose_candidate(candidate)
                candidate_removed = not candidate.root.exists()
            except BaseException:
                candidate_removed = False
        else:
            candidate_removed = True
        if source_state is not None:
            try:
                source_modified = recheck_source_unchanged(source_state).source_modified
            except BaseException:
                source_modified = True
            try:
                source_state.close()
            except BaseException:
                source_modified = True
        if candidate_parent is not None and controller_root is not None:
            controller_resources_removed = _remove_empty(
                candidate_parent, parent=controller_root
            ) and _remove_empty(controller_root, parent=controller_root.parent)
        if (
            status != "aborted"
            and (not candidate_removed or source_modified or not controller_resources_removed)
        ):
            status = "aborted"
            terminal = "cleanup_failed"
            failure_stage = "finalizing"
            failure_kind = "cleanup"
            failure = True
        if store is not None:
            try:
                _write_summary(
                    store=store,
                    status=status,
                    terminal=terminal,
                    hidden=hidden,
                    candidate_reconstructed=candidate_reconstructed,
                    candidate_removed=candidate_removed,
                    source_modified=source_modified,
                    controller_resources_removed=controller_resources_removed,
                    failure_stage=failure_stage,
                    failure_kind=failure_kind,
                )
            except BaseException:
                status = "aborted"
                terminal = "artifact_finalization_failed"
                failure = True
        if journal is not None:
            try:
                journal.publish("completed" if status != "aborted" else "failed")
            except BaseException:
                status = "aborted"
                terminal = "progress_finalization_failed"
                failure = True
        public = {
            "status": status,
            "terminal": terminal,
            "holdout_completed": hidden["completed"],
            "long_replay_eligible": hidden["long_replay_eligible"],
            "excess_total_return_pp": hidden.get("excess_total_return_pp"),
            "closed_trades": hidden.get("closed_trades"),
            "cleanup_complete": candidate_removed
            and not source_modified
            and controller_resources_removed,
            "provider_calls": 0,
            "artifact_root": None if artifact_root is None else str(artifact_root),
        }
        print("PIT_OPTIMIZER_HOLDOUT_SUMMARY=" + json.dumps(public, sort_keys=True, separators=(",", ":")))
    return 0 if not failure and status in {"preflight_completed", "holdout_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
