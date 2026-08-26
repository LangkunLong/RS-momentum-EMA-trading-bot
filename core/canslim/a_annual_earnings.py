"""A - Annual Earnings Growth.

Evaluates the year-over-year growth in annual earnings per share.
Per William O'Neil's CANSLIM methodology:
- Annual EPS should show 25%+ growth for each of the last 3-5 years
- Consistency of growth across multiple years matters
- Return on Equity (ROE) should be 17% or higher
- Companies with erratic earnings (one good year, one bad) score lower

Priority: Diluted EPS, then Basic EPS, then fallback to Net Income.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional

import pandas as pd

from config import settings

from .earnings_trace import (
    ATrace,
    MetricFamily,
    _PITProvenanceError,
    TraceReason,
    pit_public_date_for_period,
    pit_public_dates_by_period,
)


def _metric_family(row_label: object) -> MetricFamily:
    """Return the trace metric family for an already selected earnings row."""
    label = str(row_label)
    if pd.Series([label]).str.contains(r"Diluted EPS", case=False, regex=True).iloc[0]:
        return MetricFamily.DILUTED_EPS
    if pd.Series([label]).str.contains(r"Basic EPS", case=False, regex=True).iloc[0]:
        return MetricFamily.BASIC_EPS
    return MetricFamily.NET_INCOME


def _trace_float(value: object) -> float | None:
    """Coerce an observed trace value to a finite, JSON-safe float."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _period_end(label: object):
    """Return the date corresponding to an annual frame column label."""
    timestamp = pd.Timestamp(label)
    if pd.isna(timestamp):
        raise ValueError("annual period end is not a date")
    return timestamp.date()


def _a_unavailable_trace(
    annual_income: pd.DataFrame,
) -> tuple[MetricFamily, TraceReason, object, object]:
    """Describe why the legacy annual row selector found no usable family."""
    labels = annual_income.index.astype(str)
    for pattern, metric_family in (
        (r"Diluted EPS", MetricFamily.DILUTED_EPS),
        (r"Basic EPS", MetricFamily.BASIC_EPS),
        (r"Net Income", MetricFamily.NET_INCOME),
    ):
        matches = labels.str.contains(pattern, case=False, regex=True)
        for row_label in annual_income.index[matches]:
            earnings = annual_income.loc[row_label]
            if isinstance(earnings, pd.DataFrame):
                earnings = earnings.iloc[0]
            ordered = earnings.sort_index().dropna()
            if not ordered.empty:
                return (
                    metric_family,
                    TraceReason.INSUFFICIENT_ANNUAL_HISTORY,
                    ordered.index[-1],
                    ordered.iloc[-1],
                )
    return MetricFamily.UNAVAILABLE, TraceReason.NO_VISIBLE_OBSERVATION, None, None

