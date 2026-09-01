"""Crash-safe, incremental local artifacts for the PIT optimizer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from types import MappingProxyType
from typing import Mapping

from core.pit_optimizer_candidate import CandidateIdentityV4, EDITABLE_POLICY_PATHS
from core.pit_optimizer_evaluation import PanelAggregateSummary


def _closed_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _closed_relative_artifact(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _artifact_primitive(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _artifact_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_artifact_primitive(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SearchCandidateState:
    """Digest-bound durable state for one champion or exploratory branch."""

    candidate_identity: CandidateIdentityV4
    cumulative_diff_artifact: str
    cumulative_diff_sha256: str
    source_bundle_artifact: str
    source_bundle_sha256: str
    discovery_evidence_artifact: str
    discovery_evidence_sha256: str
    discovery_evidence: PanelAggregateSummary
    hypothesis: str
    behavioral_summary: str
    originating_run_id: str
    originating_iteration: int
    quick_evidence_artifact: str | None = None
    quick_evidence_sha256: str | None = None
    quick_evidence: PanelAggregateSummary | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_identity, CandidateIdentityV4):
            raise ValueError("search candidate identity is invalid")
        for name in (
            "cumulative_diff_artifact",
            "source_bundle_artifact",
            "discovery_evidence_artifact",
        ):
            _closed_relative_artifact(getattr(self, name), f"search candidate {name}")
        for name in (
            "cumulative_diff_sha256",
            "source_bundle_sha256",
            "discovery_evidence_sha256",
        ):
            _closed_sha256(getattr(self, name), f"search candidate {name}")
        if (
            self.cumulative_diff_sha256
            != self.candidate_identity.cumulative_diff_sha256
        ):
            raise ValueError("search candidate diff identity differs")
        if (
            not isinstance(self.discovery_evidence, PanelAggregateSummary)
            or self.discovery_evidence.panel_id != "discovery"
        ):
            raise ValueError("search candidate discovery evidence is invalid")
        if (self.quick_evidence is None) != (self.quick_evidence_artifact is None) or (
            self.quick_evidence is None
        ) != (self.quick_evidence_sha256 is None):
            raise ValueError("search candidate quick evidence is incomplete")
        if self.quick_evidence is not None:
            if self.quick_evidence.panel_id != "quick":
                raise ValueError("search candidate quick evidence is invalid")
            _closed_relative_artifact(
                self.quick_evidence_artifact,
                "search candidate quick evidence artifact",
            )
            _closed_sha256(
                self.quick_evidence_sha256,
                "search candidate quick evidence SHA-256",
            )
        if (
            not isinstance(self.hypothesis, str)
            or not self.hypothesis.strip()
            or not isinstance(self.behavioral_summary, str)
            or not self.behavioral_summary.strip()
            or not isinstance(self.originating_run_id, str)
            or not self.originating_run_id.strip()
            or type(self.originating_iteration) is not int
            or self.originating_iteration <= 0
        ):
            raise ValueError("search candidate provenance is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "candidate_identity": self.candidate_identity.to_primitive(),
            "cumulative_diff_artifact": self.cumulative_diff_artifact,
            "cumulative_diff_sha256": self.cumulative_diff_sha256,
            "source_bundle_artifact": self.source_bundle_artifact,
            "source_bundle_sha256": self.source_bundle_sha256,
            "discovery_evidence_artifact": self.discovery_evidence_artifact,
            "discovery_evidence_sha256": self.discovery_evidence_sha256,
            "discovery_evidence": _artifact_primitive(asdict(self.discovery_evidence)),
            "hypothesis": self.hypothesis,
            "behavioral_summary": self.behavioral_summary,
            "originating_run_id": self.originating_run_id,
            "originating_iteration": self.originating_iteration,
            "quick_evidence_artifact": self.quick_evidence_artifact,
            "quick_evidence_sha256": self.quick_evidence_sha256,
            "quick_evidence": (
                None
                if self.quick_evidence is None
                else _artifact_primitive(asdict(self.quick_evidence))
            ),
        }


@dataclass(frozen=True, slots=True)
class CampaignCheckpoint:
    """Authenticated, atomically replaced state carried between bounded runs."""

    schema_version: int
    artifact_type: str
    campaign_id: str
    campaign_sequence: int
    source_head: str
    source_fingerprint_sha256: str
    discovery_panel_plan_sha256: str
    completed_iterations: int
    champion: SearchCandidateState | None
    active_branch: SearchCandidateState | None
    feedback_tail: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if self.schema_version != 4 or self.artifact_type != "campaign_checkpoint":
            raise ValueError("campaign checkpoint schema is invalid")
        if (
            not isinstance(self.campaign_id, str)
            or not self.campaign_id.strip()
            or type(self.campaign_sequence) is not int
            or self.campaign_sequence <= 0
            or not isinstance(self.source_head, str)
            or len(self.source_head) != 40
            or any(character not in "0123456789abcdef" for character in self.source_head)
            or type(self.completed_iterations) is not int
            or self.completed_iterations < 0
        ):
            raise ValueError("campaign checkpoint provenance is invalid")
        _closed_sha256(
            self.source_fingerprint_sha256,
            "campaign checkpoint source fingerprint",
        )
        _closed_sha256(
            self.discovery_panel_plan_sha256,
            "campaign checkpoint discovery plan",
        )
        if self.champion is not None and not isinstance(
            self.champion, SearchCandidateState
        ):
            raise ValueError("campaign checkpoint champion is invalid")
        if self.active_branch is not None and not isinstance(
            self.active_branch, SearchCandidateState
        ):
            raise ValueError("campaign checkpoint active branch is invalid")
        if self.active_branch is not None and self.champion is not None and (
            self.active_branch.candidate_identity.identity_sha256
            == self.champion.candidate_identity.identity_sha256
        ):
            raise ValueError("campaign checkpoint branch duplicates champion")
        if type(self.feedback_tail) is not tuple or any(
            not isinstance(item, Mapping) for item in self.feedback_tail
        ):
            raise ValueError("campaign checkpoint feedback tail is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "campaign_id": self.campaign_id,
            "campaign_sequence": self.campaign_sequence,
            "source_head": self.source_head,
            "source_fingerprint_sha256": self.source_fingerprint_sha256,
            "discovery_panel_plan_sha256": self.discovery_panel_plan_sha256,
            "completed_iterations": self.completed_iterations,
            "champion": None if self.champion is None else self.champion.to_primitive(),
            "active_branch": (
                None if self.active_branch is None else self.active_branch.to_primitive()
            ),
            "feedback_tail": [dict(item) for item in self.feedback_tail],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_primitive())).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenChampionArtifact:
    """Canonical checkpoint inputs authenticated before disposable reconstruction."""

    checkpoint_path: Path
    checkpoint_sha256: str
    campaign_id: str
    source_head: str
    source_fingerprint_sha256: str
    discovery_panel_plan_sha256: str
    candidate_identity: Mapping[str, object]
    candidate_identity_sha256: str
    cumulative_diff: str
    policy_sources: tuple[tuple[str, str], ...]
    champion: Mapping[str, object]


def _read_frozen_artifact(
    root: Path,
    relative: object,
    expected_sha256: object,
    *,
    label: str,
) -> tuple[Path, bytes]:
    relative_path = Path(relative) if isinstance(relative, str) else Path()
    if (
        not isinstance(relative, str)
        or not relative
        or relative_path.is_absolute()
        or ".." in relative_path.parts
    ):
        raise ValueError(f"frozen champion {label} path is invalid")
    digest = _closed_sha256(
        expected_sha256,
        f"frozen champion {label} SHA-256",
    )
    target = (root / relative_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"frozen champion {label} escaped checkpoint root") from exc
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"frozen champion {label} is absent")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"frozen champion {label} digest differs")
    return target, raw


def _canonical_frozen_json(raw: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"frozen champion {label} JSON is invalid") from exc
    if not isinstance(value, dict) or raw != canonical_json_bytes(value):
        raise ValueError(f"frozen champion {label} is not canonical JSON")
    return value


def load_frozen_champion_artifact(
    checkpoint_path: Path,
    *,
    expected_sha256: str,
) -> FrozenChampionArtifact:
    """Load one branch-free schema-v4 checkpoint and all champion references."""

    candidate = Path(checkpoint_path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise ValueError("frozen champion checkpoint path is invalid")
    expected = _closed_sha256(
        expected_sha256,
        "frozen champion checkpoint SHA-256",
    )
    raw_checkpoint = candidate.read_bytes()
    if hashlib.sha256(raw_checkpoint).hexdigest() != expected:
        raise ValueError("frozen champion checkpoint digest differs")
    checkpoint = _canonical_frozen_json(raw_checkpoint, label="checkpoint")
    expected_checkpoint_keys = {
        "schema_version",
        "artifact_type",
        "campaign_id",
        "campaign_sequence",
        "source_head",
        "source_fingerprint_sha256",
        "discovery_panel_plan_sha256",
        "completed_iterations",
        "champion",
        "active_branch",
        "feedback_tail",
    }
    champion = checkpoint.get("champion")
    if (
        set(checkpoint) != expected_checkpoint_keys
        or checkpoint.get("schema_version") != 4
        or checkpoint.get("artifact_type") != "campaign_checkpoint"
        or not isinstance(champion, dict)
        or checkpoint.get("active_branch") is not None
    ):
        raise ValueError("qualification requires one frozen branch-free champion")
    identity = champion.get("candidate_identity")
    if not isinstance(identity, dict):
        raise ValueError("frozen champion candidate identity is invalid")
    identity_keys = {
        "source_commit",
        "policy_interface_version",
        "cumulative_diff_sha256",
        "editable_file_sha256s",
        "changed_paths",
        "changed_symbols",
        "immutable_constraints_sha256",
        "discovery_panel_plan_sha256",
        "parent_identity_sha256",
        "identity_sha256",
    }
    identity_sha256 = _closed_sha256(
        identity.get("identity_sha256"),
        "frozen champion candidate identity SHA-256",
    )
    identity_preimage = {
        key: value for key, value in identity.items() if key != "identity_sha256"
    }
    if (
        set(identity) != identity_keys
        or hashlib.sha256(canonical_json_bytes(identity_preimage)).hexdigest()
        != identity_sha256
        or identity.get("source_commit") != checkpoint.get("source_head")
        or identity.get("discovery_panel_plan_sha256")
        != checkpoint.get("discovery_panel_plan_sha256")
    ):
        raise ValueError("frozen champion candidate identity differs")
    root = candidate.parent.resolve()
    _diff_path, raw_diff = _read_frozen_artifact(
        root,
        champion.get("cumulative_diff_artifact"),
        champion.get("cumulative_diff_sha256"),
        label="cumulative diff",
    )
    try:
        cumulative_diff = raw_diff.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("frozen champion diff is not UTF-8") from exc
    if (
        "\x00" in cumulative_diff
        or hashlib.sha256(cumulative_diff.encode("utf-8")).hexdigest()
        != identity.get("cumulative_diff_sha256")
    ):
        raise ValueError("frozen champion diff identity differs")
    _source_path, raw_source = _read_frozen_artifact(
        root,
        champion.get("source_bundle_artifact"),
        champion.get("source_bundle_sha256"),
        label="source bundle",
    )
    source_artifact = _canonical_frozen_json(raw_source, label="source bundle")
    raw_sources = source_artifact.get("policy_sources")
    if (
        source_artifact.get("schema_version") != 4
        or source_artifact.get("candidate_identity_sha256") != identity_sha256
        or not isinstance(raw_sources, dict)
        or set(raw_sources) != set(EDITABLE_POLICY_PATHS)
        or any(not isinstance(value, str) for value in raw_sources.values())
    ):
        raise ValueError("frozen champion source bundle is invalid")
    policy_sources = tuple((path, raw_sources[path]) for path in EDITABLE_POLICY_PATHS)
    source_sha256s = tuple(
        (path, hashlib.sha256(source.encode("utf-8")).hexdigest())
        for path, source in policy_sources
    )
    editable_sha256s = identity.get("editable_file_sha256s")
    if (
        not isinstance(editable_sha256s, list)
        or tuple(tuple(item) for item in editable_sha256s) != source_sha256s
    ):
        raise ValueError("frozen champion sources differ from candidate identity")
    for prefix in ("quick", "discovery"):
        artifact_name = champion.get(f"{prefix}_evidence_artifact")
        artifact_sha256 = champion.get(f"{prefix}_evidence_sha256")
        if artifact_name is None or artifact_sha256 is None:
            raise ValueError("frozen champion panel evidence is incomplete")
        _evidence_path, evidence_raw = _read_frozen_artifact(
            root,
            artifact_name,
            artifact_sha256,
            label=f"{prefix} evidence",
        )
        evidence_artifact = _canonical_frozen_json(
            evidence_raw,
            label=f"{prefix} evidence",
        )
        if evidence_artifact.get("evidence") != champion.get(f"{prefix}_evidence"):
            raise ValueError("frozen champion panel evidence differs")
    return FrozenChampionArtifact(
        checkpoint_path=candidate.resolve(),
        checkpoint_sha256=expected,
        campaign_id=str(checkpoint.get("campaign_id")),
        source_head=str(checkpoint.get("source_head")),
        source_fingerprint_sha256=_closed_sha256(
            checkpoint.get("source_fingerprint_sha256"),
            "frozen champion source fingerprint",
        ),
        discovery_panel_plan_sha256=_closed_sha256(
            checkpoint.get("discovery_panel_plan_sha256"),
            "frozen champion discovery plan",
        ),
        candidate_identity=MappingProxyType(dict(identity)),
        candidate_identity_sha256=identity_sha256,
        cumulative_diff=cumulative_diff,
        policy_sources=policy_sources,
        champion=MappingProxyType(dict(champion)),
    )


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
    """Bounded create-only evidence with explicit replaceable snapshots."""

    _REPLACEABLE_JSON = frozenset({"accounting.json", "checkpoint.json"})
    _REPLACEABLE_DIFF = frozenset({"incumbent.diff", "champion.diff", "branch.diff"})
    _SEED_DIFF = frozenset({"seed-champion.diff", "seed-branch.diff"})
    _ROOT_JSON = frozenset(
        {
            "run.json",
            "baseline.json",
            "accounting.json",
            "checkpoint.json",
            "seed-champion-source.json",
            "seed-champion-quick.json",
            "seed-champion-discovery.json",
            "seed-branch-source.json",
            "seed-branch-quick.json",
            "seed-branch-discovery.json",
            "holdout.json",
            "summary.json",
        }
    )
    _ITERATION_JSON = frozenset(
        {
            "investigator.json",
            "author.json",
            "candidate-source.json",
            "validation.json",
            "quick.json",
            "discovery.json",
            "critic.json",
            "decision.json",
        }
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
            name in self._REPLACEABLE_DIFF | self._SEED_DIFF
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
        expected_versions = {4} if is_recovery_artifact else {3, 4}
        if value.get("schema_version") not in expected_versions:
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
