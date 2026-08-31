"""Closed contracts for one bounded point-in-time optimization cycle."""

from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
import stat
import subprocess
import uuid
from dataclasses import InitVar, dataclass, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from core.engine_policy import effective_engine_policy_sha256
from core.pit_optimizer_command import (
    authenticated_python_executable,
    render_pit_optimizer_v3_command,
)
from core.pit_optimizer_evaluation import (
    AggregateMetric,
    DiscoveryExposureProof,
    DiscoveryScore,
    FoldAggregateSummary,
    FoldManifest,
    ValidationExposureMetadata,
    ValidationWindowIdentity,
    discovery_score_from_folds,
)


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

OPTIMIZER_V2_ROLES = ("investigator", "author", "critic")
# Content-free validation stages which may be retained in schema-v3 audit
# records.  These classify a locally rejected response without retaining any
# provider text, prompt text, or parser exception detail.
PIT_OPTIMIZER_RESPONSE_VALIDATION_CODES = frozenset(
    {
        "response_semantics_invalid",
        "refusal",
        "content_shape_invalid",
        "payload_schema_invalid",
        "payload_json_invalid",
        "payload_keys_invalid",
        "payload_field_invalid",
        "payload_scope_invalid",
        "payload_size_invalid",
        "payload_enum_invalid",
        "payload_binding_invalid",
        "model_mismatch",
        "validator_boundary_invalid",
    }
)
PIT_OPTIMIZER_R1_MODEL = "deepseek/deepseek-r1"
MAX_ROLE_TEXT_BYTES = 4 * 1024
MAX_ROLE_LIST_ITEMS = 16
# Provider-facing investigator output is intentionally smaller than the broad
# controller-context limits above.  Those bounds are sized for authenticated
# local artifacts; model output must fit the investigator's 8 KiB artifact
# envelope before it reaches the local parser.
MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS = 4
MAX_INVESTIGATOR_RATIONALE_CHARS = 256
MAX_INVESTIGATOR_LIST_ITEM_CHARS = 96
MAX_AUTHOR_DIFF_BYTES = 64 * 1024
MAX_POLICY_SOURCE_BUNDLE_BYTES = 64 * 1024
MAX_DISCOVERY_EVIDENCE_BYTES = 8 * 1024
MAX_INVESTIGATOR_ARTIFACT_BYTES = 8 * 1024
MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES = 8 * 1024
MAX_CRITIC_ARTIFACT_BYTES = 8 * 1024
MAX_ITERATION_FEEDBACK_BYTES = 4 * 1024
MAX_ITERATION_HISTORY_BYTES = 32 * 1024
MAX_INVESTIGATOR_DYNAMIC_BYTES = 80_000
MAX_AUTHOR_DYNAMIC_BYTES = 76_000
MAX_CRITIC_DYNAMIC_BYTES = 24_000

_DISALLOWED_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_INVESTIGATOR_FAMILIES = ("entry", "exit", "risk_sizing")
_CRITIC_DISPOSITIONS = ("refine", "abandon", "change_family")
_CANDIDATE_COMPARISON_SEAL = object()
_POLICY_SOURCE_BUNDLE_SEAL = object()
CANDIDATE_VALIDATION_FAILURE_CODES = frozenset(
    {
        "author_diff_invalid",
        "author_diff_noop",
        "author_diff_not_applicable",
        "author_diff_oversize",
        "determinism_failed",
        "imports_failed",
        "next_context_oversize",
        "no_discovery_trades",
        "purity_failed",
        "replay_failed",
        "syntax_failed",
        "worker_failed",
    }
)
_PRECHECK_FAILURE_FLAGS = (False, False, False, False, False, False)
_VALIDATION_FAILURE_FLAGS = MappingProxyType(
    {
        "author_diff_invalid": _PRECHECK_FAILURE_FLAGS,
        "author_diff_noop": _PRECHECK_FAILURE_FLAGS,
        "author_diff_not_applicable": _PRECHECK_FAILURE_FLAGS,
        "author_diff_oversize": _PRECHECK_FAILURE_FLAGS,
        "syntax_failed": (False, False, False, False, False, False),
        "imports_failed": (True, False, False, False, False, False),
        "purity_failed": (True, True, False, False, False, False),
        "determinism_failed": (True, True, True, False, False, False),
        "worker_failed": (True, True, True, True, False, True),
        "replay_failed": (True, True, True, True, True, True),
        "no_discovery_trades": (True, True, True, True, True, True),
        "next_context_oversize": (True, True, True, True, True, True),
    }
)
_POLICY_EDITABLE_PATHS = (
    "core/strategy_policy/entry.py",
    "core/strategy_policy/risk.py",
    "core/strategy_policy/exit.py",
)
_POLICY_DECLARED_SYMBOLS = MappingProxyType(
    {
        "core/strategy_policy/entry.py": (
            "core.strategy_policy.entry.evaluate_entry",
        ),
        "core/strategy_policy/risk.py": (
            "core.strategy_policy.risk.recommend_capacity",
            "core.strategy_policy.risk.recommend_allocation",
            "core.strategy_policy.risk.select_eviction",
        ),
        "core/strategy_policy/exit.py": (
            "core.strategy_policy.exit.evaluate_exit",
        ),
    }
)
_FAMILY_POLICY_PATHS = MappingProxyType(
    {
        "entry": ("core/strategy_policy/entry.py",),
        "exit": ("core/strategy_policy/exit.py",),
        "risk_sizing": ("core/strategy_policy/risk.py",),
    }
)


