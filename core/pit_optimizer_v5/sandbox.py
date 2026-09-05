"""Authenticated, injected Docker evaluation boundary for PIT optimizer V5."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
import json
import math
import ntpath
import types
from typing import Literal, Protocol, TypeVar, get_args, get_origin, get_type_hints, runtime_checkable

from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    PolicyRevisionIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
)
from core.pit_optimizer_v5.contracts import (
    CampaignManifestV5,
    CampaignPanelPlanV5,
    EpisodeEvaluationV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    SandboxProfileV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.memory import CleanupResultPayloadV5
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    ProbeObservationV5,
    SemanticFingerprintV5,
)
from core.pit_optimizer_v5.runtime import (
    CandidateExecutionAuthorityV5,
    CandidateExecutionKeyV5,
    CandidateExecutionRegistrarV5,
    CandidateRuntimeV5,
    FeedbackRoundInputV5,
    LeaseAwareCandidateRuntimeV5,
    MaterializedVariantV5,
    OwnedLeaseV5,
    RuntimeClockV5,
    StageDeadlineV5,
)
from core.pit_optimizer_v5.search import ParentCandidateV5
from core.pit_optimizer_v5.workspace import GitCandidateMaterializerV5, WorkspaceOwnerV5


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
ExecutionRoleV5 = Literal["evaluator_process", "container"]
ReservationDispositionV5 = Literal["created", "existing"]
MountKindV5 = Literal["source", "data", "output"]
MountModeV5 = Literal["read_only", "bounded_write_only"]

_FAILURES = frozenset(get_args(SandboxFailureCodeV5))


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _windows_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


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
    """Root-bound mount capability with an independently authenticated content set."""

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

    def authority_primitive(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "root_identity_sha256": self.root_identity_sha256,
            "content_authority_sha256": self.content_authority_sha256,
            "host_path_key": _windows_key(self.host_path),
            "container_path": self.container_path,
            "maximum_bytes": self.maximum_bytes,
        }


def derive_sandbox_mount_authorities_v5(
    *,
    owner: WorkspaceOwnerV5,
    policy_revision: PolicyRevisionIdentityV5,
    evaluator_contract: EvaluatorContractV5,
    sandbox_profile: SandboxProfileV5,
    panel: EpisodePlanV5,
    scenario_ids: tuple[str, ...],
    execution_key: CandidateExecutionKeyV5,
) -> tuple[str, str, str]:
    """Bind every mount's contents to the complete evaluation authority."""

    if (
        type(owner) is not WorkspaceOwnerV5
        or type(policy_revision) is not PolicyRevisionIdentityV5
        or type(evaluator_contract) is not EvaluatorContractV5
        or type(sandbox_profile) is not SandboxProfileV5
        or type(panel) is not EpisodePlanV5
        or type(scenario_ids) is not tuple
        or type(execution_key) is not CandidateExecutionKeyV5
    ):
        raise ValueError("sandbox mount authority inputs are invalid")
    common = {
        "evaluator_contract_sha256": evaluator_contract.sha256,
        "sandbox_profile_sha256": sandbox_profile.sha256,
        "panel_sha256": panel.panel_ref.sha256,
        "policy_revision_sha256": policy_revision.sha256,
        "scenario_ids": scenario_ids,
        "execution_key": canonical_primitive_v5(execution_key),
    }
    return (
        canonical_sha256_v5({**common, "kind": "source"}),
        canonical_sha256_v5(
            {
                **common,
                "kind": "data",
                "pit_bundle_sha256": evaluator_contract.pit_bundle_sha256,
                "prices_provenance_sha256": evaluator_contract.prices_provenance_sha256,
            }
        ),
        canonical_sha256_v5({**common, "kind": "output", "owner_sha256": owner.sha256}),
    )


@dataclass(frozen=True, slots=True)
class DockerPanelRequestV5:
    owner: WorkspaceOwnerV5
    manifest: CampaignManifestV5
    execution_key: CandidateExecutionKeyV5
    policy_revision: PolicyRevisionIdentityV5
    evaluator_contract: EvaluatorContractV5
    sandbox_profile: SandboxProfileV5
    panel: EpisodePlanV5
    scenario_ids: tuple[str, ...]
    source_mount: SandboxMountHandleV5
    data_mount: SandboxMountHandleV5
    output_mount: SandboxMountHandleV5

    def __post_init__(self) -> None:
        if (
            type(self.owner) is not WorkspaceOwnerV5
            or type(self.manifest) is not CampaignManifestV5
            or type(self.execution_key) is not CandidateExecutionKeyV5
            or type(self.policy_revision) is not PolicyRevisionIdentityV5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.sandbox_profile) is not SandboxProfileV5
            or type(self.panel) is not EpisodePlanV5
        ):
            raise ValueError("Docker panel request authority is invalid")
        if (
            self.owner.campaign_id != self.manifest.campaign_id
            or self.owner.round_index > self.manifest.search.max_feedback_rounds
            or self.manifest.evaluator_contract_ref.sha256 != self.evaluator_contract.sha256
            or self.manifest.sandbox_profile_ref.sha256 != self.sandbox_profile.sha256
            or self.evaluator_contract.sandbox_profile_sha256 != self.sandbox_profile.sha256
        ):
            raise ValueError("Docker panel request differs from campaign authority")
        if (
            self.execution_key.stage in {"semantic_probe", "quick_evaluation"}
            and self.panel.episode_ordinal is not None
        ) or (
            self.execution_key.stage == "discovery_evaluation"
            and self.execution_key.episode_ordinal != self.panel.episode_ordinal
        ):
            raise ValueError("Docker panel execution key differs from its panel")
        validate_sandbox_profile_resources_v5(self.sandbox_profile, self.manifest.resources)
        declared = tuple(item.scenario_id for item in self.evaluator_contract.friction_grid)
        permitted_scenarios = (
            {()}
            if self.execution_key.stage == "semantic_probe"
            else {(self.evaluator_contract.selection_scenario_id,), declared}
        )
        if self.scenario_ids not in permitted_scenarios:
            raise ValueError("Docker panel scenarios are outside the quick/full closed scopes")
        if (
            type(self.source_mount) is not SandboxMountHandleV5
            or type(self.data_mount) is not SandboxMountHandleV5
            or type(self.output_mount) is not SandboxMountHandleV5
            or (self.source_mount.kind, self.data_mount.kind, self.output_mount.kind) != ("source", "data", "output")
        ):
            raise ValueError("Docker panel mount capabilities are invalid")
        if self.output_mount.maximum_bytes != self.manifest.resources.evaluation_output_limit_bytes:
            raise ValueError("Docker panel output mount differs from manifest authority")
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
                    _windows_key(self.source_mount.host_path),
                    _windows_key(self.data_mount.host_path),
                    _windows_key(self.output_mount.host_path),
                }
            )
            != 3
        ):
            raise ValueError("Docker panel mount roots must be distinct")
        expected = derive_sandbox_mount_authorities_v5(
            owner=self.owner,
            policy_revision=self.policy_revision,
            evaluator_contract=self.evaluator_contract,
            sandbox_profile=self.sandbox_profile,
            panel=self.panel,
            scenario_ids=self.scenario_ids,
            execution_key=self.execution_key,
        )
        if (
            self.source_mount.content_authority_sha256,
            self.data_mount.content_authority_sha256,
            self.output_mount.content_authority_sha256,
        ) != expected:
            raise ValueError("Docker panel mount contents differ from request authority")

    def to_primitive(self) -> dict[str, object]:
        return {
            "owner_sha256": self.owner.sha256,
            "manifest_sha256": self.manifest.sha256,
            "execution_key": canonical_primitive_v5(self.execution_key),
            "policy_revision": self.policy_revision.to_primitive(),
            "evaluator_contract_sha256": self.evaluator_contract.sha256,
            "sandbox_profile_sha256": self.sandbox_profile.sha256,
            "panel": canonical_primitive_v5(self.panel),
            "scenario_ids": self.scenario_ids,
            "mounts": (
                self.source_mount.authority_primitive(),
                self.data_mount.authority_primitive(),
                self.output_mount.authority_primitive(),
            ),
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self.to_primitive())


