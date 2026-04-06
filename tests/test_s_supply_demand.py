"""Unit tests for S (Supply and Demand) component."""

import numpy as np
import pandas as pd

from core.canslim.s_supply_demand import (
    _calculate_up_down_volume_ratio,
    _detect_breakout,
    _detect_volume_surge,
    evaluate_s,
)


def test_detect_volume_surge():
    # Default price_up=True — backward compatible
    assert _detect_volume_surge(1500, 1000, 1.5) == (True, 1.5)
    assert _detect_volume_surge(1400, 1000, 1.5) == (False, 1.4)
    # Explicit UP day
    assert _detect_volume_surge(1500, 1000, 1.5, price_up=True) == (True, 1.5)
    # Surge on DOWN day — volume exceeds threshold but direction is wrong
    assert _detect_volume_surge(1500, 1000, 1.5, price_up=False) == (False, 1.5)
    # Below threshold on DOWN day
    assert _detect_volume_surge(1400, 1000, 1.5, price_up=False) == (False, 1.4)


def test_detect_breakout():
    assert _detect_breakout(98, 100, 0.98) == (True, 0.98)
    assert _detect_breakout(97, 100, 0.98) == (False, 0.97)


def test_calculate_up_down_volume_ratio():
    closes = pd.Series([10, 11, 10.5, 12])
    volumes = pd.Series([100, 200, 100, 300])  # Up days: (11) vol 200, (12) vol 300. Down days: (10.5) vol 100.
    df = pd.DataFrame({"Close": closes, "Volume": volumes})
    ratio = _calculate_up_down_volume_ratio(df)
    assert ratio == 2.5  # (200+300)/2 = 250 avg up volume. down volume = 100. Ratio = 2.5


def _make_price_history(n: int = 60, base_price: float = 100.0, gap_today: bool = False) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame for testing evaluate_s()."""
    rng = np.random.default_rng(42)
    closes = base_price + rng.normal(0, 1, n)
    closes = np.clip(closes, base_price * 0.9, base_price * 1.02)
    opens = closes + rng.normal(0, 0.5, n)
    if gap_today:
        # Simulate a 3% gap-up today on heavy volume
        opens[-1] = closes[-2] * 1.03
        closes[-1] = closes[-2] * 1.03
    highs = np.maximum(opens, closes) + abs(rng.normal(0, 0.3, n))
    lows = np.minimum(opens, closes) - abs(rng.normal(0, 0.3, n))
    volumes = np.full(n, 1_000_000.0)
    if gap_today:
        volumes[-1] = 2_000_000.0  # 2x surge on gap day
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes})


def test_peg_suppressed_when_stock_deep_in_correction():
    """PEG must not fire when stock is >15% below its 52-week high."""
    df = _make_price_history(n=60, base_price=80.0, gap_today=True)
    high_52w = 100.0  # stock is at ~80% of its 52-week high
    current_price = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].iloc[:-1].mean())

    _, metrics = evaluate_s(df, avg_vol, current_price, high_52w)

    # proximity ≈ 0.80 < S_PEG_MIN_PROXIMITY (0.85) → gap must be suppressed
    assert metrics["has_power_gap"] is False


def test_peg_allowed_when_stock_near_highs():
    """PEG should fire when stock is within 15% of its 52-week high."""
    df = _make_price_history(n=60, base_price=96.0, gap_today=True)
    high_52w = 100.0  # stock is at ~96% of its 52-week high
    current_price = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].iloc[:-1].mean())

    _, metrics = evaluate_s(df, avg_vol, current_price, high_52w)

    # proximity ≈ 0.96 >= S_PEG_MIN_PROXIMITY (0.85) → gap should be detected
    assert metrics["has_power_gap"] is True
