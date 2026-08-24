"""Focused regressions for fiscal-period and adapter input parity."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from backtest import _evaluate_fundamentals_at_date, _evaluate_technical_at_date
from core.backtest_engine import CanslimStrategy
from core.canslim.a_annual_earnings import evaluate_a
from core.canslim.c_current_earnings import evaluate_c
from core.canslim.i_institutional import _score_ownership_level, evaluate_i
from core.canslim.m_market_direction import MarketTrend
from core.canslim.n_new_products import evaluate_n
from core.canslim.core import evaluate_canslim
from core.data_client import (
    _FMP_INCOME_FIELD_MAP,
    _filter_records_as_of,
    _fmp_records_to_financial_df,
    _fund_cache_set,
    clear_session_cache,
    fetch_quarterly_income_statement,
)
from core.pit_data import PITDataBundle


def _quarterly_frame(
    periods: list[object],
    *,
    eps: list[float],
    revenue: list[float],
) -> pd.DataFrame:
    return pd.DataFrame(
        [eps, revenue],
        index=["Diluted EPS", "Total Revenue"],
        columns=pd.Index([pd.Timestamp(period) for period in periods], dtype=object),
    )


def _growths(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    _c_score, eps_growth = evaluate_c(frame)
    _n_score, revenue_growth = evaluate_n(frame, proximity_to_high=0.98)
    return eps_growth, revenue_growth


@pytest.mark.parametrize("reverse_rows", [False, True])
def test_c_uses_explicit_diluted_then_basic_then_net_income_priority(
    reverse_rows: bool,
) -> None:
    """Break caught: PIT/FMP row order silently changed which EPS series C used."""
    frame = pd.DataFrame(
        {
            pd.Timestamp("2023-07-31"): [1.0, 2.0, 10.0],
            pd.Timestamp("2024-07-31"): [1.1, 3.0, 20.0],
        },
        index=["Basic EPS", "Diluted EPS", "Net Income"],
    )
    if reverse_rows:
        frame = frame.iloc[::-1]

    _score, growth = evaluate_c(frame)

    assert growth == pytest.approx(0.5)


def test_a_has_adapter_parity_when_basic_growth_would_flip_the_target() -> None:
    """Break caught: opposite adapter row order made A choose Basic instead of Diluted EPS."""
    fmp_records = [
        {
            "date": "2023-12-31",
            "acceptedDate": "2024-02-15",
            "eps": 1.0,
            "epsDiluted": 1.0,
            "netIncome": 100.0,
        },
        {
            "date": "2024-12-31",
            "acceptedDate": "2025-02-15",
            "eps": 1.1,
            "epsDiluted": 1.3,
            "netIncome": 200.0,
        },
    ]
    pit_records = [
        {
            "statement_type": "annual",
            "period_end": record["date"],
            "public_date": record["acceptedDate"],
            "basic_eps": record["eps"],
            "diluted_eps": record["epsDiluted"],
            "total_revenue": None,
            "net_income": record["netIncome"],
            "common_stock": None,
            "total_stockholders_equity": None,
        }
        for record in fmp_records
    ]
    fmp_frame = _fmp_records_to_financial_df(fmp_records, _FMP_INCOME_FIELD_MAP)
    pit_frame = PITDataBundle._statement_frame(pit_records, "annual")

    fmp_score, fmp_growth, _fmp_roe = evaluate_a(fmp_frame)
    pit_score, pit_growth, _pit_roe = evaluate_a(pit_frame)

    assert fmp_growth == pytest.approx(0.30)
    assert pit_growth == pytest.approx(0.30)
    assert fmp_score == pytest.approx(pit_score)


@pytest.mark.parametrize(
    ("labels", "prior_values", "current_values", "expected_growth"),
    [
        (["Net Income", "Basic EPS"], [10.0, 1.0], [20.0, 1.2], 0.20),
        (["Net Income"], [10.0], [15.0], 0.50),
    ],
)
def test_a_falls_back_from_diluted_to_basic_then_net_income(
    labels: list[str],
    prior_values: list[float],
    current_values: list[float],
    expected_growth: float,
) -> None:
    """Break caught: A skipped Basic EPS or failed to use Net Income as a last resort."""
    frame = pd.DataFrame(
        {
            pd.Timestamp("2023-12-31"): prior_values,
            pd.Timestamp("2024-12-31"): current_values,
        },
        index=labels,
    )

    _score, growth, _roe = evaluate_a(frame)

    assert growth == pytest.approx(expected_growth)


def test_sparse_july_periods_pair_by_fiscal_date_instead_of_column_position() -> None:
    """Break caught: sparse history paired July with an adjacent fiscal quarter."""
    frame = _quarterly_frame(
        ["2023-07-31", "2023-10-31", "2024-01-31", "2024-07-27"],
        eps=[1.0, 8.0, 9.0, 1.5],
        revenue=[100.0, 800.0, 900.0, 140.0],
    )

    eps_growth, revenue_growth = _growths(frame)

    assert eps_growth == pytest.approx(0.5)
    assert revenue_growth == pytest.approx(0.4)


def test_missing_same_quarter_does_not_fall_back_to_adjacent_or_older_current() -> None:
    """Break caught: latest July fell back to April or to April's valid YoY pair."""
    frame = _quarterly_frame(
        ["2023-04-30", "2023-10-31", "2024-04-30", "2024-07-31"],
        eps=[1.0, 7.0, 1.5, 2.0],
        revenue=[100.0, 700.0, 150.0, 200.0],
    )

    assert _growths(frame) == (None, None)


