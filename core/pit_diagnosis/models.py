"""Immutable, closed types for the versioned PIT CANSLIM rulebook."""

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping
from datetime import date
import hashlib
import json


class _Closed(str, Enum):
    @classmethod
    def _missing_(cls, value):
        raise ValueError(f"invalid {cls.__name__}: {value!r}")


class RuleClassification(_Closed):
    REQUIRED = "required"
    ALLOWED_VARIANT = "allowed_variant"
    DIAGNOSTIC_ONLY = "diagnostic_only"


class Observability(_Closed):
    PIT_OBSERVED = "pit_observed"
    PIT_PROXY = "pit_proxy"
    PIT_UNAVAILABLE = "pit_unavailable"


class FidelityLabel(_Closed):
    STRICT_CANSLIM = "strict_canslim"
    QUANTITATIVE_CANSLIM_PROXY = "quantitative_canslim_proxy"
    FIDELITY_INCOMPLETE = "fidelity_incomplete"


class ImplementationStatus(_Closed):
    IMPLEMENTED = "implemented"
    PARTIAL = "partial"
    UNIMPLEMENTED = "unimplemented"


class PartitionName(_Closed):
    DISCOVERY = "discovery"
    VALIDATION = "validation"
    LOCKED_EVALUATION = "locked_evaluation"


class ExperimentKind(_Closed):
    REPRODUCTION = "reproduction"
    DATA = "data"
    ENTRY = "entry"
    MARKET = "market"
    EXIT = "exit"
    INTERACTION = "interaction"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class RuleSource:
    source_id: str
    title: str
    url: str
    source_location: str

    def __post_init__(self) -> None:
        for name in ("source_id", "title", "url", "source_location"):
            _text(getattr(self, name), name)


@dataclass(frozen=True)
class RuleRecord:
    rule_id: str
    letter_or_domain: str
    requirement: str
    classification: RuleClassification
    observability: Observability
    parameter_policy: Mapping[str, object]
    source_id: str
    source_location: str
    implementation_status: ImplementationStatus
    satisfaction_logic: str = "all"

    def __post_init__(self) -> None:
        for name in ("rule_id", "letter_or_domain", "requirement", "source_id", "source_location", "satisfaction_logic"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "classification", RuleClassification(self.classification))
        object.__setattr__(self, "observability", Observability(self.observability))
        object.__setattr__(self, "implementation_status", ImplementationStatus(self.implementation_status))
        if not isinstance(self.parameter_policy, Mapping):
            raise ValueError("parameter_policy must be a mapping")
        object.__setattr__(self, "parameter_policy", _freeze(self.parameter_policy))


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    status: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.rule_id, "rule_id")
        if self.status not in {"passed", "failed", "unavailable", "unimplemented"}:
            raise ValueError(f"invalid outcome status: {self.status!r}")
        if not isinstance(self.evidence_ids, tuple) or any(not isinstance(x, str) or not x for x in self.evidence_ids):
            raise ValueError("evidence_ids must be a tuple of non-empty strings")

    @classmethod
    def passed(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id, "passed", tuple(evidence_ids))

    @classmethod
    def failed(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id, "failed", tuple(evidence_ids))

    @classmethod
    def unavailable(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id, "unavailable", tuple(evidence_ids))

    @classmethod
    def unimplemented(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id, "unimplemented", tuple(evidence_ids))


@dataclass(frozen=True)
class Rulebook:
    version: str
    sources: Mapping[str, RuleSource]
    rules: Mapping[str, RuleRecord]
    sha256: str = ""

    def __post_init__(self) -> None:
        _text(self.version, "version")
        if not isinstance(self.sources, Mapping) or not isinstance(self.rules, Mapping):
            raise ValueError("sources and rules must be mappings")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character SHA-256 hex digest")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        for key, source in self.sources.items():
            if not isinstance(key, str) or not isinstance(source, RuleSource):
                raise ValueError("sources must map string IDs to RuleSource values")
            if key != source.source_id:
                raise ValueError("source key does not match source_id")
        for key, rule in self.rules.items():
            if not isinstance(key, str) or not isinstance(rule, RuleRecord):
                raise ValueError("rules must map string IDs to RuleRecord values")
            if key != rule.rule_id:
                raise ValueError("rule key does not match rule_id")
            if rule.source_id not in self.sources:
                raise ValueError(f"unknown source reference: {rule.source_id}")


@dataclass(frozen=True)
class FidelityAssessment:
    label: FidelityLabel
    passed_required_rule_ids: tuple[str, ...]
    failed_required_rule_ids: tuple[str, ...]
    unavailable_required_rule_ids: tuple[str, ...]
    proxy_rule_ids: tuple[str, ...]
    promotion_eligible: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", FidelityLabel(self.label))
        for name in ("passed_required_rule_ids", "failed_required_rule_ids", "unavailable_required_rule_ids", "proxy_rule_ids"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(x, str) for x in value):
                raise ValueError(f"{name} must be a tuple of strings")
        if not isinstance(self.promotion_eligible, bool):
            raise ValueError("promotion_eligible must be bool")


