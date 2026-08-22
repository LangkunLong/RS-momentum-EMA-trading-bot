"""Acquire and normalize an explicit Alpaca SIP daily-bar snapshot.

This module deliberately imports only Alpaca's historical market-data client.
It has no trading, order, position, or account API dependency.
"""

from __future__ import annotations

import csv
import heapq
import math
import os
import stat
import tempfile
import time as time_module
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Iterator, Mapping, Sequence

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import dotenv_values

PRICE_COLUMNS = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
REQUEST_ALIASES = {"BF.B": "BF-B", "BRK.B": "BRK-B"}
MAX_SYMBOLS_PER_REQUEST = 100


@dataclass(frozen=True)
class AlpacaSnapshot:
    path: Path
    retrieved_at_utc: str
    requested_symbol_count: int
    requested_membership_symbol_count: int
    returned_symbol_count: int
    returned_membership_symbol_count: int
    chunk_count: int
    row_count: int
    adjustment: str
    identity_group_count: int


@dataclass(frozen=True)
class ProviderIdentity:
    """One canonical price identity and its reviewed provider request boundary."""

    canonical_symbol: str
    provider_symbol: str
    identity_asof: date
    admitted_start: date
    admitted_end: date
    lower_clip: bool


def load_alpaca_credentials(env_file: Path | None = None) -> tuple[str, str]:
    """Resolve the repository's existing Alpaca credential aliases without logging them."""
    values: Mapping[str, object]
    if env_file is None:
        values = os.environ
    else:
        absolute = Path(os.path.abspath(env_file))
        info = absolute.lstat()
        if not stat.S_ISREG(info.st_mode) or absolute.is_symlink() or absolute.resolve() != absolute:
            raise ValueError("Alpaca environment file must be a regular non-link file")
        values = dotenv_values(absolute)
    api_key = values.get("ALPACA_API_KEY")
    secret_key = values.get("ALPACA_SECRET_KEY")
    if not isinstance(api_key, str) or not api_key or not isinstance(secret_key, str) or not secret_key:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
    return api_key, secret_key


