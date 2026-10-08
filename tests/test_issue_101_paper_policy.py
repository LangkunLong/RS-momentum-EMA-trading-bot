"""Focused offline evidence for the activated issue #101 paper adapter."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


_MODULES = ("entry", "risk", "position", "exit")


def _policy_sources(*, floor: float = 0.0, rank_sign: float = 1.0, capacity: int = 3,
                    risk_fraction: float = 0.01, stop_fraction: float = 0.05,
                    notional_cap: float = 0.10, market_permitted: bool = True,
                    stateful_allocation: bool = False) -> dict[str, str]:
    allocation_body = (
        '''remaining = snapshot.base.pending_entries_remaining
    projected_cash_fraction = snapshot.base.projected_cash_after_eviction / snapshot.base.portfolio_equity_at_entry_open
    return AllocationDecision(remaining / 100.0, 0.10, min(0.75, projected_cash_fraction))'''
        if stateful_allocation
        else f'''return AllocationDecision({risk_fraction!r}, {stop_fraction!r}, {notional_cap!r})'''
    )
    return {
        "entry": f'''from core.strategy_policy.contracts import EntryDecision

def evaluate_entry(snapshot):
    score = float(snapshot.base.rs_score or 0.0)
    eligible = score >= {floor!r}
    return EntryDecision(eligible, {market_permitted!r}, ({rank_sign!r} * score, 0.0), () if eligible else ("fixture_floor",))
''',
        "risk": f'''from core.strategy_policy.contracts import CapacityDecision, AllocationDecision, EvictionDecision

def recommend_capacity(snapshot):
    return CapacityDecision({capacity!r}, False)

def recommend_allocation(snapshot):
    {allocation_body}

def select_eviction(snapshot):
    return EvictionDecision(None)
''',
        "position": '''from core.strategy_policy.contracts_v3 import AddOnDecisionV3

def evaluate_add_on(snapshot):
    return AddOnDecisionV3(False, 0.0, None, "fixture_no_add")
''',
        "exit": '''from core.strategy_policy.contracts import ExitDecision

def evaluate_exit(snapshot):
    return ExitDecision((), None, False, snapshot.scale_out_tier, snapshot.breakeven_armed, snapshot.ema_trailing_active)
''',
    }


def _write_policy_bundle(
    directory: Path,
    *,
    feature_contract_id: str = "feature-contract:fixture-v1",
    feature_calculator_identity: str = "feature-calculator:fixture-v1",
    sources: dict[str, str] | None = None,
    required_features: tuple[str, ...] = (),
) -> Path:
    directory.mkdir(parents=True)
    source_map = _policy_sources() if sources is None else sources
    module_hashes = {}
    for module_name in _MODULES:
        raw = source_map[module_name].encode("utf-8")
        (directory / f"{module_name}.py").write_bytes(raw)
        module_hashes[module_name] = hashlib.sha256(raw).hexdigest()
    manifest = {
        "schema_version": 1,
        "interface_version": "3",
        "feature_contract_id": feature_contract_id,
        "feature_calculator_identity": feature_calculator_identity,
        "module_sha256": module_hashes,
        "capabilities": {
            "required_entry_features": list(required_features),
            "optional_entry_features": [name for name in ("sector_rs",) if name not in required_features],
            "unsupported_entry_features": [],
            "supported_actions": ["allocation", "capacity", "entry"],
            "unsupported_actions": ["addition", "exit", "replacement"],
            "exports": {
                "entry": ["evaluate_entry"],
                "risk": ["recommend_capacity", "recommend_allocation", "select_eviction"],
                "position": ["evaluate_add_on"],
                "exit": ["evaluate_exit"],
            },
        },
        "immutable_constraints": {"fixture_policy_version": "v1"},
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    return directory


def test_frozen_policy_loader_hashes_and_dispatches_exact_four_module_bundle(tmp_path: Path) -> None:
    try:
        frozen_bundle = importlib.import_module("core.strategy_policy.frozen_bundle")
    except ModuleNotFoundError:
        pytest.fail("frozen policy bundle loader is not implemented")

    bundle_path = _write_policy_bundle(tmp_path / "fixed-policy")
    descriptor = frozen_bundle.inspect_frozen_policy_bundle(bundle_path)
    bundle = frozen_bundle.load_frozen_policy_bundle(
        bundle_path,
        expected_identity=_loader_identity(descriptor),
    )

    assert bundle.interface_version == "3"
    assert bundle.policy_artifact_id.startswith("policy-artifact:sha256:")
    assert bundle.capability_manifest_id.startswith("capability-manifest:sha256:")
    assert bundle.source_sha256 == tuple((name, hashlib.sha256((bundle_path / f"{name}.py").read_bytes()).hexdigest()) for name in _MODULES)
    assert tuple(bundle.client.exports) == (
        "evaluate_entry",
        "recommend_capacity",
        "recommend_allocation",
        "select_eviction",
        "evaluate_add_on",
        "evaluate_exit",
    )

    (bundle_path / "risk.py").write_text("# altered after freezing\n", encoding="utf-8")
    with pytest.raises(frozen_bundle.FrozenPolicyBundleError, match="hash"):
        frozen_bundle.load_frozen_policy_bundle(
            bundle_path,
            expected_identity=_loader_identity(descriptor),
        )


def test_wrong_selected_bundle_and_invalid_identity_execute_no_module_body(tmp_path, monkeypatch) -> None:
    import builtins

    from core.strategy_policy.frozen_bundle import FrozenPolicyBundleError, inspect_frozen_policy_bundle, load_frozen_policy_bundle

    selected_path = _write_policy_bundle(tmp_path / "selected-policy")
    selected_descriptor = inspect_frozen_policy_bundle(selected_path)
    selected_identity = _loader_identity(selected_descriptor)
    wrong_sources = _policy_sources()
    wrong_sources["entry"] = '''print("UNSELECTED_POLICY_INITIALIZER_EXECUTED")
from core.strategy_policy.contracts import EntryDecision

def evaluate_entry(snapshot):
    return EntryDecision(True, True, (1.0, 0.0), ())
'''
    wrong_path = _write_policy_bundle(tmp_path / "wrong-policy", sources=wrong_sources)
    exec_calls = []
    real_exec = builtins.exec

    def tracked_exec(*args, **kwargs):
        exec_calls.append(args[0] if args else None)
        return real_exec(*args, **kwargs)

    monkeypatch.setattr(builtins, "exec", tracked_exec)
    with pytest.raises(FrozenPolicyBundleError, match="does not match persisted deployment identity"):
        load_frozen_policy_bundle(wrong_path, expected_identity=selected_identity)
    assert exec_calls == []
    with pytest.raises(FrozenPolicyBundleError, match="expected deployment identity is invalid"):
        load_frozen_policy_bundle(wrong_path, expected_identity=object())
    assert exec_calls == []
    wrong_descriptor = inspect_frozen_policy_bundle(wrong_path)
    with pytest.raises(FrozenPolicyBundleError, match="top-level initializer"):
        load_frozen_policy_bundle(wrong_path, expected_identity=_loader_identity(wrong_descriptor))
    assert exec_calls == []


def test_auto_trader_exposes_explicit_policy_runtime_route() -> None:
    import inspect
    from types import SimpleNamespace

    from auto_trader import run_auto_trader

    assert "policy_orchestrator" in inspect.signature(run_auto_trader).parameters

    class SelectedPolicy:
        def __init__(self) -> None:
            self.calls = []

        def run(
            self,
            *,
            dry_run: bool,
            skip_entries: bool,
            skip_exits: bool,
            execution_ready=None,
            market_open_check=None,
            market_hours_required: bool = False,
        ):
            self.calls.append((dry_run, skip_entries, skip_exits, execution_ready, market_open_check, market_hours_required))
            return SimpleNamespace(entered=("AAA",), exited=())

    selected = SelectedPolicy()
    result = run_auto_trader(
        dry_run=True,
        skip_exits=True,
        policy_orchestrator=selected,
    )
    assert result.entered == ("AAA",)
    assert selected.calls == [(True, False, True, None, None, False)]


def _policy_api():
    try:
        return importlib.import_module("core.strategy_policy.paper_orchestration")
    except ModuleNotFoundError:
        pytest.fail("paper policy orchestration is not implemented")


def _feature_fixture(tmp_path: Path):
    from tests.paper_policy_fixtures import build_feature_fixture

    tmp_path.mkdir(parents=True, exist_ok=True)
    return build_feature_fixture(tmp_path)


def _account_reconciliation(features, *, gross_exposure: float = 100.0, pending_entries: int = 1):
    from core.strategy_policy.account_reconciliation import AccountReconciliation
    from core.strategy_policy.contracts_v3 import PortfolioFeaturesV3

    source_clock = features.decision_clock
    equity = 10_000.0
    open_risk = 10.0 if gross_exposure else 0.0
    reserved_cash = 50.0 if pending_entries else 0.0
    reserved_risk = 2.0 if pending_entries else 0.0
    gross_fraction = gross_exposure / equity
    portfolio = PortfolioFeaturesV3(
        gross_fraction,
        0.0,
        open_risk / equity,
        pending_entries,
        (("fixture-sector", gross_fraction),) if gross_exposure else (),
        (("fixture-industry", gross_fraction),) if gross_exposure else (),
    )
    settled_cash = equity - gross_exposure
    return AccountReconciliation(
        snapshot_id="offline-account-snapshot-v1",
        completed_session=source_clock.completed_session,
        as_of_cutoff=source_clock.as_of_cutoff,
        next_execution_session=source_clock.next_eligible_session,
        valuation_time=source_clock.valuation_time,
        ready=True,
        findings=(),
        equity=equity,
        settled_cash=settled_cash,
        reserved_buy_cash=reserved_cash,
        available_cash=max(0.0, settled_cash - reserved_cash),
        reserved_buy_risk=reserved_risk,
        pending_entry_count=pending_entries,
        pending_sell_count=0,
        gross_exposure=gross_exposure,
        open_position_risk=open_risk,
        sector_exposures=(),
        industry_exposures=(),
        portfolio_features=portfolio,
        total_committed_risk=open_risk + reserved_risk,
    )


def _candidate_entry_snapshot(*, market, rs_score: float, canslim_score: float):
    from core.strategy_policy.contracts import EntrySnapshot

    values: dict[str, object] = {
        "technical_only": False,
        "require_proper_base": False,
        "c_score": 0.8,
        "a_score": 0.8,
        "n_score": 0.8,
        "s_score": 0.8,
        "l_score": 0.9,
        "i_score": 0.5,
        "m_score": 0.8,
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "rs_score": 85.0,
        "canslim_score": 80.0,
        "entry_composite_score": 75.0,
        "technical_score": 75.0,
        "institutional_data_available": True,
        "event_close": 105.0,
        "prior_close": 100.0,
        "event_volume": 150.0,
        "prior_average_volume_50": 100.0,
        "pivot": 100.0,
        "volume_ratio": 1.5,
        "extension": 0.05,
        "price_advanced": True,
        "has_volume_surge": True,
        "in_buy_zone": True,
        "technical_eligible": True,
        "technical_blocking_reasons": (),
        "has_power_gap_today": False,
        "require_bullish_market": True,
        "market_is_bullish": True,
        "cash_deployment_override": False,
        "use_stateful_regime_gate": False,
        "regime_allows_entries": True,
        "market": market,
    }
    values.update(rs_score=rs_score, canslim_score=canslim_score)
    return EntrySnapshot(**values)  # type: ignore[arg-type]


def _candidate_inputs(features, price: str = "100.03"):
    from decimal import Decimal

    from core.strategy_policy.paper_orchestration import PaperPolicyCandidate

    scores = (90.0, 70.0, 50.0)
    return tuple(
        PaperPolicyCandidate(
            security_id=f"security:{symbol}",
            symbol=symbol,
            base_snapshot=_candidate_entry_snapshot(
                market=features.market_context,
                rs_score=scores[index],
                canslim_score=80.0 - index,
            ),
            reference_price=Decimal(price),
            price_source="synthetic_recorded_quote",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("0.1"),
            minimum_quantity=Decimal("0.1"),
            is_breakout=index == 0,
        )
        for index, symbol in enumerate(features.candidate_symbols)
    )


def test_candidate_inputs_do_not_import_backtest_engine_or_query_plotly_metadata(monkeypatch):
    import builtins
    import importlib.metadata
    from types import SimpleNamespace

    from core.strategy_policy.contracts import BenchmarkContextV1, MarketContextV1

    market = MarketContextV1(
        schema_version=1,
        session="2026-08-27",
        oneil_regime="confirmed_uptrend",
        distribution_days=2,
        follow_through=False,
        benchmarks=tuple(
            BenchmarkContextV1(symbol, 0.05, 0.10, 0.20)
            for symbol in ("SPY", "QQQ", "IWM")
        ),
        active_constituent_count=100,
        breadth_above_50_fraction=0.60,
        breadth_50_coverage_fraction=0.90,
        breadth_above_200_fraction=0.55,
        breadth_200_coverage_fraction=0.80,
        median_rs_score=72.0,
        rs_at_least_80_fraction=0.40,
        rs_coverage_fraction=0.95,
    )
    features = SimpleNamespace(
        market_context=market,
        candidate_symbols=("NVDA", "AAPL", "MSFT"),
    )
    observed_imports = []
    plotly_metadata_queries = []
    original_import = builtins.__import__
    original_version = importlib.metadata.version

    def observe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (
            name in {"tests.test_strategy_policy", "core.backtest_engine"}
            or name == "plotly"
            or name.startswith("plotly.")
        ):
            observed_imports.append(name)
        return original_import(name, globals, locals, fromlist, level)

    def observe_version(distribution_name, *args, **kwargs):
        if str(distribution_name).casefold() == "plotly":
            plotly_metadata_queries.append(distribution_name)
        return original_version(distribution_name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observe_import)
    monkeypatch.setattr(importlib.metadata, "version", observe_version)

    candidates = _candidate_inputs(features)

    assert observed_imports == []
    assert plotly_metadata_queries == []
    assert [candidate.base_snapshot.rs_score for candidate in candidates] == [90.0, 70.0, 50.0]
    assert [candidate.base_snapshot.canslim_score for candidate in candidates] == [80.0, 79.0, 78.0]
    assert all(candidate.base_snapshot.market is market for candidate in candidates)


def _loader_identity(bundle):
    from core.policy_execution_state import PolicyDeploymentIdentity
    from core.strategy_policy.runtime_identity import current_paper_runtime_identity

    return PolicyDeploymentIdentity(
        policy_artifact_id=bundle.policy_artifact_id,
        capability_manifest_id=bundle.capability_manifest_id,
        policy_interface_version=bundle.interface_version,
        feature_contract_id=bundle.feature_contract_id,
        feature_calculator_id=bundle.feature_calculator_identity,
        source_revision="offline-loader-fixture-v1",
        runtime_identity=current_paper_runtime_identity(),
        execution_profile_id="offline-loader-profile-v1",
        paper_account_environment_id="offline-loader-account-v1",
        store_identity="offline-loader-store-v1",
    )


def _identity(bundle, features, guard_profile, *, account_id: str, store_id: str):
    from core.policy_execution_state import PolicyDeploymentIdentity
    from core.strategy_policy.runtime_identity import current_paper_runtime_identity

    return PolicyDeploymentIdentity(
        policy_artifact_id=bundle.policy_artifact_id,
        capability_manifest_id=bundle.capability_manifest_id,
        policy_interface_version="3",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_id=features.feature_calculator_identity,
        source_revision=features.source_revision,
        runtime_identity=current_paper_runtime_identity(),
        execution_profile_id=guard_profile.execution_profile_id,
        paper_account_environment_id=account_id,
        store_identity=store_id,
    )


def _active_store(tmp_path: Path, identity, guard_id: str):
    from core.policy_execution_store import PolicyExecutionStateStore

    store = PolicyExecutionStateStore(
        tmp_path / f"{identity.store_identity}.sqlite3",
        store_identity=identity.store_identity,
    )
    store.migrate()
    store.register_deployment_identity(
        identity,
        lifecycle="eligible",
        handler_identity=identity.policy_artifact_id,
        guard_id=guard_id,
    )
    store.set_active_generation(
        identity.paper_account_environment_id,
        expected_generation_id=None,
        expected_pointer_version=None,
        new_generation_id=identity.deployment_generation_id,
        readiness_evidence_ref="offline-policy-fixture-readiness",
        outgoing_entries_reconciled=True,
    )
    return store


class _FakeEntryConsumer:
    def __init__(self) -> None:
        self.calls = []
        self.handoffs = []
        self.broker_submissions = set()

    def submit_policy_entry(
        self,
        plan,
        *,
        logical_action_id: str,
        dry_run: bool = False,
        execution_ready=None,
        market_open_check=None,
    ):
        self.calls.append((plan, logical_action_id, dry_run))
        self.handoffs.append(logical_action_id)
        if not dry_run:
            if execution_ready is None or not bool(execution_ready()):
                return type("Submission", (), {"success": False, "error": "not_ready"})()
            if market_open_check is not None and not bool(market_open_check()):
                return type("Submission", (), {"success": False, "error": "market_closed"})()
            self.broker_submissions.add(logical_action_id)
        return type("Submission", (), {"success": True})()


def _guard_profile(api, *, maximum_positions: int = 2,
                   maximum_position_risk_fraction: float = 0.01,
                   maximum_stop_distance_fraction: float = 0.10,
                   maximum_notional_fraction: float = 0.10,
                   maximum_total_risk_fraction: float = 0.05,
                   maximum_notional_amount=None,
                   allow_notional_capping: bool = True):
    from dataclasses import replace

    profile = api.PaperPolicyGuardProfile(
        guard_id="fixture-guard-pending-content-id",
        maximum_positions=maximum_positions,
        maximum_policy_positions=25,
        maximum_new_entries_per_cycle=3,
        maximum_position_risk_fraction=maximum_position_risk_fraction,
        maximum_stop_distance_fraction=maximum_stop_distance_fraction,
        maximum_notional_fraction=maximum_notional_fraction,
        maximum_total_risk_fraction=maximum_total_risk_fraction,
        maximum_notional_amount=maximum_notional_amount,
        allow_notional_capping=allow_notional_capping,
    )
    return replace(profile, guard_id=profile.content_addressed_guard_id)


def _orchestrator(api, *, store, bundle_path: Path, features, account,
                  candidates=None, guard=None, consumer=None, active_symbols=None):
    from dataclasses import replace
    from decimal import Decimal

    from core.policy_execution_state import PortfolioStateSnapshot
    from core.strategy_policy.paper_orchestration import _clock

    pointer = store.load_active_generation_pointer("offline-paper-account-v1")
    deployment = store.load_deployment_chain(pointer.active_generation_id).deployment_identity
    source_snapshot_id = account.snapshot_id
    portfolio_snapshot = PortfolioStateSnapshot(
        deployment_identity=deployment,
        clock=_clock(features),
        source_namespace="offline-paper-101-test-adapter",
        account_snapshot_id=source_snapshot_id,
        equity=Decimal(str(account.equity)),
        cash=Decimal(str(account.settled_cash)),
        gross_exposure=Decimal(str(account.gross_exposure)),
        open_risk=Decimal(str(account.open_position_risk)),
        portfolio_peak_equity=None,
        last_accepted_session=None,
    )
    account = replace(account, snapshot_id=store.record_portfolio_snapshot(portfolio_snapshot))
    reconciled_symbols = []
    if account.gross_exposure:
        reconciled_symbols.append("HELD")
    if account.pending_entry_count:
        reconciled_symbols.append("PENDING")
    return api.PaperPolicyOrchestrator(
        policy_store=store,
        bundle_path=bundle_path,
        paper_account_environment_id="offline-paper-account-v1",
        feature_context=features,
        account=account,
        candidates=_candidate_inputs(features) if candidates is None else candidates,
        guard_profile=_guard_profile(api) if guard is None else guard,
        order_manager=_FakeEntryConsumer() if consumer is None else consumer,
        open_position_count=1 if account.gross_exposure else 0,
        active_symbols=tuple(reconciled_symbols) if active_symbols is None else active_symbols,
    )


def test_two_frozen_policies_drive_rank_capacity_allocation_and_auto_trader(tmp_path, monkeypatch) -> None:
    from auto_trader import run_auto_trader
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features)
    guard = _guard_profile(api, maximum_positions=4)
    sources_a = _policy_sources(rank_sign=1.0, capacity=5, risk_fraction=0.01, stop_fraction=0.05, notional_cap=0.10)
    sources_b = _policy_sources(rank_sign=-1.0, capacity=3, risk_fraction=0.002, stop_fraction=0.04, notional_cap=0.05)
    monkeypatch.setattr("auto_trader.require_paper_mode", lambda: None)
    monkeypatch.setattr("auto_trader.settings.ENTRY_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr("auto_trader._is_market_open", lambda: True)
    results = []
    consumers = []
    stores = []
    for suffix, sources in (("a", sources_a), ("b", sources_b)):
        bundle_path = _write_policy_bundle(
            tmp_path / f"policy-{suffix}",
            feature_contract_id=features.feature_contract_id,
            feature_calculator_identity=features.feature_calculator_identity,
            sources=sources,
        )
        descriptor = inspect_frozen_policy_bundle(bundle_path)
        deployment = _identity(
            descriptor,
            features,
            guard,
            account_id="offline-paper-account-v1",
            store_id=f"offline-policy-store-{suffix}",
        )
        store = _active_store(tmp_path, deployment, guard.guard_id)
        consumer = _FakeEntryConsumer()
        orchestrator = _orchestrator(
            api,
            store=store,
            bundle_path=bundle_path,
            features=features,
            account=account,
            guard=guard,
            consumer=consumer,
        )
        result = run_auto_trader(
            dry_run=False,
            skip_exits=True,
            execution_ready=lambda: True,
            policy_orchestrator=orchestrator,
        )
        results.append(result)
        consumers.append(consumer)
        stores.append(store)

    durable = []
    for store in stores:
        with sqlite3.connect(store.db_path) as connection:
            entries = connection.execute(
                "SELECT subject_id, guard_payload_json FROM policy_state_decisions "
                "WHERE decision_category='entry'"
            ).fetchall()
            capacity = connection.execute(
                "SELECT effective_action_payload_json FROM policy_state_decisions "
                "WHERE decision_category='capacity'"
            ).fetchone()
        entry_guards = [(symbol, json.loads(guard_json)) for symbol, guard_json in entries]
        ranked_entries = sorted(
            entry_guards,
            key=lambda item: (item[1]["rank_index"] is None, item[1]["rank_index"]),
        )
        ranked = tuple(
            symbol for symbol, guard_payload in ranked_entries if guard_payload["rank_index"] is not None
        )
        selected = tuple(
            symbol
            for symbol, guard_payload in ranked_entries
            if guard_payload["selected_under_capacity"]
        )
        durable.append((ranked, json.loads(capacity[0]), selected))
    assert durable[0][0] == ("security:AAA", "security:BBB", "security:CCC")
    assert durable[0][1]["maximum_positions"] == 4
    assert durable[0][2] == ("security:AAA", "security:BBB")
    assert durable[1][0] == ("security:CCC", "security:BBB", "security:AAA")
    assert durable[1][1]["maximum_positions"] == 3
    assert durable[1][2] == ("security:CCC",)
    assert consumers[0].calls[0][0].qty != consumers[1].calls[0][0].qty
    assert all(call[2] is False for consumer in consumers for call in consumer.calls)
    assert results[0].entered == ("AAA", "BBB")
    assert results[1].entered == ("CCC",)
    monkeypatch.setattr("auto_trader.scan_for_canslim_stocks", lambda **_kwargs: pytest.fail("legacy scanner fallback"))


def test_policy_caps_and_guard_precedence_are_durable_and_explicit(tmp_path) -> None:
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features)
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
        sources=_policy_sources(capacity=5, risk_fraction=0.02, stop_fraction=0.08, notional_cap=0.4),
    )
    guard = _guard_profile(
        api,
        maximum_positions=4,
        maximum_position_risk_fraction=0.005,
        maximum_stop_distance_fraction=0.03,
        maximum_notional_fraction=0.05,
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id="offline-policy-store-guards",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        candidates=_candidate_inputs(features),
        guard=guard,
        consumer=consumer,
    )
    result = orchestrator.run(
        dry_run=False,
        skip_entries=False,
        skip_exits=True,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )
    assert result.selected_symbols == ("AAA", "BBB")
    assert result.effective_position_limit == 4
    assert consumer.calls[0][0].qty <= 5.0
    assert consumer.calls[0][0].stop_loss_pct <= 0.03

    db_path = tmp_path / "offline-policy-store-guards.sqlite3"
    with sqlite3.connect(db_path) as connection:
        allocation_row = connection.execute(
            "SELECT guard_payload_json FROM policy_state_decisions WHERE decision_category='allocation'"
        ).fetchone()
    guard_payload = json.loads(allocation_row[0])
    assert guard_payload["precedence"] == list(api.ENTRY_GUARD_PRECEDENCE)
    assert {item["field"] for item in guard_payload["overrides"]} == {
        "risk_fraction",
        "stop_distance_fraction",
        "notional_fraction",
    }

    repeated = orchestrator.run(
        dry_run=False,
        skip_entries=False,
        skip_exits=True,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )
    assert repeated.selected_symbols == ("AAA", "BBB")
    assert len(consumer.calls) == 4


def test_absolute_notional_ceiling_is_separate_from_cash_and_precision(tmp_path) -> None:
    from decimal import Decimal

    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    cases = (
        ("allowed_absolute_cap", 100.0, Decimal("150"), True),
        ("disallowed_absolute_cap", 100.0, Decimal("150"), False),
        ("nonbinding_absolute_cap_cash_limited", 9500.0, Decimal("1500"), True),
    )
    for case, gross_exposure, amount_cap, allow_capping in cases:
        api = _policy_api()
        case_path = tmp_path / case
        features = _feature_fixture(case_path / "inputs")
        account = _account_reconciliation(features, gross_exposure=gross_exposure, pending_entries=0)
        guard = _guard_profile(
            api,
            maximum_positions=4,
            maximum_notional_fraction=0.10,
            maximum_notional_amount=amount_cap,
            allow_notional_capping=allow_capping,
        )
        bundle_path = _write_policy_bundle(
            case_path / "policy",
            feature_contract_id=features.feature_contract_id,
            feature_calculator_identity=features.feature_calculator_identity,
            sources=_policy_sources(risk_fraction=0.01, stop_fraction=0.05, notional_cap=0.10),
        )
        descriptor = inspect_frozen_policy_bundle(bundle_path)
        deployment = _identity(
            descriptor,
            features,
            guard,
            account_id="offline-paper-account-v1",
            store_id=f"offline-policy-store-{case}",
        )
        store = _active_store(case_path, deployment, guard.guard_id)
        consumer = _FakeEntryConsumer()
        result = _orchestrator(
            api,
            store=store,
            bundle_path=bundle_path,
            features=features,
            account=account,
            guard=guard,
            consumer=consumer,
        ).run(
            dry_run=False,
            execution_ready=lambda: True,
            market_open_check=lambda: True,
            market_hours_required=True,
        )
        with sqlite3.connect(store.db_path) as connection:
            row = connection.execute(
                "SELECT guard_payload_json, effective_action_payload_json FROM policy_state_decisions "
                "WHERE decision_category='allocation' AND subject_id='security:AAA'"
            ).fetchone()
        guard_payload = json.loads(row[0])
        effective_action = json.loads(row[1])
        assert float(guard_payload["requested"]["notional_amount"]) == 1000.0

        if case == "allowed_absolute_cap":
            assert guard_payload["outcome"] == "pass_with_overrides"
            assert float(guard_payload["effective"]["notional_amount"]) == 150.0
            assert guard_payload["amount_constraints"]["absolute_notional_ceiling"]["binding"] is True
            assert guard_payload["amount_constraints"]["available_cash"]["binding"] is False
            assert any(
                item["field"] == "maximum_notional_amount"
                and Decimal(item["requested"]) == Decimal("1000")
                and Decimal(item["effective"]) == Decimal("150")
                and item["reason"] == "host_absolute_notional_ceiling"
                for item in guard_payload["overrides"]
            )
            assert float(effective_action["entry_plan"]["notional"]) <= 150.0
            assert consumer.calls
        elif case == "disallowed_absolute_cap":
            assert guard_payload["outcome"] == "veto"
            assert guard_payload["veto_reason"] == "maximum_notional_amount_cap_not_permitted"
            assert guard_payload["amount_constraints"]["absolute_notional_ceiling"]["binding"] is True
            assert float(guard_payload["effective"]["notional_amount"]) == 150.0
            assert effective_action["entry_plan"] is None
            assert consumer.calls == []
            assert result.entered == ()
        else:
            assert guard_payload["amount_constraints"]["absolute_notional_ceiling"]["binding"] is False
            assert guard_payload["amount_constraints"]["available_cash"]["binding"] is True
            assert guard_payload["amount_constraints"]["available_cash"]["reason"] == "account_cash_solvency_ceiling"
            assert float(guard_payload["effective"]["notional_amount"]) == 500.0
            assert all(item["field"] != "maximum_notional_amount" for item in guard_payload["overrides"])
            assert float(effective_action["entry_plan"]["notional"]) <= 500.0
            assert consumer.calls
def test_missing_required_feature_blocks_before_any_decision_or_order(tmp_path) -> None:
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features)
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
        required_features=("sector_rs",),
    )
    guard = _guard_profile(api)
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id="offline-policy-store-missing-feature",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        candidates=_candidate_inputs(features),
        consumer=consumer,
    )
    with pytest.raises(api.PaperPolicyOrchestrationError, match="required policy feature"):
        orchestrator.run(
            dry_run=False,
            skip_entries=False,
            skip_exits=True,
            execution_ready=lambda: True,
            market_open_check=lambda: True,
            market_hours_required=True,
        )
    assert consumer.calls == []
    with sqlite3.connect(tmp_path / "offline-policy-store-missing-feature.sqlite3") as connection:
        decision_count = connection.execute("SELECT count(*) FROM policy_state_decisions").fetchone()[0]
    assert decision_count == 0


@pytest.mark.parametrize(
    ("case", "gross_exposure", "pending_entries", "want_selected"),
    (
        ("held-no-pending", 100.0, 0, ("AAA", "BBB")),
        ("held-one-pending", 100.0, 1, ("AAA",)),
    ),
)
def test_reconciled_held_accounts_with_and_without_pending_entries_reach_allocation(
    tmp_path, case, gross_exposure, pending_entries, want_selected
) -> None:
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    case_path = tmp_path / case
    features = _feature_fixture(case_path / "inputs")
    account = _account_reconciliation(
        features,
        gross_exposure=gross_exposure,
        pending_entries=pending_entries,
    )
    guard = _guard_profile(api, maximum_positions=3)
    bundle_path = _write_policy_bundle(
        case_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id=f"offline-policy-store-{case}",
    )
    store = _active_store(case_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=guard,
        consumer=consumer,
        active_symbols=("HELD", "PENDING") if pending_entries else ("HELD",),
    )
    result = orchestrator.run(
        dry_run=False,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )
    assert result.selected_symbols == want_selected
    assert result.entered == want_selected
    assert len(consumer.broker_submissions) == len(want_selected)

    with sqlite3.connect(case_path / f"offline-policy-store-{case}.sqlite3") as connection:
        rows = connection.execute(
            "SELECT policy_payload_json FROM policy_state_decisions "
            "WHERE decision_category='allocation' ORDER BY subject_id"
        ).fetchall()
    snapshots = [json.loads(row[0])["allocation_snapshot"] for row in rows]
    assert snapshots
    assert all(
        snapshot["portfolio"]["pending_entry_count"]
        == max(pending_entries, snapshot["base"]["pending_entries_remaining"])
        for snapshot in snapshots
    )
    assert all(snapshot["portfolio"]["gross_exposure_fraction"] == gross_exposure / 10_000.0 for snapshot in snapshots)


def test_allocation_policy_observes_advancing_candidate_queue_and_reservations(tmp_path) -> None:
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features, gross_exposure=100.0, pending_entries=0)
    guard = _guard_profile(
        api,
        maximum_positions=4,
        maximum_position_risk_fraction=0.03,
        maximum_notional_fraction=0.75,
        maximum_total_risk_fraction=0.07,
    )
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
        sources=_policy_sources(capacity=4, stateful_allocation=True),
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id="offline-policy-store-queue",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    result = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=guard,
        active_symbols=(),
    ).run(dry_run=True)
    assert result.selected_symbols == ("AAA", "BBB", "CCC")

    with sqlite3.connect(tmp_path / "offline-policy-store-queue.sqlite3") as connection:
        rows = connection.execute(
            "SELECT policy_payload_json, effective_action_payload_json FROM policy_state_decisions "
            "WHERE decision_category='allocation' ORDER BY subject_id"
        ).fetchall()
    snapshots = [json.loads(row[0])["allocation_snapshot"] for row in rows]
    plans = [json.loads(row[1])["entry_plan"] for row in rows]
    assert [snapshot["base"]["pending_entries_remaining"] for snapshot in snapshots] == [3, 2, 1]
    assert [snapshot["portfolio"]["pending_entry_count"] for snapshot in snapshots] == [3, 2, 1]
    assert [json.loads(row[0])["allocation_decision"]["risk_fraction"] for row in rows] == [0.03, 0.02, 0.01]
    projected_cash = [snapshot["base"]["projected_cash_after_eviction"] for snapshot in snapshots]
    projected_gross = [snapshot["base"]["projected_gross_exposure_after_eviction"] for snapshot in snapshots]
    assert projected_cash[0] > projected_cash[1] > projected_cash[2]
    assert projected_gross[0] < projected_gross[1] < projected_gross[2]
    assert [plan["quantity"] for plan in plans] == [30.0, 20.0, 10.0]


@pytest.mark.parametrize("persisted_status", ("intended", "submitted"))
def test_prior_session_open_entry_requires_reconciliation_before_fresh_selection(
    tmp_path, monkeypatch, persisted_status
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle
    from core.policy_execution_state import (
        ActionAttemptStatus,
        ActionOrderAttempt,
        ActionStatus,
        ProviderOrderReference,
    )

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features, gross_exposure=100.0, pending_entries=0)
    guard = _guard_profile(api, maximum_positions=3)
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id=f"offline-policy-store-prior-session-{persisted_status}",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=guard,
        consumer=consumer,
        active_symbols=(),
    )
    record_action_intent = store.record_action_intent

    def record_then_crash(*args, **kwargs):
        record_action_intent(*args, **kwargs)
        monkeypatch.setattr(store, "record_action_intent", record_action_intent)
        raise RuntimeError("simulated process loss after durable intent")

    monkeypatch.setattr(store, "record_action_intent", record_then_crash)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        orchestrator.run(
            dry_run=False,
            execution_ready=lambda: True,
            market_open_check=lambda: True,
            market_hours_required=True,
        )
    assert consumer.handoffs == []

    snapshot = store.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=orchestrator.account.snapshot_id,
    )
    projection = next(
        item for item in snapshot.action_projections if item.status is ActionStatus.INTENDED
    )
    prior_session = features.decision_clock.completed_session - timedelta(days=1)
    if persisted_status == "submitted":
        client_order_id = f"client-{projection.logical_action_id}"
        broker_order_id = "fixture-prior-session-order"
        source_digest = hashlib.sha256(b"prior-session-open-entry").hexdigest()
        attempt = ActionOrderAttempt(
            attempt_number=1,
            requested_quantity=projection.requested_quantity,
            status=ActionAttemptStatus.SUBMITTED,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
        )
        references = tuple(
            ProviderOrderReference(
                provider_id="fixture-broker",
                paper_account_environment_id=deployment.paper_account_environment_id,
                store_identity=deployment.store_identity,
                reference_kind=kind,
                external_order_id=external_id,
                attempt_number=1,
                source_payload_sha256=source_digest,
                first_seen_at_utc=features.decision_clock.valuation_time,
            )
            for kind, external_id in (
                ("client_order_id", client_order_id),
                ("broker_order_id", broker_order_id),
            )
        )
        projection = replace(
            projection,
            decision_session=prior_session,
            status=ActionStatus.SUBMITTED,
            order_attempts=(attempt,),
            provider_order_references=references,
        )
    else:
        projection = replace(projection, decision_session=prior_session)
    prior_session_snapshot = replace(
        snapshot,
        action_projections=tuple(
            projection if item.logical_action_id == projection.logical_action_id else item
            for item in snapshot.action_projections
        ),
    )
    monkeypatch.setattr(
        store,
        "load_policy_execution_snapshot",
        lambda **kwargs: prior_session_snapshot,
    )

    replay = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        candidates=_candidate_inputs(features, price="101.03"),
        guard=guard,
        consumer=consumer,
        active_symbols=(),
    ).run(
        dry_run=False,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )

    assert replay.entered == ()
    assert replay.ranked_symbols == ()
    assert replay.selected_symbols == ("AAA",)
    assert replay.guard_outcomes == (("AAA", "pending_entry_requires_reconciliation"),)
    assert consumer.handoffs == []
    assert consumer.broker_submissions == set()


def test_intended_action_is_replayed_after_crash_before_consumer_handoff(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle
    from core.policy_execution_store import PolicyExecutionStateStore
    from core.policy_execution_state import (
        ActionAttemptStatus,
        ActionOrderAttempt,
        ActionStatus,
        ProviderOrderReference,
    )

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features, gross_exposure=100.0, pending_entries=0)
    guard = _guard_profile(api, maximum_positions=3)
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id="offline-policy-store-intent-replay",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=guard,
        consumer=consumer,
        active_symbols=(),
    )
    record_action_intent = store.record_action_intent

    def record_then_crash(*args, **kwargs):
        record_action_intent(*args, **kwargs)
        monkeypatch.setattr(store, "record_action_intent", record_action_intent)
        raise RuntimeError("simulated process loss after durable intent")

    monkeypatch.setattr(store, "record_action_intent", record_then_crash)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        orchestrator.run(
            dry_run=False,
            execution_ready=lambda: True,
            market_open_check=lambda: True,
            market_hours_required=True,
        )

    assert consumer.handoffs == []
    with sqlite3.connect(store.db_path) as connection:
        action_row = connection.execute(
            "SELECT logical_action_id FROM policy_state_actions WHERE role='entry' AND status='intended'"
        ).fetchone()
        plan_row = connection.execute(
            "SELECT effective_action_payload_json FROM policy_state_decisions "
            "WHERE decision_category='allocation' AND subject_id='security:AAA'"
        ).fetchone()
    logical_action_id = action_row[0]
    original_plan = json.loads(plan_row[0])["entry_plan"]

    # Reopen the explicit store under fresh reconciled facts that expose the pending
    # reservation, leave one remaining slot, and mark AAA active. The changed quote
    # would conflict with the immutable allocation slot if fresh policy ran first.
    store = PolicyExecutionStateStore(
        tmp_path / f"{deployment.store_identity}.sqlite3",
        store_identity=deployment.store_identity,
    )
    replay_account = _account_reconciliation(features, gross_exposure=100.0, pending_entries=1)
    replay_account = replace(replay_account, snapshot_id="offline-account-snapshot-after-intent-v2")
    replay_orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=replay_account,
        candidates=_candidate_inputs(features, price="101.03"),
        guard=guard,
        consumer=consumer,
        active_symbols=("AAA",),
    )
    replay = replay_orchestrator.run(
        dry_run=False,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )
    assert replay.entered == ("AAA",)
    assert replay.selected_symbols == ("AAA",)
    assert len(consumer.handoffs) == 1
    assert consumer.handoffs == [logical_action_id]
    assert consumer.broker_submissions == {logical_action_id}
    recovered_plan = consumer.calls[0][0]
    assert recovered_plan.entry_price == original_plan["entry_price"] == 100.03
    assert recovered_plan.stop_price == original_plan["stop_price"]
    assert recovered_plan.qty == original_plan["quantity"]
    assert recovered_plan.position_value == original_plan["notional"]

    # Model the next fresh reconciliation after the consumer has made one
    # accepted submission. The action keeps its original IDs and plan; it must
    # be observed as pending without a second consumer/broker handoff or a
    # fresh same-session decision write.
    submitted_account = _account_reconciliation(features, gross_exposure=100.0, pending_entries=1)
    submitted_account = replace(
        submitted_account,
        snapshot_id="offline-account-snapshot-after-submit-v3",
        reserved_buy_cash=original_plan["notional"],
        reserved_buy_risk=original_plan["risk_amount"],
        available_cash=submitted_account.settled_cash - original_plan["notional"],
        total_committed_risk=submitted_account.open_position_risk + original_plan["risk_amount"],
    )
    submitted_orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=submitted_account,
        candidates=_candidate_inputs(features, price="101.03"),
        guard=guard,
        consumer=consumer,
        active_symbols=("AAA",),
    )
    portfolio_snapshot_id = submitted_orchestrator.account.snapshot_id
    original_snapshot_loader = store.load_policy_execution_snapshot
    canonical_snapshot = original_snapshot_loader(
        deployment_generation_id=deployment.deployment_generation_id,
        portfolio_snapshot_id=portfolio_snapshot_id,
    )
    stored_projection = next(
        projection
        for projection in canonical_snapshot.action_projections
        if projection.logical_action_id == logical_action_id
    )
    client_order_id = f"client-{logical_action_id}"
    broker_order_id = "fixture-broker-order-1"
    source_digest = hashlib.sha256(b"same-session-submitted-replay").hexdigest()
    attempt = ActionOrderAttempt(
        attempt_number=1,
        requested_quantity=stored_projection.requested_quantity,
        status=ActionAttemptStatus.SUBMITTED,
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
    )
    references = tuple(
        ProviderOrderReference(
            provider_id="fixture-broker",
            paper_account_environment_id=deployment.paper_account_environment_id,
            store_identity=deployment.store_identity,
            reference_kind=kind,
            external_order_id=external_id,
            attempt_number=1,
            source_payload_sha256=source_digest,
            first_seen_at_utc=features.decision_clock.valuation_time,
        )
        for kind, external_id in (
            ("client_order_id", client_order_id),
            ("broker_order_id", broker_order_id),
        )
    )
    submitted_projection = replace(
        stored_projection,
        status=ActionStatus.SUBMITTED,
        order_attempts=(attempt,),
        provider_order_references=references,
    )
    submitted_snapshot = replace(
        canonical_snapshot,
        action_projections=tuple(
            submitted_projection if projection.logical_action_id == logical_action_id else projection
            for projection in canonical_snapshot.action_projections
        ),
    )
    observed_snapshot_ids = []

    def load_submitted_snapshot(*, deployment_generation_id, portfolio_snapshot_id):
        observed_snapshot_ids.append(portfolio_snapshot_id)
        if (
            deployment_generation_id == deployment.deployment_generation_id
            and portfolio_snapshot_id == submitted_snapshot.portfolio_snapshot.portfolio_snapshot_id
        ):
            return submitted_snapshot
        return original_snapshot_loader(
            deployment_generation_id=deployment_generation_id,
            portfolio_snapshot_id=portfolio_snapshot_id,
        )

    monkeypatch.setattr(store, "load_policy_execution_snapshot", load_submitted_snapshot)
    with sqlite3.connect(store.db_path) as connection:
        decision_count_before = connection.execute(
            "SELECT count(*) FROM policy_state_decisions"
        ).fetchone()[0]
    submitted_replay = submitted_orchestrator.run(
        dry_run=False,
        execution_ready=lambda: True,
        market_open_check=lambda: True,
        market_hours_required=True,
    )
    with sqlite3.connect(store.db_path) as connection:
        decision_count_after = connection.execute(
            "SELECT count(*) FROM policy_state_decisions"
        ).fetchone()[0]
    assert observed_snapshot_ids == [portfolio_snapshot_id]
    assert submitted_replay.entered == ()
    assert submitted_replay.guard_outcomes == (("AAA", "pending_entry_already_submitted"),)
    assert decision_count_after == decision_count_before
    assert consumer.handoffs == [logical_action_id]
    assert consumer.broker_submissions == {logical_action_id}


@pytest.mark.parametrize(
    ("case", "ready", "market_open", "reason"),
    (
        ("readiness", False, True, "execution_not_ready"),
        ("market", True, False, "market_closed"),
        ("missing_callback", None, True, "execution_readiness_callback_missing"),
    ),
)
def test_auto_trader_host_vetoes_are_durable_and_block_policy_consumption(
    tmp_path, monkeypatch, case, ready, market_open, reason
) -> None:
    from auto_trader import run_auto_trader
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle

    api = _policy_api()
    case_path = tmp_path / case
    features = _feature_fixture(case_path / "inputs")
    account = _account_reconciliation(features)
    guard = _guard_profile(api)
    bundle_path = _write_policy_bundle(
        case_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    descriptor = inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id=f"offline-policy-store-{case}",
    )
    store = _active_store(case_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=guard,
        consumer=consumer,
    )
    monkeypatch.setattr("auto_trader.require_paper_mode", lambda: None)
    monkeypatch.setattr("auto_trader.settings.ENTRY_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr("auto_trader._is_market_open", lambda: market_open)
    kwargs = {} if ready is None else {"execution_ready": lambda: ready}
    if ready is None:
        with pytest.raises(api.PaperPolicyOrchestrationError, match="readiness callback"):
            run_auto_trader(
                dry_run=False,
                skip_exits=True,
                policy_orchestrator=orchestrator,
                **kwargs,
            )
    else:
        result = run_auto_trader(
            dry_run=False,
            skip_exits=True,
            policy_orchestrator=orchestrator,
            **kwargs,
        )
        assert result.entered == ()
    assert consumer.broker_submissions == set()
    with sqlite3.connect(case_path / f"offline-policy-store-{case}.sqlite3") as connection:
        guard_payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT guard_payload_json FROM policy_state_decisions "
                "WHERE decision_category='candidate_evaluation'"
            )
        ]
    assert any(payload.get("veto_reason") == reason for payload in guard_payloads)


def test_same_guard_label_with_changed_caps_is_rejected_and_recorded(tmp_path) -> None:
    from dataclasses import replace

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features)
    guard = _guard_profile(api)
    bundle_path = _write_policy_bundle(
        tmp_path / "policy",
        feature_contract_id=features.feature_contract_id,
        feature_calculator_identity=features.feature_calculator_identity,
    )
    descriptor = importlib.import_module("core.strategy_policy.frozen_bundle").inspect_frozen_policy_bundle(bundle_path)
    deployment = _identity(
        descriptor,
        features,
        guard,
        account_id="offline-paper-account-v1",
        store_id="offline-policy-store-stale-guard",
    )
    store = _active_store(tmp_path, deployment, guard.guard_id)
    consumer = _FakeEntryConsumer()
    stale_guard = replace(guard, maximum_notional_fraction=0.2)
    orchestrator = _orchestrator(
        api,
        store=store,
        bundle_path=bundle_path,
        features=features,
        account=account,
        guard=stale_guard,
        consumer=consumer,
    )

    with pytest.raises(api.PaperPolicyOrchestrationError, match="guard profile differs"):
        orchestrator.run(dry_run=True)
    assert consumer.calls == []
    with sqlite3.connect(tmp_path / "offline-policy-store-stale-guard.sqlite3") as connection:
        payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT guard_payload_json FROM policy_state_decisions "
                "WHERE decision_category='candidate_evaluation'"
            )
        ]
    assert any(payload.get("veto_reason") == "execution_profile_identity_mismatch" for payload in payloads)


def test_prepared_activation_and_rollback_keep_prior_generation_state_pinned(tmp_path) -> None:
    from decimal import Decimal

    from core.policy_execution_state import (
        ActionRole,
        ActionStatus,
        DecisionCategory,
        DecisionIdentity,
        DecisionSubjectType,
        OrderSide,
        build_action_intent,
    )
    from core.strategy_policy.frozen_bundle import inspect_frozen_policy_bundle, load_frozen_policy_bundle

    api = _policy_api()
    features = _feature_fixture(tmp_path / "inputs")
    account = _account_reconciliation(features)
    guard = _guard_profile(api, maximum_positions=4)
    bundles = {}
    deployments = {}
    for suffix, rank_sign in (("a", 1.0), ("b", -1.0)):
        bundle_path = _write_policy_bundle(
            tmp_path / f"policy-{suffix}",
            feature_contract_id=features.feature_contract_id,
            feature_calculator_identity=features.feature_calculator_identity,
            sources=_policy_sources(rank_sign=rank_sign, capacity=4),
        )
        descriptor = inspect_frozen_policy_bundle(bundle_path)
        deployment = _identity(
            descriptor,
            features,
            guard,
            account_id="offline-paper-account-v1",
            store_id="offline-policy-store-rollback",
        )
        bundle = load_frozen_policy_bundle(bundle_path, expected_identity=deployment)
        bundles[suffix] = (bundle_path, bundle)
        deployments[suffix] = deployment

    deployment_a = deployments["a"]
    deployment_b = deployments["b"]
    store = _active_store(tmp_path, deployment_a, guard.guard_id)
    store.register_deployment_identity(
        deployment_b,
        lifecycle="eligible",
        handler_identity=deployment_b.policy_artifact_id,
        guard_id=guard.guard_id,
    )
    pointer_a = store.load_active_generation_pointer(deployment_a.paper_account_environment_id)
    version_b = store.set_active_generation(
        deployment_a.paper_account_environment_id,
        expected_generation_id=pointer_a.active_generation_id,
        expected_pointer_version=pointer_a.pointer_version,
        new_generation_id=deployment_b.deployment_generation_id,
        readiness_evidence_ref="offline-policy-fixture-generation-b",
        outgoing_entries_reconciled=True,
    )

    clock = api._clock(features)
    opening_decision = DecisionIdentity.build(
        deployment=deployment_b,
        clock=clock,
        snapshot_sha256=hashlib.sha256(b"offline-generation-b-opening").hexdigest(),
        category=DecisionCategory.ENTRY,
        subject_type=DecisionSubjectType.SECURITY,
        subject_id="security:AAA",
    )
    store.record_decision(opening_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    opening = build_action_intent(
        decision=opening_decision,
        security_id="security:AAA",
        broker_symbol="AAA",
        role=ActionRole.ENTRY,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        reservation_price=Decimal("100"),
        reservation_price_basis="offline_fixture_quote",
        reservation_stop_price=Decimal("95"),
        risk_per_unit=Decimal("5"),
        risk_basis="offline_fixture_stop",
    )
    store.record_action_intent(opening, expected_version=None)
    store.record_cumulative_fill(
        opening.logical_action_id,
        1,
        provider_id="offline-fixture-provider",
        fill_event_id="offline-generation-b-fill",
        cumulative_quantity=Decimal("1"),
        cumulative_notional=Decimal("100"),
        cumulative_fees=Decimal("0"),
        payload_sha256=None,
        observed_at=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
        expected_action_version=0,
        expected_holding_version=None,
    )
    holding = store.load_holding_episode_for_action(opening.logical_action_id)

    exit_decision = DecisionIdentity.build(
        deployment=deployment_b,
        clock=clock,
        snapshot_sha256=hashlib.sha256(b"offline-generation-b-pending-exit").hexdigest(),
        category=DecisionCategory.EXIT,
        subject_type=DecisionSubjectType.HOLDING,
        subject_id=holding.holding_episode_id,
    )
    store.record_decision(exit_decision, policy_payload={}, guard_payload={}, effective_action_payload={})
    pending_exit = build_action_intent(
        decision=exit_decision,
        security_id="security:AAA",
        broker_symbol="AAA",
        holding_episode_id=holding.holding_episode_id,
        role=ActionRole.CLOSE,
        side=OrderSide.SELL,
        requested_quantity=Decimal("1"),
        status=ActionStatus.INTENDED,
    )
    store.record_action_intent(pending_exit, expected_version=None)

    pointer_b = store.load_active_generation_pointer(deployment_a.paper_account_environment_id)
    assert pointer_b.pointer_version == version_b
    store.set_active_generation(
        deployment_a.paper_account_environment_id,
        expected_generation_id=pointer_b.active_generation_id,
        expected_pointer_version=pointer_b.pointer_version,
        new_generation_id=deployment_a.deployment_generation_id,
        readiness_evidence_ref="offline-policy-fixture-rollback-a",
        outgoing_entries_reconciled=True,
    )

    assert store.load_active_generation(deployment_a.paper_account_environment_id) == deployment_a.deployment_generation_id
    assert store.load_holding_episode(holding.holding_episode_id).deployment_generation_id == deployment_b.deployment_generation_id
    projection = store.load_action_projection(pending_exit.logical_action_id)
    assert projection.deployment_generation_id == deployment_b.deployment_generation_id
    assert projection.status is ActionStatus.INTENDED

    consumer = _FakeEntryConsumer()
    rolled_back = _orchestrator(
        api,
        store=store,
        bundle_path=bundles["a"][0],
        features=features,
        account=account,
        guard=guard,
        consumer=consumer,
        active_symbols=("AAA", "PENDING"),
    ).run(dry_run=True)
    assert rolled_back.ranked_symbols == ("AAA", "BBB", "CCC")
    assert rolled_back.selected_symbols == ("BBB", "CCC")
    assert rolled_back.entered == ()
    assert consumer.calls == []
