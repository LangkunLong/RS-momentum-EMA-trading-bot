"""Closed provider protocols for the quarantined PIT diagnosis controller.

This module deliberately contains no data loading, experiment execution, source access, or
provider client.  The controller supplies a small, deterministic evidence envelope; the role
protocols can only cite identifiers already present in that envelope.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from agent_loop import (
    BudgetLedger,
    ConfigurationError,
    ExactLineReplacement,
    PayloadFieldValidationError,
    PayloadKeysValidationError,
    ProtocolValidationError,
    _MAX_LIST_ITEMS,
    _MAX_PROVIDER_EVIDENCE_BYTES,
    _MAX_TEXT_BYTES,
    _optional_text,
    _parse_json_object,
    _required_text,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CLOSED_ID_RE = re.compile(r"[A-Z][A-Z0-9_.-]{0,127}")
_METRIC_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FIDELITY_LABELS = frozenset(
    {"strict_canslim", "quantitative_canslim_proxy", "fidelity_incomplete"}
)
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "raw",
        "rows",
        "transactions",
        "prices",
        "fundamentals",
        "payload",
        "secret",
        "path",
        "source_text",
    }
)


def _absolute_regular_path(value: object, field: str, *, directory: bool = False) -> Path:
    """Validate a controller input path without following a user supplied link."""
    if not isinstance(value, Path) or not value.is_absolute():
        raise ConfigurationError(f"{field} must be an absolute Path")
    try:
        info = value.lstat()
    except OSError as exc:
        raise ConfigurationError(f"{field} must exist") from exc
    if value.is_symlink() or not value.exists():
        raise ConfigurationError(f"{field} must be a regular non-symlink path")
    if directory and not value.is_dir():
        raise ConfigurationError(f"{field} must be a regular directory")
    if not directory and not value.is_file():
        raise ConfigurationError(f"{field} must be a regular file")
    if not directory and not os.path.isfile(value):
        raise ConfigurationError(f"{field} must be a regular file")
    if directory and not os.path.isdir(value):
        raise ConfigurationError(f"{field} must be a regular directory")
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise ConfigurationError(f"{field} must not be a reparse point")
    return value.resolve()


def _absolute_output_path(value: object, field: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ConfigurationError(f"{field} must be an absolute Path")
    if value.exists() and (value.is_symlink() or not value.is_dir()):
        raise ConfigurationError(f"{field} must be a regular directory when present")
    return value.resolve(strict=False)


@dataclass(frozen=True)
class PitDiagnosisGateConfig:
    """Sealed identities and hard limits for one quarantined PIT diagnosis sample.

    The five immutable input files are deliberately separate from ``SourceState``.  A
    diagnosis worker may read their controller-owned snapshots, but it cannot resolve a
    path supplied by a model or use the live checkout as its data source.
    """

    diagnosis_run: Path
    diagnosis_manifest_sha256: str
    pit_bundle: Path
    pit_bundle_sha256: str
    fact_cache: Path
    fact_cache_sha256: str
    rulebook: Path
    rulebook_sha256: str
    experiment_catalog: Path
    experiment_catalog_sha256: str
    # The baseline replay is deliberately separate from the published diagnosis
    # directory.  Deterministic D1-D4 workers need the hash-bound authority snapshot
    # but must receive it only as a read-only worker mount.
    baseline_run: Path | None = None
    checkpoint_root: Path | None = None
    output_root: Path | None = None
    partition: str = "discovery"
    max_usd: float = 0.50
    max_api_calls: int = 3
    max_tokens: int = 32_768
    wall_timeout_seconds: float = 3_600.0
    child_timeout_seconds: float = 300.0
    output_limit_bytes: int = 1_048_576
    apply: bool = False

    def __post_init__(self) -> None:
        paths = (
            ("diagnosis_run", self.diagnosis_run, True),
            ("pit_bundle", self.pit_bundle, False),
            ("fact_cache", self.fact_cache, False),
            ("rulebook", self.rulebook, False),
            ("experiment_catalog", self.experiment_catalog, False),
        )
        for field, value, directory in paths:
            object.__setattr__(self, field, _absolute_regular_path(value, field, directory=directory))
        if self.baseline_run is not None:
            object.__setattr__(
                self,
                "baseline_run",
                _absolute_regular_path(self.baseline_run, "baseline_run", directory=True),
            )
        if self.checkpoint_root is not None:
            object.__setattr__(
                self,
                "checkpoint_root",
                _absolute_output_path(self.checkpoint_root, "checkpoint_root"),
            )
        if self.output_root is not None:
            object.__setattr__(self, "output_root", _absolute_output_path(self.output_root, "output_root"))
        for field in (
            "diagnosis_manifest_sha256",
            "pit_bundle_sha256",
            "fact_cache_sha256",
            "rulebook_sha256",
            "experiment_catalog_sha256",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ConfigurationError(f"{field} must be a lowercase SHA-256")
        if self.partition not in {"discovery", "validation"}:
            raise ConfigurationError("PIT diagnosis partition must be discovery or validation")
        if type(self.max_api_calls) is not int or not 1 <= self.max_api_calls <= 3:
            raise ConfigurationError("PIT diagnosis permits at most three provider calls")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 131_072:
            raise ConfigurationError("PIT diagnosis token limit is invalid")
        if type(self.max_usd) not in {int, float} or not math.isfinite(self.max_usd) or not 0 < self.max_usd <= 0.50:
            raise ConfigurationError("PIT diagnosis USD limit must be finite and at most 0.50")
        for field in ("wall_timeout_seconds", "child_timeout_seconds"):
            value = getattr(self, field)
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{field} must be finite and positive")
        if type(self.output_limit_bytes) is not int or not 1 <= self.output_limit_bytes <= 4 * 1024 * 1024:
            raise ConfigurationError("output_limit_bytes is invalid")
        if type(self.apply) is not bool or self.apply:
            raise ConfigurationError("PIT diagnosis cannot apply patches outside its disposable candidate")


@dataclass(frozen=True)
class PitDiagnosisLoopServices:
    """Injectable deterministic/worker boundaries for PIT gate verification.

    The PIT gate deliberately does not accept the legacy free-form gateway.  A service
    must expose the isolated prompt-family method and the deterministic worker
    boundaries must return explicit isolation attestations.  This keeps a convenient
    fake useful for protocol tests without allowing an unconfined callable to look
    like a production worker.
    """

    gateway: object
    verify_diagnosis_run: Callable[[Path], Mapping[str, object]] | None = None
    build_evidence: Callable[..., "PitAgentEvidence"] | "PitAgentEvidence" | None = None
    run_experiment: Callable[..., object] | None = None
    run_deterministic_experiment: Callable[..., object] | None = None
    run_quality: Callable[..., object] | None = None
    read_snapshots: Callable[..., tuple[object, ...]] | None = None
    compile_runner: Callable[..., bool] | None = None
    allowed_replacements: Mapping[str, Sequence[ExactLineReplacement]] | Sequence[ExactLineReplacement] = ()
    editable_paths: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    invariant_ids: tuple[str, ...] = ()
    monotonic: Callable[[], float] | None = None
    known_secrets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        gateway = self.gateway
        if not callable(getattr(gateway, "request_pit_diagnosis_once", None)):
            raise ConfigurationError("PIT diagnosis gateway must expose request_pit_diagnosis_once")
        if not isinstance(getattr(gateway, "ledger", None), BudgetLedger):
            raise ConfigurationError("PIT diagnosis gateway must expose bounded accounting")
        if not isinstance(self.known_secrets, tuple) or any(not isinstance(value, str) for value in self.known_secrets):
            raise ConfigurationError("PIT diagnosis secrets must be an immutable string tuple")
        if not isinstance(self.editable_paths, tuple) or len(self.editable_paths) > 8:
            raise ConfigurationError("PIT diagnosis editable paths must be a bounded tuple")
        if any(not isinstance(value, str) or not value for value in self.editable_paths):
            raise ConfigurationError("PIT diagnosis editable paths must be non-empty strings")
        for name, values in (("evidence_ids", self.evidence_ids), ("rule_ids", self.rule_ids), ("invariant_ids", self.invariant_ids)):
            if not isinstance(values, tuple) or len(values) > _MAX_LIST_ITEMS or any(not isinstance(value, str) or not value for value in values):
                raise ConfigurationError(f"{name} must be a bounded immutable tuple")
        if self.monotonic is not None and not callable(self.monotonic):
            raise ConfigurationError("PIT diagnosis monotonic service must be callable")
        replacements = self.allowed_replacements
        if isinstance(replacements, Mapping):
            normalized: dict[str, tuple[ExactLineReplacement, ...]] = {}
            for key, values in replacements.items():
                if not isinstance(key, str) or not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                    raise ConfigurationError("PIT allowed replacements are malformed")
                normalized[key] = tuple(values)
        else:
            if not isinstance(replacements, Sequence) or isinstance(replacements, (str, bytes)):
                raise ConfigurationError("PIT allowed replacements are malformed")
            normalized = {"*": tuple(replacements)}
        if any(not isinstance(value, ExactLineReplacement) for values in normalized.values() for value in values):
            raise ConfigurationError("PIT allowed replacements must be exact line replacements")
        object.__setattr__(self, "allowed_replacements", MappingProxyType(normalized))

    def replacements_for(self, experiment_id: str) -> tuple[ExactLineReplacement, ...]:
        values = self.allowed_replacements
        assert isinstance(values, Mapping)
        return tuple(values.get(experiment_id, values.get("*", ())))


@dataclass(frozen=True)
class PitDiagnosisLoopResult:
    """Closed, aggregate-only terminal facts from one PIT diagnosis controller run."""

    terminal_status: str
    selected_experiment_id: str | None = None
    coder_called: bool = False
    source_modified: bool = False
    exported_diff_sha256: str | None = None
    deterministic_result_sha256: str | None = None
    diagnosis_result_sha256: str | None = None
    derivative_result_path: Path | None = None
    audit_path: Path | None = None
    run_id: str = "pit-diagnosis"
    call_record_sha256s: tuple[str, ...] = ()
    cleanup_complete: bool = False
    worker_confined: bool = False
    locked_metrics_excluded: bool = False
    d0_passed: bool = False
    failure_code: str = "none"

    def __post_init__(self) -> None:
        allowed = {
            "completed",
            "protocol_rejected",
            "d0_failed",
            "aborted",
            "budget_exceeded",
            "worker_failed",
            "source_modified",
            "controller_error",
        }
        if self.terminal_status not in allowed:
            raise ConfigurationError("PIT diagnosis terminal status is invalid")
        if self.selected_experiment_id is not None and (not isinstance(self.selected_experiment_id, str) or not self.selected_experiment_id):
            raise ConfigurationError("PIT selected experiment ID is invalid")
        for field in ("coder_called", "source_modified", "cleanup_complete", "worker_confined", "locked_metrics_excluded", "d0_passed"):
            if type(getattr(self, field)) is not bool:
                raise ConfigurationError(f"PIT result {field} must be boolean")
        for field in ("exported_diff_sha256", "deterministic_result_sha256", "diagnosis_result_sha256"):
            value = getattr(self, field)
            if value is not None and _SHA256_RE.fullmatch(value) is None:
                raise ConfigurationError(f"PIT result {field} must be lowercase SHA-256")
        if not isinstance(self.call_record_sha256s, tuple) or len(self.call_record_sha256s) > 3 or any(_SHA256_RE.fullmatch(value or "") is None for value in self.call_record_sha256s):
            raise ConfigurationError("PIT call record hashes are invalid")
        if self.derivative_result_path is not None and (not isinstance(self.derivative_result_path, Path) or not self.derivative_result_path.is_absolute()):
            raise ConfigurationError("PIT derivative result path must be absolute")
        if self.audit_path is not None and (not isinstance(self.audit_path, Path) or not self.audit_path.is_absolute()):
            raise ConfigurationError("PIT audit path must be absolute")
        if self.terminal_status == "completed" and (
            not self.worker_confined
            or not self.cleanup_complete
            or self.source_modified
            or not self.d0_passed
            or not self.locked_metrics_excluded
        ):
            raise ConfigurationError("completed PIT result lacks required boundary attestations")


class PitDomain(str, Enum):
    """The closed diagnosis domains that an orchestrator may route."""

    DATA = "data"
    ENTRY = "entry"
    EXECUTION = "execution"
    EXIT = "exit"
    MARKET = "market"
    PORTFOLIO = "portfolio"


def _closed_id(value: object, field: str) -> str:
    text = _required_text(value, field, max_bytes=256)
    if _CLOSED_ID_RE.fullmatch(text) is None:
        raise PayloadFieldValidationError(f"{field} must be a closed identifier")
    return text


def _closed_id_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise PayloadFieldValidationError(f"{field} must be a bounded list")
    result = tuple(_closed_id(item, field) for item in value)
    if len(set(result)) != len(result):
        raise PayloadFieldValidationError(f"{field} must not contain duplicates")
    if result != tuple(sorted(result)):
        raise PayloadFieldValidationError(f"{field} must be canonically sorted")
    return result


def _closed_id_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise PayloadFieldValidationError(f"{field} must be an immutable tuple")
    return _closed_id_list(list(value), field)


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PayloadFieldValidationError(f"{field} must be a lowercase SHA-256")
    return value


def _mapping(value: object, field: str, *, maximum: int = _MAX_LIST_ITEMS) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or len(value) > maximum:
        raise PayloadFieldValidationError(f"{field} must be a bounded mapping")
    return value


def _reject_forbidden_evidence_keys(value: object) -> None:
    """Keep raw data and source-bearing fields out of the provider evidence envelope."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise PayloadKeysValidationError("PIT evidence keys must be strings")
            if key.casefold() in _FORBIDDEN_EVIDENCE_KEYS:
                raise PayloadKeysValidationError(f"PIT evidence forbids key: {key}")
            _reject_forbidden_evidence_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_forbidden_evidence_keys(nested)


