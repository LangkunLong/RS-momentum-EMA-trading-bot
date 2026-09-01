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
    AnnualizedReturnTarget,
    DiscoveryPanelPlan,
    DiscoveryExposureProof,
    DiscoveryScore,
    FoldAggregateSummary,
    FoldManifest,
    PanelAggregateSummary,
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
    """Parse model-authored text lists into a bounded canonical sequence.

    Repeated or blank list entries carry no additional strategy meaning, and
    reasoning models occasionally repeat them even when asked for compact JSON.
    Normalize that harmless presentation drift here while preserving the hard
    type, text, item-count, byte, and non-empty requirements consumed by the
    controller.
    """

    if value is None and allow_empty:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    normalized_items: list[str] = []
    seen: set[str] = set()
    for item in value:
        stripped = item.strip()
        if not stripped:
            continue
        normalized_item = _v2_response_text(stripped, field)
        if normalized_item in seen:
            continue
        seen.add(normalized_item)
        normalized_items.append(normalized_item)
        if len(normalized_items) == MAX_ROLE_LIST_ITEMS:
            break
    normalized = tuple(normalized_items)
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
    """Canonicalize harmless model formatting in a response-local label."""

    text = _v2_response_text(value, field)
    if _ID_RE.fullmatch(text) is not None:
        return text
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", text.casefold()).strip("._-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"h-{normalized}" if normalized else "hypothesis"
    normalized = normalized[:128].rstrip("._-")
    return _v2_identifier(normalized, field)


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
    legacy_extended_profile = {
        "investigator": (8_000, 78_000, 86_000, 16_000, 8 * 1024),
        "author": (12_000, 48_500, 72_000, 14_000, 16 * 1024),
        "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024),
    }
    extended_profile = {
        "investigator": (8_000, 78_000, 86_000, 16_000, 8 * 1024),
        "author": (12_000, 48_500, 72_000, 14_000, 16 * 1024),
        # Reserve the same reasoning headroom for the critic. OpenRouter's
        # completion usage includes DeepSeek R1's hidden reasoning tokens even
        # when the visible, schema-bound critic artifact remains small.
        "critic": (8_000, 24_000, 32_000, 16_000, 8 * 1024),
    }
    if (max_iterations, len(call_budgets)) == (1, 3):
        expected_profiles = (fast_e2e_profile, author_reasoning_profile)
    elif 2 <= max_iterations <= 8 and len(call_budgets) == 3 * max_iterations:
        expected_profiles = (legacy_extended_profile, extended_profile)
    else:
        raise ValueError("subset canary iteration profile is unsupported")
    if any(budget.model != PIT_OPTIMIZER_R1_MODEL for budget in call_budgets) or not any(
        all(
            (
                budget.max_static_input_bytes,
                budget.max_dynamic_input_bytes,
                budget.max_input_tokens,
                budget.max_output_tokens,
                budget.max_response_bytes,
            )
            == profile[budget.role]
            for budget in call_budgets
        )
        for profile in expected_profiles
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
        _v2_string_tuple(
            self.evidence_ids,
            "investigator evidence IDs",
            allow_empty=True,
        )
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
            allow_empty=True,
        )
        _v2_string_tuple(
            self.known_risks,
            "investigator known risks",
            allow_empty=True,
        )
        _v2_string_tuple(
            self.author_instructions,
            "investigator author instructions",
            allow_empty=True,
        )
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
        family = (
            _v2_response_text(value["family"], "investigator family")
            .casefold()
            .replace("-", "_")
            .replace(" ", "_")
        )
        family = {
            "entries": "entry",
            "entry_policy": "entry",
            "exits": "exit",
            "exit_policy": "exit",
            "risk": "risk_sizing",
            "sizing": "risk_sizing",
            "risk_management": "risk_sizing",
        }.get(family, family)
        if family not in _INVESTIGATOR_FAMILIES:
            raise ValueError("investigator family is invalid")
        target_paths, target_symbols = _controller_scope_for_family(family)
        return cls(
            hypothesis_id=_v2_response_identifier(
                value["hypothesis_id"], "investigator hypothesis ID"
            ),
            family=family,
            evidence_ids=_v2_response_string_list(
                value["evidence_ids"],
                "investigator evidence IDs",
                allow_empty=True,
            ),
            causal_rationale=_v2_response_text(
                value["causal_rationale"], "investigator causal rationale"
            ),
            target_paths=target_paths,
            target_symbols=target_symbols,
            expected_diagnostic_changes=_v2_response_string_list(
                value["expected_diagnostic_changes"],
                "investigator expected diagnostic changes",
                allow_empty=True,
            ),
            known_risks=_v2_response_string_list(
                value["known_risks"], "investigator known risks", allow_empty=True
            ),
            author_instructions=_v2_response_string_list(
                value["author_instructions"],
                "investigator author instructions",
                allow_empty=True,
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
        disposition = (
            _v2_response_text(value["disposition"], "critic disposition")
            .casefold()
            .replace("-", "_")
            .replace(" ", "_")
        )
        disposition = {
            "revise": "refine",
            "retry": "refine",
            "reject": "abandon",
            "switch": "change_family",
            "switch_family": "change_family",
        }.get(disposition, disposition)
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
    comparison = CandidateComparisonSummary(
        folds=folds,
        score=comparison_score,
        diagnostics=(),
        _controller_seal=_CANDIDATE_COMPARISON_SEAL,
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


# Schema v4 is deliberately additive.  The schema-v3 contracts, prompts, and
# loader above authenticate historical audit material byte-for-byte; live v4
# callers use only the versioned contracts below and cannot reinterpret v3.
OPTIMIZER_V4_ROLES = OPTIMIZER_V2_ROLES
_V4_FOCUS_AREAS = ("entry", "risk_sizing", "exit")
_V4_PARENT_KINDS = ("baseline", "champion", "branch")
_V4_CRITIC_DISPOSITIONS = ("promote", "refine", "abandon")
_V4_VALIDATION_STATUSES = ("not_evaluated", "valid", "invalid")
_V4_VALIDATION_FAILURE_CODES = frozenset(
    {
        "author_output_invalid",
        "source_invalid",
        "syntax_failed",
        "imports_failed",
        "purity_failed",
        "determinism_failed",
        "worker_failed",
        "typed_decision_invalid",
        "runtime_invalid",
        "evaluation_failed",
    }
)


def _v4_utf8_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be UTF-8 text")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc


def _v4_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    _v4_utf8_bytes(value, field)
    return _v2_text(value, field, allow_empty=allow_empty)


def _v4_bounded_text(value: object, field: str, *, max_chars: int) -> str:
    text = _v4_text(value, field)
    if len(text) > max_chars:
        raise ValueError(f"{field} exceeds {max_chars} characters")
    return text


def _v4_source_text(value: object, field: str) -> str:
    encoded = _v4_utf8_bytes(value, field)
    assert isinstance(value, str)
    if not value or not value.endswith("\n"):
        raise ValueError(f"{field} must end with LF")
    if "\r" in value or "\x00" in value:
        raise ValueError(f"{field} must be LF-only text without NUL")
    # Round-tripping catches any future encoder-policy drift explicitly.
    if encoded.decode("utf-8", errors="strict") != value:
        raise ValueError(f"{field} must be valid UTF-8 text")
    return value


def _parse_v4_closed_object(
    raw: str,
    keys: frozenset[str],
    *,
    max_total_bytes: int,
) -> dict[str, object]:
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise ValueError("provider payload byte cap is invalid")
    encoded = _v4_utf8_bytes(raw, "provider payload")
    if len(encoded) > max_total_bytes:
        raise ValueError("provider payload is not bounded JSON text")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("provider payload is malformed JSON") from exc
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("provider payload has invalid keys")
    return value


def _v4_response_list(
    value: object,
    field: str,
    *,
    allow_empty: bool,
    max_items: int = MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
    max_item_chars: int = MAX_INVESTIGATOR_LIST_ITEM_CHARS,
) -> tuple[str, ...]:
    if type(value) is not list or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a JSON string array")
    if len(value) > max_items:
        raise ValueError(f"{field} may contain at most {max_items} items")
    normalized = tuple(
        _v4_bounded_text(item, field, max_chars=max_item_chars) for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must contain unique values")
    if not allow_empty and not normalized:
        raise ValueError(f"{field} cannot be empty")
    return normalized


def _v4_response_ids(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _v2_identifier(item, field)
        for item in _v4_response_list(
            value,
            field,
            allow_empty=True,
            max_item_chars=128,
        )
    )


def _v4_focus_areas(value: object, field: str) -> tuple[str, ...]:
    parsed = _v4_response_list(value, field, allow_empty=False, max_items=3)
    if any(item not in _V4_FOCUS_AREAS for item in parsed):
        raise ValueError(f"{field} are invalid")
    canonical = tuple(item for item in _V4_FOCUS_AREAS if item in parsed)
    if parsed != canonical:
        raise ValueError(f"{field} must be in canonical order")
    return parsed


@dataclass(frozen=True, slots=True)
class AuthorSourceFile(_V2Canonical):
    """One complete UTF-8/LF policy source bound to its repository path."""

    path: str
    source_sha256: str
    source: str

    def __post_init__(self) -> None:
        if self.path not in _POLICY_EDITABLE_PATHS:
            raise ValueError("author source path is outside the editable scope")
        _v4_source_text(self.source, f"author source {self.path}")
        _require_digest(self.source_sha256, "author source SHA-256")
        if hashlib.sha256(self.source.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("author source SHA-256 differs from its text")

    @classmethod
    def from_source(cls, *, path: str, source: str) -> "AuthorSourceFile":
        _v4_source_text(source, f"author source {path}")
        return cls(
            path=path,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            source=source,
        )


def _validate_v4_source_files(
    policy_sources: tuple[AuthorSourceFile, ...],
    field: str,
) -> None:
    if (
        type(policy_sources) is not tuple
        or len(policy_sources) != len(_POLICY_EDITABLE_PATHS)
        or any(not isinstance(item, AuthorSourceFile) for item in policy_sources)
        or tuple(item.path for item in policy_sources) != _POLICY_EDITABLE_PATHS
    ):
        raise ValueError(f"{field} must contain exactly the three editable paths")


def policy_source_bundle_v4_bytes(
    policy_sources: tuple[AuthorSourceFile, ...],
) -> bytes:
    """Return the exact escaped canonical source-context envelope."""

    _validate_v4_source_files(policy_sources, "policy source bundle")
    return _v2_canonical_bytes(
        {
            "policy_interface_version": 2,
            "files": policy_sources,
        }
    )


def policy_source_bundle_v4_sha256(
    policy_sources: tuple[AuthorSourceFile, ...],
) -> str:
    return hashlib.sha256(policy_source_bundle_v4_bytes(policy_sources) + b"\n").hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedParentIdentity(_V2Canonical):
    schema_version: int
    parent_kind: str
    parent_id: str
    source_head: str
    policy_interface_version: int
    policy_source_sha256s: tuple[tuple[str, str], ...]
    source_bundle_sha256: str
    parent_identity_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("selected parent schema is unsupported")
        if self.parent_kind not in _V4_PARENT_KINDS:
            raise ValueError("selected parent kind is invalid")
        _v2_identifier(self.parent_id, "selected parent ID")
        if re.fullmatch(r"[0-9a-f]{40}", self.source_head or "") is None:
            raise ValueError("selected parent source HEAD is invalid")
        if self.policy_interface_version != 2:
            raise ValueError("selected parent policy interface must be 2")
        if (
            type(self.policy_source_sha256s) is not tuple
            or tuple(path for path, _digest in self.policy_source_sha256s)
            != _POLICY_EDITABLE_PATHS
        ):
            raise ValueError("selected parent source identities are invalid")
        for path, digest in self.policy_source_sha256s:
            if path not in _POLICY_EDITABLE_PATHS:
                raise ValueError("selected parent source path is invalid")
            _require_digest(digest, "selected parent policy source SHA-256")
        _require_digest(self.source_bundle_sha256, "selected parent source bundle SHA-256")
        _require_digest(self.parent_identity_sha256, "selected parent identity SHA-256")
        expected = _v2_digest(
            {
                "schema_version": self.schema_version,
                "parent_kind": self.parent_kind,
                "parent_id": self.parent_id,
                "source_head": self.source_head,
                "policy_interface_version": self.policy_interface_version,
                "policy_source_sha256s": self.policy_source_sha256s,
                "source_bundle_sha256": self.source_bundle_sha256,
            }
        )
        if self.parent_identity_sha256 != expected:
            raise ValueError("selected parent identity is not self-authenticating")

    @classmethod
    def issue(
        cls,
        *,
        parent_kind: str,
        parent_id: str,
        source_head: str,
        policy_sources: tuple[AuthorSourceFile, ...],
    ) -> "SelectedParentIdentity":
        _validate_v4_source_files(policy_sources, "selected parent sources")
        values = {
            "schema_version": 4,
            "parent_kind": parent_kind,
            "parent_id": parent_id,
            "source_head": source_head,
            "policy_interface_version": 2,
            "policy_source_sha256s": tuple(
                (item.path, item.source_sha256) for item in policy_sources
            ),
            "source_bundle_sha256": policy_source_bundle_v4_sha256(policy_sources),
        }
        return cls(**values, parent_identity_sha256=_v2_digest(values))

    def validate_sources(self, policy_sources: tuple[AuthorSourceFile, ...]) -> None:
        _validate_v4_source_files(policy_sources, "selected parent sources")
        if tuple(
            (item.path, item.source_sha256) for item in policy_sources
        ) != self.policy_source_sha256s or (
            policy_source_bundle_v4_sha256(policy_sources)
            != self.source_bundle_sha256
        ):
            raise ValueError("selected parent source digest differs from its identity")


def _validate_v4_call_plan(
    call_budgets: tuple[PitOptimizerCallBudget, ...],
    *,
    max_iterations: int,
) -> None:
    _require_positive_int(max_iterations, "optimizer v4 iteration cap")
    if (
        type(call_budgets) is not tuple
        or len(call_budgets) != 3 * max_iterations
        or any(not isinstance(item, PitOptimizerCallBudget) for item in call_budgets)
    ):
        raise ValueError("optimizer v4 call plan is incomplete")
    expected = tuple(
        ((iteration - 1) * 3 + ordinal, iteration, role)
        for iteration in range(1, max_iterations + 1)
        for ordinal, role in enumerate(OPTIMIZER_V4_ROLES, start=1)
    )
    if tuple(
        (item.call_index, item.iteration, item.role) for item in call_budgets
    ) != expected:
        raise ValueError("optimizer v4 call order is invalid")


@dataclass(frozen=True, slots=True)
class PolicyAuthoringScopeV4(_V2Canonical):
    schema_version: int
    policy_interface_version: int
    initial_policy_source_sha256s: tuple[tuple[str, str], ...]
    initial_source_bundle_sha256: str
    canonical_source_bundle_bytes: int
    editable_paths: tuple[str, ...]
    max_iteration_feedback_bytes: int
    max_iteration_history_bytes: int
    author_response_headroom_bytes: int
    call_budgets: tuple[PitOptimizerCallBudget, ...]
    allowed_descendant_rule: str

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("policy authoring scope schema is unsupported")
        if self.policy_interface_version != 2:
            raise ValueError("policy authoring scope interface must be 2")
        if self.editable_paths != _POLICY_EDITABLE_PATHS:
            raise ValueError("policy authoring scope editable paths are invalid")
        if (
            type(self.initial_policy_source_sha256s) is not tuple
            or tuple(path for path, _digest in self.initial_policy_source_sha256s)
            != self.editable_paths
        ):
            raise ValueError("policy authoring scope initial identities are invalid")
        for _path, digest in self.initial_policy_source_sha256s:
            _require_digest(digest, "policy authoring scope source SHA-256")
        _require_digest(
            self.initial_source_bundle_sha256,
            "policy authoring scope bundle SHA-256",
        )
        for name in (
            "canonical_source_bundle_bytes",
            "max_iteration_feedback_bytes",
            "max_iteration_history_bytes",
            "author_response_headroom_bytes",
        ):
            _require_positive_int(getattr(self, name), f"policy authoring scope {name}")
        if self.allowed_descendant_rule != "authenticated_parent_plus_atomic_full_sources":
            raise ValueError("policy authoring scope descendant rule is invalid")
        if not self.call_budgets:
            raise ValueError("policy authoring scope call plan is absent")
        max_iterations = max(item.iteration for item in self.call_budgets)
        _validate_v4_call_plan(self.call_budgets, max_iterations=max_iterations)
        for budget in self.call_budgets:
            declared_components = self.max_iteration_feedback_bytes
            if budget.role in {"investigator", "author"}:
                declared_components += self.canonical_source_bundle_bytes
            if budget.role == "investigator":
                declared_components += self.max_iteration_history_bytes
            if declared_components > budget.max_dynamic_input_bytes:
                raise ValueError(
                    f"{budget.role} declared component envelopes exceed its dynamic cap"
                )
            if budget.role == "author" and budget.max_response_bytes < (
                self.canonical_source_bundle_bytes
                + self.author_response_headroom_bytes
            ):
                raise ValueError("author response cap lacks declared source headroom")

    @classmethod
    def from_sources(
        cls,
        *,
        policy_sources: tuple[AuthorSourceFile, ...],
        call_budgets: tuple[PitOptimizerCallBudget, ...],
        max_iteration_feedback_bytes: int,
        max_iteration_history_bytes: int,
        author_response_headroom_bytes: int,
    ) -> "PolicyAuthoringScopeV4":
        envelope = policy_source_bundle_v4_bytes(policy_sources)
        return cls(
            schema_version=4,
            policy_interface_version=2,
            initial_policy_source_sha256s=tuple(
                (item.path, item.source_sha256) for item in policy_sources
            ),
            initial_source_bundle_sha256=hashlib.sha256(envelope + b"\n").hexdigest(),
            canonical_source_bundle_bytes=len(envelope),
            editable_paths=_POLICY_EDITABLE_PATHS,
            max_iteration_feedback_bytes=max_iteration_feedback_bytes,
            max_iteration_history_bytes=max_iteration_history_bytes,
            author_response_headroom_bytes=author_response_headroom_bytes,
            call_budgets=call_budgets,
            allowed_descendant_rule="authenticated_parent_plus_atomic_full_sources",
        )

    @property
    def sha256(self) -> str:
        return _v2_digest(self)


@dataclass(frozen=True, slots=True)
class PitOptimizerRunManifestV4(_V2Canonical):
    schema_version: int
    campaign_id: str
    campaign_sequence: int
    model: str
    source_head: str
    source_fingerprint_sha256: str
    source_clean: bool
    policy_interface_version: int
    pit_bundle_sha256: str
    discovery_panel_plan_sha256: str
    quick_panel_sha256: str
    discovery_panel_sha256: str
    qualification_plan_sha256: str
    annualized_return_target: AnnualizedReturnTarget
    seed_checkpoint_sha256: str | None
    editable_paths: tuple[str, ...]
    policy_authoring_scope: PolicyAuthoringScopeV4
    immutable_constraints_sha256: str
    immutable_constraint_ids: tuple[str, ...]
    sandbox_image: str
    call_budgets: tuple[PitOptimizerCallBudget, ...]
    max_iterations: int
    apply: bool
    provider_retries: int

    def __post_init__(self) -> None:
        if self.schema_version != 4:
            raise ValueError("optimizer v4 manifest schema is unsupported")
        _v2_identifier(self.campaign_id, "optimizer v4 campaign ID")
        _require_positive_int(self.campaign_sequence, "optimizer v4 campaign sequence")
        if self.model != PIT_OPTIMIZER_R1_MODEL:
            raise ValueError("optimizer v4 model is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.source_head or "") is None:
            raise ValueError("optimizer v4 source HEAD is invalid")
        _require_digest(
            self.source_fingerprint_sha256,
            "optimizer v4 source fingerprint SHA-256",
        )
        if self.source_clean is not True:
            raise ValueError("optimizer v4 source must be clean")
        if self.policy_interface_version != 2:
            raise ValueError("optimizer v4 policy interface must be 2")
        for value, label in (
            (self.pit_bundle_sha256, "optimizer v4 PIT bundle SHA-256"),
            (
                self.discovery_panel_plan_sha256,
                "optimizer v4 discovery plan SHA-256",
            ),
            (self.quick_panel_sha256, "optimizer v4 quick panel SHA-256"),
            (
                self.discovery_panel_sha256,
                "optimizer v4 discovery panel SHA-256",
            ),
            (
                self.qualification_plan_sha256,
                "optimizer v4 qualification commitment",
            ),
            (
                self.immutable_constraints_sha256,
                "optimizer v4 immutable constraints SHA-256",
            ),
        ):
            _require_digest(value, label)
        if not isinstance(self.annualized_return_target, AnnualizedReturnTarget):
            raise ValueError("optimizer v4 annualized return target is invalid")
        if self.seed_checkpoint_sha256 is not None:
            _require_digest(
                self.seed_checkpoint_sha256,
                "optimizer v4 seed checkpoint SHA-256",
            )
        if self.editable_paths != _POLICY_EDITABLE_PATHS:
            raise ValueError("optimizer v4 editable paths are invalid")
        if not isinstance(self.policy_authoring_scope, PolicyAuthoringScopeV4):
            raise ValueError("optimizer v4 policy authoring scope is invalid")
        if (
            self.policy_authoring_scope.policy_interface_version
            != self.policy_interface_version
            or self.policy_authoring_scope.editable_paths != self.editable_paths
            or self.policy_authoring_scope.call_budgets != self.call_budgets
        ):
            raise ValueError("optimizer v4 authoring scope differs from manifest")
        _v2_string_tuple(self.immutable_constraint_ids, "optimizer v4 constraint IDs")
        expected_constraints = hashlib.sha256(
            _v2_canonical_bytes(self.immutable_constraint_ids) + b"\n"
        ).hexdigest()
        if self.immutable_constraints_sha256 != expected_constraints:
            raise ValueError("optimizer v4 immutable constraint identity differs")
        if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.sandbox_image or "") is None:
            raise ValueError("optimizer v4 sandbox image must be digest pinned")
        _validate_v4_call_plan(self.call_budgets, max_iterations=self.max_iterations)
        if any(item.model != self.model for item in self.call_budgets):
            raise ValueError("optimizer v4 call model differs from manifest")
        if self.apply is not False:
            raise ValueError("optimizer v4 apply must be false")
        if self.provider_retries != 0:
            raise ValueError("optimizer v4 provider retries must be zero")
        for budget in self.call_budgets:
            static_bytes = len(PIT_OPTIMIZER_V4_SYSTEM_PROMPTS[budget.role].encode("utf-8"))
            static_bytes += len(
                _v2_canonical_bytes(pit_optimizer_v4_response_format(budget.role))
            )
            if static_bytes > budget.max_static_input_bytes:
                raise ValueError("optimizer v4 static role context exceeds call cap")

    @classmethod
    def from_discovery_plan(
        cls,
        *,
        campaign_id: str,
        campaign_sequence: int,
        source_head: str,
        source_fingerprint_sha256: str,
        discovery_panel_plan: DiscoveryPanelPlan,
        policy_authoring_scope: PolicyAuthoringScopeV4,
        immutable_constraint_ids: tuple[str, ...],
        sandbox_image: str,
        seed_checkpoint_sha256: str | None = None,
        model: str = PIT_OPTIMIZER_R1_MODEL,
        apply: bool = False,
        provider_retries: int = 0,
    ) -> "PitOptimizerRunManifestV4":
        if not isinstance(discovery_panel_plan, DiscoveryPanelPlan):
            raise ValueError("optimizer v4 discovery plan is invalid")
        constraint_sha256 = hashlib.sha256(
            _v2_canonical_bytes(immutable_constraint_ids) + b"\n"
        ).hexdigest()
        max_iterations = max(item.iteration for item in policy_authoring_scope.call_budgets)
        result = cls(
            schema_version=4,
            campaign_id=campaign_id,
            campaign_sequence=campaign_sequence,
            model=model,
            source_head=source_head,
            source_fingerprint_sha256=source_fingerprint_sha256,
            source_clean=True,
            policy_interface_version=2,
            pit_bundle_sha256=discovery_panel_plan.pit_bundle_sha256,
            discovery_panel_plan_sha256=discovery_panel_plan.sha256,
            quick_panel_sha256=discovery_panel_plan.quick_panel.sha256,
            discovery_panel_sha256=discovery_panel_plan.discovery_panel.sha256,
            qualification_plan_sha256=discovery_panel_plan.qualification_plan_sha256,
            annualized_return_target=discovery_panel_plan.target,
            seed_checkpoint_sha256=seed_checkpoint_sha256,
            editable_paths=_POLICY_EDITABLE_PATHS,
            policy_authoring_scope=policy_authoring_scope,
            immutable_constraints_sha256=constraint_sha256,
            immutable_constraint_ids=immutable_constraint_ids,
            sandbox_image=sandbox_image,
            call_budgets=policy_authoring_scope.call_budgets,
            max_iterations=max_iterations,
            apply=apply,
            provider_retries=provider_retries,
        )
        result.validate_discovery_plan(discovery_panel_plan)
        return result

    def validate_discovery_plan(self, plan: DiscoveryPanelPlan) -> None:
        if (
            not isinstance(plan, DiscoveryPanelPlan)
            or plan.schema_version != 4
            or plan.sha256 != self.discovery_panel_plan_sha256
            or plan.pit_bundle_sha256 != self.pit_bundle_sha256
            or plan.quick_panel.sha256 != self.quick_panel_sha256
            or plan.discovery_panel.sha256 != self.discovery_panel_sha256
            or plan.qualification_plan_sha256 != self.qualification_plan_sha256
            or plan.target != self.annualized_return_target
        ):
            raise ValueError("optimizer v4 discovery plan binding differs")

    @property
    def sha256(self) -> str:
        return _v2_digest(self)


def _pit_optimizer_manifest_v4_from_primitive(
    primitive: Mapping[str, object],
    *,
    discovery_panel_plan: DiscoveryPanelPlan | None = None,
) -> PitOptimizerRunManifestV4:
    expected_keys = {field.name for field in fields(PitOptimizerRunManifestV4)}
    if not isinstance(primitive, Mapping) or set(primitive) != expected_keys:
        raise ValueError("optimizer v4 manifest keys are invalid")
    try:
        values = dict(primitive)
        scope_value = values["policy_authoring_scope"]
        target_value = values["annualized_return_target"]
        if not isinstance(scope_value, dict) or not isinstance(target_value, dict):
            raise ValueError("optimizer v4 nested contracts are invalid")
        scope_keys = {field.name for field in fields(PolicyAuthoringScopeV4)}
        if set(scope_value) != scope_keys:
            raise ValueError("optimizer v4 authoring scope keys are invalid")
        scope = dict(scope_value)
        scope["initial_policy_source_sha256s"] = tuple(
            tuple(item) for item in scope["initial_policy_source_sha256s"]
        )
        scope["editable_paths"] = tuple(scope["editable_paths"])
        scope["call_budgets"] = tuple(
            PitOptimizerCallBudget(**item) for item in scope["call_budgets"]
        )
        expected_target = _v2_primitive(AnnualizedReturnTarget.production())
        if target_value != expected_target:
            raise ValueError("optimizer v4 target contract is invalid")
        values["annualized_return_target"] = AnnualizedReturnTarget.production()
        values["editable_paths"] = tuple(values["editable_paths"])
        values["policy_authoring_scope"] = PolicyAuthoringScopeV4(**scope)
        values["immutable_constraint_ids"] = tuple(
            values["immutable_constraint_ids"]
        )
        values["call_budgets"] = tuple(
            PitOptimizerCallBudget(**item) for item in values["call_budgets"]
        )
        manifest = PitOptimizerRunManifestV4(**values)
        if discovery_panel_plan is not None:
            manifest.validate_discovery_plan(discovery_panel_plan)
        return manifest
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("optimizer v4 manifest closed contract is invalid") from exc


def _load_canonical_optimizer_manifest(
    path: Path,
    *,
    expected_sha256: str | None,
) -> Mapping[str, object]:
    resolved = _resolved_file(path, "optimizer manifest artifact")
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        _require_digest(expected_sha256, "optimizer manifest expected SHA-256")
        if digest != expected_sha256:
            raise ValueError("optimizer manifest digest differs")
    try:
        primitive = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("optimizer manifest is invalid JSON") from exc
    if (
        not isinstance(primitive, dict)
        or raw != _v2_canonical_bytes(primitive) + b"\n"
    ):
        raise ValueError("optimizer manifest is not canonical JSON")
    return primitive


def load_pit_optimizer_manifest_v3_audit(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> PitOptimizerRunManifest:
    """Authenticate legacy schema-v3 history; it is not resumable as v4."""

    primitive = _load_canonical_optimizer_manifest(
        path,
        expected_sha256=expected_sha256,
    )
    if primitive.get("schema_version") != 3:
        raise ValueError("legacy optimizer audit manifest must use schema 3")
    return _pit_optimizer_manifest_from_primitive(primitive)


def load_pit_optimizer_manifest_v4(
    path: Path,
    *,
    expected_sha256: str | None = None,
    discovery_panel_plan: DiscoveryPanelPlan | None = None,
) -> PitOptimizerRunManifestV4:
    primitive = _load_canonical_optimizer_manifest(
        path,
        expected_sha256=expected_sha256,
    )
    if primitive.get("schema_version") != 4:
        raise ValueError("optimizer v4 manifest must use schema 4")
    return _pit_optimizer_manifest_v4_from_primitive(
        primitive,
        discovery_panel_plan=discovery_panel_plan,
    )


PIT_OPTIMIZER_V4_SYSTEM_PROMPTS = MappingProxyType(
    {
        "investigator": (
            "You are the adaptive O'Neil strategy investigator. Diagnose only the supplied "
            "selected-parent policy sources and aggregate quick/discovery portfolio evidence. "
            "Propose one focused causal mechanism grounded in O'Neil entry quality, leadership, "
            "risk sizing, exposure, winner retention, or exit behavior. Return exactly one JSON "
            "object matching the supplied schema. Select one to three focus_areas from entry, "
            "risk_sizing, and exit in that canonical order. Do not select files, emit patches, "
            "request qualification data, or include chain-of-thought, credentials, paths outside "
            "the supplied repository-relative policy scope, raw rows, trades, holdings, or "
            "provider accounting. The local closed parser is authoritative."
        ),
        "author": (
            "You are the adaptive O'Neil strategy author. Implement the investigator's focused "
            "mechanism against the authenticated selected parent. Return exactly one JSON object "
            "matching the supplied schema, bound to parent_identity_sha256. policy_sources must "
            "contain exactly the three repository-relative policy keys and the complete UTF-8, "
            "LF-only, final-newline source for every file, including unchanged files. You may "
            "change any or all three sources coherently. Do not emit a patch, diff metadata, "
            "file-selection metadata, markdown, chain-of-thought, I/O, reflection, credentials, "
            "local paths, or hidden/qualification data. The local closed parser is authoritative."
        ),
        "critic": (
            "You are the adaptive O'Neil strategy critic. Explain the supplied validation result "
            "and measured quick/discovery portfolio CAGR behavior relative to the fixed baseline, "
            "current champion, and target. Return exactly one JSON object matching the supplied "
            "schema. disposition must be exactly promote, refine, or abandon; it is advisory and "
            "cannot override metric-owned champion selection. Do not request or reproduce policy "
            "source, raw trades, holdings, qualification data, local paths, credentials, provider "
            "accounting, or chain-of-thought. The local closed parser is authoritative."
        ),
    }
)


_V4_RESPONSE_SCHEMAS = MappingProxyType(
    {
        "investigator": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "focus_areas",
                "evidence_ids",
                "causal_rationale",
                "expected_diagnostic_changes",
                "known_risks",
                "author_instructions",
            ],
            "properties": {
                "hypothesis_id": _v2_identifier_schema(
                    "A stable lower-case identifier for the focused mechanism."
                ),
                "focus_areas": _v2_list_schema(
                    max_items=3,
                    min_items=1,
                    items={"type": "string", "enum": list(_V4_FOCUS_AREAS)},
                    description="One to three canonical O'Neil policy focus areas.",
                ),
                "evidence_ids": _v2_list_schema(
                    max_items=MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
                    min_items=1,
                    items=_v2_identifier_schema(
                        "An ID copied from supplied quick/discovery aggregate evidence."
                    ),
                ),
                "causal_rationale": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                ),
                "expected_diagnostic_changes": _v2_compact_text_list_schema(
                    "Expected aggregate diagnostic changes."
                ),
                "known_risks": _v2_compact_text_list_schema(
                    "Known aggregate-only risks.", allow_empty=True
                ),
                "author_instructions": _v2_compact_text_list_schema(
                    "Concrete full-source implementation instructions."
                ),
            },
        },
        "author": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "parent_identity_sha256",
                "behavioral_summary",
                "policy_sources",
                "assumptions",
                "validation_suggestions",
            ],
            "properties": {
                "hypothesis_id": _v2_identifier_schema(
                    "The investigator hypothesis ID copied verbatim."
                ),
                "parent_identity_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "behavioral_summary": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                ),
                "policy_sources": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(_POLICY_EDITABLE_PATHS),
                    "properties": {
                        path: {
                            "type": "string",
                            "minLength": 1,
                            "description": "Complete UTF-8/LF policy source ending in LF.",
                        }
                        for path in _POLICY_EDITABLE_PATHS
                    },
                },
                "assumptions": _v2_compact_text_list_schema(
                    "Optional bounded assumptions.", allow_empty=True
                ),
                "validation_suggestions": _v2_compact_text_list_schema(
                    "Optional bounded local validation suggestions.", allow_empty=True
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
                ),
                "causal_explanation": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                ),
                "evidence_ids": _v2_list_schema(
                    max_items=MAX_INVESTIGATOR_OUTPUT_LIST_ITEMS,
                    min_items=0,
                    items=_v2_identifier_schema(
                        "An ID copied from supplied validation or panel evidence."
                    ),
                ),
                "disposition": {
                    "type": "string",
                    "enum": list(_V4_CRITIC_DISPOSITIONS),
                },
                "next_direction": _v2_string_schema(
                    max_length=MAX_INVESTIGATOR_RATIONALE_CHARS,
                    min_length=1,
                ),
            },
        },
    }
)


