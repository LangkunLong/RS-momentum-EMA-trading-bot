"""Strict, auditable, pre-event CANSLIM base-pattern evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from .models import RuleOutcome


class BaseKind(str, Enum):
    FLAT_BASE = "flat_base"
    CUP_WITH_HANDLE = "cup_with_handle"


@dataclass(frozen=True)
class BasePolicy:
    flat_min_sessions: int
    flat_max_sessions: int
    flat_max_depth_pct: float
    cup_min_sessions: int
    cup_max_sessions: int
    cup_min_depth_pct: float
    cup_max_depth_pct: float
    handle_max_depth_pct: float
    right_lip_max_distance_pct: float

    @classmethod
    def canonical_v1(cls) -> "BasePolicy":
        """Return the reviewed, immutable first proper-base policy."""
        return cls(
            flat_min_sessions=25,
            flat_max_sessions=65,
            flat_max_depth_pct=0.15,
            cup_min_sessions=35,
            cup_max_sessions=130,
            cup_min_depth_pct=0.15,
            cup_max_depth_pct=0.33,
            handle_max_depth_pct=0.12,
            right_lip_max_distance_pct=0.05,
        )


@dataclass(frozen=True)
class BasePattern:
    kind: BaseKind
    start_session: str
    end_session: str
    pivot: float
    low: float
    depth_pct: float
    duration_sessions: int
    handle_start_session: str | None
    handle_end_session: str | None
    input_sha256: str

    @property
    def duration(self) -> int:
        """Compatibility-friendly name for the base duration in sessions."""
        return self.duration_sessions


_REQUIRED_OHLC_COLUMNS = ("High", "Low", "Close")
_ENTRY_EVIDENCE_IDS = (
    "E.PROPER_BASE",
    "E.PIVOT",
    "E.BUY_ZONE",
    "S.VOLUME_CONFIRMATION",
    "N.NEW_HIGH",
)


class _RangeExtrema:
    """Sparse range extrema used by the array-backed production detector.

    The detector evaluates many overlapping windows.  Calling NumPy reductions
    for every one of those windows repeatedly allocates slices and rescans the
    same values.  These sparse tables answer each contiguous min/max query in
    constant time while retaining the first-position tie behavior of
    ``numpy.argmin`` and ``numpy.argmax``.

    This is an internal optimization for already validated finite arrays.  The
    public detector still validates the original DataFrame before constructing
    this cache, and the private DataFrame implementation remains the parity
    oracle.
    """

    __slots__ = (
        "_high_max_values",
        "_high_max_positions",
        "_low_min_values",
        "_low_min_positions",
        "_close_min_values",
        "_close_min_positions",
    )

    def __init__(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> None:
        self._high_max_values, self._high_max_positions = _build_range_table(highs, find_min=False)
        self._low_min_values, self._low_min_positions = _build_range_table(lows, find_min=True)
        self._close_min_values, self._close_min_positions = _build_range_table(closes, find_min=True)

    def high_max(self, start_pos: int, end_pos: int) -> tuple[float, int]:
        return _query_range(
            self._high_max_values,
            self._high_max_positions,
            start_pos,
            end_pos,
            find_min=False,
        )

    def low_min(self, start_pos: int, end_pos: int) -> tuple[float, int]:
        return _query_range(
            self._low_min_values,
            self._low_min_positions,
            start_pos,
            end_pos,
            find_min=True,
        )

    def close_min(self, start_pos: int, end_pos: int) -> tuple[float, int]:
        return _query_range(
            self._close_min_values,
            self._close_min_positions,
            start_pos,
            end_pos,
            find_min=True,
        )


def _build_range_table(values: np.ndarray, *, find_min: bool) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Build sparse value and absolute-position tables for one input array."""
    values = np.asarray(values)
    value_levels = [values]
    position_levels = [np.arange(len(values), dtype=np.int32)]
    level = 1
    while (1 << level) <= len(values):
        offset = 1 << (level - 1)
        left_values = value_levels[-1][:-offset]
        right_values = value_levels[-1][offset:]
        if find_min:
            choose_left = left_values <= right_values
        else:
            choose_left = left_values >= right_values
        value_levels.append(np.where(choose_left, left_values, right_values))
        left_positions = position_levels[-1][:-offset]
        right_positions = position_levels[-1][offset:]
        position_levels.append(np.where(choose_left, left_positions, right_positions))
        level += 1
    return value_levels, position_levels


