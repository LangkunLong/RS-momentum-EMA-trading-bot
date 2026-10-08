"""In-memory paper-only broker for atomic shared-cap policy exits."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from threading import RLock
from typing import Literal

from core.policy_execution_state import ActionIntent, DecisionIdentity
from core.policy_exit_execution import PolicyExitSubmissionResult
from core.strategy_policy.account_reconciliation import BrokerAccountSnapshot, BrokerOrderFact


_WORKING = frozenset({"accepted", "new", "partially_filled", "pending_new", "submitted"})


class FakeProtectedExitBroker:
    """Keep an exit sell and its stop under one locked fake-paper position cap.

    Each fill claims remaining position inside the same lock. Submission fails
    before creating an order if the stop or position differs from the supplied
    facts. This class has no network client and cannot place operational orders.
    """

    def __init__(
        self,
        account: BrokerAccountSnapshot,
        *,
        response_mode: Literal["accepted", "reject_before_accept", "timeout_after_accept"] = "accepted",
    ) -> None:
        if type(account) is not BrokerAccountSnapshot or account.positions is None or account.open_orders is None:
            raise ValueError("fake broker needs complete paper positions and orders")
        if response_mode not in {"accepted", "reject_before_accept", "timeout_after_accept"}:
            raise ValueError("unsupported fake broker response mode")
        self._lock = RLock()
        self._account = account
        self._response_mode = response_mode
        self._sequence = 0
        self._groups: dict[str, dict[str, object]] = {}
        self.submissions: list[dict[str, object]] = []
        self._recover_working_groups()

    def _recover_working_groups(self) -> None:
        """Rebuild the cap from complete open orders and their cumulative fills."""
        claimed_stops: set[str] = set()
        for sell in self._account.open_orders:
            if sell.purpose != "strategy" or sell.side != "sell" or sell.status not in _WORKING:
                continue
            stops = tuple(
                row for row in self._account.open_orders
                if row.purpose == "protective_stop"
                and row.side == "sell"
                and row.status in _WORKING
                and row.symbol == sell.symbol
                and row.holding_episode_id == sell.holding_episode_id
            )
            if len(stops) != 1 or not sell.client_order_id or not sell.broker_order_id or not stops[0].broker_order_id:
                raise ValueError("working fake sell has no unique protected stop after restart")
            if stops[0].broker_order_id in claimed_stops:
                raise ValueError("multiple working fake sells claim one protected stop")
            claimed_stops.add(stops[0].broker_order_id)
            if sell.client_order_id in self._groups:
                raise ValueError("duplicate working fake sell client reference")
            used = Decimal(str(sell.cumulative_filled_quantity)) + Decimal(str(stops[0].cumulative_filled_quantity))
            cap = self._position_quantity(sell.symbol) + used
            if cap <= 0 or Decimal(str(stops[0].requested_quantity)) < self._position_quantity(sell.symbol):
                raise ValueError("recovered fake stop does not protect the remaining position")
            digest = sha256(sell.client_order_id.encode("utf-8")).hexdigest()[:20]
            self._groups[sell.client_order_id] = {
                "group_id": "fake-shared-position:" + digest,
                "symbol": sell.symbol,
                "cap": cap,
                "used": used,
                "sell_broker_order_id": sell.broker_order_id,
                "stop_broker_order_id": stops[0].broker_order_id,
                "initial_stop_broker_order_id": stops[0].broker_order_id,
            }

    def snapshot(self) -> BrokerAccountSnapshot:
        with self._lock:
            return self._account

    def confirms_flat_exit(self, action: ActionIntent, stop_broker_order_id: str) -> bool:
        """Require this broker's own accepted group and terminal fill/cancel event."""
        with self._lock:
            if (
                self._account.paper_account_environment_id
                != action.decision.deployment_identity.paper_account_environment_id
                or self._account.clock.completed_session != action.decision.clock.decision_session
                or self._account.clock.next_execution_session != action.decision.clock.next_execution_session
                or self._account.positions is None
                or self._account.open_orders is None
                or not self._account.account_snapshot_id
                or not action.order_attempts
                or not action.order_attempts[0].broker_order_id
            ):
                return False
            groups = tuple(
                group for group in self._groups.values()
                if group["sell_broker_order_id"] == action.order_attempts[0].broker_order_id
                and group["initial_stop_broker_order_id"] == stop_broker_order_id
            )
            if len(groups) != 1:
                return False
            group = groups[0]
            if (
                not group.get("flat_completion_snapshot_id")
                or Decimal(str(group["used"])) != Decimal(str(group["cap"]))
                or any(row.symbol == action.broker_symbol for row in self._account.positions)
                or any(
                    row.symbol == action.broker_symbol and row.side == "sell" and row.status in _WORKING
                    for row in self._account.open_orders
                )
            ):
                return False
            return True

    def observe_for(self, decision: DecisionIdentity) -> BrokerAccountSnapshot:
        """Label the next paper observation with its fixed decision identity."""
        with self._lock:
            if (
                decision.deployment_identity.paper_account_environment_id
                != self._account.paper_account_environment_id
                or decision.clock.decision_session != self._account.clock.completed_session
                or decision.clock.next_execution_session != self._account.clock.next_execution_session
            ):
                raise ValueError("fake broker observation decision differs from its account clock")
            self._account = replace(
                self._account,
                decision_slot_id=decision.decision_slot_id,
                decision_id=decision.decision_id,
                account_snapshot_id=self._next_snapshot_id("decision"),
            )
            return self._account

    def _next_snapshot_id(self, event: str) -> str:
        self._sequence += 1
        return f"fake-protected-exit:{event}:{self._sequence}"

    def _position_quantity(self, symbol: str) -> Decimal:
        rows = tuple(row for row in self._account.positions if row.symbol == symbol)
        if len(rows) != 1:
            raise ValueError("fake broker requires exactly one held symbol position")
        return Decimal(str(rows[0].quantity))

    def _order(self, broker_order_id: str) -> BrokerOrderFact:
        rows = tuple(row for row in self._account.open_orders if row.broker_order_id == broker_order_id)
        if len(rows) != 1:
            raise ValueError("fake broker order reference is not uniquely working")
        return rows[0]

    def _group_result(self, group: dict[str, object]) -> PolicyExitSubmissionResult:
        return PolicyExitSubmissionResult(
            success=True,
            broker_order_id=str(group["sell_broker_order_id"]),
            linked_stop_broker_order_id=str(group["stop_broker_order_id"]),
            shared_sell_quantity_limit=group["cap"],
            shared_sell_group_id=str(group["group_id"]),
        )

    def submit_protected_exit(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
        confirmed_stop_client_order_id: str,
        confirmed_stop_broker_order_id: str,
        protected_position_quantity: Decimal,
    ) -> PolicyExitSubmissionResult:
        """Atomically accept a sell only with its matching active stop and cap."""
        with self._lock:
            if client_order_id in self._groups:
                return self._group_result(self._groups[client_order_id])
            if (
                not isinstance(quantity, Decimal)
                or not quantity.is_finite()
                or quantity <= 0
                or not isinstance(protected_position_quantity, Decimal)
                or not protected_position_quantity.is_finite()
                or quantity > protected_position_quantity
                or self._position_quantity(symbol) != protected_position_quantity
            ):
                raise ValueError("fake broker sell exceeds its actual protected position")
            stop = self._order(confirmed_stop_broker_order_id)
            if (
                stop.client_order_id != confirmed_stop_client_order_id
                or stop.symbol != symbol
                or stop.side != "sell"
                or stop.purpose != "protective_stop"
                or stop.status not in _WORKING
                or Decimal(str(stop.requested_quantity)) != protected_position_quantity
                or Decimal(str(stop.cumulative_filled_quantity)) != 0
            ):
                raise ValueError("fake broker stop differs from the protected position")
            if any(
                row is not stop and row.symbol == symbol and row.side == "sell" and row.status in _WORKING
                for row in self._account.open_orders
            ):
                raise ValueError("fake broker already has a working sell for this position")
            if self._response_mode == "reject_before_accept":
                return PolicyExitSubmissionResult(success=False, error="fake broker rejected before accepting a sell")
            digest = sha256(client_order_id.encode("utf-8")).hexdigest()[:20]
            broker_order_id = "fake-exit-order:" + digest
            group_id = "fake-shared-position:" + digest
            order = BrokerOrderFact(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side="sell",
                status="submitted",
                requested_quantity=float(quantity),
                cumulative_filled_quantity=0,
                purpose="strategy",
                holding_episode_id=stop.holding_episode_id,
            )
            group: dict[str, object] = {
                "group_id": group_id,
                "symbol": symbol,
                "cap": protected_position_quantity,
                "used": Decimal("0"),
                "sell_broker_order_id": broker_order_id,
                "stop_broker_order_id": stop.broker_order_id,
                "initial_stop_broker_order_id": stop.broker_order_id,
            }
            self._groups[client_order_id] = group
            self.submissions.append({
                "symbol": symbol,
                "quantity": quantity,
                "client_order_id": client_order_id,
                "confirmed_stop_client_order_id": confirmed_stop_client_order_id,
                "confirmed_stop_broker_order_id": confirmed_stop_broker_order_id,
                "protected_position_quantity": protected_position_quantity,
            })
            self._account = replace(
                self._account,
                open_orders=self._account.open_orders + (order,),
                account_snapshot_id=self._next_snapshot_id("submit"),
            )
            if self._response_mode == "timeout_after_accept":
                raise TimeoutError("fake broker response lost after atomic accept")
            return self._group_result(group)

    def fill_order(self, broker_order_id: str, delta: Decimal) -> BrokerAccountSnapshot:
        """Apply one sell or stop fill without exceeding the shared position cap."""
        with self._lock:
            if not isinstance(delta, Decimal) or not delta.is_finite() or delta <= 0:
                raise ValueError("fake broker fill delta must be positive")
            matches = tuple(
                group for group in self._groups.values()
                if broker_order_id in {group["sell_broker_order_id"], group["stop_broker_order_id"]}
            )
            if len(matches) != 1:
                raise ValueError("fill has no unique protected sell/stop group")
            group = matches[0]
            order = self._order(broker_order_id)
            remaining = self._position_quantity(str(group["symbol"]))
            if (
                order.status not in _WORKING
                or Decimal(str(order.cumulative_filled_quantity)) + delta
                > Decimal(str(order.requested_quantity))
                or delta > remaining
                or Decimal(str(group["used"])) + delta > Decimal(str(group["cap"]))
            ):
                raise ValueError("fake broker shared sell cap would be exceeded")
            next_quantity = remaining - delta
            cumulative = Decimal(str(order.cumulative_filled_quantity)) + delta
            replacement = replace(
                order,
                cumulative_filled_quantity=float(cumulative),
                status="filled" if cumulative == Decimal(str(order.requested_quantity)) else "partially_filled",
            )
            positions = tuple(
                replace(row, quantity=float(next_quantity)) if row.symbol == group["symbol"] else row
                for row in self._account.positions
                if next_quantity > 0 or row.symbol != group["symbol"]
            )
            orders = tuple(row for row in self._account.open_orders if row is not order)
            if next_quantity > 0 and replacement.status in _WORKING:
                orders += (replacement,)
            if next_quantity == 0:
                orders = tuple(
                    row for row in orders
                    if row.broker_order_id not in {group["sell_broker_order_id"], group["stop_broker_order_id"]}
                )
            elif broker_order_id == group["stop_broker_order_id"]:
                orders = tuple(row for row in orders if row.broker_order_id != group["sell_broker_order_id"])
            group["used"] = Decimal(str(group["used"])) + delta
            self._account = replace(
                self._account,
                positions=positions,
                open_orders=orders,
                account_snapshot_id=self._next_snapshot_id("fill"),
            )
            if next_quantity == 0:
                group["flat_completion_snapshot_id"] = self._account.account_snapshot_id
            return self._account

    def replace_stop(
        self,
        *,
        old_order: BrokerOrderFact,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        new_client_order_id: str,
    ) -> BrokerAccountSnapshot:
        """Swap the stopped order under the same locked fake-paper group."""
        with self._lock:
            current = self._order(old_order.broker_order_id)
            if (
                current != old_order
                or current.purpose != "protective_stop"
                or current.status not in _WORKING
                or current.symbol != symbol
                or quantity != self._position_quantity(symbol)
                or Decimal(str(current.cumulative_filled_quantity)) != 0
            ):
                raise ValueError("fake broker stop exchange differs from held shares")
            if any(
                row.symbol == symbol and row.side == "sell" and row.purpose == "strategy" and row.status in _WORKING
                for row in self._account.open_orders
            ):
                raise ValueError("fake broker cannot exchange a stop beside a working strategy sell")
            digest = sha256(new_client_order_id.encode("utf-8")).hexdigest()[:20]
            new_broker_id = "fake-stop-order:" + digest
            new_order = replace(
                current,
                broker_order_id=new_broker_id,
                client_order_id=new_client_order_id,
                requested_quantity=float(quantity),
                stop_price=float(stop_price),
            )
            self._account = replace(
                self._account,
                open_orders=tuple(row for row in self._account.open_orders if row is not current) + (new_order,),
                account_snapshot_id=self._next_snapshot_id("stop-replace"),
            )
            for group in self._groups.values():
                if group["stop_broker_order_id"] == current.broker_order_id:
                    group["stop_broker_order_id"] = new_broker_id
            return self._account
