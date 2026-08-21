"""Build a validated point-in-time SQLite bundle from immutable CSV exports.

The builder is intentionally offline.  Historical membership, price, and
fundamental exports must be acquired and reviewed separately; this command
normalizes them into the exact bundle consumed by ``PITDataBundle`` and emits
the resulting digest/row counts as a manifest.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from core.pit_data import PITDataBundle, sha256_file

_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_PRICE_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
_FUNDAMENTAL_COLUMNS = (
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
_STATEMENT_TYPES = {"quarterly", "annual", "balance", "institutional"}
_TICKER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ.-")


def _regular_input(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"input must be a regular non-link file: {value}")
    return value.resolve()


def _ticker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be text")
    normalized = value.strip().upper()
    if not normalized or len(normalized) > 8 or any(char not in _TICKER_CHARS for char in normalized):
        raise ValueError(f"invalid ticker: {value!r}")
    return normalized


def _iso_date(value: object, *, field: str) -> str:
    try:
        parsed = date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    return parsed.isoformat()


def _float(value: object, *, field: str, allow_blank: bool = True) -> float | None:
    text = str(value).strip()
    if not text and allow_blank:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _int(value: object, *, field: str, allow_blank: bool = True) -> int | None:
    text = str(value).strip()
    if not text and allow_blank:
        return None
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    return number


def _rows(path: Path, expected_columns: tuple[str, ...]) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != expected_columns:
            raise ValueError(f"{path.name} header must be exactly {expected_columns!r}")
        for row_number, row in enumerate(reader, start=2):
            if any(value is None for value in row.values()):
                raise ValueError(f"{path.name}:{row_number} has malformed columns")
            yield {column: str(row[column]) for column in expected_columns}


def _load_membership(path: Path, cutoff: str) -> list[tuple[str, str, int]]:
    result: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for row in _rows(path, _MEMBERSHIP_COLUMNS):
        effective = _iso_date(row["effective_date"], field="effective_date")
        if effective > cutoff:
            raise ValueError("membership event is after data_cutoff")
        ticker = _ticker(row["ticker"])
        member = _int(row["member"], field="member", allow_blank=False)
        if member not in {0, 1}:
            raise ValueError("membership member must be 0 or 1")
        key = (effective, ticker)
        if key in seen:
            raise ValueError("duplicate membership transition")
        seen.add(key)
        result.append((effective, ticker, member))
    if not result:
        raise ValueError("membership export is empty")
    result.sort()
    return result


def _load_prices(path: Path, cutoff: str) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    for row in _rows(path, _PRICE_COLUMNS):
        trade_date = _iso_date(row["trade_date"], field="trade_date")
        if trade_date > cutoff:
            raise ValueError("price row is after data_cutoff")
        ticker = _ticker(row["ticker"])
        key = (trade_date, ticker)
        if key in seen:
            raise ValueError("duplicate price bar")
        seen.add(key)
        values = {
            field: _float(row[field], field=field, allow_blank=False)
            for field in ("open", "high", "low", "close", "volume")
        }
        if any(values[field] is None or values[field] <= 0 for field in ("open", "high", "low", "close")):
            raise ValueError("price OHLC values must be positive")
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
            raise ValueError("price high/low does not contain open/close")
        if values["volume"] is None or values["volume"] < 0:
            raise ValueError("price volume must be nonnegative")
        result.append((trade_date, ticker, *(values[field] for field in ("open", "high", "low", "close", "volume"))))
    if not result:
        raise ValueError("price export is empty")
    return sorted(result)


def _load_fundamentals(path: Path, cutoff: str) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    for row in _rows(path, _FUNDAMENTAL_COLUMNS):
        ticker = _ticker(row["ticker"])
        statement_type = row["statement_type"].strip().lower()
        if statement_type not in _STATEMENT_TYPES:
            raise ValueError("fundamental statement_type is invalid")
        period_end = _iso_date(row["period_end"], field="period_end")
        public_date = _iso_date(row["public_date"], field="public_date")
        if public_date < period_end or public_date > cutoff:
            raise ValueError("fundamental public_date must be between period_end and data_cutoff")
        numeric_fields = [
            _float(row[field], field=field)
            for field in _FUNDAMENTAL_COLUMNS[4:12]
        ]
        institution_count = _int(row["institution_count"], field="institution_count")
        prev_count = _int(row["prev_institution_count"], field="prev_institution_count")
        if institution_count is not None and institution_count < 0:
            raise ValueError("institution_count must be nonnegative")
        if prev_count is not None and prev_count < 0:
            raise ValueError("prev_institution_count must be nonnegative")
        result.append((ticker, statement_type, period_end, public_date, *numeric_fields, institution_count, prev_count))
    if not result:
        raise ValueError("fundamentals export is empty")
    return sorted(result, key=lambda row: (row[0], row[1], row[3], row[2]))


def _create_bundle(
    output: Path,
    *,
    cutoff: str,
    membership: list[tuple[str, str, int]],
    prices: list[tuple[Any, ...]],
    fundamentals: list[tuple[Any, ...]],
    source_hashes: dict[str, str],
) -> None:
    if output.exists() or output.is_symlink():
        raise ValueError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output)
    try:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE membership (effective_date TEXT NOT NULL, ticker TEXT NOT NULL, member INTEGER NOT NULL);
            CREATE TABLE price (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL);
            CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, total_stockholders_equity REAL, shares_outstanding REAL, held_percent_institutions REAL, institution_count INTEGER, prev_institution_count INTEGER);
            """
        )
        metadata = {
            "bundle_kind": "canslim_pit_v1",
            "schema_version": "1",
            "data_cutoff": cutoff,
            **{f"{key}_sha256": value for key, value in source_hashes.items()},
        }
        connection.executemany("INSERT INTO dataset_metadata(key,value) VALUES (?,?)", sorted(metadata.items()))
        connection.executemany("INSERT INTO membership VALUES (?,?,?)", membership)
        connection.executemany("INSERT INTO price VALUES (?,?,?,?,?,?,?)", prices)
        connection.executemany("INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fundamentals)
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a strict point-in-time CANSLIM SQLite bundle")
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--prices-csv", required=True)
    parser.add_argument("--fundamentals-csv", required=True)
    parser.add_argument("--data-cutoff", required=True, help="inclusive YYYY-MM-DD cutoff")
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest-output", default=None)
    args = parser.parse_args()

    cutoff = _iso_date(args.data_cutoff, field="data_cutoff")
    membership_path = _regular_input(args.membership_csv)
    prices_path = _regular_input(args.prices_csv)
    fundamentals_path = _regular_input(args.fundamentals_csv)
    output = Path(args.output).resolve()
    if output in {membership_path, prices_path, fundamentals_path}:
        raise ValueError("output must differ from all input files")
    if output.exists() or output.is_symlink():
        raise ValueError(f"refusing to overwrite existing output: {output}")
    temporary_output = output.with_name(output.name + ".tmp")
    if temporary_output.exists() or temporary_output.is_symlink():
        raise ValueError(f"temporary output already exists: {temporary_output}")

    membership = _load_membership(membership_path, cutoff)
    prices = _load_prices(prices_path, cutoff)
    fundamentals = _load_fundamentals(fundamentals_path, cutoff)
    try:
        _create_bundle(
            temporary_output,
            cutoff=cutoff,
            membership=membership,
            prices=prices,
            fundamentals=fundamentals,
            source_hashes={
                "membership_source": sha256_file(membership_path),
                "prices_source": sha256_file(prices_path),
                "fundamentals_source": sha256_file(fundamentals_path),
            },
        )
        digest = sha256_file(temporary_output)
        with PITDataBundle(temporary_output, expected_sha256=digest) as bundle:
            manifest = bundle.manifest()
            manifest["symbols"] = manifest.pop("symbol_count")
            for source_key in ("membership", "prices", "fundamentals"):
                manifest[f"{source_key}_source_sha256"] = bundle.metadata[
                    f"{source_key}_source_sha256"
                ]
        temporary_output.replace(output)
    finally:
        if temporary_output.exists() or temporary_output.is_symlink():
            temporary_output.unlink()
    if args.manifest_output:
        manifest_path = Path(args.manifest_output).resolve()
        if manifest_path.exists() or manifest_path.is_symlink():
            raise ValueError(f"refusing to overwrite existing manifest: {manifest_path}")
        temporary_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
        if temporary_manifest.exists() or temporary_manifest.is_symlink():
            raise ValueError(f"temporary manifest already exists: {temporary_manifest}")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary_manifest.replace(manifest_path)
        finally:
            if temporary_manifest.exists() or temporary_manifest.is_symlink():
                temporary_manifest.unlink()
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
