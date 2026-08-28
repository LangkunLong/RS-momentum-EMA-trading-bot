from __future__ import annotations

import json
import hashlib
import os
import shutil
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

import agent_loop
import core.pit_optimization as optimization
from core.engine_policy import effective_engine_policy_sha256
from core.pit_optimization_contract import (
    CandidateDefinition,
    OptimizationWindowMetrics,
    PitOptimizationCoding,
    PitOptimizationReasoning,
    PitOptimizationRoute,
    build_comparison,
    candidate_catalog,
    validate_coding_selection,
    validate_policy_delta,
    verify_catalog_source,
)
from core.pit_optimization import (
    PitOptimizationCanaryServices,
    PitOptimizationCleanup,
    PitOptimizationGateConfig,
    PitOptimizationLoopResult,
    PitOptimizationReadiness,
    PitOptimizationRoleCall,
    aggregate_equity_window,
    aggregate_transaction_window,
    aggregate_weekly_window,
    run_pit_optimization_canary,
)


def _metrics(
    *,
    annualized: float = 2.0,
    drawdown: float = -12.0,
    total: float = 10.0,
    sharpe: float = 0.34,
    trades: int = 176,
) -> OptimizationWindowMetrics:
    return OptimizationWindowMetrics(
        total_return_pct=total,
        annualized_return_pct=annualized,
        max_drawdown_pct=drawdown,
        sharpe_ratio=sharpe,
        closed_trades=trades,
    )


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _prepare_policy() -> dict[str, object]:
    return {
        "entry_policy": {
            definition.policy_field: {
                "classification": "active_fixed_policy",
                "optimizer_candidate": True,
                "source": (
                    "core.canslim.entry_contract."
                    f"{definition.constant_name}"
                ),
                "value": definition.old_value,
            }
            for definition in candidate_catalog().values()
        },
        "causal_invariants": {"point_in_time": True, "next_session_execution": True},
        "other_policy": {"unchanged": True},
    }


def _aggregate_window(performance: dict[str, object]) -> dict[str, object]:
    closed = int(performance["closed_trades"])
    complete_performance = {
        **performance,
        "benchmark_total_return_pct": 9.0,
        "benchmark_annualized_return_pct": 1.8,
        "benchmark_max_drawdown_pct": -10.0,
        "benchmark_sharpe_ratio": 0.30,
        "excess_return_pct": float(performance["total_return_pct"]) - 9.0,
        "excess_annualized_return_pct": float(performance["annualized_return_pct"]) - 1.8,
        "starting_equity": 100000.0,
        "ending_equity": 110000.0,
        "total_pnl": 10000.0,
        "equity_observations": 100,
        "objective": float(performance["annualized_return_pct"])
        - abs(min(float(performance["max_drawdown_pct"]), 0.0)),
    }
    return {
        "performance": complete_performance,
        "trades": {
            "closed_trades": closed,
            "open_trades": 0,
            "wins": closed,
            "losses": 0,
            "win_rate_pct": 100.0 if closed else 0.0,
            "average_return_pct": 1.0 if closed else 0.0,
            "median_return_pct": 1.0 if closed else 0.0,
            "average_win_pct": 1.0 if closed else 0.0,
            "average_loss_pct": 0.0,
            "expectancy_pct": 1.0 if closed else 0.0,
            "average_holding_days": 5.0 if closed else 0.0,
            "median_holding_days": 5.0 if closed else 0.0,
            "average_holding_sessions": 3.0 if closed else 0.0,
            "median_holding_sessions": 3.0 if closed else 0.0,
            "exit_attribution": {
                "end_of_test": {
                    "closed_trades": closed,
                    "wins": closed,
                    "win_rate_pct": 100.0 if closed else 0.0,
                    "average_return_pct": 1.0 if closed else 0.0,
                }
            },
            "scale_out_attribution": {
                "sell_count": 0,
                "quantity": 0.0,
                "proceeds": 0.0,
            },
        },
        "weekly": {
            "observations": 10,
            "average_cash_pct": 75.0,
            "minimum_cash_pct": 50.0,
            "maximum_cash_pct": 100.0,
            "average_exposure_pct": 25.0,
            "average_holding_count": 1.0,
            "maximum_holding_count": 2,
        },
        "funnel": {
            "evaluated": 100,
            "technical_setup": 90,
            "current_growth_pass": 80,
            "annual_growth_pass": 70,
            "rs_pass": 60,
            "composite_pass": 50,
            "qualified": 40,
            "attempted": 40,
            "executed": 40,
            "rejected": 0,
            "outcomes": {
                "entries_executed": 40,
                "entry_rejected_already_open": 0,
                "entry_rejected_capacity": 0,
                "entry_rejected_missing_data": 0,
                "entry_rejected_invalid_price": 0,
                "entry_rejected_next_open_buy_zone": 0,
                "entry_rejected_invalid_risk": 0,
                "entry_rejected_no_cash": 0,
            },
        },
    }


def _canary_fixture(tmp_path: Path) -> tuple[
    PitOptimizationReadiness,
    Path,
    Path,
    Path,
]:
    source_root = (tmp_path / "source").resolve()
    candidate_root = (tmp_path / "candidate").resolve()
    artifact_root = (tmp_path / "artifacts").resolve()
    source_path = source_root / "core" / "canslim" / "entry_contract.py"
    source_path.parent.mkdir(parents=True)
    live_source = (
        Path(__file__).resolve().parents[1] / "core" / "canslim" / "entry_contract.py"
    ).read_bytes()
    canonical_source = live_source.replace(b"\r\n", b"\n")
    source_path.write_bytes(canonical_source.replace(b"\n", b"\r\n"))
    candidate_path = candidate_root / "core" / "canslim" / "entry_contract.py"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(canonical_source)
    catalog = candidate_catalog()
    policy_fields: dict[str, object] = {}
    for candidate in catalog.values():
        policy_fields.setdefault(
            candidate.policy_field,
            {
                "classification": "active_fixed_policy",
                "optimizer_candidate": True,
                "source": f"core.canslim.entry_contract.{candidate.constant_name}",
                "value": candidate.old_value,
            },
        )
    effective_policy = {
        "schema_version": 1,
        "entry_policy": policy_fields,
        "causal_invariants": {
            "entry_execution_timing": {"value": "next_session_open"},
            "point_in_time": {"value": True},
        },
        "exit_policy": {"stop_loss_pct": {"value": 0.07}},
    }
    effective_policy_sha256 = _canonical_digest(effective_policy)
    baseline_full = asdict(_metrics())
    baseline_holdout = asdict(_metrics(annualized=1.0, drawdown=-5.0, total=4.0, trades=20))
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    evaluation_contract = optimization._evaluation_contract(
        verification_subset=False,
        verification_scope=None,
    )
    catalog_payload = [
        {
            "candidate_id": item.candidate_id,
            "constant_name": item.constant_name,
            "policy_field": item.policy_field,
            "old_value": item.old_value,
            "new_value": item.new_value,
            "path": item.path,
            "old_line": item.old_line,
            "new_line": item.new_line,
        }
        for item in catalog.values()
    ]
    primitive: dict[str, object] = {
        "schema_version": 1,
        "gate": "pit_optimization",
        "phase": "ready",
        "identities": {
            "entry_contract_source_sha256": source_sha256,
            "pit_bundle_sha256": "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
            "baseline_manifest_sha256": "f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
            "effective_policy_sha256": effective_policy_sha256,
        },
        "budget_contract": {
            "samples": 1,
            "iterations": 1,
            "max_calls": 3,
            "max_usd": 0.50,
            "apply": False,
            "provider_retries": 0,
        },
        "evaluation_contract": evaluation_contract,
        "candidate_catalog": catalog_payload,
        "effective_policy": effective_policy,
        "baseline": {
            "full": _aggregate_window(baseline_full),
            "holdout": _aggregate_window(baseline_holdout),
            "leader_basket": {},
        },
        "prior_discovery_feedback": [],
        "evidence_ids": [
            "metric.full.cash",
            "metric.full.entry_funnel",
            "metric.full.objective",
            "metric.full.trade_quality",
            "metric.holdout.objective",
        ],
        "invariant_ids": [
            "invariant.completed_session_facts",
            "invariant.deterministic_accounting",
            "invariant.immutable_input_identity",
            "invariant.next_session_execution",
            "invariant.no_leverage",
            "invariant.point_in_time",
        ],
    }
    provider_payload = optimization._provider_payload(primitive)
    artifact_root.mkdir()
    readiness_bytes = _canonical_bytes(primitive)
    readiness_sha256 = hashlib.sha256(readiness_bytes).hexdigest()
    readiness_path = artifact_root / "readiness.json"
    readiness_path.write_bytes(readiness_bytes)
    readiness = PitOptimizationReadiness(
        readiness_sha256=readiness_sha256,
        artifact_path=readiness_path,
        artifact_sha256=readiness_sha256,
        effective_policy_sha256=effective_policy_sha256,
        provider_payload=provider_payload,
        primitive=primitive,
    )
    return readiness, source_root, candidate_root, artifact_root


