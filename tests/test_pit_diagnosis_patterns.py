from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from core.pit_diagnosis.patterns import (
    BaseKind,
    BasePolicy,
    _RangeExtrema,
    _detect_proper_base_reference,
    _history_sha256,
    detect_proper_base,
    evaluate_new_high_entry,
)


def _flat_base_before_event() -> pd.DataFrame:
    index = pd.bdate_range("2024-01-02", periods=29)
    closes = pd.Series([100.0] * 5 + [98.0, 97.0, 99.0, 100.0] * 6, index=index)
    return pd.DataFrame({"High": closes + 0.25, "Low": closes - 0.25, "Close": closes})


def test_proper_base_excludes_event_bar_and_returns_auditable_shape() -> None:
    """Break caught: an entry bar was included while defining its own base."""
    before = _flat_base_before_event()
    event_session = (before.index[-1] + pd.offsets.BDay()).date().isoformat()

    pattern = detect_proper_base(
        before,
        event_session=event_session,
        policy=BasePolicy.canonical_v1(),
    )

    assert pattern is not None
    assert pattern.kind is BaseKind.FLAT_BASE
    assert pattern.end_session == before.index[-1].date().isoformat()
    base = before.loc[pattern.start_session : pattern.end_session]
    assert pattern.pivot == pytest.approx(base["High"].max())
    assert pattern.depth_pct <= 0.15
    assert pattern.duration_sessions == len(base)
    assert len(pattern.input_sha256) == 64


def test_event_session_in_pattern_input_is_rejected() -> None:
    """Break caught: future/event-session OHLC could establish a supposedly causal base."""
    before = _flat_base_before_event()
    event_session = before.index[-1].date().isoformat()

    with pytest.raises(ValueError, match="before event_session"):
        detect_proper_base(before, event_session=event_session, policy=BasePolicy.canonical_v1())


def test_monotonic_rally_is_not_a_flat_base() -> None:
    """Break caught: a rising trend with no consolidation was certified as E.PROPER_BASE."""
    index = pd.bdate_range("2024-01-02", periods=25)
    closes = [100.0 * (1.005**day) for day in range(len(index))]
    history = pd.DataFrame(
        {"High": [close + 0.25 for close in closes], "Low": [close - 0.25 for close in closes], "Close": closes},
        index=index,
    )
    event_session = (index[-1] + pd.offsets.BDay()).date().isoformat()

    pattern = detect_proper_base(history, event_session=event_session, policy=BasePolicy.canonical_v1())

    assert pattern is None


def test_cup_with_handle_records_pre_event_handle_bounds() -> None:
    """Break caught: a valid cup-and-handle was discarded or lost its auditable handle dates."""
    index = pd.bdate_range("2024-01-02", periods=40)
    closes = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 86.0, 84.0, 82.0, 80.0]
    closes += [82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0]
    closes += [99.0] * 15 + [98.0, 97.0, 98.0, 99.0]
    assert len(closes) == len(index)
    history = pd.DataFrame(
        {"High": [close + 0.25 for close in closes], "Low": [close - 0.25 for close in closes], "Close": closes},
        index=index,
    )
    event_session = (index[-1] + pd.offsets.BDay()).date().isoformat()

    pattern = detect_proper_base(history, event_session=event_session, policy=BasePolicy.canonical_v1())

    assert pattern is not None
    assert pattern.kind is BaseKind.CUP_WITH_HANDLE
    assert pattern.handle_start_session is not None
    assert pattern.handle_end_session == history.index[-1].date().isoformat()


def test_cup_recovery_without_handle_pullback_is_not_a_proper_base() -> None:
    """Break caught: a post-cup plateau without a retracement was called a cup-with-handle."""
    index = pd.bdate_range("2024-01-02", periods=40)
    closes = [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 86.0, 84.0, 82.0, 80.0]
    closes += [82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0]
    closes += [99.0] * 19
    assert len(closes) == len(index)
    history = pd.DataFrame(
        {"High": [close + 0.25 for close in closes], "Low": [close - 0.25 for close in closes], "Close": closes},
        index=index,
    )
    event_session = (index[-1] + pd.offsets.BDay()).date().isoformat()

    pattern = detect_proper_base(history, event_session=event_session, policy=BasePolicy.canonical_v1())

    assert pattern is None


def test_near_high_without_proper_base_fails_newness() -> None:
    """Break caught: a generic prior high silently passed the N entry rule."""
    outcome = evaluate_new_high_entry(None, 100.0, 2.0)

    assert outcome.status == "unimplemented"
    assert outcome.rule_id == "N.NEW_HIGH"
    assert "E.PROPER_BASE" in outcome.evidence_ids


