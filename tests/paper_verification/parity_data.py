"""Finite synthetic inputs for the source-reviewed historical/paper comparison."""
from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from core.current_policy_inputs import (
    CurrentDecisionClockV1,
    RecordedExchangeSessionCompletionV1,
    build_current_feature_context_snapshot,
    derive_available_from_source_date,
)
from core.pit_feature_snapshot import build_entry_features_v3
from core.pit_provenance import pit_canonical_json, pit_canonical_json_sha256


INPUT_PATH = Path(__file__).parents[1] / "fixtures" / "paper_parity_input.json"
INPUT_SHA256 = "6085a26ec832039aaa8dd906801142bdf174107ad0cfe6d0f62283be2834d051"
TRADABLES = ("AAA", "BBB", "CCC")
SOURCE_SYMBOLS = ("AAA", "BBB", "CCC", "SPY", "QQQ", "IWM")
PEER_SYMBOLS = ("DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ")
TRACE_LIMIT = 2 * 1024 * 1024  # Two explicit variants: aggregate maximum 4 MiB.


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_data(root):
    from build_pit_bundle import _create_bundle_v3
    from tests.test_current_feature_context_adapter import _BASE_METADATA

    assert digest(INPUT_PATH) == INPUT_SHA256
    data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    assert len(data["sessions"]) == 360 <= 400
    assert data["symbols"] == [*SOURCE_SYMBOLS, *PEER_SYMBOLS]
    assert data["tradables"] == [*TRADABLES, *PEER_SYMBOLS]
    assert len(data["ohlcv"]) == 4680 <= 4800
    assert len(data["fundamentals"]) == 18
    assert data["sessions"][330] == data["evaluation_start"]
    assert data["sessions"][-2:] == [data["completed_session"], data["next_execution_session"]]
    root.mkdir()
    first, last = data["sessions"][0], data["sessions"][-1]
    # The reader requires a price at its 2020 warm-up boundary. These thirteen
    # synthetic bars are outside every 2024-25 measured feature/history window.
    anchor = "2020-01-02"
    assert anchor < first
    first_rows = [row for row in data["ohlcv"] if row[0] == first]
    assert len(first_rows) == len(data["symbols"]) == 13
    assert {row[1] for row in first_rows} == set(data["symbols"])
    anchor_rows = [(anchor, *row[1:]) for row in first_rows]
    assert len(anchor_rows) + len(data["ohlcv"]) == 4693 <= 4700
    contracts = {
        symbol: {"provider_symbol": symbol, "identity_asof": last,
                 "admitted_start": anchor, "admitted_end": last,
                 "chain_id": "parity_" + symbol.lower(), "continuity_kind": "same_issuer_rename",
                 "warmup_predecessor": None, "factor_anchor": True}
        for symbol in data["symbols"]
    }
    contract_sha = pit_canonical_json_sha256(contracts)
    identity_sha = pit_canonical_json_sha256({s: "parity_" + s.lower() for s in data["symbols"]})
    provenance = {"price_identity_request_contracts": contracts,
                  "price_identity_request_contracts_sha256": contract_sha,
                  "price_identity_map_sha256": identity_sha, "price_identity_transitions": []}
    provenance_path = root / "synthetic-prices-provenance.json"
    with provenance_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(pit_canonical_json(provenance) + "\n")
    metadata = {key: INPUT_SHA256 if value == "0" * 64 else value for key, value in _BASE_METADATA.items()}
    metadata.pop("source_universe")
    metadata.update({
        "bundle_kind": "canslim_pit_v3", "schema_version": "3", "data_cutoff": last,
        "warmup_start": anchor, "evaluation_start": data["evaluation_start"],
        "source_universes_json": pit_canonical_json(["nasdaq100", "russell2000", "sp500"]),
        "membership_source_kind": "normalized_three_universe_membership",
        "membership_revision_id": "synthetic-paper-parity-v1",
        "prices_provenance_sha256": digest(provenance_path),
        "price_identity_map_sha256": identity_sha,
        "price_identity_request_contracts_sha256": contract_sha,
        "price_identity_transitions_sha256": pit_canonical_json_sha256([]),
    })
    membership = [(first, "parity_aaa", "nasdaq100", 1), (first, "parity_aaa", "sp500", 1),
                  (first, "parity_bbb", "russell2000", 1), (first, "parity_ccc", "sp500", 1)]
    membership.extend((first, "parity_" + symbol.lower(), "sp500", 1)
                      for symbol in PEER_SYMBOLS)
    metadata["membership_source_sha256"] = pit_canonical_json_sha256(membership)
    sessions = tuple(date.fromisoformat(day) for day in data["sessions"])
    availability = {symbol: [] for symbol in data["tradables"]}
    fundamentals = []
    for row in data["fundamentals"]:
        available = derive_available_from_source_date(
            period_end=date.fromisoformat(row["period_end"]),
            source_public_date=date.fromisoformat(row["source_public_date"]),
            source_public_at=datetime.fromisoformat(row["source_public_at"]), exchange_sessions=sessions,
        )
        assert available.available_from_session > available.source_public_date
        availability[row["symbol"]].append(available)
        fundamentals.append((row["symbol"], row["statement_type"], row["period_end"],
                             available.available_from_session.isoformat(), None, row["diluted_eps"],
                             row["total_revenue"], None, None, None, None, None, None, None))
    industry = [(symbol, first, "synthetic_parity_group", 1, pit_canonical_json(list(TRADABLES)),
                 pit_canonical_json(["synthetic-parity-only"])) for symbol in TRADABLES]
    path = root / "synthetic-parity.sqlite3"
    _create_bundle_v3(path, metadata=metadata, membership=membership,
                      prices=[*anchor_rows, *data["ohlcv"]],
                      fundamentals=fundamentals, industry=industry)
    identities = {"input_sha256": INPUT_SHA256, "bundle_sha256": digest(path),
                  "provenance_sha256": digest(provenance_path),
                  "membership_sha256": pit_canonical_json_sha256(membership),
                  "fundamentals_sha256": pit_canonical_json_sha256(fundamentals),
                  "industry_sha256": pit_canonical_json_sha256(industry),
                  "sessions": data["sessions"], "ohlcv_rows": 4680,
                  "bundle_price_rows": 4693, "warmup_anchor_date": anchor,
                  "warmup_anchor_rows": 13, "fundamental_rows": 18}
    return data, path, provenance_path, availability, identities


