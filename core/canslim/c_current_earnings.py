"""C - Current Quarterly Earnings Growth.

Evaluates the growth in current quarterly earnings per share.
Per William O'Neil's CANSLIM methodology:
- Current quarter EPS should be up at least 25% vs. the SAME quarter last year (YoY)
- Look for accelerating quarterly earnings growth (each quarter better than the last)
- The more quarters of strong growth, the better

Priority: EPS (Basic or Diluted) first, then fallback to Net Income.
"""

from __future__ import annotations

import math
from typing import List, Optional

import pandas as pd

from config import settings

from .earnings_trace import (
    CTrace,
    MetricFamily,
    _PITProvenanceError,
    TraceReason,
    pit_public_date_for_period,
    pit_public_dates_by_period,
)
from .fiscal_periods import match_fiscal_year_over_year_periods


def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate growth as a decimal, handling edge cases."""
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

    if previous < 0 or np.isclose(previous, 0.0):
        return None

    try:
        growth = (current - previous) / abs(previous)
        return growth if np.isfinite(growth) else None
    except ZeroDivisionError:
        return None


def _find_earnings_row(df: pd.DataFrame) -> Optional[str]:
    """Find the best earnings row label using fuzzy matching.

    Priority:
        1. Basic EPS or Diluted EPS
        2. Net Income (fallback)

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
            comparisons = match_fiscal_year_over_year_periods(earnings)
            if comparisons and comparisons[0].matched:
                latest = comparisons[0]
                if not pd.isna(latest.current_value) and not pd.isna(latest.prior_value):
                    return row_label

    return None


def _get_quarterly_yoy_growths(earnings: pd.Series) -> List[Optional[float]]:
    """Calculate year-over-year growth for each quarter.

    Compares each period to the closest unique prior-year fiscal period.

    Args:
        earnings: Time-sorted earnings series (oldest to newest).

    Returns:
        List of YoY growth rates for available quarters (most recent first).

    """
    return [
        _safe_growth(match.current_value, match.prior_value) if match.matched else None
        for match in match_fiscal_year_over_year_periods(earnings)
    ]


def _check_acceleration(growths: List[Optional[float]]) -> float:
    """Check if earnings growth is accelerating (each quarter better than prior).

    O'Neil emphasizes accelerating earnings — each quarter's YoY growth rate
    should be higher than the previous quarter's YoY growth rate.

    Args:
        growths: List of YoY growth rates, most recent first.

    Returns:
        Acceleration score 0-1. 1.0 means consistent acceleration.

    """
    valid = [g for g in growths if g is not None]
    if len(valid) < 2:
        return 0.5  # Neutral if insufficient data

    accelerating_pairs = 0
    total_pairs = 0
    # growths[0] = most recent, growths[1] = previous quarter
    # Acceleration means growths[0] > growths[1] > growths[2]...
    for i in range(len(valid) - 1):
        total_pairs += 1
        if valid[i] > valid[i + 1]:
            accelerating_pairs += 1

    return accelerating_pairs / total_pairs if total_pairs > 0 else 0.5


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


def _c_unavailable_observation(
    quarterly_income: pd.DataFrame,
) -> tuple[MetricFamily, object, object, object, object]:
    """Return the visible newest observation when C cannot select a pair."""
    labels = quarterly_income.index.astype(str)
    for pattern, metric_family in (
        (r"Diluted EPS", MetricFamily.DILUTED_EPS),
        (r"Basic EPS", MetricFamily.BASIC_EPS),
        (r"Net Income", MetricFamily.NET_INCOME),
    ):
        matches = labels.str.contains(pattern, case=False, regex=True)
        for row_label in quarterly_income.index[matches]:
            earnings = quarterly_income.loc[row_label]
            if isinstance(earnings, pd.DataFrame):
                earnings = earnings.iloc[0]
            comparisons = match_fiscal_year_over_year_periods(earnings)
            if not comparisons:
                continue
            latest = comparisons[0]
            return (
                metric_family,
                latest.current_period,
                latest.current_value,
                latest.prior_period,
                latest.prior_value,
            )
    return (
        MetricFamily.UNAVAILABLE,
        None,
        None,
        None,
        None,
    )


