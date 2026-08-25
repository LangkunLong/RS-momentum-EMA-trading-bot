"""Immutable, closed types for the versioned PIT CANSLIM rulebook."""

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping


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
