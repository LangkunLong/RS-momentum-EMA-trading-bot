"""Parity-first entry policy for policy interface V3."""

from __future__ import annotations

from core.strategy_policy.contracts import EntryDecision
from core.strategy_policy.contracts_v3 import EntrySnapshotV3
from core.strategy_policy.entry import evaluate_entry as _evaluate_entry_v2


def evaluate_entry(snapshot: EntrySnapshotV3) -> EntryDecision:
    """Apply the deterministic V2 entry baseline to the V3 base snapshot."""

    return _evaluate_entry_v2(snapshot.base)


__all__ = ["evaluate_entry"]
