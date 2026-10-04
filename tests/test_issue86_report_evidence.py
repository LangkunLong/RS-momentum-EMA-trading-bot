"""Fixed offline acceptance fixtures for issue 86 report evidence."""

from __future__ import annotations

import json
import hashlib

import pandas as pd
import pytest

from core.pit_optimizer_v5.artifacts import _decode_dataclass
from core.pit_optimizer_v5.contracts import (
    EvaluationReportV5,
    MetricCountV5,
    RoleEvidenceV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
    initial_friction_grid_v5,
)
from core.backtest_engine import (
    PortfolioSimulator,
    EntryAttemptOutcome,
    PositionQuantityChangeV5,
    Trade,
    _PositionEpisodeStateV5,
)
from core.backtest_fills import ExecutionProfileV5, apply_friction
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.diagnostics import summarize_panel_result
from core.pit_optimizer_v5.diagnostics import to_role_evidence
from core.strategy_policy.contracts import BenchmarkContextV1, MarketContextV1
from core.strategy_policy.contracts_v3 import AddOnDecisionV3
from core.pit_feature_snapshot import HoldingFeaturesV3


_SESSIONS = ("2025-01-02", "2025-01-03", "2025-01-06")
_UNKNOWN_ADD_ON_REASON = "candidate_confirmation_guard"


class _FixedAddOnPolicy:
    interface_version = 3

    def __init__(self) -> None:
        self.decisions = [
            AddOnDecisionV3(True, 0.005, 0.20, "candidate_momentum_add"),
            AddOnDecisionV3(False, 0.0, None, _UNKNOWN_ADD_ON_REASON),
        ]

    def evaluate_add_on(self, _snapshot: object) -> AddOnDecisionV3:
        return self.decisions.pop(0)


def _market(session: str) -> MarketContextV1:
    return MarketContextV1(
        schema_version=1,
        session=session,
        oneil_regime="confirmed_uptrend",
        distribution_days=0,
        follow_through=False,
        benchmarks=(
            BenchmarkContextV1("SPY", 0.0, 0.0, 0.0),
            BenchmarkContextV1("QQQ", 0.0, 0.0, 0.0),
            BenchmarkContextV1("IWM", 0.0, 0.0, 0.0),
        ),
        active_constituent_count=1,
        breadth_above_50_fraction=1.0,
        breadth_50_coverage_fraction=1.0,
        breadth_above_200_fraction=1.0,
        breadth_200_coverage_fraction=1.0,
        median_rs_score=80.0,
        rs_at_least_80_fraction=1.0,
        rs_coverage_fraction=1.0,
    )