@pytest.mark.parametrize(
    ("prior_period", "current_period", "expected"),
    [
        ("2022-12-31", "2024-01-06", 0.5),  # 53-week fiscal year crossing year-end
        ("2023-02-28", "2024-02-29", 0.5),  # leap-day target clamps to Feb. 28
        ("2023-07-03", "2024-07-31", 0.5),  # inclusive 28-day boundary
        ("2023-07-02", "2024-07-31", None),  # 29 days is outside the contract
    ],
)
def test_fiscal_yoy_calendar_edge_contract(
    prior_period: str,
    current_period: str,
    expected: float | None,
) -> None:
    """Break caught: positional/365-day matching mishandled fiscal calendar edges."""
    frame = _quarterly_frame(
        [prior_period, current_period],
        eps=[1.0, 1.5],
        revenue=[100.0, 150.0],
    )

    eps_growth, revenue_growth = _growths(frame)

    if expected is None:
        assert eps_growth is None
        assert revenue_growth is None
    else:
        assert eps_growth == pytest.approx(expected)
        assert revenue_growth == pytest.approx(expected)


def test_fiscal_matching_preserves_each_timestamp_local_date() -> None:
    """Break caught: UTC conversion moved a fiscal date across the 28-day boundary."""
    frame = _quarterly_frame(
        ["2023-07-31 23:30:00-04:00", "2024-07-03 00:30:00+14:00"],
        eps=[1.0, 1.5],
        revenue=[100.0, 150.0],
    )

    eps_growth, revenue_growth = _growths(frame)

    assert eps_growth == pytest.approx(0.5)
    assert revenue_growth == pytest.approx(0.5)


def test_equal_distance_prior_periods_are_ambiguous_and_fail_closed() -> None:
    """Break caught: an input-order tie silently selected one ±28-day candidate."""
    frame = _quarterly_frame(
        ["2023-07-03", "2023-08-28", "2024-07-31"],
        eps=[1.0, 2.0, 3.0],
        revenue=[100.0, 200.0, 300.0],
    )

    assert _growths(frame) == (None, None)


@pytest.mark.parametrize("duplicate_values", [(1.0, 1.0), (1.0, 1.1)])
def test_exact_duplicate_periods_must_agree_before_they_can_match(
    duplicate_values: tuple[float, float],
) -> None:
    """Break caught: conflicting exact period duplicates were selected by order."""
    first, second = duplicate_values
    frame = pd.DataFrame(
        [
            [first, second, 1.5],
            [first * 100.0, second * 100.0, 150.0],
        ],
        index=["Diluted EPS", "Total Revenue"],
        columns=[
            pd.Timestamp("2023-07-31"),
            pd.Timestamp("2023-07-31"),
            pd.Timestamp("2024-07-31"),
        ],
    )

    eps_growth, revenue_growth = _growths(frame)

    if first == second:
        assert eps_growth == pytest.approx(0.5)
        assert revenue_growth == pytest.approx(0.5)
    else:
        assert eps_growth is None
        assert revenue_growth is None


