"""Public pure strategy-policy contract."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Callable, Protocol

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

if TYPE_CHECKING:
    from .adapter_v3 import StrategyPolicyAdapterV3
    from .contracts_v3 import (
        AddOnDecisionV3,
        AddOnSnapshotV3,
        AllocationSnapshotV3,
        CapacitySnapshotV3,
        EntrySnapshotV3,
        EvictionPositionV3,
        EvictionSnapshotV3,
        ExitSnapshotV3,
        PortfolioFeaturesV3,
    )

POLICY_INTERFACE_VERSION = 2
POLICY_INTERFACE_VERSION_V3 = 3
SUPPORTED_POLICY_INTERFACE_VERSIONS = frozenset(
    {POLICY_INTERFACE_VERSION, POLICY_INTERFACE_VERSION_V3}
)


class StrategyPolicyClient(Protocol):
    interface_version: int

    def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision: ...
    def recommend_capacity(self, snapshot: CapacitySnapshot) -> CapacityDecision: ...
    def recommend_allocation(self, snapshot: AllocationSnapshot) -> AllocationDecision: ...
    def select_eviction(self, snapshot: EvictionSnapshot) -> EvictionDecision: ...
    def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision: ...
    def close(self) -> None: ...


StrategyPolicyClientFactory = Callable[[], StrategyPolicyClient]


class StrategyPolicyClientV3(Protocol):
    """Pure adaptive policy surface consumed by the V5 engine adapter."""

    interface_version: int

    def evaluate_entry(self, snapshot: EntrySnapshotV3) -> EntryDecision: ...
    def recommend_capacity(self, snapshot: CapacitySnapshotV3) -> CapacityDecision: ...
    def recommend_allocation(
        self, snapshot: AllocationSnapshotV3
    ) -> AllocationDecision: ...
    def select_eviction(self, snapshot: EvictionSnapshotV3) -> EvictionDecision: ...
    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3: ...
    def evaluate_exit(self, snapshot: ExitSnapshotV3) -> ExitDecision: ...
    def close(self) -> None: ...


StrategyPolicyClientFactoryV3 = Callable[[], StrategyPolicyClientV3]

_V3_CONTRACT_EXPORTS = frozenset(
    {
        "AddOnDecisionV3",
        "AddOnSnapshotV3",
        "AllocationSnapshotV3",
        "CapacitySnapshotV3",
        "EntrySnapshotV3",
        "EvictionPositionV3",
        "EvictionSnapshotV3",
        "ExitSnapshotV3",
        "PortfolioFeaturesV3",
        "validate_add_on_decision",
    }
)


def __getattr__(name: str) -> object:
    """Load V3-only surfaces without changing legacy V2 import behavior."""
    if name == "StrategyPolicyAdapterV3":
        module = import_module(".adapter_v3", __name__)
    elif name in _V3_CONTRACT_EXPORTS:
        module = import_module(".contracts_v3", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value

__all__ = [
    "POLICY_INTERFACE_VERSION", "POLICY_INTERFACE_VERSION_V3",
    "SUPPORTED_POLICY_INTERFACE_VERSIONS", "StrategyPolicyClient", "StrategyPolicyClientFactory",
    "StrategyPolicyClientV3", "StrategyPolicyClientFactoryV3", "StrategyPolicyAdapterV3",
    "BenchmarkContextV1", "MarketContextV1",
    "EntrySnapshot", "EntryDecision", "CapacitySnapshot", "CapacityDecision",
    "AllocationSnapshot", "AllocationDecision", "EvictionPosition", "EvictionSnapshot",
    "EvictionDecision", "ExitSnapshot", "ExitAction", "ExitDecision",
    "EntrySnapshotV3", "PortfolioFeaturesV3", "CapacitySnapshotV3",
    "AllocationSnapshotV3", "EvictionPositionV3", "EvictionSnapshotV3",
    "AddOnSnapshotV3", "AddOnDecisionV3", "ExitSnapshotV3",
    "validate_capacity_decision", "validate_allocation_decision", "validate_eviction_decision",
    "validate_exit_decision", "validate_add_on_decision",
]
