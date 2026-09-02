"""Public pure strategy-policy contract."""

from __future__ import annotations

from typing import Callable, Protocol

from .contracts import (
    AllocationDecision,
    AllocationSnapshot,
    BenchmarkContextV1,
    CapacityDecision,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    EvictionPosition,
    EvictionSnapshot,
    ExitAction,
    ExitDecision,
    ExitSnapshot,
    MarketContextV1,
    validate_allocation_decision,
    validate_capacity_decision,
    validate_eviction_decision,
    validate_exit_decision,
)

POLICY_INTERFACE_VERSION = 2


class StrategyPolicyClient(Protocol):
    interface_version: int

    def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision: ...
    def recommend_capacity(self, snapshot: CapacitySnapshot) -> CapacityDecision: ...
    def recommend_allocation(self, snapshot: AllocationSnapshot) -> AllocationDecision: ...
    def select_eviction(self, snapshot: EvictionSnapshot) -> EvictionDecision: ...
    def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision: ...
    def close(self) -> None: ...


StrategyPolicyClientFactory = Callable[[], StrategyPolicyClient]

__all__ = [
    "POLICY_INTERFACE_VERSION", "StrategyPolicyClient", "StrategyPolicyClientFactory",
    "BenchmarkContextV1", "MarketContextV1",
    "EntrySnapshot", "EntryDecision", "CapacitySnapshot", "CapacityDecision",
    "AllocationSnapshot", "AllocationDecision", "EvictionPosition", "EvictionSnapshot",
    "EvictionDecision", "ExitSnapshot", "ExitAction", "ExitDecision",
    "validate_capacity_decision", "validate_allocation_decision", "validate_eviction_decision",
    "validate_exit_decision",
]
