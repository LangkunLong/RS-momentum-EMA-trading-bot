"""Targeted regression tests for backtest signal logic."""

from core.canslim.entry_contract import CanslimEntryFacts
from backtest import _should_emit_buy_signal


def _eligible_facts() -> CanslimEntryFacts:
    return CanslimEntryFacts(
        event_close=102.0,
        prior_close=99.0,
        event_volume=1_300_000.0,
        prior_average_volume_50=1_000_000.0,
        pivot=100.0,
        volume_ratio=1.3,
        extension=0.02,
        price_advanced=True,
        has_volume_surge=True,
        in_buy_zone=True,
        eligible=True,
        blocking_reasons=(),
    )


def _below_pivot_facts() -> CanslimEntryFacts:
    return CanslimEntryFacts(
        event_close=99.0,
        prior_close=98.0,
        event_volume=1_300_000.0,
        prior_average_volume_50=1_000_000.0,
        pivot=100.0,
        volume_ratio=1.3,
        extension=-0.01,
        price_advanced=True,
        has_volume_surge=True,
        in_buy_zone=False,
        eligible=False,
        blocking_reasons=("close_below_pivot",),
    )


def test_backtest_buy_signal_requires_canonical_entry_facts_and_thresholds() -> None:
    # The canonical entry contract uses the non-M composite score and C/A/RS
    # floors; legacy M-inclusive total_score and technical keywords are
    # diagnostic only.
    assert (
        _should_emit_buy_signal(
            entry_facts=_eligible_facts(),
            current_growth=0.25,
            annual_growth=0.25,
            composite_score=70.0,
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
        )
        is True
    )

    # Fails below the canonical composite threshold even when the legacy
    # M-inclusive total_score is supplied.
    assert (
        _should_emit_buy_signal(
            entry_facts=_eligible_facts(),
            current_growth=0.25,
            annual_growth=0.25,
            composite_score=69.9,
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
            entry_facts=_eligible_facts(),
            current_growth=0.25,
            annual_growth=0.25,
            composite_score=70.0,
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=False,
            has_breakout=True,
            has_volume_surge=True,
            has_peg_today=False,
        )
        is True
    )


def test_backtest_buy_signal_requires_canonical_buy_zone_and_ignores_peg_bypass() -> None:
    # A setup below the pivot remains ineligible even if the legacy PEG flag
    # claims a same-day power gap.  PEG is diagnostic, not an entry bypass.
    assert (
        _should_emit_buy_signal(
            entry_facts=_below_pivot_facts(),
            current_growth=0.25,
            annual_growth=0.25,
            composite_score=70.0,
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

    # PEG does not bypass the canonical buy-zone check.
    assert (
        _should_emit_buy_signal(
            entry_facts=_below_pivot_facts(),
            current_growth=0.25,
            annual_growth=0.25,
            composite_score=70.0,
            total_score=40.0,
            rs_score=80.0,
            market_is_bullish=True,
            has_breakout=False,
            has_volume_surge=False,
            has_peg_today=True,
            in_buy_zone=False,
        )
        is False
    )