@dataclass(frozen=True, slots=True)
class ContainerCommandV5:
    request: DockerPanelRequestV5
    argv: tuple[str, ...]
    remaining_timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self.request) is not DockerPanelRequestV5:
            raise ValueError("container command request is invalid")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.argv)
        ):
            raise ValueError("container command argv is invalid")
        if (
            type(self.remaining_timeout_seconds) is not float
            or not math.isfinite(self.remaining_timeout_seconds)
            or self.remaining_timeout_seconds <= 0
        ):
            raise ValueError("container command timeout is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(
            {
                "request_sha256": self.request.sha256,
                "argv": self.argv,
            }
        )


@dataclass(frozen=True, slots=True)
class ExecutionLeaseV5:
    role_kind: ExecutionRoleV5
    owned_lease: OwnedLeaseV5
    request_sha256: str
    command_sha256: str
    output_mount_authority_sha256: str

    def __post_init__(self) -> None:
        if self.role_kind not in {"evaluator_process", "container"}:
            raise ValueError("execution lease role is invalid")
        if type(self.owned_lease) is not OwnedLeaseV5 or self.owned_lease.payload.resource_kind != self.role_kind:
            raise ValueError("execution lease differs from its owned handle")
        _digest(self.request_sha256, "execution lease request")
        _digest(self.command_sha256, "execution lease command")
        _digest(self.output_mount_authority_sha256, "execution lease output authority")


def derive_execution_lease_id_v5(command_sha256: str, role_kind: ExecutionRoleV5) -> str:
    """Return the sole lease identity permitted for one command role."""

    _digest(command_sha256, "execution lease command")
    if role_kind not in {"evaluator_process", "container"}:
        raise ValueError("execution lease role is invalid")
    return canonical_sha256_v5(
        {
            "schema_version": 5,
            "authority_kind": "candidate_execution_lease",
            "command_sha256": command_sha256,
            "role_kind": role_kind,
        }
    )


@dataclass(frozen=True, slots=True)
class ExecutionReservationV5:
    """Driver reservation that binds cleanup identities before any process launch."""

    command: ContainerCommandV5
    leases: tuple[ExecutionLeaseV5, ...]
    disposition: ReservationDispositionV5
    opaque_reservation: object

    def __post_init__(self) -> None:
        if type(self.command) is not ContainerCommandV5:
            raise ValueError("execution reservation command is invalid")
        if (
            type(self.leases) is not tuple
            or tuple(item.role_kind for item in self.leases) != ("evaluator_process", "container")
            or any(
                type(item) is not ExecutionLeaseV5
                or item.request_sha256 != self.command.request.sha256
                or item.command_sha256 != self.command.sha256
                or item.output_mount_authority_sha256 != self.command.request.output_mount.content_authority_sha256
                or item.owned_lease.payload.lease_id
                != derive_execution_lease_id_v5(self.command.sha256, item.role_kind)
                for item in self.leases
            )
        ):
            raise ValueError("execution reservation leases are invalid")
        if type(self.disposition) is not str or self.disposition not in {"created", "existing"}:
            raise ValueError("execution reservation disposition is invalid")
        if self.opaque_reservation is None:
            raise ValueError("execution reservation requires an opaque driver handle")


@dataclass(frozen=True, slots=True)
class BoundedOutputBytesV5:
    content: bytes | None
    observed_byte_count: int
    request_sha256: str
    command_sha256: str
    output_mount_authority_sha256: str

    def __post_init__(self) -> None:
        if self.content is not None and type(self.content) is not bytes:
            raise ValueError("container output must be immutable bytes")
        if type(self.observed_byte_count) is not int or self.observed_byte_count < 0:
            raise ValueError("container output byte count is invalid")
        if self.observed_byte_count != (0 if self.content is None else len(self.content)):
            raise ValueError("container output byte count differs from returned bytes")
        _digest(self.request_sha256, "container output request")
        _digest(self.command_sha256, "container output command")
        _digest(self.output_mount_authority_sha256, "container output mount authority")


