"""Admission contracts for the independent two-arm investigator study.

The ordinary role request (``F``) and the study request (``L``) deliberately
have separate identities.  This module only builds and authenticates the
admission side of that boundary; response accounting lives in ``ledger`` and
provider access lives in ``transport``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
from typing import Literal, Mapping

from core.pit_optimizer_v5.artifacts import (
    ArchiveSnapshotV5,
    ArtifactRefV5,
    ArtifactDigestMismatchV5,
    ArtifactRepositoryFailureV5,
    LocalArtifactRepositoryV5,
    RepositoryCheckpointV5,
)
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.development_evaluation import DevelopmentEvaluationContextV5
from core.pit_optimizer_v5.provider import (
    RoleCallKeyV5,
    RoleRequestV5,
    role_request_artifact_primitive_v5,
    wire_role_messages_v5,
    wire_role_schema_v5,
)

from .contracts import (
    StudyAdmissionError,
    StudyArmV1,
    StudyAuthorityError,
    StudyContractError,
    StudyManifestV1,
    StudyModeV1,
    StudyProviderSettingsV1,
)
from .registry import FrozenBehaviorRegistryV1
from .schema import parse_study_response_v1, study_response_schema_v1
from .store import StudyStoreV1


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_MAX_REQUEST_BYTES = 768 * 1024
_MAX_SCHEMA_BYTES = 4 * 1024 * 1024
_STUDY_SCHEMA_VERSION = 1
_TRANSPORT_SETTINGS_PRIMITIVE = {
    "schema_version": 1,
    "stream": False,
    "provider_require_parameters": True,
    "reasoning_exclude": True,
    "temperature": None,
    "seed": None,
    "max_attempts": 1,
    "max_retries": 0,
    "automatic_retries": 0,
    "schema_repair_calls": 0,
    "deadline_mode": "remaining-within-admitted-per-call-window",
}
_TRANSPORT_SETTINGS_BYTES = canonical_json_bytes_v5(_TRANSPORT_SETTINGS_PRIMITIVE)
_TRANSPORT_SETTINGS_SHA256 = hashlib.sha256(_TRANSPORT_SETTINGS_BYTES).hexdigest()


def study_transport_settings_bytes_v1() -> bytes:
    """Canonical transport settings frozen into every admitted L/grant."""

    return _TRANSPORT_SETTINGS_BYTES


def study_transport_settings_sha256_v1() -> str:
    return _TRANSPORT_SETTINGS_SHA256


def _absolute_locator(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value.encode("utf-8")) > 2048:
        raise StudyContractError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise StudyContractError(f"{label} must be absolute")
    return os.path.abspath(value)


def _canonical_json_bytes(raw: bytes, label: str) -> object:
    if type(raw) is not bytes:
        raise StudyContractError(f"{label} bytes are invalid")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_pairs(pairs, label),
            parse_constant=lambda value: (_ for _ in ()).throw(
                StudyContractError(f"{label} contains a nonfinite JSON constant")
            ),
        )
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} JSON is invalid") from exc
    if canonical_json_bytes_v5(decoded) != raw:
        raise StudyContractError(f"{label} JSON is not canonical")
    return decoded


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise StudyContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise StudyContractError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, maximum=192)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise StudyContractError(f"{label} is invalid")
    return text


def _decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or (nonnegative and value < 0):
        raise StudyContractError(f"{label} is invalid")
    return value


def _positive_count(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise StudyContractError(f"{label} is invalid")
    return value


def _nonnegative_count(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise StudyContractError(f"{label} is invalid")
    return value


def _strict_mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise StudyContractError(f"{label} fields are invalid")
    return value


def _raw_utf8(value: object, label: str, *, maximum: int) -> bytes:
    if type(value) is not bytes or len(value) == 0 or len(value) > maximum:
        raise StudyContractError(f"{label} bytes are invalid")
    try:
        value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StudyContractError(f"{label} bytes must be UTF-8") from exc
    return value


def _canonical_decode(cls: type[object], raw: bytes | str, label: str) -> object:
    encoded = raw.encode("utf-8") if type(raw) is str else raw
    if type(encoded) is not bytes:
        raise StudyContractError(f"{label} JSON is invalid")
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_pairs(pairs, label),
            parse_constant=lambda value: (_ for _ in ()).throw(
                StudyContractError(f"{label} contains a nonfinite JSON constant")
            ),
        )
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} JSON is invalid") from exc
    parsed = cls.from_primitive(value)  # type: ignore[attr-defined]
    if parsed.canonical_bytes() != encoded:
        raise StudyContractError(f"{label} JSON is not canonical")
    return parsed


def _unique_pairs(pairs: list[tuple[str, object]], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StudyContractError(f"{label} contains duplicate fields")
        result[key] = value
    return result


def _ref_primitive(value: ArtifactRefV5) -> dict[str, str]:
    return value.to_primitive()


def _ref_from_primitive(value: object, label: str) -> ArtifactRefV5:
    raw = _strict_mapping(value, {"relative_path", "sha256"}, label)
    try:
        return ArtifactRefV5(raw["relative_path"], raw["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _call_primitive(value: RoleCallKeyV5) -> dict[str, object]:
    return {
        "campaign_id": value.campaign_id,
        "round_index": value.round_index,
        "role": value.role,
        "role_position": value.role_position,
        "attempt_kind": value.attempt_kind,
        "attempt_index": value.attempt_index,
        "request_sha256": value.request_sha256,
    }


def _call_from_primitive(value: object) -> RoleCallKeyV5:
    raw = _strict_mapping(
        value,
        {
            "campaign_id",
            "round_index",
            "role",
            "role_position",
            "attempt_kind",
            "attempt_index",
            "request_sha256",
        },
        "fixture role call",
    )
    try:
        return RoleCallKeyV5(
            campaign_id=raw["campaign_id"],  # type: ignore[arg-type]
            round_index=raw["round_index"],  # type: ignore[arg-type]
            role=raw["role"],  # type: ignore[arg-type]
            role_position=raw["role_position"],  # type: ignore[arg-type]
            attempt_kind=raw["attempt_kind"],  # type: ignore[arg-type]
            attempt_index=raw["attempt_index"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise StudyContractError("fixture role call is invalid") from exc


def _messages_primitive(value: tuple[Mapping[str, object], ...]) -> list[object]:
    return [canonical_primitive_v5(item) for item in value]


def _messages_from_primitive(value: object) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list or not value or any(type(item) is not dict for item in value):
        raise StudyContractError("study messages are invalid")
    try:
        result = tuple(dict(item) for item in value)
        # canonical_json_bytes_v5 is also the strict type/finite-value gate for
        # nested message content.
        canonical_json_bytes_v5(result)
    except (TypeError, ValueError) as exc:
        raise StudyContractError("study messages are invalid") from exc
    return result


@dataclass(frozen=True, slots=True)
class FixturePreflightV1:
    """Authenticated fixture-side F request, with no runtime completion."""

    arm: StudyArmV1
    mode: StudyModeV1
    fixture_root_identity_sha256: str
    checkpoint_ref: ArtifactRefV5
    snapshot_ref: ArtifactRefV5
    call: RoleCallKeyV5
    request_ref: ArtifactRefV5
    request_bytes: bytes
    schema_json: bytes
    evidence_refs: tuple[ArtifactRefV5, ...]
    request_sha256: str
    fixture_root_locator: str
    checkpoint_bytes: bytes
    snapshot_bytes: bytes
    evidence_bytes: tuple[bytes, ...]
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("fixture preflight arm is invalid")
        if self.mode not in {"offline_fixture", "live_study"}:
            raise StudyContractError("fixture preflight mode is invalid")
        _digest(self.fixture_root_identity_sha256, "fixture root identity")
        _absolute_locator(self.fixture_root_locator, "fixture root locator")
        if type(self.checkpoint_ref) is not ArtifactRefV5 or type(self.snapshot_ref) is not ArtifactRefV5:
            raise StudyContractError("fixture preflight authority refs are invalid")
        if type(self.call) is not RoleCallKeyV5:
            raise StudyContractError("fixture preflight role call is invalid")
        if (
            self.call.role != "investigator"
            or self.call.role_position != 1
            or self.call.round_index != 2
            or self.call.attempt_kind != "primary"
            or self.call.attempt_index != 1
        ):
            raise StudyContractError("fixture preflight role call is not the investigator primary")
        if type(self.request_ref) is not ArtifactRefV5:
            raise StudyContractError("fixture request ref is invalid")
        _raw_utf8(self.request_bytes, "fixture request", maximum=_MAX_REQUEST_BYTES)
        _raw_utf8(self.schema_json, "fixture schema", maximum=_MAX_SCHEMA_BYTES)
        if type(self.evidence_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.evidence_refs):
            raise StudyContractError("fixture evidence refs are invalid")
        _raw_utf8(self.checkpoint_bytes, "fixture checkpoint", maximum=_MAX_SCHEMA_BYTES)
        _raw_utf8(self.snapshot_bytes, "fixture snapshot", maximum=_MAX_SCHEMA_BYTES)
        if type(self.evidence_bytes) is not tuple or len(self.evidence_bytes) != len(self.evidence_refs):
            raise StudyContractError("fixture evidence bytes are invalid")
        for index, value in enumerate(self.evidence_bytes):
            _raw_utf8(value, f"fixture evidence {index}", maximum=_MAX_SCHEMA_BYTES)
        _digest(self.request_sha256, "fixture request identity")
        if self.request_sha256 != self.call.request_sha256:
            raise StudyAuthorityError("fixture preflight request identity differs from its role call")
        if type(self.schema_version) is not int or self.schema_version != _STUDY_SCHEMA_VERSION:
            raise StudyContractError("fixture preflight schema version is invalid")
        if self.checkpoint_ref.relative_path != "checkpoint.json" or self.snapshot_ref.relative_path != "archive.json":
            raise StudyAuthorityError("fixture preflight checkpoint/snapshot refs are not the round-two projections")
        if self.request_ref.relative_path != f"roles/requests/{self.call.sha256}.json":
            raise StudyAuthorityError("fixture request ref is not the exact public request envelope")
        if self.request_ref.sha256 != hashlib.sha256(self.request_bytes).hexdigest():
            raise StudyAuthorityError("fixture request ref does not authenticate its exact bytes")
        if self.checkpoint_ref.sha256 != hashlib.sha256(self.checkpoint_bytes).hexdigest():
            raise StudyAuthorityError("fixture checkpoint ref does not authenticate its exact bytes")
        if self.snapshot_ref.sha256 != hashlib.sha256(self.snapshot_bytes).hexdigest():
            raise StudyAuthorityError("fixture snapshot ref does not authenticate its exact bytes")
        for reference, content in zip(self.evidence_refs, self.evidence_bytes, strict=True):
            if reference.sha256 != hashlib.sha256(content).hexdigest():
                raise StudyAuthorityError("fixture evidence ref does not authenticate its exact bytes")
        _canonical_json_bytes(self.checkpoint_bytes, "fixture checkpoint")
        _canonical_json_bytes(self.snapshot_bytes, "fixture snapshot")
        try:
            envelope = json.loads(self.request_bytes.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_pairs(pairs, "fixture request envelope"))
        except (UnicodeDecodeError, json.JSONDecodeError, StudyContractError) as exc:
            raise StudyAuthorityError("fixture request envelope is not valid JSON") from exc
        if (
            type(envelope) is not dict
            or set(envelope) != {"schema_version", "artifact_type", "call", "request"}
            or envelope["schema_version"] != 5
            or envelope["artifact_type"] != "role_request"
        ):
            raise StudyAuthorityError("fixture request envelope is not the public V5 request envelope")
        call_raw = envelope["call"]
        if type(call_raw) is not dict or call_raw != _call_primitive(self.call):
            raise StudyAuthorityError("fixture request envelope differs from its F identity")
        if canonical_json_bytes_v5(envelope) != self.request_bytes:
            raise StudyAuthorityError("fixture request envelope is not canonical")

    def to_primitive(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "mode": self.mode,
            "fixture_root_identity_sha256": self.fixture_root_identity_sha256,
            "checkpoint_ref": _ref_primitive(self.checkpoint_ref),
            "snapshot_ref": _ref_primitive(self.snapshot_ref),
            "call": _call_primitive(self.call),
            "request_ref": _ref_primitive(self.request_ref),
            "request_bytes_utf8": self.request_bytes.decode("utf-8"),
            "schema_json_utf8": self.schema_json.decode("utf-8"),
            "evidence_refs": [_ref_primitive(item) for item in self.evidence_refs],
            "request_sha256": self.request_sha256,
            "fixture_root_locator": self.fixture_root_locator,
            "checkpoint_bytes_utf8": self.checkpoint_bytes.decode("utf-8"),
            "snapshot_bytes_utf8": self.snapshot_bytes.decode("utf-8"),
            "evidence_bytes_utf8": [item.decode("utf-8") for item in self.evidence_bytes],
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "FixturePreflightV1":
        raw = _strict_mapping(
            value,
            {
                "arm",
                "mode",
                "fixture_root_identity_sha256",
                "checkpoint_ref",
                "snapshot_ref",
                "call",
                "request_ref",
                "request_bytes_utf8",
                "schema_json_utf8",
                "evidence_refs",
                "request_sha256",
                "fixture_root_locator",
                "checkpoint_bytes_utf8",
                "snapshot_bytes_utf8",
                "evidence_bytes_utf8",
                "schema_version",
            },
            "fixture preflight",
        )
        if type(raw["request_bytes_utf8"]) is not str or type(raw["schema_json_utf8"]) is not str:
            raise StudyContractError("fixture preflight encoded bytes are invalid")
        refs = raw["evidence_refs"]
        evidence_bytes = raw["evidence_bytes_utf8"]
        if type(refs) is not list or type(evidence_bytes) is not list or any(type(item) is not str for item in evidence_bytes):
            raise StudyContractError("fixture evidence refs must be an array")
        if type(raw["fixture_root_locator"]) is not str or type(raw["checkpoint_bytes_utf8"]) is not str or type(raw["snapshot_bytes_utf8"]) is not str:
            raise StudyContractError("fixture preflight frozen bytes are invalid")
        return cls(
            arm=raw["arm"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            fixture_root_identity_sha256=raw["fixture_root_identity_sha256"],  # type: ignore[arg-type]
            checkpoint_ref=_ref_from_primitive(raw["checkpoint_ref"], "fixture checkpoint ref"),
            snapshot_ref=_ref_from_primitive(raw["snapshot_ref"], "fixture snapshot ref"),
            call=_call_from_primitive(raw["call"]),
            request_ref=_ref_from_primitive(raw["request_ref"], "fixture request ref"),
            request_bytes=raw["request_bytes_utf8"].encode("utf-8"),
            schema_json=raw["schema_json_utf8"].encode("utf-8"),
            evidence_refs=tuple(_ref_from_primitive(item, "fixture evidence ref") for item in refs),
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            fixture_root_locator=raw["fixture_root_locator"],  # type: ignore[arg-type]
            checkpoint_bytes=raw["checkpoint_bytes_utf8"].encode("utf-8"),
            snapshot_bytes=raw["snapshot_bytes_utf8"].encode("utf-8"),
            evidence_bytes=tuple(item.encode("utf-8") for item in evidence_bytes),
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "FixturePreflightV1":
        return _canonical_decode(cls, raw, "fixture preflight")  # type: ignore[return-value]


def authenticate_fixture_preflight_v1(
    preflight: FixturePreflightV1,
    *,
    require_current: bool,
) -> RoleRequestV5:
    """Authenticate the complete fixture-side F and frozen authority graph.

    ``require_current`` is used only while admitting a new call.  Terminal
    verification and crash recovery use the frozen bytes captured in the
    preflight, because the repository's checkpoint/archive projections may
    legitimately advance after the study dispatch.
    """

    if type(preflight) is not FixturePreflightV1:
        raise StudyContractError("fixture preflight is invalid")
    try:
        repository = LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator))
        if repository.root_identity_sha256 != preflight.fixture_root_identity_sha256:
            raise StudyAuthorityError("fixture root identity differs from the preflight authority")
        exact_request = repository.authenticate_exact(preflight.request_ref)
        if exact_request.content != preflight.request_bytes:
            raise StudyAuthorityError("fixture F bytes differ from the authenticated request edge")
        call, request = repository.load_unique_role_request_entry_by_sha256(preflight.request_sha256)
        if call != preflight.call or request.sha256 != preflight.request_sha256:
            raise StudyAuthorityError("fixture F call identity differs from the preflight authority")
        if repository.load_role_request(preflight.request_ref) != request:
            raise StudyAuthorityError("fixture F request has conflicting authenticated entries")
        if require_current:
            checkpoint_edge = repository.authenticate_exact(preflight.checkpoint_ref)
            snapshot_edge = repository.authenticate_exact(preflight.snapshot_ref)
            if checkpoint_edge.content != preflight.checkpoint_bytes or snapshot_edge.content != preflight.snapshot_bytes:
                raise StudyAuthorityError("fixture checkpoint/archive bytes differ from the frozen preflight")
        checkpoint_value = _canonical_json_bytes(preflight.checkpoint_bytes, "fixture checkpoint")
        snapshot_value = _canonical_json_bytes(preflight.snapshot_bytes, "fixture snapshot")
        if type(checkpoint_value) is not dict or set(checkpoint_value) != {
            "schema_version", "artifact_type", "generation", "archive_sha256", "record_refs"
        }:
            raise StudyAuthorityError("fixture checkpoint envelope is invalid")
        record_refs = checkpoint_value["record_refs"]
        if type(record_refs) is not list:
            raise StudyAuthorityError("fixture checkpoint record refs are invalid")
        checkpoint = RepositoryCheckpointV5(
            generation=checkpoint_value["generation"],  # type: ignore[arg-type]
            archive_sha256=checkpoint_value["archive_sha256"],  # type: ignore[arg-type]
            record_refs=tuple(_ref_from_primitive(item, "fixture checkpoint record ref") for item in record_refs),
        )
        if checkpoint.to_primitive() != checkpoint_value or checkpoint.archive_sha256 != preflight.snapshot_ref.sha256:
            raise StudyAuthorityError("fixture checkpoint does not bind its archive snapshot")
        if type(snapshot_value) is not dict or set(snapshot_value) != {
            "schema_version", "artifact_type", "generation", "record_refs", "projection"
        }:
            raise StudyAuthorityError("fixture archive envelope is invalid")
        snapshot_records = snapshot_value["record_refs"]
        if type(snapshot_records) is not list:
            raise StudyAuthorityError("fixture archive record refs are invalid")
        snapshot = ArchiveSnapshotV5(
            generation=snapshot_value["generation"],  # type: ignore[arg-type]
            record_refs=tuple(_ref_from_primitive(item, "fixture archive record ref") for item in snapshot_records),
            projection=snapshot_value["projection"],
        )
        if (
            snapshot.to_primitive() != snapshot_value
            or snapshot.generation != checkpoint.generation
            or snapshot.record_refs != checkpoint.record_refs
        ):
            raise StudyAuthorityError("fixture checkpoint/archive graph is inconsistent")
        for record_ref in checkpoint.record_refs:
            repository.authenticate_exact(record_ref)
        for reference, expected in zip(preflight.evidence_refs, preflight.evidence_bytes, strict=True):
            try:
                evidence = repository.authenticate_exact(reference).content
            except ArtifactRepositoryFailureV5:
                evidence = repository.authenticate_raw_artifact(reference).content
            if evidence != expected:
                raise StudyAuthorityError("fixture evidence bytes differ from the frozen preflight")
        if require_current:
            current = repository.load_checkpoint()
            if current is None or canonical_json_bytes_v5(current.to_primitive()) != preflight.checkpoint_bytes:
                raise StudyAuthorityError("fixture checkpoint is no longer the admitted current projection")
            current_archive = repository.authenticate_exact(ArtifactRefV5("archive.json", current.archive_sha256)).content
            if current_archive != preflight.snapshot_bytes:
                raise StudyAuthorityError("fixture archive is no longer the admitted current projection")
    except StudyContractError:
        raise
    except ArtifactDigestMismatchV5 as exc:
        if require_current:
            raise StudyAuthorityError("fixture checkpoint/archive is no longer the admitted current projection") from exc
        raise StudyAuthorityError("fixture preflight authority graph could not be authenticated") from exc
    except (ArtifactRepositoryFailureV5, OSError, TypeError, ValueError) as exc:
        raise StudyAuthorityError("fixture preflight authority graph could not be authenticated") from exc
    if request.schema_authority.canonical_schema_json != preflight.schema_json:
        raise StudyAuthorityError("fixture schema bytes differ from the authenticated F request")
    return request


@dataclass(frozen=True, slots=True)
class StudyEvaluatorMetricV1:
    """One measured, typed metric from the frozen round-one evaluator."""

    metric_id: str
    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        _identifier(self.metric_id, "evaluator metric ID")
        _decimal(self.value, "evaluator metric value")
        _text(self.unit, "evaluator metric unit", maximum=64)

    def to_primitive(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "value": canonical_primitive_v5(self.value),
            "unit": self.unit,
        }

    @classmethod
    def from_primitive(cls, value: object) -> "StudyEvaluatorMetricV1":
        raw = _strict_mapping(value, {"metric_id", "value", "unit"}, "evaluator metric")
        if type(raw["value"]) is not str:
            raise StudyContractError("evaluator metric value is not canonical decimal text")
        try:
            parsed_value = Decimal(raw["value"])
        except Exception as exc:
            raise StudyContractError("evaluator metric value is invalid") from exc
        if canonical_primitive_v5(parsed_value) != raw["value"]:
            raise StudyContractError("evaluator metric value is not canonical")
        return cls(
            metric_id=raw["metric_id"],  # type: ignore[arg-type]
            value=parsed_value,
            unit=raw["unit"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class StudyRoundOneEvaluatorResultV1:
    """Content-addressed evaluator output bound to one exact round-one candidate."""

    study_id: str
    manifest_sha256: str
    grant_sha256: str
    arm: StudyArmV1
    parent_request_sha256: str
    round_one_request_sha256: str
    round_one_terminal_sha256: str
    parsed_response_sha256: str
    candidate_sha256: str
    evaluator_sha256: str
    universe_sha256: str
    status: Literal["succeeded", "failed"]
    metrics: tuple[StudyEvaluatorMetricV1, ...]
    failure_code: str | None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _identifier(self.study_id, "evaluator result study ID")
        for value, label in (
            (self.manifest_sha256, "evaluator result manifest"),
            (self.grant_sha256, "evaluator result grant"),
            (self.parent_request_sha256, "evaluator result parent request"),
            (self.round_one_request_sha256, "evaluator result round-one request"),
            (self.round_one_terminal_sha256, "evaluator result round-one terminal"),
            (self.parsed_response_sha256, "evaluator result parsed response"),
            (self.candidate_sha256, "evaluator result candidate"),
            (self.evaluator_sha256, "evaluator result evaluator"),
            (self.universe_sha256, "evaluator result universe"),
        ):
            _digest(value, label)
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("evaluator result arm is invalid")
        if self.status not in {"succeeded", "failed"}:
            raise StudyContractError("evaluator result status is invalid")
        if type(self.metrics) is not tuple or len(self.metrics) > 64:
            raise StudyContractError("evaluator result metrics are invalid")
        if any(type(item) is not StudyEvaluatorMetricV1 for item in self.metrics):
            raise StudyContractError("evaluator result metrics are invalid")
        metric_ids = tuple(item.metric_id for item in self.metrics)
        if len(set(metric_ids)) != len(metric_ids) or metric_ids != tuple(sorted(metric_ids)):
            raise StudyContractError("evaluator result metrics are not uniquely ordered")
        if self.status == "succeeded":
            if not self.metrics or self.failure_code is not None:
                raise StudyContractError("successful evaluator result must contain measurements only")
        elif self.metrics or self.failure_code is None:
            raise StudyContractError("failed evaluator result must contain a failure code only")
        else:
            _identifier(self.failure_code, "evaluator failure code")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("evaluator result schema version is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "arm": self.arm,
            "parent_request_sha256": self.parent_request_sha256,
            "round_one_request_sha256": self.round_one_request_sha256,
            "round_one_terminal_sha256": self.round_one_terminal_sha256,
            "parsed_response_sha256": self.parsed_response_sha256,
            "candidate_sha256": self.candidate_sha256,
            "evaluator_sha256": self.evaluator_sha256,
            "universe_sha256": self.universe_sha256,
            "status": self.status,
            "metrics": [item.to_primitive() for item in self.metrics],
            "failure_code": self.failure_code,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def feedback_message(self) -> Mapping[str, object]:
        if self.status != "succeeded":
            raise StudyAdmissionError("failed evaluator result cannot become round-two feedback")
        return {
            "role": "user",
            "content": {
                "study_round_feedback_v1": {
                    "round_one_request_sha256": self.round_one_request_sha256,
                    "candidate_sha256": self.candidate_sha256,
                    "evaluator_sha256": self.evaluator_sha256,
                    "universe_sha256": self.universe_sha256,
                    "metrics": [item.to_primitive() for item in self.metrics],
                }
            },
        }

    @classmethod
    def from_primitive(cls, value: object) -> "StudyRoundOneEvaluatorResultV1":
        raw = _strict_mapping(
            value,
            {
                "study_id", "manifest_sha256", "grant_sha256", "arm", "parent_request_sha256",
                "round_one_request_sha256", "round_one_terminal_sha256", "parsed_response_sha256",
                "candidate_sha256", "evaluator_sha256", "universe_sha256", "status", "metrics",
                "failure_code", "schema_version",
            },
            "round-one evaluator result",
        )
        if type(raw["metrics"]) is not list:
            raise StudyContractError("evaluator result metrics are invalid")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            parent_request_sha256=raw["parent_request_sha256"],  # type: ignore[arg-type]
            round_one_request_sha256=raw["round_one_request_sha256"],  # type: ignore[arg-type]
            round_one_terminal_sha256=raw["round_one_terminal_sha256"],  # type: ignore[arg-type]
            parsed_response_sha256=raw["parsed_response_sha256"],  # type: ignore[arg-type]
            candidate_sha256=raw["candidate_sha256"],  # type: ignore[arg-type]
            evaluator_sha256=raw["evaluator_sha256"],  # type: ignore[arg-type]
            universe_sha256=raw["universe_sha256"],  # type: ignore[arg-type]
            status=raw["status"],  # type: ignore[arg-type]
            metrics=tuple(StudyEvaluatorMetricV1.from_primitive(item) for item in raw["metrics"]),
            failure_code=raw["failure_code"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyRoundOneEvaluatorResultV1":
        return _canonical_decode(cls, raw, "round-one evaluator result")  # type: ignore[return-value]


_ROUND_ONE_EVALUATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedStudyRoundOneEvaluatorResultV1:
    result: StudyRoundOneEvaluatorResultV1
    reference: ArtifactRefV5
    _token: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise StudyAuthorityError("authenticated evaluator result is controller-issued")

    @classmethod
    def _issue(
        cls,
        *,
        result: StudyRoundOneEvaluatorResultV1,
        reference: ArtifactRefV5,
        _controller_guard: object,
    ) -> "AuthenticatedStudyRoundOneEvaluatorResultV1":
        if _controller_guard is not _ROUND_ONE_EVALUATION_TOKEN:
            raise StudyAuthorityError("evaluator result authentication is controller-only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "result", result)
        object.__setattr__(instance, "reference", reference)
        object.__setattr__(instance, "_token", _ROUND_ONE_EVALUATION_TOKEN)
        return instance

    def _is_controller_capability(self) -> bool:
        return self._token is _ROUND_ONE_EVALUATION_TOKEN


_DEVELOPMENT_ROUND_ONE_EVALUATION_TOKEN = object()


def _development_context_primitive(context: DevelopmentEvaluationContextV5) -> dict[str, object]:
    return {
        "campaign_id": context.campaign_id,
        "campaign_round_index": context.campaign_round_index,
        "study_id": context.study_id,
        "study_arm": context.study_arm,
        "study_round_one_request_sha256": context.study_round_one_request_sha256,
        "study_round_one_terminal_sha256": context.study_round_one_terminal_sha256,
        "parsed_response_sha256": context.parsed_response_sha256,
        "candidate_sha256": context.candidate_sha256,
        "policy_identity_sha256": context.policy_identity_sha256,
    }


def _development_context_from_primitive(value: object) -> DevelopmentEvaluationContextV5:
    raw = _strict_mapping(
        value,
        {
            "campaign_id", "campaign_round_index", "study_id", "study_arm",
            "study_round_one_request_sha256", "study_round_one_terminal_sha256",
            "parsed_response_sha256", "candidate_sha256", "policy_identity_sha256",
        },
        "development evaluation context",
    )
    try:
        return DevelopmentEvaluationContextV5(
            campaign_id=raw["campaign_id"],  # type: ignore[arg-type]
            campaign_round_index=raw["campaign_round_index"],  # type: ignore[arg-type]
            study_id=raw["study_id"],  # type: ignore[arg-type]
            study_arm=raw["study_arm"],  # type: ignore[arg-type]
            study_round_one_request_sha256=raw["study_round_one_request_sha256"],  # type: ignore[arg-type]
            study_round_one_terminal_sha256=raw["study_round_one_terminal_sha256"],  # type: ignore[arg-type]
            parsed_response_sha256=raw["parsed_response_sha256"],  # type: ignore[arg-type]
            candidate_sha256=raw["candidate_sha256"],  # type: ignore[arg-type]
            policy_identity_sha256=raw["policy_identity_sha256"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise StudyContractError("development evaluation context is invalid") from exc


@dataclass(frozen=True, slots=True)
class StudyDevelopmentRoundOneEvaluatorResultV1:
    """Development-only feedback derived from a retained PanelEvaluationV5."""

    study_id: str
    manifest_sha256: str
    grant_sha256: str
    arm: StudyArmV1
    parent_request_sha256: str
    round_one_request_sha256: str
    round_one_terminal_sha256: str
    parsed_response_sha256: str
    candidate_sha256: str
    context: DevelopmentEvaluationContextV5
    development_receipt_ref: ArtifactRefV5
    study_rubric_sha256: str
    development_evaluator_contract_sha256: str
    panel_sha256: str
    universe_sha256: str
    panel_evaluation_sha256: str
    metrics: tuple[StudyEvaluatorMetricV1, ...]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _identifier(self.study_id, "development evaluator result study ID")
        for value, label in (
            (self.manifest_sha256, "development evaluator result manifest"),
            (self.grant_sha256, "development evaluator result grant"),
            (self.parent_request_sha256, "development evaluator result parent request"),
            (self.round_one_request_sha256, "development evaluator result request"),
            (self.round_one_terminal_sha256, "development evaluator result terminal"),
            (self.parsed_response_sha256, "development evaluator result parsed response"),
            (self.candidate_sha256, "development evaluator result candidate"),
            (self.study_rubric_sha256, "development evaluator result study rubric"),
            (self.development_evaluator_contract_sha256, "development evaluator contract"),
            (self.panel_sha256, "development evaluator panel"),
            (self.universe_sha256, "development evaluator universe"),
            (self.panel_evaluation_sha256, "development panel evaluation"),
        ):
            _digest(value, label)
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("development evaluator result arm is invalid")
        if type(self.context) is not DevelopmentEvaluationContextV5:
            raise StudyContractError("development evaluator result context is invalid")
        if type(self.development_receipt_ref) is not ArtifactRefV5:
            raise StudyContractError("development evaluator receipt reference is invalid")
        if (
            self.development_receipt_ref.relative_path
            != f"adapter-blobs/candidate-development-evaluation-receipts/{self.development_receipt_ref.sha256}.bin"
        ):
            raise StudyAuthorityError("development evaluator receipt reference is not deterministic")
        if (
            self.context.study_id != self.study_id
            or self.context.study_arm != self.arm
            or self.context.study_round_one_request_sha256 != self.round_one_request_sha256
            or self.context.study_round_one_terminal_sha256 != self.round_one_terminal_sha256
            or self.context.parsed_response_sha256 != self.parsed_response_sha256
            or self.context.candidate_sha256 != self.candidate_sha256
        ):
            raise StudyAuthorityError("development evaluator result differs from its receipt context")
        if type(self.metrics) is not tuple or not self.metrics or len(self.metrics) > 64:
            raise StudyContractError("development evaluator result metrics are invalid")
        if any(type(item) is not StudyEvaluatorMetricV1 for item in self.metrics):
            raise StudyContractError("development evaluator result metrics are invalid")
        metric_ids = tuple(item.metric_id for item in self.metrics)
        if len(set(metric_ids)) != len(metric_ids) or metric_ids != tuple(sorted(metric_ids)):
            raise StudyContractError("development evaluator result metrics are not uniquely ordered")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("development evaluator result schema version is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "arm": self.arm,
            "parent_request_sha256": self.parent_request_sha256,
            "round_one_request_sha256": self.round_one_request_sha256,
            "round_one_terminal_sha256": self.round_one_terminal_sha256,
            "parsed_response_sha256": self.parsed_response_sha256,
            "candidate_sha256": self.candidate_sha256,
            "context": _development_context_primitive(self.context),
            "development_receipt_ref": self.development_receipt_ref.to_primitive(),
            "study_rubric_sha256": self.study_rubric_sha256,
            "development_evaluator_contract_sha256": self.development_evaluator_contract_sha256,
            "panel_sha256": self.panel_sha256,
            "universe_sha256": self.universe_sha256,
            "panel_evaluation_sha256": self.panel_evaluation_sha256,
            "metrics": [item.to_primitive() for item in self.metrics],
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def feedback_message(self) -> Mapping[str, object]:
        return {
            "role": "user",
            "content": {
                "study_development_round_feedback_v1": {
                    "round_one_request_sha256": self.round_one_request_sha256,
                    "candidate_sha256": self.candidate_sha256,
                    "study_rubric_sha256": self.study_rubric_sha256,
                    "development_evaluator_contract_sha256": self.development_evaluator_contract_sha256,
                    "panel_sha256": self.panel_sha256,
                    "panel_evaluation_sha256": self.panel_evaluation_sha256,
                    "metrics": [item.to_primitive() for item in self.metrics],
                }
            },
        }

    @classmethod
    def from_primitive(cls, value: object) -> "StudyDevelopmentRoundOneEvaluatorResultV1":
        raw = _strict_mapping(
            value,
            {
                "study_id", "manifest_sha256", "grant_sha256", "arm", "parent_request_sha256",
                "round_one_request_sha256", "round_one_terminal_sha256", "parsed_response_sha256",
                "candidate_sha256", "context", "development_receipt_ref", "study_rubric_sha256",
                "development_evaluator_contract_sha256", "panel_sha256", "universe_sha256",
                "panel_evaluation_sha256", "metrics", "schema_version",
            },
            "development evaluator result",
        )
        if type(raw["metrics"]) is not list:
            raise StudyContractError("development evaluator result metrics are invalid")
        reference = _ref_from_primitive(raw["development_receipt_ref"], "development evaluation receipt ref")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            parent_request_sha256=raw["parent_request_sha256"],  # type: ignore[arg-type]
            round_one_request_sha256=raw["round_one_request_sha256"],  # type: ignore[arg-type]
            round_one_terminal_sha256=raw["round_one_terminal_sha256"],  # type: ignore[arg-type]
            parsed_response_sha256=raw["parsed_response_sha256"],  # type: ignore[arg-type]
            candidate_sha256=raw["candidate_sha256"],  # type: ignore[arg-type]
            context=_development_context_from_primitive(raw["context"]),
            development_receipt_ref=reference,
            study_rubric_sha256=raw["study_rubric_sha256"],  # type: ignore[arg-type]
            development_evaluator_contract_sha256=raw["development_evaluator_contract_sha256"],  # type: ignore[arg-type]
            panel_sha256=raw["panel_sha256"],  # type: ignore[arg-type]
            universe_sha256=raw["universe_sha256"],  # type: ignore[arg-type]
            panel_evaluation_sha256=raw["panel_evaluation_sha256"],  # type: ignore[arg-type]
            metrics=tuple(StudyEvaluatorMetricV1.from_primitive(item) for item in raw["metrics"]),
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyDevelopmentRoundOneEvaluatorResultV1":
        return _canonical_decode(cls, raw, "development evaluator result")  # type: ignore[return-value]


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedStudyDevelopmentRoundOneEvaluatorResultV1:
    result: StudyDevelopmentRoundOneEvaluatorResultV1
    reference: ArtifactRefV5
    _token: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise StudyAuthorityError("authenticated development evaluator result is controller-issued")

    @classmethod
    def _issue(
        cls,
        *,
        result: StudyDevelopmentRoundOneEvaluatorResultV1,
        reference: ArtifactRefV5,
        _controller_guard: object,
    ) -> "AuthenticatedStudyDevelopmentRoundOneEvaluatorResultV1":
        if _controller_guard is not _DEVELOPMENT_ROUND_ONE_EVALUATION_TOKEN:
            raise StudyAuthorityError("development evaluator result authentication is controller-only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "result", result)
        object.__setattr__(instance, "reference", reference)
        object.__setattr__(instance, "_token", _DEVELOPMENT_ROUND_ONE_EVALUATION_TOKEN)
        return instance

    def _is_controller_capability(self) -> bool:
        return self._token is _DEVELOPMENT_ROUND_ONE_EVALUATION_TOKEN


@dataclass(frozen=True, slots=True)
class StudyDevelopmentRoundCallSlotV1:
    """Development-only round slot; it cannot be decoded as a production slot."""

    study_id: str
    manifest_sha256: str
    grant_sha256: str
    arm: StudyArmV1
    round_index: int
    parent_request_sha256: str
    feedback_sha256: str | None
    evaluator_result: StudyDevelopmentRoundOneEvaluatorResultV1 | None = None
    evaluator_result_ref: ArtifactRefV5 | None = None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _identifier(self.study_id, "development round-call study ID")
        _digest(self.manifest_sha256, "development round-call manifest")
        _digest(self.grant_sha256, "development round-call grant")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("development round-call arm is invalid")
        if type(self.round_index) is not int or self.round_index not in {1, 2}:
            raise StudyContractError("development round-call index must be one or two")
        _digest(self.parent_request_sha256, "development round-call parent request")
        if self.round_index == 1:
            if self.feedback_sha256 is not None or self.evaluator_result is not None or self.evaluator_result_ref is not None:
                raise StudyContractError("development round one cannot bind prior feedback")
        else:
            _digest(self.feedback_sha256, "development round-call feedback")
            if type(self.evaluator_result) is not StudyDevelopmentRoundOneEvaluatorResultV1:
                raise StudyContractError("development round two requires its typed evaluator result")
            if type(self.evaluator_result_ref) is not ArtifactRefV5:
                raise StudyContractError("development round two requires its persisted evaluator result reference")
            if (
                self.feedback_sha256 != self.evaluator_result.sha256
                or self.evaluator_result_ref.sha256 != self.evaluator_result.sha256
                or self.evaluator_result_ref.relative_path
                != f"adapter-blobs/study-v1-development-round-one-evaluations/{self.evaluator_result.sha256}.bin"
                or self.evaluator_result.study_id != self.study_id
                or self.evaluator_result.manifest_sha256 != self.manifest_sha256
                or self.evaluator_result.grant_sha256 != self.grant_sha256
                or self.evaluator_result.arm != self.arm
                or self.evaluator_result.parent_request_sha256 != self.parent_request_sha256
            ):
                raise StudyAuthorityError("development round-two evaluator result differs from its slot authority")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("development round-call slot schema version is invalid")

    @property
    def slot_id(self) -> str:
        return f"{self.study_id}:{self.arm}:round:{self.round_index}"

    def to_primitive(self) -> dict[str, object]:
        return {
            "slot_kind": "development",
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "arm": self.arm,
            "round_index": self.round_index,
            "parent_request_sha256": self.parent_request_sha256,
            "feedback_sha256": self.feedback_sha256,
            "evaluator_result": None if self.evaluator_result is None else self.evaluator_result.to_primitive(),
            "evaluator_result_ref": None if self.evaluator_result_ref is None else self.evaluator_result_ref.to_primitive(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_primitive(cls, value: object) -> "StudyDevelopmentRoundCallSlotV1":
        raw = _strict_mapping(
            value,
            {
                "slot_kind", "study_id", "manifest_sha256", "grant_sha256", "arm", "round_index",
                "parent_request_sha256", "feedback_sha256", "evaluator_result", "evaluator_result_ref",
                "schema_version",
            },
            "development round-call slot",
        )
        if raw["slot_kind"] != "development":
            raise StudyContractError("development round-call slot discriminator is invalid")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            round_index=raw["round_index"],  # type: ignore[arg-type]
            parent_request_sha256=raw["parent_request_sha256"],  # type: ignore[arg-type]
            feedback_sha256=raw["feedback_sha256"],  # type: ignore[arg-type]
            evaluator_result=(
                None if raw["evaluator_result"] is None
                else StudyDevelopmentRoundOneEvaluatorResultV1.from_primitive(raw["evaluator_result"])
            ),
            evaluator_result_ref=(
                None if raw["evaluator_result_ref"] is None
                else _ref_from_primitive(raw["evaluator_result_ref"], "development evaluator result ref")
            ),
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


def _round_call_slot_from_primitive(value: object) -> "StudyRoundCallSlotV1 | StudyDevelopmentRoundCallSlotV1":
    if type(value) is dict and value.get("slot_kind") == "development":
        return StudyDevelopmentRoundCallSlotV1.from_primitive(value)
    return StudyRoundCallSlotV1.from_primitive(value)


@dataclass(frozen=True, slots=True)
class StudyRoundCallSlotV1:
    """Immutable round identity bound to one exact base request and grant."""

    study_id: str
    manifest_sha256: str
    grant_sha256: str
    arm: StudyArmV1
    round_index: int
    parent_request_sha256: str
    feedback_sha256: str | None
    evaluator_result: StudyRoundOneEvaluatorResultV1 | None = None
    evaluator_result_ref: ArtifactRefV5 | None = None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _identifier(self.study_id, "round-call study ID")
        _digest(self.manifest_sha256, "round-call manifest")
        _digest(self.grant_sha256, "round-call grant")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("round-call arm is invalid")
        if type(self.round_index) is not int or self.round_index not in {1, 2}:
            raise StudyContractError("round-call index must be one or two")
        _digest(self.parent_request_sha256, "round-call parent request")
        if self.round_index == 1:
            if self.feedback_sha256 is not None or self.evaluator_result is not None or self.evaluator_result_ref is not None:
                raise StudyContractError("round one cannot bind prior feedback")
        elif self.round_index == 2:
            _digest(self.feedback_sha256, "round-call feedback")
            if type(self.evaluator_result) is not StudyRoundOneEvaluatorResultV1:
                raise StudyContractError("round two requires its typed evaluator result")
            if type(self.evaluator_result_ref) is not ArtifactRefV5:
                raise StudyContractError("round two requires its persisted evaluator result reference")
            if (
                self.feedback_sha256 != self.evaluator_result.sha256
                or self.evaluator_result_ref.sha256 != self.evaluator_result.sha256
                or self.evaluator_result_ref.relative_path
                != f"adapter-blobs/study-v1-round-one-evaluations/{self.evaluator_result.sha256}.bin"
                or self.evaluator_result.study_id != self.study_id
                or self.evaluator_result.manifest_sha256 != self.manifest_sha256
                or self.evaluator_result.grant_sha256 != self.grant_sha256
                or self.evaluator_result.arm != self.arm
                or self.evaluator_result.parent_request_sha256 != self.parent_request_sha256
            ):
                raise StudyAuthorityError("round-two evaluator result differs from its slot authority")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("round-call slot schema version is invalid")

    @property
    def slot_id(self) -> str:
        return f"{self.study_id}:{self.arm}:round:{self.round_index}"

    def to_primitive(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "arm": self.arm,
            "round_index": self.round_index,
            "parent_request_sha256": self.parent_request_sha256,
            "feedback_sha256": self.feedback_sha256,
            "evaluator_result": None if self.evaluator_result is None else self.evaluator_result.to_primitive(),
            "evaluator_result_ref": (
                None if self.evaluator_result_ref is None else self.evaluator_result_ref.to_primitive()
            ),
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "StudyRoundCallSlotV1":
        raw = _strict_mapping(
            value,
            {
                "study_id", "manifest_sha256", "grant_sha256", "arm", "round_index",
                "parent_request_sha256", "feedback_sha256", "evaluator_result", "evaluator_result_ref",
                "schema_version",
            },
            "round-call slot",
        )
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            round_index=raw["round_index"],  # type: ignore[arg-type]
            parent_request_sha256=raw["parent_request_sha256"],  # type: ignore[arg-type]
            feedback_sha256=raw["feedback_sha256"],  # type: ignore[arg-type]
            evaluator_result=(
                None
                if raw["evaluator_result"] is None
                else StudyRoundOneEvaluatorResultV1.from_primitive(raw["evaluator_result"])
            ),
            evaluator_result_ref=(
                None if raw["evaluator_result_ref"] is None else _ref_from_primitive(
                    raw["evaluator_result_ref"], "round-one evaluator result ref"
                )
            ),
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class StudyCallRequestV1:
    """The independent live-study request L."""

    study_id: str
    arm: StudyArmV1
    attempt_index: int
    role: Literal["investigator"]
    role_position: Literal[1]
    attempt_kind: Literal["primary"]
    preflight_ref: ArtifactRefV5
    fixture_request_sha256: str
    messages: tuple[Mapping[str, object], ...]
    schema_json: bytes
    projected_wire_messages: bytes
    projected_wire_schema: bytes
    provider: str
    model: str
    max_output_tokens: int
    temperature: Decimal | None = None
    seed: int | None = None
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION
    transport_settings_sha256: str = _TRANSPORT_SETTINGS_SHA256
    round_slot: StudyRoundCallSlotV1 | StudyDevelopmentRoundCallSlotV1 | None = None

    def __post_init__(self) -> None:
        _identifier(self.study_id, "study request ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("study request arm is invalid")
        if self.attempt_index != 1 or type(self.attempt_index) is not int:
            raise StudyContractError("study requests allow exactly one primary attempt")
        if self.role != "investigator" or self.role_position != 1 or self.attempt_kind != "primary":
            raise StudyContractError("study request role identity is invalid")
        if type(self.preflight_ref) is not ArtifactRefV5:
            raise StudyContractError("study request preflight ref is invalid")
        _digest(self.fixture_request_sha256, "fixture request identity")
        if type(self.messages) is not tuple or not self.messages:
            raise StudyContractError("study request messages are invalid")
        try:
            canonical_json_bytes_v5(self.messages)
        except (TypeError, ValueError) as exc:
            raise StudyContractError("study request messages are invalid") from exc
        _raw_utf8(self.schema_json, "study schema", maximum=_MAX_SCHEMA_BYTES)
        _raw_utf8(self.projected_wire_messages, "projected wire messages", maximum=_MAX_REQUEST_BYTES)
        _raw_utf8(self.projected_wire_schema, "projected wire schema", maximum=_MAX_SCHEMA_BYTES)
        _identifier(self.provider, "study provider")
        _text(self.model, "study model", maximum=256)
        if "/" not in self.model:
            raise StudyContractError("study model must include a provider namespace")
        _positive_count(self.max_output_tokens, "study output token ceiling")
        if self.temperature is not None:
            _decimal(self.temperature, "study temperature")
            raise StudyAdmissionError("temperature is unsupported by the pinned study transport")
        if self.seed is not None:
            if type(self.seed) is not int:
                raise StudyContractError("study seed is invalid")
            raise StudyAdmissionError("seed is unsupported by the pinned study transport")
        if type(self.schema_version) is not int or self.schema_version != _STUDY_SCHEMA_VERSION:
            raise StudyContractError("study request schema version is invalid")
        if self.transport_settings_sha256 != _TRANSPORT_SETTINGS_SHA256:
            raise StudyAuthorityError("study request transport settings differ from the frozen adapter")
        if self.input_bound_bytes > _MAX_REQUEST_BYTES:
            raise StudyAdmissionError("study request exceeds its frozen input byte bound")
        if self.round_slot is not None:
            if type(self.round_slot) not in {StudyRoundCallSlotV1, StudyDevelopmentRoundCallSlotV1}:
                raise StudyContractError("study round-call slot is invalid")
            if (
                self.round_slot.study_id != self.study_id
                or self.round_slot.arm != self.arm
                or self.round_slot.parent_request_sha256 != self.base_request_sha256
            ):
                raise StudyAuthorityError("study round-call slot differs from its exact base request")
            if self.round_slot.round_index == 2 and self.messages[-1] != self.round_slot.evaluator_result.feedback_message():
                raise StudyAuthorityError("round-two request does not contain its authenticated evaluator feedback")

    @property
    def input_bound_bytes(self) -> int:
        return len(self.projected_wire_messages) + max(len(self.schema_json), len(self.projected_wire_schema)) + 256

    @property
    def schema_sha256(self) -> str:
        return hashlib.sha256(self.schema_json).hexdigest()

    @property
    def projected_wire_messages_sha256(self) -> str:
        return hashlib.sha256(self.projected_wire_messages).hexdigest()

    @property
    def projected_wire_schema_sha256(self) -> str:
        return hashlib.sha256(self.projected_wire_schema).hexdigest()

    def to_primitive(self) -> dict[str, object]:
        result: dict[str, object] = {
            "study_id": self.study_id,
            "arm": self.arm,
            "attempt_index": self.attempt_index,
            "role": self.role,
            "role_position": self.role_position,
            "attempt_kind": self.attempt_kind,
            "preflight_ref": _ref_primitive(self.preflight_ref),
            "fixture_request_sha256": self.fixture_request_sha256,
            "messages": _messages_primitive(self.messages),
            "schema_json_utf8": self.schema_json.decode("utf-8"),
            "projected_wire_messages_utf8": self.projected_wire_messages.decode("utf-8"),
            "projected_wire_schema_utf8": self.projected_wire_schema.decode("utf-8"),
            "provider": self.provider,
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "temperature": None if self.temperature is None else canonical_primitive_v5(self.temperature),
            "seed": self.seed,
            "schema_version": self.schema_version,
            "transport_settings_sha256": self.transport_settings_sha256,
        }
        if self.round_slot is not None:
            result["round_slot"] = self.round_slot.to_primitive()
        return result

    @property
    def base_request_sha256(self) -> str:
        if self.round_slot is None:
            return self.sha256
        return hashlib.sha256(canonical_json_bytes_v5(self._base_primitive())).hexdigest()

    def _base_primitive(self) -> dict[str, object]:
        result = self.to_primitive()
        result.pop("round_slot", None)
        if self.round_slot is not None and self.round_slot.round_index == 2:
            base_messages = self.messages[:-1]
            result["messages"] = _messages_primitive(base_messages)
            result["projected_wire_messages_utf8"] = canonical_json_bytes_v5(
                wire_role_messages_v5(base_messages)
            ).decode("utf-8")
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "StudyCallRequestV1":
        fields = {
            "study_id",
            "arm",
            "attempt_index",
            "role",
            "role_position",
            "attempt_kind",
            "preflight_ref",
            "fixture_request_sha256",
            "messages",
            "schema_json_utf8",
            "projected_wire_messages_utf8",
            "projected_wire_schema_utf8",
            "provider",
            "model",
            "max_output_tokens",
            "temperature",
            "seed",
            "schema_version",
            "transport_settings_sha256",
        }
        if type(value) is not dict or frozenset(value) not in {
            frozenset(fields),
            frozenset(fields | {"round_slot"}),
        }:
            raise StudyContractError("study request fields are invalid")
        raw = value
        temperature = raw["temperature"]
        if temperature is not None:
            if type(temperature) is not str:
                raise StudyContractError("study temperature is not canonical decimal text")
            try:
                temperature = Decimal(temperature)
            except Exception as exc:
                raise StudyContractError("study temperature is invalid") from exc
            if canonical_primitive_v5(temperature) != raw["temperature"]:
                raise StudyContractError("study temperature is not canonical")
        for name in ("schema_json_utf8", "projected_wire_messages_utf8", "projected_wire_schema_utf8"):
            if type(raw[name]) is not str:
                raise StudyContractError(f"study {name} is invalid")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            attempt_index=raw["attempt_index"],  # type: ignore[arg-type]
            role=raw["role"],  # type: ignore[arg-type]
            role_position=raw["role_position"],  # type: ignore[arg-type]
            attempt_kind=raw["attempt_kind"],  # type: ignore[arg-type]
            preflight_ref=_ref_from_primitive(raw["preflight_ref"], "study preflight ref"),
            fixture_request_sha256=raw["fixture_request_sha256"],  # type: ignore[arg-type]
            messages=_messages_from_primitive(raw["messages"]),
            schema_json=raw["schema_json_utf8"].encode("utf-8"),
            projected_wire_messages=raw["projected_wire_messages_utf8"].encode("utf-8"),
            projected_wire_schema=raw["projected_wire_schema_utf8"].encode("utf-8"),
            provider=raw["provider"],  # type: ignore[arg-type]
            model=raw["model"],  # type: ignore[arg-type]
            max_output_tokens=raw["max_output_tokens"],  # type: ignore[arg-type]
            temperature=temperature,  # type: ignore[arg-type]
            seed=raw["seed"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
            transport_settings_sha256=raw["transport_settings_sha256"],  # type: ignore[arg-type]
            round_slot=(
                None
                if "round_slot" not in raw
                else _round_call_slot_from_primitive(raw["round_slot"])
            ),
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyCallRequestV1":
        return _canonical_decode(cls, raw, "study request")  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class StudyGrantV1:
    """Persisted, bounded grant.  It does not itself confer runtime authority."""

    study_id: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    mode: StudyModeV1
    provider: str
    model: str
    arm_slots: tuple[StudyArmV1, ...]
    input_token_ceiling: int
    output_token_ceiling: int
    cumulative_token_ceiling: int
    per_call_usd_ceiling: Decimal
    cumulative_usd_ceiling: Decimal
    per_call_deadline_seconds: Decimal
    input_price_upper_bound: Decimal
    output_price_upper_bound: Decimal
    response_persistence_consent: bool
    operator_approval_reference: str
    temperature: Decimal | None = None
    seed: int | None = None
    schema_version: Literal[1] = _STUDY_SCHEMA_VERSION
    transport_settings_sha256: str = _TRANSPORT_SETTINGS_SHA256

    def __post_init__(self) -> None:
        _identifier(self.study_id, "study grant study ID")
        _digest(self.manifest_sha256, "study grant manifest")
        _digest(self.repository_root_identity_sha256, "study grant repository root")
        _identifier(self.audit_domain, "study grant audit domain")
        if self.mode not in {"offline_fixture", "live_study"}:
            raise StudyContractError("study grant mode is invalid")
        _identifier(self.provider, "study grant provider")
        _text(self.model, "study grant model", maximum=256)
        if "/" not in self.model:
            raise StudyContractError("study grant model must include a provider namespace")
        if self.arm_slots != ("primary", "withheld"):
            raise StudyContractError("study grant must contain exactly the two arm slots")
        _positive_count(self.input_token_ceiling, "study input token ceiling")
        _positive_count(self.output_token_ceiling, "study output token ceiling")
        _positive_count(self.cumulative_token_ceiling, "study cumulative token ceiling")
        _decimal(self.per_call_usd_ceiling, "study per-call USD ceiling", nonnegative=True)
        _decimal(self.cumulative_usd_ceiling, "study cumulative USD ceiling", nonnegative=True)
        _decimal(self.per_call_deadline_seconds, "study call deadline", nonnegative=True)
        if self.per_call_deadline_seconds <= 0:
            raise StudyContractError("study call deadline must be positive")
        _decimal(self.input_price_upper_bound, "study input price", nonnegative=True)
        _decimal(self.output_price_upper_bound, "study output price", nonnegative=True)
        if type(self.response_persistence_consent) is not bool:
            raise StudyContractError("study response persistence consent is invalid")
        _text(self.operator_approval_reference, "study operator approval reference", maximum=256)
        if self.temperature is not None:
            _decimal(self.temperature, "study grant temperature")
            raise StudyAdmissionError("temperature is unsupported by the pinned study transport")
        if self.seed is not None:
            if type(self.seed) is not int:
                raise StudyContractError("study grant seed is invalid")
            raise StudyAdmissionError("seed is unsupported by the pinned study transport")
        if self.mode == "live_study" and not self.response_persistence_consent:
            raise StudyAdmissionError("live study response persistence must be explicitly authorized")
        if type(self.schema_version) is not int or self.schema_version != _STUDY_SCHEMA_VERSION:
            raise StudyContractError("study grant schema version is invalid")
        if self.transport_settings_sha256 != _TRANSPORT_SETTINGS_SHA256:
            raise StudyAuthorityError("study grant transport settings differ from the frozen adapter")

    def to_primitive(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "arm_slots": list(self.arm_slots),
            "input_token_ceiling": self.input_token_ceiling,
            "output_token_ceiling": self.output_token_ceiling,
            "cumulative_token_ceiling": self.cumulative_token_ceiling,
            "per_call_usd_ceiling": canonical_primitive_v5(self.per_call_usd_ceiling),
            "cumulative_usd_ceiling": canonical_primitive_v5(self.cumulative_usd_ceiling),
            "per_call_deadline_seconds": canonical_primitive_v5(self.per_call_deadline_seconds),
            "input_price_upper_bound": canonical_primitive_v5(self.input_price_upper_bound),
            "output_price_upper_bound": canonical_primitive_v5(self.output_price_upper_bound),
            "response_persistence_consent": self.response_persistence_consent,
            "operator_approval_reference": self.operator_approval_reference,
            "temperature": None if self.temperature is None else canonical_primitive_v5(self.temperature),
            "seed": self.seed,
            "schema_version": self.schema_version,
            "transport_settings_sha256": self.transport_settings_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "StudyGrantV1":
        fields = {
            "study_id",
            "manifest_sha256",
            "repository_root_identity_sha256",
            "audit_domain",
            "mode",
            "provider",
            "model",
            "arm_slots",
            "input_token_ceiling",
            "output_token_ceiling",
            "cumulative_token_ceiling",
            "per_call_usd_ceiling",
            "cumulative_usd_ceiling",
            "per_call_deadline_seconds",
            "input_price_upper_bound",
            "output_price_upper_bound",
            "response_persistence_consent",
            "operator_approval_reference",
            "temperature",
            "seed",
            "schema_version",
            "transport_settings_sha256",
        }
        raw = _strict_mapping(value, fields, "study grant")

        def decimal_field(name: str) -> Decimal:
            encoded = raw[name]
            if type(encoded) is not str:
                raise StudyContractError(f"study grant {name} is not decimal text")
            try:
                parsed = Decimal(encoded)
            except Exception as exc:
                raise StudyContractError(f"study grant {name} is invalid") from exc
            if canonical_primitive_v5(parsed) != encoded:
                raise StudyContractError(f"study grant {name} is not canonical")
            return parsed

        arm_slots = raw["arm_slots"]
        if type(arm_slots) is not list:
            raise StudyContractError("study grant arm slots are invalid")
        temperature = raw["temperature"]
        if temperature is not None:
            if type(temperature) is not str:
                raise StudyContractError("study grant temperature is invalid")
            try:
                temperature = Decimal(temperature)
            except Exception as exc:
                raise StudyContractError("study grant temperature is invalid") from exc
            if canonical_primitive_v5(temperature) != raw["temperature"]:
                raise StudyContractError("study grant temperature is not canonical")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            provider=raw["provider"],  # type: ignore[arg-type]
            model=raw["model"],  # type: ignore[arg-type]
            arm_slots=tuple(arm_slots),  # type: ignore[arg-type]
            input_token_ceiling=raw["input_token_ceiling"],  # type: ignore[arg-type]
            output_token_ceiling=raw["output_token_ceiling"],  # type: ignore[arg-type]
            cumulative_token_ceiling=raw["cumulative_token_ceiling"],  # type: ignore[arg-type]
            per_call_usd_ceiling=decimal_field("per_call_usd_ceiling"),
            cumulative_usd_ceiling=decimal_field("cumulative_usd_ceiling"),
            per_call_deadline_seconds=decimal_field("per_call_deadline_seconds"),
            input_price_upper_bound=decimal_field("input_price_upper_bound"),
            output_price_upper_bound=decimal_field("output_price_upper_bound"),
            response_persistence_consent=raw["response_persistence_consent"],  # type: ignore[arg-type]
            operator_approval_reference=raw["operator_approval_reference"],  # type: ignore[arg-type]
            temperature=temperature,  # type: ignore[arg-type]
            seed=raw["seed"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
            transport_settings_sha256=raw["transport_settings_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyGrantV1":
        return _canonical_decode(cls, raw, "study grant")  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class StudyRoundCallAdmissionV1:
    """Finite supplement for round-indexed calls, bound to an unchanged grant."""

    study_id: str
    manifest_sha256: str
    grant_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    mode: StudyModeV1
    slots: tuple[StudyRoundCallSlotV1 | StudyDevelopmentRoundCallSlotV1, ...]
    approval_reference: str
    schema_version: Literal[1] = 1
    prior_admission_sha256: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.study_id, "round-call admission study ID")
        _digest(self.manifest_sha256, "round-call admission manifest")
        _digest(self.grant_sha256, "round-call admission grant")
        _digest(self.repository_root_identity_sha256, "round-call admission root")
        _identifier(self.audit_domain, "round-call admission audit domain")
        if self.mode not in {"offline_fixture", "live_study"}:
            raise StudyContractError("round-call admission mode is invalid")
        if type(self.slots) is not tuple or not 1 <= len(self.slots) <= 2:
            raise StudyAdmissionError("round-call admission must contain one or two finite slots")
        identities: set[tuple[str, int]] = set()
        for slot in self.slots:
            if type(slot) not in {StudyRoundCallSlotV1, StudyDevelopmentRoundCallSlotV1}:
                raise StudyContractError("round-call admission slot is invalid")
            if (
                slot.study_id != self.study_id
                or slot.manifest_sha256 != self.manifest_sha256
                or slot.grant_sha256 != self.grant_sha256
            ):
                raise StudyAuthorityError("round-call admission slot authority differs")
            identity = (slot.arm, slot.round_index)
            if identity in identities:
                raise StudyAdmissionError("round-call admission contains a duplicate slot")
            identities.add(identity)
        if tuple(sorted(self.slots, key=lambda item: (item.arm, item.round_index))) != self.slots:
            raise StudyContractError("round-call admission slots are not in canonical order")
        for slot in self.slots:
            if slot.round_index == 2 and (slot.arm, 1) not in identities:
                raise StudyAdmissionError("round two requires an admitted round-one slot for the same arm")
        if self.mode == "live_study":
            if any(type(slot) is not StudyRoundCallSlotV1 for slot in self.slots):
                raise StudyAdmissionError("live admission cannot contain a development round slot")
            if len({slot.arm for slot in self.slots}) != 1 or tuple(
                slot.round_index for slot in self.slots
            ) not in {(1,), (1, 2)}:
                raise StudyAdmissionError("live admission requires one same-arm staged round sequence")
            if len(self.slots) == 1:
                if self.prior_admission_sha256 is not None:
                    raise StudyAdmissionError("first live admission cannot cite a prior admission")
            else:
                _digest(self.prior_admission_sha256, "prior live round-call admission")
                if (
                    self.slots[0].parent_request_sha256 != self.slots[1].parent_request_sha256
                    or self.slots[1].evaluator_result.status != "succeeded"
                ):
                    raise StudyAdmissionError("second live admission lacks same-base successful feedback")
        elif self.prior_admission_sha256 is not None:
            raise StudyAdmissionError("offline round-call admission cannot cite a live predecessor")
        _text(self.approval_reference, "round-call approval reference", maximum=256)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("round-call admission schema version is invalid")

    def to_primitive(self) -> dict[str, object]:
        value = {
            "study_id": self.study_id,
            "manifest_sha256": self.manifest_sha256,
            "grant_sha256": self.grant_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "mode": self.mode,
            "slots": [slot.to_primitive() for slot in self.slots],
            "approval_reference": self.approval_reference,
            "schema_version": self.schema_version,
        }
        if self.prior_admission_sha256 is not None:
            value["prior_admission_sha256"] = self.prior_admission_sha256
        return value

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "StudyRoundCallAdmissionV1":
        fields = {
            "study_id", "manifest_sha256", "grant_sha256", "repository_root_identity_sha256",
            "audit_domain", "mode", "slots", "approval_reference", "schema_version",
        }
        if type(value) is dict and "prior_admission_sha256" in value:
            fields.add("prior_admission_sha256")
        raw = _strict_mapping(value, fields, "round-call admission")
        if type(raw["slots"]) is not list:
            raise StudyContractError("round-call admission slots are invalid")
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            slots=tuple(_round_call_slot_from_primitive(item) for item in raw["slots"]),
            approval_reference=raw["approval_reference"],  # type: ignore[arg-type]
            prior_admission_sha256=raw.get("prior_admission_sha256"),  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyRoundCallAdmissionV1":
        return _canonical_decode(cls, raw, "round-call admission")  # type: ignore[return-value]


_CAPABILITY_TOKEN = object()
_CONTROLLER_ISSUANCE_TOKEN = object()
_ROUND_CALL_APPROVAL_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class StudyExecutionApprovalV1:
    """Ephemeral capability issued only by an explicit controller factory."""

    study_id: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    mode: StudyModeV1
    approval_reference: str
    capability_kind: Literal["live", "offline_fixture"]
    _token: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise StudyAuthorityError("study execution approval is controller-issued and cannot be decoded")

    @classmethod
    def _issue(
        cls,
        *,
        grant: StudyGrantV1,
        manifest: StudyManifestV1,
        approval_reference: str,
        capability_kind: Literal["live", "offline_fixture"],
        _controller_guard: object,
    ) -> "StudyExecutionApprovalV1":
        if _controller_guard is not _CONTROLLER_ISSUANCE_TOKEN:
            raise StudyAuthorityError("study execution approval issuance is controller-only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "study_id", grant.study_id)
        object.__setattr__(instance, "grant_sha256", grant.sha256)
        object.__setattr__(instance, "manifest_sha256", manifest.sha256)
        object.__setattr__(instance, "repository_root_identity_sha256", grant.repository_root_identity_sha256)
        object.__setattr__(instance, "mode", grant.mode)
        object.__setattr__(instance, "approval_reference", approval_reference)
        object.__setattr__(instance, "capability_kind", capability_kind)
        object.__setattr__(instance, "_token", _CAPABILITY_TOKEN)
        return instance

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes_v5(
                {
                    "study_id": self.study_id,
                    "grant_sha256": self.grant_sha256,
                    "manifest_sha256": self.manifest_sha256,
                    "repository_root_identity_sha256": self.repository_root_identity_sha256,
                    "mode": self.mode,
                    "approval_reference": self.approval_reference,
                    "capability_kind": self.capability_kind,
                }
            )
        ).hexdigest()

    def _is_controller_capability(self) -> bool:
        return self._token is _CAPABILITY_TOKEN


@dataclass(frozen=True, slots=True, init=False)
class StudyRoundCallApprovalV1:
    """Ephemeral controller approval for one finite round-call admission."""

    study_id: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    admission_sha256: str
    approval_reference: str
    _token: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise StudyAuthorityError("round-call approval is controller-issued and cannot be decoded")

    @classmethod
    def _issue(
        cls,
        *,
        admission: StudyRoundCallAdmissionV1,
        _controller_guard: object,
    ) -> "StudyRoundCallApprovalV1":
        if _controller_guard is not _CONTROLLER_ISSUANCE_TOKEN:
            raise StudyAuthorityError("round-call approval issuance is controller-only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "study_id", admission.study_id)
        object.__setattr__(instance, "grant_sha256", admission.grant_sha256)
        object.__setattr__(instance, "manifest_sha256", admission.manifest_sha256)
        object.__setattr__(instance, "repository_root_identity_sha256", admission.repository_root_identity_sha256)
        object.__setattr__(instance, "admission_sha256", admission.sha256)
        object.__setattr__(instance, "approval_reference", admission.approval_reference)
        object.__setattr__(instance, "_token", _ROUND_CALL_APPROVAL_TOKEN)
        return instance

    def _is_controller_capability(self) -> bool:
        return self._token is _ROUND_CALL_APPROVAL_TOKEN


def authorize_study_execution_v1(
    *,
    store: StudyStoreV1,
    manifest: StudyManifestV1,
    grant: StudyGrantV1,
    approval_reference: str,
) -> StudyExecutionApprovalV1:
    """Persist and bind a grant after the controller receives explicit approval."""

    if type(store) is not StudyStoreV1 or type(manifest) is not StudyManifestV1 or type(grant) is not StudyGrantV1:
        raise StudyAuthorityError("study execution authorization inputs are invalid")
    _text(approval_reference, "study approval reference", maximum=256)
    if (
        grant.study_id != manifest.study_id
        or grant.manifest_sha256 != manifest.sha256
        or grant.mode != manifest.mode
    ):
        raise StudyAuthorityError("study grant is not bound to the manifest")
    if grant.repository_root_identity_sha256 != store.repository.root_identity_sha256:
        raise StudyAuthorityError("study grant is not bound to the repository root")
    if approval_reference != grant.operator_approval_reference:
        raise StudyAuthorityError("study approval reference differs from the grant")
    if grant.mode == "live_study":
        if (
            type(manifest.provider_settings) is not StudyProviderSettingsV1
            or grant.provider != manifest.provider_settings.provider
            or grant.model != manifest.provider_settings.model
            or grant.output_token_ceiling != manifest.provider_settings.max_output_tokens
        ):
            raise StudyAuthorityError("live grant does not cover the pinned manifest provider settings")
    else:
        if (
            grant.provider != "offline_fixture"
            or grant.model != "offline_fixture/study-v1"
            or manifest.offline_settings is None
        ):
            raise StudyAuthorityError("offline grant lacks fixture settings")
    capability_kind: Literal["live", "offline_fixture"]
    if grant.mode == "live_study":
        capability_kind = "live"
    else:
        if not approval_reference.startswith("offline-fixture:"):
            raise StudyAuthorityError("offline mode requires an explicit fixture opt-in tag")
        capability_kind = "offline_fixture"
    store.put(kind="grants", key=grant.study_id, content=grant.canonical_bytes())
    store.put(kind="manifests", key=manifest.sha256, content=manifest.canonical_bytes())
    return StudyExecutionApprovalV1._issue(
        grant=grant,
        manifest=manifest,
        approval_reference=approval_reference,
        capability_kind=capability_kind,
        _controller_guard=_CONTROLLER_ISSUANCE_TOKEN,
    )


def authorize_offline_fixture_v1(
    *,
    store: StudyStoreV1,
    manifest: StudyManifestV1,
    grant: StudyGrantV1,
    approval_reference: str,
) -> StudyExecutionApprovalV1:
    """Issue only the explicitly tagged offline fixture capability."""

    if grant.mode != "offline_fixture" or manifest.mode != "offline_fixture":
        raise StudyAuthorityError("offline fixture authorization cannot issue a live capability")
    if not approval_reference.startswith("offline-fixture:"):
        raise StudyAuthorityError("offline fixture authorization requires its explicit tag")
    return authorize_study_execution_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        approval_reference=approval_reference,
    )


def authorize_study_round_call_admission_v1(
    *,
    store: StudyStoreV1,
    manifest: StudyManifestV1,
    grant: StudyGrantV1,
    execution_approval: StudyExecutionApprovalV1,
    slots: tuple[StudyRoundCallSlotV1 | StudyDevelopmentRoundCallSlotV1, ...],
    approval_reference: str,
    prior_admission: StudyRoundCallAdmissionV1 | None = None,
    round_one_evaluation: AuthenticatedStudyRoundOneEvaluatorResultV1 | None = None,
) -> tuple[StudyRoundCallAdmissionV1, StudyRoundCallApprovalV1]:
    """Persist a finite round-slot supplement after separate explicit approval."""

    if (
        type(store) is not StudyStoreV1
        or type(manifest) is not StudyManifestV1
        or type(grant) is not StudyGrantV1
        or type(execution_approval) is not StudyExecutionApprovalV1
        or not execution_approval._is_controller_capability()
    ):
        raise StudyAuthorityError("round-call admission inputs lack controller authority")
    if (
        execution_approval.study_id != grant.study_id
        or execution_approval.grant_sha256 != grant.sha256
        or execution_approval.manifest_sha256 != manifest.sha256
        or execution_approval.repository_root_identity_sha256 != store.repository.root_identity_sha256
        or execution_approval.approval_reference != grant.operator_approval_reference
        or execution_approval.mode != grant.mode
        or grant.study_id != manifest.study_id
        or grant.manifest_sha256 != manifest.sha256
        or grant.repository_root_identity_sha256 != store.repository.root_identity_sha256
        or grant.mode != manifest.mode
    ):
        raise StudyAuthorityError("round-call admission does not bind the current execution approval")
    _text(approval_reference, "round-call approval reference", maximum=256)
    if approval_reference == grant.operator_approval_reference:
        raise StudyAuthorityError("round-call admission requires separate explicit approval")
    if grant.mode == "offline_fixture" and not approval_reference.startswith("offline-fixture:"):
        raise StudyAuthorityError("offline round-call admission requires its explicit fixture tag")
    if type(slots) is not tuple or not slots or len(slots) > len(grant.arm_slots):
        raise StudyAdmissionError(
            "round-call slots must be nonempty and within the existing finite grant slot count"
        )
    for slot in slots:
        if (
            type(slot) not in {StudyRoundCallSlotV1, StudyDevelopmentRoundCallSlotV1}
            or slot.arm not in grant.arm_slots
        ):
            raise StudyAuthorityError("round-call slot is outside the parent grant")
        if (
            slot.study_id != grant.study_id
            or slot.manifest_sha256 != manifest.sha256
            or slot.grant_sha256 != grant.sha256
        ):
            raise StudyAuthorityError("round-call slot differs from the parent grant or manifest")
    prior_sha256 = None
    if grant.mode == "live_study":
        if execution_approval.capability_kind != "live" or not approval_reference.startswith("live-study:"):
            raise StudyAuthorityError("live round-call admission requires distinct explicit live approval")
        if any(type(slot) is not StudyRoundCallSlotV1 for slot in slots):
            raise StudyAdmissionError("live round-call admission cannot use a development slot")
        existing = []
        for reference in store.list_refs(kind="round-call-admissions"):
            raw = store.read(reference)
            previous = StudyRoundCallAdmissionV1.from_canonical_json(raw)
            if (
                reference.sha256 != hashlib.sha256(raw).hexdigest()
                or reference.relative_path
                != f"adapter-blobs/study-v1-round-call-admissions/{previous.sha256}.bin"
            ):
                raise StudyAuthorityError("persisted round-call admission identity differs")
            if previous.study_id == grant.study_id and previous.grant_sha256 == grant.sha256:
                existing.append(previous)
        if len(slots) == 1:
            if prior_admission is not None or round_one_evaluation is not None or existing:
                raise StudyAdmissionError("first live admission must be the sole unspent stage")
        elif len(slots) == 2:
            if (
                type(prior_admission) is not StudyRoundCallAdmissionV1
                or len(existing) != 1
                or existing[0] != prior_admission
                or prior_admission.mode != "live_study"
                or len(prior_admission.slots) != 1
                or prior_admission.slots[0] != slots[0]
                or prior_admission.approval_reference == approval_reference
                or slots[0].arm != slots[1].arm
                or slots[0].parent_request_sha256 != slots[1].parent_request_sha256
                or type(round_one_evaluation) is not AuthenticatedStudyRoundOneEvaluatorResultV1
                or not round_one_evaluation._is_controller_capability()
                or slots[1].evaluator_result != round_one_evaluation.result
                or slots[1].evaluator_result_ref != round_one_evaluation.reference
                or round_one_evaluation.result.status != "succeeded"
                or store.read(round_one_evaluation.reference)
                != round_one_evaluation.result.canonical_bytes()
            ):
                raise StudyAdmissionError("second live admission lacks the exact settled first stage")
            prior_sha256 = prior_admission.sha256
        else:
            raise StudyAdmissionError("live admission supports only one or two cumulative slots")
    elif prior_admission is not None or round_one_evaluation is not None:
        raise StudyAdmissionError("offline admission cannot consume live predecessor authority")
    admission = StudyRoundCallAdmissionV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain=grant.audit_domain,
        mode=grant.mode,
        slots=slots,
        approval_reference=approval_reference,
        prior_admission_sha256=prior_sha256,
    )
    store.put(kind="round-call-admissions", key=admission.sha256, content=admission.canonical_bytes())
    approval = StudyRoundCallApprovalV1._issue(
        admission=admission,
        _controller_guard=_CONTROLLER_ISSUANCE_TOKEN,
    )
    return admission, approval


def _study_prompt(*, registry: FrozenBehaviorRegistryV1) -> dict[str, object]:
    # Only candidate family semantics and typed parameters are exposed.  The
    # registry's evaluator outputs and fixed-suite vectors are deliberately
    # absent from the prompt.
    candidates = []
    family_semantics = {
        "baseline": {
            "operation": "preserve the registered parent decision",
            "missing_input": "preserve the parent decision when ATR is missing",
        },
        "threshold": {
            "operation": "evaluate the ATR threshold comparator on the registered exit axis",
            "parameters": "operator and threshold select the comparator and decimal boundary",
            "missing_input": "preserve the parent decision when ATR is missing",
        },
        "xor": {
            "operation": "apply a parent-relative XOR toggle to early_winner_hold",
            "parameters": "operator selects inert, always_on, or the registered threshold toggle",
            "missing_input": "preserve the parent decision when ATR is missing",
        },
    }
    for configuration in registry.configurations:
        semantics = dict(family_semantics[configuration.family])
        if (
            configuration.family == "xor"
            and dict(configuration.parameters).get("operator") == "always_on"
        ):
            semantics["missing_input"] = (
                "toggle the parent early_winner_hold decision when ATR is missing"
            )
        candidates.append(
            {
                "configuration_id": configuration.configuration_id,
                "family": configuration.family,
                "parent_configuration_id": configuration.parent_configuration_id,
                "parameters": [
                    [name, canonical_primitive_v5(value)]
                    for name, value in configuration.parameters
                ],
                "family_semantics": semantics,
            }
        )
    return {
        "study_protocol": "v5-two-arm-investigator-v1",
        "instructions": (
            "Preserve the supplied evidence and binding exactly. Propose one typed, registered "
            "candidate family and explain its mechanism using only the closed study vocabulary."
        ),
        "candidate_families": candidates,
    }


def study_prompt_bytes_v1(*, registry: FrozenBehaviorRegistryV1) -> bytes:
    """Return the exact arm-neutral appended prompt committed by the manifest."""

    if type(registry) is not FrozenBehaviorRegistryV1:
        raise StudyContractError("study prompt registry is invalid")
    return canonical_json_bytes_v5(_study_prompt(registry=registry))


def study_parser_authority_bytes_v1() -> bytes:
    """Return the pinned parser implementation bytes used for the manifest hash."""

    return inspect.getsource(parse_study_response_v1).encode("utf-8")


def build_study_call_v1(
    *,
    preflight: FixturePreflightV1,
    manifest: StudyManifestV1,
    fixture_request: RoleRequestV5,
    registry: FrozenBehaviorRegistryV1,
) -> StudyCallRequestV1:
    """Translate an authenticated F request into a deterministic study L request."""

    if (
        type(preflight) is not FixturePreflightV1
        or type(manifest) is not StudyManifestV1
        or type(fixture_request) is not RoleRequestV5
        or type(registry) is not FrozenBehaviorRegistryV1
    ):
        raise StudyContractError("study call construction inputs are invalid")
    if fixture_request.role != "investigator":
        raise StudyAdmissionError("study calls require the investigator fixture request")
    if preflight.mode != manifest.mode or preflight.arm not in {"primary", "withheld"}:
        raise StudyAuthorityError("fixture preflight mode or arm differs from the manifest")
    authenticated_fixture_request = authenticate_fixture_preflight_v1(preflight, require_current=True)
    if authenticated_fixture_request != fixture_request:
        raise StudyAuthorityError("supplied fixture request differs from the authenticated F request")
    expected_ref = manifest.primary_preflight_ref if preflight.arm == "primary" else manifest.withheld_preflight_ref
    if expected_ref.sha256 != preflight.sha256:
        raise StudyAuthorityError("study preflight reference does not authenticate the exact preflight bytes")
    if preflight.request_sha256 != fixture_request.sha256 or preflight.call.request_sha256 != fixture_request.sha256:
        raise StudyAuthorityError("fixture request identity differs from persisted preflight")
    if preflight.schema_json != fixture_request.schema_authority.canonical_schema_json:
        raise StudyAuthorityError("fixture schema bytes differ from persisted preflight")
    if preflight.call.role != fixture_request.role:
        raise StudyAuthorityError("fixture call role differs from the request")
    try:
        expected_fixture_bytes = canonical_json_bytes_v5(
            role_request_artifact_primitive_v5(call=preflight.call, request=fixture_request)
        )
    except (TypeError, ValueError, StudyContractError) as exc:
        raise StudyAdmissionError("fixture request envelope reconstruction failed") from exc
    if expected_fixture_bytes != preflight.request_bytes:
        raise StudyAuthorityError("fixture request bytes differ from the public persisted F envelope")
    if manifest.registry_sha256 != registry.sha256:
        raise StudyAuthorityError("study registry differs from the manifest commitment")
    try:
        schema = study_response_schema_v1(
            fixture_request=fixture_request,
            configuration_ids=tuple(item.configuration_id for item in registry.configurations),
        )
        prompt = _study_prompt(registry=registry)
        prompt_bytes = canonical_json_bytes_v5(prompt)
        if hashlib.sha256(prompt_bytes).hexdigest() != manifest.prompt_sha256:
            raise StudyAuthorityError("study prompt bytes differ from the manifest commitment")
        messages = tuple(fixture_request.messages) + (
            {"role": "user", "content": prompt},
        )
        wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(messages))
        schema_value = json.loads(schema.decode("utf-8"))
        wire_schema = canonical_json_bytes_v5(wire_role_schema_v5(schema_value))
        if hashlib.sha256(schema).hexdigest() != manifest.schema_sha256:
            raise StudyAuthorityError("study response schema differs from the manifest commitment")
        if hashlib.sha256(study_parser_authority_bytes_v1()).hexdigest() != manifest.parser_sha256:
            raise StudyAuthorityError("study parser differs from the manifest commitment")
    except (TypeError, ValueError, UnicodeError, StudyContractError) as exc:
        raise StudyAdmissionError("study request translation failed") from exc
    provider_settings = manifest.provider_settings
    if manifest.mode == "live_study":
        if type(provider_settings) is not StudyProviderSettingsV1:
            raise StudyAuthorityError("live manifest lacks provider settings")
        provider = provider_settings.provider
        model = provider_settings.model
        max_output_tokens = provider_settings.max_output_tokens
        temperature = provider_settings.temperature
        seed = provider_settings.seed
    else:
        provider = "offline_fixture"
        model = "offline_fixture/study-v1"
        max_output_tokens = fixture_request.max_output_tokens
        temperature = None
        seed = None
    return StudyCallRequestV1(
        study_id=manifest.study_id,
        arm=preflight.arm,
        attempt_index=1,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        preflight_ref=expected_ref,
        fixture_request_sha256=fixture_request.sha256,
        messages=messages,
        schema_json=schema,
        projected_wire_messages=wire_messages,
        projected_wire_schema=wire_schema,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        seed=seed,
    )


def build_study_round_call_slot_v1(
    *,
    request: StudyCallRequestV1,
    manifest: StudyManifestV1,
    grant: StudyGrantV1,
    round_index: int,
    round_one_evaluation: AuthenticatedStudyRoundOneEvaluatorResultV1 | None = None,
) -> StudyRoundCallSlotV1:
    """Bind a base request to one typed slot and its authenticated prior evaluation."""

    if (
        type(request) is not StudyCallRequestV1
        or type(manifest) is not StudyManifestV1
        or type(grant) is not StudyGrantV1
    ):
        raise StudyContractError("round-call slot construction inputs are invalid")
    if (
        request.study_id != grant.study_id
        or request.model != grant.model
        or request.provider != grant.provider
        or request.arm not in grant.arm_slots
        or manifest.study_id != grant.study_id
        or manifest.sha256 != grant.manifest_sha256
        or manifest.mode != grant.mode
    ):
        raise StudyAuthorityError("round-call slot base request differs from its grant or manifest")
    if round_index == 1:
        if round_one_evaluation is not None:
            raise StudyAdmissionError("round one cannot consume prior evaluator feedback")
        evaluator_result = None
        evaluator_result_ref = None
        feedback_sha256 = None
    elif round_index == 2:
        if (
            type(round_one_evaluation) is not AuthenticatedStudyRoundOneEvaluatorResultV1
            or not round_one_evaluation._is_controller_capability()
        ):
            raise StudyAdmissionError("round two requires an authenticated round-one evaluator result")
        evaluator_result = round_one_evaluation.result
        evaluator_result_ref = round_one_evaluation.reference
        if evaluator_result.status != "succeeded":
            raise StudyAdmissionError("failed evaluator result cannot authorize round two")
        if (
            evaluator_result.study_id != request.study_id
            or evaluator_result.manifest_sha256 != manifest.sha256
            or evaluator_result.grant_sha256 != grant.sha256
            or evaluator_result.arm != request.arm
            or evaluator_result.parent_request_sha256 != request.sha256
        ):
            raise StudyAuthorityError("round-one evaluator result differs from the round-two parent request")
        feedback_sha256 = evaluator_result.sha256
    else:
        raise StudyContractError("round-call index must be one or two")
    return StudyRoundCallSlotV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        arm=request.arm,
        round_index=round_index,
        parent_request_sha256=request.base_request_sha256,
        feedback_sha256=feedback_sha256,
        evaluator_result=evaluator_result,
        evaluator_result_ref=evaluator_result_ref,
    )


def bind_study_call_to_round_slot_v1(
    *,
    request: StudyCallRequestV1,
    slot: StudyRoundCallSlotV1,
) -> StudyCallRequestV1:
    """Bind a slot and include its measured round-one feedback in round two."""

    if type(request) is not StudyCallRequestV1 or type(slot) is not StudyRoundCallSlotV1:
        raise StudyContractError("round-call binding inputs are invalid")
    if request.round_slot is not None or request.sha256 != slot.parent_request_sha256:
        raise StudyAuthorityError("round-call slot does not bind an unbound exact base request")
    if request.study_id != slot.study_id or request.arm != slot.arm:
        raise StudyAuthorityError("round-call slot study or arm differs from the base request")
    messages = request.messages
    if slot.round_index == 2:
        if slot.evaluator_result is None:
            raise StudyAdmissionError("round two requires an authenticated round-one evaluator result")
        messages = (*messages, slot.evaluator_result.feedback_message())
    projected_wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(messages))
    return replace(
        request,
        messages=messages,
        projected_wire_messages=projected_wire_messages,
        round_slot=slot,
    )


def build_study_development_round_call_slot_v1(
    *,
    request: StudyCallRequestV1,
    manifest: StudyManifestV1,
    grant: StudyGrantV1,
    round_index: int,
    round_one_evaluation: AuthenticatedStudyDevelopmentRoundOneEvaluatorResultV1 | None = None,
) -> StudyDevelopmentRoundCallSlotV1:
    """Bind an explicitly offline development slot to an authenticated V5 result."""

    if (
        type(request) is not StudyCallRequestV1
        or type(manifest) is not StudyManifestV1
        or type(grant) is not StudyGrantV1
    ):
        raise StudyContractError("development round-call slot construction inputs are invalid")
    if (
        request.round_slot is not None
        or request.study_id != grant.study_id
        or request.model != grant.model
        or request.provider != grant.provider
        or request.arm not in grant.arm_slots
        or manifest.study_id != grant.study_id
        or manifest.sha256 != grant.manifest_sha256
    ):
        raise StudyAuthorityError("development round-call slot base request differs from its grant or manifest")
    if grant.mode != "offline_fixture" or manifest.mode != "offline_fixture":
        raise StudyAdmissionError("development round-call slots are offline-fixture only")
    if round_index == 1:
        if round_one_evaluation is not None:
            raise StudyAdmissionError("development round one cannot consume prior evaluator feedback")
        evaluation = None
        reference = None
        feedback_sha256 = None
    elif round_index == 2:
        if (
            type(round_one_evaluation) is not AuthenticatedStudyDevelopmentRoundOneEvaluatorResultV1
            or not round_one_evaluation._is_controller_capability()
        ):
            raise StudyAdmissionError("development round two requires an authenticated V5 evaluator result")
        evaluation = round_one_evaluation.result
        reference = round_one_evaluation.reference
        if (
            evaluation.study_id != request.study_id
            or evaluation.manifest_sha256 != manifest.sha256
            or evaluation.grant_sha256 != grant.sha256
            or evaluation.arm != request.arm
            or evaluation.parent_request_sha256 != request.sha256
        ):
            raise StudyAuthorityError("development evaluator result differs from the round-two parent request")
        feedback_sha256 = evaluation.sha256
    else:
        raise StudyContractError("development round-call index must be one or two")
    return StudyDevelopmentRoundCallSlotV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        arm=request.arm,
        round_index=round_index,
        parent_request_sha256=request.sha256,
        feedback_sha256=feedback_sha256,
        evaluator_result=evaluation,
        evaluator_result_ref=reference,
    )


def bind_study_call_to_development_round_slot_v1(
    *,
    request: StudyCallRequestV1,
    slot: StudyDevelopmentRoundCallSlotV1,
) -> StudyCallRequestV1:
    """Bind development feedback while preserving a separate slot discriminator."""

    if type(request) is not StudyCallRequestV1 or type(slot) is not StudyDevelopmentRoundCallSlotV1:
        raise StudyContractError("development round-call binding inputs are invalid")
    if request.round_slot is not None or request.sha256 != slot.parent_request_sha256:
        raise StudyAuthorityError("development slot does not bind an unbound exact base request")
    if request.study_id != slot.study_id or request.arm != slot.arm:
        raise StudyAuthorityError("development round-call slot study or arm differs from the base request")
    messages = request.messages
    if slot.round_index == 2:
        if slot.evaluator_result is None:
            raise StudyAdmissionError("development round two requires an authenticated V5 evaluation")
        messages = (*messages, slot.evaluator_result.feedback_message())
    projected_wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(messages))
    return replace(
        request,
        messages=messages,
        projected_wire_messages=projected_wire_messages,
        round_slot=slot,
    )


__all__ = [
    "FixturePreflightV1",
    "authenticate_fixture_preflight_v1",
    "AuthenticatedStudyDevelopmentRoundOneEvaluatorResultV1",
    "AuthenticatedStudyRoundOneEvaluatorResultV1",
    "StudyCallRequestV1",
    "StudyDevelopmentRoundCallSlotV1",
    "StudyDevelopmentRoundOneEvaluatorResultV1",
    "StudyEvaluatorMetricV1",
    "StudyExecutionApprovalV1",
    "StudyGrantV1",
    "StudyRoundCallApprovalV1",
    "StudyRoundCallAdmissionV1",
    "StudyRoundCallSlotV1",
    "StudyRoundOneEvaluatorResultV1",
    "authorize_offline_fixture_v1",
    "authorize_study_execution_v1",
    "authorize_study_round_call_admission_v1",
    "bind_study_call_to_round_slot_v1",
    "build_study_call_v1",
    "build_study_development_round_call_slot_v1",
    "build_study_round_call_slot_v1",
    "bind_study_call_to_development_round_slot_v1",
    "study_parser_authority_bytes_v1",
    "study_prompt_bytes_v1",
    "study_transport_settings_bytes_v1",
    "study_transport_settings_sha256_v1",
]
