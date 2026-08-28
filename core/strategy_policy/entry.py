"""Baseline pure entry and ranking policy."""

from __future__ import annotations

from .contracts import EntryDecision, EntrySnapshot


def _threshold_code(value: float | None, floor: float, unavailable: str, below: str) -> str | None:
    if value is None:
        return unavailable
    if value < floor:
        return below
    return None


def evaluate_entry(snapshot: EntrySnapshot) -> EntryDecision:
    """Apply the fixed CANSLIM entry gates to trusted completed-session facts."""
    blocking_codes = list(snapshot.technical_blocking_reasons)
    if not snapshot.technical_only:
        for code in (
            _threshold_code(snapshot.current_growth, 0.25, "current_growth_unavailable", "current_growth_below_threshold"),
            _threshold_code(snapshot.annual_growth, 0.25, "annual_growth_unavailable", "annual_growth_below_threshold"),
            _threshold_code(snapshot.rs_score, 80.0, "rs_score_unavailable", "rs_score_below_threshold"),
            _threshold_code(snapshot.entry_composite_score, 70.0, "composite_score_unavailable", "composite_score_below_threshold"),
        ):
            if code is not None:
                blocking_codes.append(code)
    market_permitted = (
        (not snapshot.require_bullish_market or snapshot.market_is_bullish or snapshot.cash_deployment_override)
        and (not snapshot.use_stateful_regime_gate or snapshot.regime_allows_entries)
    )
    return EntryDecision(
        qualified=not blocking_codes,
        market_permitted=market_permitted,
        rank=(snapshot.canslim_score, snapshot.rs_score),
        blocking_codes=tuple(blocking_codes),
    )
