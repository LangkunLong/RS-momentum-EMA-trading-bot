"""Immutable evaluator, campaign, and learning contracts for PIT optimizer V5."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from core.backtest_fills import FrictionScenario
from core.pit_optimizer_evaluation import EvaluationPanelSpec


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"v5\.[a-z0-9_.-]{1,120}")
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_ARTIFACT_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED_ARTIFACT_COMPONENTS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_TARGET_QUANTUM_V5 = Decimal("0.01")
ARTIFACT_ROOT_V5 = ".artifacts/pit-optimizer-v5"
MAX_ROLE_EVIDENCE_ITEMS_V5 = 2048
MAX_ROLE_EVIDENCE_BYTES_V5 = 384 * 1024

# This is a closed semantic-runtime set, not a repository hash.  In particular,
# documentation, orchestration, provider, and CLI files do not affect evaluator
# identity.  The four editable ``strategy_policy/v3`` modules are also excluded:
# their exact bytes are authenticated separately by PolicyRevisionIdentityV5.
# diagnostics.py is deliberately required before the source identity can be
# issued, even though it is introduced by the next implementation task.
EVALUATOR_SOURCE_PATHS_V5 = (
    "backtest.py",
    "config/__init__.py",
    "config/settings.py",
    "core/backtest_engine.py",
    "core/backtest_fills.py",
    "core/canslim/__init__.py",
    "core/canslim/a_annual_earnings.py",
    "core/canslim/c_current_earnings.py",
    "core/canslim/core.py",
    "core/canslim/earnings_trace.py",
    "core/canslim/entry_contract.py",
    "core/canslim/fiscal_periods.py",
    "core/canslim/i_institutional.py",
    "core/canslim/l_leader_laggard.py",
    "core/canslim/m_market_direction.py",
    "core/canslim/n_new_products.py",
    "core/canslim/s_supply_demand.py",
    "core/data_client.py",
    "core/engine_policy.py",
    "core/industry_group.py",
    "core/index_ticker_fetcher.py",
    "core/leader_evaluation.py",
    "core/momentum_analysis.py",
    "core/pit_data.py",
    "core/pit_diagnosis/fact_cache.py",
    "core/pit_diagnosis/models.py",
    "core/pit_diagnosis/patterns.py",
    "core/pit_diagnosis/rs.py",
    "core/pit_diagnosis/rulebook.py",
    "core/pit_diagnosis/supplemental.py",
    "core/pit_feature_snapshot.py",
    "core/pit_optimizer_evaluation.py",
    "core/pit_optimizer_v5/candidate_ir.py",
    "core/pit_optimizer_v5/container_entry.py",
    "core/pit_optimizer_v5/container_protocol.py",
    "core/pit_optimizer_v5/contracts.py",
    "core/pit_optimizer_v5/diagnostics.py",
    "core/pit_optimizer_v5/evaluator.py",
    "core/pit_optimizer_v5/image_manifest.py",
    "core/pit_optimizer_v5/policy_scope.py",
    "core/pit_optimizer_v5/probe_entry.py",
    "core/pit_optimizer_v5/probes.py",
    "core/pit_provenance.py",
    "core/pit_universe_v3.py",
    "core/strategy_policy/__init__.py",
    "core/strategy_policy/adapter_v3.py",
    "core/strategy_policy/contracts.py",
    "core/strategy_policy/contracts_v3.py",
    "core/strategy_policy/entry.py",
    "core/strategy_policy/exit.py",
    "core/strategy_policy/market_context.py",
    "core/strategy_policy/risk.py",
    "core/strategy_policy/runtime.py",
    "core/strategy_policy/v3/__init__.py",
    "core/strategy_policy/worker.py",
    "core/trading_sessions.py",
)


class SandboxResourceManifestV5(Protocol):
    """Resource fields consumed from the authenticated campaign manifest."""

    evaluation_cpu_limit: Decimal
    evaluation_memory_mib: int
    evaluation_pid_limit: int
    evaluation_output_limit_bytes: int


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    return value


def _count(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _evidence_ids(value: object, label: str, *, required: bool = True) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or (required and not value)
        or any(type(item) is not str or _EVIDENCE_ID_RE.fullmatch(item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must contain unique issued V5 evidence IDs")
    return value


def _date(value: object, label: str) -> date:
    _text(value, label)
    try:
        parsed = date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be an ISO calendar date")
    return parsed


def _validate_ordered_disjoint_date_intervals_v5(intervals: tuple[tuple[str, str], ...], label: str) -> None:
    """Require chronologically ordered, non-overlapping closed date intervals."""

    parsed = tuple((_date(start, f"{label} start date"), _date(end, f"{label} end date")) for start, end in intervals)
    if any(left_end >= right_start for (_, left_end), (right_start, _) in pairwise(parsed)):
        raise ValueError(f"{label} must be chronological and disjoint under inclusive-date semantics")


def _decimal_primitive(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    # normalize() applies the ambient Decimal precision and can round an
    # authenticated price/cost before hashing it.
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _primitive(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_primitive(value)
    if isinstance(value, bytes):
        # Closed protocol JSON bytes (for example semantic probe decisions)
        # retain their exact UTF-8 text inside authenticated authority records.
        # Binary campaign artifacts never pass through this JSON codec.
        return value.decode("utf-8")
    if isinstance(value, FrictionScenario):
        return {
            "scenario_id": value.scenario_id,
            "half_spread_bps": value.half_spread_bps,
            "market_impact_bps": value.market_impact_bps,
            "commission_bps": value.commission_bps,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    return value


def _sha256(value: object) -> str:
    payload = json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_primitive_v5(value: object) -> object:
    """Return the dependency-leaf canonical primitive used by V5 identities.

    Infrastructure modules may build closed envelopes around this primitive,
    but they must not silently choose a second Decimal or tuple encoding.
    """

    return _primitive(value)


def canonical_json_bytes_v5(value: object) -> bytes:
    """Encode one V5 semantic value as compact canonical UTF-8 JSON."""

    return json.dumps(
        canonical_primitive_v5(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256_v5(value: object) -> str:
    """Hash the canonical V5 semantic representation of ``value``."""

    return hashlib.sha256(canonical_json_bytes_v5(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class AnnualizedReturnTargetV5:
    target_pct: Decimal
    metric_id: Literal["portfolio_annualized_return_pct"] = "portfolio_annualized_return_pct"
    basis: Literal["absolute"] = "absolute"

    def __post_init__(self) -> None:
        target = _decimal(self.target_pct, "annualized return target")
        if target <= 0 or target != target.quantize(_TARGET_QUANTUM_V5) or target.as_tuple().exponent != -2:
            raise ValueError("annualized return target must be positive with two decimals")
        if type(self.metric_id) is not str or self.metric_id != "portfolio_annualized_return_pct":
            raise ValueError("annualized return target metric is invalid")
        if type(self.basis) is not str or self.basis != "absolute":
            raise ValueError("annualized return target basis is invalid")

    @classmethod
    def from_text(cls, target_pct: str) -> "AnnualizedReturnTargetV5":
        _text(target_pct, "annualized return target text")
        try:
            parsed = Decimal(target_pct)
        except Exception as exc:
            raise ValueError("annualized return target text is invalid") from exc
        return cls(target_pct=parsed)

    def to_text(self) -> str:
        return format(self.target_pct, ".2f")

    def to_primitive(self) -> dict[str, str]:
        return {
            "target_pct": self.to_text(),
            "metric_id": self.metric_id,
            "basis": self.basis,
        }

    @classmethod
    def from_primitive(cls, value: Mapping[str, object]) -> "AnnualizedReturnTargetV5":
        if not isinstance(value, Mapping) or set(value) != {
            "target_pct",
            "metric_id",
            "basis",
        }:
            raise ValueError("annualized return target payload is invalid")
        if type(value["target_pct"]) is not str:
            raise ValueError("annualized return target payload is invalid")
        target = cls.from_text(value["target_pct"])
        return cls(
            target_pct=target.target_pct,
            metric_id=value["metric_id"],  # type: ignore[arg-type]
            basis=value["basis"],  # type: ignore[arg-type]
        )

    @property
    def sha256(self) -> str:
        return _sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class SearchCapabilitiesV5:
    hypotheses_per_investigator: int = 3
    max_tunable_axes: int = 4
    max_variants_per_template: int = 12
    max_discovery_survivors_per_template: int = 6
    archive_capacity: int = 8
    max_feedback_rounds: int = 10
    allow_full_source_escape: bool = True
    investigator_memory_max_bytes: int = 96 * 1024

    def __post_init__(self) -> None:
        for value, label in (
            (self.hypotheses_per_investigator, "hypotheses per investigator"),
            (self.max_tunable_axes, "maximum tunable axes"),
            (self.max_variants_per_template, "maximum variants per template"),
            (
                self.max_discovery_survivors_per_template,
                "maximum discovery survivors per template",
            ),
            (self.archive_capacity, "archive capacity"),
            (self.max_feedback_rounds, "maximum feedback rounds"),
        ):
            _count(value, label, positive=True)
        if self.max_discovery_survivors_per_template > self.max_variants_per_template:
            raise ValueError("discovery survivor capacity exceeds variant capacity")
        if type(self.allow_full_source_escape) is not bool:
            raise ValueError("full source escape capability must be boolean")
        _count(self.investigator_memory_max_bytes, "investigator memory byte budget", positive=True)


@dataclass(frozen=True, slots=True)
class ModelPriceUpperBoundV5:
    """Operator-authenticated worst-case model prices, including provider surcharges."""

    model: str
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal

    def __post_init__(self) -> None:
        _text(self.model, "priced provider model")
        for value in (self.input_usd_per_million_tokens, self.output_usd_per_million_tokens):
            if _decimal(value, "model price upper bound") < 0:
                raise ValueError("model price upper bound must be nonnegative")


@dataclass(frozen=True, slots=True)
class ProviderCapabilitiesV5:
    model: str
    maximum_role_calls: int
    maximum_total_tokens: int
    maximum_output_tokens_per_role: int
    maximum_usd: Decimal | None = None
    automatic_retries: int = 0
    schema_repair_calls: int = 0
    price_upper_bound: ModelPriceUpperBoundV5 | None = None
    input_token_overhead_upper_bound: int = 4096

    def __post_init__(self) -> None:
        _text(self.model, "provider model")
        _count(self.maximum_role_calls, "maximum role calls", positive=True)
        _count(self.maximum_total_tokens, "maximum total tokens", positive=True)
        _count(
            self.maximum_output_tokens_per_role,
            "maximum output tokens per role",
            positive=True,
        )
        if self.maximum_output_tokens_per_role > self.maximum_total_tokens:
            raise ValueError("per-role output token bound exceeds total token bound")
        if self.maximum_usd is not None and _decimal(self.maximum_usd, "maximum provider USD") <= 0:
            raise ValueError("maximum provider USD must be positive")
        _count(self.automatic_retries, "automatic retries")
        _count(self.schema_repair_calls, "schema repair calls")
        _count(self.input_token_overhead_upper_bound, "input token overhead upper bound", positive=True)
        if self.price_upper_bound is not None and (
            type(self.price_upper_bound) is not ModelPriceUpperBoundV5 or self.price_upper_bound.model != self.model
        ):
            raise ValueError("provider model price authority differs")
        if self.maximum_usd is not None and self.price_upper_bound is None:
            raise ValueError("finite provider USD ceiling requires model price upper bounds")


@dataclass(frozen=True, slots=True)
class ArtifactRefV5:
    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        path = _text(self.relative_path, "artifact relative path")
        pure = PurePosixPath(path)
        unsafe_component = any(
            _ARTIFACT_COMPONENT_RE.fullmatch(part) is None
            or part.endswith((".", " "))
            or ":" in part
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_ARTIFACT_COMPONENTS
            for part in pure.parts
        )
        if (
            "\\" in path
            or pure.is_absolute()
            or not pure.parts
            or pure.as_posix() != path
            or any(part in {"", ".", ".."} for part in pure.parts)
            or unsafe_component
        ):
            raise ValueError("artifact path must be canonical POSIX beneath the V5 root")
        _digest(self.sha256, "artifact SHA-256")

    def to_primitive(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class BaselineAuthorityV5:
    """Complete baseline truth; the separate parent projection keeps its V5 identity."""

    schema_version: Literal[5]
    evaluator_contract_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    panel_plan_ref: ArtifactRefV5
    baseline_policy_revision_ref: ArtifactRefV5
    mechanics_evidence_ref: ArtifactRefV5
    quick_evidence_ref: ArtifactRefV5
    discovery_evidence_ref: ArtifactRefV5
    deterministic_repeat_ref: ArtifactRefV5
    capture_inputs_ref: ArtifactRefV5
    parent_authority_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("baseline authority schema must be V5")
        references = tuple(getattr(self, item.name) for item in fields(self) if item.name != "schema_version")
        if any(type(item) is not ArtifactRefV5 for item in references):
            raise ValueError("baseline authority requires every authenticated edge")
        if len({item.relative_path for item in references}) != len(references):
            raise ValueError("baseline authority dependency paths must be distinct")

    @property
    def sha256(self) -> str:
        return _sha256(self)


ArtifactGraphFailureCodeV5 = Literal["missing", "relocated", "cycle", "digest_mismatch"]


@dataclass(frozen=True, slots=True)
class ArtifactGraphFailureV5:
    code: ArtifactGraphFailureCodeV5
    reference: ArtifactRefV5
    relocated_path: str | None = None
    actual_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.code not in {"missing", "relocated", "cycle", "digest_mismatch"}:
            raise ValueError("artifact graph failure code is invalid")
        if type(self.reference) is not ArtifactRefV5:
            raise ValueError("artifact graph failure reference is invalid")
        if self.code == "relocated":
            if self.relocated_path is None:
                raise ValueError("relocated artifact failure requires a path")
            ArtifactRefV5(self.relocated_path, self.reference.sha256)
        elif self.relocated_path is not None:
            raise ValueError("only relocated failures may name a relocated path")
        if self.code == "digest_mismatch":
            if self.actual_sha256 is None:
                raise ValueError("digest mismatch requires the observed digest")
            _digest(self.actual_sha256, "observed artifact SHA-256")
        elif self.actual_sha256 is not None:
            raise ValueError("only digest mismatches may carry an observed digest")


@dataclass(frozen=True, slots=True)
class AuthenticatedArtifactV5:
    """Bytes authenticated against one canonical reference before decoding."""

    reference: ArtifactRefV5
    content: bytes
    child_references: tuple[ArtifactRefV5, ...] = ()

    def __post_init__(self) -> None:
        if type(self.reference) is not ArtifactRefV5 or type(self.content) is not bytes:
            raise ValueError("authenticated artifact payload is invalid")
        if hashlib.sha256(self.content).hexdigest() != self.reference.sha256:
            raise ValueError("authenticated artifact bytes differ from their reference")
        if type(self.child_references) is not tuple or any(
            type(item) is not ArtifactRefV5 for item in self.child_references
        ):
            raise ValueError("artifact child references are invalid")
        keys = tuple((item.relative_path, item.sha256) for item in self.child_references)
        if len(set(keys)) != len(keys):
            raise ValueError("artifact child references must be unique")


@dataclass(frozen=True, slots=True)
class AuthenticatedRawArtifactV5:
    """Digest-verified campaign bytes, streamed without treating data as a graph."""

    reference: ArtifactRefV5
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.reference) is not ArtifactRefV5:
            raise ValueError("raw artifact reference is invalid")
        _count(self.byte_count, "raw artifact byte count")


@dataclass(frozen=True, slots=True)
class ArtifactIndexV5:
    """Canonical authenticated child list for a multi-file artifact."""

    schema_version: Literal[5]
    child_references: tuple[ArtifactRefV5, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("artifact index schema must be V5")
        if (
            type(self.child_references) is not tuple
            or not self.child_references
            or any(type(item) is not ArtifactRefV5 for item in self.child_references)
        ):
            raise ValueError("artifact index child references are invalid")
        keys = tuple((item.relative_path, item.sha256) for item in self.child_references)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("artifact index child references must be unique and canonical")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class ArtifactGraphVerificationV5:
    manifest_ref: ArtifactRefV5
    authenticated: tuple[AuthenticatedArtifactV5 | AuthenticatedRawArtifactV5, ...]
    failure: ArtifactGraphFailureV5 | None

    def __post_init__(self) -> None:
        if type(self.manifest_ref) is not ArtifactRefV5:
            raise ValueError("artifact graph manifest reference is invalid")
        if type(self.authenticated) is not tuple or any(
            type(item) not in {AuthenticatedArtifactV5, AuthenticatedRawArtifactV5} for item in self.authenticated
        ):
            raise ValueError("artifact graph authenticated nodes are invalid")
        keys = tuple((item.reference.relative_path, item.reference.sha256) for item in self.authenticated)
        if len(set(keys)) != len(keys):
            raise ValueError("artifact graph authenticated nodes must be unique")
        if self.failure is not None and type(self.failure) is not ArtifactGraphFailureV5:
            raise ValueError("artifact graph failure is invalid")
        if self.failure is None and self.manifest_ref not in tuple(item.reference for item in self.authenticated):
            raise ValueError("verified artifact graph must include its manifest")

    @property
    def verified(self) -> bool:
        return self.failure is None


class ArtifactGraphVerifierV5(Protocol):
    """Filesystem boundary that authenticates the complete manifest graph.

    Implementations must resolve only regular, non-symlink files beneath an
    authenticated ``.artifacts/pit-optimizer-v5`` repository root, authenticate
    bytes before decoding, recursively visit index children, and return typed
    failures instead of following relocated or cyclic references.
    """

    def verify_manifest(self, manifest_path: Path) -> ArtifactGraphVerificationV5: ...


@dataclass(frozen=True, slots=True)
class EpisodePlanV5:
    episode_id: str
    episode_ordinal: int | None
    purpose: str
    start_date: str
    end_date: str
    lineage_ids: tuple[str, ...]
    panel_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        _text(self.episode_id, "episode ID")
        if self.episode_ordinal is not None:
            _count(self.episode_ordinal, "episode ordinal", positive=True)
        _text(self.purpose, "episode purpose")
        start = _date(self.start_date, "episode start date")
        end = _date(self.end_date, "episode end date")
        if start >= end:
            raise ValueError("episode date range is invalid")
        if (
            type(self.lineage_ids) is not tuple
            or not self.lineage_ids
            or any(type(item) is not str for item in self.lineage_ids)
        ):
            raise ValueError("episode lineage IDs are invalid")
        tuple(_text(item, "episode lineage ID") for item in self.lineage_ids)
        if len(set(self.lineage_ids)) != len(self.lineage_ids):
            raise ValueError("episode lineage IDs must be unique")
        if type(self.panel_ref) is not ArtifactRefV5:
            raise ValueError("episode panel reference is invalid")


@dataclass(frozen=True, slots=True)
class CampaignPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    partition_seed_sha256: str
    target_sha256: str
    mechanics: EpisodePlanV5
    quick: EpisodePlanV5
    discovery: tuple[EpisodePlanV5, ...]
    confirmation_plan_sha256: str
    qualification_plan_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("campaign panel plan schema must be V5")
        if type(self.pit_bundle_ref) is not ArtifactRefV5 or type(self.prices_provenance_ref) is not ArtifactRefV5:
            raise ValueError("campaign panel data references are invalid")
        for value, label in (
            (self.partition_seed_sha256, "partition seed SHA-256"),
            (self.target_sha256, "target SHA-256"),
            (self.confirmation_plan_sha256, "confirmation plan SHA-256"),
            (self.qualification_plan_sha256, "qualification plan SHA-256"),
        ):
            _digest(value, label)
        if type(self.mechanics) is not EpisodePlanV5 or self.mechanics.episode_ordinal is not None:
            raise ValueError("mechanics plan must be an unnumbered V5 episode")
        if type(self.quick) is not EpisodePlanV5 or self.quick.episode_ordinal is not None:
            raise ValueError("quick plan must be an unnumbered V5 episode")
        if (
            type(self.discovery) is not tuple
            or len(self.discovery) != 4
            or any(type(item) is not EpisodePlanV5 for item in self.discovery)
            or tuple(item.episode_ordinal for item in self.discovery) != (1, 2, 3, 4)
        ):
            raise ValueError("campaign requires discovery episode ordinals 1 through 4")
        episodes = (self.mechanics, self.quick, *self.discovery)
        if len({item.episode_id for item in episodes}) != len(episodes):
            raise ValueError("campaign episode IDs must be unique")
        if len({item.panel_ref.sha256 for item in self.discovery}) != 4:
            raise ValueError("campaign discovery panel identities must be unique")
        _validate_ordered_disjoint_date_intervals_v5(
            tuple((item.start_date, item.end_date) for item in self.discovery),
            "campaign discovery episodes",
        )

    @property
    def discovery_plan_sha256(self) -> str:
        return _sha256(self.discovery)

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class ConfirmationPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_sha256: str
    partition_seed_sha256: str
    target_sha256: str
    confirmation_retirement_domain_id: str
    confirmation_ledger_snapshot_sha256: str
    episode: EpisodePlanV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("confirmation panel plan schema must be V5")
        for value, label in (
            (self.pit_bundle_sha256, "confirmation PIT bundle SHA-256"),
            (self.partition_seed_sha256, "confirmation partition seed SHA-256"),
            (self.target_sha256, "confirmation target SHA-256"),
            (self.confirmation_retirement_domain_id, "confirmation retirement domain ID"),
            (self.confirmation_ledger_snapshot_sha256, "confirmation ledger snapshot SHA-256"),
        ):
            _digest(value, label)
        if type(self.episode) is not EpisodePlanV5 or self.episode.episode_ordinal is not None:
            raise ValueError("confirmation plan must contain one unnumbered V5 episode")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class QualificationPanelPlanV5:
    schema_version: Literal[5]
    pit_bundle_sha256: str
    partition_seed_sha256: str
    target: AnnualizedReturnTargetV5
    qualification_retirement_domain_id: str
    qualification_ledger_snapshot_sha256: str
    episode: EpisodePlanV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("qualification panel plan schema must be V5")
        for value, label in (
            (self.pit_bundle_sha256, "qualification PIT bundle SHA-256"),
            (self.partition_seed_sha256, "qualification partition seed SHA-256"),
            (self.qualification_retirement_domain_id, "qualification retirement domain ID"),
            (self.qualification_ledger_snapshot_sha256, "qualification ledger snapshot SHA-256"),
        ):
            _digest(value, label)
        if type(self.target) is not AnnualizedReturnTargetV5:
            raise ValueError("qualification target must use the V5 contract")
        if type(self.episode) is not EpisodePlanV5 or self.episode.episode_ordinal is not None:
            raise ValueError("qualification plan must contain one unnumbered V5 episode")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class RetirementLedgerLocatorV5:
    relative_path: str
    preopen_snapshot_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if type(self.preopen_snapshot_ref) is not ArtifactRefV5:
            raise ValueError("retirement ledger snapshot reference is invalid")
        # The mutable ledger uses the exact containment/canonicalization grammar
        # of an immutable artifact reference without pretending its digest is
        # stable after the stage is opened.
        ArtifactRefV5(self.relative_path, self.preopen_snapshot_ref.sha256)


def _require_artifact_refs_v5(*references: ArtifactRefV5) -> None:
    if any(type(item) is not ArtifactRefV5 for item in references):
        raise ValueError("stage commitment contains an invalid artifact reference")


@dataclass(frozen=True, slots=True)
class FinalizedDiscoveryRoundV5:
    """Explicit immutable event and record closure for one completed round."""

    round_index: int
    event_refs: tuple[ArtifactRefV5, ...]
    record_refs: tuple[ArtifactRefV5, ...]

    def __post_init__(self) -> None:
        _count(self.round_index, "finalized discovery round", positive=True)
        for references in (self.event_refs, self.record_refs):
            if type(references) is not tuple or not references:
                raise ValueError("finalized discovery round requires event and record authority")
            _require_artifact_refs_v5(*references)
            if len(set(references)) != len(references):
                raise ValueError("finalized discovery references are duplicated")
        if self.record_refs != tuple(sorted(self.record_refs, key=lambda item: (item.relative_path, item.sha256))):
            raise ValueError("finalized discovery records are not canonical")


@dataclass(frozen=True, slots=True)
class FinalizedDiscoveryCampaignV5:
    """Closure authority; a checkpoint counter alone cannot close discovery."""

    schema_version: Literal[5]
    discovery_manifest_ref: ArtifactRefV5
    checkpoint_ref: ArtifactRefV5
    archive_ref: ArtifactRefV5
    rounds: tuple[FinalizedDiscoveryRoundV5, ...]
    status: Literal["completed"]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5 or self.status != "completed":
            raise ValueError("discovery campaign is not finalized")
        _require_artifact_refs_v5(self.discovery_manifest_ref, self.checkpoint_ref, self.archive_ref)
        if (
            type(self.rounds) is not tuple
            or not self.rounds
            or any(type(item) is not FinalizedDiscoveryRoundV5 for item in self.rounds)
            or tuple(item.round_index for item in self.rounds) != tuple(range(1, len(self.rounds) + 1))
        ):
            raise ValueError("finalized discovery rounds are incomplete or noncanonical")
        all_records = tuple(ref for item in self.rounds for ref in item.record_refs)
        if len(set(all_records)) != len(all_records):
            raise ValueError("finalized discovery rounds repeat experiment records")


@dataclass(frozen=True, slots=True)
class ConfirmationAttemptCommitmentV5:
    schema_version: Literal[5]
    attempt_id: str
    confirmation_plan_ref: ArtifactRefV5
    discovery_manifest_ref: ArtifactRefV5
    discovery_champion_policy_ref: ArtifactRefV5
    discovery_champion_experiment_ref: ArtifactRefV5
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    scenario_grid_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    retirement_domain_id: str
    retirement_ledger: RetirementLedgerLocatorV5
    frozen_selection_ref: ArtifactRefV5
    execution_adapter_ref: ArtifactRefV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("confirmation attempt schema must be V5")
        _text(self.attempt_id, "confirmation attempt ID")
        _require_artifact_refs_v5(
            self.confirmation_plan_ref,
            self.discovery_manifest_ref,
            self.discovery_champion_policy_ref,
            self.discovery_champion_experiment_ref,
            self.pit_bundle_ref,
            self.prices_provenance_ref,
            self.execution_profile_ref,
            self.evaluator_contract_ref,
            self.scenario_grid_ref,
            self.baseline_authority_ref,
            self.sandbox_profile_ref,
            self.frozen_selection_ref,
            self.execution_adapter_ref,
        )
        _digest(self.retirement_domain_id, "confirmation attempt retirement domain ID")
        if type(self.retirement_ledger) is not RetirementLedgerLocatorV5:
            raise ValueError("confirmation attempt retirement ledger is invalid")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class QualificationAttemptCommitmentV5:
    schema_version: Literal[5]
    attempt_id: str
    qualification_plan_ref: ArtifactRefV5
    confirmation_outcome_ref: ArtifactRefV5
    confirmed_policy_ref: ArtifactRefV5
    pit_bundle_ref: ArtifactRefV5
    prices_provenance_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    scenario_grid_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    retirement_domain_id: str
    retirement_ledger: RetirementLedgerLocatorV5
    target: AnnualizedReturnTargetV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("qualification attempt schema must be V5")
        _text(self.attempt_id, "qualification attempt ID")
        _require_artifact_refs_v5(
            self.qualification_plan_ref,
            self.confirmation_outcome_ref,
            self.confirmed_policy_ref,
            self.pit_bundle_ref,
            self.prices_provenance_ref,
            self.execution_profile_ref,
            self.evaluator_contract_ref,
            self.scenario_grid_ref,
            self.baseline_authority_ref,
            self.sandbox_profile_ref,
        )
        _digest(self.retirement_domain_id, "qualification attempt retirement domain ID")
        if type(self.retirement_ledger) is not RetirementLedgerLocatorV5:
            raise ValueError("qualification attempt retirement ledger is invalid")
        if type(self.target) is not AnnualizedReturnTargetV5:
            raise ValueError("qualification attempt requires its immutable target")

    @property
    def sha256(self) -> str:
        return _sha256(self)


StageOutcomeStatusV5 = Literal["completed", "failed", "timed_out", "cancelled"]


@dataclass(frozen=True, slots=True)
class ConfirmationOutcomeV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    status: StageOutcomeStatusV5
    confirmed_policy_ref: ArtifactRefV5
    baseline_evidence_ref: ArtifactRefV5 | None
    candidate_evidence_ref: ArtifactRefV5 | None
    baseline_cagr_pct: Decimal | None
    candidate_cagr_pct: Decimal | None
    candidate_excess_cagr_pct: Decimal | None
    behaviorally_active: bool | None
    eligible_to_request_qualification: bool
    retirement_terminal_ref: ArtifactRefV5
    cleanup_evidence_ref: ArtifactRefV5
    provider_calls: Literal[0]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("confirmation outcome schema must be V5")
        if self.status not in {"completed", "failed", "timed_out", "cancelled"}:
            raise ValueError("confirmation outcome status is invalid")
        _require_artifact_refs_v5(
            self.attempt_ref,
            self.confirmed_policy_ref,
            self.retirement_terminal_ref,
            self.cleanup_evidence_ref,
        )
        if type(self.provider_calls) is not int or self.provider_calls != 0:
            raise ValueError("confirmation must make zero provider calls")
        if self.status != "completed":
            if any(
                item is not None
                for item in (
                    self.baseline_evidence_ref,
                    self.candidate_evidence_ref,
                    self.baseline_cagr_pct,
                    self.candidate_cagr_pct,
                    self.candidate_excess_cagr_pct,
                    self.behaviorally_active,
                )
            ) or self.eligible_to_request_qualification is not False:
                raise ValueError("non-completed confirmation must carry null metrics and a false gate")
            return
        if type(self.baseline_evidence_ref) is not ArtifactRefV5 or type(self.candidate_evidence_ref) is not ArtifactRefV5:
            raise ValueError("completed confirmation requires authenticated evidence references")
        baseline = _decimal(self.baseline_cagr_pct, "confirmation baseline CAGR")
        candidate = _decimal(self.candidate_cagr_pct, "confirmation candidate CAGR")
        excess = _decimal(self.candidate_excess_cagr_pct, "confirmation excess CAGR")
        if excess != candidate - baseline:
            raise ValueError("confirmation excess CAGR must be recomputed from same-panel evidence")
        if type(self.behaviorally_active) is not bool:
            raise ValueError("completed confirmation requires behavioral activity evidence")
        expected = self.behaviorally_active and candidate > baseline
        if type(self.eligible_to_request_qualification) is not bool or self.eligible_to_request_qualification != expected:
            raise ValueError("confirmation qualification gate differs from recomputed evidence")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class QualificationOutcomeV5:
    schema_version: Literal[5]
    attempt_ref: ArtifactRefV5
    status: StageOutcomeStatusV5
    qualified_policy_ref: ArtifactRefV5
    baseline_evidence_ref: ArtifactRefV5 | None
    candidate_evidence_ref: ArtifactRefV5 | None
    target_pct: Decimal
    baseline_cagr_pct: Decimal | None
    candidate_cagr_pct: Decimal | None
    candidate_excess_cagr_pct: Decimal | None
    target_reached: bool
    baseline_beaten: bool
    qualified: bool
    retirement_terminal_ref: ArtifactRefV5
    cleanup_evidence_ref: ArtifactRefV5
    provider_calls: Literal[0]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("qualification outcome schema must be V5")
        if self.status not in {"completed", "failed", "timed_out", "cancelled"}:
            raise ValueError("qualification outcome status is invalid")
        _require_artifact_refs_v5(
            self.attempt_ref,
            self.qualified_policy_ref,
            self.retirement_terminal_ref,
            self.cleanup_evidence_ref,
        )
        target = _decimal(self.target_pct, "qualification target")
        if target <= 0:
            raise ValueError("qualification target must be positive")
        if type(self.provider_calls) is not int or self.provider_calls != 0:
            raise ValueError("qualification must make zero provider calls")
        if self.status != "completed":
            if any(
                item is not None
                for item in (
                    self.baseline_evidence_ref,
                    self.candidate_evidence_ref,
                    self.baseline_cagr_pct,
                    self.candidate_cagr_pct,
                    self.candidate_excess_cagr_pct,
                )
            ) or any((self.target_reached, self.baseline_beaten, self.qualified)):
                raise ValueError("non-completed qualification must carry null metrics and false gates")
            return
        if type(self.baseline_evidence_ref) is not ArtifactRefV5 or type(self.candidate_evidence_ref) is not ArtifactRefV5:
            raise ValueError("completed qualification requires authenticated evidence references")
        baseline = _decimal(self.baseline_cagr_pct, "qualification baseline CAGR")
        candidate = _decimal(self.candidate_cagr_pct, "qualification candidate CAGR")
        excess = _decimal(self.candidate_excess_cagr_pct, "qualification excess CAGR")
        if excess != candidate - baseline:
            raise ValueError("qualification excess CAGR must be recomputed from same-panel evidence")
        expected_target = candidate >= target
        expected_baseline = candidate > baseline
        if (
            type(self.target_reached) is not bool
            or type(self.baseline_beaten) is not bool
            or type(self.qualified) is not bool
            or self.target_reached != expected_target
            or self.baseline_beaten != expected_baseline
            or self.qualified != (expected_target and expected_baseline)
        ):
            raise ValueError("qualification gates differ from recomputed evidence")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class ResourceCapabilitiesV5:
    max_parallel_evaluations: int = 2
    evaluation_cpu_limit: Decimal = Decimal("1")
    evaluation_memory_mib: int = 1024
    evaluation_pid_limit: int = 32
    evaluation_output_limit_bytes: int = 67_108_864
    policy_method_timeout_seconds: int = 1
    worker_startup_timeout_seconds: int = 30
    role_call_timeout_seconds: int = 180
    mechanics_timeout_seconds: int = 60
    quick_timeout_seconds: int = 180
    discovery_episode_timeout_seconds: int = 600
    round_wall_timeout_seconds: int = 1800
    campaign_wall_timeout_seconds: int = 18000
    cleanup_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        _count(self.max_parallel_evaluations, "maximum parallel evaluations", positive=True)
        if _decimal(self.evaluation_cpu_limit, "evaluation CPU limit") <= 0:
            raise ValueError("evaluation CPU limit must be positive")
        for item in fields(self):
            if item.name in {"max_parallel_evaluations", "evaluation_cpu_limit"}:
                continue
            _count(getattr(self, item.name), item.name.replace("_", " "), positive=True)
        if self.round_wall_timeout_seconds < self.discovery_episode_timeout_seconds:
            raise ValueError("round wall timeout is shorter than one discovery episode")
        if self.campaign_wall_timeout_seconds < self.round_wall_timeout_seconds:
            raise ValueError("campaign wall timeout is shorter than one round")


@dataclass(frozen=True, slots=True)
class CampaignManifestV5:
    schema_version: Literal[5]
    campaign_id: str
    target: AnnualizedReturnTargetV5
    search: SearchCapabilitiesV5
    provider: ProviderCapabilitiesV5 | None
    resources: ResourceCapabilitiesV5
    artifact_root: Literal[".artifacts/pit-optimizer-v5"]
    execution_profile_ref: ArtifactRefV5
    evaluator_contract_ref: ArtifactRefV5
    baseline_authority_ref: ArtifactRefV5
    panel_plan_ref: ArtifactRefV5
    policy_scope_ref: ArtifactRefV5
    baseline_policy_revision_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    source_commit: str
    apply: Literal[False]
    qualification_allowed: Literal[False]
    full_replay_allowed: Literal[False]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("campaign manifest schema must be V5")
        _text(self.campaign_id, "campaign ID")
        if type(self.target) is not AnnualizedReturnTargetV5:
            raise ValueError("campaign target is invalid")
        if type(self.search) is not SearchCapabilitiesV5:
            raise ValueError("campaign search capabilities are invalid")
        if self.provider is not None and type(self.provider) is not ProviderCapabilitiesV5:
            raise ValueError("campaign provider capabilities are invalid")
        if type(self.resources) is not ResourceCapabilitiesV5:
            raise ValueError("campaign resource capabilities are invalid")
        if type(self.artifact_root) is not str or self.artifact_root != ARTIFACT_ROOT_V5:
            raise ValueError("campaign artifact root is not the closed V5 root")
        references = (
            self.execution_profile_ref,
            self.evaluator_contract_ref,
            self.baseline_authority_ref,
            self.panel_plan_ref,
            self.policy_scope_ref,
            self.baseline_policy_revision_ref,
            self.sandbox_profile_ref,
        )
        if any(type(item) is not ArtifactRefV5 for item in references):
            raise ValueError("campaign artifact references are invalid")
        if len({item.relative_path for item in references}) != len(references):
            raise ValueError("campaign artifact reference paths must be unique")
        if type(self.source_commit) is not str or _SOURCE_COMMIT_RE.fullmatch(self.source_commit) is None:
            raise ValueError("campaign source commit must be a lowercase Git SHA-1")
        if (
            type(self.apply) is not bool
            or self.apply
            or type(self.qualification_allowed) is not bool
            or self.qualification_allowed
            or type(self.full_replay_allowed) is not bool
            or self.full_replay_allowed
        ):
            raise ValueError("campaign mutation and held-out capabilities must be disabled")

    @property
    def sha256(self) -> str:
        return _sha256(self)


def initial_friction_grid_v5() -> tuple[FrictionScenario, ...]:
    """Return the explicit initial scenario-grid manifest value."""

    return (
        FrictionScenario("gross", 0, 0, 0),
        FrictionScenario("base", 2, 3, 0),
        FrictionScenario("stress", 5, 10, 1),
    )


@dataclass(frozen=True, slots=True)
class ScenarioGridV5:
    """Immutable artifact form of the evaluator's complete friction grid."""

    schema_version: Literal[5]
    scenarios: tuple[FrictionScenario, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("scenario grid schema must be V5")
        if self.scenarios != initial_friction_grid_v5():
            raise ValueError("scenario grid differs from the canonical V5 friction authority")

    @property
    def sha256(self) -> str:
        return _sha256(self)


def evaluator_source_sha256(source_sha256_by_path: Mapping[str, str]) -> str:
    """Derive the evaluator identity from the complete closed source map."""

    if not isinstance(source_sha256_by_path, Mapping):
        raise ValueError("evaluator source map must be a mapping")
    supplied = tuple(sorted(source_sha256_by_path))
    expected = tuple(sorted(EVALUATOR_SOURCE_PATHS_V5))
    if supplied != expected:
        raise ValueError("evaluator source map is incomplete or contains unrelated paths")
    canonical: dict[str, str] = {}
    for path in expected:
        if PurePosixPath(path).as_posix() != path or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ValueError("evaluator source path is not canonical")
        canonical[path] = _digest(source_sha256_by_path[path], f"evaluator source digest for {path}")
    return _sha256(canonical)


def evaluator_source_map_v5(source_root: Path) -> dict[str, str]:
    """Hash every required runtime source, failing closed on a missing file."""

    root = Path(source_root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("evaluator source root must be an absolute non-link directory")
    resolved_root = root.resolve()
    result: dict[str, str] = {}
    for relative in EVALUATOR_SOURCE_PATHS_V5:
        candidate = resolved_root.joinpath(*PurePosixPath(relative).parts)
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"required evaluator source is unavailable: {relative}")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise ValueError(f"required evaluator source is unreadable: {relative}") from exc
        result[relative] = hashlib.sha256(content).hexdigest()
    return result


@dataclass(frozen=True, slots=True)
class SandboxProfileV5:
    schema_version: Literal[5]
    image_name: str
    image_digest: str
    runtime_source_sha256: str
    network_mode: Literal["none"]
    root_filesystem: Literal["read_only"]
    source_mount_mode: Literal["read_only"]
    data_mount_mode: Literal["read_only"]
    output_mode: Literal["bounded_write_only"]
    cpu_limit: Decimal
    memory_limit_mib: int
    output_limit_bytes: int
    pid_limit: int = 32

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("sandbox profile schema must be V5")
        name = _text(self.image_name, "sandbox image name")
        if "@" in name or any(character.isspace() for character in name):
            raise ValueError("sandbox image name must not contain an embedded digest")
        if type(self.image_digest) is not str or _IMAGE_DIGEST_RE.fullmatch(self.image_digest) is None:
            raise ValueError("sandbox image digest must be a canonical sha256 digest")
        _digest(self.runtime_source_sha256, "sandbox runtime source SHA-256")
        if (
            self.network_mode != "none"
            or self.root_filesystem != "read_only"
            or self.source_mount_mode != "read_only"
            or self.data_mount_mode != "read_only"
            or self.output_mode != "bounded_write_only"
        ):
            raise ValueError("sandbox isolation profile is not the canonical V5 profile")
        if _decimal(self.cpu_limit, "sandbox CPU limit") <= 0:
            raise ValueError("sandbox CPU limit must be positive")
        _count(self.memory_limit_mib, "sandbox memory limit", positive=True)
        _count(self.output_limit_bytes, "sandbox output limit", positive=True)
        _count(self.pid_limit, "sandbox PID limit", positive=True)

    @property
    def image_reference(self) -> str:
        return f"{self.image_name}@{self.image_digest}"

    @property
    def sha256(self) -> str:
        """Hash every declared field in canonical order-independent JSON."""

        return _sha256(self)


def sandbox_profile_from_manifest_v5(
    *,
    image_name: str,
    image_digest: str,
    runtime_source_sha256: str,
    resources: SandboxResourceManifestV5,
) -> SandboxProfileV5:
    """Construct the fixed sandbox profile from authenticated manifest resources."""

    return SandboxProfileV5(
        schema_version=5,
        image_name=image_name,
        image_digest=image_digest,
        runtime_source_sha256=runtime_source_sha256,
        network_mode="none",
        root_filesystem="read_only",
        source_mount_mode="read_only",
        data_mount_mode="read_only",
        output_mode="bounded_write_only",
        cpu_limit=resources.evaluation_cpu_limit,
        memory_limit_mib=resources.evaluation_memory_mib,
        output_limit_bytes=resources.evaluation_output_limit_bytes,
        pid_limit=resources.evaluation_pid_limit,
    )


def validate_sandbox_profile_resources_v5(profile: SandboxProfileV5, resources: SandboxResourceManifestV5) -> None:
    """Require exact equality with the campaign's authenticated resource inputs."""

    if type(profile) is not SandboxProfileV5:
        raise ValueError("sandbox profile must be a SandboxProfileV5")
    try:
        expected = (
            resources.evaluation_cpu_limit,
            resources.evaluation_memory_mib,
            resources.evaluation_output_limit_bytes,
            resources.evaluation_pid_limit,
        )
    except AttributeError as exc:
        raise ValueError("campaign resource manifest is incomplete") from exc
    if (
        type(expected[0]) is not Decimal
        or type(expected[1]) is not int
        or type(expected[2]) is not int
        or type(expected[3]) is not int
    ):
        raise ValueError("campaign resource manifest values are invalid")
    actual = (
        profile.cpu_limit,
        profile.memory_limit_mib,
        profile.output_limit_bytes,
        profile.pid_limit,
    )
    if actual != expected:
        raise ValueError("sandbox resources differ from the campaign manifest")


@dataclass(frozen=True, slots=True)
class EvaluatorContractV5:
    schema_version: Literal[5]
    execution_profile_sha256: str
    sandbox_profile_sha256: str
    evaluator_source_sha256: str
    pit_bundle_sha256: str
    prices_provenance_sha256: str
    identity_transition_contract_sha256: str
    baseline_source_bundle_sha256: str
    baseline_policy_revision_sha256: str
    friction_grid: tuple[FrictionScenario, ...]
    selection_scenario_id: str
    benchmark: Literal["SPY"] = "SPY"
    signal_every_n_days: Literal[1] = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("evaluator contract schema must be V5")
        for value, label in (
            (self.execution_profile_sha256, "execution profile SHA-256"),
            (self.sandbox_profile_sha256, "sandbox profile SHA-256"),
            (self.evaluator_source_sha256, "evaluator source SHA-256"),
            (self.pit_bundle_sha256, "PIT bundle SHA-256"),
            (self.prices_provenance_sha256, "prices provenance SHA-256"),
            (
                self.identity_transition_contract_sha256,
                "identity transition contract SHA-256",
            ),
            (self.baseline_source_bundle_sha256, "baseline source bundle SHA-256"),
            (self.baseline_policy_revision_sha256, "baseline policy revision SHA-256"),
        ):
            _digest(value, label)
        if type(self.friction_grid) is not tuple or any(
            type(item) is not FrictionScenario for item in self.friction_grid
        ):
            raise ValueError("friction grid must contain FrictionScenario records")
        scenario_ids = tuple(item.scenario_id for item in self.friction_grid)
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("friction scenario IDs must be unique")
        if self.friction_grid != initial_friction_grid_v5():
            raise ValueError("friction grid differs from the initial V5 manifest")
        _text(self.selection_scenario_id, "selection scenario ID")
        if scenario_ids.count(self.selection_scenario_id) != 1:
            raise ValueError("selection scenario must resolve exactly once")
        if self.selection_scenario_id != "base":
            raise ValueError("initial V5 selection scenario must be base")
        if self.benchmark != "SPY" or type(self.benchmark) is not str:
            raise ValueError("V5 evaluator benchmark must be SPY")
        if type(self.signal_every_n_days) is not int or self.signal_every_n_days != 1:
            raise ValueError("V5 evaluator must signal every session")

    @property
    def sha256(self) -> str:
        return _sha256(self)


@dataclass(frozen=True, slots=True)
class MetricCountV5:
    metric_id: str
    count: int

    def __post_init__(self) -> None:
        _text(self.metric_id, "metric ID")
        _count(self.count, "metric count")


@dataclass(frozen=True, slots=True)
class SliceMetricsV5:
    annualized_return_pct: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    closed_trades: int
    average_exposure_pct: Decimal

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "closed_trades":
                _count(value, "slice closed trades")
            else:
                _decimal(value, f"slice {item.name}")


@dataclass(frozen=True, slots=True)
class EvaluationSliceV5:
    dimension: Literal["regime", "episode", "calendar_year"]
    label: str
    metrics: SliceMetricsV5

    def __post_init__(self) -> None:
        if self.dimension not in {"regime", "episode", "calendar_year"}:
            raise ValueError("evaluation slice dimension is invalid")
        _text(self.label, "evaluation slice label")
        if type(self.metrics) is not SliceMetricsV5:
            raise ValueError("evaluation slice metrics must use the V5 schema")


@dataclass(frozen=True, slots=True)
class RollingReturnV5:
    window_months: Literal[12, 24, 36]
    end_date: str
    total_return_pct: Decimal
    annualized_return_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.window_months) is not int or self.window_months not in {12, 24, 36}:
            raise ValueError("rolling return window is invalid")
        _date(self.end_date, "rolling return end date")
        _decimal(self.total_return_pct, "rolling total return")
        _decimal(self.annualized_return_pct, "rolling annualized return")


