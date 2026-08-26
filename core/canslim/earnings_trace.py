"""Closed trace vocabulary shared by CANSLIM earnings evaluators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import pandas as pd

from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


class _PITProvenanceError(ValueError):
    """A supplied PIT provenance mapping cannot support a selected trace."""


class MetricFamily(StrEnum):
    DILUTED_EPS = "diluted_eps"
    BASIC_EPS = "basic_eps"
    NET_INCOME = "net_income"
    UNAVAILABLE = "unavailable"


class TraceReason(StrEnum):
    COMPLETE = "complete"
    NO_VISIBLE_OBSERVATION = "no_visible_observation"
    NO_COMPARABLE_PRIOR_PERIOD = "no_comparable_prior_period"
    INSUFFICIENT_ANNUAL_HISTORY = "insufficient_annual_history"
    NONFINITE_CURRENT_VALUE = "nonfinite_current_value"
    NONFINITE_PRIOR_VALUE = "nonfinite_prior_value"
    ZERO_PRIOR_VALUE = "zero_prior_value"
    NEGATIVE_PRIOR_VALUE = "negative_prior_value"
    EVALUATOR_EXCEPTION = "evaluator_exception"


@dataclass(frozen=True, slots=True)
class CTrace:
    score: float
    current_growth: float | None
    metric_family: MetricFamily
    terminal_reason: TraceReason
    current_period_end: date | None
    prior_period_end: date | None
    current_public_date: date | None
    prior_public_date: date | None
    current_value: float | None
    prior_value: float | None


@dataclass(frozen=True, slots=True)
class ATrace:
    score: float
    annual_growth: float | None
    roe: float | None
    metric_family: MetricFamily
    terminal_reason: TraceReason
    current_period_end: date | None
    prior_period_end: date | None
    current_public_date: date | None
    prior_public_date: date | None
    current_value: float | None
    prior_value: float | None


def pit_public_dates_by_period(frame: pd.DataFrame) -> dict[date, date] | None:
    """Return validated PIT publication dates without changing ``frame``.

    Missing provenance is represented by ``None`` so ordinary historical inputs
    remain usable. If provenance is supplied, it must cover ISO period-end keys
    with ISO public-date values; incomplete or malformed supplied provenance is
    rejected for callers that need trace dates.
    """
    if PIT_PUBLIC_DATES_ATTR not in frame.attrs:
        return None

    raw_mapping = frame.attrs[PIT_PUBLIC_DATES_ATTR]
    if not isinstance(raw_mapping, Mapping):
        raise _PITProvenanceError(f"{PIT_PUBLIC_DATES_ATTR} must be an ISO-date mapping")

    parsed: dict[date, date] = {}
    for raw_period, raw_public_date in raw_mapping.items():
        if not isinstance(raw_period, str) or not isinstance(raw_public_date, str):
            raise _PITProvenanceError(f"{PIT_PUBLIC_DATES_ATTR} must be an ISO-date mapping")
        try:
            period = date.fromisoformat(raw_period)
            public_date = date.fromisoformat(raw_public_date)
        except ValueError as error:
            raise _PITProvenanceError(
                f"{PIT_PUBLIC_DATES_ATTR} must be an ISO-date mapping"
            ) from error
        parsed[period] = public_date
    return parsed


def pit_public_date_for_period(
    frame: pd.DataFrame, period: date | None
) -> date | None:
    """Return a selected period's public date, rejecting unknown provenance."""
    if period is None:
        return None
    public_dates = pit_public_dates_by_period(frame)
    if public_dates is None:
        return None
    try:
        return public_dates[period]
    except KeyError as error:
        raise _PITProvenanceError(
            f"{PIT_PUBLIC_DATES_ATTR} has no public date for {period.isoformat()}"
        ) from error
