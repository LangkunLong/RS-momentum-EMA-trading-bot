"""Local-only lifecycle evidence for a detached PIT optimizer holdout controller."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping

from core.pit_optimizer_artifacts import atomic_replace_json, canonical_json_bytes
from core.pit_optimizer_evaluation import (
    EvaluationPanelSpec,
    PanelAggregateSummary,
    QualificationDecision,
    QualificationOutcomeProof,
    QualificationPanelIdentity,
    QualificationPanelPlan,
    QualificationReservation,
    QualificationRetirementLedger,
)


_ACTIVE_STATES = frozenset({"preflight", "baseline_replay", "candidate_replay", "finalizing"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_ALL_STATES = _ACTIVE_STATES | _TERMINAL_STATES
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_APPROVED_FULL_REPLAY_COVERAGE = frozenset(
    {"sp500", "nasdaq100", "russell2000"}
)


@dataclass(frozen=True, slots=True)
class QualificationRunResult:
    """Provider-free terminal facts from one permanently retired panel."""

    decision: QualificationDecision
    reservation: QualificationReservation
    outcome: QualificationOutcomeProof
    coverage_scope: frozenset[str]
    full_replay_ready: bool
    full_replay_started: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.decision, QualificationDecision)
            or not isinstance(self.reservation, QualificationReservation)
            or not isinstance(self.outcome, QualificationOutcomeProof)
            or not isinstance(self.coverage_scope, frozenset)
            or not self.coverage_scope
            or not self.coverage_scope.issubset(_APPROVED_FULL_REPLAY_COVERAGE)
            or type(self.full_replay_ready) is not bool
            or self.full_replay_started is not False
            or self.outcome.reservation_record_sha256
            != self.reservation.reservation_record_sha256
            or self.outcome.completed is not True
            or self.outcome.decision_sha256 is None
            or self.outcome.qualified is not self.decision.qualified
            or self.full_replay_ready
            is not (
                self.decision.qualified
                and self.coverage_scope == _APPROVED_FULL_REPLAY_COVERAGE
            )
        ):
            raise ValueError("qualification run result is invalid")


def qualification_coverage_scope(
    plan: QualificationPanelPlan,
) -> frozenset[str]:
    """Return authenticated eligible-universe affiliations bound by the plan."""

    if not isinstance(plan, QualificationPanelPlan):
        raise ValueError("qualification coverage requires a closed plan")
    scope = frozenset(
        affiliation
        for allocation in plan.stratum_allocations
        if allocation.eligible_count > 0
        for affiliation in allocation.source_affiliations
    )
    if not scope or not scope.issubset(_APPROVED_FULL_REPLAY_COVERAGE):
        raise ValueError("qualification coverage scope is invalid")
    return scope


def run_one_use_qualification(
    *,
    plan: QualificationPanelPlan,
    identity: QualificationPanelIdentity,
    candidate_identity_sha256: str,
    ledger: QualificationRetirementLedger,
    evaluate_baseline: Callable[[EvaluationPanelSpec], PanelAggregateSummary],
    evaluate_candidate: Callable[[EvaluationPanelSpec], PanelAggregateSummary],
    cleanup: Callable[[], None],
    record_reservation: Callable[
        [QualificationPanelIdentity, QualificationReservation], None
    ]
    | None = None,
    record_decision: Callable[[QualificationDecision], None] | None = None,
    record_outcome: Callable[
        [QualificationReservation, QualificationOutcomeProof], None
    ]
    | None = None,
) -> QualificationRunResult:
    """Retire, evaluate, decide, and close one qualification without a provider."""

    if (
        not isinstance(plan, QualificationPanelPlan)
        or not isinstance(identity, QualificationPanelIdentity)
        or not isinstance(ledger, QualificationRetirementLedger)
        or _SHA256_RE.fullmatch(candidate_identity_sha256 or "") is None
        or not callable(evaluate_baseline)
        or not callable(evaluate_candidate)
        or not callable(cleanup)
        or record_reservation is not None and not callable(record_reservation)
        or record_decision is not None and not callable(record_decision)
        or record_outcome is not None and not callable(record_outcome)
    ):
        raise ValueError("qualification run inputs are invalid")
    expected_identity = QualificationPanelIdentity.from_plan(
        plan,
        warmup_contract_sha256=identity.warmup_contract_sha256,
        engine_policy_sha256=identity.engine_policy_sha256,
    )
    if expected_identity != identity:
        raise ValueError("qualification identity differs from the sealed plan")

    reservation: QualificationReservation | None = None
    attempted = False
    decision: QualificationDecision | None = None
    outcome: QualificationOutcomeProof | None = None
    try:
        reservation = ledger.reserve_qualification(
            identity,
            candidate_identity_sha256=candidate_identity_sha256,
        )
        try:
            if record_reservation is not None:
                record_reservation(identity, reservation)
            attempted = True
            baseline = evaluate_baseline(plan.qualification_panel)
            candidate = evaluate_candidate(plan.qualification_panel)
            candidate_decision = QualificationDecision.from_result(
                candidate_evidence=candidate,
                baseline_evidence=baseline,
                qualification_panel=plan.qualification_panel,
                target=plan.target,
                evaluation_complete=True,
                integrity_complete=True,
            )
            if record_decision is not None:
                record_decision(candidate_decision)
            decision = candidate_decision
        finally:
            outcome = ledger.record_qualification_outcome(
                reservation,
                attempted=attempted,
                completed=decision is not None,
                terminal_code=(
                    "qualification_completed"
                    if decision is not None
                    else "qualification_evaluation_failed"
                ),
                decision=decision,
            )
            if record_outcome is not None:
                record_outcome(reservation, outcome)
    finally:
        cleanup()

    if reservation is None or decision is None or outcome is None:
        raise AssertionError("qualification terminal state was not produced")
    coverage_scope = qualification_coverage_scope(plan)
    return QualificationRunResult(
        decision=decision,
        reservation=reservation,
        outcome=outcome,
        coverage_scope=coverage_scope,
        full_replay_ready=(
            decision.qualified
            and coverage_scope == _APPROVED_FULL_REPLAY_COVERAGE
        ),
        full_replay_started=False,
    )


@dataclass(frozen=True, slots=True)
class DiscoveryWinnerEvidence:
    """Authenticated local discovery evidence consumed by the provider-free holdout phase."""

    root: Path
    run_id: str
    manifest_sha256: str
    readiness_sha256: str
    incumbent: Mapping[str, object]
    cumulative_diff: str


def _read_canonical_json(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"holdout {label} is invalid")
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"holdout {label} JSON is invalid") from exc
    if not isinstance(decoded, dict) or raw != canonical_json_bytes(decoded):
        raise ValueError(f"holdout {label} is not canonical")
    return decoded


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"holdout {label} is invalid")
    return value


def load_discovery_winner_evidence(
    *,
    root: Path,
    expected_summary_sha256: str,
    expected_role_calls: int,
) -> DiscoveryWinnerEvidence:
    """Load a closed or safely recoverable local discovery winner."""

    artifact_root = Path(root)
    if (
        not artifact_root.is_absolute()
        or artifact_root.is_symlink()
        or not artifact_root.is_dir()
        or not isinstance(expected_role_calls, int)
        or expected_role_calls < 3
    ):
        raise ValueError("holdout discovery evidence inputs are invalid")
    _require_sha256(expected_summary_sha256, label="expected summary digest")
    summary_path = artifact_root / "summary.json"
    summary = _read_canonical_json(summary_path, label="discovery summary")
    raw_summary = summary_path.read_bytes()
    if hashlib.sha256(raw_summary).hexdigest() != expected_summary_sha256:
        raise ValueError("holdout discovery summary digest differs")
    terminal = summary.get("terminal")
    hidden = summary.get("hidden_outcome")
    accounting = summary.get("accounting")
    cleanup = summary.get("cleanup")
    incumbent = summary.get("incumbent")
    discovery = summary.get("discovery_outcome")
    iterations = summary.get("iterations")
    closed_canary = (
        summary.get("status") == "loop_verified_no_long_replay_candidate"
        and terminal == {"code": "iteration_limit", "detail": None, "exit_code": 0}
        and isinstance(accounting, dict)
        and accounting.get("api_calls") == expected_role_calls
    )
    recoverable_winner = (
        summary.get("status") == "aborted"
        and terminal
        == {"code": "provider_protocol_failure", "detail": None, "exit_code": 1}
        and isinstance(accounting, dict)
        and type(accounting.get("api_calls")) is int
        and 3 <= accounting["api_calls"] <= expected_role_calls
        and isinstance(iterations, dict)
        and type(iterations.get("started")) is int
        and type(iterations.get("completed")) is int
        and type(iterations.get("valid_evaluations")) is int
        and type(iterations.get("incumbent_updates")) is int
        and iterations["started"] > iterations["completed"] >= 1
        and iterations["valid_evaluations"] >= 1
        and iterations["incumbent_updates"] >= 1
    )
    if (
        summary.get("schema_version") != 3
        or summary.get("phase") != "run"
        or not isinstance(terminal, dict)
        or not (closed_canary or recoverable_winner)
        or not isinstance(hidden, dict)
        or hidden != {
            "opened": False,
            "validation_reservation_sha256": None,
            "long_replay_eligible": None,
        }
        or not isinstance(accounting, dict)
        or accounting.get("retained_reservation_tokens") != 0
        or accounting.get("incomplete_accounting_calls") != 0
        or accounting.get("accounting_complete") is not True
        or not isinstance(cleanup, dict)
        or cleanup.get("complete") is not True
        or cleanup.get("source_modified") is not False
        or not isinstance(incumbent, dict)
        or not isinstance(discovery, dict)
    ):
        raise ValueError("holdout discovery summary is not an eligible closed canary")
    run_id = summary.get("run_id")
    manifest_sha256 = summary.get("manifest_sha256")
    readiness_sha256 = summary.get("readiness_sha256")
    winner_identity_sha256 = discovery.get("winner_identity_sha256")
    incumbent_identity_sha256 = incumbent.get("identity_sha256")
    if (
        not isinstance(run_id, str)
        or not run_id
        or _require_sha256(manifest_sha256, label="manifest digest") != manifest_sha256
        or _require_sha256(readiness_sha256, label="readiness digest") != readiness_sha256
        or _require_sha256(winner_identity_sha256, label="winner identity digest")
        != winner_identity_sha256
        or winner_identity_sha256 != incumbent_identity_sha256
    ):
        raise ValueError("holdout discovery winner identity is invalid")
    artifacts = summary.get("artifact_digests")
    if not isinstance(artifacts, list):
        raise ValueError("holdout discovery artifact index is invalid")
    digests: dict[str, str] = {}
    for item in artifacts:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item.get("path"), str)
            or item["path"] in digests
        ):
            raise ValueError("holdout discovery artifact index is invalid")
        digests[item["path"]] = _require_sha256(
            item.get("sha256"), label="artifact digest"
        )
    expected_diff_digest = digests.get("incumbent.diff")
    if expected_diff_digest is None:
        raise ValueError("holdout discovery incumbent diff is absent")
    diff_path = artifact_root / "incumbent.diff"
    if diff_path.is_symlink() or not diff_path.is_file():
        raise ValueError("holdout discovery incumbent diff is invalid")
    raw_diff = diff_path.read_bytes()
    if hashlib.sha256(raw_diff).hexdigest() != expected_diff_digest:
        raise ValueError("holdout discovery incumbent diff differs")
    try:
        cumulative_diff = raw_diff.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("holdout discovery incumbent diff is not UTF-8") from exc
    if not cumulative_diff:
        raise ValueError("holdout discovery incumbent diff is empty")
    return DiscoveryWinnerEvidence(
        root=artifact_root.resolve(),
        run_id=run_id,
        manifest_sha256=manifest_sha256,
        readiness_sha256=readiness_sha256,
        incumbent=dict(incumbent),
        cumulative_diff=cumulative_diff,
    )


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class HoldoutProgressJournal:
    """Atomically publish content-free local liveness evidence for one holdout run."""

    def __init__(self, *, root: Path, controller_pid: int) -> None:
        candidate = Path(root)
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not candidate.is_dir()
            or not isinstance(controller_pid, int)
            or controller_pid <= 0
        ):
            raise ValueError("holdout progress journal inputs are invalid")
        details = candidate.lstat()
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("holdout progress journal root is invalid")
        self._root = candidate.resolve()
        self._controller_pid = controller_pid
        self._path = self._root / "progress.json"

    @staticmethod
    def _validate(value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "state",
            "terminal",
            "sequence",
            "controller_pid",
        }:
            raise ValueError("holdout progress journal record is invalid")
        state = value.get("state")
        terminal = value.get("terminal")
        sequence = value.get("sequence")
        controller_pid = value.get("controller_pid")
        if (
            value.get("schema_version") != 3
            or state not in _ALL_STATES
            or type(terminal) is not bool
            or terminal is not (state in _TERMINAL_STATES)
            or type(sequence) is not int
            or sequence < 1
            or type(controller_pid) is not int
            or controller_pid <= 0
        ):
            raise ValueError("holdout progress journal record is invalid")
        return value

    def _existing(self) -> dict[str, object] | None:
        if not self._path.exists():
            return None
        if self._path.is_symlink() or not self._path.is_file():
            raise ValueError("holdout progress journal path is invalid")
        raw = self._path.read_bytes()
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("holdout progress journal JSON is invalid") from exc
        value = self._validate(decoded)
        if raw != canonical_json_bytes(value):
            raise ValueError("holdout progress journal JSON is not canonical")
        return value

    def publish(self, state: str) -> Mapping[str, object]:
        if state not in _ALL_STATES:
            raise ValueError("holdout progress state is invalid")
        current = self._existing()
        if current is not None:
            if current["controller_pid"] != self._controller_pid:
                raise RuntimeError("holdout progress controller differs")
            if current["terminal"] is True:
                raise RuntimeError("holdout progress journal is terminal")
            sequence = int(current["sequence"]) + 1
        else:
            sequence = 1
        value: dict[str, object] = {
            "schema_version": 3,
            "state": state,
            "terminal": state in _TERMINAL_STATES,
            "sequence": sequence,
            "controller_pid": self._controller_pid,
        }
        atomic_replace_json(self._path, value)
        return value

    def read(self) -> Mapping[str, object]:
        value = self._existing()
        if value is None:
            raise FileNotFoundError("holdout progress journal is absent")
        return value

    def health(
        self,
        *,
        process_is_running: Callable[[int], bool] = _pid_is_running,
    ) -> str:
        if not callable(process_is_running):
            raise ValueError("holdout process liveness capability is invalid")
        current = self.read()
        if current["terminal"] is True:
            return "terminal"
        return (
            "running"
            if process_is_running(int(current["controller_pid"]))
            else "controller_unavailable"
        )
