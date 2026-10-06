from datetime import timedelta
from decimal import Decimal

from core.policy_execution_state import ActionRole, ActionStatus, DecisionCategory, DecisionIdentity, DecisionSubjectType, OrderSide
from core.policy_execution_store import PolicyExecutionStateStore
from core.policy_exits import propose_protection_resize, start_full_exit, start_scale_out
from tests.test_paper_policy_chain import read_chain, seed_pending_chain


def _fixture(tmp_path):
    features, path, deployment, account, portfolio, _, _ = seed_pending_chain(tmp_path)
    store = PolicyExecutionStateStore(path, store_identity=deployment.store_identity)
    chain = read_chain(store, deployment, portfolio)
    holding = next(item for item in chain.holding_episodes if item.broker_symbol == "CCC")
    return features, store, deployment, portfolio, holding


def _exit_decision(features, deployment, portfolio, holding, *, sequence=0):
    clock = portfolio.clock
    if sequence:
        clock = type(clock)(
            exchange_id=clock.exchange_id,
            decision_session=clock.decision_session + timedelta(days=sequence),
            as_of_cutoff_at=clock.as_of_cutoff_at + timedelta(days=sequence),
            next_execution_session=clock.next_execution_session + timedelta(days=sequence),
            account_valuation_session=clock.account_valuation_session + timedelta(days=sequence),
            account_valuation_at=clock.account_valuation_at + timedelta(days=sequence),
        )
    return DecisionIdentity.build(
        deployment=deployment,
        clock=clock,
        snapshot_sha256=features.recorded_input_manifest_sha256,
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
        sequence=(sequence or None),
    )


def _fill(store, action, quantity, event_id):
    submitted = store.bind_attempt_order_refs(
        action.logical_action_id,
        1,
        provider_id="offline-fake-broker",
        client_order_id=f"client:{event_id}",
        broker_order_id=f"broker:{event_id}",
        expected_action_version=0,
        observed_at=action.decision.clock.account_valuation_at,
    )
    holding = store.load_holding_episode(action.holding_episode_id)
    return store.record_cumulative_fill(
        action.logical_action_id,
        1,
        provider_id="offline-fake-broker",
        fill_event_id=event_id,
        cumulative_quantity=Decimal(quantity),
        cumulative_notional=Decimal(quantity) * Decimal("100"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=action.decision.clock.account_valuation_at,
        expected_action_version=submitted.state_version,
        expected_holding_version=holding.state_version,
    )


def test_scale_out_uses_original_and_remaining_quantities_and_recovers_partial_action(tmp_path):
    features, store, deployment, portfolio, holding = _fixture(tmp_path)
    decision = _exit_decision(features, deployment, portfolio, holding)

    action = start_scale_out(
        store,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.5"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload={"outcome": "allow_offline_fixture"},
    )

    assert action.role is ActionRole.SCALE_OUT
    assert action.side is OrderSide.SELL
    assert action.requested_quantity == Decimal("3")
    assert action.snapshot_original_quantity == Decimal("6")
    assert action.fraction_of_original_quantity == Decimal("0.5")
    partial = _fill(store, action, "2", "exit-partial-2")
    assert partial.status is ActionStatus.PARTIALLY_FILLED
    assert store.load_holding_episode(holding.holding_episode_id).remaining_quantity == Decimal("4")

    restarted = PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity)
    replayed = start_scale_out(
        restarted,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.5"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload={"outcome": "allow_offline_fixture"},
    )

    assert replayed.logical_action_id == action.logical_action_id
    assert replayed.status is ActionStatus.PARTIALLY_FILLED
    assert replayed.confirmed_filled_quantity == Decimal("2")
    assert restarted.load_holding_episode(holding.holding_episode_id).pending_action_ids == (action.logical_action_id,)


