"""Convert approved DataFetcher pickle payloads into plain OHLCV CSV.

This program is intentionally run only inside the disposable, network-disabled
container created by ``export_pit_prices.py``.  It is the sole process in the
export path that imports ``pickle`` or deserializes cache payloads.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pandas as pd

_REQUEST_KEYS = frozenset({"end_date", "start_date", "tickers", "version"})
_OUTPUT_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.-]{0,14}")


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be an absolute regular non-link file")
    return path


def _load_request(path: Path) -> tuple[date, date, tuple[str, ...]]:
    raw = _regular_file(path, "request").read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request must be UTF-8 JSON") from exc
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if raw != canonical or not isinstance(value, dict) or set(value) != _REQUEST_KEYS:
        raise ValueError("request must use the exact canonical JSON contract")
    if value["version"] != 1:
        raise ValueError("request version is unsupported")
    try:
        start = date.fromisoformat(value["start_date"])
        end = date.fromisoformat(value["end_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("request dates must be ISO calendar dates") from exc
    raw_tickers = value["tickers"]
    if (
        start > end
        or not isinstance(raw_tickers, list)
        or not raw_tickers
        or raw_tickers != sorted(set(raw_tickers))
        or "SPY" not in raw_tickers
        or any(not isinstance(symbol, str) or _TICKER_RE.fullmatch(symbol) is None for symbol in raw_tickers)
    ):
        raise ValueError("request ticker/date contract is invalid")
    return start, end, tuple(raw_tickers)


def _column_map(frame: pd.DataFrame, ticker: str) -> dict[str, object]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError(f"price payload for {ticker} is not a non-empty DataFrame")
    columns: dict[str, object] = {}
    for column in frame.columns:
        if not isinstance(column, str):
            raise ValueError(f"price payload for {ticker} has non-text columns")
        normalized = column.strip().casefold().replace("_", " ")
        if normalized in _PRICE_COLUMNS:
            if normalized in columns:
                raise ValueError(f"price payload for {ticker} has duplicate normalized columns")
            columns[normalized] = column
    if set(columns) != set(_PRICE_COLUMNS):
        raise ValueError(f"price payload for {ticker} lacks exact OHLCV fields")
    return columns


def _frame_rows(
    ticker: str,
    frame: pd.DataFrame,
    start: date,
    end: date,
) -> tuple[tuple[date, tuple[float, float, float, float, float]], ...]:
    columns = _column_map(frame, ticker)
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError(f"price payload for {ticker} has a non-monotonic or duplicate index")
    try:
        timestamps = pd.to_datetime(frame.index, errors="raise", utc=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"price payload for {ticker} has an invalid date index") from exc
    output: list[tuple[date, tuple[float, float, float, float, float]]] = []
    seen: set[date] = set()
    for position, timestamp in enumerate(timestamps):
        trade_date = timestamp.date()
        values: list[float] = []
        for name in _PRICE_COLUMNS:
            try:
                number = float(frame.iloc[position][columns[name]])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"price payload for {ticker} has nonnumeric {name}") from exc
            if not math.isfinite(number):
                raise ValueError(f"price payload for {ticker} has non-finite {name}")
            if (name == "volume" and number < 0) or (name != "volume" and number <= 0):
                raise ValueError(
                    f"price payload for {ticker} has an invalid {name} at {trade_date.isoformat()}"
                )
            values.append(number)
        if start <= trade_date <= end:
            if trade_date in seen:
                raise ValueError(f"price payload for {ticker} has duplicate calendar dates")
            seen.add(trade_date)
            output.append((trade_date, tuple(values)))
    return tuple(output)


def _format_number(value: float) -> str:
    if value == 0:
        return "0"
    return repr(value)


def export_prices(request_path: Path, cache_path: Path, output_path: Path) -> None:
    """Deserialize validated cache payloads and write the requested plain CSV."""
    start, end, tickers = _load_request(request_path)
    _regular_file(cache_path, "cache")
    if not output_path.is_absolute() or output_path.exists() or output_path.is_symlink():
        raise ValueError("output must be an absent absolute path")
    ticker_set = set(tickers)
    selected: dict[
        str,
        tuple[tuple[date, tuple[float, float, float, float, float]], ...],
    ] = {}
    uri = f"file:{quote(cache_path.as_posix(), safe='/:')}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        cache_keys = connection.execute(
            "SELECT cache_key FROM dataset_cache WHERE cache_kind = 'price' "
            "ORDER BY created_at DESC, cache_key"
        ).fetchall()
        for (cache_key,) in cache_keys:
            row = connection.execute(
                "SELECT payload FROM dataset_cache WHERE cache_key = ? AND cache_kind = 'price'",
                (cache_key,),
            ).fetchone()
            payload = row[0] if row is not None else None
            if not isinstance(cache_key, str) or not isinstance(payload, bytes):
                raise ValueError("price cache row has invalid storage types")
            try:
                value = pickle.loads(payload)
            except (pickle.UnpicklingError, AttributeError, EOFError, ImportError, IndexError) as exc:
                raise ValueError(f"price payload cannot be deserialized: {cache_key}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"price payload is not a ticker mapping: {cache_key}")
            for raw_ticker, frame in value.items():
                if not isinstance(raw_ticker, str):
                    raise ValueError(f"price payload has a non-text ticker: {cache_key}")
                ticker = raw_ticker.strip().upper()
                if ticker not in ticker_set:
                    continue
                try:
                    frame_rows = _frame_rows(ticker, frame, start, end)
                except ValueError:
                    # Overlapping cache entries are independent candidates.  An
                    # invalid candidate can never be selected or emitted.
                    continue
                current = selected.get(ticker)
                if current is None or len(frame_rows) > len(current):
                    selected[ticker] = frame_rows

    output_path.parent.mkdir(parents=False, exist_ok=True)
    with output_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(_OUTPUT_COLUMNS)
        records = (
            (trade_date, ticker, values)
            for ticker, frame_rows in selected.items()
            for trade_date, values in frame_rows
        )
        for trade_date, ticker, values in sorted(records):
            writer.writerow((trade_date.isoformat(), ticker, *(_format_number(value) for value in values)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Confined hash-pinned cache price converter")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_prices(args.request, args.cache, args.output)
    print('{"status":"complete","version":1}', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