@dataclass(frozen=True, slots=True)
class DistributionSummaryV5:
    minimum: Decimal
    p25: Decimal
    median: Decimal
    p75: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        values = tuple(_decimal(getattr(self, item.name), f"distribution {item.name}") for item in fields(self))
        if values != tuple(sorted(values)):
            raise ValueError("distribution summary must be monotonic")


@dataclass(frozen=True, slots=True)
class EvaluationReportV5:
    portfolio_annualized_return_pct: Decimal
    portfolio_total_return_pct: Decimal
    gross_annualized_return_pct: Decimal
    benchmark_annualized_return_pct: Decimal
    benchmark_total_return_pct: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal
    closed_trades: int
    average_exposure_pct: Decimal
    average_cash_pct: Decimal
    turnover_pct: Decimal
    total_friction_usd: Decimal
    friction_drag_pct: Decimal
    win_rate_pct: Decimal | None
    average_win_pct: Decimal | None
    average_loss_pct: Decimal | None
    payoff_ratio: Decimal | None
    expectancy_pct: Decimal | None
    median_holding_sessions: Decimal | None
    invested_sleeve_annualized_return_pct: Decimal | None
    estimated_idle_cash_drag_pct: Decimal
    stop_gap_shortfall_usd: Decimal
    identity_transition_count: int
    scale_out_opportunity_cost_pct: Decimal | None
    maximum_favorable_excursion_pct: DistributionSummaryV5 | None
    maximum_adverse_excursion_pct: DistributionSummaryV5 | None
    entry_funnel: tuple[MetricCountV5, ...]
    exit_attribution: tuple[MetricCountV5, ...]
    policy_intent_outcomes: tuple[MetricCountV5, ...]
    regime_slices: tuple[EvaluationSliceV5, ...]
    episode_slices: tuple[EvaluationSliceV5, ...]
    calendar_year_slices: tuple[EvaluationSliceV5, ...]
    rolling_returns: tuple[RollingReturnV5, ...]

    def __post_init__(self) -> None:
        optional_decimals = {
            "win_rate_pct",
            "average_win_pct",
            "average_loss_pct",
            "payoff_ratio",
            "expectancy_pct",
            "median_holding_sessions",
            "invested_sleeve_annualized_return_pct",
            "scale_out_opportunity_cost_pct",
        }
        for item in fields(self):
            name = item.name
            value = getattr(self, name)
            if name in optional_decimals:
                if value is not None:
                    _decimal(value, f"report {name}")
            elif name == "closed_trades":
                _count(value, "report closed trades")
            elif name == "identity_transition_count":
                _count(value, "report identity transition count")
            elif name in {
                "maximum_favorable_excursion_pct",
                "maximum_adverse_excursion_pct",
            }:
                if value is not None and type(value) is not DistributionSummaryV5:
                    raise ValueError(f"report {name} must use the V5 schema")
            elif name in {
                "entry_funnel",
                "exit_attribution",
                "policy_intent_outcomes",
            }:
                self._validate_metric_counts(value, name)
            elif name in {"regime_slices", "episode_slices", "calendar_year_slices"}:
                self._validate_slices(value, name)
            elif name == "rolling_returns":
                self._validate_rolling_returns(value)
            else:
                _decimal(value, f"report {name}")

    @staticmethod
    def _validate_metric_counts(value: object, label: str) -> None:
        if type(value) is not tuple or any(type(item) is not MetricCountV5 for item in value):
            raise ValueError(f"report {label} must contain V5 metric counts")
        ids = tuple(item.metric_id for item in value)
        if len(set(ids)) != len(ids):
            raise ValueError(f"report {label} metric IDs must be unique")

    @staticmethod
    def _validate_slices(value: object, label: str) -> None:
        expected_dimension = {
            "regime_slices": "regime",
            "episode_slices": "episode",
            "calendar_year_slices": "calendar_year",
        }[label]
        if type(value) is not tuple or any(
            type(item) is not EvaluationSliceV5 or item.dimension != expected_dimension for item in value
        ):
            raise ValueError(f"report {label} must contain matching V5 slices")
        labels = tuple(item.label for item in value)
        if len(set(labels)) != len(labels):
            raise ValueError(f"report {label} labels must be unique")

    @staticmethod
    def _validate_rolling_returns(value: object) -> None:
        if type(value) is not tuple or any(type(item) is not RollingReturnV5 for item in value):
            raise ValueError("report rolling returns must use the V5 schema")
        keys = tuple((item.window_months, item.end_date) for item in value)
        if len(set(keys)) != len(keys):
            raise ValueError("report rolling return keys must be unique")


