"""Concrete local no-follow workspace capability for PIT optimizer V5."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Literal

from core.pit_optimizer_v5.artifacts import AdapterStateAuthorityV5, ArtifactMissingV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_sha256_v5
from core.pit_optimizer_v5.memory import ResourceLeasePayloadV5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    acquire_directory_v5,
    clear_owned_directory_v5,
    create_directory_in_directory_v5,
    directory_child_absent_v5,
    directory_is_empty_v5,
    hash_regular_in_directory_v5,
    open_regular_in_directory_v5,
    read_regular_in_directory_v5,
    remove_owned_tree_in_directory_v5,
    write_new_regular_in_directory_v5,
    write_regular_in_directory_v5,
)
from core.pit_optimizer_v5.workspace import (
    GitWorkspaceDriverV5,
    MaterializedWorkspaceV5,
    OwnedWorkspaceHandleV5,
    WorkspaceDriverBoundaryErrorV5,
    WorkspaceLeaseV5,
    WorkspaceOwnerV5,
    WorkspacePresenceV5,
    WorkspacePurposeV5,
    WorkspaceRemovalStateV5,
    WorkspaceRootHandleV5,
    WorkspaceRootsV5,
)


def _windows_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _canonical_link_free_directory(path: str) -> tuple[Path, os.stat_result]:
    candidate = Path(path)
    if not candidate.is_absolute() or "/" in path or ntpath.normpath(path) != path:
        raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
    try:
        resolved = candidate.resolve(strict=True)
        info = resolved.lstat()
    except OSError:
        raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
    if _windows_key(str(resolved)) != _windows_key(path) or not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
    current = resolved
    while True:
        try:
            if _is_reparse(current.lstat()):
                raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
        except OSError:
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
        if current.parent == current:
            break
        current = current.parent
    return resolved, info


def _closed_policy_path(relative_path: str) -> tuple[str, ...]:
    if relative_path not in EDITABLE_POLICY_PATHS_V5:
        raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or pure.as_posix() != relative_path or ".." in pure.parts:
        raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
    return pure.parts


def _hash_regular_file(path: Path) -> tuple[int, int, int, str]:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError:
        raise ValueError("Git executable is unavailable") from None
    if (
        resolved != path
        or resolved.name.casefold() not in {"git", "git.exe"}
        or not stat.S_ISREG(info.st_mode)
        or _is_reparse(info)
    ):
        raise ValueError("Git executable is not a canonical regular file")
    try:
        with acquire_absolute_directory_v5(resolved.parent) as parent:
            observed = hash_regular_in_directory_v5(
                parent,
                resolved.name,
                maximum_bytes=512 * 1024 * 1024,
            )
    except (OSError, ValueError):
        raise ValueError("Git executable could not be authenticated") from None
    if _metadata_identity(info) != observed[:2]:
        raise ValueError("Git executable changed during authentication")
    return observed


def _metadata_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_identity(path: Path, info: os.stat_result, *, lease_sha256: str) -> str:
    return canonical_sha256_v5(
        {
            "canonical_path_key": _windows_key(str(path)),
            "device": info.st_dev,
            "inode": info.st_ino,
            "lease_sha256": lease_sha256,
        }
    )


def _roots_overlap(first: str, second: str) -> bool:
    first_key = _windows_key(first)
    second_key = _windows_key(second)
    try:
        common = ntpath.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


_GIT_ENV_KEYS = frozenset({"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"})
_NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"
_GIT_FIXED_ARGS = (
    "--no-lazy-fetch",
    "--no-replace-objects",
    "-c",
    f"core.hooksPath={_NULL_DEVICE}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
    "-c",
    "pager.status=false",
)
_CREDENTIAL_COMPONENT = re.compile(
    r"(?:^|[._-])(?:secret|secrets|token|tokens|credential|credentials|private[_-]?key|api[_-]?key)(?:$|[._-])"
)


def _git_environment() -> dict[str, str]:
    allowed = {key.casefold(): key for key in _GIT_ENV_KEYS}
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        canonical = allowed.get(key.casefold())
        if canonical is None:
            continue
        if canonical in environment and environment[canonical] != value:
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        environment[canonical] = value
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": _NULL_DEVICE,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return environment


def _closed_tracked_path(raw: bytes) -> tuple[str, ...]:
    try:
        relative = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or pure.as_posix() != relative
        or "\\" in relative
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) or ":" in part for part in pure.parts)
    ):
        raise WorkspaceDriverBoundaryErrorV5("driver_failed")
    for index, component in enumerate(part.casefold() for part in pure.parts):
        public_example = index == len(pure.parts) - 1 and component in {
            ".env.example",
            ".env.template",
        }
        if (
            (not public_example and (component == ".env" or component.startswith(".env.")))
            or component in {"id_rsa", "id_ed25519", "credentials.json"}
            or component.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
            or _CREDENTIAL_COMPONENT.search(component) is not None
        ):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
    return pure.parts


@dataclass(frozen=True, slots=True)
class _RootCapabilityV5:
    driver_identity_sha256: str
    purpose: WorkspacePurposeV5
    path: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _OwnedWorkspaceCapabilityV5:
    driver_identity_sha256: str
    root_identity_sha256: str
    relative_path: str
    lease_sha256: str
    path: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseRecordV5:
    schema_version: Literal[5]
    driver_identity_sha256: str
    lease: WorkspaceLeaseV5

    def __post_init__(self) -> None:
        if self.schema_version != 5 or type(self.lease) is not WorkspaceLeaseV5:
            raise ValueError("workspace lease record is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceCreatedRecordV5:
    schema_version: Literal[5]
    driver_identity_sha256: str
    lease_id: str
    lease_sha256: str
    workspace_device: int
    workspace_inode: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.driver_identity_sha256) is None
            or not self.lease_id
            or re.fullmatch(r"[0-9a-f]{64}", self.lease_sha256) is None
            or type(self.workspace_device) is not int
            or type(self.workspace_inode) is not int
            or self.workspace_device < 0
            or self.workspace_inode <= 0
        ):
            raise ValueError("workspace created record is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseRetirementV5:
    schema_version: Literal[5]
    driver_identity_sha256: str
    lease_id: str
    lease_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or not re.fullmatch(r"[0-9a-f]{64}", self.driver_identity_sha256)
            or not self.lease_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.lease_sha256)
        ):
            raise ValueError("workspace lease retirement is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceReadyRecordV5:
    schema_version: Literal[5]
    driver_identity_sha256: str
    lease_id: str
    lease_sha256: str
    workspace_identity_sha256: str
    workspace_device: int
    workspace_inode: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or not re.fullmatch(r"[0-9a-f]{64}", self.driver_identity_sha256)
            or not self.lease_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.lease_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", self.workspace_identity_sha256)
            or type(self.workspace_device) is not int
            or type(self.workspace_inode) is not int
            or self.workspace_device < 0
            or self.workspace_inode <= 0
        ):
            raise ValueError("workspace ready record is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceLeaseStateV5:
    """Read-only authentication result for one journaled workspace lease."""

    lease: WorkspaceLeaseV5
    lifecycle: Literal["active", "removed", "retired"]
    workspace_identity_sha256: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.workspace_identity_sha256) is None
            or type(self.lease) is not WorkspaceLeaseV5
            or self.lifecycle
            not in {
                "active",
                "removed",
                "retired",
            }
        ):
            raise ValueError("workspace lease state is invalid")


class LocalGitWorkspaceDriverV5(GitWorkspaceDriverV5):
    """Exact concrete driver; Git and mutation occur only in runtime methods."""

    def __init__(
        self,
        *,
        roots: WorkspaceRootsV5,
        source_commit: str,
        git_executable: Path,
        repository: LocalArtifactRepositoryV5,
        owner: WorkspaceOwnerV5,
        export_mode: str = "full",
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("the concrete V5 Git workspace adapter requires Windows handle authority")
        if (
            type(roots) is not WorkspaceRootsV5
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
            or type(repository) is not LocalArtifactRepositoryV5
            or type(owner) is not WorkspaceOwnerV5
            or not isinstance(git_executable, Path)
            or not git_executable.is_absolute()
        ):
            raise ValueError("local Git workspace configuration is invalid")
        if export_mode not in {"full", "policy_only"}:
            raise ValueError("invalid workspace export mode")
        self._export_mode = export_mode
        executable_identity = _hash_regular_file(git_executable)
        source_path, source_info = _canonical_link_free_directory(roots.source_root)
        workspace_path, workspace_info = _canonical_link_free_directory(roots.workspace_root)
        if _roots_overlap(str(source_path), str(workspace_path)):
            raise ValueError("local Git workspace roots are not disjoint")
        self._roots = roots
        self._source_commit = source_commit
        self._git_executable = git_executable
        self._git_identity = executable_identity
        self._source_identity = _metadata_identity(source_info)
        self._workspace_identity = _metadata_identity(workspace_info)
        self._repository = repository
        self._owner = owner
        self._driver_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-local-git-workspace-v1",
                "roots": roots,
                "source_commit": source_commit,
                "git_executable_identity": executable_identity,
                "source_root": (_windows_key(str(source_path)), source_info.st_dev, source_info.st_ino),
                "workspace_root": (
                    _windows_key(str(workspace_path)),
                    workspace_info.st_dev,
                    workspace_info.st_ino,
                ),
                "repository_root_identity_sha256": repository.root_identity_sha256,
                "owner_sha256": owner.sha256,
                **(
                    {"export_mode": export_mode, "export_paths": EDITABLE_POLICY_PATHS_V5}
                    if export_mode == "policy_only"
                    else {}
                ),
            }
        )

    @property
    def export_mode(self) -> str:
        return self._export_mode

    @property
    def driver_identity_sha256(self) -> str:
        return self._driver_identity_sha256

    @property
    def roots(self) -> WorkspaceRootsV5:
        return self._roots

    @property
    def source_commit(self) -> str:
        return self._source_commit

    @property
    def repository(self) -> LocalArtifactRepositoryV5:
        return self._repository

    @property
    def owner(self) -> WorkspaceOwnerV5:
        return self._owner

    @property
    def git_executable(self) -> Path:
        return self._git_executable

    @property
    def root_identities(self) -> tuple[tuple[str, tuple[int, int]], tuple[str, tuple[int, int]]]:
        return (
            (self._roots.source_root, self._source_identity),
            (self._roots.workspace_root, self._workspace_identity),
        )

    def acquire_root(self, *, configured_path: str, purpose: WorkspacePurposeV5) -> WorkspaceRootHandleV5:
        expected = self._roots.source_root if purpose == "source" else self._roots.workspace_root
        if purpose not in {"source", "workspace"} or _windows_key(configured_path) != _windows_key(expected):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
        path, info = _canonical_link_free_directory(configured_path)
        identity = canonical_sha256_v5({"device": info.st_dev, "inode": info.st_ino})
        capability = _RootCapabilityV5(self.driver_identity_sha256, purpose, str(path), info.st_dev, info.st_ino)
        return WorkspaceRootHandleV5(
            purpose,
            configured_path,
            str(path),
            identity,
            hashlib.sha256(_windows_key(str(path)).encode("utf-8")).hexdigest(),
            True,
            capability,
        )

    def _root_capability(
        self,
        handle: WorkspaceRootHandleV5,
        purpose: WorkspacePurposeV5,
    ) -> _RootCapabilityV5:
        capability = handle.opaque_handle
        if type(handle) is not WorkspaceRootHandleV5 or type(capability) is not _RootCapabilityV5:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        if (
            capability.driver_identity_sha256 != self.driver_identity_sha256
            or capability.purpose != purpose
            or handle.purpose != purpose
            or _windows_key(handle.configured_path) != _windows_key(capability.path)
            or _windows_key(handle.canonical_path) != _windows_key(capability.path)
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        identity = canonical_sha256_v5({"device": capability.device, "inode": capability.inode})
        if (
            handle.root_identity_sha256 != identity
            or handle.canonical_path_sha256 != hashlib.sha256(_windows_key(capability.path).encode("utf-8")).hexdigest()
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return capability

    def _root(self, handle: WorkspaceRootHandleV5, purpose: WorkspacePurposeV5) -> Path:
        capability = self._root_capability(handle, purpose)
        try:
            with acquire_absolute_directory_v5(
                Path(capability.path),
                expected_identity=(capability.device, capability.inode),
            ):
                return Path(capability.path)
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def read_root_policy_file(self, *, root: WorkspaceRootHandleV5, relative_path: str) -> bytes:
        capability = self._root_capability(root, "source")
        parts = _closed_policy_path(relative_path)
        try:
            with acquire_directory_v5(
                Path(capability.path),
                parts[:-1],
                create=False,
                expected_root_identity=(capability.device, capability.inode),
            ) as parent:
                return read_regular_in_directory_v5(parent, parts[-1])[0]
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def _run_git(self, source: Path, *arguments: str, output_limit: int = 128 * 1024 * 1024) -> bytes:
        if (
            type(arguments) is not tuple
            or not arguments
            or any(type(item) is not str or not item or "\x00" in item for item in arguments)
            or type(output_limit) is not int
            or output_limit <= 0
        ):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        try:
            with (
                acquire_absolute_directory_v5(
                    source,
                    expected_identity=self._source_identity,
                ),
                acquire_absolute_directory_v5(self._git_executable.parent) as executable_parent,
            ):
                executable_stream, executable_info = open_regular_in_directory_v5(
                    executable_parent,
                    self._git_executable.name,
                    writable=False,
                )
                with executable_stream:
                    digest = hashlib.sha256()
                    while chunk := executable_stream.read(1024 * 1024):
                        digest.update(chunk)
                    observed = (
                        executable_info.st_dev,
                        executable_info.st_ino,
                        executable_info.st_size,
                        digest.hexdigest(),
                    )
                    if observed != self._git_identity:
                        raise ValueError("Git executable identity changed")
                    result = subprocess.run(
                        [str(self._git_executable), *_GIT_FIXED_ARGS, *arguments],
                        cwd=source,
                        env=_git_environment(),
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        timeout=60.0,
                        shell=False,
                    )
                    if result.returncode != 0 or len(result.stdout) > output_limit:
                        raise ValueError("Git operation failed")
                    executable_stream.seek(0)
                    after_digest = hashlib.sha256()
                    while chunk := executable_stream.read(1024 * 1024):
                        after_digest.update(chunk)
                    if after_digest.hexdigest() != self._git_identity[3]:
                        raise ValueError("Git executable identity changed")
                    return result.stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None

    def _export_commit(self, source: Path, target_root: Path, target_identity: tuple[int, int]) -> None:
        raw_tree = self._run_git(
            source,
            "ls-tree",
            "-rz",
            "-r",
            "--full-tree",
            self._source_commit,
            *(("--", *EDITABLE_POLICY_PATHS_V5) if self.export_mode == "policy_only" else ()),
            output_limit=32 * 1024 * 1024,
        )
        entries: list[tuple[tuple[str, ...], str]] = []
        for raw_entry in raw_tree.split(b"\0"):
            if not raw_entry:
                continue
            try:
                metadata, raw_path = raw_entry.split(b"\t", 1)
                mode, kind, _object_id = metadata.decode("ascii", errors="strict").split(" ")
            except (UnicodeError, ValueError):
                raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None
            parts = _closed_tracked_path(raw_path)
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise WorkspaceDriverBoundaryErrorV5("driver_failed")
            entries.append((parts, mode))
        paths = tuple("/".join(parts) for parts, _mode in entries)
        if len(set(paths)) != len(paths):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        if self.export_mode == "policy_only" and set(paths) != set(EDITABLE_POLICY_PATHS_V5):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        entries.sort(key=lambda item: "/".join(item[0]))
        owned_directories: dict[tuple[str, ...], tuple[int, int]] = {(): target_identity}
        for parts, _mode in entries:
            relative = "/".join(parts)
            content = self._run_git(
                source,
                "show",
                f"{self._source_commit}:{relative}",
            )
            try:
                with ExitStack() as stack:
                    parent = stack.enter_context(
                        acquire_absolute_directory_v5(
                            target_root,
                            expected_identity=target_identity,
                        )
                    )
                    prefix: tuple[str, ...] = ()
                    for part in parts[:-1]:
                        child_prefix = (*prefix, part)
                        expected = owned_directories.get(child_prefix)
                        if expected is None:
                            child = create_directory_in_directory_v5(parent, part)
                            owned_directories[child_prefix] = child.identity
                        else:
                            child = acquire_directory_v5(
                                parent.path,
                                (part,),
                                create=False,
                                expected_root_identity=parent.identity,
                            )
                            if child.identity != expected:
                                child.close()
                                raise ValueError("workspace export directory identity changed")
                        parent = stack.enter_context(child)
                        prefix = child_prefix
                    write_new_regular_in_directory_v5(parent, parts[-1], content)
            except (OSError, ValueError):
                raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None

    def _record(self, lease: WorkspaceLeaseV5) -> WorkspaceLeaseRecordV5:
        return WorkspaceLeaseRecordV5(5, self.driver_identity_sha256, lease)

    def create_disposable_workspace(
        self,
        *,
        source_root: WorkspaceRootHandleV5,
        workspace_root: WorkspaceRootHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> OwnedWorkspaceHandleV5:
        source_capability = self._root_capability(source_root, "source")
        destination_capability = self._root_capability(workspace_root, "workspace")
        source = Path(source_capability.path)
        destination_root = Path(destination_capability.path)
        if lease.workspace_root_identity_sha256 != workspace_root.root_identity_sha256:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = lease.lease_token_sha256
        with self._repository.adapter_state_transition(namespace="workspace", key=key):
            registered = self._active_record_unlocked(lease.payload.lease_id)
            if registered is None:
                if self._retirement(key) is not None:
                    raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
                self._repository.append_typed_state(
                    namespace="workspace-reservation",
                    key=key,
                    value=self._record(lease),
                )
            elif registered.lease != lease:
                raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
            created = self._created(key)
            ready = self._ready(key)
            if ready is not None:
                if (
                    created is None
                    or created.lease_id != lease.payload.lease_id
                    or created.lease_sha256 != lease.sha256
                    or (created.workspace_device, created.workspace_inode)
                    != (ready.workspace_device, ready.workspace_inode)
                ):
                    raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
                opened = self._open_owned_unlocked(
                    workspace_root=workspace_root,
                    lease=lease,
                    require_ready=True,
                )
                if opened is None:
                    raise WorkspaceDriverBoundaryErrorV5("driver_failed")
                return opened
            created_identity: tuple[int, int] | None = None
            created_durable: bool | None = created is not None
            try:
                with ExitStack() as stack:
                    destination_access = stack.enter_context(
                        acquire_absolute_directory_v5(
                            destination_root,
                            expected_identity=(
                                destination_capability.device,
                                destination_capability.inode,
                            ),
                        )
                    )
                    if created is None:
                        try:
                            target_access = stack.enter_context(
                                create_directory_in_directory_v5(
                                    destination_access,
                                    lease.workspace_relative_path,
                                )
                            )
                        except FileExistsError:
                            raise WorkspaceDriverBoundaryErrorV5("foreign_lease") from None
                        created_identity = target_access.identity
                        expected_created = WorkspaceCreatedRecordV5(
                            5,
                            self.driver_identity_sha256,
                            lease.payload.lease_id,
                            lease.sha256,
                            target_access.identity[0],
                            target_access.identity[1],
                        )
                        try:
                            self._repository.append_typed_state(
                                namespace="workspace-created",
                                key=key,
                                value=expected_created,
                            )
                        except BaseException:
                            try:
                                observed_created = self._created(key)
                            except BaseException:
                                created_durable = None
                            else:
                                created_durable = observed_created == expected_created
                            raise
                        created = expected_created
                        created_durable = True
                    else:
                        if created.lease_id != lease.payload.lease_id or created.lease_sha256 != lease.sha256:
                            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
                        target_access = stack.enter_context(
                            acquire_directory_v5(
                                destination_access.path,
                                (lease.workspace_relative_path,),
                                create=False,
                                expected_root_identity=destination_access.identity,
                            )
                        )
                        if target_access.identity != (
                            created.workspace_device,
                            created.workspace_inode,
                        ):
                            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
                    if created_identity is None:
                        clear_owned_directory_v5(target_access)
                    elif not directory_is_empty_v5(target_access):
                        raise WorkspaceDriverBoundaryErrorV5("driver_failed")
                    self._export_commit(source, target_access.path, target_access.identity)
                    target_path = target_access.path
                    target_info = target_path.lstat()
                    if _metadata_identity(target_info) != target_access.identity:
                        raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
                    identity = _directory_identity(
                        target_path,
                        target_info,
                        lease_sha256=lease.sha256,
                    )
                    self._repository.append_typed_state(
                        namespace="workspace-ready",
                        key=key,
                        value=WorkspaceReadyRecordV5(
                            5,
                            self.driver_identity_sha256,
                            lease.payload.lease_id,
                            lease.sha256,
                            identity,
                            target_access.identity[0],
                            target_access.identity[1],
                        ),
                    )
                opened = self._open_owned_unlocked(
                    workspace_root=workspace_root,
                    lease=lease,
                    require_ready=True,
                )
                if opened is None:
                    raise WorkspaceDriverBoundaryErrorV5("driver_failed")
                return opened
            except BaseException:
                rollback_failed = False
                if created_identity is not None and created_durable is False:
                    try:
                        with acquire_absolute_directory_v5(
                            destination_root,
                            expected_identity=(
                                destination_capability.device,
                                destination_capability.inode,
                            ),
                        ) as destination_access:
                            self._remove_exact_tree(
                                destination_access,
                                lease.workspace_relative_path,
                                created_identity,
                            )
                    except (OSError, ValueError, WorkspaceDriverBoundaryErrorV5):
                        rollback_failed = True
                if rollback_failed:
                    raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None
                raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None

    def _retirement(
        self,
        key: str,
        *,
        repair: bool = True,
    ) -> WorkspaceLeaseRetirementV5 | None:
        retirement = self._repository.load_typed_state(
            namespace="workspace-retirement",
            key=key,
            value_type=WorkspaceLeaseRetirementV5,
            repair=repair,
        )
        if retirement is not None and retirement.driver_identity_sha256 != self.driver_identity_sha256:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return retirement

    def _ready(
        self,
        key: str,
        *,
        repair: bool = True,
    ) -> WorkspaceReadyRecordV5 | None:
        ready = self._repository.load_typed_state(
            namespace="workspace-ready",
            key=key,
            value_type=WorkspaceReadyRecordV5,
            repair=repair,
        )
        if ready is not None and ready.driver_identity_sha256 != self.driver_identity_sha256:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return ready

    def _created(
        self,
        key: str,
        *,
        repair: bool = True,
    ) -> WorkspaceCreatedRecordV5 | None:
        created = self._repository.load_typed_state(
            namespace="workspace-created",
            key=key,
            value_type=WorkspaceCreatedRecordV5,
            repair=repair,
        )
        if created is not None and created.driver_identity_sha256 != self.driver_identity_sha256:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return created

    def _active_record_unlocked(self, lease_id: str) -> WorkspaceLeaseRecordV5 | None:
        if not lease_id.startswith("workspace."):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = lease_id.removeprefix("workspace.")
        record = self._repository.load_typed_state(
            namespace="workspace-reservation",
            key=key,
            value_type=WorkspaceLeaseRecordV5,
        )
        retirement = self._retirement(key)
        if record is None:
            return None
        if record.driver_identity_sha256 != self.driver_identity_sha256:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        if retirement is not None:
            if retirement.lease_id != lease_id or retirement.lease_sha256 != record.lease.sha256:
                raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
            return None
        return record

    def _active_record(self, lease_id: str) -> WorkspaceLeaseRecordV5 | None:
        if not lease_id.startswith("workspace."):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = lease_id.removeprefix("workspace.")
        with self._repository.adapter_state_transition(namespace="workspace", key=key):
            return self._active_record_unlocked(lease_id)

    def load_lease(self, lease_id: str) -> WorkspaceLeaseV5 | None:
        record = self._active_record(lease_id)
        return None if record is None else record.lease

    def authenticate_lease_history(
        self,
        payload: ResourceLeasePayloadV5,
    ) -> WorkspaceLeaseStateV5:
        """Authenticate reservation/readiness/retirement without locks or mutation."""

        return self._authenticate_lease_history(payload, recover_retirement_index=False)

    def recover_lease_history(self, payload: ResourceLeasePayloadV5) -> WorkspaceLeaseStateV5:
        """Operational cleanup recovery may repair only an exact orphan retirement index."""

        if (
            type(payload) is not ResourceLeasePayloadV5
            or re.fullmatch(
                r"workspace\.[0-9a-f]{64}",
                payload.lease_id,
            )
            is None
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = payload.lease_id.removeprefix("workspace.")
        with self._repository.adapter_state_transition(namespace="workspace", key=key):
            return self._authenticate_lease_history(payload, recover_retirement_index=True)

    def _authenticate_lease_history(
        self,
        payload: ResourceLeasePayloadV5,
        *,
        recover_retirement_index: bool,
    ) -> WorkspaceLeaseStateV5:
        if (
            type(payload) is not ResourceLeasePayloadV5
            or payload.resource_kind != "workspace"
            or not payload.lease_id.startswith("workspace.")
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = payload.lease_id.removeprefix("workspace.")
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        record = self._repository.load_typed_state(
            namespace="workspace-reservation",
            key=key,
            value_type=WorkspaceLeaseRecordV5,
            repair=False,
        )
        created = self._created(key, repair=False)
        ready = self._ready(key, repair=False)
        if record is None:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        lease = record.lease
        if (
            record.driver_identity_sha256 != self.driver_identity_sha256
            or lease.payload != payload
            or lease.owner != self._owner
            or lease.lease_token_sha256 != key
            or lease.workspace_root_identity_sha256
            != canonical_sha256_v5(
                {
                    "device": self._workspace_identity[0],
                    "inode": self._workspace_identity[1],
                }
            )
            or lease.workspace_root_path_sha256
            != hashlib.sha256(_windows_key(self._roots.workspace_root).encode("utf-8")).hexdigest()
            or created is None
            or ready is None
            or created.lease_id != payload.lease_id
            or created.lease_sha256 != lease.sha256
            or ready.lease_id != payload.lease_id
            or ready.lease_sha256 != lease.sha256
            or (created.workspace_device, created.workspace_inode) != (ready.workspace_device, ready.workspace_inode)
            or ready.workspace_identity_sha256
            != canonical_sha256_v5(
                {
                    "canonical_path_key": _windows_key(
                        str(Path(self._roots.workspace_root) / lease.workspace_relative_path)
                    ),
                    "device": created.workspace_device,
                    "inode": created.workspace_inode,
                    "lease_sha256": lease.sha256,
                }
            )
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        try:
            retirement = self._retirement(key, repair=False)
        except ArtifactMissingV5 as exc:
            expected = WorkspaceLeaseRetirementV5(5, self.driver_identity_sha256, payload.lease_id, lease.sha256)
            expected_authority = AdapterStateAuthorityV5(
                5,
                "workspace-retirement",
                key,
                ArtifactRefV5(f"adapter-state/workspace-retirement/{key}.json", canonical_sha256_v5(expected)),
            )
            missing_index = ArtifactRefV5(
                f"adapter-state-authority/workspace-retirement/{key}.json",
                canonical_sha256_v5(expected_authority),
            )
            if (
                not recover_retirement_index
                or exc.reference != missing_index
                or not self._workspace_child_absent(lease)
            ):
                raise
            retirement = self._repository.recover_typed_state_index(
                namespace="workspace-retirement",
                key=key,
                expected_value=expected,
            )
        if retirement is None:
            if self._workspace_child_absent(lease):
                return WorkspaceLeaseStateV5(lease, "removed", ready.workspace_identity_sha256)
            try:
                with (
                    acquire_absolute_directory_v5(
                        Path(self._roots.workspace_root),
                        expected_identity=self._workspace_identity,
                    ) as root,
                    acquire_directory_v5(
                        root.path,
                        (lease.workspace_relative_path,),
                        create=False,
                        expected_root_identity=root.identity,
                    ) as target,
                ):
                    info = target.path.lstat()
                    if (
                        target.identity != (created.workspace_device, created.workspace_inode)
                        or _metadata_identity(info) != target.identity
                        or _directory_identity(
                            target.path,
                            info,
                            lease_sha256=lease.sha256,
                        )
                        != ready.workspace_identity_sha256
                    ):
                        raise ValueError("workspace identity changed")
            except (OSError, ValueError):
                raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
            return WorkspaceLeaseStateV5(lease, "active", ready.workspace_identity_sha256)
        if (
            retirement.lease_id != payload.lease_id
            or retirement.lease_sha256 != lease.sha256
            or not self._workspace_child_absent(lease)
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return WorkspaceLeaseStateV5(lease, "retired", ready.workspace_identity_sha256)

    def open_owned_workspace(
        self,
        *,
        workspace_root: WorkspaceRootHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> OwnedWorkspaceHandleV5 | None:
        with self._repository.adapter_state_transition(
            namespace="workspace",
            key=lease.lease_token_sha256,
        ):
            return self._open_owned_unlocked(
                workspace_root=workspace_root,
                lease=lease,
                require_ready=False,
            )

    def _open_owned_unlocked(
        self,
        *,
        workspace_root: WorkspaceRootHandleV5,
        lease: WorkspaceLeaseV5,
        require_ready: bool,
    ) -> OwnedWorkspaceHandleV5 | None:
        root_capability = self._root_capability(workspace_root, "workspace")
        root = Path(root_capability.path)
        registered = self._active_record_unlocked(lease.payload.lease_id)
        if registered is None or registered.lease != lease:
            return None
        created = self._created(lease.lease_token_sha256)
        ready = self._ready(lease.lease_token_sha256)
        if created is None:
            if ready is not None:
                raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
            return None
        if (
            created.lease_id != lease.payload.lease_id
            or created.lease_sha256 != lease.sha256
            or ready is not None
            and (created.workspace_device, created.workspace_inode) != (ready.workspace_device, ready.workspace_inode)
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        try:
            with acquire_absolute_directory_v5(
                root,
                expected_identity=(root_capability.device, root_capability.inode),
            ) as root_access:
                if directory_child_absent_v5(root_access, lease.workspace_relative_path):
                    return None
                with acquire_directory_v5(
                    root_access.path,
                    (lease.workspace_relative_path,),
                    create=False,
                    expected_root_identity=root_access.identity,
                ) as target_access:
                    target = target_access.path
                    info = target.lstat()
                    if (
                        _metadata_identity(info) != target_access.identity
                        or target_access.identity != (created.workspace_device, created.workspace_inode)
                        or target.parent != root
                        or (
                            ready is not None
                            and target_access.identity != (ready.workspace_device, ready.workspace_inode)
                        )
                    ):
                        raise ValueError("workspace target identity changed")
                    identity = _directory_identity(target, info, lease_sha256=lease.sha256)
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
        if require_ready and ready is None:
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        if ready is not None and (
            ready.lease_id != lease.payload.lease_id
            or ready.lease_sha256 != lease.sha256
            or ready.workspace_identity_sha256 != identity
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        capability = _OwnedWorkspaceCapabilityV5(
            self.driver_identity_sha256,
            workspace_root.root_identity_sha256,
            lease.workspace_relative_path,
            lease.sha256,
            str(target),
            info.st_dev,
            info.st_ino,
        )
        return OwnedWorkspaceHandleV5(
            workspace_root.root_identity_sha256,
            workspace_root.canonical_path_sha256,
            lease.workspace_relative_path,
            lease.payload.lease_id,
            lease.lease_token_sha256,
            identity,
            capability,
        )

    def _owned(
        self,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
        *,
        require_ready: bool = True,
    ) -> Path:
        if self.load_lease(lease.payload.lease_id) != lease:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return self._owned_capability(workspace, lease, require_ready=require_ready)

    def _workspace_child_absent(self, lease: WorkspaceLeaseV5) -> bool:
        try:
            with acquire_absolute_directory_v5(
                Path(self._roots.workspace_root),
                expected_identity=self._workspace_identity,
            ) as root:
                return directory_child_absent_v5(root, lease.workspace_relative_path)
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def _owned_capability(
        self,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
        *,
        require_ready: bool = True,
    ) -> Path:
        capability = workspace.opaque_handle
        created = self._created(lease.lease_token_sha256)
        ready = self._ready(lease.lease_token_sha256)
        if (
            created is None
            or require_ready
            and ready is None
            or type(capability) is not _OwnedWorkspaceCapabilityV5
            or capability.driver_identity_sha256 != self.driver_identity_sha256
            or capability.lease_sha256 != lease.sha256
            or workspace.workspace_root_identity_sha256 != capability.root_identity_sha256
            or workspace.workspace_relative_path != capability.relative_path
            or workspace.lease_id != lease.payload.lease_id
            or workspace.lease_token_sha256 != lease.lease_token_sha256
            or (capability.device, capability.inode) != (created.workspace_device, created.workspace_inode)
            or ready is not None
            and (capability.device, capability.inode) != (ready.workspace_device, ready.workspace_inode)
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        try:
            with acquire_absolute_directory_v5(
                Path(capability.path),
                expected_identity=(capability.device, capability.inode),
            ) as target_access:
                info = target_access.path.lstat()
                if (
                    _metadata_identity(info) != target_access.identity
                    or _directory_identity(target_access.path, info, lease_sha256=lease.sha256)
                    != workspace.handle_identity_sha256
                ):
                    raise ValueError("workspace identity changed")
                return target_access.path
        except (OSError, ValueError):
            try:
                absent = self._workspace_child_absent(lease)
            except WorkspaceDriverBoundaryErrorV5:
                absent = False
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path" if absent else "foreign_lease") from None

    def _acquire_owned_directory(
        self,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
    ):
        capability = workspace.opaque_handle
        target = self._owned_capability(workspace, lease)
        assert type(capability) is _OwnedWorkspaceCapabilityV5
        try:
            access = acquire_absolute_directory_v5(
                target,
                expected_identity=(capability.device, capability.inode),
            )
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
        return access

    def workspace_presence(self, *, workspace: OwnedWorkspaceHandleV5, lease: WorkspaceLeaseV5) -> WorkspacePresenceV5:
        try:
            self._owned(workspace, lease, require_ready=False)
        except WorkspaceDriverBoundaryErrorV5 as exc:
            if exc.code == "unsafe_path" and self._workspace_child_absent(lease):
                return "absent"
            raise
        return "present"

    def read_workspace_policy_file(self, *, workspace: OwnedWorkspaceHandleV5, relative_path: str) -> bytes:
        record = self.load_lease(workspace.lease_id)
        if record is None:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        parts = _closed_policy_path(relative_path)
        try:
            with (
                self._acquire_owned_directory(workspace, record) as root,
                acquire_directory_v5(
                    root.path,
                    parts[:-1],
                    create=False,
                    expected_root_identity=root.identity,
                ) as parent,
            ):
                return read_regular_in_directory_v5(parent, parts[-1])[0]
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def write_workspace_policy_file(
        self,
        *,
        workspace: OwnedWorkspaceHandleV5,
        relative_path: str,
        content: bytes,
    ) -> None:
        if type(content) is not bytes:
            raise WorkspaceDriverBoundaryErrorV5("driver_failed")
        record = self.load_lease(workspace.lease_id)
        if record is None:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        parts = _closed_policy_path(relative_path)
        try:
            with (
                self._acquire_owned_directory(workspace, record) as root,
                acquire_directory_v5(
                    root.path,
                    parts[:-1],
                    create=False,
                    expected_root_identity=root.identity,
                ) as parent,
            ):
                write_regular_in_directory_v5(parent, parts[-1], content)
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def controller_lease_is_live(self, owner: WorkspaceOwnerV5) -> bool:
        if type(owner) is not WorkspaceOwnerV5:
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        return owner == self._owner

    def authorize_materialized_source(
        self,
        materialized: MaterializedWorkspaceV5,
    ) -> tuple[Path, str, tuple[int, int]]:
        """Return the exact root only to the concrete mount factory after reauthentication."""

        if (
            type(materialized) is not MaterializedWorkspaceV5
            or materialized.lease.owner != self._owner
            or materialized.lease.child_revision_sha256 != materialized.variant.policy_revision.sha256
        ):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        try:
            with self._acquire_owned_directory(
                materialized.workspace_handle,
                materialized.lease,
            ) as root:
                for source_file in materialized.variant.source_bundle.files:
                    parts = _closed_policy_path(source_file.path)
                    with acquire_directory_v5(
                        root.path,
                        parts[:-1],
                        create=False,
                        expected_root_identity=root.identity,
                    ) as parent:
                        observed = read_regular_in_directory_v5(parent, parts[-1])[0]
                    if hashlib.sha256(observed).hexdigest() != source_file.sha256:
                        raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
                return (
                    root.path,
                    materialized.workspace_handle.handle_identity_sha256,
                    root.identity,
                )
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None

    def remove_workspace_no_follow(
        self,
        *,
        workspace_root: WorkspaceRootHandleV5,
        workspace: OwnedWorkspaceHandleV5,
        lease: WorkspaceLeaseV5,
    ) -> WorkspaceRemovalStateV5:
        root_capability = self._root_capability(workspace_root, "workspace")
        root = Path(root_capability.path)
        key = lease.lease_token_sha256
        with self._repository.adapter_state_transition(namespace="workspace", key=key):
            registered = self._active_record_unlocked(lease.payload.lease_id)
            if registered is None or registered.lease != lease:
                raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
            created = self._created(key)
            ready = self._ready(key)
            capability = workspace.opaque_handle
            if (
                created is None
                or type(capability) is not _OwnedWorkspaceCapabilityV5
                or capability.driver_identity_sha256 != self.driver_identity_sha256
                or capability.lease_sha256 != lease.sha256
                or workspace.workspace_root_identity_sha256 != capability.root_identity_sha256
                or workspace.workspace_relative_path != capability.relative_path
                or workspace.lease_id != lease.payload.lease_id
                or workspace.lease_token_sha256 != lease.lease_token_sha256
                or (capability.device, capability.inode) != (created.workspace_device, created.workspace_inode)
                or ready is not None
                and (capability.device, capability.inode) != (ready.workspace_device, ready.workspace_inode)
            ):
                raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
            target = Path(capability.path)
            if target.parent != root or target.name != lease.workspace_relative_path:
                raise WorkspaceDriverBoundaryErrorV5("unsafe_path")
            try:
                with acquire_absolute_directory_v5(
                    root,
                    expected_identity=(root_capability.device, root_capability.inode),
                ) as root_access:
                    removed = self._remove_exact_tree(
                        root_access,
                        lease.workspace_relative_path,
                        (capability.device, capability.inode),
                    )
            except (OSError, ValueError):
                raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
            return "removed" if removed else "absent"

    @staticmethod
    def _remove_exact_tree(
        parent: object,
        relative_path: str,
        expected_identity: tuple[int, int],
    ) -> bool:
        try:
            return remove_owned_tree_in_directory_v5(
                parent,  # type: ignore[arg-type]
                relative_path,
                expected_identity=expected_identity,
            )
        except (OSError, ValueError):
            raise WorkspaceDriverBoundaryErrorV5("driver_failed") from None

    def retire_lease(self, lease_id: str) -> None:
        if not lease_id.startswith("workspace."):
            raise WorkspaceDriverBoundaryErrorV5("foreign_lease")
        key = lease_id.removeprefix("workspace.")
        with self._repository.adapter_state_transition(namespace="workspace", key=key):
            record = self._active_record_unlocked(lease_id)
            if record is None:
                return
            try:
                with acquire_absolute_directory_v5(
                    Path(self._roots.workspace_root),
                    expected_identity=self._workspace_identity,
                ) as root:
                    if not directory_child_absent_v5(
                        root,
                        record.lease.workspace_relative_path,
                    ):
                        raise WorkspaceDriverBoundaryErrorV5("driver_failed")
            except (OSError, ValueError):
                raise WorkspaceDriverBoundaryErrorV5("unsafe_path") from None
            self._repository.append_typed_state(
                namespace="workspace-retirement",
                key=key,
                value=WorkspaceLeaseRetirementV5(
                    5,
                    self.driver_identity_sha256,
                    lease_id,
                    record.lease.sha256,
                ),
            )


__all__ = [
    "LocalGitWorkspaceDriverV5",
    "WorkspaceCreatedRecordV5",
    "WorkspaceLeaseRecordV5",
    "WorkspaceLeaseRetirementV5",
    "WorkspaceLeaseStateV5",
    "WorkspaceReadyRecordV5",
]
