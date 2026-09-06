"""Read-only retirement authentication and a non-executable replay projection.

No stage runner, recovery routine, panel-spec loader, materializer, or launch
adapter is called here. Panel specs are authenticated as exact opaque leaves
selected by their already-retired owners; source bundles are data, never code.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from types import SimpleNamespace
from typing import Literal

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_v5 import confirmation, qualification
from core.pit_optimizer_v5.artifacts import ArtifactExistsV5
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5, SourceBundleV5
from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    ConfirmationAttemptCommitmentV5,
    ConfirmationOutcomeV5,
    ConfirmationPanelPlanV5,
    EpisodePlanV5,
    EvaluatorContractV5,
    FinalizedDiscoveryCampaignV5,
    QualificationAttemptCommitmentV5,
    QualificationOutcomeV5,
    QualificationPanelPlanV5,
    SandboxProfileV5,
    ScenarioGridV5,
    canonical_sha256_v5,
    validate_campaign_manifest_bindings_v5,
)
from core.pit_optimizer_v5.manifest import (
    PolicyScopeDescriptorV5,
    PolicySourceSnapshotV5,
    _sanitized_git_environment_v5,
    _validate_precomposition_bindings_v5,
)
from core.pit_optimizer_v5.panels import StageRetirementSnapshotV5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.production_fs import (
    acquire_absolute_directory_v5,
    acquire_directory_v5,
    open_regular_in_directory_v5,
    read_regular_in_directory_v5,
)
from core.pit_optimizer_v5.search import BaselineParentAuthorityV5


ReadinessBlockerV5 = Literal[
    "invalid_request",
    "qualification_unsuccessful",
    "retirement_invalid",
    "artifact_graph_invalid",
    "identity_invalid",
    "git_authority_invalid",
    "source_not_clean_or_matching",
    "output_exists",
    "output_unavailable",
]
_BLOCKERS = frozenset(ReadinessBlockerV5.__args__)
_load = confirmation._load


@dataclass(frozen=True, slots=True)
class ReadinessGitAuthorityV5:
    """Operator-supplied inspection tool identity, never sourced from artifacts."""

    executable: str
    sha256: str

    def __post_init__(self):
        ArtifactRefV5("identity", self.sha256)
        if type(self.executable) is not str or not Path(self.executable).is_absolute():
            raise ValueError("readiness requires an explicit trusted executable")


def _open_trusted_git(authority):
    """Authenticate before any process; keep executable and parent chain pinned."""
    from contextlib import ExitStack

    if type(authority) is not ReadinessGitAuthorityV5:
        raise ValueError("trusted Git authority is required")
    executable = Path(authority.executable)
    if os.path.normcase(str(executable.resolve(strict=True))) != os.path.normcase(str(executable)):
        raise ValueError("trusted Git path must be canonical")
    stack = ExitStack()
    try:
        parent = stack.enter_context(acquire_absolute_directory_v5(executable.parent))
        stream, _ = open_regular_in_directory_v5(parent, executable.name, writable=False)
        stack.enter_context(stream)
        if hashlib.file_digest(stream, "sha256").hexdigest() != authority.sha256:
            raise ValueError("trusted Git bytes differ")
        return stack
    except BaseException:
        stack.close()
        raise


def capture_readiness_source_v5(
    *, source_root: Path, expected_source_commit: str, git_authority: ReadinessGitAuthorityV5
):
    """Inspect Git using builtins that never apply filters, hooks or textconv.

    In particular, status/diff can run repository-configured clean filters, so
    compare tree/index identities and hash working files ourselves. Transformed
    checkouts, links and submodules fail closed. Normal text CRLF conversion is
    allowed only when the read-only EOL/attribute metadata permits it.
    """
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_commit) is None:
        raise ValueError("invalid source commit")
    root = Path(source_root)
    if not root.is_absolute() or os.path.normcase(str(root.resolve(strict=True))) != os.path.normcase(str(root)):
        raise ValueError("source root must be canonical")
    environment = _sanitized_git_environment_v5()
    environment.update({"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "", "PAGER": ""})
    fixed = (
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.preloadIndex=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
    )

    def invoke(*arguments, missing_is_empty=False):
        # All call sites below are literal builtin read operations. Neither a
        # command nor an argument list can enter through an artifact or CLI.
        result = subprocess.run(
            (git_authority.executable, *fixed, "-C", str(root), *arguments),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            shell=False,
            cwd=root,
            env=environment,
        )
        if result.returncode not in ((0, 1) if missing_is_empty else (0,)):
            raise ValueError("source metadata command failed")
        if result.returncode == 1:
            if result.stdout:
                raise ValueError("missing source metadata is not empty")
            return b""
        if len(result.stdout) > 32 * 1024 * 1024:
            raise ValueError("source metadata exceeds bound")
        return result.stdout

    def entries(raw, *, index):
        result = {}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, middle, last = header.decode("ascii").split(" ")
            oid = middle if index else last
            path = path.decode("utf-8", "strict")
            parts = PurePosixPath(path).parts
            if (
                mode not in {"100644", "100755"}
                or (last != "0" if index else middle != "blob")
                or re.fullmatch(r"[0-9a-f]{40}", oid) is None
                or not parts
                or PurePosixPath(path).as_posix() != path
                or PurePosixPath(path).is_absolute()
                or any(part in {".", ".."} or part.endswith((".", " ")) for part in parts)
                or any(char in path for char in ("\\", ":", "\x00"))
                or path in result
            ):
                raise ValueError("tracked source entry is invalid")
            result[path] = (mode, oid)
        if len({path.casefold() for path in result}) != len(result):
            raise ValueError("tracked source aliases collide")
        return result

    with _open_trusted_git(git_authority), acquire_absolute_directory_v5(root) as source_directory:
        version = invoke("--version").decode("ascii").strip()
        parsed_version = re.fullmatch(r"git version (\d+)\.(\d+)(?:\.[0-9A-Za-z]+)*", version)
        if parsed_version is None or tuple(map(int, parsed_version.groups())) < (2, 50):
            raise ValueError("trusted Git must support the no-lazy-fetch inspection boundary")
        # Config inspection is itself a builtin read. Refuse partial-clone
        # repositories in addition to disabling lazy fetching on every call.
        config = invoke(
            "config",
            "--null",
            "--get-regexp",
            r"^(core\.autocrlf|extensions\.partialclone|remote\..*\.promisor)$",
            missing_is_empty=True,
        )
        autocrlf = "false"
        for item in config.split(b"\0"):
            key, _, value = item.partition(b"\n")
            key = key.lower()
            if key == b"extensions.partialclone" or (key.startswith(b"remote.") and key.endswith(b".promisor")):
                raise ValueError("source inspection requires a complete local object store")
            if key == b"core.autocrlf":
                autocrlf = value.decode("ascii").lower()
        top = Path(invoke("rev-parse", "--path-format=absolute", "--show-toplevel").decode("utf-8").strip())
        if top != root or invoke("rev-parse", "--is-inside-work-tree").strip() != b"true":
            raise ValueError("source repository differs")
        if invoke("rev-parse", "--verify", "HEAD").strip() != expected_source_commit.encode("ascii"):
            raise ValueError("source HEAD differs")
        tree = entries(invoke("ls-tree", "-rz", "--full-tree", expected_source_commit), index=False)
        index_raw = invoke("ls-files", "--stage", "--full-name", "-z")
        if tree != entries(index_raw, index=True):
            raise ValueError("source index differs")
        if invoke("ls-files", "--others", "--exclude-standard", "--full-name", "-z"):
            raise ValueError("source has untracked files")
        eol = {}
        for entry in invoke("ls-files", "--eol", "--full-name", "-z").split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                eol[path.decode("utf-8")] = metadata.decode("ascii").split()
        if set(eol) != set(tree):
            raise ValueError("source EOL metadata differs")
        sources = {}
        for path, (_, oid) in tree.items():
            parts = PurePosixPath(path).parts
            with acquire_directory_v5(
                root, parts[:-1], create=False, expected_root_identity=source_directory.identity
            ) as parent:
                raw, _ = read_regular_in_directory_v5(parent, parts[-1], maximum_bytes=32 * 1024 * 1024)

            def blob_id(value):
                return hashlib.sha1(b"blob " + str(len(value)).encode("ascii") + b"\0" + value).hexdigest()

            if blob_id(raw) != oid:
                flags = eol[path]
                attributes = " ".join(flags[2:]).removeprefix("attr/")
                normalize = attributes in {
                    "text",
                    "text=auto",
                    "text eol=lf",
                    "text eol=crlf",
                    "text=auto eol=lf",
                    "text=auto eol=crlf",
                }
                normalize = normalize or (not attributes and autocrlf in {"true", "input"})
                if flags[:2] != ["i/lf", "w/crlf"] or not normalize or b"\0" in raw:
                    raise ValueError("working source differs")
                raw = raw.replace(b"\r\n", b"\n")
                if blob_id(raw) != oid:
                    raise ValueError("working source differs")
            if path in EDITABLE_POLICY_PATHS_V5:
                sources[path] = raw
        if (
            invoke("rev-parse", "--verify", "HEAD").strip() != expected_source_commit.encode("ascii")
            or invoke("ls-files", "--stage", "--full-name", "-z") != index_raw
            or invoke("ls-files", "--others", "--exclude-standard", "--full-name", "-z")
        ):
            raise ValueError("source changed during inspection")
        return PolicySourceSnapshotV5.from_tracked_bytes(
            source_commit=expected_source_commit,
            source_by_path={path: sources[path] for path in EDITABLE_POLICY_PATHS_V5},
        )


class ReplayReadinessFailureV5(ValueError):
    """Exactly one content-free blocker; underlying exception text is discarded."""

    def __init__(self, code: ReadinessBlockerV5):
        if code not in _BLOCKERS:
            raise ValueError("invalid readiness blocker")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FullReplayReadinessV5:
    schema_version: Literal[5]
    git_executable_sha256: str
    qualification_outcome_sha256: str
    qualification_attempt_sha256: str
    qualification_retirement_sha256: str
    confirmation_outcome_sha256: str
    candidate_policy_sha256: str
    candidate_source_sha256: str
    source_snapshot_sha256: str
    execution_profile_sha256: str
    evaluator_contract_sha256: str
    pit_bundle_sha256: str
    prices_provenance_sha256: str
    baseline_authority_sha256: str
    scenario_grid_sha256: str
    sandbox_profile_sha256: str
    cleanup_evidence_sha256: str
    graph_sha256: str
    source_clean: Literal[True] = True
    cleanup_complete: Literal[True] = True
    permanently_retired: Literal[True] = True
    projection: Literal["local_full_replay_requires_separate_explicit_decision"] = (
        "local_full_replay_requires_separate_explicit_decision"
    )
    executable: Literal[False] = False
    replay_started: Literal[False] = False
    provider_calls: Literal[0] = 0

    def __post_init__(self):
        for field in fields(self):
            if field.name.endswith("_sha256"):
                ArtifactRefV5("identity", getattr(self, field.name))
        if (
            type(self.schema_version) is not int
            or self.schema_version != 5
            or any(
                getattr(self, key) is not True for key in ("source_clean", "cleanup_complete", "permanently_retired")
            )
            or self.executable is not False
            or self.replay_started is not False
            or type(self.provider_calls) is not int
            or self.provider_calls != 0
            or self.projection != "local_full_replay_requires_separate_explicit_decision"
        ):
            raise ValueError("invalid replay readiness projection")


def _retired(repository, outcome, attempt, stage):
    """Read the canonical original ledger only; no lock, append or recovery."""
    snapshot = _load(repository, attempt.retirement_ledger.preopen_snapshot_ref, StageRetirementSnapshotV5)
    module = qualification if stage == "qualification" else confirmation
    ledger_type = qualification.QualificationLedgerV5 if stage == "qualification" else confirmation.ConfirmationLedgerV5
    terminal_type = (
        qualification.QualificationRetirementV5 if stage == "qualification" else confirmation.ConfirmationRetirementV5
    )
    if (
        snapshot.retirement_domain_id != attempt.retirement_domain_id
        or snapshot.ledger_relative_path != attempt.retirement_ledger.relative_path
        or module._domain(attempt) != attempt.retirement_domain_id
    ):
        raise ValueError("retirement authority differs")
    ledger = ledger_type(repository, outcome.attempt_ref, attempt, snapshot)
    if ledger.state() != ("retired", outcome.retirement_terminal_ref):
        raise ValueError("outcome is not permanently retired")
    terminal = _load(repository, outcome.retirement_terminal_ref, terminal_type)
    qualification._check_terminal(ledger, terminal)
    if (
        terminal.status != "completed"
        or terminal.status != outcome.status
        or terminal.cleanup_evidence_ref != outcome.cleanup_evidence_ref
        or terminal.baseline_evidence_ref != outcome.baseline_evidence_ref
        or terminal.candidate_evidence_ref != outcome.candidate_evidence_ref
    ):
        raise ValueError("outcome is not the retired result")
    return snapshot, terminal, ledger


def _owner(repository, reference, value_type):
    """Select only owner identity metadata, never deserialize a panel spec."""
    item = repository.authenticate(reference)
    value = json.loads(item.content)
    if (
        item.reference != reference
        or canonical_sha256_v5(value) != reference.sha256
        or set(value) != {field.name for field in fields(value_type)}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 5
    ):
        raise ValueError("stage owner is not canonical")
    episode = value["episode"]
    if (
        type(episode) is not dict
        or set(episode) != {field.name for field in fields(EpisodePlanV5)}
        or episode["episode_ordinal"] is not None
        or episode["purpose"] != "qualification"
    ):
        raise ValueError("stage episode identity is absent")
    # Lineage composition remains opaque. Its exact bytes were committed before
    # opening and are authenticated here without creating another panel object.
    projection = SimpleNamespace(
        panel_ref=ArtifactRefV5(**episode["panel_ref"]),
        start_date=episode["start_date"],
        end_date=episode["end_date"],
    )
    return value, SimpleNamespace(episode=projection)


def _graph(repository, root, raw):
    """Walk every reference, including frozen discovery ancestry; no opaque JSON cuts."""
    done, active, paths = set(), set(), {}

    def visit(reference):
        if reference in active or paths.setdefault(reference.relative_path, reference.sha256) != reference.sha256:
            raise ValueError("graph cycle or conflicting path")
        if reference in done:
            return
        active.add(reference)
        if reference in raw:
            item = repository.authenticate_raw_artifact(reference)
            if item.reference != reference:
                raise ValueError("raw authority differs")
        else:
            item = repository.authenticate(reference)
            if item.reference != reference or canonical_sha256_v5(json.loads(item.content)) != reference.sha256:
                raise ValueError("graph authority differs")
            for child in item.child_references:
                visit(child)
        active.remove(reference)
        done.add(reference)

    visit(root)
    return canonical_sha256_v5(tuple(sorted(done, key=lambda ref: (ref.relative_path, ref.sha256))))


def _identities(repository, attempt, prior, confirmed_outcome):
    """Authenticate frozen source/evaluator ancestry without composing workers."""
    selection = _load(repository, prior.frozen_selection_ref, confirmation.FrozenConfirmationSelectionV5)
    manifest = _load(repository, prior.discovery_manifest_ref, CampaignManifestV5)
    discovery = _load(repository, manifest.panel_plan_ref, CampaignPanelPlanV5)
    finalization = _load(repository, selection.finalized_campaign_ref, FinalizedDiscoveryCampaignV5)
    evaluator = _load(repository, attempt.evaluator_contract_ref, EvaluatorContractV5)
    execution = _load(repository, attempt.execution_profile_ref, ExecutionProfileV5)
    sandbox = _load(repository, attempt.sandbox_profile_ref, SandboxProfileV5)
    grid = _load(repository, attempt.scenario_grid_ref, ScenarioGridV5)
    baseline = _load(repository, attempt.baseline_authority_ref, BaselineParentAuthorityV5)
    baseline_policy = _load(repository, selection.baseline_policy_ref, PolicyRevisionIdentityV5)
    candidate_policy = _load(repository, selection.policy_ref, PolicyRevisionIdentityV5)
    baseline_source = _load(repository, selection.baseline_source_ref, SourceBundleV5)
    candidate_source = _load(repository, selection.source_ref, SourceBundleV5)
    scope = _load(repository, manifest.policy_scope_ref, PolicyScopeDescriptorV5)
    adapter = _load(repository, prior.execution_adapter_ref, confirmation.ConfirmationAdapterConfigV5)
    for name in (
        "pit_bundle_ref",
        "prices_provenance_ref",
        "execution_profile_ref",
        "evaluator_contract_ref",
        "scenario_grid_ref",
        "baseline_authority_ref",
        "sandbox_profile_ref",
    ):
        if getattr(attempt, name) != getattr(prior, name):
            raise ValueError("qualification identity differs from confirmation")
    for name in ("execution_profile_ref", "evaluator_contract_ref", "baseline_authority_ref", "sandbox_profile_ref"):
        if getattr(attempt, name) != getattr(manifest, name):
            raise ValueError("stage identity differs from manifest")
    validate_campaign_manifest_bindings_v5(manifest, panel_plan=discovery, evaluator_contract=evaluator)
    expected_source = PolicySourceSnapshotV5(scope.source_commit, scope.editable_source_sha256)
    _validate_precomposition_bindings_v5(
        target=manifest.target,
        source_snapshot=expected_source,
        execution_profile_ref=attempt.execution_profile_ref,
        execution_profile=execution,
        evaluator_contract_ref=attempt.evaluator_contract_ref,
        evaluator_contract=evaluator,
        baseline_authority_ref=attempt.baseline_authority_ref,
        baseline_authority=baseline,
        panel_plan_ref=manifest.panel_plan_ref,
        panel_plan=discovery,
        sandbox_profile_ref=attempt.sandbox_profile_ref,
        sandbox_profile=sandbox,
        resources=manifest.resources,
    )
    if (
        selection.discovery_manifest_ref != prior.discovery_manifest_ref
        or finalization.discovery_manifest_ref != prior.discovery_manifest_ref
        or finalization.checkpoint_ref != selection.checkpoint_ref
        or finalization.archive_ref != selection.archive_ref
        or len(finalization.rounds) != selection.final_round
        or selection.final_round != manifest.search.max_feedback_rounds
        or selection.experiment_ref != prior.discovery_champion_experiment_ref
        or selection.policy_ref != prior.discovery_champion_policy_ref
        or selection.policy_ref != attempt.confirmed_policy_ref
        or confirmed_outcome.confirmed_policy_ref != selection.policy_ref
        or prior.confirmation_plan_ref.sha256 != discovery.confirmation_plan_sha256
        or attempt.qualification_plan_ref.sha256 != discovery.qualification_plan_sha256
        or attempt.target != manifest.target
        or attempt.pit_bundle_ref != discovery.pit_bundle_ref
        or attempt.prices_provenance_ref != discovery.prices_provenance_ref
        or grid.scenarios != evaluator.friction_grid
        or selection.baseline_policy_ref != baseline.policy_revision_ref
        or selection.baseline_source_ref != baseline.source_bundle_ref
        or manifest.baseline_policy_revision_ref != baseline.policy_revision_ref
        or scope.baseline_policy_revision_ref != baseline.policy_revision_ref
        or baseline_policy != baseline.policy_revision
        or baseline_source != baseline.source_bundle
        or scope.source_commit != manifest.source_commit
        or scope.full_source_escape_allowed != manifest.search.allow_full_source_escape
        or adapter.discovery_manifest_ref != prior.discovery_manifest_ref
        or adapter.repository_root_identity_sha256 != repository.root_identity_sha256
    ):
        raise ValueError("frozen identity ancestry differs")
    for source, policy in ((baseline_source, baseline_policy), (candidate_source, candidate_policy)):
        if (
            tuple((file.path, file.sha256) for file in source.files) != policy.editable_source_sha256
            or policy.trusted_policy_runtime_sha256 != baseline_policy.trusted_policy_runtime_sha256
            or policy.immutable_constraints_sha256 != baseline_policy.immutable_constraints_sha256
        ):
            raise ValueError("source identity differs")
    return SimpleNamespace(
        selection=selection,
        manifest=manifest,
        discovery=discovery,
        evaluator=evaluator,
        sandbox=sandbox,
        grid=grid,
        baseline_policy=baseline_policy,
        candidate_policy=candidate_policy,
        adapter=adapter,
        expected_source=expected_source,
    )


def full_replay_readiness(
    *,
    repository,
    qualification_outcome_ref: ArtifactRefV5,
    output_path: str,
    git_authority: ReadinessGitAuthorityV5,
) -> ArtifactRefV5:
    """Write one create-only projection after every gate; never return executable arguments."""
    blocker: ReadinessBlockerV5 = "invalid_request"
    try:
        if type(qualification_outcome_ref) is not ArtifactRefV5:
            raise ValueError("outcome reference required")
        ArtifactRefV5(output_path, "0" * 64)
        blocker = "git_authority_invalid"
        # Authenticate the operator's tool before inspecting the outcome graph.
        # The source helper pins and reauthenticates it again before any launch.
        with _open_trusted_git(git_authority):
            pass
        blocker = "artifact_graph_invalid"
        outcome = _load(repository, qualification_outcome_ref, QualificationOutcomeV5)
        blocker = "qualification_unsuccessful"
        if outcome.status != "completed" or not outcome.qualified:
            raise ValueError("successful outcome required")
        blocker = "artifact_graph_invalid"
        attempt = _load(repository, outcome.attempt_ref, QualificationAttemptCommitmentV5)
        confirmed_outcome = _load(repository, attempt.confirmation_outcome_ref, ConfirmationOutcomeV5)
        prior = _load(repository, confirmed_outcome.attempt_ref, ConfirmationAttemptCommitmentV5)
        blocker = "retirement_invalid"
        snapshot, terminal, ledger = _retired(repository, outcome, attempt, "qualification")
        prior_snapshot, prior_terminal, prior_ledger = _retired(repository, confirmed_outcome, prior, "confirmation")
        blocker = "identity_invalid"
        inputs = _identities(repository, attempt, prior, confirmed_outcome)
        owner, plan = _owner(repository, attempt.qualification_plan_ref, QualificationPanelPlanV5)
        prior_owner, prior_plan = _owner(repository, prior.confirmation_plan_ref, ConfirmationPanelPlanV5)
        for value, snap, stage_attempt, stage in (
            (owner, snapshot, attempt, "qualification"),
            (prior_owner, prior_snapshot, prior, "confirmation"),
        ):
            if (
                value["pit_bundle_sha256"] != attempt.pit_bundle_ref.sha256
                or value["partition_seed_sha256"] != inputs.discovery.partition_seed_sha256
                or value[stage + "_retirement_domain_id"] != stage_attempt.retirement_domain_id
                or value[stage + "_ledger_snapshot_sha256"] != snap.ledger_head_sha256
            ):
                raise ValueError("retired owner differs")
        if (
            canonical_sha256_v5(owner["target"]) != canonical_sha256_v5(attempt.target)
            or prior_owner["target_sha256"] != attempt.target.sha256
            or max(item.end_date for item in inputs.discovery.discovery) >= prior_plan.episode.start_date
            or prior_plan.episode.end_date >= plan.episode.start_date
        ):
            raise ValueError("retired target or chronology differs")
        blocker = "artifact_graph_invalid"
        graph_sha256 = _graph(
            repository,
            qualification_outcome_ref,
            raw={
                attempt.pit_bundle_ref,
                attempt.prices_provenance_ref,
                plan.episode.panel_ref,
                prior_plan.episode.panel_ref,
                *(
                    item.panel_ref
                    for item in (inputs.discovery.mechanics, inputs.discovery.quick, *inputs.discovery.discovery)
                ),
            },
        )
        blocker = "identity_invalid"
        inputs.attempt_ref, inputs.attempt = confirmed_outcome.attempt_ref, prior
        recomputed_confirmation = confirmation._result(
            repository, inputs, confirmed_outcome.retirement_terminal_ref, prior_terminal, prior_plan
        )
        if (
            recomputed_confirmation != confirmed_outcome
            or not recomputed_confirmation.eligible_to_request_qualification
        ):
            raise ValueError("confirmation gate differs")
        inputs.attempt_ref, inputs.attempt = outcome.attempt_ref, attempt
        if qualification._result(repository, inputs, outcome.retirement_terminal_ref, terminal, plan) != outcome:
            raise ValueError("qualification gate differs")
        blocker = "source_not_clean_or_matching"
        source = capture_readiness_source_v5(
            source_root=Path(inputs.adapter.source_root),
            git_authority=git_authority,
            expected_source_commit=inputs.manifest.source_commit,
        )
        if type(source) is not PolicySourceSnapshotV5 or source != inputs.expected_source:
            raise ValueError("source drift")
        blocker = "retirement_invalid"
        if ledger.state() != ("retired", outcome.retirement_terminal_ref) or prior_ledger.state() != (
            "retired",
            confirmed_outcome.retirement_terminal_ref,
        ):
            raise ValueError("retirement changed during authentication")
        record = FullReplayReadinessV5(
            5,
            git_authority.sha256,
            qualification_outcome_ref.sha256,
            outcome.attempt_ref.sha256,
            outcome.retirement_terminal_ref.sha256,
            attempt.confirmation_outcome_ref.sha256,
            outcome.qualified_policy_ref.sha256,
            inputs.selection.source_ref.sha256,
            canonical_sha256_v5(source),
            attempt.execution_profile_ref.sha256,
            attempt.evaluator_contract_ref.sha256,
            attempt.pit_bundle_ref.sha256,
            attempt.prices_provenance_ref.sha256,
            attempt.baseline_authority_ref.sha256,
            attempt.scenario_grid_ref.sha256,
            attempt.sandbox_profile_ref.sha256,
            outcome.cleanup_evidence_ref.sha256,
            graph_sha256,
        )
        blocker = "output_unavailable"
        return repository.create_readiness_record(output_path, record)
    except ArtifactExistsV5:
        raise ReplayReadinessFailureV5("output_exists") from None
    except (
        ValueError,
        TypeError,
        OSError,
        RuntimeError,
        KeyError,
        AttributeError,
        ArithmeticError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        raise ReplayReadinessFailureV5(blocker) from None


__all__ = ["FullReplayReadinessV5", "ReplayReadinessFailureV5", "full_replay_readiness"]
