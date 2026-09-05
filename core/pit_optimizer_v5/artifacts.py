"""Contained, authenticated local artifact storage for PIT optimizer V5."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import types
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar, get_args, get_origin, get_type_hints

from core.pit_optimizer_artifacts import (
    _acquire_absolute_directory,
    _acquire_directory,
    _atomic_replace_in_directory,
    _is_link_or_reparse,
    _metadata_identity,
    _write_create_only_in_directory,
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
    ArtifactGraphFailureV5,
    ArtifactGraphVerificationV5,
    ArtifactRefV5,
    AuthenticatedArtifactV5,
    CampaignEvidenceV5,
    CriticArtifactV5,
    CriticReviewV5,
    EpisodeEvaluationV5,
    HypothesisV5,
    PanelEvaluationV5,
    ValidationResultV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.memory import (
    ArchiveReducerV5,
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
from core.pit_optimizer_v5.provider import (
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
    RoleNameV5,
    RoleRequestV5,
    RoleSchemaAuthorityV5,
    RoleTerminalAuthorityV5,
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
        if type(reference) is not ArtifactRefV5:
            raise ValueError("artifact authentication requires a V5 reference")
        _safe_relative_path(reference.relative_path)
        try:
            raw = self._read_relative(reference.relative_path)
        except ArtifactMissingV5:
            relocated = self._find_digest(reference.sha256, reference.relative_path)
            if relocated is not None:
                raise ArtifactRelocatedV5(reference, relocated) from None
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

    def verify_graph(self, manifest_ref: ArtifactRefV5) -> ArtifactGraphVerificationV5:
        authenticated: list[AuthenticatedArtifactV5] = []
        complete: set[tuple[str, str]] = set()
        active: set[tuple[str, str]] = set()

        def visit(reference: ArtifactRefV5) -> None:
            key = (reference.relative_path, reference.sha256)
            if key in active:
                raise ArtifactCycleV5(reference)
            if key in complete:
                return
            active.add(key)
            item = self.authenticate(reference)
            authenticated.append(item)
            for child in item.child_references:
                visit(child)
            active.remove(key)
            complete.add(key)

        try:
            visit(manifest_ref)
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
]
