from __future__ import annotations

import csv

import build_pit_bundle
import pytest


_FIELDS = (
    "ticker",
    "statement_type",
    "period_end",
    "public_date",
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "common_stock",
    "total_stockholders_equity",
    "shares_outstanding",
    "held_percent_institutions",
    "institution_count",
    "prev_institution_count",
)


def _write_institutional_export(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(_FIELDS)
        writer.writerows(rows)


def test_bundle_loader_derives_first_trend_and_carries_it_across_rename(tmp_path):
    exports = tmp_path / "institutional-v5.csv"
    _write_institutional_export(
        exports,
        [
            (
                "OLD", "institutional", "2024-03-31", "2024-05-15",
                "", "", "", "", "", "", "1000000", "0.35", "10", "",
            ),
            (
                "NEW", "institutional", "2024-06-30", "2024-08-16",
                "", "", "", "", "", "", "", "", "12", "",
            ),
        ],
    )
    identities = {
        "OLD": {
            "chain_id": "issuer_a",
            "admitted_start": "2024-01-01",
            "admitted_end": "2024-08-15",
        },
        "NEW": {
            "chain_id": "issuer_a",
            "admitted_start": "2024-08-16",
            "admitted_end": "2024-12-31",
        },
    }
    transitions = (
        {
            "chain_id": "issuer_a",
            "effective_date": "2024-08-16",
            "predecessor": "OLD",
            "successor": "NEW",
        },
    )

    rows = build_pit_bundle._load_institutional_fundamentals(
        [exports],
        "2024-12-31",
        ticker_parser=lambda value: value,
        identities=identities,
        transitions=transitions,
        segment_contract=None,
    )

    assert [(row[0], row[12], row[13]) for row in rows] == [
        ("OLD", 10, None),
        ("NEW", 12, 10),
    ]
    assert rows[1][10] is None
    assert rows[1][11] is None


def test_bundle_loader_rejects_mapping_to_inactive_identity(tmp_path):
    exports = tmp_path / "institutional-v5.csv"
    _write_institutional_export(
        exports,
        [
            (
                "OLD", "institutional", "2024-03-31", "2024-08-16",
                "", "", "", "", "", "", "", "0.35", "10", "",
            ),
        ],
    )
    identities = {
        "OLD": {
            "chain_id": "issuer_a",
            "admitted_start": "2024-01-01",
            "admitted_end": "2024-08-15",
        },
        "NEW": {
            "chain_id": "issuer_a",
            "admitted_start": "2024-08-16",
            "admitted_end": "2024-12-31",
        },
    }
    transitions = (
        {
            "chain_id": "issuer_a",
            "effective_date": "2024-08-16",
            "predecessor": "OLD",
            "successor": "NEW",
        },
    )

    with pytest.raises(ValueError, match="not the active"):
        build_pit_bundle._load_institutional_fundamentals(
            [exports],
            "2024-12-31",
            ticker_parser=lambda value: value,
            identities=identities,
            transitions=transitions,
            segment_contract=None,
        )
