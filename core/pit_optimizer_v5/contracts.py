"""Immutable evaluator and evidence contracts for PIT optimizer V5."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from core.backtest_fills import FrictionScenario


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"v5\.[a-z0-9_.-]{1,120}")
MAX_ROLE_EVIDENCE_ITEMS_V5 = 96
MAX_ROLE_EVIDENCE_BYTES_V5 = 16 * 1024

# This is a closed semantic-runtime set, not a repository hash.  In particular,
# documentation, orchestration, provider, and CLI files do not affect evaluator
# identity.  diagnostics.py is deliberately required before the source identity
# can be issued, even though it is introduced by the next implementation task.
EVALUATOR_SOURCE_PATHS_V5 = (
    "backtest.py",
    "core/backtest_engine.py",
    "core/backtest_fills.py",
    "core/momentum_analysis.py",
    "core/pit_data.py",
    "core/pit_diagnosis/fact_cache.py",
    "core/pit_diagnosis/patterns.py",
    "core/pit_optimizer_evaluation.py",
    "core/pit_optimizer_v5/contracts.py",
    "core/pit_optimizer_v5/diagnostics.py",
    "core/pit_optimizer_v5/evaluator.py",
    "core/strategy_policy/__init__.py",
    "core/strategy_policy/contracts.py",
    "core/strategy_policy/market_context.py",
    "core/strategy_policy/runtime.py",
    "core/strategy_policy/worker.py",
    "core/trading_sessions.py",
)


class SandboxResourceManifestV5(Protocol):
    """Resource fields consumed from the authenticated campaign manifest."""

    evaluation_cpu_limit: Decimal
    evaluation_memory_mib: int
    evaluation_output_limit_bytes: int


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
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


def _date(value: object, label: str) -> date:
    _text(value, label)
    try:
        parsed = date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be an ISO calendar date")
    return parsed


def _decimal_primitive(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    normalized = value.normalize()
    rendered = format(normalized, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _primitive(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_primitive(value)
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
        return {
            str(key): _primitive(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
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


def initial_friction_grid_v5() -> tuple[FrictionScenario, ...]:
    """Return the explicit initial scenario-grid manifest value."""

    return (
        FrictionScenario("gross", 0, 0, 0),
        FrictionScenario("base", 2, 3, 0),
        FrictionScenario("stress", 5, 10, 1),
    )


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
        if (
            PurePosixPath(path).as_posix() != path
            or path.startswith("/")
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError("evaluator source path is not canonical")
        canonical[path] = _digest(
            source_sha256_by_path[path], f"evaluator source digest for {path}"
        )
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

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("sandbox profile schema must be V5")
        name = _text(self.image_name, "sandbox image name")
        if "@" in name or any(character.isspace() for character in name):
            raise ValueError("sandbox image name must not contain an embedded digest")
        if type(self.image_digest) is not str or _IMAGE_DIGEST_RE.fullmatch(
            self.image_digest
        ) is None:
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
    )


def validate_sandbox_profile_resources_v5(
    profile: SandboxProfileV5, resources: SandboxResourceManifestV5
) -> None:
    """Require exact equality with the campaign's authenticated resource inputs."""

    if type(profile) is not SandboxProfileV5:
        raise ValueError("sandbox profile must be a SandboxProfileV5")
    try:
        expected = (
            resources.evaluation_cpu_limit,
            resources.evaluation_memory_mib,
            resources.evaluation_output_limit_bytes,
        )
    except AttributeError as exc:
        raise ValueError("campaign resource manifest is incomplete") from exc
    if (
        type(expected[0]) is not Decimal
        or type(expected[1]) is not int
        or type(expected[2]) is not int
    ):
        raise ValueError("campaign resource manifest values are invalid")
    actual = (profile.cpu_limit, profile.memory_limit_mib, profile.output_limit_bytes)
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
        values = tuple(
            _decimal(getattr(self, item.name), f"distribution {item.name}")
            for item in fields(self)
        )
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
            type(item) is not EvaluationSliceV5 or item.dimension != expected_dimension
            for item in value
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
        if (
            type(self.evidence_id) is not str
            or _EVIDENCE_ID_RE.fullmatch(self.evidence_id) is None
        ):
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
        elapsed = _count(
            self.elapsed_calendar_days, "panel elapsed calendar days", positive=True
        )
        if elapsed != (end - start).days:
            raise ValueError("panel elapsed calendar days differ from panel bounds")
        _text(self.selection_scenario_id, "panel selection scenario ID")
        if type(self.scenarios) is not tuple or not self.scenarios or any(
            type(item) is not ScenarioPanelEvaluationV5 for item in self.scenarios
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
    matches = tuple(
        item
        for item in evaluation.scenarios
        if item.scenario_id == evaluation.selection_scenario_id
    )
    if len(matches) != 1:
        raise ValueError("panel selection scenario must resolve exactly once")
    return matches[0]


__all__ = [
    "DistributionSummaryV5",
    "EVALUATOR_SOURCE_PATHS_V5",
    "EvaluationReportV5",
    "EvaluationSliceV5",
    "EvaluatorContractV5",
    "MetricCountV5",
    "MAX_ROLE_EVIDENCE_BYTES_V5",
    "MAX_ROLE_EVIDENCE_ITEMS_V5",
    "PanelEvaluationV5",
    "RollingReturnV5",
    "RoleEvidenceItemV5",
    "RoleEvidenceV5",
    "SandboxProfileV5",
    "SandboxResourceManifestV5",
    "ScenarioPanelEvaluationV5",
    "SliceMetricsV5",
    "evaluator_source_map_v5",
    "evaluator_source_sha256",
    "initial_friction_grid_v5",
    "sandbox_profile_from_manifest_v5",
    "selected_scenario",
    "validate_sandbox_profile_resources_v5",
]
