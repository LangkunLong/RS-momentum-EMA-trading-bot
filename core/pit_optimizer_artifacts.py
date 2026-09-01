"""Crash-safe, incremental local artifacts for the schema-v3 PIT optimizer."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Mapping


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Return the one canonical UTF-8 representation used by optimizer artifacts."""

    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_all(handle: object, payload: bytes) -> None:
    written = handle.write(payload)  # type: ignore[attr-defined]
    if written != len(payload):
        raise OSError("optimizer artifact write was incomplete")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _windows_extended_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\" + value


def _open_windows_directory_locked(path: Path) -> tuple[int, tuple[int, int]]:
    """Open a non-reparse directory without delete sharing and bind its identity."""

    import ctypes
    from ctypes import wintypes

    before = os.lstat(path)
    if _is_link_or_reparse(path) or not stat.S_ISDIR(before.st_mode):
        raise ValueError("optimizer artifact parent is a link or reparse point")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _windows_extended_path(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0001 | 0x0002,  # FILE_SHARE_READ | FILE_SHARE_WRITE; no delete share
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        after = os.lstat(path)
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISDIR(after.st_mode)
            or _metadata_identity(before) != _metadata_identity(after)
        ):
            raise ValueError("optimizer artifact parent changed while opening")
    except BaseException:
        _close_windows_handle(int(handle))
        raise
    return int(handle), _metadata_identity(after)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _posix_directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise RuntimeError("secure component-relative artifact opens are unavailable")
    return os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)