def _candidate_evaluation(
    readiness: PitOptimizationReadiness,
    *,
    accepted: bool,
) -> dict[str, object]:
    policy = json.loads(json.dumps(readiness.primitive["effective_policy"]))
    policy["entry_policy"]["min_rs_score"]["value"] = 75.0
    baseline = readiness.primitive["baseline"]
    full = json.loads(json.dumps(baseline["full"]))
    holdout = json.loads(json.dumps(baseline["holdout"]))
    full["performance"]["annualized_return_pct"] = 2.30
    full["performance"]["objective"] = -9.70
    full["performance"]["excess_annualized_return_pct"] = 0.50
    if not accepted:
        holdout["performance"]["annualized_return_pct"] = 0.75
        holdout["performance"]["objective"] = -4.25
        holdout["performance"]["excess_annualized_return_pct"] = -1.05
    return {
        "schema_version": 1,
        "pit_bundle_sha256": readiness.primitive["identities"]["pit_bundle_sha256"],
        "effective_policy_sha256": _canonical_digest(policy),
        "effective_policy": policy,
        "full": full,
        "holdout": holdout,
    }


def test_catalog_is_exactly_the_twelve_approved_one_line_alternatives() -> None:
    catalog = candidate_catalog()

    assert tuple(catalog) == (
        "max_buy_zone_extension_003",
        "max_buy_zone_extension_007",
        "min_annual_growth_020",
        "min_annual_growth_030",
        "min_composite_score_065",
        "min_composite_score_075",
        "min_current_growth_020",
        "min_current_growth_030",
        "min_rs_score_075",
        "min_rs_score_085",
        "min_volume_ratio_120",
        "min_volume_ratio_140",
    )
    assert len({item.new_line for item in catalog.values()}) == 12
    assert {item.path for item in catalog.values()} == {
        "core/canslim/entry_contract.py"
    }
    assert all(isinstance(item, CandidateDefinition) for item in catalog.values())


def test_catalog_matches_the_live_entry_contract_source() -> None:
    root = Path(__file__).resolve().parents[1]

    identity = verify_catalog_source(root / "core/canslim/entry_contract.py")

    assert identity.candidate_count == 12
    assert identity.constant_count == 6
    assert len(identity.source_sha256) == 64


def test_role_payloads_are_closed_and_keep_selection_out_of_orchestrator() -> None:
    route = PitOptimizationRoute.from_json(
        json.dumps(
            {
                "action": "continue",
                "domain": "return_drawdown",
                "evidence_ids": ["metric.full.cash", "metric.full.objective"],
            }
        )
    )
    assert route.action == "continue"
    with pytest.raises(ValueError, match="keys"):
        PitOptimizationRoute.from_json(
            json.dumps(
                {
                    "action": "continue",
                    "domain": "return_drawdown",
                    "evidence_ids": ["metric.full.objective"],
                    "candidate_id": "min_rs_score_075",
                }
            )
        )

    reasoning = PitOptimizationReasoning.from_json(
        json.dumps(
            {
                "hypothesis": "A bounded RS relaxation may admit more leaders.",
                "evidence_ids": ["metric.full.cash"],
                "invariant_ids": ["invariant.point_in_time"],
                "candidate_id": "min_rs_score_075",
                "skip": False,
                "skip_reason": "",
            }
        )
    )
    assert reasoning.candidate_id == "min_rs_score_075"


def test_coder_must_reproduce_the_selected_controller_replacement() -> None:
    candidate = candidate_catalog()["min_rs_score_075"]
    coding = PitOptimizationCoding.from_json(
        json.dumps(
            {
                "summary": "Reproduce the selected controller-owned candidate.",
                "candidate_id": candidate.candidate_id,
                "replacement": {
                    "path": candidate.path,
                    "old_line": candidate.old_line,
                    "new_line": candidate.new_line,
                },
            }
        )
    )
    validate_coding_selection(coding, candidate)

    wrong = PitOptimizationCoding.from_json(
        json.dumps(
            {
                "summary": "Wrong value.",
                "candidate_id": candidate.candidate_id,
                "replacement": {
                    "path": candidate.path,
                    "old_line": candidate.old_line,
                    "new_line": "MIN_RS_SCORE = 70.0",
                },
            }
        )
    )
    with pytest.raises(ValueError, match="replacement"):
        validate_coding_selection(wrong, candidate)


def test_comparison_enforces_full_and_holdout_boundaries() -> None:
    baseline_full = _metrics()
    candidate_full = _metrics(annualized=2.25, drawdown=-12.0, total=9.5, sharpe=0.29, trades=132)
    baseline_holdout = _metrics(
        annualized=-0.6373382361634428,
        drawdown=-8.166876426108992,
        total=-0.6338570633617757,
        sharpe=-0.06435758233116282,
        trades=49,
    )
    candidate_holdout = _metrics(
        annualized=-0.6373382361634428,
        drawdown=-8.166876426108992,
        total=-1.1338570633617757,
        sharpe=-0.11435758233116282,
        trades=24,
    )

    comparison = build_comparison(
        baseline_full=baseline_full,
        candidate_full=candidate_full,
        baseline_holdout=baseline_holdout,
        candidate_holdout=candidate_holdout,
    )

    assert comparison.full_checks == {
        "objective_improvement_at_least_0_25pp": True,
        "total_return_not_worse_by_more_than_0_50pp": True,
        "drawdown_not_worse_by_more_than_0_50pp": True,
        "sharpe_not_worse_by_more_than_0_05": True,
        "closed_trades_at_least_132": True,
    }
    assert comparison.holdout_minimum_closed_trades == 24
    assert all(comparison.holdout_checks.values())
    assert comparison.accepted is True


def test_comparison_rejects_when_holdout_objective_deteriorates() -> None:
    baseline_full = _metrics()
    baseline_holdout = _metrics(annualized=4.0, drawdown=-5.0, trades=49)
    comparison = build_comparison(
        baseline_full=baseline_full,
        candidate_full=_metrics(annualized=2.3),
        baseline_holdout=baseline_holdout,
        candidate_holdout=_metrics(annualized=3.9, drawdown=-5.0, trades=49),
    )

    assert comparison.holdout_checks["objective_delta_nonnegative"] is False
    assert comparison.accepted is False


def test_policy_delta_allows_one_canonical_entry_leaf_and_no_causal_change() -> None:
    candidate = candidate_catalog()["min_current_growth_020"]
    baseline = {
        "entry_policy": {
            "min_current_growth": {
                "classification": "active_fixed_policy",
                "optimizer_candidate": True,
                "source": "core.canslim.entry_contract.MIN_CURRENT_GROWTH",
                "value": 0.25,
            },
            "min_rs_score": {
                "classification": "active_fixed_policy",
                "optimizer_candidate": True,
                "source": "core.canslim.entry_contract.MIN_RS_SCORE",
                "value": 80.0,
            },
        },
        "causal_invariants": {"entry_execution_timing": {"value": "next_session_open"}},
    }
    changed = json.loads(json.dumps(baseline))
    changed["entry_policy"]["min_current_growth"]["value"] = 0.20

    delta = validate_policy_delta(baseline, changed, candidate)

    assert delta.changed_leaf == "entry_policy.min_current_growth.value"
    assert delta.old_value == 0.25
    assert delta.new_value == 0.20

    changed["causal_invariants"]["entry_execution_timing"]["value"] = "same_session"
    with pytest.raises(ValueError, match="causal"):
        validate_policy_delta(baseline, changed, candidate)


def test_gate_config_is_fixed_to_one_inert_three_call_canary(tmp_path: Path) -> None:
    config = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=(tmp_path / "baseline").resolve(),
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=(tmp_path / "pit.sqlite3").resolve(),
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256="d" * 64,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )
    assert config.phase == "prepare"

    with pytest.raises(ValueError, match="three calls"):
        PitOptimizationGateConfig(
            **{**asdict(config), "max_api_calls": 2}
        )
    with pytest.raises(ValueError, match="apply=false"):
        PitOptimizationGateConfig(
            **{**asdict(config), "apply": True}
        )


