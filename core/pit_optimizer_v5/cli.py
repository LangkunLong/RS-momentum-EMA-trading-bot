"""Thin local command boundary for the PIT optimizer V5 runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import ntpath
from pathlib import Path
import re
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

from core.pit_optimizer_v5.artifacts import ArtifactRepositoryFailureV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    validate_campaign_manifest_bindings_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.provider import (
    GatewayCompletionProviderV5,
    LedgerBackedRoleInvokerV5,
)
from core.pit_optimizer_v5.production_provider import (
    LocalRoleAuthorizationLedgerV5,
    OpenRouterOneShotJsonCompletionV5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundDependenciesV5,
    FeedbackRoundInputV5,
    FeedbackRoundResultV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.sandbox import (
    AuthenticatedCandidateBaseOperationsV5,
    AuthenticatedContainerExecutorV5,
    AuthenticatedSandboxMountFactoryV5,
    DockerCandidateRuntimeV5,
)
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5
from core.pit_optimizer_v5.summary import OptimizerSummaryV5, summarize_repository_v5, unavailable_summary_v5
from core.pit_optimizer_v5.workspace import AuthenticatedGitWorkspaceDriverV5


V5CommandName = Literal["run", "resume", "verify-run", "summarize", "import-v4-candidate"]
_COMMANDS = frozenset({"run", "resume", "verify-run", "summarize", "import-v4-candidate"})
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
        "production_composition_unavailable",
        "production_config_invalid",
        "production_config_missing",
        "production_dependency_invalid",
        "production_provider_invalid",
        "production_verifier_unavailable",
        "resume_state_missing",
        "run_not_fresh",
        "runtime_failed",
        "runtime_result_invalid",
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
        elif self.round_index is not None or self.owner_token_sha256 is not None or self.adapter_config_ref is not None:
            raise ValueError("read-only V5 commands cannot carry execution authority")


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
        ):
            raise ValueError("V5 campaign authority identity differs from the manifest")


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
        if type(self.dependencies.invoker) is not LedgerBackedRoleInvokerV5:
            raise V5CliFailure("production_provider_invalid")
        runner = self.dependencies.invoker.runner
        lifecycle = runner.authorization_lifecycle
        provider = runner.completion_provider
        candidate = self.dependencies.candidates
        if type(provider) is not GatewayCompletionProviderV5 or type(candidate) is not DockerCandidateRuntimeV5:
            raise V5CliFailure("production_dependency_invalid")
        gateway = provider.gateway
        panel_evaluator = candidate.panel_evaluator
        executor = panel_evaluator.evaluator.executor
        if (
            type(gateway) is not OpenRouterOneShotJsonCompletionV5
            or type(lifecycle) is not LocalRoleAuthorizationLedgerV5
            or self.dependencies.invoker.reconciler is not lifecycle
            or lifecycle.campaign_manifest_sha256 != authorities.manifest.sha256
            or lifecycle.ledger_identity_sha256 != adapter_config.ledger_identity_sha256
            or lifecycle.audit_store_identity_sha256 != adapter_config.audit_store_identity_sha256
            or gateway.gateway_identity_sha256 != adapter_config.gateway_identity_sha256
            or gateway.ledger_identity_sha256 != lifecycle.ledger_identity_sha256
            or gateway.audit_store_identity_sha256 != lifecycle.audit_store_identity_sha256
            or candidate.manifest != authorities.manifest
            or candidate.panel_plan != authorities.panel_plan
            or candidate.evaluator_contract != authorities.evaluator_contract
            or candidate.sandbox_profile != authorities.sandbox_profile
            or candidate.owner.campaign_id != authorities.manifest.campaign_id
            or candidate.owner.round_index != round_index
            or candidate.owner.owner_token_sha256 != owner_token_sha256
            or type(candidate.base_operations) is not AuthenticatedCandidateBaseOperationsV5
            or candidate.base_operations.base_identity_sha256 != adapter_config.candidate_base_identity_sha256
            or type(candidate.base_operations.materializer.driver) is not AuthenticatedGitWorkspaceDriverV5
            or candidate.base_operations.materializer.driver.driver_identity_sha256
            != adapter_config.workspace_driver_identity_sha256
            or type(candidate.mount_factory) is not AuthenticatedSandboxMountFactoryV5
            or candidate.mount_factory.mount_identity_sha256 != adapter_config.mount_factory_identity_sha256
            or type(executor) is not AuthenticatedContainerExecutorV5
            or executor.executor_identity_sha256 != adapter_config.container_executor_identity_sha256
        ):
            raise V5CliFailure("production_dependency_invalid")


class ProductionRoundFactoryV5:
    """Concrete campaign factory holding authenticated, non-CLI adapter capabilities."""

    def __init__(
        self,
        *,
        adapter_config_ref: ArtifactRefV5,
        adapter_config: ProductionAdapterConfigV5,
        dependencies_by_round: Mapping[int, FeedbackRoundDependenciesV5],
    ) -> None:
        if (
            type(adapter_config_ref) is not ArtifactRefV5
            or type(adapter_config) is not ProductionAdapterConfigV5
            or adapter_config_ref.sha256 != canonical_sha256_v5(adapter_config)
            or not isinstance(dependencies_by_round, Mapping)
            or not dependencies_by_round
        ):
            raise ValueError("production round factory authority is invalid")
        closed: dict[int, FeedbackRoundDependenciesV5] = {}
        for round_index, dependencies in dependencies_by_round.items():
            if type(round_index) is not int or round_index < 1 or type(dependencies) is not FeedbackRoundDependenciesV5:
                raise ValueError("production round factory dependency map is invalid")
            closed[round_index] = dependencies
        self.adapter_config_ref = adapter_config_ref
        self.adapter_config = adapter_config
        self._dependencies_by_round = MappingProxyType(closed)

    def compose_round(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        round_index: int,
        owner_token_sha256: str,
        adapter_config: ProductionAdapterConfigV5,
    ) -> ProductionRoundCompositionV5:
        if (
            adapter_config != self.adapter_config
            or authorities.manifest.sha256 != self.adapter_config.campaign_manifest_sha256
            or repository.root_identity_sha256 != self.adapter_config.repository_root_identity_sha256
        ):
            raise V5CliFailure("production_authority_mismatch")
        try:
            template = self._dependencies_by_round[round_index]
        except KeyError:
            raise V5CliFailure("production_composition_unavailable") from None
        dependencies = FeedbackRoundDependenciesV5(
            persistence=repository,
            invoker=template.invoker,
            requests=template.requests,
            novelty=template.novelty,
            candidates=template.candidates,
            records=template.records,
            archive_reducers=template.archive_reducers,
            clock=template.clock,
            cancellation=template.cancellation,
            cleanup=template.cleanup,
        )
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

    def verify_run(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
    ) -> None:
        if (
            authorities.manifest.sha256 != self.adapter_config.campaign_manifest_sha256
            or repository.root_identity_sha256 != self.adapter_config.repository_root_identity_sha256
        ):
            raise V5CliFailure("production_authority_mismatch")


_PRODUCTION_FACTORIES: dict[tuple[str, str, str], ProductionRoundFactoryV5] = {}


def _factory_key(artifact_root: Path, manifest_sha256: str, config_sha256: str) -> tuple[str, str, str]:
    return (ntpath.normcase(ntpath.normpath(str(artifact_root))), manifest_sha256, config_sha256)


def register_production_round_factory_v5(
    *,
    artifact_root: Path,
    manifest_sha256: str,
    factory: ProductionRoundFactoryV5,
) -> None:
    """Register one exact local live-capability factory; CLI text cannot construct one."""

    if (
        not isinstance(artifact_root, Path)
        or not artifact_root.is_absolute()
        or type(factory) is not ProductionRoundFactoryV5
        or factory.adapter_config.campaign_manifest_sha256 != manifest_sha256
    ):
        raise ValueError("production factory registration is invalid")
    key = _factory_key(artifact_root, manifest_sha256, factory.adapter_config_ref.sha256)
    existing = _PRODUCTION_FACTORIES.get(key)
    if existing is not None and existing is not factory:
        raise ValueError("production factory registration conflicts")
    _PRODUCTION_FACTORIES[key] = factory


@runtime_checkable
class V5CommandServices(Protocol):
    def execute(self, request: V5CliRequest) -> OptimizerSummaryV5: ...


class ProductionV5CommandServices:
    """Authenticate local authorities and invoke only approved production shapes."""

    def __init__(self, *, factory: ProductionRoundFactoryV5 | None = None) -> None:
        if factory is not None and type(factory) is not ProductionRoundFactoryV5:
            raise ValueError("V5 production factory is invalid")
        self._factory = factory

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
    ) -> tuple[bool, bool]:
        role_evidence_seen = False
        for round_index in range(1, authorities.manifest.search.max_feedback_rounds + 1):
            events = repository.load_round_events(
                campaign_id=authorities.manifest.campaign_id,
                round_index=round_index,
            )
            for event in events:
                payload = repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
                if event.event_kind == "role_completion":
                    role_evidence_seen = True
                    repository.load_role_invocation(payload)  # type: ignore[arg-type]
            repository.verify_candidate_execution_index(
                campaign_id=authorities.manifest.campaign_id,
                round_index=round_index,
            )
        checkpoint = repository.load_checkpoint()
        if checkpoint is not None:
            tuple(repository.load_experiment(reference) for reference in checkpoint.record_refs)
        return role_evidence_seen, checkpoint is not None

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
        authorities = self._authorities(repository, request.manifest_ref)
        if request.command in {"verify-run", "summarize"}:
            role_evidence, checkpoint_seen = self._verify_local_run(repository, authorities)
            if self._factory is not None:
                self._factory.verify_run(repository=repository, authorities=authorities)
            elif request.command == "verify-run" and (role_evidence or checkpoint_seen):
                raise V5CliFailure("production_verifier_unavailable")
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
        factory = self._factory
        if factory is None:
            factory = _PRODUCTION_FACTORIES.get(
                _factory_key(
                    request.artifact_root,
                    authorities.manifest.sha256,
                    request.adapter_config_ref.sha256,
                )
            )
        if factory is None:
            raise V5CliFailure("production_composition_unavailable")
        if factory.adapter_config_ref != request.adapter_config_ref or factory.adapter_config != adapter_config:
            raise V5CliFailure("production_config_invalid")
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
    for name in ("run", "resume", "verify-run", "summarize", "import-v4-candidate"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--manifest-path", required=True)
        command.add_argument("--manifest-sha256", required=True)
        if name in {"run", "resume"}:
            command.add_argument("--round-index", required=True, type=int)
            command.add_argument("--owner-token-sha256", required=True)
            command.add_argument("--adapter-config-path")
            command.add_argument("--adapter-config-sha256")
    return parser


def parse_v5_args(argv: Sequence[str]) -> V5CliRequest:
    namespace = build_parser_v5().parse_args(tuple(argv))
    command = namespace.command
    if command not in _COMMANDS:
        raise ValueError("V5 command is invalid")
    if command in {"run", "resume"} and (
        (namespace.adapter_config_path is None) != (namespace.adapter_config_sha256 is None)
    ):
        raise V5CliFailure("invalid_request")
    return V5CliRequest(
        command=command,
        artifact_root=Path(namespace.artifact_root),
        manifest_ref=ArtifactRefV5(namespace.manifest_path, namespace.manifest_sha256),
        adapter_config_ref=(
            ArtifactRefV5(namespace.adapter_config_path, namespace.adapter_config_sha256)
            if command in {"run", "resume"}
            and namespace.adapter_config_path is not None
            and namespace.adapter_config_sha256 is not None
            else None
        ),
        round_index=getattr(namespace, "round_index", None),
        owner_token_sha256=getattr(namespace, "owner_token_sha256", None),
    )


def dispatch_v5_cli(
    argv: Sequence[str],
    *,
    services: V5CommandServices | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Parse and dispatch one command; dependency construction stays outside parsing."""
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
    "parse_v5_args",
    "register_production_round_factory_v5",
]
