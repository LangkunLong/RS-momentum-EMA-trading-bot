"""Automated CANSLIM trader.

Runs the CANSLIM scanner and submits buy entry orders for every actionable
buy signal, respecting O'Neil's position-sizing and stop-loss rules.  Also
monitors existing positions and exits on stop-loss breach or MA violation.

Usage:
    python auto_trader.py

Safety features:
- Paper mode is ON by default.  Set ALPACA_PAPER=false in .env for live orders.
- Position limit: will not open more than MAX_OPEN_POSITIONS at once.
- Per-position size: at most POSITION_SIZE_PCT of account equity per stock.
- Hard stop: 7% below the actual fill price (STOP_LOSS_PCT).
- All exits are market-day orders; entries are day orders and the fill monitor
  reconciles protective stops after the fill is confirmed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


from config import settings
from core.data_client import fetch_hourly_ohlcv, fetch_latest_intraday_price, fetch_ohlcv
from core.execution_workflow import EntryExecutionPlan
from core.order_manager import OrderManager
from core.order_execution import (
    _get_trading_client,
    _is_paper_mode,
    check_exit_signals,
    get_open_orders,
    get_open_positions,
)
from enhanced_scanner import scan_for_canslim_stocks


# ---------------------------------------------------------------------------
# Market-hours guard
# ---------------------------------------------------------------------------


def _is_market_open() -> bool:
    """Return True when the US equity market is currently open.

    Uses Alpaca's /clock endpoint so we don't hard-code timezone rules.
    Falls back to False on any error (safe default).
    """
    try:
        client = _get_trading_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not check market clock: {exc}")
        return False


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------


def _get_account_equity() -> float:
    """Return current account equity in USD.  Returns 0.0 on error."""
    try:
        client = _get_trading_client()
        account = client.get_account()
        return float(account.equity)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not fetch account equity: {exc}")
        return 0.0


def _latest_close(symbol: str, market_open: Optional[bool] = None) -> Optional[float]:
    """Return the best available entry reference price for ``symbol``."""
    price, _ = _resolve_entry_reference_price(symbol, market_open=market_open)
    return price


def _resolve_entry_reference_price(
    symbol: str,
    market_open: Optional[bool] = None,
) -> tuple[Optional[float], str]:
    """Return the best available entry reference price and its data source."""
    try:
        if market_open is None:
            market_open = _is_market_open()

        if market_open:
            intraday_price = fetch_latest_intraday_price(symbol)
            if intraday_price is not None and intraday_price > 0:
                return intraday_price, "intraday_minute_close"

        bars = fetch_ohlcv(symbol, period="5d")
        if bars is None or bars.empty:
            return None, "unavailable"
        return float(bars["Close"].iloc[-1]), "daily_close"
    except Exception:  # noqa: BLE001
        return None, "unavailable"


def _build_entry_execution_plan(
    opportunity: dict,
    equity: float,
    market_open: Optional[bool] = None,
    position_size_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
) -> Optional[EntryExecutionPlan]:
    """Build the exact trade plan before the order is submitted."""
    if position_size_pct is None:
        position_size_pct = settings.POSITION_SIZE_PCT
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    symbol = str(opportunity["symbol"])
    entry_price, price_source = _resolve_entry_reference_price(symbol, market_open=market_open)
    if entry_price is None or entry_price <= 0:
        return None

    # Buy-zone enforcement: reject entries that are too extended beyond the pivot.
    buy_point = opportunity.get("buy_point")
    if not _is_within_buy_zone(entry_price, buy_point):
        if buy_point is None or float(buy_point) <= 0:
            print(f"[ENTRIES] {symbol}: missing valid pivot/buy point metadata — skipping.")
            return None
        buy_zone_max = float(buy_point) * (1 + settings.BUY_ZONE_EXTENSION_PCT)
        buy_zone_min = float(buy_point) * (1 - settings.BUY_ZONE_UNDERCUT_TOLERANCE_PCT)
        print(
            f"[ENTRIES] {symbol}: price ${entry_price:.2f} is outside the breakout buy zone "
            f"${buy_zone_min:.2f}-${buy_zone_max:.2f} around pivot ${float(buy_point):.2f} — skipping."
        )
        return None

    position_value = equity * position_size_pct
    qty = round(position_value / entry_price, 4)
    if qty < 0.001:
        return None

    stop_price = round(entry_price * (1 - stop_loss_pct), 2)
    risk_per_share = round(entry_price - stop_price, 2)
    risk_amount = round(qty * risk_per_share, 2)

    return EntryExecutionPlan(
        symbol=symbol,
        entry_price=entry_price,
        price_source=price_source,
        stop_price=stop_price,
        stop_loss_pct=stop_loss_pct,
        position_value=position_value,
        risk_amount=risk_amount,
        risk_per_share=risk_per_share,
        qty=qty,
        canslim_score=float(opportunity.get("total_score", 0.0)),
        rs_score=float(opportunity.get("rs_score", 0.0)),
        is_breakout=bool(opportunity.get("is_breakout", False)),
        has_volume_surge=bool(opportunity.get("has_volume_surge", False)),
    )


def _is_within_buy_zone(
    current_price: float,
    buy_point: Optional[float],
    max_extension_pct: Optional[float] = None,
    undercut_tolerance_pct: Optional[float] = None,
) -> bool:
    """Return True if current_price is within the acceptable buy zone above buy_point.

    O'Neil's rule: only enter within 5% above the pivot/buy point.  Stocks that
    have already run more than max_extension_pct beyond their pivot are "too
    extended" — the risk/reward deteriorates because the natural stop (at the
    base support) is now far below the entry price.

    A real breakout entry must also be at or above the pivot. Buying below the
    pivot means the breakout has not actually triggered or has already failed.
    """
    if buy_point is None or buy_point <= 0:
        return False
    if max_extension_pct is None:
        max_extension_pct = settings.BUY_ZONE_EXTENSION_PCT
    if undercut_tolerance_pct is None:
        undercut_tolerance_pct = settings.BUY_ZONE_UNDERCUT_TOLERANCE_PCT
    return buy_point * (1 - undercut_tolerance_pct) <= current_price <= buy_point * (1 + max_extension_pct)


def _is_buy_order(order: object) -> bool:
    """Return True when an Alpaca order-like object is a buy order."""
    side = str(getattr(order, "side", "")).split(".")[-1].strip().lower()
    return side == "buy"


def _entry_priority_key(opportunity: dict) -> tuple[float, float, float, float, float, float]:
    """Return a descending priority key for executable entry candidates."""
    metrics = opportunity.get("metrics", {})
    current_growth = float(metrics.get("current_growth") or -1.0)
    annual_growth = float(metrics.get("annual_growth") or -1.0)
    proximity_to_high = float(metrics.get("proximity_to_high") or -1.0)
    return (
        float(opportunity.get("total_score", 0.0)),
        float(opportunity.get("rs_score", 0.0)),
        1.0 if opportunity.get("has_volume_surge") else 0.0,
        current_growth,
        annual_growth,
        proximity_to_high,
    )


def _rank_entry_candidates(actionable_buys: list[dict]) -> list[dict]:
    """Sort actionable buys so the strongest executable setups are attempted first."""
    return sorted(actionable_buys, key=_entry_priority_key, reverse=True)


# ---------------------------------------------------------------------------
# Exit monitoring
# ---------------------------------------------------------------------------


def monitor_and_exit_positions(
    stop_loss_pct: Optional[float] = None,
    ema_exit_period: int = 21,
) -> list[str]:
    """Check all open positions for exit conditions and submit sell orders.

    Exit triggers (O'Neil rules):
    1. **Hard stop**: position is down >= ``stop_loss_pct`` (default 7%).
    2. **Moving-average violation**: daily close has fallen below the
       ``ema_exit_period``-day EMA on two consecutive closes.

    This function is safe to call even when the market is closed — exit orders
    with TimeInForce.DAY will queue for the next open.

    Args:
        stop_loss_pct: Override stop-loss percentage (default: settings value).
        ema_exit_period: EMA period for MA-violation check (default: 21-day).

    Returns:
        List of ticker symbols for which an exit order was submitted.
    """
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    positions = get_open_positions()
    if not positions:
        print("No open positions to monitor.")
        return []

    exited: list[str] = []
    order_manager = OrderManager(paper=_is_paper_mode())

    # 1. Hard stop check
    stop_breaches = check_exit_signals(positions, stop_loss_pct)
    for pos in stop_breaches:
        pct = pos.unrealized_pl_pct * 100
        print(
            f"[EXIT] Hard stop triggered for {pos.symbol}: "
            f"{pct:.1f}% loss (threshold {-stop_loss_pct * 100:.0f}%)"
        )
        result = order_manager.submit_exit(pos.symbol, exit_reason="hard stop triggered")
        if result.success:
            exited.append(pos.symbol)

    # 2. MA-violation check (only for positions not already exited)
    remaining = [p for p in positions if p.symbol not in exited]
    for pos in remaining:
        try:
            bars = fetch_ohlcv(pos.symbol, period="3mo")
            if bars is None or len(bars) < ema_exit_period + 2:
                continue
            ema = bars["Close"].ewm(span=ema_exit_period, adjust=False).mean()
            # Two consecutive closes below EMA = violation
            last_two_closes = bars["Close"].iloc[-2:]
            last_two_ema = ema.iloc[-2:]
            if (last_two_closes.values < last_two_ema.values).all():
                print(
                    f"[EXIT] MA violation for {pos.symbol}: "
                    f"2 consecutive closes below {ema_exit_period}-day EMA"
                )
                result = order_manager.submit_exit(
                    pos.symbol,
                    exit_reason=f"{ema_exit_period}-day EMA violation",
                )
                if result.success:
                    exited.append(pos.symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] MA check failed for {pos.symbol}: {exc}")

    return exited


def monitor_exits_hourly(
    ema_period: int = 21,
    consecutive: int = 2,
    history_days: int = 10,
) -> list[str]:
    """Check open positions for exit signals using hourly OHLCV bars.

    Runs the same two exit rules as ``monitor_and_exit_positions`` but on
    1-hour bars rather than daily bars.  This allows faster detection of
    deteriorating positions without waiting for the daily close.

    Exit triggers:
    1. **Hard stop**: Alpaca real-time P&L is already below ``STOP_LOSS_PCT``
       (delegates to ``check_exit_signals``).
    2. **Hourly MA violation**: ``consecutive`` hourly closes are below the
       ``ema_period``-period hourly EMA.

    Call this function once per hour after each hourly bar closes (e.g. at
    10:01, 11:01, … 16:01 ET).  The scheduler handles the timing.

    Args:
        ema_period: EMA look-back in hourly bars (default 21 ≈ ~2.6 trading days).
        consecutive: Number of consecutive hourly closes below EMA to trigger.
        history_days: Calendar days of 1H history to fetch (default 10 trading days).

    Returns:
        List of symbols for which an exit order was submitted.
    """
    positions = get_open_positions()
    if not positions:
        return []

    exited: list[str] = []
    order_manager = OrderManager(paper=_is_paper_mode())

    # 1. Hard stop — uses Alpaca's real-time unrealised P&L (same as daily check)
    stop_breaches = check_exit_signals(positions, settings.STOP_LOSS_PCT)
    for pos in stop_breaches:
        pct = pos.unrealized_pl_pct * 100
        print(
            f"[HOURLY EXIT] Hard stop for {pos.symbol}: {pct:.1f}% loss"
        )
        result = order_manager.submit_exit(pos.symbol, exit_reason="hourly hard stop triggered")
        if result.success:
            exited.append(pos.symbol)

    # 2. Hourly MA violation
    remaining = [p for p in positions if p.symbol not in exited]
    for pos in remaining:
        try:
            bars = fetch_hourly_ohlcv(pos.symbol, days=history_days)
            if bars is None or len(bars) < ema_period + consecutive:
                continue
            ema = bars["Close"].ewm(span=ema_period, adjust=False).mean()
            last_closes = bars["Close"].iloc[-consecutive:]
            last_ema = ema.iloc[-consecutive:]
            if (last_closes.values < last_ema.values).all():
                print(
                    f"[HOURLY EXIT] MA violation for {pos.symbol}: "
                    f"{consecutive} consecutive hourly closes below {ema_period}-period hourly EMA"
                )
                result = order_manager.submit_exit(
                    pos.symbol,
                    exit_reason=f"{consecutive} hourly closes below {ema_period}-period EMA",
                )
                if result.success:
                    exited.append(pos.symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Hourly MA check failed for {pos.symbol}: {exc}")

    return exited


# ---------------------------------------------------------------------------
# Entry execution
# ---------------------------------------------------------------------------


def execute_entries(
    actionable_buys: list[dict],
    dry_run: bool = False,
) -> list[str]:
    """Submit buy entry orders for CANSLIM actionable-buy signals.

    Respects:
    - MAX_OPEN_POSITIONS: skips entries if the position book is full.
    - POSITION_SIZE_PCT: sizes each position as a fraction of equity.
    - Deduplication: skips tickers already held or with open orders.

    Args:
        actionable_buys: List of CANSLIM result dicts from ``scan_for_canslim_stocks``.
        dry_run: If True, print what *would* be ordered without submitting.

    Returns:
        List of symbols for which an order was submitted (or would be in dry_run).
    """
    equity = _get_account_equity()
    if equity <= 0:
        print("[ERROR] Cannot size positions: account equity unavailable.")
        return []

    market_open = _is_market_open()

    positions = get_open_positions()
    held_symbols = {p.symbol for p in positions}
    open_orders = get_open_orders()
    open_order_symbols = {o.symbol for o in open_orders}
    pending_entry_symbols = {o.symbol for o in open_orders if _is_buy_order(o)}
    already_active = held_symbols | open_order_symbols

    active_slot_count = len(held_symbols | pending_entry_symbols)
    available_slots = settings.MAX_OPEN_POSITIONS - active_slot_count
    if available_slots <= 0:
        print(
            f"[ENTRIES] Position limit reached ({settings.MAX_OPEN_POSITIONS}). "
            "No new entries."
        )
        return []

    cycle_entry_cap = max(1, int(getattr(settings, "MAX_NEW_ENTRIES_PER_CYCLE", available_slots)))
    execution_slots = min(available_slots, cycle_entry_cap)
    if len(actionable_buys) > execution_slots:
        print(
            f"[ENTRIES] {len(actionable_buys)} actionable buys found; "
            f"executing only top {execution_slots} ranked setup(s) this cycle."
        )

    ordered: list[str] = []
    order_manager = OrderManager(paper=_is_paper_mode())
    ranked_opportunities = _rank_entry_candidates(actionable_buys)
    for opp in ranked_opportunities:
        if len(ordered) >= execution_slots:
            break

        symbol = opp["symbol"]
        if symbol in already_active:
            print(f"[ENTRIES] {symbol}: already held or has open order — skipping.")
            continue

        plan = _build_entry_execution_plan(
            opportunity=opp,
            equity=equity,
            market_open=market_open,
            position_size_pct=settings.POSITION_SIZE_PCT,
            stop_loss_pct=settings.STOP_LOSS_PCT,
        )
        if plan is None:
            print(f"[ENTRIES] {symbol}: could not fetch price — skipping.")
            continue

        print(
            f"[ENTRIES] {symbol}: CANSLIM={plan.canslim_score:.1f} RS={plan.rs_score:.1f} "
            f"breakout={plan.is_breakout} surge={plan.has_volume_surge} | "
            f"qty={plan.qty} @ ~${plan.entry_price:.2f} (${plan.position_value:.0f}) "
            f"stop=${plan.stop_price:.2f} risk~${plan.risk_amount:.2f} "
            f"source={plan.price_source}"
        )

        submission = order_manager.submit_entry(
            plan,
            signal_payload=opp,
            dry_run=dry_run,
        )
        if submission.success:
            if dry_run:
                print(f"  [DRY RUN] Would submit entry buy for {symbol}")
            ordered.append(symbol)
            already_active.add(symbol)
        else:
            print(f"[ENTRIES] {symbol}: order submission failed — {submission.error or 'unknown error'}")

    return ordered


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_auto_trader(
    dry_run: bool = False,
    skip_entries: bool = False,
    skip_exits: bool = False,
) -> None:
    """Full CANSLIM scan → exit monitoring → entry execution cycle.

    Args:
        dry_run: If True, print all intended actions without submitting orders.
        skip_entries: Skip the entry phase (monitor-only mode).
        skip_exits: Skip the exit check (entry-only mode, use with caution).
    """
    mode_label = "DRY RUN" if dry_run else ("paper" if _is_paper_mode() else "LIVE")
    print("=" * 60)
    print(f"CANSLIM AUTO TRADER  [{mode_label}]  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    if settings.ENTRY_MARKET_HOURS_ONLY and not dry_run:
        if not _is_market_open():
            print("[WARN] Market is closed. Set ENTRY_MARKET_HOURS_ONLY=False to queue orders anyway.")
            return

    # --- Phase 1: Exit monitoring ---
    if not skip_exits:
        print("\n--- Phase 1: Exit monitoring ---")
        exited = monitor_and_exit_positions()
        if exited:
            print(f"Exited {len(exited)} position(s): {', '.join(exited)}")
        else:
            print("No exit conditions triggered.")
    else:
        print("\n--- Phase 1: Exit monitoring (skipped) ---")

    # --- Phase 2: Scanner ---
    print("\n--- Phase 2: CANSLIM scan ---")
    actionable_buys, watchlist_candidates, market_trend = scan_for_canslim_stocks()
    print(f"Actionable buys from scanner: {len(actionable_buys)}")
    print(f"Watchlist candidates: {len(watchlist_candidates)}")

    # --- Phase 3: Entries ---
    if not skip_entries:
        print("\n--- Phase 3: Entry orders ---")
        if actionable_buys:
            entered = execute_entries(actionable_buys, dry_run=dry_run)
            if entered:
                print(f"Submitted entries for: {', '.join(entered)}")
            else:
                print("No new entries submitted.")
        else:
            print("No actionable buys — no entries.")
    else:
        print("\n--- Phase 3: Entry orders (skipped) ---")

    print("\nAuto-trader cycle complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CANSLIM Auto Trader")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print intended orders without submitting (default: True for safety)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Actually submit orders (overrides --dry-run). Requires ALPACA_PAPER=false for live.",
    )
    parser.add_argument("--skip-entries", action="store_true", help="Skip entry phase")
    parser.add_argument("--skip-exits", action="store_true", help="Skip exit phase")
    args = parser.parse_args()

    dry = not args.live  # --live disables dry_run
    run_auto_trader(dry_run=dry, skip_entries=args.skip_entries, skip_exits=args.skip_exits)
