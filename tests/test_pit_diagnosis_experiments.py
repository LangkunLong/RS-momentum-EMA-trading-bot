from __future__ import annotations

from dataclasses import replace
import csv
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

import pytest
import pandas as pd

from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.baseline import BaselineReproduction, BaselineSnapshot
import core.pit_diagnosis.baseline as baseline_module
import core.pit_diagnosis.experiments as experiments_module
from core.pit_diagnosis.experiments import DiagnosisContext, run_catalog, run_experiment, run_locked_catalog
from core.pit_diagnosis.fact_cache import SessionFact
from core.pit_diagnosis.models import FidelityLabel, PartitionName
from core.pit_diagnosis.metrics import LeaderRecallEvidence, PerformanceEvidence
from core.pit_diagnosis.rulebook import load_rulebook
from core.pit_diagnosis.strategy import DiagnosisPortfolioSimulator
from core.backtest_engine import SimulationResult, Trade
from pit_baseline import _equity_frame


class _Facts:
    def __init__(self, rows: tuple[SessionFact, ...]) -> None:
        self.rows = rows
        self.content_sha256 = "a" * 64
        self.schema_sha256 = "b" * 64

    def session_facts(self, start: str, end: str) -> tuple[SessionFact, ...]:
        return tuple(row for row in self.rows if start <= str(row.session) <= end)


def _fact(symbol: str, session: str, *, market: str = "uptrend", rs: float = 90.0) -> SessionFact:
    return SessionFact(
        MappingProxyType(
            {
                "symbol": symbol,
                "session": session,
                "row_sha256": ("a" if symbol == "AAA" else "b") * 64,
                "bundle_sha256": "c" * 64,
                "current_eps_yoy": 0.30,
                "sales_yoy": 0.30,
                "annual_eps_1": 4.0,
                "annual_eps_2": 3.0,
                "annual_eps_3": 2.0,
                "annual_eps_4": 1.0,
                "roe": 0.20,
                "base_kind": "flat",
                "pivot": 100.0,
                "close": 103.0,
                "open": 103.0,
                "event_volume_ratio": 1.5,
                "rs_rating": rs,
                "extension_pct": 0.03,
                "market_regime": market,
                "distribution_count": 0,
                "institutional_evidence_ids": "[]",
                "industry_evidence_ids": "[]",
            }
        )
    )


def test_replay_fact_reader_skips_members_without_explicit_price_evidence() -> None:
    unavailable_values = dict(_fact("NEW", "2024-01-02").values)
    unavailable_values.update({"open": None, "high": None, "low": None, "close": None, "volume": None, "availability_bitset": 0})
    unavailable = SessionFact(MappingProxyType(unavailable_values))
    priced_values = dict(_fact("AAA", "2024-01-02").values)
    priced_values["availability_bitset"] = 1
    priced = SessionFact(MappingProxyType(priced_values))

    assert experiments_module._facts(_Facts((unavailable, priced)), "2024-01-02", "2024-01-02") == (priced,)


