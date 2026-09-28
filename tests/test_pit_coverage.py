from __future__ import annotations

from datetime import date

import pandas as pd

from core.pit_coverage import (
    FEATURE_SPECS,
    _annual_revenue_state,
    _cell,
    _quarterly_revenue_state,
    _accumulate_cell,
    _finalize_accumulator,
    _new_accumulator,
    classify_lookback,
    resolve_asof_classification,
    resolve_asof_observation,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


def test_next_session_availability_and_restatement_keep_earlier_vintage() -> None:
    observations = (
        {
            "period_end": "2023-12-31",
            "source_public_date": "2024-01-02",
            "available_from_session": "2024-01-03",
            "value": 10.0,
            "revision": "original",
        },
        {
            "period_end": "2023-12-31",
            "source_public_date": "2024-04-01",
            "available_from_session": "2024-04-02",
            "value": 12.0,
            "revision": "restatement",
        },
    )

    assert resolve_asof_observation(observations, date(2024, 1, 2)) is None
    original = resolve_asof_observation(observations, date(2024, 1, 3))
    before_restatement = resolve_asof_observation(observations, date(2024, 4, 1))
    after_restatement = resolve_asof_observation(observations, date(2024, 4, 2))

    assert original is not None and original["value"] == 10.0
    assert before_restatement is not None and before_restatement["revision"] == "original"
    assert after_restatement is not None and after_restatement["value"] == 12.0


def test_late_classification_requires_public_and_effective_dates() -> None:
    classifications = (
        {
            "effective_date": "2024-01-01",
            "source_public_date": "2024-01-09",
            "available_from_session": "2024-01-10",
            "group_id": "industry-a",
        },
        {
            "effective_date": "2024-02-01",
            "source_public_date": "2024-02-05",
            "available_from_session": "2024-02-06",
            "group_id": "industry-b",
        },
    )

    assert resolve_asof_classification(classifications, date(2024, 1, 9)) is None
    jan_assignment = resolve_asof_classification(classifications, date(2024, 1, 10))
    assert jan_assignment is not None and jan_assignment["group_id"] == "industry-a"
    feb_assignment = resolve_asof_classification(classifications, date(2024, 2, 6))
    assert feb_assignment is not None and feb_assignment["group_id"] == "industry-b"


def test_warmup_status_uses_required_history_without_imputation() -> None:
    assert classify_lookback(49, 50) == (False, "insufficient_history")
    assert classify_lookback(50, 50) == (True, None)


def test_latest_unmatched_quarter_is_not_replaced_by_an_older_match() -> None:
    history = pd.DataFrame(
        [[100.0, 115.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2023-06-30", "2024-09-30"]),
    )
    history.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-06-30": "2023-08-02",
        "2024-09-30": "2024-11-01",
    }

    state = _quarterly_revenue_state(history)

    assert state["calculable"] is False
    assert state["lookback_ready"] is False
    assert state["reason"] == "missing_comparable_period"


def test_annual_revenue_report_calculates_adjacent_reported_periods() -> None:
    history = pd.DataFrame(
        [[100.0, 150.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2020-12-31", "2022-12-31"]),
    )
    history.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2020-12-31": "2021-02-15",
        "2022-12-31": "2023-02-15",
    }

    state = _annual_revenue_state(history)

    assert state["calculable"] is True
    assert state["lookback_ready"] is True
    assert state["selected_period_ends"] == ["2020-12-31", "2022-12-31"]
    assert state["selected_available_from_sessions"] == ["2021-02-15", "2023-02-15"]


def test_nonpositive_annual_revenue_comparator_is_invalid_not_zero_growth() -> None:
    history = pd.DataFrame(
        [[-10.0, 20.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2022-12-31", "2023-12-31"]),
    )

    state = _annual_revenue_state(history)

    assert state["calculable"] is False
    assert state["lookback_ready"] is True
    assert state["reason"] == "invalid_nonpositive_or_nonfinite_comparison"


def test_available_annual_revenue_is_not_misreported_as_a_baseline_policy_input() -> None:
    spec = next(spec for spec in FEATURE_SPECS if spec.feature_id == "annual_revenue_growth")
    cell = _cell(
        spec=spec,
        raw_count=2,
        visible_count=2,
        future_count=0,
        payload={
            "calculable": True,
            "lookback_ready": True,
            "required_history": 2,
            "available_history": 2,
            "reason": None,
            "selected_period_ends": ["2022-12-31", "2023-12-31"],
            "selected_available_from_sessions": ["2023-02-15", "2024-02-15"],
            "metric_family": None,
        },
    )

    assert cell["calculation_stage"] == "observed"
    assert cell["policy_input_stage"] == "intentionally_not_exposed"
    assert cell["policy_input_reason"] == "not_in_current_policy_interface"
    assert cell["declared_policy_consumer_status"] == "intentionally_not_a_current_baseline_consumer"
    assert cell["actual_policy_consumption"] == "not_measured_no_policy_replay"


def test_coverage_aggregates_reasons_as_sessions_and_distinct_tickers() -> None:
    overall = _new_accumulator()
    year = _new_accumulator()
    cell = {
        "source_stage": "observed",
        "publication_stage": "not_yet_public",
        "lookback_stage": "not_yet_public",
        "calculation_stage": "not_yet_public",
        "policy_input_stage": "not_yet_public",
        "reason_codes": ["not_yet_public"],
        "policy_input_reason": "not_yet_public",
    }

    _accumulate_cell(overall, year, "ABCD", cell)
    _accumulate_cell(overall, year, "ABCD", cell)

    result = _finalize_accumulator(overall)
    assert result["denominator_security_sessions"] == 2
    assert result["reason_counts"] == {"not_yet_public": 2}
    assert result["unique_tickers_by_reason"] == {"not_yet_public": 1}
    assert result["policy_input_reason_counts"] == {"not_yet_public": 2}
    assert result["unique_tickers_by_policy_input_reason"] == {"not_yet_public": 1}
