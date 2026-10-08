from core.strategy_policy.contracts import AllocationDecision, CapacityDecision, EvictionDecision


def recommend_capacity(snapshot):
    return CapacityDecision(2, False)


def recommend_allocation(snapshot):
    return AllocationDecision(0.005, 0.05, 0.05)


def select_eviction(snapshot):
    return EvictionDecision(None)