def _baseline_snapshot(root: Path) -> BaselineSnapshot:
    rows: list[dict[str, object]] = []
    reasons = [*("stop_loss",) * 97, *("ma_violation",) * 92, *("time_stop",) * 30, *("end_of_test",) * 6]
    ma_losses = 0
    ma_wins = 0
    stop_wins = 0
    time_wins = 0
    final_wins = 0
    for index, reason in enumerate(reasons):
        ticker = f"T{index:03d}"
        if reason == "ma_violation":
            price = 110.0 if ma_wins < 14 else 94.83179487179487
            ma_wins += price == 110.0
            ma_losses += price != 110.0
        elif reason == "stop_loss":
            price = 110.0 if stop_wins < 50 else 92.0
            stop_wins += price == 110.0
        elif reason == "time_stop":
            price = 110.0 if time_wins < 20 else 99.0
            time_wins += price == 110.0
        else:
            price = 110.0 if final_wins < 4 else 95.0
            final_wins += price == 110.0
        rows.extend((
            {"Ticker": ticker, "Date": "2021-01-04", "Action": "BUY", "Price": 100.0, "Quantity": 1.0, "Reason": "entry"},
            {"Ticker": ticker, "Date": "2021-01-05", "Action": "SELL", "Price": price, "Quantity": 1.0, "Reason": reason},
        ))
    with (root / "transactions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("Ticker", "Date", "Action", "Price", "Quantity", "Reason"))
        writer.writeheader()
        writer.writerows(rows)
    (root / "entry_attempt_outcomes.csv").write_text("symbol,signal_date,entry_date,pivot,buy_zone_lower,buy_zone_upper,entry_open,outcome\n", encoding="utf-8")
    sessions = pd.to_datetime(("2021-01-04", "2021-06-30", "2021-12-31"))
    baseline_result = SimulationResult(
        equity_curve=pd.Series((100.0, 105.0, 110.0), index=sessions),
        benchmark_curve=pd.Series((100.0, 103.0, 106.0), index=sessions),
        weekly_holdings=pd.DataFrame(
            (
                {
                    "Week_Ending": "2021-01-04",
                    "Holdings": "AAA",
                    "Holding_Count": 1,
                    "Cash": 50.0,
                    "Market_Value": 50.0,
                    "Total_Equity": 100.0,
                },
                {
                    "Week_Ending": "2021-06-30",
                    "Holdings": "AAA",
                    "Holding_Count": 1,
                    "Cash": 21.0,
                    "Market_Value": 84.0,
                    "Total_Equity": 105.0,
                },
                {
                    "Week_Ending": "2021-12-31",
                    "Holdings": "AAA",
                    "Holding_Count": 1,
                    "Cash": 11.0,
                    "Market_Value": 99.0,
                    "Total_Equity": 110.0,
                },
            )
        ),
    )
    _equity_frame(baseline_result).to_csv(root / "equity_curve.csv", index=False)
    baseline_result.weekly_holdings.to_csv(root / "weekly_holdings.csv", index=False)
    (root / "leader_recall.csv").write_text("ticker,buy_signal_count,entry_count\n", encoding="utf-8")
    (root / "summary.json").write_text("{}\n", encoding="utf-8")
    frame = pd.read_csv(root / "transactions.csv", keep_default_na=False)
    return BaselineSnapshot(root, "a" * 64, "b" * 40, "c" * 40, "d" * 64, {"transactions.csv": hashlib.sha256((root / "transactions.csv").read_bytes()).hexdigest(), "equity_curve.csv": hashlib.sha256((root / "equity_curve.csv").read_bytes()).hexdigest(), "weekly_holdings.csv": hashlib.sha256((root / "weekly_holdings.csv").read_bytes()).hexdigest()}, "e" * 64, "f" * 64, baseline_module._normalized_ordered_row_sha256(frame), -9.99, -2.0, -0.2, -13.0, 225, 39.11, 26.6666666667, 286, 225, 51, 10)


@pytest.fixture
def diagnosis_context(tmp_path: Path) -> DiagnosisContext:
    rulebook = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), rulebook)
    snapshot = _baseline_snapshot(tmp_path)
    return DiagnosisContext(
        rulebook=rulebook,
        catalog=catalog,
        fact_cache=_Facts(
            (
                _fact("AAA", "2021-01-04"),
                _fact("BBB", "2021-01-05", rs=82.0),
                _fact("AAA", "2024-01-02"),
            )
        ),
        partitions=fixed_partitions(),
        diagnostic_leader_labels=("AAA",),
        source_commit="d" * 40,
        source_fingerprint_sha256="e" * 64,
        strategy_identity="cached-diagnosis-v1",
        baseline_snapshot=snapshot,
        reproduced_baseline=replace(snapshot),
        baseline_reproduction=BaselineReproduction(True, (), "a" * 64, "a" * 64),
    )


def reversed_labels() -> tuple[str, ...]:
    return ("BBB",)


def test_rule_stage_funnel_is_monotone_and_missing_i_never_passes(
    diagnosis_context: DiagnosisContext,
) -> None:
    result = run_experiment(diagnosis_context, "D2.RULE_STAGE_FUNNEL", PartitionName.DISCOVERY)
    survivor_counts = [stage.survivors for stage in result.rule_attribution]
    assert survivor_counts == sorted(survivor_counts, reverse=True)
    assert result.fidelity.label is FidelityLabel.FIDELITY_INCOMPLETE
    assert "I.SPONSORSHIP" in result.fidelity.unavailable_required_rule_ids
    assert result.promotion_eligible is False


def test_d1_to_d4_require_a_verified_d0_reproduction(
    diagnosis_context: DiagnosisContext,
) -> None:
    before_d0 = replace(diagnosis_context, baseline_reproduction=None)
    with pytest.raises(ValueError, match="verified D0"):
        run_experiment(
            before_d0,
            "D2.RULE_STAGE_FUNNEL",
            PartitionName.DISCOVERY,
        )
    d0 = run_experiment(before_d0, "D0.BASELINE_REPRODUCTION", PartitionName.DISCOVERY)
    assert d0.promotion_checks["reproduction_exact"] is True
    with pytest.raises(ValueError, match="does not match"):
        before_d0.with_verified_baseline_reproduction(
            BaselineReproduction(True, (), "f" * 64, "f" * 64)
        )
    after_d0 = before_d0.with_verified_baseline_reproduction(
        BaselineReproduction(True, (), "a" * 64, "a" * 64)
    )
    assert run_experiment(after_d0, "D2.RULE_STAGE_FUNNEL", PartitionName.DISCOVERY)


