"""Paper-trading verification script.

Runs a quick end-to-end test against the live Alpaca paper account:

  1. Connect and print account equity.
  2. Start the fill monitor (WebSocket).
  3. Submit a one-share SPY entry using the configured stop distance.
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
    get_open_orders,
    get_open_positions,
    submit_bracket_buy,
)
from fill_monitor import FillMonitor

_ET = ZoneInfo("America/New_York")
_FILL_TIMEOUT = 60  # seconds to wait for fill
_TEST_SYMBOL = "SPY"

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


def main() -> int:
    print(_SEPARATOR)
    print(f"CANSLIM Paper-Trading Verification  [{datetime.now(_ET).strftime('%Y-%m-%d %H:%M ET')}]")
    print(_SEPARATOR)

    # ── Guard ─────────────────────────────────────────────────────────────────
    if not _is_paper_mode():
        print("[ERROR] ALPACA_PAPER must be 'true' for this verification script.")
        print("        Set ALPACA_PAPER=true in your .env file and retry.")
        return 1

    if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
        print("[ERROR] ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        return 1

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
        return 1

    if not _preflight_symbol_clear(_TEST_SYMBOL):
        return 1

    # ── Check market hours ─────────────────────────────────────────────────────
    print("\n[2/5] Checking market status…")
    if not _check_market_open():
        print("      Market is currently CLOSED.")
        print("      No verification order will be submitted outside market hours.")
        print("      Re-run during 09:30–16:00 ET Mon–Fri for a live fill test.")
        return 1
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
                    "side": _normalize_side(order.side),
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
    print(f"\n[4/5] Submitting test entry: 1 share {_TEST_SYMBOL} @ limit…")
    # Use the most recent regular-session minute close when available so the
    # verification order matches current tape conditions as closely as possible.
    from core.data_client import fetch_latest_intraday_price, fetch_ohlcv

    try:
        limit_price = fetch_latest_intraday_price(_TEST_SYMBOL)
        if limit_price is None:
            bars = fetch_ohlcv(_TEST_SYMBOL, period="5d")
            if bars is None or bars.empty:
                print(f"      ✗ Could not fetch {_TEST_SYMBOL} price — aborting")
                monitor.stop()
                return 1
            limit_price = float(bars["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        print(f"      ✗ Could not fetch {_TEST_SYMBOL} price: {exc}")
        monitor.stop()
        return 1

    stop_price = _calculate_stop_price(limit_price, settings.STOP_LOSS_PCT)
    print(
        f"      Limit price: ${limit_price:.2f}  |  "
        f"Stop: ${stop_price:.2f} ({settings.STOP_LOSS_PCT:.1%})"
    )

    result = submit_bracket_buy(
        symbol=_TEST_SYMBOL,
        qty=1.0,
        stop_loss_pct=settings.STOP_LOSS_PCT,
        limit_price=limit_price,
    )

    if not result.success:
        print(f"      ✗ Order submission failed: {result.error}")
        monitor.stop()
        return 1

    print(f"      ✓ Order submitted  order_id={result.order_id}")

    # ── Step 4: Wait for fill ────────────────────────────────────────────────
    print(f"\n[5/5] Waiting up to {_FILL_TIMEOUT}s for fill event…")
    deadline = time.time() + _FILL_TIMEOUT
    spy_fill = None
    cleanup_ok = False

    try:
        while time.time() < deadline:
            spy_fills = [
                f
                for f in fills_received
                if f["symbol"] == _TEST_SYMBOL and f["side"] == "buy"
            ]
            if spy_fills:
                spy_fill = spy_fills[0]
                break
            time.sleep(1)
    finally:
        # Preflight proved the symbol was clear before this order was accepted.
        try:
            cleanup_ok = _cleanup_test_symbol(_TEST_SYMBOL)
        finally:
            monitor.stop()

    # ── Results ──────────────────────────────────────────────────────────────
    print()
    print(_SEPARATOR)
    if spy_fill:
        print(f"✓ FILL RECEIVED: bought {spy_fill['qty']} SPY @ ${spy_fill['price']:.2f}")
        print("✓ Fill monitor working correctly")
    else:
        print("! No fill event received within timeout.")
        print("  (The order may still be pending — check your Alpaca paper dashboard)")

    print(_SEPARATOR)
    print("Verification complete. Check your Alpaca paper dashboard to confirm.")
    return 0 if spy_fill and cleanup_ok else 1


def _calculate_stop_price(reference_price: float, stop_loss_pct: float) -> float:
    """Return the displayed protective-stop price for a reference entry."""
    return round(reference_price * (1 - stop_loss_pct), 2)


def _normalize_side(value: object) -> str:
    """Normalize Alpaca enum-like order sides for fill matching."""
    return str(value).split(".")[-1].strip().lower()


def _preflight_symbol_clear(symbol: str) -> bool:
    """Refuse verification when the symbol has pre-existing broker state."""
    try:
        positions = get_open_positions(raise_on_error=True)
        orders = get_open_orders(symbol, raise_on_error=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Could not verify existing {symbol} broker state: {exc}")
        return False

    has_position = any(position.symbol == symbol for position in positions)
    if has_position or orders:
        print(
            f"[ERROR] Refusing verification: {symbol} already has "
            f"an open position or {len(orders)} open order(s)."
        )
        print("        Clear or choose how to preserve that state before retrying.")
        return False
    return True


def _cleanup_test_symbol(symbol: str) -> bool:
    """Cancel and close state created after a successful clear-state preflight."""
    try:
        cancelled = cancel_open_orders(symbol)
        if cancelled:
            print(f"\n[Cleanup] Cancelled {cancelled} pending {symbol} order(s)")
        positions = get_open_positions(raise_on_error=True)
        position = next((p for p in positions if p.symbol == symbol), None)
        if position:
            print(f"[Cleanup] Submitting close for {symbol} position ({position.qty} shares)…")
            result = close_position(symbol)
            if not result.success:
                print(f"[Cleanup] Warning: close submission failed: {result.error}")
                return False
            print("[Cleanup] ✓ Close order submitted")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[Cleanup] Warning: {exc}")
        return False


if __name__ == "__main__":
    raise SystemExit(main())
