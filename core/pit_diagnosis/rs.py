"""Pure, offline point-in-time relative-strength calculations."""

from __future__ import annotations

import math
from typing import Iterable

import pandas as pd

from core.trading_sessions import exact_session_row, history_through_exact_session

_DAYS_PER_QUARTER = 65
_QUARTER_WEIGHTS = (0.40, 0.20, 0.20, 0.20)
_RS_PERCENTILE_MULTIPLIER = 98
_RS_PERCENTILE_MIN = 1


def calculate_pit_rs_snapshot(
    all_closes: pd.DataFrame,
    eval_date: pd.Timestamp,
    eligible_tickers: Iterable[str] | None = None,
) -> dict[str, float]:
    """Calculate causal RS ratings without importing provider-facing modules."""
    sliced = history_through_exact_session(all_closes, eval_date)
    event_row = exact_session_row(all_closes, eval_date)
    if sliced is None or event_row is None:
        return {}
    fresh_columns = [column for column in sliced.columns if _finite_number(event_row[column]) is not None]
    sliced = sliced.loc[:, fresh_columns].dropna(axis=1, how="all")
    if eligible_tickers is not None:
        eligible = {str(ticker).upper() for ticker in eligible_tickers}
        sliced = sliced.loc[:, [column for column in sliced.columns if str(column).upper() in eligible]]
    if sliced.empty:
        return {}

    performances: dict[str, float] = {}
    for ticker in sliced.columns:
        series = sliced[ticker].dropna()
        if len(series) < 60:
            continue
        performance = _weighted_performance(series)
        if performance is None:
            raw_return = (series.iloc[-1] - series.iloc[0]) / series.iloc[0]
            performance = (1 + raw_return) ** (252 / len(series)) - 1
        if performance is not None:
            performances[str(ticker)] = float(performance)
    if len(performances) < 10:
        return {}
    ranks = pd.Series(performances).rank(pct=True)
    return {str(symbol): float(score * _RS_PERCENTILE_MULTIPLIER + _RS_PERCENTILE_MIN) for symbol, score in ranks.items()}


def _weighted_performance(series: pd.Series) -> float | None:
    if len(series) < 4 * _DAYS_PER_QUARTER:
        return None
    try:
        q1 = (series.iloc[-1] / series.iloc[-_DAYS_PER_QUARTER]) - 1
        q2 = (series.iloc[-_DAYS_PER_QUARTER] / series.iloc[-2 * _DAYS_PER_QUARTER]) - 1
        q3 = (series.iloc[-2 * _DAYS_PER_QUARTER] / series.iloc[-3 * _DAYS_PER_QUARTER]) - 1
        q4 = (series.iloc[-3 * _DAYS_PER_QUARTER] / series.iloc[-4 * _DAYS_PER_QUARTER]) - 1
        return sum(weight * value for weight, value in zip(_QUARTER_WEIGHTS, (q1, q2, q3, q4), strict=True))
    except (IndexError, TypeError, ZeroDivisionError):
        return None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
