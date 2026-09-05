"""Thin local command boundary for the PIT optimizer V5 runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Callable, Literal, Protocol, Sequence, runtime_checkable

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
    LedgerRoleAuthorizationLifecycleV5,
)
from core.pit_optimizer_v5.runtime import (
    FeedbackRoundDependenciesV5,
    FeedbackRoundInputV5,
    FeedbackRoundResultV5,
    run_feedback_round_v5,
)
from core.pit_optimizer_v5.sandbox import DockerCandidateRuntimeV5
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5
from core.pit_optimizer_v5.summary import OptimizerSummaryV5, summarize_repository_v5, unavailable_summary_v5


V5CommandName = Literal["run", "resume", "verify-run", "summarize", "import-v4-candidate"]
_COMMANDS = frozenset({"run", "resume", "verify-run", "summarize", "import-v4-candidate"})
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FAILURE_REASONS = frozenset(
    {
        "artifact_graph_invalid",
        "artifact_root_unavailable",
        "campaign_authority_invalid",
        "import_campaign_authority_unavailable",
        "invalid_request",
        "production_authority_mismatch",
        "production_composition_unavailable",
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
        elif self.round_index is not None or self.owner_token_sha256 is not None:
            raise ValueError("read-only V5 commands cannot carry execution authority")


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
        if (
            type(runner.completion_provider) is not GatewayCompletionProviderV5
            or not isinstance(lifecycle, LedgerRoleAuthorizationLifecycleV5)
            or lifecycle.campaign_manifest_sha256 != authorities.manifest.sha256
            or type(self.dependencies.candidates) is not DockerCandidateRuntimeV5
        ):
            raise V5CliFailure("production_dependency_invalid")


@runtime_checkable
class ProductionRoundFactoryV5(Protocol):
    """Campaign adapter factory; never selected from user-controlled CLI text."""

    def compose_round(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
        round_index: int,
        owner_token_sha256: str,
    ) -> ProductionRoundCompositionV5: ...

    def verify_run(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        authorities: CampaignAuthoritiesV5,
    ) -> None: ...


@runtime_checkable
class V5CommandServices(Protocol):
    def execute(self, request: V5CliRequest) -> OptimizerSummaryV5: ...


class ProductionV5CommandServices:
    """Authenticate local authorities and invoke only approved production shapes."""

    def __init__(self, *, factory: ProductionRoundFactoryV5 | None = None) -> None:
        if factory is not None and not isinstance(factory, ProductionRoundFactoryV5):
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

    def execute(self, request: V5CliRequest) -> OptimizerSummaryV5:
        if type(request) is not V5CliRequest:
            raise V5CliFailure("invalid_request")
        if request.command == "import-v4-candidate":
            raise V5CliFailure("import_campaign_authority_unavailable")
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
        if self._factory is None:
            raise V5CliFailure("production_composition_unavailable")
        assert request.round_index is not None and request.owner_token_sha256 is not None
        existing_events = repository.load_round_events(
            campaign_id=authorities.manifest.campaign_id,
            round_index=request.round_index,
        )
        if request.command == "run" and existing_events:
            raise V5CliFailure("run_not_fresh")
        if request.command == "resume" and not existing_events:
            raise V5CliFailure("resume_state_missing")
        composition = self._factory.compose_round(
            repository=repository,
            authorities=authorities,
            round_index=request.round_index,
            owner_token_sha256=request.owner_token_sha256,
        )
        if type(composition) is not ProductionRoundCompositionV5:
            raise V5CliFailure("production_dependency_invalid")
        composition.validate_for(
            repository=repository,
            authorities=authorities,
            round_index=request.round_index,
            owner_token_sha256=request.owner_token_sha256,
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


def build_parser_v5() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pit-optimizer-v5", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "verify-run", "summarize", "import-v4-candidate"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--artifact-root", required=True)
        command.add_argument("--manifest-path", required=True)
        command.add_argument("--manifest-sha256", required=True)
        if name in {"run", "resume"}:
            command.add_argument("--round-index", required=True, type=int)
            command.add_argument("--owner-token-sha256", required=True)
    return parser


def parse_v5_args(argv: Sequence[str]) -> V5CliRequest:
    namespace = build_parser_v5().parse_args(tuple(argv))
    command = namespace.command
    if command not in _COMMANDS:
        raise ValueError("V5 command is invalid")
    return V5CliRequest(
        command=command,
        artifact_root=Path(namespace.artifact_root),
        manifest_ref=ArtifactRefV5(namespace.manifest_path, namespace.manifest_sha256),
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

    request = parse_v5_args(argv)
    selected = ProductionV5CommandServices() if services is None else services
    if not isinstance(selected, V5CommandServices):
        raise ValueError("V5 CLI services are invalid")
    try:
        summary = selected.execute(request)
        exit_code = 0
    except V5CliFailure as exc:
        summary = exc.summary or unavailable_summary_v5(request.command, exc.reason)
        exit_code = exc.exit_code
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
    "ProductionRoundCompositionV5",
    "ProductionRoundFactoryV5",
    "ProductionV5CommandServices",
    "V5CliFailure",
    "V5CliRequest",
    "V5CommandServices",
    "build_parser_v5",
    "dispatch_v5_cli",
    "parse_v5_args",
]
