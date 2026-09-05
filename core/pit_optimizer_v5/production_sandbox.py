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
from pathlib import Path
import re
import stat
from typing import Iterator, Literal

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    canonical_sha256_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.production_workspace import LocalGitWorkspaceDriverV5
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    acquire_directory_v5,
    directory_child_absent_v5,
    directory_entry_names_v5,
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
from core.pit_optimizer_v5.runtime import CandidateExecutionKeyV5, MaterializedVariantV5, OwnedLeaseV5
from core.pit_optimizer_v5.sandbox import (
    BoundedOutputBytesV5,
    ContainerCommandV5,
    ContainerExecutionResultV5,
    ContainerExecutorV5,
    DockerPanelRequestV5,
    ExecutionLeaseV5,
    ExecutionReservationV5,
    SandboxMountFactoryV5,
    SandboxMountHandleV5,
    build_docker_argv_v5,
    derive_execution_lease_id_v5,
    derive_sandbox_mount_authorities_v5,
)
from core.pit_optimizer_v5.workspace import MaterializedWorkspaceV5, WorkspaceOwnerV5


_DATA_FILES_V5 = ("pit_bundle.sqlite3", "prices_provenance.json")
_DATA_FILE_MAXIMUM_BYTES_V5 = (8 * 1024 * 1024 * 1024, 64 * 1024 * 1024)
_DOCKER_EXECUTABLE_MAXIMUM_BYTES_V5 = 512 * 1024 * 1024
_POLICY_SOURCE_MAXIMUM_BYTES_V5 = 1024 * 1024
_CONTAINER_SHM_SIZE_BYTES_V5 = 16 * 1024 * 1024
_CONTROL_CHILDREN_V5 = ("config", "home", "tmp")
_DOCKER_CONFIG_NAME_V5 = "config.json"
_DOCKER_CONFIG_CONTENT_V5 = b"{}\n"
_DOCKER_CONFIG_TEXT_V5 = _DOCKER_CONFIG_CONTENT_V5.decode("ascii")