@dataclass(frozen=True, slots=True)
class RoleEvidenceItemV5:
    """One symbol-neutral aggregate exposed across the provider boundary."""

    evidence_id: str
    metric_id: str
    value: Decimal | int | None

    def __post_init__(self) -> None:
        if type(self.evidence_id) is not str or _EVIDENCE_ID_RE.fullmatch(self.evidence_id) is None:
            raise ValueError("role evidence ID is invalid")
        _text(self.metric_id, "role evidence metric ID")
        if self.value is not None:
            if type(self.value) is int:
                if self.value < 0:
                    raise ValueError("role evidence integer must be non-negative")
            else:
                _decimal(self.value, "role evidence value")

    def to_primitive(self) -> dict[str, str | int | None]:
        value: str | int | None = self.value
        if type(value) is Decimal:
            value = _decimal_primitive(value)
        return {
            "evidence_id": self.evidence_id,
            "metric_id": self.metric_id,
            "value": value,
        }


@dataclass(frozen=True, slots=True)
class RoleEvidenceV5:
    """Deterministically bounded provider-facing report projection."""

    schema_version: Literal[5]
    items: tuple[RoleEvidenceItemV5, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("role evidence schema must be V5")
        if (
            type(self.items) is not tuple
            or len(self.items) > MAX_ROLE_EVIDENCE_ITEMS_V5
            or any(type(item) is not RoleEvidenceItemV5 for item in self.items)
        ):
            raise ValueError("role evidence items are invalid or unbounded")
        ids = tuple(item.evidence_id for item in self.items)
        if len(set(ids)) != len(ids):
            raise ValueError("role evidence IDs must be unique")
        if len(self.canonical_json_bytes()) > MAX_ROLE_EVIDENCE_BYTES_V5:
            raise ValueError("role evidence exceeds the provider byte bound")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "items": [item.to_primitive() for item in self.items],
        }

    def canonical_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_primitive(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScenarioPanelEvaluationV5:
    scenario_id: str
    starting_equity: Decimal
    ending_equity: Decimal
    report: EvaluationReportV5

    def __post_init__(self) -> None:
        _text(self.scenario_id, "scenario evaluation ID")
        if _decimal(self.starting_equity, "scenario starting equity") <= 0:
            raise ValueError("scenario starting equity must be positive")
        if _decimal(self.ending_equity, "scenario ending equity") <= 0:
            raise ValueError("scenario ending equity must be positive")
        if type(self.report) is not EvaluationReportV5:
            raise ValueError("scenario report must use the V5 evidence schema")


@dataclass(frozen=True, slots=True)
class PanelEvaluationV5:
    evaluator_contract_sha256: str
    sandbox_profile_sha256: str
    panel_sha256: str
    policy_identity_sha256: str
    start_date: str
    end_date: str
    elapsed_calendar_days: int
    selection_scenario_id: str
    scenarios: tuple[ScenarioPanelEvaluationV5, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.evaluator_contract_sha256, "panel evaluator contract SHA-256"),
            (self.sandbox_profile_sha256, "panel sandbox profile SHA-256"),
            (self.panel_sha256, "panel SHA-256"),
            (self.policy_identity_sha256, "panel policy identity SHA-256"),
        ):
            _digest(value, label)
        start = _date(self.start_date, "panel start date")
        end = _date(self.end_date, "panel end date")
        if start >= end:
            raise ValueError("panel date range is invalid")
        elapsed = _count(self.elapsed_calendar_days, "panel elapsed calendar days", positive=True)
        if elapsed != (end - start).days:
            raise ValueError("panel elapsed calendar days differ from panel bounds")
        _text(self.selection_scenario_id, "panel selection scenario ID")
        if (
            type(self.scenarios) is not tuple
            or not self.scenarios
            or any(type(item) is not ScenarioPanelEvaluationV5 for item in self.scenarios)
        ):
            raise ValueError("panel scenarios must be non-empty V5 evidence")
        ids = tuple(item.scenario_id for item in self.scenarios)
        if len(set(ids)) != len(ids):
            raise ValueError("panel scenario IDs must be unique")
        if ids.count(self.selection_scenario_id) != 1:
            raise ValueError("panel selection scenario must resolve exactly once")

    @property
    def sha256(self) -> str:
        return _sha256(self)


