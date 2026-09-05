"""Inert baseline position policy for policy interface V3."""

from __future__ import annotations

from core.strategy_policy.contracts_v3 import AddOnDecisionV3, AddOnSnapshotV3


def evaluate_add_on(snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
    """Decline every add-on at the parity-first V3 baseline."""

    return AddOnDecisionV3(
        add=False,
        risk_fraction=0.0,
        notional_fraction_cap=None,
        reason_code="baseline_no_add_on",
    )


__all__ = ("evaluate_add_on",)
