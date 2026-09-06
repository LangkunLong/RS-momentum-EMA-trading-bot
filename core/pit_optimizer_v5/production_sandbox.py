"""Concrete local mount and container capabilities for PIT optimizer V5."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterator, Literal

from core.pit_optimizer_evaluation import EvaluationPanelSpec
from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    canonical_sha256_v5,
    validate_episode_plan_panel_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.container_protocol import (
    PanelExecutionRequestV5,
    panel_execution_request_bytes_v5,
)
from core.pit_optimizer_v5.production_workspace import LocalGitWorkspaceDriverV5
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    acquire_directory_v5,
    clear_owned_directory_v5,
    create_directory_in_directory_v5,
    directory_child_absent_v5,
    directory_entry_names_v5,
    directory_is_empty_v5,
    hash_regular_in_directory_v5,
    open_regular_in_directory_v5,
    remove_owned_tree_in_directory_v5,
    write_new_regular_in_directory_v5,
)
from core.pit_optimizer_v5.memory import (
    CandidateExecutionAuthorityV5,
    CleanupResultPayloadV5,
    ResourceLeasePayloadV5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.runtime import CandidateExecutionKeyV5, MaterializedVariantV5, OwnedLeaseV5
from core.pit_optimizer_v5.sandbox import (
    BoundedOutputBytesV5,
    ContainerCommandV5,
    ContainerExecutionResultV5,
    ContainerExecutorV5,
    DockerPanelRequestV5,
    EVALUATOR_RUNTIME_KIND_LABEL_V5,
    EVALUATOR_RUNTIME_KIND_V5,
    EVALUATOR_RUNTIME_SOURCE_LABEL_V5,
    ExecutionLeaseV5,
    ExecutionReservationV5,
    SandboxMountFactoryV5,
    SandboxMountHandleV5,
    build_docker_argv_v5,
    derive_execution_lease_id_v5,
    derive_sandbox_mount_authorities_v5,
    execution_output_name_v5,
    panel_execution_request_for_v5,
)
from core.pit_optimizer_v5.workspace import MaterializedWorkspaceV5, WorkspaceOwnerV5


_DATA_FILES_V5 = ("pit_bundle.sqlite3", "prices_provenance.json")
_DATA_FILE_MAXIMUM_BYTES_V5 = (8 * 1024 * 1024 * 1024, 64 * 1024 * 1024)
_DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5 = 512 * 1024 * 1024
_POLICY_SOURCE_MAXIMUM_BYTES_V5 = 1024 * 1024
_PANEL_INPUT_MAXIMUM_BYTES_V5 = 8 * 1024 * 1024
_CONTAINER_SHM_SIZE_BYTES_V5 = 16 * 1024 * 1024
_CONTROL_CHILDREN_V5 = ("config", "home", "tmp")
_DOCKER_CONFIG_NAME_V5 = "config.json"
_DOCKER_CONFIG_CONTENT_V5 = b"{}\n"
_DOCKER_CONFIG_TEXT_V5 = _DOCKER_CONFIG_CONTENT_V5.decode("ascii")
_PANEL_INPUT_NAMESPACE_V5 = "container-panel-input"
_PANEL_INPUT_TARGET_V5 = "/pit/request/panel-request.json"


def _windows_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _docker_host_path_v5(path: Path) -> str:
    value = str(path)
    if (
        not ntpath.isabs(value)
        or ntpath.normpath(value) != value
        or any(character in value for character in {",", "\x00", "\n", "\r"})
    ):
        raise ValueError("Docker host mount path is invalid")
    return value


def _panel_input_reference_v5(
    request: DockerPanelRequestV5,
) -> tuple[PanelExecutionRequestV5, ArtifactRefV5]:
    panel_input = panel_execution_request_for_v5(request)
    raw = panel_execution_request_bytes_v5(panel_input)
    reference = ArtifactRefV5(
        f"adapter-state/{_PANEL_INPUT_NAMESPACE_V5}/{request.sha256}.json",
        hashlib.sha256(raw).hexdigest(),
    )
    if reference.sha256 != panel_input.sha256:
        raise ValueError("panel input canonical identity is inconsistent")
    return panel_input, reference


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _canonical_directory(path: Path) -> tuple[Path, os.stat_result]:
    text = str(path)
    if not path.is_absolute() or "/" in text or ntpath.normpath(text) != text:
        raise ValueError("sandbox root path is not canonical")
    try:
        resolved = path.resolve(strict=True)
        with acquire_absolute_directory_v5(resolved) as access:
            info = access.path.lstat()
            if (info.st_dev, info.st_ino) != access.identity:
                raise ValueError("sandbox root identity changed")
    except (OSError, ValueError):
        raise ValueError("sandbox root is unavailable") from None
    if (
        _windows_key(str(resolved)) != _windows_key(text)
        or not stat.S_ISDIR(info.st_mode)
        or _is_reparse(info)
    ):
        raise ValueError("sandbox root is not a link-free directory")
    current = resolved
    while True:
        try:
            if _is_reparse(current.lstat()):
                raise ValueError("sandbox root chain contains a reparse point")
        except OSError:
            raise ValueError("sandbox root chain is unavailable") from None
        if current.parent == current:
            break
        current = current.parent
    return resolved, info


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("sandbox path is unavailable") from None


def _roots_overlap(first: Path, second: Path) -> bool:
    first_key = _windows_key(str(first))
    second_key = _windows_key(str(second))
    try:
        common = ntpath.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in {first_key, second_key}


def _hash_regular_no_follow(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[int, int, int, str]:
    try:
        with acquire_absolute_directory_v5(path.parent) as parent:
            return hash_regular_in_directory_v5(
                parent,
                path.name,
                maximum_bytes=maximum_bytes,
            )
    except (OSError, ValueError):
        raise ValueError("sandbox data file is not an exact regular file") from None


def _closed_child_parts(relative_path: str) -> tuple[str, ...]:
    parts = relative_path.split("/")
    if not parts or any(not item or item in {".", ".."} or "\\" in item for item in parts):
        raise ValueError("sandbox source path is invalid")
    return tuple(parts)


def _directory_identity(path: Path, info: os.stat_result) -> str:
    return canonical_sha256_v5(
        {
            "canonical_path_key": _windows_key(str(path)),
            "device": info.st_dev,
            "inode": info.st_ino,
        }
    )


def _hash_docker_executable(path: Path) -> tuple[int, int, int, str]:
    try:
        resolved = path.resolve(strict=True)
        before = resolved.lstat()
    except OSError:
        raise ValueError("Docker executable is unavailable") from None
    if (
        resolved != path
        or resolved.name.casefold() not in {"docker", "docker.exe"}
        or not stat.S_ISREG(before.st_mode)
        or _is_reparse(before)
    ):
        raise ValueError("Docker executable is not a canonical regular file")
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        stream = os.fdopen(descriptor, "rb", buffering=0)
        opened = os.fstat(stream.fileno())
        after = resolved.lstat()
    except OSError:
        raise ValueError("Docker executable could not be authenticated") from None
    if (
        (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or _is_reparse(after)
    ):
        stream.close()
        raise ValueError("Docker executable changed during authentication")
    if opened.st_size > _DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5:
        stream.close()
        raise ValueError("Docker executable exceeds its authentication bound")
    digest = hashlib.sha256()
    observed_bytes = 0
    with stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            observed_bytes += len(chunk)
            if observed_bytes > _DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5:
                raise ValueError("Docker executable exceeds its authentication bound")
            digest.update(chunk)
    if observed_bytes != opened.st_size:
        raise ValueError("Docker executable changed during authentication")
    return opened.st_dev, opened.st_ino, observed_bytes, digest.hexdigest()


def _hash_open_stream(
    stream: object,
    info: os.stat_result,
    *,
    maximum_bytes: int,
) -> tuple[int, int, int, str]:
    if (
        type(maximum_bytes) is not int
        or maximum_bytes <= 0
        or info.st_size > maximum_bytes
    ):
        raise ValueError("pinned file exceeds its authentication bound")
    stream.seek(0)  # type: ignore[attr-defined]
    digest = hashlib.sha256()
    observed_bytes = 0
    while chunk := stream.read(1024 * 1024):  # type: ignore[attr-defined]
        observed_bytes += len(chunk)
        if observed_bytes > maximum_bytes:
            raise ValueError("pinned file exceeds its authentication bound")
        digest.update(chunk)
    after = os.fstat(stream.fileno())  # type: ignore[attr-defined]
    if (
        observed_bytes != info.st_size
        or (after.st_dev, after.st_ino, after.st_size)
        != (info.st_dev, info.st_ino, info.st_size)
    ):
        raise ValueError("pinned file changed during authentication")
    return info.st_dev, info.st_ino, observed_bytes, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _MountCapabilityV5:
    factory_identity_sha256: str
    kind: Literal["source", "data", "output"]
    path: str
    device: int
    inode: int
    content_authority_sha256: str
    binding_sha256: str


@dataclass(frozen=True, slots=True)
class MountReservationRecordV5:
    schema_version: Literal[5]
    factory_identity_sha256: str
    owner: WorkspaceOwnerV5
    execution_key_sha256: str
    output_authority_sha256: str
    output_relative_path: str
    output_parent_identity_sha256: str
    source_root_identity_sha256: str
    data_root_identity_sha256: str

    def __post_init__(self) -> None:
        digests = (
            self.factory_identity_sha256,
            self.execution_key_sha256,
            self.output_authority_sha256,
            self.output_parent_identity_sha256,
            self.source_root_identity_sha256,
            self.data_root_identity_sha256,
        )
        if (
            self.schema_version != 5
            or type(self.owner) is not WorkspaceOwnerV5
            or any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests)
            or re.fullmatch(r"pit-v5-output-[0-9a-f]{24}", self.output_relative_path) is None
        ):
            raise ValueError("sandbox mount reservation is invalid")


@dataclass(frozen=True, slots=True)
class MountCreatedRecordV5:
    schema_version: Literal[5]
    factory_identity_sha256: str
    output_authority_sha256: str
    output_device: int
    output_inode: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.factory_identity_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.output_authority_sha256) is None
            or type(self.output_device) is not int
            or type(self.output_inode) is not int
            or self.output_device < 0
            or self.output_inode <= 0
        ):
            raise ValueError("sandbox mount created record is invalid")


@dataclass(frozen=True, slots=True)
class MountReadyRecordV5:
    schema_version: Literal[5]
    factory_identity_sha256: str
    output_authority_sha256: str
    output_root_identity_sha256: str
    output_device: int
    output_inode: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or any(
                re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in (
                    self.factory_identity_sha256,
                    self.output_authority_sha256,
                    self.output_root_identity_sha256,
                )
            )
            or type(self.output_device) is not int
            or type(self.output_inode) is not int
            or self.output_device < 0
            or self.output_inode <= 0
        ):
            raise ValueError("sandbox mount ready record is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionReservationRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    request_sha256: str
    owner: WorkspaceOwnerV5
    command_argv: tuple[str, ...]
    runtime_argv_sha256: str
    output_mount_authority_sha256: str
    expected_mounts: tuple[tuple[str, str, bool], ...]
    lease_ids: tuple[str, str]
    container_name: str
    control_relative_path: str

    def __post_init__(self) -> None:
        digests = (
            self.executor_identity_sha256,
            self.command_sha256,
            self.request_sha256,
            self.runtime_argv_sha256,
            self.output_mount_authority_sha256,
        )
        if (
            self.schema_version != 5
            or any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests)
            or type(self.owner) is not WorkspaceOwnerV5
            or type(self.command_argv) is not tuple
            or not self.command_argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.command_argv)
            or type(self.lease_ids) is not tuple
            or len(self.lease_ids) != 2
            or any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in self.lease_ids)
            or re.fullmatch(r"pit-v5-[0-9a-f]{40}", self.container_name) is None
            or re.fullmatch(r"execution-[0-9a-f]{24}", self.control_relative_path) is None
        ):
            raise ValueError("container execution reservation is invalid")
        separator = self.command_argv.index("--")
        semantic_probe = self.command_argv[separator + 2 : separator + 7] == (
            "python",
            "-P",
            "-B",
            "-m",
            "core.pit_optimizer_v5.probe_entry",
        )
        if (
            type(self.expected_mounts) is not tuple
            or len(self.expected_mounts) != (5 if semantic_probe else 8)
            or any(
                type(item) is not tuple
                or len(item) != 3
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not str
                or not item[1].startswith("/")
                or type(item[2]) is not bool
                for item in self.expected_mounts
            )
        ):
            raise ValueError("container execution mount authority is invalid")
        if self.expected_mounts != tuple(sorted(set(self.expected_mounts))):
            raise ValueError("container execution mount authority is not canonical")


@dataclass(frozen=True, slots=True)
class ExecutionPhaseRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    phase: Literal["launch_claim", "created", "start_claim", "started", "collected"]
    attestation_sha256: str | None
    container_identity_sha256: str | None = None
    lifecycle_status: Literal["created", "running", "exited"] | None = None
    network_attestation_sha256: str | None = None
    state_attestation_sha256: str | None = None
    network_namespace_status: Literal["empty", "private"] | None = None

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.executor_identity_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.command_sha256) is None
            or self.phase not in {"launch_claim", "created", "start_claim", "started", "collected"}
            or (
                self.attestation_sha256 is None
                if self.phase in {"created", "started", "collected"}
                else self.attestation_sha256 is not None
            )
            or (
                self.attestation_sha256 is not None
                and re.fullmatch(r"[0-9a-f]{64}", self.attestation_sha256) is None
            )
            or (
                self.phase in {"launch_claim", "start_claim"}
                and any(
                    item is not None
                    for item in (
                        self.container_identity_sha256,
                        self.lifecycle_status,
                        self.network_attestation_sha256,
                        self.state_attestation_sha256,
                        self.network_namespace_status,
                    )
                )
            )
            or (
                self.phase in {"created", "started", "collected"}
                and (
                    re.fullmatch(r"[0-9a-f]{64}", self.container_identity_sha256 or "")
                    is None
                    or re.fullmatch(r"[0-9a-f]{64}", self.network_attestation_sha256 or "")
                    is None
                    or re.fullmatch(r"[0-9a-f]{64}", self.state_attestation_sha256 or "")
                    is None
                    or self.network_namespace_status not in {"empty", "private"}
                    or self.lifecycle_status
                    not in (
                        {"created"}
                        if self.phase == "created"
                        else {"exited"}
                        if self.phase == "collected"
                        else {"running", "exited"}
                    )
                    or self.lifecycle_status == "created"
                    and self.network_namespace_status != "empty"
                    or self.lifecycle_status == "running"
                    and self.network_namespace_status != "private"
                )
            )
        ):
            raise ValueError("container execution phase is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionContainerIdentityRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    container_id: str
    created_at: str
    stable_attestation_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.executor_identity_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.command_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.container_id) is None
            or type(self.created_at) is not str
            or len(self.created_at) > 128
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z",
                self.created_at,
            )
            is None
            or re.fullmatch(r"[0-9a-f]{64}", self.stable_attestation_sha256) is None
        ):
            raise ValueError("container execution identity record is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class ExecutionControlCreatedRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    control_root_identity_sha256: str
    transaction_device: int
    transaction_inode: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.executor_identity_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.command_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.control_root_identity_sha256) is None
            or type(self.transaction_device) is not int
            or type(self.transaction_inode) is not int
            or self.transaction_device < 0
            or self.transaction_inode <= 0
        ):
            raise ValueError("container execution control creation record is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionControlRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    control_root_identity_sha256: str
    transaction_root_identity_sha256: str
    child_identity_sha256s: tuple[tuple[str, str], ...]
    docker_config_file_identity_sha256: str
    docker_config_content: str
    docker_executable_identity_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 5 or any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in (
                self.executor_identity_sha256,
                self.command_sha256,
                self.control_root_identity_sha256,
                self.transaction_root_identity_sha256,
                self.docker_config_file_identity_sha256,
                self.docker_executable_identity_sha256,
            )
        ) or (
            type(self.child_identity_sha256s) is not tuple
            or tuple(item[0] for item in self.child_identity_sha256s) != _CONTROL_CHILDREN_V5
            or any(
                type(item) is not tuple
                or len(item) != 2
                or re.fullmatch(r"[0-9a-f]{64}", item[1]) is None
                for item in self.child_identity_sha256s
            )
            or self.docker_config_content != _DOCKER_CONFIG_TEXT_V5
        ):
            raise ValueError("container execution control record is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionTerminalRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    request_sha256: str
    output_mount_authority_sha256: str
    status: Literal["succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"]
    exit_code: int | None
    output_ref: ArtifactRefV5 | None
    observed_byte_count: int

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or any(
                re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in (
                    self.executor_identity_sha256,
                    self.command_sha256,
                    self.request_sha256,
                    self.output_mount_authority_sha256,
                )
            )
            or self.status not in {"succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"}
            or (self.status == "succeeded" and self.exit_code != 0)
            or (self.status == "nonzero_exit" and (type(self.exit_code) is not int or self.exit_code == 0))
            or (self.status not in {"succeeded", "nonzero_exit"} and self.exit_code is not None)
            or (self.output_ref is not None and type(self.output_ref) is not ArtifactRefV5)
            or type(self.observed_byte_count) is not int
            or self.observed_byte_count < 0
            or (self.output_ref is None and self.observed_byte_count != 0)
        ):
            raise ValueError("container execution terminal is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionCleanupRecordV5:
    schema_version: Literal[5]
    executor_identity_sha256: str
    command_sha256: str
    owner_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 5 or any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in (self.executor_identity_sha256, self.command_sha256, self.owner_sha256)
        ):
            raise ValueError("container execution cleanup record is invalid")


@dataclass(frozen=True, slots=True)
class _ExecutionReservationCapabilityV5:
    executor_identity_sha256: str
    command_sha256: str


@dataclass(frozen=True, slots=True)
class _PinnedControlTransactionV5:
    executor_identity_sha256: str
    command_sha256: str
    transaction_root_identity: tuple[int, int]
    environment: tuple[tuple[str, str], ...]
    opaque_capability: object


@dataclass(frozen=True, slots=True)
class _ContainerInspectionV5:
    raw: dict[str, object]
    container_id: str
    created_at: str
    stable_attestation_sha256: str
    network_authority: dict[str, object]
    network_attestation_sha256: str
    state_authority: dict[str, object]
    state_attestation_sha256: str


@dataclass(frozen=True, slots=True)
class _ExecutionLeaseCapabilityV5:
    executor_identity_sha256: str
    command_sha256: str
    role_kind: Literal["evaluator_process", "container"]
    owner_sha256: str
    command: ContainerCommandV5


@dataclass(frozen=True, slots=True)
class _RecoveredExecutionLeaseCapabilityV5:
    executor_identity_sha256: str
    command_sha256: str
    role_kind: Literal["evaluator_process", "container"]
    owner_sha256: str


class LocalSandboxMountFactoryV5(SandboxMountFactoryV5):
    """Issue exact source/data/output mount handles without launching Docker."""

    def __init__(
        self,
        *,
        manifest: CampaignManifestV5,
        evaluator_contract: EvaluatorContractV5,
        sandbox_profile: SandboxProfileV5,
        owner: WorkspaceOwnerV5,
        workspace_driver: LocalGitWorkspaceDriverV5,
        data_root: Path,
        output_root: Path,
        repository: LocalArtifactRepositoryV5,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError(
                "the concrete V5 sandbox mount adapter requires Windows handle authority"
            )
        if (
            type(manifest) is not CampaignManifestV5
            or type(evaluator_contract) is not EvaluatorContractV5
            or type(sandbox_profile) is not SandboxProfileV5
            or type(owner) is not WorkspaceOwnerV5
            or type(workspace_driver) is not LocalGitWorkspaceDriverV5
            or type(repository) is not LocalArtifactRepositoryV5
            or not isinstance(data_root, Path)
            or not isinstance(output_root, Path)
            or owner.campaign_id != manifest.campaign_id
            or owner.round_index > manifest.search.max_feedback_rounds
            or manifest.evaluator_contract_ref.sha256 != evaluator_contract.sha256
            or manifest.sandbox_profile_ref.sha256 != sandbox_profile.sha256
            or evaluator_contract.sandbox_profile_sha256 != sandbox_profile.sha256
        ):
            raise ValueError("local sandbox mount configuration is invalid")
        validate_sandbox_profile_resources_v5(sandbox_profile, manifest.resources)
        data_path, data_info = _canonical_directory(data_root)
        output_path, output_info = _canonical_directory(output_root)
        workspace_roots = tuple(
            (Path(path), identity) for path, identity in workspace_driver.root_identities
        )
        try:
            for path, identity in workspace_roots:
                with acquire_absolute_directory_v5(path, expected_identity=identity):
                    pass
        except (OSError, ValueError):
            raise ValueError("workspace root authority changed") from None
        fixed_roots = (workspace_roots[0][0], workspace_roots[1][0], data_path, output_path)
        if any(
            _roots_overlap(first, second)
            for index, first in enumerate(fixed_roots)
            for second in fixed_roots[index + 1 :]
        ):
            raise ValueError("sandbox source, workspace, data, and output roots must be fully disjoint")
        try:
            with acquire_absolute_directory_v5(
                data_path,
                expected_identity=(data_info.st_dev, data_info.st_ino),
            ) as data_access:
                entries = tuple(sorted(item.name for item in os.scandir(data_access.path)))
                bundle_identity = hash_regular_in_directory_v5(
                    data_access,
                    _DATA_FILES_V5[0],
                    maximum_bytes=_DATA_FILE_MAXIMUM_BYTES_V5[0],
                )
                provenance_identity = hash_regular_in_directory_v5(
                    data_access,
                    _DATA_FILES_V5[1],
                    maximum_bytes=_DATA_FILE_MAXIMUM_BYTES_V5[1],
                )
        except (OSError, ValueError):
            raise ValueError("sandbox data root is unreadable") from None
        if entries != _DATA_FILES_V5:
            raise ValueError("sandbox data root differs from the closed V5 layout")
        if (
            bundle_identity[3] != evaluator_contract.pit_bundle_sha256
            or provenance_identity[3] != evaluator_contract.prices_provenance_sha256
        ):
            raise ValueError("sandbox data bytes differ from evaluator authority")
        self._manifest = manifest
        self._contract = evaluator_contract
        self._profile = sandbox_profile
        self._owner = owner
        self._workspace = workspace_driver
        self._data_root = data_path
        self._data_info = data_info
        self._output_root = output_path
        self._output_info = output_info
        self._repository = repository
        self._fixed_roots = fixed_roots
        self._workspace_root_identities = workspace_roots
        self._data_file_identities = (bundle_identity, provenance_identity)
        self._mount_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-local-sandbox-mount-v1",
                "manifest_sha256": manifest.sha256,
                "evaluator_contract_sha256": evaluator_contract.sha256,
                "sandbox_profile_sha256": sandbox_profile.sha256,
                "owner_sha256": owner.sha256,
                "workspace_driver_sha256": workspace_driver.driver_identity_sha256,
                "data_root_identity": _directory_identity(data_path, data_info),
                "data_file_identities": self._data_file_identities,
                "output_root_identity": _directory_identity(output_path, output_info),
                "repository_root_identity_sha256": repository.root_identity_sha256,
            }
        )

    @property
    def mount_identity_sha256(self) -> str:
        return self._mount_identity_sha256

    @property
    def workspace_driver(self) -> LocalGitWorkspaceDriverV5:
        return self._workspace

    @property
    def fixed_roots(self) -> tuple[Path, Path, Path, Path]:
        return self._fixed_roots

    def panel_for(self, panel: EpisodePlanV5) -> EvaluationPanelSpec:
        """Load and authenticate the complete panel referenced by one episode."""

        if type(panel) is not EpisodePlanV5:
            raise ValueError("sandbox panel request is invalid")
        authenticated = self._repository.load_evaluation_panel_spec(panel.panel_ref)
        validate_episode_plan_panel_v5(panel, authenticated)
        if authenticated.purpose != panel.purpose:
            raise ValueError("sandbox panel purpose differs from its episode")
        return authenticated

    def mounts_for(
        self,
        *,
        materialized: MaterializedVariantV5,
        panel: EpisodePlanV5,
        scenario_ids: tuple[str, ...],
        execution_key: CandidateExecutionKeyV5,
    ) -> tuple[SandboxMountHandleV5, ...]:
        if (
            type(materialized) is not MaterializedVariantV5
            or type(materialized.opaque_candidate) is not MaterializedWorkspaceV5
            or type(panel) is not EpisodePlanV5
            or type(scenario_ids) is not tuple
            or type(execution_key) is not CandidateExecutionKeyV5
        ):
            raise ValueError("sandbox mount request is invalid")
        candidate = materialized.opaque_candidate
        if (
            candidate.variant != materialized.variant
            or materialized.leases != (candidate.lease.as_owned_lease(),)
            or candidate.lease.owner != self._owner
        ):
            raise ValueError("sandbox source authority is foreign")
        source_path, source_binding, source_identity = self._workspace.authorize_materialized_source(
            candidate
        )
        workspace_root = self._fixed_roots[1]
        if (
            _windows_key(str(source_path.parent)) != _windows_key(str(workspace_root))
            or any(
                _roots_overlap(source_path, fixed)
                for fixed in (self._fixed_roots[0], self._data_root, self._output_root)
            )
        ):
            raise ValueError("sandbox candidate subtree is outside its workspace authority")
        authorities = derive_sandbox_mount_authorities_v5(
            owner=self._owner,
            policy_revision=materialized.variant.policy_revision,
            evaluator_contract=self._contract,
            sandbox_profile=self._profile,
            panel=panel,
            scenario_ids=scenario_ids,
            execution_key=execution_key,
        )
        semantic_probe = execution_key.stage == "semantic_probe"
        if semantic_probe != (scenario_ids == ()):
            raise ValueError("sandbox probe data authority is invalid")
        absent_data_identity = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-absent-data-mount-v1",
                "execution_key": canonical_sha256_v5(execution_key),
            }
        )
        try:
            with ExitStack() as stack:
                source_access = stack.enter_context(
                    acquire_absolute_directory_v5(source_path, expected_identity=source_identity)
                )
                output_access = stack.enter_context(
                    acquire_absolute_directory_v5(
                        self._output_root,
                        expected_identity=(self._output_info.st_dev, self._output_info.st_ino),
                    )
                )
                data_access = None
                if not semantic_probe:
                    data_access = stack.enter_context(
                        acquire_absolute_directory_v5(
                            self._data_root,
                            expected_identity=(
                                self._data_info.st_dev,
                                self._data_info.st_ino,
                            ),
                        )
                    )
                    self._authenticate_data_files(data_access)
                # This is the last pre-mutation authority check.  All involved
                # ancestors remain pinned while the output child is reserved.
                issuance_paths = (
                    (source_access.path, output_access.path)
                    if data_access is None
                    else (source_access.path, data_access.path, output_access.path)
                )
                if any(
                    _roots_overlap(first, second)
                    for index, first in enumerate(issuance_paths)
                    for second in issuance_paths[index + 1 :]
                ):
                    raise ValueError("sandbox mount roots are not fully disjoint")
                data_identity = (
                    absent_data_identity
                    if data_access is None
                    else _directory_identity(data_access.path, data_access.path.lstat())
                )
                output_path, output_info = self._create_or_load_output(
                    output_parent=output_access,
                    source_path=source_access.path,
                    source_identity=source_binding,
                    data_identity=data_identity,
                    execution_key=execution_key,
                    output_authority=authorities[2],
                )
                source_info = source_access.path.lstat()
                data_path = None if data_access is None else data_access.path
                data_info = None if data_path is None else data_path.lstat()
                source_path = source_access.path
        except (OSError, ValueError):
            raise ValueError("sandbox mount authority changed during issuance") from None
        source_handle = self._mount_handle(
            "source",
            source_path,
            source_info,
            authorities[0],
            source_binding,
            container_path="/pit/candidate",
        )
        output_handle = self._mount_handle(
            "output",
            output_path,
            output_info,
            authorities[2],
            _directory_identity(output_path, output_info),
        )
        if semantic_probe:
            if authorities[1] is not None or data_path is not None or data_info is not None:
                raise ValueError("sandbox semantic probe received a data authority")
            return (source_handle, output_handle)
        if authorities[1] is None or data_path is None or data_info is None:
            raise ValueError("sandbox panel data authority is unavailable")
        return (
            source_handle,
            self._mount_handle(
                "data",
                data_path,
                data_info,
                authorities[1],
                canonical_sha256_v5(self._data_file_identities),
            ),
            output_handle,
        )

    def _authenticate_data_files(self, access: object | None = None) -> None:
        if access is None:
            try:
                with acquire_absolute_directory_v5(
                    self._data_root,
                    expected_identity=(self._data_info.st_dev, self._data_info.st_ino),
                ) as pinned:
                    observed = tuple(
                        hash_regular_in_directory_v5(
                            pinned,
                            name,
                            maximum_bytes=maximum,
                        )
                        for name, maximum in zip(
                            _DATA_FILES_V5,
                            _DATA_FILE_MAXIMUM_BYTES_V5,
                            strict=True,
                        )
                    )
            except (OSError, ValueError):
                raise ValueError("sandbox data files changed") from None
        else:
            observed = tuple(
                hash_regular_in_directory_v5(  # type: ignore[arg-type]
                    access,
                    name,
                    maximum_bytes=maximum,
                )
                for name, maximum in zip(
                    _DATA_FILES_V5,
                    _DATA_FILE_MAXIMUM_BYTES_V5,
                    strict=True,
                )
            )
        if observed != self._data_file_identities:
            raise ValueError("sandbox data files changed")

    def _create_or_load_output(
        self,
        *,
        output_parent: object,
        source_path: Path,
        source_identity: str,
        data_identity: str,
        execution_key: CandidateExecutionKeyV5,
        output_authority: str,
    ) -> tuple[Path, os.stat_result]:
        relative = f"pit-v5-output-{output_authority[:24]}"
        if getattr(output_parent, "path", None) != self._output_root or getattr(
            output_parent, "identity", None
        ) != (self._output_info.st_dev, self._output_info.st_ino):
            raise ValueError("sandbox output parent authority is foreign")
        record = MountReservationRecordV5(
            5,
            self.mount_identity_sha256,
            self._owner,
            canonical_sha256_v5(execution_key),
            output_authority,
            relative,
            _directory_identity(self._output_root, self._output_info),
            source_identity,
            data_identity,
        )
        with self._repository.adapter_state_transition(namespace="sandbox-mount", key=output_authority):
            prior = self._repository.load_typed_state(
                namespace="sandbox-mount-reservation",
                key=output_authority,
                value_type=MountReservationRecordV5,
            )
            if prior is None:
                self._repository.append_typed_state(
                    namespace="sandbox-mount-reservation",
                    key=output_authority,
                    value=record,
                )
            elif prior != record:
                raise ValueError("sandbox output reservation is foreign")
            created = self._repository.load_typed_state(
                namespace="sandbox-mount-created",
                key=output_authority,
                value_type=MountCreatedRecordV5,
            )
            ready = self._repository.load_typed_state(
                namespace="sandbox-mount-ready",
                key=output_authority,
                value_type=MountReadyRecordV5,
            )
            if created is not None and (
                created.factory_identity_sha256 != self.mount_identity_sha256
                or created.output_authority_sha256 != output_authority
            ):
                raise ValueError("sandbox output creation authority is foreign")
            if ready is not None and (
                created is None
                or ready.factory_identity_sha256 != self.mount_identity_sha256
                or ready.output_authority_sha256 != output_authority
                or (ready.output_device, ready.output_inode)
                != (created.output_device, created.output_inode)
            ):
                raise ValueError("sandbox output readiness is foreign")
            created_identity: tuple[int, int] | None = None
            created_durable: bool | None = created is not None
            try:
                with ExitStack() as stack:
                    if created is None:
                        try:
                            target_access = stack.enter_context(
                                create_directory_in_directory_v5(
                                    output_parent,  # type: ignore[arg-type]
                                    relative,
                                )
                            )
                        except FileExistsError:
                            raise ValueError(
                                "sandbox output target predates durable creation"
                            ) from None
                        created_identity = target_access.identity
                        expected_created = MountCreatedRecordV5(
                            5,
                            self.mount_identity_sha256,
                            output_authority,
                            target_access.identity[0],
                            target_access.identity[1],
                        )
                        try:
                            self._repository.append_typed_state(
                                namespace="sandbox-mount-created",
                                key=output_authority,
                                value=expected_created,
                            )
                        except BaseException:
                            try:
                                observed_created = self._repository.load_typed_state(
                                    namespace="sandbox-mount-created",
                                    key=output_authority,
                                    value_type=MountCreatedRecordV5,
                                )
                            except BaseException:
                                created_durable = None
                            else:
                                created_durable = observed_created == expected_created
                            raise
                        created = expected_created
                        created_durable = True
                    else:
                        target_access = stack.enter_context(
                            acquire_directory_v5(
                                self._output_root,
                                (relative,),
                                create=False,
                                expected_root_identity=(
                                    self._output_info.st_dev,
                                    self._output_info.st_ino,
                                ),
                            )
                        )
                        if target_access.identity != (
                            created.output_device,
                            created.output_inode,
                        ):
                            raise ValueError(
                                "sandbox output identity differs from durable creation"
                            )
                    target_path = target_access.path
                    target_info = target_path.lstat()
                    if (target_info.st_dev, target_info.st_ino) != target_access.identity:
                        raise ValueError("sandbox output identity changed")
                    identity = _directory_identity(target_path, target_info)
                    if ready is None:
                        if not directory_is_empty_v5(target_access):
                            raise ValueError("unready sandbox output is not empty")
                        self._repository.append_typed_state(
                            namespace="sandbox-mount-ready",
                            key=output_authority,
                            value=MountReadyRecordV5(
                                5,
                                self.mount_identity_sha256,
                                output_authority,
                                identity,
                                target_access.identity[0],
                                target_access.identity[1],
                            ),
                        )
                    elif (
                        ready.factory_identity_sha256 != self.mount_identity_sha256
                        or ready.output_authority_sha256 != output_authority
                        or ready.output_root_identity_sha256 != identity
                        or (ready.output_device, ready.output_inode)
                        != target_access.identity
                    ):
                        raise ValueError("sandbox output readiness is foreign")
                    if _roots_overlap(source_path, target_path):
                        raise ValueError("sandbox source and output roots overlap")
                    return target_path, target_info
            except BaseException:
                rollback_failed = False
                if created_identity is not None and created_durable is False:
                    try:
                        remove_owned_tree_in_directory_v5(
                            output_parent,  # type: ignore[arg-type]
                            relative,
                            expected_identity=created_identity,
                        )
                    except (OSError, ValueError):
                        rollback_failed = True
                if rollback_failed:
                    raise ValueError("sandbox output creation rollback failed") from None
                raise ValueError("sandbox output could not be safely created or opened") from None

    def _mount_handle(
        self,
        kind: Literal["source", "data", "output"],
        path: Path,
        info: os.stat_result,
        content_authority: str,
        binding: str,
        *,
        container_path: str | None = None,
    ) -> SandboxMountHandleV5:
        capability = _MountCapabilityV5(
            self.mount_identity_sha256,
            kind,
            str(path),
            info.st_dev,
            info.st_ino,
            content_authority,
            binding,
        )
        return SandboxMountHandleV5(
            kind,
            "bounded_write_only" if kind == "output" else "read_only",
            _directory_identity(path, info),
            content_authority,
            str(path),
            container_path
            or {"source": "/pit/candidate", "data": "/pit/data", "output": "/pit/output"}[kind],
            self._manifest.resources.evaluation_output_limit_bytes if kind == "output" else None,
            capability,
        )

    def authenticate_handle(self, handle: SandboxMountHandleV5) -> Path:
        """Reauthenticate one issued handle immediately before Docker argv construction."""

        with self._pin_handle(handle) as access:
            return access.path

    @contextmanager
    def _pin_handle(self, handle: SandboxMountHandleV5) -> Iterator[object]:
        capability = handle.opaque_handle
        if (
            type(handle) is not SandboxMountHandleV5
            or type(capability) is not _MountCapabilityV5
            or capability.factory_identity_sha256 != self.mount_identity_sha256
            or capability.kind != handle.kind
            or capability.content_authority_sha256 != handle.content_authority_sha256
            or _windows_key(capability.path) != _windows_key(handle.host_path)
        ):
            raise ValueError("sandbox mount handle is foreign")
        try:
            with ExitStack() as stack:
                access = stack.enter_context(
                    acquire_absolute_directory_v5(
                        Path(capability.path),
                        expected_identity=(capability.device, capability.inode),
                    )
                )
                info = access.path.lstat()
                if (
                    (info.st_dev, info.st_ino) != access.identity
                    or handle.root_identity_sha256 != _directory_identity(access.path, info)
                ):
                    raise ValueError("sandbox mount root changed")
                if handle.kind == "data":
                    observed = []
                    for name, maximum in zip(
                        _DATA_FILES_V5,
                        _DATA_FILE_MAXIMUM_BYTES_V5,
                        strict=True,
                    ):
                        stream, file_info = open_regular_in_directory_v5(
                            access,
                            name,
                            writable=False,
                        )
                        stack.enter_context(stream)
                        observed.append(
                            _hash_open_stream(
                                stream,
                                file_info,
                                maximum_bytes=maximum,
                            )
                        )
                    if tuple(observed) != self._data_file_identities:
                        raise ValueError("sandbox data files changed")
                    if capability.binding_sha256 != canonical_sha256_v5(
                        self._data_file_identities
                    ):
                        raise ValueError("sandbox data binding is foreign")
                yield access
        except (OSError, ValueError):
            raise ValueError("sandbox mount root changed") from None

    def _authorize_request(self, request: DockerPanelRequestV5) -> None:
        if (
            type(request) is not DockerPanelRequestV5
            or request.manifest != self._manifest
            or request.evaluator_contract != self._contract
            or request.sandbox_profile != self._profile
            or request.owner != self._owner
        ):
            raise ValueError("sandbox request differs from mount factory authority")

    @contextmanager
    def pinned_request(
        self,
        request: DockerPanelRequestV5,
    ) -> Iterator[tuple[Path, ...]]:
        """Hold every mount ancestry stable across one Docker engine boundary."""

        self._authorize_request(request)
        with ExitStack() as stack:
            handles = (
                (request.source_mount, request.output_mount)
                if request.data_mount is None
                else (request.source_mount, request.data_mount, request.output_mount)
            )
            accesses = tuple(
                stack.enter_context(self._pin_handle(handle))
                for handle in handles
            )
            paths = tuple(access.path for access in accesses)
            if any(
                _roots_overlap(first, second)
                for index, first in enumerate(paths)
                for second in paths[index + 1 :]
            ):
                raise ValueError("sandbox request mount roots overlap")
            access_by_kind = dict(zip((item.kind for item in handles), accesses, strict=True))
            path_by_kind = {kind: access.path for kind, access in access_by_kind.items()}
            expected_sources = request.policy_revision.editable_source_sha256
            observed_sources = []
            for relative, _expected in expected_sources:
                parts = _closed_child_parts(relative)
                try:
                    parent = stack.enter_context(
                        acquire_directory_v5(
                            paths[0],
                            parts[:-1],
                            create=False,
                            expected_root_identity=access_by_kind["source"].identity,
                        )
                    )
                    stream, info = open_regular_in_directory_v5(
                        parent,
                        parts[-1],
                        writable=False,
                    )
                    stack.enter_context(stream)
                    observed = _hash_open_stream(
                        stream,
                        info,
                        maximum_bytes=_POLICY_SOURCE_MAXIMUM_BYTES_V5,
                    )
                    observed_sources.append((relative, observed[3]))
                except (OSError, ValueError):
                    raise ValueError("sandbox source path changed") from None
            if tuple(observed_sources) != expected_sources:
                raise ValueError("sandbox source bytes changed after mount issuance")
            output_created = self._repository.load_typed_state(
                namespace="sandbox-mount-created",
                key=request.output_mount.content_authority_sha256,
                value_type=MountCreatedRecordV5,
            )
            output_ready = self._repository.load_typed_state(
                namespace="sandbox-mount-ready",
                key=request.output_mount.content_authority_sha256,
                value_type=MountReadyRecordV5,
            )
            if (
                output_created is None
                or output_ready is None
                or output_created.factory_identity_sha256 != self.mount_identity_sha256
                or output_created.output_authority_sha256
                != request.output_mount.content_authority_sha256
                or output_ready.factory_identity_sha256 != self.mount_identity_sha256
                or output_ready.output_authority_sha256
                != request.output_mount.content_authority_sha256
                or output_ready.output_root_identity_sha256
                != request.output_mount.root_identity_sha256
                or (output_created.output_device, output_created.output_inode)
                != access_by_kind["output"].identity
                or (output_ready.output_device, output_ready.output_inode)
                != access_by_kind["output"].identity
            ):
                raise ValueError("sandbox output mount is not durably ready")
            pinned_paths = tuple(path_by_kind[item.kind] for item in handles)
            if request.execution_key.stage != "semantic_probe":
                panel_input, reference = _panel_input_reference_v5(request)
                recorded = self._repository.load_typed_state(
                    namespace=_PANEL_INPUT_NAMESPACE_V5,
                    key=request.sha256,
                    value_type=PanelExecutionRequestV5,
                    repair=False,
                )
                if recorded != panel_input:
                    raise ValueError("sandbox panel input is not durably authenticated")
                parts = PurePosixPath(reference.relative_path).parts
                try:
                    repository_root = stack.enter_context(
                        acquire_absolute_directory_v5(self._repository.root)
                    )
                    if self._repository.root_identity_sha256 != canonical_sha256_v5(
                        {
                            "device": repository_root.identity[0],
                            "inode": repository_root.identity[1],
                        }
                    ):
                        raise ValueError("artifact repository root changed")
                    parent = stack.enter_context(
                        acquire_directory_v5(
                            self._repository.root,
                            tuple(parts[:-1]),
                            create=False,
                            expected_root_identity=repository_root.identity,
                        )
                    )
                    stream, info = open_regular_in_directory_v5(
                        parent,
                        parts[-1],
                        writable=False,
                    )
                    stack.enter_context(stream)
                    observed = _hash_open_stream(
                        stream,
                        info,
                        maximum_bytes=_PANEL_INPUT_MAXIMUM_BYTES_V5,
                    )
                except (OSError, ValueError):
                    raise ValueError("sandbox panel input changed") from None
                if observed[3] != reference.sha256:
                    raise ValueError("sandbox panel input bytes changed")
                input_path = parent.path / parts[-1]
                if any(_roots_overlap(input_path, path) for path in pinned_paths):
                    raise ValueError("sandbox panel input overlaps another mount")
                pinned_paths = (*pinned_paths, input_path)
            yield pinned_paths

    def authenticate_request(
        self,
        request: DockerPanelRequestV5,
    ) -> tuple[Path, ...]:
        """Reauthenticate every mount and its request-bound source/data authority."""

        with self.pinned_request(request) as paths:
            return paths


class LocalContainerExecutorV5(ContainerExecutorV5):
    """Durable create/start/collect Docker executor with deterministic ownership."""

    def __init__(
        self,
        *,
        manifest: CampaignManifestV5,
        sandbox_profile: SandboxProfileV5,
        owner: WorkspaceOwnerV5,
        mount_factory: LocalSandboxMountFactoryV5,
        docker_executable: Path,
        control_root: Path,
        repository: LocalArtifactRepositoryV5,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError(
                "the concrete V5 Docker executor requires Windows handle authority"
            )
        if (
            type(manifest) is not CampaignManifestV5
            or type(sandbox_profile) is not SandboxProfileV5
            or type(owner) is not WorkspaceOwnerV5
            or type(mount_factory) is not LocalSandboxMountFactoryV5
            or type(repository) is not LocalArtifactRepositoryV5
            or not isinstance(docker_executable, Path)
            or not isinstance(control_root, Path)
            or owner.campaign_id != manifest.campaign_id
            or manifest.sandbox_profile_ref.sha256 != sandbox_profile.sha256
        ):
            raise ValueError("local container executor configuration is invalid")
        validate_sandbox_profile_resources_v5(sandbox_profile, manifest.resources)
        executable_identity = _hash_docker_executable(docker_executable)
        control_path, control_info = _canonical_directory(control_root)
        if any(_roots_overlap(control_path, root) for root in mount_factory.fixed_roots):
            raise ValueError("container control root overlaps a fixed sandbox root")
        base_environment = tuple(
            sorted(
                (key, os.environ[key])
                for key in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE"}
                if key in os.environ
            )
        )
        self._manifest = manifest
        self._profile = sandbox_profile
        self._owner = owner
        self._mount_factory = mount_factory
        self._docker_executable = docker_executable
        self._docker_identity = executable_identity
        self._control_root = control_path
        self._control_root_info = control_info
        self._repository = repository
        self._base_environment = base_environment
        self._control_capability = object()
        self._executor_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-local-container-executor-v1",
                "manifest_sha256": manifest.sha256,
                "sandbox_profile_sha256": sandbox_profile.sha256,
                "owner_sha256": owner.sha256,
                "mount_factory_sha256": mount_factory.mount_identity_sha256,
                "docker_executable_identity": executable_identity,
                "control_root_identity": _directory_identity(control_path, control_info),
                "control_environment": base_environment,
                "runtime_contract": {
                    "phases": ("create", "start", "wait", "collect", "targeted_cleanup"),
                    "pull": "never",
                    "user": "65532:65532",
                    "entrypoint": "python",
                    "python_flags": ("-P", "-B"),
                    "workdirs": (
                        ("semantic_probe", "/"),
                        ("panel_evaluation", "/"),
                    ),
                    "mount_layout": (
                        ("semantic_probe", "four_policy_files_and_output"),
                        (
                            "panel_evaluation",
                            "four_policy_files_two_data_files_request_and_output",
                        ),
                    ),
                    "output_names": (
                        ("semantic_probe", "semantic-fingerprint.json"),
                        ("panel_evaluation", "panel-evaluation.json"),
                    ),
                    "shell": False,
                },
                "repository_root_identity_sha256": repository.root_identity_sha256,
            }
        )

    @property
    def executor_identity_sha256(self) -> str:
        return self._executor_identity_sha256

    @property
    def mount_factory(self) -> LocalSandboxMountFactoryV5:
        return self._mount_factory

    def reserve(self, command: ContainerCommandV5) -> ExecutionReservationV5:
        self._authenticate_command(command, persist_panel_input=True)
        record = self._reservation_record(command)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command.sha256):
            prior = self._load_reservation(command.sha256)
            created = prior is None
            if created:
                control_target = self._control_root / record.control_relative_path
                if _lstat_optional(control_target) is not None:
                    raise ValueError("container control path predates reservation")
                self._repository.append_typed_state(
                    namespace="container-reservation",
                    key=command.sha256,
                    value=record,
                )
            elif prior != record:
                raise ValueError("container execution reservation is foreign")
        return self._runtime_reservation(command, "created" if created else "existing")

    def _ensure_panel_input(self, request: DockerPanelRequestV5) -> ArtifactRefV5 | None:
        if request.execution_key.stage == "semantic_probe":
            if request.panel_spec is not None:
                raise ValueError("semantic probe unexpectedly carries a panel input")
            return None
        panel_input, expected = _panel_input_reference_v5(request)
        stored = self._repository.append_typed_state(
            namespace=_PANEL_INPUT_NAMESPACE_V5,
            key=request.sha256,
            value=panel_input,
        )
        if stored != expected:
            raise ValueError("container panel input persistence differs from authority")
        return stored

    def _authenticate_command(
        self,
        command: ContainerCommandV5,
        *,
        persist_panel_input: bool = False,
    ) -> tuple[Path, ...]:
        if (
            type(command) is not ContainerCommandV5
            or type(persist_panel_input) is not bool
            or command.request.owner != self._owner
            or command.request.manifest != self._manifest
            or command.request.sandbox_profile != self._profile
            or command.argv != build_docker_argv_v5(command.request)
        ):
            raise ValueError("container command differs from executor authority")
        if persist_panel_input:
            self._ensure_panel_input(command.request)
        paths = self._mount_factory.authenticate_request(command.request)
        if any(_roots_overlap(self._control_root, path) for path in paths):
            raise ValueError("container control root overlaps a sandbox mount")
        self._authenticate_executable()
        return paths

    def _authenticate_executable(self) -> None:
        if _hash_docker_executable(self._docker_executable) != self._docker_identity:
            raise ValueError("Docker executable identity changed")

    def _container_name(self, command_sha256: str) -> str:
        return f"pit-v5-{command_sha256[:40]}"

    def _runtime_argv(self, command: ContainerCommandV5) -> tuple[str, ...]:
        generic = list(command.argv)
        if generic[:3] != ["docker", "run", "--rm"]:
            raise ValueError("container command does not use the closed V5 grammar")
        generic[0] = str(self._docker_executable)
        generic[1] = "create"
        del generic[2]
        injected = [
            "--name",
            self._container_name(command.sha256),
            "--label",
            f"pit-v5.executor={self.executor_identity_sha256}",
            "--label",
            f"pit-v5.command={command.sha256}",
            "--label",
            f"pit-v5.owner={self._owner.sha256}",
            "--pull",
            "never",
            "--hostname",
            self._container_name(command.sha256),
            "--runtime",
            "runc",
            "--restart",
            "no",
            "--memory-swap",
            f"{self._profile.memory_limit_mib}m",
            "--no-healthcheck",
            "--stop-signal",
            "SIGTERM",
            "--stop-timeout",
            "10",
            "--privileged=false",
            "--ipc",
            "private",
            "--cgroupns",
            "private",
            "--oom-kill-disable=false",
            "--init=false",
            "--publish-all=false",
            "--log-driver",
            "none",
            "--shm-size",
            "16m",
            "--user",
            "65532:65532",
            "--entrypoint",
            "python",
            "--workdir",
            "/",
        ]
        generic[2:2] = injected
        data_mount = command.request.data_mount
        if command.request.execution_key.stage == "semantic_probe":
            if data_mount is not None or any("/pit/data" in item for item in generic):
                raise ValueError("semantic probe Docker grammar exposes PIT data")
        else:
            if type(data_mount) is not SandboxMountHandleV5:
                raise ValueError("panel Docker grammar lacks its data authority")
            data_root_argument = (
                f"type=bind,src={data_mount.host_path},dst={data_mount.container_path},readonly"
            )
            try:
                data_argument_index = generic.index(data_root_argument)
            except ValueError:
                raise ValueError("container data mount differs from the closed V5 grammar") from None
            data_file_arguments = []
            for name in _DATA_FILES_V5:
                data_file_arguments.extend(
                    (
                        "--mount",
                        (
                            f"type=bind,src={Path(data_mount.host_path) / name},"
                            f"dst={data_mount.container_path}/{name},readonly"
                        ),
                    )
                )
            if data_argument_index == 0 or generic[data_argument_index - 1] != "--mount":
                raise ValueError("container data mount grammar is malformed")
            generic[data_argument_index - 1 : data_argument_index + 1] = data_file_arguments
            _panel_input, input_reference = _panel_input_reference_v5(command.request)
            input_path = self._repository.root.joinpath(
                *PurePosixPath(input_reference.relative_path).parts
            )
            input_host_path = _docker_host_path_v5(input_path)
            separator = generic.index("--")
            generic[separator:separator] = [
                "--mount",
                (
                    f"type=bind,src={input_host_path},"
                    f"dst={_PANEL_INPUT_TARGET_V5},readonly"
                ),
            ]
        separator = generic.index("--")
        if generic[separator + 1] != self._profile.image_reference or generic[separator + 2] != "python":
            raise ValueError("container image command differs from the closed V5 grammar")
        del generic[separator + 2]
        return tuple(generic)

    def _reservation_record(self, command: ContainerCommandV5) -> ExecutionReservationRecordV5:
        lease_ids = tuple(
            derive_execution_lease_id_v5(command.sha256, role)
            for role in ("evaluator_process", "container")
        )
        runtime_argv = self._runtime_argv(command)
        source_mounts = tuple(
            (
                _windows_key(
                    str(
                        Path(command.request.source_mount.host_path).joinpath(
                            *relative.split("/")
                        )
                    )
                ),
                f"{command.request.source_mount.container_path}/{relative.rsplit('/', 1)[-1]}",
                False,
            )
            for relative in EDITABLE_POLICY_PATHS_V5
        )
        if command.request.execution_key.stage == "semantic_probe":
            if command.request.data_mount is not None:
                raise ValueError("semantic probe reservation exposes PIT data")
            data_mounts: tuple[tuple[str, str, bool], ...] = ()
            input_mounts: tuple[tuple[str, str, bool], ...] = ()
        else:
            data_mount = command.request.data_mount
            if type(data_mount) is not SandboxMountHandleV5:
                raise ValueError("panel reservation lacks its data mount")
            data_mounts = tuple(
                (
                    _windows_key(str(Path(data_mount.host_path) / name)),
                    f"{data_mount.container_path}/{name}",
                    False,
                )
                for name in _DATA_FILES_V5
            )
            _panel_input, input_reference = _panel_input_reference_v5(command.request)
            input_path = self._repository.root.joinpath(
                *PurePosixPath(input_reference.relative_path).parts
            )
            input_mounts = (
                (
                    _windows_key(_docker_host_path_v5(input_path)),
                    _PANEL_INPUT_TARGET_V5,
                    False,
                ),
            )
        expected_mounts = tuple(
            sorted(
                (
                    *source_mounts,
                    *data_mounts,
                    *input_mounts,
                    (
                        _windows_key(command.request.output_mount.host_path),
                        command.request.output_mount.container_path,
                        True,
                    ),
                )
            )
        )
        return ExecutionReservationRecordV5(
            5,
            self.executor_identity_sha256,
            command.sha256,
            command.request.sha256,
            self._owner,
            command.argv,
            canonical_sha256_v5(runtime_argv),
            command.request.output_mount.content_authority_sha256,
            expected_mounts,
            lease_ids,
            self._container_name(command.sha256),
            f"execution-{command.sha256[:24]}",
        )

    def _load_reservation(self, command_sha256: str) -> ExecutionReservationRecordV5 | None:
        record = self._repository.load_typed_state(
            namespace="container-reservation",
            key=command_sha256,
            value_type=ExecutionReservationRecordV5,
        )
        if record is not None and (
            record.executor_identity_sha256 != self.executor_identity_sha256
            or record.command_sha256 != command_sha256
        ):
            raise ValueError("container execution reservation is foreign")
        return record

    def _leases(self, command: ContainerCommandV5) -> tuple[ExecutionLeaseV5, ...]:
        result = []
        for role in ("evaluator_process", "container"):
            lease_id = derive_execution_lease_id_v5(command.sha256, role)
            payload = ResourceLeasePayloadV5(
                lease_id,
                role,
                self._owner.campaign_id,
                self._owner.owner_token_sha256,
            )
            capability = _ExecutionLeaseCapabilityV5(
                self.executor_identity_sha256,
                command.sha256,
                role,
                self._owner.sha256,
                command,
            )
            result.append(
                ExecutionLeaseV5(
                    role,
                    OwnedLeaseV5(payload, self._owner.round_index, capability),
                    command.request.sha256,
                    command.sha256,
                    command.request.output_mount.content_authority_sha256,
                )
            )
        return tuple(result)

    def _runtime_reservation(
        self,
        command: ContainerCommandV5,
        disposition: Literal["created", "existing"],
    ) -> ExecutionReservationV5:
        return ExecutionReservationV5(
            command,
            self._leases(command),
            disposition,
            _ExecutionReservationCapabilityV5(self.executor_identity_sha256, command.sha256),
        )

    def _authorize_reservation(self, reservation: ExecutionReservationV5) -> ExecutionReservationRecordV5:
        capability = reservation.opaque_reservation
        if (
            type(reservation) is not ExecutionReservationV5
            or type(capability) is not _ExecutionReservationCapabilityV5
            or capability.executor_identity_sha256 != self.executor_identity_sha256
            or capability.command_sha256 != reservation.command.sha256
            or reservation.leases != self._leases(reservation.command)
        ):
            raise ValueError("container execution handle is foreign")
        self._authenticate_command(reservation.command)
        record = self._load_reservation(reservation.command.sha256)
        if record is None or record != self._reservation_record(reservation.command):
            raise ValueError("container execution reservation is absent or foreign")
        return record

    def _phase(
        self,
        command_sha256: str,
        phase: Literal["launch_claim", "created", "start_claim", "started", "collected"],
    ) -> ExecutionPhaseRecordV5 | None:
        record = self._repository.load_typed_state(
            namespace=f"container-{phase.replace('_', '-')}",
            key=command_sha256,
            value_type=ExecutionPhaseRecordV5,
        )
        if record is not None and (
            record.executor_identity_sha256 != self.executor_identity_sha256
            or record.command_sha256 != command_sha256
            or record.phase != phase
        ):
            raise ValueError("container phase record is foreign")
        return record

    def _append_phase(
        self,
        command_sha256: str,
        phase: Literal["launch_claim", "created", "start_claim", "started", "collected"],
        attestation_sha256: str | None = None,
        *,
        container_identity_sha256: str | None = None,
        lifecycle_status: Literal["created", "running", "exited"] | None = None,
        network_attestation_sha256: str | None = None,
        state_attestation_sha256: str | None = None,
        network_namespace_status: Literal["empty", "private"] | None = None,
    ) -> ExecutionPhaseRecordV5:
        record = ExecutionPhaseRecordV5(
            5,
            self.executor_identity_sha256,
            command_sha256,
            phase,
            attestation_sha256,
            container_identity_sha256,
            lifecycle_status,
            network_attestation_sha256,
            state_attestation_sha256,
            network_namespace_status,
        )
        self._repository.append_typed_state(
            namespace=f"container-{phase.replace('_', '-')}",
            key=command_sha256,
            value=record,
        )
        return record

    def _container_identity(
        self,
        command_sha256: str,
    ) -> ExecutionContainerIdentityRecordV5 | None:
        record = self._repository.load_typed_state(
            namespace="container-identity",
            key=command_sha256,
            value_type=ExecutionContainerIdentityRecordV5,
        )
        if record is not None and (
            record.executor_identity_sha256 != self.executor_identity_sha256
            or record.command_sha256 != command_sha256
        ):
            raise ValueError("container identity record is foreign")
        return record

    def _persist_container_identity(
        self,
        reservation: ExecutionReservationRecordV5,
        inspection: _ContainerInspectionV5,
    ) -> ExecutionContainerIdentityRecordV5:
        expected = ExecutionContainerIdentityRecordV5(
            5,
            self.executor_identity_sha256,
            reservation.command_sha256,
            inspection.container_id,
            inspection.created_at,
            inspection.stable_attestation_sha256,
        )
        existing = self._container_identity(reservation.command_sha256)
        if existing is None:
            self._repository.append_typed_state(
                namespace="container-identity",
                key=reservation.command_sha256,
                value=expected,
            )
        elif existing != expected:
            raise ValueError("container identity changed")
        return expected

    def _control_created(
        self,
        command_sha256: str,
    ) -> ExecutionControlCreatedRecordV5 | None:
        created = self._repository.load_typed_state(
            namespace="container-control-created",
            key=command_sha256,
            value_type=ExecutionControlCreatedRecordV5,
        )
        if created is not None and (
            created.executor_identity_sha256 != self.executor_identity_sha256
            or created.command_sha256 != command_sha256
        ):
            raise ValueError("container control creation record is foreign")
        return created

    @staticmethod
    def _control_root_identity(root: object) -> str:
        return canonical_sha256_v5(
            {
                "path": _windows_key(str(root.path)),  # type: ignore[attr-defined]
                "identity": root.identity,  # type: ignore[attr-defined]
            }
        )

    def _control_record(
        self,
        reservation: ExecutionReservationRecordV5,
        *,
        root: object,
        target: object,
        children: dict[str, object],
        config_identity: tuple[int, int, int, str],
    ) -> ExecutionControlRecordV5:
        root_access = root
        target_access = target
        return ExecutionControlRecordV5(
            5,
            self.executor_identity_sha256,
            reservation.command_sha256,
            self._control_root_identity(root_access),
            canonical_sha256_v5(
                {
                    "path": _windows_key(str(target_access.path)),  # type: ignore[attr-defined]
                    "identity": target_access.identity,  # type: ignore[attr-defined]
                }
            ),
            tuple(
                (
                    name,
                    canonical_sha256_v5(
                        {
                            "path": _windows_key(str(children[name].path)),  # type: ignore[attr-defined]
                            "identity": children[name].identity,  # type: ignore[attr-defined]
                        }
                    ),
                )
                for name in _CONTROL_CHILDREN_V5
            ),
            canonical_sha256_v5(
                {
                    "device": config_identity[0],
                    "inode": config_identity[1],
                    "byte_count": config_identity[2],
                    "sha256": config_identity[3],
                }
            ),
            _DOCKER_CONFIG_TEXT_V5,
            canonical_sha256_v5(self._docker_identity),
        )

    def _ensure_control(self, reservation: ExecutionReservationRecordV5) -> Path:
        target = self._control_root / reservation.control_relative_path
        created = self._control_created(reservation.command_sha256)
        ready = self._repository.load_typed_state(
            namespace="container-control-ready",
            key=reservation.command_sha256,
            value_type=ExecutionControlRecordV5,
        )
        if ready is not None and created is None:
            raise ValueError("container control readiness lacks durable creation authority")
        created_identity: tuple[int, int] | None = None
        created_durable: bool | None = created is not None
        try:
            with ExitStack() as stack:
                root = stack.enter_context(
                    acquire_absolute_directory_v5(
                        self._control_root,
                        expected_identity=(
                            self._control_root_info.st_dev,
                            self._control_root_info.st_ino,
                        ),
                    )
                )
                root_identity_sha256 = self._control_root_identity(root)
                if created is not None and (
                    created.control_root_identity_sha256 != root_identity_sha256
                ):
                    raise ValueError("container control creation root is foreign")
                if created is None:
                    try:
                        target_access = stack.enter_context(
                            create_directory_in_directory_v5(
                                root,
                                reservation.control_relative_path,
                            )
                        )
                    except FileExistsError:
                        raise ValueError(
                            "container control directory predates durable authority"
                        ) from None
                    created_identity = target_access.identity
                    expected_created = ExecutionControlCreatedRecordV5(
                        5,
                        self.executor_identity_sha256,
                        reservation.command_sha256,
                        root_identity_sha256,
                        target_access.identity[0],
                        target_access.identity[1],
                    )
                    try:
                        self._repository.append_typed_state(
                            namespace="container-control-created",
                            key=reservation.command_sha256,
                            value=expected_created,
                        )
                    except BaseException:
                        try:
                            observed_created = self._control_created(
                                reservation.command_sha256
                            )
                        except BaseException:
                            created_durable = None
                        else:
                            created_durable = observed_created == expected_created
                        raise
                    created = expected_created
                    created_durable = True
                else:
                    target_access = stack.enter_context(
                        acquire_directory_v5(
                            root.path,
                            (reservation.control_relative_path,),
                            create=False,
                            expected_root_identity=root.identity,
                        )
                    )
                    if target_access.identity != (
                        created.transaction_device,
                        created.transaction_inode,
                    ):
                        raise ValueError("container control identity differs from durable creation")
                    if ready is None:
                        clear_owned_directory_v5(target_access)
                children = {}
                for name in _CONTROL_CHILDREN_V5:
                    if ready is None:
                        child = create_directory_in_directory_v5(target_access, name)
                    else:
                        child = acquire_directory_v5(
                            target_access.path,
                            (name,),
                            create=False,
                            expected_root_identity=target_access.identity,
                        )
                    children[name] = stack.enter_context(child)
                config = children["config"]
                try:
                    config_stream, config_info = open_regular_in_directory_v5(
                        config,  # type: ignore[arg-type]
                        _DOCKER_CONFIG_NAME_V5,
                        writable=False,
                    )
                except FileNotFoundError:
                    if ready is not None or directory_entry_names_v5(config):  # type: ignore[arg-type]
                        raise ValueError("container Docker config is absent or foreign") from None
                    write_new_regular_in_directory_v5(
                        config,  # type: ignore[arg-type]
                        _DOCKER_CONFIG_NAME_V5,
                        _DOCKER_CONFIG_CONTENT_V5,
                    )
                    config_stream, config_info = open_regular_in_directory_v5(
                        config,  # type: ignore[arg-type]
                        _DOCKER_CONFIG_NAME_V5,
                        writable=False,
                    )
                with config_stream:
                    config_identity = _hash_open_stream(
                        config_stream,
                        config_info,
                        maximum_bytes=len(_DOCKER_CONFIG_CONTENT_V5),
                    )
                if (
                    config_identity[2:] != (
                        len(_DOCKER_CONFIG_CONTENT_V5),
                        hashlib.sha256(_DOCKER_CONFIG_CONTENT_V5).hexdigest(),
                    )
                    or directory_entry_names_v5(config) != (_DOCKER_CONFIG_NAME_V5,)  # type: ignore[arg-type]
                ):
                    raise ValueError("container Docker config differs from closed authority")
                expected = self._control_record(
                    reservation,
                    root=root,
                    target=target_access,
                    children=children,
                    config_identity=config_identity,
                )
                if ready is None:
                    self._repository.append_typed_state(
                        namespace="container-control-ready",
                        key=reservation.command_sha256,
                        value=expected,
                    )
                elif ready != expected:
                    raise ValueError("container control directory is foreign")
        except BaseException:
            if created_identity is not None and created_durable is False:
                try:
                    with acquire_absolute_directory_v5(
                        self._control_root,
                        expected_identity=(
                            self._control_root_info.st_dev,
                            self._control_root_info.st_ino,
                        ),
                    ) as parent:
                        remove_owned_tree_in_directory_v5(
                            parent,
                            reservation.control_relative_path,
                            expected_identity=created_identity,
                        )
                except (OSError, ValueError):
                    pass
            raise ValueError("container control directory could not be safely opened") from None
        return target

    @contextmanager
    def _pinned_control_environment(
        self,
        reservation: ExecutionReservationRecordV5,
    ) -> Iterator[tuple[tuple[tuple[str, str], ...], tuple[int, int]]]:
        created = self._control_created(reservation.command_sha256)
        ready = self._repository.load_typed_state(
            namespace="container-control-ready",
            key=reservation.command_sha256,
            value_type=ExecutionControlRecordV5,
        )
        if ready is None or created is None:
            raise ValueError("container control directory is not durably ready")
        with ExitStack() as stack:
            root = stack.enter_context(
                acquire_absolute_directory_v5(
                    self._control_root,
                    expected_identity=(
                        self._control_root_info.st_dev,
                        self._control_root_info.st_ino,
                    ),
                )
            )
            target = stack.enter_context(
                acquire_directory_v5(
                    root.path,
                    (reservation.control_relative_path,),
                    create=False,
                    expected_root_identity=root.identity,
                )
            )
            if (
                created.control_root_identity_sha256 != self._control_root_identity(root)
                or target.identity
                != (created.transaction_device, created.transaction_inode)
            ):
                raise ValueError("container control creation authority changed")
            children = {}
            for name in _CONTROL_CHILDREN_V5:
                child = stack.enter_context(
                    acquire_directory_v5(
                        target.path,
                        (name,),
                        create=False,
                        expected_root_identity=target.identity,
                    )
                )
                children[name] = child
            config = children["config"]
            config_stream, config_info = open_regular_in_directory_v5(
                config,
                _DOCKER_CONFIG_NAME_V5,
                writable=False,
            )
            stack.enter_context(config_stream)
            config_identity = _hash_open_stream(
                config_stream,
                config_info,
                maximum_bytes=len(_DOCKER_CONFIG_CONTENT_V5),
            )
            expected = self._control_record(
                reservation,
                root=root,
                target=target,
                children=children,
                config_identity=config_identity,
            )
            environment = dict(self._base_environment)
            environment.update(
                {
                    "HOME": str(children["home"].path),
                    "USERPROFILE": str(children["home"].path),
                    "DOCKER_CONFIG": str(children["config"].path),
                    "TEMP": str(children["tmp"].path),
                    "TMP": str(children["tmp"].path),
                }
            )
            environment_tuple = tuple(sorted(environment.items()))
            try:
                if (
                    ready != expected
                    or config_identity[2:] != (
                        len(_DOCKER_CONFIG_CONTENT_V5),
                        hashlib.sha256(_DOCKER_CONFIG_CONTENT_V5).hexdigest(),
                    )
                    or directory_entry_names_v5(config) != (_DOCKER_CONFIG_NAME_V5,)
                ):
                    raise ValueError("container control environment differs from durable authority")
                yield environment_tuple, target.identity
            finally:
                after_config = _hash_open_stream(
                    config_stream,
                    config_info,
                    maximum_bytes=len(_DOCKER_CONFIG_CONTENT_V5),
                )
                if (
                    after_config != config_identity
                    or directory_entry_names_v5(config) != (_DOCKER_CONFIG_NAME_V5,)
                    or self._control_record(
                        reservation,
                        root=root,
                        target=target,
                        children=children,
                        config_identity=after_config,
                    )
                    != ready
                ):
                    raise ValueError("container control environment changed during transaction")
                for access in (*children.values(), target, root):
                    access.assert_current()

    @contextmanager
    def _pinned_docker_executable(self) -> Iterator[None]:
        with acquire_absolute_directory_v5(self._docker_executable.parent) as parent:
            stream, info = open_regular_in_directory_v5(
                parent,
                self._docker_executable.name,
                writable=False,
            )
            with stream:
                observed = _hash_open_stream(
                    stream,
                    info,
                    maximum_bytes=_DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5,
                )
                if observed != self._docker_identity:
                    raise ValueError("Docker executable identity changed")
                try:
                    yield
                finally:
                    after = _hash_open_stream(
                        stream,
                        info,
                        maximum_bytes=_DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5,
                    )
                    if (
                        after != self._docker_identity
                        or _hash_docker_executable(self._docker_executable)
                        != self._docker_identity
                    ):
                        raise ValueError("Docker executable identity changed")
                    parent.assert_current()

    @contextmanager
    def _pinned_control_transaction(
        self,
        reservation: ExecutionReservationRecordV5,
    ) -> Iterator[_PinnedControlTransactionV5]:
        """Hold every Docker control capability across a multi-command operation."""

        with self._pinned_control_environment(reservation) as pinned:
            environment, target_identity = pinned
            with self._pinned_docker_executable():
                yield _PinnedControlTransactionV5(
                    self.executor_identity_sha256,
                    reservation.command_sha256,
                    target_identity,
                    environment,
                    self._control_capability,
                )

    def _control(
        self,
        transaction: _PinnedControlTransactionV5,
        arguments: tuple[str, ...],
        *,
        timeout: float,
        output_limit: int = 1024 * 1024,
    ) -> object:
        if (
            type(transaction) is not _PinnedControlTransactionV5
            or transaction.executor_identity_sha256 != self.executor_identity_sha256
            or transaction.opaque_capability is not self._control_capability
            or type(arguments) is not tuple
            or not arguments
            or any(type(item) is not str or not item or "\x00" in item for item in arguments)
            or type(timeout) is not float
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Docker control invocation is invalid")
        from agent_loop import _bounded_process
        result = _bounded_process(
            (str(self._docker_executable), *arguments),
            env=dict(transaction.environment),
            timeout=timeout,
            output_limit=output_limit,
        )
        return result

    @staticmethod
    def _successful(result: object) -> bool:
        return (
            type(getattr(result, "returncode", None)) is int
            and result.returncode == 0
            and getattr(result, "timed_out", None) is False
            and type(getattr(result, "stdout", None)) is str
        )

    def _inspect_image(
        self,
        transaction: _PinnedControlTransactionV5,
    ) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]:
        result = self._control(
            transaction,
            ("image", "inspect", self._profile.image_reference),
            timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
        )
        if not self._successful(result):
            raise ValueError("sandbox image could not be inspected")
        try:
            values = json.loads(result.stdout)
            item = values[0]
            image_id = item["Id"]
            repo_digests = item["RepoDigests"]
            image_config = item["Config"]
            image_environment = image_config["Env"]
            image_labels = image_config.get("Labels")
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            raise ValueError("sandbox image inspection is malformed") from None
        if (
            type(image_id) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
            or type(repo_digests) is not list
            or self._profile.image_reference not in repo_digests
            or type(image_config) is not dict
            or type(image_environment) is not list
            or any(
                type(value) is not str or "=" not in value or "\x00" in value
                for value in image_environment
            )
            or len({value.split("=", 1)[0] for value in image_environment})
            != len(image_environment)
            or image_labels
            != {
                EVALUATOR_RUNTIME_KIND_LABEL_V5: EVALUATOR_RUNTIME_KIND_V5,
                EVALUATOR_RUNTIME_SOURCE_LABEL_V5: self._profile.runtime_source_sha256,
            }
        ):
            raise ValueError("sandbox image differs from its immutable authority")
        return (
            image_id,
            tuple(image_environment),
            tuple(sorted(image_labels.items())),
        )

    @staticmethod
    def _closed_config_authority(
        config: object,
        *,
        container_name: str,
        image_reference: str,
        expected_environment: tuple[str, ...],
        expected_command: list[str],
        expected_labels: dict[str, str],
        expected_working_directory: str,
    ) -> dict[str, object]:
        if type(config) is not dict:
            raise ValueError("container config is malformed")
        expected: dict[str, object] = {
            "Hostname": container_name,
            "Domainname": "",
            "User": "65532:65532",
            "AttachStdin": False,
            "AttachStdout": True,
            "AttachStderr": True,
            "ExposedPorts": None,
            "Tty": False,
            "OpenStdin": False,
            "StdinOnce": False,
            "Env": list(expected_environment),
            "Cmd": expected_command,
            "Healthcheck": {"Test": ["NONE"]},
            "ArgsEscaped": False,
            "Image": image_reference,
            "Volumes": None,
            "WorkingDir": expected_working_directory,
            "Entrypoint": ["python"],
            "NetworkDisabled": False,
            "MacAddress": "",
            "OnBuild": None,
            "Labels": expected_labels,
            "StopSignal": "SIGTERM",
            "StopTimeout": 10,
            "Shell": None,
        }
        if set(config) != set(expected):
            raise ValueError("container config contains an open execution field")
        normalized = {key: config[key] for key in expected}
        if normalized != expected:
            raise ValueError("container config differs from executor authority")
        return normalized

    @staticmethod
    def _closed_host_mount_authority(
        mounts: object,
        expected_mounts: tuple[tuple[str, str, bool], ...],
    ) -> tuple[tuple[str, str, bool], ...]:
        if type(mounts) is not list or len(mounts) != len(expected_mounts):
            raise ValueError("container HostConfig mount count differs")
        actual: set[tuple[str, str, bool]] = set()
        allowed = {
            "Type",
            "Source",
            "Target",
            "ReadOnly",
            "Consistency",
            "BindOptions",
            "VolumeOptions",
            "TmpfsOptions",
            "ImageOptions",
        }
        for mount in mounts:
            if type(mount) is not dict or set(mount) - allowed:
                raise ValueError("container HostConfig mount is open or malformed")
            bind_options = mount.get("BindOptions")
            if bind_options is None:
                bind_options = {}
            if type(bind_options) is not dict or set(bind_options) - {
                "Propagation",
                "NonRecursive",
                "CreateMountpoint",
                "ReadOnlyNonRecursive",
                "ReadOnlyForceRecursive",
            }:
                raise ValueError("container bind options are open or malformed")
            normalized_bind = {
                "Propagation": bind_options.get("Propagation", "rprivate"),
                "NonRecursive": bind_options.get("NonRecursive", False),
                "CreateMountpoint": bind_options.get("CreateMountpoint", False),
                "ReadOnlyNonRecursive": bind_options.get("ReadOnlyNonRecursive", False),
                "ReadOnlyForceRecursive": bind_options.get("ReadOnlyForceRecursive", False),
            }
            if (
                normalized_bind
                != {
                    "Propagation": "rprivate",
                    "NonRecursive": False,
                    "CreateMountpoint": False,
                    "ReadOnlyNonRecursive": False,
                    "ReadOnlyForceRecursive": False,
                }
                or mount.get("Type") != "bind"
                or type(mount.get("Source")) is not str
                or type(mount.get("Target")) is not str
                or type(mount.get("ReadOnly")) is not bool
                or mount.get("Consistency", "") not in {"", "default"}
                or mount.get("VolumeOptions") is not None
                or mount.get("TmpfsOptions") is not None
                or mount.get("ImageOptions") is not None
            ):
                raise ValueError("container HostConfig mount differs from executor authority")
            actual.add(
                (
                    _windows_key(mount["Source"]),
                    mount["Target"],
                    not mount["ReadOnly"],
                )
            )
        if actual != set(expected_mounts) or len(actual) != len(expected_mounts):
            raise ValueError("container HostConfig mounts differ from executor authority")
        return tuple(sorted(actual))

    def _closed_host_authority(
        self,
        host: object,
        *,
        expected_mounts: tuple[tuple[str, str, bool], ...],
    ) -> dict[str, object]:
        if type(host) is not dict:
            raise ValueError("container HostConfig is malformed")
        mount_authority = self._closed_host_mount_authority(host.get("Mounts"), expected_mounts)
        masked_paths = [
            "/proc/asound",
            "/proc/acpi",
            "/proc/kcore",
            "/proc/keys",
            "/proc/latency_stats",
            "/proc/timer_list",
            "/proc/timer_stats",
            "/proc/sched_debug",
            "/proc/scsi",
            "/sys/firmware",
            "/sys/devices/virtual/powercap",
        ]
        readonly_paths = [
            "/proc/bus",
            "/proc/fs",
            "/proc/irq",
            "/proc/sys",
            "/proc/sysrq-trigger",
        ]
        expected: dict[str, object] = {
            "Binds": [],
            "ContainerIDFile": "",
            "LogConfig": {"Type": "none", "Config": {}},
            "NetworkMode": "none",
            "PortBindings": {},
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "AutoRemove": False,
            "VolumeDriver": "",
            "VolumesFrom": [],
            "ConsoleSize": [0, 0],
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "CgroupnsMode": "private",
            "Dns": [],
            "DnsOptions": [],
            "DnsSearch": [],
            "ExtraHosts": [],
            "GroupAdd": [],
            "IpcMode": "private",
            "Cgroup": "",
            "Links": [],
            "OomScoreAdj": 0,
            "PidMode": "",
            "Privileged": False,
            "PublishAllPorts": False,
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"],
            "UTSMode": "",
            "UsernsMode": "",
            "ShmSize": _CONTAINER_SHM_SIZE_BYTES_V5,
            "Runtime": "runc",
            "Isolation": "",
            "CpuShares": 0,
            "Memory": self._profile.memory_limit_mib * 1024 * 1024,
            "NanoCpus": int(self._profile.cpu_limit * Decimal(1_000_000_000)),
            "CgroupParent": "",
            "BlkioWeight": 0,
            "BlkioWeightDevice": [],
            "BlkioDeviceReadBps": [],
            "BlkioDeviceWriteBps": [],
            "BlkioDeviceReadIOps": [],
            "BlkioDeviceWriteIOps": [],
            "CpuPeriod": 0,
            "CpuQuota": 0,
            "CpuRealtimePeriod": 0,
            "CpuRealtimeRuntime": 0,
            "CpusetCpus": "",
            "CpusetMems": "",
            "Devices": [],
            "DeviceCgroupRules": [],
            "DeviceRequests": [],
            "MemoryReservation": 0,
            "MemorySwap": self._profile.memory_limit_mib * 1024 * 1024,
            "MemorySwappiness": None,
            "OomKillDisable": False,
            "PidsLimit": self._profile.pid_limit,
            "Ulimits": [],
            "CpuCount": 0,
            "CpuPercent": 0,
            "IOMaximumIOps": 0,
            "IOMaximumBandwidth": 0,
            "MaskedPaths": masked_paths,
            "ReadonlyPaths": readonly_paths,
            "Init": False,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m"},
            "Sysctls": {},
            "StorageOpt": {},
            "Annotations": {},
            "KernelMemory": 0,
            "KernelMemoryTCP": 0,
            "Mounts": mount_authority,
        }
        nullable_lists = {
            "Binds",
            "VolumesFrom",
            "CapAdd",
            "Dns",
            "DnsOptions",
            "DnsSearch",
            "ExtraHosts",
            "GroupAdd",
            "Links",
            "BlkioWeightDevice",
            "BlkioDeviceReadBps",
            "BlkioDeviceWriteBps",
            "BlkioDeviceReadIOps",
            "BlkioDeviceWriteIOps",
            "Devices",
            "DeviceCgroupRules",
            "DeviceRequests",
            "Ulimits",
        }
        nullable_maps = {"PortBindings", "Sysctls", "StorageOpt", "Annotations"}
        if set(host) != set(expected):
            raise ValueError("container HostConfig contains an open execution field")
        normalized: dict[str, object] = {}
        for key, expected_value in expected.items():
            value = host.get(key, expected_value)
            if key == "Mounts":
                value = mount_authority
            if key in nullable_lists and value is None:
                value = []
            if key in nullable_maps and value is None:
                value = {}
            if key == "SecurityOpt" and value == ["no-new-privileges"]:
                value = ["no-new-privileges:true"]
            normalized[key] = value
        if normalized != expected:
            raise ValueError("container HostConfig differs from executor authority")
        return normalized

    @staticmethod
    def _closed_network_authority(network: object) -> dict[str, object]:
        if type(network) is not dict:
            raise ValueError("container NetworkSettings is malformed")
        allowed = {
            "Bridge",
            "SandboxID",
            "SandboxKey",
            "Ports",
            "HairpinMode",
            "LinkLocalIPv6Address",
            "LinkLocalIPv6PrefixLen",
            "SecondaryIPAddresses",
            "SecondaryIPv6Addresses",
            "EndpointID",
            "Gateway",
            "GlobalIPv6Address",
            "GlobalIPv6PrefixLen",
            "IPAddress",
            "IPPrefixLen",
            "IPv6Gateway",
            "MacAddress",
            "Networks",
        }
        if set(network) != allowed:
            raise ValueError("container NetworkSettings contains an open execution field")
        dynamic_id = network.get("SandboxID")
        sandbox_key = network.get("SandboxKey")
        if (
            type(dynamic_id) is not str
            or (dynamic_id and re.fullmatch(r"[0-9a-f]{64}", dynamic_id) is None)
            or type(sandbox_key) is not str
            or "\x00" in sandbox_key
            or type(network.get("Networks")) is not dict
            or set(network["Networks"]) != {"none"}
        ):
            raise ValueError("container network namespace identity is malformed")
        none_network = network["Networks"]["none"]
        none_allowed = {
            "IPAMConfig",
            "Links",
            "Aliases",
            "MacAddress",
            "DriverOpts",
            "GwPriority",
            "NetworkID",
            "EndpointID",
            "Gateway",
            "IPAddress",
            "IPPrefixLen",
            "IPv6Gateway",
            "GlobalIPv6Address",
            "GlobalIPv6PrefixLen",
            "DNSNames",
        }
        if type(none_network) is not dict or set(none_network) != none_allowed:
            raise ValueError("container none-network fields are open or malformed")
        for key in ("NetworkID", "EndpointID"):
            value = none_network.get(key, "")
            if type(value) is not str or (value and re.fullmatch(r"[0-9a-f]{64}", value) is None):
                raise ValueError("container none-network identity is malformed")
        if bool(none_network["NetworkID"]) is not bool(none_network["EndpointID"]):
            raise ValueError("container none-network identity is incomplete")
        empty_values = {
            "IPAMConfig": {},
            "Links": [],
            "Aliases": [],
            "MacAddress": "",
            "DriverOpts": {},
            "GwPriority": 0,
            "Gateway": "",
            "IPAddress": "",
            "IPPrefixLen": 0,
            "IPv6Gateway": "",
            "GlobalIPv6Address": "",
            "GlobalIPv6PrefixLen": 0,
            "DNSNames": [],
        }
        for key, expected in empty_values.items():
            value = none_network.get(key, expected)
            if expected in ({}, []) and value is None:
                value = type(expected)()
            if value != expected:
                raise ValueError("container none-network has address or routing authority")
        top_empty = {
            "Bridge": "",
            "Ports": {},
            "HairpinMode": False,
            "LinkLocalIPv6Address": "",
            "LinkLocalIPv6PrefixLen": 0,
            "SecondaryIPAddresses": [],
            "SecondaryIPv6Addresses": [],
            "EndpointID": "",
            "Gateway": "",
            "GlobalIPv6Address": "",
            "GlobalIPv6PrefixLen": 0,
            "IPAddress": "",
            "IPPrefixLen": 0,
            "IPv6Gateway": "",
            "MacAddress": "",
        }
        for key, expected in top_empty.items():
            value = network.get(key, expected)
            if expected in ({}, []) and value is None:
                value = type(expected)()
            if value != expected:
                raise ValueError("container NetworkSettings exposes connectivity")
        if bool(dynamic_id) is not bool(sandbox_key) or (sandbox_key and len(sandbox_key) > 4096):
            raise ValueError("container network namespace identity is incomplete")
        normalized_none = {
            "IPAMConfig": none_network["IPAMConfig"] or {},
            "Links": none_network["Links"] or [],
            "Aliases": none_network["Aliases"] or [],
            "MacAddress": none_network["MacAddress"],
            "DriverOpts": none_network["DriverOpts"] or {},
            "GwPriority": none_network["GwPriority"],
            "NetworkID": none_network["NetworkID"],
            "EndpointID": none_network["EndpointID"],
            "Gateway": none_network["Gateway"],
            "IPAddress": none_network["IPAddress"],
            "IPPrefixLen": none_network["IPPrefixLen"],
            "IPv6Gateway": none_network["IPv6Gateway"],
            "GlobalIPv6Address": none_network["GlobalIPv6Address"],
            "GlobalIPv6PrefixLen": none_network["GlobalIPv6PrefixLen"],
            "DNSNames": none_network["DNSNames"] or [],
        }
        return {
            "Bridge": network["Bridge"],
            "SandboxID": dynamic_id,
            "SandboxKey": sandbox_key,
            "Ports": network["Ports"] or {},
            "HairpinMode": network["HairpinMode"],
            "LinkLocalIPv6Address": network["LinkLocalIPv6Address"],
            "LinkLocalIPv6PrefixLen": network["LinkLocalIPv6PrefixLen"],
            "SecondaryIPAddresses": network["SecondaryIPAddresses"] or [],
            "SecondaryIPv6Addresses": network["SecondaryIPv6Addresses"] or [],
            "EndpointID": network["EndpointID"],
            "Gateway": network["Gateway"],
            "GlobalIPv6Address": network["GlobalIPv6Address"],
            "GlobalIPv6PrefixLen": network["GlobalIPv6PrefixLen"],
            "IPAddress": network["IPAddress"],
            "IPPrefixLen": network["IPPrefixLen"],
            "IPv6Gateway": network["IPv6Gateway"],
            "MacAddress": network["MacAddress"],
            "Networks": {"none": normalized_none},
        }

    @staticmethod
    def _network_lifecycle(
        authority: dict[str, object],
        status: Literal["created", "running", "exited"],
    ) -> Literal["empty", "private"]:
        none_network = authority["Networks"]
        assert type(none_network) is dict
        none_authority = none_network["none"]
        assert type(none_authority) is dict
        values = (
            authority["SandboxID"],
            authority["SandboxKey"],
            none_authority["NetworkID"],
            none_authority["EndpointID"],
        )
        empty = all(value == "" for value in values)
        private = all(type(value) is str and value != "" for value in values)
        if status == "created" and not empty:
            raise ValueError("created container unexpectedly owns a network namespace")
        if status == "running" and not private:
            raise ValueError("running container lacks its private none-network namespace")
        if status == "exited" and not (empty or private):
            raise ValueError("exited container network namespace transition is incomplete")
        return "empty" if empty else "private"

    @staticmethod
    def _closed_state(
        state: object,
        *,
        allowed_statuses: tuple[Literal["created", "running", "exited"], ...],
    ) -> dict[str, object]:
        expected_keys = {
            "Status",
            "Running",
            "Paused",
            "Restarting",
            "OOMKilled",
            "Dead",
            "Pid",
            "ExitCode",
            "Error",
            "StartedAt",
            "FinishedAt",
        }
        if type(state) is not dict or set(state) != expected_keys:
            raise ValueError("container State contains an open execution field")
        status = state["Status"]
        if (
            type(allowed_statuses) is not tuple
            or not allowed_statuses
            or status not in allowed_statuses
            or type(state["Running"]) is not bool
            or state["Running"] is not (status == "running")
            or state["Paused"] is not False
            or state["Restarting"] is not False
            or state["OOMKilled"] is not False
            or state["Dead"] is not False
            or type(state["Pid"]) is not int
            or state["Pid"] < 0
            or type(state["ExitCode"]) is not int
            or any(
                type(state[key]) is not str
                or "\x00" in state[key]
                or len(state[key]) > 4096
                for key in ("Error", "StartedAt", "FinishedAt")
            )
            or state["Error"] != ""
        ):
            raise ValueError("container State differs from the closed lifecycle")
        zero_time = "0001-01-01T00:00:00Z"
        if status == "created" and (
            state["Pid"] != 0
            or state["ExitCode"] != 0
            or state["StartedAt"] != zero_time
            or state["FinishedAt"] != zero_time
        ):
            raise ValueError("container created state differs from the closed lifecycle")
        if status == "running" and (
            state["Pid"] <= 0
            or state["ExitCode"] != 0
            or state["StartedAt"] == zero_time
            or state["FinishedAt"] != zero_time
        ):
            raise ValueError("container running state differs from the closed lifecycle")
        if status == "exited" and (
            state["Pid"] != 0
            or state["StartedAt"] == zero_time
            or state["FinishedAt"] == zero_time
        ):
            raise ValueError("container exited state differs from the closed lifecycle")
        return {key: state[key] for key in sorted(expected_keys)}

    @staticmethod
    def _closed_resolved_mount_authority(
        mounts: object,
        expected_mounts: tuple[tuple[str, str, bool], ...],
    ) -> tuple[tuple[str, str, bool], ...]:
        if type(mounts) is not list or len(mounts) != len(expected_mounts):
            raise ValueError("container mount count differs from executor authority")
        allowed = {"Type", "Name", "Source", "Destination", "Driver", "Mode", "RW", "Propagation"}
        actual: set[tuple[str, str, bool]] = set()
        for mount in mounts:
            if (
                type(mount) is not dict
                or set(mount) - allowed
                or mount.get("Type") != "bind"
                or mount.get("Name", "") != ""
                or mount.get("Driver", "") != ""
                or mount.get("Propagation") != "rprivate"
                or mount.get("Mode") not in {"ro", "rw"}
                or type(mount.get("RW")) is not bool
                or (mount.get("Mode") == "rw") is not mount.get("RW")
                or type(mount.get("Source")) is not str
                or type(mount.get("Destination")) is not str
            ):
                raise ValueError("container resolved mount is open or malformed")
            actual.add(
                (
                    _windows_key(mount["Source"]),
                    mount["Destination"],
                    mount["RW"],
                )
            )
        if actual != set(expected_mounts) or len(actual) != len(expected_mounts):
            raise ValueError("container resolved mounts differ from executor authority")
        return tuple(sorted(actual))

    @staticmethod
    def _closed_top_level_authority(
        item: dict[str, object],
        *,
        container_name: str,
        container_id: str,
        image_id: str,
        expected_command: list[str],
    ) -> dict[str, object]:
        required = {
            "Id",
            "Created",
            "Path",
            "Args",
            "State",
            "Image",
            "ResolvConfPath",
            "HostnamePath",
            "HostsPath",
            "LogPath",
            "Name",
            "RestartCount",
            "Driver",
            "Platform",
            "MountLabel",
            "ProcessLabel",
            "AppArmorProfile",
            "ExecIDs",
            "HostConfig",
            "GraphDriver",
            "Mounts",
            "Config",
            "NetworkSettings",
        }
        optional = {"SizeRw", "SizeRootFs", "ImageManifestDescriptor"}
        if not required.issubset(item) or set(item) - required - optional:
            raise ValueError("container inspection contains an open top-level field")
        created_at = item["Created"]
        driver = item["Driver"]
        graph = item["GraphDriver"]
        if (
            item["Id"] != container_id
            or item["Name"] != f"/{container_name}"
            or item["Image"] != image_id
            or item["Path"] != "python"
            or item["Args"] != expected_command
            or type(created_at) is not str
            or len(created_at) > 128
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z",
                created_at,
            )
            is None
            or item["RestartCount"] != 0
            or type(driver) is not str
            or not driver
            or len(driver) > 128
            or item["Platform"] != "linux"
            or item["LogPath"] != ""
            or item["ExecIDs"] is not None
            and item["ExecIDs"] != []
        ):
            raise ValueError("container top-level identity differs from executor authority")
        if type(graph) is not dict or set(graph) != {"Data", "Name"}:
            raise ValueError("container graph-driver authority is malformed")
        graph_data = graph["Data"]
        if (
            graph["Name"] != driver
            or type(graph_data) is not dict
            or any(
                type(key) is not str
                or not key
                or "\x00" in key
                or type(value) is not str
                or "\x00" in value
                or len(value) > 4096
                for key, value in graph_data.items()
            )
        ):
            raise ValueError("container graph-driver authority differs")
        engine_paths = {}
        for key in ("ResolvConfPath", "HostnamePath", "HostsPath"):
            value = item[key]
            if (
                type(value) is not str
                or not value
                or "\x00" in value
                or len(value) > 4096
                or container_id not in value.casefold()
            ):
                raise ValueError("container engine path differs from exact identity")
            engine_paths[key] = value
        security_labels = {}
        for key in ("MountLabel", "ProcessLabel", "AppArmorProfile"):
            value = item[key]
            if type(value) is not str or "\x00" in value or len(value) > 4096:
                raise ValueError("container security label is malformed")
            security_labels[key] = value
        if security_labels["AppArmorProfile"] not in {"", "docker-default"}:
            raise ValueError("container AppArmor profile is outside closed authority")
        if security_labels["MountLabel"] != "" or security_labels["ProcessLabel"] != "":
            raise ValueError("container process labels are outside Windows host authority")
        size_authority = {}
        for key in ("SizeRw", "SizeRootFs"):
            value = item.get(key)
            if value not in {None, 0}:
                raise ValueError("container writable-layer size differs from read-only authority")
            size_authority[key] = value
        manifest_descriptor = item.get("ImageManifestDescriptor")
        if manifest_descriptor is not None and type(manifest_descriptor) is not dict:
            raise ValueError("container image manifest descriptor is malformed")
        return {
            "Id": container_id,
            "Created": created_at,
            "Path": "python",
            "Args": expected_command,
            "Image": image_id,
            "Name": f"/{container_name}",
            "RestartCount": 0,
            "Driver": driver,
            "Platform": "linux",
            "EnginePaths": engine_paths,
            "SecurityLabels": security_labels,
            "ExecIDs": (),
            "GraphDriver": {"Name": driver, "Data": graph_data},
            "Sizes": size_authority,
            "ImageManifestDescriptor": manifest_descriptor,
        }

    def _inspect_container(
        self,
        *,
        transaction: _PinnedControlTransactionV5,
        reservation: ExecutionReservationRecordV5,
        image_authority: tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]],
        container_id: str | None,
        allowed_statuses: tuple[Literal["created", "running", "exited"], ...],
    ) -> _ContainerInspectionV5 | None:
        if transaction.command_sha256 != reservation.command_sha256:
            raise ValueError("container inspection transaction is foreign")
        if container_id is not None and re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise ValueError("container inspection identity is invalid")
        reference = reservation.container_name if container_id is None else container_id
        result = self._control(
            transaction,
            ("inspect", reference),
            timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
        )
        if not self._successful(result):
            field = "{{.Names}}" if container_id is None else "{{.ID}}"
            filter_value = (
                f"name=^{reservation.container_name}$"
                if container_id is None
                else f"id={container_id}"
            )
            listing = self._control(
                transaction,
                (
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--format",
                    field,
                    "--filter",
                    filter_value,
                ),
                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                output_limit=64 * 1024,
            )
            if not self._successful(listing):
                raise ValueError("container absence could not be proven")
            observed = tuple(line.strip() for line in listing.stdout.splitlines() if line.strip())
            if observed:
                raise ValueError("named container could not be authenticated")
            return None
        try:
            values = json.loads(result.stdout)
            item = values[0]
            config = item["Config"]
            host = item["HostConfig"]
            mounts = item["Mounts"]
            state = item["State"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            raise ValueError("container inspection is malformed") from None
        pit_labels = {
            "pit-v5.executor": self.executor_identity_sha256,
            "pit-v5.command": reservation.command_sha256,
            "pit-v5.owner": self._owner.sha256,
        }
        image_id, expected_environment, image_labels = image_authority
        expected_labels = dict(image_labels)
        expected_labels.update(pit_labels)
        separator = reservation.command_argv.index("--")
        expected_command = list(reservation.command_argv[separator + 3 :])
        expected_working_directory = "/"
        network = item.get("NetworkSettings") if type(item) is dict else None
        observed_id = item.get("Id") if type(item) is dict else None
        if (
            type(item) is not dict
            or type(observed_id) is not str
            or re.fullmatch(r"[0-9a-f]{64}", observed_id) is None
            or container_id is not None
            and observed_id != container_id
        ):
            raise ValueError("container isolation differs from executor authority")
        top_authority = self._closed_top_level_authority(
            item,
            container_name=reservation.container_name,
            container_id=observed_id,
            image_id=image_id,
            expected_command=expected_command,
        )
        config_authority = self._closed_config_authority(
            config,
            container_name=reservation.container_name,
            image_reference=self._profile.image_reference,
            expected_environment=expected_environment,
            expected_command=expected_command,
            expected_labels=expected_labels,
            expected_working_directory=expected_working_directory,
        )
        host_authority = self._closed_host_authority(
            host,
            expected_mounts=reservation.expected_mounts,
        )
        network_authority = self._closed_network_authority(network)
        state_authority = self._closed_state(state, allowed_statuses=allowed_statuses)
        status = state_authority["Status"]
        assert status in {"created", "running", "exited"}
        self._network_lifecycle(network_authority, status)
        mount_authority = self._closed_resolved_mount_authority(
            mounts,
            reservation.expected_mounts,
        )
        stable_attestation = canonical_sha256_v5(
            {
                "top": top_authority,
                "config": config_authority,
                "host": host_authority,
                "mounts": mount_authority,
            }
        )
        return _ContainerInspectionV5(
            item,
            observed_id,
            top_authority["Created"],  # type: ignore[arg-type]
            stable_attestation,
            network_authority,
            canonical_sha256_v5(network_authority),
            state_authority,
            canonical_sha256_v5(state_authority),
        )

    @staticmethod
    def _phase_attestation(
        identity: ExecutionContainerIdentityRecordV5,
        inspection: _ContainerInspectionV5,
    ) -> str:
        return canonical_sha256_v5(
            {
                "container_identity_sha256": identity.sha256,
                "lifecycle": inspection.state_authority["Status"],
                "network": inspection.network_authority,
                "state": inspection.state_authority,
            }
        )

    def _append_observed_phase(
        self,
        *,
        command_sha256: str,
        phase: Literal["created", "started", "collected"],
        identity: ExecutionContainerIdentityRecordV5,
        inspection: _ContainerInspectionV5,
    ) -> ExecutionPhaseRecordV5:
        status = inspection.state_authority["Status"]
        assert status in {"created", "running", "exited"}
        network_namespace_status = self._network_lifecycle(
            inspection.network_authority,
            status,  # type: ignore[arg-type]
        )
        return self._append_phase(
            command_sha256,
            phase,
            self._phase_attestation(identity, inspection),
            container_identity_sha256=identity.sha256,
            lifecycle_status=status,  # type: ignore[arg-type]
            network_attestation_sha256=inspection.network_attestation_sha256,
            state_attestation_sha256=inspection.state_attestation_sha256,
            network_namespace_status=network_namespace_status,
        )

    @staticmethod
    def _require_stable_container_identity(
        identity: ExecutionContainerIdentityRecordV5,
        inspection: _ContainerInspectionV5,
    ) -> None:
        if (
            inspection.container_id != identity.container_id
            or inspection.created_at != identity.created_at
            or inspection.stable_attestation_sha256 != identity.stable_attestation_sha256
        ):
            raise ValueError("container identity or stable isolation authority changed")

    def _require_phase_transition(
        self,
        phase: ExecutionPhaseRecordV5,
        identity: ExecutionContainerIdentityRecordV5,
        inspection: _ContainerInspectionV5,
        *,
        permit_running_to_exited: bool,
    ) -> None:
        self._require_stable_container_identity(identity, inspection)
        current_status = inspection.state_authority["Status"]
        current_network = self._network_lifecycle(
            inspection.network_authority,
            current_status,  # type: ignore[arg-type]
        )
        if (
            phase.container_identity_sha256 != identity.sha256
            or phase.lifecycle_status is None
            or phase.network_namespace_status is None
            or phase.network_attestation_sha256 is None
            or phase.state_attestation_sha256 is None
        ):
            raise ValueError("container phase identity is incomplete")
        if current_status == phase.lifecycle_status:
            exact = (
                inspection.network_attestation_sha256 != phase.network_attestation_sha256
                or inspection.state_attestation_sha256 != phase.state_attestation_sha256
                or self._phase_attestation(identity, inspection) != phase.attestation_sha256
            )
            if not exact:
                return
            if (
                current_status == "exited"
                and phase.network_namespace_status == "private"
                and current_network == "empty"
                and inspection.state_attestation_sha256 == phase.state_attestation_sha256
            ):
                return
            raise ValueError("container phase authority changed")
        if not (
            permit_running_to_exited
            and phase.lifecycle_status == "running"
            and current_status == "exited"
            and (
                inspection.network_attestation_sha256 == phase.network_attestation_sha256
                or current_network == "empty"
            )
        ):
            raise ValueError("container lifecycle transition is outside authority")

    def start(self, reservation: ExecutionReservationV5) -> None:
        command = reservation.command
        record = self._authorize_reservation(reservation)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command.sha256):
            if self._terminal_record(command.sha256) is not None:
                return
            launch_claim = self._phase(command.sha256, "launch_claim")
            launch_is_new = launch_claim is None
            if launch_is_new:
                self._append_phase(command.sha256, "launch_claim")
            self._ensure_control(record)
            try:
                with self._pinned_control_transaction(record) as transaction:
                    image_authority = self._inspect_image(transaction)
                    with self._mount_factory.pinned_request(command.request):
                        created = self._phase(command.sha256, "created")
                        started = self._phase(command.sha256, "started")
                        identity = self._container_identity(command.sha256)
                        if (created is None) is not (identity is None):
                            if identity is None:
                                raise ValueError("created phase lacks exact container identity")
                        if identity is None:
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                                container_id=None,
                                allowed_statuses=("created",),
                            )
                            if inspection is not None and launch_is_new:
                                raise ValueError("container name predates exact launch authority")
                        else:
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                                container_id=identity.container_id,
                                allowed_statuses=("created", "running", "exited"),
                            )
                            if inspection is None:
                                raise ValueError("owned container identity disappeared")
                            self._require_stable_container_identity(identity, inspection)
                        if created is not None and created.container_identity_sha256 != identity.sha256:
                            raise ValueError("created phase binds a foreign container identity")
                        if started is not None and started.container_identity_sha256 != identity.sha256:
                            raise ValueError("started phase binds a foreign container identity")
                        if identity is None and inspection is None and launch_is_new:
                            runtime = self._runtime_argv(command)
                            result = self._control(
                                transaction,
                                runtime[1:],
                                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                                output_limit=64 * 1024,
                            )
                            created_id = result.stdout.strip() if self._successful(result) else ""
                            if created_id and re.fullmatch(r"[0-9a-f]{64}", created_id) is None:
                                raise ValueError("Docker create returned a malformed container identity")
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                                container_id=created_id or None,
                                allowed_statuses=("created",),
                            )
                            if not self._successful(result) and inspection is None:
                                self._persist_failure(command)
                                return
                            if inspection is not None and created_id and inspection.container_id != created_id:
                                raise ValueError("Docker create identity differs from exact inspection")
                        elif identity is None and inspection is None:
                            self._persist_failure(command)
                            return
                        if identity is None:
                            assert inspection is not None
                            identity = self._persist_container_identity(record, inspection)
                        if created is None:
                            assert inspection is not None
                            created = self._append_observed_phase(
                                command_sha256=command.sha256,
                                phase="created",
                                identity=identity,
                                inspection=inspection,
                            )
                        elif inspection is None:
                            raise ValueError("created container is absent")
                        elif inspection.state_authority["Status"] == "created":
                            self._require_phase_transition(
                                created,
                                identity,
                                inspection,
                                permit_running_to_exited=False,
                            )
                        else:
                            self._require_stable_container_identity(identity, inspection)
                        if started is not None:
                            assert inspection is not None
                            self._require_phase_transition(
                                started,
                                identity,
                                inspection,
                                permit_running_to_exited=True,
                            )
                            return
                        start_claim = self._phase(command.sha256, "start_claim")
                        start_is_new = start_claim is None
                        if start_is_new:
                            self._append_phase(command.sha256, "start_claim")
                        assert inspection is not None
                        status = inspection.state_authority["Status"]
                        if status == "created" and start_is_new:
                            result = self._control(
                                transaction,
                                ("start", identity.container_id),
                                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                                output_limit=64 * 1024,
                            )
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                                container_id=identity.container_id,
                                allowed_statuses=("running", "exited"),
                            )
                            if not self._successful(result) and inspection is None:
                                self._persist_failure(command)
                                return
                        elif status == "created":
                            self._persist_failure(command)
                            return
                        elif start_is_new:
                            raise ValueError("container started outside exact start authority")
                        if inspection is None:
                            self._persist_failure(command)
                            return
                        self._require_stable_container_identity(identity, inspection)
                        if inspection.state_authority["Status"] not in {"running", "exited"}:
                            self._persist_failure(command)
                            return
                        self._append_observed_phase(
                            command_sha256=command.sha256,
                            phase="started",
                            identity=identity,
                            inspection=inspection,
                        )
            except Exception:
                self._persist_failure(command)

    def _terminal_record(self, command_sha256: str) -> ExecutionTerminalRecordV5 | None:
        record = self._repository.load_typed_state(
            namespace="container-terminal",
            key=command_sha256,
            value_type=ExecutionTerminalRecordV5,
        )
        if record is not None and (
            record.executor_identity_sha256 != self.executor_identity_sha256
            or record.command_sha256 != command_sha256
        ):
            raise ValueError("container terminal is foreign")
        return record

    def _persist_terminal(
        self,
        command: ContainerCommandV5,
        *,
        status: Literal["succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"],
        exit_code: int | None,
        output: bytes | None,
    ) -> ExecutionTerminalRecordV5:
        output_ref = None
        if output is not None:
            output_ref = self._repository.append_binary_state(
                namespace="container-output",
                key=command.sha256,
                content=output,
            )
        record = ExecutionTerminalRecordV5(
            5,
            self.executor_identity_sha256,
            command.sha256,
            command.request.sha256,
            command.request.output_mount.content_authority_sha256,
            status,
            exit_code,
            output_ref,
            0 if output is None else len(output),
        )
        self._repository.append_typed_state(
            namespace="container-terminal",
            key=command.sha256,
            value=record,
        )
        return record

    def _persist_failure(self, command: ContainerCommandV5) -> ExecutionTerminalRecordV5:
        return self._persist_terminal(command, status="failed", exit_code=None, output=None)

    def _result_from_terminal(
        self,
        command: ContainerCommandV5,
        terminal: ExecutionTerminalRecordV5,
    ) -> ContainerExecutionResultV5:
        if (
            terminal.request_sha256 != command.request.sha256
            or terminal.output_mount_authority_sha256
            != command.request.output_mount.content_authority_sha256
        ):
            raise ValueError("container terminal differs from command authority")
        content = None
        if terminal.output_ref is not None:
            content = self._repository.load_binary_state(
                namespace="container-output",
                key=command.sha256,
                reference=terminal.output_ref,
                maximum_bytes=self._manifest.resources.evaluation_output_limit_bytes + 1,
            )
        if terminal.observed_byte_count != (0 if content is None else len(content)):
            raise ValueError("container terminal output length is invalid")
        return ContainerExecutionResultV5(
            terminal.status,
            terminal.exit_code,
            BoundedOutputBytesV5(
                content,
                terminal.observed_byte_count,
                command.request.sha256,
                command.sha256,
                command.request.output_mount.content_authority_sha256,
            ),
            self._leases(command),
        )

    def _read_output(self, command: ContainerCommandV5) -> bytes | None:
        output_name = execution_output_name_v5(command.request)
        with self._mount_factory._pin_handle(command.request.output_mount) as output_root:
            path = output_root.path / output_name
            if _lstat_optional(path) is None:
                return None
            try:
                stream, info = open_regular_in_directory_v5(
                    output_root,  # type: ignore[arg-type]
                    output_name,
                    writable=False,
                )
                try:
                    content = stream.read(self._manifest.resources.evaluation_output_limit_bytes + 1)
                finally:
                    stream.close()
            except (OSError, ValueError):
                raise ValueError("container output could not be read") from None
            if (
                len(content) <= self._manifest.resources.evaluation_output_limit_bytes
                and info.st_size != len(content)
            ):
                raise ValueError("container output changed during bounded read")
            return content

    def collect(
        self,
        reservation: ExecutionReservationV5,
        *,
        remaining_timeout_seconds: float,
    ) -> ContainerExecutionResultV5:
        if (
            type(remaining_timeout_seconds) is not float
            or not math.isfinite(remaining_timeout_seconds)
            or remaining_timeout_seconds <= 0
        ):
            raise ValueError("container collection timeout is invalid")
        command = reservation.command
        record = self._authorize_reservation(reservation)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command.sha256):
            terminal = self._terminal_record(command.sha256)
            if terminal is not None:
                return self._result_from_terminal(command, terminal)
            created = self._phase(command.sha256, "created")
            started = self._phase(command.sha256, "started")
            identity = self._container_identity(command.sha256)
            if created is None or started is None or identity is None:
                return self._result_from_terminal(command, self._persist_failure(command))
            if (
                created.container_identity_sha256 != identity.sha256
                or started.container_identity_sha256 != identity.sha256
            ):
                return self._result_from_terminal(command, self._persist_failure(command))
            self._ensure_control(record)
            try:
                with self._pinned_control_transaction(record) as transaction:
                    image_authority = self._inspect_image(transaction)
                    inspection = self._inspect_container(
                        transaction=transaction,
                        reservation=record,
                        image_authority=image_authority,
                        container_id=identity.container_id,
                        allowed_statuses=("running", "exited"),
                    )
                    if inspection is None:
                        return self._result_from_terminal(command, self._persist_failure(command))
                    self._require_stable_container_identity(identity, inspection)
                    self._require_phase_transition(
                        started,
                        identity,
                        inspection,
                        permit_running_to_exited=True,
                    )
                    waited_exit_code: int | None = None
                    if inspection.state_authority["Status"] == "running":
                        waited = self._control(
                            transaction,
                            ("wait", identity.container_id),
                            timeout=remaining_timeout_seconds,
                            output_limit=64 * 1024,
                        )
                        if getattr(waited, "timed_out", None) is True:
                            stopped = self._control(
                                transaction,
                                ("stop", "--time", "0", identity.container_id),
                                timeout=float(self._manifest.resources.cleanup_timeout_seconds),
                                output_limit=64 * 1024,
                            )
                            if not self._successful(stopped):
                                return self._result_from_terminal(
                                    command,
                                    self._persist_failure(command),
                                )
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                                container_id=identity.container_id,
                                allowed_statuses=("exited",),
                            )
                            if inspection is None:
                                return self._result_from_terminal(
                                    command,
                                    self._persist_failure(command),
                                )
                            self._require_stable_container_identity(identity, inspection)
                            self._require_phase_transition(
                                started,
                                identity,
                                inspection,
                                permit_running_to_exited=True,
                            )
                            collected = self._phase(command.sha256, "collected")
                            if collected is None:
                                self._append_observed_phase(
                                    command_sha256=command.sha256,
                                    phase="collected",
                                    identity=identity,
                                    inspection=inspection,
                                )
                            else:
                                self._require_phase_transition(
                                    collected,
                                    identity,
                                    inspection,
                                    permit_running_to_exited=False,
                                )
                            terminal = self._persist_terminal(
                                command,
                                status="timed_out",
                                exit_code=None,
                                output=None,
                            )
                            return self._result_from_terminal(command, terminal)
                        if not self._successful(waited):
                            return self._result_from_terminal(command, self._persist_failure(command))
                        wait_lines = tuple(
                            line.strip() for line in waited.stdout.splitlines() if line.strip()
                        )
                        if len(wait_lines) != 1 or re.fullmatch(r"-?[0-9]+", wait_lines[0]) is None:
                            return self._result_from_terminal(command, self._persist_failure(command))
                        waited_exit_code = int(wait_lines[0])
                        inspection = self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                            container_id=identity.container_id,
                            allowed_statuses=("exited",),
                        )
                        if inspection is None:
                            return self._result_from_terminal(command, self._persist_failure(command))
                        self._require_stable_container_identity(identity, inspection)
                        self._require_phase_transition(
                            started,
                            identity,
                            inspection,
                            permit_running_to_exited=True,
                        )
                    state = inspection.state_authority
                    if state["Status"] != "exited":
                        return self._result_from_terminal(command, self._persist_failure(command))
                    exit_code = state["ExitCode"]
                    if type(exit_code) is not int or (
                        waited_exit_code is not None and waited_exit_code != exit_code
                    ):
                        return self._result_from_terminal(command, self._persist_failure(command))
                    collected = self._phase(command.sha256, "collected")
                    if collected is None:
                        self._append_observed_phase(
                            command_sha256=command.sha256,
                            phase="collected",
                            identity=identity,
                            inspection=inspection,
                        )
                    else:
                        self._require_phase_transition(
                            collected,
                            identity,
                            inspection,
                            permit_running_to_exited=False,
                        )
                    output = self._read_output(command) if exit_code == 0 else None
                    terminal = self._persist_terminal(
                        command,
                        status="succeeded" if exit_code == 0 else "nonzero_exit",
                        exit_code=exit_code,
                        output=output,
                    )
                    return self._result_from_terminal(command, terminal)
            except Exception:
                return self._result_from_terminal(command, self._persist_failure(command))

    def reconcile(
        self,
        *,
        request: DockerPanelRequestV5,
        authority: CandidateExecutionAuthorityV5,
        remaining_timeout_seconds: float,
    ) -> ExecutionReservationV5 | None:
        if (
            type(request) is not DockerPanelRequestV5
            or type(authority) is not CandidateExecutionAuthorityV5
            or type(remaining_timeout_seconds) is not float
            or not math.isfinite(remaining_timeout_seconds)
            or remaining_timeout_seconds <= 0
            or authority.campaign_id != self._owner.campaign_id
            or authority.round_index != self._owner.round_index
            or authority.owner_token_sha256 != self._owner.owner_token_sha256
            or authority.key != request.execution_key
            or authority.request_sha256 != request.sha256
            or authority.output_mount_authority_sha256
            != request.output_mount.content_authority_sha256
        ):
            raise ValueError("container recovery authority is invalid")
        command = ContainerCommandV5(request, build_docker_argv_v5(request), remaining_timeout_seconds)
        if authority.command_sha256 != command.sha256:
            raise ValueError("container recovery command differs from durable authority")
        self._authenticate_command(command)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command.sha256):
            record = self._load_reservation(command.sha256)
            if record is None:
                return None
            reservation = self._runtime_reservation(command, "existing")
            expected_payloads = tuple(item.owned_lease.payload for item in reservation.leases)
            if record != self._reservation_record(command) or authority.lease_payloads != expected_payloads:
                raise ValueError("container recovery lease authority is foreign")
            return reservation

    def recover_lease(
        self,
        payload: ResourceLeasePayloadV5,
        *,
        round_index: int,
    ) -> OwnedLeaseV5:
        """Recover one cleanup-only lease from authenticated execution authority."""

        if (
            type(payload) is not ResourceLeasePayloadV5
            or payload.resource_kind not in {"evaluator_process", "container"}
            or type(round_index) is not int
            or round_index != self._owner.round_index
            or payload.owner_campaign_id != self._owner.campaign_id
            or payload.owner_token_sha256 != self._owner.owner_token_sha256
        ):
            raise ValueError("container recovery lease authority is invalid")
        authorities = self._repository.load_candidate_executions(
            campaign_id=self._owner.campaign_id,
            round_index=round_index,
        )
        matches = tuple(
            authority for authority in authorities if payload in authority.lease_payloads
        )
        if len(matches) != 1:
            raise ValueError("container recovery lease is absent or ambiguous")
        authority = matches[0]
        record = self._load_reservation(authority.command_sha256)
        roles = ("evaluator_process", "container")
        role_index = roles.index(payload.resource_kind)
        if (
            record is None
            or record.owner != self._owner
            or record.request_sha256 != authority.request_sha256
            or record.command_sha256 != authority.command_sha256
            or record.output_mount_authority_sha256
            != authority.output_mount_authority_sha256
            or record.lease_ids != tuple(item.lease_id for item in authority.lease_payloads)
            or authority.lease_payloads[role_index] != payload
            or payload.lease_id
            != derive_execution_lease_id_v5(authority.command_sha256, payload.resource_kind)
        ):
            raise ValueError("container recovery lease differs from durable authority")
        capability = _RecoveredExecutionLeaseCapabilityV5(
            self.executor_identity_sha256,
            authority.command_sha256,
            payload.resource_kind,
            self._owner.sha256,
        )
        return OwnedLeaseV5(payload, round_index, capability)

    def cleanup(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> CleanupResultPayloadV5:
        command_sha256 = self._authorize_cleanup(owner, leases)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command_sha256):
            record = self._load_reservation(command_sha256)
            if record is None:
                raise ValueError("container cleanup reservation is absent")
            if (
                record.owner != owner
                or record.lease_ids != tuple(item.payload.lease_id for item in leases)
            ):
                raise ValueError("container cleanup reservation is foreign")
            complete = self._repository.load_typed_state(
                namespace="container-cleanup",
                key=command_sha256,
                value_type=ExecutionCleanupRecordV5,
            )
            expected = ExecutionCleanupRecordV5(
                5,
                self.executor_identity_sha256,
                command_sha256,
                owner.sha256,
            )
            if complete is not None:
                if complete != expected:
                    raise ValueError("container cleanup record is foreign")
                return CleanupResultPayloadV5(0, 0, 1, 1, True)
            container_absent = self._repository.load_typed_state(
                namespace="container-cleanup-container-absent",
                key=command_sha256,
                value_type=ExecutionCleanupRecordV5,
            )
            if container_absent is not None and container_absent != expected:
                raise ValueError("container cleanup absence record is foreign")
            if container_absent is None:
                self._ensure_control(record)
                with self._pinned_control_transaction(record) as transaction:
                    image_authority = self._inspect_image(transaction)
                    identity = self._container_identity(command_sha256)
                    created = self._phase(command_sha256, "created")
                    started = self._phase(command_sha256, "started")
                    if identity is None:
                        if created is not None or started is not None:
                            raise ValueError("container phase exists without exact identity")
                        inspection = self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                            container_id=None,
                            allowed_statuses=("created", "running", "exited"),
                        )
                        if inspection is not None:
                            raise ValueError("container name exists without durable identity")
                    else:
                        if (
                            created is not None
                            and created.container_identity_sha256 != identity.sha256
                            or started is not None
                            and started.container_identity_sha256 != identity.sha256
                        ):
                            raise ValueError("container phase binds a foreign exact identity")
                        inspection = self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                            container_id=identity.container_id,
                            allowed_statuses=("created", "running", "exited"),
                        )
                    if identity is not None and inspection is not None:
                        self._require_stable_container_identity(identity, inspection)
                        if started is not None:
                            self._require_phase_transition(
                                started,
                                identity,
                                inspection,
                                permit_running_to_exited=True,
                            )
                        elif created is not None:
                            self._require_phase_transition(
                                created,
                                identity,
                                inspection,
                                permit_running_to_exited=False,
                            )
                        elif inspection.state_authority["Status"] == "created":
                            self._append_observed_phase(
                                command_sha256=command_sha256,
                                phase="created",
                                identity=identity,
                                inspection=inspection,
                            )
                        else:
                            raise ValueError("unphased container lifecycle is outside authority")
                        removed = self._control(
                            transaction,
                            ("rm", "--force", identity.container_id),
                            timeout=float(self._manifest.resources.cleanup_timeout_seconds),
                            output_limit=64 * 1024,
                        )
                        if not self._successful(removed):
                            raise ValueError("owned container cleanup failed")
                        if self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                            container_id=identity.container_id,
                            allowed_statuses=("created", "running", "exited"),
                        ) is not None:
                            raise ValueError("owned container remains after cleanup")
                    if self._inspect_container(
                        transaction=transaction,
                        reservation=record,
                        image_authority=image_authority,
                        container_id=None,
                        allowed_statuses=("created", "running", "exited"),
                    ) is not None:
                        raise ValueError("deterministic container name was replaced during cleanup")
                self._repository.append_typed_state(
                    namespace="container-cleanup-container-absent",
                    key=command_sha256,
                    value=expected,
                )
            try:
                control_created = self._control_created(command_sha256)
                ready = self._repository.load_typed_state(
                    namespace="container-control-ready",
                    key=command_sha256,
                    value_type=ExecutionControlRecordV5,
                )
                if control_created is None or ready is None:
                    raise ValueError("container control readiness is absent")
                with acquire_absolute_directory_v5(
                    self._control_root,
                    expected_identity=(
                        self._control_root_info.st_dev,
                        self._control_root_info.st_ino,
                    ),
                ) as parent:
                    if not directory_child_absent_v5(parent, record.control_relative_path):
                        self._ensure_control(record)
                        with self._pinned_control_transaction(record) as transaction:
                            target_identity = transaction.transaction_root_identity
                        if target_identity != (
                            control_created.transaction_device,
                            control_created.transaction_inode,
                        ):
                            raise ValueError("container control cleanup identity changed")
                        remove_owned_tree_in_directory_v5(
                            parent,
                            record.control_relative_path,
                            expected_identity=(
                                control_created.transaction_device,
                                control_created.transaction_inode,
                            ),
                        )
                    if not directory_child_absent_v5(parent, record.control_relative_path):
                        raise ValueError("container control cleanup is incomplete")
            except BaseException:
                raise ValueError("container control cleanup failed") from None
            self._repository.append_typed_state(
                namespace="container-cleanup",
                key=command_sha256,
                value=expected,
            )
            return CleanupResultPayloadV5(0, 0, 1, 1, True)

    def cleanup_many(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> CleanupResultPayloadV5:
        """Clean exact complete evaluator/container lease pairs by execution."""

        if type(leases) is not tuple or any(
            type(item) is not OwnedLeaseV5
            or item.payload.resource_kind not in {"evaluator_process", "container"}
            for item in leases
        ):
            raise ValueError("container cleanup lease collection is invalid")
        grouped: dict[str, dict[str, OwnedLeaseV5]] = {}
        for lease in leases:
            capability = lease.opaque_handle
            if type(capability) not in {
                _ExecutionLeaseCapabilityV5,
                _RecoveredExecutionLeaseCapabilityV5,
            }:
                raise ValueError("container cleanup lease capability is foreign")
            roles = grouped.setdefault(capability.command_sha256, {})
            if lease.payload.resource_kind in roles:
                raise ValueError("container cleanup lease collection contains a duplicate")
            roles[lease.payload.resource_kind] = lease
        evaluator_count = 0
        container_count = 0
        for command_sha256 in sorted(grouped):
            roles = grouped[command_sha256]
            if set(roles) != {"evaluator_process", "container"}:
                raise ValueError("container cleanup requires each complete execution lease pair")
            result = self.cleanup(
                owner=owner,
                leases=(roles["evaluator_process"], roles["container"]),
            )
            if not result.cleanup_complete:
                return CleanupResultPayloadV5(
                    0,
                    0,
                    evaluator_count + result.owned_evaluators,
                    container_count + result.owned_containers,
                    False,
                    result.failure_code,
                )
            evaluator_count += result.owned_evaluators
            container_count += result.owned_containers
        return CleanupResultPayloadV5(
            0,
            0,
            evaluator_count,
            container_count,
            True,
        )

    def _authorize_cleanup(self, owner: WorkspaceOwnerV5, leases: tuple[OwnedLeaseV5, ...]) -> str:
        if (
            type(owner) is not WorkspaceOwnerV5
            or owner != self._owner
            or type(leases) is not tuple
            or tuple(item.payload.resource_kind for item in leases) != ("evaluator_process", "container")
        ):
            raise ValueError("container cleanup authority is invalid")
        command_digests = set()
        for lease, role in zip(leases, ("evaluator_process", "container"), strict=True):
            capability = lease.opaque_handle
            if (
                type(lease) is not OwnedLeaseV5
                or type(capability)
                not in {_ExecutionLeaseCapabilityV5, _RecoveredExecutionLeaseCapabilityV5}
                or capability.executor_identity_sha256 != self.executor_identity_sha256
                or capability.role_kind != role
                or capability.owner_sha256 != owner.sha256
                or lease.round_index != owner.round_index
                or lease.payload.owner_campaign_id != owner.campaign_id
                or lease.payload.owner_token_sha256 != owner.owner_token_sha256
            ):
                raise ValueError("container cleanup lease is foreign")
            if (
                type(capability) is _ExecutionLeaseCapabilityV5
                and capability.command.sha256 != capability.command_sha256
            ):
                raise ValueError("container cleanup live command is foreign")
            command_digests.add(capability.command_sha256)
            if lease.payload.lease_id != derive_execution_lease_id_v5(capability.command_sha256, role):
                raise ValueError("container cleanup lease identity is invalid")
        if len(command_digests) != 1:
            raise ValueError("container cleanup leases span multiple executions")
        return command_digests.pop()

__all__ = [
    "ExecutionCleanupRecordV5",
    "ExecutionContainerIdentityRecordV5",
    "ExecutionControlCreatedRecordV5",
    "ExecutionControlRecordV5",
    "ExecutionPhaseRecordV5",
    "ExecutionReservationRecordV5",
    "ExecutionTerminalRecordV5",
    "LocalContainerExecutorV5",
    "LocalSandboxMountFactoryV5",
    "MountCreatedRecordV5",
    "MountReadyRecordV5",
    "MountReservationRecordV5",
]
