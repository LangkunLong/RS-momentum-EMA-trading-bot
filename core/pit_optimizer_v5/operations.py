"""Host-only setup and sequential launch wiring for the production V5 runtime."""

from __future__ import annotations

from argparse import Namespace
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
from core.pit_optimizer_v5.campaign_admission import (
    AuthenticatedCampaignAdmissionV5,
    CampaignAdmissionBindingV5,
    _campaign_admission_enrollment_state_v5,
    _campaign_observed_started_rounds_v5,
    _load_campaign_boundary_facts_v5,
    _require_campaign_enrollment_freshness_v5,
    _selected_campaign_launch_v5,
    authenticate_prepared_campaign_policy_v5,
    campaign_boundary_decision_v5,
    load_campaign_admission_v5,
)
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    authenticate_campaign_manifest_v5,
    capture_clean_policy_snapshot_v5,
)
from core.pit_optimizer_v5.development_preparation import (
    DevelopmentAdapterConfigV5,
    ProviderDevelopmentAdapterConfigV5,
    development_config_type_v5,
    development_config_paths_v5,
    DevelopmentRoundFactoryV5,
    capture_development_policy_v5,
    compose_development_round_v5,
    load_development_config_v5,
    require_development_v5,
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
) -> ProductionAdapterConfigV5 | DevelopmentAdapterConfigV5 | ProviderDevelopmentAdapterConfigV5:
    authorities = _authorities(authenticated)
    development = authenticated.manifest.pit_data_scope == "development_sp500_v2"
    composer = compose_development_round_v5 if development else _compose_production_round_from_paths_v5
    try:
        composition = composer(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            **paths,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        detail = str(exc.__cause__ or exc)
        raise ProductionOperationFailureV5(
            "Cannot compose the local campaign adapters: "
            + detail
            + ". Supply an authenticated scope-appropriate manifest, "
            "existing disjoint source/workspace/data/output/control directories, and real Git/Docker executables"
        ) from exc
    candidate = composition.dependencies.candidates
    base = candidate.base_operations
    provider_fields = {}
    if authenticated.manifest.provider is not None:
        gateway = composition.dependencies.invoker.runner.completion_provider.gateway
        provider_fields = dict(
            gateway_identity_sha256=gateway.gateway_identity_sha256,
            ledger_identity_sha256=gateway.ledger_identity_sha256,
            audit_store_identity_sha256=gateway.audit_store_identity_sha256,
        )
    config_type = development_config_type_v5(authenticated.manifest) if development else ProductionAdapterConfigV5
    config = config_type(
        schema_version=5,
        campaign_manifest_sha256=authenticated.manifest.sha256,
        repository_root_identity_sha256=repository.root_identity_sha256,
        **provider_fields,
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


def _prepare_adapter_config_v5(
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
    response_directory: str | None = None,
) -> dict[str, object]:
    """Verify local resources and save actual adapter identities without execution."""
    ArtifactRefV5(output_path, "0" * 64)
    if authenticated.manifest.pit_data_scope == "development_sp500_v2" and round_index != 1:
        raise ProductionOperationFailureV5("development setup requires original round 1 configuration")
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
    development = authenticated.manifest.pit_data_scope == "development_sp500_v2"
    if development:
        require_development_v5(authenticated.manifest)
        if authenticated.manifest.provider is None:
            paths["response_directory"] = _existing_path(response_directory, name="response directory")
        elif response_directory is not None:
            raise ProductionOperationFailureV5("Provider development cannot accept a response directory")
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
        capture = capture_development_policy_v5 if development else capture_clean_policy_snapshot_v5
        snapshot = capture(
            source_root=Path(paths["source_root"]),
            git_executable=Path(paths["git_executable"]),
            expected_source_commit=authenticated.manifest.source_commit,
        )
        if development:
            snapshot = snapshot[0]
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProductionOperationFailureV5(
            "Supply the manifest commit and its authenticated four policy blobs"
            if development
            else "Use a clean source checkout at manifest.source_commit with the authenticated baseline policy files"
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
        "provider": authenticated.manifest.provider,
        "evaluation_executed": False,
        "resources": authenticated.manifest.resources,
        "search": authenticated.manifest.search,
        "resume_command": render_production_command_v5(
            repository=repository,
            authenticated=authenticated,
            artifact_root=repository.root,
            adapter_config_ref=reference,
            round_index=round_index,
            owner_token_sha256=token,
            command="resume-campaign" if round_index == 1 else "resume",
        ),
    }


def prepare_production_adapter_config_v5(**kwargs):
    if kwargs["authenticated"].manifest.pit_data_scope != "production":
        raise ProductionOperationFailureV5("production setup requires production manifest")
    return _prepare_adapter_config_v5(**kwargs)


def prepare_development_adapter_config_v5(**kwargs):
    require_development_v5(kwargs["authenticated"].manifest)
    return _prepare_adapter_config_v5(**kwargs)


def _scope_adapters(authenticated):
    if authenticated.manifest.pit_data_scope == "development_sp500_v2":
        require_development_v5(authenticated.manifest)
        return load_development_config_v5, DevelopmentRoundFactoryV5
    return ProductionV5CommandServices._adapter_config, ProductionRoundFactoryV5


def _expected_campaign_admission_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    adapter_config_ref: ArtifactRefV5,
    owner_token_sha256: str,
    campaign_policy_ref: ArtifactRefV5,
) -> AuthenticatedCampaignAdmissionV5:
    policy = authenticate_prepared_campaign_policy_v5(
        repository=repository,
        manifest=authenticated.manifest,
        policy_ref=campaign_policy_ref,
    )
    if (
        policy.manifest_ref != authenticated.manifest_ref
        or policy.adapter_config_ref != adapter_config_ref
        or policy.owner_token_sha256 != owner_token_sha256
        or policy.repository_root_identity_sha256
        != repository.root_identity_sha256
    ):
        raise ProductionOperationFailureV5(
            "Campaign policy differs from the supplied manifest, config, owner, or repository root"
        )
    return AuthenticatedCampaignAdmissionV5(
        binding=CampaignAdmissionBindingV5(
            5,
            campaign_policy_ref,
            authenticated.manifest_ref,
            adapter_config_ref,
            owner_token_sha256,
            repository.root_identity_sha256,
        ),
        policy=policy,
    )


def _authenticated_resume_admission_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    adapter_config_ref: ArtifactRefV5,
    owner_token_sha256: str,
    expected_policy_ref: ArtifactRefV5 | None,
) -> AuthenticatedCampaignAdmissionV5 | None:
    admission = load_campaign_admission_v5(
        repository=repository,
        manifest=authenticated.manifest,
        expected_policy_ref=expected_policy_ref,
    )
    if admission is not None and (
        admission.binding.manifest_ref != authenticated.manifest_ref
        or admission.binding.adapter_config_ref != adapter_config_ref
        or admission.binding.owner_token_sha256 != owner_token_sha256
        or admission.binding.repository_root_identity_sha256
        != repository.root_identity_sha256
    ):
        raise ProductionOperationFailureV5(
            "Campaign admission differs from the original manifest, config, owner, or repository root"
        )
    return admission


