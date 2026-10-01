from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from core.execution_store import ExecutionStore
from core.policy_execution_state import (
    ActionRole,
    ActionStatus,
    ActionAttemptStatus,
    DecisionCategory,
    DecisionClock,
    DecisionIdentity,
    DecisionSubjectType,
    OrderSide,
    PolicyDeploymentIdentity,
    PortfolioStateSnapshot,
    build_action_intent,
)
from core.policy_execution_store import (
    ConcurrentStateUpdateError,
    FillReceiptConflictError,
    PolicyExecutionStateStore,
)


UTC = timezone.utc


def _clock(session: date = date(2026, 9, 30)) -> DecisionClock:
    next_session = date.fromordinal(session.toordinal() + 1)
    return DecisionClock(
        exchange_id="XNYS",
        decision_session=session,
        as_of_cutoff_at=datetime.combine(session, datetime.min.time(), tzinfo=UTC).replace(hour=20),
        next_execution_session=next_session,
        account_valuation_session=next_session,
        account_valuation_at=datetime.combine(next_session, datetime.min.time(), tzinfo=UTC).replace(hour=13, minute=30),
    )


def _deployment(
    *,
    revision: str = "ab385d792e19ff6db39d87f1123f47f660fc1e1d",
    store_identity: str = "synthetic-policy-store-v1",
) -> PolicyDeploymentIdentity:
    return PolicyDeploymentIdentity(
        policy_artifact_id=f"fixed-policy:{revision}",
        capability_manifest_id="manifest:fixture-v1",
        policy_interface_version="3",
        feature_contract_id="feature-contract-v1",
        feature_calculator_id="calculator-v1",
        source_revision=revision,
        runtime_identity="runtime:recorded-fixture",
        execution_profile_id="paper-profile-v1",
        paper_account_environment_id="synthetic-paper-account",
        store_identity=store_identity,
    )


def _decision(
    deployment: PolicyDeploymentIdentity,
    *,
    session: date = date(2026, 9, 30),
    snapshot: str = "a" * 64,
    category: DecisionCategory = DecisionCategory.ENTRY,
    subject_type: DecisionSubjectType = DecisionSubjectType.SECURITY,
    subject_id: str = "FIGI-BB1234",
) -> DecisionIdentity:
    return DecisionIdentity.build(
        deployment=deployment,
        clock=_clock(session),
        snapshot_sha256=snapshot,
        category=category,
        subject_type=subject_type,
        subject_id=subject_id,
    )


def _open_store(tmp_path: Path, *, store_identity: str = "synthetic-policy-store-v1") -> PolicyExecutionStateStore:
    path = tmp_path / "legacy-execution.sqlite3"
    legacy = ExecutionStore(str(path))
    legacy.upsert_workflow_snapshot(
        workflow_id="legacy-workflow",
        symbol="ACME",
        state="created",
        broker_order_id="",
        entry_plan=None,
        created_at_utc="2026-09-29T20:00:00Z",
        updated_at_utc="2026-09-29T20:00:00Z",
    )
    return PolicyExecutionStateStore(path, store_identity=store_identity)


def _legacy_snapshot(path: Path) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]]:
    with sqlite3.connect(path) as conn:
        schema = tuple(
            sorted(
                (str(name), str(sql))
                for name, sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND (name LIKE 'workflow_%' OR name='active_positions')"
                )
            )
        )
        data = []
        for name, _ in schema:
            rows = tuple(tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'))
            data.append((name, rows))
    return schema, tuple(data)


def _entry_intent(deployment: PolicyDeploymentIdentity, *, session: date = date(2026, 9, 30)):
    decision = _decision(deployment, session=session)
    return build_action_intent(
        decision=decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("100"),
        reservation_price=Decimal("50"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("45"),
        risk_per_unit=Decimal("5"),
        risk_basis="entry_to_protective_stop",
    )


def _record_decision(store: PolicyExecutionStateStore, intent) -> None:
    store.record_decision(
        intent.decision,
        policy_payload={"requested_role": intent.role.value},
        guard_payload={"outcome": "allow_fixture"},
        effective_action_payload={"security_id": intent.security_id},
    )


def _record_fill(
    store: PolicyExecutionStateStore,
    intent,
    *,
    event_id: str,
    cumulative_quantity: str,
    cumulative_notional: str,
    expected_action_version: int,
    expected_holding_version: int | None,
    attempt_number: int = 1,
):
    return store.record_cumulative_fill(
        intent.logical_action_id,
        attempt_number,
        provider_id="recorded-fixture-provider",
        fill_event_id=event_id,
        cumulative_quantity=Decimal(cumulative_quantity),
        cumulative_notional=Decimal(cumulative_notional),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
        expected_action_version=expected_action_version,
        expected_holding_version=expected_holding_version,
    )


def test_additive_migration_and_empty_schema_rollback_preserve_legacy_rows(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    before = _legacy_snapshot(store.db_path)

    store.migrate()
    assert before == _legacy_snapshot(store.db_path)
    with sqlite3.connect(store.db_path) as conn:
        new_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0].startswith("policy_state_")
        }
        assert "policy_state_schema_migrations" in new_tables
        assert "policy_state_actions" in new_tables
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_snapshots'"
        ).fetchone() is not None

    store.rollback_schema_v1()
    assert before == _legacy_snapshot(store.db_path)


def test_migration_failure_after_intermediate_ddl_rolls_back_all_policy_tables(tmp_path: Path, monkeypatch) -> None:
    from core import policy_execution_store as store_module

    store = _open_store(tmp_path)
    original = store_module.SCHEMA_V1_STATEMENTS
    monkeypatch.setattr(
        store_module,
        "SCHEMA_V1_STATEMENTS",
        original[:2] + ("CREATE TABLE policy_state_injected(value TEXT)", "INVALID MIGRATION SQL"),
    )

    with pytest.raises(sqlite3.Error):
        store.migrate()
    with sqlite3.connect(store.db_path) as conn:
        policy_tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0].startswith("policy_state_")
        ]
    assert policy_tables == []
    assert "legacy-workflow" in repr(_legacy_snapshot(store.db_path))


