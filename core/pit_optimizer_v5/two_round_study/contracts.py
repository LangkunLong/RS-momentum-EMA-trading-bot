"""Closed, immutable contracts for the two-round V5 study.

The study adapter is deliberately opt-in.  Its records have their own V1
wire shape and do not alter the existing investigator protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re
from typing import Literal

from core.pit_optimizer_v5.contracts import (
    ArtifactRefV5,
    HypothesisV5,
    InvestigatorArtifactV5,
    MetricPredictionV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
)
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismDisconfirmingObservationV1,
    MechanismMetricSpecV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
    MechanismResourceBudgetV1,
)
from core.pit_optimizer_v5.provider import RoleBindingV5


StudyArmV1 = Literal["primary", "withheld"]
StudyModeV1 = Literal["offline_fixture", "live_study"]
StudyGateV1 = Literal["verified", "failed", "incomplete", "not_assessed"]
StudyEvidenceUseV1 = Literal["not_assessed", "supported", "not_supported", "inconclusive"]
StudyProductionAssessmentV1 = Literal[
    "supported_on_cases",
    "contradicted_on_cases",
    "insufficient_evidence",
]
StudyCaseContrastV1 = Literal["matched_on_cases", "contradicted_on_cases", "unavailable"]
StudyFeedbackAttributionV1 = Literal["supported", "inconclusive", "not_assessed"]

STUDY_SCHEMA_VERSION_V1 = 1
STUDY_RIVAL_NAMES_V1 = ("always_on", "inert")
STUDY_MAX_ID_BYTES_V1 = 256
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_EVIDENCE_RE = re.compile(r"v5\.[a-z0-9_.-]{1,120}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_PROVIDER_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class StudyContractError(ValueError):
    """The study value is outside its closed typed contract."""


class StudyAuthorityError(StudyContractError):
    """An immutable stored value or authority edge cannot be authenticated."""


class StudyAdmissionError(StudyContractError):
    """A value cannot be admitted under the frozen study bounds."""


class StudyPendingAccounting(StudyContractError):
    """A study operation has unresolved external accounting."""


class StudyPrecommitmentError(StudyContractError):
    """A value would alter an already frozen study commitment."""


def _error(message: str) -> StudyContractError:
    return StudyContractError(message)


def _exact_mapping(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise _error(f"{label} fields are invalid")
    return value


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise _error(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, maximum=STUDY_MAX_ID_BYTES_V1)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise _error(f"{label} is invalid")
    return text


def _provider_model(value: object) -> str:
    text = _text(value, "provider model", maximum=192)
    if _PROVIDER_MODEL_RE.fullmatch(text) is None:
        raise _error("provider model is invalid")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise _error(f"{label} is not a SHA-256 digest")
    return value


def _source_revision(value: object) -> str:
    text = _text(value, "source revision", maximum=256)
    if _SHA1_RE.fullmatch(text) is None and _DIGEST_RE.fullmatch(text) is None and _IDENTIFIER_RE.fullmatch(text) is None:
        raise _error("source revision is invalid")
    return text


def _tuple(value: object, label: str, *, nonempty: bool = False, maximum: int = 256) -> tuple[object, ...]:
    if type(value) is not tuple or (nonempty and not value) or len(value) > maximum:
        raise _error(f"{label} is invalid")
    return value


def _bool_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[bool, ...]:
    items = _tuple(value, label, nonempty=nonempty, maximum=64)
    if any(type(item) is not bool for item in items):
        raise _error(f"{label} must contain booleans")
    return items  # type: ignore[return-value]


def _digest_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[str, ...]:
    items = _tuple(value, label, nonempty=nonempty, maximum=256)
    parsed = tuple(_digest(item, label) for item in items)
    if len(set(parsed)) != len(parsed):
        raise _error(f"{label} must be unique")
    return parsed


def _evidence_tuple(value: object, label: str, *, nonempty: bool = True) -> tuple[str, ...]:
    items = _tuple(value, label, nonempty=nonempty, maximum=256)
    if any(type(item) is not str or _EVIDENCE_RE.fullmatch(item) is None for item in items):
        raise _error(f"{label} contains an invalid evidence ID")
    if len(set(items)) != len(items):
        raise _error(f"{label} must be unique")
    return items  # type: ignore[return-value]


def _parse_json(raw: bytes) -> object:
    if type(raw) is not bytes:
        raise _error("canonical study bytes must be bytes")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise _error("study JSON contains duplicate fields")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise _error(f"nonfinite JSON constant {value} is not allowed")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except StudyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _error("study JSON is invalid") from exc


def _canonical_decode(cls: type[_CanonicalStudyV1], raw: bytes | str) -> _CanonicalStudyV1:
    if type(raw) is str:
        try:
            encoded = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _error("study JSON is invalid") from exc
    elif type(raw) is bytes:
        encoded = raw
    else:
        raise _error("study JSON must be UTF-8 bytes or text")
    primitive = _parse_json(encoded)
    try:
        value = cls.from_primitive(primitive)
    except StudyContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise _error("study value does not satisfy its closed contract") from exc
    if value.canonical_bytes() != encoded:
        raise StudyContractError("study JSON encoding is not canonical")
    return value


class _CanonicalStudyV1:
    """Small common surface for immutable V1 records."""

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: bytes | str):
        return _canonical_decode(cls, raw)


@dataclass(frozen=True, slots=True, init=False)
class RivalPatternV1(_CanonicalStudyV1):
    """One registered rival's ordered boolean outcome pattern."""

    name: str
    pattern: tuple[bool, ...]

    def __init__(
        self,
        name: str | None = None,
        pattern: tuple[bool, ...] | None = None,
        *,
        rival_name: str | None = None,
        expected_changed: tuple[bool, ...] | None = None,
    ) -> None:
        if name is None:
            name = rival_name
        elif rival_name is not None and rival_name != name:
            raise _error("rival name aliases differ")
        if pattern is None:
            pattern = expected_changed
        elif expected_changed is not None and expected_changed != pattern:
            raise _error("rival pattern aliases differ")
        object.__setattr__(self, "name", _identifier(name, "rival name"))
        object.__setattr__(self, "pattern", _bool_tuple(pattern, "rival pattern"))
        if self.name not in STUDY_RIVAL_NAMES_V1:
            raise _error("rival name is not registered")

    @property
    def rival_name(self) -> str:
        return self.name

    @property
    def expected_changed(self) -> tuple[bool, ...]:
        return self.pattern

    def to_primitive(self) -> dict[str, object]:
        return {"name": self.name, "pattern": list(self.pattern)}

    @classmethod
    def from_primitive(cls, value: object) -> RivalPatternV1:
        raw = _exact_mapping(value, {"name", "pattern"}, "rival pattern")
        pattern = raw["pattern"]
        if type(pattern) is not list:
            raise _error("rival pattern must be an array")
        return cls(name=raw["name"], pattern=tuple(pattern))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ExperimentDraftV1(_CanonicalStudyV1):
    """The model's closed, precommitted experiment proposal."""

    hypothesis_id: str
    cited_evidence_ids: tuple[str, ...]
    applicability: MechanismPredicateV1
    recipe: MechanismRecipeV1
    metrics: tuple[MechanismMetricSpecV1, ...]
    disconfirming_observations: tuple[MechanismDisconfirmingObservationV1, ...]
    expected_changed: tuple[bool, ...]
    rivals: tuple[RivalPatternV1, ...]
    configuration_id: str
    claim_kind: Literal["threshold", "general"]

    def __post_init__(self) -> None:
        _identifier(self.hypothesis_id, "draft hypothesis ID")
        _evidence_tuple(self.cited_evidence_ids, "draft cited evidence")
        if type(self.applicability) is not MechanismPredicateV1:
            raise _error("draft applicability is invalid")
        if type(self.recipe) is not MechanismRecipeV1:
            raise _error("draft recipe is invalid")
        metrics = _tuple(self.metrics, "draft metrics", nonempty=True, maximum=16)
        if any(type(item) is not MechanismMetricSpecV1 for item in metrics):
            raise _error("draft metrics are invalid")
        if len({item.metric_id for item in metrics}) != len(metrics):
            raise _error("draft metrics must be unique")
        if any(item.metric_id == "evaluator.exit_attribution_count" for item in metrics):
            raise _error("local study drafts cannot use the evaluator metric")
        observations = _tuple(
            self.disconfirming_observations,
            "draft disconfirming observations",
            maximum=16,
        )
        if any(type(item) is not MechanismDisconfirmingObservationV1 for item in observations):
            raise _error("draft disconfirming observations are invalid")
        if len({item.observation_id for item in observations}) != len(observations):
            raise _error("draft disconfirming observations must be unique")
        metric_ids = {item.metric_id for item in metrics}
        if any(item.metric_id not in metric_ids for item in observations):
            raise _error("draft disconfirming metric is not declared")
        expected = _bool_tuple(self.expected_changed, "draft expected pattern")
        if len(expected) != len(self.recipe.input_values):
            raise _error("draft expected pattern differs from recipe inputs")
        rivals = _tuple(self.rivals, "draft rivals", nonempty=True, maximum=16)
        if any(type(item) is not RivalPatternV1 for item in rivals):
            raise _error("draft rivals are invalid")
        if len({item.name for item in rivals}) != len(rivals):
            raise _error("draft rivals must have unique names")
        if any(len(item.pattern) != len(expected) for item in rivals):
            raise _error("draft rival pattern differs from expected pattern")
        _identifier(self.configuration_id, "draft configuration ID")
        if self.claim_kind not in {"threshold", "general"}:
            raise _error("draft claim kind is invalid")
        if self.claim_kind == "threshold" and {item.name for item in rivals} != set(STUDY_RIVAL_NAMES_V1):
            raise _error("threshold drafts require inert and always-on rivals")

    def to_primitive(self) -> dict[str, object]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "applicability": self.applicability.to_primitive(),
            "recipe": self.recipe.to_primitive(),
            "metrics": [item.to_primitive() for item in self.metrics],
            "disconfirming_observations": [item.to_primitive() for item in self.disconfirming_observations],
            "expected_changed": list(self.expected_changed),
            "rivals": [item.to_primitive() for item in self.rivals],
            "configuration_id": self.configuration_id,
            "claim_kind": self.claim_kind,
        }

    @classmethod
    def from_primitive(cls, value: object) -> ExperimentDraftV1:
        raw = _exact_mapping(
            value,
            {
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
            },
            "experiment draft",
        )
        cited = raw["cited_evidence_ids"]
        metrics = raw["metrics"]
        observations = raw["disconfirming_observations"]
        expected = raw["expected_changed"]
        rivals = raw["rivals"]
        if not all(type(item) is list for item in (cited, metrics, observations, expected, rivals)):
            raise _error("experiment draft arrays are invalid")
        try:
            return cls(
                hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
                cited_evidence_ids=tuple(cited),  # type: ignore[arg-type]
                applicability=_canonical_mechanism_from_primitive(
                    MechanismPredicateV1, raw["applicability"]
                ),
                recipe=_canonical_mechanism_from_primitive(MechanismRecipeV1, raw["recipe"]),
                metrics=tuple(
                    _canonical_mechanism_from_primitive(MechanismMetricSpecV1, item) for item in metrics
                ),
                disconfirming_observations=tuple(
                    _canonical_mechanism_from_primitive(
                        MechanismDisconfirmingObservationV1, item
                    )
                    for item in observations
                ),
                expected_changed=tuple(expected),  # type: ignore[arg-type]
                rivals=tuple(RivalPatternV1.from_primitive(item) for item in rivals),
                configuration_id=raw["configuration_id"],  # type: ignore[arg-type]
                claim_kind=raw["claim_kind"],  # type: ignore[arg-type]
            )
        except StudyContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise _error("experiment draft is invalid") from exc