def _actual_engine_result():
    """Use real V5 add-on queue/fill/close methods with bounded synthetic bars."""
    friction = next(item for item in initial_friction_grid_v5() if item.scenario_id == "base")
    execution = ExecutionProfileV5(
        schema_version=5,
        close_policy_exit_timing="next_open",
        gap_stop_rule="open_then_stop",
        end_of_test_rule="last_session_close",
        friction_model="half_spread_plus_market_impact_plus_commission_bps",
    )
    simulator = PortfolioSimulator(
        initial_capital=10_000.0,
        execution_profile=execution,
        friction_scenario=friction,
    )
    simulator._policy_client = _FixedAddOnPolicy()
    prices = pd.DataFrame(
        {
            "Open": [100.0, 120.0, 130.0],
            "High": [112.0, 125.0, 135.0],
            "Low": [95.0, 115.0, 128.0],
            "Close": [110.0, 120.0, 130.0],
            "Volume": [1_000.0, 1_000.0, 1_000.0],
        },
        index=pd.to_datetime(_SESSIONS),
    )
    simulator._v3_ticker_ohlcv = {"AAA": prices}
    simulator._v3_holding_features = lambda **_kwargs: HoldingFeaturesV3(  # type: ignore[method-assign]
        current_rs_score=80.0,
        industry_group_rs=80.0,
        atr_20_fraction=0.02,
        volume_ratio=1.2,
    )

    initial_fill = apply_friction(
        side="BUY", reference_price=100.0, quantity=5.0, scenario=friction
    )
    initial_cash = round(simulator.initial_capital + initial_fill.cash_delta, 2)
    trade = Trade(
        symbol="AAA",
        entry_date=_SESSIONS[0],
        entry_price=initial_fill.execution_price,
        qty=5.0,
        stop_price=90.0,
        remaining_qty=5.0,
    )
    state = _PositionEpisodeStateV5(
        episode_id="issue86-episode",
        entry_symbol="AAA",
        current_symbol="AAA",
        entry_session=_SESSIONS[0],
        entry_price=initial_fill.execution_price,
        maximum_completed_bar_price=112.0,
        minimum_completed_bar_price=95.0,
        quantity_path=[
            PositionQuantityChangeV5(
                _SESSIONS[0], "entry", 5.0, initial_fill.execution_price, "entry"
            )
        ],
        holding_sessions=1,
    )
    simulator._open_positions["AAA"] = trade
    simulator._position_episode_states["AAA"] = state
    simulator._record_v5_fill(
        date=_SESSIONS[0],
        ticker="AAA",
        reason="entry",
        fill=initial_fill,
        episode_id=state.episode_id,
    )
    simulator._record_transaction(
        date=_SESSIONS[0],
        ticker="AAA",
        action="BUY",
        price=initial_fill.execution_price,
        quantity=5.0,
        reason="entry",
    )
    simulator._equity = initial_cash

    pending = simulator._queue_v3_add_on_after_close(
        symbol="AAA",
        ohlcv=prices,
        eval_date=pd.Timestamp(_SESSIONS[0]),
        market=_market(_SESSIONS[0]),
        next_session=pd.Timestamp(_SESSIONS[1]),
        already_pending=False,
    )
    assert pending is not None
    assert simulator._execute_v5_pending_add_ons(
        [pending], {"AAA": prices}, pd.Timestamp(_SESSIONS[1])
    ) == []
    state.maximum_completed_bar_price = 125.0
    state.minimum_completed_bar_price = 95.0
    state.holding_sessions = 2

    declined = simulator._queue_v3_add_on_after_close(
        symbol="AAA",
        ohlcv=prices,
        eval_date=pd.Timestamp(_SESSIONS[1]),
        market=_market(_SESSIONS[1]),
        next_session=pd.Timestamp(_SESSIONS[2]),
        already_pending=False,
    )
    assert declined is None
    state.maximum_completed_bar_price = 135.0
    state.holding_sessions = 3
    simulator._close_trade("AAA", 130.0, "end_of_test", _SESSIONS[2])

    add_on_rejections = simulator._add_on_rejection_reasons
    assert add_on_rejections == {_UNKNOWN_ADD_ON_REASON: 1}
    friction_totals = dict(simulator._fill_cost_totals)
    friction_primitive = {
        "scenario_id": friction.scenario_id,
        "half_spread_bps": friction.half_spread_bps,
        "market_impact_bps": friction.market_impact_bps,
        "commission_bps": friction.commission_bps,
    }
    final_cash = simulator._equity
    # The second observation is before terminal liquidation; recover its cash
    # from the actual executed add-on fill, which is the last BUY in the log.
    add_cash_delta = float(
        next(row["CashDelta"] for row in simulator._fill_rows if row["Reason"].startswith("add_on:"))
    )
    cash_after_add = round(initial_cash + add_cash_delta, 2)
    second_equity = cash_after_add + 120.0 * float(
        next(change.quantity_after for change in state.quantity_path if change.action == "add_on")
    )
    equity = (initial_cash + 110.0 * 5.0, second_equity, final_cash)
    observations = (
        _observation(_SESSIONS[0], initial_cash, 550.0, equity[0]),
        _observation(_SESSIONS[1], cash_after_add, second_equity - cash_after_add, second_equity),
        _observation(_SESSIONS[2], final_cash, 0.0, final_cash),
    )
    simulator._portfolio_observations = list(observations)
    simulator._execution_diagnostics["entries_executed"] = 1
    simulator._entry_outcomes = [
        EntryAttemptOutcome(
            "AAA",
            _SESSIONS[0],
            _SESSIONS[0],
            100.0,
            100.0,
            105.0,
            100.0,
            "entries_executed",
        )
    ]
    result = simulator._assemble_simulation_result(
        equity_curve=pd.Series(equity, index=pd.to_datetime(_SESSIONS)),
        benchmark_curve=pd.Series((100.0, 101.0, 102.0), index=pd.to_datetime(_SESSIONS)),
        benchmark="SPY",
        result_config={
            "friction_scenario": friction_primitive,
            "fill_cost_totals": friction_totals,
        },
    )
    panel = EvaluationPanelSpec.from_lineages(
        purpose="discovery",
        sessions=_SESSIONS,
        lineages=(PanelSecurityLineage("issue86-lineage", ("AAA",), ("sp500",)),),
    )
    return panel, friction, result