def test_latest_nonfinite_period_does_not_fall_back_to_an_older_current_period() -> None:
    """Break caught: a latest NaN caused N to emit NaN or reuse April's valid YoY."""
    frame = _quarterly_frame(
        ["2023-04-30", "2023-07-31", "2024-04-30", "2024-07-31"],
        eps=[1.0, 1.0, 1.5, np.nan],
        revenue=[100.0, 100.0, 150.0, np.nan],
    )

    assert _growths(frame) == (None, None)


@pytest.mark.parametrize("newest_first", [False, True])
def test_fmp_shuffled_amendments_select_latest_visible_revision(
    newest_first: bool,
) -> None:
    """Break caught: FMP amendment choice depended on provider input ordering."""
    old = {
        "date": "2024-07-31",
        "acceptedDate": "2024-08-10 16:00:00",
        "epsDiluted": 1.0,
        "revenue": 100.0,
    }
    new = {
        "date": "2024-07-31",
        "acceptedDate": "2024-08-15 16:00:00",
        "epsDiluted": 1.5,
        "revenue": 150.0,
    }
    records = [new, old] if newest_first else [old, new]

    frame = _fmp_records_to_financial_df(records, _FMP_INCOME_FIELD_MAP)

    assert frame.loc["Diluted EPS", pd.Timestamp("2024-07-31")] == 1.5
    assert frame.loc["Total Revenue", pd.Timestamp("2024-07-31")] == 150.0


def test_fmp_conflicting_revisions_without_ordering_fail_closed() -> None:
    """Break caught: unordered conflicting revisions used whichever record came last."""
    frame = _fmp_records_to_financial_df(
        [
            {"date": "2024-07-31", "epsDiluted": 1.0, "revenue": 100.0},
            {"date": "2024-07-31", "epsDiluted": 1.5, "revenue": 150.0},
        ],
        _FMP_INCOME_FIELD_MAP,
    )

    assert frame.empty


def test_fmp_latest_nan_wins_without_backfilling_the_superseded_value() -> None:
    """Break caught: the older revision's revenue leaked through a latest NaN."""
    frame = _fmp_records_to_financial_df(
        [
            {
                "date": "2023-07-31",
                "acceptedDate": "2023-08-10 16:00:00",
                "epsDiluted": 1.0,
                "revenue": 100.0,
            },
            {
                "date": "2024-07-31",
                "acceptedDate": "2024-08-10 16:00:00",
                "epsDiluted": 1.4,
                "revenue": 140.0,
            },
            {
                "date": "2024-07-31",
                "acceptedDate": "2024-08-15 16:00:00",
                "epsDiluted": 1.5,
                "revenue": None,
            },
        ],
        _FMP_INCOME_FIELD_MAP,
    )

    _score, revenue_growth = evaluate_n(frame, proximity_to_high=0.98)

    assert frame.loc["Diluted EPS", pd.Timestamp("2024-07-31")] == 1.5
    assert pd.isna(frame.loc["Total Revenue", pd.Timestamp("2024-07-31")])
    assert revenue_growth is None


@pytest.mark.parametrize("filing_field", ["filingDate", "fillingDate", "filedDate"])
def test_fmp_pit_record_becomes_visible_on_its_filing_date(
    filing_field: str,
) -> None:
    """Break caught: filing-only data became visible on its fiscal period end."""
    record = {
        "date": "2024-07-31",
        filing_field: "2024-08-10 16:00:00",
        "revenue": 150.0,
    }

    before = _filter_records_as_of([record], pd.Timestamp("2024-08-09"))
    on_filing_date = _filter_records_as_of([record], pd.Timestamp("2024-08-10"))

    assert before == []
    assert on_filing_date == [record]


