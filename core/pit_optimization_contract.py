"""Closed contracts for one bounded point-in-time optimization cycle."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


ENTRY_CONTRACT_PATH = "core/canslim/entry_contract.py"
FULL_START_DATE = "2021-01-01"
FULL_END_DATE = "2025-12-31"
HOLDOUT_START_DATE = "2025-01-01"
HOLDOUT_END_DATE = FULL_END_DATE
PIT_BUNDLE_SHA256 = "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb"
BASELINE_MANIFEST_SHA256 = "f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382"
BASELINE_SOURCE_COMMIT = "515cb1e50d051e2ee4253603608f2fd3920004bc"
MAX_CANARY_CALLS = 3
MAX_CANARY_USD = 0.50

_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ROLE_DOMAINS = frozenset(
    {"entry_funnel", "return_drawdown", "cash_exposure", "trade_quality"}
)

PIT_OPTIMIZATION_SYSTEM_PROMPTS = MappingProxyType(
    {
        "orchestrator": (
            "You are the PIT Optimization Orchestrator. Route only. Return exactly one JSON "
            "object with exactly action, domain, and evidence_ids. action is continue or abort. "
            "For continue, choose one supplied domain and cite sorted unique supplied evidence "
            "IDs for that domain. For abort, domain is empty and evidence_ids is empty. Do not "
            "select or name a candidate, parameter, value, file, edit, command, external fact, "
            "or reasoning. Return JSON only."
        ),
        "reasoner": (
            "You are the PIT Optimization Reasoner. Return exactly one JSON object with exactly "
            "hypothesis, evidence_ids, invariant_ids, candidate_id, skip, and skip_reason. Use "
            "only the supplied aggregate metrics and closed IDs. For skip=false, choose exactly "
            "one supplied candidate_id, cite sorted unique supplied evidence and invariant IDs, "
            "and leave skip_reason empty. For skip=true, candidate_id is empty and skip_reason is "
            "nonempty. Do not invent a value, file, replacement, source fact, external knowledge, "
            "retrieval, command, or chain-of-thought. Return JSON only."
        ),
        "coder": (
            "You are the PIT Optimization Coder. Reproduce only the controller-owned selection. "
            "Return exactly one JSON object with exactly summary, candidate_id, and replacement. "
            "replacement has exactly path, old_line, and new_line and must byte-for-byte match the "
            "supplied controller replacement. Do not choose a candidate, value, file, alternative "
            "edit, command, external fact, retrieval, diff, or chain-of-thought. Return JSON only."
        ),
    }
)


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _closed_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be canonical text")
    if not allow_empty and not value:
        raise ValueError(f"{field} cannot be empty")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{field} is too large")
    return value


def _closed_ids(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    normalized = tuple(value)
    if normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field} must be canonically sorted")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must contain unique IDs")
    if not allow_empty and not normalized:
        raise ValueError(f"{field} cannot be empty")
    if any(_ID_RE.fullmatch(item) is None for item in normalized):
        raise ValueError(f"{field} contains an invalid ID")
    return normalized


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("provider JSON contains a duplicate key")
        result[key] = value
    return result


def _parse_closed_object(raw: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32 * 1024:
        raise ValueError("provider payload is not bounded JSON text")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("provider payload is malformed JSON") from exc
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("provider payload has invalid keys")
    return value


@dataclass(frozen=True, slots=True)
class CandidateDefinition:
    """One controller-owned one-line entry-contract alternative."""

    candidate_id: str
    constant_name: str
    policy_field: str
    old_value: float
    new_value: float
    old_line: str
    new_line: str
    path: str = ENTRY_CONTRACT_PATH

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.candidate_id) is None:
            raise ValueError("candidate ID is invalid")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", self.constant_name) is None:
            raise ValueError("candidate constant is invalid")
        if _ID_RE.fullmatch(self.policy_field) is None:
            raise ValueError("candidate policy field is invalid")
        old_value = _finite_number(self.old_value, "candidate old value")
        new_value = _finite_number(self.new_value, "candidate new value")
        if math.isclose(old_value, new_value, rel_tol=0.0, abs_tol=0.0):
            raise ValueError("candidate replacement cannot be a no-op")
        expected_prefix = f"{self.constant_name} = "
        if (
            self.path != ENTRY_CONTRACT_PATH
            or not self.old_line.startswith(expected_prefix)
            or not self.new_line.startswith(expected_prefix)
            or "\n" in self.old_line
            or "\n" in self.new_line
        ):
            raise ValueError("candidate replacement is outside the one-line contract")


def _candidate(
    candidate_id: str,
    constant_name: str,
    policy_field: str,
    old_text: str,
    new_text: str,
) -> CandidateDefinition:
    return CandidateDefinition(
        candidate_id=candidate_id,
        constant_name=constant_name,
        policy_field=policy_field,
        old_value=float(old_text),
        new_value=float(new_text),
        old_line=f"{constant_name} = {old_text}",
        new_line=f"{constant_name} = {new_text}",
    )


_CATALOG = MappingProxyType(
    {
        item.candidate_id: item
        for item in sorted(
            (
                _candidate("min_current_growth_020", "MIN_CURRENT_GROWTH", "min_current_growth", "0.25", "0.20"),
                _candidate("min_current_growth_030", "MIN_CURRENT_GROWTH", "min_current_growth", "0.25", "0.30"),
                _candidate("min_annual_growth_020", "MIN_ANNUAL_GROWTH", "min_annual_growth", "0.25", "0.20"),
                _candidate("min_annual_growth_030", "MIN_ANNUAL_GROWTH", "min_annual_growth", "0.25", "0.30"),
                _candidate("min_rs_score_075", "MIN_RS_SCORE", "min_rs_score", "80.0", "75.0"),
                _candidate("min_rs_score_085", "MIN_RS_SCORE", "min_rs_score", "80.0", "85.0"),
                _candidate("min_composite_score_065", "MIN_COMPOSITE_SCORE", "min_entry_composite_score", "70.0", "65.0"),
                _candidate("min_composite_score_075", "MIN_COMPOSITE_SCORE", "min_entry_composite_score", "70.0", "75.0"),
                _candidate("min_volume_ratio_120", "MIN_VOLUME_RATIO", "min_volume_ratio", "1.30", "1.20"),
                _candidate("min_volume_ratio_140", "MIN_VOLUME_RATIO", "min_volume_ratio", "1.30", "1.40"),
                _candidate("max_buy_zone_extension_003", "MAX_BUY_ZONE_EXTENSION", "max_buy_zone_extension", "0.05", "0.03"),
                _candidate("max_buy_zone_extension_007", "MAX_BUY_ZONE_EXTENSION", "max_buy_zone_extension", "0.05", "0.07"),
            ),
            key=lambda candidate: candidate.candidate_id,
        )
    }
)


def candidate_catalog() -> Mapping[str, CandidateDefinition]:
    """Return the immutable, canonically ordered 12-candidate catalog."""

    return _CATALOG


@dataclass(frozen=True, slots=True)
class CatalogSourceIdentity:
    source_sha256: str
    candidate_count: int
    constant_count: int


def verify_catalog_source(path: Path) -> CatalogSourceIdentity:
    """Bind every catalog replacement to one exact live source line."""

    source = Path(path)
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or source.is_symlink():
        raise ValueError("entry-contract source must be a regular non-link file")
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    old_lines = {candidate.old_line for candidate in _CATALOG.values()}
    if any(text.splitlines().count(line) != 1 for line in old_lines):
        raise ValueError("entry-contract source differs from the approved candidate catalog")
    if any(candidate.new_line in text.splitlines() for candidate in _CATALOG.values()):
        raise ValueError("entry-contract source already contains a candidate alternative")
    return CatalogSourceIdentity(
        source_sha256=hashlib.sha256(raw).hexdigest(),
        candidate_count=len(_CATALOG),
        constant_count=len(old_lines),
    )


@dataclass(frozen=True, slots=True)
class PitOptimizationRoute:
    action: str
    domain: str
    evidence_ids: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: str) -> "PitOptimizationRoute":
        value = _parse_closed_object(raw, frozenset({"action", "domain", "evidence_ids"}))
        action = _closed_text(value["action"], "route action")
        domain = _closed_text(value["domain"], "route domain", allow_empty=action == "abort")
        if action not in {"continue", "abort"}:
            raise ValueError("route action is invalid")
        if action == "continue" and domain not in _ROLE_DOMAINS:
            raise ValueError("route domain is invalid")
        if action == "abort" and domain:
            raise ValueError("abort route domain must be empty")
        evidence_ids = _closed_ids(
            value["evidence_ids"], "route evidence IDs", allow_empty=action == "abort"
        )
        if action == "abort" and evidence_ids:
            raise ValueError("abort route cannot cite evidence")
        return cls(action, domain, evidence_ids)


@dataclass(frozen=True, slots=True)
class PitOptimizationReasoning:
    hypothesis: str
    evidence_ids: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    candidate_id: str
    skip: bool
    skip_reason: str

    @classmethod
    def from_json(cls, raw: str) -> "PitOptimizationReasoning":
        value = _parse_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis",
                    "evidence_ids",
                    "invariant_ids",
                    "candidate_id",
                    "skip",
                    "skip_reason",
                }
            ),
        )
        if type(value["skip"]) is not bool:
            raise ValueError("reasoner skip must be boolean")
        skip = value["skip"]
        hypothesis = _closed_text(value["hypothesis"], "reasoner hypothesis")
        candidate_id = _closed_text(
            value["candidate_id"], "reasoner candidate ID", allow_empty=skip
        )
        skip_reason = _closed_text(
            value["skip_reason"], "reasoner skip reason", allow_empty=not skip
        )
        evidence_ids = _closed_ids(
            value["evidence_ids"], "reasoner evidence IDs", allow_empty=skip
        )
        invariant_ids = _closed_ids(
            value["invariant_ids"], "reasoner invariant IDs", allow_empty=skip
        )
        if skip:
            raise ValueError("reasoner must choose exactly one catalog candidate")
        if candidate_id not in _CATALOG or skip_reason:
            raise ValueError("reasoner must choose exactly one catalog candidate")
        return cls(
            hypothesis,
            evidence_ids,
            invariant_ids,
            candidate_id,
            skip,
            skip_reason,
        )


@dataclass(frozen=True, slots=True)
class PitOptimizationCoding:
    summary: str
    candidate_id: str
    path: str
    old_line: str
    new_line: str

    @classmethod
    def from_json(cls, raw: str) -> "PitOptimizationCoding":
        value = _parse_closed_object(
            raw, frozenset({"summary", "candidate_id", "replacement"})
        )
        replacement = value["replacement"]
        if not isinstance(replacement, dict) or set(replacement) != {
            "path",
            "old_line",
            "new_line",
        }:
            raise ValueError("coder replacement has invalid keys")
        return cls(
            summary=_closed_text(value["summary"], "coder summary"),
            candidate_id=_closed_text(value["candidate_id"], "coder candidate ID"),
            path=_closed_text(replacement["path"], "coder path"),
            old_line=_closed_text(replacement["old_line"], "coder old line"),
            new_line=_closed_text(replacement["new_line"], "coder new line"),
        )


def validate_coding_selection(
    coding: PitOptimizationCoding, candidate: CandidateDefinition
) -> None:
    if not isinstance(coding, PitOptimizationCoding) or not isinstance(
        candidate, CandidateDefinition
    ):
        raise ValueError("coder replacement validation requires closed types")
    if (
        coding.candidate_id,
        coding.path,
        coding.old_line,
        coding.new_line,
    ) != (
        candidate.candidate_id,
        candidate.path,
        candidate.old_line,
        candidate.new_line,
    ):
        raise ValueError("coder replacement differs from the controller selection")


@dataclass(frozen=True, slots=True)
class OptimizationWindowMetrics:
    total_return_pct: float
    annualized_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    closed_trades: int

    def __post_init__(self) -> None:
        for field in (
            "total_return_pct",
            "annualized_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
        ):
            object.__setattr__(self, field, _finite_number(getattr(self, field), field))
        if type(self.closed_trades) is not int or self.closed_trades < 0:
            raise ValueError("closed_trades must be a nonnegative integer")

    @property
    def objective(self) -> float:
        return self.annualized_return_pct - abs(min(self.max_drawdown_pct, 0.0))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "OptimizationWindowMetrics":
        required = {
            "total_return_pct",
            "annualized_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "closed_trades",
        }
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise ValueError("window metrics are incomplete")
        return cls(**{field: value[field] for field in required})


@dataclass(frozen=True, slots=True)
class OptimizationObservation:
    """One aggregate-only full/holdout observation safe for provider projection."""

    full: Mapping[str, object]
    holdout: Mapping[str, object]
    leader_basket: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.full, Mapping) or not isinstance(self.holdout, Mapping):
            raise ValueError("optimization observation windows must be mappings")
        if self.leader_basket is not None and not isinstance(self.leader_basket, Mapping):
            raise ValueError("optimization leader basket must be a mapping")
        primitive = self.to_primitive()
        try:
            encoded = json.dumps(
                primitive,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("optimization observation is not finite JSON") from exc
        if len(encoded) > 256 * 1024:
            raise ValueError("optimization observation exceeds the aggregate boundary")

    def to_primitive(self) -> dict[str, object]:
        return {
            "full": json.loads(json.dumps(dict(self.full), allow_nan=False)),
            "holdout": json.loads(json.dumps(dict(self.holdout), allow_nan=False)),
            "leader_basket": (
                None
                if self.leader_basket is None
                else json.loads(json.dumps(dict(self.leader_basket), allow_nan=False))
            ),
        }


@dataclass(frozen=True, slots=True)
class OptimizationComparison:
    full_objective_delta: float
    holdout_objective_delta: float
    full_checks: Mapping[str, bool]
    holdout_checks: Mapping[str, bool]
    holdout_minimum_closed_trades: int
    accepted: bool


def build_comparison(
    *,
    baseline_full: OptimizationWindowMetrics,
    candidate_full: OptimizationWindowMetrics,
    baseline_holdout: OptimizationWindowMetrics,
    candidate_holdout: OptimizationWindowMetrics,
) -> OptimizationComparison:
    """Apply the fixed deterministic promotion contract to both windows."""

    if not all(
        isinstance(item, OptimizationWindowMetrics)
        for item in (baseline_full, candidate_full, baseline_holdout, candidate_holdout)
    ):
        raise ValueError("comparison requires closed window metrics")
    epsilon = 1e-12
    full_delta = candidate_full.objective - baseline_full.objective
    holdout_delta = candidate_holdout.objective - baseline_holdout.objective
    full_checks = {
        "objective_improvement_at_least_0_25pp": full_delta + epsilon >= 0.25,
        "total_return_not_worse_by_more_than_0_50pp": (
            candidate_full.total_return_pct + epsilon >= baseline_full.total_return_pct - 0.50
        ),
        "drawdown_not_worse_by_more_than_0_50pp": (
            abs(min(candidate_full.max_drawdown_pct, 0.0))
            <= abs(min(baseline_full.max_drawdown_pct, 0.0)) + 0.50 + epsilon
        ),
        "sharpe_not_worse_by_more_than_0_05": (
            candidate_full.sharpe_ratio + epsilon >= baseline_full.sharpe_ratio - 0.05
        ),
        "closed_trades_at_least_132": candidate_full.closed_trades >= 132,
    }
    minimum_holdout_trades = max(5, math.floor(0.5 * baseline_holdout.closed_trades))
    holdout_checks = {
        "objective_delta_nonnegative": holdout_delta + epsilon >= 0.0,
        "total_return_not_worse_by_more_than_0_50pp": (
            candidate_holdout.total_return_pct + epsilon
            >= baseline_holdout.total_return_pct - 0.50
        ),
        "drawdown_not_worse_by_more_than_0_50pp": (
            abs(min(candidate_holdout.max_drawdown_pct, 0.0))
            <= abs(min(baseline_holdout.max_drawdown_pct, 0.0)) + 0.50 + epsilon
        ),
        "sharpe_not_worse_by_more_than_0_05": (
            candidate_holdout.sharpe_ratio + epsilon
            >= baseline_holdout.sharpe_ratio - 0.05
        ),
        "closed_trades_at_least_half_baseline_floor": (
            candidate_holdout.closed_trades >= minimum_holdout_trades
        ),
    }
    return OptimizationComparison(
        full_objective_delta=full_delta,
        holdout_objective_delta=holdout_delta,
        full_checks=MappingProxyType(full_checks),
        holdout_checks=MappingProxyType(holdout_checks),
        holdout_minimum_closed_trades=minimum_holdout_trades,
        accepted=all(full_checks.values()) and all(holdout_checks.values()),
    )


@dataclass(frozen=True, slots=True)
class PolicyDelta:
    changed_leaf: str
    old_value: float
    new_value: float


def _policy_entry_projection(policy: Mapping[str, object]) -> Mapping[str, object]:
    entry = policy.get("entry_policy")
    if not isinstance(entry, Mapping):
        raise ValueError("effective policy lacks entry_policy")
    return entry


def validate_policy_delta(
    baseline: Mapping[str, object],
    candidate_policy: Mapping[str, object],
    candidate: CandidateDefinition,
) -> PolicyDelta:
    """Require one canonical entry-policy value delta and stable causal invariants.

    Compatibility aliases elsewhere in the complete policy may mirror an entry constant.
    The canonical optimizer projection is ``entry_policy``; it must have exactly one changed
    leaf, while the complete causal-invariant section must remain byte-equivalent.
    """

    if not isinstance(baseline, Mapping) or not isinstance(candidate_policy, Mapping):
        raise ValueError("effective policy delta requires mappings")
    if baseline.get("causal_invariants") != candidate_policy.get("causal_invariants"):
        raise ValueError("causal invariants changed during candidate evaluation")
    if set(baseline) != set(candidate_policy):
        raise ValueError("candidate effective policy shape changed")
    baseline_entry = _policy_entry_projection(baseline)
    changed_entry = _policy_entry_projection(candidate_policy)
    if set(baseline_entry) != set(changed_entry):
        raise ValueError("candidate entry-policy shape changed")
    changed_leaves: list[str] = []
    for field in sorted(baseline_entry):
        before = baseline_entry[field]
        after = changed_entry[field]
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise ValueError("entry-policy field is malformed")
        if set(before) != set(after):
            raise ValueError("candidate entry-policy field shape changed")
        for key in sorted(before):
            if before[key] != after[key]:
                changed_leaves.append(f"entry_policy.{field}.{key}")
    expected_leaf = f"entry_policy.{candidate.policy_field}.value"
    if changed_leaves != [expected_leaf]:
        raise ValueError("candidate did not produce exactly one canonical policy leaf delta")

    def leaves(value: object, prefix: str = "") -> dict[str, object]:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key in sorted(value):
                child = f"{prefix}.{key}" if prefix else str(key)
                result.update(leaves(value[key], child))
            return result
        if isinstance(value, list):
            result = {}
            for index, item in enumerate(value):
                result.update(leaves(item, f"{prefix}[{index}]"))
            return result
        return {prefix: value}

    before_leaves = leaves(baseline)
    after_leaves = leaves(candidate_policy)
    if set(before_leaves) != set(after_leaves):
        raise ValueError("candidate effective policy leaf set changed")
    complete_changes = {
        key for key in before_leaves if before_leaves[key] != after_leaves[key]
    }
    allowed_changes = {expected_leaf}
    alias_fields = {
        "min_current_growth": "min_c_a_growth",
        "min_annual_growth": "min_c_a_growth",
        "min_rs_score": "min_rs_score",
        "min_entry_composite_score": "min_canslim_score",
    }
    alias = alias_fields.get(candidate.policy_field)
    if alias is not None:
        alias_leaf = f"unsupported_requests.{alias}.value"
        if alias_leaf in before_leaves:
            allowed_changes.add(alias_leaf)
    if expected_leaf not in complete_changes or not complete_changes.issubset(allowed_changes):
        raise ValueError("effective policy changed outside the selected candidate semantics")
    before_field = baseline_entry[candidate.policy_field]
    after_field = changed_entry[candidate.policy_field]
    assert isinstance(before_field, Mapping) and isinstance(after_field, Mapping)
    if (
        before_field.get("classification") != "active_fixed_policy"
        or before_field.get("optimizer_candidate") is not True
        or before_field.get("source")
        != f"core.canslim.entry_contract.{candidate.constant_name}"
    ):
        raise ValueError("baseline policy does not authorize the selected candidate")
    old_value = _finite_number(before_field.get("value"), "baseline policy value")
    new_value = _finite_number(after_field.get("value"), "candidate policy value")
    if not math.isclose(old_value, candidate.old_value, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("baseline policy value differs from candidate catalog")
    if not math.isclose(new_value, candidate.new_value, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("candidate policy value differs from candidate catalog")
    return PolicyDelta(expected_leaf, old_value, new_value)


_RESPONSE_SCHEMAS = MappingProxyType(
    {
        "orchestrator": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "domain", "evidence_ids"],
            "properties": {
                "action": {"type": "string", "enum": ["continue", "abort"]},
                "domain": {"type": "string", "enum": ["", *sorted(_ROLE_DOMAINS)]},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
        "reasoner": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis",
                "evidence_ids",
                "invariant_ids",
                "candidate_id",
                "skip",
                "skip_reason",
            ],
            "properties": {
                "hypothesis": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "invariant_ids": {"type": "array", "items": {"type": "string"}},
                "candidate_id": {"type": "string", "enum": ["", *tuple(_CATALOG)]},
                "skip": {"type": "boolean"},
                "skip_reason": {"type": "string"},
            },
        },
        "coder": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "candidate_id", "replacement"],
            "properties": {
                "summary": {"type": "string"},
                "candidate_id": {"type": "string", "enum": list(_CATALOG)},
                "replacement": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "old_line", "new_line"],
                    "properties": {
                        "path": {"type": "string", "enum": [ENTRY_CONTRACT_PATH]},
                        "old_line": {"type": "string"},
                        "new_line": {"type": "string"},
                    },
                },
            },
        },
    }
)


def pit_optimization_response_format(role: str) -> dict[str, object]:
    try:
        schema = _RESPONSE_SCHEMAS[role]
    except KeyError as exc:
        raise ValueError("unknown PIT optimization role") from exc
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"pit_optimization_{role}_v1",
            "strict": True,
            "schema": json.loads(json.dumps(schema, separators=(",", ":"))),
        },
    }
