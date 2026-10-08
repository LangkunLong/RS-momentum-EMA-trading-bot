from core.strategy_policy.contracts import AllocationDecision, CapacityDecision, EvictionDecision


def recommend_capacity(snapshot):
    return CapacityDecision(1, False)


def recommend_allocation(snapshot):
    return AllocationDecision(0.01, 0.05, 0.10)


def select_eviction(snapshot):
    return EvictionDecision(None)
