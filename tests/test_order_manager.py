"""Unit tests for the high-level OrderManager execution service."""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.execution_store import get_execution_store
from core.execution_workflow import (
    EntryExecutionPlan,
    WorkflowState,
    create_entry_workflow,
    reset_workflow_state,
    resolve_workflow as strict_resolve_workflow,
)
from core.order_execution import OrderResult, PositionSummary, ProtectiveStopResult
from core.order_manager import OrderManager


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
        canslim_score=82.0,
        rs_score=94.0,
        is_breakout=True,
        has_volume_surge=True,
    )


class TestSubmitEntry:
    def test_submit_entry_creates_workflow_submits_order_and_notifies(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_dry_run_skipped=MagicMock(),
            mark_order_submission_intent=MagicMock(),
            mark_order_submitted=MagicMock(),
            mark_entry_notification=MagicMock(),
            mark_order_submit_failed=MagicMock(),
            mark_submission_intent_resolved=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.create_entry_workflow", return_value=workflow),
            patch(
                "core.order_manager.submit_bracket_buy",
                return_value=OrderResult(True, "broker-1", "NVDA", "buy", 20.0, client_order_id="wf-nvda-1"),
            ) as mock_submit,
            patch("core.order_manager.notify_entry_submitted", return_value=True) as mock_notify,
        ):
            outcome = manager.submit_entry(_plan(), signal_payload={"symbol": "NVDA"})

        assert outcome.success is True
        assert outcome.workflow_id == "wf-nvda-1"
        mock_submit.assert_called_once()
        assert mock_submit.call_args.kwargs["client_order_id"] == "wf-nvda-1"
        mock_notify.assert_called_once()
        workflow.mark_order_submitted.assert_called_once_with(broker_order_id="broker-1")
        workflow.mark_entry_notification.assert_called_once_with(sent=True)

    def test_submit_entry_marks_dry_run_without_order(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-amd-1",
            mark_dry_run_skipped=MagicMock(),
            mark_order_submitted=MagicMock(),
            mark_entry_notification=MagicMock(),
            mark_order_submit_failed=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.create_entry_workflow", return_value=workflow),
            patch("core.order_manager.submit_bracket_buy") as mock_submit,
        ):
            outcome = manager.submit_entry(_plan("AMD"), signal_payload={"symbol": "AMD"}, dry_run=True)

        assert outcome.success is True
        assert outcome.dry_run is True
        mock_submit.assert_not_called()
        workflow.mark_dry_run_skipped.assert_called_once()

    def test_entry_submit_serializes_order_reference_before_stream_fill(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"
        broker_submit_started = threading.Event()
        allow_broker_return = threading.Event()
        fill_resolution_attempted = threading.Event()
        fill_errors: list[BaseException] = []
        submission_errors: list[BaseException] = []
        submission_outcomes: list[object] = []

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            stop = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl",
            )
            manager = OrderManager(paper=True)

            def delayed_submit(**_kwargs):
                broker_submit_started.set()
                if not allow_broker_return.wait(timeout=3.0):
                    raise TimeoutError("test did not release broker response")
                return OrderResult(
                    True,
                    "entry-race",
                    "NVDA",
                    "buy",
                    20.0,
                    client_order_id=workflow.workflow_id,
                )

            def observed_resolve(**kwargs):
                fill_resolution_attempted.set()
                return strict_resolve_workflow(**kwargs)

            def process_fill() -> None:
                try:
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-race",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )
                except BaseException as exc:  # noqa: BLE001 - thread handoff for assertion
                    fill_errors.append(exc)

            def process_submission() -> None:
                try:
                    submission_outcomes.append(manager.submit_entry(_plan()))
                except BaseException as exc:  # noqa: BLE001 - thread handoff for assertion
                    submission_errors.append(exc)

            with (
                patch("core.order_manager.create_entry_workflow", return_value=workflow),
                patch("core.order_manager.submit_bracket_buy", side_effect=delayed_submit),
                patch("core.order_manager.resolve_workflow", side_effect=observed_resolve),
                patch("core.order_manager.ensure_protective_stop", return_value=stop),
                patch("core.order_manager.notify_entry_submitted", return_value=True),
                patch("core.order_manager.notify_buy_filled", return_value=True),
            ):
                submit_thread = threading.Thread(
                    target=process_submission,
                    daemon=True,
                )
                submit_thread.start()
                assert broker_submit_started.wait(timeout=1.0)

                fill_thread = threading.Thread(target=process_fill, daemon=True)
                fill_thread.start()
                try:
                    assert not fill_resolution_attempted.wait(timeout=0.25)
                finally:
                    allow_broker_return.set()
                    submit_thread.join(timeout=2.0)
                    fill_thread.join(timeout=2.0)

            active = get_execution_store().load_active_position("NVDA")

        assert not submit_thread.is_alive()
        assert not fill_thread.is_alive()
        assert fill_errors == []
        assert submission_errors == []
        assert len(submission_outcomes) == 1
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(20.0)

    def test_entry_transport_unknown_keeps_submission_intent_pending(self, tmp_path) -> None:
        db_path = tmp_path / "entry-unknown.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            uncertain = OrderResult(
                False,
                "",
                "NVDA",
                "buy",
                20.0,
                error="connection reset after request",
                outcome_uncertain=True,
            )
            with patch("core.order_manager.submit_bracket_buy", return_value=uncertain):
                outcome = OrderManager(paper=True).submit_entry(
                    _plan(),
                    signal_payload={"symbol": "NVDA"},
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            snapshots = get_execution_store().list_recent_workflows()
            assert len(snapshots) == 1
            payload = get_execution_store().load_workflow(snapshots[0]["workflow_id"])

        assert outcome.success is False
        assert len(pending) == 1
        assert pending[0]["event"] == "entry_submission_intent"
        assert payload is not None
        assert "order_submit_failed" not in {
            transition["event"] for transition in payload["transitions"]
        }

    def test_submit_entry_refuses_new_order_while_symbol_intent_is_unresolved(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "entry-pending-block.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            existing = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            existing.mark_order_submission_intent(
                client_order_id=existing.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )

            with (
                patch("core.order_manager.create_entry_workflow") as create_again,
                patch("core.order_manager.submit_bracket_buy") as submit_again,
            ):
                with pytest.raises(RuntimeError, match="unresolved submission intent"):
                    OrderManager(paper=True).submit_entry(_plan())

        create_again.assert_not_called()
        submit_again.assert_not_called()

    def test_entry_acceptance_stays_pending_until_final_fill(self, tmp_path) -> None:
        db_path = tmp_path / "entry-accepted-pending.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            accepted = OrderResult(
                True,
                "entry-accepted",
                "NVDA",
                "buy",
                20.0,
            )
            manager = OrderManager(paper=True)
            with (
                patch("core.order_manager.submit_bracket_buy", return_value=accepted),
                patch("core.order_manager.notify_entry_submitted", return_value=False),
            ):
                outcome = manager.submit_entry(_plan())

            pending_before_fill = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            protected = ProtectiveStopResult(
                success=True,
                order_id="stop-after-fill",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{outcome.workflow_id}-sl-fill",
            )
            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protected,
                ),
                patch("core.order_manager.notify_buy_filled", return_value=False),
            ):
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-accepted",
                    client_order_id=outcome.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            pending_after_fill = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert len(pending_before_fill) == 1
        assert pending_before_fill[0]["event"] == "entry_submission_intent"
        assert pending_after_fill == []

    def test_fill_claims_entry_accepted_before_submission_reference_persisted(
        self,
        tmp_path,
    ) -> None:
        """The canonical client id closes the broker-accept/process-crash gap."""
        db_path = tmp_path / "accepted-before-persist.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(
                _plan(),
                signal_payload={"symbol": "NVDA"},
            )
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-after-recovery",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-recovery",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=False),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-accepted-before-crash",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        ensure.assert_called_once()
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        assert any(
            ref["broker_order_id"] == "entry-accepted-before-crash"
            and ref["client_order_id"] == workflow.workflow_id
            for ref in snapshot["order_refs"]
        )

    def test_entry_notification_transition_waits_for_fill_saga_lock(self) -> None:
        import core.order_manager as order_manager_module

        notification_started = threading.Event()
        allow_notification_return = threading.Event()
        fill_lock_held = threading.Event()
        release_fill_lock = threading.Event()
        marker_called = threading.Event()
        submission_errors: list[BaseException] = []
        workflow = SimpleNamespace(
            workflow_id="wf-entry-notification-race",
            mark_order_submission_intent=MagicMock(),
            mark_order_submitted=MagicMock(),
            mark_order_submit_failed=MagicMock(),
            mark_submission_intent_resolved=MagicMock(),
            mark_entry_notification=MagicMock(
                side_effect=lambda **_kwargs: marker_called.set()
            ),
        )

        def notify(**_kwargs) -> bool:
            notification_started.set()
            assert allow_notification_return.wait(timeout=2.0)
            return True

        def submit() -> None:
            try:
                OrderManager(paper=True).submit_entry(_plan())
            except BaseException as exc:  # noqa: BLE001 - thread handoff for assertion
                submission_errors.append(exc)

        def hold_fill_lock() -> None:
            with order_manager_module._FILL_HANDLING_LOCK:  # noqa: SLF001
                fill_lock_held.set()
                assert release_fill_lock.wait(timeout=2.0)

        with (
            patch("core.order_manager.create_entry_workflow", return_value=workflow),
            patch(
                "core.order_manager.submit_bracket_buy",
                return_value=OrderResult(
                    True,
                    "entry-race",
                    "NVDA",
                    "buy",
                    20.0,
                    client_order_id=workflow.workflow_id,
                ),
            ),
            patch("core.order_manager.notify_entry_submitted", side_effect=notify),
        ):
            submit_thread = threading.Thread(target=submit, daemon=True)
            submit_thread.start()
            assert notification_started.wait(timeout=1.0)

            holder_thread = threading.Thread(target=hold_fill_lock, daemon=True)
            holder_thread.start()
            assert fill_lock_held.wait(timeout=1.0)
            allow_notification_return.set()
            try:
                assert not marker_called.wait(timeout=0.2)
            finally:
                release_fill_lock.set()
                submit_thread.join(timeout=2.0)
                holder_thread.join(timeout=2.0)

        assert submission_errors == []
        assert marker_called.is_set()


