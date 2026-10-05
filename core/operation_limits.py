"""Explicit per-operation resource contracts for supervised paper operations.

Contracts contain no operational defaults.  Manifests must bind one operation
to its own account, allowance owner, source/runtime/store identity, and finite
category and dimension limits before an operation can be admitted.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from core.scheduler_observation import current_scheduler_observation


COMMON_HTTP_CATEGORIES = (
    "account_read",
    "clock_read",
    "position_read",
    "open_order_read",
    "closed_order_read",
    "market_data_read",
    "fmp_read",
    "index_read",
    "entry_submit",
    "stop_submit",
    "exit_submit",
    "stop_replace",
    "order_cancel",
    "exact_order_lookup",
)

SAFETY_HTTP_CATEGORIES = frozenset(
    {
        "position_read",
        "open_order_read",
        "closed_order_read",
        "stop_submit",
        "exit_submit",
        "stop_replace",
        "order_cancel",
        "exact_order_lookup",
    }
)

ORDER_MUTATION_CATEGORIES = frozenset(
    {
        "entry_submit",
        "stop_submit",
        "exit_submit",
        "stop_replace",
        "order_cancel",
    }
)

LIFECYCLE_DIMENSIONS = (
    "stable_sample_iterations",
    "clock_reads",
    "durable_entry_polls",
    "final_clear_polls",
    "final_clear_invocations",
    "safety_recovery_passes",
    "eligible_order_ids",
    "replacement_chain_length",
    "cancel_verification_polls",
    "cancel_attempts_per_order",
    "exact_lookup_polls",
    "exact_order_lookups_per_order",
    "partial_fill_events",
    "stream_connections",
    "stream_events",
    "order_page_size",
    "order_pages",
    "market_data_pages",
)

OBSERVER_DIMENSIONS = (
    "scheduler_ticks",
    "clock_reads",
    "scan_cycles",
    "exit_check_cycles",
    "symbols_per_scan",
    "market_data_pages_per_symbol",
    "market_trend_pages_per_scan",
    "fmp_requests_per_symbol",
    "index_requests_per_scan",
    "stream_connections",
    "stream_events",
    "receipt_events",
    "output_bytes",
)

LIFECYCLE_DEADLINES = (
    "request_timeout_seconds",
    "entry_wait_seconds",
    "stable_sample_seconds",
    "exit_wait_seconds",
    "final_clear_wait_seconds",
    "cancel_verify_seconds",
    "terminal_chain_seconds",
    "stream_connect_seconds",
    "stream_stop_seconds",
)

OBSERVER_DEADLINES = (
    "request_timeout_seconds",
    "session_seconds",
    "hard_deadline_grace_seconds",
    "stream_connect_seconds",
    "stream_stop_seconds",
)

_SCHEMA = "paper-operation-limits/v1"
_OBSERVER_LOOP_SECONDS = 30
_MAX_ORDER_PAGE_SIZE = 500
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_LOCK = threading.RLock()
_ACTIVE_BUDGET: OperationBudget | None = None


class OperationManifestError(ValueError):
    """A manifest is missing, inconsistent, or outside its operation scope."""


class OperationCapExceeded(RuntimeError):
    """Raised before a request, event, identity, or loop exceeds its cap."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationManifestError(f"{label} must be an object")
    return value


