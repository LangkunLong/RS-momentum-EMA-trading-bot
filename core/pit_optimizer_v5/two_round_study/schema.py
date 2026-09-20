"""Strict opt-in wire schema and parser for study investigator responses."""

from __future__ import annotations

import copy
import json
import re
from typing import Mapping

from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5
from core.pit_optimizer_v5.provider import (
    RoleRequestV5,
    parse_and_bind_role_artifact,
)

from .contracts import (
    STUDY_RIVAL_NAMES_V1,
    StudyAuthorityError,
    StudyContractError,
    StudyResponseV1,
)


_STUDY_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_STUDY_DECIMAL_PATTERN = (
    r"^(?!-0(?:\.0*)?$)-?(?=(?:[^0-9]*[0-9]){1,64}[^0-9]*$)"
    r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$"
)
_STUDY_EVIDENCE_PATTERN = r"^v5\.[a-z0-9_.-]{1,120}$"


def _strict_tuple_text(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value or any(
        type(item) is not str
        or not item
        or item != item.strip()
        or re.fullmatch(_STUDY_IDENTIFIER_PATTERN[1:-1], item) is None
        for item in value
    ):
        raise StudyContractError(f"{label} is invalid")
    if len(set(value)) != len(value):
        raise StudyContractError(f"{label} must be unique")
    return value


def _schema_object(properties: Mapping[str, object], required: tuple[str, ...]) -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
        "type": "object",
    }


def _decimal_text_schema(*, nullable: bool = False) -> dict[str, object]:
    return {
        "maxLength": 96,
        "pattern": _STUDY_DECIMAL_PATTERN,
        "type": ["string", "null"] if nullable else "string",
    }


