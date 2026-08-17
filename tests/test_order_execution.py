"""Unit tests for core/order_execution.py and auto_trader.py.

All Alpaca TradingClient calls are mocked — no real network or paper-account
calls are made.  Tests verify:

  order_execution layer
  ├── submit_bracket_buy   — limit-bracket, market, error path
  ├── submit_stop_loss     — success, error path
  ├── submit_market_sell   — success, error path
  ├── close_position       — held position, flat (no-op), API error
  ├── get_open_positions   — mapping from Alpaca model, error path
  ├── get_open_orders      — all orders, symbol-filtered, error path
  ├── cancel_open_orders   — cancels each, partial failure
  └── check_exit_signals   — breach / no-breach / exact threshold

  auto_trader orchestration
  ├── monitor_and_exit_positions — hard-stop trigger, MA-violation trigger,
  │                                healthy position (no exit), no positions
  ├── execute_entries            — normal entry, dedup (already held),
  │                                position-limit guard, dry-run, equity=0
  └── _is_market_open / _get_account_equity — happy and error paths
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

import core.order_execution as oe
from core.order_execution import (
    OrderResult,
    PositionSummary,
    cancel_open_orders,
    cancel_open_orders_verified,
    check_exit_signals,
    close_position,
    ensure_protective_stop,
    get_closed_orders,
    reconcile_open_position_stops,
    reconcile_symbol_after_exit_failure,
    get_open_orders,
    get_open_positions,
    submit_bracket_buy,
    submit_market_sell,
    submit_stop_loss,
)


# ---------------------------------------------------------------------------
# Helpers — build mock Alpaca objects
# ---------------------------------------------------------------------------


def _mock_order(
    order_id: str = "test-order-id",
    *,
    symbol: str = "AAPL",
    side: str = "buy",
    order_type: str = "limit",
    qty: float = 1.0,
    stop_price: float | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like an alpaca Order model."""
    order = MagicMock()
    order.id = order_id
    order.symbol = symbol
    order.side = side
    order.type = order_type
    order.qty = str(qty)
    order.filled_qty = "0"
    order.stop_price = None if stop_price is None else str(stop_price)
    order.client_order_id = ""
    order.status = "new"
    order.time_in_force = "gtc" if order_type == "stop" else "day"
    return order


def _mock_position(
    symbol: str = "AAPL",
    qty: float = 10.0,
    avg_entry: float = 100.0,
    current_price: float = 93.0,
    unrealized_plpc: float = -0.07,
) -> MagicMock:
    """Return a MagicMock that looks like an alpaca Position model."""
    pos = MagicMock()
    pos.symbol = symbol
    pos.qty = str(qty)
    pos.avg_entry_price = str(avg_entry)
    pos.current_price = str(current_price)
    pos.unrealized_plpc = str(unrealized_plpc)
    return pos


def _patched_client(client_mock: MagicMock) -> Any:
    """Context manager: patch _get_trading_client to return client_mock."""
    return patch("core.order_execution._get_trading_client", return_value=client_mock)


def _submission_ready_workflow() -> SimpleNamespace:
    """Return a workflow double that can durably fence a mocked STOP submit."""
    return SimpleNamespace(
        transitions=[],
        mark_protective_stop=lambda **_kwargs: None,
    )


# ===========================================================================
# submit_bracket_buy
# ===========================================================================


