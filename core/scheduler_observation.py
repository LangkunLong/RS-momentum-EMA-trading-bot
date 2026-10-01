"""Thread-safe process-wide observation state for one scheduler session."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping


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
    if depth > 3:
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

    def __init__(self, run_id: str):
        self.run_id = _safe_string(str(run_id))
        self._lock = threading.RLock()
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
        self._service_health = "healthy"
        self._required_input_coverage = "complete"

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

    def to_receipt(self) -> dict[str, Any]:
        with self._lock:
            overall_readiness = (
                "fail"
                if self._service_health == "failed"
                or self._required_input_coverage == "failed"
                else "unverified"
            )
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
