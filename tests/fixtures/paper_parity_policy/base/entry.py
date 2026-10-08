from core.strategy_policy.contracts import EntryDecision


def evaluate_entry(snapshot):
    eligible = snapshot.base.market.session == "2025-05-16" and snapshot.base.technical_eligible
    return EntryDecision(eligible, True, (snapshot.base.rs_score, 0.0), () if eligible else ("synthetic_session_or_setup",))
