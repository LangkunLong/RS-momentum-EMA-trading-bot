from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.backtest_engine import (
    EntryAttemptOutcome,
    PortfolioObservationV5,
    PositionEpisodeV5,
    PositionQuantityChangeV5,
    SimulationResultV5,
    Trade,
)
from core.backtest_fills import FrictionScenario, apply_friction
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.contracts import initial_friction_grid_v5
from core.pit_optimizer_v5.diagnostics import summarize_panel_result


_ENTRY_OUTCOME_IDS = (
    "entries_executed",
    "entry_rejected_already_open",
    "entry_rejected_capacity",
    "entry_rejected_missing_data",
    "entry_rejected_invalid_price",
    "entry_rejected_next_open_buy_zone",
    "entry_rejected_invalid_risk",
    "entry_rejected_no_cash",
)


def _fixed_result() -> tuple[EvaluationPanelSpec, FrictionScenario, SimulationResultV5]:
    sessions = ("2025-01-02", "2025-07-02", "2026-01-02")
    panel = EvaluationPanelSpec.from_lineages(
        purpose="quick",
        sessions=sessions,
        lineages=(
            PanelSecurityLineage(
                "issue87-fixed-lineage", ("LEAD",), ("sp500",)
            ),
        ),
    )
    scenario = FrictionScenario(
        scenario_id="base",
        half_spread_bps=2,
        market_impact_bps=3,
        commission_bps=0,
    )

    fills = []
    for session, action, reason, reference, quantity in (
        (sessions[0], "BUY", "fixture_entry", 100.0, 5.0),
        (sessions[1], "SELL", "fixture_scale_out", 110.0, 2.0),
        (sessions[2], "SELL", "fixture_exit", 120.0, 3.0),
    ):
        fill = apply_friction(
            side=action, reference_price=reference, quantity=quantity, scenario=scenario
        )
        friction = round(
            fill.commission_usd + fill.spread_cost_usd + fill.market_impact_cost_usd,
            2,
        )
        fills.append(
            {
                "Date": session,
                "Ticker": "LEAD",
                "Action": action,
                "Reason": reason,
                "EpisodeId": "fixture-episode",
                "ReferencePrice": reference,
                "ExecutionPrice": fill.execution_price,
                "Quantity": quantity,
                "GrossValue": fill.gross_value,
                "CommissionUSD": fill.commission_usd,
                "SlippageUSD": round(
                    fill.spread_cost_usd + fill.market_impact_cost_usd, 2
                ),
                "SpreadCostUSD": fill.spread_cost_usd,
                "MarketImpactCostUSD": fill.market_impact_cost_usd,
                "CashDelta": fill.cash_delta,
                "TotalFrictionUSD": friction,
                "StopReferenceKind": None,
                "StopPrice": None,
            }
        )

    fill_frame = pd.DataFrame(fills)
    fill_cost_totals = {
        "commission_usd": float(fill_frame["CommissionUSD"].sum()),
        "spread_cost_usd": float(fill_frame["SpreadCostUSD"].sum()),
        "market_impact_cost_usd": float(fill_frame["MarketImpactCostUSD"].sum()),
        "total_friction_usd": float(fill_frame["TotalFrictionUSD"].sum()),
    }
    entry_cash = -float(fill_frame.iloc[0]["CashDelta"])
    net_episode_cash = float(fill_frame["CashDelta"].sum())
    episode = PositionEpisodeV5(
        episode_id="fixture-episode",
        entry_symbol="LEAD",
        exit_symbol="LEAD",
        entry_session=sessions[0],
        exit_session=sessions[2],
        entry_price=float(fill_frame.iloc[0]["ExecutionPrice"]),
        exit_price=float(fill_frame.iloc[-1]["ExecutionPrice"]),
        # The fixture's $130 peak is before the July scale-out. The report
        # input stores only an episode maximum, with no timestamp for the peak.
        maximum_completed_bar_price=130.0,
        minimum_completed_bar_price=90.0,
        realized_return_pct=net_episode_cash / entry_cash * 100.0,
        quantity_path=(
            PositionQuantityChangeV5(
                sessions[0], "entry", 5.0, float(fill_frame.iloc[0]["ExecutionPrice"]), "fixture_entry"
            ),
            PositionQuantityChangeV5(
                sessions[1], "scale_out", 3.0, float(fill_frame.iloc[1]["ExecutionPrice"]), "fixture_scale_out"
            ),
            PositionQuantityChangeV5(
                sessions[2], "exit", 0.0, float(fill_frame.iloc[2]["ExecutionPrice"]), "fixture_exit"
            ),
        ),
        add_on_count=0,
        holding_sessions=2,
        exit_reason="fixture_exit",
    )
    trade = Trade(
        symbol="LEAD",
        entry_date=sessions[0],
        entry_price=episode.entry_price,
        qty=5.0,
        stop_price=90.0,
        exit_date=sessions[2],
        exit_price=episode.exit_price,
        exit_reason="fixture_exit",
        remaining_qty=0.0,
    )
    scenario_config = {
        "scenario_id": scenario.scenario_id,
        "half_spread_bps": scenario.half_spread_bps,
        "market_impact_bps": scenario.market_impact_bps,
        "commission_bps": scenario.commission_bps,
    }
    result = SimulationResultV5(
        trades=[trade],
        equity_curve=pd.Series(
            [999.75, 1049.64, 1079.46], index=sessions, dtype=float
        ),
        benchmark_curve=pd.Series([1000.0, 1000.0, 1000.0], index=sessions, dtype=float),
        initial_capital=1000.0,
        config={
            "tickers": ["LEAD"],
            "friction_scenario": scenario_config,
            "fill_cost_totals": fill_cost_totals,
        },
        signal_log=pd.DataFrame(),
        execution_diagnostics={
            key: int(key == "entries_executed") for key in _ENTRY_OUTCOME_IDS
        },
        entry_outcomes=(
            EntryAttemptOutcome(
                symbol="LEAD",
                signal_date="2024-12-31",
                entry_date=sessions[0],
                pivot=100.0,
                buy_zone_lower=90.0,
                buy_zone_upper=105.0,
                entry_open=100.0,
                outcome="entries_executed",
            ),
        ),
        fill_log=fill_frame,
        fill_cost_totals=fill_cost_totals,
        position_episodes=(episode,),
        portfolio_observations=(
            PortfolioObservationV5(sessions[0], 499.75, 500.0, 999.75, "bull"),
            PortfolioObservationV5(sessions[1], 719.64, 330.0, 1049.64, "bull"),
            PortfolioObservationV5(sessions[2], 1079.46, 0.0, 1079.46, "bull"),
        ),
        policy_intent_outcomes={
            "queued": 0,
            "executed_next_open": 0,
            "delayed_missing_open_sessions": 0,
            "preempted_by_stop": 0,
            "unexecuted_terminal": 0,
        },
    )
    return panel, scenario, result