def _query_range(
    value_levels: list[np.ndarray],
    position_levels: list[np.ndarray],
    start_pos: int,
    end_pos: int,
    *,
    find_min: bool,
) -> tuple[float, int]:
    """Return an inclusive value/first absolute position for ``[start:end)``."""
    length = end_pos - start_pos
    level = length.bit_length() - 1
    right_start = end_pos - (1 << level)
    left_value = value_levels[level][start_pos]
    right_value = value_levels[level][right_start]
    if (find_min and left_value <= right_value) or (not find_min and left_value >= right_value):
        return float(left_value), int(position_levels[level][start_pos])
    return float(right_value), int(position_levels[level][right_start])


def detect_proper_base(
    history_before_event: pd.DataFrame,
    *,
    event_session: str,
    policy: BasePolicy,
) -> BasePattern | None:
    """Return the most-recent valid pre-event flat base or cup-with-handle.

    The function is deliberately fail-closed: malformed history or an input
    containing the event session is rejected instead of being normalized or
    silently truncated.
    """
    history = _validate_pre_event_history(history_before_event, event_session)
    if len(history) < policy.flat_min_sessions:
        raise ValueError(f"history must contain at least {policy.flat_min_sessions} sessions")
    input_sha256 = _history_sha256(history)

    values = history.to_numpy(dtype=float, copy=False)
    return _detect_proper_base_arrays(
        index=history.index,
        highs=values[:, 0],
        lows=values[:, 1],
        closes=values[:, 2],
        policy=policy,
        input_sha256=input_sha256,
    )


def _detect_proper_base_reference(
    history_before_event: pd.DataFrame,
    *,
    event_session: str,
    policy: BasePolicy,
) -> BasePattern | None:
    """Retain the original DataFrame implementation as a parity oracle.

    This is intentionally private and exercised only by regression tests.  It
    establishes that the production array path preserves the reviewed v1
    candidate ordering and every returned field.
    """
    history = _validate_pre_event_history(history_before_event, event_session)
    if len(history) < policy.flat_min_sessions:
        raise ValueError(f"history must contain at least {policy.flat_min_sessions} sessions")
    input_sha256 = _history_sha256(history)

    for end_pos in range(len(history), policy.flat_min_sessions - 1, -1):
        candidate_end = history.iloc[:end_pos]
        cup = _most_recent_cup(candidate_end, policy, input_sha256)
        if cup is not None:
            return cup
        flat = _most_recent_flat(candidate_end, policy, input_sha256)
        if flat is not None:
            return flat
    return None


def _detect_proper_base_arrays(
    *,
    index: pd.DatetimeIndex,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    policy: BasePolicy,
    input_sha256: str,
) -> BasePattern | None:
    """Evaluate canonical candidates without creating pandas slices per window.

    Candidate ordering deliberately matches ``_detect_proper_base_reference``:
    latest end-session first, then cup-with-handle before flat base, and the
    shortest qualifying duration within each kind.
    """
    ranges = _RangeExtrema(highs, lows, closes)
    for end_pos in range(len(index), policy.flat_min_sessions - 1, -1):
        cup = _most_recent_cup_arrays(index, highs, lows, closes, end_pos, policy, input_sha256, ranges=ranges)
        if cup is not None:
            return cup
        flat = _most_recent_flat_arrays(index, highs, lows, end_pos, policy, input_sha256, ranges=ranges)
        if flat is not None:
            return flat
    return None


def _most_recent_flat_arrays(
    index: pd.DatetimeIndex,
    highs: np.ndarray,
    lows: np.ndarray,
    end_pos: int,
    policy: BasePolicy,
    input_sha256: str,
    *,
    ranges: _RangeExtrema | None = None,
) -> BasePattern | None:
    max_sessions = min(policy.flat_max_sessions, end_pos)
    for duration in range(policy.flat_min_sessions, max_sessions + 1):
        start_pos = end_pos - duration
        if ranges is None:
            base_highs = highs[start_pos:end_pos]
            base_lows = lows[start_pos:end_pos]
            pivot = float(base_highs.max())
            low = float(base_lows.min())
            high_pos = start_pos + int(base_highs.argmax())
            low_pos = start_pos + int(base_lows.argmin())
        else:
            pivot, high_pos = ranges.high_max(start_pos, end_pos)
            low, low_pos = ranges.low_min(start_pos, end_pos)
        depth_pct = (pivot - low) / pivot
        if depth_pct > policy.flat_max_depth_pct:
            continue
        if high_pos >= low_pos or low_pos >= end_pos - 1:
            continue
        if ranges is None:
            post_low_high = float(base_highs[low_pos - start_pos + 1 :].max())
        else:
            post_low_high = ranges.high_max(low_pos + 1, end_pos)[0]
        if post_low_high < low + (pivot - low) * 0.5:
            continue
        return _pattern_from_positions(
            BaseKind.FLAT_BASE,
            index=index,
            start_pos=start_pos,
            end_pos=end_pos,
            pivot=pivot,
            low=low,
            depth_pct=depth_pct,
            input_sha256=input_sha256,
        )
    return None