def _canonical_mechanism_from_primitive(
    contract_type: type[object], value: object
) -> object:
    try:
        parsed = contract_type.from_primitive(value)  # type: ignore[attr-defined]
        raw_canonical = canonical_json_bytes_v5(value).decode("utf-8")
        if parsed.to_canonical_json() != raw_canonical:  # type: ignore[attr-defined]
            raise _error("nested mechanism encoding is not canonical")
        return parsed
    except StudyContractError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("nested mechanism contract is invalid") from exc


def _metric_prediction_to_primitive(value: MetricPredictionV5) -> dict[str, object]:
    return {
        "metric_id": value.metric_id,
        "direction": value.direction,
        "rationale": value.rationale,
    }


def _hypothesis_to_primitive(value: HypothesisV5) -> dict[str, object]:
    return {
        "hypothesis_id": value.hypothesis_id,
        "rank": value.rank,
        "primary_mechanism": value.primary_mechanism,
        "causal_claim": value.causal_claim,
        "predicted_changes": [_metric_prediction_to_primitive(item) for item in value.predicted_changes],
        "evidence_ids": list(value.evidence_ids),
        "author_instructions": value.author_instructions,
        "authoring_mode": value.authoring_mode,
    }


def _hypothesis_from_primitive(value: object) -> HypothesisV5:
    raw = _exact_mapping(
        value,
        {
            "hypothesis_id",
            "rank",
            "primary_mechanism",
            "causal_claim",
            "predicted_changes",
            "evidence_ids",
            "author_instructions",
            "authoring_mode",
        },
        "study hypothesis",
    )
    predictions = raw["predicted_changes"]
    evidence_ids = raw["evidence_ids"]
    if type(predictions) is not list or type(evidence_ids) is not list:
        raise _error("study hypothesis arrays are invalid")
    parsed_predictions: list[MetricPredictionV5] = []
    for item in predictions:
        prediction = _exact_mapping(item, {"metric_id", "direction", "rationale"}, "study prediction")
        parsed_predictions.append(
            MetricPredictionV5(
                metric_id=prediction["metric_id"],  # type: ignore[arg-type]
                direction=prediction["direction"],  # type: ignore[arg-type]
                rationale=prediction["rationale"],  # type: ignore[arg-type]
            )
        )
    try:
        return HypothesisV5(
            hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
            rank=raw["rank"],  # type: ignore[arg-type]
            primary_mechanism=raw["primary_mechanism"],  # type: ignore[arg-type]
            causal_claim=raw["causal_claim"],  # type: ignore[arg-type]
            predicted_changes=tuple(parsed_predictions),
            evidence_ids=tuple(evidence_ids),  # type: ignore[arg-type]
            author_instructions=raw["author_instructions"],  # type: ignore[arg-type]
            authoring_mode=raw["authoring_mode"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise _error("study hypothesis is invalid") from exc


def _artifact_to_primitive(value: InvestigatorArtifactV5) -> dict[str, object]:
    return {"hypotheses": [_hypothesis_to_primitive(item) for item in value.hypotheses]}


def _artifact_from_primitive(value: object) -> InvestigatorArtifactV5:
    raw = _exact_mapping(value, {"hypotheses"}, "study investigator artifact")
    hypotheses = raw["hypotheses"]
    if type(hypotheses) is not list:
        raise _error("study hypotheses must be an array")
    try:
        return InvestigatorArtifactV5(tuple(_hypothesis_from_primitive(item) for item in hypotheses))
    except (TypeError, ValueError) as exc:
        raise _error("study investigator artifact is invalid") from exc


@dataclass(frozen=True, slots=True)
class StudyResponseV1(_CanonicalStudyV1):
    """The opt-in response envelope around the ordinary investigator artifact."""

    schema_version: Literal[1]
    binding: RoleBindingV5
    artifact: InvestigatorArtifactV5
    drafts: tuple[ExperimentDraftV1, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != STUDY_SCHEMA_VERSION_V1:
            raise _error("study response schema version is invalid")
        if type(self.binding) is not RoleBindingV5:
            raise _error("study response binding is invalid")
        if type(self.artifact) is not InvestigatorArtifactV5:
            raise _error("study response artifact is invalid")
        drafts = _tuple(self.drafts, "study response drafts", nonempty=True, maximum=64)
        if any(type(item) is not ExperimentDraftV1 for item in drafts):
            raise _error("study response drafts are invalid")
        hypothesis_ids = tuple(item.hypothesis_id for item in self.artifact.hypotheses)
        draft_ids = tuple(item.hypothesis_id for item in drafts)
        if draft_ids != hypothesis_ids:
            raise _error("study response must contain one ordered draft per hypothesis")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "binding": canonical_primitive_v5(self.binding),
            "artifact": _artifact_to_primitive(self.artifact),
            "drafts": [item.to_primitive() for item in self.drafts],
        }

    @classmethod
    def from_primitive(cls, value: object) -> StudyResponseV1:
        raw = _exact_mapping(value, {"schema_version", "binding", "artifact", "drafts"}, "study response")
        binding = _decode_binding(raw["binding"])
        drafts = raw["drafts"]
        if type(drafts) is not list:
            raise _error("study response drafts must be an array")
        return cls(
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
            binding=binding,
            artifact=_artifact_from_primitive(raw["artifact"]),
            drafts=tuple(ExperimentDraftV1.from_primitive(item) for item in drafts),
        )


def _decode_binding(value: object) -> RoleBindingV5:
    raw = _exact_mapping(
        value,
        {"parent_revision_sha256", "hypothesis_id", "experiment_ids", "discovery_plan_sha256"},
        "study binding",
    )
    experiments = raw["experiment_ids"]
    if type(experiments) is not list:
        raise _error("study binding experiment IDs must be an array")
    try:
        return RoleBindingV5(
            parent_revision_sha256=raw["parent_revision_sha256"],  # type: ignore[arg-type]
            hypothesis_id=raw["hypothesis_id"],  # type: ignore[arg-type]
            experiment_ids=tuple(experiments),  # type: ignore[arg-type]
            discovery_plan_sha256=raw["discovery_plan_sha256"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise _error("study binding is invalid") from exc


@dataclass(frozen=True, slots=True)
class StudyProviderSettingsV1(_CanonicalStudyV1):
    """Pinned settings that are actually sent to a live provider."""

    provider: str
    model: str
    max_output_tokens: int
    temperature: Decimal | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.provider, "provider")
        _provider_model(self.model)
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise _error("provider output limit is invalid")
        if self.temperature is not None and (
            type(self.temperature) is not Decimal or not self.temperature.is_finite()
        ):
            raise _error("provider temperature is invalid")
        if self.seed is not None and type(self.seed) is not int:
            raise _error("provider seed is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
            "temperature": (
                None if self.temperature is None else canonical_primitive_v5(self.temperature)
            ),
            "seed": self.seed,
        }

    @classmethod
    def from_primitive(cls, value: object) -> StudyProviderSettingsV1:
        raw = _exact_mapping(value, {"provider", "model", "max_output_tokens", "temperature", "seed"}, "provider settings")
        temperature = raw["temperature"]
        if temperature is not None:
            if type(temperature) is not str:
                raise _error("provider temperature must be decimal text")
            try:
                temperature = Decimal(temperature)
            except Exception as exc:
                raise _error("provider temperature is invalid") from exc
            if canonical_primitive_v5(temperature) != raw["temperature"]:
                raise _error("provider temperature is not canonical decimal text")
        return cls(
            provider=raw["provider"],  # type: ignore[arg-type]
            model=raw["model"],  # type: ignore[arg-type]
            max_output_tokens=raw["max_output_tokens"],  # type: ignore[arg-type]
            temperature=temperature,  # type: ignore[arg-type]
            seed=raw["seed"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class StudyOfflineSettingsV1(_CanonicalStudyV1):
    """Explicitly simulated settings used by offline fixture tests."""

    fixture_id: str
    simulated_input_tokens: int = 0
    simulated_output_tokens: int = 0
    simulated_cost_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _identifier(self.fixture_id, "offline fixture ID")
        for value, label in (
            (self.simulated_input_tokens, "simulated input tokens"),
            (self.simulated_output_tokens, "simulated output tokens"),
        ):
            if type(value) is not int or value < 0:
                raise _error(f"{label} is invalid")
        if type(self.simulated_cost_usd) is not Decimal or not self.simulated_cost_usd.is_finite() or self.simulated_cost_usd < 0:
            raise _error("simulated cost is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "simulated_input_tokens": self.simulated_input_tokens,
            "simulated_output_tokens": self.simulated_output_tokens,
            "simulated_cost_usd": canonical_primitive_v5(self.simulated_cost_usd),
        }

    @classmethod
    def from_primitive(cls, value: object) -> StudyOfflineSettingsV1:
        raw = _exact_mapping(
            value,
            {"fixture_id", "simulated_input_tokens", "simulated_output_tokens", "simulated_cost_usd"},
            "offline settings",
        )
        cost = raw["simulated_cost_usd"]
        if type(cost) is not str:
            raise _error("simulated cost must be decimal text")
        try:
            parsed_cost = Decimal(cost)
        except Exception as exc:
            raise _error("simulated cost is invalid") from exc
        if canonical_primitive_v5(parsed_cost) != cost:
            raise _error("simulated cost is not canonical decimal text")
        return cls(
            fixture_id=raw["fixture_id"],  # type: ignore[arg-type]
            simulated_input_tokens=raw["simulated_input_tokens"],  # type: ignore[arg-type]
            simulated_output_tokens=raw["simulated_output_tokens"],  # type: ignore[arg-type]
            simulated_cost_usd=parsed_cost,
        )


@dataclass(frozen=True, slots=True)
class StudyManifestV1(_CanonicalStudyV1):
    """Create-only study authority frozen before either arm attempts a call."""

    schema_version: Literal[1]
    study_id: str
    mode: StudyModeV1
    source_revision: str
    fixture_sha256: str
    registry_sha256: str
    rubric_sha256: str
    schema_sha256: str
    parser_sha256: str
    prompt_sha256: str
    round_one_checkpoint_ref: ArtifactRefV5
    round_one_snapshot_ref: ArtifactRefV5
    primary_preflight_ref: ArtifactRefV5
    withheld_preflight_ref: ArtifactRefV5
    resource_limits: MechanismResourceBudgetV1
    provider_settings: StudyProviderSettingsV1 | None
    offline_settings: StudyOfflineSettingsV1 | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != STUDY_SCHEMA_VERSION_V1:
            raise _error("study manifest schema version is invalid")
        _identifier(self.study_id, "study ID")
        if self.mode not in {"offline_fixture", "live_study"}:
            raise _error("study mode is invalid")
        _source_revision(self.source_revision)
        for value, label in (
            (self.fixture_sha256, "fixture hash"),
            (self.registry_sha256, "registry hash"),
            (self.rubric_sha256, "rubric hash"),
            (self.schema_sha256, "schema hash"),
            (self.parser_sha256, "parser hash"),
            (self.prompt_sha256, "prompt hash"),
        ):
            _digest(value, label)
        for value, label in (
            (self.round_one_checkpoint_ref, "round-one checkpoint ref"),
            (self.round_one_snapshot_ref, "round-one snapshot ref"),
            (self.primary_preflight_ref, "primary preflight ref"),
            (self.withheld_preflight_ref, "withheld preflight ref"),
        ):
            if type(value) is not ArtifactRefV5:
                raise _error(f"{label} is invalid")
        if type(self.resource_limits) is not MechanismResourceBudgetV1:
            raise _error("study resource limits are invalid")
        if self.mode == "live_study":
            if type(self.provider_settings) is not StudyProviderSettingsV1 or self.offline_settings is not None:
                raise _error("live study settings are invalid")
        elif type(self.offline_settings) is not StudyOfflineSettingsV1 or self.provider_settings is not None:
            raise _error("offline study settings are invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "study_id": self.study_id,
            "mode": self.mode,
            "source_revision": self.source_revision,
            "fixture_sha256": self.fixture_sha256,
            "registry_sha256": self.registry_sha256,
            "rubric_sha256": self.rubric_sha256,
            "schema_sha256": self.schema_sha256,
            "parser_sha256": self.parser_sha256,
            "prompt_sha256": self.prompt_sha256,
            "round_one_checkpoint_ref": self.round_one_checkpoint_ref.to_primitive(),
            "round_one_snapshot_ref": self.round_one_snapshot_ref.to_primitive(),
            "primary_preflight_ref": self.primary_preflight_ref.to_primitive(),
            "withheld_preflight_ref": self.withheld_preflight_ref.to_primitive(),
            "resource_limits": self.resource_limits.to_primitive(),
            "provider_settings": (
                None if self.provider_settings is None else self.provider_settings.to_primitive()
            ),
            "offline_settings": None if self.offline_settings is None else self.offline_settings.to_primitive(),
        }

    @classmethod
    def from_primitive(cls, value: object) -> StudyManifestV1:
        raw = _exact_mapping(
            value,
            {
                "schema_version",
                "study_id",
                "mode",
                "source_revision",
                "fixture_sha256",
                "registry_sha256",
                "rubric_sha256",
                "schema_sha256",
                "parser_sha256",
                "prompt_sha256",
                "round_one_checkpoint_ref",
                "round_one_snapshot_ref",
                "primary_preflight_ref",
                "withheld_preflight_ref",
                "resource_limits",
                "provider_settings",
                "offline_settings",
            },
            "study manifest",
        )
        try:
            refs = []
            for name in (
                "round_one_checkpoint_ref",
                "round_one_snapshot_ref",
                "primary_preflight_ref",
                "withheld_preflight_ref",
            ):
                ref_raw = _exact_mapping(raw[name], {"relative_path", "sha256"}, name)
                refs.append(ArtifactRefV5(ref_raw["relative_path"], ref_raw["sha256"]))  # type: ignore[arg-type]
            resource_limits = MechanismResourceBudgetV1.from_primitive(raw["resource_limits"])
            if resource_limits.to_canonical_json() != canonical_json_bytes_v5(raw["resource_limits"]).decode("utf-8"):
                raise _error("study resource limits are not canonical")
            return cls(
                schema_version=raw["schema_version"],  # type: ignore[arg-type]
                study_id=raw["study_id"],  # type: ignore[arg-type]
                mode=raw["mode"],  # type: ignore[arg-type]
                source_revision=raw["source_revision"],  # type: ignore[arg-type]
                fixture_sha256=raw["fixture_sha256"],  # type: ignore[arg-type]
                registry_sha256=raw["registry_sha256"],  # type: ignore[arg-type]
                rubric_sha256=raw["rubric_sha256"],  # type: ignore[arg-type]
                schema_sha256=raw["schema_sha256"],  # type: ignore[arg-type]
                parser_sha256=raw["parser_sha256"],  # type: ignore[arg-type]
                prompt_sha256=raw["prompt_sha256"],  # type: ignore[arg-type]
                round_one_checkpoint_ref=refs[0],
                round_one_snapshot_ref=refs[1],
                primary_preflight_ref=refs[2],
                withheld_preflight_ref=refs[3],
                resource_limits=resource_limits,
                provider_settings=(
                    None
                    if raw["provider_settings"] is None
                    else StudyProviderSettingsV1.from_primitive(raw["provider_settings"])
                ),
                offline_settings=(
                    None
                    if raw["offline_settings"] is None
                    else StudyOfflineSettingsV1.from_primitive(raw["offline_settings"])
                ),
            )
        except StudyContractError:
            raise
        except (TypeError, ValueError, UnicodeError) as exc:
            raise _error("study manifest is invalid") from exc


@dataclass(frozen=True, slots=True)
class ArtifactBackedReasonV1(_CanonicalStudyV1):
    """A human-readable gate reason that points to immutable evidence bytes."""

    gate: str
    reason: str
    artifact_refs: tuple[ArtifactRefV5, ...]

    def __post_init__(self) -> None:
        _identifier(self.gate, "verdict reason gate")
        _text(self.reason, "verdict reason")
        refs = _tuple(self.artifact_refs, "verdict reason refs", nonempty=True, maximum=32)
        if any(type(item) is not ArtifactRefV5 for item in refs):
            raise _error("verdict reason refs are invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "gate": self.gate,
            "reason": self.reason,
            "artifact_refs": [item.to_primitive() for item in self.artifact_refs],
        }

    @classmethod
    def from_primitive(cls, value: object) -> ArtifactBackedReasonV1:
        raw = _exact_mapping(value, {"gate", "reason", "artifact_refs"}, "verdict reason")
        refs = raw["artifact_refs"]
        if type(refs) is not list:
            raise _error("verdict reason refs must be an array")
        try:
            parsed = []
            for item in refs:
                ref = _exact_mapping(item, {"relative_path", "sha256"}, "verdict reason ref")
                parsed.append(ArtifactRefV5(ref["relative_path"], ref["sha256"]))  # type: ignore[arg-type]
            return cls(gate=raw["gate"], reason=raw["reason"], artifact_refs=tuple(parsed))  # type: ignore[arg-type]
        except StudyContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise _error("verdict reason is invalid") from exc


StudyVerdictReasonV1 = ArtifactBackedReasonV1


@dataclass(frozen=True, slots=True)
class StudyVerdictsV1(_CanonicalStudyV1):
    """Independent gate results; aggregate support never implies other gates."""

    schema_version: Literal[1]
    trace_integrity: StudyGateV1
    evidence_delivery: StudyGateV1
    evidence_use: StudyEvidenceUseV1
    production_assessment: StudyProductionAssessmentV1
    case_contrast: StudyCaseContrastV1
    experiment_completion: StudyGateV1
    feedback_attribution: StudyFeedbackAttributionV1
    optimization_improvement: Literal["not_established"]
    authored_code_execution: Literal["not_established"]
    reasons: tuple[ArtifactBackedReasonV1, ...]

    @property
    def artifact_backed_reasons(self) -> tuple[ArtifactBackedReasonV1, ...]:
        """Descriptive alias used by readers of the rubric export."""

        return self.reasons

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != STUDY_SCHEMA_VERSION_V1:
            raise _error("study verdicts schema version is invalid")
        for value, label in (
            (self.trace_integrity, "trace integrity"),
            (self.evidence_delivery, "evidence delivery"),
            (self.experiment_completion, "experiment completion"),
        ):
            if value not in {"verified", "failed", "incomplete", "not_assessed"}:
                raise _error(f"{label} verdict is invalid")
        if self.evidence_use not in {"not_assessed", "supported", "not_supported", "inconclusive"}:
            raise _error("evidence use verdict is invalid")
        if self.production_assessment not in {
            "supported_on_cases",
            "contradicted_on_cases",
            "insufficient_evidence",
        }:
            raise _error("production assessment verdict is invalid")
        if self.case_contrast not in {"matched_on_cases", "contradicted_on_cases", "unavailable"}:
            raise _error("case contrast verdict is invalid")
        if self.feedback_attribution not in {"supported", "inconclusive", "not_assessed"}:
            raise _error("feedback attribution verdict is invalid")
        if self.optimization_improvement != "not_established" or self.authored_code_execution != "not_established":
            raise _error("V1 optimization and authored-code verdicts are not established")
        reasons = _tuple(self.reasons, "verdict reasons", maximum=128)
        if any(type(item) is not ArtifactBackedReasonV1 for item in reasons):
            raise _error("verdict reasons are invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "trace_integrity": self.trace_integrity,
            "evidence_delivery": self.evidence_delivery,
            "evidence_use": self.evidence_use,
            "production_assessment": self.production_assessment,
            "case_contrast": self.case_contrast,
            "experiment_completion": self.experiment_completion,
            "feedback_attribution": self.feedback_attribution,
            "optimization_improvement": self.optimization_improvement,
            "authored_code_execution": self.authored_code_execution,
            "reasons": [item.to_primitive() for item in self.reasons],
        }

    @classmethod
    def from_primitive(cls, value: object) -> StudyVerdictsV1:
        raw = _exact_mapping(
            value,
            {
                "schema_version",
                "trace_integrity",
                "evidence_delivery",
                "evidence_use",
                "production_assessment",
                "case_contrast",
                "experiment_completion",
                "feedback_attribution",
                "optimization_improvement",
                "authored_code_execution",
                "reasons",
            },
            "study verdicts",
        )
        reasons = raw["reasons"]
        if type(reasons) is not list:
            raise _error("verdict reasons must be an array")
        return cls(
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
            trace_integrity=raw["trace_integrity"],  # type: ignore[arg-type]
            evidence_delivery=raw["evidence_delivery"],  # type: ignore[arg-type]
            evidence_use=raw["evidence_use"],  # type: ignore[arg-type]
            production_assessment=raw["production_assessment"],  # type: ignore[arg-type]
            case_contrast=raw["case_contrast"],  # type: ignore[arg-type]
            experiment_completion=raw["experiment_completion"],  # type: ignore[arg-type]
            feedback_attribution=raw["feedback_attribution"],  # type: ignore[arg-type]
            optimization_improvement=raw["optimization_improvement"],  # type: ignore[arg-type]
            authored_code_execution=raw["authored_code_execution"],  # type: ignore[arg-type]
            reasons=tuple(ArtifactBackedReasonV1.from_primitive(item) for item in reasons),
        )


STUDY_CONTRACT_TYPES_V1: tuple[type[object], ...] = (
    RivalPatternV1,
    ExperimentDraftV1,
    StudyResponseV1,
    StudyProviderSettingsV1,
    StudyOfflineSettingsV1,
    StudyManifestV1,
    ArtifactBackedReasonV1,
    StudyVerdictsV1,
)


def study_contract_bytes_v1(value: object) -> bytes:
    """Encode only explicitly registered V1 contract values."""

    if type(value) not in STUDY_CONTRACT_TYPES_V1:
        raise StudyContractError("value is not an explicitly registered study contract")
    contract = value
    return contract.canonical_bytes()  # type: ignore[union-attr]


__all__ = [
    "ArtifactBackedReasonV1",
    "ExperimentDraftV1",
    "RivalPatternV1",
    "STUDY_CONTRACT_TYPES_V1",
    "STUDY_MAX_ID_BYTES_V1",
    "STUDY_RIVAL_NAMES_V1",
    "STUDY_SCHEMA_VERSION_V1",
    "StudyAdmissionError",
    "StudyArmV1",
    "StudyAuthorityError",
    "StudyCaseContrastV1",
    "StudyContractError",
    "StudyEvidenceUseV1",
    "StudyFeedbackAttributionV1",
    "StudyGateV1",
    "StudyManifestV1",
    "StudyModeV1",
    "StudyPendingAccounting",
    "StudyPrecommitmentError",
    "StudyProductionAssessmentV1",
    "StudyProviderSettingsV1",
    "StudyOfflineSettingsV1",
    "StudyResponseV1",
    "StudyVerdictReasonV1",
    "StudyVerdictsV1",
    "study_contract_bytes_v1",
]
