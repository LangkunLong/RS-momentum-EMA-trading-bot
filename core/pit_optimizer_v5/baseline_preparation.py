"""Host-only composition of the existing V5 baseline capture authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from core.pit_optimizer_v5.artifacts import _extract_artifact_refs, _strict_json_object
from core.pit_optimizer_v5.baseline import BaselineCaptureInputsV5, authenticate_baseline_inputs_v5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, AuthenticatedArtifactV5, canonical_json_bytes_v5


@dataclass(frozen=True, slots=True)
class PreparedBaselineInputsV5:
    """Content-free result of baseline input preparation."""

    inputs_ref: ArtifactRefV5
    capture_argv: tuple[str, ...]


def prepare_baseline_inputs_v5(
    *,
    repository,
    output_path: str,
    evaluator_contract_ref: ArtifactRefV5,
    execution_profile_ref: ArtifactRefV5,
    sandbox_profile_ref: ArtifactRefV5,
    panel_plan_ref: ArtifactRefV5,
    baseline_policy_revision_ref: ArtifactRefV5,
    source_bundle_ref: ArtifactRefV5,
    policy_scope_ref: ArtifactRefV5,
    evaluator_source_ref: ArtifactRefV5,
    identity_transition_ref: ArtifactRefV5,
    resources_ref: ArtifactRefV5,
) -> PreparedBaselineInputsV5:
    """Authenticate and create-only publish one complete baseline input graph.

    The pending root is authenticated in memory before publication. Every child
    is consequently read only at its explicitly supplied path and digest.
    """

    inputs = BaselineCaptureInputsV5(
        schema_version=5,
        evaluator_contract_ref=evaluator_contract_ref,
        execution_profile_ref=execution_profile_ref,
        sandbox_profile_ref=sandbox_profile_ref,
        panel_plan_ref=panel_plan_ref,
        baseline_policy_revision_ref=baseline_policy_revision_ref,
        source_bundle_ref=source_bundle_ref,
        policy_scope_ref=policy_scope_ref,
        evaluator_source_ref=evaluator_source_ref,
        identity_transition_ref=identity_transition_ref,
        resources_ref=resources_ref,
    )
    raw = canonical_json_bytes_v5(inputs)
    pending_ref = ArtifactRefV5(output_path, hashlib.sha256(raw).hexdigest())

    class PendingInputsRepository:
        def authenticate_exact(self, reference):
            if reference == pending_ref:
                return AuthenticatedArtifactV5(
                    reference,
                    raw,
                    _extract_artifact_refs(_strict_json_object(raw, reference)),
                )
            return repository.authenticate_exact(reference)

        def __getattr__(self, name):
            return getattr(repository, name)

    authenticated = authenticate_baseline_inputs_v5(repository=PendingInputsRepository(), inputs_ref=pending_ref)
    if authenticated.inputs != inputs:
        raise ValueError("prepared baseline inputs changed during authentication")
    created = repository.create_baseline_artifact(output_path, inputs)
    if created != pending_ref:
        raise ValueError("prepared baseline input identity differs")
    if authenticate_baseline_inputs_v5(repository=repository, inputs_ref=created).inputs != inputs:
        raise ValueError("published baseline inputs failed authentication")
    capture_argv = (
        "python",
        "-B",
        "-m",
        "core.pit_optimizer_v5.cli",
        "capture-baseline",
        "--artifact-root",
        "<ABSOLUTE_ARTIFACT_ROOT>",
        "--capture-inputs-path",
        created.relative_path,
        "--capture-inputs-sha256",
        created.sha256,
        "--output-path",
        "evaluator/baseline-authority.json",
        "--source-root",
        "<ABSOLUTE_CLEAN_SOURCE_ROOT>",
        "--scratch-root",
        "<ABSOLUTE_SCRATCH_ROOT>",
        "--trusted-git-executable",
        "<ABSOLUTE_GIT_EXECUTABLE>",
        "--trusted-git-sha256",
        "<GIT_SHA256>",
        "--docker-executable",
        "<ABSOLUTE_DOCKER_EXECUTABLE>",
        "--docker-sha256",
        "<DOCKER_SHA256>",
    )
    return PreparedBaselineInputsV5(created, capture_argv)


__all__ = ["PreparedBaselineInputsV5", "prepare_baseline_inputs_v5"]
