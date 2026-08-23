"""Pure tests for point-in-time leader labels and signal recall."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.leader_evaluation import (
    LeaderIdentityContract,
    LeaderLabel,
    LeaderPriceIdentity,
    MembershipEvent,
    PointInTimeUniverse,
    label_five_year_leaders,
    label_rolling_leaders,
    label_future_leaders,
    score_leader_recall,
)


def test_membership_is_point_in_time_and_rejects_duplicate_transitions() -> None:
    universe = PointInTimeUniverse.from_rows(
        [
            {"effective_date": "2024-01-01", "ticker": "AAA", "member": "true"},
            {"effective_date": "2025-01-01", "ticker": "AAA", "member": "false"},
            {"effective_date": "2025-06-01", "ticker": "BBB", "member": "1"},
        ]
    )
    assert universe.members_at("2024-12-31") == frozenset({"AAA"})
    assert universe.members_at("2025-02-01") == frozenset()
    assert universe.members_at("2025-07-01") == frozenset({"BBB"})
    with pytest.raises(ValueError, match="duplicate"):
        PointInTimeUniverse(
            (
                MembershipEvent(date(2024, 1, 1), "AAA", True),
                MembershipEvent(date(2024, 1, 1), "AAA", False),
            )
        )


def test_future_leaders_can_be_labeled_by_later_index_membership() -> None:
    dates = pd.bdate_range("2024-01-01", periods=4)
    closes = pd.DataFrame(
        {"AAA": [100.0, 101.0, 102.0, 140.0], "BBB": [100.0, 100.0, 100.0, 110.0]},
        index=dates,
    )
    universe = PointInTimeUniverse.from_rows(
        [{"effective_date": "2024-01-03", "ticker": "AAA", "member": "true"}]
    )
    labels = label_future_leaders(
        closes,
        dates[0].date(),
        forward_trading_days=3,
        top_n=2,
        membership=universe,
    )
    assert labels[0].ticker == "AAA"
    assert labels[0].future_index_member is True
    assert labels[0].forward_return_pct == pytest.approx(40.0)


def test_recall_report_counts_only_signals_before_labeled_leaders() -> None:
    labels = (
        # The index confirmation is an outcome label, not an eligibility rule.
        LeaderLabel(date(2024, 1, 10), date(2024, 2, 10), "AAA", 50.0, 1, True),
        LeaderLabel(date(2024, 1, 10), date(2024, 2, 10), "BBB", 40.0, 2, False),
    )
    signal_log = pd.DataFrame(
        [
            {"signal_date": "2024-01-05", "symbol": "AAA", "buy_signal": True},
            {"signal_date": "2024-01-05", "symbol": "CCC", "buy_signal": True},
        ]
    )
    report = score_leader_recall(signal_log, labels, lookback_days=10)
    assert report.future_index_leaders == 1
    assert report.leaders_recalled == 1
    assert report.recall_rate_pct == 100.0
    assert report.false_positive_signals == 1


def test_five_year_and_rolling_labels_preserve_membership_facts_without_changing_eligibility() -> None:
    """Break caught: outcome labels silently filtered late members or accepted short/missing history."""
    dates = pd.bdate_range("2021-01-01", periods=800)
    closes = pd.DataFrame({
        "SPY": range(100, 900),
        "AAA": range(100, 900),
        "BBB": range(100, 900),
        "CCC": range(100, 900),
        "DDD": [float("nan")] * 100 + list(range(100, 800)),
        "EEE": [float("nan")] * 22 + list(range(122, 900)),
    }, index=dates, dtype=float)
    universe = PointInTimeUniverse.from_rows([
        {"effective_date": "2021-01-01", "ticker": ticker, "member": "true"}
        for ticker in ("AAA", "CCC", "DDD", "EEE")
    ] + [
        {"effective_date": "2022-01-03", "ticker": "BBB", "member": "true"},
        {"effective_date": "2023-01-03", "ticker": "CCC", "member": "false"},
    ])
    contract = LeaderIdentityContract("0" * 64, "1" * 64, {
        ticker: LeaderPriceIdentity(ticker, dates[0].date(), dates[-1].date(), ticker, "standalone")
        for ticker in ("AAA", "BBB", "CCC", "DDD", "EEE")
    })
    leaders = label_five_year_leaders(closes, universe, start_date=dates[0].date(), end_date=dates[-1].date(), identity_contract=contract, top_n=10)
    assert {item.ticker for item in leaders} == {"AAA", "BBB", "CCC"}
    assert next(item for item in leaders if item.ticker == "BBB").member_at_start is False
    assert universe.members_at(dates[0].date()) == frozenset({"AAA", "CCC", "DDD", "EEE"})
    rolling = label_rolling_leaders(closes, universe, start_date=dates[0].date(), end_date=dates[-1].date(), identity_contract=contract, forward_trading_days=20, top_n=10)
    assert any(item.ticker == "BBB" and item.member_at_evaluation is False for item in rolling)