def test_fmp_pit_visibility_falls_back_from_malformed_accepted_to_filing_date() -> None:
    """Break caught: malformed acceptedDate hid an otherwise ordered filing forever."""
    record = {
        "date": "2024-07-31",
        "acceptedDate": "not-a-timestamp",
        "filingDate": "2024-08-10 16:00:00",
        "revenue": 150.0,
    }

    before = _filter_records_as_of([record], pd.Timestamp("2024-08-09"))
    on_filing_date = _filter_records_as_of([record], pd.Timestamp("2024-08-10"))

    assert before == []
    assert on_filing_date == [record]


def test_fmp_pit_prefers_valid_accepted_date_over_earlier_filing_date() -> None:
    """Break caught: a valid filingDate overrode the authoritative acceptedDate."""
    record = {
        "date": "2024-07-31",
        "acceptedDate": "2024-08-15 16:00:00",
        "filingDate": "2024-08-10 16:00:00",
        "revenue": 150.0,
    }

    before_acceptance = _filter_records_as_of([record], pd.Timestamp("2024-08-10"))
    on_acceptance_date = _filter_records_as_of([record], pd.Timestamp("2024-08-15"))

    assert before_acceptance == []
    assert on_acceptance_date == [record]


def test_fmp_pit_excludes_record_without_a_public_timestamp() -> None:
    """Break caught: an undated filing became visible on its fiscal period end."""
    record = {
        "date": "2024-07-31",
        "revenue": 150.0,
    }

    assert _filter_records_as_of([record], pd.Timestamp("2025-12-31")) == []


def test_fmp_non_pit_adapter_keeps_a_single_record_without_filing_metadata() -> None:
    """Break caught: tightening PIT visibility also removed current-fetch compatibility."""
    record = {
        "date": "2024-07-31",
        "revenue": 150.0,
    }

    frame = _fmp_records_to_financial_df([record], _FMP_INCOME_FIELD_MAP)

    assert frame.loc["Total Revenue", pd.Timestamp("2024-07-31")] == pytest.approx(150.0)


def test_quarterly_fetch_does_not_reuse_pre_revision_selection_disk_cache(
    tmp_path: Path,
) -> None:
    """Break caught: deploys reused frames built by the old order-dependent parser."""
    clear_session_cache()
    stale = pd.DataFrame(
        {pd.Timestamp("2024-07-31"): [100.0]},
        index=["Total Revenue"],
    )
    old_cache_key = ("quarterly_income", "CACHEPARITY", 5)
    records = [
        {
            "date": "2024-07-31",
            "acceptedDate": "2024-08-15 16:00:00",
            "revenue": 150.0,
        }
    ]

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(old_cache_key, stale)
        with patch("core.data_client._fmp_get", return_value=records):
            result = fetch_quarterly_income_statement("CACHEPARITY", limit=5)

    assert result.loc["Total Revenue", pd.Timestamp("2024-07-31")] == 150.0


def _prices(*, proximity_to_high: float = 1.0) -> pd.DataFrame:
    dates = pd.date_range("2023-09-01", "2024-08-15", freq="B")
    close = np.linspace(80.0, 100.0, len(dates))
    if proximity_to_high < 1.0:
        close[-10] = close[-1] / proximity_to_high
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(len(dates), 1_000_000.0),
        },
        index=dates,
    )


def _fundamentals(quarterly_income: pd.DataFrame) -> dict[str, object]:
    return {
        "quarterly_income": quarterly_income,
        "annual_income": pd.DataFrame(),
        "balance_sheet": pd.DataFrame(),
        "company_info": {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": 110,
            "prev_institution_count": 100,
        },
    }


