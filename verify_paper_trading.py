"""Supervised durable paper-trading lifecycle verification.

The verifier submits one SPY share through the canonical OrderManager path,
waits for a durable buy fill and protective stop, simulates a monitor/process
restart by clearing only the in-memory workflow registry, reconciles protection
from SQLite state, then closes the test position and proves broker and local
state are flat.

Usage:
    python verify_paper_trading.py --execute

Requires ALPACA_API_KEY, ALPACA_SECRET_KEY, and ALPACA_PAPER=true in .env.
The price path is Alpaca-only; the verifier does not call FMP.
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from core.execution_store import get_execution_store
from core.execution_workflow import (
    ClosedSellCheckpoint,
    EntryExecutionPlan,
    build_exit_client_order_id,
    build_stop_client_order_id,
    clear_workflow_registry,
    get_active_workflow_for_symbol,
    get_latest_workflow_for_symbol,
    get_or_recover_workflow,
    normalize_workflow_id,
    resolve_workflow,
)
from core.notifier import _is_configured as notify_configured
from core.order_execution import (
    _get_trading_client,
    _is_paper_mode,
    _sample_stable_symbol_state,
    cancel_open_orders_verified,
    get_closed_orders,
    get_open_orders,
    get_open_positions,
    reconcile_symbol_after_exit_failure,
)
from core.order_manager import OrderManager
from fill_monitor import FillMonitor

_ET = ZoneInfo("America/New_York")
_ENTRY_TIMEOUT = 60
_EXIT_TIMEOUT = 60
_MONITOR_CONNECT_TIMEOUT = 15
_MONITOR_STOP_TIMEOUT = 10
_POLL_INTERVAL = 0.5
_FINAL_CLEAR_CONFIRMATIONS = 3
_SAFETY_RETRY_INTERVAL = 5.0
_TEST_SYMBOL = "SPY"

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
_WORKING_ORDER_STATUSES = {"new", "partially_filled"}

_SEPARATOR = "=" * 60
_ENTRY_EVENTS = {
    "signal_accepted",
    "plan_built",
    "order_submitted",
    "buy_fill_received",
    "protective_stop_reconciled",
}


@dataclass(frozen=True)
class _RestartRecovery:
    """Result of the deliberate monitor and workflow-cache restart."""

    success: bool
    monitor: FillMonitor
    manager: OrderManager
    error: str = ""


def _check_market_open() -> bool:
    """Return True when the regular market session is currently open."""
    try:
        client = _get_trading_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not check clock: {exc}")
        return False


def main(*, execute: bool = False) -> int:
    if not execute:
        print("[REFUSED] Pass --execute to authorize the one-share paper lifecycle.")
        return 2

    print(_SEPARATOR)
    print(
        "CANSLIM Durable Paper-Trading Verification  "
        f"[{datetime.now(_ET).strftime('%Y-%m-%d %H:%M ET')}]"
    )
    print(_SEPARATOR)

    if not _is_paper_mode():
        print("[ERROR] ALPACA_PAPER must be 'true' for this verification script.")
        return 1

    if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
        print("[ERROR] ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        return 1

    print("\n[1/8] Connecting to Alpaca paper account...")
    try:
        client = _get_trading_client()
        account = client.get_account()
        equity = float(account.equity)
        buying_power = float(account.buying_power)
        print(f"      Equity:        ${equity:,.2f}")
        print(f"      Buying power:  ${buying_power:,.2f}")
        print("      OK: connected")
    except Exception as exc:  # noqa: BLE001
        print(f"      ERROR: connection failed: {exc}")
        return 1

    print(f"\n[2/8] Confirming {_TEST_SYMBOL} broker and local state are clear...")
    if not _preflight_symbol_clear(_TEST_SYMBOL):
        return 1
    print("      OK: no pre-existing SPY position, order, or active workflow")

    print("\n[3/8] Checking market status...")
    if not _check_market_open():
        print("      Market is currently CLOSED.")
        print("      No verification order will be submitted outside regular hours.")
        return 1
    print("      OK: market is open")

    print(f"\n[4/8] Building deterministic one-share {_TEST_SYMBOL} entry plan...")
    from core.data_client import fetch_latest_intraday_price, fetch_ohlcv

    try:
        limit_price = fetch_latest_intraday_price(_TEST_SYMBOL)
        if limit_price is None:
            bars = fetch_ohlcv(_TEST_SYMBOL, period="5d")
            if bars is None or bars.empty:
                print(f"      ERROR: could not fetch {_TEST_SYMBOL} price")
                return 1
            limit_price = float(bars["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        print(f"      ERROR: could not fetch {_TEST_SYMBOL} price: {exc}")
        return 1

    plan = _build_entry_plan(_TEST_SYMBOL, float(limit_price))
    print(
        f"      DAY limit: ${plan.entry_price:.2f} | "
        f"GTC stop after fill: {plan.stop_loss_pct:.1%} below actual fill"
    )

    monitor: FillMonitor | None = None
    manager: OrderManager | None = None
    workflow_id = ""
    mutation_attempted = False
    cleanup_started = False
    cleanup_complete = False

    try:
        print("\n[5/8] Starting fill monitor and submitting through OrderManager...")
        manager = OrderManager(paper=True)
        monitor = FillMonitor()
        monitor.start()
        if not _wait_for_monitor_connection(monitor):
            print("      ERROR: fill monitor did not connect; entry was not submitted")
            return 1
        print("      OK: fill monitor started")

        if notify_configured():
            print("      Email notifications are configured")
        else:
            print("      Email notifications are not configured")

        mutation_attempted = True
        outcome = manager.submit_entry(
            plan,
            signal_payload={
                "symbol": _TEST_SYMBOL,
                "source": "supervised_paper_verification",
            },
            dry_run=False,
        )
        workflow_id = outcome.workflow_id
        if not outcome.success:
            print(f"      ERROR: entry submission failed: {outcome.error}")
            return 1

        print(
            f"      OK: entry submitted order_id={outcome.order_id} "
            f"workflow_id={workflow_id}"
        )

        print(
            f"\n[6/8] Waiting up to {_ENTRY_TIMEOUT}s for durable fill and protection..."
        )
        entry_snapshot = _wait_for_durable_entry(
            workflow_id,
            timeout=_ENTRY_TIMEOUT,
        )
        if entry_snapshot is None:
            print("      ERROR: durable buy-fill/protective-stop state was not proven")
            return 1

        fill = _transition_details(entry_snapshot, "buy_fill_received")
        print(
            f"      OK: bought {float(fill.get('qty', 0.0))} {_TEST_SYMBOL} "
            f"@ ${float(fill.get('fill_price', 0.0)):.2f}"
        )
        print("      OK: entry plan, order, fill, stop, and active ownership are durable")

        print("\n[7/8] Restarting monitor and reloading active workflow from SQLite...")
        restart = _restart_monitor_and_recover(
            _TEST_SYMBOL,
            workflow_id,
            monitor,
            manager,
        )
        monitor = restart.monitor
        manager = restart.manager
        if not restart.success:
            print(f"      ERROR: restart recovery failed: {restart.error}")
            return 1
        print("      OK: same workflow reloaded and protective stop reconciled")

        print("\n[8/8] Closing test position and proving final state is clear...")
        cleanup_started = True
        cleanup_complete = _cleanup_test_symbol(
            _TEST_SYMBOL,
            manager,
            workflow_id,
            monitor=monitor,
        )
        if not cleanup_complete:
            print("      ERROR: cleanup was not durably proven")
            return 1

        if not _stop_monitor_and_wait(monitor):
            print("      ERROR: fill monitor did not stop cleanly")
            return 1
        monitor = None

        print("      OK: sell fill persisted")
        print("      OK: broker has no SPY position or open order")
        print("      OK: SQLite has no active SPY ownership or pending intent")
        print("      OK: fill monitor remained healthy through final proof")
        print(_SEPARATOR)
        print("VERIFICATION PASSED: durable SPY paper lifecycle completed and cleaned up")
        print(_SEPARATOR)
        return 0
    finally:
        if mutation_attempted and not cleanup_complete and manager is not None:
            if not workflow_id:
                latest = get_latest_workflow_for_symbol(_TEST_SYMBOL)
                workflow_id = latest.workflow_id if latest is not None else ""
            if not cleanup_started:
                cleanup_started = True
                print("\n[Emergency cleanup] Entry submission may have reached the broker.")
                try:
                    cleanup_complete = _cleanup_test_symbol(
                        _TEST_SYMBOL,
                        manager,
                        workflow_id,
                        monitor=monitor,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[Emergency cleanup] ERROR: {exc}")
            if not cleanup_complete:
                _hold_until_symbol_safe(_TEST_SYMBOL, manager, workflow_id)
                cleanup_complete = True

        if monitor is not None:
            _stop_monitor_and_wait(monitor)


def _build_entry_plan(symbol: str, limit_price: float) -> EntryExecutionPlan:
    """Build the deterministic one-share plan used by the verifier."""
    stop_price = _calculate_stop_price(limit_price, settings.STOP_LOSS_PCT)
    risk_per_share = round(limit_price - stop_price, 2)
    return EntryExecutionPlan(
        symbol=symbol,
        entry_price=round(limit_price, 2),
        price_source="latest_regular_session_minute_close",
        stop_price=stop_price,
        stop_loss_pct=settings.STOP_LOSS_PCT,
        position_value=round(limit_price, 2),
        risk_amount=risk_per_share,
        risk_per_share=risk_per_share,
        qty=1.0,
        canslim_score=0.0,
        rs_score=0.0,
        is_breakout=False,
        has_volume_surge=False,
    )


def _wait_for_durable_entry(
    workflow_id: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Wait for the full persisted entry, fill, stop, and ownership evidence."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        snapshot = get_execution_store().load_workflow(workflow_id)
        if snapshot is not None:
            transitions = snapshot.get("transitions", [])
            events = {
                str(transition.get("event", ""))
                for transition in transitions
            }
            if events & {
                "exit_order_submitted",
                "exit_order_submit_failed",
                "sell_fill_received",
            }:
                return None
            active = get_execution_store().load_active_position(_TEST_SYMBOL)
            entry_plan = snapshot.get("entry_plan") or {}
            try:
                planned_qty = float(entry_plan.get("qty", 0.0) or 0.0)
                active_qty = float((active or {}).get("qty", 0.0) or 0.0)
            except (TypeError, ValueError):
                planned_qty = active_qty = 0.0
            cumulative_fills: dict[str, tuple[float, int]] = {}
            for index, transition in enumerate(transitions):
                if transition.get("event") != "buy_fill_received":
                    continue
                try:
                    fill_qty = float(
                        transition.get("details", {}).get("qty", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    continue
                if fill_qty <= 0:
                    continue
                broker_order_id = str(
                    transition.get("details", {}).get("broker_order_id", "") or ""
                )
                fill_key = broker_order_id or "__unidentified_entry__"
                previous = cumulative_fills.get(fill_key)
                if previous is None or fill_qty > previous[0] + 0.0001:
                    cumulative_fills[fill_key] = (fill_qty, index)
            total_filled_qty = sum(item[0] for item in cumulative_fills.values())
            full_fill_index = (
                max(item[1] for item in cumulative_fills.values())
                if planned_qty > 0
                and cumulative_fills
                and abs(total_filled_qty - planned_qty) < 0.0001
                else -1
            )
            post_fill_protections = [
                transition
                for transition in transitions[full_fill_index + 1 :]
                if transition.get("event") == "protective_stop_reconciled"
            ] if full_fill_index >= 0 else []
            latest_protection = (
                post_fill_protections[-1] if post_fill_protections else None
            )
            protection_details = (
                latest_protection.get("details", {}) if latest_protection else {}
            )
            durable_stop_order_id = str(
                protection_details.get("stop_order_id", "") or ""
            )
            durable_stop_client_id = str(
                protection_details.get("client_order_id", "") or ""
            )
            durable_stop_ref = any(
                str(reference.get("order_role", "")) == "protective_stop"
                and str(reference.get("broker_order_id", "") or "")
                == durable_stop_order_id
                and str(reference.get("client_order_id", "") or "")
                == durable_stop_client_id
                for reference in snapshot.get("order_refs", [])
            )
            roles = {
                str(reference.get("order_role", ""))
                for reference in snapshot.get("order_refs", [])
            }
            live_stop = (
                _verified_protective_stop_identity(
                    _TEST_SYMBOL,
                    workflow_id,
                    expected_qty=planned_qty,
                )
                if planned_qty > 0
                else None
            )
            complete = (
                _ENTRY_EVENTS.issubset(events)
                and full_fill_index >= 0
                and bool(protection_details.get("success"))
                and bool(durable_stop_order_id and durable_stop_client_id)
                and durable_stop_ref
                and {"entry_order", "protective_stop"}.issubset(roles)
                and active is not None
                and str(active.get("workflow_id", "")) == workflow_id
                and abs(active_qty - planned_qty) < 0.0001
                and live_stop == (durable_stop_order_id, durable_stop_client_id)
            )
            if complete:
                return snapshot

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_POLL_INTERVAL, remaining))


def _wait_for_workflow_events(
    workflow_id: str,
    required_events: set[str],
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Poll the durable store until all required workflow events exist."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        snapshot = get_execution_store().load_workflow(workflow_id)
        if snapshot is not None:
            events = {
                str(transition.get("event", ""))
                for transition in snapshot.get("transitions", [])
            }
            if required_events.issubset(events):
                return snapshot

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_POLL_INTERVAL, remaining))


def _restart_monitor_and_recover(
    symbol: str,
    workflow_id: str,
    monitor: FillMonitor,
    manager: OrderManager,
) -> _RestartRecovery:
    """Restart the fill stream and prove SQLite-backed workflow recovery."""
    if not _stop_monitor_and_wait(monitor):
        return _RestartRecovery(
            success=False,
            monitor=monitor,
            manager=manager,
            error="original fill monitor did not stop",
        )

    clear_workflow_registry()
    recovered = get_active_workflow_for_symbol(symbol)

    restarted_manager = OrderManager(paper=True)
    restarted_monitor = FillMonitor()
    restarted_monitor.start()
    if not _wait_for_monitor_connection(restarted_monitor):
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error="restarted fill monitor did not connect",
        )

    if recovered is None or recovered.workflow_id != workflow_id:
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error="active workflow did not reload from durable storage",
        )

    try:
        position_remains = _reconcile_restart_gap(
            symbol,
            workflow_id,
            restarted_manager,
        )
    except Exception as exc:  # noqa: BLE001
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error=f"could not reconcile restart-gap fills: {exc}",
        )
    if not position_remains:
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error="position closed during restart; sell fill was replayed from REST history",
        )

    try:
        results = restarted_manager.reconcile_startup_stops(symbol)
    except Exception as exc:  # noqa: BLE001
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error=f"startup stop reconciliation raised: {exc}",
        )

    matching = [result for result in results if result.symbol == symbol]
    if len(matching) != 1:
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error=f"expected one {symbol} reconciliation result, got {len(matching)}",
        )

    result = matching[0]
    if not result.success or result.action == "skipped_pending_exit":
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error=result.error or f"unexpected reconciliation action: {result.action}",
        )

    snapshot = get_execution_store().load_workflow(workflow_id)
    startup_protection = any(
        transition.get("event") == "protective_stop_reconciled"
        and bool(transition.get("details", {}).get("success"))
        and str(transition.get("details", {}).get("action", "")).startswith("startup_")
        for transition in (snapshot or {}).get("transitions", [])
    )
    if not startup_protection:
        return _RestartRecovery(
            success=False,
            monitor=restarted_monitor,
            manager=restarted_manager,
            error="startup protection was not appended to the durable workflow",
        )

    return _RestartRecovery(
        success=True,
        monitor=restarted_monitor,
        manager=restarted_manager,
    )


def _closed_sell_replay_chain(
    symbol: str,
    workflow_id: str,
    closed_orders: list[object],
    *,
    required_qty: float,
    recorded_fill_qty_by_id: dict[str, float] | None = None,
    recorded_final_order_ids: set[str] | None = None,
) -> list[object]:
    """Return one terminal sell chain whose unrecorded fills cover exposure."""
    eligible: list[object] = []
    orders_by_id: dict[str, object] = {}
    replay_qty_by_id: dict[str, float] = {}
    filled_qty_by_id: dict[str, float] = {}
    recorded_fill_qty_by_id = recorded_fill_qty_by_id or {}
    recorded_final_order_ids = recorded_final_order_ids or set()
    for order in closed_orders:
        if (
            str(getattr(order, "symbol", "") or "").strip().upper()
            != symbol.strip().upper()
            or _normalize_side(getattr(order, "side", "")) != "sell"
            or not _is_exact_workflow_sell(order, workflow_id)
            or _normalize_side(getattr(order, "status", ""))
            not in _TERMINAL_ORDER_STATUSES
        ):
            continue
        order_id = str(getattr(order, "id", "") or "").strip()
        if not order_id or order_id in orders_by_id:
            raise RuntimeError("closed sell history contains a missing or duplicate order id")
        try:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("closed sell history contains invalid fill data") from exc
        if filled_qty < 0 or (filled_qty > 0 and fill_price <= 0):
            raise RuntimeError("closed sell history contains unsafe fill data")
        eligible.append(order)
        orders_by_id[order_id] = order
        recorded_qty = float(recorded_fill_qty_by_id.get(order_id, 0.0) or 0.0)
        if recorded_qty < 0 or recorded_qty > filled_qty + 0.0001:
            raise RuntimeError("durable sell checkpoint exceeds closed broker fill")
        replay_qty_by_id[order_id] = max(0.0, filled_qty - recorded_qty)
        filled_qty_by_id[order_id] = filled_qty

    if not eligible:
        return []

    parent_by_child: dict[str, str] = {}
    children_by_parent: dict[str, set[str]] = {}

    def add_link(parent_id: str, child_id: str) -> None:
        if parent_id not in orders_by_id or child_id not in orders_by_id:
            raise RuntimeError("closed sell replacement chain is missing a linked order")
        if parent_id == child_id:
            raise RuntimeError("closed sell replacement chain contains a self-link")
        existing_parent = parent_by_child.get(child_id)
        if existing_parent and existing_parent != parent_id:
            raise RuntimeError("closed sell replacement chain has multiple parents")
        parent_by_child[child_id] = parent_id
        children_by_parent.setdefault(parent_id, set()).add(child_id)
        if len(children_by_parent[parent_id]) > 1:
            raise RuntimeError("closed sell replacement chain branches ambiguously")

    for order in eligible:
        order_id = str(getattr(order, "id", "") or "")
        replaces = str(getattr(order, "replaces", "") or "")
        replaced_by = str(getattr(order, "replaced_by", "") or "")
        if replaces:
            add_link(replaces, order_id)
        if replaced_by:
            add_link(order_id, replaced_by)

    for order in eligible:
        order_id = str(getattr(order, "id", "") or "")
        if (
            _normalize_side(getattr(order, "status", "")) == "replaced"
            and not children_by_parent.get(order_id)
        ):
            raise RuntimeError("replaced closed sell has no traversable child")

    roots = [
        order_id for order_id in orders_by_id if order_id not in parent_by_child
    ]
    if not roots:
        raise RuntimeError("closed sell replacement chain contains a cycle")

    visited: set[str] = set()
    covering_chains: list[list[object]] = []
    for root_id in roots:
        chain: list[object] = []
        order_id = root_id
        while order_id:
            if order_id in visited:
                raise RuntimeError("closed sell replacement chain is cyclic or overlapping")
            visited.add(order_id)
            chain.append(orders_by_id[order_id])
            children = children_by_parent.get(order_id, set())
            order_id = next(iter(children)) if children else ""
        unrecorded_coverage = sum(
            replay_qty_by_id[str(getattr(order, "id", "") or "")]
            for order in chain
        )
        total_coverage = sum(
            filled_qty_by_id[str(getattr(order, "id", "") or "")]
            for order in chain
        )
        leaf_id = str(getattr(chain[-1], "id", "") or "")
        repairs_recorded_final = (
            leaf_id in recorded_final_order_ids
            and total_coverage + 0.0001 >= required_qty
        )
        if unrecorded_coverage + 0.0001 >= required_qty or repairs_recorded_final:
            covering_chains.append(chain)

    if visited != set(orders_by_id):
        raise RuntimeError("closed sell replacement history is incomplete")
    if len(covering_chains) != 1:
        return []
    return [
        order
        for order in covering_chains[0]
        if filled_qty_by_id[str(getattr(order, "id", "") or "")] > 0
    ]


def _durable_fill_checkpoints_by_order(
    snapshot: dict[str, Any],
    *,
    events: set[str],
) -> dict[str, tuple[float, float, str]]:
    """Return the latest cumulative durable fill for each exact broker order."""
    checkpoints: dict[str, tuple[float, float, str]] = {}
    for transition in snapshot.get("transitions", []):
        if str(transition.get("event", "")) not in events:
            continue
        details = transition.get("details", {})
        broker_order_id = str(details.get("broker_order_id", "") or "").strip()
        client_order_id = str(details.get("client_order_id", "") or "").strip()
        try:
            qty = float(details.get("qty", 0.0) or 0.0)
            fill_price = float(details.get("fill_price", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("durable fill checkpoint contains invalid data") from exc
        if (
            not broker_order_id
            or not math.isfinite(qty)
            or not math.isfinite(fill_price)
            or qty <= 0
            or fill_price <= 0
        ):
            raise RuntimeError("durable fill checkpoint contains unsafe data")
        previous = checkpoints.get(broker_order_id)
        if previous is None or qty >= previous[0] - 0.0001:
            checkpoints[broker_order_id] = (qty, fill_price, client_order_id)
    return checkpoints


def _require_exact_pending_exit_intent(
    pending_intents: list[dict[str, Any]],
    *,
    workflow_id: str,
    client_order_id: str,
) -> None:
    """Fail closed unless one pending intent is the exact workflow exit."""
    exact = [
        intent
        for intent in pending_intents
        if str(intent.get("workflow_id", "") or "") == workflow_id
        and str(intent.get("event", "") or "") == "exit_submission_intent"
        and str(
            intent.get("details", {}).get("client_order_id", "") or ""
        ).strip()
        == client_order_id
    ]
    if len(pending_intents) != 1 or len(exact) != 1:
        raise RuntimeError(
            "broker is flat but unresolved submission intent identity is ambiguous"
        )


def _trusted_recorded_sell_coverage(
    symbol: str,
    workflow_id: str,
    snapshot: dict[str, Any],
    closed_orders: list[object],
    *,
    required_qty: float,
    sell_checkpoints: dict[str, tuple[float, float, str]],
    exit_client_order_id: str,
) -> str:
    """Prove exact durable sell coverage across complete closed-order chains."""
    all_orders_by_id: dict[str, object] = {}
    exact_orders_by_id: dict[str, object] = {}
    normalized_symbol = symbol.strip().upper()
    for order in closed_orders:
        order_id = str(getattr(order, "id", "") or "").strip()
        order_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        is_symbol_sell = (
            order_symbol == normalized_symbol
            and _normalize_side(getattr(order, "side", "")) == "sell"
        )
        is_exact_sell = is_symbol_sell and _is_exact_workflow_sell(order, workflow_id)
        if is_exact_sell and not order_id:
            raise RuntimeError("closed sell history contains a missing order id")
        if not order_id:
            continue
        if order_id in all_orders_by_id:
            raise RuntimeError("closed sell history contains a duplicate order id")
        all_orders_by_id[order_id] = order
        if not is_exact_sell:
            continue
        if _normalize_side(getattr(order, "status", "")) not in (
            _TERMINAL_ORDER_STATUSES
        ):
            raise RuntimeError("closed sell replacement chain is not terminal")
        exact_orders_by_id[order_id] = order

    parent_by_child: dict[str, str] = {}
    child_by_parent: dict[str, str] = {}

    def add_link(parent_id: str, child_id: str) -> None:
        if parent_id not in exact_orders_by_id or child_id not in exact_orders_by_id:
            raise RuntimeError("closed sell replacement chain is incomplete")
        if parent_id == child_id:
            raise RuntimeError("closed sell replacement chain contains a self-link")
        existing_parent = parent_by_child.get(child_id)
        if existing_parent and existing_parent != parent_id:
            raise RuntimeError("closed sell replacement chain has multiple parents")
        existing_child = child_by_parent.get(parent_id)
        if existing_child and existing_child != child_id:
            raise RuntimeError("closed sell replacement chain branches ambiguously")
        parent_by_child[child_id] = parent_id
        child_by_parent[parent_id] = child_id

    for order_id, order in exact_orders_by_id.items():
        replaces = str(getattr(order, "replaces", "") or "").strip()
        replaced_by = str(getattr(order, "replaced_by", "") or "").strip()
        if replaces:
            add_link(replaces, order_id)
        if replaced_by:
            add_link(order_id, replaced_by)

    for order_id, order in exact_orders_by_id.items():
        replaces = str(getattr(order, "replaces", "") or "").strip()
        replaced_by = str(getattr(order, "replaced_by", "") or "").strip()
        if replaces:
            parent = exact_orders_by_id.get(replaces)
            if str(getattr(parent, "replaced_by", "") or "").strip() != order_id:
                raise RuntimeError("closed sell replacement chain is incomplete")
        if replaced_by:
            child = exact_orders_by_id.get(replaced_by)
            if str(getattr(child, "replaces", "") or "").strip() != order_id:
                raise RuntimeError("closed sell replacement chain is incomplete")
        if (
            _normalize_side(getattr(order, "status", "")) == "replaced"
            and order_id not in child_by_parent
        ):
            raise RuntimeError("replaced closed sell has no traversable child")

    roots = [
        order_id for order_id in exact_orders_by_id if order_id not in parent_by_child
    ]
    if exact_orders_by_id and not roots:
        raise RuntimeError("closed sell replacement chain contains a cycle")
    visited: set[str] = set()
    for root_id in roots:
        order_id = root_id
        while order_id:
            if order_id in visited:
                raise RuntimeError("closed sell replacement chain is cyclic or overlapping")
            visited.add(order_id)
            order_id = child_by_parent.get(order_id, "")
    if visited != set(exact_orders_by_id):
        raise RuntimeError("closed sell replacement history is incomplete")
    for broker_order_id, order in exact_orders_by_id.items():
        try:
            broker_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
            broker_price = float(getattr(order, "filled_avg_price", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("closed sell history contains invalid fill data") from exc
        if (
            not math.isfinite(broker_qty)
            or not math.isfinite(broker_price)
            or broker_qty < 0
            or (broker_qty > 0 and broker_price <= 0)
        ):
            raise RuntimeError("closed sell history contains unsafe fill data")
        if broker_qty > 0.0001 and broker_order_id not in sell_checkpoints:
            raise RuntimeError("pending exit lacks exact trusted sell coverage")

    store = get_execution_store()
    sell_references = [
        reference
        for reference in snapshot.get("order_refs", [])
        if str(reference.get("order_role", "") or "") == "sell_fill"
    ]
    transitions = snapshot.get("transitions", [])
    intent_indexes = [
        index
        for index, transition in enumerate(transitions)
        if transition.get("event") == "exit_submission_intent"
        and str(
            transition.get("details", {}).get("client_order_id", "") or ""
        ).strip()
        == exit_client_order_id
    ]
    if not intent_indexes:
        raise RuntimeError(
            "pending exit lacks exact durable final sell checkpoint/reference coverage"
        )
    intent_index = intent_indexes[-1]
    final_order_ids = {
        str(transition.get("details", {}).get("broker_order_id", "") or "").strip()
        for transition in transitions[intent_index + 1 :]
        if transition.get("event") == "sell_fill_received"
        and str(
            transition.get("details", {}).get("client_order_id", "") or ""
        ).strip()
        == exit_client_order_id
    }
    final_order_ids.discard("")
    if len(final_order_ids) != 1:
        raise RuntimeError(
            "pending exit lacks exact durable final sell checkpoint/reference coverage"
        )
    final_order_id = next(iter(final_order_ids))

    trusted_coverage = 0.0
    for broker_order_id, checkpoint in sell_checkpoints.items():
        order = exact_orders_by_id.get(broker_order_id)
        checkpoint_qty, checkpoint_price, checkpoint_client_id = checkpoint
        exact_references = [
            reference
            for reference in sell_references
            if str(reference.get("broker_order_id", "") or "") == broker_order_id
            and str(reference.get("client_order_id", "") or "")
            == checkpoint_client_id
        ]
        broker_references = [
            reference
            for reference in sell_references
            if str(reference.get("broker_order_id", "") or "") == broker_order_id
        ]
        invalid_identity = bool(
            order is None
            or len(exact_references) != 1
            or len(broker_references) != 1
            or store.find_workflow_ids_by_broker_order_id(broker_order_id)
            != {workflow_id}
            or store.find_workflow_ids_by_client_order_id(checkpoint_client_id)
            != {workflow_id}
            or str(getattr(order, "client_order_id", "") or "").strip()
            != checkpoint_client_id
        )
        if invalid_identity:
            if broker_order_id == final_order_id:
                raise RuntimeError(
                    "pending exit lacks exact durable final sell "
                    "checkpoint/reference coverage"
                )
            raise RuntimeError("pending exit lacks exact trusted sell coverage")
        try:
            broker_qty = float(getattr(order, "filled_qty", 0.0) or 0.0)
            broker_price = float(getattr(order, "filled_avg_price", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("closed sell history contains invalid fill data") from exc
        if (
            broker_qty <= 0
            or broker_price <= 0
            or abs(broker_qty - checkpoint_qty) >= 0.0001
            or abs(broker_price - checkpoint_price) > 0.01
        ):
            raise RuntimeError("pending exit lacks exact trusted sell coverage")
        trusted_coverage += checkpoint_qty

    if abs(trusted_coverage - required_qty) >= 0.0001:
        raise RuntimeError("pending exit lacks exact trusted sell coverage")

    final_order = exact_orders_by_id.get(final_order_id)
    final_checkpoint = sell_checkpoints.get(final_order_id)
    has_final_reference = any(
        str(reference.get("broker_order_id", "") or "") == final_order_id
        and str(reference.get("client_order_id", "") or "") == exit_client_order_id
        for reference in sell_references
    )
    if (
        final_order is None
        or final_checkpoint is None
        or not has_final_reference
        or str(getattr(final_order, "client_order_id", "") or "").strip()
        != exit_client_order_id
        or _normalize_side(getattr(final_order, "type", "")) != "market"
        or final_checkpoint[2] != exit_client_order_id
    ):
        raise RuntimeError(
            "pending exit lacks exact durable final sell checkpoint/reference coverage"
        )
    return final_order_id


def _repair_flat_pending_exit_intent(
    symbol: str,
    workflow_id: str,
    pending_intents: list[dict[str, Any]],
) -> bool:
    """Resolve only the final-fill/intent marker crash window.

    The caller has already proved one stable broker-flat/no-orders sample.
    This path additionally requires exact durable fill coverage, the final
    sell reference, matching closed broker history, and a second flat sample
    immediately before appending the idempotent resolution marker.
    """
    exit_client_order_id = build_exit_client_order_id(workflow_id)
    _require_exact_pending_exit_intent(
        pending_intents,
        workflow_id=workflow_id,
        client_order_id=exit_client_order_id,
    )

    store = get_execution_store()
    snapshot = store.load_workflow(workflow_id)
    if snapshot is None or str(snapshot.get("symbol", "") or "") != symbol:
        raise RuntimeError("pending exit workflow is missing or belongs to another symbol")

    buy_checkpoints = _durable_fill_checkpoints_by_order(
        snapshot,
        events={"buy_fill_received"},
    )
    sell_checkpoints = _durable_fill_checkpoints_by_order(
        snapshot,
        events={"sell_partial_fill_received", "sell_fill_received"},
    )
    required_qty = sum(checkpoint[0] for checkpoint in buy_checkpoints.values())
    if required_qty <= 0:
        raise RuntimeError(
            "pending exit lacks durable entry-fill coverage"
        )

    closed_orders = get_closed_orders(symbol, limit=50, raise_on_error=True)
    final_order_id = _trusted_recorded_sell_coverage(
        symbol,
        workflow_id,
        snapshot,
        closed_orders,
        required_qty=required_qty,
        sell_checkpoints=sell_checkpoints,
        exit_client_order_id=exit_client_order_id,
    )

    final_position, final_open_orders = _sample_stable_symbol_state(symbol)
    if final_position is not None:
        return True
    if final_open_orders:
        raise RuntimeError(
            "broker-flat pending-exit recovery gained working symbol orders"
        )
    if store.load_active_position(symbol) is not None:
        return True

    current_pending = store.load_pending_submission_intents(symbol=symbol)
    if not current_pending:
        return False
    _require_exact_pending_exit_intent(
        current_pending,
        workflow_id=workflow_id,
        client_order_id=exit_client_order_id,
    )
    workflow = resolve_workflow(
        symbol=symbol,
        workflow_id=workflow_id,
        client_order_id=exit_client_order_id,
        broker_order_id=final_order_id,
    )
    if workflow is None or workflow.workflow_id != workflow_id:
        raise RuntimeError("final sell identity does not resolve to the pending workflow")
    workflow.mark_submission_intent_resolved(
        role="exit",
        client_order_id=exit_client_order_id,
        outcome="final_fill_checkpoint_recovered",
        broker_order_id=final_order_id,
    )
    if store.load_pending_submission_intents(symbol=symbol):
        raise RuntimeError("pending exit intent remained after exact final-fill recovery")
    return store.load_active_position(symbol) is not None


def _reconcile_restart_gap(
    symbol: str,
    workflow_id: str,
    manager: OrderManager,
) -> bool:
    """Replay a sell fill missed while the trade-update stream was restarting.

    Returns True when the broker position still exists, or False after proving
    the broker is flat and replaying the matching filled sell into SQLite.
    """
    position, open_orders = _sample_stable_symbol_state(symbol)
    if position is not None:
        return True
    if open_orders:
        raise RuntimeError("broker-flat restart recovery still has working symbol orders")

    store = get_execution_store()
    local_active = store.load_active_position(symbol)
    pending_intents = store.load_pending_submission_intents(symbol=symbol)
    if local_active is None:
        if not pending_intents:
            return False
        return _repair_flat_pending_exit_intent(
            symbol,
            workflow_id,
            pending_intents,
        )
    if str(local_active.get("workflow_id", "") or "") != workflow_id:
        raise RuntimeError("broker/local workflow ownership does not match restart recovery")
    try:
        active_qty = float(local_active.get("qty", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("durable active quantity is invalid") from exc

    closed_orders = get_closed_orders(symbol, limit=50, raise_on_error=True)
    snapshot = store.load_workflow(workflow_id)
    if snapshot is None:
        raise RuntimeError("durable workflow is missing during restart recovery")
    recorded_sell_qty_by_id: dict[str, float] = {}
    latest_recorded_final_order_id = ""
    latest_recorded_final_timestamp = ""
    for transition in snapshot.get("transitions", []):
        if transition.get("event") not in {
            "sell_partial_fill_received",
            "sell_fill_received",
        }:
            continue
        details = transition.get("details", {})
        order_id = str(details.get("broker_order_id", "") or "").strip()
        if not order_id:
            continue
        try:
            recorded_qty = float(details.get("qty", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("durable sell checkpoint quantity is invalid") from exc
        if recorded_qty < 0:
            raise RuntimeError("durable sell checkpoint quantity is invalid")
        recorded_sell_qty_by_id[order_id] = max(
            recorded_sell_qty_by_id.get(order_id, 0.0),
            recorded_qty,
        )
        if transition.get("event") == "sell_fill_received":
            latest_recorded_final_order_id = order_id
            latest_recorded_final_timestamp = str(
                transition.get("timestamp_utc", "") or ""
            )
    active_updated_at = str(local_active.get("updated_at_utc", "") or "")
    recorded_final_order_ids = set()
    if (
        latest_recorded_final_order_id
        and latest_recorded_final_timestamp
        and (not active_updated_at or active_updated_at <= latest_recorded_final_timestamp)
    ):
        recorded_final_order_ids.add(latest_recorded_final_order_id)
    replay_orders = _closed_sell_replay_chain(
        symbol,
        workflow_id,
        closed_orders,
        required_qty=active_qty,
        recorded_fill_qty_by_id=recorded_sell_qty_by_id,
        recorded_final_order_ids=recorded_final_order_ids,
    )
    if not replay_orders:
        raise RuntimeError(
            "broker is flat but no matching filled sell exists in recent order history"
        )

    resolved_workflow = None
    replay_checkpoints: list[ClosedSellCheckpoint] = []
    exit_client_order_id = build_exit_client_order_id(workflow_id)
    replayed_exit_order_id = ""
    for filled_order in replay_orders:
        broker_order_id = str(getattr(filled_order, "id", "") or "").strip()
        client_order_id = str(
            getattr(filled_order, "client_order_id", "") or ""
        ).strip()
        resolved = resolve_workflow(
            symbol=symbol,
            workflow_id=workflow_id,
            client_order_id=client_order_id,
        )
        if not broker_order_id or resolved is None or resolved.workflow_id != workflow_id:
            raise RuntimeError("closed sell identity does not match the durable workflow")

        order_type = _normalize_side(getattr(filled_order, "type", ""))
        order_role = (
            "exit_order"
            if client_order_id == build_exit_client_order_id(workflow_id)
            and order_type == "market"
            else "protective_stop"
        )
        durable_broker_owners = store.find_workflow_ids_by_broker_order_id(
            broker_order_id
        )
        if durable_broker_owners not in (set(), {workflow_id}):
            raise RuntimeError("closed sell broker id belongs to another workflow")
        # Alpaca can accept the canonical client id immediately before a
        # process crash prevents the broker id/transition from reaching
        # SQLite.  Broker-flat, full-chain coverage is the narrow trust anchor
        # for atomically claiming each missing reference.
        resolved.repair_order_reference(
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            order_role=order_role,
        )
        if (
            resolved_workflow is not None
            and resolved.workflow_id != resolved_workflow.workflow_id
        ):
            raise RuntimeError("closed sell replay resolved to multiple workflow objects")
        if resolved_workflow is None:
            resolved_workflow = resolved
        try:
            filled_qty = float(getattr(filled_order, "filled_qty", 0) or 0)
            fill_price = float(getattr(filled_order, "filled_avg_price", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("closed sell replay contains invalid fill data") from exc
        replay_checkpoints.append(
            ClosedSellCheckpoint(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                qty=filled_qty,
                fill_price=fill_price,
            )
        )
        if client_order_id == exit_client_order_id:
            replayed_exit_order_id = broker_order_id

    if resolved_workflow is None:
        raise RuntimeError("closed sell replay did not resolve a durable workflow")
    final_position, final_open_orders = _sample_stable_symbol_state(symbol)
    if final_position is not None:
        return True
    if final_open_orders:
        raise RuntimeError(
            "broker-flat restart recovery gained working orders before durable replay"
        )
    final_order_type = _normalize_side(getattr(replay_orders[-1], "type", ""))
    resolved_workflow.replay_closed_sell_chain(
        replay_checkpoints,
        exit_reason=(
            "stop-loss triggered"
            if final_order_type in {"stop", "stop_limit"}
            else "exit order filled"
        ),
        expected_active_position=local_active,
    )
    if replayed_exit_order_id and any(
        str(intent.get("details", {}).get("client_order_id", "") or "")
        == exit_client_order_id
        for intent in store.load_pending_submission_intents(symbol=symbol)
    ):
        resolved_workflow.mark_submission_intent_resolved(
            role="exit",
            client_order_id=exit_client_order_id,
            outcome="restart_gap_fill_replayed",
            broker_order_id=replayed_exit_order_id,
        )
    if get_execution_store().load_active_position(symbol) is not None:
        raise RuntimeError("sell-fill replay did not clear durable active ownership")
    return False


def _cleanup_test_symbol(
    symbol: str,
    manager: OrderManager,
    workflow_id: str,
    *,
    monitor: FillMonitor | None,
) -> bool:
    """Prove cleanup fills, durable clear state, and monitor health."""
    try:
        result = manager.submit_exit(
            symbol,
            exit_reason="supervised verification cleanup",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Cleanup] ERROR: exit submission raised: {exc}")
        return False

    if not workflow_id:
        latest = get_latest_workflow_for_symbol(symbol)
        workflow_id = latest.workflow_id if latest is not None else ""

    if not result.success:
        print(f"[Cleanup] ERROR: exit submission failed: {result.error}")
        if workflow_id:
            try:
                position_remains = _reconcile_restart_gap(
                    symbol,
                    workflow_id,
                    manager,
                )
                if not position_remains:
                    return _wait_for_symbol_clear(
                        symbol,
                        timeout=1.0,
                        workflow_id=workflow_id,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"[Cleanup] Could not replay an ambiguous exit: {exc}")
        _ensure_symbol_safe(symbol, manager, workflow_id)
        return False

    if result.qty > 0:
        print(f"[Cleanup] Exit submitted for {result.qty} {symbol}; waiting for sell fill...")
        sell_snapshot = _wait_for_workflow_events(
            workflow_id,
            {"sell_fill_received"},
            timeout=_EXIT_TIMEOUT,
        )
        if sell_snapshot is None:
            try:
                position_remains = _reconcile_restart_gap(
                    symbol,
                    workflow_id,
                    manager,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[Cleanup] ERROR: sell fill was not persisted or replayable: {exc}")
                return False
            if position_remains:
                print("[Cleanup] ERROR: exit did not flatten the broker position")
                return False
    else:
        print(f"[Cleanup] No open {symbol} position remained after order cancellation")

    final_clear = _wait_for_symbol_clear(
        symbol,
        timeout=_EXIT_TIMEOUT,
        workflow_id=workflow_id,
    )
    if not final_clear and workflow_id:
        try:
            position_remains = _reconcile_restart_gap(
                symbol,
                workflow_id,
                manager,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Cleanup] ERROR: final-state recovery failed: {exc}")
            return False
        if not position_remains:
            final_clear = _wait_for_symbol_clear(
                symbol,
                timeout=2.0,
                workflow_id=workflow_id,
            )
    if not final_clear:
        print(f"[Cleanup] ERROR: {symbol} broker or local state is not clear")
        return False

    if monitor is None or not monitor.is_connected():
        print("[Cleanup] ERROR: fill monitor health was not proven at final clear")
        return False

    return True


def _ensure_symbol_safe(
    symbol: str,
    manager: OrderManager,
    workflow_id: str,
) -> bool:
    """Fail-safe a non-final cleanup into either flat or stop-protected state."""
    if not workflow_id:
        latest = get_latest_workflow_for_symbol(symbol)
        workflow_id = latest.workflow_id if latest is not None else ""

    if workflow_id:
        try:
            if not _reconcile_restart_gap(symbol, workflow_id, manager):
                return _wait_for_symbol_clear(
                    symbol,
                    timeout=1.0,
                    workflow_id=workflow_id,
                )
        except Exception:
            pass

    try:
        safety = reconcile_symbol_after_exit_failure(
            symbol,
            workflow_id=workflow_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Emergency cleanup] Could not inspect/restore protection: {exc}")
        return False

    if safety.action == "flat":
        if workflow_id:
            try:
                _reconcile_restart_gap(symbol, workflow_id, manager)
            except Exception as exc:  # noqa: BLE001
                print(f"[Emergency cleanup] Flat broker state could not be replayed: {exc}")
                return False
        return _wait_for_symbol_clear(
            symbol,
            timeout=1.0,
            workflow_id=workflow_id,
        )

    if safety.action == "pending_exit":
        print("[Emergency cleanup] Exit is pending; waiting for final broker/local clear...")
        if _wait_for_symbol_clear(
            symbol,
            timeout=_EXIT_TIMEOUT,
            workflow_id=workflow_id,
        ):
            return True
        if workflow_id:
            try:
                if not _reconcile_restart_gap(symbol, workflow_id, manager):
                    return _wait_for_symbol_clear(
                        symbol,
                        timeout=1.0,
                        workflow_id=workflow_id,
                    )
            except Exception:
                pass
        try:
            cancel_open_orders_verified(symbol)
            safety = reconcile_symbol_after_exit_failure(
                symbol,
                workflow_id=workflow_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency cleanup] Could not cancel stuck exit/restore stop: {exc}")
            return False

    if safety.action in {"pending_buy", "unsafe_orders", "flat_with_open_orders"}:
        try:
            cancel_open_orders_verified(symbol)
            safety = reconcile_symbol_after_exit_failure(
                symbol,
                workflow_id=workflow_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency cleanup] Could not clear unsafe orders: {exc}")
            return False

    if not safety.success or safety.action in {"flat", "pending_exit"}:
        print(
            "[Emergency cleanup] Position safety could not be proven: "
            f"action={safety.action} error={safety.error}"
        )
        return False

    try:
        positions = get_open_positions(raise_on_error=True)
        position = next(
            (item for item in positions if str(item.symbol) == symbol),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Emergency cleanup] Could not verify durable ownership: {exc}")
        return False
    if position is None or position.qty <= 0 or position.avg_entry_price <= 0:
        return False

    workflow = get_active_workflow_for_symbol(symbol)
    if workflow is not None and workflow.workflow_id != workflow_id:
        print("[Emergency cleanup] Durable position belongs to a different workflow")
        return False
    if workflow is None:
        workflow = get_or_recover_workflow(workflow_id, symbol=symbol)
        workflow.repair_buy_fill_storage(
            qty=position.qty,
            fill_price=position.avg_entry_price,
            broker_order_id="",
            restore_active=True,
        )

    active = get_execution_store().load_active_position(symbol)
    if (
        active is None
        or str(active.get("workflow_id", "") or "") != workflow_id
        or abs(float(active.get("qty", 0.0) or 0.0) - position.qty) >= 0.0001
    ):
        print("[Emergency cleanup] Durable active ownership could not be verified")
        return False

    workflow.mark_protective_stop(
        success=safety.success,
        stop_order_id=safety.order_id,
        stop_price=safety.stop_price,
        action=f"emergency_{safety.action}",
        error=safety.error,
        stop_client_order_id=safety.client_order_id,
    )

    if not _has_verified_protective_stop(
        symbol,
        workflow_id,
        expected_qty=position.qty,
    ):
        print("[Emergency cleanup] Broker stop protection could not be verified")
        return False
    print("[Emergency cleanup] Position remains open but verified stop protection is active")
    return True


def _hold_until_symbol_safe(
    symbol: str,
    manager: OrderManager,
    workflow_id: str,
) -> None:
    """Keep recovery and the fill monitor alive until broker safety is proven."""
    while True:
        try:
            if _ensure_symbol_safe(symbol, manager, workflow_id):
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[Emergency cleanup] Safety inspection raised: {exc}")
        print(
            "[Emergency cleanup] CRITICAL: safety is still unproven; "
            "keeping the monitor alive and retrying."
        )
        time.sleep(_SAFETY_RETRY_INTERVAL)


def _verified_protective_stop_identity(
    symbol: str,
    workflow_id: str,
    *,
    expected_qty: float | None = None,
) -> tuple[str, str] | None:
    """Return the exact live stop identity only after a full safety proof."""
    try:
        position, orders = _sample_stable_symbol_state(symbol)
    except Exception:
        return None
    if (
        position is None
        or str(position.symbol) != symbol
        or position.qty <= 0
        or position.avg_entry_price <= 0
    ):
        return None
    if expected_qty is not None and abs(position.qty - expected_qty) >= 0.0001:
        return None
    if len(orders) != 1:
        return None
    stop = orders[0]
    stop_order_id = str(getattr(stop, "id", "") or "")
    stop_client_order_id = str(getattr(stop, "client_order_id", "") or "")
    stop_role_prefix = f"{build_stop_client_order_id(workflow_id)}-"
    try:
        remaining_qty = float(getattr(stop, "qty", 0) or 0) - float(
            getattr(stop, "filled_qty", 0) or 0
        )
        expected_stop_price = round(
            float(getattr(position, "avg_entry_price", 0) or 0)
            * (1 - settings.STOP_LOSS_PCT),
            2,
        )
        actual_stop_price = float(getattr(stop, "stop_price", 0) or 0)
    except (TypeError, ValueError):
        return None
    exact = (
        bool(stop_order_id and stop_client_order_id)
        and _normalize_side(getattr(stop, "side", "")) == "sell"
        and _normalize_side(getattr(stop, "type", "")) == "stop"
        and _normalize_side(getattr(stop, "time_in_force", "")) == "gtc"
        and _normalize_side(getattr(stop, "status", "")) in _WORKING_ORDER_STATUSES
        and normalize_workflow_id(stop_client_order_id) == workflow_id
        and (
            stop_client_order_id == build_stop_client_order_id(workflow_id)
            or stop_client_order_id.startswith(stop_role_prefix)
        )
        and abs(remaining_qty - position.qty) < 0.0001
        and expected_stop_price > 0
        and abs(actual_stop_price - expected_stop_price) <= 0.01
    )
    if not exact:
        return None
    return stop_order_id, stop_client_order_id


def _has_verified_protective_stop(
    symbol: str,
    workflow_id: str,
    *,
    expected_qty: float | None = None,
) -> bool:
    """Return True only for one workflow-linked stop protecting the full long."""
    return _verified_protective_stop_identity(
        symbol,
        workflow_id,
        expected_qty=expected_qty,
    ) is not None


def _wait_for_symbol_clear(
    symbol: str,
    *,
    timeout: float,
    workflow_id: str = "",
) -> bool:
    """Require stable broker/local/intent clear state and terminal entry coverage."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_error = ""
    clear_confirmations = 0
    entry_is_terminal = not workflow_id
    while True:
        try:
            positions = get_open_positions(raise_on_error=True)
            orders = get_open_orders(symbol, raise_on_error=True)
            store = get_execution_store()
            local_active = store.load_active_position(symbol)
            pending_intents = store.load_pending_submission_intents(symbol=symbol)
            has_position = any(str(position.symbol) == symbol for position in positions)
            is_clear = (
                not has_position
                and not orders
                and local_active is None
                and not pending_intents
            )
            if is_clear and not entry_is_terminal:
                entry_is_terminal = _entry_order_is_terminal(symbol, workflow_id)
            if is_clear and entry_is_terminal:
                clear_confirmations += 1
                if clear_confirmations >= _FINAL_CLEAR_CONFIRMATIONS:
                    return True
                last_error = (
                    "clear state has not remained stable long enough "
                    f"({clear_confirmations}/{_FINAL_CLEAR_CONFIRMATIONS})"
                )
            else:
                clear_confirmations = 0
                last_error = (
                    f"position={has_position}, open_orders={len(orders)}, "
                    f"local_active={local_active is not None}, "
                    f"pending_intents={len(pending_intents)}, "
                    f"entry_terminal={entry_is_terminal}"
                )
        except Exception as exc:  # noqa: BLE001
            clear_confirmations = 0
            last_error = str(exc)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if last_error:
                print(f"[Cleanup] Final-clear timeout: {last_error}")
            return False
        time.sleep(min(_POLL_INTERVAL, remaining))