def selected_scenario(evaluation: PanelEvaluationV5) -> ScenarioPanelEvaluationV5:
    """Resolve the selected scenario without depending on tuple position."""

    if type(evaluation) is not PanelEvaluationV5:
        raise ValueError("evaluation must be PanelEvaluationV5 evidence")
    matches = tuple(item for item in evaluation.scenarios if item.scenario_id == evaluation.selection_scenario_id)
    if len(matches) != 1:
        raise ValueError("panel selection scenario must resolve exactly once")
    return matches[0]


def validate_episode_plan_panel_v5(episode: EpisodePlanV5, panel: EvaluationPanelSpec) -> None:
    """Bind duplicated episode fields to an already authenticated panel."""

    if type(episode) is not EpisodePlanV5:
        raise ValueError("episode plan must use the V5 schema")
    if type(panel) is not EvaluationPanelSpec:
        raise ValueError("authenticated panel must be a complete EvaluationPanelSpec")
    lineage_ids = tuple(item.security_lineage_id for item in panel.lineages)
    if (
        panel.sha256 != episode.panel_ref.sha256
        or panel.start_date != episode.start_date
        or panel.end_date != episode.end_date
        or lineage_ids != episode.lineage_ids
    ):
        raise ValueError("episode fields differ from the authenticated panel")


