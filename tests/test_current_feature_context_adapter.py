"""Offline parity and pre-screen-boundary evidence for issue #98."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import string
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import build_pit_bundle as pit_bundle_builder
from core.canslim.entry_contract import MIN_RS_SCORE as LEGACY_RS_FLOOR
from core.pit_data import PITDataBundle, sha256_file
from core.pit_feature_snapshot import build_entry_features_v3
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_sha256,
)
from core.strategy_policy.market_context import build_market_context
from core.current_policy_inputs import (
    CurrentDecisionClockV1,
    RecordedExchangeSessionCompletionV1,
    derive_available_from_source_date,
    validate_normalized_availability,
    build_current_feature_context_snapshot,
)


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "issue98_recorded_current_snapshot.json"
_BASE_METADATA = {
    "bundle_kind": "canslim_pit_v2",
    "schema_version": "2",
    "data_cutoff": "2026-04-03",
    "evaluation_start": "2020-01-02",
    "warmup_start": "2020-01-01",
    "membership_source_sha256": "0" * 64,
    "prices_source_sha256": "0" * 64,
    "fundamentals_source_sha256": "0" * 64,
    "membership_provenance_sha256": "0" * 64,
    "prices_provenance_sha256": "0" * 64,
    "fundamentals_provenance_sha256": "0" * 64,
    "membership_source_kind": "synthetic_offline_fixture",
    "membership_revision_id": "issue98-fixture-v1",
    "membership_raw_sha256": "0" * 64,
    "membership_symbol_map_sha256": "0" * 64,
    "membership_security_names_sha256": "0" * 64,
    "prices_source_kind": "synthetic_offline_fixture",
    "prices_upstream_source_sha256": "0" * 64,
    "spy_trading_days_sha256": "0" * 64,
    "price_identity_map_sha256": "0" * 64,
    "price_identity_request_contracts_sha256": "0" * 64,
    "price_exclusion_count": "0",
    "price_exclusions_sha256": "0" * 64,
    "fundamentals_source_kind": "synthetic_offline_fixture",
    "fundamentals_submissions_archive_sha256": "0" * 64,
    "fundamentals_companyfacts_archive_sha256": "0" * 64,
    "fundamentals_identity_manifest_csv_sha256": "0" * 64,
    "non_tradable_reference_symbols_json": pit_canonical_json(
        list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    ),
    "non_tradable_reference_symbols_sha256": pit_canonical_json_sha256(
        list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    ),
    "source_universe": "sp500",
}


def _fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _synthetic_member_symbols() -> tuple[str, ...]:
    used = set(_fixture()["active_symbols"]) | set(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    available = (
        "".join(parts)
        for parts in itertools.product(string.ascii_uppercase, repeat=3)
        if "".join(parts) not in used
    )
    return tuple(itertools.islice(available, 492))


def _unavailable_active_members(active: tuple[str, ...], fixture: dict[str, Any]):
    candidates = set(fixture["candidate_symbols"])
    disposition = fixture["unavailable_member_disposition"]
    return {symbol: disposition for symbol in active if symbol not in candidates}


def _write_bundle(
    tmp_path: Path,
    *,
    include_future_filing: bool = True,
) -> tuple[Path, str, tuple[Any, ...]]:
    fixture = _fixture()
    session = date.fromisoformat(fixture["session"])
    members = (*fixture["active_symbols"], *_synthetic_member_symbols())
    metadata = dict(_BASE_METADATA)
    fundamentals = [dict(row) for row in fixture["fundamentals"]]
    exchange_sessions = tuple(
        timestamp.date()
        for timestamp in pd.bdate_range("2023-01-01", "2026-04-10")
    )
    availability = tuple(
        derive_available_from_source_date(
            period_end=date.fromisoformat(row["period_end"]),
            source_public_date=date.fromisoformat(row["source_public_date"]),
            source_public_at=datetime.fromisoformat(row["source_public_at"]),
            exchange_sessions=exchange_sessions,
        )
        for row in fundamentals
    )
    if not include_future_filing:
        paired = [
            (row, normalized)
            for row, normalized in zip(fundamentals, availability, strict=True)
            if normalized.available_from_session <= session
        ]
        fundamentals = [row for row, _normalized in paired]
        availability = tuple(normalized for _row, normalized in paired)

    path = tmp_path / f"issue98-{int(include_future_filing)}.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE membership (effective_date TEXT NOT NULL, ticker TEXT NOT NULL, member INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE price (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, total_stockholders_equity REAL, shares_outstanding REAL, held_percent_institutions REAL, institution_count INTEGER, prev_institution_count INTEGER)"
        )
        connection.executemany("INSERT INTO dataset_metadata VALUES (?, ?)", metadata.items())
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, 1)",
            (("2020-01-01", symbol) for symbol in members),
        )

        bars: list[tuple[str, str, float, float, float, float, float]] = []
        member_set = set(members)
        for symbol in (*members, *PIT_NON_TRADABLE_REFERENCE_SYMBOLS):
            if symbol not in member_set:
                bars.append(("2020-01-02", symbol, 9.95, 10.1, 9.9, 10.0, 1000.0))
            elif symbol not in fixture["price_series"]:
                bars.append(("2020-01-02", symbol, 9.95, 10.1, 9.9, 10.0, 1000.0))

        for symbol, spec in fixture["price_series"].items():
            sessions = pd.bdate_range(end=session, periods=spec["bars"])
            for index, timestamp in enumerate(sessions):
                close = spec["start_close"] + index * spec["daily_increment"]
                bars.append(
                    (
                        timestamp.date().isoformat(),
                        symbol,
                        close * 0.995,
                        close * 1.01,
                        close * 0.99,
                        close,
                        float(spec["volume"]),
                    )
                )
        connection.executemany("INSERT INTO price VALUES (?, ?, ?, ?, ?, ?, ?)", bars)

        fundamental_rows = []
        for row, normalized in zip(fundamentals, availability, strict=True):
            fundamental_rows.append(
                (
                    row["ticker"],
                    row["statement_type"],
                    row["period_end"],
                    normalized.available_from_session.isoformat(),
                    None,
                    row.get("diluted_eps"),
                    row.get("total_revenue"),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
        connection.executemany(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            fundamental_rows,
        )
        connection.commit()
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), availability


def _write_schema_v3_bundle(tmp_path: Path) -> tuple[Path, str, Path, tuple[Any, ...]]:
    fixture = _fixture()
    session = date.fromisoformat(fixture["session"])
    tickers = tuple(fixture["active_symbols"])
    lineages = {ticker: f"issue98_{ticker.lower()}" for ticker in tickers}
    identity_contracts: dict[str, dict[str, object]] = {}
    for ticker, lineage in (
        *lineages.items(),
        ("IWM", "issue98_ref_iwm"),
        ("QQQ", "issue98_ref_qqq"),
        ("SPY", "issue98_ref_spy"),
    ):
        identity_contracts[ticker] = {
            "provider_symbol": ticker,
            "identity_asof": "2026-04-03",
            "admitted_start": "2020-01-02",
            "admitted_end": "2026-04-03",
            "chain_id": lineage,
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": True,
        }
    identity_contract_sha256 = pit_canonical_json_sha256(identity_contracts)
    identity_map_sha256 = "b" * 64
    transitions: list[dict[str, object]] = []
    prices_provenance = {
        "price_identity_request_contracts": identity_contracts,
        "price_identity_request_contracts_sha256": identity_contract_sha256,
        "price_identity_map_sha256": identity_map_sha256,
        "price_identity_transitions": transitions,
    }
    provenance_path = tmp_path / "issue98-v3-prices-provenance.json"
    provenance_path.write_text(
        pit_canonical_json(prices_provenance) + "\n",
        encoding="utf-8",
    )

    metadata = dict(_BASE_METADATA)
    metadata.update(
        {
            "bundle_kind": "canslim_pit_v3",
            "schema_version": "3",
            "source_universes_json": pit_canonical_json(
                ["nasdaq100", "russell2000", "sp500"]
            ),
            "membership_source_kind": "normalized_three_universe_membership",
            "membership_revision_id": "issue98-controlled-v3-fixture",
            "prices_provenance_sha256": sha256_file(provenance_path),
            "price_identity_map_sha256": identity_map_sha256,
            "price_identity_request_contracts_sha256": identity_contract_sha256,
            "price_identity_transitions_sha256": pit_canonical_json_sha256(transitions),
        }
    )
    metadata.pop("source_universe")
    membership_universes = {
        "AAA": ("nasdaq100", "sp500"),
        "BBB": ("russell2000",),
        "CCC": ("sp500",),
    }
    membership = sorted(
        (
            "2020-01-02",
            lineages[ticker],
            universe_id,
            1,
        )
        for ticker, universe_ids in membership_universes.items()
        for universe_id in universe_ids
    )
    price_rows: list[tuple[str, str, float, float, float, float, float]] = []
    all_tickers = (*PIT_NON_TRADABLE_REFERENCE_SYMBOLS, *tickers)
    for ticker in all_tickers:
        price_rows.append(("2020-01-02", ticker, 9.95, 10.1, 9.9, 10.0, 1000.0))
        spec = fixture["price_series"][ticker]
        for index, timestamp in enumerate(pd.bdate_range(end=session, periods=spec["bars"])):
            close = spec["start_close"] + index * spec["daily_increment"]
            price_rows.append(
                (
                    timestamp.date().isoformat(),
                    ticker,
                    close * 0.995,
                    close * 1.01,
                    close * 0.99,
                    close,
                    float(spec["volume"]),
                )
            )

    fundamentals: list[tuple[Any, ...]] = []
    availability: list[Any] = []
    for row in fixture["fundamentals"]:
        source_public_date = date.fromisoformat(row["source_public_date"])
        sessions = tuple(
            timestamp.date()
            for timestamp in pd.bdate_range("2023-01-01", "2026-04-10")
        )
        available = derive_available_from_source_date(
            period_end=date.fromisoformat(row["period_end"]),
            source_public_date=source_public_date,
            source_public_at=datetime.fromisoformat(row["source_public_at"]),
            exchange_sessions=sessions,
        )
        availability.append(available)
        fundamentals.append(
            (
                row["ticker"],
                row["statement_type"],
                row["period_end"],
                available.available_from_session.isoformat(),
                None,
                row.get("diluted_eps"),
                row.get("total_revenue"),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
    industry_members = pit_canonical_json(list(tickers))
    industry_evidence = pit_canonical_json(["issue98-v3-fixture"])
    industry = [
        (ticker, session.isoformat(), "issue98_fixture_group", 1, industry_members, industry_evidence)
        for ticker in tickers
    ]
    bundle_path = tmp_path / "issue98-v3.sqlite"
    pit_bundle_builder._create_bundle_v3(
        bundle_path,
        metadata=metadata,
        membership=membership,
        prices=price_rows,
        fundamentals=fundamentals,
        industry=industry,
    )
    return bundle_path, sha256_file(bundle_path), provenance_path, tuple(availability)


def _market_inputs(
    bundle: PITDataBundle,
    fixture: dict[str, Any],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, tuple[str, ...], dict[str, float]]:
    session = date.fromisoformat(fixture["session"])
    candidates = tuple(fixture["candidate_symbols"])
    reference_and_members = (
        *PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
        *sorted(bundle.members_at(session)),
    )
    histories = bundle.fetch_price_data(
        candidates,
        pd.Timestamp("2025-01-01"),
        pd.Timestamp(session),
    )
    closes = bundle.fetch_closes(
        reference_and_members,
        pd.Timestamp("2020-01-02"),
        pd.Timestamp(session),
    )
    return histories, closes, tuple(sorted(bundle.members_at(session))), fixture["rs_snapshot"]


def _decision_clock(fixture: dict[str, Any]) -> CurrentDecisionClockV1:
    evidence = fixture["exchange_session_completion"]
    exchange_timezone = evidence["exchange_timezone"]
    exchange_zone = ZoneInfo(exchange_timezone)
    completion_evidence = RecordedExchangeSessionCompletionV1(
        session_date=date.fromisoformat(evidence["session_date"]),
        exchange_timezone=exchange_timezone,
        session_close_at=datetime.fromisoformat(evidence["session_close_at"]).astimezone(
            exchange_zone
        ),
        source_identity=evidence["source_identity"],
        evidence_sha256=evidence["evidence_sha256"],
    )
    as_of_cutoff = datetime.fromisoformat(fixture["as_of_cutoff"])
    if not fixture.get("retain_cutoff_timezone", False):
        as_of_cutoff = as_of_cutoff.astimezone(exchange_zone)
    return CurrentDecisionClockV1(
        completed_session=date.fromisoformat(fixture["session"]),
        as_of_cutoff=as_of_cutoff,
        next_eligible_session=date.fromisoformat(fixture["next_eligible_session"]),
        valuation_time=datetime.fromisoformat(fixture["valuation_time"]),
        completion_evidence=completion_evidence,
    )


def _build_current(
    bundle: PITDataBundle,
    fixture: dict[str, Any],
    fundamental_availability: tuple[Any, ...],
    *,
    price_history_by_symbol: dict[str, pd.DataFrame] | None = None,
    rs_snapshot_override: dict[str, float] | None = None,
    market_closes: pd.DataFrame | None = None,
    oneil_regime: str | None = None,
    distribution_days: int | None = None,
    follow_through: bool | None = None,
):
    histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
    if price_history_by_symbol is not None:
        histories = price_history_by_symbol
    if rs_snapshot_override is not None:
        rs_snapshot = rs_snapshot_override
    if market_closes is not None:
        closes = market_closes
    return build_current_feature_context_snapshot(
        bundle=bundle,
        decision_clock=_decision_clock(fixture),
        candidate_symbols=tuple(fixture["candidate_symbols"]),
        price_history_by_symbol=histories,
        rs_snapshot=rs_snapshot,
        market_closes=closes,
        oneil_regime=(oneil_regime or fixture["market"]["oneil_regime"]),
        distribution_days=(
            distribution_days
            if distribution_days is not None
            else fixture["market"]["distribution_days"]
        ),
        follow_through=(
            follow_through
            if follow_through is not None
            else fixture["market"]["follow_through"]
        ),
        missingness=fixture["missingness"],
        fundamental_availability={"AAA": fundamental_availability},
        unavailable_members=_unavailable_active_members(active, fixture),
        source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        allow_schema_v2_development=True,
    )


def test_current_adapter_matches_historical_builders_and_keeps_legacy_excluded_candidates(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    path, bundle_sha256, fundamental_availability = _write_bundle(tmp_path)

    with PITDataBundle(path, expected_sha256=bundle_sha256) as bundle:
        histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
        current = build_current_feature_context_snapshot(
            bundle=bundle,
            decision_clock=_decision_clock(fixture),
            candidate_symbols=tuple(fixture["candidate_symbols"]),
            price_history_by_symbol=histories,
            rs_snapshot=rs_snapshot,
            market_closes=closes,
            oneil_regime=fixture["market"]["oneil_regime"],
            distribution_days=fixture["market"]["distribution_days"],
            follow_through=fixture["market"]["follow_through"],
            missingness=fixture["missingness"],
            fundamental_availability={"AAA": fundamental_availability},
            unavailable_members=_unavailable_active_members(active, fixture),
            source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
            allow_schema_v2_development=True,
        )
        historical_features = {
            symbol: build_entry_features_v3(
                bundle=bundle,
                symbol=symbol,
                session=date.fromisoformat(fixture["session"]),
                price_history=histories[symbol],
                rs_snapshot=rs_snapshot,
                allow_schema_v2_development=True,
            )
            for symbol in fixture["candidate_symbols"]
        }
        historical_context = build_market_context(
            session=pd.Timestamp(fixture["session"]),
            oneil_regime=fixture["market"]["oneil_regime"],
            distribution_days=fixture["market"]["distribution_days"],
            follow_through=fixture["market"]["follow_through"],
            closes=closes,
            active_constituents=active,
            rs_scores=rs_snapshot,
        )

    assert rs_snapshot["AAA"] < LEGACY_RS_FLOOR
    assert current.candidate_symbols == tuple(fixture["candidate_symbols"])
    assert current.entry_features["AAA"] == historical_features["AAA"]
    assert current.entry_features == historical_features
    assert current.market_context.to_canonical_json() == historical_context.to_canonical_json()
    assert current.data_bundle_sha256 == bundle_sha256
    assert current.source_revision == "ab385d792e19ff6db39d87f1123f47f660fc1e1d"
    assert current.decision_clock.valuation_time == datetime.fromisoformat(
        "2026-04-01T09:35:00-04:00"
    )
    assert current.market_context.active_constituent_count == 495
    assert current.market_context.breadth_50_coverage_fraction == pytest.approx(3 / 495)
    assert current.market_context.breadth_200_coverage_fraction == pytest.approx(2 / 495)
    assert current.market_context.rs_coverage_fraction == pytest.approx(3 / 495)
    assert len(current.unavailable_members) == 492
    assert set(current.unavailable_members) == set(active).difference(
        fixture["candidate_symbols"]
    )
    assert all(
        member.state == "absent"
        and member.policy_boundary_state == "unavailable"
        and member.source_identity == "issue98-recorded-current-snapshot-v1"
        for member in current.unavailable_members.values()
    )
    assert current.entry_features["AAA"].sector_rs is None
    sector_missingness = current.missingness["AAA"]["sector_rs"]
    assert sector_missingness.state == "unsupported_scope"
    assert sector_missingness.policy_boundary_state == "unavailable"
    assert sector_missingness.reason == fixture["missingness"]["AAA"]["sector_rs"]["reason"]
    future_fact = next(
        fact for fact in current.fundamental_availability["AAA"]
        if fact.period_end == date(2025, 12, 31)
    )
    assert future_fact.source_public_date == date(2026, 4, 1)
    assert future_fact.available_from_session == date(2026, 4, 2)
    assert current.entry_features["AAA"].fundamental_age_days == (
        date.fromisoformat(fixture["session"]) - date(2025, 11, 3)
    ).days


def test_schema_v3_three_universe_fixture_matches_historical_builders(tmp_path: Path) -> None:
    fixture = _fixture()
    path, bundle_sha256, provenance_path, fundamental_availability = (
        _write_schema_v3_bundle(tmp_path)
    )

    with PITDataBundle(
        path,
        expected_sha256=bundle_sha256,
        prices_provenance=provenance_path,
    ) as bundle:
        histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
        v3_missingness = {
            symbol: {
                name: (
                    {
                        "state": record["state"],
                        "reason": "controlled schema-V3 fixture has no dated sector taxonomy",
                    }
                    if name == "sector_rs"
                    else record
                )
                for name, record in records.items()
                if name != "industry_group_rs"
            }
            for symbol, records in fixture["missingness"].items()
        }
        current = build_current_feature_context_snapshot(
            bundle=bundle,
            decision_clock=_decision_clock(fixture),
            candidate_symbols=tuple(fixture["candidate_symbols"]),
            price_history_by_symbol=histories,
            rs_snapshot=rs_snapshot,
            market_closes=closes,
            oneil_regime=fixture["market"]["oneil_regime"],
            distribution_days=fixture["market"]["distribution_days"],
            follow_through=fixture["market"]["follow_through"],
            missingness=v3_missingness,
            fundamental_availability={"AAA": fundamental_availability},
            unavailable_members=_unavailable_active_members(active, fixture),
            source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
        )
        historical_features = {
            symbol: build_entry_features_v3(
                bundle=bundle,
                symbol=symbol,
                session=date.fromisoformat(fixture["session"]),
                price_history=histories[symbol],
                rs_snapshot=rs_snapshot,
            )
            for symbol in fixture["candidate_symbols"]
        }
        historical_context = build_market_context(
            session=pd.Timestamp(fixture["session"]),
            oneil_regime=fixture["market"]["oneil_regime"],
            distribution_days=fixture["market"]["distribution_days"],
            follow_through=fixture["market"]["follow_through"],
            closes=closes,
            active_constituents=active,
            rs_scores=rs_snapshot,
        )

    assert current.entry_features == historical_features
    assert current.market_context.to_canonical_json() == historical_context.to_canonical_json()
    assert current.universe_ids == ("nasdaq100", "russell2000", "sp500")
    assert current.development_only is False
    assert current.market_context.active_constituent_count == 3
    assert current.entry_features["AAA"].industry_group_rs is not None


def test_current_decision_clock_rejects_cutoff_before_recorded_session_close() -> None:
    fixture = _fixture()
    fixture["as_of_cutoff"] = "2026-03-31T09:00:00-04:00"
    with pytest.raises(ValueError, match="cutoff precedes recorded exchange close"):
        _decision_clock(fixture)


def test_current_decision_clock_rejects_timezone_and_exchange_date_boundaries() -> None:
    fixture = _fixture()
    fixture["session"] = "2026-04-01"
    fixture["next_eligible_session"] = "2026-04-02"
    fixture["as_of_cutoff"] = "2026-04-01T00:30:00+00:00"
    fixture["retain_cutoff_timezone"] = True
    evidence = fixture["exchange_session_completion"]
    evidence["session_date"] = "2026-04-01"
    evidence["session_close_at"] = "2026-04-01T16:00:00-04:00"
    payload = {
        "session_date": evidence["session_date"],
        "exchange_timezone": evidence["exchange_timezone"],
        "session_close_at": evidence["session_close_at"],
        "source_identity": evidence["source_identity"],
    }
    evidence["evidence_sha256"] = pit_canonical_json_sha256(payload)
    with pytest.raises(ValueError, match="exchange-local timezone"):
        _decision_clock(fixture)

    previous_local_date = _fixture()
    previous_local_date["session"] = "2026-04-01"
    previous_local_date["next_eligible_session"] = "2026-04-02"
    previous_local_date["as_of_cutoff"] = "2026-03-31T20:30:00-04:00"
    previous_evidence = previous_local_date["exchange_session_completion"]
    previous_evidence["session_date"] = "2026-04-01"
    previous_evidence["session_close_at"] = "2026-04-01T16:00:00-04:00"
    previous_payload = {
        "session_date": previous_evidence["session_date"],
        "exchange_timezone": previous_evidence["exchange_timezone"],
        "session_close_at": previous_evidence["session_close_at"],
        "source_identity": previous_evidence["source_identity"],
    }
    previous_evidence["evidence_sha256"] = pit_canonical_json_sha256(previous_payload)
    with pytest.raises(ValueError, match="exchange-local completed feature date"):
        _decision_clock(previous_local_date)


def test_current_decision_clock_accepts_recorded_shortened_session_close() -> None:
    session = date(2026, 11, 27)
    exchange_timezone = "America/New_York"
    close_at = datetime(2026, 11, 27, 13, 0, tzinfo=ZoneInfo(exchange_timezone))
    source_identity = "issue98-recorded-shortened-session-fixture"
    payload = {
        "session_date": session.isoformat(),
        "exchange_timezone": exchange_timezone,
        "session_close_at": close_at.isoformat(),
        "source_identity": source_identity,
    }
    evidence = RecordedExchangeSessionCompletionV1(
        session_date=session,
        exchange_timezone=exchange_timezone,
        session_close_at=close_at,
        source_identity=source_identity,
        evidence_sha256=pit_canonical_json_sha256(payload),
    )

    clock = CurrentDecisionClockV1(
        completed_session=session,
        as_of_cutoff=datetime(2026, 11, 27, 13, 1, tzinfo=ZoneInfo(exchange_timezone)),
        next_eligible_session=date(2026, 11, 30),
        valuation_time=datetime(2026, 11, 30, 9, 35, tzinfo=ZoneInfo(exchange_timezone)),
        completion_evidence=evidence,
    )

    assert clock.as_of_cutoff > evidence.session_close_at
    assert clock.valuation_time > clock.as_of_cutoff


def test_current_decision_clock_rejects_tampered_exchange_completion_evidence() -> None:
    session = date(2026, 3, 31)
    timezone = "America/New_York"
    close_at = datetime(2026, 3, 31, 16, 0, tzinfo=ZoneInfo(timezone))
    with pytest.raises(ValueError, match="evidence digest does not match"):
        RecordedExchangeSessionCompletionV1(
            session_date=session,
            exchange_timezone=timezone,
            session_close_at=close_at,
            source_identity="issue98-recorded-exchange-calendar-v1",
            evidence_sha256="0" * 64,
        )


def test_recorded_input_identity_changes_with_same_session_market_data_and_cutoff(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    path, bundle_sha256, fundamental_availability = _write_bundle(tmp_path)

    with PITDataBundle(path, expected_sha256=bundle_sha256) as bundle:
        histories, closes, _active, rs_snapshot = _market_inputs(bundle, fixture)
        baseline = _build_current(bundle, fixture, fundamental_availability)
        same_inputs = _build_current(bundle, fixture, fundamental_availability)

        altered_histories = dict(histories)
        altered_aaa = histories["AAA"].copy(deep=True)
        for column in ("Open", "High", "Low", "Close"):
            altered_aaa[column] = altered_aaa[column] * 1.01
        altered_histories["AAA"] = altered_aaa
        changed_data = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            price_history_by_symbol=altered_histories,
        )

        changed_cutoff_fixture = dict(fixture)
        changed_cutoff_fixture["as_of_cutoff"] = "2026-03-31T16:01:00-04:00"
        changed_cutoff = _build_current(
            bundle,
            changed_cutoff_fixture,
            fundamental_availability,
        )

        altered_closes = closes.copy(deep=True)
        altered_closes.loc[altered_closes.index[-1], "SPY"] *= 1.01
        changed_benchmark = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            market_closes=altered_closes,
        )
        altered_rs_snapshot = dict(rs_snapshot)
        altered_rs_snapshot["AAA"] += 0.25
        changed_rs = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            rs_snapshot_override=altered_rs_snapshot,
        )
        changed_regime = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            oneil_regime="correction",
        )
        changed_distribution_days = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            distribution_days=fixture["market"]["distribution_days"] + 1,
        )
        changed_follow_through = _build_current(
            bundle,
            fixture,
            fundamental_availability,
            follow_through=False,
        )

    assert baseline.data_bundle_sha256 == changed_data.data_bundle_sha256
    assert baseline.data_bundle_sha256 == changed_cutoff.data_bundle_sha256
    assert baseline.recorded_input_manifest_sha256 == same_inputs.recorded_input_manifest_sha256
    assert len(
        {
            baseline.recorded_input_manifest_sha256,
            changed_data.recorded_input_manifest_sha256,
            changed_cutoff.recorded_input_manifest_sha256,
            changed_benchmark.recorded_input_manifest_sha256,
            changed_rs.recorded_input_manifest_sha256,
            changed_regime.recorded_input_manifest_sha256,
            changed_distribution_days.recorded_input_manifest_sha256,
            changed_follow_through.recorded_input_manifest_sha256,
        }
    ) == 8


def test_current_adapter_rejects_candidates_outside_the_asof_universe(tmp_path: Path) -> None:
    fixture = _fixture()
    path, digest, fundamental_availability = _write_bundle(tmp_path)
    with PITDataBundle(path, expected_sha256=digest) as bundle:
        histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
        with pytest.raises(ValueError, match="candidate is not active"):
            build_current_feature_context_snapshot(
                bundle=bundle,
                decision_clock=_decision_clock(fixture),
                candidate_symbols=("NOTMEMBER",),
                price_history_by_symbol=histories,
                rs_snapshot=rs_snapshot,
                market_closes=closes,
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                missingness=fixture["missingness"],
                fundamental_availability={"AAA": fundamental_availability},
                unavailable_members={},
                source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
                allow_schema_v2_development=True,
            )


def test_current_adapter_requires_a_reason_for_every_unavailable_feature(tmp_path: Path) -> None:
    fixture = _fixture()
    path, digest, fundamental_availability = _write_bundle(tmp_path)
    with PITDataBundle(path, expected_sha256=digest) as bundle:
        histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
        with pytest.raises(ValueError, match="missingness reason is required"):
            build_current_feature_context_snapshot(
                bundle=bundle,
                decision_clock=_decision_clock(fixture),
                candidate_symbols=tuple(fixture["candidate_symbols"]),
                price_history_by_symbol=histories,
                rs_snapshot=rs_snapshot,
                market_closes=closes,
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                missingness={},
                fundamental_availability={"AAA": fundamental_availability},
                unavailable_members=_unavailable_active_members(active, fixture),
                source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
                allow_schema_v2_development=True,
            )


def test_current_adapter_rejects_unexplained_omissions_from_active_universe(tmp_path: Path) -> None:
    fixture = _fixture()
    path, digest, fundamental_availability = _write_bundle(tmp_path)
    with PITDataBundle(path, expected_sha256=digest) as bundle:
        histories, closes, _active, rs_snapshot = _market_inputs(bundle, fixture)
        with pytest.raises(ValueError, match="every omitted active member"):
            build_current_feature_context_snapshot(
                bundle=bundle,
                decision_clock=_decision_clock(fixture),
                candidate_symbols=("AAA",),
                price_history_by_symbol=histories,
                rs_snapshot=rs_snapshot,
                market_closes=closes,
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                missingness=fixture["missingness"],
                fundamental_availability={"AAA": fundamental_availability},
                unavailable_members={},
                source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
                allow_schema_v2_development=True,
            )


def test_current_adapter_rejects_omitting_a_legacy_filtered_candidate_with_inputs(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    path, digest, fundamental_availability = _write_bundle(tmp_path)
    with PITDataBundle(path, expected_sha256=digest) as bundle:
        histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
        candidates = ("BBB", "CCC")
        unavailable = {
            symbol: fixture["unavailable_member_disposition"]
            for symbol in active
            if symbol not in candidates
        }
        with pytest.raises(ValueError, match="has current price and RS inputs"):
            build_current_feature_context_snapshot(
                bundle=bundle,
                decision_clock=_decision_clock(fixture),
                candidate_symbols=candidates,
                price_history_by_symbol=histories,
                rs_snapshot=rs_snapshot,
                market_closes=closes,
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                missingness=fixture["missingness"],
                fundamental_availability={"AAA": fundamental_availability},
                unavailable_members=unavailable,
                source_revision="ab385d792e19ff6db39d87f1123f47f660fc1e1d",
                allow_schema_v2_development=True,
            )


def test_source_public_dates_shift_strictly_after_exchange_session_and_normalized_dates_do_not_shift_again() -> None:
    sessions = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    holiday_release = derive_available_from_source_date(
        period_end=date(2025, 9, 30),
        source_public_date=date(2026, 1, 1),
        source_public_at=datetime.fromisoformat("2026-01-01T11:00:00-05:00"),
        exchange_sessions=sessions,
    )
    intraday_release = derive_available_from_source_date(
        period_end=date(2025, 9, 30),
        source_public_date=date(2026, 1, 2),
        source_public_at=datetime.fromisoformat("2026-01-02T10:00:00-05:00"),
        exchange_sessions=sessions,
    )
    already_normalized = validate_normalized_availability(
        period_end=date(2025, 9, 30),
        source_public_date=date(2026, 1, 2),
        source_public_at=datetime.fromisoformat("2026-01-02T10:00:00-05:00"),
        available_from_session=date(2026, 1, 5),
        exchange_sessions=sessions,
    )

    assert holiday_release.source_public_date == date(2026, 1, 1)
    assert holiday_release.available_from_session == date(2026, 1, 2)
    assert intraday_release.source_public_at.date() == date(2026, 1, 2)
    assert intraday_release.available_from_session == date(2026, 1, 5)
    assert already_normalized.source_public_date == date(2026, 1, 2)
    assert already_normalized.available_from_session == date(2026, 1, 5)
    assert already_normalized.date_basis == "already_normalized"


def test_entry_features_compute_sector_rs_from_dated_visible_assignments(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    bundle_path, _old_digest, provenance_path, _availability = (
        _write_schema_v3_bundle(tmp_path)
    )
    session = date.fromisoformat(fixture["session"])
    connection = sqlite3.connect(bundle_path)
    connection.executemany(
        "UPDATE industry_group_snapshots SET sector_id=? WHERE symbol=?",
        [
            ("gics-sector:information_technology", "AAA"),
            ("gics-sector:financials", "BBB"),
            ("gics-sector:information_technology", "CCC"),
        ],
    )
    future_date = max(session.replace(day=1), date(2026, 4, 2)).isoformat()
    connection.execute(
        "INSERT INTO industry_group_snapshots "
        "(symbol,as_of_date,group_id,group_rank,group_members,evidence_ids,sector_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "AAA", future_date, "future_group", 1, '["AAA"]',
            '["revision:future"]', "gics-sector:communication_services",
        ),
    )
    connection.commit()
    connection.close()

    digest = sha256_file(bundle_path)
    with PITDataBundle(
        bundle_path, expected_sha256=digest, prices_provenance=provenance_path
    ) as bundle:
        history = bundle.fetch_price_data(
            ("AAA",), pd.Timestamp("2025-01-01"), pd.Timestamp(session)
        )["AAA"]
        features = build_entry_features_v3(
            bundle=bundle,
            symbol="AAA",
            session=session,
            price_history=history,
            rs_snapshot=fixture["rs_snapshot"],
        )

    expected = (
        fixture["rs_snapshot"]["AAA"] + fixture["rs_snapshot"]["CCC"]
    ) / 2
    assert features.sector_rs == pytest.approx(expected)


def test_industry_preparation_stage_bundle_is_rejected_by_decision_features(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    bundle_path, _old_digest, provenance_path, _availability = (
        _write_schema_v3_bundle(tmp_path)
    )
    connection = sqlite3.connect(bundle_path)
    connection.execute(
        "INSERT INTO dataset_metadata(key,value) VALUES (?,?)",
        ("bundle_stage", "industry_preparation"),
    )
    connection.execute("DELETE FROM industry_group_snapshots")
    connection.commit()
    connection.close()

    with PITDataBundle(
        bundle_path,
        expected_sha256=sha256_file(bundle_path),
        prices_provenance=provenance_path,
    ) as bundle:
        session = date.fromisoformat(fixture["session"])
        history = bundle.fetch_price_data(
            ("AAA",), pd.Timestamp("2025-01-01"), pd.Timestamp(session)
        )["AAA"]
        with pytest.raises(ValueError, match="industry-preparation"):
            build_entry_features_v3(
                bundle=bundle,
                symbol="AAA",
                session=session,
                price_history=history,
                rs_snapshot=fixture["rs_snapshot"],
            )


def _write_d1_causal_bundle(tmp_path: Path):
    """Extend the accepted V3 fixture with dated ownership/classification changes."""
    bundle_path, _old_digest, provenance_path, base_availability = (
        _write_schema_v3_bundle(tmp_path)
    )
    quarterly_availability = {
        "AAA": list(base_availability),
        "BBB": [],
        "CCC": [],
    }
    exchange_sessions = tuple(
        timestamp.date()
        for timestamp in pd.bdate_range("2023-01-01", "2026-04-10")
    )
    connection = sqlite3.connect(bundle_path)
    connection.execute(
        "DELETE FROM membership_v3 WHERE effective_date=? "
        "AND security_lineage_id=? AND universe_id=?",
        ("2020-01-02", "issue98_aaa", "nasdaq100"),
    )
    connection.execute(
        "INSERT INTO membership_v3 VALUES (?,?,?,?)",
        ("2026-04-02", "issue98_aaa", "nasdaq100", 1),
    )
    connection.execute(
        "UPDATE industry_group_snapshots SET group_id=?,group_rank=?,"
        "group_members=?,evidence_ids=?,sector_id=? "
        "WHERE symbol=? AND as_of_date=?",
        (
            "industry:legacy",
            1,
            pit_canonical_json(["AAA"]),
            pit_canonical_json(["d1:initial"]),
            "gics-sector:information_technology",
            "AAA",
            "2026-03-31",
        ),
    )
    connection.execute(
        "UPDATE industry_group_snapshots SET group_id=?,group_rank=?,"
        "group_members=?,evidence_ids=?,sector_id=? "
        "WHERE symbol=? AND as_of_date=?",
        (
            "industry:other",
            1,
            pit_canonical_json(["CCC"]),
            pit_canonical_json(["d1:initial"]),
            "gics-sector:consumer_discretionary",
            "CCC",
            "2026-03-31",
        ),
    )
    connection.execute(
        "DELETE FROM industry_group_snapshots WHERE symbol=?", ("BBB",)
    )

    def add_record(
        ticker: str,
        statement_type: str,
        period_end: str,
        source_public_date: str,
        source_public_at: str,
        *,
        diluted_eps: float | None = None,
        total_revenue: float | None = None,
        shares_outstanding: float | None = None,
        held_percent: float | None = None,
        institution_count: int | None = None,
        previous_institution_count: int | None = None,
    ) -> None:
        available = derive_available_from_source_date(
            period_end=date.fromisoformat(period_end),
            source_public_date=date.fromisoformat(source_public_date),
            source_public_at=datetime.fromisoformat(source_public_at),
            exchange_sessions=exchange_sessions,
        )
        connection.execute(
            "INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticker,
                statement_type,
                period_end,
                available.available_from_session.isoformat(),
                None,
                diluted_eps,
                total_revenue,
                None,
                None,
                None,
                shares_outstanding,
                held_percent,
                institution_count,
                previous_institution_count,
            ),
        )
        if statement_type == "quarterly":
            quarterly_availability[ticker].append(available)

    # Complete the Q4 comparison around the already-dated future Q4 2025 filing.
    add_record(
        "AAA", "quarterly", "2023-12-31", "2024-02-15",
        "2024-02-15T10:00:00-05:00", diluted_eps=0.9, total_revenue=90.0
    )
    add_record(
        "AAA", "quarterly", "2024-12-31", "2025-02-18",
        "2025-02-18T10:00:00-05:00", diluted_eps=1.2, total_revenue=120.0
    )
    add_record(
        "AAA", "annual", "2023-12-31", "2024-02-15",
        "2024-02-15T10:00:00-05:00", diluted_eps=0.9
    )
    add_record(
        "AAA", "annual", "2024-12-31", "2025-02-18",
        "2025-02-18T10:00:00-05:00", diluted_eps=1.35
    )
    add_record(
        "AAA", "institutional", "2025-09-30", "2025-11-14",
        "2025-11-14T10:00:00-05:00", shares_outstanding=10_000_000,
        held_percent=0.40, institution_count=100, previous_institution_count=90
    )
    add_record(
        "AAA", "institutional", "2025-12-31", "2026-04-01",
        "2026-04-01T10:00:00-04:00", shares_outstanding=10_200_000,
        held_percent=0.42, institution_count=110, previous_institution_count=100
    )
    # BBB has a current fiscal period but no comparable prior-year period.
    add_record(
        "BBB", "quarterly", "2025-06-30", "2025-08-15",
        "2025-08-15T10:00:00-04:00", diluted_eps=5.0, total_revenue=100.0
    )
    # CCC has a matched period whose zero EPS denominator must remain unknown.
    add_record(
        "CCC", "quarterly", "2024-03-31", "2024-05-15",
        "2024-05-15T10:00:00-04:00", diluted_eps=0.0, total_revenue=100.0
    )
    add_record(
        "CCC", "quarterly", "2025-03-31", "2025-05-15",
        "2025-05-15T10:00:00-04:00", diluted_eps=2.0, total_revenue=200.0
    )
    add_record(
        "CCC", "annual", "2023-12-31", "2024-02-15",
        "2024-02-15T10:00:00-05:00", diluted_eps=0.0
    )
    add_record(
        "CCC", "annual", "2024-12-31", "2025-02-18",
        "2025-02-18T10:00:00-05:00", diluted_eps=2.0
    )

    for ticker in ("SPY", "QQQ", "IWM", "AAA", "BBB", "CCC"):
        last = connection.execute(
            "SELECT close,volume FROM price WHERE ticker=? "
            "ORDER BY trade_date DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        previous_close = float(last[0])
        previous_volume = float(last[1])
        for trade_date in ("2026-04-01", "2026-04-02"):
            close = previous_close * 1.005
            connection.execute(
                "INSERT INTO price VALUES (?,?,?,?,?,?,?)",
                (
                    trade_date,
                    ticker,
                    previous_close,
                    close * 1.01,
                    previous_close * 0.99,
                    close,
                    previous_volume * (2.0 if ticker == "AAA" else 1.0),
                ),
            )
            previous_close = close
    connection.execute(
        "UPDATE price SET volume=volume*2 WHERE ticker='AAA' AND trade_date='2026-03-31'"
    )
    for ticker, sector in (
        ("AAA", "gics-sector:information_technology"),
        ("CCC", "gics-sector:information_technology"),
    ):
        connection.execute(
            "INSERT INTO industry_group_snapshots VALUES (?,?,?,?,?,?,?)",
            (
                ticker,
                "2026-04-02",
                "industry:reclassified",
                1,
                pit_canonical_json(["AAA", "CCC"]),
                pit_canonical_json(["d1:reclassification"]),
                sector,
            ),
        )
    connection.commit()
    connection.close()
    return (
        bundle_path,
        sha256_file(bundle_path),
        provenance_path,
        {symbol: tuple(records) for symbol, records in quarterly_availability.items()},
    )


def _d1_fixture_for_session(fixture: dict[str, Any], session: date) -> dict[str, Any]:
    next_session = {
        date(2026, 3, 31): date(2026, 4, 1),
        date(2026, 4, 2): date(2026, 4, 3),
    }[session]
    result = dict(fixture)
    result["session"] = session.isoformat()
    result["next_eligible_session"] = next_session.isoformat()
    result["as_of_cutoff"] = f"{session.isoformat()}T16:00:00-04:00"
    result["valuation_time"] = f"{next_session.isoformat()}T09:35:00-04:00"
    evidence = dict(fixture["exchange_session_completion"])
    evidence["session_date"] = session.isoformat()
    evidence["session_close_at"] = f"{session.isoformat()}T16:00:00-04:00"
    payload = {
        "session_date": evidence["session_date"],
        "exchange_timezone": evidence["exchange_timezone"],
        "session_close_at": evidence["session_close_at"],
        "source_identity": evidence["source_identity"],
    }
    evidence["evidence_sha256"] = pit_canonical_json_sha256(payload)
    result["exchange_session_completion"] = evidence
    return result


def _d1_missingness(features: dict[str, Any]) -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {}
    for symbol, snapshot in features.items():
        records: dict[str, dict[str, str]] = {}
        for name in snapshot.__dataclass_fields__:
            if getattr(snapshot, name) is not None:
                continue
            classification = name in {"industry_group_rs", "sector_rs"}
            records[name] = {
                "state": "absent" if classification else "insufficient_history",
                "reason": (
                    "the dated synthetic fixture has no visible classification"
                    if classification
                    else "the dated synthetic fixture lacks a usable comparison"
                ),
            }
        result[symbol] = records
    return result


def test_d1_causal_canslim_inputs_are_shared_and_fail_closed(tmp_path: Path) -> None:
    """One fixture proves dated financial, ownership, classification and price inputs."""
    from core.backtest_engine import CanslimStrategy
    from core.canslim.i_institutional import evaluate_i

    fixture = _fixture()
    bundle_path, bundle_sha256, provenance_path, availability = (
        _write_d1_causal_bundle(tmp_path)
    )
    before = date(2026, 3, 31)
    after = date(2026, 4, 2)
    evidence: dict[date, dict[str, Any]] = {}

    with PITDataBundle(
        bundle_path,
        expected_sha256=bundle_sha256,
        prices_provenance=provenance_path,
    ) as bundle:
        for session in (before, after):
            session_fixture = _d1_fixture_for_session(fixture, session)
            histories, closes, active, rs_snapshot = _market_inputs(
                bundle, session_fixture
            )
            historical = {
                symbol: build_entry_features_v3(
                    bundle=bundle,
                    symbol=symbol,
                    session=session,
                    price_history=histories[symbol],
                    rs_snapshot=rs_snapshot,
                )
                for symbol in fixture["candidate_symbols"]
            }
            current = build_current_feature_context_snapshot(
                bundle=bundle,
                decision_clock=_decision_clock(session_fixture),
                candidate_symbols=tuple(fixture["candidate_symbols"]),
                price_history_by_symbol=histories,
                rs_snapshot=rs_snapshot,
                market_closes=closes,
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                missingness=_d1_missingness(historical),
                fundamental_availability=availability,
                unavailable_members=_unavailable_active_members(active, session_fixture),
                source_revision="bc1f33829799f2fd2dfd33f4ab7bbec1645d146e",
            )
            market = build_market_context(
                session=pd.Timestamp(session),
                oneil_regime=fixture["market"]["oneil_regime"],
                distribution_days=fixture["market"]["distribution_days"],
                follow_through=fixture["market"]["follow_through"],
                closes=closes,
                active_constituents=active,
                rs_scores=rs_snapshot,
            )
            signals = {}
            for symbol in fixture["candidate_symbols"]:
                signal = CanslimStrategy(
                    fundamental_provider=bundle.fundamentals_provider
                ).evaluate_symbol(
                    ticker=symbol,
                    ticker_ohlcv={symbol: histories[symbol]},
                    all_closes=closes,
                    eval_date=pd.Timestamp(session),
                    market_state={
                        "m_score": 1.0,
                        "market_is_bullish": True,
                        "market": market,
                    },
                    rs_score=rs_snapshot[symbol],
                )
                assert signal is not None
                signals[symbol] = signal
            assert current.entry_features == historical
            assert current.data_bundle_sha256 == bundle_sha256
            evidence[session] = {
                "current": current,
                "historical": historical,
                "signals": signals,
                "histories": histories,
                "closes": closes,
                "active": active,
                "rs_snapshot": rs_snapshot,
                "session_fixture": session_fixture,
            }

        early = evidence[before]
        late = evidence[after]
        early_fundamentals = bundle.fundamentals_provider(
            "AAA", pd.Timestamp(before)
        )
        late_fundamentals = bundle.fundamentals_provider(
            "AAA", pd.Timestamp(after)
        )
        q4_availability = next(
            row for row in availability["AAA"]
            if row.period_end == date(2025, 12, 31)
        )
        assert q4_availability.available_from_session == after
        assert pd.Timestamp("2025-12-31") not in early_fundamentals[
            "quarterly_income"
        ].columns
        assert pd.Timestamp("2025-12-31") in late_fundamentals[
            "quarterly_income"
        ].columns
        assert early["historical"]["AAA"].fundamental_age_days > 0
        assert late["historical"]["AAA"].fundamental_age_days == 0

        assert bundle.affiliations_at(before)["AAA"] == frozenset({"sp500"})
        assert bundle.affiliations_at(after)["AAA"] == frozenset(
            {"nasdaq100", "sp500"}
        )
        assert early["historical"]["AAA"].affiliations == ("sp500",)
        assert late["historical"]["AAA"].affiliations == ("nasdaq100", "sp500")
        assert early["historical"]["AAA"].industry_group_rs == pytest.approx(35.0)
        assert late["historical"]["AAA"].industry_group_rs == pytest.approx(50.0)
        assert early["historical"]["AAA"].sector_rs == pytest.approx(35.0)
        assert late["historical"]["AAA"].sector_rs == pytest.approx(50.0)

        early_owner = early_fundamentals["company_info"]
        late_owner = late_fundamentals["company_info"]
        assert (
            early_owner["held_percent_institutions"],
            early_owner["institution_count"],
            early_owner["prev_institution_count"],
        ) == (0.40, 100, 90)
        assert (
            late_owner["held_percent_institutions"],
            late_owner["institution_count"],
            late_owner["prev_institution_count"],
        ) == (0.42, 110, 100)
        assert early["signals"]["AAA"]["current_growth"] == pytest.approx(0.50)
        assert early["signals"]["AAA"]["annual_growth"] == pytest.approx(0.50)
        assert early["signals"]["AAA"]["i_score"] == pytest.approx(
            evaluate_i(0.40, 100, 90)
        )
        assert "current_growth_unavailable" not in early["signals"]["AAA"][
            "entry_blocking_reasons"
        ]
        assert "annual_growth_unavailable" not in early["signals"]["AAA"][
            "entry_blocking_reasons"
        ]

        unknown_period = early["signals"]["BBB"]
        unknown_denominator = early["signals"]["CCC"]
        assert unknown_period["current_growth"] is None
        assert unknown_period["annual_growth"] is None
        assert "current_growth_unavailable" in unknown_period[
            "entry_blocking_reasons"
        ]
        assert early["historical"]["BBB"].earnings_growth_acceleration is None
        assert early["historical"]["BBB"].sales_growth_acceleration is None
        assert early["historical"]["BBB"].industry_group_rs is None
        assert early["historical"]["BBB"].sector_rs is None
        assert unknown_denominator["current_growth"] is None
        assert unknown_denominator["annual_growth"] is None
        assert "current_growth_unavailable" in unknown_denominator[
            "entry_blocking_reasons"
        ]
        assert "annual_growth_unavailable" in unknown_denominator[
            "entry_blocking_reasons"
        ]

        replayed = {
            symbol: build_entry_features_v3(
                bundle=bundle,
                symbol=symbol,
                session=before,
                price_history=early["histories"][symbol],
                rs_snapshot=early["rs_snapshot"],
            )
            for symbol in fixture["candidate_symbols"]
        }
        assert replayed == early["historical"]

        record_counts = {
            str(row[0]): int(row[1])
            for row in bundle._connection.execute(
                "SELECT statement_type,COUNT(*) FROM fundamentals "
                "GROUP BY statement_type"
            ).fetchall()
        }
        coverage = {
            "scope": "synthetic_fixture_only",
            "production_source_coverage": False,
            "tradable_symbols": len(bundle.tradable_symbols()),
            "reference_symbols": len(bundle.reference_symbols()),
            "decision_sessions": (before.isoformat(), after.isoformat()),
            "price_history_bars": {
                symbol: len(early["histories"][symbol])
                for symbol in fixture["candidate_symbols"]
            },
            "fundamental_rows": record_counts,
            "lookback_sessions": {
                "atr": 20,
                "average_dollar_volume": 50,
                "52_week_high": 252,
            },
        }
        assert coverage == {
            "scope": "synthetic_fixture_only",
            "production_source_coverage": False,
            "tradable_symbols": 3,
            "reference_symbols": 3,
            "decision_sessions": ("2026-03-31", "2026-04-02"),
            "price_history_bars": {"AAA": 252, "BBB": 252, "CCC": 100},
            "fundamental_rows": {
                "annual": 4,
                "institutional": 2,
                "quarterly": 12,
            },
            "lookback_sessions": {
                "atr": 20,
                "average_dollar_volume": 50,
                "52_week_high": 252,
            },
        }
