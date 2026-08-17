"""Safety tests for the supervised paper-account lifecycle check."""

import tempfile
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

import verify_paper_trading as verification
from core.execution_store import get_execution_store
from core.execution_workflow import (
    WorkflowState,
    clear_workflow_registry,
    create_entry_workflow,
    get_workflow,
    reset_workflow_state,
)
from core.order_manager import EntrySubmissionOutcome
from core.order_execution import OrderResult, ProtectiveStopResult


def _persist_split_liquidation_marker_gap(
    *,
    stop_qty: float = 0.7,
    exit_qty: float = 0.3,
) -> tuple[object, object, object]:
    """Persist a final-exit marker crash after an earlier stop partial."""
    workflow = create_entry_workflow(
        verification._build_entry_plan("SPY", 100.0),
        signal_payload={"symbol": "SPY"},
    )
    workflow.mark_order_submitted(broker_order_id="entry-1")
    workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
    stop_client_order_id = f"{workflow.workflow_id}-sl"
    workflow.mark_sell_partial_fill(
        qty=stop_qty,
        fill_price=92.0,
        broker_order_id="stop-partial",
        client_order_id=stop_client_order_id,
    )
    workflow.repair_buy_fill_storage(
        qty=max(0.0001, 1.0 - stop_qty),
        fill_price=100.0,
        broker_order_id="entry-1",
        preserve_higher_qty=False,
    )
    exit_client_order_id = f"{workflow.workflow_id}-exit"
    workflow.mark_exit_submission_intent(
        exit_reason="supervised verification cleanup",
        client_order_id=exit_client_order_id,
    )
    workflow.mark_exit_order_submitted(
        exit_reason="supervised verification cleanup",
        broker_order_id="exit-residual",
    )
    workflow.mark_sell_fill(
        qty=exit_qty,
        fill_price=101.0,
        exit_reason="exit order filled",
        broker_order_id="exit-residual",
        client_order_id=exit_client_order_id,
        clear_active=True,
    )
    closed_stop = SimpleNamespace(
        id="stop-partial",
        symbol="SPY",
        side="sell",
        type="stop",
        status="canceled",
        client_order_id=stop_client_order_id,
        filled_qty=str(stop_qty),
        filled_avg_price="92.00",
        filled_at="2026-08-17T14:00:00Z",
        replaces="",
        replaced_by="",
    )
    closed_exit = SimpleNamespace(
        id="exit-residual",
        symbol="SPY",
        side="sell",
        type="market",
        status="filled",
        client_order_id=exit_client_order_id,
        filled_qty=str(exit_qty),
        filled_avg_price="101.00",
        filled_at="2026-08-17T14:00:01Z",
        replaces="",
        replaced_by="",
    )
    return workflow, closed_stop, closed_exit


def test_main_requires_explicit_execute_gate_before_broker_access() -> None:
    with patch("verify_paper_trading._get_trading_client") as client:
        assert verification.main(execute=False) == 2

    client.assert_not_called()


def test_preflight_blocks_existing_symbol_state() -> None:
    with (
        patch(
            "verify_paper_trading.get_open_positions",
            return_value=[SimpleNamespace(symbol="SPY")],
        ) as mock_positions,
        patch(
            "verify_paper_trading.get_open_orders",
            return_value=[SimpleNamespace(symbol="SPY", id="existing-order")],
        ) as mock_orders,
    ):
        assert verification._preflight_symbol_clear("SPY") is False

    mock_positions.assert_called_once_with(raise_on_error=True)
    mock_orders.assert_called_once_with("SPY", raise_on_error=True)


def test_preflight_blocks_unresolved_submission_intent() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = [
        {
            "workflow_id": "wf-spy-unknown",
            "symbol": "SPY",
            "event": "entry_submission_intent",
            "details": {"client_order_id": "wf-spy-unknown"},
        }
    ]
    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch("verify_paper_trading.get_execution_store", return_value=store),
    ):
        assert verification._preflight_symbol_clear("SPY") is False

    store.load_pending_submission_intents.assert_called_once_with(symbol="SPY")


def test_closed_market_never_runs_cleanup_or_submits() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=False),
        patch("verify_paper_trading._cleanup_test_symbol") as mock_cleanup,
        patch("verify_paper_trading.OrderManager") as mock_manager,
    ):
        assert verification.main(execute=True) == 1

    mock_cleanup.assert_not_called()
    mock_manager.assert_not_called()


def test_stop_preview_uses_configured_risk_default() -> None:
    assert verification._calculate_stop_price(100.0, 0.08) == 92.0


@pytest.mark.parametrize(
    ("qty", "fill_price"),
    [(float("nan"), 100.0), (1.0, float("inf"))],
)
def test_durable_fill_checkpoints_reject_nonfinite_values(
    qty: float,
    fill_price: float,
) -> None:
    snapshot = {
        "transitions": [
            {
                "event": "buy_fill_received",
                "details": {
                    "broker_order_id": "entry-1",
                    "client_order_id": "wf-spy-1",
                    "qty": qty,
                    "fill_price": fill_price,
                },
            }
        ]
    }

    with pytest.raises(RuntimeError, match="unsafe data"):
        verification._durable_fill_checkpoints_by_order(
            snapshot,
            events={"buy_fill_received"},
        )


def test_alpaca_enum_side_is_normalized_for_fill_detection() -> None:
    assert verification._normalize_side("OrderSide.BUY") == "buy"


def test_price_provider_failure_never_starts_monitor_or_cleanup() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor_class = MagicMock()

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", monitor_class),
        patch("verify_paper_trading.notify_configured", return_value=False),
        patch(
            "core.data_client.fetch_latest_intraday_price",
            side_effect=RuntimeError("price provider unavailable"),
        ),
        patch("verify_paper_trading._cleanup_test_symbol") as mock_cleanup,
    ):
        assert verification.main(execute=True) == 1

    monitor_class.assert_not_called()
    mock_cleanup.assert_not_called()


def test_interrupt_after_submission_still_cleans_up_and_stops_monitor() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor = MagicMock()
    manager = MagicMock()
    manager.submit_entry.return_value = EntrySubmissionOutcome(
        symbol="SPY",
        workflow_id="wf-spy-1",
        success=True,
        dry_run=False,
        order_id="order-1",
    )

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=monitor),
        patch("verify_paper_trading.OrderManager", return_value=manager),
        patch("verify_paper_trading.notify_configured", return_value=False),
        patch("core.data_client.fetch_latest_intraday_price", return_value=100.0),
        patch("verify_paper_trading._wait_for_durable_entry", side_effect=KeyboardInterrupt),
        patch("verify_paper_trading._cleanup_test_symbol", return_value=True) as cleanup,
        patch("verify_paper_trading._stop_monitor_and_wait", return_value=True) as stop_monitor,
    ):
        with pytest.raises(KeyboardInterrupt):
            verification.main(execute=True)

    cleanup.assert_called_once_with(
        "SPY",
        manager,
        "wf-spy-1",
        monitor=monitor,
    )
    stop_monitor.assert_called_once_with(monitor)


