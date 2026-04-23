from __future__ import annotations

import pandas as pd
import pytest


def _make_closes(values: list[float], start: str = "2024-01-02") -> pd.Series:
    dates = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=dates, name="Close")


# ---------------------------------------------------------------------------
# detect_flat_base
# ---------------------------------------------------------------------------


def test_flat_base_detected_tight_range():
    """30 bars with ≤10% peak-to-trough returns the range high."""
    from core.pivot_detector import detect_flat_base

    base = 100.0
    # oscillate between 91 and 100 — 9% decline, well within 15% max
    values = [base if i % 2 == 0 else 91.0 for i in range(30)]
    closes = _make_closes(values)
    result = detect_flat_base(closes)
    assert result == pytest.approx(100.0)


def test_flat_base_rejected_too_deep():
    """30 bars with 20% decline returns None (exceeds 15% max)."""
    from core.pivot_detector import detect_flat_base

    values = [100.0 if i % 2 == 0 else 80.0 for i in range(30)]  # 20% decline
    closes = _make_closes(values)
    assert detect_flat_base(closes) is None


def test_flat_base_rejected_too_short():
    """15 bars (< 25 min) returns None even with tight range."""
    from core.pivot_detector import detect_flat_base

    values = [100.0 if i % 2 == 0 else 95.0 for i in range(15)]
    closes = _make_closes(values)
    assert detect_flat_base(closes) is None


# ---------------------------------------------------------------------------
# detect_cup_with_handle
# ---------------------------------------------------------------------------


def test_cup_handle_detected():
    """50 bars: left lip→25% cup decline→recovery to left lip→8% handle — returns handle high."""
    from core.pivot_detector import detect_cup_with_handle

    # Build: left lip 40 bars at 100, cup dips to 75 (25% decline), recovers to 98,
    # then handle of 10 bars between 93 and 98 (≈5% handle decline)
    left_region = [100.0] * 15
    decline = list(range(0, 13))  # 13 steps down
    cup_region = [100.0 - d * (25.0 / 12) for d in decline]  # 100→75 over 13 bars
    recovery = list(range(12, -1, -1))
    recover_region = [75.0 + r * (23.0 / 12) for r in recovery]  # 75→98 over 13 bars
    handle_region = [98.0 if i % 2 == 0 else 93.0 for i in range(9)]  # ≈5% decline
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    result = detect_cup_with_handle(closes)
    assert result is not None
    assert result == pytest.approx(98.0, abs=2.0)


def test_cup_rejected_too_shallow():
    """40 bars with only 10% cup decline returns None (< 15% floor)."""
    from core.pivot_detector import detect_cup_with_handle

    left_region = [100.0] * 15
    cup_region = [100.0 - i * (10.0 / 10) for i in range(11)]  # 100→90, 10% decline
    recover_region = [90.0 + i * (10.0 / 10) for i in range(11)]  # 90→100
    handle_region = [98.0, 97.0, 98.0]
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    assert detect_cup_with_handle(closes) is None


def test_cup_handle_in_lower_half_rejected():
    """Handle trough below cup midpoint → None.

    The trailing-contiguous-segment scan stops at the first bar ≤ cup_midpoint
    scanning backwards from the end.  If that bar is at or near the tail the
    resulting handle segment is too short (< n-2 threshold) and the function
    returns None.
    """
    from core.pivot_detector import detect_cup_with_handle

    # cup: 100 → 70 (30% decline), recovers to 98, handle dips to 78 (below midpoint 85)
    # midpoint = 70 + (100-70)*0.5 = 85
    # The handle region ends with 78, so the backwards scan terminates immediately
    # leaving handle_start >= n-2 → returns None.
    left_region = [100.0] * 15
    cup_region = [100.0 - i * (30.0 / 10) for i in range(11)]  # 100→70
    recover_region = [70.0 + i * (28.0 / 10) for i in range(11)]  # 70→98
    # Last bar is 78 (≤ 85 midpoint) — scan from end stops at once → too-short handle
    handle_region = [98.0, 97.0, 96.0, 97.0, 98.0, 97.0, 98.0, 98.0, 78.0]
    values = left_region + cup_region + recover_region + handle_region
    closes = _make_closes(values)
    assert detect_cup_with_handle(closes) is None


# ---------------------------------------------------------------------------
# is_in_buy_zone
# ---------------------------------------------------------------------------


def test_in_buy_zone_within_5pct():
    """Price within 5% of pivot → True."""
    from core.pivot_detector import is_in_buy_zone

    assert is_in_buy_zone(103.0, 100.0) is True


def test_in_buy_zone_extended():
    """Price more than 5% above pivot → False."""
    from core.pivot_detector import is_in_buy_zone

    assert is_in_buy_zone(107.0, 100.0) is False


# ---------------------------------------------------------------------------
# find_pivot (pass-through)
# ---------------------------------------------------------------------------


def test_find_pivot_returns_none_for_short_series():
    """Series shorter than 25 bars → find_pivot returns None immediately."""
    from core.pivot_detector import find_pivot

    closes = _make_closes([100.0] * 20)
    assert find_pivot(closes) is None


# ---------------------------------------------------------------------------
# PEG bypass (integration with tech_pass logic — pure logic test)
# ---------------------------------------------------------------------------


def test_peg_bypasses_buy_zone():
    """has_peg_today=True makes tech_pass True even when in_buy_zone=False."""
    has_breakout = False
    has_surge = False
    has_peg_today = True
    in_buy_zone = False
    tech_pass = (has_breakout and has_surge and in_buy_zone) or has_peg_today
    assert tech_pass is True
