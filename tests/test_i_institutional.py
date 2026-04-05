"""Unit tests for I (Institutional) component."""

from core.canslim.i_institutional import _score_ownership_level, _score_ownership_trend, evaluate_i


def test_score_ownership_level():
    # Below 10%
    assert _score_ownership_level(0.05) < 0.3
    # Sweet spot 30-60%
    assert 0.7 <= _score_ownership_level(0.40) <= 1.0
    # Over 90%
    assert _score_ownership_level(0.95) <= 0.6


def test_score_ownership_trend():
    # Increase
    assert _score_ownership_trend(110, 100) == 1.0
    # Decrease (distribution)
    assert _score_ownership_trend(90, 100) < 0.5
    # No history
    assert _score_ownership_trend(100, None) == 0.5


def test_evaluate_i():
    score = evaluate_i(0.40, 110, 100)
    assert score > 0.8  # Should be very high as it hits sweet spot and 10% increase