def _required_text(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationManifestError(f"{label}.{key} is required")
    return value.strip()


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OperationManifestError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_seconds(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OperationManifestError(f"{label} must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise OperationManifestError(f"{label} must be a finite positive number")
    return seconds


def _parse_aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise OperationManifestError(f"{label} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperationManifestError(
            f"{label} must be an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationManifestError(f"{label} must include a UTC offset")
    return parsed


@dataclass(frozen=True)
class OperationManifest:
    """Validated, operation-specific inputs; totals and reserve are derived."""

    operation: str
    admission_status: str
    admission_ref: str
    binding: Mapping[str, Any]
    scope: Mapping[str, Any]
    dimensions: Mapping[str, int]
    deadlines_seconds: Mapping[str, float]
    http_category_limits: Mapping[str, int]
    total_http_attempts: int
    safety_reserve_http_attempts: int
    allowance_remaining_http_attempts: int

    @property
    def admitted(self) -> bool:
        return self.admission_status == "admitted"

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_operation: str | None = None,
    ) -> OperationManifest:
        data = _mapping(payload, "manifest")
        if data.get("schema") != _SCHEMA:
            raise OperationManifestError(f"schema must be {_SCHEMA}")
        allowed_root = {
            "schema",
            "operation",
            "admission_status",
            "admission_ref",
            "binding",
            "scope",
            "dimensions",
            "deadlines_seconds",
            "http_category_limits",
        }
        unknown_root = sorted(set(data) - allowed_root)
        if unknown_root:
            raise OperationManifestError(
                "unknown manifest fields: " + ", ".join(unknown_root)
            )

        operation = data.get("operation")
        if operation not in {"lifecycle", "observer"}:
            raise OperationManifestError("operation must be lifecycle or observer")
        if expected_operation is not None and operation != expected_operation:
            raise OperationManifestError(
                f"operation must be {expected_operation}, got {operation}"
            )

        admission_status = data.get("admission_status")
        if admission_status not in {"unadmitted", "admitted"}:
            raise OperationManifestError(
                "admission_status must be unadmitted or admitted"
            )
        admission_ref = data.get("admission_ref", "")
        if not isinstance(admission_ref, str):
            raise OperationManifestError("admission_ref must be text")
        admission_ref = admission_ref.strip()
        if admission_status == "admitted" and not admission_ref:
            raise OperationManifestError(
                "admitted manifests require a principal admission_ref"
            )

        binding = _mapping(data.get("binding"), "binding")
        required_binding = (
            "source_commit",
            "runtime_id",
            "store_id",
            "lockfile_sha256",
            "account_id",
            "allowance_owner",
            "allowance_remaining_http_attempts",
            "session_start",
            "session_stop",
        )
        for key in required_binding:
            if key not in binding:
                raise OperationManifestError(f"binding.{key} is required")
        unknown_binding = sorted(set(binding) - set(required_binding))
        if unknown_binding:
            raise OperationManifestError(
                "unknown binding fields: " + ", ".join(unknown_binding)
            )
        source_commit = _required_text(binding, "source_commit", "binding").lower()
        if not _HEX_40.fullmatch(source_commit):
            raise OperationManifestError("binding.source_commit must be a 40-digit SHA")
        lockfile_hash = _required_text(binding, "lockfile_sha256", "binding").lower()
        if not _HEX_64.fullmatch(lockfile_hash):
            raise OperationManifestError(
                "binding.lockfile_sha256 must be a 64-digit SHA-256"
            )
        for key in ("runtime_id", "store_id", "account_id", "allowance_owner"):
            _required_text(binding, key, "binding")
        allowance = _strict_int(
            binding.get("allowance_remaining_http_attempts"),
            "binding.allowance_remaining_http_attempts",
        )
        session_start = _parse_aware_datetime(
            binding.get("session_start"), "binding.session_start"
        )
        session_stop = _parse_aware_datetime(
            binding.get("session_stop"), "binding.session_stop"
        )
        if session_stop <= session_start:
            raise OperationManifestError("session_stop must be later than session_start")

        dimensions_raw = _mapping(data.get("dimensions"), "dimensions")
        dimension_names = (
            LIFECYCLE_DIMENSIONS if operation == "lifecycle" else OBSERVER_DIMENSIONS
        )
        missing_dimensions = sorted(set(dimension_names) - set(dimensions_raw))
        unknown_dimensions = sorted(set(dimensions_raw) - set(dimension_names))
        if missing_dimensions:
            raise OperationManifestError(
                "missing required dimensions: " + ", ".join(missing_dimensions)
            )
        if unknown_dimensions:
            raise OperationManifestError(
                "unknown dimensions: " + ", ".join(unknown_dimensions)
            )
        dimensions = {
            name: _strict_int(dimensions_raw[name], f"dimensions.{name}")
            for name in dimension_names
        }
        if operation == "observer" and dimensions["output_bytes"] < 1:
            raise OperationManifestError("dimensions.output_bytes must be positive")
        if dimensions["stream_connections"] < 1:
            raise OperationManifestError(
                "dimensions.stream_connections must permit the initial connection"
            )
        if dimensions["stream_events"] < 1:
            raise OperationManifestError("dimensions.stream_events must be positive")
        if operation == "observer" and dimensions["receipt_events"] < 1:
            raise OperationManifestError("dimensions.receipt_events must be positive")

        deadlines_raw = _mapping(data.get("deadlines_seconds"), "deadlines_seconds")
        deadline_names = (
            LIFECYCLE_DEADLINES if operation == "lifecycle" else OBSERVER_DEADLINES
        )
        missing_deadlines = sorted(set(deadline_names) - set(deadlines_raw))
        unknown_deadlines = sorted(set(deadlines_raw) - set(deadline_names))
        if missing_deadlines:
            raise OperationManifestError(
                "missing required deadlines: " + ", ".join(missing_deadlines)
            )
        if unknown_deadlines:
            raise OperationManifestError(
                "unknown deadlines: " + ", ".join(unknown_deadlines)
            )
        deadlines = {
            name: _finite_seconds(deadlines_raw[name], f"deadlines_seconds.{name}")
            for name in deadline_names
        }
        if deadlines["request_timeout_seconds"] > 30:
            raise OperationManifestError(
                "request_timeout_seconds cannot exceed 30 seconds"
            )

        scope = _mapping(data.get("scope"), "scope")
        if operation == "lifecycle":
            allowed_scope = {
                "symbol",
                "quantity",
                "max_entry_notional",
                "data_source",
            }
            if set(scope) != allowed_scope:
                raise OperationManifestError(
                    "lifecycle scope must contain exactly symbol, quantity, "
                    "max_entry_notional, and data_source"
                )
            if str(scope.get("symbol", "")).strip().upper() != "SPY":
                raise OperationManifestError("lifecycle scope is fixed to SPY")
            if isinstance(scope.get("quantity"), bool) or scope.get("quantity") != 1:
                raise OperationManifestError("lifecycle scope is fixed to one share")
            notional = scope.get("max_entry_notional")
            if (
                isinstance(notional, bool)
                or not isinstance(notional, (int, float))
                or not math.isfinite(float(notional))
                or not 0 < float(notional) <= 1000
            ):
                raise OperationManifestError(
                    "lifecycle max_entry_notional must be in (0, 1000]"
                )
            if str(scope.get("data_source", "")).strip().lower() != "alpaca":
                raise OperationManifestError("lifecycle market data is Alpaca-only")
            if dimensions["order_page_size"] > _MAX_ORDER_PAGE_SIZE:
                raise OperationManifestError(
                    f"order_page_size cannot exceed {_MAX_ORDER_PAGE_SIZE}"
                )
            if dimensions["order_page_size"] < 1 or dimensions["order_pages"] < 1:
                raise OperationManifestError(
                    "order_page_size and order_pages must be positive"
                )
            if dimensions["market_data_pages"] < 1:
                raise OperationManifestError("market_data_pages must be positive")
            for name in (
                "stable_sample_iterations",
                "durable_entry_polls",
                "final_clear_polls",
                "final_clear_invocations",
                "safety_recovery_passes",
                "cancel_verification_polls",
                "exact_lookup_polls",
                "partial_fill_events",
            ):
                if dimensions[name] < 1:
                    raise OperationManifestError(f"dimensions.{name} must be positive")
            if dimensions["eligible_order_ids"] < 3:
                raise OperationManifestError(
                    "eligible_order_ids must cover entry, protective stop, and exit identities"
                )
            if dimensions["cancel_attempts_per_order"] < 1:
                raise OperationManifestError(
                    "cancel_attempts_per_order must permit a first safety cancellation"
                )
            if dimensions["exact_order_lookups_per_order"] < 1:
                raise OperationManifestError(
                    "exact_order_lookups_per_order must permit a first safety lookup"
                )
        else:
            allowed_scope = {"symbols", "market_trend_symbol"}
            if set(scope) != allowed_scope:
                raise OperationManifestError(
                    "observer scope must contain exactly symbols and market_trend_symbol"
                )
            symbols = scope.get("symbols")
            if (
                not isinstance(symbols, list)
                or not symbols
                or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)
            ):
                raise OperationManifestError(
                    "observer scope.symbols must be a non-empty string list"
                )
            normalized = [symbol.strip().upper() for symbol in symbols]
            if len(normalized) != len(set(normalized)):
                raise OperationManifestError("observer scope.symbols must be unique")
            if len(normalized) > dimensions["symbols_per_scan"]:
                raise OperationManifestError(
                    "observer scope.symbols exceeds dimensions.symbols_per_scan"
                )
            if str(scope.get("market_trend_symbol", "")).strip().upper() != "SPY":
                raise OperationManifestError(
                    "observer scope.market_trend_symbol must bind the scanner's SPY baseline"
                )
            if dimensions["scan_cycles"] < len(normalized):
                raise OperationManifestError(
                    "scan_cycles must cover one explicit scan for every scoped symbol"
                )
            duration = (session_stop - session_start).total_seconds()
            if deadlines["session_seconds"] < duration:
                raise OperationManifestError(
                    "session_seconds cannot cover binding.session_start to session_stop"
                )
            required_ticks = math.ceil(duration / _OBSERVER_LOOP_SECONDS) + 1
            if dimensions["scheduler_ticks"] < required_ticks:
                raise OperationManifestError(
                    "scheduler_ticks cannot cover the declared session window"
                )
            if dimensions["clock_reads"] < dimensions["scheduler_ticks"] + 1:
                raise OperationManifestError(
                    "clock_reads must cover startup plus every bounded scheduler tick"
                )
            if deadlines["request_timeout_seconds"] > deadlines["hard_deadline_grace_seconds"]:
                raise OperationManifestError(
                    "request_timeout_seconds cannot exceed hard_deadline_grace_seconds"
                )
            if dimensions["scan_cycles"] < 1:
                raise OperationManifestError("scan_cycles must be positive")
            if dimensions["market_data_pages_per_symbol"] < 1:
                raise OperationManifestError(
                    "market_data_pages_per_symbol must be positive"
                )
            if dimensions["market_trend_pages_per_scan"] < 1:
                raise OperationManifestError(
                    "market_trend_pages_per_scan must cover the SPY market baseline"
                )

        category_raw = _mapping(
            data.get("http_category_limits"), "http_category_limits"
        )
        missing_categories = sorted(set(COMMON_HTTP_CATEGORIES) - set(category_raw))
        unknown_categories = sorted(set(category_raw) - set(COMMON_HTTP_CATEGORIES))
        if missing_categories:
            raise OperationManifestError(
                "missing required HTTP categories: " + ", ".join(missing_categories)
            )
        if unknown_categories:
            raise OperationManifestError(
                "unknown HTTP categories: " + ", ".join(unknown_categories)
            )
        category_limits = {
            name: _strict_int(category_raw[name], f"http_category_limits.{name}")
            for name in COMMON_HTTP_CATEGORIES
        }

        if operation == "lifecycle":
            if category_limits["fmp_read"] != 0:
                raise OperationManifestError(
                    "lifecycle FMP allowance must be zero"
                )
            if category_limits["index_read"] != 0:
                raise OperationManifestError(
                    "lifecycle index-read allowance must be zero"
                )
            if category_limits["account_read"] < 1 or category_limits["clock_read"] < 1:
                raise OperationManifestError(
                    "lifecycle requires explicit account and clock read caps"
                )
            if category_limits["clock_read"] < dimensions["clock_reads"]:
                raise OperationManifestError(
                    "clock_read cap is below the lifecycle clock-read bound"
                )
            if category_limits["entry_submit"] < 1:
                raise OperationManifestError("lifecycle requires one entry submit cap")
            if category_limits["entry_submit"] != 1:
                raise OperationManifestError(
                    "lifecycle entry_submit cap must be exactly one"
                )
            for name in (
                "stop_submit",
                "exit_submit",
                "stop_replace",
                "order_cancel",
                "exact_order_lookup",
            ):
                if category_limits[name] < 1:
                    raise OperationManifestError(
                        f"lifecycle safety category {name} must be positive"
                    )
            min_state_reads = (
                dimensions["stable_sample_iterations"]
                + dimensions["final_clear_polls"]
                * dimensions["final_clear_invocations"]
            )
            for name in ("position_read", "open_order_read"):
                if category_limits[name] < min_state_reads:
                    raise OperationManifestError(
                        f"{name} cap is below the stable-sample/final-clear bound"
                    )
            min_closed_reads = (
                dimensions["final_clear_polls"]
                * dimensions["final_clear_invocations"]
            )
            if category_limits["closed_order_read"] < min_closed_reads:
                raise OperationManifestError(
                    "closed_order_read cap is below the final-clear poll bound"
                )
            if category_limits["market_data_read"] < dimensions["market_data_pages"]:
                raise OperationManifestError(
                    "market_data_read cap is below market_data_pages"
                )
            min_cancel = (
                dimensions["eligible_order_ids"]
                * dimensions["cancel_attempts_per_order"]
            )
            if category_limits["order_cancel"] < min_cancel:
                raise OperationManifestError(
                    "order_cancel cap is below eligible-order cancellation bound"
                )
            min_lookup = (
                dimensions["eligible_order_ids"]
                * dimensions["exact_order_lookups_per_order"]
            )
            if category_limits["exact_order_lookup"] < min_lookup:
                raise OperationManifestError(
                    "exact_order_lookup cap is below eligible-order lookup bound"
                )
            if category_limits["open_order_read"] + category_limits["closed_order_read"] < dimensions["order_pages"]:
                raise OperationManifestError(
                    "order read category caps are below the order_pages bound"
                )
            safety_categories = SAFETY_HTTP_CATEGORIES
        else:
            if category_limits["account_read"] < 1:
                raise OperationManifestError(
                    "observer requires one account read to verify binding.account_id"
                )
            if any(category_limits[name] != 0 for name in ORDER_MUTATION_CATEGORIES):
                raise OperationManifestError(
                    "observer order mutation category caps must all be zero"
                )
            if dimensions["exit_check_cycles"] != 0:
                raise OperationManifestError(
                    "observer exit_check_cycles must be zero; observer mode is scan-only"
                )
            if category_limits["position_read"] != 0 or category_limits["open_order_read"] != 0:
                raise OperationManifestError(
                    "observer position and order reads must be zero in scan-only mode"
                )
            symbols_count = len(scope["symbols"])
            min_market = (
                dimensions["scan_cycles"]
                * (
                    symbols_count * dimensions["market_data_pages_per_symbol"]
                    + dimensions["market_trend_pages_per_scan"]
                )
            )
            if category_limits["market_data_read"] < min_market:
                raise OperationManifestError(
                    "market_data_read cap is below the declared observer scan bound"
                )
            min_fmp = (
                dimensions["scan_cycles"]
                * symbols_count
                * dimensions["fmp_requests_per_symbol"]
            )
            if category_limits["fmp_read"] < min_fmp:
                raise OperationManifestError(
                    "fmp_read cap is below the declared observer scan bound"
                )
            if category_limits["position_read"] < dimensions["exit_check_cycles"]:
                raise OperationManifestError(
                    "position_read cap is below the observer exit-check bound"
                )
            if category_limits["open_order_read"] < dimensions["exit_check_cycles"]:
                raise OperationManifestError(
                    "open_order_read cap is below the observer exit-check bound"
                )
            if category_limits["clock_read"] < dimensions["clock_reads"]:
                raise OperationManifestError(
                    "clock_read cap is below the observer clock-read bound"
                )
            if dimensions["index_requests_per_scan"] != 0 or category_limits["index_read"] != 0:
                raise OperationManifestError(
                    "observer index reads must be zero for explicit-symbol scope"
                )
            safety_categories = frozenset()

        total = sum(category_limits.values())
        reserve = sum(category_limits[name] for name in safety_categories)
        if total <= 0:
            raise OperationManifestError("HTTP category caps must allow at least one request")
        if reserve >= total:
            raise OperationManifestError(
                "safety reserve must be smaller than the total HTTP cap"
            )
        if total > allowance:
            raise OperationManifestError(
                f"derived HTTP total {total} exceeds bound allowance {allowance}"
            )

        return cls(
            operation=operation,
            admission_status=admission_status,
            admission_ref=admission_ref,
            binding=dict(binding),
            scope=dict(scope),
            dimensions=dimensions,
            deadlines_seconds=deadlines,
            http_category_limits=category_limits,
            total_http_attempts=total,
            safety_reserve_http_attempts=reserve,
            allowance_remaining_http_attempts=allowance,
        )

    def require_admitted(self) -> None:
        if self.admission_status != "admitted" or not self.admission_ref:
            raise OperationManifestError(
                f"{self.operation} operation manifest is not admitted"
            )


class OperationBudget:
    """Thread-safe counters shared by every worker in one admitted operation."""

    def __init__(self, manifest: OperationManifest):
        manifest.require_admitted()
        self.manifest = manifest
        self._lock = threading.RLock()
        self._http_attempts = 0
        self._http_categories = {name: 0 for name in COMMON_HTTP_CATEGORIES}
        self._reserve_remaining = manifest.safety_reserve_http_attempts
        self._dimension_counts = {name: 0 for name in manifest.dimensions}
        self._orders: set[str] = set()
        self._replacement_orders: set[str] = set()
        self._order_attempts: dict[tuple[str, str], int] = {}
        self._client_order_aliases: dict[str, str] = {}
        self._symbol_dimension_counts: dict[tuple[str, str], int] = {}
        self._pending_observation_denials: list[tuple[str, dict[str, str]]] = []
        self._evidence_complete = True
        self._incomplete_reason = ""
        self._denials = 0
        self._started_monotonic = time.monotonic()

    def _deny(self, resource: str, detail: str) -> None:
        with self._lock:
            self._evidence_complete = False
            self._denials += 1
            if not self._incomplete_reason:
                self._incomplete_reason = f"{resource}: {detail}"
            if resource != "output_bytes":
                self._pending_observation_denials.append(
                    ("operation_cap_denied", {"resource": resource, "detail": detail})
                )
        raise OperationCapExceeded(f"{resource} cap exceeded: {detail}")

    def consume(self, dimension: str, amount: int = 1) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("dimension amount must be a positive integer")
        with self._lock:
            self.enforce_operation_window()
            if dimension not in self._dimension_counts:
                self._deny(dimension, "not declared by the manifest")
            current = self._dimension_counts[dimension]
            limit = self.manifest.dimensions[dimension]
            if current + amount > limit:
                self._deny(dimension, f"{current + amount} would exceed {limit}")
            self._dimension_counts[dimension] = current + amount
            return self._dimension_counts[dimension]

    def consume_symbol_dimension(
        self,
        dimension: str,
        symbol: str,
        amount: int = 1,
        *,
        multiplier: int = 1,
    ) -> int:
        """Consume one symbol's allowance across bounded repeated scan cycles."""
        normalized = str(symbol).strip().upper()
        if not normalized:
            self._deny(dimension, "symbol identity is empty")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("symbol dimension amount must be a positive integer")
        if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
            raise ValueError("symbol dimension multiplier must be a positive integer")
        with self._lock:
            self.enforce_operation_window()
            if dimension not in self.manifest.dimensions:
                self._deny(dimension, "not declared by the manifest")
            key = (dimension, normalized)
            current = self._symbol_dimension_counts.get(key, 0)
            limit = self.manifest.dimensions[dimension] * multiplier
            if current + amount > limit:
                self._deny(
                    dimension,
                    f"{normalized} would reach {current + amount}, exceeding scaled cap {limit}",
                )
            self._symbol_dimension_counts[key] = current + amount
            return self._symbol_dimension_counts[key]

    def consume_scaled(
        self, dimension: str, *, multiplier: int, amount: int = 1
    ) -> int:
        """Consume a per-scan cap across a manifest-bounded number of scans."""
        if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
            raise ValueError("dimension multiplier must be a positive integer")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError("dimension amount must be a positive integer")
        with self._lock:
            self.enforce_operation_window()
            if dimension not in self.manifest.dimensions:
                self._deny(dimension, "not declared by the manifest")
            current = self._dimension_counts[dimension]
            limit = self.manifest.dimensions[dimension] * multiplier
            if current + amount > limit:
                self._deny(
                    dimension,
                    f"{current + amount} would exceed scaled cap {limit}",
                )
            self._dimension_counts[dimension] = current + amount
            return self._dimension_counts[dimension]

    def enforce_deadline(self, name: str, started_monotonic: float | None = None) -> None:
        """Reject work once its manifest-bound monotonic deadline has elapsed."""
        if name not in self.manifest.deadlines_seconds:
            self._deny(name, "deadline is not declared by the manifest")
        started = (
            self._started_monotonic
            if started_monotonic is None
            else float(started_monotonic)
        )
        elapsed = max(0.0, time.monotonic() - started)
        limit = self.manifest.deadlines_seconds[name]
        if elapsed > limit:
            self._deny(name, f"elapsed {elapsed:.3f}s would exceed {limit:.3f}s")

    def reject_incomplete(self, resource: str, detail: str) -> None:
        """Latch a non-retryable evidence gap and stop the current path."""
        with self._lock:
            self._deny(resource, detail)

    def reserve_http(self, category: str) -> int:
        """Reserve one physical HTTP send before invoking the transport."""
        with self._lock:
            self.enforce_operation_window(
                required_seconds=self.manifest.deadlines_seconds[
                    "request_timeout_seconds"
                ],
                resource="request_timeout_seconds",
            )
            if category not in self._http_categories:
                self._deny("http_category", f"unknown category {category}")
            category_limit = self.manifest.http_category_limits[category]
            category_count = self._http_categories[category]
            if category_count >= category_limit:
                self._deny(category, f"{category_count + 1} would exceed {category_limit}")

            safety = category in SAFETY_HTTP_CATEGORIES
            if self._http_attempts >= self.manifest.total_http_attempts:
                self._deny("total_http_attempts", "total HTTP cap reached")
            if safety:
                if self._reserve_remaining <= 0:
                    self._deny("safety_reserve_http_attempts", "safety reserve exhausted")
                self._reserve_remaining -= 1
            else:
                if not self._evidence_complete:
                    self._deny(category, "operation is incomplete; non-safety requests are stopped")
                remaining = self.manifest.total_http_attempts - self._http_attempts
                if remaining <= self._reserve_remaining:
                    self._deny(category, "remaining HTTP attempts are reserved for safety")
            self._http_attempts += 1
            self._http_categories[category] = category_count + 1
            return self._http_attempts

    def enforce_operation_window(
        self,
        *,
        required_seconds: float = 0.0,
        resource: str = "session_window",
    ) -> None:
        """Enforce manifest start/stop timestamps before work or network I/O."""
        start = _parse_aware_datetime(
            self.manifest.binding["session_start"], "binding.session_start"
        )
        stop = _parse_aware_datetime(
            self.manifest.binding["session_stop"], "binding.session_stop"
        )
        if self.manifest.operation == "observer":
            stop += timedelta(
                seconds=self.manifest.deadlines_seconds["hard_deadline_grace_seconds"]
            )
        now = datetime.now().astimezone()
        if now < start:
            self._deny("session_start", "operation started before its bound window")
        remaining = (stop - now).total_seconds()
        if remaining <= 0:
            self._deny("session_stop", "operation hard deadline has elapsed")
        if required_seconds > remaining:
            self._deny(
                resource,
                f"only {remaining:.3f}s remain for a {required_seconds:.3f}s bounded action",
            )

    def admit_order_id(self, order_id: str, *, replacement: bool = False) -> bool:
        normalized = str(order_id).strip()
        with self._lock:
            if not normalized:
                self._deny("eligible_order_ids", "order identity is empty")
            if normalized in self._orders:
                return False
            if len(self._orders) >= self.manifest.dimensions["eligible_order_ids"]:
                self._deny(
                    "eligible_order_ids",
                    f"new order identity would exceed {self.manifest.dimensions['eligible_order_ids']}",
                )
            if replacement and (
                len(self._replacement_orders)
                >= self.manifest.dimensions["replacement_chain_length"]
            ):
                self._deny(
                    "replacement_chain_length",
                    "replacement identity would exceed the declared chain length",
                )
            self._orders.add(normalized)
            if replacement:
                self._replacement_orders.add(normalized)
            return True

    def order_id_is_known(self, order_id: str) -> bool:
        with self._lock:
            return str(order_id).strip() in self._orders

    def consume_order_attempt(self, action: str, order_id: str) -> int:
        normalized = str(order_id).strip()
        action_key = str(action).strip().lower()
        dimension = {
            "cancel": "cancel_attempts_per_order",
            "lookup": "exact_order_lookups_per_order",
        }.get(action_key)
        if dimension is None:
            raise ValueError("order action must be cancel or lookup")
        with self._lock:
            if action_key == "lookup":
                normalized = self._client_order_aliases.get(normalized, normalized)
            if not normalized:
                self._deny(dimension, "order identity is empty")
            if normalized not in self._orders:
                self.admit_order_id(normalized)
            key = (action_key, normalized)
            current = self._order_attempts.get(key, 0)
            limit = self.manifest.dimensions[dimension]
            if current >= limit:
                self._deny(
                    dimension,
                    f"order {normalized} would exceed {limit} {action_key} attempts",
                )
            self._order_attempts[key] = current + 1
            return self._order_attempts[key]

    def consume_client_order_lookup(self, client_order_id: str) -> int:
        """Count a recovery lookup by client identity before its broker id exists."""
        normalized = str(client_order_id).strip()
        with self._lock:
            if not normalized:
                self._deny("exact_order_lookups_per_order", "client order identity is empty")
            order_id = self._client_order_aliases.get(normalized)
            key = ("lookup", order_id) if order_id else ("lookup_client", normalized)
            current = self._order_attempts.get(key, 0)
            limit = self.manifest.dimensions["exact_order_lookups_per_order"]
            if current >= limit:
                self._deny(
                    "exact_order_lookups_per_order",
                    f"client order {normalized} would exceed {limit} lookup attempts",
                )
            self._order_attempts[key] = current + 1
            return self._order_attempts[key]

    def link_client_order_identity(self, client_order_id: str, order_id: str) -> None:
        """Unify client-id recovery and broker-id lookups for one verified order."""
        client_identity = str(client_order_id).strip()
        broker_identity = str(order_id).strip()
        with self._lock:
            if not client_identity or not broker_identity:
                self._deny("eligible_order_ids", "order identity link is incomplete")
            existing = self._client_order_aliases.get(client_identity)
            if existing and existing != broker_identity:
                self._deny("eligible_order_ids", "client order identity changed broker order")
            self.admit_order_id(broker_identity)
            self._client_order_aliases[client_identity] = broker_identity
            client_key = ("lookup_client", client_identity)
            pending = self._order_attempts.pop(client_key, 0)
            if pending:
                lookup_key = ("lookup", broker_identity)
                combined = self._order_attempts.get(lookup_key, 0) + pending
                limit = self.manifest.dimensions["exact_order_lookups_per_order"]
                if combined > limit:
                    self._deny(
                        "exact_order_lookups_per_order",
                        f"verified order {broker_identity} already exceeds {limit} lookup attempts",
                    )
                self._order_attempts[lookup_key] = combined

    def consume_output(self, amount: int) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("output amount must be a non-negative byte count")
        with self._lock:
            self.enforce_operation_window()
            if "output_bytes" not in self._dimension_counts:
                self._deny("output_bytes", "not declared by the manifest")
            current = self._dimension_counts["output_bytes"]
            limit = self.manifest.dimensions["output_bytes"]
            if current + amount > limit:
                self._deny("output_bytes", f"{current + amount} would exceed {limit}")
            self._dimension_counts["output_bytes"] = current + amount
            return self._dimension_counts["output_bytes"]

    def mark_incomplete(self, reason: str) -> None:
        with self._lock:
            self._evidence_complete = False
            if not self._incomplete_reason:
                self._incomplete_reason = str(reason)

    def ensure_complete(self) -> None:
        """Stop an operation after any earlier resource or evidence denial."""
        with self._lock:
            if not self._evidence_complete:
                raise OperationCapExceeded(
                    "operation is already incomplete: " + self._incomplete_reason
                )

    def snapshot(self) -> dict[str, Any]:
        self.flush_observation_denials()
        with self._lock:
            return {
                "operation": self.manifest.operation,
                "manifest_status": self.manifest.admission_status,
                "evidence_complete": self._evidence_complete,
                "incomplete_reason": self._incomplete_reason,
                "http_attempts": self._http_attempts,
                "http_attempt_cap": self.manifest.total_http_attempts,
                "http_category_attempts": dict(self._http_categories),
                "safety_reserve_initial": self.manifest.safety_reserve_http_attempts,
                "safety_reserve_remaining": self._reserve_remaining,
                "dimension_counts": dict(self._dimension_counts),
                "symbol_dimension_counts": {
                    f"{dimension}:{symbol}": count
                    for (dimension, symbol), count in self._symbol_dimension_counts.items()
                },
                "eligible_order_ids": len(self._orders),
                "replacement_order_ids": len(self._replacement_orders),
                "denials": self._denials,
            }

    def flush_observation_denials(self) -> None:
        """Publish cap denials only after releasing the operation-budget lock."""
        observation = current_scheduler_observation()
        if observation is None:
            return
        with self._lock:
            pending = self._pending_observation_denials
            self._pending_observation_denials = []
        for code, details in pending:
            observation.record_resource_denial(code, details)


@contextmanager
def activate_operation_budget(budget: OperationBudget) -> Iterator[None]:
    """Expose one operation budget process-wide, including worker threads."""
    global _ACTIVE_BUDGET
    with _ACTIVE_LOCK:
        if _ACTIVE_BUDGET is not None:
            raise RuntimeError("An operation budget is already active")
        _ACTIVE_BUDGET = budget
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            if _ACTIVE_BUDGET is budget:
                _ACTIVE_BUDGET = None


def current_operation_budget() -> OperationBudget | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_BUDGET


_MARKET_TREND_DATA_REQUEST = threading.local()
_OUTPUT_REDIRECT_LOCK = threading.RLock()


class _OutputCapState:
    def __init__(self) -> None:
        self.exhausted = False


class _BudgetedTextOutput:
    """Charge UTF-8 bytes before allowing text onto a process output stream."""

    def __init__(self, stream: Any, budget: OperationBudget, state: _OutputCapState):
        self._stream = stream
        self._budget = budget
        self._state = state

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._stream, "errors", None) or "strict"

    def write(self, value: str) -> int:
        text = str(value)
        if self._state.exhausted:
            return len(text)
        try:
            self._budget.consume_output(len(text.encode("utf-8")))
        except OperationCapExceeded:
            self._state.exhausted = True
            return len(text)
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def writable(self) -> bool:
        return True


@contextmanager
def budgeted_standard_output(budget: OperationBudget) -> Iterator[None]:
    """Bound stdout and stderr bytes while an admitted operation is running."""
    with _OUTPUT_REDIRECT_LOCK:
        previous_stdout = sys.stdout
        previous_stderr = sys.stderr
        state = _OutputCapState()
        sys.stdout = _BudgetedTextOutput(previous_stdout, budget, state)  # type: ignore[assignment]
        sys.stderr = _BudgetedTextOutput(previous_stderr, budget, state)  # type: ignore[assignment]
        try:
            yield
        finally:
            sys.stdout = previous_stdout
            sys.stderr = previous_stderr


@contextmanager
def market_trend_data_request() -> Iterator[None]:
    """Tag the one benchmark fetch so its physical pages use the trend cap."""
    previous = getattr(_MARKET_TREND_DATA_REQUEST, "active", False)
    _MARKET_TREND_DATA_REQUEST.active = True
    try:
        yield
    finally:
        _MARKET_TREND_DATA_REQUEST.active = previous


def is_market_trend_data_request() -> bool:
    """Return whether the current thread is fetching the observer benchmark."""
    return bool(getattr(_MARKET_TREND_DATA_REQUEST, "active", False))


def consume_operation_dimension(dimension: str, amount: int = 1) -> int | None:
    """Consume a declaration only while an operation manifest is active."""
    budget = current_operation_budget()
    return None if budget is None else budget.consume(dimension, amount)


def validate_runtime_binding(manifest: OperationManifest, project_root: str | Path) -> None:
    """Bind an admitted contract to this clean source/runtime/store environment."""
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        source_commit = result.stdout.strip().lower()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationManifestError(
            f"could not verify clean source identity: {type(exc).__name__}"
        ) from exc
    if source_commit != str(manifest.binding["source_commit"]).lower():
        raise OperationManifestError("manifest source_commit does not match this checkout")
    if status.stdout.strip():
        raise OperationManifestError("operation source checkout must be clean")

    lockfile = root / "requirements-lock.txt"
    try:
        actual_lock_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    except OSError as exc:
        raise OperationManifestError("could not read requirements-lock.txt") from exc
    if actual_lock_hash != str(manifest.binding["lockfile_sha256"]).lower():
        raise OperationManifestError("manifest lockfile hash does not match requirements-lock.txt")

    expected_env = {
        "PAPER_OPERATION_RUNTIME_ID": "runtime_id",
        "PAPER_OPERATION_STORE_ID": "store_id",
        "PAPER_OPERATION_ALLOWANCE_OWNER": "allowance_owner",
    }
    for env_name, binding_name in expected_env.items():
        actual = os.environ.get(env_name, "").strip()
        expected = str(manifest.binding[binding_name]).strip()
        if not actual or actual != expected:
            raise OperationManifestError(
                f"{env_name} is missing or does not match binding.{binding_name}"
            )

    allowance_raw = os.environ.get("PAPER_OPERATION_ALLOWANCE_REMAINING_HTTP_ATTEMPTS", "")
    try:
        actual_allowance = int(allowance_raw)
    except (TypeError, ValueError) as exc:
        raise OperationManifestError(
            "PAPER_OPERATION_ALLOWANCE_REMAINING_HTTP_ATTEMPTS is required"
        ) from exc
    if actual_allowance != manifest.allowance_remaining_http_attempts:
        raise OperationManifestError(
            "remaining HTTP allowance does not match the bound allowance owner"
        )


def enforce_manifest_session_window(
    manifest: OperationManifest,
    *,
    now: datetime | None = None,
) -> None:
    """Reject operations started outside the explicitly bound session window."""
    current = now or datetime.now().astimezone()
    start = _parse_aware_datetime(manifest.binding["session_start"], "binding.session_start")
    stop = _parse_aware_datetime(manifest.binding["session_stop"], "binding.session_stop")
    if current < start or current >= stop:
        raise OperationManifestError("current time is outside the manifest session window")


def load_operation_manifest(
    path: str | Path,
    *,
    expected_operation: str,
    require_admitted: bool = True,
) -> OperationManifest:
    """Load a local manifest and reject missing or mismatched operation identity."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationManifestError(
            f"could not load operation manifest: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise OperationManifestError("operation manifest root must be an object")
    manifest = OperationManifest.from_mapping(
        payload, expected_operation=expected_operation
    )
    if require_admitted:
        manifest.require_admitted()
    return manifest