def _most_recent_cup_arrays(
    index: pd.DatetimeIndex,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    end_pos: int,
    policy: BasePolicy,
    input_sha256: str,
    *,
    ranges: _RangeExtrema | None = None,
) -> BasePattern | None:
    max_sessions = min(policy.cup_max_sessions, end_pos)
    for duration in range(policy.cup_min_sessions, max_sessions + 1):
        start_pos = end_pos - duration
        pattern = _cup_pattern_arrays(
            index=index,
            highs=highs,
            lows=lows,
            closes=closes,
            start_pos=start_pos,
            end_pos=end_pos,
            policy=policy,
            input_sha256=input_sha256,
            ranges=ranges,
        )
        if pattern is not None:
            return pattern
    return None


def _cup_pattern_arrays(
    *,
    index: pd.DatetimeIndex,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    start_pos: int,
    end_pos: int,
    policy: BasePolicy,
    input_sha256: str,
    ranges: _RangeExtrema | None = None,
) -> BasePattern | None:
    left_lip = float(highs[start_pos])
    if ranges is None:
        base_lows = lows[start_pos:end_pos]
        low = float(base_lows.min())
        low_pos = int(base_lows.argmin())
    else:
        low, low_pos_absolute = ranges.low_min(start_pos, end_pos)
        low_pos = low_pos_absolute - start_pos
    duration = end_pos - start_pos
    if left_lip <= 0 or low_pos == 0:
        return None
    depth_pct = (left_lip - low) / left_lip
    if not policy.cup_min_depth_pct <= depth_pct <= policy.cup_max_depth_pct:
        return None

    midpoint = low + (left_lip - low) * 0.5
    max_handle_sessions = min(15, duration - low_pos - 1)
    for handle_sessions in range(3, max_handle_sessions + 1):
        handle_start = end_pos - handle_sessions
        if ranges is None:
            handle_lows = lows[handle_start:end_pos]
            handle_low = float(handle_lows.min())
        else:
            handle_low = ranges.low_min(handle_start, end_pos)[0]
        if handle_low <= midpoint:
            continue
        pre_handle_start = start_pos + low_pos + 1
        if pre_handle_start >= handle_start:
            continue
        if ranges is None:
            pre_handle_highs = highs[pre_handle_start:handle_start]
            right_lip = float(pre_handle_highs.max())
            right_lip_pos = int(pre_handle_highs.argmax())
        else:
            right_lip, right_lip_absolute = ranges.high_max(pre_handle_start, handle_start)
            right_lip_pos = right_lip_absolute - pre_handle_start
        if not left_lip * (1.0 - policy.right_lip_max_distance_pct) <= right_lip <= left_lip * (
            1.0 + policy.right_lip_max_distance_pct
        ):
            continue
        right_lip_close = float(closes[pre_handle_start + right_lip_pos])
        if ranges is None:
            handle_closes = closes[handle_start:end_pos]
            handle_close_low = float(handle_closes.min())
        else:
            handle_close_low = ranges.close_min(handle_start, end_pos)[0]
        if handle_close_low >= right_lip_close:
            continue
        if ranges is None:
            handle_highs = highs[handle_start:end_pos]
            pivot = float(handle_highs.max())
        else:
            pivot = ranges.high_max(handle_start, end_pos)[0]
        if (pivot - handle_low) / pivot > policy.handle_max_depth_pct:
            continue
        return _pattern_from_positions(
            BaseKind.CUP_WITH_HANDLE,
            index=index,
            start_pos=start_pos,
            end_pos=end_pos,
            pivot=pivot,
            low=low,
            depth_pct=depth_pct,
            input_sha256=input_sha256,
            handle_start_pos=handle_start,
        )
    return None