def test_store_identity_is_bound_to_the_explicit_database(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    reopened = PolicyExecutionStateStore(store.db_path, store_identity="another-store")
    with pytest.raises(ValueError, match="store identity"):
        reopened.migrate()


def test_deployment_pointer_rollback_keeps_open_holding_and_action_generation_pinned(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    generation_a = _deployment()
    generation_b = _deployment(revision="b" * 40)
    store.register_deployment_identity(generation_a, lifecycle="prepared", handler_identity="handler-a", guard_id="guard-v1")
    store.register_deployment_identity(generation_b, lifecycle="prepared", handler_identity="handler-b", guard_id="guard-v1")
    first_pointer_version = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=None,
        expected_pointer_version=None,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-ready-a",
        outgoing_entries_reconciled=True,
    )
    second_pointer_version = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_a.deployment_generation_id,
        expected_pointer_version=first_pointer_version,
        new_generation_id=generation_b.deployment_generation_id,
        readiness_evidence_ref="synthetic-ready-b",
        outgoing_entries_reconciled=True,
    )

    entry = _entry_intent(generation_b)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="entry-fill-1",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    exit_decision = _decision(
        generation_b,
        session=date(2026, 10, 1),
        snapshot="c" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(exit_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    pending_exit = build_action_intent(
        decision=exit_decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(pending_exit, expected_version=None)

    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_b.deployment_generation_id,
        expected_pointer_version=second_pointer_version,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-rollback-a",
        outgoing_entries_reconciled=True,
    )

    assert store.load_active_generation(generation_a.paper_account_environment_id) == generation_a.deployment_generation_id
    assert store.load_holding_episode(holding.holding_episode_id).deployment_generation_id == generation_b.deployment_generation_id
    projection = store.load_action_projection(pending_exit.logical_action_id)
    assert projection.deployment_generation_id == generation_b.deployment_generation_id
    assert projection.status is ActionStatus.INTENDED


def test_decision_slot_and_cross_session_tier_uniqueness_are_durable(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    first = _entry_intent(deployment)
    _record_decision(store, first)
    with pytest.raises(ValueError, match="decision slot"):
        store.record_decision(
            replace(first.decision, snapshot_sha256="b" * 64),
            policy_payload={},
            guard_payload={},
            effective_action_payload={},
        )
    another_security = _decision(deployment, subject_id="FIGI-CC5678")
    store.record_decision(another_security, policy_payload={}, guard_payload={}, effective_action_payload={})

    opening = first
    store.record_action_intent(opening, expected_version=None)
    _record_fill(
        store,
        opening,
        event_id="opening-fill-for-tier",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(opening.logical_action_id)
    tier_decision = _decision(
        deployment,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(tier_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    tier = build_action_intent(
        decision=tier_decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(tier, expected_version=None)

    next_decision = _decision(
        deployment,
        session=date(2026, 10, 1),
        snapshot="d" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(next_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    duplicate_tier = build_action_intent(
        decision=next_decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("25"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("50"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    with pytest.raises(ValueError, match="logical action|tier"):
        store.record_action_intent(duplicate_tier, expected_version=None)


def test_fill_receipt_attempt_action_and_opening_holding_commit_atomically(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    intent = _entry_intent(deployment)
    _record_decision(store, intent)
    store.record_action_intent(intent, expected_version=None)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_policy_action_update BEFORE UPDATE ON policy_state_actions "
            "BEGIN SELECT RAISE(ABORT, 'injected action update failure'); END"
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected action update failure"):
        _record_fill(
            store,
            intent,
            event_id="entry-fill-atomic",
            cumulative_quantity="40",
            cumulative_notional="2000",
            expected_action_version=0,
            expected_holding_version=None,
        )
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_state_fill_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT confirmed_quantity FROM policy_state_actions").fetchone()[0] == "0"
        assert conn.execute("SELECT COUNT(*) FROM policy_state_holdings").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_policy_action_update")
        conn.commit()

    projection = _record_fill(
        store,
        intent,
        event_id="entry-fill-atomic",
        cumulative_quantity="40",
        cumulative_notional="2000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    assert projection.confirmed_quantity == Decimal("40")
    holding = store.load_holding_episode_for_action(intent.logical_action_id)
    assert holding.initial_filled_quantity == Decimal("40")
    assert holding.remaining_quantity == Decimal("40")
    assert holding.addition_count == 0
    assert holding.cost_basis == Decimal("50")

    filled_again = _record_fill(
        store,
        intent,
        event_id="entry-fill-atomic-2",
        cumulative_quantity="60",
        cumulative_notional="3100",
        expected_action_version=1,
        expected_holding_version=1,
    )
    assert filled_again.confirmed_quantity == Decimal("60")
    holding = store.load_holding_episode(holding.holding_episode_id)
    assert holding.initial_filled_quantity == Decimal("40")
    assert holding.opening_later_fills_quantity == Decimal("20")
    assert holding.remaining_quantity == Decimal("60")
    assert holding.addition_count == 0
    assert holding.cost_basis == Decimal("51.66666666666666666666666667")


def test_duplicate_fill_ids_compare_payload_and_cumulative_replays_apply_zero_delta(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    intent = _entry_intent(deployment)
    _record_decision(store, intent)
    store.record_action_intent(intent, expected_version=None)
    first = _record_fill(
        store,
        intent,
        event_id="same-fill",
        cumulative_quantity="40",
        cumulative_notional="2000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    duplicate = _record_fill(
        store,
        intent,
        event_id="same-fill",
        cumulative_quantity="40",
        cumulative_notional="2000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    assert duplicate.confirmed_quantity == first.confirmed_quantity
    with pytest.raises(ValueError, match="fill event.*conflict|payload"):
        _record_fill(
            store,
            intent,
            event_id="same-fill",
            cumulative_quantity="50",
            cumulative_notional="2500",
            expected_action_version=1,
            expected_holding_version=1,
        )
    same_watermark = _record_fill(
        store,
        intent,
        event_id="different-observation-same-watermark",
        cumulative_quantity="40",
        cumulative_notional="2000",
        expected_action_version=1,
        expected_holding_version=1,
    )
    assert same_watermark.confirmed_quantity == Decimal("40")
    assert store.load_holding_episode_for_action(intent.logical_action_id).remaining_quantity == Decimal("40")


def test_same_watermark_monetary_refinement_is_receipt_only(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    intent = _entry_intent(deployment)
    _record_decision(store, intent)
    store.record_action_intent(intent, expected_version=None)
    first = store.record_cumulative_fill(
        intent.logical_action_id,
        1,
        provider_id="recorded-fixture-provider",
        fill_event_id="unknown-value-fill",
        cumulative_quantity=Decimal("40"),
        cumulative_notional=None,
        cumulative_fees=None,
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
        expected_action_version=0,
        expected_holding_version=None,
    )
    refined = store.record_cumulative_fill(
        intent.logical_action_id,
        1,
        provider_id="recorded-fixture-provider",
        fill_event_id="known-value-same-watermark",
        cumulative_quantity=Decimal("40"),
        cumulative_notional=Decimal("2000"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
        expected_action_version=first.state_version,
        expected_holding_version=1,
    )
    assert refined.state_version == first.state_version
    assert store.load_holding_episode_for_action(intent.logical_action_id).cost_basis is None
    with sqlite3.connect(store.db_path) as conn:
        notional, fees = conn.execute(
            "SELECT cumulative_notional, cumulative_fees FROM policy_state_order_attempts WHERE logical_action_id=? AND attempt_number=1",
            (intent.logical_action_id,),
        ).fetchone()
    assert notional is None
    assert fees is None


def test_active_pointer_compare_and_set_serializes_competing_writers(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    base = _deployment()
    second = _deployment(revision="b" * 40)
    third = _deployment(revision="c" * 40)
    for deployment in (base, second, third):
        store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    initial_pointer_version = store.set_active_generation(
        base.paper_account_environment_id,
        expected_generation_id=None,
        expected_pointer_version=None,
        new_generation_id=base.deployment_generation_id,
        readiness_evidence_ref="synthetic-initial-pointer",
        outgoing_entries_reconciled=True,
    )
    barrier = Barrier(2)
    expected_pointer = store.load_active_generation_pointer(base.paper_account_environment_id)
    assert expected_pointer.pointer_version == initial_pointer_version

    def contender(target: PolicyDeploymentIdentity) -> bool:
        writer = PolicyExecutionStateStore(store.db_path, store_identity=base.store_identity)
        barrier.wait()
        try:
            writer.set_active_generation(
                base.paper_account_environment_id,
                expected_generation_id=expected_pointer.active_generation_id,
                expected_pointer_version=expected_pointer.pointer_version,
                new_generation_id=target.deployment_generation_id,
                readiness_evidence_ref=f"synthetic-{target.source_revision[:4]}",
                outgoing_entries_reconciled=True,
            )
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contender, (second, third)))
    assert sorted(outcomes) == [False, True]
    assert store.load_active_generation(base.paper_account_environment_id) in {
        second.deployment_generation_id,
        third.deployment_generation_id,
    }


def test_active_pointer_compare_and_set_rejects_aba_stale_version(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    generation_a = _deployment()
    generation_b = _deployment(revision="b" * 40)
    generation_c = _deployment(revision="c" * 40)
    for deployment in (generation_a, generation_b, generation_c):
        store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")

    version_a1 = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=None,
        expected_pointer_version=None,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-a1",
        outgoing_entries_reconciled=True,
    )
    version_b2 = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_a.deployment_generation_id,
        expected_pointer_version=version_a1,
        new_generation_id=generation_b.deployment_generation_id,
        readiness_evidence_ref="synthetic-b2",
        outgoing_entries_reconciled=True,
    )
    version_a3 = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_b.deployment_generation_id,
        expected_pointer_version=version_b2,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-a3",
        outgoing_entries_reconciled=True,
    )
    assert version_a3 == version_a1 + 2
    with pytest.raises(ConcurrentStateUpdateError, match="changed since it was read"):
        store.set_active_generation(
            generation_a.paper_account_environment_id,
            expected_generation_id=generation_a.deployment_generation_id,
            expected_pointer_version=version_a1,
            new_generation_id=generation_c.deployment_generation_id,
            readiness_evidence_ref="synthetic-stale-a1",
            outgoing_entries_reconciled=True,
        )
    pointer = store.load_active_generation_pointer(generation_a.paper_account_environment_id)
    assert pointer.active_generation_id == generation_a.deployment_generation_id
    assert pointer.pointer_version == version_a3


def test_schema_rollback_refuses_any_durable_policy_state(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    with pytest.raises(ValueError, match="dependent policy state"):
        store.rollback_schema_v1()


def test_unissued_action_can_be_resolved_but_issued_action_waits_for_terminal_evidence(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    intent = _entry_intent(deployment)
    _record_decision(store, intent)
    store.record_action_intent(intent, expected_version=None)

    with pytest.raises(ValueError, match="unissued order intent"):
        store.confirm_order_terminal(
            intent.logical_action_id,
            1,
            terminal_status=ActionAttemptStatus.CANCELLED,
            expected_action_version=0,
            observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
        )
    resolved = store.record_explicit_action_resolution(
        intent.logical_action_id,
        resolution_reason="Synthetic missed-session reconciliation confirms no order was submitted",
        expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    assert resolved.status is ActionStatus.RESOLVED
    assert resolved.resolution_reason == "Synthetic missed-session reconciliation confirms no order was submitted"
    assert resolved.state_version == 1
    late_reference = store.bind_attempt_order_refs(
        intent.logical_action_id,
        1,
        provider_id="recorded-fixture-provider",
        client_order_id="late-discovered-client",
        expected_action_version=resolved.state_version,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
    )
    assert late_reference.status is ActionStatus.RECONCILIATION_REQUIRED
    assert late_reference.resolution_reason == resolved.resolution_reason
    assert late_reference.order_attempts[0].status is ActionAttemptStatus.SUBMITTED

    issued = build_action_intent(
        decision=_decision(deployment, subject_id="FIGI-ISSUED"),
        security_id="FIGI-ISSUED",
        broker_symbol="ISSUED",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        reservation_price=Decimal("5"),
        reservation_price_basis="limit_price",
    )
    _record_decision(store, issued)
    store.record_action_intent(issued, expected_version=None)
    submitted = store.bind_attempt_order_refs(
        issued.logical_action_id,
        1,
        provider_id="recorded-fixture-provider",
        client_order_id="issued-client-order",
        expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="issued order attempts must be terminal"):
        store.record_explicit_action_resolution(
            issued.logical_action_id,
            resolution_reason="Synthetic evidence does not establish an issued order's terminal state",
            expected_action_version=submitted.state_version,
            observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
        )


def test_late_references_reopen_resolved_addition_and_restore_holding_conflict(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    opening = _entry_intent(deployment)
    _record_decision(store, opening)
    store.record_action_intent(opening, expected_version=None)
    _record_fill(
        store,
        opening,
        event_id="resolved-addition-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(opening.logical_action_id)
    addition_decision = _decision(
        deployment,
        session=date(2026, 10, 1),
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    addition = build_action_intent(
        decision=addition_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("2"),
    )
    _record_decision(store, addition)
    store.record_action_intent(addition, expected_version=None)
    pending = store.load_holding_episode(holding.holding_episode_id)
    resolved = store.record_explicit_action_resolution(
        addition.logical_action_id,
        resolution_reason="Synthetic position check confirms no addition order was issued",
        expected_action_version=0,
        expected_holding_version=pending.state_version,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    assert resolved.status is ActionStatus.RESOLVED
    cleared = store.load_holding_episode(holding.holding_episode_id)
    assert cleared.pending_action_ids == ()

    other_decision = _decision(
        deployment,
        session=date(2026, 10, 2),
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    other_action = build_action_intent(
        decision=other_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    _record_decision(store, other_action)
    store.record_action_intent(other_action, expected_version=None)
    with_other_action = store.load_holding_episode(holding.holding_episode_id)
    assert with_other_action.pending_action_ids == (other_action.logical_action_id,)

    reopened = store.bind_attempt_order_refs(
        addition.logical_action_id,
        1,
        provider_id="recorded-late-addition-provider",
        client_order_id="late-addition-client",
        broker_order_id="late-addition-broker",
        expected_action_version=resolved.state_version,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
    )
    assert reopened.status is ActionStatus.RECONCILIATION_REQUIRED
    assert reopened.order_attempts[0].status is ActionAttemptStatus.SUBMITTED
    reopened_holding = store.load_holding_episode(holding.holding_episode_id)
    assert reopened_holding.pending_action_ids == tuple(sorted((other_action.logical_action_id, addition.logical_action_id)))
    assert reopened_holding.state_version == with_other_action.state_version + 1
    assert reopened_holding.remaining_quantity == holding.remaining_quantity
    with sqlite3.connect(store.db_path) as conn:
        history = conn.execute(
            "SELECT event_kind, logical_action_id FROM policy_state_holding_history "
            "WHERE holding_episode_id=? ORDER BY state_version DESC LIMIT 1",
            (holding.holding_episode_id,),
        ).fetchone()
    assert tuple(history) == ("holding_action_reopened_for_reconciliation", addition.logical_action_id)

    competing_decision = _decision(
        deployment,
        session=date(2026, 10, 3),
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    competing = build_action_intent(
        decision=competing_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
    )
    _record_decision(store, competing)
    with pytest.raises(ValueError, match="pending logical action"):
        store.record_action_intent(competing, expected_version=None)

    terminal = store.confirm_order_terminal(
        addition.logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.CANCELLED,
        expected_action_version=reopened.state_version,
        observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
    )
    assert terminal.status is ActionStatus.REMAINDER_READY
    resolved_again = store.record_explicit_action_resolution(
        addition.logical_action_id,
        resolution_reason="Synthetic terminal evidence confirms the discovered order was cancelled without fills",
        expected_action_version=terminal.state_version,
        expected_holding_version=reopened_holding.state_version,
        observed_at=datetime(2026, 10, 1, 13, 34, tzinfo=UTC),
    )
    assert resolved_again.status is ActionStatus.RESOLVED
    after_reopened_resolution = store.load_holding_episode(holding.holding_episode_id)
    assert after_reopened_resolution.pending_action_ids == (other_action.logical_action_id,)
    assert after_reopened_resolution.remaining_quantity == holding.remaining_quantity
    other_resolved = store.record_explicit_action_resolution(
        other_action.logical_action_id,
        resolution_reason="Synthetic session review confirms the separate exit intent was not issued",
        expected_action_version=0,
        expected_holding_version=after_reopened_resolution.state_version,
        observed_at=datetime(2026, 10, 1, 13, 35, tzinfo=UTC),
    )
    assert other_resolved.status is ActionStatus.RESOLVED
    final_holding = store.load_holding_episode(holding.holding_episode_id)
    assert final_holding.pending_action_ids == ()
    assert final_holding.remaining_quantity == holding.remaining_quantity

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    assert restarted.load_action_projection(addition.logical_action_id).status is ActionStatus.RESOLVED
    assert restarted.load_action_projection(other_action.logical_action_id).status is ActionStatus.RESOLVED
    restarted_holding = restarted.load_holding_episode(holding.holding_episode_id)
    assert restarted_holding == final_holding
    assert addition.logical_action_id not in restarted_holding.pending_action_ids


def test_mixed_generation_consistent_read_exposes_action_and_holding_versions(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    generation_a = _deployment()
    generation_b = _deployment(revision="b" * 40)
    for deployment in (generation_a, generation_b):
        store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")

    historical_entry = _entry_intent(generation_a)
    _record_decision(store, historical_entry)
    store.record_action_intent(historical_entry, expected_version=None)
    historical_fill = _record_fill(
        store,
        historical_entry,
        event_id="generation-a-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(historical_entry.logical_action_id)
    assert holding.state_version == 1

    historical_terminal_decision = _decision(generation_a, subject_id="FIGI-UNRELATED")
    store.record_decision(historical_terminal_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    unrelated = build_action_intent(
        decision=historical_terminal_decision,
        security_id="FIGI-UNRELATED",
        broker_symbol="UNRELATED",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    store.record_action_intent(unrelated, expected_version=None)
    store.record_explicit_action_resolution(
        unrelated.logical_action_id,
        resolution_reason="Synthetic session expired before submission",
        expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 13, 35, tzinfo=UTC),
    )

    generation_a_pointer_version = store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=None,
        expected_pointer_version=None,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-generation-a",
        outgoing_entries_reconciled=True,
    )
    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_a.deployment_generation_id,
        expected_pointer_version=generation_a_pointer_version,
        new_generation_id=generation_b.deployment_generation_id,
        readiness_evidence_ref="synthetic-generation-b",
        outgoing_entries_reconciled=True,
    )

    current_entry = _entry_intent(generation_b, session=date(2026, 10, 1))
    _record_decision(store, current_entry)
    store.record_action_intent(current_entry, expected_version=None)
    portfolio = PortfolioStateSnapshot(
        deployment_identity=generation_b,
        clock=_clock(date(2026, 10, 1)),
        source_namespace="synthetic-account-source",
        account_snapshot_id="snapshot-generation-b",
        equity=Decimal("5000"),
        cash=Decimal("0"),
        gross_exposure=Decimal("5000"),
        open_risk=Decimal("500"),
        portfolio_peak_equity=Decimal("5000"),
        last_accepted_session=date(2026, 10, 1),
    )
    store.record_portfolio_snapshot(portfolio)

    restarted_store = PolicyExecutionStateStore(store.db_path, store_identity=generation_b.store_identity)
    read = restarted_store.load_policy_execution_snapshot(
        deployment_generation_id=generation_b.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    assert read.deployment_identity == generation_b
    assert read.portfolio_snapshot == portfolio
    assert tuple(item.holding_episode_id for item in read.holding_episodes) == (holding.holding_episode_id,)
    assert read.holding_episodes[0].deployment_generation_id == generation_a.deployment_generation_id
    assert read.holding_episodes[0].state_version == 1
    projections = {item.logical_action_id: item for item in read.action_projections}
    assert set(projections) == {historical_entry.logical_action_id, current_entry.logical_action_id}
    assert projections[historical_entry.logical_action_id].deployment_generation_id == generation_a.deployment_generation_id
    assert projections[historical_entry.logical_action_id].clock_id == historical_entry.decision.clock.clock_id
    assert projections[historical_entry.logical_action_id].state_version == historical_fill.state_version == 1
    assert projections[current_entry.logical_action_id].status is ActionStatus.INTENDED
    assert projections[current_entry.logical_action_id].state_version == 0

    marked = restarted_store.update_holding_marks(
        holding.holding_episode_id,
        valuation_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
        peak_price=Decimal("55"),
        expected_holding_version=read.holding_episodes[0].state_version,
    )
    assert marked.peak_price == Decimal("55")
    assert marked.state_version == 2


def test_stop_update_replay_uses_durable_intent_and_confirmed_price(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry = _entry_intent(deployment)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="stop-test-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    decision = _decision(
        deployment,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    observed_at = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    intent = store.propose_stop_update(
        holding.holding_episode_id,
        decision=decision,
        stop_price=Decimal("47"),
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    replayed = store.propose_stop_update(
        holding.holding_episode_id,
        decision=decision,
        stop_price=Decimal("47"),
        expected_holding_version=holding.state_version,
        observed_at=observed_at,
    )
    assert replayed == intent
    with pytest.raises(ValueError, match="requested facts"):
        store.propose_stop_update(
            holding.holding_episode_id,
            decision=decision,
            stop_price=Decimal("48"),
            expected_holding_version=holding.state_version,
            observed_at=observed_at,
        )

    proposed_holding = store.load_holding_episode(holding.holding_episode_id)
    confirmed = store.confirm_protective_stop(
        intent,
        stop_price=Decimal("48"),
        client_order_id="stop-client",
        broker_order_id="stop-broker",
        observed_at=datetime(2026, 10, 1, 14, 1, tzinfo=UTC),
        expected_holding_version=proposed_holding.state_version,
    )
    assert confirmed.confirmed_protective_stop_price == Decimal("48")
    assert confirmed.state_version == proposed_holding.state_version + 1
    confirmed_replay = store.confirm_protective_stop(
        intent,
        stop_price=Decimal("48"),
        client_order_id="stop-client",
        broker_order_id="stop-broker",
        observed_at=datetime(2026, 10, 1, 14, 1, tzinfo=UTC),
        expected_holding_version=proposed_holding.state_version,
    )
    assert confirmed_replay == confirmed


def test_protective_stop_can_confirm_partial_entry_exposure_while_residual_stays_pending(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry_decision = _decision(deployment)
    entry = build_action_intent(
        decision=entry_decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        reservation_price=Decimal("50"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("45"),
        risk_per_unit=Decimal("5"),
        risk_basis="entry_to_protective_stop",
    )
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    partial = _record_fill(
        store,
        entry,
        event_id="partial-entry-four-shares",
        cumulative_quantity="4",
        cumulative_notional="200",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    assert partial.status is ActionStatus.PARTIALLY_FILLED
    assert partial.residual_quantity == Decimal("6")
    assert holding.remaining_quantity == Decimal("4")
    assert holding.pending_action_ids == (entry.logical_action_id,)

    stop_decision = _decision(
        deployment,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(stop_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    stop_time = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    stop_intent = store.propose_stop_update(
        holding.holding_episode_id,
        decision=stop_decision,
        stop_price=Decimal("45"),
        expected_holding_version=holding.state_version,
        observed_at=stop_time,
    )
    proposed_holding = store.load_holding_episode(holding.holding_episode_id)
    assert set(proposed_holding.pending_action_ids) == {entry.logical_action_id, stop_intent.logical_action_id}
    confirmed = store.confirm_protective_stop(
        stop_intent,
        stop_price=Decimal("45"),
        client_order_id="stop-client-four-shares",
        broker_order_id="stop-broker-four-shares",
        observed_at=stop_time,
        expected_holding_version=proposed_holding.state_version,
    )
    assert confirmed.remaining_quantity == Decimal("4")
    assert confirmed.confirmed_protective_stop_price == Decimal("45")
    assert confirmed.confirmed_stop_client_order_id == "stop-client-four-shares"
    assert confirmed.confirmed_stop_broker_order_id == "stop-broker-four-shares"
    assert confirmed.pending_action_ids == (entry.logical_action_id,)
    reloaded_entry = store.load_action_projection(entry.logical_action_id)
    assert reloaded_entry.status is ActionStatus.PARTIALLY_FILLED
    assert reloaded_entry.confirmed_quantity == Decimal("4")
    assert reloaded_entry.residual_quantity == Decimal("6")


@pytest.mark.parametrize(
    ("first_quantity", "first_notional", "first_status", "first_residual"),
    [
        ("4", "200", ActionStatus.PARTIALLY_FILLED, Decimal("6")),
        ("10", "500", ActionStatus.FILLED, Decimal("0")),
    ],
)
def test_replacement_opening_fill_is_stable_and_canonical_after_restart(
    tmp_path: Path,
    first_quantity: str,
    first_notional: str,
    first_status: ActionStatus,
    first_residual: Decimal,
) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    decision = _decision(deployment)
    replacement = build_action_intent(
        decision=decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        role=ActionRole.REPLACEMENT,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        reservation_price=Decimal("50"),
        reservation_price_basis="limit_price",
        reservation_stop_price=Decimal("45"),
        risk_per_unit=Decimal("5"),
        risk_basis="entry_to_protective_stop",
    )
    _record_decision(store, replacement)
    store.record_action_intent(replacement, expected_version=None)

    first = _record_fill(
        store,
        replacement,
        event_id="replacement-first-fill",
        cumulative_quantity=first_quantity,
        cumulative_notional=first_notional,
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(replacement.logical_action_id)
    assert first.status is first_status
    assert first.holding_episode_id == holding.holding_episode_id
    assert first.logical_action_id == replacement.logical_action_id
    assert first.residual_quantity == first_residual
    assert holding.opening_action_id == replacement.logical_action_id
    assert holding.initial_filled_quantity == Decimal(first_quantity)
    assert holding.remaining_quantity == Decimal(first_quantity)
    assert holding.committed_risk == Decimal(first_quantity) * Decimal("5")
    assert holding.committed_risk_basis == "entry_to_protective_stop"
    if first_status is ActionStatus.PARTIALLY_FILLED:
        assert holding.pending_action_ids == (replacement.logical_action_id,)
        assert first.reservation_amount == Decimal("300")
        assert first.residual_committed_risk == Decimal("30")
    else:
        assert holding.pending_action_ids == ()
        assert first.reservation_amount == Decimal("0")
        assert first.residual_committed_risk == Decimal("0")

    if first_status is ActionStatus.PARTIALLY_FILLED:
        stop_decision = _decision(
            deployment,
            category=DecisionCategory.EXIT,
            subject_type=DecisionSubjectType.HOLDING,
            subject_id=holding.holding_episode_id,
        )
        store.record_decision(stop_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
        with pytest.raises(ConcurrentStateUpdateError, match="holding changed"):
            store.propose_stop_update(
                holding.holding_episode_id,
                decision=stop_decision,
                stop_price=Decimal("45"),
                expected_holding_version=holding.state_version - 1,
                observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
            )
        stop_intent = store.propose_stop_update(
            holding.holding_episode_id,
            decision=stop_decision,
            stop_price=Decimal("45"),
            expected_holding_version=holding.state_version,
            observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
        )
        with_stop = store.load_holding_episode(holding.holding_episode_id)
        assert replacement.logical_action_id in with_stop.pending_action_ids

        with pytest.raises(ConcurrentStateUpdateError, match="action changed"):
            _record_fill(
                store,
                replacement,
                event_id="replacement-final-fill",
                cumulative_quantity="10",
                cumulative_notional="500",
                expected_action_version=first.state_version - 1,
                expected_holding_version=with_stop.state_version,
            )
        with pytest.raises(ConcurrentStateUpdateError, match="holding changed"):
            _record_fill(
                store,
                replacement,
                event_id="replacement-final-fill",
                cumulative_quantity="10",
                cumulative_notional="500",
                expected_action_version=first.state_version,
                expected_holding_version=holding.state_version,
            )
        completed = _record_fill(
            store,
            replacement,
            event_id="replacement-final-fill",
            cumulative_quantity="10",
            cumulative_notional="500",
            expected_action_version=first.state_version,
            expected_holding_version=with_stop.state_version,
        )
        full_holding = store.load_holding_episode(holding.holding_episode_id)
        assert completed.status is ActionStatus.FILLED
        assert completed.logical_action_id == replacement.logical_action_id
        assert completed.holding_episode_id == holding.holding_episode_id
        assert completed.confirmed_quantity == Decimal("10")
        assert completed.reservation_amount == Decimal("0")
        assert completed.residual_committed_risk == Decimal("0")
        assert full_holding.initial_filled_quantity == Decimal("4")
        assert full_holding.opening_later_fills_quantity == Decimal("6")
        assert full_holding.remaining_quantity == Decimal("10")
        assert full_holding.completed_additions_quantity == Decimal("0")
        assert full_holding.addition_count == 0
        assert full_holding.cost_basis == Decimal("50")
        assert full_holding.committed_risk == Decimal("50")
        assert full_holding.pending_action_ids == (stop_intent.logical_action_id,)

        restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
        replayed = _record_fill(
            restarted,
            replacement,
            event_id="replacement-final-fill",
            cumulative_quantity="10",
            cumulative_notional="500",
            expected_action_version=first.state_version,
            expected_holding_version=with_stop.state_version,
        )
        assert replayed == completed
        assert restarted.load_holding_episode(holding.holding_episode_id) == full_holding
        with pytest.raises(FillReceiptConflictError, match="reused"):
            _record_fill(
                restarted,
                replacement,
                event_id="replacement-final-fill",
                cumulative_quantity="9",
                cumulative_notional="450",
                expected_action_version=first.state_version,
                expected_holding_version=with_stop.state_version,
            )
        with pytest.raises(ConcurrentStateUpdateError, match="holding changed"):
            restarted.confirm_protective_stop(
                stop_intent,
                stop_price=Decimal("45"),
                client_order_id="replacement-stop-client",
                broker_order_id="replacement-stop-broker",
                observed_at=datetime(2026, 10, 1, 13, 34, tzinfo=UTC),
                expected_holding_version=with_stop.state_version,
            )
        protected = restarted.confirm_protective_stop(
            stop_intent,
            stop_price=Decimal("45"),
            client_order_id="replacement-stop-client",
            broker_order_id="replacement-stop-broker",
            observed_at=datetime(2026, 10, 1, 13, 35, tzinfo=UTC),
            expected_holding_version=full_holding.state_version,
        )
        assert protected.remaining_quantity == Decimal("10")
        assert protected.confirmed_protective_stop_price == Decimal("45")
        assert protected.pending_action_ids == ()

    portfolio = PortfolioStateSnapshot(
        deployment_identity=deployment,
        clock=_clock(),
        source_namespace="synthetic-account-source",
        account_snapshot_id=f"replacement-{first_quantity}",
        equity=Decimal("1000"),
        cash=Decimal("1000"),
        gross_exposure=Decimal("0"),
        open_risk=Decimal("0"),
        portfolio_peak_equity=Decimal("1000"),
        last_accepted_session=date(2026, 10, 1),
    )
    store.record_portfolio_snapshot(portfolio)
    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    snapshot = restarted.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    actions = {item.logical_action_id: item for item in snapshot.action_projections}
    assert replacement.logical_action_id in actions
    assert actions[replacement.logical_action_id].holding_episode_id == holding.holding_episode_id
    assert actions[replacement.logical_action_id].status is ActionStatus.FILLED
    assert tuple(item.holding_episode_id for item in snapshot.holding_episodes) == (holding.holding_episode_id,)


def test_replacement_cannot_attach_an_unrelated_existing_holding(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry = _entry_intent(deployment)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="unrelated-opening-fill",
        cumulative_quantity="10",
        cumulative_notional="500",
        expected_action_version=0,
        expected_holding_version=None,
    )
    unrelated_holding = store.load_holding_episode_for_action(entry.logical_action_id)
    replacement_decision = _decision(
        deployment,
        snapshot="b" * 64,
        subject_type=DecisionSubjectType.CANDIDATE,
        subject_id="replacement-candidate",
    )
    replacement = build_action_intent(
        decision=replacement_decision,
        security_id=entry.security_id,
        broker_symbol=entry.broker_symbol,
        role=ActionRole.REPLACEMENT,
        side=OrderSide.BUY,
        requested_quantity=Decimal("5"),
        holding_episode_id=unrelated_holding.holding_episode_id,
    )
    _record_decision(store, replacement)

    with pytest.raises(ValueError, match="replacement.*opening holding"):
        store.record_action_intent(replacement, expected_version=None)

    with pytest.raises(KeyError, match="logical action"):
        store.load_action_projection(replacement.logical_action_id)


def test_stop_proposal_stays_blocked_by_pending_strategy_exit(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry_decision = _decision(deployment)
    entry = build_action_intent(
        decision=entry_decision,
        security_id="FIGI-CC5678",
        broker_symbol="OTHER",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        reservation_price=Decimal("50"),
        reservation_price_basis="limit_price",
    )
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="strategy-exit-opening-fill",
        cumulative_quantity="10",
        cumulative_notional="500",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    exit_decision = _decision(
        deployment,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        snapshot="b" * 64,
    )
    store.record_decision(exit_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    exit_intent = build_action_intent(
        decision=exit_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("5"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("10"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(exit_intent, expected_version=None)
    pending_holding = store.load_holding_episode(holding.holding_episode_id)
    stop_decision = _decision(
        deployment,
        session=date(2026, 10, 1),
        snapshot="c" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(stop_decision, policy_payload={}, guard_payload={}, effective_action_payload={})

    with pytest.raises(ValueError, match="partially filled entry"):
        store.propose_stop_update(
            holding.holding_episode_id,
            decision=stop_decision,
            stop_price=Decimal("45"),
            expected_holding_version=pending_holding.state_version,
            observed_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
        )
    unchanged = store.load_holding_episode(holding.holding_episode_id)
    assert unchanged.pending_action_ids == (exit_intent.logical_action_id,)
    assert unchanged.proposed_stop_price is None
    assert unchanged.state_version == pending_holding.state_version


@pytest.mark.parametrize(
    ("existing_risk_per_unit", "addition_risk_per_unit"),
    ((None, Decimal("5")), (Decimal("5"), None)),
)
def test_addition_never_turns_unknown_aggregate_holding_risk_into_known_risk(
    tmp_path: Path,
    existing_risk_per_unit: Decimal | None,
    addition_risk_per_unit: Decimal | None,
) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry_decision = _decision(deployment)
    entry_kwargs = {} if existing_risk_per_unit is None else {
        "risk_per_unit": existing_risk_per_unit,
        "risk_basis": "entry_to_protective_stop",
    }
    entry = build_action_intent(
        decision=entry_decision,
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
        **entry_kwargs,
    )
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="risk-opening-fill",
        cumulative_quantity="10",
        cumulative_notional="500",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    expected_known_risk = None if existing_risk_per_unit is None else Decimal("50")
    assert holding.committed_risk == expected_known_risk

    addition_decision = _decision(
        deployment,
        category=DecisionCategory.ADDITION,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        snapshot="b" * 64,
    )
    store.record_decision(addition_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    addition_kwargs = {} if addition_risk_per_unit is None else {
        "risk_per_unit": addition_risk_per_unit,
        "risk_basis": "entry_to_protective_stop",
    }
    addition = build_action_intent(
        decision=addition_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.ADDITION,
        side=OrderSide.BUY,
        requested_quantity=Decimal("2"),
        **addition_kwargs,
    )
    store.record_action_intent(addition, expected_version=None)
    pending_holding = store.load_holding_episode(holding.holding_episode_id)
    _record_fill(
        store,
        addition,
        event_id="risk-addition-fill",
        cumulative_quantity="2",
        cumulative_notional="100",
        expected_action_version=0,
        expected_holding_version=pending_holding.state_version,
    )

    updated_holding = store.load_holding_episode(holding.holding_episode_id)
    assert updated_holding.remaining_quantity == Decimal("12")
    assert updated_holding.committed_risk is None
    assert updated_holding.committed_risk_basis is None


def test_public_holding_reconciliation_rejects_quantity_and_protection_bypasses(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry = _entry_intent(deployment)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="holding-reconciliation-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)

    changed_initial_quantity = replace(
        holding,
        initial_filled_quantity=Decimal("200"),
        remaining_quantity=Decimal("200"),
    )
    with pytest.raises(ValueError, match="only policy flags"):
        store.record_holding_episode(
            changed_initial_quantity,
            expected_version=holding.state_version,
            evidence_ref="synthetic-position-check-1",
        )
    changed_peak = replace(holding, peak_price=Decimal("60"))
    with pytest.raises(ValueError, match="only policy flags"):
        store.record_holding_episode(
            changed_peak,
            expected_version=holding.state_version,
            evidence_ref="synthetic-position-check-2",
        )
    fabricated_stop = replace(
        holding,
        confirmed_protective_stop_price=Decimal("45"),
        confirmed_stop_action_id="unrecorded-stop-action",
        confirmed_stop_client_order_id="unrecorded-client",
        confirmed_stop_broker_order_id="unrecorded-broker",
        confirmed_stop_observed_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="only policy flags"):
        store.record_holding_episode(
            fabricated_stop,
            expected_version=holding.state_version,
            evidence_ref="synthetic-position-check-3",
        )
    unchanged = store.load_holding_episode(holding.holding_episode_id)
    assert unchanged.initial_filled_quantity == Decimal("100")
    assert unchanged.remaining_quantity == Decimal("100")
    assert unchanged.state_version == holding.state_version


def test_public_holding_reconciliation_retains_flag_evidence_in_history(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry = _entry_intent(deployment)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="holding-flag-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    flagged = replace(
        holding,
        policy_flags=(("position_reconciliation_required", "offline account comparison pending"),),
    )
    version = store.record_holding_episode(
        flagged,
        expected_version=holding.state_version,
        evidence_ref="offline-account-snapshot-2026-10-01",
    )
    flagged = store.load_holding_episode(holding.holding_episode_id)
    assert version == flagged.state_version == holding.state_version + 1
    cleared = replace(flagged, policy_flags=())
    version = store.record_holding_episode(
        cleared,
        expected_version=flagged.state_version,
        evidence_ref="offline-account-reconciliation-2026-10-01",
    )
    assert version == flagged.state_version + 1
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT evidence_ref, state_json FROM policy_state_holding_history "
            "WHERE holding_episode_id=? ORDER BY state_version DESC LIMIT 2",
            (holding.holding_episode_id,),
        ).fetchall()
    assert [row[0] for row in rows] == [
        "offline-account-reconciliation-2026-10-01",
        "offline-account-snapshot-2026-10-01",
    ]
    assert all(row[0] in row[1] for row in rows)


def test_provider_scoped_order_aliases_survive_action_projection_reload(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    intent = _entry_intent(deployment)
    _record_decision(store, intent)
    store.record_action_intent(intent, expected_version=None)
    first = store.bind_attempt_order_refs(
        intent.logical_action_id,
        1,
        provider_id="recorded-provider-a",
        client_order_id="client-primary",
        broker_order_id="broker-primary",
        expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    second = store.bind_attempt_order_refs(
        intent.logical_action_id,
        1,
        provider_id="recorded-provider-b",
        client_order_id="client-alias",
        broker_order_id="broker-alias",
        expected_action_version=first.state_version,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
    )
    third = store.bind_attempt_order_refs(
        intent.logical_action_id,
        1,
        provider_id="recorded-provider-c",
        client_order_id="client-alias",
        broker_order_id="broker-alias",
        expected_action_version=second.state_version,
        observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
    )
    projection = store.load_action_projection(intent.logical_action_id)
    attempt = projection.order_attempts[0]
    assert third.state_version == projection.state_version == 3
    assert attempt.client_order_id == "client-primary"
    assert attempt.broker_order_id == "broker-primary"
    assert attempt.client_order_aliases == ("client-alias",)
    assert attempt.broker_order_aliases == ("broker-alias",)
    assert {
        (reference.provider_id, reference.reference_kind, reference.external_order_id)
        for reference in projection.provider_order_references
    } == {
        ("recorded-provider-a", "client_order_id", "client-primary"),
        ("recorded-provider-a", "broker_order_id", "broker-primary"),
        ("recorded-provider-b", "client_order_id", "client-alias"),
        ("recorded-provider-b", "broker_order_id", "broker-alias"),
        ("recorded-provider-c", "client_order_id", "client-alias"),
        ("recorded-provider-c", "broker_order_id", "broker-alias"),
    }
    aliases = store.load_order_reference_aliases(intent.logical_action_id)
    assert {row["external_order_id"] for row in aliases} == {
        "client-primary",
        "broker-primary",
        "client-alias",
        "broker-alias",
    }
    with sqlite3.connect(store.db_path) as conn:
        history = conn.execute(
            "SELECT state_json FROM policy_state_action_history WHERE logical_action_id=? AND state_version=3",
            (intent.logical_action_id,),
        ).fetchone()[0]
    assert "recorded-provider-c" in history

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    reopened = restarted.load_action_projection(intent.logical_action_id)
    assert reopened.provider_order_references == projection.provider_order_references
    assert reopened.order_attempts[0].client_order_aliases == ("client-alias",)


def test_binding_remainder_refs_preserves_late_fill_reconciliation_across_restart(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    entry = _entry_intent(deployment)
    _record_decision(store, entry)
    store.record_action_intent(entry, expected_version=None)
    _record_fill(
        store,
        entry,
        event_id="remainder-recovery-opening-fill",
        cumulative_quantity="100",
        cumulative_notional="5000",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(entry.logical_action_id)
    exit_decision = _decision(
        deployment,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(exit_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    action = build_action_intent(
        decision=exit_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("50"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("100"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    store.record_action_intent(action, expected_version=None)
    submitted = store.bind_attempt_order_refs(
        action.logical_action_id,
        1,
        provider_id="recorded-remainder-provider",
        client_order_id="first-client",
        broker_order_id="first-broker",
        expected_action_version=0,
        observed_at=datetime(2026, 10, 1, 13, 31, tzinfo=UTC),
    )
    after_first_fill = store.load_holding_episode(holding.holding_episode_id)
    underfilled = _record_fill(
        store,
        action,
        event_id="first-attempt-fill-20",
        cumulative_quantity="20",
        cumulative_notional="1000",
        expected_action_version=submitted.state_version,
        expected_holding_version=after_first_fill.state_version,
    )
    cancel = store.request_order_cancel(
        action.logical_action_id,
        1,
        expected_action_version=underfilled.state_version,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
    )
    terminal = store.confirm_order_terminal(
        action.logical_action_id,
        1,
        terminal_status=ActionAttemptStatus.CANCELLED,
        expected_action_version=cancel.state_version,
        observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
    )
    remainder = store.create_single_remainder_attempt(
        action.logical_action_id,
        expected_action_version=terminal.state_version,
        observed_at=datetime(2026, 10, 1, 13, 34, tzinfo=UTC),
    )
    after_late_fill = store.load_holding_episode(holding.holding_episode_id)
    late_fill = store.record_cumulative_fill(
        action.logical_action_id,
        1,
        provider_id="recorded-remainder-provider",
        fill_event_id="first-attempt-late-fill-25",
        cumulative_quantity=Decimal("25"),
        cumulative_notional=Decimal("1250"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 35, tzinfo=UTC),
        expected_action_version=remainder.state_version,
        expected_holding_version=after_late_fill.state_version,
    )
    assert late_fill.status is ActionStatus.RECONCILIATION_REQUIRED
    assert late_fill.state_version == remainder.state_version + 1

    bound_remainder = store.bind_attempt_order_refs(
        action.logical_action_id,
        2,
        provider_id="recorded-remainder-provider",
        client_order_id="remainder-client",
        broker_order_id="remainder-broker",
        expected_action_version=late_fill.state_version,
        observed_at=datetime(2026, 10, 1, 13, 36, tzinfo=UTC),
    )
    assert bound_remainder.status is ActionStatus.RECONCILIATION_REQUIRED
    assert bound_remainder.order_attempts[0].terminal_status is ActionAttemptStatus.CANCELLED
    assert bound_remainder.order_attempts[1].status is ActionAttemptStatus.SUBMITTED

    before_remainder_fill = store.load_holding_episode(holding.holding_episode_id)
    filled_while_live = store.record_cumulative_fill(
        action.logical_action_id,
        2,
        provider_id="recorded-remainder-provider",
        fill_event_id="remainder-attempt-fill-25",
        cumulative_quantity=Decimal("25"),
        cumulative_notional=Decimal("1250"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 37, tzinfo=UTC),
        expected_action_version=bound_remainder.state_version,
        expected_holding_version=before_remainder_fill.state_version,
    )
    assert filled_while_live.status is ActionStatus.RECONCILIATION_REQUIRED
    assert filled_while_live.confirmed_quantity == Decimal("50")
    assert filled_while_live.residual_quantity == Decimal("0")
    assert filled_while_live.order_attempts[1].requested_quantity == Decimal("30")
    assert filled_while_live.order_attempts[1].confirmed_filled_quantity == Decimal("25")
    still_pending = store.load_holding_episode(holding.holding_episode_id)
    assert still_pending.last_exit_tier == 0
    assert still_pending.pending_action_ids == (action.logical_action_id,)

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    projection = restarted.load_action_projection(action.logical_action_id)
    assert projection.status is ActionStatus.RECONCILIATION_REQUIRED
    assert projection.confirmed_quantity == Decimal("50")
    assert projection.order_attempts[1].requested_quantity == Decimal("30")
    assert restarted.load_holding_episode(holding.holding_episode_id).last_exit_tier == 0


def test_late_fill_after_completed_scale_out_requires_explicit_resolution(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    deployment = _deployment()
    store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    opening = build_action_intent(
        decision=_decision(deployment),
        security_id="FIGI-BB1234",
        broker_symbol="ACME",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("10"),
    )
    _record_decision(store, opening)
    store.record_action_intent(opening, expected_version=None)
    _record_fill(
        store,
        opening,
        event_id="late-scale-opening-fill",
        cumulative_quantity="10",
        cumulative_notional="500",
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(opening.logical_action_id)
    decision = _decision(
        deployment,
        session=date(2026, 10, 1),
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    scale_out = build_action_intent(
        decision=decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("5"),
        exit_tier=1,
        snapshot_original_quantity=Decimal("10"),
        fraction_of_original_quantity=Decimal("0.5"),
        rounding_rule_id="whole_share_floor_v1",
    )
    _record_decision(store, scale_out)
    store.record_action_intent(scale_out, expected_version=None)
    after_intent = store.load_holding_episode(holding.holding_episode_id)
    filled = _record_fill(
        store,
        scale_out,
        event_id="late-scale-target-fill",
        cumulative_quantity="5",
        cumulative_notional="250",
        expected_action_version=0,
        expected_holding_version=after_intent.state_version,
    )
    completed = store.load_holding_episode(holding.holding_episode_id)
    assert filled.status is ActionStatus.FILLED
    assert completed.remaining_quantity == Decimal("5")
    assert completed.last_exit_tier == 1
    assert completed.pending_action_ids == ()

    late_fill = store.record_cumulative_fill(
        scale_out.logical_action_id,
        1,
        provider_id="recorded-fixture-provider",
        fill_event_id="late-scale-extra-fill",
        cumulative_quantity=Decimal("6"),
        cumulative_notional=Decimal("300"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 13, 32, tzinfo=UTC),
        expected_action_version=filled.state_version,
        expected_holding_version=completed.state_version,
    )
    conflicted = store.load_holding_episode(holding.holding_episode_id)
    assert late_fill.status is ActionStatus.RECONCILIATION_REQUIRED
    assert conflicted.remaining_quantity == Decimal("4")
    assert conflicted.last_exit_tier == 1
    assert conflicted.pending_action_ids == (scale_out.logical_action_id,)

    conflicting_decision = _decision(
        deployment,
        session=date(2026, 10, 2),
        snapshot="e" * 64,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    conflicting_action = build_action_intent(
        decision=conflicting_decision,
        security_id=holding.security_id,
        broker_symbol=holding.broker_symbol,
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.SCALE_OUT,
        side=OrderSide.SELL,
        requested_quantity=Decimal("2"),
        exit_tier=2,
        snapshot_original_quantity=Decimal("10"),
        fraction_of_original_quantity=Decimal("0.2"),
        rounding_rule_id="whole_share_floor_v1",
    )
    _record_decision(store, conflicting_action)
    with pytest.raises(ValueError, match="pending logical action"):
        store.record_action_intent(conflicting_action, expected_version=None)

    resolved = store.record_explicit_action_resolution(
        scale_out.logical_action_id,
        resolution_reason="Synthetic reconciliation confirms the extra share was sold",
        expected_action_version=late_fill.state_version,
        expected_holding_version=conflicted.state_version,
        observed_at=datetime(2026, 10, 1, 13, 33, tzinfo=UTC),
    )
    assert resolved.status is ActionStatus.RESOLVED
    resolved_holding = store.load_holding_episode(holding.holding_episode_id)
    assert resolved_holding.remaining_quantity == Decimal("4")
    assert resolved_holding.last_exit_tier == 1
    assert resolved_holding.pending_action_ids == ()

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    assert restarted.load_action_projection(scale_out.logical_action_id).status is ActionStatus.RESOLVED
    assert restarted.load_holding_episode(holding.holding_episode_id) == resolved_holding
