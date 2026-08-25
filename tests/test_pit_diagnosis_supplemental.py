from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.pit_diagnosis.supplemental import SQLiteSupplementalPITProvider
from pit_diagnosis import build_parser


def _build_input(path: Path, *, cutoff: str = "2024-12-31") -> str:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE institutional_snapshots(
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                ownership_percent REAL NOT NULL,
                holder_count INTEGER NOT NULL,
                previous_holder_count INTEGER NOT NULL,
                evidence_ids TEXT NOT NULL,
                PRIMARY KEY(symbol, as_of_date)
            );
            CREATE TABLE industry_group_snapshots(
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_rank INTEGER NOT NULL,
                group_members TEXT NOT NULL,
                evidence_ids TEXT NOT NULL,
                PRIMARY KEY(symbol, as_of_date)
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("schema_version", "1"),
                ("source_kind", "offline-fixture"),
                ("data_cutoff", cutoff),
                ("provenance_sha256", "1" * 64),
            ),
        )
        connection.executemany(
            "INSERT INTO institutional_snapshots VALUES (?,?,?,?,?,?)",
            (
                ("AAA", "2024-01-01", 0.40, 10, 8, json.dumps(["13f:aaa:2023q4"])),
                ("AAA", "2024-06-30", 0.50, 12, 10, json.dumps(["13f:aaa:2024q2"])),
            ),
        )
        connection.executemany(
            "INSERT INTO industry_group_snapshots VALUES (?,?,?,?,?,?)",
            (
                ("AAA", "2024-01-01", "technology", 1, json.dumps(["AAA", "BBB"]), json.dumps(["group:2024-01-01"])),
                ("AAA", "2024-06-30", "technology", 1, json.dumps(["AAA", "BBB"]), json.dumps(["group:2024-06-30"])),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def test_sqlite_provider_selects_latest_snapshot_without_future_fallback(tmp_path: Path) -> None:
    path = tmp_path / "supplemental.sqlite3"
    digest = _build_input(path)

    with SQLiteSupplementalPITProvider(path, digest) as provider:
        before_first = provider.institutional_snapshot("AAA", "2023-12-31")
        assert not before_first.available

        early = provider.institutional_snapshot("AAA", "2024-03-01")
        assert early.as_of_date == "2024-01-01"
        assert early.holder_count == 10

        latest = provider.institutional_snapshot("AAA", "2024-12-31")
        assert latest.as_of_date == "2024-06-30"
        assert latest.ownership_percent == pytest.approx(0.50)

        group = provider.industry_group_snapshot("AAA", "2024-03-01")
        assert group.as_of_date == "2024-01-01"
        assert group.group_members == ("AAA", "BBB")


def test_sqlite_provider_is_hash_pinned_and_rejects_rows_after_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "supplemental.sqlite3"
    _build_input(path)
    with pytest.raises(ValueError, match="SHA-256"):
        SQLiteSupplementalPITProvider(path, "2" * 64)

    future_path = tmp_path / "future.sqlite3"
    future_digest = _build_input(future_path, cutoff="2024-12-31")
    connection = sqlite3.connect(future_path)
    try:
        connection.execute(
            "INSERT INTO institutional_snapshots VALUES (?,?,?,?,?,?)",
            ("AAA", "2025-01-01", 0.55, 13, 12, json.dumps(["future"])),
        )
        connection.commit()
    finally:
        connection.close()
    future_digest = hashlib.sha256(future_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="data_cutoff"):
        SQLiteSupplementalPITProvider(future_path, future_digest)


def test_strict_preflight_rejects_one_sided_supplemental_input(tmp_path: Path) -> None:
    path = tmp_path / "supplemental.sqlite3"
    digest = _build_input(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM industry_group_snapshots")
        connection.commit()
    finally:
        connection.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with SQLiteSupplementalPITProvider(path, digest) as provider:
        with pytest.raises(ValueError, match="industry_group_snapshots"):
            provider.require_strict_inputs()


def test_build_facts_parser_exposes_strict_preflight_switch(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "build-facts",
            "--pit-bundle", str(tmp_path / "bundle.sqlite3"),
            "--pit-bundle-sha256", "a" * 64,
            "--rulebook", str(tmp_path / "rulebook.json"),
            "--output", str(tmp_path / "facts.sqlite3"),
            "--checkpoint", str(tmp_path / "facts.checkpoint.json"),
            "--progress", str(tmp_path / "facts.progress.jsonl"),
            "--strict-canslim",
        ]
    )
    assert args.strict_canslim is True


def test_build_facts_accepts_supplemental_input_pair(tmp_path: Path) -> None:
    path = tmp_path / "supplemental.sqlite3"
    digest = _build_input(path)
    args = build_parser().parse_args(
        [
            "build-facts",
            "--pit-bundle", str(tmp_path / "bundle.sqlite3"),
            "--pit-bundle-sha256", "a" * 64,
            "--rulebook", str(tmp_path / "rulebook.json"),
            "--output", str(tmp_path / "facts.sqlite3"),
            "--checkpoint", str(tmp_path / "facts.checkpoint.json"),
            "--progress", str(tmp_path / "facts.progress.jsonl"),
            "--supplemental-input", str(path),
            "--supplemental-sha256", digest,
        ]
    )
    assert args.supplemental_input == path
    assert args.supplemental_sha256 == digest


def test_run_exposes_explicit_strict_canslim_switch(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "run",
            "--pit-bundle", str(tmp_path / "bundle.sqlite3"),
            "--pit-bundle-sha256", "a" * 64,
            "--baseline-run", str(tmp_path / "baseline"),
            "--rulebook", str(tmp_path / "rulebook.json"),
            "--experiment-catalog", str(tmp_path / "catalog.json"),
            "--fact-cache", str(tmp_path / "facts.sqlite3"),
            "--fact-cache-sha256", "b" * 64,
            "--output-root", str(tmp_path / "runs"),
            "--checkpoint-root", str(tmp_path / "checkpoints"),
            "--strict-canslim",
        ]
    )
    assert args.strict_canslim is True