def _observation(session: str, cash: float, gross: float, equity: float):
    from core.backtest_engine import PortfolioObservationV5

    return PortfolioObservationV5(
        session=session,
        cash=cash,
        gross_long_notional=gross,
        total_equity=equity,
        oneil_regime="confirmed_uptrend",
    )


def test_actual_engine_observation_reaches_v2_report() -> None:
    panel, friction, result = _actual_engine_result()

    assert result.add_on_outcomes["queued"] == 1
    assert result.add_on_outcomes["executed"] == 1
    assert result.add_on_rejection_reasons == {_UNKNOWN_ADD_ON_REASON: 1}
    assert result.add_on_rejection_telemetry_status == "complete"
    assert result.position_episodes[0].add_on_count == 1

    report = summarize_panel_result(panel=panel, scenario=friction, result=result)
    assert report.report_semantics_version == 2
    assert _counts(report.add_on_outcomes)["executed"] == 1
    assert _counts(report.add_on_outcomes)["declined"] == 1
    assert _counts(report.add_on_rejection_reason_counts)[_UNKNOWN_ADD_ON_REASON] == 1
    assert _counts(report.unregistered_add_on_rejection_reason_counts)[
        _UNKNOWN_ADD_ON_REASON
    ] == 1
    assert report.friction_scenario == friction
    assert report.friction_calibration_status == "not_supplied"
    assert "arithmetic mean of observed-session gross-long-notional / total-equity" in next(
        item.definition
        for item in report.metric_definitions
        if item.metric_id == "estimated_idle_cash_drag_pct"
    )
    assert "chronology relative to a sale is unknown" in next(
        item.definition
        for item in report.metric_definitions
        if item.metric_id == "scale_out_opportunity_cost_pct"
    )

    evidence = to_role_evidence(report)
    evidence_by_metric = {item.metric_id: item for item in evidence.items}
    reason = next(
        item
        for item in evidence.items
        if item.metric_id.startswith("add_on.rejection_reason_count.")
    )
    assert reason.value == 1
    assert "sha256" in reason.description.lower()
    assert evidence_by_metric[
        "report.gross_annualized_return_pct"
    ].description is not None
    assert "not supplied" in evidence_by_metric[
        "report.friction_calibration_status"
    ].description.lower()

    from dataclasses import replace

    partial_result = replace(
        result,
        add_on_rejection_reasons={},
        add_on_rejection_telemetry_status="incomplete_legacy_checkpoint",
    )
    partial_report = summarize_panel_result(
        panel=panel,
        scenario=friction,
        result=partial_result,
    )
    assert partial_report.add_on_rejection_telemetry_status == "unavailable_legacy_checkpoint"
    assert "declined" not in _counts(partial_report.add_on_outcomes)
    partial_evidence = to_role_evidence(partial_report)
    partial_status = next(
        item
        for item in partial_evidence.items
        if item.metric_id == "report.add_on_rejection_telemetry_status"
    )
    assert "unavailable_legacy_checkpoint" in partial_status.description
    assert not any(
        item.metric_id == "add_on.outcome.declined"
        for item in partial_evidence.items
    )

    many_reasons = tuple(
        MetricCountV5(f"candidate_reason_{index}", 1)
        for index in range(66)
    )
    expanded = replace(
        report,
        add_on_outcomes=tuple(
            MetricCountV5(item.metric_id, 66 if item.metric_id == "declined" else item.count)
            for item in report.add_on_outcomes
        ),
        add_on_rejection_reason_counts=many_reasons,
        unregistered_add_on_rejection_reason_counts=many_reasons,
    )
    bounded = to_role_evidence(expanded)
    reason_items = [
        item
        for item in bounded.items
        if item.metric_id.startswith("add_on.rejection_reason_count.")
        and item.metric_id
        != "add_on.rejection_reason_count.unrepresented_reason_category_count"
    ]
    assert len(reason_items) == 64
    unrepresented = next(
        item
        for item in bounded.items
        if item.metric_id == "add_on.rejection_reason_count.unrepresented_reason_category_count"
    )
    assert unrepresented.value == 2
    assert "sha256" in unrepresented.description.lower()


