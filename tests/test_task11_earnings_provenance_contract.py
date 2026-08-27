"""Focused contracts for Task 11 C/A traces and PIT provenance transport."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.canslim import a_annual_earnings as annual_evaluator
from core.canslim import c_current_earnings as current_evaluator
from core.canslim.a_annual_earnings import evaluate_a, evaluate_a_with_trace
from core.canslim.c_current_earnings import evaluate_c, evaluate_c_with_trace
from core.canslim.earnings_trace import MetricFamily, TraceReason
from core.pit_data import PITDataBundle
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


def _with_public_dates(
    frame: pd.DataFrame, mapping: dict[str, str]
) -> pd.DataFrame:
    frame.attrs[PIT_PUBLIC_DATES_ATTR] = mapping
    return frame


def test_c_trace_preserves_legacy_result_and_selected_pit_pair() -> None:
    """Break caught: trace mode changes C scoring or reports a different filing pair."""
    quarterly = _with_public_dates(
        pd.DataFrame(
            {
                pd.Timestamp("2023-03-31"): [1.0],
                pd.Timestamp("2024-03-31"): [1.4],
            },
            index=["Diluted EPS"],
        ),
        {
            "2023-03-31": "2023-05-01",
            "2024-03-31": "2024-05-01",
        },
    )

    legacy_score, legacy_growth = evaluate_c(quarterly)
    trace = evaluate_c_with_trace(quarterly)

    assert legacy_score == pytest.approx(0.78)
    assert legacy_growth == pytest.approx(0.40)
    assert trace.score == pytest.approx(0.78)
    assert trace.current_growth == pytest.approx(0.40)
    assert trace.metric_family is MetricFamily.DILUTED_EPS
    assert trace.terminal_reason is TraceReason.COMPLETE
    assert (
        trace.current_period_end,
        trace.prior_period_end,
        trace.current_public_date,
        trace.prior_public_date,
    ) == (
        date(2024, 3, 31),
        date(2023, 3, 31),
        date(2024, 5, 1),
        date(2023, 5, 1),
    )
    assert (trace.current_value, trace.prior_value) == (1.4, 1.0)


def test_a_trace_preserves_legacy_result_and_selected_pit_pair() -> None:
    """Break caught: trace mode changes A scoring or loses the actual annual pair."""
    annual = _with_public_dates(
        pd.DataFrame(
            {
                pd.Timestamp("2022-12-31"): [1.0, 100.0],
                pd.Timestamp("2023-12-31"): [1.2, 120.0],
                pd.Timestamp("2024-12-31"): [1.5, 150.0],
            },
            index=["Diluted EPS", "Net Income"],
        ),
        {
            "2022-12-31": "2023-02-15",
            "2023-12-31": "2024-02-15",
            "2024-12-31": "2025-02-15",
        },
    )
    balance = pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [500.0]},
        index=["Stockholders Equity"],
    )

    legacy_score, legacy_growth, legacy_roe = evaluate_a(
        annual, balance_sheet=balance
    )
    trace = evaluate_a_with_trace(annual, balance_sheet=balance)

    assert legacy_score == pytest.approx(0.5764705882352941)
    assert legacy_growth == pytest.approx(0.25)
    assert legacy_roe == pytest.approx(0.30)
    assert trace.score == pytest.approx(0.5764705882352941)
    assert trace.annual_growth == pytest.approx(0.25)
    assert trace.roe == pytest.approx(0.30)
    assert trace.metric_family is MetricFamily.DILUTED_EPS
    assert trace.terminal_reason is TraceReason.COMPLETE
    assert (
        trace.current_period_end,
        trace.prior_period_end,
        trace.current_public_date,
        trace.prior_public_date,
    ) == (
        date(2024, 12, 31),
        date(2023, 12, 31),
        date(2025, 2, 15),
        date(2024, 2, 15),
    )
    assert (trace.current_value, trace.prior_value) == (1.5, 1.2)


@pytest.mark.parametrize(
    ("current", "prior", "reason", "expected_current", "expected_prior"),
    [
        (float("inf"), 0.0, TraceReason.NONFINITE_CURRENT_VALUE, None, 0.0),
        (2.0, float("inf"), TraceReason.NONFINITE_PRIOR_VALUE, 2.0, None),
        (2.0, 0.0, TraceReason.ZERO_PRIOR_VALUE, 2.0, 0.0),
        (2.0, -1.0, TraceReason.NEGATIVE_PRIOR_VALUE, 2.0, -1.0),
    ],
)
def test_c_trace_reason_precedence_keeps_the_observed_pair(
    current: float,
    prior: float,
    reason: TraceReason,
    expected_current: float | None,
    expected_prior: float | None,
) -> None:
    """Break caught: a later guard masks the first invalid C observation."""
    quarterly = _with_public_dates(
        pd.DataFrame(
            {
                pd.Timestamp("2023-03-31"): [prior],
                pd.Timestamp("2024-03-31"): [current],
            },
            index=["Basic EPS"],
        ),
        {
            "2023-03-31": "2023-05-02",
            "2024-03-31": "2024-05-02",
        },
    )

    trace = evaluate_c_with_trace(quarterly)

    assert trace.score == 0.0
    assert trace.current_growth is None
    assert trace.terminal_reason is reason
    assert trace.current_value == expected_current
    assert trace.prior_value == expected_prior
    assert trace.current_period_end == date(2024, 3, 31)
    assert trace.prior_period_end == date(2023, 3, 31)
    assert trace.current_public_date == date(2024, 5, 2)
    assert trace.prior_public_date == date(2023, 5, 2)


@pytest.mark.parametrize(
    ("current", "prior", "reason", "expected_current", "expected_prior"),
    [
        (float("inf"), 0.0, TraceReason.NONFINITE_CURRENT_VALUE, None, 0.0),
        (2.0, float("inf"), TraceReason.NONFINITE_PRIOR_VALUE, 2.0, None),
        (2.0, 0.0, TraceReason.ZERO_PRIOR_VALUE, 2.0, 0.0),
        (2.0, -1.0, TraceReason.NEGATIVE_PRIOR_VALUE, 2.0, -1.0),
    ],
)
def test_a_trace_reason_precedence_keeps_the_observed_pair(
    current: float,
    prior: float,
    reason: TraceReason,
    expected_current: float | None,
    expected_prior: float | None,
) -> None:
    """Break caught: a later guard masks the first invalid A observation."""
    annual = _with_public_dates(
        pd.DataFrame(
            {
                pd.Timestamp("2023-12-31"): [prior],
                pd.Timestamp("2024-12-31"): [current],
            },
            index=["Net Income"],
        ),
        {
            "2023-12-31": "2024-02-16",
            "2024-12-31": "2025-02-16",
        },
    )

    trace = evaluate_a_with_trace(annual)

    assert trace.score == 0.0
    assert trace.annual_growth is None
    assert trace.roe is None
    assert trace.terminal_reason is reason
    assert trace.current_value == expected_current
    assert trace.prior_value == expected_prior
    assert trace.current_period_end == date(2024, 12, 31)
    assert trace.prior_period_end == date(2023, 12, 31)
    assert trace.current_public_date == date(2025, 2, 16)
    assert trace.prior_public_date == date(2024, 2, 16)


def test_pit_statement_provenance_is_opt_in_and_tracks_selected_revision() -> None:
    """Break caught: public dates leak by default or point at a superseded filing."""
    records = [
        {
            "statement_type": "quarterly",
            "period_end": "2023-03-31",
            "public_date": "2023-05-01",
            "diluted_eps": 1.0,
        },
        {
            "statement_type": "quarterly",
            "period_end": "2024-03-31",
            "public_date": "2024-05-01",
            "diluted_eps": 1.3,
        },
        {
            "statement_type": "quarterly",
            "period_end": "2024-03-31",
            "public_date": "2024-05-03",
            "diluted_eps": 1.4,
        },
    ]

    ordinary = PITDataBundle._statement_frame(records, "quarterly")
    traced = PITDataBundle._statement_frame(
        records, "quarterly", include_provenance=True
    )

    assert PIT_PUBLIC_DATES_ATTR not in ordinary.attrs
    assert traced.attrs[PIT_PUBLIC_DATES_ATTR] == {
        "2023-03-31": "2023-05-01",
        "2024-03-31": "2024-05-03",
    }
    assert traced.loc["Diluted EPS", pd.Timestamp("2024-03-31")] == 1.4
    trace = evaluate_c_with_trace(traced)
    assert trace.current_public_date == date(2024, 5, 3)


def test_trace_serialization_vocabulary_is_closed_and_literal() -> None:
    """Break caught: persisted trace or frame vocabulary changes incompatibly."""
    assert PIT_PUBLIC_DATES_ATTR == "pit_public_date_by_period"
    assert {family.value for family in MetricFamily} == {
        "diluted_eps",
        "basic_eps",
        "net_income",
        "unavailable",
    }
    assert {reason.value for reason in TraceReason} == {
        "complete",
        "no_visible_observation",
        "no_comparable_prior_period",
        "insufficient_annual_history",
        "nonfinite_current_value",
        "nonfinite_prior_value",
        "zero_prior_value",
        "negative_prior_value",
        "evaluator_exception",
    }


def test_trace_mode_rejects_incomplete_supplied_provenance_without_affecting_legacy() -> None:
    """Break caught: an authenticated trace silently invents an absent public date."""
    quarterly = _with_public_dates(
        pd.DataFrame(
            {
                pd.Timestamp("2023-03-31"): [1.0],
                pd.Timestamp("2024-03-31"): [1.4],
            },
            index=["Diluted EPS"],
        ),
        {"2024-03-31": "2024-05-01"},
    )

    assert evaluate_c(quarterly)[1] == pytest.approx(0.4)
    with pytest.raises(ValueError, match="no public date for 2023-03-31"):
        evaluate_c_with_trace(quarterly)


def test_empty_earnings_frames_report_no_visible_observation() -> None:
    """Break caught: absence of visible observations is mislabeled as bad growth."""
    c_trace = evaluate_c_with_trace(pd.DataFrame())
    a_trace = evaluate_a_with_trace(pd.DataFrame())

    assert (c_trace.metric_family.value, c_trace.terminal_reason.value) == (
        "unavailable",
        "no_visible_observation",
    )
    assert (a_trace.metric_family.value, a_trace.terminal_reason.value) == (
        "unavailable",
        "no_visible_observation",
    )
    assert c_trace.current_growth is None
    assert a_trace.annual_growth is None


def test_single_visible_period_reports_the_gate_specific_history_reason() -> None:
    """Break caught: C and A collapse distinct missing-history conditions."""
    quarterly = _with_public_dates(
        pd.DataFrame(
            {pd.Timestamp("2024-03-31"): [1.4]},
            index=["Basic EPS"],
        ),
        {"2024-03-31": "2024-05-01"},
    )
    annual = _with_public_dates(
        pd.DataFrame(
            {pd.Timestamp("2023-12-31"): [1.5]},
            index=["Net Income"],
        ),
        {"2023-12-31": "2024-02-15"},
    )

    c_trace = evaluate_c_with_trace(quarterly)
    a_trace = evaluate_a_with_trace(annual)

    assert (c_trace.metric_family.value, c_trace.terminal_reason.value) == (
        "basic_eps",
        "no_comparable_prior_period",
    )
    assert (
        c_trace.current_period_end,
        c_trace.current_public_date,
        c_trace.prior_period_end,
        c_trace.prior_public_date,
    ) == (date(2024, 3, 31), date(2024, 5, 1), None, None)
    assert (a_trace.metric_family.value, a_trace.terminal_reason.value) == (
        "net_income",
        "insufficient_annual_history",
    )
    assert (
        a_trace.current_period_end,
        a_trace.current_public_date,
        a_trace.prior_period_end,
        a_trace.prior_public_date,
    ) == (date(2023, 12, 31), date(2024, 2, 15), None, None)


def test_public_trace_evaluators_report_internal_evaluator_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: unexpected evaluator failures masquerade as unavailable data."""
    quarterly = pd.DataFrame(
        {pd.Timestamp("2024-03-31"): [1.0]}, index=["Diluted EPS"]
    )
    annual = pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [1.0]}, index=["Diluted EPS"]
    )

    def explode(_frame: pd.DataFrame) -> None:
        raise RuntimeError("synthetic evaluator failure")

    monkeypatch.setattr(current_evaluator, "_find_earnings_row", explode)
    monkeypatch.setattr(annual_evaluator, "_find_earnings_row", explode)

    assert evaluate_c_with_trace(quarterly).terminal_reason.value == (
        "evaluator_exception"
    )
    assert evaluate_a_with_trace(annual).terminal_reason.value == (
        "evaluator_exception"
    )