def test_entry_is_never_submitted_before_monitor_connects() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor = MagicMock()
    manager = MagicMock()

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=monitor),
        patch("verify_paper_trading.OrderManager", return_value=manager),
        patch("core.data_client.fetch_latest_intraday_price", return_value=100.0),
        patch("verify_paper_trading._wait_for_monitor_connection", return_value=False),
        patch("verify_paper_trading._stop_monitor_and_wait", return_value=True),
    ):
        assert verification.main(execute=True) == 1

    monitor.start.assert_called_once_with()
    manager.submit_entry.assert_not_called()


def test_failed_submission_outcome_still_triggers_emergency_cleanup() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    monitor = MagicMock()
    manager = MagicMock()
    manager.submit_entry.return_value = EntrySubmissionOutcome(
        symbol="SPY",
        workflow_id="wf-spy-1",
        success=False,
        dry_run=False,
        error="ambiguous broker timeout",
    )

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=monitor),
        patch("verify_paper_trading.OrderManager", return_value=manager),
        patch("verify_paper_trading._wait_for_monitor_connection", return_value=True),
        patch("core.data_client.fetch_latest_intraday_price", return_value=100.0),
        patch("verify_paper_trading._cleanup_test_symbol", return_value=True) as cleanup,
        patch("verify_paper_trading._stop_monitor_and_wait", return_value=True),
    ):
        assert verification.main(execute=True) == 1

    cleanup.assert_called_once_with(
        "SPY",
        manager,
        "wf-spy-1",
        monitor=monitor,
    )


def test_durable_entry_waits_for_reference_commit_and_latest_stop_success() -> None:
    missing_reference = {
        "workflow_id": "wf-spy-1",
        "entry_plan": {"qty": 1.0},
        "transitions": [
            {
                "event": event,
                "details": {"qty": 1.0} if event == "buy_fill_received" else {},
            }
            for event in verification._ENTRY_EVENTS - {"protective_stop_reconciled"}
        ]
        + [
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": True,
                    "stop_order_id": "stop-1",
                    "client_order_id": "wf-spy-1-sl-old",
                },
            }
        ],
        "order_refs": [
            {
                "order_role": "entry_order",
                "broker_order_id": "entry-1",
                "client_order_id": "wf-spy-1",
            }
        ],
    }
    complete = {
        **missing_reference,
        "transitions": missing_reference["transitions"]
        + [
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": False,
                    "stop_order_id": "",
                    "client_order_id": "",
                },
            },
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": True,
                    "stop_order_id": "stop-2",
                    "client_order_id": "wf-spy-1-sl-a1b2c3",
                },
            },
        ],
        "order_refs": [
            {
                "order_role": "entry_order",
                "broker_order_id": "entry-1",
                "client_order_id": "wf-spy-1",
            },
            {
                "order_role": "protective_stop",
                "broker_order_id": "stop-2",
                "client_order_id": "wf-spy-1-sl-a1b2c3",
            },
        ],
    }
    store = MagicMock()
    store.load_workflow.side_effect = [missing_reference, complete]
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 1.0,
    }

    with (
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch(
            "verify_paper_trading._verified_protective_stop_identity",
            return_value=("stop-2", "wf-spy-1-sl-a1b2c3"),
        ),
        patch("verify_paper_trading.time.monotonic", return_value=0.0),
        patch("verify_paper_trading.time.sleep"),
    ):
        result = verification._wait_for_durable_entry("wf-spy-1", timeout=1.0)

    assert result is complete
    assert store.load_workflow.call_count == 2


def test_durable_entry_rejects_different_live_stop_identity() -> None:
    snapshot = {
        "workflow_id": "wf-spy-1",
        "entry_plan": {"qty": 1.0},
        "transitions": [
            {"event": "signal_accepted", "details": {}},
            {"event": "plan_built", "details": {}},
            {"event": "order_submitted", "details": {}},
            {"event": "buy_fill_received", "details": {"qty": 1.0}},
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": True,
                    "stop_order_id": "stop-stale",
                    "client_order_id": "wf-spy-1-sl-stale1",
                },
            },
        ],
        "order_refs": [
            {"order_role": "entry_order"},
            {
                "order_role": "protective_stop",
                "broker_order_id": "stop-stale",
                "client_order_id": "wf-spy-1-sl-stale1",
            },
        ],
    }
    store = MagicMock()
    store.load_workflow.return_value = snapshot
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 1.0,
    }

    with (
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch(
            "verify_paper_trading._verified_protective_stop_identity",
            return_value=("stop-live", "wf-spy-1-sl-live01"),
        ),
    ):
        assert verification._wait_for_durable_entry("wf-spy-1", timeout=0.0) is None


def test_durable_entry_rejects_complete_looking_partial_fill() -> None:
    partial = {
        "workflow_id": "wf-spy-1",
        "entry_plan": {"qty": 1.0},
        "transitions": [
            {
                "event": event,
                "details": {"qty": 0.1} if event == "buy_fill_received" else {"success": True},
            }
            for event in verification._ENTRY_EVENTS
        ],
        "order_refs": [
            {"order_role": "entry_order"},
            {"order_role": "protective_stop"},
        ],
    }
    store = MagicMock()
    store.load_workflow.return_value = partial
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 0.1,
    }

    with (
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._has_verified_protective_stop", return_value=True),
    ):
        assert verification._wait_for_durable_entry("wf-spy-1", timeout=0.0) is None


def test_durable_entry_accepts_exact_fill_across_replacement_chain() -> None:
    snapshot = {
        "workflow_id": "wf-spy-1",
        "entry_plan": {"qty": 1.0},
        "transitions": [
            {"event": "signal_accepted", "details": {}},
            {"event": "plan_built", "details": {}},
            {"event": "order_submitted", "details": {}},
            {
                "event": "buy_fill_received",
                "details": {"qty": 0.4, "broker_order_id": "entry-parent"},
            },
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": True,
                    "stop_order_id": "stop-partial",
                    "client_order_id": "wf-spy-1-sl-partial",
                },
            },
            {
                "event": "buy_fill_received",
                "details": {"qty": 0.6, "broker_order_id": "entry-child"},
            },
            {
                "event": "protective_stop_reconciled",
                "details": {
                    "success": True,
                    "stop_order_id": "stop-full",
                    "client_order_id": "wf-spy-1-sl-full",
                },
            },
        ],
        "order_refs": [
            {
                "order_role": "entry_order",
                "broker_order_id": "entry-parent",
                "client_order_id": "wf-spy-1",
            },
            {
                "order_role": "buy_fill",
                "broker_order_id": "entry-parent",
                "client_order_id": "wf-spy-1",
            },
            {
                "order_role": "buy_fill",
                "broker_order_id": "entry-child",
                "client_order_id": "wf-spy-1",
            },
            {
                "order_role": "protective_stop",
                "broker_order_id": "stop-full",
                "client_order_id": "wf-spy-1-sl-full",
            },
        ],
    }
    store = MagicMock()
    store.load_workflow.return_value = snapshot
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 1.0,
    }

    with (
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch(
            "verify_paper_trading._verified_protective_stop_identity",
            return_value=("stop-full", "wf-spy-1-sl-full"),
        ),
    ):
        assert verification._wait_for_durable_entry(
            "wf-spy-1",
            timeout=0.0,
        ) is snapshot


