"""Test-only comparison rules for the early #106 replacement parity slice.

Policy identity, recorded facts, decision fields, and fixed action intentions
compare exactly. Decimal representation scale is ignored, but there is no
numeric epsilon. Timestamp offsets compare by exact UTC instant, with no time
tolerance. Nested mappings and sequences are rejected until their fields are
flattened into an explicit comparison contract. Fill price, fees, valuation
time, and pending state are reported as execution variances; that classification
does not accept full #106 behavior.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class ParityDisposition(StrEnum):
    MATCHED = "matched"
    MATCHED_WITH_EXECUTION_VARIANCE = "matched_with_execution_variance"
    INCOMPLETE = "incomplete"
    MISMATCH = "mismatch"
    INCOMPATIBLE_POLICY = "incompatible_policy"


@dataclass(frozen=True, slots=True)
class ParityComparison:
    disposition: ParityDisposition
    matched_fields: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    mismatches: tuple[str, ...] = ()
    incompatible_fields: tuple[str, ...] = ()
    execution_variances: tuple[str, ...] = ()


_EXACT_GROUPS = ("policy_identity", "facts", "decision", "intent")
_EXECUTION_VARIANCE_FIELDS = frozenset(
    {"fill_price", "fees", "valuation_time", "pending_state"}
)
_TIMESTAMP_FIELDS = frozenset({"as_of_cutoff_at", "account_valuation_at", "valuation_time"})
_MISSING = object()


def _is_nested(value: object) -> bool:
    return isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    )


def _equal(left: Any, right: Any) -> bool:
    if _is_nested(left) or _is_nested(right):
        raise TypeError("nested parity values are unsupported; flatten them into declared fields")
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    numeric = (Decimal, int, float)
    if isinstance(left, numeric) and isinstance(right, numeric):
        try:
            left_value = Decimal(str(left))
            right_value = Decimal(str(right))
        except (InvalidOperation, ValueError):
            return False
        return left_value.is_finite() and right_value.is_finite() and left_value == right_value
    return left == right


def _same_value(field: str, left: Any, right: Any) -> bool:
    if field in _TIMESTAMP_FIELDS and isinstance(left, str) and isinstance(right, str):
        try:
            left_time = datetime.fromisoformat(left.replace("Z", "+00:00"))
            right_time = datetime.fromisoformat(right.replace("Z", "+00:00"))
        except ValueError:
            return left == right
        if left_time.utcoffset() is not None and right_time.utcoffset() is not None:
            return left_time.astimezone(timezone.utc) == right_time.astimezone(timezone.utc)
    return _equal(left, right)


def compare_replacement_cases(
    historical: Mapping[str, object],
    paper: Mapping[str, object],
) -> ParityComparison:
    """Compare only declared fields; missing evidence stays unknown.

    Fixed inputs, decision identity, and durable intention fields require exact
    semantic equality. A known policy identity difference makes the cases
    incomparable. Only the listed execution fields are surfaced as variances;
    every other concrete difference remains a mismatch.
    """
    matched: list[str] = []
    unknown: list[str] = []
    mismatches: list[str] = []
    incompatible: list[str] = []
    execution_variances: list[str] = []

    for group in _EXACT_GROUPS:
        expected_group = historical.get(group)
        observed_group = paper.get(group)
        if not isinstance(expected_group, Mapping) or not isinstance(observed_group, Mapping):
            unknown.append(group)
            continue
        if not expected_group and not observed_group:
            unknown.append(group)
            continue
        for field, expected_value in expected_group.items():
            label = f"{group}.{field}"
            observed_value = observed_group.get(field, _MISSING)
            if expected_value is None or observed_value is _MISSING or observed_value is None:
                unknown.append(label)
            elif _same_value(field, expected_value, observed_value):
                matched.append(label)
            elif group == "policy_identity":
                incompatible.append(label)
            else:
                mismatches.append(label)
        for field in observed_group:
            if field not in expected_group:
                unknown.append(f"{group}.{field}")

    expected_execution = historical.get("execution")
    observed_execution = paper.get("execution")
    if not isinstance(expected_execution, Mapping) or not isinstance(observed_execution, Mapping):
        unknown.append("execution")
    elif not expected_execution and not observed_execution:
        unknown.append("execution")
    else:
        for field, expected_value in expected_execution.items():
            label = f"execution.{field}"
            observed_value = observed_execution.get(field, _MISSING)
            if expected_value is None or observed_value is _MISSING or observed_value is None:
                unknown.append(label)
            elif _same_value(field, expected_value, observed_value):
                matched.append(label)
            elif field in _EXECUTION_VARIANCE_FIELDS:
                execution_variances.append(label)
            else:
                mismatches.append(label)
        for field in observed_execution:
            if field not in expected_execution:
                unknown.append(f"execution.{field}")

    if incompatible:
        disposition = ParityDisposition.INCOMPATIBLE_POLICY
    elif mismatches:
        disposition = ParityDisposition.MISMATCH
    elif unknown:
        disposition = ParityDisposition.INCOMPLETE
    elif execution_variances:
        disposition = ParityDisposition.MATCHED_WITH_EXECUTION_VARIANCE
    else:
        disposition = ParityDisposition.MATCHED

    return ParityComparison(
        disposition=disposition,
        matched_fields=tuple(sorted(matched)),
        unknowns=tuple(sorted(unknown)),
        mismatches=tuple(sorted(mismatches)),
        incompatible_fields=tuple(sorted(incompatible)),
        execution_variances=tuple(sorted(execution_variances)),
    )
