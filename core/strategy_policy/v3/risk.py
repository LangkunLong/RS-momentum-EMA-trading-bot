"""Parity-first risk policy for policy interface V3."""

from __future__ import annotations

from core.strategy_policy.contracts import (
    AllocationDecision,
    CapacityDecision,
    EvictionDecision,
)
from core.strategy_policy.contracts_v3 import (
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EvictionSnapshotV3,
)
from core.strategy_policy.risk import (
    recommend_allocation as _recommend_allocation_v2,
)
from core.strategy_policy.risk import recommend_capacity as _recommend_capacity_v2
from core.strategy_policy.risk import select_eviction as _select_eviction_v2


def recommend_capacity(snapshot: CapacitySnapshotV3) -> CapacityDecision:
    """Apply the deterministic V2 capacity baseline to the V3 base snapshot."""

    return _recommend_capacity_v2(snapshot.base)


def recommend_allocation(snapshot: AllocationSnapshotV3) -> AllocationDecision:
    """Apply the deterministic V2 allocation baseline to the V3 base snapshot."""

    return _recommend_allocation_v2(snapshot.base)


def select_eviction(snapshot: EvictionSnapshotV3) -> EvictionDecision:
    """Apply the deterministic V2 eviction baseline to the V3 base snapshot."""

    return _select_eviction_v2(snapshot.base)


__all__ = ["recommend_allocation", "recommend_capacity", "select_eviction"]