def pit_optimizer_v4_response_schema(role: str) -> dict[str, object]:
    """Return a defensive copy of the authoritative local v4 role schema."""

    try:
        schema = _V4_RESPONSE_SCHEMAS[role]
    except KeyError as exc:
        raise ValueError("unknown PIT optimizer v4 role") from exc
    copied = json.loads(json.dumps(schema, separators=(",", ":"), ensure_ascii=False))
    if not isinstance(copied, dict):
        raise AssertionError("optimizer v4 local response schema is not an object")
    return copied


def pit_optimizer_v4_response_format(role: str) -> dict[str, object]:
    # DeepSeek through OpenRouter accepts generic JSON-object mode.  The local
    # closed parsers and ``pit_optimizer_v4_response_schema`` remain authoritative.
    pit_optimizer_v4_response_schema(role)
    return {"type": "json_object"}


@dataclass(frozen=True, slots=True)
class InvestigatorArtifactV4(_V2Canonical):
    hypothesis_id: str
    focus_areas: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    causal_rationale: str
    expected_diagnostic_changes: tuple[str, ...]
    known_risks: tuple[str, ...]
    author_instructions: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "investigator v4 hypothesis ID")
        _v4_focus_areas(list(self.focus_areas), "investigator v4 focus areas")
        for name, allow_empty in (
            ("expected_diagnostic_changes", False),
            ("known_risks", True),
            ("author_instructions", False),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ValueError(f"investigator v4 {name} must be a tuple")
            _v4_response_list(
                list(value),
                f"investigator v4 {name}",
                allow_empty=allow_empty,
            )
        if type(self.evidence_ids) is not tuple or not self.evidence_ids:
            raise ValueError("investigator v4 evidence IDs must be a non-empty tuple")
        _v4_response_ids(list(self.evidence_ids), "investigator v4 evidence IDs")
        _v4_bounded_text(
            self.causal_rationale,
            "investigator v4 causal rationale",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        if len(self.canonical_json_bytes()) > MAX_INVESTIGATOR_ARTIFACT_BYTES:
            raise ValueError("investigator v4 artifact exceeds its byte cap")

    @classmethod
    def from_json(cls, raw: str, *, max_total_bytes: int) -> "InvestigatorArtifactV4":
        value = _parse_v4_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis_id",
                    "focus_areas",
                    "evidence_ids",
                    "causal_rationale",
                    "expected_diagnostic_changes",
                    "known_risks",
                    "author_instructions",
                }
            ),
            max_total_bytes=max_total_bytes,
        )
        return cls(
            hypothesis_id=_v2_identifier(
                _v4_text(value["hypothesis_id"], "investigator v4 hypothesis ID"),
                "investigator v4 hypothesis ID",
            ),
            focus_areas=_v4_focus_areas(
                value["focus_areas"], "investigator v4 focus areas"
            ),
            evidence_ids=_v4_response_ids(
                value["evidence_ids"],
                "investigator v4 evidence IDs",
            ),
            causal_rationale=_v4_bounded_text(
                value["causal_rationale"],
                "investigator v4 causal rationale",
                max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
            ),
            expected_diagnostic_changes=_v4_response_list(
                value["expected_diagnostic_changes"],
                "investigator v4 expected diagnostic changes",
                allow_empty=False,
            ),
            known_risks=_v4_response_list(
                value["known_risks"],
                "investigator v4 known risks",
                allow_empty=True,
            ),
            author_instructions=_v4_response_list(
                value["author_instructions"],
                "investigator v4 author instructions",
                allow_empty=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthorArtifactV4(_V2Canonical):
    hypothesis_id: str
    parent_identity_sha256: str
    behavioral_summary: str
    policy_sources: tuple[AuthorSourceFile, ...]
    assumptions: tuple[str, ...]
    validation_suggestions: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "author v4 hypothesis ID")
        _require_digest(self.parent_identity_sha256, "author v4 parent identity SHA-256")
        _v4_bounded_text(
            self.behavioral_summary,
            "author v4 behavioral summary",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        _validate_v4_source_files(self.policy_sources, "author v4 policy sources")
        for value, field in (
            (self.assumptions, "author v4 assumptions"),
            (self.validation_suggestions, "author v4 validation suggestions"),
        ):
            if type(value) is not tuple:
                raise ValueError(f"{field} must be a tuple")
            _v4_response_list(list(value), field, allow_empty=True)

    def to_primitive(self) -> dict[str, object]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "parent_identity_sha256": self.parent_identity_sha256,
            "behavioral_summary": self.behavioral_summary,
            "policy_sources": {
                item.path: item.source for item in self.policy_sources
            },
            "assumptions": list(self.assumptions),
            "validation_suggestions": list(self.validation_suggestions),
        }

    def canonical_json_bytes(self) -> bytes:
        return _v2_canonical_bytes(self.to_primitive())

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        selected_parent: SelectedParentIdentity,
        max_total_bytes: int,
    ) -> "AuthorArtifactV4":
        if not isinstance(selected_parent, SelectedParentIdentity):
            raise ValueError("author v4 selected parent is invalid")
        value = _parse_v4_closed_object(
            raw,
            frozenset(
                {
                    "hypothesis_id",
                    "parent_identity_sha256",
                    "behavioral_summary",
                    "policy_sources",
                    "assumptions",
                    "validation_suggestions",
                }
            ),
            max_total_bytes=max_total_bytes,
        )
        parent_identity_sha256 = value["parent_identity_sha256"]
        _require_digest(parent_identity_sha256, "author v4 parent identity SHA-256")
        if parent_identity_sha256 != selected_parent.parent_identity_sha256:
            raise ValueError("author v4 parent identity differs from selected parent")
        source_map = value["policy_sources"]
        if not isinstance(source_map, dict) or set(source_map) != set(
            _POLICY_EDITABLE_PATHS
        ):
            raise ValueError(
                "author v4 policy sources must contain exactly the three editable paths"
            )
        sources = tuple(
            AuthorSourceFile.from_source(path=path, source=source_map[path])
            for path in _POLICY_EDITABLE_PATHS
        )
        changed = tuple(
            path
            for (path, parent_sha256), item in zip(
                selected_parent.policy_source_sha256s,
                sources,
                strict=True,
            )
            if item.source_sha256 != parent_sha256
        )
        if not changed:
            raise ValueError("author v4 response must contain at least one changed file")
        return cls(
            hypothesis_id=_v2_identifier(
                _v4_text(value["hypothesis_id"], "author v4 hypothesis ID"),
                "author v4 hypothesis ID",
            ),
            parent_identity_sha256=parent_identity_sha256,
            behavioral_summary=_v4_bounded_text(
                value["behavioral_summary"],
                "author v4 behavioral summary",
                max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
            ),
            policy_sources=sources,
            assumptions=_v4_response_list(
                value["assumptions"], "author v4 assumptions", allow_empty=True
            ),
            validation_suggestions=_v4_response_list(
                value["validation_suggestions"],
                "author v4 validation suggestions",
                allow_empty=True,
            ),
        )

    @property
    def source_bundle_sha256(self) -> str:
        return policy_source_bundle_v4_sha256(self.policy_sources)

    @property
    def replacement_sources(self) -> Mapping[str, str]:
        """Expose the complete canonical source map for atomic materialization."""

        return MappingProxyType(
            {item.path: item.source for item in self.policy_sources}
        )

    def changed_paths(self, parent: SelectedParentIdentity) -> tuple[str, ...]:
        if self.parent_identity_sha256 != parent.parent_identity_sha256:
            raise ValueError("author v4 artifact parent identity differs")
        return tuple(
            item.path
            for item, (_path, parent_sha256) in zip(
                self.policy_sources,
                parent.policy_source_sha256s,
                strict=True,
            )
            if item.source_sha256 != parent_sha256
        )


