from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from core.backtest_engine import CanslimStrategy
from core.canslim.i_institutional import evaluate_i
from core.pit_data import PITDataBundle
from core.strategy_policy.runtime import InProcessPolicyClient
from core.strategy_policy import (
    BenchmarkContextV1,
    EntryDecision,
    EntrySnapshot,
    MarketContextV1,
)


def _institutional_row(
    ticker: str,
    period_end: str,
    public_date: str,
    *,
    shares: float | None,
    ownership: float | None,
    current: int | None,
    previous: int | None,
) -> tuple[object, ...]:
    return (
        ticker,
        "institutional",
        period_end,
        public_date,
        None,
        None,
        None,
        None,
        None,
        None,
        shares,
        ownership,
        current,
        previous,
    )


@pytest.fixture
def lineage_bundle():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, "
        "period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, "
        "diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, "
        "total_stockholders_equity REAL, shares_outstanding REAL, "
        "held_percent_institutions REAL, institution_count INTEGER, "
        "prev_institution_count INTEGER)"
    )
    connection.executemany(
        "INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _institutional_row(
                "OLD",
                "2024-03-31",
                "2024-05-15",
                shares=1_000_000,
                ownership=0.35,
                current=10,
                previous=None,
            ),
            _institutional_row(
                "NEW",
                "2024-06-30",
                "2024-08-16",
                shares=None,
                ownership=None,
                current=12,
                previous=10,
            ),
        ],
    )
    bundle = PITDataBundle.__new__(PITDataBundle)
    bundle._connection = connection
    bundle.metadata = {"schema_version": "3", "data_cutoff": "2024-12-31"}
    bundle._price_symbols = frozenset({"OLD", "NEW"})
    bundle._tradable_symbols = frozenset({"OLD", "NEW"})
    bundle._security_lineage_ids = {"OLD": "issuer_a", "NEW": "issuer_a"}
    try:
        yield bundle
    finally:
        connection.close()


def _market_context(session: str) -> MarketContextV1:
    return MarketContextV1(
        schema_version=1,
        session=session,
        oneil_regime="confirmed_uptrend",
        distribution_days=0,
        follow_through=False,
        benchmarks=tuple(
            BenchmarkContextV1(symbol, 1.0, 1.0, 0.2)
            for symbol in ("SPY", "QQQ", "IWM")
        ),
        active_constituent_count=1,
        breadth_above_50_fraction=1.0,
        breadth_50_coverage_fraction=1.0,
        breadth_above_200_fraction=1.0,
        breadth_200_coverage_fraction=1.0,
        median_rs_score=95.0,
        rs_at_least_80_fraction=1.0,
        rs_coverage_fraction=1.0,
    )


class _CapturingPolicyClient(InProcessPolicyClient):
    def __init__(self) -> None:
        self.snapshot: EntrySnapshot | None = None

    def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision:
        self.snapshot = snapshot
        return EntryDecision(False, True, (None, None), ())


def test_new_institutional_missingness_does_not_backfill_older_ownership() -> None:
    records = [
        {
            "statement_type": "institutional",
            "shares_outstanding": 1_000_000,
            "held_percent_institutions": 0.35,
            "institution_count": 10,
            "prev_institution_count": None,
        },
        {
            "statement_type": "institutional",
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": 12,
            "prev_institution_count": 10,
        },
    ]

    info = PITDataBundle._company_info(records)

    assert info["shares_outstanding"] == 1_000_000
    assert info["held_percent_institutions"] is None
    assert info["institution_count"] == 12
    assert info["prev_institution_count"] == 10


def test_v3_reader_carries_only_visible_institutional_history_across_rename(
    lineage_bundle: PITDataBundle,
) -> None:
    before_publication = lineage_bundle.fundamentals_as_of(
        "NEW", pd.Timestamp("2024-08-15")
    )["company_info"]
    after_publication = lineage_bundle.fundamentals_as_of(
        "NEW", pd.Timestamp("2024-08-16")
    )["company_info"]

    assert before_publication["held_percent_institutions"] == pytest.approx(0.35)
    assert before_publication["institution_count"] == 10
    assert before_publication["prev_institution_count"] is None

    assert after_publication["held_percent_institutions"] is None
    assert after_publication["institution_count"] == 12
    assert after_publication["prev_institution_count"] == 10
    assert after_publication["shares_outstanding"] == 1_000_000


def test_policy_input_snapshot_receives_bundle_i_score_across_rename(
    lineage_bundle: PITDataBundle,
) -> None:
    policy = _CapturingPolicyClient()
    strategy = CanslimStrategy(fundamental_provider=lineage_bundle.fundamentals_provider)
    strategy._policy_client_provider = lambda: policy

    close = np.linspace(40.0, 52.0, 70)
    prices = pd.DataFrame(
        {
            "Open": close * 0.998,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(len(close), 1_000_000.0),
        },
        index=pd.bdate_range(end="2024-08-16", periods=len(close)),
    )
    session = prices.index[-1]
    signal = strategy.evaluate_symbol(
        ticker="NEW",
        ticker_ohlcv={"NEW": prices},
        all_closes=pd.DataFrame({"NEW": prices["Close"]}),
        eval_date=session,
        market_state={
            "market": _market_context(session.date().isoformat()),
            "m_score": 1.0,
            "market_is_bullish": True,
        },
        rs_score=95.0,
    )

    assert signal is not None
    assert policy.snapshot is not None
    expected_i = evaluate_i(None, 12, 10)
    assert signal["i_score"] == pytest.approx(expected_i)
    assert policy.snapshot.i_score == pytest.approx(expected_i)
    assert policy.snapshot.institutional_data_available is True


def test_fundamental_state_boundaries_include_alias_publication_changes(
    lineage_bundle: PITDataBundle,
) -> None:
    states = list(
        lineage_bundle.iter_fundamental_state_boundaries(
            {"NEW": (pd.Timestamp("2024-05-15"), pd.Timestamp("2024-08-16"))}
        )
    )

    assert [(ticker, when.isoformat()) for ticker, when, _ in states] == [
        ("NEW", "2024-05-15"),
        ("NEW", "2024-08-16"),
    ]
    assert states[0][2]["company_info"]["institution_count"] == 10
    assert states[1][2]["company_info"]["institution_count"] == 12
    assert states[1][2]["company_info"]["held_percent_institutions"] is None
