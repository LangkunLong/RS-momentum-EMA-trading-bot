"""Independent append-only accounting ledger for the two live study slots."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from typing import Literal, Mapping

from core.pit_optimizer_v5.artifacts import ArtifactRefV5
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.provider import (
    CompletionResultV5,
    ProviderFailureDiagnosticV5,
    ProviderResponseAccountingErrorV5,
    RoleBindingV5,
    RoleAttemptFactsV5,
    RoleFailureCode,
    RoleTerminalReceiptV5,
    RoleUsageFactsV5,
    RoleRequestV5,
    wire_role_messages_v5,
    wire_role_schema_v5,
)

from .contracts import (
    StudyAdmissionError,
    StudyArmV1,
    StudyAuthorityError,
    StudyContractError,
    StudyManifestV1,
    StudyPendingAccounting,
    StudyProviderSettingsV1,
    StudyResponseV1,
)
from .live_calls import (
    FixturePreflightV1,
    StudyCallRequestV1,
    StudyExecutionApprovalV1,
    StudyGrantV1,
    authenticate_fixture_preflight_v1,
    study_parser_authority_bytes_v1,
    study_transport_settings_sha256_v1,
)
from .schema import parse_study_response_v1
from .store import StudyStoreV1


_SCHEMA_VERSION = 1
_LEDGER_NAMESPACE = "study-v1-ledger"
_MAX_RECORDS = 4096
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_PARSED_BYTES = 4 * 1024 * 1024
_ZERO = Decimal("0")
_OVERSIZE_RESPONSE_REASON = (
    "provider response is non-importable; exact bytes are unavailable because they exceed the frozen 4 MiB raw-response bound"
)
_OVERSIZE_RESPONSE_LIMITATION = "exact provider bytes exceed the frozen 4 MiB raw-response bound"
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_PENDING_REQUEST_PREFIX = "pending-"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
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


def _count(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or (value <= 0 if positive else value < 0):
        raise StudyContractError(f"{label} is invalid")
    return value


def _decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or (nonnegative and value < 0):
        raise StudyContractError(f"{label} is invalid")
    return value


def _strict(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise StudyContractError(f"{label} fields are invalid")
    return value


def _ref(value: ArtifactRefV5) -> dict[str, str]:
    return value.to_primitive()


def _ref_from(value: object, label: str) -> ArtifactRefV5:
    raw = _strict(value, {"relative_path", "sha256"}, label)
    try:
        return ArtifactRefV5(raw["relative_path"], raw["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _decimal_from(value: object, label: str) -> Decimal:
    if type(value) is not str:
        raise StudyContractError(f"{label} is not canonical decimal text")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc
    if canonical_primitive_v5(parsed) != value:
        raise StudyContractError(f"{label} is not canonical")
    return parsed


def _dataclass_primitive(value: object) -> object:
    if isinstance(value, Decimal):
        return canonical_primitive_v5(value)
    if isinstance(value, ArtifactRefV5):
        return value.to_primitive()
    if isinstance(value, RoleFailureCode):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _dataclass_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_dataclass_primitive(item) for item in value]
    if isinstance(value, list):
        return [_dataclass_primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dataclass_primitive(item) for key, item in value.items()}
    return value


def _usage_primitive(value: RoleUsageFactsV5) -> dict[str, object]:
    return _dataclass_primitive(value)  # type: ignore[return-value]


def _usage_from(value: object) -> RoleUsageFactsV5:
    raw = _strict(
        value,
        {
            "external_attempt_count",
            "request_started",
            "response_received",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "requested_model",
            "returned_model",
            "provider_request_id",
        },
        "study usage",
    )
    return RoleUsageFactsV5(
        external_attempt_count=raw["external_attempt_count"],  # type: ignore[arg-type]
        request_started=raw["request_started"],  # type: ignore[arg-type]
        response_received=raw["response_received"],  # type: ignore[arg-type]
        input_tokens=raw["input_tokens"],  # type: ignore[arg-type]
        output_tokens=raw["output_tokens"],  # type: ignore[arg-type]
        total_tokens=raw["total_tokens"],  # type: ignore[arg-type]
        cost_usd=_decimal_from(raw["cost_usd"], "study usage cost"),
        requested_model=raw["requested_model"],  # type: ignore[arg-type]
        returned_model=raw["returned_model"],  # type: ignore[arg-type]
        provider_request_id=raw["provider_request_id"],  # type: ignore[arg-type]
    )


def _attempt_primitive(value: RoleAttemptFactsV5) -> dict[str, object]:
    return _dataclass_primitive(value)  # type: ignore[return-value]


def _attempt_from(value: object) -> RoleAttemptFactsV5:
    raw = _strict(
        value,
        {
            "role",
            "attempt_kind",
            "attempt_index",
            "request_sha256",
            "slot_id",
            "outcome",
            "failure_code",
            "usage",
            "response_sha256",
            "artifact_sha256",
        },
        "study attempt",
    )
    failure_code = raw["failure_code"]
    if failure_code is not None:
        try:
            failure_code = RoleFailureCode(failure_code)
        except ValueError as exc:
            raise StudyContractError("study attempt failure code is invalid") from exc
    return RoleAttemptFactsV5(
        role=raw["role"],  # type: ignore[arg-type]
        attempt_kind=raw["attempt_kind"],  # type: ignore[arg-type]
        attempt_index=raw["attempt_index"],  # type: ignore[arg-type]
        request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
        slot_id=raw["slot_id"],  # type: ignore[arg-type]
        outcome=raw["outcome"],  # type: ignore[arg-type]
        failure_code=failure_code,  # type: ignore[arg-type]
        usage=_usage_from(raw["usage"]),
        response_sha256=raw["response_sha256"],  # type: ignore[arg-type]
        artifact_sha256=raw["artifact_sha256"],  # type: ignore[arg-type]
    )


def _receipt_primitive(value: RoleTerminalReceiptV5) -> dict[str, object]:
    return _dataclass_primitive(value)  # type: ignore[return-value]


def _receipt_from(value: object) -> RoleTerminalReceiptV5:
    raw = _strict(
        value,
        {
            "slot_id",
            "slot_request_sha256",
            "authorization_sha256",
            "attempt_facts_sha256",
            "cumulative_external_attempts",
            "cumulative_total_tokens",
            "cumulative_cost_usd",
            "terminal_sequence",
            "receipt_sha256",
        },
        "study receipt",
    )
    return RoleTerminalReceiptV5(
        slot_id=raw["slot_id"],  # type: ignore[arg-type]
        slot_request_sha256=raw["slot_request_sha256"],  # type: ignore[arg-type]
        authorization_sha256=raw["authorization_sha256"],  # type: ignore[arg-type]
        attempt_facts_sha256=raw["attempt_facts_sha256"],  # type: ignore[arg-type]
        cumulative_external_attempts=raw["cumulative_external_attempts"],  # type: ignore[arg-type]
        cumulative_total_tokens=raw["cumulative_total_tokens"],  # type: ignore[arg-type]
        cumulative_cost_usd=_decimal_from(raw["cumulative_cost_usd"], "study receipt cumulative cost"),
        terminal_sequence=raw["terminal_sequence"],  # type: ignore[arg-type]
        receipt_sha256=raw["receipt_sha256"],  # type: ignore[arg-type]
    )


def _completion_metadata_primitive(value: CompletionResultV5) -> dict[str, object]:
    result: dict[str, object] = {
        "accepted": value.accepted,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "provider_request_id": value.provider_request_id,
        "returned_model": value.returned_model,
        "cost_usd": canonical_primitive_v5(value.cost_usd),
        "external_attempt_count": value.external_attempt_count,
        "response_received": value.response_received,
    }
    if value.cleanup_diagnostic is not None:
        result["cleanup_diagnostic"] = {
            "phase": value.cleanup_diagnostic[0],
            "code": value.cleanup_diagnostic[1],
        }
    return result


def _optional_cleanup_diagnostic(value: Mapping[str, object]) -> tuple[str, str] | None:
    if "cleanup_diagnostic" not in value:
        return None
    raw = _strict(
        value["cleanup_diagnostic"],
        {"phase", "code"},
        "study cleanup diagnostic",
    )
    diagnostic = ProviderFailureDiagnosticV5(
        phase=raw["phase"],  # type: ignore[arg-type]
        code=raw["code"],  # type: ignore[arg-type]
    )
    if (diagnostic.phase, diagnostic.code) != ("client_cleanup", "client_cleanup_failed"):
        raise StudyAuthorityError("study cleanup diagnostic phase or code is invalid")
    return diagnostic.phase, diagnostic.code


def _completion_from_metadata(value: object, response_text: str) -> CompletionResultV5:
    base_fields = {
        "accepted",
        "input_tokens",
        "output_tokens",
        "provider_request_id",
        "returned_model",
        "cost_usd",
        "external_attempt_count",
        "response_received",
    }
    if not isinstance(value, Mapping):
        raise StudyAuthorityError("study completion metadata is invalid")
    expected_fields = base_fields | ({"cleanup_diagnostic"} if "cleanup_diagnostic" in value else set())
    raw = _strict(
        value,
        expected_fields,
        "study completion",
    )
    return CompletionResultV5(
        response_text=response_text,
        accepted=raw["accepted"],  # type: ignore[arg-type]
        input_tokens=raw["input_tokens"],  # type: ignore[arg-type]
        output_tokens=raw["output_tokens"],  # type: ignore[arg-type]
        provider_request_id=raw["provider_request_id"],  # type: ignore[arg-type]
        returned_model=raw["returned_model"],  # type: ignore[arg-type]
        cost_usd=_decimal_from(raw["cost_usd"], "study completion cost"),
        external_attempt_count=raw["external_attempt_count"],  # type: ignore[arg-type]
        response_received=raw["response_received"],  # type: ignore[arg-type]
        cleanup_diagnostic=_optional_cleanup_diagnostic(raw),
    )


def _completion_fingerprint(value: CompletionResultV5, *, response_sha256: str, response_length: int) -> str:
    return _sha256(
        canonical_json_bytes_v5(
            {
                "completion": _completion_metadata_primitive(value),
                "response_sha256": response_sha256,
                "response_length": response_length,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class StudyReservationV1:
    study_id: str
    arm: StudyArmV1
    request_sha256: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    slot_id: str
    invocation_owner: str
    prospective_input_tokens: int
    prospective_output_tokens: int
    prospective_cost_usd: Decimal
    sequence: int
    reservation_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.study_id, "reservation study ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("reservation arm is invalid")
        for value, label in (
            (self.request_sha256, "reservation request"),
            (self.grant_sha256, "reservation grant"),
            (self.manifest_sha256, "reservation manifest"),
            (self.repository_root_identity_sha256, "reservation root"),
        ):
            _digest(value, label)
        _identifier(self.audit_domain, "reservation audit domain")
        _text(self.slot_id, "reservation slot", maximum=128)
        _text(self.invocation_owner, "reservation invocation owner", maximum=256)
        _count(self.prospective_input_tokens, "reservation input tokens")
        _count(self.prospective_output_tokens, "reservation output tokens")
        _decimal(self.prospective_cost_usd, "reservation prospective cost", nonnegative=True)
        _count(self.sequence, "reservation sequence", positive=True)
        _digest(self.reservation_sha256, "reservation identity")
        if self.reservation_sha256 != hashlib.sha256(canonical_json_bytes_v5(self.authenticated_payload())).hexdigest():
            raise StudyContractError("reservation digest differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("reservation schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "arm": self.arm,
            "request_sha256": self.request_sha256,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "slot_id": self.slot_id,
            "invocation_owner": self.invocation_owner,
            "prospective_input_tokens": self.prospective_input_tokens,
            "prospective_output_tokens": self.prospective_output_tokens,
            "prospective_cost_usd": canonical_primitive_v5(self.prospective_cost_usd),
            "sequence": self.sequence,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"reservation_sha256": self.reservation_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyReservationV1":
        raw = _strict(
            value,
            {
                "study_id",
                "arm",
                "request_sha256",
                "grant_sha256",
                "manifest_sha256",
                "repository_root_identity_sha256",
                "audit_domain",
                "slot_id",
                "invocation_owner",
                "prospective_input_tokens",
                "prospective_output_tokens",
                "prospective_cost_usd",
                "sequence",
                "reservation_sha256",
                "schema_version",
            },
            "study reservation",
        )
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            slot_id=raw["slot_id"],  # type: ignore[arg-type]
            invocation_owner=raw["invocation_owner"],  # type: ignore[arg-type]
            prospective_input_tokens=raw["prospective_input_tokens"],  # type: ignore[arg-type]
            prospective_output_tokens=raw["prospective_output_tokens"],  # type: ignore[arg-type]
            prospective_cost_usd=_decimal_from(raw["prospective_cost_usd"], "reservation prospective cost"),
            sequence=raw["sequence"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyReservationV1":
        return _decode_canonical(cls, raw, "study reservation")


@dataclass(frozen=True, slots=True)
class _StudyReservationPublicationV1:
    """Authenticated intent for a reservation's three-record publication.

    The envelope lives in the existing request namespace so an interrupted
    create-only sequence remains readable by old stores and export walkers.
    It binds the exact request and reservation bytes that recovery is allowed
    to publish; the envelope itself never authorizes provider dispatch.
    """

    study_id: str
    arm: StudyArmV1
    request_sha256: str
    reservation_sha256: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    request: StudyCallRequestV1
    reservation: StudyReservationV1
    publication_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.study_id, "reservation publication study ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("reservation publication arm is invalid")
        for value, label in (
            (self.request_sha256, "reservation publication request"),
            (self.reservation_sha256, "reservation publication reservation"),
            (self.grant_sha256, "reservation publication grant"),
            (self.manifest_sha256, "reservation publication manifest"),
            (self.repository_root_identity_sha256, "reservation publication root"),
        ):
            _digest(value, label)
        _identifier(self.audit_domain, "reservation publication audit domain")
        if type(self.request) is not StudyCallRequestV1 or type(self.reservation) is not StudyReservationV1:
            raise StudyContractError("reservation publication records are invalid")
        if (
            self.request.study_id != self.study_id
            or self.request.arm != self.arm
            or self.request.sha256 != self.request_sha256
            or self.reservation.study_id != self.study_id
            or self.reservation.arm != self.arm
            or self.reservation.request_sha256 != self.request_sha256
            or self.reservation.reservation_sha256 != self.reservation_sha256
        ):
            raise StudyContractError("reservation publication records are not bound")
        _digest(self.publication_sha256, "reservation publication identity")
        if self.publication_sha256 != _sha256(canonical_json_bytes_v5(self.authenticated_payload())):
            raise StudyContractError("reservation publication digest differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("reservation publication schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "arm": self.arm,
            "request_sha256": self.request_sha256,
            "reservation_sha256": self.reservation_sha256,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "request": self.request.to_primitive(),
            "reservation": self.reservation.to_primitive(),
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"publication_sha256": self.publication_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "_StudyReservationPublicationV1":
        raw = _strict(
            value,
            {
                "study_id",
                "arm",
                "request_sha256",
                "reservation_sha256",
                "grant_sha256",
                "manifest_sha256",
                "repository_root_identity_sha256",
                "audit_domain",
                "request",
                "reservation",
                "publication_sha256",
                "schema_version",
            },
            "study reservation publication",
        )
        try:
            request = StudyCallRequestV1.from_primitive(raw["request"])
            reservation = StudyReservationV1.from_primitive(raw["reservation"])
        except (StudyContractError, TypeError, ValueError) as exc:
            raise StudyContractError("study reservation publication records are invalid") from exc
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            request=request,
            reservation=reservation,
            publication_sha256=raw["publication_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "_StudyReservationPublicationV1":
        return _decode_canonical(cls, raw, "study reservation publication")


@dataclass(frozen=True, slots=True)
class StudyAdmissionRejectionV1:
    """Durable local rejection before a provider handoff was claimed."""

    study_id: str
    arm: StudyArmV1
    request_sha256: str
    reservation_sha256: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    invocation_owner: str
    reason: str
    rejection_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.study_id, "admission rejection study ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("admission rejection arm is invalid")
        for value, label in (
            (self.request_sha256, "admission rejection request"),
            (self.reservation_sha256, "admission rejection reservation"),
            (self.grant_sha256, "admission rejection grant"),
            (self.manifest_sha256, "admission rejection manifest"),
            (self.repository_root_identity_sha256, "admission rejection root"),
        ):
            _digest(value, label)
        _identifier(self.audit_domain, "admission rejection audit domain")
        _text(self.invocation_owner, "admission rejection invocation owner", maximum=256)
        _text(self.reason, "admission rejection reason", maximum=1024)
        _digest(self.rejection_sha256, "admission rejection identity")
        if self.rejection_sha256 != _sha256(canonical_json_bytes_v5(self.authenticated_payload())):
            raise StudyContractError("admission rejection identity differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("admission rejection schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "arm": self.arm,
            "request_sha256": self.request_sha256,
            "reservation_sha256": self.reservation_sha256,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "invocation_owner": self.invocation_owner,
            "reason": self.reason,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"rejection_sha256": self.rejection_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyAdmissionRejectionV1":
        raw = _strict(
            value,
            {
                "study_id",
                "arm",
                "request_sha256",
                "reservation_sha256",
                "grant_sha256",
                "manifest_sha256",
                "repository_root_identity_sha256",
                "audit_domain",
                "invocation_owner",
                "reason",
                "rejection_sha256",
                "schema_version",
            },
            "study admission rejection",
        )
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            invocation_owner=raw["invocation_owner"],  # type: ignore[arg-type]
            reason=raw["reason"],  # type: ignore[arg-type]
            rejection_sha256=raw["rejection_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyAdmissionRejectionV1":
        return _decode_canonical(cls, raw, "study admission rejection")


@dataclass(frozen=True, slots=True)
class StudyCallTerminalV1:
    study_id: str
    arm: StudyArmV1
    request_sha256: str
    reservation_sha256: str
    grant_sha256: str
    manifest_sha256: str
    repository_root_identity_sha256: str
    audit_domain: str
    invocation_owner: str
    prior_journal_sha256: str | None
    terminal_sequence: int
    response_ref: ArtifactRefV5
    parsed_ref: ArtifactRefV5 | None
    attempt: RoleAttemptFactsV5
    usage: RoleUsageFactsV5
    receipt: RoleTerminalReceiptV5
    failure_code: RoleFailureCode | None
    failure_reason: str | None
    terminal_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.study_id, "terminal study ID")
        if self.arm not in {"primary", "withheld"}:
            raise StudyContractError("terminal arm is invalid")
        for value, label in (
            (self.request_sha256, "terminal request"),
            (self.reservation_sha256, "terminal reservation"),
            (self.grant_sha256, "terminal grant"),
            (self.manifest_sha256, "terminal manifest"),
            (self.repository_root_identity_sha256, "terminal root"),
        ):
            _digest(value, label)
        if self.prior_journal_sha256 is not None:
            _digest(self.prior_journal_sha256, "terminal previous journal")
        _identifier(self.audit_domain, "terminal audit domain")
        _text(self.invocation_owner, "terminal invocation owner", maximum=256)
        _count(self.terminal_sequence, "terminal sequence", positive=True)
        if type(self.response_ref) is not ArtifactRefV5 or (
            self.parsed_ref is not None and type(self.parsed_ref) is not ArtifactRefV5
        ):
            raise StudyContractError("terminal response refs are invalid")
        if type(self.attempt) is not RoleAttemptFactsV5 or type(self.usage) is not RoleUsageFactsV5:
            raise StudyContractError("terminal attempt facts are invalid")
        if type(self.receipt) is not RoleTerminalReceiptV5:
            raise StudyContractError("terminal receipt is invalid")
        if self.receipt.attempt_facts_sha256 != self.attempt.sha256:
            raise StudyAuthorityError("terminal receipt does not bind its attempt facts")
        if self.failure_code is not None and type(self.failure_code) is not RoleFailureCode:
            raise StudyContractError("terminal failure code is invalid")
        if self.failure_reason is not None:
            _text(self.failure_reason, "terminal failure reason", maximum=1024)
        _digest(self.terminal_sha256, "terminal identity")
        if self.terminal_sha256 != hashlib.sha256(canonical_json_bytes_v5(self.authenticated_payload())).hexdigest():
            raise StudyContractError("terminal digest differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("terminal schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "arm": self.arm,
            "request_sha256": self.request_sha256,
            "reservation_sha256": self.reservation_sha256,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "repository_root_identity_sha256": self.repository_root_identity_sha256,
            "audit_domain": self.audit_domain,
            "invocation_owner": self.invocation_owner,
            "prior_journal_sha256": self.prior_journal_sha256,
            "terminal_sequence": self.terminal_sequence,
            "response_ref": _ref(self.response_ref),
            "parsed_ref": None if self.parsed_ref is None else _ref(self.parsed_ref),
            "attempt": _attempt_primitive(self.attempt),
            "usage": _usage_primitive(self.usage),
            "receipt": _receipt_primitive(self.receipt),
            "failure_code": None if self.failure_code is None else self.failure_code.value,
            "failure_reason": self.failure_reason,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"terminal_sha256": self.terminal_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyCallTerminalV1":
        fields_set = {
            "study_id",
            "arm",
            "request_sha256",
            "reservation_sha256",
            "grant_sha256",
            "manifest_sha256",
            "repository_root_identity_sha256",
            "audit_domain",
            "invocation_owner",
            "prior_journal_sha256",
            "terminal_sequence",
            "response_ref",
            "parsed_ref",
            "attempt",
            "usage",
            "receipt",
            "failure_code",
            "failure_reason",
            "terminal_sha256",
            "schema_version",
        }
        raw = _strict(value, fields_set, "study terminal")
        code = raw["failure_code"]
        if code is not None:
            try:
                code = RoleFailureCode(code)
            except ValueError as exc:
                raise StudyContractError("terminal failure code is invalid") from exc
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            arm=raw["arm"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            repository_root_identity_sha256=raw["repository_root_identity_sha256"],  # type: ignore[arg-type]
            audit_domain=raw["audit_domain"],  # type: ignore[arg-type]
            invocation_owner=raw["invocation_owner"],  # type: ignore[arg-type]
            prior_journal_sha256=raw["prior_journal_sha256"],  # type: ignore[arg-type]
            terminal_sequence=raw["terminal_sequence"],  # type: ignore[arg-type]
            response_ref=_ref_from(raw["response_ref"], "terminal response ref"),
            parsed_ref=(None if raw["parsed_ref"] is None else _ref_from(raw["parsed_ref"], "terminal parsed ref")),
            attempt=_attempt_from(raw["attempt"]),
            usage=_usage_from(raw["usage"]),
            receipt=_receipt_from(raw["receipt"]),
            failure_code=code,  # type: ignore[arg-type]
            failure_reason=raw["failure_reason"],  # type: ignore[arg-type]
            terminal_sha256=raw["terminal_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyCallTerminalV1":
        return _decode_canonical(cls, raw, "study terminal")


@dataclass(frozen=True, slots=True)
class AuthenticatedStudyTerminalV1:
    reference: ArtifactRefV5
    terminal: StudyCallTerminalV1

    def __post_init__(self) -> None:
        if type(self.reference) is not ArtifactRefV5 or type(self.terminal) is not StudyCallTerminalV1:
            raise StudyAuthorityError("authenticated study terminal is invalid")
        if self.reference.relative_path != f"adapter-blobs/study-v1-terminals/{self.terminal.terminal_sha256}.bin":
            raise StudyAuthorityError("authenticated terminal path is not deterministic")
        if self.reference.sha256 != _sha256(self.terminal.canonical_bytes()):
            raise StudyAuthorityError("authenticated terminal bytes differ from its reference")

    @property
    def study_id(self) -> str:
        return self.terminal.study_id

    @property
    def arm(self) -> StudyArmV1:
        return self.terminal.arm

    @property
    def request_sha256(self) -> str:
        return self.terminal.request_sha256

    @property
    def failure_code(self) -> RoleFailureCode | None:
        return self.terminal.failure_code

    @property
    def usage(self) -> RoleUsageFactsV5:
        return self.terminal.usage

    @property
    def receipt(self) -> RoleTerminalReceiptV5:
        return self.terminal.receipt

    @property
    def terminal_sha256(self) -> str:
        return self.terminal.terminal_sha256


@dataclass(frozen=True, slots=True)
class StudyDispatchClaimV1:
    """The single durable handoff from a reservation to a provider adapter."""

    study_id: str
    request_sha256: str
    reservation_sha256: str
    invocation_owner: str
    grant_sha256: str
    manifest_sha256: str
    model: str
    messages_sha256: str
    response_schema_sha256: str
    max_output_tokens: int
    claim_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION
    transport_settings_sha256: str = study_transport_settings_sha256_v1()

    def __post_init__(self) -> None:
        _text(self.study_id, "dispatch study ID")
        for value, label in (
            (self.request_sha256, "dispatch request"),
            (self.reservation_sha256, "dispatch reservation"),
            (self.grant_sha256, "dispatch grant"),
            (self.manifest_sha256, "dispatch manifest"),
            (self.messages_sha256, "dispatch messages"),
            (self.response_schema_sha256, "dispatch schema"),
            (self.claim_sha256, "dispatch identity"),
            (self.transport_settings_sha256, "dispatch transport settings"),
        ):
            _digest(value, label)
        _text(self.invocation_owner, "dispatch invocation owner", maximum=256)
        _text(self.model, "dispatch model", maximum=256)
        _count(self.max_output_tokens, "dispatch output tokens", positive=True)
        if self.claim_sha256 != _sha256(canonical_json_bytes_v5(self.authenticated_payload())):
            raise StudyContractError("dispatch identity differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("dispatch schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "request_sha256": self.request_sha256,
            "reservation_sha256": self.reservation_sha256,
            "invocation_owner": self.invocation_owner,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "model": self.model,
            "messages_sha256": self.messages_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "max_output_tokens": self.max_output_tokens,
            "transport_settings_sha256": self.transport_settings_sha256,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"claim_sha256": self.claim_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyDispatchClaimV1":
        raw = _strict(
            value,
            {
                "study_id",
                "request_sha256",
                "reservation_sha256",
                "invocation_owner",
                "grant_sha256",
                "manifest_sha256",
                "model",
                "messages_sha256",
                "response_schema_sha256",
                "max_output_tokens",
                "claim_sha256",
                "schema_version",
                "transport_settings_sha256",
            },
            "study dispatch claim",
        )
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            invocation_owner=raw["invocation_owner"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            model=raw["model"],  # type: ignore[arg-type]
            messages_sha256=raw["messages_sha256"],  # type: ignore[arg-type]
            response_schema_sha256=raw["response_schema_sha256"],  # type: ignore[arg-type]
            max_output_tokens=raw["max_output_tokens"],  # type: ignore[arg-type]
            claim_sha256=raw["claim_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
            transport_settings_sha256=raw["transport_settings_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyDispatchClaimV1":
        return _decode_canonical(cls, raw, "study dispatch claim")


@dataclass(frozen=True, slots=True)
class StudyUsageReconciliationV1:
    """Immutable authenticated usage supplied after an uncertain dispatch."""

    study_id: str
    request_sha256: str
    reservation_sha256: str
    invocation_owner: str
    grant_sha256: str
    manifest_sha256: str
    provider_reference: str
    completion_sha256: str
    event_sha256: str
    schema_version: Literal[1] = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.study_id, "reconciliation study ID")
        for value, label in (
            (self.request_sha256, "reconciliation request"),
            (self.reservation_sha256, "reconciliation reservation"),
            (self.grant_sha256, "reconciliation grant"),
            (self.manifest_sha256, "reconciliation manifest"),
            (self.completion_sha256, "reconciliation completion"),
            (self.event_sha256, "reconciliation event"),
        ):
            _digest(value, label)
        _text(self.invocation_owner, "reconciliation invocation owner", maximum=256)
        _text(self.provider_reference, "reconciliation provider reference", maximum=512)
        if self.event_sha256 != _sha256(canonical_json_bytes_v5(self.authenticated_payload())):
            raise StudyContractError("reconciliation event identity differs from its payload")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise StudyContractError("reconciliation schema version is invalid")

    def authenticated_payload(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "request_sha256": self.request_sha256,
            "reservation_sha256": self.reservation_sha256,
            "invocation_owner": self.invocation_owner,
            "grant_sha256": self.grant_sha256,
            "manifest_sha256": self.manifest_sha256,
            "provider_reference": self.provider_reference,
            "completion_sha256": self.completion_sha256,
        }

    def to_primitive(self) -> dict[str, object]:
        result = self.authenticated_payload()
        result.update({"event_sha256": self.event_sha256, "schema_version": self.schema_version})
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyUsageReconciliationV1":
        raw = _strict(
            value,
            {
                "study_id",
                "request_sha256",
                "reservation_sha256",
                "invocation_owner",
                "grant_sha256",
                "manifest_sha256",
                "provider_reference",
                "completion_sha256",
                "event_sha256",
                "schema_version",
            },
            "study reconciliation event",
        )
        return cls(
            study_id=raw["study_id"],  # type: ignore[arg-type]
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
            reservation_sha256=raw["reservation_sha256"],  # type: ignore[arg-type]
            invocation_owner=raw["invocation_owner"],  # type: ignore[arg-type]
            grant_sha256=raw["grant_sha256"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifest_sha256"],  # type: ignore[arg-type]
            provider_reference=raw["provider_reference"],  # type: ignore[arg-type]
            completion_sha256=raw["completion_sha256"],  # type: ignore[arg-type]
            event_sha256=raw["event_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyUsageReconciliationV1":
        return _decode_canonical(cls, raw, "study reconciliation event")


def _decode_canonical(cls: type[object], raw: bytes | str, label: str):
    encoded = raw.encode("utf-8") if type(raw) is str else raw
    if type(encoded) is not bytes:
        raise StudyContractError(f"{label} bytes are invalid")
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_pairs(pairs, label),
            parse_constant=lambda constant: (_ for _ in ()).throw(StudyContractError("nonfinite JSON")),
        )
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} JSON is invalid") from exc
    parsed = cls.from_primitive(value)
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


class StudyLedgerV1:
    """Create-only journal that accounts for each arm exactly once."""

    def __init__(
        self,
        store: StudyStoreV1,
        manifest: StudyManifestV1,
        grant: StudyGrantV1,
        approval: StudyExecutionApprovalV1 | None = None,
    ) -> None:
        if type(store) is not StudyStoreV1 or type(manifest) is not StudyManifestV1 or type(grant) is not StudyGrantV1:
            raise StudyContractError("study ledger inputs are invalid")
        if grant.study_id != manifest.study_id or grant.manifest_sha256 != manifest.sha256:
            raise StudyAuthorityError("study grant does not bind the manifest")
        if grant.repository_root_identity_sha256 != store.repository.root_identity_sha256:
            raise StudyAuthorityError("study grant does not bind the repository root")
        if grant.mode != manifest.mode:
            raise StudyAuthorityError("study grant mode differs from the manifest")
        self.store = store
        self.manifest = manifest
        self.grant = grant
        self.approval = approval
        self._lock = threading.RLock()
        self._owner_prefix = f"{os.getpid()}:{threading.get_ident()}"
        self._owner_capabilities: dict[str, object] = {}
        if approval is not None:
            self._check_approval(approval)
        self._authenticate_authority_records(publish=approval is not None)
        self._authenticate_setup_graph()
        self._authenticate_graph_orphans(self._reservations(), self._terminals())

    @property
    def study_id(self) -> str:
        return self.grant.study_id

    def _check_approval(self, approval: StudyExecutionApprovalV1) -> None:
        if type(approval) is not StudyExecutionApprovalV1 or not approval._is_controller_capability():
            raise StudyAuthorityError("study approval is not a controller-issued capability")
        if (
            approval.study_id != self.grant.study_id
            or approval.grant_sha256 != self.grant.sha256
            or approval.manifest_sha256 != self.manifest.sha256
            or approval.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
            or approval.mode != self.grant.mode
            or approval.approval_reference != self.grant.operator_approval_reference
        ):
            raise StudyAuthorityError("study approval does not match the current grant")
        if self.grant.mode == "live_study" and approval.capability_kind != "live":
            raise StudyAuthorityError("live study requires a live execution capability")
        if self.grant.mode == "offline_fixture" and approval.capability_kind != "offline_fixture":
            raise StudyAuthorityError("offline study requires an explicit fixture capability")

    def _authenticate_authority_records(self, *, publish: bool) -> None:
        """Read the persisted grant/manifest, publishing only with approval."""

        expected = (
            ("grants", self.grant.study_id, self.grant.canonical_bytes(), StudyGrantV1, self.grant),
            ("manifests", self.manifest.sha256, self.manifest.canonical_bytes(), StudyManifestV1, self.manifest),
        )
        for kind, key, content, value_type, expected_value in expected:
            refs = self.store.list_refs(kind=kind, maximum_entries=_MAX_RECORDS)
            expected_path = f"adapter-blobs/study-v1-{kind}/{key}.bin"
            if not refs:
                if not publish:
                    raise StudyAuthorityError(f"persisted study {kind[:-1]} authority is missing")
                self.store.put(kind=kind, key=key, content=content)
                continue
            if len(refs) != 1 or refs[0].relative_path != expected_path:
                raise StudyAuthorityError(f"persisted study {kind[:-1]} authority is ambiguous")
            raw = self.store.read(refs[0])
            if refs[0].sha256 != _sha256(raw) or raw != content:
                raise StudyAuthorityError(f"persisted study {kind[:-1]} authority differs")
            parsed = value_type.from_canonical_json(raw)  # type: ignore[attr-defined]
            if parsed != expected_value:
                raise StudyAuthorityError(f"persisted study {kind[:-1]} authority differs")

    def _ensure_new_call_authorized(self) -> None:
        if self.approval is None:
            raise StudyAuthorityError("new study reservations require current explicit approval")
        self._check_approval(self.approval)

    def _validate_live_deadline(self, deadline_monotonic: float) -> None:
        """Reject deterministic live-window errors before reserving a slot."""

        if self.grant.mode != "live_study":
            return
        if type(deadline_monotonic) is not float or not math.isfinite(deadline_monotonic):
            raise StudyAdmissionError("study live deadline is invalid")
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0 or remaining > float(self.grant.per_call_deadline_seconds):
            raise StudyAdmissionError("study live deadline is outside the admitted call window")

    def _authenticate_setup_graph(self) -> None:
        """Authenticate the bounded setup records owned by this adapter."""

        preflight_refs = self._refs("preflights")
        expected_preflights = {
            self.manifest.primary_preflight_ref,
            self.manifest.withheld_preflight_ref,
        }
        if len(preflight_refs) != 2 or set(preflight_refs) != expected_preflights:
            raise StudyAuthorityError("study preflight setup graph is not exactly pinned")
        preflights: dict[StudyArmV1, FixturePreflightV1] = {}
        for reference in preflight_refs:
            preflight = FixturePreflightV1.from_canonical_json(self.store.read(reference))
            if reference.sha256 != _sha256(preflight.canonical_bytes()):
                raise StudyAuthorityError("study preflight reference differs from its bytes")
            authenticate_fixture_preflight_v1(preflight, require_current=False)
            if preflight.mode != self.manifest.mode:
                raise StudyAuthorityError("study preflight mode differs from the manifest")
            expected = self.manifest.primary_preflight_ref if preflight.arm == "primary" else self.manifest.withheld_preflight_ref
            if reference != expected:
                raise StudyAuthorityError("study preflight arm reference differs from the manifest")
            if preflight.checkpoint_ref != self.manifest.round_one_checkpoint_ref or preflight.snapshot_ref != self.manifest.round_one_snapshot_ref:
                raise StudyAuthorityError("study preflight does not bind the manifest round-two authority")
            preflights[preflight.arm] = preflight
        if set(preflights) != {"primary", "withheld"}:
            raise StudyAuthorityError("study preflight graph does not contain both arm slots")
        if (
            preflights["primary"].fixture_root_identity_sha256
            == preflights["withheld"].fixture_root_identity_sha256
            or self.store.repository.root_identity_sha256 in {
                preflights["primary"].fixture_root_identity_sha256,
                preflights["withheld"].fixture_root_identity_sha256,
            }
        ):
            raise StudyAuthorityError("study arm fixture roots must be distinct from each other and the shared ledger")
        rubric_refs = self._refs("rubrics")
        if len(rubric_refs) != 1 or rubric_refs[0].sha256 != self.manifest.rubric_sha256:
            raise StudyAuthorityError("study rubric setup graph is not exactly pinned")

    def _transition(self):
        return self.store.repository.adapter_state_transition(namespace=_LEDGER_NAMESPACE, key=self.study_id)

    def _refs(self, kind: str) -> tuple[ArtifactRefV5, ...]:
        return self.store.list_refs(kind=kind, maximum_entries=_MAX_RECORDS)

    def _read_kind(self, kind: str) -> tuple[tuple[ArtifactRefV5, bytes], ...]:
        return tuple((reference, self.store.read(reference)) for reference in self._refs(kind))

    @staticmethod
    def _publication_key(reservation_sha256: str) -> str:
        return f"{_PENDING_REQUEST_PREFIX}{reservation_sha256}"

    def _reservation_publications(self) -> tuple[tuple[ArtifactRefV5, _StudyReservationPublicationV1], ...]:
        """Read and authenticate pending reservation intent envelopes."""

        result: list[tuple[ArtifactRefV5, _StudyReservationPublicationV1]] = []
        request_namespace = "adapter-blobs/study-v1-requests/"
        seen_requests: set[str] = set()
        seen_reservations: set[str] = set()
        for ref in self._refs("requests"):
            if not ref.relative_path.startswith(request_namespace):
                raise StudyAuthorityError("study request namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if not key.startswith(_PENDING_REQUEST_PREFIX):
                continue
            raw = self.store.read(ref)
            if ref.relative_path != f"{request_namespace}{key}.bin" or ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("study reservation publication reference differs from its bytes")
            try:
                publication = _StudyReservationPublicationV1.from_canonical_json(raw)
            except (StudyContractError, StudyAdmissionError, TypeError, ValueError) as exc:
                raise StudyAuthorityError("study reservation publication is not authenticated") from exc
            if key != self._publication_key(publication.reservation_sha256):
                raise StudyAuthorityError("study reservation publication path is not deterministic")
            if (
                publication.study_id != self.study_id
                or publication.grant_sha256 != self.grant.sha256
                or publication.manifest_sha256 != self.manifest.sha256
                or publication.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
                or publication.audit_domain != self.grant.audit_domain
                or publication.arm not in self.grant.arm_slots
            ):
                raise StudyAuthorityError("study reservation publication authority identity differs")
            try:
                self._validate_request(publication.request, require_current=False)
                self._validate_reservation_request_binding(publication.reservation, publication.request)
            except (StudyAdmissionError, StudyAuthorityError, StudyContractError) as exc:
                raise StudyAuthorityError("study reservation publication request is not authorized") from exc
            reservation = publication.reservation
            if (
                reservation.grant_sha256 != self.grant.sha256
                or reservation.manifest_sha256 != self.manifest.sha256
                or reservation.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
                or reservation.audit_domain != self.grant.audit_domain
                or reservation.slot_id != f"{self.study_id}:{reservation.arm}"
            ):
                raise StudyAuthorityError("study reservation publication reservation authority differs")
            if publication.request_sha256 in seen_requests or publication.reservation_sha256 in seen_reservations:
                raise StudyAuthorityError("study reservation publications contain a duplicate identity")
            seen_requests.add(publication.request_sha256)
            seen_reservations.add(publication.reservation_sha256)
            result.append((ref, publication))
        return tuple(sorted(result, key=lambda item: item[1].reservation.sequence))

    def _reservations(self) -> tuple[tuple[ArtifactRefV5, StudyReservationV1], ...]:
        result = []
        for ref, raw in self._read_kind("reservations"):
            reservation = _decode_canonical(StudyReservationV1, raw, "study reservation")
            if ref.relative_path != f"adapter-blobs/study-v1-reservations/{reservation.reservation_sha256}.bin":
                raise StudyAuthorityError("reservation path is not deterministic")
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("reservation reference digest differs from bytes")
            if (
                reservation.study_id != self.study_id
                or reservation.grant_sha256 != self.grant.sha256
                or reservation.manifest_sha256 != self.manifest.sha256
                or reservation.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
                or reservation.audit_domain != self.grant.audit_domain
                or reservation.arm not in self.grant.arm_slots
                or reservation.slot_id != f"{self.study_id}:{reservation.arm}"
            ):
                raise StudyAuthorityError("reservation authority identity differs")
            result.append((ref, reservation))
        ordered = tuple(sorted(result, key=lambda item: item[1].sequence))
        if tuple(item.sequence for _ref_value, item in ordered) != tuple(range(1, len(ordered) + 1)):
            raise StudyAuthorityError("study reservation sequence has a gap or duplicate")
        if len({item.request_sha256 for _ref_value, item in ordered}) != len(ordered):
            raise StudyAuthorityError("study reservations contain a duplicate request")
        if len({item.arm for _ref_value, item in ordered}) != len(ordered):
            raise StudyAuthorityError("study reservations contain a duplicate arm slot")
        if len({item.slot_id for _ref_value, item in ordered}) > 2:
            raise StudyAuthorityError("study reservations exceed the two shared arm slots")
        return ordered

    def read_only_reservation_projection(
        self,
    ) -> tuple[tuple[StudyReservationV1, ArtifactRefV5 | None, ArtifactRefV5 | None], ...]:
        """Return the authenticated persisted/envelope reservation union without repair.

        Each tuple contains the reservation, its ordinary reservation reference
        when materialized, and its publication reference when envelope-bound.
        The latter keeps an interrupted publication visible to read-only
        accounting without granting dispatch or mutating the study store.
        """

        reservations = self._reservations()
        terminals = self._terminals()
        self._authenticate_graph_orphans(reservations, terminals)
        publication_refs = {
            publication.reservation_sha256: reference
            for reference, publication in self._reservation_publications()
        }
        persisted_ids = {reservation.reservation_sha256 for _reference, reservation in reservations}
        projected = [
            (reservation, reference, publication_refs.get(reservation.reservation_sha256))
            for reference, reservation in reservations
        ]
        projected.extend(
            (publication.reservation, None, reference)
            for reference, publication in self._reservation_publications()
            if publication.reservation_sha256 not in persisted_ids
        )
        return tuple(sorted(projected, key=lambda item: item[0].sequence))

    def _terminals(self) -> tuple[AuthenticatedStudyTerminalV1, ...]:
        result = []
        for ref, raw in self._read_kind("terminals"):
            terminal = _decode_canonical(StudyCallTerminalV1, raw, "study terminal")
            if ref.relative_path != f"adapter-blobs/study-v1-terminals/{terminal.terminal_sha256}.bin":
                raise StudyAuthorityError("terminal path is not deterministic")
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("terminal reference digest differs from bytes")
            result.append(AuthenticatedStudyTerminalV1(ref, terminal))
        ordered = tuple(sorted(result, key=lambda item: item.terminal.terminal_sequence))
        previous = None
        expected_sequence = 1
        requests: set[str] = set()
        reservations: set[str] = set()
        arms: set[StudyArmV1] = set()
        for item in ordered:
            record = item.terminal
            if record.study_id != self.study_id:
                raise StudyAuthorityError("terminal belongs to another study")
            if (
                record.grant_sha256 != self.grant.sha256
                or record.manifest_sha256 != self.manifest.sha256
                or record.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
                or record.audit_domain != self.grant.audit_domain
                or record.arm not in self.grant.arm_slots
            ):
                raise StudyAuthorityError("terminal authority identity differs")
            if record.terminal_sequence != expected_sequence:
                raise StudyAuthorityError("study terminal sequence has a gap or duplicate")
            if record.prior_journal_sha256 != previous:
                raise StudyAuthorityError("study terminal journal chain is forked")
            if record.request_sha256 in requests or record.reservation_sha256 in reservations:
                raise StudyAuthorityError("study terminal journal contains a duplicate slot settlement")
            if record.arm in arms:
                raise StudyAuthorityError("study terminal journal contains a duplicate arm settlement")
            requests.add(record.request_sha256)
            reservations.add(record.reservation_sha256)
            arms.add(record.arm)
            previous = record.terminal_sha256
            expected_sequence += 1
        return ordered

    def _response_metadata(self, request: StudyCallRequestV1) -> tuple[ArtifactRefV5, dict[str, object]] | None:
        key = request.sha256
        matches = [ref for ref in self._refs(kind="responses") if ref.relative_path == f"adapter-blobs/study-v1-responses/{key}.bin"]
        if len(matches) > 1:
            raise StudyAuthorityError("study response record is duplicated")
        if not matches:
            return None
        ref = matches[0]
        raw = self.store.read(ref)
        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=lambda pairs: _unique_pairs(pairs, "study response record"),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, StudyContractError) as exc:
            raise StudyAuthorityError("study response record is invalid") from exc
        if type(decoded) is not dict or set(decoded) != {
            "request_sha256", "completion_metadata", "response_sha256", "response_length"
        }:
            raise StudyAuthorityError("study response metadata fields are invalid")
        if decoded["request_sha256"] != key or ref.sha256 != _sha256(raw) or canonical_json_bytes_v5(decoded) != raw:
            raise StudyAuthorityError("study response metadata reference differs")
        _completion_from_metadata(decoded["completion_metadata"], "")
        _digest(decoded["response_sha256"], "study response digest")
        if type(decoded["response_length"]) is not int or decoded["response_length"] < 0:
            raise StudyAuthorityError("study response length is invalid")
        return ref, decoded

    def _raw_response_reference(self, request: StudyCallRequestV1) -> ArtifactRefV5 | None:
        normal = [ref for ref in self._refs("raw-responses") if ref.relative_path == f"adapter-blobs/study-v1-raw-responses/{request.sha256}.bin"]
        unavailable = [ref for ref in self._refs("raw-response-failures") if ref.relative_path == f"adapter-blobs/study-v1-raw-response-failures/{request.sha256}.bin"]
        if len(normal) + len(unavailable) > 1:
            raise StudyAuthorityError("study response has duplicate raw persistence records")
        return (normal or unavailable or [None])[0]

    def _raw_failure(self, request: StudyCallRequestV1) -> tuple[ArtifactRefV5, dict[str, object]] | None:
        reference = next(
            (ref for ref in self._refs("raw-response-failures") if ref.relative_path == f"adapter-blobs/study-v1-raw-response-failures/{request.sha256}.bin"),
            None,
        )
        if reference is None:
            return None
        raw = self.store.read(reference)
        value = _decode_canonical_json_object(raw, "study unavailable raw response marker")
        if type(value) is not dict or set(value) != {"request_sha256", "raw_sha256", "raw_length", "reason", "unavailable_bytes"}:
            raise StudyAuthorityError("study unavailable raw response marker is invalid")
        if value["request_sha256"] != request.sha256 or type(value["raw_length"]) is not int or value["raw_length"] <= _MAX_RESPONSE_BYTES:
            raise StudyAuthorityError("study unavailable raw response marker is not bounded")
        _digest(value["raw_sha256"], "study unavailable raw response digest")
        _text(value["reason"], "study unavailable raw response reason", maximum=1024)
        if value["unavailable_bytes"] != _OVERSIZE_RESPONSE_LIMITATION:
            raise StudyAuthorityError("study unavailable raw response limitation is invalid")
        if value["reason"] != _OVERSIZE_RESPONSE_REASON:
            raise StudyAuthorityError("study unavailable raw response reason is invalid")
        if reference.sha256 != _sha256(raw):
            raise StudyAuthorityError("study unavailable raw response marker reference differs")
        return reference, value

    def _response_record(self, request: StudyCallRequestV1) -> tuple[ArtifactRefV5, CompletionResultV5] | None:
        metadata_record = self._response_metadata(request)
        if metadata_record is None:
            return None
        metadata_ref, value = metadata_record
        raw_ref = self._raw_response_reference(request)
        if raw_ref is None or raw_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
            return None
        raw = self.store.read(raw_ref)
        if _sha256(raw) != value["response_sha256"] or len(raw) != value["response_length"]:
            raise StudyAuthorityError("study raw response differs from its bounded usage metadata")
        try:
            response_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StudyAuthorityError("study raw response is not UTF-8") from exc
        completion = _completion_from_metadata(value["completion_metadata"], response_text)
        return metadata_ref, completion

    def _provider_diagnostic_records(self) -> dict[str, tuple[ArtifactRefV5, dict[str, object]]]:
        """Read safe pending-provider diagnostic envelopes without exception text."""

        result: dict[str, tuple[ArtifactRefV5, dict[str, object]]] = {}
        namespace = "adapter-blobs/study-v1-provider-diagnostics/"
        fields = {
            "request_sha256", "reservation_sha256", "claim_sha256", "grant_sha256", "manifest_sha256",
            "phase", "code", "http_status", "provider_request_id", "accounting_status",
        }
        for ref in self._refs("provider-diagnostics"):
            if not ref.relative_path.startswith(namespace):
                raise StudyAuthorityError("study provider diagnostic namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"{namespace}{key}.bin":
                raise StudyAuthorityError("study provider diagnostic path is not deterministic")
            _digest(key, "study provider diagnostic request")
            if key in result:
                raise StudyAuthorityError("study provider diagnostic is duplicated")
            raw = self.store.read(ref)
            try:
                value = _decode_canonical_json_object(raw, "study provider diagnostic")
                diagnostic_fields = fields | ({"cleanup_diagnostic"} if "cleanup_diagnostic" in value else set())
                payload = _strict(value, diagnostic_fields, "study provider diagnostic")
                if canonical_json_bytes_v5(payload) != raw:
                    raise StudyAuthorityError("study provider diagnostic is not canonical")
                for field in ("request_sha256", "reservation_sha256", "claim_sha256", "grant_sha256", "manifest_sha256"):
                    _digest(payload[field], f"study provider diagnostic {field}")
                if payload["request_sha256"] != key or payload["accounting_status"] != "pending":
                    raise StudyAuthorityError("study provider diagnostic identity or accounting state is invalid")
                ProviderFailureDiagnosticV5(
                    phase=payload["phase"],  # type: ignore[arg-type]
                    code=payload["code"],  # type: ignore[arg-type]
                    http_status=payload["http_status"],  # type: ignore[arg-type]
                    provider_request_id=payload["provider_request_id"],  # type: ignore[arg-type]
                    cleanup_diagnostic=_optional_cleanup_diagnostic(payload),
                )
            except (StudyContractError, TypeError, ValueError) as exc:
                raise StudyAuthorityError("study provider diagnostic is invalid") from exc
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("study provider diagnostic reference differs from bytes")
            result[key] = (ref, payload)
        return result

    def _observed_response_records(
        self,
    ) -> dict[str, tuple[ArtifactRefV5, dict[str, object], ArtifactRefV5 | None, bytes | None]]:
        """Read bounded pending-accounting observations with their exact bytes."""

        observation_refs: dict[str, ArtifactRefV5] = {}
        for ref in self._refs("response-observations"):
            namespace = "adapter-blobs/study-v1-response-observations/"
            if not ref.relative_path.startswith(namespace):
                raise StudyAuthorityError("study response observation namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"{namespace}{key}.bin":
                raise StudyAuthorityError("study response observation path is not deterministic")
            _digest(key, "study response observation request")
            if key in observation_refs:
                raise StudyAuthorityError("study response observation is duplicated")
            observation_refs[key] = ref
        raw_refs: dict[str, ArtifactRefV5] = {}
        for ref in self._refs("observed-raw-responses"):
            namespace = "adapter-blobs/study-v1-observed-raw-responses/"
            if not ref.relative_path.startswith(namespace):
                raise StudyAuthorityError("study observed raw response namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"{namespace}{key}.bin":
                raise StudyAuthorityError("study observed raw response path is not deterministic")
            _digest(key, "study observed raw response request")
            if key in raw_refs:
                raise StudyAuthorityError("study observed raw response is duplicated")
            raw_refs[key] = ref
        if not set(raw_refs).issubset(observation_refs):
            raise StudyAuthorityError("study observed raw response has no authenticated observation envelope")
        result: dict[str, tuple[ArtifactRefV5, dict[str, object], ArtifactRefV5 | None, bytes | None]] = {}
        fields = {
            "request_sha256",
            "reservation_sha256",
            "claim_sha256",
            "grant_sha256",
            "manifest_sha256",
            "phase",
            "code",
            "provider_request_id",
            "raw_response_sha256",
            "raw_response_length",
            "accounting_status",
        }
        for key, observation_ref in observation_refs.items():
            observation_raw = self.store.read(observation_ref)
            try:
                value = _decode_canonical_json_object(observation_raw, "study response observation")
                observation_fields = fields | ({"cleanup_diagnostic"} if "cleanup_diagnostic" in value else set())
                payload = _strict(value, observation_fields, "study response observation")
                if canonical_json_bytes_v5(payload) != observation_raw:
                    raise StudyAuthorityError("study response observation is not canonical")
                for field in (
                    "request_sha256",
                    "reservation_sha256",
                    "claim_sha256",
                    "grant_sha256",
                    "manifest_sha256",
                    "raw_response_sha256",
                ):
                    _digest(payload[field], f"study response observation {field}")
                if payload["request_sha256"] != key:
                    raise StudyAuthorityError("study response observation request differs from its path")
                if payload["phase"] != "response_accounting":
                    raise StudyAuthorityError("study response observation phase is invalid")
                if type(payload["code"]) is not str or re.fullmatch(r"[a-z0-9_]{1,64}", payload["code"]) is None:
                    raise StudyAuthorityError("study response observation code is invalid")
                if payload["provider_request_id"] is not None:
                    _text(payload["provider_request_id"], "study response observation provider request ID", maximum=512)
                if type(payload["raw_response_length"]) is not int or not 0 <= payload["raw_response_length"] <= _MAX_RESPONSE_BYTES:
                    raise StudyAuthorityError("study response observation raw response length is invalid")
                if payload["accounting_status"] != "pending":
                    raise StudyAuthorityError("study response observation accounting status is invalid")
                _optional_cleanup_diagnostic(payload)
            except (StudyContractError, TypeError, ValueError) as exc:
                raise StudyAuthorityError("study response observation is invalid") from exc
            if observation_ref.sha256 != _sha256(observation_raw):
                raise StudyAuthorityError("study response observation reference differs from bytes")
            raw_ref = raw_refs.get(key)
            raw = None if raw_ref is None else self.store.read(raw_ref)
            if raw_ref is not None:
                assert raw is not None
                if raw_ref.sha256 != _sha256(raw):
                    raise StudyAuthorityError("study observed raw response reference differs from bytes")
                if payload["raw_response_sha256"] != raw_ref.sha256 or payload["raw_response_length"] != len(raw):
                    raise StudyAuthorityError("study response observation differs from its exact raw bytes")
            result[key] = (observation_ref, payload, raw_ref, raw)
        return result

    def _observed_response_record(
        self,
        request: StudyCallRequestV1,
    ) -> tuple[ArtifactRefV5, dict[str, object], ArtifactRefV5 | None, bytes | None] | None:
        return self._observed_response_records().get(request.sha256)

    def _assert_completion_agrees_with_observation(
        self,
        request: StudyCallRequestV1,
        completion: CompletionResultV5,
        *,
        allow_missing_raw: bool = False,
    ) -> bool:
        """Authenticate response identity; return whether envelope bytes need restoration."""

        observed = self._observed_response_record(request)
        if observed is None:
            return False
        _observation_ref, payload, _raw_ref, raw = observed
        incoming = completion.response_text.encode("utf-8")
        if raw is None:
            if not allow_missing_raw:
                raise StudyPendingAccounting("preserved provider response bytes are not durably available")
            if (
                payload["raw_response_sha256"] != _sha256(incoming)
                or payload["raw_response_length"] != len(incoming)
                or payload["provider_request_id"] != completion.provider_request_id
            ):
                raise StudyAuthorityError("accounted completion differs from the preserved provider response observation")
            return True
        if raw != incoming or payload["provider_request_id"] != completion.provider_request_id:
            raise StudyAuthorityError("accounted completion differs from the preserved provider response observation")
        return False

    def _parse_authenticated_response(self, request: StudyCallRequestV1, raw: bytes) -> StudyResponseV1:
        """Use the one study parser for normal and recovery paths."""

        fixture_request = self._authenticated_fixture_request(request)
        return parse_study_response_v1(
            raw=raw,
            fixture_request=fixture_request,
            configuration_ids=self._configuration_ids(request),
        )

    @staticmethod
    def _classify_parse_failure(exc: BaseException) -> tuple[RoleFailureCode, str]:
        reason = str(exc).strip() or "study response failed its authenticated parser"
        lowered = reason.lower()
        if isinstance(exc, StudyAuthorityError) and any(
            marker in lowered for marker in ("binding", "evidence", "cites", "citation")
        ):
            return RoleFailureCode.EVIDENCE_BINDING, reason
        if "authoring mode" in lowered or "ordinary artifact" in lowered or "projection" in lowered:
            return RoleFailureCode.AUTHORIZATION, reason
        return RoleFailureCode.RESPONSE_SCHEMA, reason

    def _find_terminal(self, request: StudyCallRequestV1) -> AuthenticatedStudyTerminalV1 | None:
        for terminal in self._terminals():
            if terminal.request_sha256 == request.sha256:
                return terminal
        return None

    def _find_reservation(self, request: StudyCallRequestV1) -> StudyReservationV1 | None:
        for _ref_value, reservation in self._reservations():
            if reservation.request_sha256 == request.sha256:
                return reservation
        return None

    def _find_reservation_publication(
        self,
        request: StudyCallRequestV1,
    ) -> _StudyReservationPublicationV1 | None:
        matches = [
            publication
            for _ref_value, publication in self._reservation_publications()
            if publication.request_sha256 == request.sha256
        ]
        if len(matches) > 1:
            raise StudyAuthorityError("study request has duplicate reservation publications")
        return matches[0] if matches else None

    def _has_provider_accounting_prefix(self, request: StudyCallRequestV1) -> bool:
        if self._response_metadata(request) is not None or self._raw_response_reference(request) is not None:
            return True
        parsed_path = f"adapter-blobs/study-v1-parsed/{request.sha256}.bin"
        if any(ref.relative_path == parsed_path for ref in self._refs("parsed")):
            return True
        return any(event.request_sha256 == request.sha256 for event in self._reconciliations())

    def _stored_request(self, request_sha256: str) -> StudyCallRequestV1:
        refs = tuple(
            ref
            for ref in self._refs("requests")
            if ref.relative_path == f"adapter-blobs/study-v1-requests/{request_sha256}.bin"
        )
        if len(refs) != 1:
            raise StudyAuthorityError("study request does not have one authenticated record")
        raw = self.store.read(refs[0])
        request = _decode_canonical(StudyCallRequestV1, raw, "study request")
        if request.sha256 != request_sha256 or refs[0].sha256 != _sha256(raw):
            raise StudyAuthorityError("study request record identity differs")
        return request

    def _validate_reservation_request_binding(
        self,
        reservation: StudyReservationV1,
        request: StudyCallRequestV1,
    ) -> None:
        if (
            reservation.study_id != self.study_id
            or reservation.request_sha256 != request.sha256
            or reservation.arm != request.arm
            or request.study_id != self.study_id
        ):
            raise StudyAuthorityError("study reservation does not bind its stored request study and arm")

    def _stored_preflight(self, request: StudyCallRequestV1) -> FixturePreflightV1:
        raw = self.store.read(request.preflight_ref)
        preflight = FixturePreflightV1.from_canonical_json(raw)
        if request.preflight_ref.sha256 != _sha256(raw) or preflight.sha256 != request.preflight_ref.sha256:
            raise StudyAuthorityError("study preflight bytes differ from their reference")
        if preflight.arm != request.arm or preflight.mode != self.manifest.mode:
            raise StudyAuthorityError("study preflight mode or arm differs from the request")
        if preflight.request_sha256 != request.fixture_request_sha256:
            raise StudyAuthorityError("study preflight F identity differs from the L request")
        return preflight

    def _authenticated_fixture_request(
        self,
        request: StudyCallRequestV1,
        *,
        require_current: bool = False,
    ) -> RoleRequestV5:
        preflight = self._stored_preflight(request)
        authenticated = authenticate_fixture_preflight_v1(preflight, require_current=require_current)
        if authenticated.sha256 != request.fixture_request_sha256:
            raise StudyAuthorityError("study L does not bind the authenticated F request")
        return authenticated

    def _configuration_ids(self, request: StudyCallRequestV1) -> tuple[str, ...]:
        try:
            value = json.loads(request.schema_json.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_pairs(pairs, "study schema"))
            drafts = value["properties"]["drafts"]["items"]
            ids = drafts["properties"]["configuration_id"]["enum"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, StudyContractError) as exc:
            raise StudyAuthorityError("study schema has no authenticated configuration vocabulary") from exc
        if type(ids) is not list or not ids or any(type(item) is not str for item in ids):
            raise StudyAuthorityError("study schema configuration vocabulary is invalid")
        if len(set(ids)) != len(ids):
            raise StudyAuthorityError("study schema configuration vocabulary is not unique")
        return tuple(ids)

    def _request_binding(self, request: StudyCallRequestV1) -> RoleBindingV5:
        """Return the binding from the complete authenticated F envelope."""

        fixture_request = self._authenticated_fixture_request(request)
        try:
            exact_messages = canonical_json_bytes_v5(request.messages) == canonical_json_bytes_v5(
                tuple(fixture_request.messages) + (request.messages[-1],)
            )
        except (TypeError, ValueError) as exc:
            raise StudyAuthorityError("study L message envelope is not canonical") from exc
        if not exact_messages:
            raise StudyAuthorityError("study L does not contain the exact F message tuple")
        return fixture_request.expected_binding

    def _validate_request(self, request: StudyCallRequestV1, *, require_current: bool = False) -> None:
        if type(request) is not StudyCallRequestV1 or request.study_id != self.study_id:
            raise StudyAuthorityError("study request does not belong to this ledger")
        if request.arm not in self.grant.arm_slots:
            raise StudyAdmissionError("study request arm is not granted")
        if request.model != self.grant.model or request.provider != self.grant.provider:
            raise StudyAuthorityError("study request provider/model differs from the grant")
        if (
            request.transport_settings_sha256 != self.grant.transport_settings_sha256
            or request.transport_settings_sha256 != study_transport_settings_sha256_v1()
        ):
            raise StudyAuthorityError("study request transport settings differ from the grant")
        if self.manifest.mode == "live_study":
            settings = self.manifest.provider_settings
            if (
                type(settings) is not StudyProviderSettingsV1
                or request.provider != settings.provider
                or request.model != settings.model
                or request.max_output_tokens != settings.max_output_tokens
            ):
                raise StudyAuthorityError("study live request settings differ from the manifest")
        else:
            fixture_request = self._authenticated_fixture_request(request, require_current=require_current)
            if request.max_output_tokens != fixture_request.max_output_tokens:
                raise StudyAuthorityError("offline study request output bound differs from the authenticated F request")
        if request.max_output_tokens > self.grant.output_token_ceiling:
            raise StudyAdmissionError("study request output bound exceeds the grant")
        expected_ref = self.manifest.primary_preflight_ref if request.arm == "primary" else self.manifest.withheld_preflight_ref
        if request.preflight_ref != expected_ref:
            raise StudyAuthorityError("study request preflight differs from the manifest")
        preflight = self._stored_preflight(request)
        fixture_request = self._authenticated_fixture_request(request, require_current=require_current)
        if preflight.call.request_sha256 != request.fixture_request_sha256:
            raise StudyAuthorityError("study request is not bound to its authenticated F call")
        try:
            exact_messages = canonical_json_bytes_v5(request.messages) == canonical_json_bytes_v5(
                tuple(fixture_request.messages) + (request.messages[-1],)
            )
        except (TypeError, ValueError) as exc:
            raise StudyAuthorityError("study request message envelope is not canonical") from exc
        if not exact_messages:
            raise StudyAuthorityError("study request messages do not preserve the complete F envelope")
        if not isinstance(request.messages[-1], Mapping):
            raise StudyAuthorityError("study request prompt envelope is invalid")
        if request.messages[-1].get("role") != "user":
            raise StudyAuthorityError("study request must append one user study prompt")
        if request.schema_sha256 != self.manifest.schema_sha256:
            raise StudyAuthorityError("study request schema differs from the manifest")
        if _sha256(study_parser_authority_bytes_v1()) != self.manifest.parser_sha256:
            raise StudyAuthorityError("study parser differs from the manifest commitment")
        if request.projected_wire_messages != canonical_json_bytes_v5(wire_role_messages_v5(request.messages)):
            raise StudyAuthorityError("study request wire messages differ from their projection")
        try:
            schema_value = json.loads(request.schema_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudyAuthorityError("study request schema is invalid") from exc
        if request.projected_wire_schema != canonical_json_bytes_v5(wire_role_schema_v5(schema_value)):
            raise StudyAuthorityError("study request wire schema differs from its projection")
        if not request.messages or not isinstance(request.messages[-1], Mapping):
            raise StudyAuthorityError("study request has no appended study prompt")
        if len(request.messages) != len(fixture_request.messages) + 1:
            raise StudyAuthorityError("study request must contain exactly one appended study prompt")
        prompt_content = request.messages[-1].get("content")
        try:
            prompt_hash = hashlib.sha256(canonical_json_bytes_v5(prompt_content)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise StudyAuthorityError("study request prompt is not canonical JSON") from exc
        if prompt_hash != self.manifest.prompt_sha256:
            raise StudyAuthorityError("study request prompt differs from the manifest commitment")
        self._configuration_ids(request)
        self._request_binding(request)

    def _dispatch_claims(self) -> tuple[StudyDispatchClaimV1, ...]:
        result: list[StudyDispatchClaimV1] = []
        for ref, raw in self._read_kind("dispatches"):
            claim = _decode_canonical(StudyDispatchClaimV1, raw, "study dispatch claim")
            if ref.relative_path != f"adapter-blobs/study-v1-dispatches/{claim.request_sha256}.bin":
                raise StudyAuthorityError("dispatch claim path is not deterministic")
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("dispatch claim reference differs from bytes")
            if claim.study_id != self.study_id or claim.grant_sha256 != self.grant.sha256 or claim.manifest_sha256 != self.manifest.sha256:
                raise StudyAuthorityError("dispatch claim authority identity differs")
            if claim.transport_settings_sha256 != study_transport_settings_sha256_v1():
                raise StudyAuthorityError("dispatch claim transport settings are not frozen")
            result.append(claim)
        if len({claim.request_sha256 for claim in result}) != len(result):
            raise StudyAuthorityError("study dispatch claims contain a duplicate request")
        return tuple(result)

    def _admission_rejections(self) -> tuple[tuple[ArtifactRefV5, StudyAdmissionRejectionV1], ...]:
        result: list[tuple[ArtifactRefV5, StudyAdmissionRejectionV1]] = []
        for ref, raw in self._read_kind("admission-rejections"):
            rejection = _decode_canonical(StudyAdmissionRejectionV1, raw, "study admission rejection")
            if ref.relative_path != f"adapter-blobs/study-v1-admission-rejections/{rejection.reservation_sha256}.bin":
                raise StudyAuthorityError("admission rejection path is not deterministic")
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("admission rejection reference differs from bytes")
            if (
                rejection.study_id != self.study_id
                or rejection.grant_sha256 != self.grant.sha256
                or rejection.manifest_sha256 != self.manifest.sha256
                or rejection.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
                or rejection.audit_domain != self.grant.audit_domain
                or rejection.arm not in self.grant.arm_slots
            ):
                raise StudyAuthorityError("admission rejection authority identity differs")
            result.append((ref, rejection))
        if len({item.reservation_sha256 for _ref_value, item in result}) != len(result):
            raise StudyAuthorityError("admission rejections contain a duplicate reservation")
        if len({item.request_sha256 for _ref_value, item in result}) != len(result):
            raise StudyAuthorityError("admission rejections contain a duplicate request")
        return tuple(result)

    def _find_admission_rejection(self, request: StudyCallRequestV1) -> StudyAdmissionRejectionV1 | None:
        matches = [
            rejection
            for _ref_value, rejection in self._admission_rejections()
            if rejection.request_sha256 == request.sha256
        ]
        if len(matches) > 1:
            raise StudyAuthorityError("study request has duplicate admission rejections")
        return matches[0] if matches else None

    def _active_reservations(
        self,
        reservations: tuple[tuple[ArtifactRefV5, StudyReservationV1], ...],
    ) -> tuple[tuple[ArtifactRefV5, StudyReservationV1], ...]:
        rejected = {item.reservation_sha256 for _ref_value, item in self._admission_rejections()}
        return tuple(item for item in reservations if item[1].reservation_sha256 not in rejected)

    def _reconciliations(self) -> tuple[StudyUsageReconciliationV1, ...]:
        result: list[StudyUsageReconciliationV1] = []
        for ref, raw in self._read_kind("reconciliations"):
            event = _decode_canonical(StudyUsageReconciliationV1, raw, "study reconciliation event")
            if ref.relative_path != f"adapter-blobs/study-v1-reconciliations/{event.request_sha256}.bin":
                raise StudyAuthorityError("reconciliation event path is not deterministic")
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("reconciliation event reference differs from bytes")
            if event.study_id != self.study_id or event.grant_sha256 != self.grant.sha256 or event.manifest_sha256 != self.manifest.sha256:
                raise StudyAuthorityError("reconciliation authority identity differs")
            result.append(event)
        if len({event.request_sha256 for event in result}) != len(result):
            raise StudyAuthorityError("reconciliation events contain a duplicate request")
        return tuple(result)

    def claim_dispatch(
        self,
        *,
        request_sha256: str,
        model: str,
        messages: tuple[Mapping[str, object], ...],
        response_schema_json: bytes,
        max_output_tokens: int,
    ) -> StudyDispatchClaimV1:
        """Atomically consume the one provider-dispatch opportunity for L."""

        _digest(request_sha256, "dispatch request")
        self._ensure_new_call_authorized()
        if type(messages) is not tuple or type(response_schema_json) is not bytes or type(max_output_tokens) is not int:
            raise StudyContractError("dispatch request shape is invalid")
        request = self._stored_request(request_sha256)
        self._validate_request(request, require_current=True)
        try:
            wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(messages))
        except (TypeError, ValueError, StudyContractError) as exc:
            raise StudyAuthorityError("provider dispatch messages are invalid") from exc
        if (
            model != request.model
            or response_schema_json != request.schema_json
            or max_output_tokens != request.max_output_tokens
            or wire_messages != request.projected_wire_messages
        ):
            raise StudyAuthorityError("provider dispatch body differs from the authenticated L request")
        if self._find_terminal(request) is not None:
            raise StudyAuthorityError("study request already has a terminal")
        with self._lock, self._transition():
            reservation = self._find_reservation(request)
            if reservation is None:
                raise StudyAuthorityError("provider dispatch has no durable reservation")
            self._validate_reservation_request_binding(reservation, request)
            if reservation.invocation_owner not in self._owner_capabilities:
                raise StudyAuthorityError("provider dispatch owner capability is unavailable")
            terminals = self._terminals()
            if terminals:
                self.verify_terminal(terminals[-1].reference)
            existing = next(
                (claim for claim in self._dispatch_claims() if claim.request_sha256 == request_sha256),
                None,
            )
            if existing is not None:
                raise StudyAuthorityError("study request already has a dispatch claim")
            messages_hash = _sha256(canonical_json_bytes_v5(wire_role_messages_v5(messages)))
            schema_value = json.loads(response_schema_json.decode("utf-8"))
            schema_hash = _sha256(canonical_json_bytes_v5(wire_role_schema_v5(schema_value)))
            payload = {
                "study_id": self.study_id,
                "request_sha256": request_sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "invocation_owner": reservation.invocation_owner,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "model": model,
                "messages_sha256": messages_hash,
                "response_schema_sha256": schema_hash,
                "max_output_tokens": max_output_tokens,
                "transport_settings_sha256": request.transport_settings_sha256,
            }
            claim = StudyDispatchClaimV1(
                study_id=self.study_id,
                request_sha256=request_sha256,
                reservation_sha256=reservation.reservation_sha256,
                invocation_owner=reservation.invocation_owner,
                grant_sha256=self.grant.sha256,
                manifest_sha256=self.manifest.sha256,
                model=model,
                messages_sha256=messages_hash,
                response_schema_sha256=schema_hash,
                max_output_tokens=max_output_tokens,
                transport_settings_sha256=request.transport_settings_sha256,
                claim_sha256=_sha256(canonical_json_bytes_v5(payload)),
            )
            self.store.put(kind="dispatches", key=request_sha256, content=claim.canonical_bytes())
            return claim

    def _prospective_cost(self, request: StudyCallRequestV1) -> Decimal:
        with localcontext() as context:
            context.prec = max(
                100,
                len(self.grant.input_price_upper_bound.as_tuple().digits)
                + len(self.grant.output_price_upper_bound.as_tuple().digits)
                + len(str(request.input_bound_bytes))
                + len(str(request.max_output_tokens))
                + 32,
            )
            context.rounding = ROUND_CEILING
            return (
                Decimal(request.input_bound_bytes) * self.grant.input_price_upper_bound
                + Decimal(request.max_output_tokens) * self.grant.output_price_upper_bound
            ) / Decimal(1_000_000)

    def _usage_totals(self, terminals: tuple[AuthenticatedStudyTerminalV1, ...], reservations: tuple[tuple[ArtifactRefV5, StudyReservationV1], ...]) -> tuple[int, int, Decimal, int, int, Decimal]:
        actual_input = actual_output = 0
        actual_cost = _ZERO
        pending_input = pending_output = 0
        pending_cost = _ZERO
        for item in terminals:
            actual_input += item.usage.input_tokens
            actual_output += item.usage.output_tokens
            actual_cost += item.usage.cost_usd
        terminal_requests = {item.request_sha256 for item in terminals}
        for _ref_value, reservation in reservations:
            if reservation.request_sha256 not in terminal_requests:
                pending_input += reservation.prospective_input_tokens
                pending_output += reservation.prospective_output_tokens
                pending_cost += reservation.prospective_cost_usd
        return actual_input, actual_output, actual_cost, pending_input, pending_output, pending_cost

    def reserve(self, request: StudyCallRequestV1) -> StudyReservationV1:
        self._validate_request(request, require_current=True)
        self._ensure_new_call_authorized()
        with self._lock, self._transition():
            terminals = self._terminals()
            if terminals:
                self.verify_terminal(terminals[-1].reference)
            existing_terminal = self._find_terminal(request)
            if existing_terminal is not None:
                raise StudyAdmissionError("study request already has a terminal")
            reservations = self._reservations()
            publications = self._reservation_publications()
            request_keys = {
                ref.relative_path.rsplit("/", 1)[-1][:-4]
                for ref in self._refs("requests")
                if not ref.relative_path.rsplit("/", 1)[-1][:-4].startswith(_PENDING_REQUEST_PREFIX)
            }
            reservation_keys = {item.reservation_sha256 for _ref_value, item in reservations}
            incomplete_publications = tuple(
                publication
                for _ref_value, publication in publications
                if publication.request_sha256 not in request_keys
                or publication.reservation_sha256 not in reservation_keys
            )
            if incomplete_publications:
                if any(item.request_sha256 == request.sha256 for item in incomplete_publications):
                    raise StudyPendingAccounting("study request has an unrepaired reservation publication")
                raise StudyPendingAccounting("an unrepaired study reservation publication blocks admission")
            if self._find_admission_rejection(request) is not None:
                raise StudyAdmissionError("study request has a prior pre-dispatch rejection")
            existing = self._find_reservation(request)
            if existing is not None:
                raise StudyPendingAccounting("study request already has a pending reservation")
            active_reservations = self._active_reservations(reservations)
            if any(
                item.arm != request.arm
                for _ref_value, item in active_reservations
                if item.request_sha256 not in {term.request_sha256 for term in terminals}
            ):
                raise StudyPendingAccounting("an unresolved study arm reservation blocks the other arm")
            if any(item.arm == request.arm for _ref_value, item in reservations):
                raise StudyAdmissionError("study arm slot has already been consumed")
            prospective_input = request.input_bound_bytes
            prospective_output = request.max_output_tokens
            prospective_cost = self._prospective_cost(request)
            if prospective_input > self.grant.input_token_ceiling:
                raise StudyAdmissionError("prospective input bound exceeds the per-call input ceiling")
            if prospective_output > self.grant.output_token_ceiling:
                raise StudyAdmissionError("prospective output bound exceeds the per-call output ceiling")
            if prospective_cost > self.grant.per_call_usd_ceiling:
                raise StudyAdmissionError("prospective cost exceeds the per-call USD ceiling")
            actual_input, actual_output, actual_cost, pending_input, pending_output, pending_cost = self._usage_totals(
                terminals, active_reservations
            )
            if actual_input + pending_input + prospective_input > self.grant.input_token_ceiling * 2:
                raise StudyAdmissionError("shared input token ceiling is exhausted")
            if actual_output + pending_output + prospective_output > self.grant.cumulative_token_ceiling:
                raise StudyAdmissionError("shared output token ceiling is exhausted")
            if actual_input + actual_output + pending_input + pending_output + prospective_input + prospective_output > self.grant.cumulative_token_ceiling:
                raise StudyAdmissionError("shared cumulative token ceiling is exhausted")
            if actual_cost + pending_cost + prospective_cost > self.grant.cumulative_usd_ceiling:
                raise StudyAdmissionError("shared cumulative USD ceiling is exhausted")
            sequence = len(reservations) + 1
            owner = f"{self._owner_prefix}:{uuid.uuid4().hex}"
            payload = {
                "study_id": self.study_id,
                "arm": request.arm,
                "request_sha256": request.sha256,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
                "audit_domain": self.grant.audit_domain,
                "slot_id": f"{self.study_id}:{request.arm}",
                "invocation_owner": owner,
                "prospective_input_tokens": prospective_input,
                "prospective_output_tokens": prospective_output,
                "prospective_cost_usd": canonical_primitive_v5(prospective_cost),
                "sequence": sequence,
            }
            reservation = StudyReservationV1(
                study_id=self.study_id,
                arm=request.arm,
                request_sha256=request.sha256,
                grant_sha256=self.grant.sha256,
                manifest_sha256=self.manifest.sha256,
                repository_root_identity_sha256=self.store.repository.root_identity_sha256,
                audit_domain=self.grant.audit_domain,
                slot_id=f"{self.study_id}:{request.arm}",
                invocation_owner=owner,
                prospective_input_tokens=prospective_input,
                prospective_output_tokens=prospective_output,
                prospective_cost_usd=prospective_cost,
                sequence=sequence,
                reservation_sha256=_sha256(canonical_json_bytes_v5(payload)),
            )
            publication_payload = {
                "study_id": self.study_id,
                "arm": request.arm,
                "request_sha256": request.sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
                "audit_domain": self.grant.audit_domain,
                "request": request.to_primitive(),
                "reservation": reservation.to_primitive(),
            }
            publication = _StudyReservationPublicationV1(
                study_id=self.study_id,
                arm=request.arm,
                request_sha256=request.sha256,
                reservation_sha256=reservation.reservation_sha256,
                grant_sha256=self.grant.sha256,
                manifest_sha256=self.manifest.sha256,
                repository_root_identity_sha256=self.store.repository.root_identity_sha256,
                audit_domain=self.grant.audit_domain,
                request=request,
                reservation=reservation,
                publication_sha256=_sha256(canonical_json_bytes_v5(publication_payload)),
            )
            # Publish intent first, then the independently addressable request
            # and reservation records.  Each partial prefix is recoverable from
            # the authenticated envelope; no prefix can authorize dispatch.
            self.store.put(
                kind="requests",
                key=self._publication_key(reservation.reservation_sha256),
                content=publication.canonical_bytes(),
            )
            self.store.put(kind="requests", key=request.sha256, content=request.canonical_bytes())
            self.store.put(kind="reservations", key=reservation.reservation_sha256, content=reservation.canonical_bytes())
            self._owner_capabilities[reservation.invocation_owner] = object()
            return reservation

    def reject_pre_dispatch(
        self,
        reservation: StudyReservationV1,
        *,
        reason: str,
    ) -> StudyAdmissionRejectionV1:
        """Record a deterministic no-provider rejection and release the slot."""

        if type(reservation) is not StudyReservationV1:
            raise StudyContractError("study admission rejection reservation is invalid")
        _text(reason, "study admission rejection reason", maximum=1024)
        self._ensure_new_call_authorized()
        with self._lock, self._transition():
            stored = next(
                (item for _ref_value, item in self._reservations() if item.reservation_sha256 == reservation.reservation_sha256),
                None,
            )
            if stored is None or stored != reservation:
                raise StudyAuthorityError("study admission rejection reservation is not authenticated")
            existing = self._find_admission_rejection(self._stored_request(reservation.request_sha256))
            if existing is not None:
                if existing.reason != reason:
                    raise StudyAuthorityError("study admission rejection conflicts with the existing event")
                return existing
            request = self._stored_request(reservation.request_sha256)
            self._validate_reservation_request_binding(reservation, request)
            self._validate_request(request)
            if any(claim.request_sha256 == request.sha256 for claim in self._dispatch_claims()):
                raise StudyAuthorityError("study admission rejection has an authenticated dispatch claim")
            if self._response_metadata(request) is not None or self._raw_response_reference(request) is not None:
                raise StudyAuthorityError("study admission rejection has persisted provider accounting")
            if any(ref.relative_path == f"adapter-blobs/study-v1-parsed/{request.sha256}.bin" for ref in self._refs("parsed")):
                raise StudyAuthorityError("study admission rejection has a parsed response")
            if self._find_terminal(request) is not None or any(
                event.request_sha256 == request.sha256 for event in self._reconciliations()
            ):
                raise StudyAuthorityError("study admission rejection follows a settled provider attempt")
            self._authenticate_graph_orphans(self._reservations(), self._terminals())
            if reservation.invocation_owner not in self._owner_capabilities:
                raise StudyAuthorityError("study admission rejection owner capability is unavailable")
            payload = {
                "study_id": self.study_id,
                "arm": reservation.arm,
                "request_sha256": request.sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
                "audit_domain": self.grant.audit_domain,
                "invocation_owner": reservation.invocation_owner,
                "reason": reason,
            }
            rejection = StudyAdmissionRejectionV1(
                study_id=self.study_id,
                arm=reservation.arm,
                request_sha256=request.sha256,
                reservation_sha256=reservation.reservation_sha256,
                grant_sha256=self.grant.sha256,
                manifest_sha256=self.manifest.sha256,
                repository_root_identity_sha256=self.store.repository.root_identity_sha256,
                audit_domain=self.grant.audit_domain,
                invocation_owner=reservation.invocation_owner,
                reason=reason,
                rejection_sha256=_sha256(canonical_json_bytes_v5(payload)),
            )
            self.store.put(
                kind="admission-rejections",
                key=reservation.reservation_sha256,
                content=rejection.canonical_bytes(),
            )
            self._owner_capabilities.pop(reservation.invocation_owner, None)
            return rejection

    def _persist_response(self, request: StudyCallRequestV1, completion: CompletionResultV5) -> ArtifactRefV5:
        response_bytes = completion.response_text.encode("utf-8")
        raw = canonical_json_bytes_v5(
            {
                "request_sha256": request.sha256,
                "completion_metadata": _completion_metadata_primitive(completion),
                "response_sha256": _sha256(response_bytes),
                "response_length": len(response_bytes),
            }
        )
        return self.store.put(kind="responses", key=request.sha256, content=raw)

    def _persist_raw_response(self, request: StudyCallRequestV1, completion: CompletionResultV5) -> ArtifactRefV5:
        if type(completion.response_text) is not str:
            raise StudyContractError("provider response text is invalid")
        raw = completion.response_text.encode("utf-8")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise StudyAdmissionError("provider response exceeds its frozen persistence bound")
        return self.store.put(kind="raw-responses", key=request.sha256, content=raw)

    def _persist_raw_failure(self, request: StudyCallRequestV1, completion: CompletionResultV5) -> ArtifactRefV5:
        raw = completion.response_text.encode("utf-8")
        if len(raw) <= _MAX_RESPONSE_BYTES:
            raise StudyContractError("raw response failure marker is only for oversized responses")
        marker = {
            "request_sha256": request.sha256,
            "raw_sha256": _sha256(raw),
            "raw_length": len(raw),
            "reason": _OVERSIZE_RESPONSE_REASON,
            "unavailable_bytes": _OVERSIZE_RESPONSE_LIMITATION,
        }
        return self.store.put(
            kind="raw-response-failures",
            key=request.sha256,
            content=canonical_json_bytes_v5(marker),
        )

    def persist_response_for_request(self, request: StudyCallRequestV1, completion: CompletionResultV5) -> ArtifactRefV5:
        """Persist usage and exact raw response before any parse attempt."""

        self._validate_request(request)
        if type(completion) is not CompletionResultV5:
            raise StudyContractError("provider completion is invalid")
        with self._lock, self._transition():
            reservation = self._find_reservation(request)
            if reservation is None:
                raise StudyAuthorityError("provider response has no durable reservation")
            self._validate_reservation_request_binding(reservation, request)
            if not any(claim.request_sha256 == request.sha256 for claim in self._dispatch_claims()):
                raise StudyAuthorityError("provider response has no authenticated dispatch claim")
            self._assert_completion_agrees_with_observation(request, completion)
            if self._find_terminal(request) is not None:
                raise StudyAdmissionError("study request already has a terminal")
            existing = self._response_record(request)
            if existing is not None:
                if existing[1] != completion:
                    raise StudyAuthorityError("study response record conflicts with the existing attempt")
                return self._persist_raw_response(request, completion)
            metadata = self._response_metadata(request)
            if metadata is not None:
                _metadata_ref, value = metadata
                if _completion_metadata_primitive(completion) != value["completion_metadata"]:
                    raise StudyAuthorityError("study response metadata conflicts with the existing attempt")
                if _sha256(completion.response_text.encode("utf-8")) != value["response_sha256"] or len(completion.response_text.encode("utf-8")) != value["response_length"]:
                    raise StudyAuthorityError("study response bytes conflict with the existing usage metadata")
                if self._raw_response_reference(request) is not None:
                    return _metadata_ref
                try:
                    return self._persist_raw_response(request, completion)
                except StudyAdmissionError:
                    return self._persist_raw_failure(request, completion)
            response_record_ref = self._persist_response(request, completion)
            try:
                self._persist_raw_response(request, completion)
            except StudyAdmissionError:
                self._persist_raw_failure(request, completion)
            return response_record_ref

    def persist_provider_diagnostic_for_pending_accounting(
        self, request: StudyCallRequestV1, diagnostic: ProviderFailureDiagnosticV5
    ) -> None:
        """Persist only safe phase/code/status/ID for an unresolved handoff."""

        self._validate_request(request)
        if type(diagnostic) is not ProviderFailureDiagnosticV5:
            raise StudyContractError("provider failure diagnostic is invalid")
        with self._lock, self._transition():
            reservation = self._find_reservation(request)
            if reservation is None:
                raise StudyAuthorityError("provider diagnostic has no durable reservation")
            self._validate_reservation_request_binding(reservation, request)
            claims = tuple(claim for claim in self._dispatch_claims() if claim.request_sha256 == request.sha256)
            if len(claims) != 1:
                raise StudyAuthorityError("provider diagnostic has no authenticated dispatch claim")
            claim = claims[0]
            payload = {
                "request_sha256": request.sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "claim_sha256": claim.claim_sha256,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "phase": diagnostic.phase,
                "code": diagnostic.code,
                "http_status": diagnostic.http_status,
                "provider_request_id": diagnostic.provider_request_id,
                "accounting_status": "pending",
            }
            if diagnostic.cleanup_diagnostic is not None:
                payload["cleanup_diagnostic"] = {
                    "phase": diagnostic.cleanup_diagnostic[0],
                    "code": diagnostic.cleanup_diagnostic[1],
                }
            self.store.put(kind="provider-diagnostics", key=request.sha256, content=canonical_json_bytes_v5(payload))

    def persist_observed_response_for_pending_accounting(
        self, request: StudyCallRequestV1, observed: ProviderResponseAccountingErrorV5
    ) -> None:
        """Durably preserve returned model content without inventing usage."""
        self._validate_request(request)
        if type(observed) is not ProviderResponseAccountingErrorV5:
            raise StudyContractError("observed provider accounting error is invalid")
        if not self.grant.response_persistence_consent:
            return
        raw = observed.response_text.encode("utf-8")
        if len(raw) > _MAX_RESPONSE_BYTES:
            self.persist_provider_diagnostic_for_pending_accounting(
                request,
                ProviderFailureDiagnosticV5(
                    phase="response_accounting",
                    code="response_content_oversized",
                    provider_request_id=observed.provider_request_id,
                    cleanup_diagnostic=observed.cleanup_diagnostic,
                ),
            )
            return
        with self._lock, self._transition():
            reservation = self._find_reservation(request)
            if reservation is None:
                raise StudyAuthorityError("observed provider response has no durable reservation")
            self._validate_reservation_request_binding(reservation, request)
            claims = tuple(claim for claim in self._dispatch_claims() if claim.request_sha256 == request.sha256)
            if len(claims) != 1:
                raise StudyAuthorityError("observed provider response has no authenticated dispatch claim")
            claim = claims[0]
            payload = {
                "request_sha256": request.sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "claim_sha256": claim.claim_sha256,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "phase": observed.phase,
                "code": observed.code,
                "provider_request_id": observed.provider_request_id,
                "raw_response_sha256": _sha256(raw),
                "raw_response_length": len(raw),
                "accounting_status": "pending",
            }
            if observed.cleanup_diagnostic is not None:
                payload["cleanup_diagnostic"] = {
                    "phase": observed.cleanup_diagnostic[0],
                    "code": observed.cleanup_diagnostic[1],
                }
            # Publish the bound envelope first.  If the later byte write is
            # interrupted, reopen can retain a safe pending prefix instead of
            # treating a legitimate write boundary as an orphan.
            self.store.put(kind="response-observations", key=request.sha256, content=canonical_json_bytes_v5(payload))
            try:
                self.store.put(kind="observed-raw-responses", key=request.sha256, content=raw)
            except (StudyAdmissionError, StudyAuthorityError):
                return

    def reconcile_pending_usage(
        self,
        request: StudyCallRequestV1,
        *,
        completion: CompletionResultV5,
        provider_reference: str,
    ) -> AuthenticatedStudyTerminalV1:
        """Authenticate externally supplied usage before freeing an uncertain slot."""

        self._validate_request(request)
        self._ensure_new_call_authorized()
        if type(completion) is not CompletionResultV5:
            raise StudyContractError("reconciled completion is invalid")
        _text(provider_reference, "reconciliation provider reference", maximum=512)
        with self._lock, self._transition():
            # A long-lived ledger instance may see append-only records written
            # after construction. Reauthenticate the pinned authority and the
            # full bounded graph under the transition lock before using an
            # envelope to restore provider bytes.
            self._authenticate_authority_records(publish=False)
            self._authenticate_setup_graph()
            self._authenticate_graph_orphans(self._reservations(), self._terminals())
            reservation = self._find_reservation(request)
            if reservation is None:
                raise StudyAuthorityError("reconciliation has no durable reservation")
            self._validate_reservation_request_binding(reservation, request)
            claims = tuple(claim for claim in self._dispatch_claims() if claim.request_sha256 == request.sha256)
            if len(claims) != 1:
                raise StudyAuthorityError("reconciliation has no authenticated dispatch claim")
            claim = claims[0]
            if claim.reservation_sha256 != reservation.reservation_sha256:
                raise StudyAuthorityError("reconciliation dispatch claim differs from its reservation")
            incoming_bytes = completion.response_text.encode("utf-8")
            incoming_completion_hash = _completion_fingerprint(
                completion,
                response_sha256=_sha256(incoming_bytes),
                response_length=len(incoming_bytes),
            )
            prior_events = tuple(event for event in self._reconciliations() if event.request_sha256 == request.sha256)
            if len(prior_events) > 1:
                raise StudyAuthorityError("reconciliation contains duplicate usage events")
            if prior_events:
                prior_event = prior_events[0]
                if (
                    prior_event.provider_reference != provider_reference
                    or prior_event.completion_sha256 != incoming_completion_hash
                ):
                    raise StudyAuthorityError("reconciliation conflicts with the immutable usage event")
            existing = self._find_terminal(request)
            if existing is not None:
                if not prior_events:
                    raise StudyAuthorityError("terminal has no authenticated reconciliation event")
                return self.verify_terminal(existing.reference)
            response = self._response_record(request)
            metadata_record = self._response_metadata(request)
            if response is not None and response[1] != completion:
                raise StudyAuthorityError("reconciliation conflicts with the persisted usage receipt")
            if metadata_record is not None:
                if _completion_metadata_primitive(completion) != metadata_record[1]["completion_metadata"]:
                    raise StudyAuthorityError("reconciliation conflicts with the persisted usage metadata")
                if (
                    metadata_record[1]["response_sha256"] != _sha256(incoming_bytes)
                    or metadata_record[1]["response_length"] != len(incoming_bytes)
                ):
                    raise StudyAuthorityError("reconciliation response differs from the persisted usage metadata")
            restore_observed_raw = self._assert_completion_agrees_with_observation(
                request,
                completion,
                allow_missing_raw=True,
            )
            if restore_observed_raw:
                # Publish only the exact byte sequence authenticated by the
                # observation envelope. The normal reconciliation path below
                # then writes its separate usage receipt and settles once.
                self.store.put(kind="observed-raw-responses", key=request.sha256, content=incoming_bytes)
            if metadata_record is None:
                self._persist_response(request, completion)
            raw_ref = self._raw_response_reference(request)
            if raw_ref is None:
                try:
                    raw_ref = self._persist_raw_response(request, completion)
                except StudyAdmissionError:
                    raw_ref = self._persist_raw_failure(request, completion)
            elif raw_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
                marker = self._raw_failure(request)
                if marker is None or marker[1]["raw_sha256"] != _sha256(completion.response_text.encode("utf-8")):
                    raise StudyAuthorityError("reconciliation raw response marker differs from its usage receipt")
            elif self.store.read(raw_ref) != completion.response_text.encode("utf-8"):
                raise StudyAuthorityError("reconciliation raw response differs from its usage receipt")
            metadata_value = self._response_metadata(request)
            assert metadata_value is not None
            completion_hash = _completion_fingerprint(
                completion,
                response_sha256=metadata_value[1]["response_sha256"],
                response_length=metadata_value[1]["response_length"],
            )
            payload = {
                "study_id": self.study_id,
                "request_sha256": request.sha256,
                "reservation_sha256": reservation.reservation_sha256,
                "invocation_owner": reservation.invocation_owner,
                "grant_sha256": self.grant.sha256,
                "manifest_sha256": self.manifest.sha256,
                "provider_reference": provider_reference,
                "completion_sha256": completion_hash,
            }
            event = StudyUsageReconciliationV1(
                study_id=self.study_id,
                request_sha256=request.sha256,
                reservation_sha256=reservation.reservation_sha256,
                invocation_owner=reservation.invocation_owner,
                grant_sha256=self.grant.sha256,
                manifest_sha256=self.manifest.sha256,
                provider_reference=provider_reference,
                completion_sha256=completion_hash,
                event_sha256=_sha256(canonical_json_bytes_v5(payload)),
            )
            self.store.put(kind="reconciliations", key=request.sha256, content=event.canonical_bytes())
            parsed_ref = None
            parse_code = None
            parse_reason = None
            parse_nonimportable = raw_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/")
            if completion.accepted and completion.response_received and not parse_nonimportable:
                parsed_ref, parse_code, parse_reason = self._parse_and_persist(request, completion, raw_ref)
            code = None if parsed_ref is not None else (
                RoleFailureCode.TRANSPORT
                if (not completion.accepted or not completion.response_received)
                else parse_code or (RoleFailureCode.ACCOUNTING if parse_nonimportable else RoleFailureCode.RESPONSE_SCHEMA)
            )
            return self._settle_locked(
                reservation,
                request,
                completion=completion,
                response_ref=raw_ref,
                parsed_ref=parsed_ref,
                failure_code=code,
                failure_reason=parse_reason or (_OVERSIZE_RESPONSE_REASON if parse_nonimportable else None),
            )

    def _usage_for_completion(self, request: StudyCallRequestV1, completion: CompletionResultV5) -> RoleUsageFactsV5:
        return RoleUsageFactsV5(
            external_attempt_count=completion.external_attempt_count,
            request_started=True,
            response_received=completion.response_received,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            total_tokens=completion.input_tokens + completion.output_tokens,
            cost_usd=completion.cost_usd,
            requested_model=request.model,
            returned_model=completion.returned_model,
            provider_request_id=completion.provider_request_id,
        )

    def _derive_settlement_components(
        self,
        reservation: StudyReservationV1,
        request: StudyCallRequestV1,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
        parsed_ref: ArtifactRefV5 | None,
        failure_code: RoleFailureCode | None,
        terminals: tuple[AuthenticatedStudyTerminalV1, ...],
        failure_reason: str | None = None,
    ) -> tuple[RoleUsageFactsV5, RoleAttemptFactsV5, RoleTerminalReceiptV5, RoleFailureCode | None, str | None]:
        usage = self._usage_for_completion(request, completion)
        prior_input = sum(item.usage.input_tokens for item in terminals)
        prior_output = sum(item.usage.output_tokens for item in terminals)
        prior_cost = sum((item.usage.cost_usd for item in terminals), _ZERO)
        cumulative_tokens = prior_input + prior_output + usage.total_tokens
        cumulative_cost = prior_cost + usage.cost_usd
        accounting_overage = (
            completion.returned_model != request.model
            or completion.input_tokens > self.grant.input_token_ceiling
            or completion.output_tokens > self.grant.output_token_ceiling
            or cumulative_tokens > self.grant.cumulative_token_ceiling
            or completion.cost_usd > self.grant.per_call_usd_ceiling
            or cumulative_cost > self.grant.cumulative_usd_ceiling
        )
        code = failure_code
        reason = failure_reason
        if accounting_overage:
            code = RoleFailureCode.ACCOUNTING
            reason = "provider usage or returned model exceeded the admitted accounting boundary"
        elif code is RoleFailureCode.ACCOUNTING and not response_ref.relative_path.startswith(
            "adapter-blobs/study-v1-raw-response-failures/"
        ):
            raise StudyAuthorityError("study accounting failure is not supported by the authenticated usage")
        elif code is None and (not completion.accepted or not completion.response_received):
            code = RoleFailureCode.TRANSPORT
            reason = "provider did not return an accepted completion"
        elif code is None and parsed_ref is None:
            code = RoleFailureCode.RESPONSE_SCHEMA
            reason = "provider response did not satisfy the study schema"
        if code is not None and reason is None:
            reason = {
                RoleFailureCode.TRANSPORT: "provider did not return an accepted completion",
                RoleFailureCode.RESPONSE_SCHEMA: "provider response did not satisfy the study schema",
                RoleFailureCode.EVIDENCE_BINDING: "provider response failed the study evidence binding",
                RoleFailureCode.AUTHORIZATION: "provider response failed the study authorization boundary",
                RoleFailureCode.ACCOUNTING: "provider usage or returned model exceeded the admitted accounting boundary",
            }[code]
        outcome = "accepted" if code is None else {
            RoleFailureCode.TRANSPORT: "transport_failure",
            RoleFailureCode.RESPONSE_SCHEMA: "response_schema_failure",
            RoleFailureCode.EVIDENCE_BINDING: "evidence_binding_failure",
            RoleFailureCode.AUTHORIZATION: "authorization_failure",
            RoleFailureCode.ACCOUNTING: "accounting_failure",
        }[code]
        attempt = RoleAttemptFactsV5(
            role="investigator",
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=request.sha256,
            slot_id=reservation.slot_id,
            outcome=outcome,  # type: ignore[arg-type]
            failure_code=code,
            usage=usage,
            response_sha256=response_ref.sha256,
            artifact_sha256=None if code is not None else (None if parsed_ref is None else parsed_ref.sha256),
        )
        sequence = len(terminals) + 1
        receipt_payload = {
            "slot_id": reservation.slot_id,
            "slot_request_sha256": request.sha256,
            "authorization_sha256": self.grant.sha256,
            "attempt_facts_sha256": attempt.sha256,
            "cumulative_external_attempts": sum(item.usage.external_attempt_count for item in terminals) + usage.external_attempt_count,
            "cumulative_total_tokens": cumulative_tokens,
            "cumulative_cost_usd": canonical_primitive_v5(cumulative_cost),
            "terminal_sequence": sequence,
        }
        receipt = RoleTerminalReceiptV5(
            slot_id=reservation.slot_id,
            slot_request_sha256=request.sha256,
            authorization_sha256=self.grant.sha256,
            attempt_facts_sha256=attempt.sha256,
            cumulative_external_attempts=receipt_payload["cumulative_external_attempts"],  # type: ignore[arg-type]
            cumulative_total_tokens=cumulative_tokens,
            cumulative_cost_usd=cumulative_cost,
            terminal_sequence=sequence,
            receipt_sha256=_sha256(canonical_json_bytes_v5(receipt_payload)),
        )
        return usage, attempt, receipt, code, reason

    def _parser_classification(
        self,
        request: StudyCallRequestV1,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
        parsed_ref: ArtifactRefV5 | None,
    ) -> tuple[RoleFailureCode | None, str | None]:
        """Recompute parser acceptance and failure from authenticated bytes."""

        if response_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
            marker = self._raw_failure(request)
            if marker is None or marker[0] != response_ref:
                raise StudyAuthorityError("study unavailable raw response marker is not authenticated")
            if parsed_ref is not None:
                raise StudyAuthorityError("non-importable response cannot have a parsed artifact")
            return RoleFailureCode.ACCOUNTING, str(marker[1]["reason"])
        if not completion.accepted or not completion.response_received:
            if parsed_ref is not None:
                raise StudyAuthorityError("transport failure cannot reference a parsed study response")
            return RoleFailureCode.TRANSPORT, "provider did not return an accepted completion"
        raw = self.store.read(response_ref)
        try:
            self._parse_authenticated_response(request, raw)
        except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if parsed_ref is not None:
                raise StudyAuthorityError("study parsed response is not supported by its raw response") from exc
            return self._classify_parse_failure(exc)
        if parsed_ref is None:
            raise StudyAuthorityError("accepted study response has no authenticated parsed artifact")
        return None, None

    def _authenticated_parser_fields(
        self,
        request: StudyCallRequestV1,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
        parsed_ref: ArtifactRefV5 | None,
        failure_code: RoleFailureCode | None,
        failure_reason: str | None,
    ) -> tuple[RoleFailureCode | None, str | None]:
        parser_code, parser_reason = self._parser_classification(request, completion, response_ref, parsed_ref)
        if parser_code is None:
            if failure_code not in {None, RoleFailureCode.ACCOUNTING}:
                raise StudyAuthorityError("study settlement failure code differs from the authenticated parser")
            if failure_code is None and failure_reason is not None:
                raise StudyAuthorityError("study settlement failure reason differs from the authenticated parser")
            return failure_code, failure_reason
        if failure_code is None:
            return parser_code, parser_reason
        if failure_code is RoleFailureCode.ACCOUNTING:
            return failure_code, failure_reason
        if failure_code is not parser_code:
            raise StudyAuthorityError("study settlement failure code differs from the authenticated parser")
        if failure_reason not in {None, parser_reason}:
            raise StudyAuthorityError("study settlement failure reason differs from the authenticated parser")
        return parser_code, parser_reason

    def _settle_locked(
        self,
        reservation: StudyReservationV1,
        request: StudyCallRequestV1,
        *,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
        parsed_ref: ArtifactRefV5 | None,
        failure_code: RoleFailureCode | None,
        failure_reason: str | None,
    ) -> AuthenticatedStudyTerminalV1:
        terminals = self._terminals()
        if terminals:
            self.verify_terminal(terminals[-1].reference)
            terminals = self._terminals()
        failure_code, failure_reason = self._authenticated_parser_fields(
            request,
            completion,
            response_ref,
            parsed_ref,
            failure_code,
            failure_reason,
        )
        usage, attempt, receipt, code, derived_reason = self._derive_settlement_components(
            reservation,
            request,
            completion,
            response_ref,
            parsed_ref,
            failure_code,
            terminals,
            failure_reason=failure_reason,
        )
        failure_reason = derived_reason if derived_reason is not None else failure_reason
        sequence = len(terminals) + 1
        terminal_payload = {
            "study_id": self.study_id,
            "arm": request.arm,
            "request_sha256": request.sha256,
            "reservation_sha256": reservation.reservation_sha256,
            "grant_sha256": self.grant.sha256,
            "manifest_sha256": self.manifest.sha256,
            "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
            "audit_domain": self.grant.audit_domain,
            "invocation_owner": reservation.invocation_owner,
            "prior_journal_sha256": terminals[-1].terminal_sha256 if terminals else None,
            "terminal_sequence": sequence,
            "response_ref": _ref(response_ref),
            "parsed_ref": None if parsed_ref is None else _ref(parsed_ref),
            "attempt": _attempt_primitive(attempt),
            "usage": _usage_primitive(usage),
            "receipt": _receipt_primitive(receipt),
            "failure_code": None if code is None else code.value,
            "failure_reason": failure_reason,
        }
        terminal = StudyCallTerminalV1(
            study_id=self.study_id,
            arm=request.arm,
            request_sha256=request.sha256,
            reservation_sha256=reservation.reservation_sha256,
            grant_sha256=self.grant.sha256,
            manifest_sha256=self.manifest.sha256,
            repository_root_identity_sha256=self.store.repository.root_identity_sha256,
            audit_domain=self.grant.audit_domain,
            invocation_owner=reservation.invocation_owner,
            prior_journal_sha256=terminals[-1].terminal_sha256 if terminals else None,
            terminal_sequence=sequence,
            response_ref=response_ref,
            parsed_ref=parsed_ref,
            attempt=attempt,
            usage=usage,
            receipt=receipt,
            failure_code=code,
            failure_reason=failure_reason,
            terminal_sha256=_sha256(canonical_json_bytes_v5(terminal_payload)),
        )
        terminal_ref = self.store.put(kind="terminals", key=terminal.terminal_sha256, content=terminal.canonical_bytes())
        return AuthenticatedStudyTerminalV1(terminal_ref, terminal)

    def settle(
        self,
        reservation: StudyReservationV1,
        *,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
        parsed_ref: ArtifactRefV5 | None,
        failure_code: RoleFailureCode | None,
        failure_reason: str | None = None,
    ) -> AuthenticatedStudyTerminalV1:
        if type(reservation) is not StudyReservationV1 or type(completion) is not CompletionResultV5:
            raise StudyContractError("study settlement inputs are invalid")
        with self._lock, self._transition():
            existing = self._find_terminal_by_reservation(reservation.reservation_sha256)
            if existing is not None:
                return self.verify_terminal(existing.reference)
            stored = next(
                (item for _ref_value, item in self._reservations() if item.reservation_sha256 == reservation.reservation_sha256),
                None,
            )
            if stored is None or stored != reservation:
                raise StudyAuthorityError("study reservation is not authenticated by this ledger")
            request = self._stored_request(reservation.request_sha256)
            self._validate_reservation_request_binding(reservation, request)
            self._validate_request(request)
            if not any(claim.request_sha256 == request.sha256 for claim in self._dispatch_claims()):
                raise StudyAuthorityError("study settlement has no authenticated dispatch claim")
            if reservation.invocation_owner not in self._owner_capabilities:
                raise StudyAuthorityError("study settlement owner capability is unavailable")
            if type(response_ref) is not ArtifactRefV5:
                raise StudyContractError("study response reference is invalid")
            failure_marker = response_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/")
            if response_ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{request.sha256}.bin" and not failure_marker:
                raise StudyAuthorityError("study response reference is outside the raw-response namespace")
            raw_response = self.store.read(response_ref)
            if response_ref.sha256 != _sha256(raw_response):
                raise StudyAuthorityError("study response reference differs from its bytes")
            response_record = self._response_record(request)
            metadata_record = self._response_metadata(request)
            if metadata_record is None:
                raise StudyAuthorityError("study completion has no persisted usage metadata")
            _metadata_ref, metadata_value = metadata_record
            if _completion_metadata_primitive(completion) != metadata_value["completion_metadata"]:
                raise StudyAuthorityError("study completion differs from its persisted usage metadata")
            if failure_marker:
                marker = self._raw_failure(request)
                if marker is None or marker[0] != response_ref:
                    raise StudyAuthorityError("study unavailable raw response marker is not authenticated")
                completion_bytes = completion.response_text.encode("utf-8")
                if marker[1]["raw_sha256"] != _sha256(completion_bytes) or marker[1]["raw_length"] != len(completion_bytes):
                    raise StudyAuthorityError("study unavailable raw response marker differs from the completion")
                if failure_code is not RoleFailureCode.ACCOUNTING:
                    failure_code = RoleFailureCode.ACCOUNTING
                failure_reason = str(marker[1]["reason"])
                raw_response = b""
            else:
                if raw_response != completion.response_text.encode("utf-8"):
                    raise StudyAuthorityError("study response bytes differ from the completion receipt")
                if response_record is None or response_record[1] != completion:
                    raise StudyAuthorityError("study completion differs from its persisted usage receipt")
            if (not completion.accepted or not completion.response_received) and parsed_ref is not None:
                raise StudyAuthorityError("transport failure cannot reference a parsed study response")
            if parsed_ref is not None:
                if parsed_ref.relative_path != f"adapter-blobs/study-v1-parsed/{request.sha256}.bin":
                    raise StudyAuthorityError("study parsed response reference is not deterministic")
                parsed_raw = self.store.read(parsed_ref)
                if parsed_ref.sha256 != _sha256(parsed_raw):
                    raise StudyAuthorityError("study parsed response reference differs from its bytes")
                parsed = _decode_canonical_json_object(parsed_raw, "study parsed response")
                _validate_parsed_study_response(
                    parsed,
                    self._configuration_ids(request),
                    expected_binding=self._request_binding(request),
                )
                try:
                    expected_parsed = self._parse_authenticated_response(request, raw_response).to_primitive()
                except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise StudyAuthorityError("study parsed response is not supported by its raw response") from exc
                if parsed != expected_parsed:
                    raise StudyAuthorityError("study parsed response differs from its raw response")
            return self._settle_locked(
                reservation,
                request,
                completion=completion,
                response_ref=response_ref,
                parsed_ref=parsed_ref,
                failure_code=failure_code,
                failure_reason=failure_reason,
            )

    def _find_terminal_by_reservation(self, reservation_sha256: str) -> AuthenticatedStudyTerminalV1 | None:
        for terminal in self._terminals():
            if terminal.terminal.reservation_sha256 == reservation_sha256:
                return terminal
        return None

    def _parse_and_persist(
        self,
        request: StudyCallRequestV1,
        completion: CompletionResultV5,
        response_ref: ArtifactRefV5,
    ) -> tuple[ArtifactRefV5 | None, RoleFailureCode | None, str | None]:
        if response_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
            marker = self._raw_failure(request)
            if marker is None:
                raise StudyAuthorityError("study unavailable raw response marker is missing")
            return None, RoleFailureCode.ACCOUNTING, str(marker[1]["reason"])
        try:
            raw = completion.response_text.encode("utf-8")
            parsed = self._parse_authenticated_response(request, raw)
            return self.store.put(kind="parsed", key=request.sha256, content=parsed.canonical_bytes()), None, None
        except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            code, reason = self._classify_parse_failure(exc)
            return None, code, reason

    def recover(self, request: StudyCallRequestV1) -> AuthenticatedStudyTerminalV1 | None:
        self._validate_request(request)
        existing = self._find_terminal(request)
        if existing is not None:
            return self.verify_terminal(existing.reference)
        publication = self._find_reservation_publication(request)
        if publication is not None:
            # The envelope is the sole authority for repairing a partial
            # reservation publication.  A caller-supplied request without an
            # envelope is still a clean no-write prefix.
            with self._lock, self._transition():
                # Reauthenticate while holding the transition lock.  A
                # long-lived reader must reject downstream or conflicting
                # bytes appended after construction before it mutates state.
                self._authenticate_graph_orphans(self._reservations(), self._terminals())
                request_refs = tuple(
                    ref
                    for ref in self._refs("requests")
                    if ref.relative_path == f"adapter-blobs/study-v1-requests/{request.sha256}.bin"
                )
                if len(request_refs) > 1:
                    raise StudyAuthorityError("study request has duplicate authenticated records")
                if request_refs:
                    if self._stored_request(request.sha256).canonical_bytes() != publication.request.canonical_bytes():
                        raise StudyAuthorityError("study request differs from its reservation publication")
                else:
                    self.store.put(kind="requests", key=request.sha256, content=publication.request.canonical_bytes())
                reservations = self._reservations()
                existing_reservation = next(
                    (
                        item
                        for _ref_value, item in reservations
                        if item.reservation_sha256 == publication.reservation_sha256
                    ),
                    None,
                )
                if (
                    existing_reservation is not None
                    and existing_reservation.canonical_bytes() != publication.reservation.canonical_bytes()
                ):
                    raise StudyAuthorityError("study reservation differs from its reservation publication")
                if existing_reservation is None:
                    self.store.put(
                        kind="reservations",
                        key=publication.reservation.reservation_sha256,
                        content=publication.reservation.canonical_bytes(),
                    )
                # Reauthenticate after the repair so no downstream evidence
                # can be attached to a partial prefix.
                self._authenticate_graph_orphans(self._reservations(), self._terminals())
        reservation = self._find_reservation(request)
        if reservation is None:
            return None
        self._validate_reservation_request_binding(reservation, request)
        if self._find_admission_rejection(request) is not None:
            raise StudyAdmissionError("study request has a deterministic pre-dispatch rejection")
        has_dispatch_claim = any(claim.request_sha256 == request.sha256 for claim in self._dispatch_claims())
        if not has_dispatch_claim:
            if self._has_provider_accounting_prefix(request):
                raise StudyAuthorityError("study recovery has provider accounting without an authenticated dispatch claim")
            raise StudyPendingAccounting("study reservation has no authenticated provider usage")
        response = self._response_record(request)
        metadata_record = self._response_metadata(request)
        raw_ref = self._raw_response_reference(request)
        if metadata_record is None or raw_ref is None:
            raise StudyPendingAccounting("study reservation has no authenticated provider usage")
        _metadata_ref, metadata_value = metadata_record
        if raw_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
            marker = self._raw_failure(request)
            if marker is None:
                raise StudyPendingAccounting("study response persistence marker is unavailable")
            completion = _completion_from_metadata(metadata_value["completion_metadata"], "")
            response_ref = raw_ref
            raw = b""
        else:
            if response is None:
                raise StudyPendingAccounting("study response bytes are not durably persisted")
            response_ref, completion = response
            raw = self.store.read(raw_ref)
            if raw_ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("study raw response reference differs from its bytes")
            if raw != completion.response_text.encode("utf-8"):
                raise StudyAuthorityError("study raw response differs from the usage record")
        with self._lock, self._transition():
            existing = self._find_terminal(request)
            if existing is not None:
                return self.verify_terminal(existing.reference)
            if not completion.accepted or not completion.response_received:
                parsed_ref, parse_code, parse_reason = None, RoleFailureCode.TRANSPORT, "provider did not return an accepted completion"
            else:
                parsed_ref, parse_code, parse_reason = self._parse_and_persist(request, completion, raw_ref)
            code = None if parsed_ref is not None else (
                RoleFailureCode.TRANSPORT
                if (not completion.accepted or not completion.response_received)
                else parse_code or RoleFailureCode.RESPONSE_SCHEMA
            )
            return self._settle_locked(
                reservation,
                request,
                completion=completion,
                response_ref=raw_ref,
                parsed_ref=parsed_ref,
                failure_code=code,
                failure_reason=parse_reason,
            )

    def verify_terminal(self, reference: ArtifactRefV5) -> AuthenticatedStudyTerminalV1:
        if type(reference) is not ArtifactRefV5:
            raise StudyContractError("study terminal reference is invalid")
        raw = self.store.read(reference)
        terminal = _decode_canonical(StudyCallTerminalV1, raw, "study terminal")
        authenticated = AuthenticatedStudyTerminalV1(reference, terminal)
        if terminal.study_id != self.study_id or terminal.grant_sha256 != self.grant.sha256 or terminal.manifest_sha256 != self.manifest.sha256:
            raise StudyAuthorityError("study terminal authority identity differs from this ledger")
        if terminal.repository_root_identity_sha256 != self.store.repository.root_identity_sha256 or terminal.audit_domain != self.grant.audit_domain:
            raise StudyAuthorityError("study terminal root or audit identity differs")
        reservations = self._reservations()
        reservation = next((item for _ref_value, item in reservations if item.reservation_sha256 == terminal.reservation_sha256), None)
        if reservation is None or reservation.request_sha256 != terminal.request_sha256 or reservation.arm != terminal.arm:
            raise StudyAuthorityError("study terminal has no matching reservation")
        if reservation.invocation_owner != terminal.invocation_owner:
            raise StudyAuthorityError("study terminal invocation owner differs from its reservation")
        request = self._stored_request(terminal.request_sha256)
        self._validate_request(request)
        if terminal.arm != request.arm:
            raise StudyAuthorityError("study terminal arm differs from its request")
        failure_marker = terminal.response_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/")
        if terminal.response_ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{request.sha256}.bin" and not failure_marker:
            raise StudyAuthorityError("study terminal response is outside the raw-response namespace")
        raw_response = self.store.read(terminal.response_ref)
        if terminal.response_ref.sha256 != _sha256(raw_response):
            raise StudyAuthorityError("study terminal response reference differs from its bytes")
        response_record = self._response_record(request)
        metadata_record = self._response_metadata(request)
        if metadata_record is None:
            raise StudyAuthorityError("study terminal response has no usage metadata")
        _metadata_ref, metadata_value = metadata_record
        if failure_marker:
            marker = self._raw_failure(request)
            if marker is None or marker[0] != terminal.response_ref or terminal.failure_code is not RoleFailureCode.ACCOUNTING:
                raise StudyAuthorityError("study terminal unavailable response marker is not authenticated")
            if marker[1]["raw_sha256"] != metadata_value["response_sha256"] or marker[1]["raw_length"] != metadata_value["response_length"]:
                raise StudyAuthorityError("study terminal unavailable response marker differs from usage metadata")
            response_for_accounting = _completion_from_metadata(metadata_value["completion_metadata"], "")
        else:
            if response_record is None or raw_response != response_record[1].response_text.encode("utf-8"):
                raise StudyAuthorityError("study terminal response is not bound to its usage receipt")
            response_for_accounting = response_record[1]
        parsed_ref = terminal.parsed_ref
        if failure_marker and parsed_ref is not None:
            raise StudyAuthorityError("non-importable response cannot have a parsed artifact")
        if (not response_for_accounting.accepted or not response_for_accounting.response_received) and parsed_ref is not None:
            raise StudyAuthorityError("transport failure cannot reference a parsed study response")
        if parsed_ref is not None:
            if parsed_ref.relative_path != f"adapter-blobs/study-v1-parsed/{request.sha256}.bin":
                raise StudyAuthorityError("study terminal parsed response path is not deterministic")
            parsed_raw = self.store.read(parsed_ref)
            if parsed_ref.sha256 != _sha256(parsed_raw):
                raise StudyAuthorityError("study terminal parsed reference differs from its bytes")
            parsed_value = _decode_canonical_json_object(parsed_raw, "study parsed response")
            _validate_parsed_study_response(
                parsed_value,
                self._configuration_ids(request),
                expected_binding=self._request_binding(request),
            )
            try:
                expected_parsed = self._parse_authenticated_response(request, raw_response).to_primitive()
            except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise StudyAuthorityError("study terminal has a parsed record for an invalid response") from exc
            if parsed_value != expected_parsed:
                raise StudyAuthorityError("study terminal parsed response differs from its raw response")
        terminals = self._terminals()
        try:
            index = next(index for index, item in enumerate(terminals) if item.terminal.terminal_sha256 == terminal.terminal_sha256)
        except StopIteration as exc:
            raise StudyAuthorityError("terminal is not referenced by the complete journal") from exc
        if terminals[index].reference != reference or terminal.terminal_sequence != index + 1:
            raise StudyAuthorityError("terminal sequence is not authenticated by the journal")
        prior = terminals[:index]
        expected_code, expected_reason = self._parser_classification(
            request,
            response_for_accounting,
            terminal.response_ref,
            parsed_ref,
        )
        usage, attempt, receipt, code, reason = self._derive_settlement_components(
            reservation,
            request,
            response_for_accounting,
            terminal.response_ref,
            parsed_ref,
            expected_code,
            prior,
            failure_reason=expected_reason,
        )
        expected_terminal_payload = {
            "study_id": self.study_id,
            "arm": request.arm,
            "request_sha256": request.sha256,
            "reservation_sha256": reservation.reservation_sha256,
            "grant_sha256": self.grant.sha256,
            "manifest_sha256": self.manifest.sha256,
            "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
            "audit_domain": self.grant.audit_domain,
            "invocation_owner": reservation.invocation_owner,
            "prior_journal_sha256": prior[-1].terminal_sha256 if prior else None,
            "terminal_sequence": index + 1,
            "response_ref": _ref(terminal.response_ref),
            "parsed_ref": None if parsed_ref is None else _ref(parsed_ref),
            "attempt": _attempt_primitive(attempt),
            "usage": _usage_primitive(usage),
            "receipt": _receipt_primitive(receipt),
            "failure_code": None if code is None else code.value,
            "failure_reason": reason,
        }
        expected_terminal = StudyCallTerminalV1(
            study_id=self.study_id,
            arm=request.arm,
            request_sha256=request.sha256,
            reservation_sha256=reservation.reservation_sha256,
            grant_sha256=self.grant.sha256,
            manifest_sha256=self.manifest.sha256,
            repository_root_identity_sha256=self.store.repository.root_identity_sha256,
            audit_domain=self.grant.audit_domain,
            invocation_owner=reservation.invocation_owner,
            prior_journal_sha256=prior[-1].terminal_sha256 if prior else None,
            terminal_sequence=index + 1,
            response_ref=terminal.response_ref,
            parsed_ref=parsed_ref,
            attempt=attempt,
            usage=usage,
            receipt=receipt,
            failure_code=code,
            failure_reason=reason,
            terminal_sha256=_sha256(canonical_json_bytes_v5(expected_terminal_payload)),
        )
        if terminal != expected_terminal:
            raise StudyAuthorityError("study terminal graph does not rederive from its authenticated response and usage")
        self._authenticate_graph_orphans(reservations, terminals)
        for index, item in enumerate(terminals):
            if item.terminal.terminal_sha256 != terminal.terminal_sha256:
                self._rederive_terminal_record(item, index, reservations, terminals)
        return authenticated

    def _rederive_terminal_record(
        self,
        authenticated: AuthenticatedStudyTerminalV1,
        index: int,
        reservations: tuple[tuple[ArtifactRefV5, StudyReservationV1], ...],
        terminals: tuple[AuthenticatedStudyTerminalV1, ...],
    ) -> None:
        """Recompute an earlier journal node before accepting a later node."""

        terminal = authenticated.terminal
        reservation = next(
            (item for _ref_value, item in reservations if item.reservation_sha256 == terminal.reservation_sha256),
            None,
        )
        if reservation is None:
            raise StudyAuthorityError("earlier terminal has no matching reservation")
        if (
            terminal.study_id != self.study_id
            or terminal.grant_sha256 != self.grant.sha256
            or terminal.manifest_sha256 != self.manifest.sha256
            or terminal.repository_root_identity_sha256 != self.store.repository.root_identity_sha256
            or terminal.audit_domain != self.grant.audit_domain
        ):
            raise StudyAuthorityError("earlier terminal authority identity differs")
        request = self._stored_request(terminal.request_sha256)
        self._validate_request(request)
        if terminal.invocation_owner != reservation.invocation_owner or terminal.arm != request.arm:
            raise StudyAuthorityError("earlier terminal identity differs from its reservation")
        failure_marker = terminal.response_ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/")
        if terminal.response_ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{request.sha256}.bin" and not failure_marker:
            raise StudyAuthorityError("earlier terminal response path is invalid")
        response_record = self._response_record(request)
        raw = self.store.read(terminal.response_ref)
        if terminal.response_ref.sha256 != _sha256(raw):
            raise StudyAuthorityError("earlier terminal response reference differs from its bytes")
        metadata_record = self._response_metadata(request)
        if metadata_record is None:
            raise StudyAuthorityError("earlier terminal response has no usage metadata")
        _metadata_ref, metadata_value = metadata_record
        if failure_marker:
            marker = self._raw_failure(request)
            if marker is None or marker[0] != terminal.response_ref or terminal.failure_code is not RoleFailureCode.ACCOUNTING:
                raise StudyAuthorityError("earlier terminal unavailable response marker is not authenticated")
            if marker[1]["raw_sha256"] != metadata_value["response_sha256"] or marker[1]["raw_length"] != metadata_value["response_length"]:
                raise StudyAuthorityError("earlier terminal unavailable response marker differs from usage metadata")
            response_for_accounting = _completion_from_metadata(metadata_value["completion_metadata"], "")
        else:
            if response_record is None or raw != response_record[1].response_text.encode("utf-8"):
                raise StudyAuthorityError("earlier terminal response is not bound to usage")
            response_for_accounting = response_record[1]
        parsed_ref = terminal.parsed_ref
        if failure_marker and parsed_ref is not None:
            raise StudyAuthorityError("earlier non-importable response cannot have a parsed artifact")
        if (not response_for_accounting.accepted or not response_for_accounting.response_received) and parsed_ref is not None:
            raise StudyAuthorityError("transport failure cannot reference a parsed study response")
        if parsed_ref is not None:
            if parsed_ref.relative_path != f"adapter-blobs/study-v1-parsed/{request.sha256}.bin":
                raise StudyAuthorityError("earlier terminal parsed path is invalid")
            parsed_raw = self.store.read(parsed_ref)
            if parsed_ref.sha256 != _sha256(parsed_raw):
                raise StudyAuthorityError("earlier terminal parsed reference differs from its bytes")
            parsed = _decode_canonical_json_object(parsed_raw, "study parsed response")
            _validate_parsed_study_response(
                parsed,
                self._configuration_ids(request),
                expected_binding=self._request_binding(request),
            )
            try:
                expected_parsed = self._parse_authenticated_response(request, raw).to_primitive()
            except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise StudyAuthorityError("earlier terminal has a parsed record for an invalid response") from exc
            if parsed != expected_parsed:
                raise StudyAuthorityError("earlier terminal parsed response differs from its raw response")
        expected_code, expected_reason = self._parser_classification(
            request,
            response_for_accounting,
            terminal.response_ref,
            parsed_ref,
        )
        usage, attempt, receipt, code, reason = self._derive_settlement_components(
            reservation,
            request,
            response_for_accounting,
            terminal.response_ref,
            parsed_ref,
            expected_code,
            terminals[:index],
            failure_reason=expected_reason,
        )
        payload = {
            "study_id": self.study_id,
            "arm": request.arm,
            "request_sha256": request.sha256,
            "reservation_sha256": reservation.reservation_sha256,
            "grant_sha256": self.grant.sha256,
            "manifest_sha256": self.manifest.sha256,
            "repository_root_identity_sha256": self.store.repository.root_identity_sha256,
            "audit_domain": self.grant.audit_domain,
            "invocation_owner": reservation.invocation_owner,
            "prior_journal_sha256": terminals[index - 1].terminal_sha256 if index else None,
            "terminal_sequence": index + 1,
            "response_ref": _ref(terminal.response_ref),
            "parsed_ref": None if parsed_ref is None else _ref(parsed_ref),
            "attempt": _attempt_primitive(attempt),
            "usage": _usage_primitive(usage),
            "receipt": _receipt_primitive(receipt),
            "failure_code": None if code is None else code.value,
            "failure_reason": reason,
        }
        expected = StudyCallTerminalV1(
            study_id=self.study_id,
            arm=request.arm,
            request_sha256=request.sha256,
            reservation_sha256=reservation.reservation_sha256,
            grant_sha256=self.grant.sha256,
            manifest_sha256=self.manifest.sha256,
            repository_root_identity_sha256=self.store.repository.root_identity_sha256,
            audit_domain=self.grant.audit_domain,
            invocation_owner=reservation.invocation_owner,
            prior_journal_sha256=terminals[index - 1].terminal_sha256 if index else None,
            terminal_sequence=index + 1,
            response_ref=terminal.response_ref,
            parsed_ref=parsed_ref,
            attempt=attempt,
            usage=usage,
            receipt=receipt,
            failure_code=code,
            failure_reason=reason,
            terminal_sha256=_sha256(canonical_json_bytes_v5(payload)),
        )
        if terminal != expected:
            raise StudyAuthorityError("earlier terminal graph does not rederive from its authenticated response")

    def _authenticate_graph_orphans(
        self,
        reservations: tuple[tuple[ArtifactRefV5, StudyReservationV1], ...],
        terminals: tuple[AuthenticatedStudyTerminalV1, ...],
    ) -> None:
        publications = self._reservation_publications()
        publication_by_request = {item.request_sha256: item for _ref_value, item in publications}
        reservation_requests = {item.request_sha256 for _ref_value, item in reservations}
        reservation_by_request = {item.request_sha256: item for _ref_value, item in reservations}
        reservation_by_sha = {item.reservation_sha256: item for _ref_value, item in reservations}
        pending_reservations = [
            publication.reservation
            for _ref_value, publication in publications
            if publication.reservation_sha256 not in reservation_by_sha
        ]
        combined_reservations = [item for _ref_value, item in reservations] + pending_reservations
        ordered_sequences = tuple(sorted(item.sequence for item in combined_reservations))
        if ordered_sequences != tuple(range(1, len(ordered_sequences) + 1)):
            raise StudyAuthorityError("study reservation sequence has a gap or duplicate")
        if len({item.request_sha256 for item in combined_reservations}) != len(combined_reservations):
            raise StudyAuthorityError("study reservations contain a duplicate request")
        if len({item.arm for item in combined_reservations}) != len(combined_reservations):
            raise StudyAuthorityError("study reservations contain a duplicate arm slot")
        if len({item.slot_id for item in combined_reservations}) > 2:
            raise StudyAuthorityError("study reservations exceed the two shared arm slots")
        request_refs = self._refs("requests")
        request_keys: set[str] = set()
        for ref in request_refs:
            if not ref.relative_path.startswith("adapter-blobs/study-v1-requests/"):
                raise StudyAuthorityError("study request namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if key.startswith(_PENDING_REQUEST_PREFIX):
                continue
            request = self._stored_request(key)
            request_keys.add(request.sha256)
        for _ref_value, publication in publications:
            stored_reservation = reservation_by_sha.get(publication.reservation_sha256)
            if (
                stored_reservation is not None
                and stored_reservation.canonical_bytes() != publication.reservation.canonical_bytes()
            ):
                raise StudyAuthorityError("study reservation differs from its authenticated publication")
            if publication.request_sha256 in request_keys:
                if self._stored_request(publication.request_sha256).canonical_bytes() != publication.request.canonical_bytes():
                    raise StudyAuthorityError("study request differs from its authenticated publication")
        publication_requests = set(publication_by_request)
        if (request_keys ^ reservation_requests) - publication_requests:
            raise StudyAuthorityError("study request records do not exactly match reservations")
        for _ref_value, reservation in reservations:
            request = self._stored_request(reservation.request_sha256)
            self._validate_reservation_request_binding(reservation, request)
            self._validate_request(request, require_current=False)
        terminal_requests = {item.request_sha256 for item in terminals}
        terminal_by_request = {item.request_sha256: item for item in terminals}
        dispatch_claims = self._dispatch_claims()
        dispatch_request_keys = {claim.request_sha256 for claim in dispatch_claims}
        claim_by_request = {claim.request_sha256: claim for claim in dispatch_claims}
        diagnostics = self._provider_diagnostic_records()
        observations = self._observed_response_records()
        for request_sha256, (_diagnostic_ref, diagnostic) in diagnostics.items():
            reservation = reservation_by_request.get(request_sha256)
            claim = claim_by_request.get(request_sha256)
            if reservation is None or claim is None:
                raise StudyAuthorityError("study provider diagnostic has no authenticated reservation and dispatch claim")
            if (
                diagnostic["reservation_sha256"] != reservation.reservation_sha256
                or diagnostic["claim_sha256"] != claim.claim_sha256
                or diagnostic["grant_sha256"] != self.grant.sha256
                or diagnostic["manifest_sha256"] != self.manifest.sha256
            ):
                raise StudyAuthorityError("study provider diagnostic authority binding differs")
        for request_sha256, (_observation_ref, observation, _raw_ref, _raw) in observations.items():
            reservation = reservation_by_request.get(request_sha256)
            claim = claim_by_request.get(request_sha256)
            if reservation is None or claim is None:
                raise StudyAuthorityError("study response observation has no authenticated reservation and dispatch claim")
            if (
                observation["reservation_sha256"] != reservation.reservation_sha256
                or observation["claim_sha256"] != claim.claim_sha256
                or observation["grant_sha256"] != self.grant.sha256
                or observation["manifest_sha256"] != self.manifest.sha256
            ):
                raise StudyAuthorityError("study response observation authority binding differs")
            request = self._stored_request(request_sha256)
            self._validate_reservation_request_binding(reservation, request)
            if claim.reservation_sha256 != reservation.reservation_sha256:
                raise StudyAuthorityError("study response observation dispatch claim differs from its reservation")
        admission_rejections = self._admission_rejections()
        rejection_by_request = {item.request_sha256: item for _ref_value, item in admission_rejections}
        reconciliation_events = self._reconciliations()
        incomplete_publication_requests = {
            publication.request_sha256
            for _ref_value, publication in publications
            if publication.request_sha256 not in request_keys or publication.reservation_sha256 not in reservation_by_sha
        }
        if incomplete_publication_requests and (
            incomplete_publication_requests & dispatch_request_keys
            or incomplete_publication_requests & terminal_requests
            or incomplete_publication_requests & rejection_by_request.keys()
            or any(event.request_sha256 in incomplete_publication_requests for event in reconciliation_events)
            or any(
                ref.relative_path.rsplit("/", 1)[-1][:-4] in incomplete_publication_requests
                for kind in ("responses", "raw-responses", "raw-response-failures", "observed-raw-responses", "response-observations", "provider-diagnostics", "parsed")
                for ref in self._refs(kind)
            )
        ):
            raise StudyAuthorityError("incomplete reservation publication has downstream accounting evidence")
        response_keys: set[str] = set()
        response_metadata: dict[str, dict[str, object]] = {}
        for ref in self._refs("responses"):
            if not ref.relative_path.startswith("adapter-blobs/study-v1-responses/"):
                raise StudyAuthorityError("study response namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"adapter-blobs/study-v1-responses/{key}.bin":
                raise StudyAuthorityError("study response path is not deterministic")
            if key not in reservation_requests:
                raise StudyAuthorityError("study response record has no reservation")
            if key not in dispatch_request_keys:
                raise StudyAuthorityError("study response record has no authenticated dispatch claim")
            raw = self.store.read(ref)
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("study response reference differs from its bytes")
            response_request = self._stored_request(key)
            metadata_record = self._response_metadata(response_request)
            if metadata_record is None:
                raise StudyAuthorityError("study response metadata is not authenticated")
            response_keys.add(key)
            response_metadata[key] = metadata_record[1]
        raw_keys: set[str] = set()
        raw_values: dict[str, bytes] = {}
        raw_refs: dict[str, ArtifactRefV5] = {}
        for ref in self._refs("raw-responses"):
            if not ref.relative_path.startswith("adapter-blobs/study-v1-raw-responses/"):
                raise StudyAuthorityError("study raw response namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"adapter-blobs/study-v1-raw-responses/{key}.bin":
                raise StudyAuthorityError("study raw response path is not deterministic")
            if key not in reservation_requests:
                raise StudyAuthorityError("study raw response has no reservation")
            if key not in dispatch_request_keys:
                raise StudyAuthorityError("study raw response has no authenticated dispatch claim")
            raw = self.store.read(ref)
            if ref.sha256 != _sha256(raw):
                raise StudyAuthorityError("study raw response reference differs from its bytes")
            if key not in response_keys:
                raise StudyAuthorityError("study raw response has no usage receipt")
            if _sha256(raw) != response_metadata[key]["response_sha256"] or len(raw) != response_metadata[key]["response_length"]:
                raise StudyAuthorityError("study raw response differs from its usage metadata")
            raw_keys.add(key)
            raw_values[key] = raw
            raw_refs[key] = ref
        failure_refs: dict[str, ArtifactRefV5] = {}
        for ref in self._refs("raw-response-failures"):
            if not ref.relative_path.startswith("adapter-blobs/study-v1-raw-response-failures/"):
                raise StudyAuthorityError("study raw response failure namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"adapter-blobs/study-v1-raw-response-failures/{key}.bin":
                raise StudyAuthorityError("study raw response failure path is not deterministic")
            if key not in response_keys:
                raise StudyAuthorityError("study raw response failure has no usage receipt")
            if key not in dispatch_request_keys:
                raise StudyAuthorityError("study raw response failure has no authenticated dispatch claim")
            marker = self._raw_failure(self._stored_request(key))
            if marker is None or marker[0] != ref:
                raise StudyAuthorityError("study raw response failure marker is not authenticated")
            if marker[1]["raw_sha256"] != response_metadata[key]["response_sha256"] or marker[1]["raw_length"] != response_metadata[key]["response_length"]:
                raise StudyAuthorityError("study raw response failure differs from its usage metadata")
            raw_keys.add(key)
            failure_refs[key] = ref
        if set(raw_refs) & set(failure_refs):
            raise StudyAuthorityError("study response has both raw and unavailable persistence records")
        for key, (_observation_ref, observation, _observed_raw_ref, observed_raw) in observations.items():
            if key in response_metadata:
                accounted = _completion_from_metadata(response_metadata[key]["completion_metadata"], "")
                if accounted.provider_request_id != observation["provider_request_id"]:
                    raise StudyAuthorityError("accounted completion differs from the preserved provider response observation")
            if observed_raw is None and key in response_metadata:
                raise StudyAuthorityError("accounted completion follows an incomplete preserved response observation")
            if observed_raw is not None and key in raw_values and raw_values[key] != observed_raw:
                raise StudyAuthorityError("accounted completion differs from the preserved provider response observation")
            if key in failure_refs:
                raise StudyAuthorityError("observed provider response conflicts with unavailable raw response marker")
        failure_keys = {
            ref.relative_path.rsplit("/", 1)[-1][:-4]
            for ref in self._refs("raw-response-failures")
        }
        for key in terminal_requests:
            if key not in response_keys or key not in raw_keys:
                raise StudyAuthorityError("study terminal lacks its complete response accounting pair")
        parsed_refs_by_request: dict[str, ArtifactRefV5] = {}
        for ref in self._refs("parsed"):
            if not ref.relative_path.startswith("adapter-blobs/study-v1-parsed/"):
                raise StudyAuthorityError("study parsed namespace contains an orphan record")
            key = ref.relative_path.rsplit("/", 1)[-1][:-4]
            if ref.relative_path != f"adapter-blobs/study-v1-parsed/{key}.bin":
                raise StudyAuthorityError("study parsed response path is not deterministic")
            if key not in terminal_requests and key not in reservation_requests:
                raise StudyAuthorityError("study parsed response has no reservation")
            if key not in dispatch_request_keys:
                raise StudyAuthorityError("study parsed response has no authenticated dispatch claim")
            parsed_request = self._stored_request(key)
            parsed_raw = self.store.read(ref)
            if ref.sha256 != _sha256(parsed_raw):
                raise StudyAuthorityError("study parsed response reference differs from its bytes")
            if key in parsed_refs_by_request:
                raise StudyAuthorityError("study parsed response is duplicated")
            parsed = _decode_canonical_json_object(parsed_raw, "study parsed response")
            _validate_parsed_study_response(
                parsed,
                self._configuration_ids(parsed_request),
                expected_binding=self._request_binding(parsed_request),
            )
            if key not in response_keys or key not in raw_keys:
                raise StudyAuthorityError("study parsed response lacks its complete response accounting pair")
            if key in failure_keys:
                raise StudyAuthorityError("non-importable response cannot have a parsed artifact")
            try:
                expected_parsed = self._parse_authenticated_response(parsed_request, raw_values[key]).to_primitive()
            except (StudyContractError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise StudyAuthorityError("study parsed response is not supported by its raw response") from exc
            if parsed != expected_parsed:
                raise StudyAuthorityError("study parsed response differs from its raw response")
            parsed_refs_by_request[key] = ref
        for key, terminal in terminal_by_request.items():
            if terminal.terminal.parsed_ref is None:
                if key in parsed_refs_by_request:
                    raise StudyAuthorityError("study parsed response is not referenced by its terminal")
            elif parsed_refs_by_request.get(key) != terminal.terminal.parsed_ref:
                raise StudyAuthorityError("study terminal parsed reference is not authenticated")
        for _ref_value, rejection in admission_rejections:
            reservation = reservation_by_sha.get(rejection.reservation_sha256)
            if (
                reservation is None
                or reservation.request_sha256 != rejection.request_sha256
                or reservation.arm != rejection.arm
                or reservation.invocation_owner != rejection.invocation_owner
                or rejection.request_sha256 in dispatch_request_keys
                or rejection.request_sha256 in response_keys
                or rejection.request_sha256 in raw_keys
                or rejection.request_sha256 in parsed_refs_by_request
                or rejection.request_sha256 in terminal_requests
            ):
                raise StudyAuthorityError("admission rejection is not a pre-dispatch reservation event")
        for claim in dispatch_claims:
            if claim.request_sha256 not in reservation_requests:
                raise StudyAuthorityError("study dispatch claim has no reservation")
            request = self._stored_request(claim.request_sha256)
            self._validate_request(request)
            reservation = reservation_by_sha.get(claim.reservation_sha256)
            if (
                reservation is None
                or reservation.request_sha256 != claim.request_sha256
                or reservation.invocation_owner != claim.invocation_owner
                or claim.model != request.model
                or claim.messages_sha256 != request.projected_wire_messages_sha256
                or claim.response_schema_sha256 != request.projected_wire_schema_sha256
                or claim.max_output_tokens != request.max_output_tokens
                or claim.transport_settings_sha256 != request.transport_settings_sha256
            ):
                raise StudyAuthorityError("study dispatch claim differs from its request reservation")
        for terminal in terminals:
            if terminal.request_sha256 not in dispatch_request_keys:
                raise StudyAuthorityError("study terminal has no authenticated dispatch claim")
            reservation = reservation_by_request.get(terminal.request_sha256)
            if (
                reservation is None
                or reservation.reservation_sha256 != terminal.terminal.reservation_sha256
                or reservation.arm != terminal.arm
                or reservation.invocation_owner != terminal.terminal.invocation_owner
            ):
                raise StudyAuthorityError("study terminal does not bind its reservation")
            expected_response_ref = raw_refs.get(terminal.request_sha256) or failure_refs.get(terminal.request_sha256)
            if expected_response_ref is None or terminal.terminal.response_ref != expected_response_ref:
                raise StudyAuthorityError("study terminal response reference is not authenticated")
        for event in reconciliation_events:
            if event.request_sha256 not in reservation_requests:
                raise StudyAuthorityError("reconciliation event has no reservation")
            if event.request_sha256 in rejection_by_request:
                raise StudyAuthorityError("reconciliation event follows a pre-dispatch rejection")
            if event.request_sha256 not in dispatch_request_keys:
                raise StudyAuthorityError("reconciliation event has no authenticated dispatch claim")
            reservation = reservation_by_request[event.request_sha256]
            if event.reservation_sha256 != reservation.reservation_sha256 or event.invocation_owner != reservation.invocation_owner:
                raise StudyAuthorityError("reconciliation event differs from its reservation")
            event_request = self._stored_request(event.request_sha256)
            metadata_record = self._response_metadata(event_request)
            if metadata_record is None:
                raise StudyAuthorityError("reconciliation event has no usage metadata")
            response = self._response_record(event_request)
            completion = response[1] if response is not None else _completion_from_metadata(metadata_record[1]["completion_metadata"], "")
            expected_fingerprint = _completion_fingerprint(
                completion,
                response_sha256=metadata_record[1]["response_sha256"],
                response_length=metadata_record[1]["response_length"],
            )
            if event.completion_sha256 != expected_fingerprint:
                raise StudyAuthorityError("reconciliation event is not bound to its usage receipt")
        for index, item in enumerate(terminals):
            self._rederive_terminal_record(item, index, reservations, terminals)


def _decode_canonical_json_object(raw: bytes, label: str) -> object:
    if type(raw) is not bytes:
        raise StudyContractError(f"{label} bytes are invalid")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_pairs(pairs, label), parse_constant=lambda _: (_ for _ in ()).throw(StudyContractError("nonfinite JSON")))
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid JSON") from exc
    if canonical_json_bytes_v5(value) != raw:
        raise StudyContractError(f"{label} is not canonical JSON")
    return value


def _validate_parsed_study_response(
    value: object,
    configuration_ids: tuple[str, ...],
    *,
    expected_binding: RoleBindingV5 | None = None,
) -> StudyResponseV1:
    try:
        parsed = StudyResponseV1.from_primitive(value)
    except (StudyContractError, TypeError, ValueError) as exc:
        raise StudyAuthorityError("study parsed response is not a closed response") from exc
    if any(item.configuration_id not in configuration_ids for item in parsed.drafts):
        raise StudyAuthorityError("study parsed response uses an unregistered configuration")
    if expected_binding is not None and parsed.binding != expected_binding:
        raise StudyAuthorityError("study parsed response binding differs from the authenticated F binding")
    return parsed


def run_study_call_v1(
    *,
    request: StudyCallRequestV1,
    fixture_request,
    ledger: StudyLedgerV1,
    gateway,
    deadline_monotonic: float,
) -> AuthenticatedStudyTerminalV1:
    """Run one admitted call; transport exceptions leave accounting pending."""

    if type(fixture_request) is not RoleRequestV5:
        raise StudyAuthorityError("study caller fixture request is invalid")
    authenticated_fixture_request = ledger._authenticated_fixture_request(request)
    if fixture_request != authenticated_fixture_request or fixture_request.sha256 != request.fixture_request_sha256:
        raise StudyAuthorityError("study caller fixture request differs from the authenticated F request")
    recovered = ledger.recover(request)
    if recovered is not None:
        return recovered
    ledger._validate_live_deadline(deadline_monotonic)
    reservation = ledger.reserve(request)
    from .transport import StudyOpenRouterGatewayV1

    try:
        if type(gateway) is not StudyOpenRouterGatewayV1:
            ledger.claim_dispatch(
                request_sha256=request.sha256,
                model=request.model,
                messages=request.messages,
                response_schema_json=request.schema_json,
                max_output_tokens=request.max_output_tokens,
            )
        completion = gateway.invoke_json_once(
            request_sha256=request.sha256,
            model=request.model,
            messages=request.messages,
            response_schema_json=request.schema_json,
            max_output_tokens=request.max_output_tokens,
            automatic_retries=0,
            schema_repair_calls=0,
            deadline_monotonic=deadline_monotonic,
        )
    except (StudyAdmissionError, StudyAuthorityError, StudyContractError) as exc:
        if any(claim.request_sha256 == request.sha256 for claim in ledger._dispatch_claims()):
            ledger.persist_provider_diagnostic_for_pending_accounting(request, ProviderFailureDiagnosticV5.from_exception(exc))
            raise StudyPendingAccounting("study provider dispatch has unresolved accounting") from exc
        ledger.reject_pre_dispatch(
            reservation,
            reason=str(exc).strip() or "study provider dispatch was rejected before provider handoff",
        )
        raise
    except ProviderResponseAccountingErrorV5 as exc:
        ledger.persist_observed_response_for_pending_accounting(request, exc)
        raise StudyPendingAccounting("study provider dispatch has unresolved accounting") from exc
    except ProviderFailureDiagnosticV5 as exc:
        ledger.persist_provider_diagnostic_for_pending_accounting(request, exc)
        raise StudyPendingAccounting("study provider dispatch has unresolved accounting") from exc
    except BaseException as exc:
        # No zero-usage completion is synthesized.  Persist only closed safe
        # diagnostic fields; exception text, headers, and credentials never enter the study store.
        ledger.persist_provider_diagnostic_for_pending_accounting(request, ProviderFailureDiagnosticV5.from_exception(exc))
        raise StudyPendingAccounting("study provider dispatch has unresolved accounting") from exc
    if type(completion) is not CompletionResultV5:
        raise StudyPendingAccounting("study provider returned an untyped completion")
    # Persist the exact response text separately from its usage envelope.
    ledger.persist_response_for_request(request, completion)
    raw_response_ref = ledger._raw_response_reference(request)
    if raw_response_ref is None:
        raise StudyPendingAccounting("study response bytes were not durably persisted")
    if not completion.accepted or not completion.response_received:
        parsed_ref, parse_code, parse_reason = None, RoleFailureCode.TRANSPORT, "provider did not return an accepted completion"
    else:
        parsed_ref, parse_code, parse_reason = ledger._parse_and_persist(request, completion, raw_response_ref)
    failure_code = None if parsed_ref is not None else (
        RoleFailureCode.TRANSPORT
        if (not completion.accepted or not completion.response_received)
        else parse_code or RoleFailureCode.RESPONSE_SCHEMA
    )
    terminal = ledger.settle(
        reservation,
        completion=completion,
        response_ref=raw_response_ref,
        parsed_ref=parsed_ref,
        failure_code=failure_code,
        failure_reason=parse_reason,
    )
    return terminal


def recover_study_call_v1(*, request: StudyCallRequestV1, ledger: StudyLedgerV1) -> AuthenticatedStudyTerminalV1 | None:
    return ledger.recover(request)


__all__ = [
    "StudyAdmissionRejectionV1",
    "AuthenticatedStudyTerminalV1",
    "StudyCallTerminalV1",
    "StudyDispatchClaimV1",
    "StudyLedgerV1",
    "StudyReservationV1",
    "StudyUsageReconciliationV1",
    "recover_study_call_v1",
    "run_study_call_v1",
]
