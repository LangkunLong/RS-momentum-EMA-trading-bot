"""Alpaca order execution layer for the CANSLIM scanner.

Handles entry orders (market/limit), protective stop-losses (7-8% O'Neil rule),
and exit logic (MA violations, trailing stops). All order management functions
operate through a single TradingClient instance per process.

Design principles:
- Paper trading is mandatory. Live-account trading is deliberately disabled.
- Buy entries are submitted first; protective stops are reconciled after the
  actual fill so the stop is anchored to the real fill price.
- Stop-loss distance follows O'Neil: 8% below the *fill price* (configurable via
  ``STOP_LOSS_PCT`` in settings).
- Callers never import alpaca directly — this is the single seam for trading ops.
"""

from __future__ import annotations

import os
import math
import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Optional
from uuid import uuid4

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from config import settings
from core.execution_store import get_execution_store
from core.execution_workflow import (
    build_exit_client_order_id,
    build_stop_client_order_id,
    get_active_workflow_for_symbol,
    get_workflow,
    normalize_workflow_id,
    recover_active_position_workflow,
)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_trading_client: Optional[TradingClient] = None

_WORKING_ORDER_STATUSES = {"new", "partially_filled"}
_ACCEPTED_SUBMISSION_STATUSES = {
    "accepted",
    "accepted_for_bidding",
    "pending_new",
    *_WORKING_ORDER_STATUSES,
    "filled",
}
_TRANSITIONAL_ORDER_STATUSES = {
    "accepted",
    "accepted_for_bidding",
    "pending_cancel",
    "pending_new",
    "pending_replace",
    "pending_review",
    "held",
}
_TERMINAL_ORDER_STATUSES = {
    "calculated",
    "canceled",
    "done_for_day",
    "expired",
    "filled",
    "rejected",
    "replaced",
    "stopped",
    "suspended",
}
_DEFINITIVE_ZERO_FILL_SUBMISSION_FAILURE_STATUSES = {
    "calculated",
    "canceled",
    "done_for_day",
    "expired",
    "rejected",
    "stopped",
    "suspended",
}
_SAFETY_SNAPSHOT_TIMEOUT = 2.0
_SAFETY_SNAPSHOT_POLL_INTERVAL = 0.1
_SAFETY_SNAPSHOT_CONFIRMATIONS = 2
_CANCEL_EMPTY_CONFIRMATIONS = 2
_POSITION_SYNC_TIMEOUT = 5.0
_SAFETY_RECONCILIATION_LOCK = threading.RLock()