class _DirectoryAccess:
    """Held directory capabilities for one verified absolute component chain."""

    def __init__(
        self,
        *,
        path: Path,
        identity: tuple[int, int],
        posix_descriptors: list[int] | None = None,
        windows_handles: list[int] | None = None,
    ) -> None:
        self.path = path
        self.identity = identity
        self._posix_descriptors = posix_descriptors or []
        self._windows_handles = windows_handles or []
        self._closed = False

    @property
    def descriptor(self) -> int:
        if os.name == "nt" or not self._posix_descriptors:
            raise RuntimeError("component-relative directory descriptor is unavailable")
        return self._posix_descriptors[-1]

    def append_posix(self, path: Path, descriptor: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.identity = identity
        self._posix_descriptors.append(descriptor)

    def append_windows(self, path: Path, handle: int, identity: tuple[int, int]) -> None:
        self.path = path
        self.identity = identity
        self._windows_handles.append(handle)

    def assert_current(self) -> None:
        try:
            current = _acquire_absolute_directory(self.path)
        except (OSError, ValueError) as exc:
            raise ValueError("optimizer artifact parent changed before mutation") from exc
        with current:
            if current.identity != self.identity:
                raise ValueError("optimizer artifact parent changed before mutation")

    def entry_exists(self, name: str) -> bool:
        try:
            if os.name == "nt":
                metadata = os.lstat(self.path / name)
            else:
                metadata = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
            raise ValueError("optimizer artifact target is a link or reparse point")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("optimizer artifact target is not a regular file")
        return True

    def unlink(self, name: str) -> None:
        if os.name == "nt":
            (self.path / name).unlink(missing_ok=True)
            return
        try:
            os.unlink(name, dir_fd=self.descriptor)
        except FileNotFoundError:
            pass

    def replace(self, source: str, target: str) -> None:
        if os.name == "nt":
            os.replace(self.path / source, self.path / target)
            return
        os.replace(
            source,
            target,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[OSError] = []
        for descriptor in reversed(self._posix_descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                failures.append(exc)
        for handle in reversed(self._windows_handles):
            try:
                _close_windows_handle(handle)
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def __enter__(self) -> "_DirectoryAccess":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_posix_component(
    parent_descriptor: int,
    part: str,
) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(
            part,
            _posix_directory_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
            raise ValueError("optimizer artifact parent is a link or invalid") from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("optimizer artifact parent is invalid")
    return descriptor, _metadata_identity(metadata)


def _acquire_absolute_directory(path: Path) -> _DirectoryAccess:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("optimizer artifact parent is not absolute")
    parts = candidate.parts
    if not parts:
        raise ValueError("optimizer artifact parent is invalid")
    current = Path(parts[0])
    if os.name == "nt":
        handles: list[int] = []
        try:
            handle, identity = _open_windows_directory_locked(current)
            handles.append(handle)
            access = _DirectoryAccess(
                path=current,
                identity=identity,
                windows_handles=handles,
            )
            for part in parts[1:]:
                child = current / part
                handle, identity = _open_windows_directory_locked(child)
                access.append_windows(child, handle, identity)
                current = child
            return access
        except BaseException:
            for handle in reversed(handles):
                try:
                    _close_windows_handle(handle)
                except OSError:
                    pass
            raise

    descriptors: list[int] = []
    try:
        descriptor = os.open(current, _posix_directory_flags())
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        access = _DirectoryAccess(
            path=current,
            identity=_metadata_identity(metadata),
            posix_descriptors=descriptors,
        )
        for part in parts[1:]:
            descriptor, identity = _open_posix_component(access.descriptor, part)
            child = current / part
            access.append_posix(child, descriptor, identity)
            current = child
        return access
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _sync_directory_access(directory: _DirectoryAccess) -> None:
    if os.name != "nt":
        os.fsync(directory.descriptor)
    # Preserve the existing injectable durability boundary.  The held
    # descriptor/handle above is authoritative; this call is redundant on
    # POSIX and intentionally a no-op on Windows unless a test injects failure.
    _fsync_directory(directory.path)


def _extend_directory(
    access: _DirectoryAccess,
    part: str,
    *,
    create: bool,
) -> None:
    child = access.path / part
    if os.name == "nt":
        try:
            handle, identity = _open_windows_directory_locked(child)
        except FileNotFoundError:
            if not create:
                raise ValueError("optimizer artifact parent is absent") from None
            try:
                child.mkdir()
            except FileExistsError:
                pass
            handle, identity = _open_windows_directory_locked(child)
            _sync_directory_access(access)
        access.append_windows(child, handle, identity)
        return

    try:
        descriptor, identity = _open_posix_component(access.descriptor, part)
    except FileNotFoundError:
        if not create:
            raise ValueError("optimizer artifact parent is absent") from None
        try:
            os.mkdir(part, mode=0o700, dir_fd=access.descriptor)
        except FileExistsError:
            pass
        descriptor, identity = _open_posix_component(access.descriptor, part)
        _sync_directory_access(access)
    access.append_posix(child, descriptor, identity)


def _acquire_directory(
    root: Path,
    parts: tuple[str, ...],
    *,
    create: bool,
    expected_root_identity: tuple[int, int] | None,
) -> _DirectoryAccess:
    access = _acquire_absolute_directory(root)
    try:
        if expected_root_identity is not None and access.identity != expected_root_identity:
            raise ValueError("optimizer artifact root identity changed")
        for part in parts:
            _extend_directory(access, part, create=create)
        return access
    except BaseException:
        access.close()
        raise


def _open_exclusive_file(directory: _DirectoryAccess, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, flags, 0o600, dir_fd=directory.descriptor)
    return os.open(directory.path / name, flags, 0o600)


def _write_create_only_in_directory(
    directory: _DirectoryAccess,
    name: str,
    payload: bytes,
) -> tuple[Path, str]:
    directory.assert_current()
    descriptor = _open_exclusive_file(directory, name)
    created = True
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory.assert_current()
        _sync_directory_access(directory)
        directory.assert_current()
    except BaseException:
        if created:
            try:
                directory.unlink(name)
                _sync_directory_access(directory)
            except OSError:
                pass
        raise
    return directory.path / name, hashlib.sha256(payload).hexdigest()


def _open_temporary_file(
    directory: _DirectoryAccess,
    target_name: str,
) -> tuple[int, str]:
    if os.name == "nt":
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target_name}.",
            suffix=".tmp",
            dir=directory.path,
        )
        return descriptor, Path(temporary_name).name
    for _attempt in range(128):
        name = f".{target_name}.{secrets.token_hex(12)}.tmp"
        try:
            return _open_exclusive_file(directory, name), name
        except FileExistsError:
            continue
    raise FileExistsError("optimizer artifact temporary name space is exhausted")


def _atomic_replace_in_directory(
    directory: _DirectoryAccess,
    name: str,
    payload: bytes,
) -> tuple[Path, str]:
    directory.assert_current()
    descriptor, temporary_name = _open_temporary_file(directory, name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
        directory.assert_current()
        directory.replace(temporary_name, name)
        directory.assert_current()
        _sync_directory_access(directory)
        directory.assert_current()
    finally:
        try:
            directory.unlink(temporary_name)
        except OSError:
            pass
    return directory.path / name, hashlib.sha256(payload).hexdigest()


def write_create_only_bytes(path: Path, payload: bytes) -> tuple[Path, str]:
    """Create and fsync one artifact without permitting an overwrite."""

    target = Path(path)
    if not target.is_absolute():
        raise ValueError("optimizer artifact target is invalid")
    with _acquire_absolute_directory(target.parent) as directory:
        return _write_create_only_in_directory(directory, target.name, payload)


def write_create_only_json(
    path: Path,
    value: Mapping[str, object],
) -> tuple[Path, str]:
    return write_create_only_bytes(Path(path), canonical_json_bytes(value))


def atomic_replace_bytes(path: Path, payload: bytes) -> tuple[Path, str]:
    """Fsync a sibling temporary file before atomically replacing an artifact."""

    target = Path(path)
    if not target.is_absolute():
        raise ValueError("optimizer artifact replacement target is invalid")
    with _acquire_absolute_directory(target.parent) as directory:
        return _atomic_replace_in_directory(directory, target.name, payload)


def atomic_replace_json(
    path: Path,
    value: Mapping[str, object],
) -> tuple[Path, str]:
    return atomic_replace_bytes(Path(path), canonical_json_bytes(value))


class IncrementalArtifactStore:
    """Bounded create-only evidence with two explicit replaceable snapshots."""

    _REPLACEABLE_JSON = frozenset({"accounting.json"})
    _REPLACEABLE_DIFF = frozenset({"incumbent.diff"})
    _ROOT_JSON = frozenset(
        {"run.json", "baseline.json", "accounting.json", "holdout.json", "summary.json"}
    )
    _ITERATION_JSON = frozenset(
        {"investigator.json", "author.json", "validation.json", "discovery.json", "critic.json", "decision.json"}
    )
    _ROLE_OUTPUT_INVALID_JSON = frozenset(
        {
            "investigator_output_invalid.json",
            "author_output_invalid.json",
            "critic_output_invalid.json",
        }
    )
    _PLAN_SKIP_JSON = "authorization_skips.json"

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise ValueError("optimizer artifact root is invalid")
        candidate = Path(os.path.abspath(candidate))
        try:
            with _acquire_absolute_directory(candidate) as access:
                self._root_identity = access.identity
        except (OSError, ValueError) as exc:
            raise ValueError("optimizer artifact root is invalid") from exc
        self._root = candidate

    @property
    def root(self) -> Path:
        return self._root

    def _secure_directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> _DirectoryAccess:
        return _acquire_directory(
            self._root,
            parts,
            create=create,
            expected_root_identity=self._root_identity,
        )

    def prepare_iteration(self, iteration: int) -> Path:
        """Create and sync the numbered directory before any role authority is used."""

        if type(iteration) is not int or not 1 <= iteration <= 999:
            raise ValueError("optimizer iteration number is invalid")
        with self._secure_directory(("iterations",), create=True) as parent:
            _sync_directory_access(parent)
        with self._secure_directory(
            ("iterations", f"{iteration:03d}"),
            create=True,
        ) as directory:
            _sync_directory_access(directory)
            return directory.path

    def _target(
        self,
        name: str,
        *,
        json_artifact: bool,
    ) -> tuple[tuple[str, ...], str]:
        if not isinstance(name, str) or "\\" in name:
            raise ValueError("optimizer artifact name is invalid")
        parts = Path(name).parts
        valid = False
        if json_artifact and name in self._ROOT_JSON:
            valid = True
        elif (
            json_artifact
            and len(parts) == 3
            and parts[0] == "iterations"
            and len(parts[1]) == 3
            and parts[1].isdigit()
            and parts[2]
            in self._ITERATION_JSON
            | self._ROLE_OUTPUT_INVALID_JSON
            | {self._PLAN_SKIP_JSON}
        ):
            valid = True
        elif not json_artifact and (
            name in self._REPLACEABLE_DIFF
            or (
                len(parts) == 3
                and parts[0] == "iterations"
                and len(parts[1]) == 3
                and parts[1].isdigit()
                and parts[2] == "candidate.diff"
            )
        ):
            valid = True
        if not valid:
            raise ValueError("optimizer artifact name is outside the closed layout")
        return tuple(parts[:-1]), parts[-1]

    def write_json_artifact(
        self,
        name: str,
        value: Mapping[str, object],
    ) -> tuple[Path, str]:
        if not isinstance(value, Mapping):
            raise ValueError("optimizer JSON artifact schema is invalid")
        parent_parts, target_name = self._target(name, json_artifact=True)
        is_recovery_artifact = target_name in (
            self._ROLE_OUTPUT_INVALID_JSON | {self._PLAN_SKIP_JSON}
        )
        if value.get("schema_version") != (4 if is_recovery_artifact else 3):
            raise ValueError("optimizer JSON artifact schema is invalid")
        payload = canonical_json_bytes(value)
        with self._secure_directory(parent_parts, create=False) as directory:
            if name in self._REPLACEABLE_JSON and directory.entry_exists(target_name):
                return _atomic_replace_in_directory(directory, target_name, payload)
            return _write_create_only_in_directory(directory, target_name, payload)

    def write_role_output_invalid(self, attempt: object) -> tuple[Path, str]:
        """Persist closed plan/provider facts, never rejected provider content."""

        from core.pit_optimizer_authorization import PitOptimizerRoleAttempt

        if (
            not isinstance(attempt, PitOptimizerRoleAttempt)
            or not attempt.recoverable_schema_invalid
        ):
            raise ValueError("optimizer role-output-invalid artifact is invalid")
        self.prepare_iteration(attempt.plan.iteration)
        return self.write_json_artifact(
            (
                f"iterations/{attempt.plan.iteration:03d}/"
                f"{attempt.plan.role}_output_invalid.json"
            ),
            {
                "schema_version": 4,
                "artifact_type": "role_output_invalid",
                "plan": attempt.plan.to_primitive(),
                "provider_facts": asdict(attempt.facts),
                "payload": None,
            },
        )

    def write_authorization_plan_skips(
        self,
        skips: tuple[object, ...],
    ) -> tuple[Path, str]:
        """Persist hash-chained zero-charge settlements without provider content."""

        from core.pit_optimizer_authorization import AuthorizationPlanSkip

        if (
            type(skips) is not tuple
            or not skips
            or any(not isinstance(skip, AuthorizationPlanSkip) for skip in skips)
        ):
            raise ValueError("optimizer authorization skip artifact is invalid")
        typed_skips = tuple(
            skip for skip in skips if isinstance(skip, AuthorizationPlanSkip)
        )
        iteration = typed_skips[0].iteration
        if any(skip.iteration != iteration for skip in typed_skips):
            raise ValueError("optimizer authorization skips span iterations")
        self.prepare_iteration(iteration)
        return self.write_json_artifact(
            f"iterations/{iteration:03d}/{self._PLAN_SKIP_JSON}",
            {
                "schema_version": 4,
                "artifact_type": "authorization_plan_skips",
                "iteration": iteration,
                "skips": [skip.to_record() for skip in typed_skips],
            },
        )

    def write_diff_artifact(self, name: str, value: str) -> tuple[Path, str]:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("optimizer diff artifact is invalid")
        parent_parts, target_name = self._target(name, json_artifact=False)
        payload = value.encode("utf-8")
        with self._secure_directory(parent_parts, create=False) as directory:
            if name in self._REPLACEABLE_DIFF and directory.entry_exists(target_name):
                return _atomic_replace_in_directory(directory, target_name, payload)
            return _write_create_only_in_directory(directory, target_name, payload)


PitOptimizerArtifactStore = IncrementalArtifactStore