def _freeze_json_schema(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_schema(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_schema(item) for item in value)
    return value


def _plain_json_schema(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json_schema(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json_schema(item) for item in value]
    return value


def _validate_provider_evidence_payload(payload: Mapping[str, object]) -> None:
    """Reject unsafe or oversized data before it can reach a provider client."""
    _reject_forbidden_evidence_keys(payload)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PayloadFieldValidationError("PIT evidence must be JSON serializable") from exc
    if len(encoded) > _MAX_PROVIDER_EVIDENCE_BYTES:
        raise PayloadFieldValidationError("PIT evidence exceeds the provider byte limit")


@dataclass(frozen=True)
class PitRoute:
    """A route-only response; it intentionally has no free-text reasoning fields."""

    action: str
    domain: PitDomain
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action not in {"reason", "abort"}:
            raise PayloadFieldValidationError("action must be reason or abort")
        if not isinstance(self.domain, PitDomain):
            raise PayloadFieldValidationError("domain must be a PIT domain")
        evidence_ids = _closed_id_tuple(self.evidence_ids, "evidence_ids")
        if self.action == "reason" and not evidence_ids:
            raise PayloadFieldValidationError("reason routes require evidence_ids")
        if self.action == "abort" and evidence_ids:
            raise PayloadFieldValidationError("abort routes require empty evidence_ids")
        object.__setattr__(self, "evidence_ids", evidence_ids)

    @classmethod
    def from_json(cls, raw: str) -> "PitRoute":
        value = _parse_json_object(
            raw,
            {"action", "domain", "evidence_ids"},
            max_bytes=_MAX_TEXT_BYTES,
        )
        action = _required_text(value["action"], "action", max_bytes=32)
        try:
            domain = PitDomain(_required_text(value["domain"], "domain", max_bytes=32))
        except ValueError as exc:
            raise PayloadFieldValidationError("domain must be a PIT domain") from exc
        return cls(action=action, domain=domain, evidence_ids=_closed_id_list(value["evidence_ids"], "evidence_ids"))


@dataclass(frozen=True)
class PitReasoningPlan:
    """One bounded, falsifiable hypothesis tied only to supplied controller IDs."""

    causal_hypothesis: str
    evidence_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    experiment_id: str
    skip: bool
    skip_reason: str

    def __post_init__(self) -> None:
        _required_text(self.causal_hypothesis, "causal_hypothesis")
        evidence_ids = _closed_id_tuple(self.evidence_ids, "evidence_ids")
        rule_ids = _closed_id_tuple(self.rule_ids, "rule_ids")
        invariant_ids = _closed_id_tuple(self.invariant_ids, "invariant_ids")
        experiment_id = _optional_text(self.experiment_id, "experiment_id", max_bytes=256)
        if experiment_id:
            experiment_id = _closed_id(experiment_id, "experiment_id")
        if type(self.skip) is not bool:
            raise PayloadFieldValidationError("skip must be a boolean")
        skip_reason = _optional_text(self.skip_reason, "skip_reason")
        if self.skip:
            if experiment_id:
                raise PayloadFieldValidationError("skip plans must not select an experiment")
            if not skip_reason.strip():
                raise PayloadFieldValidationError("skip_reason must not be blank when skip is true")
        else:
            if len(evidence_ids) != 1:
                raise PayloadFieldValidationError("non-skip plans require exactly one evidence ID")
            if len(rule_ids) != 1:
                raise PayloadFieldValidationError("non-skip plans require exactly one rule ID")
            if len(invariant_ids) != 1:
                raise PayloadFieldValidationError("non-skip plans require exactly one invariant ID")
            if not experiment_id:
                raise PayloadFieldValidationError("non-skip plans require an experiment")
            if skip_reason:
                raise PayloadFieldValidationError("skip_reason must be empty when skip is false")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "rule_ids", rule_ids)
        object.__setattr__(self, "invariant_ids", invariant_ids)
        object.__setattr__(self, "experiment_id", experiment_id)
        object.__setattr__(self, "skip_reason", skip_reason)

    @classmethod
    def from_json(cls, raw: str) -> "PitReasoningPlan":
        value = _parse_json_object(
            raw,
            {
                "causal_hypothesis",
                "evidence_ids",
                "rule_ids",
                "invariant_ids",
                "experiment_id",
                "skip",
                "skip_reason",
            },
            max_bytes=_MAX_TEXT_BYTES,
        )
        if type(value["skip"]) is not bool:
            raise PayloadFieldValidationError("skip must be a boolean")
        return cls(
            causal_hypothesis=_required_text(value["causal_hypothesis"], "causal_hypothesis"),
            evidence_ids=_closed_id_list(value["evidence_ids"], "evidence_ids"),
            rule_ids=_closed_id_list(value["rule_ids"], "rule_ids"),
            invariant_ids=_closed_id_list(value["invariant_ids"], "invariant_ids"),
            experiment_id=_optional_text(value["experiment_id"], "experiment_id", max_bytes=256),
            skip=value["skip"],
            skip_reason=_optional_text(value["skip_reason"], "skip_reason"),
        )


@dataclass(frozen=True)
class PitAgentEvidence:
    """The only deterministic facts that may cross the provider boundary."""

    diagnosis_run_sha256: str
    pit_bundle_sha256: str
    fact_cache_sha256: str
    rulebook_sha256: str
    experiment_catalog_sha256: str
    experiment_result_sha256s: Mapping[str, str]
    metrics: Mapping[str, float | int]
    evidence_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    experiment_ids: tuple[str, ...]
    fidelity_label: str
    promotion_eligible: bool
    partition: str = "discovery"
    experiment_partition_result_sha256s: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        for field in (
            "diagnosis_run_sha256",
            "pit_bundle_sha256",
            "fact_cache_sha256",
            "rulebook_sha256",
            "experiment_catalog_sha256",
        ):
            _sha256(getattr(self, field), field)
        if self.partition not in {"discovery", "validation"}:
            raise PayloadFieldValidationError("evidence partition is invalid")
        hashes = _mapping(self.experiment_result_sha256s, "experiment_result_sha256s")
        _reject_forbidden_evidence_keys(hashes)
        if tuple(hashes) != tuple(sorted(hashes)):
            raise PayloadFieldValidationError("experiment_result_sha256s must be canonically sorted")
        canonical_hashes: dict[str, str] = {}
        for experiment_id, value in hashes.items():
            canonical_hashes[_closed_id(experiment_id, "experiment result ID")] = _sha256(
                value, "experiment result SHA-256"
            )
        metrics = _mapping(self.metrics, "metrics", maximum=64)
        _reject_forbidden_evidence_keys(metrics)
        if tuple(metrics) != tuple(sorted(metrics)):
            raise PayloadFieldValidationError("metrics must be canonically sorted")
        canonical_metrics: dict[str, float | int] = {}
        for name, value in metrics.items():
            if not isinstance(name, str) or _METRIC_NAME_RE.fullmatch(name) is None:
                raise PayloadFieldValidationError("metric name is invalid")
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise PayloadFieldValidationError("metric values must be finite numbers")
            canonical_metrics[name] = value
        evidence_ids = _closed_id_tuple(self.evidence_ids, "evidence_ids")
        rule_ids = _closed_id_tuple(self.rule_ids, "rule_ids")
        invariant_ids = _closed_id_tuple(self.invariant_ids, "invariant_ids")
        experiment_ids = _closed_id_tuple(self.experiment_ids, "experiment_ids")
        if tuple(canonical_hashes) != experiment_ids:
            raise PayloadFieldValidationError("experiment IDs must exactly match experiment hashes")
        partition_hashes_input = self.experiment_partition_result_sha256s
        if partition_hashes_input is None:
            partition_hashes_input = {
                f"{experiment_id}@{self.partition}": value
                for experiment_id, value in canonical_hashes.items()
            }
        partition_hashes = _mapping(
            partition_hashes_input,
            "experiment_partition_result_sha256s",
        )
        if tuple(partition_hashes) != tuple(sorted(partition_hashes)):
            raise PayloadFieldValidationError(
                "experiment_partition_result_sha256s must be canonically sorted"
            )
        canonical_partition_hashes: dict[str, str] = {}
        for key, value in partition_hashes.items():
            if not isinstance(key, str) or key.count("@") != 1:
                raise PayloadFieldValidationError("experiment partition result key is invalid")
            experiment_id, partition = key.rsplit("@", 1)
            experiment_id = _closed_id(experiment_id, "experiment partition result ID")
            if partition not in {"discovery", "validation"}:
                raise PayloadFieldValidationError("experiment partition result partition is invalid")
            if experiment_id not in experiment_ids:
                raise PayloadFieldValidationError("experiment partition result ID is not selected")
            canonical_key = f"{experiment_id}@{partition}"
            if canonical_key != key:
                raise PayloadFieldValidationError("experiment partition result key is not canonical")
            canonical_partition_hashes[canonical_key] = _sha256(
                value,
                "experiment partition result SHA-256",
            )
        if not canonical_partition_hashes:
            raise PayloadFieldValidationError("experiment partition result hashes cannot be empty")
        selected_keys = {
            f"{experiment_id}@{self.partition}" for experiment_id in experiment_ids
        }
        if not selected_keys.issubset(canonical_partition_hashes):
            raise PayloadFieldValidationError(
                "evidence is missing a result hash for its selected partition"
            )
        # ``experiment_result_sha256s`` is the selected-partition projection used
        # by the provider envelope.  Keep the full partition map only as an audit
        # cross-check; allowing these two views to disagree would let the reasoner
        # cite one result while the controller later executes another.
        for experiment_id, result_hash in canonical_hashes.items():
            partition_hash = canonical_partition_hashes.get(
                f"{experiment_id}@{self.partition}"
            )
            if partition_hash != result_hash:
                raise PayloadFieldValidationError(
                    "selected experiment result hash disagrees with its partition result hash"
                )
        if self.fidelity_label not in _FIDELITY_LABELS:
            raise PayloadFieldValidationError("fidelity_label is invalid")
        if type(self.promotion_eligible) is not bool:
            raise PayloadFieldValidationError("promotion_eligible must be a boolean")
        object.__setattr__(self, "experiment_result_sha256s", MappingProxyType(canonical_hashes))
        object.__setattr__(
            self,
            "experiment_partition_result_sha256s",
            MappingProxyType(canonical_partition_hashes),
        )
        object.__setattr__(self, "metrics", MappingProxyType(canonical_metrics))
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "rule_ids", rule_ids)
        object.__setattr__(self, "invariant_ids", invariant_ids)
        object.__setattr__(self, "experiment_ids", experiment_ids)
        _validate_provider_evidence_payload(self.to_provider_payload())

    @classmethod
    def from_json(cls, raw: str) -> "PitAgentEvidence":
        value = _parse_json_object(
            raw,
            {
                "diagnosis_run_sha256",
                "pit_bundle_sha256",
                "fact_cache_sha256",
                "rulebook_sha256",
                "experiment_catalog_sha256",
                "experiment_result_sha256s",
                "experiment_partition_result_sha256s",
                "metrics",
                "evidence_ids",
                "rule_ids",
                "invariant_ids",
                "experiment_ids",
                "fidelity_label",
                "promotion_eligible",
                "partition",
            },
            max_bytes=_MAX_PROVIDER_EVIDENCE_BYTES,
        )
        _reject_forbidden_evidence_keys(value)
        if type(value["promotion_eligible"]) is not bool:
            raise PayloadFieldValidationError("promotion_eligible must be a boolean")
        return cls(
            diagnosis_run_sha256=value["diagnosis_run_sha256"],
            pit_bundle_sha256=value["pit_bundle_sha256"],
            fact_cache_sha256=value["fact_cache_sha256"],
            rulebook_sha256=value["rulebook_sha256"],
            experiment_catalog_sha256=value["experiment_catalog_sha256"],
            experiment_result_sha256s=_mapping(value["experiment_result_sha256s"], "experiment_result_sha256s"),
            metrics=_mapping(value["metrics"], "metrics", maximum=64),
            evidence_ids=_closed_id_list(value["evidence_ids"], "evidence_ids"),
            rule_ids=_closed_id_list(value["rule_ids"], "rule_ids"),
            invariant_ids=_closed_id_list(value["invariant_ids"], "invariant_ids"),
            experiment_ids=_closed_id_list(value["experiment_ids"], "experiment_ids"),
            fidelity_label=_required_text(value["fidelity_label"], "fidelity_label", max_bytes=128),
            promotion_eligible=value["promotion_eligible"],
            partition=_required_text(value["partition"], "partition", max_bytes=32),
            experiment_partition_result_sha256s=_mapping(
                value["experiment_partition_result_sha256s"],
                "experiment_partition_result_sha256s",
            ),
        )

    def to_provider_payload(self) -> dict[str, object]:
        """Return a JSON-safe, data-free copy of the closed evidence envelope."""
        return {
            "diagnosis_run_sha256": self.diagnosis_run_sha256,
            "pit_bundle_sha256": self.pit_bundle_sha256,
            "fact_cache_sha256": self.fact_cache_sha256,
            "rulebook_sha256": self.rulebook_sha256,
            "experiment_catalog_sha256": self.experiment_catalog_sha256,
            "experiment_result_sha256s": dict(self.experiment_result_sha256s),
            "experiment_partition_result_sha256s": dict(self.experiment_partition_result_sha256s),
            "metrics": dict(self.metrics),
            "evidence_ids": list(self.evidence_ids),
            "rule_ids": list(self.rule_ids),
            "invariant_ids": list(self.invariant_ids),
            "experiment_ids": list(self.experiment_ids),
            "fidelity_label": self.fidelity_label,
            "promotion_eligible": self.promotion_eligible,
            "partition": self.partition,
        }


