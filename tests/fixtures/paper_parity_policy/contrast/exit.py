from core.strategy_policy.contracts import ExitDecision


def evaluate_exit(snapshot):
    return ExitDecision((), None, False, snapshot.base.scale_out_tier, snapshot.base.breakeven_armed,
                        snapshot.base.ema_trailing_active)