def test_restart_gap_replays_closed_sell_fill_into_sqlite() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_gap_{uuid4().hex}.sqlite3"
    closed_stop = SimpleNamespace(
        id="stop-1",
        symbol="SPY",
        side="sell",
        type="stop",
        status="filled",
        client_order_id="wf-placeholder-sl",
        filled_qty="1",
        filled_avg_price="92.00",
        filled_at="2026-08-17T14:00:00Z",
    )

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        workflow.mark_protective_stop(
            success=True,
            stop_order_id="stop-1",
            stop_price=92.0,
            action="submitted",
        )
        closed_stop.client_order_id = f"{workflow.workflow_id}-sl"
        clear_workflow_registry()

        manager = verification.OrderManager(paper=True)
        with (
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_stop]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                manager,
            )

        assert position_remains is False
        assert get_execution_store().load_active_position("SPY") is None
        recovered = get_workflow(workflow.workflow_id)
        assert recovered is not None
        assert "sell_fill_received" in [item.event for item in recovered.transitions]
        reset_workflow_state()


def test_restart_gap_replays_exit_accepted_before_reference_was_persisted() -> None:
    """A crash after broker acceptance must not strand durable ownership."""
    db_path = Path(tempfile.gettempdir()) / f"verify_exit_gap_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        closed_exit = SimpleNamespace(
            id="exit-accepted-before-crash",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=f"{workflow.workflow_id}-exit",
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
        )
        clear_workflow_registry()

        manager = verification.OrderManager(paper=True)
        with (
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                manager,
            )

        assert position_remains is False
        store = get_execution_store()
        assert store.load_active_position("SPY") is None
        snapshot = store.load_workflow(workflow.workflow_id)
        assert snapshot is not None
        assert any(
            ref["broker_order_id"] == "exit-accepted-before-crash"
            and ref["client_order_id"] == closed_exit.client_order_id
            for ref in snapshot["order_refs"]
        )
        reset_workflow_state()


def test_restart_gap_repairs_owner_clear_after_final_checkpoint_was_recorded() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_final_clear_gap_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        exit_client_order_id = f"{workflow.workflow_id}-exit"
        workflow.mark_sell_fill(
            qty=1.0,
            fill_price=101.0,
            exit_reason="exit order filled",
            broker_order_id="exit-recorded-before-crash",
            client_order_id=exit_client_order_id,
            clear_active=False,
        )
        closed_exit = SimpleNamespace(
            id="exit-recorded-before-crash",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=exit_client_order_id,
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        clear_workflow_registry()

        with (
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            )

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)

        assert position_remains is False
        assert store.load_active_position("SPY") is None
        assert snapshot is not None
        assert [
            transition["event"] for transition in snapshot["transitions"]
        ].count("sell_fill_received") == 1
        reset_workflow_state()


def test_restart_gap_resolves_exact_pending_exit_after_final_checkpoint_and_owner_clear() -> None:
    """Flat state alone is insufficient; the exact durable final fill repairs the intent."""
    db_path = Path(tempfile.gettempdir()) / f"verify_exit_intent_gap_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        exit_client_order_id = f"{workflow.workflow_id}-exit"
        workflow.mark_exit_submission_intent(
            exit_reason="supervised verification cleanup",
            client_order_id=exit_client_order_id,
        )
        workflow.mark_exit_order_submitted(
            exit_reason="supervised verification cleanup",
            broker_order_id="exit-final-before-intent-resolution",
        )
        workflow.mark_sell_fill(
            qty=1.0,
            fill_price=101.0,
            exit_reason="exit order filled",
            broker_order_id="exit-final-before-intent-resolution",
            client_order_id=exit_client_order_id,
            clear_active=True,
        )
        closed_exit = SimpleNamespace(
            id="exit-final-before-intent-resolution",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=exit_client_order_id,
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        clear_workflow_registry()

        with (
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ) as sample,
        ):
            assert verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            ) is False
            # Recovery is idempotent after the exact intent marker is durable.
            assert verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            ) is False

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)
        assert sample.call_count >= 3
        assert store.load_active_position("SPY") is None
        assert store.load_pending_submission_intents(symbol="SPY") == []
        assert snapshot is not None
        resolutions = [
            transition
            for transition in snapshot["transitions"]
            if transition["event"] == "submission_intent_resolved"
            and transition["details"].get("role") == "exit"
        ]
        assert len(resolutions) == 1
        assert resolutions[0]["details"]["client_order_id"] == exit_client_order_id
        assert (
            resolutions[0]["details"]["broker_order_id"]
            == "exit-final-before-intent-resolution"
        )
        reset_workflow_state()


def test_restart_gap_resolves_split_liquidation_marker_gap_idempotently() -> None:
    """A trusted stop partial plus residual market exit covers the exact buy."""
    db_path = Path(tempfile.gettempdir()) / f"verify_split_marker_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            assert verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            ) is False
            assert verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            ) is False

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)
        assert store.load_pending_submission_intents(symbol="SPY") == []
        assert snapshot is not None
        assert [
            transition["event"]
            for transition in snapshot["transitions"]
        ].count("submission_intent_resolved") == 1
        reset_workflow_state()


