"""Unit tests for N (New Products) component."""

import pandas as pd

from core.canslim.n_new_products import evaluate_n


def test_evaluate_n_reweights_to_price_when_revenue_is_missing():
    score, revenue_growth = evaluate_n(pd.DataFrame(), proximity_to_high=0.98)

    assert revenue_growth is None
    assert score == 1.0


def test_evaluate_n_uses_both_signals_when_revenue_exists():
    quarterly_income = pd.DataFrame(
        {
            pd.Timestamp("2024-03-31"): [100],
            pd.Timestamp("2024-06-30"): [110],
            pd.Timestamp("2024-09-30"): [115],
            pd.Timestamp("2025-03-31"): [130],
        },
        index=["Total Revenue"],
    )

    score, revenue_growth = evaluate_n(quarterly_income, proximity_to_high=0.98)

    assert revenue_growth is not None
    assert 0.0 < score <= 1.0
