"""Alpaca order execution layer for the CANSLIM scanner.

Handles entry orders (market/limit), protective stop-losses (7-8% O'Neil rule),
and exit logic (MA violations, trailing stops). All order management functions
operate through a single TradingClient instance per process.

Design principles:
- Paper trading by default (``paper=True``) — set ``ALPACA_PAPER`` in ``.env``
  to ``false`` only when ready for live trading.
- Buy entries are submitted first; protective stops are reconciled after the
  actual fill so the stop is anchored to the real fill price.
- Stop-loss distance follows O'Neil: 7% below the *fill price* (configurable via
  ``STOP_LOSS_PCT`` in settings).
- Callers never import alpaca directly — this is the single seam for trading ops.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

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
from core.execution_workflow import build_stop_client_order_id


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_trading_client: Optional[TradingClient] = None


def _get_trading_client() -> TradingClient:
    """Return the module-level TradingClient, creating it on first call."""
    global _trading_client
    if _trading_client is None:
        api_key = settings.ALPACA_API_KEY
        secret_key = settings.ALPACA_SECRET_KEY
        if not api_key or not secret_key:
            raise EnvironmentError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. See .env.example."
            )
        paper = _is_paper_mode()
        _trading_client = TradingClient(api_key, secret_key, paper=paper)
    return _trading_client


def _is_paper_mode() -> bool:
    """Return True when operating in paper (simulated) trading mode.

    Reads ALPACA_PAPER from the environment (default: True — paper mode).
    Set ALPACA_PAPER=false in .env only when ready for live trading.
    """
    raw = os.environ.get("ALPACA_PAPER", "true").strip().lower()
    return raw not in ("false", "0", "no")


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

    client = _get_trading_client()

    try:
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

        order: Order = client.submit_order(req)
        mode = "paper" if _is_paper_mode() else "LIVE"
        print(
            f"[ORDER/{mode}] BUY {qty} {symbol}"
            f"{f' @ limit ${limit_price:.2f}' if limit_price else ' @ market'}"
            f" | order_id={order.id}"
            f" | protective stop pending fill reconciliation ({stop_loss_pct:.1%})"
        )
        return OrderResult(
            success=True,
            order_id=str(order.id),
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
    client = _get_trading_client()
    try:
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        order: Order = client.submit_order(req)
        mode = "paper" if _is_paper_mode() else "LIVE"
        print(f"[ORDER/{mode}] STOP SELL {qty} {symbol} @ ${stop_price:.2f} | order_id={order.id}")
        return OrderResult(
            success=True,
            order_id=str(order.id),
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
        )


def _normalize_enum_like(value: object) -> str:
    """Return a normalized lower-case string for Alpaca enum-like values."""
    return str(value).split(".")[-1].strip().lower()


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


def ensure_protective_stop(
    symbol: str,
    qty: float,
    fill_price: float,
    stop_loss_pct: Optional[float] = None,
    open_orders: Optional[list[Order]] = None,
    workflow_id: Optional[str] = None,
) -> ProtectiveStopResult:
    """Ensure there is exactly one protective stop anchored to ``fill_price``."""
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    stop_price = round(fill_price * (1 - stop_loss_pct), 2)
    candidate_orders = open_orders if open_orders is not None else get_open_orders(symbol)
    stop_orders = [order for order in candidate_orders if _is_stop_sell_order(order)]

    matching_orders: list[Order] = []
    stale_orders: list[Order] = []
    for order in stop_orders:
        order_qty = _order_qty(order)
        order_stop_price = _order_stop_price(order)
        qty_matches = _is_close_match(order_qty, qty, tolerance=0.0001)
        price_matches = order_stop_price is not None and _is_close_match(order_stop_price, stop_price)
        if qty_matches and price_matches:
            matching_orders.append(order)
        else:
            stale_orders.append(order)

    client = _get_trading_client()

    if matching_orders:
        keeper = matching_orders[0]
        duplicates = matching_orders[1:] + stale_orders
        for order in duplicates:
            try:
                client.cancel_order_by_id(str(order.id))
            except Exception as exc:  # noqa: BLE001
                print(f"[ORDER ERROR] cancel duplicate stop {order.id} for {symbol}: {exc}")
        action = "cleaned" if duplicates else "reused"
        return ProtectiveStopResult(
            success=True,
            order_id=str(keeper.id),
            symbol=symbol,
            qty=qty,
            stop_price=stop_price,
            action=action,
        )

    for order in stale_orders:
        try:
            client.cancel_order_by_id(str(order.id))
        except Exception as exc:  # noqa: BLE001
            print(f"[ORDER ERROR] cancel stale stop {order.id} for {symbol}: {exc}")

    stop_result = submit_stop_loss(
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        client_order_id=build_stop_client_order_id(workflow_id) if workflow_id else None,
    )
    return ProtectiveStopResult(
        success=stop_result.success,
        order_id=stop_result.order_id,
        symbol=symbol,
        qty=qty,
        stop_price=stop_price,
        action="replaced" if stale_orders else "submitted",
        error=stop_result.error,
    )


def reconcile_open_position_stops(
    stop_loss_pct: Optional[float] = None,
    positions: Optional[list[PositionSummary]] = None,
) -> list[ProtectiveStopResult]:
    """Repair missing or stale protective stops for the current portfolio."""
    if stop_loss_pct is None:
        stop_loss_pct = settings.STOP_LOSS_PCT

    current_positions = positions if positions is not None else get_open_positions()
    results: list[ProtectiveStopResult] = []

    for position in current_positions:
        if position.qty <= 0 or position.avg_entry_price <= 0:
            continue

        open_orders = get_open_orders(position.symbol)
        if any(_is_non_stop_sell_order(order) for order in open_orders):
            results.append(
                ProtectiveStopResult(
                    success=True,
                    order_id="",
                    symbol=position.symbol,
                    qty=position.qty,
                    stop_price=round(position.avg_entry_price * (1 - stop_loss_pct), 2),
                    action="skipped_pending_exit",
                )
            )
            continue

        results.append(
            ensure_protective_stop(
                symbol=position.symbol,
                qty=position.qty,
                fill_price=position.avg_entry_price,
                stop_loss_pct=stop_loss_pct,
                open_orders=open_orders,
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
    client = _get_trading_client()
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        order: Order = client.submit_order(req)
        mode = "paper" if _is_paper_mode() else "LIVE"
        print(f"[ORDER/{mode}] SELL {qty} {symbol} @ market | order_id={order.id}")
        return OrderResult(
            success=True,
            order_id=str(order.id),
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
    client = _get_trading_client()
    try:
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


def get_open_positions() -> list[PositionSummary]:
    """Return all open positions with unrealized P&L percentages.

    Returns:
        List of PositionSummary objects, one per open position.
        Returns an empty list on any error.
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
        print(f"[ORDER ERROR] get_open_positions: {exc}")
        return []


def get_open_orders(symbol: Optional[str] = None) -> list[Order]:
    """Return pending orders, optionally filtered to a single symbol.

    Args:
        symbol: If provided, only return orders for this ticker.

    Returns:
        List of Alpaca Order objects. Empty list on error.
    """
    client = _get_trading_client()
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None)
        return client.get_orders(req)
    except Exception as exc:  # noqa: BLE001
        print(f"[ORDER ERROR] get_open_orders: {exc}")
        return []


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
