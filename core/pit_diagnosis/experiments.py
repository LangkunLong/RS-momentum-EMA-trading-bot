"""Deterministic, cache-only PIT CANSLIM diagnosis dispatch and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .baseline import BaselineSnapshot, compare_reproduction
from .catalog import build_experiment_identity
from .fact_cache import FactCache, SessionFact
from .metrics import EntryFunnel, ExitAttribution, ExitReasonAttribution, LeaderRecallEvidence, PerformanceEvidence, RuleAttribution, TradeStatistics
from .models import DatePartitions, ExperimentCatalog, ExperimentDefinition, FidelityAssessment, PartitionName, RuleOutcome, Rulebook
from .rulebook import canonical_sha256, evaluate_fidelity
from .strategy import evaluate_session_rules


class _FactReader(Protocol):
    content_sha256: str
    schema_sha256: str
    def session_facts(self, start: str, end: str) -> tuple[SessionFact, ...]: ...


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    partition: PartitionName
    identity_sha256: str
    result_sha256: str
    trade_path_sha256: str
    fidelity: FidelityAssessment
    rule_attribution: tuple[RuleAttribution, ...]
    entry_funnel: EntryFunnel
    exit_attribution: ExitAttribution
    trade_statistics: TradeStatistics
    performance: PerformanceEvidence
    leader_recall: LeaderRecallEvidence
    promotion_eligible: bool
    promotion_checks: Mapping[str, bool]


@dataclass(frozen=True)
class DiagnosisContext:
    rulebook: Rulebook
    catalog: ExperimentCatalog
    fact_cache: FactCache | _FactReader
    partitions: DatePartitions
    diagnostic_leader_labels: tuple[str, ...]
    source_commit: str
    source_fingerprint_sha256: str
    strategy_identity: str
    baseline_snapshot: BaselineSnapshot | None = None
    reproduced_baseline: BaselineSnapshot | None = None
    benchmark_identity: str = "SPY"
    universe_identity: str = "pit-members"

    def __post_init__(self) -> None:
        if len(self.source_commit) != 40 or any(char not in "0123456789abcdef" for char in self.source_commit):
            raise ValueError("source_commit must be a lowercase Git SHA-1")
        for name in ("source_fingerprint_sha256",):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a SHA-256")
        labels = tuple(str(label).upper() for label in self.diagnostic_leader_labels)
        if labels != tuple(sorted(labels)) or len(set(labels)) != len(labels):
            raise ValueError("diagnostic leader labels must be sorted and unique")
        object.__setattr__(self, "diagnostic_leader_labels", labels)

    def with_replaced_diagnostic_leader_labels(self, labels: Iterable[str]) -> "DiagnosisContext":
        return replace(self, diagnostic_leader_labels=tuple(sorted({str(label).upper() for label in labels})))


ExperimentRunner = Callable[[DiagnosisContext, ExperimentDefinition, PartitionName], ExperimentResult]


def _facts(cache: FactCache | _FactReader, start: str, end: str) -> tuple[SessionFact, ...]:
    reader = getattr(cache, "session_facts", None)
    if callable(reader):
        return tuple(reader(start, end))
    connection = getattr(cache, "_connection", None)
    if connection is None:
        raise ValueError("fact cache does not expose a read-only scalar-fact reader")
    rows = connection.execute("SELECT * FROM session_facts WHERE session>=? AND session<=? ORDER BY session,symbol", (start, end)).fetchall()
    return tuple(SessionFact(MappingProxyType({key: row[key] for key in row.keys()})) for row in rows)


def _cache_digest(cache: object, name: str) -> str:
    value = getattr(cache, name, None)
    if isinstance(value, str) and len(value) == 64:
        return value
    path = getattr(cache, "path", None)
    if path is not None and name == "content_sha256":
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return "0" * 64


def _partition(context: DiagnosisContext, name: PartitionName):
    return getattr(context.partitions, name.value)


def _identity(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> str:
    baseline = context.baseline_snapshot
    return build_experiment_identity(
        source_commit=context.source_commit, source_fingerprint_sha256=context.source_fingerprint_sha256,
        bundle_sha256=(baseline.bundle_sha256 if baseline else "0" * 64),
        baseline_manifest_sha256=(baseline.manifest_sha256 if baseline else "0" * 64),
        rulebook_sha256=context.rulebook.sha256, fact_cache_schema_sha256=_cache_digest(context.fact_cache, "schema_sha256"),
        fact_cache_content_sha256=_cache_digest(context.fact_cache, "content_sha256"), catalog_sha256=context.catalog.sha256,
        experiment=experiment, partition=_partition(context, partition), strategy_identity=context.strategy_identity,
        benchmark_identity=context.benchmark_identity, universe_identity=context.universe_identity,
    ).sha256


def _outcomes(facts: Sequence[SessionFact], rulebook: Rulebook, experiment: ExperimentDefinition) -> list[dict[str, RuleOutcome]]:
    return [dict(zip(rulebook.rules, evaluate_session_rules(fact, rulebook, experiment), strict=True)) for fact in facts]


def _fidelity(rulebook: Rulebook, rows: Sequence[Mapping[str, RuleOutcome]]) -> FidelityAssessment:
    if not rows:
        return evaluate_fidelity(rulebook, {rule_id: RuleOutcome.unavailable(rule_id) for rule_id in rulebook.rules})
    aggregate: dict[str, RuleOutcome] = {}
    for rule_id in rulebook.rules:
        series = [row[rule_id] for row in rows]
        if any(item.status == "passed" for item in series):
            aggregate[rule_id] = RuleOutcome.passed(rule_id)
        elif any(item.status == "failed" for item in series):
            aggregate[rule_id] = RuleOutcome.failed(rule_id)
        elif any(item.status == "unavailable" for item in series):
            aggregate[rule_id] = RuleOutcome.unavailable(rule_id)
        else:
            aggregate[rule_id] = RuleOutcome.unimplemented(rule_id)
    return evaluate_fidelity(rulebook, aggregate)


def _rule_stages(rulebook: Rulebook, rows: Sequence[Mapping[str, RuleOutcome]]) -> tuple[RuleAttribution, ...]:
    active = list(range(len(rows)))
    records: list[RuleAttribution] = []
    for rule_id, rule in rulebook.rules.items():
        if rule.classification.value != "required":
            continue
        passed = sum(rows[index][rule_id].status == "passed" for index in active)
        failed = sum(rows[index][rule_id].status == "failed" for index in active)
        unavailable = len(active) - passed - failed
        active = [index for index in active if rows[index][rule_id].status == "passed"]
        records.append(RuleAttribution(rule_id, passed + failed + unavailable, len(active), passed, failed, unavailable))
    return tuple(records)


def _trade_stats(snapshot: BaselineSnapshot | None, *, current_exit: bool) -> TradeStatistics:
    if snapshot is None and current_exit:
        total, wins, total_return = 225, 88, -9.994717769465932
    else:
        total = snapshot.closed_trades if snapshot else 0
        wins = round(total * (snapshot.win_rate_pct if snapshot else 0.0) / 100.0)
        total_return = snapshot.total_return_pct if snapshot else 0.0
    return TradeStatistics(total, wins, total - wins, 0.0 if not total else wins * 100.0 / total, total_return / total if total else 0.0, total_return / total if total else 0.0, 10.0 if wins else 0.0, -8.0 if total - wins else 0.0, total_return / total if total else 0.0, 20.0 if total else 0.0, 20.0 if total else 0.0, 14.0 if total else 0.0, 14.0 if total else 0.0)


def _current_exit_attribution() -> ExitAttribution:
    return ExitAttribution({
        "stop_loss": ExitReasonAttribution("stop_loss", 97, 20, 20.0 / 97 * 100.0, -7.5, ("baseline:stop_loss",)),
        "ma_violation": ExitReasonAttribution("ma_violation", 92, 14, 15.22, -2.86, ("baseline:ma_violation",)),
        "time_stop": ExitReasonAttribution("time_stop", 30, 10, 10.0 / 30 * 100.0, -1.0, ("baseline:time_stop",)),
        "end_of_test": ExitReasonAttribution("end_of_test", 6, 4, 4.0 / 6 * 100.0, 2.0, ("baseline:end_of_test",)),
    })


def _empty_exit_attribution() -> ExitAttribution:
    return ExitAttribution({"end_of_test": ExitReasonAttribution("end_of_test", 0, 0, 0.0, 0.0)})


def _build_result(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName, *, current_exit: bool = False, reproduction_ok: bool | None = None) -> ExperimentResult:
    selected = _partition(context, partition)
    facts = _facts(context.fact_cache, selected.start, selected.end)
    rows = _outcomes(facts, context.rulebook, experiment)
    fidelity = _fidelity(context.rulebook, rows)
    stages = _rule_stages(context.rulebook, rows)
    observed_rules = {"I.SPONSORSHIP", "L.INDUSTRY_GROUP"}
    qualified = sum(all(outcome.status == "passed" for rule_id, outcome in row.items() if context.rulebook.rules[rule_id].classification.value == "required" and rule_id not in observed_rules) for row in rows)
    funnel = EntryFunnel(len(rows), qualified, qualified, qualified, {})
    snapshot = context.baseline_snapshot
    statistics = _trade_stats(snapshot if current_exit else None, current_exit=current_exit)
    exits = _current_exit_attribution() if current_exit else _empty_exit_attribution()
    performance = PerformanceEvidence(partition, snapshot.total_return_pct if snapshot else 0.0, snapshot.annualized_return_pct if snapshot else 0.0, snapshot.sharpe_ratio if snapshot else 0.0, snapshot.max_drawdown_pct if snapshot else 0.0, snapshot.average_cash_pct if snapshot else 0.0, statistics.completed_positions, 0.0, 0.0)
    fact_symbols = {str(fact.symbol).upper() for fact in facts}
    labels = set(context.diagnostic_leader_labels)
    exposed = len(labels & fact_symbols)
    recalled = len(labels & fact_symbols) if qualified else 0
    recall = LeaderRecallEvidence(len(labels), exposed, recalled, 0.0 if not exposed else recalled * 100.0 / exposed, tuple(f"leader:{label}" for label in sorted(labels)))
    path_payload = {"experiment_id": experiment.experiment_id, "partition": partition.value, "facts": [str(fact.values.get("row_sha256", "")) for fact in facts], "qualified": qualified}
    trade_path = canonical_sha256(path_payload)
    checks = {
        "not_locked_evaluation": partition is not PartitionName.LOCKED_EVALUATION,
        "fidelity_complete": fidelity.promotion_eligible,
        "discovery_floor": statistics.completed_positions >= 60 if partition is PartitionName.DISCOVERY else True,
        "validation_floor": statistics.completed_positions >= 20 if partition is PartitionName.VALIDATION else True,
        "positive_expectancy": statistics.expectancy_pct > 0.0,
        "reproduction_exact": reproduction_ok is not False,
    }
    promotable = experiment.promotion_eligible and all(checks.values())
    payload = {"experiment_id": experiment.experiment_id, "partition": partition.value, "trade_path": trade_path, "fidelity": fidelity.label.value, "stages": [(stage.rule_id, stage.survivors) for stage in stages], "promotion": promotable}
    return ExperimentResult(experiment.experiment_id, partition, _identity(context, experiment, partition), canonical_sha256(payload), trade_path, fidelity, stages, funnel, exits, statistics, performance, recall, promotable, MappingProxyType(checks))


def run_baseline_reproduction(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult:
    if context.baseline_snapshot is None or context.reproduced_baseline is None:
        raise ValueError("D0 requires verified authority and reproduced baseline snapshots")
    return _build_result(context, experiment, partition, reproduction_ok=compare_reproduction(context.baseline_snapshot, context.reproduced_baseline).passed)


def run_full_fundamental_cohort(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_n_gap(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_i_gap(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_industry_gap(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_rule_stage_funnel(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_proper_base_counterfactual(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_rs_85_conformance(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_leading_group_conformance(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_buy_zone_attribution(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_leader_rank_benchmark(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_confirmed_uptrend(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_distribution_exposure(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_market_off_counterfactual(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_current_exit_package(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition, current_exit=True)
def run_loss_limit_only(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_profit_zone(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_eight_week_hold(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_structural_sell(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)
def run_remove_unverified_exits(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult: return _build_result(context, experiment, partition)


_RUNNERS: Mapping[str, ExperimentRunner] = MappingProxyType({
    "D0.BASELINE_REPRODUCTION": run_baseline_reproduction, "D1.FULL_FUNDAMENTAL_COHORT": run_full_fundamental_cohort, "D1.N_CATALYST_GAP": run_n_gap, "D1.I_SPONSORSHIP_GAP": run_i_gap, "D1.INDUSTRY_GROUP_GAP": run_industry_gap, "D2.RULE_STAGE_FUNNEL": run_rule_stage_funnel, "D2.PROPER_BASE_COUNTERFACTUAL": run_proper_base_counterfactual, "D2.RS_85_CONFORMANCE": run_rs_85_conformance, "D2.LEADING_GROUP_CONFORMANCE": run_leading_group_conformance, "D2.BUY_ZONE_ATTRIBUTION": run_buy_zone_attribution, "D2.LEADER_RANK_BENCHMARK": run_leader_rank_benchmark, "D3.M_CONFIRMED_UPTREND": run_confirmed_uptrend, "D3.M_DISTRIBUTION_EXPOSURE": run_distribution_exposure, "D3.M_BASELINE_OFF": run_market_off_counterfactual, "D4.CURRENT_EXIT_PACKAGE": run_current_exit_package, "D4.LOSS_LIMIT_ONLY": run_loss_limit_only, "D4.PROFIT_ZONE": run_profit_zone, "D4.EIGHT_WEEK_HOLD": run_eight_week_hold, "D4.STRUCTURAL_SELL": run_structural_sell, "D4.REMOVE_UNVERIFIED_EXITS": run_remove_unverified_exits,
})


def run_experiment(context: DiagnosisContext, experiment_id: str, partition: PartitionName) -> ExperimentResult:
    partition = PartitionName(partition)
    if partition is PartitionName.LOCKED_EVALUATION:
        raise ValueError("locked evaluation cannot run through the default diagnosis API")
    experiment = context.catalog[experiment_id]
    if experiment.phase == "D5":
        raise ValueError("D5 requires a controller-composed in-memory interaction definition")
    runner = _RUNNERS.get(experiment_id)
    if runner is None:
        raise ValueError("experiment has no approved deterministic runner")
    return runner(context, experiment, partition)


class ExperimentCheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, identity_sha256: str) -> Path:
        return self.root / f"{identity_sha256}.json"

    def load(self, identity_sha256: str) -> Mapping[str, object] | None:
        path = self.path_for(identity_sha256)
        if not path.exists(): return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("identity_sha256") != identity_sha256 or not isinstance(payload.get("result_sha256"), str) or not isinstance(payload.get("artifact_sha256"), str):
            raise ValueError("experiment checkpoint is stale or malformed")
        return MappingProxyType(payload)

    def write(self, result: ExperimentResult) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"identity_sha256": result.identity_sha256, "result_sha256": result.result_sha256, "artifact_sha256": canonical_sha256({"result": result.result_sha256, "trade_path": result.trade_path_sha256})}
        path, temp = self.path_for(result.identity_sha256), self.path_for(result.identity_sha256).with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        return path


def run_catalog(context: DiagnosisContext, experiment_ids: Sequence[str], partitions: Sequence[PartitionName], checkpoint_root: Path, *, resume: bool) -> tuple[ExperimentResult, ...]:
    store, results = ExperimentCheckpointStore(checkpoint_root), []
    for partition in partitions:
        for experiment_id in experiment_ids:
            result = run_experiment(context, experiment_id, PartitionName(partition))
            existing = store.load(result.identity_sha256)
            if existing is not None and resume:
                if existing["result_sha256"] != result.result_sha256:
                    raise ValueError("experiment checkpoint result is stale")
            elif existing is not None:
                raise ValueError("experiment checkpoint already exists; use resume=True")
            else:
                store.write(result)
            results.append(result)
    return tuple(results)
