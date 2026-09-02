"""Build an immutable, provenance-bound point-in-time SQLite bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.pit_data import PITDataBundle, sha256_file
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_sha256,
)

_MEMBERSHIP_COLUMNS = ("effective_date", "ticker", "member")
_PRICE_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
_FUNDAMENTAL_COLUMNS = (
    "ticker", "statement_type", "period_end", "public_date", "basic_eps",
    "diluted_eps", "total_revenue", "net_income", "common_stock",
    "total_stockholders_equity", "shares_outstanding",
    "held_percent_institutions", "institution_count", "prev_institution_count",
)
_STATEMENT_TYPES = {"quarterly", "annual", "balance", "institutional"}
_TICKER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ.-")
_DIGEST_CHARS = frozenset("0123456789abcdef")
_PUBLIC_DATE_RULE = (
    "first supplied SPY trading day strictly after SEC acceptance calendar date; "
    "filed date fallback only"
)


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
    text = str(value).strip()
    if len(text) != 10:
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _digest(value: object, *, field: str) -> str:
    text = str(value)
    if len(text) != 64 or any(char not in _DIGEST_CHARS for char in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


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
        return int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _rows(path: Path, expected_columns: tuple[str, ...]) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != expected_columns:
            raise ValueError(f"{path.name} header must be exactly {expected_columns!r}")
        for row_number, row in enumerate(reader, start=2):
            if any(value is None for value in row.values()):
                raise ValueError(f"{path.name}:{row_number} has malformed columns")
            yield {column: str(row[column]) for column in expected_columns}


def _json_input(path: str | Path) -> tuple[Path, Mapping[str, object]]:
    resolved = _regular_input(path)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid provenance JSON: {resolved}") from exc
    if not isinstance(value, dict):
        raise ValueError("provenance JSON must contain an object")
    return resolved, value


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
    active: set[str] = set()
    for _, ticker, member in result:
        if bool(member) == (ticker in active):
            raise ValueError(f"membership transition is not a state change: {ticker}")
        active.add(ticker) if member else active.remove(ticker)
    return result


def _load_prices(path: Path, cutoff: str) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str]] = set()
    for row in _rows(path, _PRICE_COLUMNS):
        trade_date = _iso_date(row["trade_date"], field="trade_date")
        if trade_date > cutoff:
            raise ValueError("price row is after data_cutoff")
        ticker = _ticker(row["ticker"])
        if (trade_date, ticker) in seen:
            raise ValueError("duplicate price bar")
        seen.add((trade_date, ticker))
        values = {field: _float(row[field], field=field, allow_blank=False)
                  for field in ("open", "high", "low", "close", "volume")}
        if any(values[field] is None or values[field] <= 0
               for field in ("open", "high", "low", "close")):
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
    seen: set[tuple[str, str, str, str]] = set()
    for row in _rows(path, _FUNDAMENTAL_COLUMNS):
        ticker = _ticker(row["ticker"])
        statement_type = row["statement_type"].strip().lower()
        if statement_type not in _STATEMENT_TYPES:
            raise ValueError("fundamental statement_type is invalid")
        period_end = _iso_date(row["period_end"], field="period_end")
        public_date = _iso_date(row["public_date"], field="public_date")
        if public_date <= period_end or public_date > cutoff:
            raise ValueError("fundamental public_date must be after period_end and no later than data_cutoff")
        key = (ticker, statement_type, period_end, public_date)
        if key in seen:
            raise ValueError("duplicate visible fundamental snapshot")
        seen.add(key)
        numeric_fields = [_float(row[field], field=field) for field in _FUNDAMENTAL_COLUMNS[4:12]]
        institution_count = _int(row["institution_count"], field="institution_count")
        prev_count = _int(row["prev_institution_count"], field="prev_institution_count")
        if institution_count is not None and institution_count < 0:
            raise ValueError("institution_count must be nonnegative")
        if prev_count is not None and prev_count < 0:
            raise ValueError("prev_institution_count must be nonnegative")
        if not any(value is not None for value in (*numeric_fields, institution_count, prev_count)):
            raise ValueError("fundamental snapshot has no metrics")
        result.append((ticker, statement_type, period_end, public_date, *numeric_fields, institution_count, prev_count))
    if not result:
        raise ValueError("fundamentals export is empty")
    return sorted(result, key=lambda row: (row[0], row[1], row[3], row[2]))


def _provenance_metadata(
    *, membership_path: Path, prices_path: Path, fundamentals_path: Path,
    cutoff: str, evaluation_start: str, warmup_start: str,
    membership_provenance_path: Path, membership_provenance: Mapping[str, object],
    prices_provenance_path: Path, prices_provenance: Mapping[str, object],
    fundamentals_provenance_path: Path, fundamentals_provenance: Mapping[str, object],
) -> tuple[dict[str, str], set[str]]:
    membership_sha = sha256_file(membership_path)
    prices_sha = sha256_file(prices_path)
    fundamentals_sha = sha256_file(fundamentals_path)
    if membership_provenance.get("membership_sha256") != membership_sha:
        raise ValueError("membership provenance does not bind the membership CSV")
    if prices_provenance.get("prices_sha256") != prices_sha:
        raise ValueError("prices provenance does not bind the prices CSV")
    if prices_provenance.get("membership_sha256") != membership_sha:
        raise ValueError("prices provenance does not bind the membership CSV")
    if fundamentals_provenance.get("fundamentals_sha256") != fundamentals_sha:
        raise ValueError("fundamentals provenance does not bind the fundamentals CSV")
    if fundamentals_provenance.get("membership_csv_sha256") != membership_sha:
        raise ValueError("fundamentals provenance does not bind the membership CSV")
    membership_names_sha = _digest(
        membership_provenance.get("security_names_sha256"),
        field="security_names_sha256",
    )
    if fundamentals_provenance.get("security_names_csv_sha256") != membership_names_sha:
        raise ValueError("fundamentals provenance does not bind reviewed security names")
    spy_days_sha = _digest(
        prices_provenance.get("spy_trading_days_sha256"),
        field="spy_trading_days_sha256",
    )
    if fundamentals_provenance.get("spy_trading_days_csv_sha256") != spy_days_sha:
        raise ValueError("fundamentals provenance does not bind the price SPY calendar")
    if fundamentals_provenance.get("start_date") != warmup_start:
        raise ValueError("fundamentals provenance start_date does not match warmup_start")
    if fundamentals_provenance.get("end_date") != cutoff:
        raise ValueError("fundamentals provenance end_date does not match data_cutoff")
    if fundamentals_provenance.get("public_date_rule") != _PUBLIC_DATE_RULE:
        raise ValueError("fundamentals provenance public-date rule is invalid")
    if prices_provenance.get("start_date") != warmup_start:
        raise ValueError("prices provenance start_date does not match warmup_start")
    if prices_provenance.get("end_date") != cutoff:
        raise ValueError("prices provenance end_date does not match data_cutoff")
    if membership_provenance.get("first_effective_date") != evaluation_start:
        raise ValueError("membership provenance does not seed evaluation_start")

    contracts = prices_provenance.get("price_identity_request_contracts")
    if not isinstance(contracts, dict) or not contracts:
        raise ValueError("prices provenance has no price identity request contracts")
    contract_sha = pit_canonical_json_sha256(contracts)
    if prices_provenance.get("price_identity_request_contracts_sha256") != contract_sha:
        raise ValueError("prices provenance identity contract digest is invalid")
    reference_values = list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    if (
        prices_provenance.get("non_tradable_reference_symbols_json")
        != pit_canonical_json(reference_values)
        or prices_provenance.get("non_tradable_reference_symbols_sha256")
        != pit_canonical_json_sha256(reference_values)
    ):
        raise ValueError("prices provenance reference-symbol contract is invalid")
    raw_exclusions = prices_provenance.get("symbols_with_no_prices")
    if not isinstance(raw_exclusions, list):
        raise ValueError("prices provenance price exclusions must be a list")
    exclusions = {_ticker(value) for value in raw_exclusions}
    if len(exclusions) != len(raw_exclusions):
        raise ValueError("prices provenance has duplicate price exclusions")

    def required_text(source: Mapping[str, object], key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"provenance field is required: {key}")
        return value.strip()

    metadata = {
        "membership_source_sha256": membership_sha,
        "prices_source_sha256": prices_sha,
        "fundamentals_source_sha256": fundamentals_sha,
        "membership_provenance_sha256": sha256_file(membership_provenance_path),
        "prices_provenance_sha256": sha256_file(prices_provenance_path),
        "fundamentals_provenance_sha256": sha256_file(fundamentals_provenance_path),
        "membership_source_kind": "wikipedia_sp500_revision",
        "membership_revision_id": required_text(membership_provenance, "revision_id"),
        "membership_raw_sha256": _digest(membership_provenance.get("raw_sha256"), field="raw_sha256"),
        "membership_symbol_map_sha256": _digest(membership_provenance.get("symbol_map_sha256"), field="symbol_map_sha256"),
        "membership_security_names_sha256": membership_names_sha,
        "prices_source_kind": required_text(prices_provenance, "source_kind"),
        "prices_upstream_source_sha256": _digest(prices_provenance.get("source_sha256"), field="source_sha256"),
        "price_identity_map_sha256": _digest(prices_provenance.get("price_identity_map_sha256"), field="price_identity_map_sha256"),
        "price_identity_request_contracts_sha256": contract_sha,
        "spy_trading_days_sha256": spy_days_sha,
        "price_exclusion_count": str(len(exclusions)),
        "price_exclusions_sha256": pit_canonical_json_sha256(sorted(exclusions)),
        "fundamentals_source_kind": required_text(fundamentals_provenance, "source"),
        "non_tradable_reference_symbols_json": pit_canonical_json(
            list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
        ),
        "non_tradable_reference_symbols_sha256": pit_canonical_json_sha256(
            list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
        ),
        "source_universe": "sp500",
    }
    if metadata["fundamentals_source_kind"] != "SEC EDGAR official bulk archives":
        raise ValueError("fundamentals provenance source is not the approved SEC bulk archive source")
    for key in ("submissions_archive_sha256", "companyfacts_archive_sha256", "identity_manifest_csv_sha256"):
        metadata[f"fundamentals_{key}"] = _digest(fundamentals_provenance.get(key), field=key)
    return metadata, exclusions


def _integrity_gate(
    *, cutoff: str, evaluation_start: str, warmup_start: str,
    membership: list[tuple[str, str, int]], prices: list[tuple[Any, ...]],
    fundamentals: list[tuple[Any, ...]], price_exclusions: set[str],
    membership_provenance: Mapping[str, object], prices_provenance: Mapping[str, object],
) -> None:
    cutoff_date = date.fromisoformat(cutoff)
    eval_date = date.fromisoformat(evaluation_start)
    warmup_date = date.fromisoformat(warmup_start)
    if not warmup_date < eval_date <= cutoff_date:
        raise ValueError("date contract must satisfy warmup_start < evaluation_start <= data_cutoff")
    if cutoff != "2025-12-31":
        raise ValueError("five-year baseline data_cutoff must be 2025-12-31")
    membership_symbols = {row[1] for row in membership}
    price_symbols = {row[1] for row in prices}
    fundamental_symbols = {row[0] for row in fundamentals}
    reference_symbols = set(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    if price_exclusions:
        raise ValueError("price exclusions are not permitted in schema-v2 bundles")
    outside = fundamental_symbols.difference(membership_symbols)
    if outside:
        raise ValueError(f"fundamental tickers are outside membership: {sorted(outside)}")
    if reference_symbols.intersection(membership_symbols):
        raise ValueError("market references must not exist in membership")
    if reference_symbols.intersection(fundamental_symbols):
        raise ValueError("market references must not exist in fundamentals")
    required_price_symbols = membership_symbols.union(reference_symbols)
    if price_symbols != required_price_symbols:
        raise ValueError(
            "price symbols must exactly equal membership plus references; "
            f"missing={sorted(required_price_symbols - price_symbols)}, "
            f"outside={sorted(price_symbols - required_price_symbols)}"
        )
    raw_contracts = prices_provenance.get("price_identity_request_contracts")
    if not isinstance(raw_contracts, dict) or {
        _ticker(value) for value in raw_contracts
    } != membership_symbols.union(reference_symbols):
        raise ValueError(
            "price identity request contracts do not exactly cover membership plus references"
        )
    first_price = date.fromisoformat(min(row[0] for row in prices))
    if first_price < warmup_date or first_price > date(2020, 1, 2):
        raise ValueError("earliest price must be within warmup start through 2020-01-02")
    if date.fromisoformat(min(row[0] for row in membership)) > eval_date:
        raise ValueError("membership history does not seed the evaluation start")
    active: set[str] = set()
    events: dict[str, list[tuple[str, int]]] = {}
    for effective, ticker, member in membership:
        events.setdefault(effective, []).append((ticker, member))
    reference_days = {
        reference: sorted({row[0] for row in prices if row[1] == reference})
        for reference in PIT_NON_TRADABLE_REFERENCE_SYMBOLS
    }
    if any(not days for days in reference_days.values()):
        raise ValueError("market-reference price calendars must be non-empty")
    if len({tuple(days) for days in reference_days.values()}) != 1:
        raise ValueError("IWM, QQQ, and SPY trading-session calendars must be identical")
    all_spy_days = reference_days["SPY"]
    if all_spy_days[0] != "2020-01-02" or all_spy_days[-1] != cutoff:
        raise ValueError("market-reference calendar does not cover the exact five-year baseline")
    spy_payload = ("trade_date\n" + "\n".join(all_spy_days) + "\n").encode()
    spy_digest = hashlib.sha256(spy_payload).hexdigest()
    if prices_provenance.get("spy_trading_days_sha256") != spy_digest:
        raise ValueError("recomputed SPY calendar digest does not match prices provenance")
    if prices_provenance.get("spy_first_date") != all_spy_days[0] or prices_provenance.get("spy_last_date") != all_spy_days[-1]:
        raise ValueError("prices provenance SPY date range is inconsistent")
    expected_reference_coverage = {
        reference: {
            "first_date": days[0],
            "last_date": days[-1],
            "session_count": len(days),
        }
        for reference, days in reference_days.items()
    }
    if (
        prices_provenance.get("reference_symbol_coverage")
        != expected_reference_coverage
    ):
        raise ValueError("prices provenance market-reference coverage is inconsistent")
    if _int(prices_provenance.get("price_row_count"), field="price_row_count", allow_blank=False) != len(prices):
        raise ValueError("prices provenance row count is inconsistent")
    if _int(membership_provenance.get("event_count"), field="event_count", allow_blank=False) != len(membership):
        raise ValueError("membership provenance event count is inconsistent")
    if _int(membership_provenance.get("symbol_count"), field="symbol_count", allow_blank=False) != len(membership_symbols):
        raise ValueError("membership provenance symbol count is inconsistent")
    if membership_provenance.get("last_effective_date") != max(row[0] for row in membership):
        raise ValueError("membership provenance last effective date is inconsistent")
    spy_days = [day for day in all_spy_days if day >= evaluation_start]
    prices_by_day: dict[str, set[str]] = {}
    for trade_date, ticker, *_ in prices:
        prices_by_day.setdefault(trade_date, set()).add(ticker)
    event_days = sorted(events)
    event_index = 0
    member_pairs = 0
    covered_pairs = 0
    for spy_day in spy_days:
        while event_index < len(event_days) and event_days[event_index] <= spy_day:
            for ticker, member in events[event_days[event_index]]:
                active.add(ticker) if member else active.remove(ticker)
            event_index += 1
        if not 495 <= len(active) <= 510:
            raise ValueError(f"membership count is outside 495 through 510 on {spy_day}: {len(active)}")
        member_pairs += len(active)
        covered_pairs += len(active.intersection(prices_by_day.get(spy_day, set())))
    gaps = member_pairs - covered_pairs
    expected_coverage = round(100.0 * covered_pairs / member_pairs, 8)
    if _int(prices_provenance.get("member_trading_day_pairs"), field="member_trading_day_pairs", allow_blank=False) != member_pairs:
        raise ValueError("prices provenance member/trading-day pair count is inconsistent")
    if _int(prices_provenance.get("covered_member_trading_day_pairs"), field="covered_member_trading_day_pairs", allow_blank=False) != covered_pairs:
        raise ValueError("prices provenance covered pair count is inconsistent")
    if _int(prices_provenance.get("remaining_member_pair_gap_count"), field="remaining_member_pair_gap_count", allow_blank=False) != gaps:
        raise ValueError("prices provenance gap count is inconsistent")
    coverage = _float(prices_provenance.get("coverage_pct"), field="coverage_pct", allow_blank=False)
    if coverage is None or round(coverage, 8) != expected_coverage:
        raise ValueError("prices provenance coverage percentage is inconsistent")
    if coverage < 98.0:
        raise ValueError("price membership/trading-day coverage is below 98 percent")


def _create_bundle(output: Path, *, metadata: Mapping[str, str], membership: list[tuple[str, str, int]],
                   prices: list[tuple[Any, ...]], fundamentals: list[tuple[Any, ...]]) -> None:
    if output.exists() or output.is_symlink():
        raise ValueError(f"refusing to overwrite existing output: {output}")
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
        connection.executemany("INSERT INTO dataset_metadata(key,value) VALUES (?,?)", sorted(metadata.items()))
        connection.executemany("INSERT INTO membership VALUES (?,?,?)", membership)
        connection.executemany("INSERT INTO price VALUES (?,?,?,?,?,?,?)", prices)
        connection.executemany("INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fundamentals)
        connection.execute(
            "CREATE INDEX fundamentals_ticker_public_date_period_end_idx "
            "ON fundamentals(ticker, public_date, period_end)"
        )
        connection.commit()
    finally:
        connection.close()


def _publish_no_clobber(staged: list[tuple[Path, Path]]) -> None:
    published: list[tuple[Path, Path]] = []
    try:
        for temporary, target in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(temporary, target)
            published.append((temporary, target))
    except FileExistsError as exc:
        for temporary, target in reversed(published):
            try:
                if target.exists() and os.path.samefile(temporary, target):
                    target.unlink()
            except OSError:
                pass
        raise ValueError(f"refusing to overwrite existing output: {exc.filename}") from exc
    except Exception:
        for temporary, target in reversed(published):
            try:
                if target.exists() and os.path.samefile(temporary, target):
                    target.unlink()
            except OSError:
                pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a strict point-in-time CANSLIM SQLite bundle")
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--prices-csv", required=True)
    parser.add_argument("--fundamentals-csv", required=True)
    parser.add_argument("--data-cutoff", required=True, help="inclusive YYYY-MM-DD cutoff")
    parser.add_argument("--evaluation-start", required=True)
    parser.add_argument("--warmup-start", required=True)
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--fundamentals-provenance", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest-output", required=True, help="required manifest-last commit marker")
    args = parser.parse_args()

    cutoff = _iso_date(args.data_cutoff, field="data_cutoff")
    evaluation_start = _iso_date(args.evaluation_start, field="evaluation_start")
    warmup_start = _iso_date(args.warmup_start, field="warmup_start")
    membership_path = _regular_input(args.membership_csv)
    prices_path = _regular_input(args.prices_csv)
    fundamentals_path = _regular_input(args.fundamentals_csv)
    membership_provenance_path, membership_provenance = _json_input(args.membership_provenance)
    prices_provenance_path, prices_provenance = _json_input(args.prices_provenance)
    fundamentals_provenance_path, fundamentals_provenance = _json_input(args.fundamentals_provenance)
    inputs = {membership_path, prices_path, fundamentals_path, membership_provenance_path,
              prices_provenance_path, fundamentals_provenance_path}
    output = Path(args.output).resolve()
    manifest_path = Path(args.manifest_output).resolve()
    if output in inputs or manifest_path in inputs or manifest_path == output:
        raise ValueError("outputs must differ from each other and all inputs")
    for target in (output, manifest_path):
        if target.exists() or target.is_symlink():
            raise ValueError(f"refusing to overwrite existing output: {target}")

    before_hashes = {path: sha256_file(path) for path in inputs}
    membership = _load_membership(membership_path, cutoff)
    prices = _load_prices(prices_path, cutoff)
    fundamentals = _load_fundamentals(fundamentals_path, cutoff)
    provenance_metadata, price_exclusions = _provenance_metadata(
        membership_path=membership_path, prices_path=prices_path,
        fundamentals_path=fundamentals_path,
        cutoff=cutoff, evaluation_start=evaluation_start, warmup_start=warmup_start,
        membership_provenance_path=membership_provenance_path,
        membership_provenance=membership_provenance,
        prices_provenance_path=prices_provenance_path,
        prices_provenance=prices_provenance,
        fundamentals_provenance_path=fundamentals_provenance_path,
        fundamentals_provenance=fundamentals_provenance,
    )
    _integrity_gate(cutoff=cutoff, evaluation_start=evaluation_start, warmup_start=warmup_start,
                    membership=membership, prices=prices, fundamentals=fundamentals,
                    price_exclusions=price_exclusions,
                    membership_provenance=membership_provenance,
                    prices_provenance=prices_provenance)
    if before_hashes != {path: sha256_file(path) for path in inputs}:
        raise ValueError("an input changed while the bundle was being built")

    metadata = {
        "bundle_kind": "canslim_pit_v2", "schema_version": "2", "data_cutoff": cutoff,
        "evaluation_start": evaluation_start, "warmup_start": warmup_start,
        **provenance_metadata,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    temp_paths: list[Path] = []
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temp_output = Path(temp_name)
        temp_paths.append(temp_output)
        temp_output.unlink()
        _create_bundle(temp_output, metadata=metadata, membership=membership, prices=prices, fundamentals=fundamentals)
        digest = sha256_file(temp_output)
        with PITDataBundle(temp_output, expected_sha256=digest) as bundle:
            bundle.load_price_identity_transition_contract(prices_provenance_path)
            manifest = bundle.manifest()
            manifest["symbols"] = manifest.pop("symbol_count")
        staged.append((temp_output, output))
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", suffix=".tmp", dir=manifest_path.parent)
        os.close(descriptor)
        temp_manifest = Path(temp_name)
        temp_paths.append(temp_manifest)
        temp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged.append((temp_manifest, manifest_path))
        if before_hashes != {path: sha256_file(path) for path in inputs}:
            raise ValueError("an input changed before bundle publication")
        _publish_no_clobber(staged)
    finally:
        for temporary in temp_paths:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
