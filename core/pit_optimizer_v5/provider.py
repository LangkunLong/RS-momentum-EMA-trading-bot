"""Provider-neutral, one-shot role boundary for PIT optimizer V5."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    SourceFileV5,
    SourceOperationV5,
    StructuralTemplateV5,
)
from core.pit_optimizer_v5.contracts import (
    CampaignManifestV5,
    CriticArtifactV5,
    CriticReviewV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
    ProviderCapabilitiesV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5


RoleNameV5 = Literal["investigator", "author", "critic"]
RoleAttemptKindV5 = Literal["primary", "retry", "repair"]
RoleOutcomeV5 = Literal[
    "accepted",
    "transport_failure",
    "response_schema_failure",
    "evidence_binding_failure",
    "authorization_failure",
    "accounting_failure",
]
ParsedRoleArtifactV5 = InvestigatorArtifactV5 | StructuralTemplateV5 | CriticArtifactV5

_ROLE_NAMES = frozenset(("investigator", "author", "critic"))
_ATTEMPT_KINDS = frozenset(("primary", "retry", "repair"))
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"v5\.[a-z0-9_.-]{1,120}")
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/|\\\\)")
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "accession",
        "api_key",
        "credential",
        "credentials",
        "close",
        "environment",
        "filesystem",
        "file",
        "files",
        "heldout",
        "high",
        "instrument",
        "instruments",
        "low",
        "ohlcv",
        "open",
        "path",
        "paths",
        "price",
        "prices",
        "password",
        "qualification",
        "raw",
        "row",
        "rows",
        "secret",
        "security",
        "symbol",
        "symbols",
        "timestamp",
        "ticker",
        "tickers",
        "url",
        "volume",
    }
)
_FORBIDDEN_COMPOUND_KEYS = frozenset(
    {
        "filesystempath",
        "filesystempaths",
        "heldoutresult",
        "heldoutresults",
        "rawmarketrow",
        "rawmarketrows",
        "rawrow",
        "rawrows",
        "securityid",
        "securityids",
        "tickersymbol",
        "tickersymbols",
    }
)
_MAX_ROLE_REQUEST_BYTES = 256 * 1024
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:\bbearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|\bapi[_ -]?key\s*[:=])",
    re.IGNORECASE,
)
_TICKER_VALUE_RE = re.compile(r"(?:\$?[A-Z]{1,5}|[A-Z]{1,5}\.(?:TO|US))")


def _role(value: object) -> RoleNameV5:
    if type(value) is not str or value not in _ROLE_NAMES:
        raise ValueError("role must be a closed V5 role")
    return value  # type: ignore[return-value]


def _attempt_kind(value: object) -> RoleAttemptKindV5:
    if type(value) is not str or value not in _ATTEMPT_KINDS:
        raise ValueError("role attempt kind is invalid")
    return value  # type: ignore[return-value]


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _count(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _exact_mapping(value: object, keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys or any(type(key) is not str for key in value):
        raise ValueError("mapping differs from its closed schema")
    return value


def _string_tuple(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if type(value) is not list or (required and not value) or any(type(item) is not str for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must be unique")
    return result


def _duplicate_rejecting_json(raw: str | bytes, *, require_canonical: bool) -> object:
    class DuplicateKey(ValueError):
        pass

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise DuplicateKey
            result[key] = value
        return result

    try:
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if type(encoded) is not bytes:
            raise TypeError
        decoded = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if require_canonical and canonical_json_bytes_v5(decoded) != encoded:
            raise ValueError
        return decoded
    except (DuplicateKey, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ValueError("JSON is not duplicate-free canonical UTF-8") from None


def _freeze_json(value: object) -> object:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("role input contains a non-finite number")
        return value
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)  # type: ignore[arg-type]
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("role input keys must be strings")
        return MappingProxyType({key: _freeze_json(value[key]) for key in sorted(value)})
    raise ValueError("role input is not an exact JSON primitive")


def _json_schema_object(properties: Mapping[str, object], required: tuple[str, ...]) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
        "type": "object",
    }


def _text_schema() -> dict[str, object]:
    return {"minLength": 1, "type": "string"}


def _string_array_schema(*, minimum: int = 0, maximum: int | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "items": _text_schema(),
        "minItems": minimum,
        "type": "array",
        "uniqueItems": True,
    }
    if maximum is not None:
        result["maxItems"] = maximum
    return result


def _binding_schema() -> dict[str, object]:
    properties = {
        "discovery_plan_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
        "experiment_ids": _string_array_schema(),
        "hypothesis_id": {"type": ["string", "null"]},
        "parent_revision_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
    }
    return _json_schema_object(properties, tuple(properties))


def _evidence_ids_schema() -> dict[str, object]:
    return {
        "items": {"pattern": r"^v5\.[a-z0-9_.-]{1,120}$", "type": "string"},
        "minItems": 1,
        "type": "array",
        "uniqueItems": True,
    }


def _hypothesis_schema() -> dict[str, object]:
    prediction = _json_schema_object(
        {
            "direction": {"enum": ["decrease", "increase", "unchanged"], "type": "string"},
            "metric_id": _text_schema(),
            "rationale": _text_schema(),
        },
        ("direction", "metric_id", "rationale"),
    )
    properties = {
        "author_instructions": _text_schema(),
        "causal_claim": _text_schema(),
        "evidence_ids": _evidence_ids_schema(),
        "hypothesis_id": _text_schema(),
        "predicted_changes": {"items": prediction, "minItems": 1, "type": "array"},
        "primary_mechanism": {
            "enum": ["cross_policy", "entry", "exit", "position_management", "risk_sizing"],
            "type": "string",
        },
        "rank": {"minimum": 1, "type": "integer"},
    }
    return _json_schema_object(properties, tuple(properties))


def _investigator_artifact_schema(count: int) -> dict[str, object]:
    return _json_schema_object(
        {
            "hypotheses": {
                "items": _hypothesis_schema(),
                "maxItems": count,
                "minItems": count,
                "type": "array",
            }
        },
        ("hypotheses",),
    )


def _literal_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _json_schema_object(
                {"kind": {"const": "bool"}, "value": {"type": "boolean"}},
                ("kind", "value"),
            ),
            _json_schema_object(
                {"kind": {"const": "int"}, "value": {"type": "integer"}},
                ("kind", "value"),
            ),
            _json_schema_object(
                {
                    "kind": {"const": "float"},
                    "value": {
                        "pattern": r"^-?0x[0-9a-f]+\.[0-9a-f]+p[+-][0-9]+$",
                        "type": "string",
                    },
                },
                ("kind", "value"),
            ),
            _json_schema_object(
                {"kind": {"const": "str"}, "value": {"type": "string"}},
                ("kind", "value"),
            ),
        ]
    }


def _author_artifact_schema(
    *,
    paths: tuple[str, ...],
    max_tunable_axes: int,
    max_variants: int,
    allow_full_source_escape: bool,
) -> dict[str, object]:
    operation = _json_schema_object(
        {
            "kind": {"enum": ["replace_constant", "replace_function"], "type": "string"},
            "path": {"enum": list(paths), "type": "string"},
            "replacement_source": _text_schema(),
            "symbol": _text_schema(),
        },
        ("kind", "path", "replacement_source", "symbol"),
    )
    axis = _json_schema_object(
        {
            "default": _literal_schema(),
            "name": _text_schema(),
            "values": {
                "items": _literal_schema(),
                "maxItems": max_variants,
                "minItems": 1,
                "type": "array",
            },
        },
        ("default", "name", "values"),
    )
    source_file = _json_schema_object(
        {
            "path": {"enum": list(paths), "type": "string"},
            "source": _text_schema(),
        },
        ("path", "source"),
    )
    full_source: object = {"type": "null"}
    if allow_full_source_escape:
        full_source = {
            "oneOf": [
                {"type": "null"},
                {
                    "items": source_file,
                    "maxItems": len(EDITABLE_POLICY_PATHS_V5),
                    "minItems": len(EDITABLE_POLICY_PATHS_V5),
                    "type": "array",
                },
            ]
        }
    properties = {
        "axes": {
            "items": axis,
            "maxItems": max_tunable_axes,
            "type": "array",
        },
        "changed_symbols": _string_array_schema(minimum=1),
        "full_source_escape": full_source,
        "hypothesis_id": _text_schema(),
        "parent_revision_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
        "source_operations": {"items": operation, "type": "array"},
    }
    return _json_schema_object(properties, tuple(properties))


def _critic_artifact_schema() -> dict[str, object]:
    review_properties = {
        "causal_explanation": _text_schema(),
        "disposition": {"enum": ["abandon", "promote", "refine"], "type": "string"},
        "evidence_ids": _evidence_ids_schema(),
        "experiment_id": _text_schema(),
        "next_direction": _text_schema(),
        "prediction_vs_observation": _text_schema(),
    }
    properties = {
        "comparative_assessment": _text_schema(),
        "evidence_ids": _evidence_ids_schema(),
        "next_campaign_direction": _text_schema(),
        "reviews": {
            "items": _json_schema_object(review_properties, tuple(review_properties)),
            "minItems": 1,
            "type": "array",
        },
    }
    return _json_schema_object(properties, tuple(properties))


def _role_schema_document(
    *,
    role: RoleNameV5,
    hypotheses_per_investigator: int,
    max_tunable_axes: int,
    max_variants: int,
    allow_full_source_escape: bool,
    policy_scope_sha256: str,
    author_policy_paths: tuple[str, ...],
) -> dict[str, object]:
    if role == "investigator":
        artifact = _investigator_artifact_schema(hypotheses_per_investigator)
    elif role == "author":
        artifact = _author_artifact_schema(
            paths=author_policy_paths,
            max_tunable_axes=max_tunable_axes,
            max_variants=max_variants,
            allow_full_source_escape=allow_full_source_escape,
        )
    else:
        artifact = _critic_artifact_schema()
    envelope = _json_schema_object(
        {"artifact": artifact, "binding": _binding_schema()},
        ("artifact", "binding"),
    )
    envelope["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    envelope["x-pit-optimizer-v5-authority"] = {
        "allow_full_source_escape": allow_full_source_escape,
        "author_policy_paths": list(author_policy_paths),
        "hypotheses_per_investigator": hypotheses_per_investigator,
        "max_tunable_axes": max_tunable_axes,
        "max_variants": max_variants,
        "policy_scope_sha256": policy_scope_sha256,
        "role": role,
    }
    return envelope


@dataclass(frozen=True, slots=True)
class IssuedEvidenceV5:
    evidence_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.evidence_id) is not str or _EVIDENCE_ID_RE.fullmatch(self.evidence_id) is None:
            raise ValueError("issued evidence ID is invalid")
        _digest(self.payload_sha256, "issued evidence payload")

    def to_primitive(self) -> dict[str, str]:
        return {"evidence_id": self.evidence_id, "payload_sha256": self.payload_sha256}


@dataclass(frozen=True, slots=True)
class RoleBindingV5:
    parent_revision_sha256: str
    hypothesis_id: str | None
    experiment_ids: tuple[str, ...]
    discovery_plan_sha256: str

    def __post_init__(self) -> None:
        _digest(self.parent_revision_sha256, "role parent revision")
        _digest(self.discovery_plan_sha256, "role discovery plan")
        if self.hypothesis_id is not None:
            _text(self.hypothesis_id, "role hypothesis ID")
        if type(self.experiment_ids) is not tuple or any(type(item) is not str for item in self.experiment_ids):
            raise ValueError("role experiment IDs are invalid")
        tuple(_text(item, "role experiment ID") for item in self.experiment_ids)
        if len(set(self.experiment_ids)) != len(self.experiment_ids):
            raise ValueError("role experiment IDs must be unique")

    def to_primitive(self) -> dict[str, object]:
        return {
            "parent_revision_sha256": self.parent_revision_sha256,
            "hypothesis_id": self.hypothesis_id,
            "experiment_ids": self.experiment_ids,
            "discovery_plan_sha256": self.discovery_plan_sha256,
        }


@dataclass(frozen=True, slots=True)
class RoleSchemaAuthorityV5:
    """Deeply immutable response schema plus its manifest-derived variable authority."""

    role: RoleNameV5
    canonical_schema_json: bytes
    hypotheses_per_investigator: int
    max_tunable_axes: int
    max_variants: int
    allow_full_source_escape: bool
    policy_scope_sha256: str
    author_policy_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        role = _role(self.role)
        if type(self.canonical_schema_json) is not bytes:
            raise ValueError("response schema must be immutable canonical JSON bytes")
        _count(self.hypotheses_per_investigator, "schema hypothesis count", positive=True)
        _count(self.max_tunable_axes, "schema tunable-axis maximum", positive=True)
        _count(self.max_variants, "schema variant maximum", positive=True)
        if type(self.allow_full_source_escape) is not bool:
            raise ValueError("schema full-source capability must be boolean")
        _digest(self.policy_scope_sha256, "schema policy scope")
        if (
            type(self.author_policy_paths) is not tuple
            or any(type(item) is not str for item in self.author_policy_paths)
            or len(set(self.author_policy_paths)) != len(self.author_policy_paths)
            or tuple(path for path in EDITABLE_POLICY_PATHS_V5 if path in self.author_policy_paths)
            != self.author_policy_paths
        ):
            raise ValueError("schema author paths are invalid")
        if role == "author":
            if not self.author_policy_paths:
                raise ValueError("author schema requires an exact policy path scope")
            if self.allow_full_source_escape and self.author_policy_paths != EDITABLE_POLICY_PATHS_V5:
                raise ValueError("full-source author schema requires the exact four-file scope")
        elif self.author_policy_paths or self.allow_full_source_escape:
            raise ValueError("non-author schema cannot carry source authority")
        expected = _role_schema_document(
            role=role,
            hypotheses_per_investigator=self.hypotheses_per_investigator,
            max_tunable_axes=self.max_tunable_axes,
            max_variants=self.max_variants,
            allow_full_source_escape=self.allow_full_source_escape,
            policy_scope_sha256=self.policy_scope_sha256,
            author_policy_paths=self.author_policy_paths,
        )
        decoded = _duplicate_rejecting_json(self.canonical_schema_json, require_canonical=True)
        if decoded != expected:
            raise ValueError("response schema bytes differ from their declared authority")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_schema_json).hexdigest()


def role_schema_authority_from_manifest_v5(
    *,
    role: RoleNameV5,
    manifest: CampaignManifestV5,
    author_policy_paths: tuple[str, ...] = (),
    full_source_escape: bool = False,
) -> RoleSchemaAuthorityV5:
    """Derive one role schema only from a validated manifest and exact policy scope."""

    canonical_role = _role(role)
    if type(manifest) is not CampaignManifestV5:
        raise ValueError("role schema manifest is invalid")
    if type(full_source_escape) is not bool:
        raise ValueError("role schema full-source selection is invalid")
    if canonical_role == "author":
        if full_source_escape:
            if not manifest.search.allow_full_source_escape:
                raise ValueError("manifest does not authorize full-source escape")
            if author_policy_paths != EDITABLE_POLICY_PATHS_V5:
                raise ValueError("full-source request must carry all four editable paths")
        elif not author_policy_paths:
            raise ValueError("normal author request requires its exact editable path subset")
    elif author_policy_paths or full_source_escape:
        raise ValueError("only author schemas may select editable policy paths")
    document = _role_schema_document(
        role=canonical_role,
        hypotheses_per_investigator=manifest.search.hypotheses_per_investigator,
        max_tunable_axes=manifest.search.max_tunable_axes,
        max_variants=manifest.search.max_variants_per_template,
        allow_full_source_escape=full_source_escape,
        policy_scope_sha256=manifest.policy_scope_ref.sha256,
        author_policy_paths=author_policy_paths,
    )
    return RoleSchemaAuthorityV5(
        role=canonical_role,
        canonical_schema_json=canonical_json_bytes_v5(document),
        hypotheses_per_investigator=manifest.search.hypotheses_per_investigator,
        max_tunable_axes=manifest.search.max_tunable_axes,
        max_variants=manifest.search.max_variants_per_template,
        allow_full_source_escape=full_source_escape,
        policy_scope_sha256=manifest.policy_scope_ref.sha256,
        author_policy_paths=author_policy_paths,
    )


def _forbidden_key(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", separated).lower().strip("_")
    collapsed = normalized.replace("_", "")
    return bool(_FORBIDDEN_KEY_PARTS.intersection(normalized.split("_")) or collapsed in _FORBIDDEN_COMPOUND_KEYS)


def _validate_aggregate_only(value: object, *, allow_policy_source: bool = False) -> None:
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is str:
        if not allow_policy_source and (
            "\\" in value
            or _ABSOLUTE_PATH_RE.search(value)
            or _SENSITIVE_VALUE_RE.search(value)
            or _TICKER_VALUE_RE.fullmatch(value)
        ):
            raise ValueError("role input contains non-aggregate or sensitive text")
        return
    if type(value) is tuple:
        for item in value:
            _validate_aggregate_only(item, allow_policy_source=allow_policy_source)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str or (not allow_policy_source and _forbidden_key(key)):
                raise ValueError("role input contains a forbidden non-aggregate field")
            _validate_aggregate_only(item, allow_policy_source=allow_policy_source)
        return
    raise ValueError("role input is not a closed immutable JSON value")


def _decode_prediction(value: object) -> MetricPredictionV5:
    item = _exact_mapping(
        value,
        frozenset(("metric_id", "direction", "rationale")),
    )
    return MetricPredictionV5(
        metric_id=item["metric_id"],  # type: ignore[arg-type]
        direction=item["direction"],  # type: ignore[arg-type]
        rationale=item["rationale"],  # type: ignore[arg-type]
    )


def _decode_hypothesis(value: object) -> HypothesisV5:
    item = _exact_mapping(
        value,
        frozenset(
            (
                "hypothesis_id",
                "rank",
                "primary_mechanism",
                "causal_claim",
                "predicted_changes",
                "evidence_ids",
                "author_instructions",
            )
        ),
    )
    predictions = item["predicted_changes"]
    if type(predictions) is not list:
        raise ValueError("hypothesis predictions must be a JSON array")
    return HypothesisV5(
        hypothesis_id=item["hypothesis_id"],  # type: ignore[arg-type]
        rank=item["rank"],  # type: ignore[arg-type]
        primary_mechanism=item["primary_mechanism"],  # type: ignore[arg-type]
        causal_claim=item["causal_claim"],  # type: ignore[arg-type]
        predicted_changes=tuple(_decode_prediction(prediction) for prediction in predictions),
        evidence_ids=_string_tuple(item["evidence_ids"], "hypothesis evidence", required=True),
        author_instructions=item["author_instructions"],  # type: ignore[arg-type]
    )


def _validate_batch_rows(value: object, experiment_ids: tuple[str, ...], label: str) -> None:
    if type(value) is not tuple or len(value) != len(experiment_ids):
        raise ValueError(f"critic {label} must cover the complete experiment batch")
    actual: list[str] = []
    for row in value:
        if not isinstance(row, Mapping) or "experiment_id" not in row:
            raise ValueError(f"critic {label} row is invalid")
        experiment_id = row["experiment_id"]
        if type(experiment_id) is not str:
            raise ValueError(f"critic {label} experiment ID is invalid")
        actual.append(experiment_id)
    if tuple(actual) != experiment_ids:
        raise ValueError(f"critic {label} differs from the complete experiment batch")


def _validate_role_input(
    *,
    role: RoleNameV5,
    role_input: Mapping[str, object],
    binding: RoleBindingV5,
    schema_authority: RoleSchemaAuthorityV5,
    issued_ids: frozenset[str],
) -> None:
    if role == "investigator":
        item = _exact_mapping(
            role_input,
            frozenset(
                (
                    "aggregate_evaluator_evidence",
                    "archive_family_summaries",
                    "critic_directions",
                )
            ),
        )
        if any(type(item[name]) is not tuple for name in item):
            raise ValueError("investigator input sections must be immutable arrays")
        _validate_aggregate_only(item)
        return
    if role == "author":
        item = _exact_mapping(
            role_input,
            frozenset(
                (
                    "editable_sources",
                    "full_source_escape",
                    "hypothesis",
                    "policy_contracts",
                )
            ),
        )
        if type(item["full_source_escape"]) is not bool or item["full_source_escape"] is not (
            schema_authority.allow_full_source_escape
        ):
            raise ValueError("author full-source mode differs from its schema authority")
        hypothesis_value = canonical_primitive_v5(item["hypothesis"])
        hypothesis = _decode_hypothesis(hypothesis_value)
        if hypothesis.hypothesis_id != binding.hypothesis_id:
            raise ValueError("author hypothesis differs from its expected binding")
        if not set(hypothesis.evidence_ids).issubset(issued_ids):
            raise ValueError("author hypothesis cites evidence that was not issued")
        contracts = _exact_mapping(
            item["policy_contracts"],
            frozenset(
                (
                    "immutable_constraints_sha256",
                    "policy_interface_version",
                    "policy_scope_sha256",
                    "trusted_policy_runtime_sha256",
                )
            ),
        )
        if (
            type(contracts["policy_interface_version"]) is not int
            or contracts["policy_interface_version"] != 3
            or contracts["policy_scope_sha256"] != schema_authority.policy_scope_sha256
        ):
            raise ValueError("author V3 policy contracts differ from schema authority")
        _digest(contracts["immutable_constraints_sha256"], "author immutable constraints")
        _digest(contracts["trusted_policy_runtime_sha256"], "author trusted runtime")
        sources = item["editable_sources"]
        if type(sources) is not tuple:
            raise ValueError("author editable sources must be an immutable array")
        decoded_sources = tuple(
            SourceFileV5(
                path=_exact_mapping(source, frozenset(("path", "source")))["path"],  # type: ignore[arg-type]
                source=_exact_mapping(source, frozenset(("path", "source")))["source"],  # type: ignore[arg-type]
            )
            for source in sources
        )
        if tuple(source.path for source in decoded_sources) != schema_authority.author_policy_paths:
            raise ValueError("author sources differ from their exact path authority")
        _validate_aggregate_only(_freeze_json(hypothesis_value))
        _validate_aggregate_only(contracts)
        return
    item = _exact_mapping(
        role_input,
        frozenset(
            (
                "evaluation_summaries",
                "predictions",
                "semantic_differences",
                "typed_failures",
            )
        ),
    )
    for label, value in item.items():
        _validate_batch_rows(value, binding.experiment_ids, label)
    _validate_aggregate_only(item)


@dataclass(frozen=True, slots=True)
class RoleRequestV5:
    role: RoleNameV5
    messages: tuple[Mapping[str, object], ...]
    response_schema_sha256: str
    issued_evidence: tuple[IssuedEvidenceV5, ...]
    expected_binding: RoleBindingV5
    max_output_tokens: int
    schema_authority: RoleSchemaAuthorityV5

    def __post_init__(self) -> None:
        role = _role(self.role)
        if type(self.schema_authority) is not RoleSchemaAuthorityV5 or self.schema_authority.role != role:
            raise ValueError("role request schema authority differs from its role")
        if _digest(self.response_schema_sha256, "role response schema") != self.schema_authority.sha256:
            raise ValueError("role response schema digest differs from its canonical bytes")
        if type(self.expected_binding) is not RoleBindingV5:
            raise ValueError("role request binding is invalid")
        _count(self.max_output_tokens, "role maximum output tokens", positive=True)
        if type(self.issued_evidence) is not tuple or any(
            type(item) is not IssuedEvidenceV5 for item in self.issued_evidence
        ):
            raise ValueError("role issued evidence is invalid")
        issued_ids = tuple(item.evidence_id for item in self.issued_evidence)
        if len(set(issued_ids)) != len(issued_ids):
            raise ValueError("role issued evidence IDs must be unique")
        if role == "investigator" and (
            self.expected_binding.hypothesis_id is not None or self.expected_binding.experiment_ids
        ):
            raise ValueError("investigator binding cannot predeclare hypothesis or experiments")
        if role == "author" and (self.expected_binding.hypothesis_id is None or self.expected_binding.experiment_ids):
            raise ValueError("author binding requires one hypothesis and no experiments")
        if role == "critic" and (
            self.expected_binding.hypothesis_id is None or not self.expected_binding.experiment_ids
        ):
            raise ValueError("critic binding requires a hypothesis and complete experiment batch")
        frozen = _freeze_json(self.messages)
        if type(frozen) is not tuple or len(frozen) != 1 or not isinstance(frozen[0], Mapping):
            raise ValueError("role request must contain one immutable user message")
        message = _exact_mapping(frozen[0], frozenset(("content", "role")))
        if message["role"] != "user" or type(message["role"]) is not str:
            raise ValueError("role request message role is invalid")
        content = _exact_mapping(
            message["content"],
            frozenset(("binding", "evidence", "role_input")),
        )
        if canonical_primitive_v5(content["binding"]) != canonical_primitive_v5(self.expected_binding.to_primitive()):
            raise ValueError("role request message binding differs")
        evidence = content["evidence"]
        if type(evidence) is not tuple or len(evidence) != len(self.issued_evidence):
            raise ValueError("role request evidence payloads are incomplete")
        for supplied, issued in zip(evidence, self.issued_evidence, strict=True):
            row = _exact_mapping(supplied, frozenset(("evidence_id", "payload")))
            if row["evidence_id"] != issued.evidence_id or canonical_sha256_v5(row["payload"]) != issued.payload_sha256:
                raise ValueError("role request evidence payload differs from its issued digest")
            _validate_aggregate_only(row["payload"])
        role_input = content["role_input"]
        if not isinstance(role_input, Mapping):
            raise ValueError("role request input is invalid")
        _validate_role_input(
            role=role,
            role_input=role_input,
            binding=self.expected_binding,
            schema_authority=self.schema_authority,
            issued_ids=frozenset(issued_ids),
        )
        object.__setattr__(self, "messages", frozen)
        if len(canonical_json_bytes_v5(self.to_primitive())) > _MAX_ROLE_REQUEST_BYTES:
            raise ValueError("role request exceeds its aggregate input bound")

    def to_primitive(self) -> dict[str, object]:
        return {
            "role": self.role,
            "messages": self.messages,
            "response_schema_sha256": self.response_schema_sha256,
            "issued_evidence": tuple(item.to_primitive() for item in self.issued_evidence),
            "expected_binding": self.expected_binding.to_primitive(),
            "max_output_tokens": self.max_output_tokens,
            "schema_authority_sha256": self.schema_authority.sha256,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self.to_primitive())


def build_role_request_v5(
    *,
    role: RoleNameV5,
    role_input: Mapping[str, object],
    evidence_payloads: tuple[tuple[str, object], ...],
    expected_binding: RoleBindingV5,
    schema_authority: RoleSchemaAuthorityV5,
    max_output_tokens: int,
) -> RoleRequestV5:
    """Construct an authenticated aggregate-only request without prompt-text inference."""

    canonical_role = _role(role)
    if not isinstance(role_input, Mapping):
        raise ValueError("role input must be a mapping")
    if type(evidence_payloads) is not tuple or any(
        type(item) is not tuple or len(item) != 2 or type(item[0]) is not str for item in evidence_payloads
    ):
        raise ValueError("role evidence payloads are invalid")
    issued = tuple(
        IssuedEvidenceV5(evidence_id=evidence_id, payload_sha256=canonical_sha256_v5(payload))
        for evidence_id, payload in evidence_payloads
    )
    canonical_role_input = canonical_primitive_v5(role_input)
    canonical_evidence_payloads = tuple(
        (evidence_id, canonical_primitive_v5(payload)) for evidence_id, payload in evidence_payloads
    )
    message = {
        "role": "user",
        "content": {
            "binding": expected_binding.to_primitive(),
            "evidence": [
                {"evidence_id": evidence_id, "payload": payload} for evidence_id, payload in canonical_evidence_payloads
            ],
            "role_input": canonical_role_input,
        },
    }
    return RoleRequestV5(
        role=canonical_role,
        messages=(message,),
        response_schema_sha256=schema_authority.sha256,
        issued_evidence=issued,
        expected_binding=expected_binding,
        max_output_tokens=max_output_tokens,
        schema_authority=schema_authority,
    )


class RoleFailureCode(StrEnum):
    """Stable, provider-neutral role failure categories."""

    TRANSPORT = "transport"
    RESPONSE_SCHEMA = "response_schema"
    EVIDENCE_BINDING = "evidence_binding"
    AUTHORIZATION = "authorization"
    ACCOUNTING = "accounting"


class RoleFailureV5(RuntimeError):
    """A sanitized role failure; provider exception details never enter stable data."""

    code: RoleFailureCode
    role: RoleNameV5
    attempt: RoleAttemptFactsV5 | None

    def __init__(
        self,
        *,
        role: RoleNameV5,
        code: RoleFailureCode,
        attempt: RoleAttemptFactsV5 | None = None,
    ) -> None:
        self.role = _role(role)
        if type(code) is not RoleFailureCode:
            raise ValueError("role failure code is invalid")
        self.code = code
        self.attempt = attempt
        super().__init__(f"PIT optimizer V5 {self.role} role failed: {self.code.value}")


class RoleTransportFailureV5(RoleFailureV5):
    def __init__(self, *, role: RoleNameV5, attempt: RoleAttemptFactsV5 | None = None) -> None:
        super().__init__(role=role, code=RoleFailureCode.TRANSPORT, attempt=attempt)


class RoleResponseSchemaFailureV5(RoleFailureV5):
    def __init__(self, *, role: RoleNameV5, attempt: RoleAttemptFactsV5 | None = None) -> None:
        super().__init__(role=role, code=RoleFailureCode.RESPONSE_SCHEMA, attempt=attempt)


class RoleEvidenceBindingFailureV5(RoleFailureV5):
    def __init__(self, *, role: RoleNameV5, attempt: RoleAttemptFactsV5 | None = None) -> None:
        super().__init__(role=role, code=RoleFailureCode.EVIDENCE_BINDING, attempt=attempt)


class RoleAuthorizationFailureV5(RoleFailureV5):
    def __init__(self, *, role: RoleNameV5, attempt: RoleAttemptFactsV5 | None = None) -> None:
        super().__init__(role=role, code=RoleFailureCode.AUTHORIZATION, attempt=attempt)


class RoleAccountingFailureV5(RoleFailureV5):
    def __init__(self, *, role: RoleNameV5, attempt: RoleAttemptFactsV5 | None = None) -> None:
        super().__init__(role=role, code=RoleFailureCode.ACCOUNTING, attempt=attempt)


def _binding_from_json(value: object) -> RoleBindingV5:
    item = _exact_mapping(
        value,
        frozenset(
            (
                "parent_revision_sha256",
                "hypothesis_id",
                "experiment_ids",
                "discovery_plan_sha256",
            )
        ),
    )
    hypothesis_id = item["hypothesis_id"]
    if hypothesis_id is not None and type(hypothesis_id) is not str:
        raise ValueError("role response hypothesis ID is invalid")
    return RoleBindingV5(
        parent_revision_sha256=item["parent_revision_sha256"],  # type: ignore[arg-type]
        hypothesis_id=hypothesis_id,
        experiment_ids=_string_tuple(item["experiment_ids"], "role response experiments"),
        discovery_plan_sha256=item["discovery_plan_sha256"],  # type: ignore[arg-type]
    )


def _investigator_from_json(value: object) -> InvestigatorArtifactV5:
    item = _exact_mapping(value, frozenset(("hypotheses",)))
    hypotheses = item["hypotheses"]
    if type(hypotheses) is not list:
        raise ValueError("investigator hypotheses must be a JSON array")
    return InvestigatorArtifactV5(hypotheses=tuple(_decode_hypothesis(hypothesis) for hypothesis in hypotheses))


def _literal_from_json(value: object) -> bool | int | float | str:
    item = _exact_mapping(value, frozenset(("kind", "value")))
    kind = item["kind"]
    literal = item["value"]
    expected_types: Mapping[str, type[object]] = {
        "bool": bool,
        "int": int,
        "float": str,
        "str": str,
    }
    if type(kind) is not str or kind not in expected_types or type(literal) is not expected_types[kind]:
        raise ValueError("author literal is not tagged with its exact primitive type")
    if kind == "float":
        try:
            parsed = float.fromhex(literal)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("author float literal is invalid") from None
        if not math.isfinite(parsed) or parsed.hex() != literal:
            raise ValueError("author float literal is not canonical and finite")
        return parsed
    return literal  # type: ignore[return-value]


def _source_operation_from_json(value: object) -> SourceOperationV5:
    item = _exact_mapping(
        value,
        frozenset(("path", "symbol", "kind", "replacement_source")),
    )
    return SourceOperationV5(
        path=item["path"],  # type: ignore[arg-type]
        symbol=item["symbol"],  # type: ignore[arg-type]
        kind=item["kind"],  # type: ignore[arg-type]
        replacement_source=item["replacement_source"],  # type: ignore[arg-type]
    )


def _literal_axis_from_json(value: object) -> LiteralAxisV5:
    item = _exact_mapping(value, frozenset(("name", "default", "values")))
    values = item["values"]
    if type(values) is not list:
        raise ValueError("author axis values must be a JSON array")
    return LiteralAxisV5(
        name=item["name"],  # type: ignore[arg-type]
        default=_literal_from_json(item["default"]),
        values=tuple(_literal_from_json(literal) for literal in values),
    )


def _source_file_from_json(value: object) -> SourceFileV5:
    item = _exact_mapping(value, frozenset(("path", "source")))
    return SourceFileV5(
        path=item["path"],  # type: ignore[arg-type]
        source=item["source"],  # type: ignore[arg-type]
    )


def _author_from_json(value: object) -> StructuralTemplateV5:
    item = _exact_mapping(
        value,
        frozenset(
            (
                "hypothesis_id",
                "parent_revision_sha256",
                "changed_symbols",
                "source_operations",
                "axes",
                "full_source_escape",
            )
        ),
    )
    operations = item["source_operations"]
    axes = item["axes"]
    full_source = item["full_source_escape"]
    if type(operations) is not list or type(axes) is not list:
        raise ValueError("author operations and axes must be JSON arrays")
    if full_source is not None and type(full_source) is not list:
        raise ValueError("author full-source escape must be null or a JSON array")
    return StructuralTemplateV5(
        hypothesis_id=item["hypothesis_id"],  # type: ignore[arg-type]
        parent_revision_sha256=item["parent_revision_sha256"],  # type: ignore[arg-type]
        changed_symbols=_string_tuple(item["changed_symbols"], "author changed symbols", required=True),
        source_operations=tuple(_source_operation_from_json(operation) for operation in operations),
        axes=tuple(_literal_axis_from_json(axis) for axis in axes),
        full_source_escape=(
            None if full_source is None else tuple(_source_file_from_json(source_file) for source_file in full_source)
        ),
    )


def _critic_review_from_json(value: object) -> CriticReviewV5:
    item = _exact_mapping(
        value,
        frozenset(
            (
                "experiment_id",
                "prediction_vs_observation",
                "causal_explanation",
                "evidence_ids",
                "disposition",
                "next_direction",
            )
        ),
    )
    return CriticReviewV5(
        experiment_id=item["experiment_id"],  # type: ignore[arg-type]
        prediction_vs_observation=item["prediction_vs_observation"],  # type: ignore[arg-type]
        causal_explanation=item["causal_explanation"],  # type: ignore[arg-type]
        evidence_ids=_string_tuple(item["evidence_ids"], "critic review evidence", required=True),
        disposition=item["disposition"],  # type: ignore[arg-type]
        next_direction=item["next_direction"],  # type: ignore[arg-type]
    )


def _critic_from_json(value: object) -> CriticArtifactV5:
    item = _exact_mapping(
        value,
        frozenset(("reviews", "comparative_assessment", "next_campaign_direction", "evidence_ids")),
    )
    reviews = item["reviews"]
    if type(reviews) is not list:
        raise ValueError("critic reviews must be a JSON array")
    return CriticArtifactV5(
        reviews=tuple(_critic_review_from_json(review) for review in reviews),
        comparative_assessment=item["comparative_assessment"],  # type: ignore[arg-type]
        next_campaign_direction=item["next_campaign_direction"],  # type: ignore[arg-type]
        evidence_ids=_string_tuple(item["evidence_ids"], "critic artifact evidence", required=True),
    )


def _issued_evidence_ids(request: RoleRequestV5) -> frozenset[str]:
    return frozenset(item.evidence_id for item in request.issued_evidence)


def _bind_investigator_artifact(*, request: RoleRequestV5, artifact: InvestigatorArtifactV5) -> None:
    expected_count = request.schema_authority.hypotheses_per_investigator
    if len(artifact.hypotheses) != expected_count or tuple(item.rank for item in artifact.hypotheses) != tuple(
        range(1, expected_count + 1)
    ):
        raise RoleResponseSchemaFailureV5(role=request.role)
    if not set(evidence_id for hypothesis in artifact.hypotheses for evidence_id in hypothesis.evidence_ids).issubset(
        _issued_evidence_ids(request)
    ):
        raise RoleEvidenceBindingFailureV5(role=request.role)


def _bind_author_artifact(*, request: RoleRequestV5, artifact: StructuralTemplateV5) -> None:
    authority = request.schema_authority
    if (
        artifact.parent_revision_sha256 != request.expected_binding.parent_revision_sha256
        or artifact.hypothesis_id != request.expected_binding.hypothesis_id
    ):
        raise RoleEvidenceBindingFailureV5(role=request.role)
    if len(artifact.axes) > authority.max_tunable_axes or any(
        len(axis.values) > authority.max_variants for axis in artifact.axes
    ):
        raise RoleResponseSchemaFailureV5(role=request.role)
    if artifact.full_source_escape is None:
        if authority.allow_full_source_escape or not artifact.source_operations:
            raise RoleResponseSchemaFailureV5(role=request.role)
        if any(operation.path not in authority.author_policy_paths for operation in artifact.source_operations):
            raise RoleResponseSchemaFailureV5(role=request.role)
    elif (
        not authority.allow_full_source_escape
        or artifact.source_operations
        or tuple(item.path for item in artifact.full_source_escape) != authority.author_policy_paths
    ):
        raise RoleResponseSchemaFailureV5(role=request.role)


def _bind_critic_artifact(*, request: RoleRequestV5, artifact: CriticArtifactV5) -> None:
    cited = {
        *artifact.evidence_ids,
        *(evidence_id for review in artifact.reviews for evidence_id in review.evidence_ids),
    }
    if tuple(
        review.experiment_id for review in artifact.reviews
    ) != request.expected_binding.experiment_ids or not cited.issubset(_issued_evidence_ids(request)):
        raise RoleEvidenceBindingFailureV5(role=request.role)


def parse_and_bind_role_artifact(
    *,
    request: RoleRequestV5,
    response_text: str,
) -> ParsedRoleArtifactV5:
    """Parse one canonical role envelope and bind it only to controller-issued authority."""

    if type(request) is not RoleRequestV5:
        raise ValueError("role parser requires an authenticated V5 request")
    if type(response_text) is not str:
        raise RoleResponseSchemaFailureV5(role=request.role)
    try:
        decoded = _duplicate_rejecting_json(response_text, require_canonical=False)
        envelope = _exact_mapping(decoded, frozenset(("artifact", "binding")))
        binding = _binding_from_json(envelope["binding"])
    except (TypeError, ValueError):
        raise RoleResponseSchemaFailureV5(role=request.role) from None
    if binding != request.expected_binding:
        raise RoleEvidenceBindingFailureV5(role=request.role)
    try:
        if request.role == "investigator":
            artifact = _investigator_from_json(envelope["artifact"])
            _bind_investigator_artifact(request=request, artifact=artifact)
        elif request.role == "author":
            artifact = _author_from_json(envelope["artifact"])
            _bind_author_artifact(request=request, artifact=artifact)
        else:
            artifact = _critic_from_json(envelope["artifact"])
            _bind_critic_artifact(request=request, artifact=artifact)
    except RoleFailureV5:
        raise
    except (TypeError, ValueError):
        raise RoleResponseSchemaFailureV5(role=request.role) from None
    return artifact


@dataclass(frozen=True, slots=True)
class ProviderCompletionRequestV5:
    role_request: RoleRequestV5
    model: str

    def __post_init__(self) -> None:
        if type(self.role_request) is not RoleRequestV5:
            raise ValueError("provider completion role request is invalid")
        _text(self.model, "provider completion model")


@dataclass(frozen=True, slots=True)
class CompletionResultV5:
    response_text: str
    accepted: bool
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None
    returned_model: str
    cost_usd: Decimal
    external_attempt_count: Literal[1]
    response_received: bool

    def __post_init__(self) -> None:
        if type(self.response_text) is not str:
            raise ValueError("provider response text is invalid")
        if type(self.accepted) is not bool:
            raise ValueError("provider acceptance flag is invalid")
        _count(self.input_tokens, "provider input tokens")
        _count(self.output_tokens, "provider output tokens")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "provider request ID")
        _text(self.returned_model, "provider returned model")
        if type(self.cost_usd) is not Decimal or not self.cost_usd.is_finite() or self.cost_usd < 0:
            raise ValueError("provider completion cost is invalid")
        if type(self.external_attempt_count) is not int or self.external_attempt_count != 1:
            raise ValueError("provider completion must attest exactly one external attempt")
        if type(self.response_received) is not bool or (self.accepted and not self.response_received):
            raise ValueError("provider completion response state is invalid")


@runtime_checkable
class CompletionProvider(Protocol):
    """Provider-neutral boundary whose one method performs exactly one completion."""

    def complete_once(self, request: ProviderCompletionRequestV5) -> CompletionResultV5: ...


@runtime_checkable
class OneShotJsonCompletionV5(Protocol):
    """Task-9 composition boundary for one accounted JSON completion."""

    def invoke_json_once(
        self,
        *,
        model: str,
        messages: tuple[Mapping[str, object], ...],
        response_schema_json: bytes,
        max_output_tokens: int,
        automatic_retries: int,
        schema_repair_calls: int,
    ) -> CompletionResultV5: ...


class GatewayCompletionProviderV5:
    """Adapt a truthful one-shot callable without claiming a current gateway implementation."""

    __slots__ = ("_gateway",)

    def __init__(self, gateway: OneShotJsonCompletionV5) -> None:
        if not isinstance(gateway, OneShotJsonCompletionV5):
            raise ValueError("V5 completion gateway lacks the one-shot JSON boundary")
        self._gateway = gateway

    def complete_once(self, request: ProviderCompletionRequestV5) -> CompletionResultV5:
        if type(request) is not ProviderCompletionRequestV5:
            raise ValueError("V5 gateway completion request is invalid")
        result = self._gateway.invoke_json_once(
            model=request.model,
            messages=request.role_request.messages,
            response_schema_json=request.role_request.schema_authority.canonical_schema_json,
            max_output_tokens=request.role_request.max_output_tokens,
            automatic_retries=0,
            schema_repair_calls=0,
        )
        if type(result) is not CompletionResultV5:
            raise ValueError("V5 completion gateway returned an invalid result")
        return result


@dataclass(frozen=True, slots=True)
class RoleSlotRequestV5:
    request_sha256: str
    role: RoleNameV5
    attempt_kind: RoleAttemptKindV5
    attempt_index: int
    model: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        _digest(self.request_sha256, "role slot request")
        _role(self.role)
        _attempt_kind(self.attempt_kind)
        _count(self.attempt_index, "role attempt index", positive=True)
        _text(self.model, "role slot model")
        _count(self.max_output_tokens, "role slot output tokens", positive=True)

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class AuthorizedRoleSlotV5:
    slot_id: str
    request: RoleSlotRequestV5
    authorization_sha256: str
    prior_external_attempts: int
    prior_total_tokens: int
    prior_cost_usd: Decimal
    prior_terminal_sequence: int

    def __post_init__(self) -> None:
        _text(self.slot_id, "authorized role slot ID")
        if type(self.request) is not RoleSlotRequestV5:
            raise ValueError("authorized role slot request is invalid")
        _digest(self.authorization_sha256, "authorized role slot")
        _count(self.prior_external_attempts, "role slot prior attempts")
        _count(self.prior_total_tokens, "role slot prior tokens")
        if type(self.prior_cost_usd) is not Decimal or not self.prior_cost_usd.is_finite() or self.prior_cost_usd < 0:
            raise ValueError("role slot prior cost is invalid")
        _count(self.prior_terminal_sequence, "role slot prior terminal sequence")


@dataclass(frozen=True, slots=True)
class RoleUsageFactsV5:
    external_attempt_count: int
    request_started: bool
    response_received: bool
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal
    requested_model: str | None
    returned_model: str | None
    provider_request_id: str | None

    def __post_init__(self) -> None:
        if type(self.external_attempt_count) is not int or self.external_attempt_count not in {0, 1}:
            raise ValueError("role usage external-call count is invalid")
        if type(self.request_started) is not bool or type(self.response_received) is not bool:
            raise ValueError("role usage request state is invalid")
        if self.response_received and not self.request_started:
            raise ValueError("role usage cannot receive a response before a request")
        _count(self.input_tokens, "role usage input tokens")
        _count(self.output_tokens, "role usage output tokens")
        _count(self.total_tokens, "role usage total tokens")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("role usage total tokens differ from input plus output")
        if type(self.cost_usd) is not Decimal or not self.cost_usd.is_finite() or self.cost_usd < 0:
            raise ValueError("role usage cost is invalid")
        if self.requested_model is not None:
            _text(self.requested_model, "role usage requested model")
        if self.returned_model is not None:
            _text(self.returned_model, "role usage returned model")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "role usage provider request ID")
        if self.external_attempt_count == 0:
            if (
                self.request_started
                or self.response_received
                or self.requested_model is not None
                or self.returned_model is not None
                or self.provider_request_id is not None
                or (self.input_tokens, self.output_tokens, self.total_tokens) != (0, 0, 0)
                or self.cost_usd != Decimal("0")
            ):
                raise ValueError("provider-free usage must be the exact zero-usage shape")
        elif not self.request_started or self.requested_model is None or self.returned_model is None:
            raise ValueError("external role usage must identify a started provider request")


@dataclass(frozen=True, slots=True)
class RoleAttemptFactsV5:
    role: RoleNameV5
    attempt_kind: RoleAttemptKindV5
    attempt_index: int
    request_sha256: str
    slot_id: str | None
    outcome: RoleOutcomeV5
    failure_code: RoleFailureCode | None
    usage: RoleUsageFactsV5
    response_sha256: str | None
    artifact_sha256: str | None

    def __post_init__(self) -> None:
        _role(self.role)
        _attempt_kind(self.attempt_kind)
        _count(self.attempt_index, "role attempt facts index", positive=True)
        _digest(self.request_sha256, "role attempt request")
        if self.slot_id is not None:
            _text(self.slot_id, "role attempt slot ID")
        if self.outcome not in {
            "accepted",
            "transport_failure",
            "response_schema_failure",
            "evidence_binding_failure",
            "authorization_failure",
            "accounting_failure",
        }:
            raise ValueError("role attempt outcome is invalid")
        expected_code = {
            "accepted": None,
            "transport_failure": RoleFailureCode.TRANSPORT,
            "response_schema_failure": RoleFailureCode.RESPONSE_SCHEMA,
            "evidence_binding_failure": RoleFailureCode.EVIDENCE_BINDING,
            "authorization_failure": RoleFailureCode.AUTHORIZATION,
            "accounting_failure": RoleFailureCode.ACCOUNTING,
        }[self.outcome]
        if self.failure_code is not expected_code:
            raise ValueError("role attempt outcome differs from its failure code")
        if type(self.usage) is not RoleUsageFactsV5:
            raise ValueError("role attempt usage is invalid")
        if self.response_sha256 is not None:
            _digest(self.response_sha256, "role response")
        if self.artifact_sha256 is not None:
            _digest(self.artifact_sha256, "role artifact")
        if self.outcome == "accepted":
            if self.response_sha256 is None or self.artifact_sha256 is None:
                raise ValueError("accepted role attempt requires response and artifact identities")
        elif self.artifact_sha256 is not None:
            raise ValueError("failed role attempt cannot carry an accepted artifact identity")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class RoleTerminalReceiptV5:
    """Authenticated lifecycle projection for one durable terminal settlement."""

    slot_id: str
    slot_request_sha256: str
    authorization_sha256: str
    attempt_facts_sha256: str
    cumulative_external_attempts: int
    cumulative_total_tokens: int
    cumulative_cost_usd: Decimal
    terminal_sequence: int
    receipt_sha256: str

    def __post_init__(self) -> None:
        _text(self.slot_id, "role terminal receipt slot ID")
        _digest(self.slot_request_sha256, "role terminal receipt slot request")
        _digest(self.authorization_sha256, "role terminal receipt authorization")
        _digest(self.attempt_facts_sha256, "role terminal receipt facts")
        _count(self.cumulative_external_attempts, "role receipt cumulative attempts")
        _count(self.cumulative_total_tokens, "role receipt cumulative tokens")
        if (
            type(self.cumulative_cost_usd) is not Decimal
            or not self.cumulative_cost_usd.is_finite()
            or self.cumulative_cost_usd < 0
        ):
            raise ValueError("role receipt cumulative cost is invalid")
        _count(self.terminal_sequence, "role receipt terminal sequence", positive=True)
        _digest(self.receipt_sha256, "role terminal receipt")
        if self.receipt_sha256 != canonical_sha256_v5(self.authenticated_payload()):
            raise ValueError("role terminal receipt digest differs from its payload")

    def authenticated_payload(self) -> dict[str, object]:
        """Return the fields that a ledger implementation must sign/authenticate."""

        return {
            "slot_id": self.slot_id,
            "slot_request_sha256": self.slot_request_sha256,
            "authorization_sha256": self.authorization_sha256,
            "attempt_facts_sha256": self.attempt_facts_sha256,
            "cumulative_external_attempts": self.cumulative_external_attempts,
            "cumulative_total_tokens": self.cumulative_total_tokens,
            "cumulative_cost_usd": self.cumulative_cost_usd,
            "terminal_sequence": self.terminal_sequence,
        }


@dataclass(frozen=True, slots=True)
class RecoveredRoleTerminalV5:
    facts: RoleAttemptFactsV5
    receipt: RoleTerminalReceiptV5

    def __post_init__(self) -> None:
        if type(self.facts) is not RoleAttemptFactsV5 or type(self.receipt) is not RoleTerminalReceiptV5:
            raise ValueError("recovered role terminal package is invalid")
        if self.receipt.attempt_facts_sha256 != self.facts.sha256:
            raise ValueError("recovered role terminal facts differ from their receipt")


@runtime_checkable
class RoleAuthorizationLifecycleV5(Protocol):
    """Opaque controller-owned reserve/settle lifecycle for authorized provider slots."""

    def reserve_role_slot(self, request: RoleSlotRequestV5) -> AuthorizedRoleSlotV5: ...

    def verify_role_slot(self, slot: AuthorizedRoleSlotV5) -> None: ...

    def settle_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
    ) -> RoleTerminalReceiptV5: ...

    def settle_unreported_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
        failure_code: RoleFailureCode,
    ) -> RecoveredRoleTerminalV5: ...

    def recover_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
    ) -> RecoveredRoleTerminalV5 | None: ...

    def verify_role_slot_receipt(
        self,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        receipt: RoleTerminalReceiptV5,
    ) -> None: ...


@runtime_checkable
class RoleRunnerV5(Protocol):
    def invoke_once(self, request: RoleRequestV5) -> ParsedRoleArtifactV5: ...


def _zero_usage_facts() -> RoleUsageFactsV5:
    return RoleUsageFactsV5(
        external_attempt_count=0,
        request_started=False,
        response_received=False,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=Decimal("0"),
        requested_model=None,
        returned_model=None,
        provider_request_id=None,
    )


def _live_usage_facts(*, model: str, result: CompletionResultV5) -> RoleUsageFactsV5:
    return RoleUsageFactsV5(
        external_attempt_count=result.external_attempt_count,
        request_started=True,
        response_received=result.response_received,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.input_tokens + result.output_tokens,
        cost_usd=result.cost_usd,
        requested_model=model,
        returned_model=result.returned_model,
        provider_request_id=result.provider_request_id,
    )


def _failure_outcome(code: RoleFailureCode) -> RoleOutcomeV5:
    return {
        RoleFailureCode.TRANSPORT: "transport_failure",
        RoleFailureCode.RESPONSE_SCHEMA: "response_schema_failure",
        RoleFailureCode.EVIDENCE_BINDING: "evidence_binding_failure",
        RoleFailureCode.AUTHORIZATION: "authorization_failure",
        RoleFailureCode.ACCOUNTING: "accounting_failure",
    }[code]  # type: ignore[return-value]


def _failure_with_attempt(*, role: RoleNameV5, code: RoleFailureCode, attempt: RoleAttemptFactsV5) -> RoleFailureV5:
    failure_types: Mapping[RoleFailureCode, type[RoleFailureV5]] = {
        RoleFailureCode.TRANSPORT: RoleTransportFailureV5,
        RoleFailureCode.RESPONSE_SCHEMA: RoleResponseSchemaFailureV5,
        RoleFailureCode.EVIDENCE_BINDING: RoleEvidenceBindingFailureV5,
        RoleFailureCode.AUTHORIZATION: RoleAuthorizationFailureV5,
        RoleFailureCode.ACCOUNTING: RoleAccountingFailureV5,
    }
    return failure_types[code](role=role, attempt=attempt)


def _canonical_response_sha256(response_text: str) -> str | None:
    try:
        decoded = _duplicate_rejecting_json(response_text, require_canonical=False)
    except ValueError:
        return None
    return hashlib.sha256(canonical_json_bytes_v5(decoded)).hexdigest()


class AuthorizedRoleRunnerV5:
    """Execute one authorized call and publish only ledger-verified terminal facts."""

    __slots__ = (
        "_attempts",
        "_blocked",
        "_capabilities",
        "_kinds_used",
        "_lifecycle",
        "_provider",
        "_receipts",
        "_slot_ids",
    )

    def __init__(
        self,
        *,
        capabilities: ProviderCapabilitiesV5,
        provider: CompletionProvider,
        lifecycle: RoleAuthorizationLifecycleV5,
    ) -> None:
        if type(capabilities) is not ProviderCapabilitiesV5:
            raise ValueError("authorized role runner capabilities are invalid")
        try:
            provider_valid = isinstance(provider, CompletionProvider)
            lifecycle_valid = isinstance(lifecycle, RoleAuthorizationLifecycleV5)
        except BaseException:
            provider_valid = False
            lifecycle_valid = False
        if not provider_valid or not lifecycle_valid:
            raise ValueError("authorized role runner boundary is invalid")
        self._capabilities = capabilities
        self._provider = provider
        self._lifecycle = lifecycle
        self._attempts: list[RoleAttemptFactsV5] = []
        self._receipts: list[RoleTerminalReceiptV5] = []
        self._kinds_used = {"primary": 0, "retry": 0, "repair": 0}
        self._slot_ids: set[str] = set()
        self._blocked = False

    @property
    def attempts(self) -> tuple[RoleAttemptFactsV5, ...]:
        return tuple(self._attempts)

    @property
    def receipts(self) -> tuple[RoleTerminalReceiptV5, ...]:
        return tuple(self._receipts)

    def _attempt_facts(
        self,
        *,
        request: RoleRequestV5,
        attempt_kind: RoleAttemptKindV5,
        attempt_index: int,
        slot_id: str | None,
        code: RoleFailureCode | None,
        usage: RoleUsageFactsV5,
        response_text: str | None = None,
        artifact: ParsedRoleArtifactV5 | None = None,
    ) -> RoleAttemptFactsV5:
        return RoleAttemptFactsV5(
            role=request.role,
            attempt_kind=attempt_kind,
            attempt_index=attempt_index,
            request_sha256=request.sha256,
            slot_id=slot_id,
            outcome="accepted" if code is None else _failure_outcome(code),
            failure_code=code,
            usage=usage,
            response_sha256=(None if response_text is None else _canonical_response_sha256(response_text)),
            artifact_sha256=None if artifact is None else canonical_sha256_v5(artifact),
        )

    def _fail_before_reservation(
        self,
        *,
        request: RoleRequestV5,
        attempt_kind: RoleAttemptKindV5,
        attempt_index: int,
    ) -> ParsedRoleArtifactV5:
        facts = self._attempt_facts(
            request=request,
            attempt_kind=attempt_kind,
            attempt_index=attempt_index,
            slot_id=None,
            code=RoleFailureCode.AUTHORIZATION,
            usage=_zero_usage_facts(),
        )
        self._attempts.append(facts)
        raise RoleAuthorizationFailureV5(role=request.role, attempt=facts)

    def _validate_receipt(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        receipt: RoleTerminalReceiptV5,
        allow_stale_slot_prior: bool = False,
    ) -> None:
        prior_attempts = slot.prior_external_attempts
        prior_tokens = slot.prior_total_tokens
        prior_cost = slot.prior_cost_usd
        prior_sequence = slot.prior_terminal_sequence
        if self._receipts and allow_stale_slot_prior:
            previous = self._receipts[-1]
            prior_attempts = previous.cumulative_external_attempts
            prior_tokens = previous.cumulative_total_tokens
            prior_cost = previous.cumulative_cost_usd
            prior_sequence = previous.terminal_sequence
        if (
            type(receipt) is not RoleTerminalReceiptV5
            or receipt.slot_id != slot.slot_id
            or receipt.slot_request_sha256 != slot.request.sha256
            or receipt.authorization_sha256 != slot.authorization_sha256
            or receipt.attempt_facts_sha256 != facts.sha256
            or receipt.cumulative_external_attempts != prior_attempts + facts.usage.external_attempt_count
            or receipt.cumulative_total_tokens != prior_tokens + facts.usage.total_tokens
            or receipt.cumulative_cost_usd != prior_cost + facts.usage.cost_usd
            or receipt.terminal_sequence != prior_sequence + 1
        ):
            raise ValueError("role terminal receipt differs from its slot and facts")
        if self._receipts and not allow_stale_slot_prior:
            previous = self._receipts[-1]
            if (
                slot.prior_external_attempts != previous.cumulative_external_attempts
                or slot.prior_total_tokens != previous.cumulative_total_tokens
                or slot.prior_cost_usd != previous.cumulative_cost_usd
                or slot.prior_terminal_sequence != previous.terminal_sequence
            ):
                raise ValueError("role lifecycle receipt chain is discontinuous")
        capabilities = self._capabilities
        if (
            receipt.cumulative_external_attempts > capabilities.maximum_role_calls
            or receipt.cumulative_total_tokens > capabilities.maximum_total_tokens
            or (capabilities.maximum_usd is not None and receipt.cumulative_cost_usd > capabilities.maximum_usd)
        ) and facts.failure_code is not RoleFailureCode.ACCOUNTING:
            raise ValueError("role terminal receipt overage lacks an accounting failure")
        self._lifecycle.verify_role_slot_receipt(slot, facts, receipt)

    def _recover_verified(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        expected_facts: RoleAttemptFactsV5 | None,
        allow_stale_slot_prior: bool = False,
    ) -> RecoveredRoleTerminalV5 | None:
        try:
            recovered = self._lifecycle.recover_role_slot(slot)
            self._validate_recovered(
                slot=slot,
                recovered=recovered,
                expected_facts=expected_facts,
                allow_stale_slot_prior=allow_stale_slot_prior,
            )
        except BaseException:
            return None
        assert isinstance(recovered, RecoveredRoleTerminalV5)
        return recovered

    def _validate_recovered(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        recovered: object,
        expected_facts: RoleAttemptFactsV5 | None,
        allow_stale_slot_prior: bool = False,
    ) -> None:
        if type(recovered) is not RecoveredRoleTerminalV5 or (
            expected_facts is not None and recovered.facts != expected_facts
        ):
            raise ValueError("recovered role terminal package differs")
        self._validate_receipt(
            slot=slot,
            facts=recovered.facts,
            receipt=recovered.receipt,
            allow_stale_slot_prior=allow_stale_slot_prior,
        )

    def _accounting_failure(
        self,
        *,
        request: RoleRequestV5,
        attempt_kind: RoleAttemptKindV5,
        attempt_index: int,
        slot_id: str | None,
        usage: RoleUsageFactsV5 | None = None,
        response_sha256: str | None = None,
    ) -> ParsedRoleArtifactV5:
        facts = RoleAttemptFactsV5(
            role=request.role,
            attempt_kind=attempt_kind,
            attempt_index=attempt_index,
            request_sha256=request.sha256,
            slot_id=slot_id,
            outcome="accounting_failure",
            failure_code=RoleFailureCode.ACCOUNTING,
            usage=_zero_usage_facts() if usage is None else usage,
            response_sha256=response_sha256,
            artifact_sha256=None,
        )
        self._attempts.append(facts)
        self._blocked = True
        raise RoleAccountingFailureV5(role=request.role, attempt=facts)

    def _settle_once(
        self,
        *,
        request: RoleRequestV5,
        attempt_kind: RoleAttemptKindV5,
        attempt_index: int,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        allow_stale_slot_prior: bool = False,
    ) -> RoleTerminalReceiptV5:
        receipt: RoleTerminalReceiptV5 | None
        try:
            receipt = self._lifecycle.settle_role_slot(slot, facts)
            self._validate_receipt(
                slot=slot,
                facts=facts,
                receipt=receipt,
                allow_stale_slot_prior=allow_stale_slot_prior,
            )
        except BaseException:
            recovered = self._recover_verified(
                slot=slot,
                expected_facts=facts,
                allow_stale_slot_prior=allow_stale_slot_prior,
            )
            if recovered is None:
                return self._accounting_failure(
                    request=request,
                    attempt_kind=attempt_kind,
                    attempt_index=attempt_index,
                    slot_id=slot.slot_id,
                    usage=facts.usage,
                    response_sha256=facts.response_sha256,
                )
            receipt = recovered.receipt
        self._attempts.append(facts)
        self._receipts.append(receipt)
        return receipt

    def _raise_recovered_failure(
        self,
        *,
        request: RoleRequestV5,
        attempt_kind: RoleAttemptKindV5,
        attempt_index: int,
        slot: AuthorizedRoleSlotV5,
        failure_code: RoleFailureCode,
    ) -> ParsedRoleArtifactV5:
        try:
            recovered = self._lifecycle.settle_unreported_role_slot(
                slot,
                failure_code,
            )
            self._validate_recovered(
                slot=slot,
                recovered=recovered,
                expected_facts=None,
            )
        except BaseException:
            recovered = self._recover_verified(slot=slot, expected_facts=None)
        if (
            recovered is None
            or recovered.facts.role != request.role
            or recovered.facts.attempt_kind != attempt_kind
            or recovered.facts.attempt_index != attempt_index
            or recovered.facts.request_sha256 != request.sha256
            or recovered.facts.slot_id != slot.slot_id
            or recovered.facts.failure_code not in {failure_code, RoleFailureCode.ACCOUNTING}
        ):
            return self._accounting_failure(
                request=request,
                attempt_kind=attempt_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
            )
        self._attempts.append(recovered.facts)
        self._receipts.append(recovered.receipt)
        raise _failure_with_attempt(
            role=request.role,
            code=recovered.facts.failure_code,
            attempt=recovered.facts,
        )

    def invoke_once(
        self,
        request: RoleRequestV5,
        *,
        attempt_kind: RoleAttemptKindV5 = "primary",
    ) -> ParsedRoleArtifactV5:
        if type(request) is not RoleRequestV5:
            raise ValueError("authorized runner requires a V5 role request")
        canonical_kind = _attempt_kind(attempt_kind)
        attempt_index = len(self._attempts) + 1
        capabilities = self._capabilities
        kind_limit = {
            "primary": capabilities.maximum_role_calls,
            "retry": capabilities.automatic_retries,
            "repair": capabilities.schema_repair_calls,
        }[canonical_kind]
        if (
            self._blocked
            or request.max_output_tokens != capabilities.maximum_output_tokens_per_role
            or self._kinds_used[canonical_kind] >= kind_limit
        ):
            return self._fail_before_reservation(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
            )
        slot_request = RoleSlotRequestV5(
            request_sha256=request.sha256,
            role=request.role,
            attempt_kind=canonical_kind,
            attempt_index=attempt_index,
            model=capabilities.model,
            max_output_tokens=capabilities.maximum_output_tokens_per_role,
        )
        try:
            slot = self._lifecycle.reserve_role_slot(slot_request)
        except BaseException:
            return self._fail_before_reservation(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
            )
        if type(slot) is not AuthorizedRoleSlotV5:
            return self._accounting_failure(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=None,
            )
        malformed_slot = slot.request != slot_request or slot.slot_id in self._slot_ids
        try:
            self._lifecycle.verify_role_slot(slot)
        except BaseException:
            malformed_slot = True
        self._slot_ids.add(slot.slot_id)
        if malformed_slot:
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=RoleFailureCode.AUTHORIZATION,
                usage=_zero_usage_facts(),
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
                allow_stale_slot_prior=True,
            )
            raise RoleAuthorizationFailureV5(role=request.role, attempt=facts)
        exhausted_slot = (
            slot.prior_external_attempts >= capabilities.maximum_role_calls
            or slot.prior_total_tokens >= capabilities.maximum_total_tokens
            or (capabilities.maximum_usd is not None and slot.prior_cost_usd >= capabilities.maximum_usd)
        )
        if exhausted_slot:
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=RoleFailureCode.ACCOUNTING,
                usage=_zero_usage_facts(),
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
            )
            self._blocked = True
            raise RoleAccountingFailureV5(role=request.role, attempt=facts)
        self._kinds_used[canonical_kind] += 1
        completion_request = ProviderCompletionRequestV5(
            role_request=request,
            model=capabilities.model,
        )
        try:
            result = self._provider.complete_once(completion_request)
        except BaseException:
            return self._raise_recovered_failure(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                failure_code=RoleFailureCode.TRANSPORT,
            )
        if type(result) is not CompletionResultV5:
            return self._raise_recovered_failure(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                failure_code=RoleFailureCode.ACCOUNTING,
            )
        usage = _live_usage_facts(model=capabilities.model, result=result)
        exceeds_cumulative_cap = (
            slot.prior_external_attempts + usage.external_attempt_count > capabilities.maximum_role_calls
            or slot.prior_total_tokens + usage.total_tokens > capabilities.maximum_total_tokens
            or (
                capabilities.maximum_usd is not None and slot.prior_cost_usd + usage.cost_usd > capabilities.maximum_usd
            )
        )
        if (
            result.returned_model != capabilities.model
            or result.external_attempt_count != 1
            or result.output_tokens > request.max_output_tokens
            or exceeds_cumulative_cap
        ):
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=RoleFailureCode.ACCOUNTING,
                usage=usage,
                response_text=result.response_text,
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
            )
            self._blocked = True
            raise RoleAccountingFailureV5(role=request.role, attempt=facts)
        if not result.accepted:
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=RoleFailureCode.TRANSPORT,
                usage=usage,
                response_text=result.response_text,
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
            )
            raise RoleTransportFailureV5(role=request.role, attempt=facts)
        try:
            artifact = parse_and_bind_role_artifact(
                request=request,
                response_text=result.response_text,
            )
        except RoleFailureV5 as failure:
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=failure.code,
                usage=usage,
                response_text=result.response_text,
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
            )
            raise _failure_with_attempt(role=request.role, code=failure.code, attempt=facts) from None
        except BaseException:
            facts = self._attempt_facts(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot_id=slot.slot_id,
                code=RoleFailureCode.RESPONSE_SCHEMA,
                usage=usage,
                response_text=result.response_text,
            )
            self._settle_once(
                request=request,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                slot=slot,
                facts=facts,
            )
            raise RoleResponseSchemaFailureV5(role=request.role, attempt=facts) from None
        facts = self._attempt_facts(
            request=request,
            attempt_kind=canonical_kind,
            attempt_index=attempt_index,
            slot_id=slot.slot_id,
            code=None,
            usage=usage,
            response_text=result.response_text,
            artifact=artifact,
        )
        self._settle_once(
            request=request,
            attempt_kind=canonical_kind,
            attempt_index=attempt_index,
            slot=slot,
            facts=facts,
        )
        return artifact


class FixtureRoleRunnerV5:
    """Provider-free role runner using explicitly declared canonical fixture slots."""

    __slots__ = ("_attempts", "_positions", "_responses")

    def __init__(
        self,
        *,
        responses: Mapping[
            RoleNameV5 | tuple[RoleNameV5, RoleAttemptKindV5],
            tuple[str, ...],
        ],
    ) -> None:
        if not isinstance(responses, Mapping):
            raise ValueError("fixture role responses are invalid")
        normalized: dict[tuple[RoleNameV5, RoleAttemptKindV5], tuple[str, ...]] = {}
        for key, values in responses.items():
            if type(key) is str:
                identity = (_role(key), "primary")
            elif type(key) is tuple and len(key) == 2:
                identity = (_role(key[0]), _attempt_kind(key[1]))
            else:
                raise ValueError("fixture role slot key is invalid")
            if identity in normalized or type(values) is not tuple or any(type(item) is not str for item in values):
                raise ValueError("fixture role response slots are invalid")
            normalized[identity] = values
        self._responses = MappingProxyType(normalized)
        self._positions = {identity: 0 for identity in normalized}
        self._attempts: list[RoleAttemptFactsV5] = []

    @property
    def attempts(self) -> tuple[RoleAttemptFactsV5, ...]:
        return tuple(self._attempts)

    def invoke_once(
        self,
        request: RoleRequestV5,
        *,
        attempt_kind: RoleAttemptKindV5 = "primary",
    ) -> ParsedRoleArtifactV5:
        if type(request) is not RoleRequestV5:
            raise ValueError("fixture runner requires a V5 role request")
        canonical_kind = _attempt_kind(attempt_kind)
        attempt_index = len(self._attempts) + 1
        identity = (request.role, canonical_kind)
        position = self._positions.get(identity, 0)
        declared = self._responses.get(identity, ())
        usage = _zero_usage_facts()
        if position >= len(declared):
            facts = RoleAttemptFactsV5(
                role=request.role,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                request_sha256=request.sha256,
                slot_id=None,
                outcome="authorization_failure",
                failure_code=RoleFailureCode.AUTHORIZATION,
                usage=usage,
                response_sha256=None,
                artifact_sha256=None,
            )
            self._attempts.append(facts)
            raise RoleAuthorizationFailureV5(role=request.role, attempt=facts)
        self._positions[identity] = position + 1
        response_text = declared[position]
        response_sha256 = _canonical_response_sha256(response_text)
        slot_id = f"fixture.{request.role}.{canonical_kind}.{position + 1}"
        try:
            artifact = parse_and_bind_role_artifact(
                request=request,
                response_text=response_text,
            )
        except RoleFailureV5 as failure:
            facts = RoleAttemptFactsV5(
                role=request.role,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                request_sha256=request.sha256,
                slot_id=slot_id,
                outcome=_failure_outcome(failure.code),
                failure_code=failure.code,
                usage=usage,
                response_sha256=response_sha256,
                artifact_sha256=None,
            )
            self._attempts.append(facts)
            raise _failure_with_attempt(role=request.role, code=failure.code, attempt=facts) from None
        except BaseException:
            facts = RoleAttemptFactsV5(
                role=request.role,
                attempt_kind=canonical_kind,
                attempt_index=attempt_index,
                request_sha256=request.sha256,
                slot_id=slot_id,
                outcome="response_schema_failure",
                failure_code=RoleFailureCode.RESPONSE_SCHEMA,
                usage=usage,
                response_sha256=response_sha256,
                artifact_sha256=None,
            )
            self._attempts.append(facts)
            raise RoleResponseSchemaFailureV5(role=request.role, attempt=facts) from None
        facts = RoleAttemptFactsV5(
            role=request.role,
            attempt_kind=canonical_kind,
            attempt_index=attempt_index,
            request_sha256=request.sha256,
            slot_id=slot_id,
            outcome="accepted",
            failure_code=None,
            usage=usage,
            response_sha256=response_sha256,
            artifact_sha256=canonical_sha256_v5(artifact),
        )
        self._attempts.append(facts)
        return artifact


__all__ = [
    "AuthorizedRoleRunnerV5",
    "AuthorizedRoleSlotV5",
    "CompletionProvider",
    "CompletionResultV5",
    "FixtureRoleRunnerV5",
    "GatewayCompletionProviderV5",
    "IssuedEvidenceV5",
    "OneShotJsonCompletionV5",
    "ParsedRoleArtifactV5",
    "ProviderCompletionRequestV5",
    "RecoveredRoleTerminalV5",
    "RoleAccountingFailureV5",
    "RoleAttemptFactsV5",
    "RoleAttemptKindV5",
    "RoleAuthorizationFailureV5",
    "RoleAuthorizationLifecycleV5",
    "RoleBindingV5",
    "RoleEvidenceBindingFailureV5",
    "RoleFailureCode",
    "RoleFailureV5",
    "RoleNameV5",
    "RoleOutcomeV5",
    "RoleRequestV5",
    "RoleResponseSchemaFailureV5",
    "RoleRunnerV5",
    "RoleSchemaAuthorityV5",
    "RoleSlotRequestV5",
    "RoleTerminalReceiptV5",
    "RoleTransportFailureV5",
    "RoleUsageFactsV5",
    "build_role_request_v5",
    "parse_and_bind_role_artifact",
    "role_schema_authority_from_manifest_v5",
]
