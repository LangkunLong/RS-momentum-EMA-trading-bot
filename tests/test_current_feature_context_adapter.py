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

import pandas as pd
import pytest

from core.canslim.entry_contract import MIN_RS_SCORE as LEGACY_RS_FLOOR
from core.pit_data import PITDataBundle
from core.pit_feature_snapshot import build_entry_features_v3
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_sha256,
)
from core.strategy_policy.market_context import build_market_context
from core.current_policy_inputs import (
    CurrentDecisionClockV1,
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
    return CurrentDecisionClockV1(
        completed_session=date.fromisoformat(fixture["session"]),
        as_of_cutoff=datetime.fromisoformat(fixture["as_of_cutoff"]),
        next_eligible_session=date.fromisoformat(fixture["next_eligible_session"]),
        valuation_time=datetime.fromisoformat(fixture["valuation_time"]),
    )


def _build_current(
    bundle: PITDataBundle,
    fixture: dict[str, Any],
    fundamental_availability: tuple[Any, ...],
):
    histories, closes, active, rs_snapshot = _market_inputs(bundle, fixture)
    return build_current_feature_context_snapshot(
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
