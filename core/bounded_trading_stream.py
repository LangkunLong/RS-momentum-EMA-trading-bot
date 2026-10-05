"""Finite Alpaca trade-update streams for admitted paper operations."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from alpaca.trading.stream import TradingStream

from config import settings
from core.operation_limits import OperationBudget, OperationCapExceeded
from core.scheduler_observation import record_event


class BudgetedTradingStream(TradingStream):
    """TradingStream that counts each websocket connect before network I/O."""

    def __init__(self, budget: OperationBudget, **kwargs: Any):
        self._operation_budget = budget
        super().__init__(**kwargs)
        self._websocket_params["open_timeout"] = budget.manifest.deadlines_seconds[
            "stream_connect_seconds"
        ]
        self._websocket_params["close_timeout"] = budget.manifest.deadlines_seconds[
            "stream_stop_seconds"
        ]

    async def _connect(self) -> None:
        try:
            self._operation_budget.enforce_operation_window(
                required_seconds=self._operation_budget.manifest.deadlines_seconds[
                    "stream_connect_seconds"
                ],
                resource="stream_connect_seconds",
            )
            self._operation_budget.consume("stream_connections")
        except OperationCapExceeded:
            # TradingStream's SDK loop catches connection errors and retries.
            # Lower its run flag so the SDK exits on the next loop boundary.
            self._should_run = False
            raise
        await super()._connect()


class ReadOnlyTradeUpdateObserver:
    """Observe bounded trade-update events without loading or mutating orders."""

    def __init__(self, budget: OperationBudget):
        self._budget = budget
        self._stream = BudgetedTradingStream(
            budget,
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=True,
        )
        self._thread: threading.Thread | None = None
        self._running = False
        self._handler_fault = False

        async def _on_trade_update(data: Any) -> None:
            try:
                self._budget.consume("stream_events")
            except OperationCapExceeded:
                self._handler_fault = True
                self._running = False
                self._stream._should_run = False
                return
            order = getattr(data, "order", None)
            details = {
                "event": str(getattr(data, "event", "unknown")),
                "symbol": str(getattr(order, "symbol", "") or "").upper(),
                "side": str(getattr(order, "side", "") or "").split(".")[-1].lower(),
                "order_type": str(getattr(order, "type", "") or "").split(".")[-1].lower(),
            }
            try:
                record_event(
                    "trade_update",
                    details["symbol"] or "unidentified",
                    "observed",
                    details,
                )
            except OperationCapExceeded:
                self._handler_fault = True
                self._running = False
                self._stream._should_run = False

        self._stream.subscribe_trade_updates(_on_trade_update)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_stream,
            name="ReadOnlyTradeUpdateObserver",
            daemon=True,
        )
        self._thread.start()

    def _run_stream(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._stream.run()
        except Exception:  # noqa: BLE001
            self._handler_fault = True
        finally:
            self._running = False
            loop.close()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_connected(self) -> bool:
        return (
            self._running
            and self.is_running()
            and bool(getattr(self._stream, "_running", False))
            and not self._handler_fault
        )

    def stop(self) -> bool:
        self._running = False
        self._stream._should_run = False
        timeout = self._budget.manifest.deadlines_seconds["stream_stop_seconds"]
        deadline = time.monotonic() + timeout
        errors: list[Exception] = []

        def request_stop() -> None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        stopper = threading.Thread(target=request_stop, daemon=True)
        stopper.start()
        stopper.join(timeout=max(0.0, deadline - time.monotonic()))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not stopper.is_alive() and not errors and not self.is_running()