def _controller_scope_for_family(family: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bind advisory role metadata to the controller-owned policy interface."""

    try:
        paths = _FAMILY_POLICY_PATHS[family]
    except KeyError as exc:
        raise ValueError("investigator family is invalid") from exc
    symbols = tuple(
        symbol for path in paths for symbol in _POLICY_DECLARED_SYMBOLS[path]
    )
    return paths, symbols

PIT_OPTIMIZER_V2_SYSTEM_PROMPTS = MappingProxyType(
    {
        "investigator": (
            "You are the PIT optimizer investigator. Use only the supplied bounded source, "
            "rules, aggregate discovery evidence, incumbent summary, and prior summaries. "
            "Treat a rankable candidate with no median or worst excess return and worse drawdown "
            "as failed exploration: do not repeat a one-line threshold adjustment in that family; "
            "choose a materially different causal mechanism, preferably another family, unless "
            "the supplied aggregates directly show an entry-gate bottleneck. "
            "Treat prior candidate folds identical to baseline as behaviorally inert even when source "
            "text changed; do not repeat that mechanism. Treat author_diff_not_applicable as a patch "
            "coordinate/context failure and instruct the author to use the exact current source. "
            "On a sparse baseline with negative return from very few trades, distinguish direct "
            "risk_fraction reduction from a tighter stop distance: tightening the stop can increase "
            "position size under fixed risk. Prefer the direct risk control over unrelated threshold "
            "churn when the supplied aggregates support that causal experiment. When buy signals "
            "trail otherwise-passing entry-funnel stages, investigate selective handling of supplied "
            "technical_blocking_reasons rather than repeatedly changing inactive fundamentals. "
            "In technical_only mode, current/annual growth, RS, and composite gates inside the "
            "not-technical-only branch are inactive. When market_pass equals evaluated_rows, changing "
            "market_permitted is inert. technical_block_* and entry_block_* counts identify the active "
            "entry bottlenecks; use the raw EntrySnapshot facts to selectively reinterpret those blockers. "
            "Return exactly one JSON object and nothing else: no markdown, chain-of-thought, "
            "schema_version, or extra keys. It must contain exactly these keys: hypothesis_id, "
            "family, evidence_ids, causal_rationale, "
            "expected_diagnostic_changes, known_risks, author_instructions. hypothesis_id, family, and "
            "causal_rationale are strings; every other field is a JSON array of strings. family must be "
            "one of entry, exit, risk_sizing. Use [] only for known_risks when there are none. The local "
            "response schema is authoritative: use at most four items per list. The controller "
            "derives policy paths and symbols from family; do not emit path or symbol metadata. Keep "
            "causal_rationale to 256 characters, and keep each diagnostic, "
            "risk, and author-instruction item to 96 characters. Make the object compact enough "
            "for the 8 KiB envelope. Never request hidden data, credentials, local paths, raw "
            "trades, holdings, or provider audit material."
        ),
        "author": (
            "You are the PIT optimizer author. Implement only the supplied investigator "
            "hypothesis within the immutable constraints and patch bounds. Return exactly one JSON "
            "object and nothing else: no markdown, chain-of-thought, schema_version, or extra keys. "
            "It must contain exactly these keys: hypothesis_id, behavioral_summary, unified_diff, "
            "assumptions, validation_suggestions. hypothesis_id, "
            "behavioral_summary, and unified_diff are strings; every other field is a JSON array of "
            "strings. Copy hypothesis_id verbatim from the investigator. unified_diff must be a "
            "nonempty full-source envelope: the exact first line PIT_FULL_SOURCE_V1 followed by a "
            "newline and the complete replacement text of the single controller-targeted file. "
            "Do not emit diff headers, hunk markers, or markdown fences. The controller derives a "
            "canonical unified diff plus changed-path and changed-symbol "
            "metadata and independently validates the diff, so do not emit that metadata. "
            "assumptions and validation_suggestions may be []. Use at most four assumption "
            "or validation-suggestion items, keep every such item to 96 characters and "
            "behavioral_summary to 256 characters, and keep unified_diff within the supplied "
            "candidate diff bound. Treat source_bundle.files as the only base revision: copy the "
            "selected supplied file completely, byte-for-byte except for the intended strategy edit. "
            "Preserve its imports, module structure, public functions, helpers, and final newline. "
            "For a risk-reduction hypothesis, change risk_fraction directly and leave stop distance "
            "unchanged unless the hypothesis explicitly requires both. Do not quote a prior version "
            "of the source. In technical_only mode, do not edit inactive fundamental thresholds. For an "
            "entry-bottleneck hypothesis, implement selective technical_blocking_reasons handling from "
            "the supplied raw snapshot facts; do not merely force market permission when market_pass "
            "already equals evaluated_rows. Do not execute code or access hidden data, "
            "credentials, local paths, or unrelated source."
        ),
        "critic": (
            "You are the PIT optimizer critic. Analyze only the supplied sanitized validation "
            "and aggregate discovery comparisons. Return exactly one JSON object and nothing else: "
            "no markdown, chain-of-thought, schema_version, or extra keys. It must contain exactly "
            "these keys: hypothesis_id, prediction_vs_observation, causal_explanation, evidence_ids, "
            "disposition, next_direction. All fields except evidence_ids are strings; evidence_ids is "
            "a JSON array of supplied evidence IDs. Copy hypothesis_id and at most four evidence IDs "
            "verbatim from the supplied aggregates. When candidate comparisons are null because local "
            "validation failed, use [] for evidence_ids. disposition must be exactly refine, abandon, or "
            "change_family. Keep every free-text field to 256 characters. You cannot accept a "
            "candidate and must not request hidden results, credentials, local paths, raw trades, "
            "holdings, or provider audit material."
        ),
    }
)

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
            "IDs exactly from observation.domain_evidence_ids for that domain. For abort, domain "
            "is empty and evidence_ids is empty. Do not "
            "abort when verification_directive.route_required is true and the supplied "
            "observation contains candidate IDs and evidence IDs; verification_only "
            "removes performance acceptance authority but still requires routing all three roles. "
            "In that case action must be continue. Do not "
            "select or name a candidate, parameter, value, file, edit, command, external fact, "
            "or reasoning. Return JSON only."
        ),
        "reasoner": (
            "You are the PIT Optimization Reasoner. Return exactly one JSON object with exactly "
            "hypothesis, evidence_ids, invariant_ids, candidate_id, skip, and skip_reason. Use "
            "only the supplied aggregate metrics and closed IDs. This cycle requires exactly one "
            "candidate: set skip to false, set skip_reason to the empty string, choose exactly one "
            "supplied candidate_id, and cite nonempty sorted unique supplied evidence and invariant "
            "IDs. Do not invent a value, file, replacement, source fact, external knowledge, "
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
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must contain unique IDs")
    if not allow_empty and not normalized:
        raise ValueError(f"{field} cannot be empty")
    if any(_ID_RE.fullmatch(item) is None for item in normalized):
        raise ValueError(f"{field} contains an invalid ID")
    return tuple(sorted(normalized))


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


def _parse_v2_closed_object(
    raw: str,
    keys: frozenset[str],
    *,
    max_total_bytes: int,
    optional_keys: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise ValueError("provider payload byte cap is invalid")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_total_bytes:
        raise ValueError("provider payload is not bounded JSON text")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("provider payload is malformed JSON") from exc
    actual_keys = set(value) if isinstance(value, dict) else set()
    if (
        not isinstance(value, dict)
        or not keys.issubset(actual_keys)
        or not actual_keys.issubset(keys | optional_keys)
    ):
        raise ValueError("provider payload has invalid keys")
    return value


def _v2_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _DISALLOWED_TEXT_CONTROL_RE.search(value) is not None
    ):
        if isinstance(value, str) and _DISALLOWED_TEXT_CONTROL_RE.search(value):
            raise ValueError(f"{field} contains a disallowed control character")
        raise ValueError(f"{field} must be canonical text")
    if not allow_empty and not value:
        raise ValueError(f"{field} cannot be empty")
    if len(value.encode("utf-8")) > MAX_ROLE_TEXT_BYTES:
        raise ValueError(f"{field} is too large")
    return value


def _v2_response_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    """Normalize harmless outer whitespace in model-authored text fields.

    Provider JSON is untrusted, but outer whitespace has no strategy meaning once
    the response is converted into a canonical local artifact.  Tolerating that
    one formatting variation avoids discarding an otherwise valid role result;
    types, control characters, emptiness, byte caps, and all subsequent binding
    checks remain enforced by ``_v2_text``.
    """

    if not isinstance(value, str):
        return _v2_text(value, field, allow_empty=allow_empty)
    return _v2_text(value.strip(), field, allow_empty=allow_empty)


def _v2_string_list(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    if len(value) > MAX_ROLE_LIST_ITEMS:
        raise ValueError(f"{field} may contain at most {MAX_ROLE_LIST_ITEMS} items")
    normalized = tuple(_v2_text(item, field) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must contain unique values")
    if not allow_empty and not normalized:
        raise ValueError(f"{field} cannot be empty")
    return normalized


def _v2_response_string_list(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Parse a model-authored list while canonicalizing only outer whitespace."""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    if len(value) > MAX_ROLE_LIST_ITEMS:
        raise ValueError(f"{field} may contain at most {MAX_ROLE_LIST_ITEMS} items")
    normalized = tuple(_v2_response_text(item, field) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must contain unique values")
    if not allow_empty and not normalized:
        raise ValueError(f"{field} cannot be empty")
    return normalized


def _v2_string_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field} must be a tuple")
    return _v2_string_list(list(value), field, allow_empty=allow_empty)


def _v2_identifier(value: object, field: str) -> str:
    text = _v2_text(value, field)
    if _ID_RE.fullmatch(text) is None:
        raise ValueError(f"{field} is invalid")
    return text


def _v2_response_identifier(value: object, field: str) -> str:
    """Validate a model-authored identifier after harmless outer trimming."""

    text = _v2_response_text(value, field)
    if _ID_RE.fullmatch(text) is None:
        raise ValueError(f"{field} is invalid")
    return text


def _v2_blob(value: object, field: str, *, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or _DISALLOWED_TEXT_CONTROL_RE.search(value) is not None
        or len(value.encode("utf-8")) > max_bytes
    ):
        if isinstance(value, str) and _DISALLOWED_TEXT_CONTROL_RE.search(value):
            raise ValueError(f"{field} contains a disallowed control character")
        raise ValueError(f"{field} exceeds its byte cap")
    return value


def _v2_canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _v2_primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")


def _v2_digest(value: object) -> str:
    return hashlib.sha256(_v2_canonical_bytes(value) + b"\n").hexdigest()


def _v2_primitive(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _v2_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _v2_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_v2_primitive(item) for item in value]
    return value


def _v2_max_canonical_text(max_raw_bytes: int) -> str:
    """Return deterministic valid text with maximum canonical JSON expansion."""

    _require_positive_int(max_raw_bytes, "canonical text raw-byte cap")
    best = ""
    best_size = -1
    # UTF-8 is emitted raw. After closing unsafe controls, backslash/quote are
    # the maximum two-byte JSON escapes; representative Unicode cannot exceed
    # its raw UTF-8 size. The first maximum wins to make ties deterministic.
    for unit in ("\\", '"', "é", "😀"):
        unit_bytes = len(unit.encode("utf-8"))
        candidate = unit * (max_raw_bytes // unit_bytes)
        candidate += "x" * (max_raw_bytes - len(candidate.encode("utf-8")))
        size = len(_v2_canonical_bytes(candidate))
        if size > best_size:
            best = candidate
            best_size = size
    return best


class _V2Canonical:
    __slots__ = ()

    def to_primitive(self) -> dict[str, object]:
        primitive = _v2_primitive(self)
        if not isinstance(primitive, dict):
            raise ValueError("v2 contract is not an object")
        return primitive

    def canonical_json_bytes(self) -> bytes:
        return _v2_canonical_bytes(self)

    def to_json(self) -> str:
        return self.canonical_json_bytes().decode("utf-8")


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _require_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class PatchBounds(_V2Canonical):
    max_files: int
    max_hunks: int
    max_changed_lines: int
    max_diff_bytes: int

    def __post_init__(self) -> None:
        for name in ("max_files", "max_hunks", "max_changed_lines", "max_diff_bytes"):
            _require_positive_int(getattr(self, name), f"patch {name}")


def _bounds_fit(inner: PatchBounds, outer: PatchBounds) -> bool:
    return all(
        getattr(inner, name) <= getattr(outer, name)
        for name in ("max_files", "max_hunks", "max_changed_lines", "max_diff_bytes")
    )


def _validate_scoped_paths_symbols(
    paths: tuple[str, ...],
    symbols: tuple[str, ...],
    label: str,
) -> None:
    if any(path not in _POLICY_EDITABLE_PATHS for path in paths):
        raise ValueError(f"{label} paths are outside the editable scope")
    canonical_paths = tuple(path for path in _POLICY_EDITABLE_PATHS if path in paths)
    if paths != canonical_paths:
        raise ValueError(f"{label} paths are not in canonical policy order")
    allowed_symbols = {
        symbol
        for path in paths
        for symbol in _POLICY_DECLARED_SYMBOLS[path]
    }
    allowed_constant_prefixes = tuple(
        f"{path.removesuffix('.py').replace('/', '.')}." for path in paths
    )
    if any(
        symbol not in allowed_symbols
        # Symbol metadata never grants edit authority: candidate diffs remain
        # constrained to authenticated paths and bounded by the patch verifier.
        and re.fullmatch(r"[A-Z][A-Z0-9_]*", symbol) is None
        and not any(
            symbol.startswith(prefix)
            and (
                re.fullmatch(
                    r"[A-Z][A-Z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
                or re.fullmatch(
                    r"_[A-Za-z][A-Za-z0-9_]*", symbol.removeprefix(prefix)
                )
                is not None
            )
            for prefix in allowed_constant_prefixes
        )
        for symbol in symbols
    ):
        raise ValueError(f"{label} symbols are outside the editable scope")


@dataclass(frozen=True, slots=True)
class PitOptimizerCallBudget(_V2Canonical):
    call_index: int
    iteration: int
    role: str
    model: str
    max_static_input_bytes: int
    max_dynamic_input_bytes: int
    max_input_tokens: int
    max_output_tokens: int
    max_response_bytes: int

    def __post_init__(self) -> None:
        _require_positive_int(self.call_index, "optimizer call index")
        _require_positive_int(self.iteration, "optimizer call iteration")
        if self.role not in OPTIMIZER_V2_ROLES:
            raise ValueError("optimizer call role is invalid")
        _v2_text(self.model, "optimizer call model")
        for name in (
            "max_static_input_bytes",
            "max_dynamic_input_bytes",
            "max_input_tokens",
            "max_output_tokens",
            "max_response_bytes",
        ):
            _require_positive_int(getattr(self, name), f"optimizer call {name}")
        if self.max_static_input_bytes + self.max_dynamic_input_bytes > self.max_input_tokens:
            raise ValueError("optimizer call input sections exceed the input token cap")


@dataclass(frozen=True, slots=True)
class PolicySourceScope(_V2Canonical):
    schema_version: int
    policy_interface_version: int
    initial_policy_source_sha256s: tuple[tuple[str, str], ...]
    editable_paths: tuple[str, ...]
    max_policy_source_bundle_bytes: int
    max_iteration_feedback_bytes: int
    max_iteration_history_bytes: int
    hard_patch_bounds: PatchBounds
    candidate_bounds: PatchBounds
    max_iterations: int
    allowed_descendant_rule: str

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("policy source scope schema is unsupported")
        _require_positive_int(self.policy_interface_version, "policy source scope interface")
        if type(self.editable_paths) is not tuple or self.editable_paths != _POLICY_EDITABLE_PATHS:
            raise ValueError("policy source scope editable paths are invalid")
        if (
            type(self.initial_policy_source_sha256s) is not tuple
            or tuple(path for path, _digest in self.initial_policy_source_sha256s)
            != self.editable_paths
        ):
            raise ValueError("policy source scope initial identities are invalid")
        for path, digest in self.initial_policy_source_sha256s:
            _v2_text(path, "policy source scope path")
            _require_digest(digest, "policy source scope initial SHA-256")
        for name, ceiling in (
            ("max_policy_source_bundle_bytes", MAX_POLICY_SOURCE_BUNDLE_BYTES),
            ("max_iteration_feedback_bytes", MAX_ITERATION_FEEDBACK_BYTES),
            ("max_iteration_history_bytes", MAX_ITERATION_HISTORY_BYTES),
        ):
            value = _require_positive_int(getattr(self, name), f"policy source scope {name}")
            if value > ceiling:
                raise ValueError(f"policy source scope {name} exceeds its hard ceiling")
        if not isinstance(self.hard_patch_bounds, PatchBounds) or not isinstance(
            self.candidate_bounds, PatchBounds
        ):
            raise ValueError("policy source scope patch bounds are invalid")
        absolute_hard = PatchBounds(3, 12, 200, MAX_AUTHOR_DIFF_BYTES)
        if not _bounds_fit(self.hard_patch_bounds, absolute_hard):
            raise ValueError("policy source scope hard patch bounds exceed the absolute ceiling")
        if not _bounds_fit(self.candidate_bounds, self.hard_patch_bounds):
            raise ValueError("policy source scope candidate bounds exceed hard patch bounds")
        if type(self.max_iterations) is not int or not 1 <= self.max_iterations <= 8:
            raise ValueError("policy source scope iterations are invalid")
        if (
            self.allowed_descendant_rule
            != "authenticated_initial_sources_plus_validated_cumulative_diff"
        ):
            raise ValueError("policy source scope descendant rule is invalid")

    @property
    def sha256(self) -> str:
        return _v2_digest(self)


@dataclass(frozen=True, slots=True)
class AuthorizationRequirement(_V2Canonical):
    window_id: str
    max_calls: int
    max_tokens: int
    policy_source_scope_sha256: str
    provider_retries: int
    apply: bool

    def __post_init__(self) -> None:
        _v2_identifier(self.window_id, "authorization window ID")
        _require_positive_int(self.max_calls, "authorization call cap")
        _require_positive_int(self.max_tokens, "authorization token cap")
        _require_digest(
            self.policy_source_scope_sha256,
            "authorization policy source scope SHA-256",
        )
        if self.provider_retries != 0:
            raise ValueError("authorization provider retries must be zero")
        if self.apply is not False:
            raise ValueError("authorization apply must be false")

    @property
    def sha256(self) -> str:
        return _v2_digest(self)


@dataclass(frozen=True, slots=True)
class PitOptimizerRunManifest(_V2Canonical):
    schema_version: int
    run_id: str
    run_kind: str
    model: str
    source_head: str
    source_fingerprint_sha256: str
    legacy_readiness_sha256: str
    pit_bundle_sha256: str
    baseline_manifest_sha256: str
    effective_policy_sha256: str
    policy_interface_version: int
    policy_source_sha256s: tuple[tuple[str, str], ...]
    editable_paths: tuple[str, ...]
    policy_source_scope: PolicySourceScope
    immutable_constraints_sha256: str
    fold_manifest: FoldManifest
    parity_attestation_sha256: str
    sandbox_image: str
    validation_ledger_name: str
    immutable_constraint_ids: tuple[str, ...]
    candidate_bounds: PatchBounds
    call_budgets: tuple[PitOptimizerCallBudget, ...]
    max_iterations: int
    non_improving_limit: int
    authorization_requirement: AuthorizationRequirement

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise ValueError("optimizer run manifest schema is unsupported")
        _v2_identifier(self.run_id, "optimizer run ID")
        if self.run_kind != "subset_canary":
            raise ValueError("optimizer run kind is invalid")
        if self.model != PIT_OPTIMIZER_R1_MODEL:
            raise ValueError("optimizer model is invalid")
        if not isinstance(self.source_head, str) or re.fullmatch(
            r"[0-9a-f]{40}", self.source_head
        ) is None:
            raise ValueError("optimizer source HEAD is invalid")
        for name in (
            "source_fingerprint_sha256",
            "legacy_readiness_sha256",
            "pit_bundle_sha256",
            "baseline_manifest_sha256",
            "effective_policy_sha256",
            "immutable_constraints_sha256",
            "parity_attestation_sha256",
        ):
            _require_digest(getattr(self, name), f"optimizer {name}")
        _require_positive_int(self.policy_interface_version, "optimizer policy interface")
        if not isinstance(self.policy_source_scope, PolicySourceScope):
            raise ValueError("optimizer policy source scope is invalid")
        if self.policy_source_scope.policy_interface_version != self.policy_interface_version:
            raise ValueError("optimizer policy interface differs from source scope")
        if self.editable_paths != self.policy_source_scope.editable_paths:
            raise ValueError("optimizer editable paths differ from source scope")
        if self.policy_source_sha256s != self.policy_source_scope.initial_policy_source_sha256s:
            raise ValueError("optimizer policy source identities differ from source scope")
        if not isinstance(self.authorization_requirement, AuthorizationRequirement):
            raise ValueError("optimizer authorization requirement is invalid")
        if (
            self.authorization_requirement.policy_source_scope_sha256
            != self.policy_source_scope.sha256
        ):
            raise ValueError("optimizer source scope authorization is stale")
        if not isinstance(self.fold_manifest, FoldManifest):
            raise ValueError("optimizer fold manifest is invalid")
        if self.fold_manifest.data_identity_sha256 != self.pit_bundle_sha256:
            raise ValueError("optimizer fold and PIT bundle identities differ")
        if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.sandbox_image or "") is None:
            raise ValueError("optimizer sandbox image must be digest pinned")
        if self.validation_ledger_name != "pit_optimizer_validation_ledger.jsonl":
            raise ValueError("optimizer validation ledger name is invalid")
        _v2_string_tuple(self.immutable_constraint_ids, "immutable constraint IDs")
        expected_constraints = hashlib.sha256(
            _v2_canonical_bytes(self.immutable_constraint_ids) + b"\n"
        ).hexdigest()
        if self.immutable_constraints_sha256 != expected_constraints:
            raise ValueError("optimizer immutable constraint identity differs from IDs")
        if not isinstance(self.candidate_bounds, PatchBounds):
            raise ValueError("optimizer candidate bounds are invalid")
        absolute_hard = PatchBounds(3, 12, 200, MAX_AUTHOR_DIFF_BYTES)
        if not _bounds_fit(self.candidate_bounds, absolute_hard):
            raise ValueError("optimizer candidate exceeds hard patch bounds")
        if self.candidate_bounds != self.policy_source_scope.candidate_bounds:
            raise ValueError("optimizer candidate bounds differ from source scope")
        if self.max_iterations != self.policy_source_scope.max_iterations:
            raise ValueError("optimizer iteration limit differs from source scope")
        if type(self.max_iterations) is not int or not 1 <= self.max_iterations <= 8:
            raise ValueError("optimizer iteration limit is invalid")
        if (
            type(self.non_improving_limit) is not int
            or not 1 <= self.non_improving_limit <= 8
        ):
            raise ValueError("optimizer non-improving limit is invalid")
        if (
            type(self.call_budgets) is not tuple
            or len(self.call_budgets) != 3 * self.max_iterations
            or any(not isinstance(item, PitOptimizerCallBudget) for item in self.call_budgets)
        ):
            raise ValueError("optimizer call plan is incomplete")
        expected_order = tuple(
            ((iteration - 1) * 3 + ordinal, iteration, role)
            for iteration in range(1, self.max_iterations + 1)
            for ordinal, role in enumerate(OPTIMIZER_V2_ROLES, start=1)
        )
        actual_order = tuple(
            (item.call_index, item.iteration, item.role) for item in self.call_budgets
        )
        if actual_order != expected_order:
            raise ValueError("optimizer call order is invalid")
        if any(item.model != self.model for item in self.call_budgets):
            raise ValueError("optimizer call model differs from manifest")
        dynamic_ceilings = {
            "investigator": MAX_INVESTIGATOR_DYNAMIC_BYTES,
            "author": MAX_AUTHOR_DYNAMIC_BYTES,
            "critic": MAX_CRITIC_DYNAMIC_BYTES,
        }
        for budget in self.call_budgets:
            if budget.max_dynamic_input_bytes > dynamic_ceilings[budget.role]:
                raise ValueError("optimizer call dynamic section exceeds its hard ceiling")
            static_bytes = len(PIT_OPTIMIZER_V2_SYSTEM_PROMPTS[budget.role].encode("utf-8"))
            static_bytes += len(_v2_canonical_bytes(pit_optimizer_response_format(budget.role)))
            if static_bytes > budget.max_static_input_bytes:
                raise ValueError("optimizer call static section exceeds its declared cap")
        if len(self.call_budgets) > self.authorization_requirement.max_calls:
            raise ValueError("optimizer calls exceed authorization")
        total_tokens = sum(
            item.max_input_tokens + item.max_output_tokens
            for item in self.call_budgets
        )
        if total_tokens > self.authorization_requirement.max_tokens:
            raise ValueError("optimizer tokens exceed authorization")
        if total_tokens != self.authorization_requirement.max_tokens:
            raise ValueError("optimizer tokens must exactly consume authorization")
        _require_subset_canary_call_plan(
            self.call_budgets,
            max_iterations=self.max_iterations,
        )
        if (
            self.authorization_requirement.max_calls != len(self.call_budgets)
            or self.authorization_requirement.max_tokens != total_tokens
        ):
            raise ValueError(
                "subset canary authorization limits are invalid"
            )

    @property
    def sha256(self) -> str:
        return _v2_digest(self)


@dataclass(frozen=True, slots=True)
class PitOptimizerGateConfig:
    phase: str
    baseline_run: Path
    baseline_manifest_sha256: str
    pit_bundle: Path
    pit_bundle_sha256: str
    effective_policy_sha256: str
    optimizer_manifest: Path
    optimizer_manifest_sha256: str
    verified_parity_artifact: Path
    verified_parity_sha256: str
    readiness_artifact: Path | None
    readiness_sha256: str | None
    authorization_window_id: str | None
    authorization_requirement_sha256: str
    source_transmission_authorized: bool
    max_api_calls: int
    max_tokens: int
    max_iterations: int
    apply: bool
    source_root: Path | None = None
    permanent_runtime_root: Path | None = None
    controller_temp_parent: Path | None = None
    artifact_root: Path | None = None
    git_executable: Path | None = None
    docker_executable: Path | None = None
    sandbox_image: str | None = None

    def validate(self) -> None:
        if self.phase not in {"prepare", "canary"}:
            raise ValueError("optimizer gate phase is invalid")
        if self.phase == "prepare":
            if self.source_transmission_authorized is not False:
                raise ValueError(
                    "prepare phase cannot authorize policy source transmission"
                )
            if self.authorization_window_id is not None:
                raise ValueError("prepare phase cannot carry an authorization window")
            if self.readiness_artifact is not None or self.readiness_sha256 is not None:
                raise ValueError("prepare phase cannot carry a readiness identity")
        else:
            if self.source_transmission_authorized is not True:
                raise ValueError(
                    "canary phase policy source transmission must be authorized"
                )
            _v2_identifier(self.authorization_window_id, "authorization window ID")
            if self.readiness_artifact is None or self.readiness_sha256 is None:
                raise ValueError("canary phase requires authenticated readiness")
        if self.apply is not False:
            raise ValueError("optimizer gate apply must be false")
        execution_context = (
            self.source_root,
            self.permanent_runtime_root,
            self.controller_temp_parent,
            self.artifact_root,
            self.git_executable,
            self.docker_executable,
            self.sandbox_image,
        )
        if any(value is not None for value in execution_context):
            if any(value is None for value in execution_context):
                raise ValueError("optimizer gate execution context is incomplete")
            assert self.source_root is not None
            assert self.permanent_runtime_root is not None
            assert self.controller_temp_parent is not None
            assert self.artifact_root is not None
            assert self.git_executable is not None
            assert self.docker_executable is not None
            assert self.sandbox_image is not None
            _resolved_directory(self.source_root, "optimizer gate source root")
            _resolved_directory(
                self.permanent_runtime_root,
                "optimizer gate permanent runtime root",
            )
            _resolved_directory(
                self.controller_temp_parent,
                "optimizer gate controller temp parent",
            )
            _resolved_directory(self.artifact_root, "optimizer gate artifact root")
            _resolved_file(self.git_executable, "optimizer gate Git executable")
            _resolved_file(self.docker_executable, "optimizer gate Docker executable")
            if (
                re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.sandbox_image)
                is None
            ):
                raise ValueError("optimizer gate sandbox image is invalid")
        baseline = _resolved_directory(self.baseline_run, "optimizer gate baseline run")
        baseline_manifest = _resolved_file(
            baseline / "run_manifest.json",
            "optimizer gate baseline manifest",
        )
        manifest_path = _resolved_file(
            self.optimizer_manifest,
            "optimizer manifest artifact",
        )
        bundle_path = _resolved_file(self.pit_bundle, "optimizer gate PIT bundle")
        parity_path = _resolved_file(
            self.verified_parity_artifact,
            "optimizer gate parity artifact",
        )
        readiness_path = (
            None
            if self.readiness_artifact is None
            else _resolved_file(
                self.readiness_artifact,
                "optimizer gate readiness artifact",
            )
        )
        authenticated_files = tuple(
            path
            for path in (
                baseline_manifest,
                manifest_path,
                bundle_path,
                parity_path,
                readiness_path,
            )
            if path is not None
        )
        try:
            paths_overlap = any(
                first == second or os.path.samefile(first, second)
                for index, first in enumerate(authenticated_files)
                for second in authenticated_files[index + 1 :]
            )
        except OSError as exc:
            raise ValueError(
                "optimizer gate authenticated input aliases cannot be verified"
            ) from exc
        if paths_overlap:
            raise ValueError("optimizer gate authenticated input paths overlap")
        for name in (
            "baseline_manifest_sha256",
            "pit_bundle_sha256",
            "effective_policy_sha256",
            "optimizer_manifest_sha256",
            "verified_parity_sha256",
            "authorization_requirement_sha256",
        ):
            _require_digest(getattr(self, name), f"optimizer gate {name}")
        if _sha256_file(baseline_manifest) != self.baseline_manifest_sha256:
            raise ValueError("optimizer gate baseline manifest digest differs")
        if _sha256_file(bundle_path) != self.pit_bundle_sha256:
            raise ValueError("optimizer gate PIT bundle digest differs")
        if _sha256_file(parity_path) != self.verified_parity_sha256:
            raise ValueError("optimizer gate parity artifact digest differs")
        raw_manifest = manifest_path.read_bytes()
        if hashlib.sha256(raw_manifest).hexdigest() != self.optimizer_manifest_sha256:
            raise ValueError("optimizer manifest digest differs")
        try:
            primitive = json.loads(raw_manifest)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("optimizer manifest is invalid JSON") from exc
        if not isinstance(primitive, dict) or raw_manifest != _v2_canonical_bytes(primitive) + b"\n":
            raise ValueError("optimizer manifest is not canonical JSON")
        closed_manifest = _pit_optimizer_manifest_from_primitive(primitive)
        if closed_manifest.sha256 != self.optimizer_manifest_sha256:
            raise ValueError("optimizer manifest closed identity differs")
        identity_fields = {
            "baseline_manifest_sha256": self.baseline_manifest_sha256,
            "pit_bundle_sha256": self.pit_bundle_sha256,
            "effective_policy_sha256": self.effective_policy_sha256,
        }
        if any(primitive.get(name) != expected for name, expected in identity_fields.items()):
            raise ValueError("optimizer gate identities differ from manifest")
        if closed_manifest.parity_attestation_sha256 != self.verified_parity_sha256:
            raise ValueError("optimizer gate parity identity differs from manifest")
        if (
            self.sandbox_image is not None
            and self.sandbox_image != closed_manifest.sandbox_image
        ):
            raise ValueError("optimizer gate sandbox image differs from manifest")
        authorization = primitive.get("authorization_requirement")
        if not isinstance(authorization, dict):
            raise ValueError("optimizer manifest authorization requirement is absent")
        authorization_sha256 = hashlib.sha256(
            _v2_canonical_bytes(authorization) + b"\n"
        ).hexdigest()
        if authorization_sha256 != self.authorization_requirement_sha256:
            raise ValueError("optimizer gate authorization requirement digest differs")
        _require_positive_int(self.max_api_calls, "optimizer gate call cap")
        _require_positive_int(self.max_tokens, "optimizer gate token cap")
        _require_positive_int(self.max_iterations, "optimizer gate iteration cap")
        if (
            authorization.get("max_calls") != self.max_api_calls
            or authorization.get("max_tokens") != self.max_tokens
            or primitive.get("max_iterations") != self.max_iterations
        ):
            raise ValueError("optimizer gate ceilings differ from manifest")
        if authorization.get("apply") is not self.apply:
            raise ValueError("optimizer gate apply differs from authorization requirement")
        if readiness_path is not None:
            assert self.readiness_sha256 is not None
            _require_digest(self.readiness_sha256, "optimizer gate readiness SHA-256")
            raw_readiness = readiness_path.read_bytes()
            if hashlib.sha256(raw_readiness).hexdigest() != self.readiness_sha256:
                raise ValueError("optimizer gate readiness artifact digest differs")
            try:
                readiness = json.loads(raw_readiness)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("optimizer gate readiness artifact is invalid JSON") from exc
            if (
                not isinstance(readiness, dict)
                or set(readiness)
                != {
                    "schema_version",
                    "manifest",
                    "manifest_sha256",
                    "parity",
                    "baseline_discovery",
                    "provider_seed",
                }
                or raw_readiness != _v2_canonical_bytes(readiness) + b"\n"
            ):
                raise ValueError("optimizer gate readiness artifact is not canonical")
            if (
                readiness.get("schema_version") != 3
                or readiness.get("manifest_sha256") != closed_manifest.sha256
                or readiness.get("manifest") != primitive
            ):
                raise ValueError("optimizer gate readiness identity differs from manifest")
        if (
            self.phase == "canary"
            and self.authorization_window_id
            != closed_manifest.authorization_requirement.window_id
        ):
            raise ValueError("optimizer gate authorization window differs from manifest")


def _resolved_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"{label} must be an existing absolute non-link directory")
    return candidate.resolve()