def test_friction_grid_is_an_explicit_assumption_set() -> None:
    assert tuple(
        (
            item.scenario_id,
            item.half_spread_bps,
            item.market_impact_bps,
            item.commission_bps,
        )
        for item in initial_friction_grid_v5()
    ) == (("gross", 0, 0, 0), ("base", 2, 3, 0), ("stress", 5, 10, 1))


def test_public_diagnostic_values_match_fixed_same_path_arithmetic() -> None:
    panel, scenario, result = _fixed_result()
    report = summarize_panel_result(panel=panel, scenario=scenario, result=result)

    # First-to-last observed equity covers 365 calendar days in this fixture.
    # The source reconstructs the no-friction path by adding cumulative fill
    # costs to each observed equity value: [1000.00, 1050.00, 1080.00].
    assert report.portfolio_total_return_pct == (
        (Decimal("1079.46") / Decimal("999.75") - 1) * 100
    ).quantize(Decimal("0.000001"))
    assert report.gross_annualized_return_pct == Decimal("8.000000")
    assert report.total_friction_usd == Decimal("0.54")

    mean_exposure_fraction = (
        Decimal("500") / Decimal("999.75")
        + Decimal("330") / Decimal("1049.64")
        + Decimal("0") / Decimal("1079.46")
    ) / 3
    net_annualized = report.portfolio_annualized_return_pct
    expected_invested_sleeve = (net_annualized / mean_exposure_fraction).quantize(
        Decimal("0.000001")
    )
    expected_idle_cash_drag = (expected_invested_sleeve - net_annualized).quantize(
        Decimal("0.000001")
    )
    assert report.invested_sleeve_annualized_return_pct == expected_invested_sleeve
    assert report.estimated_idle_cash_drag_pct == expected_idle_cash_drag
    assert report.average_exposure_pct == Decimal("27.150618")
    assert report.invested_sleeve_annualized_return_pct == Decimal("29.365788")
    assert report.estimated_idle_cash_drag_pct == Decimal("21.392795")

    expected_scale_out_gap = (
        (Decimal("130") - Decimal("109.945"))
        * Decimal("2")
        / (Decimal("109.945") * Decimal("2"))
        * 100
    ).quantize(Decimal("0.000001"))
    assert report.scale_out_opportunity_cost_pct == expected_scale_out_gap
    assert report.scale_out_opportunity_cost_pct == Decimal("18.240939")