def validate_pit_route(route: PitRoute, evidence: PitAgentEvidence) -> None:
    """Reject a route that cites facts outside the controller-provided evidence set."""
    if not isinstance(route, PitRoute) or not isinstance(evidence, PitAgentEvidence):
        raise ProtocolValidationError("PIT route grounding requires closed protocol types")
    if not set(route.evidence_ids).issubset(evidence.evidence_ids):
        raise ProtocolValidationError("route cites unknown evidence")


def validate_pit_reasoning_plan(plan: PitReasoningPlan, evidence: PitAgentEvidence) -> None:
    """Ground every reasoner citation and experiment in controller-owned closed IDs."""
    if not isinstance(plan, PitReasoningPlan) or not isinstance(evidence, PitAgentEvidence):
        raise ProtocolValidationError("PIT reasoning grounding requires closed protocol types")
    if not set(plan.evidence_ids).issubset(evidence.evidence_ids):
        raise ProtocolValidationError("reasoner cites unknown evidence")
    if not set(plan.rule_ids).issubset(evidence.rule_ids):
        raise ProtocolValidationError("reasoner cites unknown rule")
    if not set(plan.invariant_ids).issubset(evidence.invariant_ids):
        raise ProtocolValidationError("reasoner cites unknown invariant")
    if not plan.skip and plan.experiment_id not in evidence.experiment_ids:
        raise ProtocolValidationError("reasoner selected an unknown experiment")