def test_dispatch_rejects_a_forged_passed_d0_reproduction(
    diagnosis_context: DiagnosisContext,
) -> None:
    forged = replace(
        diagnosis_context,
        baseline_reproduction=BaselineReproduction(True, (), "f" * 64, "f" * 64),
    )
    with pytest.raises(ValueError, match="verified D0"):
        run_experiment(forged, "D2.RULE_STAGE_FUNNEL", PartitionName.DISCOVERY)


def test_current_exit_package_explains_the_known_ma_loss_cluster(
    diagnosis_context: DiagnosisContext,
) -> None:
    result = run_experiment(diagnosis_context, "D4.CURRENT_EXIT_PACKAGE", PartitionName.DISCOVERY)
    ma = result.exit_attribution.by_reason["ma_violation"]
    assert ma.closed_positions > 0
    assert ma.win_rate_pct < result.trade_statistics.win_rate_pct
    assert ma.average_completed_position_return_pct < 0.0
    assert result.performance.closed_positions == result.trade_statistics.completed_positions
    assert result.performance.total_return_pct != result.trade_statistics.mean_return_pct


def test_d4_computes_performance_from_the_actual_verified_equity_schema(
    diagnosis_context: DiagnosisContext,
) -> None:
    frame = pd.read_csv(
        diagnosis_context.baseline_snapshot.run_dir / "equity_curve.csv"
    )
    assert tuple(frame.columns) == ("date", "portfolio", "benchmark")
    holdings = pd.read_csv(
        diagnosis_context.baseline_snapshot.run_dir / "weekly_holdings.csv"
    )
    assert tuple(holdings.columns) == (
        "Week_Ending",
        "Holdings",
        "Holding_Count",
        "Cash",
        "Market_Value",
        "Total_Equity",
    )

    result = run_experiment(
        diagnosis_context,
        "D4.CURRENT_EXIT_PACKAGE",
        PartitionName.DISCOVERY,
    )

    assert result.performance is not None
    assert result.performance.total_return_pct == pytest.approx(10.0)
    assert result.performance.annualized_return_pct == pytest.approx(10.1234972764)
    assert result.performance.sharpe_ratio == pytest.approx(460.2238585732)
    assert result.performance.max_drawdown_pct == pytest.approx(0.0)
    assert result.performance.benchmark_total_return_delta_pct == pytest.approx(4.0)
    assert result.performance.benchmark_annualized_return_delta_pct == pytest.approx(
        4.0507572519
    )
    assert result.performance.average_cash_pct == pytest.approx(26.6666666667)
    assert result.promotion_checks["performance_complete"] is True
    assert result.promotion_eligible is False


@pytest.mark.parametrize("malformation", ("missing", "invalid_cash", "nonfinite_cash", "nonfinite_equity"))
def test_d4_fails_closed_when_verified_partition_cash_evidence_is_malformed(
    diagnosis_context: DiagnosisContext,
    malformation: str,
) -> None:
    path = diagnosis_context.baseline_snapshot.run_dir / "weekly_holdings.csv"
    context = diagnosis_context
    if malformation == "missing":
        path.unlink()
    else:
        cash, equity = (
            ("inf", "100") if malformation == "nonfinite_cash" else
            ("50", "inf") if malformation == "nonfinite_equity" else
            ("not-cash", "100")
        )
        path.write_text(
            "Week_Ending,Holdings,Holding_Count,Cash,Market_Value,Total_Equity\n"
            f"2021-01-04,AAA,1,{cash},50,{equity}\n",
            encoding="utf-8",
        )
        artifacts = dict(diagnosis_context.baseline_snapshot.artifact_sha256)
        artifacts["weekly_holdings.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot = replace(
            diagnosis_context.baseline_snapshot,
            artifact_sha256=artifacts,
        )
        context = replace(
            diagnosis_context,
            baseline_snapshot=snapshot,
            reproduced_baseline=replace(snapshot),
        )

    result = run_experiment(
        context,
        "D4.CURRENT_EXIT_PACKAGE",
        PartitionName.DISCOVERY,
    )

    assert result.performance is None
    assert result.promotion_checks["performance_complete"] is False
    assert result.promotion_eligible is False


