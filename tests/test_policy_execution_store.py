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
from core.policy_execution_store import PolicyExecutionStateStore


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
    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=None,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-ready-a",
        outgoing_entries_reconciled=True,
    )
    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_a.deployment_generation_id,
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


def test_active_pointer_compare_and_set_serializes_competing_writers(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.migrate()
    base = _deployment()
    second = _deployment(revision="b" * 40)
    third = _deployment(revision="c" * 40)
    for deployment in (base, second, third):
        store.register_deployment_identity(deployment, lifecycle="prepared", handler_identity="fixture-handler", guard_id="guard-v1")
    store.set_active_generation(
        base.paper_account_environment_id,
        expected_generation_id=None,
        new_generation_id=base.deployment_generation_id,
            readiness_evidence_ref="synthetic-initial-pointer",
        outgoing_entries_reconciled=True,
    )
    barrier = Barrier(2)

    def contender(target: PolicyDeploymentIdentity) -> bool:
        writer = PolicyExecutionStateStore(store.db_path, store_identity=base.store_identity)
        barrier.wait()
        try:
            writer.set_active_generation(
                base.paper_account_environment_id,
                expected_generation_id=base.deployment_generation_id,
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

    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=None,
        new_generation_id=generation_a.deployment_generation_id,
        readiness_evidence_ref="synthetic-generation-a",
        outgoing_entries_reconciled=True,
    )
    store.set_active_generation(
        generation_a.paper_account_environment_id,
        expected_generation_id=generation_a.deployment_generation_id,
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
    projection = store.load_action_projection(intent.logical_action_id)
    attempt = projection.order_attempts[0]
    assert second.state_version == projection.state_version == 2
    assert attempt.client_order_id == "client-primary"
    assert attempt.broker_order_id == "broker-primary"
    assert attempt.client_order_aliases == ("client-alias",)
    assert attempt.broker_order_aliases == ("broker-alias",)
    aliases = store.load_order_reference_aliases(intent.logical_action_id)
    assert {row["external_order_id"] for row in aliases} == {
        "client-primary",
        "broker-primary",
        "client-alias",
        "broker-alias",
    }