def render_production_command_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    artifact_root: Path,
    adapter_config_ref: ArtifactRefV5,
    round_index: int,
    owner_token_sha256: str,
    command: str = "run",
    campaign_policy_ref: ArtifactRefV5 | None = None,
) -> dict[str, object]:
    """Return argv plus a PowerShell command after verifying the local adapter graph."""
    if command not in {"run", "resume", "run-campaign", "resume-campaign"}:
        raise ProductionOperationFailureV5("The rendered command must be run, resume, run-campaign, or resume-campaign")
    if authenticated.manifest.pit_data_scope == "development_sp500_v2" and not command.endswith("campaign"):
        raise ProductionOperationFailureV5("development uses run-campaign or resume-campaign")
    if command.endswith("campaign") and round_index != 1:
        raise ProductionOperationFailureV5("Campaign launch uses the round 1 configuration and owner")
    if not command.endswith("campaign") and campaign_policy_ref is not None:
        raise ProductionOperationFailureV5("Campaign policy is valid only for campaign commands")
    token = _owner_token(owner_token_sha256)
    authorities = _authorities(authenticated)
    load_config, factory = _scope_adapters(authenticated)
    config = load_config(repository, authorities, adapter_config_ref)
    composition = factory(adapter_config_ref=adapter_config_ref, adapter_config=config).compose_round(
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
    selected_policy_ref = campaign_policy_ref
    if command == "run-campaign" and campaign_policy_ref is not None:
        _expected_campaign_admission_v5(
            repository=repository,
            authenticated=authenticated,
            adapter_config_ref=adapter_config_ref,
            owner_token_sha256=token,
            campaign_policy_ref=campaign_policy_ref,
        )
    elif command == "resume-campaign":
        admission = _authenticated_resume_admission_v5(
            repository=repository,
            authenticated=authenticated,
            adapter_config_ref=adapter_config_ref,
            owner_token_sha256=token,
            expected_policy_ref=campaign_policy_ref,
        )
        selected_policy_ref = None if admission is None else admission.binding.policy_ref
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
    ]
    if selected_policy_ref is not None:
        argv.extend(
            (
                "--campaign-policy-path",
                selected_policy_ref.relative_path,
                "--campaign-policy-sha256",
                selected_policy_ref.sha256,
            )
        )
    argv.extend(
        (
            "--adapter-config-path",
            adapter_config_ref.relative_path,
            "--adapter-config-sha256",
            adapter_config_ref.sha256,
            "--owner-token-sha256",
            token,
        )
    )
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


