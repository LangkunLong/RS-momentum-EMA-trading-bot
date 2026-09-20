"""Immutable case contrasts and raw-decision verification for Task 5."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from core.pit_optimizer_v5.contracts import ArtifactRefV5, canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.mechanism_probes import MechanismObservationRunV1

from .contracts import RivalPatternV1, StudyContractError, StudyPrecommitmentError


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SCHEMA_VERSION_V1 = 1


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
    value = _text(value, label, maximum=128)
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise StudyContractError(f"{label} is invalid")
    return value


def _strict_mapping(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise StudyContractError(f"{label} fields are invalid")
    return value


def _strict_json(raw: bytes, label: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise StudyContractError(f"{label} contains duplicate fields")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise StudyContractError(f"{label} contains nonfinite JSON value {value}")

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    except StudyContractError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StudyContractError(f"{label} is invalid JSON") from exc
    if canonical_json_bytes_v5(parsed) != raw:
        raise StudyContractError(f"{label} is not canonical JSON")
    return parsed


def _ref(value: object, label: str) -> ArtifactRefV5:
    if type(value) is not dict or set(value) != {"relative_path", "sha256"}:
        raise StudyContractError(f"{label} is invalid")
    try:
        return ArtifactRefV5(value["relative_path"], value["sha256"])  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StudyContractError(f"{label} is invalid") from exc


def _bool_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[bool, ...]:
    if type(value) is not tuple or (nonempty and not value) or len(value) > 64:
        raise StudyContractError(f"{label} is invalid")
    if any(type(item) is not bool for item in value):
        raise StudyContractError(f"{label} must contain strict booleans")
    return value


def _text_tuple(value: object, label: str, *, maximum: int = 100_000) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > maximum:
        raise StudyContractError(f"{label} is invalid")
    parsed = tuple(_text(item, label, maximum=256) for item in value)
    if len(set(parsed)) != len(parsed):
        raise StudyContractError(f"{label} must be unique")
    return parsed


def _digest_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value or len(value) > 64:
        raise StudyContractError(f"{label} is invalid")
    parsed = tuple(_digest(item, label) for item in value)
    if len(set(parsed)) != len(parsed):
        raise StudyContractError(f"{label} must be unique")
    return parsed


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class StudyContrastV1:
    """Model-authored ordered case pattern frozen before any authoring."""

    draft_binding_ref: ArtifactRefV5
    draft_binding_sha256: str
    parent_revision_sha256: str
    parent_source_bundle_ref: ArtifactRefV5
    spec_sha256: str
    corpus_sha256: str
    precommitment_id: str
    case_ids: tuple[str, ...]
    input_identities: tuple[str, ...]
    expected_changed: tuple[bool, ...]
    rivals: tuple[RivalPatternV1, ...]
    claim_kind: Literal["threshold", "general"] = "general"
    schema_version: Literal[1] = _SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if type(self.draft_binding_ref) is not ArtifactRefV5:
            raise StudyContractError("contrast draft-binding reference is invalid")
        _digest(self.draft_binding_sha256, "contrast draft-binding identity")
        if self.draft_binding_ref.sha256 != self.draft_binding_sha256:
            raise StudyPrecommitmentError("contrast draft-binding reference differs from its identity")
        _digest(self.parent_revision_sha256, "contrast parent revision")
        if type(self.parent_source_bundle_ref) is not ArtifactRefV5:
            raise StudyContractError("contrast parent source reference is invalid")
        _digest(self.spec_sha256, "contrast spec identity")
        _digest(self.corpus_sha256, "contrast corpus identity")
        _identifier(self.precommitment_id, "contrast precommitment ID")
        case_ids = _text_tuple(self.case_ids, "contrast case IDs")
        input_ids = _digest_tuple(self.input_identities, "contrast input identities")
        expected = _bool_tuple(self.expected_changed, "contrast expected pattern")
        if not (len(case_ids) == len(input_ids) == len(expected)):
            raise StudyPrecommitmentError("contrast case IDs, inputs, and expected pattern differ in length")
        if tuple(case_ids) != tuple(f"case-{index}" for index in range(len(case_ids))):
            raise StudyPrecommitmentError("contrast case IDs are not the frozen ordered case vocabulary")
        if type(self.rivals) is not tuple or not self.rivals or any(type(item) is not RivalPatternV1 for item in self.rivals):
            raise StudyContractError("contrast rival patterns are invalid")
        if len({item.name for item in self.rivals}) != len(self.rivals):
            raise StudyPrecommitmentError("contrast rival names are not unique")
        if any(item.pattern == expected for item in self.rivals):
            raise StudyPrecommitmentError("claimed and rival patterns cannot be identical")
        if any(len(item.pattern) != len(expected) for item in self.rivals):
            raise StudyPrecommitmentError("contrast rival pattern length differs from the frozen cases")
        if self.claim_kind not in {"threshold", "general"}:
            raise StudyContractError("contrast claim kind is invalid")
        if self.claim_kind == "threshold":
            by_name = {item.name: item.pattern for item in self.rivals}
            all_false = (False,) * len(expected)
            all_true = (True,) * len(expected)
            if by_name.get("inert") != all_false or by_name.get("always_on") != all_true:
                raise StudyPrecommitmentError("threshold contrast requires inert and always-on rivals")
        if self.schema_version != _SCHEMA_VERSION_V1 or type(self.schema_version) is not int:
            raise StudyContractError("contrast schema version is invalid")

    @property
    def binding_ref(self) -> ArtifactRefV5:
        return self.draft_binding_ref

    @property
    def draft_binding_identity_sha256(self) -> str:
        return self.draft_binding_sha256

    @property
    def parent_source_bundle_sha256(self) -> str:
        return self.parent_source_bundle_ref.sha256

    @property
    def storage_ref(self) -> ArtifactRefV5:
        return ArtifactRefV5(f"adapter-blobs/study-v1-contrasts/{self.sha256}.bin", self.sha256)

    def to_primitive(self) -> dict[str, object]:
        return {
            "draft_binding_ref": self.draft_binding_ref.to_primitive(),
            "draft_binding_sha256": self.draft_binding_sha256,
            "parent_revision_sha256": self.parent_revision_sha256,
            "parent_source_bundle_ref": self.parent_source_bundle_ref.to_primitive(),
            "spec_sha256": self.spec_sha256,
            "corpus_sha256": self.corpus_sha256,
            "precommitment_id": self.precommitment_id,
            "case_ids": list(self.case_ids),
            "input_identities": list(self.input_identities),
            "expected_changed": list(self.expected_changed),
            "rivals": [item.to_primitive() for item in self.rivals],
            "claim_kind": self.claim_kind,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "StudyContrastV1":
        raw = _strict_mapping(
            value,
            {
                "draft_binding_ref",
                "draft_binding_sha256",
                "parent_revision_sha256",
                "parent_source_bundle_ref",
                "spec_sha256",
                "corpus_sha256",
                "precommitment_id",
                "case_ids",
                "input_identities",
                "expected_changed",
                "rivals",
                "claim_kind",
                "schema_version",
            },
            "study contrast",
        )
        arrays = (raw["case_ids"], raw["input_identities"], raw["expected_changed"], raw["rivals"])
        if any(type(item) is not list for item in arrays):
            raise StudyContractError("study contrast arrays are invalid")
        return cls(
            draft_binding_ref=_ref(raw["draft_binding_ref"], "contrast draft-binding reference"),
            draft_binding_sha256=raw["draft_binding_sha256"],  # type: ignore[arg-type]
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            parent_source_bundle_ref=_ref(raw["parent_source_bundle_ref"], "contrast parent source reference"),
            spec_sha256=raw["spec_sha256"],  # type: ignore[arg-type]
            corpus_sha256=raw["corpus_sha256"],  # type: ignore[arg-type]
            precommitment_id=raw["precommitment_id"],  # type: ignore[arg-type]
            case_ids=tuple(raw["case_ids"]),  # type: ignore[arg-type]
            input_identities=tuple(raw["input_identities"]),  # type: ignore[arg-type]
            expected_changed=tuple(raw["expected_changed"]),  # type: ignore[arg-type]
            rivals=tuple(RivalPatternV1.from_primitive(item) for item in raw["rivals"]),  # type: ignore[arg-type]
            claim_kind=raw["claim_kind"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "StudyContrastV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _strict_json(encoded, "study contrast")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise StudyContractError("study contrast encoding is not canonical")
        return parsed


def compare_case_patterns_v1(
    *,
    expected: tuple[bool, ...],
    observed: tuple[bool, ...] | None,
) -> tuple[str, tuple[int, ...]]:
    """Compare a complete ordered pattern without treating missing data as false."""

    expected = _bool_tuple(expected, "expected case pattern")
    if observed is None:
        return "unavailable", ()
    observed = _bool_tuple(observed, "observed case pattern")
    if len(observed) != len(expected):
        raise StudyContractError("observed case pattern length differs from expected")
    mismatches = tuple(index for index, (left, right) in enumerate(zip(expected, observed, strict=True)) if left != right)
    return ("matched_on_cases" if not mismatches else "contradicted_on_cases", mismatches)


def _run_identity(run: MechanismObservationRunV1) -> str:
    try:
        return canonical_sha256_v5(run)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise StudyContractError("mechanism run cannot be canonically identified") from exc


@dataclass(frozen=True, slots=True)
class CaseContrastResultV1:
    """Raw, nonmutating comparison result for one authenticated mechanism run."""

    contract_ref: str
    run_ref: str
    status: Literal["matched_on_cases", "contradicted_on_cases", "unavailable"]
    observed_changed: tuple[bool, ...] | None
    mismatched_case_ids: tuple[str, ...]
    missing_case_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    schema_version: Literal[1] = _SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _digest(self.contract_ref, "contrast result contract reference")
        _digest(self.run_ref, "contrast result run reference")
        if self.status not in {"matched_on_cases", "contradicted_on_cases", "unavailable"}:
            raise StudyContractError("contrast result status is invalid")
        if self.observed_changed is not None:
            _bool_tuple(self.observed_changed, "contrast result observed pattern")
        _text_tuple_allow_empty(self.mismatched_case_ids, "contrast result mismatched case IDs")
        _text_tuple_allow_empty(self.missing_case_ids, "contrast result missing case IDs")
        _text_tuple_allow_empty(self.limitations, "contrast result limitations", maximum=32)
        if self.schema_version != _SCHEMA_VERSION_V1 or type(self.schema_version) is not int:
            raise StudyContractError("contrast result schema version is invalid")
        if self.status == "unavailable" and self.observed_changed is not None:
            raise StudyContractError("unavailable contrast result cannot carry an observed pattern")
        if self.status == "unavailable" and self.mismatched_case_ids:
            raise StudyContractError("unavailable contrast result cannot carry mismatched cases")
        if self.status != "unavailable" and self.observed_changed is None:
            raise StudyContractError("complete contrast result requires an observed pattern")
        if self.status == "matched_on_cases" and (self.mismatched_case_ids or self.missing_case_ids):
            raise StudyContractError("matched contrast result cannot carry mismatches or missing cases")
        if self.status == "contradicted_on_cases" and (not self.mismatched_case_ids or self.missing_case_ids):
            raise StudyContractError("contradicted contrast result must carry only mismatched cases")
        if set(self.mismatched_case_ids).intersection(self.missing_case_ids):
            raise StudyContractError("contrast result case cannot be both mismatched and missing")

    @property
    def contract_sha256(self) -> str:
        return self.contract_ref

    @property
    def run_sha256(self) -> str:
        return self.run_ref

    @property
    def observed_pattern(self) -> tuple[bool, ...] | None:
        return self.observed_changed

    @property
    def mismatch_case_ids(self) -> tuple[str, ...]:
        return self.mismatched_case_ids

    def to_primitive(self) -> dict[str, object]:
        return {
            "contract_ref": self.contract_ref,
            "run_ref": self.run_ref,
            "status": self.status,
            "observed_changed": None if self.observed_changed is None else list(self.observed_changed),
            "mismatched_case_ids": list(self.mismatched_case_ids),
            "missing_case_ids": list(self.missing_case_ids),
            "limitations": list(self.limitations),
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_primitive(cls, value: object) -> "CaseContrastResultV1":
        raw = _strict_mapping(
            value,
            {
                "contract_ref",
                "run_ref",
                "status",
                "observed_changed",
                "mismatched_case_ids",
                "missing_case_ids",
                "limitations",
                "schema_version",
            },
            "contrast result",
        )
        observed = raw["observed_changed"]
        if observed is not None:
            if type(observed) is not list:
                raise StudyContractError("contrast result observed pattern is invalid")
            observed = tuple(observed)
        for key in ("mismatched_case_ids", "missing_case_ids", "limitations"):
            if type(raw[key]) is not list:
                raise StudyContractError(f"contrast result {key} is invalid")
        return cls(
            contract_ref=raw["contract_ref"],  # type: ignore[arg-type]
            run_ref=raw["run_ref"],  # type: ignore[arg-type]
            status=raw["status"],  # type: ignore[arg-type]
            observed_changed=observed,  # type: ignore[arg-type]
            mismatched_case_ids=tuple(raw["mismatched_case_ids"]),  # type: ignore[arg-type]
            missing_case_ids=tuple(raw["missing_case_ids"]),  # type: ignore[arg-type]
            limitations=tuple(raw["limitations"]),  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "CaseContrastResultV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _strict_json(encoded, "contrast result")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise StudyContractError("contrast result encoding is not canonical")
        return parsed


def _text_tuple_allow_empty(value: object, label: str, *, maximum: int = 100_000) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise StudyContractError(f"{label} is invalid")
    parsed = tuple(_text(item, label, maximum=512) for item in value)
    if len(set(parsed)) != len(parsed):
        raise StudyContractError(f"{label} must be unique")
    return parsed


def evaluate_case_contrast_v1(
    *,
    contrast: StudyContrastV1,
    run: MechanismObservationRunV1,
) -> CaseContrastResultV1:
    """Reauthenticate a run and compare raw canonical decision bytes.

    A failed or partial run remains unavailable.  In particular, this helper
    never pads a prefix with false observations and never mutates a mechanism
    report or its aggregate assessment.
    """

    if type(contrast) is not StudyContrastV1 or type(run) is not MechanismObservationRunV1:
        raise StudyContractError("case contrast inputs are invalid")
    contract_ref = contrast.sha256
    run_ref = _run_identity(run)
    frozen_case_ids = contrast.case_ids
    frozen_inputs = contrast.input_identities
    limitations = list(run.limitations)
    binding = run.binding
    if (
        binding.spec_sha256 != contrast.spec_sha256
        or binding.corpus_sha256 != contrast.corpus_sha256
        or binding.parent_revision_sha256 != contrast.parent_revision_sha256
        or run.corpus.sha256 != contrast.corpus_sha256
        or tuple(case.input_identity_sha256 for case in run.corpus.cases) != frozen_inputs
    ):
        limitations.append("The mechanism run is bound to a different immutable contrast graph.")
        return CaseContrastResultV1(
            contract_ref=contract_ref,
            run_ref=run_ref,
            status="unavailable",
            observed_changed=None,
            mismatched_case_ids=(),
            missing_case_ids=frozen_case_ids,
            limitations=tuple(dict.fromkeys(limitations)),
        )
    if run.execution.status != "completed":
        limitations.append("Execution did not complete the full ordered case lattice.")
        observed_orders = {item.case_order for item in run.observations if item.repetition == 0}
        missing = tuple(frozen_case_ids[index] for index in range(len(frozen_case_ids)) if index not in observed_orders)
        return CaseContrastResultV1(
            contract_ref=contract_ref,
            run_ref=run_ref,
            status="unavailable",
            observed_changed=None,
            mismatched_case_ids=(),
            missing_case_ids=missing,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    first = tuple(item for item in run.observations if item.repetition == 0)
    observed_orders = tuple(item.case_order for item in first)
    if observed_orders != tuple(range(len(frozen_case_ids))):
        limitations.append("The completed run did not expose the frozen ordered case set.")
        missing = tuple(frozen_case_ids[index] for index in range(len(frozen_case_ids)) if index not in observed_orders)
        return CaseContrastResultV1(
            contract_ref=contract_ref,
            run_ref=run_ref,
            status="unavailable",
            observed_changed=None,
            mismatched_case_ids=(),
            missing_case_ids=missing,
            limitations=tuple(dict.fromkeys(limitations)),
        )
    observed = tuple(item.parent_decision_json != item.candidate_decision_json for item in first)
    status, mismatch_indices = compare_case_patterns_v1(expected=contrast.expected_changed, observed=observed)
    return CaseContrastResultV1(
        contract_ref=contract_ref,
        run_ref=run_ref,
        status=status,  # type: ignore[arg-type]
        observed_changed=observed,
        mismatched_case_ids=tuple(frozen_case_ids[index] for index in mismatch_indices),
        missing_case_ids=(),
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "CaseContrastResultV1",
    "StudyContrastV1",
    "compare_case_patterns_v1",
    "evaluate_case_contrast_v1",
]
