from core.strategy_policy.contracts_v3 import AddOnDecisionV3


def evaluate_add_on(snapshot):
    return AddOnDecisionV3(False, 0.0, None, "synthetic_no_add")
