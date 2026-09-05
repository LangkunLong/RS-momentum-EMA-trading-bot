"""Root-bound, no-follow candidate workspace adapter for PIT optimizer V5.

The adapter contains no Git or filesystem implementation. It makes authority
decisions around opaque handles supplied by an injected ``GitWorkspaceDriverV5``.
After root acquisition, no mutation API accepts an absolute string path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ntpath
from pathlib import PurePosixPath, PureWindowsPath
import re
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
WorkspacePurposeV5 = Literal["source", "workspace"]
WorkspacePresenceV5 = Literal["present", "absent"]
WorkspaceRemovalStateV5 = Literal["removed", "absent"]

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


def _canonical_windows_path(value: object, label: str) -> str:
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


def _windows_key(path: str) -> str:
    return ntpath.normcase(_canonical_windows_path(path, "Windows path"))


def _roots_overlap(first: str, second: str) -> bool:
    first_key = _windows_key(first)
    second_key = _windows_key(second)
    try:
        common = ntpath.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _relative_component(value: object, label: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError(f"{label} must be one canonical relative component")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or len(pure.parts) != 1
        or pure.as_posix() != value
        or pure.parts[0] in {"", ".", ".."}
        or pure.parts[0].endswith((".", " "))
    ):
        raise ValueError(f"{label} must be one canonical relative component")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceFailureV5:
    """Sanitized failure that never carries a path, token, or driver message."""

    code: WorkspaceFailureCodeV5

    def __post_init__(self) -> None:
        if self.code not in _FAILURE_CODES_V5:
            raise ValueError("workspace failure is outside the closed V5 taxonomy")


class WorkspaceAdapterErrorV5(RuntimeError):
    def __init__(self, failure: WorkspaceFailureV5) -> None:
        super().__init__(failure.code)
        self.failure = failure


class WorkspaceDriverBoundaryErrorV5(RuntimeError):
    """Stable rejection emitted by a no-follow driver operation."""

    def __init__(self, code: Literal["unsafe_path", "foreign_lease", "driver_failed"]) -> None:
        if code not in {"unsafe_path", "foreign_lease", "driver_failed"}:
            raise ValueError("workspace driver failure is invalid")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkspaceOwnerV5:
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
    source_root: str
    workspace_root: str

    def __post_init__(self) -> None:
        source = _canonical_windows_path(self.source_root, "workspace source root")
        workspace = _canonical_windows_path(self.workspace_root, "disposable workspace root")
        if _roots_overlap(source, workspace):
            raise ValueError("source and disposable workspace roots must be fully disjoint")


@dataclass(frozen=True, slots=True)
class WorkspaceRootHandleV5:
    """Driver-acquired canonical, link-free root and its opaque capability."""

    purpose: WorkspacePurposeV5
    configured_path: str
    canonical_path: str
    root_identity_sha256: str
    canonical_path_sha256: str
    entire_chain_link_free: bool
    opaque_handle: object

    def __post_init__(self) -> None:
        if self.purpose not in {"source", "workspace"}:
            raise ValueError("workspace root handle purpose is invalid")
        configured = _canonical_windows_path(self.configured_path, "configured root")
        canonical = _canonical_windows_path(self.canonical_path, "canonical root")
        if _windows_key(configured) != _windows_key(canonical):
            raise ValueError("driver root handle resolves to a different path")
        _digest(self.root_identity_sha256, "workspace root identity")
        expected = hashlib.sha256(_windows_key(canonical).encode("utf-8")).hexdigest()
        if self.canonical_path_sha256 != expected:
            raise ValueError("workspace root canonical path digest is invalid")
        if self.entire_chain_link_free is not True or self.opaque_handle is None:
            raise ValueError("workspace root handle is not an opaque no-follow capability")


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseV5:
    payload: ResourceLeasePayloadV5
    owner: WorkspaceOwnerV5
    workspace_root_identity_sha256: str
    workspace_root_path_sha256: str
    workspace_relative_path: str
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
        _digest(self.workspace_root_identity_sha256, "workspace lease root identity")
        _digest(self.workspace_root_path_sha256, "workspace lease root path")
        relative = _relative_component(self.workspace_relative_path, "leased workspace path")
        _digest(self.lease_token_sha256, "workspace lease token")
        _digest(self.parent_revision_sha256, "workspace parent revision")
        _digest(self.child_revision_sha256, "workspace child revision")
        prefix = f"pit-v5-{self.owner.round_index}-"
        if not relative.startswith(prefix):
            raise ValueError("leased workspace path differs from its owner round")
        token = _token(relative.removeprefix(prefix), "leased workspace token")
        if (
            hashlib.sha256(token.encode("utf-8")).hexdigest() != self.lease_token_sha256
            or self.payload.lease_id != f"workspace.{self.lease_token_sha256}"
        ):
            raise ValueError("workspace lease token binding is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)

    def as_owned_lease(self) -> OwnedLeaseV5:
        return OwnedLeaseV5(self.payload, self.owner.round_index, self)


@dataclass(frozen=True, slots=True)
class OwnedWorkspaceHandleV5:
    """Opaque workspace capability bound to one exact root-relative lease."""

    workspace_root_identity_sha256: str
    workspace_root_path_sha256: str
    workspace_relative_path: str
    lease_id: str
    lease_token_sha256: str
    handle_identity_sha256: str
    opaque_handle: object

    def __post_init__(self) -> None:
        _digest(self.workspace_root_identity_sha256, "workspace handle root identity")
        _digest(self.workspace_root_path_sha256, "workspace handle root path")
        _relative_component(self.workspace_relative_path, "workspace handle relative path")
        _text(self.lease_id, "workspace handle lease ID")
        _digest(self.lease_token_sha256, "workspace handle lease token")
        _digest(self.handle_identity_sha256, "workspace opaque handle identity")
        if self.opaque_handle is None:
            raise ValueError("owned workspace requires an opaque driver capability")


def _handle_matches_lease(handle: OwnedWorkspaceHandleV5, lease: WorkspaceLeaseV5) -> bool:
    return (
        handle.workspace_root_identity_sha256 == lease.workspace_root_identity_sha256
        and handle.workspace_root_path_sha256 == lease.workspace_root_path_sha256
        and handle.workspace_relative_path == lease.workspace_relative_path
        and handle.lease_id == lease.payload.lease_id
        and handle.lease_token_sha256 == lease.lease_token_sha256
    )


@dataclass(frozen=True, slots=True)
class MaterializedWorkspaceV5:
    parent_revision: PolicyRevisionIdentityV5
    variant: RenderedVariantV5
    lease: WorkspaceLeaseV5
    workspace_handle: OwnedWorkspaceHandleV5

    def __post_init__(self) -> None:
        if (
            type(self.parent_revision) is not PolicyRevisionIdentityV5
            or type(self.variant) is not RenderedVariantV5
            or type(self.lease) is not WorkspaceLeaseV5
            or type(self.workspace_handle) is not OwnedWorkspaceHandleV5
        ):
            raise ValueError("materialized workspace authority is invalid")
        if (
            self.lease.parent_revision_sha256 != self.parent_revision.sha256
            or self.lease.child_revision_sha256 != self.variant.policy_revision.sha256
            or not _handle_matches_lease(self.workspace_handle, self.lease)
        ):
            raise ValueError("materialized workspace identity differs from its lease")

    def as_runtime_materialization(self, source_bundle_ref: ArtifactRefV5) -> MaterializedVariantV5:
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
    """No-follow capability driver; only root acquisition accepts an absolute path."""

    def acquire_root(self, *, configured_path: str, purpose: WorkspacePurposeV5) -> WorkspaceRootHandleV5: ...

    def read_root_policy_file(self, *, root: WorkspaceRootHandleV5, relative_path: str) -> bytes: ...

    def create_disposable_workspace(
        self,
        *,
        source_root: WorkspaceRootHandleV5,
        workspace_root: WorkspaceRootHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> OwnedWorkspaceHandleV5: ...

    def open_owned_workspace(
        self,
        *,
        workspace_root: WorkspaceRootHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> OwnedWorkspaceHandleV5 | None: ...

    def workspace_presence(
        self,
        *,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> WorkspacePresenceV5: ...

    def read_workspace_policy_file(self, *, workspace: OwnedWorkspaceHandleV5, relative_path: str) -> bytes: ...

    def write_workspace_policy_file(
        self,
        *,
        workspace: OwnedWorkspaceHandleV5,
        relative_path: str,
        content: bytes,
    ) -> None: ...

    def load_lease(self, lease_id: str) -> WorkspaceLeaseV5 | None: ...

    def controller_lease_is_live(self, owner: WorkspaceOwnerV5) -> bool: ...

    def remove_workspace_no_follow(
        self,
        *,
        workspace_root: WorkspaceRootHandleV5,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> WorkspaceRemovalStateV5: ...

    def retire_lease(self, lease_id: str) -> None: ...


class GitCandidateMaterializerV5:
    def __init__(
        self,
        *,
        roots: WorkspaceRootsV5,
        driver: GitWorkspaceDriverV5,
        token_factory: Callable[[], str],
    ) -> None:
        if (
            type(roots) is not WorkspaceRootsV5
            or not isinstance(driver, GitWorkspaceDriverV5)
            or not callable(token_factory)
        ):
            raise ValueError("workspace adapter dependencies are invalid")
        self._driver = driver
        self._token_factory = token_factory
        self._source_root = self._acquire_root(roots.source_root, "source")
        self._workspace_root = self._acquire_root(roots.workspace_root, "workspace")
        if _roots_overlap(self._source_root.canonical_path, self._workspace_root.canonical_path):
            raise ValueError("acquired source and workspace roots are not fully disjoint")
        if self._source_root.root_identity_sha256 == self._workspace_root.root_identity_sha256:
            raise ValueError("source and workspace root handles must be distinct")

    def _fail(self, code: WorkspaceFailureCodeV5) -> None:
        raise WorkspaceAdapterErrorV5(WorkspaceFailureV5(code))

    def _driver_call(self, operation: Callable[[], object]) -> object:
        try:
            return operation()
        except WorkspaceDriverBoundaryErrorV5 as exc:
            self._fail(exc.code)
        except BaseException:
            self._fail("driver_failed")

    def _acquire_root(self, path: str, purpose: WorkspacePurposeV5) -> WorkspaceRootHandleV5:
        result = self._driver_call(lambda: self._driver.acquire_root(configured_path=path, purpose=purpose))
        if (
            type(result) is not WorkspaceRootHandleV5
            or result.purpose != purpose
            or _windows_key(result.configured_path) != _windows_key(path)
        ):
            self._fail("invalid_root")
        return result

    @staticmethod
    def _validate_policy_path(relative_path: str) -> str:
        if relative_path not in EDITABLE_POLICY_PATHS_V5:
            raise ValueError("policy path is outside the closed V5 editable scope")
        return relative_path

    def _source_file(
        self,
        relative_path: str,
        raw: object,
        failure: WorkspaceFailureCodeV5,
    ) -> SourceFileV5:
        if type(raw) is not bytes:
            self._fail("driver_failed")
        try:
            return SourceFileV5(relative_path, raw.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            self._fail(failure)

    def _read_source_bundle(self) -> SourceBundleV5:
        files = []
        for relative_path in EDITABLE_POLICY_PATHS_V5:
            raw = self._driver_call(
                lambda relative_path=relative_path: self._driver.read_root_policy_file(
                    root=self._source_root,
                    relative_path=self._validate_policy_path(relative_path),
                )
            )
            files.append(self._source_file(relative_path, raw, "source_identity_mismatch"))
        return SourceBundleV5(tuple(files))

    def _read_workspace_bundle(self, workspace: OwnedWorkspaceHandleV5) -> SourceBundleV5:
        files = []
        for relative_path in EDITABLE_POLICY_PATHS_V5:
            raw = self._driver_call(
                lambda relative_path=relative_path: self._driver.read_workspace_policy_file(
                    workspace=workspace,
                    relative_path=self._validate_policy_path(relative_path),
                )
            )
            files.append(self._source_file(relative_path, raw, "workspace_identity_mismatch"))
        return SourceBundleV5(tuple(files))

    @staticmethod
    def _bundle_matches_revision(bundle: SourceBundleV5, revision: PolicyRevisionIdentityV5) -> bool:
        return tuple((item.path, item.sha256) for item in bundle.files) == revision.editable_source_sha256

    def _new_lease(
        self,
        owner: WorkspaceOwnerV5,
        parent: PolicyRevisionIdentityV5,
        variant: RenderedVariantV5,
    ) -> WorkspaceLeaseV5:
        try:
            token = _token(self._token_factory(), "workspace lease token")
        except BaseException:
            self._fail("driver_failed")
        token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return WorkspaceLeaseV5(
            ResourceLeasePayloadV5(
                f"workspace.{token_sha256}",
                "workspace",
                owner.campaign_id,
                owner.owner_token_sha256,
            ),
            owner,
            self._workspace_root.root_identity_sha256,
            self._workspace_root.canonical_path_sha256,
            f"pit-v5-{owner.round_index}-{token}",
            token_sha256,
            parent.sha256,
            variant.policy_revision.sha256,
        )

    def _authorize_lease(self, owner: WorkspaceOwnerV5, lease: WorkspaceLeaseV5) -> None:
        if type(owner) is not WorkspaceOwnerV5 or type(lease) is not WorkspaceLeaseV5:
            self._fail("invalid_authority")
        if (
            lease.owner != owner
            or lease.workspace_root_identity_sha256 != self._workspace_root.root_identity_sha256
            or lease.workspace_root_path_sha256 != self._workspace_root.canonical_path_sha256
        ):
            self._fail("foreign_lease")

    def _authorize_handle(self, handle: object, lease: WorkspaceLeaseV5) -> OwnedWorkspaceHandleV5:
        if type(handle) is not OwnedWorkspaceHandleV5 or not _handle_matches_lease(handle, lease):
            self._fail("foreign_lease")
        presence = self._driver_call(lambda: self._driver.workspace_presence(workspace=handle, lease=lease))
        if presence != "present":
            self._fail("unsafe_path")
        return handle

    def _load_lease(self, lease_id: str) -> WorkspaceLeaseV5 | None:
        observed = self._driver_call(lambda: self._driver.load_lease(lease_id))
        if observed is not None and type(observed) is not WorkspaceLeaseV5:
            self._fail("foreign_lease")
        return observed

    def materialize(
        self,
        *,
        owner: WorkspaceOwnerV5,
        parent_revision: PolicyRevisionIdentityV5,
        variant: RenderedVariantV5,
    ) -> MaterializedWorkspaceV5:
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
        source_bundle = self._read_source_bundle()
        if not self._bundle_matches_revision(source_bundle, parent_revision):
            self._fail("source_identity_mismatch")

        lease = self._new_lease(owner, parent_revision, variant)
        handle: OwnedWorkspaceHandleV5 | None = None
        try:
            created = self._driver_call(
                lambda: self._driver.create_disposable_workspace(
                    source_root=self._source_root,
                    workspace_root=self._workspace_root,
                    lease=lease,
                )
            )
            handle = self._authorize_handle(created, lease)
            if self._load_lease(lease.payload.lease_id) != lease:
                self._fail("foreign_lease")
            workspace_parent = self._read_workspace_bundle(handle)
            if not self._bundle_matches_revision(workspace_parent, parent_revision):
                self._fail("source_identity_mismatch")
            for source_file in variant.source_bundle.files:
                self._driver_call(
                    lambda source_file=source_file: self._driver.write_workspace_policy_file(
                        workspace=handle,
                        relative_path=self._validate_policy_path(source_file.path),
                        content=source_file.source.encode("utf-8"),
                    )
                )
            child_bundle = self._read_workspace_bundle(handle)
            child_revision = derive_policy_revision_identity_v5(
                source_bundle=child_bundle,
                trusted_policy_runtime_sha256=parent_revision.trusted_policy_runtime_sha256,
                immutable_constraints_sha256=parent_revision.immutable_constraints_sha256,
            )
            if child_bundle != variant.source_bundle or child_revision != variant.policy_revision:
                self._fail("workspace_identity_mismatch")
            handle = self._authorize_handle(handle, lease)
            if self._load_lease(lease.payload.lease_id) != lease:
                self._fail("foreign_lease")
        except BaseException:
            self._cleanup_failed_materialization(owner, lease)
            raise
        return MaterializedWorkspaceV5(parent_revision, variant, lease, handle)

    def _cleanup_failed_materialization(self, owner: WorkspaceOwnerV5, lease: WorkspaceLeaseV5) -> None:
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
        materialized = self.materialize(owner=owner, parent_revision=parent_revision, variant=variant)
        try:
            yield materialized
        finally:
            self.cleanup(owner=owner, lease=materialized.lease)

    def cleanup(self, *, owner: WorkspaceOwnerV5, lease: WorkspaceLeaseV5) -> bool:
        """Atomically remove one root-bound workspace and retire its exact lease."""

        self._authorize_lease(owner, lease)
        registered = self._load_lease(lease.payload.lease_id)
        opened = self._driver_call(
            lambda: self._driver.open_owned_workspace(workspace_root=self._workspace_root, lease=lease)
        )
        if opened is not None and (
            type(opened) is not OwnedWorkspaceHandleV5 or not _handle_matches_lease(opened, lease)
        ):
            self._fail("foreign_lease")
        if registered is None:
            if opened is not None:
                self._fail("foreign_lease")
            return False
        if registered != lease:
            self._fail("foreign_lease")

        removed = False
        if opened is not None:
            handle = self._authorize_handle(opened, lease)
            state = self._driver_call(
                lambda: self._driver.remove_workspace_no_follow(
                    workspace_root=self._workspace_root,
                    workspace=handle,
                    lease=lease,
                )
            )
            if state not in {"removed", "absent"}:
                self._fail("driver_failed")
            removed = state == "removed"
        # An exact registered lease with no directory means removal committed
        # before retirement. Retiring now makes that crash boundary idempotent.
        self._driver_call(lambda: self._driver.retire_lease(lease.payload.lease_id))
        if self._load_lease(lease.payload.lease_id) is not None:
            self._fail("driver_failed")
        reopened = self._driver_call(
            lambda: self._driver.open_owned_workspace(workspace_root=self._workspace_root, lease=lease)
        )
        if reopened is not None:
            self._fail("unsafe_path")
        return removed

    def reclaim_stale(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[WorkspaceLeaseV5, ...],
    ) -> int:
        if type(owner) is not WorkspaceOwnerV5 or type(leases) is not tuple:
            self._fail("invalid_authority")
        if any(type(item) is not WorkspaceLeaseV5 for item in leases):
            self._fail("invalid_authority")
        if len({item.payload.lease_id for item in leases}) != len(leases):
            self._fail("invalid_authority")
        for lease in leases:
            self._authorize_lease(owner, lease)
        live = self._driver_call(lambda: self._driver.controller_lease_is_live(owner))
        if type(live) is not bool:
            self._fail("driver_failed")
        if live:
            self._fail("live_lease")
        return sum(self.cleanup(owner=owner, lease=lease) for lease in leases)


__all__ = [
    "GitCandidateMaterializerV5",
    "GitWorkspaceDriverV5",
    "MaterializedWorkspaceV5",
    "OwnedWorkspaceHandleV5",
    "WorkspaceAdapterErrorV5",
    "WorkspaceDriverBoundaryErrorV5",
    "WorkspaceFailureCodeV5",
    "WorkspaceFailureV5",
    "WorkspaceLeaseV5",
    "WorkspaceOwnerV5",
    "WorkspacePresenceV5",
    "WorkspaceRemovalStateV5",
    "WorkspaceRootHandleV5",
    "WorkspaceRootsV5",
]