def test_live_simple_and_pit_use_the_same_n_and_i_inputs() -> None:
    """Break caught: simple/PIT dropped quarterly revenue and live/simple dropped prior I."""
    prices = _prices(proximity_to_high=0.94)
    eval_date = prices.index[-1]
    quarterly_income = _quarterly_frame(
        ["2023-07-31", "2024-07-27"],
        eps=[1.0, 1.5],
        revenue=[100.0, 110.0],
    )
    raw = _fundamentals(quarterly_income)
    proximity = float(prices["Close"].iloc[-1] / prices["Close"].max())
    direct_n, direct_growth = evaluate_n(quarterly_income, proximity_to_high=proximity)
    price_only_n, _missing_growth = evaluate_n(pd.DataFrame(), proximity_to_high=proximity)

    with patch("backtest.fetch_fundamental_data_as_of", return_value=raw):
        simple_fund = _evaluate_fundamentals_at_date("AAA", eval_date)
    with patch("core.backtest_engine._evaluate_fundamentals_at_date", return_value=simple_fund):
        simple = CanslimStrategy().evaluate_symbol(
            ticker="AAA",
            ticker_ohlcv={"AAA": prices},
            all_closes=pd.DataFrame({"AAA": prices["Close"]}),
            eval_date=eval_date,
            market_state={"m_score": 1.0, "market_is_bullish": True},
            rs_score=95.0,
        )
    pit = CanslimStrategy(fundamental_provider=lambda _symbol, _date: raw).evaluate_symbol(
        ticker="AAA",
        ticker_ohlcv={"AAA": prices},
        all_closes=pd.DataFrame({"AAA": prices["Close"]}),
        eval_date=eval_date,
        market_state={"m_score": 1.0, "market_is_bullish": True},
        rs_score=95.0,
    )
    market = MarketTrend("SPY", 1.0, True, 100.0, {})
    with (
        patch("core.canslim.core.fetch_ohlcv", return_value=prices),
        patch("core.canslim.core.fetch_company_info", return_value=raw["company_info"]),
        patch("core.canslim.core.fetch_quarterly_income_statement", return_value=quarterly_income),
        patch("core.canslim.core.fetch_annual_income_statement", return_value=pd.DataFrame()),
        patch("core.canslim.core.fetch_balance_sheet", return_value=pd.DataFrame()),
    ):
        live = evaluate_canslim(
            "AAA",
            rs_scores_df=pd.DataFrame({"Ticker": ["AAA"], "RS_Score": [95.0]}),
            market_trend=market,
        )

    assert direct_growth == pytest.approx(0.1)
    assert direct_n != pytest.approx(price_only_n)
    assert simple is not None
    assert pit is not None
    assert live is not None
    assert simple["n_score"] == pytest.approx(direct_n)
    assert pit["n_score"] == pytest.approx(direct_n)
    assert live["scores"]["N"] == pytest.approx(direct_n)
    assert simple["i_score"] == pytest.approx(1.0)
    assert pit["i_score"] == pytest.approx(1.0)
    assert live["scores"]["I"] == pytest.approx(1.0)
    assert live["data_availability"]["I_trend"] is True


def test_technical_only_remains_price_only_and_never_calls_fundamentals() -> None:
    """Break caught: N parity made technical-only invoke the fundamental provider."""
    prices = _prices()

    def forbidden_provider(_symbol: str, _date: pd.Timestamp) -> dict[str, object]:
        raise AssertionError("technical-only must not request fundamentals")

    row = CanslimStrategy(
        technical_only=True,
        fundamental_provider=forbidden_provider,
    ).evaluate_symbol(
        ticker="AAA",
        ticker_ohlcv={"AAA": prices},
        all_closes=pd.DataFrame({"AAA": prices["Close"]}),
        eval_date=prices.index[-1],
        market_state={"m_score": 1.0, "market_is_bullish": True},
        rs_score=95.0,
    )

    expected_n, expected_growth = evaluate_n(pd.DataFrame(), proximity_to_high=1.0)
    assert expected_growth is None
    assert row is not None
    assert row["n_score"] == pytest.approx(expected_n)


@contextmanager
def _pit_bundle_with_revenue_timing(path: Path):
    connection = sqlite3.connect(path)
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
            (
                "AAA", "quarterly", "2023-07-31", "2023-08-10", 1.0, None,
                100.0, None, None, None, None, None, None, None,
            ),
            (
                "AAA", "quarterly", "2024-07-27", "2024-08-15", 1.5, None,
                150.0, None, None, None, None, None, None, None,
            ),
        ],
    )
    connection.commit()
    bundle = PITDataBundle.__new__(PITDataBundle)
    bundle._connection = connection
    bundle.metadata = {"data_cutoff": "2024-12-31"}
    try:
        yield bundle
    finally:
        connection.close()


