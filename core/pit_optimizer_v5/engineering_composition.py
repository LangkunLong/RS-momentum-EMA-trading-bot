"""Bounded, provider-free composition for schema-V3 engineering research.

This module only constructs the existing isolated Docker candidate path. It does
not launch a container or turn engineering evidence into production authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from core.pit_optimizer_v5.cli import (
    CampaignAuthoritiesV5,
    _compose_round_from_paths_v5,
)
from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.baseline import verify_baseline_v5
from core.pit_optimizer_v5.controller_roles import FileBackedControllerRoleInvokerV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5
from core.pit_optimizer_v5.manifest import authenticate_campaign_manifest_v5
from core.pit_optimizer_v5.production_runtime import LocalRoleRequestFactoryV5
from core.pit_optimizer_v5.runtime import FeedbackRoundDependenciesV5, FeedbackRoundInputV5


@dataclass(frozen=True, slots=True)
class EngineeringRoundCompositionV5:
    inputs: FeedbackRoundInputV5
    dependencies: FeedbackRoundDependenciesV5
    feedback_experiment_id: str | None


def compose_engineering_round_v5(
    *,
    repository: LocalArtifactRepositoryV5,
    manifest_ref: ArtifactRefV5,
    baseline_capture_authority_ref: ArtifactRefV5,
    authorities: CampaignAuthoritiesV5,
    round_index: int,
    owner_token_sha256: str,
    response_directory: str,
    source_root: str,
    workspace_root: str,
    data_root: str,
    output_root: str,
    control_root: str,
    git_executable: str,
    docker_executable: str,
    feedback_experiment_id: str | None = None,
) -> EngineeringRoundCompositionV5:
    """Prepare one ordinary feedback round using the isolated V5 worker graph.

    Round two must identify the persisted round-one candidate experiment even
    when archive selection kept the baseline as parent. The request factory
    authenticates that experiment and its measured campaign before issuing
    any investigator evidence.
    """

    if (
        type(repository) is not LocalArtifactRepositoryV5
        or type(manifest_ref) is not ArtifactRefV5
        or type(baseline_capture_authority_ref) is not ArtifactRefV5
        or type(authorities) is not CampaignAuthoritiesV5
    ):
        raise ValueError("engineering composition authority is invalid")
    authenticated = authenticate_campaign_manifest_v5(
        repository=repository, manifest_ref=manifest_ref
    )
    if authorities != CampaignAuthoritiesV5(
        authenticated.manifest,
        authenticated.panel_plan,
        authenticated.evaluator_contract,
        authenticated.baseline_authority,
        authenticated.sandbox_profile,
    ):
        raise ValueError("engineering composition differs from authenticated manifest")
    baseline_receipt = verify_baseline_v5(
        repository=repository,
        authority_ref=baseline_capture_authority_ref,
        pit_data_scope="engineering_v3",
    )
    if baseline_receipt["parent_authority_ref"] != authenticated.manifest.baseline_authority_ref:
        raise ValueError("engineering manifest baseline differs from measured capture")
    manifest = authorities.manifest
    search = manifest.search
    if (
        manifest.pit_data_scope != "engineering_v3"
        or authorities.baseline.pit_data_scope != "engineering_v3"
        or manifest.semantic_mode != "required"
        or manifest.provider is not None
        or search.max_feedback_rounds != 2
        or search.hypotheses_per_investigator != 1
        or search.max_variants_per_template != 1
        or search.max_discovery_survivors_per_template != 1
        or search.allow_full_source_escape
        or round_index not in (1, 2)
        or (round_index == 1 and feedback_experiment_id is not None)
        or (round_index == 2 and feedback_experiment_id is None)
    ):
        raise ValueError("engineering composition requires the finite two-round research authority")
    response_path = Path(response_directory)
    if not response_path.is_absolute() or not response_path.is_dir() or response_path.is_symlink():
        raise ValueError("engineering controller response directory is invalid")
    invoker = FileBackedControllerRoleInvokerV5(
        repository=repository,
        manifest=manifest,
        response_directory=response_path,
    )
    composed = _compose_round_from_paths_v5(
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
        export_mode="policy_only",
    )
    requests = LocalRoleRequestFactoryV5(
        repository=repository,
        manifest=manifest,
        feedback_experiment_id=feedback_experiment_id,
    )
    return EngineeringRoundCompositionV5(
        composed.inputs,
        replace(composed.dependencies, requests=requests),
        feedback_experiment_id,
    )


__all__ = ["EngineeringRoundCompositionV5", "compose_engineering_round_v5"]