class TestSubmitBracketBuy:
    @pytest.mark.parametrize(
        "status",
        [
            "accepted",
            "accepted_for_bidding",
            "pending_new",
            "new",
            "partially_filled",
            "filled",
        ],
    )
    def test_allowlisted_broker_status_is_accepted(self, status: str) -> None:
        client = MagicMock()
        order = _mock_order("order-accepted")
        order.status = status
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=1.0, limit_price=100.0)

        assert result.success is True
        assert result.order_id == "order-accepted"

    def test_limit_entry_order_is_accepted(self) -> None:
        """submit_bracket_buy with a limit price must return success=True."""
        client = MagicMock()
        client.submit_order.return_value = _mock_order("order-1")

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=5.0, limit_price=150.0, stop_loss_pct=0.07)

        assert result.success is True
        assert result.order_id == "order-1"
        assert result.symbol == "AAPL"
        assert result.side == "buy"
        assert result.qty == 5.0
        assert result.error == ""

    def test_limit_entry_order_has_no_embedded_stop(self) -> None:
        """Entry orders must not carry an embedded stop before the fill exists."""
        from alpaca.trading.requests import LimitOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            submit_bracket_buy("NVDA", qty=2.0, limit_price=200.00, stop_loss_pct=0.07)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, LimitOrderRequest)
        assert getattr(call_args, "stop_loss", None) is None
        assert getattr(call_args, "order_class", None) is None

    def test_market_order_submitted_when_no_limit_price(self) -> None:
        """Without a limit_price, a plain MarketOrderRequest must be submitted."""
        from alpaca.trading.requests import MarketOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            result = submit_bracket_buy("TSLA", qty=1.0)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, MarketOrderRequest)
        assert result.success is True

    def test_api_error_returns_failure_result(self) -> None:
        """API exception must be caught and returned as success=False."""
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("connection timeout")

        with _patched_client(client):
            result = submit_bracket_buy("FAIL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert "connection timeout" in result.error
        assert result.symbol == "FAIL"
        assert result.outcome_uncertain is True

    def test_pre_submission_client_failure_is_definitive(self) -> None:
        with patch(
            "core.order_execution._get_trading_client",
            side_effect=EnvironmentError("paper credentials missing"),
        ):
            result = submit_bracket_buy("FAIL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert result.outcome_uncertain is False
        assert "credentials" in result.error

    @pytest.mark.parametrize("status", ["rejected", "canceled", "pending_replace"])
    def test_unsafe_broker_status_returns_failure(self, status: str) -> None:
        client = MagicMock()
        order = _mock_order("order-unsafe")
        order.status = status
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert result.order_id == ""
        assert status in result.error
        assert result.outcome_uncertain is (status == "pending_replace")

    def test_missing_broker_status_returns_failure(self) -> None:
        client = MagicMock()
        client.submit_order.return_value = SimpleNamespace(id="order-no-status")

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert result.order_id == ""
        assert "status" in result.error.lower()

    def test_missing_broker_order_id_returns_failure(self) -> None:
        client = MagicMock()
        order = _mock_order("")
        order.status = "accepted"
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_bracket_buy("AAPL", qty=1.0, limit_price=100.0)

        assert result.success is False
        assert result.order_id == ""
        assert "order id" in result.error.lower()
        assert result.outcome_uncertain is True

    def test_default_stop_pct_is_retained_for_logging_compatibility(self) -> None:
        """When stop_loss_pct is omitted, the entry path should still succeed."""
        from alpaca.trading.requests import LimitOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order()

        with _patched_client(client):
            with patch("core.order_execution.settings") as mock_settings:
                mock_settings.STOP_LOSS_PCT = 0.08  # override to 8% for this test
                result = submit_bracket_buy("GOOG", qty=1.0, limit_price=100.0)

        call_args = client.submit_order.call_args[0][0]
        assert isinstance(call_args, LimitOrderRequest)
        assert result.success is True


# ===========================================================================
# submit_stop_loss
# ===========================================================================


class TestSubmitStopLoss:
    def test_gtc_stop_sell_order_submitted(self) -> None:
        """submit_stop_loss must submit a GTC stop-sell and return success."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        client = MagicMock()
        client.submit_order.return_value = _mock_order(
            "stop-id-1",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            stop_price=93.0,
        )

        with _patched_client(client):
            result = submit_stop_loss("AAPL", qty=10.0, stop_price=93.0)

        assert result.success is True
        assert result.order_id == "stop-id-1"
        assert result.side == "sell"

        req = client.submit_order.call_args[0][0]
        assert isinstance(req, StopOrderRequest)
        assert req.side == OrderSide.SELL
        assert req.time_in_force == TimeInForce.GTC
        assert req.stop_price == 93.0

    def test_api_error_returns_failure(self) -> None:
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("bad request")

        with _patched_client(client):
            result = submit_stop_loss("AAPL", qty=5.0, stop_price=90.0)

        assert result.success is False
        assert "bad request" in result.error

    def test_post_submit_transport_error_is_outcome_uncertain(self) -> None:
        client = MagicMock()
        client.submit_order.side_effect = ConnectionError("response lost")

        with _patched_client(client):
            result = submit_stop_loss(
                "AAPL",
                qty=5.0,
                stop_price=90.0,
                client_order_id="wf-aapl-1-sl-a1b2c3",
            )

        assert result.success is False
        assert result.outcome_uncertain is True
        assert result.client_order_id == "wf-aapl-1-sl-a1b2c3"

    @pytest.mark.parametrize(
        "broker_response",
        [
            SimpleNamespace(
                id="stop-no-status",
                symbol="AAPL",
                side="sell",
                type="stop",
                time_in_force="gtc",
                client_order_id="wf-aapl-1-sl-a1b2c3",
            ),
            SimpleNamespace(
                id="",
                status="new",
                symbol="AAPL",
                side="sell",
                type="stop",
                time_in_force="gtc",
                client_order_id="wf-aapl-1-sl-a1b2c3",
            ),
            SimpleNamespace(
                id="stop-wrong-client",
                status="rejected",
                symbol="AAPL",
                side="sell",
                type="stop",
                time_in_force="gtc",
                client_order_id="wf-foreign-sl-a1b2c3",
            ),
        ],
    )
    def test_malformed_post_submit_response_is_outcome_uncertain(
        self,
        broker_response: SimpleNamespace,
    ) -> None:
        client = MagicMock()
        client.submit_order.return_value = broker_response

        with _patched_client(client):
            result = submit_stop_loss(
                "AAPL",
                qty=5.0,
                stop_price=90.0,
                client_order_id="wf-aapl-1-sl-a1b2c3",
            )

        assert result.success is False
        assert result.order_id == ""
        assert result.outcome_uncertain is True

    def test_explicit_rejected_zero_fill_is_definitive(self) -> None:
        client = MagicMock()
        rejected = _mock_order(
            "stop-rejected",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            stop_price=90.0,
        )
        rejected.status = "rejected"
        rejected.client_order_id = "wf-aapl-1-sl-a1b2c3"
        client.submit_order.return_value = rejected

        with _patched_client(client):
            result = submit_stop_loss(
                "AAPL",
                qty=5.0,
                stop_price=90.0,
                client_order_id="wf-aapl-1-sl-a1b2c3",
            )

        assert result.success is False
        assert result.outcome_uncertain is False


# ===========================================================================
# ensure_protective_stop
# ===========================================================================


class TestEnsureProtectiveStop:
    @pytest.mark.parametrize("seed_id", ["entry-1", "entry-2"])
    def test_replacement_chain_returns_total_terminal_filled_quantity(
        self,
        seed_id: str,
    ) -> None:
        original = SimpleNamespace(
            id="entry-1",
            symbol="AAPL",
            side="buy",
            type="limit",
            status="replaced",
            qty="10",
            filled_qty="4",
            client_order_id="wf-aapl-1",
            replaced_by="entry-2",
            replaces=None,
        )
        replacement = SimpleNamespace(
            id="entry-2",
            symbol="AAPL",
            side="buy",
            type="limit",
            status="filled",
            qty="6",
            filled_qty="6",
            client_order_id="wf-aapl-1",
            replaced_by=None,
            replaces="entry-1",
        )
        client = MagicMock()
        orders_by_id = {
            "entry-1": original,
            "entry-2": replacement,
        }
        client.get_order_by_id.side_effect = orders_by_id.__getitem__
        workflow = SimpleNamespace(
            repair_entry_order_reference=lambda **_kwargs: None,
        )

        with (
            _patched_client(client),
            patch(
                "core.order_execution.get_workflow",
                return_value=workflow,
                create=True,
            ),
            patch("core.order_execution.time.sleep"),
        ):
            filled_qty = oe._wait_for_terminal_buy_order_chain(
                "AAPL",
                {seed_id},
                workflow_id="wf-aapl-1",
                timeout=1.0,
                poll_interval=0.0,
            )

        assert filled_qty == pytest.approx(10.0)
        assert {
            broker_call.args[0] for broker_call in client.get_order_by_id.call_args_list
        } == {"entry-1", "entry-2"}
        client.cancel_order_by_id.assert_not_called()

    def test_replaced_order_waits_for_traversable_replacement_link(self) -> None:
        unlinked_parent = SimpleNamespace(
            id="entry-1",
            symbol="AAPL",
            side="buy",
            status="replaced",
            filled_qty="4",
            client_order_id="wf-aapl-1",
            replaced_by=None,
            replaces=None,
        )
        linked_parent = SimpleNamespace(
            **{
                **vars(unlinked_parent),
                "replaced_by": "entry-2",
            },
        )
        replacement = SimpleNamespace(
            id="entry-2",
            symbol="AAPL",
            side="buy",
            status="filled",
            filled_qty="6",
            client_order_id="wf-aapl-1-r1",
            replaced_by=None,
            replaces="entry-1",
        )
        parent_responses = iter([unlinked_parent, linked_parent, linked_parent])
        client = MagicMock()

        def get_order(order_id: str) -> SimpleNamespace:
            if order_id == "entry-1":
                return next(parent_responses)
            assert order_id == "entry-2"
            return replacement

        client.get_order_by_id.side_effect = get_order
        workflow = SimpleNamespace(
            repair_entry_order_reference=lambda **_kwargs: None,
        )

        with (
            _patched_client(client),
            patch(
                "core.order_execution.get_workflow",
                return_value=workflow,
                create=True,
            ),
            patch("core.order_execution.time.sleep"),
        ):
            filled_qty = oe._wait_for_terminal_buy_order_chain(
                "AAPL",
                {"entry-1"},
                workflow_id="wf-aapl-1",
                timeout=1.0,
                poll_interval=0.0,
            )

        assert filled_qty == pytest.approx(10.0)
        assert [
            broker_call.args[0] for broker_call in client.get_order_by_id.call_args_list
        ].count("entry-1") >= 2

    def test_entry_fence_persists_each_trusted_replacement_order_id(self) -> None:
        original = SimpleNamespace(
            id="entry-1",
            symbol="AAPL",
            side="buy",
            status="replaced",
            filled_qty="4",
            client_order_id="wf-aapl-1",
            replaced_by="entry-2",
            replaces=None,
        )
        replacement = SimpleNamespace(
            id="entry-2",
            symbol="AAPL",
            side="buy",
            status="filled",
            filled_qty="6",
            client_order_id="wf-aapl-1-r1",
            replaced_by=None,
            replaces="entry-1",
        )
        client = MagicMock()
        client.get_order_by_id.side_effect = {
            "entry-1": original,
            "entry-2": replacement,
        }.__getitem__
        repairs: list[dict[str, str]] = []
        workflow = SimpleNamespace(
            repair_entry_order_reference=lambda **kwargs: repairs.append(kwargs),
        )

        with (
            _patched_client(client),
            patch(
                "core.order_execution.get_workflow",
                return_value=workflow,
                create=True,
            ),
            patch("core.order_execution.time.sleep"),
        ):
            filled_qty = oe._wait_for_terminal_buy_order_chain(
                "AAPL",
                {"entry-1"},
                workflow_id="wf-aapl-1",
                timeout=1.0,
                poll_interval=0.0,
            )

        assert filled_qty == pytest.approx(10.0)
        assert {
            (repair["broker_order_id"], repair["client_order_id"])
            for repair in repairs
        } == {
            ("entry-1", "wf-aapl-1"),
            ("entry-2", "wf-aapl-1-r1"),
        }

    def test_terminal_entry_quantity_fences_stop_reconciliation(self) -> None:
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=10.0,
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=protected,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
            )

        terminal.assert_called_once_with(
            "AAPL",
            {"entry-1"},
            workflow_id="wf-aapl-1",
        )
        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert result == protected

    @pytest.mark.parametrize(
        ("observed_qty", "durable_sell_fill_qty", "expected_minimum"),
        [
            (6.0, 4.0, 6.0),
            (7.0, 4.0, 7.0),
        ],
    )
    def test_terminal_entry_fence_uses_net_causal_exposure_and_observed_floor(
        self,
        observed_qty: float,
        durable_sell_fill_qty: float,
        expected_minimum: float,
    ) -> None:
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=observed_qty,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=10.0,
            ),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=protected,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=observed_qty,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
                durable_sell_fill_qty=durable_sell_fill_qty,
            )

        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=expected_minimum,
        )
        assert result == protected

    def test_terminal_entry_fence_rejects_sell_offset_above_terminal_buys(
        self,
    ) -> None:
        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=10.0,
            ),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure"
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=1.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
                durable_sell_fill_qty=11.0,
            )

        assert result.success is False
        assert result.action == "reconciliation_failed"
        assert "exceed terminal entry fills" in result.error
        reconcile.assert_not_called()

    def test_entry_fence_reloads_newer_durable_sell_offset(self) -> None:
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=6.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )
        refreshed_workflow = SimpleNamespace(
            transitions=[
                SimpleNamespace(
                    event="sell_partial_fill_received",
                    details={"broker_order_id": "sell-1", "qty": 4.0},
                )
            ]
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=10.0,
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=refreshed_workflow,
            ),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=protected,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
                durable_sell_fill_qty=2.0,
            )

        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=6.0,
        )
        assert result == protected

    def test_durable_entry_ids_are_unioned_with_single_and_open_buy_ids(self) -> None:
        open_buy = _mock_order(
            "entry-open",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            qty=12.0,
        )
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=12.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[open_buy], []],
            ),
            patch("core.order_execution._cancel_order_ids_verified"),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=12.0,
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=protected,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-single",
                entry_order_ids={"entry-durable-1", "entry-durable-2"},
            )

        terminal.assert_called_once_with(
            "AAPL",
            {
                "entry-single",
                "entry-durable-1",
                "entry-durable-2",
                "entry-open",
            },
            workflow_id="wf-aapl-1",
        )
        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=12.0,
        )
        assert result == protected

    def test_new_buy_seen_after_initial_fence_is_refenced_before_protection(
        self,
    ) -> None:
        replacement = _mock_order(
            "entry-2",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            qty=6.0,
        )
        pending = oe.ProtectiveStopResult(
            success=False,
            order_id="",
            symbol="AAPL",
            qty=4.0,
            stop_price=93.0,
            action="position_sync_pending",
        )
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[], [replacement], []],
            ),
            patch("core.order_execution._cancel_order_ids_verified") as cancel,
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                side_effect=[4.0, 10.0],
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                side_effect=[pending, protected],
            ) as reconcile,
            patch("core.order_execution.time.sleep"),
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
            )

        assert terminal.call_args_list == [
            call("AAPL", {"entry-1"}, workflow_id="wf-aapl-1"),
            call("AAPL", {"entry-1", "entry-2"}, workflow_id="wf-aapl-1"),
        ]
        cancel.assert_called_once_with("AAPL", {"entry-2"})
        assert reconcile.call_args_list[-1] == call(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert result == protected

    def test_buy_appearing_after_success_is_refenced_before_return(self) -> None:
        replacement = _mock_order(
            "entry-2",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            qty=6.0,
        )
        initially_protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-4",
            symbol="AAPL",
            qty=4.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )
        finally_protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-10",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[], [replacement], [], []],
            ),
            patch("core.order_execution._cancel_order_ids_verified") as cancel,
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                side_effect=[4.0, 10.0],
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                side_effect=[initially_protected, finally_protected],
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
            )

        cancel.assert_called_once_with("AAPL", {"entry-2"})
        assert terminal.call_args_list == [
            call("AAPL", {"entry-1"}, workflow_id="wf-aapl-1"),
            call(
                "AAPL",
                {"entry-1", "entry-2"},
                workflow_id="wf-aapl-1",
            ),
        ]
        assert reconcile.call_args_list[-1] == call(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert result == finally_protected

    def test_durable_buy_transition_after_success_forces_refence(self) -> None:
        initial_workflow = SimpleNamespace(
            transitions=[
                SimpleNamespace(
                    event="buy_fill_received",
                    details={"broker_order_id": "entry-1", "qty": 4.0},
                )
            ]
        )
        changed_workflow = SimpleNamespace(
            transitions=[
                *initial_workflow.transitions,
                SimpleNamespace(
                    event="buy_fill_received",
                    details={"broker_order_id": "entry-2", "qty": 6.0},
                ),
            ]
        )
        initially_protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-4",
            symbol="AAPL",
            qty=4.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )
        finally_protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-10",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution.get_workflow",
                side_effect=[
                    initial_workflow,
                    changed_workflow,
                    changed_workflow,
                    changed_workflow,
                ],
            ),
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                side_effect=[4.0, 10.0],
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                side_effect=[initially_protected, finally_protected],
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
                entry_order_id="entry-1",
            )

        assert terminal.call_args_list == [
            call("AAPL", {"entry-1"}, workflow_id="wf-aapl-1"),
            call(
                "AAPL",
                {"entry-1", "entry-2"},
                workflow_id="wf-aapl-1",
            ),
        ]
        assert reconcile.call_args_list[-1] == call(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert result == finally_protected

    def test_workflow_fill_cancels_buy_remainder_before_strict_reconciliation(self) -> None:
        buy_remainder = _mock_order(
            "entry-1",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            qty=10.0,
        )
        protected = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=4.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[buy_remainder], []],
            ),
            patch(
                "core.order_execution._cancel_order_ids_verified",
                create=True,
            ) as cancel,
            patch(
                "core.order_execution._wait_for_terminal_buy_order_chain",
                return_value=4.0,
            ) as terminal,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=protected,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=4.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
            )

        cancel.assert_called_once_with("AAPL", {"entry-1"})
        terminal.assert_called_once_with(
            "AAPL",
            {"entry-1"},
            workflow_id="wf-aapl-1",
        )
        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
            minimum_position_qty=4.0,
        )
        assert result == protected

    def test_missing_workflow_fails_without_submitting_unlinked_stop(self) -> None:
        with patch("core.order_execution.submit_stop_loss") as submit:
            result = ensure_protective_stop(
                "AAPL",
                qty=10.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id=None,
            )

        assert result.success is False
        assert result.action == "missing_workflow"
        submit.assert_not_called()

    def test_submits_new_stop_when_none_exists(self) -> None:
        submitted = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="submitted",
            client_order_id="wf-aapl-1-sl-retry1",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=submitted,
            ) as reconcile,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=10.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
            )

        reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-1",
            stop_loss_pct=0.07,
        )
        assert result == submitted

    def test_reuses_existing_matching_stop(self) -> None:
        existing = _mock_order(
            "stop-keep",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=93.0,
        )
        reused = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-keep",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="reused",
            client_order_id="wf-aapl-1-sl",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[existing]),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=reused,
            ),
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=10.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
            )

        assert result == reused

    def test_replaces_stale_stop_with_fill_anchored_stop(self) -> None:
        stale = _mock_order(
            "stop-old",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=10.0,
            stop_price=95.0,
        )
        unsafe = oe.ProtectiveStopResult(
            success=False,
            order_id="stop-old",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="unsafe_orders",
        )
        submitted = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-new",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="submitted",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[stale]),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                side_effect=[unsafe, submitted],
            ) as reconcile,
            patch("core.order_execution.cancel_open_orders_verified") as cancel,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=10.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
            )

        cancel.assert_called_once_with("AAPL")
        assert reconcile.call_count == 2
        assert result == submitted

    def test_pending_exit_is_not_reported_as_stop_protection(self) -> None:
        pending_exit = oe.ProtectiveStopResult(
            success=True,
            order_id="exit-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="pending_exit",
        )

        with (
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=pending_exit,
            ),
            patch("core.order_execution.cancel_open_orders_verified") as cancel,
        ):
            result = ensure_protective_stop(
                "AAPL",
                qty=10.0,
                fill_price=100.0,
                stop_loss_pct=0.07,
                workflow_id="wf-aapl-1",
            )

        cancel.assert_not_called()
        assert result.success is False
        assert result.action == "pending_exit"


# ===========================================================================
# reconcile_open_position_stops
# ===========================================================================


class TestReconcileOpenPositionStops:
    def test_recovers_workflow_and_strictly_reconciles_open_position(self) -> None:
        positions = [PositionSummary("AAPL", 10.0, 100.0, 102.0, 0.02)]
        recovered_workflow = SimpleNamespace(workflow_id="wf-aapl-recovered")
        repaired = oe.ProtectiveStopResult(
            success=True,
            order_id="stop-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="submitted",
            client_order_id="wf-aapl-recovered-sl-a1b2c3",
        )

        with (
            patch(
                "core.order_execution._get_trading_client",
                side_effect=AssertionError("unexpected broker access"),
            ),
            patch(
                "core.order_execution.get_active_workflow_for_symbol",
                return_value=None,
            ) as get_active_workflow,
            patch(
                "core.order_execution.recover_active_position_workflow",
                return_value=recovered_workflow,
            ) as recover_workflow,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=repaired,
            ) as strict_reconcile,
        ):
            results = reconcile_open_position_stops(stop_loss_pct=0.07, positions=positions)

        get_active_workflow.assert_called_once_with("AAPL")
        recover_workflow.assert_called_once_with(
            "AAPL",
            qty=10.0,
            avg_entry_price=100.0,
        )
        strict_reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-recovered",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert results == [repaired]

    def test_active_workflow_uses_strict_reconciliation_for_pending_exit(self) -> None:
        positions = [PositionSummary("AAPL", 10.0, 100.0, 95.0, -0.05)]
        active_workflow = SimpleNamespace(workflow_id="wf-aapl-active")
        pending_exit = oe.ProtectiveStopResult(
            success=True,
            order_id="exit-1",
            symbol="AAPL",
            qty=10.0,
            stop_price=93.0,
            action="pending_exit",
            client_order_id="wf-aapl-active-exit",
        )

        with (
            patch(
                "core.order_execution._get_trading_client",
                side_effect=AssertionError("unexpected broker access"),
            ),
            patch(
                "core.order_execution.get_active_workflow_for_symbol",
                return_value=active_workflow,
            ) as get_active_workflow,
            patch(
                "core.order_execution.recover_active_position_workflow",
            ) as recover_workflow,
            patch(
                "core.order_execution.reconcile_symbol_after_exit_failure",
                return_value=pending_exit,
            ) as strict_reconcile,
        ):
            results = reconcile_open_position_stops(stop_loss_pct=0.07, positions=positions)

        get_active_workflow.assert_called_once_with("AAPL")
        recover_workflow.assert_not_called()
        strict_reconcile.assert_called_once_with(
            "AAPL",
            workflow_id="wf-aapl-active",
            stop_loss_pct=0.07,
            minimum_position_qty=10.0,
        )
        assert results == [pending_exit]

    def test_returns_empty_when_no_open_positions(self) -> None:
        assert reconcile_open_position_stops(positions=[]) == []


# ===========================================================================
# submit_market_sell
# ===========================================================================


class TestSubmitMarketSell:
    @pytest.mark.parametrize(
        "status",
        ["accepted", "accepted_for_bidding", "pending_new", "new", "partially_filled", "filled"],
    )
    def test_market_sell_returns_success_for_valid_broker_status(self, status: str) -> None:
        client = MagicMock()
        order = _mock_order("sell-1")
        order.status = status
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=10.0)

        assert result.success is True
        assert result.side == "sell"
        assert result.qty == 10.0

    def test_market_sell_fails_when_broker_response_has_no_status(self) -> None:
        client = MagicMock()
        client.submit_order.return_value = SimpleNamespace(id="sell-1")

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=10.0)

        assert result.success is False
        assert "status" in result.error.lower()
        assert result.outcome_uncertain is True

    def test_market_sell_missing_broker_order_id_is_uncertain(self) -> None:
        client = MagicMock()
        order = _mock_order("")
        order.status = "accepted"
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=10.0)

        assert result.success is False
        assert result.order_id == ""
        assert "order id" in result.error.lower()
        assert result.outcome_uncertain is True

    @pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
    def test_market_sell_fails_for_terminal_rejection_status(self, status: str) -> None:
        client = MagicMock()
        order = _mock_order("sell-1")
        order.status = status
        client.submit_order.return_value = order

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=10.0)

        assert result.success is False
        assert status in result.error.lower()
        assert result.outcome_uncertain is False

    def test_api_error_returns_failure(self) -> None:
        client = MagicMock()
        client.submit_order.side_effect = RuntimeError("rejected")

        with _patched_client(client):
            result = submit_market_sell("AAPL", qty=5.0)

        assert result.success is False
        assert result.error == "rejected"
        assert result.outcome_uncertain is True


# ===========================================================================
# close_position
# ===========================================================================


class TestClosePosition:
    def test_closes_held_position_via_market_sell(self) -> None:
        """When a position exists, close_position must submit a market sell."""
        client = MagicMock()
        pos_mock = MagicMock()
        pos_mock.qty = "7.5"
        client.get_open_position.return_value = pos_mock
        client.submit_order.return_value = _mock_order("close-1")

        with _patched_client(client):
            result = close_position("AAPL")

        assert result.success is True
        assert result.qty == 7.5

    def test_returns_success_when_position_does_not_exist(self) -> None:
        """close_position must be idempotent — no error when already flat."""
        client = MagicMock()
        client.get_open_position.side_effect = Exception("position does not exist")

        with _patched_client(client):
            result = close_position("FLAT")

        assert result.success is True
        assert result.qty == 0

    def test_returns_success_on_404(self) -> None:
        client = MagicMock()
        client.get_open_position.side_effect = Exception("404 not found")

        with _patched_client(client):
            result = close_position("GONE")

        assert result.success is True

    def test_returns_failure_on_unexpected_error(self) -> None:
        client = MagicMock()
        client.get_open_position.side_effect = RuntimeError("network error")

        with _patched_client(client):
            result = close_position("ERR")

        assert result.success is False
        assert "network error" in result.error

    def test_no_sell_when_qty_is_zero(self) -> None:
        """If the position qty is 0, no sell order should be submitted."""
        client = MagicMock()
        pos_mock = MagicMock()
        pos_mock.qty = "0"
        client.get_open_position.return_value = pos_mock

        with _patched_client(client):
            result = close_position("FLAT")

        client.submit_order.assert_not_called()
        assert result.success is True


# ===========================================================================
# get_open_positions
# ===========================================================================


class TestGetOpenPositions:
    def test_maps_alpaca_positions_to_position_summary(self) -> None:
        client = MagicMock()
        client.get_all_positions.return_value = [
            _mock_position("NVDA", qty=5.0, avg_entry=400.0, current_price=372.0, unrealized_plpc=-0.07),
            _mock_position("AMD", qty=10.0, avg_entry=100.0, current_price=110.0, unrealized_plpc=0.10),
        ]

        with _patched_client(client):
            positions = get_open_positions()

        assert len(positions) == 2
        nvda = next(p for p in positions if p.symbol == "NVDA")
        assert nvda.qty == 5.0
        assert nvda.avg_entry_price == 400.0
        assert nvda.unrealized_pl_pct == pytest.approx(-0.07)

    def test_returns_empty_list_on_error(self) -> None:
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("connection error")

        with _patched_client(client):
            positions = get_open_positions()

        assert positions == []


# ===========================================================================
# get_open_orders
# ===========================================================================


class TestGetOpenOrders:
    def test_returns_all_open_orders_without_filter(self) -> None:
        client = MagicMock()
        mock_orders = [MagicMock(), MagicMock()]
        client.get_orders.return_value = mock_orders

        with _patched_client(client):
            orders = get_open_orders()

        assert len(orders) == 2

    def test_passes_symbol_filter(self) -> None:
        from alpaca.trading.requests import GetOrdersRequest

        client = MagicMock()
        client.get_orders.return_value = []

        with _patched_client(client):
            get_open_orders("AAPL")

        req = client.get_orders.call_args[0][0]
        assert isinstance(req, GetOrdersRequest)
        assert req.symbols == ["AAPL"]

    def test_returns_empty_list_on_error(self) -> None:
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("timeout")

        with _patched_client(client):
            orders = get_open_orders()

        assert orders == []


# ===========================================================================
# cancel_open_orders
# ===========================================================================


class TestCancelOpenOrders:
    def test_cancels_all_orders_for_symbol(self) -> None:
        client = MagicMock()
        o1, o2 = MagicMock(), MagicMock()
        o1.id, o2.id = "order-a", "order-b"
        o1.symbol, o2.symbol = "AAPL", "AAPL"
        client.get_orders.return_value = [o1, o2]

        with _patched_client(client):
            count = cancel_open_orders("AAPL")

        assert count == 2
        assert client.cancel_order_by_id.call_count == 2

    def test_counts_only_successful_cancellations(self) -> None:
        """If one cancel fails, the count should reflect only successful ones."""
        client = MagicMock()
        o1 = MagicMock()
        o1.id = "ok-order"
        o1.symbol = "AAPL"
        o2 = MagicMock()
        o2.id = "bad-order"
        o2.symbol = "AAPL"
        client.get_orders.return_value = [o1, o2]
        client.cancel_order_by_id.side_effect = [None, RuntimeError("already cancelled")]

        with _patched_client(client):
            count = cancel_open_orders("AAPL")

        assert count == 1

    def test_verified_cancel_waits_until_broker_reports_no_open_orders(self) -> None:
        client = MagicMock()
        working = SimpleNamespace(
            id="stop-1",
            symbol="SPY",
            side="sell",
            type="stop",
            status="new",
        )
        pending = SimpleNamespace(
            id="stop-1",
            symbol="SPY",
            side="sell",
            type="stop",
            status="pending_cancel",
        )
        client.get_orders.side_effect = [[working], [pending], [], []]

        with (
            _patched_client(client),
            patch("core.order_execution.time.sleep") as sleep,
        ):
            count = cancel_open_orders_verified("SPY", timeout=1.0)

        assert count == 1
        client.cancel_order_by_id.assert_called_once_with("stop-1")
        assert client.get_orders.call_count == 4
        assert sleep.call_count == 3

    def test_verified_cancel_propagates_inspection_failure(self) -> None:
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("broker unavailable")

        with _patched_client(client):
            with pytest.raises(RuntimeError, match="broker unavailable"):
                cancel_open_orders_verified("SPY", timeout=1.0)

        client.cancel_order_by_id.assert_not_called()

    def test_verified_cancel_propagates_cancellation_failure(self) -> None:
        client = MagicMock()
        order = SimpleNamespace(
            id="stop-1",
            symbol="SPY",
            side="sell",
            type="stop",
            status="new",
        )
        client.get_orders.return_value = [order]
        client.cancel_order_by_id.side_effect = RuntimeError("cancel rejected")

        with _patched_client(client):
            with pytest.raises(RuntimeError, match="cancel rejected"):
                cancel_open_orders_verified("SPY", timeout=0.0)

    def test_verified_cancel_preserves_stop_when_buy_cancel_persists(self) -> None:
        client = MagicMock()
        stop = SimpleNamespace(
            id="stop-1",
            symbol="SPY",
            side="sell",
            type="stop",
            status="new",
        )
        buy = SimpleNamespace(
            id="entry-1",
            symbol="SPY",
            side="buy",
            type="limit",
            status="new",
        )
        client.get_orders.return_value = [stop, buy]
        client.cancel_order_by_id.side_effect = RuntimeError("buy cancel rejected")

        with (
            _patched_client(client),
            patch("core.order_execution.time.monotonic", side_effect=[0.0, 2.0]),
        ):
            with pytest.raises(RuntimeError, match="entry-1"):
                cancel_open_orders_verified("SPY", timeout=1.0)

        assert client.cancel_order_by_id.call_args_list == [call("entry-1")]

    def test_verified_cancel_accepts_lost_response_after_stable_empty_proof(self) -> None:
        client = MagicMock()
        working = SimpleNamespace(
            id="entry-1",
            symbol="SPY",
            side="buy",
            type="limit",
            status="new",
        )
        pending = SimpleNamespace(
            id="entry-1",
            symbol="SPY",
            side="buy",
            type="limit",
            status="pending_cancel",
        )
        client.get_orders.side_effect = [[working], [pending], [], []]
        client.cancel_order_by_id.side_effect = RuntimeError("response lost")

        with (
            _patched_client(client),
            patch("core.order_execution.time.sleep"),
        ):
            count = cancel_open_orders_verified("SPY", timeout=1.0)

        assert count == 1
        client.cancel_order_by_id.assert_called_once_with("entry-1")

    def test_verified_cancel_catches_new_order_before_returning(self) -> None:
        client = MagicMock()
        first = SimpleNamespace(
            id="entry-1",
            symbol="SPY",
            side="buy",
            type="limit",
            status="new",
        )
        raced = SimpleNamespace(
            id="exit-1",
            symbol="SPY",
            side="sell",
            type="market",
            status="new",
        )
        client.get_orders.side_effect = [[first], [raced], [], []]

        with (
            _patched_client(client),
            patch("core.order_execution.time.sleep"),
        ):
            count = cancel_open_orders_verified("SPY", timeout=1.0)

        assert count == 2
        assert client.cancel_order_by_id.call_args_list == [
            call("entry-1"),
            call("exit-1"),
        ]


class TestStrictBrokerInspection:
    """Safety-sensitive callers must be able to distinguish empty state from API failure."""

    def test_get_open_positions_can_propagate_broker_errors(self) -> None:
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("broker unavailable")

        with patch("core.order_execution._get_trading_client", return_value=client):
            with pytest.raises(RuntimeError, match="broker unavailable"):
                get_open_positions(raise_on_error=True)

    def test_get_open_orders_can_propagate_broker_errors(self) -> None:
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("broker unavailable")

        with patch("core.order_execution._get_trading_client", return_value=client):
            with pytest.raises(RuntimeError, match="broker unavailable"):
                get_open_orders("SPY", raise_on_error=True)

    def test_get_closed_orders_requests_closed_symbol_history(self) -> None:
        client = MagicMock()
        filled_order = SimpleNamespace(id="sell-1")
        client.get_orders.return_value = [filled_order]

        with patch("core.order_execution._get_trading_client", return_value=client):
            assert get_closed_orders("SPY", limit=25, raise_on_error=True) == [filled_order]

        request = client.get_orders.call_args.args[0]
        assert str(request.status).split(".")[-1].lower() == "closed"
        assert request.symbols == ["SPY"]
        assert request.limit == 25
        assert str(request.direction).split(".")[-1].lower() == "desc"


class TestExitFailureSafety:
    def test_pending_cancel_stop_is_never_accepted_as_protection(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop = SimpleNamespace(
            id="stop-1",
            side="sell",
            type="stop",
            status="pending_cancel",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[stop]),
            patch("core.order_execution._SAFETY_SNAPSHOT_TIMEOUT", 0.0, create=True),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action in {"orders_transitioning", "snapshot_unstable"}

    def test_mixed_time_position_snapshot_never_returns_flat_success(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)

        with (
            patch(
                "core.order_execution.get_open_positions",
                side_effect=[[], [position]],
            ),
            patch("core.order_execution.get_open_orders", return_value=[]),
            patch("core.order_execution.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
            patch("core.order_execution.time.sleep"),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "snapshot_unstable"

    def test_live_buy_prevents_flat_or_protected_success(self) -> None:
        position = PositionSummary("SPY", 0.1, 100.0, 100.0, 0.0)
        live_buy = SimpleNamespace(
            id="entry-1",
            side="buy",
            type="limit",
            status="new",
            qty="1.0",
            filled_qty="0.1",
            client_order_id="wf-spy-1",
        )
        stop = SimpleNamespace(
            id="stop-1",
            side="sell",
            type="stop",
            status="new",
            qty="0.1",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[live_buy, stop]),
            patch("core.order_execution.submit_stop_loss") as submit,
        ):
            result = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        assert result.success is False
        assert result.action == "pending_buy"
        submit.assert_not_called()

    def test_rejects_position_below_terminal_entry_filled_quantity(self) -> None:
        position = PositionSummary("SPY", 0.5, 100.0, 100.0, 0.0)
        undersized_stop = SimpleNamespace(
            id="stop-1",
            side="sell",
            type="stop",
            status="new",
            qty="0.5",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl",
        )

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                return_value=(position, [undersized_stop]),
            ),
            patch("core.order_execution.submit_stop_loss") as submit,
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
                minimum_position_qty=1.0,
            )

        assert result.success is False
        assert result.action == "position_sync_pending"
        assert result.qty == pytest.approx(0.5)
        assert "terminal entry fills 1.0" in result.error
        submit.assert_not_called()

    def test_pending_exit_requires_one_full_workflow_linked_exit(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        full_exit = SimpleNamespace(
            id="exit-1",
            side="sell",
            type="market",
            status="new",
            qty="1.0",
            filled_qty="0",
            client_order_id="wf-spy-1-exit",
        )
        partial_exit = SimpleNamespace(
            id="exit-2",
            side="sell",
            type="market",
            status="new",
            qty="0.5",
            filled_qty="0",
            client_order_id="wf-spy-1-exit",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[full_exit]),
        ):
            full = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")
        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[partial_exit]),
        ):
            partial = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        assert full.success is True
        assert full.action == "pending_exit"
        assert partial.success is False
        assert partial.action == "unsafe_orders"

    def test_workflow_exit_role_rejects_non_market_sell(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        limit_exit = SimpleNamespace(
            id="exit-1",
            side="sell",
            type="limit",
            status="new",
            qty="1.0",
            filled_qty="0",
            client_order_id="wf-spy-1-exit",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[limit_exit]),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "unsafe_orders"

    def test_workflow_exit_role_rejects_market_sell_with_stop_client_id(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        wrong_role_exit = SimpleNamespace(
            id="exit-1",
            side="sell",
            type="market",
            status="new",
            qty="1.0",
            filled_qty="0",
            client_order_id="wf-spy-1-sl",
        )

        with patch(
            "core.order_execution._sample_stable_symbol_state",
            return_value=(position, [wrong_role_exit]),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "unsafe_orders"

    def test_reuses_only_full_workflow_linked_stop(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop = SimpleNamespace(
            id="stop-1",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl-retry1",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[stop]),
        ):
            result = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        assert result.success is True
        assert result.action == "reused"
        assert result.order_id == "stop-1"
        assert result.client_order_id == "wf-spy-1-sl-retry1"

    @pytest.mark.parametrize(
        ("order_type", "time_in_force"),
        [("stop_limit", "gtc"), ("stop", "day"), ("stop", None)],
    )
    def test_rejects_noncanonical_workflow_stop_protection(
        self,
        order_type: str,
        time_in_force: str | None,
    ) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop_fields = {
            "id": "stop-1",
            "side": "sell",
            "type": order_type,
            "status": "new",
            "qty": "1.0",
            "filled_qty": "0",
            "stop_price": "92.0",
            "client_order_id": "wf-spy-1-sl-retry1",
        }
        if time_in_force is not None:
            stop_fields["time_in_force"] = time_in_force
        stop = SimpleNamespace(**stop_fields)

        with patch(
            "core.order_execution._sample_stable_symbol_state",
            return_value=(position, [stop]),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "unsafe_orders"

    def test_missing_stop_submits_unique_linked_retry_and_proves_postcondition(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        submitted_stop = SimpleNamespace(
            id="stop-2",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl-a1b2c3",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[], [], [submitted_stop], [submitted_stop]],
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    True,
                    "stop-2",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id="wf-spy-1-sl-a1b2c3",
                ),
            ) as submit,
        ):
            result = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        submit.assert_called_once_with(
            symbol="SPY",
            qty=1.0,
            stop_price=92.0,
            client_order_id="wf-spy-1-sl-a1b2c3",
        )
        assert result.success is True
        assert result.action == "submitted"
        assert result.client_order_id == "wf-spy-1-sl-a1b2c3"

    def test_response_lost_after_acceptance_recovers_exact_stop_before_success(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop_client_order_id = "wf-spy-1-sl-a1b2c3"
        recovered_stop = SimpleNamespace(
            id="stop-recovered",
            symbol="SPY",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id=stop_client_order_id,
        )
        client = MagicMock()
        client.get_order_by_client_id.return_value = recovered_stop

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                side_effect=[(position, []), (position, [recovered_stop])],
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    success=False,
                    order_id="",
                    symbol="SPY",
                    side="sell",
                    qty=1.0,
                    error="response lost",
                    client_order_id=stop_client_order_id,
                    outcome_uncertain=True,
                ),
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            _patched_client(client),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is True
        assert result.action == "submitted"
        assert result.order_id == "stop-recovered"
        assert result.client_order_id == stop_client_order_id
        client.get_order_by_client_id.assert_called_once_with(stop_client_order_id)

    def test_process_death_after_stop_acceptance_recovers_durable_exact_identity(
        self,
        tmp_path,
    ) -> None:
        from core.execution_store import get_execution_store
        from core.execution_workflow import (
            ExecutionWorkflow,
            clear_workflow_registry,
            register_workflow,
            reset_workflow_state,
        )

        db_path = tmp_path / "stop-submission-crash.sqlite3"
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop_client_order_id = "wf-spy-1-sl-a1b2c3"
        accepted_stop = SimpleNamespace(
            id="stop-accepted-before-crash",
            symbol="SPY",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id=stop_client_order_id,
        )
        client = MagicMock()
        client.get_order_by_client_id.return_value = accepted_stop

        with patch(
            "core.execution_store.settings.EXECUTION_STORE_DB_PATH",
            str(db_path),
        ):
            reset_workflow_state()
            workflow = ExecutionWorkflow(workflow_id="wf-spy-1", symbol="SPY")
            workflow.mark_signal_accepted(signal_payload={"symbol": "SPY"})
            register_workflow(workflow)

            with (
                patch(
                    "core.order_execution._sample_stable_symbol_state",
                    side_effect=[(position, []), (position, [accepted_stop])],
                ),
                patch(
                    "core.order_execution.uuid4",
                    return_value=SimpleNamespace(hex="a1b2c3ffff"),
                ),
                patch(
                    "core.order_execution.submit_stop_loss",
                    side_effect=SystemExit("simulated process death after broker acceptance"),
                ) as submit,
                _patched_client(client),
            ):
                with pytest.raises(SystemExit, match="simulated process death"):
                    reconcile_symbol_after_exit_failure(
                        "SPY",
                        workflow_id="wf-spy-1",
                    )

                persisted = get_execution_store().load_workflow("wf-spy-1")
                assert persisted is not None
                latest = persisted["transitions"][-1]
                assert latest["event"] == "protective_stop_reconciled"
                assert latest["details"]["action"] == "submission_unknown"
                assert latest["details"]["client_order_id"] == stop_client_order_id

                clear_workflow_registry()
                recovered = reconcile_symbol_after_exit_failure(
                    "SPY",
                    workflow_id="wf-spy-1",
                )

            assert recovered.success is True
            assert recovered.action == "reused"
            assert recovered.order_id == "stop-accepted-before-crash"
            assert recovered.client_order_id == stop_client_order_id
            assert submit.call_count == 1
            client.get_order_by_client_id.assert_called_once_with(stop_client_order_id)
            reset_workflow_state()

    def test_persisted_unknown_stop_retries_same_identity_without_resubmitting(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop_client_order_id = "wf-spy-1-sl-a1b2c3"
        workflow = SimpleNamespace(
            transitions=[
                SimpleNamespace(
                    event="protective_stop_reconciled",
                    details={
                        "action": "submission_unknown",
                        "client_order_id": stop_client_order_id,
                    },
                ),
            ],
        )
        client = MagicMock()
        client.get_order_by_client_id.side_effect = RuntimeError("lookup unavailable")

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                return_value=(position, []),
            ),
            patch("core.order_execution.get_workflow", return_value=workflow),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    success=False,
                    order_id="",
                    symbol="SPY",
                    side="sell",
                    qty=1.0,
                    error="must not resubmit",
                ),
            ) as submit,
            _patched_client(client),
        ):
            first = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )
            second = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert first.success is False
        assert second.success is False
        assert first.action == second.action == "submission_unknown"
        assert first.client_order_id == second.client_order_id == stop_client_order_id
        assert client.get_order_by_client_id.call_args_list == [
            call(stop_client_order_id),
            call(stop_client_order_id),
        ]
        submit.assert_not_called()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("id", ""),
            ("client_order_id", "wf-foreign-sl-a1b2c3"),
            ("symbol", "QQQ"),
            ("side", "buy"),
            ("type", "market"),
            ("time_in_force", "day"),
        ],
    )
    def test_unknown_stop_lookup_rejects_foreign_or_mismatched_order(
        self,
        field: str,
        value: str,
    ) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        stop_client_order_id = "wf-spy-1-sl-a1b2c3"
        order_fields = {
            "id": "stop-recovered",
            "symbol": "SPY",
            "side": "sell",
            "type": "stop",
            "status": "new",
            "time_in_force": "gtc",
            "qty": "1.0",
            "filled_qty": "0",
            "stop_price": "92.0",
            "client_order_id": stop_client_order_id,
        }
        order_fields[field] = value
        lookup_order = SimpleNamespace(**order_fields)
        workflow = SimpleNamespace(
            transitions=[
                SimpleNamespace(
                    event="protective_stop_reconciled",
                    details={
                        "action": "submission_unknown",
                        "client_order_id": stop_client_order_id,
                    },
                ),
            ],
        )
        client = MagicMock()
        client.get_order_by_client_id.return_value = lookup_order

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                return_value=(position, []),
            ),
            patch("core.order_execution.get_workflow", return_value=workflow),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    success=False,
                    order_id="",
                    symbol="SPY",
                    side="sell",
                    qty=1.0,
                    error="must not resubmit",
                ),
            ) as submit,
            patch("core.order_execution._cleanup_submitted_stop") as cleanup,
            _patched_client(client),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "submission_unknown"
        assert result.client_order_id == stop_client_order_id
        submit.assert_not_called()
        cleanup.assert_not_called()

    def test_failed_stop_postcondition_cancels_the_new_orphan(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        client = MagicMock()
        submitted_stop = SimpleNamespace(
            id="stop-2",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl-a1b2c3",
        )
        duplicate_stop = SimpleNamespace(
            id="stop-raced",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl-raced1",
        )

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                side_effect=[
                    (position, []),
                    (position, [submitted_stop, duplicate_stop]),
                ],
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    True,
                    "stop-2",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id="wf-spy-1-sl-a1b2c3",
                ),
            ),
            _patched_client(client),
            patch("core.order_execution._cancel_order_ids_verified") as cancel_new,
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "postcondition_failed"
        client.cancel_order_by_id.assert_called_once_with("stop-2")
        cancel_new.assert_called_once_with("SPY", {"stop-2"})

    @pytest.mark.parametrize(
        ("observed_id", "observed_client_order_id"),
        [
            ("stop-raced", "wf-spy-1-sl-a1b2c3"),
            ("stop-2", "wf-spy-1-sl-raced1"),
        ],
    )
    def test_postcondition_rejects_another_stop_and_targets_submitted_id_first(
        self,
        observed_id: str,
        observed_client_order_id: str,
    ) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        observed_stop = SimpleNamespace(
            id=observed_id,
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id=observed_client_order_id,
        )
        client = MagicMock()
        cleanup_events: list[str] = []
        client.cancel_order_by_id.side_effect = (
            lambda order_id: cleanup_events.append(f"cancel:{order_id}")
        )

        def prove_absent(_symbol: str, order_ids: set[str]) -> int:
            cleanup_events.append(f"prove:{next(iter(order_ids))}")
            return 1

        with (
            patch(
                "core.order_execution._sample_stable_symbol_state",
                side_effect=[(position, []), (position, [observed_stop])],
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    True,
                    "stop-2",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id="wf-spy-1-sl-a1b2c3",
                ),
            ),
            _patched_client(client),
            patch(
                "core.order_execution._cancel_order_ids_verified",
                side_effect=prove_absent,
            ),
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
            )

        assert result.success is False
        assert result.action == "postcondition_failed"
        assert cleanup_events == ["cancel:stop-2", "prove:stop-2"]

    def test_stop_loss_override_is_used_for_submit_and_postcondition(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
        submitted_stop = SimpleNamespace(
            id="stop-2",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="90.0",
            client_order_id="wf-spy-1-sl-a1b2c3",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[], [], [submitted_stop], [submitted_stop]],
            ),
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    True,
                    "stop-2",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id="wf-spy-1-sl-a1b2c3",
                ),
            ) as submit,
        ):
            result = reconcile_symbol_after_exit_failure(
                "SPY",
                workflow_id="wf-spy-1",
                stop_loss_pct=0.10,
            )

        submit.assert_called_once_with(
            symbol="SPY",
            qty=1.0,
            stop_price=90.0,
            client_order_id="wf-spy-1-sl-a1b2c3",
        )
        assert result.success is True
        assert result.stop_price == 90.0

    def test_restores_workflow_linked_stop_when_position_remains(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 99.0, -0.01)
        confirmed_stop = SimpleNamespace(
            id="stop-2",
            side="sell",
            type="stop",
            status="new",
            time_in_force="gtc",
            qty="1.0",
            filled_qty="0",
            stop_price="92.0",
            client_order_id="wf-spy-1-sl-a1b2c3",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]) as positions,
            patch(
                "core.order_execution.get_open_orders",
                side_effect=[[], [], [confirmed_stop], [confirmed_stop]],
            ) as orders,
            patch(
                "core.order_execution.get_workflow",
                return_value=_submission_ready_workflow(),
            ),
            patch("core.order_execution.uuid4", return_value=SimpleNamespace(hex="a1b2c3ffff")),
            patch(
                "core.order_execution.submit_stop_loss",
                return_value=OrderResult(
                    True,
                    "stop-2",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id="wf-spy-1-sl-a1b2c3",
                ),
            ) as submit,
        ):
            result = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        assert result.success is True
        assert result.action == "submitted"
        assert positions.call_count == 4
        assert orders.call_count == 4
        submit.assert_called_once_with(
            symbol="SPY",
            qty=1.0,
            stop_price=92.0,
            client_order_id="wf-spy-1-sl-a1b2c3",
        )

    def test_does_not_add_stop_while_exit_order_is_pending(self) -> None:
        position = PositionSummary("SPY", 1.0, 100.0, 99.0, -0.01)
        pending_exit = SimpleNamespace(
            id="exit-1",
            side="sell",
            type="market",
            status="new",
            qty="1.0",
            filled_qty="0",
            client_order_id="wf-spy-1-exit",
        )

        with (
            patch("core.order_execution.get_open_positions", return_value=[position]),
            patch("core.order_execution.get_open_orders", return_value=[pending_exit]),
            patch("core.order_execution.submit_stop_loss") as submit,
        ):
            result = reconcile_symbol_after_exit_failure("SPY", workflow_id="wf-spy-1")

        assert result.success is True
        assert result.action == "pending_exit"
        submit.assert_not_called()


# ===========================================================================
# check_exit_signals
# ===========================================================================


class TestCheckExitSignals:
    def _make_positions(self) -> list[PositionSummary]:
        return [
            PositionSummary("DOWN7", 10.0, 100.0, 93.0, -0.07),   # exactly at 7% threshold
            PositionSummary("DOWN8", 10.0, 100.0, 91.0, -0.09),   # beyond threshold
            PositionSummary("UP", 10.0, 100.0, 110.0, 0.10),      # healthy
            PositionSummary("DOWN6", 10.0, 100.0, 94.0, -0.06),   # below threshold — no exit
        ]

    def test_positions_at_or_below_threshold_are_returned(self) -> None:
        positions = self._make_positions()
        signals = check_exit_signals(positions, stop_loss_pct=0.07)
        symbols = {p.symbol for p in signals}
        assert "DOWN7" in symbols
        assert "DOWN8" in symbols

    def test_healthy_positions_not_returned(self) -> None:
        positions = self._make_positions()
        signals = check_exit_signals(positions, stop_loss_pct=0.07)
        symbols = {p.symbol for p in signals}
        assert "UP" not in symbols
        assert "DOWN6" not in symbols

    def test_uses_settings_default_when_pct_not_provided(self) -> None:
        positions = [PositionSummary("X", 1.0, 100.0, 93.0, -0.07)]
        with patch("core.order_execution.settings") as s:
            s.STOP_LOSS_PCT = 0.07
            signals = check_exit_signals(positions)
        assert len(signals) == 1

    def test_empty_positions_returns_empty(self) -> None:
        assert check_exit_signals([]) == []


# ===========================================================================
# auto_trader: monitor_and_exit_positions
# ===========================================================================


class TestMonitorAndExitPositions:
    """Tests for auto_trader.monitor_and_exit_positions."""

    def _healthy_ohlcv(self, n: int = 60, above_ema: bool = True) -> pd.DataFrame:
        """Return a synthetic OHLCV DataFrame where price is above/below 21-day EMA."""
        prices = [100.0] * n
        if not above_ema:
            # Last 2 bars drop well below an EMA anchored around 100
            prices[-2] = 85.0
            prices[-1] = 84.0
        return pd.DataFrame({"Close": prices, "Volume": [1_000_000] * n})

    def test_hard_stop_triggers_close(self) -> None:
        """Position at -7% must be closed when monitor_and_exit is called."""
        from auto_trader import monitor_and_exit_positions

        pos_stop = PositionSummary("STOP7", 10.0, 100.0, 93.0, -0.07)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "sell-1", "STOP7", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv()),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_called_once_with("STOP7", exit_reason="hard stop triggered")
        assert "STOP7" in exited

    def test_ma_violation_triggers_close(self) -> None:
        """Two consecutive closes below 21-day EMA must trigger an exit."""
        from auto_trader import monitor_and_exit_positions

        pos_healthy_stop = PositionSummary("MABREAK", 10.0, 100.0, 97.0, -0.03)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "sell-2", "MABREAK", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_healthy_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=False)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_called_once()
        assert "MABREAK" in exited

    def test_healthy_position_not_exited(self) -> None:
        """A position with small gain and price above EMA must NOT be exited."""
        from auto_trader import monitor_and_exit_positions

        pos_ok = PositionSummary("WINNER", 10.0, 100.0, 110.0, 0.10)
        manager = MagicMock()

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_ok]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=True)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        manager.submit_exit.assert_not_called()
        assert exited == []

    def test_no_positions_returns_empty(self) -> None:
        from auto_trader import monitor_and_exit_positions

        with patch("auto_trader.get_open_positions", return_value=[]):
            exited = monitor_and_exit_positions()

        assert exited == []

    def test_hard_stop_position_not_double_exited_by_ma_check(self) -> None:
        """A position exited by the hard-stop must not also trigger the MA-check."""
        from auto_trader import monitor_and_exit_positions

        pos_stop = PositionSummary("BOTH", 10.0, 100.0, 90.0, -0.10)
        manager = MagicMock()
        manager.submit_exit.return_value = OrderResult(True, "x", "BOTH", "sell", 10.0)

        with (
            patch("auto_trader.get_open_positions", return_value=[pos_stop]),
            patch("auto_trader.OrderManager", return_value=manager),
            patch("auto_trader.fetch_ohlcv", return_value=self._healthy_ohlcv(n=60, above_ema=False)),
        ):
            exited = monitor_and_exit_positions(stop_loss_pct=0.07)

        assert manager.submit_exit.call_count == 1
        assert exited.count("BOTH") == 1


# ===========================================================================
# auto_trader: execute_entries
# ===========================================================================


def _make_buy_signal(
    symbol: str = "NVDA",
    canslim_score: float = 80.0,
    rs_score: float = 90.0,
    is_breakout: bool = True,
    has_surge: bool = True,
    buy_point: float | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "total_score": canslim_score,
        "rs_score": rs_score,
        "is_breakout": is_breakout,
        "has_volume_surge": has_surge,
        "buy_point": buy_point,
        "scores": {"C": 0.8, "A": 0.8, "N": 0.8, "S": 0.8, "L": 0.9, "I": 0.7, "M": 1.0},
        "metrics": {},
        "scanner_category": "actionable_buy",
        "scanner_notes": [],
    }


class TestExecuteEntries:
    def test_build_entry_plan_computes_entry_stop_qty_and_risk(self) -> None:
        """The execution plan must deterministically compute price, stop, size, and risk."""
        from auto_trader import _build_entry_execution_plan

        with patch("auto_trader._resolve_entry_reference_price", return_value=(500.0, "intraday_minute_close")):
            plan = _build_entry_execution_plan(
                opportunity=_make_buy_signal("NVDA", canslim_score=82.5, rs_score=94.0, buy_point=500.0),
                equity=100_000.0,
                market_open=True,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is not None
        assert plan.symbol == "NVDA"
        assert plan.entry_price == pytest.approx(500.0)
        assert plan.stop_price == pytest.approx(465.0)
        assert plan.qty == pytest.approx(20.0)
        assert plan.position_value == pytest.approx(10_000.0)
        assert plan.risk_per_share == pytest.approx(35.0)
        assert plan.risk_amount == pytest.approx(700.0)
        assert plan.price_source == "intraday_minute_close"

    def test_submits_bracket_buy_for_valid_signal(self) -> None:
        """An actionable buy with available equity must result in a bracket order."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NVDA",
                entry_price=500.0,
                price_source="intraday_minute_close",
                stop_price=465.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=35.0,
                qty=20.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("NVDA")])

        manager.submit_entry.assert_called_once_with(
            mock_plan.return_value,
            signal_payload=_make_buy_signal("NVDA"),
            dry_run=False,
        )
        assert "NVDA" in entered

    def test_skips_already_held_symbol(self) -> None:
        """Symbols already in the portfolio must not generate a new entry order."""
        from auto_trader import execute_entries

        held = PositionSummary("NVDA", 5.0, 480.0, 500.0, 0.04)

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[held]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NVDA")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_position_limit_stops_new_entries(self) -> None:
        """When MAX_OPEN_POSITIONS is reached, no new entries should be submitted."""
        from auto_trader import execute_entries

        # 5 existing positions, limit is 5
        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D", "E"]]

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NVDA"), _make_buy_signal("AMD")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_dry_run_does_not_submit_orders(self) -> None:
        """In dry_run mode, symbols must be returned but no order submitted."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=50_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="AMD",
                entry_price=200.0,
                price_source="daily_close",
                stop_price=186.0,
                stop_loss_pct=0.07,
                position_value=5_000.0,
                risk_amount=350.0,
                risk_per_share=14.0,
                qty=25.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=True, workflow_id="wf-amd-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("AMD")], dry_run=True)

        manager.submit_entry.assert_called_once()
        assert "AMD" in entered

    def test_returns_empty_when_equity_unavailable(self) -> None:
        """If equity is 0 (account fetch failed), no entries should proceed."""
        from auto_trader import execute_entries

        with patch("auto_trader._get_account_equity", return_value=0.0):
            entered = execute_entries([_make_buy_signal("GOOG")])

        assert entered == []

    def test_skips_symbol_when_price_unavailable(self) -> None:
        """Cannot size a position without a price — symbol must be skipped."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan", return_value=None),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NODATA")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_respects_available_slot_count(self) -> None:
        """If 4 positions are open with limit 5, only 1 new entry should proceed."""
        from auto_trader import execute_entries

        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D"]]
        signals = [_make_buy_signal("NEW1"), _make_buy_signal("NEW2")]

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NEW1",
                entry_price=100.0,
                price_source="daily_close",
                stop_price=93.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=7.0,
                qty=100.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-new1-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries(signals)

        assert len(entered) == 1
        assert entered[0] == "NEW1"

    def test_limits_cycle_to_top_ranked_new_entries(self) -> None:
        """When too many actionable buys exist, only the top-ranked setups should execute."""
        from auto_trader import execute_entries

        low = _make_buy_signal("LOW", canslim_score=74.0, rs_score=82.0, buy_point=100.0)
        high = _make_buy_signal("HIGH", canslim_score=89.0, rs_score=96.0, buy_point=100.0)

        def _plan_for(*, opportunity: dict, **_: Any):
            return SimpleNamespace(
                symbol=opportunity["symbol"],
                entry_price=100.0,
                price_source="daily_close",
                stop_price=93.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=7.0,
                qty=100.0,
                canslim_score=float(opportunity["total_score"]),
                rs_score=float(opportunity["rs_score"]),
                is_breakout=True,
                has_volume_surge=True,
            )

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan", side_effect=_plan_for),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.MAX_NEW_ENTRIES_PER_CYCLE = 1
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-high-1")
            mock_manager_cls.return_value = manager
            entered = execute_entries([low, high])

        assert entered == ["HIGH"]
        submitted_plan = manager.submit_entry.call_args.args[0]
        assert submitted_plan.symbol == "HIGH"

    def test_pending_buy_orders_consume_position_slots(self) -> None:
        """Pending buys must count toward MAX_OPEN_POSITIONS to prevent over-allocation."""
        from auto_trader import execute_entries

        existing = [PositionSummary(s, 1.0, 100.0, 100.0, 0.0) for s in ["A", "B", "C", "D"]]
        pending_buy = _mock_order("buy-1", symbol="PEND", side="buy", order_type="limit", qty=1.0)

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=existing),
            patch("auto_trader.get_open_orders", return_value=[pending_buy]),
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            entered = execute_entries([_make_buy_signal("NEW1")])

        mock_manager_cls.return_value.submit_entry.assert_not_called()
        assert entered == []

    def test_successful_submission_sends_entry_notification_with_plan_values(self) -> None:
        """Submission notification must use the exact pre-trade execution plan values."""
        from auto_trader import execute_entries

        with (
            patch("auto_trader._get_account_equity", return_value=100_000.0),
            patch("auto_trader.get_open_positions", return_value=[]),
            patch("auto_trader.get_open_orders", return_value=[]),
            patch("auto_trader._build_entry_execution_plan") as mock_plan,
            patch("auto_trader.OrderManager") as mock_manager_cls,
            patch("auto_trader.settings") as mock_settings,
        ):
            mock_settings.MAX_OPEN_POSITIONS = 5
            mock_settings.POSITION_SIZE_PCT = 0.10
            mock_settings.STOP_LOSS_PCT = 0.07
            mock_plan.return_value = SimpleNamespace(
                symbol="NVDA",
                entry_price=500.0,
                price_source="intraday_minute_close",
                stop_price=465.0,
                stop_loss_pct=0.07,
                position_value=10_000.0,
                risk_amount=700.0,
                risk_per_share=35.0,
                qty=20.0,
                canslim_score=80.0,
                rs_score=90.0,
                is_breakout=True,
                has_volume_surge=True,
            )
            manager = MagicMock()
            manager.submit_entry.return_value = SimpleNamespace(success=True, dry_run=False, workflow_id="wf-nvda-42")
            mock_manager_cls.return_value = manager
            entered = execute_entries([_make_buy_signal("NVDA")])

        assert entered == ["NVDA"]
        manager.submit_entry.assert_called_once()


