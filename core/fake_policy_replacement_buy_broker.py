"""Concrete offline broker for one protected replacement buy contract."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator, Literal
from uuid import uuid4

from core.strategy_policy.account_reconciliation import (
    BrokerAccountSnapshot,
    BrokerOrderFact,
    BrokerPositionFact,
)


_WORKING = frozenset({"accepted", "new", "partially_filled", "pending_new", "submitted"})


@dataclass(frozen=True, slots=True)
class ReplacementBuySubmission:
    success: bool
    broker_order_id: str | None = None
    outcome_uncertain: bool = False
    error: str = ""


class FakeProtectedReplacementBuyBroker:
    """Hold a capped buy and create its stop atomically with each fake fill."""

    def __init__(
        self,
        account: BrokerAccountSnapshot,
        *,
        quote_symbol: str,
        quote_price: Decimal,
        receipt_store_path: str | Path,
        response_mode: Literal["accepted", "reject_before_accept", "timeout_after_accept"] = "accepted",
    ) -> None:
        if type(account) is not BrokerAccountSnapshot or account.positions is None or account.open_orders is None:
            raise ValueError("fake replacement broker requires complete paper account facts")
        if not account.account_snapshot_id or account.cash is None:
            raise ValueError("fake replacement broker requires account identity and cash")
        if type(quote_symbol) is not str or not quote_symbol or quote_symbol != quote_symbol.upper():
            raise ValueError("fake replacement quote symbol must be uppercase")
        if not isinstance(quote_price, Decimal) or not quote_price.is_finite() or quote_price <= 0:
            raise ValueError("fake replacement quote must be positive")
        if response_mode not in {"accepted", "reject_before_accept", "timeout_after_accept"}:
            raise ValueError("unsupported fake replacement broker response mode")
        self.receipt_store_path = Path(receipt_store_path)
        if not self.receipt_store_path.parent.is_dir():
            raise ValueError("fake replacement receipt directory must already exist")
        self._lock = RLock()
        self._account = account
        self._quote_symbol = quote_symbol
        self._quote_price = quote_price
        self._response_mode = response_mode
        self.submissions: list[dict[str, object]] = []
        with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS fake_replacement_buy_receipts (
                    client_order_id TEXT PRIMARY KEY,
                    broker_order_id TEXT NOT NULL UNIQUE,
                    paper_account_environment_id TEXT NOT NULL,
                    source_account_snapshot_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    stop_price TEXT NOT NULL,
                    quote_price TEXT NOT NULL,
                    holding_episode_id TEXT NOT NULL,
                    filled_quantity TEXT NOT NULL,
                    filled_notional TEXT NOT NULL,
                    source_cash TEXT NOT NULL
                )"""
            )
            conn.commit()
        self._validate_receipts_against_account()

    def _validate_receipts_against_account(self, account: BrokerAccountSnapshot | None = None) -> None:
        """Fail closed if a receipt survived without its exact broker order/stop facts."""
        observed = self._account if account is None else account
        with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
            rows = conn.execute("SELECT * FROM fake_replacement_buy_receipts").fetchall()
        for raw in rows:
            (
                client, broker, environment, _, symbol, quantity, _, stop_price, _,
                holding_id, filled_quantity, filled_notional, source_cash,
            ) = (str(value) for value in raw)
            orders = tuple(row for row in observed.open_orders or () if row.broker_order_id == broker)
            if (
                environment != observed.paper_account_environment_id
                or len(orders) != 1
                or orders[0].client_order_id != client
                or orders[0].symbol != symbol
                or orders[0].side != "buy"
                or orders[0].holding_episode_id != holding_id
                or Decimal(str(orders[0].requested_quantity)) != Decimal(quantity)
                or Decimal(str(orders[0].cumulative_filled_quantity)) != Decimal(filled_quantity)
                or observed.cash is None
                or Decimal(str(observed.cash)) != Decimal(source_cash) - Decimal(filled_notional)
            ):
                raise ValueError("fake replacement receipt and account order did not recover together")
            filled = Decimal(filled_quantity)
            if filled > 0:
                positions = tuple(row for row in observed.positions or () if row.symbol == symbol)
                stops = tuple(
                    row for row in observed.open_orders or ()
                    if row.purpose == "protective_stop"
                    and row.symbol == symbol
                    and row.holding_episode_id == holding_id
                    and row.status in _WORKING
                )
                if (
                    len(positions) != 1
                    or Decimal(str(positions[0].quantity)) != filled
                    or len(stops) != 1
                    or Decimal(str(stops[0].requested_quantity)) != filled
                    or stops[0].stop_price is None
                    or Decimal(str(stops[0].stop_price)) != Decimal(stop_price)
                ):
                    raise ValueError("fake replacement filled buy lacks matching protected position")

    def snapshot(self) -> BrokerAccountSnapshot:
        with self._lock:
            return self._account

    @contextmanager
    def atomic_observation(self) -> Iterator[BrokerAccountSnapshot]:
        """Keep fake fills and stop swaps still while durable state catches up."""
        with self._lock:
            yield self._account

    def quote(self, symbol: str) -> Decimal:
        with self._lock:
            if symbol != self._quote_symbol:
                raise ValueError("candidate symbol differs from fake broker quote")
            return self._quote_price

    def set_quote(self, price: Decimal) -> None:
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError("fake replacement quote must be positive")
        with self._lock:
            self._quote_price = price

    def observe_account(self, account: BrokerAccountSnapshot) -> None:
        """Supply a newer complete fake-paper observation before submission."""
        if type(account) is not BrokerAccountSnapshot or account.positions is None or account.open_orders is None:
            raise ValueError("replacement observation must be complete")
        with self._lock:
            if (
                account.paper_account_environment_id != self._account.paper_account_environment_id
                or account.source_namespace != self._account.source_namespace
                or not account.account_snapshot_id
            ):
                raise ValueError("replacement observation differs from fake paper account")
            self._validate_receipts_against_account(account)
            self._account = account

    def _receipt(self, client_order_id: str) -> tuple[str, ...] | None:
        with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
            row = conn.execute(
                "SELECT * FROM fake_replacement_buy_receipts WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else tuple(str(value) for value in row)

    def _receipt_for_broker(self, broker_order_id: str) -> tuple[str, ...] | None:
        with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
            row = conn.execute(
                "SELECT * FROM fake_replacement_buy_receipts WHERE broker_order_id=?",
                (broker_order_id,),
            ).fetchone()
        return None if row is None else tuple(str(value) for value in row)

    def fill_report(self, broker_order_id: str) -> tuple[Decimal, Decimal]:
        """Return durable cumulative quantity and notional for a fake fill."""
        with self._lock:
            receipt = self._receipt_for_broker(broker_order_id)
            if receipt is None:
                raise ValueError("fake replacement buy has no durable fill receipt")
            return Decimal(receipt[10]), Decimal(receipt[11])

    def submit_protected_buy(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        limit_price: Decimal,
        stop_price: Decimal,
        candidate_price: Decimal,
        expected_account_snapshot_id: str,
        expected_account: BrokerAccountSnapshot,
        client_order_id: str,
        holding_episode_id: str,
    ) -> ReplacementBuySubmission:
        with self._lock:
            if any(
                not isinstance(value, Decimal) or not value.is_finite() or value <= 0
                for value in (quantity, limit_price, stop_price, candidate_price)
            ):
                raise ValueError("fake replacement buy quantities and prices must be positive")
            if (
                symbol != self._quote_symbol
                or candidate_price != self._quote_price
                or stop_price >= candidate_price
                or candidate_price > limit_price
                or not client_order_id
                or not holding_episode_id
            ):
                return ReplacementBuySubmission(False, error="fixed candidate or protected price changed")
            digest = sha256(client_order_id.encode("utf-8")).hexdigest()[:20]
            broker_order_id = "fake-replacement-buy:" + digest
            values = (
                client_order_id,
                broker_order_id,
                self._account.paper_account_environment_id,
                expected_account_snapshot_id,
                symbol,
                str(quantity),
                str(limit_price),
                str(stop_price),
                str(candidate_price),
                holding_episode_id,
                "0",
                "0",
                str(self._account.cash),
            )
            prior = self._receipt(client_order_id)
            if prior is not None:
                if prior[:10] != values[:10]:
                    raise ValueError("fake replacement buy client reference conflicts with fixed facts")
                orders = tuple(
                    row for row in self._account.open_orders
                    if row.client_order_id == client_order_id and row.broker_order_id == broker_order_id
                )
                if len(orders) != 1:
                    return ReplacementBuySubmission(False, outcome_uncertain=True, error="accepted buy needs broker reconciliation")
                return ReplacementBuySubmission(True, broker_order_id)
            if (
                self._account.account_snapshot_id != expected_account_snapshot_id
                or self._account != expected_account
                or self._account.cash is None
                or Decimal(str(self._account.cash)) < quantity * limit_price
                or any(row.symbol == symbol for row in self._account.positions)
            ):
                return ReplacementBuySubmission(False, error="paper cash, candidate, or source account changed")
            if self._response_mode == "reject_before_accept":
                return ReplacementBuySubmission(False, error="fake broker rejected before accepting buy")
            with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO fake_replacement_buy_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                conn.commit()
            order = BrokerOrderFact(
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side="buy",
                status="submitted",
                requested_quantity=float(quantity),
                cumulative_filled_quantity=0,
                purpose="strategy",
                holding_episode_id=holding_episode_id,
            )
            self._account = replace(
                self._account,
                open_orders=self._account.open_orders + (order,),
                account_snapshot_id="fake-replacement:submit:" + uuid4().hex,
            )
            self.submissions.append({
                "client_order_id": client_order_id,
                "broker_order_id": broker_order_id,
                "quantity": quantity,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "source_account_snapshot_id": expected_account_snapshot_id,
            })
            if self._response_mode == "timeout_after_accept":
                raise TimeoutError("fake replacement buy response lost after accept")
            return ReplacementBuySubmission(True, broker_order_id)

    def fill_buy(
        self,
        broker_order_id: str,
        delta: Decimal,
        *,
        fill_price: Decimal,
        observed_at: datetime,
    ) -> BrokerAccountSnapshot:
        """Atomically add bought shares and a stop for the exact filled quantity."""
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in (delta, fill_price)
        ):
            raise ValueError("fake replacement fill quantity and price must be positive")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("fake replacement fill observation must be timezone-aware")
        with self._lock:
            receipt = self._receipt_for_broker(broker_order_id)
            if receipt is None:
                raise ValueError("fake replacement buy has no durable broker contract")
            client, _, environment, _, symbol, qty_raw, limit_raw, stop_raw, _, holding_id, filled_raw, notional_raw, source_cash = receipt
            orders = tuple(row for row in self._account.open_orders if row.broker_order_id == broker_order_id)
            if len(orders) != 1 or orders[0].client_order_id != client:
                raise ValueError("fake replacement buy order is not uniquely observed")
            order = orders[0]
            cumulative = Decimal(str(order.cumulative_filled_quantity)) + delta
            if (
                environment != self._account.paper_account_environment_id
                or order.status not in _WORKING
                or cumulative > Decimal(qty_raw)
                or Decimal(filled_raw) != Decimal(str(order.cumulative_filled_quantity))
                or fill_price > Decimal(limit_raw)
                or fill_price <= Decimal(stop_raw)
                or self._account.cash is None
                or Decimal(str(self._account.cash)) != Decimal(source_cash) - Decimal(notional_raw)
                or Decimal(str(self._account.cash)) < delta * fill_price
            ):
                raise ValueError("fake replacement buy exceeds its fixed order or cash cap")
            prior_positions = tuple(row for row in self._account.positions if row.symbol == symbol)
            if len(prior_positions) > 1 or (
                prior_positions and Decimal(str(prior_positions[0].quantity)) != Decimal(str(order.cumulative_filled_quantity))
            ):
                raise ValueError("fake replacement position diverged from buy fills")
            stop = Decimal(stop_raw)
            stop_digest = sha256(f"{client}:{cumulative}".encode("utf-8")).hexdigest()[:20]
            stop_order = BrokerOrderFact(
                broker_order_id="fake-replacement-stop:" + stop_digest,
                client_order_id="fake-replacement-stop-client:" + stop_digest,
                symbol=symbol,
                side="sell",
                status="submitted",
                requested_quantity=float(cumulative),
                cumulative_filled_quantity=0,
                purpose="protective_stop",
                holding_episode_id=holding_id,
                stop_price=float(stop),
            )
            filled_order = replace(
                order,
                cumulative_filled_quantity=float(cumulative),
                status="filled" if cumulative == Decimal(qty_raw) else "partially_filled",
            )
            position = BrokerPositionFact(
                symbol=symbol,
                quantity=float(cumulative),
                mark_price=float(fill_price),
                mark_observed_at=observed_at,
                sector=None,
                industry=None,
            )
            orders = tuple(
                row for row in self._account.open_orders
                if row.broker_order_id != broker_order_id
                and not (row.purpose == "protective_stop" and row.holding_episode_id == holding_id)
            ) + (filled_order, stop_order)
            with closing(sqlite3.connect(str(self.receipt_store_path))) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """UPDATE fake_replacement_buy_receipts
                       SET filled_quantity=?, filled_notional=?
                       WHERE broker_order_id=? AND filled_quantity=? AND filled_notional=?""",
                    (
                        str(cumulative),
                        str(Decimal(notional_raw) + delta * fill_price),
                        broker_order_id,
                        filled_raw,
                        notional_raw,
                    ),
                )
                if conn.total_changes != 1:
                    raise ValueError("fake replacement fill receipt changed concurrently")
                conn.commit()
            self._account = replace(
                self._account,
                cash=float(Decimal(str(self._account.cash)) - delta * fill_price),
                balance_observed_at=observed_at,
                positions=tuple(row for row in self._account.positions if row.symbol != symbol) + (position,),
                open_orders=orders,
                account_snapshot_id="fake-replacement:fill:" + uuid4().hex,
            )
            return self._account