def _serialized_reconciliation(func):
    """Serialize broker safety mutations within this process."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _SAFETY_RECONCILIATION_LOCK:
            return func(*args, **kwargs)

    return wrapped


def _get_trading_client() -> TradingClient:
    """Return the module-level TradingClient, creating it on first call."""
    global _trading_client
    require_paper_mode()
    if _trading_client is None:
        api_key = settings.ALPACA_API_KEY
        secret_key = settings.ALPACA_SECRET_KEY
        if not api_key or not secret_key:
            raise EnvironmentError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. See .env.example."
            )
        _trading_client = TradingClient(api_key, secret_key, paper=True)
    return _trading_client


def _is_paper_mode() -> bool:
    """Return True when operating in paper (simulated) trading mode.

    Reads ALPACA_PAPER from the environment (default: True — paper mode).
    """
    raw = os.environ.get("ALPACA_PAPER", "true").strip().lower()
    return raw not in ("false", "0", "no")


def require_paper_mode(paper: bool | None = None) -> None:
    """Reject any attempt to connect execution components to a live account."""
    if paper is False or not _is_paper_mode():
        raise RuntimeError(
            "Live-account trading is disabled for this project. "
            "Set ALPACA_PAPER=true and use paper-account credentials."
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    """Outcome of a single order submission attempt.

    Attributes:
        success: True when the order was accepted by Alpaca.
        order_id: Alpaca order UUID when successful; empty string otherwise.
        symbol: Ticker symbol for this order.
        side: 'buy' or 'sell'.
        qty: Requested quantity (fractional shares supported).
        error: Error description when success is False; empty string otherwise.
    """

    success: bool
    order_id: str
    symbol: str
    side: str
    qty: float
    error: str = ""
    client_order_id: str = ""
    outcome_uncertain: bool = False


@dataclass
class ProtectiveStopResult:
    """Outcome of protective-stop reconciliation for a filled long position."""

    success: bool
    order_id: str
    symbol: str
    qty: float
    stop_price: float
    action: str
    error: str = ""
    client_order_id: str = ""


@dataclass
class PositionSummary:
    """Lightweight view of a current Alpaca position.

    Attributes:
        symbol: Ticker.
        qty: Shares held (positive = long).
        avg_entry_price: Average cost basis.
        current_price: Latest market price.
        unrealized_pl_pct: Unrealized P&L as a decimal (e.g. -0.07 = -7%).
    """

    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    unrealized_pl_pct: float
    open_orders: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry orders
# ---------------------------------------------------------------------------


def submit_bracket_buy(
    symbol: str,
    qty: float,
    stop_loss_pct: Optional[float] = None,
    limit_price: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> OrderResult:
    """Submit a buy entry order.

    The public name is retained for backward compatibility with the current
    auto-trader call sites. Protective stops are reconciled after the fill so
    they can be anchored to the actual fill price.

    Args:
        symbol: Ticker to buy (e.g. ``'NVDA'``).
        qty: Number of shares (fractional allowed).
        stop_loss_pct: Retained for API compatibility. Protection is placed
            after fill by ``ensure_protective_stop()``.
        limit_price: If set, submit a day limit order at this price; otherwise
            submit a market order.

    Returns:
        OrderResult describing the accepted order or any error.
    """
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    broker_submission_started = False
    try:
        client = _get_trading_client()
        if limit_price is not None:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=client_order_id,
            )
        else:
            # Market bracket order — Alpaca requires a stop-loss price even
            # for market orders; we use the last known ask as a reference if
            # available, else the scanner must provide a reference price.
            # Here we let Alpaca handle the reference via ``trail_percent``
            # on a separate stop order after fill (see submit_stop_loss).
            # For now: submit the market buy, then set the stop separately
            # in a follow-up call so callers can always pair them.
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )

        broker_submission_started = True
        order: Order = client.submit_order(req)
        response_status = _order_status(order)
        if response_status not in _ACCEPTED_SUBMISSION_STATUSES:
            error = (
                "Broker returned an unsafe BUY response status: "
                f"{response_status or 'missing'}"
            )
            print(f"[ORDER ERROR] BUY {symbol}: {error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="buy",
                qty=qty,
                error=error,
                client_order_id=client_order_id or "",
                outcome_uncertain=not _submission_failure_is_definitive(
                    order,
                    response_status,
                ),
            )
        order_id = str(getattr(order, "id", "") or "").strip()
        if not order_id:
            error = "Broker returned a BUY response without an order id"
            print(f"[ORDER ERROR] BUY {symbol}: {error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="buy",
                qty=qty,
                error=error,
                client_order_id=client_order_id or "",
                outcome_uncertain=True,
            )
        mode = "paper"
        print(
            f"[ORDER/{mode}] BUY {qty} {symbol}"
            f"{f' @ limit ${limit_price:.2f}' if limit_price else ' @ market'}"
            f" | order_id={order_id}"
            f" | protective stop pending fill reconciliation ({stop_loss_pct:.1%})"
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            symbol=symbol,
            side="buy",
            qty=qty,
            client_order_id=client_order_id or "",
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[ORDER ERROR] BUY {symbol}: {exc}")
        return OrderResult(
            success=False,
            order_id="",
            symbol=symbol,
            side="buy",
            qty=qty,
            error=str(exc),
            client_order_id=client_order_id or "",
            outcome_uncertain=broker_submission_started,
        )


def submit_stop_loss(
    symbol: str,
    qty: float,
    stop_price: float,
    client_order_id: Optional[str] = None,
) -> OrderResult:
    """Place a GTC stop-sell order at ``stop_price``.

    Use this after a buy entry is filled to set the protective stop.

    Args:
        symbol: Ticker to protect.
        qty: Shares to sell on stop trigger.
        stop_price: Exact stop price (should be 7-8% below fill price).

    Returns:
        OrderResult for the stop order.
    """
    broker_submission_started = False
    try:
        client = _get_trading_client()
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        broker_submission_started = True
        order: Order = client.submit_order(req)
        identity_error = _stop_order_identity_error(
            order,
            symbol=symbol,
            client_order_id=client_order_id or "",
        )
        if identity_error:
            print(f"[ORDER ERROR] STOP {symbol}: {identity_error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="sell",
                qty=qty,
                error=identity_error,
                client_order_id=client_order_id or "",
                outcome_uncertain=True,
            )
        response_status = _order_status(order)
        if response_status not in _ACCEPTED_SUBMISSION_STATUSES:
            error = (
                "Broker returned an unsafe STOP response status: "
                f"{response_status or 'missing'}"
            )
            print(f"[ORDER ERROR] STOP {symbol}: {error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="sell",
                qty=qty,
                error=error,
                client_order_id=client_order_id or "",
                outcome_uncertain=not _submission_failure_is_definitive(
                    order,
                    response_status,
                ),
            )
        order_id = str(getattr(order, "id", "") or "").strip()
        mode = "paper"
        print(
            f"[ORDER/{mode}] STOP SELL {qty} {symbol} @ ${stop_price:.2f}"
            f" | order_id={order_id}"
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            symbol=symbol,
            side="sell",
            qty=qty,
            client_order_id=client_order_id or "",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ORDER ERROR] STOP {symbol}: {exc}")
        return OrderResult(
            success=False,
            order_id="",
            symbol=symbol,
            side="sell",
            qty=qty,
            error=str(exc),
            client_order_id=client_order_id or "",
            outcome_uncertain=broker_submission_started,
        )


def _normalize_enum_like(value: object) -> str:
    """Return a normalized lower-case string for Alpaca enum-like values."""
    return str(value).split(".")[-1].strip().lower()


def _order_status(order: Order) -> str:
    """Return the normalized broker order status."""
    return _normalize_enum_like(getattr(order, "status", ""))


def _submission_failure_is_definitive(order: Order, status: str) -> bool:
    """Return True only for an explicit terminal response with zero fill."""
    if status not in _DEFINITIVE_ZERO_FILL_SUBMISSION_FAILURE_STATUSES:
        return False
    try:
        return float(getattr(order, "filled_qty", 0) or 0) <= 0
    except (TypeError, ValueError):
        return False


def _is_working_order(order: Order) -> bool:
    """Return True only for broker statuses safe to rely on as working."""
    return _order_status(order) in _WORKING_ORDER_STATUSES


class _SnapshotUnstableError(RuntimeError):
    """Raised when broker position/order state does not converge safely."""

    def __init__(self, message: str, *, action: str = "snapshot_unstable") -> None:
        super().__init__(message)
        self.action = action


def _order_qty(order: Order) -> float:
    """Return an order quantity as float, defaulting to 0.0 on parse errors."""
    try:
        return float(getattr(order, "qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_stop_price(order: Order) -> float | None:
    """Return an order stop price when available."""
    try:
        stop_price = getattr(order, "stop_price", None)
        if stop_price is None:
            return None
        return float(stop_price)
    except (TypeError, ValueError):
        return None


def _is_stop_sell_order(order: Order) -> bool:
    """Return True when an open order is a stop-based sell order."""
    side = _normalize_enum_like(getattr(order, "side", ""))
    order_type = _normalize_enum_like(getattr(order, "type", ""))
    return side == "sell" and order_type in {"stop", "stop_limit"}


def _is_non_stop_sell_order(order: Order) -> bool:
    """Return True when an open order is a non-stop sell order."""
    side = _normalize_enum_like(getattr(order, "side", ""))
    return side == "sell" and not _is_stop_sell_order(order)


def _is_close_match(left: float, right: float, tolerance: float = 0.01) -> bool:
    """Return True when two values are within a small tolerance."""
    return abs(left - right) <= tolerance


def _order_remaining_qty(order: Order) -> float:
    """Return the unfilled quantity still capable of changing exposure."""
    try:
        qty = float(getattr(order, "qty", 0) or 0)
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, qty - filled_qty)


def _is_workflow_stop_order(order: Order, workflow_id: str) -> bool:
    """Return True for an exact working GTC stop owned by ``workflow_id``."""
    client_order_id = str(getattr(order, "client_order_id", "") or "")
    stop_prefix = f"{build_stop_client_order_id(workflow_id)}-"
    return (
        _is_working_order(order)
        and _normalize_enum_like(getattr(order, "side", "")) == "sell"
        and _normalize_enum_like(getattr(order, "type", "")) == "stop"
        and _normalize_enum_like(getattr(order, "time_in_force", "")) == "gtc"
        and (
            client_order_id == build_stop_client_order_id(workflow_id)
            or (
                client_order_id.startswith(stop_prefix)
                and normalize_workflow_id(client_order_id) == workflow_id
            )
        )
    )


def _stop_order_identity_error(
    order: Order,
    *,
    symbol: str,
    client_order_id: str,
) -> str:
    """Return why a broker object cannot identify the requested GTC stop."""
    order_id = str(getattr(order, "id", "") or "").strip()
    if not order_id:
        return "Broker returned a STOP response without an order id"
    observed_client_order_id = str(
        getattr(order, "client_order_id", "") or ""
    ).strip()
    if client_order_id and observed_client_order_id != client_order_id:
        return "Broker STOP response client order id does not match the request"
    observed_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
    if observed_symbol != symbol.strip().upper():
        return "Broker STOP response symbol does not match the request"
    if _normalize_enum_like(getattr(order, "side", "")) != "sell":
        return "Broker STOP response is not a sell order"
    if _normalize_enum_like(getattr(order, "type", "")) != "stop":
        return "Broker STOP response is not a stop order"
    if _normalize_enum_like(getattr(order, "time_in_force", "")) != "gtc":
        return "Broker STOP response is not GTC"
    return ""


def _latest_unknown_stop_submission(
    workflow_id: str,
) -> tuple[object | None, str | None, str]:
    """Return the exact unresolved stop client id from the latest stop audit."""
    workflow = get_workflow(workflow_id)
    if workflow is None:
        return None, None, ""
    for transition in reversed(getattr(workflow, "transitions", [])):
        if getattr(transition, "event", "") != "protective_stop_reconciled":
            continue
        details = getattr(transition, "details", {})
        if not isinstance(details, dict):
            return workflow, None, ""
        action = str(details.get("action", "") or "")
        if action != "submission_unknown" and not action.endswith(
            "_submission_unknown"
        ):
            return workflow, None, ""
        client_order_id = str(details.get("client_order_id", "") or "").strip()
        if not client_order_id:
            return (
                workflow,
                "",
                "Latest unresolved STOP submission has no client order id",
            )
        stop_base = build_stop_client_order_id(workflow_id)
        owns_identity = client_order_id == stop_base or (
            client_order_id.startswith(f"{stop_base}-")
            and normalize_workflow_id(client_order_id) == workflow_id
        )
        if not owns_identity:
            return (
                workflow,
                client_order_id,
                "Latest unresolved STOP client order id does not belong to the workflow",
            )
        return workflow, client_order_id, ""
    return workflow, None, ""


def _lookup_stop_submission(
    *,
    symbol: str,
    client_order_id: str,
) -> tuple[Order | None, bool, str]:
    """Resolve one STOP identity without treating lookup ambiguity as absence."""
    try:
        order = _get_trading_client().get_order_by_client_id(client_order_id)
    except Exception as exc:  # noqa: BLE001
        return None, False, f"Exact STOP lookup remains unresolved: {exc}"

    identity_error = _stop_order_identity_error(
        order,
        symbol=symbol,
        client_order_id=client_order_id,
    )
    if identity_error:
        return None, False, identity_error
    status = _order_status(order)
    if status in _ACCEPTED_SUBMISSION_STATUSES:
        return order, False, ""
    if _submission_failure_is_definitive(order, status):
        return (
            order,
            True,
            f"Exact STOP submission resolved to terminal status {status}",
        )
    return (
        None,
        False,
        f"Exact STOP lookup returned unresolved status {status or 'missing'}",
    )


def _submission_unknown_result(
    *,
    symbol: str,
    qty: float,
    stop_price: float,
    client_order_id: str,
    error: str,
) -> ProtectiveStopResult:
    """Keep one ambiguous broker mutation fenced behind its durable identity."""
    return ProtectiveStopResult(
        success=False,
        order_id="",
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        action="submission_unknown",
        error=error,
        client_order_id=client_order_id,
    )


def _is_workflow_exit_order(order: Order, workflow_id: str) -> bool:
    """Return True for the exact market-exit role owned by ``workflow_id``."""
    return (
        _is_working_order(order)
        and _is_non_stop_sell_order(order)
        and _normalize_enum_like(getattr(order, "type", "")) == "market"
        and str(getattr(order, "client_order_id", "") or "")
        == build_exit_client_order_id(workflow_id)
    )


def _position_fingerprint(position: PositionSummary | None) -> tuple[object, ...]:
    """Return the position fields that can change a safety decision."""
    if position is None:
        return (None,)
    return (
        position.symbol,
        round(float(position.qty), 8),
        round(float(position.avg_entry_price), 8),
    )


def _order_fingerprint(order: Order) -> tuple[object, ...]:
    """Return the order fields that can change exposure or protection."""
    return (
        str(getattr(order, "id", "") or ""),
        _order_status(order),
        _normalize_enum_like(getattr(order, "side", "")),
        _normalize_enum_like(getattr(order, "type", "")),
        str(getattr(order, "client_order_id", "") or ""),
        round(_order_qty(order), 8),
        round(_order_remaining_qty(order), 8),
        _order_stop_price(order),
    )


def _sample_stable_symbol_state(
    symbol: str,
    *,
    timeout: float | None = None,
) -> tuple[PositionSummary | None, list[Order]]:
    """Return two identical strict broker observations with working orders only."""
    resolved_timeout = _SAFETY_SNAPSHOT_TIMEOUT if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + resolved_timeout
    previous: tuple[object, ...] | None = None
    confirmations = 0
    last_transitioning = False

    while True:
        positions = get_open_positions(raise_on_error=True)
        position = next((item for item in positions if item.symbol == symbol), None)
        orders = get_open_orders(symbol, raise_on_error=True)
        last_transitioning = any(not _is_working_order(order) for order in orders)
        fingerprint = (
            _position_fingerprint(position),
            tuple(sorted(_order_fingerprint(order) for order in orders)),
        )

        if not last_transitioning and fingerprint == previous:
            confirmations += 1
        elif not last_transitioning:
            previous = fingerprint
            confirmations = 1
        else:
            previous = None
            confirmations = 0

        if confirmations >= _SAFETY_SNAPSHOT_CONFIRMATIONS:
            return position, orders

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            action = "orders_transitioning" if last_transitioning else "snapshot_unstable"
            raise _SnapshotUnstableError(
                f"Broker state for {symbol} did not converge to stable working orders",
                action=action,
            )
        time.sleep(min(_SAFETY_SNAPSHOT_POLL_INTERVAL, remaining))


def _cancel_order_ids_verified(
    symbol: str,
    order_ids: set[str],
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> int:
    """Cancel explicit symbol order IDs and prove each target disappears."""
    targets = {str(order_id) for order_id in order_ids if str(order_id)}
    if not targets:
        return 0
    client = _get_trading_client()
    deadline = time.monotonic() + max(0.0, timeout)
    absent_confirmations = 0
    errors: dict[str, str] = {}

    while True:
        open_orders = get_open_orders(symbol, raise_on_error=True)
        remaining_by_id = {
            str(getattr(order, "id", "") or ""): order
            for order in open_orders
            if str(getattr(order, "id", "") or "") in targets
        }
        if not remaining_by_id:
            absent_confirmations += 1
            if absent_confirmations >= _CANCEL_EMPTY_CONFIRMATIONS:
                return len(targets)
        else:
            absent_confirmations = 0
            for order_id, order in remaining_by_id.items():
                if _order_status(order) == "pending_cancel":
                    continue
                try:
                    client.cancel_order_by_id(order_id)
                    errors.pop(order_id, None)
                except Exception as exc:  # noqa: BLE001
                    errors[order_id] = str(exc)

        wait_remaining = deadline - time.monotonic()
        if wait_remaining <= 0:
            remaining_text = ", ".join(
                f"{order_id}:{_order_status(order) or 'unknown'}"
                for order_id, order in remaining_by_id.items()
            )
            error_text = "; ".join(
                f"{order_id}: {message}" for order_id, message in errors.items()
            )
            detail = error_text or "broker still reports target orders open"
            raise RuntimeError(
                f"Could not verify cancellation for {symbol} [{remaining_text}]: {detail}"
            )
        time.sleep(min(max(0.0, poll_interval), wait_remaining))


def _wait_for_terminal_buy_order_chain(
    symbol: str,
    order_ids: set[str],
    *,
    workflow_id: str,
    timeout: float = 5.0,
    poll_interval: float = 0.1,
    persist_fill_checkpoints: bool = False,
) -> float:
    """Fence entry cancellation on terminal order/replacement-chain state."""
    known_ids = {str(order_id) for order_id in order_ids if str(order_id)}
    if not known_ids:
        return 0.0

    workflow = get_workflow(workflow_id)
    if workflow is None:
        raise RuntimeError(f"Entry fence workflow {workflow_id} was not found")

    client = _get_trading_client()
    deadline = time.monotonic() + max(0.0, timeout)
    terminal_orders: dict[str, Order] = {}
    last_errors: dict[str, str] = {}
    persisted_ids: set[str] = set()

    while True:
        all_terminal = True
        for order_id in list(known_ids):
            try:
                order = client.get_order_by_id(order_id)
                last_errors.pop(order_id, None)
            except Exception as exc:  # noqa: BLE001
                all_terminal = False
                last_errors[order_id] = str(exc)
                continue

            order_symbol = str(getattr(order, "symbol", symbol) or symbol)
            if order_symbol != symbol:
                raise RuntimeError(
                    f"Entry order {order_id} belongs to {order_symbol}, not {symbol}"
                )
            if _normalize_enum_like(getattr(order, "side", "")) != "buy":
                raise RuntimeError(f"Entry fence order {order_id} is not a buy")

            if order_id not in persisted_ids:
                workflow.repair_entry_order_reference(
                    broker_order_id=order_id,
                    client_order_id=str(
                        getattr(order, "client_order_id", "") or ""
                    ),
                )
                persisted_ids.add(order_id)

            for relation in ("replaces", "replaced_by"):
                related_order_id = str(getattr(order, relation, "") or "")
                if related_order_id:
                    known_ids.add(related_order_id)

            status = _order_status(order)
            replaced_by = str(getattr(order, "replaced_by", "") or "")
            if status == "replaced" and (
                not replaced_by or replaced_by == order_id
            ):
                all_terminal = False
                terminal_orders.pop(order_id, None)
                last_errors[order_id] = "replaced order has no traversable child"
                continue
            if status in _TERMINAL_ORDER_STATUSES:
                terminal_orders[order_id] = order
                continue

            all_terminal = False
            if status not in _TRANSITIONAL_ORDER_STATUSES | _WORKING_ORDER_STATUSES:
                last_errors[order_id] = f"unknown order status {status or 'missing'}"
                continue
            if status != "pending_cancel":
                try:
                    client.cancel_order_by_id(order_id)
                    last_errors.pop(order_id, None)
                except Exception as exc:  # noqa: BLE001
                    last_errors[order_id] = str(exc)

        if all_terminal and known_ids.issubset(terminal_orders):
            total_filled = 0.0
            for order in terminal_orders.values():
                try:
                    filled_qty = float(getattr(order, "filled_qty", 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Terminal entry filled quantity is invalid") from exc
                if not math.isfinite(filled_qty) or filled_qty < 0:
                    raise RuntimeError("Terminal entry filled quantity is invalid")
                total_filled += filled_qty
                if not persist_fill_checkpoints or filled_qty <= 0:
                    continue
                try:
                    fill_price = float(
                        getattr(order, "filled_avg_price", 0) or 0
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Terminal entry fill price is invalid") from exc
                if not math.isfinite(fill_price) or fill_price <= 0:
                    raise RuntimeError("Terminal entry fill price is invalid")
                order_id = str(getattr(order, "id", "") or "")
                durable_qty = max(
                    (
                        float(transition.details.get("qty", 0.0) or 0.0)
                        for transition in workflow.transitions
                        if transition.event == "buy_fill_received"
                        and str(
                            transition.details.get("broker_order_id", "") or ""
                        )
                        == order_id
                    ),
                    default=0.0,
                )
                if filled_qty > durable_qty + 0.0001:
                    workflow.mark_buy_fill(
                        qty=filled_qty,
                        fill_price=fill_price,
                        broker_order_id=order_id,
                        restore_active=False,
                    )
            return total_filled

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            details = "; ".join(
                f"{order_id}: {message}" for order_id, message in last_errors.items()
            ) or "entry order chain did not become terminal"
            raise RuntimeError(f"Could not fence terminal {symbol} entry: {details}")
        time.sleep(min(max(0.0, poll_interval), remaining))


def _durable_sell_fill_qty_from_workflow(workflow: object) -> float:
    """Sum the maximum cumulative SELL checkpoint per durable broker order."""
    by_order: dict[str, float] = {}
    for transition in getattr(workflow, "transitions", []):
        if getattr(transition, "event", "") not in {
            "sell_partial_fill_received",
            "sell_fill_received",
        }:
            continue
        details = getattr(transition, "details", {})
        order_id = str(details.get("broker_order_id", "") or "")
        if not order_id:
            continue
        try:
            quantity = float(details.get("qty", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Durable sell fill quantity is invalid") from exc
        if not math.isfinite(quantity) or quantity < 0:
            raise RuntimeError("Durable sell fill quantity is invalid")
        by_order[order_id] = max(by_order.get(order_id, 0.0), quantity)
    return sum(by_order.values())


def _durable_buy_fill_order_ids_from_workflow(workflow: object) -> set[str]:
    """Return every identified BUY fill order persisted by the workflow."""
    return {
        str(getattr(transition, "details", {}).get("broker_order_id", "") or "")
        for transition in getattr(workflow, "transitions", [])
        if getattr(transition, "event", "") == "buy_fill_received"
        and str(
            getattr(transition, "details", {}).get("broker_order_id", "") or ""
        )
    }


@_serialized_reconciliation
def ensure_protective_stop(
    symbol: str,
    qty: float,
    fill_price: float,
    stop_loss_pct: Optional[float] = None,
    open_orders: Optional[list[Order]] = None,
    workflow_id: Optional[str] = None,
    entry_order_id: Optional[str] = None,
    entry_order_ids: Optional[set[str]] = None,
    durable_sell_fill_qty: float = 0.0,
) -> ProtectiveStopResult:
    """Fence causal entry exposure and prove exact workflow-linked protection."""
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT
    del open_orders  # Injected snapshots cannot prove current broker safety.

    if not workflow_id:
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=qty,
            stop_price=round(fill_price * (1 - stop_loss_pct), 2),
            action="missing_workflow",
            error="A workflow id is required before protective-stop mutation",
        )

    try:
        observed_qty = max(0.0, float(qty))
        resolved_sell_fill_qty = float(durable_sell_fill_qty)
        if not math.isfinite(resolved_sell_fill_qty) or resolved_sell_fill_qty < 0:
            raise ValueError("Durable sell fill quantity must be finite and non-negative")
        deadline = time.monotonic() + _POSITION_SYNC_TIMEOUT
        attempt = 0
        terminal_filled_qty = 0.0
        durable_entry_ids = {
            str(order_id)
            for order_id in (entry_order_ids or set())
            if str(order_id)
        }
        if entry_order_id:
            durable_entry_ids.add(str(entry_order_id))
        fenced_entry_ids: set[str] = set()
        while True:
            attempt += 1
            current_orders = get_open_orders(symbol, raise_on_error=True)
            buy_ids = {
                str(getattr(order, "id", "") or "")
                for order in current_orders
                if _normalize_enum_like(getattr(order, "side", "")) == "buy"
                and str(getattr(order, "id", "") or "")
            }
            if buy_ids:
                _cancel_order_ids_verified(symbol, buy_ids)

            if buy_ids:
                durable_entry_ids.update(buy_ids)
            refreshed_workflow = get_workflow(workflow_id)
            if refreshed_workflow is not None:
                refreshed_buy_ids = _durable_buy_fill_order_ids_from_workflow(
                    refreshed_workflow
                )
                if refreshed_buy_ids:
                    durable_entry_ids.update(refreshed_buy_ids)
                refreshed_sell_fill_qty = _durable_sell_fill_qty_from_workflow(
                    refreshed_workflow
                )
                if refreshed_sell_fill_qty > resolved_sell_fill_qty:
                    observed_qty = max(
                        0.0,
                        observed_qty
                        - (refreshed_sell_fill_qty - resolved_sell_fill_qty),
                    )
                    resolved_sell_fill_qty = refreshed_sell_fill_qty
            if durable_entry_ids - fenced_entry_ids:
                terminal_filled_qty = max(
                    terminal_filled_qty,
                    _wait_for_terminal_buy_order_chain(
                        symbol,
                        set(durable_entry_ids),
                        workflow_id=workflow_id,
                    ),
                )
                fenced_entry_ids = set(durable_entry_ids)

            reconcile_kwargs = {
                "workflow_id": workflow_id,
                "stop_loss_pct": stop_loss_pct,
            }
            if durable_entry_ids:
                if resolved_sell_fill_qty > terminal_filled_qty + 0.0001:
                    raise RuntimeError(
                        "Durable sell fills exceed terminal entry fills "
                        f"({resolved_sell_fill_qty} > {terminal_filled_qty})"
                    )
                causal_net_qty = max(
                    0.0,
                    terminal_filled_qty - resolved_sell_fill_qty,
                )
                reconcile_kwargs["minimum_position_qty"] = max(
                    observed_qty,
                    causal_net_qty,
                )
            protection = reconcile_symbol_after_exit_failure(symbol, **reconcile_kwargs)
            if protection.success and protection.action in {"reused", "submitted"}:
                post_reconcile_workflow = get_workflow(workflow_id)
                durable_transition_changed = False
                if post_reconcile_workflow is not None:
                    post_buy_ids = _durable_buy_fill_order_ids_from_workflow(
                        post_reconcile_workflow
                    )
                    if post_buy_ids - fenced_entry_ids:
                        durable_entry_ids.update(post_buy_ids)
                        durable_transition_changed = True
                    post_sell_fill_qty = _durable_sell_fill_qty_from_workflow(
                        post_reconcile_workflow
                    )
                    if post_sell_fill_qty > resolved_sell_fill_qty:
                        observed_qty = max(
                            0.0,
                            observed_qty
                            - (post_sell_fill_qty - resolved_sell_fill_qty),
                        )
                        resolved_sell_fill_qty = post_sell_fill_qty
                        durable_transition_changed = True
                if durable_transition_changed:
                    if time.monotonic() >= deadline:
                        return ProtectiveStopResult(
                            success=False,
                            order_id=protection.order_id,
                            symbol=symbol,
                            qty=qty,
                            stop_price=protection.stop_price,
                            action="position_sync_pending",
                            error=(
                                "Durable exposure changed while final protection "
                                "was being verified"
                            ),
                            client_order_id=protection.client_order_id,
                        )
                    continue
                verification_orders = get_open_orders(symbol, raise_on_error=True)
                late_buy_ids = {
                    str(getattr(order, "id", "") or "")
                    for order in verification_orders
                    if _normalize_enum_like(getattr(order, "side", "")) == "buy"
                    and str(getattr(order, "id", "") or "")
                }
                if late_buy_ids:
                    _cancel_order_ids_verified(symbol, late_buy_ids)
                    durable_entry_ids.update(late_buy_ids)
                    fenced_entry_ids.difference_update(late_buy_ids)
                    if time.monotonic() >= deadline:
                        return ProtectiveStopResult(
                            success=False,
                            order_id=protection.order_id,
                            symbol=symbol,
                            qty=qty,
                            stop_price=protection.stop_price,
                            action="pending_buy",
                            error=(
                                "A BUY appeared while final protection was being "
                                "verified"
                            ),
                            client_order_id=protection.client_order_id,
                        )
                    continue
                return protection
            can_retry = (
                (bool(durable_entry_ids) and time.monotonic() < deadline)
                or (not durable_entry_ids and attempt < 3)
            )
            if protection.action in {"pending_buy", "position_sync_pending"} and can_retry:
                time.sleep(_SAFETY_SNAPSHOT_POLL_INTERVAL)
                continue
            if protection.action in {"unsafe_orders", "flat_with_open_orders"}:
                cancel_open_orders_verified(symbol)
                if not can_retry:
                    return protection
                continue
            if protection.action == "flat":
                if can_retry:
                    time.sleep(_SAFETY_SNAPSHOT_POLL_INTERVAL)
                    continue
                return ProtectiveStopResult(
                    success=False,
                    order_id="",
                    symbol=symbol,
                    qty=qty,
                    stop_price=round(fill_price * (1 - stop_loss_pct), 2),
                    action="position_not_visible",
                    error="A reported buy fill is not yet visible as a broker position",
                )
            if protection.action == "pending_exit":
                return ProtectiveStopResult(
                    success=False,
                    order_id=protection.order_id,
                    symbol=symbol,
                    qty=protection.qty,
                    stop_price=protection.stop_price,
                    action="pending_exit",
                    error="Position is exiting rather than stop-protected",
                    client_order_id=protection.client_order_id,
                )
            return protection
    except Exception as exc:  # noqa: BLE001
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=qty,
            stop_price=round(fill_price * (1 - stop_loss_pct), 2),
            action="reconciliation_failed",
            error=str(exc),
        )

    return ProtectiveStopResult(
        success=False,
        order_id="",
        symbol=symbol,
        qty=qty,
        stop_price=round(fill_price * (1 - stop_loss_pct), 2),
        action="pending_buy",
        error="Entry remainder did not converge before protection",
    )


@_serialized_reconciliation
def reconcile_open_position_stops(
    stop_loss_pct: Optional[float] = None,
    positions: Optional[list[PositionSummary]] = None,
) -> list[ProtectiveStopResult]:
    """Repair missing or stale protective stops for the current portfolio."""
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    current_positions = (
        positions if positions is not None else get_open_positions(raise_on_error=True)
    )
    results: list[ProtectiveStopResult] = []

    for position in current_positions:
        if position.qty <= 0 or position.avg_entry_price <= 0:
            continue

        workflow = get_active_workflow_for_symbol(position.symbol)
        if workflow is None:
            workflow = recover_active_position_workflow(
                position.symbol,
                qty=position.qty,
                avg_entry_price=position.avg_entry_price,
            )
        results.append(
            reconcile_symbol_after_exit_failure(
                position.symbol,
                workflow_id=workflow.workflow_id,
                stop_loss_pct=stop_loss_pct,
                minimum_position_qty=position.qty,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Exit orders
# ---------------------------------------------------------------------------


def submit_market_sell(
    symbol: str,
    qty: float,
    client_order_id: Optional[str] = None,
) -> OrderResult:
    """Submit an immediate market sell for ``qty`` shares of ``symbol``.

    Use for O'Neil-style exits: price violates key moving average, loss > 8%,
    or stop already triggered and you want to ensure closure.

    Args:
        symbol: Ticker to sell.
        qty: Shares to liquidate.

    Returns:
        OrderResult for the sell.
    """
    broker_submission_started = False
    try:
        client = _get_trading_client()
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        broker_submission_started = True
        order: Order = client.submit_order(req)
        response_status = _order_status(order)
        if response_status not in _ACCEPTED_SUBMISSION_STATUSES:
            error = (
                "Broker returned an unsafe SELL response status: "
                f"{response_status or 'missing'}"
            )
            print(f"[ORDER ERROR] SELL {symbol}: {error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="sell",
                qty=qty,
                error=error,
                client_order_id=client_order_id or "",
                outcome_uncertain=not _submission_failure_is_definitive(
                    order,
                    response_status,
                ),
            )
        order_id = str(getattr(order, "id", "") or "").strip()
        if not order_id:
            error = "Broker returned a SELL response without an order id"
            print(f"[ORDER ERROR] SELL {symbol}: {error}")
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                side="sell",
                qty=qty,
                error=error,
                client_order_id=client_order_id or "",
                outcome_uncertain=True,
            )
        mode = "paper"
        print(f"[ORDER/{mode}] SELL {qty} {symbol} @ market | order_id={order_id}")
        return OrderResult(
            success=True,
            order_id=order_id,
            symbol=symbol,
            side="sell",
            qty=qty,
            client_order_id=client_order_id or "",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ORDER ERROR] SELL {symbol}: {exc}")
        return OrderResult(
            success=False,
            order_id="",
            symbol=symbol,
            side="sell",
            qty=qty,
            error=str(exc),
            client_order_id=client_order_id or "",
            outcome_uncertain=broker_submission_started,
        )

def close_position(
    symbol: str,
    client_order_id: Optional[str] = None,
) -> OrderResult:
    """Close the entire open position in ``symbol`` at market.

    Convenience wrapper: fetches current qty from Alpaca and submits a
    full market sell. Idempotent — returns success=True if no position exists.

    Args:
        symbol: Ticker of the position to close.

    Returns:
        OrderResult for the closing sell, or a synthetic success if flat.
    """
    try:
        client = _get_trading_client()
        pos = client.get_open_position(symbol)
        qty = float(pos.qty)
        if qty <= 0:
            return OrderResult(success=True, order_id="", symbol=symbol, side="sell", qty=0)
        return submit_market_sell(symbol, qty, client_order_id=client_order_id)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "position does not exist" in msg.lower() or "404" in msg:
            return OrderResult(success=True, order_id="", symbol=symbol, side="sell", qty=0)
        print(f"[ORDER ERROR] close_position {symbol}: {exc}")
        return OrderResult(success=False, order_id="", symbol=symbol, side="sell", qty=0, error=msg)


# ---------------------------------------------------------------------------
# Position & order inspection
# ---------------------------------------------------------------------------


def get_open_positions(*, raise_on_error: bool = False) -> list[PositionSummary]:
    """Return all open positions with unrealized P&L percentages.

    Returns:
        List of PositionSummary objects, one per open position.
        Returns an empty list on any error unless ``raise_on_error`` is true.
    """
    client = _get_trading_client()
    try:
        positions: list[Position] = client.get_all_positions()
        return [
            PositionSummary(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                unrealized_pl_pct=float(p.unrealized_plpc),
            )
            for p in positions
        ]
    except Exception as exc:  # noqa: BLE001
        if raise_on_error:
            raise
        print(f"[ORDER ERROR] get_open_positions: {exc}")
        return []


def get_open_orders(
    symbol: Optional[str] = None,
    *,
    raise_on_error: bool = False,
) -> list[Order]:
    """Return pending orders, optionally filtered to a single symbol.

    Args:
        symbol: If provided, only return orders for this ticker.

    Returns:
        List of Alpaca Order objects. Empty on error unless
        ``raise_on_error`` is true.
    """
    client = _get_trading_client()
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None)
        return client.get_orders(req)
    except Exception as exc:  # noqa: BLE001
        if raise_on_error:
            raise
        print(f"[ORDER ERROR] get_open_orders: {exc}")
        return []


def get_closed_orders(
    symbol: str,
    *,
    limit: int = 50,
    raise_on_error: bool = False,
) -> list[Order]:
    """Return recent closed orders for a symbol, optionally propagating errors."""
    client = _get_trading_client()
    try:
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            limit=max(1, int(limit)),
            direction=Sort.DESC,
        )
        return client.get_orders(request)
    except Exception as exc:  # noqa: BLE001
        if raise_on_error:
            raise
        print(f"[ORDER ERROR] get_closed_orders: {exc}")
        return []


def _cleanup_submitted_stop(symbol: str, order_id: str) -> str:
    """Best-effort removal of a stop that failed its exact postcondition."""
    if not order_id:
        return "submitted stop had no broker order id"
    cancel_error = ""
    try:
        _get_trading_client().cancel_order_by_id(order_id)
    except Exception as exc:  # noqa: BLE001
        cancel_error = f"targeted cancel for unproven stop {order_id} failed: {exc}"
    try:
        _cancel_order_ids_verified(symbol, {order_id})
    except Exception as exc:  # noqa: BLE001
        proof_error = f"could not prove unproven stop {order_id} terminal or absent: {exc}"
        return "; ".join(item for item in (cancel_error, proof_error) if item)
    return ""


@_serialized_reconciliation
def reconcile_symbol_after_exit_failure(
    symbol: str,
    *,
    workflow_id: str,
    stop_loss_pct: float | None = None,
    minimum_position_qty: float = 0.0,
) -> ProtectiveStopResult:
    """Prove the symbol is flat, fully exiting, or exactly stop-protected."""
    if not workflow_id:
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=0.0,
            stop_price=0.0,
            action="missing_workflow",
            error="A workflow id is required for safety reconciliation",
        )

    resolved_stop_loss_pct = (
        settings.STOP_LOSS_PCT if stop_loss_pct is None else abs(float(stop_loss_pct))
    )
    workflow, stop_client_order_id, persisted_identity_error = (
        _latest_unknown_stop_submission(workflow_id)
    )
    recovered_stop_order: Order | None = None
    if stop_client_order_id is not None:
        if persisted_identity_error:
            return _submission_unknown_result(
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                client_order_id=stop_client_order_id,
                error=persisted_identity_error,
            )
        recovered_stop_order, definitive_failure, lookup_error = (
            _lookup_stop_submission(
                symbol=symbol,
                client_order_id=stop_client_order_id,
            )
        )
        if recovered_stop_order is None:
            return _submission_unknown_result(
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                client_order_id=stop_client_order_id,
                error=lookup_error,
            )
        if definitive_failure:
            return ProtectiveStopResult(
                success=False,
                order_id=str(getattr(recovered_stop_order, "id", "") or ""),
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                action="submit_failed",
                error=lookup_error,
                client_order_id=stop_client_order_id,
            )
    try:
        position, open_orders = _sample_stable_symbol_state(symbol)
    except _SnapshotUnstableError as exc:
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=0.0,
            stop_price=0.0,
            action=exc.action,
            error=str(exc),
        )
    pending_exit_intents = [
        intent
        for intent in get_execution_store().load_pending_submission_intents(
            symbol=symbol
        )
        if intent.get("event") == "exit_submission_intent"
    ]
    if pending_exit_intents:
        visible_exit_orders = [
            order for order in open_orders if _is_non_stop_sell_order(order)
        ]
        visible_exit = (
            visible_exit_orders[0] if len(visible_exit_orders) == 1 else None
        )
        full_linked_exit = bool(
            position is not None
            and visible_exit is not None
            and len(open_orders) == 1
            and _is_workflow_exit_order(visible_exit, workflow_id)
            and _is_close_match(
                _order_remaining_qty(visible_exit),
                position.qty,
                tolerance=0.0001,
            )
        )
        if full_linked_exit:
            return ProtectiveStopResult(
                success=True,
                order_id=str(getattr(visible_exit, "id", "") or ""),
                symbol=symbol,
                qty=position.qty,
                stop_price=round(
                    position.avg_entry_price * (1 - resolved_stop_loss_pct),
                    2,
                ),
                action="pending_exit",
            )
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=position.qty if position is not None else 0.0,
            stop_price=(
                round(position.avg_entry_price * (1 - resolved_stop_loss_pct), 2)
                if position is not None
                else 0.0
            ),
            action="exit_outcome_unresolved",
            error=(
                "The exact exit outcome remains unresolved; a competing STOP "
                "cannot be reconciled"
            ),
        )
    if position is None:
        if recovered_stop_order is not None:
            if _order_status(recovered_stop_order) == "filled" and not open_orders:
                return ProtectiveStopResult(
                    success=True,
                    order_id=str(getattr(recovered_stop_order, "id", "") or ""),
                    symbol=symbol,
                    qty=0.0,
                    stop_price=0.0,
                    action="flat",
                    client_order_id=stop_client_order_id or "",
                )
            return _submission_unknown_result(
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                client_order_id=stop_client_order_id or "",
                error=(
                    "Exact STOP identity has not converged with a visible position "
                    "and open-order postcondition"
                ),
            )
        if minimum_position_qty > 0:
            return ProtectiveStopResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                action="position_sync_pending",
                error="Terminal entry fill is not yet visible in broker positions",
            )
        if open_orders:
            return ProtectiveStopResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=0.0,
                stop_price=0.0,
                action="flat_with_open_orders",
                error="Broker position is flat but symbol orders can still create exposure",
            )
        return ProtectiveStopResult(
            success=True,
            order_id="",
            symbol=symbol,
            qty=0.0,
            stop_price=0.0,
            action="flat",
        )

    if position.qty + 0.0001 < max(0.0, float(minimum_position_qty)):
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=position.qty,
            stop_price=round(position.avg_entry_price * (1 - resolved_stop_loss_pct), 2),
            action="position_sync_pending",
            error=(
                f"Broker position {position.qty} has not caught up to terminal "
                f"entry fills {minimum_position_qty}"
            ),
        )

    stop_price = round(position.avg_entry_price * (1 - resolved_stop_loss_pct), 2)
    if recovered_stop_order is not None:
        recovered_order_id = str(getattr(recovered_stop_order, "id", "") or "")
        matching_recovered_stops = [
            order
            for order in open_orders
            if _is_workflow_stop_order(order, workflow_id)
            and str(getattr(order, "id", "") or "") == recovered_order_id
            and str(getattr(order, "client_order_id", "") or "")
            == stop_client_order_id
            and _is_close_match(
                _order_remaining_qty(order),
                position.qty,
                tolerance=0.0001,
            )
            and _is_close_match(_order_stop_price(order) or 0.0, stop_price)
        ]
        if len(open_orders) == 1 and len(matching_recovered_stops) == 1:
            return ProtectiveStopResult(
                success=True,
                order_id=recovered_order_id,
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                action="reused",
                client_order_id=stop_client_order_id or "",
            )
        if open_orders:
            return _submission_unknown_result(
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                client_order_id=stop_client_order_id or "",
                error=(
                    "Exact STOP identity did not match the stable position/open-order "
                    "postcondition"
                ),
            )
    if any(_normalize_enum_like(getattr(order, "side", "")) == "buy" for order in open_orders):
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=position.qty,
            stop_price=stop_price,
            action="pending_buy",
            error="An open buy can increase exposure after the current safety snapshot",
        )

    exit_orders = [order for order in open_orders if _is_non_stop_sell_order(order)]
    if exit_orders:
        exit_order = exit_orders[0] if len(exit_orders) == 1 else None
        full_linked_exit = bool(
            exit_order is not None
            and len(open_orders) == 1
            and _is_workflow_exit_order(exit_order, workflow_id)
            and _is_close_match(
                _order_remaining_qty(exit_order),
                position.qty,
                tolerance=0.0001,
            )
        )
        if not full_linked_exit:
            return ProtectiveStopResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                action="unsafe_orders",
                error="Open sell orders do not prove one full workflow-linked exit",
            )
        return ProtectiveStopResult(
            success=True,
            order_id=str(getattr(exit_order, "id", "") or ""),
            symbol=symbol,
            qty=position.qty,
            stop_price=stop_price,
            action="pending_exit",
        )

    stop_orders = [order for order in open_orders if _is_stop_sell_order(order)]
    if stop_orders:
        stop_order = stop_orders[0] if len(stop_orders) == 1 else None
        exact_stop = bool(
            stop_order is not None
            and len(open_orders) == 1
            and _is_workflow_stop_order(stop_order, workflow_id)
            and _is_close_match(
                _order_remaining_qty(stop_order),
                position.qty,
                tolerance=0.0001,
            )
            and _is_close_match(_order_stop_price(stop_order) or 0.0, stop_price)
        )
        if not exact_stop:
            return ProtectiveStopResult(
                success=False,
                order_id=str(getattr(stop_order, "id", "") or "") if stop_order else "",
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                action="unsafe_orders",
                error="Open stop orders do not prove exact workflow-linked protection",
            )
        return ProtectiveStopResult(
            success=True,
            order_id=str(getattr(stop_order, "id", "") or ""),
            symbol=symbol,
            qty=position.qty,
            stop_price=stop_price,
            action="reused",
            client_order_id=str(getattr(stop_order, "client_order_id", "") or ""),
        )

    if open_orders:
        return ProtectiveStopResult(
            success=False,
            order_id="",
            symbol=symbol,
            qty=position.qty,
            stop_price=stop_price,
            action="unsafe_orders",
            error="Unrecognized open orders prevent a safety proof",
        )

    if recovered_stop_order is not None:
        submitted = OrderResult(
            success=True,
            order_id=str(getattr(recovered_stop_order, "id", "") or ""),
            symbol=symbol,
            side="sell",
            qty=position.qty,
            client_order_id=stop_client_order_id or "",
        )
    else:
        if workflow is None:
            return ProtectiveStopResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                action="missing_workflow",
                error="Cannot durably identify a STOP submission without its workflow",
            )
        stop_client_order_id = build_stop_client_order_id(workflow_id, uuid4().hex[:6])
        try:
            workflow.mark_protective_stop(
                success=False,
                stop_order_id="",
                stop_price=stop_price,
                action="submission_unknown",
                error="STOP submission identity persisted before broker mutation",
                stop_client_order_id=stop_client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            return ProtectiveStopResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=position.qty,
                stop_price=stop_price,
                action="submission_intent_persist_failed",
                error=str(exc),
                client_order_id=stop_client_order_id,
            )
        submitted = submit_stop_loss(
            symbol=symbol,
            qty=position.qty,
            stop_price=stop_price,
            client_order_id=stop_client_order_id,
        )
        if not submitted.success:
            if not submitted.outcome_uncertain:
                return ProtectiveStopResult(
                    success=False,
                    order_id=submitted.order_id,
                    symbol=symbol,
                    qty=position.qty,
                    stop_price=stop_price,
                    action="submit_failed",
                    error=submitted.error,
                    client_order_id=stop_client_order_id,
                )
            recovered_stop_order, definitive_failure, lookup_error = (
                _lookup_stop_submission(
                    symbol=symbol,
                    client_order_id=stop_client_order_id,
                )
            )
            if recovered_stop_order is None:
                return _submission_unknown_result(
                    symbol=symbol,
                    qty=position.qty,
                    stop_price=stop_price,
                    client_order_id=stop_client_order_id,
                    error="; ".join(
                        item for item in (submitted.error, lookup_error) if item
                    ),
                )
            if definitive_failure:
                return ProtectiveStopResult(
                    success=False,
                    order_id=str(getattr(recovered_stop_order, "id", "") or ""),
                    symbol=symbol,
                    qty=position.qty,
                    stop_price=stop_price,
                    action="submit_failed",
                    error=lookup_error,
                    client_order_id=stop_client_order_id,
                )
            submitted = OrderResult(
                success=True,
                order_id=str(getattr(recovered_stop_order, "id", "") or ""),
                symbol=symbol,
                side="sell",
                qty=position.qty,
                client_order_id=stop_client_order_id,
            )

    try:
        refreshed_position, refreshed_orders = _sample_stable_symbol_state(symbol)
    except Exception as exc:  # noqa: BLE001
        cleanup_error = _cleanup_submitted_stop(symbol, submitted.order_id)
        action = (
            "submission_unknown"
            if cleanup_error
            else (
                exc.action
                if isinstance(exc, _SnapshotUnstableError)
                else "postcondition_failed"
            )
        )
        return ProtectiveStopResult(
            success=False,
            order_id=submitted.order_id,
            symbol=symbol,
            qty=0.0,
            stop_price=stop_price,
            action=action,
            error="; ".join(item for item in (str(exc), cleanup_error) if item),
            client_order_id=stop_client_order_id,
        )
    if refreshed_position is None and not refreshed_orders:
        cleanup_error = _cleanup_submitted_stop(symbol, submitted.order_id)
        if cleanup_error:
            return _submission_unknown_result(
                symbol=symbol,
                qty=0.0,
                stop_price=stop_price,
                client_order_id=stop_client_order_id,
                error=cleanup_error,
            )
        return ProtectiveStopResult(
            success=True,
            order_id="",
            symbol=symbol,
            qty=0.0,
            stop_price=0.0,
            action="flat",
        )

    matching_stops = [
        order
        for order in refreshed_orders
        if refreshed_position is not None
        and _is_workflow_stop_order(order, workflow_id)
        and str(getattr(order, "id", "") or "") == submitted.order_id
        and str(getattr(order, "client_order_id", "") or "") == stop_client_order_id
        and _is_close_match(
            _order_remaining_qty(order),
            refreshed_position.qty,
            tolerance=0.0001,
        )
        and _is_close_match(
            _order_stop_price(order) or 0.0,
            round(refreshed_position.avg_entry_price * (1 - resolved_stop_loss_pct), 2),
        )
    ]
    if refreshed_position is not None and len(refreshed_orders) == 1 and len(matching_stops) == 1:
        return ProtectiveStopResult(
            success=True,
            order_id=submitted.order_id,
            symbol=symbol,
            qty=refreshed_position.qty,
            stop_price=round(
                refreshed_position.avg_entry_price * (1 - resolved_stop_loss_pct),
                2,
            ),
            action="submitted",
            client_order_id=stop_client_order_id,
        )

    cleanup_error = _cleanup_submitted_stop(symbol, submitted.order_id)
    action = "submission_unknown" if cleanup_error else "postcondition_failed"
    return ProtectiveStopResult(
        success=False,
        order_id=submitted.order_id,
        symbol=symbol,
        qty=refreshed_position.qty if refreshed_position is not None else 0.0,
        stop_price=stop_price,
        action=action,
        error="; ".join(
            item
            for item in (
                "Submitted stop did not converge to one exact protected broker state",
                cleanup_error,
            )
            if item
        ),
        client_order_id=stop_client_order_id,
    )


def cancel_open_orders(symbol: str) -> int:
    """Cancel all open orders for ``symbol``.

    Args:
        symbol: Ticker whose pending orders to cancel.

    Returns:
        Number of orders successfully cancelled.
    """
    client = _get_trading_client()
    orders = get_open_orders(symbol)
    cancelled = 0
    for order in orders:
        try:
            client.cancel_order_by_id(str(order.id))
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[ORDER ERROR] cancel order {order.id} for {symbol}: {exc}")
    return cancelled


@_serialized_reconciliation
def cancel_open_orders_verified(
    symbol: str,
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.1,
) -> int:
    """Cancel symbol orders in exposure-safe priority and prove stable emptiness."""
    client = _get_trading_client()
    deadline = time.monotonic() + max(0.0, timeout)
    seen_ids: set[str] = set()
    errors: dict[str, str] = {}
    empty_confirmations = 0

    while True:
        remaining = get_open_orders(symbol, raise_on_error=True)
        if not remaining:
            empty_confirmations += 1
            if empty_confirmations >= _CANCEL_EMPTY_CONFIRMATIONS:
                return len(seen_ids)
        else:
            empty_confirmations = 0
            priorities = {
                0
                if _normalize_enum_like(getattr(order, "side", "")) == "buy"
                else 2
                if _is_stop_sell_order(order)
                else 1
                for order in remaining
            }
            active_priority = min(priorities)
            active_group = [
                order
                for order in remaining
                if (
                    0
                    if _normalize_enum_like(getattr(order, "side", "")) == "buy"
                    else 2
                    if _is_stop_sell_order(order)
                    else 1
                )
                == active_priority
            ]
            for order in active_group:
                order_id = str(getattr(order, "id", "") or "")
                if not order_id:
                    continue
                seen_ids.add(order_id)
                if _order_status(order) == "pending_cancel":
                    continue
                try:
                    client.cancel_order_by_id(order_id)
                    errors.pop(order_id, None)
                except Exception as exc:  # noqa: BLE001
                    errors[order_id] = str(exc)

        wait_remaining = deadline - time.monotonic()
        if wait_remaining <= 0:
            remaining_text = ", ".join(
                f"{getattr(order, 'id', '')}:{_order_status(order) or 'unknown'}"
                for order in remaining
            )
            error_text = "; ".join(
                f"{order_id}: {message}" for order_id, message in errors.items()
            )
            raise RuntimeError(
                f"Timed out canceling {symbol} orders [{remaining_text}]"
                + (f": {error_text}" if error_text else "")
            )
        time.sleep(min(max(0.0, poll_interval), wait_remaining))


# ---------------------------------------------------------------------------
# Exit monitoring helpers
# ---------------------------------------------------------------------------


def check_exit_signals(
    positions: list[PositionSummary],
    stop_loss_pct: Optional[float] = None,
) -> list[PositionSummary]:
    """Filter positions that have breached the O'Neil stop-loss threshold.

    This is a *signal detector only* — it does NOT submit orders. Call
    ``close_position()`` on any returned symbols to act.

    O'Neil rule: sell any stock that drops 7-8% below your buy price.
    The default threshold is ``settings.STOP_LOSS_PCT``.

    Args:
        positions: List returned by ``get_open_positions()``.
        stop_loss_pct: Override the stop-loss percentage (default: settings value).

    Returns:
        Subset of ``positions`` whose ``unrealized_pl_pct`` is at or below
        the negative of ``stop_loss_pct`` (e.g., -0.07 for 7%).
    """
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT
    threshold = -abs(stop_loss_pct)
    return [p for p in positions if p.unrealized_pl_pct <= threshold]