@dataclass(frozen=True, slots=True)
class ContainerExecutionResultV5:
    status: ContainerStatusV5
    exit_code: int | None
    output: BoundedOutputBytesV5
    leases: tuple[ExecutionLeaseV5, ...]

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "cancelled", "timed_out", "nonzero_exit", "failed"}:
            raise ValueError("container execution status is invalid")
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("successful container execution requires exit zero")
        if self.status == "nonzero_exit" and (type(self.exit_code) is not int or self.exit_code == 0):
            raise ValueError("nonzero container result requires a nonzero exit")
        if self.status not in {"succeeded", "nonzero_exit"} and self.exit_code is not None:
            raise ValueError("interrupted container result cannot claim an exit code")
        if type(self.output) is not BoundedOutputBytesV5:
            raise ValueError("container execution output is unbound")
        if type(self.leases) is not tuple or any(type(item) is not ExecutionLeaseV5 for item in self.leases):
            raise ValueError("container execution leases are unbound")


@dataclass(frozen=True, slots=True)
class DockerPanelOutcomeV5:
    evaluation: PanelEvaluationV5 | SemanticFingerprintV5 | None
    failure: SandboxFailureV5 | None
    leases: tuple[ExecutionLeaseV5, ...]

    def __post_init__(self) -> None:
        if type(self.leases) is not tuple or any(type(item) is not ExecutionLeaseV5 for item in self.leases):
            raise ValueError("Docker panel outcome leases are invalid")
        if (self.evaluation is None) == (self.failure is None):
            raise ValueError("Docker panel outcome must contain exactly one result")
        if self.evaluation is not None and type(self.evaluation) not in {
            PanelEvaluationV5,
            SemanticFingerprintV5,
        }:
            raise ValueError("Docker panel outcome evaluation is invalid")
        if self.failure is not None and type(self.failure) is not SandboxFailureV5:
            raise ValueError("Docker panel outcome failure is invalid")


@runtime_checkable
class ContainerExecutorV5(Protocol):
    """Reserve durably first, then start or reconcile exactly one execution.

    ``reserve`` is an atomic create-or-load operation keyed by the command digest.
    Repeated calls must return the same deterministic lease identities and report
    ``existing`` after the first reservation. Collecting an existing reservation
    reconciles that exact execution and must never create or start another one.
    """

    def reserve(self, command: ContainerCommandV5) -> ExecutionReservationV5: ...

    def start(self, reservation: ExecutionReservationV5) -> None: ...

    def collect(
        self,
        reservation: ExecutionReservationV5,
        *,
        remaining_timeout_seconds: float,
    ) -> ContainerExecutionResultV5: ...

    def reconcile(
        self,
        *,
        request: DockerPanelRequestV5,
        authority: CandidateExecutionAuthorityV5,
        remaining_timeout_seconds: float,
    ) -> ExecutionReservationV5 | None: ...

    def cleanup(self, *, owner: WorkspaceOwnerV5, leases: tuple[OwnedLeaseV5, ...]) -> CleanupResultPayloadV5: ...


