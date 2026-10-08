"""Two bounded real-simulator/current-paper cases; execution requires batch admission."""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pytest

from tests import paper_policy_fixtures, test_issue_101_paper_policy as owner101
from tests.paper_verification.parity_data import (
    INPUT_PATH, INPUT_SHA256, TRACE_LIMIT, TRADABLES, create_data, current_features, digest, reconcile_actual, synthetic_clock,
)
from tests.paper_verification.parity_fixture import RecordingSimulatorPolicy


class NoProvider:
    """Truthy constructor dependency; any provider/cache request is a failure."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"synthetic historical path requested provider dependency: {name}")


@pytest.fixture
def parity_evidence(tmp_path, request):
    trace = {"status": "incomplete", "nodeid": request.node.nodeid}

    def retain():
        raw = (json.dumps(trace, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        assert len(raw) <= TRACE_LIMIT, "parity trace overflow; no cropping"
        output = tmp_path / "parity-trace.json"
        with output.open("xb") as stream:
            stream.write(raw)
        print("PAPER_PARITY_TRACE " + json.dumps({"path": str(output), "bytes": len(raw),
                                                   "sha256": hashlib.sha256(raw).hexdigest()}))
        # Full bounded evidence travels in the wrapper's already retained stdout;
        # no additional copy, old-root read, or receipt-retention rule is needed.
        print("PAPER_PARITY_TRACE_DATA " + raw.decode("utf-8").rstrip("\n"))

    request.addfinalizer(retain)
    return trace


def _fill_money(store, action_id):
    # Only the explicit new guard-owned store created by this test is read.
    with closing(sqlite3.connect(str(store.db_path))) as connection:
        row = connection.execute(
            "SELECT confirmed_quantity, cumulative_notional, cumulative_fees "
            "FROM policy_state_order_attempts WHERE logical_action_id=? AND attempt_number=1", (action_id,),
        ).fetchone()
    assert row is not None
    return Decimal(row[0]), None if row[1] is None else Decimal(row[1]), row[2]


def _differences(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = {}
        for key in sorted(set(left) | set(right)):
            result.update(_differences(left.get(key), right.get(key), prefix + "." + key))
        return result
    return {} if left == right else {prefix.lstrip("."): {"historical": left, "paper": right}}


def _policy_sources(variant):
    root = Path(__file__).parents[1] / "fixtures" / "paper_parity_policy" / variant
    return {name: (root / f"{name}.py").read_bytes().decode("utf-8")
            for name in ("entry", "risk", "position", "exit")}


def test_parity_clock_rejects_cutoff_before_recorded_close():
    """The actual current-input clock rejects this before a feature context exists."""
    assert digest(INPUT_PATH) == INPUT_SHA256
    data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    clock, _completion = synthetic_clock(data)
    with pytest.raises(ValueError, match="as-of cutoff precedes recorded exchange close"):
        replace(clock, as_of_cutoff=clock.completion_evidence.session_close_at - timedelta(seconds=1))


def _lifecycle(monkeypatch, store, deployment, clock, manager, submissions, actions, stop_calls):
    from core.execution_store import get_execution_store
    from core.execution_workflow import clear_workflow_registry
    from core.order_manager import OrderManager
    from core.policy_execution_store import PolicyExecutionStateStore
    from core.strategy_policy.account_reconciliation import BrokerOrderFact, BrokerPositionFact, ClassificationFact

    # The fake broker callbacks occur at the fixture's declared valuation time.
    # Keep actual bridge methods; only their external observation clock is fixed.
    class ObservationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = clock.account_valuation_at
            return stamp.astimezone(tz) if tz is not None else stamp.replace(tzinfo=None)

    monkeypatch.setattr("core.policy_protection_bridge.datetime", ObservationClock)
    evidence = []
    initial_submissions = len(submissions)
    for submission, action in zip(submissions, actions, strict=True):
        qty = action.requested_quantity
        manager.handle_partial_fill(
            symbol=action.broker_symbol, broker_order_id=submission["broker_order_id"],
            client_order_id=submission["client_order_id"], side="buy", filled_qty=float(qty / 2),
            fill_price=100.0, order_type="limit",
        )
        holding = store.load_holding_episode_for_action(action.logical_action_id)
        projection = store.load_action_projection(action.logical_action_id)
        assert holding.remaining_quantity == qty / 2
        assert holding.cost_basis is None  # The public callback does not report fees.
        assert _fill_money(store, action.logical_action_id) == (qty / 2, qty / 2 * 100, None)
        assert holding.committed_risk == qty / 2 * 5
        assert holding.confirmed_protective_stop_price == Decimal("95")
        assert projection.confirmed_quantity == qty / 2
        assert projection.residual_quantity == qty / 2
        assert projection.reservation_amount == qty / 2 * 100
        assert projection.residual_committed_risk == qty / 2 * 5
        assert get_execution_store().load_active_position(action.broker_symbol)["qty"] == float(qty / 2)
        assert stop_calls[-1]["qty"] == float(qty / 2)
        evidence.append({"action": action.logical_action_id, "security": action.security_id,
                         "generation": deployment.deployment_generation_id,
                         "holding": holding.holding_episode_id, "partial_quantity": str(qty / 2),
                         "reserved_cash": str(projection.reservation_amount),
                         "reserved_risk": str(projection.residual_committed_risk),
                         "confirmed_stop": str(holding.confirmed_protective_stop_price)})

    clear_workflow_registry()
    reopened = PolicyExecutionStateStore(store.db_path, store_identity=store.store_identity)
    classification = ClassificationFact("synthetic", "observed", clock.decision_session,
                                      clock.decision_session, None, clock.as_of_cutoff_at)
    positions, orders = [], []
    gross = risk = Decimal("0")
    for action in actions:
        holding = reopened.load_holding_episode_for_action(action.logical_action_id)
        attempt = reopened.load_action_projection(action.logical_action_id).order_attempts[0]
        filled = float(holding.remaining_quantity)
        positions.append(BrokerPositionFact(action.broker_symbol, filled, 100.0, clock.account_valuation_at,
                                            classification, classification))
        orders.extend((
            BrokerOrderFact(attempt.broker_order_id, attempt.client_order_id, action.broker_symbol,
                            "buy", "partially_filled", float(action.requested_quantity), filled),
            BrokerOrderFact(holding.confirmed_stop_broker_order_id, holding.confirmed_stop_client_order_id,
                            action.broker_symbol, "sell", "submitted", filled, 0,
                            purpose="protective_stop", holding_episode_id=holding.holding_episode_id, stop_price=95),
        ))
        gross += holding.remaining_quantity * 100
        risk += holding.remaining_quantity * 5
    after_restart = reconcile_actual(
        reopened, deployment, clock, label="partial-after-restart", cash=Decimal("10000") - gross,
        gross=gross, risk=risk, positions=positions, orders=orders,
        decision_slot_id=actions[0].decision.decision_slot_id, decision_id=actions[0].decision.decision_id,
    )
    assert after_restart.pending_entry_count == len(actions)
    assert after_restart.reserved_buy_cash == float(gross)
    assert after_restart.reserved_buy_risk == float(risk)
    assert after_restart.open_position_risk == float(risk)
    assert after_restart.pending_sell_count == 0  # Authenticated protective orders are not strategy exits.
    manager = OrderManager(paper=True, policy_store=reopened)
    for submission, action, record in zip(submissions, actions, evidence, strict=True):
        arguments = dict(symbol=action.broker_symbol, broker_order_id=submission["broker_order_id"],
                         client_order_id=submission["client_order_id"], side="buy",
                         filled_qty=float(action.requested_quantity), fill_price=100.0, order_type="limit")
        manager.handle_fill(**arguments)
        complete = reopened.load_action_projection(action.logical_action_id)
        holding = reopened.load_holding_episode_for_action(action.logical_action_id)
        assert holding.holding_episode_id == record["holding"]
        assert holding.remaining_quantity == complete.confirmed_quantity == action.requested_quantity
        assert holding.cost_basis is None
        assert _fill_money(reopened, action.logical_action_id) == (
            action.requested_quantity, action.requested_quantity * 100, None,
        )
        assert holding.committed_risk == action.requested_quantity * 5
        assert holding.confirmed_protective_stop_price == Decimal("95")
        assert complete.residual_quantity == 0 and complete.reservation_amount == 0
        assert complete.residual_committed_risk == 0
        assert stop_calls[-1]["qty"] == float(action.requested_quantity)
        assert get_execution_store().load_active_position(action.broker_symbol)["qty"] == float(action.requested_quantity)
        manager.handle_fill(**arguments)
        assert reopened.load_action_projection(action.logical_action_id) == complete
        replayed = reopened.load_holding_episode_for_action(action.logical_action_id)
        assert replayed.remaining_quantity == holding.remaining_quantity
        assert replayed.applied_action_fill_watermarks == holding.applied_action_fill_watermarks
        assert replayed.committed_risk == holding.committed_risk
        assert replayed.cost_basis is None
        assert _fill_money(reopened, action.logical_action_id) == (
            action.requested_quantity, action.requested_quantity * 100, None,
        )
        record.update({"full_quantity": str(complete.confirmed_quantity), "reservation_after": "0",
                       "full_notional": str(action.requested_quantity * 100), "fees": None,
                       "full_cost_basis": None, "full_risk": str(holding.committed_risk),
                       "duplicate_delta": "0"})
    assert len(submissions) == initial_submissions
    return {"entries": evidence, "reopened_reconciliation_ready": after_restart.ready,
            "partial_reserved_cash": after_restart.reserved_buy_cash,
            "partial_reserved_risk": after_restart.reserved_buy_risk,
            "submit_count": len(submissions), "protection_call_count": len(stop_calls)}


@pytest.mark.parametrize("variant,capacity,quantity,risk_fraction,notional_fraction,expected_symbols", [
    ("base", 1, 10, 0.01, 0.10, ("AAA",)),
    ("contrast", 2, 5, 0.005, 0.05, ("CCC", "BBB")),
], ids=["base", "contrast"])
def test_real_simulator_current_paper_parity_and_public_lifecycle(
    tmp_path, monkeypatch, parity_evidence, variant, capacity, quantity, risk_fraction, notional_fraction, expected_symbols,
):
    from auto_trader import run_auto_trader
    from core.backtest_engine import PortfolioSimulator
    from core.backtest_fills import ExecutionProfileV5, FrictionScenario
    from core.current_policy_inputs import FEATURE_CALCULATOR_IDENTITY, FEATURE_CONTRACT_ID
    from core.execution_workflow import clear_workflow_registry, get_workflow
    from core.order_execution import OrderResult, ProtectiveStopResult
    from core.order_manager import OrderManager
    from core.policy_execution_state import DecisionClock
    from core.strategy_policy.frozen_bundle import (
        FrozenPolicyClient, inspect_frozen_policy_bundle, load_frozen_policy_bundle,
    )

    data, path, provenance, availability, inputs = create_data(tmp_path / "data")
    parity_evidence.update({"variant": variant, "inputs": inputs})
    policy_path = owner101._write_policy_bundle(
        tmp_path / "policy", feature_contract_id=FEATURE_CONTRACT_ID,
        feature_calculator_identity=FEATURE_CALCULATOR_IDENTITY, sources=_policy_sources(variant),
    )
    descriptor = inspect_frozen_policy_bundle(policy_path)
    policy_pins = json.loads((Path(__file__).parents[1] / "fixtures" / "paper_parity_policy"
                              / "expected-inputs.json").read_text(encoding="utf-8"))[variant]
    assert digest(policy_path / "manifest.json") == policy_pins["manifest_sha256"]
    assert dict(descriptor.source_sha256) == {name: record["sha256"] for name, record in policy_pins["sources"].items()}
    parity_evidence["policy_pins"] = policy_pins
    frozen = load_frozen_policy_bundle(policy_path, expected_identity=owner101._loader_identity(descriptor))
    historical = RecordingSimulatorPolicy(descriptor, frozen)
    parity_evidence["historical_calls"] = historical.canonical_records
    provider = NoProvider()
    profile = ExecutionProfileV5(5, "next_open", "open_then_stop", "last_session_close",
                                 "half_spread_plus_market_impact_plus_commission_bps")
    friction = FrictionScenario("synthetic-zero-bps", 0, 0, 0)
    with paper_policy_fixtures.PITDataBundle(path, expected_sha256=inputs["bundle_sha256"],
                                            prices_provenance=provenance) as bundle:
        simulator = PortfolioSimulator(
            initial_capital=10000.0, max_positions=2, position_risk_pct=0.01, stop_loss_pct=0.08,
            technical_only=True, signal_every_n_days=1, enable_eviction=False,
            data_fetcher=provider, pit_bundle=bundle,
            identity_transition_contract=bundle.load_price_identity_transition_contract(provenance),
            policy_client_factory=lambda: historical, execution_profile=profile, friction_scenario=friction,
        )
        result = simulator.run(list(data["tradables"]), start_date=data["evaluation_start"],
                               end_date=data["next_execution_session"], history_start_date=data["sessions"][0])
        assert len(result.equity_curve) == 30
        assert provider.calls == []
        target = [call for call in historical.calls if call.method == "evaluate_entry"
                  and call.snapshot.base.market.session == data["completed_session"]]
        assert len(target) == len(data["tradables"]) == 10
        actual_by_symbol = dict(zip(data["tradables"], target, strict=True))
        assert all(actual_by_symbol[symbol].snapshot.base.technical_eligible
                   and actual_by_symbol[symbol].decision.qualified for symbol in TRADABLES)
        assert all(not actual_by_symbol[symbol].decision.qualified
                   for symbol in data["tradables"] if symbol not in TRADABLES)
        assert all(not call.decision.qualified for call in historical.calls if call.method == "evaluate_entry"
                   and call.snapshot.base.market.session != data["completed_session"])
        features, rs, completion = current_features(bundle, data, availability, target[0].snapshot.base.market)
        assert set(rs) == set(data["tradables"])
        assert len({rs[symbol] for symbol in TRADABLES}) == 3
        assert features.market_context.to_canonical_json() == target[0].snapshot.base.market.to_canonical_json()
        for symbol, call in actual_by_symbol.items():
            assert rs[symbol] == call.snapshot.base.rs_score
            assert asdict(features.entry_features[symbol]) == asdict(call.snapshot.features)

    buys = result.fill_log.loc[result.fill_log["Action"] == "BUY"]
    assert tuple(buys["Ticker"]) == expected_symbols
    assert list(buys["Quantity"]) == [quantity] * capacity
    assert list(buys["ExecutionPrice"]) == [100.0] * capacity
    assert all(trade.stop_price == 95.0 for trade in result.trades)
    assert result.execution_diagnostics["capacity_truncated_signals"] == 3 - capacity
    api = owner101._policy_api()
    guard = owner101._guard_profile(api, maximum_positions=2, maximum_stop_distance_fraction=0.08)
    deployment = owner101._identity(descriptor, features, guard,
                                    account_id="offline-paper-account-v1", store_id="parity-" + variant)
    store = owner101._active_store(tmp_path, deployment, guard.guard_id)
    source_clock = features.decision_clock
    clock = DecisionClock(source_clock.completion_evidence.exchange_timezone,
                          source_clock.completed_session, source_clock.as_of_cutoff,
                          source_clock.next_eligible_session, source_clock.next_eligible_session, source_clock.valuation_time)
    account = reconcile_actual(store, deployment, clock, label="flat-pretrade")
    assert account.pending_entry_count == 0 and account.reserved_buy_cash == 0
    assert account.equity == account.settled_cash == account.available_cash == 10000
    monkeypatch.setattr("core.execution_store.settings.EXECUTION_STORE_DB_PATH", str(store.db_path))
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr("auto_trader.settings.ENTRY_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr("auto_trader._is_market_open", lambda: True)
    clear_workflow_registry()
    manager = OrderManager(paper=True, policy_store=store)
    submissions, stop_calls, durable_decisions = [], [], []

    def submit(**kwargs):
        row = dict(kwargs, broker_order_id="parity-buy-" + kwargs["symbol"])
        submissions.append(row)
        return OrderResult(True, row["broker_order_id"], kwargs["symbol"], "buy", kwargs["qty"],
                           client_order_id=kwargs["client_order_id"])

    def protect(**kwargs):
        stop_calls.append(kwargs)
        return ProtectiveStopResult(True, f"parity-stop-{len(stop_calls)}", kwargs["symbol"], kwargs["qty"],
                                    kwargs["stop_price_override"], "submitted",
                                    client_order_id=f"parity-stop-client-{len(stop_calls)}")

    monkeypatch.setattr("core.order_manager.submit_bracket_buy", submit)
    monkeypatch.setattr("core.order_execution.ensure_protective_stop", protect)
    paper = RecordingSimulatorPolicy(descriptor, frozen)
    parity_evidence["paper_calls"] = paper.canonical_records
    original_call = FrozenPolicyClient._call

    def record_call(client, name, snapshot, expected_snapshot, expected_result):
        if name == "evaluate_entry":
            matches = [call for call in target if call.snapshot.base.rs_score == snapshot.base.rs_score]
            assert len(matches) == 1
            # Complete comparison occurs before the paper policy is invoked.
            assert snapshot.to_canonical_json() == matches[0].snapshot.to_canonical_json()
        decision = original_call(client, name, snapshot, expected_snapshot, expected_result)
        return paper._record(name, snapshot, decision)

    monkeypatch.setattr(FrozenPolicyClient, "_call", record_call)
    original_record = store.record_decision

    def record_decision(identity, **kwargs):
        result = original_record(identity, **kwargs)
        durable_decisions.append({"decision_id": identity.decision_id, "category": identity.category.value,
                                  **kwargs})
        return result

    monkeypatch.setattr(store, "record_decision", record_decision)
    candidates = tuple(api.PaperPolicyCandidate(
        security_id="security:" + symbol, symbol=symbol, base_snapshot=actual_by_symbol[symbol].snapshot.base,
        reference_price=Decimal("100.00"), price_source="synthetic-next-session-open",
        tick_size=Decimal("0.01"), lot_size=Decimal("0.1"), minimum_quantity=Decimal("0.1"), is_breakout=True,
    ) for symbol in data["tradables"])
    orchestrator = api.PaperPolicyOrchestrator(
        policy_store=store, bundle_path=policy_path, paper_account_environment_id=deployment.paper_account_environment_id,
        feature_context=features, account=account, guard_profile=guard, order_manager=manager,
        active_symbols=(), candidates=candidates, open_position_count=0,
    )
    outcome = run_auto_trader(dry_run=False, skip_exits=True, execution_ready=lambda: True,
                              policy_orchestrator=orchestrator)
    assert outcome.entered == expected_symbols and outcome.exited == ()
    assert tuple(row["symbol"] for row in submissions) == expected_symbols
    assert [row["qty"] for row in submissions] == [quantity] * capacity
    paper_entries = [call for call in paper.calls if call.method == "evaluate_entry"]
    assert len(paper_entries) == 10
    for call in paper_entries:
        previous = next(item for item in target if item.snapshot.base.rs_score == call.snapshot.base.rs_score)
        assert call.decision.to_canonical_json() == previous.decision.to_canonical_json()
    historical_capacity = next(call for call in historical.calls if call.method == "recommend_capacity"
                               and call.snapshot.base.market.session == data["completed_session"])
    paper_capacity = next(call for call in paper.calls if call.method == "recommend_capacity")
    assert historical_capacity.decision == paper_capacity.decision
    assert paper_capacity.decision.max_positions == capacity
    assert historical_capacity.snapshot.portfolio.pending_entry_count == 3
    assert paper_capacity.snapshot.portfolio.pending_entry_count == 0
    allocations = [call for call in historical.calls if call.method == "recommend_allocation"]
    paper_allocations = [call for call in paper.calls if call.method == "recommend_allocation"]
    assert len(allocations) == len(paper_allocations) == capacity
    allocation_differences = []
    for index, (left, right) in enumerate(zip(allocations, paper_allocations, strict=True)):
        assert left.decision == right.decision
        assert left.decision.risk_fraction == risk_fraction
        assert left.decision.notional_fraction_cap == notional_fraction
        assert left.decision.stop_distance_fraction == 0.05
        assert left.snapshot.base.projected_cash_after_eviction == right.snapshot.base.projected_cash_after_eviction
        assert left.snapshot.base.pending_entries_remaining == right.snapshot.base.pending_entries_remaining == capacity - index
        assert left.snapshot.portfolio.pending_entry_count == capacity - index
        assert right.snapshot.portfolio.pending_entry_count == capacity - index
        assert right.snapshot.portfolio.gross_exposure_fraction == 0
        allocation_differences.append(_differences(left.snapshot.to_primitive(), right.snapshot.to_primitive()))
    assert allocations[0].snapshot.base.gross_exposure_before == math.ulp(1.0)
    assert paper_allocations[0].snapshot.base.gross_exposure_before == float(Decimal("10000") * Decimal(str(math.ulp(1.0))))
    actions = []
    for row in submissions:
        workflow = get_workflow(row["client_order_id"])
        action_id = next(item.details["signal"]["logical_action_id"] for item in workflow.transitions
                         if item.event == "signal_accepted")
        action = store.load_action_intent(action_id)
        linked = store.load_decision_record(action.decision.decision_id)
        assert linked.effective_action_payload["entry_plan"]["order_type"] == "limit"
        assert row["limit_price"] == float(action.reservation_price)
        assert action.requested_quantity == Decimal(quantity)
        assert action.reservation_price == 100 and action.reservation_stop_price == 95
        assert action.risk_per_unit == 5
        actions.append(action)
    lifecycle = _lifecycle(monkeypatch, store, deployment, clock, manager, submissions, actions, stop_calls)
    parity_evidence.update({
        "status": "assertions_completed",
        "variant": variant, "scope": "fixed-policy technical-only engineering parity; historical base-fact replay",
        "model_limits": ["historical next-open full fills versus paper asynchronous partial fills",
                         "actual historical holdings versus unfilled paper reservations",
                         "queue counts versus actual commitments; raw zero-exposure epsilon conventions",
                         "allocation ranking scores may reflect historical decision ranks versus replayed base facts",
                         "synthetic declared sessions; no scanner extraction, profitability or P&L parity claim"],
        "inputs": inputs, "completion": completion, "policy_sources": dict(frozen.source_sha256),
        "policy_manifest_sha256": digest(policy_path / "manifest.json"),
        "policy_artifact_id": descriptor.policy_artifact_id, "capability_manifest_id": descriptor.capability_manifest_id,
        "runtime_identity": descriptor.runtime_identity, "guard_id": guard.guard_id,
        "execution_profile": profile.to_primitive(), "friction": asdict(friction),
        "historical_calls": historical.canonical_records, "paper_calls": paper.canonical_records,
        "capacity_differences": _differences(historical_capacity.snapshot.to_primitive(), paper_capacity.snapshot.to_primitive()),
        "allocation_differences": allocation_differences, "selected_symbols": list(expected_symbols),
        "historical_buy_fills": buys.to_dict(orient="records"), "durable_decisions": durable_decisions,
        "lifecycle": lifecycle, "provider_calls": provider.calls,
    })
