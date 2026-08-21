"""Read-only point-in-time dataset resolver for historical backtests.

The normal backtest cache is a convenience cache of provider responses.  It
is not a point-in-time dataset because it contains today's index membership and
does not carry public dates for every fundamental record.  This module defines
the stricter bundle used by ``pit-canslim`` and ``leader-basket`` modes.

The bundle is a SQLite database with four required tables:

``dataset_metadata(key TEXT PRIMARY KEY, value TEXT)``
    Must contain ``schema_version=1`` and a non-empty ``data_cutoff``.
``membership(effective_date TEXT, ticker TEXT, member INTEGER)``
    Dated index membership transitions.  ``member`` is 0 or 1.
``price(trade_date TEXT, ticker TEXT, open REAL, high REAL, low REAL,
close REAL, volume REAL)``
    Split-adjusted daily bars.
``fundamentals(ticker TEXT, statement_type TEXT, period_end TEXT,
public_date TEXT, basic_eps REAL, diluted_eps REAL, total_revenue REAL,
net_income REAL, common_stock REAL, total_stockholders_equity REAL,
shares_outstanding REAL, held_percent_institutions REAL,
institution_count INTEGER, prev_institution_count INTEGER)``

The CLI requires an expected SHA-256 for the bundle.  The connection is opened
read-only and no current-provider fallback is allowed when a historical field
is missing.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import pandas as pd

from core.leader_evaluation import PointInTimeUniverse

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_TABLES = {"dataset_metadata", "membership", "price", "fundamentals"}
_REQUIRED_PRICE_COLUMNS = {
    "trade_date",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "volume",
}
_REQUIRED_FUNDAMENTAL_COLUMNS = {
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
}
_FUNDAMENTAL_COLUMN_MAP = {
    "basic_eps": "Basic EPS",
    "diluted_eps": "Diluted EPS",
    "total_revenue": "Total Revenue",
    "net_income": "Net Income",
    "common_stock": "Common Stock",
    "total_stockholders_equity": "Total Stockholders Equity",
}
_STATEMENT_TYPES = {"quarterly", "annual", "balance", "institutional"}


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a regular file without modifying it."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("point-in-time bundle must be a regular non-link file")
    return candidate.resolve()


def _canonical_ticker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be text")
    ticker = value.strip().upper()
    if not ticker or len(ticker) > 8 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ.-" for char in ticker):
        raise ValueError("ticker is not canonical")
    return ticker


class PITDataBundle:
    """Validated, read-only point-in-time price/fundamental data bundle."""

    def __init__(self, path: str | Path, *, expected_sha256: str) -> None:
        self.path = _regular_file(path)
        if not isinstance(expected_sha256, str) or not _DIGEST_RE.fullmatch(expected_sha256):
            raise ValueError("expected_sha256 must be lowercase hexadecimal SHA-256")
        actual = sha256_file(self.path)
        if actual != expected_sha256:
            raise ValueError("point-in-time bundle SHA-256 does not match the expected digest")
        self.sha256 = actual
        uri = f"{self.path.as_uri()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row
        try:
            self._validate_schema()
            self.metadata = self._load_metadata()
            self.membership = self._load_membership()
            self._symbols = self._load_symbols()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PITDataBundle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _validate_schema(self) -> None:
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"point-in-time bundle is missing tables: {sorted(missing)}")
        if not {"key", "value"}.issubset(self._table_columns("dataset_metadata")):
            raise ValueError("dataset_metadata columns are invalid")
        if not {"effective_date", "ticker", "member"}.issubset(self._table_columns("membership")):
            raise ValueError("membership columns are invalid")
        if not _REQUIRED_PRICE_COLUMNS.issubset(self._table_columns("price")):
            raise ValueError("price columns are invalid")
        if not _REQUIRED_FUNDAMENTAL_COLUMNS.issubset(self._table_columns("fundamentals")):
            raise ValueError("fundamentals columns are invalid")
        for table in sorted(_REQUIRED_TABLES - {"dataset_metadata"}):
            count = int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if count == 0:
                raise ValueError(f"point-in-time bundle table is empty: {table}")

    def _load_metadata(self) -> dict[str, str]:
        rows = self._connection.execute("SELECT key, value FROM dataset_metadata").fetchall()
        metadata = {str(row[0]): str(row[1]) for row in rows}
        if metadata.get("schema_version") != "1":
            raise ValueError("point-in-time bundle schema_version must be 1")
        cutoff = metadata.get("data_cutoff", "").strip()
        if not cutoff:
            raise ValueError("point-in-time bundle data_cutoff is required")
        try:
            pd.Timestamp(cutoff)
        except (TypeError, ValueError) as exc:
            raise ValueError("point-in-time bundle data_cutoff is invalid") from exc
        return metadata

    def _load_membership(self) -> PointInTimeUniverse:
        rows = self._connection.execute(
            "SELECT effective_date, ticker, member FROM membership "
            "ORDER BY effective_date, ticker"
        ).fetchall()
        normalized: list[dict[str, object]] = []
        for row in rows:
            member = row[2]
            if type(member) is not int or member not in {0, 1}:
                raise ValueError("membership member must be integer 0 or 1")
            effective_date = pd.Timestamp(row[0])
            if effective_date > self.data_cutoff:
                raise ValueError("membership event exceeds point-in-time bundle cutoff")
            normalized.append(
                {
                    "effective_date": effective_date.date().isoformat(),
                    "ticker": _canonical_ticker(row[1]),
                    "member": bool(member),
                }
            )
        return PointInTimeUniverse.from_rows(normalized)

    def _load_symbols(self) -> frozenset[str]:
        prices = self._connection.execute("SELECT DISTINCT ticker FROM price").fetchall()
        symbols = {_canonical_ticker(row[0]) for row in prices}
        symbols.update(event.ticker for event in self.membership.events)
        return frozenset(symbols)

    @property
    def data_cutoff(self) -> pd.Timestamp:
        return pd.Timestamp(self.metadata["data_cutoff"])

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._symbols))

    def manifest(self) -> dict[str, object]:
        """Return content-free identity and coverage facts for audit logs."""

        price_coverage = self._connection.execute(
            "SELECT MIN(trade_date), MAX(trade_date), COUNT(*), COUNT(DISTINCT ticker) FROM price"
        ).fetchone()
        fundamental_coverage = self._connection.execute(
            "SELECT MIN(public_date), MAX(public_date), COUNT(*), COUNT(DISTINCT ticker) FROM fundamentals"
        ).fetchone()
        membership_coverage = self._connection.execute(
            "SELECT MIN(effective_date), MAX(effective_date), COUNT(*), COUNT(DISTINCT ticker) FROM membership"
        ).fetchone()
        return {
            "bundle_sha256": self.sha256,
            "schema_version": self.metadata["schema_version"],
            "data_cutoff": str(self.data_cutoff.date()),
            "membership_events": len(self.membership.events),
            "symbol_count": len(self._symbols),
            "metadata": {
                key: value
                for key, value in self.metadata.items()
                if key.endswith("_sha256") or key in {"bundle_kind"}
            },
            "coverage": {
                "price": {
                    "first_date": price_coverage[0],
                    "last_date": price_coverage[1],
                    "rows": int(price_coverage[2]),
                    "symbols": int(price_coverage[3]),
                },
                "fundamentals": {
                    "first_public_date": fundamental_coverage[0],
                    "last_public_date": fundamental_coverage[1],
                    "rows": int(fundamental_coverage[2]),
                    "symbols": int(fundamental_coverage[3]),
                },
                "membership": {
                    "first_effective_date": membership_coverage[0],
                    "last_effective_date": membership_coverage[1],
                    "events": int(membership_coverage[2]),
                    "symbols": int(membership_coverage[3]),
                },
            },
        }

    def members_at(self, when: str | datetime | pd.Timestamp) -> frozenset[str]:
        if isinstance(when, datetime):
            when = when.date().isoformat()
        elif isinstance(when, pd.Timestamp):
            when = when.date().isoformat()
        return self.membership.members_at(when)

    def _query_prices(
        self,
        tickers: Iterable[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        symbols = tuple(sorted({_canonical_ticker(ticker) for ticker in tickers}))
        if not symbols:
            return pd.DataFrame()
        if pd.Timestamp(end_date) > self.data_cutoff:
            raise ValueError("requested end date exceeds point-in-time bundle cutoff")
        placeholders = ",".join("?" for _ in symbols)
        rows = self._connection.execute(
            f"SELECT trade_date, ticker, open, high, low, close, volume FROM price "
            f"WHERE trade_date >= ? AND trade_date <= ? AND ticker IN ({placeholders}) "
            "ORDER BY trade_date, ticker",
            (pd.Timestamp(start_date).date().isoformat(), pd.Timestamp(end_date).date().isoformat(), *symbols),
        ).fetchall()
        frame = pd.DataFrame.from_records([tuple(row) for row in rows], columns=[
            "trade_date", "ticker", "Open", "High", "Low", "Close", "Volume"
        ])
        if frame.empty:
            return frame
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise").dt.normalize()
        frame["ticker"] = frame["ticker"].map(_canonical_ticker)
        if frame.duplicated(["trade_date", "ticker"]).any():
            raise ValueError("point-in-time price bundle contains duplicate bars")
        for column in ("Open", "High", "Low", "Close", "Volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if not frame[column].map(lambda value: math.isfinite(float(value))).all():
                raise ValueError("point-in-time price bundle contains non-finite values")
        if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any():
            raise ValueError("point-in-time OHLC values must be positive")
        if (frame["Volume"] < 0).any():
            raise ValueError("point-in-time volume must be nonnegative")
        if (frame["High"] < frame[["Open", "Close"]].max(axis=1)).any():
            raise ValueError("point-in-time high does not contain open/close")
        if (frame["Low"] > frame[["Open", "Close"]].min(axis=1)).any():
            raise ValueError("point-in-time low does not contain open/close")
        return frame

    def fetch_price_data(
        self,
        tickers: Iterable[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> dict[str, pd.DataFrame]:
        frame = self._query_prices(tickers, start_date, end_date)
        result: dict[str, pd.DataFrame] = {}
        for ticker, group in frame.groupby("ticker", sort=True):
            result[ticker] = (
                group.drop(columns=["ticker"])
                .set_index("trade_date")
                .sort_index()
            )
        return result

    def fetch_closes(
        self,
        tickers: Iterable[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        frame = self._query_prices(tickers, start_date, end_date)
        if frame.empty:
            return pd.DataFrame()
        return (
            frame.pivot(index="trade_date", columns="ticker", values="Close")
            .sort_index()
            .sort_index(axis=1)
        )

    def fundamentals_as_of(self, symbol: str, as_of_date: pd.Timestamp | datetime) -> dict[str, Any]:
        """Return only fundamental records publicly available by *as_of_date*."""

        ticker = _canonical_ticker(symbol)
        if pd.Timestamp(as_of_date) > self.data_cutoff:
            raise ValueError("requested fundamental date exceeds point-in-time bundle cutoff")
        cutoff = pd.Timestamp(as_of_date).date().isoformat()
        rows = self._connection.execute(
            "SELECT ticker, statement_type, period_end, public_date, basic_eps, diluted_eps, "
            "total_revenue, net_income, common_stock, total_stockholders_equity, "
            "shares_outstanding, held_percent_institutions, institution_count, "
            "prev_institution_count FROM fundamentals "
            "WHERE ticker = ? AND public_date <= ? ORDER BY public_date, period_end",
            (ticker, cutoff),
        ).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            if record["statement_type"] not in _STATEMENT_TYPES:
                raise ValueError("fundamentals statement_type is invalid")
        result: dict[str, Any] = {
            "quarterly_income": self._statement_frame(records, "quarterly"),
            "annual_income": self._statement_frame(records, "annual"),
            "balance_sheet": self._statement_frame(records, "balance"),
            "company_info": self._company_info(records),
        }
        return result

    @staticmethod
    def _statement_frame(records: list[dict[str, Any]], statement_type: str) -> pd.DataFrame:
        selected = [record for record in records if record["statement_type"] == statement_type]
        if not selected:
            return pd.DataFrame()
        frame = pd.DataFrame(selected)
        frame["period_end"] = pd.to_datetime(frame["period_end"], errors="raise")
        frame = frame.sort_values(["period_end", "public_date"]).drop_duplicates(
            subset=["period_end"], keep="last"
        )
        frame = frame.set_index("period_end")
        return frame.rename(columns=_FUNDAMENTAL_COLUMN_MAP)[
            list(_FUNDAMENTAL_COLUMN_MAP.values())
        ]

    @staticmethod
    def _company_info(records: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        }
        for record in reversed(records):
            for key in result:
                value = record.get(key)
                if result[key] is None and value is not None:
                    result[key] = value
        return result

    def fundamentals_provider(self, symbol: str, as_of_date: pd.Timestamp) -> dict[str, Any]:
        return self.fundamentals_as_of(symbol, as_of_date)