class TestHandleFill:
    def test_cross_process_stale_buy_reloads_and_preserves_higher_checkpoint(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "cross-process-buy.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            external_process_workflow = copy.deepcopy(workflow)
            real_mark_buy_fill = workflow.mark_buy_fill
            injected = False

            def race_with_higher_checkpoint(**kwargs) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    external_process_workflow.mark_buy_fill(
                        qty=20.0,
                        fill_price=500.0,
                        broker_order_id="entry-1",
                        restore_active=False,
                    )
                real_mark_buy_fill(**kwargs)

            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-20",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-race",
            )
            with (
                patch.object(
                    workflow,
                    "mark_buy_fill",
                    side_effect=race_with_higher_checkpoint,
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=False),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=10.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert active is not None and active["qty"] == pytest.approx(20.0)
        assert ensure.call_args.kwargs["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        assert [
            item["details"]["qty"]
            for item in snapshot["transitions"]
            if item["event"] == "buy_fill_received"
        ] == [20.0]

    def test_handle_buy_fill_reconciles_stop_and_notifies(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="broker-1")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as mock_stop,
                patch(
                    "core.order_manager.notify_buy_filled",
                    return_value=True,
                ) as mock_notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="broker-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")
            reset_workflow_state()

        mock_stop.assert_called_once_with(
            symbol="NVDA",
            qty=20.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="broker-1",
            entry_order_ids={"broker-1"},
            durable_sell_fill_qty=0,
        )
        mock_notify.assert_called_once()
        assert payload is not None
        events = [item["event"] for item in payload["transitions"]]
        assert events.count("buy_fill_received") == 1
        assert events.count("protective_stop_reconciled") == 1
        assert events.count("buy_fill_notification_claimed") == 1
        assert events.count("buy_fill_notified") == 1
        assert active is not None and active["qty"] == 20.0

    def test_handle_sell_fill_marks_workflow_and_notifies(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="broker-1",
            )
            sell_client_order_id = f"{workflow.workflow_id}-sl-retry1"
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="broker-2",
                stop_price=465.0,
                action="submitted",
                stop_client_order_id=sell_client_order_id,
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch(
                    "core.order_manager.notify_sell_filled",
                    return_value=True,
                ) as mock_notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="broker-2",
                    client_order_id=sell_client_order_id,
                    side="sell",
                    filled_qty=20.0,
                    fill_price=465.0,
                    order_type="stop",
                )

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")
            reset_workflow_state()

        mock_notify.assert_called_once()
        assert payload is not None
        sell_transitions = [
            item for item in payload["transitions"] if item["event"] == "sell_fill_received"
        ]
        assert len(sell_transitions) == 1
        assert sell_transitions[0]["details"] == {
            "broker_order_id": "broker-2",
            "client_order_id": sell_client_order_id,
            "exit_reason": "stop-loss triggered",
            "fill_price": 465.0,
            "qty": 20.0,
        }
        sell_refs = [
            item for item in payload["order_refs"] if item["order_role"] == "sell_fill"
        ]
        assert len(sell_refs) == 1
        assert sell_refs[0]["client_order_id"] == sell_client_order_id
        assert active is None

    def test_final_sell_checkpoint_precedes_owner_clear_and_repairs_after_crash(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "sell-checkpoint-before-clear.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-before-crash",
            )
            sell_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_order_submitted(
                exit_reason="verification cleanup",
                broker_order_id="exit-before-crash",
            )
            store = get_execution_store()

            def crash_after_checkpoint(**kwargs):
                snapshot = store.load_workflow(workflow.workflow_id)
                assert snapshot is not None
                assert any(
                    transition["event"] == "sell_fill_received"
                    for transition in snapshot["transitions"]
                )
                assert any(
                    ref["broker_order_id"] == "exit-before-crash"
                    and ref["client_order_id"] == sell_client_order_id
                    for ref in snapshot["order_refs"]
                )
                assert set(kwargs) == {
                    "symbol",
                    "workflow_id",
                    "qty",
                    "entry_price",
                    "opened_at_utc",
                    "updated_at_utc",
                }
                raise RuntimeError("simulated crash before owner clear")

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch.object(
                    store,
                    "clear_active_position_if_unchanged",
                    side_effect=crash_after_checkpoint,
                ),
                patch("core.order_manager.notify_sell_filled") as notify,
            ):
                with pytest.raises(RuntimeError, match="simulated crash"):
                    OrderManager(paper=True).handle_fill(
                        symbol="NVDA",
                        broker_order_id="exit-before-crash",
                        client_order_id=sell_client_order_id,
                        side="sell",
                        filled_qty=20.0,
                        fill_price=490.0,
                        order_type="market",
                    )

            after_crash = store.load_active_position("NVDA")
            assert after_crash is not None
            notify.assert_not_called()

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="exit-before-crash",
                    client_order_id=sell_client_order_id,
                    side="sell",
                    filled_qty=20.0,
                    fill_price=490.0,
                    order_type="market",
                )

            snapshot = store.load_workflow(workflow.workflow_id)
            active = store.load_active_position("NVDA")

        assert active is None
        assert snapshot is not None
        assert [
            transition["event"] for transition in snapshot["transitions"]
        ].count("sell_fill_received") == 1
        assert len(
            [
                ref
                for ref in snapshot["order_refs"]
                if ref["broker_order_id"] == "exit-before-crash"
                and ref["order_role"] == "sell_fill"
            ]
        ) == 1

    def test_unresolved_orphan_sell_fill_fails_closed_without_notification(self, tmp_path) -> None:
        """An unlinked sell cannot clear ownership or emit an attributed notification."""
        db_path = tmp_path / "execution.sqlite3"
        now = "2026-08-16T20:00:00+00:00"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            store = get_execution_store()
            store.upsert_active_position(
                symbol="AAPL",
                workflow_id="orphaned-workflow",
                qty=10.0,
                entry_price=100.0,
                opened_at_utc=now,
                updated_at_utc=now,
            )

            with patch(
                "core.order_manager.notify_sell_filled",
                return_value=True,
            ) as notify:
                with pytest.raises(RuntimeError, match="Unable to resolve.*sell"):
                    OrderManager(paper=True).handle_fill(
                        symbol="AAPL",
                        broker_order_id="sell-1",
                        client_order_id="",
                        side="sell",
                        filled_qty=10.0,
                        fill_price=110.0,
                        order_type="market",
                    )

            active = store.load_active_position("AAPL")
            reset_workflow_state()

        assert active is not None
        assert active["workflow_id"] == "orphaned-workflow"
        assert active["qty"] == 10.0
        assert active["entry_price"] == 100.0
        notify.assert_not_called()

    def test_workflow_cost_basis_precedes_active_position_fallback(self) -> None:
        """A fully resolved workflow is more specific than symbol-level recovery state."""
        workflow = SimpleNamespace(entry_plan=SimpleNamespace(entry_price=95.0))
        store = SimpleNamespace(load_active_position=lambda _symbol: {"entry_price": 100.0})

        with patch("core.order_manager.get_execution_store", return_value=store):
            result = OrderManager._resolve_entry_price("AAPL", workflow)

        assert result == 95.0

    def test_missing_cost_basis_returns_none(self) -> None:
        """Missing workflow and active ownership must remain explicitly unknown."""
        store = SimpleNamespace(load_active_position=lambda _symbol: None)

        with patch("core.order_manager.get_execution_store", return_value=store):
            result = OrderManager._resolve_entry_price("AAPL", None)

        assert result is None

    def test_duplicate_final_buy_fill_is_idempotent(self, tmp_path) -> None:
        """A replayed broker fill must not duplicate transitions, stops, or notifications."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            protection = SimpleNamespace(
                success=True,
                order_id="stop-1",
                stop_price=465.0,
                action="submitted",
                error="",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=protection) as ensure_stop,
                patch("core.order_manager.notify_buy_filled", return_value=True) as notify,
            ):
                manager = OrderManager(paper=True)
                for _ in range(2):
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-1",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

            events = [transition.event for transition in workflow.transitions]
            assert events.count("buy_fill_received") == 1
            assert events.count("protective_stop_reconciled") == 1
            assert events.count("buy_fill_notified") == 1
            assert ensure_stop.call_count == 2
            assert notify.call_count == 1
            reset_workflow_state()

    def test_buy_notification_is_claimed_before_external_send(self, tmp_path) -> None:
        """A crash after email acceptance cannot make replay send it twice."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ),
                patch(
                    "core.order_manager.notify_buy_filled",
                    side_effect=[RuntimeError("crash after send"), True],
                ) as notify,
            ):
                manager = OrderManager(paper=True)
                with pytest.raises(RuntimeError, match="crash after send"):
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-1",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            events = [item.event for item in workflow.transitions]

        assert events.count("buy_fill_notification_claimed") == 1
        assert notify.call_count == 1

    def test_replayed_final_buy_fill_rechecks_live_stop(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-old",
                stop_price=460.0,
                action="submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl",
            )
            workflow.mark_buy_fill_notification(sent=True)
            replacement = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=20.0,
                stop_price=460.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-retry1",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=replacement,
                ) as ensure,
                patch(
                    "core.order_manager.notify_buy_filled",
                    return_value=True,
                ) as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            protection_events = [
                item
                for item in workflow.transitions
                if item.event == "protective_stop_reconciled"
            ]

        ensure.assert_called_once_with(
            symbol="NVDA",
            qty=20.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="entry-1",
            entry_order_ids={"entry-1"},
            durable_sell_fill_qty=0,
        )
        notify.assert_not_called()
        assert len(protection_events) == 2
        assert protection_events[-1].details["stop_order_id"] == "stop-new"

    def test_replayed_partial_buy_fill_rechecks_live_stop(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=5.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-old",
                stop_price=460.0,
                action="partial_submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl",
            )
            replacement = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=5.0,
                stop_price=460.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-retry1",
            )

            with patch(
                "core.order_manager.ensure_protective_stop",
                return_value=replacement,
            ) as ensure:
                OrderManager(paper=True).handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            events = [item.event for item in workflow.transitions]
            protection_events = [
                item
                for item in workflow.transitions
                if item.event == "protective_stop_reconciled"
            ]

        ensure.assert_called_once_with(
            symbol="NVDA",
            qty=5.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="entry-1",
            entry_order_ids={"entry-1"},
            durable_sell_fill_qty=0,
        )
        assert events.count("buy_fill_received") == 1
        assert len(protection_events) == 2
        assert protection_events[-1].details["action"] == "partial_submitted"

    def test_partial_then_final_buy_fill_reconciles_final_quantity(self, tmp_path) -> None:
        """A larger cumulative final fill is not mistaken for a partial-fill replay."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            protection = SimpleNamespace(
                success=True,
                order_id="stop-1",
                stop_price=465.0,
                action="submitted",
                error="",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=protection) as ensure_stop,
                patch("core.order_manager.notify_buy_filled", return_value=True) as notify,
            ):
                manager = OrderManager(paper=True)
                manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            fill_quantities = [
                transition.details["qty"]
                for transition in workflow.transitions
                if transition.event == "buy_fill_received"
            ]
            assert fill_quantities == [5.0, 20.0]
            assert [call.kwargs["qty"] for call in ensure_stop.call_args_list] == [5.0, 20.0]
            assert notify.call_count == 1
            reset_workflow_state()

    def test_final_then_late_partial_keeps_full_durable_quantity_and_rechecks(self, tmp_path) -> None:
        """A stale cumulative partial cannot downsize ownership or duplicate notification."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=True) as notify,
            ):
                manager = OrderManager(paper=True)
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )
                manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")
            fill_quantities = [
                transition.details["qty"]
                for transition in workflow.transitions
                if transition.event == "buy_fill_received"
            ]

        assert active is not None and active["qty"] == 20.0
        assert fill_quantities == [20.0]
        assert [item.kwargs["qty"] for item in ensure.call_args_list] == [20.0, 20.0]
        assert [item.kwargs["entry_order_id"] for item in ensure.call_args_list] == [
            "entry-1",
            "entry-1",
        ]
        assert notify.call_count == 1

    def test_partial_final_then_partial_replay_rechecks_full_live_protection(self, tmp_path) -> None:
        """An old partial replay after a final fill must protect current exposure."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=True) as notify,
            ):
                manager = OrderManager(paper=True)
                manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )
                manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["qty"] == 20.0
        assert [item.kwargs["qty"] for item in ensure.call_args_list] == [5.0, 20.0, 20.0]
        assert [item.kwargs["entry_order_id"] for item in ensure.call_args_list] == [
            "entry-1",
            "entry-1",
            "entry-1",
        ]
        assert notify.call_count == 1

    def test_stale_buy_replay_reconciles_current_workflow_owner(self, tmp_path) -> None:
        """An old workflow event cannot replace or cancel a newer workflow's stop."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            old_workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-old",
            )
            old_workflow.mark_buy_fill_notification(sent=True)
            new_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=7.0,
                stop_price=474.3,
                action="reused",
                client_order_id=f"{new_workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-old",
                    client_order_id=old_workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == 7.0
        assert ensure.call_args.kwargs["entry_order_id"] == "entry-new"
        notify.assert_not_called()

    def test_first_seen_old_buy_fill_cannot_replace_newer_owner(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            old_workflow.mark_order_submitted(broker_order_id="entry-old")
            new_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=7.0,
                stop_price=474.3,
                action="reused",
                client_order_id=f"{new_workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-old",
                    client_order_id=old_workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == 7.0
        notify.assert_not_called()

    def test_late_higher_buy_after_sell_retains_causal_exposure_when_rest_is_flat(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            workflow.mark_buy_fill(
                qty=5.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_sell_fill(
                qty=5.0,
                fill_price=465.0,
                exit_reason="closed",
                broker_order_id="exit-1",
                client_order_id=f"{workflow.workflow_id}-exit",
            )

            unproven = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=15.0,
                stop_price=465.0,
                action="position_not_visible",
                error="late causal fill is not visible in broker positions yet",
            )
            manager = OrderManager(paper=True)
            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=unproven,
                ) as ensure,
                patch(
                    "core.order_manager.notify_buy_filled",
                    return_value=True,
                ) as notify,
            ):
                with pytest.raises(RuntimeError, match="Safety remains unproven"):
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-1",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(15.0)
        assert [
            item.details["qty"]
            for item in workflow.transitions
            if item.event == "buy_fill_received"
        ] == [5.0, 20.0]
        sample.assert_called_once_with("NVDA")
        assert ensure.call_args.kwargs["qty"] == pytest.approx(15.0)
        assert ensure.call_args.kwargs["durable_sell_fill_qty"] == pytest.approx(5.0)
        notify.assert_not_called()

    def test_late_partial_buy_fences_hidden_source_replacement_once_across_replay(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "late-buy-foreign-owner.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(
                _plan(),
                signal_payload={"generation": 1},
            )
            old_workflow.mark_order_submitted(broker_order_id="entry-old")
            old_workflow.mark_buy_fill(
                qty=5.0,
                fill_price=500.0,
                broker_order_id="entry-old",
            )
            old_workflow.mark_sell_fill(
                qty=5.0,
                fill_price=465.0,
                exit_reason="closed",
                broker_order_id="exit-old",
                client_order_id=f"{old_workflow.workflow_id}-exit",
            )
            new_workflow = create_entry_workflow(
                _plan(),
                signal_payload={"generation": 2},
            )
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )
            parent = SimpleNamespace(
                id="entry-old",
                symbol="NVDA",
                side="buy",
                type="limit",
                status="replaced",
                qty="30",
                filled_qty="20",
                filled_avg_price="500",
                client_order_id=old_workflow.workflow_id,
                replaces=None,
                replaced_by="entry-old-r1",
            )
            child = SimpleNamespace(
                id="entry-old-r1",
                symbol="NVDA",
                side="buy",
                type="limit",
                status="partially_filled",
                qty="10",
                filled_qty="10",
                filled_avg_price="502",
                client_order_id=f"{old_workflow.workflow_id}-r1",
                replaces="entry-old",
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_id.side_effect = {
                "entry-old": parent,
                "entry-old-r1": child,
            }.__getitem__

            def cancel_child(order_id: str) -> None:
                assert order_id == "entry-old-r1"
                child.status = "canceled"

            client.cancel_order_by_id.side_effect = cancel_child
            broker_position = PositionSummary(
                symbol="NVDA",
                qty=22.0,
                avg_entry_price=510.0,
                current_price=510.0,
                unrealized_pl_pct=0.0,
            )
            protected = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=32.0,
                stop_price=474.3,
                action="submitted",
                client_order_id=f"{new_workflow.workflow_id}-sl-late",
            )
            manager = OrderManager(paper=True)

            with (
                patch("core.order_execution._get_trading_client", return_value=client),
                patch("core.order_execution.time.sleep"),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(broker_position, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protected,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                for _ in range(2):
                    manager.handle_partial_fill(
                        symbol="NVDA",
                        broker_order_id="entry-old",
                        client_order_id=old_workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

            active = get_execution_store().load_active_position("NVDA")
            old_snapshot = get_execution_store().load_workflow(
                old_workflow.workflow_id
            )
            new_snapshot = get_execution_store().load_workflow(
                new_workflow.workflow_id
            )

        assert active is not None
        assert active["workflow_id"] == new_workflow.workflow_id
        assert active["qty"] == pytest.approx(32.0)
        assert [item.kwargs["qty"] for item in ensure.call_args_list] == [32.0, 32.0]
        assert all(
            item.kwargs["workflow_id"] == new_workflow.workflow_id
            for item in ensure.call_args_list
        )
        assert all(
            item.kwargs["entry_order_ids"] == {"entry-new"}
            for item in ensure.call_args_list
        )
        assert old_snapshot is not None
        assert new_snapshot is not None
        assert {
            reference["broker_order_id"]
            for reference in old_snapshot["order_refs"]
            if reference["order_role"] in {"entry_order", "buy_fill"}
        } >= {"entry-old", "entry-old-r1"}
        assert {
            reference["broker_order_id"]
            for reference in new_snapshot["order_refs"]
        }.isdisjoint({"entry-old", "entry-old-r1"})
        assert [
            transition["details"]["qty"]
            for transition in old_snapshot["transitions"]
            if transition["event"] == "buy_fill_received"
            and transition["details"].get("broker_order_id") == "entry-old-r1"
        ] == [10.0]
        assert sum(
            transition["event"] == "late_buy_exposure_recovered"
            for transition in new_snapshot["transitions"]
        ) == 1
        client.cancel_order_by_id.assert_called_once_with("entry-old-r1")
        notify.assert_not_called()

    def test_late_higher_buy_after_sell_reprotects_reappeared_position(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            workflow.mark_buy_fill(
                qty=5.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_sell_fill(
                qty=5.0,
                fill_price=465.0,
                exit_reason="closed",
                broker_order_id="exit-1",
                client_order_id=f"{workflow.workflow_id}-exit",
            )
            broker_position = PositionSummary(
                symbol="NVDA",
                qty=15.0,
                avg_entry_price=501.0,
                current_price=501.0,
                unrealized_pl_pct=0.0,
            )
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-recovered",
                symbol="NVDA",
                qty=15.0,
                stop_price=465.93,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-recovered",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(broker_position, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert sample.call_count >= 1
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(15.0)
        assert active["entry_price"] == pytest.approx(501.0)
        assert ensure.call_args.kwargs["workflow_id"] == workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == pytest.approx(15.0)
        assert ensure.call_args.kwargs["fill_price"] == pytest.approx(501.0)
        notify.assert_not_called()

    def test_late_replacement_buy_after_sell_fences_parent_and_child(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "late-buy-replacement.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-parent")
            workflow.mark_buy_fill(
                qty=4.0,
                fill_price=500.0,
                broker_order_id="entry-parent",
            )
            workflow.mark_sell_fill(
                qty=4.0,
                fill_price=465.0,
                exit_reason="closed",
                broker_order_id="exit-1",
                client_order_id=f"{workflow.workflow_id}-exit",
            )
            protected = ProtectiveStopResult(
                success=True,
                order_id="stop-late-child",
                symbol="NVDA",
                qty=6.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-late-child",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protected,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-child",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=6.0,
                    fill_price=500.0,
                    order_type="limit",
                    replaces="entry-parent",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["qty"] == pytest.approx(6.0)
        assert ensure.call_args.kwargs["entry_order_ids"] == {
            "entry-parent",
            "entry-child",
        }
        assert ensure.call_args.kwargs["durable_sell_fill_qty"] == pytest.approx(4.0)
        notify.assert_not_called()

    def test_replacement_chain_fills_aggregate_durable_quantity(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-parent")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=10.0,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=True),
            ):
                manager = OrderManager(paper=True)
                manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="entry-parent",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=4.0,
                    fill_price=500.0,
                    order_type="limit",
                )
                manager.handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="entry-parent",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    order_type="limit",
                    status="replaced",
                    replaced_by="entry-child",
                )
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-child",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=6.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["qty"] == 10.0
        assert ensure.call_args_list[-1].kwargs["qty"] == 10.0
        assert ensure.call_args_list[-1].kwargs["entry_order_ids"] == {
            "entry-parent",
            "entry-child",
        }

    def test_stale_sell_fill_cannot_clear_newer_workflow_ownership(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            old_workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-old",
            )
            new_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )
            old_workflow.mark_exit_order_submitted(
                exit_reason="old exit",
                broker_order_id="sell-old",
            )
            current_position = PositionSummary("NVDA", 7.0, 510.0, 500.0, -0.02)
            current_stop = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=7.0,
                stop_price=474.3,
                action="reused",
                client_order_id=f"{new_workflow.workflow_id}-sl",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(current_position, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=current_stop,
                ) as ensure,
                patch("core.order_manager.notify_sell_filled", return_value=True),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="sell-old",
                    client_order_id=f"{old_workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=20.0,
                    fill_price=465.0,
                    order_type="market",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == 7.0

    def test_stale_sell_flat_proof_clears_unchanged_foreign_owner(self, tmp_path) -> None:
        """Stable broker flatness invalidates the exact durable row observed before it."""
        db_path = tmp_path / "stale-sell-flat.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            old_workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-old",
            )
            old_workflow.mark_exit_order_submitted(
                exit_reason="old exit",
                broker_order_id="sell-old",
            )
            new_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="sell-old",
                    client_order_id=f"{old_workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=20.0,
                    fill_price=490.0,
                    order_type="market",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is None

    def test_undersized_terminal_sell_retains_and_protects_residual_position(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-partial",
                stop_price=465.0,
                action="submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl",
            )
            residual_position = PositionSummary("NVDA", 0.5, 500.0, 470.0, -0.06)
            residual_stop = ProtectiveStopResult(
                success=True,
                order_id="stop-residual",
                symbol="NVDA",
                qty=0.5,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-residual",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual_position, []),
                    create=True,
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=residual_stop,
                ) as ensure,
                patch(
                    "core.order_manager.notify_sell_filled",
                    return_value=True,
                ),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="stop-partial",
                    client_order_id=f"{workflow.workflow_id}-sl",
                    side="sell",
                    filled_qty=0.5,
                    fill_price=465.0,
                    order_type="stop",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(0.5)
        assert ensure.call_args.kwargs["workflow_id"] == workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == pytest.approx(0.5)

    @pytest.mark.parametrize("extra_side", ["buy", "sell"])
    def test_terminal_sell_flat_cancels_all_remaining_symbol_orders(
        self,
        tmp_path,
        extra_side,
    ) -> None:
        db_path = tmp_path / f"terminal-{extra_side}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-filled",
            )
            extra_order = SimpleNamespace(id=f"extra-{extra_side}", side=extra_side)

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    side_effect=[(None, [extra_order]), (None, [])],
                ) as sample,
                patch(
                    "core.order_manager.cancel_open_orders_verified",
                ) as cancel,
                patch("core.order_manager.notify_sell_filled", return_value=True),
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="exit-filled",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=1.0,
                    fill_price=490.0,
                    order_type="market",
                )

            active = get_execution_store().load_active_position("NVDA")

        cancel.assert_called_once_with("NVDA")
        assert sample.call_count == 2
        assert active is None

    def test_partial_sell_immediately_reprotects_remaining_position(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-partial",
            )
            residual_position = PositionSummary("NVDA", 0.6, 500.0, 480.0, -0.04)
            residual_stop = ProtectiveStopResult(
                success=True,
                order_id="stop-residual",
                symbol="NVDA",
                qty=0.6,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-residual",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual_position, []),
                    create=True,
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=residual_stop,
                ) as ensure,
                patch("core.order_manager.notify_sell_filled") as notify,
            ):
                OrderManager(paper=True).handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="exit-partial",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=0.4,
                    fill_price=480.0,
                    order_type="market",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["qty"] == pytest.approx(0.6)
        assert ensure.call_args.kwargs["qty"] == pytest.approx(0.6)
        notify.assert_not_called()

    def test_partial_sell_fences_entry_chain_against_net_residual_exposure(
        self,
        tmp_path,
    ) -> None:
        """Fence entry replacements while protecting gross buys minus durable sells."""
        db_path = tmp_path / "partial-sell-net-fence.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-partial",
                stop_price=465.0,
                action="submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl-partial",
            )
            residual = PositionSummary("NVDA", 0.5, 500.0, 480.0, -0.04)
            protected = ProtectiveStopResult(
                success=True,
                order_id="stop-residual",
                symbol="NVDA",
                qty=0.5,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl-residual",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual, []),
                ),
                patch("core.order_execution.get_open_orders", return_value=[]),
                patch(
                    "core.order_execution._wait_for_terminal_buy_order_chain",
                    return_value=1.0,
                ) as terminal_fence,
                patch(
                    "core.order_execution.reconcile_symbol_after_exit_failure",
                    return_value=protected,
                ) as reconcile,
            ):
                OrderManager(paper=True).handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="stop-partial",
                    client_order_id=f"{workflow.workflow_id}-sl-partial",
                    side="sell",
                    filled_qty=0.5,
                    fill_price=465.0,
                    order_type="stop",
                )

            active = get_execution_store().load_active_position("NVDA")

        terminal_fence.assert_called_once_with(
            "NVDA",
            {"entry-1"},
            workflow_id=workflow.workflow_id,
        )
        reconcile.assert_called_once_with(
            "NVDA",
            workflow_id=workflow.workflow_id,
            stop_loss_pct=0.08,
            minimum_position_qty=0.5,
        )
        assert active is not None and active["qty"] == pytest.approx(0.5)

    def test_partial_sell_transient_flat_rest_fails_closed_without_cancel(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "partial-sell-transient-flat.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="stop-existing",
                stop_price=465.0,
                action="submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-partial",
            )
            protective_order = SimpleNamespace(id="stop-existing", side="sell")

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    side_effect=[(None, [protective_order]), (None, [])],
                ) as sample,
                patch(
                    "core.order_manager.cancel_open_orders_verified",
                ) as cancel,
                patch(
                    "core.order_manager.time.monotonic",
                    side_effect=[0.0, 5.0],
                ),
                patch("core.order_manager.ensure_protective_stop") as ensure,
            ):
                with pytest.raises(
                    RuntimeError,
                    match="did not reflect terminal sell quantity",
                ):
                    OrderManager(paper=True).handle_partial_fill(
                        symbol="NVDA",
                        broker_order_id="exit-partial",
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        filled_qty=0.4,
                        fill_price=480.0,
                        order_type="market",
                    )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(1.0)
        assert sample.call_count == 1
        cancel.assert_not_called()
        ensure.assert_not_called()
        assert "sell_partial_fill_received" not in {
            transition.event for transition in workflow.transitions
        }

    def test_unknown_fill_side_cannot_clear_active_ownership(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )

            with patch("core.order_manager.notify_sell_filled") as notify:
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="unknown-1",
                    client_order_id=workflow.workflow_id,
                    side="unknown",
                    filled_qty=1.0,
                    fill_price=500.0,
                    order_type="market",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None and active["workflow_id"] == workflow.workflow_id
        notify.assert_not_called()

    def test_buy_fill_with_unknown_broker_reference_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-known")
            stop = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="submitted",
            )

            with patch(
                "core.order_manager.ensure_protective_stop",
                return_value=stop,
            ) as ensure:
                with pytest.raises(RuntimeError, match="Unable to resolve.*buy"):
                    OrderManager(paper=True).handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-unknown",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=1.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

            active = get_execution_store().load_active_position("NVDA")

        ensure.assert_not_called()
        assert active is None

    def test_protection_failure_immediately_submits_safety_exit(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            failed = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submit_failed",
                error="broker rejected stop",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=failed),
                patch.object(
                    OrderManager,
                    "_submit_exit_locked",
                    return_value=OrderResult(True, "exit-1", "NVDA", "sell", 20.0),
                ) as safety_exit,
                patch("core.order_manager.notify_buy_filled") as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

        safety_exit.assert_called_once_with(
            "NVDA",
            exit_reason="protective stop reconciliation failed",
        )
        notify.assert_not_called()

    def test_unknown_stop_submission_does_not_submit_a_competing_exit(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "unknown-stop-no-competing-exit.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            unknown = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submission_unknown",
                error="STOP response lost after broker mutation",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=unknown),
                patch.object(
                    OrderManager,
                    "_submit_exit_locked",
                    return_value=OrderResult(True, "exit-1", "NVDA", "sell", 20.0),
                ) as safety_exit,
            ):
                with pytest.raises(RuntimeError, match="STOP response lost"):
                    OrderManager(paper=True).handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-1",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

            active = get_execution_store().load_active_position("NVDA")

        safety_exit.assert_not_called()
        assert active is not None and active["workflow_id"] == workflow.workflow_id

    def test_failed_safety_exit_must_surface_unproven_exposure(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-1")
            failed_stop = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submit_failed",
                error="stop rejected",
            )
            failed_safety = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submit_failed",
                error="still naked",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=failed_stop),
                patch.object(
                    OrderManager,
                    "_submit_exit_locked",
                    return_value=OrderResult(
                        False,
                        "",
                        "NVDA",
                        "sell",
                        20.0,
                        error="exit rejected",
                    ),
                ),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    return_value=failed_safety,
                ),
            ):
                with pytest.raises(RuntimeError, match="still naked"):
                    OrderManager(paper=True).handle_fill(
                        symbol="NVDA",
                        broker_order_id="entry-1",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        filled_qty=20.0,
                        fill_price=500.0,
                        order_type="limit",
                    )

    def test_duplicate_sell_fill_is_idempotent(self, tmp_path) -> None:
        """A replayed terminal sell cannot duplicate P&L notification or audit transitions."""
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(qty=20.0, fill_price=500.0, broker_order_id="entry-1")
            workflow.mark_protective_stop(
                success=True,
                stop_order_id="sell-1",
                stop_price=465.0,
                action="submitted",
                stop_client_order_id=f"{workflow.workflow_id}-sl-retry1",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=True) as notify,
            ):
                manager = OrderManager(paper=True)
                for _ in range(2):
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="sell-1",
                        client_order_id=f"{workflow.workflow_id}-sl-retry1",
                        side="sell",
                        filled_qty=20.0,
                        fill_price=465.0,
                        order_type="stop",
                    )

            events = [transition.event for transition in workflow.transitions]
            assert events.count("sell_fill_received") == 1
            assert events.count("sell_fill_notified") == 1
            assert notify.call_count == 1
            assert get_execution_store().load_active_position("NVDA") is None
            payload = get_execution_store().load_workflow(workflow.workflow_id)
            assert payload is not None
            sell_refs = [
                ref for ref in payload["order_refs"] if ref["order_role"] == "sell_fill"
            ]
            assert len(sell_refs) == 1
            assert sell_refs[0]["client_order_id"] == (
                f"{workflow.workflow_id}-sl-retry1"
            )
            reset_workflow_state()

    def test_sell_notification_is_claimed_before_external_send(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(qty=20.0, fill_price=500.0, broker_order_id="entry-1")
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="sell-1",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch(
                    "core.order_manager.notify_sell_filled",
                    side_effect=[RuntimeError("crash after send"), True],
                ) as notify,
            ):
                manager = OrderManager(paper=True)
                with pytest.raises(RuntimeError, match="crash after send"):
                    manager.handle_fill(
                        symbol="NVDA",
                        broker_order_id="sell-1",
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        filled_qty=20.0,
                        fill_price=465.0,
                        order_type="market",
                    )
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="sell-1",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=20.0,
                    fill_price=465.0,
                    order_type="market",
                )

            events = [item.event for item in workflow.transitions]

        assert events.count("sell_fill_notification_claimed") == 1
        assert notify.call_count == 1

    def test_recorded_buy_fill_repairs_torn_storage_and_finishes_saga(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.transition(
                WorkflowState.BUY_FILL_RECEIVED,
                event="buy_fill_received",
                details={
                    "qty": 20.0,
                    "fill_price": 500.0,
                    "broker_order_id": "entry-1",
                },
            )
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=20.0,
                stop_price=460.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl",
            )

            with (
                patch("core.order_manager.ensure_protective_stop", return_value=protection) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=False) as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="entry-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert payload is not None
        assert [item["event"] for item in payload["transitions"]].count("buy_fill_received") == 1
        assert any(ref["order_role"] == "buy_fill" for ref in payload["order_refs"])
        assert active is not None and active["workflow_id"] == workflow.workflow_id
        assert ensure.call_count == 1
        assert notify.call_count == 1

    def test_recorded_sell_fill_repairs_reference_and_stale_active_ownership(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(qty=20.0, fill_price=500.0, broker_order_id="entry-1")
            workflow.transition(
                WorkflowState.SELL_FILL_RECEIVED,
                event="sell_fill_received",
                details={
                    "qty": 20.0,
                    "fill_price": 465.0,
                    "exit_reason": "exit order filled",
                    "broker_order_id": "sell-1",
                },
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch(
                    "core.order_manager.notify_sell_filled",
                    return_value=False,
                ) as notify,
            ):
                OrderManager(paper=True).handle_fill(
                    symbol="NVDA",
                    broker_order_id="sell-1",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=20.0,
                    fill_price=465.0,
                    order_type="market",
                )

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert payload is not None
        assert [item["event"] for item in payload["transitions"]].count("sell_fill_received") == 1
        sell_refs = [
            ref for ref in payload["order_refs"] if ref["order_role"] == "sell_fill"
        ]
        assert len(sell_refs) == 1
        assert sell_refs[0]["client_order_id"] == f"{workflow.workflow_id}-exit"
        assert active is None
        assert notify.call_count == 1

    def test_handle_partial_buy_fill_reconciles_stop_without_notification(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="broker-1")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=5.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-a1b2c3",
            )

            with (
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as mock_stop,
                patch("core.order_manager.notify_buy_filled") as mock_notify,
            ):
                OrderManager(paper=True).handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="broker-1",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    filled_qty=5.0,
                    fill_price=500.0,
                    order_type="limit",
                )

            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")
            reset_workflow_state()

        mock_stop.assert_called_once_with(
            symbol="NVDA",
            qty=5.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="broker-1",
            entry_order_ids={"broker-1"},
            durable_sell_fill_qty=0,
        )
        mock_notify.assert_not_called()
        assert payload is not None
        events = [item["event"] for item in payload["transitions"]]
        assert events.count("buy_fill_received") == 1
        assert events.count("protective_stop_reconciled") == 1
        assert "buy_fill_notification_claimed" not in events
        protection_details = next(
            item["details"]
            for item in payload["transitions"]
            if item["event"] == "protective_stop_reconciled"
        )
        assert protection_details["action"] == "partial_submitted"
        assert active is not None and active["qty"] == 5.0


