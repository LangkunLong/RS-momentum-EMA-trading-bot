"""Real-time Alpaca order-fill monitor using the Trade Updates WebSocket stream.

Listens for fill events on the paper account and routes them through
the OrderManager so workflow transitions stay centralized and auditable.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from alpaca.trading.stream import TradingStream

from config import settings
from core.order_execution import require_paper_mode
from core.order_manager import OrderManager


_STOP_JOIN_TIMEOUT_SECS = 5.0


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
        self._handler_fault = False

        async def _on_trade_update(data: Any) -> None:  # noqa: ANN401
            try:
                await asyncio.to_thread(self._dispatch, data)
            except Exception as exc:  # noqa: BLE001
                self._handler_fault = True
                print(f"[FILL MONITOR] Trade update handler error: {exc}")

        self._stream.subscribe_trade_updates(_on_trade_update)

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

    def stop(self) -> bool:
        """Stop the stream within one time budget and report full termination."""
        self._running = False
        deadline = time.monotonic() + _STOP_JOIN_TIMEOUT_SECS
        stop_errors: list[Exception] = []

        def request_stream_stop() -> None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                stop_errors.append(exc)

        stop_request = threading.Thread(
            target=request_stream_stop,
            name="FillMonitorStop",
            daemon=True,
        )
        stop_request.start()
        stop_request.join(timeout=max(0.0, deadline - time.monotonic()))
        request_finished = not stop_request.is_alive()

        if not request_finished:
            print("[FILL MONITOR] Stream stop request did not finish before timeout.")
        elif stop_errors:
            print(f"[FILL MONITOR] Error stopping stream: {stop_errors[0]}")

        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        worker_stopped = thread is None or not thread.is_alive()
        if not worker_stopped:
            print("[FILL MONITOR] Stream thread did not stop before timeout.")

        stopped = request_finished and not stop_errors and worker_stopped
        if stopped:
            print("[FILL MONITOR] Stopped.")
        else:
            print("[FILL MONITOR] Shutdown incomplete.")
        return stopped

    def is_running(self) -> bool:
        """Return True when the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def is_connected(self) -> bool:
        """Return True only while the wrapper and SDK stream are healthy."""
        return (
            self._running
            and self.is_running()
            and bool(getattr(self._stream, "_running", False))
            and not self._handler_fault
        )

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
        event = _normalize_enum_like(getattr(data, "event", "unknown"))
        order = getattr(data, "order", None)

        if order is None:
            return

        symbol = str(order.symbol)
        broker_order_id = str(getattr(order, "id", "") or "")
        client_order_id = str(getattr(order, "client_order_id", "") or "")
        order_type = str(getattr(order, "type", "") or "")
        replaces = str(getattr(order, "replaces", "") or "")
        replaced_by = str(getattr(order, "replaced_by", "") or "")

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
                client_order_id=client_order_id,
                side=side,
                filled_qty=filled_qty,
                fill_price=fill_price,
                order_type=order_type,
                replaces=replaces,
                replaced_by=replaced_by,
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
            if side in {"buy", "sell"}:
                self._order_manager.handle_partial_fill(
                    symbol=symbol,
                    broker_order_id=broker_order_id,
                    client_order_id=client_order_id,
                    side=side,
                    filled_qty=filled_qty,
                    fill_price=fill_price,
                    order_type=order_type,
                    replaces=replaces,
                    replaced_by=replaced_by,
                )
            return

        if event in {"canceled", "expired", "rejected", "replaced", "restated"}:
            print(f"[FILL MONITOR] ORDER {event.upper()}: {order.side} {symbol}")
            side = _normalize_enum_like(getattr(order, "side", ""))
            self._order_manager.handle_order_failure(
                symbol=symbol,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                side=side,
                order_type=order_type,
                status=event,
                filled_qty=float(getattr(order, "filled_qty", 0) or 0),
                fill_price=float(getattr(order, "filled_avg_price", 0) or 0),
                replaces=replaces,
                replaced_by=replaced_by,
            )


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
