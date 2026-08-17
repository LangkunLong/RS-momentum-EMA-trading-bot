"""Paper-trading verification script.

Runs a quick end-to-end test against the live Alpaca paper account:

  1. Connect and print account equity.
  2. Start the fill monitor (WebSocket).
  3. Submit a small bracket buy for SPY (1 share, 7% stop).
  4. Wait up to 60 seconds for the fill event.
  5. Close the position immediately after fill.
  6. Print a pass/fail summary.

Usage:
    python verify_paper_trading.py

Requires ALPACA_API_KEY, ALPACA_SECRET_KEY, and ALPACA_PAPER=true in .env.
Optional: set NOTIFY_EMAIL_* in .env to verify email notifications too.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from core.notifier import _is_configured as notify_configured
from core.order_execution import (
    _get_trading_client,
    _is_paper_mode,
    cancel_open_orders,
    close_position,
    get_open_positions,
    submit_bracket_buy,
)
from fill_monitor import FillMonitor

_ET = ZoneInfo("America/New_York")
_FILL_TIMEOUT = 60  # seconds to wait for fill

_SEPARATOR = "=" * 60


def _check_market_open() -> bool:
    """Return True when market is currently open."""
    try:
        client = _get_trading_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not check clock: {exc}")
        return False


def main() -> None:
    print(_SEPARATOR)
    print(f"CANSLIM Paper-Trading Verification  [{datetime.now(_ET).strftime('%Y-%m-%d %H:%M ET')}]")
    print(_SEPARATOR)

    # ── Guard ─────────────────────────────────────────────────────────────────
    if not _is_paper_mode():
        print("[ERROR] ALPACA_PAPER must be 'true' for this verification script.")
        print("        Set ALPACA_PAPER=true in your .env file and retry.")
        return

    if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
        print("[ERROR] ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        return

    # ── Step 1: Account equity ─────────────────────────────────────────────────
    print("\n[1/5] Connecting to Alpaca paper account…")
    try:
        client = _get_trading_client()
        account = client.get_account()
        equity = float(account.equity)
        buying_power = float(account.buying_power)
        print(f"      Equity:        ${equity:,.2f}")
        print(f"      Buying power:  ${buying_power:,.2f}")
        print("      ✓ Connected")
    except Exception as exc:  # noqa: BLE001
        print(f"      ✗ Connection failed: {exc}")
        return

    # ── Check market hours ─────────────────────────────────────────────────────
    print("\n[2/5] Checking market status…")
    if not _check_market_open():
        print("      Market is currently CLOSED.")
        print("      Bracket limit orders will be submitted but won't fill until open.")
        print("      Re-run during 09:30–16:00 ET Mon–Fri for a live fill test.")
        _cleanup_and_exit()
        return
    print("      ✓ Market is OPEN")

    # ── Step 2: Start fill monitor ────────────────────────────────────────────
    print("\n[3/5] Starting fill monitor (WebSocket)…")
    fills_received: list[dict] = []

    # Monkey-patch _dispatch to capture fills without sending real emails
    monitor = FillMonitor()

    original_dispatch = monitor._dispatch

    def _capture_dispatch(data):
        if getattr(data, "event", "") == "fill":
            order = getattr(data, "order", None)
            if order:
                fills_received.append({
                    "symbol": str(order.symbol),
                    "side": str(order.side).lower(),
                    "qty": float(order.filled_qty or 0),
                    "price": float(order.filled_avg_price or 0),
                })
        original_dispatch(data)  # still sends email if configured

    monitor._dispatch = _capture_dispatch
    monitor.start()
    print("      ✓ Fill monitor running")

    if notify_configured():
        print("      ✓ Email notifications configured — you will receive fill emails")
    else:
        print("      ! Email not configured (NOTIFY_EMAIL_* not set) — no email alerts")

    # ── Step 3: Submit test bracket buy ──────────────────────────────────────
    print("\n[4/5] Submitting test bracket buy: 1 share SPY @ market limit…")
    # Use the most recent regular-session minute close when available so the
    # verification order matches current tape conditions as closely as possible.
    from core.data_client import fetch_latest_intraday_price, fetch_ohlcv

    limit_price = fetch_latest_intraday_price("SPY")
    if limit_price is None:
        bars = fetch_ohlcv("SPY", period="5d")
        if bars is None or bars.empty:
            print("      ✗ Could not fetch SPY price — aborting")
            monitor.stop()
            return
        limit_price = float(bars["Close"].iloc[-1])

    print(f"      Limit price: ${limit_price:.2f}  |  Stop: ${limit_price * 0.93:.2f}")

    result = submit_bracket_buy(
        symbol="SPY",
        qty=1.0,
        stop_loss_pct=settings.STOP_LOSS_PCT,
        limit_price=limit_price,
    )

    if not result.success:
        print(f"      ✗ Order submission failed: {result.error}")
        monitor.stop()
        return

    print(f"      ✓ Order submitted  order_id={result.order_id}")

    # ── Step 4: Wait for fill ────────────────────────────────────────────────
    print(f"\n[5/5] Waiting up to {_FILL_TIMEOUT}s for fill event…")
    deadline = time.time() + _FILL_TIMEOUT
    spy_fill = None

    while time.time() < deadline:
        spy_fills = [f for f in fills_received if f["symbol"] == "SPY" and f["side"] == "buy"]
        if spy_fills:
            spy_fill = spy_fills[0]
            break
        time.sleep(1)

    # ── Results ──────────────────────────────────────────────────────────────
    print()
    print(_SEPARATOR)
    if spy_fill:
        print(f"✓ FILL RECEIVED: bought {spy_fill['qty']} SPY @ ${spy_fill['price']:.2f}")
        print("✓ Fill monitor working correctly")
    else:
        print("! No fill event received within timeout.")
        print("  (The order may still be pending — check your Alpaca paper dashboard)")

    # Always clean up — cancel the bracket and close any open SPY position
    _cleanup_and_exit()
    monitor.stop()
    print(_SEPARATOR)
    print("Verification complete. Check your Alpaca paper dashboard to confirm.")


def _cleanup_and_exit() -> None:
    """Cancel pending SPY orders and close any SPY position."""
    try:
        cancelled = cancel_open_orders("SPY")
        if cancelled:
            print(f"\n[Cleanup] Cancelled {cancelled} pending SPY order(s)")
        positions = get_open_positions()
        spy_pos = next((p for p in positions if p.symbol == "SPY"), None)
        if spy_pos:
            print(f"[Cleanup] Closing SPY position ({spy_pos.qty} shares)…")
            close_position("SPY")
            print("[Cleanup] ✓ Position closed")
    except Exception as exc:  # noqa: BLE001
        print(f"[Cleanup] Warning: {exc}")


if __name__ == "__main__":
    main()