def _resolved_file(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be an existing absolute regular non-link file")
    return candidate.resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _committed_policy_source_text(source_root: Path, relative: str) -> str:
    """Read the exact policy text a candidate export receives from HEAD."""

    _resolved_file(source_root / Path(relative), f"policy source {relative}")
    try:
        completed = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=source_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.decode("utf-8")
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "committed policy source must be readable as UTF-8 text"
        ) from exc


def _pit_optimizer_manifest_from_primitive(
    primitive: Mapping[str, object],
) -> PitOptimizerRunManifest:
    expected_keys = {field.name for field in fields(PitOptimizerRunManifest)}
    if set(primitive) != expected_keys:
        raise ValueError("optimizer manifest keys are invalid")
    try:
        scope_value = primitive["policy_source_scope"]
        fold_value = primitive["fold_manifest"]
        authorization_value = primitive["authorization_requirement"]
        if not all(
            isinstance(value, dict)
            for value in (scope_value, fold_value, authorization_value)
        ):
            raise ValueError("optimizer manifest nested contracts are invalid")
        scope_primitive = dict(scope_value)
        scope_primitive["initial_policy_source_sha256s"] = tuple(
            tuple(item)
            for item in scope_primitive["initial_policy_source_sha256s"]
        )
        scope_primitive["editable_paths"] = tuple(scope_primitive["editable_paths"])
        scope_primitive["hard_patch_bounds"] = PatchBounds(
            **scope_primitive["hard_patch_bounds"]
        )
        scope_primitive["candidate_bounds"] = PatchBounds(
            **scope_primitive["candidate_bounds"]
        )
        fold_primitive = dict(fold_value)

        def fold_spec(value: object) -> object:
            if not isinstance(value, dict):
                raise ValueError("optimizer fold specification is invalid")
            nested = dict(value)
            nested["sessions"] = tuple(nested["sessions"])
            from core.pit_optimizer_evaluation import FoldSpec

            return FoldSpec(**nested)

        fold_primitive["discovery_folds"] = tuple(
            fold_spec(item) for item in fold_primitive["discovery_folds"]
        )
        fold_primitive["hidden_fold"] = fold_spec(fold_primitive["hidden_fold"])
        values = dict(primitive)
        values["policy_source_sha256s"] = tuple(
            tuple(item) for item in values["policy_source_sha256s"]
        )
        values["editable_paths"] = tuple(values["editable_paths"])
        values["policy_source_scope"] = PolicySourceScope(**scope_primitive)
        values["fold_manifest"] = FoldManifest(**fold_primitive)
        values["immutable_constraint_ids"] = tuple(
            values["immutable_constraint_ids"]
        )
        values["candidate_bounds"] = PatchBounds(**values["candidate_bounds"])
        values["call_budgets"] = tuple(
            PitOptimizerCallBudget(**item) for item in values["call_budgets"]
        )
        values["authorization_requirement"] = AuthorizationRequirement(
            **authorization_value
        )
        return PitOptimizerRunManifest(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("optimizer manifest closed contract is invalid") from exc


def _canonical_mapping_bytes(value: Mapping[str, object]) -> bytes:
    return _v2_canonical_bytes(value) + b"\n"


def _require_canonical_mapping_artifact(
    value: Mapping[str, object],
    path: Path,
    label: str,
) -> tuple[Path, str]:
    resolved = _resolved_file(path, label)
    expected = _canonical_mapping_bytes(value)
    if resolved.read_bytes() != expected:
        raise ValueError(f"{label} differs from the supplied canonical object")
    return resolved, hashlib.sha256(expected).hexdigest()


def _require_parity_artifact(parity: object, path: Path) -> tuple[Path, str]:
    from core.pit_policy_parity import ParityAttestation

    if not isinstance(parity, ParityAttestation):
        raise ValueError("verified parity must be a closed attestation")
    resolved = _resolved_file(path, "verified parity artifact")
    if parity.artifact_path.resolve() != resolved:
        raise ValueError("verified parity artifact path differs from attestation")
    primitive = {
        field.name: _v2_primitive(getattr(parity, field.name))
        for field in fields(parity)
        if field.name not in {"artifact_path", "artifact_sha256"}
    }
    expected = _v2_canonical_bytes(primitive) + b"\n"
    actual = resolved.read_bytes()
    digest = hashlib.sha256(actual).hexdigest()
    if actual != expected or digest != parity.artifact_sha256:
        raise ValueError("verified parity artifact identity is invalid")
    return resolved, digest


def _source_bundle_envelope_bytes(
    *,
    policy_interface_version: int,
    source_texts: Mapping[str, str],
) -> tuple[int, int]:
    records = [
        {
            "path": path,
            "sha256": hashlib.sha256(source_texts[path].encode("utf-8")).hexdigest(),
            "declared_symbols": list(_POLICY_DECLARED_SYMBOLS[path]),
            "text": source_texts[path],
        }
        for path in _POLICY_EDITABLE_PATHS
    ]
    primitive = {
        "policy_interface_version": policy_interface_version,
        "cumulative_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "cumulative_diff": "",
        "files": records,
    }
    initial_bytes = sum(len(source_texts[path].encode("utf-8")) for path in _POLICY_EDITABLE_PATHS)
    envelope_bytes = len(_v2_canonical_bytes(primitive)) - initial_bytes
    return initial_bytes, envelope_bytes


def _require_subset_canary_call_plan(
    call_budgets: tuple[PitOptimizerCallBudget, ...],
    *,
    max_iterations: int,
) -> None:
    """Require one of the explicitly bounded subset-canary profiles.

    The single-iteration profile exercises every optimizer role and its local
    candidate/evaluation path before a second round of model work is allowed.
    """

    fast_e2e_profile = {
        "investigator": (8_000, 78_000, 86_000, 8_000, 8 * 1024),
        "author": (12_000, 48_500, 72_000, 8_000, 16 * 1024),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024),
    }
    author_reasoning_profile = {
        "investigator": (8_000, 78_000, 86_000, 8_000, 8 * 1024),
        # Reallocate unused author input headroom to completion headroom.  Deep
        # reasoning models may account for hidden reasoning in completion
        # tokens even when the visible JSON artifact remains byte-bounded.
        "author": (12_000, 48_500, 64_000, 16_000, 16 * 1024),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024),
    }
    extended_profile = {
        "investigator": (8_000, 78_000, 86_000, 16_000, 8 * 1024),
        "author": (12_000, 48_500, 72_000, 14_000, 16 * 1024),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024),
    }
    if (max_iterations, len(call_budgets)) == (1, 3):
        expected_profiles = (fast_e2e_profile, author_reasoning_profile)
    elif 2 <= max_iterations <= 8 and len(call_budgets) == 3 * max_iterations:
        expected_profiles = (extended_profile,)
    else:
        raise ValueError("subset canary iteration profile is unsupported")
    actual_profile = {
        budget.role: (
            budget.max_static_input_bytes,
            budget.max_dynamic_input_bytes,
            budget.max_input_tokens,
            budget.max_output_tokens,
            budget.max_response_bytes,
        )
        for budget in call_budgets
    }
    if any(budget.model != PIT_OPTIMIZER_R1_MODEL for budget in call_budgets) or (
        actual_profile not in expected_profiles
    ):
        raise ValueError("subset canary call caps are invalid")