def _windows_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


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
class MountReadyRecordV5:
    schema_version: Literal[5]
    factory_identity_sha256: str
    output_authority_sha256: str
    output_root_identity_sha256: str

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
        if (
            type(self.expected_mounts) is not tuple
            or len(self.expected_mounts) != 4
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
    phase: Literal["launch_claim", "created", "start_claim", "started"]
    attestation_sha256: str | None

    def __post_init__(self) -> None:
        if (
            self.schema_version != 5
            or re.fullmatch(r"[0-9a-f]{64}", self.executor_identity_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.command_sha256) is None
            or self.phase not in {"launch_claim", "created", "start_claim", "started"}
            or (
                self.attestation_sha256 is None
                if self.phase in {"created", "started"}
                else self.attestation_sha256 is not None
            )
            or (
                self.attestation_sha256 is not None
                and re.fullmatch(r"[0-9a-f]{64}", self.attestation_sha256) is None
            )
        ):
            raise ValueError("container execution phase is invalid")


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
class _ExecutionLeaseCapabilityV5:
    executor_identity_sha256: str
    command_sha256: str
    role_kind: Literal["evaluator_process", "container"]
    owner_sha256: str
    command: ContainerCommandV5


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
        self.mount_identity_sha256 = canonical_sha256_v5(
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
    def fixed_roots(self) -> tuple[Path, Path, Path, Path]:
        return self._fixed_roots

    def mounts_for(
        self,
        *,
        materialized: MaterializedVariantV5,
        panel: EpisodePlanV5,
        scenario_ids: tuple[str, ...],
        execution_key: CandidateExecutionKeyV5,
    ) -> tuple[SandboxMountHandleV5, SandboxMountHandleV5, SandboxMountHandleV5]:
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
        try:
            with ExitStack() as stack:
                source_access = stack.enter_context(
                    acquire_absolute_directory_v5(source_path, expected_identity=source_identity)
                )
                data_access = stack.enter_context(
                    acquire_absolute_directory_v5(
                        self._data_root,
                        expected_identity=(self._data_info.st_dev, self._data_info.st_ino),
                    )
                )
                output_access = stack.enter_context(
                    acquire_absolute_directory_v5(
                        self._output_root,
                        expected_identity=(self._output_info.st_dev, self._output_info.st_ino),
                    )
                )
                self._authenticate_data_files(data_access)
                # This is the last pre-mutation authority check.  All involved
                # ancestors remain pinned while the output child is reserved.
                if any(
                    _roots_overlap(first, second)
                    for first, second in (
                        (source_access.path, data_access.path),
                        (source_access.path, output_access.path),
                        (data_access.path, output_access.path),
                    )
                ):
                    raise ValueError("sandbox mount roots are not fully disjoint")
                output_path, output_info = self._create_or_load_output(
                    output_parent=output_access,
                    source_path=source_access.path,
                    source_identity=source_binding,
                    data_identity=_directory_identity(data_access.path, data_access.path.lstat()),
                    execution_key=execution_key,
                    output_authority=authorities[2],
                )
                source_info = source_access.path.lstat()
                data_path = data_access.path
                data_info = data_path.lstat()
                source_path = source_access.path
        except (OSError, ValueError):
            raise ValueError("sandbox mount authority changed during issuance") from None
        return (
            self._mount_handle("source", source_path, source_info, authorities[0], source_binding),
            self._mount_handle(
                "data",
                data_path,
                data_info,
                authorities[1],
                canonical_sha256_v5(self._data_file_identities),
            ),
            self._mount_handle(
                "output",
                output_path,
                output_info,
                authorities[2],
                _directory_identity(output_path, output_info),
            ),
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
        target = self._output_root / relative
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
                if _lstat_optional(target) is not None:
                    raise ValueError("sandbox output target predates its reservation")
                self._repository.append_typed_state(
                    namespace="sandbox-mount-reservation",
                    key=output_authority,
                    value=record,
                )
            elif prior != record:
                raise ValueError("sandbox output reservation is foreign")
            ready = self._repository.load_typed_state(
                namespace="sandbox-mount-ready",
                key=output_authority,
                value_type=MountReadyRecordV5,
            )
            existing = _lstat_optional(target)
            if existing is None and ready is not None:
                raise ValueError("sandbox output disappeared after readiness")
            try:
                with acquire_directory_v5(
                    self._output_root,
                    (relative,),
                    create=existing is None,
                    expected_root_identity=(self._output_info.st_dev, self._output_info.st_ino),
                ) as target_access:
                    target_path = target_access.path
                    target_info = target_path.lstat()
                    if (target_info.st_dev, target_info.st_ino) != target_access.identity:
                        raise ValueError("sandbox output identity changed")
                    identity = _directory_identity(target_path, target_info)
                    if ready is None:
                        if any(os.scandir(target_access.path)):
                            raise ValueError("unready sandbox output is not empty")
                        self._repository.append_typed_state(
                            namespace="sandbox-mount-ready",
                            key=output_authority,
                            value=MountReadyRecordV5(
                                5,
                                self.mount_identity_sha256,
                                output_authority,
                                identity,
                            ),
                        )
                    elif (
                        ready.factory_identity_sha256 != self.mount_identity_sha256
                        or ready.output_authority_sha256 != output_authority
                        or ready.output_root_identity_sha256 != identity
                    ):
                        raise ValueError("sandbox output readiness is foreign")
                    if _roots_overlap(source_path, target_path):
                        raise ValueError("sandbox source and output roots overlap")
                    return target_path, target_info
            except (OSError, ValueError):
                raise ValueError("sandbox output could not be safely created or opened") from None

    def _mount_handle(
        self,
        kind: Literal["source", "data", "output"],
        path: Path,
        info: os.stat_result,
        content_authority: str,
        binding: str,
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
            {"source": "/pit/source", "data": "/pit/data", "output": "/pit/output"}[kind],
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
    ) -> Iterator[tuple[Path, Path, Path]]:
        """Hold every mount ancestry stable across one Docker engine boundary."""

        self._authorize_request(request)
        with ExitStack() as stack:
            accesses = tuple(
                stack.enter_context(self._pin_handle(handle))
                for handle in (request.source_mount, request.data_mount, request.output_mount)
            )
            paths = tuple(access.path for access in accesses)
            if any(
                _roots_overlap(first, second)
                for first, second in (
                    (paths[0], paths[1]),
                    (paths[0], paths[2]),
                    (paths[1], paths[2]),
                )
            ):
                raise ValueError("sandbox request mount roots overlap")
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
                            expected_root_identity=accesses[0].identity,
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
            output_ready = self._repository.load_typed_state(
                namespace="sandbox-mount-ready",
                key=request.output_mount.content_authority_sha256,
                value_type=MountReadyRecordV5,
            )
            if (
                output_ready is None
                or output_ready.factory_identity_sha256 != self.mount_identity_sha256
                or output_ready.output_authority_sha256
                != request.output_mount.content_authority_sha256
                or output_ready.output_root_identity_sha256
                != request.output_mount.root_identity_sha256
            ):
                raise ValueError("sandbox output mount is not durably ready")
            yield paths

    def authenticate_request(
        self,
        request: DockerPanelRequestV5,
    ) -> tuple[Path, Path, Path]:
        """Reauthenticate every mount and its request-bound source/data authority."""

        with self.pinned_request(request) as paths:
            return paths


class LocalContainerExecutorV5(ContainerExecutorV5):
    """Durable create/start/collect Docker executor with deterministic ownership."""

    _OUTPUT_NAME = "panel-evaluation.json"

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
        self.executor_identity_sha256 = canonical_sha256_v5(
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
                    "workdir": "/pit/source",
                    "output_name": self._OUTPUT_NAME,
                    "shell": False,
                },
                "repository_root_identity_sha256": repository.root_identity_sha256,
            }
        )

    def reserve(self, command: ContainerCommandV5) -> ExecutionReservationV5:
        self._authenticate_command(command)
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

    def _authenticate_command(self, command: ContainerCommandV5) -> tuple[Path, Path, Path]:
        if (
            type(command) is not ContainerCommandV5
            or command.request.owner != self._owner
            or command.request.manifest != self._manifest
            or command.request.sandbox_profile != self._profile
            or command.argv != build_docker_argv_v5(command.request)
        ):
            raise ValueError("container command differs from executor authority")
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
            "/pit/source",
        ]
        generic[2:2] = injected
        data_mount = command.request.data_mount
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
        expected_mounts = tuple(
            sorted(
                (
                    (
                        _windows_key(command.request.source_mount.host_path),
                        command.request.source_mount.container_path,
                        False,
                    ),
                    *(
                        (
                            _windows_key(str(Path(command.request.data_mount.host_path) / name)),
                            f"{command.request.data_mount.container_path}/{name}",
                            False,
                        )
                        for name in _DATA_FILES_V5
                    ),
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
        phase: Literal["launch_claim", "created", "start_claim", "started"],
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
        phase: Literal["launch_claim", "created", "start_claim", "started"],
        attestation_sha256: str | None = None,
    ) -> ExecutionPhaseRecordV5:
        record = ExecutionPhaseRecordV5(
            5,
            self.executor_identity_sha256,
            command_sha256,
            phase,
            attestation_sha256,
        )
        self._repository.append_typed_state(
            namespace=f"container-{phase.replace('_', '-')}",
            key=command_sha256,
            value=record,
        )
        return record

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
            canonical_sha256_v5(
                {
                    "path": _windows_key(str(root_access.path)),  # type: ignore[attr-defined]
                    "identity": root_access.identity,  # type: ignore[attr-defined]
                }
            ),
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
        ready = self._repository.load_typed_state(
            namespace="container-control-ready",
            key=reservation.command_sha256,
            value_type=ExecutionControlRecordV5,
        )
        existing = _lstat_optional(target)
        if existing is None and ready is not None:
            raise ValueError("container control directory disappeared")
        if existing is not None and ready is None:
            raise ValueError("container control directory predates durable authority")
        created_identity: tuple[int, int] | None = None
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
                target_access = stack.enter_context(
                    acquire_directory_v5(
                        root.path,
                        (reservation.control_relative_path,),
                        create=existing is None,
                        expected_root_identity=root.identity,
                    )
                )
                if existing is None:
                    created_identity = target_access.identity
                children = {
                    name: stack.enter_context(
                        acquire_directory_v5(
                            target_access.path,
                            (name,),
                            create=ready is None,
                            expected_root_identity=target_access.identity,
                        )
                    )
                    for name in _CONTROL_CHILDREN_V5
                }
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
        except (OSError, ValueError):
            if created_identity is not None:
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
        ready = self._repository.load_typed_state(
            namespace="container-control-ready",
            key=reservation.command_sha256,
            value_type=ExecutionControlRecordV5,
        )
        if ready is None:
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
    ) -> tuple[str, tuple[str, ...]]:
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
        ):
            raise ValueError("sandbox image differs from its immutable authority")
        return image_id, tuple(image_environment)

    @staticmethod
    def _closed_config_authority(
        config: object,
        *,
        container_name: str,
        image_reference: str,
        expected_environment: tuple[str, ...],
        expected_command: list[str],
        expected_labels: dict[str, str],
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
            "WorkingDir": "/pit/source",
            "Entrypoint": ["python"],
            "OnBuild": None,
            "Labels": expected_labels,
            "StopSignal": "",
            "StopTimeout": None,
            "Shell": None,
        }
        optional_defaults = {
            "ExposedPorts",
            "ArgsEscaped",
            "Volumes",
            "OnBuild",
            "StopSignal",
            "StopTimeout",
            "Shell",
        }
        if set(config) - set(expected) or any(
            key not in config and key not in optional_defaults for key in expected
        ):
            raise ValueError("container config contains an open execution field")
        normalized = {
            key: config[key] if key in config else value
            for key, value in expected.items()
        }
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
        optional_defaults = {
            "CgroupnsMode",
            "ConsoleSize",
            "DeviceCgroupRules",
            "Init",
            "KernelMemory",
            "KernelMemoryTCP",
            "Annotations",
        }
        if set(host) - set(expected) or any(
            key not in host and key not in optional_defaults for key in expected
        ):
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
        required = allowed - {"Bridge", "HairpinMode", "LinkLocalIPv6Address", "LinkLocalIPv6PrefixLen"}
        if set(network) - allowed or not required.issubset(network):
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
        if type(none_network) is not dict or set(none_network) - none_allowed:
            raise ValueError("container none-network fields are open or malformed")
        for key in ("NetworkID", "EndpointID"):
            value = none_network.get(key, "")
            if type(value) is not str or (value and re.fullmatch(r"[0-9a-f]{64}", value) is None):
                raise ValueError("container none-network identity is malformed")
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
        return {
            "mode": "none",
            "ports": {},
            "addresses": (),
            "aliases": (),
        }

    @staticmethod
    def _closed_state(state: object) -> None:
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
            status not in {"created", "running", "exited", "dead", "removing"}
            or type(state["Running"]) is not bool
            or state["Running"] is not (status == "running")
            or state["Paused"] is not False
            or state["Restarting"] is not False
            or type(state["OOMKilled"]) is not bool
            or type(state["Dead"]) is not bool
            or state["Dead"] is not (status == "dead")
            or type(state["Pid"]) is not int
            or state["Pid"] < 0
            or type(state["ExitCode"]) is not int
            or any(
                type(state[key]) is not str
                or "\x00" in state[key]
                or len(state[key]) > 4096
                for key in ("Error", "StartedAt", "FinishedAt")
            )
        ):
            raise ValueError("container State differs from the closed lifecycle")

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

    def _inspect_container(
        self,
        *,
        transaction: _PinnedControlTransactionV5,
        reservation: ExecutionReservationRecordV5,
        image_authority: tuple[str, tuple[str, ...]],
    ) -> tuple[dict[str, object], str] | None:
        if transaction.command_sha256 != reservation.command_sha256:
            raise ValueError("container inspection transaction is foreign")
        result = self._control(
            transaction,
            ("inspect", reservation.container_name),
            timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
        )
        if not self._successful(result):
            listing = self._control(
                transaction,
                (
                    "container",
                    "ls",
                    "--all",
                    "--format",
                    "{{.Names}}",
                    "--filter",
                    f"name=^{reservation.container_name}$",
                ),
                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                output_limit=64 * 1024,
            )
            if not self._successful(listing):
                raise ValueError("container absence could not be proven")
            names = tuple(line.strip() for line in listing.stdout.splitlines() if line.strip())
            if reservation.container_name in names:
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
        expected_labels = {
            "pit-v5.executor": self.executor_identity_sha256,
            "pit-v5.command": reservation.command_sha256,
            "pit-v5.owner": self._owner.sha256,
        }
        image_id, expected_environment = image_authority
        separator = reservation.command_argv.index("--")
        expected_command = list(reservation.command_argv[separator + 3 :])
        network = item.get("NetworkSettings") if type(item) is dict else None
        if (
            type(item) is not dict
            or item.get("Name") != f"/{reservation.container_name}"
            or item.get("Image") != image_id
            or item.get("Path") != "python"
            or item.get("Args") != expected_command
        ):
            raise ValueError("container isolation differs from executor authority")
        config_authority = self._closed_config_authority(
            config,
            container_name=reservation.container_name,
            image_reference=self._profile.image_reference,
            expected_environment=expected_environment,
            expected_command=expected_command,
            expected_labels=expected_labels,
        )
        host_authority = self._closed_host_authority(
            host,
            expected_mounts=reservation.expected_mounts,
        )
        network_authority = self._closed_network_authority(network)
        self._closed_state(state)
        mount_authority = self._closed_resolved_mount_authority(
            mounts,
            reservation.expected_mounts,
        )
        attestation = canonical_sha256_v5(
            {
                "name": item.get("Name"),
                "image": item.get("Image"),
                "path": item.get("Path"),
                "arguments": item.get("Args"),
                "config": config_authority,
                "host": host_authority,
                "network": network_authority,
                "mounts": mount_authority,
            }
        )
        return item, attestation

    def start(self, reservation: ExecutionReservationV5) -> None:
        command = reservation.command
        record = self._authorize_reservation(reservation)
        with self._repository.adapter_state_transition(namespace="container-execution", key=command.sha256):
            if self._terminal_record(command.sha256) is not None or self._phase(command.sha256, "started") is not None:
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
                        inspection = self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                        )
                        if created is None and inspection is None and launch_is_new:
                            runtime = self._runtime_argv(command)
                            result = self._control(
                                transaction,
                                runtime[1:],
                                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                                output_limit=64 * 1024,
                            )
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                            )
                            if not self._successful(result) and inspection is None:
                                self._persist_failure(command)
                                return
                        if created is None:
                            if inspection is None:
                                self._persist_failure(command)
                                return
                            created = self._append_phase(
                                command.sha256,
                                "created",
                                inspection[1],
                            )
                        elif inspection is None or created.attestation_sha256 != inspection[1]:
                            self._persist_failure(command)
                            return
                        start_claim = self._phase(command.sha256, "start_claim")
                        start_is_new = start_claim is None
                        if start_is_new:
                            self._append_phase(command.sha256, "start_claim")
                        state = inspection[0]["State"]
                        assert type(state) is dict
                        status = state.get("Status")
                        if status == "created" and start_is_new:
                            result = self._control(
                                transaction,
                                ("start", record.container_name),
                                timeout=float(self._manifest.resources.worker_startup_timeout_seconds),
                                output_limit=64 * 1024,
                            )
                            inspection = self._inspect_container(
                                transaction=transaction,
                                reservation=record,
                                image_authority=image_authority,
                            )
                            if not self._successful(result) and inspection is None:
                                self._persist_failure(command)
                                return
                        if inspection is None:
                            self._persist_failure(command)
                            return
                        state = inspection[0]["State"]
                        assert type(state) is dict
                        if state.get("Status") not in {"running", "exited"}:
                            self._persist_failure(command)
                            return
                        self._append_phase(command.sha256, "started", inspection[1])
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
        with self._mount_factory._pin_handle(command.request.output_mount) as output_root:
            path = output_root.path / self._OUTPUT_NAME
            if _lstat_optional(path) is None:
                return None
            try:
                stream, info = open_regular_in_directory_v5(
                    output_root,  # type: ignore[arg-type]
                    self._OUTPUT_NAME,
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
            if self._phase(command.sha256, "started") is None:
                return self._result_from_terminal(command, self._persist_failure(command))
            self._ensure_control(record)
            try:
                with self._pinned_control_transaction(record) as transaction:
                    image_authority = self._inspect_image(transaction)
                    inspection = self._inspect_container(
                        transaction=transaction,
                        reservation=record,
                        image_authority=image_authority,
                    )
                    if inspection is None:
                        return self._result_from_terminal(command, self._persist_failure(command))
                    state = inspection[0]["State"]
                    assert type(state) is dict
                    if state.get("Running") is True:
                        waited = self._control(
                            transaction,
                            ("wait", record.container_name),
                            timeout=remaining_timeout_seconds,
                            output_limit=64 * 1024,
                        )
                        if getattr(waited, "timed_out", None) is True:
                            self._control(
                                transaction,
                                ("stop", "--time", "0", record.container_name),
                                timeout=float(self._manifest.resources.cleanup_timeout_seconds),
                                output_limit=64 * 1024,
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
                        inspection = self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                        )
                        if inspection is None:
                            return self._result_from_terminal(command, self._persist_failure(command))
                        state = inspection[0]["State"]
                        assert type(state) is dict
                    if state.get("Running") is True or state.get("Status") != "exited":
                        return self._result_from_terminal(command, self._persist_failure(command))
                    exit_code = state.get("ExitCode")
                    if type(exit_code) is not int:
                        return self._result_from_terminal(command, self._persist_failure(command))
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
                    inspection = self._inspect_container(
                        transaction=transaction,
                        reservation=record,
                        image_authority=image_authority,
                    )
                    if inspection is not None:
                        removed = self._control(
                            transaction,
                            ("rm", "--force", record.container_name),
                            timeout=float(self._manifest.resources.cleanup_timeout_seconds),
                            output_limit=64 * 1024,
                        )
                        if not self._successful(removed):
                            raise ValueError("owned container cleanup failed")
                        if self._inspect_container(
                            transaction=transaction,
                            reservation=record,
                            image_authority=image_authority,
                        ) is not None:
                            raise ValueError("owned container remains after cleanup")
                self._repository.append_typed_state(
                    namespace="container-cleanup-container-absent",
                    key=command_sha256,
                    value=expected,
                )
            try:
                ready = self._repository.load_typed_state(
                    namespace="container-control-ready",
                    key=command_sha256,
                    value_type=ExecutionControlRecordV5,
                )
                if ready is None:
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
                        remove_owned_tree_in_directory_v5(
                            parent,
                            record.control_relative_path,
                            expected_identity=target_identity,
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
                or type(capability) is not _ExecutionLeaseCapabilityV5
                or capability.executor_identity_sha256 != self.executor_identity_sha256
                or capability.role_kind != role
                or capability.owner_sha256 != owner.sha256
                or capability.command.sha256 != capability.command_sha256
                or lease.round_index != owner.round_index
                or lease.payload.owner_campaign_id != owner.campaign_id
                or lease.payload.owner_token_sha256 != owner.owner_token_sha256
            ):
                raise ValueError("container cleanup lease is foreign")
            command_digests.add(capability.command_sha256)
            if lease.payload.lease_id != derive_execution_lease_id_v5(capability.command_sha256, role):
                raise ValueError("container cleanup lease identity is invalid")
        if len(command_digests) != 1:
            raise ValueError("container cleanup leases span multiple executions")
        return command_digests.pop()

__all__ = [
    "ExecutionCleanupRecordV5",
    "ExecutionControlRecordV5",
    "ExecutionPhaseRecordV5",
    "ExecutionReservationRecordV5",
    "ExecutionTerminalRecordV5",
    "LocalContainerExecutorV5",
    "LocalSandboxMountFactoryV5",
    "MountReadyRecordV5",
    "MountReservationRecordV5",
]
