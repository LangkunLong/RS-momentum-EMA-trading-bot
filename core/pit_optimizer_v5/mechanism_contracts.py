"""Pure, closed contracts for one V5 mechanism-evidence slice.

The mechanism extension is deliberately smaller than the campaign protocol.  It
describes one registered V3 policy method and carries identities into later
observation/report work without importing candidate code or starting execution.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

from .contracts import HypothesisV5, canonical_json_bytes_v5


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")

MECHANISM_SCHEMA_VERSION_V1 = 1
MECHANISM_MAX_CANONICAL_JSON_BYTES_V1 = 256 * 1024
MECHANISM_MAX_DECIMAL_DIGITS_V1 = 64
MECHANISM_MAX_DECIMAL_EXPONENT_V1 = 64
MECHANISM_MAX_DECIMAL_TEXT_BYTES_V1 = 96
MECHANISM_MAX_CPU_SECONDS_V1 = Decimal("86400")
MECHANISM_TARGET_METHODS_V1 = ("evaluate_exit",)
MECHANISM_ALLOWED_SYMBOLS_V1 = ("core.strategy_policy.v3.exit.evaluate_exit",)
MECHANISM_INPUT_UNITS_V1 = MappingProxyType({"features.atr_20_fraction": "fraction"})
MECHANISM_RECIPE_IDS_V1 = ("evaluate_exit_atr20_fraction_v1",)
MECHANISM_METRIC_UNITS_V1 = MappingProxyType(
    {
        "exit.decision_changed_count": "count",
        "exit.protected_control_unchanged_count": "count",
        "evaluator.exit_attribution_count": "count",
    }
)
MECHANISM_DIRECTIONS_V1 = ("increase", "decrease", "unchanged")
MECHANISM_DENOMINATORS_V1 = (
    "relevant_cases",
    "control_cases",
    "all_cases",
    "evaluator_cases",
)
MECHANISM_AGGREGATIONS_V1 = ("paired_case_delta",)
MECHANISM_CONSEQUENCE_AGGREGATIONS_V1 = ("sum_contexts_v1",)
MECHANISM_PROVENANCES_V1 = ("synthetic_offline", "authorized_discovery")
MECHANISM_EVALUATOR_STAGES_V1 = ("quick", "discovery")
MECHANISM_DIAGNOSTIC_SECTIONS_V1 = ("exit_attribution",)
MECHANISM_EXIT_REASON_IDS_V1 = (
    "ma_violation",
    "policy_exit",
    "take_profit_scale_out",
    "time_stop",
)
MECHANISM_CONTROL_IDS_V1 = ("protected_next_stop_price",)
MECHANISM_DISCONFIRMING_IDS_V1 = (
    "protected_control_changed",
    "decision_unchanged_when_applicable",
    "diagnostic_metric_unavailable",
)
MECHANISM_UNAVAILABLE_REASONS_V1 = (
    "evaluator_metric_missing",
    "execution_failed",
    "mixed_context",
    "not_run",
    "unsupported_case",
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical JSON contains a duplicate field")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _text(value: object, label: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value or len(value) > max_length:
        raise ValueError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str, *, pattern: re.Pattern[str] = _TEXT_RE) -> str:
    text = _text(value, label, max_length=128)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{label} is invalid")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    decimal_tuple = value.as_tuple()
    coefficient_digits = len(decimal_tuple.digits)
    if coefficient_digits > MECHANISM_MAX_DECIMAL_DIGITS_V1:
        raise ValueError(f"{label} exceeds supported numeric precision")
    exponent = decimal_tuple.exponent
    if abs(exponent) > MECHANISM_MAX_DECIMAL_EXPONENT_V1:
        raise ValueError(f"{label} exceeds supported numeric exponent")
    if coefficient_digits + max(exponent, 0) > MECHANISM_MAX_DECIMAL_DIGITS_V1:
        raise ValueError(f"{label} exceeds supported numeric precision")
    if nonnegative and value < 0:
        raise ValueError(f"{label} must be nonnegative")
    return value


def _decimal_from_primitive(value: object, label: str) -> Decimal:
    # Decimal values use canonical JSON strings through canonical_json_bytes_v5.
    # Numeric JSON values are deliberately rejected so that a float cannot enter
    # a digest-bearing contract through a permissive decoder.
    if type(value) is not str:
        raise ValueError(f"{label} must be canonical numeric text")
    try:
        text_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be canonical numeric text") from exc
    if text_size > MECHANISM_MAX_DECIMAL_TEXT_BYTES_V1:
        raise ValueError(f"{label} must be canonical numeric text")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"{label} must be canonical numeric text") from exc
    return _decimal(parsed, label)


def _count_decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    number = _decimal(value, label, nonnegative=nonnegative)
    decimal_tuple = number.as_tuple()
    exponent = decimal_tuple.exponent
    if exponent < 0:
        fractional_digits = -exponent
        trailing_digits = decimal_tuple.digits[-fractional_digits:]
        if any(digit != 0 for digit in trailing_digits):
            raise ValueError(f"{label} must be integral")
    return number


def _count_integer(value: object, label: str, *, nonnegative: bool = False) -> int:
    number = _count_decimal(value, label, nonnegative=nonnegative)
    if number != Decimal(int(number)):
        raise ValueError(f"{label} must be integral")
    return int(number)


def _count(value: object, label: str, *, positive: bool = False, maximum: int | None = None) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be a bounded {'positive' if positive else 'nonnegative'} integer")
    return value


def _tuple(value: object, label: str, *, maximum: int = 256) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    return value


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    return value


def _require_fields(value: object, expected: set[str], label: str) -> dict[str, object]:
    mapping = _require_mapping(value, label)
    actual = set(mapping)
    if actual != expected:
        if actual - expected:
            raise ValueError(f"{label} has unknown fields")
        raise ValueError(f"{label} is missing fields")
    return mapping


def _canonical_decode(cls: type[_CanonicalMechanismV1], raw: str) -> _CanonicalMechanismV1:
    if type(raw) is not str:
        raise ValueError("canonical JSON must be a string")
    try:
        if len(raw.encode("utf-8")) > MECHANISM_MAX_CANONICAL_JSON_BYTES_V1:
            raise ValueError("canonical JSON exceeds the mechanism contract size bound")
    except UnicodeEncodeError as exc:
        raise ValueError("canonical JSON is invalid") from exc
    try:
        primitive = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("canonical JSON is invalid") from exc
    except ValueError as exc:
        if "duplicate field" in str(exc):
            raise
        raise ValueError("canonical JSON is invalid") from exc
    value = cls.from_primitive(primitive)
    if value.to_canonical_json() != raw:
        raise ValueError("canonical JSON encoding is not canonical")
    return value


class _CanonicalMechanismV1:
    def to_primitive(self) -> dict[str, object]:
        # canonical_json_bytes_v5 is the existing V5 identity leaf.  Using it
        # here keeps Decimal and tuple encoding identical to legacy identities.
        primitive = json.loads(canonical_json_bytes_v5(self).decode("utf-8"))
        if type(primitive) is not dict:
            raise ValueError("mechanism contract must encode as an object")
        return primitive

    def to_canonical_json(self) -> str:
        return canonical_json_bytes_v5(self).decode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: str) -> _CanonicalMechanismV1:
        return _canonical_decode(cls, raw)


@dataclass(frozen=True, slots=True)
class MechanismPredicateV1(_CanonicalMechanismV1):
    """One closed predicate over the registered nullable fraction input."""

    field: Literal["features.atr_20_fraction"]
    operator: Literal["is_present", "is_missing", "gt", "gte", "lt", "lte", "eq"]
    value: Decimal | None

    def __post_init__(self) -> None:
        if self.field not in MECHANISM_INPUT_UNITS_V1:
            raise ValueError("predicate field is unsupported")
        if self.operator not in {"is_present", "is_missing", "gt", "gte", "lt", "lte", "eq"}:
            raise ValueError("predicate operator is unsupported")
        if self.operator in {"is_present", "is_missing"}:
            if self.value is not None:
                raise ValueError("presence predicate cannot carry a comparison value")
        else:
            _decimal(self.value, "predicate value")
            assert self.value is not None
            if self.value < 0 or self.value > 1:
                raise ValueError("predicate fraction value is out of bounds")

    def matches(self, observed: Decimal | None) -> bool:
        if observed is not None:
            number = _decimal(observed, "observed predicate value")
            if number < 0 or number > 1:
                raise ValueError("observed predicate fraction is out of bounds")
        else:
            number = None
        if self.operator == "is_present":
            return observed is not None
        if self.operator == "is_missing":
            return observed is None
        if number is None:
            return False
        assert self.value is not None
        return {
            "gt": number > self.value,
            "gte": number >= self.value,
            "lt": number < self.value,
            "lte": number <= self.value,
            "eq": number == self.value,
        }[self.operator]

    @classmethod
    def from_primitive(cls, value: object) -> MechanismPredicateV1:
        raw = _require_fields(value, {"field", "operator", "value"}, "predicate")
        parsed = None if raw["value"] is None else _decimal_from_primitive(raw["value"], "predicate value")
        return cls(field=raw["field"], operator=raw["operator"], value=parsed)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MechanismDiagnosticSelectorV1(_CanonicalMechanismV1):
    """Selector for a named count already present in EvaluationReportV5."""

    section: Literal["exit_attribution"]
    metric_id: str

    def __post_init__(self) -> None:
        if self.section not in MECHANISM_DIAGNOSTIC_SECTIONS_V1:
            raise ValueError("diagnostic section is unsupported")
        if self.metric_id not in MECHANISM_EXIT_REASON_IDS_V1:
            raise ValueError("diagnostic metric ID is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismDiagnosticSelectorV1:
        raw = _require_fields(value, {"section", "metric_id"}, "diagnostic selector")
        return cls(section=raw["section"], metric_id=raw["metric_id"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MechanismControlV1(_CanonicalMechanismV1):
    control_id: Literal["protected_next_stop_price"]
    observed_field: Literal["next_stop_price"] = "next_stop_price"
    metric_id: Literal["exit.protected_control_unchanged_count"] = "exit.protected_control_unchanged_count"

    def __post_init__(self) -> None:
        if self.control_id not in MECHANISM_CONTROL_IDS_V1:
            raise ValueError("control ID is unsupported")
        if self.observed_field != "next_stop_price":
            raise ValueError("control observed field is unsupported")
        if self.metric_id != "exit.protected_control_unchanged_count":
            raise ValueError("control metric ID is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismControlV1:
        raw = _require_fields(value, {"control_id", "observed_field", "metric_id"}, "control")
        return cls(
            control_id=raw["control_id"],  # type: ignore[arg-type]
            observed_field=raw["observed_field"],  # type: ignore[arg-type]
            metric_id=raw["metric_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MechanismMetricSpecV1(_CanonicalMechanismV1):
    metric_id: str
    unit: Literal["count"]
    direction: Literal["increase", "decrease", "unchanged"]
    tolerance: Decimal
    denominator: Literal["relevant_cases", "control_cases", "all_cases", "evaluator_cases"]
    selector: MechanismDiagnosticSelectorV1 | None = None

    def __post_init__(self) -> None:
        expected_unit = MECHANISM_METRIC_UNITS_V1.get(self.metric_id)
        if expected_unit is None:
            raise ValueError("metric ID is unsupported")
        if self.unit != expected_unit:
            raise ValueError("metric unit is invalid")
        if self.direction not in MECHANISM_DIRECTIONS_V1:
            raise ValueError("metric direction is unsupported")
        _count_decimal(self.tolerance, "metric tolerance", nonnegative=True)
        if self.denominator not in MECHANISM_DENOMINATORS_V1:
            raise ValueError("metric denominator is unsupported")
        if self.metric_id == "evaluator.exit_attribution_count":
            if type(self.selector) is not MechanismDiagnosticSelectorV1:
                raise ValueError("evaluator metric requires a registered selector")
            if self.denominator != "evaluator_cases":
                raise ValueError("evaluator metric requires evaluator_cases denominator")
        else:
            if self.selector is not None:
                raise ValueError("non-evaluator metric cannot carry a diagnostic selector")
            if self.denominator == "evaluator_cases":
                raise ValueError("paired metric cannot use evaluator_cases denominator")
            if self.metric_id == "exit.decision_changed_count" and self.denominator != "relevant_cases":
                raise ValueError("conditional decision metric requires relevant_cases denominator")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismMetricSpecV1:
        raw = _require_fields(
            value,
            {"metric_id", "unit", "direction", "tolerance", "denominator", "selector"},
            "metric specification",
        )
        selector = None if raw["selector"] is None else MechanismDiagnosticSelectorV1.from_primitive(raw["selector"])
        return cls(
            metric_id=raw["metric_id"],  # type: ignore[arg-type]
            unit=raw["unit"],  # type: ignore[arg-type]
            direction=raw["direction"],  # type: ignore[arg-type]
            tolerance=_decimal_from_primitive(raw["tolerance"], "metric tolerance"),
            denominator=raw["denominator"],  # type: ignore[arg-type]
            selector=selector,
        )


@dataclass(frozen=True, slots=True)
class MechanismRecipeV1(_CanonicalMechanismV1):
    recipe_id: Literal["evaluate_exit_atr20_fraction_v1"]
    input_field: Literal["features.atr_20_fraction"]
    input_values: tuple[Decimal | None, ...]
    version: Literal[1] = 1

    def __post_init__(self) -> None:
        if self.recipe_id not in MECHANISM_RECIPE_IDS_V1:
            raise ValueError("recipe ID is unsupported")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("recipe version is unsupported")
        if self.input_field not in MECHANISM_INPUT_UNITS_V1:
            raise ValueError("recipe input field is unsupported")
        values = _tuple(self.input_values, "recipe input values", maximum=32)
        if not values:
            raise ValueError("recipe input values are empty")
        normalized: list[Decimal | None] = []
        for item in values:
            if item is None:
                normalized.append(None)
                continue
            number = _decimal(item, "recipe input value")
            if number < 0 or number > 1:
                raise ValueError("recipe fraction input is out of bounds")
            normalized.append(number)
        if len(set(normalized)) != len(normalized):
            raise ValueError("recipe input values must be unique")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismRecipeV1:
        raw = _require_fields(value, {"recipe_id", "input_field", "input_values", "version"}, "recipe")
        raw_values = raw["input_values"]
        if type(raw_values) is not list:
            raise ValueError("recipe input values are invalid")
        values = tuple(
            None if item is None else _decimal_from_primitive(item, "recipe input value") for item in raw_values
        )
        return cls(
            recipe_id=raw["recipe_id"],  # type: ignore[arg-type]
            input_field=raw["input_field"],  # type: ignore[arg-type]
            input_values=values,
            version=raw["version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MechanismDisconfirmingObservationV1(_CanonicalMechanismV1):
    observation_id: Literal[
        "protected_control_changed",
        "decision_unchanged_when_applicable",
        "diagnostic_metric_unavailable",
    ]
    metric_id: str

    def __post_init__(self) -> None:
        if self.observation_id not in MECHANISM_DISCONFIRMING_IDS_V1:
            raise ValueError("disconfirming observation is unsupported")
        expected = {
            "protected_control_changed": "exit.protected_control_unchanged_count",
            "decision_unchanged_when_applicable": "exit.decision_changed_count",
            "diagnostic_metric_unavailable": "evaluator.exit_attribution_count",
        }[self.observation_id]
        if self.metric_id != expected:
            raise ValueError("disconfirming observation metric differs from its registered observation")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismDisconfirmingObservationV1:
        raw = _require_fields(value, {"observation_id", "metric_id"}, "disconfirming observation")
        return cls(observation_id=raw["observation_id"], metric_id=raw["metric_id"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MechanismResourceBudgetV1(_CanonicalMechanismV1):
    max_cases: int
    max_repetitions: int
    timeout_ms: int
    cpu_seconds: Decimal
    memory_mib: int
    output_bytes: int

    def __post_init__(self) -> None:
        _count(self.max_cases, "budget max cases", positive=True, maximum=100_000)
        _count(self.max_repetitions, "budget max repetitions", positive=True, maximum=64)
        _count(self.timeout_ms, "budget timeout", positive=True, maximum=86_400_000)
        _decimal(self.cpu_seconds, "budget CPU seconds", nonnegative=True)
        if self.cpu_seconds == 0:
            raise ValueError("budget CPU seconds must be positive")
        if self.cpu_seconds > MECHANISM_MAX_CPU_SECONDS_V1:
            raise ValueError("budget CPU seconds exceed the mechanism ceiling")
        _count(self.memory_mib, "budget memory", positive=True, maximum=1_048_576)
        _count(self.output_bytes, "budget output bytes", positive=True, maximum=64 * 1024 * 1024)

    @classmethod
    def from_primitive(cls, value: object) -> MechanismResourceBudgetV1:
        raw = _require_fields(
            value,
            {"max_cases", "max_repetitions", "timeout_ms", "cpu_seconds", "memory_mib", "output_bytes"},
            "resource budget",
        )
        return cls(
            max_cases=raw["max_cases"],  # type: ignore[arg-type]
            max_repetitions=raw["max_repetitions"],  # type: ignore[arg-type]
            timeout_ms=raw["timeout_ms"],  # type: ignore[arg-type]
            cpu_seconds=_decimal_from_primitive(raw["cpu_seconds"], "budget CPU seconds"),
            memory_mib=raw["memory_mib"],  # type: ignore[arg-type]
            output_bytes=raw["output_bytes"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MechanismExperimentSpecV1(_CanonicalMechanismV1):
    precommitment_id: str
    hypothesis_id: str
    hypothesis_sha256: str
    parent_revision_sha256: str
    round_intent_sha256: str
    target_method: Literal["evaluate_exit"]
    allowed_symbols: tuple[str, ...]
    applicability: MechanismPredicateV1
    controls: tuple[MechanismControlV1, ...]
    metrics: tuple[MechanismMetricSpecV1, ...]
    aggregation: Literal["paired_case_delta"]
    minimum_relevant_cases: int
    recipe: MechanismRecipeV1
    disconfirming_observations: tuple[MechanismDisconfirmingObservationV1, ...]
    provenance: Literal["synthetic_offline", "authorized_discovery"]
    frozen_before_authoring: bool = True
    schema_version: Literal[1] = MECHANISM_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _identifier(self.precommitment_id, "precommitment ID")
        _identifier(self.hypothesis_id, "hypothesis ID")
        _digest(self.hypothesis_sha256, "hypothesis SHA-256")
        _digest(self.parent_revision_sha256, "parent revision SHA-256")
        _digest(self.round_intent_sha256, "round intent SHA-256")
        if self.target_method not in MECHANISM_TARGET_METHODS_V1:
            raise ValueError("target method is unsupported")
        symbols = _tuple(self.allowed_symbols, "allowed symbols", maximum=8)
        if not symbols or any(type(item) is not str or _SYMBOL_RE.fullmatch(item) is None for item in symbols):
            raise ValueError("allowed symbols are invalid")
        if tuple(sorted(symbols)) != symbols or len(set(symbols)) != len(symbols):
            raise ValueError("allowed symbols must be sorted and unique")
        if symbols != MECHANISM_ALLOWED_SYMBOLS_V1:
            raise ValueError("allowed symbols exceed the registered recipe")
        if type(self.applicability) is not MechanismPredicateV1:
            raise ValueError("applicability predicate is invalid")
        controls = _tuple(self.controls, "controls", maximum=8)
        if not controls or any(type(item) is not MechanismControlV1 for item in controls):
            raise ValueError("controls are invalid")
        if len({item.control_id for item in controls}) != len(controls):
            raise ValueError("controls must be unique")
        metrics = _tuple(self.metrics, "metrics", maximum=16)
        if not metrics or any(type(item) is not MechanismMetricSpecV1 for item in metrics):
            raise ValueError("metric specifications are invalid")
        metric_ids = tuple(item.metric_id for item in metrics)
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("metric specifications must be unique")
        for control in controls:
            if control.metric_id not in metric_ids:
                raise ValueError("protected control metric is not declared")
            control_metric = next(item for item in metrics if item.metric_id == control.metric_id)
            if (
                control_metric.unit != "count"
                or control_metric.direction != "unchanged"
                or control_metric.denominator == "evaluator_cases"
            ):
                raise ValueError("protected control metric semantics are invalid")
        if self.aggregation not in MECHANISM_AGGREGATIONS_V1:
            raise ValueError("aggregation is unsupported")
        _count(self.minimum_relevant_cases, "minimum relevant cases", positive=True, maximum=100_000)
        if type(self.recipe) is not MechanismRecipeV1:
            raise ValueError("recipe is invalid")
        observations = _tuple(self.disconfirming_observations, "disconfirming observations", maximum=16)
        if any(type(item) is not MechanismDisconfirmingObservationV1 for item in observations):
            raise ValueError("disconfirming observations are invalid")
        if len({item.observation_id for item in observations}) != len(observations):
            raise ValueError("disconfirming observations must be unique")
        if any(item.metric_id not in metric_ids for item in observations):
            raise ValueError("disconfirming observation metric is not declared")
        if self.provenance not in MECHANISM_PROVENANCES_V1:
            raise ValueError("mechanism provenance is unsupported")
        if self.frozen_before_authoring is not True:
            raise ValueError("mechanism spec must be frozen before authoring")
        if type(self.schema_version) is not int or self.schema_version != MECHANISM_SCHEMA_VERSION_V1:
            raise ValueError("mechanism spec schema version is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismExperimentSpecV1:
        raw = _require_fields(
            value,
            {
                "precommitment_id",
                "hypothesis_id",
                "hypothesis_sha256",
                "parent_revision_sha256",
                "round_intent_sha256",
                "target_method",
                "allowed_symbols",
                "applicability",
                "controls",
                "metrics",
                "aggregation",
                "minimum_relevant_cases",
                "recipe",
                "disconfirming_observations",
                "provenance",
                "frozen_before_authoring",
                "schema_version",
            },
            "mechanism experiment spec",
        )
        symbols = raw["allowed_symbols"]
        if type(symbols) is not list:
            raise ValueError("allowed symbols are invalid")
        controls = raw["controls"]
        metrics = raw["metrics"]
        observations = raw["disconfirming_observations"]
        if type(controls) is not list or type(metrics) is not list or type(observations) is not list:
            raise ValueError("mechanism experiment collections are invalid")
        return cls(
            precommitment_id=raw["precommitment_id"],  # type: ignore[arg-type]
            hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
            hypothesis_sha256=raw["hypothesis_sha256"],  # type: ignore[arg-type]
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            round_intent_sha256=raw["round_intent_sha256"],  # type: ignore[arg-type]
            target_method=raw["target_method"],  # type: ignore[arg-type]
            allowed_symbols=tuple(symbols),
            applicability=MechanismPredicateV1.from_primitive(raw["applicability"]),
            controls=tuple(MechanismControlV1.from_primitive(item) for item in controls),
            metrics=tuple(MechanismMetricSpecV1.from_primitive(item) for item in metrics),
            aggregation=raw["aggregation"],  # type: ignore[arg-type]
            minimum_relevant_cases=raw["minimum_relevant_cases"],  # type: ignore[arg-type]
            recipe=MechanismRecipeV1.from_primitive(raw["recipe"]),
            disconfirming_observations=tuple(
                MechanismDisconfirmingObservationV1.from_primitive(item) for item in observations
            ),
            provenance=raw["provenance"],  # type: ignore[arg-type]
            frozen_before_authoring=raw["frozen_before_authoring"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


def validate_mechanism_spec_hypothesis_v1(
    spec: MechanismExperimentSpecV1,
    hypothesis: HypothesisV5,
    *,
    parent_revision_sha256: str,
    round_intent_sha256: str,
) -> None:
    """Bind a mechanism spec to the exact selected hypothesis and round intent."""

    if type(spec) is not MechanismExperimentSpecV1 or type(hypothesis) is not HypothesisV5:
        raise ValueError("mechanism spec/hypothesis binding is invalid")
    if hypothesis.primary_mechanism != "exit":
        raise ValueError("mechanism spec target requires an exit primary mechanism")
    _digest(parent_revision_sha256, "expected parent revision SHA-256")
    _digest(round_intent_sha256, "expected round intent SHA-256")
    declared_metrics = {item.metric_id: item for item in spec.metrics}
    for predicted in hypothesis.predicted_changes:
        declared = declared_metrics.get(predicted.metric_id)
        if declared is None:
            raise ValueError("hypothesis metric is not declared in the mechanism spec")
        if declared.direction != predicted.direction:
            raise ValueError("hypothesis metric direction differs from the mechanism spec")
    if spec.hypothesis_id != hypothesis.hypothesis_id or spec.hypothesis_sha256 != hypothesis.sha256:
        raise ValueError("mechanism spec differs from the selected hypothesis")
    if spec.parent_revision_sha256 != parent_revision_sha256:
        raise ValueError("mechanism spec differs from the exact parent revision")
    if spec.round_intent_sha256 != round_intent_sha256:
        raise ValueError("mechanism spec differs from the round intent")


@dataclass(frozen=True, slots=True)
class MechanismObservationBindingV1(_CanonicalMechanismV1):
    experiment_id: str
    spec_sha256: str
    hypothesis_sha256: str
    parent_revision_sha256: str
    candidate_bytes_sha256: str
    corpus_sha256: str
    evaluator_contract_sha256: str
    scenario_id: str
    recipe_sha256: str
    resource_budget: MechanismResourceBudgetV1
    provenance: Literal["synthetic_offline", "authorized_discovery"]
    bound_before_measurement: bool = True
    schema_version: Literal[1] = MECHANISM_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "binding experiment ID")
        for value, label in (
            (self.spec_sha256, "spec SHA-256"),
            (self.hypothesis_sha256, "binding hypothesis SHA-256"),
            (self.parent_revision_sha256, "binding parent revision SHA-256"),
            (self.candidate_bytes_sha256, "candidate bytes SHA-256"),
            (self.corpus_sha256, "case corpus SHA-256"),
            (self.evaluator_contract_sha256, "evaluator contract SHA-256"),
            (self.recipe_sha256, "recipe SHA-256"),
        ):
            _digest(value, label)
        if self.candidate_bytes_sha256 == self.parent_revision_sha256:
            raise ValueError("candidate bytes must differ from the exact parent revision")
        _identifier(self.scenario_id, "scenario ID")
        if type(self.resource_budget) is not MechanismResourceBudgetV1:
            raise ValueError("resource budget is invalid")
        if self.provenance not in MECHANISM_PROVENANCES_V1:
            raise ValueError("binding provenance is unsupported")
        if self.bound_before_measurement is not True:
            raise ValueError("observation binding must be recorded before measurement")
        if type(self.schema_version) is not int or self.schema_version != MECHANISM_SCHEMA_VERSION_V1:
            raise ValueError("observation binding schema version is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismObservationBindingV1:
        raw = _require_fields(
            value,
            {
                "experiment_id",
                "spec_sha256",
                "hypothesis_sha256",
                "parent_revision_sha256",
                "candidate_bytes_sha256",
                "corpus_sha256",
                "evaluator_contract_sha256",
                "scenario_id",
                "recipe_sha256",
                "resource_budget",
                "provenance",
                "bound_before_measurement",
                "schema_version",
            },
            "observation binding",
        )
        return cls(
            experiment_id=raw["experiment_id"],  # type: ignore[arg-type]
            spec_sha256=raw["spec_sha256"],  # type: ignore[arg-type]
            hypothesis_sha256=raw["hypothesis_sha256"],  # type: ignore[arg-type]
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            candidate_bytes_sha256=raw["candidate_bytes_sha256"],  # type: ignore[arg-type]
            corpus_sha256=raw["corpus_sha256"],  # type: ignore[arg-type]
            evaluator_contract_sha256=raw["evaluator_contract_sha256"],  # type: ignore[arg-type]
            scenario_id=raw["scenario_id"],  # type: ignore[arg-type]
            recipe_sha256=raw["recipe_sha256"],  # type: ignore[arg-type]
            resource_budget=MechanismResourceBudgetV1.from_primitive(raw["resource_budget"]),
            provenance=raw["provenance"],  # type: ignore[arg-type]
            bound_before_measurement=raw["bound_before_measurement"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


def bind_mechanism_observation_v1(
    spec: MechanismExperimentSpecV1,
    *,
    experiment_id: str,
    candidate_bytes_sha256: str,
    corpus_sha256: str,
    evaluator_contract_sha256: str,
    scenario_id: str,
    resource_budget: MechanismResourceBudgetV1,
) -> MechanismObservationBindingV1:
    """Create the post-render identity binding before any measurement occurs."""

    if type(spec) is not MechanismExperimentSpecV1:
        raise ValueError("mechanism spec is invalid")
    binding = MechanismObservationBindingV1(
        experiment_id=experiment_id,
        spec_sha256=spec.sha256,
        hypothesis_sha256=spec.hypothesis_sha256,
        parent_revision_sha256=spec.parent_revision_sha256,
        candidate_bytes_sha256=candidate_bytes_sha256,
        corpus_sha256=corpus_sha256,
        evaluator_contract_sha256=evaluator_contract_sha256,
        scenario_id=scenario_id,
        recipe_sha256=spec.recipe.sha256,
        resource_budget=resource_budget,
        provenance=spec.provenance,
    )
    validate_mechanism_observation_binding_v1(spec, binding)
    return binding


def validate_mechanism_observation_binding_v1(
    spec: MechanismExperimentSpecV1,
    binding: MechanismObservationBindingV1,
    *,
    expected_candidate_bytes_sha256: str | None = None,
) -> None:
    if type(spec) is not MechanismExperimentSpecV1 or type(binding) is not MechanismObservationBindingV1:
        raise ValueError("mechanism spec/observation binding is invalid")
    _digest(binding.experiment_id, "binding experiment ID")
    if binding.spec_sha256 != spec.sha256:
        raise ValueError("observation binding differs from the frozen spec")
    if binding.hypothesis_sha256 != spec.hypothesis_sha256:
        raise ValueError("observation binding differs from the selected hypothesis")
    if binding.parent_revision_sha256 != spec.parent_revision_sha256:
        raise ValueError("observation binding differs from the exact parent revision")
    if expected_candidate_bytes_sha256 is not None:
        _digest(expected_candidate_bytes_sha256, "expected candidate bytes SHA-256")
        if binding.candidate_bytes_sha256 != expected_candidate_bytes_sha256:
            raise ValueError("observation binding differs from the rendered candidate")
    if binding.recipe_sha256 != spec.recipe.sha256:
        raise ValueError("observation binding differs from the registered recipe")
    if binding.provenance != spec.provenance:
        raise ValueError("observation binding provenance differs from the spec")
    if binding.bound_before_measurement is not True:
        raise ValueError("observation binding was recorded after measurement")


@dataclass(frozen=True, slots=True)
class MechanismResourceUsageV1(_CanonicalMechanismV1):
    cases: int
    repetitions: int
    elapsed_ms: int
    cpu_seconds: Decimal
    peak_memory_mib: int
    output_bytes: int

    def __post_init__(self) -> None:
        _count(self.cases, "usage cases", maximum=100_000)
        _count(self.repetitions, "usage repetitions", maximum=64)
        _count(self.elapsed_ms, "usage elapsed milliseconds", maximum=86_400_000)
        _decimal(self.cpu_seconds, "usage CPU seconds", nonnegative=True)
        if self.cpu_seconds > MECHANISM_MAX_CPU_SECONDS_V1:
            raise ValueError("usage CPU seconds exceed the mechanism ceiling")
        _count(self.peak_memory_mib, "usage peak memory", maximum=1_048_576)
        _count(self.output_bytes, "usage output bytes", maximum=64 * 1024 * 1024)

    @classmethod
    def from_primitive(cls, value: object) -> MechanismResourceUsageV1:
        raw = _require_fields(
            value,
            {"cases", "repetitions", "elapsed_ms", "cpu_seconds", "peak_memory_mib", "output_bytes"},
            "resource usage",
        )
        return cls(
            cases=raw["cases"],  # type: ignore[arg-type]
            repetitions=raw["repetitions"],  # type: ignore[arg-type]
            elapsed_ms=raw["elapsed_ms"],  # type: ignore[arg-type]
            cpu_seconds=_decimal_from_primitive(raw["cpu_seconds"], "usage CPU seconds"),
            peak_memory_mib=raw["peak_memory_mib"],  # type: ignore[arg-type]
            output_bytes=raw["output_bytes"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MechanismExecutionV1(_CanonicalMechanismV1):
    status: Literal["completed", "not_run", "failed"]
    reason: str
    resource_usage: MechanismResourceUsageV1 | None = None

    def __post_init__(self) -> None:
        completed_reasons = {"completed"}
        not_run_reasons = {
            "disabled_development",
            "spec_recorded_too_late",
            "unsupported_recipe",
            "missing_input",
            "heldout_provenance",
            "worker_unavailable",
        }
        failed_reasons = {"execution_failed", "resource_limit", "timeout", "protocol_failure", "corrupt_artifact"}
        if self.status not in {"completed", "not_run", "failed"}:
            raise ValueError("execution status is unsupported")
        _identifier(self.reason, "execution reason")
        allowed = {
            "completed": completed_reasons,
            "not_run": not_run_reasons,
            "failed": failed_reasons,
        }[self.status]
        if self.reason not in allowed:
            raise ValueError("execution reason does not match status")
        if self.resource_usage is not None and type(self.resource_usage) is not MechanismResourceUsageV1:
            raise ValueError("execution resource usage is invalid")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismExecutionV1:
        raw = _require_fields(value, {"status", "reason", "resource_usage"}, "execution")
        usage = (
            None if raw["resource_usage"] is None else MechanismResourceUsageV1.from_primitive(raw["resource_usage"])
        )
        return cls(status=raw["status"], reason=raw["reason"], resource_usage=usage)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MechanismCoverageV1(_CanonicalMechanismV1):
    total_cases: int
    relevant_cases: int
    decision_changed_cases: int
    protected_control_cases: int
    protected_control_unchanged_cases: int
    unsupported_cases: int
    branch_coverage: Literal["unavailable", "instrumented"] = "unavailable"

    def __post_init__(self) -> None:
        values = (
            (self.total_cases, "total cases"),
            (self.relevant_cases, "relevant cases"),
            (self.decision_changed_cases, "decision changed cases"),
            (self.protected_control_cases, "protected control cases"),
            (self.protected_control_unchanged_cases, "protected control unchanged cases"),
            (self.unsupported_cases, "unsupported cases"),
        )
        for value, label in values:
            _count(value, label, maximum=100_000)
        if (
            self.relevant_cases > self.total_cases
            or self.unsupported_cases > self.total_cases
            or self.relevant_cases + self.unsupported_cases > self.total_cases
        ):
            raise ValueError("coverage cases exceed total cases")
        if self.decision_changed_cases > self.relevant_cases:
            raise ValueError("decision changes exceed relevant cases")
        if self.protected_control_cases > self.total_cases - self.unsupported_cases:
            raise ValueError("protected controls exceed supported cases")
        if self.protected_control_unchanged_cases > self.protected_control_cases:
            raise ValueError("protected unchanged cases exceed protected controls")
        if self.branch_coverage not in {"unavailable", "instrumented"}:
            raise ValueError("branch coverage status is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismCoverageV1:
        raw = _require_fields(
            value,
            {
                "total_cases",
                "relevant_cases",
                "decision_changed_cases",
                "protected_control_cases",
                "protected_control_unchanged_cases",
                "unsupported_cases",
                "branch_coverage",
            },
            "coverage",
        )
        return cls(
            total_cases=raw["total_cases"],  # type: ignore[arg-type]
            relevant_cases=raw["relevant_cases"],  # type: ignore[arg-type]
            decision_changed_cases=raw["decision_changed_cases"],  # type: ignore[arg-type]
            protected_control_cases=raw["protected_control_cases"],  # type: ignore[arg-type]
            protected_control_unchanged_cases=raw["protected_control_unchanged_cases"],  # type: ignore[arg-type]
            unsupported_cases=raw["unsupported_cases"],  # type: ignore[arg-type]
            branch_coverage=raw["branch_coverage"],  # type: ignore[arg-type]
        )


MechanismAssessmentV1 = Literal[
    "supported_on_cases",
    "contradicted_on_cases",
    "insufficient_evidence",
]


def _assessment_for_measurement(
    *,
    direction: Literal["increase", "decrease", "unchanged"],
    delta: int,
    tolerance: int,
    denominator: int,
    minimum_relevant_cases: int,
) -> MechanismAssessmentV1:
    if denominator == 0 or denominator < minimum_relevant_cases:
        return "insufficient_evidence"
    if direction == "increase":
        return "supported_on_cases" if delta > tolerance else "contradicted_on_cases"
    if direction == "decrease":
        return "supported_on_cases" if delta < -tolerance else "contradicted_on_cases"
    return "supported_on_cases" if abs(delta) <= tolerance else "contradicted_on_cases"


@dataclass(frozen=True, slots=True)
class MechanismPredictionResultV1(_CanonicalMechanismV1):
    metric_id: str
    unit: Literal["count"]
    direction: Literal["increase", "decrease", "unchanged"]
    denominator_kind: Literal["relevant_cases", "control_cases", "all_cases", "evaluator_cases"]
    selector: MechanismDiagnosticSelectorV1 | None
    minimum_relevant_cases: int
    parent_value: Decimal | None
    candidate_value: Decimal | None
    numerator: Decimal | None
    denominator: int
    tolerance: Decimal
    paired_delta: Decimal | None
    assessment: MechanismAssessmentV1
    evidence_origin: Literal["precommitted"] = "precommitted"
    availability: Literal["measured", "unavailable"] = "measured"
    unavailable_reason: (
        Literal[
            "evaluator_metric_missing",
            "execution_failed",
            "mixed_context",
            "not_run",
            "unsupported_case",
        ]
        | None
    ) = None

    def __post_init__(self) -> None:
        expected_unit = MECHANISM_METRIC_UNITS_V1.get(self.metric_id)
        if expected_unit is None:
            raise ValueError("prediction metric ID is unsupported")
        if self.unit != expected_unit:
            raise ValueError("prediction metric unit is invalid")
        if self.direction not in MECHANISM_DIRECTIONS_V1:
            raise ValueError("prediction direction is unsupported")
        if self.denominator_kind not in MECHANISM_DENOMINATORS_V1:
            raise ValueError("prediction denominator kind is unsupported")
        expected_evaluator = self.metric_id == "evaluator.exit_attribution_count"
        if expected_evaluator:
            if type(self.selector) is not MechanismDiagnosticSelectorV1:
                raise ValueError("evaluator prediction requires a registered selector")
            if self.denominator_kind != "evaluator_cases":
                raise ValueError("evaluator prediction requires evaluator_cases denominator")
        elif self.selector is not None:
            raise ValueError("non-evaluator prediction cannot carry a diagnostic selector")
        elif self.denominator_kind == "evaluator_cases":
            raise ValueError("paired prediction cannot use evaluator_cases denominator")
        _count(self.minimum_relevant_cases, "prediction minimum relevant cases", positive=True, maximum=100_000)
        _count(self.denominator, "prediction denominator", maximum=100_000)
        if self.assessment not in {
            "supported_on_cases",
            "contradicted_on_cases",
            "insufficient_evidence",
        }:
            raise ValueError("prediction assessment is unsupported")
        if self.evidence_origin != "precommitted":
            raise ValueError("retrospective evidence cannot become a precommitted prediction")
        tolerance_count = _count_integer(self.tolerance, "prediction tolerance", nonnegative=True)
        if self.availability not in {"measured", "unavailable"}:
            raise ValueError("prediction availability is unsupported")
        if self.availability == "unavailable":
            if self.unavailable_reason not in MECHANISM_UNAVAILABLE_REASONS_V1:
                raise ValueError("unavailable prediction requires a closed reason")
            if self.unavailable_reason == "evaluator_metric_missing" and not expected_evaluator:
                raise ValueError("evaluator metric missing reason requires an evaluator prediction")
            if self.unavailable_reason == "mixed_context" and not expected_evaluator:
                raise ValueError("mixed context reason requires an evaluator prediction")
            if any(
                value is not None
                for value in (self.parent_value, self.candidate_value, self.numerator, self.paired_delta)
            ):
                raise ValueError("unavailable prediction cannot carry fabricated measurements")
            if self.denominator != 0:
                raise ValueError("unavailable prediction denominator must be zero")
            if self.assessment != "insufficient_evidence":
                raise ValueError("unavailable prediction requires insufficient evidence")
            return
        if self.unavailable_reason is not None:
            raise ValueError("measured prediction cannot carry an unavailable reason")
        parent_count = _count_integer(self.parent_value, "prediction parent value", nonnegative=True)
        candidate_count = _count_integer(self.candidate_value, "prediction candidate value", nonnegative=True)
        numerator_count = _count_integer(self.numerator, "prediction numerator", nonnegative=True)
        delta_count = _count_integer(self.paired_delta, "prediction paired delta")
        if delta_count != candidate_count - parent_count:
            raise ValueError("prediction paired delta does not reconcile")
        if numerator_count != candidate_count:
            raise ValueError("prediction numerator must equal the candidate count")
        if not expected_evaluator and any(
            value > self.denominator for value in (parent_count, candidate_count, numerator_count)
        ):
            raise ValueError("prediction count exceeds its selected case denominator")
        expected_assessment = _assessment_for_measurement(
            direction=self.direction,
            delta=delta_count,
            tolerance=tolerance_count,
            denominator=self.denominator,
            minimum_relevant_cases=self.minimum_relevant_cases,
        )
        if self.assessment != expected_assessment:
            raise ValueError("prediction assessment does not match its declared meaning")

    @classmethod
    def unavailable(
        cls,
        metric: MechanismMetricSpecV1,
        *,
        minimum_relevant_cases: int,
        reason: Literal[
            "evaluator_metric_missing",
            "execution_failed",
            "mixed_context",
            "not_run",
            "unsupported_case",
        ],
    ) -> MechanismPredictionResultV1:
        if type(metric) is not MechanismMetricSpecV1:
            raise ValueError("prediction metric specification is invalid")
        _count(minimum_relevant_cases, "minimum relevant cases", positive=True, maximum=100_000)
        if reason not in MECHANISM_UNAVAILABLE_REASONS_V1:
            raise ValueError("unavailable prediction reason is unsupported")
        return cls(
            metric_id=metric.metric_id,
            unit=metric.unit,
            direction=metric.direction,
            denominator_kind=metric.denominator,
            selector=metric.selector,
            minimum_relevant_cases=minimum_relevant_cases,
            parent_value=None,
            candidate_value=None,
            numerator=None,
            denominator=0,
            tolerance=metric.tolerance,
            paired_delta=None,
            assessment="insufficient_evidence",
            availability="unavailable",
            unavailable_reason=reason,
        )

    @classmethod
    def from_measurement(
        cls,
        metric: MechanismMetricSpecV1,
        *,
        parent_value: Decimal,
        candidate_value: Decimal,
        numerator: Decimal,
        denominator: int,
        minimum_relevant_cases: int,
    ) -> MechanismPredictionResultV1:
        if type(metric) is not MechanismMetricSpecV1:
            raise ValueError("prediction metric specification is invalid")
        _count(minimum_relevant_cases, "minimum relevant cases", positive=True, maximum=100_000)
        parent = _count_integer(parent_value, "parent value", nonnegative=True)
        candidate = _count_integer(candidate_value, "candidate value", nonnegative=True)
        count = _count_integer(numerator, "prediction numerator", nonnegative=True)
        _count(denominator, "prediction denominator", maximum=100_000)
        tolerance = _count_integer(metric.tolerance, "metric tolerance", nonnegative=True)
        delta = candidate - parent
        assessment = _assessment_for_measurement(
            direction=metric.direction,
            delta=delta,
            tolerance=tolerance,
            denominator=denominator,
            minimum_relevant_cases=minimum_relevant_cases,
        )
        return cls(
            metric_id=metric.metric_id,
            unit=metric.unit,
            direction=metric.direction,
            denominator_kind=metric.denominator,
            selector=metric.selector,
            minimum_relevant_cases=minimum_relevant_cases,
            parent_value=Decimal(parent),
            candidate_value=Decimal(candidate),
            numerator=Decimal(count),
            denominator=denominator,
            tolerance=Decimal(tolerance),
            paired_delta=Decimal(delta),
            assessment=assessment,
        )

    @classmethod
    def from_primitive(cls, value: object) -> MechanismPredictionResultV1:
        raw = _require_fields(
            value,
            {
                "metric_id",
                "unit",
                "direction",
                "denominator_kind",
                "selector",
                "minimum_relevant_cases",
                "parent_value",
                "candidate_value",
                "numerator",
                "denominator",
                "tolerance",
                "paired_delta",
                "assessment",
                "evidence_origin",
                "availability",
                "unavailable_reason",
            },
            "prediction result",
        )
        parent_value = (
            None
            if raw["parent_value"] is None
            else _decimal_from_primitive(raw["parent_value"], "prediction parent value")
        )
        candidate_value = (
            None
            if raw["candidate_value"] is None
            else _decimal_from_primitive(raw["candidate_value"], "prediction candidate value")
        )
        numerator = (
            None if raw["numerator"] is None else _decimal_from_primitive(raw["numerator"], "prediction numerator")
        )
        paired_delta = (
            None
            if raw["paired_delta"] is None
            else _decimal_from_primitive(raw["paired_delta"], "prediction paired delta")
        )
        return cls(
            metric_id=raw["metric_id"],  # type: ignore[arg-type]
            unit=raw["unit"],  # type: ignore[arg-type]
            direction=raw["direction"],  # type: ignore[arg-type]
            denominator_kind=raw["denominator_kind"],  # type: ignore[arg-type]
            selector=None if raw["selector"] is None else MechanismDiagnosticSelectorV1.from_primitive(raw["selector"]),
            minimum_relevant_cases=raw["minimum_relevant_cases"],  # type: ignore[arg-type]
            parent_value=parent_value,
            candidate_value=candidate_value,
            numerator=numerator,
            denominator=raw["denominator"],  # type: ignore[arg-type]
            tolerance=_decimal_from_primitive(raw["tolerance"], "prediction tolerance"),
            paired_delta=paired_delta,
            assessment=raw["assessment"],  # type: ignore[arg-type]
            evidence_origin=raw["evidence_origin"],  # type: ignore[arg-type]
            availability=raw["availability"],  # type: ignore[arg-type]
            unavailable_reason=raw["unavailable_reason"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class MechanismEvaluatorContextV1(_CanonicalMechanismV1):
    """Identity of one matched parent/candidate evaluator consequence."""

    stage: Literal["quick", "discovery"]
    episode_id: str
    episode_ordinal: int | None
    scenario_id: str
    evaluator_contract_sha256: str
    panel_sha256: str
    parent_policy_identity_sha256: str
    candidate_policy_identity_sha256: str
    parent_source_bundle_sha256: str
    candidate_source_bundle_sha256: str
    parent_report_sha256: str
    candidate_report_sha256: str

    def __post_init__(self) -> None:
        if self.stage not in MECHANISM_EVALUATOR_STAGES_V1:
            raise ValueError("evaluator context stage is unsupported")
        _identifier(self.episode_id, "evaluator context episode ID")
        if self.stage == "quick" and self.episode_id != "quick":
            raise ValueError("quick evaluator contexts require the registered quick episode ID")
        if self.episode_ordinal is None:
            if self.stage != "quick":
                raise ValueError("only quick evaluator contexts may be unnumbered")
        else:
            _count(self.episode_ordinal, "evaluator context episode ordinal", positive=True, maximum=100_000)
            if self.stage == "quick":
                raise ValueError("quick evaluator contexts must be unnumbered")
        _identifier(self.scenario_id, "evaluator context scenario ID")
        for value, label in (
            (self.evaluator_contract_sha256, "evaluator context evaluator SHA-256"),
            (self.panel_sha256, "evaluator context panel SHA-256"),
            (self.parent_policy_identity_sha256, "evaluator context parent policy SHA-256"),
            (self.candidate_policy_identity_sha256, "evaluator context candidate policy SHA-256"),
            (self.parent_source_bundle_sha256, "evaluator context parent source bundle SHA-256"),
            (self.candidate_source_bundle_sha256, "evaluator context candidate source bundle SHA-256"),
            (self.parent_report_sha256, "evaluator context parent report SHA-256"),
            (self.candidate_report_sha256, "evaluator context candidate report SHA-256"),
        ):
            _digest(value, label)
        if self.parent_policy_identity_sha256 == self.candidate_policy_identity_sha256:
            raise ValueError("evaluator context parent and candidate policies must differ")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismEvaluatorContextV1:
        raw = _require_fields(
            value,
            {
                "stage",
                "episode_id",
                "episode_ordinal",
                "scenario_id",
                "evaluator_contract_sha256",
                "panel_sha256",
                "parent_policy_identity_sha256",
                "candidate_policy_identity_sha256",
                "parent_source_bundle_sha256",
                "candidate_source_bundle_sha256",
                "parent_report_sha256",
                "candidate_report_sha256",
            },
            "evaluator context",
        )
        return cls(
            stage=raw["stage"],  # type: ignore[arg-type]
            episode_id=raw["episode_id"],  # type: ignore[arg-type]
            episode_ordinal=raw["episode_ordinal"],  # type: ignore[arg-type]
            scenario_id=raw["scenario_id"],  # type: ignore[arg-type]
            evaluator_contract_sha256=raw["evaluator_contract_sha256"],  # type: ignore[arg-type]
            panel_sha256=raw["panel_sha256"],  # type: ignore[arg-type]
            parent_policy_identity_sha256=raw["parent_policy_identity_sha256"],  # type: ignore[arg-type]
            candidate_policy_identity_sha256=raw["candidate_policy_identity_sha256"],  # type: ignore[arg-type]
            parent_source_bundle_sha256=raw["parent_source_bundle_sha256"],  # type: ignore[arg-type]
            candidate_source_bundle_sha256=raw["candidate_source_bundle_sha256"],  # type: ignore[arg-type]
            parent_report_sha256=raw["parent_report_sha256"],  # type: ignore[arg-type]
            candidate_report_sha256=raw["candidate_report_sha256"],  # type: ignore[arg-type]
        )


def mechanism_evaluator_context_key_v1(
    context: MechanismEvaluatorContextV1,
) -> tuple[str, str, int | None, str, str, str, str, str, str, str]:
    """Return the semantic evaluator identity without report payload digests."""

    if type(context) is not MechanismEvaluatorContextV1:
        raise ValueError("evaluator context is invalid")
    return (
        context.stage,
        context.episode_id,
        context.episode_ordinal,
        context.scenario_id,
        context.evaluator_contract_sha256,
        context.panel_sha256,
        context.parent_policy_identity_sha256,
        context.candidate_policy_identity_sha256,
        context.parent_source_bundle_sha256,
        context.candidate_source_bundle_sha256,
    )


@dataclass(frozen=True, slots=True)
class MechanismEvaluatorContextGroupV1(_CanonicalMechanismV1):
    """Per-context evaluator rows retained alongside report-level predictions."""

    context: MechanismEvaluatorContextV1
    predictions: tuple[MechanismPredictionResultV1, ...]
    aggregate_formula: Literal["sum_contexts_v1"] = "sum_contexts_v1"

    def __post_init__(self) -> None:
        if type(self.context) is not MechanismEvaluatorContextV1:
            raise ValueError("evaluator context group identity is invalid")
        predictions = _tuple(self.predictions, "evaluator context predictions", maximum=16)
        if not predictions or any(type(item) is not MechanismPredictionResultV1 for item in predictions):
            raise ValueError("evaluator context predictions are invalid")
        if len({item.metric_id for item in predictions}) != len(predictions):
            raise ValueError("evaluator context predictions must be unique")
        if any(item.metric_id != "evaluator.exit_attribution_count" for item in predictions):
            raise ValueError("evaluator context can only carry the registered evaluator metric")
        if self.aggregate_formula not in MECHANISM_CONSEQUENCE_AGGREGATIONS_V1:
            raise ValueError("evaluator context aggregate formula is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismEvaluatorContextGroupV1:
        raw = _require_fields(
            value,
            {"context", "predictions", "aggregate_formula"},
            "evaluator context group",
        )
        predictions = raw["predictions"]
        if type(predictions) is not list:
            raise ValueError("evaluator context predictions are invalid")
        return cls(
            context=MechanismEvaluatorContextV1.from_primitive(raw["context"]),
            predictions=tuple(MechanismPredictionResultV1.from_primitive(item) for item in predictions),
            aggregate_formula=raw["aggregate_formula"],  # type: ignore[arg-type]
        )


def aggregate_mechanism_evaluator_predictions_v1(
    metric: MechanismMetricSpecV1,
    predictions: tuple[MechanismPredictionResultV1, ...],
    *,
    minimum_relevant_cases: int,
) -> MechanismPredictionResultV1:
    """Apply the registered evaluator aggregation to retained context rows."""

    if type(metric) is not MechanismMetricSpecV1 or metric.metric_id != "evaluator.exit_attribution_count":
        raise ValueError("evaluator aggregation requires the registered evaluator metric")
    if type(predictions) is not tuple or any(type(item) is not MechanismPredictionResultV1 for item in predictions):
        raise ValueError("evaluator aggregation predictions are invalid")
    for prediction in predictions:
        if (
            prediction.metric_id != metric.metric_id
            or prediction.unit != metric.unit
            or prediction.direction != metric.direction
            or prediction.denominator_kind != metric.denominator
            or prediction.selector != metric.selector
            or prediction.tolerance != metric.tolerance
            or prediction.minimum_relevant_cases != minimum_relevant_cases
        ):
            raise ValueError("evaluator aggregation prediction meaning differs from the metric")
    if not predictions:
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="evaluator_metric_missing",
        )
    if all(
        item.availability == "unavailable" and item.unavailable_reason == "evaluator_metric_missing"
        for item in predictions
    ):
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="evaluator_metric_missing",
        )
    if any(item.availability != "measured" for item in predictions):
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="mixed_context",
        )
    tolerance = int(metric.tolerance)
    deltas = tuple(int(item.paired_delta) for item in predictions if item.paired_delta is not None)
    if any(delta > tolerance for delta in deltas) and any(delta < -tolerance for delta in deltas):
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="mixed_context",
        )
    assessments = {item.assessment for item in predictions if item.assessment != "insufficient_evidence"}
    if len(assessments) > 1:
        return MechanismPredictionResultV1.unavailable(
            metric,
            minimum_relevant_cases=minimum_relevant_cases,
            reason="mixed_context",
        )
    parent_value = sum(int(item.parent_value) for item in predictions if item.parent_value is not None)
    candidate_value = sum(int(item.candidate_value) for item in predictions if item.candidate_value is not None)
    return MechanismPredictionResultV1.from_measurement(
        metric,
        parent_value=Decimal(parent_value),
        candidate_value=Decimal(candidate_value),
        numerator=Decimal(candidate_value),
        denominator=len(predictions),
        minimum_relevant_cases=minimum_relevant_cases,
    )


def _resource_usage_exceeds_budget(
    usage: MechanismResourceUsageV1,
    budget: MechanismResourceBudgetV1,
) -> bool:
    return (
        usage.cases > budget.max_cases
        or usage.repetitions > budget.max_repetitions
        or usage.elapsed_ms > budget.timeout_ms
        or usage.cpu_seconds > budget.cpu_seconds
        or usage.peak_memory_mib > budget.memory_mib
        or usage.output_bytes > budget.output_bytes
    )


def _expected_unavailable_reason(execution: MechanismExecutionV1) -> str | None:
    if execution.status == "not_run":
        return "not_run"
    if execution.status == "failed":
        return "execution_failed"
    return None


@dataclass(frozen=True, slots=True)
class MechanismEvidenceReportV1(_CanonicalMechanismV1):
    binding: MechanismObservationBindingV1
    execution: MechanismExecutionV1
    coverage: MechanismCoverageV1
    predictions: tuple[MechanismPredictionResultV1, ...]
    limitations: tuple[str, ...]
    consequence_report_sha256: str | None = None
    consequence_contexts: tuple[MechanismEvaluatorContextGroupV1, ...] = ()
    schema_version: Literal[1] = MECHANISM_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.binding) is not MechanismObservationBindingV1:
            raise ValueError("evidence report binding is invalid")
        if type(self.execution) is not MechanismExecutionV1:
            raise ValueError("evidence report execution is invalid")
        if type(self.coverage) is not MechanismCoverageV1:
            raise ValueError("evidence report coverage is invalid")
        predictions = _tuple(self.predictions, "evidence predictions", maximum=64)
        if any(type(item) is not MechanismPredictionResultV1 for item in predictions):
            raise ValueError("evidence predictions are invalid")
        if len({item.metric_id for item in predictions}) != len(predictions):
            raise ValueError("evidence predictions must be unique")
        if self.execution.resource_usage is not None and _resource_usage_exceeds_budget(
            self.execution.resource_usage,
            self.binding.resource_budget,
        ):
            if self.execution.status != "failed" or self.execution.reason not in {"resource_limit", "timeout"}:
                raise ValueError("execution resource usage exceeds its declared budget")
        unavailable_reason = _expected_unavailable_reason(self.execution)
        if unavailable_reason is not None and any(
            item.availability != "unavailable" or item.unavailable_reason != unavailable_reason for item in predictions
        ):
            raise ValueError("failed or unrun execution requires typed unavailable predictions")
        if self.execution.status == "completed" and any(
            item.availability == "unavailable" and item.unavailable_reason in {"execution_failed", "not_run"}
            for item in predictions
        ):
            raise ValueError("completed execution cannot carry a worker failure availability reason")
        limitations = _tuple(self.limitations, "evidence limitations", maximum=32)
        if any(type(item) is not str or not item.strip() or len(item) > 512 for item in limitations):
            raise ValueError("evidence limitations are invalid")
        if self.consequence_report_sha256 is not None:
            _digest(self.consequence_report_sha256, "consequence report SHA-256")
        consequence_contexts = _tuple(self.consequence_contexts, "consequence contexts", maximum=64)
        if any(type(item) is not MechanismEvaluatorContextGroupV1 for item in consequence_contexts):
            raise ValueError("consequence contexts are invalid")
        context_keys = tuple(mechanism_evaluator_context_key_v1(item.context) for item in consequence_contexts)
        if len(set(context_keys)) != len(context_keys):
            raise ValueError("consequence contexts must be semantically unique")
        if type(self.schema_version) is not int or self.schema_version != MECHANISM_SCHEMA_VERSION_V1:
            raise ValueError("evidence report schema version is unsupported")

    @classmethod
    def from_primitive(cls, value: object) -> MechanismEvidenceReportV1:
        raw = _require_fields(
            value,
            {
                "binding",
                "execution",
                "coverage",
                "predictions",
                "limitations",
                "consequence_report_sha256",
                "consequence_contexts",
                "schema_version",
            },
            "evidence report",
        )
        predictions = raw["predictions"]
        limitations = raw["limitations"]
        consequence_contexts = raw["consequence_contexts"]
        if type(predictions) is not list or type(limitations) is not list or type(consequence_contexts) is not list:
            raise ValueError("evidence report collections are invalid")
        return cls(
            binding=MechanismObservationBindingV1.from_primitive(raw["binding"]),
            execution=MechanismExecutionV1.from_primitive(raw["execution"]),
            coverage=MechanismCoverageV1.from_primitive(raw["coverage"]),
            predictions=tuple(MechanismPredictionResultV1.from_primitive(item) for item in predictions),
            limitations=tuple(limitations),
            consequence_report_sha256=raw["consequence_report_sha256"],  # type: ignore[arg-type]
            consequence_contexts=tuple(
                MechanismEvaluatorContextGroupV1.from_primitive(item) for item in consequence_contexts
            ),
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


def validate_mechanism_report_against_spec_v1(
    spec: MechanismExperimentSpecV1,
    report: MechanismEvidenceReportV1,
) -> None:
    """Authenticate report rows against the frozen metric meanings."""

    if type(spec) is not MechanismExperimentSpecV1 or type(report) is not MechanismEvidenceReportV1:
        raise ValueError("mechanism spec/report are invalid")
    validate_mechanism_observation_binding_v1(spec, report.binding)
    declared = {item.metric_id: item for item in spec.metrics}
    actual = {item.metric_id for item in report.predictions}
    if actual != set(declared):
        raise ValueError("report predictions differ from the declared mechanism metrics")
    for result in report.predictions:
        metric = declared[result.metric_id]
        if (
            result.unit != metric.unit
            or result.direction != metric.direction
            or result.denominator_kind != metric.denominator
            or result.selector != metric.selector
            or result.tolerance != metric.tolerance
            or result.minimum_relevant_cases != spec.minimum_relevant_cases
        ):
            raise ValueError("report prediction meaning differs from the frozen mechanism spec")
    evaluator_metric = declared.get("evaluator.exit_attribution_count")
    if report.consequence_contexts and evaluator_metric is None:
        raise ValueError("consequence contexts require the declared evaluator metric")
    context_keys = tuple(mechanism_evaluator_context_key_v1(item.context) for item in report.consequence_contexts)
    if len(set(context_keys)) != len(context_keys):
        raise ValueError("consequence contexts are semantically duplicated")
    for group in report.consequence_contexts:
        context = group.context
        if (
            context.evaluator_contract_sha256 != report.binding.evaluator_contract_sha256
            or context.parent_policy_identity_sha256 != report.binding.parent_revision_sha256
            or context.candidate_source_bundle_sha256 != report.binding.candidate_bytes_sha256
            or context.scenario_id != report.binding.scenario_id
        ):
            raise ValueError("consequence context identity differs from the observation binding")
        for result in group.predictions:
            if evaluator_metric is None:
                raise ValueError("consequence context metric is not declared")
            if (
                result.metric_id != evaluator_metric.metric_id
                or result.unit != evaluator_metric.unit
                or result.direction != evaluator_metric.direction
                or result.denominator_kind != evaluator_metric.denominator
                or result.selector != evaluator_metric.selector
                or result.tolerance != evaluator_metric.tolerance
                or result.minimum_relevant_cases != spec.minimum_relevant_cases
            ):
                raise ValueError("consequence context prediction meaning differs from the frozen mechanism spec")
    unavailable_reason = _expected_unavailable_reason(report.execution)
    if unavailable_reason is not None:
        if any(
            result.availability != "unavailable" or result.unavailable_reason != unavailable_reason
            for result in report.predictions
        ):
            raise ValueError("report execution status and prediction availability differ")
    elif any(
        result.availability == "unavailable" and result.unavailable_reason in {"execution_failed", "not_run"}
        for result in report.predictions
    ):
        raise ValueError("completed report cannot use an execution failure availability reason")
    if report.execution.status == "completed" and evaluator_metric is not None:
        evaluator_result = next(item for item in report.predictions if item.metric_id == evaluator_metric.metric_id)
        retained = tuple(group.predictions[0] for group in report.consequence_contexts)
        expected = aggregate_mechanism_evaluator_predictions_v1(
            evaluator_metric,
            retained,
            minimum_relevant_cases=spec.minimum_relevant_cases,
        )
        if evaluator_result != expected:
            raise ValueError("report evaluator aggregate differs from retained consequence contexts")


@dataclass(frozen=True, slots=True)
class MechanismLearningProjectionV1(_CanonicalMechanismV1):
    """A bounded measured finding; critic text is retained as interpretation."""

    experiment_id: str
    hypothesis_id: str
    spec_sha256: str
    binding_sha256: str
    report_sha256: str
    parent_revision_sha256: str
    binding: MechanismObservationBindingV1
    execution: MechanismExecutionV1
    controls: tuple[MechanismControlV1, ...]
    applicability: MechanismPredicateV1
    coverage: MechanismCoverageV1
    predictions: tuple[MechanismPredictionResultV1, ...]
    limitations: tuple[str, ...]
    consequence_contexts: tuple[MechanismEvaluatorContextGroupV1, ...] = ()
    critic_interpretation: str | None = None
    critic_next_direction: str | None = None
    critic_interpretation_is_retrospective: bool = True
    schema_version: Literal[1] = MECHANISM_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _digest(self.experiment_id, "projection experiment ID")
        _identifier(self.hypothesis_id, "projection hypothesis ID")
        for value, label in (
            (self.spec_sha256, "projection spec SHA-256"),
            (self.binding_sha256, "projection binding SHA-256"),
            (self.report_sha256, "projection report SHA-256"),
            (self.parent_revision_sha256, "projection parent revision SHA-256"),
        ):
            _digest(value, label)
        if type(self.binding) is not MechanismObservationBindingV1:
            raise ValueError("projection binding is invalid")
        if type(self.execution) is not MechanismExecutionV1:
            raise ValueError("projection execution is invalid")
        controls = _tuple(self.controls, "projection controls", maximum=8)
        if not controls or any(type(item) is not MechanismControlV1 for item in controls):
            raise ValueError("projection controls are invalid")
        if len({item.control_id for item in controls}) != len(controls):
            raise ValueError("projection controls must be unique")
        if (
            self.experiment_id != self.binding.experiment_id
            or self.binding_sha256 != self.binding.sha256
            or self.spec_sha256 != self.binding.spec_sha256
            or self.parent_revision_sha256 != self.binding.parent_revision_sha256
        ):
            raise ValueError("projection identity differs from its observation binding")
        if type(self.applicability) is not MechanismPredicateV1:
            raise ValueError("projection applicability is invalid")
        if type(self.coverage) is not MechanismCoverageV1:
            raise ValueError("projection coverage is invalid")
        predictions = _tuple(self.predictions, "projection predictions", maximum=64)
        if any(type(item) is not MechanismPredictionResultV1 for item in predictions):
            raise ValueError("projection predictions are invalid")
        if len({item.metric_id for item in predictions}) != len(predictions):
            raise ValueError("projection predictions must be unique")
        consequence_contexts = _tuple(self.consequence_contexts, "projection consequence contexts", maximum=64)
        if any(type(item) is not MechanismEvaluatorContextGroupV1 for item in consequence_contexts):
            raise ValueError("projection consequence contexts are invalid")
        context_keys = tuple(mechanism_evaluator_context_key_v1(item.context) for item in consequence_contexts)
        if len(set(context_keys)) != len(context_keys):
            raise ValueError("projection consequence contexts must be semantically unique")
        evaluator_predictions = tuple(
            item for item in predictions if item.metric_id == "evaluator.exit_attribution_count"
        )
        if len(evaluator_predictions) > 1:
            raise ValueError("projection must retain at most one evaluator metric")
        evaluator_prediction = evaluator_predictions[0] if evaluator_predictions else None
        if consequence_contexts and evaluator_prediction is None:
            raise ValueError("projection consequence contexts require the declared evaluator metric")
        if evaluator_prediction is not None:
            evaluator_metric = MechanismMetricSpecV1(
                metric_id=evaluator_prediction.metric_id,
                unit=evaluator_prediction.unit,
                direction=evaluator_prediction.direction,
                tolerance=evaluator_prediction.tolerance,
                denominator=evaluator_prediction.denominator_kind,
                selector=evaluator_prediction.selector,
            )
            for group in consequence_contexts:
                context = group.context
                if (
                    context.evaluator_contract_sha256 != self.binding.evaluator_contract_sha256
                    or context.parent_policy_identity_sha256 != self.binding.parent_revision_sha256
                    or context.candidate_source_bundle_sha256 != self.binding.candidate_bytes_sha256
                    or context.scenario_id != self.binding.scenario_id
                ):
                    raise ValueError("projection consequence context identity differs from its binding")
                for result in group.predictions:
                    if (
                        result.metric_id != evaluator_prediction.metric_id
                        or result.unit != evaluator_prediction.unit
                        or result.direction != evaluator_prediction.direction
                        or result.denominator_kind != evaluator_prediction.denominator_kind
                        or result.selector != evaluator_prediction.selector
                        or result.tolerance != evaluator_prediction.tolerance
                        or result.minimum_relevant_cases != evaluator_prediction.minimum_relevant_cases
                    ):
                        raise ValueError("projection consequence context meaning differs from its evaluator metric")
        unavailable_reason = _expected_unavailable_reason(self.execution)
        if unavailable_reason is not None and any(
            item.availability != "unavailable" or item.unavailable_reason != unavailable_reason for item in predictions
        ):
            raise ValueError("projection execution status and prediction availability differ")
        if self.execution.status == "completed" and evaluator_prediction is not None:
            expected = aggregate_mechanism_evaluator_predictions_v1(
                evaluator_metric,
                tuple(group.predictions[0] for group in consequence_contexts),
                minimum_relevant_cases=evaluator_prediction.minimum_relevant_cases,
            )
            if evaluator_prediction != expected:
                raise ValueError("projection evaluator aggregate differs from retained consequence contexts")
        limitations = _tuple(self.limitations, "projection limitations", maximum=32)
        if any(type(item) is not str or not item.strip() or len(item) > 512 for item in limitations):
            raise ValueError("projection limitations are invalid")
        for value, label in (
            (self.critic_interpretation, "critic interpretation"),
            (self.critic_next_direction, "critic next direction"),
        ):
            if value is not None:
                _text(value, label, max_length=1024)
        if self.critic_interpretation_is_retrospective is not True:
            raise ValueError("critic interpretation must remain retrospective")
        if type(self.schema_version) is not int or self.schema_version != MECHANISM_SCHEMA_VERSION_V1:
            raise ValueError("learning projection schema version is unsupported")

    @classmethod
    def from_report(
        cls,
        spec: MechanismExperimentSpecV1,
        report: MechanismEvidenceReportV1,
        *,
        critic_interpretation: str | None = None,
        critic_next_direction: str | None = None,
    ) -> MechanismLearningProjectionV1:
        if type(spec) is not MechanismExperimentSpecV1 or type(report) is not MechanismEvidenceReportV1:
            raise ValueError("projection spec/report are invalid")
        validate_mechanism_report_against_spec_v1(spec, report)
        return cls(
            experiment_id=report.binding.experiment_id,
            hypothesis_id=spec.hypothesis_id,
            spec_sha256=spec.sha256,
            binding_sha256=report.binding.sha256,
            report_sha256=report.sha256,
            parent_revision_sha256=spec.parent_revision_sha256,
            binding=report.binding,
            execution=report.execution,
            controls=spec.controls,
            applicability=spec.applicability,
            coverage=report.coverage,
            predictions=report.predictions,
            limitations=report.limitations,
            consequence_contexts=report.consequence_contexts,
            critic_interpretation=critic_interpretation,
            critic_next_direction=critic_next_direction,
        )

    @classmethod
    def from_primitive(cls, value: object) -> MechanismLearningProjectionV1:
        raw = _require_fields(
            value,
            {
                "experiment_id",
                "hypothesis_id",
                "spec_sha256",
                "binding_sha256",
                "report_sha256",
                "parent_revision_sha256",
                "binding",
                "execution",
                "controls",
                "applicability",
                "coverage",
                "predictions",
                "limitations",
                "consequence_contexts",
                "critic_interpretation",
                "critic_next_direction",
                "critic_interpretation_is_retrospective",
                "schema_version",
            },
            "learning projection",
        )
        predictions = raw["predictions"]
        limitations = raw["limitations"]
        controls = raw["controls"]
        consequence_contexts = raw["consequence_contexts"]
        if (
            type(predictions) is not list
            or type(limitations) is not list
            or type(controls) is not list
            or type(consequence_contexts) is not list
        ):
            raise ValueError("learning projection collections are invalid")
        return cls(
            experiment_id=raw["experiment_id"],  # type: ignore[arg-type]
            hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
            spec_sha256=raw["spec_sha256"],  # type: ignore[arg-type]
            binding_sha256=raw["binding_sha256"],  # type: ignore[arg-type]
            report_sha256=raw["report_sha256"],  # type: ignore[arg-type]
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            binding=MechanismObservationBindingV1.from_primitive(raw["binding"]),
            execution=MechanismExecutionV1.from_primitive(raw["execution"]),
            controls=tuple(MechanismControlV1.from_primitive(item) for item in controls),
            applicability=MechanismPredicateV1.from_primitive(raw["applicability"]),
            coverage=MechanismCoverageV1.from_primitive(raw["coverage"]),
            predictions=tuple(MechanismPredictionResultV1.from_primitive(item) for item in predictions),
            limitations=tuple(limitations),
            consequence_contexts=tuple(
                MechanismEvaluatorContextGroupV1.from_primitive(item) for item in consequence_contexts
            ),
            critic_interpretation=raw["critic_interpretation"],  # type: ignore[arg-type]
            critic_next_direction=raw["critic_next_direction"],  # type: ignore[arg-type]
            critic_interpretation_is_retrospective=raw["critic_interpretation_is_retrospective"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


__all__ = [
    "MECHANISM_AGGREGATIONS_V1",
    "MECHANISM_CONSEQUENCE_AGGREGATIONS_V1",
    "MECHANISM_ALLOWED_SYMBOLS_V1",
    "MECHANISM_CONTROL_IDS_V1",
    "MECHANISM_DENOMINATORS_V1",
    "MECHANISM_DIAGNOSTIC_SECTIONS_V1",
    "MECHANISM_DIRECTIONS_V1",
    "MECHANISM_DISCONFIRMING_IDS_V1",
    "MECHANISM_EVALUATOR_STAGES_V1",
    "MECHANISM_EXIT_REASON_IDS_V1",
    "MECHANISM_INPUT_UNITS_V1",
    "MECHANISM_MAX_CANONICAL_JSON_BYTES_V1",
    "MECHANISM_MAX_CPU_SECONDS_V1",
    "MECHANISM_MAX_DECIMAL_DIGITS_V1",
    "MECHANISM_MAX_DECIMAL_EXPONENT_V1",
    "MECHANISM_MAX_DECIMAL_TEXT_BYTES_V1",
    "MECHANISM_METRIC_UNITS_V1",
    "MECHANISM_PROVENANCES_V1",
    "MECHANISM_RECIPE_IDS_V1",
    "MECHANISM_SCHEMA_VERSION_V1",
    "MECHANISM_TARGET_METHODS_V1",
    "MECHANISM_UNAVAILABLE_REASONS_V1",
    "MechanismAssessmentV1",
    "MechanismControlV1",
    "MechanismCoverageV1",
    "MechanismDiagnosticSelectorV1",
    "MechanismDisconfirmingObservationV1",
    "MechanismEvidenceReportV1",
    "MechanismEvaluatorContextGroupV1",
    "MechanismEvaluatorContextV1",
    "MechanismExecutionV1",
    "MechanismExperimentSpecV1",
    "MechanismLearningProjectionV1",
    "MechanismMetricSpecV1",
    "MechanismObservationBindingV1",
    "MechanismPredicateV1",
    "MechanismPredictionResultV1",
    "MechanismRecipeV1",
    "MechanismResourceBudgetV1",
    "MechanismResourceUsageV1",
    "aggregate_mechanism_evaluator_predictions_v1",
    "bind_mechanism_observation_v1",
    "mechanism_evaluator_context_key_v1",
    "validate_mechanism_observation_binding_v1",
    "validate_mechanism_report_against_spec_v1",
    "validate_mechanism_spec_hypothesis_v1",
]