def _attested_parity_reference_folds(
    *,
    parity_attestation: object,
    parity_reference: object,
) -> tuple[FoldManifest, tuple[str, ...]]:
    """Return only the fold plan and universe sealed by a matching parity reference."""

    from core.pit_policy_parity import ParityAttestation, ParityReference

    if not isinstance(parity_attestation, ParityAttestation) or not isinstance(
        parity_reference, ParityReference
    ):
        raise ValueError("attested parity reference is invalid")
    if (
        parity_reference.artifact_sha256
        != parity_attestation.reference_artifact_sha256
        or parity_reference.reference_source_head
        != parity_attestation.reference_source_head
        or parity_reference.pit_bundle_sha256 != parity_attestation.pit_bundle_sha256
        or parity_reference.baseline_manifest_sha256
        != parity_attestation.baseline_manifest_sha256
        or parity_reference.effective_policy_sha256
        != parity_attestation.effective_policy_sha256
        or parity_reference.fold_manifest.sha256
        != parity_attestation.discovery_fold_manifest_sha256
        or parity_reference.discovery_output_sha256s
        != parity_attestation.reference_output_sha256s
    ):
        raise ValueError("attested parity reference fold manifest differs")
    return parity_reference.fold_manifest, parity_reference.universe


def build_subset_manifest(
    *,
    legacy_readiness: Mapping[str, object],
    legacy_readiness_path: Path,
    parity_attestation: object,
    verified_parity_path: Path,
    pit_bundle: Path,
    baseline_run: Path,
    source_root: Path,
    permanent_runtime_root: Path,
    controller_temp_parent: Path,
    artifact_root: Path,
    sandbox_image: str,
    call_budgets: tuple[PitOptimizerCallBudget, ...],
    candidate_bounds: PatchBounds,
    max_iterations: int,
    parity_reference: object | None = None,
) -> PitOptimizerRunManifest:
    """Authenticate inert inputs and seal one provider-free subset manifest."""

    if not isinstance(legacy_readiness, Mapping):
        raise ValueError("legacy readiness must be a mapping")
    _readiness_path, readiness_sha256 = _require_canonical_mapping_artifact(
        legacy_readiness,
        Path(legacy_readiness_path),
        "legacy readiness artifact",
    )
    _parity_path, parity_sha256 = _require_parity_artifact(
        parity_attestation,
        Path(verified_parity_path),
    )
    source = _resolved_directory(Path(source_root), "source root")
    from core.pit_policy_parity import (
        _authenticated_readiness,
        _require_later_descendant_source,
        _source_identity,
    )

    actual_source_head, actual_source_fingerprint = _source_identity(source)
    authenticated_readiness, authenticated_readiness_sha256 = _authenticated_readiness(
        _readiness_path,
        source_root=source,
    )
    if (
        authenticated_readiness != dict(legacy_readiness)
        or authenticated_readiness_sha256 != readiness_sha256
    ):
        raise ValueError("legacy readiness differs from authenticated readiness")
    _resolved_directory(Path(permanent_runtime_root), "permanent runtime root")
    _resolved_directory(Path(controller_temp_parent), "controller temp parent")
    _resolved_directory(Path(artifact_root), "optimizer artifact root")
    bundle_path = _resolved_file(Path(pit_bundle), "PIT bundle")
    baseline = _resolved_directory(Path(baseline_run), "baseline run")
    baseline_manifest_path = _resolved_file(
        baseline / "run_manifest.json",
        "baseline run manifest",
    )

    if (
        legacy_readiness.get("schema_version") != 1
        or legacy_readiness.get("gate") != "pit_optimization"
        or legacy_readiness.get("phase") != "ready"
    ):
        raise ValueError("legacy readiness is not authenticated ready input")
    identities = legacy_readiness.get("identities")
    sealed_inputs = legacy_readiness.get("sealed_inputs")
    evaluation = legacy_readiness.get("evaluation_contract")
    if not isinstance(identities, Mapping) or not isinstance(sealed_inputs, Mapping):
        raise ValueError("legacy readiness identities are absent")
    parity = parity_attestation
    _require_later_descendant_source(
        source_root=source,
        reference_head=identities.get("source_head"),
        final_head=parity.reference_source_head,
    )
    _require_later_descendant_source(
        source_root=source,
        reference_head=parity.reference_source_head,
        final_head=parity.final_source_head,
    )
    if not isinstance(evaluation, Mapping) or evaluation.get("verification_only") is not True:
        raise ValueError("legacy readiness evaluation contract is invalid")

    bundle_sha256 = _sha256_file(bundle_path)
    baseline_sha256 = _sha256_file(baseline_manifest_path)
    if identities.get("pit_bundle_sha256") != bundle_sha256:
        raise ValueError("legacy readiness PIT bundle identity differs")
    if identities.get("baseline_manifest_sha256") != baseline_sha256:
        raise ValueError("legacy readiness baseline manifest identity differs")
    if sealed_inputs.get("pit_bundle_sha256") != bundle_sha256:
        raise ValueError("legacy readiness sealed PIT bundle identity differs")
    baseline_map = sealed_inputs.get("baseline_artifact_sha256")
    if not isinstance(baseline_map, Mapping) or baseline_map.get("run_manifest.json") != baseline_sha256:
        raise ValueError("legacy readiness sealed baseline identity differs")
    effective_policy = legacy_readiness.get("effective_policy")
    if not isinstance(effective_policy, Mapping):
        raise ValueError("legacy readiness effective policy is absent")
    effective_policy_sha256 = effective_engine_policy_sha256(effective_policy)
    if identities.get("effective_policy_sha256") != effective_policy_sha256:
        raise ValueError("legacy readiness effective policy identity differs")

    if parity_reference is None:
        from core.pit_data import PITDataBundle
        from core.pit_policy_parity import build_fixed_fold_manifest

        with PITDataBundle(bundle_path, expected_sha256=bundle_sha256) as bundle:
            rows = bundle._connection.execute(
                "SELECT trade_date FROM price WHERE ticker='SPY' "
                "AND trade_date>='2021-06-25' AND trade_date<='2022-03-11' "
                "ORDER BY trade_date"
            ).fetchall()
            benchmark_sessions = tuple(str(row[0]) for row in rows)
        fold_manifest, selected_universe = build_fixed_fold_manifest(
            readiness=legacy_readiness,
            benchmark_sessions=benchmark_sessions,
            data_identity_sha256=bundle_sha256,
        )
    else:
        fold_manifest, selected_universe = _attested_parity_reference_folds(
            parity_attestation=parity_attestation,
            parity_reference=parity_reference,
        )
        scope = evaluation.get("scope") if isinstance(evaluation, Mapping) else None
        raw_universe = scope.get("symbols") if isinstance(scope, Mapping) else None
        if (
            fold_manifest.data_identity_sha256 != bundle_sha256
            or fold_manifest.benchmark != "SPY"
            or not isinstance(raw_universe, list)
            or tuple(raw_universe) != selected_universe
        ):
            raise ValueError("attested parity reference differs from legacy readiness")

    if (
        parity.pit_bundle_sha256 != bundle_sha256
        or parity.baseline_manifest_sha256 != baseline_sha256
        or parity.effective_policy_sha256 != effective_policy_sha256
        or parity.discovery_fold_manifest_sha256 != fold_manifest.sha256
        or parity.final_source_head != actual_source_head
        or parity.final_source_fingerprint_sha256
        != actual_source_fingerprint
    ):
        raise ValueError("verified parity differs from the authenticated identity graph")

    # Candidates are exported from the committed tree, not copied from the
    # checkout.  Seal those exact Git blob bytes here as well: a Windows
    # checkout may materialize CRLF while the exported candidate uses the
    # committed LF blob, and hashing the checkout would make every policy
    # scope fail before the first authorized role call.
    source_texts: dict[str, str] = {}
    source_sha256s: list[tuple[str, str]] = []
    for relative in _POLICY_EDITABLE_PATHS:
        text = _committed_policy_source_text(source, relative)
        source_texts[relative] = text
        source_sha256s.append((relative, hashlib.sha256(text.encode("utf-8")).hexdigest()))

    if not isinstance(candidate_bounds, PatchBounds):
        raise ValueError("candidate bounds are invalid")
    hard_bounds = PatchBounds(3, 12, 200, MAX_AUTHOR_DIFF_BYTES)
    if not _bounds_fit(candidate_bounds, hard_bounds):
        raise ValueError("candidate bounds exceed hard patch bounds")
    initial_policy_bytes, envelope_bytes = _source_bundle_envelope_bytes(
        policy_interface_version=parity.policy_interface_version,
        source_texts=source_texts,
    )
    if (
        initial_policy_bytes
        + (2 * candidate_bounds.max_diff_bytes)
        + envelope_bytes
        > MAX_POLICY_SOURCE_BUNDLE_BYTES
    ):
        raise ValueError("prospective policy source bundle exceeds its byte cap")

    if type(call_budgets) is not tuple or any(
        not isinstance(item, PitOptimizerCallBudget) for item in call_budgets
    ):
        raise ValueError("optimizer call budgets are invalid")
    _require_subset_canary_call_plan(call_budgets, max_iterations=max_iterations)
    constraint_ids_value = legacy_readiness.get("invariant_ids")
    if not isinstance(constraint_ids_value, list):
        raise ValueError("legacy readiness invariant IDs are absent")
    constraint_ids = _v2_string_list(
        constraint_ids_value,
        "legacy readiness invariant IDs",
    )
    constraints_sha256 = hashlib.sha256(
        _v2_canonical_bytes(constraint_ids) + b"\n"
    ).hexdigest()
    scope = PolicySourceScope(
        schema_version=2,
        policy_interface_version=parity.policy_interface_version,
        initial_policy_source_sha256s=tuple(source_sha256s),
        editable_paths=_POLICY_EDITABLE_PATHS,
        max_policy_source_bundle_bytes=MAX_POLICY_SOURCE_BUNDLE_BYTES,
        max_iteration_feedback_bytes=MAX_ITERATION_FEEDBACK_BYTES,
        max_iteration_history_bytes=MAX_ITERATION_HISTORY_BYTES,
        hard_patch_bounds=hard_bounds,
        candidate_bounds=candidate_bounds,
        max_iterations=max_iterations,
        allowed_descendant_rule=(
            "authenticated_initial_sources_plus_validated_cumulative_diff"
        ),
    )
    render_worst_iteration_two_role_inputs(
        scope=scope,
        source_texts=source_texts,
        immutable_constraint_ids=constraint_ids,
        call_budgets=call_budgets,
    )
    authorization = AuthorizationRequirement(
        window_id=f"window_{uuid.uuid4().hex}",
        max_calls=len(call_budgets),
        max_tokens=sum(
            item.max_input_tokens + item.max_output_tokens
            for item in call_budgets
        ),
        policy_source_scope_sha256=scope.sha256,
        provider_retries=0,
        apply=False,
    )
    return PitOptimizerRunManifest(
        schema_version=3,
        run_id=f"run_{uuid.uuid4().hex}",
        run_kind="subset_canary",
        model=PIT_OPTIMIZER_R1_MODEL,
        source_head=actual_source_head,
        source_fingerprint_sha256=actual_source_fingerprint,
        legacy_readiness_sha256=readiness_sha256,
        pit_bundle_sha256=bundle_sha256,
        baseline_manifest_sha256=baseline_sha256,
        effective_policy_sha256=effective_policy_sha256,
        policy_interface_version=parity.policy_interface_version,
        policy_source_sha256s=tuple(source_sha256s),
        editable_paths=_POLICY_EDITABLE_PATHS,
        policy_source_scope=scope,
        immutable_constraints_sha256=constraints_sha256,
        fold_manifest=fold_manifest,
        parity_attestation_sha256=parity_sha256,
        sandbox_image=sandbox_image,
        validation_ledger_name="pit_optimizer_validation_ledger.jsonl",
        immutable_constraint_ids=constraint_ids,
        candidate_bounds=candidate_bounds,
        call_budgets=call_budgets,
        max_iterations=max_iterations,
        non_improving_limit=max_iterations,
        authorization_requirement=authorization,
    )


def write_optimizer_manifest(
    manifest: PitOptimizerRunManifest,
    output: Path,
) -> tuple[Path, str]:
    if not isinstance(manifest, PitOptimizerRunManifest):
        raise ValueError("optimizer manifest writer requires a closed manifest")
    path = Path(output)
    if not path.is_absolute():
        raise ValueError("optimizer manifest output must be absolute")
    parent = _resolved_directory(path.parent, "optimizer manifest parent")
    resolved = parent / path.name
    payload = manifest.canonical_json_bytes() + b"\n"
    with resolved.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    digest = hashlib.sha256(payload).hexdigest()
    if digest != manifest.sha256:
        raise ValueError("optimizer manifest writer produced a digest mismatch")
    return resolved, digest


def build_prepare_command(
    manifest: PitOptimizerRunManifest,
    *,
    manifest_path: Path,
    legacy_readiness_path: Path,
    verified_parity_path: Path,
    pit_bundle_path: Path,
    baseline_run_path: Path,
    repo_root: Path,
    permanent_runtime_root: Path,
    controller_temp_parent: Path,
    artifact_root: Path,
    git_executable: Path,
    docker_executable: Path,
    sandbox_image: str,
) -> str:
    if not isinstance(manifest, PitOptimizerRunManifest):
        raise ValueError("prepare command requires a closed optimizer manifest")
    optimizer_manifest = _resolved_file(manifest_path, "optimizer manifest artifact")
    legacy_readiness = _resolved_file(legacy_readiness_path, "legacy readiness artifact")
    verified_parity = _resolved_file(verified_parity_path, "verified parity artifact")
    pit_bundle = _resolved_file(pit_bundle_path, "PIT bundle")
    baseline_run = _resolved_directory(baseline_run_path, "baseline run")
    baseline_manifest = _resolved_file(
        baseline_run / "run_manifest.json",
        "baseline run manifest",
    )
    repository = _resolved_directory(repo_root, "repository root")
    runtime_root = _resolved_directory(permanent_runtime_root, "permanent runtime root")
    temp_parent = _resolved_directory(controller_temp_parent, "controller temp parent")
    artifacts = _resolved_directory(artifact_root, "optimizer artifact root")
    git_path = _resolved_file(git_executable, "Git executable")
    docker_path = _resolved_file(docker_executable, "Docker executable")
    if _sha256_file(optimizer_manifest) != manifest.sha256:
        raise ValueError("optimizer manifest artifact differs from manifest")
    if _sha256_file(legacy_readiness) != manifest.legacy_readiness_sha256:
        raise ValueError("legacy readiness artifact differs from manifest")
    if _sha256_file(verified_parity) != manifest.parity_attestation_sha256:
        raise ValueError("verified parity artifact differs from manifest")
    if _sha256_file(pit_bundle) != manifest.pit_bundle_sha256:
        raise ValueError("PIT bundle differs from manifest")
    if _sha256_file(baseline_manifest) != manifest.baseline_manifest_sha256:
        raise ValueError("baseline run differs from manifest")
    for relative, expected_sha256 in manifest.policy_source_sha256s:
        source_text = _committed_policy_source_text(repository, relative)
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != expected_sha256:
            raise ValueError("repository policy source differs from manifest")
    if sandbox_image != manifest.sandbox_image:
        raise ValueError("prepare sandbox image differs from manifest")
    arguments = [
        authenticated_python_executable(),
        "-B",
        str((repository / "agent_loop.py").resolve()),
        "--repo-root",
        str(repository),
        "--permanent-runtime-root",
        str(runtime_root),
        "--git-executable",
        str(git_path),
        "--controller-temp-parent",
        str(temp_parent),
        "--artifact-root",
        str(artifacts),
        "--docker-executable",
        str(docker_path),
        "--sandbox-image",
        sandbox_image,
        "--gate",
        "pit_optimizer",
        "--optimization-phase",
        "prepare",
        "--optimizer-manifest",
        str(optimizer_manifest),
        "--optimizer-manifest-sha256",
        manifest.sha256,
        "--verified-parity",
        str(verified_parity),
        "--verified-parity-sha256",
        manifest.parity_attestation_sha256,
        "--pit-bundle",
        str(pit_bundle),
        "--pit-bundle-sha256",
        manifest.pit_bundle_sha256,
        "--baseline-run",
        str(baseline_run),
        "--baseline-manifest-sha256",
        manifest.baseline_manifest_sha256,
        "--effective-policy-sha256",
        manifest.effective_policy_sha256,
        "--optimizer-authorization-requirement-sha256",
        manifest.authorization_requirement.sha256,
        "--max-api-calls",
        str(manifest.authorization_requirement.max_calls),
        "--max-tokens",
        str(manifest.authorization_requirement.max_tokens),
        "--max-iterations",
        str(manifest.max_iterations),
    ]
    return render_pit_optimizer_v3_command(arguments)


