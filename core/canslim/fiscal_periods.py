"""Shared fiscal-period matching for quarterly CANSLIM inputs."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd


FISCAL_YOY_TOLERANCE_DAYS = 28


@dataclass(frozen=True)
class FiscalYearOverYearMatch:
    """One fiscal period and its uniquely matched prior-year comparator."""

    current_period: date
    current_value: Any
    prior_period: date | None = None
    prior_value: Any = None

    @property
    def matched(self) -> bool:
        return self.prior_period is not None


@dataclass(frozen=True)
class _ValidatedPeriod:
    period: date
    value: Any
    valid: bool


def _local_period_date(label: object) -> date | None:
    """Return the label's wall-clock date without converting its timezone."""
    try:
        timestamp = pd.Timestamp(label)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _is_missing(value: object) -> bool:
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _same_scalar(left: object, right: object) -> bool:
    left_missing = _is_missing(left)
    right_missing = _is_missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing
    try:
        equal = left == right
    except (TypeError, ValueError):
        return False
    try:
        return bool(equal)
    except (TypeError, ValueError):
        return False


def _prior_calendar_year_target(period: date) -> date | None:
    if period.year <= 1:
        return None
    prior_year = period.year - 1
    day = min(period.day, calendar.monthrange(prior_year, period.month)[1])
    return date(prior_year, period.month, day)


def match_fiscal_year_over_year_periods(
    values: pd.Series,
    *,
    tolerance_days: int = FISCAL_YOY_TOLERANCE_DAYS,
) -> tuple[FiscalYearOverYearMatch, ...]:
    """Match every period to one closest comparator around the prior-year date.

    Exact duplicate period ends are collapsed only when their values agree. A
    missing comparator, conflicting duplicate, or equal-distance tie produces an
    unmatched observation. Results are newest-first and retain the latest period
    even when it is unmatched, preventing fallback to an older current quarter.
    """
    if tolerance_days < 0:
        raise ValueError("tolerance_days must be nonnegative")

    grouped: dict[date, list[object]] = {}
    for label, value in values.items():
        period = _local_period_date(label)
        if period is not None:
            grouped.setdefault(period, []).append(value)

    validated: dict[date, _ValidatedPeriod] = {}
    for period, duplicates in grouped.items():
        first = duplicates[0]
        valid = all(_same_scalar(first, candidate) for candidate in duplicates[1:])
        validated[period] = _ValidatedPeriod(period, first if valid else None, valid)

    observations: list[FiscalYearOverYearMatch] = []
    periods = sorted(validated, reverse=True)
    for current_period in periods:
        current = validated[current_period]
        target = _prior_calendar_year_target(current_period)
        if not current.valid or target is None:
            observations.append(FiscalYearOverYearMatch(current_period, current.value))
            continue

        candidates = [
            candidate
            for candidate in periods
            if candidate < current_period
            and abs((candidate - target).days) <= tolerance_days
        ]
        if not candidates:
            observations.append(FiscalYearOverYearMatch(current_period, current.value))
            continue

        closest_distance = min(abs((candidate - target).days) for candidate in candidates)
        closest = [
            candidate
            for candidate in candidates
            if abs((candidate - target).days) == closest_distance
        ]
        if len(closest) != 1:
            observations.append(FiscalYearOverYearMatch(current_period, current.value))
            continue

        prior = validated[closest[0]]
        if not prior.valid:
            observations.append(FiscalYearOverYearMatch(current_period, current.value))
            continue
        observations.append(
            FiscalYearOverYearMatch(
                current_period=current_period,
                current_value=current.value,
                prior_period=prior.period,
                prior_value=prior.value,
            )
        )

    return tuple(observations)