def _pattern_from_positions(
    kind: BaseKind,
    *,
    index: pd.DatetimeIndex,
    start_pos: int,
    end_pos: int,
    pivot: float,
    low: float,
    depth_pct: float,
    input_sha256: str,
    handle_start_pos: int | None = None,
) -> BasePattern:
    return BasePattern(
        kind=kind,
        start_session=_session_text(index[start_pos]),
        end_session=_session_text(index[end_pos - 1]),
        pivot=pivot,
        low=low,
        depth_pct=depth_pct,
        duration_sessions=end_pos - start_pos,
        handle_start_session=None if handle_start_pos is None else _session_text(index[handle_start_pos]),
        handle_end_session=None if handle_start_pos is None else _session_text(index[end_pos - 1]),
        input_sha256=input_sha256,
    )


def evaluate_new_high_entry(
    pattern: BasePattern | None,
    event_close: float,
    event_volume_ratio: float,
    *,
    max_extension_pct: float = 0.05,
    minimum_volume_ratio: float = 1.30,
) -> RuleOutcome:
    """Evaluate N.NEW_HIGH only from a structured base and event-bar facts."""
    if pattern is None:
        return RuleOutcome.unimplemented("N.NEW_HIGH", "E.PROPER_BASE")
    if not _finite_positive(event_close) or not _finite_positive(event_volume_ratio):
        return RuleOutcome.failed("N.NEW_HIGH", *_ENTRY_EVIDENCE_IDS)
    if not math.isfinite(max_extension_pct) or max_extension_pct < 0:
        raise ValueError("max_extension_pct must be a finite non-negative number")
    if not math.isfinite(minimum_volume_ratio) or minimum_volume_ratio <= 0:
        raise ValueError("minimum_volume_ratio must be a finite positive number")

    event_close = float(event_close)
    event_volume_ratio = float(event_volume_ratio)
    in_buy_zone = pattern.pivot <= event_close <= pattern.pivot * (1.0 + max_extension_pct)
    if not in_buy_zone or event_volume_ratio < minimum_volume_ratio:
        return RuleOutcome.failed("N.NEW_HIGH", *_ENTRY_EVIDENCE_IDS)
    return RuleOutcome.passed("N.NEW_HIGH", *_ENTRY_EVIDENCE_IDS)


def _most_recent_flat(history: pd.DataFrame, policy: BasePolicy, input_sha256: str) -> BasePattern | None:
    max_sessions = min(policy.flat_max_sessions, len(history))
    for duration in range(policy.flat_min_sessions, max_sessions + 1):
        base = history.iloc[-duration:]
        pivot = float(base["High"].max())
        low = float(base["Low"].min())
        depth_pct = (pivot - low) / pivot
        if depth_pct <= policy.flat_max_depth_pct and _is_flat_consolidation(base, pivot, low):
            return _pattern(
                BaseKind.FLAT_BASE,
                base,
                pivot=pivot,
                low=low,
                depth_pct=depth_pct,
                input_sha256=input_sha256,
            )
    return None


def _is_flat_consolidation(base: pd.DataFrame, pivot: float, low: float) -> bool:
    """Require a prior peak, pullback, and recovery instead of a directional trend."""
    high_pos = int(base["High"].to_numpy().argmax())
    low_pos = int(base["Low"].to_numpy().argmin())
    if high_pos >= low_pos or low_pos >= len(base) - 1:
        return False
    post_low_high = float(base["High"].iloc[low_pos + 1 :].max())
    return post_low_high >= low + (pivot - low) * 0.5


def _most_recent_cup(history: pd.DataFrame, policy: BasePolicy, input_sha256: str) -> BasePattern | None:
    max_sessions = min(policy.cup_max_sessions, len(history))
    for duration in range(policy.cup_min_sessions, max_sessions + 1):
        base = history.iloc[-duration:]
        pattern = _cup_pattern(base, policy, input_sha256)
        if pattern is not None:
            return pattern
    return None


def _cup_pattern(base: pd.DataFrame, policy: BasePolicy, input_sha256: str) -> BasePattern | None:
    left_lip = float(base["High"].iloc[0])
    low = float(base["Low"].min())
    low_pos = int(base["Low"].to_numpy().argmin())
    if left_lip <= 0 or low_pos == 0:
        return None
    depth_pct = (left_lip - low) / left_lip
    if not policy.cup_min_depth_pct <= depth_pct <= policy.cup_max_depth_pct:
        return None

    midpoint = low + (left_lip - low) * 0.5
    max_handle_sessions = min(15, len(base) - low_pos - 1)
    for handle_sessions in range(3, max_handle_sessions + 1):
        handle = base.iloc[-handle_sessions:]
        if float(handle["Low"].min()) <= midpoint:
            continue
        pre_handle = base.iloc[low_pos + 1 : -handle_sessions]
        if pre_handle.empty:
            continue
        right_lip = float(pre_handle["High"].max())
        if not left_lip * (1.0 - policy.right_lip_max_distance_pct) <= right_lip <= left_lip * (
            1.0 + policy.right_lip_max_distance_pct
        ):
            continue
        right_lip_pos = int(pre_handle["High"].to_numpy().argmax())
        right_lip_close = float(pre_handle["Close"].iloc[right_lip_pos])
        if float(handle["Close"].min()) >= right_lip_close:
            continue
        pivot = float(handle["High"].max())
        handle_low = float(handle["Low"].min())
        if (pivot - handle_low) / pivot > policy.handle_max_depth_pct:
            continue
        return _pattern(
            BaseKind.CUP_WITH_HANDLE,
            base,
            pivot=pivot,
            low=low,
            depth_pct=depth_pct,
            input_sha256=input_sha256,
            handle=handle,
        )
    return None


