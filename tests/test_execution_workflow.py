"""Unit tests for the durable execution workflow state machine."""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from core.execution_workflow import (
    EntryExecutionPlan,
    WorkflowState,
    build_stop_client_order_id,
    clear_workflow_registry,
    get_active_workflow_for_symbol,
    create_entry_workflow,
    get_latest_workflow_for_symbol,
    get_workflow_by_broker_order_id,
    get_workflow_by_client_order_id,
    get_or_recover_workflow,
    normalize_workflow_id,
    reset_workflow_state,
    resolve_workflow,
)


def _plan(symbol: str = "NVDA") -> EntryExecutionPlan:
    return EntryExecutionPlan(
        symbol=symbol,
        entry_price=500.0,
        price_source="intraday_minute_close",
        stop_price=465.0,
        stop_loss_pct=0.07,
        position_value=10_000.0,
        risk_amount=700.0,
        risk_per_share=35.0,
        qty=20.0,
        canslim_score=82.5,
        rs_score=94.0,
        is_breakout=True,
        has_volume_surge=True,
    )


class TestExecutionWorkflow:
    def test_create_entry_workflow_persists_and_recovers_from_store(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan(),
                signal_payload={"symbol": "NVDA", "total_score": 82.5, "rs_score": 94.0},
            )
            clear_workflow_registry()
            recovered = get_or_recover_workflow(workflow.workflow_id, symbol="NVDA")

        assert workflow.state == WorkflowState.PLAN_BUILT
        assert len(workflow.transitions) == 2
        assert workflow.transitions[0].to_state == WorkflowState.SIGNAL_ACCEPTED.value
        assert workflow.transitions[1].to_state == WorkflowState.PLAN_BUILT.value
        assert db_path.exists(), "Execution store DB was not created"
        assert recovered.workflow_id == workflow.workflow_id
        assert recovered.state == WorkflowState.PLAN_BUILT
        assert recovered.entry_plan is not None
        assert recovered.entry_plan.symbol == "NVDA"

    def test_recover_workflow_bootstraps_from_broker_event(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = get_or_recover_workflow("cslm-nvda-1", symbol="NVDA", broker_order_id="broker-1")

        assert workflow.workflow_id == "cslm-nvda-1"
        assert workflow.state == WorkflowState.RECOVERED_FROM_BROKER
        assert workflow.transitions[-1].details["broker_order_id"] == "broker-1"

    def test_order_reference_lookups_recover_same_workflow_after_cache_clear(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("AMD"), signal_payload={"symbol": "AMD"})
            workflow.mark_order_submitted(broker_order_id="broker-entry-1")
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="broker-stop-1",
                stop_price=465.0,
                action="submitted",
            )
            clear_workflow_registry()

            by_symbol = get_latest_workflow_for_symbol("AMD")
            by_broker = get_workflow_by_broker_order_id("broker-entry-1")
            by_client = get_workflow_by_client_order_id(build_stop_client_order_id(workflow.workflow_id))

        assert by_symbol is not None
        assert by_broker is not None
        assert by_client is not None
        assert by_symbol.workflow_id == workflow.workflow_id
        assert by_broker.workflow_id == workflow.workflow_id
        assert by_client.workflow_id == workflow.workflow_id

    def test_normalize_workflow_id_and_stop_suffix_are_inverse(self) -> None:
        workflow_id = "cslm-nvda-20260417120000-ab12cd"
        stop_client_order_id = build_stop_client_order_id(workflow_id)

        assert stop_client_order_id.endswith("-sl")
        assert normalize_workflow_id(stop_client_order_id) == workflow_id
        assert normalize_workflow_id(workflow_id) == workflow_id

    def test_active_position_mapping_survives_cache_clear_and_clears_on_sell_fill(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("MSFT"), signal_payload={"symbol": "MSFT"})
            workflow.mark_order_submitted(broker_order_id="broker-entry-2")
            workflow.mark_buy_fill(qty=15.0, fill_price=410.25, broker_order_id="broker-entry-2")
            clear_workflow_registry()

            recovered_active = get_active_workflow_for_symbol("MSFT")

            assert recovered_active is not None
            assert recovered_active.workflow_id == workflow.workflow_id

            recovered_active.mark_sell_fill(
                qty=15.0,
                fill_price=395.0,
                exit_reason="stop-loss triggered",
                broker_order_id="broker-sell-2",
            )
            clear_workflow_registry()

            assert get_active_workflow_for_symbol("MSFT") is None

    def test_resolve_workflow_uses_strongest_reference_precedence(self, tmp_path) -> None:
        """Explicit, client, broker, active, then latest references win in that order."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            active = create_entry_workflow(_plan("NVDA"), signal_payload={"source": "active"})
            active.mark_order_submitted(broker_order_id="broker-active")
            active.mark_buy_fill(qty=20.0, fill_price=500.0, broker_order_id="broker-active")

            latest = create_entry_workflow(_plan("NVDA"), signal_payload={"source": "latest"})
            latest.mark_order_submitted(broker_order_id="broker-latest")
            clear_workflow_registry()

            assert resolve_workflow(
                symbol="NVDA",
                workflow_id=active.workflow_id,
                client_order_id=latest.workflow_id,
            ).workflow_id == active.workflow_id
            assert resolve_workflow(
                symbol="NVDA",
                client_order_id=latest.workflow_id,
                broker_order_id="broker-active",
            ).workflow_id == latest.workflow_id
            assert resolve_workflow(
                symbol="NVDA",
                broker_order_id="broker-active",
            ).workflow_id == active.workflow_id
            assert resolve_workflow(symbol="NVDA").workflow_id == active.workflow_id

            from core.execution_store import get_execution_store

            get_execution_store().clear_active_position("NVDA")
            clear_workflow_registry()
            assert resolve_workflow(symbol="NVDA").workflow_id == latest.workflow_id
            reset_workflow_state()
