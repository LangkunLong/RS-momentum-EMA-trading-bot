"""Host-only setup and sequential launch wiring for the production V5 runtime."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
from typing import Callable, Sequence

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.cli import (
    CampaignAuthoritiesV5,
    ProductionAdapterConfigV5,
    ProductionRoundFactoryV5,
    ProductionV5CommandServices,
    _compose_production_round_from_paths_v5,
    build_parser_v5,
)
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    authenticate_campaign_manifest_v5,
    capture_clean_policy_snapshot_v5,
)
from core.pit_optimizer_v5.memory import CleanupResultPayloadV5, RoundOutcomePayloadV5
from core.pit_optimizer_v5.runtime import reconcile_existing_paid_roles_v5, run_feedback_round_v5
from core.pit_optimizer_v5.summary import summarize_repository_v5


_PATH_FIELDS = (
    "source_root",
    "workspace_root",
    "data_root",
    "output_root",
    "control_root",
    "git_executable",
    "docker_executable",
)


class ProductionOperationFailureV5(ValueError):
    """Actionable local operator diagnostic; never includes provider responses."""


@dataclass(frozen=True, slots=True)
class CampaignLaunchV5:
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    started_epoch_ms: int

    def __post_init__(self) -> None:
        if type(self.manifest_ref) is not ArtifactRefV5 or type(self.adapter_config_ref) is not ArtifactRefV5:
            raise ValueError("campaign launch references are invalid")
        _owner_token(self.owner_token_sha256)
        if type(self.started_epoch_ms) is not int or self.started_epoch_ms < 1:
            raise ValueError("campaign start time is invalid")


@dataclass(frozen=True, slots=True)
class CampaignRoundLaunchV5:
    round_index: int
    adapter_config_ref: ArtifactRefV5
    owner_token_sha256: str
    started_epoch_ms: int | None = None

    def __post_init__(self) -> None:
        if type(self.round_index) is not int or self.round_index < 1:
            raise ValueError("campaign launch round is invalid")
        if type(self.adapter_config_ref) is not ArtifactRefV5:
            raise ValueError("campaign round config reference is invalid")
        _owner_token(self.owner_token_sha256)
        if self.started_epoch_ms is not None and (type(self.started_epoch_ms) is not int or self.started_epoch_ms < 1):
            raise ValueError("round start time is invalid")


def _owner_token(value: str) -> str:
    if type(value) is not str or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ProductionOperationFailureV5("--owner-token-sha256 must contain 64 lowercase hexadecimal characters")
    return value


def _authorities(authenticated: AuthenticatedCampaignManifestV5) -> CampaignAuthoritiesV5:
    return CampaignAuthoritiesV5(
        authenticated.manifest,
        authenticated.panel_plan,
        authenticated.evaluator_contract,
        authenticated.baseline_authority,
        authenticated.sandbox_profile,
    )


def _existing_path(value: str | None, *, name: str, executable: bool = False) -> str:
    selected = shutil.which(name) if value is None and executable else value
    if selected is None:
        raise ProductionOperationFailureV5(
            f"Install {name} or pass --{name}-executable with its absolute executable path"
        )
    path = Path(os.path.abspath(selected))
    if not (path.is_file() if executable else path.is_dir()):
        kind = "executable file" if executable else "directory"
        raise ProductionOperationFailureV5(f"Missing {name} {kind}: {path}. Create or supply the required resource")
    return str(path)


def _derive_config(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    round_index: int,
    owner_token_sha256: str,
    paths: dict[str, str],
) -> ProductionAdapterConfigV5:
    authorities = _authorities(authenticated)
    try:
        composition = _compose_production_round_from_paths_v5(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            **paths,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        detail = str(exc.__cause__ or exc)
        raise ProductionOperationFailureV5(
            "Cannot compose the local production adapters: "
            + detail
            + ". Supply a provider-enabled manifest with zero automatic retries/schema repairs, "
            "existing disjoint source/workspace/data/output/control directories, and real Git/Docker executables"
        ) from exc
    candidate = composition.dependencies.candidates
    base = candidate.base_operations
    gateway = composition.dependencies.invoker.runner.completion_provider.gateway
    config = ProductionAdapterConfigV5(
        schema_version=5,
        campaign_manifest_sha256=authenticated.manifest.sha256,
        repository_root_identity_sha256=repository.root_identity_sha256,
        gateway_identity_sha256=gateway.gateway_identity_sha256,
        ledger_identity_sha256=gateway.ledger_identity_sha256,
        audit_store_identity_sha256=gateway.audit_store_identity_sha256,
        candidate_base_identity_sha256=base.base_identity_sha256,
        workspace_driver_identity_sha256=base.materializer.driver.driver_identity_sha256,
        mount_factory_identity_sha256=candidate.mount_factory.mount_identity_sha256,
        container_executor_identity_sha256=candidate.panel_evaluator.evaluator.executor.executor_identity_sha256,
        **paths,
    )
    composition.validate_for(
        repository=repository,
        authorities=authorities,
        round_index=round_index,
        owner_token_sha256=owner_token_sha256,
        adapter_config=config,
    )
    return config


def prepare_production_adapter_config_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    output_path: str,
    source_root: str,
    workspace_root: str,
    data_root: str,
    output_root: str,
    control_root: str,
    git_executable: str | None = None,
    docker_executable: str | None = None,
    round_index: int = 1,
    owner_token_sha256: str | None = None,
) -> dict[str, object]:
    """Verify local resources and save actual adapter identities without execution."""
    ArtifactRefV5(output_path, "0" * 64)
    token = _owner_token(secrets.token_hex(32) if owner_token_sha256 is None else owner_token_sha256)
    paths = {
        "source_root": _existing_path(source_root, name="source root"),
        "workspace_root": _existing_path(workspace_root, name="workspace root"),
        "data_root": _existing_path(data_root, name="data root"),
        "output_root": _existing_path(output_root, name="output root"),
        "control_root": _existing_path(control_root, name="control root"),
        "git_executable": _existing_path(git_executable, name="git", executable=True),
        "docker_executable": _existing_path(docker_executable, name="docker", executable=True),
    }
    data = Path(paths["data_root"])
    expected = {
        "pit_bundle.sqlite3": authenticated.evaluator_contract.pit_bundle_sha256,
        "prices_provenance.json": authenticated.evaluator_contract.prices_provenance_sha256,
    }
    if set(item.name for item in data.iterdir()) != set(expected):
        raise ProductionOperationFailureV5(
            "The data root must contain exactly pit_bundle.sqlite3 and prices_provenance.json from this manifest"
        )
    for name, digest in expected.items():
        path = data / name
        if not path.is_file() or path.is_symlink():
            raise ProductionOperationFailureV5(f"The data resource must be a regular file: {path}")
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != digest:
            raise ProductionOperationFailureV5(f"Data digest mismatch for {path}; supply the manifest-bound file")
    try:
        snapshot = capture_clean_policy_snapshot_v5(
            source_root=Path(paths["source_root"]),
            git_executable=Path(paths["git_executable"]),
            expected_source_commit=authenticated.manifest.source_commit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProductionOperationFailureV5(
            "Use a clean source checkout at manifest.source_commit with the authenticated baseline policy files"
        ) from exc
    if snapshot.editable_source_sha256 != authenticated.policy_scope.editable_source_sha256:
        raise ProductionOperationFailureV5("Source policy files differ from the authenticated manifest policy scope")
    config = _derive_config(
        repository=repository,
        authenticated=authenticated,
        round_index=round_index,
        owner_token_sha256=token,
        paths=paths,
    )
    reference = repository.create_typed_artifact(output_path, config)
    return {
        "schema_version": 5,
        "status": "prepared",
        "manifest_ref": authenticated.manifest_ref,
        "adapter_config_ref": reference,
        "round_index": round_index,
        "owner_token_sha256": token,
        "provider_executed": False,
        "evaluation_executed": False,
    }


def render_production_command_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    artifact_root: Path,
    adapter_config_ref: ArtifactRefV5,
    round_index: int,
    owner_token_sha256: str,
    command: str = "run",
) -> dict[str, object]:
    """Return argv plus a PowerShell command after verifying the local adapter graph."""
    if command not in {"run", "resume", "run-campaign", "resume-campaign"}:
        raise ProductionOperationFailureV5("The rendered command must be run, resume, run-campaign, or resume-campaign")
    if command.endswith("campaign") and round_index != 1:
        raise ProductionOperationFailureV5("Campaign launch uses the round 1 configuration and owner")
    token = _owner_token(owner_token_sha256)
    authorities = _authorities(authenticated)
    config = ProductionV5CommandServices._adapter_config(repository, authorities, adapter_config_ref)
    composition = ProductionRoundFactoryV5(adapter_config_ref=adapter_config_ref, adapter_config=config).compose_round(
        repository=repository,
        authorities=authorities,
        round_index=round_index,
        owner_token_sha256=token,
        adapter_config=config,
    )
    composition.validate_for(
        repository=repository,
        authorities=authorities,
        round_index=round_index,
        owner_token_sha256=token,
        adapter_config=config,
    )
    argv = [
        sys.executable,
        "-B",
        "-m",
        "core.pit_optimizer_v5.cli",
        command,
        "--artifact-root",
        str(artifact_root),
        "--manifest-path",
        authenticated.manifest_ref.relative_path,
        "--manifest-sha256",
        authenticated.manifest_ref.sha256,
        "--adapter-config-path",
        adapter_config_ref.relative_path,
        "--adapter-config-sha256",
        adapter_config_ref.sha256,
        "--owner-token-sha256",
        token,
    ]
    if not command.endswith("campaign"):
        argv.extend(("--round-index", str(round_index)))
    return {
        "schema_version": 5,
        "executable": True,
        "adapter_composition": "prepared",
        "manifest_ref": authenticated.manifest_ref,
        "adapter_config_ref": adapter_config_ref,
        "round_index": round_index,
        "owner_token_sha256": token,
        "argv": tuple(argv),
        "working_directory": str(Path(__file__).resolve().parents[2]),
        "powershell_command": "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv),
        "provider_executed": False,
        "evaluation_executed": False,
    }


def run_production_campaign_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    adapter_config_ref: ArtifactRefV5,
    owner_token_sha256: str,
    resume: bool = False,
    emit_round: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Run existing rounds sequentially under the original persisted campaign deadline."""
    manifest = authenticated.manifest
    authorities = _authorities(authenticated)
    token = _owner_token(owner_token_sha256)
    config = ProductionV5CommandServices._adapter_config(repository, authorities, adapter_config_ref)
    key = manifest.sha256
    with repository.adapter_state_transition(namespace="campaign-launch", key=key):
        # Recheck the original round's identities even on resume, so deriving a
        # later owner-bound config cannot quietly accept changed host resources.
        first = ProductionRoundFactoryV5(adapter_config_ref=adapter_config_ref, adapter_config=config).compose_round(
            repository=repository,
            authorities=authorities,
            round_index=1,
            owner_token_sha256=token,
            adapter_config=config,
        )
        first.validate_for(
            repository=repository,
            authorities=authorities,
            round_index=1,
            owner_token_sha256=token,
            adapter_config=config,
        )
        launch = repository.load_typed_state(namespace="campaign-launch", key=key, value_type=CampaignLaunchV5)
        if launch is None:
            if resume:
                raise ProductionOperationFailureV5(
                    "No campaign launch state exists; use run-campaign for a fresh campaign"
                )
            if repository.load_checkpoint() is not None or any(
                repository.load_round_events(campaign_id=manifest.campaign_id, round_index=index)
                for index in range(1, manifest.search.max_feedback_rounds + 1)
            ):
                raise ProductionOperationFailureV5("Campaign already has round state; use its original resume command")
            # An unjournaled paid reservation also makes a campaign non-fresh.
            ProductionV5CommandServices._verify_local_run(repository, authorities, config)
            launch = CampaignLaunchV5(
                authenticated.manifest_ref, adapter_config_ref, token, time.time_ns() // 1_000_000
            )
            repository.append_typed_state(namespace="campaign-launch", key=key, value=launch)
        elif not resume:
            raise ProductionOperationFailureV5(
                "Campaign launch already exists; use resume-campaign with the original config and owner"
            )
        if (launch.manifest_ref, launch.adapter_config_ref, launch.owner_token_sha256) != (
            authenticated.manifest_ref,
            adapter_config_ref,
            token,
        ):
            raise ProductionOperationFailureV5(
                "Resume must use the original campaign manifest, adapter config, and owner"
            )
        now_ms = time.time_ns() // 1_000_000
        if now_ms < launch.started_epoch_ms:
            raise ProductionOperationFailureV5(
                "Host clock precedes persisted campaign start; correct the clock before resuming"
            )
        remaining = manifest.resources.campaign_wall_timeout_seconds - (now_ms - launch.started_epoch_ms) / 1000
        deadline = time.monotonic() + remaining
        rounds = []
        reason = "max_feedback_rounds"
        for round_index in range(1, manifest.search.max_feedback_rounds + 1):
            # Also inspect already-terminal rounds before their reuse shortcut:
            # old interrupted paid work must not disappear behind complete cleanup.
            reconcile_existing_paid_roles_v5(
                replace(first.inputs, round_index=round_index),
                persistence=repository,
                invoker=first.dependencies.invoker,
            )
            events = repository.load_round_events(campaign_id=manifest.campaign_id, round_index=round_index)
            payloads = tuple(
                repository.load_round_payload(item.payload_ref, expected_kind=item.event_kind) for item in events
            )
            terminal = next((item for item in payloads if type(item) is RoundOutcomePayloadV5), None)
            cleanup = next((item for item in reversed(payloads) if type(item) is CleanupResultPayloadV5), None)
            checkpoint = repository.load_checkpoint()
            committed = checkpoint is not None and any(
                repository.load_experiment(ref).round_index == round_index for ref in checkpoint.record_refs
            )
            settled = committed or terminal is not None
            if settled and cleanup is not None and cleanup.cleanup_complete:
                status = "completed" if committed else terminal.outcome
                rounds.append({"round_index": round_index, "status": status, "reused": True})
                if status not in {"completed", "no_novel_hypothesis"}:
                    reason = status
                    break
                continue
            round_key = canonical_sha256_v5({"campaign_manifest_sha256": key, "round_index": round_index})
            entry = repository.load_typed_state(
                namespace="campaign-round", key=round_key, value_type=CampaignRoundLaunchV5
            )
            if entry is None:
                if events:
                    raise ProductionOperationFailureV5(
                        "Round journal has no campaign launch record; inspect its original single-round owner/config"
                    )
                if time.monotonic() >= deadline:
                    reason = "campaign_deadline_exhausted"
                    break
                usage = summarize_repository_v5(
                    repository=repository, manifest=manifest, command="summarize", readiness_code="ready"
                )
                provider = manifest.provider
                if provider is not None and (
                    usage.role_calls >= provider.maximum_role_calls
                    or usage.total_tokens >= provider.maximum_total_tokens
                    or (provider.maximum_usd is not None and usage.cost_usd >= provider.maximum_usd)
                ):
                    reason = "provider_budget_exhausted"
                    break
                round_config = (
                    config
                    if round_index == 1
                    else _derive_config(
                        repository=repository,
                        authenticated=authenticated,
                        round_index=round_index,
                        owner_token_sha256=token,
                        paths={field: getattr(config, field) for field in _PATH_FIELDS},
                    )
                )
                round_ref = (
                    adapter_config_ref
                    if round_index == 1
                    else repository.create_typed_artifact(
                        f"campaigns/{key}/adapter-config-round-{round_index}.json",
                        round_config,
                    )
                )
                entry = CampaignRoundLaunchV5(round_index, round_ref, token)
                repository.append_typed_state(namespace="campaign-round", key=round_key, value=entry)
            if (
                entry.round_index != round_index
                or entry.owner_token_sha256 != token
                or entry.started_epoch_ms is not None
            ):
                raise ProductionOperationFailureV5("Persisted round ownership differs from campaign launch")
            started = repository.load_typed_state(
                namespace="campaign-round-started", key=round_key, value_type=CampaignRoundLaunchV5
            )
            if started is not None and (
                started.started_epoch_ms is None or replace(started, started_epoch_ms=None) != entry
            ):
                raise ProductionOperationFailureV5("Persisted round start differs from its configuration")
            if started is not None and not events:
                raise ProductionOperationFailureV5(
                    "Round start was persisted but no round journal exists; inspect provider reservations before recovery. "
                    "The campaign will not silently restart this uncertain round"
                )
            if time.monotonic() >= deadline and not events:
                reason = "campaign_deadline_exhausted"
                break
            round_config = ProductionV5CommandServices._adapter_config(
                repository, authorities, entry.adapter_config_ref
            )
            composition = ProductionRoundFactoryV5(
                adapter_config_ref=entry.adapter_config_ref,
                adapter_config=round_config,
            ).compose_round(
                repository=repository,
                authorities=authorities,
                round_index=round_index,
                owner_token_sha256=entry.owner_token_sha256,
                adapter_config=round_config,
            )
            composition.validate_for(
                repository=repository,
                authorities=authorities,
                round_index=round_index,
                owner_token_sha256=entry.owner_token_sha256,
                adapter_config=round_config,
            )
            now_ms = time.time_ns() // 1_000_000
            if started is None:
                started = replace(entry, started_epoch_ms=now_ms)
                repository.append_typed_state(namespace="campaign-round-started", key=round_key, value=started)
            if now_ms < started.started_epoch_ms:
                raise ProductionOperationFailureV5(
                    "Host clock precedes persisted round start; correct the clock before resuming"
                )
            round_remaining = manifest.resources.round_wall_timeout_seconds - (now_ms - started.started_epoch_ms) / 1000
            round_deadline = min(deadline, time.monotonic() + round_remaining)
            result = run_feedback_round_v5(
                composition.inputs,
                composition.dependencies,
                campaign_deadline_monotonic=round_deadline,
            )
            ProductionV5CommandServices._verify_local_run(repository, authorities, round_config)
            row = {
                "round_index": round_index,
                "status": result.status,
                "reused": False,
                "adapter_config_ref": entry.adapter_config_ref,
                "failure": result.failure,
                "cleanup_failure": result.cleanup_failure,
            }
            rounds.append(row)
            if emit_round is not None:
                emit_round(row)
            if result.cleanup is None or not result.cleanup.cleanup_complete or result.cleanup_failure is not None:
                reason = "cleanup_incomplete"
                break
            if result.status not in {"completed", "no_novel_hypothesis"}:
                reason = result.status
                break
        # Defer the complete ledger/journal check until pending runtime recovery
        # has had its opportunity to reconcile a paid reservation.
        ProductionV5CommandServices._verify_local_run(repository, authorities, config)
        summary = summarize_repository_v5(
            repository=repository,
            manifest=manifest,
            command="resume" if resume else "run",
            readiness_code="runtime_failed"
            if reason in {"failed", "runtime_failed", "cleanup_incomplete"}
            else "completed",
        )
        return {
            "schema_version": 5,
            "status": "completed" if reason == "max_feedback_rounds" else "stopped",
            "reason": reason,
            "manifest_ref": authenticated.manifest_ref,
            "rounds": tuple(rounds),
            "started_epoch_ms": launch.started_epoch_ms,
            "deadline_epoch_ms": launch.started_epoch_ms + manifest.resources.campaign_wall_timeout_seconds * 1000,
            "summary": summary,
        }