@pytest.mark.parametrize(
    ("diluted", "basic", "expected_family"),
    [
        ((1.0, 1.4), (1.0, 1.3), "diluted_eps"),
        ((float("nan"), 1.4), (1.0, 1.3), "basic_eps"),
        ((float("nan"), 1.4), (float("nan"), 1.3), "net_income"),
    ],
)
def test_metric_family_fallback_order_is_diluted_then_basic_then_net_income(
    diluted: tuple[float, float],
    basic: tuple[float, float],
    expected_family: str,
) -> None:
    """Break caught: a lower-priority family masks an available preferred pair."""
    quarterly = _with_public_dates(
        pd.DataFrame(
            [diluted, basic, (100.0, 140.0)],
            index=["Diluted EPS", "Basic EPS", "Net Income"],
            columns=[pd.Timestamp("2023-03-31"), pd.Timestamp("2024-03-31")],
        ),
        {"2023-03-31": "2023-05-01", "2024-03-31": "2024-05-01"},
    )
    annual = _with_public_dates(
        pd.DataFrame(
            [diluted, basic, (100.0, 140.0)],
            index=["Diluted EPS", "Basic EPS", "Net Income"],
            columns=[pd.Timestamp("2022-12-31"), pd.Timestamp("2023-12-31")],
        ),
        {"2022-12-31": "2023-02-15", "2023-12-31": "2024-02-15"},
    )

    assert evaluate_c_with_trace(quarterly).metric_family.value == expected_family
    assert evaluate_a_with_trace(annual).metric_family.value == expected_family
