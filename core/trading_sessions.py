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
    return _canonical_history(history).index[-1].date()


def exact_session_row(
    history: pd.DataFrame,
    session: SessionLike,
) -> pd.Series | None:
    """Return the final row labeled with ``session``; never use an adjacent row."""
    if history.empty:
        return None
    target = normalize_us_equity_session(session)
    canonical = _canonical_history(history)
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
    canonical = _canonical_history(history)
    if target not in canonical.index:
        return None
    return canonical.loc[canonical.index <= target].copy()


def _canonical_history(history: pd.DataFrame) -> pd.DataFrame:
    """Normalize, order, and collapse a daily history without mutating its caller."""
    canonical = history.copy(deep=False)
    canonical.index = _normalized_session_index(history.index)
    if not canonical.index.is_monotonic_increasing:
        canonical = canonical.sort_index(kind="stable")
    if canonical.index.has_duplicates:
        canonical = canonical.loc[~canonical.index.duplicated(keep="last")]
    return canonical


def _normalized_session_index(index: pd.Index) -> pd.DatetimeIndex:
    try:
        sessions = pd.DatetimeIndex(pd.to_datetime(index, errors="raise"))
        if sessions.tz is not None:
            sessions = sessions.tz_localize(None)
        return sessions.normalize()
    except (TypeError, ValueError):
        return pd.DatetimeIndex([normalize_us_equity_session(value) for value in index])