def test_split_marker_recovery_preserves_owner_added_after_flat_proof() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_owner_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        workflow_id = workflow.workflow_id
        clear_workflow_registry()
        sample_count = 0

        def flat_with_new_owner(_symbol: str):
            nonlocal sample_count
            sample_count += 1
            if sample_count == 2:
                recovered = get_workflow(workflow_id)
                assert recovered is not None
                recovered.repair_buy_fill_storage(
                    qty=0.2,
                    fill_price=100.0,
                    broker_order_id="entry-1",
                    preserve_higher_qty=True,
                )
            return None, []

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                side_effect=flat_with_new_owner,
            ),
        ):
            assert verification._reconcile_restart_gap(
                "SPY",
                workflow_id,
                verification.OrderManager(paper=True),
            ) is True

        store = get_execution_store()
        active = store.load_active_position("SPY")
        assert sample_count == 2
        assert active is not None
        assert active["workflow_id"] == workflow_id
        assert active["qty"] == pytest.approx(0.2)
        assert store.load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_split_liquidation_with_missing_closed_partial() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_missing_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, _closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        clear_workflow_registry()

        with (
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="trusted sell coverage"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_split_liquidation_with_foreign_partial_identity() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_foreign_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        closed_stop.client_order_id = "foreign-workflow-sl"
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="trusted sell coverage"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_unrecorded_positive_sell_in_split_history() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_unrecorded_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        unrecorded_sell = SimpleNamespace(
            id="stop-unrecorded",
            symbol="SPY",
            side="sell",
            type="stop",
            status="canceled",
            client_order_id=f"{workflow.workflow_id}-sl-extra",
            filled_qty="0.1",
            filled_avg_price="91.50",
            filled_at="2026-08-17T13:59:59Z",
            replaces="",
            replaced_by="",
        )
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[unrecorded_sell, closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="trusted sell coverage"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_ambiguous_durable_sell_references() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_refs_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        workflow.repair_order_reference(
            broker_order_id="stop-partial",
            client_order_id=f"{workflow.workflow_id}-sl-shadow",
            order_role="sell_fill",
        )
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="trusted sell coverage"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_ambiguous_split_liquidation_chain() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_branch_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap()
        closed_stop.status = "replaced"
        closed_stop.replaced_by = "stop-child-a"
        child_a = SimpleNamespace(
            id="stop-child-a",
            symbol="SPY",
            side="sell",
            type="stop",
            status="canceled",
            client_order_id=f"{workflow.workflow_id}-sl-a",
            filled_qty="0",
            filled_avg_price="0",
            filled_at=None,
            replaces="stop-partial",
            replaced_by="",
        )
        child_b = SimpleNamespace(
            id="stop-child-b",
            symbol="SPY",
            side="sell",
            type="stop",
            status="canceled",
            client_order_id=f"{workflow.workflow_id}-sl-b",
            filled_qty="0",
            filled_avg_price="0",
            filled_at=None,
            replaces="stop-partial",
            replaced_by="",
        )
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, child_a, child_b, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="branches ambiguously"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rejects_split_liquidation_overcoverage() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_split_over_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow, closed_stop, closed_exit = _persist_split_liquidation_marker_gap(
            stop_qty=0.8,
            exit_qty=0.3,
        )
        clear_workflow_registry()

        with (
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[closed_stop, closed_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="trusted sell coverage"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_never_resolves_pending_exit_from_flat_state_without_final_ref() -> None:
    """A pending exit remains unresolved without both final checkpoint and reference."""
    db_path = Path(tempfile.gettempdir()) / f"verify_exit_intent_no_ref_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        exit_client_order_id = f"{workflow.workflow_id}-exit"
        workflow.mark_exit_submission_intent(
            exit_reason="supervised verification cleanup",
            client_order_id=exit_client_order_id,
        )
        workflow.mark_exit_order_submitted(
            exit_reason="supervised verification cleanup",
            broker_order_id="exit-checkpoint-without-fill-ref",
        )
        workflow.transition(
            WorkflowState.SELL_FILL_RECEIVED,
            event="sell_fill_received",
            details={
                "qty": 1.0,
                "fill_price": 101.0,
                "exit_reason": "exit order filled",
                "broker_order_id": "exit-checkpoint-without-fill-ref",
                "client_order_id": exit_client_order_id,
            },
        )
        get_execution_store().clear_active_position("SPY")
        closed_exit = SimpleNamespace(
            id="exit-checkpoint-without-fill-ref",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=exit_client_order_id,
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        clear_workflow_registry()

        with (
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            with pytest.raises(RuntimeError, match="final sell checkpoint/reference"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow.workflow_id,
                    verification.OrderManager(paper=True),
                )

        assert get_execution_store().load_pending_submission_intents(symbol="SPY")
        reset_workflow_state()


def test_restart_gap_rechecks_flat_state_before_clearing_durable_owner() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_gap_recheck_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        closed_exit = SimpleNamespace(
            id="exit-before-position-reappeared",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=f"{workflow.workflow_id}-exit",
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        reappeared_position = SimpleNamespace(
            symbol="SPY",
            qty=1.0,
            avg_entry_price=100.0,
        )
        clear_workflow_registry()

        with (
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                side_effect=[(None, []), (reappeared_position, [])],
            ) as sample,
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                verification.OrderManager(paper=True),
            )

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)

        assert sample.call_count == 2
        assert position_remains is True
        assert store.load_active_position("SPY") is not None
        assert snapshot is not None
        assert "sell_fill_received" not in {
            transition["event"] for transition in snapshot["transitions"]
        }
        reset_workflow_state()


def test_restart_gap_preserves_newer_same_workflow_owner_during_replay() -> None:
    """A late cumulative BUY cannot be erased after the broker-flat proof."""
    db_path = Path(tempfile.gettempdir()) / f"verify_replay_owner_cas_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        closed_exit = SimpleNamespace(
            id="exit-before-late-buy",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=f"{workflow.workflow_id}-exit",
            filled_qty="1",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        workflow_id = workflow.workflow_id
        clear_workflow_registry()
        sample_count = 0

        def flat_with_late_buy_interleave(_symbol: str):
            nonlocal sample_count
            sample_count += 1
            if sample_count == 2:
                recovered = get_workflow(workflow_id)
                assert recovered is not None
                recovered.mark_buy_fill(
                    qty=1.2,
                    fill_price=100.0,
                    broker_order_id="entry-1",
                    restore_active=False,
                )
                recovered.repair_buy_fill_storage(
                    qty=1.2,
                    fill_price=100.0,
                    broker_order_id="entry-1",
                    preserve_higher_qty=True,
                )
            return None, []

        with (
            patch("verify_paper_trading.get_closed_orders", return_value=[closed_exit]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                side_effect=flat_with_late_buy_interleave,
            ),
        ):
            with pytest.raises(RuntimeError, match="active ownership changed"):
                verification._reconcile_restart_gap(
                    "SPY",
                    workflow_id,
                    verification.OrderManager(paper=True),
                )

        store = get_execution_store()
        active = store.load_active_position("SPY")
        snapshot = store.load_workflow(workflow_id)
        assert sample_count == 2
        assert active is not None
        assert active["workflow_id"] == workflow_id
        assert active["qty"] == pytest.approx(1.2)
        assert snapshot is not None
        assert max(
            float(item["details"]["qty"])
            for item in snapshot["transitions"]
            if item["event"] == "buy_fill_received"
        ) == pytest.approx(1.2)
        reset_workflow_state()


def test_restart_gap_replays_fills_across_one_sell_replacement_chain() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_sell_chain_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=10.0, fill_price=100.0, broker_order_id="entry-1")
        workflow.mark_protective_stop(
            success=True,
            stop_order_id="stop-parent",
            stop_price=92.0,
            action="submitted",
            stop_client_order_id=f"{workflow.workflow_id}-sl",
        )
        parent = SimpleNamespace(
            id="stop-parent",
            symbol="SPY",
            side="sell",
            type="stop",
            status="replaced",
            client_order_id=f"{workflow.workflow_id}-sl",
            filled_qty="4",
            filled_avg_price="92.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="stop-child",
        )
        child = SimpleNamespace(
            id="stop-child",
            symbol="SPY",
            side="sell",
            type="stop",
            status="filled",
            client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            filled_qty="6",
            filled_avg_price="91.50",
            filled_at="2026-08-17T14:00:01Z",
            replaces="stop-parent",
            replaced_by="",
        )
        clear_workflow_registry()

        manager = verification.OrderManager(paper=True)
        with (
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch("verify_paper_trading.get_closed_orders", return_value=[child, parent]),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                manager,
            )

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)
        assert position_remains is False
        assert store.load_active_position("SPY") is None
        assert snapshot is not None
        assert {
            ref["broker_order_id"]
            for ref in snapshot["order_refs"]
            if ref["broker_order_id"].startswith("stop-")
        } == {"stop-parent", "stop-child"}
        assert [
            item["details"]["broker_order_id"]
            for item in snapshot["transitions"]
            if item["event"] in {"sell_partial_fill_received", "sell_fill_received"}
        ] == ["stop-parent", "stop-child"]
        reset_workflow_state()


def test_restart_gap_ignores_recorded_partial_when_selecting_disconnected_exit() -> None:
    """Previously persisted fills must not compete with the residual cleanup fill."""
    db_path = Path(tempfile.gettempdir()) / f"verify_residual_gap_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        workflow.mark_sell_partial_fill(
            qty=0.7,
            fill_price=92.0,
            broker_order_id="stop-old",
            client_order_id=f"{workflow.workflow_id}-sl",
        )
        workflow.repair_buy_fill_storage(
            qty=0.3,
            fill_price=100.0,
            broker_order_id="entry-1",
            preserve_higher_qty=False,
        )
        old_partial = SimpleNamespace(
            id="stop-old",
            symbol="SPY",
            side="sell",
            type="stop",
            status="canceled",
            client_order_id=f"{workflow.workflow_id}-sl",
            filled_qty="0.7",
            filled_avg_price="92.00",
            filled_at="2026-08-17T14:00:00Z",
            replaces="",
            replaced_by="",
        )
        cleanup_exit = SimpleNamespace(
            id="exit-cleanup",
            symbol="SPY",
            side="sell",
            type="market",
            status="filled",
            client_order_id=f"{workflow.workflow_id}-exit",
            filled_qty="0.3",
            filled_avg_price="101.00",
            filled_at="2026-08-17T14:00:01Z",
            replaces="",
            replaced_by="",
        )
        clear_workflow_registry()

        manager = verification.OrderManager(paper=True)
        with (
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[old_partial, cleanup_exit],
            ),
            patch(
                "verify_paper_trading._sample_stable_symbol_state",
                return_value=(None, []),
            ),
        ):
            position_remains = verification._reconcile_restart_gap(
                "SPY",
                workflow.workflow_id,
                manager,
            )

        store = get_execution_store()
        snapshot = store.load_workflow(workflow.workflow_id)
        assert position_remains is False
        assert store.load_active_position("SPY") is None
        assert snapshot is not None
        assert [
            item["details"]["broker_order_id"]
            for item in snapshot["transitions"]
            if item["event"] == "sell_fill_received"
        ] == ["exit-cleanup"]
        reset_workflow_state()


@pytest.mark.parametrize(
    "closed_sell",
    [
        pytest.param(
            SimpleNamespace(
                id="limit-1",
                side="sell",
                type="limit",
                status="filled",
                client_order_id="wf-spy-1-exit",
                filled_qty="1",
                filled_avg_price="100",
                filled_at="2026-08-17T14:00:00Z",
            ),
            id="non-market-exit",
        ),
        pytest.param(
            SimpleNamespace(
                id="wrong-role-1",
                side="sell",
                type="market",
                status="filled",
                client_order_id="wf-spy-1",
                filled_qty="1",
                filled_avg_price="100",
                filled_at="2026-08-17T14:00:00Z",
            ),
            id="wrong-workflow-role",
        ),
        pytest.param(
            SimpleNamespace(
                id="stop-partial",
                side="sell",
                type="stop",
                status="filled",
                client_order_id="wf-spy-1-sl-a1b2c3",
                filled_qty="0.5",
                filled_avg_price="92",
                filled_at="2026-08-17T14:00:00Z",
            ),
            id="undersized-stop-coverage",
        ),
    ],
)
def test_restart_gap_rejects_non_market_wrong_role_or_undersized_sell(
    closed_sell,
) -> None:
    store = MagicMock()
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 1.0,
    }
    manager = MagicMock()

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_closed_orders", return_value=[closed_sell]),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch(
            "verify_paper_trading._sample_stable_symbol_state",
            return_value=(None, []),
        ),
    ):
        with pytest.raises(RuntimeError, match="no matching filled sell"):
            verification._reconcile_restart_gap("SPY", "wf-spy-1", manager)

    manager.handle_fill.assert_not_called()


