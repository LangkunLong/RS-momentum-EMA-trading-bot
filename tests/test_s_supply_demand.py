"""Unit tests for S (Supply and Demand) component."""

import pandas as pd

from core.canslim.s_supply_demand import _calculate_up_down_volume_ratio, _detect_breakout, _detect_volume_surge


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