def _request_symbols(
    canonical_symbols: Sequence[str],
    provider_symbols: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    canonical = tuple(sorted(set(canonical_symbols)))
    if len(canonical) != len(canonical_symbols):
        raise ValueError("canonical provider symbols must be unique and sorted")
    canonical_to_request = (
        {
            canonical_symbol: provider_symbol
            for provider_symbol, canonical_symbol in REQUEST_ALIASES.items()
        }
        if provider_symbols is None
        else dict(provider_symbols)
    )
    if provider_symbols is not None and set(canonical_to_request) != set(canonical):
        raise ValueError("provider symbol mapping differs from canonical symbols")
    request_to_canonical: dict[str, str] = {}
    requested: list[str] = []
    for symbol in canonical:
        request_symbol = canonical_to_request.get(symbol, symbol)
        if not isinstance(request_symbol, str) or not request_symbol:
            raise ValueError("provider symbol mapping contains an invalid symbol")
        if request_symbol in request_to_canonical:
            raise ValueError("provider symbol aliases are ambiguous")
        request_to_canonical[request_symbol] = symbol
        requested.append(request_symbol)
    return tuple(requested), request_to_canonical


def _chunks(values: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(values[offset : offset + MAX_SYMBOLS_PER_REQUEST])
        for offset in range(0, len(values), MAX_SYMBOLS_PER_REQUEST)
    )


def _column_map(frame: pd.DataFrame) -> dict[str, object]:
    columns: dict[str, object] = {}
    for column in frame.columns:
        if not isinstance(column, str):
            raise ValueError("Alpaca SIP returned a non-text column")
        normalized = column.strip().casefold()
        if normalized in PRICE_COLUMNS[2:]:
            if normalized in columns:
                raise ValueError("Alpaca SIP returned duplicate normalized OHLCV columns")
            columns[normalized] = column
    if set(columns) != set(PRICE_COLUMNS[2:]):
        raise ValueError("Alpaca SIP response lacks exact OHLCV fields")
    return columns


def _response_rows(
    frame: pd.DataFrame,
    requested: frozenset[str],
    request_to_canonical: Mapping[str, str],
    start: date,
    end: date,
    expected_days: frozenset[date],
    identities: Mapping[str, ProviderIdentity],
) -> tuple[list[tuple[date, str, float, float, float, float, float]], set[str]]:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Alpaca SIP returned a non-tabular response")
    if frame.empty:
        return [], set()
    if not isinstance(frame.index, pd.MultiIndex) or frame.index.nlevels != 2:
        raise ValueError("Alpaca SIP response must use a symbol/timestamp index")
    if set(frame.index.names) != {"symbol", "timestamp"}:
        raise ValueError("Alpaca SIP response index differs from the expected contract")
    columns = _column_map(frame)
    symbols = frame.index.get_level_values("symbol")
    timestamps = frame.index.get_level_values("timestamp")
    values = frame[[columns[name] for name in PRICE_COLUMNS[2:]]].itertuples(index=False, name=None)
    previous_by_symbol: dict[str, pd.Timestamp] = {}
    seen: set[tuple[date, str]] = set()
    returned: set[str] = set()
    rows: list[tuple[date, str, float, float, float, float, float]] = []
    for raw_symbol, raw_timestamp, raw_values in zip(symbols, timestamps, values, strict=True):
        if not isinstance(raw_symbol, str):
            raise ValueError("Alpaca SIP returned a non-text symbol")
        request_symbol = raw_symbol.strip().upper()
        if request_symbol not in requested or request_symbol not in request_to_canonical:
            raise ValueError("Alpaca SIP returned a symbol outside its requested chunk")
        canonical = request_to_canonical[request_symbol]
        timestamp = pd.Timestamp(raw_timestamp)
        if timestamp.tzinfo is None:
            raise ValueError("Alpaca SIP returned a timezone-naive timestamp")
        timestamp = timestamp.tz_convert("UTC")
        previous = previous_by_symbol.get(request_symbol)
        if previous is not None and timestamp <= previous:
            raise ValueError("Alpaca SIP rows are not strictly monotonic per symbol")
        previous_by_symbol[request_symbol] = timestamp
        trade_date = timestamp.date()
        numbers: list[float] = []
        for raw_number in raw_values:
            try:
                number = float(raw_number)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Alpaca SIP returned a nonnumeric OHLCV value") from None
            if not math.isfinite(number):
                raise ValueError("Alpaca SIP returned a non-finite OHLCV value")
            numbers.append(number)
        open_price, high, low, close, volume = numbers
        if (
            not start <= trade_date <= end
            or trade_date not in expected_days
            or min(open_price, high, low, close) <= 0
            or volume < 0
            or low > min(open_price, close)
            or high < max(open_price, close)
            or low > high
        ):
            raise ValueError("Alpaca SIP row violates the requested daily OHLCV contract")
        identity_contract = identities.get(canonical)
        if identity_contract is None:
            raise ValueError("Alpaca SIP row lacks a canonical identity contract")
        if trade_date > identity_contract.admitted_end or (
            identity_contract.lower_clip and trade_date < identity_contract.admitted_start
        ):
            continue
        identity = (trade_date, canonical)
        if identity in seen:
            raise ValueError("Alpaca SIP returned a duplicate canonical ticker/date row")
        seen.add(identity)
        returned.add(canonical)
        rows.append((trade_date, canonical, open_price, high, low, close, volume))
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows, returned


def _format_number(value: float) -> str:
    return "0" if value == 0 else repr(value)


def _tail_rows(
    path: Path,
    symbols: Sequence[str],
    *,
    tail_size: int,
) -> dict[str, dict[date, tuple[float, float, float, float, float]]]:
    expected = frozenset(symbols)
    tails = {symbol: deque(maxlen=tail_size) for symbol in symbols}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != PRICE_COLUMNS:
            raise ValueError("Alpaca calibration snapshot has an unexpected header")
        for row in reader:
            ticker = row["ticker"]
            if ticker not in expected:
                continue
            try:
                trade_date = date.fromisoformat(row["trade_date"])
                values = tuple(float(row[name]) for name in PRICE_COLUMNS[2:])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Alpaca calibration snapshot contains a malformed row") from None
            tails[ticker].append((trade_date, values))
    return {ticker: dict(rows) for ticker, rows in tails.items()}


def _derive_cutoff_split_factors(
    split_path: Path,
    raw_path: Path,
    symbols: Sequence[str],
    *,
    tail_size: int = 20,
    minimum_matching_days: int = 5,
) -> dict[str, float]:
    """Prove stable RAW/SPLIT ratios and derive the cutoff-basis factor per symbol."""
    canonical = tuple(sorted(set(symbols)))
    if tuple(symbols) != canonical or tail_size < minimum_matching_days or minimum_matching_days < 2:
        raise ValueError("cutoff-factor symbol/tail contract is invalid")
    split_tails = _tail_rows(split_path, canonical, tail_size=tail_size)
    raw_tails = _tail_rows(raw_path, canonical, tail_size=tail_size)
    factors: dict[str, float] = {}
    for ticker in canonical:
        shared_dates = sorted(set(split_tails[ticker]).intersection(raw_tails[ticker]))[-tail_size:]
        if len(shared_dates) < minimum_matching_days:
            raise ValueError(f"cannot prove a cutoff adjustment factor for {ticker}")
        ratios_by_date: dict[date, tuple[tuple[float, ...], float | None]] = {}
        for trade_date in shared_dates:
            split_values = split_tails[ticker][trade_date]
            raw_values = raw_tails[ticker][trade_date]
            price_ratios = tuple(
                raw_value / split_value
                for raw_value, split_value in zip(raw_values[:4], split_values[:4], strict=True)
            )
            raw_volume = raw_values[4]
            split_volume = split_values[4]
            if raw_volume == 0 or split_volume == 0:
                if raw_volume != split_volume:
                    raise ValueError(f"cutoff volume factor is not provable for {ticker}")
                volume_ratio = None
            else:
                volume_ratio = raw_volume / split_volume
            ratios_by_date[trade_date] = (price_ratios, volume_ratio)
        observed_factor = median(ratios_by_date[shared_dates[-1]][0])
        rational_factor = float(Fraction(observed_factor).limit_denominator(20))
        if (
            not math.isfinite(observed_factor)
            or observed_factor <= 0
            or abs(observed_factor - rational_factor) / observed_factor > 0.005
        ):
            raise ValueError(f"cutoff split factor is unstable or not a credible split ratio for {ticker}")
        stable_suffix_count = 0
        for trade_date in reversed(shared_dates):
            price_ratios, volume_ratio = ratios_by_date[trade_date]
            if any(abs(ratio - rational_factor) / rational_factor > 0.01 for ratio in price_ratios):
                break
            if volume_ratio is not None and abs(volume_ratio - 1 / rational_factor) * rational_factor > 0.01:
                raise ValueError(f"cutoff split volume factor is unstable for {ticker}")
            stable_suffix_count += 1
        if stable_suffix_count < minimum_matching_days:
            raise ValueError(f"cutoff split factor lacks a stable suffix for {ticker}")
        factors[ticker] = rational_factor
    return factors


def _apply_cutoff_split_factors(
    split_path: Path,
    factors: Mapping[str, float],
    output_path: Path,
) -> None:
    """Normalize today's SPLIT bars back to the data-cutoff adjustment basis."""
    created = False
    try:
        with (
            split_path.open("r", encoding="utf-8", newline="") as source,
            output_path.open("x", encoding="utf-8", newline="") as output,
        ):
            created = True
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != PRICE_COLUMNS:
                raise ValueError("Alpaca SPLIT snapshot has an unexpected header")
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            observed: set[str] = set()
            for row in reader:
                ticker = row["ticker"]
                factor = factors.get(ticker)
                if factor is None or not math.isfinite(factor) or factor <= 0:
                    raise ValueError("Alpaca SPLIT snapshot lacks a proved cutoff factor")
                observed.add(ticker)
                values = tuple(float(row[name]) for name in PRICE_COLUMNS[2:])
                adjusted = (
                    *(value * factor for value in values[:4]),
                    values[4] / factor,
                )
                writer.writerow(
                    (row["trade_date"], ticker, *(_format_number(value) for value in adjusted))
                )
            if observed != set(factors):
                raise ValueError("cutoff-factor symbols differ from the SPLIT snapshot")
    except Exception:
        if created:
            output_path.unlink(missing_ok=True)
        raise


def _write_chunk(
    path: Path,
    rows: Iterable[tuple[date, str, float, float, float, float, float]],
) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(PRICE_COLUMNS)
        for trade_date, ticker, *numbers in rows:
            writer.writerow((trade_date.isoformat(), ticker, *(_format_number(value) for value in numbers)))


def _chunk_rows(stream: object) -> Iterator[list[str]]:
    reader = csv.reader(stream)  # type: ignore[arg-type]
    if tuple(next(reader, ())) != PRICE_COLUMNS:
        raise ValueError("normalized Alpaca chunk has an unexpected header")
    yield from reader


def _merge_chunks(chunk_paths: Sequence[Path], output_path: Path) -> int:
    row_count = 0
    previous: tuple[str, str] | None = None
    output_created = False
    try:
        with ExitStack() as stack, output_path.open("x", encoding="utf-8", newline="") as output:
            output_created = True
            inputs = [stack.enter_context(path.open("r", encoding="utf-8", newline="")) for path in chunk_paths]
            merged = heapq.merge(*(_chunk_rows(stream) for stream in inputs), key=lambda row: (row[0], row[1]))
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(PRICE_COLUMNS)
            for row in merged:
                identity = (row[0], row[1])
                if previous is not None and identity <= previous:
                    raise ValueError("normalized Alpaca snapshot has duplicate or unsorted rows")
                previous = identity
                writer.writerow(row)
                row_count += 1
    except Exception:
        if output_created:
            output_path.unlink(missing_ok=True)
        raise
    return row_count


def _provider_failure_category(exc: Exception) -> tuple[str, bool]:
    try:
        status_code = getattr(exc, "status_code", None)
    except Exception:
        status_code = None
    if isinstance(status_code, int):
        try:
            provider_code = getattr(exc, "code", None)
        except Exception:
            provider_code = None
        code_suffix = (
            f", provider code {provider_code}"
            if isinstance(provider_code, (int, str)) and str(provider_code).isalnum()
            else ""
        )
        return f"HTTP {status_code}{code_suffix}", status_code == 429 or status_code >= 500
    category = type(exc).__name__
    retryable = category.casefold() in {
        "connectionerror",
        "connecttimeout",
        "readtimeout",
        "remotedisconnected",
        "timeouterror",
    }
    return f"transport category {category}", retryable


def _fetch_alpaca_sip_snapshot(
    canonical_symbols: Sequence[str],
    *,
    membership_symbol_count: int,
    start: date,
    end: date,
    expected_trading_days: Sequence[date],
    output_path: Path,
    api_key: str,
    secret_key: str,
    adjustment: Adjustment,
    identities: Mapping[str, ProviderIdentity],
    client_factory: Callable[..., object] = StockHistoricalDataClient,
) -> AlpacaSnapshot:
    """Fetch exact SIP daily-bar chunks for one fixed adjustment mode."""
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("Alpaca snapshot output must not already exist")
    canonical = tuple(sorted(set(canonical_symbols)))
    if tuple(canonical_symbols) != canonical or "SPY" not in canonical:
        raise ValueError("provider symbols must be sorted, unique, and include SPY")
    if membership_symbol_count != len(canonical) - 1:
        raise ValueError("membership symbol count does not match the provider request")
    if set(identities) != set(canonical):
        raise ValueError("provider identity contracts differ from requested symbols")
    for symbol, identity in identities.items():
        if (
            identity.canonical_symbol != symbol
            or not start <= identity.admitted_start <= identity.admitted_end <= end
            or not identity.admitted_end <= identity.identity_asof <= end
        ):
            raise ValueError("provider identity contract is invalid")
    _, request_to_canonical = _request_symbols(
        canonical,
        {symbol: identities[symbol].provider_symbol for symbol in canonical},
    )
    requested_by_canonical = {
        canonical_symbol: request_symbol
        for request_symbol, canonical_symbol in request_to_canonical.items()
    }
    canonical_by_asof: dict[date, list[str]] = {}
    for symbol in canonical:
        canonical_by_asof.setdefault(identities[symbol].identity_asof, []).append(symbol)
    request_groups = tuple(
        (asof, chunk)
        for asof in sorted(canonical_by_asof)
        for chunk in _chunks(
            tuple(requested_by_canonical[symbol] for symbol in sorted(canonical_by_asof[asof]))
        )
    )
    expected_days = tuple(expected_trading_days)
    if not expected_days or tuple(sorted(set(expected_days))) != expected_days:
        raise ValueError("expected trading days must be non-empty, sorted, and unique")
    try:
        client = client_factory(api_key, secret_key)
    except Exception:
        raise RuntimeError("Alpaca SIP market-data client initialization failed") from None
    returned: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="alpaca-sip-chunks-") as temporary:
        root = Path(temporary)
        chunk_paths: list[Path] = []
        for chunk_number, (identity_asof, chunk) in enumerate(request_groups, start=1):
            request = StockBarsRequest(
                symbol_or_symbols=list(chunk),
                timeframe=TimeFrame.Day,
                start=datetime.combine(start, time.min, UTC),
                end=datetime.combine(end + timedelta(days=1), time.min, UTC),
                adjustment=adjustment,
                feed=DataFeed.SIP,
                asof=identity_asof.isoformat(),
            )
            frame: pd.DataFrame | None = None
            for attempt in range(1, 4):
                try:
                    response = client.get_stock_bars(request)  # type: ignore[attr-defined]
                    frame = response.df
                    break
                except Exception as exc:
                    category, retryable = _provider_failure_category(exc)
                    if not retryable or attempt == 3:
                        raise RuntimeError(
                            f"Alpaca SIP {adjustment.value} historical-bars request failed "
                            f"for chunk {chunk_number} "
                            f"({category})"
                        ) from None
                    time_module.sleep(2**attempt)
            if frame is None:
                raise RuntimeError(f"Alpaca SIP historical-bars request failed for chunk {chunk_number}")
            rows, chunk_returned = _response_rows(
                frame,
                frozenset(chunk),
                request_to_canonical,
                start,
                end,
                frozenset(expected_days),
                identities,
            )
            if returned.intersection(chunk_returned):
                raise ValueError("Alpaca SIP alias results are ambiguous across chunks")
            returned.update(chunk_returned)
            chunk_path = root / f"chunk-{chunk_number:02d}.csv"
            _write_chunk(chunk_path, rows)
            chunk_paths.append(chunk_path)
        if returned != set(canonical):
            missing = sorted(set(canonical) - returned)
            raise ValueError(f"Alpaca SIP returned no valid rows for requested symbol {missing[0]}")
        row_count = _merge_chunks(chunk_paths, output_path)
    spy_dates: list[date] = []
    with output_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            if row["ticker"] == "SPY":
                spy_dates.append(date.fromisoformat(row["trade_date"]))
    if tuple(spy_dates) != expected_days:
        output_path.unlink(missing_ok=True)
        raise ValueError("Alpaca SIP does not provide complete SPY coverage for the required calendar")
    return AlpacaSnapshot(
        path=output_path,
        retrieved_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        requested_symbol_count=len(canonical),
        requested_membership_symbol_count=membership_symbol_count,
        returned_symbol_count=len(returned),
        returned_membership_symbol_count=len(returned - {"SPY"}),
        chunk_count=len(request_groups),
        row_count=row_count,
        adjustment=adjustment.value,
        identity_group_count=len(canonical_by_asof),
    )