def test_new_high_entry_requires_pivot_buy_zone_and_volume_confirmation() -> None:
    """Break caught: a proper base could pass N despite an under-pivot price or weak volume."""
    before = _flat_base_before_event()
    event_session = (before.index[-1] + pd.offsets.BDay()).date().isoformat()
    pattern = detect_proper_base(before, event_session=event_session, policy=BasePolicy.canonical_v1())
    assert pattern is not None

    outcome = evaluate_new_high_entry(pattern, pattern.pivot * 1.02, 1.30)

    assert outcome.status == "passed"
    assert outcome.rule_id == "N.NEW_HIGH"
    assert outcome.evidence_ids == (
        "E.PROPER_BASE",
        "E.PIVOT",
        "E.BUY_ZONE",
        "S.VOLUME_CONFIRMATION",
        "N.NEW_HIGH",
    )


@pytest.mark.parametrize(
    "history_factory",
    (
        _flat_base_before_event,
        lambda: pd.DataFrame(
            {
                "High": [100.25, 98.25, 96.25, 94.25, 92.25, 90.25, 88.25, 86.25, 84.25, 82.25, 80.25]
                + [value + 0.25 for value in [82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0]]
                + [99.25] * 15
                + [98.25, 97.25, 98.25, 99.25],
                "Low": [99.75, 97.75, 95.75, 93.75, 91.75, 89.75, 87.75, 85.75, 83.75, 81.75, 79.75]
                + [value - 0.25 for value in [82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0]]
                + [98.75] * 15
                + [97.75, 96.75, 97.75, 98.75],
                "Close": [100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 86.0, 84.0, 82.0, 80.0]
                + [82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0]
                + [99.0] * 15
                + [98.0, 97.0, 98.0, 99.0],
            },
            index=pd.bdate_range("2024-01-02", periods=40),
        ),
        lambda: pd.DataFrame(
            {
                "High": [100.25 * (1.005**day) for day in range(131)],
                "Low": [99.75 * (1.005**day) for day in range(131)],
                "Close": [100.0 * (1.005**day) for day in range(131)],
            },
            index=pd.bdate_range("2024-01-02", periods=131),
        ),
    ),
)
def test_array_detector_matches_reference_for_flat_cup_and_no_pattern(history_factory) -> None:
    """Break caught: array fast path changed canonical pattern precedence or fields."""
    history = history_factory()
    event_session = (history.index[-1] + pd.offsets.BDay()).date().isoformat()

    expected = _detect_proper_base_reference(
        history,
        event_session=event_session,
        policy=BasePolicy.canonical_v1(),
    )
    actual = detect_proper_base(
        history,
        event_session=event_session,
        policy=BasePolicy.canonical_v1(),
    )

    assert actual == expected


def _legacy_history_sha256(history: pd.DataFrame) -> str:
    rows = [
        [
            pd.Timestamp(index).date().isoformat(),
            float(row.High),
            float(row.Low),
            float(row.Close),
        ]
        for index, row in history.iterrows()
    ]
    payload = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_history_hash_is_byte_identical_to_v1_iterrows_encoding() -> None:
    """Break caught: the faster hash serializer changed canonical evidence IDs."""
    index = pd.date_range("2024-01-02", periods=17, freq="B", tz="America/New_York")
    values = np.array(
        [
            [100.25, 99.75, 100.0],
            [101.125, 99.5, 100.75],
            [100.5, 98.25, 99.0],
        ]
        * 6,
        dtype=float,
    )[: len(index)]
    history = pd.DataFrame(values, columns=["High", "Low", "Close"], index=index)
    history["unused"] = 1

    assert _history_sha256(history) == _legacy_history_sha256(history)

    malformed = history.copy()
    malformed.iloc[2, 0] = np.nan
    with pytest.raises(ValueError, match="Out of range float values"):
        _history_sha256(malformed)
    with pytest.raises(ValueError, match="Out of range float values"):
        _legacy_history_sha256(malformed)


def test_sparse_range_extrema_preserves_first_arg_position_on_ties() -> None:
    """Break caught: range caching changed v1 argmin/argmax tie precedence."""
    highs = np.array([4.0, 7.0, 7.0, 2.0, 7.0, 3.0, 3.0, 9.0])
    lows = np.array([5.0, 1.0, 1.0, 2.0, 1.0, 0.0, 0.0, 4.0])
    closes = np.array([4.0, 6.0, 6.0, 2.0, 6.0, 2.0, 2.0, 8.0])
    ranges = _RangeExtrema(highs, lows, closes)

    for start in range(len(highs)):
        for end in range(start + 1, len(highs) + 1):
            high_slice = highs[start:end]
            low_slice = lows[start:end]
            close_slice = closes[start:end]
            high, high_pos = ranges.high_max(start, end)
            low, low_pos = ranges.low_min(start, end)
            close, close_pos = ranges.close_min(start, end)
            assert high == float(high_slice.max())
            assert high_pos == start + int(high_slice.argmax())
            assert low == float(low_slice.min())
            assert low_pos == start + int(low_slice.argmin())
            assert close == float(close_slice.min())
            assert close_pos == start + int(close_slice.argmin())