def test_equity_window_exposes_strategy_benchmark_and_objective_metrics() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
            "portfolio": [100_000.0, 110_000.0, 105_000.0],
            "benchmark": [100_000.0, 102_000.0, 103_000.0],
        }
    )

    metrics = aggregate_equity_window(
        frame, start_date="2025-01-01", end_date="2025-12-31", closed_trades=2
    )

    assert metrics["total_return_pct"] == pytest.approx(5.0)
    assert metrics["max_drawdown_pct"] == pytest.approx(-4.545454545454541)
    assert metrics["benchmark_total_return_pct"] == pytest.approx(3.0)
    assert metrics["excess_return_pct"] == pytest.approx(2.0)
    assert metrics["ending_equity"] == 105_000.0
    assert metrics["total_pnl"] == 5_000.0
    assert metrics["closed_trades"] == 2


def test_transaction_window_reconstructs_scale_out_lots_and_terminal_exits() -> None:
    transactions = pd.DataFrame(
        [
            {"Date": "2025-01-02", "Ticker": "AAA", "Action": "BUY", "Price": 100.0, "Quantity": 10.0, "Reason": "Volume Breakout"},
            {"Date": "2025-01-03", "Ticker": "AAA", "Action": "SELL", "Price": 110.0, "Quantity": 2.5, "Reason": "take_profit_scale_out"},
            {"Date": "2025-01-06", "Ticker": "AAA", "Action": "SELL", "Price": 90.0, "Quantity": 7.5, "Reason": "stop_loss"},
        ]
    )
    sessions = pd.DatetimeIndex(pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]))

    metrics = aggregate_transaction_window(
        transactions,
        sessions=sessions,
        start_date="2025-01-01",
        end_date="2025-12-31",
    )

    assert metrics["closed_trades"] == 1
    assert metrics["open_trades"] == 0
    assert metrics["average_return_pct"] == pytest.approx(-5.0)
    assert metrics["exit_attribution"]["stop_loss"]["closed_trades"] == 1
    assert metrics["scale_out_attribution"] == {
        "sell_count": 1,
        "quantity": 2.5,
        "proceeds": 275.0,
    }


def test_weekly_window_exposes_cash_exposure_and_holdings() -> None:
    weekly = pd.DataFrame(
        {
            "Week_Ending": ["2025-01-03", "2025-01-10"],
            "Cash": [75_000.0, 50_000.0],
            "Market_Value": [25_000.0, 50_000.0],
            "Total_Equity": [100_000.0, 100_000.0],
            "Holding_Count": [2, 4],
        }
    )

    metrics = aggregate_weekly_window(
        weekly, start_date="2025-01-01", end_date="2025-12-31"
    )

    assert metrics == {
        "observations": 2,
        "average_cash_pct": 62.5,
        "minimum_cash_pct": 50.0,
        "maximum_cash_pct": 75.0,
        "average_exposure_pct": 37.5,
        "average_holding_count": 3.0,
        "maximum_holding_count": 4,
    }


def test_agent_loop_builds_a_separate_prepare_gate_without_legacy_options(
    tmp_path: Path,
) -> None:
    values = {
        "source": (tmp_path / "source").resolve(),
        "runtime": (tmp_path / "runtime").resolve(),
        "git": (tmp_path / "bin" / "git.exe").resolve(),
        "controller": (tmp_path / "controller").resolve(),
        "artifacts": (tmp_path / "artifacts").resolve(),
        "docker": (tmp_path / "bin" / "docker.exe").resolve(),
        "baseline": (tmp_path / "baseline").resolve(),
        "bundle": (tmp_path / "pit.sqlite3").resolve(),
    }
    namespace = agent_loop.build_parser().parse_args(
        [
            "--repo-root", str(values["source"]),
            "--permanent-runtime-root", str(values["runtime"]),
            "--git-executable", str(values["git"]),
            "--controller-temp-parent", str(values["controller"]),
            "--artifact-root", str(values["artifacts"]),
            "--docker-executable", str(values["docker"]),
            "--sandbox-image", "localhost/rs-agent-loop@sha256:" + "a" * 64,
            "--gate", "pit_optimization",
            "--optimization-phase", "prepare",
            "--baseline-run", str(values["baseline"]),
            "--baseline-manifest-sha256", "f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
            "--pit-bundle", str(values["bundle"]),
            "--pit-bundle-sha256", "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
            "--effective-policy-sha256", "d" * 64,
            "--max-usd", "0.50",
            "--max-api-calls", "3",
            "--max-iterations", "1",
        ]
    )

    config, _, _ = agent_loop._build_cli_config(namespace)

    assert isinstance(config.gate, PitOptimizationGateConfig)
    assert config.gate.phase == "prepare"
    assert config.limits.max_api_calls == 3
    assert config.mode.apply is False


def test_prepare_summary_is_closed_and_reports_zero_paid_calls(tmp_path: Path) -> None:
    artifact = (tmp_path / "readiness.json").resolve()
    result = PitOptimizationLoopResult(
        phase="prepare",
        status="ready",
        exit_code=0,
        run_id="pit-opt-20260826T120000Z-abcdef123456",
        readiness_sha256="a" * 64,
        effective_policy_sha256="b" * 64,
        selected_candidate_id=None,
        accepted=None,
        artifact_paths=((artifact, "a" * 64),),
        provider_calls=0,
        spent_usd=0.0,
        source_modified=False,
        cleanup_complete=True,
    )

    summary = agent_loop._pit_optimization_summary(result)

    assert summary["phase"] == "prepare"
    assert summary["status"] == "ready"
    assert summary["provider_calls"] == 0
    assert summary["spent_usd"] == 0.0
    assert summary["source_modified"] is False


def test_prepare_output_contains_canonical_readiness_and_exact_canary_command(
    tmp_path: Path,
) -> None:
    readiness, source_root, _candidate_root, artifact_root = _canary_fixture(tmp_path)
    gate = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=(tmp_path / "baseline").resolve(),
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=(tmp_path / "pit.sqlite3").resolve(),
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256=readiness.effective_policy_sha256,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )
    config = agent_loop.LoopConfig(
        source_root=source_root,
        permanent_runtime_root=(tmp_path / "runtime").resolve(),
        git_executable=(tmp_path / "git.exe").resolve(),
        controller_temp_parent=(tmp_path / "controller").resolve(),
        artifact_root=artifact_root,
        mode=agent_loop.ExecutionMode(),
        gate=gate,
        models=agent_loop.ModelConfig(),
        limits=agent_loop.LoopLimits(max_usd=0.50, max_api_calls=3),
    )

    lines = agent_loop._pit_optimization_prepare_lines(
        config,
        docker_executable=(tmp_path / "docker.exe").resolve(),
        sandbox_image="localhost/rs-agent-loop@sha256:" + "a" * 64,
        readiness=readiness,
    )

    assert len(lines) == 2
    assert lines[0].startswith("PIT_OPTIMIZATION_READY=")
    ready = json.loads(lines[0].split("=", 1)[1])
    assert ready == {
        "canary_command": lines[1].split("=", 1)[1],
        "effective_policy_sha256": readiness.effective_policy_sha256,
        "readiness_artifact": str(readiness.artifact_path),
        "readiness_sha256": readiness.readiness_sha256,
    }
    command = ready["canary_command"]
    assert "--optimization-phase canary" in command
    assert f"--readiness-sha256 {readiness.readiness_sha256}" in command
    assert "--max-api-calls 3" in command
    assert "--max-usd 0.50" in command
    assert "--apply" not in command


