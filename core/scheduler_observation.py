"""Thread-safe process-wide observation state for one scheduler session."""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


_active_lock = threading.Lock()
_active_observation: SchedulerObservation | None = None
_SENSITIVE_KEY_PARTS = (
    "account",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)=([^&\s]+)"
)
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_COVERAGE_RANK = {"complete": 0, "degraded": 1, "unverified": 2, "failed": 3}
_INDEX_REQUEST_LIMIT = 6
_WORKFLOW_TRANSITION_WRITE_LIMIT = 6
_WORKFLOW_SNAPSHOT_WRITE_LIMIT = 2


class IndexRequestBudgetExceeded(RuntimeError):
    """Raised before an index-source request would exceed the observation cap."""


class WorkflowWriteBudgetExceeded(RuntimeError):
    """Raised before a workflow write would exceed the observation cap."""


def _safe_string(value: str) -> str:
    text = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    return text[:240]


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_string(value)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key)[:80]
            if any(part in safe_key.lower() for part in _SENSITIVE_KEY_PARTS):
                continue
            safe[safe_key] = _safe_value(item, depth=depth + 1)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:50]]
    return _safe_string(type(value).__name__)


class SchedulerObservation:
    """Accumulate sanitized work outcomes and latched health signals."""

    def __init__(self, run_id: str, *, snapshot_path: str | Path | None = None):
        self.run_id = _safe_string(str(run_id))
        self._lock = threading.RLock()
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._snapshot_error: str | None = None
        self._events: list[dict[str, Any]] = []
        self._input_gaps: list[dict[str, Any]] = []
        self._issues: list[dict[str, Any]] = []
        self._resource_denials: dict[str, int] = {}
        self._resource_counters: dict[str, int] = {
            "index_attempts": 0,
            "workflow_transition_writes": 0,
            "workflow_snapshot_writes": 0,
        }
        self._provider_counters: dict[str, dict[str, Any]] = {}
        self._scan_candidates: dict[str, dict[str, Any]] = {}
        self._scan_coverage: dict[str, Any] = {
            "requested": 0,
            "validated": 0,
            "analyzed": 0,
            "rs_covered": 0,
            "fundamental_covered": 0,
            "candidate_outcomes": [],
        }
        self._service_health = "healthy"
        self._required_input_coverage = "complete"
        self._in_flight_work: list[dict[str, str]] = []
        self._execution_window: dict[str, Any] = {
            "requested_stop_at": None,
            "hard_deadline_at": None,
            "scheduler_started_at": None,
            "stop_requested_at": None,
            "interrupted_at": None,
            "actual_stop_at": None,
            "deadline_exceeded": False,
        }

    def configure_execution_window(
        self, *, requested_stop_at: datetime, hard_deadline_at: datetime
    ) -> None:
        with self._lock:
            self._execution_window["requested_stop_at"] = requested_stop_at.isoformat()
            self._execution_window["hard_deadline_at"] = hard_deadline_at.isoformat()
            self._persist_partial_snapshot_locked()

    def record_scheduler_started(self, at: datetime) -> None:
        with self._lock:
            self._execution_window["scheduler_started_at"] = at.isoformat()
            self.record_event(
                "scheduler", "bounded_session", "started", {"at": at.isoformat()}
            )

    def begin_work(self, kind: str, key: str, scheduled_at: datetime) -> None:
        with self._lock:
            self._in_flight_work.append(
                {
                    "kind": _safe_string(str(kind)),
                    "key": _safe_string(str(key)),
                    "scheduled_at": scheduled_at.isoformat(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._persist_partial_snapshot_locked()

    def finish_work(self, kind: str, key: str) -> None:
        with self._lock:
            for index in range(len(self._in_flight_work) - 1, -1, -1):
                work = self._in_flight_work[index]
                if work["kind"] == kind and work["key"] == key:
                    del self._in_flight_work[index]
                    break
            self._persist_partial_snapshot_locked()

    def try_admit_work(
        self, kind: str, key: str, now: Callable[[], datetime]
    ) -> bool:
        """Admit work only while before the bounded observation stop."""
        with self._lock:
            at = now()
            requested_stop_at = self._execution_window["requested_stop_at"]
            if requested_stop_at is None or at < datetime.fromisoformat(requested_stop_at):
                # This is the final admission transition: keep it in memory so
                # no synchronous evidence write can move callback start past
                # the cutoff after the clock check. The persisted in-flight
                # marker remains the crash-safe evidence if execution stops.
                self._events.append(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "kind": "scheduler_work",
                        "key": _safe_string(f"{kind}:{key}"),
                        "status": "started",
                        "details": _safe_value({"started_at": at.isoformat()}),
                    }
                )
                return True

            self.finish_work(kind, key)
            details = {
                "reason": "requested_stop_cutoff",
                "not_started": True,
                "checked_at": at.isoformat(),
                "requested_stop_at": requested_stop_at,
            }
            self.request_stop(at)
            self.record_event(
                "scheduler_work", f"{kind}:{key}", "denied", details
            )
            self.latch_service_issue(
                "observation_work_denied_at_stop", details, unverified=True
            )
            return False

    def request_stop(self, at: datetime) -> None:
        with self._lock:
            if self._execution_window["stop_requested_at"] is None:
                self._execution_window["stop_requested_at"] = at.isoformat()
                self.record_event(
                    "scheduler", "bounded_stop", "requested", {"at": at.isoformat()}
                )
            self._persist_partial_snapshot_locked()

    def mark_interrupted(self, at: datetime) -> None:
        """Latch an early Ctrl-C as incomplete for a bounded observation."""
        with self._lock:
            self._execution_window["interrupted_at"] = at.isoformat()
            details = {"interrupted_at": at.isoformat()}
            self.latch_service_issue(
                "observation_interrupted", details, unverified=True
            )
            self.record_event(
                "scheduler", "bounded_session", "interrupted", details
            )

    def mark_stopped(self, at: datetime, *, deadline_exceeded: bool = False) -> None:
        with self._lock:
            self._execution_window["actual_stop_at"] = at.isoformat()
            self._execution_window["deadline_exceeded"] = bool(deadline_exceeded)
            if self._in_flight_work:
                self.latch_service_issue(
                    "observation_stopped_with_work_in_flight",
                    {"in_flight_work": self._in_flight_work},
                )
            if deadline_exceeded:
                self.latch_service_issue(
                    "observation_hard_deadline_exceeded",
                    {"actual_stop_at": at.isoformat()},
                )
            self.record_event(
                "scheduler",
                "bounded_stop",
                (
                    "deadline_exceeded"
                    if deadline_exceeded
                    else "interrupted"
                    if self._execution_window["interrupted_at"] is not None
                    else "completed"
                ),
                {
                    "actual_stop_at": at.isoformat(),
                    "work_in_flight": bool(self._in_flight_work),
                },
            )
            self._persist_partial_snapshot_locked()

    def _partial_receipt_locked(self) -> dict[str, Any]:
        candidates = list(self._scan_candidates.values())
        readiness = (
            "fail"
            if self._service_health == "failed"
            or self._required_input_coverage == "failed"
            else "unverified"
        )
        provider_counters = {
            provider: {
                **{key: value for key, value in counter.items() if key != "refusal_statuses"},
                "refusal_statuses": dict(counter["refusal_statuses"]),
            }
            for provider, counter in self._provider_counters.items()
        }
        return {
            "schema": "scheduler-observation-partial/v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "service_health": self._service_health,
            "required_input_coverage": self._required_input_coverage,
            "overall_readiness": readiness,
            "execution_window": dict(self._execution_window),
            "in_flight_work": [dict(work) for work in self._in_flight_work],
            "event_count": len(self._events),
            "recent_events": [dict(event) for event in self._events[-20:]],
            "issues": [dict(issue) for issue in self._issues[-50:]],
            "resource_denials": dict(self._resource_denials),
            "resource_counters": dict(self._resource_counters),
            "provider_counters": provider_counters,
            "input_gap_count": len(self._input_gaps),
            "input_gap_sample": [dict(gap) for gap in self._input_gaps[:50]],
            "scan_coverage": {
                **{
                    key: value
                    for key, value in self._scan_coverage.items()
                    if key != "candidate_outcomes"
                },
                "candidate_outcome_count": len(candidates),
                "candidate_outcome_sample": [
                    _safe_value(candidate) for candidate in candidates[-20:]
                ],
            },
            "snapshot_error": self._snapshot_error,
        }

    def _persist_partial_snapshot_locked(self) -> None:
        if self._snapshot_path is None or self._snapshot_error is not None:
            return
        temporary_path = self._snapshot_path.with_name(
            f".{self._snapshot_path.name}.{os.getpid()}.tmp"
        )
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(self._partial_receipt_locked(), sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary_path, self._snapshot_path)
        except Exception as exc:  # noqa: BLE001
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._snapshot_error = type(exc).__name__
            self._service_health = "failed"
            self._issues.append(
                {
                    "code": "observation_snapshot_write_failed",
                    "details": {"error_type": self._snapshot_error},
                    "unverified": False,
                }
            )

    def record_event(
        self,
        kind: str,
        key: str,
        status: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._events.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "kind": _safe_string(str(kind)),
                    "key": _safe_string(str(key)),
                    "status": _safe_string(str(status)),
                    "details": _safe_value(details or {}),
                }
            )
            self._persist_partial_snapshot_locked()

    def latch_service_issue(
        self,
        code: str,
        details: Mapping[str, Any] | None = None,
        *,
        unverified: bool = False,
    ) -> None:
        safe_code = _safe_string(str(code))
        with self._lock:
            self._issues.append(
                {
                    "code": safe_code,
                    "details": _safe_value(details or {}),
                    "unverified": bool(unverified),
                }
            )
            if unverified:
                if self._service_health == "healthy":
                    self._service_health = "unverified"
            else:
                self._service_health = "failed"
            self._persist_partial_snapshot_locked()

    def record_resource_denial(
        self,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_code = _safe_string(str(code))
        with self._lock:
            self._resource_denials[safe_code] = self._resource_denials.get(safe_code, 0) + 1
            self.latch_service_issue(safe_code, details)

    def reserve_index_attempt(self, source: str) -> None:
        """Reserve one of six index requests before the caller performs I/O."""
        with self._lock:
            attempts = self._resource_counters["index_attempts"]
            if attempts >= _INDEX_REQUEST_LIMIT:
                self.record_resource_denial(
                    "index_cap_denied",
                    {"attempt": attempts + 1, "cap": _INDEX_REQUEST_LIMIT},
                )
                raise IndexRequestBudgetExceeded(
                    f"Index request cap ({_INDEX_REQUEST_LIMIT}) has been reached"
                )
            self._resource_counters["index_attempts"] = attempts + 1
            self.record_event(
                "provider_request", "index_source", "reserved", {"source": source}
            )

    def reserve_workflow_writes(
        self, *, transitions: int = 0, snapshots: int = 0
    ) -> None:
        """Atomically reserve observation write slots before a store transaction."""
        if transitions < 0 or snapshots < 0 or (transitions == 0 and snapshots == 0):
            raise ValueError("workflow write reservation must request positive counts")
        with self._lock:
            transition_attempt = self._resource_counters["workflow_transition_writes"] + transitions
            snapshot_attempt = self._resource_counters["workflow_snapshot_writes"] + snapshots
            denied_resource: str | None = None
            attempted = 0
            cap = 0
            if transition_attempt > _WORKFLOW_TRANSITION_WRITE_LIMIT:
                denied_resource = "workflow_transition_writes"
                attempted = transition_attempt
                cap = _WORKFLOW_TRANSITION_WRITE_LIMIT
            elif snapshot_attempt > _WORKFLOW_SNAPSHOT_WRITE_LIMIT:
                denied_resource = "workflow_snapshot_writes"
                attempted = snapshot_attempt
                cap = _WORKFLOW_SNAPSHOT_WRITE_LIMIT
            if denied_resource is not None:
                self.record_resource_denial(
                    "workflow_write_cap_denied",
                    {"resource": denied_resource, "attempt": attempted, "cap": cap},
                )
                raise WorkflowWriteBudgetExceeded(
                    f"Workflow {denied_resource} cap ({cap}) has been reached"
                )
            self._resource_counters["workflow_transition_writes"] = transition_attempt
            self._resource_counters["workflow_snapshot_writes"] = snapshot_attempt
            self.record_event(
                "workflow_write", "execution_store", "reserved",
                {"transitions": transitions, "snapshots": snapshots},
            )

    def record_scan_coverage(
        self,
        *,
        requested: int | None = None,
        validated: int | None = None,
        rs_covered: int | None = None,
        candidate_outcomes: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Record full-universe scan counts and per-symbol outcomes."""
        with self._lock:
            if requested is not None:
                self._scan_coverage["requested"] = max(0, int(requested))
            if validated is not None:
                self._scan_coverage["validated"] = max(0, int(validated))
            if rs_covered is not None:
                self._scan_coverage["rs_covered"] = max(0, int(rs_covered))
            for candidate in candidate_outcomes or []:
                safe = _safe_value(candidate)
                symbol = str(safe.get("symbol", ""))
                if not symbol:
                    continue
                endpoint_coverage = dict(safe.get("endpoint_coverage", {}))
                for gap in self._input_gaps:
                    if gap["symbol"] == symbol:
                        endpoint_coverage[gap["endpoint"]] = {
                            "status": gap["coverage_status"],
                            "reason": gap["reason"],
                        }
                safe["endpoint_coverage"] = endpoint_coverage
                prior = self._scan_candidates.get(symbol, {})
                if prior and safe.get("category") == "not_emitted":
                    prior["endpoint_coverage"] = {
                        **prior.get("endpoint_coverage", {}),
                        **endpoint_coverage,
                    }
                    continue
                self._scan_candidates[symbol] = safe
            self._scan_coverage["candidate_outcomes"] = [
                self._scan_candidates[symbol]
                for symbol in sorted(self._scan_candidates)
            ]
            self._scan_coverage["analyzed"] = sum(
                1
                for candidate in self._scan_candidates.values()
                if candidate.get("analyzed") is True
            )
            self._scan_coverage["fundamental_covered"] = sum(
                1
                for candidate in self._scan_candidates.values()
                if candidate.get("fundamental_coverage", {}).get("quarterly_income")
                == "available"
                and candidate.get("fundamental_coverage", {}).get("annual_income")
                == "available"
            )
            self._persist_partial_snapshot_locked()

    def record_provider_event(
        self,
        provider: str,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_provider = _safe_string(str(provider))
        safe_details = _safe_value(details or {})
        with self._lock:
            counter = self._provider_counters.setdefault(
                safe_provider,
                {
                    "logical_requests": 0,
                    "provider_refusals": 0,
                    "transport_errors": 0,
                    "suppressed_followups": 0,
                    "refusal_statuses": {},
                },
            )
            if outcome == "attempted":
                counter["logical_requests"] += 1
            elif outcome == "provider_refusal":
                counter["provider_refusals"] += 1
                status = safe_details.get("http_status")
                if status is not None:
                    statuses = counter["refusal_statuses"]
                    key = str(status)
                    statuses[key] = statuses.get(key, 0) + 1
            elif outcome == "transport_error":
                counter["transport_errors"] += 1
            elif outcome == "suppressed_followup":
                counter["suppressed_followups"] += 1
            self._persist_partial_snapshot_locked()

    def record_input_gap(
        self,
        symbol: str,
        endpoint: str,
        reason: str,
        *,
        coverage_status: str = "degraded",
    ) -> None:
        if coverage_status not in _COVERAGE_RANK or coverage_status == "complete":
            raise ValueError("coverage_status must be degraded, unverified, or failed")
        with self._lock:
            if _COVERAGE_RANK[coverage_status] > _COVERAGE_RANK[self._required_input_coverage]:
                self._required_input_coverage = coverage_status
            self._input_gaps.append(
                {
                    "symbol": _safe_string(str(symbol)),
                    "endpoint": _safe_string(str(endpoint)),
                    "reason": _safe_string(str(reason)),
                    "coverage_status": coverage_status,
                }
            )
            self._persist_partial_snapshot_locked()

    def to_receipt(self) -> dict[str, Any]:
        with self._lock:
            overall_readiness = (
                "fail"
                if self._service_health == "failed"
                or self._required_input_coverage == "failed"
                else "unverified"
            )
            scan_coverage = {
                key: value
                for key, value in self._scan_coverage.items()
                if key != "candidate_outcomes"
            }
            safe_scan_coverage = _safe_value(scan_coverage)
            safe_scan_coverage["candidate_outcomes"] = [
                _safe_value(candidate)
                for candidate in self._scan_coverage["candidate_outcomes"]
            ]
            return {
                "run_id": self.run_id,
                "service_health": self._service_health,
                "required_input_coverage": self._required_input_coverage,
                "overall_readiness": overall_readiness,
                "events": [dict(event) for event in self._events],
                "input_gaps": [dict(gap) for gap in self._input_gaps],
                "issues": [dict(issue) for issue in self._issues],
                "resource_denials": dict(self._resource_denials),
                "resource_counters": dict(self._resource_counters),
                "provider_counters": {
                    provider: {
                        **{key: value for key, value in counter.items() if key != "refusal_statuses"},
                        "refusal_statuses": dict(counter["refusal_statuses"]),
                    }
                    for provider, counter in self._provider_counters.items()
                },
                "scan_coverage": safe_scan_coverage,
                "execution_window": dict(self._execution_window),
                "in_flight_work": [dict(work) for work in self._in_flight_work],
                "snapshot_error": self._snapshot_error,
            }


@contextmanager
def activate_scheduler_observation(observation: SchedulerObservation) -> Iterator[None]:
    """Install one observation globally so worker threads share its state."""
    global _active_observation
    with _active_lock:
        if _active_observation is not None:
            raise RuntimeError("A scheduler observation is already active")
        _active_observation = observation
    try:
        yield
    finally:
        with _active_lock:
            if _active_observation is observation:
                _active_observation = None


def current_scheduler_observation() -> SchedulerObservation | None:
    with _active_lock:
        return _active_observation


def record_event(
    kind: str,
    key: str,
    status: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_event(kind, key, status, details)


def latch_service_issue(
    code: str,
    details: Mapping[str, Any] | None = None,
    *,
    unverified: bool = False,
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.latch_service_issue(code, details, unverified=unverified)


def record_resource_denial(
    code: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_resource_denial(code, details)


def record_provider_event(
    provider: str,
    outcome: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_provider_event(provider, outcome, details)


def record_input_gap(
    symbol: str,
    endpoint: str,
    reason: str,
    *,
    coverage_status: str = "degraded",
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_input_gap(
            symbol, endpoint, reason, coverage_status=coverage_status
        )


def record_scan_coverage(
    *,
    requested: int | None = None,
    validated: int | None = None,
    rs_covered: int | None = None,
    candidate_outcomes: list[Mapping[str, Any]] | None = None,
) -> None:
    observation = current_scheduler_observation()
    if observation is not None:
        observation.record_scan_coverage(
            requested=requested,
            validated=validated,
            rs_covered=rs_covered,
            candidate_outcomes=candidate_outcomes,
        )