def test_absent_legacy_add_on_outcomes_stay_unavailable_without_zeroes() -> None:
    from dataclasses import replace

    panel, friction, result = _actual_engine_result()
    legacy_result = replace(
        result,
        add_on_outcomes={},
        add_on_rejection_reasons={},
        add_on_rejection_telemetry_status="unavailable_unspecified",
    )

    report = summarize_panel_result(
        panel=panel,
        scenario=friction,
        result=legacy_result,
    )

    assert report.add_on_rejection_telemetry_status == "unavailable_unspecified"
    assert report.add_on_outcomes == ()
    evidence = to_role_evidence(report)
    assert not any(item.metric_id.startswith("add_on.outcome.") for item in evidence.items)
    status = next(
        item
        for item in evidence.items
        if item.metric_id == "report.add_on_rejection_telemetry_status"
    )
    assert status.value is None
    assert "unavailable_unspecified" in status.description
    assert "not a zero" in status.description

    with pytest.raises(ValueError, match="add-on outcome telemetry is incomplete"):
        replace(report, add_on_outcomes=(MetricCountV5("queued", 1),))


def test_nonempty_partial_add_on_outcome_map_remains_invalid() -> None:
    from dataclasses import replace

    panel, friction, result = _actual_engine_result()
    partial_result = replace(
        result,
        add_on_outcomes={"queued": 1},
        add_on_rejection_reasons={},
        add_on_rejection_telemetry_status="unavailable_unspecified",
    )

    with pytest.raises(ValueError, match="V5 add-on outcomes are incomplete"):
        summarize_panel_result(
            panel=panel,
            scenario=friction,
            result=partial_result,
        )


def test_legacy_v1_report_bytes_and_decode_remain_unchanged() -> None:
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _zero_report

    legacy_report = _zero_report()
    legacy_bytes = canonical_json_bytes_v5(legacy_report)
    assert canonical_sha256_v5(legacy_report) == (
        "5cccfb2c87faf7b6ec91ce3cf0b38945ff0cff04af1f5ab082090281604aa706"
    )
    decoded = _decode_dataclass(EvaluationReportV5, json.loads(legacy_bytes))
    assert canonical_json_bytes_v5(decoded) == legacy_bytes
    assert decoded.report_semantics_version == 1