@dataclass(frozen=True)
class PitAgentEvent:
    """A compact linkage record; provider content remains in the controller audit root."""

    event_type: str
    timestamp_utc: str
    role: str
    experiment_id: str
    outcome: str
    call_record_sha256: str
    deterministic_result_sha256: str

    def __post_init__(self) -> None:
        if self.event_type not in {"route", "reason", "coder", "terminal"}:
            raise PayloadFieldValidationError("PIT event type is invalid")
        if self.role not in {"orchestrator", "reasoner", "coder", "controller"}:
            raise PayloadFieldValidationError("PIT event role is invalid")
        if self.outcome not in {"accepted", "rejected", "skipped", "aborted", "completed"}:
            raise PayloadFieldValidationError("PIT event outcome is invalid")
        _closed_id(self.experiment_id, "experiment_id")
        _sha256(self.call_record_sha256, "call_record_sha256")
        _sha256(self.deterministic_result_sha256, "deterministic_result_sha256")
        if not isinstance(self.timestamp_utc, str):
            raise PayloadFieldValidationError("timestamp_utc must be a string")
        try:
            parsed = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PayloadFieldValidationError("timestamp_utc must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise PayloadFieldValidationError("timestamp_utc must be UTC")

    def to_primitive(self) -> dict[str, str]:
        return {
            "event_type": self.event_type,
            "timestamp_utc": self.timestamp_utc,
            "role": self.role,
            "experiment_id": self.experiment_id,
            "outcome": self.outcome,
            "call_record_sha256": self.call_record_sha256,
            "deterministic_result_sha256": self.deterministic_result_sha256,
        }


PIT_DIAGNOSIS_SYSTEM_PROMPTS = MappingProxyType(
    {
        "orchestrator": (
            "You are the PIT Diagnosis Orchestrator. Route only. Return exactly one JSON object with exactly "
            'these keys: "action", "domain", "evidence_ids". action must be "reason" or "abort"; '
            "domain must be one supplied PIT domain; evidence_ids must be sorted unique IDs selected only from "
            "the supplied evidence_ids. reason requires at least one evidence ID; abort requires an empty evidence "
            "list. Do not summarize, hypothesize, choose an experiment or parameter, name files, retrieve facts, "
            "issue commands, reveal chain-of-thought, add keys, or include prose."
        ),
        "reasoner": (
            "You are the PIT Diagnosis Reasoner. Return exactly one JSON object with exactly these keys: "
            '"causal_hypothesis", "evidence_ids", "rule_ids", "invariant_ids", "experiment_id", "skip", '
            '"skip_reason". Return one concise falsifiable hypothesis supported only by supplied closed metrics and '
            "cited IDs. For skip=false, cite exactly one sorted supplied evidence ID, rule ID, and invariant ID, "
            "and choose exactly one supplied experiment_id; skip_reason must be empty. For skip=true, "
            "experiment_id must be empty and skip_reason must be nonblank. "
            "Do not invent facts, rules, thresholds, files, commands, experiments, retrieval, external knowledge, "
            "or chain-of-thought. Do not recommend a diagnostic-only experiment for promotion. Return JSON only."
        ),
        "coder": (
            "You are the PIT Diagnosis Coder. Return exactly one JSON object with exactly these keys: "
            '"summary", "replacements". replacements must contain exactly one controller-owned replacement with '
            "exactly path, old_lines, and new_lines supplied by the controller. Do not select a file, rule, "
            "threshold, command, retrieval, external fact, or alternative edit. Do not reveal chain-of-thought, "
            "return a diff, add keys, or include prose outside the JSON object."
        ),
    }
)

PIT_DIAGNOSIS_RESPONSE_SCHEMA_NAMES = MappingProxyType(
    {
        "orchestrator": "pit_diagnosis_orchestrator_v1",
        "reasoner": "pit_diagnosis_reasoner_v1",
        "coder": "pit_diagnosis_coder_v1",
    }
)

PIT_DIAGNOSIS_RESPONSE_SCHEMAS = _freeze_json_schema(
    {
        "orchestrator": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "domain", "evidence_ids"],
            "properties": {
                "action": {"type": "string", "enum": ["reason", "abort"]},
                "domain": {"type": "string", "enum": [item.value for item in PitDomain]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        "reasoner": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "causal_hypothesis",
                "evidence_ids",
                "rule_ids",
                "invariant_ids",
                "experiment_id",
                "skip",
                "skip_reason",
            ],
            "properties": {
                "causal_hypothesis": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "rule_ids": {"type": "array", "items": {"type": "string"}},
                "invariant_ids": {"type": "array", "items": {"type": "string"}},
                "experiment_id": {"type": "string"},
                "skip": {"type": "boolean"},
                "skip_reason": {"type": "string"},
            },
        },
        "coder": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "replacements"],
            "properties": {
                "summary": {"type": "string"},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "old_lines", "new_lines"],
                        "properties": {
                            "path": {"type": "string"},
                            "old_lines": {"type": "array", "items": {"type": "string"}},
                            "new_lines": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    }
)


def pit_diagnosis_response_format(role: str) -> dict[str, object]:
    """Return an isolated response schema without mutating the legacy gateway family."""
    try:
        name = PIT_DIAGNOSIS_RESPONSE_SCHEMA_NAMES[role]
        schema = PIT_DIAGNOSIS_RESPONSE_SCHEMAS[role]
    except KeyError as exc:
        raise ProtocolValidationError("unknown PIT diagnosis role") from exc
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": json.loads(json.dumps(_plain_json_schema(schema), separators=(",", ":"))),
        },
    }


def run_pit_diagnosis_loop(
    config: PitDiagnosisGateConfig,
    source_state: object,
    candidate: object,
    audit: object,
    services: PitDiagnosisLoopServices,
) -> PitDiagnosisLoopResult:
    """Public import location for the controller implementation.

    The implementation lives beside the existing quarantine state machine in ``agent_loop``
    so it can reuse source/candidate/audit capabilities without importing those capabilities
    into the provider protocol module during gateway initialization.
    """
    from agent_loop import run_pit_diagnosis_loop as _run

    return _run(config, source_state, candidate, audit, services)
