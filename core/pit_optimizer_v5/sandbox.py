"""Injected, bounded Docker panel-evaluation boundary for optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
import json
import ntpath
import types
from typing import Literal, Protocol, TypeVar, get_args, get_origin, get_type_hints, runtime_checkable

from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5
from core.pit_optimizer_v5.contracts import (
    EpisodePlanV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    SandboxProfileV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.memory import CleanupResultPayloadV5
from core.pit_optimizer_v5.runtime import OwnedLeaseV5
from core.pit_optimizer_v5.workspace import WorkspaceOwnerV5


SandboxFailureCodeV5 = Literal[
    "invalid_request",
    "cancelled",
    "timed_out",
    "nonzero_exit",
    "output_too_large",
    "missing_output",
    "noncanonical_output",
    "output_schema",
    "identity_mismatch",
    "foreign_lease",
    "cleanup_failed",
    "driver_failed",
]
ContainerStatusV5 = Literal["succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"]
MountKindV5 = Literal["source", "data", "output"]
MountModeV5 = Literal["read_only", "bounded_write_only"]

_FAILURES = frozenset(
    {
        "invalid_request",
        "cancelled",
        "timed_out",
        "nonzero_exit",
        "output_too_large",
        "missing_output",
        "noncanonical_output",
        "output_schema",
        "identity_mismatch",
        "foreign_lease",
        "cleanup_failed",
        "driver_failed",
    }
)


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class SandboxFailureV5:
    code: SandboxFailureCodeV5

    def __post_init__(self) -> None:
        if self.code not in _FAILURES:
            raise ValueError("sandbox failure is outside the closed V5 taxonomy")


class SandboxAdapterErrorV5(RuntimeError):
    def __init__(self, failure: SandboxFailureV5) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class SandboxMountHandleV5:
    """Authenticated root-bound mount capability supplied by local composition."""

    kind: MountKindV5
    mode: MountModeV5
    root_identity_sha256: str
    content_authority_sha256: str
    host_path: str
    container_path: str
    maximum_bytes: int | None
    opaque_handle: object

    def __post_init__(self) -> None:
        if self.kind not in {"source", "data", "output"}:
            raise ValueError("sandbox mount kind is invalid")
        expected_mode = "bounded_write_only" if self.kind == "output" else "read_only"
        if self.mode != expected_mode:
            raise ValueError("sandbox mount mode differs from its purpose")
        _digest(self.root_identity_sha256, "sandbox mount root identity")
        _digest(self.content_authority_sha256, "sandbox mount content authority")
        if (
            type(self.host_path) is not str
            or not self.host_path
            or self.host_path.strip() != self.host_path
            or not ntpath.isabs(self.host_path)
            or ntpath.normpath(self.host_path) != self.host_path
            or any(character in self.host_path for character in {",", "\x00", "\n", "\r"})
        ):
            raise ValueError("sandbox mount host path is invalid")
        expected_target = {"source": "/pit/source", "data": "/pit/data", "output": "/pit/output"}[self.kind]
        if self.container_path != expected_target:
            raise ValueError("sandbox mount target is not the closed V5 target")
        if self.kind == "output":
            _positive(self.maximum_bytes, "sandbox output mount bound")
        elif self.maximum_bytes is not None:
            raise ValueError("read-only sandbox mount cannot carry an output bound")
        if self.opaque_handle is None:
            raise ValueError("sandbox mount requires an opaque root-bound handle")


@dataclass(frozen=True, slots=True)
class ContainerLimitsV5:
    pid_limit: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        _positive(self.pid_limit, "container PID limit")
        _positive(self.timeout_seconds, "container timeout")


@dataclass(frozen=True, slots=True)
class DockerPanelRequestV5:
    owner: WorkspaceOwnerV5
    policy_revision: PolicyRevisionIdentityV5
    evaluator_contract: EvaluatorContractV5
    sandbox_profile: SandboxProfileV5
    panel: EpisodePlanV5
    scenario_ids: tuple[str, ...]
    source_mount: SandboxMountHandleV5
    data_mount: SandboxMountHandleV5
    output_mount: SandboxMountHandleV5
    limits: ContainerLimitsV5

    def __post_init__(self) -> None:
        if (
            type(self.owner) is not WorkspaceOwnerV5
            or type(self.policy_revision) is not PolicyRevisionIdentityV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.sandbox_profile) is not SandboxProfileV5
            or type(self.panel) is not EpisodePlanV5
            or type(self.limits) is not ContainerLimitsV5
        ):
            raise ValueError("Docker panel request authority is invalid")
        if self.evaluator_contract.sandbox_profile_sha256 != self.sandbox_profile.sha256:
            raise ValueError("Docker panel request sandbox differs from evaluator authority")
        declared = tuple(item.scenario_id for item in self.evaluator_contract.friction_grid)
        if self.scenario_ids not in {
            (self.evaluator_contract.selection_scenario_id,),
            declared,
        }:
            raise ValueError("Docker panel scenarios are outside the quick/full closed scopes")
        if (
            type(self.source_mount) is not SandboxMountHandleV5
            or type(self.data_mount) is not SandboxMountHandleV5
            or type(self.output_mount) is not SandboxMountHandleV5
            or (self.source_mount.kind, self.data_mount.kind, self.output_mount.kind) != ("source", "data", "output")
        ):
            raise ValueError("Docker panel mount capabilities are invalid")
        if self.output_mount.maximum_bytes != self.sandbox_profile.output_limit_bytes:
            raise ValueError("Docker panel output mount differs from sandbox authority")
        if (
            len(
                {
                    self.source_mount.root_identity_sha256,
                    self.data_mount.root_identity_sha256,
                    self.output_mount.root_identity_sha256,
                }
            )
            != 3
            or len(
                {
                    self.source_mount.host_path,
                    self.data_mount.host_path,
                    self.output_mount.host_path,
                }
            )
            != 3
        ):
            raise ValueError("Docker panel mount roots must be distinct")
        expected = derive_sandbox_mount_authorities_v5(
            owner=self.owner,
            policy_revision=self.policy_revision,
            evaluator_contract=self.evaluator_contract,
            panel=self.panel,
        )
        if (
            self.source_mount.content_authority_sha256,
            self.data_mount.content_authority_sha256,
            self.output_mount.content_authority_sha256,
        ) != expected:
            raise ValueError("Docker panel mount contents differ from request authority")


@dataclass(frozen=True, slots=True)
class ContainerCommandV5:
    request: DockerPanelRequestV5
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.request) is not DockerPanelRequestV5:
            raise ValueError("container command request is invalid")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.argv)
        ):
            raise ValueError("container command argv is invalid")


@dataclass(frozen=True, slots=True)
class ContainerExecutionResultV5:
    status: ContainerStatusV5
    exit_code: int | None
    output_bytes: bytes | None
    leases: tuple[OwnedLeaseV5, ...]

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"}:
            raise ValueError("container execution status is invalid")
        if self.status == "succeeded":
            if self.exit_code != 0:
                raise ValueError("successful container execution requires exit zero")
        elif self.status == "nonzero_exit":
            if type(self.exit_code) is not int or self.exit_code == 0:
                raise ValueError("nonzero container result requires a nonzero exit")
        elif self.exit_code is not None:
            raise ValueError("interrupted container result cannot claim an exit code")
        if self.output_bytes is not None and type(self.output_bytes) is not bytes:
            raise ValueError("container output must be immutable bytes")
        if type(self.leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in self.leases):
            raise ValueError("container execution leases are invalid")


@dataclass(frozen=True, slots=True)
class DockerPanelOutcomeV5:
    evaluation: PanelEvaluationV5 | None
    failure: SandboxFailureV5 | None
    leases: tuple[OwnedLeaseV5, ...]

    def __post_init__(self) -> None:
        if type(self.leases) is not tuple or any(type(item) is not OwnedLeaseV5 for item in self.leases):
            raise ValueError("Docker panel outcome leases are invalid")
        if (self.evaluation is None) == (self.failure is None):
            raise ValueError("Docker panel outcome must contain exactly one result")
        if self.evaluation is not None and type(self.evaluation) is not PanelEvaluationV5:
            raise ValueError("Docker panel outcome evaluation is invalid")
        if self.failure is not None and type(self.failure) is not SandboxFailureV5:
            raise ValueError("Docker panel outcome failure is invalid")


@runtime_checkable
class ContainerExecutorV5(Protocol):
    def execute(self, command: ContainerCommandV5) -> ContainerExecutionResultV5: ...

    def cleanup(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> CleanupResultPayloadV5: ...


@runtime_checkable
class RuntimeLeaseRegistrarV5(Protocol):
    def register_leases(self, leases: tuple[OwnedLeaseV5, ...]) -> None: ...


def derive_sandbox_mount_authorities_v5(
    *,
    owner: WorkspaceOwnerV5,
    policy_revision: PolicyRevisionIdentityV5,
    evaluator_contract: EvaluatorContractV5,
    panel: EpisodePlanV5,
) -> tuple[str, str, str]:
    """Bind source, data, and output handles to their exact semantic contents."""

    if (
        type(owner) is not WorkspaceOwnerV5
        or type(policy_revision) is not PolicyRevisionIdentityV5
        or type(evaluator_contract) is not EvaluatorContractV5
        or type(panel) is not EpisodePlanV5
    ):
        raise ValueError("sandbox mount authority inputs are invalid")
    source = canonical_sha256_v5(
        {
            "evaluator_source_sha256": evaluator_contract.evaluator_source_sha256,
            "policy_revision_sha256": policy_revision.sha256,
        }
    )
    data = canonical_sha256_v5(
        {
            "panel_sha256": panel.panel_ref.sha256,
            "pit_bundle_sha256": evaluator_contract.pit_bundle_sha256,
            "prices_provenance_sha256": evaluator_contract.prices_provenance_sha256,
        }
    )
    output = canonical_sha256_v5(
        {
            "owner_sha256": owner.sha256,
            "panel_sha256": panel.panel_ref.sha256,
            "policy_revision_sha256": policy_revision.sha256,
        }
    )
    return source, data, output


def _mount_arg(mount: SandboxMountHandleV5) -> str:
    options = f"type=bind,src={mount.host_path},dst={mount.container_path}"
    return f"{options},readonly" if mount.mode == "read_only" else options


def build_docker_argv_v5(request: DockerPanelRequestV5) -> tuple[str, ...]:
    """Build the sole production-shaped command form; never a shell string."""

    if type(request) is not DockerPanelRequestV5:
        raise ValueError("Docker argv requires a V5 panel request")
    cpu = canonical_primitive_v5(request.sandbox_profile.cpu_limit)
    assert type(cpu) is str
    argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(request.limits.pid_limit),
        "--cpus",
        cpu,
        "--memory",
        f"{request.sandbox_profile.memory_limit_mib}m",
        "--mount",
        _mount_arg(request.source_mount),
        "--mount",
        _mount_arg(request.data_mount),
        "--mount",
        _mount_arg(request.output_mount),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--",
        request.sandbox_profile.image_reference,
        "python",
        "-B",
        "-m",
        "core.pit_optimizer_v5.container_entry",
        "--evaluator-sha256",
        request.evaluator_contract.sha256,
        "--sandbox-sha256",
        request.sandbox_profile.sha256,
        "--policy-sha256",
        request.policy_revision.sha256,
        "--panel-sha256",
        request.panel.panel_ref.sha256,
        "--start-date",
        request.panel.start_date,
        "--end-date",
        request.panel.end_date,
        "--output-limit-bytes",
        str(request.sandbox_profile.output_limit_bytes),
    ]
    for scenario_id in request.scenario_ids:
        argv.extend(("--scenario", scenario_id))
    return tuple(argv)


T = TypeVar("T")


def _strict_json(raw: bytes) -> object:
    duplicate = False

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result = {}
        for key, value in items:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
        canonical = canonical_json_bytes_v5(decoded)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError):
        raise SandboxAdapterErrorV5(SandboxFailureV5("noncanonical_output")) from None
    if duplicate or canonical != raw:
        raise SandboxAdapterErrorV5(SandboxFailureV5("noncanonical_output"))
    return decoded


def _decode_value(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if annotation is Decimal:
        if type(value) is not str:
            raise ValueError
        parsed = Decimal(value)
        if not parsed.is_finite() or canonical_primitive_v5(parsed) != value:
            raise ValueError
        return parsed
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in arguments):
            raise ValueError
        return value
    if origin in {types.UnionType, __import__("typing").Union}:
        if value is None and type(None) in arguments:
            return None
        successes = []
        for argument in arguments:
            if argument is type(None):
                continue
            try:
                successes.append(_decode_value(argument, value))
            except (TypeError, ValueError, ArithmeticError):
                pass
        if len(successes) != 1:
            raise ValueError
        return successes[0]
    if origin is tuple:
        if type(value) is not list or len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise ValueError
        return tuple(_decode_value(arguments[0], item) for item in value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(annotation, value)
    if annotation in {str, int, bool}:
        if type(value) is not annotation:
            raise ValueError
        return value
    raise TypeError


def _decode_dataclass(cls: type[T], value: object) -> T:
    expected = {item.name for item in fields(cls) if item.init}
    if type(value) is not dict or set(value) != expected:
        raise ValueError
    hints = get_type_hints(cls)
    return cls(**{item.name: _decode_value(hints[item.name], value[item.name]) for item in fields(cls) if item.init})


def decode_panel_evaluation_v5(raw: bytes) -> PanelEvaluationV5:
    try:
        return _decode_dataclass(PanelEvaluationV5, _strict_json(raw))
    except SandboxAdapterErrorV5:
        raise
    except (TypeError, ValueError, ArithmeticError, RecursionError):
        raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema")) from None


class DockerPanelEvaluatorV5:
    def __init__(self, executor: ContainerExecutorV5) -> None:
        if not isinstance(executor, ContainerExecutorV5):
            raise ValueError("Docker panel executor does not implement its V5 protocol")
        self._executor = executor

    @staticmethod
    def _failure(code: SandboxFailureCodeV5, leases: tuple[OwnedLeaseV5, ...] = ()) -> DockerPanelOutcomeV5:
        return DockerPanelOutcomeV5(None, SandboxFailureV5(code), leases)

    @staticmethod
    def _validate_leases(
        request: DockerPanelRequestV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> bool:
        if type(leases) is not tuple or len(leases) != 2:
            return False
        kinds = tuple(item.payload.resource_kind for item in leases)
        if kinds != ("evaluator_process", "container"):
            return False
        return all(
            item.round_index == request.owner.round_index
            and item.payload.owner_campaign_id == request.owner.campaign_id
            and item.payload.owner_token_sha256 == request.owner.owner_token_sha256
            for item in leases
        ) and len({item.payload.lease_id for item in leases}) == len(leases)

    def evaluate(
        self,
        request: DockerPanelRequestV5,
        *,
        registrar: RuntimeLeaseRegistrarV5,
    ) -> DockerPanelOutcomeV5:
        if type(request) is not DockerPanelRequestV5 or not isinstance(registrar, RuntimeLeaseRegistrarV5):
            return self._failure("invalid_request")
        command = ContainerCommandV5(request, build_docker_argv_v5(request))
        try:
            result = self._executor.execute(command)
        except BaseException:
            return self._failure("driver_failed")
        if type(result) is not ContainerExecutionResultV5:
            return self._failure("driver_failed")
        if not self._validate_leases(request, result.leases):
            return self._failure("foreign_lease")
        try:
            registrar.register_leases(result.leases)
        except BaseException:
            return self._failure("foreign_lease")
        if result.status != "succeeded":
            code = {
                "cancelled": "cancelled",
                "timed_out": "timed_out",
                "nonzero_exit": "nonzero_exit",
                "failed": "driver_failed",
            }[result.status]
            return self._failure(code, result.leases)
        if result.output_bytes is None:
            return self._failure("missing_output", result.leases)
        if len(result.output_bytes) > request.sandbox_profile.output_limit_bytes:
            return self._failure("output_too_large", result.leases)
        try:
            evaluation = decode_panel_evaluation_v5(result.output_bytes)
        except SandboxAdapterErrorV5 as exc:
            return DockerPanelOutcomeV5(None, exc.failure, result.leases)
        if (
            evaluation.evaluator_contract_sha256 != request.evaluator_contract.sha256
            or evaluation.sandbox_profile_sha256 != request.sandbox_profile.sha256
            or evaluation.panel_sha256 != request.panel.panel_ref.sha256
            or evaluation.policy_identity_sha256 != request.policy_revision.sha256
            or evaluation.start_date != request.panel.start_date
            or evaluation.end_date != request.panel.end_date
            or evaluation.selection_scenario_id != request.evaluator_contract.selection_scenario_id
            or tuple(item.scenario_id for item in evaluation.scenarios) != request.scenario_ids
        ):
            return self._failure("identity_mismatch", result.leases)
        return DockerPanelOutcomeV5(evaluation, None, result.leases)

    def cleanup(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> CleanupResultPayloadV5:
        if type(owner) is not WorkspaceOwnerV5 or not self._validate_cleanup_leases(owner, leases):
            raise SandboxAdapterErrorV5(SandboxFailureV5("foreign_lease"))
        try:
            result = self._executor.cleanup(owner=owner, leases=leases)
        except BaseException:
            raise SandboxAdapterErrorV5(SandboxFailureV5("cleanup_failed")) from None
        expected = (
            0,
            0,
            sum(item.payload.resource_kind == "evaluator_process" for item in leases),
            sum(item.payload.resource_kind == "container" for item in leases),
        )
        if (
            type(result) is not CleanupResultPayloadV5
            or (
                result.owned_workspaces,
                result.owned_policy_workers,
                result.owned_evaluators,
                result.owned_containers,
            )
            != expected
        ):
            raise SandboxAdapterErrorV5(SandboxFailureV5("cleanup_failed"))
        return result

    @staticmethod
    def _validate_cleanup_leases(owner: WorkspaceOwnerV5, leases: tuple[OwnedLeaseV5, ...]) -> bool:
        return (
            type(leases) is tuple
            and all(
                type(item) is OwnedLeaseV5
                and item.round_index == owner.round_index
                and item.payload.resource_kind in {"evaluator_process", "container"}
                and item.payload.owner_campaign_id == owner.campaign_id
                and item.payload.owner_token_sha256 == owner.owner_token_sha256
                for item in leases
            )
            and len({item.payload.lease_id for item in leases}) == len(leases)
        )


class RuntimeDockerPanelEvaluatorV5:
    """Register executor leases before returning or raising on panel evidence."""

    def __init__(
        self,
        evaluator: DockerPanelEvaluatorV5,
        registrar: RuntimeLeaseRegistrarV5,
    ) -> None:
        if type(evaluator) is not DockerPanelEvaluatorV5 or not isinstance(registrar, RuntimeLeaseRegistrarV5):
            raise ValueError("runtime Docker evaluator dependencies are invalid")
        self._evaluator = evaluator
        self._registrar = registrar

    def evaluate(self, request: DockerPanelRequestV5) -> PanelEvaluationV5:
        outcome = self._evaluator.evaluate(request, registrar=self._registrar)
        if outcome.failure is not None:
            raise SandboxAdapterErrorV5(outcome.failure)
        assert outcome.evaluation is not None
        return outcome.evaluation


__all__ = [
    "ContainerExecutionResultV5",
    "ContainerExecutorV5",
    "ContainerLimitsV5",
    "ContainerStatusV5",
    "DockerPanelEvaluatorV5",
    "DockerPanelOutcomeV5",
    "DockerPanelRequestV5",
    "MountKindV5",
    "MountModeV5",
    "RuntimeDockerPanelEvaluatorV5",
    "RuntimeLeaseRegistrarV5",
    "SandboxAdapterErrorV5",
    "SandboxFailureCodeV5",
    "SandboxFailureV5",
    "SandboxMountHandleV5",
    "build_docker_argv_v5",
    "decode_panel_evaluation_v5",
    "derive_sandbox_mount_authorities_v5",
]
