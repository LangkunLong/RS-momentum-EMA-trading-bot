"""Host-only development composition, authenticated imports, and controller exchange."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.controller_roles import (
    FileBackedControllerRoleInvokerV5,
    _classify_controller_response_v5,
    _decode_unique_json,
)
from core.pit_optimizer_v5.manifest import PolicySourceSnapshotV5, _sanitized_git_environment_v5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.provider import (
    ControllerRoleTerminalAuthorityV5,
    ExistingPersistedRoleRequestV5,
    parse_and_bind_role_artifact,
)

if TYPE_CHECKING:
    from core.pit_optimizer_v5.historical_verification import (
        CompletedHistoricalRoundResourcesV5,
    )
    from core.pit_optimizer_v5.manifest import AuthenticatedCampaignManifestV5
    from core.pit_optimizer_v5.memory import ExperimentRecordV5
    from core.pit_optimizer_v5.operations import CampaignLaunchV5
    from core.pit_optimizer_v5.provider import RoleInvocationPackageV5

PATH_FIELDS = (
    "source_root",
    "workspace_root",
    "data_root",
    "output_root",
    "control_root",
    "git_executable",
    "docker_executable",
)


def require_development_v5(manifest):
    if (
        manifest.pit_data_scope != "development_sp500_v2"
        or manifest.semantic_mode != "disabled_development"
        or manifest.apply
        or manifest.qualification_allowed
        or manifest.full_replay_allowed
    ):
        raise ValueError("explicit semantic-disabled development manifest required")

    if manifest.provider is not None and (
        manifest.provider.automatic_retries != 0 or manifest.provider.schema_repair_calls != 0
    ):
        raise ValueError("development provider retries and repair calls must be zero")


def require_controller_development_v5(manifest):
    require_development_v5(manifest)
    if manifest.provider is not None:
        raise ValueError("controller exchange requires provider-free development")


@dataclass(frozen=True, slots=True)
class DevelopmentAdapterConfigV5:
    schema_version: int
    campaign_manifest_sha256: str
    repository_root_identity_sha256: str
    candidate_base_identity_sha256: str
    workspace_driver_identity_sha256: str
    mount_factory_identity_sha256: str
    container_executor_identity_sha256: str
    source_root: str
    workspace_root: str
    data_root: str
    output_root: str
    control_root: str
    git_executable: str
    docker_executable: str
    response_directory: str
    export_mode: str = "policy_only"

    def __post_init__(self):
        from core.pit_optimizer_v5.cli import _canonical_windows_path_v5, _DIGEST

        if type(self.schema_version) is not int or self.schema_version != 5 or self.export_mode != "policy_only":
            raise ValueError("development config schema/export mode invalid")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("sha256") and (type(value) is not str or not _DIGEST.fullmatch(value)):
                raise ValueError("development config identity invalid")
        for field in (*PATH_FIELDS, "response_directory"):
            _canonical_windows_path_v5(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class ProviderDevelopmentAdapterConfigV5:
    schema_version: int
    campaign_manifest_sha256: str
    repository_root_identity_sha256: str
    candidate_base_identity_sha256: str
    workspace_driver_identity_sha256: str
    mount_factory_identity_sha256: str
    container_executor_identity_sha256: str
    source_root: str
    workspace_root: str
    data_root: str
    output_root: str
    control_root: str
    git_executable: str
    docker_executable: str
    gateway_identity_sha256: str
    ledger_identity_sha256: str
    audit_store_identity_sha256: str
    api_key_environment_variable: str = "OPENROUTER_API_KEY"
    export_mode: str = "policy_only"

    def __post_init__(self):
        from core.pit_optimizer_v5.cli import _canonical_windows_path_v5, _DIGEST

        if type(self.schema_version) is not int or self.schema_version != 5 or self.export_mode != "policy_only":
            raise ValueError("development config schema/export mode invalid")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("sha256") and (type(value) is not str or not _DIGEST.fullmatch(value)):
                raise ValueError("development config identity invalid")
        for field in PATH_FIELDS:
            _canonical_windows_path_v5(getattr(self, field), field)
        if self.api_key_environment_variable != "OPENROUTER_API_KEY":
            raise ValueError("development provider secret handle is invalid")


@dataclass(frozen=True, slots=True)
class _CompletedDevelopmentHistoryV5:
    """Data-only result of the complete immutable history authentication path."""

    authenticated_manifest: AuthenticatedCampaignManifestV5
    campaign_launch: CampaignLaunchV5
    original_config: DevelopmentAdapterConfigV5 | ProviderDevelopmentAdapterConfigV5
    round_indices: tuple[int, ...]
    rounds: tuple[CompletedHistoricalRoundResourcesV5, ...]
    records: tuple[ExperimentRecordV5, ...]
    packages: tuple[RoleInvocationPackageV5, ...]

    def __post_init__(self) -> None:
        from core.pit_optimizer_v5.historical_verification import (
            CompletedHistoricalRoundResourcesV5,
        )
        from core.pit_optimizer_v5.manifest import AuthenticatedCampaignManifestV5
        from core.pit_optimizer_v5.memory import ExperimentRecordV5
        from core.pit_optimizer_v5.operations import CampaignLaunchV5
        from core.pit_optimizer_v5.provider import RoleInvocationPackageV5

        if (
            type(self.authenticated_manifest) is not AuthenticatedCampaignManifestV5
            or type(self.campaign_launch) is not CampaignLaunchV5
            or type(self.original_config)
            not in {DevelopmentAdapterConfigV5, ProviderDevelopmentAdapterConfigV5}
            or type(self.round_indices) is not tuple
            or not self.round_indices
            or self.round_indices != tuple(sorted(set(self.round_indices)))
            or type(self.rounds) is not tuple
            or any(type(item) is not CompletedHistoricalRoundResourcesV5 for item in self.rounds)
            or tuple(item.round_authority.owner.round_index for item in self.rounds)
            != self.round_indices
            or type(self.records) is not tuple
            or any(type(item) is not ExperimentRecordV5 for item in self.records)
            or type(self.packages) is not tuple
            or any(type(item) is not RoleInvocationPackageV5 for item in self.packages)
        ):
            raise ValueError("completed development history facts are invalid")


def development_config_type_v5(manifest):
    require_development_v5(manifest)
    return DevelopmentAdapterConfigV5 if manifest.provider is None else ProviderDevelopmentAdapterConfigV5


def development_config_paths_v5(config):
    if type(config) is DevelopmentAdapterConfigV5:
        return (*PATH_FIELDS, "response_directory")
    if type(config) is ProviderDevelopmentAdapterConfigV5:
        return (*PATH_FIELDS, "api_key_environment_variable")
    raise ValueError("development config type invalid")


@dataclass(frozen=True, slots=True)
class DevelopmentRoundCompositionV5:
    inputs: object
    dependencies: object

    def validate_for(self, *, repository, authorities, round_index, owner_token_sha256, adapter_config):
        from core.pit_optimizer_v5.cli import (
            _exact_production_host_adapter_graph_v5,
            LocalRoleRequestFactoryV5,
            SelectionNoveltyResolverV5,
            CanonicalExperimentRecordFactoryV5,
            LocalArchiveReducerFactoryV5,
            SystemMonotonicClockV5,
            ControllerCancellationV5,
            CompositeOwnedCleanupV5,
        )
        from core.pit_optimizer_v5.runtime import FeedbackRoundInputV5, FeedbackRoundDependenciesV5

        require_development_v5(authorities.manifest)
        if (
            type(adapter_config) is not development_config_type_v5(authorities.manifest)
            or adapter_config.campaign_manifest_sha256 != authorities.manifest.sha256
            or adapter_config.repository_root_identity_sha256 != repository.root_identity_sha256
            or type(self.dependencies) is not FeedbackRoundDependenciesV5
        ):
            raise ValueError("development configuration mode or identity differs from manifest")
        if authorities.manifest.provider is None:
            invoker = self.dependencies.invoker
            if (
                type(invoker) is not FileBackedControllerRoleInvokerV5
                or invoker._repository is not repository
                or invoker._response_directory != Path(adapter_config.response_directory)
            ):
                raise ValueError("development controller graph differs from its authority")
        else:
            from core.pit_optimizer_v5.cli import _require_paid_adapter_graph_v5

            _require_paid_adapter_graph_v5(
                self.dependencies.invoker, repository=repository,
                authorities=authorities, adapter_config=adapter_config,
            )
        if (
            authorities.baseline.pit_data_scope != "development_sp500_v2"
            or authorities.baseline.semantic_mode != "disabled_development"
            or self.inputs
            != FeedbackRoundInputV5(
                authorities.manifest,
                authorities.panel_plan,
                authorities.evaluator_contract,
                authorities.baseline,
                round_index,
                owner_token_sha256,
            )
            or type(self.dependencies) is not FeedbackRoundDependenciesV5
            or self.dependencies.persistence is not repository
            or not _exact_production_host_adapter_graph_v5(
                self.dependencies.candidates, adapter_config, repository=repository, authorities=authorities
            )
        ):
            raise ValueError("development adapter graph differs from its authority")
        if (
            type(self.dependencies.requests) is not LocalRoleRequestFactoryV5
            or self.dependencies.requests.repository is not repository
            or self.dependencies.requests.manifest is not authorities.manifest
            or type(self.dependencies.novelty) is not SelectionNoveltyResolverV5
            or type(self.dependencies.records) is not CanonicalExperimentRecordFactoryV5
            or type(self.dependencies.archive_reducers) is not LocalArchiveReducerFactoryV5
            or self.dependencies.archive_reducers.repository is not repository
            or type(self.dependencies.clock) is not SystemMonotonicClockV5
            or type(self.dependencies.cancellation) is not ControllerCancellationV5
            or type(self.dependencies.cleanup) is not CompositeOwnedCleanupV5
        ):
            raise ValueError("development shared runtime dependencies are invalid")
        candidate = self.dependencies.candidates
        cleanup = self.dependencies.cleanup
        if (
            candidate.owner.round_index != round_index
            or candidate.owner.owner_token_sha256 != owner_token_sha256
            or cleanup.owner is not candidate.owner
            or cleanup.materializer is not candidate.base_operations.materializer
            or cleanup.executor is not candidate.panel_evaluator.evaluator.executor
            or cleanup.clock is not self.dependencies.clock
        ):
            raise ValueError("development cleanup/owner differs from runtime")


def compose_development_round_v5(
    *, repository, authorities, response_directory=None,
    api_key_environment_variable="OPENROUTER_API_KEY", **kwargs
):
    from core.pit_optimizer_v5.cli import _compose_round_from_paths_v5, _compose_paid_invoker_v5

    require_development_v5(authorities.manifest)
    if authorities.manifest.provider is None:
        if response_directory is None:
            raise ValueError("controller development requires response directory")
        invoker = FileBackedControllerRoleInvokerV5(
            repository=repository, manifest=authorities.manifest, response_directory=Path(response_directory)
        )
    else:
        if response_directory is not None:
            raise ValueError("provider development cannot accept controller responses")
        invoker = _compose_paid_invoker_v5(
            repository=repository, manifest=authorities.manifest,
            api_key_environment_variable=api_key_environment_variable,
        )
    composed = _compose_round_from_paths_v5(
        repository=repository, authorities=authorities, invoker=invoker, export_mode="policy_only", **kwargs
    )
    return DevelopmentRoundCompositionV5(composed.inputs, composed.dependencies)


class DevelopmentRoundFactoryV5:
    def __init__(self, *, adapter_config_ref, adapter_config):
        if (
            type(adapter_config) not in {DevelopmentAdapterConfigV5, ProviderDevelopmentAdapterConfigV5}
            or adapter_config_ref.sha256 != canonical_sha256_v5(adapter_config)
        ):
            raise ValueError("development factory config identity invalid")
        self.adapter_config = adapter_config

    def compose_round(self, *, repository, authorities, round_index, owner_token_sha256, adapter_config):
        if (
            type(adapter_config) is not development_config_type_v5(authorities.manifest)
            or adapter_config != self.adapter_config
            or adapter_config.campaign_manifest_sha256 != authorities.manifest.sha256
            or adapter_config.repository_root_identity_sha256 != repository.root_identity_sha256
        ):
            raise ValueError("development factory authority mismatch")
        result = compose_development_round_v5(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            **{name: getattr(adapter_config, name) for name in development_config_paths_v5(adapter_config)},
        )
        result.validate_for(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            adapter_config=adapter_config,
        )
        return result


def load_development_config_v5(repository, authorities, reference):
    require_development_v5(authorities.manifest)
    config = repository.load_typed_artifact(reference, value_type=development_config_type_v5(authorities.manifest))
    if (
        config.campaign_manifest_sha256 != authorities.manifest.sha256
        or config.repository_root_identity_sha256 != repository.root_identity_sha256
    ):
        raise ValueError("development configuration authority mismatch")
    return config


def capture_development_policy_v5(*, source_root, git_executable, expected_source_commit):
    """Read only immutable four-file blobs; host HEAD may advance independently."""
    import re

    root, executable = Path(source_root), Path(git_executable)
    if (
        not root.is_absolute()
        or not executable.is_absolute()
        or not re.fullmatch("[0-9a-f]{40}", expected_source_commit)
    ):
        raise ValueError("explicit absolute Git/source paths and immutable commit required")

    def git(*args):
        return subprocess.run(
            (str(executable), "-C", str(root), *args),
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=_sanitized_git_environment_v5(),
        ).stdout

    if (
        git("rev-parse", "--verify", expected_source_commit + "^{commit}").decode("ascii").strip()
        != expected_source_commit
    ):
        raise ValueError("source commit identity differs")
    tree = git("ls-tree", "-rz", "--full-tree", expected_source_commit, "--", *EDITABLE_POLICY_PATHS_V5)
    members = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, _ = metadata.split(b" ")
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError("development source requires regular policy blobs")
        members.append(path.decode("utf-8"))
    if len(members) != 4 or set(members) != set(EDITABLE_POLICY_PATHS_V5):
        raise ValueError("immutable policy membership differs")
    sources = {
        path: git("show", "--no-ext-diff", "--no-textconv", f"{expected_source_commit}:{path}")
        for path in EDITABLE_POLICY_PATHS_V5
    }
    return PolicySourceSnapshotV5.from_tracked_bytes(
        source_commit=expected_source_commit, source_by_path=sources
    ), sources


def _authenticate_controller_request(repository, manifest, reference, *, repair: bool = True):
    from core.pit_optimizer_v5.operations import CampaignLaunchV5, CampaignRoundLaunchV5
    from core.pit_optimizer_v5.contracts import CampaignPanelPlanV5

    require_controller_development_v5(manifest)
    call, request = repository._load_role_request_entry(reference)
    launch = repository.load_typed_state(
        namespace="campaign-launch", key=manifest.sha256, value_type=CampaignLaunchV5, repair=repair
    )
    round_key = canonical_sha256_v5({"campaign_manifest_sha256": manifest.sha256, "round_index": call.round_index})
    started = repository.load_typed_state(
        namespace="campaign-round-started", key=round_key, value_type=CampaignRoundLaunchV5, repair=repair
    )
    if (
        call.campaign_id != manifest.campaign_id
        or call.round_index > manifest.search.max_feedback_rounds
        or launch is None
        or launch.manifest_ref.sha256 != manifest.sha256
        or started is None
        or started.started_epoch_ms is None
        or started.owner_token_sha256 != launch.owner_token_sha256
    ):
        raise ValueError("controller call lacks manifest-bound campaign/round launch")
    plan = repository.load_typed_artifact(manifest.panel_plan_ref, value_type=CampaignPanelPlanV5)
    if request.expected_binding.discovery_plan_sha256 != plan.discovery_plan_sha256:
        raise ValueError("controller request discovery binding differs")
    return ExistingPersistedRoleRequestV5(reference, call, request)


def authenticate_controller_packages_v5(repository, manifest, packages):
    require_controller_development_v5(manifest)
    for package in packages:
        if type(package.terminal_authority) is not ControllerRoleTerminalAuthorityV5:
            raise ValueError("development accepts only controller terminal provenance")
        reference = ArtifactRefV5(
            f"roles/requests/{package.call.sha256}.json",
            hashlib.sha256(repository._read_relative(f"roles/requests/{package.call.sha256}.json")).hexdigest(),
        )
        persisted = _authenticate_controller_request(repository, manifest, reference)
        if (
            persisted.call != package.call
            or persisted.request != package.request
            or repository.load_controller_role_response(call=package.call) is None
        ):
            raise ValueError("controller package request/response unavailable")
        invoker = FileBackedControllerRoleInvokerV5(
            repository=repository, manifest=manifest, response_directory=Path.cwd()
        )
        if invoker.reconcile_once(persisted) != package:
            raise ValueError("controller package differs from exact sealed response")


def authenticate_existing_controller_packages_v5(repository, manifest, packages) -> None:
    require_controller_development_v5(manifest)
    for package in packages:
        if type(package.terminal_authority) is not ControllerRoleTerminalAuthorityV5:
            raise ValueError("development accepts only controller terminal provenance")
        reference = ArtifactRefV5(
            f"roles/requests/{package.call.sha256}.json",
            hashlib.sha256(repository._read_relative(f"roles/requests/{package.call.sha256}.json")).hexdigest(),
        )
        persisted = _authenticate_controller_request(repository, manifest, reference, repair=False)
        sealed = repository.load_controller_role_response(call=package.call)
        if persisted.call != package.call or persisted.request != package.request or sealed is None:
            raise ValueError("controller package request/response unavailable")
        response_ref, raw = sealed
        if _classify_controller_response_v5(persisted=persisted, reference=response_ref, raw=raw) != package:
            raise ValueError("controller package differs from exact sealed response")


def _authenticate_completed_development_history_facts_v5(
    repository,
    manifest,
) -> _CompletedDevelopmentHistoryV5:
    """Return facts after the complete immutable history authentication path."""

    from core.pit_optimizer_v5.contracts import CampaignManifestV5
    from core.pit_optimizer_v5.historical_verification import (
        _authenticate_completed_history_census_v5,
        _campaign_authorities,
        _existing_round_state_v5,
        authenticate_completed_historical_round_resources_v5,
        authenticate_historical_round_v5,
    )
    from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
    from core.pit_optimizer_v5.memory import RoundOutcomePayloadV5
    from core.pit_optimizer_v5.operations import CampaignLaunchV5
    from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5

    if type(repository) is not LocalArtifactRepositoryV5 or type(manifest) is not CampaignManifestV5:
        raise ValueError("completed development history authority is invalid")
    require_development_v5(manifest)
    launch = repository.load_typed_state(
        namespace="campaign-launch",
        key=manifest.sha256,
        value_type=CampaignLaunchV5,
        repair=False,
    )
    if launch is None or launch.manifest_ref.sha256 != manifest.sha256:
        raise ValueError("completed development history lacks its original campaign launch")
    authenticated = authenticate_campaign_manifest_v5(
        repository=repository,
        manifest_ref=launch.manifest_ref,
    )
    if authenticated.manifest != manifest:
        raise ValueError("completed development history differs from its original manifest")
    authorities = _campaign_authorities(authenticated)
    original_config = load_development_config_v5(
        repository,
        authorities,
        launch.adapter_config_ref,
    )

    round_indices = _existing_round_state_v5(repository, manifest)
    if not round_indices:
        raise ValueError("completed development history has no settled round")
    rounds = []
    for round_index in round_indices:
        round_authority = authenticate_historical_round_v5(
            repository=repository,
            manifest=manifest,
            round_index=round_index,
        )
        completed = authenticate_completed_historical_round_resources_v5(
            repository=repository,
            manifest=manifest,
            round_index=round_index,
        )
        if (
            completed.round_authority != round_authority
            or round_authority.authenticated_manifest != authenticated
            or round_authority.campaign_launch != launch
            or round_authority.original_config_ref != launch.adapter_config_ref
            or round_authority.original_config != original_config
        ):
            raise ValueError("completed round differs from its original H1 authority")
        rounds.append(completed)

    checkpoint = repository.load_checkpoint()
    records = (
        ()
        if checkpoint is None
        else tuple(repository.load_experiment(reference) for reference in checkpoint.record_refs)
    )
    actual_rounds = set(round_indices)
    if any(record.round_index not in actual_rounds for record in records):
        raise ValueError("checkpoint record belongs to an unstarted historical round")
    record_rounds = {record.round_index for record in records}
    for completed in rounds:
        round_index = completed.round_authority.owner.round_index
        outcomes = tuple(
            payload
            for payload in completed.payloads
            if type(payload) is RoundOutcomePayloadV5
        )
        if any(
            outcome.campaign_id != manifest.campaign_id
            or outcome.round_index != round_index
            for outcome in outcomes
        ):
            raise ValueError("historical terminal outcome is foreign")
        if len(outcomes) > 1:
            raise ValueError("historical terminal outcome is duplicated")
        if round_index not in record_rounds and not outcomes:
            raise ValueError("historical round lacks a checkpoint record or terminal outcome")

    packages = _authenticate_completed_history_census_v5(
        repository=repository,
        manifest=manifest,
        rounds=tuple(rounds),
    )
    if manifest.provider is None:
        authenticate_existing_controller_packages_v5(
            repository,
            manifest,
            packages,
        )
    else:
        from core.pit_optimizer_v5.production_provider import (
            authenticate_paid_role_history_v5,
        )

        authenticate_paid_role_history_v5(
            repository=repository,
            manifest=manifest,
            adapter_config=original_config,
            packages=packages,
        )
    LocalArchiveReducerFactoryV5(repository).verify_projection(
        manifest=manifest,
        panel_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        repair=False,
    )
    return _CompletedDevelopmentHistoryV5(
        authenticated_manifest=authenticated,
        campaign_launch=launch,
        original_config=original_config,
        round_indices=round_indices,
        rounds=tuple(rounds),
        records=records,
        packages=packages,
    )


def authenticate_completed_development_history_v5(repository, manifest) -> None:
    """Authenticate one completed development campaign without repair or adapters."""

    facts = _authenticate_completed_development_history_facts_v5(repository, manifest)
    from core.pit_optimizer_v5.campaign_admission import (
        _authenticate_completed_campaign_policy_v5,
    )

    _authenticate_completed_campaign_policy_v5(
        repository=repository,
        manifest=manifest,
        history=facts,
        expected_policy_ref=None,
    )


def authenticate_development_history_v5(repository, manifest):
    """Authenticate controller or paid history and resources using the original saved launch."""
    from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
    from core.pit_optimizer_v5.operations import CampaignLaunchV5, _authorities
    from core.pit_optimizer_v5.cli import ProductionV5CommandServices

    require_development_v5(manifest)
    launch = repository.load_typed_state(namespace="campaign-launch", key=manifest.sha256, value_type=CampaignLaunchV5)
    if launch is None or launch.manifest_ref.sha256 != manifest.sha256:
        raise ValueError("development history lacks its original campaign launch")
    authenticated = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=launch.manifest_ref)
    if authenticated.manifest != manifest:
        raise ValueError("development summary differs from original launch manifest")
    authorities = _authorities(authenticated)
    config = load_development_config_v5(repository, authorities, launch.adapter_config_ref)
    ProductionV5CommandServices._verify_local_run(repository, authorities, config)


def prepare_development_campaign_v5(
    *,
    repository,
    source_root,
    source_commit,
    git_executable,
    campaign_id,
    target_pct,
    quick_panel_ref,
    parent_results,
    output_prefix,
    evaluator_contract_sha256,
    sandbox_profile_sha256,
    caps,
    provider=None,
    validate_only=False,
    panel_plan_ref=None,
):
    """Authenticate four actual pairs before publishing any campaign authority."""
    from decimal import Decimal
    from core.pit_optimizer_v5.contracts import ProviderCapabilitiesV5

    if provider is not None and (
        type(provider) is not ProviderCapabilitiesV5
        or provider.automatic_retries != 0
        or provider.schema_repair_calls != 0
    ):
        raise ValueError("development provider capabilities invalid")
    from core.pit_optimizer_v5.candidate_ir import SourceBundleV5, SourceFileV5
    from core.pit_optimizer_v5.container_protocol import (
        decode_panel_execution_request_v5,
        decode_panel_execution_output_v5,
        panel_execution_output_bytes_v5,
    )
    from core.pit_optimizer_v5.contracts import (
        AnnualizedReturnTargetV5,
        CampaignPanelPlanV5,
        EpisodePlanV5,
        EpisodeEvaluationV5,
        CampaignEvidenceV5,
        ResourceCapabilitiesV5,
        SearchCapabilitiesV5,
        selected_scenario,
        validate_campaign_evidence_v5,
        validate_episode_plan_panel_v5,
        validate_sandbox_profile_resources_v5,
    )
    from core.pit_optimizer_v5.manifest import build_campaign_manifest_v5
    from core.pit_optimizer_v5.search import BaselineParentAuthorityV5, campaign_cagr_pct

    if not repository.root.is_absolute() or len(parent_results) != 4:
        raise ValueError("absolute repository and four real parent pairs required")
    ArtifactRefV5(output_prefix + "/manifest.json", "0" * 64)
    pairs = []
    for ordinal, item in enumerate(parent_results, 1):
        paths = (Path(item["request_path"]), Path(item["output_path"]))
        if any(not path.is_absolute() or not path.is_file() or path.is_symlink() for path in paths):
            raise ValueError(f"parent {ordinal} requires absolute regular request/output files")
        request_raw, output_raw = (path.read_bytes() for path in paths)
        request = decode_panel_execution_request_v5(request_raw)
        output = decode_panel_execution_output_v5(output_raw)
        if (
            request.pit_data_scope != "development_sp500_v2"
            or request.episode.episode_ordinal != ordinal
            or request.episode.episode_id != f"development-discovery-{ordinal}"
            or panel_execution_output_bytes_v5(request=request, evaluation=output.evaluation) != output_raw
        ):
            raise ValueError(f"parent {ordinal} request/output identity differs")
        pairs.append((request, output, request_raw, output_raw))
    first = pairs[0][0]
    if any(
        (
            request.evaluator_contract,
            request.sandbox_profile,
            request.execution_profile,
            request.policy_revision,
            request.policy_method_timeout_seconds,
            request.worker_startup_timeout_seconds,
        )
        != (
            first.evaluator_contract,
            first.sandbox_profile,
            first.execution_profile,
            first.policy_revision,
            first.policy_method_timeout_seconds,
            first.worker_startup_timeout_seconds,
        )
        for request, _, _, _ in pairs
    ):
        raise ValueError("parent executions do not share one exact evaluator/policy/resource authority")
    contract, profile = first.evaluator_contract, first.sandbox_profile
    if contract.sha256 != evaluator_contract_sha256 or profile.sha256 != sandbox_profile_sha256:
        raise ValueError("parent executions differ from explicitly selected evaluator/sandbox identities")
    if (
        tuple(item.scenario_id for item in contract.friction_grid) != ("gross", "base", "stress")
        or contract.selection_scenario_id != "base"
    ):
        raise ValueError("development parent requires gross/base/stress and base selection")
    snapshot, sources = capture_development_policy_v5(
        source_root=source_root, git_executable=git_executable, expected_source_commit=source_commit
    )
    bundle = SourceBundleV5(tuple(SourceFileV5(path, raw.decode("utf-8")) for path, raw in sources.items()))
    if (
        snapshot.editable_source_sha256 != first.policy_revision.editable_source_sha256
        or bundle.sha256 != contract.baseline_source_bundle_sha256
        or first.policy_revision.sha256 != contract.baseline_policy_revision_sha256
    ):
        raise ValueError("immutable policy bytes differ from actual parent/evaluator")
    data_refs = (
        ArtifactRefV5("data/pit_bundle.sqlite3", contract.pit_bundle_sha256),
        ArtifactRefV5("data/prices_provenance.json", contract.prices_provenance_sha256),
    )
    for reference in data_refs:
        repository.authenticate_raw_artifact(reference)
    quick = repository.load_evaluation_panel_spec(quick_panel_ref)
    if quick.purpose != "quick":
        raise ValueError("quick reference must identify the actual quick panel")
    target = AnnualizedReturnTargetV5(Decimal(target_pct))
    # Unused local descriptors carry truthful content identities, no held-out authority or evaluation.
    local_descriptors = {
        name: {
            "schema_version": 5,
            "pit_data_scope": "development_sp500_v2",
            "purpose": name,
            "executed": False,
            "capability_enabled": False,
            "quick_panel_sha256": quick.sha256,
        }
        for name in ("confirmation", "qualification")
    }
    # Existing panel schema permits quick/discovery/qualification only. Reuse actual quick
    # content for the disabled mechanics slot, without claiming mechanics execution.
    mechanics = quick
    mechanics_ref = quick_panel_ref

    def episode(panel, reference, name, ordinal=None):
        return EpisodePlanV5(
            name,
            ordinal,
            panel.purpose,
            panel.start_date,
            panel.end_date,
            tuple(row.security_lineage_id for row in panel.lineages),
            reference,
        )

    planned = tuple(
        replace(
            request.episode,
            panel_ref=ArtifactRefV5(f"{output_prefix}/panels/discovery-{index}.json", request.panel.sha256),
        )
        for index, (request, _, _, _) in enumerate(pairs, 1)
    )
    plan = CampaignPanelPlanV5(
        5,
        *data_refs,
        canonical_sha256_v5(
            {"scope": "development_sp500_v2", "panels": tuple(item.panel_ref.sha256 for item in planned)}
        ),
        target.sha256,
        episode(mechanics, mechanics_ref, "development-mechanics-unexecuted"),
        episode(quick, quick_panel_ref, "development-quick"),
        planned,
        canonical_sha256_v5(local_descriptors["confirmation"]),
        canonical_sha256_v5(local_descriptors["qualification"]),
    )
    if panel_plan_ref is not None:
        plan = repository.load_typed_artifact(panel_plan_ref, value_type=CampaignPanelPlanV5)
        if plan.sha256 != panel_plan_ref.sha256:
            raise ValueError("supplied panel plan identity differs")
        # Authenticate every existing child before preflight success or create-only writes.
        for reference in (plan.pit_bundle_ref, plan.prices_provenance_ref):
            repository.authenticate_raw_artifact(reference)
        panel_children = tuple(
            repository.load_evaluation_panel_spec(item.panel_ref)
            for item in (plan.mechanics, plan.quick, *plan.discovery)
        )
    else:
        panel_children = (mechanics, quick, *(request.panel for request, _, _, _ in pairs))
    if plan.target_sha256 != target.sha256:
        raise ValueError("panel plan target differs from the supplied target")
    for planned_episode, panel_child in zip((plan.mechanics, plan.quick, *plan.discovery), panel_children, strict=True):
        validate_episode_plan_panel_v5(planned_episode, panel_child)
        if planned_episode.purpose != panel_child.purpose:
            raise ValueError("panel purpose differs from its authenticated plan")
    # Imported path text may differ; actual content identity/dates remain mandatory below.
    episodes = tuple(
        EpisodeEvaluationV5(
            request.episode.episode_id, index, request.episode.start_date, request.episode.end_date, output.evaluation
        )
        for index, (request, output, _, _) in enumerate(pairs, 1)
    )
    evidence = CampaignEvidenceV5(
        plan.discovery_plan_sha256,
        episodes,
        campaign_cagr_pct(episodes=episodes, discovery_plan=plan, evaluator_contract=contract),
        sum(selected_scenario(item.evaluation).report.closed_trades for item in episodes),
    )
    validate_campaign_evidence_v5(
        evidence, panel_plan=plan, evaluator_contract=contract, policy_identity_sha256=first.policy_revision.sha256
    )
    resources = ResourceCapabilitiesV5(
        max_parallel_evaluations=1,
        evaluation_cpu_limit=profile.cpu_limit,
        evaluation_memory_mib=profile.memory_limit_mib,
        evaluation_pid_limit=profile.pid_limit,
        evaluation_output_limit_bytes=profile.output_limit_bytes,
        policy_method_timeout_seconds=first.policy_method_timeout_seconds,
        worker_startup_timeout_seconds=first.worker_startup_timeout_seconds,
        quick_timeout_seconds=600,
        discovery_episode_timeout_seconds=600,
        round_wall_timeout_seconds=7200,
        campaign_wall_timeout_seconds=14400,
    )
    expected_cap_names = {
        "quick_timeout_seconds",
        "discovery_episode_timeout_seconds",
        "round_wall_timeout_seconds",
        "campaign_wall_timeout_seconds",
    }
    if type(caps) is not dict or set(caps) != expected_cap_names:
        raise ValueError("caps must explicitly supply quick, discovery, round and campaign timeouts")
    resources = replace(resources, **caps)
    validate_sandbox_profile_resources_v5(profile, resources)
    search = SearchCapabilitiesV5(
        hypotheses_per_investigator=1,
        max_tunable_axes=1,
        max_variants_per_template=1,
        max_discovery_survivors_per_template=1,
        archive_capacity=2,
        max_feedback_rounds=2,
        investigator_memory_max_bytes=96 * 1024,
        allow_full_source_escape=False,
    )
    if validate_only:
        return {
            "status": "validated",
            "provider": provider,
            "resources": resources,
            "search": search,
            "parent_policy_sha256": first.policy_revision.sha256,
            "campaign_cagr_pct": evidence.campaign_cagr_pct,
        }

    def create(name, value):
        path = output_prefix + "/" + name + ".json"
        return (
            repository._create_only(path, value)
            if type(value) is dict
            else repository.create_typed_artifact(path, value)
        )

    # All real outputs and immutable source/data identities passed before the first write.
    provenance = []
    for index, (request, _output, request_raw, output_raw) in enumerate(pairs, 1):
        refs = tuple(
            repository.append_binary_state(
                namespace="development-parent-originals", key=hashlib.sha256(raw).hexdigest(), content=raw
            )
            for raw in (request_raw, output_raw)
        )
        provenance.append(
            {"episode_id": request.episode.episode_id, "original_request_ref": refs[0], "original_output_ref": refs[1]}
        )
        if panel_plan_ref is None:
            repository.create_evaluation_panel_spec(f"{output_prefix}/panels/discovery-{index}.json", request.panel)
    if panel_plan_ref is None:
        for name, descriptor in local_descriptors.items():
            create(name + "-unexecuted", descriptor)
        panel_plan_ref = create("panel-plan", plan)
    create("parent-provenance", {"schema_version": 5, "pairs": tuple(provenance)})
    source_ref = create("source-bundle", bundle)
    policy_ref = create("policy-revision", first.policy_revision)
    baseline = BaselineParentAuthorityV5(
        first.policy_revision,
        policy_ref,
        None,
        evidence,
        bundle,
        source_ref,
        "development_sp500_v2",
        "disabled_development",
    )
    baseline_ref = create("baseline-parent", baseline)
    return build_campaign_manifest_v5(
        repository=repository,
        campaign_id=campaign_id,
        target=target,
        source_snapshot=snapshot,
        execution_profile_ref=create("execution-profile", first.execution_profile),
        evaluator_contract_ref=create("evaluator-contract", contract),
        baseline_authority_ref=baseline_ref,
        panel_plan_ref=panel_plan_ref,
        sandbox_profile_ref=create("sandbox-profile", profile),
        policy_scope_path=output_prefix + "/policy-scope.json",
        manifest_path=output_prefix + "/manifest.json",
        search=search,
        resources=resources,
        provider=provider,
        pit_data_scope="development_sp500_v2",
        semantic_mode="disabled_development",
    )


def dispatch_development_cli_v5(argv, *, emit=print):
    from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5

    from core.pit_optimizer_v5.cli import build_parser_v5

    args = build_parser_v5().parse_args(argv)
    try:
        repository = LocalArtifactRepositoryV5(Path(args.artifact_root))
        if args.command == "import-development":
            path = Path(args.input_file)
            if not path.is_absolute():
                raise ValueError("import input file must be absolute")
            inputs = json.loads(path.read_text(encoding="utf-8-sig"))
            if inputs.get("provider") is not None:
                from core.pit_optimizer_v5.artifacts import _decode_dataclass
                from core.pit_optimizer_v5.contracts import ProviderCapabilitiesV5

                inputs["provider"] = _decode_dataclass(ProviderCapabilitiesV5, inputs["provider"])
            inputs["quick_panel_ref"] = ArtifactRefV5(**inputs["quick_panel_ref"])
            if inputs.get("panel_plan_ref") is not None:
                inputs["panel_plan_ref"] = ArtifactRefV5(**inputs["panel_plan_ref"])
            result = prepare_development_campaign_v5(repository=repository, validate_only=args.validate_only, **inputs)
            if not args.validate_only:
                result = {
                    "status": "prepared",
                    "manifest_ref": result.manifest_ref,
                    "resources": result.manifest.resources,
                    "search": result.manifest.search,
                    "provider": result.manifest.provider,
                }
        else:
            authenticated = authenticate_campaign_manifest_v5(
                repository=repository, manifest_ref=ArtifactRefV5(args.manifest_path, args.manifest_sha256)
            )
            reference = ArtifactRefV5(args.request_path, args.request_sha256)
            persisted = _authenticate_controller_request(repository, authenticated.manifest, reference)
            if args.command == "controller-request":
                emit(repository.authenticate(reference).content.decode("utf-8"))
                return 0
            path = Path(args.response_file)
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise ValueError("response file must be an absolute regular file")
            with path.open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            envelope = _decode_unique_json(raw)
            if (
                set(envelope) != {"schema_version", "artifact_type", "call_key_sha256", "request_sha256", "response"}
                or envelope["schema_version"] != 5
                or envelope["artifact_type"] != "controller_role_response"
                or envelope["call_key_sha256"] != persisted.call.sha256
                or envelope["request_sha256"] != persisted.request.sha256
                or type(envelope["response"]) is not dict
            ):
                raise ValueError("response envelope differs from original persisted call")
            parse_and_bind_role_artifact(
                request=persisted.request, response_text=json.dumps(envelope["response"], ensure_ascii=False)
            )
            response_ref = repository.append_controller_role_response(call=persisted.call, content=raw)
            result = {
                "status": "response_imported",
                "response_ref": response_ref,
                "request_ref": reference,
                "role": persisted.call.role,
                "round_index": persisted.call.round_index,
            }
        emit("PIT_OPTIMIZER_V5_DEVELOPMENT=" + canonical_json_bytes_v5(result).decode("utf-8"))
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        emit(
            "PIT_OPTIMIZER_V5_DEVELOPMENT="
            + canonical_json_bytes_v5({"status": "failed", "diagnostic": str(exc)}).decode("utf-8")
        )
        return 2