def test_successful_lifecycle_uses_manager_restart_and_durable_cleanup() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(equity="100000", buying_power="50000")
    first_monitor = MagicMock()
    restarted_monitor = MagicMock()
    first_manager = MagicMock()
    restarted_manager = MagicMock()
    first_manager.submit_entry.return_value = EntrySubmissionOutcome(
        symbol="SPY",
        workflow_id="wf-spy-1",
        success=True,
        dry_run=False,
        order_id="entry-1",
    )
    durable_entry = {
        "workflow_id": "wf-spy-1",
        "transitions": [
            {
                "event": "buy_fill_received",
                "details": {"qty": 1.0, "fill_price": 100.0},
            },
            {
                "event": "protective_stop_reconciled",
                "details": {"success": True},
            },
        ],
    }
    restart = SimpleNamespace(
        success=True,
        monitor=restarted_monitor,
        manager=restarted_manager,
        error="",
    )

    with (
        patch("verify_paper_trading._is_paper_mode", return_value=True),
        patch.object(verification.settings, "ALPACA_API_KEY", "present"),
        patch.object(verification.settings, "ALPACA_SECRET_KEY", "present"),
        patch("verify_paper_trading._get_trading_client", return_value=client),
        patch("verify_paper_trading._preflight_symbol_clear", return_value=True),
        patch("verify_paper_trading._check_market_open", return_value=True),
        patch("verify_paper_trading.FillMonitor", return_value=first_monitor),
        patch("verify_paper_trading.OrderManager", return_value=first_manager),
        patch("verify_paper_trading._wait_for_monitor_connection", return_value=True) as wait_connected,
        patch("verify_paper_trading.notify_configured", return_value=False),
        patch("core.data_client.fetch_latest_intraday_price", return_value=100.0),
        patch("verify_paper_trading._wait_for_durable_entry", return_value=durable_entry),
        patch("verify_paper_trading._restart_monitor_and_recover", return_value=restart) as restart_call,
        patch("verify_paper_trading._cleanup_test_symbol", return_value=True) as cleanup,
        patch("verify_paper_trading._stop_monitor_and_wait", return_value=True) as stop_monitor,
    ):
        assert verification.main(execute=True) == 0

    submitted_plan = first_manager.submit_entry.call_args.args[0]
    assert submitted_plan.symbol == "SPY"
    assert submitted_plan.qty == 1.0
    assert submitted_plan.entry_price == 100.0
    wait_connected.assert_called_once_with(first_monitor)
    restart_call.assert_called_once_with("SPY", "wf-spy-1", first_monitor, first_manager)
    cleanup.assert_called_once_with(
        "SPY",
        restarted_manager,
        "wf-spy-1",
        monitor=restarted_monitor,
    )
    stop_monitor.assert_called_once_with(restarted_monitor)


