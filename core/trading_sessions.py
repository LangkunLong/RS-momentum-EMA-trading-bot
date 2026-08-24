"""Canonical helpers for completed US-equity daily sessions."""

from __future__ import annotations

from datetime import date, datetime
from typing import TypeAlias

import pandas as pd


SessionLike: TypeAlias = str | date | datetime | pd.Timestamp


def normalize_us_equity_session(value: SessionLike) -> pd.Timestamp:
    """Return a timezone-naive midnight label for a daily US-equity session."""
    session = pd.Timestamp(value)
    if pd.isna(session):
        raise ValueError("US-equity session is missing")
    if session.tzinfo is not None:
        session = session.tz_localize(None)
    return session.normalize()


def latest_us_equity_session(history: pd.DataFrame) -> date | None:
    """Return the session represented by the final row, if one exists."""
    if history.empty:
        return None
    return canonicalize_us_equity_history(history).index[-1].date()


def exact_session_row(
    history: pd.DataFrame,
    session: SessionLike,
) -> pd.Series | None:
    """Return the final row labeled with ``session``; never use an adjacent row."""
    if history.empty:
        return None
    target = normalize_us_equity_session(session)
    canonical = canonicalize_us_equity_history(history)
    if target not in canonical.index:
        return None
    return canonical.loc[target]


def history_through_exact_session(
    history: pd.DataFrame,
    session: SessionLike,
) -> pd.DataFrame | None:
    """Return history through ``session`` only when that exact session exists."""
    if history.empty:
        return None
    target = normalize_us_equity_session(session)
    canonical = canonicalize_us_equity_history(history)
    if target not in canonical.index:
        return None
    return canonical.loc[canonical.index <= target].copy()


def canonicalize_us_equity_history(history: pd.DataFrame) -> pd.DataFrame:
    """Return one ordered, timezone-naive row per US-equity session."""
    if history.empty:
        return history

    timestamps = _timestamp_index(history.index)
    normalized = _normalized_session_index(timestamps)
    if (
        isinstance(history.index, pd.DatetimeIndex)
        and history.index.tz is None
        and history.index.is_monotonic_increasing
        and not history.index.has_duplicates
        and history.index.equals(normalized)
    ):
        return history

    order = timestamps.argsort(kind="stable")
    canonical = history.iloc[order].copy(deep=False)
    canonical.index = _normalized_session_index(timestamps.take(order))
    if canonical.index.has_duplicates:
        canonical = canonical.loc[~canonical.index.duplicated(keep="last")]
    return canonical


def _normalized_session_index(index: pd.Index) -> pd.DatetimeIndex:
    sessions = _timestamp_index(index)
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    return sessions.normalize()


def _timestamp_index(index: pd.Index) -> pd.DatetimeIndex:
    try:
        sessions = pd.DatetimeIndex(pd.to_datetime(index, errors="raise"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "US-equity history index must contain parseable timestamps in one timezone"
        ) from exc
    if sessions.hasnans:
        raise ValueError("US-equity session is missing")
    return sessions
