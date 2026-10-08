"""Offline policy-action consumption through the legacy execution workflow."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.execution_store import get_execution_store
from core.execution_workflow import (
    EntryExecutionPlan,
    ExecutionWorkflow,
    build_exit_client_order_id,
    clear_workflow_registry,
    create_entry_workflow,
    get_workflow,
)
from core.order_execution import OrderResult, ProtectiveStopResult
from core.order_execution import (
    PositionSummary,
    build_stop_client_order_id,
    ensure_protective_stop,
    reconcile_symbol_after_exit_failure,
)
from core.order_manager import OrderManager
from core.policy_execution_state import (
    ActionAttemptStatus,
    ActionRole,
    ActionStatus,
    DecisionCategory,
    DecisionClock,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    PolicyDeploymentIdentity,
    build_action_intent,
)
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_protection_bridge import PolicyProtectionBridge


def _plan() -> EntryExecutionPlan:
    return EntryExecutionPlan(
        symbol="NVDA",
        entry_price=500.0,
        price_source="offline_policy_fixture",
        stop_price=465.0,
        stop_loss_pct=0.07,
        position_value=10_000.0,
        risk_amount=700.0,
        risk_per_share=35.0,
        qty=20.0,
        canslim_score=0.0,
        rs_score=0.0,
        is_breakout=False,
        has_volume_surge=False,
    )


def _entry_guard_kwargs() -> dict[str, object]:
    return {"execution_ready": lambda: True, "market_open_check": lambda: True}


@pytest.fixture
def action_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "same-fixture.sqlite3"
    monkeypatch.setattr("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(db_path))
    clear_workflow_registry()

    store = PolicyExecutionStateStore(db_path, store_identity="offline-policy-store")
    store.migrate()
    deployment = PolicyDeploymentIdentity(
        policy_artifact_id="fixed-policy:102-test",
        capability_manifest_id="manifest:102-test",
        policy_interface_version="3",
        feature_contract_id="feature-contract:102-test",
        feature_calculator_id="calculator:102-test",
        source_revision="c6c5b715f77a1225ff9f6e456c8cf3d5145654ab",
        runtime_identity="offline-test-only",
        execution_profile_id="paper-fixture",
        paper_account_environment_id="paper-account:102-test",
        store_identity=store.store_identity,
    )
    store.register_deployment_identity(
        deployment,
        lifecycle="prepared",
        handler_identity="fixed-policy-test",
        guard_id="explicit-test-guard",
    )
    clock = DecisionClock(
        exchange_id="XNYS",
        decision_session=date(2026, 10, 1),
        as_of_cutoff_at=datetime(2026, 10, 1, 20, tzinfo=UTC),
        next_execution_session=date(2026, 10, 2),
        account_valuation_session=date(2026, 10, 2),
        account_valuation_at=datetime(2026, 10, 2, 13, 31, tzinfo=UTC),
    )
    decision = DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256="a" * 64,
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="security:NVDA",
    )
    action = build_action_intent(
        decision=decision,
        security_id="security:NVDA",
        broker_symbol="NVDA",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("20"),
        status=ActionStatus.INTENDED,
        reservation_price=Decimal("500"),
        reservation_price_basis="policy_limit",
        reservation_stop_price=Decimal("465"),
        risk_per_unit=Decimal("35"),
        risk_basis="policy_stop_distance",
    )
    store.record_decision(
        decision,
        policy_payload={"policy": "fixed-policy:102-test"},
        guard_payload={"outcome": "allow"},
        effective_action_payload={"logical_action_id": action.logical_action_id},
    )
    store.record_action_intent(action, expected_version=None)
    yield store, action
    clear_workflow_registry()


def test_policy_entry_binds_action_and_submission_intent_before_fake_broker(
    action_store,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    observed = []

    def submit(**kwargs):
        projection = store.load_action_projection(action.logical_action_id)
        attempt = projection.order_attempts[0]
        pending = get_execution_store().load_pending_submission_intents(symbol="NVDA")
        observed.append((attempt.client_order_id, attempt.status.value, len(pending)))
        return OrderResult(
            True,
            "broker-order-102",
            "NVDA",
            "buy",
            20.0,
            client_order_id=kwargs["client_order_id"],
        )

    with (
        patch("core.order_manager.submit_bracket_buy", side_effect=submit) as broker_submit,
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert outcome.success is True
    assert outcome.outcome_uncertain is False
    assert observed == [(outcome.workflow_id, "submitted", 1)]
    broker_submit.assert_called_once()
    projection = store.load_action_projection(action.logical_action_id)
    assert projection.status is ActionStatus.SUBMITTED
    assert projection.order_attempts[0].client_order_id == outcome.workflow_id
    assert projection.order_attempts[0].broker_order_id == "broker-order-102"
    workflow = get_execution_store().load_workflow(outcome.workflow_id)
    assert workflow is not None
    assert [item["event"] for item in workflow["transitions"][:4]] == [
        "signal_accepted",
        "plan_built",
        "policy_entry_guards_passed",
        "entry_submission_intent",
    ]


def test_policy_entry_refuses_a_plan_that_changes_the_persisted_stop(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    wrong_plan = _plan()
    wrong_plan = EntryExecutionPlan(**{**wrong_plan.__dict__, "stop_price": 460.0})

    with patch("core.order_manager.submit_bracket_buy") as broker_submit:
        with pytest.raises(ValueError, match="stop"):
            manager.submit_policy_entry(
                wrong_plan, logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    broker_submit.assert_not_called()
    assert store.load_action_projection(action.logical_action_id).status is ActionStatus.INTENDED


def test_live_policy_entry_requires_dynamic_readiness_and_configured_market_guard(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with patch("core.order_manager.settings.ENTRY_MARKET_HOURS_ONLY", True):
        with pytest.raises(RuntimeError, match="requires an execution readiness callback"):
            manager.submit_policy_entry(_plan(), logical_action_id=action.logical_action_id)
        with pytest.raises(RuntimeError, match="requires a market-open callback"):
            manager.submit_policy_entry(
                _plan(),
                logical_action_id=action.logical_action_id,
                execution_ready=lambda: True,
            )

    projection = store.load_action_projection(action.logical_action_id)
    assert projection.status is ActionStatus.INTENDED
    assert projection.order_attempts[0].client_order_id is None


@pytest.mark.parametrize(
    ("execution_ready", "market_open", "expected_reason"),
    [
        (False, True, "readiness callback denied"),
        (True, False, "market-open callback denied"),
    ],
)
def test_policy_entry_guard_veto_is_audited_and_resumes_without_broker_intent(
    action_store,
    execution_ready,
    market_open,
    expected_reason,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with (
        patch("core.order_manager.settings.ENTRY_MARKET_HOURS_ONLY", True),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
        with pytest.raises(RuntimeError, match=expected_reason):
            manager.submit_policy_entry(
                _plan(),
                logical_action_id=action.logical_action_id,
                execution_ready=lambda: execution_ready,
                market_open_check=lambda: market_open,
            )

    broker_submit.assert_not_called()
    projection = store.load_action_projection(action.logical_action_id)
    assert projection.status is ActionStatus.SUBMITTED
    workflow_id = projection.order_attempts[0].client_order_id
    assert workflow_id
    workflow = get_workflow(workflow_id)
    assert workflow is not None
    assert workflow.entry_plan == _plan()
    assert any(item.event == "policy_entry_guard_blocked" for item in workflow.transitions)
    assert not any(item.event == "entry_submission_intent" for item in workflow.transitions)
    assert get_execution_store().load_pending_submission_intents(symbol="NVDA") == []

    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-after-guard-ready", "NVDA", "buy", 20.0),
        ) as broker_submit,
        patch("core.order_manager.notify_entry_submitted", return_value=True),
        patch("core.order_manager.settings.ENTRY_MARKET_HOURS_ONLY", True),
    ):
        recovered = manager.submit_policy_entry(
            _plan(),
            logical_action_id=action.logical_action_id,
            **_entry_guard_kwargs(),
        )
    assert recovered.success is True
    broker_submit.assert_called_once()


def test_cumulative_entry_facts_update_policy_holding_and_replay_after_restart(action_store) -> None:
    store, action = action_store
    bridge = PolicyProtectionBridge(store, provider_id="fake-paper-broker")

    first = bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("4"),
        average_fill_price=Decimal("501"),
        observed_at=datetime(2026, 10, 2, 13, 32, tzinfo=UTC),
    )
    assert first.confirmed_quantity == Decimal("4")
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.remaining_quantity == Decimal("4")
    assert holding.initial_filled_quantity == Decimal("4")
    assert holding.committed_risk == Decimal("140")
    assert holding.cost_basis is None
    assert first.reservation_amount == Decimal("8000")

    second = bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("10"),
        average_fill_price=Decimal("502"),
        observed_at=datetime(2026, 10, 2, 13, 33, tzinfo=UTC),
    )
    assert second.confirmed_quantity == Decimal("10")
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.initial_filled_quantity == Decimal("4")
    assert holding.opening_later_fills_quantity == Decimal("6")
    assert holding.remaining_quantity == Decimal("10")
    assert holding.committed_risk == Decimal("350")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    replay = PolicyProtectionBridge(restarted, provider_id="fake-paper-broker").record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("10"),
        average_fill_price=Decimal("502"),
        observed_at=datetime(2026, 10, 2, 13, 33, tzinfo=UTC),
    )
    assert replay == restarted.load_action_projection(action.logical_action_id)
    assert replay.confirmed_quantity == Decimal("10")
    assert restarted.load_holding_episode_for_action(action.logical_action_id).remaining_quantity == Decimal("10")


def test_cumulative_fill_facts_fail_closed_when_price_changes_at_same_watermark(action_store) -> None:
    store, action = action_store
    bridge = PolicyProtectionBridge(store, provider_id="fake-paper-broker")
    bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("4"),
        average_fill_price=Decimal("501"),
        observed_at=datetime(2026, 10, 2, 13, 32, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="notional or fees changed"):
        bridge.record_cumulative_fill(
            action.logical_action_id,
            attempt_number=1,
            broker_order_id="broker-entry-102",
            client_order_id="client-entry-102",
            cumulative_quantity=Decimal("4"),
            average_fill_price=Decimal("502"),
            observed_at=datetime(2026, 10, 2, 13, 33, tzinfo=UTC),
        )


def test_policy_to_workflow_handoff_recovers_after_failure_before_workflow_creation(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)

    with patch("core.order_manager.create_entry_workflow", side_effect=RuntimeError("handoff interruption")):
        with pytest.raises(RuntimeError, match="handoff interruption"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    prepared = store.load_action_projection(action.logical_action_id)
    durable_client_id = prepared.order_attempts[0].client_order_id
    assert durable_client_id
    assert prepared.status is ActionStatus.SUBMITTED
    assert get_execution_store().load_workflow(durable_client_id) is None

    with (
        patch("core.order_manager.create_entry_workflow", wraps=create_entry_workflow),
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-after-handoff", "NVDA", "buy", 20.0),
        ) as broker_submit,
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert outcome.success is True
    assert outcome.workflow_id == durable_client_id
    broker_submit.assert_called_once()
    assert store.load_action_projection(action.logical_action_id).order_attempts[0].broker_order_id == (
        "broker-after-handoff"
    )


def test_signal_only_policy_workflow_resumes_plan_after_restart(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    workflow_id = manager._policy_workflow_id("NVDA", action.logical_action_id)  # noqa: SLF001

    with (
        patch.object(ExecutionWorkflow, "mark_plan_built", side_effect=RuntimeError("crash after signal")),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
        with pytest.raises(RuntimeError, match="crash after signal"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    snapshot = get_execution_store().load_workflow(workflow_id)
    assert snapshot is not None
    assert [item["event"] for item in snapshot["transitions"]] == ["signal_accepted"]
    broker_submit.assert_not_called()

    clear_workflow_registry()
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-after-plan-resume", "NVDA", "buy", 20.0),
        ) as broker_submit,
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        resumed = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
        replayed = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert resumed.success is True
    assert replayed.success is True
    assert resumed.workflow_id == workflow_id == replayed.workflow_id
    broker_submit.assert_called_once()


def test_plan_built_policy_workflow_resumes_before_submission_intent(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)

    with (
        patch.object(
            ExecutionWorkflow,
            "mark_order_submission_intent",
            side_effect=RuntimeError("crash before submission intent"),
        ),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
        with pytest.raises(RuntimeError, match="crash before submission intent"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    workflow_id = manager._policy_workflow_id("NVDA", action.logical_action_id)  # noqa: SLF001
    snapshot = get_execution_store().load_workflow(workflow_id)
    assert snapshot is not None
    assert [item["event"] for item in snapshot["transitions"]] == ["signal_accepted", "plan_built", "policy_entry_guards_passed"]
    broker_submit.assert_not_called()

    clear_workflow_registry()
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-after-plan", "NVDA", "buy", 20.0),
        ) as broker_submit,
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        first = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
        replayed = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert first.success is True and replayed.success is True
    assert first.workflow_id == replayed.workflow_id == workflow_id
    broker_submit.assert_called_once()


def test_submission_intent_restart_reconciles_exact_order_without_duplicate_submit(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    original = ExecutionWorkflow.mark_order_submission_intent

    def persist_then_interrupt(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("crash after durable submission intent")

    with (
        patch.object(ExecutionWorkflow, "mark_order_submission_intent", persist_then_interrupt),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
        with pytest.raises(RuntimeError, match="crash after durable submission intent"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )
    broker_submit.assert_not_called()
    clear_workflow_registry()

    workflow_id = manager._policy_workflow_id("NVDA", action.logical_action_id)  # noqa: SLF001
    client = SimpleNamespace(
        get_order_by_client_id=lambda client_order_id: SimpleNamespace(
            id="broker-found-after-intent",
            client_order_id=client_order_id,
            symbol="NVDA",
            side="buy",
        )
    )
    with (
        patch("core.order_manager._get_trading_client", return_value=client),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
        recovered = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
        replayed = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert recovered.success is True and replayed.success is True
    assert recovered.workflow_id == replayed.workflow_id == workflow_id
    assert recovered.order_id == "broker-found-after-intent"
    broker_submit.assert_not_called()
    assert store.load_action_projection(action.logical_action_id).order_attempts[0].broker_order_id == (
        "broker-found-after-intent"
    )


def test_broker_acceptance_restart_binds_policy_reference_without_resubmission(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-accepted-before-ref", "NVDA", "buy", 20.0),
        ) as broker_submit,
        patch.object(
            OrderManager,
            "_bind_policy_broker_reference",
            side_effect=RuntimeError("crash before policy broker-reference bind"),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="crash before policy broker-reference bind"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )
    broker_submit.assert_called_once()

    clear_workflow_registry()
    with patch("core.order_manager.submit_bracket_buy") as broker_submit:
        recovered = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
        replayed = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    assert recovered.success is True and replayed.success is True
    assert recovered.order_id == replayed.order_id == "broker-accepted-before-ref"
    broker_submit.assert_not_called()
    assert store.load_action_projection(action.logical_action_id).order_attempts[0].broker_order_id == (
        "broker-accepted-before-ref"
    )


def test_recovered_policy_workflow_rejects_different_persisted_plan(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    projection = store.load_action_projection(action.logical_action_id)
    workflow_id = manager._policy_workflow_id("NVDA", action.logical_action_id)  # noqa: SLF001
    conflicting_plan = EntryExecutionPlan(
        **{**_plan().__dict__, "entry_price": 499.0}
    )
    create_entry_workflow(
        conflicting_plan,
        signal_payload=manager._policy_signal_payload(projection),  # noqa: SLF001
        workflow_id=workflow_id,
    )

    with patch("core.order_manager.submit_bracket_buy") as broker_submit:
        with pytest.raises(ValueError, match="plan differs"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    broker_submit.assert_not_called()


def test_unknown_policy_entry_submission_is_reconciled_without_blind_resubmit(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)

    with patch("core.order_manager.submit_bracket_buy", side_effect=SystemExit("crash after durable intent")):
        with pytest.raises(SystemExit, match="crash after durable intent"):
            manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    client = SimpleNamespace(
        get_order_by_client_id=lambda _client_id: (_ for _ in ()).throw(
            RuntimeError("lookup unavailable")
        )
    )
    with (
        patch("core.order_manager._get_trading_client", return_value=client),
        patch("core.order_manager.submit_bracket_buy") as broker_submit,
    ):
            outcome = manager.submit_policy_entry(
                _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
            )

    assert outcome.success is False
    assert outcome.outcome_uncertain is True
    assert "remains unresolved" in outcome.error
    broker_submit.assert_not_called()


def test_policy_stop_proposal_precedes_broker_and_confirms_price_and_remaining_quantity(
    action_store,
) -> None:
    store, action = action_store
    bridge = PolicyProtectionBridge(store, provider_id="fake-paper-broker")
    bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("4"),
        average_fill_price=Decimal("501"),
        cumulative_fees=Decimal("0"),
        observed_at=datetime(2026, 10, 2, 13, 32, tzinfo=UTC),
    )
    workflow = create_entry_workflow(_plan(), signal_payload={"logical_action_id": action.logical_action_id})
    observed = []

    def ensure(**kwargs):
        holding = store.load_holding_episode_for_action(action.logical_action_id)
        observed.append(
            (
                holding.proposed_stop_price,
                holding.confirmed_protective_stop_price,
                kwargs.get("stop_price_override"),
                kwargs["qty"],
            )
        )
        return ProtectiveStopResult(
            success=True,
            order_id="broker-stop-102",
            symbol="NVDA",
            qty=4.0,
            stop_price=465.0,
            action="submitted",
            client_order_id="client-stop-102",
        )

    with patch("core.order_execution.ensure_protective_stop", side_effect=ensure):
        result = bridge.reconcile_protective_stop(
            action.logical_action_id,
            workflow=workflow,
            average_entry_price=Decimal("501"),
            entry_order_ids={"broker-entry-102"},
            observed_at=datetime(2026, 10, 2, 13, 34, tzinfo=UTC),
        )

    assert result.success is True
    assert observed == [(Decimal("465"), None, 465.0, 4.0)]
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.proposed_stop_price == Decimal("465")
    assert holding.confirmed_protective_stop_price == Decimal("465")
    assert holding.remaining_quantity == Decimal("4")
    assert holding.confirmed_stop_client_order_id == "client-stop-102"
    assert holding.confirmed_stop_broker_order_id == "broker-stop-102"


def test_startup_recovery_records_exact_flattening_stop_fill_through_canonical_store(
    action_store,
) -> None:
    store, action = action_store
    bridge = PolicyProtectionBridge(store, provider_id="fake-paper-broker")
    bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-startup-stop",
        client_order_id="client-entry-startup-stop",
        cumulative_quantity=Decimal("4"),
        average_fill_price=Decimal("501"),
        cumulative_fees=Decimal("0"),
        observed_at=datetime(2026, 10, 2, 13, 32, tzinfo=UTC),
    )
    workflow = create_entry_workflow(
        _plan(), signal_payload={"logical_action_id": action.logical_action_id}
    )
    stop_result = ProtectiveStopResult(
        success=True,
        order_id="broker-stop-startup-flat",
        symbol="NVDA",
        qty=4.0,
        stop_price=465.0,
        action="submitted",
        client_order_id="client-stop-startup-flat",
    )
    with patch("core.order_execution.ensure_protective_stop", return_value=stop_result):
        bridge.reconcile_protective_stop(
            action.logical_action_id,
            workflow=workflow,
            average_entry_price=Decimal("501"),
            entry_order_ids={"broker-entry-startup-stop"},
            observed_at=datetime(2026, 10, 2, 13, 34, tzinfo=UTC),
        )

    broker_order = SimpleNamespace(
        id="broker-stop-startup-flat",
        client_order_id="client-stop-startup-flat",
        symbol="NVDA",
        side="sell",
        type="stop",
        status="filled",
        filled_qty="4",
        filled_avg_price="460",
    )
    client = SimpleNamespace(get_order_by_id=lambda _order_id: broker_order)
    manager = OrderManager(paper=True, policy_store=store)
    with patch("core.order_manager._get_trading_client", return_value=client):
        recovered = manager._recover_confirmed_policy_stop_fills(None)  # noqa: SLF001

    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.remaining_quantity == Decimal("0")
    assert holding.pending_action_ids == (action.logical_action_id,)
    assert len(recovered) == 1
    assert recovered[0].role is ActionRole.CLOSE
    assert recovered[0].side is OrderSide.SELL
    assert recovered[0].status is ActionStatus.FILLED
    assert recovered[0].confirmed_quantity == Decimal("4")


def test_failed_policy_stop_replacement_keeps_proposal_and_old_confirmation(action_store) -> None:
    store, action = action_store
    bridge = PolicyProtectionBridge(store, provider_id="fake-paper-broker")
    bridge.record_cumulative_fill(
        action.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-entry-102",
        client_order_id="client-entry-102",
        cumulative_quantity=Decimal("20"),
        average_fill_price=Decimal("501"),
        cumulative_fees=Decimal("0"),
        observed_at=datetime(2026, 10, 2, 13, 32, tzinfo=UTC),
    )
    workflow = create_entry_workflow(_plan(), signal_payload={"logical_action_id": action.logical_action_id})
    original_time = datetime(2026, 10, 2, 13, 34, tzinfo=UTC)
    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=ProtectiveStopResult(
            True,
            "broker-stop-original",
            "NVDA",
            20.0,
            465.0,
            "submitted",
            client_order_id="client-stop-original",
        ),
    ):
        bridge.reconcile_protective_stop(
            action.logical_action_id,
            workflow=workflow,
            average_entry_price=Decimal("501"),
            observed_at=original_time,
        )

    add_decision = DecisionIdentity.build(
        deployment=action.decision.deployment_identity,
        clock=action.decision.clock,
        snapshot_sha256="b" * 64,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=store.load_holding_episode_for_action(action.logical_action_id).holding_episode_id,
    )
    store.record_decision(
        add_decision,
        policy_payload={"stop_target": "480"},
        guard_payload={"outcome": "allow"},
        effective_action_payload={"stop_target": "480"},
    )
    addition = build_action_intent(
        decision=add_decision,
        security_id=action.security_id,
        broker_symbol="NVDA",
        holding_episode_id=store.load_holding_episode_for_action(action.logical_action_id).holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("2"),
        status=ActionStatus.SUBMITTED,
        reservation_price=Decimal("505"),
        reservation_price_basis="policy_limit",
        reservation_stop_price=Decimal("480"),
        risk_per_unit=Decimal("25"),
        risk_basis="tightened_policy_stop_distance",
    )
    store.record_action_intent(addition, expected_version=None)
    bridge.record_cumulative_fill(
        addition.logical_action_id,
        attempt_number=1,
        broker_order_id="broker-add-102",
        client_order_id="client-add-102",
        cumulative_quantity=Decimal("2"),
        average_fill_price=Decimal("505"),
        cumulative_fees=Decimal("0"),
        observed_at=datetime(2026, 10, 2, 13, 35, tzinfo=UTC),
    )
    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=ProtectiveStopResult(
            False,
            "",
            "NVDA",
            22.0,
            480.0,
            "submission_unknown",
            error="fake broker outcome unknown",
            client_order_id="client-stop-replacement",
        ),
    ):
        failed = bridge.reconcile_protective_stop(
            addition.logical_action_id,
            workflow=workflow,
            average_entry_price=Decimal("502"),
            entry_order_ids={"broker-entry-102", "broker-add-102"},
            observed_at=datetime(2026, 10, 2, 13, 36, tzinfo=UTC),
        )

    assert failed.success is False
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.proposed_stop_price == Decimal("480")
    assert holding.confirmed_protective_stop_price == Decimal("465")
    assert holding.confirmed_stop_broker_order_id == "broker-stop-original"
    assert holding.remaining_quantity == Decimal("22")


def test_partial_entry_callback_updates_policy_and_uses_persisted_stop(action_store) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    stop_result = ProtectiveStopResult(
        True,
        "broker-stop-from-fill",
        "NVDA",
        4.0,
        465.0,
        "submitted",
        client_order_id="client-stop-from-fill",
    )
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-entry-callback", "NVDA", "buy", 20.0),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    with patch("core.order_execution.ensure_protective_stop", return_value=stop_result) as ensure:
        manager.handle_partial_fill(
            symbol="NVDA",
            broker_order_id="broker-entry-callback",
            client_order_id=outcome.workflow_id,
            side="buy",
            filled_qty=4.0,
            fill_price=501.0,
            order_type="limit",
        )

    projection = store.load_action_projection(action.logical_action_id)
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert projection.confirmed_quantity == Decimal("4")
    assert holding.remaining_quantity == Decimal("4")
    assert holding.confirmed_protective_stop_price == Decimal("465")
    assert ensure.call_args.kwargs["stop_price_override"] == 465.0
    assert ensure.call_args.kwargs["qty"] == 4.0


@pytest.mark.parametrize("failed_action", ["position_not_visible", "position_sync_pending"])
def test_policy_buy_fill_keeps_prefixed_position_sync_failure_unresolved(
    action_store,
    failed_action: str,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-entry-sync", "NVDA", "buy", 20.0),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    unresolved = ProtectiveStopResult(
        False,
        "",
        "NVDA",
        20.0,
        465.0,
        failed_action,
        error="fake broker position sync is unresolved",
        client_order_id="client-stop-sync-pending",
    )
    with (
        patch("core.order_execution.ensure_protective_stop", return_value=unresolved),
        patch.object(manager, "_submit_exit_locked") as emergency_exit,
        patch("core.order_manager.cancel_open_orders_verified") as cancel_orders,
        pytest.raises(RuntimeError, match="Safety remains unproven for NVDA"),
    ):
        manager.handle_partial_fill(
            symbol="NVDA",
            broker_order_id="broker-entry-sync",
            client_order_id=outcome.workflow_id,
            side="buy",
            filled_qty=20.0,
            fill_price=501.0,
            order_type="limit",
        )

    emergency_exit.assert_not_called()
    cancel_orders.assert_not_called()
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.proposed_stop_action_id is not None
    assert holding.confirmed_stop_action_id is None
    workflow = get_workflow(outcome.workflow_id)
    assert workflow is not None
    failed_events = [
        transition
        for transition in workflow.transitions
        if transition.event == "protective_stop_reconciled"
        and transition.details.get("success") is False
    ]
    assert failed_events[-1].details["action"] == f"policy_{failed_action}"


@pytest.mark.parametrize("failed_action", ["position_not_visible", "position_sync_pending"])
def test_policy_residual_sell_keeps_prefixed_position_sync_failure_unresolved(
    action_store,
    failed_action: str,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-entry-residual", "NVDA", "buy", 20.0),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )

    confirmed_stop = ProtectiveStopResult(
        True,
        "broker-stop-residual",
        "NVDA",
        20.0,
        465.0,
        "submitted",
        client_order_id="client-stop-residual",
    )
    with patch("core.order_execution.ensure_protective_stop", return_value=confirmed_stop):
        manager.handle_partial_fill(
            symbol="NVDA",
            broker_order_id="broker-entry-residual",
            client_order_id=outcome.workflow_id,
            side="buy",
            filled_qty=20.0,
            fill_price=501.0,
            order_type="limit",
        )

    workflow = get_workflow(outcome.workflow_id)
    assert workflow is not None
    exit_client_order_id = build_exit_client_order_id(workflow.workflow_id)
    workflow.mark_exit_submission_intent(
        exit_reason="test partial scale-out",
        client_order_id=exit_client_order_id,
    )
    workflow.mark_exit_order_submitted(
        exit_reason="test partial scale-out",
        broker_order_id="broker-partial-exit",
        requested_quantity=20.0,
    )
    residual = PositionSummary("NVDA", 17.0, 501.0, 520.0, 0.01)
    unresolved = ProtectiveStopResult(
        False,
        "",
        "NVDA",
        20.0,
        465.0,
        failed_action,
        error="fake broker residual position sync is unresolved",
        client_order_id="client-stop-residual-pending",
    )
    with (
        patch.object(manager, "_wait_for_sell_position_sync", return_value=(residual, [])),
        patch("core.order_execution.ensure_protective_stop", return_value=unresolved),
        patch.object(manager, "_submit_exit_locked") as emergency_exit,
        patch("core.order_manager.cancel_open_orders_verified") as cancel_orders,
        pytest.raises(RuntimeError, match="Safety remains unproven for NVDA"),
    ):
        manager.handle_fill(
            symbol="NVDA",
            broker_order_id="broker-partial-exit",
            client_order_id=exit_client_order_id,
            side="sell",
            filled_qty=3.0,
            fill_price=520.0,
            order_type="market",
        )

    emergency_exit.assert_not_called()
    cancel_orders.assert_not_called()
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.proposed_stop_action_id is not None
    assert holding.confirmed_protective_stop_price == Decimal("465")
    failed_events = [
        transition
        for transition in workflow.transitions
        if transition.event == "protective_stop_reconciled"
        and transition.details.get("success") is False
    ]
    assert failed_events[-1].details["action"] == f"policy_{failed_action}"


def test_policy_addition_partial_sale_and_restart_keep_strongest_stop(action_store) -> None:
    store, entry = action_store
    manager = OrderManager(paper=True, policy_store=store)
    opening = manager.record_policy_cumulative_fill(
        entry.logical_action_id,
        attempt_number=1,
        side="buy",
        broker_order_id="broker-opening-lifecycle",
        client_order_id="client-opening-lifecycle",
        cumulative_quantity=Decimal("20"),
        average_fill_price=Decimal("501"),
    )
    assert opening.confirmed_quantity == Decimal("20")
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    workflow = create_entry_workflow(
        _plan(),
        signal_payload={"logical_action_id": entry.logical_action_id},
        workflow_id="cslm-nvda-policy-lifecycle",
    )

    def confirmed_stop(*, order_id: str, qty: float, price: float) -> ProtectiveStopResult:
        return ProtectiveStopResult(
            True,
            order_id,
            "NVDA",
            qty,
            price,
            "submitted",
            client_order_id=f"client-{order_id}",
        )

    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=confirmed_stop(order_id="stop-opening", qty=20.0, price=465.0),
    ):
        initial = manager.reconcile_policy_protection(
            entry.logical_action_id,
            workflow_id=workflow.workflow_id,
            average_entry_price=Decimal("501"),
        )
    assert initial.success is True

    addition_decision = DecisionIdentity.build(
        deployment=entry.decision.deployment_identity,
        clock=entry.decision.clock,
        snapshot_sha256="b" * 64,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(
        addition_decision,
        policy_payload={"fixture": "addition"},
        guard_payload={"outcome": "allow"},
        effective_action_payload={"stop_price": "480"},
    )
    addition = build_action_intent(
        decision=addition_decision,
        security_id=entry.security_id,
        broker_symbol="NVDA",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("2"),
        status=ActionStatus.INTENDED,
        reservation_price=Decimal("505"),
        reservation_price_basis="policy_limit",
        reservation_stop_price=Decimal("480"),
        risk_per_unit=Decimal("25"),
        risk_basis="tightened_policy_stop_distance",
    )
    store.record_action_intent(addition, expected_version=None)
    manager.record_policy_cumulative_fill(
        addition.logical_action_id,
        attempt_number=1,
        side="buy",
        broker_order_id="broker-addition-lifecycle",
        client_order_id="client-addition-lifecycle",
        cumulative_quantity=Decimal("2"),
        average_fill_price=Decimal("505"),
    )
    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=confirmed_stop(order_id="stop-addition", qty=22.0, price=480.0),
    ) as ensure_addition:
        stronger = manager.reconcile_policy_protection(
            addition.logical_action_id,
            workflow_id=workflow.workflow_id,
            average_entry_price=Decimal("501.3636363636"),
            entry_order_ids={"broker-opening-lifecycle", "broker-addition-lifecycle"},
        )
    assert stronger.success is True
    assert ensure_addition.call_args.kwargs["stop_price_override"] == 480.0
    assert store.load_holding_episode(holding.holding_episode_id).remaining_quantity == Decimal("22")

    sale_decision = DecisionIdentity.build(
        deployment=entry.decision.deployment_identity,
        clock=entry.decision.clock,
        snapshot_sha256="c" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(
        sale_decision,
        policy_payload={"fixture": "partial-sale"},
        guard_payload={"outcome": "allow"},
        effective_action_payload={"quantity": "5"},
    )
    sale = build_action_intent(
        decision=sale_decision,
        security_id=entry.security_id,
        broker_symbol="NVDA",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("5"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("22"),
        fraction_of_original_quantity=Decimal("0.25"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(sale, expected_version=None)
    manager.record_policy_cumulative_fill(
        sale.logical_action_id,
        attempt_number=1,
        side="sell",
        broker_order_id="broker-sale-lifecycle",
        client_order_id="client-sale-lifecycle",
        cumulative_quantity=Decimal("3"),
        average_fill_price=Decimal("520"),
    )
    after_sale = store.load_holding_episode(holding.holding_episode_id)
    assert after_sale.remaining_quantity == Decimal("19")
    terminal_sale = store.confirm_order_terminal(
        sale.logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.CANCELLED,
        expected_action_version=store.load_action_projection(sale.logical_action_id).state_version,
        expected_holding_version=after_sale.state_version,
        observed_at=datetime.now(UTC),
    )
    resolved_sale = store.record_explicit_action_resolution(
        sale.logical_action_id,
        resolution_reason="Fake broker confirms the unfilled scale-out remainder was cancelled",
        expected_action_version=terminal_sale.state_version,
        expected_holding_version=store.load_holding_episode(holding.holding_episode_id).state_version,
        observed_at=datetime.now(UTC),
    )
    assert resolved_sale.status is ActionStatus.RESOLVED
    assert store.load_holding_episode(holding.holding_episode_id).remaining_quantity == Decimal("19")

    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=ProtectiveStopResult(
            False,
            "",
            "NVDA",
            19.0,
            480.0,
            "submission_unknown",
            error="simulated lost acknowledgement",
            client_order_id="client-stop-pending",
        ),
    ):
        failed = manager.reconcile_policy_protection(
            sale.logical_action_id,
            workflow_id=workflow.workflow_id,
            average_entry_price=Decimal("501.3636363636"),
        )
    assert failed.success is False
    pending_action_id = store.load_holding_episode(holding.holding_episode_id).proposed_stop_action_id
    assert pending_action_id is not None

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    restarted_manager = OrderManager(paper=True, policy_store=restarted_store)
    with patch(
        "core.order_execution.ensure_protective_stop",
        return_value=confirmed_stop(order_id="stop-after-restart", qty=19.0, price=480.0),
    ) as ensure_restart:
        recovered = restarted_manager.reconcile_policy_protection(
            entry.logical_action_id,
            workflow_id=workflow.workflow_id,
            average_entry_price=Decimal("501.3636363636"),
        )
    assert recovered.success is True
    assert ensure_restart.call_args.kwargs["qty"] == 19.0
    assert ensure_restart.call_args.kwargs["stop_price_override"] == 480.0
    final_holding = restarted_store.load_holding_episode(holding.holding_episode_id)
    assert final_holding.proposed_stop_action_id == pending_action_id
    assert final_holding.confirmed_stop_action_id == pending_action_id
    assert final_holding.remaining_quantity == Decimal("19")
    assert final_holding.confirmed_protective_stop_price == Decimal("480")


def test_ensure_protective_stop_forwards_the_durable_policy_price() -> None:
    expected = ProtectiveStopResult(
        True,
        "policy-stop-order",
        "NVDA",
        4.0,
        465.0,
        "reused",
        client_order_id="policy-stop-client",
    )
    with (
        patch("core.order_execution.get_open_orders", return_value=[]),
        patch("core.order_execution.reconcile_symbol_after_exit_failure", return_value=expected) as reconcile,
        patch("core.order_execution.get_workflow", return_value=None),
    ):
        result = ensure_protective_stop(
            symbol="NVDA",
            qty=4.0,
            fill_price=501.0,
            workflow_id="policy-workflow",
            stop_price_override=465.0,
        )

    assert result is expected
    assert reconcile.call_args.kwargs["stop_price_override"] == 465.0


def _stop_order(order_id: str, *, stop_price: float, quantity: float, client_order_id: str):
    return SimpleNamespace(
        id=order_id,
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty=str(quantity),
        filled_qty="0",
        stop_price=str(stop_price),
        client_order_id=client_order_id,
    )


def test_policy_reconciliation_replaces_only_its_exact_stale_stop() -> None:
    workflow_id = "cslm-nvda-policy102"
    base_client_id = build_stop_client_order_id(workflow_id)
    stale = _stop_order("stop-old", stop_price=465.0, quantity=4.0, client_order_id=base_client_id)
    replacement = _stop_order("stop-new", stop_price=480.0, quantity=4.0, client_order_id="")
    position = PositionSummary("NVDA", 4.0, 501.0, 510.0, 0.02)
    workflow = SimpleNamespace(
        workflow_id=workflow_id,
        mark_protective_stop=lambda **_kwargs: None,
    )
    samples = [(position, [stale]), (position, []), (position, [replacement])]

    def submit(**kwargs):
        replacement.client_order_id = kwargs["client_order_id"]
        return OrderResult(
            True,
            "stop-new",
            "NVDA",
            "sell",
            kwargs["qty"],
            client_order_id=kwargs["client_order_id"],
        )

    with (
        patch("core.order_execution._latest_unknown_stop_submission", return_value=(workflow, None, "")),
        patch("core.order_execution._sample_stable_symbol_state", side_effect=samples),
        patch("core.order_execution.get_execution_store", return_value=SimpleNamespace(load_pending_submission_intents=lambda **_: [])),
        patch("core.order_execution._cancel_order_ids_verified", return_value=1) as cancel,
        patch("core.order_execution.submit_stop_loss", side_effect=submit) as broker_submit,
    ):
        result = reconcile_symbol_after_exit_failure(
            "NVDA",
            workflow_id=workflow_id,
            stop_price_override=480.0,
            minimum_position_qty=4.0,
        )

    assert result.success is True
    assert result.action == "submitted"
    assert result.stop_price == 480.0
    cancel.assert_called_once_with("NVDA", {"stop-old"})
    broker_submit.assert_called_once()
    assert broker_submit.call_args.kwargs["stop_price"] == 480.0


def test_policy_reconciliation_keeps_existing_stop_when_cancel_is_unresolved() -> None:
    workflow_id = "cslm-nvda-policy102"
    stale = _stop_order(
        "stop-old",
        stop_price=465.0,
        quantity=4.0,
        client_order_id=build_stop_client_order_id(workflow_id),
    )
    position = PositionSummary("NVDA", 4.0, 501.0, 510.0, 0.02)
    workflow = SimpleNamespace(
        workflow_id=workflow_id,
        mark_protective_stop=lambda **_kwargs: None,
    )
    with (
        patch("core.order_execution._latest_unknown_stop_submission", return_value=(workflow, None, "")),
        patch("core.order_execution._sample_stable_symbol_state", return_value=(position, [stale])),
        patch("core.order_execution.get_execution_store", return_value=SimpleNamespace(load_pending_submission_intents=lambda **_: [])),
        patch("core.order_execution._cancel_order_ids_verified", side_effect=RuntimeError("cancel remains pending")),
        patch("core.order_execution.submit_stop_loss") as broker_submit,
    ):
        result = reconcile_symbol_after_exit_failure(
            "NVDA",
            workflow_id=workflow_id,
            stop_price_override=480.0,
            minimum_position_qty=4.0,
        )

    assert result.success is False
    assert result.action == "policy_stop_replace_cancel_failed"
    assert result.order_id == "stop-old"
    broker_submit.assert_not_called()


def _open_policy_position_for_sell_callback(store, action, manager: OrderManager):
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-entry-sell-callback", "NVDA", "buy", 20.0),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
    confirmed_stop = ProtectiveStopResult(
        True,
        "broker-stop-sell-callback",
        "NVDA",
        20.0,
        465.0,
        "submitted",
        client_order_id="client-stop-sell-callback",
    )
    with patch("core.order_execution.ensure_protective_stop", return_value=confirmed_stop):
        manager.handle_fill(
            symbol="NVDA",
            broker_order_id="broker-entry-sell-callback",
            client_order_id=outcome.workflow_id,
            side="buy",
            filled_qty=20.0,
            fill_price=501.0,
            order_type="limit",
        )
    workflow = get_workflow(outcome.workflow_id)
    assert workflow is not None
    return workflow


@pytest.mark.parametrize(
    ("partial", "filled_qty", "broker_qty"),
    [(True, 3.0, 17.0), (False, 20.0, 0.0)],
)
def test_public_policy_buy_workflow_market_sell_updates_legacy_and_canonical_positions(
    action_store,
    partial: bool,
    filled_qty: float,
    broker_qty: float,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    workflow = _open_policy_position_for_sell_callback(store, action, manager)
    pending_addition_id = None
    if partial:
        holding_before = store.load_holding_episode_for_action(action.logical_action_id)
        addition_decision = DecisionIdentity.build(
            deployment=action.decision.deployment_identity,
            clock=action.decision.clock,
            snapshot_sha256="d" * 64,
            category=DecisionCategory.ADDITION,
            subject_type=DecisionSubjectType.HOLDING,
            subject_id=holding_before.holding_episode_id,
        )
        store.record_decision(
            addition_decision,
            policy_payload={"fixture": "pending-addition-during-guard-sell"},
            guard_payload={"outcome": "allow"},
            effective_action_payload={"quantity": "2"},
        )
        addition = build_action_intent(
            decision=addition_decision,
            security_id=action.security_id,
            broker_symbol="NVDA",
            role=ActionRole.ADDITION,
            side=OrderSide.BUY,
            requested_quantity=Decimal("2"),
            holding_episode_id=holding_before.holding_episode_id,
            status=ActionStatus.INTENDED,
            reservation_price=Decimal("505"),
            reservation_price_basis="policy_limit",
            reservation_stop_price=Decimal("480"),
            risk_per_unit=Decimal("25"),
            risk_basis="tightened_policy_stop_distance",
        )
        store.record_action_intent(addition, expected_version=None)
        pending_addition_id = addition.logical_action_id
    exit_client_id = build_exit_client_order_id(workflow.workflow_id)
    with (
        patch("core.order_manager.cancel_open_orders_verified"),
        patch(
            "core.order_manager.close_position",
            return_value=OrderResult(
                True,
                "broker-guard-market-sell",
                "NVDA",
                "sell",
                20.0,
                client_order_id=exit_client_id,
            ),
        ),
    ):
        submitted = manager.submit_exit("NVDA", exit_reason="execution guard: residual protection")
    assert submitted.success is True

    broker_position = (
        PositionSummary("NVDA", broker_qty, 501.0, 510.0, 0.01)
        if broker_qty > 0
        else None
    )
    with (
        patch.object(
            manager,
            "_wait_for_sell_position_sync",
            return_value=(broker_position, []),
        ),
        patch.object(
            manager,
            "reconcile_policy_protection",
            return_value=ProtectiveStopResult(
                True,
                "broker-stop-after-guard-sell",
                "NVDA",
                broker_qty,
                465.0,
                "reused",
                client_order_id="client-stop-after-guard-sell",
            ),
        ),
        patch("core.order_manager.notify_sell_filled", return_value=True),
    ):
        callback = manager.handle_partial_fill if partial else manager.handle_fill
        callback(
            symbol="NVDA",
            broker_order_id="broker-guard-market-sell",
            client_order_id=exit_client_id,
            side="sell",
            filled_qty=filled_qty,
            fill_price=520.0,
            order_type="market",
        )

    holding = store.load_holding_episode_for_action(action.logical_action_id)
    legacy_position = get_execution_store().load_active_position("NVDA")
    assert holding.remaining_quantity == Decimal(str(broker_qty))
    if broker_qty:
        assert legacy_position is not None
        assert float(legacy_position["qty"]) == broker_qty
    else:
        assert legacy_position is None
    if pending_addition_id is None:
        assert holding.policy_flags == ()
    else:
        assert holding.pending_action_ids == (pending_addition_id,)
    with store._transaction(write=False) as conn:
        guard_action_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT logical_action_id FROM policy_state_actions WHERE role=? AND side=?",
                (ActionRole.CLOSE.value, OrderSide.SELL.value),
            ).fetchall()
        ]
    guard_actions = [store.load_action_projection(action_id) for action_id in guard_action_ids]
    assert len(guard_actions) == 1
    assert guard_actions[0].confirmed_quantity == Decimal(str(filled_qty))
    assert store.load_decision_record(guard_actions[0].decision_id).decision.subject_id.startswith(
        "execution-derived:guard-sell:"
    )

    if partial:
        replay_manager = manager
        for _ in range(2):
            if _ == 1:
                clear_workflow_registry()
                store = PolicyExecutionStateStore(
                    store.db_path, store_identity=store.store_identity
                )
                replay_manager = OrderManager(paper=True, policy_store=store)
            with (
                patch.object(
                    replay_manager,
                    "_wait_for_sell_position_sync",
                    return_value=(PositionSummary("NVDA", 17.0, 501.0, 510.0, 0.01), []),
                ),
                patch.object(
                    replay_manager,
                    "reconcile_policy_protection",
                    return_value=ProtectiveStopResult(
                        True,
                        "broker-stop-after-guard-sell",
                        "NVDA",
                        17.0,
                        465.0,
                        "reused",
                        client_order_id="client-stop-after-guard-sell",
                    ),
                ),
            ):
                replay_manager.handle_partial_fill(
                    symbol="NVDA",
                    broker_order_id="broker-guard-market-sell",
                    client_order_id=exit_client_id,
                    side="sell",
                    filled_qty=filled_qty,
                    fill_price=520.0,
                    order_type="market",
                )
        replayed_holding = store.load_holding_episode_for_action(action.logical_action_id)
        assert replayed_holding.remaining_quantity == Decimal("17")
        assert replayed_holding.pending_action_ids == (pending_addition_id,)


def test_public_policy_buy_workflow_unknown_market_sell_is_retained_and_blocks_reconciliation(
    action_store,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    workflow = _open_policy_position_for_sell_callback(store, action, manager)
    workflow.repair_order_reference(
        broker_order_id="broker-unknown-guard-sell",
        client_order_id="malformed-client-order-link",
        order_role="sell_order",
    )
    with (
        patch.object(manager, "_wait_for_sell_position_sync", return_value=(None, [])),
        pytest.raises(RuntimeError, match="retained with unresolved execution facts"),
    ):
        manager.handle_fill(
            symbol="NVDA",
            broker_order_id="broker-unknown-guard-sell",
            client_order_id="malformed-client-order-link",
            side="sell",
            filled_qty=20.0,
            fill_price=520.0,
            order_type="market",
        )

    holding = store.load_holding_episode_for_action(action.logical_action_id)
    assert holding.remaining_quantity == Decimal("20")
    flags = dict(holding.policy_flags)
    assert flags["position_reconciliation_required"].startswith(
        "non-STOP SELL fill has no unique durable exit intent"
    )
    evidence = [value for key, value in holding.policy_flags if key.startswith("unresolved_execution_sell_")]
    assert len(evidence) == 1
    assert "broker-unknown-guard-sell" in evidence[0]
    assert get_execution_store().load_active_position("NVDA") is None
    workflow = get_workflow(workflow.workflow_id)
    assert workflow is not None
    assert any(
        transition.event == "sell_fill_received"
        and transition.details["broker_order_id"] == "broker-unknown-guard-sell"
        for transition in workflow.transitions
    )


def test_public_genuine_policy_scale_out_sell_remains_on_its_authored_action(
    action_store,
) -> None:
    store, entry_action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    _open_policy_position_for_sell_callback(store, entry_action, manager)
    holding = store.load_holding_episode_for_action(entry_action.logical_action_id)
    decision = DecisionIdentity.build(
        deployment=entry_action.decision.deployment_identity,
        clock=entry_action.decision.clock,
        snapshot_sha256="e" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(
        decision,
        policy_payload={"fixture": "genuine-policy-scale-out"},
        guard_payload={"outcome": "allow"},
        effective_action_payload={"quantity": "5"},
    )
    sale_action = build_action_intent(
        decision=decision,
        security_id=entry_action.security_id,
        broker_symbol="NVDA",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("5"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("20"),
        fraction_of_original_quantity=Decimal("0.25"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(sale_action, expected_version=None)
    sale_workflow = create_entry_workflow(
        _plan(), signal_payload={"logical_action_id": sale_action.logical_action_id}
    )
    sale_workflow.repair_order_reference(
        broker_order_id="broker-genuine-policy-scale-out",
        client_order_id="client-genuine-policy-scale-out",
        order_role="sell_order",
    )
    with (
        patch.object(
            manager,
            "_wait_for_sell_position_sync",
            return_value=(PositionSummary("NVDA", 17.0, 501.0, 510.0, 0.01), []),
        ),
        patch.object(
            manager,
            "reconcile_policy_protection",
            return_value=ProtectiveStopResult(
                True,
                "broker-stop-after-policy-scale-out",
                "NVDA",
                17.0,
                465.0,
                "reused",
                client_order_id="client-stop-after-policy-scale-out",
            ),
        ),
    ):
        manager.handle_partial_fill(
            symbol="NVDA",
            broker_order_id="broker-genuine-policy-scale-out",
            client_order_id="client-genuine-policy-scale-out",
            side="sell",
            filled_qty=3.0,
            fill_price=520.0,
            order_type="market",
        )

    sale_projection = store.load_action_projection(sale_action.logical_action_id)
    updated_holding = store.load_holding_episode_for_action(entry_action.logical_action_id)
    assert sale_projection.status is ActionStatus.PARTIALLY_FILLED
    assert sale_projection.confirmed_quantity == Decimal("3")
    assert updated_holding.remaining_quantity == Decimal("17")


@pytest.mark.parametrize(
    ("failed_action", "uncertain"),
    [("submit_failed", False), ("submission_unknown", True)],
)
def test_public_policy_buy_fill_classifies_first_stop_failure_before_owned_safety_action(
    action_store,
    failed_action: str,
    uncertain: bool,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    with (
        patch(
            "core.order_manager.submit_bracket_buy",
            return_value=OrderResult(True, "broker-entry-first-stop", "NVDA", "buy", 20.0),
        ),
        patch("core.order_manager.notify_entry_submitted", return_value=True),
    ):
        outcome = manager.submit_policy_entry(
            _plan(), logical_action_id=action.logical_action_id, **_entry_guard_kwargs()
        )
    failure = ProtectiveStopResult(
        False,
        "",
        "NVDA",
        20.0,
        465.0,
        failed_action,
        error="fake broker first STOP outcome",
        client_order_id="client-first-stop-failure",
        outcome_uncertain=uncertain,
    )
    with (
        patch("core.order_execution.ensure_protective_stop", return_value=failure),
        patch.object(
            manager,
            "_submit_exit_locked",
            return_value=OrderResult(True, "broker-owned-safety-exit", "NVDA", "sell", 20.0),
        ) as owned_safety,
        patch("core.order_manager.cancel_open_orders_verified") as cancel_orders,
    ):
        if uncertain:
            with pytest.raises(RuntimeError, match="Safety remains unproven for NVDA"):
                manager.handle_fill(
                    symbol="NVDA",
                    broker_order_id="broker-entry-first-stop",
                    client_order_id=outcome.workflow_id,
                    side="buy",
                    filled_qty=20.0,
                    fill_price=501.0,
                    order_type="limit",
                )
        else:
            manager.handle_fill(
                symbol="NVDA",
                broker_order_id="broker-entry-first-stop",
                client_order_id=outcome.workflow_id,
                side="buy",
                filled_qty=20.0,
                fill_price=501.0,
                order_type="limit",
            )

    if uncertain:
        owned_safety.assert_not_called()
        cancel_orders.assert_not_called()
    else:
        owned_safety.assert_called_once()
        assert owned_safety.call_args.kwargs["exit_reason"] == "protective stop reconciliation failed"


@pytest.mark.parametrize(
    ("failed_action", "should_submit_safety_exit"),
    [("submit_failed", True), ("policy_pending_exit", False)],
)
def test_public_policy_order_failure_classifies_protection_before_safety_exit(
    action_store,
    failed_action: str,
    should_submit_safety_exit: bool,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    workflow = _open_policy_position_for_sell_callback(store, action, manager)
    get_execution_store().clear_active_position("NVDA")
    exit_client_id = build_exit_client_order_id(workflow.workflow_id)
    workflow.mark_exit_submission_intent(
        exit_reason="test terminal exit failure",
        client_order_id=exit_client_id,
    )
    workflow.mark_exit_order_submitted(
        exit_reason="test terminal exit failure",
        broker_order_id="broker-terminal-exit",
        requested_quantity=20.0,
    )
    failed_protection = ProtectiveStopResult(
        False,
        "broker-stop-order-failure",
        "NVDA",
        20.0,
        465.0,
        failed_action,
        error="fake broker protection outcome",
        client_order_id="client-stop-order-failure",
    )
    with (
        patch(
            "core.order_manager._sample_stable_symbol_state",
            return_value=(PositionSummary("NVDA", 20.0, 501.0, 510.0, 0.01), []),
        ),
        patch.object(
            manager, "reconcile_policy_protection", return_value=failed_protection
        ),
        patch.object(
            manager,
            "_submit_exit_locked",
            return_value=OrderResult(True, "broker-owned-order-failure-exit", "NVDA", "sell", 20.0),
        ) as safety_exit,
    ):
        if should_submit_safety_exit:
            manager.handle_order_failure(
                symbol="NVDA",
                broker_order_id="broker-terminal-exit",
                client_order_id=exit_client_id,
                side="sell",
                order_type="market",
                status="canceled",
            )
        else:
            with pytest.raises(RuntimeError, match="Safety remains unproven for NVDA"):
                manager.handle_order_failure(
                    symbol="NVDA",
                    broker_order_id="broker-terminal-exit",
                    client_order_id=exit_client_id,
                    side="sell",
                    order_type="market",
                    status="canceled",
                )

    if should_submit_safety_exit:
        safety_exit.assert_called_once()
    else:
        safety_exit.assert_not_called()


def test_public_policy_sell_keeps_verified_old_stop_without_market_exit_when_replacement_fails(
    action_store,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    workflow = _open_policy_position_for_sell_callback(store, action, manager)
    exit_client_id = build_exit_client_order_id(workflow.workflow_id)
    with (
        patch("core.order_manager.cancel_open_orders_verified"),
        patch(
            "core.order_manager.close_position",
            return_value=OrderResult(
                True,
                "broker-guard-replace-sell",
                "NVDA",
                "sell",
                20.0,
                client_order_id=exit_client_id,
            ),
        ),
    ):
        manager.submit_exit("NVDA", exit_reason="execution guard: partial exit")

    old_stop_failure = ProtectiveStopResult(
        False,
        "broker-stop-old-valid",
        "NVDA",
        17.0,
        470.0,
        "policy_stronger_existing_stop_unadopted",
        error="the exact old workflow STOP remains stronger and quantity-matched",
        client_order_id="client-stop-old-valid",
        prior_protection_retained=True,
    )
    with (
        patch.object(
            manager,
            "_wait_for_sell_position_sync",
            return_value=(PositionSummary("NVDA", 17.0, 501.0, 510.0, 0.01), []),
        ),
        patch.object(manager, "reconcile_policy_protection", return_value=old_stop_failure),
        patch.object(manager, "_submit_exit_locked") as emergency_exit,
    ):
        with pytest.raises(RuntimeError, match="existing protective stop remains active"):
            manager.handle_partial_fill(
                symbol="NVDA",
                broker_order_id="broker-guard-replace-sell",
                client_order_id=exit_client_id,
                side="sell",
                filled_qty=3.0,
                fill_price=520.0,
                order_type="market",
            )
    emergency_exit.assert_not_called()


def test_guard_sell_accounting_rolls_back_decision_alias_receipt_and_holding_together(
    action_store,
) -> None:
    store, action = action_store
    manager = OrderManager(paper=True, policy_store=store)
    _open_policy_position_for_sell_callback(store, action, manager)
    holding = store.load_holding_episode_for_action(action.logical_action_id)
    provider_id = manager._policy_provider_id(store.load_action_projection(action.logical_action_id))
    bridge = PolicyProtectionBridge(store, provider_id=provider_id)

    def persisted_guard_sell_counts() -> tuple[int, int, int, int]:
        with store._transaction(write=False) as conn:
            decision_count = conn.execute(
                "SELECT COUNT(*) FROM policy_state_decisions WHERE policy_payload_json LIKE ?",
                ("%execution_derived_guard_sell_order_v1%",),
            ).fetchone()[0]
            action_count = conn.execute(
                "SELECT COUNT(*) FROM policy_state_actions WHERE role=? AND side=?",
                (ActionRole.CLOSE.value, OrderSide.SELL.value),
            ).fetchone()[0]
            alias_count = conn.execute(
                "SELECT COUNT(*) FROM policy_state_order_reference_aliases WHERE external_order_id IN (?, ?)",
                ("broker-rollback-guard-sell", "client-rollback-guard-sell"),
            ).fetchone()[0]
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM policy_state_fill_receipts WHERE fill_event_id LIKE ?",
                ("guard-sell-fill:%",),
            ).fetchone()[0]
        return decision_count, action_count, alias_count, receipt_count

    with (
        patch.object(
            store,
            "_record_cumulative_fill_in_transaction",
            side_effect=RuntimeError("injected canonical fill failure"),
        ),
        pytest.raises(RuntimeError, match="injected canonical fill failure"),
    ):
        bridge.record_execution_guard_sell_fill(
            action.logical_action_id,
            workflow_id="workflow-rollback-guard-sell",
            broker_order_id="broker-rollback-guard-sell",
            client_order_id="client-rollback-guard-sell",
            order_type="market",
            execution_cause="execution guard: rollback fixture",
            requested_quantity=Decimal("20"),
            cumulative_quantity=Decimal("3"),
            average_fill_price=Decimal("520"),
            expected_holding_version=int(holding.state_version or 0),
        )

    assert persisted_guard_sell_counts() == (0, 0, 0, 0)
    unchanged = store.load_holding_episode_for_action(action.logical_action_id)
    assert unchanged.state_version == holding.state_version
    assert unchanged.remaining_quantity == Decimal("20")
    assert unchanged.policy_flags == holding.policy_flags
