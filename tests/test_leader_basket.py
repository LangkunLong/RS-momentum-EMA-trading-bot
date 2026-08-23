"""Execution/leader-recall reconciliation regressions."""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.backtest_engine import SimulationResult
from core.leader_evaluation import FiveYearLeader
from core.pit_baseline_report import build_leader_recall_frame, reconcile_signals_to_transactions


def test_recall_and_execution_reconciliation_counts_each_gate_once() -> None:
    """Break caught: leaders signaled, cash-blocked, or failing gates were conflated."""
    leaders = tuple(FiveYearLeader(symbol, date(2024, 1, 2), date(2024, 2, 1), 50.0 - rank, rank, True, date(2024, 1, 2)) for rank, symbol in enumerate(("AAA", "BBB", "CCC", "DDD"), 1))
    signals = pd.DataFrame([
        {"symbol": "AAA", "signal_date": "2024-01-02", "buy_signal": True, "current_growth": 30, "annual_growth": 30, "rs_score": 90, "has_breakout": True, "has_volume_surge": True, "in_buy_zone": True, "canslim_score": 90},
        {"symbol": "BBB", "signal_date": "2024-01-02", "buy_signal": True, "current_growth": 30, "annual_growth": 30, "rs_score": 90, "has_breakout": True, "has_volume_surge": True, "in_buy_zone": True, "canslim_score": 90},
        {"symbol": "CCC", "signal_date": "2024-01-02", "buy_signal": False, "current_growth": 10, "annual_growth": 10, "rs_score": 40, "has_breakout": False, "has_volume_surge": False, "in_buy_zone": False, "canslim_score": 40},
        {"symbol": "DDD", "signal_date": "2024-01-02", "buy_signal": False, "current_growth": None, "annual_growth": None, "rs_score": 90, "has_breakout": True, "has_volume_surge": True, "in_buy_zone": True, "canslim_score": 90},
    ])
    transactions = pd.DataFrame([{"Date": "2024-01-03", "Ticker": "AAA", "Action": "BUY"}])
    result = SimulationResult(signal_log=signals, transaction_log=transactions)
    recall = build_leader_recall_frame(leaders, result.signal_log, result.transaction_log, start_date=date(2024, 1, 2), min_c_a_growth=25, min_rs_score=80, min_canslim_score=75, blocked_for_cash={"BBB": 1})
    assert recall[["ticker", "buy_signal_count", "entry_count", "blocked_for_cash_count", "c_fail_count", "a_fail_count", "rs_fail_count", "breakout_fail_count", "volume_fail_count", "buy_zone_fail_count", "composite_fail_count", "missing_fundamentals_count"]].to_dict("records") == [
        {"ticker": "AAA", "buy_signal_count": 1, "entry_count": 1, "blocked_for_cash_count": 0, "c_fail_count": 0, "a_fail_count": 0, "rs_fail_count": 0, "breakout_fail_count": 0, "volume_fail_count": 0, "buy_zone_fail_count": 0, "composite_fail_count": 0, "missing_fundamentals_count": 0},
        {"ticker": "BBB", "buy_signal_count": 1, "entry_count": 0, "blocked_for_cash_count": 1, "c_fail_count": 0, "a_fail_count": 0, "rs_fail_count": 0, "breakout_fail_count": 0, "volume_fail_count": 0, "buy_zone_fail_count": 0, "composite_fail_count": 0, "missing_fundamentals_count": 0},
        {"ticker": "CCC", "buy_signal_count": 0, "entry_count": 0, "blocked_for_cash_count": 0, "c_fail_count": 1, "a_fail_count": 1, "rs_fail_count": 1, "breakout_fail_count": 1, "volume_fail_count": 1, "buy_zone_fail_count": 1, "composite_fail_count": 1, "missing_fundamentals_count": 0},
        {"ticker": "DDD", "buy_signal_count": 0, "entry_count": 0, "blocked_for_cash_count": 0, "c_fail_count": 0, "a_fail_count": 0, "rs_fail_count": 0, "breakout_fail_count": 0, "volume_fail_count": 0, "buy_zone_fail_count": 0, "composite_fail_count": 0, "missing_fundamentals_count": 1},
    ]
    diagnostics = {"buy_signal_rows": 2, "entries_executed": 1, "entry_attempts": 2, "entry_rejected_no_cash": 1, "entry_rejected_already_open": 0, "entry_rejected_capacity": 0, "entry_rejected_missing_data": 0, "entry_rejected_invalid_price": 0, "entry_rejected_invalid_risk": 0, "buy_signal_rows_when_entries_allowed": 2, "buy_signal_rows_blocked_by_regime": 0, "buy_signal_rows_blocked_by_market": 0, "buy_signal_rows_blocked_by_both": 0, "capacity_truncated_signals": 0}
    reconciliation = reconcile_signals_to_transactions(result.signal_log, result.transaction_log, diagnostics, trading_days=["2024-01-02", "2024-01-03"])
    assert reconciliation["buy_signal_count"] == 2
    assert reconciliation["entry_count"] == 1
    assert reconciliation["cash_blocked_count"] == 1
    assert reconciliation["blocked_for_cash_by_symbol"] == {"BBB": 1}
