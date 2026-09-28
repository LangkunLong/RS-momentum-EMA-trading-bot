"""Fixed-history checks for the accepted Historical-06 financial semantics."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.canslim.a_annual_earnings import evaluate_a_with_trace
from core.pit_data import PITDataBundle
from core.pit_feature_snapshot import (
    FINANCIAL_FEATURE_CALCULATOR_ID,
    _earnings_acceleration,
    _fundamental_age_days,
    _growth_acceleration_for_label,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


def test_financial_feature_calculator_identity_is_versioned() -> None:
    assert FINANCIAL_FEATURE_CALCULATOR_ID == "pit-financial-features-v2"


def _quarterly_history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            [0.8, 1.0, 1.0, 1.5],
            [80.0, 100.0, 100.0, 150.0],
        ],
        index=["Diluted EPS", "Total Revenue"],
        columns=pd.to_datetime(
            ["2023-06-30", "2024-06-30", "2023-12-31", "2024-12-31"]
        ),
    )


def test_acceleration_is_unavailable_when_an_intervening_quarter_is_missing() -> None:
    quarterly = _quarterly_history()

    # The two available YoY rates are 25% for Q2 and 50% for Q4. Their
    # difference is not adjacent-quarter acceleration because Q3 is absent.
    assert _earnings_acceleration(quarterly) is None
    assert _growth_acceleration_for_label(quarterly, "Total Revenue") is None


def test_acceleration_uses_the_two_newest_adjacent_quarters() -> None:
    quarterly = pd.DataFrame(
        [[0.8, 0.9, 1.0, 1.0, 1.2, 1.5]],
        index=["Diluted EPS"],
        columns=pd.to_datetime(
            [
                "2023-06-30",
                "2023-09-30",
                "2023-12-31",
                "2024-06-30",
                "2024-09-30",
                "2024-12-31",
            ]
        ),
    )

    # Q4 YoY is 50%, Q3 YoY is 33 1/3%; acceleration is their 16 2/3 pp delta.
    assert _earnings_acceleration(quarterly) == pytest.approx(1 / 6)


def test_annual_growth_trace_preserves_the_reported_period_gap() -> None:
    annual = pd.DataFrame(
        [[1.0, 1.2, 2.4]],
        index=["Diluted EPS"],
        columns=pd.to_datetime(["2020-12-31", "2021-12-31", "2023-12-31"]),
    )
    annual.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2020-12-31": "2021-02-15",
        "2021-12-31": "2022-02-15",
        "2023-12-31": "2024-02-15",
    }

    trace = evaluate_a_with_trace(annual)

    # #66 retains adjacent available annual observations. This is 100% across
    # two fiscal years, not a source-comparable one-year growth statistic.
    assert trace.annual_growth == pytest.approx(1.0)
    assert trace.current_period_end == date(2023, 12, 31)
    assert trace.prior_period_end == date(2021, 12, 31)


def test_roe_uses_latest_visible_equity_even_when_its_period_differs() -> None:
    annual = pd.DataFrame(
        [[1.0, 1.2], [100.0, 120.0]],
        index=["Diluted EPS", "Net Income"],
        columns=pd.to_datetime(["2023-12-31", "2024-12-31"]),
    )
    annual.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-12-31": "2024-02-15",
        "2024-12-31": "2025-02-15",
    }
    balance_sheet = pd.DataFrame(
        [[400.0, 800.0]],
        index=["Stockholders Equity"],
        columns=pd.to_datetime(["2023-12-31", "2024-03-31"]),
    )

    trace = evaluate_a_with_trace(annual, balance_sheet=balance_sheet)

    # #66 explicitly preserves latest annual net income / latest nonmissing
    # equity. The period mismatch is retained as a modeling limitation.
    assert trace.roe == pytest.approx(0.15)


def test_restatement_changes_only_snapshots_after_its_public_date() -> None:
    records = [
        {
            "statement_type": "annual",
            "period_end": "2023-12-31",
            "public_date": "2024-02-15",
            "diluted_eps": 1.0,
        },
        {
            "statement_type": "annual",
            "period_end": "2024-12-31",
            "public_date": "2025-02-15",
            "diluted_eps": 1.4,
        },
        {
            "statement_type": "annual",
            "period_end": "2023-12-31",
            "public_date": "2025-03-01",
            "diluted_eps": 0.8,
        },
    ]
    before = PITDataBundle._statement_frame(
        [record for record in records if record["public_date"] <= "2025-02-28"],
        "annual",
        include_provenance=True,
    )
    after = PITDataBundle._statement_frame(
        [record for record in records if record["public_date"] <= "2025-03-02"],
        "annual",
        include_provenance=True,
    )

    before_trace = evaluate_a_with_trace(before)
    after_trace = evaluate_a_with_trace(after)

    assert before_trace.annual_growth == pytest.approx(0.40)
    assert after_trace.annual_growth == pytest.approx(0.75)
    assert before_trace.prior_public_date == date(2024, 2, 15)
    assert after_trace.prior_public_date == date(2025, 3, 1)


def test_freshness_ignores_a_period_with_no_observed_financial_values() -> None:
    quarterly = pd.DataFrame(
        [[1.2, None], [100.0, None]],
        index=["Diluted EPS", "Total Revenue"],
        columns=pd.to_datetime(["2024-06-30", "2024-09-30"]),
    )
    quarterly.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2024-06-30": "2024-08-01",
        "2024-09-30": "2024-11-01",
    }

    assert _fundamental_age_days(quarterly, date(2024, 12, 1)) == 122


def test_freshness_is_unavailable_when_no_quarterly_metric_is_observed() -> None:
    quarterly = pd.DataFrame(
        [[None, None]],
        index=["Diluted EPS"],
        columns=pd.to_datetime(["2024-06-30", "2024-09-30"]),
    )
    quarterly.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2024-06-30": "2024-08-01",
        "2024-09-30": "2024-11-01",
    }

    assert _fundamental_age_days(quarterly, date(2024, 12, 1)) is None


def test_freshness_fails_closed_when_an_observed_period_lacks_public_date() -> None:
    quarterly = pd.DataFrame(
        [[1.2]],
        index=["Diluted EPS"],
        columns=pd.to_datetime(["2024-06-30"]),
    )
    quarterly.attrs[PIT_PUBLIC_DATES_ATTR] = {}

    with pytest.raises(ValueError, match="no public date for 2024-06-30"):
        _fundamental_age_days(quarterly, date(2024, 8, 6))


def test_freshness_does_not_shift_an_already_normalized_available_date() -> None:
    quarterly = pd.DataFrame(
        [[1.2]],
        index=["Diluted EPS"],
        columns=pd.to_datetime(["2024-06-30"]),
    )
    # This mapping is the retained bundle contract: public_date is already the
    # first usable exchange session, derived from the SEC acceptance date.
    quarterly.attrs[PIT_PUBLIC_DATES_ATTR] = {"2024-06-30": "2024-08-05"}

    assert _fundamental_age_days(quarterly, date(2024, 8, 6)) == 1