def test_persisted_report_reaches_next_investigator_request(tmp_path) -> None:
    from dataclasses import replace

    from core.pit_optimizer_v5.production_runtime import LocalRoleRequestFactoryV5
    from core.pit_optimizer_v5.runtime import FeedbackRoundInputV5, SearchProjectionV5
    from core.pit_optimizer_v5.search import (
        CandidateArchiveV5,
        SearchStateV5,
        baseline_parent_candidate_v5,
    )
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _capability

    panel, friction, result = _actual_engine_result()
    result = replace(
        result,
        add_on_rejection_telemetry_status="incomplete_legacy_checkpoint",
    )
    report = summarize_panel_result(panel=panel, scenario=friction, result=result)
    mechanism_repository, capability, _candidate_bundle, _candidate_revision = _capability(tmp_path)
    authenticated = capability.authenticated_manifest
    artifact_repository = mechanism_repository.repository
    baseline = authenticated.baseline_authority

    persisted_evaluation = replace(
        baseline.campaign.episodes[0].evaluation,
        scenarios=tuple(
            replace(scenario, report=report)
            if scenario.scenario_id == "base"
            else scenario
            for scenario in baseline.campaign.episodes[0].evaluation.scenarios
        ),
    )
    evaluation_ref = artifact_repository.create_typed_artifact(
        "issue86/persisted-report-panel.json", persisted_evaluation
    )
    restored_evaluation = artifact_repository.load_typed_artifact(
        evaluation_ref, value_type=type(persisted_evaluation)
    )
    assert restored_evaluation == persisted_evaluation

    first_episode = replace(baseline.campaign.episodes[0], evaluation=restored_evaluation)
    campaign = replace(
        baseline.campaign,
        episodes=(first_episode, *baseline.campaign.episodes[1:]),
        closed_trades=report.closed_trades,
    )
    baseline_with_report = replace(baseline, campaign=campaign)
    inputs = FeedbackRoundInputV5(
        manifest=authenticated.manifest,
        panel_plan=authenticated.panel_plan,
        evaluator_contract=authenticated.evaluator_contract,
        baseline=baseline_with_report,
        round_index=1,
        owner_token_sha256="e" * 64,
    )
    projection = SearchProjectionV5(
        checkpoint=None,
        state=SearchStateV5(
            next_round_index=1,
            archive=CandidateArchiveV5(capacity=inputs.manifest.search.archive_capacity),
            attempted_novelty_keys=(),
        ),
        stored_records=(),
    )
    parent = baseline_parent_candidate_v5(
        authority=baseline_with_report,
        discovery_plan=inputs.panel_plan,
        evaluator_contract=inputs.evaluator_contract,
        pit_data_scope=baseline_with_report.pit_data_scope,
    )
    factory = LocalRoleRequestFactoryV5(
        repository=artifact_repository,
        manifest=inputs.manifest,
    )
    request = factory.investigator_request(inputs, projection, parent)
    evidence = {item.metric_id: item for item in request.role_evidence.items}

    reason = next(
        item
        for item in request.role_evidence.items
        if item.metric_id.startswith(
            "episode.1.add_on.unregistered_rejection_reason_count."
        )
    )
    assert reason.value == 1
    assert "candidate-authored" in reason.description.lower()
    assert evidence["episode.1.report_semantics_version"].value == 2
    assert "Same-path gross-of-configured-friction" in evidence[
        "episode.1.gross_annualized_return_pct"
    ].description
    assert "arithmetic mean of observed-session gross-long-notional / total-equity" in evidence[
        "episode.1.estimated_idle_cash_drag_pct"
    ].description
    assert "chronology relative to a sale is unknown" in evidence[
        "episode.1.scale_out_opportunity_cost_pct"
    ].description
    assert evidence["episode.1.friction_scenario.half_spread_bps"].value == 2
    assert evidence["episode.1.friction_scenario.market_impact_bps"].value == 3
    assert evidence["episode.1.friction_scenario.commission_bps"].value == 0
    assert "no observed quote" in evidence[
        "episode.1.friction_calibration_status"
    ].description

    message_content = request.messages[0]["content"]
    assert message_content["evidence_schema_version"] == 2
    message_payloads = {
        item["payload"]["metric_id"]: item["payload"]
        for item in message_content["evidence"]
    }
    assert _UNKNOWN_ADD_ON_REASON in message_payloads[reason.metric_id]["description"]
    assert "sha256" in message_payloads[reason.metric_id]["description"].lower()
    message_status = message_payloads[
        "episode.1.add_on_rejection_telemetry_status"
    ]["description"]
    assert "unavailable_legacy_checkpoint" in message_status
    assert "not a zero count" in message_status
    assert "Same-path gross-of-configured-friction" in message_payloads[
        "episode.1.gross_annualized_return_pct"
    ]["description"]
    assert "arithmetic mean of observed-session gross-long-notional / total-equity" in message_payloads[
        "episode.1.estimated_idle_cash_drag_pct"
    ]["description"]
    assert "chronology relative to a sale is unknown" in message_payloads[
        "episode.1.scale_out_opportunity_cost_pct"
    ]["description"]
    assert "no observed quote" in message_payloads[
        "episode.1.friction_calibration_status"
    ]["description"]

    from core.pit_optimizer_v5.production_runtime import (
        _EvidenceBuilderV5,
        _add_on_reason_description,
    )

    unsafe_reason = "ticker: ABC"
    withheld_description = _add_on_reason_description(unsafe_reason)
    unsafe_digest = hashlib.sha256(unsafe_reason.encode("utf-8")).hexdigest()
    from core.pit_optimizer_v5.provider import _validate_safe_text

    assert unsafe_reason not in withheld_description
    assert unsafe_digest in withheld_description
    assert "withheld" in withheld_description
    assert _validate_safe_text(withheld_description, "fixture description") == withheld_description

    unsafe_items = tuple(
        replace(item, description="ticker: ABC")
        if item.metric_id == reason.metric_id
        else item
        for item in request.role_evidence.items
    )
    with pytest.raises(ValueError, match="role evidence description contains"):
        replace(request, role_evidence=RoleEvidenceV5(5, unsafe_items))

    detailed_builder = _EvidenceBuilderV5("critic")
    LocalRoleRequestFactoryV5._report_evidence(
        detailed_builder,
        report,
        prefix="candidate.1",
    )
    detailed_items = detailed_builder.build().items

    def shared_projection(items, prefix):
        projected = {}
        for item in items:
            if not item.metric_id.startswith(prefix):
                continue
            suffix = item.metric_id[len(prefix) :]
            if (
                suffix.startswith("add_on.")
                or suffix == "add_on_rejection_telemetry_status"
                or suffix.startswith("friction_scenario.")
                or suffix == "friction_calibration_status"
            ):
                projected[suffix] = (item.value, item.description)
        return projected

    assert shared_projection(
        request.role_evidence.items,
        "episode.1.",
    ) == shared_projection(detailed_items, "candidate.1.")