@pytest.mark.parametrize(
    ("candidate_accepted", "expected_status"),
    [(True, "accepted"), (False, "rejected")],
)
def test_canary_runs_three_closed_roles_once_and_returns_a_clean_terminal_result(
    tmp_path: Path,
    candidate_accepted: bool,
    expected_status: str,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    source_before = (source_root / "core/canslim/entry_contract.py").read_bytes()
    candidate = candidate_catalog()["min_rs_score_075"]
    responses = {
        "orchestrator": {
            "action": "continue",
            "domain": "return_drawdown",
            "evidence_ids": ["metric.full.objective", "metric.holdout.objective"],
        },
        "reasoner": {
            "hypothesis": "A bounded RS relaxation may improve the observed objective.",
            "evidence_ids": ["metric.full.objective", "metric.holdout.objective"],
            "invariant_ids": [
                "invariant.next_session_execution",
                "invariant.point_in_time",
            ],
            "candidate_id": candidate.candidate_id,
            "skip": False,
            "skip_reason": "",
        },
        "coder": {
            "summary": "Reproduce the controller-owned replacement.",
            "candidate_id": candidate.candidate_id,
            "replacement": {
                "path": candidate.path,
                "old_line": candidate.old_line,
                "new_line": candidate.new_line,
            },
        },
    }
    calls: list[tuple[str, dict[str, object]]] = []
    evaluations: list[Path] = []
    input_checks: list[int] = []

    def call_role(role: str, dynamic: dict[str, object], parser: object) -> PitOptimizationRoleCall:
        calls.append((role, dynamic))
        payload = parser(json.dumps(responses[role]))
        return PitOptimizationRoleCall(
            role=role,
            call_index=len(calls),
            payload=payload,
            cost_usd=0.02,
            accounting_complete=True,
            audit_sha256=hashlib.sha256(role.encode()).hexdigest(),
        )

    def evaluate(candidate_path: Path) -> dict[str, object]:
        evaluations.append(candidate_path)
        assert (candidate_path / candidate.path).read_text(encoding="utf-8").count(
            candidate.new_line
        ) == 1
        return _candidate_evaluation(readiness, accepted=candidate_accepted)

    def verify_inputs() -> None:
        input_checks.append(len(calls))

    def cleanup() -> PitOptimizationCleanup:
        shutil.rmtree(candidate_root)
        return PitOptimizationCleanup(source_modified=False, cleanup_complete=True)

    result = run_pit_optimization_canary(
        readiness=readiness,
        expected_readiness_sha256=readiness.readiness_sha256,
        expected_effective_policy_sha256=readiness.effective_policy_sha256,
        source_root=source_root,
        candidate_root=candidate_root,
        artifact_root=artifact_root,
        run_id="pit-opt-test",
        services=PitOptimizationCanaryServices(
            call_role=call_role,
            evaluate_candidate=evaluate,
            verify_inputs=verify_inputs,
            cleanup=cleanup,
        ),
    )

    assert [role for role, _ in calls] == ["orchestrator", "reasoner", "coder"]
    assert "replacement" not in calls[0][1]
    assert calls[1][1]["route"]["domain"] == "return_drawdown"
    assert calls[2][1]["candidate"] == {
        "candidate_id": candidate.candidate_id,
        "path": candidate.path,
        "old_line": candidate.old_line,
        "new_line": candidate.new_line,
    }
    assert len(evaluations) == 1
    assert len(input_checks) >= 4
    assert result.status == expected_status
    assert result.accepted is candidate_accepted
    assert result.provider_calls == 3
    assert result.spent_usd == pytest.approx(0.06)
    assert result.selected_candidate_id == candidate.candidate_id
    assert result.source_modified is False
    assert result.cleanup_complete is True
    assert (source_root / candidate.path).read_bytes() == source_before
    assert not candidate_root.exists()
    assert len(result.artifact_paths) == 4
    assert any(path.name == "baseline.json" for path, _ in result.artifact_paths)
    assert all(path.is_file() for path, _ in result.artifact_paths)


@pytest.mark.parametrize(
    ("broken_role", "response", "match"),
    [
        (
            "orchestrator",
            {
                "action": "continue",
                "domain": "cash_exposure",
                "evidence_ids": ["metric.full.objective"],
            },
            "domain evidence",
        ),
        (
            "reasoner",
            {
                "hypothesis": "Use an unsupported invariant.",
                "evidence_ids": ["metric.full.objective"],
                "invariant_ids": ["invariant.same_session_execution"],
                "candidate_id": "min_rs_score_075",
                "skip": False,
                "skip_reason": "",
            },
            "invariant",
        ),
        (
            "coder",
            {
                "summary": "Alter the controller selection.",
                "candidate_id": "min_rs_score_075",
                "replacement": {
                    "path": "core/canslim/entry_contract.py",
                    "old_line": "MIN_RS_SCORE = 80.0",
                    "new_line": "MIN_RS_SCORE = 70.0",
                },
            },
            "replacement",
        ),
    ],
)
def test_canary_rejects_open_citations_or_a_changed_coder_replacement_without_retry(
    tmp_path: Path,
    broken_role: str,
    response: dict[str, object],
    match: str,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    candidate = candidate_catalog()["min_rs_score_075"]
    valid = {
        "orchestrator": {
            "action": "continue",
            "domain": "return_drawdown",
            "evidence_ids": [
                "metric.full.objective",
                "metric.holdout.objective",
            ],
        },
        "reasoner": {
            "hypothesis": "Try one bounded controller candidate.",
            "evidence_ids": ["metric.full.objective"],
            "invariant_ids": ["invariant.point_in_time"],
            "candidate_id": candidate.candidate_id,
            "skip": False,
            "skip_reason": "",
        },
        "coder": {
            "summary": "Reproduce the controller candidate.",
            "candidate_id": candidate.candidate_id,
            "replacement": {
                "path": candidate.path,
                "old_line": candidate.old_line,
                "new_line": candidate.new_line,
            },
        },
    }
    valid[broken_role] = response
    roles: list[str] = []
    evaluations = 0

    def call_role(role: str, dynamic: dict[str, object], parser: object) -> PitOptimizationRoleCall:
        roles.append(role)
        return PitOptimizationRoleCall(
            role=role,
            call_index=len(roles),
            payload=parser(json.dumps(valid[role])),
            cost_usd=0.01,
            accounting_complete=True,
            audit_sha256=hashlib.sha256(role.encode()).hexdigest(),
        )

    def evaluate(_candidate_path: Path) -> dict[str, object]:
        nonlocal evaluations
        evaluations += 1
        return _candidate_evaluation(readiness, accepted=True)

    def cleanup() -> PitOptimizationCleanup:
        shutil.rmtree(candidate_root)
        return PitOptimizationCleanup(source_modified=False, cleanup_complete=True)

    with pytest.raises(ValueError, match=match):
        run_pit_optimization_canary(
            readiness=readiness,
            expected_readiness_sha256=readiness.readiness_sha256,
            expected_effective_policy_sha256=readiness.effective_policy_sha256,
            source_root=source_root,
            candidate_root=candidate_root,
            artifact_root=artifact_root,
            run_id="pit-opt-invalid",
            services=PitOptimizationCanaryServices(
                call_role=call_role,
                evaluate_candidate=evaluate,
                verify_inputs=lambda: None,
                cleanup=cleanup,
            ),
        )

    expected_calls = {"orchestrator": 1, "reasoner": 2, "coder": 3}[broken_role]
    assert len(roles) == expected_calls
    assert evaluations == 0
    assert not candidate_root.exists()


def test_canary_rejects_readiness_or_sealed_input_mutation_before_another_call(
    tmp_path: Path,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    roles: list[str] = []
    sealed_input_mutated = False

    def call_role(role: str, dynamic: dict[str, object], parser: object) -> PitOptimizationRoleCall:
        nonlocal sealed_input_mutated
        roles.append(role)
        sealed_input_mutated = True
        readiness.artifact_path.write_text("{}\n", encoding="utf-8")
        payload = parser(
            json.dumps(
                {
                    "action": "continue",
                    "domain": "return_drawdown",
                    "evidence_ids": [
                        "metric.full.objective",
                        "metric.holdout.objective",
                    ],
                }
            )
        )
        return PitOptimizationRoleCall(
            role=role,
            call_index=1,
            payload=payload,
            cost_usd=0.01,
            accounting_complete=True,
            audit_sha256="a" * 64,
        )

    def verify_inputs() -> None:
        if sealed_input_mutated:
            raise ValueError("sealed optimization input changed")

    def cleanup() -> PitOptimizationCleanup:
        shutil.rmtree(candidate_root)
        return PitOptimizationCleanup(source_modified=False, cleanup_complete=True)

    with pytest.raises(ValueError, match="(readiness artifact|sealed optimization input)"):
        run_pit_optimization_canary(
            readiness=readiness,
            expected_readiness_sha256=readiness.readiness_sha256,
            expected_effective_policy_sha256=readiness.effective_policy_sha256,
            source_root=source_root,
            candidate_root=candidate_root,
            artifact_root=artifact_root,
            run_id="pit-opt-mutated",
            services=PitOptimizationCanaryServices(
                call_role=call_role,
                evaluate_candidate=lambda _path: _candidate_evaluation(readiness, accepted=True),
                verify_inputs=verify_inputs,
                cleanup=cleanup,
            ),
        )

    assert roles == ["orchestrator"]
    assert not candidate_root.exists()


def test_canary_requires_complete_monotonic_three_call_accounting_without_retry(
    tmp_path: Path,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    roles: list[str] = []
    candidate = candidate_catalog()["min_rs_score_075"]
    responses = (
        {
            "action": "continue",
            "domain": "return_drawdown",
            "evidence_ids": [
                "metric.full.objective",
                "metric.holdout.objective",
            ],
        },
        {
            "hypothesis": "Try one bounded controller candidate.",
            "evidence_ids": ["metric.full.objective"],
            "invariant_ids": ["invariant.point_in_time"],
            "candidate_id": candidate.candidate_id,
            "skip": False,
            "skip_reason": "",
        },
    )

    def call_role(role: str, dynamic: dict[str, object], parser: object) -> PitOptimizationRoleCall:
        roles.append(role)
        return PitOptimizationRoleCall(
            role=role,
            call_index=1,
            payload=parser(json.dumps(responses[len(roles) - 1])),
            cost_usd=0.01,
            accounting_complete=True,
            audit_sha256=hashlib.sha256(role.encode()).hexdigest(),
        )

    def cleanup() -> PitOptimizationCleanup:
        shutil.rmtree(candidate_root)
        return PitOptimizationCleanup(source_modified=False, cleanup_complete=True)

    with pytest.raises(ValueError, match="call accounting"):
        run_pit_optimization_canary(
            readiness=readiness,
            expected_readiness_sha256=readiness.readiness_sha256,
            expected_effective_policy_sha256=readiness.effective_policy_sha256,
            source_root=source_root,
            candidate_root=candidate_root,
            artifact_root=artifact_root,
            run_id="pit-opt-accounting",
            services=PitOptimizationCanaryServices(
                call_role=call_role,
                evaluate_candidate=lambda _path: _candidate_evaluation(readiness, accepted=True),
                verify_inputs=lambda: None,
                cleanup=cleanup,
            ),
        )

    assert roles == ["orchestrator", "reasoner"]
    assert not candidate_root.exists()


def test_gateway_uses_the_isolated_one_attempt_optimizer_prompt_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = agent_loop.OpenRouterGateway(
        client=object(),
        ledger=agent_loop.BudgetLedger(max_usd=0.50, max_calls=3),
        pricing_loader=lambda _model: {"prompt": 1.0, "completion": 1.0},
        max_attempts=1,
    )
    sentinel = object()
    captured: dict[str, object] = {}

    def request_attempt(
        role: str,
        dynamic: str,
        parser: object,
        **kwargs: object,
    ) -> object:
        captured.update(
            {"role": role, "dynamic": dynamic, "parser": parser, **kwargs}
        )
        return sentinel

    monkeypatch.setattr(gateway, "_request_attempt", request_attempt)

    result = gateway.request_pit_optimization_once(
        "orchestrator", "{}", PitOptimizationRoute.from_json
    )

    assert result is sentinel
    assert captured["role"] == "orchestrator"
    assert captured["dynamic"] == "<dynamic-input>\n{}\n</dynamic-input>"
    assert captured["require_complete_accounting"] is True
    assert captured["system_prompts"] is not agent_loop.OpenRouterGateway.SYSTEM_PROMPTS
    assert captured["response_format"]["json_schema"]["name"] == (
        "pit_optimization_orchestrator_v1"
    )


def test_candidate_evaluator_uses_the_existing_network_none_sandbox_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_root = (tmp_path / "candidate").resolve()
    candidate_root.mkdir()
    bundle = (tmp_path / "pit.sqlite3").resolve()
    bundle.write_bytes(b"sealed fixture")
    gate = PitOptimizationGateConfig(
        phase="canary",
        baseline_run=(tmp_path / "baseline").resolve(),
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=bundle,
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256="d" * 64,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
        readiness_sha256="e" * 64,
    )
    candidate = object()
    captured: dict[str, object] = {}
    result_payload = {"schema_version": 1, "aggregate": True}

    class FakeObservation:
        returncode = 0
        timed_out = False
        stdout = ""
        stderr = ""

    class FakeSandbox:
        def run_worker(
            self,
            layout: agent_loop.WorkerLayout,
            argv: tuple[str, ...],
            environment: dict[str, str],
        ) -> FakeObservation:
            captured.update({"argv": argv, "environment": environment})
            (layout.output / "pit-optimization-result.json").write_bytes(
                _canonical_bytes(result_payload)
            )
            return FakeObservation()

    def fake_worker_boundary(
        supplied_candidate: object,
        setup: object,
        runner: object,
    ) -> object:
        assert supplied_candidate is candidate
        layout = agent_loop._make_worker_layout(tmp_path / "worker")
        setup(layout)
        return runner(layout)

    monkeypatch.setattr(agent_loop, "_run_pit_worker_with_setup", fake_worker_boundary)
    def fake_stage_file(source: Path, destination: Path, expected: str) -> None:
        assert source == bundle
        assert expected == gate.pit_bundle_sha256
        shutil.copyfile(source, destination)

    monkeypatch.setattr(agent_loop, "_pit_stage_file", fake_stage_file)
    monkeypatch.setattr(
        agent_loop,
        "_pit_observation_payload",
        lambda _sandbox, _observation: {
            "gate_observation": True,
            "network_disabled": True,
            "read_only": True,
            "worker_confined": True,
        },
    )

    evaluator = agent_loop._pit_optimization_sandbox_evaluator(
        FakeSandbox(), gate, candidate
    )
    result = evaluator(candidate_root)

    assert result == result_payload
    assert captured["argv"] == (
        "-m",
        "core.pit_optimization",
        "--worker-evaluate",
        "--pit-bundle",
        "/workspace/data/pit-bundle.sqlite3",
        "--pit-bundle-sha256",
        gate.pit_bundle_sha256,
        "--output",
        "/workspace/output/pit-optimization-result.json",
    )
    environment = captured["environment"]
    assert "OPENROUTER_API_KEY" not in environment
    assert environment["FMP_DAILY_REQUEST_BUDGET"] == "0"


def test_optimizer_canary_has_a_separate_closed_audit_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "source": (tmp_path / "source").resolve(),
        "runtime": (tmp_path / "runtime").resolve(),
        "git": (tmp_path / "bin" / "git.exe").resolve(),
        "controller": (tmp_path / "controller").resolve(),
        "artifacts": (tmp_path / "artifacts").resolve(),
        "docker": (tmp_path / "bin" / "docker.exe").resolve(),
        "baseline": (tmp_path / "baseline").resolve(),
        "bundle": (tmp_path / "pit.sqlite3").resolve(),
    }
    namespace = agent_loop.build_parser().parse_args(
        [
            "--repo-root", str(values["source"]),
            "--permanent-runtime-root", str(values["runtime"]),
            "--git-executable", str(values["git"]),
            "--controller-temp-parent", str(values["controller"]),
            "--artifact-root", str(values["artifacts"]),
            "--docker-executable", str(values["docker"]),
            "--sandbox-image", "localhost/rs-agent-loop@sha256:" + "a" * 64,
            "--gate", "pit_optimization",
            "--optimization-phase", "canary",
            "--baseline-run", str(values["baseline"]),
            "--baseline-manifest-sha256", "f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
            "--pit-bundle", str(values["bundle"]),
            "--pit-bundle-sha256", "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
            "--effective-policy-sha256", "d" * 64,
            "--readiness-sha256", "e" * 64,
            "--max-usd", "0.50",
            "--max-api-calls", "3",
            "--max-iterations", "1",
        ]
    )
    config, _, _ = agent_loop._build_cli_config(namespace)
    audit = agent_loop.AuditTrail(values["artifacts"], "pit-opt-manifest")
    captured: dict[str, object] = {}

    def capture_manifest(path: Path, value: object) -> Path:
        captured.update(value)
        return path

    monkeypatch.setattr(audit, "_write_json", capture_manifest)

    audit.write_manifest(
        config,
        source_head="a" * 40,
        source_fingerprint_sha256="b" * 64,
    )

    assert captured["gate"] == {
        "kind": "pit_optimization",
        "phase": "canary",
        "baseline_run": values["baseline"],
        "baseline_manifest_sha256": "f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        "pit_bundle": values["bundle"],
        "pit_bundle_sha256": "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        "effective_policy_sha256": "d" * 64,
        "readiness_sha256": "e" * 64,
        "max_usd": 0.5,
        "max_api_calls": 3,
        "max_iterations": 1,
        "verification_subset": False,
        "prior_discovery_feedback": None,
        "prior_discovery_feedback_sha256": None,
        "apply": False,
    }


def test_cli_routes_optimizer_canary_through_the_network_none_docker_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    controller_root = (tmp_path / "controller").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    gate = PitOptimizationGateConfig(
        phase="canary",
        baseline_run=(tmp_path / "baseline").resolve(),
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=(tmp_path / "pit.sqlite3").resolve(),
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256=readiness.effective_policy_sha256,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
        readiness_sha256=readiness.readiness_sha256,
    )
    config = agent_loop.LoopConfig(
        source_root=source_root,
        permanent_runtime_root=runtime_root,
        git_executable=(tmp_path / "git.exe").resolve(),
        controller_temp_parent=controller_root,
        artifact_root=artifact_root,
        mode=agent_loop.ExecutionMode(),
        gate=gate,
        models=agent_loop.ModelConfig(),
        limits=agent_loop.LoopLimits(max_usd=0.50, max_api_calls=3),
    )
    fingerprint = agent_loop.SourceFingerprint(
        head="a" * 40,
        branch="codex/pit-optimization-cycle",
        index_sha256="b" * 64,
        tracked_manifest_sha256="c" * 64,
        untracked_names=(),
        sha256="d" * 64,
    )
    state = agent_loop.SourceState(
        source_root,
        fingerprint.head,
        fingerprint.branch,
        "",
        (tmp_path / "agent-loop.lock").resolve(),
        fingerprint=fingerprint,
        controller_temp_parent=controller_root,
    )
    capability = object()
    candidate = agent_loop.Candidate(
        candidate_root,
        state.head,
        ("core/canslim/entry_contract.py",),
        capability,
        controller_root,
        (source_root,),
    )
    cleanup_calls = 0
    docker_calls = 0

    class FakeGateway:
        api_key = None

        def __init__(self, **kwargs: object) -> None:
            self.ledger = kwargs["ledger"]

    class FakeAudit:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.run_root = artifact_root

        def write_manifest(self, *_args: object, **_kwargs: object) -> None:
            return None

    def cleanup_resources(
        source_state: agent_loop.SourceState,
        disposable: agent_loop.Candidate,
        *,
        retain_candidate: bool,
    ) -> agent_loop.CleanupObservation:
        nonlocal cleanup_calls
        assert source_state is state
        assert disposable is candidate
        assert retain_candidate is False
        cleanup_calls += 1
        return agent_loop.CleanupObservation(True, False, False, True, True, ())

    def fake_controller(**kwargs: object) -> PitOptimizationLoopResult:
        assert kwargs["readiness"] is readiness
        assert kwargs["candidate_root"] == candidate_root
        services = kwargs["services"]
        cleanup = services.cleanup()
        assert cleanup.cleanup_complete is True
        artifact_paths = tuple(
            ((artifact_root / name).resolve(), hashlib.sha256(name.encode()).hexdigest())
            for name in ("baseline.json", "candidate.json", "comparison.json", "candidate.diff")
        )
        return PitOptimizationLoopResult(
            phase="canary",
            status="accepted",
            exit_code=0,
            run_id="pit-opt-route",
            readiness_sha256=readiness.readiness_sha256,
            effective_policy_sha256=readiness.effective_policy_sha256,
            selected_candidate_id="min_rs_score_075",
            accepted=True,
            artifact_paths=artifact_paths,
            provider_calls=3,
            spent_usd=0.03,
            source_modified=False,
            cleanup_complete=True,
        )

    monkeypatch.setattr(agent_loop, "configure_git_executable", lambda _path: object())
    monkeypatch.setattr(agent_loop, "preflight_source", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(agent_loop, "export_candidate", lambda _state: candidate)
    monkeypatch.setattr(agent_loop, "cleanup_run_resources", cleanup_resources)
    monkeypatch.setattr(agent_loop, "OpenRouterGateway", FakeGateway)
    monkeypatch.setattr(agent_loop, "AuditTrail", FakeAudit)
    monkeypatch.setattr(optimization, "prepare_pit_optimization", lambda *_args, **_kwargs: readiness)
    monkeypatch.setattr(optimization, "run_pit_optimization_canary", fake_controller)

    def configure_docker(*_args: object, **_kwargs: object) -> object:
        nonlocal docker_calls
        docker_calls += 1
        return object()

    monkeypatch.setattr(agent_loop, "configure_docker_executable", configure_docker)
    monkeypatch.setattr(agent_loop, "SandboxRunner", lambda **_kwargs: object())
    monkeypatch.setattr(
        agent_loop,
        "_pit_optimization_sandbox_evaluator",
        lambda *_args, **_kwargs: lambda _root: {},
    )
    monkeypatch.setattr(agent_loop, "_candidate_tracked_manifest_sha256", lambda _c: "f" * 64)
    monkeypatch.setattr(
        agent_loop,
        "_git",
        lambda *_args, **_kwargs: type("GitResult", (), {"stdout": b""})(),
    )

    result = agent_loop._execute_cli_run(
        config,
        docker_executable=(tmp_path / "docker.exe").resolve(),
        sandbox_image="localhost/rs-agent-loop@sha256:" + "a" * 64,
        run_id="pit-opt-route",
    )

    assert result.status == "accepted"
    assert cleanup_calls == 1
    assert docker_calls == 1


@pytest.mark.parametrize("skip_reason", ["No candidate is compelling.", "Stop."])
def test_reasoner_cannot_skip_the_exact_candidate_cycle(skip_reason: str) -> None:
    with pytest.raises(ValueError, match="must choose exactly one"):
        PitOptimizationReasoning.from_json(
            json.dumps(
                {
                    "hypothesis": "Skip this cycle.",
                    "evidence_ids": ["metric.full.objective"],
                    "invariant_ids": ["invariant.point_in_time"],
                    "candidate_id": "",
                    "skip": True,
                    "skip_reason": skip_reason,
                }
            )
        )


def test_terminal_success_requires_exactly_three_calls_and_complete_artifacts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exactly three"):
        PitOptimizationLoopResult(
            phase="canary",
            status="accepted",
            exit_code=0,
            run_id="pit-opt-invalid-result",
            readiness_sha256="a" * 64,
            effective_policy_sha256="b" * 64,
            selected_candidate_id="min_rs_score_075",
            accepted=True,
            artifact_paths=((tmp_path.resolve() / "one.json", "c" * 64),),
            provider_calls=2,
            spent_usd=0.01,
            source_modified=False,
            cleanup_complete=True,
        )


def test_candidate_evaluation_rejects_raw_or_extra_nested_fields(tmp_path: Path) -> None:
    readiness, _source_root, _candidate_root, _artifact_root = _canary_fixture(tmp_path)
    evaluation = _candidate_evaluation(readiness, accepted=True)
    evaluation["full"]["ticker"] = "NVDA"

    with pytest.raises(ValueError, match="aggregate schema"):
        optimization._candidate_comparison(
            readiness,
            evaluation,
            candidate_catalog()["min_rs_score_075"],
        )


@pytest.mark.parametrize("mutation", ["missing_trades", "count_mismatch", "raw_outcome"])
def test_candidate_evaluation_requires_exact_consistent_aggregate_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    readiness, _source_root, _candidate_root, _artifact_root = _canary_fixture(tmp_path)
    evaluation = _candidate_evaluation(readiness, accepted=True)
    if mutation == "missing_trades":
        del evaluation["full"]["trades"]
    elif mutation == "count_mismatch":
        evaluation["full"]["trades"]["wins"] -= 1
    else:
        evaluation["full"]["funnel"]["outcomes"]["ticker_NVDA"] = 1

    with pytest.raises(ValueError, match="aggregate schema"):
        optimization._candidate_comparison(
            readiness,
            evaluation,
            candidate_catalog()["min_rs_score_075"],
        )


def test_regular_optimizer_inputs_reject_hard_links(tmp_path: Path) -> None:
    original = tmp_path / "sealed.json"
    linked = tmp_path / "linked.json"
    original.write_text("{}\n", encoding="utf-8")
    os.link(original, linked)

    with pytest.raises(ValueError, match="single-link"):
        optimization._regular_file(linked, "sealed input")


def test_canary_artifact_publication_rolls_back_as_one_staged_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness, _source_root, _candidate_root, artifact_root = _canary_fixture(tmp_path)
    evaluation = _candidate_evaluation(readiness, accepted=True)
    comparison = optimization._candidate_comparison(
        readiness,
        evaluation,
        candidate_catalog()["min_rs_score_075"],
    )

    monkeypatch.setattr(
        optimization,
        "_publish_staged_directory",
        lambda *_args: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(ValueError, match="publish atomically"):
        optimization._write_canary_artifacts(
            artifact_root,
            run_id="atomic-failure",
            candidate_id="min_rs_score_075",
            baseline=readiness.primitive["baseline"],
            evaluation=evaluation,
            comparison=comparison,
            diff="--- a/core/canslim/entry_contract.py\n+++ b/core/canslim/entry_contract.py\n",
            call_sha256s=["a" * 64, "b" * 64, "c" * 64],
        )
    assert not tuple(artifact_root.glob("pit-optimization-atomic-failure*"))
    assert not tuple(artifact_root.glob("pit-optimization-stage-*"))


def test_policy_delta_rejects_changes_outside_the_selected_catalog_semantics() -> None:
    candidate = candidate_catalog()["min_rs_score_075"]
    baseline = {
        "schema_version": 1,
        "entry_policy": {
            candidate.policy_field: {
                "classification": "active_fixed_policy",
                "optimizer_candidate": True,
                "source": f"core.canslim.entry_contract.{candidate.constant_name}",
                "value": candidate.old_value,
            }
        },
        "causal_invariants": {"point_in_time": {"value": True}},
        "exit_policy": {"stop_loss_pct": {"value": 0.07}},
    }
    changed = json.loads(json.dumps(baseline))
    changed["entry_policy"][candidate.policy_field]["value"] = candidate.new_value
    changed["exit_policy"]["stop_loss_pct"]["value"] = 0.08

    with pytest.raises(ValueError, match="outside the selected candidate"):
        validate_policy_delta(baseline, changed, candidate)


@pytest.mark.parametrize("mutation", ["digest", "leaf", "shape"])
def test_candidate_authenticates_the_complete_effective_policy(
    tmp_path: Path,
    mutation: str,
) -> None:
    readiness, _source_root, _candidate_root, _artifact_root = _canary_fixture(tmp_path)
    evaluation = _candidate_evaluation(readiness, accepted=True)
    if mutation == "digest":
        evaluation["effective_policy_sha256"] = "f" * 64
    elif mutation == "leaf":
        evaluation["effective_policy"]["exit_policy"]["stop_loss_pct"]["value"] = 0.08
        evaluation["effective_policy_sha256"] = _canonical_digest(
            evaluation["effective_policy"]
        )
    else:
        evaluation["effective_policy"]["unexpected_policy"] = {"raw": "NVDA"}
        evaluation["effective_policy_sha256"] = _canonical_digest(
            evaluation["effective_policy"]
        )

    with pytest.raises(ValueError, match="(effective policy|selected candidate)"):
        optimization._candidate_comparison(
            readiness,
            evaluation,
            candidate_catalog()["min_rs_score_075"],
        )


@pytest.mark.parametrize("overlap", ["source", "controller", "baseline", "bundle_parent"])
def test_optimizer_rejects_artifact_root_overlap_with_every_protected_boundary(
    tmp_path: Path,
    overlap: str,
) -> None:
    source = (tmp_path / "source").resolve()
    runtime = (tmp_path / "runtime").resolve()
    controller = (tmp_path / "controller").resolve()
    baseline = (tmp_path / "baseline").resolve()
    bundle_parent = (tmp_path / "sealed").resolve()
    bundle = bundle_parent / "pit.sqlite3"
    artifact_roots = {
        "source": source / "artifacts",
        "controller": controller / "artifacts",
        "baseline": baseline / "artifacts",
        "bundle_parent": bundle_parent,
    }
    gate = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=baseline,
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=bundle,
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256="d" * 64,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )

    with pytest.raises(agent_loop.ConfigurationError, match="artifact_root"):
        agent_loop.LoopConfig(
            source_root=source,
            permanent_runtime_root=runtime,
            git_executable=(tmp_path / "git.exe").resolve(),
            controller_temp_parent=controller,
            artifact_root=artifact_roots[overlap],
            mode=agent_loop.ExecutionMode(),
            gate=gate,
            models=agent_loop.ModelConfig(),
            limits=agent_loop.LoopLimits(max_usd=0.50, max_api_calls=3),
        )


@pytest.mark.parametrize("overlap", ["source", "baseline", "bundle_parent"])
def test_prepare_rejects_artifact_overlap_before_reading_sealed_inputs(
    tmp_path: Path,
    overlap: str,
) -> None:
    source = (tmp_path / "source").resolve()
    baseline = (tmp_path / "baseline").resolve()
    bundle_parent = (tmp_path / "sealed").resolve()
    source.mkdir()
    baseline.mkdir()
    bundle_parent.mkdir()
    bundle = bundle_parent / "pit.sqlite3"
    artifact_roots = {
        "source": source / "artifacts",
        "baseline": baseline / "artifacts",
        "bundle_parent": bundle_parent,
    }
    gate = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=baseline,
        baseline_manifest_sha256=optimization.BASELINE_MANIFEST_SHA256,
        pit_bundle=bundle,
        pit_bundle_sha256=optimization.PIT_BUNDLE_SHA256,
        effective_policy_sha256="d" * 64,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )

    with pytest.raises(ValueError, match="artifact root must not overlap"):
        optimization.prepare_pit_optimization(
            gate,
            source_root=source,
            artifact_root=artifact_roots[overlap],
            source_head="a" * 40,
            source_fingerprint_sha256="b" * 64,
        )


def _synthetic_prepare_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PitOptimizationGateConfig, Path, Path, Path]:
    source = (tmp_path / "source").resolve()
    baseline = (tmp_path / "baseline").resolve()
    artifacts = (tmp_path / "artifacts").resolve()
    source.mkdir()
    baseline.mkdir()
    (source / "core" / "canslim").mkdir(parents=True)
    (source / optimization.ENTRY_CONTRACT_PATH).write_text("sealed catalog\n", encoding="utf-8")
    bundle = (tmp_path / "sealed" / "pit.sqlite3").resolve()
    bundle.parent.mkdir()
    bundle.write_bytes(b"synthetic-pit")
    manifest = baseline / "run_manifest.json"
    manifest.write_bytes(b"synthetic-manifest")
    sealed = baseline / "summary.json"
    sealed.write_bytes(b"{}\n")
    policy = _prepare_policy()
    policy_sha = effective_engine_policy_sha256(policy)
    gate = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=baseline,
        baseline_manifest_sha256=optimization.BASELINE_MANIFEST_SHA256,
        pit_bundle=bundle,
        pit_bundle_sha256=optimization.PIT_BUNDLE_SHA256,
        effective_policy_sha256=policy_sha,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )

    class FakeBundle:
        metadata = {"evaluation_start": optimization.FULL_START_DATE}
        data_cutoff = pd.Timestamp(optimization.FULL_END_DATE)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeBundle":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeSimulator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._effective_engine_policy = policy

        def _verify_effective_engine_policy(self) -> str:
            return policy_sha

    catalog_identity = type("CatalogIdentity", (), {"source_sha256": "c" * 64})()
    monkeypatch.setattr(optimization, "verify_catalog_source", lambda _path: catalog_identity)
    monkeypatch.setattr("core.pit_data.PITDataBundle", FakeBundle)
    monkeypatch.setattr("core.backtest_engine.PortfolioSimulator", FakeSimulator)
    monkeypatch.setattr(
        optimization,
        "_baseline_observation",
        lambda *_args, **_kwargs: (
            {
                "full": _aggregate_window(asdict(_metrics())),
                "holdout": _aggregate_window(asdict(_metrics())),
                "leader_basket": {"count": 5},
            },
            {
                "run_manifest.json": optimization.BASELINE_MANIFEST_SHA256,
                "summary.json": hashlib.sha256(sealed.read_bytes()).hexdigest(),
            },
        ),
    )
    real_sha256 = optimization._sha256_file

    def sealed_hash(path: Path) -> str:
        if path == bundle:
            return optimization.PIT_BUNDLE_SHA256
        if path == manifest:
            return optimization.BASELINE_MANIFEST_SHA256
        return real_sha256(path)

    monkeypatch.setattr(optimization, "_sha256_file", sealed_hash)
    return gate, source, baseline, artifacts


def test_prepare_synthetic_success_is_provider_free_and_publishes_authenticated_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source, _baseline, artifacts = _synthetic_prepare_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        agent_loop.OpenRouterGateway,
        "request_pit_optimization_once",
        lambda *_args, **_kwargs: pytest.fail("prepare must not call a provider"),
    )

    readiness = optimization.prepare_pit_optimization(
        gate,
        source_root=source,
        artifact_root=artifacts,
        source_head="a" * 40,
        source_fingerprint_sha256="b" * 64,
    )

    assert json.loads(readiness.artifact_path.read_bytes()) == dict(readiness.primitive)
    assert hashlib.sha256(readiness.artifact_path.read_bytes()).hexdigest() == (
        readiness.readiness_sha256
    )
    assert readiness.primitive["sealed_inputs"]["baseline_artifact_sha256"] == {
        "run_manifest.json": optimization.BASELINE_MANIFEST_SHA256,
        "summary.json": hashlib.sha256(b"{}\n").hexdigest(),
    }


