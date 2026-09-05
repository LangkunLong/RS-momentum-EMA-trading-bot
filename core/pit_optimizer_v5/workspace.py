"""Exact, campaign-owned workspace materialization for PIT optimizer V5.

This module deliberately contains no Git or filesystem implementation.  Production
composition supplies a :class:`GitWorkspaceDriverV5`; the adapter below owns all
identity, containment, ownership, and cleanup decisions around that driver.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from collections.abc import Iterator
from typing import Callable, Literal, Protocol, runtime_checkable

from core.pit_optimizer_v5.candidate_ir import (
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    SourceFileV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_sha256_v5
from core.pit_optimizer_v5.memory import ResourceLeasePayloadV5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.runtime import MaterializedVariantV5, OwnedLeaseV5


WorkspaceFailureCodeV5 = Literal[
    "invalid_authority",
    "invalid_root",
    "source_identity_mismatch",
    "workspace_identity_mismatch",
    "foreign_lease",
    "live_lease",
    "unsafe_path",
    "driver_failed",
]

_FAILURE_CODES_V5 = frozenset(
    {
        "invalid_authority",
        "invalid_root",
        "source_identity_mismatch",
        "workspace_identity_mismatch",
        "foreign_lease",
        "live_lease",
        "unsafe_path",
        "driver_failed",
    }
)
_OPAQUE_TOKEN_RE_V5 = re.compile(r"[a-z0-9][a-z0-9_-]{15,127}")


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _token(value: object, label: str) -> str:
    if type(value) is not str or _OPAQUE_TOKEN_RE_V5.fullmatch(value) is None:
        raise ValueError(f"{label} must be an opaque lowercase token")
    return value


def _canonical_absolute_path(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be an absolute canonical path")
    candidate = Path(value)
    if (
        not candidate.is_absolute()
        or str(candidate) != value
        or os.path.normpath(value) != value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"{label} must be an absolute canonical path")
    return value


def _contained_child(root: str, candidate: str) -> bool:
    try:
        relative = Path(candidate).relative_to(Path(root))
    except ValueError:
        return False
    return bool(relative.parts) and all(part not in {"", ".", ".."} for part in relative.parts)


@dataclass(frozen=True, slots=True)
class WorkspaceFailureV5:
    """Sanitized stable workspace failure without local path or token details."""

    code: WorkspaceFailureCodeV5

    def __post_init__(self) -> None:
        if self.code not in _FAILURE_CODES_V5:
            raise ValueError("workspace failure is outside the closed V5 taxonomy")


class WorkspaceAdapterErrorV5(RuntimeError):
    """Raised when a workspace operation fails closed."""

    def __init__(self, failure: WorkspaceFailureV5) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class WorkspaceOwnerV5:
    """Authenticated campaign/round/controller ownership authority."""

    campaign_id: str
    round_index: int
    owner_token_sha256: str
    controller_lease_id: str

    def __post_init__(self) -> None:
        _text(self.campaign_id, "workspace owner campaign")
        if type(self.round_index) is not int or self.round_index <= 0:
            raise ValueError("workspace owner round must be a positive integer")
        _digest(self.owner_token_sha256, "workspace owner token")
        _token(self.controller_lease_id, "controller lease ID")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class WorkspaceRootsV5:
    """Canonical configured checkout and disposable-workspace roots."""

    source_root: str
    workspace_root: str

    def __post_init__(self) -> None:
        source = _canonical_absolute_path(self.source_root, "workspace source root")
        workspace = _canonical_absolute_path(self.workspace_root, "disposable workspace root")
        if Path(source) == Path(workspace) or _contained_child(workspace, source):
            raise ValueError("source checkout cannot be the disposable workspace root or its child")


@dataclass(frozen=True, slots=True)
class WorkspacePathFactsV5:
    """Driver-observed exact path facts; resolved paths never replace authority."""

    requested_path: str
    absolute_path: str
    resolved_path: str | None
    kind: Literal["missing", "file", "directory"]
    entire_chain_link_free: bool

    def __post_init__(self) -> None:
        requested = _canonical_absolute_path(self.requested_path, "inspected requested path")
        absolute = _canonical_absolute_path(self.absolute_path, "inspected absolute path")
        if requested != absolute:
            raise ValueError("path inspection changed the requested absolute path")
        if self.kind not in {"missing", "file", "directory"}:
            raise ValueError("path inspection kind is invalid")
        if type(self.entire_chain_link_free) is not bool:
            raise ValueError("path inspection link fact is invalid")
        if self.kind == "missing":
            if self.resolved_path is not None:
                raise ValueError("missing path cannot have a resolved target")
        else:
            resolved = _canonical_absolute_path(self.resolved_path, "inspected resolved path")
            if resolved != requested:
                raise ValueError("inspected path resolves through an alias or link")


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseV5:
    """Exact ownership record for one disposable candidate checkout."""

    payload: ResourceLeasePayloadV5
    owner: WorkspaceOwnerV5
    workspace_path: str
    lease_token_sha256: str
    parent_revision_sha256: str
    child_revision_sha256: str

    def __post_init__(self) -> None:
        if type(self.payload) is not ResourceLeasePayloadV5 or self.payload.resource_kind != "workspace":
            raise ValueError("workspace lease payload is invalid")
        if type(self.owner) is not WorkspaceOwnerV5:
            raise ValueError("workspace lease owner is invalid")
        if (
            self.payload.owner_campaign_id != self.owner.campaign_id
            or self.payload.owner_token_sha256 != self.owner.owner_token_sha256
        ):
            raise ValueError("workspace lease payload differs from its owner")
        _canonical_absolute_path(self.workspace_path, "leased workspace path")
        _digest(self.lease_token_sha256, "workspace lease token")
        _digest(self.parent_revision_sha256, "workspace parent revision")
        _digest(self.child_revision_sha256, "workspace child revision")
        prefix = f"pit-v5-{self.owner.round_index}-"
        name = Path(self.workspace_path).name
        if not name.startswith(prefix):
            raise ValueError("leased workspace name differs from its owner round")
        token = _token(name.removeprefix(prefix), "leased workspace token")
        if (
            hashlib.sha256(token.encode("utf-8")).hexdigest() != self.lease_token_sha256
            or self.payload.lease_id != f"workspace.{self.lease_token_sha256}"
        ):
            raise ValueError("workspace lease token binding is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)

    def as_owned_lease(self) -> OwnedLeaseV5:
        """Adapt the exact typed lease to the pure runtime cleanup boundary."""

        return OwnedLeaseV5(
            payload=self.payload,
            round_index=self.owner.round_index,
            opaque_handle=self,
        )


@dataclass(frozen=True, slots=True)
class MaterializedWorkspaceV5:
    """A verified rendered revision in one exact disposable workspace."""

    parent_revision: PolicyRevisionIdentityV5
    variant: RenderedVariantV5
    lease: WorkspaceLeaseV5

    def __post_init__(self) -> None:
        if (
            type(self.parent_revision) is not PolicyRevisionIdentityV5
            or type(self.variant) is not RenderedVariantV5
            or type(self.lease) is not WorkspaceLeaseV5
        ):
            raise ValueError("materialized workspace authority is invalid")
        if (
            self.lease.parent_revision_sha256 != self.parent_revision.sha256
            or self.lease.child_revision_sha256 != self.variant.policy_revision.sha256
        ):
            raise ValueError("materialized workspace identity differs from its lease")

    @property
    def candidate_root(self) -> str:
        return self.lease.workspace_path

    def as_runtime_materialization(self, source_bundle_ref: ArtifactRefV5) -> MaterializedVariantV5:
        """Adapt to Task 8B after the caller supplies its persisted source reference."""

        if type(source_bundle_ref) is not ArtifactRefV5:
            raise ValueError("materialized source reference is invalid")
        return MaterializedVariantV5(
            variant=self.variant,
            source_bundle_ref=source_bundle_ref,
            leases=(self.lease.as_owned_lease(),),
            opaque_candidate=self,
        )


@runtime_checkable
class GitWorkspaceDriverV5(Protocol):
    """Injected exact local operations; implementations may use Git, this module does not."""

    def inspect_path(self, path: str) -> WorkspacePathFactsV5: ...

    def read_file_bytes(self, path: str) -> bytes: ...

    def create_disposable_workspace(
        self,
        *,
        source_root: str,
        workspace_path: str,
        lease: WorkspaceLeaseV5,
    ) -> None: ...

    def write_policy_file(
        self,
        *,
        workspace_path: str,
        relative_path: str,
        content: bytes,
    ) -> None: ...

    def load_lease(self, lease_id: str) -> WorkspaceLeaseV5 | None: ...

    def controller_lease_is_live(self, owner: WorkspaceOwnerV5) -> bool: ...

    def remove_workspace_exact(self, *, workspace_path: str, lease_token_sha256: str) -> None: ...

    def retire_lease(self, lease_id: str) -> None: ...


class GitCandidateMaterializerV5:
    """Verify, materialize, and remove one exact V5 policy workspace."""

    def __init__(
        self,
        *,
        roots: WorkspaceRootsV5,
        driver: GitWorkspaceDriverV5,
        token_factory: Callable[[], str],
    ) -> None:
        if type(roots) is not WorkspaceRootsV5 or not isinstance(driver, GitWorkspaceDriverV5):
            raise ValueError("workspace adapter dependencies are invalid")
        if not callable(token_factory):
            raise ValueError("workspace token factory is invalid")
        self._roots = roots
        self._driver = driver
        self._token_factory = token_factory
        self._verify_configured_roots()

    def _fail(self, code: WorkspaceFailureCodeV5) -> None:
        raise WorkspaceAdapterErrorV5(WorkspaceFailureV5(code))

    def _inspect(self, path: str, *, expected_kind: Literal["missing", "file", "directory"]) -> WorkspacePathFactsV5:
        try:
            facts = self._driver.inspect_path(path)
        except BaseException:
            self._fail("driver_failed")
        if (
            type(facts) is not WorkspacePathFactsV5
            or facts.requested_path != path
            or facts.kind != expected_kind
            or not facts.entire_chain_link_free
        ):
            self._fail("unsafe_path")
        return facts

    def _verify_configured_roots(self) -> None:
        self._inspect(self._roots.source_root, expected_kind="directory")
        self._inspect(self._roots.workspace_root, expected_kind="directory")

    @staticmethod
    def _policy_path(root: str, relative_path: str) -> str:
        if relative_path not in EDITABLE_POLICY_PATHS_V5:
            raise ValueError("policy path is outside the closed V5 editable scope")
        candidate = str(Path(root).joinpath(*PurePosixPath(relative_path).parts))
        _canonical_absolute_path(candidate, "policy source path")
        if not _contained_child(root, candidate):
            raise ValueError("policy source path escapes its configured root")
        return candidate

    def _read_source_bundle(self, root: str) -> SourceBundleV5:
        files: list[SourceFileV5] = []
        for relative_path in EDITABLE_POLICY_PATHS_V5:
            absolute = self._policy_path(root, relative_path)
            self._inspect(absolute, expected_kind="file")
            try:
                raw = self._driver.read_file_bytes(absolute)
            except BaseException:
                self._fail("driver_failed")
            if type(raw) is not bytes:
                self._fail("driver_failed")
            try:
                source = raw.decode("utf-8")
                files.append(SourceFileV5(path=relative_path, source=source))
            except (UnicodeDecodeError, TypeError, ValueError):
                self._fail("source_identity_mismatch")
        return SourceBundleV5(files=tuple(files))

    @staticmethod
    def _bundle_matches_revision(bundle: SourceBundleV5, revision: PolicyRevisionIdentityV5) -> bool:
        return tuple((item.path, item.sha256) for item in bundle.files) == revision.editable_source_sha256

    def _new_lease(
        self,
        *,
        owner: WorkspaceOwnerV5,
        parent_revision: PolicyRevisionIdentityV5,
        variant: RenderedVariantV5,
    ) -> WorkspaceLeaseV5:
        try:
            token = _token(self._token_factory(), "workspace lease token")
        except BaseException:
            self._fail("driver_failed")
        lease_token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        workspace_name = f"pit-v5-{owner.round_index}-{token}"
        workspace_path = str(Path(self._roots.workspace_root, workspace_name))
        if not _contained_child(self._roots.workspace_root, workspace_path):
            self._fail("unsafe_path")
        return WorkspaceLeaseV5(
            payload=ResourceLeasePayloadV5(
                lease_id=f"workspace.{lease_token_sha256}",
                resource_kind="workspace",
                owner_campaign_id=owner.campaign_id,
                owner_token_sha256=owner.owner_token_sha256,
            ),
            owner=owner,
            workspace_path=workspace_path,
            lease_token_sha256=lease_token_sha256,
            parent_revision_sha256=parent_revision.sha256,
            child_revision_sha256=variant.policy_revision.sha256,
        )

    def materialize(
        self,
        *,
        owner: WorkspaceOwnerV5,
        parent_revision: PolicyRevisionIdentityV5,
        variant: RenderedVariantV5,
    ) -> MaterializedWorkspaceV5:
        """Create one disposable exact child revision through the injected driver."""

        if (
            type(owner) is not WorkspaceOwnerV5
            or type(parent_revision) is not PolicyRevisionIdentityV5
            or type(variant) is not RenderedVariantV5
        ):
            self._fail("invalid_authority")
        if (
            variant.policy_revision.trusted_policy_runtime_sha256 != parent_revision.trusted_policy_runtime_sha256
            or variant.policy_revision.immutable_constraints_sha256 != parent_revision.immutable_constraints_sha256
        ):
            self._fail("source_identity_mismatch")
        self._verify_configured_roots()
        source_bundle = self._read_source_bundle(self._roots.source_root)
        if not self._bundle_matches_revision(source_bundle, parent_revision):
            self._fail("source_identity_mismatch")

        lease = self._new_lease(owner=owner, parent_revision=parent_revision, variant=variant)
        self._inspect(lease.workspace_path, expected_kind="missing")
        try:
            self._driver.create_disposable_workspace(
                source_root=self._roots.source_root,
                workspace_path=lease.workspace_path,
                lease=lease,
            )
        except BaseException:
            self._cleanup_failed_materialization(owner=owner, lease=lease)
            self._fail("driver_failed")
        try:
            self._inspect(lease.workspace_path, expected_kind="directory")
            registered = self._load_exact_lease(lease.payload.lease_id)
            if registered != lease:
                self._fail("foreign_lease")

            workspace_parent = self._read_source_bundle(lease.workspace_path)
            if not self._bundle_matches_revision(workspace_parent, parent_revision):
                self._fail("source_identity_mismatch")
            for source_file in variant.source_bundle.files:
                try:
                    self._driver.write_policy_file(
                        workspace_path=lease.workspace_path,
                        relative_path=source_file.path,
                        content=source_file.source.encode("utf-8"),
                    )
                except BaseException:
                    self._fail("driver_failed")

            child_bundle = self._read_source_bundle(lease.workspace_path)
            child_revision = derive_policy_revision_identity_v5(
                source_bundle=child_bundle,
                trusted_policy_runtime_sha256=parent_revision.trusted_policy_runtime_sha256,
                immutable_constraints_sha256=parent_revision.immutable_constraints_sha256,
            )
            if child_bundle != variant.source_bundle or child_revision != variant.policy_revision:
                self._fail("workspace_identity_mismatch")
            self._authorize_lease(owner=owner, lease=lease)
        except BaseException:
            self._cleanup_failed_materialization(owner=owner, lease=lease)
            raise
        return MaterializedWorkspaceV5(
            parent_revision=parent_revision,
            variant=variant,
            lease=lease,
        )

    def _cleanup_failed_materialization(
        self,
        *,
        owner: WorkspaceOwnerV5,
        lease: WorkspaceLeaseV5,
    ) -> None:
        """Best-effort exact cleanup; never broadens authority after a failed create."""

        try:
            self.cleanup(owner=owner, lease=lease)
        except BaseException:
            pass

    @contextmanager
    def leased_workspace(
        self,
        *,
        owner: WorkspaceOwnerV5,
        parent_revision: PolicyRevisionIdentityV5,
        variant: RenderedVariantV5,
    ) -> Iterator[MaterializedWorkspaceV5]:
        """Yield one verified materialization and exactly clean it on context exit."""

        materialized = self.materialize(
            owner=owner,
            parent_revision=parent_revision,
            variant=variant,
        )
        try:
            yield materialized
        finally:
            self.cleanup(owner=owner, lease=materialized.lease)

    def _load_exact_lease(self, lease_id: str) -> WorkspaceLeaseV5 | None:
        try:
            observed = self._driver.load_lease(lease_id)
        except BaseException:
            self._fail("driver_failed")
        if observed is not None and type(observed) is not WorkspaceLeaseV5:
            self._fail("foreign_lease")
        return observed

    def _authorize_lease(self, *, owner: WorkspaceOwnerV5, lease: WorkspaceLeaseV5) -> None:
        if type(owner) is not WorkspaceOwnerV5 or type(lease) is not WorkspaceLeaseV5:
            self._fail("invalid_authority")
        if lease.owner != owner or not _contained_child(self._roots.workspace_root, lease.workspace_path):
            self._fail("foreign_lease")
        observed = self._load_exact_lease(lease.payload.lease_id)
        try:
            facts = self._driver.inspect_path(lease.workspace_path)
        except BaseException:
            self._fail("driver_failed")
        if type(facts) is not WorkspacePathFactsV5 or facts.requested_path != lease.workspace_path:
            self._fail("unsafe_path")
        if observed is None and facts.kind == "missing":
            return
        if observed != lease:
            self._fail("foreign_lease")
        if (
            facts.kind != "directory"
            or not facts.entire_chain_link_free
            or facts.absolute_path != lease.workspace_path
            or facts.resolved_path != lease.workspace_path
        ):
            self._fail("unsafe_path")

    def cleanup(self, *, owner: WorkspaceOwnerV5, lease: WorkspaceLeaseV5) -> bool:
        """Remove exactly one owned workspace; return whether it existed."""

        self._authorize_lease(owner=owner, lease=lease)
        observed = self._load_exact_lease(lease.payload.lease_id)
        try:
            facts = self._driver.inspect_path(lease.workspace_path)
        except BaseException:
            self._fail("driver_failed")
        existed = observed is not None or facts.kind != "missing"
        if observed is None and facts.kind == "missing":
            return False
        try:
            self._driver.remove_workspace_exact(
                workspace_path=lease.workspace_path,
                lease_token_sha256=lease.lease_token_sha256,
            )
        except BaseException:
            self._fail("driver_failed")
        self._inspect(lease.workspace_path, expected_kind="missing")
        try:
            self._driver.retire_lease(lease.payload.lease_id)
        except BaseException:
            self._fail("driver_failed")
        if self._load_exact_lease(lease.payload.lease_id) is not None:
            self._fail("driver_failed")
        return existed

    def reclaim_stale(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[WorkspaceLeaseV5, ...],
    ) -> int:
        """Reclaim only supplied authenticated leases for this dead controller."""

        if type(owner) is not WorkspaceOwnerV5 or type(leases) is not tuple:
            self._fail("invalid_authority")
        if any(type(item) is not WorkspaceLeaseV5 for item in leases):
            self._fail("invalid_authority")
        if len({item.payload.lease_id for item in leases}) != len(leases):
            self._fail("invalid_authority")
        if any(item.owner != owner for item in leases):
            self._fail("foreign_lease")
        try:
            live = self._driver.controller_lease_is_live(owner)
        except BaseException:
            self._fail("driver_failed")
        if type(live) is not bool:
            self._fail("driver_failed")
        if live:
            self._fail("live_lease")
        reclaimed = 0
        for lease in leases:
            reclaimed += int(self.cleanup(owner=owner, lease=lease))
        return reclaimed


__all__ = [
    "GitCandidateMaterializerV5",
    "GitWorkspaceDriverV5",
    "MaterializedWorkspaceV5",
    "WorkspaceAdapterErrorV5",
    "WorkspaceFailureCodeV5",
    "WorkspaceFailureV5",
    "WorkspaceLeaseV5",
    "WorkspaceOwnerV5",
    "WorkspacePathFactsV5",
    "WorkspaceRootsV5",
]
