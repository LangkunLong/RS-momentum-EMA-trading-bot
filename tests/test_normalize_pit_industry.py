"""Focused tests for the offline Wikipedia PIT industry normalizer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools.normalize_pit_industry import normalize_revision


def _sessions(path: Path, *dates: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("trade_date",))
        writer.writerows((value,) for value in dates)


def _membership(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("effective_date", "ticker", "member"))
        writer.writerows(rows)


def _compact_revision(path: Path, *, revid: int = 123) -> None:
    path.write_text(
        json.dumps(
            {
                "revid": revid,
                "timestamp": "2024-01-02T15:00:00Z",
                "rows": [
                    {"Symbol": "BBB", "GICS Sub-Industry": "Software [1]", "CIK": "789"},
                    {"Symbol": "AAA", "GICS Sub-Industry": "Semiconductors"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_compact_json_maps_to_next_session_and_ranker_contract(tmp_path: Path) -> None:
    revision = tmp_path / "revision.json"
    sessions = tmp_path / "sessions.csv"
    output = tmp_path / "classification.csv"
    _compact_revision(revision)
    _sessions(sessions, "2024-01-02", "2024-01-03")

    result = normalize_revision(revision_export=revision, sessions=("2024-01-02", "2024-01-03"), output=output)

    assert result.revid == 123
    assert result.as_of_date == "2024-01-03"
    assert result.rows == 2
    with output.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["symbol"] for row in rows] == ["AAA", "BBB"]
    assert {row["as_of_date"] for row in rows} == {"2024-01-03"}
    assert rows[0]["group_id"] == "gics-subindustry:Semiconductors"
    assert rows[1]["group_id"] == "gics-subindustry:Software"
    assert json.loads(rows[0]["evidence_ids"]) == ["wikipedia:revid:123"]


def test_html_revision_reads_metadata_and_table(tmp_path: Path) -> None:
    revision = tmp_path / "revision.html"
    sessions = tmp_path / "sessions.csv"
    output = tmp_path / "classification.csv"
    revision.write_text(
        """
        <html><head>
          <meta name="revision-id" content="456">
          <meta name="revision-timestamp" content="2024-02-01T20:00:00Z">
        </head><body>
          <table><thead><tr><th>Symbol</th><th>GICS Sub-Industry</th><th>CIK</th></tr></thead>
          <tbody><tr><td>AAA</td><td>Application Software</td><td>123456789</td></tr></tbody>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    _sessions(sessions, "2024-02-02")

    result = normalize_revision(revision_export=revision, sessions=("2024-02-02",), output=output)

    assert result.revid == 456
    assert result.as_of_date == "2024-02-02"
    with output.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {
            "symbol": "AAA",
            "as_of_date": "2024-02-02",
            "group_id": "gics-subindustry:Application Software",
            "evidence_ids": '["wikipedia:revid:456"]',
        }
    ]


def test_pit_membership_is_an_exact_active_set_gate(tmp_path: Path) -> None:
    revision = tmp_path / "revision.json"
    sessions = tmp_path / "sessions.csv"
    membership = tmp_path / "membership.csv"
    _compact_revision(revision)
    _sessions(sessions, "2024-01-03")
    _membership(membership, [("2024-01-03", "AAA", "1"), ("2024-01-03", "BBB", "1")])

    output = tmp_path / "ok.csv"
    normalize_revision(
        revision_export=revision,
        sessions=("2024-01-03",),
        membership_csv=membership,
        output=output,
    )
    assert output.is_file()

    missing_revision = tmp_path / "missing.json"
    missing_revision.write_text(
        json.dumps(
            {
                "revid": 124,
                "timestamp": "2024-01-02T15:00:00Z",
                "rows": [{"Symbol": "AAA", "GICS Sub-Industry": "Semiconductors"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"missing=\['BBB'\]"):
        normalize_revision(
            revision_export=missing_revision,
            sessions=("2024-01-03",),
            membership_csv=membership,
            output=tmp_path / "missing.csv",
        )


@pytest.mark.parametrize(
    ("payload", "sessions", "message"),
    [
        (
            {
                "revid": 1,
                "timestamp": "2024-01-02T15:00:00Z",
                "rows": [
                    {"Symbol": "AAA", "GICS Sub-Industry": "Software"},
                    {"Symbol": "AAA", "GICS Sub-Industry": "Hardware"},
                ],
            },
            ("2024-01-03",),
            "duplicate",
        ),
        (
            {
                "revid": 1,
                "timestamp": "2024-01-02T15:00:00",
                "rows": [{"Symbol": "AAA", "GICS Sub-Industry": "Software"}],
            },
            ("2024-01-03",),
            "timezone",
        ),
        (
            {
                "revid": 1,
                "timestamp": "2024-01-02T15:00:00Z",
                "rows": [{"Symbol": "AAA", "GICS Sub-Industry": "Software"}],
            },
            ("2024-01-02",),
            "no later",
        ),
    ],
)
def test_normalizer_fails_closed_on_ambiguous_or_noncausal_input(
    tmp_path: Path,
    payload: dict[str, object],
    sessions: tuple[str, ...],
    message: str,
) -> None:
    revision = tmp_path / "revision.json"
    revision.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        normalize_revision(revision_export=revision, sessions=sessions, output=tmp_path / "output.csv")


def test_mediawiki_api_shaped_revision_is_supported(tmp_path: Path) -> None:
    revision = tmp_path / "api.json"
    revision.write_text(
        json.dumps(
            {
                "query": {
                    "pages": {
                        "123": {
                            "revisions": [
                                {
                                    "revid": 987,
                                    "timestamp": "2024-03-01T01:00:00Z",
                                    "rows": [{"Symbol": "AAA", "GICS Sub-Industry": "Banks"}],
                                }
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = normalize_revision(
        revision_export=revision,
        sessions=("2024-03-01", "2024-03-04"),
        output=tmp_path / "classification.csv",
    )
    assert result.revid == 987
    assert result.as_of_date == "2024-03-04"