def fetch_alpaca_sip_snapshot(
    canonical_symbols: Sequence[str],
    *,
    membership_symbol_count: int,
    start: date,
    end: date,
    expected_trading_days: Sequence[date],
    output_path: Path,
    api_key: str,
    secret_key: str,
    identities: Mapping[str, ProviderIdentity],
    client_factory: Callable[..., object] = StockHistoricalDataClient,
) -> AlpacaSnapshot:
    """Fetch the mandatory primary Alpaca SIP/SPLIT/Day snapshot."""
    return _fetch_alpaca_sip_snapshot(
        canonical_symbols,
        membership_symbol_count=membership_symbol_count,
        start=start,
        end=end,
        expected_trading_days=expected_trading_days,
        output_path=output_path,
        api_key=api_key,
        secret_key=secret_key,
        adjustment=Adjustment.SPLIT,
        identities=identities,
        client_factory=client_factory,
    )


def fetch_alpaca_sip_raw_calibration(
    canonical_symbols: Sequence[str],
    *,
    membership_symbol_count: int,
    start: date,
    end: date,
    expected_trading_days: Sequence[date],
    output_path: Path,
    api_key: str,
    secret_key: str,
    identities: Mapping[str, ProviderIdentity],
    client_factory: Callable[..., object] = StockHistoricalDataClient,
) -> AlpacaSnapshot:
    """Fetch the explicit SIP/RAW calibration snapshot for cutoff factors."""
    return _fetch_alpaca_sip_snapshot(
        canonical_symbols,
        membership_symbol_count=membership_symbol_count,
        start=start,
        end=end,
        expected_trading_days=expected_trading_days,
        output_path=output_path,
        api_key=api_key,
        secret_key=secret_key,
        adjustment=Adjustment.RAW,
        identities=identities,
        client_factory=client_factory,
    )
