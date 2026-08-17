"""Unit tests for the durable execution workflow state machine."""

from __future__ import annotations

import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest

from core.execution_store import ExecutionStore, get_execution_store
from core.execution_workflow import (
    ClosedSellCheckpoint,
    EntryExecutionPlan,
    WorkflowState,
    build_exit_client_order_id,
    build_stop_client_order_id,
    clear_workflow_registry,
    get_active_workflow_for_symbol,
    create_entry_workflow,
    get_latest_workflow_for_symbol,
    get_workflow,
    get_workflow_by_broker_order_id,
    get_workflow_by_client_order_id,
    get_or_recover_workflow,
    normalize_workflow_id,
    recover_active_position_workflow,
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
    def test_unrelated_protective_transition_does_not_hide_pending_exit_intent(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
            workflow.mark_exit_submission_intent(
                exit_reason="risk exit",
                client_order_id=exit_client_order_id,
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-after-ambiguous-exit",
                stop_price=465.0,
                action="exit_failure_submitted",
            )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert len(pending) == 1
        assert pending[0]["workflow_id"] == workflow.workflow_id
        assert pending[0]["event"] == "exit_submission_intent"
        assert pending[0]["details"]["client_order_id"] == exit_client_order_id

    def test_matching_resolution_marker_clears_pending_exit_intent(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
            workflow.mark_exit_submission_intent(
                exit_reason="risk exit",
                client_order_id=exit_client_order_id,
            )
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="broker_rejected",
                broker_order_id="exit-rejected",
            )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            payload = get_execution_store().load_workflow(workflow.workflow_id)

        assert pending == []
        assert payload is not None
        assert payload["transitions"][-1]["event"] == "submission_intent_resolved"
        assert payload["transitions"][-1]["details"] == {
            "broker_order_id": "exit-rejected",
            "client_order_id": exit_client_order_id,
            "outcome": "broker_rejected",
            "role": "exit",
        }

    def test_entry_submit_failure_resolves_pending_intent_without_marker(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            workflow.mark_order_submit_failed(error="request rejected before acceptance")

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert pending == []

    def test_exit_submit_failure_resolves_pending_intent_without_marker(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
            workflow.mark_exit_submission_intent(
                exit_reason="risk exit",
                client_order_id=exit_client_order_id,
            )
            workflow.mark_exit_order_submit_failed(
                exit_reason="risk exit",
                error="request rejected before acceptance",
            )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert pending == []

    def test_claim_submission_reference_after_unrelated_transition(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
            workflow.mark_exit_submission_intent(
                exit_reason="risk exit",
                client_order_id=exit_client_order_id,
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-after-ambiguous-exit",
                stop_price=465.0,
                action="exit_failure_submitted",
            )

            workflow.claim_order_reference_from_submission_intent(
                broker_order_id="exit-accepted-before-crash",
                client_order_id=exit_client_order_id,
                order_role="exit_order",
                intent_event="exit_submission_intent",
                side="sell",
            )
            payload = get_execution_store().load_workflow(workflow.workflow_id)

        assert payload is not None
        assert any(
            reference["broker_order_id"] == "exit-accepted-before-crash"
            and reference["client_order_id"] == exit_client_order_id
            and reference["order_role"] == "exit_order"
            for reference in payload["order_refs"]
        )

    def test_claim_submission_reference_rejects_resolved_intent(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
            workflow.mark_exit_submission_intent(
                exit_reason="risk exit",
                client_order_id=exit_client_order_id,
            )
            workflow.mark_submission_intent_resolved(
                role="exit",
                client_order_id=exit_client_order_id,
                outcome="broker_rejected",
            )

            with pytest.raises(
                ValueError,
                match="no pending matching submission intent",
            ):
                workflow.claim_order_reference_from_submission_intent(
                    broker_order_id="late-exit-reference",
                    client_order_id=exit_client_order_id,
                    order_role="exit_order",
                    intent_event="exit_submission_intent",
                    side="sell",
                )

    @pytest.mark.parametrize("notification_kind", ["buy_fill", "sell_fill"])
    def test_notification_claim_is_atomic_across_store_instances(
        self,
        tmp_path: Path,
        notification_kind: str,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"
        stores = [ExecutionStore(str(db_path)), ExecutionStore(str(db_path))]
        barrier = Barrier(2)

        def claim(store: ExecutionStore) -> bool:
            barrier.wait()
            return store.claim_notification(
                workflow_id="workflow-atomic-claim",
                notification_kind=notification_kind,
                claimed_at_utc="2026-08-17T00:00:00Z",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, stores))

        assert sorted(results) == [False, True]

    @pytest.mark.parametrize(
        ("notification_kind", "event", "state"),
        [
            (
                "buy_fill",
                "buy_fill_notification_claimed",
                WorkflowState.BUY_FILL_NOTIFICATION_PENDING,
            ),
            (
                "buy_fill",
                "buy_fill_notified",
                WorkflowState.BUY_FILL_NOTIFICATION_SENT,
            ),
            (
                "sell_fill",
                "sell_fill_notification_claimed",
                WorkflowState.SELL_NOTIFICATION_PENDING,
            ),
            (
                "sell_fill",
                "sell_fill_notified",
                WorkflowState.SELL_NOTIFICATION_SENT,
            ),
        ],
    )
    def test_legacy_notification_transition_is_backfilled_and_suppresses_claim(
        self,
        tmp_path: Path,
        notification_kind: str,
        event: str,
        state: WorkflowState,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"source": "legacy-notification"},
            )
            workflow.transition(state, event=event, details={"channel": "email"})

            claimed = get_execution_store().claim_notification(
                workflow_id=workflow.workflow_id,
                notification_kind=notification_kind,
                claimed_at_utc="2026-08-17T00:00:01Z",
            )

            with closing(sqlite3.connect(db_path)) as conn:
                durable_claim = conn.execute(
                    """
                    SELECT workflow_id, notification_kind
                    FROM workflow_notification_claims
                    WHERE workflow_id = ? AND notification_kind = ?
                    """,
                    (workflow.workflow_id, notification_kind),
                ).fetchone()

        assert claimed is False
        assert durable_claim == (workflow.workflow_id, notification_kind)

    def test_reset_clears_durable_notification_claims(self, tmp_path: Path) -> None:
        store = ExecutionStore(str(tmp_path / "execution.sqlite3"))
        claim = {
            "workflow_id": "workflow-reset-claim",
            "notification_kind": "buy_fill",
            "claimed_at_utc": "2026-08-17T00:00:00Z",
        }

        assert store.claim_notification(**claim) is True
        store.reset()
        assert store.claim_notification(**claim) is True

    @pytest.mark.parametrize(
        "claim_method",
        ["claim_buy_fill_notification", "claim_sell_notification"],
    )
    def test_stale_workflow_instance_cannot_duplicate_notification_claim(
        self,
        tmp_path: Path,
        claim_method: str,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"source": "stale-claim"},
            )
            clear_workflow_registry()
            stale_workflow = get_workflow(workflow.workflow_id)

            assert stale_workflow is not None
            assert getattr(workflow, claim_method)() is True
            assert getattr(stale_workflow, claim_method)() is False

    def test_orphan_broker_position_gets_fresh_durable_active_workflow(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            unrelated_latest = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"source": "unrelated_terminal_workflow"},
            )

            recovered = recover_active_position_workflow(
                "NVDA",
                qty=3.0,
                avg_entry_price=101.25,
            )
            first_transition_count = len(recovered.transitions)
            clear_workflow_registry()
            repeated = recover_active_position_workflow(
                "NVDA",
                qty=3.0,
                avg_entry_price=101.25,
            )
            active_record = get_execution_store().load_active_position("NVDA")

        assert recovered.workflow_id != unrelated_latest.workflow_id
        assert repeated.workflow_id == recovered.workflow_id
        assert len(repeated.transitions) == first_transition_count
        assert repeated.transitions[-1].event == "active_position_recovered_from_broker"
        assert active_record is not None
        assert active_record["workflow_id"] == recovered.workflow_id
        assert active_record["qty"] == 3.0
        assert active_record["entry_price"] == 101.25

    def test_orphan_recovery_heals_dangling_active_workflow_id(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            get_execution_store().upsert_active_position(
                symbol="AMD",
                workflow_id="dangling-amd-workflow",
                qty=2.0,
                entry_price=150.0,
                opened_at_utc="2026-08-17T00:00:00Z",
                updated_at_utc="2026-08-17T00:00:00Z",
            )

            recovered = recover_active_position_workflow(
                "AMD",
                qty=2.0,
                avg_entry_price=150.0,
            )
            clear_workflow_registry()
            durable = get_active_workflow_for_symbol("AMD")

        assert recovered.workflow_id == "dangling-amd-workflow"
        assert durable is not None
        assert durable.workflow_id == "dangling-amd-workflow"

    def test_orphan_recovery_rejects_invalid_position_values_without_writing(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            with pytest.raises(ValueError, match="positive"):
                recover_active_position_workflow("AMD", qty=0.0, avg_entry_price=150.0)
            assert get_execution_store().load_active_position("AMD") is None

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

    def test_normalize_workflow_id_and_order_suffixes_are_inverse(self) -> None:
        workflow_id = "cslm-nvda-20260417120000-ab12cd"
        stop_client_order_id = build_stop_client_order_id(workflow_id)
        retry_stop_client_order_id = build_stop_client_order_id(workflow_id, "a1b2c3")
        exit_client_order_id = build_exit_client_order_id(workflow_id)

        assert stop_client_order_id.endswith("-sl")
        assert retry_stop_client_order_id.endswith("-sl-a1b2c3")
        assert exit_client_order_id.endswith("-exit")
        assert normalize_workflow_id(stop_client_order_id) == workflow_id
        assert normalize_workflow_id(retry_stop_client_order_id) == workflow_id
        assert normalize_workflow_id(exit_client_order_id) == workflow_id
        assert normalize_workflow_id(workflow_id) == workflow_id

        delimiter_workflow_id = "cslm-sl-20260817000000-abcdef"
        retry_for_delimiter_id = build_stop_client_order_id(
            delimiter_workflow_id,
            "retry1",
        )
        assert normalize_workflow_id(delimiter_workflow_id) == delimiter_workflow_id
        assert normalize_workflow_id(retry_for_delimiter_id) == delimiter_workflow_id

    def test_exit_order_reference_uses_distinct_exit_client_order_id(self) -> None:
        db_path = Path(tempfile.gettempdir()) / f"exec_store_test_{uuid4().hex}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={"symbol": "NVDA"})
            workflow.mark_exit_order_submitted(
                exit_reason="verification cleanup",
                broker_order_id="broker-exit-1",
            )

            from core.execution_store import get_execution_store

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            reset_workflow_state()

        assert payload is not None
        exit_refs = [
            ref for ref in payload["order_refs"] if ref["order_role"] == "exit_order"
        ]
        assert exit_refs == [
            {
                "broker_order_id": "broker-exit-1",
                "client_order_id": build_exit_client_order_id(workflow.workflow_id),
                "order_role": "exit_order",
                "created_at_utc": exit_refs[0]["created_at_utc"],
            }
        ]
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
                client_order_id=f"{workflow.workflow_id}-sl",
            )
            clear_workflow_registry()

            assert get_active_workflow_for_symbol("MSFT") is None

    def test_buy_fill_can_persist_audit_without_overwriting_foreign_owner(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            owner = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"source": "current-owner"},
            )
            owner.mark_buy_fill(qty=20.0, fill_price=500.0, broker_order_id="owner-buy")
            audit = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"source": "old-fill-audit"},
            )

            audit.mark_buy_fill(
                qty=5.0,
                fill_price=490.0,
                broker_order_id="old-buy",
                restore_active=False,
            )
            active = get_execution_store().load_active_position("NVDA")
            durable_audit = get_execution_store().load_workflow(audit.workflow_id)

        assert active is not None
        assert active["workflow_id"] == owner.workflow_id
        assert durable_audit is not None
        assert durable_audit["transitions"][-1]["event"] == "buy_fill_received"

    def test_residual_sell_fill_can_retain_active_ownership(self, tmp_path: Path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan("AMD"),
                signal_payload={"source": "residual-sell"},
            )
            workflow.mark_buy_fill(qty=10.0, fill_price=150.0, broker_order_id="buy-1")

            workflow.mark_sell_fill(
                qty=4.0,
                fill_price=145.0,
                exit_reason="partial stop fill",
                broker_order_id="sell-1",
                client_order_id=f"{workflow.workflow_id}-sl",
                clear_active=False,
            )
            active = get_execution_store().load_active_position("AMD")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert workflow.transitions[-1].event == "sell_fill_received"

    def test_partial_sell_fill_records_audit_and_retains_active_ownership(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan("AMD"),
                signal_payload={"source": "partial-sell"},
            )
            workflow.mark_buy_fill(qty=10.0, fill_price=150.0, broker_order_id="buy-1")

            workflow.mark_sell_partial_fill(
                qty=4.0,
                fill_price=145.0,
                broker_order_id="sell-partial-1",
                client_order_id=f"{workflow.workflow_id}-sl",
            )
            active = get_execution_store().load_active_position("AMD")
            durable = get_execution_store().load_workflow(workflow.workflow_id)

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert workflow.state == WorkflowState.SELL_PARTIAL_FILL_RECEIVED
        assert workflow.transitions[-1].event == "sell_partial_fill_received"
        assert durable is not None
        assert durable["order_refs"][-1]["broker_order_id"] == "sell-partial-1"
        assert durable["order_refs"][-1]["order_role"] == "sell_fill"

    def test_residual_sell_repair_can_retain_active_ownership(self, tmp_path: Path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan("AMD"),
                signal_payload={"source": "residual-repair"},
            )
            workflow.mark_buy_fill(qty=10.0, fill_price=150.0, broker_order_id="buy-1")

            workflow.repair_sell_fill_storage(
                broker_order_id="sell-repair-1",
                client_order_id=f"{workflow.workflow_id}-sl",
                clear_active=False,
            )
            active = get_execution_store().load_active_position("AMD")
            repaired = get_workflow_by_broker_order_id("sell-repair-1")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert repaired is not None
        assert repaired.workflow_id == workflow.workflow_id

    def test_resolve_workflow_requires_all_explicit_references_to_agree(
        self,
        tmp_path: Path,
    ) -> None:
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
            ) is None
            assert resolve_workflow(
                symbol="NVDA",
                client_order_id=latest.workflow_id,
                broker_order_id="broker-active",
            ) is None
            resolved = resolve_workflow(
                symbol="NVDA",
                workflow_id=active.workflow_id,
                client_order_id=f"{active.workflow_id}-sl",
                broker_order_id="broker-active",
            )
            assert resolved is not None
            assert resolved.workflow_id == active.workflow_id
            assert get_workflow(active.workflow_id) is resolved
            assert get_workflow_by_client_order_id(
                f"{active.workflow_id}-sl"
            ) is resolved
            assert get_workflow_by_broker_order_id("broker-active") is resolved
            assert resolve_workflow(
                symbol="NVDA",
                workflow_id=active.workflow_id,
                broker_order_id="broker-missing",
            ) is None
            assert resolve_workflow(
                symbol="NVDA",
                workflow_id="wf-unknown-old",
            ) is None
            assert resolve_workflow(
                symbol="NVDA",
                client_order_id="wf-unknown-old-exit",
            ) is None
            assert resolve_workflow(
                symbol="MSFT",
                workflow_id=active.workflow_id,
            ) is None
            assert resolve_workflow(symbol="NVDA").workflow_id == active.workflow_id

            from core.execution_store import get_execution_store

            get_execution_store().clear_active_position("NVDA")
            clear_workflow_registry()
            assert resolve_workflow(symbol="NVDA").workflow_id == latest.workflow_id
            reset_workflow_state()

    def test_resolve_workflow_accepts_torn_order_ref_when_transition_is_durable(
        self,
        tmp_path: Path,
    ) -> None:
        """A replay may repair an order ref already evidenced by its fill transition."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={})
            workflow.transition(
                WorkflowState.SELL_FILL_RECEIVED,
                event="sell_fill_received",
                details={
                    "qty": 1.0,
                    "fill_price": 490.0,
                    "broker_order_id": "sell-torn-ref",
                },
            )
            clear_workflow_registry()

            resolved = resolve_workflow(
                symbol="NVDA",
                workflow_id=workflow.workflow_id,
                client_order_id=f"{workflow.workflow_id}-exit",
                broker_order_id="sell-torn-ref",
            )

        assert resolved is not None
        assert resolved.workflow_id == workflow.workflow_id

    def test_loading_old_workflow_does_not_relabel_it_as_latest(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old = create_entry_workflow(_plan("NVDA"), signal_payload={"generation": 1})
            latest = create_entry_workflow(
                _plan("NVDA"),
                signal_payload={"generation": 2},
            )
            clear_workflow_registry()

            assert get_workflow(old.workflow_id) is not None
            resolved_latest = get_latest_workflow_for_symbol("NVDA")

        assert resolved_latest is not None
        assert resolved_latest.workflow_id == latest.workflow_id

    def test_active_lookup_revalidates_durable_owner(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            first = create_entry_workflow(_plan("NVDA"), signal_payload={"generation": 1})
            first.mark_buy_fill(qty=1.0, fill_price=500.0, broker_order_id="buy-a")
            second = create_entry_workflow(_plan("NVDA"), signal_payload={"generation": 2})
            assert get_active_workflow_for_symbol("NVDA") is first

            external = ExecutionStore(str(db_path))
            external.upsert_active_position(
                symbol="NVDA",
                workflow_id=second.workflow_id,
                qty=2.0,
                entry_price=510.0,
                opened_at_utc=second.created_at_utc,
                updated_at_utc=second.updated_at_utc,
            )

            resolved = get_active_workflow_for_symbol("NVDA")

        assert resolved is second

    def test_stale_repair_cannot_overwrite_new_durable_owner(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            stale = create_entry_workflow(_plan("NVDA"), signal_payload={"generation": 1})
            stale.mark_buy_fill(qty=1.0, fill_price=500.0, broker_order_id="buy-a")
            current = create_entry_workflow(_plan("NVDA"), signal_payload={"generation": 2})
            store = get_execution_store()
            store.upsert_active_position(
                symbol="NVDA",
                workflow_id=current.workflow_id,
                qty=2.0,
                entry_price=510.0,
                opened_at_utc=current.created_at_utc,
                updated_at_utc=current.updated_at_utc,
            )

            stale.repair_buy_fill_storage(
                qty=3.0,
                fill_price=490.0,
                broker_order_id="buy-a",
            )

            active = store.load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == current.workflow_id
        assert active["qty"] == 2.0

    def test_flat_clear_rejects_changed_quantity_with_reused_timestamp(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"
        store = ExecutionStore(str(db_path))
        observed = {
            "symbol": "NVDA",
            "workflow_id": "wf-active",
            "qty": 5.0,
            "entry_price": 500.0,
            "opened_at_utc": "2026-08-17T00:00:00Z",
            "updated_at_utc": "2026-08-17T00:00:01Z",
        }
        store.upsert_active_position(**observed)
        store.upsert_active_position(
            **{
                **observed,
                "qty": 7.0,
            }
        )

        cleared = store.clear_active_position_if_unchanged(**observed)
        active = store.load_active_position("NVDA")

        assert cleared is False
        assert active is not None
        assert active["qty"] == 7.0

    def test_stale_same_state_transition_count_fails_across_store_instances(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"
        first = ExecutionStore(str(db_path))
        second = ExecutionStore(str(db_path))
        base = {
            "workflow_id": "wf-stale-count",
            "symbol": "NVDA",
            "to_state": WorkflowState.BUY_FILL_RECEIVED.value,
            "broker_order_id": "entry-1",
            "entry_plan": None,
            "created_at_utc": "2026-08-17T00:00:00Z",
        }

        first.persist_transition_and_snapshot(
            **base,
            timestamp_utc="2026-08-17T00:00:01Z",
            from_state=None,
            event="initial_buy_fill",
            details={"qty": 5.0},
            expected_transition_count=0,
        )
        second.persist_transition_and_snapshot(
            **base,
            timestamp_utc="2026-08-17T00:00:02Z",
            from_state=WorkflowState.BUY_FILL_RECEIVED.value,
            event="higher_buy_checkpoint",
            details={"qty": 20.0},
            expected_transition_count=1,
        )

        with pytest.raises(RuntimeError, match="expected 1.*found 2"):
            first.persist_transition_and_snapshot(
                **base,
                timestamp_utc="2026-08-17T00:00:03Z",
                from_state=WorkflowState.BUY_FILL_RECEIVED.value,
                event="stale_buy_checkpoint",
                details={"qty": 10.0},
                expected_transition_count=1,
            )

        payload = first.load_workflow("wf-stale-count")
        assert payload is not None
        assert [item["event"] for item in payload["transitions"]] == [
            "initial_buy_fill",
            "higher_buy_checkpoint",
        ]

    def test_buy_checkpoint_repair_preserves_greater_owned_aggregate(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=505.0,
                broker_order_id="entry-1",
            )

            workflow.repair_buy_fill_storage(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == 20.0
        assert active["entry_price"] == 505.0

    def test_broker_truth_repair_can_explicitly_decrease_owned_aggregate(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=505.0,
                broker_order_id="entry-1",
            )

            workflow.repair_buy_fill_storage(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
                preserve_higher_qty=False,
            )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == 10.0
        assert active["entry_price"] == 500.0

    def test_broker_order_id_cannot_be_remapped_to_another_workflow(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            first = create_entry_workflow(_plan("NVDA"), signal_payload={})
            second = create_entry_workflow(_plan("NVDA"), signal_payload={})
            first.mark_order_submitted(broker_order_id="broker-shared")

            with pytest.raises(ValueError, match="broker order id"):
                second.mark_order_submitted(broker_order_id="broker-shared")

            clear_workflow_registry()
            resolved = get_workflow_by_broker_order_id("broker-shared")

        assert resolved is not None
        assert resolved.workflow_id == first.workflow_id

    def test_normalized_client_id_conflict_fails_closed(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            normalized = get_or_recover_workflow("wf-a", symbol="NVDA")
            conflicting = get_or_recover_workflow("wf-b", symbol="NVDA")
            get_execution_store().record_order_reference(
                workflow_id=conflicting.workflow_id,
                symbol="NVDA",
                client_order_id="wf-a-exit",
                order_role="exit_order",
                created_at_utc=conflicting.updated_at_utc,
            )
            clear_workflow_registry()

            assert get_workflow("wf-a") is not None
            assert get_workflow_by_client_order_id("wf-a-exit") is None
            assert normalized.workflow_id == "wf-a"

    def test_recovery_rejects_existing_workflow_symbol_mismatch(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            get_or_recover_workflow("wf-shared", symbol="NVDA")

            with pytest.raises(ValueError, match="belongs to NVDA"):
                get_or_recover_workflow("wf-shared", symbol="MSFT")

    def test_concurrent_exact_recovery_returns_one_canonical_workflow(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            barrier = Barrier(16)

            def recover() -> object:
                barrier.wait()
                return get_or_recover_workflow(
                    "wf-race",
                    symbol="NVDA",
                    broker_order_id="broker-race",
                )

            with ThreadPoolExecutor(max_workers=16) as pool:
                recovered = list(pool.map(lambda _index: recover(), range(16)))

            payload = get_execution_store().load_workflow("wf-race")

        assert len({id(item) for item in recovered}) == 1
        assert payload is not None
        assert [
            item["event"] for item in payload["transitions"]
        ].count("workflow_recovered_from_broker_event") == 1

    def test_concurrent_active_recovery_creates_one_owner(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            barrier = Barrier(12)

            def recover() -> object:
                barrier.wait()
                return recover_active_position_workflow(
                    "NVDA",
                    qty=2.0,
                    avg_entry_price=500.0,
                )

            with ThreadPoolExecutor(max_workers=12) as pool:
                recovered = list(pool.map(lambda _index: recover(), range(12)))

            active = get_execution_store().load_active_position("NVDA")

        assert len({item.workflow_id for item in recovered}) == 1
        assert len({id(item) for item in recovered}) == 1
        assert active is not None
        assert active["workflow_id"] == recovered[0].workflow_id

    def test_cached_workflow_refreshes_in_place_from_durable_store(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"
        refreshed_at = "9999-12-31T23:59:59.999999Z"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            cached = get_or_recover_workflow(
                "wf-refresh",
                symbol="NVDA",
                broker_order_id="entry-1",
            )
            external = ExecutionStore(str(db_path))
            external.upsert_workflow_snapshot(
                workflow_id=cached.workflow_id,
                symbol="NVDA",
                state=WorkflowState.SELL_FILL_RECEIVED.value,
                broker_order_id="sell-1",
                entry_plan=None,
                created_at_utc=cached.created_at_utc,
                updated_at_utc=refreshed_at,
            )
            external.append_transition(
                timestamp_utc=refreshed_at,
                workflow_id=cached.workflow_id,
                symbol="NVDA",
                from_state=WorkflowState.RECOVERED_FROM_BROKER.value,
                to_state=WorkflowState.SELL_FILL_RECEIVED.value,
                event="sell_fill_received",
                details={
                    "qty": 1.0,
                    "fill_price": 490.0,
                    "broker_order_id": "sell-1",
                },
            )
            external.record_order_reference(
                workflow_id=cached.workflow_id,
                symbol="NVDA",
                broker_order_id="sell-1",
                client_order_id="wf-refresh-exit",
                order_role="sell_fill",
                created_at_utc=refreshed_at,
            )

            refreshed = get_workflow_by_broker_order_id("sell-1")

        assert refreshed is cached
        assert refreshed.state is WorkflowState.SELL_FILL_RECEIVED
        assert refreshed.broker_order_id == "sell-1"
        assert refreshed.transitions[-1].event == "sell_fill_received"

    def test_cross_symbol_active_reference_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={})
            get_execution_store().upsert_active_position(
                symbol="AAPL",
                workflow_id=workflow.workflow_id,
                qty=1.0,
                entry_price=200.0,
                opened_at_utc=workflow.created_at_utc,
                updated_at_utc=workflow.updated_at_utc,
            )

            with pytest.raises(RuntimeError, match="references NVDA workflow"):
                get_active_workflow_for_symbol("AAPL")

    def test_transition_snapshot_and_event_rollback_atomically(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan("NVDA"), signal_payload={})
            before_state = workflow.state
            before_events = [item.event for item in workflow.transitions]
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_forced_transition
                    BEFORE INSERT ON workflow_transitions
                    WHEN NEW.event = 'forced_failure'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced transition failure');
                    END
                    """
                )

            with pytest.raises(sqlite3.IntegrityError, match="forced transition"):
                workflow.transition(
                    WorkflowState.BUY_FILL_RECEIVED,
                    event="forced_failure",
                    details={"qty": 1.0},
                )

            durable = get_execution_store().load_workflow(workflow.workflow_id)

        assert workflow.state is before_state
        assert [item.event for item in workflow.transitions] == before_events
        assert durable is not None
        assert durable["state"] == before_state.value
        assert [item["event"] for item in durable["transitions"]] == before_events

    def test_legacy_ambiguous_broker_reference_fails_closed(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            first = create_entry_workflow(_plan("NVDA"), signal_payload={})
            second = create_entry_workflow(_plan("NVDA"), signal_payload={})
            first.mark_order_submitted(broker_order_id="legacy-shared")
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO workflow_order_refs (
                        workflow_id,
                        symbol,
                        broker_order_id,
                        client_order_id,
                        order_role,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        second.workflow_id,
                        "NVDA",
                        "legacy-shared",
                        second.workflow_id,
                        "legacy_conflict",
                        second.updated_at_utc,
                    ),
                )
            clear_workflow_registry()

            assert get_workflow_by_broker_order_id("legacy-shared") is None

    def test_resolve_rejects_ambiguous_broker_despite_transition_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            first = create_entry_workflow(_plan("NVDA"), signal_payload={})
            second = create_entry_workflow(_plan("NVDA"), signal_payload={})
            first.mark_order_submitted(broker_order_id="legacy-shared")
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO workflow_order_refs (
                        workflow_id,
                        symbol,
                        broker_order_id,
                        client_order_id,
                        order_role,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        second.workflow_id,
                        "NVDA",
                        "legacy-shared",
                        second.workflow_id,
                        "legacy_conflict",
                        second.updated_at_utc,
                    ),
                )
            clear_workflow_registry()

            resolved = resolve_workflow(
                symbol="NVDA",
                workflow_id=first.workflow_id,
                client_order_id=first.workflow_id,
                broker_order_id="legacy-shared",
            )

        assert resolved is None

    def test_normalized_client_fallback_rejects_ambiguous_exact_reference(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            normalized = get_or_recover_workflow("wf-a", symbol="NVDA")
            conflicting = get_or_recover_workflow("wf-b", symbol="NVDA")
            store = get_execution_store()
            store.record_order_reference(
                workflow_id=normalized.workflow_id,
                symbol="NVDA",
                client_order_id="wf-a-exit",
                order_role="exit_order",
                created_at_utc=normalized.updated_at_utc,
            )
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO workflow_order_refs (
                        workflow_id,
                        symbol,
                        broker_order_id,
                        client_order_id,
                        order_role,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conflicting.workflow_id,
                        "NVDA",
                        "",
                        "wf-a-exit",
                        "legacy_conflict",
                        conflicting.updated_at_utc,
                    ),
                )
            clear_workflow_registry()

            direct = get_workflow_by_client_order_id("wf-a-exit")
            resolved = resolve_workflow(
                symbol="NVDA",
                client_order_id="wf-a-exit",
            )

        assert direct is None
        assert resolved is None


