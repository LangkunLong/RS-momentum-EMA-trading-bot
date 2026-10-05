"""Registered, source-bound mechanism workers backed by the V5 container executor."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from core.strategy_policy.contracts import ExitDecision

from .candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from .mechanism_artifacts import MechanismBoundCandidateV1
from .mechanism_entry import decode_mechanism_case_output_v1
from .mechanism_probes import (
    MechanismPairedCaseV1,
    MechanismWorkerRegistrationV1,
    MechanismWorkerRoleV1,
)
from .production_sandbox import LocalContainerExecutorV5, LocalSandboxMountFactoryV5
from .sandbox import (
    ContainerCommandV5,
    ContainerExecutionResultV5,
    ExecutionReservationV5,
    MechanismDockerCaseRequestV1,
    build_docker_argv_v5,
    derive_mechanism_case_mount_authorities_v1,
    mechanism_request_allocation_v1,
)
from .workspace import WorkspaceOwnerV5


@dataclass(slots=True)
class _MechanismDockerSessionV1:
    worker_identity: object
    next_case_index: int = 0
    pending_case: MechanismPairedCaseV1 | None = None
    pending_repetition: int | None = None
    pending_decision: ExitDecision | None = None


class MechanismDockerCaseWorkerV1:
    """One registered worker role; each reset executes one isolated case container."""

    def __init__(
        self,
        *,
        bound: MechanismBoundCandidateV1,
        owner: WorkspaceOwnerV5,
        role: MechanismWorkerRoleV1,
        executor: LocalContainerExecutorV5,
        inject_first_candidate_crash: bool = False,
    ) -> None:
        if (
            type(bound) is not MechanismBoundCandidateV1
            or type(owner) is not WorkspaceOwnerV5
            or role not in {"parent", "candidate"}
            or type(executor) is not LocalContainerExecutorV5
            or owner != executor.owner
            or type(inject_first_candidate_crash) is not bool
            or (inject_first_candidate_crash and role != "candidate")
            or type(executor.mount_factory) is not LocalSandboxMountFactoryV5
            or executor.mount_factory.repository is not executor.repository
        ):
            raise ValueError("registered mechanism worker authority is invalid")
        capability = bound.capability
        if (
            capability.execution_kind != "registered_sandbox"
            or capability.authenticated_manifest.manifest != executor.manifest
            or capability.authenticated_manifest.sandbox_profile != executor.sandbox_profile
            or owner.campaign_id != capability.campaign_id
            or owner.round_index != capability.round_index
        ):
            raise ValueError("registered mechanism worker differs from the campaign sandbox")
        if role == "parent":
            revision = capability.parent_revision
            source = capability.parent_source_bundle
        else:
            revision = bound.candidate_revision
            source = bound.candidate_source_bundle
        self.registration = MechanismWorkerRegistrationV1(
            experiment_id=bound.binding.experiment_id,
            spec_sha256=bound.binding.spec_sha256,
            role=role,
            parent_revision_sha256=bound.binding.parent_revision_sha256,
            candidate_bytes_sha256=bound.binding.candidate_bytes_sha256,
            corpus_sha256=bound.binding.corpus_sha256,
            resource_budget=bound.binding.resource_budget,
            execution_kind="registered_sandbox",
            reset_semantics="reset_per_case",
            cpu_memory_enforced=True,
        )
        self._bound = bound
        self._owner = owner
        self._role = role
        self._revision: PolicyRevisionIdentityV5 = revision
        self._source: SourceBundleV5 = source
        self._executor = executor
        self._mount_factory = executor.mount_factory
        self._inject_first_candidate_crash = inject_first_candidate_crash
        self._identity = object()

    def open(self) -> object:
        return _MechanismDockerSessionV1(self._identity)

    def reset(
        self,
        session: object,
        case: MechanismPairedCaseV1,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if (
            type(session) is not _MechanismDockerSessionV1
            or session.worker_identity is not self._identity
            or type(case) is not MechanismPairedCaseV1
            or session.pending_case is not None
            or type(deadline_monotonic) not in {float, type(None)}
            or deadline_monotonic is not None and not math.isfinite(deadline_monotonic)
        ):
            raise RuntimeError("registered mechanism worker session is invalid")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("registered mechanism run deadline expired before case reset")
        cases = self._bound.capability.corpus.cases
        if not cases:
            raise RuntimeError("registered mechanism corpus is empty")
        repetition, case_index = divmod(session.next_case_index, len(cases))
        if case_index >= len(cases) or cases[case_index] != case:
            raise ValueError("registered mechanism reset is outside the bound case order")
        failure_mode = (
            "terminate_child_after_ready"
            if self._inject_first_candidate_crash and self._role == "candidate" and repetition == 0 and case.order == 0
            else "none"
        )
        execution_authority, output_authority = derive_mechanism_case_mount_authorities_v1(
            owner=self._owner,
            binding=self._bound.binding,
            role=self._role,
            policy_revision=self._revision,
            source_bundle=self._source,
            corpus=self._bound.capability.corpus,
            case=case,
            repetition=repetition,
            sandbox_profile=self._bound.capability.authenticated_manifest.sandbox_profile,
            failure_mode=failure_mode,
        )
        manifest = self._bound.capability.authenticated_manifest.manifest
        profile = self._bound.capability.authenticated_manifest.sandbox_profile
        output_limit = min(
            mechanism_request_allocation_v1(self._bound.binding)[3],
            manifest.resources.evaluation_output_limit_bytes,
            profile.output_limit_bytes,
        )
        output_mount = self._mount_factory.mechanism_case_output_mount(
            execution_authority_sha256=execution_authority,
            output_authority_sha256=output_authority,
            source_bundle_sha256=self._source.sha256,
            maximum_bytes=output_limit,
        )
        request = MechanismDockerCaseRequestV1(
            owner=self._owner,
            manifest=manifest,
            sandbox_profile=profile,
            role=self._role,
            policy_revision=self._revision,
            source_bundle=self._source,
            binding=self._bound.binding,
            corpus=self._bound.capability.corpus,
            case=case,
            repetition=repetition,
            output_mount=output_mount,
            failure_mode=failure_mode,
        )
        case_deadline_monotonic = time.monotonic() + request.timeout_seconds
        if deadline_monotonic is not None:
            case_deadline_monotonic = min(case_deadline_monotonic, deadline_monotonic)
        remaining_timeout_seconds = case_deadline_monotonic - time.monotonic()
        if remaining_timeout_seconds <= 0:
            raise TimeoutError("registered mechanism run deadline expired before container reservation")
        command = ContainerCommandV5(
            request,
            build_docker_argv_v5(request),
            float(remaining_timeout_seconds),
            case_deadline_monotonic,
        )
        decision = self._execute_case(
            command,
            session=session,
            deadline_monotonic=case_deadline_monotonic,
        )
        session.pending_case = case
        session.pending_repetition = repetition
        session.pending_decision = decision

    def _execute_case(
        self,
        command: ContainerCommandV5,
        *,
        session: _MechanismDockerSessionV1,
        deadline_monotonic: float | None,
    ) -> ExitDecision:
        request = command.request
        if type(request) is not MechanismDockerCaseRequestV1:
            raise ValueError("registered mechanism command is not a case request")
        deadline = (
            command.deadline_monotonic
            if command.deadline_monotonic is not None
            else time.monotonic() + min(command.remaining_timeout_seconds, request.timeout_seconds)
        )
        if deadline_monotonic is not None:
            deadline = min(deadline, deadline_monotonic)
        reservation: ExecutionReservationV5 | None = None
        primary_error: BaseException | None = None
        try:
            reservation = self._executor.reserve(command)
            if reservation.command != command or reservation.disposition not in {"created", "existing"}:
                raise RuntimeError("registered mechanism reservation differs from its command")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("registered mechanism case timed out before container start")
            expected_leases = tuple(
                sorted(reservation.leases, key=lambda item: item.role_kind)
            )
            if (
                len(expected_leases) != 2
                or any(
                    item.request_sha256 != request.sha256
                    or item.command_sha256 != command.sha256
                    or item.output_mount_authority_sha256 != request.output_mount.content_authority_sha256
                    for item in expected_leases
                )
            ):
                raise RuntimeError("registered mechanism leases differ from command authority")
            self._executor.start(reservation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("registered mechanism case timed out before collection")
            result = self._executor.collect(reservation, remaining_timeout_seconds=float(remaining))
            return self._decode_result(request, command, reservation, result)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if reservation is not None:
                cleanup = self._executor.cleanup(
                    owner=self._owner,
                    leases=tuple(item.owned_lease for item in reservation.leases),
                )
                if not cleanup.cleanup_complete:
                    error = RuntimeError("registered mechanism container cleanup did not complete")
                    if primary_error is not None:
                        raise error from primary_error
                    raise error

    @staticmethod
    def _decode_result(
        request: MechanismDockerCaseRequestV1,
        command: ContainerCommandV5,
        reservation: ExecutionReservationV5,
        result: object,
    ) -> ExitDecision:
        if type(result) is not ContainerExecutionResultV5:
            raise RuntimeError("registered mechanism executor returned an untyped result")
        if (
            result.leases != reservation.leases
            or result.output.request_sha256 != request.sha256
            or result.output.command_sha256 != command.sha256
            or result.output.output_mount_authority_sha256 != request.output_mount.content_authority_sha256
        ):
            raise RuntimeError("registered mechanism result differs from its execution leases")
        if result.status == "timed_out":
            raise TimeoutError("registered mechanism container timed out")
        if result.status != "succeeded" or result.exit_code != 0:
            raise RuntimeError("registered mechanism container exited without a complete decision")
        raw = result.output.content
        if raw is None:
            raise ValueError("registered mechanism output is missing")
        return decode_mechanism_case_output_v1(
            raw,
            expected_request_sha256=request.sha256,
            expected_binding_sha256=request.binding.sha256,
            expected_role=request.role,
            expected_policy_sha256=request.policy_revision.sha256,
            expected_source_bundle_sha256=request.source_bundle.sha256,
            expected_corpus_sha256=request.corpus.sha256,
            expected_case=request.case,
            expected_repetition=request.repetition,
            maximum_bytes=request.output_limit_bytes,
        )

    def evaluate(
        self,
        session: object,
        case: MechanismPairedCaseV1,
        *,
        deadline_monotonic: float | None,
    ) -> object:
        if (
            type(session) is not _MechanismDockerSessionV1
            or session.worker_identity is not self._identity
            or session.pending_case != case
            or type(session.pending_decision) is not ExitDecision
            or type(deadline_monotonic) not in {float, type(None)}
            or deadline_monotonic is not None
            and (not math.isfinite(deadline_monotonic) or time.monotonic() >= deadline_monotonic)
        ):
            raise RuntimeError("registered mechanism decision is unavailable at its deadline")
        decision = session.pending_decision
        session.pending_case = None
        session.pending_repetition = None
        session.pending_decision = None
        session.next_case_index += 1
        return decision

    def close(self, session: object) -> None:
        if type(session) is not _MechanismDockerSessionV1 or session.worker_identity is not self._identity:
            raise RuntimeError("registered mechanism worker session is foreign")
        session.pending_case = None
        session.pending_repetition = None
        session.pending_decision = None


class MechanismDockerCaseWorkerFactoryV1:
    """Create the exact parent/candidate worker pair for an authenticated bound run."""

    def __init__(
        self,
        *,
        owner: WorkspaceOwnerV5,
        executor: LocalContainerExecutorV5,
        inject_first_candidate_crash: bool = False,
    ) -> None:
        if (
            type(owner) is not WorkspaceOwnerV5
            or type(executor) is not LocalContainerExecutorV5
            or owner != executor.owner
            or type(inject_first_candidate_crash) is not bool
        ):
            raise ValueError("registered mechanism worker factory authority is invalid")
        self.owner = owner
        self.executor = executor
        self.inject_first_candidate_crash = inject_first_candidate_crash

    def __call__(
        self,
        bound: MechanismBoundCandidateV1,
    ) -> tuple[MechanismDockerCaseWorkerV1, MechanismDockerCaseWorkerV1]:
        if type(bound) is not MechanismBoundCandidateV1 or bound.capability.execution_kind != "registered_sandbox":
            raise ValueError("registered mechanism factory received a foreign binding")
        parent = MechanismDockerCaseWorkerV1(
            bound=bound,
            owner=self.owner,
            role="parent",
            executor=self.executor,
        )
        candidate = MechanismDockerCaseWorkerV1(
            bound=bound,
            owner=self.owner,
            role="candidate",
            executor=self.executor,
            inject_first_candidate_crash=self.inject_first_candidate_crash,
        )
        return parent, candidate


__all__ = ["MechanismDockerCaseWorkerFactoryV1", "MechanismDockerCaseWorkerV1"]