@dataclass(frozen=True, slots=True)
class MetricPredictionV5:
    metric_id: str
    direction: Literal["increase", "decrease", "unchanged"]
    rationale: str

    def __post_init__(self) -> None:
        _text(self.metric_id, "predicted metric ID")
        if self.direction not in {"increase", "decrease", "unchanged"}:
            raise ValueError("metric prediction direction is invalid")
        _text(self.rationale, "metric prediction rationale")


@dataclass(frozen=True, slots=True)
class ValidationResultV5:
    valid: bool
    failure_code: str | None
    changed_symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise ValueError("validation result validity must be boolean")
        if self.valid:
            if self.failure_code is not None:
                raise ValueError("valid result cannot carry a failure code")
        else:
            if self.failure_code is None:
                raise ValueError("invalid result requires a failure code")
            _text(self.failure_code, "validation failure code")
        if type(self.changed_symbols) is not tuple or any(type(item) is not str for item in self.changed_symbols):
            raise ValueError("changed symbols are invalid")
        tuple(_text(item, "changed symbol") for item in self.changed_symbols)
        if len(set(self.changed_symbols)) != len(self.changed_symbols):
            raise ValueError("changed symbols must be unique")


@dataclass(frozen=True, slots=True)
class EpisodeEvaluationV5:
    episode_id: str
    episode_ordinal: int
    start_date: str
    end_date: str
    evaluation: PanelEvaluationV5

    def __post_init__(self) -> None:
        _text(self.episode_id, "evaluated episode ID")
        _count(self.episode_ordinal, "evaluated episode ordinal", positive=True)
        if type(self.evaluation) is not PanelEvaluationV5:
            raise ValueError("episode evaluation must wrap V5 panel evidence")
        if self.start_date != self.evaluation.start_date or self.end_date != self.evaluation.end_date:
            raise ValueError("episode dates differ from the authenticated panel evaluation")


