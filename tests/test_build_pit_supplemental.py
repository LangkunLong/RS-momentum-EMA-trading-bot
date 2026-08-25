from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import csv

import pytest

from core.pit_diagnosis.rulebook import canonical_sha256
from core.pit_diagnosis.supplemental import SQLiteSupplementalPITProvider
from tools.build_pit_supplemental import build_artifact


def _write_inputs(root: Path, *, cutoff: str = "2025-12-31") -> tuple[Path, Path]:
    institutional = root / "institutional.csv"
    with institutional.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("symbol", "as_of_date", "ownership_percent", "holder_count", "previous_holder_count", "evidence_ids"))
        writer.writerow(("AAA", "2025-06-30", "0.25", "12", "10", '["sec13f:aaa:2025q2"]'))
        writer.writerow(("AAA", cutoff, "0.30", "15", "12", '["sec13f:aaa:2025q4"]'))
    industry = root / "industry.csv"
    with industry.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("symbol", "as_of_date", "group_id", "group_rank", "group_members", "evidence_ids"))
        writer.writerow(("AAA", "2025-06-30", "technology", "3", '["AAA", "BBB"]', '["industry:2025-06-30"]'))
        writer.writerow(("AAA", cutoff, "technology", "2", '["BBB", "AAA"]', '["industry:2025-12-31"]'))
    return institutional, industry


def test_builder_seals_provenance_and_provider_reads_as_of_rows(tmp_path: Path) -> None:
    institutional, industry = _write_inputs(tmp_path)
    output = tmp_path / "supplemental.sqlite3"
    provenance = tmp_path / "supplemental.provenance.json"

    result = build_artifact(
        institutional_csv=institutional,
        industry_csv=industry,
        source_kind="sec-13f-plus-dated-industry-export",
        source_references=("sec-13f:2025q4", "industry:immutable-2025q4"),
        data_cutoff="2025-12-31",
        output=output,
        provenance_output=provenance,
    )

    assert result.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = json.loads(provenance.read_text(encoding="utf-8"))
    assert canonical_sha256(manifest) == result.provenance_sha256
    assert manifest["source_references"] == ["industry:immutable-2025q4", "sec-13f:2025q4"]
    connection = sqlite3.connect(output)
    try:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
    finally:
        connection.close()
    assert metadata["provenance_sha256"] == result.provenance_sha256

    with SQLiteSupplementalPITProvider(output, result.sha256) as provider:
        institutional_snapshot = provider.institutional_snapshot("AAA", "2025-09-01")
        assert institutional_snapshot.as_of_date == "2025-06-30"
        assert institutional_snapshot.holder_count == 12
        industry_snapshot = provider.industry_group_snapshot("AAA", "2025-12-31")
        assert industry_snapshot.group_rank == 2
        assert industry_snapshot.group_members == ("AAA", "BBB")


def test_production_coverage_manifest_is_required_for_strict_preflight(tmp_path: Path) -> None:
    institutional, industry = _write_inputs(tmp_path)
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "production",
                "expected_institutional_symbols": ["AAA"],
                "expected_industry_symbols": ["AAA"],
                "expected_institutional_dates": ["2025-06-30", "2025-12-31"],
                "expected_industry_dates": ["2025-06-30", "2025-12-31"],
            }
        ),
        encoding="utf-8",
    )
    result = build_artifact(
        institutional_csv=institutional,
        industry_csv=industry,
        source_kind="sec-13f-plus-dated-industry-export",
        data_cutoff="2025-12-31",
        output=tmp_path / "production.sqlite3",
        coverage_manifest=coverage,
    )
    with SQLiteSupplementalPITProvider(result.output, result.sha256) as provider:
        provider.require_strict_inputs()
    assert result.coverage_manifest_sha256


def test_builder_is_byte_reproducible_for_identical_inputs(tmp_path: Path) -> None:
    institutional, industry = _write_inputs(tmp_path)
    first = build_artifact(
        institutional_csv=institutional,
        industry_csv=industry,
        source_kind="offline-fixture",
        data_cutoff="2025-12-31",
        output=tmp_path / "one.sqlite3",
    )
    second = build_artifact(
        institutional_csv=institutional,
        industry_csv=industry,
        source_kind="offline-fixture",
        data_cutoff="2025-12-31",
        output=tmp_path / "two.sqlite3",
    )
    assert first.sha256 == second.sha256
    assert first.provenance_sha256 == second.provenance_sha256
    assert first.output.read_bytes() == second.output.read_bytes()


def test_builder_rejects_future_rows_and_duplicate_keys(tmp_path: Path) -> None:
    institutional, industry = _write_inputs(tmp_path)
    institutional.write_text(
        institutional.read_text(encoding="utf-8")
        + 'AAA,2026-01-01,0.40,20,15,"[""future""]"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="after data_cutoff"):
        build_artifact(
            institutional_csv=institutional,
            industry_csv=industry,
            source_kind="offline-fixture",
            data_cutoff="2025-12-31",
            output=tmp_path / "future.sqlite3",
        )

    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(
        "symbol,as_of_date,ownership_percent,holder_count,previous_holder_count,evidence_ids\n"
        'AAA,2025-01-01,0.20,10,8,"[""a""]"\n'
        'AAA,2025-01-01,0.25,11,10,"[""b""]"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate institutional"):
        build_artifact(
            institutional_csv=duplicate,
            industry_csv=industry,
            source_kind="offline-fixture",
            data_cutoff="2025-12-31",
            output=tmp_path / "duplicate.sqlite3",
        )