def test_restart_reloads_same_active_workflow_and_reconciles_stop() -> None:
    first_monitor = MagicMock()
    first_manager = MagicMock()
    restarted_monitor = MagicMock()
    restarted_manager = MagicMock()
    restarted_manager.reconcile_startup_stops.return_value = [
        SimpleNamespace(symbol="SPY", success=True, action="reused", error="")
    ]
    store = MagicMock()
    store.load_workflow.return_value = {
        "workflow_id": "wf-spy-1",
        "transitions": [
            {
                "event": "protective_stop_reconciled",
                "details": {"success": True, "action": "startup_reused"},
            }
        ],
    }

    with (
        patch("verify_paper_trading._stop_monitor_and_wait", return_value=True),
        patch("verify_paper_trading.clear_workflow_registry") as clear_registry,
        patch(
            "verify_paper_trading.get_active_workflow_for_symbol",
            return_value=SimpleNamespace(workflow_id="wf-spy-1"),
        ),
        patch("verify_paper_trading.FillMonitor", return_value=restarted_monitor),
        patch("verify_paper_trading.OrderManager", return_value=restarted_manager),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._wait_for_monitor_connection", return_value=True) as wait_connected,
        patch("verify_paper_trading._reconcile_restart_gap", return_value=True) as gap_replay,
    ):
        outcome = verification._restart_monitor_and_recover(
            "SPY",
            "wf-spy-1",
            first_monitor,
            first_manager,
        )

    assert outcome.success is True
    assert outcome.monitor is restarted_monitor
    assert outcome.manager is restarted_manager
    clear_registry.assert_called_once_with()
    restarted_monitor.start.assert_called_once_with()
    wait_connected.assert_called_once_with(restarted_monitor)
    restarted_manager.reconcile_startup_stops.assert_called_once_with("SPY")
    gap_replay.assert_called_once_with(
        "SPY",
        "wf-spy-1",
        restarted_manager,
    )
    store.load_workflow.assert_called_once_with("wf-spy-1")


def test_cleanup_waits_for_sell_fill_then_broker_and_local_clear() -> None:
    manager = MagicMock()
    monitor = MagicMock()
    monitor.is_connected.return_value = True
    manager.submit_exit.return_value = OrderResult(
        True,
        "exit-1",
        "SPY",
        "sell",
        1.0,
        client_order_id="wf-spy-1-exit",
    )

    with (
        patch(
            "verify_paper_trading._wait_for_workflow_events",
            return_value={"workflow_id": "wf-spy-1"},
        ) as wait_for_events,
        patch("verify_paper_trading._wait_for_symbol_clear", return_value=True) as wait_for_clear,
    ):
        assert verification._cleanup_test_symbol(
            "SPY",
            manager,
            "wf-spy-1",
            monitor=monitor,
        ) is True

    manager.submit_exit.assert_called_once_with(
        "SPY",
        exit_reason="supervised verification cleanup",
    )
    wait_for_events.assert_called_once_with(
        "wf-spy-1",
        {"sell_fill_received"},
        timeout=verification._EXIT_TIMEOUT,
    )
    wait_for_clear.assert_called_once_with(
        "SPY",
        timeout=verification._EXIT_TIMEOUT,
        workflow_id="wf-spy-1",
    )
    monitor.is_connected.assert_called_once_with()


def test_cleanup_cannot_report_success_after_fill_monitor_fault() -> None:
    manager = MagicMock()
    monitor = MagicMock()
    monitor.is_connected.return_value = False
    manager.submit_exit.return_value = OrderResult(
        True,
        "exit-1",
        "SPY",
        "sell",
        1.0,
        client_order_id="wf-spy-1-exit",
    )

    with (
        patch(
            "verify_paper_trading._wait_for_workflow_events",
            return_value={"workflow_id": "wf-spy-1"},
        ),
        patch("verify_paper_trading._wait_for_symbol_clear", return_value=True),
    ):
        assert verification._cleanup_test_symbol(
            "SPY",
            manager,
            "wf-spy-1",
            monitor=monitor,
        ) is False

    monitor.is_connected.assert_called_once_with()


def test_failed_exit_enters_safety_recovery_without_waiting_first() -> None:
    manager = MagicMock()
    monitor = MagicMock()
    manager.submit_exit.return_value = OrderResult(
        False,
        "",
        "SPY",
        "sell",
        1.0,
        error="cancel failed after mutation",
        client_order_id="wf-spy-1-exit",
    )

    with (
        patch("verify_paper_trading._reconcile_restart_gap", return_value=True),
        patch("verify_paper_trading._ensure_symbol_safe", return_value=True) as ensure,
        patch("verify_paper_trading._wait_for_symbol_clear") as wait_for_clear,
    ):
        assert verification._cleanup_test_symbol(
            "SPY",
            manager,
            "wf-spy-1",
            monitor=monitor,
        ) is False

    ensure.assert_called_once_with("SPY", manager, "wf-spy-1")
    wait_for_clear.assert_not_called()


def test_cleanup_routes_real_sell_fill_and_clears_sqlite() -> None:
    db_path = Path(tempfile.gettempdir()) / f"verify_cleanup_{uuid4().hex}.sqlite3"

    with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
        reset_workflow_state()
        workflow = create_entry_workflow(
            verification._build_entry_plan("SPY", 100.0),
            signal_payload={"symbol": "SPY"},
        )
        workflow.mark_order_submitted(broker_order_id="entry-1")
        workflow.mark_buy_fill(qty=1.0, fill_price=100.0, broker_order_id="entry-1")
        workflow.mark_protective_stop(
            success=True,
            stop_order_id="stop-1",
            stop_price=92.0,
            action="submitted",
        )
        manager = verification.OrderManager(paper=True)
        monitor = MagicMock()
        monitor.is_connected.return_value = True
        dispatched = False

        def dispatch_sell_fill(_seconds: float) -> None:
            nonlocal dispatched
            if dispatched:
                return
            dispatched = True
            manager.handle_fill(
                symbol="SPY",
                broker_order_id="exit-1",
                client_order_id=f"{workflow.workflow_id}-exit",
                side="sell",
                filled_qty=1.0,
                fill_price=100.0,
                order_type="market",
            )

        with (
            patch("core.order_manager.cancel_open_orders_verified", return_value=1),
            patch(
                "core.order_manager.close_position",
                return_value=OrderResult(
                    True,
                    "exit-1",
                    "SPY",
                    "sell",
                    1.0,
                    client_order_id=f"{workflow.workflow_id}-exit",
                ),
            ),
            patch("verify_paper_trading.get_open_positions", return_value=[]),
            patch("verify_paper_trading.get_open_orders", return_value=[]),
            patch(
                "core.order_manager._sample_stable_symbol_state",
                return_value=(None, []),
            ),
            patch(
                "verify_paper_trading.get_closed_orders",
                return_value=[
                    SimpleNamespace(
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        status="filled",
                        filled_qty="1",
                    ),
                    SimpleNamespace(
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        type="market",
                        status="filled",
                        filled_qty="1",
                    ),
                ],
            ),
            patch("core.order_manager.notify_sell_filled", return_value=False),
            patch("verify_paper_trading.time.sleep", side_effect=dispatch_sell_fill),
        ):
            assert verification._cleanup_test_symbol(
                "SPY",
                manager,
                workflow.workflow_id,
                monitor=monitor,
            ) is True

        assert dispatched is True
        assert get_execution_store().load_active_position("SPY") is None
        reset_workflow_state()