def _draft_schema(configuration_ids: tuple[str, ...]) -> dict[str, object]:
    predicate = _schema_object(
        {
            "field": {"const": "features.atr_20_fraction"},
            "operator": {
                "enum": ["is_present", "is_missing", "gt", "gte", "lt", "lte", "eq"],
                "type": "string",
            },
            "value": _decimal_text_schema(nullable=True),
        },
        ("field", "operator", "value"),
    )
    recipe = _schema_object(
        {
            "recipe_id": {"const": "evaluate_exit_atr20_fraction_v1"},
            "input_field": {"const": "features.atr_20_fraction"},
            "input_values": {
                "items": _decimal_text_schema(nullable=True),
                "maxItems": 32,
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "version": {"enum": [1], "type": "integer"},
        },
        ("recipe_id", "input_field", "input_values", "version"),
    )
    metric = _schema_object(
        {
            "metric_id": {
                "enum": [
                    "exit.decision_changed_count",
                    "exit.protected_control_unchanged_count",
                ],
                "type": "string",
            },
            "unit": {"const": "count"},
            "direction": {"enum": ["increase", "decrease", "unchanged"], "type": "string"},
            "tolerance": _decimal_text_schema(),
            "denominator": {
                "enum": ["relevant_cases", "control_cases", "all_cases"],
                "type": "string",
            },
            "selector": {"type": "null"},
        },
        ("metric_id", "unit", "direction", "tolerance", "denominator", "selector"),
    )
    observation = _schema_object(
        {
            "observation_id": {
                "enum": ["protected_control_changed", "decision_unchanged_when_applicable"],
                "type": "string",
            },
            "metric_id": {
                "enum": [
                    "exit.decision_changed_count",
                    "exit.protected_control_unchanged_count",
                ],
                "type": "string",
            },
        },
        ("observation_id", "metric_id"),
    )
    rival = _schema_object(
        {
            "name": {"enum": list(STUDY_RIVAL_NAMES_V1), "type": "string"},
            "pattern": {
                "items": {"type": "boolean"},
                "maxItems": 64,
                "minItems": 1,
                "type": "array",
            },
        },
        ("name", "pattern"),
    )
    return _schema_object(
        {
            "hypothesis_id": {
                "maxLength": 128,
                "pattern": _STUDY_IDENTIFIER_PATTERN,
                "type": "string",
            },
            "cited_evidence_ids": {
                "items": {"pattern": _STUDY_EVIDENCE_PATTERN, "type": "string"},
                "maxItems": 256,
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "applicability": predicate,
            "recipe": recipe,
            "metrics": {
                "items": metric,
                "maxItems": 16,
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "disconfirming_observations": {
                "items": observation,
                "maxItems": 16,
                "type": "array",
                "uniqueItems": True,
            },
            "expected_changed": {
                "items": {"type": "boolean"},
                "maxItems": 64,
                "minItems": 1,
                "type": "array",
            },
            "rivals": {
                "items": rival,
                "maxItems": 16,
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "configuration_id": {
                "enum": list(configuration_ids),
                "pattern": _STUDY_IDENTIFIER_PATTERN,
                "type": "string",
            },
            "claim_kind": {"enum": ["threshold", "general"], "type": "string"},
        },
        (
            "hypothesis_id",
            "cited_evidence_ids",
            "applicability",
            "recipe",
            "metrics",
            "disconfirming_observations",
            "expected_changed",
            "rivals",
            "configuration_id",
            "claim_kind",
        ),
    )


def study_response_schema_v1(*, fixture_request: RoleRequestV5, configuration_ids: tuple[str, ...]) -> bytes:
    """Build the canonical study schema bound to one ordinary fixture request."""

    if type(fixture_request) is not RoleRequestV5 or fixture_request.role != "investigator":
        raise StudyContractError("study response schema requires an investigator request")
    ids = _strict_tuple_text(configuration_ids, "study configuration IDs")
    try:
        ordinary = json.loads(fixture_request.schema_authority.canonical_schema_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyAuthorityError("ordinary investigator schema is invalid") from exc
    if type(ordinary) is not dict or type(ordinary.get("properties")) is not dict:
        raise StudyAuthorityError("ordinary investigator schema is invalid")
    properties = ordinary["properties"]
    if set(properties) != {"artifact", "binding"}:
        raise StudyAuthorityError("ordinary investigator schema has unexpected fields")
    binding_schema = properties["binding"]
    if type(binding_schema) is not dict:
        raise StudyAuthorityError("ordinary investigator binding schema is invalid")
    count = fixture_request.schema_authority.hypotheses_per_investigator
    artifact_schema = copy.deepcopy(properties["artifact"])
    # Investigator requests are never authorized to select full-source
    # authoring.  Keep this restriction local to the opt-in study schema; the
    # legacy schema bytes remain untouched.
    try:
        mode_schema = artifact_schema["properties"]["hypotheses"]["items"]["properties"]["authoring_mode"]
        if fixture_request.schema_authority.allow_full_source_escape is False:
            mode_schema["enum"] = ["symbol_edits"]
    except (KeyError, TypeError) as exc:
        raise StudyAuthorityError("ordinary investigator schema lacks authoring mode authority") from exc
    envelope = _schema_object(
        {
            "schema_version": {"enum": [1], "type": "integer"},
            "binding": copy.deepcopy(binding_schema),
            "artifact": artifact_schema,
            "drafts": {
                "items": _draft_schema(ids),
                "maxItems": count,
                "minItems": count,
                "type": "array",
            },
        },
        ("schema_version", "binding", "artifact", "drafts"),
    )
    envelope["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return canonical_json_bytes_v5(envelope)


def parse_study_response_v1(
    *,
    raw: bytes,
    fixture_request: RoleRequestV5,
    configuration_ids: tuple[str, ...],
) -> StudyResponseV1:
    """Parse a study envelope and losslessly bind its ordinary projection."""

    if type(raw) is not bytes:
        raise StudyContractError("study response must be immutable bytes")
    if type(fixture_request) is not RoleRequestV5 or fixture_request.role != "investigator":
        raise StudyContractError("study response parser requires an investigator request")
    ids = _strict_tuple_text(configuration_ids, "study configuration IDs")
    # Validate against the closed schema before projecting away study-only
    # fields.  The dataclass decoder provides the precise strict validation for
    # every nested value and is intentionally independent of prose.
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StudyContractError("study response JSON is invalid") from exc
    try:
        response = StudyResponseV1.from_primitive(decoded)
    except StudyContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise StudyContractError("study response does not satisfy its closed schema") from exc
    if response.binding != fixture_request.expected_binding:
        raise StudyAuthorityError("study response binding differs from the fixture request")
    if len(response.artifact.hypotheses) != fixture_request.schema_authority.hypotheses_per_investigator:
        raise StudyContractError("study response hypothesis count differs from the fixture schema")
    if any(item.configuration_id not in ids for item in response.drafts):
        raise StudyContractError("study draft configuration is not registered")
    if any(item.authoring_mode != "symbol_edits" for item in response.artifact.hypotheses):
        raise StudyContractError("study investigator authoring mode is unsupported")
    issued_evidence_ids = {item.evidence_id for item in fixture_request.issued_evidence}
    for hypothesis, draft in zip(response.artifact.hypotheses, response.drafts, strict=True):
        cited = set(draft.cited_evidence_ids)
        if not cited.issubset(issued_evidence_ids):
            raise StudyAuthorityError("study draft cites evidence outside the fixture request")
        if not cited.issubset(hypothesis.evidence_ids):
            raise StudyAuthorityError("study draft cites evidence outside its hypothesis")

    try:
        translated = canonical_json_bytes_v5(
            {
                "binding": decoded["binding"],
                "artifact": decoded["artifact"],
            }
        )
        ordinary = parse_and_bind_role_artifact(
            request=fixture_request,
            response_text=translated.decode("utf-8"),
        )
    except Exception as exc:
        # The legacy parser's detailed failure classes are authority failures
        # at the study boundary; preserve the original exception for audit.
        if isinstance(exc, StudyContractError):
            raise
        raise StudyAuthorityError("study ordinary artifact projection failed") from exc
    if ordinary != response.artifact:
        raise StudyAuthorityError("study projection rewrote the ordinary artifact")
    return response


def _reject_duplicate_keys(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise StudyContractError("study response contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise StudyContractError(f"nonfinite JSON constant {value} is not allowed")


__all__ = ["parse_study_response_v1", "study_response_schema_v1"]
