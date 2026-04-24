from __future__ import annotations

"""O'Neil-style pivot pattern detection: flat base and cup-with-handle."""

import pandas as pd

import config.settings as settings


def detect_flat_base(closes: pd.Series) -> float | None:
    """Return the flat base pivot price, or None if no valid flat base detected.

    A flat base has a peak-to-trough decline ≤ PIVOT_FLAT_BASE_MAX_DECLINE_PCT
    over at least PIVOT_FLAT_BASE_MIN_WEEKS * 5 trading bars.
    The pivot is the range high (resistance level of the consolidation).
    """
    min_bars = settings.PIVOT_FLAT_BASE_MIN_WEEKS * 5
    if len(closes) < min_bars:
        return None
    base_high = float(closes.max())
    base_low = float(closes.min())
    if base_high <= 0:
        return None
    decline = (base_high - base_low) / base_high
    if decline > settings.PIVOT_FLAT_BASE_MAX_DECLINE_PCT:  # inclusive: exactly 15% is accepted
        return None
    return base_high


def detect_cup_with_handle(closes: pd.Series) -> float | None:
    """Return the cup-with-handle pivot price, or None if pattern not detected.

    Cup must be 15–33% deep over ≥ PIVOT_CUP_MIN_WEEKS * 5 bars.
    Right lip must recover to within 5% of left lip in the pre-handle region.
    Handle is the trailing contiguous segment where every bar exceeds the cup midpoint,
    with a pullback of ≤ PIVOT_HANDLE_MAX_DECLINE_PCT. Pivot is the handle high.
    """
    min_bars = settings.PIVOT_CUP_MIN_WEEKS * 5
    if len(closes) < min_bars:
        return None

    left_lip = float(closes.iloc[0])
    if left_lip <= 0:
        return None

    # Cup low must occur within first 80% of the window
    cup_region_end = int(len(closes) * 0.80)
    cup_low_idx = int(closes.iloc[:cup_region_end].argmin())
    cup_low = float(closes.iloc[cup_low_idx])

    cup_decline = (left_lip - cup_low) / left_lip
    if cup_decline < settings.PIVOT_CUP_MIN_DECLINE_PCT or cup_decline > settings.PIVOT_CUP_MAX_DECLINE_PCT:
        return None

    cup_midpoint = cup_low + (left_lip - cup_low) * 0.5

    # Find handle: the trailing contiguous segment where every bar is above the cup midpoint.
    # Scan backwards from the end, stopping at the first bar at or below the midpoint.
    n = len(closes)
    handle_start = n
    for i in range(n - 1, cup_low_idx, -1):
        if closes.iloc[i] <= cup_midpoint:
            break
        handle_start = i

    if handle_start >= n - 2:
        return None

    handle = closes.iloc[handle_start:]
    handle_high = float(handle.max())
    handle_low = float(handle.min())
    if handle_high <= 0:
        return None

    handle_decline = (handle_high - handle_low) / handle_high
    if handle_decline > settings.PIVOT_HANDLE_MAX_DECLINE_PCT:
        return None

    # Right lip must be >= 95% of left lip somewhere in the pre-handle post-cup region.
    post_cup_before_handle = closes.iloc[cup_low_idx:handle_start]
    right_lip = float(post_cup_before_handle.max())
    if right_lip < left_lip * 0.95:
        return None

    return handle_high


def find_pivot(closes: pd.Series) -> float | None:
    """Entry point: try cup-with-handle first, fall back to flat base.

    Accepts a pre-sliced closing price series (caller is responsible for
    slicing to eval_date). Returns None if no pattern detected — callers
    should treat None as a pass-through (do not block the signal).
    """
    if len(closes) < 25:
        return None
    lookback = min(65, len(closes))
    window = closes.iloc[-lookback:]
    pivot = detect_cup_with_handle(window)
    if pivot is not None:
        return pivot
    return detect_flat_base(window)


def is_in_buy_zone(
    current_price: float,
    pivot: float,
    zone_pct: float = settings.PIVOT_BUY_ZONE_PCT,
) -> bool:
    """Return True if current_price is at or above pivot and within zone_pct above it."""
    return pivot <= current_price <= pivot * (1 + zone_pct)