@pytest.mark.parametrize("residue", ["position", "order", "local", "pending"])
def test_final_clear_rejects_each_residue_source(residue: str) -> None:
    positions = [SimpleNamespace(symbol="SPY")] if residue == "position" else []
    orders = [SimpleNamespace(id="open-1")] if residue == "order" else []
    store = MagicMock()
    store.load_active_position.return_value = (
        {"workflow_id": "wf-spy-1"} if residue == "local" else None
    )
    store.load_pending_submission_intents.return_value = (
        [
            {
                "workflow_id": "wf-spy-1",
                "symbol": "SPY",
                "event": "exit_submission_intent",
                "details": {"client_order_id": "wf-spy-1-exit"},
            }
        ]
        if residue == "pending"
        else []
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=positions),
        patch("verify_paper_trading.get_open_orders", return_value=orders),
        patch("verify_paper_trading.get_execution_store", return_value=store),
    ):
        assert verification._wait_for_symbol_clear("SPY", timeout=0.0) is False


def test_final_clear_requires_broker_and_durable_store_to_be_empty() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []
    closed_entry = SimpleNamespace(
        client_order_id="wf-spy-1",
        side="buy",
        status="filled",
        filled_qty="1",
    )
    closed_exit = SimpleNamespace(
        client_order_id="wf-spy-1-exit",
        side="sell",
        type="market",
        status="filled",
        filled_qty="1",
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]) as positions,
        patch("verify_paper_trading.get_open_orders", return_value=[]) as orders,
        patch(
            "verify_paper_trading.get_closed_orders",
            return_value=[closed_entry, closed_exit],
        ) as closed_orders,
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading.time.sleep") as sleep,
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=10.0,
            workflow_id="wf-spy-1",
        ) is True

    assert positions.call_count == verification._FINAL_CLEAR_CONFIRMATIONS
    assert orders.call_count == verification._FINAL_CLEAR_CONFIRMATIONS
    assert store.load_active_position.call_count == verification._FINAL_CLEAR_CONFIRMATIONS
    assert (
        store.load_pending_submission_intents.call_count
        == verification._FINAL_CLEAR_CONFIRMATIONS
    )
    store.load_pending_submission_intents.assert_called_with(symbol="SPY")
    closed_orders.assert_called_once_with("SPY", limit=50, raise_on_error=True)
    assert sleep.call_count == verification._FINAL_CLEAR_CONFIRMATIONS - 1


def test_final_clear_rejects_filled_entry_without_covering_sell() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []
    filled_entry = SimpleNamespace(
        client_order_id="wf-spy-1",
        side="buy",
        status="filled",
        filled_qty="1",
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch("verify_paper_trading.get_closed_orders", return_value=[filled_entry]),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._FINAL_CLEAR_CONFIRMATIONS", 1),
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=0.0,
            workflow_id="wf-spy-1",
        ) is False


def test_final_clear_rejects_zero_fill_replaced_parent_without_terminal_child() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []
    replaced_parent = SimpleNamespace(
        id="entry-1",
        client_order_id="wf-spy-1",
        side="buy",
        status="replaced",
        filled_qty="0",
        replaced_by="entry-2",
        replaces=None,
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch(
            "verify_paper_trading.get_closed_orders",
            return_value=[replaced_parent],
        ),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._FINAL_CLEAR_CONFIRMATIONS", 1),
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=0.0,
            workflow_id="wf-spy-1",
        ) is False


@pytest.mark.parametrize("link_source", ["replaced_by", "replaces"])
def test_entry_terminal_sums_replacement_chain_before_exact_sell_coverage(
    link_source: str,
) -> None:
    parent_replaced_by = "entry-2" if link_source == "replaced_by" else None
    child_replaces = "entry-1" if link_source == "replaces" else None
    replaced_parent = SimpleNamespace(
        id="entry-1",
        client_order_id="wf-spy-1",
        side="buy",
        status="replaced",
        filled_qty="0.4",
        replaced_by=parent_replaced_by,
        replaces=None,
    )
    terminal_child = SimpleNamespace(
        id="entry-2",
        client_order_id="replacement-child",
        side="buy",
        status="filled",
        filled_qty="0.6",
        replaced_by=None,
        replaces=child_replaces,
    )

    def closed_sell(order_id: str, qty: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=order_id,
            client_order_id="wf-spy-1-exit",
            side="sell",
            type="market",
            status="filled",
            filled_qty=qty,
        )

    with patch(
        "verify_paper_trading.get_closed_orders",
        side_effect=[
            [replaced_parent, terminal_child, closed_sell("sell-under", "0.4")],
            [replaced_parent, terminal_child, closed_sell("sell-exact", "1.0")],
            [replaced_parent, terminal_child, closed_sell("sell-over", "1.1")],
        ],
    ) as closed_orders:
        assert verification._entry_order_is_terminal("SPY", "wf-spy-1") is False
        assert verification._entry_order_is_terminal("SPY", "wf-spy-1") is True
        assert verification._entry_order_is_terminal("SPY", "wf-spy-1") is False

    assert closed_orders.call_count == 3


def test_final_clear_accepts_zero_fill_terminal_entry_without_sell() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []
    canceled_entry = SimpleNamespace(
        client_order_id="wf-spy-1",
        side="buy",
        status="canceled",
        filled_qty="0",
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch("verify_paper_trading.get_closed_orders", return_value=[canceled_entry]),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._FINAL_CLEAR_CONFIRMATIONS", 1),
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=0.0,
            workflow_id="wf-spy-1",
        ) is True


def test_final_clear_rejects_zero_fill_entry_when_another_entry_has_exposure() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []
    closed_orders = [
        SimpleNamespace(
            client_order_id="wf-spy-1",
            side="buy",
            status="canceled",
            filled_qty="0",
        ),
        SimpleNamespace(
            client_order_id="wf-spy-1",
            side="buy",
            status="filled",
            filled_qty="1",
        ),
    ]

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch("verify_paper_trading.get_closed_orders", return_value=closed_orders),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._FINAL_CLEAR_CONFIRMATIONS", 1),
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=0.0,
            workflow_id="wf-spy-1",
        ) is False


def test_final_clear_rejects_missing_terminal_entry_confirmation() -> None:
    store = MagicMock()
    store.load_active_position.return_value = None
    store.load_pending_submission_intents.return_value = []

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[]),
        patch("verify_paper_trading.get_open_orders", return_value=[]),
        patch("verify_paper_trading.get_closed_orders", return_value=[]) as closed_orders,
        patch("verify_paper_trading.get_execution_store", return_value=store),
    ):
        assert verification._wait_for_symbol_clear(
            "SPY",
            timeout=0.0,
            workflow_id="wf-spy-1",
        ) is False

    closed_orders.assert_called_once_with("SPY", limit=50, raise_on_error=True)


