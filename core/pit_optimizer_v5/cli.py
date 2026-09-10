"""Thin local command boundary for the PIT optimizer V5 runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from decimal import Decimal
import ntpath
from pathlib import Path, PureWindowsPath
import re
import secrets
from typing import Callable, Literal, Protocol, Sequence, runtime_checkable

from core.pit_optimizer_v5.artifacts import ArtifactRepositoryFailureV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    ModelPriceUpperBoundV5,
    ProviderCapabilitiesV5,
    ResourceCapabilitiesV5,
    SandboxProfileV5,
    SearchCapabilitiesV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    validate_campaign_manifest_bindings_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.manifest import (
    AuthenticatedCampaignManifestV5,
    authenticate_campaign_manifest_v5,
    build_campaign_manifest_v5,
    capture_clean_policy_snapshot_v5,
    provider_capabilities_from_values_v5,
    render_discovery_command_v5,
)
from core.pit_optimizer_v5.panels import (
    build_panels_v5,
    initialize_stage_ledgers_v5,
    verify_panels_v5,
)
from core.pit_optimizer_v5.provider import (
    AuthorizedRoleRunnerV5,
    GatewayCompletionProviderV5,
    LedgerBackedRoleInvokerV5,
    RoleInvocationPackageV5,
)
from core.pit_optimizer_v5.production_provider import (
    LocalRoleAuthorizationLedgerV5,
    OpenRouterOneShotJsonCompletionV5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundDependenciesV5,
    FeedbackRoundInputV5,
    FeedbackRoundResultV5,
    PaidRoleRecoveryRequiredV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.memory import (
    CandidateExecutionAuthorityV5,
    CandidateExecutionKeyV5,
    CandidateStageResultPayloadV5,
    CleanupResultPayloadV5,
    EpisodeEvidencePayloadV5,
    QuickEvidencePayloadV5,
    ResourceLeasePayloadV5,
    RoleCompletionPayloadV5,
)
from core.pit_optimizer_v5.sandbox import (
    DockerCandidateRuntimeV5,
    DockerPanelEvaluatorV5,
    RuntimeDockerPanelEvaluatorV5,
)
from core.pit_optimizer_v5.production_sandbox import (
    LocalContainerExecutorV5,
    LocalSandboxMountFactoryV5,
)
from core.pit_optimizer_v5.production_workspace import LocalGitWorkspaceDriverV5
from core.pit_optimizer_v5.production_runtime import (
    CanonicalExperimentRecordFactoryV5,
    CompositeOwnedCleanupV5,
    ControllerCancellationV5,
    LocalArchiveReducerFactoryV5,
    LocalCandidateBaseOperationsV5,
    LocalRoleRequestFactoryV5,
    SelectionNoveltyResolverV5,
    SystemMonotonicClockV5,
)
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5
from core.pit_optimizer_v5.summary import OptimizerSummaryV5, summarize_repository_v5, unavailable_summary_v5
from core.pit_optimizer_v5.workspace import (
    GitCandidateMaterializerV5,
    WorkspaceOwnerV5,
    WorkspaceRootsV5,
)


V5CommandName = Literal["run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"]
_COMMANDS = frozenset({"run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"})
_PANEL_COMMANDS = frozenset({"init-stage-ledgers", "build-panels", "verify-panels"})
_MANIFEST_COMMANDS = frozenset({"build-manifest", "verify-manifest", "render-command"})
_CONFIRMATION_COMMANDS = frozenset({"build-confirmation-attempt", "confirm"})
_QUALIFICATION_COMMANDS = frozenset({"build-qualification-attempt", "qualify"})
_BASELINE_COMMANDS = frozenset(
    {
        "write-execution-profile",
        "build-sandbox-profile",
        "prepare-baseline-inputs",
        "capture-baseline",
        "verify-baseline",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FAILURE_REASONS = frozenset(
    {
        "artifact_graph_invalid",
        "artifact_root_unavailable",
        "campaign_authority_invalid",
        "import_campaign_authority_unavailable",
        "internal_failure",
        "invalid_request",
        "production_authority_mismatch",
        "production_config_invalid",
        "production_config_missing",
        "production_dependency_invalid",
        "production_provider_invalid",
        "resume_state_missing",
        "run_not_fresh",
        "runtime_failed",
        "runtime_result_invalid",
    }
)


def _canonical_windows_path_v5(value: object, label: str) -> str:
    if type(value) is not str or not value or "/" in value:
        raise ValueError(f"{label} must be an absolute canonical Windows path")
    pure = PureWindowsPath(value)
    if (
        not ntpath.isabs(value)
        or ntpath.normpath(value) != value
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in pure.parts[1:])
    ):
        raise ValueError(f"{label} must be an absolute canonical Windows path")
    return value


def _windows_paths_equal_v5(first: str | Path, second: str | Path) -> bool:
    return ntpath.normcase(ntpath.normpath(str(first))) == ntpath.normcase(ntpath.normpath(str(second)))


def _controller_lease_id_v5(
    *,
    manifest_sha256: str,
    round_index: int,
    owner_token_sha256: str,
) -> str:
    return "pit-v5-controller-" + canonical_sha256_v5(
        {
            "domain": "pit-optimizer-v5-controller-lease-v1",
            "manifest_sha256": manifest_sha256,
            "round_index": round_index,
            "owner_token_sha256": owner_token_sha256,
        }
    )


class V5CliFailure(RuntimeError):
    """Sanitized command failure with a closed readiness reason."""

    def __init__(
        self,
        reason: str,
        *,
        exit_code: int = 2,
        summary: OptimizerSummaryV5 | None = None,
    ) -> None:
        if reason not in _FAILURE_REASONS or type(exit_code) is not int or exit_code < 1:
            raise ValueError("V5 CLI failure is invalid")
        if summary is not None and type(summary) is not OptimizerSummaryV5:
            raise ValueError("V5 CLI failure summary is invalid")
        self.reason = reason
        self.exit_code = exit_code
        self.summary = summary
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class V5CliRequest:
    command: V5CommandName
    artifact_root: Path
    manifest_ref: ArtifactRefV5
    adapter_config_ref: ArtifactRefV5 | None
    round_index: int | None
    owner_token_sha256: str | None

    def __post_init__(self) -> None:
        if self.command not in _COMMANDS:
            raise ValueError("V5 CLI command is invalid")
        if not isinstance(self.artifact_root, Path) or not self.artifact_root.is_absolute():
            raise ValueError("V5 artifact root must be absolute")
        if type(self.manifest_ref) is not ArtifactRefV5:
            raise ValueError("V5 manifest reference is invalid")
        execution = self.command in {"run", "resume"}
        if execution:
            if type(self.round_index) is not int or self.round_index < 1:
                raise ValueError("V5 execution round is invalid")
            if type(self.owner_token_sha256) is not str or _DIGEST.fullmatch(self.owner_token_sha256) is None:
                raise ValueError("V5 execution owner token is invalid")
            if self.adapter_config_ref is not None and type(self.adapter_config_ref) is not ArtifactRefV5:
                raise ValueError("V5 execution adapter config is invalid")
        elif self.command in {"verify-run", "summarize"}:
            if self.round_index is not None or self.owner_token_sha256 is not None:
                raise ValueError("read-only V5 commands cannot carry execution authority")
            if self.adapter_config_ref is not None and type(self.adapter_config_ref) is not ArtifactRefV5:
                raise ValueError("read-only V5 adapter config is invalid")
        elif self.round_index is not None or self.owner_token_sha256 is not None or self.adapter_config_ref is not None:
            raise ValueError("V5 import command cannot carry production authority")


@dataclass(frozen=True, slots=True)
class ProductionAdapterConfigV5:
    schema_version: Literal[5]
    campaign_manifest_sha256: str
    repository_root_identity_sha256: str
    gateway_identity_sha256: str
    ledger_identity_sha256: str
    audit_store_identity_sha256: str
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
    api_key_environment_variable: Literal["OPENROUTER_API_KEY"] = "OPENROUTER_API_KEY"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("production adapter config schema is invalid")
        for value in (
            self.campaign_manifest_sha256,
            self.repository_root_identity_sha256,
            self.gateway_identity_sha256,
            self.ledger_identity_sha256,
            self.audit_store_identity_sha256,
            self.candidate_base_identity_sha256,
            self.workspace_driver_identity_sha256,
            self.mount_factory_identity_sha256,
            self.container_executor_identity_sha256,
        ):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError("production adapter config identity is invalid")
        for value, label in (
            (self.source_root, "production source root"),
            (self.workspace_root, "production workspace root"),
            (self.data_root, "production data root"),
            (self.output_root, "production output root"),
            (self.control_root, "production control root"),
            (self.git_executable, "production Git executable"),
            (self.docker_executable, "production Docker executable"),
        ):
            _canonical_windows_path_v5(value, label)
        if self.api_key_environment_variable != "OPENROUTER_API_KEY":
            raise ValueError("production provider secret handle is invalid")


@dataclass(frozen=True, slots=True)
class CampaignAuthoritiesV5:
    manifest: CampaignManifestV5
    panel_plan: CampaignPanelPlanV5
    evaluator_contract: EvaluatorContractV5
    baseline: BaselineParentAuthorityV5
    sandbox_profile: SandboxProfileV5

    def __post_init__(self) -> None:
        if (
            type(self.manifest) is not CampaignManifestV5
            or type(self.panel_plan) is not CampaignPanelPlanV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.baseline) is not BaselineParentAuthorityV5
            or type(self.sandbox_profile) is not SandboxProfileV5
        ):
            raise ValueError("V5 campaign authorities are invalid")
        validate_campaign_manifest_bindings_v5(
            self.manifest,
            panel_plan=self.panel_plan,
            evaluator_contract=self.evaluator_contract,
        )
        validate_sandbox_profile_resources_v5(self.sandbox_profile, self.manifest.resources)
        if (
            self.manifest.baseline_authority_ref.sha256 != canonical_sha256_v5(self.baseline)
            or self.manifest.sandbox_profile_ref.sha256 != self.sandbox_profile.sha256
            or self.manifest.pit_data_scope != self.baseline.pit_data_scope
            or self.manifest.semantic_mode != self.baseline.semantic_mode
        ):
            raise ValueError("V5 campaign authority identity differs from the manifest")


def _exact_production_host_adapter_graph_v5(
    candidate: object,
    adapter_config: ProductionAdapterConfigV5,
    *,
    repository: LocalArtifactRepositoryV5,
    authorities: CampaignAuthoritiesV5,
) -> bool:
    """Accept only the concrete, internally identified local host adapter graph."""

    if type(candidate) is not DockerCandidateRuntimeV5:
        return False
    base = candidate.base_operations
    mount_factory = candidate.mount_factory
    runtime_evaluator = candidate.panel_evaluator
    if type(runtime_evaluator) is not RuntimeDockerPanelEvaluatorV5:
        return False
    panel_evaluator = runtime_evaluator.evaluator
    if type(panel_evaluator) is not DockerPanelEvaluatorV5:
        return False
    executor = panel_evaluator.executor
    if (
        type(base) is not LocalCandidateBaseOperationsV5
        or type(base.materializer) is not GitCandidateMaterializerV5
        or type(base.materializer.driver) is not LocalGitWorkspaceDriverV5
        or type(mount_factory) is not LocalSandboxMountFactoryV5
        or type(executor) is not LocalContainerExecutorV5
    ):
        return False
    driver = base.materializer.driver
    return (
        candidate.manifest is authorities.manifest
        and candidate.panel_plan is authorities.panel_plan
        and candidate.evaluator_contract is authorities.evaluator_contract
        and candidate.sandbox_profile is authorities.sandbox_profile
        and base.manifest is authorities.manifest
        and base.panel_plan is authorities.panel_plan
        and base.evaluator_contract is authorities.evaluator_contract
        and base.sandbox_profile is authorities.sandbox_profile
        and base.baseline is authorities.baseline
        and base.repository is repository
        and base.mount_factory is mount_factory
        and base.probe_evaluator is runtime_evaluator
        and runtime_evaluator.registrar is None
        and panel_evaluator.executor is executor
        and panel_evaluator.clock is base.clock
        and mount_factory.workspace_driver is driver
        and mount_factory.manifest is authorities.manifest
        and mount_factory.evaluator_contract is authorities.evaluator_contract
        and mount_factory.sandbox_profile is authorities.sandbox_profile
        and mount_factory.owner is candidate.owner
        and mount_factory.repository is repository
        and _windows_paths_equal_v5(mount_factory.data_root, adapter_config.data_root)
        and _windows_paths_equal_v5(mount_factory.output_root, adapter_config.output_root)
        and executor.mount_factory is mount_factory
        and executor.manifest is authorities.manifest
        and executor.sandbox_profile is authorities.sandbox_profile
        and executor.owner is candidate.owner
        and executor.repository is repository
        and _windows_paths_equal_v5(executor.docker_executable, adapter_config.docker_executable)
        and _windows_paths_equal_v5(executor.control_root, adapter_config.control_root)
        and driver.export_mode == getattr(adapter_config, "export_mode", "full")
        and driver.repository is repository
        and driver.owner is candidate.owner
        and driver.source_commit == authorities.manifest.source_commit
        and driver.roots == WorkspaceRootsV5(adapter_config.source_root, adapter_config.workspace_root)
        and _windows_paths_equal_v5(driver.git_executable, adapter_config.git_executable)
        and base.base_identity_sha256 == adapter_config.candidate_base_identity_sha256
        and driver.driver_identity_sha256 == adapter_config.workspace_driver_identity_sha256
        and mount_factory.mount_identity_sha256 == adapter_config.mount_factory_identity_sha256
        and executor.executor_identity_sha256 == adapter_config.container_executor_identity_sha256
    )


def _require_paid_adapter_graph_v5(invoker, *, repository, authorities, adapter_config):
    if type(invoker) is not LedgerBackedRoleInvokerV5:
        raise V5CliFailure("production_provider_invalid")
    runner = invoker.runner
    if type(runner) is not AuthorizedRoleRunnerV5:
        raise V5CliFailure("production_provider_invalid")
    lifecycle = runner.authorization_lifecycle
    provider = runner.completion_provider
    if type(provider) is not GatewayCompletionProviderV5:
        raise V5CliFailure("production_dependency_invalid")
    gateway = provider.gateway
    if (
        type(gateway) is not OpenRouterOneShotJsonCompletionV5
        or type(lifecycle) is not LocalRoleAuthorizationLedgerV5
        or invoker.reconciler is not lifecycle
        or lifecycle.repository is not repository
        or lifecycle.manifest is not authorities.manifest
        or gateway.ledger is not lifecycle
        or gateway.api_key_environment_variable != adapter_config.api_key_environment_variable
        or runner.capabilities is not authorities.manifest.provider
        or lifecycle.campaign_manifest_sha256 != authorities.manifest.sha256
        or lifecycle.ledger_identity_sha256 != adapter_config.ledger_identity_sha256
        or lifecycle.audit_store_identity_sha256 != adapter_config.audit_store_identity_sha256
        or gateway.gateway_identity_sha256 != adapter_config.gateway_identity_sha256
        or gateway.ledger_identity_sha256 != lifecycle.ledger_identity_sha256
        or gateway.audit_store_identity_sha256 != lifecycle.audit_store_identity_sha256
    ):
        raise V5CliFailure("production_dependency_invalid")


@dataclass(frozen=True, slots=True)
class ProductionRoundCompositionV5:
    inputs: FeedbackRoundInputV5
    dependencies: FeedbackRoundDependenciesV5

    def validate_for(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        round_index: int,
        owner_token_sha256: str,
        adapter_config: ProductionAdapterConfigV5,
    ) -> None:
        if authorities.manifest.pit_data_scope != "production" or authorities.baseline.pit_data_scope != "production":
            raise V5CliFailure("production_authority_mismatch")
        if type(self.inputs) is not FeedbackRoundInputV5 or type(self.dependencies) is not FeedbackRoundDependenciesV5:
            raise V5CliFailure("production_dependency_invalid")
        if (
            self.inputs.manifest != authorities.manifest
            or self.inputs.panel_plan != authorities.panel_plan
            or self.inputs.evaluator_contract != authorities.evaluator_contract
            or self.inputs.baseline != authorities.baseline
            or self.inputs.round_index != round_index
            or self.inputs.owner_token_sha256 != owner_token_sha256
            or self.dependencies.persistence is not repository
        ):
            raise V5CliFailure("production_authority_mismatch")
        _require_paid_adapter_graph_v5(
            self.dependencies.invoker, repository=repository, authorities=authorities, adapter_config=adapter_config
        )
        candidate = self.dependencies.candidates
        base = candidate.base_operations if type(candidate) is DockerCandidateRuntimeV5 else None
        clock = self.dependencies.clock
        cleanup = self.dependencies.cleanup
        if (
            type(candidate) is not DockerCandidateRuntimeV5
            or candidate.manifest != authorities.manifest
            or candidate.panel_plan != authorities.panel_plan
            or candidate.evaluator_contract != authorities.evaluator_contract
            or candidate.sandbox_profile != authorities.sandbox_profile
            or candidate.owner.campaign_id != authorities.manifest.campaign_id
            or candidate.owner.round_index != round_index
            or candidate.owner.owner_token_sha256 != owner_token_sha256
            or not _exact_production_host_adapter_graph_v5(
                candidate,
                adapter_config,
                repository=repository,
                authorities=authorities,
            )
            or type(self.dependencies.requests) is not LocalRoleRequestFactoryV5
            or self.dependencies.requests.repository is not repository
            or self.dependencies.requests.manifest is not authorities.manifest
            or type(self.dependencies.novelty) is not SelectionNoveltyResolverV5
            or type(self.dependencies.records) is not CanonicalExperimentRecordFactoryV5
            or type(self.dependencies.archive_reducers) is not LocalArchiveReducerFactoryV5
            or self.dependencies.archive_reducers.repository is not repository
            or type(clock) is not SystemMonotonicClockV5
            or base is None
            or base.clock is not clock
            or type(self.dependencies.cancellation) is not ControllerCancellationV5
            or type(cleanup) is not CompositeOwnedCleanupV5
            or cleanup.owner is not candidate.owner
            or cleanup.materializer is not base.materializer
            or cleanup.executor is not candidate.panel_evaluator.evaluator.executor
            or cleanup.clock is not clock
        ):
            raise V5CliFailure("production_dependency_invalid")


class ProductionRoundFactoryV5:
    """Construct one exact local production graph from authenticated config data."""

    def __init__(
        self,
        *,
        adapter_config_ref: ArtifactRefV5,
        adapter_config: ProductionAdapterConfigV5,
    ) -> None:
        if (
            type(adapter_config_ref) is not ArtifactRefV5
            or type(adapter_config) is not ProductionAdapterConfigV5
            or adapter_config_ref.sha256 != canonical_sha256_v5(adapter_config)
        ):
            raise ValueError("production round factory authority is invalid")
        self.adapter_config_ref = adapter_config_ref
        self.adapter_config = adapter_config

    def compose_round(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        round_index: int,
        owner_token_sha256: str,
        adapter_config: ProductionAdapterConfigV5,
    ) -> ProductionRoundCompositionV5:
        if authorities.manifest.pit_data_scope != "production" or authorities.baseline.pit_data_scope != "production":
            raise V5CliFailure("production_authority_mismatch")
        if (
            adapter_config != self.adapter_config
            or authorities.manifest.sha256 != self.adapter_config.campaign_manifest_sha256
            or repository.root_identity_sha256 != self.adapter_config.repository_root_identity_sha256
        ):
            raise V5CliFailure("production_authority_mismatch")
        composition = _compose_production_round_from_paths_v5(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            source_root=adapter_config.source_root,
            workspace_root=adapter_config.workspace_root,
            data_root=adapter_config.data_root,
            output_root=adapter_config.output_root,
            control_root=adapter_config.control_root,
            git_executable=adapter_config.git_executable,
            docker_executable=adapter_config.docker_executable,
            api_key_environment_variable=adapter_config.api_key_environment_variable,
        )
        composition.validate_for(
            repository=repository,
            authorities=authorities,
            round_index=round_index,
            owner_token_sha256=owner_token_sha256,
            adapter_config=adapter_config,
        )
        return composition


def _compose_paid_invoker_v5(*, repository, manifest, api_key_environment_variable="OPENROUTER_API_KEY"):
    provider_capabilities = manifest.provider
    if (
        provider_capabilities is None
        or provider_capabilities.automatic_retries != 0
        or provider_capabilities.schema_repair_calls != 0
    ):
        raise V5CliFailure("production_provider_invalid")
    try:
        ledger = LocalRoleAuthorizationLedgerV5(
            repository=repository,
            manifest=manifest,
        )
        gateway = OpenRouterOneShotJsonCompletionV5(
            ledger=ledger,
            api_key_environment_variable=api_key_environment_variable,
        )
        role_runner = AuthorizedRoleRunnerV5(
            capabilities=provider_capabilities,
            provider=GatewayCompletionProviderV5(gateway),
            lifecycle=ledger,
        )
        invoker = LedgerBackedRoleInvokerV5(
            runner=role_runner,
            reconciler=ledger,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise V5CliFailure("production_provider_invalid") from exc
    return invoker


def _compose_production_round_from_paths_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authorities: CampaignAuthoritiesV5,
    round_index: int,
    owner_token_sha256: str,
    source_root: str,
    workspace_root: str,
    data_root: str,
    output_root: str,
    control_root: str,
    git_executable: str,
    docker_executable: str,
    api_key_environment_variable: str = "OPENROUTER_API_KEY",
) -> ProductionRoundCompositionV5:
    """Construct real local adapters without invoking a provider, Git, or Docker."""
    provider_capabilities = authorities.manifest.provider
    if (
        authorities.manifest.pit_data_scope != "production"
        or authorities.baseline.pit_data_scope != "production"
        or provider_capabilities is None
        or provider_capabilities.automatic_retries != 0
        or provider_capabilities.schema_repair_calls != 0
    ):
        raise V5CliFailure("production_provider_invalid")
    invoker = _compose_paid_invoker_v5(
        repository=repository, manifest=authorities.manifest,
        api_key_environment_variable=api_key_environment_variable,
    )
    return _compose_round_from_paths_v5(
        repository=repository,
        authorities=authorities,
        round_index=round_index,
        owner_token_sha256=owner_token_sha256,
        source_root=source_root,
        workspace_root=workspace_root,
        data_root=data_root,
        output_root=output_root,
        control_root=control_root,
        git_executable=git_executable,
        docker_executable=docker_executable,
        invoker=invoker,
        export_mode="full",
    )


def _compose_round_from_paths_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    authorities: CampaignAuthoritiesV5,
    round_index: int,
    owner_token_sha256: str,
    source_root: str,
    workspace_root: str,
    data_root: str,
    output_root: str,
    control_root: str,
    git_executable: str,
    docker_executable: str,
    invoker: object,
    export_mode: str,
) -> ProductionRoundCompositionV5:
    try:
        owner = WorkspaceOwnerV5(
            authorities.manifest.campaign_id,
            round_index,
            owner_token_sha256,
            _controller_lease_id_v5(
                manifest_sha256=authorities.manifest.sha256,
                round_index=round_index,
                owner_token_sha256=owner_token_sha256,
            ),
        )
        clock = SystemMonotonicClockV5()
        roots = WorkspaceRootsV5(
            source_root,
            workspace_root,
        )
        driver = LocalGitWorkspaceDriverV5(
            roots=roots,
            source_commit=authorities.manifest.source_commit,
            git_executable=Path(git_executable),
            repository=repository,
            owner=owner,
            export_mode=export_mode,
        )
        materializer = GitCandidateMaterializerV5(
            roots=roots,
            driver=driver,
            token_factory=lambda: secrets.token_hex(32),
        )
        mount_factory = LocalSandboxMountFactoryV5(
            manifest=authorities.manifest,
            evaluator_contract=authorities.evaluator_contract,
            sandbox_profile=authorities.sandbox_profile,
            owner=owner,
            workspace_driver=driver,
            data_root=Path(data_root),
            output_root=Path(output_root),
            repository=repository,
        )
        executor = LocalContainerExecutorV5(
            manifest=authorities.manifest,
            sandbox_profile=authorities.sandbox_profile,
            owner=owner,
            mount_factory=mount_factory,
            docker_executable=Path(docker_executable),
            control_root=Path(control_root),
            repository=repository,
        )
        runtime_evaluator = RuntimeDockerPanelEvaluatorV5(DockerPanelEvaluatorV5(executor=executor, clock=clock))
        base = LocalCandidateBaseOperationsV5(
            manifest=authorities.manifest,
            panel_plan=authorities.panel_plan,
            evaluator_contract=authorities.evaluator_contract,
            sandbox_profile=authorities.sandbox_profile,
            baseline=authorities.baseline,
            owner=owner,
            repository=repository,
            materializer=materializer,
            mount_factory=mount_factory,
            probe_evaluator=runtime_evaluator,
            clock=clock,
        )
        candidate = DockerCandidateRuntimeV5(
            manifest=authorities.manifest,
            panel_plan=authorities.panel_plan,
            evaluator_contract=authorities.evaluator_contract,
            sandbox_profile=authorities.sandbox_profile,
            owner=owner,
            base=base,
            mounts=mount_factory,
            evaluator=runtime_evaluator,
        )
        dependencies = FeedbackRoundDependenciesV5(
            persistence=repository,
            invoker=invoker,
            requests=LocalRoleRequestFactoryV5(
                repository=repository,
                manifest=authorities.manifest,
            ),
            novelty=SelectionNoveltyResolverV5(),
            candidates=candidate,
            records=CanonicalExperimentRecordFactoryV5(),
            archive_reducers=LocalArchiveReducerFactoryV5(repository),
            clock=clock,
            cancellation=ControllerCancellationV5(),
            cleanup=CompositeOwnedCleanupV5(
                owner,
                materializer,
                executor,
                clock,
            ),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise V5CliFailure("production_dependency_invalid") from exc
    return ProductionRoundCompositionV5(
        FeedbackRoundInputV5(
            authorities.manifest,
            authorities.panel_plan,
            authorities.evaluator_contract,
            authorities.baseline,
            round_index,
            owner_token_sha256,
        ),
        dependencies,
    )


@runtime_checkable
class V5CommandServices(Protocol):
    def execute(self, request: V5CliRequest) -> OptimizerSummaryV5: ...


class ProductionV5CommandServices:
    """Authenticate local authorities and invoke only approved production shapes."""

    @staticmethod
    def _repository(request: V5CliRequest) -> LocalArtifactRepositoryV5:
        try:
            return LocalArtifactRepositoryV5(request.artifact_root)
        except (OSError, ValueError) as exc:
            raise V5CliFailure("artifact_root_unavailable") from exc

    @staticmethod
    def _authorities(
        repository: LocalArtifactRepositoryV5,
        manifest_ref: ArtifactRefV5,
    ) -> CampaignAuthoritiesV5:
        verification = repository.verify_graph(manifest_ref)
        if not verification.verified:
            raise V5CliFailure("artifact_graph_invalid")
        try:
            manifest = repository.load_typed_artifact(
                manifest_ref,
                value_type=CampaignManifestV5,
            )
            if manifest.pit_data_scope != "production":
                raise V5CliFailure("campaign_authority_invalid")
            return CampaignAuthoritiesV5(
                manifest=manifest,
                panel_plan=repository.load_typed_artifact(
                    manifest.panel_plan_ref,
                    value_type=CampaignPanelPlanV5,
                ),
                evaluator_contract=repository.load_typed_artifact(
                    manifest.evaluator_contract_ref,
                    value_type=EvaluatorContractV5,
                ),
                baseline=repository.load_typed_artifact(
                    manifest.baseline_authority_ref,
                    value_type=BaselineParentAuthorityV5,
                ),
                sandbox_profile=repository.load_typed_artifact(
                    manifest.sandbox_profile_ref,
                    value_type=SandboxProfileV5,
                ),
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise V5CliFailure("campaign_authority_invalid") from exc

    @staticmethod
    def _verify_local_run(
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        adapter_config: ProductionAdapterConfigV5 | None = None,
    ) -> None:
        packages: list[RoleInvocationPackageV5] = []
        resource_cleanup_states: list[bool] = []
        last_cleanup: CleanupResultPayloadV5 | None = None
        for round_index in range(1, authorities.manifest.search.max_feedback_rounds + 1):
            events = repository.load_round_events(
                campaign_id=authorities.manifest.campaign_id,
                round_index=round_index,
            )
            leases: list[ResourceLeasePayloadV5] = []
            cleanups: list[CleanupResultPayloadV5] = []
            successful_keys: list[CandidateExecutionKeyV5] = []
            for event in events:
                payload = repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
                if type(payload) is RoleCompletionPayloadV5:
                    packages.append(repository.load_role_invocation(payload))
                elif type(payload) is ResourceLeasePayloadV5:
                    leases.append(payload)
                elif type(payload) is CleanupResultPayloadV5:
                    cleanups.append(payload)
                elif type(payload) is CandidateStageResultPayloadV5 and payload.semantic_fingerprint is not None:
                    successful_keys.append(CandidateExecutionKeyV5(payload.experiment_id, "semantic_probe", None))
                elif type(payload) is QuickEvidencePayloadV5:
                    successful_keys.append(CandidateExecutionKeyV5(payload.experiment_id, "quick_evaluation", None))
                elif type(payload) is EpisodeEvidencePayloadV5:
                    successful_keys.append(
                        CandidateExecutionKeyV5(
                            payload.experiment_id,
                            "discovery_evaluation",
                            payload.episode.episode_ordinal,
                        )
                    )
            executions = repository.verify_candidate_execution_index(
                campaign_id=authorities.manifest.campaign_id,
                round_index=round_index,
            )
            resource_cleanup_states.append(
                ProductionV5CommandServices._verify_resource_history(
                    repository,
                    authorities,
                    adapter_config,
                    round_index,
                    tuple(leases),
                    executions,
                    tuple(cleanups),
                    tuple(successful_keys),
                )
            )
            if cleanups:
                last_cleanup = cleanups[-1]
        # A prior development round's cleanup does not retire a pending later round.
        if (
            authorities.manifest.pit_data_scope != "development_sp500_v2"
            and last_cleanup is not None
            and last_cleanup.cleanup_complete
            and not all(resource_cleanup_states)
        ):
            raise V5CliFailure("artifact_graph_invalid")
        checkpoint = repository.load_checkpoint()
        if checkpoint is not None:
            tuple(repository.load_experiment(reference) for reference in checkpoint.record_refs)
        if authorities.manifest.provider is None:
            if authorities.manifest.pit_data_scope == "development_sp500_v2":
                from core.pit_optimizer_v5.development_preparation import authenticate_controller_packages_v5

                authenticate_controller_packages_v5(repository, authorities.manifest, tuple(packages))
            elif packages:
                raise V5CliFailure("production_provider_invalid")
        else:
            try:
                ledger = LocalRoleAuthorizationLedgerV5(
                    repository=repository,
                    manifest=authorities.manifest,
                )
                ledger.authenticate_role_invocations(tuple(packages))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise V5CliFailure("production_provider_invalid") from exc
        try:
            LocalArchiveReducerFactoryV5(repository).verify_projection(
                manifest=authorities.manifest,
                panel_plan=authorities.panel_plan,
                evaluator_contract=authorities.evaluator_contract,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise V5CliFailure("artifact_graph_invalid") from exc

    @staticmethod
    def _verify_resource_history(
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        config: ProductionAdapterConfigV5 | None,
        round_index: int,
        leases: tuple[ResourceLeasePayloadV5, ...],
        executions: tuple[CandidateExecutionAuthorityV5, ...],
        cleanups: tuple[CleanupResultPayloadV5, ...],
        successful_keys: tuple[CandidateExecutionKeyV5, ...] = (),
    ) -> bool:
        """Reconstruct only local read authorities; never launch or repair."""

        if any(key not in {item.key for item in executions} for key in successful_keys):
            raise V5CliFailure("artifact_graph_invalid")
        expected_counts = (
            sum(item.resource_kind == "workspace" for item in leases),
            0,
            len(executions),
            len(executions),
        )
        if any(
            result.cleanup_complete
            and (
                result.owned_workspaces,
                result.owned_policy_workers,
                result.owned_evaluators,
                result.owned_containers,
            )
            != expected_counts
            for result in cleanups
        ):
            raise V5CliFailure("artifact_graph_invalid")
        if not leases and not executions:
            return True
        if config is None:
            raise V5CliFailure("production_config_missing")
        tokens = {item.owner_token_sha256 for item in leases} | {item.owner_token_sha256 for item in executions}
        if (
            len(tokens) != 1
            or any(item.owner_campaign_id != authorities.manifest.campaign_id for item in leases)
            or any(item.resource_kind != "workspace" for item in leases)
        ):
            raise V5CliFailure("artifact_graph_invalid")
        token = next(iter(tokens))
        try:
            owner = WorkspaceOwnerV5(
                authorities.manifest.campaign_id,
                round_index,
                token,
                _controller_lease_id_v5(
                    manifest_sha256=authorities.manifest.sha256,
                    round_index=round_index,
                    owner_token_sha256=token,
                ),
            )
            driver = LocalGitWorkspaceDriverV5(
                roots=WorkspaceRootsV5(config.source_root, config.workspace_root),
                source_commit=authorities.manifest.source_commit,
                git_executable=Path(config.git_executable),
                repository=repository,
                owner=owner,
                export_mode=getattr(config, "export_mode", "full"),
            )
            workspace_states = tuple(driver.authenticate_lease_history(item) for item in leases)
            retired = tuple(state.lifecycle == "retired" for state in workspace_states)
            cleaned: tuple[bool, ...] = ()
            if executions:
                mounts = LocalSandboxMountFactoryV5(
                    manifest=authorities.manifest,
                    evaluator_contract=authorities.evaluator_contract,
                    sandbox_profile=authorities.sandbox_profile,
                    owner=owner,
                    workspace_driver=driver,
                    data_root=Path(config.data_root),
                    output_root=Path(config.output_root),
                    repository=repository,
                )
                executor = LocalContainerExecutorV5(
                    manifest=authorities.manifest,
                    sandbox_profile=authorities.sandbox_profile,
                    owner=owner,
                    mount_factory=mounts,
                    docker_executable=Path(config.docker_executable),
                    control_root=Path(config.control_root),
                    repository=repository,
                )
                cleaned = tuple(
                    executor.authenticate_execution_history(
                        item,
                        workspace_source_identity_sha256s=tuple(
                            state.workspace_identity_sha256 for state in workspace_states
                        ),
                        require_successful_output=item.key in successful_keys,
                    )
                    for item in executions
                )
            if any(result.cleanup_complete for result in cleanups) and not all((*retired, *cleaned)):
                raise ValueError("journal cleanup lacks durable resource retirement")
            return all((*retired, *cleaned))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise V5CliFailure("artifact_graph_invalid") from exc

    @staticmethod
    def _adapter_config(
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        reference: ArtifactRefV5,
    ) -> ProductionAdapterConfigV5:
        try:
            config = repository.load_typed_artifact(
                reference,
                value_type=ProductionAdapterConfigV5,
            )
        except (ArtifactRepositoryFailureV5, TypeError, ValueError) as exc:
            raise V5CliFailure("production_config_invalid") from exc
        if (
            config.campaign_manifest_sha256 != authorities.manifest.sha256
            or config.repository_root_identity_sha256 != repository.root_identity_sha256
        ):
            raise V5CliFailure("production_config_invalid")
        return config

    def execute(self, request: V5CliRequest) -> OptimizerSummaryV5:
        try:
            return self._execute(request)
        except PaidRoleRecoveryRequiredV5:
            raise
        except V5CliFailure:
            raise
        except BaseException:
            raise V5CliFailure("internal_failure", exit_code=1) from None

    def _execute(self, request: V5CliRequest) -> OptimizerSummaryV5:
        if type(request) is not V5CliRequest:
            raise V5CliFailure("invalid_request")
        if request.command == "import-v4-candidate":
            raise V5CliFailure("import_campaign_authority_unavailable")
        if request.command in {"run", "resume"} and request.adapter_config_ref is None:
            raise V5CliFailure("production_config_missing")
        repository = self._repository(request)
        if request.command == "run-fixture":
            from core.pit_optimizer_v5.fixture_runtime import run_fixture_campaign_v5

            try:
                authenticated = authenticate_campaign_manifest_v5(
                    repository=repository, manifest_ref=request.manifest_ref
                )
                if authenticated.manifest.provider is not None:
                    raise ValueError("fixture manifest cannot authorize a provider")
                if repository.load_checkpoint() is not None or any(
                    repository.load_round_events(campaign_id=authenticated.manifest.campaign_id, round_index=index)
                    for index in range(1, authenticated.manifest.search.max_feedback_rounds + 1)
                ):
                    raise V5CliFailure("run_not_fresh")
                results = run_fixture_campaign_v5(repository=repository, authorities=authenticated)
            except V5CliFailure:
                raise
            except (TypeError, ValueError, ArtifactRepositoryFailureV5):
                raise V5CliFailure("campaign_authority_invalid") from None
            completed = len(results) == 2 and all(
                result.status == "completed" and result.cleanup is not None and result.cleanup.cleanup_complete
                for result in results
            )
            summary = summarize_repository_v5(
                repository=repository,
                manifest=authenticated.manifest,
                command=request.command,
                readiness_code="completed" if completed else "runtime_failed",
            )
            if not completed:
                raise V5CliFailure("runtime_failed", exit_code=1, summary=summary)
            return summary
        if request.command in {"verify-run", "summarize"}:
            authenticated = authenticate_campaign_manifest_v5(repository=repository, manifest_ref=request.manifest_ref)
            if authenticated.manifest.pit_data_scope == "development_sp500_v2":
                from core.pit_optimizer_v5.development_preparation import require_development_v5

                require_development_v5(authenticated.manifest)
                authorities = CampaignAuthoritiesV5(
                    authenticated.manifest,
                    authenticated.panel_plan,
                    authenticated.evaluator_contract,
                    authenticated.baseline_authority,
                    authenticated.sandbox_profile,
                )
            else:
                authorities = self._authorities(repository, request.manifest_ref)
        else:
            # Standalone run/resume retain the production-only authority boundary.
            authorities = self._authorities(repository, request.manifest_ref)
        if request.command in {"verify-run", "summarize"}:
            if (
                authorities.manifest.pit_data_scope == "development_sp500_v2"
                and authorities.manifest.semantic_mode == "disabled_development"
            ):
                from core.pit_optimizer_v5.development_preparation import load_development_config_v5

                if request.adapter_config_ref is None:
                    raise V5CliFailure("production_config_missing")
                config = load_development_config_v5(repository, authorities, request.adapter_config_ref)
                self._verify_local_run(repository, authorities, config)
                return summarize_repository_v5(
                    repository=repository,
                    manifest=authorities.manifest,
                    command=request.command,
                    readiness_code="verified" if request.command == "verify-run" else "ready",
                )
            if authorities.manifest.provider is None:
                from core.pit_optimizer_v5.fixture_runtime import verify_fixture_run_v5

                if request.adapter_config_ref is not None:
                    raise V5CliFailure("production_config_invalid")
                authenticate_campaign_manifest_v5(repository=repository, manifest_ref=request.manifest_ref)
                verify_fixture_run_v5(repository=repository, manifest=authorities.manifest)
                return summarize_repository_v5(
                    repository=repository,
                    manifest=authorities.manifest,
                    command=request.command,
                    readiness_code="verified" if request.command == "verify-run" else "ready",
                )
            config = (
                None
                if request.adapter_config_ref is None
                else self._adapter_config(repository, authorities, request.adapter_config_ref)
            )
            self._verify_local_run(repository, authorities, config)
            return summarize_repository_v5(
                repository=repository,
                manifest=authorities.manifest,
                command=request.command,
                readiness_code="verified" if request.command == "verify-run" else "ready",
            )
        if request.adapter_config_ref is None:
            raise V5CliFailure("production_config_missing")
        adapter_config = self._adapter_config(
            repository,
            authorities,
            request.adapter_config_ref,
        )
        try:
            factory = ProductionRoundFactoryV5(
                adapter_config_ref=request.adapter_config_ref,
                adapter_config=adapter_config,
            )
        except (TypeError, ValueError) as exc:
            raise V5CliFailure("production_config_invalid") from exc
        assert request.round_index is not None and request.owner_token_sha256 is not None
        existing_events = repository.load_round_events(
            campaign_id=authorities.manifest.campaign_id,
            round_index=request.round_index,
        )
        if request.command == "run" and existing_events:
            raise V5CliFailure("run_not_fresh")
        if request.command == "resume" and not existing_events:
            raise V5CliFailure("resume_state_missing")
        composition = factory.compose_round(
            repository=repository,
            authorities=authorities,
            round_index=request.round_index,
            owner_token_sha256=request.owner_token_sha256,
            adapter_config=adapter_config,
        )
        if type(composition) is not ProductionRoundCompositionV5:
            raise V5CliFailure("production_dependency_invalid")
        composition.validate_for(
            repository=repository,
            authorities=authorities,
            round_index=request.round_index,
            owner_token_sha256=request.owner_token_sha256,
            adapter_config=adapter_config,
        )
        result = run_feedback_round_v5(composition.inputs, composition.dependencies)
        if type(result) is not FeedbackRoundResultV5:
            raise V5CliFailure("runtime_result_invalid")
        self._verify_local_run(repository, authorities, adapter_config)
        summary = summarize_repository_v5(
            repository=repository,
            manifest=authorities.manifest,
            command=request.command,
            readiness_code="completed" if result.status != "failed" else "runtime_failed",
        )
        if result.status == "failed":
            raise V5CliFailure(
                "runtime_failed",
                exit_code=1,
                summary=replace(summary, status="failed", readiness_code="runtime_failed"),
            )
        return summary


class _ClosedArgumentParserV5(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise V5CliFailure("invalid_request")


def build_parser_v5() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParserV5(prog="pit-optimizer-v5", allow_abbrev=False)
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_ClosedArgumentParserV5,
    )
    for name in ("run", "run-fixture", "resume", "verify-run", "summarize", "import-v4-candidate"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--manifest-path", required=True)
        command.add_argument("--manifest-sha256", required=True)
        if name in {"run", "resume"}:
            command.add_argument("--round-index", required=True, type=int)
            command.add_argument("--owner-token-sha256", required=True)
        if name in {"run", "resume", "verify-run", "summarize"}:
            command.add_argument("--adapter-config-path")
            command.add_argument("--adapter-config-sha256")
    for name in ("prepare-production", "prepare-development", "run-campaign", "resume-campaign"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--manifest-path", required=True)
        command.add_argument("--manifest-sha256", required=True)
        command.add_argument("--owner-token-sha256", required=not name.startswith("prepare-"))
        if name.startswith("prepare-"):
            for root in ("source", "workspace", "data", "output", "control"):
                command.add_argument(f"--{root}-root", required=True)
            command.add_argument("--git-executable", help="Git executable; defaults to discovery on PATH")
            command.add_argument("--docker-executable", help="Docker executable; defaults to discovery on PATH")
            if name == "prepare-development":
                command.add_argument("--response-directory")
            command.add_argument("--round-index", type=int, default=1)
            command.add_argument(
                "--output-path", required=True, help="Adapter config path relative to the artifact root"
            )
        else:
            command.add_argument("--adapter-config-path", required=True)
            command.add_argument("--adapter-config-sha256", required=True)
            command.add_argument("--campaign-policy-path")
            command.add_argument("--campaign-policy-sha256")
    for name in ("import-development", "controller-request", "controller-response"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        if name == "import-development":
            command.add_argument("--input-file", required=True)
            command.add_argument("--validate-only", action="store_true")
        else:
            for option in ("manifest-path", "manifest-sha256", "request-path", "request-sha256"):
                command.add_argument("--" + option, required=True)
            if name == "controller-response":
                command.add_argument("--response-file", required=True)
    initialize = commands.add_parser("init-stage-ledgers", allow_abbrev=False)
    _add_panel_data_arguments_v5(initialize)
    initialize.add_argument("--confirmation-ledger-path", required=True)
    initialize.add_argument("--qualification-ledger-path", required=True)
    build = commands.add_parser("build-panels", allow_abbrev=False)
    _add_panel_data_arguments_v5(build)
    build.add_argument("--start-date", required=True)
    build.add_argument("--end-date", required=True)
    build.add_argument("--partition-seed", required=True)
    build.add_argument("--target-pct", required=True)
    build.add_argument("--confirmation-ledger-path", required=True)
    build.add_argument("--qualification-ledger-path", required=True)
    build.add_argument("--discovery-output-path", required=True)
    build.add_argument("--confirmation-output-path", required=True)
    build.add_argument("--qualification-output-path", required=True)
    verify = commands.add_parser("verify-panels", allow_abbrev=False)
    verify.add_argument("--artifact-root", required=True)
    for prefix in ("discovery", "confirmation", "qualification"):
        verify.add_argument(f"--{prefix}-plan-path", required=True)
        verify.add_argument(f"--{prefix}-plan-sha256", required=True)
    verify.add_argument("--keep-held-out-sealed", action="store_true")
    build_manifest = commands.add_parser("build-manifest", allow_abbrev=False)
    build_manifest.add_argument("--artifact-root", required=True)
    build_manifest.add_argument("--campaign-id", required=True)
    build_manifest.add_argument("--target-pct", required=True)
    build_manifest.add_argument("--source-root", required=True)
    build_manifest.add_argument("--git-executable", required=True)
    build_manifest.add_argument("--source-commit", required=True)
    for prefix in (
        "execution-profile",
        "evaluator-contract",
        "baseline-authority",
        "panel-plan",
        "sandbox-profile",
    ):
        build_manifest.add_argument(f"--{prefix}-path", required=True)
        build_manifest.add_argument(f"--{prefix}-sha256", required=True)
    build_manifest.add_argument("--policy-scope-output-path", required=True)
    build_manifest.add_argument("--output-path", required=True)
    build_manifest.add_argument("--hypotheses-per-investigator", type=int, default=3)
    build_manifest.add_argument("--max-tunable-axes", type=int, default=4)
    build_manifest.add_argument("--max-variants-per-template", type=int, default=12)
    build_manifest.add_argument("--max-discovery-survivors-per-template", type=int, default=6)
    build_manifest.add_argument("--archive-capacity", type=int, default=8)
    build_manifest.add_argument("--max-feedback-rounds", type=int, default=10)
    build_manifest.add_argument(
        "--pit-data-scope",
        choices=("production", "development_sp500_v2"),
        default="production",
    )
    build_manifest.add_argument(
        "--semantic-mode",
        choices=("required", "disabled_development"),
        default="required",
    )
    build_manifest.add_argument("--deny-full-source-escape", action="store_true")
    build_manifest.add_argument("--investigator-memory-max-bytes", type=int, default=96 * 1024)
    build_manifest.add_argument("--max-parallel-evaluations", type=int, default=2)
    build_manifest.add_argument("--evaluation-cpu-limit", type=Decimal, default=Decimal("1"))
    build_manifest.add_argument("--evaluation-memory-mib", type=int, default=1024)
    build_manifest.add_argument("--evaluation-pid-limit", type=int, default=32)
    build_manifest.add_argument("--evaluation-output-limit-bytes", type=int, default=64 * 1024 * 1024)
    build_manifest.add_argument("--policy-method-timeout-seconds", type=int, default=1)
    build_manifest.add_argument("--worker-startup-timeout-seconds", type=int, default=30)
    build_manifest.add_argument("--role-call-timeout-seconds", type=int, default=180)
    build_manifest.add_argument("--mechanics-timeout-seconds", type=int, default=60)
    build_manifest.add_argument("--quick-timeout-seconds", type=int, default=180)
    build_manifest.add_argument("--discovery-episode-timeout-seconds", type=int, default=600)
    build_manifest.add_argument("--round-wall-timeout-seconds", type=int, default=1800)
    build_manifest.add_argument("--campaign-wall-timeout-seconds", type=int, default=18000)
    build_manifest.add_argument("--cleanup-timeout-seconds", type=int, default=60)
    build_manifest.add_argument("--provider-model")
    build_manifest.add_argument("--maximum-role-calls", type=int)
    build_manifest.add_argument("--maximum-total-tokens", type=int)
    build_manifest.add_argument("--maximum-output-tokens-per-role", type=int)
    build_manifest.add_argument("--maximum-usd", type=Decimal)
    build_manifest.add_argument("--automatic-retries", type=int, default=0)
    build_manifest.add_argument("--schema-repair-calls", type=int, default=0)
    build_manifest.add_argument("--input-token-overhead-upper-bound", type=int, default=4096)
    build_manifest.add_argument("--input-usd-per-million-tokens", type=Decimal)
    build_manifest.add_argument("--output-usd-per-million-tokens", type=Decimal)
    for name in ("verify-manifest", "render-command"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--manifest-path", required=True)
        command.add_argument("--manifest-sha256", required=True)
        if name == "render-command":
            command.add_argument("--adapter-config-path")
            command.add_argument("--adapter-config-sha256")
            command.add_argument("--round-index", type=int, default=1)
            command.add_argument("--owner-token-sha256")
            command.add_argument(
                "--execution-command", choices=("run", "resume", "run-campaign", "resume-campaign"), default="run"
            )
    confirmation = commands.add_parser("build-confirmation-attempt", allow_abbrev=False)
    confirmation.add_argument("--artifact-root", required=True)
    confirmation.add_argument("--attempt-id", required=True)
    confirmation.add_argument("--output-path", required=True)
    confirmation.add_argument("--confirmation-ledger-path", required=True)
    for prefix in (
        "discovery-manifest",
        "discovery-checkpoint",
        "discovery-archive",
        "finalized-campaign",
        "confirmation-plan",
        "scenario-grid",
        "preopen-snapshot",
        "execution-adapter",
    ):
        confirmation.add_argument(f"--{prefix}-path", required=True)
        confirmation.add_argument(f"--{prefix}-sha256", required=True)
    confirm = commands.add_parser("confirm", allow_abbrev=False)
    confirm.add_argument("--artifact-root", required=True)
    confirm.add_argument("--attempt-path", required=True)
    confirm.add_argument("--attempt-sha256", required=True)
    qualification = commands.add_parser("build-qualification-attempt", allow_abbrev=False)
    qualification.add_argument("--artifact-root", required=True)
    qualification.add_argument("--attempt-id", required=True)
    qualification.add_argument("--output-path", required=True)
    qualification.add_argument("--qualification-ledger-path", required=True)
    qualification.add_argument("--operator-approved", action="store_true", required=True)
    for prefix in ("confirmation-outcome", "qualification-plan", "preopen-snapshot"):
        qualification.add_argument(f"--{prefix}-path", required=True)
        qualification.add_argument(f"--{prefix}-sha256", required=True)
    qualify = commands.add_parser("qualify", allow_abbrev=False)
    qualify.add_argument("--artifact-root", required=True)
    qualify.add_argument("--attempt-path", required=True)
    qualify.add_argument("--attempt-sha256", required=True)
    readiness = commands.add_parser("full-replay-readiness", allow_abbrev=False)
    readiness.add_argument("--artifact-root", required=True)
    readiness.add_argument("--qualification-outcome-path", required=True)
    readiness.add_argument("--qualification-outcome-sha256", required=True)
    readiness.add_argument("--output-path", required=True)
    readiness.add_argument("--trusted-git-executable", required=True)
    readiness.add_argument("--trusted-git-sha256", required=True)
    for name in sorted(_BASELINE_COMMANDS):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        if name == "verify-baseline":
            command.add_argument("--authority", required=True, help="Canonical V5 artifact-relative authority path")
            command.add_argument("--authority-sha256", required=True)
        else:
            command.add_argument("--output-path", required=True)
        if name == "capture-baseline":
            command.add_argument("--capture-inputs-path", required=True)
            command.add_argument("--capture-inputs-sha256", required=True)
            for option in (
                "source-root",
                "scratch-root",
                "trusted-git-executable",
                "trusted-git-sha256",
                "docker-executable",
                "docker-sha256",
            ):
                command.add_argument(f"--{option}", required=True)
        if name == "prepare-baseline-inputs":
            for prefix in (
                "evaluator-contract",
                "execution-profile",
                "sandbox-profile",
                "panel-plan",
                "baseline-policy-revision",
                "source-bundle",
                "policy-scope",
                "evaluator-source",
                "identity-transition",
                "resources",
            ):
                required = prefix not in {
                    "evaluator-contract",
                    "baseline-policy-revision",
                    "source-bundle",
                    "policy-scope",
                }
                command.add_argument(f"--{prefix}-path", required=required)
                command.add_argument(f"--{prefix}-sha256", required=required)
            command.add_argument("--compose-source", action="store_true")
            command.add_argument("--source-commit")
            command.add_argument("--immutable-constraints-path")
            command.add_argument("--immutable-constraints-sha256")
            command.add_argument("--source-bundle-output-path")
            command.add_argument("--policy-revision-output-path")
            command.add_argument("--policy-scope-output-path")
            command.add_argument("--evaluator-contract-output-path")
            command.add_argument("--source-root", required=True)
            command.add_argument("--scratch-root", required=True)
            command.add_argument("--git-executable", required=True)
            command.add_argument("--git-sha256")
            command.add_argument("--docker-executable", required=True)
            command.add_argument("--docker-sha256")
        if name == "build-sandbox-profile":
            command.add_argument("--image-name", required=True)
            command.add_argument(
                "--image-digest", required=True, help="Explicit immutable sha256 image digest; no Docker action"
            )
            for prefix in ("resources", "evaluator-source"):
                command.add_argument(f"--{prefix}-path", required=True)
                command.add_argument(f"--{prefix}-sha256", required=True)
    return parser


def _add_panel_data_arguments_v5(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--pit-bundle-path", required=True)
    parser.add_argument("--pit-bundle-sha256", required=True)
    parser.add_argument("--prices-provenance-path", required=True)
    parser.add_argument("--prices-provenance-sha256", required=True)


def parse_v5_args(argv: Sequence[str]) -> V5CliRequest:
    namespace = build_parser_v5().parse_args(tuple(argv))
    command = namespace.command
    if command not in _COMMANDS:
        raise ValueError("V5 command is invalid")
    if command in {"run", "resume", "verify-run", "summarize"} and (
        (namespace.adapter_config_path is None) != (namespace.adapter_config_sha256 is None)
    ):
        raise V5CliFailure("invalid_request")
    return V5CliRequest(
        command=command,
        artifact_root=Path(namespace.artifact_root),
        manifest_ref=ArtifactRefV5(namespace.manifest_path, namespace.manifest_sha256),
        adapter_config_ref=(
            ArtifactRefV5(namespace.adapter_config_path, namespace.adapter_config_sha256)
            if command in {"run", "resume", "verify-run", "summarize"}
            and namespace.adapter_config_path is not None
            and namespace.adapter_config_sha256 is not None
            else None
        ),
        round_index=getattr(namespace, "round_index", None),
        owner_token_sha256=getattr(namespace, "owner_token_sha256", None),
    )


def dispatch_panel_cli_v5(
    argv: Sequence[str],
    *,
    emit: Callable[[str], None] = print,
) -> int:
    """Own panel build/verification without exposing held-out content."""

    try:
        namespace = build_parser_v5().parse_args(tuple(argv))
        if namespace.command not in _PANEL_COMMANDS:
            raise V5CliFailure("invalid_request")
        repository = LocalArtifactRepositoryV5(Path(namespace.artifact_root))
        if namespace.command == "verify-panels":
            if not namespace.keep_held_out_sealed:
                raise V5CliFailure("invalid_request")
            projection = verify_panels_v5(
                repository=repository,
                discovery_ref=ArtifactRefV5(namespace.discovery_plan_path, namespace.discovery_plan_sha256),
                confirmation_ref=ArtifactRefV5(
                    namespace.confirmation_plan_path,
                    namespace.confirmation_plan_sha256,
                ),
                qualification_ref=ArtifactRefV5(
                    namespace.qualification_plan_path,
                    namespace.qualification_plan_sha256,
                ),
            )
        else:
            pit_bundle_ref = ArtifactRefV5(namespace.pit_bundle_path, namespace.pit_bundle_sha256)
            prices_provenance_ref = ArtifactRefV5(
                namespace.prices_provenance_path,
                namespace.prices_provenance_sha256,
            )
            if namespace.command == "init-stage-ledgers":
                projection = initialize_stage_ledgers_v5(
                    repository=repository,
                    pit_bundle_ref=pit_bundle_ref,
                    prices_provenance_ref=prices_provenance_ref,
                    confirmation_ledger_path=namespace.confirmation_ledger_path,
                    qualification_ledger_path=namespace.qualification_ledger_path,
                )
            else:
                _plans, references = build_panels_v5(
                    repository=repository,
                    pit_bundle_ref=pit_bundle_ref,
                    prices_provenance_ref=prices_provenance_ref,
                    start_date=namespace.start_date,
                    end_date=namespace.end_date,
                    partition_seed=namespace.partition_seed,
                    target=AnnualizedReturnTargetV5.from_text(namespace.target_pct),
                    confirmation_ledger_path=namespace.confirmation_ledger_path,
                    qualification_ledger_path=namespace.qualification_ledger_path,
                    discovery_output_path=namespace.discovery_output_path,
                    confirmation_output_path=namespace.confirmation_output_path,
                    qualification_output_path=namespace.qualification_output_path,
                )
                projection = {
                    "schema_version": 5,
                    "status": "created",
                    "discovery_plan_sha256": references[0].sha256,
                    "confirmation_plan_sha256": references[1].sha256,
                    "qualification_plan_sha256": references[2].sha256,
                }
        emit("PIT_OPTIMIZER_V5_PANELS=" + canonical_json_bytes_v5(projection).decode("utf-8"))
        return 0
    except SystemExit:
        raise
    except BaseException:
        emit(
            "PIT_OPTIMIZER_V5_PANELS="
            + canonical_json_bytes_v5(
                {"schema_version": 5, "status": "failed", "reason": "panel_command_failed"}
            ).decode("utf-8")
        )
        return 2


def _manifest_ref_argument_v5(namespace: argparse.Namespace, prefix: str) -> ArtifactRefV5:
    attribute = prefix.replace("-", "_")
    return ArtifactRefV5(
        getattr(namespace, f"{attribute}_path"),
        getattr(namespace, f"{attribute}_sha256"),
    )


def _manifest_search_capabilities_v5(namespace: argparse.Namespace) -> SearchCapabilitiesV5:
    return SearchCapabilitiesV5(
        hypotheses_per_investigator=namespace.hypotheses_per_investigator,
        max_tunable_axes=namespace.max_tunable_axes,
        max_variants_per_template=namespace.max_variants_per_template,
        max_discovery_survivors_per_template=(namespace.max_discovery_survivors_per_template),
        archive_capacity=namespace.archive_capacity,
        max_feedback_rounds=namespace.max_feedback_rounds,
        allow_full_source_escape=not namespace.deny_full_source_escape,
        investigator_memory_max_bytes=namespace.investigator_memory_max_bytes,
    )


def _manifest_resource_capabilities_v5(namespace: argparse.Namespace) -> ResourceCapabilitiesV5:
    return ResourceCapabilitiesV5(
        max_parallel_evaluations=namespace.max_parallel_evaluations,
        evaluation_cpu_limit=namespace.evaluation_cpu_limit,
        evaluation_memory_mib=namespace.evaluation_memory_mib,
        evaluation_pid_limit=namespace.evaluation_pid_limit,
        evaluation_output_limit_bytes=namespace.evaluation_output_limit_bytes,
        policy_method_timeout_seconds=namespace.policy_method_timeout_seconds,
        worker_startup_timeout_seconds=namespace.worker_startup_timeout_seconds,
        role_call_timeout_seconds=namespace.role_call_timeout_seconds,
        mechanics_timeout_seconds=namespace.mechanics_timeout_seconds,
        quick_timeout_seconds=namespace.quick_timeout_seconds,
        discovery_episode_timeout_seconds=namespace.discovery_episode_timeout_seconds,
        round_wall_timeout_seconds=namespace.round_wall_timeout_seconds,
        campaign_wall_timeout_seconds=namespace.campaign_wall_timeout_seconds,
        cleanup_timeout_seconds=namespace.cleanup_timeout_seconds,
    )


def _manifest_provider_capabilities_v5(namespace: argparse.Namespace) -> ProviderCapabilitiesV5 | None:
    prices = (
        namespace.input_usd_per_million_tokens,
        namespace.output_usd_per_million_tokens,
    )
    if (prices[0] is None) != (prices[1] is None):
        raise ValueError("provider price authority must supply both token prices")
    price_upper_bound = None
    if prices[0] is not None and prices[1] is not None:
        if namespace.provider_model is None:
            raise ValueError("provider prices require an authorized model")
        price_upper_bound = ModelPriceUpperBoundV5(
            model=namespace.provider_model,
            input_usd_per_million_tokens=prices[0],
            output_usd_per_million_tokens=prices[1],
        )
    return provider_capabilities_from_values_v5(
        model=namespace.provider_model,
        maximum_role_calls=namespace.maximum_role_calls,
        maximum_total_tokens=namespace.maximum_total_tokens,
        maximum_output_tokens_per_role=namespace.maximum_output_tokens_per_role,
        maximum_usd=namespace.maximum_usd,
        automatic_retries=namespace.automatic_retries,
        schema_repair_calls=namespace.schema_repair_calls,
        input_token_overhead_upper_bound=(namespace.input_token_overhead_upper_bound),
        price_upper_bound=price_upper_bound,
    )


def _manifest_projection_v5(
    authenticated: AuthenticatedCampaignManifestV5,
    *,
    status: str,
) -> dict[str, object]:
    manifest = authenticated.manifest
    provider = manifest.provider
    return {
        "schema_version": 5,
        "status": status,
        "manifest_ref": authenticated.manifest_ref.to_primitive(),
        "campaign_id": manifest.campaign_id,
        "target_pct": manifest.target.to_text(),
        "source_commit": manifest.source_commit,
        "pit_data_scope": manifest.pit_data_scope,
        "semantic_mode": manifest.semantic_mode,
        "provider": (
            None
            if provider is None
            else {
                "model": provider.model,
                "maximum_role_calls": provider.maximum_role_calls,
                "maximum_total_tokens": provider.maximum_total_tokens,
                "maximum_output_tokens_per_role": provider.maximum_output_tokens_per_role,
                "maximum_usd": provider.maximum_usd,
                "automatic_retries": provider.automatic_retries,
                "schema_repair_calls": provider.schema_repair_calls,
            }
        ),
        "policy_scope_ref": manifest.policy_scope_ref.to_primitive(),
        "apply": manifest.apply,
        "qualification_allowed": manifest.qualification_allowed,
        "full_replay_allowed": manifest.full_replay_allowed,
        "authenticated_artifact_count": len(authenticated.graph.authenticated),
    }


def dispatch_manifest_cli_v5(
    argv: Sequence[str],
    *,
    emit: Callable[[str], None] = print,
) -> int:
    """Build, verify, or render an authenticated discovery manifest."""

    try:
        namespace = build_parser_v5().parse_args(tuple(argv))
        if namespace.command not in _MANIFEST_COMMANDS:
            raise V5CliFailure("invalid_request")
        artifact_root = Path(namespace.artifact_root)
        repository = LocalArtifactRepositoryV5(artifact_root)
        if namespace.command == "build-manifest":
            source_snapshot = capture_clean_policy_snapshot_v5(
                source_root=Path(namespace.source_root),
                git_executable=Path(namespace.git_executable),
                expected_source_commit=namespace.source_commit,
            )
            authenticated = build_campaign_manifest_v5(
                repository=repository,
                campaign_id=namespace.campaign_id,
                target=AnnualizedReturnTargetV5.from_text(namespace.target_pct),
                source_snapshot=source_snapshot,
                execution_profile_ref=_manifest_ref_argument_v5(namespace, "execution-profile"),
                evaluator_contract_ref=_manifest_ref_argument_v5(namespace, "evaluator-contract"),
                baseline_authority_ref=_manifest_ref_argument_v5(namespace, "baseline-authority"),
                panel_plan_ref=_manifest_ref_argument_v5(namespace, "panel-plan"),
                sandbox_profile_ref=_manifest_ref_argument_v5(namespace, "sandbox-profile"),
                policy_scope_path=namespace.policy_scope_output_path,
                manifest_path=namespace.output_path,
                search=_manifest_search_capabilities_v5(namespace),
                provider=_manifest_provider_capabilities_v5(namespace),
                resources=_manifest_resource_capabilities_v5(namespace),
                pit_data_scope=namespace.pit_data_scope,
                semantic_mode=namespace.semantic_mode,
            )
            projection = _manifest_projection_v5(authenticated, status="created")
        else:
            authenticated = authenticate_campaign_manifest_v5(
                repository=repository,
                manifest_ref=ArtifactRefV5(
                    namespace.manifest_path,
                    namespace.manifest_sha256,
                ),
            )
            projection = _manifest_projection_v5(authenticated, status="verified")
            if namespace.command == "render-command":
                if (namespace.adapter_config_path is None) != (namespace.adapter_config_sha256 is None):
                    raise ValueError("Supply both --adapter-config-path and --adapter-config-sha256")
                if namespace.adapter_config_path is None:
                    rendered = render_discovery_command_v5(authenticated=authenticated)
                else:
                    from core.pit_optimizer_v5.operations import render_production_command_v5

                    if namespace.owner_token_sha256 is None:
                        raise ValueError("Supply the --owner-token-sha256 returned by prepare-production")
                    rendered = render_production_command_v5(
                        repository=repository,
                        authenticated=authenticated,
                        artifact_root=artifact_root,
                        adapter_config_ref=ArtifactRefV5(
                            namespace.adapter_config_path, namespace.adapter_config_sha256
                        ),
                        round_index=namespace.round_index,
                        owner_token_sha256=namespace.owner_token_sha256,
                        command=namespace.execution_command,
                    )
                projection["authorization"] = rendered
        emit("PIT_OPTIMIZER_V5_MANIFEST=" + canonical_json_bytes_v5(projection).decode("utf-8"))
        return 0
    except SystemExit:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        emit(
            "PIT_OPTIMIZER_V5_MANIFEST="
            + canonical_json_bytes_v5(
                {"schema_version": 5, "status": "failed", "reason": "manifest_command_failed", "diagnostic": str(exc)}
            ).decode("utf-8")
        )
        return 2


def dispatch_confirmation_cli_v5(argv: Sequence[str], *, emit: Callable[[str], None] = print) -> int:
    """Only explicit authenticated references can select or open confirmation."""
    from core.pit_optimizer_v5.confirmation import ConfirmationCleanupV5, build_confirmation_attempt, run_confirmation
    from core.pit_optimizer_v5.contracts import ConfirmationOutcomeV5, RetirementLedgerLocatorV5

    try:
        namespace = build_parser_v5().parse_args(argv)
        root = Path(namespace.artifact_root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("confirmation requires an existing absolute artifact root")
        repository = LocalArtifactRepositoryV5(root)
        if namespace.command == "build-confirmation-attempt":
            reference = build_confirmation_attempt(
                repository=repository,
                discovery_manifest_ref=_manifest_ref_argument_v5(namespace, "discovery-manifest"),
                discovery_checkpoint_ref=_manifest_ref_argument_v5(namespace, "discovery-checkpoint"),
                discovery_archive_ref=_manifest_ref_argument_v5(namespace, "discovery-archive"),
                finalized_campaign_ref=_manifest_ref_argument_v5(namespace, "finalized-campaign"),
                confirmation_plan_ref=_manifest_ref_argument_v5(namespace, "confirmation-plan"),
                scenario_grid_ref=_manifest_ref_argument_v5(namespace, "scenario-grid"),
                retirement_ledger=RetirementLedgerLocatorV5(
                    namespace.confirmation_ledger_path, _manifest_ref_argument_v5(namespace, "preopen-snapshot")
                ),
                execution_adapter_ref=_manifest_ref_argument_v5(namespace, "execution-adapter"),
                attempt_id=namespace.attempt_id,
                output_path=namespace.output_path,
            )
            payload = {"schema_version": 5, "status": "sealed", "attempt_sha256": reference.sha256, "provider_calls": 0}
        else:
            reference = run_confirmation(
                repository=repository, attempt_ref=_manifest_ref_argument_v5(namespace, "attempt")
            )
            outcome = repository.load_typed_artifact(reference, value_type=ConfirmationOutcomeV5)
            cleanup = repository.load_typed_artifact(outcome.cleanup_evidence_ref, value_type=ConfirmationCleanupV5)
            payload = {
                "schema_version": 5,
                "status": outcome.status,
                "outcome_sha256": reference.sha256,
                "eligible_to_request_qualification": outcome.eligible_to_request_qualification,
                "provider_calls": 0,
                "source_unchanged": cleanup.source_unchanged,
                "cleanup_complete": cleanup.cleanup_complete,
            }
        exit_code = 0 if payload["status"] in {"sealed", "completed"} else 1
    except V5CliFailure as exc:
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": exc.reason}, exc.exit_code
    except SystemExit as exc:
        if exc.code == 0:
            raise
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": "invalid_request"}, 2
    except (ValueError, TypeError, OSError, RuntimeError):
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": "confirmation_authority_invalid"}, 2
    emit("PIT_OPTIMIZER_V5_CONFIRMATION=" + canonical_json_bytes_v5(payload).decode("utf-8"))
    return exit_code


def dispatch_qualification_cli_v5(argv: Sequence[str], *, emit: Callable[[str], None] = print) -> int:
    """Only explicit authenticated references can select or open qualification."""
    from core.pit_optimizer_v5.qualification import (
        build_qualification_attempt,
        run_qualification,
        qualification_cleanup_evidence_v5,
        qualification_evidence_summary_v5,
    )
    from core.pit_optimizer_v5.contracts import QualificationOutcomeV5, RetirementLedgerLocatorV5

    try:
        namespace = build_parser_v5().parse_args(argv)
        root = Path(namespace.artifact_root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("qualification requires an existing absolute artifact root")
        repository = LocalArtifactRepositoryV5(root)
        if namespace.command == "build-qualification-attempt":
            reference = build_qualification_attempt(
                repository=repository,
                confirmation_outcome_ref=_manifest_ref_argument_v5(namespace, "confirmation-outcome"),
                qualification_plan_ref=_manifest_ref_argument_v5(namespace, "qualification-plan"),
                retirement_ledger=RetirementLedgerLocatorV5(
                    namespace.qualification_ledger_path, _manifest_ref_argument_v5(namespace, "preopen-snapshot")
                ),
                operator_approved=namespace.operator_approved,
                attempt_id=namespace.attempt_id,
                output_path=namespace.output_path,
            )
            payload = {"schema_version": 5, "status": "sealed", "attempt_sha256": reference.sha256, "provider_calls": 0}
        else:
            reference = run_qualification(
                repository=repository, attempt_ref=_manifest_ref_argument_v5(namespace, "attempt")
            )
            outcome = repository.load_typed_artifact(reference, value_type=QualificationOutcomeV5)
            cleanup = qualification_cleanup_evidence_v5(repository, outcome)
            payload = {
                "schema_version": 5,
                "status": outcome.status,
                "outcome_sha256": reference.sha256,
                "target_pct": outcome.target_pct,
                "baseline_cagr_pct": outcome.baseline_cagr_pct,
                "candidate_cagr_pct": outcome.candidate_cagr_pct,
                "candidate_excess_cagr_pct": outcome.candidate_excess_cagr_pct,
                "target_reached": outcome.target_reached,
                "baseline_beaten": outcome.baseline_beaten,
                "qualified": outcome.qualified,
                "provider_calls": 0,
                "source_unchanged": cleanup.source_unchanged,
                "cleanup_complete": cleanup.cleanup_complete,
            }
            payload["evidence"] = qualification_evidence_summary_v5(repository, outcome)
        exit_code = 0 if payload["status"] in {"sealed", "completed"} else 1
    except V5CliFailure as exc:
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": exc.reason}, exc.exit_code
    except SystemExit as exc:
        if exc.code == 0:
            raise
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": "invalid_request"}, 2
    except (ValueError, TypeError, OSError, RuntimeError):
        payload, exit_code = {"schema_version": 5, "status": "rejected", "reason": "qualification_authority_invalid"}, 2
    emit("PIT_OPTIMIZER_V5_CONFIRMATION=" + canonical_json_bytes_v5(payload).decode("utf-8"))
    return exit_code


def dispatch_readiness_cli_v5(argv: Sequence[str], *, emit: Callable[[str], None] = print) -> int:
    """Read-only qualification authentication followed by a non-executable projection."""
    from core.pit_optimizer_v5.readiness import ReadinessGitAuthorityV5, ReplayReadinessFailureV5, full_replay_readiness

    try:
        namespace = build_parser_v5().parse_args(argv)
        if namespace.command != "full-replay-readiness":
            raise V5CliFailure("invalid_request")
        root = Path(namespace.artifact_root)
        if not root.is_absolute() or not root.is_dir():
            raise V5CliFailure("invalid_request")
        repository = LocalArtifactRepositoryV5(root)
        reference = full_replay_readiness(
            repository=repository,
            qualification_outcome_ref=_manifest_ref_argument_v5(namespace, "qualification-outcome"),
            output_path=namespace.output_path,
            git_authority=ReadinessGitAuthorityV5(namespace.trusted_git_executable, namespace.trusted_git_sha256),
        )
        payload = {
            "schema_version": 5,
            "status": "ready",
            "readiness_sha256": reference.sha256,
            "projection": "local_full_replay_requires_separate_explicit_decision",
            "executable": False,
            "replay_started": False,
            "provider_calls": 0,
        }
        exit_code = 0
    except ReplayReadinessFailureV5 as exc:
        payload, exit_code = {"schema_version": 5, "status": "blocked", "blocker": exc.code}, 2
    except (V5CliFailure, ValueError, TypeError, OSError, RuntimeError):
        payload, exit_code = {"schema_version": 5, "status": "blocked", "blocker": "invalid_request"}, 2
    emit("PIT_OPTIMIZER_V5_READINESS=" + canonical_json_bytes_v5(payload).decode("utf-8"))
    return exit_code


def dispatch_baseline_cli_v5(
    argv: Sequence[str],
    *,
    emit: Callable[[str], None] = print,
    repository_factory=LocalArtifactRepositoryV5,
    worker_factory=None,
) -> int:
    """Explicit V5 roots and digest references; verification owns no runtime."""
    from core.pit_optimizer_v5.baseline import (
        BaselineHostAuthorityV5,
        LocalBaselineCaptureFactoryV5,
        build_sandbox_profile_v5,
        capture_baseline_v5,
        verify_baseline_v5,
        write_execution_profile_v5,
    )

    try:
        args = build_parser_v5().parse_args(tuple(argv))
        if args.command not in _BASELINE_COMMANDS:
            raise ValueError("invalid baseline command")
        root = Path(args.artifact_root)
        if not root.is_absolute() or ".." in root.parts or root.parts[-2:] != (".artifacts", "pit-optimizer-v5"):
            raise ValueError("baseline requires the explicit canonical V5 artifact root")
        repository = repository_factory(root)
        if args.command == "verify-baseline":
            payload = verify_baseline_v5(
                repository=repository,
                authority_ref=ArtifactRefV5(args.authority, args.authority_sha256),
            )
        else:
            ArtifactRefV5(args.output_path, "0" * 64)
            if args.command == "capture-baseline":
                from contextlib import nullcontext

                host = BaselineHostAuthorityV5(
                    args.source_root,
                    args.scratch_root,
                    args.trusted_git_executable,
                    args.trusted_git_sha256,
                    args.docker_executable,
                    args.docker_sha256,
                )
                factory_context = (
                    LocalBaselineCaptureFactoryV5(repository=repository, host=host)
                    if worker_factory is None
                    else nullcontext(worker_factory)
                )
                with factory_context as selected_factory:
                    ref = capture_baseline_v5(
                        repository=repository,
                        inputs_ref=ArtifactRefV5(args.capture_inputs_path, args.capture_inputs_sha256),
                        output_path=args.output_path,
                        worker_factory=selected_factory,
                    )
            elif args.command == "prepare-baseline-inputs":
                from core.pit_optimizer_v5.baseline_preparation import (
                    prepare_baseline_inputs_v5,
                    prepare_composed_baseline_inputs_v5,
                )

                common = {
                    "repository": repository,
                    "output_path": args.output_path,
                    "execution_profile_ref": ArtifactRefV5(args.execution_profile_path, args.execution_profile_sha256),
                    "sandbox_profile_ref": ArtifactRefV5(args.sandbox_profile_path, args.sandbox_profile_sha256),
                    "panel_plan_ref": ArtifactRefV5(args.panel_plan_path, args.panel_plan_sha256),
                    "evaluator_source_ref": ArtifactRefV5(args.evaluator_source_path, args.evaluator_source_sha256),
                    "identity_transition_ref": ArtifactRefV5(
                        args.identity_transition_path, args.identity_transition_sha256
                    ),
                    "resources_ref": ArtifactRefV5(args.resources_path, args.resources_sha256),
                    "source_root": args.source_root,
                    "scratch_root": args.scratch_root,
                    "git_executable": args.git_executable,
                    "git_sha256": args.git_sha256,
                    "docker_executable": args.docker_executable,
                    "docker_sha256": args.docker_sha256,
                }
                if args.compose_source:
                    required_values = (
                        args.source_commit,
                        args.immutable_constraints_path,
                        args.immutable_constraints_sha256,
                        args.source_bundle_output_path,
                        args.policy_revision_output_path,
                        args.policy_scope_output_path,
                        args.evaluator_contract_output_path,
                    )
                    explicit_values = (
                        args.evaluator_contract_path,
                        args.evaluator_contract_sha256,
                        args.baseline_policy_revision_path,
                        args.baseline_policy_revision_sha256,
                        args.source_bundle_path,
                        args.source_bundle_sha256,
                        args.policy_scope_path,
                        args.policy_scope_sha256,
                    )
                    if any(value is None for value in required_values) or any(
                        value is not None for value in explicit_values
                    ):
                        raise ValueError("composed baseline inputs are incomplete or mixed with explicit source refs")
                    prepared = prepare_composed_baseline_inputs_v5(
                        **common,
                        source_commit=args.source_commit,
                        immutable_constraints_ref=ArtifactRefV5(
                            args.immutable_constraints_path, args.immutable_constraints_sha256
                        ),
                        source_bundle_output_path=args.source_bundle_output_path,
                        policy_revision_output_path=args.policy_revision_output_path,
                        policy_scope_output_path=args.policy_scope_output_path,
                        evaluator_contract_output_path=args.evaluator_contract_output_path,
                    )
                else:
                    values = (
                        args.evaluator_contract_path,
                        args.evaluator_contract_sha256,
                        args.baseline_policy_revision_path,
                        args.baseline_policy_revision_sha256,
                        args.source_bundle_path,
                        args.source_bundle_sha256,
                        args.policy_scope_path,
                        args.policy_scope_sha256,
                    )
                    if any(value is None for value in values):
                        raise ValueError("explicit baseline inputs are incomplete")
                    prepared = prepare_baseline_inputs_v5(
                        **common,
                        evaluator_contract_ref=ArtifactRefV5(
                            args.evaluator_contract_path, args.evaluator_contract_sha256
                        ),
                        baseline_policy_revision_ref=ArtifactRefV5(
                            args.baseline_policy_revision_path, args.baseline_policy_revision_sha256
                        ),
                        source_bundle_ref=ArtifactRefV5(args.source_bundle_path, args.source_bundle_sha256),
                        policy_scope_ref=ArtifactRefV5(args.policy_scope_path, args.policy_scope_sha256),
                    )
                payload = {
                    "schema_version": 5,
                    "status": "created",
                    "artifact_ref": prepared.inputs_ref,
                    "capture_argv": prepared.capture_argv,
                    "powershell_command": prepared.powershell_command,
                    "provider_calls": 0,
                    "evaluation_performed": False,
                }
                ref = None
            elif args.command == "write-execution-profile":
                ref = write_execution_profile_v5(repository=repository, output_path=args.output_path)
            else:
                ref = build_sandbox_profile_v5(
                    repository=repository,
                    resources_ref=ArtifactRefV5(args.resources_path, args.resources_sha256),
                    evaluator_source_ref=ArtifactRefV5(args.evaluator_source_path, args.evaluator_source_sha256),
                    image_name=args.image_name,
                    image_digest=args.image_digest,
                    output_path=args.output_path,
                )
            if args.command != "prepare-baseline-inputs":
                payload = {"schema_version": 5, "status": "created", "artifact_ref": ref}
        exit_code = 0
    except Exception:
        # Includes bounded process timeouts and malformed graph metadata; do
        # not disclose source paths or subprocess arguments in CLI errors.
        payload, exit_code = {"schema_version": 5, "status": "blocked", "blocker": "invalid_request"}, 2
    emit("PIT_OPTIMIZER_V5_BASELINE=" + canonical_json_bytes_v5(payload).decode("utf-8"))
    return exit_code


def dispatch_v5_cli(
    argv: Sequence[str],
    *,
    services: V5CommandServices | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Parse and dispatch one command; dependency construction stays outside parsing."""
    if argv and argv[0] in {"prepare-production", "prepare-development", "run-campaign", "resume-campaign"}:
        from core.pit_optimizer_v5.operations import dispatch_production_operations_cli_v5

        return dispatch_production_operations_cli_v5(argv, emit=emit)
    if argv and argv[0] in {"import-development", "controller-request", "controller-response"}:
        from core.pit_optimizer_v5.development_preparation import dispatch_development_cli_v5

        return dispatch_development_cli_v5(argv, emit=emit)
    if argv and argv[0] in _BASELINE_COMMANDS:
        return dispatch_baseline_cli_v5(argv, emit=emit)
    if argv and argv[0] in _PANEL_COMMANDS:
        return dispatch_panel_cli_v5(argv, emit=emit)
    if argv and argv[0] in _MANIFEST_COMMANDS:
        return dispatch_manifest_cli_v5(argv, emit=emit)
    if argv and argv[0] in _CONFIRMATION_COMMANDS:
        return dispatch_confirmation_cli_v5(argv, emit=emit)
    if argv and argv[0] in _QUALIFICATION_COMMANDS:
        return dispatch_qualification_cli_v5(argv, emit=emit)
    if argv and argv[0] == "full-replay-readiness":
        return dispatch_readiness_cli_v5(argv, emit=emit)
    command: V5CommandName = (
        argv[0]  # type: ignore[assignment]
        if argv and argv[0] in _COMMANDS
        else "summarize"
    )
    try:
        request = parse_v5_args(argv)
        command = request.command
        selected = ProductionV5CommandServices() if services is None else services
        if not isinstance(selected, V5CommandServices):
            raise V5CliFailure("production_dependency_invalid")
        summary = selected.execute(request)
        if type(summary) is not OptimizerSummaryV5:
            raise V5CliFailure("runtime_result_invalid")
        exit_code = 0
    except PaidRoleRecoveryRequiredV5 as exc:
        emit(
            "PIT_OPTIMIZER_V5_RECOVERY="
            + canonical_json_bytes_v5(
                {
                    "schema_version": 5,
                    "status": "recovery_required",
                    "round_index": exc.round_index,
                    "role": exc.role,
                    "reason": exc.reason,
                    "diagnostic": str(exc),
                }
            ).decode("utf-8")
        )
        summary = unavailable_summary_v5(command, "runtime_failed")
        exit_code = 2
    except V5CliFailure as exc:
        summary = exc.summary or unavailable_summary_v5(command, exc.reason)
        exit_code = exc.exit_code
    except SystemExit as exc:
        if exc.code == 0:
            raise
        summary = unavailable_summary_v5(command, "invalid_request")
        exit_code = 2
    except BaseException:
        summary = unavailable_summary_v5(command, "internal_failure")
        exit_code = 1
    payload = canonical_json_bytes_v5(summary.to_primitive()).decode("utf-8")
    emit("PIT_OPTIMIZER_V5_SUMMARY=" + payload)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    return dispatch_v5_cli(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CampaignAuthoritiesV5",
    "ProductionAdapterConfigV5",
    "ProductionRoundCompositionV5",
    "ProductionRoundFactoryV5",
    "ProductionV5CommandServices",
    "V5CliFailure",
    "V5CliRequest",
    "V5CommandServices",
    "build_parser_v5",
    "dispatch_v5_cli",
    "dispatch_manifest_cli_v5",
    "dispatch_panel_cli_v5",
    "dispatch_confirmation_cli_v5",
    "parse_v5_args",
]
