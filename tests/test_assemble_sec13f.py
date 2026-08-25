from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools.normalize_sec13f import assemble_institutional_csv


_FIELDS = ("symbol", "as_of_date", "ownership_percent", "holder_count", "previous_holder_count", "evidence_ids")
_MANIFEST_FIELDS = ("quarter", "institutional_csv", "source_reference", "evidence_ids")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)
    return path


def _quarter_files(tmp_path: Path) -> tuple[Path, Path]:
    q1 = _write_csv(
        tmp_path / "2024q1.csv",
        _FIELDS,
        [
            ("AAA", "2024-05-01", "0.20", "2", "99", '["row:q1:aaa"]'),
            ("BBB", "2024-05-01", "0.10", "1", "99", '["row:q1:bbb"]'),
        ],
    )
    q2 = _write_csv(
        tmp_path / "2024q2.csv",
        _FIELDS,
        [
            ("AAA", "2024-08-01", "0.30", "3", "99", '["row:q2:aaa"]'),
            ("CCC", "2024-08-01", "0.40", "4", "99", '["row:q2:ccc"]'),
        ],
    )
    return q1, q2


def _manifest_csv(path: Path, rows: list[tuple[str, ...]]) -> Path:
    return _write_csv(path, _MANIFEST_FIELDS, rows)


def test_assembler_replaces_previous_counts_and_unions_manifest_evidence(tmp_path: Path) -> None:
    q1, q2 = _quarter_files(tmp_path)
    manifest = _manifest_csv(
        tmp_path / "quarters.csv",
        [
            ("2024Q1", q1.name, "sec13f:2024q1", '["manifest:q1"]'),
            ("2024Q2", q2.name, "sec13f:2024q2", '["manifest:q2"]'),
        ],
    )
    output = tmp_path / "institutional.csv"

    result = assemble_institutional_csv(manifest=manifest, output=output)

    assert result.quarter_count == 2
    assert result.input_rows == result.output_rows == 4
    assert result.source_references == ("sec13f:2024q1", "sec13f:2024q2")
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["symbol"], row["as_of_date"], row["previous_holder_count"]) for row in rows] == [
        ("AAA", "2024-05-01", "0"),
        ("AAA", "2024-08-01", "2"),
        ("BBB", "2024-05-01", "0"),
        ("CCC", "2024-08-01", "0"),
    ]
    assert rows[1]["ownership_percent"] == "0.3"
    assert rows[1]["holder_count"] == "3"
    assert set(json.loads(rows[1]["evidence_ids"])) == {
        "manifest:q2",
        "row:q2:aaa",
        "sec13f:2024q2",
    }


def test_assembler_accepts_versioned_json_manifest_and_cutoff(tmp_path: Path) -> None:
    q1, q2 = _quarter_files(tmp_path)
    manifest = tmp_path / "quarters.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quarters": [
                    {"quarter": "2024Q1", "institutional_csv": q1.name, "source_reference": "q1", "evidence_ids": ["m1"]},
                    {"quarter": "2024Q2", "institutional_csv": q2.name, "source_reference": "q2", "evidence_ids": ["m2"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = assemble_institutional_csv(manifest=manifest, output=tmp_path / "out.csv", data_cutoff="2024-12-31")
    assert result.output_rows == 4


def test_assembler_rejects_duplicate_snapshot_across_quarters(tmp_path: Path) -> None:
    q1, _ = _quarter_files(tmp_path)
    duplicate = _write_csv(
        tmp_path / "2024q2-duplicate.csv",
        _FIELDS,
        [("AAA", "2024-05-01", "0.21", "3", "0", '["duplicate"]')],
    )
    manifest = _manifest_csv(
        tmp_path / "quarters.csv",
        [("2024Q1", q1.name, "q1", '["e1"]'), ("2024Q2", duplicate.name, "q2", '["e2"]')],
    )
    with pytest.raises(ValueError, match="duplicate institutional snapshot"):
        assemble_institutional_csv(manifest=manifest, output=tmp_path / "out.csv")


def test_assembler_rejects_non_monotonic_manifest(tmp_path: Path) -> None:
    q1, q2 = _quarter_files(tmp_path)
    manifest = _manifest_csv(
        tmp_path / "quarters.csv",
        [("2024Q2", q2.name, "q2", '["e2"]'), ("2024Q1", q1.name, "q1", '["e1"]')],
    )
    with pytest.raises(ValueError, match="strictly chronological"):
        assemble_institutional_csv(manifest=manifest, output=tmp_path / "out.csv")


def test_assembler_rejects_staggered_event_snapshots_without_manager_state(tmp_path: Path) -> None:
    staggered = _write_csv(
        tmp_path / "2024q1-staggered.csv",
        _FIELDS,
        [
            ("AAA", "2024-05-01", "0.20", "2", "0", '["a"]'),
            ("BBB", "2024-05-13", "0.10", "1", "0", '["b"]'),
        ],
    )
    manifest = _manifest_csv(tmp_path / "quarters.csv", [("2024Q1", staggered.name, "q1", '["e1"]')])
    with pytest.raises(ValueError, match="one aligned as_of_date"):
        assemble_institutional_csv(manifest=manifest, output=tmp_path / "out.csv")


def test_assembler_rejects_future_rows_and_unknown_manifest_fields(tmp_path: Path) -> None:
    q1, _ = _quarter_files(tmp_path)
    manifest = _manifest_csv(tmp_path / "quarters.csv", [("2024Q1", q1.name, "q1", '["e1"]')])
    with pytest.raises(ValueError, match="after data_cutoff"):
        assemble_institutional_csv(manifest=manifest, output=tmp_path / "out.csv", data_cutoff="2024-04-30")

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quarters": [
                    {"quarter": "2024Q1", "institutional_csv": q1.name, "source_reference": "q1", "evidence_ids": [], "extra": "reject"}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fields must be exactly"):
        assemble_institutional_csv(manifest=bad, output=tmp_path / "bad-out.csv")
