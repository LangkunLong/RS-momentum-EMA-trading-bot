"""Tests for projecting V3 security-lineage membership into SEC ticker inputs."""

from __future__ import annotations

import csv
import hashlib
import json
import runpy
import sqlite3
from datetime import date
from pathlib import Path
import zipfile

import pytest

import build_pit_bundle as bundle_builder
import fetch_sec_pit_fundamentals as sec_export
from core.sec_pit_fundamentals import (
    FundamentalAuditRow,
    FundamentalExportResult,
    FundamentalRow,
    IdentityExtractionRow,
    SecurityMasterResult,
    SecurityMasterRow,
)
from core.pit_provenance import pit_canonical_json_bytes, pit_canonical_json_sha256
from core.financial_lineage_bridge import (
    IdentityBoundary,
    _clip_projection_to_membership_window,
    _identity_extraction_history_rows,
    build_membership_projection,
    identity_boundaries_from_contract,
    main as bridge_main,
    project_v3_membership_to_ticker,
)


def _resolve(ticker_by_lineage: dict[str, str]):
    return lambda lineage, _when: ticker_by_lineage[lineage]


def test_projection_unions_overlapping_indexes_and_injects_rename_boundary() -> None:
    membership = [
        ("2020-01-02", "lineage_alpha", "sp500", 1),
        ("2020-01-15", "lineage_alpha", "nasdaq100", 1),
        ("2020-02-10", "lineage_alpha", "sp500", 0),
        ("2020-03-01", "lineage_alpha", "nasdaq100", 0),
        ("2020-03-10", "lineage_alpha", "russell2000", 1),
        ("2020-03-20", "lineage_alpha", "russell2000", 0),
    ]
    projection = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=lambda lineage, when: (
            "OLD" if lineage == "lineage_alpha" and when < "2020-02-03" else "NEW"
        ),
        identity_boundaries=(
            IdentityBoundary(
                effective_date=date(2020, 2, 3),
                lineage_id="lineage_alpha",
                predecessor_ticker="OLD",
                successor_ticker="NEW",
                boundary_id="old-to-new",
            ),
        ),
    )

    assert projection.ticker_events == (
        ("2020-01-02", "OLD", 1),
        ("2020-02-03", "NEW", 1),
        ("2020-02-03", "OLD", 0),
        ("2020-03-01", "NEW", 0),
        ("2020-03-10", "NEW", 1),
        ("2020-03-20", "NEW", 0),
    )
    assert all(
        row["effective_date"] != "2020-01-15"
        and row["effective_date"] != "2020-02-10"
        for row in projection.ledger_rows
    )
    assert any(
        row["identity_boundary_id"] == "old-to-new" and row["ticker"] == "NEW"
        for row in projection.ledger_rows
    )


def test_projection_keeps_short_membership_episodes_and_same_ticker_reentry() -> None:
    membership = [
        ("2020-01-02", "lineage_alpha", "sp500", 1),
        ("2020-01-10", "lineage_alpha", "sp500", 0),
        ("2020-01-15", "lineage_alpha", "sp500", 1),
        ("2020-01-20", "lineage_alpha", "sp500", 0),
    ]
    projection = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=_resolve({"lineage_alpha": "REUSE"}),
    )

    assert projection.ticker_events == (
        ("2020-01-02", "REUSE", 1),
        ("2020-01-10", "REUSE", 0),
        ("2020-01-15", "REUSE", 1),
        ("2020-01-20", "REUSE", 0),
    )


def test_membership_window_seed_does_not_relabel_pre_window_history_as_membership() -> None:
    membership = [
        ("2020-01-02", "lineage_alpha", "sp500", 1),
        ("2020-01-03", "lineage_alpha", "nasdaq100", 1),
        ("2020-01-03", "lineage_alpha", "sp500", 0),
        ("2023-01-02", "lineage_alpha", "nasdaq100", 0),
    ]
    full = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=lambda _lineage, when: "OLD" if when < "2020-01-03" else "NEW",
        identity_boundaries=(
            IdentityBoundary(
                date(2020, 1, 3),
                "lineage_alpha",
                "OLD",
                "NEW",
                "old-to-new",
            ),
        ),
    )
    clipped = _clip_projection_to_membership_window(
        full,
        membership,
        start_date="2021-01-01",
        end_date="2023-01-02",
    )
    assert clipped.ticker_events == (
        ("2021-01-01", "NEW", 1),
        ("2023-01-02", "NEW", 0),
    )
    assert clipped.ledger_rows[0]["projection_reason"] == "membership_window_seed"
    assert all(row["ticker"] != "OLD" for row in clipped.ledger_rows)


def test_projection_rejects_overlapping_lineages_mapped_to_the_same_ticker() -> None:
    membership = [
        ("2020-01-02", "lineage_alpha", "sp500", 1),
        ("2020-01-02", "lineage_beta", "nasdaq100", 1),
    ]

    with pytest.raises(ValueError, match="ticker identity is ambiguous"):
        project_v3_membership_to_ticker(
            membership,
            resolve_ticker_for_lineage=lambda _lineage, _when: "SAME",
        )