def _evaluate_c_core(
    quarterly_income: pd.DataFrame,
    c_growth_target: Optional[float] = None,
    *,
    strict_trace_provenance: bool,
) -> CTrace:
    """Evaluate C and retain the selected fiscal pair and terminal condition."""
    import numpy as np

    if c_growth_target is None:
        c_growth_target = settings.C_GROWTH_TARGET
    if strict_trace_provenance:
        pit_public_dates_by_period(quarterly_income)

    current_growth = None
    metric_family = MetricFamily.UNAVAILABLE
    terminal_reason = TraceReason.NO_VISIBLE_OBSERVATION
    current_period_end = None
    prior_period_end = None
    current_public_date = None
    prior_public_date = None
    current_value = None
    prior_value = None

    if quarterly_income.empty:
        return CTrace(
            0.0, None, metric_family, terminal_reason, None, None, None, None, None, None
        )

    try:
        row_label = _find_earnings_row(quarterly_income)
        if row_label is None:
            (
                metric_family,
                current_period_end,
                current_raw_value,
                prior_period_end,
                prior_raw_value,
            ) = _c_unavailable_observation(quarterly_income)
            current_value = _trace_float(current_raw_value)
            prior_value = _trace_float(prior_raw_value)
            if current_period_end is None:
                terminal_reason = TraceReason.NO_VISIBLE_OBSERVATION
            elif prior_period_end is None:
                terminal_reason = TraceReason.NO_COMPARABLE_PRIOR_PERIOD
            elif current_value is None:
                terminal_reason = TraceReason.NONFINITE_CURRENT_VALUE
            elif prior_value is None:
                terminal_reason = TraceReason.NONFINITE_PRIOR_VALUE
            elif np.isclose(prior_value, 0.0):
                terminal_reason = TraceReason.ZERO_PRIOR_VALUE
            elif prior_value < 0:
                terminal_reason = TraceReason.NEGATIVE_PRIOR_VALUE
            else:
                terminal_reason = TraceReason.COMPLETE
            if strict_trace_provenance:
                current_public_date = pit_public_date_for_period(
                    quarterly_income, current_period_end
                )
                prior_public_date = pit_public_date_for_period(
                    quarterly_income, prior_period_end
                )
            return CTrace(
                0.0,
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

        metric_family = _metric_family(row_label)
        earnings = quarterly_income.loc[row_label]
        if isinstance(earnings, pd.DataFrame):
            earnings = earnings.iloc[0]
        matches = match_fiscal_year_over_year_periods(earnings)
        latest = matches[0]
        current_period_end = latest.current_period
        prior_period_end = latest.prior_period
        current_value = _trace_float(latest.current_value)
        prior_value = _trace_float(latest.prior_value)
        if strict_trace_provenance:
            current_public_date = pit_public_date_for_period(quarterly_income, current_period_end)
            prior_public_date = pit_public_date_for_period(quarterly_income, prior_period_end)

        if not latest.matched:
            terminal_reason = TraceReason.NO_COMPARABLE_PRIOR_PERIOD
        elif current_value is None or not np.isfinite(current_value):
            terminal_reason = TraceReason.NONFINITE_CURRENT_VALUE
        elif prior_value is None or not np.isfinite(prior_value):
            terminal_reason = TraceReason.NONFINITE_PRIOR_VALUE
        elif np.isclose(prior_value, 0.0):
            terminal_reason = TraceReason.ZERO_PRIOR_VALUE
        elif prior_value < 0:
            terminal_reason = TraceReason.NEGATIVE_PRIOR_VALUE
        else:
            terminal_reason = TraceReason.COMPLETE

        yoy_growths = _get_quarterly_yoy_growths(earnings)
        if not yoy_growths or yoy_growths[0] is None:
            return CTrace(
                0.0,
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

        current_growth = yoy_growths[0]
        growth_score = float(np.clip(current_growth / c_growth_target, 0, 2) / 2)
        valid_growths = [growth for growth in yoy_growths[:3] if growth is not None]
        if valid_growths:
            quarters_above_target = sum(
                1 for growth in valid_growths if growth >= c_growth_target
            )
            consistency_score = quarters_above_target / len(valid_growths)
        else:
            consistency_score = 0.0
        acceleration_score = _check_acceleration(yoy_growths[:4])
        score = (
            settings.C_GROWTH_WEIGHT * growth_score
            + settings.C_CONSISTENCY_WEIGHT * consistency_score
            + settings.C_ACCELERATION_WEIGHT * acceleration_score
        )
        score = float(np.clip(score, 0, 1))
        return CTrace(
            score,
            current_growth,
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
        return CTrace(
            0.0,
            current_growth,
            metric_family,
            TraceReason.EVALUATOR_EXCEPTION,
            current_period_end,
            prior_period_end,
            current_public_date,
            prior_public_date,
            current_value,
            prior_value,
        )


def evaluate_c_with_trace(
    quarterly_income: pd.DataFrame, c_growth_target: Optional[float] = None
) -> CTrace:
    """Evaluate C with strict PIT provenance validation for trace metadata."""
    return _evaluate_c_core(
        quarterly_income, c_growth_target, strict_trace_provenance=True
    )


def evaluate_c(
    quarterly_income: pd.DataFrame, c_growth_target: Optional[float] = None
) -> tuple[float, Optional[float]]:
    """Evaluate C (Current Quarterly Earnings Growth) score.

    Per O'Neil's methodology:
    1. Current quarter EPS must be up 25%+ vs same quarter last year (YoY)
    2. Prefer accelerating growth across recent quarters
    3. Multiple quarters of 25%+ growth is ideal

    Scoring breakdown:
    - 60% weight: Most recent quarter YoY growth vs target
    - 20% weight: Consistency (how many of last 3 quarters show 25%+ growth)
    - 20% weight: Acceleration (are growth rates increasing?)

    Args:
        quarterly_income: Quarterly income statement DataFrame
        c_growth_target: Target growth rate (defaults to settings.C_GROWTH_TARGET)

    Returns:
        tuple: (score, current_growth) where score is 0-1 and current_growth is decimal

    """
    trace = _evaluate_c_core(
        quarterly_income, c_growth_target, strict_trace_provenance=False
    )
    return trace.score, trace.current_growth