def test_pit_n_uses_revenue_only_on_and_after_its_public_date(tmp_path: Path) -> None:
    """Break caught: PIT N ignored public timing or failed to receive its as-of frame."""
    prices = _prices(proximity_to_high=0.94)
    with _pit_bundle_with_revenue_timing(tmp_path / "pit.sqlite3") as bundle:
        before = bundle.fundamentals_as_of("AAA", pd.Timestamp("2024-08-14"))
        after = bundle.fundamentals_as_of("AAA", pd.Timestamp("2024-08-15"))

    before_technical = _evaluate_technical_at_date(
        prices,
        pd.Timestamp("2024-08-14"),
        None,
        quarterly_income=before["quarterly_income"],
    )
    after_technical = _evaluate_technical_at_date(
        prices,
        pd.Timestamp("2024-08-15"),
        None,
        quarterly_income=after["quarterly_income"],
    )

    assert before_technical is not None
    assert after_technical is not None
    price_only_n, _growth = evaluate_n(pd.DataFrame(), before_technical["proximity"])
    full_n, revenue_growth = evaluate_n(after["quarterly_income"], after_technical["proximity"])
    price_only_after, _missing_growth = evaluate_n(
        pd.DataFrame(),
        after_technical["proximity"],
    )
    assert before_technical["n_score"] == pytest.approx(price_only_n)
    assert revenue_growth == pytest.approx(0.5)
    assert full_n != pytest.approx(price_only_after)
    assert after_technical["n_score"] == pytest.approx(full_n)


def test_i_trend_requires_both_counts_while_level_remains_independently_available() -> None:
    """Break caught: a lone holder count activated a neutral trend and changed level weight."""
    level_only = evaluate_i(0.40, 110, None)

    assert level_only == pytest.approx(_score_ownership_level(0.40))
    assert evaluate_i(None, 110, None) == pytest.approx(0.5)
    assert evaluate_i(None, 110, 100) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("latest_current", "latest_previous"),
    [(110, None), (None, 100)],
)
def test_pit_company_info_never_synthesizes_an_institutional_pair_across_rows(
    latest_current: int | None,
    latest_previous: int | None,
) -> None:
    """Break caught: PIT independently backfilled half of I from an older snapshot."""
    records = [
        {
            "shares_outstanding": 1_000_000,
            "held_percent_institutions": 0.35,
            "institution_count": 90,
            "prev_institution_count": 80,
        },
        {
            "shares_outstanding": None,
            "held_percent_institutions": 0.40,
            "institution_count": latest_current,
            "prev_institution_count": latest_previous,
        },
    ]

    info = PITDataBundle._company_info(records)

    assert info["institution_count"] == latest_current
    assert info["prev_institution_count"] == latest_previous
    assert info["shares_outstanding"] == 1_000_000
    assert info["held_percent_institutions"] == pytest.approx(0.40)


@pytest.mark.parametrize(
    ("current", "previous", "available"),
    [(110, 100, True), (110, None, False), (None, 100, False), (None, None, False)],
)
def test_simple_i_availability_requires_current_and_prior_counts(
    current: int | None,
    previous: int | None,
    available: bool,
) -> None:
    """Break caught: simple mode exposed a trend component from one-sided/blank SEC-v1 I."""
    raw = {
        "quarterly_income": pd.DataFrame(),
        "annual_income": pd.DataFrame(),
        "balance_sheet": pd.DataFrame(),
        "company_info": {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": current,
            "prev_institution_count": previous,
        },
    }

    with patch("backtest.fetch_fundamental_data_as_of", return_value=raw):
        result = _evaluate_fundamentals_at_date("AAA", pd.Timestamp("2024-08-15"))

    assert result["institutional_data_available"] is available
    assert result["i_score"] == pytest.approx(1.0 if available else 0.5)
