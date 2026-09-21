"""Exact, auditable comparison of the two round-two study requests.

The comparison is deliberately independent of the driver.  It authenticates
the fixture-side F bytes from each descendant and the persisted study-side L
request from the shared store, then normalizes only the Task 6 mechanism input
wrapper.  Every other byte or semantic difference is retained in the result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.provider import (
    MechanismRoleInputV1,
    RoleRequestV5,
    wire_role_messages_v5,
)

from .contracts import StudyAuthorityError, StudyContractError
from .live_calls import (
    FixturePreflightV1,
    StudyCallRequestV1,
    authenticate_fixture_preflight_v1,
)
from .store import StudyStoreV1


ComparisonStatusV1 = Literal["eligible", "inconclusive"]

_COMPARISON_FIELDS = {
    "schema_version",
    "primary_preflight_sha256",
    "withheld_preflight_sha256",
    "primary_fixture_request_sha256",
    "withheld_fixture_request_sha256",
    "primary_live_call_sha256",
    "withheld_live_call_sha256",
    "base_input_equal",
    "same_parent",
    "same_task_wording",
    "same_candidate_domain",
    "same_model_settings",
    "same_schema_shape",
    "same_caps",
    "f_byte_sizes",
    "l_byte_sizes",
    "wire_message_byte_sizes",
    "schema_byte_sizes",
    "projection_counts",
    "row_counts",
    "retained_identity_counts",
    "omitted_identity_counts",
    "usage_bound_counts",
    "evidence_identity_sets",
    "row_identity_sets",
    "row_payload_digests",
    "retained_id_sets",
    "omitted_id_sets",
    "paired_changes",
    "allowed_differences",
    "actual_differences",
    "design_differences",
    "leak_observations",
    "audit_channels",
    "audit_limitations",
    "unresolved_audit_channels",
    "confounds",
    "status",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tuple_int(value: object, label: str) -> tuple[int, int]:
    if type(value) is not list or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
        raise StudyContractError(f"comparison {label} is invalid")
    return int(value[0]), int(value[1])


def _tuple_text(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise StudyContractError(f"comparison {label} is invalid")
    if len(set(value)) != len(value):
        raise StudyContractError(f"comparison {label} must be unique")
    return tuple(value)


def _tuple_text_pair(value: object, label: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(value) is not list or len(value) != 2:
        raise StudyContractError(f"comparison {label} is invalid")
    result: list[tuple[str, ...]] = []
    for item in value:
        if type(item) is not list or any(type(entry) is not str or not entry for entry in item):
            raise StudyContractError(f"comparison {label} is invalid")
        if len(set(item)) != len(item):
            raise StudyContractError(f"comparison {label} must be unique")
        result.append(tuple(item))
    return result[0], result[1]


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise StudyContractError(f"comparison {label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class RequestComparisonV1:
    """The preregistered paired-request comparison and leak audit."""

    schema_version: int
    primary_preflight_sha256: str
    withheld_preflight_sha256: str
    primary_fixture_request_sha256: str
    withheld_fixture_request_sha256: str
    primary_live_call_sha256: str
    withheld_live_call_sha256: str
    base_input_equal: bool
    same_parent: bool
    same_task_wording: bool
    same_candidate_domain: bool
    same_model_settings: bool
    same_schema_shape: bool
    same_caps: bool
    f_byte_sizes: tuple[int, int]
    l_byte_sizes: tuple[int, int]
    wire_message_byte_sizes: tuple[int, int]
    schema_byte_sizes: tuple[int, int]
    projection_counts: tuple[int, int]
    row_counts: tuple[int, int]
    retained_identity_counts: tuple[int, int]
    omitted_identity_counts: tuple[int, int]
    usage_bound_counts: tuple[int, int]
    evidence_identity_sets: tuple[tuple[str, ...], tuple[str, ...]]
    row_identity_sets: tuple[tuple[str, ...], tuple[str, ...]]
    row_payload_digests: tuple[tuple[str, ...], tuple[str, ...]]
    retained_id_sets: tuple[tuple[str, ...], tuple[str, ...]]
    omitted_id_sets: tuple[tuple[str, ...], tuple[str, ...]]
    paired_changes: tuple[str, ...]
    allowed_differences: tuple[str, ...]
    actual_differences: tuple[str, ...]
    design_differences: tuple[str, ...]
    leak_observations: tuple[str, ...]
    audit_channels: tuple[str, ...]
    audit_limitations: tuple[str, ...]
    unresolved_audit_channels: tuple[str, ...]
    confounds: tuple[str, ...]
    status: ComparisonStatusV1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise StudyContractError("comparison schema version is invalid")
        for value, label in (
            (self.primary_preflight_sha256, "primary preflight"),
            (self.withheld_preflight_sha256, "withheld preflight"),
            (self.primary_fixture_request_sha256, "primary fixture request"),
            (self.withheld_fixture_request_sha256, "withheld fixture request"),
            (self.primary_live_call_sha256, "primary live call"),
            (self.withheld_live_call_sha256, "withheld live call"),
        ):
            if type(value) is not str or len(value) != 64 or any(item not in "0123456789abcdef" for item in value):
                raise StudyContractError(f"comparison {label} identity is invalid")
        for value, label in (
            (self.base_input_equal, "base input equality"),
            (self.same_parent, "parent equality"),
            (self.same_task_wording, "task wording equality"),
            (self.same_candidate_domain, "candidate domain equality"),
            (self.same_model_settings, "model settings equality"),
            (self.same_schema_shape, "schema equality"),
            (self.same_caps, "caps equality"),
        ):
            if type(value) is not bool:
                raise StudyContractError(f"comparison {label} is invalid")
        for value, label in (
            (self.f_byte_sizes, "F byte sizes"),
            (self.l_byte_sizes, "L byte sizes"),
            (self.wire_message_byte_sizes, "wire message byte sizes"),
            (self.schema_byte_sizes, "schema byte sizes"),
            (self.projection_counts, "projection counts"),
            (self.row_counts, "row counts"),
            (self.retained_identity_counts, "retained identity counts"),
            (self.omitted_identity_counts, "omitted identity counts"),
            (self.usage_bound_counts, "usage bound counts"),
        ):
            if type(value) is not tuple or len(value) != 2 or any(type(item) is not int or item < 0 for item in value):
                raise StudyContractError(f"comparison {label} is invalid")
        for value, label in (
            (self.evidence_identity_sets, "evidence identity sets"),
            (self.row_identity_sets, "row identity sets"),
            (self.row_payload_digests, "row payload digests"),
            (self.retained_id_sets, "retained identity sets"),
            (self.omitted_id_sets, "omitted identity sets"),
        ):
            if type(value) is not tuple or len(value) != 2 or any(
                type(item) is not tuple or any(type(entry) is not str or not entry for entry in item)
                or len(set(item)) != len(item)
                for item in value
            ):
                raise StudyContractError(f"comparison {label} is invalid")
        for value, label in (
            (self.paired_changes, "paired changes"),
            (self.allowed_differences, "allowed differences"),
            (self.actual_differences, "actual differences"),
            (self.design_differences, "design differences"),
            (self.leak_observations, "leak observations"),
            (self.audit_channels, "audit channels"),
            (self.audit_limitations, "audit limitations"),
            (self.unresolved_audit_channels, "unresolved audit channels"),
            (self.confounds, "confounds"),
        ):
            if type(value) is not tuple or any(type(item) is not str or not item for item in value):
                raise StudyContractError(f"comparison {label} is invalid")
            if len(set(value)) != len(value):
                raise StudyContractError(f"comparison {label} must be unique")
        if self.status not in {"eligible", "inconclusive"}:
            raise StudyContractError("comparison status is invalid")
        if self.primary_fixture_request_sha256 == self.withheld_fixture_request_sha256:
            raise StudyAuthorityError("paired fixture requests unexpectedly share an identity")
        if self.primary_live_call_sha256 == self.withheld_live_call_sha256:
            raise StudyAuthorityError("paired live requests unexpectedly share an identity")
        if self.status == "inconclusive" and not (self.leak_observations or self.confounds):
            raise StudyAuthorityError("inconclusive comparison lacks a recorded reason")
        if self.status == "eligible" and (
            not all(
                (
                    self.base_input_equal,
                    self.same_parent,
                    self.same_task_wording,
                    self.same_candidate_domain,
                    self.same_model_settings,
                    self.same_schema_shape,
                    self.same_caps,
                )
            )
            or self.leak_observations
            or self.confounds
        ):
            raise StudyAuthorityError("eligible comparison has an equality gate failure or recorded confound")

    # Short names are retained for callers that use the F/L nomenclature from
    # the study protocol.  The explicit names above remain the canonical wire
    # fields and, crucially, keep fixture F distinct from live-call L.
    @property
    def primary_f_sha256(self) -> str:
        return self.primary_fixture_request_sha256

    @property
    def withheld_f_sha256(self) -> str:
        return self.withheld_fixture_request_sha256

    @property
    def primary_l_sha256(self) -> str:
        return self.primary_live_call_sha256

    @property
    def withheld_l_sha256(self) -> str:
        return self.withheld_live_call_sha256

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "primary_preflight_sha256": self.primary_preflight_sha256,
            "withheld_preflight_sha256": self.withheld_preflight_sha256,
            "primary_fixture_request_sha256": self.primary_fixture_request_sha256,
            "withheld_fixture_request_sha256": self.withheld_fixture_request_sha256,
            "primary_live_call_sha256": self.primary_live_call_sha256,
            "withheld_live_call_sha256": self.withheld_live_call_sha256,
            "base_input_equal": self.base_input_equal,
            "same_parent": self.same_parent,
            "same_task_wording": self.same_task_wording,
            "same_candidate_domain": self.same_candidate_domain,
            "same_model_settings": self.same_model_settings,
            "same_schema_shape": self.same_schema_shape,
            "same_caps": self.same_caps,
            "f_byte_sizes": list(self.f_byte_sizes),
            "l_byte_sizes": list(self.l_byte_sizes),
            "wire_message_byte_sizes": list(self.wire_message_byte_sizes),
            "schema_byte_sizes": list(self.schema_byte_sizes),
            "projection_counts": list(self.projection_counts),
            "row_counts": list(self.row_counts),
            "retained_identity_counts": list(self.retained_identity_counts),
            "omitted_identity_counts": list(self.omitted_identity_counts),
            "usage_bound_counts": list(self.usage_bound_counts),
            "evidence_identity_sets": [list(item) for item in self.evidence_identity_sets],
            "row_identity_sets": [list(item) for item in self.row_identity_sets],
            "row_payload_digests": [list(item) for item in self.row_payload_digests],
            "retained_id_sets": [list(item) for item in self.retained_id_sets],
            "omitted_id_sets": [list(item) for item in self.omitted_id_sets],
            "paired_changes": list(self.paired_changes),
            "allowed_differences": list(self.allowed_differences),
            "actual_differences": list(self.actual_differences),
            "design_differences": list(self.design_differences),
            "leak_observations": list(self.leak_observations),
            "audit_channels": list(self.audit_channels),
            "audit_limitations": list(self.audit_limitations),
            "unresolved_audit_channels": list(self.unresolved_audit_channels),
            "confounds": list(self.confounds),
            "status": self.status,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "RequestComparisonV1":
        if type(value) is not dict or set(value) != _COMPARISON_FIELDS:
            raise StudyContractError("comparison fields are invalid")
        return cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            primary_preflight_sha256=value["primary_preflight_sha256"],  # type: ignore[arg-type]
            withheld_preflight_sha256=value["withheld_preflight_sha256"],  # type: ignore[arg-type]
            primary_fixture_request_sha256=value["primary_fixture_request_sha256"],  # type: ignore[arg-type]
            withheld_fixture_request_sha256=value["withheld_fixture_request_sha256"],  # type: ignore[arg-type]
            primary_live_call_sha256=value["primary_live_call_sha256"],  # type: ignore[arg-type]
            withheld_live_call_sha256=value["withheld_live_call_sha256"],  # type: ignore[arg-type]
            base_input_equal=_bool(value["base_input_equal"], "base input equality"),
            same_parent=_bool(value["same_parent"], "parent equality"),
            same_task_wording=_bool(value["same_task_wording"], "task wording equality"),
            same_candidate_domain=_bool(value["same_candidate_domain"], "candidate domain equality"),
            same_model_settings=_bool(value["same_model_settings"], "model settings equality"),
            same_schema_shape=_bool(value["same_schema_shape"], "schema equality"),
            same_caps=_bool(value["same_caps"], "caps equality"),
            f_byte_sizes=_tuple_int(value["f_byte_sizes"], "F byte sizes"),
            l_byte_sizes=_tuple_int(value["l_byte_sizes"], "L byte sizes"),
            wire_message_byte_sizes=_tuple_int(value["wire_message_byte_sizes"], "wire message byte sizes"),
            schema_byte_sizes=_tuple_int(value["schema_byte_sizes"], "schema byte sizes"),
            projection_counts=_tuple_int(value["projection_counts"], "projection counts"),
            row_counts=_tuple_int(value["row_counts"], "row counts"),
            retained_identity_counts=_tuple_int(value["retained_identity_counts"], "retained identity counts"),
            omitted_identity_counts=_tuple_int(value["omitted_identity_counts"], "omitted identity counts"),
            usage_bound_counts=_tuple_int(value["usage_bound_counts"], "usage bound counts"),
            evidence_identity_sets=_tuple_text_pair(value["evidence_identity_sets"], "evidence identity sets"),
            row_identity_sets=_tuple_text_pair(value["row_identity_sets"], "row identity sets"),
            row_payload_digests=_tuple_text_pair(value["row_payload_digests"], "row payload digests"),
            retained_id_sets=_tuple_text_pair(value["retained_id_sets"], "retained identity sets"),
            omitted_id_sets=_tuple_text_pair(value["omitted_id_sets"], "omitted identity sets"),
            paired_changes=_tuple_text(value["paired_changes"], "paired changes"),
            allowed_differences=_tuple_text(value["allowed_differences"], "allowed differences"),
            actual_differences=_tuple_text(value["actual_differences"], "actual differences"),
            design_differences=_tuple_text(value["design_differences"], "design differences"),
            leak_observations=_tuple_text(value["leak_observations"], "leak observations"),
            audit_channels=_tuple_text(value["audit_channels"], "audit channels"),
            audit_limitations=_tuple_text(value["audit_limitations"], "audit limitations"),
            unresolved_audit_channels=_tuple_text(
                value["unresolved_audit_channels"], "unresolved audit channels"
            ),
            confounds=_tuple_text(value["confounds"], "confounds"),
            status=value["status"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "RequestComparisonV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        if type(encoded) is not bytes:
            raise StudyContractError("comparison bytes are invalid")
        try:
            primitive = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudyContractError("comparison JSON is invalid") from exc
        value = cls.from_primitive(primitive)
        if value.canonical_bytes() != encoded:
            raise StudyContractError("comparison JSON is not canonical")
        return value


def _load_live_call(*, store: StudyStoreV1, preflight: FixturePreflightV1) -> StudyCallRequestV1:
    matches: list[StudyCallRequestV1] = []
    for reference in store.list_refs(kind="calls"):
        try:
            call = StudyCallRequestV1.from_canonical_json(store.read(reference))
        except (StudyContractError, ValueError, TypeError) as exc:
            raise StudyAuthorityError("persisted study call is not canonical") from exc
        if call.fixture_request_sha256 == preflight.request_sha256:
            matches.append(call)
    if len(matches) != 1:
        raise StudyAuthorityError("study call lookup is not unique for the fixture request")
    return matches[0]


def _base_input(request: RoleRequestV5) -> object:
    role_input = request.role_input
    if type(role_input) is MechanismRoleInputV1:
        return role_input.base_input
    return role_input


@dataclass(frozen=True, slots=True)
class _TreatmentLayout:
    """Authenticated treatment positions for one fixture-side F request.

    The mechanism wrapper and issued evidence are allowed to differ between
    the primary and withheld arms, but only at the positions actually present
    in the authenticated F envelope.  Keeping the path and token together is
    intentional: an issued row copied to another position remains ordinary
    request material and is not normalized or classified as treatment.
    """

    wrapper: object | None
    message_wrapper_paths: frozenset[str]
    message_evidence_tokens_by_path: tuple[tuple[str, bytes], ...]
    role_evidence_tokens_by_path: tuple[tuple[str, bytes], ...]
    fixture_wrapper_paths: frozenset[str]
    fixture_evidence_tokens_by_path: tuple[tuple[str, bytes], ...]

    def paths_for_channel(self, channel: str) -> tuple[frozenset[str], dict[str, bytes]]:
        if channel in {"messages", "wire"}:
            return self.message_wrapper_paths, dict(self.message_evidence_tokens_by_path)
        if channel == "role_input":
            return (frozenset({""}) if self.wrapper is not None else frozenset()), {}
        if channel == "role_evidence":
            return frozenset(), dict(self.role_evidence_tokens_by_path)
        if channel == "fixture_request":
            return self.fixture_wrapper_paths, dict(self.fixture_evidence_tokens_by_path)
        return frozenset(), {}


def _primitive_paths(value: object, path: str = "") -> dict[str, object]:
    """Return every canonical primitive node keyed by its exact structural path."""

    result = {path: value}
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            result.update(_primitive_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_primitive_paths(item, f"{path}[{index}]"))
    return result


def _mechanism_evidence_items(request: RoleRequestV5) -> tuple[object, ...]:
    role_input = request.role_input
    if type(role_input) is not MechanismRoleInputV1:
        return ()
    mechanism_ids = {
        evidence_id
        for projection in role_input.projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    }
    return tuple(item for item in request.role_evidence.items if item.evidence_id in mechanism_ids)


def _message_evidence_token(item: object) -> bytes:
    return canonical_json_bytes_v5(
        {
            "evidence_id": item.evidence_id,
            "payload": {
                "metric_id": item.metric_id,
                "value": canonical_primitive_v5(item.value),
            },
        }
    )


def _build_treatment_layout(request: RoleRequestV5) -> _TreatmentLayout:
    """Derive exact treatment paths and occurrence counts from authenticated F."""

    if type(request) is not RoleRequestV5:
        raise StudyAuthorityError("fixture treatment layout request is invalid")
    wrapper = canonical_primitive_v5(request.role_input) if type(request.role_input) is MechanismRoleInputV1 else None
    mechanism_items = _mechanism_evidence_items(request)
    message_primitive = canonical_primitive_v5(request.messages)
    if not isinstance(message_primitive, list):
        raise StudyAuthorityError("fixture F messages are not a canonical list")

    message_wrapper_paths: list[str] = []
    message_row_candidates: list[tuple[str, bytes]] = []
    for message_index, message in enumerate(message_primitive):
        if not isinstance(message, dict) or not isinstance(message.get("content"), dict):
            raise StudyAuthorityError("fixture F message envelope is invalid")
        content = message["content"]
        role_input_value = content.get("role_input")
        if wrapper is not None and role_input_value == wrapper:
            message_wrapper_paths.append(f"[{message_index}].content.role_input")
        evidence = content.get("evidence")
        if not isinstance(evidence, list):
            raise StudyAuthorityError("fixture F evidence envelope is invalid")
        for row_index, row in enumerate(evidence):
            try:
                token = canonical_json_bytes_v5(row)
            except (TypeError, ValueError) as exc:
                raise StudyAuthorityError("fixture F evidence row is not canonical") from exc
            message_row_candidates.append((f"[{message_index}].content.evidence[{row_index}]", token))

    message_values = _primitive_paths(message_primitive)
    if wrapper is not None:
        wrapper_paths = tuple(path for path, value in message_values.items() if value == wrapper)
        if tuple(message_wrapper_paths) != wrapper_paths or len(message_wrapper_paths) != 1:
            raise StudyAuthorityError("fixture F mechanism wrapper position is not unique")

    message_evidence_tokens_by_path: list[tuple[str, bytes]] = []
    for item in mechanism_items:
        token = _message_evidence_token(item)
        matches = [(path, candidate) for path, candidate in message_row_candidates if candidate == token]
        if len(matches) != 1:
            raise StudyAuthorityError("fixture F mechanism evidence position is not unique")
        path, matched_token = matches[0]
        all_matches = [
            path_value for path_value, value in message_values.items()
            if _canonical_bytes_or_none(value) == matched_token
        ]
        if all_matches != [path]:
            raise StudyAuthorityError("fixture F mechanism evidence has unexpected duplicate positions")
        message_evidence_tokens_by_path.append((path, matched_token))

    role_evidence_primitive = canonical_primitive_v5(request.role_evidence)
    if not isinstance(role_evidence_primitive, dict) or not isinstance(role_evidence_primitive.get("items"), list):
        raise StudyAuthorityError("fixture F role evidence is not canonical")
    role_evidence_values = _primitive_paths(role_evidence_primitive)
    role_evidence_candidates = [
        (f"items[{index}]", canonical_json_bytes_v5(item))
        for index, item in enumerate(role_evidence_primitive["items"])
    ]
    role_evidence_tokens_by_path: list[tuple[str, bytes]] = []
    for item in mechanism_items:
        token = canonical_json_bytes_v5(item)
        matches = [(path, candidate) for path, candidate in role_evidence_candidates if candidate == token]
        if len(matches) != 1:
            raise StudyAuthorityError("fixture role evidence position is not unique")
        path, matched_token = matches[0]
        all_matches = [
            path_value for path_value, value in role_evidence_values.items()
            if _canonical_bytes_or_none(value) == matched_token
        ]
        if all_matches != [path]:
            raise StudyAuthorityError("fixture role evidence has unexpected duplicate positions")
        role_evidence_tokens_by_path.append((path, matched_token))

    fixture_primitive = canonical_primitive_v5(request)
    fixture_values = _primitive_paths(fixture_primitive)
    fixture_wrapper_paths = frozenset(
        {"role_input", *(f"messages{path}" for path in message_wrapper_paths)}
    ) if wrapper is not None else frozenset()
    fixture_evidence_tokens_by_path = (
        tuple((f"role_evidence.{path}", token) for path, token in role_evidence_tokens_by_path)
        + tuple((f"messages{path}", token) for path, token in message_evidence_tokens_by_path)
    )
    if wrapper is not None:
        fixture_wrapper_matches = frozenset(path for path, value in fixture_values.items() if value == wrapper)
        if fixture_wrapper_matches != fixture_wrapper_paths:
            raise StudyAuthorityError("fixture request wrapper positions are not authenticated")
    for path, token in fixture_evidence_tokens_by_path:
        all_matches = [
            path_value for path_value, value in fixture_values.items()
            if _canonical_bytes_or_none(value) == token
        ]
        if all_matches != [path]:
            raise StudyAuthorityError("fixture request evidence positions are not authenticated")

    return _TreatmentLayout(
        wrapper=wrapper,
        message_wrapper_paths=frozenset(message_wrapper_paths),
        message_evidence_tokens_by_path=tuple(message_evidence_tokens_by_path),
        role_evidence_tokens_by_path=tuple(role_evidence_tokens_by_path),
        fixture_wrapper_paths=fixture_wrapper_paths,
        fixture_evidence_tokens_by_path=fixture_evidence_tokens_by_path,
    )


def _canonical_bytes_or_none(value: object) -> bytes | None:
    try:
        return canonical_json_bytes_v5(value)
    except (TypeError, ValueError):
        return None


def _normalized_message_primitive(
    messages: object,
    layout: _TreatmentLayout,
) -> object:
    """Normalize only the known treatment wrapper and its issued rows.

    ``RoleRequestV5`` serializes the request-local evidence rows beside the
    role input.  The mechanism adapter issues additional rows for the wrapper;
    those rows are an intended treatment difference, while ordinary evidence
    rows remain in the wording equality check and therefore still expose a
    real instruction/history change.
    """

    primitive = canonical_primitive_v5(messages)
    wrapper = layout.wrapper
    evidence_tokens_by_path = dict(layout.message_evidence_tokens_by_path)

    def normalize(value: object, path: str = "") -> object:
        if isinstance(value, dict):
            result: dict[object, object] = {}
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if (
                    wrapper is not None
                    and key == "role_input"
                    and item == wrapper
                    and child_path in layout.message_wrapper_paths
                ):
                    result[key] = normalize(item["base_input"], child_path)
                else:
                    result[key] = normalize(item, child_path)
            return result
        if isinstance(value, list):
            result = []
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]"
                expected_token = evidence_tokens_by_path.get(child_path)
                if expected_token is not None and _canonical_bytes_or_none(item) == expected_token:
                    continue
                result.append(normalize(item, child_path))
            return tuple(result)
        if isinstance(value, tuple):
            result = []
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]"
                expected_token = evidence_tokens_by_path.get(child_path)
                if expected_token is not None and _canonical_bytes_or_none(item) == expected_token:
                    continue
                result.append(normalize(item, child_path))
            return tuple(result)
        return value

    return normalize(primitive)


def _normalized_messages(request: RoleRequestV5) -> object:
    return _normalized_message_primitive(request.messages, _build_treatment_layout(request))


def _identity_ids(request: RoleRequestV5) -> tuple[str, ...]:
    base = _base_input(request)
    values: list[str] = []
    for collection_name in ("critic_directions", "experiment_summaries"):
        for item in getattr(base, collection_name, ()):
            values.append(item.experiment_id)
    wrapper = request.role_input
    if type(wrapper) is MechanismRoleInputV1:
        values.extend(item.experiment_id for item in wrapper.projections)
    return tuple(dict.fromkeys(values))


def _role_evidence_ids(request: RoleRequestV5) -> tuple[str, ...]:
    return tuple(item.evidence_id for item in request.role_evidence.items)


def _row_identity(row: object) -> str:
    return canonical_json_bytes_v5(
        {
            "stage": row.stage,
            "episode_id": row.episode_id,
            "episode_ordinal": row.episode_ordinal,
            "scenario_id": row.scenario_id,
            "experiment_id": row.experiment_id,
            "hypothesis_id": row.hypothesis_id,
            "metric_id": row.prediction.metric_id,
        }
    ).decode("utf-8")


def _row_payload_digest(row: object) -> str:
    return _sha256(canonical_json_bytes_v5(canonical_primitive_v5(row)))


def _row_details(request: RoleRequestV5) -> tuple[tuple[str, ...], tuple[str, ...]]:
    role_input = request.role_input
    if type(role_input) is not MechanismRoleInputV1:
        return (), ()
    identities: list[str] = []
    payloads: list[str] = []
    for projection in role_input.projections:
        for row in projection.rows:
            identity = _row_identity(row)
            identities.append(identity)
            payloads.append(f"{identity}={_row_payload_digest(row)}")
    return tuple(identities), tuple(payloads)


def _paired_changes(
    *,
    primary: tuple[str, ...],
    withheld: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    changes = [
        f"{label}:primary_only:{item}" for item in primary if item not in withheld
    ]
    changes.extend(f"{label}:withheld_only:{item}" for item in withheld if item not in primary)
    return tuple(changes)


def _wrapper_counts(request: RoleRequestV5) -> tuple[int, int, int, tuple[str, ...]]:
    role_input = request.role_input
    if type(role_input) is not MechanismRoleInputV1:
        return 0, 0, 0, ()
    projections = role_input.projections
    retained = tuple(item.experiment_id for item in projections)
    omitted = tuple(item.experiment_id for item in role_input.omitted)
    return len(projections), sum(len(item.rows) for item in projections), len(retained), omitted


def _wire_content(call: StudyCallRequestV1) -> object:
    try:
        return json.loads(call.projected_wire_messages.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StudyAuthorityError("study wire messages are not JSON") from exc


def _validate_wire_projection(call: StudyCallRequestV1) -> None:
    """Bind persisted L wire bytes to the authenticated L message tuple."""

    try:
        expected = canonical_json_bytes_v5(wire_role_messages_v5(call.messages))
    except (TypeError, ValueError, StudyContractError) as exc:
        raise StudyAuthorityError("study wire messages cannot be reconstructed") from exc
    if call.projected_wire_messages != expected:
        raise StudyAuthorityError("study projected wire messages differ from L messages")


def _wire_audit_value(call: StudyCallRequestV1) -> object:
    """Decode the canonical wire envelope while retaining its message paths."""

    value = _wire_content(call)
    if not isinstance(value, list):
        return value
    decoded: list[object] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            decoded.append(item)
            continue
        content = item["content"]
        if not isinstance(content, str):
            raise StudyAuthorityError(f"study wire message {index} content is invalid")
        try:
            content_value = json.loads(content)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudyAuthorityError(f"study wire message {index} content is not JSON") from exc
        decoded.append({"role": item["role"], "content": content_value})
    return decoded


_LEAK_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contradiction assessment", re.compile(r"\bcontradicted\s+on\s+cases\b")),
    ("critic mechanism prose", re.compile(r"\bpaired\s+local\s+observations\b")),
    ("contradiction count", re.compile(r"\b0\s*/\s*3\s+decisions?\s+changed\b")),
    ("contradiction count", re.compile(r"\bzero\s+of\s+three\b")),
    ("contradiction statement", re.compile(r"\ba\s+was\s+contradicted\b")),
    ("contradiction statement", re.compile(r"\bno\s+decisions?\s+changed\b")),
    ("contradiction statement", re.compile(r"\bcontradiction\s+on\s+cases\b")),
    ("contradiction statement", re.compile(r"\bmechanism\s+contradicts\s+the\s+parent\b")),
    ("critic mechanism prose", re.compile(r"\bmechanism\s+contradiction\b")),
)


def _normalized_text(value: str) -> str:
    value = value.casefold().replace("_", " ").replace("-", " ")
    return " ".join(value.split())


def _audit_markers(
    value: object,
    *,
    side: str,
    channel: str,
    path: str,
    findings: list[str],
    treatment_findings: list[str],
    allow_treatment: bool,
    mechanism_wrapper: object | None,
    treatment_wrapper_paths: frozenset[str],
    treatment_evidence_tokens_by_path: dict[str, bytes],
    treatment_context: bool = False,
) -> None:
    def inspect_text(raw: str, location: str) -> None:
        normalized = _normalized_text(raw)
        for label, pattern in _LEAK_MARKERS:
            if pattern.search(normalized):
                evidence = " ".join(raw.split())[:160]
                finding = f"{side}:{channel}:{location}:{label}: {evidence}"
                target = treatment_findings if allow_treatment and treatment_context else findings
                if finding not in target:
                    target.append(finding)

    if isinstance(value, str):
        inspect_text(value, path or "<root>")
        return
    if isinstance(value, dict):
        if mechanism_wrapper is not None and value == mechanism_wrapper:
            # Only the explicit extension is intended treatment.  The
            # authenticated base input remains ordinary study history and is
            # audited for disclosure independently.
            _audit_markers(
                value["base_input"],
                side=side,
                channel=channel,
                path=f"{path}.base_input" if path else "base_input",
                findings=findings,
                treatment_findings=treatment_findings,
                allow_treatment=allow_treatment,
                mechanism_wrapper=mechanism_wrapper,
                treatment_wrapper_paths=treatment_wrapper_paths,
                treatment_evidence_tokens_by_path=treatment_evidence_tokens_by_path,
                treatment_context=False,
            )
            treatment_location = allow_treatment and path in treatment_wrapper_paths
            for key in ("role", "projection", "omitted"):
                child_path = f"{path}.{key}" if path else key
                _audit_markers(
                    value[key],
                    side=side,
                    channel=channel,
                    path=child_path,
                    findings=findings,
                    treatment_findings=treatment_findings,
                    allow_treatment=allow_treatment,
                    mechanism_wrapper=mechanism_wrapper,
                    treatment_wrapper_paths=treatment_wrapper_paths,
                    treatment_evidence_tokens_by_path=treatment_evidence_tokens_by_path,
                    treatment_context=treatment_location,
                )
            return
        try:
            evidence_token = canonical_json_bytes_v5(value)
        except (TypeError, ValueError):
            evidence_token = None
        if evidence_token is not None and path in treatment_evidence_tokens_by_path:
            treatment_context = (
                allow_treatment
                and treatment_evidence_tokens_by_path[path] == evidence_token
            )
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                normalized_key = _normalized_text(key)
                for label, pattern in _LEAK_MARKERS:
                    if pattern.search(normalized_key):
                        evidence = " ".join(key.split())[:160]
                        finding = f"{side}:{channel}:{key_path}[key]:{label}: {evidence}"
                        target = treatment_findings if allow_treatment and treatment_context else findings
                        if finding not in target:
                            target.append(finding)
            _audit_markers(
                item,
                side=side,
                channel=channel,
                path=key_path,
                findings=findings,
                treatment_findings=treatment_findings,
                allow_treatment=allow_treatment,
                mechanism_wrapper=mechanism_wrapper,
                treatment_wrapper_paths=treatment_wrapper_paths,
                treatment_evidence_tokens_by_path=treatment_evidence_tokens_by_path,
                treatment_context=treatment_context,
            )
        return
    if isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _audit_markers(
                item,
                side=side,
                channel=channel,
                path=f"{path}[{index}]",
                findings=findings,
                treatment_findings=treatment_findings,
                allow_treatment=allow_treatment,
                mechanism_wrapper=mechanism_wrapper,
                treatment_wrapper_paths=treatment_wrapper_paths,
                treatment_evidence_tokens_by_path=treatment_evidence_tokens_by_path,
                treatment_context=treatment_context,
            )


def _audit_channels(
    *,
    side: str,
    call: StudyCallRequestV1,
    request: RoleRequestV5,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    _validate_wire_projection(call)
    channels = (
        ("wire", _wire_audit_value(call)),
        ("messages", canonical_primitive_v5(call.messages)),
        ("role_input", canonical_primitive_v5(request.role_input)),
        ("role_evidence", canonical_primitive_v5(request.role_evidence)),
        ("fixture_request", canonical_primitive_v5(request)),
        ("schema", json.loads(call.schema_json.decode("utf-8"))),
    )
    findings: list[str] = []
    treatment_findings: list[str] = []
    names: list[str] = []
    layout = _build_treatment_layout(request)
    for channel, value in channels:
        treatment_wrapper_paths, treatment_evidence_tokens_by_path = layout.paths_for_channel(channel)
        names.append(f"{side}.{channel}.keys+values")
        _audit_markers(
            value,
            side=side,
            channel=channel,
            path="",
            findings=findings,
            treatment_findings=treatment_findings,
            allow_treatment=side == "primary",
            mechanism_wrapper=layout.wrapper,
            treatment_wrapper_paths=treatment_wrapper_paths,
            treatment_evidence_tokens_by_path=treatment_evidence_tokens_by_path,
        )
    return tuple(names), tuple(findings), tuple(treatment_findings)


def compare_study_requests_v1(
    *,
    primary: FixturePreflightV1,
    withheld: FixturePreflightV1,
    store: StudyStoreV1,
) -> RequestComparisonV1:
    """Authenticate and compare both persisted round-two F/L pairs."""

    if type(primary) is not FixturePreflightV1 or type(withheld) is not FixturePreflightV1:
        raise StudyContractError("study comparison preflights are invalid")
    if primary.arm != "primary" or withheld.arm != "withheld" or primary.mode != withheld.mode:
        raise StudyAuthorityError("study comparison arms or modes differ")
    if type(store) is not StudyStoreV1:
        raise StudyContractError("study comparison store is invalid")
    primary_f = authenticate_fixture_preflight_v1(primary, require_current=False)
    withheld_f = authenticate_fixture_preflight_v1(withheld, require_current=False)
    primary_l = _load_live_call(store=store, preflight=primary)
    withheld_l = _load_live_call(store=store, preflight=withheld)
    if primary_l.arm != "primary" or withheld_l.arm != "withheld":
        raise StudyAuthorityError("study call arms differ from their preflights")
    if primary_l.preflight_ref.sha256 != primary.sha256:
        raise StudyAuthorityError("primary live call is not bound to its preflight")
    if withheld_l.preflight_ref.sha256 != withheld.sha256:
        raise StudyAuthorityError("withheld live call is not bound to its preflight")

    primary_projection_count, _primary_wrapper_row_count, primary_retained_wrapper, primary_omitted = _wrapper_counts(primary_f)
    withheld_projection_count, _withheld_wrapper_row_count, withheld_retained_wrapper, withheld_omitted = _wrapper_counts(withheld_f)
    base_input_equal = canonical_primitive_v5(_base_input(primary_f)) == canonical_primitive_v5(_base_input(withheld_f))
    same_parent = primary_f.expected_binding.parent_revision_sha256 == withheld_f.expected_binding.parent_revision_sha256
    primary_layout = _build_treatment_layout(primary_f)
    withheld_layout = _build_treatment_layout(withheld_f)
    same_task_wording = (
        _normalized_message_primitive(primary_f.messages, primary_layout)
        == _normalized_message_primitive(withheld_f.messages, withheld_layout)
        and _normalized_message_primitive(
            primary_l.messages,
            primary_layout,
        )
        == _normalized_message_primitive(
            withheld_l.messages,
            withheld_layout,
        )
    )

    def candidate_domain(request: StudyCallRequestV1) -> tuple[object, ...]:
        try:
            schema = json.loads(request.schema_json.decode("utf-8"))
            ids = schema["properties"]["drafts"]["items"]["properties"]["configuration_id"]["enum"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudyAuthorityError("study schema candidate domain is unavailable") from exc
        if type(ids) is not list or any(type(item) is not str for item in ids):
            raise StudyAuthorityError("study schema candidate domain is invalid")
        return tuple(ids)

    same_candidate_domain = candidate_domain(primary_l) == candidate_domain(withheld_l)
    same_model_settings = (
        primary_l.provider,
        primary_l.model,
        primary_l.temperature,
        primary_l.seed,
    ) == (
        withheld_l.provider,
        withheld_l.model,
        withheld_l.temperature,
        withheld_l.seed,
    )
    same_schema_shape = primary_l.schema_json == withheld_l.schema_json
    same_caps = (primary_l.max_output_tokens, primary_l.transport_settings_sha256) == (
        withheld_l.max_output_tokens,
        withheld_l.transport_settings_sha256,
    )

    actual: list[str] = []
    if primary.sha256 != withheld.sha256:
        actual.append("preflight_identity")
    if primary.request_sha256 != withheld.request_sha256:
        actual.append("fixture_request_identity")
    if primary_f.role_input != withheld_f.role_input:
        if type(primary_f.role_input) is MechanismRoleInputV1 and type(withheld_f.role_input) is MechanismRoleInputV1:
            if canonical_primitive_v5(_base_input(primary_f)) == canonical_primitive_v5(_base_input(withheld_f)):
                actual.append("mechanism_wrapper")
            else:
                actual.append("role_input")
        elif type(primary_f.role_input) is MechanismRoleInputV1 or type(withheld_f.role_input) is MechanismRoleInputV1:
            actual.append("role_wrapper_presence")
        else:
            actual.append("role_input")
    if primary_f.role_evidence != withheld_f.role_evidence:
        actual.append("evidence_map")
    primary_evidence_ids = _role_evidence_ids(primary_f)
    withheld_evidence_ids = _role_evidence_ids(withheld_f)
    primary_row_ids, primary_row_payloads = _row_details(primary_f)
    withheld_row_ids, withheld_row_payloads = _row_details(withheld_f)
    # The exact typed rows are the authoritative row-count source.  The
    # wrapper aggregate is retained separately for projection/retention
    # accounting, but a count inferred from that aggregate could conceal a
    # same-count replacement when row identity or payload extraction changes.
    row_counts = (len(primary_row_ids), len(withheld_row_ids))
    primary_retained = tuple(
        item.experiment_id for item in primary_f.role_input.projections
    ) if type(primary_f.role_input) is MechanismRoleInputV1 else ()
    withheld_retained = tuple(
        item.experiment_id for item in withheld_f.role_input.projections
    ) if type(withheld_f.role_input) is MechanismRoleInputV1 else ()
    primary_omitted_ids = tuple(primary_omitted)
    withheld_omitted_ids = tuple(withheld_omitted)
    paired_changes = list(
        _paired_changes(primary=primary_evidence_ids, withheld=withheld_evidence_ids, label="evidence_id")
    )
    paired_changes.extend(
        _paired_changes(primary=primary_row_ids, withheld=withheld_row_ids, label="row_identity")
    )
    paired_changes.extend(
        _paired_changes(primary=primary_row_payloads, withheld=withheld_row_payloads, label="row_payload")
    )
    paired_changes.extend(
        _paired_changes(primary=primary_retained, withheld=withheld_retained, label="retained_id")
    )
    paired_changes.extend(
        _paired_changes(primary=primary_omitted_ids, withheld=withheld_omitted_ids, label="omitted_id")
    )
    if paired_changes:
        actual.append("paired_identity_or_payload_changes")
    if primary.fixture_root_identity_sha256 != withheld.fixture_root_identity_sha256:
        actual.append("fixture_root_identity")
    if primary.request_bytes != withheld.request_bytes:
        actual.append("fixture_request_bytes")
    if primary_l.messages != withheld_l.messages:
        actual.append("live_messages")
    if primary_l.projected_wire_messages != withheld_l.projected_wire_messages:
        actual.append("wire_message_bytes")
    if primary_l.schema_json != withheld_l.schema_json:
        actual.append("study_schema_bytes")
    if primary_l.max_output_tokens != withheld_l.max_output_tokens:
        actual.append("output_token_cap")
    if primary_l.input_bound_bytes != withheld_l.input_bound_bytes:
        actual.append("prospective_input_bound")
    if primary_l.transport_settings_sha256 != withheld_l.transport_settings_sha256:
        actual.append("transport_settings")
    if primary_l.sha256 != withheld_l.sha256:
        actual.append("live_request_identity")
    actual = list(dict.fromkeys(actual))

    retained_counts = (len(_identity_ids(primary_f)), len(_identity_ids(withheld_f)))
    omitted_counts = (len(primary_omitted), len(withheld_omitted))
    audit_channels: list[str] = []
    primary_findings: tuple[str, ...] = ()
    primary_treatment_findings: tuple[str, ...] = ()
    try:
        primary_channel_names, primary_findings, primary_treatment_findings = _audit_channels(
            side="primary",
            call=primary_l,
            request=primary_f,
        )
        withheld_channel_names, withheld_findings, _withheld_treatment_findings = _audit_channels(
            side="withheld",
            call=withheld_l,
            request=withheld_f,
        )
        audit_channels.extend(primary_channel_names)
        audit_channels.extend(withheld_channel_names)
        leaks = [*primary_findings, *withheld_findings]
    except (StudyAuthorityError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        leaks = []
        unresolved_audit_channels = ("primary/withheld.structural_channels",)
        actual.append("audit_unresolved")
        audit_error = str(exc)
    else:
        unresolved_audit_channels = ()
        audit_error = ""
    if primary_treatment_findings:
        actual.extend(
            f"primary treatment observation: {finding}" for finding in primary_treatment_findings
        )
    if primary_findings:
        actual.extend(
            f"primary ordinary disclosure: {finding}" for finding in primary_findings
        )
    if primary_f.role_input != withheld_f.role_input and type(primary_f.role_input) is MechanismRoleInputV1:
        actual.append("primary_mechanism_assessment_delivery")
    actual = list(dict.fromkeys(actual))
    confounds: list[str] = []
    design_differences: list[str] = []
    if primary_l.input_bound_bytes != withheld_l.input_bound_bytes:
        design_differences.append("prospective_input_size")
    if len(primary.request_bytes) != len(withheld.request_bytes):
        design_differences.append("fixture_request_size")
    if len(primary_l.canonical_bytes()) != len(withheld_l.canonical_bytes()):
        design_differences.append("live_request_size")
    if row_counts[0] != row_counts[1]:
        design_differences.append("mechanism_row_retention")
    if primary_retained_wrapper != withheld_retained_wrapper:
        design_differences.append("mechanism_projection_retention")
    if primary_omitted != withheld_omitted:
        design_differences.append("mechanism_omitted_id_retention")

    allowed = (
        "mechanism_wrapper",
        "evidence_map",
        "fixture_root_identity",
        "preflight_identity",
        "fixture_request_identity",
        "fixture_request_bytes",
        "live_messages",
        "wire_message_bytes",
        "live_request_identity",
        "primary_mechanism_assessment_delivery",
        "paired_identity_or_payload_changes",
    )
    equality_gates = {
        "base_input": base_input_equal,
        "same_parent": same_parent,
        "same_task_wording": same_task_wording,
        "same_candidate_domain": same_candidate_domain,
        "same_model_settings": same_model_settings,
        "same_schema_shape": same_schema_shape,
        "same_caps": same_caps,
    }
    for gate, passed in equality_gates.items():
        if not passed:
            confounds.append(f"paired equality gate failed: {gate}")
    if unresolved_audit_channels:
        confounds.extend(f"unresolved audit channel: {item}" for item in unresolved_audit_channels)
    if audit_error:
        confounds.append("audit parser failure")
    confounds = list(dict.fromkeys(confounds))
    status: ComparisonStatusV1 = "inconclusive" if leaks or confounds else "eligible"
    audit_limitations = (
        "deterministic normalized marker audit over persisted keys/values; it cannot establish arbitrary semantic absence",
    )
    return RequestComparisonV1(
        schema_version=1,
        primary_preflight_sha256=primary.sha256,
        withheld_preflight_sha256=withheld.sha256,
        primary_fixture_request_sha256=primary.request_sha256,
        withheld_fixture_request_sha256=withheld.request_sha256,
        primary_live_call_sha256=primary_l.sha256,
        withheld_live_call_sha256=withheld_l.sha256,
        base_input_equal=base_input_equal,
        same_parent=same_parent,
        same_task_wording=same_task_wording,
        same_candidate_domain=same_candidate_domain,
        same_model_settings=same_model_settings,
        same_schema_shape=same_schema_shape,
        same_caps=same_caps,
        f_byte_sizes=(len(primary.request_bytes), len(withheld.request_bytes)),
        l_byte_sizes=(len(primary_l.canonical_bytes()), len(withheld_l.canonical_bytes())),
        wire_message_byte_sizes=(len(primary_l.projected_wire_messages), len(withheld_l.projected_wire_messages)),
        schema_byte_sizes=(len(primary_l.schema_json), len(withheld_l.schema_json)),
        projection_counts=(primary_projection_count, withheld_projection_count),
        row_counts=row_counts,
        retained_identity_counts=retained_counts,
        omitted_identity_counts=omitted_counts,
        usage_bound_counts=(primary_l.input_bound_bytes, withheld_l.input_bound_bytes),
        evidence_identity_sets=(primary_evidence_ids, withheld_evidence_ids),
        row_identity_sets=(primary_row_ids, withheld_row_ids),
        row_payload_digests=(primary_row_payloads, withheld_row_payloads),
        retained_id_sets=(primary_retained, withheld_retained),
        omitted_id_sets=(primary_omitted_ids, withheld_omitted_ids),
        paired_changes=tuple(dict.fromkeys(paired_changes)),
        allowed_differences=allowed,
        actual_differences=tuple(actual),
        design_differences=tuple(dict.fromkeys(design_differences)),
        leak_observations=tuple(leaks),
        audit_channels=tuple(audit_channels),
        audit_limitations=audit_limitations,
        unresolved_audit_channels=unresolved_audit_channels,
        confounds=tuple(confounds),
        status=status,
    )


__all__ = ["RequestComparisonV1", "compare_study_requests_v1"]
