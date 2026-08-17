"""Real-time Alpaca order-fill monitor using the Trade Updates WebSocket stream.

Listens for fill events on the paper account and routes them through
the OrderManager so workflow transitions stay centralized and auditable.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from alpaca.trading.stream import TradingStream

from config import settings
from core.order_execution import require_paper_mode
from core.order_manager import OrderManager


class FillMonitor:
    """Wrap Alpaca's TradingStream and forward meaningful events to OrderManager."""

    def __init__(self) -> None:
        require_paper_mode()
        self._paper = True
        self._order_manager = OrderManager(paper=self._paper)
        self._stream = TradingStream(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=self._paper,
        )
        self._thread: threading.Thread | None = None
        self._running = False

        @self._stream.on("trade_updates")
        async def _on_trade_update(data: Any) -> None:  # noqa: ANN401
            self._dispatch(data)

    def start(self) -> None:
        """Start the fill monitor in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_stream,
            name="FillMonitor",
            daemon=True,
        )
        self._thread.start()
        print("[FILL MONITOR] Started (paper mode) — listening for trade updates")

    def stop(self) -> None:
        """Signal the WebSocket stream to close."""
        if not self._running:
            return
        self._running = False
        try:
            self._stream.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"[FILL MONITOR] Error stopping stream: {exc}")
        print("[FILL MONITOR] Stop requested.")

    def is_running(self) -> bool:
        """Return True when the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _run_stream(self) -> None:
        """Entry point for the daemon thread — runs the asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._stream.run()
        except Exception as exc:  # noqa: BLE001
            print(f"[FILL MONITOR] Stream error: {exc}")
        finally:
            self._running = False
            loop.close()

    def _dispatch(self, data: Any) -> None:  # noqa: ANN401
        """Forward fill events to the OrderManager."""
        event: str = getattr(data, "event", "unknown")
        order = getattr(data, "order", None)

        if order is None:
            return

        symbol = str(order.symbol)
        broker_order_id = str(getattr(order, "id", "") or "")

        if event == "fill":
            filled_qty = float(order.filled_qty or 0)
            fill_price = float(order.filled_avg_price or 0)
            side = _normalize_enum_like(getattr(order, "side", ""))
            print(
                f"[FILL MONITOR] FILL {side.upper()} {filled_qty} {symbol} "
                f"@ ${fill_price:.2f}"
            )
            self._order_manager.handle_fill(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=str(getattr(order, "client_order_id", "") or ""),
                side=side,
                filled_qty=filled_qty,
                fill_price=fill_price,
                order_type=str(getattr(order, "type", "") or ""),
            )
            return

        if event == "partial_fill":
            filled_qty = float(order.filled_qty or 0)
            fill_price = float(order.filled_avg_price or 0)
            side = _normalize_enum_like(getattr(order, "side", ""))
            print(
                f"[FILL MONITOR] PARTIAL FILL {order.side} {filled_qty}/{order.qty} "
                f"{symbol} @ ${fill_price:.2f}"
            )
            if side == "buy":
                self._order_manager.handle_partial_fill(
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    client_order_id=str(getattr(order, "client_order_id", "") or ""),
                    side=side,
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    order_type=str(getattr(order, "type", "") or ""),
                )
        elif event in ("canceled", "expired"):
            print(f"[FILL MONITOR] ORDER {event.upper()}: {order.side} {symbol}")


def _normalize_enum_like(value: object) -> str:
    """Return a normalized lower-case string for enum-like values."""
    return str(value).split(".")[-1].strip().lower()


if __name__ == "__main__":
    import time

    monitor = FillMonitor()
    monitor.start()
    print("Fill monitor running. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
        print("Stopped.")
