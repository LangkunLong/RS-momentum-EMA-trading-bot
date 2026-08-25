"""Deterministic, cache-only PIT CANSLIM diagnosis dispatch and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
from statistics import mean, median
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence

import pandas as pd

from .baseline import BaselineReproduction, BaselineSnapshot, compare_reproduction
from .catalog import build_experiment_identity
from .fact_cache import FactCache, SessionFact
from .metrics import EntryFunnel, ExitAttribution, ExitReasonAttribution, LeaderRecallEvidence, PerformanceEvidence, RuleAttribution, TradeStatistics
from .models import DatePartitions, ExperimentCatalog, ExperimentDefinition, FidelityAssessment, PartitionName, RuleOutcome, Rulebook
from .rulebook import canonical_sha256, evaluate_fidelity
from .strategy import CachedDiagnosisStrategy, DiagnosisPortfolioSimulator, evaluate_session_rules


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


def _result_primitive(result: ExperimentResult) -> dict[str, object]:
    return {
        "experiment_id": result.experiment_id, "partition": result.partition.value,
        "identity_sha256": result.identity_sha256, "result_sha256": result.result_sha256,
        "trade_path_sha256": result.trade_path_sha256,
        "fidelity": {"label": result.fidelity.label.value, "passed": result.fidelity.passed_required_rule_ids, "failed": result.fidelity.failed_required_rule_ids, "unavailable": result.fidelity.unavailable_required_rule_ids, "proxy": result.fidelity.proxy_rule_ids, "promotion": result.fidelity.promotion_eligible},
        "rule_attribution": [record.__dict__ for record in result.rule_attribution],
        "entry_funnel": {"evaluated": result.entry_funnel.evaluated, "qualified": result.entry_funnel.qualified, "attempted": result.entry_funnel.attempted, "executed": result.entry_funnel.executed, "rejections": dict(result.entry_funnel.rejections)},
        "exit_attribution": {reason: record.__dict__ for reason, record in result.exit_attribution.by_reason.items()},
        "trade_statistics": result.trade_statistics.__dict__,
        "performance": {**result.performance.__dict__, "partition": result.performance.partition.value},
        "leader_recall": result.leader_recall.__dict__, "promotion_eligible": result.promotion_eligible,
        "promotion_checks": dict(result.promotion_checks),
    }


def _result_from_primitive(value: Mapping[str, object]) -> ExperimentResult:
    fidelity_value = value["fidelity"]
    if not isinstance(fidelity_value, Mapping):
        raise ValueError("checkpoint result fidelity is malformed")
    fidelity = FidelityAssessment(fidelity_value["label"], tuple(fidelity_value["passed"]), tuple(fidelity_value["failed"]), tuple(fidelity_value["unavailable"]), tuple(fidelity_value["proxy"]), bool(fidelity_value["promotion"]))
    rules = tuple(RuleAttribution(**{**record, "evidence_ids": tuple(record["evidence_ids"])}) for record in value["rule_attribution"])
    funnel = EntryFunnel(**value["entry_funnel"])
    exits = ExitAttribution({reason: ExitReasonAttribution(**{**record, "evidence_ids": tuple(record["evidence_ids"])}) for reason, record in value["exit_attribution"].items()})
    performance_value = dict(value["performance"])
    performance = PerformanceEvidence(**performance_value)
    leader = dict(value["leader_recall"])
    leader["evidence_ids"] = tuple(leader["evidence_ids"])
    return ExperimentResult(str(value["experiment_id"]), PartitionName(value["partition"]), str(value["identity_sha256"]), str(value["result_sha256"]), str(value["trade_path_sha256"]), fidelity, rules, funnel, exits, TradeStatistics(**value["trade_statistics"]), performance, LeaderRecallEvidence(**leader), bool(value["promotion_eligible"]), MappingProxyType(dict(value["promotion_checks"])))


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
    baseline_reproduction: BaselineReproduction | None = None
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

    def with_verified_baseline_reproduction(
        self, reproduction: BaselineReproduction
    ) -> "DiagnosisContext":
        if not isinstance(reproduction, BaselineReproduction) or not reproduction.passed:
            raise ValueError("verified D0 reproduction is required")
        if self.baseline_snapshot is None or self.reproduced_baseline is None:
            raise ValueError("verified D0 reproduction requires both baseline snapshots")
        if (
            reproduction.authority_manifest_sha256 != self.baseline_snapshot.manifest_sha256
            or reproduction.reproduced_manifest_sha256 != self.reproduced_baseline.manifest_sha256
        ):
            raise ValueError("verified D0 reproduction does not match the supplied baseline snapshots")
        if self.baseline_snapshot is not self.reproduced_baseline and not compare_reproduction(
            self.baseline_snapshot, self.reproduced_baseline
        ).passed:
            raise ValueError("verified D0 reproduction does not exactly reproduce the baseline")
        return replace(self, baseline_reproduction=reproduction)


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


class _MaterializedFactCache:
    """Read-once scalar cache view used by every runner in a catalog batch."""

    def __init__(self, facts: Sequence[SessionFact]) -> None:
        self._facts = {(str(fact.symbol).upper(), str(fact.session)): fact for fact in facts}

    def session_fact(self, symbol: str, session: str) -> SessionFact:
        try:
            return self._facts[(str(symbol).upper(), str(session))]
        except KeyError as exc:
            raise KeyError(f"no cached diagnosis fact for {symbol} {session}") from exc

    def session_facts(self, start: str, end: str) -> tuple[SessionFact, ...]:
        return tuple(fact for (_symbol, session), fact in self._facts.items() if start <= session <= end)


@dataclass(frozen=True)
class _Replay:
    facts: tuple[SessionFact, ...]
    outcomes: tuple[Mapping[str, RuleOutcome], ...]
    signals: tuple[Mapping[str, object], ...]
    simulator: DiagnosisPortfolioSimulator
    equity: tuple[float, ...]


def _frame_for_facts(facts: Sequence[SessionFact]) -> dict[str, pd.DataFrame]:
    frames: dict[str, list[dict[str, object]]] = {}
    for fact in facts:
        values = fact.values
        frames.setdefault(str(fact.symbol).upper(), []).append({
            "date": str(fact.session), "Open": float(values.get("open") or 0.0),
            "High": float(values.get("high") or values.get("close") or 0.0),
            "Low": float(values.get("low") or values.get("close") or 0.0),
            "Close": float(values.get("close") or 0.0), "Volume": float(values.get("volume") or 0.0),
        })
    return {
        symbol: pd.DataFrame(rows).set_index(pd.to_datetime(pd.DataFrame(rows)["date"])).drop(columns="date").sort_index()
        for symbol, rows in frames.items()
    }


def _replay(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> _Replay:
    selected = _partition(context, partition)
    facts = _facts(context.fact_cache, selected.start, selected.end)
    cache = _MaterializedFactCache(facts)
    strategy = CachedDiagnosisStrategy(cache, context.rulebook, experiment)
    simulator = DiagnosisPortfolioSimulator(strategy=strategy, experiment_id=experiment.experiment_id, enable_eviction=False)
    frames = _frame_for_facts(facts)
    by_session: dict[str, list[SessionFact]] = {}
    for fact in facts:
        by_session.setdefault(str(fact.session), []).append(fact)
    sessions = tuple(sorted(by_session))
    pending: list[dict[str, object]] = []
    signals: list[Mapping[str, object]] = []
    equity: list[float] = []
    for session in sessions:
        eval_date = pd.Timestamp(session)
        for symbol in tuple(simulator._open_positions):
            simulator._check_exits(symbol, frames[symbol], eval_date)
        simulator._pending_entries_remaining = len(pending)
        for signal in pending:
            simulator._enter_position(signal, frames, eval_date)
            simulator._pending_entries_remaining -= 1
        pending = []
        for fact in sorted(by_session[session], key=lambda item: str(item.symbol)):
            signal = strategy.evaluate_symbol(
                ticker=str(fact.symbol), ticker_ohlcv=frames, all_closes=pd.DataFrame(),
                eval_date=eval_date, market_state={}, rs_score=None,
            )
            if signal is not None:
                signals.append(MappingProxyType(signal))
                if bool(signal["buy_signal"]):
                    pending.append(signal)
        equity.append(simulator._mark_equity(frames, eval_date))
    if sessions:
        last = pd.Timestamp(sessions[-1])
        for symbol in tuple(simulator._open_positions):
            simulator._close_trade(symbol, float(frames[symbol].loc[last, "Close"]), "end_of_test", str(last.date()))
        equity[-1] = simulator._mark_equity(frames, last)
    outcomes = tuple(MappingProxyType(dict(zip(context.rulebook.rules, evaluate_session_rules(fact, context.rulebook, experiment), strict=True))) for fact in facts)
    return _Replay(facts, outcomes, tuple(signals), simulator, tuple(equity))


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


def _trade_stats(trades: Sequence[object]) -> TradeStatistics:
    returns = [float(trade.pnl_pct) * 100.0 for trade in trades]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    calendar_holds = [max((pd.Timestamp(trade.exit_date) - pd.Timestamp(trade.entry_date)).days, 0) for trade in trades]
    sessions = [max(int(trade.days_held), 0) for trade in trades]
    return TradeStatistics(
        len(returns), len(wins), len(losses), 0.0 if not returns else len(wins) * 100.0 / len(returns),
        mean(returns) if returns else 0.0, median(returns) if returns else 0.0,
        mean(wins) if wins else 0.0, mean(losses) if losses else 0.0, mean(returns) if returns else 0.0,
        mean(calendar_holds) if calendar_holds else 0.0, median(calendar_holds) if calendar_holds else 0.0,
        mean(sessions) if sessions else 0.0, median(sessions) if sessions else 0.0,
    )


def _exit_attribution(trades: Sequence[object], *, evidence_prefix: str) -> ExitAttribution:
    groups: dict[str, list[object]] = {}
    for trade in trades:
        groups.setdefault(str(trade.exit_reason), []).append(trade)
    records: dict[str, ExitReasonAttribution] = {}
    for reason, grouped in groups.items():
        if reason not in {"stop_loss", "ma_violation", "time_stop", "end_of_test", "profit_zone", "structural_sell", "eight_week_hold"}:
            continue
        values = [float(trade.pnl_pct) * 100.0 for trade in grouped]
        wins = sum(value > 0.0 for value in values)
        records[reason] = ExitReasonAttribution(
            reason, len(grouped), wins, wins * 100.0 / len(grouped), mean(values),
            tuple(sorted(f"{evidence_prefix}:{reason}:{index}" for index in range(len(grouped)))),
        )
    return ExitAttribution(records or {"end_of_test": ExitReasonAttribution("end_of_test", 0, 0, 0.0, 0.0)})


def _baseline_trades(snapshot: BaselineSnapshot, partition: PartitionName | None, context: DiagnosisContext) -> tuple[object, ...]:
    path = snapshot.run_dir / "transactions.csv"
    if not path.is_file():
        raise ValueError("verified baseline snapshot lacks transactions.csv for exit attribution")
    frame = pd.read_csv(path, keep_default_na=False)
    required = {"Ticker", "Date", "Action", "Price", "Quantity", "Reason"}
    if not required.issubset(frame):
        raise ValueError("verified baseline transaction schema is incomplete")
    selected = _partition(context, partition) if partition is not None else None
    open_positions: dict[str, dict[str, object]] = {}
    completed: list[object] = []
    for row in frame.itertuples(index=False):
        ticker, action, date = str(row.Ticker), str(row.Action).upper(), str(row.Date)
        if action == "BUY":
            open_positions[ticker] = {"entry_date": date, "entry_price": float(row.Price), "qty": float(row.Quantity), "proceeds": 0.0, "sold": 0.0}
        elif action == "SELL" and ticker in open_positions:
            position = open_positions[ticker]
            position["proceeds"] = float(position["proceeds"]) + float(row.Price) * float(row.Quantity)
            position["sold"] = float(position["sold"]) + float(row.Quantity)
            if float(position["sold"]) + 1e-9 >= float(position["qty"]):
                entry = float(position["entry_price"])
                result = type("BaselineTrade", (), {})()
                result.entry_date, result.exit_date, result.days_held = str(position["entry_date"]), date, max((pd.Timestamp(date) - pd.Timestamp(position["entry_date"])).days, 0)
                result.pnl_pct = float(position["proceeds"]) / (entry * float(position["qty"])) - 1.0
                result.exit_reason = str(row.Reason)
                if selected is None or selected.start <= date <= selected.end:
                    completed.append(result)
                del open_positions[ticker]
    return tuple(completed)


def _verify_known_current_exit_cluster(snapshot: BaselineSnapshot, context: DiagnosisContext) -> None:
    if snapshot.closed_trades != 225:
        return
    trades = _baseline_trades(snapshot, None, context)
    attribution = _exit_attribution(trades, evidence_prefix="baseline")
    expected = {"stop_loss": 97, "ma_violation": 92, "time_stop": 30, "end_of_test": 6}
    actual = {reason: record.closed_positions for reason, record in attribution.by_reason.items()}
    if actual != expected:
        raise ValueError("verified baseline exit counts do not match immutable authority")
    ma = attribution.by_reason["ma_violation"]
    if round(ma.win_rate_pct, 2) != 15.22 or round(ma.average_completed_position_return_pct, 2) != -2.86:
        raise ValueError("verified baseline MA exit cluster does not match immutable authority")


def _build_result(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName, *, current_exit: bool = False, reproduction_ok: bool | None = None) -> ExperimentResult:
    replay = _replay(context, experiment, partition)
    facts, rows = replay.facts, replay.outcomes
    fidelity = _fidelity(context.rulebook, rows)
    stages = _rule_stages(context.rulebook, rows)
    observed_rules = {"I.SPONSORSHIP", "L.INDUSTRY_GROUP"}
    qualified = sum(all(outcome.status == "passed" for rule_id, outcome in row.items() if context.rulebook.rules[rule_id].classification.value == "required" and rule_id not in observed_rules) for row in rows)
    outcome_counts: dict[str, int] = {}
    for outcome in replay.simulator._entry_outcomes:
        outcome_counts[outcome.outcome] = outcome_counts.get(outcome.outcome, 0) + 1
    executed = outcome_counts.pop("entries_executed", 0)
    rejections = {name.removeprefix("entry_rejected_"): count for name, count in outcome_counts.items()}
    funnel = EntryFunnel(len(rows), qualified, sum(outcome_counts.values()) + executed, executed, rejections)
    snapshot = context.baseline_snapshot
    if current_exit and snapshot is not None:
        _verify_known_current_exit_cluster(snapshot, context)
        trades = _baseline_trades(snapshot, partition, context)
    else:
        trades = tuple(replay.simulator._trades)
    statistics = _trade_stats(trades)
    exits = _exit_attribution(trades, evidence_prefix="baseline" if current_exit else "replay")
    start_equity, end_equity = (replay.equity[0], replay.equity[-1]) if replay.equity else (1.0, 1.0)
    total_return = (end_equity / start_equity - 1.0) * 100.0 if start_equity else 0.0
    benchmark = [float(fact.values.get("close") or 0.0) for fact in facts if str(fact.symbol).upper() == "SPY"]
    benchmark_return = (benchmark[-1] / benchmark[0] - 1.0) * 100.0 if len(benchmark) > 1 and benchmark[0] else 0.0
    performance = PerformanceEvidence(partition, total_return, total_return, 0.0, min(0.0, total_return), 100.0 if not replay.equity else max(0.0, min(100.0, replay.simulator._equity / end_equity * 100.0 if end_equity else 100.0)), statistics.completed_positions, total_return - benchmark_return, total_return - benchmark_return)
    fact_symbols = {str(fact.symbol).upper() for fact in facts}
    labels = set(context.diagnostic_leader_labels)
    exposed = len(labels & fact_symbols)
    signalled_symbols = {str(signal["symbol"]).upper() for signal in replay.signals if bool(signal["buy_signal"])}
    recalled = len(labels & signalled_symbols)
    recall = LeaderRecallEvidence(len(labels), exposed, recalled, 0.0 if not exposed else recalled * 100.0 / exposed, tuple(f"leader:{label}" for label in sorted(labels)))
    path_payload = {"experiment_id": experiment.experiment_id, "partition": partition.value, "signals": list(sorted((str(signal["symbol"]), str(signal["signal_date"])) for signal in replay.signals if bool(signal["buy_signal"]))), "outcomes": [outcome.to_primitive() for outcome in replay.simulator._entry_outcomes], "transactions": replay.simulator._transactions}
    trade_path = canonical_sha256(path_payload)
    checks = {
        "not_locked_evaluation": partition is not PartitionName.LOCKED_EVALUATION,
        "fidelity_complete": fidelity.promotion_eligible,
        "discovery_floor": statistics.completed_positions >= 60 if partition is PartitionName.DISCOVERY else True,
        "validation_floor": statistics.completed_positions >= 20 if partition is PartitionName.VALIDATION else True,
        "positive_expectancy": statistics.expectancy_pct > 0.0,
        "non_worse_return": snapshot is None or performance.total_return_pct >= snapshot.total_return_pct,
        "non_worse_annualized_return": snapshot is None or performance.annualized_return_pct >= snapshot.annualized_return_pct,
        "non_worse_sharpe": snapshot is None or performance.sharpe_ratio >= snapshot.sharpe_ratio,
        "non_worse_drawdown": snapshot is None or performance.max_drawdown_pct >= snapshot.max_drawdown_pct,
        "non_worse_pit_recall": recall.recall_pct >= 0.0,
        "strict_improvement": snapshot is not None and (performance.total_return_pct > snapshot.total_return_pct or performance.annualized_return_pct > snapshot.annualized_return_pct or performance.sharpe_ratio > snapshot.sharpe_ratio or performance.max_drawdown_pct > snapshot.max_drawdown_pct),
        "reproduction_exact": reproduction_ok is not False,
    }
    promotable = experiment.promotion_eligible and all(checks.values())
    payload = {"experiment_id": experiment.experiment_id, "partition": partition.value, "trade_path": trade_path, "fidelity": fidelity.label.value, "stages": [(stage.rule_id, stage.survivors) for stage in stages], "promotion": promotable}
    return ExperimentResult(experiment.experiment_id, partition, _identity(context, experiment, partition), canonical_sha256(payload), trade_path, fidelity, stages, funnel, exits, statistics, performance, recall, promotable, MappingProxyType(checks))


def run_baseline_reproduction(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult:
    if context.baseline_snapshot is None or context.reproduced_baseline is None:
        raise ValueError("D0 requires verified authority and reproduced baseline snapshots")
    exact = context.baseline_snapshot is context.reproduced_baseline or compare_reproduction(context.baseline_snapshot, context.reproduced_baseline).passed
    return _build_result(context, experiment, partition, reproduction_ok=exact)


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
    if experiment.phase != "D0" and (
        context.baseline_reproduction is None or not context.baseline_reproduction.passed
    ):
        raise ValueError("D1-D4 require a verified D0 baseline reproduction")
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
        if payload.get("identity_sha256") != identity_sha256 or not isinstance(payload.get("result_sha256"), str) or not isinstance(payload.get("artifact_sha256"), str) or not isinstance(payload.get("result"), Mapping):
            raise ValueError("experiment checkpoint is stale or malformed")
        expected = canonical_sha256({"result": payload["result_sha256"], "trade_path": payload["result"].get("trade_path_sha256")})
        if payload["artifact_sha256"] != expected:
            raise ValueError("experiment checkpoint artifact hash is stale")
        return MappingProxyType(payload)

    def write(self, result: ExperimentResult) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"identity_sha256": result.identity_sha256, "result_sha256": result.result_sha256, "result": _result_primitive(result), "artifact_sha256": canonical_sha256({"result": result.result_sha256, "trade_path": result.trade_path_sha256})}
        path, temp = self.path_for(result.identity_sha256), self.path_for(result.identity_sha256).with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        return path


def run_catalog(context: DiagnosisContext, experiment_ids: Sequence[str], partitions: Sequence[PartitionName], checkpoint_root: Path, *, resume: bool) -> tuple[ExperimentResult, ...]:
    store, results = ExperimentCheckpointStore(checkpoint_root), []
    batch_context: DiagnosisContext | None = None
    for partition in partitions:
        for experiment_id in experiment_ids:
            name = PartitionName(partition)
            experiment = context.catalog[experiment_id]
            if name is PartitionName.LOCKED_EVALUATION:
                raise ValueError("locked evaluation cannot run through the default diagnosis API")
            if experiment.phase == "D5":
                raise ValueError("D5 requires a controller-composed in-memory interaction definition")
            if experiment.phase != "D0" and (context.baseline_reproduction is None or not context.baseline_reproduction.passed):
                raise ValueError("D1-D4 require a verified D0 baseline reproduction")
            identity = _identity(context, experiment, name)
            existing = store.load(identity)
            if existing is not None and resume:
                result = _result_from_primitive(existing["result"])
                if result.identity_sha256 != identity or result.result_sha256 != existing["result_sha256"]:
                    raise ValueError("experiment checkpoint result is stale")
                results.append(result)
                continue
            if existing is not None:
                raise ValueError("experiment checkpoint already exists; use resume=True")
            if batch_context is None:
                all_facts = _facts(context.fact_cache, context.partitions.discovery.start, context.partitions.validation.end)
                materialized = _MaterializedFactCache(all_facts)
                materialized.content_sha256 = _cache_digest(context.fact_cache, "content_sha256")
                materialized.schema_sha256 = _cache_digest(context.fact_cache, "schema_sha256")
                batch_context = replace(context, fact_cache=materialized)
            result = run_experiment(batch_context, experiment_id, name)
            store.write(result)
            results.append(result)
    return tuple(results)