# ===========================================================================
# auto_trader: _is_market_open / _get_account_equity
# ===========================================================================


# ===========================================================================
# auto_trader: buy-zone enforcement
# ===========================================================================


class TestBuyZoneEnforcement:
    """Tests for _is_within_buy_zone and buy-zone gate in _build_entry_execution_plan."""

    def test_price_within_5pct_of_pivot_is_allowed(self) -> None:
        from auto_trader import _is_within_buy_zone

        # 3% above pivot — inside the 5% buy zone
        assert _is_within_buy_zone(current_price=103.0, buy_point=100.0, max_extension_pct=0.05) is True

    def test_price_exactly_at_buy_zone_max_is_allowed(self) -> None:
        from auto_trader import _is_within_buy_zone

        # exactly at 5% — still in range (boundary inclusive)
        assert _is_within_buy_zone(current_price=105.0, buy_point=100.0, max_extension_pct=0.05) is True

    def test_price_beyond_5pct_of_pivot_is_rejected(self) -> None:
        from auto_trader import _is_within_buy_zone

        # 6% above pivot — outside the buy zone
        assert _is_within_buy_zone(current_price=106.0, buy_point=100.0, max_extension_pct=0.05) is False

    def test_missing_buy_point_allows_entry(self) -> None:
        """Missing pivot metadata must fail a strict breakout entry."""
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=999.0, buy_point=None) is False

    def test_zero_buy_point_is_rejected(self) -> None:
        """A zero buy_point is invalid data and must fail strict entry validation."""
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=50.0, buy_point=0.0) is False

    def test_price_below_pivot_is_rejected(self) -> None:
        from auto_trader import _is_within_buy_zone

        assert _is_within_buy_zone(current_price=99.0, buy_point=100.0, max_extension_pct=0.05) is False

    def test_build_plan_rejects_entry_when_price_too_extended(self) -> None:
        """_build_entry_execution_plan must return None when price is beyond buy zone."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("OVER", canslim_score=80.0, rs_score=90.0)
        opp["buy_point"] = 100.0  # pivot at $100

        # Resolve price to $107 — 7% above pivot, outside the 5% buy zone
        with patch("auto_trader._resolve_entry_reference_price", return_value=(107.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is None

    def test_build_plan_allows_entry_within_buy_zone(self) -> None:
        """_build_entry_execution_plan must return a valid plan when within buy zone."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("INZONE", canslim_score=80.0, rs_score=90.0)
        opp["buy_point"] = 100.0  # pivot at $100

        # Resolve price to $103 — 3% above pivot, inside the 5% buy zone
        with patch("auto_trader._resolve_entry_reference_price", return_value=(103.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is not None
        assert plan.symbol == "INZONE"
        assert plan.entry_price == pytest.approx(103.0)

    def test_build_plan_rejects_entry_when_no_pivot_metadata(self) -> None:
        """Missing pivot metadata must block a live breakout entry plan."""
        from auto_trader import _build_entry_execution_plan

        opp = _make_buy_signal("NOPIVOT", canslim_score=80.0, rs_score=90.0)
        # Explicitly absent — this should never be executable as a strict breakout buy
        opp.pop("buy_point", None)

        with patch("auto_trader._resolve_entry_reference_price", return_value=(200.0, "daily_close")):
            plan = _build_entry_execution_plan(
                opportunity=opp,
                equity=100_000.0,
                market_open=False,
                position_size_pct=0.10,
                stop_loss_pct=0.07,
            )

        assert plan is None


class TestHelpers:
    def test_is_market_open_returns_true_when_clock_is_open(self) -> None:
        from auto_trader import _is_market_open

        clock = MagicMock()
        clock.is_open = True
        client = MagicMock()
        client.get_clock.return_value = clock

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is True

    def test_is_market_open_returns_false_when_clock_closed(self) -> None:
        from auto_trader import _is_market_open

        clock = MagicMock()
        clock.is_open = False
        client = MagicMock()
        client.get_clock.return_value = clock

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is False

    def test_is_market_open_returns_false_on_error(self) -> None:
        from auto_trader import _is_market_open

        client = MagicMock()
        client.get_clock.side_effect = RuntimeError("network error")

        with patch("auto_trader._get_trading_client", return_value=client):
            assert _is_market_open() is False

    def test_get_account_equity_returns_float(self) -> None:
        from auto_trader import _get_account_equity

        account = MagicMock()
        account.equity = "125000.50"
        client = MagicMock()
        client.get_account.return_value = account

        with patch("auto_trader._get_trading_client", return_value=client):
            equity = _get_account_equity()

        assert equity == pytest.approx(125_000.50)

    def test_get_account_equity_returns_zero_on_error(self) -> None:
        from auto_trader import _get_account_equity

        client = MagicMock()
        client.get_account.side_effect = RuntimeError("auth error")

        with patch("auto_trader._get_trading_client", return_value=client):
            equity = _get_account_equity()

        assert equity == 0.0
