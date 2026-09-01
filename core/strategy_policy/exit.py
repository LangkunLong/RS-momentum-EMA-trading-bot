"""Baseline pure exit policy."""

from __future__ import annotations

from .contracts import ExitAction, ExitDecision, ExitSnapshot


def evaluate_exit(snapshot: ExitSnapshot) -> ExitDecision:
    """Return the baseline plan; interface-v2 market context is observational."""
    early_winner_hold = snapshot.early_winner_hold
    scale_out_tier = snapshot.scale_out_tier
    gain_fraction = (snapshot.current_close - snapshot.entry_price) / snapshot.entry_price

    if early_winner_hold and snapshot.days_held >= snapshot.early_winner_release_days:
        early_winner_hold = False
        scale_out_tier = 0
    if (
        not early_winner_hold
        and snapshot.days_held <= snapshot.early_winner_trigger_days
        and gain_fraction >= snapshot.early_winner_gain_pct
    ):
        early_winner_hold = True

    actions: list[ExitAction] = []
    planned_quantity = 0.0
    if not early_winner_hold:
        while scale_out_tier < len(snapshot.scale_out_tiers):
            trigger, fraction = snapshot.scale_out_tiers[scale_out_tier]
            if snapshot.current_high < snapshot.entry_price * (1 + trigger):
                break
            quantity = snapshot.original_qty * fraction
            if planned_quantity + quantity > snapshot.remaining_qty + 1e-12:
                break
            actions.append(ExitAction("scale_out", trigger, fraction, "take_profit_scale_out"))
            planned_quantity += quantity
            scale_out_tier += 1

    if (
        snapshot.days_held >= snapshot.stagnation_days
        and snapshot.peak_close < snapshot.entry_price * (1 + snapshot.stagnation_threshold_pct)
    ):
        actions.append(ExitAction("close", None, None, "time_stop"))
    elif snapshot.consecutive_closes_below_ema:
        actions.append(ExitAction("close", None, None, "ma_violation"))

    breakeven_armed = snapshot.breakeven_armed or (
        snapshot.current_high >= snapshot.entry_price * (1 + snapshot.breakeven_trigger_pct)
    )
    ema_trailing_active = snapshot.ema_trailing_active or (
        breakeven_armed
        and snapshot.history_session_count >= snapshot.ema_period
        and snapshot.ema_today is not None
    )
    selected_stop = max(snapshot.protective_stop_candidates)
    next_stop_price = selected_stop if selected_stop > snapshot.stop_price else None
    return ExitDecision(
        actions=tuple(actions),
        next_stop_price=next_stop_price,
        early_winner_hold=early_winner_hold,
        scale_out_tier=scale_out_tier,
        breakeven_armed=breakeven_armed,
        ema_trailing_active=ema_trailing_active,
    )