def dispatch_production_operations_cli_v5(argv: Sequence[str], *, emit: Callable[[str], None] = print) -> int:
    try:
        args = build_parser_v5().parse_args(tuple(argv))
        repository = LocalArtifactRepositoryV5(Path(args.artifact_root))
        authenticated = authenticate_campaign_manifest_v5(
            repository=repository,
            manifest_ref=ArtifactRefV5(args.manifest_path, args.manifest_sha256),
        )
        if args.command == "prepare-production":
            result = prepare_production_adapter_config_v5(
                repository=repository,
                authenticated=authenticated,
                output_path=args.output_path,
                round_index=args.round_index,
                owner_token_sha256=args.owner_token_sha256,
                **{field: getattr(args, field) for field in _PATH_FIELDS},
            )
        else:
            result = run_production_campaign_v5(
                repository=repository,
                authenticated=authenticated,
                adapter_config_ref=ArtifactRefV5(args.adapter_config_path, args.adapter_config_sha256),
                owner_token_sha256=args.owner_token_sha256,
                resume=args.command == "resume-campaign",
                emit_round=lambda row: emit("PIT_OPTIMIZER_V5_ROUND=" + canonical_json_bytes_v5(row).decode("utf-8")),
            )
        emit("PIT_OPTIMIZER_V5_OPERATIONS=" + canonical_json_bytes_v5(result).decode("utf-8"))
        return (
            1 if result.get("reason") in {"failed", "runtime_failed", "cleanup_incomplete", "critic_unavailable"} else 0
        )
    except SystemExit:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        diagnostic = (
            str(exc)
            if type(exc) is ProductionOperationFailureV5
            else (
                "Cannot authenticate or use the supplied production resources. Verify the artifact root, manifest/config "
                "paths and SHA-256 digests, and the saved round owner. " + str(exc)
            )
        )
        emit(
            "PIT_OPTIMIZER_V5_OPERATIONS="
            + canonical_json_bytes_v5(
                {
                    "schema_version": 5,
                    "status": "failed",
                    "reason": "production_operation_failed",
                    "diagnostic": diagnostic,
                }
            ).decode("utf-8")
        )
        return 2