@dataclass(frozen=True, slots=True)
class CampaignEvidenceV5:
    discovery_plan_sha256: str
    episodes: tuple[EpisodeEvaluationV5, ...]
    campaign_cagr_pct: Decimal
    closed_trades: int

    def __post_init__(self) -> None:
        _digest(self.discovery_plan_sha256, "discovery plan SHA-256")
        if (
            type(self.episodes) is not tuple
            or len(self.episodes) != 4
            or any(type(item) is not EpisodeEvaluationV5 for item in self.episodes)
            or tuple(item.episode_ordinal for item in self.episodes) != (1, 2, 3, 4)
        ):
            raise ValueError("campaign evidence requires discovery ordinals 1 through 4")
        if len({item.episode_id for item in self.episodes}) != 4:
            raise ValueError("campaign evidence episode IDs must be unique")
        if len({item.evaluation.panel_sha256 for item in self.episodes}) != 4:
            raise ValueError("campaign evidence panel identities must be unique")
        _validate_ordered_disjoint_date_intervals_v5(
            tuple((item.start_date, item.end_date) for item in self.episodes),
            "campaign evidence episodes",
        )
        if len({item.evaluation.evaluator_contract_sha256 for item in self.episodes}) != 1:
            raise ValueError("campaign evidence must share one evaluator identity")
        if len({item.evaluation.policy_identity_sha256 for item in self.episodes}) != 1:
            raise ValueError("campaign evidence must share one policy identity")
        if len({item.evaluation.selection_scenario_id for item in self.episodes}) != 1:
            raise ValueError("campaign evidence must share one selected scenario")
        expected_closed_trades = sum(selected_scenario(item.evaluation).report.closed_trades for item in self.episodes)
        _count(self.closed_trades, "campaign closed trades")
        if self.closed_trades != expected_closed_trades:
            raise ValueError("campaign closed trades differ from selected-scenario evidence")
        _decimal(self.campaign_cagr_pct, "campaign CAGR")

    @property
    def policy_identity_sha256(self) -> str:
        return self.episodes[0].evaluation.policy_identity_sha256

    @property
    def evaluator_contract_sha256(self) -> str:
        return self.episodes[0].evaluation.evaluator_contract_sha256


