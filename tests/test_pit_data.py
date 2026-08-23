"""Offline trust-boundary tests for confined price-cache imports."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

import export_pit_prices as exporter


def _cache(path: Path, *, extra_table: bool = False) -> str:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE dataset_cache (cache_key TEXT PRIMARY KEY, cache_kind TEXT NOT NULL, created_at TEXT NOT NULL, payload BLOB NOT NULL)")
        conn.execute("INSERT INTO dataset_cache VALUES (?, ?, ?, ?)", ("price::1d::2024-01-01::2024-01-02::AAPL,SPY", "price", "2024-01-03T00:00:00Z", b"not-a-pickle"))
        if extra_table:
            conn.execute("CREATE TABLE surprise (payload BLOB)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cache_copy_validates_sqlite_schema_without_deserializing_worker_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: host unpickled worker-controlled cache payloads before schema validation."""
    source = tmp_path / "worker.sqlite3"
    digest = _cache(source)
    monkeypatch.setattr("pickle.loads", lambda *_: (_ for _ in ()).throw(AssertionError("payload deserialized")))
    destination = tmp_path / "copy"
    destination.mkdir()
    snapshot = exporter._copy_and_validate_cache(source, digest, destination)
    assert snapshot.key_count == 1
    assert snapshot.path.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("wrong_hash,extra_table,expected", [(True, False, "cache changed"), (False, True, "unexpected SQLite")])
def test_cache_copy_rejects_bad_hash_and_extra_schema(tmp_path: Path, wrong_hash: bool, extra_table: bool, expected: str) -> None:
    source = tmp_path / "worker.sqlite3"
    digest = _cache(source, extra_table=extra_table)
    destination = tmp_path / "copy"
    destination.mkdir()
    with pytest.raises(ValueError, match=expected):
        exporter._copy_and_validate_cache(source, "0" * 64 if wrong_hash else digest, destination)