def test_current_exit_rejects_a_weekly_holdings_hash_mismatch(
    diagnosis_context: DiagnosisContext,
) -> None:
    path = diagnosis_context.baseline_snapshot.run_dir / "weekly_holdings.csv"
    path.write_text(
        path.read_text(encoding="utf-8").replace("50.0,50.0", "49.0,51.0", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="weekly holdings hash"):
        run_experiment(
            diagnosis_context,
            "D4.CURRENT_EXIT_PACKAGE",
            PartitionName.DISCOVERY,
        )


def test_rs_85_variant_changes_the_materialized_entry_evidence(
    diagnosis_context: DiagnosisContext,
) -> None:
    base = run_experiment(diagnosis_context, "D2.PROPER_BASE_COUNTERFACTUAL", PartitionName.DISCOVERY)
    rs_85 = run_experiment(diagnosis_context, "D2.RS_85_CONFORMANCE", PartitionName.DISCOVERY)
    assert rs_85.entry_funnel.qualified < base.entry_funnel.qualified


def test_current_exit_attribution_is_partitioned_and_rejects_tampering(
    diagnosis_context: DiagnosisContext,
) -> None:
    validation = run_experiment(diagnosis_context, "D4.CURRENT_EXIT_PACKAGE", PartitionName.VALIDATION)
    assert validation.trade_statistics.completed_positions == 0
    path = diagnosis_context.baseline_snapshot.run_dir / "transactions.csv"
    text = path.read_text(encoding="utf-8").replace("ma_violation", "tampered_exit", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="transaction ledger hash"):
        run_experiment(diagnosis_context, "D4.CURRENT_EXIT_PACKAGE", PartitionName.DISCOVERY)


def test_current_exit_rejects_a_ledger_hash_mismatch_before_metrics(
    diagnosis_context: DiagnosisContext,
) -> None:
    path = diagnosis_context.baseline_snapshot.run_dir / "transactions.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("110.0", "110.1", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="transaction ledger hash"):
        run_experiment(diagnosis_context, "D4.CURRENT_EXIT_PACKAGE", PartitionName.DISCOVERY)


def test_remove_unverified_exits_keeps_stop_and_protective_behavior() -> None:
    dates = pd.bdate_range("2024-01-02", periods=25)
    frame = pd.DataFrame({"Open": [100.0] * 25, "High": [110.0] * 25, "Low": [99.0] * 25, "Close": [99.0] * 25}, index=dates)
    simulator = DiagnosisPortfolioSimulator(experiment_id="D4.REMOVE_UNVERIFIED_EXITS", enable_eviction=False)
    simulator._open_positions["AAA"] = Trade("AAA", "2024-01-02", 100.0, 1.0, 92.0, 90.0, 90.0, "test")
    simulator._check_exits("AAA", frame, dates[-1])
    assert "AAA" in simulator._open_positions
    assert simulator._open_positions["AAA"].stop_price >= 100.0


def test_promotion_checks_require_all_predeclared_comparisons(
    diagnosis_context: DiagnosisContext,
) -> None:
    result = run_experiment(diagnosis_context, "D3.M_CONFIRMED_UPTREND", PartitionName.VALIDATION)
    assert {"non_worse_return", "non_worse_annualized_return", "non_worse_sharpe", "non_worse_drawdown", "non_worse_pit_recall", "strict_improvement", "fidelity_complete", "reproduction_exact"} <= set(result.promotion_checks)
    assert result.promotion_eligible is False


def test_worse_partition_performance_and_recall_fail_promotion_evidence(
    diagnosis_context: DiagnosisContext,
) -> None:
    reference = PerformanceEvidence(PartitionName.DISCOVERY, 10.0, 10.0, 1.0, -1.0, 0.0, 1, 0.0, 0.0)
    recall = LeaderRecallEvidence(1, 1, 1, 100.0, ("leader:AAA",))
    context = replace(
        diagnosis_context.with_replaced_diagnostic_leader_labels(("ZZZ",)),
        partition_baseline_performance={PartitionName.DISCOVERY: reference},
        partition_baseline_recall={PartitionName.DISCOVERY: recall},
    )
    result = run_experiment(context, "D2.RULE_STAGE_FUNNEL", PartitionName.DISCOVERY)
    assert result.promotion_checks["non_worse_return"] is False
    assert result.promotion_checks["non_worse_pit_recall"] is False
    assert result.promotion_eligible is False


def test_catalog_resume_loads_completed_result_before_invoking_runner(
    diagnosis_context: DiagnosisContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = run_catalog(diagnosis_context, ("D2.RULE_STAGE_FUNNEL",), (PartitionName.DISCOVERY,), tmp_path / "checkpoints", resume=False)

    def runner_must_not_execute(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("completed experiment runner was invoked")

    monkeypatch.setattr(experiments_module, "run_experiment", runner_must_not_execute)
    resumed = run_catalog(diagnosis_context, ("D2.RULE_STAGE_FUNNEL",), (PartitionName.DISCOVERY,), tmp_path / "checkpoints", resume=True)
    assert resumed[0].result_sha256 == first[0].result_sha256


@pytest.mark.parametrize(
    ("section", "field", "replacement"),
    (
        ("performance", "total_return_pct", 1.0),
        ("leader_recall", "evidence_ids", ["leader:ZZZ"]),
        ("promotion_checks", "reproduction_exact", False),
        ("trade_statistics", "mean_return_pct", 1.0),
        ("exit_attribution", "average_completed_position_return_pct", 1.0),
    ),
)
def test_catalog_resume_rejects_tampered_full_result_evidence(
    diagnosis_context: DiagnosisContext,
    tmp_path: Path,
    section: str,
    field: str,
    replacement: object,
) -> None:
    root = tmp_path / f"checkpoints-{section}-{field}"
    first = run_catalog(
        diagnosis_context,
        ("D4.CURRENT_EXIT_PACKAGE",),
        (PartitionName.DISCOVERY,),
        root,
        resume=False,
    )[0]
    path = root / f"{first.identity_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if section == "exit_attribution":
        payload["result"][section]["ma_violation"][field] = replacement
    else:
        payload["result"][section][field] = replacement
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash|stale"):
        run_catalog(
            diagnosis_context,
            ("D4.CURRENT_EXIT_PACKAGE",),
            (PartitionName.DISCOVERY,),
            root,
            resume=True,
        )


def test_catalog_identity_changes_when_partition_promotion_evidence_changes(
    diagnosis_context: DiagnosisContext, tmp_path: Path,
) -> None:
    first_reference = PerformanceEvidence(PartitionName.DISCOVERY, -1.0, -1.0, 0.0, -1.0, 0.0, 1, 0.0, 0.0)
    second_reference = PerformanceEvidence(PartitionName.DISCOVERY, 1.0, 1.0, 0.0, -1.0, 0.0, 1, 0.0, 0.0)
    first_context = replace(diagnosis_context, partition_baseline_performance={PartitionName.DISCOVERY: first_reference})
    second_context = replace(diagnosis_context, partition_baseline_performance={PartitionName.DISCOVERY: second_reference})
    root = tmp_path / "checkpoints"
    first = run_catalog(first_context, ("D2.RULE_STAGE_FUNNEL",), (PartitionName.DISCOVERY,), root, resume=False)
    resumed = run_catalog(second_context, ("D2.RULE_STAGE_FUNNEL",), (PartitionName.DISCOVERY,), root, resume=True)
    assert resumed[0].identity_sha256 != first[0].identity_sha256


def test_ex_post_leader_labels_cannot_change_a_trade_path(
    diagnosis_context: DiagnosisContext,
) -> None:
    first = run_experiment(diagnosis_context, "D3.M_CONFIRMED_UPTREND", PartitionName.VALIDATION)
    second = run_experiment(
        diagnosis_context.with_replaced_diagnostic_leader_labels(reversed_labels()),
        "D3.M_CONFIRMED_UPTREND",
        PartitionName.VALIDATION,
    )
    assert first.trade_path_sha256 == second.trade_path_sha256
    assert first.leader_recall != second.leader_recall


def test_locked_catalog_requires_identifiers_and_isolates_checkpoint_identity(
    diagnosis_context: DiagnosisContext, tmp_path: Path,
) -> None:
    context = diagnosis_context.with_verified_baseline_reproduction(
        diagnosis_context.baseline_reproduction
    )
    with pytest.raises(ValueError, match="human selection"):
        run_locked_catalog(context, ("D2.RULE_STAGE_FUNNEL",), tmp_path, human_selection_id="", research_generation_id="generation", resume=False)
    first = run_locked_catalog(context, ("D2.RULE_STAGE_FUNNEL",), tmp_path, human_selection_id="selection-a", research_generation_id="generation", resume=False)
    second = run_locked_catalog(context, ("D2.RULE_STAGE_FUNNEL",), tmp_path, human_selection_id="selection-b", research_generation_id="generation", resume=False)
    assert first[0].identity_sha256 != second[0].identity_sha256