def test_actual_completed_checkpoint_round_trip_marks_legacy_reason_counts_unavailable(
    tmp_path,
) -> None:
    from unittest.mock import MagicMock, patch

    import core.backtest_engine as backtest_engine

    sessions = pd.bdate_range("2024-01-02", periods=300)
    rs_symbols = tuple(f"SYM{index}" for index in range(10))
    history = pd.DataFrame(
        {
            "Open": [100.0] * len(sessions),
            "High": [101.0] * len(sessions),
            "Low": [99.0] * len(sessions),
            "Close": [100.0] * len(sessions),
            "Volume": [1_000_000.0] * len(sessions),
        },
        index=sessions,
    )
    closes = pd.DataFrame(
        {
            symbol: history["Close"]
            for symbol in ("AAA", "SPY", "QQQ", "IWM", *rs_symbols)
        },
        index=sessions,
    )

    def simulator() -> PortfolioSimulator:
        fetcher = MagicMock()
        fetcher.fetch_price_data.return_value = {
            symbol: history.copy()
                for symbol in ("AAA", "SPY", "QQQ", "IWM", *rs_symbols)
        }
        fetcher.fetch_rs_universe_closes.return_value = closes.copy()
        strategy = MagicMock()
        strategy.checkpoint_identity = {"name": "issue86-fixed-no-signals", "version": 1}
        strategy.effective_policy_identity = dict(strategy.checkpoint_identity)
        strategy.evaluate_market.return_value = {"market_is_bullish": True}
        strategy.evaluate_symbol.return_value = None
        return PortfolioSimulator(
            technical_only=True,
            signal_every_n_days=5,
            data_fetcher=fetcher,
            strategy=strategy,
            execution_profile=ExecutionProfileV5(
                5,
                "next_open",
                "open_then_stop",
                "last_session_close",
                "half_spread_plus_market_impact_plus_commission_bps",
            ),
            friction_scenario=next(
                item for item in initial_friction_grid_v5() if item.scenario_id == "base"
            ),
        )

    checkpoint = tmp_path / "portfolio-checkpoint.json"
    args = {
        "tickers": ["AAA"],
        "start_date": str(sessions[265].date()),
        "end_date": str(sessions[-1].date()),
        "history_start_date": str(sessions[0].date()),
        "benchmark_symbol": "SPY",
        "checkpoint_path": checkpoint,
        "checkpoint_every_days": 40,
        "checkpoint_code_identity": "issue86-fixed-checkpoint",
    }
    with (
            patch("core.backtest_engine.get_sp500_tickers", return_value=list(rs_symbols)),
        patch("core.backtest_engine.clear_session_cache"),
    ):
        complete_result = simulator().run(**args)

    assert complete_result.add_on_rejection_telemetry_status == "complete"
    assert complete_result.add_on_rejection_reasons == {}
    complete_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert complete_payload["add_on_rejection_telemetry_status"] == "complete"
    assert complete_payload["add_on_rejection_reasons"] == {}

    del complete_payload["add_on_rejection_telemetry_status"]
    del complete_payload["add_on_rejection_reasons"]
    checkpoint.write_bytes(backtest_engine._checkpoint_bytes(complete_payload))
    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=list(rs_symbols)),
        patch("core.backtest_engine.clear_session_cache"),
    ):
        legacy_result = simulator().run(**args, resume=True)

    assert legacy_result.add_on_rejection_telemetry_status == "incomplete_legacy_checkpoint"
    assert legacy_result.add_on_rejection_reasons == {}

    complete_payload["add_on_rejection_telemetry_status"] = None
    complete_payload["add_on_rejection_reasons"] = {}
    checkpoint.write_bytes(backtest_engine._checkpoint_bytes(complete_payload))
    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=list(rs_symbols)),
        patch("core.backtest_engine.clear_session_cache"),
        pytest.raises(ValueError, match="rejection telemetry status is invalid"),
    ):
        simulator().run(**args, resume=True)


def _counts(metrics) -> dict[str, int]:
    return {item.metric_id: item.count for item in metrics}
