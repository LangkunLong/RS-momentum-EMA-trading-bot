"""Admission contracts for the independent two-arm investigator study.

The ordinary role request (``F``) and the study request (``L``) deliberately
have separate identities.  This module only builds and authenticates the
admission side of that boundary; response accounting lives in ``ledger`` and
provider access lives in ``transport``.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        return {
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

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_primitive(cls, value: object) -> "StudyCallRequestV1":
        raw = _strict_mapping(
            value,
            {
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
            },
            "study request",
        )
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


_CAPABILITY_TOKEN = object()
_CONTROLLER_ISSUANCE_TOKEN = object()


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
        candidates.append(
            {
                "configuration_id": configuration.configuration_id,
                "family": configuration.family,
                "parent_configuration_id": configuration.parent_configuration_id,
                "parameters": [
                    [name, canonical_primitive_v5(value)]
                    for name, value in configuration.parameters
                ],
                "family_semantics": family_semantics[configuration.family],
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


__all__ = [
    "FixturePreflightV1",
    "authenticate_fixture_preflight_v1",
    "StudyCallRequestV1",
    "StudyExecutionApprovalV1",
    "StudyGrantV1",
    "authorize_offline_fixture_v1",
    "authorize_study_execution_v1",
    "build_study_call_v1",
    "study_parser_authority_bytes_v1",
    "study_prompt_bytes_v1",
    "study_transport_settings_bytes_v1",
    "study_transport_settings_sha256_v1",
]