class TestOrderFailureRecovery:
    def test_sell_offset_aggregates_replacements_without_replay_double_count(self) -> None:
        workflow = SimpleNamespace(
            transitions=[
                SimpleNamespace(
                    event="sell_partial_fill_received",
                    details={"broker_order_id": "sell-parent", "qty": 2.0},
                ),
                SimpleNamespace(
                    event="sell_fill_received",
                    details={"broker_order_id": "sell-parent", "qty": 4.0},
                ),
                SimpleNamespace(
                    event="sell_partial_fill_received",
                    details={"broker_order_id": "sell-parent", "qty": 4.0},
                ),
                SimpleNamespace(
                    event="sell_fill_received",
                    details={"broker_order_id": "sell-child", "qty": 2.0},
                ),
                SimpleNamespace(
                    event="sell_fill_received",
                    details={"broker_order_id": "", "qty": 100.0},
                ),
            ]
        )

        assert OrderManager._cumulative_workflow_sell_fill_qty(workflow) == (  # noqa: SLF001
            pytest.approx(6.0)
        )

    @pytest.mark.parametrize("handler", ["fill", "partial_fill", "failure"])
    def test_stream_event_without_any_order_reference_fails_closed(
        self,
        tmp_path,
        handler,
    ) -> None:
        db_path = tmp_path / f"{handler}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-known",
            )
            manager = OrderManager(paper=True)
            broker_position = PositionSummary("NVDA", 0.6, 500.0, 480.0, -0.04)
            stop = ProtectiveStopResult(
                success=True,
                order_id="stop-1",
                symbol="NVDA",
                qty=0.6,
                stop_price=465.0,
                action="submitted",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(broker_position, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=stop,
                ) as ensure,
            ):
                with pytest.raises(RuntimeError, match="missing all order references"):
                    if handler == "fill":
                        manager.handle_fill(
                            symbol="NVDA",
                            broker_order_id="",
                            client_order_id="",
                            side="buy",
                            filled_qty=1.0,
                            fill_price=500.0,
                            order_type="limit",
                        )
                    elif handler == "partial_fill":
                        manager.handle_partial_fill(
                            symbol="NVDA",
                            broker_order_id="",
                            client_order_id="",
                            side="sell",
                            filled_qty=0.4,
                            fill_price=480.0,
                            order_type="market",
                        )
                    else:
                        manager.handle_order_failure(
                            symbol="NVDA",
                            broker_order_id="",
                            client_order_id="",
                            side="sell",
                            order_type="market",
                            status="canceled",
                        )

            active = get_execution_store().load_active_position("NVDA")

        sample.assert_not_called()
        ensure.assert_not_called()
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(1.0)

    def test_failed_sell_repairs_broker_residual_and_protects_owner(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-canceled",
            )
            residual = PositionSummary("NVDA", 0.6, 505.0, 480.0, -0.05)
            stop = ProtectiveStopResult(
                success=True,
                order_id="stop-residual",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-recovery",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=stop,
                ) as ensure,
            ):
                OrderManager(paper=True).handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="exit-canceled",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    order_type="market",
                    status="canceled",
                )

            active = get_execution_store().load_active_position("NVDA")
            payload = get_execution_store().load_workflow(workflow.workflow_id)

        sample.assert_not_called()
        ensure.assert_called_once_with(
            symbol="NVDA",
            qty=1.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="entry-1",
            entry_order_ids={"entry-1"},
            durable_sell_fill_qty=0,
        )
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(1.0)
        assert active["entry_price"] == pytest.approx(500.0)
        assert payload is not None
        assert "sell_fill_received" not in {
            item["event"] for item in payload["transitions"]
        }
        assert any(
            item["event"] == "protective_stop_reconciled"
            and item["details"]["action"] == "sell_canceled_submitted"
            for item in payload["transitions"]
        )

    def test_terminal_buy_with_missed_partial_recovers_broker_position(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-canceled")
            broker_position = PositionSummary("NVDA", 0.4, 502.0, 500.0, -0.01)
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-partial",
                symbol="NVDA",
                qty=0.4,
                stop_price=466.86,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-partial",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(broker_position, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=True) as notify,
            ):
                OrderManager(paper=True).handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="entry-canceled",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    order_type="limit",
                    status="canceled",
                    filled_qty=0.4,
                    fill_price=502.0,
                )

            active = get_execution_store().load_active_position("NVDA")

        sample.assert_not_called()
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(0.4)
        assert active["entry_price"] == pytest.approx(502.0)
        assert ensure.call_args.kwargs["qty"] == pytest.approx(0.4)
        assert ensure.call_args.kwargs["fill_price"] == pytest.approx(502.0)
        notify.assert_called_once()

    def test_terminal_buy_rest_lag_keeps_fill_owned_until_protection(
        self,
        tmp_path,
    ) -> None:
        """A factual positive fill cannot be discarded by a temporarily flat REST view."""
        db_path = tmp_path / "terminal-buy-rest-lag.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-canceled")
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-after-rest-catchup",
                symbol="NVDA",
                qty=0.4,
                stop_price=466.86,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-rest-lag",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=True),
            ):
                OrderManager(paper=True).handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="entry-canceled",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    order_type="limit",
                    status="canceled",
                    filled_qty=0.4,
                    fill_price=502.0,
                )

            active = get_execution_store().load_active_position("NVDA")

        sample.assert_not_called()
        ensure.assert_called_once()
        assert ensure.call_args.kwargs["qty"] == pytest.approx(0.4)
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(0.4)

    @pytest.mark.parametrize("action", ["position_not_visible", "position_sync_pending"])
    def test_terminal_buy_visibility_timeout_retains_owner_and_fails_closed(
        self,
        tmp_path,
        action,
    ) -> None:
        db_path = tmp_path / f"terminal-buy-{action}.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submitted(broker_order_id="entry-canceled")
            unproven = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=0.0,
                stop_price=466.86,
                action=action,
                error="terminal fill has not reached the positions endpoint",
            )
            manager = OrderManager(paper=True)

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    side_effect=AssertionError("terminal BUY must not trust an eager flat sample"),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=unproven,
                ),
                patch.object(manager, "_submit_exit_locked") as submit_exit,
            ):
                with pytest.raises(RuntimeError, match="Safety remains unproven"):
                    manager.handle_order_failure(
                        symbol="NVDA",
                        broker_order_id="entry-canceled",
                        client_order_id=workflow.workflow_id,
                        side="buy",
                        order_type="limit",
                        status="canceled",
                        filled_qty=0.4,
                        fill_price=502.0,
                    )

            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        submit_exit.assert_not_called()
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(0.4)
        assert snapshot is not None
        assert any(
            item["event"] == "protective_stop_reconciled"
            and item["details"]["success"] is False
            and item["details"]["action"] == action
            for item in snapshot["transitions"]
        )

    def test_zero_fill_terminal_sell_retains_owner_when_rest_is_transiently_flat(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-canceled",
            )
            unproven = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="position_not_visible",
                error="zero-fill sell cannot prove the durable long disappeared",
            )
            manager = OrderManager(paper=True)

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ) as sample,
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=unproven,
                ) as ensure,
                patch("core.order_manager.cancel_open_orders_verified") as cancel,
            ):
                with pytest.raises(RuntimeError, match="Safety remains unproven"):
                    manager.handle_order_failure(
                        symbol="NVDA",
                        broker_order_id="exit-canceled",
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        order_type="market",
                        status="canceled",
                    )

            active = get_execution_store().load_active_position("NVDA")

        sample.assert_not_called()
        ensure.assert_called_once()
        cancel.assert_not_called()
        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(1.0)

    def test_stale_failed_sell_preserves_new_owner_and_broker_quantity(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            old_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            old_workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-old",
            )
            old_workflow.mark_exit_order_submitted(
                exit_reason="old exit",
                broker_order_id="exit-old",
            )
            new_workflow = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            new_workflow.mark_buy_fill(
                qty=7.0,
                fill_price=510.0,
                broker_order_id="entry-new",
            )
            broker_position = PositionSummary("NVDA", 6.5, 512.0, 500.0, -0.02)
            stop = ProtectiveStopResult(
                success=True,
                order_id="stop-new",
                symbol="NVDA",
                qty=6.5,
                stop_price=476.16,
                action="reused",
                client_order_id=f"{new_workflow.workflow_id}-sl",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(broker_position, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=stop,
                ) as ensure,
            ):
                OrderManager(paper=True).handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="exit-old",
                    client_order_id=f"{old_workflow.workflow_id}-exit",
                    side="sell",
                    order_type="market",
                    status="expired",
                )

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == new_workflow.workflow_id
        assert active["qty"] == pytest.approx(6.5)
        assert active["entry_price"] == pytest.approx(512.0)
        assert ensure.call_args.kwargs["workflow_id"] == new_workflow.workflow_id
        assert ensure.call_args.kwargs["qty"] == pytest.approx(6.5)

    def test_failed_sell_raises_when_workflow_references_conflict(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow_a = create_entry_workflow(_plan(), signal_payload={"generation": 1})
            workflow_a.mark_exit_order_submitted(
                exit_reason="exit a",
                broker_order_id="exit-a",
            )
            workflow_b = create_entry_workflow(_plan(), signal_payload={"generation": 2})
            workflow_b.mark_exit_order_submitted(
                exit_reason="exit b",
                broker_order_id="exit-b",
            )

            with (
                patch("core.order_manager._sample_stable_symbol_state") as sample,
                patch("core.order_manager.ensure_protective_stop") as ensure,
            ):
                with pytest.raises(RuntimeError, match="Unable to resolve.*sell"):
                    OrderManager(paper=True).handle_order_failure(
                        symbol="NVDA",
                        broker_order_id="exit-b",
                        client_order_id=f"{workflow_a.workflow_id}-exit",
                        side="sell",
                        order_type="market",
                        status="rejected",
                    )

        sample.assert_not_called()
        ensure.assert_not_called()

    def test_failed_sell_surfaces_unproven_residual_exposure(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            workflow.mark_exit_order_submitted(
                exit_reason="test exit",
                broker_order_id="exit-rejected",
            )
            residual = PositionSummary("NVDA", 1.0, 500.0, 480.0, -0.04)
            failed_stop = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="submit_failed",
                error="stop rejected",
            )
            unsafe = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=1.0,
                stop_price=465.0,
                action="unsafe_orders",
                error="still naked",
            )

            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=failed_stop,
                ),
                patch.object(
                    OrderManager,
                    "_submit_exit_locked",
                    return_value=OrderResult(
                        False,
                        "",
                        "NVDA",
                        "sell",
                        1.0,
                        error="exit rejected",
                    ),
                ),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    return_value=unsafe,
                ),
            ):
                with pytest.raises(RuntimeError, match="stop rejected"):
                    OrderManager(paper=True).handle_order_failure(
                        symbol="NVDA",
                        broker_order_id="exit-rejected",
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        order_type="market",
                        status="rejected",
                    )

    def test_buy_order_failure_cannot_mutate_position_ownership(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )

            with (
                patch("core.order_manager._sample_stable_symbol_state") as sample,
                patch("core.order_manager.ensure_protective_stop") as ensure,
            ):
                OrderManager(paper=True).handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="entry-canceled",
                    client_order_id=workflow.workflow_id,
                    side="buy",
                    order_type="limit",
                    status="canceled",
                )

            active = get_execution_store().load_active_position("NVDA")
            payload = get_execution_store().load_workflow(workflow.workflow_id)

        sample.assert_not_called()
        ensure.assert_not_called()
        assert active is not None and active["qty"] == pytest.approx(1.0)
        assert payload is not None
        assert [
            item["event"] for item in payload["transitions"]
        ].count("buy_fill_received") == 1

    def test_partial_sell_with_unknown_references_fails_closed(self, tmp_path) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )

            with patch("core.order_manager._sample_stable_symbol_state") as sample:
                with pytest.raises(RuntimeError, match="Unable to resolve.*sell"):
                    OrderManager(paper=True).handle_partial_fill(
                        symbol="NVDA",
                        broker_order_id="unknown-sell",
                        client_order_id=f"{workflow.workflow_id}-exit",
                        side="sell",
                        filled_qty=0.4,
                        fill_price=480.0,
                        order_type="market",
                    )

        sample.assert_not_called()


