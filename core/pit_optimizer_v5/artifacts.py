"""Contained, authenticated local artifact storage for PIT optimizer V5."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import types
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal, TypeVar, get_args, get_origin, get_type_hints

from core.pit_optimizer_artifacts import (
    _acquire_absolute_directory,
    _acquire_directory,
    _atomic_replace_in_directory,
    _is_link_or_reparse,
    _metadata_identity,
    _write_create_only_in_directory,
)
from core.pit_optimizer_evaluation import (
    EvaluationPanelSpec,
    _canonical_json_bytes as _canonical_panel_json_bytes,
    _panel_json_value,
)
from core.pit_optimizer_v5.candidate_ir import (
    ExperimentIdentityV5,
    LiteralAxisV5,
    PolicyRevisionIdentityV5,
    PreValidationInvalidExperimentIdentityV5,
    RenderedVariantV5,
    SourceBundleV5,
    SourceFileV5,
    SourceOperationV5,
    StructuralTemplateV5,
    VariantAssignmentV5,
)
from core.pit_optimizer_v5.contracts import (
    AnnualizedReturnTargetV5,
    ArtifactGraphFailureV5,
    ArtifactGraphVerificationV5,
    ArtifactRefV5,
    AuthenticatedArtifactV5,
    AuthenticatedRawArtifactV5,
    CampaignEvidenceV5,
    CampaignManifestV5,
    CampaignPanelPlanV5,
    CriticArtifactV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    EvaluatorContractV5,
    FinalizedDiscoveryCampaignV5,
    HypothesisV5,
    PanelEvaluationV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
    validate_episode_plan_panel_v5,
)
from core.pit_optimizer_v5.memory import (
    ArchiveReducerV5,
    CandidateExecutionAuthorityV5,
    CandidateExecutionKeyV5,
    CandidateStageResultPayloadV5,
    CleanupResultPayloadV5,
    EpisodeEvidencePayloadV5,
    ExperimentRecordV5,
    QuickEvidencePayloadV5,
    RecoveryStepV5,
    RenderedVariantPayloadV5,
    ResourceLeasePayloadV5,
    RoleCompletionPayloadV5,
    RoundEventPayloadV5,
    RoundEventV5,
    RoundIntentPayloadV5,
    RoundOutcomePayloadV5,
    RoundRecoveryV5,
    StoredExperimentRecordV5,
    event_kind_for_payload_v5,
    fold_round_events_v5,
    is_testable_experiment_status_v5,
    reduce_experiment_journal_v5,
    round_event_payload_primitive_v5,
)
from core.pit_optimizer_v5.probes import ProbeObservationV5, SemanticFingerprintV5
from core.pit_optimizer_v5.search import ArchiveEntryV5, SearchStateV5
from core.pit_optimizer_v5.provider import (
    AuthorizedRoleSlotV5,
    ExistingPersistedRoleRequestV5,
    FixtureRoleTerminalAuthorityV5,
    FreshPersistedRoleRequestV5,
    LedgerRoleTerminalAuthorityV5,
    ParsedRoleArtifactV5,
    PersistedRoleRequestV5,
    RoleAttemptFactsV5,
    RoleCallKeyV5,
    RoleFailureCode,
    RoleInputV5,
    RoleInvocationPackageV5,
    RoleInvocationClaimV5,
    RoleLedgerReservationV5,
    RoleLedgerTerminalV5,
    RoleNameV5,
    RoleProviderResponseV5,
    RoleRequestV5,
    RoleSchemaAuthorityV5,
    RoleTerminalAuthorityV5,
    RoleTerminalReceiptV5,
    decode_parsed_role_artifact_v5,
    parsed_role_artifact_primitive_v5,
    role_request_artifact_primitive_v5,
)


_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)

ArtifactFailureCodeV5 = Literal[
    "missing",
    "relocated",
    "cycle",
    "digest_mismatch",
    "noncanonical",
    "schema",
    "exists",
    "migration_required",
]


class ArtifactRepositoryFailureV5(ValueError):
    """Sanitized typed artifact failure; filesystem exception text never escapes."""

    code: ArtifactFailureCodeV5

    def __init__(
        self,
        code: ArtifactFailureCodeV5,
        reference: ArtifactRefV5 | None = None,
    ) -> None:
        self.code = code
        self.reference = reference
        super().__init__(f"optimizer V5 artifact failure: {code}")


class ArtifactMissingV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5 | None = None) -> None:
        super().__init__("missing", reference)


class ArtifactRelocatedV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5, relocated_path: str) -> None:
        self.relocated_path = relocated_path
        super().__init__("relocated", reference)


class ArtifactCycleV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5) -> None:
        super().__init__("cycle", reference)


class ArtifactDigestMismatchV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5, actual_sha256: str) -> None:
        self.actual_sha256 = actual_sha256
        super().__init__("digest_mismatch", reference)


class ArtifactNonCanonicalV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5 | None = None) -> None:
        super().__init__("noncanonical", reference)


class ArtifactSchemaFailureV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5 | None = None) -> None:
        super().__init__("schema", reference)


class ArtifactExistsV5(ArtifactRepositoryFailureV5):
    def __init__(self, reference: ArtifactRefV5 | None = None) -> None:
        super().__init__("exists", reference)


class RoleLedgerMigrationRequiredV5(ArtifactRepositoryFailureV5):
    """Stable fail-closed result for the two superseded pre-release ledger shapes."""

    def __init__(self, record_revision: Literal[1, 2], reference: ArtifactRefV5 | None = None) -> None:
        self.record_revision = record_revision
        super().__init__("migration_required", reference)


@dataclass(frozen=True, slots=True)
class RoleLedgerMigrationAuthorityV5:
    """Exact current authority used only to authenticate superseded ledger records."""

    campaign_id: str
    campaign_manifest_sha256: str
    ledger_identity_sha256: str
    audit_store_identity_sha256: str
    model: str
    maximum_output_tokens_per_role: int

    def __post_init__(self) -> None:
        _safe_component(self.campaign_id, "role-ledger migration campaign")
        for value, label in (
            (self.campaign_manifest_sha256, "role-ledger migration manifest"),
            (self.ledger_identity_sha256, "role-ledger migration identity"),
            (self.audit_store_identity_sha256, "role-ledger migration audit store"),
        ):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{label} is invalid")
        if type(self.model) is not str or not self.model or self.model.strip() != self.model:
            raise ValueError("role-ledger migration model is invalid")
        if type(self.maximum_output_tokens_per_role) is not int or self.maximum_output_tokens_per_role <= 0:
            raise ValueError("role-ledger migration output bound is invalid")


def _safe_component(value: object, label: str) -> str:
    if type(value) is not str or _COMPONENT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical artifact component")
    if value.endswith((".", " ")) or ":" in value or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise ValueError(f"{label} is not portable artifact text")
    return value


def _safe_relative_path(value: str) -> tuple[str, ...]:
    reference = ArtifactRefV5(value, "0" * 64)
    parts = PurePosixPath(reference.relative_path).parts
    for part in parts:
        _safe_component(part, "artifact path component")
    return parts


def _strict_json_object(raw: bytes, reference: ArtifactRefV5 | None) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ArtifactNonCanonicalV5(reference)
            result[key] = value
        return result

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except ArtifactRepositoryFailureV5:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise ArtifactNonCanonicalV5(reference) from None
    if type(decoded) is not dict:
        raise ArtifactSchemaFailureV5(reference)
    if canonical_json_bytes_v5(decoded) != raw:
        raise ArtifactNonCanonicalV5(reference)
    return decoded


def _exact_keys(
    value: object,
    expected: set[str],
    reference: ArtifactRefV5 | None = None,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ArtifactSchemaFailureV5(reference)
    return value


T = TypeVar("T")


def _constructor_primitive(value: object) -> object:
    """Encode exactly the public constructor surface of immutable contracts."""

    if isinstance(value, Decimal):
        return canonical_primitive_v5(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _constructor_primitive(getattr(value, item.name)) for item in fields(value) if item.init}
    if isinstance(value, tuple):
        return [_constructor_primitive(item) for item in value]
    if isinstance(value, dict):
        return {key: _constructor_primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeError:
            raise ValueError("durable contract bytes must be UTF-8") from None
    if isinstance(value, str):
        return str(value)
    if value is None or type(value) in {bool, int, float}:
        return value
    raise ValueError("durable contract contains an unsupported value")


def _decode_value(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if annotation is Decimal:
        if type(value) is not str:
            raise ValueError
        parsed = Decimal(value)
        if not parsed.is_finite() or canonical_primitive_v5(parsed) != value:
            raise ValueError
        return parsed
    if annotation is bytes:
        if type(value) is not str:
            raise ValueError
        return value.encode("utf-8")
    if annotation is AnnualizedReturnTargetV5:
        primitive = _exact_keys(value, {"target_pct", "metric_id", "basis"})
        raw_target = primitive["target_pct"]
        if type(raw_target) is not str:
            raise ValueError
        parsed = Decimal(raw_target)
        if not parsed.is_finite() or canonical_primitive_v5(parsed) != raw_target:
            raise ValueError
        return AnnualizedReturnTargetV5(
            target_pct=parsed.quantize(Decimal("0.01")),
            metric_id=primitive["metric_id"],  # type: ignore[arg-type]
            basis=primitive["basis"],  # type: ignore[arg-type]
        )
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in arguments):
            raise ValueError
        return value
    if origin in {types.UnionType, __import__("typing").Union}:
        if value is None and type(None) in arguments:
            return None
        failures = 0
        result: object | None = None
        for argument in arguments:
            if argument is type(None):
                continue
            try:
                candidate = _decode_value(argument, value)
            except (TypeError, ValueError, ArithmeticError):
                failures += 1
                continue
            if result is not None:
                raise ValueError
            result = candidate
        if result is None or failures == len(arguments):
            raise ValueError
        return result
    if origin is tuple:
        if type(value) is not list:
            raise ValueError
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode_value(arguments[0], item) for item in value)
        if len(arguments) != len(value):
            raise ValueError
        return tuple(_decode_value(expected, item) for expected, item in zip(arguments, value, strict=True))
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(annotation, value)
    if annotation in {str, int, bool}:
        if type(value) is not annotation:
            raise ValueError
        return value
    if annotation is object:
        canonical_json_bytes_v5(value)
        return value
    raise TypeError


def _decode_dataclass(cls: type[T], value: object) -> T:
    primitive = _exact_keys(value, {item.name for item in fields(cls) if item.init})
    hints = get_type_hints(cls)
    decoded = {item.name: _decode_value(hints[item.name], primitive[item.name]) for item in fields(cls) if item.init}
    return cls(**decoded)


def _decode_role_ledger_terminal(value: object) -> RoleLedgerTerminalV5:
    primitive = _exact_keys(
        value,
        {"schema_version", "reservation_sha256", "facts", "receipt", "artifact"},
    )
    facts = _decode_role_attempt(primitive["facts"])
    artifact_value = primitive["artifact"]
    artifact = (
        None
        if artifact_value is None
        else decode_parsed_role_artifact_v5(
            role=facts.role,
            primitive={"role": facts.role, "artifact": artifact_value},
        )
    )
    return RoleLedgerTerminalV5(
        schema_version=_decode_value(Literal[5], primitive["schema_version"]),  # type: ignore[arg-type]
        reservation_sha256=_decode_value(str, primitive["reservation_sha256"]),  # type: ignore[arg-type]
        facts=facts,
        receipt=_decode_dataclass(RoleTerminalReceiptV5, primitive["receipt"]),
        artifact=artifact,
    )


def _decode_role_ledger_reservation(
    value: object,
    *,
    request: RoleRequestV5,
) -> RoleLedgerReservationV5:
    """Decode only the current V3 reservation shape."""

    if type(value) is not dict:
        raise ArtifactSchemaFailureV5()
    current_keys = {
        "schema_version",
        "record_schema_revision",
        "ledger_ordinal",
        "campaign_id",
        "campaign_manifest_sha256",
        "ledger_identity_sha256",
        "audit_store_identity_sha256",
        "slot",
        "invocation_claim",
    }
    primitive = _exact_keys(value, current_keys)
    _decode_value(Literal[5], primitive["schema_version"])
    if primitive["record_schema_revision"] != 3:
        raise ArtifactSchemaFailureV5()
    campaign_id = _decode_value(str, primitive["campaign_id"])
    _safe_component(campaign_id, "role-ledger campaign")
    for field_name in (
        "campaign_manifest_sha256",
        "ledger_identity_sha256",
        "audit_store_identity_sha256",
    ):
        digest = _decode_value(str, primitive[field_name])
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ArtifactSchemaFailureV5()
    ordinal = _decode_value(int, primitive["ledger_ordinal"])
    if ordinal < 1:
        raise ArtifactSchemaFailureV5()
    authorized_slot_value = primitive["slot"]
    if type(authorized_slot_value) is not dict:
        raise ArtifactSchemaFailureV5()
    authorized_slot_keys = {
        "slot_id",
        "request",
        "authorization_sha256",
        "prior_external_attempts",
        "prior_total_tokens",
        "prior_cost_usd",
        "prior_terminal_sequence",
        "input_tokens_upper_bound",
        "cost_upper_bound_usd",
    }
    authorized_slot_primitive = _exact_keys(authorized_slot_value, authorized_slot_keys)
    slot_value = authorized_slot_primitive["request"]
    if type(slot_value) is not dict:
        raise ArtifactSchemaFailureV5()
    slot_keys = {
        "request_sha256",
        "role",
        "attempt_kind",
        "attempt_index",
        "model",
        "max_output_tokens",
        "response_schema_sha256",
    }
    slot_primitive = _exact_keys(slot_value, slot_keys)
    decoded_authorized_slot = dict(authorized_slot_primitive)
    decoded_authorized_slot["request"] = slot_primitive
    slot = _decode_dataclass(AuthorizedRoleSlotV5, decoded_authorized_slot)
    if (
        slot.request.request_sha256 != request.sha256
        or slot.request.role != request.role
        or slot.request.max_output_tokens != request.max_output_tokens
        or slot.request.response_schema_sha256 != request.response_schema_sha256
    ):
        raise ArtifactSchemaFailureV5()
    return RoleLedgerReservationV5(
        schema_version=_decode_value(Literal[5], primitive["schema_version"]),  # type: ignore[arg-type]
        record_schema_revision=3,
        ledger_ordinal=_decode_value(int, primitive["ledger_ordinal"]),  # type: ignore[arg-type]
        campaign_id=_decode_value(str, primitive["campaign_id"]),  # type: ignore[arg-type]
        campaign_manifest_sha256=_decode_value(str, primitive["campaign_manifest_sha256"]),  # type: ignore[arg-type]
        ledger_identity_sha256=_decode_value(str, primitive["ledger_identity_sha256"]),  # type: ignore[arg-type]
        audit_store_identity_sha256=_decode_value(str, primitive["audit_store_identity_sha256"]),  # type: ignore[arg-type]
        slot=slot,
        invocation_claim=_decode_dataclass(RoleInvocationClaimV5, primitive["invocation_claim"]),
    )


def _decode_role_request(value: object) -> RoleRequestV5:
    primitive = _exact_keys(
        value,
        {
            "role",
            "role_input",
            "role_evidence",
            "expected_binding",
            "max_output_tokens",
            "schema_authority",
        },
    )
    hints = get_type_hints(RoleRequestV5)
    return RoleRequestV5(
        role=_decode_value(hints["role"], primitive["role"]),  # type: ignore[arg-type]
        role_input=_decode_value(RoleInputV5, primitive["role_input"]),  # type: ignore[arg-type]
        role_evidence=_decode_value(hints["role_evidence"], primitive["role_evidence"]),  # type: ignore[arg-type]
        expected_binding=_decode_value(hints["expected_binding"], primitive["expected_binding"]),  # type: ignore[arg-type]
        max_output_tokens=_decode_value(int, primitive["max_output_tokens"]),  # type: ignore[arg-type]
        schema_authority=_decode_value(RoleSchemaAuthorityV5, primitive["schema_authority"]),  # type: ignore[arg-type]
    )


def _decode_role_attempt(value: object) -> RoleAttemptFactsV5:
    primitive = _exact_keys(
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
    )
    hints = get_type_hints(RoleAttemptFactsV5)
    failure_value = primitive["failure_code"]
    if failure_value is not None and type(failure_value) is not str:
        raise ValueError
    return RoleAttemptFactsV5(
        role=_decode_value(hints["role"], primitive["role"]),  # type: ignore[arg-type]
        attempt_kind=_decode_value(hints["attempt_kind"], primitive["attempt_kind"]),  # type: ignore[arg-type]
        attempt_index=_decode_value(int, primitive["attempt_index"]),  # type: ignore[arg-type]
        request_sha256=_decode_value(str, primitive["request_sha256"]),  # type: ignore[arg-type]
        slot_id=_decode_value(str | None, primitive["slot_id"]),  # type: ignore[arg-type]
        outcome=_decode_value(hints["outcome"], primitive["outcome"]),  # type: ignore[arg-type]
        failure_code=None if failure_value is None else RoleFailureCode(failure_value),
        usage=_decode_value(hints["usage"], primitive["usage"]),  # type: ignore[arg-type]
        response_sha256=_decode_value(str | None, primitive["response_sha256"]),  # type: ignore[arg-type]
        artifact_sha256=_decode_value(str | None, primitive["artifact_sha256"]),  # type: ignore[arg-type]
    )


def _decode_literal(value: object) -> object:
    primitive = _exact_keys(value, {"kind", "value"})
    kind = primitive["kind"]
    raw = primitive["value"]
    if kind == "bool" and type(raw) is bool:
        return raw
    if kind == "int" and type(raw) is int:
        return raw
    if kind == "str" and type(raw) is str:
        return raw
    if kind == "float" and type(raw) is float:
        if raw == raw and abs(raw) != float("inf"):
            return raw
    raise ArtifactSchemaFailureV5()


def _decode_assignment(value: object) -> VariantAssignmentV5:
    primitive = _exact_keys(value, {"values"})
    rows = primitive["values"]
    if type(rows) is not list:
        raise ArtifactSchemaFailureV5()
    decoded: list[tuple[str, object]] = []
    for row in rows:
        item = _exact_keys(row, {"name", "value"})
        if type(item["name"]) is not str:
            raise ArtifactSchemaFailureV5()
        decoded.append((item["name"], _decode_literal(item["value"])))
    return VariantAssignmentV5(values=tuple(decoded))  # type: ignore[arg-type]


def _decode_policy_revision(value: object) -> PolicyRevisionIdentityV5:
    primitive = _exact_keys(
        value,
        {
            "policy_interface_version",
            "trusted_policy_runtime_sha256",
            "immutable_constraints_sha256",
            "editable_source_sha256",
        },
    )
    hashes = primitive["editable_source_sha256"]
    if type(hashes) is not list or any(
        type(item) is not list or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not str
        for item in hashes
    ):
        raise ArtifactSchemaFailureV5()
    return PolicyRevisionIdentityV5(
        policy_interface_version=primitive["policy_interface_version"],  # type: ignore[arg-type]
        trusted_policy_runtime_sha256=primitive["trusted_policy_runtime_sha256"],  # type: ignore[arg-type]
        immutable_constraints_sha256=primitive["immutable_constraints_sha256"],  # type: ignore[arg-type]
        editable_source_sha256=tuple((item[0], item[1]) for item in hashes),
    )


def _decode_source_bundle(value: object) -> SourceBundleV5:
    primitive = _exact_keys(value, {"files"})
    files = primitive["files"]
    if type(files) is not list:
        raise ArtifactSchemaFailureV5()
    return SourceBundleV5(files=tuple(_decode_dataclass(SourceFileV5, item) for item in files))


def _decode_template(value: object) -> StructuralTemplateV5:
    primitive = _exact_keys(
        value,
        {
            "hypothesis_id",
            "parent_revision_sha256",
            "changed_symbols",
            "source_operations",
            "axes",
            "full_source_escape",
        },
    )
    changed_symbols = primitive["changed_symbols"]
    operations = primitive["source_operations"]
    axes = primitive["axes"]
    full_source = primitive["full_source_escape"]
    if (
        type(changed_symbols) is not list
        or any(type(item) is not str for item in changed_symbols)
        or type(operations) is not list
        or type(axes) is not list
        or (full_source is not None and type(full_source) is not list)
    ):
        raise ArtifactSchemaFailureV5()
    decoded_axes: list[LiteralAxisV5] = []
    for axis_value in axes:
        axis = _exact_keys(axis_value, {"name", "default", "values"})
        values = axis["values"]
        if type(axis["name"]) is not str or type(values) is not list:
            raise ArtifactSchemaFailureV5()
        decoded_axes.append(
            LiteralAxisV5(
                name=axis["name"],
                default=_decode_literal(axis["default"]),  # type: ignore[arg-type]
                values=tuple(_decode_literal(item) for item in values),  # type: ignore[arg-type]
            )
        )
    return StructuralTemplateV5(
        hypothesis_id=primitive["hypothesis_id"],  # type: ignore[arg-type]
        parent_revision_sha256=primitive["parent_revision_sha256"],  # type: ignore[arg-type]
        changed_symbols=tuple(changed_symbols),
        source_operations=tuple(_decode_dataclass(SourceOperationV5, item) for item in operations),
        axes=tuple(decoded_axes),
        full_source_escape=(
            None if full_source is None else tuple(_decode_dataclass(SourceFileV5, item) for item in full_source)
        ),
    )


def _decode_semantic_fingerprint(value: object) -> SemanticFingerprintV5:
    primitive = _exact_keys(value, {"suite_id", "observations", "fingerprint_sha256"})
    observations = primitive["observations"]
    if type(observations) is not list:
        raise ArtifactSchemaFailureV5()
    decoded: list[ProbeObservationV5] = []
    for observation_value in observations:
        observation = _exact_keys(
            observation_value,
            {"probe_id", "method", "input_sha256", "decision_json_utf8"},
        )
        if any(type(observation[key]) is not str for key in observation):
            raise ArtifactSchemaFailureV5()
        decoded.append(
            ProbeObservationV5(
                probe_id=observation["probe_id"],
                method=observation["method"],
                input_sha256=observation["input_sha256"],
                decision_json=observation["decision_json_utf8"].encode("utf-8"),
            )
        )
    return SemanticFingerprintV5(
        suite_id=primitive["suite_id"],  # type: ignore[arg-type]
        observations=tuple(decoded),
        fingerprint_sha256=primitive["fingerprint_sha256"],  # type: ignore[arg-type]
    )


def _decode_identity(kind: object, value: object) -> ExperimentIdentityV5 | PreValidationInvalidExperimentIdentityV5:
    keys = {
        "policy_revision_sha256",
        "parent_revision_sha256",
        "hypothesis_sha256",
        "template_sha256",
        "assignment_sha256",
        "round_index",
        "discovery_plan_sha256",
    }
    primitive = _exact_keys(value, keys)
    if kind == "post_validation":
        return ExperimentIdentityV5(**primitive)  # type: ignore[arg-type]
    if kind == "pre_validation_invalid":
        if primitive["policy_revision_sha256"] is not None:
            raise ArtifactSchemaFailureV5()
        del primitive["policy_revision_sha256"]
        return PreValidationInvalidExperimentIdentityV5(**primitive)  # type: ignore[arg-type]
    raise ArtifactSchemaFailureV5()


def _decode_experiment_record(value: object) -> ExperimentRecordV5:
    keys = {
        "schema_version",
        "experiment_id",
        "experiment_identity_kind",
        "experiment_identity",
        "round_index",
        "parent_revision_sha256",
        "parent_semantic_fingerprint_sha256",
        "hypothesis",
        "template",
        "template_sha256",
        "variant_assignment",
        "policy_revision",
        "semantic_fingerprint",
        "status",
        "validation",
        "quick_evidence",
        "discovery_episodes",
        "campaign_evidence",
        "target_gap_pct",
        "critic_review",
        "artifact_refs",
        "critic_artifact_ref",
    }
    primitive = _exact_keys(value, keys)
    if primitive["schema_version"] != 5 or type(primitive["schema_version"]) is not int:
        raise ArtifactSchemaFailureV5()
    template = _decode_template(primitive["template"])
    assignment = _decode_assignment(primitive["variant_assignment"])
    policy_value = primitive["policy_revision"]
    semantic_value = primitive["semantic_fingerprint"]
    quick_value = primitive["quick_evidence"]
    episode_values = primitive["discovery_episodes"]
    campaign_value = primitive["campaign_evidence"]
    target_gap = primitive["target_gap_pct"]
    critic_value = primitive["critic_review"]
    ref_values = primitive["artifact_refs"]
    critic_ref_value = primitive["critic_artifact_ref"]
    if type(episode_values) is not list or type(ref_values) is not list:
        raise ArtifactSchemaFailureV5()
    return ExperimentRecordV5(
        experiment_id=primitive["experiment_id"],  # type: ignore[arg-type]
        experiment_identity=_decode_identity(primitive["experiment_identity_kind"], primitive["experiment_identity"]),
        round_index=primitive["round_index"],  # type: ignore[arg-type]
        parent_revision_sha256=primitive["parent_revision_sha256"],  # type: ignore[arg-type]
        parent_semantic_fingerprint_sha256=primitive["parent_semantic_fingerprint_sha256"],  # type: ignore[arg-type]
        hypothesis=_decode_dataclass(HypothesisV5, primitive["hypothesis"]),
        template=template,
        template_sha256=primitive["template_sha256"],  # type: ignore[arg-type]
        variant_assignment=assignment,
        policy_revision=(None if policy_value is None else _decode_policy_revision(policy_value)),
        semantic_fingerprint=(None if semantic_value is None else _decode_semantic_fingerprint(semantic_value)),
        status=primitive["status"],  # type: ignore[arg-type]
        validation=_decode_dataclass(ValidationResultV5, primitive["validation"]),
        quick_evidence=(None if quick_value is None else _decode_dataclass(PanelEvaluationV5, quick_value)),
        discovery_episodes=tuple(_decode_dataclass(EpisodeEvaluationV5, item) for item in episode_values),
        campaign_evidence=(None if campaign_value is None else _decode_dataclass(CampaignEvidenceV5, campaign_value)),
        target_gap_pct=(None if target_gap is None else _decode_value(Decimal, target_gap)),  # type: ignore[arg-type]
        critic_review=(None if critic_value is None else _decode_dataclass(CriticReviewV5, critic_value)),
        artifact_refs=tuple(_decode_dataclass(ArtifactRefV5, item) for item in ref_values),
        critic_artifact_ref=(None if critic_ref_value is None else _decode_dataclass(ArtifactRefV5, critic_ref_value)),
    )


def _decode_round_event(value: object) -> RoundEventV5:
    primitive = _exact_keys(
        value,
        {
            "schema_version",
            "campaign_id",
            "round_index",
            "sequence",
            "prior_event_sha256",
            "event_kind",
            "experiment_id",
            "payload_ref",
        },
    )
    if primitive["schema_version"] != 5 or type(primitive["schema_version"]) is not int:
        raise ArtifactSchemaFailureV5()
    return RoundEventV5(
        campaign_id=primitive["campaign_id"],  # type: ignore[arg-type]
        round_index=primitive["round_index"],  # type: ignore[arg-type]
        sequence=primitive["sequence"],  # type: ignore[arg-type]
        prior_event_sha256=primitive["prior_event_sha256"],  # type: ignore[arg-type]
        event_kind=primitive["event_kind"],  # type: ignore[arg-type]
        experiment_id=primitive["experiment_id"],  # type: ignore[arg-type]
        payload_ref=_decode_dataclass(ArtifactRefV5, primitive["payload_ref"]),
    )


def _decode_rendered_variant(value: object) -> RenderedVariantV5:
    primitive = _exact_keys(value, {"assignment", "source_bundle", "policy_revision"})
    return RenderedVariantV5(
        assignment=_decode_assignment(primitive["assignment"]),
        source_bundle=_decode_source_bundle(primitive["source_bundle"]),
        policy_revision=_decode_policy_revision(primitive["policy_revision"]),
    )


def _decode_round_payload(expected_kind: str, value: object) -> RoundEventPayloadV5:
    envelope = _exact_keys(value, {"schema_version", "payload_kind", "payload"})
    if (
        type(envelope["schema_version"]) is not int
        or envelope["schema_version"] != 5
        or envelope["payload_kind"] != expected_kind
    ):
        raise ArtifactSchemaFailureV5()
    body = envelope["payload"]
    if expected_kind == "round_intent":
        return _decode_dataclass(RoundIntentPayloadV5, body)
    if expected_kind == "role_completion":
        return _decode_dataclass(RoleCompletionPayloadV5, body)
    if expected_kind == "rendered_variant":
        item = _exact_keys(body, {"variant"})
        return RenderedVariantPayloadV5(variant=_decode_rendered_variant(item["variant"]))
    if expected_kind == "candidate_stage_result":
        item = _exact_keys(
            body,
            {
                "experiment_id",
                "stage",
                "stage_index",
                "outcome",
                "validation",
                "semantic_fingerprint",
                "failure_code",
                "failure_ref",
                "episode_ordinal",
            },
        )
        return CandidateStageResultPayloadV5(
            experiment_id=item["experiment_id"],  # type: ignore[arg-type]
            stage=item["stage"],  # type: ignore[arg-type]
            stage_index=item["stage_index"],  # type: ignore[arg-type]
            outcome=item["outcome"],  # type: ignore[arg-type]
            validation=(
                None if item["validation"] is None else _decode_dataclass(ValidationResultV5, item["validation"])
            ),
            semantic_fingerprint=(
                None
                if item["semantic_fingerprint"] is None
                else _decode_semantic_fingerprint(item["semantic_fingerprint"])
            ),
            failure_code=item["failure_code"],  # type: ignore[arg-type]
            failure_ref=(
                None if item["failure_ref"] is None else _decode_dataclass(ArtifactRefV5, item["failure_ref"])
            ),
            episode_ordinal=item["episode_ordinal"],  # type: ignore[arg-type]
        )
    if expected_kind == "quick_evidence":
        item = _exact_keys(body, {"experiment_id", "semantic_fingerprint", "evaluation"})
        return QuickEvidencePayloadV5(
            experiment_id=item["experiment_id"],  # type: ignore[arg-type]
            semantic_fingerprint=_decode_semantic_fingerprint(item["semantic_fingerprint"]),
            evaluation=_decode_dataclass(PanelEvaluationV5, item["evaluation"]),
        )
    if expected_kind == "episode_evidence":
        item = _exact_keys(body, {"experiment_id", "episode"})
        return EpisodeEvidencePayloadV5(
            experiment_id=item["experiment_id"],  # type: ignore[arg-type]
            episode=_decode_dataclass(EpisodeEvaluationV5, item["episode"]),
        )
    if expected_kind == "candidate_execution":
        return _decode_dataclass(CandidateExecutionAuthorityV5, body)
    if expected_kind == "resource_lease":
        return _decode_dataclass(ResourceLeasePayloadV5, body)
    if expected_kind == "cleanup_result":
        return _decode_dataclass(CleanupResultPayloadV5, body)
    if expected_kind == "round_outcome":
        return _decode_dataclass(RoundOutcomePayloadV5, body)
    raise ArtifactSchemaFailureV5()


def _extract_artifact_refs(value: object) -> tuple[ArtifactRefV5, ...]:
    found: dict[tuple[str, str], ArtifactRefV5] = {}

    def visit(item: object) -> None:
        if type(item) is dict:
            if set(item) == {"relative_path", "sha256"}:
                try:
                    reference = _decode_dataclass(ArtifactRefV5, item)
                except (TypeError, ValueError):
                    raise ArtifactSchemaFailureV5() from None
                found[(reference.relative_path, reference.sha256)] = reference
                return
            for child in item.values():
                visit(child)
        elif type(item) is list:
            for child in item:
                visit(child)

    visit(value)
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True, slots=True)
class ArchiveSnapshotV5:
    generation: int
    record_refs: tuple[ArtifactRefV5, ...]
    projection: object

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("archive generation is invalid")
        if type(self.record_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.record_refs):
            raise ValueError("archive record references are invalid")
        keys = tuple((item.relative_path, item.sha256) for item in self.record_refs)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("archive record references must be canonical")
        canonical_json_bytes_v5(self.projection)

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": 5,
            "artifact_type": "archive",
            "generation": self.generation,
            "record_refs": [item.to_primitive() for item in self.record_refs],
            "projection": self.projection,
        }


@dataclass(frozen=True, slots=True)
class RepositoryCheckpointV5:
    generation: int
    archive_sha256: str
    record_refs: tuple[ArtifactRefV5, ...]

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("checkpoint generation is invalid")
        ArtifactRefV5("archive.json", self.archive_sha256)
        if type(self.record_refs) is not tuple or any(type(item) is not ArtifactRefV5 for item in self.record_refs):
            raise ValueError("checkpoint record references are invalid")
        keys = tuple((item.relative_path, item.sha256) for item in self.record_refs)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("checkpoint record references must be canonical")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": 5,
            "artifact_type": "checkpoint",
            "generation": self.generation,
            "archive_sha256": self.archive_sha256,
            "record_refs": [item.to_primitive() for item in self.record_refs],
        }


@dataclass(frozen=True, slots=True)
class PersistedRoleInvocationV5:
    payload: RoleCompletionPayloadV5
    call: RoleCallKeyV5
    package: RoleInvocationPackageV5

    def __post_init__(self) -> None:
        if (
            type(self.payload) is not RoleCompletionPayloadV5
            or type(self.call) is not RoleCallKeyV5
            or type(self.package) is not RoleInvocationPackageV5
        ):
            raise ValueError("persisted role invocation is invalid")
        artifact_sha256 = None if self.package.artifact is None else canonical_sha256_v5(self.package.artifact)
        expected = (
            self.call.sha256,
            self.package.request.role,
            self.package.attempt.outcome,
            self.package.request.sha256,
            self.package.binding_sha256,
            self.package.attempt.sha256,
            self.package.terminal_authority_sha256,
            artifact_sha256,
            self.package.sha256,
        )
        actual = (
            self.payload.call_key_sha256,
            self.payload.role,
            self.payload.outcome,
            self.payload.request_sha256,
            self.payload.binding_sha256,
            self.payload.attempt_sha256,
            self.payload.terminal_authority_sha256,
            self.payload.artifact_sha256,
            self.payload.package_sha256,
        )
        if actual != expected:
            raise ValueError("role completion differs from its durable package")
        if (
            self.package.call != self.call
            or self.call.campaign_id != self.payload.campaign_id
            or self.call.round_index != self.payload.round_index
            or self.call.role_position != self.payload.role_position
            or self.call.role != self.package.request.role
            or self.call.attempt_kind != self.package.attempt.attempt_kind
            or self.call.attempt_index != self.package.attempt.attempt_index
            or self.call.request_sha256 != self.package.request.sha256
        ):
            raise ValueError("role completion call key differs from its package")


@dataclass(frozen=True, slots=True)
class AdapterStateAuthorityV5:
    """Create-only authority binding an adapter state slot to its exact bytes."""

    schema_version: Literal[5]
    namespace: str
    key: str
    value_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        namespace = _safe_component(self.namespace, "adapter state authority namespace")
        key = _safe_component(self.key, "adapter state authority key")
        if (
            self.schema_version != 5
            or type(self.value_ref) is not ArtifactRefV5
            or self.value_ref.relative_path != f"adapter-state/{namespace}/{key}.json"
        ):
            raise ValueError("adapter state authority is invalid")


class LocalArtifactRepositoryV5:
    """Closed-layout repository with immutable evidence and atomic projections."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise ValueError("optimizer V5 artifact root must be absolute")
        candidate = Path(os.path.abspath(candidate))
        try:
            with _acquire_absolute_directory(candidate) as access:
                self._root_identity = access.identity
        except (OSError, ValueError):
            raise ValueError("optimizer V5 artifact root is invalid") from None
        self._root = candidate

    @property
    def root(self) -> Path:
        return self._root

    @property
    def root_identity_sha256(self) -> str:
        return canonical_sha256_v5({"device": self._root_identity[0], "inode": self._root_identity[1]})

    def _directory(self, parts: tuple[str, ...], *, create: bool):
        try:
            return _acquire_directory(
                self._root,
                parts,
                create=create,
                expected_root_identity=self._root_identity,
            )
        except (OSError, ValueError):
            if not create:
                try:
                    os.lstat(self._root.joinpath(*parts))
                except FileNotFoundError:
                    raise ArtifactMissingV5() from None
                except OSError:
                    pass
            raise ArtifactRelocatedV5(ArtifactRefV5("repository.json", "0" * 64), "repository.json") from None

    def _read_relative(self, relative_path: str) -> bytes:
        parts = _safe_relative_path(relative_path)
        try:
            with self._directory(tuple(parts[:-1]), create=False) as directory:
                name = parts[-1]
                if not directory.entry_exists(name):
                    raise ArtifactMissingV5()
                if os.name == "nt":
                    before = os.lstat(directory.path / name)
                    if _is_link_or_reparse(directory.path / name):
                        raise ArtifactRelocatedV5(ArtifactRefV5(relative_path, "0" * 64), relative_path)
                    descriptor = os.open(
                        directory.path / name,
                        os.O_RDONLY | getattr(os, "O_BINARY", 0),
                    )
                else:
                    before = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
                    descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_BINARY", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory.descriptor,
                    )
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or opened.st_size > _MAX_ARTIFACT_BYTES
                        or _metadata_identity(opened) != _metadata_identity(before)
                    ):
                        raise ArtifactRelocatedV5(ArtifactRefV5(relative_path, "0" * 64), relative_path)
                    chunks: list[bytes] = []
                    remaining = _MAX_ARTIFACT_BYTES + 1
                    while remaining:
                        chunk = os.read(descriptor, min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    if len(raw) > _MAX_ARTIFACT_BYTES:
                        raise ArtifactSchemaFailureV5()
                finally:
                    os.close(descriptor)
                if os.name == "nt":
                    after = os.lstat(directory.path / name)
                else:
                    after = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
                if _metadata_identity(before) != _metadata_identity(after) or _metadata_identity(
                    opened
                ) != _metadata_identity(after):
                    raise ArtifactRelocatedV5(ArtifactRefV5(relative_path, "0" * 64), relative_path)
                return raw
        except ArtifactRepositoryFailureV5:
            raise
        except FileNotFoundError:
            raise ArtifactMissingV5() from None
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(ArtifactRefV5(relative_path, "0" * 64), relative_path) from None

    def _find_digest(self, sha256: str, expected_path: str) -> str | None:
        def walk(directory: Path, prefix: tuple[str, ...]) -> str | None:
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                return None
            for entry in entries:
                try:
                    _safe_component(entry.name, "artifact entry")
                    metadata = entry.stat(follow_symlinks=False)
                    attributes = getattr(metadata, "st_file_attributes", 0)
                    reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                    if entry.is_symlink() or reparse:
                        continue
                    parts = (*prefix, entry.name)
                    relative = PurePosixPath(*parts).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        located = walk(Path(entry.path), parts)
                        if located is not None:
                            return located
                    elif (
                        relative != expected_path
                        and entry.is_file(follow_symlinks=False)
                        and hashlib.sha256(self._read_relative(relative)).hexdigest() == sha256
                    ):
                        return relative
                except (OSError, ValueError, ArtifactRepositoryFailureV5):
                    continue
            return None

        return walk(self._root, ())

    def authenticate(self, reference: ArtifactRefV5) -> AuthenticatedArtifactV5:
        try:
            return self.authenticate_exact(reference)
        except ArtifactMissingV5:
            relocated = self._find_digest(reference.sha256, reference.relative_path)
            if relocated is not None:
                raise ArtifactRelocatedV5(reference, relocated) from None
            raise ArtifactMissingV5(reference) from None

    def authenticate_exact(self, reference: ArtifactRefV5) -> AuthenticatedArtifactV5:
        """Authenticate only the named JSON edge; never search for relocation."""
        if type(reference) is not ArtifactRefV5:
            raise ValueError("artifact authentication requires a V5 reference")
        _safe_relative_path(reference.relative_path)
        try:
            raw = self._read_relative(reference.relative_path)
        except ArtifactMissingV5:
            raise ArtifactMissingV5(reference) from None
        actual = hashlib.sha256(raw).hexdigest()
        if actual != reference.sha256:
            raise ArtifactDigestMismatchV5(reference, actual)
        primitive = _strict_json_object(raw, reference)
        return AuthenticatedArtifactV5(
            reference=reference,
            content=raw,
            child_references=_extract_artifact_refs(primitive),
        )

    def authenticate_raw_artifact(self, reference: ArtifactRefV5) -> AuthenticatedRawArtifactV5:
        """Stream-authenticate one explicitly typed non-JSON campaign data edge."""

        if type(reference) is not ArtifactRefV5:
            raise ValueError("raw artifact authentication requires a V5 reference")
        return self._authenticate_raw_campaign_edge(reference)

    def read_stage_ledger(self, relative_path: str) -> bytes:
        """Read only an explicitly selected canonical V5 stage ledger."""
        if relative_path not in {"panels/confirmation-retirement.json", "panels/qualification-retirement.json"}:
            raise ValueError("stage ledger path is not canonical")
        return self._read_relative(relative_path)

    def load_confirmation_record(self, relative_path: str, *, value_type: type[T]) -> tuple[ArtifactRefV5, T] | None:
        """Recover an exact attempt-owned immutable record, without path searches."""
        parts = _safe_relative_path(relative_path)
        if len(parts) != 3 or parts[0] != "confirmation" or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None:
            raise ValueError("confirmation record path is not attempt-owned")
        try:
            raw = self._read_relative(relative_path)
        except ArtifactMissingV5:
            return None
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        return reference, self.load_typed_artifact(reference, value_type=value_type)

    def load_qualification_record(self, relative_path: str, *, value_type: type[T]) -> tuple[ArtifactRefV5, T] | None:
        """Recover only an exact qualification-owned immutable record."""
        parts = _safe_relative_path(relative_path)
        if len(parts) != 3 or parts[0] != "qualification" or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None:
            raise ValueError("qualification record path is not attempt-owned")
        try:
            raw = self._read_relative(relative_path)
        except ArtifactMissingV5:
            return None
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        return reference, self.load_typed_artifact(reference, value_type=value_type)

    def append_stage_ledger(self, relative_path: str, *, prior: bytes, event: bytes) -> None:
        """Atomically publish a byte-preserving append while the stage lock is held."""
        if not prior.endswith(b"\n") or not event.endswith(b"\n") or self.read_stage_ledger(relative_path) != prior:
            raise ValueError("stage ledger changed before append")
        parts = _safe_relative_path(relative_path)
        with self._directory(tuple(parts[:-1]), create=False) as directory:
            _atomic_replace_in_directory(directory, parts[-1], prior + event)

    def load_confirmation_champion(
        self,
        *,
        checkpoint_ref: ArtifactRefV5,
        archive_ref: ArtifactRefV5,
        finalized_campaign_ref: ArtifactRefV5,
        discovery_manifest_ref: ArtifactRefV5,
    ):
        """Reconstruct the whole explicitly finalized discovery projection, read-only."""
        from core.pit_optimizer_v5.search import ArchiveRecordAuthorityV5, CandidateArchiveReducerV5

        finalization = self.load_typed_artifact(finalized_campaign_ref, value_type=FinalizedDiscoveryCampaignV5)
        manifest = self.load_typed_artifact(discovery_manifest_ref, value_type=CampaignManifestV5)
        if (
            finalization.discovery_manifest_ref != discovery_manifest_ref
            or finalization.checkpoint_ref != checkpoint_ref
            or finalization.archive_ref != archive_ref
            or len(finalization.rounds) != manifest.search.max_feedback_rounds
        ):
            raise ArtifactSchemaFailureV5(finalized_campaign_ref)
        final_round = len(finalization.rounds)
        checkpoint = _strict_json_object(self.authenticate(checkpoint_ref).content, checkpoint_ref)
        archive = _strict_json_object(self.authenticate(archive_ref).content, archive_ref)
        _exact_keys(
            checkpoint,
            {"schema_version", "artifact_type", "generation", "archive_sha256", "record_refs"},
            checkpoint_ref,
        )
        _exact_keys(
            archive, {"schema_version", "artifact_type", "generation", "record_refs", "projection"}, archive_ref
        )
        if (
            checkpoint["schema_version"] != 5
            or checkpoint["artifact_type"] != "checkpoint"
            or archive["schema_version"] != 5
            or archive["artifact_type"] != "archive"
            or checkpoint["archive_sha256"] != archive_ref.sha256
            or checkpoint["generation"] != final_round
            or archive["generation"] != final_round
            or checkpoint["record_refs"] != archive["record_refs"]
        ):
            raise ArtifactSchemaFailureV5(checkpoint_ref)
        references = tuple(_decode_dataclass(ArtifactRefV5, item) for item in checkpoint["record_refs"])
        closed_checkpoint = RepositoryCheckpointV5(final_round, archive_ref.sha256, references)
        if canonical_json_bytes_v5(closed_checkpoint.to_primitive()) != self.authenticate(checkpoint_ref).content:
            raise ArtifactSchemaFailureV5(checkpoint_ref)
        records = self._verify_record_refs(references)
        closed_refs = tuple(
            sorted(
                (ref for item in finalization.rounds for ref in item.record_refs),
                key=lambda item: (item.relative_path, item.sha256),
            )
        )
        if references != closed_refs:
            raise ArtifactSchemaFailureV5(finalized_campaign_ref)
        self._verify_finalized_discovery_rounds(finalization, manifest, records)
        source_authorities = []
        for item in records:
            if item.record.status not in {"evaluated", "zero_trade"}:
                continue
            policy = item.record.policy_revision
            if policy is None:
                raise ArtifactSchemaFailureV5(item.reference)
            # Resolve the exact source edge already present in the immutable
            # experiment. Never repair or consult mutable search-source indexes.
            source_refs = tuple(
                ref
                for ref in item.record.artifact_refs
                if ref.relative_path == f"adapter-state/policy-source/{policy.sha256}.json"
            )
            if len(source_refs) != 1:
                raise ArtifactSchemaFailureV5(item.reference)
            source = self.load_typed_artifact(source_refs[0], value_type=SourceBundleV5)
            source_authorities.append(
                ArchiveRecordAuthorityV5(item.record.experiment_id, item.reference, source, source_refs[0])
            )
        reducer = CandidateArchiveReducerV5(
            self.load_typed_artifact(manifest.panel_plan_ref, value_type=CampaignPanelPlanV5),
            self.load_typed_artifact(manifest.evaluator_contract_ref, value_type=EvaluatorContractV5),
            manifest.target,
            tuple(sorted(source_authorities, key=lambda item: item.experiment_id)),
            manifest.search.archive_capacity,
        )
        state = reduce_experiment_journal_v5(tuple(item.record for item in records), reducer)
        expected_archive = ArchiveSnapshotV5(final_round, references, reducer.to_primitive(state))
        if canonical_json_bytes_v5(expected_archive.to_primitive()) != self.authenticate(archive_ref).content:
            raise ArtifactSchemaFailureV5(archive_ref)
        if (
            not state.archive.entries
            or state.next_round_index != final_round + 1
            or not records
            or max(item.record.round_index for item in records) != final_round
        ):
            raise ArtifactSchemaFailureV5(archive_ref)
        champion = state.archive.entries[0]
        matches = tuple(item.record for item in records if item.reference == champion.experiment_record_ref)
        if len(matches) != 1:
            raise ArtifactSchemaFailureV5(archive_ref)
        record = matches[0]
        if (
            record.status != "evaluated"
            or record.policy_revision != champion.policy_revision
            or record.campaign_evidence != champion.campaign
            or champion.source_bundle_ref not in record.artifact_refs
            or any(entry.campaign_cagr_pct > champion.campaign_cagr_pct for entry in state.archive.entries)
            or any(
                item.record.status == "evaluated"
                and item.record.campaign_evidence is not None
                and item.record.campaign_evidence.closed_trades > 0
                and item.record.campaign_evidence.campaign_cagr_pct > champion.campaign_cagr_pct
                for item in records
            )
        ):
            raise ArtifactSchemaFailureV5(archive_ref)
        return champion, record

    def _verify_finalized_discovery_rounds(
        self,
        finalization: FinalizedDiscoveryCampaignV5,
        manifest: CampaignManifestV5,
        records: tuple[StoredExperimentRecordV5, ...],
    ) -> None:
        """Bind complete round chains, completed role slots, candidates, and cleanup."""
        for closed_round in finalization.rounds:
            events, payloads = [], []
            for sequence, reference in enumerate(closed_round.event_refs):
                expected_path = f"events/{manifest.campaign_id}/{closed_round.round_index:04d}/{sequence:06d}.json"
                if reference.relative_path != expected_path:
                    raise ArtifactSchemaFailureV5(reference)
                raw = self.authenticate(reference).content
                event = _decode_round_event(_strict_json_object(raw, reference))
                if event.sha256 != reference.sha256 or canonical_json_bytes_v5(event.to_primitive()) != raw:
                    raise ArtifactSchemaFailureV5(reference)
                if event.campaign_id != manifest.campaign_id or event.round_index != closed_round.round_index:
                    raise ArtifactSchemaFailureV5(reference)
                events.append(event)
                payloads.append(self.load_round_payload(event.payload_ref, expected_kind=event.event_kind))
            fold_round_events_v5(events=tuple(events), payloads=tuple(payloads))
            current = tuple(item for item in records if item.record.round_index == closed_round.round_index)
            if (
                tuple(item.reference for item in current) != closed_round.record_refs
                or not payloads
                or type(payloads[-1]) is not CleanupResultPayloadV5
                or not payloads[-1].cleanup_complete
                or any(type(item) is RoundOutcomePayloadV5 for item in payloads)
                or {item.experiment_id for item in events if item.event_kind == "rendered_variant"}
                != {item.record.experiment_id for item in current}
            ):
                raise ArtifactSchemaFailureV5(closed_round.event_refs[-1])
            completions = tuple(item for item in payloads if type(item) is RoleCompletionPayloadV5)
            if tuple(item.role for item in completions) != ("investigator", "author", "critic"):
                raise ArtifactSchemaFailureV5(closed_round.event_refs[-1])
            packages = tuple(self.load_role_invocation(completion) for completion in completions)
            for completion, package in zip(completions, packages, strict=True):
                if (
                    completion.outcome != "accepted"
                    or not package.accepted
                    or package.call.campaign_id != manifest.campaign_id
                    or package.call.round_index != closed_round.round_index
                ):
                    raise ArtifactSchemaFailureV5(completion.request_ref)
            intents = tuple(item for item in payloads if type(item) is RoundIntentPayloadV5)
            if len(intents) != 1 or any(
                item.record.parent_revision_sha256 != intents[0].parent_revision_sha256
                or item.record.parent_semantic_fingerprint_sha256 != intents[0].parent_semantic_fingerprint_sha256
                or item.record.hypothesis != intents[0].hypothesis
                or item.record.experiment_identity.discovery_plan_sha256 != intents[0].discovery_plan_sha256
                or item.record.template != packages[1].artifact
                for item in current
            ):
                raise ArtifactSchemaFailureV5(closed_round.event_refs[-1])
            critic = packages[-1].artifact
            if type(critic) is not CriticArtifactV5 or any(
                item.record.critic_artifact_ref is None
                or item.record.critic_artifact_ref.sha256 != critic.sha256
                for item in current
                if is_testable_experiment_status_v5(item.record.status)
            ):
                raise ArtifactSchemaFailureV5(completions[-1].artifact_ref)

    def _create_only_with_status(
        self,
        relative_path: str,
        primitive: object,
    ) -> tuple[ArtifactRefV5, bool]:
        parts = _safe_relative_path(relative_path)
        raw = canonical_json_bytes_v5(primitive)
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        created = True
        try:
            with self._directory(tuple(parts[:-1]), create=True) as directory:
                _write_create_only_in_directory(directory, parts[-1], raw)
        except FileExistsError:
            created = False
            try:
                existing = self.authenticate(reference)
            except ArtifactRepositoryFailureV5:
                raise ArtifactExistsV5(reference) from None
            if existing.content != raw:
                raise ArtifactExistsV5(reference) from None
        except ArtifactRepositoryFailureV5:
            raise
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(reference, relative_path) from None
        return reference, created

    def _create_only(self, relative_path: str, primitive: object) -> ArtifactRefV5:
        return self._create_only_with_status(relative_path, primitive)[0]

    def create_typed_artifact(self, relative_path: str, value: object) -> ArtifactRefV5:
        """Create or authenticate one immutable canonical V5 dataclass artifact."""

        if not is_dataclass(value) or isinstance(value, type):
            raise ValueError("typed artifact value must be a dataclass instance")
        return self._create_only(relative_path, canonical_primitive_v5(value))

    def require_baseline_output_absent(self, relative_path: str) -> None:
        """Preflight an exact output; final exclusive creation also closes races."""
        parts = _safe_relative_path(relative_path)
        if parts[0] != "evaluator" or len(parts) < 2:
            raise ValueError("baseline outputs must belong to the V5 evaluator directory")
        try:
            with self._directory(tuple(parts[:-1]), create=False) as directory:
                if directory.entry_exists(parts[-1]):
                    raise ArtifactExistsV5()
        except ArtifactMissingV5:
            return

    def create_baseline_artifact(self, relative_path: str, value: object) -> ArtifactRefV5:
        """Strict create-only baseline/profile output, including identical retries."""
        parts = _safe_relative_path(relative_path)
        if parts[0] != "evaluator" or len(parts) < 2:
            raise ValueError("baseline outputs must belong to the V5 evaluator directory")
        raw = canonical_json_bytes_v5(value)
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        try:
            with self._directory(tuple(parts[:-1]), create=True) as directory:
                _write_create_only_in_directory(directory, parts[-1], raw)
        except FileExistsError:
            raise ArtifactExistsV5(reference) from None
        return reference

    def create_readiness_record(self, relative_path: str, value: object) -> ArtifactRefV5:
        """Strictly create one readiness record in an existing output directory.

        Unlike retryable stage terminal persistence, an existing readiness record
        is always a blocker, including identical bytes. No parent is created.
        """
        from core.pit_optimizer_v5.readiness import FullReplayReadinessV5

        if type(value) is not FullReplayReadinessV5:
            raise ValueError("readiness requires its closed content-free contract")
        parts = _safe_relative_path(relative_path)
        raw = canonical_json_bytes_v5(value)
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        try:
            with self._directory(tuple(parts[:-1]), create=False) as directory:
                _write_create_only_in_directory(directory, parts[-1], raw)
        except FileExistsError:
            raise ArtifactExistsV5(reference) from None
        return reference

    def create_evaluation_panel_spec(
        self,
        relative_path: str,
        panel: EvaluationPanelSpec,
    ) -> ArtifactRefV5:
        """Create or authenticate the canonical newline-bearing raw panel contract."""

        if type(panel) is not EvaluationPanelSpec:
            raise ValueError("evaluation panel artifact must use the canonical panel schema")
        parts = _safe_relative_path(relative_path)
        raw = _canonical_panel_json_bytes(_panel_json_value(panel))
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        try:
            with self._directory(tuple(parts[:-1]), create=True) as directory:
                _write_create_only_in_directory(directory, parts[-1], raw)
        except FileExistsError:
            try:
                authenticated = self.load_evaluation_panel_spec(reference)
            except ArtifactRepositoryFailureV5:
                raise ArtifactExistsV5(reference) from None
            if authenticated != panel:
                raise ArtifactExistsV5(reference) from None
        except ArtifactRepositoryFailureV5:
            raise
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(reference, relative_path) from None
        return reference

    def _replace(self, relative_path: str, primitive: object) -> ArtifactRefV5:
        if relative_path not in {"archive.json", "checkpoint.json"}:
            raise ValueError("only archive and checkpoint artifacts are replaceable")
        raw = canonical_json_bytes_v5(primitive)
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        try:
            with self._directory((), create=False) as directory:
                _atomic_replace_in_directory(directory, relative_path, raw)
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(reference, relative_path) from None
        return reference

    def append_input(self, *, artifact_kind: str, value: object) -> ArtifactRefV5:
        kind = _safe_component(artifact_kind, "input artifact kind")
        primitive = {
            "schema_version": 5,
            "artifact_type": kind,
            "payload": canonical_primitive_v5(value),
        }
        digest = hashlib.sha256(canonical_json_bytes_v5(primitive)).hexdigest()
        return self._create_only(f"inputs/{kind}/{digest}.json", primitive)

    def _create_or_authenticate_typed(
        self,
        relative_path: str,
        value: object,
        *,
        create_if_missing: bool = True,
    ) -> ArtifactRefV5:
        raw = canonical_json_bytes_v5(value)
        reference = ArtifactRefV5(relative_path, hashlib.sha256(raw).hexdigest())
        if not create_if_missing:
            authenticated = self.authenticate(reference)
            if authenticated.content != raw:
                raise ArtifactDigestMismatchV5(
                    reference,
                    hashlib.sha256(authenticated.content).hexdigest(),
                )
            return reference
        try:
            return self._create_only(relative_path, canonical_primitive_v5(value))
        except ArtifactExistsV5:
            authenticated = self.authenticate(reference)
            if authenticated.content != raw:
                raise ArtifactDigestMismatchV5(
                    reference,
                    hashlib.sha256(authenticated.content).hexdigest(),
                ) from None
            return reference

    def _load_adapter_state_authority(
        self,
        *,
        namespace: str,
        key: str,
    ) -> tuple[AdapterStateAuthorityV5, ArtifactRefV5] | None:
        authority_relative = f"adapter-state-authority/{namespace}/{key}.json"
        try:
            authority_raw = self._read_relative(authority_relative)
        except ArtifactMissingV5:
            return None
        authority_reference = ArtifactRefV5(
            authority_relative,
            hashlib.sha256(authority_raw).hexdigest(),
        )
        authority = self.load_typed_artifact(
            authority_reference,
            value_type=AdapterStateAuthorityV5,
        )
        if (
            authority.namespace != namespace
            or authority.key != key
            or authority.value_ref.relative_path != f"adapter-state/{namespace}/{key}.json"
        ):
            raise ArtifactSchemaFailureV5(authority_reference)
        return authority, authority_reference

    def append_typed_state(self, *, namespace: str, key: str, value: object) -> ArtifactRefV5:
        """Create or authenticate one deterministic adapter state record."""

        safe_namespace = _safe_component(namespace, "typed state namespace")
        safe_key = _safe_component(key, "typed state key")
        if not is_dataclass(value) or isinstance(value, type):
            raise ValueError("typed state value must be a dataclass instance")
        relative = f"adapter-state/{safe_namespace}/{safe_key}.json"
        raw = canonical_json_bytes_v5(value)
        reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
        authority = AdapterStateAuthorityV5(
            5,
            safe_namespace,
            safe_key,
            reference,
        )
        authority_relative = f"adapter-state-authority/{safe_namespace}/{safe_key}.json"
        prior = self._load_adapter_state_authority(
            namespace=safe_namespace,
            key=safe_key,
        )
        if prior is not None and prior[0] != authority:
            raise ArtifactExistsV5(prior[1])

        # Publish immutable value bytes before their index.  A crash between the
        # two writes leaves an exact orphan which this same operation can reuse;
        # it never leaves a newly published authority pointing at absent bytes.
        stored_reference = self._create_or_authenticate_typed(
            relative,
            value,
            create_if_missing=prior is None,
        )
        if stored_reference != reference:
            raise ArtifactDigestMismatchV5(reference, stored_reference.sha256)
        self._create_or_authenticate_typed(authority_relative, authority)
        return stored_reference

    def load_typed_state(
        self,
        *,
        namespace: str,
        key: str,
        value_type: type[T],
        repair: bool = True,
    ) -> T | None:
        """Load exact state; operational mode repairs only an authenticated orphan index."""

        safe_namespace = _safe_component(namespace, "typed state namespace")
        safe_key = _safe_component(key, "typed state key")
        if type(repair) is not bool:
            raise ValueError("typed state repair mode is invalid")
        relative = f"adapter-state/{safe_namespace}/{safe_key}.json"
        authority_relative = f"adapter-state-authority/{safe_namespace}/{safe_key}.json"
        loaded_authority = self._load_adapter_state_authority(
            namespace=safe_namespace,
            key=safe_key,
        )
        if loaded_authority is None:
            try:
                value_raw = self._read_relative(relative)
            except ArtifactMissingV5:
                return None
            value_reference = ArtifactRefV5(
                relative,
                hashlib.sha256(value_raw).hexdigest(),
            )
            # Authenticate the orphan exactly before reporting the missing
            # index.  A later identical append can now repair this crash window.
            orphan_value = self.load_typed_artifact(value_reference, value_type=value_type)
            expected_authority = AdapterStateAuthorityV5(
                5,
                safe_namespace,
                safe_key,
                value_reference,
            )
            expected_authority_ref = ArtifactRefV5(
                authority_relative,
                hashlib.sha256(canonical_json_bytes_v5(expected_authority)).hexdigest(),
            )
            if not repair:
                raise ArtifactMissingV5(expected_authority_ref) from None
            repaired_ref = self._create_or_authenticate_typed(
                authority_relative,
                expected_authority,
            )
            if repaired_ref != expected_authority_ref:
                raise ArtifactDigestMismatchV5(expected_authority_ref, repaired_ref.sha256)
            repaired = self._load_adapter_state_authority(
                namespace=safe_namespace,
                key=safe_key,
            )
            if repaired is None or repaired[0] != expected_authority or repaired[1] != expected_authority_ref:
                raise ArtifactSchemaFailureV5(expected_authority_ref)
            if self.load_typed_artifact(value_reference, value_type=value_type) != orphan_value:
                raise ArtifactSchemaFailureV5(value_reference)
            return orphan_value
        authority, _authority_reference = loaded_authority
        return self.load_typed_artifact(authority.value_ref, value_type=value_type)

    def recover_typed_state_index(self, *, namespace: str, key: str, expected_value: T) -> T:
        """Publish only an exact missing index for independently authorized existing bytes."""

        safe_namespace = _safe_component(namespace, "typed state namespace")
        safe_key = _safe_component(key, "typed state key")
        if not is_dataclass(expected_value) or isinstance(expected_value, type):
            raise ValueError("typed state recovery requires an exact dataclass value")
        value_ref = ArtifactRefV5(
            f"adapter-state/{safe_namespace}/{safe_key}.json",
            hashlib.sha256(canonical_json_bytes_v5(expected_value)).hexdigest(),
        )
        authority = AdapterStateAuthorityV5(5, safe_namespace, safe_key, value_ref)
        prior = self._load_adapter_state_authority(namespace=safe_namespace, key=safe_key)
        if prior is not None and prior[0] != authority:
            raise ArtifactSchemaFailureV5(prior[1])
        # Never create value bytes: missing, changed, or noncanonical values fail
        # before the sole permitted write, even when the authority is absent.
        if self.load_typed_artifact(value_ref, value_type=type(expected_value)) != expected_value:
            raise ArtifactSchemaFailureV5(value_ref)
        if prior is None:
            self._create_or_authenticate_typed(
                f"adapter-state-authority/{safe_namespace}/{safe_key}.json",
                authority,
            )
        observed = self.load_typed_state(
            namespace=safe_namespace,
            key=safe_key,
            value_type=type(expected_value),
            repair=False,
        )
        if observed != expected_value:
            raise ArtifactSchemaFailureV5(value_ref)
        return observed

    def append_binary_state(self, *, namespace: str, key: str, content: bytes) -> ArtifactRefV5:
        """Create or authenticate one exact bounded adapter-owned byte payload."""

        safe_namespace = _safe_component(namespace, "binary state namespace")
        safe_key = _safe_component(key, "binary state key")
        if type(content) is not bytes:
            raise ValueError("binary state content must be immutable bytes")
        relative = f"adapter-blobs/{safe_namespace}/{safe_key}.bin"
        reference = ArtifactRefV5(relative, hashlib.sha256(content).hexdigest())
        parts = _safe_relative_path(relative)
        try:
            with self._directory(tuple(parts[:-1]), create=True) as directory:
                _write_create_only_in_directory(directory, parts[-1], content)
        except FileExistsError:
            existing = self._read_relative(relative)
            actual = hashlib.sha256(existing).hexdigest()
            if actual != reference.sha256 or existing != content:
                raise ArtifactDigestMismatchV5(
                    reference,
                    actual,
                ) from None
        except ArtifactRepositoryFailureV5:
            raise
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(reference, relative) from None
        return reference

    def load_binary_state(
        self,
        *,
        namespace: str,
        key: str,
        reference: ArtifactRefV5,
        maximum_bytes: int,
    ) -> bytes:
        """Authenticate one exact adapter byte payload and enforce its read bound."""

        safe_namespace = _safe_component(namespace, "binary state namespace")
        safe_key = _safe_component(key, "binary state key")
        expected = f"adapter-blobs/{safe_namespace}/{safe_key}.bin"
        if (
            type(reference) is not ArtifactRefV5
            or reference.relative_path != expected
            or type(maximum_bytes) is not int
            or maximum_bytes <= 0
        ):
            raise ValueError("binary state authority is invalid")
        try:
            content = self._read_relative(reference.relative_path)
        except ArtifactMissingV5:
            raise ArtifactMissingV5(reference) from None
        actual = hashlib.sha256(content).hexdigest()
        if actual != reference.sha256:
            raise ArtifactDigestMismatchV5(reference, actual)
        if len(content) > maximum_bytes:
            raise ArtifactSchemaFailureV5(reference)
        return content

    @contextmanager
    def adapter_state_transition(self, *, namespace: str, key: str) -> Iterator[None]:
        """Serialize one exact durable adapter-state transition across processes."""

        safe_namespace = _safe_component(namespace, "adapter transition namespace")
        safe_key = _safe_component(key, "adapter transition key")
        lock_name = f"{safe_key}.lock"
        lock_relative = f"adapter-state-locks/{safe_namespace}/{lock_name}"
        lock_reference = ArtifactRefV5(lock_relative, "0" * 64)
        try:
            directory_context = self._directory(("adapter-state-locks", safe_namespace), create=True)
            directory = directory_context.__enter__()
            try:
                try:
                    _write_create_only_in_directory(directory, lock_name, b"\0")
                except FileExistsError:
                    pass
                if not directory.entry_exists(lock_name):
                    raise ValueError("adapter transition lock is absent")
                lock_path = directory.path / lock_name
                before = os.lstat(lock_path)
                if _is_link_or_reparse(lock_path) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError("adapter transition lock is invalid")
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                handle = os.fdopen(descriptor, "r+b", buffering=0)
                opened = os.fstat(handle.fileno())
                if _metadata_identity(before) != _metadata_identity(opened):
                    handle.close()
                    raise ValueError("adapter transition lock changed before open")
            except BaseException:
                directory_context.__exit__(None, None, None)
                raise
        except ArtifactRepositoryFailureV5:
            raise
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(lock_reference, lock_relative) from None
        locked = False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            after = os.lstat(lock_path)
            if (
                _is_link_or_reparse(lock_path)
                or _metadata_identity(opened) != _metadata_identity(after)
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
            ):
                raise ArtifactRelocatedV5(lock_reference, lock_relative)
            directory.assert_current()
            yield
        finally:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                directory_context.__exit__(None, None, None)

    @contextmanager
    def role_provider_transition(self, *, campaign_id: str) -> Iterator[None]:
        """Serialize one response-or-terminal transition inside the held repository root."""

        campaign = _safe_component(campaign_id, "role-provider transition campaign")
        lock_name = "role-provider-transition.lock"
        lock_relative = f"authorization/{campaign}/{lock_name}"
        lock_reference = ArtifactRefV5(lock_relative, "0" * 64)
        try:
            directory_context = self._directory(("authorization", campaign), create=True)
            directory = directory_context.__enter__()
            try:
                try:
                    _write_create_only_in_directory(directory, lock_name, b"\0")
                except FileExistsError:
                    pass
                if not directory.entry_exists(lock_name):
                    raise ValueError("role-provider transition lock is absent")
                lock_path = directory.path / lock_name
                before = os.lstat(lock_path)
                if _is_link_or_reparse(lock_path) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError("role-provider transition lock is invalid")
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                handle = os.fdopen(descriptor, "r+b", buffering=0)
                opened = os.fstat(handle.fileno())
                if _metadata_identity(before) != _metadata_identity(opened):
                    handle.close()
                    raise ValueError("role-provider transition lock changed before open")
            except BaseException:
                directory_context.__exit__(None, None, None)
                raise
        except ArtifactRepositoryFailureV5:
            raise
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(lock_reference, lock_relative) from None
        locked = False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            after = os.lstat(lock_path)
            if (
                _is_link_or_reparse(lock_path)
                or _metadata_identity(opened) != _metadata_identity(after)
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
            ):
                raise ArtifactRelocatedV5(lock_reference, lock_relative)
            directory.assert_current()
            yield
        finally:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                directory_context.__exit__(None, None, None)

    def append_role_ledger_reservation(self, record: RoleLedgerReservationV5) -> ArtifactRefV5:
        if type(record) is not RoleLedgerReservationV5:
            raise ValueError("role-ledger reservation is invalid")
        campaign = _safe_component(record.campaign_id, "role-ledger campaign")
        slot = _safe_component(record.slot.slot_id, "role-ledger slot")
        return self._create_only(
            f"authorization/{campaign}/reservations/{slot}.json",
            record.persisted_primitive(),
        )

    def append_role_ledger_terminal(
        self,
        campaign_id: str,
        slot_id: str,
        terminal: RoleLedgerTerminalV5,
    ) -> ArtifactRefV5:
        if type(terminal) is not RoleLedgerTerminalV5:
            raise ValueError("role-ledger terminal is invalid")
        campaign = _safe_component(campaign_id, "role-ledger campaign")
        slot = _safe_component(slot_id, "role-ledger slot")
        return self._create_or_authenticate_typed(
            f"authorization/{campaign}/terminals/{slot}.json",
            terminal,
        )

    def append_role_provider_response(self, response: RoleProviderResponseV5) -> ArtifactRefV5:
        if type(response) is not RoleProviderResponseV5:
            raise ValueError("role provider response is invalid")
        campaign = _safe_component(response.campaign_id, "role provider-response campaign")
        request = _safe_component(response.request_sha256, "role provider-response request")
        return self._create_or_authenticate_typed(
            f"authorization/{campaign}/responses/{request}.json",
            response,
        )

    def load_role_provider_response(
        self,
        *,
        campaign_id: str,
        request_sha256: str,
    ) -> RoleProviderResponseV5 | None:
        campaign = _safe_component(campaign_id, "role provider-response campaign")
        request = _safe_component(request_sha256, "role provider-response request")
        relative = f"authorization/{campaign}/responses/{request}.json"
        try:
            raw = self._read_relative(relative)
        except ArtifactMissingV5:
            return None
        reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
        response = self.load_typed_artifact(reference, value_type=RoleProviderResponseV5)
        if response.campaign_id != campaign_id or response.request_sha256 != request_sha256:
            raise ArtifactSchemaFailureV5(reference)
        return response

    def load_role_ledger_records(
        self,
        *,
        campaign_id: str,
        migration_authority: RoleLedgerMigrationAuthorityV5,
    ) -> tuple[tuple[RoleLedgerReservationV5, ...], tuple[RoleLedgerTerminalV5, ...]]:
        campaign = _safe_component(campaign_id, "role-ledger campaign")
        if (
            type(migration_authority) is not RoleLedgerMigrationAuthorityV5
            or migration_authority.campaign_id != campaign_id
        ):
            raise ArtifactSchemaFailureV5()

        def load_group(group: str, value_type: type[T]) -> tuple[T, ...]:
            try:
                names = self._names(("authorization", campaign, group))
            except ArtifactMissingV5:
                return ()
            values: list[T] = []
            for name in names:
                if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                    raise ArtifactSchemaFailureV5()
                relative = f"authorization/{campaign}/{group}/{name}"
                raw = self._read_relative(relative)
                reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
                value = self.load_typed_artifact(reference, value_type=value_type)
                expected_slot = value.slot.slot_id if type(value) is RoleLedgerReservationV5 else value.receipt.slot_id
                if name != f"{expected_slot}.json":
                    raise ArtifactSchemaFailureV5(reference)
                values.append(value)
            return tuple(values)

        terminals = load_group("terminals", RoleLedgerTerminalV5)
        try:
            reservation_names = self._names(("authorization", campaign, "reservations"))
        except ArtifactMissingV5:
            reservation_names = ()
        legacy_entries: list[tuple[ArtifactRefV5, dict[str, object], Literal[1, 2]]] = []
        current_seen = False
        shared_keys = {
            "schema_version",
            "campaign_id",
            "campaign_manifest_sha256",
            "ledger_identity_sha256",
            "audit_store_identity_sha256",
            "slot",
        }
        for name in reservation_names:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ArtifactSchemaFailureV5()
            relative = f"authorization/{campaign}/reservations/{name}"
            raw = self._read_relative(relative)
            reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
            authenticated = self.authenticate(reference)
            primitive = _strict_json_object(authenticated.content, reference)
            if canonical_json_bytes_v5(primitive) != authenticated.content:
                raise ArtifactNonCanonicalV5(reference)
            keys = set(primitive)
            if keys == shared_keys:
                legacy_entries.append((reference, primitive, 1))
            elif keys == {*shared_keys, "ledger_ordinal"}:
                legacy_entries.append((reference, primitive, 2))
            else:
                current_seen = True
        if legacy_entries:
            if current_seen or len(legacy_entries) != len(reservation_names):
                raise ArtifactSchemaFailureV5(legacy_entries[0][0])
            self._require_exact_legacy_role_ledger(
                entries=tuple(legacy_entries),
                terminals=terminals,
                authority=migration_authority,
            )
            revisions = {item[2] for item in legacy_entries}
            if len(revisions) != 1:
                raise ArtifactSchemaFailureV5(legacy_entries[0][0])
            revision = next(iter(revisions))
            raise RoleLedgerMigrationRequiredV5(revision, legacy_entries[0][0])
        return load_group("reservations", RoleLedgerReservationV5), terminals

    def _require_exact_legacy_role_ledger(
        self,
        *,
        entries: tuple[tuple[ArtifactRefV5, dict[str, object], Literal[1, 2]], ...],
        terminals: tuple[RoleLedgerTerminalV5, ...],
        authority: RoleLedgerMigrationAuthorityV5,
    ) -> None:
        """Authenticate every relation in the two exact superseded reservation shapes."""

        decoded: list[dict[str, object]] = []
        slot_keys = {
            "slot_id",
            "request",
            "authorization_sha256",
            "prior_external_attempts",
            "prior_total_tokens",
            "prior_cost_usd",
            "prior_terminal_sequence",
        }
        request_keys_v1 = {
            "request_sha256",
            "role",
            "attempt_kind",
            "attempt_index",
            "model",
            "max_output_tokens",
        }
        for reference, primitive, revision in entries:
            if (
                primitive["schema_version"] != 5
                or primitive["campaign_id"] != authority.campaign_id
                or primitive["campaign_manifest_sha256"] != authority.campaign_manifest_sha256
                or primitive["ledger_identity_sha256"] != authority.ledger_identity_sha256
                or primitive["audit_store_identity_sha256"] != authority.audit_store_identity_sha256
            ):
                raise ArtifactSchemaFailureV5(reference)
            slot = _exact_keys(primitive["slot"], slot_keys, reference)
            request_value = slot["request"]
            expected_request_keys = request_keys_v1 | ({"response_schema_sha256"} if revision == 2 else set())
            request_primitive = _exact_keys(request_value, expected_request_keys, reference)
            request_sha256 = _decode_value(str, request_primitive["request_sha256"])
            if re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None:
                raise ArtifactSchemaFailureV5(reference)
            call, request = self.load_unique_role_request_entry_by_sha256(request_sha256)
            role = _decode_value(RoleNameV5, request_primitive["role"])
            attempt_kind = _decode_value(Literal["primary", "retry", "repair"], request_primitive["attempt_kind"])
            attempt_index = _decode_value(int, request_primitive["attempt_index"])
            model = _decode_value(str, request_primitive["model"])
            maximum_output_tokens = _decode_value(int, request_primitive["max_output_tokens"])
            if (
                attempt_index < 1
                or maximum_output_tokens < 1
                or model != authority.model
                or maximum_output_tokens != authority.maximum_output_tokens_per_role
                or request_sha256 != request.sha256
                or role != request.role
                or maximum_output_tokens != request.max_output_tokens
                or call.campaign_id != authority.campaign_id
                or call.role != role
                or call.attempt_kind != attempt_kind
                or call.attempt_index != attempt_index
                or call.request_sha256 != request.sha256
                or request.response_schema_sha256 != request.schema_authority.sha256
            ):
                raise ArtifactSchemaFailureV5(reference)
            if revision == 2:
                response_schema_sha256 = _decode_value(str, request_primitive["response_schema_sha256"])
                if response_schema_sha256 != request.response_schema_sha256:
                    raise ArtifactSchemaFailureV5(reference)
            slot_request_sha256 = canonical_sha256_v5(request_primitive)
            slot_id = _decode_value(str, slot["slot_id"])
            authorization_sha256 = _decode_value(str, slot["authorization_sha256"])
            prior_attempts = _decode_value(int, slot["prior_external_attempts"])
            prior_tokens = _decode_value(int, slot["prior_total_tokens"])
            prior_cost = _decode_value(Decimal, slot["prior_cost_usd"])
            prior_sequence = _decode_value(int, slot["prior_terminal_sequence"])
            if (
                prior_attempts < 0
                or prior_tokens < 0
                or prior_sequence < 0
                or slot_id
                != canonical_sha256_v5(
                    {
                        "domain": "pit-optimizer-v5-role-slot-id-v1",
                        "campaign_manifest_sha256": authority.campaign_manifest_sha256,
                        "request_sha256": slot_request_sha256,
                    }
                )
                or authorization_sha256
                != canonical_sha256_v5(
                    {
                        "domain": "pit-optimizer-v5-role-slot-v1",
                        "ledger_identity_sha256": authority.ledger_identity_sha256,
                        "request_sha256": slot_request_sha256,
                        "terminal_sequence": prior_sequence + 1,
                    }
                )
                or reference.relative_path != f"authorization/{authority.campaign_id}/reservations/{slot_id}.json"
            ):
                raise ArtifactSchemaFailureV5(reference)
            ordinal = _decode_value(int, primitive["ledger_ordinal"]) if revision == 2 else prior_sequence + 1
            if ordinal < 1 or ordinal != prior_sequence + 1:
                raise ArtifactSchemaFailureV5(reference)
            decoded.append(
                {
                    "reference": reference,
                    "reservation_sha256": reference.sha256,
                    "ordinal": ordinal,
                    "slot_id": slot_id,
                    "slot_request_sha256": slot_request_sha256,
                    "request_sha256": request.sha256,
                    "role": role,
                    "attempt_kind": attempt_kind,
                    "attempt_index": attempt_index,
                    "authorization_sha256": authorization_sha256,
                    "prior_attempts": prior_attempts,
                    "prior_tokens": prior_tokens,
                    "prior_cost": prior_cost,
                    "prior_sequence": prior_sequence,
                }
            )
        ordered = tuple(sorted(decoded, key=lambda item: int(item["ordinal"])))
        if tuple(item["ordinal"] for item in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ArtifactSchemaFailureV5(entries[0][0])
        ordered_terminals = tuple(sorted(terminals, key=lambda item: item.receipt.terminal_sequence))
        if len(ordered_terminals) not in {len(ordered) - 1, len(ordered)} or tuple(
            item.receipt.terminal_sequence for item in ordered_terminals
        ) != tuple(range(1, len(ordered_terminals) + 1)):
            raise ArtifactSchemaFailureV5(entries[0][0])
        for index, item in enumerate(ordered):
            previous = ordered_terminals[index - 1].receipt if index else None
            expected_prior = (
                0 if previous is None else previous.cumulative_external_attempts,
                0 if previous is None else previous.cumulative_total_tokens,
                Decimal("0") if previous is None else previous.cumulative_cost_usd,
                index,
            )
            if (
                item["prior_attempts"],
                item["prior_tokens"],
                item["prior_cost"],
                item["prior_sequence"],
            ) != expected_prior:
                raise ArtifactSchemaFailureV5(item["reference"])  # type: ignore[arg-type]
            if index >= len(ordered_terminals):
                continue
            terminal = ordered_terminals[index]
            expected_cumulative_attempts = int(item["prior_attempts"]) + terminal.facts.usage.external_attempt_count
            expected_cumulative_tokens = int(item["prior_tokens"]) + terminal.facts.usage.total_tokens
            expected_cumulative_cost = item["prior_cost"] + terminal.facts.usage.cost_usd  # type: ignore[operator]
            if (
                terminal.reservation_sha256 != item["reservation_sha256"]
                or terminal.facts.slot_id != item["slot_id"]
                or terminal.facts.request_sha256 != item["request_sha256"]
                or terminal.facts.role != item["role"]
                or terminal.facts.attempt_kind != item["attempt_kind"]
                or terminal.facts.attempt_index != item["attempt_index"]
                or terminal.receipt.slot_id != item["slot_id"]
                or terminal.receipt.slot_request_sha256 != item["slot_request_sha256"]
                or terminal.receipt.authorization_sha256 != item["authorization_sha256"]
                or terminal.receipt.cumulative_external_attempts != expected_cumulative_attempts
                or terminal.receipt.cumulative_total_tokens != expected_cumulative_tokens
                or terminal.receipt.cumulative_cost_usd != expected_cumulative_cost
                or terminal.receipt.terminal_sequence != item["ordinal"]
            ):
                raise ArtifactSchemaFailureV5(item["reference"])  # type: ignore[arg-type]

    def load_evaluation_panel_spec(self, reference: ArtifactRefV5) -> EvaluationPanelSpec:
        """Authenticate the panel's authoritative newline-bearing legacy encoding."""

        panel, _ = self._authenticate_evaluation_panel_spec(reference)
        return panel

    def load_evaluation_panel_spec_exact(self, reference: ArtifactRefV5) -> EvaluationPanelSpec:
        """Decode only the named digest-authenticated panel, without relocation."""
        panel, _ = self._authenticate_evaluation_panel_spec(reference, exact=True)
        return panel

    def _authenticate_evaluation_panel_spec(
        self, reference: ArtifactRefV5, *, exact: bool = False
    ) -> tuple[EvaluationPanelSpec, AuthenticatedArtifactV5]:
        if type(reference) is not ArtifactRefV5:
            raise ValueError("panel authentication requires a V5 reference")
        _safe_relative_path(reference.relative_path)
        try:
            raw = self._read_relative(reference.relative_path)
        except ArtifactMissingV5:
            if not exact:
                relocated = self._find_digest(reference.sha256, reference.relative_path)
                if relocated is not None:
                    raise ArtifactRelocatedV5(reference, relocated) from None
            raise ArtifactMissingV5(reference) from None
        actual = hashlib.sha256(raw).hexdigest()
        if actual != reference.sha256:
            raise ArtifactDigestMismatchV5(reference, actual)

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ArtifactNonCanonicalV5(reference)
                result[key] = value
            return result

        try:
            primitive = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
            panel = _decode_dataclass(EvaluationPanelSpec, primitive)
            if _canonical_panel_json_bytes(_panel_json_value(panel)) != raw:
                raise ArtifactNonCanonicalV5(reference)
        except ArtifactRepositoryFailureV5:
            raise
        except (UnicodeError, TypeError, ValueError, ArithmeticError, RecursionError):
            raise ArtifactSchemaFailureV5(reference) from None
        if panel.sha256 != reference.sha256:
            raise ArtifactDigestMismatchV5(reference, panel.sha256)
        return panel, AuthenticatedArtifactV5(reference=reference, content=raw, child_references=())

    def load_typed_artifact(self, reference: ArtifactRefV5, *, value_type: type[T]) -> T:
        """Authenticate exact canonical dataclass bytes without an extra envelope."""

        if not isinstance(value_type, type) or not is_dataclass(value_type):
            raise ValueError("typed artifact type must be a dataclass")
        authenticated = self.authenticate(reference)
        try:
            primitive = _strict_json_object(authenticated.content, reference)
            if canonical_json_bytes_v5(primitive) != authenticated.content:
                raise ArtifactNonCanonicalV5(reference)
            if value_type is RoleLedgerTerminalV5:
                value = _decode_role_ledger_terminal(primitive)
            elif value_type is RoleLedgerReservationV5:
                slot_value = primitive.get("slot")
                request_value = None if type(slot_value) is not dict else slot_value.get("request")
                if type(request_value) is not dict or type(request_value.get("request_sha256")) is not str:
                    raise ArtifactSchemaFailureV5(reference)
                request = self.load_unique_role_request_by_sha256(request_value["request_sha256"])
                value = _decode_role_ledger_reservation(primitive, request=request)
            else:
                value = _decode_dataclass(value_type, primitive)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        expected = value.persisted_primitive() if type(value) is RoleLedgerReservationV5 else value
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return value

    def append_role_request(
        self,
        *,
        call: RoleCallKeyV5,
        request: RoleRequestV5,
    ) -> PersistedRoleRequestV5:
        """Persist the full reconstructible request before any role invocation."""

        if type(call) is not RoleCallKeyV5 or type(request) is not RoleRequestV5:
            raise ValueError("role call and request must use the V5 schema")
        if call.role != request.role or call.request_sha256 != request.sha256:
            raise ValueError("role call differs from its exact request")
        primitive = role_request_artifact_primitive_v5(call=call, request=request)
        path = f"roles/requests/{call.sha256}.json"
        reference, created = self._create_only_with_status(path, primitive)
        capability_type = FreshPersistedRoleRequestV5 if created else ExistingPersistedRoleRequestV5
        return capability_type(reference=reference, call=call, request=request)

    def _load_role_request_entry(
        self,
        reference: ArtifactRefV5,
    ) -> tuple[RoleCallKeyV5, RoleRequestV5]:
        authenticated = self.authenticate(reference)
        try:
            envelope = _exact_keys(
                _strict_json_object(authenticated.content, reference),
                {"schema_version", "artifact_type", "call", "request"},
                reference,
            )
            if envelope["schema_version"] != 5 or envelope["artifact_type"] != "role_request":
                raise ArtifactSchemaFailureV5(reference)
            call = _decode_dataclass(RoleCallKeyV5, envelope["call"])
            request = _decode_role_request(envelope["request"])
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        expected = role_request_artifact_primitive_v5(call=call, request=request)
        if reference.relative_path != f"roles/requests/{call.sha256}.json":
            raise ArtifactSchemaFailureV5(reference)
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return call, request

    def load_role_request(self, reference: ArtifactRefV5) -> RoleRequestV5:
        return self._load_role_request_entry(reference)[1]

    def load_unique_role_request_by_sha256(self, request_sha256: str) -> RoleRequestV5:
        """Resolve exactly one authenticated durable request by semantic identity."""

        matches = self._role_request_entries_by_sha256(request_sha256)
        if any(item[1] != matches[0][1] for item in matches[1:]):
            raise ArtifactSchemaFailureV5()
        return matches[0][1]

    def load_unique_role_request_entry_by_sha256(
        self,
        request_sha256: str,
    ) -> tuple[RoleCallKeyV5, RoleRequestV5]:
        """Resolve one exact authenticated call/request pair by request identity."""

        matches = self._role_request_entries_by_sha256(request_sha256)
        if len(matches) != 1:
            raise ArtifactSchemaFailureV5()
        return matches[0]

    def _role_request_entries_by_sha256(
        self,
        request_sha256: str,
    ) -> tuple[tuple[RoleCallKeyV5, RoleRequestV5], ...]:
        """Load every authenticated durable call carrying one request identity."""

        if type(request_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None:
            raise ArtifactSchemaFailureV5()
        try:
            names = self._names(("roles", "requests"))
        except ArtifactMissingV5:
            raise ArtifactSchemaFailureV5() from None
        matches: list[tuple[RoleCallKeyV5, RoleRequestV5]] = []
        for name in names:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ArtifactSchemaFailureV5()
            relative = f"roles/requests/{name}"
            raw = self._read_relative(relative)
            reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
            call, request = self._load_role_request_entry(reference)
            if request.sha256 == request_sha256:
                matches.append((call, request))
        if not matches:
            raise ArtifactSchemaFailureV5()
        return tuple(matches)

    def append_role_attempt(self, attempt: RoleAttemptFactsV5) -> ArtifactRefV5:
        if type(attempt) is not RoleAttemptFactsV5:
            raise ValueError("role attempt must use the V5 schema")
        primitive = {
            "schema_version": 5,
            "artifact_type": "role_attempt",
            "attempt": _constructor_primitive(attempt),
        }
        return self._create_only(f"roles/attempts/{attempt.role}/{attempt.sha256}.json", primitive)

    def load_role_attempt(self, reference: ArtifactRefV5) -> RoleAttemptFactsV5:
        authenticated = self.authenticate(reference)
        try:
            envelope = _exact_keys(
                _strict_json_object(authenticated.content, reference),
                {"schema_version", "artifact_type", "attempt"},
                reference,
            )
            if envelope["schema_version"] != 5 or envelope["artifact_type"] != "role_attempt":
                raise ArtifactSchemaFailureV5(reference)
            attempt = _decode_role_attempt(envelope["attempt"])
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        if reference.relative_path != f"roles/attempts/{attempt.role}/{attempt.sha256}.json":
            raise ArtifactSchemaFailureV5(reference)
        expected = {
            "schema_version": 5,
            "artifact_type": "role_attempt",
            "attempt": _constructor_primitive(attempt),
        }
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return attempt

    def append_role_terminal_authority(
        self,
        *,
        role: RoleNameV5,
        authority: RoleTerminalAuthorityV5,
    ) -> ArtifactRefV5:
        if role not in {"investigator", "author", "critic"}:
            raise ValueError("role terminal authority role is invalid")
        if type(authority) is LedgerRoleTerminalAuthorityV5:
            kind = "ledger_receipt"
            identity = authority.sha256
        elif type(authority) is FixtureRoleTerminalAuthorityV5:
            kind = "fixture_authority"
            identity = authority.sha256
        else:
            raise ValueError("role terminal authority is outside the closed V5 union")
        primitive = {
            "schema_version": 5,
            "artifact_type": "role_terminal_authority",
            "authority_kind": kind,
            "authority": _constructor_primitive(authority),
        }
        return self._create_only(f"roles/terminals/{role}/{identity}.json", primitive)

    def load_role_terminal_authority(
        self,
        reference: ArtifactRefV5,
        *,
        role: RoleNameV5,
    ) -> RoleTerminalAuthorityV5:
        authenticated = self.authenticate(reference)
        try:
            envelope = _exact_keys(
                _strict_json_object(authenticated.content, reference),
                {"schema_version", "artifact_type", "authority_kind", "authority"},
                reference,
            )
            if envelope["schema_version"] != 5 or envelope["artifact_type"] != "role_terminal_authority":
                raise ArtifactSchemaFailureV5(reference)
            kind = envelope["authority_kind"]
            if kind == "ledger_receipt":
                authority: RoleTerminalAuthorityV5 = _decode_dataclass(
                    LedgerRoleTerminalAuthorityV5,
                    envelope["authority"],
                )
                identity = authority.sha256
            elif kind == "fixture_authority":
                authority = _decode_dataclass(FixtureRoleTerminalAuthorityV5, envelope["authority"])
                identity = authority.sha256
            else:
                raise ArtifactSchemaFailureV5(reference)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        if reference.relative_path != f"roles/terminals/{role}/{identity}.json":
            raise ArtifactSchemaFailureV5(reference)
        expected = {
            "schema_version": 5,
            "artifact_type": "role_terminal_authority",
            "authority_kind": kind,
            "authority": _constructor_primitive(authority),
        }
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return authority

    def append_role_artifact(
        self,
        *,
        role: RoleNameV5,
        artifact: ParsedRoleArtifactV5,
    ) -> ArtifactRefV5:
        primitive = {
            "schema_version": 5,
            "artifact_type": "role_artifact",
            "payload": parsed_role_artifact_primitive_v5(artifact),
        }
        identity = canonical_sha256_v5(artifact)
        return self._create_only(f"roles/artifacts/{role}/{identity}.json", primitive)

    def load_role_artifact(
        self,
        reference: ArtifactRefV5,
        *,
        role: RoleNameV5,
    ) -> ParsedRoleArtifactV5:
        authenticated = self.authenticate(reference)
        try:
            envelope = _exact_keys(
                _strict_json_object(authenticated.content, reference),
                {"schema_version", "artifact_type", "payload"},
                reference,
            )
            if envelope["schema_version"] != 5 or envelope["artifact_type"] != "role_artifact":
                raise ArtifactSchemaFailureV5(reference)
            artifact = decode_parsed_role_artifact_v5(role=role, primitive=envelope["payload"])
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        identity = canonical_sha256_v5(artifact)
        if reference.relative_path != f"roles/artifacts/{role}/{identity}.json":
            raise ArtifactSchemaFailureV5(reference)
        expected = {
            "schema_version": 5,
            "artifact_type": "role_artifact",
            "payload": parsed_role_artifact_primitive_v5(artifact),
        }
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return artifact

    def persist_role_invocation(
        self,
        *,
        call: RoleCallKeyV5,
        request_ref: ArtifactRefV5,
        package: RoleInvocationPackageV5,
    ) -> PersistedRoleInvocationV5:
        """Durably store one terminal package after its request already exists."""

        if type(call) is not RoleCallKeyV5 or type(package) is not RoleInvocationPackageV5:
            raise ValueError("role call and invocation package must use the V5 schema")
        persisted_call, persisted_request = self._load_role_request_entry(request_ref)
        if persisted_call != call or persisted_request != package.request:
            raise ValueError("role invocation request differs from its durable request")
        attempt_ref = self.append_role_attempt(package.attempt)
        terminal_ref = self.append_role_terminal_authority(
            role=package.request.role,
            authority=package.terminal_authority,
        )
        artifact_ref = (
            None
            if package.artifact is None
            else self.append_role_artifact(role=package.request.role, artifact=package.artifact)
        )
        payload = RoleCompletionPayloadV5(
            campaign_id=call.campaign_id,
            round_index=call.round_index,
            role=package.request.role,
            role_position=call.role_position,
            outcome=package.attempt.outcome,
            call_key_sha256=call.sha256,
            request_sha256=package.request.sha256,
            binding_sha256=package.binding_sha256,
            attempt_sha256=package.attempt.sha256,
            terminal_authority_sha256=package.terminal_authority_sha256,
            artifact_sha256=(None if package.artifact is None else canonical_sha256_v5(package.artifact)),
            package_sha256=package.sha256,
            request_ref=request_ref,
            attempt_ref=attempt_ref,
            terminal_authority_ref=terminal_ref,
            artifact_ref=artifact_ref,
        )
        return PersistedRoleInvocationV5(payload=payload, call=call, package=package)

    def load_role_invocation(self, payload: RoleCompletionPayloadV5) -> RoleInvocationPackageV5:
        if type(payload) is not RoleCompletionPayloadV5:
            raise ValueError("role completion must use the V5 schema")
        call, request = self._load_role_request_entry(payload.request_ref)
        attempt = self.load_role_attempt(payload.attempt_ref)
        authority = self.load_role_terminal_authority(
            payload.terminal_authority_ref,
            role=payload.role,
        )
        artifact = (
            None if payload.artifact_ref is None else self.load_role_artifact(payload.artifact_ref, role=payload.role)
        )
        package = RoleInvocationPackageV5(
            call=call,
            request=request,
            attempt=attempt,
            terminal_authority=authority,
            artifact=artifact,
        )
        PersistedRoleInvocationV5(payload=payload, call=call, package=package)
        return package

    def append_round_payload(self, payload: RoundEventPayloadV5) -> ArtifactRefV5:
        if isinstance(payload, RoleCompletionPayloadV5):
            self.load_role_invocation(payload)
        if isinstance(payload, CandidateStageResultPayloadV5):
            self._validate_candidate_stage_failure(payload)
        kind = event_kind_for_payload_v5(payload)
        primitive = round_event_payload_primitive_v5(payload)
        digest = hashlib.sha256(canonical_json_bytes_v5(primitive)).hexdigest()
        return self._create_only(f"payloads/{kind}/{digest}.json", primitive)

    def load_round_payload(self, reference: ArtifactRefV5, *, expected_kind: str) -> RoundEventPayloadV5:
        _safe_component(expected_kind, "round payload kind")
        authenticated = self.authenticate(reference)
        try:
            primitive = _strict_json_object(authenticated.content, reference)
            payload = _decode_round_payload(expected_kind, primitive)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        if canonical_json_bytes_v5(round_event_payload_primitive_v5(payload)) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        if isinstance(payload, RoleCompletionPayloadV5):
            self.load_role_invocation(payload)
        if isinstance(payload, CandidateStageResultPayloadV5):
            self._validate_candidate_stage_failure(payload)
        return payload

    def _validate_candidate_stage_failure(self, payload: CandidateStageResultPayloadV5) -> None:
        if payload.failure_ref is None:
            return
        authenticated = self.authenticate(payload.failure_ref)
        try:
            envelope = _exact_keys(
                _strict_json_object(authenticated.content, payload.failure_ref),
                {"schema_version", "artifact_type", "payload"},
                payload.failure_ref,
            )
            body = _exact_keys(
                envelope["payload"],
                {"code", "stage", "experiment_id"},
                payload.failure_ref,
            )
            if (
                type(envelope["schema_version"]) is not int
                or envelope["schema_version"] != 5
                or envelope["artifact_type"] != "typed_failure"
                or body
                != {
                    "code": payload.failure_code,
                    "stage": payload.stage,
                    "experiment_id": payload.experiment_id,
                }
            ):
                raise ArtifactSchemaFailureV5(payload.failure_ref)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(payload.failure_ref) from None
        expected = {
            "schema_version": 5,
            "artifact_type": "typed_failure",
            "payload": body,
        }
        if canonical_json_bytes_v5(expected) != authenticated.content:
            raise ArtifactNonCanonicalV5(payload.failure_ref)

    def append_round_event(self, event: RoundEventV5) -> ArtifactRefV5:
        if type(event) is not RoundEventV5:
            raise ValueError("round event must use the V5 schema")
        payload = self.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        event.validate_payload(payload)
        prior = self.load_round_events(campaign_id=event.campaign_id, round_index=event.round_index)
        if event.sequence != len(prior):
            raise ValueError("round event sequence is not the next durable position")
        expected_prior = None if not prior else prior[-1].sha256
        if event.prior_event_sha256 != expected_prior:
            raise ValueError("round event predecessor is not the durable head")
        prior_payloads = tuple(
            self.load_round_payload(item.payload_ref, expected_kind=item.event_kind) for item in prior
        )
        terminal_seen = any(isinstance(item, RoundOutcomePayloadV5) for item in prior_payloads)
        cleanup_seen = tuple(item for item in prior_payloads if isinstance(item, CleanupResultPayloadV5))
        if terminal_seen and not isinstance(payload, CleanupResultPayloadV5):
            raise ValueError("only cleanup may follow a terminal round outcome")
        if cleanup_seen:
            if cleanup_seen[-1].cleanup_complete:
                raise ValueError("cannot append after complete cleanup")
            if not isinstance(payload, CleanupResultPayloadV5):
                raise ValueError("only cleanup retry may follow incomplete cleanup")
        fold_round_events_v5(
            events=(*prior, event),
            payloads=(*prior_payloads, payload),
        )
        campaign = _safe_component(event.campaign_id, "round event campaign")
        path = f"events/{campaign}/{event.round_index:04d}/{event.sequence:06d}.json"
        reference = self._create_only(path, event.to_primitive())
        if reference.sha256 != event.sha256:
            raise ArtifactNonCanonicalV5(reference)
        return reference

    def _names(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        try:
            with self._directory(parts, create=False) as directory:
                return tuple(sorted(os.listdir(directory.path)))
        except ArtifactRepositoryFailureV5:
            raise
        except (FileNotFoundError, OSError, ValueError):
            raise ArtifactMissingV5() from None

    def load_round_events(self, *, campaign_id: str, round_index: int) -> tuple[RoundEventV5, ...]:
        campaign = _safe_component(campaign_id, "round event campaign")
        if type(round_index) is not int or round_index < 1:
            raise ValueError("round event round is invalid")
        parts = ("events", campaign, f"{round_index:04d}")
        try:
            names = self._names(parts)
        except ArtifactMissingV5:
            return ()
        if any(re.fullmatch(r"[0-9]{6}\.json", name) is None for name in names):
            raise ArtifactSchemaFailureV5()
        events: list[RoundEventV5] = []
        payloads: list[RoundEventPayloadV5] = []
        for position, name in enumerate(names):
            relative = PurePosixPath(*parts, name).as_posix()
            raw = self._read_relative(relative)
            digest = hashlib.sha256(raw).hexdigest()
            reference = ArtifactRefV5(relative, digest)
            try:
                primitive = _strict_json_object(raw, reference)
                event = _decode_round_event(primitive)
            except ArtifactRepositoryFailureV5:
                raise
            except (TypeError, ValueError, ArithmeticError):
                raise ArtifactSchemaFailureV5(reference) from None
            if canonical_json_bytes_v5(event.to_primitive()) != raw or event.sha256 != digest:
                raise ArtifactNonCanonicalV5(reference)
            if event.sequence != position or name != f"{position:06d}.json":
                raise ArtifactSchemaFailureV5(reference)
            payload = self.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            events.append(event)
            payloads.append(payload)
        fold_round_events_v5(events=tuple(events), payloads=tuple(payloads))
        return tuple(events)

    def load_candidate_executions(
        self, *, campaign_id: str, round_index: int
    ) -> tuple[CandidateExecutionAuthorityV5, ...]:
        events = self.load_round_events(campaign_id=campaign_id, round_index=round_index)
        return tuple(
            payload
            for event in events
            if isinstance(
                payload := self.load_round_payload(
                    event.payload_ref,
                    expected_kind=event.event_kind,
                ),
                CandidateExecutionAuthorityV5,
            )
        )

    def load_candidate_execution(
        self,
        *,
        campaign_id: str,
        round_index: int,
        key: CandidateExecutionKeyV5,
    ) -> tuple[CandidateExecutionAuthorityV5, ArtifactRefV5] | None:
        if type(key) is not CandidateExecutionKeyV5:
            raise ValueError("candidate execution lookup key is invalid")
        events = self.load_round_events(campaign_id=campaign_id, round_index=round_index)
        indexed = self._indexed_candidate_execution(events=events, key=key)
        orphan = self._candidate_execution_orphan(
            campaign_id=campaign_id,
            round_index=round_index,
            key=key,
            events=events,
        )
        if indexed is not None:
            if orphan is not None:
                raise ArtifactSchemaFailureV5(orphan[1])
            return indexed
        if orphan is None:
            return None
        authority, payload_ref = orphan
        event = RoundEventV5(
            campaign_id=campaign_id,
            round_index=round_index,
            sequence=len(events),
            prior_event_sha256=None if not events else events[-1].sha256,
            event_kind="candidate_execution",
            experiment_id=authority.key.experiment_id,
            payload_ref=payload_ref,
        )
        self.append_round_event(event)
        promoted_events = self.load_round_events(campaign_id=campaign_id, round_index=round_index)
        promoted = self._indexed_candidate_execution(events=promoted_events, key=key)
        if promoted != orphan:
            raise ArtifactNonCanonicalV5(payload_ref)
        return promoted

    def _indexed_candidate_execution(
        self,
        *,
        events: tuple[RoundEventV5, ...],
        key: CandidateExecutionKeyV5,
    ) -> tuple[CandidateExecutionAuthorityV5, ArtifactRefV5] | None:
        matches: list[tuple[CandidateExecutionAuthorityV5, ArtifactRefV5]] = []
        for event in events:
            if event.event_kind != "candidate_execution":
                continue
            payload = self.load_round_payload(
                event.payload_ref,
                expected_kind=event.event_kind,
            )
            if not isinstance(payload, CandidateExecutionAuthorityV5):
                raise ArtifactSchemaFailureV5(event.payload_ref)
            if payload.key == key:
                matches.append((payload, event.payload_ref))
        if len(matches) > 1:
            raise ArtifactSchemaFailureV5()
        return None if not matches else matches[0]

    def _candidate_execution_orphan(
        self,
        *,
        campaign_id: str,
        round_index: int,
        key: CandidateExecutionKeyV5,
        events: tuple[RoundEventV5, ...] | None = None,
    ) -> tuple[CandidateExecutionAuthorityV5, ArtifactRefV5] | None:
        if events is None:
            events = self.load_round_events(
                campaign_id=campaign_id,
                round_index=round_index,
            )
        indexed = {event.payload_ref for event in events if event.event_kind == "candidate_execution"}
        try:
            names = self._names(("payloads", "candidate_execution"))
        except ArtifactMissingV5:
            return None
        matches: list[tuple[CandidateExecutionAuthorityV5, ArtifactRefV5]] = []
        for name in names:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ArtifactSchemaFailureV5()
            relative = f"payloads/candidate_execution/{name}"
            reference = ArtifactRefV5(relative, name.removesuffix(".json"))
            if reference in indexed:
                continue
            payload = self.load_round_payload(
                reference,
                expected_kind="candidate_execution",
            )
            if not isinstance(payload, CandidateExecutionAuthorityV5):
                raise ArtifactSchemaFailureV5(reference)
            if payload.campaign_id == campaign_id and payload.round_index == round_index and payload.key == key:
                matches.append((payload, reference))
        if len(matches) > 1:
            raise ArtifactSchemaFailureV5()
        return None if not matches else matches[0]

    def verify_candidate_execution_index(
        self,
        *,
        campaign_id: str,
        round_index: int,
    ) -> tuple[CandidateExecutionAuthorityV5, ...]:
        """Read-only verification that every matching reservation is indexed."""

        events = self.load_round_events(campaign_id=campaign_id, round_index=round_index)
        indexed: dict[ArtifactRefV5, CandidateExecutionAuthorityV5] = {}
        for event in events:
            if event.event_kind != "candidate_execution":
                continue
            payload = self.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
            if (
                type(payload) is not CandidateExecutionAuthorityV5
                or payload.campaign_id != campaign_id
                or payload.round_index != round_index
                or event.payload_ref in indexed
            ):
                raise ArtifactSchemaFailureV5(event.payload_ref)
            indexed[event.payload_ref] = payload
        try:
            names = self._names(("payloads", "candidate_execution"))
        except ArtifactMissingV5:
            names = ()
        for name in names:
            if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
                raise ArtifactSchemaFailureV5()
            reference = ArtifactRefV5(
                f"payloads/candidate_execution/{name}",
                name.removesuffix(".json"),
            )
            payload = self.load_round_payload(reference, expected_kind="candidate_execution")
            if (
                type(payload) is CandidateExecutionAuthorityV5
                and payload.campaign_id == campaign_id
                and payload.round_index == round_index
                and reference not in indexed
            ):
                raise ArtifactSchemaFailureV5(reference)
        return tuple(indexed[event.payload_ref] for event in events if event.payload_ref in indexed)

    def append_candidate_execution(self, authority: CandidateExecutionAuthorityV5) -> ArtifactRefV5:
        if type(authority) is not CandidateExecutionAuthorityV5:
            raise ValueError("candidate execution authority is invalid")
        existing = self.load_candidate_executions(
            campaign_id=authority.campaign_id,
            round_index=authority.round_index,
        )
        indexed = self.load_candidate_execution(
            campaign_id=authority.campaign_id,
            round_index=authority.round_index,
            key=authority.key,
        )
        if indexed is not None:
            if indexed[0] == authority:
                return indexed[1]
            raise ArtifactExistsV5()
        existing_lease_ids = {payload.lease_id for item in existing for payload in item.lease_payloads}
        if any(payload.lease_id in existing_lease_ids for payload in authority.lease_payloads):
            raise ArtifactExistsV5()
        payload_ref = self.append_round_payload(authority)
        reloaded = self.load_candidate_execution(
            campaign_id=authority.campaign_id,
            round_index=authority.round_index,
            key=authority.key,
        )
        if (
            reloaded is None
            or reloaded[0] != authority
            or reloaded[1] != payload_ref
            or type(reloaded[1]) is not ArtifactRefV5
        ):
            raise ArtifactNonCanonicalV5(payload_ref)
        return payload_ref

    def recover_round(
        self,
        *,
        campaign_id: str,
        round_index: int,
        expected_steps: tuple[RecoveryStepV5, ...],
    ) -> RoundRecoveryV5:
        events = self.load_round_events(campaign_id=campaign_id, round_index=round_index)
        payloads = tuple(self.load_round_payload(event.payload_ref, expected_kind=event.event_kind) for event in events)
        return fold_round_events_v5(events=events, payloads=payloads, expected_steps=expected_steps)

    def append_critic(self, artifact: CriticArtifactV5) -> ArtifactRefV5:
        if type(artifact) is not CriticArtifactV5:
            raise ValueError("critic artifact must use the V5 schema")
        primitive = canonical_primitive_v5(artifact)
        reference = self._create_only(f"critics/{artifact.sha256}.json", primitive)
        if reference.sha256 != artifact.sha256:
            raise ArtifactNonCanonicalV5(reference)
        return reference

    def load_critic(self, reference: ArtifactRefV5) -> CriticArtifactV5:
        if reference.relative_path != f"critics/{reference.sha256}.json":
            raise ArtifactSchemaFailureV5(reference)
        authenticated = self.authenticate(reference)
        try:
            primitive = _strict_json_object(authenticated.content, reference)
            artifact = _decode_dataclass(CriticArtifactV5, primitive)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        if canonical_json_bytes_v5(artifact) != authenticated.content:
            raise ArtifactNonCanonicalV5(reference)
        return artifact

    def _validate_experiment_critic_binding(
        self,
        record: ExperimentRecordV5,
        *,
        record_reference: ArtifactRefV5 | None = None,
    ) -> None:
        testable = is_testable_experiment_status_v5(record.status)
        complete_pair = record.critic_review is not None and record.critic_artifact_ref is not None
        absent_pair = record.critic_review is None and record.critic_artifact_ref is None
        if (testable and not complete_pair) or (not testable and not absent_pair):
            if record_reference is not None:
                raise ArtifactSchemaFailureV5(record_reference)
            raise ValueError("experiment critic binding differs from status testability")
        if not testable:
            return
        assert record.critic_artifact_ref is not None
        artifact = self.load_critic(record.critic_artifact_ref)
        matches = tuple(review for review in artifact.reviews if review.experiment_id == record.experiment_id)
        if matches != (record.critic_review,):
            if record_reference is not None:
                raise ArtifactSchemaFailureV5(record_reference)
            raise ValueError("experiment review differs from its full critic artifact")

    def append_experiment(self, record: ExperimentRecordV5) -> ArtifactRefV5:
        if type(record) is not ExperimentRecordV5:
            raise ValueError("experiment record must use the V5 schema")
        for reference in record.artifact_refs:
            self.authenticate(reference)
        self._validate_experiment_critic_binding(record)
        path = f"records/{record.experiment_id}.json"
        reference = self._create_only(path, record.to_primitive())
        if reference.sha256 != record.sha256:
            raise ArtifactNonCanonicalV5(reference)
        return reference

    def load_experiment(self, reference: ArtifactRefV5) -> ExperimentRecordV5:
        authenticated = self.authenticate(reference)
        try:
            primitive = _strict_json_object(authenticated.content, reference)
            record = _decode_experiment_record(primitive)
        except ArtifactRepositoryFailureV5:
            raise
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(reference) from None
        if record.canonical_json_bytes() != authenticated.content or record.sha256 != reference.sha256:
            raise ArtifactNonCanonicalV5(reference)
        self._validate_experiment_critic_binding(record, record_reference=reference)
        return record

    def load_experiment_journal(self) -> tuple[StoredExperimentRecordV5, ...]:
        try:
            names = self._names(("records",))
        except ArtifactMissingV5:
            return ()
        if any(re.fullmatch(r"[0-9a-f]{64}\.json", name) is None for name in names):
            raise ArtifactSchemaFailureV5()
        stored: list[StoredExperimentRecordV5] = []
        for name in names:
            relative = f"records/{name}"
            raw = self._read_relative(relative)
            reference = ArtifactRefV5(relative, hashlib.sha256(raw).hexdigest())
            record = self.load_experiment(reference)
            if name != f"{record.experiment_id}.json":
                raise ArtifactSchemaFailureV5(reference)
            stored.append(StoredExperimentRecordV5(reference=reference, record=record))
        return tuple(sorted(stored, key=lambda item: (item.record.round_index, item.record.experiment_id)))

    def _verify_record_refs(self, references: tuple[ArtifactRefV5, ...]) -> tuple[StoredExperimentRecordV5, ...]:
        stored = tuple(
            StoredExperimentRecordV5(reference=reference, record=self.load_experiment(reference))
            for reference in references
        )
        if tuple(item.reference for item in stored) != references:
            raise ValueError("record reference order changed")
        by_round: dict[int, list[StoredExperimentRecordV5]] = {}
        for item in stored:
            by_round.setdefault(item.record.round_index, []).append(item)
        for round_records in by_round.values():
            testable = tuple(item for item in round_records if is_testable_experiment_status_v5(item.record.status))
            if not testable:
                continue
            critic_refs = {item.record.critic_artifact_ref for item in testable}
            if len(critic_refs) != 1 or None in critic_refs:
                raise ArtifactSchemaFailureV5(testable[0].reference)
            critic_ref = next(iter(critic_refs))
            assert critic_ref is not None
            artifact = self.load_critic(critic_ref)
            expected_ids = {item.record.experiment_id for item in testable}
            actual_ids = {review.experiment_id for review in artifact.reviews}
            if actual_ids != expected_ids:
                raise ArtifactSchemaFailureV5(critic_ref)
        return stored

    def publish_projection(
        self,
        *,
        record_refs: tuple[ArtifactRefV5, ...],
        reducer: ArchiveReducerV5[T],
        generation: int,
    ) -> RepositoryCheckpointV5:
        if type(record_refs) is not tuple:
            raise ValueError("archive record references must be immutable")
        ordered_refs = tuple(sorted(record_refs, key=lambda item: (item.relative_path, item.sha256)))
        if ordered_refs != record_refs or len(set(record_refs)) != len(record_refs):
            raise ValueError("archive record references must be unique and canonical")
        current = self._load_checkpoint()
        if current is not None:
            if generation == current.generation and record_refs == current.record_refs:
                recovered, _state = self.recover_projection(reducer)
                if recovered is None:
                    raise ArtifactSchemaFailureV5()
                return recovered
            if generation != current.generation + 1:
                raise ValueError("checkpoint generation must advance exactly once")
            if not set(current.record_refs).issubset(record_refs):
                raise ValueError("checkpoint cannot forget finalized experiment records")
        elif generation != 1:
            raise ValueError("first checkpoint generation must be one")
        stored = self._verify_record_refs(record_refs)
        state = reduce_experiment_journal_v5(tuple(item.record for item in stored), reducer)
        snapshot = ArchiveSnapshotV5(
            generation=generation,
            record_refs=record_refs,
            projection=reducer.to_primitive(state),
        )
        archive_ref = self._replace("archive.json", snapshot.to_primitive())
        checkpoint = RepositoryCheckpointV5(
            generation=generation,
            archive_sha256=archive_ref.sha256,
            record_refs=record_refs,
        )
        self._replace("checkpoint.json", checkpoint.to_primitive())
        return checkpoint

    def _load_checkpoint(self) -> RepositoryCheckpointV5 | None:
        try:
            raw = self._read_relative("checkpoint.json")
        except ArtifactMissingV5:
            return None
        reference = ArtifactRefV5("checkpoint.json", hashlib.sha256(raw).hexdigest())
        primitive = _strict_json_object(raw, reference)
        item = _exact_keys(
            primitive,
            {
                "schema_version",
                "artifact_type",
                "generation",
                "archive_sha256",
                "record_refs",
            },
            reference,
        )
        if item["schema_version"] != 5 or item["artifact_type"] != "checkpoint":
            raise ArtifactSchemaFailureV5(reference)
        refs = item["record_refs"]
        if type(refs) is not list:
            raise ArtifactSchemaFailureV5(reference)
        try:
            checkpoint = RepositoryCheckpointV5(
                generation=item["generation"],  # type: ignore[arg-type]
                archive_sha256=item["archive_sha256"],  # type: ignore[arg-type]
                record_refs=tuple(_decode_dataclass(ArtifactRefV5, value) for value in refs),
            )
        except (TypeError, ValueError):
            raise ArtifactSchemaFailureV5(reference) from None
        if canonical_json_bytes_v5(checkpoint.to_primitive()) != raw:
            raise ArtifactNonCanonicalV5(reference)
        return checkpoint

    def load_checkpoint(self) -> RepositoryCheckpointV5 | None:
        """Authenticate the current checkpoint without repairing or mutating it."""

        return self._load_checkpoint()

    def load_frozen_discovery_champion(
        self,
        experiment_ref: ArtifactRefV5,
    ) -> ArchiveEntryV5:
        """Read the checkpoint-authenticated leading archive entry without repair."""

        checkpoint = self._load_checkpoint()
        if checkpoint is None or experiment_ref not in checkpoint.record_refs:
            raise ArtifactSchemaFailureV5(experiment_ref)
        archive_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
        authenticated = self.authenticate(archive_ref)
        primitive = _strict_json_object(authenticated.content, archive_ref)
        item = _exact_keys(
            primitive,
            {"schema_version", "artifact_type", "generation", "record_refs", "projection"},
            archive_ref,
        )
        refs = item["record_refs"]
        if (
            item["schema_version"] != 5
            or item["artifact_type"] != "archive"
            or type(refs) is not list
        ):
            raise ArtifactSchemaFailureV5(archive_ref)
        try:
            snapshot = ArchiveSnapshotV5(
                generation=item["generation"],  # type: ignore[arg-type]
                record_refs=tuple(_decode_dataclass(ArtifactRefV5, value) for value in refs),
                projection=item["projection"],
            )
            state = _decode_dataclass(SearchStateV5, snapshot.projection)
        except (TypeError, ValueError, ArithmeticError):
            raise ArtifactSchemaFailureV5(archive_ref) from None
        if (
            canonical_json_bytes_v5(snapshot.to_primitive()) != authenticated.content
            or snapshot.generation != checkpoint.generation
            or snapshot.record_refs != checkpoint.record_refs
            or not state.archive.entries
            or state.archive.entries[0].experiment_record_ref != experiment_ref
        ):
            raise ArtifactSchemaFailureV5(archive_ref)
        return state.archive.entries[0]

    def recover_projection(self, reducer: ArchiveReducerV5[T]) -> tuple[RepositoryCheckpointV5 | None, T]:
        checkpoint = self._load_checkpoint()
        if checkpoint is None:
            return None, reducer.initial()
        stored = self._verify_record_refs(checkpoint.record_refs)
        state = reduce_experiment_journal_v5(tuple(item.record for item in stored), reducer)
        expected = ArchiveSnapshotV5(
            generation=checkpoint.generation,
            record_refs=checkpoint.record_refs,
            projection=reducer.to_primitive(state),
        )
        raw = canonical_json_bytes_v5(expected.to_primitive())
        expected_digest = hashlib.sha256(raw).hexdigest()
        if expected_digest != checkpoint.archive_sha256:
            raise ArtifactSchemaFailureV5(ArtifactRefV5("checkpoint.json", "0" * 64))
        archive_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
        try:
            authenticated = self.authenticate(archive_ref)
            if authenticated.content != raw:
                raise ArtifactNonCanonicalV5(archive_ref)
        except (ArtifactMissingV5, ArtifactDigestMismatchV5):
            repaired = self._replace("archive.json", expected.to_primitive())
            if repaired.sha256 != checkpoint.archive_sha256:
                raise ArtifactNonCanonicalV5(repaired) from None
        return checkpoint, state

    def _authenticate_raw_campaign_edge(self, reference: ArtifactRefV5) -> AuthenticatedRawArtifactV5:
        """Stream exact data bytes. Only verify_graph's closed plan edges select this codec."""

        def same_file_version(left, right):
            return (
                _metadata_identity(left) == _metadata_identity(right)
                and left.st_size == right.st_size
                and left.st_mtime_ns == right.st_mtime_ns
                # Windows stat and descriptor-stat disagree on creation/change
                # time; file identity, size, mtime, and exact digest bind bytes.
                and (os.name == "nt" or left.st_ctime_ns == right.st_ctime_ns)
                and stat.S_ISREG(right.st_mode)
                and right.st_nlink == 1
            )

        parts = _safe_relative_path(reference.relative_path)
        try:
            with self._directory(tuple(parts[:-1]), create=False) as directory:
                name = parts[-1]
                if os.name == "nt":
                    before = os.lstat(directory.path / name)
                    if _is_link_or_reparse(directory.path / name):
                        raise ArtifactRelocatedV5(reference, reference.relative_path)
                    descriptor = os.open(directory.path / name, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                else:
                    before = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
                    descriptor = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory.descriptor,
                    )
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or not same_file_version(before, opened)
                    ):
                        raise ArtifactRelocatedV5(reference, reference.relative_path)
                    digest = hashlib.sha256()
                    size = 0
                    while size < opened.st_size:
                        chunk = os.read(descriptor, min(1024 * 1024, opened.st_size - size))
                        if not chunk:
                            raise ArtifactRelocatedV5(reference, reference.relative_path)
                        digest.update(chunk)
                        size += len(chunk)
                    if os.read(descriptor, 1) or not same_file_version(opened, os.fstat(descriptor)):
                        raise ArtifactRelocatedV5(reference, reference.relative_path)
                finally:
                    os.close(descriptor)
                after = (
                    os.lstat(directory.path / name)
                    if os.name == "nt"
                    else os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
                )
                if not same_file_version(opened, after):
                    raise ArtifactRelocatedV5(reference, reference.relative_path)
                if digest.hexdigest() != reference.sha256:
                    raise ArtifactDigestMismatchV5(reference, digest.hexdigest())
                return AuthenticatedRawArtifactV5(reference, size)
        except ArtifactRepositoryFailureV5:
            raise
        except FileNotFoundError:
            raise ArtifactMissingV5(reference) from None
        except (OSError, ValueError):
            raise ArtifactRelocatedV5(reference, reference.relative_path) from None

    def verify_graph(self, manifest_ref: ArtifactRefV5) -> ArtifactGraphVerificationV5:
        authenticated: list[AuthenticatedArtifactV5 | AuthenticatedRawArtifactV5] = []
        authenticated_keys: set[tuple[str, str]] = set()
        panels: dict[tuple[str, str], EvaluationPanelSpec] = {}
        complete: set[tuple[str, str, str]] = set()
        active: set[tuple[str, str]] = set()

        def visit(
            reference: ArtifactRefV5,
            kind: Literal["generic", "manifest", "panel_plan", "panel", "campaign_raw"] = "generic",
        ) -> None:
            key = (reference.relative_path, reference.sha256)
            if key in active:
                raise ArtifactCycleV5(reference)
            typed_key = (*key, kind)
            if typed_key in complete:
                return
            active.add(key)
            if kind == "panel":
                panels[key], item = self._authenticate_evaluation_panel_spec(reference)
            elif kind == "campaign_raw":
                item = self._authenticate_raw_campaign_edge(reference)
            else:
                item = self.authenticate(reference)
            if key not in authenticated_keys:
                authenticated.append(item)
                authenticated_keys.add(key)
            if kind in {"manifest", "panel_plan"}:
                value_type = CampaignManifestV5 if kind == "manifest" else CampaignPanelPlanV5
                try:
                    value = _decode_dataclass(value_type, _strict_json_object(item.content, reference))
                    if canonical_json_bytes_v5(value) != item.content:
                        raise ArtifactNonCanonicalV5(reference)
                except ArtifactRepositoryFailureV5:
                    raise
                except (TypeError, ValueError, ArithmeticError, RecursionError):
                    raise ArtifactSchemaFailureV5(reference) from None
                if type(value) is CampaignManifestV5:
                    for child in item.child_references:
                        visit(child, "panel_plan" if child == value.panel_plan_ref else "generic")
                else:
                    visit(value.pit_bundle_ref, "campaign_raw")
                    visit(value.prices_provenance_ref, "campaign_raw")
                    for episode in (value.mechanics, value.quick, *value.discovery):
                        visit(episode.panel_ref, "panel")
                        panel = panels[(episode.panel_ref.relative_path, episode.panel_ref.sha256)]
                        try:
                            validate_episode_plan_panel_v5(episode, panel)
                            if episode.purpose != panel.purpose:
                                raise ValueError
                        except ValueError:
                            raise ArtifactSchemaFailureV5(episode.panel_ref) from None
            elif kind != "campaign_raw":
                for child in item.child_references:
                    visit(child)
            active.remove(key)
            complete.add(typed_key)

        try:
            visit(manifest_ref, "manifest")
        except ArtifactRepositoryFailureV5 as exc:
            reference = exc.reference or manifest_ref
            if exc.code == "relocated":
                failure = ArtifactGraphFailureV5(
                    code="relocated",
                    reference=reference,
                    relocated_path=getattr(exc, "relocated_path", reference.relative_path),
                )
            elif exc.code == "cycle":
                failure = ArtifactGraphFailureV5(code="cycle", reference=reference)
            elif exc.code == "digest_mismatch":
                failure = ArtifactGraphFailureV5(
                    code="digest_mismatch",
                    reference=reference,
                    actual_sha256=getattr(exc, "actual_sha256", "0" * 64),
                )
            else:
                failure = ArtifactGraphFailureV5(code="missing", reference=reference)
            return ArtifactGraphVerificationV5(
                manifest_ref=manifest_ref,
                authenticated=tuple(authenticated),
                failure=failure,
            )
        return ArtifactGraphVerificationV5(
            manifest_ref=manifest_ref,
            authenticated=tuple(authenticated),
            failure=None,
        )


__all__ = [
    "ArchiveSnapshotV5",
    "ArtifactCycleV5",
    "ArtifactDigestMismatchV5",
    "ArtifactExistsV5",
    "ArtifactFailureCodeV5",
    "ArtifactMissingV5",
    "ArtifactNonCanonicalV5",
    "ArtifactRelocatedV5",
    "ArtifactRepositoryFailureV5",
    "ArtifactSchemaFailureV5",
    "LocalArtifactRepositoryV5",
    "PersistedRoleInvocationV5",
    "PersistedRoleRequestV5",
    "RepositoryCheckpointV5",
    "RoleLedgerMigrationAuthorityV5",
    "RoleLedgerMigrationRequiredV5",
]
