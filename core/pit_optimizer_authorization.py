"""Explicit, append-only authorization for schema-v2 PIT optimizer runs.

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
from typing import Iterator, Sequence

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


def _positive_usd(value: object, label: str) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise ValueError(f"{label} must be positive and finite")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return normalized


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


def _frozen_pricing_digest(
    model: str,
    prompt_per_million: Decimal,
    completion_per_million: Decimal,
) -> str:
    payload = {
        "model": model,
        "prompt_per_million": _canonical_decimal_text(prompt_per_million),
        "completion_per_million": _canonical_decimal_text(completion_per_million),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


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


@dataclass(frozen=True, slots=True)
class OperatorAuthorizationGrant:
    grant_id: str
    additional_calls: int
    additional_tokens: int
    additional_usd: float
    policy_source_scope_sha256: str

    def __post_init__(self) -> None:
        _require_id(self.grant_id, "authorization grant ID")
        _positive_int(self.additional_calls, "authorization grant additional calls")
        _positive_int(self.additional_tokens, "authorization grant additional tokens")
        _positive_usd(self.additional_usd, "authorization grant additional USD")
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
    max_usd: float
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
        _positive_usd(self.max_usd, "authorization window USD cap")
        _require_digest(
            self.policy_source_scope_sha256,
            "authorization window policy source scope SHA-256",
        )


@dataclass(frozen=True, slots=True)
class FrozenModelPricing:
    model: str
    prompt_per_million: Decimal
    completion_per_million: Decimal
    pricing_payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or _MODEL_RE.fullmatch(self.model) is None:
            raise ValueError("frozen pricing model is invalid")
        for value in (self.prompt_per_million, self.completion_per_million):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("frozen pricing rates must be finite non-negative Decimals")
        _require_digest(self.pricing_payload_sha256, "frozen pricing payload SHA-256")
        if self.pricing_payload_sha256 != _frozen_pricing_digest(
            self.model,
            self.prompt_per_million,
            self.completion_per_million,
        ):
            raise ValueError("frozen pricing digest differs from model and rates")

    @classmethod
    def from_rates(
        cls,
        *,
        model: str,
        prompt_per_million: Decimal,
        completion_per_million: Decimal,
    ) -> "FrozenModelPricing":
        return cls(
            model=model,
            prompt_per_million=prompt_per_million,
            completion_per_million=completion_per_million,
            pricing_payload_sha256=_frozen_pricing_digest(
                model,
                prompt_per_million,
                completion_per_million,
            ),
        )

    @property
    def pricing_sha256(self) -> str:
        return self.pricing_payload_sha256


@dataclass(frozen=True, slots=True)
class AuthorizationRunLease:
    lease_id: str
    one_shot_key_sha256: str
    window_id: str
    run_manifest_sha256: str
    frozen_pricing_sha256: str
    max_calls: int
    max_tokens: int
    max_usd: float

    def __post_init__(self) -> None:
        _require_id(self.lease_id, "authorization lease ID")
        _require_digest(self.one_shot_key_sha256, "authorization one-shot key SHA-256")
        _require_id(self.window_id, "authorization lease window ID")
        _require_digest(self.run_manifest_sha256, "authorization lease manifest SHA-256")
        _require_digest(self.frozen_pricing_sha256, "authorization lease pricing SHA-256")
        _positive_int(self.max_calls, "authorization lease call cap")
        _positive_int(self.max_tokens, "authorization lease token cap")
        _positive_usd(self.max_usd, "authorization lease USD cap")


@dataclass(frozen=True, slots=True)
class AuthorizationCallReservation:
    reservation_id: str
    lease_id: str
    call_index: int
    iteration: int
    role: str
    reserved_tokens: int
    reserved_usd: float

    def __post_init__(self) -> None:
        _require_id(self.reservation_id, "authorization reservation ID")
        _require_id(self.lease_id, "authorization reservation lease ID")
        _positive_int(self.call_index, "authorization reservation call index")
        _positive_int(self.iteration, "authorization reservation iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("authorization reservation role is invalid")
        _positive_int(self.reserved_tokens, "authorization reservation token cap")
        _positive_usd(self.reserved_usd, "authorization reservation USD cap")


@dataclass(frozen=True, slots=True)
class PitOptimizerProviderFacts:
    call_index: int
    iteration: int
    role: str
    requested_model: str
    returned_model: str | None
    frozen_pricing_sha256: str
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
    retained_reservation_usd: float
    audit_sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.call_index, "optimizer provider call index")
        _positive_int(self.iteration, "optimizer provider iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("optimizer provider role is invalid")
        if _MODEL_RE.fullmatch(self.requested_model or "") is None:
            raise ValueError("optimizer requested model is invalid")
        if self.returned_model is not None and _MODEL_RE.fullmatch(self.returned_model) is None:
            raise ValueError("optimizer returned model is invalid")
        _require_digest(self.frozen_pricing_sha256, "optimizer frozen pricing SHA-256")
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
        if type(self.response_schema_valid) is not bool or type(self.accounting_complete) is not bool:
            raise ValueError("optimizer provider validation/accounting facts are invalid")
        if (
            type(self.retained_reservation_tokens) is not int
            or self.retained_reservation_tokens < 0
            or type(self.retained_reservation_usd) not in {int, float}
            or not math.isfinite(float(self.retained_reservation_usd))
            or self.retained_reservation_usd < 0
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
            if self.retained_reservation_tokens != 0 or self.retained_reservation_usd != 0:
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
            if self.retained_reservation_tokens <= 0 or self.retained_reservation_usd <= 0:
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
            if artifact.hypothesis_id != self.expected_hypothesis_id:
                raise ValueError("author hypothesis differs from investigator")
            assert self.source_bundle is not None
            assert self.candidate_bounds is not None
            _resulting_texts, stats = _contract._apply_unified_diff(
                {record.path: record.text for record in self.source_bundle.files},
                artifact.unified_diff,
                bounds=self.candidate_bounds,
            )
            if stats.paths != artifact.changed_paths:
                raise ValueError(
                    "author changed paths differ from candidate unified diff"
                )
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
        self._path = candidate.resolve()
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._manifest = manifest
        # Strong references make object identity a bounded, live controller
        # capability.  Reopening the ledger intentionally loses these
        # process-local capabilities and therefore fails closed.
        self._role_input_capabilities: dict[
            int,
            tuple[object, int, bytes],
        ] = {}
        with _authorization_file_lock(self._lock_path):
            self._read_records()

    @property
    def manifest(self) -> PitOptimizerRunManifest:
        """Return the immutable manifest authenticated by this ledger instance."""

        return self._manifest

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
    ) -> AuthenticatedRoleInputSnapshot:
        """Register the exact live object produced by the trusted controller."""

        snapshot = self._snapshot_role_input(dynamic_input, plan)
        key = id(dynamic_input)
        prior = self._role_input_capabilities.get(key)
        capability = (dynamic_input, plan.call_index, snapshot.canonical_bytes)
        if prior is not None and prior != capability:
            raise AuthorizationError("optimizer role input capability changed")
        if prior is None and len(self._role_input_capabilities) >= len(
            self._manifest.call_budgets
        ):
            raise AuthorizationError("optimizer role input capabilities are exhausted")
        self._role_input_capabilities[key] = capability
        return snapshot

    def capture_controller_role_input(
        self,
        dynamic_input: object,
        plan: PitOptimizerCallBudget,
    ) -> AuthenticatedRoleInputSnapshot:
        """Reauthenticate an exact snapshot against a live controller capability."""

        snapshot = self._snapshot_role_input(dynamic_input, plan)
        capability = self._role_input_capabilities.get(id(dynamic_input))
        if (
            capability is None
            or capability[0] is not dynamic_input
            or capability[1] != plan.call_index
            or capability[2] != snapshot.canonical_bytes
        ):
            raise AuthorizationError(
                "optimizer role input provenance is not controller authenticated"
            )
        return snapshot

    @staticmethod
    def _record_digest(record: dict[str, object]) -> str:
        preimage = dict(record)
        preimage.pop("record_sha256", None)
        return hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()

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
        leases: dict[str, AuthorizationRunLease] = {}
        one_shot_keys: set[str] = set()
        active_reservations: dict[str, AuthorizationCallReservation] = {}
        reconciled_reservations: set[str] = set()
        closed_leases: set[str] = set()
        for index, line in enumerate(raw.splitlines(keepends=True), start=1):
            try:
                value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, AuthorizationError) as exc:
                raise AuthorizationError("authorization ledger contains invalid JSON") from exc
            if not isinstance(value, dict) or line != _canonical_json_bytes(value):
                raise AuthorizationError("authorization ledger record is not canonical JSON")
            if value.get("schema_version") != 2 or value.get("record_index") != index:
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
                    or Decimal(str(window.max_usd))
                    > sum(
                        Decimal(str(grant.additional_usd)) for grant in selected
                    )
                ):
                    raise AuthorizationError("authorization window exceeds named grants")
                windows[window.window_id] = window
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
            elif record_type == "reconciliation":
                expected = expected_common | {
                    "reservation_id",
                    "provider_facts",
                    "charged_calls",
                    "charged_tokens",
                    "charged_usd",
                    "charge_basis",
                }
                if frozenset(value) not in {
                    frozenset(expected),
                    frozenset(expected | {"terminal_audit_sha256"}),
                }:
                    raise AuthorizationError("authorization reconciliation keys are invalid")
                terminal_audit_sha256 = value.get("terminal_audit_sha256")
                if terminal_audit_sha256 is not None:
                    try:
                        _require_digest(
                            terminal_audit_sha256,
                            "authorization terminal audit SHA-256",
                        )
                    except ValueError as exc:
                        raise AuthorizationError(str(exc)) from exc
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
                ):
                    raise AuthorizationError("authorization provider facts differ from reservation")
                if not facts.request_started:
                    expected_charge = (0, 0, Decimal("0"), "before_send_release")
                elif facts.accounting_complete:
                    assert facts.total_tokens is not None
                    assert facts.cost_usd is not None
                    expected_charge = (
                        1,
                        facts.total_tokens,
                        Decimal(str(facts.cost_usd)),
                        "authoritative",
                    )
                else:
                    expected_charge = (
                        1,
                        facts.retained_reservation_tokens,
                        Decimal(str(facts.retained_reservation_usd)),
                        "retained_reservation",
                    )
                try:
                    actual_charge = (
                        value.get("charged_calls"),
                        value.get("charged_tokens"),
                        Decimal(str(value.get("charged_usd"))),
                        value.get("charge_basis"),
                    )
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise AuthorizationError(
                        "authorization reconciliation charge is invalid"
                    ) from exc
                if actual_charge != expected_charge:
                    raise AuthorizationError("authorization reconciliation charge is invalid")
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
                if value.get("terminal_code") not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "early_stop",
                    "budget_exhausted",
                }:
                    raise AuthorizationError("authorization lease terminal code is invalid")
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
        return records

    def _build_record(
        self,
        records: list[dict[str, object]],
        primitive: dict[str, object],
    ) -> dict[str, object]:
        record = {
            "schema_version": 2,
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
            or Decimal(str(grant.additional_usd)) > Decimal(str(requirement.max_usd))
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
        grant_calls, grant_tokens, grant_usd = self._remaining_grant_capacity(
            records,
            window.grant_ids,
        )
        if (
            window.max_calls > min(requirement.max_calls, grant_calls)
            or window.max_tokens > min(requirement.max_tokens, grant_tokens)
            or Decimal(str(window.max_usd))
            > min(Decimal(str(requirement.max_usd)), grant_usd)
        ):
            raise AuthorizationError("authorization window exceeds effective ceilings")

    @classmethod
    def _remaining_grant_capacity(
        cls,
        records: Sequence[dict[str, object]],
        grant_ids: Sequence[str],
    ) -> tuple[int, int, Decimal]:
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
        spent_usd = Decimal("0")
        for record in records:
            if (
                record.get("record_type") != "reconciliation"
                or record.get("reservation_id") not in reservation_ids
            ):
                continue
            try:
                calls = record["charged_calls"]
                tokens = record["charged_tokens"]
                usd = Decimal(str(record["charged_usd"]))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise AuthorizationError(
                    "authorization reconciliation totals are invalid"
                ) from exc
            if (
                type(calls) is not int
                or calls not in {0, 1}
                or type(tokens) is not int
                or tokens < 0
                or not usd.is_finite()
                or usd < 0
            ):
                raise AuthorizationError(
                    "authorization reconciliation totals are invalid"
                )
            spent_calls += calls
            spent_tokens += tokens
            spent_usd += usd
        total_calls = sum(grant.additional_calls for grant in grants.values())
        total_tokens = sum(grant.additional_tokens for grant in grants.values())
        total_usd = sum(
            (Decimal(str(grant.additional_usd)) for grant in grants.values()),
            start=Decimal("0"),
        )
        return (
            max(0, total_calls - spent_calls),
            max(0, total_tokens - spent_tokens),
            max(Decimal("0"), total_usd - spent_usd),
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

    def open_run_lease(
        self,
        *,
        window_id: str,
        authorization_requirement_sha256: str,
        run_manifest_sha256: str,
        frozen_pricing_sha256: str,
    ) -> AuthorizationRunLease:
        """Atomically acquire the manifest/window once without debiting allowance."""

        try:
            _require_id(window_id, "authorization lease window ID")
            _require_digest(
                authorization_requirement_sha256,
                "authorization lease requirement SHA-256",
            )
            _require_digest(run_manifest_sha256, "authorization lease manifest SHA-256")
            _require_digest(frozen_pricing_sha256, "authorization lease pricing SHA-256")
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        requirement = self._manifest.authorization_requirement
        if authorization_requirement_sha256 != requirement.sha256:
            raise AuthorizationError("authorization requirement mismatch")
        if run_manifest_sha256 != self._manifest.sha256:
            raise AuthorizationError("authorization run manifest mismatch")
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
            grant_calls, grant_tokens, grant_usd = self._remaining_grant_capacity(
                records,
                window.grant_ids,
            )
            planned_calls = len(self._manifest.call_budgets)
            planned_tokens = sum(
                item.max_input_tokens + item.max_output_tokens
                for item in self._manifest.call_budgets
            )
            planned_usd = sum(
                Decimal(str(item.max_usd)) for item in self._manifest.call_budgets
            )
            if (
                planned_calls > min(window.max_calls, grant_calls)
                or planned_tokens > min(window.max_tokens, grant_tokens)
                or planned_usd > min(Decimal(str(window.max_usd)), grant_usd)
            ):
                raise AuthorizationError("authorization window cannot cover the complete call plan")
            lease = AuthorizationRunLease(
                lease_id=f"lease_{os.urandom(16).hex()}",
                one_shot_key_sha256=one_shot_key,
                window_id=window.window_id,
                run_manifest_sha256=run_manifest_sha256,
                frozen_pricing_sha256=frozen_pricing_sha256,
                max_calls=min(requirement.max_calls, window.max_calls, grant_calls),
                max_tokens=min(requirement.max_tokens, window.max_tokens, grant_tokens),
                max_usd=float(
                    min(
                        Decimal(str(requirement.max_usd)),
                        Decimal(str(window.max_usd)),
                        grant_usd,
                    )
                ),
            )
            self._append_records(
                records,
                [{"record_type": "lease_open", "lease": asdict(lease)}],
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

    @classmethod
    def _verify_reconciliation_records(
        cls,
        records: Sequence[dict[str, object]],
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        terminal_audit_sha256: str,
        terminal_code: str | None,
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
        ):
            raise AuthorizationError(
                "authorization reconciliation postcondition is absent"
            )
        expected_close = cls._terminal_code_for_reconciliation(
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
                raise AuthorizationError(
                    "authorization accepted reconciliation was terminally closed"
                )
        elif close is None or close.get("terminal_code") != expected_close:
            raise AuthorizationError(
                "authorization terminal reconciliation is not closed"
            )

    @staticmethod
    def _charged_totals(
        records: Sequence[dict[str, object]],
        reservation_records: Sequence[dict[str, object]],
    ) -> tuple[int, int, Decimal]:
        reservation_ids = {
            str(item["reservation"]["reservation_id"])
            for item in reservation_records
        }
        calls = 0
        tokens = 0
        usd = Decimal("0")
        for item in records:
            if (
                item.get("record_type") == "reconciliation"
                and item.get("reservation_id") in reservation_ids
            ):
                charged_calls = item.get("charged_calls")
                charged_tokens = item.get("charged_tokens")
                charged_usd = item.get("charged_usd")
                if (
                    type(charged_calls) is not int
                    or charged_calls not in {0, 1}
                    or type(charged_tokens) is not int
                    or charged_tokens < 0
                    or type(charged_usd) not in {int, float}
                    or not math.isfinite(float(charged_usd))
                    or charged_usd < 0
                ):
                    raise AuthorizationError("authorization reconciliation totals are invalid")
                calls += charged_calls
                tokens += charged_tokens
                usd += Decimal(str(charged_usd))
        return calls, tokens, usd

    def reserve_call(
        self,
        lease: AuthorizationRunLease,
        plan: PitOptimizerCallBudget,
    ) -> AuthorizationCallReservation:
        """Reserve only the next sealed call's exact token/USD maxima."""

        if not isinstance(plan, PitOptimizerCallBudget):
            raise AuthorizationError("optimizer call plan is invalid")
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
            expected = self._manifest.call_budgets[next_offset]
            if plan != expected:
                raise AuthorizationError("authorization call is not the next sealed plan")
            if stored_lease.run_manifest_sha256 != self._manifest.sha256:
                raise AuthorizationError("authorization lease manifest is stale")
            reserved_tokens = plan.max_input_tokens + plan.max_output_tokens
            calls, tokens, usd = self._charged_totals(records, reservations)
            if (
                calls + 1 > stored_lease.max_calls
                or tokens + reserved_tokens > stored_lease.max_tokens
                or usd + Decimal(str(plan.max_usd))
                > Decimal(str(stored_lease.max_usd))
            ):
                raise AuthorizationError("authorization call exceeds remaining lease ceilings")
            reservation = AuthorizationCallReservation(
                reservation_id=f"reservation_{os.urandom(16).hex()}",
                lease_id=lease.lease_id,
                call_index=plan.call_index,
                iteration=plan.iteration,
                role=plan.role,
                reserved_tokens=reserved_tokens,
                reserved_usd=float(plan.max_usd),
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
        terminal_audit_sha256: str,
        terminal_code: str | None = None,
    ) -> None:
        """Publish reconciliation and any terminal lease close as one transaction."""

        if not isinstance(reservation, AuthorizationCallReservation) or not isinstance(
            provider_facts, PitOptimizerProviderFacts
        ):
            raise AuthorizationError("authorization reconciliation contracts are invalid")
        try:
            _require_digest(
                terminal_audit_sha256,
                "authorization terminal audit SHA-256",
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        effective_terminal_code = self._terminal_code_for_reconciliation(
            provider_facts,
            terminal_code,
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
            plan = self._manifest.call_budgets[reservation.call_index - 1]
            if (
                (
                    reservation.call_index,
                    reservation.iteration,
                    reservation.role,
                    reservation.reserved_tokens,
                    Decimal(str(reservation.reserved_usd)),
                )
                != (
                    plan.call_index,
                    plan.iteration,
                    plan.role,
                    plan.max_input_tokens + plan.max_output_tokens,
                    Decimal(str(plan.max_usd)),
                )
                or
                (provider_facts.call_index, provider_facts.iteration, provider_facts.role)
                != (reservation.call_index, reservation.iteration, reservation.role)
                or provider_facts.requested_model != plan.model
                or provider_facts.frozen_pricing_sha256
                != lease.frozen_pricing_sha256
            ):
                raise AuthorizationError("optimizer provider facts differ from reservation")
            if not provider_facts.request_started:
                charged_calls = 0
                charged_tokens = 0
                charged_usd = Decimal("0")
                charge_basis = "before_send_release"
            elif provider_facts.accounting_complete:
                assert provider_facts.total_tokens is not None
                assert provider_facts.cost_usd is not None
                charged_calls = 1
                charged_tokens = provider_facts.total_tokens
                charged_usd = Decimal(str(provider_facts.cost_usd))
                charge_basis = "authoritative"
            else:
                if (
                    provider_facts.retained_reservation_tokens
                    != reservation.reserved_tokens
                    or Decimal(str(provider_facts.retained_reservation_usd))
                    != Decimal(str(reservation.reserved_usd))
                ):
                    raise AuthorizationError("uncertain accounting must retain the full reservation")
                charged_calls = 1
                charged_tokens = reservation.reserved_tokens
                charged_usd = Decimal(str(reservation.reserved_usd))
                charge_basis = "retained_reservation"
            primitive = {
                "record_type": "reconciliation",
                "reservation_id": reservation.reservation_id,
                "provider_facts": asdict(provider_facts),
                "charged_calls": charged_calls,
                "charged_tokens": charged_tokens,
                "charged_usd": float(charged_usd),
                "charge_basis": charge_basis,
                "terminal_audit_sha256": terminal_audit_sha256,
            }
            reservations = self._reservation_records(records, lease.lease_id)
            prior_calls, prior_tokens, prior_usd = self._charged_totals(
                records,
                reservations,
            )
            overage = (
                charged_tokens > reservation.reserved_tokens
                or charged_usd > Decimal(str(reservation.reserved_usd))
                or prior_calls + charged_calls > lease.max_calls
                or prior_tokens + charged_tokens > lease.max_tokens
                or prior_usd + charged_usd > Decimal(str(lease.max_usd))
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

    def verify_reconciliation(
        self,
        reservation: AuthorizationCallReservation,
        provider_facts: PitOptimizerProviderFacts,
        *,
        terminal_audit_sha256: str,
        terminal_code: str | None = None,
    ) -> None:
        """Verify the exact durable postcondition after an interrupted publication."""

        with _authorization_file_lock(self._lock_path):
            records = self._read_records()
            self._verify_reconciliation_records(
                records,
                reservation,
                provider_facts,
                terminal_audit_sha256,
                terminal_code,
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
                    if not (
                        facts.outcome == "accepted"
                        and facts.request_started
                        and facts.response_received
                        and facts.finish_reason == "stop"
                        and facts.returned_model == plan.model
                        and facts.requested_model == plan.model
                        and facts.response_schema_valid
                        and facts.accounting_complete
                    ):
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
        or Decimal(str(grant.additional_usd)) > Decimal(str(requirement.max_usd))
    ):
        raise AuthorizationError("authorization grant exceeds manifest ceilings")
    window = OperatorAuthorizationWindow(
        window_id=requirement.window_id,
        grant_ids=(grant.grant_id,),
        authorization_requirement_sha256=requirement.sha256,
        max_calls=min(requirement.max_calls, grant.additional_calls),
        max_tokens=min(requirement.max_tokens, grant.additional_tokens),
        max_usd=float(
            min(Decimal(str(requirement.max_usd)), Decimal(str(grant.additional_usd)))
        ),
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
    record.add_argument("--additional-usd", type=Decimal, required=True)
    record.add_argument("--policy-source-scope-sha256", required=True)
    record.add_argument("--operator-approval-reference", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if args.command != "record-grant":  # pragma: no cover - argparse closes this branch
        raise AuthorizationError("authorization command is invalid")
    try:
        additional_usd = float(args.additional_usd)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise AuthorizationError("authorization grant additional USD is invalid") from exc
    grant = OperatorAuthorizationGrant(
        grant_id=args.grant_id,
        additional_calls=args.additional_calls,
        additional_tokens=args.additional_tokens,
        additional_usd=additional_usd,
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