def _pattern(
    kind: BaseKind,
    base: pd.DataFrame,
    *,
    pivot: float,
    low: float,
    depth_pct: float,
    input_sha256: str,
    handle: pd.DataFrame | None = None,
) -> BasePattern:
    handle_start = None if handle is None else _session_text(handle.index[0])
    handle_end = None if handle is None else _session_text(handle.index[-1])
    return BasePattern(
        kind=kind,
        start_session=_session_text(base.index[0]),
        end_session=_session_text(base.index[-1]),
        pivot=pivot,
        low=low,
        depth_pct=depth_pct,
        duration_sessions=len(base),
        handle_start_session=handle_start,
        handle_end_session=handle_end,
        input_sha256=input_sha256,
    )


def _validate_pre_event_history(history_before_event: pd.DataFrame, event_session: str) -> pd.DataFrame:
    if not isinstance(history_before_event, pd.DataFrame):
        raise ValueError("history_before_event must be a DataFrame")
    missing = [column for column in _REQUIRED_OHLC_COLUMNS if column not in history_before_event.columns]
    if missing:
        raise ValueError(f"history is missing required OHLC columns: {missing}")
    if history_before_event.empty:
        raise ValueError("history must not be empty")
    try:
        index = pd.DatetimeIndex(pd.to_datetime(history_before_event.index, errors="raise"))
        event = pd.Timestamp(event_session)
    except (TypeError, ValueError) as exc:
        raise ValueError("history index and event_session must be valid sessions") from exc
    if index.hasnans or index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError("history index must be sorted and contain no duplicates")
    if index.tz is not None:
        index = index.tz_localize(None)
    index = index.normalize()
    if event.tzinfo is not None:
        event = event.tz_localize(None)
    event = event.normalize()
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError("history index must have one sorted row per session")
    if (index >= event).any():
        raise ValueError("history rows must be strictly before event_session")
    history = history_before_event.loc[:, _REQUIRED_OHLC_COLUMNS].copy()
    history.index = index
    for column in _REQUIRED_OHLC_COLUMNS:
        history[column] = pd.to_numeric(history[column], errors="raise")
    values = history.to_numpy(dtype=float)
    if not values.size or not bool(pd.notna(history).to_numpy().all()) or not math.isfinite(float(values.min())) or not math.isfinite(float(values.max())):
        raise ValueError("history OHLC values must be finite")
    if (
        (history <= 0).to_numpy().any()
        or (history["High"] < history["Low"]).any()
        or (history["Close"] < history["Low"]).any()
        or (history["Close"] > history["High"]).any()
    ):
        raise ValueError("history OHLC values must be positive and internally consistent")
    return history


def _history_sha256(history: pd.DataFrame) -> str:
    # ``iterrows`` materializes a Series for every session.  The validated
    # production history is a DatetimeIndex with three numeric columns, so
    # retain the exact v1 JSON row representation while extracting the values
    # from one NumPy block instead.  ``float`` conversion is intentional: it
    # preserves Python's canonical JSON number formatting used by v1.
    if isinstance(history.index, pd.DatetimeIndex):
        session_texts = history.index.strftime("%Y-%m-%d")
        values = history.loc[:, _REQUIRED_OHLC_COLUMNS].to_numpy(dtype=float, copy=False)
        rows = [
            [session, float(high), float(low), float(close)]
            for session, (high, low, close) in zip(session_texts, values, strict=True)
        ]
    else:
        # Keep the private helper compatible for callers outside the validated
        # public path, whose original implementation accepted any index that
        # ``_session_text`` could parse.
        rows = [
            [_session_text(index), float(row.High), float(row.Low), float(row.Close)]
            for index, row in history.iterrows()
        ]
    payload = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _session_text(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()
