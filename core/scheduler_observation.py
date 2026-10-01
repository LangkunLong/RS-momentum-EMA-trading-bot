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