def test_segment_contract_keeps_ordinary_history_in_a_mixed_universe(
    tmp_path: Path,
) -> None:
    segment_fixtures = runpy.run_path(
        str(Path(__file__).with_name("test_pit_identity_segments.py"))
    )
    _bundle_with_segments = segment_fixtures["_bundle_with_segments"]
    bundle, prices_provenance = _bundle_with_segments(tmp_path)
    contract = bundle.load_price_identity_transition_contract(prices_provenance)
    membership = (
        ("2021-01-04", "fiserv", "sp500", 1),
        ("2021-01-04", "ordinary", "sp500", 1),
        ("2025-12-31", "fiserv", "sp500", 0),
        ("2025-12-31", "ordinary", "sp500", 0),
    )
    identities = dict(contract.identities)
    identities["ORD"] = {
        "chain_id": "ordinary",
        "admitted_start": "2020-01-01",
        "admitted_end": "2025-12-31",
    }
    projection = project_v3_membership_to_ticker(
        membership,
        resolve_ticker_for_lineage=lambda lineage, when: (
            contract.resolve_ticker_for_lineage(lineage, when)
            if contract.has_segmented_chain(lineage)
            else "ORD"
        ),
        identity_boundaries=identity_boundaries_from_contract((), contract),
    )
    assert projection.ticker_events == (
        ("2021-01-04", "FISV", 1),
        ("2021-01-04", "ORD", 1),
        ("2023-06-07", "FI", 1),
        ("2023-06-07", "FISV", 0),
        ("2025-11-11", "FI", 0),
        ("2025-11-11", "FISV", 1),
        ("2025-12-31", "FISV", 0),
        ("2025-12-31", "ORD", 0),
    )
    history = _identity_extraction_history_rows(
        membership,
        identities=identities,
        transitions=(),
        segment_contract=contract,
        start_date="2020-01-01",
        end_date="2025-12-31",
    )
    assert history == (
        ("FI", "2023-06-07", "2025-11-10", "fiserv", "fiserv-fi-2023-2025"),
        ("FISV", "2020-01-01", "2023-06-06", "fiserv", "fiserv-fisv-pre-2023"),
        ("FISV", "2025-11-11", "2025-12-31", "fiserv", "fiserv-fisv-post-2025"),
        ("ORD", "2020-01-01", "2025-12-31", "ordinary", ""),
    )

    membership_csv = tmp_path / "mixed_membership.csv"
    _write_csv(
        membership_csv,
        ("effective_date", "ticker", "member"),
        [tuple(map(str, row)) for row in projection.ticker_events],
    )
    history_csv = tmp_path / "mixed_identity_history.csv"
    _write_csv(
        history_csv,
        (
            "ticker",
            "first_extraction_date",
            "last_extraction_date",
            "security_lineage_id",
            "identity_segment_id",
        ),
        list(history),
    )
    names_csv = tmp_path / "mixed_security_names.csv"
    _write_csv(
        names_csv,
        ("ticker", "company_name"),
        [("FI", "Fiserv Inc"), ("FISV", "Fiserv Inc"), ("ORD", "Ordinary Co")],
    )
    identity_csv = tmp_path / "mixed_identity_manifest.csv"
    _write_csv(
        identity_csv,
        (
            "canonical_ticker", "provider_symbol", "identity_asof", "admitted_start",
            "admitted_end", "chain_id", "continuity_kind", "warmup_predecessor",
            "factor_anchor", "evidence_url",
        ),
        [
            ("FI", "FI", "2025-11-10", "2023-06-07", "2025-11-10", "fiserv", "same_issuer_ticker_reuse", "FISV", "0", "synthetic://fi"),
            ("FISV", "FISV", "2025-12-31", "2020-01-01", "2025-12-31", "fiserv", "same_issuer_ticker_reuse", "", "1", "synthetic://fisv"),
            ("ORD", "ORD", "2025-12-31", "2020-01-01", "2025-12-31", "ordinary", "standalone", "", "1", "synthetic://ord"),
        ],
    )
    submissions_zip = tmp_path / "mixed_submissions.zip"
    empty_recent = {
        "accessionNumber": [], "form": [], "filingDate": [], "acceptanceDateTime": []
    }
    with zipfile.ZipFile(submissions_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cik, name, tickers in (
            (798354, "Fiserv Inc", ["FISV", "FI"]),
            (2, "Ordinary Co", ["ORD"]),
        ):
            archive.writestr(
                f"CIK{cik:010d}.json",
                json.dumps({
                    "cik": cik,
                    "name": name,
                    "tickers": tickers,
                    "formerNames": [],
                    "filings": {"recent": empty_recent, "files": []},
                }),
            )
    security_master = sec_export.build_security_master(
        membership_csv,
        names_csv,
        submissions_zip,
        identity_csv,
        start_date=date(2021, 1, 1),
        end_date=date(2025, 12, 31),
        extraction_start_date=date(2020, 1, 1),
        identity_extraction_history_csv=history_csv,
    )
    assert any(row.ticker == "ORD" for row in security_master.identity_extraction_rows)

    companyfacts_zip = tmp_path / "mixed_companyfacts.zip"
    with zipfile.ZipFile(companyfacts_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cik, name, accession, filed, period_end, quarter_start, value in (
            (798354, "Fiserv Inc", "000000000120230001", "2023-01-01", "2022-12-31", "2022-10-01", 1.0),
            (2, "Ordinary Co", "000000000220200001", "2020-01-02", "2019-12-31", "2019-10-01", 3.0),
        ):
            archive.writestr(
                f"CIK{cik:010d}.json",
                json.dumps({
                    "cik": cik,
                    "entityName": name,
                    "facts": {
                        "us-gaap": {
                            "EarningsPerShareBasic": {
                                "units": {
                                    "USD/shares": [{
                                        "accn": accession,
                                        "form": "10-Q",
                                        "filed": filed,
                                        "end": period_end,
                                        "start": quarter_start,
                                        "fy": period_end[:4],
                                        "fp": "Q4",
                                        "val": value,
                                    }]
                                }
                            }
                        }
                    },
                }),
            )
    spy_csv = tmp_path / "mixed_spy_days.csv"
    _write_csv(
        spy_csv,
        ("trade_date",),
        [(day,) for day in ("2020-01-03", "2023-01-03", "2025-12-31")],
    )
    fundamentals = sec_export.extract_fundamentals(
        companyfacts_zip,
        security_master,
        spy_csv,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
    )
    assert any(row.ticker == "ORD" and row.basic_eps == 3.0 for row in fundamentals.rows)


def test_same_issuer_reentry_keeps_intervening_alias_fundamentals(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "companyfacts.zip"
    facts = {
        "cik": 798354,
        "entityName": "Fiserv Inc",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "units": {
                        "USD/shares": [
                            {
                                "accn": "000000000120230001",
                                "form": "10-Q",
                                "filed": "2023-01-01",
                                "end": "2022-12-31",
                                "start": "2022-10-01",
                                "fy": "2022",
                                "fp": "Q4",
                                "val": 1.0,
                            },
                            {
                                "accn": "000000000120240001",
                                "form": "10-Q",
                                "filed": "2024-01-02",
                                "end": "2023-12-31",
                                "start": "2023-10-01",
                                "fy": "2023",
                                "fp": "Q4",
                                "val": 2.0,
                            },
                        ]
                    }
                }
            }
        },
    }
    _write_json_zip(facts_path, "CIK0000798354.json", facts)
    spy_csv = tmp_path / "spy_days.csv"
    _write_csv(
        spy_csv,
        ("trade_date",),
        [(day,) for day in ("2023-01-03", "2024-01-03", "2025-12-31")],
    )
    security_master = SecurityMasterResult(
        rows=(
            SecurityMasterRow(
                "FISV", "0000798354", "Fiserv Inc", date(2021, 1, 4), date(2023, 6, 6), "identity_manifest"
            ),
            SecurityMasterRow(
                "FI", "0000798354", "Fiserv Inc", date(2023, 6, 7), date(2025, 11, 10), "identity_manifest"
            ),
            SecurityMasterRow(
                "FISV", "0000798354", "Fiserv Inc", date(2025, 11, 11), date(2025, 12, 31), "identity_manifest"
            ),
        ),
        exclusions=(),
        acceptance_by_cik={},
        membership_union=("FI", "FISV"),
        identity_manifest_sha256="a" * 64,
        submissions_archive_sha256="b" * 64,
        missing_submission_fragments=0,
        identity_extraction_rows=(
            IdentityExtractionRow(
                "FISV", "0000798354", "Fiserv Inc", date(2020, 1, 1), date(2023, 6, 6),
                "fiserv", "fiserv-fisv-pre-2023", "identity_manifest"
            ),
            IdentityExtractionRow(
                "FI", "0000798354", "Fiserv Inc", date(2023, 6, 7), date(2025, 11, 10),
                "fiserv", "fiserv-fi-2023-2025", "identity_manifest"
            ),
            IdentityExtractionRow(
                "FISV", "0000798354", "Fiserv Inc", date(2025, 11, 11), date(2025, 12, 31),
                "fiserv", "fiserv-fisv-post-2025", "identity_manifest"
            ),
        ),
    )

    extracted = sec_export.extract_fundamentals(
        facts_path,
        security_master,
        spy_csv,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert [
        (row.ticker, row.public_date.isoformat(), row.basic_eps)
        for row in extracted.rows
    ] == [
        ("FI", "2023-01-03", 1.0),
        ("FI", "2024-01-03", 2.0),
        ("FISV", "2023-01-03", 1.0),
        ("FISV", "2024-01-03", 2.0),
    ]


def test_reused_ticker_keeps_each_issuer_inside_its_own_window(
    tmp_path: Path,
) -> None:
    facts_path = tmp_path / "companyfacts.zip"

    def payload(cik: int, name: str, records: list[tuple[str, str, str, str, float]]) -> dict[str, object]:
        return {
            "cik": cik,
            "entityName": name,
            "facts": {
                "us-gaap": {
                    "EarningsPerShareBasic": {
                        "units": {
                            "USD/shares": [
                                {
                                    "accn": accession,
                                    "form": "10-Q",
                                    "filed": filed,
                                    "end": period_end,
                                    "start": quarter_start,
                                    "fy": period_end[:4],
                                    "fp": "Q4",
                                    "val": value,
                                }
                                for accession, filed, period_end, quarter_start, value in records
                            ]
                        }
                    }
                }
            },
        }

    with zipfile.ZipFile(facts_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "CIK0000000001.json",
            json.dumps(payload(1, "Old Issuer", [
                ("000000000120210001", "2021-01-01", "2020-12-31", "2020-10-01", 0.5),
                ("000000000120240001", "2024-01-02", "2022-12-31", "2022-10-01", 5.0),
            ])),
        )
        archive.writestr(
            "CIK0000000002.json",
            json.dumps(payload(2, "New Issuer", [
                ("000000000220210001", "2021-08-01", "2021-06-30", "2021-04-01", 10.0),
                ("000000000220240001", "2024-01-02", "2023-12-31", "2023-10-01", 2.0),
            ])),
        )
    spy_csv = tmp_path / "spy_days.csv"
    _write_csv(
        spy_csv,
        ("trade_date",),
        [(day,) for day in ("2021-01-04", "2021-08-02", "2024-01-03", "2025-12-31")],
    )
    security_master = SecurityMasterResult(
        rows=(
            SecurityMasterRow(
                "REUSE", "0000000001", "Old Issuer", date(2020, 1, 1), date(2022, 12, 31), "identity_manifest"
            ),
            SecurityMasterRow(
                "REUSE", "0000000002", "New Issuer", date(2023, 1, 1), date(2025, 12, 31), "identity_manifest"
            ),
        ),
        exclusions=(),
        acceptance_by_cik={},
        membership_union=("REUSE",),
        identity_manifest_sha256="c" * 64,
        submissions_archive_sha256="d" * 64,
        missing_submission_fragments=0,
        identity_extraction_rows=(
            IdentityExtractionRow(
                "REUSE", "0000000001", "Old Issuer", date(2020, 1, 1), date(2022, 12, 31),
                "old_issuer", "", "identity_manifest"
            ),
            IdentityExtractionRow(
                "REUSE", "0000000002", "New Issuer", date(2023, 1, 1), date(2025, 12, 31),
                "new_issuer", "", "identity_manifest"
            ),
        ),
    )

    extracted = sec_export.extract_fundamentals(
        facts_path,
        security_master,
        spy_csv,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert [(row.public_date.isoformat(), row.basic_eps) for row in extracted.rows] == [
        ("2021-01-04", 0.5),
        ("2024-01-03", 2.0),
    ]


def _write_csv(path: Path, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(pit_canonical_json_bytes(value))


def _write_json_zip(path: Path, member_name: str, payload: object) -> None:
    info = zipfile.ZipInfo(member_name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, json.dumps(payload, separators=(",", ":")))


def _synthetic_v3_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    membership_csv = tmp_path / "membership_v3.csv"
    membership_rows = [
        ("2021-01-04", "lineage_alpha", "nasdaq100", "1"),
        ("2021-01-04", "lineage_alpha", "russell2000", "1"),
        ("2021-01-04", "lineage_alpha", "sp500", "1"),
    ]
    _write_csv(
        membership_csv,
        ("effective_date", "security_lineage_id", "universe_id", "member"),
        membership_rows,
    )

    identities = {
        "IWM": {
            "provider_symbol": "IWM",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_iwm",
            "continuity_kind": "standalone",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
        "NEW": {
            "provider_symbol": "NEW",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-03",
            "admitted_end": "2025-12-31",
            "chain_id": "lineage_alpha",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": "OLD",
            "factor_anchor": True,
        },
        "OLD": {
            "provider_symbol": "OLD",
            "identity_asof": "2020-01-02",
            "admitted_start": "2020-01-01",
            "admitted_end": "2020-01-02",
            "chain_id": "lineage_alpha",
            "continuity_kind": "same_issuer_rename",
            "warmup_predecessor": None,
            "factor_anchor": False,
        },
        "QQQ": {
            "provider_symbol": "QQQ",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_qqq",
            "continuity_kind": "standalone",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
        "SPY": {
            "provider_symbol": "SPY",
            "identity_asof": "2025-12-31",
            "admitted_start": "2020-01-01",
            "admitted_end": "2025-12-31",
            "chain_id": "ref_spy",
            "continuity_kind": "standalone",
            "warmup_predecessor": None,
            "factor_anchor": True,
        },
    }
    transitions = [
        {
            "effective_date": "2020-01-03",
            "predecessor": "OLD",
            "successor": "NEW",
            "chain_id": "lineage_alpha",
            "continuity_kind": "same_issuer_rename",
        }
    ]
    identity_digest = pit_canonical_json_sha256(identities)
    transition_digest = pit_canonical_json_sha256(transitions)
    prices_csv = tmp_path / "prices.csv"
    price_days = [
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
        "2020-01-06",
        "2021-01-04",
        "2025-12-31",
    ]
    price_symbols = ["IWM", "NEW", "OLD", "QQQ", "SPY"]
    _write_csv(
        prices_csv,
        ("trade_date", "ticker", "open", "high", "low", "close", "volume"),
        [
            (day, ticker, "100", "101", "99", "100", "1000")
            for day in price_days
            for ticker in price_symbols
        ],
    )
    reference_coverage = {
        ticker: {
            "first_date": price_days[0],
            "last_date": price_days[-1],
            "session_count": len(price_days),
        }
        for ticker in ("SPY", "QQQ", "IWM")
    }
    spy_calendar = ("trade_date\n" + "\n".join(price_days) + "\n").encode("utf-8")
    reference_symbols = ["IWM", "QQQ", "SPY"]
    prices_provenance = tmp_path / "prices_provenance.json"
    _write_json(
        prices_provenance,
        {
            "prices_sha256": hashlib.sha256(prices_csv.read_bytes()).hexdigest(),
            "price_row_count": len(price_days) * len(price_symbols),
            "source_kind": "synthetic fixture",
            "source_sha256": "a" * 64,
            "price_identity_map_sha256": "b" * 64,
            "price_identity_request_contracts": identities,
            "price_identity_request_contracts_sha256": identity_digest,
            "price_identity_transitions": transitions,
            "price_identity_transitions_sha256": transition_digest,
            "start_date": "2020-01-01",
            "end_date": "2025-12-31",
            "non_tradable_reference_symbols_json": json.dumps(
                reference_symbols, separators=(",", ":")
            ),
            "non_tradable_reference_symbols_sha256": pit_canonical_json_sha256(
                reference_symbols
            ),
            "reference_symbol_coverage": reference_coverage,
            "spy_trading_days_sha256": hashlib.sha256(spy_calendar).hexdigest(),
            "symbols_with_no_prices": [],
        },
    )
    membership_provenance = tmp_path / "membership_v3_provenance.json"
    _write_json(
        membership_provenance,
        {
            "schema_version": 3,
            "kind": "pit_universe_membership_v3",
            "source_universes": ["nasdaq100", "russell2000", "sp500"],
            "universe_count": 3,
            "admission_status": "nonproduction_fixture",
            "source_evidence_mode": "nonproduction_fixture",
            "membership_sha256": hashlib.sha256(membership_csv.read_bytes()).hexdigest(),
            "event_count": 3,
            "lineage_count": 1,
            "first_effective_date": "2021-01-04",
            "last_effective_date": "2021-01-04",
            "coalesced_transition_count": 0,
            "universe_event_counts": {
                "nasdaq100": 1,
                "russell2000": 1,
                "sp500": 1,
            },
            "prices_provenance_sha256": hashlib.sha256(
                prices_provenance.read_bytes()
            ).hexdigest(),
            "price_identity_request_contracts_sha256": identity_digest,
            "price_identity_transitions_sha256": transition_digest,
            "inputs": [
                {
                    "admission_status": "nonproduction_fixture",
                    "event_csv_integrity_sha256": None,
                    "event_row_evidence_sha256": None,
                    "event_count": 1,
                    "membership_sha256": (str(index + 1) * 64),
                    "provenance_sha256": (str(index + 4) * 64),
                    "retrieved_at_utc": "2020-01-01T00:00:00Z",
                    "source_evidence_mode": "nonproduction_fixture",
                    "source_kind": "synthetic fixture",
                    "universe_id": universe,
                }
                for index, universe in enumerate(
                    ("nasdaq100", "russell2000", "sp500")
                )
            ],
        },
    )
    return membership_csv, membership_provenance, prices_provenance


def test_projection_cli_retains_source_hashes_and_reproducible_rename_ledger(
    tmp_path: Path,
) -> None:
    membership_csv, membership_provenance, prices_provenance = _synthetic_v3_inputs(
        tmp_path
    )
    projected_csv = tmp_path / "ticker_membership.csv"
    ledger_csv = tmp_path / "lineage_projection.csv"
    history_csv = tmp_path / "identity_extraction_history.csv"
    projection_provenance = tmp_path / "lineage_projection.json"

    assert bridge_main(
        [
            "--membership-v3-csv",
            str(membership_csv),
            "--membership-provenance",
            str(membership_provenance),
            "--prices-provenance",
            str(prices_provenance),
            "--output-ticker-membership-csv",
            str(projected_csv),
            "--output-projection-ledger-csv",
            str(ledger_csv),
            "--output-extraction-history-csv",
            str(history_csv),
            "--output-projection-provenance",
            str(projection_provenance),
        ]
    ) == 0

    assert projected_csv.read_text(encoding="utf-8") == (
        "effective_date,ticker,member\n"
        "2021-01-04,NEW,1\n"
    )
    assert history_csv.read_text(encoding="utf-8") == (
        "ticker,first_extraction_date,last_extraction_date,security_lineage_id,identity_segment_id\n"
        "NEW,2020-01-03,2025-12-31,lineage_alpha,\n"
        "OLD,2020-01-01,2020-01-02,lineage_alpha,\n"
    )
    provenance = json.loads(projection_provenance.read_text(encoding="utf-8"))
    assert provenance["source_v3_membership_sha256"] == hashlib.sha256(
        membership_csv.read_bytes()
    ).hexdigest()
    assert provenance["source_v3_membership_provenance_sha256"] == hashlib.sha256(
        membership_provenance.read_bytes()
    ).hexdigest()
    assert provenance["source_prices_provenance_sha256"] == hashlib.sha256(
        prices_provenance.read_bytes()
    ).hexdigest()
    ledger = ledger_csv.read_text(encoding="utf-8")
    assert "segment" not in ledger
    assert "union_membership_add" in ledger
    assert "identity_boundary_exit" not in ledger


def test_sec_exporter_embeds_bridge_and_preserves_synthetic_financial_rows(
    tmp_path: Path,
) -> None:
    membership_v3, membership_provenance, prices_provenance = _synthetic_v3_inputs(
        tmp_path
    )
    projected_csv = tmp_path / "ticker_membership.csv"
    ledger_csv = tmp_path / "lineage_projection.csv"
    history_csv = tmp_path / "identity_extraction_history.csv"
    projection_provenance = tmp_path / "lineage_projection.json"
    build_membership_projection(
        membership_csv=membership_v3,
        membership_provenance=membership_provenance,
        prices_provenance=prices_provenance,
        output_membership_csv=projected_csv,
        output_projection_ledger_csv=ledger_csv,
        output_extraction_history_csv=history_csv,
        output_projection_provenance=projection_provenance,
    )

    output_dir = tmp_path / "sec-output"
    output_dir.mkdir()
    (output_dir / "submissions.zip").write_bytes(b"synthetic submissions")
    (output_dir / "companyfacts.zip").write_bytes(b"synthetic companyfacts")
    names_csv = tmp_path / "security_names.csv"
    spy_csv = tmp_path / "spy.csv"
    identity_csv = tmp_path / "identity.csv"
    _write_csv(names_csv, ("ticker", "company_name"), [("OLD", "Old Co"), ("NEW", "New Co")])
    _write_csv(spy_csv, ("trade_date",), [("2020-01-01",), ("2020-01-02",)])
    identity_csv.write_text("synthetic identity\n", encoding="utf-8")
    consumed_hashes = sec_export._consumed_hashes(
        membership_csv=projected_csv,
        security_names_csv=names_csv,
        spy_trading_days_csv=spy_csv,
        identity_manifest_csv=identity_csv,
        output_dir=output_dir,
        identity_extraction_history_csv=history_csv,
    )
    start_date = date(2020, 1, 1)
    filing_date = date(2020, 1, 1)
    rows = (
        FundamentalRow(
            "OLD", "quarterly", date(2019, 12, 31), date(2020, 1, 1), total_revenue=100.0
        ),
        FundamentalRow(
            "NEW", "quarterly", date(2019, 12, 31), date(2020, 1, 4), total_revenue=125.0
        ),
    )
    audit = tuple(
        FundamentalAuditRow(
            row.ticker,
            row.statement_type,
            row.period_end,
            row.public_date,
            f"accession-{index}",
            "10-Q",
            filing_date,
            "2019",
            "Q4",
            "",
            "filed_date_fallback",
            "{}",
            "",
            "{}",
        )
        for index, row in enumerate(rows)
    )
    security_master = SecurityMasterResult(
        rows=(
            SecurityMasterRow("NEW", "0000000002", "New Co", date(2020, 1, 3), date(2020, 1, 5), "fixture"),
            SecurityMasterRow("OLD", "0000000001", "Old Co", date(2020, 1, 2), date(2020, 1, 2), "fixture"),
        ),
        exclusions=(),
        acceptance_by_cik={},
        membership_union=("NEW", "OLD"),
        identity_manifest_sha256=consumed_hashes["identity_manifest_csv_sha256"],
        submissions_archive_sha256=consumed_hashes["submissions_archive_sha256"],
        missing_submission_fragments=0,
    )
    fundamentals = FundamentalExportResult(
        rows=rows,
        audit_rows=audit,
        coverage={"fundamental_row_count": 2, "filed_date_fallback_count": 2},
        companyfacts_archive_sha256=consumed_hashes["companyfacts_archive_sha256"],
    )
    provenance = sec_export.publish_normalized_outputs(
        output_dir,
        security_master=security_master,
        fundamentals=fundamentals,
        archive_manifest={"schema_version": 1, "archives": {}},
        membership_csv=projected_csv,
        security_names_csv=names_csv,
        spy_trading_days_csv=spy_csv,
        identity_manifest_csv=identity_csv,
        consumed_hashes=consumed_hashes,
        start_date=start_date,
        end_date=date(2025, 12, 31),
        membership_lineage_projection_provenance=projection_provenance,
        identity_extraction_history_csv=history_csv,
    )

    bridge = provenance["financial_lineage_bridge_v1"]
    assert bridge["values_and_public_dates_transformed"] is False
    with (output_dir / "fundamentals.csv").open(encoding="utf-8", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert [(row["ticker"], row["public_date"], row["total_revenue"]) for row in exported] == [
        ("OLD", "2020-01-01", "100"),
        ("NEW", "2020-01-04", "125"),
    ]
    with (output_dir / "fundamentals_audit.csv").open(encoding="utf-8", newline="") as stream:
        audited = list(csv.DictReader(stream))
    assert [(row["ticker"], row["public_date"]) for row in audited] == [
        ("OLD", "2020-01-01"),
        ("NEW", "2020-01-04"),
    ]


def _actual_builder_case(tmp_path: Path) -> tuple[list[str], Path, Path]:
    """Run projection, real SEC master/extractor functions, publisher, then builder."""
    membership_v3, membership_provenance, prices_provenance = _synthetic_v3_inputs(
        tmp_path
    )
    projected_csv = tmp_path / "ticker_membership.csv"
    projection_ledger = tmp_path / "lineage_projection.csv"
    history_csv = tmp_path / "identity_extraction_history.csv"
    projection_provenance = tmp_path / "lineage_projection.json"
    build_membership_projection(
        membership_csv=membership_v3,
        membership_provenance=membership_provenance,
        prices_provenance=prices_provenance,
        output_membership_csv=projected_csv,
        output_projection_ledger_csv=projection_ledger,
        output_extraction_history_csv=history_csv,
        output_projection_provenance=projection_provenance,
    )

    output_dir = tmp_path / "sec-output"
    output_dir.mkdir()
    submissions_zip = output_dir / "submissions.zip"
    companyfacts_zip = output_dir / "companyfacts.zip"
    accession_old = "000000000120000001"
    accession_new = "000000000120000002"
    submission = {
        "cik": 1,
        "name": "Renamed Co",
        "tickers": ["OLD", "NEW"],
        "formerNames": [],
        "filings": {
            "recent": {
                "accessionNumber": [accession_old, accession_new],
                "form": ["10-Q", "10-Q"],
                "filingDate": ["2020-01-01", "2020-01-03"],
                "acceptanceDateTime": ["", ""],
            },
            "files": [],
        },
    }
    facts = {
        "cik": 1,
        "entityName": "Renamed Co",
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {
                    "units": {
                        "USD/shares": [
                            {
                                "accn": accession_old,
                                "form": "10-Q",
                                "filed": "2020-01-01",
                                "end": "2019-12-31",
                                "start": "2019-10-01",
                                "fy": "2019",
                                "fp": "Q4",
                                "frame": "CY2019Q4",
                                "val": 0.75,
                            },
                            {
                                "accn": accession_new,
                                "form": "10-Q",
                                "filed": "2020-01-03",
                                "end": "2019-12-31",
                                "start": "2019-10-01",
                                "fy": "2019",
                                "fp": "Q4",
                                "frame": "CY2019Q4",
                                "val": 1.25,
                            },
                        ]
                    }
                }
            }
        },
    }
    _write_json_zip(submissions_zip, "CIK0000000001.json", submission)
    _write_json_zip(companyfacts_zip, "CIK0000000001.json", facts)

    names_csv = tmp_path / "security_names.csv"
    spy_csv = tmp_path / "spy_days.csv"
    identity_csv = tmp_path / "identity_manifest.csv"
    _write_csv(
        names_csv,
        ("ticker", "company_name"),
        [("OLD", "Old Co"), ("NEW", "New Co")],
    )
    spy_days = ("2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06", "2021-01-04", "2025-12-31")
    _write_csv(spy_csv, ("trade_date",), [(day,) for day in spy_days])
    _write_csv(
        identity_csv,
        (
            "canonical_ticker", "provider_symbol", "identity_asof", "admitted_start",
            "admitted_end", "chain_id", "continuity_kind", "warmup_predecessor",
            "factor_anchor", "evidence_url",
        ),
        [
            ("NEW", "NEW", "2025-12-31", "2020-01-03", "2025-12-31", "lineage_alpha", "same_issuer_rename", "OLD", "1", "synthetic://new"),
            ("OLD", "OLD", "2020-01-02", "2020-01-01", "2020-01-02", "lineage_alpha", "same_issuer_rename", "", "0", "synthetic://old"),
        ],
    )
    archive_manifest = {
        "schema_version": 1,
        "archives": {
            "submissions.zip": {"sha256": hashlib.sha256(submissions_zip.read_bytes()).hexdigest()},
            "companyfacts.zip": {"sha256": hashlib.sha256(companyfacts_zip.read_bytes()).hexdigest()},
        },
    }
    consumed_hashes = sec_export._consumed_hashes(
        membership_csv=projected_csv,
        security_names_csv=names_csv,
        spy_trading_days_csv=spy_csv,
        identity_manifest_csv=identity_csv,
        output_dir=output_dir,
        identity_extraction_history_csv=history_csv,
    )
    security_master = sec_export.build_security_master(
        projected_csv,
        names_csv,
        submissions_zip,
        identity_csv,
        start_date=date(2021, 1, 1),
        end_date=date(2025, 12, 31),
        extraction_start_date=date(2020, 1, 1),
        identity_extraction_history_csv=history_csv,
    )
    fundamentals = sec_export.extract_fundamentals(
        companyfacts_zip,
        security_master,
        spy_csv,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
    )
    sec_export.publish_normalized_outputs(
        output_dir,
        security_master=security_master,
        fundamentals=fundamentals,
        archive_manifest=archive_manifest,
        membership_csv=projected_csv,
        security_names_csv=names_csv,
        spy_trading_days_csv=spy_csv,
        identity_manifest_csv=identity_csv,
        consumed_hashes=consumed_hashes,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        membership_start_date=date(2021, 1, 1),
        membership_lineage_projection_provenance=projection_provenance,
        identity_extraction_history_csv=history_csv,
    )

    industry_csv = tmp_path / "industry.csv"
    _write_csv(
        industry_csv,
        ("symbol", "as_of_date", "group_id", "group_rank", "group_members", "evidence_ids"),
        [("NEW", "2025-12-31", "fixture", "1", '["NEW"]', '["synthetic"]')],
    )
    industry_provenance = tmp_path / "industry_provenance.json"
    _write_json(
        industry_provenance,
        {
            "industry_sha256": hashlib.sha256(industry_csv.read_bytes()).hexdigest(),
            "membership_csv_sha256": hashlib.sha256(membership_v3.read_bytes()).hexdigest(),
            "data_cutoff": "2025-12-31",
            "row_count": 1,
            "symbol_count": 1,
            "first_as_of_date": "2025-12-31",
            "last_as_of_date": "2025-12-31",
            "source_kind": "synthetic fixture",
            "retrieved_at_utc": "2020-01-01T00:00:00Z",
        },
    )
    args = [
        "--schema-version", "3",
        "--membership-csv", str(membership_v3),
        "--prices-csv", str(tmp_path / "prices.csv"),
        "--fundamentals-csv", str(output_dir / "fundamentals.csv"),
        "--industry-csv", str(industry_csv),
        "--data-cutoff", "2025-12-31",
        "--evaluation-start", "2025-12-31",
        "--warmup-start", "2020-01-01",
        "--membership-provenance", str(membership_provenance),
        "--prices-provenance", str(prices_provenance),
        "--fundamentals-provenance", str(output_dir / "fundamentals_provenance.json"),
        "--industry-provenance", str(industry_provenance),
        "--output", str(tmp_path / "bundle.sqlite3"),
        "--manifest-output", str(tmp_path / "bundle_manifest.json"),
        "--allow-nonproduction-fixture",
    ]
    return args, membership_v3, output_dir / "fundamentals.csv"


def _run_builder(args: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.argv", ["build_pit_bundle.py", *args])
    return bundle_builder.main()


def test_production_builder_path_accepts_coherent_synthetic_bridge_and_warmup_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, fundamentals_csv = _actual_builder_case(tmp_path)

    assert _run_builder(args, monkeypatch) == 0

    with fundamentals_csv.open("r", encoding="utf-8", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert [(row["ticker"], row["period_end"], row["public_date"], row["basic_eps"]) for row in exported] == [
        ("NEW", "2019-12-31", "2020-01-02", "0.75"),
        ("NEW", "2019-12-31", "2020-01-06", "1.25"),
        ("OLD", "2019-12-31", "2020-01-02", "0.75"),
        ("OLD", "2019-12-31", "2020-01-06", "1.25"),
    ]
    with sqlite3.connect(tmp_path / "bundle.sqlite3") as connection:
        sqlite_rows = connection.execute(
            "SELECT ticker, period_end, public_date, basic_eps FROM fundamentals ORDER BY ticker, public_date"
        ).fetchall()
        dataset_metadata = dict(connection.execute("SELECT key, value FROM dataset_metadata"))
    assert sqlite_rows == [
        ("NEW", "2019-12-31", "2020-01-02", 0.75),
        ("NEW", "2019-12-31", "2020-01-06", 1.25),
        ("OLD", "2019-12-31", "2020-01-02", 0.75),
        ("OLD", "2019-12-31", "2020-01-06", 1.25),
    ]
    assert len(dataset_metadata["financial_lineage_extraction_history_sha256"]) == 64
    with (tmp_path / "bundle_manifest.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    assert manifest["bundle_sha256"]


def test_fixture_builder_rejects_unbound_destination_membership_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    provenance_path = Path(args[args.index("--fundamentals-provenance") + 1])
    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    document["source"] = "synthetic fixture"
    document.pop("financial_lineage_bridge_v1")
    document["membership_csv_sha256"] = "0" * 64
    _write_json(provenance_path, document)

    with pytest.raises(
        ValueError,
        match="synthetic fixture fundamentals provenance does not bind schema-V3 membership",
    ):
        _run_builder(args, monkeypatch)

    document["membership_csv_sha256"] = hashlib.sha256(membership_csv.read_bytes()).hexdigest()
    _write_json(provenance_path, document)
    assert _run_builder(args, monkeypatch) == 0


def test_production_builder_rejects_foreign_membership_relabelled_behind_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    with membership_csv.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    rows[-1][1] = "lineage_other"
    _write_csv(membership_csv, tuple(rows[0]), [tuple(row) for row in rows[1:]])
    membership_provenance = Path(args[args.index("--membership-provenance") + 1])
    document = json.loads(membership_provenance.read_text(encoding="utf-8"))
    document["membership_sha256"] = hashlib.sha256(membership_csv.read_bytes()).hexdigest()
    document["lineage_count"] = 2
    _write_json(membership_provenance, document)

    with pytest.raises(ValueError, match="foreign V3 inputs"):
        _run_builder(args, monkeypatch)


def test_production_builder_rejects_membership_relabel_without_projection_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    provenance_path = Path(args[args.index("--fundamentals-provenance") + 1])
    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    document["membership_csv_sha256"] = hashlib.sha256(membership_csv.read_bytes()).hexdigest()
    _write_json(provenance_path, document)

    with pytest.raises(ValueError, match="does not bind projected ticker membership"):
        _run_builder(args, monkeypatch)


def test_production_builder_rejects_foreign_projection_provenance_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    provenance_path = Path(args[args.index("--fundamentals-provenance") + 1])
    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    document["financial_lineage_bridge_v1"]["projection_provenance_sha256"] = "0" * 64
    _write_json(provenance_path, document)

    with pytest.raises(ValueError, match="projection provenance hash is invalid"):
        _run_builder(args, monkeypatch)


def test_production_builder_rejects_projection_ledger_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    exporter_provenance_path = Path(args[args.index("--fundamentals-provenance") + 1])
    exporter_provenance = json.loads(exporter_provenance_path.read_text(encoding="utf-8"))
    projection_path = (
        exporter_provenance_path.parent
        / exporter_provenance["financial_lineage_bridge_v1"]["projection_provenance_path"]
    ).resolve()
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    ledger_path = projection_path.parent / projection["references"]["lineage_projection_csv"]
    ledger_path.write_text(ledger_path.read_text(encoding="utf-8").replace("union_membership_add", "foreign_projection_add"), encoding="utf-8")

    with pytest.raises(ValueError, match="transformation ledger is inconsistent"):
        _run_builder(args, monkeypatch)


def test_production_builder_rejects_tampered_audit_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, fundamentals_csv = _actual_builder_case(tmp_path)
    audit_path = fundamentals_csv.with_name("fundamentals_audit.csv")
    original = audit_path.read_text(encoding="utf-8")
    audit_path.write_text(original.replace("2020-01-06", "2020-01-07", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="does not bind its audit CSV"):
        _run_builder(args, monkeypatch)


def test_production_builder_rejects_tampered_security_master_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, fundamentals_csv = _actual_builder_case(tmp_path)
    master_path = fundamentals_csv.with_name("security_master.csv")
    original = master_path.read_text(encoding="utf-8")
    assert "0000000001" in original
    master_path.write_text(original.replace("0000000001", "0000000002", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="does not bind its security master inputs"):
        _run_builder(args, monkeypatch)


def test_nonproduction_builder_guard_still_requires_explicit_fixture_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _membership_csv, _fundamentals_csv = _actual_builder_case(tmp_path)
    args.remove("--allow-nonproduction-fixture")

    with pytest.raises(
        ValueError,
        match="nonproduction V3 membership requires explicit fixture-build opt-in",
    ):
        _run_builder(args, monkeypatch)


@pytest.mark.parametrize(
    ("before", "after"),
    (("1.25", "1.26"), ("2020-01-06", "2020-01-03")),
)
def test_production_builder_rejects_tampered_financial_values_or_publication_dates(
    tmp_path: Path,
    before: str,
    after: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _membership_csv, fundamentals_csv = _actual_builder_case(tmp_path)
    original = fundamentals_csv.read_text(encoding="utf-8")
    assert before in original
    fundamentals_csv.write_text(original.replace(before, after, 1), encoding="utf-8")

    with pytest.raises(ValueError, match="does not bind the fundamentals CSV"):
        _run_builder(args, monkeypatch)