class AuthenticatedContainerExecutorV5:
    """Identity-bound production container capability supplied by the local host."""

    def __init__(self, *, delegate: ContainerExecutorV5, executor_identity_sha256: str) -> None:
        if not isinstance(delegate, ContainerExecutorV5):
            raise ValueError("production container executor delegate is invalid")
        self.executor_identity_sha256 = _digest(executor_identity_sha256, "container executor identity")
        self._delegate = delegate

    def reserve(self, command: ContainerCommandV5) -> ExecutionReservationV5:
        return self._delegate.reserve(command)

    def start(self, reservation: ExecutionReservationV5) -> None:
        self._delegate.start(reservation)

    def collect(
        self,
        reservation: ExecutionReservationV5,
        *,
        remaining_timeout_seconds: float,
    ) -> ContainerExecutionResultV5:
        return self._delegate.collect(
            reservation,
            remaining_timeout_seconds=remaining_timeout_seconds,
        )

    def reconcile(
        self,
        *,
        request: DockerPanelRequestV5,
        authority: CandidateExecutionAuthorityV5,
        remaining_timeout_seconds: float,
    ) -> ExecutionReservationV5 | None:
        return self._delegate.reconcile(
            request=request,
            authority=authority,
            remaining_timeout_seconds=remaining_timeout_seconds,
        )

    def cleanup(
        self,
        *,
        owner: WorkspaceOwnerV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> CleanupResultPayloadV5:
        return self._delegate.cleanup(owner=owner, leases=leases)


@runtime_checkable
class RuntimeLeaseRegistrarV5(Protocol):
    def register_execution(
        self,
        authority: CandidateExecutionAuthorityV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> None: ...


@runtime_checkable
class CandidateBaseOperationsV5(Protocol):
    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5: ...

    def materialize(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5: ...

    def recover_materialized(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        leases: tuple[OwnedLeaseV5, ...],
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5: ...

    def validate(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> ValidationResultV5: ...

    def fingerprint(
        self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5
    ) -> SemanticFingerprintV5: ...


class AuthenticatedCandidateBaseOperationsV5:
    """Identity-bound source/workspace/probe capability for production composition."""

    def __init__(
        self,
        *,
        delegate: CandidateBaseOperationsV5,
        materializer: GitCandidateMaterializerV5,
        base_identity_sha256: str,
    ) -> None:
        if not isinstance(delegate, CandidateBaseOperationsV5) or type(materializer) is not GitCandidateMaterializerV5:
            raise ValueError("production candidate-base delegate is invalid")
        self.base_identity_sha256 = _digest(base_identity_sha256, "candidate-base identity")
        self._delegate = delegate
        self.materializer = materializer

    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5:
        return self._delegate.load_parent_source(parent)

    def materialize(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        return self._delegate.materialize(
            inputs=inputs,
            experiment_identity=experiment_identity,
            variant=variant,
            deadline=deadline,
        )

    def recover_materialized(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        leases: tuple[OwnedLeaseV5, ...],
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        return self._delegate.recover_materialized(
            inputs=inputs,
            experiment_identity=experiment_identity,
            variant=variant,
            leases=leases,
            deadline=deadline,
        )

    def validate(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> ValidationResultV5:
        return self._delegate.validate(materialized, deadline=deadline)

    def fingerprint(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
    ) -> SemanticFingerprintV5:
        return self._delegate.fingerprint(materialized, deadline=deadline)


@runtime_checkable
class SandboxMountFactoryV5(Protocol):
    def mounts_for(
        self,
        *,
        materialized: MaterializedVariantV5,
        panel: EpisodePlanV5,
        scenario_ids: tuple[str, ...],
        execution_key: CandidateExecutionKeyV5,
    ) -> tuple[SandboxMountHandleV5, SandboxMountHandleV5, SandboxMountHandleV5]: ...


class AuthenticatedSandboxMountFactoryV5:
    """Identity-bound root-handle mount capability for production composition."""

    def __init__(self, *, delegate: SandboxMountFactoryV5, mount_identity_sha256: str) -> None:
        if not isinstance(delegate, SandboxMountFactoryV5):
            raise ValueError("production mount-factory delegate is invalid")
        self.mount_identity_sha256 = _digest(mount_identity_sha256, "sandbox mount-factory identity")
        self._delegate = delegate

    def mounts_for(
        self,
        *,
        materialized: MaterializedVariantV5,
        panel: EpisodePlanV5,
        scenario_ids: tuple[str, ...],
        execution_key: CandidateExecutionKeyV5,
    ) -> tuple[SandboxMountHandleV5, SandboxMountHandleV5, SandboxMountHandleV5]:
        return self._delegate.mounts_for(
            materialized=materialized,
            panel=panel,
            scenario_ids=scenario_ids,
            execution_key=execution_key,
        )


def _mount_arg(mount: SandboxMountHandleV5) -> str:
    options = f"type=bind,src={mount.host_path},dst={mount.container_path}"
    return f"{options},readonly" if mount.mode == "read_only" else options


def execution_output_name_v5(request: DockerPanelRequestV5) -> str:
    if type(request) is not DockerPanelRequestV5:
        raise ValueError("execution output request is invalid")
    return (
        "semantic-fingerprint.json"
        if request.execution_key.stage == "semantic_probe"
        else "panel-evaluation.json"
    )


def build_docker_argv_v5(request: DockerPanelRequestV5) -> tuple[str, ...]:
    if type(request) is not DockerPanelRequestV5:
        raise ValueError("Docker argv requires a V5 panel request")
    cpu = canonical_primitive_v5(request.manifest.resources.evaluation_cpu_limit)
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
        str(request.manifest.resources.evaluation_pid_limit),
        "--cpus",
        cpu,
        "--memory",
        f"{request.manifest.resources.evaluation_memory_mib}m",
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
    ]
    if request.execution_key.stage == "semantic_probe":
        argv.extend(
            (
                "core.pit_optimizer_v5.probe_entry",
                "--request-sha256",
                request.sha256,
                "--policy-sha256",
                request.policy_revision.sha256,
                "--trusted-runtime-sha256",
                request.policy_revision.trusted_policy_runtime_sha256,
                "--immutable-constraints-sha256",
                request.policy_revision.immutable_constraints_sha256,
                "--suite-id",
                PROBE_SUITE_ID_V5,
                "--call-timeout-seconds",
                str(request.manifest.resources.policy_method_timeout_seconds),
                "--output-limit-bytes",
                str(request.manifest.resources.evaluation_output_limit_bytes),
            )
        )
        return tuple(argv)
    argv.extend(
        (
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
            str(request.manifest.resources.evaluation_output_limit_bytes),
        )
    )
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


def decode_semantic_fingerprint_output_v5(
    raw: bytes,
    *,
    request: DockerPanelRequestV5,
) -> SemanticFingerprintV5:
    """Decode one exact request-bound semantic-probe output envelope."""

    try:
        value = _strict_json(raw)
        if type(value) is not dict or set(value) != {
            "request_sha256",
            "policy_revision_sha256",
            "suite_id",
            "fingerprint",
        }:
            raise ValueError
        if (
            value["request_sha256"] != request.sha256
            or value["policy_revision_sha256"] != request.policy_revision.sha256
            or value["suite_id"] != PROBE_SUITE_ID_V5
        ):
            raise SandboxAdapterErrorV5(SandboxFailureV5("identity_mismatch"))
        primitive = value["fingerprint"]
        if type(primitive) is not dict or set(primitive) != {
            "suite_id",
            "observations",
            "fingerprint_sha256",
        }:
            raise ValueError
        raw_observations = primitive["observations"]
        if type(raw_observations) is not list:
            raise ValueError
        observations: list[ProbeObservationV5] = []
        for item in raw_observations:
            if type(item) is not dict or set(item) != {
                "probe_id",
                "method",
                "input_sha256",
                "decision_json_utf8",
            }:
                raise ValueError
            decision = item["decision_json_utf8"]
            if type(decision) is not str:
                raise ValueError
            observations.append(
                ProbeObservationV5(
                    item["probe_id"],  # type: ignore[arg-type]
                    item["method"],  # type: ignore[arg-type]
                    item["input_sha256"],  # type: ignore[arg-type]
                    decision.encode("utf-8"),
                )
            )
        fingerprint = SemanticFingerprintV5(
            primitive["suite_id"],  # type: ignore[arg-type]
            tuple(observations),
            primitive["fingerprint_sha256"],  # type: ignore[arg-type]
        )
        if canonical_json_bytes_v5(fingerprint.to_primitive()) != canonical_json_bytes_v5(primitive):
            raise ValueError
        return fingerprint
    except SandboxAdapterErrorV5:
        raise
    except (TypeError, ValueError, ArithmeticError, UnicodeError, RecursionError):
        raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema")) from None


class DockerPanelEvaluatorV5:
    def __init__(self, *, executor: ContainerExecutorV5, clock: RuntimeClockV5) -> None:
        if not isinstance(executor, ContainerExecutorV5) or not isinstance(clock, RuntimeClockV5):
            raise ValueError("Docker panel evaluator dependencies are invalid")
        self._executor = executor
        self._clock = clock

    @property
    def executor(self) -> ContainerExecutorV5:
        return self._executor

    @staticmethod
    def _failure(code: SandboxFailureCodeV5, leases: tuple[ExecutionLeaseV5, ...] = ()) -> DockerPanelOutcomeV5:
        return DockerPanelOutcomeV5(None, SandboxFailureV5(code), leases)

    def _remaining_timeout(self, request: DockerPanelRequestV5, deadline: StageDeadlineV5) -> float | None:
        if deadline.stage == "semantic_probe":
            cap = request.manifest.resources.mechanics_timeout_seconds
            if (
                request.execution_key.stage != "semantic_probe"
                or request.panel.episode_ordinal is not None
                or request.scenario_ids
            ):
                return None
        elif deadline.stage == "quick_evaluation":
            cap = request.manifest.resources.quick_timeout_seconds
            if request.execution_key.stage != "quick_evaluation" or request.panel.episode_ordinal is not None or request.scenario_ids != (
                request.evaluator_contract.selection_scenario_id,
            ):
                return None
        elif deadline.stage == "discovery_evaluation":
            cap = request.manifest.resources.discovery_episode_timeout_seconds
            if request.execution_key.stage != "discovery_evaluation" or request.panel.episode_ordinal not in {1, 2, 3, 4} or request.scenario_ids != tuple(
                item.scenario_id for item in request.evaluator_contract.friction_grid
            ):
                return None
        else:
            return None
        try:
            now = self._clock.monotonic()
        except BaseException:
            return None
        if type(now) is not float or not math.isfinite(now):
            return None
        remaining = min(float(cap), deadline.expires_at_monotonic - now)
        return remaining if remaining > 0 else 0.0

    @staticmethod
    def _bound_execution(
        request: DockerPanelRequestV5,
        command_sha256: str,
        result: ContainerExecutionResultV5,
    ) -> tuple[ExecutionLeaseV5, ...] | None:
        expected_output = request.output_mount.content_authority_sha256
        if (
            result.output.request_sha256 != request.sha256
            or result.output.command_sha256 != command_sha256
            or result.output.output_mount_authority_sha256 != expected_output
            or tuple(item.role_kind for item in result.leases) != ("evaluator_process", "container")
        ):
            return None
        for execution_lease in result.leases:
            owned = execution_lease.owned_lease
            if (
                execution_lease.request_sha256 != request.sha256
                or execution_lease.command_sha256 != command_sha256
                or execution_lease.output_mount_authority_sha256 != expected_output
                or owned.round_index != request.owner.round_index
                or owned.payload.owner_campaign_id != request.owner.campaign_id
                or owned.payload.owner_token_sha256 != request.owner.owner_token_sha256
            ):
                return None
        owned = tuple(item.owned_lease for item in result.leases)
        return result.leases if len({item.payload.lease_id for item in owned}) == len(owned) else None

    @classmethod
    def _bound_reservation(
        cls,
        request: DockerPanelRequestV5,
        command_sha256: str,
        reservation: ExecutionReservationV5,
        *,
        required_disposition: ReservationDispositionV5 | None = None,
    ) -> tuple[ExecutionLeaseV5, ...] | None:
        if (
            type(reservation) is not ExecutionReservationV5
            or reservation.command.request != request
            or reservation.command.sha256 != command_sha256
            or (required_disposition is not None and reservation.disposition != required_disposition)
        ):
            return None
        probe = ContainerExecutionResultV5(
            "timed_out",
            None,
            BoundedOutputBytesV5(
                None,
                0,
                request.sha256,
                command_sha256,
                request.output_mount.content_authority_sha256,
            ),
            reservation.leases,
        )
        return cls._bound_execution(request, command_sha256, probe)

    @staticmethod
    def _execution_authority(
        request: DockerPanelRequestV5,
        command_sha256: str,
        leases: tuple[ExecutionLeaseV5, ...],
    ) -> CandidateExecutionAuthorityV5:
        return CandidateExecutionAuthorityV5(
            campaign_id=request.owner.campaign_id,
            round_index=request.owner.round_index,
            owner_token_sha256=request.owner.owner_token_sha256,
            key=request.execution_key,
            request_sha256=request.sha256,
            command_sha256=command_sha256,
            output_mount_authority_sha256=request.output_mount.content_authority_sha256,
            lease_payloads=tuple(item.owned_lease.payload for item in leases),
        )

    def _consume_result(
        self,
        request: DockerPanelRequestV5,
        result: ContainerExecutionResultV5,
        execution_leases: tuple[ExecutionLeaseV5, ...],
    ) -> DockerPanelOutcomeV5:
        if result.status != "succeeded":
            code = {
                "cancelled": "cancelled",
                "timed_out": "timed_out",
                "nonzero_exit": "nonzero_exit",
                "failed": "driver_failed",
            }[result.status]
            return self._failure(code, execution_leases)
        output = result.output
        if output.content is None:
            return self._failure("missing_output", execution_leases)
        if output.observed_byte_count > request.manifest.resources.evaluation_output_limit_bytes:
            return self._failure("output_too_large", execution_leases)
        try:
            evaluation = (
                decode_semantic_fingerprint_output_v5(output.content, request=request)
                if request.execution_key.stage == "semantic_probe"
                else decode_panel_evaluation_v5(output.content)
            )
        except SandboxAdapterErrorV5 as exc:
            return DockerPanelOutcomeV5(None, exc.failure, execution_leases)
        if type(evaluation) is SemanticFingerprintV5:
            return DockerPanelOutcomeV5(evaluation, None, execution_leases)
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
            return self._failure("identity_mismatch", execution_leases)
        return DockerPanelOutcomeV5(evaluation, None, execution_leases)

    def evaluate(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        registrar: RuntimeLeaseRegistrarV5,
    ) -> DockerPanelOutcomeV5:
        if (
            type(request) is not DockerPanelRequestV5
            or type(deadline) is not StageDeadlineV5
            or not isinstance(registrar, RuntimeLeaseRegistrarV5)
        ):
            return self._failure("invalid_request")
        remaining = self._remaining_timeout(request, deadline)
        if remaining is None:
            return self._failure("invalid_request")
        if remaining == 0:
            return self._failure("timed_out")
        command = ContainerCommandV5(request, build_docker_argv_v5(request), remaining)
        try:
            reservation = self._executor.reserve(command)
        except BaseException:
            return self._failure("driver_failed")
        execution_leases = self._bound_reservation(request, command.sha256, reservation)
        if execution_leases is None:
            return self._failure("foreign_lease")
        owned_leases = tuple(item.owned_lease for item in execution_leases)
        authority = self._execution_authority(request, command.sha256, execution_leases)
        registrar.register_execution(authority, owned_leases)
        try:
            if reservation.disposition == "created":
                started = self._executor.start(reservation)
                if started is not None:
                    return self._failure("driver_failed", execution_leases)
            result = self._executor.collect(
                reservation,
                remaining_timeout_seconds=remaining,
            )
        except BaseException:
            return self._failure("driver_failed", execution_leases)
        if type(result) is not ContainerExecutionResultV5:
            return self._failure("driver_failed", execution_leases)
        bound_result = self._bound_execution(request, command.sha256, result)
        if bound_result != execution_leases:
            return self._failure("foreign_lease", execution_leases)
        return self._consume_result(request, result, execution_leases)

    def recover(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> DockerPanelOutcomeV5:
        if (
            type(request) is not DockerPanelRequestV5
            or type(deadline) is not StageDeadlineV5
            or type(authority) is not CandidateExecutionAuthorityV5
            or authority.campaign_id != request.owner.campaign_id
            or authority.round_index != request.owner.round_index
            or authority.owner_token_sha256 != request.owner.owner_token_sha256
            or authority.key != request.execution_key
            or authority.request_sha256 != request.sha256
            or authority.output_mount_authority_sha256 != request.output_mount.content_authority_sha256
        ):
            return self._failure("invalid_request")
        remaining = self._remaining_timeout(request, deadline)
        if remaining is None:
            return self._failure("invalid_request")
        if remaining == 0:
            return self._failure("timed_out")
        try:
            reservation = self._executor.reconcile(
                request=request,
                authority=authority,
                remaining_timeout_seconds=remaining,
            )
        except BaseException:
            return self._failure("driver_failed")
        if reservation is None:
            return self._failure("missing_output")
        execution_leases = self._bound_reservation(
            request,
            authority.command_sha256,
            reservation,
            required_disposition="existing",
        )
        if (
            execution_leases is None
            or tuple(item.owned_lease.payload for item in execution_leases) != authority.lease_payloads
        ):
            return self._failure("foreign_lease")
        try:
            result = self._executor.collect(
                reservation,
                remaining_timeout_seconds=remaining,
            )
        except BaseException:
            return self._failure("driver_failed", execution_leases)
        if type(result) is not ContainerExecutionResultV5:
            return self._failure("driver_failed", execution_leases)
        bound_result = self._bound_execution(request, authority.command_sha256, result)
        if bound_result != execution_leases:
            return self._failure("foreign_lease", execution_leases)
        return self._consume_result(request, result, execution_leases)

    def cleanup(self, *, owner: WorkspaceOwnerV5, leases: tuple[ExecutionLeaseV5, ...]) -> CleanupResultPayloadV5:
        if type(owner) is not WorkspaceOwnerV5 or not self._cleanup_leases_match(owner, leases):
            raise SandboxAdapterErrorV5(SandboxFailureV5("foreign_lease"))
        owned_leases = tuple(item.owned_lease for item in leases)
        try:
            result = self._executor.cleanup(owner=owner, leases=owned_leases)
        except BaseException:
            raise SandboxAdapterErrorV5(SandboxFailureV5("cleanup_failed")) from None
        expected = (
            0,
            0,
            sum(item.payload.resource_kind == "evaluator_process" for item in owned_leases),
            sum(item.payload.resource_kind == "container" for item in owned_leases),
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
    def _cleanup_leases_match(owner: WorkspaceOwnerV5, leases: tuple[ExecutionLeaseV5, ...]) -> bool:
        if type(leases) is not tuple or tuple(item.role_kind for item in leases) != (
            "evaluator_process",
            "container",
        ):
            return False
        owned = tuple(item.owned_lease for item in leases)
        return all(
            item.round_index == owner.round_index
            and item.payload.owner_campaign_id == owner.campaign_id
            and item.payload.owner_token_sha256 == owner.owner_token_sha256
            for item in owned
        ) and len({item.payload.lease_id for item in owned}) == len(owned)


class RuntimeDockerPanelEvaluatorV5:
    def __init__(
        self,
        evaluator: DockerPanelEvaluatorV5,
        registrar: RuntimeLeaseRegistrarV5 | None = None,
    ) -> None:
        if type(evaluator) is not DockerPanelEvaluatorV5 or (
            registrar is not None and not isinstance(registrar, RuntimeLeaseRegistrarV5)
        ):
            raise ValueError("runtime Docker evaluator dependencies are invalid")
        self._evaluator = evaluator
        self._registrar = registrar

    @property
    def evaluator(self) -> DockerPanelEvaluatorV5:
        return self._evaluator

    @property
    def registrar(self) -> RuntimeLeaseRegistrarV5 | None:
        return self._registrar

    def evaluate(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        registrar: RuntimeLeaseRegistrarV5 | None = None,
    ) -> PanelEvaluationV5:
        selected_registrar = self._registrar if registrar is None else registrar
        if selected_registrar is None:
            raise ValueError("runtime Docker evaluation requires durable registration")
        if not isinstance(selected_registrar, RuntimeLeaseRegistrarV5):
            raise ValueError("runtime Docker evaluator registrar is invalid")
        outcome = self._evaluator.evaluate(
            request,
            deadline=deadline,
            registrar=selected_registrar,
        )
        if outcome.failure is not None:
            raise SandboxAdapterErrorV5(outcome.failure)
        if type(outcome.evaluation) is not PanelEvaluationV5:
            raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema"))
        return outcome.evaluation

    def recover(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> PanelEvaluationV5:
        outcome = self._evaluator.recover(
            request,
            deadline=deadline,
            authority=authority,
        )
        if outcome.failure is not None:
            raise SandboxAdapterErrorV5(outcome.failure)
        if type(outcome.evaluation) is not PanelEvaluationV5:
            raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema"))
        return outcome.evaluation

    def fingerprint(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        registrar: RuntimeLeaseRegistrarV5 | None = None,
    ) -> SemanticFingerprintV5:
        selected_registrar = self._registrar if registrar is None else registrar
        if selected_registrar is None:
            raise ValueError("runtime Docker evaluation requires durable registration")
        if not isinstance(selected_registrar, RuntimeLeaseRegistrarV5):
            raise ValueError("runtime Docker evaluator registrar is invalid")
        outcome = self._evaluator.evaluate(
            request,
            deadline=deadline,
            registrar=selected_registrar,
        )
        if outcome.failure is not None:
            raise SandboxAdapterErrorV5(outcome.failure)
        if type(outcome.evaluation) is not SemanticFingerprintV5:
            raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema"))
        return outcome.evaluation

    def recover_fingerprint(
        self,
        request: DockerPanelRequestV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> SemanticFingerprintV5:
        outcome = self._evaluator.recover(
            request,
            deadline=deadline,
            authority=authority,
        )
        if outcome.failure is not None:
            raise SandboxAdapterErrorV5(outcome.failure)
        if type(outcome.evaluation) is not SemanticFingerprintV5:
            raise SandboxAdapterErrorV5(SandboxFailureV5("output_schema"))
        return outcome.evaluation


class _CallbackExecutionRegistrarV5:
    def __init__(self, callback: CandidateExecutionRegistrarV5) -> None:
        if not callable(callback):
            raise ValueError("candidate execution registrar is invalid")
        self._callback = callback

    def register_execution(
        self,
        authority: CandidateExecutionAuthorityV5,
        leases: tuple[OwnedLeaseV5, ...],
    ) -> None:
        self._callback(authority, leases)


class DockerCandidateRuntimeV5:
    """Concrete CandidateRuntimeV5 composition using Docker for quick/episode panels."""

    def __init__(
        self,
        *,
        manifest: CampaignManifestV5,
        panel_plan: CampaignPanelPlanV5,
        evaluator_contract: EvaluatorContractV5,
        sandbox_profile: SandboxProfileV5,
        owner: WorkspaceOwnerV5,
        base: CandidateBaseOperationsV5,
        mounts: SandboxMountFactoryV5,
        evaluator: RuntimeDockerPanelEvaluatorV5,
    ) -> None:
        if (
            type(manifest) is not CampaignManifestV5
            or type(panel_plan) is not CampaignPanelPlanV5
            or type(evaluator_contract) is not EvaluatorContractV5
            or type(sandbox_profile) is not SandboxProfileV5
            or type(owner) is not WorkspaceOwnerV5
            or not isinstance(base, CandidateBaseOperationsV5)
            or not isinstance(mounts, SandboxMountFactoryV5)
            or type(evaluator) is not RuntimeDockerPanelEvaluatorV5
        ):
            raise ValueError("Docker candidate runtime dependencies are invalid")
        if (
            owner.campaign_id != manifest.campaign_id
            or owner.round_index > manifest.search.max_feedback_rounds
            or manifest.panel_plan_ref.sha256 != panel_plan.sha256
            or manifest.evaluator_contract_ref.sha256 != evaluator_contract.sha256
            or manifest.sandbox_profile_ref.sha256 != sandbox_profile.sha256
            or evaluator_contract.sandbox_profile_sha256 != sandbox_profile.sha256
        ):
            raise ValueError("Docker candidate runtime authority is inconsistent")
        self._manifest = manifest
        self._panel_plan = panel_plan
        self._contract = evaluator_contract
        self._profile = sandbox_profile
        self._owner = owner
        self._base = base
        self._mounts = mounts
        self._evaluator = evaluator
        if not isinstance(self, CandidateRuntimeV5) or not isinstance(self, LeaseAwareCandidateRuntimeV5):
            raise ValueError("Docker candidate runtime does not satisfy CandidateRuntimeV5")

    @property
    def base_operations(self) -> CandidateBaseOperationsV5:
        return self._base

    @property
    def manifest(self) -> CampaignManifestV5:
        return self._manifest

    @property
    def panel_plan(self) -> CampaignPanelPlanV5:
        return self._panel_plan

    @property
    def evaluator_contract(self) -> EvaluatorContractV5:
        return self._contract

    @property
    def sandbox_profile(self) -> SandboxProfileV5:
        return self._profile

    @property
    def owner(self) -> WorkspaceOwnerV5:
        return self._owner

    @property
    def mount_factory(self) -> SandboxMountFactoryV5:
        return self._mounts

    @property
    def panel_evaluator(self) -> RuntimeDockerPanelEvaluatorV5:
        return self._evaluator

    def load_parent_source(self, parent: ParentCandidateV5) -> SourceBundleV5:
        return self._base.load_parent_source(parent)

    def materialize(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        self._require_inputs(inputs)
        return self._base.materialize(
            inputs=inputs,
            experiment_identity=experiment_identity,
            variant=variant,
            deadline=deadline,
        )

    def recover_materialized(
        self,
        *,
        inputs: FeedbackRoundInputV5,
        experiment_identity: ExperimentIdentityV5,
        variant: RenderedVariantV5,
        leases: tuple[OwnedLeaseV5, ...],
        deadline: StageDeadlineV5,
    ) -> MaterializedVariantV5:
        self._require_inputs(inputs)
        return self._base.recover_materialized(
            inputs=inputs,
            experiment_identity=experiment_identity,
            variant=variant,
            leases=leases,
            deadline=deadline,
        )

    def validate(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> ValidationResultV5:
        return self._base.validate(materialized, deadline=deadline)

    def fingerprint(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> SemanticFingerprintV5:
        request = self._request(
            materialized,
            self._panel_plan.mechanics,
            (),
            CandidateExecutionKeyV5(materialized.variant.sha256, "semantic_probe", None),
        )
        return self._evaluator.fingerprint(request, deadline=deadline)

    def fingerprint_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        register_execution: CandidateExecutionRegistrarV5,
    ) -> SemanticFingerprintV5:
        request = self._request(
            materialized,
            self._panel_plan.mechanics,
            (),
            execution_key,
        )
        return self._evaluator.fingerprint(
            request,
            deadline=deadline,
            registrar=_CallbackExecutionRegistrarV5(register_execution),
        )

    def recover_fingerprint_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> SemanticFingerprintV5:
        request = self._request(
            materialized,
            self._panel_plan.mechanics,
            (),
            authority.key,
        )
        return self._evaluator.recover_fingerprint(
            request,
            deadline=deadline,
            authority=authority,
        )

    def _require_inputs(self, inputs: FeedbackRoundInputV5) -> None:
        if (
            type(inputs) is not FeedbackRoundInputV5
            or inputs.manifest != self._manifest
            or inputs.panel_plan != self._panel_plan
            or inputs.evaluator_contract != self._contract
            or inputs.round_index != self._owner.round_index
            or inputs.owner_token_sha256 != self._owner.owner_token_sha256
        ):
            raise ValueError("Docker candidate runtime received different round authority")

    def _request(
        self,
        materialized: MaterializedVariantV5,
        panel: EpisodePlanV5,
        scenario_ids: tuple[str, ...],
        execution_key: CandidateExecutionKeyV5,
    ) -> DockerPanelRequestV5:
        if type(materialized) is not MaterializedVariantV5:
            raise ValueError("Docker candidate runtime materialization is invalid")
        supplied = self._mounts.mounts_for(
            materialized=materialized,
            panel=panel,
            scenario_ids=scenario_ids,
            execution_key=execution_key,
        )
        if type(supplied) is not tuple or len(supplied) != 3:
            raise ValueError("Docker candidate runtime mounts are invalid")
        return DockerPanelRequestV5(
            owner=self._owner,
            manifest=self._manifest,
            execution_key=execution_key,
            policy_revision=materialized.variant.policy_revision,
            evaluator_contract=self._contract,
            sandbox_profile=self._profile,
            panel=panel,
            scenario_ids=scenario_ids,
            source_mount=supplied[0],
            data_mount=supplied[1],
            output_mount=supplied[2],
        )

    def evaluate_quick(self, materialized: MaterializedVariantV5, *, deadline: StageDeadlineV5) -> PanelEvaluationV5:
        scenarios = (self._contract.selection_scenario_id,)
        request = self._request(
            materialized,
            self._panel_plan.quick,
            scenarios,
            CandidateExecutionKeyV5(materialized.variant.sha256, "quick_evaluation", None),
        )
        return self._evaluator.evaluate(request, deadline=deadline)

    def evaluate_quick_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        register_execution: CandidateExecutionRegistrarV5,
    ) -> PanelEvaluationV5:
        scenarios = (self._contract.selection_scenario_id,)
        request = self._request(materialized, self._panel_plan.quick, scenarios, execution_key)
        return self._evaluator.evaluate(
            request,
            deadline=deadline,
            registrar=_CallbackExecutionRegistrarV5(register_execution),
        )

    def recover_quick_registered(
        self,
        materialized: MaterializedVariantV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> PanelEvaluationV5:
        scenarios = (self._contract.selection_scenario_id,)
        request = self._request(
            materialized,
            self._panel_plan.quick,
            scenarios,
            authority.key,
        )
        return self._evaluator.recover(
            request,
            deadline=deadline,
            authority=authority,
        )

    def evaluate_episode(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
    ) -> EpisodeEvaluationV5:
        return self._evaluate_episode(
            materialized,
            episode,
            deadline=deadline,
            execution_key=CandidateExecutionKeyV5(
                materialized.variant.sha256,
                "discovery_evaluation",
                episode.episode_ordinal,
            ),
            registrar=None,
            recovery_authority=None,
        )

    def evaluate_episode_registered(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        register_execution: CandidateExecutionRegistrarV5,
    ) -> EpisodeEvaluationV5:
        return self._evaluate_episode(
            materialized,
            episode,
            deadline=deadline,
            execution_key=execution_key,
            registrar=_CallbackExecutionRegistrarV5(register_execution),
            recovery_authority=None,
        )

    def recover_episode_registered(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
        authority: CandidateExecutionAuthorityV5,
    ) -> EpisodeEvaluationV5:
        return self._evaluate_episode(
            materialized,
            episode,
            deadline=deadline,
            execution_key=authority.key,
            registrar=None,
            recovery_authority=authority,
        )

    def _evaluate_episode(
        self,
        materialized: MaterializedVariantV5,
        episode: EpisodePlanV5,
        *,
        deadline: StageDeadlineV5,
        execution_key: CandidateExecutionKeyV5,
        registrar: RuntimeLeaseRegistrarV5 | None,
        recovery_authority: CandidateExecutionAuthorityV5 | None,
    ) -> EpisodeEvaluationV5:
        if episode not in self._panel_plan.discovery:
            raise ValueError("Docker candidate runtime episode is outside the discovery plan")
        scenarios = tuple(item.scenario_id for item in self._contract.friction_grid)
        request = self._request(materialized, episode, scenarios, execution_key)
        evaluation = (
            self._evaluator.evaluate(
                request,
                deadline=deadline,
                registrar=registrar,
            )
            if recovery_authority is None
            else self._evaluator.recover(
                request,
                deadline=deadline,
                authority=recovery_authority,
            )
        )
        assert episode.episode_ordinal is not None
        return EpisodeEvaluationV5(
            episode.episode_id,
            episode.episode_ordinal,
            episode.start_date,
            episode.end_date,
            evaluation,
        )


__all__ = [
    "AuthenticatedCandidateBaseOperationsV5",
    "AuthenticatedContainerExecutorV5",
    "AuthenticatedSandboxMountFactoryV5",
    "BoundedOutputBytesV5",
    "CandidateBaseOperationsV5",
    "ContainerExecutionResultV5",
    "ContainerExecutorV5",
    "ContainerStatusV5",
    "DockerCandidateRuntimeV5",
    "DockerPanelEvaluatorV5",
    "DockerPanelOutcomeV5",
    "DockerPanelRequestV5",
    "ExecutionLeaseV5",
    "ExecutionReservationV5",
    "ExecutionRoleV5",
    "MountKindV5",
    "MountModeV5",
    "RuntimeDockerPanelEvaluatorV5",
    "RuntimeLeaseRegistrarV5",
    "ReservationDispositionV5",
    "SandboxAdapterErrorV5",
    "SandboxFailureCodeV5",
    "SandboxFailureV5",
    "SandboxMountFactoryV5",
    "SandboxMountHandleV5",
    "build_docker_argv_v5",
    "decode_panel_evaluation_v5",
    "decode_semantic_fingerprint_output_v5",
    "derive_sandbox_mount_authorities_v5",
    "derive_execution_lease_id_v5",
    "execution_output_name_v5",
]