@dataclass(frozen=True)
class DatePartition:
    name: PartitionName | str
    start: str
    end: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", PartitionName(self.name))
        for field in ("start", "end"):
            value = getattr(self, field)
            try:
                parsed = date.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be an ISO date") from exc
            if parsed.isoformat() != value:
                raise ValueError(f"{field} must be YYYY-MM-DD")
        if self.start > self.end:
            raise ValueError("partition start must not be after end")

    def as_tuple(self) -> tuple[str, str]:
        return self.start, self.end


@dataclass(frozen=True)
class DatePartitions:
    discovery: DatePartition
    validation: DatePartition
    locked_evaluation: DatePartition

    def __post_init__(self) -> None:
        expected = ("discovery", "validation", "locked_evaluation")
        actual = tuple(item.name.value for item in (self.discovery, self.validation, self.locked_evaluation))
        if actual != expected:
            raise ValueError("date partitions must be discovery, validation, locked_evaluation")
        if self.discovery.end >= self.validation.start or self.validation.end >= self.locked_evaluation.start:
            raise ValueError("date partitions must be chronological and non-overlapping")


@dataclass(frozen=True)
class ExperimentDefinition:
    experiment_id: str
    phase: str
    domain: str
    kind: ExperimentKind | str
    changed_dimensions: tuple[str, ...]
    rule_ids: tuple[str, ...]
    promotion_eligible: bool
    controller_composed: bool
    requires_code: bool
    allowed_variant_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("experiment_id", "phase", "domain"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "kind", ExperimentKind(self.kind))
        for name in ("changed_dimensions", "rule_ids", "allowed_variant_ids"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError(f"{name} must be a tuple of non-empty strings")
            if len(set(value)) != len(value):
                raise ValueError(f"{name} must not contain duplicates")
        for name in ("promotion_eligible", "controller_composed", "requires_code"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")

    @classmethod
    def from_mapping(cls, item: Mapping[str, object], rulebook: Rulebook) -> "ExperimentDefinition":
        expected = {"experiment_id", "phase", "domain", "kind", "changed_dimensions", "rule_ids", "promotion_eligible", "controller_composed", "requires_code", "allowed_variant_ids"}
        if not isinstance(item, Mapping) or set(item) != expected:
            raise ValueError("experiment record has unexpected fields")
        values = dict(item)
        for field in ("changed_dimensions", "rule_ids", "allowed_variant_ids"):
            if not isinstance(values[field], list):
                raise ValueError(f"{field} must be a JSON array")
            values[field] = tuple(values[field])
        result = cls(**values)
        if result.phase != "D5" and len(result.changed_dimensions) != 1:
            raise ValueError("D0-D4 experiments must change exactly one causal dimension")
        missing = set(result.rule_ids) - set(rulebook.rules)
        if missing:
            raise ValueError(f"experiment cites absent rule IDs: {sorted(missing)}")
        if any(rulebook.rules[rid].classification is RuleClassification.DIAGNOSTIC_ONLY for rid in result.rule_ids) and result.promotion_eligible:
            raise ValueError("diagnostic_only rule cannot be promotable")
        if result.phase == "D5" and not result.controller_composed:
            raise ValueError("D5 experiments must be controller composed")
        if result.controller_composed and result.phase != "D5":
            raise ValueError("only D5 experiments may be controller composed")
        return result


@dataclass(frozen=True)
class ExperimentCatalog:
    version: str
    _experiments: Mapping[str, ExperimentDefinition]
    sha256: str

    @classmethod
    def from_records(cls, version: str, records: tuple[ExperimentDefinition, ...], sha256: str) -> "ExperimentCatalog":
        if not records or len({record.experiment_id for record in records}) != len(records):
            raise ValueError("experiment IDs must be unique and non-empty")
        return cls(version, MappingProxyType({record.experiment_id: record for record in records}), sha256)

    @property
    def experiments(self) -> Mapping[str, ExperimentDefinition]:
        return self._experiments

    def __getitem__(self, key: str) -> ExperimentDefinition:
        return self._experiments[key]


@dataclass(frozen=True)
class ExperimentIdentity:
    fields: Mapping[str, object]
    sha256: str

    @classmethod
    def from_fields(cls, fields: Mapping[str, object]) -> "ExperimentIdentity":
        def normalize(value):
            if isinstance(value, Enum): return value.value
            if isinstance(value, DatePartition): return {"name": value.name.value, "start": value.start, "end": value.end}
            if isinstance(value, ExperimentDefinition): return value.experiment_id
            if isinstance(value, Mapping): return {str(k): normalize(v) for k, v in value.items() if k != "fields"}
            if isinstance(value, (tuple, list)): return [normalize(v) for v in value]
            return value
        normalized = normalize(dict(fields))
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return cls(MappingProxyType(normalized), hashlib.sha256(encoded).hexdigest())