class TestSubmitExit:
    def test_submit_exit_uses_existing_workflow_and_submits_close(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_exit_submission_intent=MagicMock(),
            mark_exit_order_submitted=MagicMock(),
            mark_exit_order_submit_failed=MagicMock(),
            mark_submission_intent_resolved=MagicMock(),
        )
        manager = OrderManager(paper=True)

        with (
            patch("core.order_manager.get_or_create_exit_workflow", return_value=workflow),
            patch("core.order_manager.cancel_open_orders_verified") as mock_cancel,
            patch(
                "core.order_manager.close_position",
                return_value=OrderResult(
                    True,
                    "sell-1",
                    "NVDA",
                    "sell",
                    20.0,
                    client_order_id="wf-nvda-1-exit",
                ),
            ) as mock_close,
        ):
            result = manager.submit_exit("NVDA", exit_reason="hard stop triggered")

        assert result.success is True
        mock_cancel.assert_called_once_with("NVDA")
        mock_close.assert_called_once_with("NVDA", client_order_id="wf-nvda-1-exit")
        workflow.mark_exit_order_submitted.assert_called_once_with(
            exit_reason="hard stop triggered",
            broker_order_id="sell-1",
        )

    def test_submit_exit_fails_closed_when_open_orders_cannot_be_cleared(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_exit_submission_intent=MagicMock(),
            mark_exit_order_submitted=MagicMock(),
            mark_exit_order_submit_failed=MagicMock(),
            mark_submission_intent_resolved=MagicMock(),
            mark_protective_stop=MagicMock(),
        )
        manager = OrderManager(paper=True)
        restored = ProtectiveStopResult(
            success=True,
            order_id="stop-keep",
            symbol="NVDA",
            qty=20.0,
            stop_price=465.0,
            action="reused",
            client_order_id="wf-nvda-1-sl",
        )

        with (
            patch("core.order_manager.get_or_create_exit_workflow", return_value=workflow),
            patch(
                "core.order_manager.cancel_open_orders_verified",
                side_effect=RuntimeError("broker unavailable"),
            ),
            patch(
                "core.order_manager.reconcile_symbol_after_exit_failure",
                return_value=restored,
            ) as reconcile,
            patch("core.order_manager.close_position") as mock_close,
        ):
            result = manager.submit_exit("NVDA", exit_reason="verification cleanup")

        assert result.success is False
        assert "broker unavailable" in result.error
        assert result.client_order_id == "wf-nvda-1-exit"
        mock_close.assert_not_called()
        reconcile.assert_called_once_with(
            "NVDA",
            workflow_id="wf-nvda-1",
        )
        workflow.mark_protective_stop.assert_called_once_with(
            success=True,
            stop_order_id="stop-keep",
            stop_price=465.0,
            action="exit_failure_reused",
            error="",
            stop_client_order_id="wf-nvda-1-sl",
        )
        workflow.mark_exit_order_submitted.assert_not_called()
        workflow.mark_exit_order_submit_failed.assert_called_once_with(
            exit_reason="verification cleanup",
            error=result.error,
        )

    def test_exit_intent_is_durable_before_stop_cancellation(self, tmp_path) -> None:
        db_path = tmp_path / "exit-intent-before-cancel.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            saw_pending_intent = False

            def fail_cancel(_symbol: str) -> int:
                nonlocal saw_pending_intent
                pending = get_execution_store().load_pending_submission_intents(
                    symbol="NVDA"
                )
                saw_pending_intent = bool(
                    pending and pending[0]["event"] == "exit_submission_intent"
                )
                raise RuntimeError("cancel failed")

            restored = ProtectiveStopResult(
                success=True,
                order_id="stop-restored",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="reused",
                client_order_id=f"{workflow.workflow_id}-sl",
            )
            pending_when_restoring: list[dict[str, Any]] | None = None

            def restore_after_cancel_failure(
                _symbol: str,
                *,
                workflow_id: str,
            ) -> ProtectiveStopResult:
                nonlocal pending_when_restoring
                assert workflow_id == workflow.workflow_id
                pending_when_restoring = (
                    get_execution_store().load_pending_submission_intents(
                        symbol="NVDA"
                    )
                )
                return restored

            with (
                patch(
                    "core.order_manager.cancel_open_orders_verified",
                    side_effect=fail_cancel,
                ),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    side_effect=restore_after_cancel_failure,
                ),
                patch("core.order_manager.close_position") as close_position,
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="verification cleanup",
                )

            pending_after_failure = (
                get_execution_store().load_pending_submission_intents(symbol="NVDA")
            )

        assert saw_pending_intent is True
        assert result.success is False
        assert pending_when_restoring == []
        assert pending_after_failure == []
        close_position.assert_not_called()

    def test_submit_exit_restores_protection_after_close_failure(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            mark_exit_submission_intent=MagicMock(),
            mark_exit_order_submitted=MagicMock(),
            mark_exit_order_submit_failed=MagicMock(),
            mark_protective_stop=MagicMock(),
            mark_submission_intent_resolved=MagicMock(),
        )
        manager = OrderManager(paper=True)
        failed_close = OrderResult(
            False,
            "",
            "NVDA",
            "sell",
            20.0,
            error="close rejected",
            client_order_id="wf-nvda-1-exit",
        )
        restored = ProtectiveStopResult(
            success=True,
            order_id="stop-2",
            symbol="NVDA",
            qty=20.0,
            stop_price=465.0,
            action="submitted",
        )

        with (
            patch("core.order_manager.get_or_create_exit_workflow", return_value=workflow),
            patch("core.order_manager.cancel_open_orders_verified"),
            patch("core.order_manager.close_position", return_value=failed_close),
            patch(
                "core.order_manager.reconcile_symbol_after_exit_failure",
                return_value=restored,
            ) as restore,
        ):
            result = manager.submit_exit("NVDA", exit_reason="verification cleanup")

        assert result.success is False
        restore.assert_called_once_with("NVDA", workflow_id="wf-nvda-1")
        workflow.mark_protective_stop.assert_called_once_with(
            success=True,
            stop_order_id="stop-2",
            stop_price=465.0,
            action="exit_failure_submitted",
            error="",
        )
        workflow.mark_exit_order_submit_failed.assert_called_once()

    def test_definitive_exit_rejection_resolves_intent_before_stop_restoration(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "definitive-exit-rejection.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            rejected = OrderResult(
                False,
                "exit-rejected",
                "NVDA",
                "sell",
                20.0,
                error="close rejected",
                client_order_id=exit_client_order_id,
            )
            restored = ProtectiveStopResult(
                success=True,
                order_id="stop-after-rejection",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-rejected",
            )
            pending_when_restoring: list[dict[str, Any]] | None = None

            def restore_after_resolution(
                _symbol: str,
                *,
                workflow_id: str,
            ) -> ProtectiveStopResult:
                nonlocal pending_when_restoring
                assert workflow_id == workflow.workflow_id
                pending_when_restoring = (
                    get_execution_store().load_pending_submission_intents(
                        symbol="NVDA"
                    )
                )
                return restored

            with (
                patch("core.order_manager.cancel_open_orders_verified"),
                patch("core.order_manager.close_position", return_value=rejected),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    side_effect=restore_after_resolution,
                ),
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="verification cleanup",
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")

        assert result.success is False
        assert pending_when_restoring == []
        assert pending == []
        assert active is not None and active["workflow_id"] == workflow.workflow_id

    def test_exit_transport_unknown_does_not_submit_competing_stop(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "exit-unknown.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            uncertain = OrderResult(
                False,
                "",
                "NVDA",
                "sell",
                20.0,
                error="response lost after request",
                client_order_id=f"{workflow.workflow_id}-exit",
                outcome_uncertain=True,
            )
            restored = ProtectiveStopResult(
                success=True,
                order_id="stop-restored",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-recovery",
            )

            with (
                patch("core.order_manager.cancel_open_orders_verified"),
                patch("core.order_manager.close_position", return_value=uncertain),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    return_value=restored,
                ) as reconcile,
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="verification cleanup",
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            payload = get_execution_store().load_workflow(workflow.workflow_id)
            active = get_execution_store().load_active_position("NVDA")

        assert result.success is False
        assert result.outcome_uncertain is True
        assert "exact broker outcome remains unresolved" in result.error
        reconcile.assert_not_called()
        assert len(pending) == 1
        assert pending[0]["event"] == "exit_submission_intent"
        assert active is not None and active["workflow_id"] == workflow.workflow_id
        assert payload is not None
        assert "exit_order_submit_failed" not in {
            transition["event"] for transition in payload["transitions"]
        }

    def test_submit_exit_refuses_retry_while_symbol_intent_is_unresolved(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "exit-pending-block.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            workflow.mark_exit_submission_intent(
                exit_reason="first cleanup",
                client_order_id=f"{workflow.workflow_id}-exit",
            )

            with (
                patch("core.order_manager.cancel_open_orders_verified") as cancel_again,
                patch("core.order_manager.close_position") as close_again,
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="retry cleanup",
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert result.success is False
        assert result.outcome_uncertain is True
        assert "unresolved submission intent" in result.error
        assert len(pending) == 1
        cancel_again.assert_not_called()
        close_again.assert_not_called()

    def test_exit_acceptance_stays_pending_until_final_fill(self, tmp_path) -> None:
        db_path = tmp_path / "exit-accepted-pending.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            manager = OrderManager(paper=True)
            accepted = OrderResult(
                True,
                "exit-accepted",
                "NVDA",
                "sell",
                20.0,
                client_order_id=f"{workflow.workflow_id}-exit",
            )
            with (
                patch("core.order_manager.cancel_open_orders_verified"),
                patch("core.order_manager.close_position", return_value=accepted),
            ):
                result = manager.submit_exit("NVDA", exit_reason="verification cleanup")

            pending_before_fill = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            with (
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="exit-accepted",
                    client_order_id=f"{workflow.workflow_id}-exit",
                    side="sell",
                    filled_qty=20.0,
                    fill_price=490.0,
                    order_type="market",
                )

            pending_after_fill = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert result.success is True
        assert len(pending_before_fill) == 1
        assert pending_before_fill[0]["event"] == "exit_submission_intent"
        assert pending_after_fill == []

    def test_zero_qty_exit_response_keeps_owner_when_position_is_not_visible(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "exit-zero-qty-position-lag.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            client_order_id = f"{workflow.workflow_id}-exit"
            hidden_position = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="position_not_visible",
                error="owned position is not yet visible",
            )
            synthetic_flat = OrderResult(
                True,
                "",
                "NVDA",
                "sell",
                0.0,
                client_order_id=client_order_id,
            )

            with (
                patch("core.order_manager.cancel_open_orders_verified"),
                patch("core.order_manager.close_position", return_value=synthetic_flat),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=hidden_position,
                ) as ensure,
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="verification cleanup",
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert result.success is False
        assert result.outcome_uncertain is True
        assert "not yet visible" in result.error
        assert len(pending) == 1
        assert active is not None and active["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        assert "exit_order_submit_failed" not in {
            transition["event"] for transition in snapshot["transitions"]
        }
        ensure.assert_called_once_with(
            symbol="NVDA",
            qty=20.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="entry-filled",
            entry_order_ids={"entry-filled"},
            durable_sell_fill_qty=0,
        )

    def test_zero_qty_exit_reports_failure_after_owned_position_is_reprotected(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "exit-zero-qty-reprotected.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            client_order_id = f"{workflow.workflow_id}-exit"
            synthetic_flat = OrderResult(
                True,
                "",
                "NVDA",
                "sell",
                0.0,
                client_order_id=client_order_id,
            )
            restored = ProtectiveStopResult(
                success=True,
                order_id="stop-restored",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-restored",
            )

            with (
                patch("core.order_manager.cancel_open_orders_verified"),
                patch("core.order_manager.close_position", return_value=synthetic_flat),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=restored,
                ),
            ):
                result = OrderManager(paper=True).submit_exit(
                    "NVDA",
                    exit_reason="verification cleanup",
                )

            active = get_execution_store().load_active_position("NVDA")
            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert result.success is False
        assert "remains stop-protected" in result.error
        assert active is not None and active["workflow_id"] == workflow.workflow_id
        assert pending == []


class TestStartupReconciliation:
    def test_startup_partial_entry_keeps_intent_until_working_leaf_is_terminal(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-entry-partial.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            order = SimpleNamespace(
                id="entry-partial",
                client_order_id=workflow.workflow_id,
                symbol="NVDA",
                side="buy",
                type="limit",
                status="partially_filled",
                qty="20",
                filled_qty="5",
                filled_avg_price="500",
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-partial-entry",
                symbol="NVDA",
                qty=5.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-partial-entry",
            )

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ),
                patch("core.order_manager.notify_buy_filled") as notify,
                patch("core.order_manager.submit_bracket_buy") as resubmit,
            ):
                OrderManager(paper=True)._reconcile_pending_entry_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert len(pending) == 1
        assert pending[0]["event"] == "entry_submission_intent"
        assert active is not None and active["qty"] == pytest.approx(5.0)
        assert snapshot is not None
        assert "buy_fill_received" in {
            transition["event"] for transition in snapshot["transitions"]
        }
        resubmit.assert_not_called()
        notify.assert_not_called()

    def test_startup_recovers_entry_filled_while_stream_was_down(self, tmp_path) -> None:
        db_path = tmp_path / "pending-entry-filled.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            order = SimpleNamespace(
                id="entry-accepted-before-crash",
                client_order_id=workflow.workflow_id,
                symbol="NVDA",
                side="buy",
                status="filled",
                filled_qty="20",
                filled_avg_price="500",
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-after-startup",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-startup",
            )
            manager = OrderManager(paper=True)

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ) as ensure,
                patch("core.order_manager.notify_buy_filled", return_value=False),
            ):
                manager._reconcile_pending_entry_intents_locked("NVDA")  # noqa: SLF001

            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        client.get_order_by_client_id.assert_called_once_with(workflow.workflow_id)
        ensure.assert_called_once()
        assert active is not None and active["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        assert any(
            ref["broker_order_id"] == "entry-accepted-before-crash"
            and ref["client_order_id"] == workflow.workflow_id
            for ref in snapshot["order_refs"]
        )

    def test_startup_follows_zero_fill_replaced_entry_to_filled_child(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-entry-replaced.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            parent = SimpleNamespace(
                id="entry-parent",
                client_order_id=workflow.workflow_id,
                symbol="NVDA",
                side="buy",
                type="limit",
                status="replaced",
                filled_qty="0",
                filled_avg_price="0",
                replaces=None,
                replaced_by="entry-child",
            )
            child = SimpleNamespace(
                id="entry-child",
                client_order_id=f"{workflow.workflow_id}-replacement",
                symbol="NVDA",
                side="buy",
                type="limit",
                status="filled",
                filled_qty="20",
                filled_avg_price="500",
                replaces="entry-parent",
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = parent
            client.get_order_by_id.return_value = child
            protection = ProtectiveStopResult(
                success=True,
                order_id="stop-after-child",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-child",
            )

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=protection,
                ),
                patch("core.order_manager.notify_buy_filled", return_value=False),
            ):
                OrderManager(paper=True)._reconcile_pending_entry_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        client.get_order_by_client_id.assert_called_once_with(workflow.workflow_id)
        client.get_order_by_id.assert_called_once_with("entry-child")
        assert active is not None and active["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        refs = {ref["broker_order_id"] for ref in snapshot["order_refs"]}
        assert {"entry-parent", "entry-child"}.issubset(refs)
        assert "order_submit_failed" not in {
            transition["event"] for transition in snapshot["transitions"]
        }

    def test_startup_pending_entry_lookup_failure_is_ambiguous_and_fails_closed(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-entry-missing.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_order_submission_intent(
                client_order_id=workflow.workflow_id,
                qty=20.0,
                limit_price=500.0,
            )
            client = MagicMock()
            client.get_order_by_client_id.side_effect = RuntimeError("404 order not found")

            with patch("core.order_manager._get_trading_client", return_value=client):
                with pytest.raises(RuntimeError, match="Cannot resolve pending NVDA entry"):
                    OrderManager(paper=True)._reconcile_pending_entry_intents_locked(  # noqa: SLF001
                        "NVDA"
                    )

            active = get_execution_store().load_active_position("NVDA")
            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )

        assert active is None
        assert len(pending) == 1
        assert pending[0]["workflow_id"] == workflow.workflow_id

    def test_startup_replays_filled_exit_accepted_before_reference_persisted(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-filled.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            order = SimpleNamespace(
                id="exit-accepted-before-crash",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="filled",
                filled_qty="20",
                filled_avg_price="490",
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch("core.order_manager.notify_sell_filled", return_value=False),
            ):
                OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            active = get_execution_store().load_active_position("NVDA")
            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert active is None
        assert pending == []
        assert snapshot is not None
        assert any(
            ref["broker_order_id"] == "exit-accepted-before-crash"
            and ref["client_order_id"] == exit_client_order_id
            for ref in snapshot["order_refs"]
        )
        assert "sell_fill_received" in {
            transition["event"] for transition in snapshot["transitions"]
        }

    def test_startup_replays_closed_exit_replacement_chain_from_current_flat_state(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-replacement-filled.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            root = SimpleNamespace(
                id="exit-parent",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="replaced",
                filled_qty="4",
                filled_avg_price="490",
                replaces=None,
                replaced_by="exit-child",
            )
            child = SimpleNamespace(
                id="exit-child",
                client_order_id=f"{workflow.workflow_id}-exit-r1",
                symbol="NVDA",
                side="sell",
                type="market",
                status="filled",
                filled_qty="16",
                filled_avg_price="489",
                replaces="exit-parent",
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = root
            client.get_order_by_id.return_value = child
            manager = OrderManager(paper=True)

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ) as sample,
                patch.object(
                    manager,
                    "_handle_sell_checkpoint_locked",
                    side_effect=AssertionError(
                        "historical broker snapshots are unavailable after restart"
                    ),
                ),
            ):
                manager._reconcile_pending_exit_intents_locked("NVDA")  # noqa: SLF001

            active = get_execution_store().load_active_position("NVDA")
            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        sample.assert_called()
        assert active is None
        assert pending == []
        assert snapshot is not None
        events = [transition["event"] for transition in snapshot["transitions"]]
        assert events.count("sell_partial_fill_received") == 1
        assert events.count("sell_fill_received") == 1
        assert {
            ref["broker_order_id"]
            for ref in snapshot["order_refs"]
            if ref["order_role"] == "sell_fill"
        } == {"exit-parent", "exit-child"}

    def test_startup_claims_working_exit_without_resubmitting(self, tmp_path) -> None:
        db_path = tmp_path / "pending-exit-working.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            order = SimpleNamespace(
                id="exit-working",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="new",
                filled_qty="0",
                filled_avg_price=None,
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            pending_exit = ProtectiveStopResult(
                success=True,
                order_id="exit-working",
                symbol="NVDA",
                qty=20.0,
                stop_price=0.0,
                action="pending_exit",
                client_order_id=exit_client_order_id,
            )

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    return_value=pending_exit,
                ) as reconcile,
                patch("core.order_manager.close_position") as close_again,
            ):
                OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        close_again.assert_not_called()
        reconcile.assert_called_once_with(
            "NVDA",
            workflow_id=workflow.workflow_id,
        )
        assert len(pending) == 1
        assert pending[0]["event"] == "exit_submission_intent"
        assert snapshot is not None
        assert any(
            ref["broker_order_id"] == "exit-working"
            and ref["client_order_id"] == exit_client_order_id
            for ref in snapshot["order_refs"]
        )

    def test_startup_partial_exit_keeps_intent_until_working_leaf_is_terminal(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-partial.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            order = SimpleNamespace(
                id="exit-partial",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="partially_filled",
                qty="20",
                filled_qty="5",
                filled_avg_price="490",
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            residual = PositionSummary("NVDA", 15.0, 500.0, 490.0, -0.02)
            pending_exit = ProtectiveStopResult(
                success=True,
                order_id="exit-partial",
                symbol="NVDA",
                qty=15.0,
                stop_price=0.0,
                action="pending_exit",
                client_order_id=exit_client_order_id,
            )

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(residual, [order]),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=pending_exit,
                ),
                patch("core.order_manager.close_position") as resubmit,
            ):
                OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert len(pending) == 1
        assert pending[0]["event"] == "exit_submission_intent"
        assert active is not None and active["qty"] == pytest.approx(15.0)
        assert snapshot is not None
        assert "sell_partial_fill_received" in {
            transition["event"] for transition in snapshot["transitions"]
        }
        resubmit.assert_not_called()

    def test_startup_exit_lookup_failure_keeps_intent_without_competing_stop(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-missing.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            client = MagicMock()
            client.get_order_by_client_id.side_effect = RuntimeError("404 order not found")

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_execution._sample_stable_symbol_state",
                    return_value=(
                        PositionSummary("NVDA", 20.0, 500.0, 500.0, 0.0),
                        [],
                    ),
                ),
                patch(
                    "core.order_execution.submit_stop_loss",
                    return_value=OrderResult(
                        False,
                        "",
                        "NVDA",
                        "sell",
                        20.0,
                        error="unexpected competing stop submission",
                    ),
                ) as submit_stop,
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                        "NVDA"
                    )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert "exact exit outcome remains unresolved" in str(exc_info.value)
        assert len(pending) == 1
        assert pending[0]["event"] == "exit_submission_intent"
        assert active is not None and active["workflow_id"] == workflow.workflow_id
        assert snapshot is not None
        protection_audits = [
            transition
            for transition in snapshot["transitions"]
            if transition["event"] == "protective_stop_reconciled"
        ]
        assert protection_audits[-1]["details"]["success"] is False
        assert protection_audits[-1]["details"]["stop_order_id"] == ""
        assert (
            protection_audits[-1]["details"]["action"]
            == "pending_exit_lookup_exit_outcome_unresolved"
        )
        submit_stop.assert_not_called()

    def test_startup_zero_fill_rejected_exit_restores_stop_and_resolves(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-rejected.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            order = SimpleNamespace(
                id="exit-rejected",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="rejected",
                filled_qty="0",
                filled_avg_price=None,
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            restored = ProtectiveStopResult(
                success=True,
                order_id="stop-after-rejection",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="submitted",
                client_order_id=f"{workflow.workflow_id}-sl-rejected",
            )
            pending_when_restoring: list[dict[str, Any]] | None = None

            def restore_after_resolution(**_kwargs: Any) -> ProtectiveStopResult:
                nonlocal pending_when_restoring
                pending_when_restoring = (
                    get_execution_store().load_pending_submission_intents(
                        symbol="NVDA"
                    )
                )
                return restored

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(
                        PositionSummary("NVDA", 20.0, 500.0, 500.0, 0.0),
                        [],
                    ),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    side_effect=restore_after_resolution,
                ),
            ):
                OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                    "NVDA"
                )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert pending == []
        assert pending_when_restoring == []
        assert active is not None and active["qty"] == pytest.approx(20.0)
        assert snapshot is not None
        events = [transition["event"] for transition in snapshot["transitions"]]
        assert "exit_order_submit_failed" in events
        assert "submission_intent_resolved" in events

    def test_startup_zero_fill_exit_resolves_intent_and_keeps_hidden_owner(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "pending-exit-rejected-position-lag.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=20.0,
                fill_price=500.0,
                broker_order_id="entry-filled",
            )
            exit_client_order_id = f"{workflow.workflow_id}-exit"
            workflow.mark_exit_submission_intent(
                exit_reason="verification cleanup",
                client_order_id=exit_client_order_id,
            )
            order = SimpleNamespace(
                id="exit-rejected-lag",
                client_order_id=exit_client_order_id,
                symbol="NVDA",
                side="sell",
                type="market",
                status="rejected",
                filled_qty="0",
                filled_avg_price=None,
                replaces=None,
                replaced_by=None,
            )
            client = MagicMock()
            client.get_order_by_client_id.return_value = order
            hidden_position = ProtectiveStopResult(
                success=False,
                order_id="",
                symbol="NVDA",
                qty=20.0,
                stop_price=465.0,
                action="position_not_visible",
                error="owned position is not yet visible",
            )

            with (
                patch("core.order_manager._get_trading_client", return_value=client),
                patch(
                    "core.order_manager._sample_stable_symbol_state",
                    return_value=(None, []),
                ),
                patch(
                    "core.order_manager.ensure_protective_stop",
                    return_value=hidden_position,
                ) as ensure,
            ):
                with pytest.raises(RuntimeError, match="not yet visible"):
                    OrderManager(paper=True)._reconcile_pending_exit_intents_locked(  # noqa: SLF001
                        "NVDA"
                    )

            pending = get_execution_store().load_pending_submission_intents(
                symbol="NVDA"
            )
            active = get_execution_store().load_active_position("NVDA")
            snapshot = get_execution_store().load_workflow(workflow.workflow_id)

        assert pending == []
        assert active is not None and active["workflow_id"] == workflow.workflow_id
        assert snapshot is not None
        events = [transition["event"] for transition in snapshot["transitions"]]
        assert "exit_order_submit_failed" in events
        assert "submission_intent_resolved" in events
        ensure.assert_called_once_with(
            symbol="NVDA",
            qty=20.0,
            fill_price=500.0,
            workflow_id=workflow.workflow_id,
            entry_order_id="entry-filled",
            entry_order_ids={"entry-filled"},
            durable_sell_fill_qty=0,
        )

    def test_startup_reconciliation_is_strict_scoped_and_workflow_linked(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-nvda-1",
            repair_buy_fill_storage=MagicMock(),
            mark_protective_stop=MagicMock(),
        )
        manager = OrderManager(paper=True)
        repaired = ProtectiveStopResult(
            success=True,
            action="submitted",
            symbol="NVDA",
            order_id="stop-1",
            qty=20.0,
            stop_price=465.0,
        )

        with (
            patch("core.order_manager.get_open_positions") as positions,
            patch("core.order_manager.get_active_workflow_for_symbol", return_value=workflow),
            patch(
                "core.order_manager.reconcile_symbol_after_exit_failure",
                return_value=repaired,
            ) as reconcile,
        ):
            positions.return_value = [
                PositionSummary("NVDA", 20.0, 500.0, 500.0, 0.0),
                PositionSummary("AAPL", 10.0, 200.0, 200.0, 0.0),
            ]
            results = manager.reconcile_startup_stops("NVDA")

        assert results == [repaired]
        positions.assert_called_once_with(raise_on_error=True)
        reconcile.assert_called_once_with(
            "NVDA",
            workflow_id="wf-nvda-1",
            minimum_position_qty=20.0,
        )
        workflow.mark_protective_stop.assert_called_once_with(
            success=True,
            stop_order_id="stop-1",
            stop_price=465.0,
            action="startup_submitted",
            error="",
        )

    def test_startup_reconciliation_repairs_existing_owner_to_broker_quantity(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "execution.sqlite3"

        with patch("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path)):
            reset_workflow_state()
            workflow = create_entry_workflow(_plan(), signal_payload={"symbol": "NVDA"})
            workflow.mark_buy_fill(
                qty=1.0,
                fill_price=500.0,
                broker_order_id="entry-1",
            )
            broker_position = PositionSummary("NVDA", 0.6, 505.0, 500.0, -0.01)
            protected = ProtectiveStopResult(
                success=True,
                action="reused",
                symbol="NVDA",
                order_id="stop-1",
                qty=0.6,
                stop_price=469.65,
            )

            with (
                patch(
                    "core.order_manager.get_open_positions",
                    return_value=[broker_position],
                ),
                patch(
                    "core.order_manager.reconcile_symbol_after_exit_failure",
                    return_value=protected,
                ),
            ):
                OrderManager(paper=True).reconcile_startup_stops("NVDA")

            active = get_execution_store().load_active_position("NVDA")

        assert active is not None
        assert active["workflow_id"] == workflow.workflow_id
        assert active["qty"] == pytest.approx(0.6)
        assert active["entry_price"] == pytest.approx(505.0)

    def test_startup_reconciliation_recovers_orphan_position_ownership(self) -> None:
        workflow = SimpleNamespace(
            workflow_id="wf-recovered-1",
            mark_protective_stop=MagicMock(),
        )
        manager = OrderManager(paper=True)
        repaired = ProtectiveStopResult(
            success=False,
            action="unlinked_stop",
            symbol="NVDA",
            order_id="stop-old",
            qty=20.0,
            stop_price=465.0,
            error="stop is not linked to recovered workflow",
        )

        with (
            patch(
                "core.order_manager.get_open_positions",
                return_value=[PositionSummary("NVDA", 20.0, 500.0, 500.0, 0.0)],
            ),
            patch("core.order_manager.get_active_workflow_for_symbol", return_value=None),
            patch(
                "core.order_manager.recover_active_position_workflow",
                return_value=workflow,
            ) as recover,
            patch(
                "core.order_manager.reconcile_symbol_after_exit_failure",
                return_value=repaired,
            ),
        ):
            results = manager.reconcile_startup_stops("NVDA")

        assert results == [repaired]
        recover.assert_called_once_with("NVDA", qty=20.0, avg_entry_price=500.0)
        workflow.mark_protective_stop.assert_called_once_with(
            success=False,
            stop_order_id="stop-old",
            stop_price=465.0,
            action="startup_unlinked_stop",
            error="stop is not linked to recovered workflow",
        )

    def test_startup_reconciliation_propagates_strict_broker_read_failure(self) -> None:
        manager = OrderManager(paper=True)

        with (
            patch(
                "core.order_manager.get_open_positions",
                side_effect=RuntimeError("broker unavailable"),
            ),
            patch("core.order_manager.reconcile_symbol_after_exit_failure") as reconcile,
        ):
            with pytest.raises(RuntimeError, match="broker unavailable"):
                manager.reconcile_startup_stops("NVDA")

        reconcile.assert_not_called()


class TestInferExitReason:
    @pytest.mark.parametrize(
        ("order_type", "client_order_id", "expected"),
        [
            ("stop", "wf-nvda-1", "stop-loss triggered"),
            ("market", "wf-nvda-ma", "MA violation exit"),
            ("market", "wf-nvda-1", "exit order filled"),
        ],
    )
    def test_infer_exit_reason(self, order_type: str, client_order_id: str, expected: str) -> None:
        assert OrderManager._infer_exit_reason(order_type=order_type, client_order_id=client_order_id) == expected