@dataclass(frozen=True, slots=True)
class AuthorManifestSummaryV4(_V2Canonical):
    hypothesis_id: str
    parent_identity_sha256: str
    behavioral_summary: str
    policy_source_sha256s: tuple[tuple[str, str], ...]
    source_bundle_sha256: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "author manifest v4 hypothesis ID")
        _require_digest(
            self.parent_identity_sha256,
            "author manifest v4 parent identity SHA-256",
        )
        _v4_bounded_text(
            self.behavioral_summary,
            "author manifest v4 behavioral summary",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        if (
            type(self.policy_source_sha256s) is not tuple
            or tuple(path for path, _digest in self.policy_source_sha256s)
            != _POLICY_EDITABLE_PATHS
        ):
            raise ValueError("author manifest v4 source identities are invalid")
        for _path, digest in self.policy_source_sha256s:
            _require_digest(digest, "author manifest v4 source SHA-256")
        _require_digest(
            self.source_bundle_sha256,
            "author manifest v4 source bundle SHA-256",
        )
        if (
            type(self.changed_paths) is not tuple
            or not self.changed_paths
            or self.changed_paths
            != tuple(path for path in _POLICY_EDITABLE_PATHS if path in self.changed_paths)
        ):
            raise ValueError("author manifest v4 changed paths are invalid")

    @classmethod
    def from_artifact(
        cls,
        artifact: AuthorArtifactV4,
        *,
        selected_parent: SelectedParentIdentity,
    ) -> "AuthorManifestSummaryV4":
        if not isinstance(artifact, AuthorArtifactV4):
            raise ValueError("author manifest v4 artifact is invalid")
        changed_paths = artifact.changed_paths(selected_parent)
        return cls(
            hypothesis_id=artifact.hypothesis_id,
            parent_identity_sha256=artifact.parent_identity_sha256,
            behavioral_summary=artifact.behavioral_summary,
            policy_source_sha256s=tuple(
                (item.path, item.source_sha256) for item in artifact.policy_sources
            ),
            source_bundle_sha256=artifact.source_bundle_sha256,
            changed_paths=changed_paths,
        )


@dataclass(frozen=True, slots=True)
class CriticArtifactV4(_V2Canonical):
    hypothesis_id: str
    prediction_vs_observation: str
    causal_explanation: str
    evidence_ids: tuple[str, ...]
    disposition: str
    next_direction: str

    def __post_init__(self) -> None:
        _v2_identifier(self.hypothesis_id, "critic v4 hypothesis ID")
        _v4_bounded_text(
            self.prediction_vs_observation,
            "critic v4 prediction versus observation",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        _v4_bounded_text(
            self.causal_explanation,
            "critic v4 causal explanation",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        if type(self.evidence_ids) is not tuple:
            raise ValueError("critic v4 evidence IDs must be a tuple")
        _v4_response_ids(list(self.evidence_ids), "critic v4 evidence IDs")
        if self.disposition not in _V4_CRITIC_DISPOSITIONS:
            raise ValueError("critic v4 disposition is invalid")
        _v4_bounded_text(
            self.next_direction,
            "critic v4 next direction",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        if len(self.canonical_json_bytes()) > MAX_CRITIC_ARTIFACT_BYTES:
            raise ValueError("critic v4 artifact exceeds its byte cap")

    @classmethod
    def from_json(cls, raw: str, *, max_total_bytes: int) -> "CriticArtifactV4":
        value = _parse_v4_closed_object(
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
        disposition = _v4_text(value["disposition"], "critic v4 disposition")
        if disposition not in _V4_CRITIC_DISPOSITIONS:
            raise ValueError("critic v4 disposition is invalid")
        return cls(
            hypothesis_id=_v2_identifier(
                _v4_text(value["hypothesis_id"], "critic v4 hypothesis ID"),
                "critic v4 hypothesis ID",
            ),
            prediction_vs_observation=_v4_bounded_text(
                value["prediction_vs_observation"],
                "critic v4 prediction versus observation",
                max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
            ),
            causal_explanation=_v4_bounded_text(
                value["causal_explanation"],
                "critic v4 causal explanation",
                max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
            ),
            evidence_ids=_v4_response_ids(
                value["evidence_ids"],
                "critic v4 evidence IDs",
            ),
            disposition=disposition,
            next_direction=_v4_bounded_text(
                value["next_direction"],
                "critic v4 next direction",
                max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
            ),
        )


@dataclass(frozen=True, slots=True)
class RoleOutputInvalidSummary(_V2Canonical):
    iteration: int
    call_index: int
    role: str
    validation_code: str

    def __post_init__(self) -> None:
        _require_positive_int(self.iteration, "role output invalid summary iteration")
        if self.role != "author":
            raise ValueError("role output invalid summary must describe the author")
        expected_call_index = (self.iteration - 1) * len(OPTIMIZER_V4_ROLES) + 2
        if self.call_index != expected_call_index:
            raise ValueError("role output invalid summary author slot is invalid")
        if self.validation_code not in PIT_OPTIMIZER_RESPONSE_VALIDATION_CODES:
            raise ValueError("role output invalid summary validation code is invalid")


def _v4_cagr(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if value != value.quantize(Decimal("0.01")):
        raise ValueError(f"{field} must use 0.01 percentage-point precision")
    return value


def _require_v4_panel_summary(
    value: object,
    purpose: str,
    field: str,
    *,
    expected_sha256: str | None = None,
) -> PanelAggregateSummary:
    if not isinstance(value, PanelAggregateSummary) or value.panel_id != purpose:
        raise ValueError(f"{field} must be {purpose} panel evidence")
    if expected_sha256 is not None:
        _require_digest(expected_sha256, f"{field} expected panel SHA-256")
        if value.panel_sha256 != expected_sha256:
            raise ValueError(f"{field} differs from the authenticated panel")
    _v4_cagr(value.portfolio_annualized_return_pct, f"{field} CAGR")
    return value


@dataclass(frozen=True, slots=True)
class CandidateValidationStatusV4(_V2Canonical):
    status: str
    failure_code: str | None

    def __post_init__(self) -> None:
        if self.status not in _V4_VALIDATION_STATUSES:
            raise ValueError("candidate validation v4 status is invalid")
        if self.status == "invalid":
            if self.failure_code not in _V4_VALIDATION_FAILURE_CODES:
                raise ValueError("candidate validation v4 failure code is invalid")
        elif self.failure_code is not None:
            raise ValueError("candidate validation v4 nonfailure cannot carry a code")


@dataclass(frozen=True, slots=True)
class TargetProgressV4(_V2Canonical):
    target_pct: Decimal
    baseline_cagr_pct: Decimal
    selected_parent_cagr_pct: Decimal
    champion_cagr_pct: Decimal
    target_gap_pp: Decimal

    def __post_init__(self) -> None:
        for name in (
            "target_pct",
            "baseline_cagr_pct",
            "selected_parent_cagr_pct",
            "champion_cagr_pct",
            "target_gap_pp",
        ):
            _v4_cagr(getattr(self, name), f"target progress {name}")
        if self.target_pct != AnnualizedReturnTarget.production().target_pct:
            raise ValueError("target progress differs from annualized objective")
        if self.target_gap_pp != (self.target_pct - self.selected_parent_cagr_pct).quantize(
            Decimal("0.01")
        ):
            raise ValueError("target progress gap differs from selected parent CAGR")

    @classmethod
    def from_summaries(
        cls,
        *,
        target: AnnualizedReturnTarget,
        baseline: PanelAggregateSummary,
        selected_parent: PanelAggregateSummary,
        champion: PanelAggregateSummary,
    ) -> "TargetProgressV4":
        if not isinstance(target, AnnualizedReturnTarget):
            raise ValueError("target progress objective is invalid")
        for value, field in (
            (baseline, "target progress baseline"),
            (selected_parent, "target progress selected parent"),
            (champion, "target progress champion"),
        ):
            _require_v4_panel_summary(value, "discovery", field)
        selected = selected_parent.portfolio_annualized_return_pct
        return cls(
            target_pct=target.target_pct,
            baseline_cagr_pct=baseline.portfolio_annualized_return_pct,
            selected_parent_cagr_pct=selected,
            champion_cagr_pct=champion.portfolio_annualized_return_pct,
            target_gap_pp=(target.target_pct - selected).quantize(Decimal("0.01")),
        )

    def validate_summaries(
        self,
        *,
        target: AnnualizedReturnTarget,
        baseline: PanelAggregateSummary,
        selected_parent: PanelAggregateSummary,
        champion: PanelAggregateSummary,
        discovery_panel_sha256: str,
    ) -> None:
        for value, field in (
            (baseline, "target progress baseline"),
            (selected_parent, "target progress selected parent"),
            (champion, "target progress champion"),
        ):
            _require_v4_panel_summary(
                value,
                "discovery",
                field,
                expected_sha256=discovery_panel_sha256,
            )
        expected = TargetProgressV4.from_summaries(
            target=target,
            baseline=baseline,
            selected_parent=selected_parent,
            champion=champion,
        )
        if self != expected:
            raise ValueError("target progress differs from authenticated summaries")


@dataclass(frozen=True, slots=True)
class SelectedParentSummary(_V2Canonical):
    identity: SelectedParentIdentity
    hypothesis_id: str
    behavioral_summary: str
    quick_panel: PanelAggregateSummary
    discovery_panel: PanelAggregateSummary

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SelectedParentIdentity):
            raise ValueError("selected parent summary identity is invalid")
        _v2_identifier(self.hypothesis_id, "selected parent summary hypothesis ID")
        _v4_bounded_text(
            self.behavioral_summary,
            "selected parent behavioral summary",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        _require_v4_panel_summary(
            self.quick_panel,
            "quick",
            "selected parent quick panel",
        )
        _require_v4_panel_summary(
            self.discovery_panel,
            "discovery",
            "selected parent discovery panel",
        )

    def validate_panel_identities(
        self,
        *,
        quick_panel_sha256: str,
        discovery_panel_sha256: str,
    ) -> None:
        _require_v4_panel_summary(
            self.quick_panel,
            "quick",
            "selected parent quick panel",
            expected_sha256=quick_panel_sha256,
        )
        _require_v4_panel_summary(
            self.discovery_panel,
            "discovery",
            "selected parent discovery panel",
            expected_sha256=discovery_panel_sha256,
        )


@dataclass(frozen=True, slots=True)
class PriorHypothesisSummaryV4(_V2Canonical):
    iteration: int
    hypothesis_id: str
    focus_areas: tuple[str, ...]
    behavioral_summary: str
    validation: CandidateValidationStatusV4
    discovery_cagr_pct: Decimal | None
    critic_disposition: str

    def __post_init__(self) -> None:
        _require_positive_int(self.iteration, "prior hypothesis v4 iteration")
        _v2_identifier(self.hypothesis_id, "prior hypothesis v4 ID")
        _v4_focus_areas(list(self.focus_areas), "prior hypothesis v4 focus areas")
        _v4_bounded_text(
            self.behavioral_summary,
            "prior hypothesis v4 behavioral summary",
            max_chars=MAX_INVESTIGATOR_RATIONALE_CHARS,
        )
        if not isinstance(self.validation, CandidateValidationStatusV4):
            raise ValueError("prior hypothesis v4 validation is invalid")
        if self.discovery_cagr_pct is not None:
            _v4_cagr(self.discovery_cagr_pct, "prior hypothesis v4 discovery CAGR")
        if self.validation.status == "valid" and self.discovery_cagr_pct is None:
            raise ValueError("valid prior hypothesis requires discovery CAGR")
        if self.validation.status != "valid" and self.discovery_cagr_pct is not None:
            raise ValueError("invalid prior hypothesis cannot carry discovery CAGR")
        if self.critic_disposition not in _V4_CRITIC_DISPOSITIONS:
            raise ValueError("prior hypothesis v4 critic disposition is invalid")


def _validate_role_common_v4(
    *,
    schema_version: int,
    iteration: int,
    run_manifest_sha256: str,
    policy_authoring_scope_sha256: str,
    policy_interface_version: int,
    immutable_constraint_ids: tuple[str, ...],
    annualized_return_target: AnnualizedReturnTarget,
    discovery_panel_plan_sha256: str,
    quick_panel_sha256: str,
    discovery_panel_sha256: str,
    selected_parent_identity: SelectedParentIdentity,
) -> None:
    if schema_version != 4:
        raise ValueError("optimizer v4 role input schema is unsupported")
    _require_positive_int(iteration, "optimizer v4 role iteration")
    _require_digest(run_manifest_sha256, "optimizer v4 role manifest SHA-256")
    _require_digest(
        policy_authoring_scope_sha256,
        "optimizer v4 role authoring scope SHA-256",
    )
    if policy_interface_version != 2:
        raise ValueError("optimizer v4 role policy interface must be 2")
    _v2_string_tuple(immutable_constraint_ids, "optimizer v4 immutable constraints")
    if not isinstance(annualized_return_target, AnnualizedReturnTarget):
        raise ValueError("optimizer v4 role annualized target is invalid")
    _require_digest(
        discovery_panel_plan_sha256,
        "optimizer v4 role discovery plan SHA-256",
    )
    _require_digest(quick_panel_sha256, "optimizer v4 role quick panel SHA-256")
    _require_digest(
        discovery_panel_sha256,
        "optimizer v4 role discovery panel SHA-256",
    )
    if not isinstance(selected_parent_identity, SelectedParentIdentity):
        raise ValueError("optimizer v4 role selected parent identity is invalid")


def _validate_role_v4_budget(
    *,
    role_context: object,
    role: str,
    iteration: int,
    payload: bytes,
    budget: PitOptimizerCallBudget,
    scope: PolicyAuthoringScopeV4,
    manifest: PitOptimizerRunManifestV4,
    expected_scope_sha256: str,
    source_component_bytes: int,
    feedback_component: bytes,
    history_component: bytes | None = None,
) -> None:
    if not isinstance(manifest, PitOptimizerRunManifestV4):
        raise ValueError("optimizer v4 role manifest is invalid")
    if not isinstance(scope, PolicyAuthoringScopeV4):
        raise ValueError("optimizer v4 role authoring scope is invalid")
    if (
        manifest.sha256 != getattr(role_context, "run_manifest_sha256", None)
        or manifest.policy_authoring_scope != scope
        or manifest.policy_interface_version
        != getattr(role_context, "policy_interface_version", None)
        or manifest.immutable_constraint_ids
        != getattr(role_context, "immutable_constraint_ids", None)
        or manifest.annualized_return_target
        != getattr(role_context, "annualized_return_target", None)
        or manifest.discovery_panel_plan_sha256
        != getattr(role_context, "discovery_panel_plan_sha256", None)
        or manifest.quick_panel_sha256
        != getattr(role_context, "quick_panel_sha256", None)
        or manifest.discovery_panel_sha256
        != getattr(role_context, "discovery_panel_sha256", None)
    ):
        raise ValueError("optimizer v4 role manifest binding differs")
    if scope.sha256 != expected_scope_sha256:
        raise ValueError("optimizer v4 role authoring scope binding differs")
    role_ordinal = OPTIMIZER_V4_ROLES.index(role) + 1
    expected_call_index = (iteration - 1) * len(OPTIMIZER_V4_ROLES) + role_ordinal
    if (
        not isinstance(budget, PitOptimizerCallBudget)
        or budget.role != role
        or budget.iteration != iteration
        or budget.call_index != expected_call_index
        or expected_call_index > len(scope.call_budgets)
        or scope.call_budgets[expected_call_index - 1] != budget
    ):
        raise ValueError("optimizer v4 role budget binding differs")
    if len(feedback_component) > scope.max_iteration_feedback_bytes:
        raise ValueError("optimizer v4 role feedback component exceeds scope")
    if history_component is not None and (
        len(history_component) > scope.max_iteration_history_bytes
    ):
        raise ValueError("optimizer v4 role history component exceeds scope")
    component_bytes = source_component_bytes + len(feedback_component)
    if history_component is not None:
        component_bytes += len(history_component)
    if component_bytes > budget.max_dynamic_input_bytes:
        raise ValueError("optimizer v4 role components exceed dynamic call cap")
    static_bytes = len(PIT_OPTIMIZER_V4_SYSTEM_PROMPTS[role].encode("utf-8"))
    static_bytes += len(_v2_canonical_bytes(pit_optimizer_v4_response_format(role)))
    if static_bytes > budget.max_static_input_bytes:
        raise ValueError("optimizer v4 static role context exceeds call cap")
    if len(payload) > budget.max_dynamic_input_bytes:
        raise ValueError("optimizer v4 dynamic role context exceeds call cap")


@dataclass(frozen=True, slots=True)
class InvestigatorInputV4(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    policy_authoring_scope_sha256: str
    policy_interface_version: int
    immutable_constraint_ids: tuple[str, ...]
    annualized_return_target: AnnualizedReturnTarget
    discovery_panel_plan_sha256: str
    quick_panel_sha256: str
    discovery_panel_sha256: str
    selected_parent_identity: SelectedParentIdentity
    selected_parent_source_bundle_sha256: str
    selected_parent_sources: tuple[AuthorSourceFile, ...]
    selected_parent_summary: SelectedParentSummary
    baseline_summary: SelectedParentSummary
    champion_summary: SelectedParentSummary | None
    branch_summary: SelectedParentSummary | None
    target_progress: TargetProgressV4
    prior_hypotheses: tuple[PriorHypothesisSummaryV4, ...]
    validation_status: CandidateValidationStatusV4

    def __post_init__(self) -> None:
        _validate_role_common_v4(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            policy_authoring_scope_sha256=self.policy_authoring_scope_sha256,
            policy_interface_version=self.policy_interface_version,
            immutable_constraint_ids=self.immutable_constraint_ids,
            annualized_return_target=self.annualized_return_target,
            discovery_panel_plan_sha256=self.discovery_panel_plan_sha256,
            quick_panel_sha256=self.quick_panel_sha256,
            discovery_panel_sha256=self.discovery_panel_sha256,
            selected_parent_identity=self.selected_parent_identity,
        )
        _require_digest(
            self.selected_parent_source_bundle_sha256,
            "investigator v4 selected parent source bundle SHA-256",
        )
        self.selected_parent_identity.validate_sources(self.selected_parent_sources)
        if (
            self.selected_parent_source_bundle_sha256
            != self.selected_parent_identity.source_bundle_sha256
            or not isinstance(self.selected_parent_summary, SelectedParentSummary)
            or self.selected_parent_summary.identity != self.selected_parent_identity
        ):
            raise ValueError("investigator v4 selected parent binding differs")
        if (
            not isinstance(self.baseline_summary, SelectedParentSummary)
            or self.baseline_summary.identity.parent_kind != "baseline"
        ):
            raise ValueError("investigator v4 baseline summary is invalid")
        if self.champion_summary is not None and (
            not isinstance(self.champion_summary, SelectedParentSummary)
            or self.champion_summary.identity.parent_kind != "champion"
        ):
            raise ValueError("investigator v4 champion summary is invalid")
        if self.branch_summary is not None and (
            not isinstance(self.branch_summary, SelectedParentSummary)
            or self.branch_summary.identity.parent_kind != "branch"
        ):
            raise ValueError("investigator v4 branch summary is invalid")
        summaries = tuple(
            summary
            for summary in (
                self.selected_parent_summary,
                self.baseline_summary,
                self.champion_summary,
                self.branch_summary,
            )
            if summary is not None
        )
        for summary in summaries:
            summary.validate_panel_identities(
                quick_panel_sha256=self.quick_panel_sha256,
                discovery_panel_sha256=self.discovery_panel_sha256,
            )
        if self.branch_summary is not None:
            expected_parent = self.branch_summary
        elif self.champion_summary is not None:
            expected_parent = self.champion_summary
        else:
            expected_parent = self.baseline_summary
        if self.selected_parent_summary != expected_parent:
            raise ValueError(
                "investigator v4 selected parent differs from deterministic parent state"
            )
        if not isinstance(self.target_progress, TargetProgressV4):
            raise ValueError("investigator v4 target progress is invalid")
        champion_panel = (
            self.champion_summary.discovery_panel
            if self.champion_summary is not None
            else self.baseline_summary.discovery_panel
        )
        self.target_progress.validate_summaries(
            target=self.annualized_return_target,
            baseline=self.baseline_summary.discovery_panel,
            selected_parent=self.selected_parent_summary.discovery_panel,
            champion=champion_panel,
            discovery_panel_sha256=self.discovery_panel_sha256,
        )
        if (
            type(self.prior_hypotheses) is not tuple
            or any(
                not isinstance(item, PriorHypothesisSummaryV4)
                for item in self.prior_hypotheses
            )
            or tuple(item.iteration for item in self.prior_hypotheses)
            != tuple(range(1, self.iteration))
        ):
            raise ValueError("investigator v4 prior hypotheses must be contiguous")
        if not isinstance(self.validation_status, CandidateValidationStatusV4):
            raise ValueError("investigator v4 validation status is invalid")

    def validate_budget(
        self,
        budget: PitOptimizerCallBudget,
        *,
        scope: PolicyAuthoringScopeV4,
        manifest: PitOptimizerRunManifestV4,
    ) -> None:
        feedback_component = _v2_canonical_bytes(
            {
                "selected_parent_summary": self.selected_parent_summary,
                "baseline_summary": self.baseline_summary,
                "champion_summary": self.champion_summary,
                "branch_summary": self.branch_summary,
                "target_progress": self.target_progress,
                "validation_status": self.validation_status,
            }
        )
        history_component = _v2_canonical_bytes(
            {"prior_hypotheses": self.prior_hypotheses}
        )
        _validate_role_v4_budget(
            role_context=self,
            role="investigator",
            iteration=self.iteration,
            payload=self.canonical_json_bytes(),
            budget=budget,
            scope=scope,
            manifest=manifest,
            expected_scope_sha256=self.policy_authoring_scope_sha256,
            source_component_bytes=len(
                policy_source_bundle_v4_bytes(self.selected_parent_sources)
            ),
            feedback_component=feedback_component,
            history_component=history_component,
        )


@dataclass(frozen=True, slots=True)
class AuthorInputV4(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    policy_authoring_scope_sha256: str
    policy_interface_version: int
    immutable_constraint_ids: tuple[str, ...]
    annualized_return_target: AnnualizedReturnTarget
    discovery_panel_plan_sha256: str
    quick_panel_sha256: str
    discovery_panel_sha256: str
    selected_parent_identity: SelectedParentIdentity
    selected_parent_source_bundle_sha256: str
    selected_parent_sources: tuple[AuthorSourceFile, ...]
    investigator: InvestigatorArtifactV4

    def __post_init__(self) -> None:
        _validate_role_common_v4(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            policy_authoring_scope_sha256=self.policy_authoring_scope_sha256,
            policy_interface_version=self.policy_interface_version,
            immutable_constraint_ids=self.immutable_constraint_ids,
            annualized_return_target=self.annualized_return_target,
            discovery_panel_plan_sha256=self.discovery_panel_plan_sha256,
            quick_panel_sha256=self.quick_panel_sha256,
            discovery_panel_sha256=self.discovery_panel_sha256,
            selected_parent_identity=self.selected_parent_identity,
        )
        _require_digest(
            self.selected_parent_source_bundle_sha256,
            "author v4 selected parent source bundle SHA-256",
        )
        self.selected_parent_identity.validate_sources(self.selected_parent_sources)
        if (
            self.selected_parent_source_bundle_sha256
            != self.selected_parent_identity.source_bundle_sha256
        ):
            raise ValueError("author v4 selected parent source binding differs")
        if not isinstance(self.investigator, InvestigatorArtifactV4):
            raise ValueError("author v4 investigator artifact is invalid")

    def validate_artifact(self, artifact: AuthorArtifactV4) -> None:
        if not isinstance(artifact, AuthorArtifactV4):
            raise ValueError("author v4 response has an invalid type")
        if (
            artifact.hypothesis_id != self.investigator.hypothesis_id
            or artifact.parent_identity_sha256
            != self.selected_parent_identity.parent_identity_sha256
        ):
            raise ValueError("author v4 response binding differs from its input")

    def validate_budget(
        self,
        budget: PitOptimizerCallBudget,
        *,
        scope: PolicyAuthoringScopeV4,
        manifest: PitOptimizerRunManifestV4,
    ) -> None:
        _validate_role_v4_budget(
            role_context=self,
            role="author",
            iteration=self.iteration,
            payload=self.canonical_json_bytes(),
            budget=budget,
            scope=scope,
            manifest=manifest,
            expected_scope_sha256=self.policy_authoring_scope_sha256,
            source_component_bytes=len(
                policy_source_bundle_v4_bytes(self.selected_parent_sources)
            ),
            feedback_component=_v2_canonical_bytes(
                {"investigator": self.investigator}
            ),
        )


@dataclass(frozen=True, slots=True)
class CriticInputV4(_V2Canonical):
    schema_version: int
    iteration: int
    run_manifest_sha256: str
    policy_authoring_scope_sha256: str
    policy_interface_version: int
    immutable_constraint_ids: tuple[str, ...]
    annualized_return_target: AnnualizedReturnTarget
    discovery_panel_plan_sha256: str
    quick_panel_sha256: str
    discovery_panel_sha256: str
    selected_parent_identity: SelectedParentIdentity
    selected_parent_summary: SelectedParentSummary
    hypothesis_id: str
    investigator_summary: InvestigatorArtifactV4
    author_manifest: AuthorManifestSummaryV4 | None
    author_output_invalid: RoleOutputInvalidSummary | None
    validation_status: CandidateValidationStatusV4
    candidate_quick: PanelAggregateSummary | None
    candidate_discovery: PanelAggregateSummary | None
    baseline_quick: PanelAggregateSummary
    baseline_discovery: PanelAggregateSummary
    champion_discovery: PanelAggregateSummary
    target_progress: TargetProgressV4

    def __post_init__(self) -> None:
        _validate_role_common_v4(
            schema_version=self.schema_version,
            iteration=self.iteration,
            run_manifest_sha256=self.run_manifest_sha256,
            policy_authoring_scope_sha256=self.policy_authoring_scope_sha256,
            policy_interface_version=self.policy_interface_version,
            immutable_constraint_ids=self.immutable_constraint_ids,
            annualized_return_target=self.annualized_return_target,
            discovery_panel_plan_sha256=self.discovery_panel_plan_sha256,
            quick_panel_sha256=self.quick_panel_sha256,
            discovery_panel_sha256=self.discovery_panel_sha256,
            selected_parent_identity=self.selected_parent_identity,
        )
        if (
            not isinstance(self.selected_parent_summary, SelectedParentSummary)
            or self.selected_parent_summary.identity != self.selected_parent_identity
        ):
            raise ValueError("critic v4 selected parent summary binding differs")
        self.selected_parent_summary.validate_panel_identities(
            quick_panel_sha256=self.quick_panel_sha256,
            discovery_panel_sha256=self.discovery_panel_sha256,
        )
        _v2_identifier(self.hypothesis_id, "critic v4 hypothesis ID")
        if (
            not isinstance(self.investigator_summary, InvestigatorArtifactV4)
            or self.investigator_summary.hypothesis_id != self.hypothesis_id
        ):
            raise ValueError("critic v4 investigator binding differs")
        if (self.author_manifest is None) == (self.author_output_invalid is None):
            raise ValueError(
                "critic v4 requires exactly one author manifest or invalid summary"
            )
        if self.author_manifest is not None:
            if (
                not isinstance(self.author_manifest, AuthorManifestSummaryV4)
                or self.author_manifest.hypothesis_id != self.hypothesis_id
                or self.author_manifest.parent_identity_sha256
                != self.selected_parent_identity.parent_identity_sha256
            ):
                raise ValueError("critic v4 author manifest binding differs")
        else:
            if (
                not isinstance(self.author_output_invalid, RoleOutputInvalidSummary)
                or self.author_output_invalid.role != "author"
                or self.author_output_invalid.iteration != self.iteration
                or self.author_output_invalid.call_index
                != (self.iteration - 1) * len(OPTIMIZER_V4_ROLES) + 2
            ):
                raise ValueError("critic v4 invalid summary differs from its author slot")
        if not isinstance(self.validation_status, CandidateValidationStatusV4):
            raise ValueError("critic v4 validation status is invalid")
        if self.author_output_invalid is not None and (
            self.validation_status.status != "invalid"
            or self.validation_status.failure_code != "author_output_invalid"
        ):
            raise ValueError("critic v4 invalid author status differs")
        if self.author_manifest is not None and (
            self.validation_status.failure_code == "author_output_invalid"
        ):
            raise ValueError("critic v4 valid author cannot use invalid-output status")
        if self.candidate_quick is not None:
            _require_v4_panel_summary(
                self.candidate_quick,
                "quick",
                "critic v4 candidate quick",
                expected_sha256=self.quick_panel_sha256,
            )
        if self.candidate_discovery is not None:
            _require_v4_panel_summary(
                self.candidate_discovery,
                "discovery",
                "critic v4 candidate discovery",
                expected_sha256=self.discovery_panel_sha256,
            )
            if self.candidate_quick is None:
                raise ValueError("critic v4 discovery evidence requires quick evidence")
        has_complete_candidate_evidence = (
            self.candidate_quick is not None and self.candidate_discovery is not None
        )
        if self.validation_status.status == "valid":
            if not has_complete_candidate_evidence:
                raise ValueError("critic v4 valid status requires both candidate panels")
        elif self.candidate_quick is not None or self.candidate_discovery is not None:
            raise ValueError("critic v4 nonvalid status cannot carry candidate panels")
        _require_v4_panel_summary(
            self.baseline_quick,
            "quick",
            "critic v4 baseline quick",
            expected_sha256=self.quick_panel_sha256,
        )
        _require_v4_panel_summary(
            self.baseline_discovery,
            "discovery",
            "critic v4 baseline discovery",
            expected_sha256=self.discovery_panel_sha256,
        )
        _require_v4_panel_summary(
            self.champion_discovery,
            "discovery",
            "critic v4 champion discovery",
            expected_sha256=self.discovery_panel_sha256,
        )
        if not isinstance(self.target_progress, TargetProgressV4):
            raise ValueError("critic v4 target progress is invalid")
        self.target_progress.validate_summaries(
            target=self.annualized_return_target,
            baseline=self.baseline_discovery,
            selected_parent=self.selected_parent_summary.discovery_panel,
            champion=self.champion_discovery,
            discovery_panel_sha256=self.discovery_panel_sha256,
        )
        if any(
            key in self.to_primitive()
            for key in ("selected_parent_sources", "policy_sources", "source")
        ):
            raise ValueError("critic v4 input cannot expose policy source")

    def validate_artifact(self, artifact: CriticArtifactV4) -> None:
        if not isinstance(artifact, CriticArtifactV4):
            raise ValueError("critic v4 response has an invalid type")
        if artifact.hypothesis_id != self.hypothesis_id:
            raise ValueError("critic v4 hypothesis differs from its input")

    def validate_budget(
        self,
        budget: PitOptimizerCallBudget,
        *,
        scope: PolicyAuthoringScopeV4,
        manifest: PitOptimizerRunManifestV4,
    ) -> None:
        feedback_component = _v2_canonical_bytes(
            {
                "selected_parent_summary": self.selected_parent_summary,
                "hypothesis_id": self.hypothesis_id,
                "investigator_summary": self.investigator_summary,
                "author_manifest": self.author_manifest,
                "author_output_invalid": self.author_output_invalid,
                "validation_status": self.validation_status,
                "candidate_quick": self.candidate_quick,
                "candidate_discovery": self.candidate_discovery,
                "baseline_quick": self.baseline_quick,
                "baseline_discovery": self.baseline_discovery,
                "champion_discovery": self.champion_discovery,
                "target_progress": self.target_progress,
            }
        )
        _validate_role_v4_budget(
            role_context=self,
            role="critic",
            iteration=self.iteration,
            payload=self.canonical_json_bytes(),
            budget=budget,
            scope=scope,
            manifest=manifest,
            expected_scope_sha256=self.policy_authoring_scope_sha256,
            source_component_bytes=0,
            feedback_component=feedback_component,
        )