def synthetic_clock(data):
    session = date.fromisoformat(data["completed_session"])
    next_session = date.fromisoformat(data["next_execution_session"])
    zone = ZoneInfo("America/New_York")
    close = datetime.combine(session, datetime.min.time(), tzinfo=zone).replace(hour=16)
    completion_payload = {"session_date": session.isoformat(), "exchange_timezone": "America/New_York",
                          "session_close_at": close.isoformat(), "source_identity": "synthetic-XNYS-close-v1"}
    completion = RecordedExchangeSessionCompletionV1(
        session, "America/New_York", close, completion_payload["source_identity"],
        pit_canonical_json_sha256(completion_payload),
    )
    clock = CurrentDecisionClockV1(
        session, close + timedelta(minutes=1), next_session,
        datetime.combine(next_session, datetime.min.time(), tzinfo=zone).replace(hour=9, minute=31), completion,
    )
    return clock, completion_payload


def current_features(bundle, data, availability, historical_market):
    from core.momentum_analysis import calculate_rs_snapshot

    clock, completion_payload = synthetic_clock(data)
    session = clock.completed_session
    start, end = pd.Timestamp(data["sessions"][0]), pd.Timestamp(session)
    histories = bundle.fetch_price_data(data["tradables"], start, end)
    closes = bundle.fetch_closes(data["symbols"], start, end)
    active = bundle.members_at(session)
    assert active == set(data["tradables"])
    rs = calculate_rs_snapshot(closes, end, eligible_tickers=active)
    missingness = {}
    for symbol in data["tradables"]:
        calculated = build_entry_features_v3(
            bundle=bundle, symbol=symbol, session=session, price_history=histories[symbol], rs_snapshot=rs,
        )
        missingness[symbol] = {
            field.name: {"state": "absent", "reason": "finite synthetic inventory lacks this observation"}
            for field in fields(calculated) if getattr(calculated, field.name) is None
        }
    features = build_current_feature_context_snapshot(
        bundle=bundle, decision_clock=clock, candidate_symbols=data["tradables"],
        price_history_by_symbol=histories, rs_snapshot=rs, market_closes=closes,
        oneil_regime=historical_market.oneil_regime,
        distribution_days=historical_market.distribution_days, follow_through=historical_market.follow_through,
        missingness=missingness, fundamental_availability=availability, unavailable_members={},
        source_revision="synthetic-paper-parity-v1",
    )
    return features, rs, completion_payload


def reconcile_actual(store, deployment, clock, *, label, cash=10000, gross=0, risk=0,
                     positions=(), orders=(), decision_slot_id="parity-pretrade-slot", decision_id="parity-pretrade"):
    from core.policy_execution_state import PortfolioStateSnapshot
    from core.strategy_policy.account_reconciliation import (
        AccountValuationClock, BrokerAccountSnapshot, policy_execution_state_to_projection, reconcile_account_snapshot,
    )

    portfolio = PortfolioStateSnapshot(
        deployment_identity=deployment, clock=clock, source_namespace="synthetic-parity-broker",
        account_snapshot_id=label, equity=Decimal("10000"), cash=Decimal(str(cash)),
        gross_exposure=Decimal(str(gross)), open_risk=Decimal(str(risk)), portfolio_peak_equity=Decimal("10000"),
        last_accepted_session=clock.decision_session,
    )
    store.record_portfolio_snapshot(portfolio)
    canonical = store.load_policy_execution_snapshot(
        deployment_generation_id=deployment.deployment_generation_id, portfolio_snapshot_id=portfolio.portfolio_snapshot_id,
    )
    broker = BrokerAccountSnapshot(
        deployment.paper_account_environment_id, decision_slot_id, decision_id,
        AccountValuationClock(clock.decision_session, clock.as_of_cutoff_at,
                             clock.next_execution_session, clock.account_valuation_at),
        10000.0, float(cash), 10000.0, clock.account_valuation_at, clock.account_valuation_at,
        tuple(positions), tuple(orders), "synthetic-parity-broker", label,
    )
    projection = policy_execution_state_to_projection(
        account=broker, portfolio_snapshot=canonical.portfolio_snapshot,
        action_projections=canonical.action_projections, holding_episodes=canonical.holding_episodes,
    )
    result = reconcile_account_snapshot(account=broker, projection=projection,
                                        maximum_balance_age=timedelta(minutes=15), maximum_mark_age=timedelta(minutes=15))
    assert result.ready, result.findings
    return result
