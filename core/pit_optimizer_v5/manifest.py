"""Authenticated campaign-manifest composition for PIT optimizer V5.

This module deliberately composes the canonical capability contracts from
``contracts.py``.  It does not provide alternate search, provider, resource,
or campaign schemas.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, Protocol, TypeVar

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_v5.artifacts import ArtifactMissingV5
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from core.pit_optimizer_v5.contracts import (
    ARTIFACT_ROOT_V5,
    AnnualizedReturnTargetV5,
    ArtifactGraphVerificationV5,
    ArtifactRefV5,
    AuthenticatedArtifactV5,
    AuthenticatedRawArtifactV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EvaluatorContractV5,
    ProviderCapabilitiesV5,
    ResourceCapabilitiesV5,
    SandboxProfileV5,
    SearchCapabilitiesV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    validate_campaign_manifest_bindings_v5,
    validate_episode_plan_panel_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.search import (
    BaselineParentAuthorityV5,
    baseline_parent_candidate_v5,
)
from core.pit_optimizer_evaluation import EvaluationPanelSpec


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_T = TypeVar("_T")


class ManifestRepositoryV5(Protocol):
    """Closed storage boundary needed by manifest composition."""

    def authenticate(self, reference: ArtifactRefV5) -> AuthenticatedArtifactV5: ...

    def authenticate_raw_artifact(self, reference: ArtifactRefV5) -> AuthenticatedRawArtifactV5: ...

    def load_evaluation_panel_spec(self, reference: ArtifactRefV5) -> EvaluationPanelSpec: ...

    def load_typed_artifact(self, reference: ArtifactRefV5, *, value_type: type[_T]) -> _T: ...

    def create_typed_artifact(self, relative_path: str, value: object) -> ArtifactRefV5: ...

    def verify_graph(self, manifest_ref: ArtifactRefV5) -> ArtifactGraphVerificationV5: ...


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _source_commit(value: object) -> str:
    if type(value) is not str or _SOURCE_COMMIT_RE.fullmatch(value) is None:
        raise ValueError("source commit must be a lowercase Git SHA-1")
    return value


def _source_digests(
    value: object,
    *,
    label: str,
) -> tuple[tuple[str, str], ...]:
    if (
        type(value) is not tuple
        or tuple(item[0] for item in value if type(item) is tuple and len(item) == 2) != EDITABLE_POLICY_PATHS_V5
        or len(value) != len(EDITABLE_POLICY_PATHS_V5)
    ):
        raise ValueError(f"{label} must identify the exact ordered editable policy scope")
    for item, expected_path in zip(value, EDITABLE_POLICY_PATHS_V5, strict=True):
        if type(item) is not tuple or len(item) != 2 or item[0] != expected_path:
            raise ValueError(f"{label} must identify the exact ordered editable policy scope")
        _digest(item[1], f"{label} digest for {expected_path}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PolicySourceSnapshotV5:
    """Clean tracked policy bytes captured from one immutable source commit."""

    source_commit: str
    editable_source_sha256: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _source_commit(self.source_commit)
        _source_digests(self.editable_source_sha256, label="source snapshot")

    @classmethod
    def from_tracked_bytes(
        cls,
        *,
        source_commit: str,
        source_by_path: Mapping[str, bytes],
    ) -> "PolicySourceSnapshotV5":
        if not isinstance(source_by_path, Mapping) or tuple(source_by_path) != EDITABLE_POLICY_PATHS_V5:
            raise ValueError("tracked source bytes must contain the exact ordered editable policy scope")
        digests: list[tuple[str, str]] = []
        for path in EDITABLE_POLICY_PATHS_V5:
            content = source_by_path[path]
            if type(content) is not bytes:
                raise ValueError("tracked policy source must be bytes")
            digests.append((path, hashlib.sha256(content).hexdigest()))
        return cls(source_commit=source_commit, editable_source_sha256=tuple(digests))


@dataclass(frozen=True, slots=True)
class PolicyScopeDescriptorV5:
    """Immutable source-scope authority for one discovery campaign."""

    schema_version: Literal[5]
    source_commit: str
    editable_source_sha256: tuple[tuple[str, str], ...]
    baseline_policy_revision_ref: ArtifactRefV5
    full_source_escape_allowed: bool

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("policy scope descriptor schema must be V5")
        _source_commit(self.source_commit)
        _source_digests(self.editable_source_sha256, label="policy scope")
        if type(self.baseline_policy_revision_ref) is not ArtifactRefV5:
            raise ValueError("policy scope baseline revision reference is invalid")
        if type(self.full_source_escape_allowed) is not bool:
            raise ValueError("policy scope full-source permission must be boolean")


@dataclass(frozen=True, slots=True)
class AuthenticatedCampaignManifestV5:
    """Fully authenticated and cross-bound campaign authorities."""

    manifest_ref: ArtifactRefV5
    graph: ArtifactGraphVerificationV5
    manifest: CampaignManifestV5
    panel_plan: CampaignPanelPlanV5
    execution_profile: ExecutionProfileV5
    evaluator_contract: EvaluatorContractV5
    baseline_authority: BaselineParentAuthorityV5
    baseline_policy_revision: PolicyRevisionIdentityV5
    policy_scope: PolicyScopeDescriptorV5
    sandbox_profile: SandboxProfileV5

    def __post_init__(self) -> None:
        if (
            type(self.manifest_ref) is not ArtifactRefV5
            or type(self.graph) is not ArtifactGraphVerificationV5
            or not self.graph.verified
            or self.graph.manifest_ref != self.manifest_ref
            or type(self.manifest) is not CampaignManifestV5
            or type(self.panel_plan) is not CampaignPanelPlanV5
            or type(self.execution_profile) is not ExecutionProfileV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.baseline_authority) is not BaselineParentAuthorityV5
            or type(self.baseline_policy_revision) is not PolicyRevisionIdentityV5
            or type(self.policy_scope) is not PolicyScopeDescriptorV5
            or type(self.sandbox_profile) is not SandboxProfileV5
        ):
            raise ValueError("authenticated campaign manifest authorities are invalid")


@dataclass(frozen=True, slots=True)
class RenderedDiscoveryAuthorizationV5:
    """Content-free authorization projection for later adapter composition."""

    schema_version: Literal[5]
    manifest_ref: ArtifactRefV5
    campaign_id: str
    target: AnnualizedReturnTargetV5
    search: SearchCapabilitiesV5
    provider: ProviderCapabilitiesV5 | None
    resources: ResourceCapabilitiesV5
    policy_scope_ref: ArtifactRefV5
    source_commit: str
    apply: Literal[False]
    qualification_allowed: Literal[False]
    full_replay_allowed: Literal[False]
    adapter_composition: Literal["pending"]
    executable: Literal[False]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 5
            or type(self.manifest_ref) is not ArtifactRefV5
            or type(self.campaign_id) is not str
            or not self.campaign_id
            or type(self.target) is not AnnualizedReturnTargetV5
            or type(self.search) is not SearchCapabilitiesV5
            or (self.provider is not None and type(self.provider) is not ProviderCapabilitiesV5)
            or type(self.resources) is not ResourceCapabilitiesV5
            or type(self.policy_scope_ref) is not ArtifactRefV5
        ):
            raise ValueError("rendered discovery authorization is invalid")
        _source_commit(self.source_commit)
        if (
            self.apply is not False
            or self.qualification_allowed is not False
            or self.full_replay_allowed is not False
            or self.adapter_composition != "pending"
            or self.executable is not False
        ):
            raise ValueError("rendered discovery authorization exceeds Task 2 authority")


class ManifestAuthenticationFailureV5(ValueError):
    """The complete campaign graph or its cross-artifact bindings failed."""


@dataclass(frozen=True, slots=True)
class _AuthenticatedBuildDependencyGraphV5:
    baseline_policy_revision_ref: ArtifactRefV5
    baseline_policy_revision_present: bool
    authenticated: tuple[AuthenticatedArtifactV5 | AuthenticatedRawArtifactV5, ...]

    def __post_init__(self) -> None:
        if type(self.baseline_policy_revision_ref) is not ArtifactRefV5:
            raise ValueError("build graph baseline policy reference is invalid")
        if type(self.baseline_policy_revision_present) is not bool:
            raise ValueError("build graph baseline policy presence is invalid")
        if type(self.authenticated) is not tuple or any(
            type(item) not in {AuthenticatedArtifactV5, AuthenticatedRawArtifactV5} for item in self.authenticated
        ):
            raise ValueError("build graph authenticated nodes are invalid")
        references = tuple(item.reference for item in self.authenticated)
        if len(set(references)) != len(references):
            raise ValueError("build graph authenticated nodes must be unique")


def _sanitized_git_environment_v5() -> dict[str, str]:
    """Remove every ambient Git override and disable external config selection."""

    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _same_resolved_path_v5(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve(strict=True))) == os.path.normcase(str(second.resolve(strict=True)))


def capture_clean_policy_snapshot_v5(
    *,
    source_root: Path,
    git_executable: Path,
    expected_source_commit: str,
) -> PolicySourceSnapshotV5:
    """Capture exact tracked bytes from a clean immutable Git commit.

    The executable and source root are explicit inputs; no PATH, cwd, or
    environment-derived repository default is consulted.
    """

    commit = _source_commit(expected_source_commit)
    root = Path(source_root)
    executable = Path(git_executable)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("source root must be an absolute non-link directory")
    if not executable.is_absolute() or not executable.is_file() or executable.is_symlink():
        raise ValueError("Git executable must be an absolute non-link file")
    resolved_root = root.resolve(strict=True)
    if os.path.normcase(str(root)) != os.path.normcase(str(resolved_root)):
        raise ValueError("source root must be its canonical resolved path")
    git_environment = _sanitized_git_environment_v5()

    def invoke(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                (str(executable), "-C", str(root), *arguments),
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                shell=False,
                cwd=resolved_root,
                env=git_environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("clean source authority could not be authenticated") from exc

    try:
        top_level_text = (
            invoke("rev-parse", "--path-format=absolute", "--show-toplevel").stdout.decode("utf-8", "strict").strip()
        )
        top_level = Path(top_level_text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Git worktree root could not be authenticated") from exc
    if not top_level_text or not top_level.is_absolute() or not _same_resolved_path_v5(top_level, resolved_root):
        raise ValueError("Git resolved worktree differs from the supplied source root")
    if invoke("rev-parse", "--is-inside-work-tree").stdout.strip() != b"true":
        raise ValueError("source root is not a Git worktree")
    head = invoke("rev-parse", "--verify", "HEAD").stdout.decode("ascii", "strict").strip()
    if head != commit:
        raise ValueError("source HEAD differs from the declared immutable commit")
    if invoke("status", "--porcelain=v1", "--untracked-files=all").stdout:
        raise ValueError("source worktree must be clean before manifest composition")
    source_by_path = {
        path: invoke("show", "--no-ext-diff", "--no-textconv", f"{commit}:{path}").stdout
        for path in EDITABLE_POLICY_PATHS_V5
    }
    return PolicySourceSnapshotV5.from_tracked_bytes(
        source_commit=commit,
        source_by_path=source_by_path,
    )


def _authenticated_object_v5(artifact: AuthenticatedArtifactV5, label: str) -> dict[str, object]:
    try:
        value = json.loads(artifact.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not authenticated canonical JSON") from exc
    if type(value) is not dict or canonical_json_bytes_v5(value) != artifact.content:
        raise ValueError(f"{label} is not authenticated canonical JSON")
    return value


def _primitive_artifact_ref_v5(value: object, label: str) -> ArtifactRefV5:
    if type(value) is not dict or set(value) != {"relative_path", "sha256"}:
        raise ValueError(f"{label} reference is invalid")
    try:
        return ArtifactRefV5(value["relative_path"], value["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} reference is invalid") from exc


def _episode_panel_ref_v5(value: object, label: str) -> ArtifactRefV5:
    if type(value) is not dict or "panel_ref" not in value:
        raise ValueError(f"{label} panel reference is invalid")
    return _primitive_artifact_ref_v5(value["panel_ref"], f"{label} panel")


def _authenticate_build_dependency_graph_v5(
    *,
    repository: ManifestRepositoryV5,
    execution_profile_ref: ArtifactRefV5,
    evaluator_contract_ref: ArtifactRefV5,
    baseline_authority_ref: ArtifactRefV5,
    panel_plan_ref: ArtifactRefV5,
    sandbox_profile_ref: ArtifactRefV5,
) -> _AuthenticatedBuildDependencyGraphV5:
    """Authenticate every existing build edge before typed construction."""

    authenticated_by_ref: dict[
        ArtifactRefV5,
        AuthenticatedArtifactV5 | AuthenticatedRawArtifactV5,
    ] = {}

    def authenticate_json(reference: ArtifactRefV5) -> AuthenticatedArtifactV5:
        item = repository.authenticate(reference)
        if type(item) is not AuthenticatedArtifactV5 or item.reference != reference:
            raise ValueError("build dependency authentication returned an invalid JSON node")
        authenticated_by_ref[reference] = item
        return item

    def authenticate_raw(reference: ArtifactRefV5) -> None:
        item = repository.authenticate_raw_artifact(reference)
        if type(item) is not AuthenticatedRawArtifactV5 or item.reference != reference:
            raise ValueError("build dependency authentication returned an invalid raw node")
        authenticated_by_ref[reference] = item

    execution_item = authenticate_json(execution_profile_ref)
    evaluator_item = authenticate_json(evaluator_contract_ref)
    baseline_item = authenticate_json(baseline_authority_ref)
    panel_plan_item = authenticate_json(panel_plan_ref)
    sandbox_item = authenticate_json(sandbox_profile_ref)
    if any(item.child_references for item in (execution_item, evaluator_item, sandbox_item)):
        raise ValueError("leaf build authority contains unexpected artifact references")

    panel_value = _authenticated_object_v5(panel_plan_item, "campaign panel plan")
    try:
        discovery = panel_value["discovery"]
        if type(discovery) is not list:
            raise TypeError
        panel_edges = (
            _primitive_artifact_ref_v5(panel_value["pit_bundle_ref"], "PIT bundle"),
            _primitive_artifact_ref_v5(
                panel_value["prices_provenance_ref"],
                "prices provenance",
            ),
            _episode_panel_ref_v5(panel_value["mechanics"], "mechanics"),
            _episode_panel_ref_v5(panel_value["quick"], "quick"),
            *(
                _episode_panel_ref_v5(item, f"discovery episode {index}")
                for index, item in enumerate(discovery, start=1)
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("campaign panel plan dependency graph is invalid") from exc
    if set(panel_edges) != set(panel_plan_item.child_references):
        raise ValueError("campaign panel plan dependency graph differs from its references")
    for reference in panel_edges:
        authenticate_raw(reference)

    baseline_value = _authenticated_object_v5(baseline_item, "baseline authority")
    try:
        baseline_policy_ref = _primitive_artifact_ref_v5(
            baseline_value["policy_revision_ref"],
            "baseline policy revision",
        )
        baseline_source_ref = _primitive_artifact_ref_v5(
            baseline_value["source_bundle_ref"],
            "baseline source bundle",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("baseline authority dependency graph is invalid") from exc
    if set(baseline_item.child_references) != {baseline_policy_ref, baseline_source_ref}:
        raise ValueError("baseline authority dependency graph differs from its references")
    if baseline_policy_ref == baseline_source_ref:
        raise ValueError("baseline policy and source dependencies must be distinct")
    source_item = authenticate_json(baseline_source_ref)
    if source_item.child_references:
        raise ValueError("baseline source bundle contains unexpected artifact references")
    baseline_policy_present = True
    try:
        policy_item = authenticate_json(baseline_policy_ref)
    except ArtifactMissingV5:
        baseline_policy_present = False
    else:
        if policy_item.child_references:
            raise ValueError("baseline policy revision contains unexpected artifact references")
    return _AuthenticatedBuildDependencyGraphV5(
        baseline_policy_revision_ref=baseline_policy_ref,
        baseline_policy_revision_present=baseline_policy_present,
        authenticated=tuple(authenticated_by_ref.values()),
    )


def _authenticate_build_dependencies_v5(
    *,
    repository: ManifestRepositoryV5,
    execution_profile_ref: ArtifactRefV5,
    evaluator_contract_ref: ArtifactRefV5,
    baseline_authority_ref: ArtifactRefV5,
    panel_plan_ref: ArtifactRefV5,
    sandbox_profile_ref: ArtifactRefV5,
) -> tuple[
    ExecutionProfileV5,
    EvaluatorContractV5,
    BaselineParentAuthorityV5,
    CampaignPanelPlanV5,
    SandboxProfileV5,
]:
    graph = _authenticate_build_dependency_graph_v5(
        repository=repository,
        execution_profile_ref=execution_profile_ref,
        evaluator_contract_ref=evaluator_contract_ref,
        baseline_authority_ref=baseline_authority_ref,
        panel_plan_ref=panel_plan_ref,
        sandbox_profile_ref=sandbox_profile_ref,
    )
    # Every existing edge is authenticated before the first typed load.  The
    # exact baseline policy edge may be absent only because this command owns
    # its create-only descriptor write.
    execution = repository.load_typed_artifact(execution_profile_ref, value_type=ExecutionProfileV5)
    evaluator = repository.load_typed_artifact(evaluator_contract_ref, value_type=EvaluatorContractV5)
    baseline = repository.load_typed_artifact(baseline_authority_ref, value_type=BaselineParentAuthorityV5)
    panel_plan = repository.load_typed_artifact(panel_plan_ref, value_type=CampaignPanelPlanV5)
    sandbox = repository.load_typed_artifact(sandbox_profile_ref, value_type=SandboxProfileV5)

    for episode in (panel_plan.mechanics, panel_plan.quick, *panel_plan.discovery):
        panel = repository.load_evaluation_panel_spec(episode.panel_ref)
        validate_episode_plan_panel_v5(episode, panel)
        if episode.purpose != panel.purpose:
            raise ValueError("panel purpose differs from its authenticated V5 owner")

    source_bundle = repository.load_typed_artifact(baseline.source_bundle_ref, value_type=SourceBundleV5)
    if source_bundle != baseline.source_bundle:
        raise ValueError("baseline source artifact differs from its authority")
    if baseline.policy_revision_ref != graph.baseline_policy_revision_ref:
        raise ValueError("baseline policy reference differs from its preauthenticated graph")
    if graph.baseline_policy_revision_present:
        existing_policy = repository.load_typed_artifact(
            graph.baseline_policy_revision_ref,
            value_type=PolicyRevisionIdentityV5,
        )
        if existing_policy != baseline.policy_revision:
            raise ValueError("existing baseline policy descriptor differs from its authority")
    return execution, evaluator, baseline, panel_plan, sandbox


def _validate_precomposition_bindings_v5(
    *,
    target: AnnualizedReturnTargetV5,
    source_snapshot: PolicySourceSnapshotV5,
    execution_profile_ref: ArtifactRefV5,
    execution_profile: ExecutionProfileV5,
    evaluator_contract_ref: ArtifactRefV5,
    evaluator_contract: EvaluatorContractV5,
    baseline_authority_ref: ArtifactRefV5,
    baseline_authority: BaselineParentAuthorityV5,
    panel_plan_ref: ArtifactRefV5,
    panel_plan: CampaignPanelPlanV5,
    sandbox_profile_ref: ArtifactRefV5,
    sandbox_profile: SandboxProfileV5,
    resources: ResourceCapabilitiesV5,
) -> None:
    if type(target) is not AnnualizedReturnTargetV5 or type(source_snapshot) is not PolicySourceSnapshotV5:
        raise ValueError("manifest target or source snapshot is invalid")
    if (
        execution_profile_ref.sha256 != execution_profile.sha256
        or evaluator_contract_ref.sha256 != evaluator_contract.sha256
        or baseline_authority_ref.sha256 != canonical_sha256_v5(baseline_authority)
        or panel_plan_ref.sha256 != panel_plan.sha256
        or sandbox_profile_ref.sha256 != sandbox_profile.sha256
        or target.sha256 != panel_plan.target_sha256
        or execution_profile.sha256 != evaluator_contract.execution_profile_sha256
        or sandbox_profile.sha256 != evaluator_contract.sandbox_profile_sha256
        or sandbox_profile.runtime_source_sha256 != evaluator_contract.evaluator_source_sha256
        or panel_plan.pit_bundle_ref.sha256 != evaluator_contract.pit_bundle_sha256
        or panel_plan.prices_provenance_ref.sha256 != evaluator_contract.prices_provenance_sha256
        or baseline_authority.policy_revision_ref.sha256 != evaluator_contract.baseline_policy_revision_sha256
        or baseline_authority.source_bundle_ref.sha256 != evaluator_contract.baseline_source_bundle_sha256
    ):
        raise ValueError("manifest dependency identities are inconsistent")
    baseline_parent_candidate_v5(
        authority=baseline_authority,
        discovery_plan=panel_plan,
        evaluator_contract=evaluator_contract,
    )
    expected_source = baseline_authority.policy_revision.editable_source_sha256
    bundle_source = tuple((item.path, item.sha256) for item in baseline_authority.source_bundle.files)
    if source_snapshot.editable_source_sha256 != expected_source or bundle_source != expected_source:
        raise ValueError("clean source snapshot differs from the authenticated baseline policy")
    validate_sandbox_profile_resources_v5(sandbox_profile, resources)


def build_campaign_manifest_v5(
    *,
    repository: ManifestRepositoryV5,
    campaign_id: str,
    target: AnnualizedReturnTargetV5,
    source_snapshot: PolicySourceSnapshotV5,
    execution_profile_ref: ArtifactRefV5,
    evaluator_contract_ref: ArtifactRefV5,
    baseline_authority_ref: ArtifactRefV5,
    panel_plan_ref: ArtifactRefV5,
    sandbox_profile_ref: ArtifactRefV5,
    policy_scope_path: str,
    manifest_path: str,
    search: SearchCapabilitiesV5 | None = None,
    provider: ProviderCapabilitiesV5 | None = None,
    resources: ResourceCapabilitiesV5 | None = None,
) -> AuthenticatedCampaignManifestV5:
    """Create and reauthenticate one discovery-only campaign manifest."""

    for path in (policy_scope_path, manifest_path):
        ArtifactRefV5(path, "0" * 64)
    if policy_scope_path == manifest_path:
        raise ValueError("policy scope and manifest paths must be distinct")
    resolved_search = SearchCapabilitiesV5() if search is None else search
    resolved_resources = ResourceCapabilitiesV5() if resources is None else resources
    if type(resolved_search) is not SearchCapabilitiesV5:
        raise ValueError("manifest search capabilities are invalid")
    if provider is not None and type(provider) is not ProviderCapabilitiesV5:
        raise ValueError("manifest provider capabilities are invalid")
    if type(resolved_resources) is not ResourceCapabilitiesV5:
        raise ValueError("manifest resource capabilities are invalid")

    execution, evaluator, baseline, panel_plan, sandbox = _authenticate_build_dependencies_v5(
        repository=repository,
        execution_profile_ref=execution_profile_ref,
        evaluator_contract_ref=evaluator_contract_ref,
        baseline_authority_ref=baseline_authority_ref,
        panel_plan_ref=panel_plan_ref,
        sandbox_profile_ref=sandbox_profile_ref,
    )
    _validate_precomposition_bindings_v5(
        target=target,
        source_snapshot=source_snapshot,
        execution_profile_ref=execution_profile_ref,
        execution_profile=execution,
        evaluator_contract_ref=evaluator_contract_ref,
        evaluator_contract=evaluator,
        baseline_authority_ref=baseline_authority_ref,
        baseline_authority=baseline,
        panel_plan_ref=panel_plan_ref,
        panel_plan=panel_plan,
        sandbox_profile_ref=sandbox_profile_ref,
        sandbox_profile=sandbox,
        resources=resolved_resources,
    )

    # Preserve the accepted Task 1 identity: the baseline descriptor is the
    # exact PolicyRevisionIdentityV5 child already named by the baseline.
    baseline_revision_ref = repository.create_typed_artifact(
        baseline.policy_revision_ref.relative_path,
        baseline.policy_revision,
    )
    if baseline_revision_ref != baseline.policy_revision_ref:
        raise ValueError("baseline policy descriptor differs from its accepted authority")
    policy_scope = PolicyScopeDescriptorV5(
        schema_version=5,
        source_commit=source_snapshot.source_commit,
        editable_source_sha256=source_snapshot.editable_source_sha256,
        baseline_policy_revision_ref=baseline_revision_ref,
        full_source_escape_allowed=resolved_search.allow_full_source_escape,
    )
    policy_scope_ref = repository.create_typed_artifact(policy_scope_path, policy_scope)
    manifest = CampaignManifestV5(
        schema_version=5,
        campaign_id=campaign_id,
        target=target,
        search=resolved_search,
        provider=provider,
        resources=resolved_resources,
        artifact_root=ARTIFACT_ROOT_V5,
        execution_profile_ref=execution_profile_ref,
        evaluator_contract_ref=evaluator_contract_ref,
        baseline_authority_ref=baseline_authority_ref,
        panel_plan_ref=panel_plan_ref,
        policy_scope_ref=policy_scope_ref,
        baseline_policy_revision_ref=baseline_revision_ref,
        sandbox_profile_ref=sandbox_profile_ref,
        source_commit=source_snapshot.source_commit,
        apply=False,
        qualification_allowed=False,
        full_replay_allowed=False,
    )
    manifest_ref = repository.create_typed_artifact(manifest_path, manifest)
    return authenticate_campaign_manifest_v5(repository=repository, manifest_ref=manifest_ref)


def authenticate_campaign_manifest_v5(
    *,
    repository: ManifestRepositoryV5,
    manifest_ref: ArtifactRefV5,
) -> AuthenticatedCampaignManifestV5:
    """Authenticate the complete graph, then parse and cross-bind authorities."""

    if type(manifest_ref) is not ArtifactRefV5:
        raise ValueError("manifest reference is invalid")
    graph = repository.verify_graph(manifest_ref)
    if type(graph) is not ArtifactGraphVerificationV5 or not graph.verified:
        reason = "invalid"
        if type(graph) is ArtifactGraphVerificationV5 and graph.failure is not None:
            reason = graph.failure.code
        raise ManifestAuthenticationFailureV5(f"campaign artifact graph is {reason}")

    # Typed construction is intentionally after complete graph authentication.
    manifest = repository.load_typed_artifact(manifest_ref, value_type=CampaignManifestV5)
    execution = repository.load_typed_artifact(
        manifest.execution_profile_ref,
        value_type=ExecutionProfileV5,
    )
    evaluator = repository.load_typed_artifact(
        manifest.evaluator_contract_ref,
        value_type=EvaluatorContractV5,
    )
    baseline = repository.load_typed_artifact(
        manifest.baseline_authority_ref,
        value_type=BaselineParentAuthorityV5,
    )
    panel_plan = repository.load_typed_artifact(
        manifest.panel_plan_ref,
        value_type=CampaignPanelPlanV5,
    )
    policy_scope = repository.load_typed_artifact(
        manifest.policy_scope_ref,
        value_type=PolicyScopeDescriptorV5,
    )
    baseline_revision = repository.load_typed_artifact(
        manifest.baseline_policy_revision_ref,
        value_type=PolicyRevisionIdentityV5,
    )
    sandbox = repository.load_typed_artifact(
        manifest.sandbox_profile_ref,
        value_type=SandboxProfileV5,
    )
    source_bundle = repository.load_typed_artifact(
        baseline.source_bundle_ref,
        value_type=SourceBundleV5,
    )

    validate_campaign_manifest_bindings_v5(
        manifest,
        panel_plan=panel_plan,
        evaluator_contract=evaluator,
    )
    validate_sandbox_profile_resources_v5(sandbox, manifest.resources)
    _validate_precomposition_bindings_v5(
        target=manifest.target,
        source_snapshot=PolicySourceSnapshotV5(
            source_commit=policy_scope.source_commit,
            editable_source_sha256=policy_scope.editable_source_sha256,
        ),
        execution_profile_ref=manifest.execution_profile_ref,
        execution_profile=execution,
        evaluator_contract_ref=manifest.evaluator_contract_ref,
        evaluator_contract=evaluator,
        baseline_authority_ref=manifest.baseline_authority_ref,
        baseline_authority=baseline,
        panel_plan_ref=manifest.panel_plan_ref,
        panel_plan=panel_plan,
        sandbox_profile_ref=manifest.sandbox_profile_ref,
        sandbox_profile=sandbox,
        resources=manifest.resources,
    )
    if (
        manifest_ref.sha256 != manifest.sha256
        or manifest.source_commit != policy_scope.source_commit
        or manifest.baseline_policy_revision_ref != baseline.policy_revision_ref
        or policy_scope.baseline_policy_revision_ref != baseline.policy_revision_ref
        or baseline_revision != baseline.policy_revision
        or source_bundle != baseline.source_bundle
        or policy_scope.full_source_escape_allowed != manifest.search.allow_full_source_escape
        or manifest.apply is not False
        or manifest.qualification_allowed is not False
        or manifest.full_replay_allowed is not False
    ):
        raise ManifestAuthenticationFailureV5("campaign manifest authorities differ")
    return AuthenticatedCampaignManifestV5(
        manifest_ref=manifest_ref,
        graph=graph,
        manifest=manifest,
        panel_plan=panel_plan,
        execution_profile=execution,
        evaluator_contract=evaluator,
        baseline_authority=baseline,
        baseline_policy_revision=baseline_revision,
        policy_scope=policy_scope,
        sandbox_profile=sandbox,
    )


def render_discovery_command_v5(
    *,
    authenticated: AuthenticatedCampaignManifestV5,
) -> RenderedDiscoveryAuthorizationV5:
    """Render content-free manifest scope without inventing adapter authority."""

    if type(authenticated) is not AuthenticatedCampaignManifestV5:
        raise ValueError("rendering requires an authenticated campaign manifest")
    manifest = authenticated.manifest
    return RenderedDiscoveryAuthorizationV5(
        schema_version=5,
        manifest_ref=authenticated.manifest_ref,
        campaign_id=manifest.campaign_id,
        target=manifest.target,
        search=manifest.search,
        provider=manifest.provider,
        resources=manifest.resources,
        policy_scope_ref=manifest.policy_scope_ref,
        source_commit=manifest.source_commit,
        apply=False,
        qualification_allowed=False,
        full_replay_allowed=False,
        adapter_composition="pending",
        executable=False,
    )


def provider_capabilities_from_values_v5(
    *,
    model: str | None,
    maximum_role_calls: int | None,
    maximum_total_tokens: int | None,
    maximum_output_tokens_per_role: int | None,
    maximum_usd: Decimal | None,
    automatic_retries: int,
    schema_repair_calls: int,
    input_token_overhead_upper_bound: int,
    price_upper_bound: object = None,
) -> ProviderCapabilitiesV5 | None:
    """Construct an optional provider authority without weakening numeric caps."""

    required = (model, maximum_role_calls, maximum_total_tokens, maximum_output_tokens_per_role)
    if all(item is None for item in required):
        if (
            maximum_usd is not None
            or automatic_retries != 0
            or schema_repair_calls != 0
            or price_upper_bound is not None
        ):
            raise ValueError("provider-free manifest cannot carry provider allowances")
        return None
    if any(item is None for item in required):
        raise ValueError("provider authority requires model, call, and token ceilings")
    return ProviderCapabilitiesV5(
        model=model,  # type: ignore[arg-type]
        maximum_role_calls=maximum_role_calls,  # type: ignore[arg-type]
        maximum_total_tokens=maximum_total_tokens,  # type: ignore[arg-type]
        maximum_output_tokens_per_role=maximum_output_tokens_per_role,  # type: ignore[arg-type]
        maximum_usd=maximum_usd,
        automatic_retries=automatic_retries,
        schema_repair_calls=schema_repair_calls,
        price_upper_bound=price_upper_bound,  # type: ignore[arg-type]
        input_token_overhead_upper_bound=input_token_overhead_upper_bound,
    )


__all__ = [
    "AuthenticatedCampaignManifestV5",
    "ManifestAuthenticationFailureV5",
    "ManifestRepositoryV5",
    "PolicyScopeDescriptorV5",
    "PolicySourceSnapshotV5",
    "RenderedDiscoveryAuthorizationV5",
    "authenticate_campaign_manifest_v5",
    "build_campaign_manifest_v5",
    "capture_clean_policy_snapshot_v5",
    "provider_capabilities_from_values_v5",
    "render_discovery_command_v5",
]
