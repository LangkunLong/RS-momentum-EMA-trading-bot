"""Baseline pure capacity, allocation, and eviction policy."""

from __future__ import annotations

from .contracts import (
    AllocationDecision,
    AllocationSnapshot,
    CapacityDecision,
    CapacitySnapshot,
    EvictionDecision,
    EvictionSnapshot,
)


def recommend_capacity(snapshot: CapacitySnapshot) -> CapacityDecision:
    """Preserve baseline capacity while interface v2 supplies market context."""
    return CapacityDecision(
        max_positions=snapshot.configured_max_positions,
        eviction_enabled=snapshot.configured_eviction_enabled,
    )


def recommend_allocation(snapshot: AllocationSnapshot) -> AllocationDecision:
    """Preserve baseline risk and stop fractions independent of market context."""
    return AllocationDecision(
        risk_fraction=snapshot.configured_position_risk_pct,
        stop_distance_fraction=snapshot.configured_stop_loss_pct,
        notional_fraction_cap=None,
    )


def select_eviction(snapshot: EvictionSnapshot) -> EvictionDecision:
    """Preserve baseline relative-strength eviction selection."""
    eligible = tuple(
        position
        for position in snapshot.positions
        if position.causal_execution_price is not None
        and position.rs_score < snapshot.candidate_rs_score
    )
    underwater = tuple(
        position
        for position in eligible
        if position.causal_execution_price < position.entry_price
    )
    pool = underwater or eligible
    if not snapshot.capacity_is_finite or not snapshot.capacity_is_full:
        return EvictionDecision(slot=None)
    if not snapshot.eviction_enabled or not pool:
        return EvictionDecision(slot=None)
    selected = min(pool, key=lambda position: (position.rs_score, position.slot))
    return EvictionDecision(slot=selected.slot)
