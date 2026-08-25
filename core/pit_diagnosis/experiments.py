"""Deterministic, cache-only PIT CANSLIM diagnosis dispatch and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean, median
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence

import pandas as pd

from . import baseline as baseline_module
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
    performance: PerformanceEvidence | None
    leader_recall: LeaderRecallEvidence
    promotion_eligible: bool
    promotion_checks: Mapping[str, bool]


_RESULT_FIELDS = frozenset(ExperimentResult.__dataclass_fields__)
_CHECKPOINT_SCHEMA_VERSION = 1


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
        "performance": None if result.performance is None else {**result.performance.__dict__, "partition": result.performance.partition.value},
        "leader_recall": result.leader_recall.__dict__, "promotion_eligible": result.promotion_eligible,
        "promotion_checks": dict(result.promotion_checks),
    }


def _result_payload_sha256(value: ExperimentResult | Mapping[str, object]) -> str:
    primitive = _result_primitive(value) if isinstance(value, ExperimentResult) else dict(value)
    if set(primitive) != _RESULT_FIELDS:
        raise ValueError("experiment result payload is malformed")
    primitive.pop("result_sha256")
    return canonical_sha256(
        {"result_schema_version": 1, "result": primitive}
    )


def _checkpoint_payload(
    result: ExperimentResult,
) -> dict[str, object]:
    primitive = _result_primitive(result)
    payload: dict[str, object] = {
        "checkpoint_schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "identity_sha256": result.identity_sha256,
        "result_sha256": result.result_sha256,
        "result": primitive,
    }
    payload["artifact_sha256"] = canonical_sha256(payload)
    return payload


def _result_from_primitive(value: Mapping[str, object]) -> ExperimentResult:
    fidelity_value = value["fidelity"]
    if not isinstance(fidelity_value, Mapping):
        raise ValueError("checkpoint result fidelity is malformed")
    fidelity = FidelityAssessment(fidelity_value["label"], tuple(fidelity_value["passed"]), tuple(fidelity_value["failed"]), tuple(fidelity_value["unavailable"]), tuple(fidelity_value["proxy"]), bool(fidelity_value["promotion"]))
    rules = tuple(RuleAttribution(**{**record, "evidence_ids": tuple(record["evidence_ids"])}) for record in value["rule_attribution"])
    funnel = EntryFunnel(**value["entry_funnel"])
    exits = ExitAttribution({reason: ExitReasonAttribution(**{**record, "evidence_ids": tuple(record["evidence_ids"])}) for reason, record in value["exit_attribution"].items()})
    performance = None if value["performance"] is None else PerformanceEvidence(**dict(value["performance"]))
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
    # The bundle used by the current diagnosis run.  ``baseline_snapshot`` is
    # intentionally kept separate: it identifies the frozen authority replay,
    # while this digest identifies the PIT data actually evaluated.
    bundle_sha256: str | None = None
    baseline_snapshot: BaselineSnapshot | None = None
    reproduced_baseline: BaselineSnapshot | None = None
    baseline_reproduction: BaselineReproduction | None = None
    partition_baseline_performance: Mapping[PartitionName, PerformanceEvidence] = field(default_factory=dict)
    partition_baseline_recall: Mapping[PartitionName, LeaderRecallEvidence] = field(default_factory=dict)
    benchmark_identity: str = "SPY"
    universe_identity: str = "pit-members"

    def __post_init__(self) -> None:
        if len(self.source_commit) != 40 or any(char not in "0123456789abcdef" for char in self.source_commit):
            raise ValueError("source_commit must be a lowercase Git SHA-1")
        for name in ("source_fingerprint_sha256",):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a SHA-256")
        if self.bundle_sha256 is not None and (
            not isinstance(self.bundle_sha256, str)
            or len(self.bundle_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.bundle_sha256)
        ):
            raise ValueError("bundle_sha256 must be a lowercase SHA-256")
        labels = tuple(str(label).upper() for label in self.diagnostic_leader_labels)
        if labels != tuple(sorted(labels)) or len(set(labels)) != len(labels):
            raise ValueError("diagnostic leader labels must be sorted and unique")
        object.__setattr__(self, "diagnostic_leader_labels", labels)
        performance: dict[PartitionName, PerformanceEvidence] = {}
        for key, value in self.partition_baseline_performance.items():
            name = PartitionName(key)
            if not isinstance(value, PerformanceEvidence) or value.partition is not name:
                raise ValueError("partition baseline performance must match its partition")
            performance[name] = value
        recall: dict[PartitionName, LeaderRecallEvidence] = {}
        for key, value in self.partition_baseline_recall.items():
            name = PartitionName(key)
            if not isinstance(value, LeaderRecallEvidence):
                raise ValueError("partition baseline recall must contain LeaderRecallEvidence")
            recall[name] = value
        object.__setattr__(self, "partition_baseline_performance", MappingProxyType(performance))
        object.__setattr__(self, "partition_baseline_recall", MappingProxyType(recall))

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
        if not compare_reproduction(self.baseline_snapshot, self.reproduced_baseline).passed:
            raise ValueError("verified D0 reproduction does not exactly reproduce the baseline")
        return replace(self, baseline_reproduction=reproduction)


ExperimentRunner = Callable[[DiagnosisContext, ExperimentDefinition, PartitionName], ExperimentResult]


def _require_verified_d0(context: DiagnosisContext) -> None:
    reproduction = context.baseline_reproduction
    authority, reproduced = context.baseline_snapshot, context.reproduced_baseline
    if not isinstance(reproduction, BaselineReproduction) or not reproduction.passed:
        raise ValueError("D1-D4 require a verified D0 baseline reproduction")
    if authority is None or reproduced is None:
        raise ValueError("D1-D4 require a verified D0 baseline reproduction")
    if (
        reproduction.authority_manifest_sha256 != authority.manifest_sha256
        or reproduction.reproduced_manifest_sha256 != reproduced.manifest_sha256
    ):
        raise ValueError("D1-D4 require a verified D0 baseline reproduction")
    if not compare_reproduction(authority, reproduced).passed:
        raise ValueError("D1-D4 require a verified D0 baseline reproduction")


def _facts(cache: FactCache | _FactReader, start: str, end: str) -> tuple[SessionFact, ...]:
    reader = getattr(cache, "session_facts", None)
    if callable(reader):
        return tuple(fact for fact in reader(start, end) if _has_price_evidence(fact))
    connection = getattr(cache, "_connection", None)
    if connection is None:
        raise ValueError("fact cache does not expose a read-only scalar-fact reader")
    rows = connection.execute("SELECT * FROM session_facts WHERE session>=? AND session<=? ORDER BY session,symbol", (start, end)).fetchall()
    return tuple(
        fact for row in rows
        if _has_price_evidence(fact := SessionFact(MappingProxyType({key: row[key] for key in row.keys()})))
    )


def _has_price_evidence(fact: SessionFact) -> bool:
    availability = fact.values.get("availability_bitset")
    # Legacy test readers predate the bitset; finalized cache rows always have it.
    return availability is None or type(availability) is int and availability & 1 == 1


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
        bundle_sha256=(context.bundle_sha256 or (baseline.bundle_sha256 if baseline else "0" * 64)),
        baseline_manifest_sha256=(baseline.manifest_sha256 if baseline else "0" * 64),
        rulebook_sha256=context.rulebook.sha256, fact_cache_schema_sha256=_cache_digest(context.fact_cache, "schema_sha256"),
        fact_cache_content_sha256=_cache_digest(context.fact_cache, "content_sha256"), catalog_sha256=context.catalog.sha256,
        experiment=experiment, partition=_partition(context, partition), strategy_identity=context.strategy_identity,
        benchmark_identity=context.benchmark_identity, universe_identity=context.universe_identity,
        promotion_evidence_sha256=_promotion_evidence_sha256(context),
    ).sha256


def _promotion_evidence_sha256(context: DiagnosisContext) -> str:
    performance = {
        name.value: {
            "total_return_pct": value.total_return_pct, "annualized_return_pct": value.annualized_return_pct,
            "sharpe_ratio": value.sharpe_ratio, "max_drawdown_pct": value.max_drawdown_pct,
            "average_cash_pct": value.average_cash_pct, "closed_positions": value.closed_positions,
            "benchmark_total_return_delta_pct": value.benchmark_total_return_delta_pct,
            "benchmark_annualized_return_delta_pct": value.benchmark_annualized_return_delta_pct,
        }
        for name, value in sorted(context.partition_baseline_performance.items(), key=lambda item: item[0].value)
    }
    recall = {
        name.value: {"labelled_leaders": value.labelled_leaders, "pit_exposed_leaders": value.pit_exposed_leaders, "recalled_leaders": value.recalled_leaders, "recall_pct": value.recall_pct, "evidence_ids": value.evidence_ids}
        for name, value in sorted(context.partition_baseline_recall.items(), key=lambda item: item[0].value)
    }
    return canonical_sha256({"performance": performance, "recall": recall})


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
    raw_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact_sha = snapshot.artifact_sha256.get("transactions.csv")
    if artifact_sha is not None and raw_sha != artifact_sha:
        raise ValueError("baseline transaction ledger hash does not match snapshot artifact")
    frame = pd.read_csv(path, keep_default_na=False)
    if baseline_module._normalized_ordered_row_sha256(frame) != snapshot.transaction_row_sha256:
        raise ValueError("baseline transaction ledger hash does not match snapshot row identity")
    required = {"Ticker", "Date", "Action", "Price", "Quantity", "Reason"}
    if not required.issubset(frame):
        raise ValueError("verified baseline transaction schema is incomplete")
    selected = _partition(context, partition) if partition is not None else None
    # The execution ledger contains one BUY row per entry and can contain
    # several SELL rows for a position (scale-outs followed by the terminal
    # exit).  Keep lots instead of indexing by ticker alone: a ticker can be
    # re-entered after a prior lot's final sell, and the quantities are rounded
    # to a fixed precision in the CSV.  The old ticker -> position map could
    # therefore overwrite an entry when its last scale-out was only a few
    # floating-point units short, dropping otherwise authoritative trades.
    open_positions: dict[str, list[dict[str, object]]] = {}
    completed: list[object] = []

    def _quantity_tolerance(quantity: float) -> float:
        # Transaction quantities are serialized at approximately six decimal
        # places.  Use a small relative tolerance for the residual generated
        # by summing rounded scale-outs, while keeping the minimum non-zero so
        # very small lots are treated consistently.
        return max(5e-6, abs(quantity) * 1e-8)

    for row in frame.itertuples(index=False):
        ticker, action, date = str(row.Ticker), str(row.Action).upper(), str(row.Date)
        if action == "BUY":
            quantity = float(row.Quantity)
            if quantity <= 0.0:
                raise ValueError("baseline transaction ledger contains a non-positive BUY quantity")
            open_positions.setdefault(ticker, []).append(
                {
                    "entry_date": date,
                    "entry_price": float(row.Price),
                    "qty": quantity,
                    "proceeds": 0.0,
                    "sold": 0.0,
                }
            )
        elif action == "SELL":
            remaining = float(row.Quantity)
            if remaining <= 0.0:
                raise ValueError("baseline transaction ledger contains a non-positive SELL quantity")
            lots = open_positions.get(ticker)
            if not lots:
                raise ValueError("baseline transaction ledger contains a SELL without an open BUY lot")
            while remaining > 0.0 and lots:
                position = lots[0]
                quantity = float(position["qty"])
                sold = float(position["sold"])
                unsold = max(quantity - sold, 0.0)
                if unsold <= _quantity_tolerance(quantity):
                    raise ValueError("baseline transaction ledger contains a residual BUY lot before SELL")
                sold_now = min(remaining, unsold)
                position["proceeds"] = float(position["proceeds"]) + float(row.Price) * sold_now
                position["sold"] = sold + sold_now
                remaining -= sold_now
                if float(position["qty"]) - float(position["sold"]) <= _quantity_tolerance(quantity):
                    entry = float(position["entry_price"])
                    result = type("BaselineTrade", (), {})()
                    result.entry_date = str(position["entry_date"])
                    result.exit_date = date
                    result.days_held = max((pd.Timestamp(date) - pd.Timestamp(position["entry_date"])).days, 0)
                    result.pnl_pct = float(position["proceeds"]) / (entry * quantity) - 1.0
                    result.exit_reason = str(row.Reason)
                    if selected is None or selected.start <= date <= selected.end:
                        completed.append(result)
                    lots.pop(0)
            if remaining > _quantity_tolerance(float(row.Quantity)):
                raise ValueError("baseline transaction ledger SELL quantity exceeds open BUY lots")
            if not lots:
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


def _baseline_partition_performance(
    snapshot: BaselineSnapshot, partition: PartitionName, context: DiagnosisContext,
    closed_positions: int,
) -> PerformanceEvidence | None:
    """Compute portfolio metrics only from hash-bound partition ledgers."""
    holdings_path = snapshot.run_dir / "weekly_holdings.csv"
    expected_holdings_sha = snapshot.artifact_sha256.get("weekly_holdings.csv")
    if not holdings_path.is_file() or expected_holdings_sha is None:
        return None
    if hashlib.sha256(holdings_path.read_bytes()).hexdigest() != expected_holdings_sha:
        raise ValueError("baseline weekly holdings hash does not match snapshot artifact")
    try:
        holdings = pd.read_csv(holdings_path, keep_default_na=False)
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    required_holdings = ("Week_Ending", "Cash", "Total_Equity")
    if not set(required_holdings).issubset(holdings):
        return None
    holdings["Week_Ending"] = pd.to_datetime(
        holdings["Week_Ending"], errors="coerce"
    )
    for column in ("Cash", "Total_Equity"):
        holdings[column] = pd.to_numeric(holdings[column], errors="coerce")
    invalid_holdings = (
        holdings.empty
        or holdings[list(required_holdings)].isna().any().any()
        or not holdings[["Cash", "Total_Equity"]].map(math.isfinite).all().all()
        or holdings["Week_Ending"].duplicated().any()
        or not holdings["Week_Ending"].is_monotonic_increasing
        or (holdings["Cash"] < 0.0).any()
        or (holdings["Total_Equity"] <= 0.0).any()
        or (holdings["Cash"] > holdings["Total_Equity"]).any()
    )
    if invalid_holdings:
        return None
    selected = _partition(context, partition)
    holdings = holdings.loc[
        (holdings["Week_Ending"] >= selected.start)
        & (holdings["Week_Ending"] <= selected.end)
    ]
    if holdings.empty:
        return None
    average_cash = float((holdings["Cash"] / holdings["Total_Equity"]).mean() * 100.0)

    path = snapshot.run_dir / "equity_curve.csv"
    expected_sha = snapshot.artifact_sha256.get("equity_curve.csv")
    if not path.is_file() or expected_sha is None:
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("baseline equity ledger hash does not match snapshot artifact")
    try:
        frame = pd.read_csv(path, keep_default_na=False)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ValueError("baseline equity ledger is invalid") from exc
    if not {"date", "portfolio", "benchmark"}.issubset(frame):
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("portfolio", "benchmark"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[(frame["date"] >= selected.start) & (frame["date"] <= selected.end)].sort_values("date")
    if len(frame) < 2 or frame[["date", "portfolio", "benchmark"]].isna().any().any() or (frame[["portfolio", "benchmark"]] <= 0.0).any().any():
        return None
    first, last = frame.iloc[0], frame.iloc[-1]
    total_return = (float(last.portfolio) / float(first.portfolio) - 1.0) * 100.0
    benchmark_return = (float(last.benchmark) / float(first.benchmark) - 1.0) * 100.0
    elapsed_days = max((last.date - first.date).days, 0)
    annualized = total_return if elapsed_days == 0 else ((float(last.portfolio) / float(first.portfolio)) ** (365.25 / elapsed_days) - 1.0) * 100.0
    benchmark_annualized = benchmark_return if elapsed_days == 0 else ((float(last.benchmark) / float(first.benchmark)) ** (365.25 / elapsed_days) - 1.0) * 100.0
    returns = frame["portfolio"].pct_change().dropna()
    volatility = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    sharpe = 0.0 if volatility == 0.0 else float(returns.mean() / volatility * (252.0 ** 0.5))
    drawdown = float((frame["portfolio"] / frame["portfolio"].cummax() - 1.0).min() * 100.0)
    return PerformanceEvidence(partition, total_return, annualized, sharpe, drawdown, average_cash, closed_positions, total_return - benchmark_return, annualized - benchmark_annualized)


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
    if current_exit:
        performance = _baseline_partition_performance(snapshot, partition, context, statistics.completed_positions) if snapshot is not None else None
    else:
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
    reference_performance = context.partition_baseline_performance.get(partition)
    reference_recall = context.partition_baseline_recall.get(partition)
    checks = {
        "not_locked_evaluation": partition is not PartitionName.LOCKED_EVALUATION,
        "fidelity_complete": fidelity.promotion_eligible,
        "discovery_floor": statistics.completed_positions >= 60 if partition is PartitionName.DISCOVERY else True,
        "validation_floor": statistics.completed_positions >= 20 if partition is PartitionName.VALIDATION else True,
        "positive_expectancy": statistics.expectancy_pct > 0.0,
        "performance_complete": performance is not None,
        "partition_baseline_performance_available": reference_performance is not None,
        "partition_baseline_recall_available": reference_recall is not None,
        "non_worse_return": performance is not None and reference_performance is not None and performance.total_return_pct >= reference_performance.total_return_pct,
        "non_worse_annualized_return": performance is not None and reference_performance is not None and performance.annualized_return_pct >= reference_performance.annualized_return_pct,
        "non_worse_sharpe": performance is not None and reference_performance is not None and performance.sharpe_ratio >= reference_performance.sharpe_ratio,
        "non_worse_drawdown": performance is not None and reference_performance is not None and performance.max_drawdown_pct >= reference_performance.max_drawdown_pct,
        "non_worse_pit_recall": reference_recall is not None and recall.recall_pct >= reference_recall.recall_pct,
        "strict_improvement": performance is not None and reference_performance is not None and (performance.total_return_pct > reference_performance.total_return_pct or performance.annualized_return_pct > reference_performance.annualized_return_pct or performance.sharpe_ratio > reference_performance.sharpe_ratio or performance.max_drawdown_pct > reference_performance.max_drawdown_pct),
        "reproduction_exact": (context.baseline_reproduction is not None and context.baseline_reproduction.passed) if reproduction_ok is None else reproduction_ok,
    }
    promotable = experiment.promotion_eligible and all(checks.values())
    result = ExperimentResult(experiment.experiment_id, partition, _identity(context, experiment, partition), "0" * 64, trade_path, fidelity, stages, funnel, exits, statistics, performance, recall, promotable, MappingProxyType(checks))
    return replace(result, result_sha256=_result_payload_sha256(result))


def run_baseline_reproduction(context: DiagnosisContext, experiment: ExperimentDefinition, partition: PartitionName) -> ExperimentResult:
    if context.baseline_snapshot is None or context.reproduced_baseline is None:
        raise ValueError("D0 requires verified authority and reproduced baseline snapshots")
    exact = compare_reproduction(context.baseline_snapshot, context.reproduced_baseline).passed
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
    if experiment.phase != "D0":
        _require_verified_d0(context)
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
        required = {"checkpoint_schema_version", "identity_sha256", "result_sha256", "result", "artifact_sha256"}
        if not isinstance(payload, Mapping) or set(payload) != required or payload.get("checkpoint_schema_version") != _CHECKPOINT_SCHEMA_VERSION or payload.get("identity_sha256") != identity_sha256 or not isinstance(payload.get("result_sha256"), str) or not isinstance(payload.get("artifact_sha256"), str) or not isinstance(payload.get("result"), Mapping):
            raise ValueError("experiment checkpoint is stale or malformed")
        artifact_payload = dict(payload)
        artifact_sha256 = artifact_payload.pop("artifact_sha256")
        if artifact_sha256 != canonical_sha256(artifact_payload):
            raise ValueError("experiment checkpoint artifact hash is stale")
        result = payload["result"]
        if set(result) != _RESULT_FIELDS or result.get("identity_sha256") != identity_sha256 or result.get("result_sha256") != payload["result_sha256"]:
            raise ValueError("experiment checkpoint result is stale")
        if payload["result_sha256"] != _result_payload_sha256(result):
            raise ValueError("experiment checkpoint result hash is stale")
        return MappingProxyType(payload)

    def write(self, result: ExperimentResult) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if result.result_sha256 != _result_payload_sha256(result):
            raise ValueError("experiment result hash is stale")
        payload = _checkpoint_payload(result)
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
            if experiment.phase != "D0":
                _require_verified_d0(context)
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


def run_locked_catalog(
    context: DiagnosisContext,
    experiment_ids: Sequence[str],
    checkpoint_root: Path,
    *,
    human_selection_id: str,
    research_generation_id: str,
    resume: bool,
) -> tuple[ExperimentResult, ...]:
    """Run explicitly selected locked evidence without weakening default lock guards."""
    if not isinstance(human_selection_id, str) or not human_selection_id:
        raise ValueError("locked evaluation requires a human selection ID")
    if not isinstance(research_generation_id, str) or not research_generation_id:
        raise ValueError("locked evaluation requires a research generation ID")
    selection_sha256 = canonical_sha256({
        "human_selection_id": human_selection_id,
        "research_generation_id": research_generation_id,
    })
    locked_context = replace(
        context,
        strategy_identity=f"{context.strategy_identity}:locked:{selection_sha256}",
    )
    store, results = ExperimentCheckpointStore(checkpoint_root), []
    for experiment_id in experiment_ids:
        experiment = locked_context.catalog[experiment_id]
        if experiment.phase in {"D0", "D5"}:
            raise ValueError("locked evaluation accepts exactly approved D1-D4 experiments")
        _require_verified_d0(locked_context)
        identity = _identity(locked_context, experiment, PartitionName.LOCKED_EVALUATION)
        existing = store.load(identity)
        if existing is not None and resume:
            results.append(_result_from_primitive(existing["result"]))
            continue
        if existing is not None:
            raise ValueError("experiment checkpoint already exists; use resume=True")
        runner = _RUNNERS.get(experiment_id)
        if runner is None:
            raise ValueError("experiment has no approved deterministic runner")
        result = runner(locked_context, experiment, PartitionName.LOCKED_EVALUATION)
        store.write(result)
        results.append(result)
    return tuple(results)