def test_prepare_rolls_back_new_readiness_if_post_publication_reauthentication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source, _baseline, artifacts = _synthetic_prepare_inputs(tmp_path, monkeypatch)
    checks = 0
    real_verify = optimization.verify_sealed_baseline_artifacts

    def verify(*args: object, **kwargs: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("sealed baseline changed after publication")
        real_verify(*args, **kwargs)

    monkeypatch.setattr(optimization, "verify_sealed_baseline_artifacts", verify)

    with pytest.raises(ValueError, match="changed after publication"):
        optimization.prepare_pit_optimization(
            gate,
            source_root=source,
            artifact_root=artifacts,
            source_head="a" * 40,
            source_fingerprint_sha256="b" * 64,
        )

    assert list(artifacts.iterdir()) == []


def test_sealed_baseline_reauthentication_rejects_any_artifact_mutation(
    tmp_path: Path,
) -> None:
    baseline = (tmp_path / "baseline").resolve()
    baseline.mkdir()
    files = {
        "run_manifest.json": b"manifest\n",
        "summary.json": b"{}\n",
        "equity_curve.csv": b"date,equity\n2026-04-01,1\n",
    }
    identities: dict[str, str] = {}
    for name, payload in files.items():
        path = baseline / name
        path.write_bytes(payload)
        identities[name] = hashlib.sha256(payload).hexdigest()

    optimization.verify_sealed_baseline_artifacts(baseline, identities)
    (baseline / "summary.json").write_bytes(b'{"changed":true}\n')

    with pytest.raises(ValueError, match="sealed baseline artifact changed: summary.json"):
        optimization.verify_sealed_baseline_artifacts(baseline, identities)


@pytest.mark.parametrize("field", ["bundle", "manifest"])
def test_prepare_rejects_wrong_operator_hashes_before_baseline_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    gate, source, _baseline, artifacts = _synthetic_prepare_inputs(tmp_path, monkeypatch)
    real_sha256 = hashlib.sha256

    def wrong_hash(path: Path) -> str:
        if path == gate.pit_bundle:
            return "0" * 64 if field == "bundle" else optimization.PIT_BUNDLE_SHA256
        if path == gate.baseline_run / "run_manifest.json":
            return "0" * 64 if field == "manifest" else optimization.BASELINE_MANIFEST_SHA256
        return real_sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(optimization, "_sha256_file", wrong_hash)

    with pytest.raises(ValueError, match="hash differs"):
        optimization.prepare_pit_optimization(
            gate,
            source_root=source,
            artifact_root=artifacts,
            source_head="a" * 40,
            source_fingerprint_sha256="b" * 64,
        )


def test_prepare_rejects_nonregular_bundle_before_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (tmp_path / "source").resolve()
    baseline = (tmp_path / "baseline").resolve()
    bundle = (tmp_path / "sealed" / "pit.sqlite3").resolve()
    source.mkdir()
    baseline.mkdir()
    bundle.mkdir(parents=True)
    gate = PitOptimizationGateConfig(
        phase="prepare",
        baseline_run=baseline,
        baseline_manifest_sha256=optimization.BASELINE_MANIFEST_SHA256,
        pit_bundle=bundle,
        pit_bundle_sha256=optimization.PIT_BUNDLE_SHA256,
        effective_policy_sha256="d" * 64,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
    )
    monkeypatch.setattr(optimization, "verify_catalog_source", lambda _path: object())

    with pytest.raises(ValueError, match="regular non-reparse single-link file"):
        optimization.prepare_pit_optimization(
            gate,
            source_root=source,
            artifact_root=(tmp_path / "artifacts").resolve(),
            source_head="a" * 40,
            source_fingerprint_sha256="b" * 64,
        )


def test_prepare_rejects_wrong_pit_date_contract_before_simulator_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, source, _baseline, artifacts = _synthetic_prepare_inputs(tmp_path, monkeypatch)

    class WrongDateBundle:
        metadata = {"evaluation_start": "2023-04-02"}
        data_cutoff = pd.Timestamp(optimization.FULL_END_DATE)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "WrongDateBundle":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("core.pit_data.PITDataBundle", WrongDateBundle)
    monkeypatch.setattr(
        "core.backtest_engine.PortfolioSimulator",
        lambda *_args, **_kwargs: pytest.fail("date mismatch must fail before simulator"),
    )

    with pytest.raises(ValueError, match="date contract"):
        optimization.prepare_pit_optimization(
            gate,
            source_root=source,
            artifact_root=artifacts,
            source_head="a" * 40,
            source_fingerprint_sha256="b" * 64,
        )


def test_optimizer_gateway_is_constructed_with_controller_owned_offline_pricing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness, source_root, candidate_root, artifact_root = _canary_fixture(tmp_path)
    controller_root = (tmp_path / "controller").resolve()
    gate = PitOptimizationGateConfig(
        phase="canary",
        baseline_run=(tmp_path / "baseline").resolve(),
        baseline_manifest_sha256="f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382",
        pit_bundle=(tmp_path / "pit.sqlite3").resolve(),
        pit_bundle_sha256="1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb",
        effective_policy_sha256=readiness.effective_policy_sha256,
        max_usd=0.50,
        max_api_calls=3,
        max_iterations=1,
        apply=False,
        readiness_sha256=readiness.readiness_sha256,
    )
    config = agent_loop.LoopConfig(
        source_root=source_root,
        permanent_runtime_root=(tmp_path / "runtime").resolve(),
        git_executable=(tmp_path / "git.exe").resolve(),
        controller_temp_parent=controller_root,
        artifact_root=artifact_root,
        mode=agent_loop.ExecutionMode(),
        gate=gate,
        models=agent_loop.ModelConfig(),
        limits=agent_loop.LoopLimits(max_usd=0.50, max_api_calls=3),
    )
    fingerprint = agent_loop.SourceFingerprint(
        "a" * 40,
        "codex/pit-optimization-cycle",
        "b" * 64,
        "c" * 64,
        (),
        "d" * 64,
    )
    state = agent_loop.SourceState(
        source_root,
        fingerprint.head,
        fingerprint.branch,
        "",
        (tmp_path / "agent-loop.lock").resolve(),
        fingerprint=fingerprint,
        controller_temp_parent=controller_root,
    )
    capability = object()
    candidate = agent_loop.Candidate(
        candidate_root,
        state.head,
        ("core/canslim/entry_contract.py",),
        capability,
        controller_root,
        (source_root,),
    )
    captured: dict[str, object] = {}

    class FakeGateway:
        api_key = None

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.ledger = kwargs["ledger"]

    class FakeAudit:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def write_manifest(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(agent_loop, "configure_git_executable", lambda _path: object())
    monkeypatch.setattr(agent_loop, "preflight_source", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(agent_loop, "export_candidate", lambda _state: candidate)
    monkeypatch.setattr(agent_loop, "OpenRouterGateway", FakeGateway)
    monkeypatch.setattr(agent_loop, "AuditTrail", FakeAudit)
    monkeypatch.setattr(optimization, "prepare_pit_optimization", lambda *_a, **_k: readiness)
    monkeypatch.setattr(
        optimization,
        "run_pit_optimization_canary",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("stop after construction")),
    )
    monkeypatch.setattr(agent_loop, "configure_docker_executable", lambda *_a, **_k: object())
    monkeypatch.setattr(agent_loop, "SandboxRunner", lambda **_kwargs: object())
    monkeypatch.setattr(
        agent_loop,
        "_pit_optimization_sandbox_evaluator",
        lambda *_args, **_kwargs: lambda _root: {},
    )
    monkeypatch.setattr(agent_loop, "_candidate_tracked_manifest_sha256", lambda _c: "f" * 64)
    monkeypatch.setattr(
        agent_loop,
        "_git",
        lambda *_args, **_kwargs: type("GitResult", (), {"stdout": b""})(),
    )
    monkeypatch.setattr(agent_loop, "dispose_candidate", lambda _candidate: None)

    with pytest.raises(agent_loop.ControllerInitializationError, match="pit_optimization_canary"):
        agent_loop._execute_cli_run(
            config,
            docker_executable=(tmp_path / "docker.exe").resolve(),
            sandbox_image="localhost/rs-agent-loop@sha256:" + "a" * 64,
            run_id="pit-opt-pricing",
        )

    assert callable(captured["pricing_loader"])
    for model in (
        agent_loop.ORCHESTRATOR_MODEL,
        agent_loop.REASONER_MODEL,
        agent_loop.CODER_MODEL,
    ):
        price = captured["pricing_loader"](model)
        assert set(price) == {"prompt", "completion"}
