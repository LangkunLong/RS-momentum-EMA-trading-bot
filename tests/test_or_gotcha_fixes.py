"""Regression tests for the `x or settings.X` gotcha.

Each test verifies that a 0.0 (or otherwise falsy) caller-supplied value is
honoured instead of being silently replaced by the settings default.

All tests in this file are expected to FAIL before the fix and PASS after it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.canslim.a_annual_earnings import evaluate_a
from core.canslim.c_current_earnings import evaluate_c
from core.canslim.m_market_direction import evaluate_m
from core.canslim.n_new_products import evaluate_n


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_quarterly_income(eps_values: list[float]) -> pd.DataFrame:
    """5-quarter income statement DataFrame for C / N tests."""
    dates = pd.date_range("2023-01-01", periods=len(eps_values), freq="QE")
    return pd.DataFrame(
        {d: {"Diluted EPS": v} for d, v in zip(dates, eps_values, strict=True)}
    )


def _make_annual_income(eps_values: list[float]) -> pd.DataFrame:
    """Annual income statement DataFrame for A tests."""
    dates = pd.date_range("2020-01-01", periods=len(eps_values), freq="YE")
    return pd.DataFrame(
        {d: {"Diluted EPS": v} for d, v in zip(dates, eps_values, strict=True)}
    )


def _make_revenue_income(rev_values: list[float]) -> pd.DataFrame:
    """Sparse revenue frame whose latest period has a true prior-year match."""
    dates = pd.to_datetime(["2023-03-31", "2023-06-30", "2023-09-30", "2024-03-31"])
    if len(rev_values) != len(dates):
        raise ValueError("N weight fixture requires four literal fiscal periods")
    return pd.DataFrame(
        {d: {"Total Revenue": v} for d, v in zip(dates, rev_values, strict=True)}
    )


def _flat_market_df(n: int = 300, trend: str = "flat") -> pd.DataFrame:
    """Return a synthetic price DataFrame for evaluate_m tests."""
    if trend == "bull":
        closes = np.linspace(100, 200, n)
    elif trend == "bear":
        closes = np.linspace(200, 100, n)
    else:
        closes = np.full(n, 150.0)
    dates = pd.date_range("2023-01-01", periods=n)
    return pd.DataFrame(
        {
            "Open": closes * 0.99,
            "High": closes * 1.01,
            "Low": closes * 0.98,
            "Close": closes,
            "Volume": np.random.default_rng(42).integers(1_000_000, 5_000_000, n),
        },
        index=dates,
    )


# ─── C: c_growth_target ──────────────────────────────────────────────────────


def test_c_lower_growth_target_gives_higher_score() -> None:
    """A lower target means easier bar — same growth should score higher.

    With 40% YoY growth:
      target=0.25 → growth_score = clip(0.4/0.25, 0, 2)/2 = clip(1.6, 0, 2)/2 = 0.80
      target=0.10 → growth_score = clip(0.4/0.10, 0, 2)/2 = clip(4.0, 0, 2)/2 = 1.00

    If the `or` gotcha is present, both calls use target=0.25 → equal scores → FAIL.
    """
    # 5 quarters: oldest→newest, most recent quarter is 40% above same-Q last year
    income = _make_quarterly_income([1.0, 1.1, 1.2, 1.3, 1.4])
    score_high_target, _ = evaluate_c(income, c_growth_target=0.25)
    score_low_target, _ = evaluate_c(income, c_growth_target=0.10)
    assert score_low_target > score_high_target, (
        f"Lower c_growth_target should yield higher score; "
        f"got low={score_low_target:.3f}, high={score_high_target:.3f}"
    )


# ─── A: a_growth_target ──────────────────────────────────────────────────────


def test_a_lower_growth_target_gives_higher_score() -> None:
    """Same logic as C: lower target = higher score for identical growth.

    With 30% growth: target=0.25 yields growth_score<1.0, target=0.10 yields 1.0.
    """
    income = _make_annual_income([1.0, 1.3, 1.69, 2.20])
    score_high_target, _, _ = evaluate_a(income, a_growth_target=0.25)
    score_low_target, _, _ = evaluate_a(income, a_growth_target=0.10)
    assert score_low_target > score_high_target, (
        f"Lower a_growth_target should yield higher score; "
        f"got low={score_low_target:.3f}, high={score_high_target:.3f}"
    )


# ─── N: n_revenue_weight / n_proximity_weight ────────────────────────────────


def test_n_zero_revenue_weight_excludes_revenue_when_data_present() -> None:
    """n_revenue_weight=0.0 means all weight goes to proximity.

    With moderate revenue growth but stock AT new high:
      revenue alone  → moderate score
      proximity alone (proximity_to_high=0.99) → full score

    n_revenue_weight=0.0 should produce the same result as proximity-only.
    If the `or` gotcha is present, weight is silently set to 0.5 (equal split)
    and the score will be between revenue_score and proximity_score, not equal
    to proximity_score.
    """
    # Revenue grew 20% YoY (below 25% target → partial score)
    income = _make_revenue_income([100.0, 105.0, 110.0, 120.0])
    proximity = 0.99  # At new high → full proximity score

    score_zero_rev_weight, _ = evaluate_n(
        income, proximity_to_high=proximity, n_revenue_weight=0.0, n_proximity_weight=1.0
    )
    # Proximity at 0.99 → proximity_score = 1.0
    assert abs(score_zero_rev_weight - 1.0) < 0.01, (
        f"With n_revenue_weight=0.0 and proximity=0.99, score should be ~1.0; "
        f"got {score_zero_rev_weight:.3f}"
    )


def test_n_zero_proximity_weight_excludes_price_when_data_present() -> None:
    """n_proximity_weight=0.0 means all weight goes to revenue growth."""
    # Revenue grew 50% YoY → high revenue score
    income = _make_revenue_income([100.0, 105.0, 110.0, 150.0])
    # Stock is 60% below high → low proximity score
    proximity = 0.40

    score_zero_prox_weight, _ = evaluate_n(
        income, proximity_to_high=proximity, n_revenue_weight=1.0, n_proximity_weight=0.0
    )
    # Revenue: 50/100=0.5 growth, target=0.25, clip(0.5/0.25,0,2)/2=0.5→ revenue_score=0.5×2/2=1.0
    # Actually: clip(0.5/0.25, 0, 2) = clip(2.0, 0, 2) = 2.0 → /2 = 1.0
    # So score_zero_prox_weight should be ~1.0 (high revenue, price irrelevant)
    assert score_zero_prox_weight > 0.8, (
        f"With n_proximity_weight=0.0 and strong revenue, score should be high; "
        f"got {score_zero_prox_weight:.3f}"
    )


# ─── M: bullish_threshold ────────────────────────────────────────────────────


def test_m_zero_bullish_threshold_always_returns_bullish() -> None:
    """bullish_threshold=0.0 means any non-negative score is bullish.

    Use a downtrend to get a very low M score, then verify:
      - default threshold (0.6): is_bullish=False
      - threshold=0.0:           is_bullish=True  (score >= 0.0 is always True)

    If the `or` gotcha is present, both calls use 0.6 → both False → FAIL.
    """
    bear_df = _flat_market_df(n=300, trend="bear")

    m_default = evaluate_m(price_data=bear_df)
    m_zero = evaluate_m(price_data=bear_df, bullish_threshold=0.0)

    assert not m_default.is_bullish, "Downtrend should be bearish at default threshold"
    assert m_zero.is_bullish, (
        f"bullish_threshold=0.0 should always be bullish (score={m_zero.score:.3f}); "
        f"or-gotcha still present?"
    )


def test_m_higher_bullish_threshold_reduces_bullish_classification() -> None:
    """Higher threshold is harder to meet — a moderate uptrend should pass low but fail high."""
    bull_df = _flat_market_df(n=300, trend="bull")

    m_low_thresh = evaluate_m(price_data=bull_df, bullish_threshold=0.20)
    m_high_thresh = evaluate_m(price_data=bull_df, bullish_threshold=0.95)

    assert m_low_thresh.is_bullish, (
        "Uptrend should be bullish at threshold=0.20"
    )
    assert not m_high_thresh.is_bullish, (
        "Even uptrend unlikely to reach threshold=0.95 (very few distribution-day-free days)"
    )
