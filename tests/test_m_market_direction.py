"""Unit tests for M (Market Direction) component."""

import pandas as pd

from core.canslim.m_market_direction import _count_distribution_days, _detect_follow_through_day


def test_count_distribution_days():
    closes = pd.Series([100, 99, 98.5, 99.5, 98])
    volumes = pd.Series([1000, 1500, 1600, 1000, 2000])
    # 99 < 100 on higher vol (dist), 98.5 < 99 on higher vol (dist), 98 < 99.5 on higher vol (dist)
    dist = _count_distribution_days(closes, volumes, lookback=4)
    assert dist == 3


def test_detect_follow_through_day():
    closes = pd.Series([100, 95, 96, 97, 98, 100])
    volumes = pd.Series([1000, 1000, 1100, 1000, 1100, 1500])
    # Down day then 4 up days, last day >1.5% and high volume
    ftd = _detect_follow_through_day(closes, volumes, min_rally_pct=0.015, min_rally_day=4)
    assert ftd is True