def _run_campaign_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authenticated: AuthenticatedCampaignManifestV5,
    adapter_config_ref: ArtifactRefV5,
    owner_token_sha256: str,
    resume: bool = False,
    emit_round: Callable[[dict[str, object]], None] | None = None,
    campaign_policy_ref: ArtifactRefV5 | None = None,
) -> dict[str, object]:
    """Run existing rounds sequentially under the original persisted campaign deadline."""
    manifest = authenticated.manifest
    authorities = _authorities(authenticated)
    token = _owner_token(owner_token_sha256)
    load_config, factory = _scope_adapters(authenticated)
    config = load_config(repository, authorities, adapter_config_ref)
    key = manifest.sha256
    with repository.adapter_state_transition(namespace="campaign-launch", key=key):
        # Recheck the original round's identities even on resume, so deriving a
        # later owner-bound config cannot quietly accept changed host resources.
        first = factory(adapter_config_ref=adapter_config_ref, adapter_config=config).compose_round(
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
        admission: AuthenticatedCampaignAdmissionV5 | None
        if campaign_policy_ref is None:
            # Preserve the legacy operational repair and error order when no
            # explicit enrollment is being created.
            launch = repository.load_typed_state(
                namespace="campaign-launch",
                key=key,
                value_type=CampaignLaunchV5,
            )
            if launch is None:
                if resume:
                    raise ProductionOperationFailureV5(
                        "No campaign launch state exists; use run-campaign for a fresh campaign"
                    )
                if repository.load_checkpoint() is not None or any(
                    repository.load_round_events(
                        campaign_id=manifest.campaign_id,
                        round_index=index,
                    )
                    for index in range(1, manifest.search.max_feedback_rounds + 1)
                ):
                    raise ProductionOperationFailureV5(
                        "Campaign already has round state; use its original resume command"
                    )
                # An unjournaled paid reservation also makes a campaign non-fresh.
                ProductionV5CommandServices._verify_local_run(
                    repository,
                    authorities,
                    config,
                )
                admission = _authenticated_resume_admission_v5(
                    repository=repository,
                    authenticated=authenticated,
                    adapter_config_ref=adapter_config_ref,
                    owner_token_sha256=token,
                    expected_policy_ref=None,
                )
                if admission is not None:
                    raise ProductionOperationFailureV5(
                        "An interrupted enrolled launch requires its explicit campaign policy"
                    )
                launch = CampaignLaunchV5(
                    authenticated.manifest_ref,
                    adapter_config_ref,
                    token,
                    time.time_ns() // 1_000_000,
                )
                repository.append_typed_state(
                    namespace="campaign-launch",
                    key=key,
                    value=launch,
                )
            elif not resume:
                raise ProductionOperationFailureV5(
                    "Campaign launch already exists; use resume-campaign with the original config and owner"
                )
            admission = _authenticated_resume_admission_v5(
                repository=repository,
                authenticated=authenticated,
                adapter_config_ref=adapter_config_ref,
                owner_token_sha256=token,
                expected_policy_ref=None,
            )
        else:
            expected_admission = _expected_campaign_admission_v5(
                repository=repository,
                authenticated=authenticated,
                adapter_config_ref=adapter_config_ref,
                owner_token_sha256=token,
                campaign_policy_ref=campaign_policy_ref,
            )
            enrollment_state = _campaign_admission_enrollment_state_v5(
                repository=repository,
                manifest=manifest,
                expected=expected_admission,
            )
            launch = repository.load_typed_state(
                namespace="campaign-launch",
                key=key,
                value_type=CampaignLaunchV5,
                repair=False,
            )
            if launch is None:
                if resume:
                    raise ProductionOperationFailureV5(
                        "No campaign launch state exists; use run-campaign for a fresh campaign"
                    )
                _require_campaign_enrollment_freshness_v5(
                    repository=repository,
                    manifest=manifest,
                    expected=expected_admission,
                )
                ProductionV5CommandServices._verify_local_run(
                    repository,
                    authorities,
                    config,
                )
                if enrollment_state == "absent":
                    repository.append_typed_state(
                        namespace="campaign-admission",
                        key=key,
                        value=expected_admission.binding,
                    )
                elif enrollment_state == "missing_index":
                    repository.recover_typed_state_index(
                        namespace="campaign-admission",
                        key=key,
                        expected_value=expected_admission.binding,
                    )
                admission = _authenticated_resume_admission_v5(
                    repository=repository,
                    authenticated=authenticated,
                    adapter_config_ref=adapter_config_ref,
                    owner_token_sha256=token,
                    expected_policy_ref=campaign_policy_ref,
                )
                if admission != expected_admission:
                    raise ProductionOperationFailureV5(
                        "Campaign enrollment differs after publication recovery"
                    )
                launch = CampaignLaunchV5(
                    authenticated.manifest_ref,
                    adapter_config_ref,
                    token,
                    time.time_ns() // 1_000_000,
                )
                repository.append_typed_state(
                    namespace="campaign-launch",
                    key=key,
                    value=launch,
                )
            else:
                if not resume:
                    raise ProductionOperationFailureV5(
                        "Campaign launch already exists; use resume-campaign with the original config and owner"
                    )
                if enrollment_state != "enrolled":
                    raise ProductionOperationFailureV5(
                        "A launched campaign cannot be newly enrolled during resume"
                    )
                admission = _authenticated_resume_admission_v5(
                    repository=repository,
                    authenticated=authenticated,
                    adapter_config_ref=adapter_config_ref,
                    owner_token_sha256=token,
                    expected_policy_ref=campaign_policy_ref,
                )
        if (launch.manifest_ref, launch.adapter_config_ref, launch.owner_token_sha256) != (
            authenticated.manifest_ref,
            adapter_config_ref,
            token,
        ):
            raise ProductionOperationFailureV5(
                "Resume must use the original campaign manifest, adapter config, and owner"
            )
        if admission is not None and _selected_campaign_launch_v5(
            repository=repository,
            manifest=manifest,
            admission=admission,
        ) != launch:
            raise ProductionOperationFailureV5(
                "Campaign admission selects a different original launch"
            )
        now_ms = time.time_ns() // 1_000_000
        if now_ms < launch.started_epoch_ms:
            raise ProductionOperationFailureV5(
                "Host clock precedes persisted campaign start; correct the clock before resuming"
            )
        remaining = manifest.resources.campaign_wall_timeout_seconds - (now_ms - launch.started_epoch_ms) / 1000
        deadline = time.monotonic() + remaining
        enrolled_policy_ref = None if admission is None else admission.binding.policy_ref

        def reload_admission() -> AuthenticatedCampaignAdmissionV5 | None:
            current = _authenticated_resume_admission_v5(
                repository=repository,
                authenticated=authenticated,
                adapter_config_ref=adapter_config_ref,
                owner_token_sha256=token,
                expected_policy_ref=enrolled_policy_ref,
            )
            if enrolled_policy_ref is None and current is not None:
                raise ProductionOperationFailureV5(
                    "A legacy campaign cannot acquire enrollment after launch"
                )
            return current

        def policy_boundary(next_round_index: int) -> str | None:
            current = reload_admission()
            if current is None:
                return None
            facts = _load_campaign_boundary_facts_v5(
                repository=repository,
                manifest=manifest,
                admission=current,
                owner_token_sha256=token,
                next_round_index=next_round_index,
            )
            if time.monotonic() >= deadline:
                return "campaign_deadline_exhausted"
            return campaign_boundary_decision_v5(
                policy=current.policy,
                manifest=manifest,
                evaluated_feedback_rounds=facts.evaluated_feedback_rounds,
                attempted_rounds=facts.attempted_rounds,
                external_attempts=facts.external_attempts,
                total_tokens=facts.total_tokens,
                cost_usd=facts.cost_usd,
            )

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
                current_admission = reload_admission()
                if current_admission is not None:
                    observed_started = _campaign_observed_started_rounds_v5(
                        repository=repository,
                        manifest=manifest,
                        admission=current_admission,
                        owner_token_sha256=token,
                    )
                    if any(index > round_index for index in observed_started):
                        continue
                    decision = policy_boundary(round_index + 1)
                    if decision != "admit":
                        assert decision is not None
                        reason = decision
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
                decision = policy_boundary(round_index)
                if enrolled_policy_ref is not None and time.monotonic() >= deadline:
                    reason = "campaign_deadline_exhausted"
                    break
                if decision is not None and decision != "admit":
                    reason = decision
                    break
                round_config = (
                    config
                    if round_index == 1
                    else _derive_config(
                        repository=repository,
                        authenticated=authenticated,
                        round_index=round_index,
                        owner_token_sha256=token,
                        paths={
                            field: getattr(config, field)
                            for field in (
                                development_config_paths_v5(config)
                                if authenticated.manifest.pit_data_scope == "development_sp500_v2"
                                else _PATH_FIELDS
                            )
                        },
                    )
                )
                if enrolled_policy_ref is not None and time.monotonic() >= deadline:
                    reason = "campaign_deadline_exhausted"
                    break
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
            if started is not None and not events and not (
                type(config) is DevelopmentAdapterConfigV5 and manifest.provider is None
            ):
                raise ProductionOperationFailureV5(
                    "Round start was persisted but no round journal exists; inspect provider reservations before recovery. "
                    "The campaign will not silently restart this uncertain round"
                )
            if time.monotonic() >= deadline and not events:
                reason = "campaign_deadline_exhausted"
                break
            round_config = load_config(repository, authorities, entry.adapter_config_ref)
            composition = factory(
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
            if started is None:
                if enrolled_policy_ref is not None:
                    usage = summarize_repository_v5(
                        repository=repository,
                        manifest=manifest,
                        command="summarize",
                        readiness_code="ready",
                    )
                    provider = manifest.provider
                    if provider is not None and (
                        usage.role_calls >= provider.maximum_role_calls
                        or usage.total_tokens >= provider.maximum_total_tokens
                        or (
                            provider.maximum_usd is not None
                            and usage.cost_usd >= provider.maximum_usd
                        )
                    ):
                        reason = "provider_budget_exhausted"
                        break
                    decision = policy_boundary(round_index)
                    if time.monotonic() >= deadline:
                        reason = "campaign_deadline_exhausted"
                        break
                    if decision is not None and decision != "admit":
                        reason = decision
                        break
                if enrolled_policy_ref is not None and time.monotonic() >= deadline:
                    reason = "campaign_deadline_exhausted"
                    break
                now_ms = time.time_ns() // 1_000_000
                started = replace(entry, started_epoch_ms=now_ms)
                repository.append_typed_state(namespace="campaign-round-started", key=round_key, value=started)
            else:
                now_ms = time.time_ns() // 1_000_000
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
            if result.status == "awaiting_controller_response":
                return {
                    "schema_version": 5,
                    "status": "pending_controller_response",
                    "reason": "pending_controller_response",
                    "pending": result.pending_controller_role,
                    "round_index": round_index,
                    "rounds": tuple(rounds),
                    "manifest_ref": authenticated.manifest_ref,
                    "started_epoch_ms": launch.started_epoch_ms,
                    "round_started_epoch_ms": started.started_epoch_ms,
                    "deadline_epoch_ms": launch.started_epoch_ms
                    + manifest.resources.campaign_wall_timeout_seconds * 1000,
                    "resume_command": render_production_command_v5(
                        repository=repository,
                        authenticated=authenticated,
                        artifact_root=repository.root,
                        adapter_config_ref=adapter_config_ref,
                        round_index=1,
                        owner_token_sha256=token,
                        command="resume-campaign",
                        campaign_policy_ref=enrolled_policy_ref,
                    ),
                }
            if result.cleanup is None or not result.cleanup.cleanup_complete or result.cleanup_failure is not None:
                reason = "cleanup_incomplete"
                break
            if result.status not in {"completed", "no_novel_hypothesis"}:
                reason = result.status
                break
            current_admission = reload_admission()
            if current_admission is not None:
                observed_started = _campaign_observed_started_rounds_v5(
                    repository=repository,
                    manifest=manifest,
                    admission=current_admission,
                    owner_token_sha256=token,
                )
                if not any(index > round_index for index in observed_started):
                    decision = policy_boundary(round_index + 1)
                    if decision != "admit":
                        assert decision is not None
                        reason = decision
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
            "status": "completed"
            if reason in {"max_feedback_rounds", "evaluated_feedback_target_reached"}
            else "stopped",
            "reason": reason,
            "manifest_ref": authenticated.manifest_ref,
            "rounds": tuple(rounds),
            "started_epoch_ms": launch.started_epoch_ms,
            "deadline_epoch_ms": launch.started_epoch_ms + manifest.resources.campaign_wall_timeout_seconds * 1000,
            "summary": summary,
        }


def run_production_campaign_v5(**kwargs):
    if kwargs["authenticated"].manifest.pit_data_scope != "production":
        raise ProductionOperationFailureV5("production run requires production manifest")
    return _run_campaign_v5(**kwargs)


def run_development_campaign_v5(**kwargs):
    require_development_v5(kwargs["authenticated"].manifest)
    return _run_campaign_v5(**kwargs)


def _campaign_policy_ref_from_args_v5(args: Namespace) -> ArtifactRefV5 | None:
    path = args.campaign_policy_path
    sha256 = args.campaign_policy_sha256
    if (path is None) != (sha256 is None):
        raise ProductionOperationFailureV5(
            "Campaign policy path and SHA-256 must be supplied together"
        )
    return None if path is None else ArtifactRefV5(path, sha256)


def dispatch_production_operations_cli_v5(argv: Sequence[str], *, emit: Callable[[str], None] = print) -> int:
    try:
        args = build_parser_v5().parse_args(tuple(argv))
        repository = LocalArtifactRepositoryV5(Path(args.artifact_root))
        authenticated = authenticate_campaign_manifest_v5(
            repository=repository,
            manifest_ref=ArtifactRefV5(args.manifest_path, args.manifest_sha256),
        )
        if args.command in {"prepare-production", "prepare-development"}:
            prepare = (
                prepare_development_adapter_config_v5
                if args.command == "prepare-development"
                else prepare_production_adapter_config_v5
            )
            result = prepare(
                repository=repository,
                authenticated=authenticated,
                output_path=args.output_path,
                round_index=args.round_index,
                owner_token_sha256=args.owner_token_sha256,
                **{field: getattr(args, field) for field in _PATH_FIELDS},
                **({"response_directory": args.response_directory} if args.command == "prepare-development" else {}),
            )
        else:
            run = (
                run_development_campaign_v5
                if authenticated.manifest.pit_data_scope == "development_sp500_v2"
                else run_production_campaign_v5
            )
            result = run(
                repository=repository,
                authenticated=authenticated,
                adapter_config_ref=ArtifactRefV5(args.adapter_config_path, args.adapter_config_sha256),
                owner_token_sha256=args.owner_token_sha256,
                resume=args.command == "resume-campaign",
                emit_round=lambda row: emit("PIT_OPTIMIZER_V5_ROUND=" + canonical_json_bytes_v5(row).decode("utf-8")),
                campaign_policy_ref=_campaign_policy_ref_from_args_v5(args),
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
                "Cannot authenticate or use the supplied campaign resources. Verify the artifact root, manifest/config "
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
