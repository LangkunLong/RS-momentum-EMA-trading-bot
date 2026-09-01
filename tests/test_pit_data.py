"""Offline trust-boundary tests for confined price-cache imports and PIT bars."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

import export_pit_prices as exporter
from core.pit_data import PITDataBundle
from core.pit_provenance import PIT_NON_TRADABLE_REFERENCE_SYMBOLS


def _cache(path: Path, *, extra_table: bool = False) -> str:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE dataset_cache (cache_key TEXT PRIMARY KEY, cache_kind TEXT NOT NULL, "
            "created_at TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT INTO dataset_cache VALUES (?, ?, ?, ?)",
            (
                "price::1d::2024-01-02::2024-01-05::AAPL,IWM,QQQ,SPY",
                "price",
                "2024-01-06T00:00:00Z",
                b"worker-controlled-not-a-pickle",
            ),
        )
        if extra_table:
            conn.execute("CREATE TABLE surprise (payload BLOB)")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _price_rows(*, missing_spy: bool = False, partial_aapl: bool = False, duplicate: bool = False) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for index, day in enumerate(("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")):
        for reference in PIT_NON_TRADABLE_REFERENCE_SYMBOLS:
            if not (missing_spy and reference == "SPY"):
                rows.append((day, reference, 100, 101, 99, 100, 1_000))
        if not (partial_aapl and index == 3):
            rows.append((day, "AAPL", 100, 101, 99, 100, 1_000))
    if duplicate:
        rows.append(("2024-01-05", "AAPL", 100, 101, 99, 100, 1_000))
    return sorted(rows)


def _write_prices(path: Path, rows: list[tuple[object, ...]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("trade_date", "ticker", "open", "high", "low", "close", "volume"))
        writer.writerows(rows)


def _membership() -> exporter.Membership:
    return exporter.Membership(((pd.Timestamp("2024-01-02").date(), "AAPL", True),), ("AAPL",))


def test_worker_cache_and_spy_aapl_export_are_validated_without_payload_deserialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: host decoded worker data or rejected a complete reference/AAPL export."""
    source = tmp_path / "worker.sqlite3"
    digest = _cache(source)
    monkeypatch.setattr(
        "pickle.loads",
        lambda *_: (_ for _ in ()).throw(AssertionError("worker payload was deserialized")),
    )
    destination = tmp_path / "copy"
    destination.mkdir()
    snapshot = exporter._copy_and_validate_cache(source, digest, destination)
    prices = tmp_path / "prices.csv"
    _write_prices(prices, _price_rows())
    metrics, spy_days = exporter._validate_prices(
        prices,
        _membership(),
        pd.Timestamp("2024-01-02").date(),
        pd.Timestamp("2024-01-05").date(),
    )
    assert snapshot.key_count == 1
    assert metrics["coverage_pct"] == 100.0
    assert metrics["reference_symbol_coverage"] == {
        reference: {
            "first_date": "2024-01-02",
            "last_date": "2024-01-05",
            "session_count": 4,
        }
        for reference in ("IWM", "QQQ", "SPY")
    }
    assert tuple(str(item) for item in spy_days) == ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")


@pytest.mark.parametrize(
    ("wrong_hash", "extra_table", "expected"),
    [(True, False, "cache changed"), (False, True, "unexpected SQLite")],
)
def test_worker_cache_rejects_bad_hash_and_extra_schema(
    tmp_path: Path, wrong_hash: bool, extra_table: bool, expected: str,
) -> None:
    source = tmp_path / "worker.sqlite3"
    digest = _cache(source, extra_table=extra_table)
    destination = tmp_path / "copy"
    destination.mkdir()
    with pytest.raises(ValueError, match=expected):
        exporter._copy_and_validate_cache(source, "0" * 64 if wrong_hash else digest, destination)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (_price_rows(duplicate=True), "violates the output contract"),
        (_price_rows(missing_spy=True), "SPY reference coverage is incomplete"),
        (_price_rows(partial_aapl=True), "below 98%"),
    ],
)
def test_worker_price_export_rejects_duplicate_missing_spy_and_partial_member_coverage(
    tmp_path: Path, rows: list[tuple[object, ...]], expected: str,
) -> None:
    path = tmp_path / "prices.csv"
    _write_prices(path, rows)
    with pytest.raises(ValueError, match=expected):
        exporter._validate_prices(path, _membership(), pd.Timestamp("2024-01-02").date(), pd.Timestamp("2024-01-05").date())


def test_pit_price_reader_rejects_invalid_ohlc_rows() -> None:
    """Break caught: an apparently positive bar let high fall below close."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE price (trade_date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        connection.execute("INSERT INTO price VALUES ('2024-01-02', 'AAPL', 100, 99, 98, 101, 1)")
        bundle = SimpleNamespace(_connection=connection, data_cutoff=pd.Timestamp("2024-01-02"))
        with pytest.raises(ValueError, match="high does not contain"):
            PITDataBundle._query_prices(bundle, ("AAPL",), pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-02"))
    finally:
        connection.close()
