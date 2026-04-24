"""Targeted regression tests for backtest signal logic."""

from backtest import _should_emit_buy_signal


def test_backtest_buy_signal_requires_score_40_and_rs_80() -> None:
    # Passes at the 40-point threshold with a valid breakout
    assert (
        _should_emit_buy_signal(
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
        )
        is True
    )

    # Fails below 40
    assert (
        _should_emit_buy_signal(
            total_score=39.9,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
        )
        is False
    )

    # Market gate is NOT enforced in the backtest — bearish market still allows signals
    assert (
        _should_emit_buy_signal(
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=False,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
        )
        is True
    )


def test_backtest_buy_signal_in_buy_zone_blocks_without_peg() -> None:
    # in_buy_zone=False blocks a normal breakout signal
    assert (
        _should_emit_buy_signal(
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
            in_buy_zone=False,
        )
        is False
    )

    # PEG exempts the in_buy_zone check
    assert (
        _should_emit_buy_signal(
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=False,
            has_volume_surge=False,
            has_peg_today=True,
            in_buy_zone=False,
        )
        is True
    )