class TestClosedSellChainReplay:
    def test_replay_persists_parent_and_final_before_clearing_owner(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_buy_fill(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            checkpoints = [
                ClosedSellCheckpoint(
                    broker_order_id="stop-parent",
                    client_order_id=f"{workflow.workflow_id}-sl",
                    qty=4.0,
                    fill_price=465.0,
                ),
                ClosedSellCheckpoint(
                    broker_order_id="stop-child",
                    client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
                    qty=6.0,
                    fill_price=464.0,
                ),
            ]

            workflow.replay_closed_sell_chain(
                checkpoints,
                exit_reason="protective stop filled during restart",
            )

            snapshot = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert snapshot is not None
        assert [
            (
                transition["event"],
                transition["details"]["broker_order_id"],
                transition["details"]["qty"],
            )
            for transition in snapshot["transitions"]
            if transition["event"]
            in {"sell_partial_fill_received", "sell_fill_received"}
        ] == [
            ("sell_partial_fill_received", "stop-parent", 4.0),
            ("sell_fill_received", "stop-child", 6.0),
        ]
        assert {
            (
                reference["broker_order_id"],
                reference["client_order_id"],
            )
            for reference in snapshot["order_refs"]
            if reference["order_role"] == "sell_fill"
        } == {
            ("stop-parent", f"{workflow.workflow_id}-sl"),
            ("stop-child", f"{workflow.workflow_id}-sl-a1b2c3"),
        }
        assert active is None

    def test_replay_is_idempotent_for_already_persisted_checkpoints(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_buy_fill(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            checkpoints = [
                ClosedSellCheckpoint(
                    broker_order_id="stop-parent",
                    client_order_id=f"{workflow.workflow_id}-sl",
                    qty=4.0,
                    fill_price=465.0,
                ),
                ClosedSellCheckpoint(
                    broker_order_id="stop-child",
                    client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
                    qty=6.0,
                    fill_price=464.0,
                ),
            ]

            for _ in range(2):
                workflow.replay_closed_sell_chain(
                    checkpoints,
                    exit_reason="protective stop filled during restart",
                )

            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert snapshot is not None
        sell_transitions = [
            transition
            for transition in snapshot["transitions"]
            if transition["event"]
            in {"sell_partial_fill_received", "sell_fill_received"}
        ]
        sell_references = [
            reference
            for reference in snapshot["order_refs"]
            if reference["order_role"] == "sell_fill"
        ]
        assert len(sell_transitions) == 2
        assert len(sell_references) == 2

    def test_replay_repairs_crash_after_final_checkpoint_before_owner_clear(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_buy_fill(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            checkpoints = [
                ClosedSellCheckpoint(
                    broker_order_id="stop-parent",
                    client_order_id=f"{workflow.workflow_id}-sl",
                    qty=4.0,
                    fill_price=465.0,
                ),
                ClosedSellCheckpoint(
                    broker_order_id="stop-child",
                    client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
                    qty=6.0,
                    fill_price=464.0,
                ),
            ]

            with (
                patch.object(
                    workflow,
                    "_clear_active_position",
                    side_effect=RuntimeError("simulated process crash"),
                ),
                pytest.raises(RuntimeError, match="simulated process crash"),
            ):
                workflow.replay_closed_sell_chain(
                    checkpoints,
                    exit_reason="protective stop filled during restart",
                )

            torn_snapshot = get_execution_store().load_workflow(workflow.workflow_id)
            torn_active = get_execution_store().load_active_position("NVDA")
            workflow.replay_closed_sell_chain(
                checkpoints,
                exit_reason="protective stop filled during restart",
            )
            repaired_snapshot = get_execution_store().load_workflow(
                workflow.workflow_id
            )
            repaired_active = get_execution_store().load_active_position("NVDA")

        assert torn_snapshot is not None
        assert torn_active is not None
        assert [
            transition["event"]
            for transition in torn_snapshot["transitions"]
            if transition["event"]
            in {"sell_partial_fill_received", "sell_fill_received"}
        ] == ["sell_partial_fill_received", "sell_fill_received"]
        assert {
            reference["broker_order_id"]
            for reference in torn_snapshot["order_refs"]
            if reference["order_role"] == "sell_fill"
        } == {"stop-parent", "stop-child"}
        assert repaired_snapshot is not None
        assert len(
            [
                transition
                for transition in repaired_snapshot["transitions"]
                if transition["event"]
                in {"sell_partial_fill_received", "sell_fill_received"}
            ]
        ) == 2
        assert repaired_active is None

    @pytest.mark.parametrize(
        "raw_checkpoints",
        [
            pytest.param([], id="empty-chain"),
            pytest.param(
                [
                    {
                        "broker_order_id": " ",
                        "client_order_id": "wf-placeholder-sl",
                        "qty": 10.0,
                        "fill_price": 465.0,
                    }
                ],
                id="blank-broker-id",
            ),
            pytest.param(
                [
                    {
                        "broker_order_id": "stop-final",
                        "client_order_id": " ",
                        "qty": 10.0,
                        "fill_price": 465.0,
                    }
                ],
                id="blank-client-id",
            ),
            pytest.param(
                [
                    {
                        "broker_order_id": "stop-final",
                        "client_order_id": "wf-placeholder-sl",
                        "qty": 0.0,
                        "fill_price": 465.0,
                    }
                ],
                id="nonpositive-quantity",
            ),
            pytest.param(
                [
                    {
                        "broker_order_id": "stop-final",
                        "client_order_id": "wf-placeholder-sl",
                        "qty": 10.0,
                        "fill_price": 0.0,
                    }
                ],
                id="nonpositive-price",
            ),
            pytest.param(
                [
                    {
                        "broker_order_id": "stop-parent",
                        "client_order_id": "wf-placeholder-sl",
                        "qty": 4.0,
                        "fill_price": 465.0,
                    },
                    {
                        "broker_order_id": "stop-child",
                        "client_order_id": "wf-placeholder-sl-child",
                        "qty": -1.0,
                        "fill_price": 464.0,
                    },
                ],
                id="invalid-final-after-valid-parent",
            ),
        ],
    )
    def test_replay_validates_entire_chain_before_writing(
        self,
        tmp_path: Path,
        raw_checkpoints: list[dict[str, object]],
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_buy_fill(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            checkpoints = [
                ClosedSellCheckpoint(**checkpoint)
                for checkpoint in raw_checkpoints
            ]

            with pytest.raises(ValueError, match="closed sell chain"):
                workflow.replay_closed_sell_chain(
                    checkpoints,
                    exit_reason="protective stop filled during restart",
                )

            snapshot = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert snapshot is not None
        assert not any(
            transition["event"]
            in {"sell_partial_fill_received", "sell_fill_received"}
            for transition in snapshot["transitions"]
        )
        assert active is not None

    def test_replay_rejects_final_quantity_below_durable_order_checkpoint(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={})
            workflow.mark_buy_fill(
                qty=10.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            client_order_id = f"{workflow.workflow_id}-sl"
            workflow.mark_sell_partial_fill(
                qty=6.0,
                fill_price=465.0,
                broker_order_id="stop-final",
                client_order_id=client_order_id,
            )

            with pytest.raises(ValueError, match="cannot regress"):
                workflow.replay_closed_sell_chain(
                    [
                        ClosedSellCheckpoint(
                            broker_order_id="stop-final",
                            client_order_id=client_order_id,
                            qty=5.0,
                            fill_price=464.0,
                        )
                    ],
                    exit_reason="protective stop filled during restart",
                )

            snapshot = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert snapshot is not None
        assert not any(
            transition["event"] == "sell_fill_received"
            for transition in snapshot["transitions"]
        )
        assert active is not None