def _entry_order_is_terminal(symbol: str, workflow_id: str) -> bool:
    """Confirm terminal entry state cannot leave unaccounted filled exposure."""
    if not workflow_id:
        return True
    closed_orders = get_closed_orders(symbol, limit=50, raise_on_error=True)
    buy_orders = [
        order
        for order in closed_orders
        if _normalize_side(getattr(order, "side", "")) == "buy"
    ]
    roots = [
        order
        for order in buy_orders
        if str(getattr(order, "client_order_id", "") or "") == workflow_id
    ]
    if len(roots) != 1:
        return False

    orders_by_id = {
        str(getattr(order, "id", "") or ""): order
        for order in buy_orders
        if str(getattr(order, "id", "") or "")
    }
    children_by_parent: dict[str, list[object]] = {}
    for order in buy_orders:
        parent_id = str(getattr(order, "replaces", "") or "")
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(order)

    chain: list[object] = []
    pending = [roots[0]]
    visited: set[str] = set()
    while pending:
        order = pending.pop()
        order_id = str(getattr(order, "id", "") or "")
        visit_key = order_id or f"object:{id(order)}"
        if visit_key in visited:
            continue
        visited.add(visit_key)

        status = _normalize_side(getattr(order, "status", ""))
        if status not in _TERMINAL_ORDER_STATUSES:
            return False
        chain.append(order)

        linked_orders: list[object] = []
        for linked_id in (
            str(getattr(order, "replaced_by", "") or ""),
            str(getattr(order, "replaces", "") or ""),
        ):
            if not linked_id:
                continue
            linked = orders_by_id.get(linked_id)
            if linked is None:
                return False
            linked_orders.append(linked)
        if order_id:
            linked_orders.extend(children_by_parent.get(order_id, []))

        replacement_children = {
            str(getattr(linked, "id", "") or f"object:{id(linked)}")
            for linked in linked_orders
            if (
                str(getattr(order, "replaced_by", "") or "")
                == str(getattr(linked, "id", "") or "")
                or str(getattr(linked, "replaces", "") or "") == order_id
            )
        }
        if status == "replaced" and not replacement_children:
            return False
        pending.extend(linked_orders)

    sold_qty = 0.0
    for order in closed_orders:
        if (
            _normalize_side(getattr(order, "side", "")) != "sell"
            or not _is_exact_workflow_sell(order, workflow_id)
            or _normalize_side(getattr(order, "status", ""))
            not in _TERMINAL_ORDER_STATUSES
        ):
            continue
        try:
            order_filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        except (TypeError, ValueError):
            return False
        if order_filled_qty < 0:
            return False
        if order_filled_qty > 0:
            sold_qty += order_filled_qty

    entry_filled_qty = 0.0
    saw_zero_fill_terminal_leaf = False
    for entry in chain:
        status = _normalize_side(getattr(entry, "status", ""))
        try:
            order_filled_qty = float(getattr(entry, "filled_qty", 0) or 0)
        except (TypeError, ValueError):
            return False
        if order_filled_qty < 0:
            return False
        if order_filled_qty <= 0.0001:
            if status == "filled":
                return False
            if status != "replaced":
                saw_zero_fill_terminal_leaf = True
            continue
        entry_filled_qty += order_filled_qty

    if entry_filled_qty <= 0.0001:
        return saw_zero_fill_terminal_leaf
    return abs(sold_qty - entry_filled_qty) < 0.0001