@dataclass(frozen=True, slots=True)
class PolicySourceRecord(_V2Canonical):
    path: str
    sha256: str
    declared_symbols: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        path = _v2_text(self.path, "policy source path")
        if path not in _POLICY_EDITABLE_PATHS:
            raise ValueError("policy source path is outside the editable scope")
        _require_digest(self.sha256, "policy source SHA-256")
        _v2_blob(
            self.text,
            "policy source text",
            max_bytes=MAX_POLICY_SOURCE_BUNDLE_BYTES,
        )
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("policy source SHA-256 differs from text")
        _v2_string_tuple(self.declared_symbols, "policy declared symbols")
        if self.declared_symbols != _POLICY_DECLARED_SYMBOLS[path]:
            raise ValueError("policy declared symbols differ from the closed interface")


@dataclass(frozen=True, slots=True)
class PolicySourceBundle(_V2Canonical):
    policy_interface_version: int
    cumulative_diff_sha256: str
    cumulative_diff: str
    files: tuple[PolicySourceRecord, ...]
    _controller_seal: InitVar[object] = None

    def __post_init__(self, _controller_seal: object) -> None:
        if _controller_seal is not _POLICY_SOURCE_BUNDLE_SEAL:
            raise ValueError("policy source bundle must be controller derived")
        _require_positive_int(self.policy_interface_version, "policy interface version")
        _require_digest(self.cumulative_diff_sha256, "cumulative diff SHA-256")
        if not isinstance(self.cumulative_diff, str) or "\x00" in self.cumulative_diff:
            raise ValueError("cumulative diff is invalid")
        if len(self.cumulative_diff.encode("utf-8")) > MAX_AUTHOR_DIFF_BYTES:
            raise ValueError("cumulative diff exceeds its byte cap")
        if hashlib.sha256(self.cumulative_diff.encode("utf-8")).hexdigest() != self.cumulative_diff_sha256:
            raise ValueError("cumulative diff SHA-256 differs from text")
        if type(self.files) is not tuple or any(
            not isinstance(item, PolicySourceRecord) for item in self.files
        ):
            raise ValueError("policy source files are invalid")
        if tuple(item.path for item in self.files) != _POLICY_EDITABLE_PATHS:
            raise ValueError("policy source bundle must contain the three editable files")
        if len(self.canonical_json_bytes()) > MAX_POLICY_SOURCE_BUNDLE_BYTES:
            raise ValueError("policy source bundle exceeds its byte cap")


@dataclass(frozen=True, slots=True)
class _PatchStats:
    paths: tuple[str, ...]
    hunks: int
    changed_lines: int
    diff_bytes: int


_HUNK_RE = re.compile(
    r"@@ -(0|[1-9][0-9]*)(?:,([0-9]+))? \+(0|[1-9][0-9]*)(?:,([0-9]+))? @@(?: [^\r\n]*)?\n?"
)


def _require_patch_bounds(stats: _PatchStats, bounds: PatchBounds) -> None:
    comparisons = (
        (len(stats.paths), bounds.max_files, "max_files"),
        (stats.hunks, bounds.max_hunks, "max_hunks"),
        (stats.changed_lines, bounds.max_changed_lines, "max_changed_lines"),
        (stats.diff_bytes, bounds.max_diff_bytes, "max_diff_bytes"),
    )
    for actual, maximum, name in comparisons:
        if actual > maximum:
            raise ValueError(f"candidate patch exceeds {name}")


def _apply_unified_diff(
    source_texts: Mapping[str, str],
    unified_diff: str,
    *,
    bounds: PatchBounds,
    allow_empty: bool = False,
) -> tuple[dict[str, str], _PatchStats]:
    if not isinstance(bounds, PatchBounds):
        raise ValueError("candidate patch bounds are invalid")
    if not isinstance(unified_diff, str) or "\x00" in unified_diff:
        raise ValueError("candidate unified diff is invalid")
    diff_bytes = len(unified_diff.encode("utf-8"))
    if not unified_diff:
        if not allow_empty:
            raise ValueError("candidate unified diff is empty")
        stats = _PatchStats((), 0, 0, 0)
        _require_patch_bounds(stats, bounds)
        return dict(source_texts), stats
    lines = unified_diff.splitlines(keepends=True)
    output = dict(source_texts)
    paths: list[str] = []
    hunks = 0
    changed_lines = 0
    index = 0
    while index < len(lines):
        if lines[index].startswith("diff --git "):
            fields = lines[index].rstrip("\r\n").split(" ")
            if (
                len(fields) != 4
                or not fields[2].startswith("a/")
                or fields[3] != f"b/{fields[2][2:]}"
            ):
                raise ValueError("candidate unified diff Git header is invalid")
            git_path = fields[2][2:]
            index += 1
            if index >= len(lines) or re.fullmatch(
                r"index [0-9a-fA-F]+\.\.[0-9a-fA-F]+ 100644\r?\n?",
                lines[index],
            ) is None:
                raise ValueError("candidate unified diff Git index is invalid")
            index += 1
        else:
            git_path = None
        old_header = lines[index]
        if not old_header.startswith("--- a/"):
            raise ValueError("candidate unified diff old-file header is invalid")
        path = old_header[len("--- a/") :].rstrip("\r\n")
        if git_path is not None and path != git_path:
            raise ValueError("candidate unified diff Git path differs")
        index += 1
        if index >= len(lines) or lines[index].rstrip("\r\n") != f"+++ b/{path}":
            raise ValueError("candidate unified diff new-file header is invalid")
        index += 1
        if path not in source_texts or path in paths:
            raise ValueError("candidate unified diff path is invalid")
        paths.append(path)
        original = source_texts[path].splitlines(keepends=True)
        result: list[str] = []
        source_index = 0
        file_hunks = 0
        while index < len(lines) and not lines[index].startswith(
            ("--- a/", "diff --git ")
        ):
            header = lines[index]
            match = _HUNK_RE.fullmatch(header)
            if match is None:
                raise ValueError("candidate unified diff hunk header is invalid")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            target_index = old_start if old_count == 0 else max(old_start - 1, 0)
            if target_index < source_index or target_index > len(original):
                raise ValueError("candidate unified diff hunk location is invalid")
            result.extend(original[source_index:target_index])
            source_index = target_index
            expected_new_start = len(result) + (0 if new_count == 0 else 1)
            if new_start != expected_new_start:
                raise ValueError("candidate unified diff new hunk location is invalid")
            index += 1
            observed_old = 0
            observed_new = 0
            while index < len(lines) and not lines[index].startswith(
                ("@@ ", "--- a/", "diff --git ")
            ):
                line = lines[index]
                if line.startswith("\\ No newline at end of file"):
                    raise ValueError("candidate unified diff newline markers are unsupported")
                if not line or line[0] not in {" ", "+", "-"}:
                    raise ValueError("candidate unified diff body is invalid")
                payload = line[1:]
                if line[0] in {" ", "-"}:
                    if source_index >= len(original) or original[source_index] != payload:
                        raise ValueError("candidate unified diff does not apply to source")
                    source_index += 1
                    observed_old += 1
                if line[0] in {" ", "+"}:
                    result.append(payload)
                    observed_new += 1
                if line[0] in {"+", "-"}:
                    changed_lines += 1
                index += 1
            if observed_old != old_count or observed_new != new_count:
                raise ValueError("candidate unified diff hunk counts differ")
            file_hunks += 1
            hunks += 1
        if file_hunks == 0:
            raise ValueError("candidate unified diff file has no hunks")
        result.extend(original[source_index:])
        output[path] = "".join(result)
    canonical_paths = tuple(path for path in _POLICY_EDITABLE_PATHS if path in paths)
    stats = _PatchStats(canonical_paths, hunks, changed_lines, diff_bytes)
    _require_patch_bounds(stats, bounds)
    if all(output[path] == source_texts[path] for path in paths):
        raise ValueError("candidate unified diff is a no-op")
    return output, stats


def initial_policy_source_bundle(
    *,
    scope: PolicySourceScope,
    source_texts: Mapping[str, str],
) -> PolicySourceBundle:
    if not isinstance(scope, PolicySourceScope) or not isinstance(source_texts, Mapping):
        raise ValueError("initial policy source inputs are invalid")
    if tuple(source_texts) != scope.editable_paths:
        raise ValueError("initial policy source paths differ from scope")
    records = tuple(
        PolicySourceRecord(
            path=path,
            sha256=hashlib.sha256(source_texts[path].encode("utf-8")).hexdigest(),
            declared_symbols=_POLICY_DECLARED_SYMBOLS[path],
            text=source_texts[path],
        )
        for path in scope.editable_paths
    )
    if tuple((record.path, record.sha256) for record in records) != (
        scope.initial_policy_source_sha256s
    ):
        raise ValueError("initial policy source hashes differ from scope")
    bundle = PolicySourceBundle(
        policy_interface_version=scope.policy_interface_version,
        cumulative_diff_sha256=hashlib.sha256(b"").hexdigest(),
        cumulative_diff="",
        files=records,
        _controller_seal=_POLICY_SOURCE_BUNDLE_SEAL,
    )
    if len(bundle.canonical_json_bytes()) > scope.max_policy_source_bundle_bytes:
        raise ValueError("initial policy source bundle exceeds scope")
    return bundle


def validate_policy_source_bundle_descendant(
    *,
    scope: PolicySourceScope,
    initial_bundle: PolicySourceBundle,
    bundle: PolicySourceBundle,
) -> None:
    if not isinstance(scope, PolicySourceScope) or not all(
        isinstance(item, PolicySourceBundle) for item in (initial_bundle, bundle)
    ):
        raise ValueError("policy source descendant contracts are invalid")
    initial_texts = {record.path: record.text for record in initial_bundle.files}
    authenticated_initial = initial_policy_source_bundle(
        scope=scope,
        source_texts=initial_texts,
    )
    if authenticated_initial != initial_bundle:
        raise ValueError("initial policy source bundle is not canonical")
    if bundle.policy_interface_version != scope.policy_interface_version:
        raise ValueError("policy source descendant interface differs from scope")
    if len(bundle.canonical_json_bytes()) > scope.max_policy_source_bundle_bytes:
        raise ValueError("policy source descendant exceeds scope")
    resulting_texts, stats = _apply_unified_diff(
        initial_texts,
        bundle.cumulative_diff,
        bounds=scope.hard_patch_bounds,
        allow_empty=True,
    )
    if stats.paths and stats.paths != tuple(
        record.path
        for record in bundle.files
        if record.text != initial_texts[record.path]
    ):
        raise ValueError("policy source cumulative diff paths differ from files")
    supplied_texts = {record.path: record.text for record in bundle.files}
    if resulting_texts != supplied_texts:
        raise ValueError("policy source cumulative diff resulting source texts differ")


def _reverse_unified_diff(unified_diff: str) -> str:
    """Reverse one already closed unified diff without trusting omitted source context."""

    output: list[str] = []
    remaining_old = 0
    remaining_new = 0
    in_hunk = False
    for line in unified_diff.splitlines(keepends=True):
        if not in_hunk:
            match = _HUNK_RE.fullmatch(line)
            if match is None:
                output.append(line)
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            closing_marker = line.find("@@", 2)
            if closing_marker < 0:
                raise ValueError("policy source cumulative diff hunk is invalid")
            suffix = line[closing_marker + 2 :]
            output.append(
                f"@@ -{new_start},{new_count} +{old_start},{old_count} @@{suffix}"
            )
            remaining_old = old_count
            remaining_new = new_count
            in_hunk = remaining_old > 0 or remaining_new > 0
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise ValueError("policy source cumulative diff body is invalid")
        prefix = line[0]
        if prefix in {" ", "-"}:
            remaining_old -= 1
        if prefix in {" ", "+"}:
            remaining_new -= 1
        if remaining_old < 0 or remaining_new < 0:
            raise ValueError("policy source cumulative diff counts are invalid")
        output.append({"+": "-", "-": "+"}.get(prefix, prefix) + line[1:])
        in_hunk = remaining_old > 0 or remaining_new > 0
    if in_hunk:
        raise ValueError("policy source cumulative diff is truncated")
    return "".join(output)


def authenticate_policy_source_bundle(
    *,
    scope: PolicySourceScope,
    bundle: PolicySourceBundle,
) -> None:
    """Bind actual initial/current policy bytes to one exact authorized source scope."""

    if not isinstance(scope, PolicySourceScope) or not isinstance(
        bundle, PolicySourceBundle
    ):
        raise ValueError("policy source authentication contracts are invalid")
    current_texts = {record.path: record.text for record in bundle.files}
    if bundle.cumulative_diff:
        initial_texts, _stats = _apply_unified_diff(
            current_texts,
            _reverse_unified_diff(bundle.cumulative_diff),
            bounds=scope.hard_patch_bounds,
        )
    else:
        initial_texts = current_texts
    try:
        initial_bundle = initial_policy_source_bundle(
            scope=scope,
            source_texts=initial_texts,
        )
    except ValueError as exc:
        raise ValueError("initial policy source hashes differ from scope") from exc
    validate_policy_source_bundle_descendant(
        scope=scope,
        initial_bundle=initial_bundle,
        bundle=bundle,
    )


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
        if value["skip"] is not False:
            raise ValueError("reasoner must choose exactly one catalog candidate")
        skip = value["skip"]
        hypothesis = _closed_text(value["hypothesis"], "reasoner hypothesis")
        candidate_id = _closed_text(value["candidate_id"], "reasoner candidate ID")
        skip_reason = _closed_text(
            value["skip_reason"], "reasoner skip reason", allow_empty=True
        )
        evidence_ids = _closed_ids(value["evidence_ids"], "reasoner evidence IDs")
        invariant_ids = _closed_ids(value["invariant_ids"], "reasoner invariant IDs")
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
                "candidate_id": {"type": "string", "enum": list(_CATALOG)},
                "skip": {"type": "boolean", "enum": [False]},
                "skip_reason": {"type": "string", "enum": [""]},
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


