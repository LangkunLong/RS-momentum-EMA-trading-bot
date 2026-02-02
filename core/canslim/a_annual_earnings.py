"""
A - Annual Earnings Growth

Evaluates the year-over-year growth in annual earnings per share.
Per William O'Neil's CANSLIM methodology:
- Annual EPS should show 25%+ growth for each of the last 3-5 years
- Consistency of growth across multiple years matters
- Return on Equity (ROE) should be 17% or higher
- Companies with erratic earnings (one good year, one bad) score lower

Priority: EPS (Basic or Diluted) first, then fallback to Net Income.
"""
from __future__ import annotations
from typing import Optional, List
import pandas as pd
from config import settings


def _safe_growth(current: float, previous: float) -> Optional[float]:
    """Calculate YoY growth as a decimal."""
    if current is None or previous in (None, 0):
        return None

    try:
        current = float(current)
        previous = float(previous)
    except (TypeError, ValueError):
        return None

    import numpy as np
    if np.isclose(previous, 0.0):
        return None

    try:
        return (current - previous) / abs(previous)
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
    # Priority 1: EPS rows
    eps_mask = df.index.str.contains(r'Basic EPS|Diluted EPS', case=False, regex=True)
    if eps_mask.any():
        return df.index[eps_mask][0]

    # Priority 2: Net Income
    ni_mask = df.index.str.contains(r'Net Income', case=False, regex=True)
    if ni_mask.any():
        return df.index[ni_mask][0]

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
        ni_mask = annual_income.index.str.contains(r'Net Income', case=False, regex=True)
        if not ni_mask.any():
            return None

        ni_row = annual_income.index[ni_mask][0]
        net_income_series = annual_income.loc[ni_row].sort_index()
        net_income = float(net_income_series.iloc[-1])

        # Find shareholders' equity
        equity_patterns = [
            r'Stockholders.? Equity',
            r'Shareholders.? Equity',
            r'Total Equity',
            r'Common Stock Equity',
        ]
        equity_val = None
        for pattern in equity_patterns:
            eq_mask = balance_sheet.index.str.contains(pattern, case=False, regex=True)
            if eq_mask.any():
                eq_row = balance_sheet.index[eq_mask][0]
                eq_series = balance_sheet.loc[eq_row].sort_index()
                equity_val = float(eq_series.iloc[-1])
                break

        if equity_val is None or equity_val <= 0:
            return None

        return net_income / equity_val

    except Exception:
        return None


def evaluate_a(
    annual_income: pd.DataFrame,
    a_growth_target: Optional[float] = None,
    balance_sheet: Optional[pd.DataFrame] = None
) -> tuple[float, Optional[float], Optional[float]]:
    """
    Evaluate A (Annual Earnings Growth) score.

    Per O'Neil's methodology:
    1. Annual EPS should be up 25%+ for each of the last 3-5 years
    2. Consistency across multiple years is critical
    3. ROE should be 17% or higher

    For IPO stocks with limited annual data (< 3 years):
    - Score based on available years with a data-quality discount
    - Shift weight from consistency (can't measure) to growth + ROE
    - O'Neil still wants to see strong earnings even for new companies

    Scoring breakdown (full data):
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
    import numpy as np

    a_growth_target = a_growth_target or settings.A_GROWTH_TARGET
    annual_growth = None
    roe = None

    if annual_income.empty:
        return 0.0, None, None

    try:
        row_label = _find_earnings_row(annual_income)
        if row_label is None:
            return 0.0, None, None

        # Sort columns by date oldest→newest so iloc[-1] is most recent
        earnings = annual_income.loc[row_label].sort_index()

        if len(earnings) < 2:
            return 0.0, None, None

        # Get year-over-year growth rates (most recent first)
        yoy_growths = _get_annual_growths(earnings)

        if not yoy_growths or yoy_growths[0] is None:
            return 0.0, None, None

        annual_growth = yoy_growths[0]  # Most recent year

        # Component 1: Most recent year growth vs target
        growth_score = float(np.clip(annual_growth / a_growth_target, 0, 2) / 2)

        # Component 3: ROE check — O'Neil requires 17%+ ROE
        roe_score = 0.0
        if balance_sheet is not None and not balance_sheet.empty:
            roe = _calculate_roe(annual_income, balance_sheet)
            if roe is not None:
                roe_target = settings.A_ROE_TARGET
                roe_score = float(np.clip(roe / roe_target, 0, 2) / 2)

        # Determine if this is an IPO / limited data scenario
        years_available = len(yoy_growths)
        is_ipo = years_available < settings.A_MIN_YEARS_GROWTH

        if is_ipo:
            # --- IPO / Limited Data Path ---
            # Can't properly assess multi-year consistency with < 3 years.
            # Shift consistency weight to growth + ROE, apply discount.
            valid_growths = [g for g in yoy_growths if g is not None]
            if valid_growths:
                years_above_target = sum(1 for g in valid_growths if g >= a_growth_target)
                consistency_score = years_above_target / len(valid_growths)
            else:
                consistency_score = 0.0

            # For IPOs: 60% growth, 15% consistency (limited), 25% ROE
            score = (
                0.60 * growth_score
                + 0.15 * consistency_score
                + 0.25 * roe_score
            )

            # Apply IPO data-quality discount
            score *= settings.A_IPO_DATA_DISCOUNT

        else:
            # --- Standard Path: 3+ years of data ---
            # Component 2: Consistency — how many of last 3 years show 25%+ growth
            valid_growths = [g for g in yoy_growths[:settings.A_MIN_YEARS_GROWTH] if g is not None]
            if valid_growths:
                years_above_target = sum(1 for g in valid_growths if g >= a_growth_target)
                consistency_score = years_above_target / len(valid_growths)
            else:
                consistency_score = 0.0

            # Weighted combination
            score = (
                settings.A_GROWTH_WEIGHT * growth_score
                + settings.A_CONSISTENCY_WEIGHT * consistency_score
                + settings.A_ROE_WEIGHT * roe_score
            )

        score = float(np.clip(score, 0, 1))
        return score, annual_growth, roe

    except Exception:
        return 0.0, annual_growth, roe
