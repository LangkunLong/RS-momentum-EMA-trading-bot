"""Host-only composition of the existing V5 baseline capture authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import sys

from core.pit_optimizer_v5.artifacts import _extract_artifact_refs, _strict_json_object
from core.pit_optimizer_v5.baseline import (
    BaselineCaptureInputsV5,
    BaselineHostAuthorityV5,
    authenticate_baseline_inputs_v5,
)
from core.pit_optimizer_v5.candidate_ir import SourceBundleV5, SourceFileV5, derive_policy_revision_identity_v5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    AuthenticatedArtifactV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    canonical_json_bytes_v5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.manifest import PolicyScopeDescriptorV5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.readiness import ReadinessGitAuthorityV5, capture_readiness_source_v5


@dataclass(frozen=True, slots=True)
class PreparedBaselineInputsV5:
    inputs_ref: ArtifactRefV5
    capture_argv: tuple[str, ...]
    powershell_command: str


def _file_sha256(path: str, expected: str | None) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file() or candidate.is_symlink():
        raise ValueError("host executable must be an absolute non-link file")
    resolved = candidate.resolve(strict=True)
    if str(candidate).casefold() != str(resolved).casefold():
        raise ValueError("host executable path must be canonical")
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected is not None and expected != actual:
        raise ValueError("host executable digest differs")
    return actual


def _host(*, source_root, scratch_root, git_executable, git_sha256, docker_executable, docker_sha256):
    for value in (source_root, scratch_root):
        directory = Path(value)
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise ValueError("baseline host root must be an absolute existing non-link directory")
        if str(directory).casefold() != str(directory.resolve(strict=True)).casefold():
            raise ValueError("baseline host root must be canonical")
    return BaselineHostAuthorityV5(
        source_root,
        scratch_root,
        git_executable,
        _file_sha256(git_executable, git_sha256),
        docker_executable,
        _file_sha256(docker_executable, docker_sha256),
    )


def _command(repository, host, created):
    argv = (
        sys.executable,
        "-B",
        "-m",
        "core.pit_optimizer_v5.cli",
        "capture-baseline",
        "--artifact-root",
        str(repository.root.resolve(strict=True)),
        "--capture-inputs-path",
        created.relative_path,
        "--capture-inputs-sha256",
        created.sha256,
        "--output-path",
        "evaluator/baseline-authority.json",
        "--source-root",
        host.source_root,
        "--scratch-root",
        host.scratch_root,
        "--trusted-git-executable",
        host.git_executable,
        "--trusted-git-sha256",
        host.git_sha256,
        "--docker-executable",
        host.docker_executable,
        "--docker-sha256",
        host.docker_sha256,
    )
    return argv, "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)


def _prepare(*, repository, output_path, inputs, pending, host):
    raw = canonical_json_bytes_v5(inputs)
    root_ref = ArtifactRefV5(output_path, hashlib.sha256(raw).hexdigest())
    pending[root_ref] = raw

    class PendingRepository:
        def authenticate_exact(self, reference):
            content = pending.get(reference)
            if content is not None:
                return AuthenticatedArtifactV5(
                    reference, content, _extract_artifact_refs(_strict_json_object(content, reference))
                )
            return repository.authenticate_exact(reference)

        def __getattr__(self, name):
            return getattr(repository, name)

    if authenticate_baseline_inputs_v5(repository=PendingRepository(), inputs_ref=root_ref).inputs != inputs:
        raise ValueError("prepared baseline inputs changed during authentication")
    for reference, content in pending.items():
        if reference != root_ref:
            created = repository.create_baseline_artifact(
                reference.relative_path, _strict_json_object(content, reference)
            )
            if created != reference:
                raise ValueError("prepared baseline dependency identity differs")
    created = repository.create_baseline_artifact(output_path, inputs)
    if (
        created != root_ref
        or authenticate_baseline_inputs_v5(repository=repository, inputs_ref=created).inputs != inputs
    ):
        raise ValueError("published baseline inputs failed authentication")
    argv, powershell = _command(repository, host, created)
    return PreparedBaselineInputsV5(created, argv, powershell)


def prepare_baseline_inputs_v5(
    *,
    repository,
    output_path,
    evaluator_contract_ref,
    execution_profile_ref,
    sandbox_profile_ref,
    panel_plan_ref,
    baseline_policy_revision_ref,
    source_bundle_ref,
    policy_scope_ref,
    evaluator_source_ref,
    identity_transition_ref,
    resources_ref,
    source_root,
    scratch_root,
    git_executable,
    docker_executable,
    git_sha256=None,
    docker_sha256=None,
):
    """Publish an explicitly referenced graph and a concrete capture command."""
    host = _host(
        source_root=source_root,
        scratch_root=scratch_root,
        git_executable=git_executable,
        git_sha256=git_sha256,
        docker_executable=docker_executable,
        docker_sha256=docker_sha256,
    )
    inputs = BaselineCaptureInputsV5(
        5,
        evaluator_contract_ref,
        execution_profile_ref,
        sandbox_profile_ref,
        panel_plan_ref,
        baseline_policy_revision_ref,
        source_bundle_ref,
        policy_scope_ref,
        evaluator_source_ref,
        identity_transition_ref,
        resources_ref,
    )
    return _prepare(repository=repository, output_path=output_path, inputs=inputs, pending={}, host=host)


def _source_bundle(root, snapshot):
    files, expected = [], dict(snapshot.editable_source_sha256)
    for relative in EDITABLE_POLICY_PATHS_V5:
        raw = root.joinpath(*PurePosixPath(relative).parts).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected[relative]:
            raw = raw.replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected[relative]:
                raise ValueError("policy source bytes differ from clean source snapshot")
        files.append(SourceFileV5(relative, raw.decode("utf-8", "strict")))
    return SourceBundleV5(tuple(files))


def prepare_composed_baseline_inputs_v5(
    *,
    repository,
    output_path,
    execution_profile_ref,
    sandbox_profile_ref,
    panel_plan_ref,
    evaluator_source_ref,
    identity_transition_ref,
    resources_ref,
    immutable_constraints_ref,
    source_bundle_output_path,
    policy_revision_output_path,
    policy_scope_output_path,
    evaluator_contract_output_path,
    source_commit,
    source_root,
    scratch_root,
    git_executable,
    docker_executable,
    git_sha256=None,
    docker_sha256=None,
):
    """Derive source and evaluator descriptors from pinned real inputs."""
    host = _host(
        source_root=source_root,
        scratch_root=scratch_root,
        git_executable=git_executable,
        git_sha256=git_sha256,
        docker_executable=docker_executable,
        docker_sha256=docker_sha256,
    )
    authority = ReadinessGitAuthorityV5(host.git_executable, host.git_sha256)
    with capture_readiness_source_v5(
        source_root=Path(source_root), expected_source_commit=source_commit, git_authority=authority
    ) as inspection:
        bundle = _source_bundle(Path(source_root), inspection.snapshot)
        inspection.revalidate()
        sandbox = repository.load_typed_artifact(sandbox_profile_ref, value_type=SandboxProfileV5)
        panel = repository.load_typed_artifact(panel_plan_ref, value_type=CampaignPanelPlanV5)
        repository.authenticate_exact(execution_profile_ref)
        repository.authenticate_exact(evaluator_source_ref)
        repository.authenticate_raw_artifact(identity_transition_ref)
        repository.authenticate_exact(resources_ref)
        constraints = repository.authenticate_exact(immutable_constraints_ref)
        if constraints.child_references:
            raise ValueError("immutable constraints configuration must be a leaf artifact")
        revision = derive_policy_revision_identity_v5(
            source_bundle=bundle,
            trusted_policy_runtime_sha256=sandbox.runtime_source_sha256,
            immutable_constraints_sha256=immutable_constraints_ref.sha256,
        )
        source_ref = ArtifactRefV5(source_bundle_output_path, bundle.sha256)
        revision_ref = ArtifactRefV5(policy_revision_output_path, revision.sha256)
        scope = PolicyScopeDescriptorV5(
            5, source_commit, inspection.snapshot.editable_source_sha256, revision_ref, False
        )
        scope_ref = ArtifactRefV5(policy_scope_output_path, hashlib.sha256(canonical_json_bytes_v5(scope)).hexdigest())
        evaluator = EvaluatorContractV5(
            5,
            execution_profile_ref.sha256,
            sandbox_profile_ref.sha256,
            evaluator_source_ref.sha256,
            panel.pit_bundle_ref.sha256,
            panel.prices_provenance_ref.sha256,
            identity_transition_ref.sha256,
            source_ref.sha256,
            revision_ref.sha256,
            initial_friction_grid_v5(),
            "base",
        )
        evaluator_ref = ArtifactRefV5(evaluator_contract_output_path, evaluator.sha256)
        pending = {
            source_ref: canonical_json_bytes_v5(bundle),
            revision_ref: canonical_json_bytes_v5(revision),
            scope_ref: canonical_json_bytes_v5(scope),
            evaluator_ref: canonical_json_bytes_v5(evaluator),
        }
        inputs = BaselineCaptureInputsV5(
            5,
            evaluator_ref,
            execution_profile_ref,
            sandbox_profile_ref,
            panel_plan_ref,
            revision_ref,
            source_ref,
            scope_ref,
            evaluator_source_ref,
            identity_transition_ref,
            resources_ref,
        )
        result = _prepare(repository=repository, output_path=output_path, inputs=inputs, pending=pending, host=host)
        inspection.revalidate()
        return result


__all__ = ["PreparedBaselineInputsV5", "prepare_baseline_inputs_v5", "prepare_composed_baseline_inputs_v5"]