@dataclass(frozen=True, slots=True)
class InvestigatorArtifact(_V2Canonical):
    hypothesis_id: str
    family: str
    evidence_ids: tuple[str, ...]
    causal_rationale: str
    target_paths: tuple[str, ...]
    target_symbols: tuple[str, ...]
    expected_diagnostic_changes: tuple[str, ...]
    known_risks: tuple[str, ...]
    author_instructions: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "investigator hypothesis ID")
        if self.family not in _INVESTIGATOR_FAMILIES:
            raise ValueError("investigator family is invalid")
        _v2_string_tuple(self.evidence_ids, "investigator evidence IDs")
        _v2_text(self.causal_rationale, "investigator causal rationale")
        _v2_string_tuple(self.target_paths, "investigator target paths")
        _v2_string_tuple(self.target_symbols, "investigator target symbols")
        _validate_scoped_paths_symbols(
            self.target_paths,
            self.target_symbols,
            "investigator target",
        )
        _v2_string_tuple(
            self.expected_diagnostic_changes,
            "investigator expected diagnostic changes",
        )
        _v2_string_tuple(
            self.known_risks,
            "investigator known risks",
            allow_empty=True,
        )
        _v2_string_tuple(self.author_instructions, "investigator author instructions")
        if len(self.canonical_json_bytes()) > MAX_INVESTIGATOR_ARTIFACT_BYTES:
            raise ValueError("investigator artifact exceeds its byte cap")

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        max_total_bytes: int,
    ) -> "InvestigatorArtifact":
        value = _parse_v2_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis_id",
                    "family",
                    "evidence_ids",
                    "causal_rationale",
                    "expected_diagnostic_changes",
                    "known_risks",
                    "author_instructions",
                }
            ),
            max_total_bytes=max_total_bytes,
            optional_keys=frozenset({"target_paths", "target_symbols"}),
        )
        family = _v2_response_text(value["family"], "investigator family")
        if family not in _INVESTIGATOR_FAMILIES:
            raise ValueError("investigator family is invalid")
        target_paths, target_symbols = _controller_scope_for_family(family)
        return cls(
            hypothesis_id=_v2_response_identifier(
                value["hypothesis_id"], "investigator hypothesis ID"
            ),
            family=family,
            evidence_ids=_v2_response_string_list(
                value["evidence_ids"], "investigator evidence IDs"
            ),
            causal_rationale=_v2_response_text(
                value["causal_rationale"], "investigator causal rationale"
            ),
            target_paths=target_paths,
            target_symbols=target_symbols,
            expected_diagnostic_changes=_v2_response_string_list(
                value["expected_diagnostic_changes"],
                "investigator expected diagnostic changes",
            ),
            known_risks=_v2_response_string_list(
                value["known_risks"], "investigator known risks", allow_empty=True
            ),
            author_instructions=_v2_response_string_list(
                value["author_instructions"], "investigator author instructions"
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthorArtifact(_V2Canonical):
    hypothesis_id: str
    behavioral_summary: str
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]
    unified_diff: str
    assumptions: tuple[str, ...]
    validation_suggestions: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "author hypothesis ID")
        _v2_text(self.behavioral_summary, "author behavioral summary")
        _v2_string_tuple(self.changed_paths, "author changed paths")
        _v2_string_tuple(self.changed_symbols, "author changed symbols")
        _validate_scoped_paths_symbols(
            self.changed_paths,
            self.changed_symbols,
            "author changed",
        )
        _v2_blob(self.unified_diff, "author diff", max_bytes=MAX_AUTHOR_DIFF_BYTES)
        _v2_string_tuple(self.assumptions, "author assumptions", allow_empty=True)
        _v2_string_tuple(
            self.validation_suggestions,
            "author validation suggestions",
            allow_empty=True,
        )
        primitive = self.to_primitive()
        primitive["unified_diff"] = ""
        if len(_v2_canonical_bytes(primitive)) > MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES:
            raise ValueError("author non-diff artifact exceeds its byte cap")

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        max_diff_bytes: int,
        max_total_bytes: int,
        controller_paths: tuple[str, ...] | None = None,
        controller_symbols: tuple[str, ...] | None = None,
    ) -> "AuthorArtifact":
        if (
            type(max_diff_bytes) is not int
            or max_diff_bytes <= 0
            or max_diff_bytes > MAX_AUTHOR_DIFF_BYTES
        ):
            raise ValueError("author diff byte cap is invalid")
        value = _parse_v2_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis_id",
                    "behavioral_summary",
                    "unified_diff",
                    "assumptions",
                    "validation_suggestions",
                }
            ),
            max_total_bytes=max_total_bytes,
            optional_keys=frozenset({"changed_paths", "changed_symbols"}),
        )
        unified_diff = _v2_blob(
            value["unified_diff"], "author diff", max_bytes=max_diff_bytes
        )
        if (controller_paths is None) != (controller_symbols is None):
            raise ValueError("author controller scope is incomplete")
        if controller_paths is None:
            try:
                changed_paths = _v2_string_list(
                    value["changed_paths"], "author changed paths"
                )
                changed_symbols = _v2_string_list(
                    value["changed_symbols"], "author changed symbols"
                )
            except KeyError as exc:
                raise ValueError("author controller scope is required") from exc
        else:
            changed_paths = _v2_string_tuple(
                controller_paths, "author controller paths"
            )
            changed_symbols = _v2_string_tuple(
                controller_symbols, "author controller symbols"
            )
        hypothesis_id = _v2_response_identifier(
            value["hypothesis_id"], "author hypothesis ID"
        )
        behavioral_summary = _v2_response_text(
            value["behavioral_summary"], "author behavioral summary"
        )
        assumptions = _v2_response_string_list(
            value["assumptions"], "author assumptions", allow_empty=True
        )
        validation_suggestions = _v2_response_string_list(
            value["validation_suggestions"],
            "author validation suggestions",
            allow_empty=True,
        )
        non_diff = {
            "hypothesis_id": hypothesis_id,
            "behavioral_summary": behavioral_summary,
            "changed_paths": list(changed_paths),
            "changed_symbols": list(changed_symbols),
            "unified_diff": "",
            "assumptions": list(assumptions),
            "validation_suggestions": list(validation_suggestions),
        }
        if len(_v2_canonical_bytes(non_diff)) > MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES:
            raise ValueError("author non-diff artifact exceeds its byte cap")
        return cls(
            hypothesis_id=hypothesis_id,
            behavioral_summary=behavioral_summary,
            changed_paths=changed_paths,
            changed_symbols=changed_symbols,
            unified_diff=unified_diff,
            assumptions=assumptions,
            validation_suggestions=validation_suggestions,
        )


@dataclass(frozen=True, slots=True)
class CriticArtifact(_V2Canonical):
    hypothesis_id: str
    prediction_vs_observation: str
    causal_explanation: str
    evidence_ids: tuple[str, ...]
    disposition: str
    next_direction: str

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "critic hypothesis ID")
        _v2_text(
            self.prediction_vs_observation,
            "critic prediction versus observation",
        )
        _v2_text(self.causal_explanation, "critic causal explanation")
        _v2_string_tuple(
            self.evidence_ids, "critic evidence IDs", allow_empty=True
        )
        if self.disposition not in _CRITIC_DISPOSITIONS:
            raise ValueError("critic disposition is invalid")
        _v2_text(self.next_direction, "critic next direction")
        if len(self.canonical_json_bytes()) > MAX_CRITIC_ARTIFACT_BYTES:
            raise ValueError("critic artifact exceeds its byte cap")

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        max_total_bytes: int,
    ) -> "CriticArtifact":
        value = _parse_v2_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis_id",
                    "prediction_vs_observation",
                    "causal_explanation",
                    "evidence_ids",
                    "disposition",
                    "next_direction",
                }
            ),
            max_total_bytes=max_total_bytes,
        )
        disposition = _v2_response_text(value["disposition"], "critic disposition")
        if disposition not in _CRITIC_DISPOSITIONS:
            raise ValueError("critic disposition is invalid")
        return cls(
            hypothesis_id=_v2_response_identifier(
                value["hypothesis_id"], "critic hypothesis ID"
            ),
            prediction_vs_observation=_v2_response_text(
                value["prediction_vs_observation"],
                "critic prediction versus observation",
            ),
            causal_explanation=_v2_response_text(
                value["causal_explanation"], "critic causal explanation"
            ),
            evidence_ids=_v2_response_string_list(
                value["evidence_ids"], "critic evidence IDs", allow_empty=True
            ),
            disposition=disposition,
            next_direction=_v2_response_text(
                value["next_direction"], "critic next direction"
            ),
        )


@dataclass(frozen=True, slots=True)
class RuleSummaryRecord(_V2Canonical):
    rule_id: str
    text: str

    def __post_init__(self) -> None:
        _v2_identifier(self.rule_id, "rule summary ID")
        _v2_text(self.text, "rule summary text")


@dataclass(frozen=True, slots=True)
class StrategyRuleSummary(_V2Canonical):
    records: tuple[RuleSummaryRecord, ...]

    def __post_init__(self) -> None:
        if (
            type(self.records) is not tuple
            or not self.records
            or len(self.records) > MAX_ROLE_LIST_ITEMS
            or any(not isinstance(item, RuleSummaryRecord) for item in self.records)
        ):
            raise ValueError("strategy rule summary is invalid")
        ids = tuple(item.rule_id for item in self.records)
        if len(ids) != len(set(ids)):
            raise ValueError("strategy rule summary IDs must be unique")
        if len(self.canonical_json_bytes()) > MAX_DISCOVERY_EVIDENCE_BYTES:
            raise ValueError("strategy rule summary exceeds its byte cap")


@dataclass(frozen=True, slots=True)
class DiscoveryEvidenceSummary(_V2Canonical):
    folds: tuple[FoldAggregateSummary, ...]
    score: DiscoveryScore | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.folds) is not tuple
            or len(self.folds) != 2
            or any(not isinstance(item, FoldAggregateSummary) for item in self.folds)
            or any(not item.fold_id.startswith("discovery_") for item in self.folds)
        ):
            raise ValueError("discovery evidence must contain two discovery folds")
        fold_ids = tuple(item.fold_id for item in self.folds)
        if len(fold_ids) != len(set(fold_ids)):
            raise ValueError("discovery evidence fold IDs must be unique")
        _v2_string_tuple(self.evidence_ids, "discovery evidence IDs")
        if len(self.evidence_ids) != len(self.folds):
            raise ValueError("discovery evidence IDs must match folds")
        if self.score is not None and not isinstance(self.score, DiscoveryScore):
            raise ValueError("discovery score is invalid")
        if len(self.canonical_json_bytes()) > MAX_DISCOVERY_EVIDENCE_BYTES:
            raise ValueError("discovery evidence exceeds its byte cap")


@dataclass(frozen=True, slots=True)
class ProviderSeed(_V2Canonical):
    rule_summary: StrategyRuleSummary
    baseline_discovery: DiscoveryEvidenceSummary

    def __post_init__(self) -> None:
        if not isinstance(self.rule_summary, StrategyRuleSummary) or not isinstance(
            self.baseline_discovery, DiscoveryEvidenceSummary
        ):
            raise ValueError("provider seed is invalid")


@dataclass(frozen=True, slots=True)
class IncumbentSummary(_V2Canonical):
    candidate_identity_sha256: str | None
    accepted_iteration: int | None
    behavioral_summary: str
    discovery: DiscoveryEvidenceSummary

    def __post_init__(self) -> None:
        if self.candidate_identity_sha256 is None:
            if self.accepted_iteration is not None:
                raise ValueError("baseline incumbent cannot have an accepted iteration")
        else:
            _require_digest(
                self.candidate_identity_sha256,
                "incumbent candidate identity SHA-256",
            )
            _require_positive_int(self.accepted_iteration, "incumbent accepted iteration")
        _v2_text(self.behavioral_summary, "incumbent behavioral summary")
        if not isinstance(self.discovery, DiscoveryEvidenceSummary):
            raise ValueError("incumbent discovery evidence is invalid")


@dataclass(frozen=True, slots=True)
class AuthorManifestSummary(_V2Canonical):
    hypothesis_id: str
    behavioral_summary: str
    changed_paths: tuple[str, ...]
    changed_symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "author manifest hypothesis ID")
        _v2_text(self.behavioral_summary, "author manifest behavioral summary")
        _v2_string_tuple(self.changed_paths, "author manifest changed paths")
        _v2_string_tuple(self.changed_symbols, "author manifest changed symbols")
        _validate_scoped_paths_symbols(
            self.changed_paths,
            self.changed_symbols,
            "author manifest changed",
        )
        if len(self.canonical_json_bytes()) > MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES:
            raise ValueError("author manifest exceeds its byte cap")


@dataclass(frozen=True, slots=True)
class CandidateValidationSummary(_V2Canonical):
    failure_code: str | None
    syntax_ok: bool
    imports_ok: bool
    purity_ok: bool
    deterministic_ok: bool
    worker_ok: bool
    replay_attempted: bool

    def __post_init__(self) -> None:
        flag_names = (
            "syntax_ok",
            "imports_ok",
            "purity_ok",
            "deterministic_ok",
            "worker_ok",
            "replay_attempted",
        )
        for name in flag_names:
            _require_bool(getattr(self, name), f"candidate validation {name}")
        actual_flags = tuple(getattr(self, name) for name in flag_names)
        if self.failure_code is None:
            if actual_flags != (True, True, True, True, True, True):
                raise ValueError("candidate successful validation flags are inconsistent")
            return
        if self.failure_code not in CANDIDATE_VALIDATION_FAILURE_CODES:
            raise ValueError("candidate validation failure code is not closed")
        if actual_flags != _VALIDATION_FAILURE_FLAGS[self.failure_code]:
            raise ValueError(
                f"candidate {self.failure_code} flags are inconsistent"
            )


@dataclass(frozen=True, slots=True)
class PolicySourceMaterialization(_V2Canonical):
    bundle: PolicySourceBundle | None
    validation: CandidateValidationSummary

    def __post_init__(self) -> None:
        if self.bundle is not None and not isinstance(self.bundle, PolicySourceBundle):
            raise ValueError("policy source materialization bundle is invalid")
        if not isinstance(self.validation, CandidateValidationSummary):
            raise ValueError("policy source materialization validation is invalid")
        if self.bundle is None:
            if self.validation.failure_code != "next_context_oversize":
                raise ValueError("missing policy source bundle requires context overflow")
        elif self.validation.failure_code is not None:
            raise ValueError("failed policy source materialization cannot carry a bundle")


def _successful_candidate_validation() -> CandidateValidationSummary:
    return CandidateValidationSummary(
        failure_code=None,
        syntax_ok=True,
        imports_ok=True,
        purity_ok=True,
        deterministic_ok=True,
        worker_ok=True,
        replay_attempted=True,
    )