def test_verified_protection_rejects_live_buy_remainder() -> None:
    position = SimpleNamespace(symbol="SPY", qty=0.1, avg_entry_price=100.0)
    stop = SimpleNamespace(
        id="stop-1",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="0.1",
        filled_qty="0",
        stop_price="92.0",
        client_order_id="wf-spy-1-sl",
    )
    buy_remainder = SimpleNamespace(
        id="entry-1",
        side="buy",
        type="limit",
        qty="0.9",
        client_order_id="wf-spy-1",
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[position]),
        patch("verify_paper_trading.get_open_orders", return_value=[stop, buy_remainder]),
        patch(
            "verify_paper_trading._sample_stable_symbol_state",
            return_value=(position, [stop, buy_remainder]),
            create=True,
        ),
    ):
        assert verification._has_verified_protective_stop("SPY", "wf-spy-1") is False


def test_verified_protection_uses_one_converged_composite_symbol_snapshot() -> None:
    position = SimpleNamespace(symbol="SPY", qty=1.0, avg_entry_price=100.0)
    stop = SimpleNamespace(
        id="stop-1",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1.0",
        filled_qty="0",
        stop_price="92.0",
        client_order_id="wf-spy-1-sl-a1b2c3",
    )

    with (
        patch(
            "verify_paper_trading._sample_stable_symbol_state",
            return_value=(position, [stop]),
            create=True,
        ) as sample,
        patch(
            "verify_paper_trading.get_open_positions",
            side_effect=AssertionError("independent position read is unsafe"),
        ),
        patch(
            "verify_paper_trading.get_open_orders",
            side_effect=AssertionError("independent order read is unsafe"),
        ),
    ):
        assert verification._verified_protective_stop_identity(
            "SPY",
            "wf-spy-1",
            expected_qty=1.0,
        ) == ("stop-1", "wf-spy-1-sl-a1b2c3")

    sample.assert_called_once_with("SPY")


@pytest.mark.parametrize(
    ("order_type", "time_in_force"),
    [("stop_limit", "gtc"), ("stop", "day")],
)
def test_verified_protection_requires_exact_pure_stop_gtc(
    order_type: str,
    time_in_force: str,
) -> None:
    position = SimpleNamespace(symbol="SPY", qty=1.0, avg_entry_price=100.0)
    stop = SimpleNamespace(
        id="stop-1",
        side="sell",
        type=order_type,
        time_in_force=time_in_force,
        status="new",
        qty="1.0",
        filled_qty="0",
        stop_price="92.0",
        client_order_id="wf-spy-1-sl-a1b2c3",
    )

    with (
        patch(
            "verify_paper_trading._sample_stable_symbol_state",
            return_value=(position, [stop]),
            create=True,
        ),
        patch("verify_paper_trading.get_open_positions", return_value=[position]),
        patch("verify_paper_trading.get_open_orders", return_value=[stop]),
    ):
        assert verification._verified_protective_stop_identity(
            "SPY",
            "wf-spy-1",
            expected_qty=1.0,
        ) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [("new", True), ("pending_cancel", False)],
)
def test_verified_protection_requires_working_stop_status(
    status: str,
    expected: bool,
) -> None:
    position = SimpleNamespace(symbol="SPY", qty=1.0, avg_entry_price=100.0)
    stop = SimpleNamespace(
        id="stop-1",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status=status,
        qty="1.0",
        filled_qty="0",
        stop_price="92.0",
        client_order_id="wf-spy-1-sl",
    )

    with (
        patch("verify_paper_trading.get_open_positions", return_value=[position]),
        patch("verify_paper_trading.get_open_orders", return_value=[stop]),
        patch(
            "verify_paper_trading._sample_stable_symbol_state",
            return_value=(position, [stop]),
            create=True,
        ),
    ):
        assert (
            verification._has_verified_protective_stop("SPY", "wf-spy-1")
            is expected
        )


def test_safety_hold_retries_without_returning_until_proven() -> None:
    manager = MagicMock()

    with (
        patch("verify_paper_trading._ensure_symbol_safe", side_effect=[False, True]) as ensure,
        patch("verify_paper_trading.time.sleep") as sleep,
    ):
        verification._hold_until_symbol_safe("SPY", manager, "wf-spy-1")

    assert ensure.call_count == 2
    sleep.assert_called_once()


def test_safety_hold_logs_and_retries_after_ordinary_exception(capsys) -> None:
    manager = MagicMock()

    with (
        patch(
            "verify_paper_trading._ensure_symbol_safe",
            side_effect=[RuntimeError("transient safety inspection failure"), True],
        ) as ensure,
        patch("verify_paper_trading.time.sleep") as sleep,
    ):
        verification._hold_until_symbol_safe("SPY", manager, "wf-spy-1")

    output = capsys.readouterr().out
    assert "transient safety inspection failure" in output
    assert "retry" in output.lower()
    assert ensure.call_count == 2
    sleep.assert_called_once()


def test_emergency_safety_cancels_unsafe_orders_before_reprotecting() -> None:
    unsafe = ProtectiveStopResult(
        success=False,
        order_id="",
        symbol="SPY",
        qty=0.1,
        stop_price=92.0,
        action="pending_buy",
        error="live buy remains",
    )
    protected = ProtectiveStopResult(
        success=True,
        order_id="stop-2",
        symbol="SPY",
        qty=0.1,
        stop_price=92.0,
        action="submitted",
        client_order_id="wf-spy-1-sl-a1b2c3",
    )
    broker_position = SimpleNamespace(
        symbol="SPY",
        qty=0.1,
        avg_entry_price=100.0,
    )
    recovered = MagicMock(workflow_id="wf-spy-1")
    store = MagicMock()
    store.load_active_position.return_value = {
        "workflow_id": "wf-spy-1",
        "qty": 0.1,
    }

    with (
        patch("verify_paper_trading._reconcile_restart_gap", return_value=True),
        patch("verify_paper_trading._wait_for_symbol_clear", return_value=False),
        patch(
            "verify_paper_trading.reconcile_symbol_after_exit_failure",
            side_effect=[unsafe, protected],
        ) as reconcile,
        patch("verify_paper_trading.cancel_open_orders_verified") as cancel,
        patch("verify_paper_trading.get_active_workflow_for_symbol", return_value=None),
        patch("verify_paper_trading.get_open_positions", return_value=[broker_position]),
        patch("verify_paper_trading.get_or_recover_workflow", return_value=recovered),
        patch("verify_paper_trading.get_execution_store", return_value=store),
        patch("verify_paper_trading._has_verified_protective_stop", return_value=True),
    ):
        assert verification._ensure_symbol_safe("SPY", MagicMock(), "wf-spy-1") is True

    cancel.assert_called_once_with("SPY")
    assert reconcile.call_count == 2
    recovered.repair_buy_fill_storage.assert_called_once_with(
        qty=0.1,
        fill_price=100.0,
        broker_order_id="",
        restore_active=True,
    )
    recovered.mark_protective_stop.assert_called_once()