def validate_campaign_evidence_v5(
    evidence: CampaignEvidenceV5,
    *,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
    policy_identity_sha256: str,
) -> None:
    """Bind scored evidence to its authenticated plan, evaluator, policy and scenario."""

    if type(evidence) is not CampaignEvidenceV5:
        raise ValueError("campaign evidence must use the V5 schema")
    if type(panel_plan) is not CampaignPanelPlanV5:
        raise ValueError("campaign panel plan must use the V5 schema")
    if type(evaluator_contract) is not EvaluatorContractV5:
        raise ValueError("evaluator contract must use the V5 schema")
    _digest(policy_identity_sha256, "expected policy identity SHA-256")
    if evidence.discovery_plan_sha256 != panel_plan.discovery_plan_sha256:
        raise ValueError("campaign evidence differs from the discovery plan identity")
    if evidence.evaluator_contract_sha256 != evaluator_contract.sha256:
        raise ValueError("campaign evidence differs from the evaluator identity")
    if evidence.policy_identity_sha256 != policy_identity_sha256:
        raise ValueError("campaign evidence differs from the policy identity")
    for actual, planned in zip(evidence.episodes, panel_plan.discovery, strict=True):
        if (
            actual.episode_id != planned.episode_id
            or actual.episode_ordinal != planned.episode_ordinal
            or actual.start_date != planned.start_date
            or actual.end_date != planned.end_date
            or actual.evaluation.panel_sha256 != planned.panel_ref.sha256
            or actual.evaluation.selection_scenario_id != evaluator_contract.selection_scenario_id
            or actual.evaluation.sandbox_profile_sha256 != evaluator_contract.sandbox_profile_sha256
        ):
            raise ValueError("campaign episode differs from its authenticated plan")


def validate_campaign_manifest_bindings_v5(
    manifest: CampaignManifestV5,
    *,
    panel_plan: CampaignPanelPlanV5,
    evaluator_contract: EvaluatorContractV5,
) -> None:
    """Validate identities duplicated across authenticated campaign artifacts."""

    if type(manifest) is not CampaignManifestV5:
        raise ValueError("campaign manifest must use the V5 schema")
    if type(panel_plan) is not CampaignPanelPlanV5:
        raise ValueError("campaign panel plan must use the V5 schema")
    if type(evaluator_contract) is not EvaluatorContractV5:
        raise ValueError("evaluator contract must use the V5 schema")
    if (
        manifest.target.sha256 != panel_plan.target_sha256
        or manifest.panel_plan_ref.sha256 != panel_plan.sha256
        or manifest.evaluator_contract_ref.sha256 != evaluator_contract.sha256
        or panel_plan.pit_bundle_ref.sha256 != evaluator_contract.pit_bundle_sha256
        or panel_plan.prices_provenance_ref.sha256 != evaluator_contract.prices_provenance_sha256
        or manifest.execution_profile_ref.sha256 != evaluator_contract.execution_profile_sha256
        or manifest.sandbox_profile_ref.sha256 != evaluator_contract.sandbox_profile_sha256
    ):
        raise ValueError("campaign manifest identities are inconsistent")


