"""Source-first V5 baseline authority, with no implicit evaluation capability.

Verification is read-only. The production capture factory owns a pre-manifest
network-disabled sandbox. No host-evaluation fallback is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from contextlib import ExitStack
import hashlib
import ntpath
import os
from pathlib import Path
import re
import subprocess
from typing import Literal, Protocol
import uuid

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_v5.artifacts import _decode_dataclass, _extract_artifact_refs, _strict_json_object
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    AuthenticatedArtifactV5,
    BaselineAuthorityV5,
    CampaignEvidenceV5,
    CampaignPanelPlanV5,
    EpisodeEvaluationV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    ResourceCapabilitiesV5,
    SandboxProfileV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    evaluator_source_sha256,
    sandbox_profile_from_manifest_v5,
    selected_scenario,
    validate_episode_plan_panel_v5,
    validate_sandbox_profile_resources_v5,
)
from core.pit_optimizer_v5.manifest import PolicyScopeDescriptorV5, PolicySourceSnapshotV5
from core.pit_optimizer_v5.probes import SemanticFingerprintV5
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5, baseline_parent_candidate_v5, campaign_cagr_pct


def _references(value) -> tuple[ArtifactRefV5, ...]:
    if type(value.schema_version) is not int or value.schema_version != 5:
        raise ValueError("baseline record schema must be V5")
    refs = tuple(getattr(value, field.name) for field in fields(value) if field.name.endswith("_ref"))
    if any(type(ref) is not ArtifactRefV5 for ref in refs):
        raise ValueError("baseline record requires complete references")
    return refs


@dataclass(frozen=True, slots=True)
class BaselineCaptureInputsV5:
    schema_version: Literal[5]
    evaluator_contract_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    panel_plan_ref: ArtifactRefV5
    baseline_policy_revision_ref: ArtifactRefV5
    source_bundle_ref: ArtifactRefV5
    policy_scope_ref: ArtifactRefV5
    evaluator_source_ref: ArtifactRefV5
    identity_transition_ref: ArtifactRefV5
    resources_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        refs = _references(self)
        if len({ref.relative_path for ref in refs}) != len(refs):
            raise ValueError("baseline inputs require distinct dependency paths")


@dataclass(frozen=True, slots=True)
class BaselineRunV5:
    schema_version: Literal[5]
    capture_inputs_ref: ArtifactRefV5
    mechanics_evidence_ref: ArtifactRefV5
    quick_evidence_ref: ArtifactRefV5
    discovery_evidence_ref: ArtifactRefV5
    semantic_fingerprint_ref: ArtifactRefV5
    source_unchanged: Literal[True]
    cleanup_complete: Literal[True]

    def __post_init__(self) -> None:
        _references(self)
        if self.source_unchanged is not True or self.cleanup_complete is not True:
            raise ValueError("baseline capture requires unchanged source and completed cleanup")


@dataclass(frozen=True, slots=True)
class BaselineDeterministicRepeatV5:
    schema_version: Literal[5]
    first_run_ref: ArtifactRefV5
    second_run_ref: ArtifactRefV5
    fresh_workers: Literal[True]
    byte_identical: Literal[True]

    def __post_init__(self) -> None:
        _references(self)
        if (
            self.first_run_ref.relative_path == self.second_run_ref.relative_path
            or self.fresh_workers is not True
            or self.byte_identical is not True
        ):
            raise ValueError("baseline repeat requires distinct fresh runs and exact byte equality")


@dataclass(frozen=True, slots=True)
class AuthenticatedBaselineInputsV5:
    reference: ArtifactRefV5
    inputs: BaselineCaptureInputsV5
    evaluator: EvaluatorContractV5
    execution: ExecutionProfileV5
    sandbox: SandboxProfileV5
    plan: CampaignPanelPlanV5
    policy: PolicyRevisionIdentityV5
    source: SourceBundleV5
    scope: PolicyScopeDescriptorV5
    resources: ResourceCapabilitiesV5


class BaselineRuntimeUnavailableV5(ValueError):
    """Programmatic capture did not supply its explicit sandbox composition."""


class BaselineCaptureWorkerV5(Protocol):
    """An explicitly authorized fresh sandbox worker; no provider capability.

    bind authenticates the bundle/provenance/transition, image/runtime, resources,
    and unchanged clean source before any action. Each evaluate call must launch
    fresh simulators/policy clients for all requested scenarios. close completes
    owned cleanup even after failure. This protocol is a trusted injection seam,
    never a serialized plugin or an arbitrary CLI import path.
    """

    def bind(self, inputs: AuthenticatedBaselineInputsV5) -> None: ...
    def source_snapshot(self) -> PolicySourceSnapshotV5: ...
    def evaluate(self, episode, *, scenario_ids: tuple[str, ...]) -> bytes: ...
    def fingerprint(self) -> SemanticFingerprintV5: ...
    def close(self) -> bool: ...


def _ref(value) -> ArtifactRefV5:
    return _decode_dataclass(ArtifactRefV5, value)


def _graph(repository, roots, *, input_ref):
    """Walk exact authenticated JSON edges; typed parsing starts after this pass.

    The closed input record selects its owning panel plan. Only that owner's
    exact bundle/provenance/panel edges use a raw codec. No filename inference,
    sibling lookup, hidden-stage traversal, or directory scan occurs.
    """
    cached, paths, active = {}, {}, set()

    def read(ref):
        item = repository.authenticate_exact(ref)
        if item.reference != ref or hashlib.sha256(item.content).hexdigest() != ref.sha256:
            raise ValueError("baseline graph digest mismatch")
        return item

    input_item = read(input_ref)
    primitive = _strict_json_object(input_item.content, input_ref)
    if set(primitive) != {field.name for field in fields(BaselineCaptureInputsV5)}:
        raise ValueError("baseline input graph schema is invalid")
    if type(primitive["schema_version"]) is not int or primitive["schema_version"] != 5:
        raise ValueError("baseline input graph is not V5")
    plan_ref = _ref(primitive["panel_plan_ref"])
    plan_item = read(plan_ref)
    plan = _strict_json_object(plan_item.content, plan_ref)
    if set(plan) != {field.name for field in fields(CampaignPanelPlanV5)}:
        raise ValueError("baseline panel owner schema is invalid")
    if type(plan["discovery"]) is not list or len(plan["discovery"]) != 4:
        raise ValueError("baseline requires four discovery episodes")
    raw = {_ref(plan["pit_bundle_ref"]), _ref(plan["prices_provenance_ref"])}
    for episode in (plan["mechanics"], plan["quick"], *plan["discovery"]):
        raw.add(_ref(episode["panel_ref"]))
    if raw.intersection({input_ref, plan_ref}):
        raise ValueError("baseline graph raw and JSON roles overlap")
    if set(plan_item.child_references) != raw:
        raise ValueError("baseline panel owner has unexpected graph edges")
    # Transition contract identity uses the PIT canonical codec rather than
    # the V5 JSON codec. Its exact bytes are an explicitly owned raw input.
    raw.add(_ref(primitive["identity_transition_ref"]))

    def visit(ref):
        if type(ref) is not ArtifactRefV5:
            raise ValueError("baseline edge is not a V5 reference")
        if ref in active:
            raise ValueError("baseline graph contains a cycle")
        if paths.setdefault(ref.relative_path, ref.sha256) != ref.sha256:
            raise ValueError("baseline graph has conflicting path identities")
        if ref in cached:
            return
        active.add(ref)
        if ref in raw:
            item = repository.authenticate_raw_artifact(ref)
            if item.reference != ref:
                raise ValueError("baseline raw graph authentication failed")
            cached[ref] = None
        else:
            item = read(ref)
            cached[ref] = item.content
            for child in item.child_references:
                visit(child)
        active.remove(ref)

    for root in (*roots, input_ref):
        visit(root)
    return cached


def _load(cached, ref, cls):
    raw = cached[ref]
    value = _decode_dataclass(cls, _strict_json_object(raw, ref))
    if canonical_json_bytes_v5(value) != raw:
        raise ValueError("baseline typed artifact is noncanonical")
    return value


def _inputs(repository, reference, cached):
    inputs = _load(cached, reference, BaselineCaptureInputsV5)
    values = tuple(
        _load(cached, ref, cls)
        for ref, cls in (
            (inputs.evaluator_contract_ref, EvaluatorContractV5),
            (inputs.execution_profile_ref, ExecutionProfileV5),
            (inputs.sandbox_profile_ref, SandboxProfileV5),
            (inputs.panel_plan_ref, CampaignPanelPlanV5),
            (inputs.baseline_policy_revision_ref, PolicyRevisionIdentityV5),
            (inputs.source_bundle_ref, SourceBundleV5),
            (inputs.policy_scope_ref, PolicyScopeDescriptorV5),
            (inputs.resources_ref, ResourceCapabilitiesV5),
        )
    )
    result = AuthenticatedBaselineInputsV5(reference, inputs, *values)
    evaluator, execution, sandbox, plan, policy, source, scope, resources = values
    runtime_map = _strict_json_object(cached[inputs.evaluator_source_ref], inputs.evaluator_source_ref)
    if (
        inputs.execution_profile_ref.sha256 != evaluator.execution_profile_sha256
        or inputs.sandbox_profile_ref.sha256 != evaluator.sandbox_profile_sha256
        or inputs.baseline_policy_revision_ref.sha256 != evaluator.baseline_policy_revision_sha256
        or inputs.source_bundle_ref.sha256 != evaluator.baseline_source_bundle_sha256
        or inputs.evaluator_source_ref.sha256 != evaluator.evaluator_source_sha256
        or evaluator_source_sha256(runtime_map) != evaluator.evaluator_source_sha256
        or sandbox.runtime_source_sha256 != evaluator.evaluator_source_sha256
        or inputs.identity_transition_ref.sha256 != evaluator.identity_transition_contract_sha256
        or plan.pit_bundle_ref.sha256 != evaluator.pit_bundle_sha256
        or plan.prices_provenance_ref.sha256 != evaluator.prices_provenance_sha256
        or scope.baseline_policy_revision_ref != inputs.baseline_policy_revision_ref
        or scope.editable_source_sha256 != policy.editable_source_sha256
        or tuple((file.path, file.sha256) for file in source.files) != policy.editable_source_sha256
        or scope.full_source_escape_allowed is not False
    ):
        raise ValueError("baseline input identities differ")
    validate_sandbox_profile_resources_v5(sandbox, resources)
    for episode in (plan.mechanics, plan.quick, *plan.discovery):
        panel = repository.load_evaluation_panel_spec_exact(episode.panel_ref)
        validate_episode_plan_panel_v5(episode, panel)
        if panel.purpose != episode.purpose:
            raise ValueError("baseline panel purpose differs from owner")
    return result


def authenticate_baseline_inputs_v5(*, repository, inputs_ref):
    cached = _graph(repository, (inputs_ref,), input_ref=inputs_ref)
    return _inputs(repository, inputs_ref, cached)


def _validate_report(inputs, episode, evaluation):
    if type(evaluation) is not PanelEvaluationV5 or (
        evaluation.evaluator_contract_sha256 != inputs.evaluator.sha256
        or evaluation.sandbox_profile_sha256 != inputs.sandbox.sha256
        or evaluation.panel_sha256 != episode.panel_ref.sha256
        or evaluation.policy_identity_sha256 != inputs.policy.sha256
        or evaluation.start_date != episode.start_date
        or evaluation.end_date != episode.end_date
        or evaluation.selection_scenario_id != inputs.evaluator.selection_scenario_id
        or tuple(item.scenario_id for item in evaluation.scenarios)
        != tuple(item.scenario_id for item in inputs.evaluator.friction_grid)
    ):
        raise ValueError("baseline evidence differs from full-scenario panel authority")


def _campaign(inputs, evaluations):
    episodes = tuple(
        EpisodeEvaluationV5(plan.episode_id, plan.episode_ordinal, plan.start_date, plan.end_date, evaluation)
        for plan, evaluation in zip(inputs.plan.discovery, evaluations, strict=True)
    )
    return CampaignEvidenceV5(
        inputs.plan.discovery_plan_sha256,
        episodes,
        campaign_cagr_pct(episodes=episodes, discovery_plan=inputs.plan, evaluator_contract=inputs.evaluator),
        sum(selected_scenario(item).report.closed_trades for item in evaluations),
    )


def verify_baseline_v5(*, repository, authority_ref: ArtifactRefV5) -> dict[str, object]:
    """Authenticate the complete baseline graph and return content-free identities."""
    item = repository.authenticate_exact(authority_ref)
    if item.reference != authority_ref or hashlib.sha256(item.content).hexdigest() != authority_ref.sha256:
        raise ValueError("baseline root identity differs")
    primitive = _strict_json_object(item.content, authority_ref)
    if set(primitive) != {field.name for field in fields(BaselineAuthorityV5)}:
        raise ValueError("legacy or partial baseline authority is not V5")
    inputs_ref = _ref(primitive["capture_inputs_ref"])
    cached = _graph(repository, (authority_ref,), input_ref=inputs_ref)
    authority = _load(cached, authority_ref, BaselineAuthorityV5)
    inputs = _inputs(repository, inputs_ref, cached)
    for name in (
        "evaluator_contract_ref",
        "execution_profile_ref",
        "sandbox_profile_ref",
        "panel_plan_ref",
        "baseline_policy_revision_ref",
    ):
        if getattr(authority, name) != getattr(inputs.inputs, name):
            raise ValueError("baseline authority differs from capture inputs")
    repeat = _load(cached, authority.deterministic_repeat_ref, BaselineDeterministicRepeatV5)
    first = _load(cached, repeat.first_run_ref, BaselineRunV5)
    second = _load(cached, repeat.second_run_ref, BaselineRunV5)
    for run in (first, second):
        if run.capture_inputs_ref != inputs_ref:
            raise ValueError("baseline repeat changed input identities")
    for name in (
        "mechanics_evidence_ref",
        "quick_evidence_ref",
        "discovery_evidence_ref",
        "semantic_fingerprint_ref",
    ):
        left, right = getattr(first, name), getattr(second, name)
        if left.relative_path == right.relative_path or cached[left] != cached[right]:
            raise ValueError("baseline repeat reports are not distinct byte-identical outputs")
        if name != "semantic_fingerprint_ref" and getattr(authority, name) != left:
            raise ValueError("baseline authority evidence differs from repeat")
    mechanics = _load(cached, first.mechanics_evidence_ref, PanelEvaluationV5)
    quick = _load(cached, first.quick_evidence_ref, PanelEvaluationV5)
    campaign = _load(cached, first.discovery_evidence_ref, CampaignEvidenceV5)
    fingerprint = _load(cached, first.semantic_fingerprint_ref, SemanticFingerprintV5)
    _validate_report(inputs, inputs.plan.mechanics, mechanics)
    _validate_report(inputs, inputs.plan.quick, quick)
    for plan, evidence in zip(inputs.plan.discovery, campaign.episodes, strict=True):
        _validate_report(inputs, plan, evidence.evaluation)
    if campaign != _campaign(inputs, tuple(item.evaluation for item in campaign.episodes)):
        raise ValueError("baseline campaign differs from recomputed discovery evidence")
    parent = _load(cached, authority.parent_authority_ref, BaselineParentAuthorityV5)
    expected = BaselineParentAuthorityV5(
        inputs.policy,
        inputs.inputs.baseline_policy_revision_ref,
        fingerprint,
        campaign,
        inputs.source,
        inputs.inputs.source_bundle_ref,
    )
    if parent != expected:
        raise ValueError("baseline parent projection differs from full authority")
    baseline_parent_candidate_v5(authority=parent, discovery_plan=inputs.plan, evaluator_contract=inputs.evaluator)
    return {
        "schema_version": 5,
        "status": "verified",
        "authority_ref": authority_ref,
        "parent_authority_ref": authority.parent_authority_ref,
        "authenticated_artifacts": len(cached),
        "panels": 6,
        "scenarios": 3,
        "byte_identical": True,
        "source_unchanged": True,
        "cleanup_complete": True,
        "provider_calls": 0,
        "evaluation_performed": False,
    }


def capture_baseline_v5(*, repository, inputs_ref, output_path, worker_factory=None):
    """Capture two unchanged full-grid baseline runs, publishing authority last.

    All outputs are preflighted before allocation. Failure leaves no authority;
    exclusive persistence never overwrites a partial capture or an earlier run.
    """
    output = ArtifactRefV5(output_path, "0" * 64)
    parent_path = str(Path(output.relative_path).with_suffix("")).replace("\\", "/")
    paths = {
        (run, kind): f"{parent_path}/run-{run}-{kind}.json"
        for run in (1, 2)
        for kind in ("mechanics", "quick", "discovery", "fingerprint", "run")
    }
    repeat_path, projection_path = f"{parent_path}/repeat.json", f"{parent_path}/parent.json"
    for path in (output_path, repeat_path, projection_path, *paths.values()):
        repository.require_baseline_output_absent(path)
    inputs = authenticate_baseline_inputs_v5(repository=repository, inputs_ref=inputs_ref)
    if worker_factory is None:
        raise BaselineRuntimeUnavailableV5("capture requires an explicit sandbox factory")
    expected_snapshot = PolicySourceSnapshotV5(inputs.scope.source_commit, inputs.policy.editable_source_sha256)
    reports, fingerprints, workers = [], [], []
    for _ in (1, 2):
        worker = worker_factory(inputs)
        if any(worker is previous for previous in workers):
            raise ValueError("baseline repeat reused a worker")
        workers.append(worker)
        try:
            worker.bind(inputs)
            if worker.source_snapshot() != expected_snapshot:
                raise ValueError("baseline clean source differs before evaluation")
            current = []
            for episode in (inputs.plan.mechanics, inputs.plan.quick, *inputs.plan.discovery):
                raw = worker.evaluate(
                    episode, scenario_ids=tuple(s.scenario_id for s in inputs.evaluator.friction_grid)
                )
                if type(raw) is not bytes or not raw or len(raw) > inputs.sandbox.output_limit_bytes:
                    raise ValueError("baseline report is absent or exceeds its bound")
                evaluation = _decode_dataclass(PanelEvaluationV5, _strict_json_object(raw, None))
                _validate_report(inputs, episode, evaluation)
                if canonical_json_bytes_v5(evaluation) != raw:
                    raise ValueError("baseline report is noncanonical")
                current.append((raw, evaluation))
            fingerprint = worker.fingerprint()
            if type(fingerprint) is not SemanticFingerprintV5:
                raise ValueError("baseline mechanics fingerprint is absent")
            if worker.source_snapshot() != expected_snapshot:
                raise ValueError("baseline source changed during evaluation")
            reports.append(tuple(current))
            fingerprints.append(fingerprint)
        finally:
            if worker.close() is not True:
                raise ValueError("baseline cleanup did not complete")
        if worker.source_snapshot() != expected_snapshot:
            raise ValueError("baseline source changed during cleanup")
    if tuple(raw for raw, _ in reports[0]) != tuple(raw for raw, _ in reports[1]) or canonical_json_bytes_v5(
        fingerprints[0]
    ) != canonical_json_bytes_v5(fingerprints[1]):
        raise ValueError("baseline deterministic repeat differs byte for byte")
    # Reauthenticate complete input ancestry after evaluation, before any output.
    if authenticate_baseline_inputs_v5(repository=repository, inputs_ref=inputs_ref) != inputs:
        raise ValueError("baseline input authority changed during capture")
    runs = []
    for index, evaluations in enumerate(reports, start=1):
        values = (
            ("mechanics", evaluations[0][1]),
            ("quick", evaluations[1][1]),
            ("discovery", _campaign(inputs, tuple(value for _, value in evaluations[2:]))),
            ("fingerprint", fingerprints[index - 1]),
        )
        refs = tuple(repository.create_baseline_artifact(paths[index, name], value) for name, value in values)
        run = BaselineRunV5(5, inputs_ref, *refs, True, True)
        runs.append((repository.create_baseline_artifact(paths[index, "run"], run), run))
    repeat = BaselineDeterministicRepeatV5(5, runs[0][0], runs[1][0], True, True)
    repeat_ref = repository.create_baseline_artifact(repeat_path, repeat)
    parent = BaselineParentAuthorityV5(
        inputs.policy,
        inputs.inputs.baseline_policy_revision_ref,
        fingerprints[0],
        _campaign(inputs, tuple(value for _, value in reports[0][2:])),
        inputs.source,
        inputs.inputs.source_bundle_ref,
    )
    parent_ref = repository.create_baseline_artifact(projection_path, parent)
    first = runs[0][1]
    authority = BaselineAuthorityV5(
        5,
        inputs.inputs.evaluator_contract_ref,
        inputs.inputs.execution_profile_ref,
        inputs.inputs.sandbox_profile_ref,
        inputs.inputs.panel_plan_ref,
        inputs.inputs.baseline_policy_revision_ref,
        first.mechanics_evidence_ref,
        first.quick_evidence_ref,
        first.discovery_evidence_ref,
        repeat_ref,
        inputs_ref,
        parent_ref,
    )
    reference = ArtifactRefV5(output_path, authority.sha256)
    raw = canonical_json_bytes_v5(authority)

    class PendingAuthorityRepository:
        def authenticate_exact(self, ref):
            if ref == reference:
                return AuthenticatedArtifactV5(ref, raw, _extract_artifact_refs(_strict_json_object(raw, ref)))
            return repository.authenticate_exact(ref)

        def __getattr__(self, name):
            return getattr(repository, name)

    verify_baseline_v5(repository=PendingAuthorityRepository(), authority_ref=reference)
    if workers[-1].source_snapshot() != expected_snapshot:
        raise ValueError("baseline source changed before authority publication")
    created = repository.create_baseline_artifact(output_path, authority)
    if created != reference:
        raise ValueError("baseline output identity differs")
    return reference


def write_execution_profile_v5(*, repository, output_path):
    return repository.create_baseline_artifact(
        output_path,
        ExecutionProfileV5(
            5,
            "next_open",
            "open_then_stop",
            "last_session_close",
            "half_spread_plus_market_impact_plus_commission_bps",
        ),
    )


def build_sandbox_profile_v5(*, repository, resources_ref, evaluator_source_ref, image_name, image_digest, output_path):
    """Compose a digest-pinned contract only; never build, inspect, pull or run."""
    source = repository.authenticate_exact(evaluator_source_ref)
    resources_item = repository.authenticate_exact(resources_ref)
    if source.child_references or resources_item.child_references:
        raise ValueError("sandbox profile requires leaf source and resource authority")
    source_map = _strict_json_object(source.content, evaluator_source_ref)
    identity = evaluator_source_sha256(source_map)
    if identity != evaluator_source_ref.sha256:
        raise ValueError("sandbox runtime source identity differs")
    resources = _load({resources_ref: resources_item.content}, resources_ref, ResourceCapabilitiesV5)
    profile = sandbox_profile_from_manifest_v5(
        image_name=image_name,
        image_digest=image_digest,
        runtime_source_sha256=identity,
        resources=resources,
    )
    return repository.create_baseline_artifact(output_path, profile)


@dataclass(frozen=True, slots=True)
class BaselineHostAuthorityV5:
    """Explicit operator tool/path authority; artifacts cannot choose executables."""

    source_root: str
    scratch_root: str
    git_executable: str
    git_sha256: str
    docker_executable: str
    docker_sha256: str

    def __post_init__(self):
        for path in (self.source_root, self.scratch_root, self.git_executable, self.docker_executable):
            if (
                type(path) is not str
                or not ntpath.isabs(path)
                or ntpath.normpath(path) != path
                or "/" in path
                or "," in path
                or "\x00" in path
                or re.fullmatch(r"[A-Za-z]:", ntpath.splitdrive(path)[0]) is None
            ):
                raise ValueError("baseline host paths must be explicit canonical Windows paths")
        for digest in (self.git_sha256, self.docker_sha256):
            ArtifactRefV5("identity", digest)
        roots = (ntpath.normcase(self.source_root), ntpath.normcase(self.scratch_root))
        try:
            common = ntpath.commonpath(roots)
        except ValueError:
            common = None
        if common in roots:
            raise ValueError("baseline scratch and source roots must not overlap")


class BaselineSandboxTransportV5(Protocol):
    """Bounded sandbox I/O seam used by production and direct synthetic fakes."""

    def source_snapshot(self) -> PolicySourceSnapshotV5: ...
    def panel(self, request) -> bytes: ...
    def probe(self, *, request_sha256: str) -> bytes: ...
    def close(self) -> bool: ...


class BaselineSandboxWorkerV5:
    """Compose exact baseline requests independently of campaign/search authority."""

    def __init__(self, *, repository, inputs, transport: BaselineSandboxTransportV5, ordinal: int):
        if type(ordinal) is not int or ordinal not in (1, 2):
            raise ValueError("baseline worker ordinal is invalid")
        self.repository, self.inputs, self.transport, self.ordinal = repository, inputs, transport, ordinal
        self.evaluated = set()

    def bind(self, inputs):
        if inputs is not self.inputs:
            raise ValueError("baseline sandbox worker inputs changed")

    def source_snapshot(self):
        return self.transport.source_snapshot()

    def evaluate(self, episode, *, scenario_ids):
        from .container_protocol import PanelExecutionRequestV5, decode_panel_execution_output_v5

        expected = (self.inputs.plan.mechanics, self.inputs.plan.quick, *self.inputs.plan.discovery)
        if episode not in expected or episode.episode_id in self.evaluated:
            raise ValueError("baseline sandbox panel is outside its one-use scope")
        self.evaluated.add(episode.episode_id)
        request = PanelExecutionRequestV5(
            schema_version=5,
            request_sha256=canonical_sha256_v5((self.inputs.reference, self.ordinal, episode)),
            evaluator_contract=self.inputs.evaluator,
            sandbox_profile=self.inputs.sandbox,
            execution_profile=self.inputs.execution,
            policy_revision=self.inputs.policy,
            episode=episode,
            panel=self.repository.load_evaluation_panel_spec_exact(episode.panel_ref),
            scenario_ids=scenario_ids,
            policy_method_timeout_seconds=self.inputs.resources.policy_method_timeout_seconds,
            worker_startup_timeout_seconds=self.inputs.resources.worker_startup_timeout_seconds,
            output_limit_bytes=self.inputs.sandbox.output_limit_bytes,
            baseline_capture_inputs_ref=self.inputs.reference,
        )
        raw = self.transport.panel(request)
        if type(raw) is not bytes or len(raw) > self.inputs.sandbox.output_limit_bytes:
            raise ValueError("baseline sandbox output exceeds its bound")
        output = decode_panel_execution_output_v5(raw)
        if output.input_sha256 != request.sha256 or output.request_sha256 != request.request_sha256:
            raise ValueError("baseline sandbox output changed request identity")
        _validate_report(self.inputs, episode, output.evaluation)
        return canonical_json_bytes_v5(output.evaluation)

    def fingerprint(self):
        request = canonical_sha256_v5((self.inputs.reference, self.ordinal, "semantic-probe"))
        raw = self.transport.probe(request_sha256=request)
        if type(raw) is not bytes or len(raw) > self.inputs.sandbox.output_limit_bytes:
            raise ValueError("baseline probe output exceeds its bound")
        primitive = _strict_json_object(raw, None)
        if set(primitive) != {"fingerprint", "policy_revision_sha256", "request_sha256", "suite_id"}:
            raise ValueError("baseline probe output schema is invalid")
        fingerprint = _decode_dataclass(SemanticFingerprintV5, primitive["fingerprint"])
        if (
            primitive["request_sha256"] != request
            or primitive["policy_revision_sha256"] != self.inputs.policy.sha256
            or primitive["suite_id"] != fingerprint.suite_id
        ):
            raise ValueError("baseline probe output identity differs")
        return fingerprint

    def close(self):
        return self.transport.close()


def baseline_container_argv_v5(
    *, inputs, policy_root, input_root, output_root, bundle_path, provenance_path, module_args, container_name
):
    """Pure pre-manifest argv; image pull, networking and host execution are absent."""
    from .policy_scope import EDITABLE_POLICY_PATHS_V5

    if not re.fullmatch(r"pit-v5-baseline-[0-9a-f]{32}", container_name):
        raise ValueError("baseline container ownership name is invalid")
    sandbox = inputs.sandbox
    argv = [
        "create",
        "--name",
        container_name,
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(sandbox.pid_limit),
        "--cpus",
        str(sandbox.cpu_limit),
        "--memory",
        f"{sandbox.memory_limit_mib}m",
        "--memory-swap",
        f"{sandbox.memory_limit_mib}m",
        "--log-driver",
        "none",
        "--entrypoint",
        "python",
        "--workdir",
        "/",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
    ]
    mounts = [
        (ntpath.join(policy_root, path.rsplit("/", 1)[-1]), "/pit/candidate/" + path.rsplit("/", 1)[-1], True)
        for path in EDITABLE_POLICY_PATHS_V5
    ]
    mounts.extend(((input_root, "/pit/request", True), (output_root, "/pit/output", False)))
    if bundle_path is not None:
        mounts.extend(
            (
                (bundle_path, "/pit/data/pit_bundle.sqlite3", True),
                (provenance_path, "/pit/data/prices_provenance.json", True),
            )
        )
    for host, target, read_only in mounts:
        if type(host) is not str or "," in host or "\x00" in host or not ntpath.isabs(host):
            raise ValueError("baseline mount path is invalid")
        argv.extend(("--mount", f"type=bind,src={host},dst={target}" + (",readonly" if read_only else "")))
    argv.extend(("--", sandbox.image_reference, "-P", "-B", "-m", *module_args))
    return tuple(argv)


def _pin_baseline_policy_overlay_v5(*, inputs, directory, stack):
    """Materialize authenticated LF source into an owned, deny-write-pinned overlay.

    The caller owns the directory and closes these pins only after container
    cleanup, then removes that exact directory even if materialization fails.
    """
    from .production_fs import write_new_regular_in_directory_v5
    from .readiness import _pin_inspection_file

    if (
        type(inputs.source) is not SourceBundleV5
        or tuple((file.path, file.sha256) for file in inputs.source.files) != inputs.policy.editable_source_sha256
    ):
        raise ValueError("baseline overlay source identity differs")
    pins = []
    for file in inputs.source.files:
        raw = file.source.encode("utf-8")
        name = file.path.rsplit("/", 1)[-1]
        write_new_regular_in_directory_v5(directory, name, raw)
        pin, mounted = _pin_inspection_file(stack, directory, name)
        if mounted != raw or pin.sha256 != file.sha256:
            raise ValueError("baseline overlay bytes differ from authenticated source")
        pins.append(pin)
    return tuple(pins)


class LocalBaselineCaptureFactoryV5:
    """Windows owner: pins source/data/tools through final authority publication.

    Fresh containers execute only image-owned V5 panel/probe entrypoints. No
    campaign manifest, provider, Git materialization or image build is involved.
    """

    def __init__(self, *, repository, host: BaselineHostAuthorityV5):
        if type(host) is not BaselineHostAuthorityV5:
            raise ValueError("baseline capture host authority is absent")
        self.repository, self.host = repository, host
        self.stack = ExitStack()
        self.inputs = None
        self.ordinal = 0
        self.active = False

    def __enter__(self):
        self.stack.__enter__()
        self.active = True
        return self

    def __exit__(self, *exc):
        self.active = False
        return self.stack.__exit__(*exc)

    def _bind(self, inputs):
        from .production_fs import acquire_absolute_directory_v5, open_regular_in_directory_v5
        from .readiness import ReadinessGitAuthorityV5, _open_trusted_git, capture_readiness_source_v5

        if self.inputs is not None:
            if self.inputs != inputs:
                raise ValueError("baseline production factory changed inputs")
            return
        source_root, scratch_root = Path(self.host.source_root), Path(self.host.scratch_root)
        if scratch_root.is_relative_to(self.repository.root) or self.repository.root.is_relative_to(scratch_root):
            raise ValueError("baseline scratch and artifact roots overlap")
        self.source_guard = self.stack.enter_context(
            capture_readiness_source_v5(
                source_root=source_root,
                expected_source_commit=inputs.scope.source_commit,
                git_authority=ReadinessGitAuthorityV5(self.host.git_executable, self.host.git_sha256),
            )
        )
        # The shared helper pins an exact protected Windows executable. Reuse
        # its existing authority wrapper for the explicit Docker CLI as well.
        self.docker_guard = self.stack.enter_context(
            _open_trusted_git(
                ReadinessGitAuthorityV5(self.host.docker_executable, self.host.docker_sha256),
                forbidden_roots=(source_root, scratch_root, self.repository.root),
            )
        )
        self.scratch = self.stack.enter_context(acquire_absolute_directory_v5(scratch_root))
        self.data_paths = []
        for ref in (inputs.plan.pit_bundle_ref, inputs.plan.prices_provenance_ref):
            path = self.repository.root.joinpath(*ref.relative_path.split("/"))
            parent = self.stack.enter_context(acquire_absolute_directory_v5(path.parent))
            stream, _ = open_regular_in_directory_v5(parent, path.name, writable=False)
            self.stack.enter_context(stream)
            if hashlib.file_digest(stream, "sha256").hexdigest() != ref.sha256:
                raise ValueError("baseline pinned data identity differs")
            self.data_paths.append(str(path))
        self.inputs = inputs
        if self.source_snapshot() != PolicySourceSnapshotV5(
            inputs.scope.source_commit, inputs.policy.editable_source_sha256
        ):
            raise ValueError("baseline production source differs")

    def source_snapshot(self):
        self.source_guard.revalidate()
        self.docker_guard.revalidate()
        self.scratch.assert_current()
        return self.source_guard.snapshot

    def __call__(self, inputs):
        if not self.active:
            raise ValueError("baseline production factory requires its held context")
        self._bind(inputs)
        self.ordinal += 1
        return BaselineSandboxWorkerV5(
            repository=self.repository,
            inputs=inputs,
            ordinal=self.ordinal,
            transport=_LocalBaselineTransportV5(self, inputs),
        )


class _LocalBaselineTransportV5:
    def __init__(self, factory, inputs):
        self.factory, self.inputs = factory, inputs
        self.clean = True

    def source_snapshot(self):
        return self.factory.source_snapshot()

    def _invoke(self, args, *, config_root, timeout, capture=False):
        self.factory.docker_guard.revalidate()
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        result = subprocess.run(
            (
                self.factory.host.docker_executable,
                "--host",
                "npipe:////./pipe/docker_engine",
                "--config",
                str(config_root),
                *args,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
            cwd=config_root,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.factory.docker_guard.revalidate()
        if result.returncode != 0:
            raise ValueError("baseline sandbox command failed")
        return result.stdout if capture else b""

    def _run(self, *, module_args, input_bytes, output_name, timeout, with_data):
        from .production_fs import (
            create_directory_in_directory_v5,
            remove_owned_tree_in_directory_v5,
            read_regular_in_directory_v5,
            write_new_regular_in_directory_v5,
        )

        self.source_snapshot()
        parent = self.factory.scratch
        name = "pit-v5-baseline-" + uuid.uuid4().hex
        root = create_directory_in_directory_v5(parent, name)
        identity, container_id, reserved = root.identity, None, False
        children = []
        policy_stack = ExitStack()
        try:
            for child in ("input", "output", "config", "policy"):
                children.append(create_directory_in_directory_v5(root, child))
            input_dir, output_dir, config_dir, policy_dir = children
            policy_pins = _pin_baseline_policy_overlay_v5(inputs=self.inputs, directory=policy_dir, stack=policy_stack)
            if input_bytes is not None:
                write_new_regular_in_directory_v5(input_dir, "panel-request.json", input_bytes)
            args = baseline_container_argv_v5(
                inputs=self.inputs,
                policy_root=str(policy_dir.path),
                input_root=str(input_dir.path),
                output_root=str(output_dir.path),
                bundle_path=self.factory.data_paths[0] if with_data else None,
                provenance_path=self.factory.data_paths[1] if with_data else None,
                module_args=module_args,
                container_name=name,
            )
            for pin in policy_pins:
                pin.revalidate()
            # Reserve before invocation: ambiguous create completion also gets
            # exact owner-name cleanup, never enumeration of other containers.
            reserved = True
            raw_id = self._invoke(
                args,
                config_root=config_dir.path,
                timeout=self.inputs.resources.worker_startup_timeout_seconds,
                capture=True,
            )
            candidate_id = raw_id.decode("ascii").strip()
            if not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
                raise ValueError("baseline container identity is invalid")
            container_id = candidate_id
            self._invoke(("start", "--attach", container_id), config_root=config_dir.path, timeout=timeout)
            raw, _ = read_regular_in_directory_v5(
                output_dir, output_name, maximum_bytes=self.inputs.sandbox.output_limit_bytes
            )
            self.source_snapshot()
            for pin in policy_pins:
                pin.revalidate()
            return raw
        finally:
            try:
                if reserved:
                    self._invoke(
                        ("rm", "--force", container_id or name),
                        config_root=children[2].path,
                        timeout=self.inputs.resources.cleanup_timeout_seconds,
                    )
            except BaseException:
                self.clean = False
            # Release all overlay pins before deleting the owned invocation.
            # A failed close must not skip later cleanup or bless success.
            for resource in (policy_stack, *reversed(children), root):
                try:
                    resource.close()
                except BaseException:
                    self.clean = False
            try:
                if not remove_owned_tree_in_directory_v5(parent, name, expected_identity=identity):
                    self.clean = False
            except BaseException:
                self.clean = False
            if not self.clean:
                raise ValueError("baseline owned sandbox cleanup is incomplete")

    def panel(self, request):
        from .container_protocol import panel_execution_request_bytes_v5

        args = [
            "core.pit_optimizer_v5.container_entry",
            "--request-sha256",
            request.request_sha256,
            "--input-sha256",
            request.sha256,
            "--evaluator-sha256",
            request.evaluator_contract.sha256,
            "--sandbox-sha256",
            request.sandbox_profile.sha256,
            "--policy-sha256",
            request.policy_revision.sha256,
            "--panel-sha256",
            request.panel.sha256,
            "--start-date",
            request.panel.start_date,
            "--end-date",
            request.panel.end_date,
            "--output-limit-bytes",
            str(request.output_limit_bytes),
        ]
        for scenario in request.scenario_ids:
            args.extend(("--scenario", scenario))
        return self._run(
            module_args=tuple(args),
            input_bytes=panel_execution_request_bytes_v5(request),
            output_name="panel-evaluation.json",
            with_data=True,
            timeout=self.inputs.resources.discovery_episode_timeout_seconds,
        )

    def probe(self, *, request_sha256):
        from .probes import PROBE_SUITE_ID_V5
        from .sandbox import derive_trusted_probe_runtime_authority_v5

        inputs = self.inputs
        args = (
            "core.pit_optimizer_v5.probe_entry",
            "--request-sha256",
            request_sha256,
            "--policy-sha256",
            inputs.policy.sha256,
            "--trusted-runtime-sha256",
            inputs.policy.trusted_policy_runtime_sha256,
            "--immutable-constraints-sha256",
            inputs.policy.immutable_constraints_sha256,
            "--suite-id",
            PROBE_SUITE_ID_V5,
            "--evaluator-contract-sha256",
            inputs.evaluator.sha256,
            "--sandbox-profile-sha256",
            inputs.sandbox.sha256,
            "--probe-runtime-sha256",
            inputs.sandbox.runtime_source_sha256,
            "--probe-runtime-authority-sha256",
            derive_trusted_probe_runtime_authority_v5(
                evaluator_contract=inputs.evaluator, sandbox_profile=inputs.sandbox
            ),
            "--call-timeout-seconds",
            str(inputs.resources.policy_method_timeout_seconds),
            "--output-limit-bytes",
            str(inputs.sandbox.output_limit_bytes),
        )
        return self._run(
            module_args=args,
            input_bytes=None,
            output_name="semantic-fingerprint.json",
            with_data=False,
            timeout=inputs.resources.mechanics_timeout_seconds,
        )

    def close(self):
        return self.clean