def _stop_monitor_and_wait(monitor: FillMonitor) -> bool:
    """Request monitor shutdown and wait for its background thread to exit."""
    monitor.stop()
    deadline = time.monotonic() + _MONITOR_STOP_TIMEOUT
    while monitor.is_running():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def _wait_for_monitor_connection(monitor: FillMonitor) -> bool:
    """Wait until the Alpaca stream has authenticated and subscribed."""
    deadline = time.monotonic() + _MONITOR_CONNECT_TIMEOUT
    while True:
        if monitor.is_connected():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def _calculate_stop_price(reference_price: float, stop_loss_pct: float) -> float:
    """Return the displayed protective-stop price for a reference entry."""
    return round(reference_price * (1 - stop_loss_pct), 2)


def _normalize_side(value: object) -> str:
    """Normalize Alpaca enum-like order sides."""
    return str(value).split(".")[-1].strip().lower()


def _is_exact_workflow_sell(order: object, workflow_id: str) -> bool:
    """Accept only canonical market exits or workflow-linked protective stops."""
    client_order_id = str(getattr(order, "client_order_id", "") or "")
    order_type = _normalize_side(getattr(order, "type", ""))
    if (
        client_order_id == build_exit_client_order_id(workflow_id)
        and order_type == "market"
    ):
        return True
    stop_prefix = f"{build_stop_client_order_id(workflow_id)}-"
    return bool(
        order_type in {"stop", "stop_limit"}
        and normalize_workflow_id(client_order_id) == workflow_id
        and (
            client_order_id == build_stop_client_order_id(workflow_id)
            or client_order_id.startswith(stop_prefix)
        )
    )


def _transition_details(snapshot: dict[str, Any], event: str) -> dict[str, Any]:
    """Return details from the latest matching durable transition."""
    for transition in reversed(snapshot.get("transitions", [])):
        if transition.get("event") == event:
            return dict(transition.get("details", {}))
    return {}


def _preflight_symbol_clear(symbol: str) -> bool:
    """Refuse verification when the symbol has pre-existing broker/local state."""
    try:
        positions = get_open_positions(raise_on_error=True)
        orders = get_open_orders(symbol, raise_on_error=True)
        store = get_execution_store()
        local_active = store.load_active_position(symbol)
        pending_intents = store.load_pending_submission_intents(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Could not verify existing {symbol} state: {exc}")
        return False

    has_position = any(str(position.symbol) == symbol for position in positions)
    if has_position or orders or local_active is not None or pending_intents:
        print(
            f"[ERROR] Refusing verification: {symbol} already has broker or "
            "durable active/pending submission state."
        )
        print("        Clear or preserve that state deliberately before retrying.")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run one supervised, durable SPY paper-trading lifecycle.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="explicitly authorize one SPY paper entry and verified cleanup",
    )
    raise SystemExit(main(execute=parser.parse_args().execute))
