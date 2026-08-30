"""Explicit, append-only authorization for schema-v3 PIT optimizer runs.

Preparing a manifest never calls this module.  Authority is created only by an
explicit ``record-grant`` API/CLI invocation against an authenticated manifest.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence

from core.pit_optimization_contract import (
    AuthorizationRequirement,
    AuthorArtifact,
    AuthorInput,
    CriticArtifact,
    CriticInput,
    InvestigatorArtifact,
    InvestigatorInput,
    OPTIMIZER_V2_ROLES,
    PatchBounds,
    PitOptimizerCallBudget,
    PitOptimizerRunManifest,
    PolicySourceBundle,
    PolicySourceRecord,
    authenticate_policy_source_bundle,
)
import core.pit_optimization_contract as _contract


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_MODEL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
)
_UNSAFE_APPROVAL_RE = re.compile(
    r"(?:sk-|bearer\s|api[_-]?key|credential|password|secret)",
    re.IGNORECASE,
)
_ZERO_SHA256 = "0" * 64
_GATEWAY_TERMINAL_RECOVERY_SEAL = object()


class AuthorizationError(RuntimeError):
    """Raised when explicit optimizer authority is absent or inconsistent."""


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _canonical_decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _optimizer_pricing_digest(
    model: str,
    lookup_status: str,
    prompt: Decimal | None,
    completion: Decimal | None,
) -> str:
    payload = {
        "model": model,
        "lookup_status": lookup_status,
        "prompt_per_million": (
            None if prompt is None else _canonical_decimal_text(prompt)
        ),
        "completion_per_million": (
            None if completion is None else _canonical_decimal_text(completion)
        ),
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _canonical_optional_decimal(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a finite non-negative Decimal or null")
    return _canonical_decimal_text(value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise AuthorizationError("authorization ledger contains duplicate JSON keys")
        value[key] = item
    return value


def _approval_reference_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or _UNSAFE_APPROVAL_RE.search(value) is not None
    ):
        raise AuthorizationError("operator approval reference must be non-secret canonical text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authorization_record_digest(record: Mapping[str, object]) -> str:
    preimage = dict(record)
    preimage.pop("record_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def read_legacy_authorization_history(
    path: Path,
) -> tuple[Mapping[str, object], ...]:
    """Verify and expose schema-v2 records as immutable, non-resumable history."""

    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or not candidate.is_file()
        or candidate.is_symlink()
    ):
        raise AuthorizationError("legacy authorization history path is invalid")
    raw = candidate.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise AuthorizationError("legacy authorization history has a partial record")
    records: list[Mapping[str, object]] = []
    previous = _ZERO_SHA256
    for index, line in enumerate(raw.splitlines(keepends=True), start=1):
        try:
            value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, AuthorizationError) as exc:
            raise AuthorizationError(
                "legacy authorization history contains invalid JSON"
            ) from exc
        if not isinstance(value, dict) or line != _canonical_json_bytes(value):
            raise AuthorizationError(
                "legacy authorization history record is not canonical JSON"
            )
        if value.get("schema_version") != 2 or value.get("record_index") != index:
            raise AuthorizationError(
                "legacy authorization history record index is invalid"
            )
        if value.get("previous_record_sha256") != previous:
            raise AuthorizationError(
                "legacy authorization history hash chain is broken"
            )
        try:
            digest = _require_digest(
                value.get("record_sha256"),
                "legacy authorization history record SHA-256",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        if digest != _authorization_record_digest(value):
            raise AuthorizationError(
                "legacy authorization history record digest differs"
            )
        frozen = _freeze_json_value(value)
        assert isinstance(frozen, Mapping)
        records.append(frozen)
        previous = digest
    return tuple(records)


@dataclass(frozen=True, slots=True)
class OperatorAuthorizationGrant:
    grant_id: str
    additional_calls: int
    additional_tokens: int
    policy_source_scope_sha256: str

    def __post_init__(self) -> None:
        _require_id(self.grant_id, "authorization grant ID")
        _positive_int(self.additional_calls, "authorization grant additional calls")
        _positive_int(self.additional_tokens, "authorization grant additional tokens")
        _require_digest(
            self.policy_source_scope_sha256,
            "authorization grant policy source scope SHA-256",
        )


@dataclass(frozen=True, slots=True)
class OperatorAuthorizationWindow:
    window_id: str
    grant_ids: tuple[str, ...]
    authorization_requirement_sha256: str
    max_calls: int
    max_tokens: int
    policy_source_scope_sha256: str

    def __post_init__(self) -> None:
        _require_id(self.window_id, "authorization window ID")
        if (
            type(self.grant_ids) is not tuple
            or not self.grant_ids
            or len(self.grant_ids) != len(set(self.grant_ids))
        ):
            raise ValueError("authorization window grant IDs are invalid")
        for grant_id in self.grant_ids:
            _require_id(grant_id, "authorization window grant ID")
        _require_digest(
            self.authorization_requirement_sha256,
            "authorization window requirement SHA-256",
        )
        _positive_int(self.max_calls, "authorization window call cap")
        _positive_int(self.max_tokens, "authorization window token cap")
        _require_digest(
            self.policy_source_scope_sha256,
            "authorization window policy source scope SHA-256",
        )


@dataclass(frozen=True, slots=True)
class OptimizerPricingSnapshot:
    model: str
    lookup_status: str
    prompt_per_million: Decimal | None
    completion_per_million: Decimal | None
    pricing_payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or _MODEL_RE.fullmatch(self.model) is None:
            raise ValueError("optimizer pricing model is invalid")
        if self.lookup_status not in {"available", "unavailable"}:
            raise ValueError("optimizer pricing lookup status is invalid")
        if self.lookup_status == "available":
            for value in (self.prompt_per_million, self.completion_per_million):
                if (
                    not isinstance(value, Decimal)
                    or not value.is_finite()
                    or value < 0
                ):
                    raise ValueError(
                        "available optimizer pricing rates must be finite "
                        "non-negative Decimals"
                    )
        elif self.prompt_per_million is not None or self.completion_per_million is not None:
            raise ValueError("unavailable optimizer pricing cannot contain rates")
        _require_digest(self.pricing_payload_sha256, "optimizer pricing payload SHA-256")
        if self.pricing_payload_sha256 != _optimizer_pricing_digest(
            self.model,
            self.lookup_status,
            self.prompt_per_million,
            self.completion_per_million,
        ):
            raise ValueError("optimizer pricing digest differs from its closed payload")

    @classmethod
    def available(
        cls,
        *,
        model: str,
        prompt: Decimal,
        completion: Decimal,
    ) -> "OptimizerPricingSnapshot":
        return cls(
            model=model,
            lookup_status="available",
            prompt_per_million=prompt,
            completion_per_million=completion,
            pricing_payload_sha256=_optimizer_pricing_digest(
                model,
                "available",
                prompt,
                completion,
            ),
        )

    @classmethod
    def unavailable(cls, *, model: str) -> "OptimizerPricingSnapshot":
        return cls(
            model=model,
            lookup_status="unavailable",
            prompt_per_million=None,
            completion_per_million=None,
            pricing_payload_sha256=_optimizer_pricing_digest(
                model,
                "unavailable",
                None,
                None,
            ),
        )

    def projected_call_usd(
        self,
        prompt_bytes: int,
        output_tokens: int,
    ) -> Decimal | None:
        if (
            type(prompt_bytes) is not int
            or prompt_bytes < 0
            or type(output_tokens) is not int
            or output_tokens < 0
        ):
            raise ValueError("optimizer pricing projection bounds are invalid")
        if self.lookup_status == "unavailable":
            return None
        assert self.prompt_per_million is not None
        assert self.completion_per_million is not None
        return (
            Decimal(prompt_bytes) * self.prompt_per_million
            + Decimal(output_tokens) * self.completion_per_million
        ) / Decimal(1_000_000)

    def to_primitive(self) -> dict[str, object]:
        return {
            "model": self.model,
            "lookup_status": self.lookup_status,
            "prompt_per_million": (
                None
                if self.prompt_per_million is None
                else _canonical_decimal_text(self.prompt_per_million)
            ),
            "completion_per_million": (
                None
                if self.completion_per_million is None
                else _canonical_decimal_text(self.completion_per_million)
            ),
            "pricing_payload_sha256": self.pricing_payload_sha256,
        }


def _pricing_snapshot_from_primitive(value: object) -> OptimizerPricingSnapshot:
    expected = {
        "model",
        "lookup_status",
        "prompt_per_million",
        "completion_per_million",
        "pricing_payload_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("optimizer pricing snapshot keys are invalid")

    def rate(name: str) -> Decimal | None:
        raw = value.get(name)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError("optimizer pricing rate is not canonical text")
        parsed = Decimal(raw)
        if _canonical_decimal_text(parsed) != raw:
            raise ValueError("optimizer pricing rate is not canonical text")
        return parsed

    return OptimizerPricingSnapshot(
        model=value.get("model"),
        lookup_status=value.get("lookup_status"),
        prompt_per_million=rate("prompt_per_million"),
        completion_per_million=rate("completion_per_million"),
        pricing_payload_sha256=value.get("pricing_payload_sha256"),
    )


@dataclass(frozen=True, slots=True)
class AuthorizationRunLease:
    lease_id: str
    one_shot_key_sha256: str
    window_id: str
    run_manifest_sha256: str
    pricing_snapshot_sha256: str
    pricing_status: str
    projected_plan_usd: str | None
    max_calls: int
    max_tokens: int

    def __post_init__(self) -> None:
        _require_id(self.lease_id, "authorization lease ID")
        _require_digest(self.one_shot_key_sha256, "authorization one-shot key SHA-256")
        _require_id(self.window_id, "authorization lease window ID")
        _require_digest(self.run_manifest_sha256, "authorization lease manifest SHA-256")
        _require_digest(self.pricing_snapshot_sha256, "authorization lease pricing SHA-256")
        if self.pricing_status not in {"available", "unavailable"}:
            raise ValueError("authorization lease pricing status is invalid")
        if self.projected_plan_usd is not None:
            try:
                projected = Decimal(self.projected_plan_usd)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("authorization lease projection is invalid") from exc
            if (
                not isinstance(self.projected_plan_usd, str)
                or not projected.is_finite()
                or projected < 0
                or _canonical_decimal_text(projected) != self.projected_plan_usd
            ):
                raise ValueError("authorization lease projection is invalid")
        if (self.projected_plan_usd is None) is (self.pricing_status == "available"):
            raise ValueError("authorization lease pricing projection is inconsistent")
        _positive_int(self.max_calls, "authorization lease call cap")
        _positive_int(self.max_tokens, "authorization lease token cap")


@dataclass(frozen=True, slots=True)
class AuthorizationCallReservation:
    reservation_id: str
    lease_id: str
    call_index: int
    iteration: int
    role: str
    reserved_tokens: int
    projected_call_usd: str | None

    def __post_init__(self) -> None:
        _require_id(self.reservation_id, "authorization reservation ID")
        _require_id(self.lease_id, "authorization reservation lease ID")
        _positive_int(self.call_index, "authorization reservation call index")
        _positive_int(self.iteration, "authorization reservation iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("authorization reservation role is invalid")
        _positive_int(self.reserved_tokens, "authorization reservation token cap")
        if self.projected_call_usd is not None:
            try:
                projected = Decimal(self.projected_call_usd)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("authorization reservation projection is invalid") from exc
            if (
                not isinstance(self.projected_call_usd, str)
                or not projected.is_finite()
                or projected < 0
                or _canonical_decimal_text(projected) != self.projected_call_usd
            ):
                raise ValueError("authorization reservation projection is invalid")


@dataclass(frozen=True, slots=True)
class PitOptimizerProviderFacts:
    call_index: int
    iteration: int
    role: str
    requested_model: str
    returned_model: str | None
    pricing_snapshot_sha256: str
    outcome: str
    request_started: bool
    response_received: bool
    finish_reason: str | None
    response_schema_valid: bool
    accounting_complete: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    retained_reservation_tokens: int
    audit_sha256: str
    request_failure_class: str | None = None
    request_failure_status_code: int | None = None
    response_validation_code: str | None = None
    accounting_failure_code: str | None = None
    accounting_source: str | None = None

    def __post_init__(self) -> None:
        _positive_int(self.call_index, "optimizer provider call index")
        _positive_int(self.iteration, "optimizer provider iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("optimizer provider role is invalid")
        if _MODEL_RE.fullmatch(self.requested_model or "") is None:
            raise ValueError("optimizer requested model is invalid")
        if self.returned_model is not None and _MODEL_RE.fullmatch(self.returned_model) is None:
            raise ValueError("optimizer returned model is invalid")
        _require_digest(self.pricing_snapshot_sha256, "optimizer pricing snapshot SHA-256")
        if self.outcome not in {
            "accepted",
            "schema_invalid",
            "budget_exceeded",
            "failed_before_send",
            "uncertain_accounting",
            "provider_failed",
        }:
            raise ValueError("optimizer provider outcome is invalid")
        if type(self.request_started) is not bool or type(self.response_received) is not bool:
            raise ValueError("optimizer provider lifecycle facts are invalid")
        if self.response_received and not self.request_started:
            raise ValueError("optimizer provider response cannot precede request start")
        if self.finish_reason not in {None, "stop", "non_stop", "unknown"}:
            raise ValueError("optimizer provider finish reason is invalid")
        if self.request_failure_class not in {
            None,
            "provider_http",
            "transport",
            "unknown",
        }:
            raise ValueError("optimizer request failure class is invalid")
        if self.request_failure_class is None:
            if self.request_failure_status_code is not None:
                raise ValueError("optimizer request failure status is inconsistent")
        elif self.request_failure_class == "provider_http":
            if (
                type(self.request_failure_status_code) is not int
                or not 100 <= self.request_failure_status_code <= 599
            ):
                raise ValueError("optimizer provider HTTP status is invalid")
        elif self.request_failure_status_code is not None:
            raise ValueError("optimizer non-HTTP failure cannot carry a status")
        if self.response_validation_code not in {
            None,
            *_contract.PIT_OPTIMIZER_RESPONSE_VALIDATION_CODES,
        }:
            raise ValueError("optimizer response validation code is invalid")
        if self.accounting_failure_code is not None and (
            not isinstance(self.accounting_failure_code, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.accounting_failure_code)
            is None
        ):
            raise ValueError("optimizer accounting failure code is invalid")
        if type(self.response_schema_valid) is not bool or type(self.accounting_complete) is not bool:
            raise ValueError("optimizer provider validation/accounting facts are invalid")
        if self.accounting_source not in {
            None,
            "inline",
            "generation_endpoint",
            "frozen_pricing",
        }:
            raise ValueError("optimizer accounting source is invalid")
        if not self.accounting_complete and self.accounting_source is not None:
            raise ValueError("incomplete optimizer accounting cannot name a source")
        if self.request_failure_class is not None and not (
            self.outcome == "uncertain_accounting"
            and self.request_started
            and not self.response_received
            and not self.accounting_complete
        ):
            raise ValueError("optimizer request failure provenance is inconsistent")
        if self.response_validation_code is not None and not (
            self.outcome == "schema_invalid"
            and self.request_started
            and self.response_received
            and not self.response_schema_valid
            and self.accounting_complete
        ):
            raise ValueError("optimizer response validation code is inconsistent")
        if self.accounting_failure_code is not None and not (
            self.outcome == "uncertain_accounting"
            and self.request_started
            and self.response_received
            and not self.accounting_complete
        ):
            raise ValueError("optimizer accounting failure code is inconsistent")
        if (
            type(self.retained_reservation_tokens) is not int
            or self.retained_reservation_tokens < 0
        ):
            raise ValueError("optimizer retained reservation is invalid")
        if self.accounting_complete:
            values = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
            if any(type(value) is not int or value < 0 for value in values):
                raise ValueError("optimizer authoritative token accounting is invalid")
            assert self.prompt_tokens is not None
            assert self.completion_tokens is not None
            assert self.total_tokens is not None
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError("optimizer authoritative token total is inconsistent")
            if (
                type(self.cost_usd) not in {int, float}
                or not math.isfinite(float(self.cost_usd))
                or self.cost_usd < 0
            ):
                raise ValueError("optimizer authoritative cost is invalid")
            if self.retained_reservation_tokens != 0:
                raise ValueError("authoritative optimizer facts cannot retain a reservation")
        else:
            if any(
                value is not None
                for value in (
                    self.prompt_tokens,
                    self.completion_tokens,
                    self.total_tokens,
                    self.cost_usd,
                )
            ):
                raise ValueError("incomplete optimizer accounting cannot claim exact usage")
            if not self.request_started or self.outcome != "uncertain_accounting":
                raise ValueError("incomplete optimizer accounting lifecycle is inconsistent")
            if self.retained_reservation_tokens <= 0:
                raise ValueError("incomplete optimizer accounting must retain its reservation")
        if not self.request_started:
            if (
                self.outcome != "failed_before_send"
                or self.response_received
                or not self.accounting_complete
                or self.total_tokens != 0
                or self.cost_usd != 0
            ):
                raise ValueError("before-send optimizer failure facts are inconsistent")
        if self.outcome == "accepted" and not (
            self.request_started
            and self.response_received
            and self.returned_model == self.requested_model
            and self.finish_reason == "stop"
            and self.response_schema_valid
            and self.accounting_complete
        ):
            raise ValueError("accepted optimizer provider facts are not fully closed")
        _require_digest(self.audit_sha256, "optimizer provider audit SHA-256")


@dataclass(frozen=True, slots=True)
class PitOptimizerRoleCall:
    """One schema-v2 role artifact paired with sealed plan and durable facts."""

    plan: PitOptimizerCallBudget
    payload: InvestigatorArtifact | AuthorArtifact | CriticArtifact
    facts: PitOptimizerProviderFacts

    def __post_init__(self) -> None:
        if not isinstance(self.plan, PitOptimizerCallBudget):
            raise ValueError("optimizer role call plan is invalid")
        expected_payload = {
            "investigator": InvestigatorArtifact,
            "author": AuthorArtifact,
            "critic": CriticArtifact,
        }[self.plan.role]
        if not isinstance(self.payload, expected_payload):
            raise ValueError("optimizer role call payload differs from plan role")
        if not isinstance(self.facts, PitOptimizerProviderFacts):
            raise ValueError("optimizer role call facts are invalid")
        if (
            self.facts.call_index,
            self.facts.iteration,
            self.facts.role,
            self.facts.requested_model,
        ) != (
            self.plan.call_index,
            self.plan.iteration,
            self.plan.role,
            self.plan.model,
        ):
            raise ValueError("optimizer role call facts differ from plan")
        if self.facts.outcome != "accepted" or not self.facts.response_schema_valid:
            raise ValueError("optimizer role call requires accepted provider facts")


@dataclass(frozen=True, slots=True)
class TerminalAuditReceipt:
    """Durable identity cross-verified against one immutable AuditTrail event."""

    audit_run_id: str
    run_manifest_sha256: str
    call_index: int
    iteration: int
    role: str
    outcome: str
    provider_record_sha256: str
    terminal_event_sha256: str
    payload_sha256: str | None
    terminal_code: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", self.audit_run_id) is None:
            raise ValueError("terminal audit receipt run ID is invalid")
        _require_digest(
            self.run_manifest_sha256,
            "terminal audit receipt run manifest SHA-256",
        )
        _positive_int(self.call_index, "terminal audit receipt call index")
        _positive_int(self.iteration, "terminal audit receipt iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("terminal audit receipt role is invalid")
        if self.outcome not in {
            "accepted",
            "schema_invalid",
            "budget_exceeded",
            "failed_before_send",
            "uncertain_accounting",
            "provider_failed",
        }:
            raise ValueError("terminal audit receipt outcome is invalid")
        _require_digest(
            self.provider_record_sha256,
            "terminal audit receipt provider record SHA-256",
        )
        _require_digest(
            self.terminal_event_sha256,
            "terminal audit receipt terminal event SHA-256",
        )
        if self.outcome == "accepted":
            _require_digest(
                self.payload_sha256,
                "terminal audit receipt payload SHA-256",
            )
            if self.terminal_code not in {
                None,
                "failed",
                "cancelled",
                "budget_exhausted",
            }:
                raise ValueError("terminal audit receipt code is invalid")
        elif self.payload_sha256 is not None:
            raise ValueError("rejected terminal audit receipt cannot bind a payload")
        elif self.terminal_code not in {
            "failed",
            "cancelled",
            "budget_exhausted",
        }:
            raise ValueError("rejected terminal audit receipt code is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "audit_run_id": self.audit_run_id,
            "run_manifest_sha256": self.run_manifest_sha256,
            "call_index": self.call_index,
            "iteration": self.iteration,
            "role": self.role,
            "outcome": self.outcome,
            "provider_record_sha256": self.provider_record_sha256,
            "terminal_event_sha256": self.terminal_event_sha256,
            "payload_sha256": self.payload_sha256,
            "terminal_code": self.terminal_code,
        }


@dataclass(frozen=True, slots=True)
class AuthenticatedRoleInputSnapshot:
    """One controller-originated role input, closed over its exact wire bytes."""

    role: str
    iteration: int
    canonical_bytes: bytes
    expected_hypothesis_id: str | None = None
    source_bundle: PolicySourceBundle | None = None
    candidate_bounds: PatchBounds | None = None

    def validate_artifact(
        self,
        artifact: InvestigatorArtifact | AuthorArtifact | CriticArtifact,
    ) -> None:
        """Validate response relationships only against the authenticated snapshot."""

        expected_type = {
            "investigator": InvestigatorArtifact,
            "author": AuthorArtifact,
            "critic": CriticArtifact,
        }[self.role]
        if not isinstance(artifact, expected_type):
            raise ValueError("optimizer response has an invalid type")
        if self.role == "author":
            assert isinstance(artifact, AuthorArtifact)
            # The author proposal is structurally parsed before this point.
            # Its one authoritative applicability check runs later against the
            # disposable candidate checkout with Git.  Repeating that check
            # against an in-memory source bundle changes line-ending semantics
            # and can reject a diff that Git correctly accepts, so metadata and
            # diff applicability remain controller-derived at that boundary.
        elif self.role == "critic":
            assert isinstance(artifact, CriticArtifact)
            if artifact.hypothesis_id != self.expected_hypothesis_id:
                raise ValueError("critic hypothesis differs from its input")


def require_authorized_policy_source_scope(
    manifest: PitOptimizerRunManifest,
    requirement: AuthorizationRequirement,
    window: OperatorAuthorizationWindow,
) -> str:
    """Authenticate one window against the exact manifest policy-source scope."""

    if not isinstance(manifest, PitOptimizerRunManifest):
        raise AuthorizationError("authorization manifest is invalid")
    if not isinstance(requirement, AuthorizationRequirement):
        raise AuthorizationError("authorization requirement is invalid")
    if not isinstance(window, OperatorAuthorizationWindow):
        raise AuthorizationError("authorization window is invalid")
    expected = manifest.policy_source_scope.sha256
    if requirement.sha256 != manifest.authorization_requirement.sha256:
        raise AuthorizationError("authorization requirement mismatch")
    if window.authorization_requirement_sha256 != requirement.sha256:
        raise AuthorizationError("authorization window requirement mismatch")
    if (
        requirement.policy_source_scope_sha256 != expected
        or window.policy_source_scope_sha256 != expected
    ):
        raise AuthorizationError("policy source scope mismatch")
    return expected


@contextmanager
def _authorization_file_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AuthorizationLedger:
    """Permanent hash-chained grant/window ledger for one authenticated manifest."""

    def __init__(self, path: Path, manifest: PitOptimizerRunManifest) -> None:
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or candidate.name != "pit_optimizer_authorization_ledger.jsonl"
            or not candidate.parent.is_dir()
            or candidate.parent.is_symlink()
            or candidate.is_symlink()
        ):
            raise AuthorizationError("authorization ledger path is invalid")
        if not isinstance(manifest, PitOptimizerRunManifest):
            raise AuthorizationError("authorization ledger manifest is invalid")
        try:
            manifest_primitive = json.loads(
                manifest.canonical_json_bytes(),
                object_pairs_hook=_reject_duplicate_keys,
            )
            if not isinstance(manifest_primitive, dict):
                raise ValueError("optimizer manifest snapshot is invalid")
            manifest_snapshot = _contract._pit_optimizer_manifest_from_primitive(
                manifest_primitive
            )
        except (AuthorizationError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthorizationError(
                "authorization ledger manifest snapshot is invalid"
            ) from exc
        self._path = candidate.resolve()
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._manifest = manifest_snapshot
        self._call_plan_snapshots = tuple(
            PitOptimizerCallBudget(**item.to_primitive())
            for item in manifest_snapshot.call_budgets
        )
        # Strong references make object identity a bounded, live controller
        # capability.  Reopening the ledger intentionally loses these
        # process-local capabilities and therefore fails closed.
        self._role_input_capabilities: dict[
            int,
            tuple[object, AuthenticatedRoleInputSnapshot],
        ] = {}
        self._consumed_role_input_plans: set[int] = set()
        self._role_input_lock = threading.Lock()
        self._audit_trail: object | None = None
        with _authorization_file_lock(self._lock_path):
            self._read_records()

    @property
    def manifest(self) -> PitOptimizerRunManifest:
        """Return the immutable manifest authenticated by this ledger instance."""

        return self._manifest

    def attach_audit_trail(self, audit_trail: object) -> None:
        """Bind and cross-verify the one AuditTrail backing durable receipts."""

        # The import stays local so the authorization contract remains usable by
        # manifest tooling without importing the provider controller at startup.
        from agent_loop import AuditTrail

        if not isinstance(audit_trail, AuditTrail):
            raise AuthorizationError("authorization audit trail is invalid")
        if audit_trail.run_id != self._manifest.run_id:
            raise AuthorizationError(
                "authorization audit run differs from manifest"
            )
        if self._audit_trail is not None and self._audit_trail is not audit_trail:
            raise AuthorizationError("authorization audit trail is already bound")
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
        reservations: dict[str, AuthorizationCallReservation] = {}
        for item in records:
            if item.get("record_type") != "reservation":
                continue
            primitive = item.get("reservation")
            if not isinstance(primitive, dict):
                raise AuthorizationError(
                    "authorization reservation record is invalid"
                )
            try:
                reservation = AuthorizationCallReservation(**primitive)
            except (TypeError, ValueError) as exc:
                raise AuthorizationError(
                    "authorization reservation record is invalid"
                ) from exc
            reservations[reservation.reservation_id] = reservation
        for item in records:
            primitive = item.get("terminal_audit_receipt")
            if item.get("record_type") != "reconciliation" or primitive is None:
                continue
            try:
                if not isinstance(primitive, dict):
                    raise TypeError("receipt is not a mapping")
                receipt = TerminalAuditReceipt(**primitive)
                facts_primitive = item.get("provider_facts")
                if not isinstance(facts_primitive, dict):
                    raise TypeError("facts are not a mapping")
                facts = PitOptimizerProviderFacts(**facts_primitive)
                reservation = reservations[str(item.get("reservation_id"))]
                budget_state = audit_trail.verify_terminal_audit_receipt(
                    receipt,
                    authorization_reservation=reservation,
                    provider_facts=facts,
                )
                self._require_gateway_terminal_commitment(
                    records,
                    reservation,
                    facts,
                    receipt,
                    budget_state=budget_state,
                )
            except Exception as exc:
                raise AuthorizationError(
                    "authorization audit receipt cross-verification failed"
                ) from exc
        self._audit_trail = audit_trail

    def _cross_verify_audit_receipt(
        self,
        receipt: TerminalAuditReceipt,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        *,
        records: Sequence[dict[str, object]] | None = None,
        require_terminal_commitment: bool = True,
    ) -> dict[str, object]:
        audit_trail = self._audit_trail
        if audit_trail is None:
            raise AuthorizationError(
                "authorization audit trail is required for terminal receipt"
            )
        try:
            if records is None:
                with _authorization_file_lock(self._lock_path):
                    durable_records = self._read_records()
            else:
                durable_records = list(records)
            matches = [
                item
                for item in durable_records
                if item.get("record_type") == "reservation"
                and isinstance(item.get("reservation"), dict)
                and item["reservation"].get("reservation_id")
                == reservation.reservation_id
            ]
            if len(matches) != 1:
                raise AuthorizationError(
                    "authorization audit reservation is absent"
                )
            durable_reservation = AuthorizationCallReservation(
                **matches[0]["reservation"]
            )
            if durable_reservation != reservation:
                raise AuthorizationError(
                    "authorization audit reservation identity mismatch"
                )
            budget_state = audit_trail.verify_terminal_audit_receipt(
                receipt,
                authorization_reservation=durable_reservation,
                provider_facts=provider_facts,
            )
            if require_terminal_commitment:
                self._require_gateway_terminal_commitment(
                    durable_records,
                    durable_reservation,
                    provider_facts,
                    receipt,
                    budget_state=budget_state,
                )
            else:
                self._require_gateway_lifecycle_commitment(
                    durable_records,
                    durable_reservation,
                    provider_facts,
                    receipt,
                    budget_state=budget_state,
                )
            return budget_state
        except Exception as exc:
            raise AuthorizationError(
                "authorization audit receipt cross-verification failed"
            ) from exc

    def verify_terminal_audit_receipt(
        self,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
    ) -> dict[str, object]:
        """Authenticate terminal evidence and return only its verified budget image."""

        if (
            not isinstance(reservation, AuthorizationCallReservation)
            or not isinstance(provider_facts, PitOptimizerProviderFacts)
            or not isinstance(receipt, TerminalAuditReceipt)
        ):
            raise AuthorizationError(
                "authorization terminal audit verification is invalid"
            )
        if (
            receipt.run_manifest_sha256 != self._manifest.sha256
            or (
                receipt.call_index,
                receipt.iteration,
                receipt.role,
                receipt.outcome,
            )
            != (
                reservation.call_index,
                reservation.iteration,
                reservation.role,
                provider_facts.outcome,
            )
        ):
            raise AuthorizationError(
                "authorization terminal audit receipt differs from reservation"
            )
        return self._cross_verify_audit_receipt(
            receipt,
            reservation,
            provider_facts,
        )

    def snapshot_call_plan(
        self,
        plan: PitOptimizerCallBudget,
    ) -> PitOptimizerCallBudget:
        """Authenticate one caller plan and return the ledger-owned canonical copy."""

        if (
            not isinstance(plan, PitOptimizerCallBudget)
            or plan.call_index > len(self._call_plan_snapshots)
        ):
            raise AuthorizationError("optimizer call plan is invalid")
        expected = self._call_plan_snapshots[plan.call_index - 1]
        if plan != expected:
            raise AuthorizationError("authorization call differs from sealed plan")
        return PitOptimizerCallBudget(**expected.to_primitive())

    def _snapshot_role_input(
        self,
        dynamic_input: object,
        plan: PitOptimizerCallBudget,
    ) -> AuthenticatedRoleInputSnapshot:
        """Capture first, then authenticate provenance from those exact bytes."""

        expected_types = {
            "investigator": InvestigatorInput,
            "author": AuthorInput,
            "critic": CriticInput,
        }
        if (
            not isinstance(plan, PitOptimizerCallBudget)
            or not isinstance(dynamic_input, expected_types.get(plan.role, ()))
        ):
            raise AuthorizationError("optimizer role input contract is invalid")
        try:
            canonical_bytes = dynamic_input.canonical_json_bytes()
            primitive = json.loads(
                canonical_bytes,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthorizationError("optimizer role input snapshot is invalid") from exc
        if (
            not isinstance(primitive, dict)
            or canonical_bytes
            != json.dumps(
                primitive,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                ensure_ascii=False,
            ).encode("utf-8")
        ):
            raise AuthorizationError("optimizer role input snapshot is not canonical")
        if (
            primitive.get("schema_version") != 2
            or primitive.get("iteration") != plan.iteration
            or primitive.get("run_manifest_sha256") != self._manifest.sha256
            or primitive.get("immutable_constraint_ids")
            != list(self._manifest.immutable_constraint_ids)
        ):
            raise AuthorizationError("optimizer role input differs from run manifest")

        source_bundle: PolicySourceBundle | None = None
        candidate_bounds: PatchBounds | None = None
        expected_hypothesis_id: str | None = None
        if plan.role in {"investigator", "author"}:
            if (
                primitive.get("policy_interface_version")
                != self._manifest.policy_interface_version
                or primitive.get("candidate_bounds")
                != self._manifest.candidate_bounds.to_primitive()
            ):
                raise AuthorizationError(
                    "optimizer source input differs from run manifest"
                )
            source = primitive.get("source_bundle")
            try:
                if not isinstance(source, dict) or set(source) != {
                    "policy_interface_version",
                    "cumulative_diff_sha256",
                    "cumulative_diff",
                    "files",
                }:
                    raise ValueError("policy source snapshot keys are invalid")
                files = source.get("files")
                if not isinstance(files, list):
                    raise ValueError("policy source snapshot files are invalid")
                source_records: list[PolicySourceRecord] = []
                for item in files:
                    if not isinstance(item, dict) or set(item) != {
                        "path",
                        "sha256",
                        "declared_symbols",
                        "text",
                    }:
                        raise ValueError("policy source record snapshot is invalid")
                    declared_symbols = item.get("declared_symbols")
                    if not isinstance(declared_symbols, list):
                        raise ValueError("policy source symbols snapshot is invalid")
                    source_records.append(
                        PolicySourceRecord(
                            path=item.get("path"),
                            sha256=item.get("sha256"),
                            declared_symbols=tuple(declared_symbols),
                            text=item.get("text"),
                        )
                    )
                source_bundle = PolicySourceBundle(
                    policy_interface_version=source.get(
                        "policy_interface_version"
                    ),
                    cumulative_diff_sha256=source.get(
                        "cumulative_diff_sha256"
                    ),
                    cumulative_diff=source.get("cumulative_diff"),
                    files=tuple(source_records),
                    _controller_seal=_contract._POLICY_SOURCE_BUNDLE_SEAL,
                )
                if source_bundle.to_primitive() != source:
                    raise ValueError("policy source snapshot differs after rebuild")
                authenticate_policy_source_bundle(
                    scope=self._manifest.policy_source_scope,
                    bundle=source_bundle,
                )
                bounds = primitive.get("candidate_bounds")
                if not isinstance(bounds, dict):
                    raise ValueError("candidate bounds snapshot is invalid")
                candidate_bounds = PatchBounds(**bounds)
            except (TypeError, ValueError) as exc:
                raise AuthorizationError(
                    "policy source bundle differs from authorized scope"
                ) from exc
            if plan.role == "author":
                investigator = primitive.get("investigator")
                if not isinstance(investigator, dict):
                    raise AuthorizationError(
                        "optimizer author input provenance is invalid"
                    )
                expected_hypothesis_id = investigator.get("hypothesis_id")
        elif plan.role == "critic":
            expected_hypothesis_id = primitive.get("hypothesis_id")

        if expected_hypothesis_id is not None and not isinstance(
            expected_hypothesis_id, str
        ):
            raise AuthorizationError("optimizer role input provenance is invalid")
        return AuthenticatedRoleInputSnapshot(
            role=plan.role,
            iteration=plan.iteration,
            canonical_bytes=canonical_bytes,
            expected_hypothesis_id=expected_hypothesis_id,
            source_bundle=source_bundle,
            candidate_bounds=candidate_bounds,
        )

    def bind_controller_role_input(
        self,
        dynamic_input: object,
        plan: PitOptimizerCallBudget,
        *,
        predecessor_calls: Sequence[PitOptimizerRoleCall] = (),
    ) -> AuthenticatedRoleInputSnapshot:
        """Issue one plan-exclusive snapshot from durable predecessor receipts."""

        snapshot = self._snapshot_role_input(dynamic_input, plan)
        canonical_plan = self.snapshot_call_plan(plan)
        if type(predecessor_calls) is not tuple or any(
            not isinstance(item, PitOptimizerRoleCall) for item in predecessor_calls
        ):
            raise AuthorizationError("optimizer predecessor calls are invalid")
        expected_predecessors: tuple[PitOptimizerCallBudget, ...]
        if canonical_plan.role == "investigator":
            expected_predecessors = self._call_plan_snapshots[
                : canonical_plan.call_index - 1
            ]
        elif canonical_plan.role == "author":
            expected_predecessors = (
                self._call_plan_snapshots[canonical_plan.call_index - 2],
            )
        else:
            expected_predecessors = (
                self._call_plan_snapshots[canonical_plan.call_index - 3],
                self._call_plan_snapshots[canonical_plan.call_index - 2],
            )
        if len(predecessor_calls) != len(expected_predecessors):
            raise AuthorizationError(
                "optimizer role input predecessor lineage is incomplete"
            )
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            for predecessor, expected_plan in zip(
                predecessor_calls,
                expected_predecessors,
                strict=True,
            ):
                if predecessor.plan != expected_plan:
                    raise AuthorizationError(
                        "optimizer role input predecessor plan differs"
                    )
                payload_sha256 = hashlib.sha256(
                    predecessor.payload.canonical_json_bytes()
                ).hexdigest()
                matches = [
                    item
                    for item in records
                    if item.get("record_type") == "reconciliation"
                    and item.get("provider_facts") == asdict(predecessor.facts)
                    and isinstance(item.get("terminal_audit_receipt"), dict)
                    and item["terminal_audit_receipt"].get(
                        "run_manifest_sha256"
                    )
                    == self._manifest.sha256
                    and (
                        item["terminal_audit_receipt"].get("call_index"),
                        item["terminal_audit_receipt"].get("iteration"),
                        item["terminal_audit_receipt"].get("role"),
                        item["terminal_audit_receipt"].get("outcome"),
                        item["terminal_audit_receipt"].get("payload_sha256"),
                    )
                    == (
                        expected_plan.call_index,
                        expected_plan.iteration,
                        expected_plan.role,
                        "accepted",
                        payload_sha256,
                    )
                ]
                if len(matches) != 1:
                    raise AuthorizationError(
                        "optimizer role input predecessor receipt is absent"
                    )
                reconciliation = matches[0]
                receipt_primitive = reconciliation.get(
                    "terminal_audit_receipt"
                )
                reservation_id = reconciliation.get("reservation_id")
                reservation_record = next(
                    (
                        item
                        for item in records
                        if item.get("record_type") == "reservation"
                        and isinstance(item.get("reservation"), dict)
                        and item["reservation"].get("reservation_id")
                        == reservation_id
                    ),
                    None,
                )
                try:
                    if not isinstance(receipt_primitive, dict):
                        raise TypeError("receipt is not a mapping")
                    if reservation_record is None:
                        raise TypeError("reservation is absent")
                    receipt = TerminalAuditReceipt(**receipt_primitive)
                    durable_reservation = AuthorizationCallReservation(
                        **reservation_record["reservation"]
                    )
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError(
                        "optimizer role input predecessor receipt is invalid"
                    ) from exc
                self._cross_verify_audit_receipt(
                    receipt,
                    durable_reservation,
                    predecessor.facts,
                    records=records,
                )
        primitive = json.loads(
            snapshot.canonical_bytes,
            object_pairs_hook=_reject_duplicate_keys,
        )
        if canonical_plan.role == "investigator" and canonical_plan.iteration > 1:
            feedbacks = primitive.get("prior_iterations")
            if (
                not isinstance(feedbacks, list)
                or len(feedbacks) * len(OPTIMIZER_V2_ROLES)
                != len(predecessor_calls)
            ):
                raise AuthorizationError(
                    "optimizer role input predecessor artifact differs"
                )
            for offset, feedback in enumerate(feedbacks):
                investigator_call, author_call, critic_call = predecessor_calls[
                    offset * len(OPTIMIZER_V2_ROLES) :
                    (offset + 1) * len(OPTIMIZER_V2_ROLES)
                ]
                investigator_payload = investigator_call.payload
                author_payload = author_call.payload
                critic_payload = critic_call.payload
                assert isinstance(investigator_payload, InvestigatorArtifact)
                assert isinstance(author_payload, AuthorArtifact)
                assert isinstance(critic_payload, CriticArtifact)
                if not isinstance(feedback, dict) or (
                    feedback.get("iteration"),
                    feedback.get("hypothesis_id"),
                    feedback.get("family"),
                    feedback.get("author_summary"),
                    feedback.get("critic_disposition"),
                    feedback.get("critic_next_direction"),
                ) != (
                    offset + 1,
                    investigator_payload.hypothesis_id,
                    investigator_payload.family,
                    author_payload.behavioral_summary,
                    critic_payload.disposition,
                    critic_payload.next_direction,
                ):
                    raise AuthorizationError(
                        "optimizer role input predecessor artifact differs"
                    )
        elif canonical_plan.role == "author":
            if primitive.get("investigator") != predecessor_calls[0].payload.to_primitive():
                raise AuthorizationError(
                    "optimizer role input predecessor artifact differs"
                )
        elif canonical_plan.role == "critic":
            investigator_call, author_call = predecessor_calls
            author_payload = author_call.payload
            assert isinstance(author_payload, AuthorArtifact)
            expected_author_manifest = {
                "hypothesis_id": author_payload.hypothesis_id,
                "behavioral_summary": author_payload.behavioral_summary,
                "changed_paths": list(author_payload.changed_paths),
                "changed_symbols": list(author_payload.changed_symbols),
            }
            if (
                primitive.get("investigator_summary")
                != investigator_call.payload.to_primitive()
                or primitive.get("author_manifest") != expected_author_manifest
            ):
                raise AuthorizationError(
                    "optimizer role input predecessor artifact differs"
                )
        with self._role_input_lock:
            if canonical_plan.call_index in self._consumed_role_input_plans:
                raise AuthorizationError("optimizer role input plan was already consumed")
            prior = self._role_input_capabilities.get(canonical_plan.call_index)
            if prior is not None and (
                prior[0] is not dynamic_input
                or prior[1].canonical_bytes != snapshot.canonical_bytes
            ):
                raise AuthorizationError("optimizer role input plan is already bound")
            self._role_input_capabilities[canonical_plan.call_index] = (
                dynamic_input,
                snapshot,
            )
        return snapshot

    def capture_controller_role_input(
        self,
        dynamic_input: object,
        plan: PitOptimizerCallBudget,
    ) -> AuthenticatedRoleInputSnapshot:
        """Reauthenticate an exact snapshot against a live controller capability."""

        canonical_plan = self.snapshot_call_plan(plan)
        with self._role_input_lock:
            if canonical_plan.call_index in self._consumed_role_input_plans:
                raise AuthorizationError("optimizer role input plan was already consumed")
            capability = self._role_input_capabilities.pop(
                canonical_plan.call_index,
                None,
            )
            if capability is None or capability[0] is not dynamic_input:
                raise AuthorizationError(
                    "optimizer role input provenance is not controller authenticated"
                )
            self._consumed_role_input_plans.add(canonical_plan.call_index)
        return capability[1]

    def verify_consumed_role_input_unchanged(
        self,
        dynamic_input: object,
        plan: PitOptimizerCallBudget,
        snapshot: AuthenticatedRoleInputSnapshot,
    ) -> None:
        """Reject mutation of the live source object after snapshot consumption."""

        canonical_plan = self.snapshot_call_plan(plan)
        with self._role_input_lock:
            if (
                canonical_plan.call_index not in self._consumed_role_input_plans
                or not isinstance(snapshot, AuthenticatedRoleInputSnapshot)
            ):
                raise AuthorizationError("optimizer role input snapshot was not consumed")
            current = self._snapshot_role_input(dynamic_input, canonical_plan)
            if current.canonical_bytes != snapshot.canonical_bytes:
                raise AuthorizationError("optimizer role input changed after authentication")

    @staticmethod
    def _record_digest(record: dict[str, object]) -> str:
        return _authorization_record_digest(record)

    @staticmethod
    def _gateway_lifecycle_base(
        value: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            key: item
            for key, item in value.items()
            if key != "start_event_sha256"
        }

    def _parse_gateway_lifecycle(
        self,
        value: object,
        *,
        reservation: AuthorizationCallReservation,
        lease: AuthorizationRunLease,
    ) -> dict[str, object]:
        expected_keys = {
            "authorization_reservation_id",
            "authorization_reservation_sha256",
            "lease_id",
            "call_plan",
            "call_plan_sha256",
            "run_manifest_sha256",
            "audit_run_id",
            "budget_reservation_id",
            "budget_reservation_sha256",
            "reservation_event_sha256",
            "start_event_sha256",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise AuthorizationError(
                "authorization gateway lifecycle commitment is malformed"
            )
        primitive = dict(value)
        plan_primitive = primitive.get("call_plan")
        try:
            if not isinstance(plan_primitive, dict):
                raise TypeError("plan is not a mapping")
            plan = PitOptimizerCallBudget(**plan_primitive)
            authorization_digest = _require_digest(
                primitive.get("authorization_reservation_sha256"),
                "authorization reservation commitment SHA-256",
            )
            call_plan_digest = _require_digest(
                primitive.get("call_plan_sha256"),
                "authorization call plan commitment SHA-256",
            )
            run_manifest_sha256 = _require_digest(
                primitive.get("run_manifest_sha256"),
                "authorization lifecycle run manifest SHA-256",
            )
            budget_digest = _require_digest(
                primitive.get("budget_reservation_sha256"),
                "authorization budget reservation commitment SHA-256",
            )
            reserved_digest = _require_digest(
                primitive.get("reservation_event_sha256"),
                "authorization reserved event commitment SHA-256",
            )
            started_value = primitive.get("start_event_sha256")
            started_digest = (
                None
                if started_value is None
                else _require_digest(
                    started_value,
                    "authorization started event commitment SHA-256",
                )
            )
        except (TypeError, ValueError) as exc:
            raise AuthorizationError(
                "authorization gateway lifecycle commitment is invalid"
            ) from exc
        audit_run_id = primitive.get("audit_run_id")
        budget_reservation_id = primitive.get("budget_reservation_id")
        if (
            primitive.get("authorization_reservation_id")
            != reservation.reservation_id
            or authorization_digest
            != hashlib.sha256(
                _canonical_json_bytes(asdict(reservation))
            ).hexdigest()
            or primitive.get("lease_id") != reservation.lease_id
            or reservation.lease_id != lease.lease_id
            or call_plan_digest
            != hashlib.sha256(plan.canonical_json_bytes()).hexdigest()
            or reservation.call_index > len(self._call_plan_snapshots)
            or plan != self._call_plan_snapshots[reservation.call_index - 1]
            or (
                plan.call_index,
                plan.iteration,
                plan.role,
                plan.max_input_tokens + plan.max_output_tokens,
            )
            != (
                reservation.call_index,
                reservation.iteration,
                reservation.role,
                reservation.reserved_tokens,
            )
            or run_manifest_sha256 != lease.run_manifest_sha256
            or not isinstance(audit_run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", audit_run_id)
            is None
            or audit_run_id != self._manifest.run_id
            or not isinstance(budget_reservation_id, str)
            or re.fullmatch(
                r"optimizer_budget_[0-9a-f]{32}",
                budget_reservation_id,
            )
            is None
        ):
            raise AuthorizationError(
                "authorization gateway lifecycle commitment differs"
            )
        primitive["authorization_reservation_sha256"] = authorization_digest
        primitive["call_plan_sha256"] = call_plan_digest
        primitive["run_manifest_sha256"] = run_manifest_sha256
        primitive["budget_reservation_sha256"] = budget_digest
        primitive["reservation_event_sha256"] = reserved_digest
        primitive["start_event_sha256"] = started_digest
        return primitive

    def _require_gateway_lifecycle_commitment(
        self,
        records: Sequence[dict[str, object]],
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
        *,
        budget_state: Mapping[str, object],
    ) -> dict[str, object]:
        lease_record = next(
            (
                item
                for item in records
                if item.get("record_type") == "lease_open"
                and isinstance(item.get("lease"), dict)
                and item["lease"].get("lease_id") == reservation.lease_id
            ),
            None,
        )
        if lease_record is None:
            raise AuthorizationError("authorization lifecycle lease is absent")
        try:
            lease = AuthorizationRunLease(**lease_record["lease"])
        except (TypeError, ValueError) as exc:
            raise AuthorizationError(
                "authorization lifecycle lease is invalid"
            ) from exc
        commitments = [
            self._parse_gateway_lifecycle(
                item.get("gateway_lifecycle"),
                reservation=reservation,
                lease=lease,
            )
            for item in records
            if item.get("record_type") == "gateway_lifecycle"
            and isinstance(item.get("gateway_lifecycle"), dict)
            and item["gateway_lifecycle"].get(
                "authorization_reservation_id"
            )
            == reservation.reservation_id
        ]
        started_commitments = [
            item
            for item in commitments
            if item.get("start_event_sha256") is not None
        ]
        if provider_facts.request_started != (len(started_commitments) == 1):
            raise AuthorizationError(
                "authorization gateway lifecycle stage differs"
            )
        expected = [
            item
            for item in commitments
            if (item.get("start_event_sha256") is not None)
            is provider_facts.request_started
        ]
        if len(expected) != 1:
            raise AuthorizationError(
                "authorization gateway lifecycle commitment is absent"
            )
        commitment = expected[0]
        lifecycle_digest = (
            commitment["start_event_sha256"]
            or commitment["reservation_event_sha256"]
        )
        reconciliations = budget_state.get("reconciliations")
        budget_matches = (
            [
                item
                for item in reconciliations
                if isinstance(item, dict)
                and isinstance(item.get("reservation"), dict)
                and item["reservation"].get("reservation_id")
                == commitment["budget_reservation_id"]
            ]
            if isinstance(reconciliations, list)
            else []
        )
        budget_reservation = (
            budget_matches[0]["reservation"]
            if len(budget_matches) == 1
            else None
        )
        if (
            provider_facts.audit_sha256 != lifecycle_digest
            or receipt.audit_run_id != commitment["audit_run_id"]
            or receipt.run_manifest_sha256
            != commitment["run_manifest_sha256"]
            or (
                receipt.call_index,
                receipt.iteration,
                receipt.role,
            )
            != (
                reservation.call_index,
                reservation.iteration,
                reservation.role,
            )
            or not isinstance(budget_reservation, dict)
            or hashlib.sha256(
                _canonical_json_bytes(budget_reservation)
            ).hexdigest()
            != commitment["budget_reservation_sha256"]
        ):
            raise AuthorizationError(
                "authorization gateway lifecycle commitment differs"
            )
        return commitment

    @staticmethod
    def _gateway_terminal_commitment(
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
        lifecycle_commitment: Mapping[str, object],
        *,
        budget_recovery_sha256: str,
    ) -> dict[str, object]:
        try:
            budget_digest = _require_digest(
                budget_recovery_sha256,
                "authorization budget recovery SHA-256",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        if not provider_facts.request_started:
            charged_calls = 0
            charged_tokens = 0
            actual_cost_usd: float | None = 0.0
            cost_accounting_status = "not_started"
        elif provider_facts.accounting_complete:
            assert provider_facts.total_tokens is not None
            assert provider_facts.cost_usd is not None
            charged_calls = 1
            charged_tokens = provider_facts.total_tokens
            actual_cost_usd = float(provider_facts.cost_usd)
            cost_accounting_status = "authoritative"
        else:
            charged_calls = 1
            charged_tokens = provider_facts.retained_reservation_tokens
            actual_cost_usd = None
            cost_accounting_status = "unavailable"
        reservation_primitive = asdict(reservation)
        facts_primitive = asdict(provider_facts)
        return {
            "authorization_reservation_id": reservation.reservation_id,
            "authorization_reservation": reservation_primitive,
            "authorization_reservation_sha256": hashlib.sha256(
                _canonical_json_bytes(reservation_primitive)
            ).hexdigest(),
            "gateway_lifecycle_sha256": hashlib.sha256(
                _canonical_json_bytes(dict(lifecycle_commitment))
            ).hexdigest(),
            "provider_facts_sha256": hashlib.sha256(
                _canonical_json_bytes(facts_primitive)
            ).hexdigest(),
            "provider_record_sha256": receipt.provider_record_sha256,
            "budget_recovery_sha256": budget_digest,
            "terminal_event_sha256": receipt.terminal_event_sha256,
            "audit_run_id": receipt.audit_run_id,
            "run_manifest_sha256": receipt.run_manifest_sha256,
            "call_index": receipt.call_index,
            "iteration": receipt.iteration,
            "role": receipt.role,
            "usage": {
                "prompt_tokens": provider_facts.prompt_tokens,
                "completion_tokens": provider_facts.completion_tokens,
                "total_tokens": provider_facts.total_tokens,
                "cost_usd": provider_facts.cost_usd,
                "retained_reservation_tokens": (
                    provider_facts.retained_reservation_tokens
                ),
            },
            "charged_calls": charged_calls,
            "charged_tokens": charged_tokens,
            "actual_cost_usd": actual_cost_usd,
            "cost_accounting_status": cost_accounting_status,
            "terminal_outcome": provider_facts.outcome,
            "terminal_code": receipt.terminal_code,
            "payload_sha256": receipt.payload_sha256,
        }

    def _require_gateway_terminal_commitment(
        self,
        records: Sequence[dict[str, object]],
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
        *,
        budget_state: Mapping[str, object],
    ) -> dict[str, object]:
        lifecycle_commitment = self._require_gateway_lifecycle_commitment(
            records,
            reservation,
            provider_facts,
            receipt,
            budget_state=budget_state,
        )
        matches = [
            item
            for item in records
            if item.get("record_type") == "reconciliation"
            and item.get("reservation_id") == reservation.reservation_id
        ]
        if len(matches) != 1 or not isinstance(
            matches[0].get("gateway_terminal"),
            dict,
        ):
            raise AuthorizationError(
                "authorization gateway terminal audit commitment is absent"
            )
        commitment = matches[0]["gateway_terminal"]
        assert isinstance(commitment, dict)
        expected = self._gateway_terminal_commitment(
            reservation,
            provider_facts,
            receipt,
            lifecycle_commitment,
            budget_recovery_sha256=hashlib.sha256(
                _canonical_json_bytes(dict(budget_state))
            ).hexdigest(),
        )
        if commitment != expected:
            raise AuthorizationError(
                "authorization gateway terminal audit commitment differs"
            )
        self._verify_reconciliation_records(
            records,
            reservation,
            provider_facts,
            receipt.terminal_event_sha256,
            receipt.terminal_code,
            receipt,
        )
        return commitment

    def _require_gateway_terminal_capability(
        self,
        lifecycle: object,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
    ) -> dict[str, object]:
        from agent_loop import OpenRouterGateway, _PitOptimizerGatewayLifecycle

        if not isinstance(lifecycle, _PitOptimizerGatewayLifecycle):
            raise AuthorizationError(
                "authorization gateway terminal lifecycle is required"
            )
        gateway = lifecycle.gateway
        if not isinstance(gateway, OpenRouterGateway):
            raise AuthorizationError(
                "authorization gateway terminal lifecycle is invalid"
            )
        gateway._require_pit_optimizer_lifecycle(lifecycle)
        if (
            lifecycle.authorization_ledger is not self
            or lifecycle.audit_trail is not self._audit_trail
            or lifecycle.authorization_reservation != reservation
            or lifecycle.facts != provider_facts
            or lifecycle.terminal_receipt != receipt
            or not isinstance(lifecycle.budget_state, dict)
        ):
            raise AuthorizationError(
                "authorization gateway terminal lifecycle differs"
            )
        return dict(lifecycle.budget_state)

    def _commit_gateway_terminal_reconciliation(
        self,
        lifecycle: object,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
        *,
        terminal_code: str | None,
    ) -> None:
        """Atomically commit the gateway terminal package and reconciliation."""

        from agent_loop import _PitOptimizerGatewayLifecycle

        if not isinstance(lifecycle, _PitOptimizerGatewayLifecycle):
            raise AuthorizationError(
                "authorization gateway terminal lifecycle is required"
            )
        self._require_gateway_terminal_capability(
            lifecycle,
            lifecycle.authorization_reservation,
            provider_facts,
            receipt,
        )
        self.reconcile_call(
            lifecycle.authorization_reservation,
            provider_facts,
            terminal_audit_receipt=receipt,
            terminal_code=terminal_code,
            _gateway_lifecycle=lifecycle,
        )

    def _bind_gateway_lifecycle_commitment(self, lifecycle: object) -> None:
        """Durably bind one gateway-owned reserved/started transition."""

        from agent_loop import (
            OpenRouterGateway,
            PitOptimizerResourceLedger,
            _PitOptimizerGatewayLifecycle,
        )

        if not isinstance(lifecycle, _PitOptimizerGatewayLifecycle):
            raise AuthorizationError(
                "authorization gateway lifecycle capability is invalid"
            )
        gateway = lifecycle.gateway
        if (
            not isinstance(gateway, OpenRouterGateway)
            or lifecycle.authorization_ledger is not self
            or lifecycle.reserved_event_sha256 is None
        ):
            raise AuthorizationError(
                "authorization gateway lifecycle capability is invalid"
            )
        gateway._require_pit_optimizer_lifecycle(lifecycle)
        plan = self.snapshot_call_plan(lifecycle.call_budget)
        reservation = lifecycle.authorization_reservation
        if not isinstance(reservation, AuthorizationCallReservation):
            raise AuthorizationError(
                "authorization gateway lifecycle reservation is invalid"
            )
        budget_primitive = (
            PitOptimizerResourceLedger._pit_optimizer_reservation_primitive(
                lifecycle.budget_reservation
            )
        )
        commitment = {
            "authorization_reservation_id": reservation.reservation_id,
            "authorization_reservation_sha256": hashlib.sha256(
                _canonical_json_bytes(asdict(reservation))
            ).hexdigest(),
            "lease_id": reservation.lease_id,
            "call_plan": plan.to_primitive(),
            "call_plan_sha256": hashlib.sha256(
                plan.canonical_json_bytes()
            ).hexdigest(),
            "run_manifest_sha256": lifecycle.authorization_lease.run_manifest_sha256,
            "audit_run_id": lifecycle.audit_trail.run_id,
            "budget_reservation_id": lifecycle.budget_reservation.reservation_id,
            "budget_reservation_sha256": hashlib.sha256(
                _canonical_json_bytes(budget_primitive)
            ).hexdigest(),
            "reservation_event_sha256": lifecycle.reserved_event_sha256,
            "start_event_sha256": lifecycle.started_event_sha256,
        }
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            reservation_record = next(
                (
                    item
                    for item in records
                    if item.get("record_type") == "reservation"
                    and isinstance(item.get("reservation"), dict)
                    and item["reservation"].get("reservation_id")
                    == reservation.reservation_id
                ),
                None,
            )
            try:
                if reservation_record is None:
                    raise TypeError("reservation is absent")
                stored_reservation = AuthorizationCallReservation(
                    **reservation_record["reservation"]
                )
            except (TypeError, ValueError) as exc:
                raise AuthorizationError(
                    "authorization gateway lifecycle reservation is invalid"
                ) from exc
            if stored_reservation != reservation:
                raise AuthorizationError(
                    "authorization gateway lifecycle reservation differs"
                )
            existing = [
                item["gateway_lifecycle"]
                for item in records
                if item.get("record_type") == "gateway_lifecycle"
                and isinstance(item.get("gateway_lifecycle"), dict)
                and item["gateway_lifecycle"].get(
                    "authorization_reservation_id"
                )
                == reservation.reservation_id
            ]
            if commitment in existing:
                return
            same_stage = [
                item
                for item in existing
                if (item.get("start_event_sha256") is not None)
                is (commitment["start_event_sha256"] is not None)
            ]
            if same_stage:
                raise AuthorizationError(
                    "authorization gateway lifecycle commitment changed"
                )
            if commitment["start_event_sha256"] is not None and not any(
                self._gateway_lifecycle_base(item)
                == self._gateway_lifecycle_base(commitment)
                and item.get("start_event_sha256") is None
                for item in existing
            ):
                raise AuthorizationError(
                    "authorization reserved lifecycle commitment is absent"
                )
            self._append_records(
                records,
                [
                    {
                        "record_type": "gateway_lifecycle",
                        "gateway_lifecycle": commitment,
                    }
                ],
            )

    def _read_records(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        if self._path.is_symlink() or not self._path.is_file():
            raise AuthorizationError("authorization ledger must be a regular non-link file")
        raw = self._path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise AuthorizationError("authorization ledger has a partial record")
        records: list[dict[str, object]] = []
        previous = _ZERO_SHA256
        grants: dict[str, OperatorAuthorizationGrant] = {}
        windows: dict[str, OperatorAuthorizationWindow] = {}
        pricing_snapshots: dict[str, OptimizerPricingSnapshot] = {}
        leases: dict[str, AuthorizationRunLease] = {}
        one_shot_keys: set[str] = set()
        active_reservations: dict[str, AuthorizationCallReservation] = {}
        reconciled_reservations: set[str] = set()
        closed_leases: set[str] = set()
        charged_by_lease: dict[str, tuple[int, int]] = {}
        overage_leases: set[str] = set()
        reconciliations_by_lease: dict[
            str,
            list[tuple[AuthorizationCallReservation, PitOptimizerProviderFacts]],
        ] = {}
        pending_terminal_closes: dict[str, frozenset[str]] = {}
        gateway_lifecycles: dict[str, list[dict[str, object]]] = {}
        for index, line in enumerate(raw.splitlines(keepends=True), start=1):
            try:
                value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, AuthorizationError) as exc:
                raise AuthorizationError("authorization ledger contains invalid JSON") from exc
            if not isinstance(value, dict) or line != _canonical_json_bytes(value):
                raise AuthorizationError("authorization ledger record is not canonical JSON")
            if value.get("schema_version") == 2:
                raise AuthorizationError(
                    "schema-v2 authorization history is not resumable; "
                    "use read_legacy_authorization_history"
                )
            if value.get("schema_version") != 3 or value.get("record_index") != index:
                raise AuthorizationError("authorization ledger record index is invalid")
            if value.get("previous_record_sha256") != previous:
                raise AuthorizationError("authorization ledger hash chain is broken")
            digest = value.get("record_sha256")
            try:
                _require_digest(digest, "authorization ledger record SHA-256")
            except ValueError as exc:
                raise AuthorizationError(str(exc)) from exc
            if digest != self._record_digest(value):
                raise AuthorizationError("authorization ledger record digest differs")
            record_type = value.get("record_type")
            if pending_terminal_closes:
                if len(pending_terminal_closes) != 1:
                    raise AuthorizationError(
                        "authorization terminal reconciliation state is ambiguous"
                    )
                pending_lease_id = next(iter(pending_terminal_closes))
                if (
                    record_type != "lease_close"
                    or value.get("lease_id") != pending_lease_id
                ):
                    raise AuthorizationError(
                        "authorization terminal reconciliation is not closed"
                    )
            expected_common = {
                "schema_version",
                "record_type",
                "record_index",
                "previous_record_sha256",
                "record_sha256",
            }
            if record_type == "grant":
                if set(value) != expected_common | {
                    "grant",
                    "operator_approval_reference_sha256",
                }:
                    raise AuthorizationError("authorization grant record keys are invalid")
                primitive = value.get("grant")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization grant record is malformed")
                try:
                    grant = OperatorAuthorizationGrant(**primitive)
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError("authorization grant record is invalid") from exc
                requirement = self._manifest.authorization_requirement
                if (
                    grant.policy_source_scope_sha256
                    != self._manifest.policy_source_scope.sha256
                    or grant.additional_calls > requirement.max_calls
                    or grant.additional_tokens > requirement.max_tokens
                ):
                    raise AuthorizationError(
                        "authorization grant differs from manifest authority"
                    )
                if grant.grant_id in grants:
                    raise AuthorizationError("authorization grant ID is already recorded")
                grants[grant.grant_id] = grant
            elif record_type == "window":
                if set(value) != expected_common | {
                    "window",
                    "operator_approval_reference_sha256",
                }:
                    raise AuthorizationError("authorization window record keys are invalid")
                primitive = value.get("window")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization window record is malformed")
                primitive = dict(primitive)
                primitive["grant_ids"] = tuple(primitive.get("grant_ids", ()))
                try:
                    window = OperatorAuthorizationWindow(**primitive)
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError("authorization window record is invalid") from exc
                if window.window_id in windows:
                    raise AuthorizationError("authorization window ID is already recorded")
                if any(grant_id not in grants for grant_id in window.grant_ids):
                    raise AuthorizationError("authorization window names an absent grant")
                selected = tuple(grants[grant_id] for grant_id in window.grant_ids)
                requirement = self._manifest.authorization_requirement
                if any(
                    grant.policy_source_scope_sha256
                    != window.policy_source_scope_sha256
                    for grant in selected
                ):
                    raise AuthorizationError("authorization window grant scope differs")
                if (
                    window.max_calls
                    > sum(grant.additional_calls for grant in selected)
                    or window.max_tokens
                    > sum(grant.additional_tokens for grant in selected)
                    or window.window_id != requirement.window_id
                    or window.authorization_requirement_sha256
                    != requirement.sha256
                    or window.policy_source_scope_sha256
                    != requirement.policy_source_scope_sha256
                    or window.max_calls > requirement.max_calls
                    or window.max_tokens > requirement.max_tokens
                ):
                    raise AuthorizationError("authorization window exceeds named grants")
                windows[window.window_id] = window
            elif record_type == "pricing_snapshot":
                if set(value) != expected_common | {"pricing_snapshot"}:
                    raise AuthorizationError(
                        "authorization pricing snapshot record keys are invalid"
                    )
                try:
                    snapshot = _pricing_snapshot_from_primitive(
                        value.get("pricing_snapshot")
                    )
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise AuthorizationError(
                        "authorization pricing snapshot record is invalid"
                    ) from exc
                if snapshot.pricing_payload_sha256 in pricing_snapshots:
                    raise AuthorizationError(
                        "authorization pricing snapshot is already recorded"
                    )
                pricing_snapshots[snapshot.pricing_payload_sha256] = snapshot
            elif record_type == "lease_open":
                if set(value) != expected_common | {"lease"}:
                    raise AuthorizationError("authorization lease record keys are invalid")
                primitive = value.get("lease")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization lease record is malformed")
                try:
                    lease = AuthorizationRunLease(**primitive)
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError("authorization lease record is invalid") from exc
                if lease.lease_id in leases or lease.one_shot_key_sha256 in one_shot_keys:
                    raise AuthorizationError("authorization one-shot lease is repeated")
                if lease.window_id not in windows:
                    raise AuthorizationError("authorization lease window is absent")
                snapshot = pricing_snapshots.get(lease.pricing_snapshot_sha256)
                expected_one_shot_key = hashlib.sha256(
                    _canonical_json_bytes(
                        {
                            "window_id": lease.window_id,
                            "run_manifest_sha256": lease.run_manifest_sha256,
                        }
                    )
                ).hexdigest()
                if (
                    snapshot is None
                    or snapshot.lookup_status != lease.pricing_status
                    or snapshot.model != self._manifest.model
                    or lease.run_manifest_sha256 != self._manifest.sha256
                    or lease.one_shot_key_sha256 != expected_one_shot_key
                    or not records
                    or records[-1].get("record_type") != "pricing_snapshot"
                    or not isinstance(records[-1].get("pricing_snapshot"), dict)
                    or records[-1]["pricing_snapshot"].get(
                        "pricing_payload_sha256"
                    )
                    != lease.pricing_snapshot_sha256
                ):
                    raise AuthorizationError(
                        "authorization lease pricing or manifest identity differs"
                    )
                window = windows[lease.window_id]
                grant_calls, grant_tokens = self._remaining_grant_capacity(
                    records,
                    window.grant_ids,
                )
                requirement = self._manifest.authorization_requirement
                if (
                    lease.max_calls
                    != min(requirement.max_calls, window.max_calls, grant_calls)
                    or lease.max_tokens
                    != min(requirement.max_tokens, window.max_tokens, grant_tokens)
                ):
                    raise AuthorizationError(
                        "authorization lease exceeds its window"
                    )
                leases[lease.lease_id] = lease
                one_shot_keys.add(lease.one_shot_key_sha256)
            elif record_type == "reservation":
                if set(value) != expected_common | {"reservation"}:
                    raise AuthorizationError("authorization reservation record keys are invalid")
                primitive = value.get("reservation")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization reservation record is malformed")
                try:
                    reservation = AuthorizationCallReservation(**primitive)
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError("authorization reservation record is invalid") from exc
                if reservation.lease_id not in leases:
                    raise AuthorizationError("authorization reservation lease is absent")
                lease = leases[reservation.lease_id]
                prior_reservations = [
                    record
                    for record in records
                    if record.get("record_type") == "reservation"
                    and isinstance(record.get("reservation"), dict)
                    and record["reservation"].get("lease_id")
                    == reservation.lease_id
                ]
                if (
                    (reservation.projected_call_usd is not None)
                    != (lease.pricing_status == "available")
                    or reservation.call_index > len(self._call_plan_snapshots)
                    or reservation.call_index != len(prior_reservations) + 1
                ):
                    raise AuthorizationError(
                        "authorization reservation pricing or plan is invalid"
                    )
                plan = self._call_plan_snapshots[reservation.call_index - 1]
                if (
                    reservation.call_index,
                    reservation.iteration,
                    reservation.role,
                    reservation.reserved_tokens,
                ) != (
                    plan.call_index,
                    plan.iteration,
                    plan.role,
                    plan.max_input_tokens + plan.max_output_tokens,
                ):
                    raise AuthorizationError(
                        "authorization reservation differs from sealed plan"
                    )
                if reservation.lease_id in closed_leases:
                    raise AuthorizationError("closed authorization lease has a reservation")
                if reservation.lease_id in active_reservations:
                    raise AuthorizationError("authorization lease has concurrent reservations")
                if (
                    reservation.reservation_id in reconciled_reservations
                    or any(
                        item.reservation_id == reservation.reservation_id
                        for item in active_reservations.values()
                    )
                ):
                    raise AuthorizationError("authorization reservation ID is repeated")
                active_reservations[reservation.lease_id] = reservation
            elif record_type == "gateway_lifecycle":
                if set(value) != expected_common | {"gateway_lifecycle"}:
                    raise AuthorizationError(
                        "authorization gateway lifecycle record keys are invalid"
                    )
                primitive = value.get("gateway_lifecycle")
                reservation_id = (
                    primitive.get("authorization_reservation_id")
                    if isinstance(primitive, dict)
                    else None
                )
                match = next(
                    (
                        (lease_id, reservation)
                        for lease_id, reservation in active_reservations.items()
                        if reservation.reservation_id == reservation_id
                    ),
                    None,
                )
                if match is None:
                    raise AuthorizationError(
                        "authorization gateway lifecycle reservation is absent"
                    )
                lease_id, reservation = match
                commitment = self._parse_gateway_lifecycle(
                    primitive,
                    reservation=reservation,
                    lease=leases[lease_id],
                )
                existing = gateway_lifecycles.setdefault(
                    reservation.reservation_id,
                    [],
                )
                started = commitment.get("start_event_sha256") is not None
                if any(
                    (item.get("start_event_sha256") is not None) is started
                    for item in existing
                ):
                    raise AuthorizationError(
                        "authorization gateway lifecycle stage is repeated"
                    )
                if started and not any(
                    self._gateway_lifecycle_base(item)
                    == self._gateway_lifecycle_base(commitment)
                    and item.get("start_event_sha256") is None
                    for item in existing
                ):
                    raise AuthorizationError(
                        "authorization reserved lifecycle commitment is absent"
                    )
                existing.append(commitment)
            elif record_type == "reconciliation":
                expected = expected_common | {
                    "reservation_id",
                    "provider_facts",
                    "charged_calls",
                    "charged_tokens",
                    "actual_cost_usd",
                    "cost_accounting_status",
                }
                if frozenset(value) not in {
                    frozenset(expected | {"terminal_audit_sha256"}),
                    frozenset(
                        expected
                        | {
                            "terminal_audit_sha256",
                            "terminal_audit_receipt",
                        }
                    ),
                    frozenset(
                        expected
                        | {
                            "terminal_audit_sha256",
                            "terminal_audit_receipt",
                            "gateway_terminal",
                        }
                    ),
                }:
                    raise AuthorizationError("authorization reconciliation keys are invalid")
                terminal_audit_sha256 = value.get("terminal_audit_sha256")
                try:
                    _require_digest(
                        terminal_audit_sha256,
                        "authorization terminal audit SHA-256",
                    )
                except ValueError as exc:
                    raise AuthorizationError(str(exc)) from exc
                terminal_audit_receipt = value.get("terminal_audit_receipt")
                lifecycle_commitment: dict[str, object] | None = None
                if terminal_audit_receipt is not None:
                    if not isinstance(terminal_audit_receipt, dict):
                        raise AuthorizationError(
                            "authorization terminal audit receipt is malformed"
                        )
                    try:
                        replayed_receipt = TerminalAuditReceipt(
                            **terminal_audit_receipt
                        )
                    except (TypeError, ValueError) as exc:
                        raise AuthorizationError(
                            "authorization terminal audit receipt is invalid"
                        ) from exc
                    if (
                        replayed_receipt.terminal_event_sha256
                        != terminal_audit_sha256
                    ):
                        raise AuthorizationError(
                            "authorization terminal audit receipt digest differs"
                        )
                reservation_id = value.get("reservation_id")
                match = next(
                    (
                        (lease_id, reservation)
                        for lease_id, reservation in active_reservations.items()
                        if reservation.reservation_id == reservation_id
                    ),
                    None,
                )
                if match is None:
                    raise AuthorizationError("authorization reconciliation reservation is absent")
                lease_id, reservation = match
                primitive = value.get("provider_facts")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization provider facts are malformed")
                try:
                    facts = PitOptimizerProviderFacts(**primitive)
                except (TypeError, ValueError) as exc:
                    raise AuthorizationError("authorization provider facts are invalid") from exc
                if (
                    (facts.call_index, facts.iteration, facts.role)
                    != (reservation.call_index, reservation.iteration, reservation.role)
                    or facts.requested_model
                    != self._call_plan_snapshots[facts.call_index - 1].model
                    or facts.pricing_snapshot_sha256
                    != leases[lease_id].pricing_snapshot_sha256
                ):
                    raise AuthorizationError("authorization provider facts differ from reservation")
                if terminal_audit_receipt is not None:
                    lease = leases[lease_id]
                    if (
                        replayed_receipt.run_manifest_sha256
                        != lease.run_manifest_sha256
                        or (
                            replayed_receipt.call_index,
                            replayed_receipt.iteration,
                            replayed_receipt.role,
                            replayed_receipt.outcome,
                        )
                        != (
                            reservation.call_index,
                            reservation.iteration,
                            reservation.role,
                            facts.outcome,
                        )
                    ):
                        raise AuthorizationError(
                            "authorization terminal audit receipt differs from reservation"
                        )
                    commitments = gateway_lifecycles.get(
                        reservation.reservation_id,
                        [],
                    )
                    started_commitments = [
                        item
                        for item in commitments
                        if item.get("start_event_sha256") is not None
                    ]
                    if facts.request_started != (len(started_commitments) == 1):
                        raise AuthorizationError(
                            "authorization gateway lifecycle stage differs"
                        )
                    expected_commitments = [
                        item
                        for item in commitments
                        if (item.get("start_event_sha256") is not None)
                        is facts.request_started
                    ]
                    if len(expected_commitments) != 1:
                        raise AuthorizationError(
                            "authorization gateway lifecycle commitment is absent"
                        )
                    commitment = expected_commitments[0]
                    lifecycle_commitment = commitment
                    if (
                        facts.audit_sha256
                        != (
                            commitment["start_event_sha256"]
                            or commitment["reservation_event_sha256"]
                        )
                        or replayed_receipt.audit_run_id
                        != commitment["audit_run_id"]
                        or replayed_receipt.run_manifest_sha256
                        != commitment["run_manifest_sha256"]
                    ):
                        raise AuthorizationError(
                            "authorization gateway lifecycle commitment differs"
                        )
                if not facts.request_started:
                    expected_charge = (0, 0, 0.0, "not_started")
                elif facts.accounting_complete:
                    assert facts.total_tokens is not None
                    assert facts.cost_usd is not None
                    expected_charge = (
                        1,
                        facts.total_tokens,
                        float(facts.cost_usd),
                        "authoritative",
                    )
                else:
                    expected_charge = (
                        1,
                        facts.retained_reservation_tokens,
                        None,
                        "unavailable",
                    )
                actual_charge = (
                    value.get("charged_calls"),
                    value.get("charged_tokens"),
                    value.get("actual_cost_usd"),
                    value.get("cost_accounting_status"),
                )
                if actual_charge != expected_charge:
                    raise AuthorizationError("authorization reconciliation charge is invalid")
                prior_calls, prior_tokens = charged_by_lease.get(lease_id, (0, 0))
                charged_calls = int(actual_charge[0])
                charged_tokens = int(actual_charge[1])
                total_calls = prior_calls + charged_calls
                total_tokens = prior_tokens + charged_tokens
                charged_by_lease[lease_id] = (total_calls, total_tokens)
                lease = leases[lease_id]
                if (
                    charged_tokens > reservation.reserved_tokens
                    or total_calls > lease.max_calls
                    or total_tokens > lease.max_tokens
                ):
                    overage_leases.add(lease_id)
                resource_overage = lease_id in overage_leases
                terminal_commitment = value.get("gateway_terminal")
                if terminal_commitment is not None:
                    if (
                        not isinstance(terminal_commitment, dict)
                        or terminal_audit_receipt is None
                        or lifecycle_commitment is None
                    ):
                        raise AuthorizationError(
                            "authorization gateway terminal audit commitment is malformed"
                        )
                    try:
                        budget_recovery_sha256 = _require_digest(
                            terminal_commitment.get("budget_recovery_sha256"),
                            "authorization budget recovery SHA-256",
                        )
                    except ValueError as exc:
                        raise AuthorizationError(str(exc)) from exc
                    expected_terminal = self._gateway_terminal_commitment(
                        reservation,
                        facts,
                        replayed_receipt,
                        lifecycle_commitment,
                        budget_recovery_sha256=budget_recovery_sha256,
                    )
                    if terminal_commitment != expected_terminal:
                        raise AuthorizationError(
                            "authorization gateway terminal audit commitment differs"
                        )
                reconciliations_by_lease.setdefault(lease_id, []).append(
                    (reservation, facts)
                )
                receipt_terminal_code = (
                    replayed_receipt.terminal_code
                    if terminal_audit_receipt is not None
                    else None
                )
                if resource_overage:
                    if receipt_terminal_code not in {None, "budget_exhausted"}:
                        raise AuthorizationError(
                            "authorization resource overage terminal receipt differs"
                        )
                    pending_terminal_closes[lease_id] = frozenset(
                        {"budget_exhausted"}
                    )
                elif receipt_terminal_code is not None:
                    pending_terminal_closes[lease_id] = frozenset(
                        {receipt_terminal_code}
                    )
                elif facts.outcome != "accepted":
                    # Without a terminal receipt, an explicit caller-supplied
                    # failed/cancelled/budget code is not otherwise persisted.
                    pending_terminal_closes[lease_id] = frozenset(
                        {"failed", "cancelled", "budget_exhausted"}
                    )
                del active_reservations[lease_id]
                reconciled_reservations.add(reservation.reservation_id)
            elif record_type == "lease_close":
                if set(value) != expected_common | {"lease_id", "terminal_code"}:
                    raise AuthorizationError("authorization lease close keys are invalid")
                lease_id = value.get("lease_id")
                if lease_id not in leases or lease_id in closed_leases:
                    raise AuthorizationError("authorization lease close is invalid")
                if lease_id in active_reservations:
                    raise AuthorizationError("authorization lease closed with an active reservation")
                terminal_code = value.get("terminal_code")
                if terminal_code not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "early_stop",
                    "budget_exhausted",
                }:
                    raise AuthorizationError("authorization lease terminal code is invalid")
                if lease_id in overage_leases and terminal_code != "budget_exhausted":
                    raise AuthorizationError(
                        "authorization resource overage is not fail-closed"
                    )
                expected_terminal_codes = pending_terminal_closes.get(str(lease_id))
                if (
                    expected_terminal_codes is not None
                    and terminal_code not in expected_terminal_codes
                ):
                    raise AuthorizationError(
                        "authorization terminal reconciliation close differs"
                    )
                if terminal_code in {"completed", "early_stop"}:
                    reconciliations = reconciliations_by_lease.get(str(lease_id), [])
                    for offset, (reservation, facts) in enumerate(reconciliations):
                        if offset >= len(self._call_plan_snapshots):
                            raise AuthorizationError(
                                "authorization completed plan has excess calls"
                            )
                        plan = self._call_plan_snapshots[offset]
                        if (
                            reservation.call_index,
                            reservation.iteration,
                            reservation.role,
                        ) != (plan.call_index, plan.iteration, plan.role) or not (
                            self._provider_facts_accept_plan(facts, plan)
                        ):
                            raise AuthorizationError(
                                "authorization completed plan has a non-accepted call"
                            )
                    if terminal_code == "completed" and len(reconciliations) != len(
                        self._call_plan_snapshots
                    ):
                        raise AuthorizationError(
                            "authorization completed lease requires the exact call plan"
                        )
                    if terminal_code == "early_stop" and not (
                        0 < len(reconciliations) < len(self._call_plan_snapshots)
                    ):
                        raise AuthorizationError(
                            "authorization early stop requires an accepted partial plan"
                        )
                pending_terminal_closes.pop(str(lease_id), None)
                closed_leases.add(str(lease_id))
            else:
                raise AuthorizationError("authorization ledger record type is invalid")
            if record_type in {"grant", "window"}:
                approval_digest = value.get("operator_approval_reference_sha256")
                try:
                    _require_digest(approval_digest, "operator approval reference SHA-256")
                except ValueError as exc:
                    raise AuthorizationError(str(exc)) from exc
            previous = str(digest)
            records.append(value)
        if set(pricing_snapshots) != {
            lease.pricing_snapshot_sha256 for lease in leases.values()
        }:
            raise AuthorizationError(
                "authorization pricing snapshot is not bound to exactly one lease"
            )
        if not overage_leases.issubset(closed_leases):
            raise AuthorizationError(
                "authorization resource overage has an open lease"
            )
        if pending_terminal_closes:
            raise AuthorizationError(
                "authorization terminal reconciliation is not closed"
            )
        return records

    def _build_record(
        self,
        records: list[dict[str, object]],
        primitive: dict[str, object],
    ) -> dict[str, object]:
        record = {
            "schema_version": 3,
            "record_index": len(records) + 1,
            "previous_record_sha256": (
                _ZERO_SHA256 if not records else records[-1]["record_sha256"]
            ),
            **primitive,
        }
        record["record_sha256"] = self._record_digest(record)
        return record

    def _append_records(
        self,
        records: list[dict[str, object]],
        primitives: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        appended: list[dict[str, object]] = []
        working = list(records)
        for primitive in primitives:
            record = self._build_record(working, primitive)
            appended.append(record)
            working.append(record)
        expected_existing = b"".join(
            _canonical_json_bytes(record) for record in records
        )
        actual_existing = self._path.read_bytes() if self._path.exists() else b""
        if actual_existing != expected_existing:
            raise AuthorizationError("authorization ledger changed during append")
        payload = b"".join(_canonical_json_bytes(record) for record in working)
        temporary = self._path.with_name(
            f".auth-{os.urandom(4).hex()}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                self._write_all(handle, payload)
                if handle.tell() != len(payload):
                    raise OSError("authorization ledger write was incomplete")
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.read_bytes() != payload:
                raise OSError("authorization ledger write was incomplete")
            os.replace(temporary, self._path)
            if self._path.read_bytes() != payload:
                raise AuthorizationError("authorization ledger atomic replace was incomplete")
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(self._path.parent, directory_flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return appended

    @staticmethod
    def _write_all(handle: object, payload: bytes) -> None:
        """Write every byte or fail without publishing the temporary ledger."""

        offset = 0
        while offset < len(payload):
            written = handle.write(payload[offset:])  # type: ignore[attr-defined]
            if (
                type(written) is not int
                or written <= 0
                or written > len(payload) - offset
            ):
                raise OSError("authorization ledger write was incomplete")
            offset += written

    def _validate_grant(
        self,
        grant: OperatorAuthorizationGrant,
        records: list[dict[str, object]],
    ) -> None:
        if not isinstance(grant, OperatorAuthorizationGrant):
            raise AuthorizationError("authorization grant is invalid")
        requirement = self._manifest.authorization_requirement
        if grant.policy_source_scope_sha256 != self._manifest.policy_source_scope.sha256:
            raise AuthorizationError("policy source scope mismatch")
        if (
            grant.additional_calls > requirement.max_calls
            or grant.additional_tokens > requirement.max_tokens
        ):
            raise AuthorizationError("authorization grant exceeds manifest ceilings")
        if any(
            record.get("record_type") == "grant"
            and isinstance(record.get("grant"), dict)
            and record["grant"].get("grant_id") == grant.grant_id
            for record in records
        ):
            raise AuthorizationError("authorization grant ID is already recorded")

    def _validate_window(
        self,
        window: OperatorAuthorizationWindow,
        requirement: AuthorizationRequirement,
        records: list[dict[str, object]],
    ) -> None:
        if not isinstance(window, OperatorAuthorizationWindow):
            raise AuthorizationError("authorization window is invalid")
        if requirement is not self._manifest.authorization_requirement and (
            requirement != self._manifest.authorization_requirement
        ):
            raise AuthorizationError("authorization requirement mismatch")
        require_authorized_policy_source_scope(self._manifest, requirement, window)
        if window.window_id != requirement.window_id:
            raise AuthorizationError("authorization window ID mismatch")
        if any(
            record.get("record_type") == "window"
            and isinstance(record.get("window"), dict)
            and record["window"].get("window_id") == window.window_id
            for record in records
        ):
            raise AuthorizationError("authorization window ID is already recorded")
        grants = {
            str(record["grant"]["grant_id"]): OperatorAuthorizationGrant(
                **record["grant"]
            )
            for record in records
            if record.get("record_type") == "grant"
            and isinstance(record.get("grant"), dict)
        }
        try:
            selected = tuple(grants[grant_id] for grant_id in window.grant_ids)
        except KeyError as exc:
            raise AuthorizationError("authorization window names an absent grant") from exc
        if any(
            grant.policy_source_scope_sha256 != window.policy_source_scope_sha256
            for grant in selected
        ):
            raise AuthorizationError("policy source scope mismatch")
        grant_calls, grant_tokens = self._remaining_grant_capacity(
            records,
            window.grant_ids,
        )
        if (
            window.max_calls > min(requirement.max_calls, grant_calls)
            or window.max_tokens > min(requirement.max_tokens, grant_tokens)
        ):
            raise AuthorizationError("authorization window exceeds effective ceilings")

    @classmethod
    def _remaining_grant_capacity(
        cls,
        records: Sequence[dict[str, object]],
        grant_ids: Sequence[str],
    ) -> tuple[int, int]:
        """Return conservative remaining capacity for an explicitly named grant pool."""

        selected_ids = set(grant_ids)
        grants = {
            str(record["grant"]["grant_id"]): OperatorAuthorizationGrant(
                **record["grant"]
            )
            for record in records
            if record.get("record_type") == "grant"
            and isinstance(record.get("grant"), dict)
            and record["grant"].get("grant_id") in selected_ids
        }
        if set(grants) != selected_ids:
            raise AuthorizationError("authorization window names an absent grant")
        windows = {
            str(record["window"]["window_id"]): tuple(
                record["window"].get("grant_ids", ())
            )
            for record in records
            if record.get("record_type") == "window"
            and isinstance(record.get("window"), dict)
        }
        relevant_window_ids = {
            window_id
            for window_id, named_grants in windows.items()
            if selected_ids.intersection(named_grants)
        }
        relevant_lease_ids = {
            str(record["lease"]["lease_id"])
            for record in records
            if record.get("record_type") == "lease_open"
            and isinstance(record.get("lease"), dict)
            and record["lease"].get("window_id") in relevant_window_ids
        }
        reservation_ids = {
            str(record["reservation"]["reservation_id"])
            for record in records
            if record.get("record_type") == "reservation"
            and isinstance(record.get("reservation"), dict)
            and record["reservation"].get("lease_id") in relevant_lease_ids
        }
        spent_calls = 0
        spent_tokens = 0
        for record in records:
            if (
                record.get("record_type") != "reconciliation"
                or record.get("reservation_id") not in reservation_ids
            ):
                continue
            try:
                calls = record["charged_calls"]
                tokens = record["charged_tokens"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AuthorizationError(
                    "authorization reconciliation totals are invalid"
                ) from exc
            if (
                type(calls) is not int
                or calls not in {0, 1}
                or type(tokens) is not int
                or tokens < 0
            ):
                raise AuthorizationError(
                    "authorization reconciliation totals are invalid"
                )
            spent_calls += calls
            spent_tokens += tokens
        total_calls = sum(grant.additional_calls for grant in grants.values())
        total_tokens = sum(grant.additional_tokens for grant in grants.values())
        return (
            max(0, total_calls - spent_calls),
            max(0, total_tokens - spent_tokens),
        )

    def append_grant(
        self,
        grant: OperatorAuthorizationGrant,
        *,
        operator_approval_reference: str,
    ) -> None:
        approval_sha256 = _approval_reference_sha256(operator_approval_reference)
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._validate_grant(grant, records)
            self._append_records(
                records,
                [
                    {
                        "record_type": "grant",
                        "grant": asdict(grant),
                        "operator_approval_reference_sha256": approval_sha256,
                    }
                ],
            )

    def bind_window(
        self,
        *,
        window: OperatorAuthorizationWindow,
        requirement: AuthorizationRequirement,
        operator_approval_reference: str,
    ) -> None:
        approval_sha256 = _approval_reference_sha256(operator_approval_reference)
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._validate_window(window, requirement, records)
            self._append_records(
                records,
                [
                    {
                        "record_type": "window",
                        "window": asdict(window),
                        "operator_approval_reference_sha256": approval_sha256,
                    }
                ],
            )

    def record_grant_and_window(
        self,
        *,
        grant: OperatorAuthorizationGrant,
        window: OperatorAuthorizationWindow,
        requirement: AuthorizationRequirement,
        operator_approval_reference: str,
    ) -> None:
        """Append one grant and its sole window under one exclusive lock."""

        approval_sha256 = _approval_reference_sha256(operator_approval_reference)
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            existing_grant = next(
                (
                    record
                    for record in records
                    if record.get("record_type") == "grant"
                    and isinstance(record.get("grant"), dict)
                    and record["grant"].get("grant_id") == grant.grant_id
                ),
                None,
            )
            existing_window = next(
                (
                    record
                    for record in records
                    if record.get("record_type") == "window"
                    and isinstance(record.get("window"), dict)
                    and record["window"].get("window_id") == window.window_id
                ),
                None,
            )
            if existing_grant is not None or existing_window is not None:
                if (
                    existing_grant is not None
                    and existing_window is not None
                    and _canonical_json_bytes(existing_grant["grant"])
                    == _canonical_json_bytes(asdict(grant))
                    and _canonical_json_bytes(existing_window["window"])
                    == _canonical_json_bytes(asdict(window))
                    and existing_grant.get("operator_approval_reference_sha256")
                    == approval_sha256
                    and existing_window.get("operator_approval_reference_sha256")
                    == approval_sha256
                ):
                    return
                raise AuthorizationError(
                    "authorization grant/window retry differs from recorded authority"
                )
            self._validate_grant(grant, records)
            grant_record = self._build_record(
                records,
                {
                    "record_type": "grant",
                    "grant": asdict(grant),
                    "operator_approval_reference_sha256": approval_sha256,
                },
            )
            self._validate_window(window, requirement, [*records, grant_record])
            self._append_records(
                records,
                [
                    {
                        "record_type": "grant",
                        "grant": asdict(grant),
                        "operator_approval_reference_sha256": approval_sha256,
                    },
                    {
                        "record_type": "window",
                        "window": asdict(window),
                        "operator_approval_reference_sha256": approval_sha256,
                    },
                ],
            )

    def authenticate_window(
        self,
        *,
        window_id: str,
        authorization_requirement_sha256: str,
    ) -> OperatorAuthorizationWindow:
        """Return the exact authenticated window without mutating authority."""

        try:
            _require_id(window_id, "authorization window ID")
            _require_digest(
                authorization_requirement_sha256,
                "authorization requirement SHA-256",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        requirement = self._manifest.authorization_requirement
        if authorization_requirement_sha256 != requirement.sha256:
            raise AuthorizationError("authorization requirement mismatch")
        if window_id != requirement.window_id:
            raise AuthorizationError("authorization window ID mismatch")
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            matches = [
                record
                for record in records
                if record.get("record_type") == "window"
                and isinstance(record.get("window"), dict)
                and record["window"].get("window_id") == window_id
            ]
        if len(matches) != 1:
            raise AuthorizationError("authorization window is absent")
        primitive = dict(matches[0]["window"])
        primitive["grant_ids"] = tuple(primitive.get("grant_ids", ()))
        try:
            window = OperatorAuthorizationWindow(**primitive)
        except (TypeError, ValueError) as exc:
            raise AuthorizationError("authorization window record is invalid") from exc
        require_authorized_policy_source_scope(
            self._manifest,
            requirement,
            window,
        )
        return window

    def open_run_lease(
        self,
        *,
        window_id: str,
        authorization_requirement_sha256: str,
        run_manifest_sha256: str,
        pricing_snapshot: OptimizerPricingSnapshot,
        projected_plan_usd: Decimal | None,
    ) -> AuthorizationRunLease:
        """Atomically acquire the manifest/window once without debiting allowance."""

        try:
            _require_id(window_id, "authorization lease window ID")
            _require_digest(
                authorization_requirement_sha256,
                "authorization lease requirement SHA-256",
            )
            _require_digest(run_manifest_sha256, "authorization lease manifest SHA-256")
            if not isinstance(pricing_snapshot, OptimizerPricingSnapshot):
                raise ValueError("authorization lease pricing snapshot is invalid")
            canonical_pricing = _pricing_snapshot_from_primitive(
                pricing_snapshot.to_primitive()
            )
            if canonical_pricing != pricing_snapshot:
                raise ValueError("authorization lease pricing snapshot differs")
            projected_plan_text = _canonical_optional_decimal(
                projected_plan_usd,
                "authorization lease projected plan USD",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        requirement = self._manifest.authorization_requirement
        if authorization_requirement_sha256 != requirement.sha256:
            raise AuthorizationError("authorization requirement mismatch")
        if run_manifest_sha256 != self._manifest.sha256:
            raise AuthorizationError("authorization run manifest mismatch")
        if canonical_pricing.model != self._manifest.model:
            raise AuthorizationError("authorization pricing model differs from manifest")
        if (
            canonical_pricing.lookup_status == "available"
        ) != (projected_plan_text is not None):
            raise AuthorizationError(
                "authorization pricing availability differs from plan projection"
            )
        one_shot_key = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "window_id": window_id,
                    "run_manifest_sha256": run_manifest_sha256,
                }
            )
        ).hexdigest()
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            window_record = next(
                (
                    record
                    for record in records
                    if record.get("record_type") == "window"
                    and isinstance(record.get("window"), dict)
                    and record["window"].get("window_id") == window_id
                ),
                None,
            )
            if window_record is None:
                raise AuthorizationError("authorization window is absent")
            primitive = dict(window_record["window"])
            primitive["grant_ids"] = tuple(primitive["grant_ids"])
            window = OperatorAuthorizationWindow(**primitive)
            require_authorized_policy_source_scope(self._manifest, requirement, window)
            if any(
                record.get("record_type") == "lease_open"
                and isinstance(record.get("lease"), dict)
                and record["lease"].get("one_shot_key_sha256") == one_shot_key
                for record in records
            ):
                raise AuthorizationError("authorization one-shot run is already consumed")
            windows_by_id = {
                str(record["window"]["window_id"]): tuple(
                    record["window"].get("grant_ids", ())
                )
                for record in records
                if record.get("record_type") == "window"
                and isinstance(record.get("window"), dict)
            }
            closed_lease_ids = {
                str(record.get("lease_id"))
                for record in records
                if record.get("record_type") == "lease_close"
            }
            if any(
                record["lease"].get("lease_id") not in closed_lease_ids
                and set(window.grant_ids).intersection(
                    windows_by_id.get(
                        str(record["lease"].get("window_id")),
                        (),
                    )
                )
                for record in records
                if record.get("record_type") == "lease_open"
                and isinstance(record.get("lease"), dict)
            ):
                raise AuthorizationError("authorization grants have an active run lease")
            grant_calls, grant_tokens = self._remaining_grant_capacity(
                records,
                window.grant_ids,
            )
            planned_calls = len(self._manifest.call_budgets)
            planned_tokens = sum(
                item.max_input_tokens + item.max_output_tokens
                for item in self._manifest.call_budgets
            )
            if (
                planned_calls > min(window.max_calls, grant_calls)
                or planned_tokens > min(window.max_tokens, grant_tokens)
            ):
                raise AuthorizationError("authorization window cannot cover the complete call plan")
            lease = AuthorizationRunLease(
                lease_id=f"lease_{os.urandom(16).hex()}",
                one_shot_key_sha256=one_shot_key,
                window_id=window.window_id,
                run_manifest_sha256=run_manifest_sha256,
                pricing_snapshot_sha256=canonical_pricing.pricing_payload_sha256,
                pricing_status=canonical_pricing.lookup_status,
                projected_plan_usd=projected_plan_text,
                max_calls=min(requirement.max_calls, window.max_calls, grant_calls),
                max_tokens=min(requirement.max_tokens, window.max_tokens, grant_tokens),
            )
            self._append_records(
                records,
                [
                    {
                        "record_type": "pricing_snapshot",
                        "pricing_snapshot": canonical_pricing.to_primitive(),
                    },
                    {"record_type": "lease_open", "lease": asdict(lease)},
                ],
            )
            return lease

    @staticmethod
    def _lease_from_records(
        records: Sequence[dict[str, object]],
        lease: AuthorizationRunLease,
    ) -> AuthorizationRunLease:
        if not isinstance(lease, AuthorizationRunLease):
            raise AuthorizationError("authorization lease is invalid")
        record = next(
            (
                item
                for item in records
                if item.get("record_type") == "lease_open"
                and isinstance(item.get("lease"), dict)
                and item["lease"].get("lease_id") == lease.lease_id
            ),
            None,
        )
        if record is None:
            raise AuthorizationError("authorization lease is absent")
        try:
            stored = AuthorizationRunLease(**record["lease"])
        except (TypeError, ValueError) as exc:
            raise AuthorizationError("authorization lease record is invalid") from exc
        if stored != lease:
            raise AuthorizationError("authorization lease identity mismatch")
        return stored

    @staticmethod
    def _reservation_records(
        records: Sequence[dict[str, object]],
        lease_id: str,
    ) -> list[dict[str, object]]:
        return [
            item
            for item in records
            if item.get("record_type") == "reservation"
            and isinstance(item.get("reservation"), dict)
            and item["reservation"].get("lease_id") == lease_id
        ]

    @staticmethod
    def _reconciled_ids(records: Sequence[dict[str, object]]) -> set[str]:
        return {
            str(item["reservation_id"])
            for item in records
            if item.get("record_type") == "reconciliation"
        }

    @staticmethod
    def _terminal_code_for_reconciliation(
        provider_facts: PitOptimizerProviderFacts,
        requested_terminal_code: str | None,
    ) -> str | None:
        if requested_terminal_code not in {
            None,
            "failed",
            "cancelled",
            "budget_exhausted",
        }:
            raise AuthorizationError(
                "authorization reconciliation terminal code is invalid"
            )
        if requested_terminal_code is not None:
            return requested_terminal_code
        if provider_facts.outcome == "accepted":
            return None
        if provider_facts.outcome == "budget_exceeded":
            return "budget_exhausted"
        return "failed"

    @staticmethod
    def _provider_facts_accept_plan(
        facts: PitOptimizerProviderFacts,
        plan: PitOptimizerCallBudget,
    ) -> bool:
        return (
            facts.outcome == "accepted"
            and facts.request_started
            and facts.response_received
            and facts.finish_reason == "stop"
            and facts.returned_model == plan.model
            and facts.requested_model == plan.model
            and facts.response_schema_valid
            and facts.accounting_complete
        )

    def _verify_reconciliation_records(
        self,
        records: Sequence[dict[str, object]],
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        terminal_audit_sha256: str,
        terminal_code: str | None,
        terminal_audit_receipt: TerminalAuditReceipt | None = None,
    ) -> None:
        reconciliation = next(
            (
                item
                for item in records
                if item.get("record_type") == "reconciliation"
                and item.get("reservation_id") == reservation.reservation_id
            ),
            None,
        )
        if (
            reconciliation is None
            or reconciliation.get("provider_facts") != asdict(provider_facts)
            or reconciliation.get("terminal_audit_sha256")
            != terminal_audit_sha256
            or (
                terminal_audit_receipt is not None
                and reconciliation.get("terminal_audit_receipt")
                != terminal_audit_receipt.to_primitive()
            )
        ):
            raise AuthorizationError(
                "authorization reconciliation postcondition is absent"
            )
        expected_close = self._terminal_code_for_reconciliation(
            provider_facts,
            terminal_code,
        )
        close = next(
            (
                item
                for item in records
                if item.get("record_type") == "lease_close"
                and item.get("lease_id") == reservation.lease_id
            ),
            None,
        )
        if expected_close is None:
            if close is not None:
                # A lease close belongs to the lease's terminal
                # reconciliation, not to every earlier accepted call. Validate
                # the close when this reconciliation is the final one; earlier
                # accepted calls remain valid evidence for a later failed,
                # early-stop, or completed lease transition.
                reservation_ids = {
                    str(item["reservation"]["reservation_id"])
                    for item in records
                    if item.get("record_type") == "reservation"
                    and isinstance(item.get("reservation"), dict)
                    and item["reservation"].get("lease_id")
                    == reservation.lease_id
                }
                current_index = next(
                    (
                        index
                        for index, item in enumerate(records)
                        if item.get("record_type") == "reconciliation"
                        and item.get("reservation_id")
                        == reservation.reservation_id
                    ),
                    None,
                )
                if current_index is None:
                    raise AuthorizationError(
                        "authorization reconciliation postcondition is absent"
                    )
                if any(
                    index > current_index
                    and item.get("record_type") == "reconciliation"
                    and item.get("reservation_id") in reservation_ids
                    for index, item in enumerate(records)
                ):
                    return

                close_code = close.get("terminal_code")
                if close_code not in {"completed", "early_stop"}:
                    raise AuthorizationError(
                        "authorization accepted reconciliation was terminally closed"
                    )
                lease_reconciliations: list[
                    tuple[AuthorizationCallReservation, PitOptimizerProviderFacts]
                ] = []
                for item in records:
                    if (
                        item.get("record_type") != "reconciliation"
                        or item.get("reservation_id") not in reservation_ids
                    ):
                        continue
                    reservation_record = next(
                        (
                            candidate
                            for candidate in records
                            if candidate.get("record_type") == "reservation"
                            and isinstance(candidate.get("reservation"), dict)
                            and candidate["reservation"].get("reservation_id")
                            == item.get("reservation_id")
                        ),
                        None,
                    )
                    facts_primitive = item.get("provider_facts")
                    if not isinstance(reservation_record, dict) or not isinstance(
                        facts_primitive, dict
                    ):
                        raise AuthorizationError(
                            "authorization reconciliation evidence is malformed"
                        )
                    try:
                        lease_reservation = AuthorizationCallReservation(
                            **reservation_record["reservation"]
                        )
                        lease_facts = PitOptimizerProviderFacts(**facts_primitive)
                    except (TypeError, ValueError) as exc:
                        raise AuthorizationError(
                            "authorization reconciliation evidence is invalid"
                        ) from exc
                    lease_reconciliations.append((lease_reservation, lease_facts))
                expected_plans = self._call_plan_snapshots
                if not lease_reconciliations or any(
                    facts.outcome != "accepted"
                    for _lease_reservation, facts in lease_reconciliations
                ):
                    raise AuthorizationError(
                        "authorization accepted reconciliation close has a non-accepted prefix"
                    )
                if close_code == "completed":
                    if len(lease_reconciliations) != len(expected_plans):
                        raise AuthorizationError(
                            "authorization completed lease has an incomplete call plan"
                        )
                elif not 0 < len(lease_reconciliations) < len(expected_plans):
                    raise AuthorizationError(
                        "authorization early-stop lease has an invalid call prefix"
                    )
                for offset, (lease_reservation, lease_facts) in enumerate(
                    lease_reconciliations
                ):
                    if offset >= len(expected_plans):
                        raise AuthorizationError(
                            "authorization lease call plan has excess calls"
                        )
                    expected_plan = expected_plans[offset]
                    if (
                        lease_reservation.call_index,
                        lease_reservation.iteration,
                        lease_reservation.role,
                    ) != (
                        expected_plan.call_index,
                        expected_plan.iteration,
                        expected_plan.role,
                    ) or (
                        lease_facts.call_index,
                        lease_facts.iteration,
                        lease_facts.role,
                    ) != (
                        expected_plan.call_index,
                        expected_plan.iteration,
                        expected_plan.role,
                    ):
                        raise AuthorizationError(
                            "authorization lease call order differs"
                        )
        elif close is None or close.get("terminal_code") != expected_close:
            raise AuthorizationError(
                "authorization terminal reconciliation is not closed"
            )

    @staticmethod
    def _charged_totals(
        records: Sequence[dict[str, object]],
        reservation_records: Sequence[dict[str, object]],
    ) -> tuple[int, int]:
        reservation_ids = {
            str(item["reservation"]["reservation_id"])
            for item in reservation_records
        }
        calls = 0
        tokens = 0
        for item in records:
            if (
                item.get("record_type") == "reconciliation"
                and item.get("reservation_id") in reservation_ids
            ):
                charged_calls = item.get("charged_calls")
                charged_tokens = item.get("charged_tokens")
                if (
                    type(charged_calls) is not int
                    or charged_calls not in {0, 1}
                    or type(charged_tokens) is not int
                    or charged_tokens < 0
                ):
                    raise AuthorizationError("authorization reconciliation totals are invalid")
                calls += charged_calls
                tokens += charged_tokens
        return calls, tokens

    def reserve_call(
        self,
        lease: AuthorizationRunLease,
        plan: PitOptimizerCallBudget,
        *,
        projected_call_usd: Decimal | None,
    ) -> AuthorizationCallReservation:
        """Reserve the next sealed call's tokens and retain cost as advisory."""

        canonical_plan = self.snapshot_call_plan(plan)
        try:
            projected_call_text = _canonical_optional_decimal(
                projected_call_usd,
                "authorization call projected USD",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            stored_lease = self._lease_from_records(records, lease)
            if any(
                item.get("record_type") == "lease_close"
                and item.get("lease_id") == lease.lease_id
                for item in records
            ):
                raise AuthorizationError("authorization lease is closed")
            reservations = self._reservation_records(records, lease.lease_id)
            reconciled = self._reconciled_ids(records)
            reservation_ids = {
                str(item["reservation"].get("reservation_id"))
                for item in reservations
            }
            if any(
                item.get("record_type") == "reconciliation"
                and item.get("reservation_id") in reservation_ids
                and isinstance(item.get("provider_facts"), dict)
                and item["provider_facts"].get("outcome") != "accepted"
                for item in records
            ):
                raise AuthorizationError(
                    "authorization lease has a terminal reconciliation"
                )
            if any(
                item["reservation"].get("reservation_id") not in reconciled
                for item in reservations
            ):
                raise AuthorizationError("authorization lease has an active call reservation")
            next_offset = len(reservations)
            if next_offset >= len(self._manifest.call_budgets):
                raise AuthorizationError("authorization planned calls are exhausted")
            expected = self._call_plan_snapshots[next_offset]
            if canonical_plan != expected:
                raise AuthorizationError("authorization call is not the next sealed plan")
            if stored_lease.run_manifest_sha256 != self._manifest.sha256:
                raise AuthorizationError("authorization lease manifest is stale")
            if (
                stored_lease.pricing_status == "available"
            ) != (projected_call_text is not None):
                raise AuthorizationError(
                    "authorization pricing availability differs from call projection"
                )
            reserved_tokens = canonical_plan.max_input_tokens + canonical_plan.max_output_tokens
            calls, tokens = self._charged_totals(records, reservations)
            if (
                calls + 1 > stored_lease.max_calls
                or tokens + reserved_tokens > stored_lease.max_tokens
            ):
                raise AuthorizationError("authorization call exceeds remaining lease ceilings")
            reservation = AuthorizationCallReservation(
                reservation_id=f"reservation_{os.urandom(16).hex()}",
                lease_id=lease.lease_id,
                call_index=canonical_plan.call_index,
                iteration=canonical_plan.iteration,
                role=canonical_plan.role,
                reserved_tokens=reserved_tokens,
                projected_call_usd=projected_call_text,
            )
            self._append_records(
                records,
                [{"record_type": "reservation", "reservation": asdict(reservation)}],
            )
            return reservation

    def reconcile_call(
        self,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        *,
        terminal_audit_sha256: str | None = None,
        terminal_audit_receipt: TerminalAuditReceipt | None = None,
        terminal_code: str | None = None,
        _gateway_lifecycle: object | None = None,
        _recovery_seal: object | None = None,
    ) -> None:
        """Publish reconciliation and any terminal lease close as one transaction."""

        if not isinstance(reservation, AuthorizationCallReservation) or not isinstance(
            provider_facts, PitOptimizerProviderFacts
        ):
            raise AuthorizationError("authorization reconciliation contracts are invalid")
        recovering_gateway_terminal = (
            _recovery_seal is _GATEWAY_TERMINAL_RECOVERY_SEAL
        )
        if (
            (_recovery_seal is not None and not recovering_gateway_terminal)
            or (_gateway_lifecycle is not None and recovering_gateway_terminal)
        ):
            raise AuthorizationError("authorization recovery capability is invalid")
        terminal_commit_in_progress = (
            _gateway_lifecycle is not None or recovering_gateway_terminal
        )
        if self._audit_trail is not None and terminal_audit_receipt is None:
            raise AuthorizationError(
                "authorization terminal audit receipt is required"
            )
        if terminal_audit_receipt is not None:
            if not isinstance(terminal_audit_receipt, TerminalAuditReceipt):
                raise AuthorizationError(
                    "authorization terminal audit receipt is invalid"
                )
            if (
                terminal_audit_receipt.run_manifest_sha256 != self._manifest.sha256
                or (
                    terminal_audit_receipt.call_index,
                    terminal_audit_receipt.iteration,
                    terminal_audit_receipt.role,
                    terminal_audit_receipt.outcome,
                )
                != (
                    reservation.call_index,
                    reservation.iteration,
                    reservation.role,
                    provider_facts.outcome,
                )
            ):
                raise AuthorizationError(
                    "authorization terminal audit receipt differs from reservation"
                )
            if (
                terminal_audit_sha256 is not None
                and terminal_audit_sha256
                != terminal_audit_receipt.terminal_event_sha256
            ):
                raise AuthorizationError(
                    "authorization terminal audit receipt digest differs"
                )
            terminal_audit_sha256 = terminal_audit_receipt.terminal_event_sha256
        try:
            terminal_audit_sha256 = _require_digest(
                terminal_audit_sha256,
                "authorization terminal audit SHA-256",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        effective_terminal_code = self._terminal_code_for_reconciliation(
            provider_facts,
            terminal_code,
        )
        if (
            provider_facts.accounting_complete
            and provider_facts.request_started
            and provider_facts.total_tokens is not None
            and provider_facts.total_tokens > reservation.reserved_tokens
        ):
            terminal_code = "budget_exhausted"
            effective_terminal_code = "budget_exhausted"
        verified_budget_state: dict[str, object] | None = None
        if terminal_audit_receipt is not None:
            if terminal_audit_receipt.terminal_code != effective_terminal_code:
                raise AuthorizationError(
                    "authorization terminal audit receipt code differs"
                )
            verified_budget_state = self._cross_verify_audit_receipt(
                terminal_audit_receipt,
                reservation,
                provider_facts,
                require_terminal_commitment=not terminal_commit_in_progress,
            )
        lifecycle_budget_state: dict[str, object] | None = None
        if _gateway_lifecycle is not None:
            if terminal_audit_receipt is None:
                raise AuthorizationError(
                    "authorization gateway terminal receipt is required"
                )
            lifecycle_budget_state = self._require_gateway_terminal_capability(
                _gateway_lifecycle,
                reservation,
                provider_facts,
                terminal_audit_receipt,
            )
            if lifecycle_budget_state != verified_budget_state:
                raise AuthorizationError(
                    "authorization gateway terminal budget state differs"
                )
        overage = False
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            reservation_record = next(
                (
                    item
                    for item in records
                    if item.get("record_type") == "reservation"
                    and isinstance(item.get("reservation"), dict)
                    and item["reservation"].get("reservation_id")
                    == reservation.reservation_id
                ),
                None,
            )
            if reservation_record is None:
                raise AuthorizationError("authorization reservation is absent")
            try:
                stored_reservation = AuthorizationCallReservation(
                    **reservation_record["reservation"]
                )
            except (TypeError, ValueError) as exc:
                raise AuthorizationError("authorization reservation record is invalid") from exc
            if stored_reservation != reservation:
                raise AuthorizationError("authorization reservation identity mismatch")
            gateway_terminal: dict[str, object] | None = None
            if terminal_commit_in_progress:
                assert terminal_audit_receipt is not None
                assert verified_budget_state is not None
                lifecycle_commitment = self._require_gateway_lifecycle_commitment(
                    records,
                    reservation,
                    provider_facts,
                    terminal_audit_receipt,
                    budget_state=verified_budget_state,
                )
                gateway_terminal = self._gateway_terminal_commitment(
                    reservation,
                    provider_facts,
                    terminal_audit_receipt,
                    lifecycle_commitment,
                    budget_recovery_sha256=hashlib.sha256(
                        _canonical_json_bytes(verified_budget_state)
                    ).hexdigest(),
                )
            if reservation.reservation_id in self._reconciled_ids(records):
                existing = next(
                    item
                    for item in records
                    if item.get("record_type") == "reconciliation"
                    and item.get("reservation_id") == reservation.reservation_id
                )
                if (
                    existing.get("provider_facts") != asdict(provider_facts)
                    or existing.get("terminal_audit_sha256")
                    != terminal_audit_sha256
                    or (
                        terminal_audit_receipt is not None
                        and existing.get("terminal_audit_receipt")
                        != terminal_audit_receipt.to_primitive()
                    )
                    or (
                        gateway_terminal is not None
                        and existing.get("gateway_terminal")
                        != gateway_terminal
                    )
                ):
                    raise AuthorizationError(
                        "authorization reservation reconciliation differs "
                        "from prior facts"
                    )
                prior_close = next(
                    (
                        item
                        for item in records
                        if item.get("record_type") == "lease_close"
                        and item.get("lease_id") == reservation.lease_id
                    ),
                    None,
                )
                if effective_terminal_code is not None and prior_close is None:
                    if provider_facts.outcome != "accepted":
                        raise AuthorizationError(
                            "authorization terminal reconciliation is not closed"
                        )
                    self._append_records(
                        records,
                        [
                            {
                                "record_type": "lease_close",
                                "lease_id": reservation.lease_id,
                                "terminal_code": effective_terminal_code,
                            }
                        ],
                    )
                    return
                self._verify_reconciliation_records(
                    records,
                    reservation,
                    provider_facts,
                    terminal_audit_sha256,
                    terminal_code,
                    terminal_audit_receipt,
                )
                return
            lease_record = next(
                item
                for item in records
                if item.get("record_type") == "lease_open"
                and isinstance(item.get("lease"), dict)
                and item["lease"].get("lease_id") == reservation.lease_id
            )
            lease = AuthorizationRunLease(**lease_record["lease"])
            if reservation.call_index > len(self._manifest.call_budgets):
                raise AuthorizationError("authorization reservation call index is invalid")
            plan = self._call_plan_snapshots[reservation.call_index - 1]
            if (
                (
                    reservation.call_index,
                    reservation.iteration,
                    reservation.role,
                    reservation.reserved_tokens,
                )
                != (
                    plan.call_index,
                    plan.iteration,
                    plan.role,
                    plan.max_input_tokens + plan.max_output_tokens,
                )
                or
                (provider_facts.call_index, provider_facts.iteration, provider_facts.role)
                != (reservation.call_index, reservation.iteration, reservation.role)
                or provider_facts.requested_model != plan.model
                or provider_facts.pricing_snapshot_sha256
                != lease.pricing_snapshot_sha256
            ):
                raise AuthorizationError("optimizer provider facts differ from reservation")
            if not provider_facts.request_started:
                charged_calls = 0
                charged_tokens = 0
                actual_cost_usd: float | None = 0.0
                cost_accounting_status = "not_started"
            elif provider_facts.accounting_complete:
                assert provider_facts.total_tokens is not None
                assert provider_facts.cost_usd is not None
                charged_calls = 1
                charged_tokens = provider_facts.total_tokens
                actual_cost_usd = float(provider_facts.cost_usd)
                cost_accounting_status = "authoritative"
            else:
                if provider_facts.retained_reservation_tokens != reservation.reserved_tokens:
                    raise AuthorizationError("uncertain accounting must retain the full reservation")
                charged_calls = 1
                charged_tokens = reservation.reserved_tokens
                actual_cost_usd = None
                cost_accounting_status = "unavailable"
            primitive = {
                "record_type": "reconciliation",
                "reservation_id": reservation.reservation_id,
                "provider_facts": asdict(provider_facts),
                "charged_calls": charged_calls,
                "charged_tokens": charged_tokens,
                "actual_cost_usd": actual_cost_usd,
                "cost_accounting_status": cost_accounting_status,
                "terminal_audit_sha256": terminal_audit_sha256,
            }
            if terminal_audit_receipt is not None:
                primitive["terminal_audit_receipt"] = (
                    terminal_audit_receipt.to_primitive()
                )
            if gateway_terminal is not None:
                primitive["gateway_terminal"] = gateway_terminal
            reservations = self._reservation_records(records, lease.lease_id)
            prior_calls, prior_tokens = self._charged_totals(
                records,
                reservations,
            )
            overage = (
                charged_tokens > reservation.reserved_tokens
                or prior_calls + charged_calls > lease.max_calls
                or prior_tokens + charged_tokens > lease.max_tokens
            )
            primitives = [primitive]
            if effective_terminal_code is not None:
                primitives.append(
                    {
                        "record_type": "lease_close",
                        "lease_id": lease.lease_id,
                        "terminal_code": effective_terminal_code,
                    }
                )
            self._append_records(records, primitives)
        if overage:
            raise AuthorizationError("authoritative provider overage was committed")

    def _recover_gateway_terminal_reconciliation(
        self,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        receipt: TerminalAuditReceipt,
    ) -> None:
        """Finish a receipt-authenticated terminal commit after a process interruption."""

        self.reconcile_call(
            reservation,
            provider_facts,
            terminal_audit_receipt=receipt,
            terminal_code=receipt.terminal_code,
            _recovery_seal=_GATEWAY_TERMINAL_RECOVERY_SEAL,
        )

    def verify_reconciliation(
        self,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        *,
        terminal_audit_sha256: str | None = None,
        terminal_audit_receipt: TerminalAuditReceipt | None = None,
        terminal_code: str | None = None,
    ) -> None:
        """Verify the exact durable postcondition after an interrupted publication."""

        if not isinstance(reservation, AuthorizationCallReservation) or not isinstance(
            provider_facts, PitOptimizerProviderFacts
        ):
            raise AuthorizationError(
                "authorization reconciliation verification contracts are invalid"
            )
        if (
            provider_facts.accounting_complete
            and provider_facts.request_started
            and provider_facts.total_tokens is not None
            and provider_facts.total_tokens > reservation.reserved_tokens
        ):
            terminal_code = "budget_exhausted"
        if self._audit_trail is not None and terminal_audit_receipt is None:
            raise AuthorizationError(
                "authorization terminal audit receipt is required"
            )
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            if terminal_audit_receipt is not None:
                if not isinstance(terminal_audit_receipt, TerminalAuditReceipt):
                    raise AuthorizationError(
                        "authorization terminal audit receipt is invalid"
                    )
                if (
                    terminal_audit_sha256 is not None
                    and terminal_audit_sha256
                    != terminal_audit_receipt.terminal_event_sha256
                ):
                    raise AuthorizationError(
                        "authorization terminal audit receipt digest differs"
                    )
                terminal_audit_sha256 = (
                    terminal_audit_receipt.terminal_event_sha256
                )
                effective_terminal_code = self._terminal_code_for_reconciliation(
                    provider_facts,
                    terminal_code,
                )
                if terminal_audit_receipt.terminal_code != effective_terminal_code:
                    raise AuthorizationError(
                        "authorization terminal audit receipt code differs"
                    )
                self._cross_verify_audit_receipt(
                    terminal_audit_receipt,
                    reservation,
                    provider_facts,
                    records=records,
                )
            try:
                terminal_audit_sha256 = _require_digest(
                    terminal_audit_sha256,
                    "authorization terminal audit SHA-256",
                )
            except ValueError as exc:
                raise AuthorizationError(str(exc)) from exc
            self._verify_reconciliation_records(
                records,
                reservation,
                provider_facts,
                terminal_audit_sha256,
                terminal_code,
                terminal_audit_receipt,
            )

    def recover_active_reservation(
        self,
        lease: AuthorizationRunLease,
        plan: PitOptimizerCallBudget,
    ) -> AuthorizationCallReservation | None:
        """Recover the one durable active reservation after an interrupted reserve call."""

        if not isinstance(lease, AuthorizationRunLease) or not isinstance(
            plan, PitOptimizerCallBudget
        ):
            raise AuthorizationError("authorization reservation recovery is invalid")
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._lease_from_records(records, lease)
            reconciled = self._reconciled_ids(records)
            matches: list[AuthorizationCallReservation] = []
            for item in self._reservation_records(records, lease.lease_id):
                primitive = item.get("reservation")
                if not isinstance(primitive, dict):
                    raise AuthorizationError("authorization reservation record is invalid")
                reservation = AuthorizationCallReservation(**primitive)
                if (
                    reservation.reservation_id not in reconciled
                    and (
                        reservation.call_index,
                        reservation.iteration,
                        reservation.role,
                    )
                    == (plan.call_index, plan.iteration, plan.role)
                ):
                    matches.append(reservation)
            if len(matches) > 1:
                raise AuthorizationError("authorization lease has concurrent reservations")
            return matches[0] if matches else None

    def recover_call_reservation(
        self,
        lease: AuthorizationRunLease,
        plan: PitOptimizerCallBudget,
    ) -> AuthorizationCallReservation:
        """Recover the one durable reservation whether active or reconciled."""

        canonical_plan = self.snapshot_call_plan(plan)
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._lease_from_records(records, lease)
            matches: list[AuthorizationCallReservation] = []
            for item in self._reservation_records(records, lease.lease_id):
                primitive = item.get("reservation")
                if not isinstance(primitive, dict):
                    raise AuthorizationError(
                        "authorization reservation record is invalid"
                    )
                reservation = AuthorizationCallReservation(**primitive)
                if (
                    reservation.call_index,
                    reservation.iteration,
                    reservation.role,
                ) == (
                    canonical_plan.call_index,
                    canonical_plan.iteration,
                    canonical_plan.role,
                ):
                    matches.append(reservation)
            if len(matches) != 1:
                raise AuthorizationError(
                    "authorization call reservation is absent or ambiguous"
                )
            return matches[0]

    def close_run_lease(
        self,
        lease: AuthorizationRunLease,
        *,
        terminal_code: str,
    ) -> None:
        """Permanently close a one-shot lease and release only unused future allowance."""

        if terminal_code not in {
            "completed",
            "failed",
            "cancelled",
            "early_stop",
            "budget_exhausted",
        }:
            raise AuthorizationError("authorization lease terminal code is invalid")
        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._lease_from_records(records, lease)
            prior_close = next(
                (
                    item
                    for item in records
                    if item.get("record_type") == "lease_close"
                    and item.get("lease_id") == lease.lease_id
                ),
                None,
            )
            if prior_close is not None:
                if prior_close.get("terminal_code") == terminal_code:
                    return
                raise AuthorizationError("authorization lease is already closed")
            reservations = self._reservation_records(records, lease.lease_id)
            reconciled = self._reconciled_ids(records)
            if any(
                item["reservation"].get("reservation_id") not in reconciled
                for item in reservations
            ):
                raise AuthorizationError("authorization lease has an active call reservation")
            if terminal_code in {"completed", "early_stop"}:
                accepted_plans: list[PitOptimizerCallBudget] = []
                for offset, item in enumerate(reservations):
                    primitive = item.get("reservation")
                    if not isinstance(primitive, dict):
                        raise AuthorizationError(
                            "authorization reservation record is invalid"
                        )
                    reservation = AuthorizationCallReservation(**primitive)
                    if offset >= len(self._manifest.call_budgets):
                        raise AuthorizationError(
                            "authorization completed plan has excess calls"
                        )
                    plan = self._manifest.call_budgets[offset]
                    if (
                        reservation.call_index,
                        reservation.iteration,
                        reservation.role,
                    ) != (plan.call_index, plan.iteration, plan.role):
                        raise AuthorizationError(
                            "authorization completed plan order differs"
                        )
                    reconciliation = next(
                        record
                        for record in records
                        if record.get("record_type") == "reconciliation"
                        and record.get("reservation_id")
                        == reservation.reservation_id
                    )
                    facts_primitive = reconciliation.get("provider_facts")
                    if not isinstance(facts_primitive, dict):
                        raise AuthorizationError(
                            "authorization provider facts are invalid"
                        )
                    facts = PitOptimizerProviderFacts(**facts_primitive)
                    if not self._provider_facts_accept_plan(facts, plan):
                        raise AuthorizationError(
                            "authorization completed plan has a non-accepted call"
                        )
                    accepted_plans.append(plan)
                if terminal_code == "completed" and len(accepted_plans) != len(
                    self._manifest.call_budgets
                ):
                    raise AuthorizationError(
                        "authorization completed lease requires the exact call plan"
                    )
                if terminal_code == "early_stop" and not (
                    0 < len(accepted_plans) < len(self._manifest.call_budgets)
                ):
                    raise AuthorizationError(
                        "authorization early stop requires an accepted partial plan"
                    )
            self._append_records(
                records,
                [
                    {
                        "record_type": "lease_close",
                        "lease_id": lease.lease_id,
                        "terminal_code": terminal_code,
                    }
                ],
            )


def _load_authenticated_manifest(
    manifest_path: Path,
    manifest_sha256: str,
) -> PitOptimizerRunManifest:
    path = Path(manifest_path)
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or _SHA256_RE.fullmatch(manifest_sha256 or "") is None
    ):
        raise AuthorizationError("authorization manifest path or digest is invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise AuthorizationError("authorization manifest digest mismatch")
    try:
        primitive = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, AuthorizationError) as exc:
        raise AuthorizationError("authorization manifest is invalid JSON") from exc
    if not isinstance(primitive, dict) or raw != _canonical_json_bytes(primitive):
        raise AuthorizationError("authorization manifest is not canonical JSON")
    try:
        manifest = _contract._pit_optimizer_manifest_from_primitive(primitive)
    except ValueError as exc:
        raise AuthorizationError("authorization manifest contract is invalid") from exc
    if manifest.sha256 != manifest_sha256:
        raise AuthorizationError("authorization manifest closed identity mismatch")
    return manifest


def record_authorized_grant(
    *,
    ledger_path: Path,
    manifest_path: Path,
    manifest_sha256: str,
    grant: OperatorAuthorizationGrant,
    operator_approval_reference: str,
) -> OperatorAuthorizationWindow:
    """Authenticate a manifest and atomically append one explicit grant/window."""

    manifest = _load_authenticated_manifest(manifest_path, manifest_sha256)
    if not isinstance(grant, OperatorAuthorizationGrant):
        raise AuthorizationError("authorization grant is invalid")
    if grant.policy_source_scope_sha256 != manifest.policy_source_scope.sha256:
        raise AuthorizationError("policy source scope mismatch")
    requirement = manifest.authorization_requirement
    if (
        grant.additional_calls > requirement.max_calls
        or grant.additional_tokens > requirement.max_tokens
    ):
        raise AuthorizationError("authorization grant exceeds manifest ceilings")
    window = OperatorAuthorizationWindow(
        window_id=requirement.window_id,
        grant_ids=(grant.grant_id,),
        authorization_requirement_sha256=requirement.sha256,
        max_calls=min(requirement.max_calls, grant.additional_calls),
        max_tokens=min(requirement.max_tokens, grant.additional_tokens),
        policy_source_scope_sha256=manifest.policy_source_scope.sha256,
    )
    ledger = AuthorizationLedger(Path(ledger_path), manifest)
    ledger.record_grant_and_window(
        grant=grant,
        window=window,
        requirement=requirement,
        operator_approval_reference=operator_approval_reference,
    )
    return window


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -B -m core.pit_optimizer_authorization")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record-grant")
    record.add_argument("--ledger-path", type=Path, required=True)
    record.add_argument("--manifest-path", type=Path, required=True)
    record.add_argument("--manifest-sha256", required=True)
    record.add_argument("--grant-id", required=True)
    record.add_argument("--additional-calls", type=int, required=True)
    record.add_argument("--additional-tokens", type=int, required=True)
    record.add_argument("--policy-source-scope-sha256", required=True)
    record.add_argument("--operator-approval-reference", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if args.command != "record-grant":  # pragma: no cover - argparse closes this branch
        raise AuthorizationError("authorization command is invalid")
    grant = OperatorAuthorizationGrant(
        grant_id=args.grant_id,
        additional_calls=args.additional_calls,
        additional_tokens=args.additional_tokens,
        policy_source_scope_sha256=args.policy_source_scope_sha256,
    )
    record_authorized_grant(
        ledger_path=args.ledger_path,
        manifest_path=args.manifest_path,
        manifest_sha256=args.manifest_sha256,
        grant=grant,
        operator_approval_reference=args.operator_approval_reference,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main with temp roots
    raise SystemExit(main())