@dataclass(frozen=True, slots=True)
class HypothesisV5:
    hypothesis_id: str
    rank: int
    primary_mechanism: Literal["entry", "risk_sizing", "position_management", "exit", "cross_policy"]
    causal_claim: str
    predicted_changes: tuple[MetricPredictionV5, ...]
    evidence_ids: tuple[str, ...]
    author_instructions: str
    authoring_mode: Literal["symbol_edits", "full_source_escape"] = "symbol_edits"

    def __post_init__(self) -> None:
        _text(self.hypothesis_id, "hypothesis ID")
        _count(self.rank, "hypothesis rank", positive=True)
        if self.primary_mechanism not in {
            "entry",
            "risk_sizing",
            "position_management",
            "exit",
            "cross_policy",
        }:
            raise ValueError("hypothesis primary mechanism is invalid")
        _text(self.causal_claim, "hypothesis causal claim")
        if (
            type(self.predicted_changes) is not tuple
            or not self.predicted_changes
            or any(type(item) is not MetricPredictionV5 for item in self.predicted_changes)
        ):
            raise ValueError("hypothesis metric predictions are invalid")
        metric_ids = tuple(item.metric_id for item in self.predicted_changes)
        if len(set(metric_ids)) != len(metric_ids):
            raise ValueError("hypothesis predicted metric IDs must be unique")
        _evidence_ids(self.evidence_ids, "hypothesis evidence IDs")
        _text(self.author_instructions, "hypothesis author instructions")
        if self.authoring_mode not in {"symbol_edits", "full_source_escape"}:
            raise ValueError("hypothesis authoring mode is invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class InvestigatorArtifactV5:
    hypotheses: tuple[HypothesisV5, ...]

    def __post_init__(self) -> None:
        if type(self.hypotheses) is not tuple or any(type(item) is not HypothesisV5 for item in self.hypotheses):
            raise ValueError("investigator hypotheses are invalid")
        ids = tuple(item.hypothesis_id for item in self.hypotheses)
        if len(set(ids)) != len(ids):
            raise ValueError("investigator hypothesis IDs must be unique")

    def validate_for(self, *, manifest: CampaignManifestV5, issued_evidence: RoleEvidenceV5) -> None:
        if type(manifest) is not CampaignManifestV5:
            raise ValueError("investigator manifest must use the V5 schema")
        expected_count = manifest.search.hypotheses_per_investigator
        if len(self.hypotheses) != expected_count or tuple(item.rank for item in self.hypotheses) != tuple(
            range(1, expected_count + 1)
        ):
            raise ValueError("investigator hypothesis count or ranks differ from the manifest")
        _validate_role_evidence_bindings_v5(
            tuple(evidence_id for hypothesis in self.hypotheses for evidence_id in hypothesis.evidence_ids),
            issued_evidence,
        )


def investigator_hypothesis_schema_bounds_v5(
    manifest: CampaignManifestV5,
) -> dict[str, int]:
    """Return the one manifest-bound array cardinality for strict provider schemas."""

    if type(manifest) is not CampaignManifestV5:
        raise ValueError("investigator manifest must use the V5 schema")
    count = manifest.search.hypotheses_per_investigator
    return {"minItems": count, "maxItems": count}


@dataclass(frozen=True, slots=True)
class CriticReviewV5:
    experiment_id: str
    prediction_vs_observation: str
    causal_explanation: str
    evidence_ids: tuple[str, ...]
    disposition: Literal["promote", "refine", "abandon"]
    next_direction: str

    def __post_init__(self) -> None:
        _text(self.experiment_id, "critic experiment ID")
        _text(self.prediction_vs_observation, "critic prediction comparison")
        _text(self.causal_explanation, "critic causal explanation")
        _evidence_ids(self.evidence_ids, "critic review evidence IDs")
        if self.disposition not in {"promote", "refine", "abandon"}:
            raise ValueError("critic disposition is invalid")
        _text(self.next_direction, "critic next direction")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class CriticArtifactV5:
    reviews: tuple[CriticReviewV5, ...]
    comparative_assessment: str
    next_campaign_direction: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.reviews) is not tuple or any(type(item) is not CriticReviewV5 for item in self.reviews):
            raise ValueError("critic reviews are invalid")
        ids = tuple(item.experiment_id for item in self.reviews)
        if len(set(ids)) != len(ids):
            raise ValueError("critic experiment reviews must be unique")
        _text(self.comparative_assessment, "critic comparative assessment")
        _text(self.next_campaign_direction, "critic next campaign direction")
        _evidence_ids(self.evidence_ids, "critic artifact evidence IDs")

    def validate_for(
        self,
        *,
        testable_experiment_ids: tuple[str, ...],
        issued_evidence: RoleEvidenceV5,
    ) -> None:
        if type(testable_experiment_ids) is not tuple or any(type(item) is not str for item in testable_experiment_ids):
            raise ValueError("testable experiment IDs are invalid")
        tuple(_text(item, "testable experiment ID") for item in testable_experiment_ids)
        if len(set(testable_experiment_ids)) != len(testable_experiment_ids):
            raise ValueError("testable experiment IDs must be unique")
        if tuple(item.experiment_id for item in self.reviews) != testable_experiment_ids:
            raise ValueError("critic reviews differ from the testable experiment batch")
        _validate_role_evidence_bindings_v5(
            (
                *self.evidence_ids,
                *(evidence_id for review in self.reviews for evidence_id in review.evidence_ids),
            ),
            issued_evidence,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


def _validate_role_evidence_bindings_v5(referenced_ids: tuple[str, ...], issued_evidence: RoleEvidenceV5) -> None:
    if type(issued_evidence) is not RoleEvidenceV5:
        raise ValueError("issued role evidence must use the V5 schema")
    issued = {item.evidence_id for item in issued_evidence.items}
    unresolved = set(referenced_ids).difference(issued)
    if unresolved:
        raise ValueError("role artifact references evidence that was not issued")


__all__ = [
    "ARTIFACT_ROOT_V5",
    "AnnualizedReturnTargetV5",
    "ArtifactGraphFailureCodeV5",
    "ArtifactGraphFailureV5",
    "ArtifactGraphVerificationV5",
    "ArtifactGraphVerifierV5",
    "ArtifactIndexV5",
    "ArtifactRefV5",
    "AuthenticatedArtifactV5",
    "AuthenticatedRawArtifactV5",
    "BaselineAuthorityV5",
    "CampaignEvidenceV5",
    "CampaignManifestV5",
    "CampaignPanelPlanV5",
    "ConfirmationAttemptCommitmentV5",
    "ConfirmationOutcomeV5",
    "ConfirmationPanelPlanV5",
    "CriticArtifactV5",
    "CriticReviewV5",
    "DistributionSummaryV5",
    "EVALUATOR_SOURCE_PATHS_V5",
    "EpisodeEvaluationV5",
    "EpisodePlanV5",
    "EvaluationReportV5",
    "EvaluationSliceV5",
    "EvaluatorContractV5",
    "FinalizedDiscoveryCampaignV5",
    "FinalizedDiscoveryRoundV5",
    "HypothesisV5",
    "InvestigatorArtifactV5",
    "MetricCountV5",
    "MetricPredictionV5",
    "ModelPriceUpperBoundV5",
    "MAX_ROLE_EVIDENCE_BYTES_V5",
    "MAX_ROLE_EVIDENCE_ITEMS_V5",
    "PanelEvaluationV5",
    "ProviderCapabilitiesV5",
    "QualificationAttemptCommitmentV5",
    "QualificationOutcomeV5",
    "QualificationPanelPlanV5",
    "ResourceCapabilitiesV5",
    "RetirementLedgerLocatorV5",
    "RollingReturnV5",
    "RoleEvidenceItemV5",
    "RoleEvidenceV5",
    "SandboxProfileV5",
    "SandboxResourceManifestV5",
    "ScenarioPanelEvaluationV5",
    "ScenarioGridV5",
    "SearchCapabilitiesV5",
    "SliceMetricsV5",
    "StageOutcomeStatusV5",
    "ValidationResultV5",
    "canonical_json_bytes_v5",
    "canonical_primitive_v5",
    "canonical_sha256_v5",
    "evaluator_source_map_v5",
    "evaluator_source_sha256",
    "initial_friction_grid_v5",
    "investigator_hypothesis_schema_bounds_v5",
    "sandbox_profile_from_manifest_v5",
    "selected_scenario",
    "validate_campaign_evidence_v5",
    "validate_campaign_manifest_bindings_v5",
    "validate_episode_plan_panel_v5",
    "validate_sandbox_profile_resources_v5",
]