def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate YoY growth as a decimal.

    Returns None when previous is negative to enforce the CANSLIM
    requirement for established, positive earnings growth.
    """
    if current is None or previous in (None, 0):
        return None

    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return None

    import numpy as np

    if not np.isfinite(current) or not np.isfinite(previous):
        return None

    if np.isclose(previous, 0.0):
        return None

    # Reject negative prior-period earnings — transitioning from a loss
    # to a profit produces misleading growth percentages for CANSLIM.
    if previous < 0:
        return None

    try:
        growth = (current - previous) / abs(previous)
        return growth if np.isfinite(growth) else None
    except ZeroDivisionError:
        return None


def _find_earnings_row(df: pd.DataFrame) -> Optional[str]:
    """Find the best earnings row label using fuzzy matching.

    Priority:
        1. Diluted EPS
        2. Basic EPS
        3. Net Income (fallback)

    Args:
        df: Income statement DataFrame with row labels as index.

    Returns:
        The matching index label, or None if nothing found.
    """
    labels = df.index.astype(str)
    for pattern in (r"Diluted EPS", r"Basic EPS", r"Net Income"):
        matches = labels.str.contains(pattern, case=False, regex=True)
        for row_label in df.index[matches]:
            earnings = df.loc[row_label]
            if isinstance(earnings, pd.DataFrame):
                earnings = earnings.iloc[0]
            ordered = earnings.sort_index()
            if len(ordered) >= 2 and not pd.isna(ordered.iloc[-1]):
                preceding = ordered.iloc[:-1].dropna()
                if not preceding.empty:
                    return row_label

    return None


def _get_annual_growths(earnings: pd.Series) -> List[Optional[float]]:
    """Calculate year-over-year growth for each available year.

    Args:
        earnings: Time-sorted annual earnings series (oldest to newest).

    Returns:
        List of YoY growth rates (most recent first).
    """
    growths = []
    n = len(earnings)
    for i in range(n - 1, 0, -1):
        growth = _safe_growth(earnings.iloc[i], earnings.iloc[i - 1])
        growths.append(growth)
    return growths


def _calculate_roe(annual_income: pd.DataFrame, balance_sheet: pd.DataFrame) -> Optional[float]:
    """Calculate Return on Equity from financial statements.

    ROE = Net Income / Shareholders' Equity

    Args:
        annual_income: Annual income statement DataFrame.
        balance_sheet: Annual balance sheet DataFrame.

    Returns:
        ROE as a decimal (e.g., 0.20 = 20%), or None if unavailable.
    """
    try:
        # Find net income
        ni_mask = annual_income.index.str.contains(r"Net Income", case=False, regex=True)
        if not ni_mask.any():
            return None

        ni_row = annual_income.index[ni_mask][0]
        net_income_series = annual_income.loc[ni_row].dropna().sort_index()
        if net_income_series.empty:
            return None
        net_income = float(net_income_series.iloc[-1])

        # Find shareholders' equity
        equity_patterns = [
            r"Stockholders.? Equity",
            r"Shareholders.? Equity",
            r"Total Equity",
            r"Common Stock Equity",
        ]
        equity_val = None
        for pattern in equity_patterns:
            eq_mask = balance_sheet.index.str.contains(pattern, case=False, regex=True)
            if eq_mask.any():
                eq_row = balance_sheet.index[eq_mask][0]
                eq_series = balance_sheet.loc[eq_row].dropna().sort_index()
                if not eq_series.empty:
                    equity_val = float(eq_series.iloc[-1])
                break

        if equity_val is None or equity_val <= 0:
            return None

        return net_income / equity_val

    except Exception:
        return None


def _evaluate_a_core_unchecked(
    annual_income: pd.DataFrame,
    a_growth_target: Optional[float] = None,
    balance_sheet: Optional[pd.DataFrame] = None,
    *,
    strict_trace_provenance: bool,
    trace_safe_numbers: bool = False,
) -> ATrace:
    """Evaluate A and retain the actual two annual observations used."""
    import numpy as np

    if a_growth_target is None:
        a_growth_target = settings.A_GROWTH_TARGET
    if strict_trace_provenance:
        pit_public_dates_by_period(annual_income)

    annual_growth = None
    roe = None
    metric_family = MetricFamily.UNAVAILABLE
    terminal_reason = TraceReason.NO_VISIBLE_OBSERVATION
    current_period_end = None
    prior_period_end = None
    current_public_date = None
    prior_public_date = None
    current_value = None
    prior_value = None

    if annual_income.empty:
        return ATrace(
            0.0,
            None,
            None,
            metric_family,
            terminal_reason,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    try:
        row_label = _find_earnings_row(annual_income)
        if row_label is None:
            (
                metric_family,
                terminal_reason,
                current_label,
                current_raw_value,
            ) = _a_unavailable_trace(annual_income)
            current_value = _trace_float(current_raw_value)
            if strict_trace_provenance and current_label is not None:
                current_period_end = _period_end(current_label)
                current_public_date = pit_public_date_for_period(
                    annual_income, current_period_end
                )
            return ATrace(
                0.0,
                None,
                None,
                metric_family,
                terminal_reason,
                current_period_end,
                None,
                current_public_date,
                None,
                current_value,
                None,
            )

        metric_family = _metric_family(row_label)
        earnings = annual_income.loc[row_label].sort_index().dropna()
        if len(earnings) < 2:
            return ATrace(
                0.0,
                None,
                None,
                metric_family,
                TraceReason.INSUFFICIENT_ANNUAL_HISTORY,
                None,
                None,
                None,
                None,
                None,
                None,
            )

        if strict_trace_provenance:
            trace_earnings = earnings.iloc[0] if isinstance(earnings, pd.DataFrame) else earnings
            current_period_end = _period_end(trace_earnings.index[-1])
            prior_period_end = _period_end(trace_earnings.index[-2])
            current_value = _trace_float(trace_earnings.iloc[-1])
            prior_value = _trace_float(trace_earnings.iloc[-2])
            current_public_date = pit_public_date_for_period(annual_income, current_period_end)
            prior_public_date = pit_public_date_for_period(annual_income, prior_period_end)

        if current_value is None or not np.isfinite(current_value):
            terminal_reason = TraceReason.NONFINITE_CURRENT_VALUE
        elif prior_value is None or not np.isfinite(prior_value):
            terminal_reason = TraceReason.NONFINITE_PRIOR_VALUE
        elif np.isclose(prior_value, 0.0):
            terminal_reason = TraceReason.ZERO_PRIOR_VALUE
        elif prior_value < 0:
            terminal_reason = TraceReason.NEGATIVE_PRIOR_VALUE
        else:
            terminal_reason = TraceReason.COMPLETE

        yoy_growths = _get_annual_growths(earnings)
        if not yoy_growths or yoy_growths[0] is None:
            return ATrace(
                0.0,
                None,
                None,
                metric_family,
                terminal_reason,
                current_period_end,
                prior_period_end,
                current_public_date,
                prior_public_date,
                current_value,
                prior_value,
            )

        annual_growth = yoy_growths[0]
        growth_score = float(np.clip(annual_growth / a_growth_target, 0, 2) / 2)
        valid_growths = [
            growth for growth in yoy_growths[: settings.A_MIN_YEARS_GROWTH] if growth is not None
        ]
        if valid_growths:
            years_above_target = sum(
                1 for growth in valid_growths if growth >= a_growth_target
            )
            consistency_score = years_above_target / len(valid_growths)
        else:
            consistency_score = 0.0

        roe_score = 0.0
        if balance_sheet is not None and not balance_sheet.empty:
            roe = _calculate_roe(annual_income, balance_sheet)
            if trace_safe_numbers:
                roe = _trace_float(roe)
            if roe is not None:
                roe_target = settings.A_ROE_TARGET
                roe_score = float(np.clip(roe / roe_target, 0, 2) / 2)

        score = (
            settings.A_GROWTH_WEIGHT * growth_score
            + settings.A_CONSISTENCY_WEIGHT * consistency_score
            + settings.A_ROE_WEIGHT * roe_score
        )
        score = float(np.clip(score, 0, 1))
        return ATrace(
            score,
            annual_growth,
            roe,
            metric_family,
            terminal_reason,
            current_period_end,
            prior_period_end,
            current_public_date,
            prior_public_date,
            current_value,
            prior_value,
        )
    except _PITProvenanceError:
        raise
    except Exception:
        return ATrace(
            0.0,
            annual_growth,
            roe,
            metric_family,
            TraceReason.EVALUATOR_EXCEPTION,
            current_period_end,
            prior_period_end,
            current_public_date,
            prior_public_date,
            current_value,
            prior_value,
        )


def _finite_a_trace(trace: ATrace) -> ATrace:
    """Return an A trace whose numeric payload is finite and JSON-safe."""
    score = _trace_float(trace.score)
    return replace(
        trace,
        score=0.0 if score is None else score,
        annual_growth=_trace_float(trace.annual_growth),
        roe=_trace_float(trace.roe),
        current_value=_trace_float(trace.current_value),
        prior_value=_trace_float(trace.prior_value),
    )


def _evaluate_a_core(
    annual_income: pd.DataFrame,
    a_growth_target: Optional[float] = None,
    balance_sheet: Optional[pd.DataFrame] = None,
    *,
    strict_trace_provenance: bool,
) -> ATrace:
    """Evaluate A and normalize every numeric trace field at the boundary."""
    return _finite_a_trace(
        _evaluate_a_core_unchecked(
            annual_income,
            a_growth_target,
            balance_sheet,
            strict_trace_provenance=strict_trace_provenance,
            trace_safe_numbers=True,
        )
    )


def evaluate_a_with_trace(
    annual_income: pd.DataFrame,
    a_growth_target: Optional[float] = None,
    balance_sheet: Optional[pd.DataFrame] = None,
) -> ATrace:
    """Evaluate A with strict PIT provenance validation for trace metadata."""
    return _evaluate_a_core(
        annual_income,
        a_growth_target,
        balance_sheet,
        strict_trace_provenance=True,
    )


def evaluate_a(
    annual_income: pd.DataFrame,
    a_growth_target: Optional[float] = None,
    balance_sheet: Optional[pd.DataFrame] = None,
) -> tuple[float, Optional[float], Optional[float]]:
    """Evaluate A (Annual Earnings Growth) score.

    Per O'Neil's methodology:
    1. Annual EPS should be up 25%+ for each of the last 3-5 years
    2. Consistency across multiple years is critical
    3. ROE should be 17% or higher

    Scoring breakdown:
    - 50% weight: Most recent year's EPS growth vs target
    - 30% weight: Consistency (how many of last 3 years show 25%+ growth)
    - 20% weight: ROE score (17%+ = full score)

    Args:
        annual_income: Annual income statement DataFrame
        a_growth_target: Target growth rate (defaults to settings.A_GROWTH_TARGET)
        balance_sheet: Annual balance sheet DataFrame for ROE calculation

    Returns:
        tuple: (score, annual_growth, roe) where score is 0-1,
               annual_growth is most recent year decimal,
               roe is return on equity decimal
    """
    trace = _evaluate_a_core_unchecked(
        annual_income,
        a_growth_target,
        balance_sheet,
        strict_trace_provenance=False,
    )
    return trace.score, trace.annual_growth, trace.roe