def _controller_cumulative_diff(
    initial_texts: Mapping[str, str],
    resulting_texts: Mapping[str, str],
) -> str:
    return "".join(
        line
        for path in _POLICY_EDITABLE_PATHS
        if initial_texts[path] != resulting_texts[path]
        for line in difflib.unified_diff(
            initial_texts[path].splitlines(keepends=True),
            resulting_texts[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )


def materialize_policy_source_descendant(
    *,
    scope: PolicySourceScope,
    initial_bundle: PolicySourceBundle,
    current_bundle: PolicySourceBundle,
    artifact: AuthorArtifact,
    immutable_constraint_ids: tuple[str, ...],
    call_budgets: tuple[PitOptimizerCallBudget, ...],
) -> PolicySourceMaterialization:
    if not isinstance(artifact, AuthorArtifact):
        raise ValueError("policy source materialization author artifact is invalid")
    validate_policy_source_bundle_descendant(
        scope=scope,
        initial_bundle=initial_bundle,
        bundle=current_bundle,
    )
    current_texts = {record.path: record.text for record in current_bundle.files}
    resulting_texts, stats = _apply_unified_diff(
        current_texts,
        artifact.unified_diff,
        bounds=scope.candidate_bounds,
    )
    if stats.paths != artifact.changed_paths:
        raise ValueError("author changed paths differ from candidate unified diff")
    initial_texts = {record.path: record.text for record in initial_bundle.files}
    cumulative_diff = _controller_cumulative_diff(initial_texts, resulting_texts)
    cumulative_texts, _stats = _apply_unified_diff(
        initial_texts,
        cumulative_diff,
        bounds=scope.hard_patch_bounds,
        allow_empty=False,
    )
    if cumulative_texts != resulting_texts:
        raise ValueError("controller cumulative diff does not reproduce candidate source")
    records = tuple(
        PolicySourceRecord(
            path=path,
            sha256=hashlib.sha256(resulting_texts[path].encode("utf-8")).hexdigest(),
            declared_symbols=_POLICY_DECLARED_SYMBOLS[path],
            text=resulting_texts[path],
        )
        for path in scope.editable_paths
    )
    primitive = {
        "policy_interface_version": scope.policy_interface_version,
        "cumulative_diff_sha256": hashlib.sha256(
            cumulative_diff.encode("utf-8")
        ).hexdigest(),
        "cumulative_diff": cumulative_diff,
        "files": records,
    }
    if len(_v2_canonical_bytes(primitive)) > scope.max_policy_source_bundle_bytes:
        return _next_context_oversize_materialization()
    bundle = PolicySourceBundle(
        **primitive,
        _controller_seal=_POLICY_SOURCE_BUNDLE_SEAL,
    )
    validate_policy_source_bundle_descendant(
        scope=scope,
        initial_bundle=initial_bundle,
        bundle=bundle,
    )
    try:
        render_worst_iteration_two_role_inputs(
            scope=scope,
            source_texts=resulting_texts,
            immutable_constraint_ids=immutable_constraint_ids,
            call_budgets=call_budgets,
            prospective_source_bundle=bundle,
        )
    except ValueError as exc:
        if str(exc).startswith("worst iteration-2"):
            return _next_context_oversize_materialization()
        raise
    return PolicySourceMaterialization(
        bundle=bundle,
        validation=_successful_candidate_validation(),
    )


def _next_context_oversize_materialization() -> PolicySourceMaterialization:
    return PolicySourceMaterialization(
        bundle=None,
        validation=CandidateValidationSummary(
            failure_code="next_context_oversize",
            syntax_ok=True,
            imports_ok=True,
            purity_ok=True,
            deterministic_ok=True,
            worker_ok=True,
            replay_attempted=True,
        ),
    )


@dataclass(frozen=True, slots=True)
class CandidateComparisonSummary(_V2Canonical):
    folds: tuple[FoldAggregateSummary, ...]
    score: DiscoveryScore | None
    diagnostics: tuple[AggregateMetric, ...]
    _controller_seal: InitVar[object] = None

    def __post_init__(self, _controller_seal: object) -> None:
        if _controller_seal is not _CANDIDATE_COMPARISON_SEAL:
            raise ValueError("candidate comparison must be controller derived")
        if (
            type(self.folds) is not tuple
            or len(self.folds) != 2
            or any(not isinstance(item, FoldAggregateSummary) for item in self.folds)
        ):
            raise ValueError("candidate comparison folds are invalid")
        if tuple(item.fold_id for item in self.folds) != (
            "discovery_1",
            "discovery_2",
        ):
            raise ValueError("candidate comparison must use sealed discovery fold IDs")
        if self.score is not None and not isinstance(self.score, DiscoveryScore):
            raise ValueError("candidate comparison score is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) > MAX_ROLE_LIST_ITEMS
            or any(not isinstance(item, AggregateMetric) for item in self.diagnostics)
        ):
            raise ValueError("candidate comparison diagnostics are invalid")
        ids = tuple(item.metric_id for item in self.diagnostics)
        if len(ids) != len(set(ids)):
            raise ValueError("candidate comparison diagnostic IDs must be unique")


def candidate_comparison_from_fixed_baseline(
    *,
    candidate_folds: tuple[FoldAggregateSummary, ...],
    original_baseline_folds: tuple[FoldAggregateSummary, ...],
    original_baseline_sha256: str,
    expected_original_baseline_sha256: str,
    discovery_exposure: DiscoveryExposureProof,
    expected_window_identities: tuple[
        ValidationWindowIdentity,
        ValidationWindowIdentity,
    ],
    expected_metadata: ValidationExposureMetadata,
    diagnostics: tuple[AggregateMetric, ...],
    supplied_score: DiscoveryScore | None = None,
) -> CandidateComparisonSummary:
    if not isinstance(discovery_exposure, DiscoveryExposureProof):
        raise ValueError("candidate comparison discovery exposure is invalid")
    if (
        type(expected_window_identities) is not tuple
        or len(expected_window_identities) != 2
        or any(
            not isinstance(item, ValidationWindowIdentity)
            for item in expected_window_identities
        )
        or discovery_exposure.window_identities != expected_window_identities
    ):
        raise ValueError("candidate comparison expected window identities differ")
    if (
        not isinstance(expected_metadata, ValidationExposureMetadata)
        or discovery_exposure.metadata != expected_metadata
    ):
        raise ValueError("candidate comparison expected metadata lineage differs")
    if tuple(item.fold_id for item in candidate_folds) != discovery_exposure.fold_ids:
        raise ValueError("candidate comparison folds differ from ledger exposure")
    score = discovery_score_from_folds(
        candidate_folds,
        original_baseline_folds,
        original_baseline_sha256=original_baseline_sha256,
        expected_original_baseline_sha256=expected_original_baseline_sha256,
    )
    if supplied_score is not None and supplied_score != score:
        raise ValueError("candidate comparison supplied score differs from fixed baseline")
    derived_folds = tuple(
        replace(
            candidate,
            excess_total_return_pp=(
                float(candidate.total_return_pct)
                - float(original_baseline.total_return_pct)
            ),
        )
        for candidate, original_baseline in zip(
            candidate_folds,
            original_baseline_folds,
            strict=True,
        )
    )
    return CandidateComparisonSummary(
        folds=derived_folds,
        score=score,
        diagnostics=diagnostics,
        _controller_seal=_CANDIDATE_COMPARISON_SEAL,
    )


@dataclass(frozen=True, slots=True)
class IterationFeedbackSummary(_V2Canonical):
    iteration: int
    hypothesis_id: str
    family: str
    author_summary: str
    validation_code: str
    candidate_folds: tuple[FoldAggregateSummary, ...]
    discovery_score: DiscoveryScore | None
    critic_disposition: str
    critic_next_direction: str
    incumbent_changed: bool

    def __post_init__(self) -> None:
        _require_positive_int(self.iteration, "feedback iteration")
        _v2_identifier(self.hypothesis_id, "feedback hypothesis ID")
        if self.family not in _INVESTIGATOR_FAMILIES:
            raise ValueError("feedback family is invalid")
        _v2_text(self.author_summary, "feedback author summary")
        _v2_identifier(self.validation_code, "feedback validation code")
        if (
            type(self.candidate_folds) is not tuple
            or len(self.candidate_folds) not in {0, 2}
            or any(
                not isinstance(item, FoldAggregateSummary)
                for item in self.candidate_folds
            )
        ):
            raise ValueError("feedback candidate folds are invalid")
        if self.candidate_folds and tuple(
            item.fold_id for item in self.candidate_folds
        ) != ("discovery_1", "discovery_2"):
            raise ValueError("feedback candidate fold identities are invalid")
        if self.discovery_score is not None and not isinstance(
            self.discovery_score, DiscoveryScore
        ):
            raise ValueError("feedback discovery score is invalid")
        if self.critic_disposition not in _CRITIC_DISPOSITIONS:
            raise ValueError("feedback critic disposition is invalid")
        _v2_text(self.critic_next_direction, "feedback critic next direction")
        _require_bool(self.incumbent_changed, "feedback incumbent_changed")
        if len(self.canonical_json_bytes()) > MAX_ITERATION_FEEDBACK_BYTES:
            raise ValueError("iteration feedback exceeds its byte cap")


def _validate_role_common(
    *,
    schema_version: int,
    iteration: int,
    run_manifest_sha256: str,
    immutable_constraint_ids: tuple[str, ...],
) -> None:
    if schema_version != 2:
        raise ValueError("optimizer role input schema is unsupported")
    _require_positive_int(iteration, "optimizer role iteration")
    _require_digest(run_manifest_sha256, "optimizer role run manifest SHA-256")
    _v2_string_tuple(immutable_constraint_ids, "immutable constraint IDs")


@dataclass(frozen=True, slots=True)
class InvestigatorInput(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    policy_interface_version: int
    immutable_constraint_ids: tuple[str, ...]
    candidate_bounds: PatchBounds
    rule_summary: StrategyRuleSummary
    source_bundle: PolicySourceBundle
    baseline_discovery: DiscoveryEvidenceSummary
    incumbent_summary: IncumbentSummary
    prior_iterations: tuple[IterationFeedbackSummary, ...]

    def __post_init__(self) -> None:
        _validate_role_common(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            immutable_constraint_ids=self.immutable_constraint_ids,
        )
        _require_positive_int(self.policy_interface_version, "policy interface version")
        if not isinstance(self.candidate_bounds, PatchBounds):
            raise ValueError("investigator candidate bounds are invalid")
        if not isinstance(self.rule_summary, StrategyRuleSummary):
            raise ValueError("investigator rule summary is invalid")
        if not isinstance(self.source_bundle, PolicySourceBundle):
            raise ValueError("investigator source bundle is invalid")
        if self.source_bundle.policy_interface_version != self.policy_interface_version:
            raise ValueError("investigator policy interface versions differ")
        if not isinstance(self.baseline_discovery, DiscoveryEvidenceSummary):
            raise ValueError("investigator baseline discovery is invalid")
        if not isinstance(self.incumbent_summary, IncumbentSummary):
            raise ValueError("investigator incumbent summary is invalid")
        if (
            type(self.prior_iterations) is not tuple
            or len(self.prior_iterations) > 8
            or any(
                not isinstance(item, IterationFeedbackSummary)
                for item in self.prior_iterations
            )
        ):
            raise ValueError("investigator prior iterations may contain at most 8 summaries")
        iterations = tuple(item.iteration for item in self.prior_iterations)
        if iterations != tuple(range(1, self.iteration)):
            raise ValueError("investigator prior iterations must be a contiguous prefix")
        if len(_v2_canonical_bytes(self.prior_iterations)) > MAX_ITERATION_HISTORY_BYTES:
            raise ValueError("investigator iteration history exceeds its byte cap")
        if len(self.canonical_json_bytes()) > MAX_INVESTIGATOR_DYNAMIC_BYTES:
            raise ValueError("investigator dynamic input exceeds its byte cap")


@dataclass(frozen=True, slots=True)
class AuthorInput(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    policy_interface_version: int
    immutable_constraint_ids: tuple[str, ...]
    candidate_bounds: PatchBounds
    investigator: InvestigatorArtifact
    source_bundle: PolicySourceBundle

    def __post_init__(self) -> None:
        _validate_role_common(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            immutable_constraint_ids=self.immutable_constraint_ids,
        )
        _require_positive_int(self.policy_interface_version, "policy interface version")
        if not isinstance(self.candidate_bounds, PatchBounds):
            raise ValueError("author candidate bounds are invalid")
        if not isinstance(self.investigator, InvestigatorArtifact):
            raise ValueError("author investigator artifact is invalid")
        if not isinstance(self.source_bundle, PolicySourceBundle):
            raise ValueError("author source bundle is invalid")
        if self.source_bundle.policy_interface_version != self.policy_interface_version:
            raise ValueError("author policy interface versions differ")
        if len(self.canonical_json_bytes()) > MAX_AUTHOR_DYNAMIC_BYTES:
            raise ValueError("author dynamic input exceeds its byte cap")

    def validate_artifact(self, artifact: AuthorArtifact) -> None:
        if not isinstance(artifact, AuthorArtifact):
            raise ValueError("author response has an invalid type")
        # The closed response parser validates artifact shape.  Applicability,
        # scope, and identity are derived later from the authenticated Git
        # checkout, which is the sole authority for candidate acceptance.
        # Reapplying a text diff here can change line-ending semantics and
        # reject a proposal Git correctly accepts.


@dataclass(frozen=True, slots=True)
class CriticInput(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    immutable_constraint_ids: tuple[str, ...]
    hypothesis_id: str
    investigator_summary: InvestigatorArtifact
    author_manifest: AuthorManifestSummary
    validation: CandidateValidationSummary
    candidate_vs_baseline: CandidateComparisonSummary | None
    candidate_vs_incumbent: CandidateComparisonSummary | None

    def __post_init__(self) -> None:
        _validate_role_common(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            immutable_constraint_ids=self.immutable_constraint_ids,
        )
        _v2_identifier(self.hypothesis_id, "critic input hypothesis ID")
        if not isinstance(self.investigator_summary, InvestigatorArtifact):
            raise ValueError("critic investigator summary is invalid")
        if not isinstance(self.author_manifest, AuthorManifestSummary):
            raise ValueError("critic author manifest is invalid")
        if not isinstance(self.validation, CandidateValidationSummary):
            raise ValueError("critic validation summary is invalid")
        if not all(
            item is None or isinstance(item, CandidateComparisonSummary)
            for item in (self.candidate_vs_baseline, self.candidate_vs_incumbent)
        ):
            raise ValueError("critic candidate comparison is invalid")
        if not (
            self.hypothesis_id
            == self.investigator_summary.hypothesis_id
            == self.author_manifest.hypothesis_id
        ):
            raise ValueError("critic hypothesis IDs differ")
        if len(self.canonical_json_bytes()) > MAX_CRITIC_DYNAMIC_BYTES:
            raise ValueError("critic dynamic input exceeds its byte cap")

    def validate_artifact(self, artifact: CriticArtifact) -> None:
        if not isinstance(artifact, CriticArtifact):
            raise ValueError("critic response has an invalid type")
        if artifact.hypothesis_id != self.hypothesis_id:
            raise ValueError("critic hypothesis differs from its input")


def render_worst_iteration_two_role_inputs(
    *,
    scope: PolicySourceScope,
    source_texts: Mapping[str, str],
    immutable_constraint_ids: tuple[str, ...],
    call_budgets: tuple[PitOptimizerCallBudget, ...],
    prospective_source_bundle: PolicySourceBundle | None = None,
) -> Mapping[str, bytes]:
    """Render complete bounded iteration-2 role inputs before a manifest is sealed."""

    if not isinstance(scope, PolicySourceScope):
        raise ValueError("worst role input source scope is invalid")
    _v2_string_tuple(immutable_constraint_ids, "worst role immutable constraints")
    if tuple(source_texts) != scope.editable_paths:
        raise ValueError("worst role source paths differ from scope")
    if scope.max_iterations == 1:
        if any(budget.iteration != 1 for budget in call_budgets):
            raise ValueError("single-iteration call plan contains a later iteration")
        return MappingProxyType({})
    if prospective_source_bundle is None:
        grown_texts = dict(source_texts)
        worst_raw_text = _v2_max_canonical_text(
            scope.candidate_bounds.max_diff_bytes
        )
        grown_texts[scope.editable_paths[0]] += worst_raw_text
        cumulative_diff = worst_raw_text
        source_records = tuple(
            PolicySourceRecord(
                path=path,
                sha256=hashlib.sha256(grown_texts[path].encode("utf-8")).hexdigest(),
                declared_symbols=_POLICY_DECLARED_SYMBOLS[path],
                text=grown_texts[path],
            )
            for path in scope.editable_paths
        )
        source_primitive = {
            "policy_interface_version": scope.policy_interface_version,
            "cumulative_diff_sha256": hashlib.sha256(
                cumulative_diff.encode("utf-8")
            ).hexdigest(),
            "cumulative_diff": cumulative_diff,
            "files": source_records,
        }
        if (
            len(_v2_canonical_bytes(source_primitive))
            > scope.max_policy_source_bundle_bytes
        ):
            raise ValueError("worst iteration-2 source bundle exceeds source cap")
        source_bundle = PolicySourceBundle(
            **source_primitive,
            _controller_seal=_POLICY_SOURCE_BUNDLE_SEAL,
        )
    else:
        if not isinstance(prospective_source_bundle, PolicySourceBundle):
            raise ValueError("worst role prospective source bundle is invalid")
        if (
            prospective_source_bundle.policy_interface_version
            != scope.policy_interface_version
            or tuple(record.path for record in prospective_source_bundle.files)
            != scope.editable_paths
            or {
                record.path: record.text
                for record in prospective_source_bundle.files
            }
            != dict(source_texts)
        ):
            raise ValueError("worst role prospective source bundle differs from source")
        if (
            len(prospective_source_bundle.canonical_json_bytes())
            > scope.max_policy_source_bundle_bytes
        ):
            raise ValueError("worst iteration-2 source bundle exceeds source cap")
        source_bundle = prospective_source_bundle
    folds = tuple(
        FoldAggregateSummary(
            fold_id=fold_id,
            total_return_pct=1.0,
            excess_total_return_pp=0.0,
            max_drawdown_pct=-1.0,
            sharpe_ratio=1.0,
            closed_trades=1,
            turnover_pct=1.0,
            average_exposure_pct=1.0,
            entry_funnel=(AggregateMetric("entries_executed", 1),),
            exit_attribution=(AggregateMetric("end_of_test", 1),),
        )
        for fold_id in ("discovery_1", "discovery_2")
    )

    def maximize_contract(factory: object, cap: int) -> object:
        if not callable(factory):
            raise ValueError("worst role section factory is invalid")
        best: object | None = None
        best_size = -1
        low = 0
        high = MAX_ROLE_TEXT_BYTES
        while low <= high:
            count = (low + high) // 2
            try:
                candidate = factory(count)
                size = len(_v2_canonical_bytes(candidate))
            except ValueError:
                size = cap + 1
                candidate = None
            if size <= cap:
                best = candidate
                best_size = size
                low = count + 1
            else:
                high = count - 1
        if best is None or best_size < cap - 1:
            raise ValueError("worst role section cannot reach its canonical cap")
        return best

    def maximized(count: int) -> str:
        return _v2_max_canonical_text(count)
    rule_summary = maximize_contract(
        lambda count: StrategyRuleSummary(
            records=(RuleSummaryRecord("rule_1", "r" + maximized(count)),)
        ),
        MAX_DISCOVERY_EVIDENCE_BYTES,
    )
    discovery = maximize_contract(
        lambda count: DiscoveryEvidenceSummary(
            folds=folds,
            score=None,
            evidence_ids=("e" + maximized(count), "evidence_2"),
        ),
        MAX_DISCOVERY_EVIDENCE_BYTES,
    )
    incumbent = IncumbentSummary(
        candidate_identity_sha256="1" * 64,
        accepted_iteration=1,
        behavioral_summary=_v2_max_canonical_text(MAX_ROLE_TEXT_BYTES),
        discovery=discovery,
    )
    feedback = maximize_contract(
        lambda count: IterationFeedbackSummary(
            iteration=1,
            hypothesis_id="hypothesis_1",
            family="entry",
            author_summary="a" + maximized(count),
            validation_code="valid",
            candidate_folds=folds,
            discovery_score=None,
            critic_disposition="refine",
            critic_next_direction="next",
            incumbent_changed=True,
        ),
        scope.max_iteration_feedback_bytes,
    )
    investigator_artifact = maximize_contract(
        lambda count: InvestigatorArtifact(
            hypothesis_id="hypothesis_2",
            family="entry",
            evidence_ids=("evidence_1",),
            causal_rationale="c" + maximized(count),
            target_paths=(scope.editable_paths[0],),
            target_symbols=(_POLICY_DECLARED_SYMBOLS[scope.editable_paths[0]][0],),
            expected_diagnostic_changes=("diagnostic",),
            known_risks=("risk",),
            author_instructions=("instruction",),
        ),
        MAX_INVESTIGATOR_ARTIFACT_BYTES,
    )
    author_manifest = maximize_contract(
        lambda count: AuthorManifestSummary(
            hypothesis_id=investigator_artifact.hypothesis_id,
            behavioral_summary="b" + maximized(count),
            changed_paths=(scope.editable_paths[0],),
            changed_symbols=(_POLICY_DECLARED_SYMBOLS[scope.editable_paths[0]][0],),
        ),
        MAX_AUTHOR_NON_DIFF_ARTIFACT_BYTES,
    )
    synthetic_baseline_sha256 = hashlib.sha256(
        _v2_canonical_bytes([_v2_primitive(item) for item in folds]) + b"\n"
    ).hexdigest()
    comparison_score = discovery_score_from_folds(
        folds,
        folds,
        original_baseline_sha256=synthetic_baseline_sha256,
        expected_original_baseline_sha256=synthetic_baseline_sha256,
    )
    comparison = maximize_contract(
        lambda count: CandidateComparisonSummary(
            folds=folds,
            score=comparison_score,
            diagnostics=(AggregateMetric("d" + maximized(count), 1),),
            _controller_seal=_CANDIDATE_COMPARISON_SEAL,
        ),
        MAX_DISCOVERY_EVIDENCE_BYTES,
    )
    validation = _successful_candidate_validation()
    dynamic_values = {
        "investigator": {
            "schema_version": 2,
            "iteration": 2,
            "run_manifest_sha256": "0" * 64,
            "policy_interface_version": scope.policy_interface_version,
            "immutable_constraint_ids": immutable_constraint_ids,
            "candidate_bounds": scope.candidate_bounds,
            "rule_summary": rule_summary,
            "source_bundle": source_bundle,
            "baseline_discovery": discovery,
            "incumbent_summary": incumbent,
            "prior_iterations": (feedback,),
        },
        "author": {
            "schema_version": 2,
            "iteration": 2,
            "run_manifest_sha256": "0" * 64,
            "policy_interface_version": scope.policy_interface_version,
            "immutable_constraint_ids": immutable_constraint_ids,
            "candidate_bounds": scope.candidate_bounds,
            "investigator": investigator_artifact,
            "source_bundle": source_bundle,
        },
        "critic": {
            "schema_version": 2,
            "iteration": 2,
            "run_manifest_sha256": "0" * 64,
            "immutable_constraint_ids": immutable_constraint_ids,
            "hypothesis_id": investigator_artifact.hypothesis_id,
            "investigator_summary": investigator_artifact,
            "author_manifest": author_manifest,
            "validation": validation,
            "candidate_vs_baseline": comparison,
            "candidate_vs_incumbent": comparison,
        },
    }
    rendered = {
        role: _v2_canonical_bytes(value) for role, value in dynamic_values.items()
    }
    iteration_two = {
        budget.role: budget
        for budget in call_budgets
        if budget.iteration == 2
    }
    if set(iteration_two) != set(OPTIMIZER_V2_ROLES):
        raise ValueError("worst iteration-2 call plan is incomplete")
    for role, payload in rendered.items():
        budget = iteration_two[role]
        static_bytes = len(PIT_OPTIMIZER_V2_SYSTEM_PROMPTS[role].encode("utf-8"))
        static_bytes += len(_v2_canonical_bytes(pit_optimizer_response_format(role)))
        if len(payload) > budget.max_dynamic_input_bytes:
            raise ValueError(f"worst iteration-2 {role} dynamic input exceeds call cap")
        if static_bytes + len(payload) > budget.max_input_tokens:
            raise ValueError(f"worst iteration-2 {role} message exceeds token cap")
    return MappingProxyType(rendered)


def _v2_string_schema(
    *,
    max_length: int = MAX_ROLE_TEXT_BYTES,
    min_length: int | None = None,
    pattern: str | None = None,
    description: str | None = None,
) -> dict[str, object]:
    """Build a provider-facing string schema with explicit compact bounds."""

    if type(max_length) is not int or max_length < 1:
        raise ValueError("response schema string maximum is invalid")
    if min_length is not None and (
        type(min_length) is not int or min_length < 0 or min_length > max_length
    ):
        raise ValueError("response schema string minimum is invalid")
    value: dict[str, object] = {"type": "string", "maxLength": max_length}
    if min_length is not None:
        value["minLength"] = min_length
    if pattern is not None:
        value["pattern"] = pattern
    if description is not None:
        value["description"] = description
    return value


def _v2_list_schema(
    *,
    max_items: int = MAX_ROLE_LIST_ITEMS,
    min_items: int | None = None,
    items: dict[str, object] | None = None,
    description: str | None = None,
) -> dict[str, object]:
    """Build a provider-facing unique-list schema with explicit item bounds."""

    if type(max_items) is not int or max_items < 1:
        raise ValueError("response schema list maximum is invalid")
    if min_items is not None and (
        type(min_items) is not int or min_items < 0 or min_items > max_items
    ):
        raise ValueError("response schema list minimum is invalid")
    value: dict[str, object] = {
        "type": "array",
        "maxItems": max_items,
        "uniqueItems": True,
        "items": items or _v2_string_schema(),
    }
    if min_items is not None:
        value["minItems"] = min_items
    if description is not None:
        value["description"] = description
    return value


def _v2_identifier_schema(description: str) -> dict[str, object]:
    return _v2_string_schema(
        max_length=128,
        min_length=1,
        pattern=r"^[a-z][a-z0-9_.-]{0,127}$",
        description=description,
    )


def _v2_compact_text_list_schema(
    description: str,
    *,
    allow_empty: bool = False,
) -> dict[str, object]:
    return _v2_list_schema(
        max_items=MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
        min_items=0 if allow_empty else 1,
        items=_v2_string_schema(
            max_length=MAX_INVESTIGATOR_LIST_ITEM_CHARS,
            min_length=1,
            description="One concise controller-safe item.",
        ),
        description=description,
    )


def _v2_scoped_name_list_schema(
    names: tuple[str, ...],
    description: str,
    *,
    max_items: int = MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
) -> dict[str, object]:
    return _v2_list_schema(
        max_items=max_items,
        min_items=1,
        items={
            "type": "string",
            "enum": list(names),
            "description": "One value copied verbatim from the supplied scope.",
        },
        description=description,
    )


_V2_RESPONSE_SCHEMAS = MappingProxyType(
    {
        "investigator": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "family",
                "evidence_ids",
                "causal_rationale",
                "expected_diagnostic_changes",
                "known_risks",
                "author_instructions",
            ],
            "properties": {
                "hypothesis_id": _v2_identifier_schema(
                    "A stable lower-case identifier for this hypothesis."
                ),
                "family": {
                    "type": "string",
                    "enum": list(_INVESTIGATOR_FAMILIES),
                    "description": "The one editable strategy family to investigate.",
                },
                "evidence_ids": _v2_list_schema(
                    max_items=MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
                    min_items=1,
                    items=_v2_identifier_schema(
                        "An evidence ID copied verbatim from the supplied aggregate evidence."
                    ),
                    description="Unique aggregate-evidence IDs supporting the hypothesis.",
                ),
                "causal_rationale": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                    description="A concise causal explanation grounded in the supplied aggregates.",
                ),
                "expected_diagnostic_changes": _v2_compact_text_list_schema(
                    "Concrete aggregate diagnostics expected to change if the hypothesis is right."
                ),
                "known_risks": _v2_compact_text_list_schema(
                    "Known aggregate-only risks; use an empty list when none apply.",
                    allow_empty=True,
                ),
                "author_instructions": _v2_compact_text_list_schema(
                    "Bounded implementation instructions for the author role."
                ),
            },
        },
        "author": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "behavioral_summary",
                "unified_diff",
                "assumptions",
                "validation_suggestions",
            ],
            "properties": {
                "hypothesis_id": _v2_identifier_schema(
                    "The investigator hypothesis ID copied verbatim."
                ),
                "behavioral_summary": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                    description="A concise summary of the resulting policy behavior.",
                ),
                "unified_diff": {
                    "type": "string",
                    "maxLength": MAX_AUTHOR_DIFF_BYTES,
                    "minLength": 1,
                    "description": (
                        "A unified diff limited by the supplied candidate_bounds.max_diff_bytes."
                    ),
                },
                "assumptions": _v2_compact_text_list_schema(
                    "Optional bounded assumptions; use an empty list when none apply.",
                    allow_empty=True,
                ),
                "validation_suggestions": _v2_compact_text_list_schema(
                    "Optional bounded validation checks; use an empty list when none apply.",
                    allow_empty=True,
                ),
            },
        },
        "critic": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "prediction_vs_observation",
                "causal_explanation",
                "evidence_ids",
                "disposition",
                "next_direction",
            ],
            "properties": {
                "hypothesis_id": _v2_identifier_schema(
                    "The investigator hypothesis ID copied verbatim."
                ),
                "prediction_vs_observation": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                    description="A concise comparison of the prediction and observed aggregates.",
                ),
                "causal_explanation": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                    description="A concise causal explanation grounded in the supplied aggregates.",
                ),
                "evidence_ids": _v2_list_schema(
                    max_items=MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
                    min_items=0,
                    items=_v2_identifier_schema(
                        "An evidence ID copied verbatim from the supplied aggregate evidence."
                    ),
                    description=(
                        "Unique aggregate-evidence IDs supporting the critique; use [] "
                        "when local validation supplies no candidate comparison."
                    ),
                ),
                "disposition": {
                    "type": "string",
                    "enum": list(_CRITIC_DISPOSITIONS),
                    "description": "The controller-safe advisory disposition.",
                },
                "next_direction": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                    description="One concise aggregate-only next direction.",
                ),
            },
        },
    }
)


def pit_optimizer_response_format(role: str) -> dict[str, object]:
    if role in OPTIMIZER_V2_ROLES:
        # Every role is parsed, bounded, and input-bound locally before the
        # controller can consume it.  Provider-side JSON Schema is therefore a
        # brittle transport constraint, not a trust boundary.  Request JSON
        # objects and retain the same strict local acceptance checks.
        return {"type": "json_object"}
    try:
        schema = _V2_RESPONSE_SCHEMAS[role]
    except KeyError as exc:
        raise ValueError("unknown PIT optimizer v2 role") from exc
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"pit_optimizer_{role}_v2",
            "strict": True,
            "schema": json.loads(json.dumps(schema, separators=(",", ":"))),
        },
    }