def test_scale_out_is_capped_by_the_actual_remaining_position_after_a_prior_exit(tmp_path):
    features, store, deployment, portfolio, holding = _fixture(tmp_path)
    first_decision = _exit_decision(features, deployment, portfolio, holding)
    first = start_scale_out(
        store,
        decision=first_decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.75"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload={"outcome": "allow_offline_fixture"},
    )
    assert first.requested_quantity == Decimal("4")  # floor(6 * .75)
    _fill(store, first, "4", "first-tier-4")
    reduced = store.load_holding_episode(holding.holding_episode_id)
    assert reduced.remaining_quantity == Decimal("2")
    assert reduced.last_exit_tier == 1

    next_decision = _exit_decision(features, deployment, portfolio, reduced, sequence=1)
    second = start_scale_out(
        store,
        decision=next_decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.5"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=reduced.state_version,
        policy_payload={"tier": 2},
        guard_payload={"outcome": "allow_offline_fixture"},
    )
    assert second.exit_tier == 2
    assert second.snapshot_original_quantity == Decimal("6")
    assert second.requested_quantity == Decimal("2")  # min(floor(6 * .5), current remainder)


def test_close_action_and_protective_resize_use_the_remaining_quantity(tmp_path):
    features, store, deployment, portfolio, holding = _fixture(tmp_path)
    first_decision = _exit_decision(features, deployment, portfolio, holding)
    first = start_scale_out(
        store,
        decision=first_decision,
        holding_episode_id=holding.holding_episode_id,
        fraction_of_original_quantity=Decimal("0.75"),
        quantity_increment=Decimal("1"),
        rounding_rule_id="whole_share_floor_v1",
        expected_holding_version=holding.state_version,
        policy_payload={"tier": 1},
        guard_payload={"outcome": "allow_offline_fixture"},
    )
    _fill(store, first, "4", "protective-resize-precondition")
    reduced = store.load_holding_episode(holding.holding_episode_id)
    decision = _exit_decision(features, deployment, portfolio, reduced, sequence=1)

    resize = propose_protection_resize(
        store,
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        stop_price=reduced.confirmed_protective_stop_price,
        expected_holding_version=reduced.state_version,
        policy_payload={"reason": "post_scale_out"},
        guard_payload={"outcome": "allow_offline_fixture"},
        observed_at=decision.clock.account_valuation_at,
    )

    assert resize.remaining_quantity == Decimal("2")
    assert resize.replaces_broker_order_id == reduced.confirmed_stop_broker_order_id
    assert resize.replaces_client_order_id == reduced.confirmed_stop_client_order_id
    assert resize.stop_intent.requested_stop_price == reduced.confirmed_protective_stop_price
    record = store.load_decision_record(decision.decision_id)
    assert record.effective_action_payload["remaining_quantity"] == "2"
    # The fake broker receives the persisted quantity, then its confirmed order reference is durable.
    fake_stop_order = {
        "holding_episode_id": resize.holding_episode_id,
        "replaces_broker_order_id": resize.replaces_broker_order_id,
        "quantity": resize.remaining_quantity,
        "stop_price": resize.stop_price,
    }
    assert fake_stop_order["replaces_broker_order_id"] == reduced.confirmed_stop_broker_order_id
    assert fake_stop_order["quantity"] == Decimal("2")
    confirmed = store.confirm_protective_stop(
        resize.stop_intent,
        stop_price=resize.stop_intent.requested_stop_price,
        client_order_id="resized-stop-client",
        broker_order_id="resized-stop-broker",
        observed_at=decision.clock.account_valuation_at,
        expected_holding_version=store.load_holding_episode(holding.holding_episode_id).state_version,
    )
    assert confirmed.remaining_quantity == Decimal("2")
    assert confirmed.confirmed_stop_broker_order_id == "resized-stop-broker"
    recovered_resize = propose_protection_resize(
        PolicyExecutionStateStore(store.db_path, store_identity=deployment.store_identity),
        decision=decision,
        holding_episode_id=holding.holding_episode_id,
        stop_price=reduced.confirmed_protective_stop_price,
        expected_holding_version=reduced.state_version,
        policy_payload={"reason": "post_scale_out"},
        guard_payload={"outcome": "allow_offline_fixture"},
        observed_at=decision.clock.account_valuation_at,
    )
    assert recovered_resize.remaining_quantity == Decimal("2")
    assert recovered_resize.stop_intent == resize.stop_intent

    close_decision = _exit_decision(features, deployment, portfolio, confirmed, sequence=2)
    close = start_full_exit(
        store,
        decision=close_decision,
        holding_episode_id=holding.holding_episode_id,
        expected_holding_version=confirmed.state_version,
        policy_payload={"reason": "fixed_full_exit"},
        guard_payload={"outcome": "allow_offline_fixture"},
    )
    assert close.role is ActionRole.CLOSE
    assert close.requested_quantity == Decimal("2")
