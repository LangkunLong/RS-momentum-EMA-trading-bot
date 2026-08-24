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
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from itertools import groupby
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

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
_REQUIRED_METADATA = {
    "bundle_kind",
    "schema_version",
    "data_cutoff",
    "evaluation_start",
    "warmup_start",
    "membership_source_sha256",
    "prices_source_sha256",
    "fundamentals_source_sha256",
    "membership_provenance_sha256",
    "prices_provenance_sha256",
    "fundamentals_provenance_sha256",
    "membership_source_kind",
    "membership_revision_id",
    "membership_raw_sha256",
    "membership_symbol_map_sha256",
    "membership_security_names_sha256",
    "prices_source_kind",
    "prices_upstream_source_sha256",
    "spy_trading_days_sha256",
    "price_identity_map_sha256",
    "price_identity_request_contracts_sha256",
    "price_exclusion_count",
    "price_exclusions_sha256",
    "fundamentals_source_kind",
    "fundamentals_submissions_archive_sha256",
    "fundamentals_companyfacts_archive_sha256",
    "fundamentals_identity_manifest_csv_sha256",
}
_SAME_ISSUER_CONTINUITIES = {
    "same_issuer_rename",
    "same_issuer_ticker_reuse",
    "legacy_survivor_rename",
    "accounting_acquirer_rename",
}


@dataclass(frozen=True)
class IdentityTransition:
    effective_date: date
    predecessor: str
    successor: str
    chain_id: str
    continuity_kind: str


@dataclass(frozen=True)
class PriceIdentityTransitionContract:
    """Hash-bound rules for carrying an open holding across ticker identities."""

    prices_provenance_sha256: str
    request_contracts_sha256: str
    identities: Mapping[str, Mapping[str, object]]
    transitions: tuple[IdentityTransition, ...]

    def __post_init__(self) -> None:
        frozen = {
            ticker: MappingProxyType(dict(values))
            for ticker, values in self.identities.items()
        }
        object.__setattr__(self, "identities", MappingProxyType(frozen))
        boundary_predecessors: set[tuple[date, str]] = set()
        boundary_successors: set[tuple[date, str]] = set()
        for transition in self.transitions:
            predecessor = frozen.get(transition.predecessor)
            successor = frozen.get(transition.successor)
            if predecessor is None or successor is None:
                raise ValueError("holding transition references an unknown identity")
            successor_bounds = (
                date.fromisoformat(str(successor["admitted_start"])),
                date.fromisoformat(str(successor["admitted_end"])),
            )
            if not successor_bounds[0] <= transition.effective_date <= successor_bounds[1]:
                raise ValueError("holding transition successor is not admitted at the boundary")
            predecessor_key = (transition.effective_date, transition.predecessor)
            successor_key = (transition.effective_date, transition.successor)
            if predecessor_key in boundary_predecessors or successor_key in boundary_successors:
                raise ValueError("holding transition boundary is not one-to-one")
            boundary_predecessors.add(predecessor_key)
            boundary_successors.add(successor_key)

    def resolve_open_holding(self, ticker: str, on_date: date | str) -> str:
        """Return an approved identity or fail closed after an ended identity."""
        symbol = _canonical_ticker(ticker)
        when = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
        if not isinstance(when, date):
            raise ValueError("holding transition date is invalid")
        matches = [
            item
            for item in self.transitions
            if item.predecessor == symbol and item.effective_date == when
        ]
        if len(matches) > 1:
            raise ValueError("holding identity transition is ambiguous")
        if matches:
            transition = matches[0]
            successor = self.identities.get(transition.successor)
            if successor is None:
                raise ValueError("holding transition successor is unknown")
            successor_start = date.fromisoformat(str(successor["admitted_start"]))
            successor_end = date.fromisoformat(str(successor["admitted_end"]))
            if not successor_start <= when <= successor_end:
                raise ValueError("holding transition successor is not admitted at the boundary")
            return transition.successor
        identity = self.identities.get(symbol)
        if identity is None:
            raise ValueError(f"holding identity is not in the hash-bound contract: {symbol}")
        admitted_start = date.fromisoformat(str(identity["admitted_start"]))
        admitted_end = date.fromisoformat(str(identity["admitted_end"]))
        if when < admitted_start or when > admitted_end:
            raise ValueError(f"open holding crossed an unsupported price identity boundary: {symbol}")
        active = True
        for transition in self.transitions:
            if transition.effective_date > when:
                continue
            if transition.predecessor == symbol:
                active = False
            if transition.successor == symbol:
                active = True
        if not active:
            raise ValueError(f"open holding crossed an unhandled same-issuer transition: {symbol}")
        return symbol


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


def _canonical_json_sha256(value: object) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return hashlib.sha256(payload).hexdigest()


def _json_file(path: str | Path) -> tuple[Path, Mapping[str, object]]:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("provenance must be a regular non-link JSON file")
    resolved = candidate.resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("provenance JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("provenance JSON must contain an object")
    return resolved, value


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
            self._validate_integrity()
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
        missing = _REQUIRED_METADATA.difference(metadata)
        if missing:
            raise ValueError(f"point-in-time bundle metadata is incomplete: {sorted(missing)}")
        if metadata.get("bundle_kind") != "canslim_pit_v1":
            raise ValueError("point-in-time bundle kind is invalid")
        cutoff = metadata.get("data_cutoff", "").strip()
        if not cutoff:
            raise ValueError("point-in-time bundle data_cutoff is required")
        try:
            cutoff_date = date.fromisoformat(cutoff)
            evaluation_start = date.fromisoformat(metadata["evaluation_start"])
            warmup_start = date.fromisoformat(metadata["warmup_start"])
        except (TypeError, ValueError) as exc:
            raise ValueError("point-in-time bundle date metadata is invalid") from exc
        if not warmup_start < evaluation_start <= cutoff_date:
            raise ValueError("point-in-time bundle date contract is invalid")
        for key, value in metadata.items():
            if key.endswith("_sha256") and not _DIGEST_RE.fullmatch(value):
                raise ValueError(f"point-in-time bundle metadata digest is invalid: {key}")
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

    def _validate_integrity(self) -> None:
        duplicate_membership = self._connection.execute(
            "SELECT 1 FROM membership GROUP BY effective_date,ticker HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        duplicate_prices = self._connection.execute(
            "SELECT 1 FROM price GROUP BY trade_date,ticker HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        duplicate_fundamentals = self._connection.execute(
            "SELECT 1 FROM fundamentals GROUP BY ticker,statement_type,period_end,public_date "
            "HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate_membership or duplicate_prices or duplicate_fundamentals:
            raise ValueError("point-in-time bundle contains duplicate logical rows")
        bad_fundamental = self._connection.execute(
            "SELECT 1 FROM fundamentals WHERE public_date <= period_end OR public_date > ? LIMIT 1",
            (self.metadata["data_cutoff"],),
        ).fetchone()
        if bad_fundamental:
            raise ValueError("point-in-time bundle contains an invalid fundamental public date")
        after_cutoff = self._connection.execute(
            "SELECT 1 FROM membership WHERE effective_date > ? UNION ALL "
            "SELECT 1 FROM price WHERE trade_date > ? LIMIT 1",
            (self.metadata["data_cutoff"], self.metadata["data_cutoff"]),
        ).fetchone()
        if after_cutoff:
            raise ValueError("point-in-time bundle contains rows after data_cutoff")
        nonmember_fundamental = self._connection.execute(
            "SELECT 1 FROM fundamentals f WHERE NOT EXISTS "
            "(SELECT 1 FROM membership m WHERE m.ticker=f.ticker) LIMIT 1"
        ).fetchone()
        if nonmember_fundamental:
            raise ValueError("point-in-time bundle contains fundamentals outside membership")
        spy_membership = self._connection.execute(
            "SELECT 1 FROM membership WHERE ticker='SPY' LIMIT 1"
        ).fetchone()
        spy_price = self._connection.execute(
            "SELECT 1 FROM price WHERE ticker='SPY' LIMIT 1"
        ).fetchone()
        if spy_membership or not spy_price:
            raise ValueError("point-in-time bundle SPY membership/price invariant failed")
        first_price = self._connection.execute("SELECT MIN(trade_date) FROM price").fetchone()[0]
        if first_price is None:
            raise ValueError("point-in-time bundle has no prices")
        first_price_date = date.fromisoformat(str(first_price))
        warmup_start = date.fromisoformat(self.metadata["warmup_start"])
        if first_price_date < warmup_start or first_price_date > date(2020, 1, 2):
            raise ValueError("point-in-time bundle does not satisfy the warm-up price boundary")
        evaluation_start = self.metadata["evaluation_start"]
        spy_days = [
            str(row[0])
            for row in self._connection.execute(
                "SELECT trade_date FROM price WHERE ticker='SPY' AND trade_date >= ? ORDER BY trade_date",
                (evaluation_start,),
            ).fetchall()
        ]
        if not spy_days:
            raise ValueError("point-in-time bundle has no evaluation-period SPY sessions")
        counts = [len(self.membership.members_at(day)) for day in spy_days]
        if min(counts) < 495 or max(counts) > 510:
            raise ValueError("point-in-time bundle membership count is outside 495 through 510")

    @property
    def data_cutoff(self) -> pd.Timestamp:
        return pd.Timestamp(self.metadata["data_cutoff"])

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._symbols))

    def load_price_identity_transition_contract(
        self,
        prices_provenance: str | Path,
    ) -> PriceIdentityTransitionContract:
        """Load reviewed holding transitions bound to this exact bundle."""
        path, provenance = _json_file(prices_provenance)
        provenance_sha = sha256_file(path)
        if provenance_sha != self.metadata["prices_provenance_sha256"]:
            raise ValueError("prices provenance does not match the bundle metadata digest")
        raw_contracts = provenance.get("price_identity_request_contracts")
        if not isinstance(raw_contracts, dict) or not raw_contracts:
            raise ValueError("prices provenance has no identity request contract")
        contract_sha = _canonical_json_sha256(raw_contracts)
        if contract_sha != self.metadata["price_identity_request_contracts_sha256"]:
            raise ValueError("price identity request contract digest does not match the bundle")
        if provenance.get("price_identity_request_contracts_sha256") != contract_sha:
            raise ValueError("prices provenance identity contract digest is internally inconsistent")
        if provenance.get("price_identity_map_sha256") != self.metadata.get("price_identity_map_sha256"):
            raise ValueError("prices provenance identity map digest does not match the bundle")
        expected_fields = {
            "provider_symbol",
            "identity_asof",
            "admitted_start",
            "admitted_end",
            "chain_id",
            "continuity_kind",
            "warmup_predecessor",
            "factor_anchor",
        }
        identities: dict[str, Mapping[str, object]] = {}
        for raw_ticker, raw_identity in raw_contracts.items():
            ticker = _canonical_ticker(raw_ticker)
            if ticker != raw_ticker or not isinstance(raw_identity, dict) or set(raw_identity) != expected_fields:
                raise ValueError("prices provenance contains an invalid identity contract row")
            start = date.fromisoformat(str(raw_identity["admitted_start"]))
            end = date.fromisoformat(str(raw_identity["admitted_end"]))
            date.fromisoformat(str(raw_identity["identity_asof"]))
            if end < start or not isinstance(raw_identity["chain_id"], str):
                raise ValueError("prices provenance identity bounds are invalid")
            if not isinstance(raw_identity["continuity_kind"], str):
                raise ValueError("prices provenance continuity kind is invalid")
            identities[ticker] = MappingProxyType(dict(raw_identity))
        required_identities = {event.ticker for event in self.membership.events}.union({"SPY"})
        if set(identities) != required_identities:
            raise ValueError("prices provenance identities do not exactly cover membership plus SPY")

        events_by_date: dict[date, dict[bool, list[str]]] = {}
        for event in self.membership.events:
            grouped = events_by_date.setdefault(event.effective_date, {True: [], False: []})
            grouped[event.member].append(event.ticker)
        transitions: list[IdentityTransition] = []
        for effective, grouped in sorted(events_by_date.items()):
            for predecessor in sorted(grouped[False]):
                predecessor_identity = identities.get(predecessor)
                if predecessor_identity is None:
                    continue
                predecessor_kind = str(predecessor_identity["continuity_kind"])
                if predecessor_kind not in _SAME_ISSUER_CONTINUITIES:
                    continue
                chain_id = str(predecessor_identity["chain_id"])
                successors = [
                    successor
                    for successor in grouped[True]
                    if successor in identities
                    and str(identities[successor]["chain_id"]) == chain_id
                    and str(identities[successor]["continuity_kind"]) in _SAME_ISSUER_CONTINUITIES
                ]
                if len(successors) > 1:
                    raise ValueError("prices provenance has an ambiguous same-issuer transition")
                if successors:
                    transitions.append(
                        IdentityTransition(
                            effective,
                            predecessor,
                            successors[0],
                            chain_id,
                            str(identities[successors[0]]["continuity_kind"]),
                        )
                    )
        return PriceIdentityTransitionContract(
            provenance_sha,
            contract_sha,
            identities,
            tuple(transitions),
        )

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
        spy_days = [
            str(row[0])
            for row in self._connection.execute(
                "SELECT trade_date FROM price WHERE ticker='SPY' AND trade_date >= ? ORDER BY trade_date",
                (self.metadata["evaluation_start"],),
            ).fetchall()
        ]
        membership_counts = [len(self.membership.members_at(day)) for day in spy_days]
        return {
            "bundle_sha256": self.sha256,
            "schema_version": self.metadata["schema_version"],
            "data_cutoff": str(self.data_cutoff.date()),
            "evaluation_start": self.metadata["evaluation_start"],
            "warmup_start": self.metadata["warmup_start"],
            "membership_events": len(self.membership.events),
            "symbol_count": len(self._symbols),
            "metadata": dict(sorted(self.metadata.items())),
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
                    "evaluation_session_min_members": min(membership_counts),
                    "evaluation_session_max_members": max(membership_counts),
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
        return self._fundamental_snapshot(records)

    def iter_fundamental_state_boundaries(
        self,
        date_bounds: Mapping[
            str,
            tuple[
                pd.Timestamp | datetime | date,
                pd.Timestamp | datetime | date,
            ],
        ],
    ) -> Iterable[tuple[str, date, dict[str, Any]]]:
        """Yield exact as-of snapshots at each requested ticker's state boundaries.

        The first boundary is the inclusive start-date snapshot with all earlier
        filings folded into it. Later boundaries are distinct public dates, with
        every row sharing a date applied atomically. A right-inclusive lookup can
        therefore reproduce ``fundamentals_as_of`` anywhere inside each range
        without evaluating unobservable pre-range intermediate states.
        """

        bounds: dict[str, tuple[date, date]] = {}
        for raw_ticker, raw_bounds in date_bounds.items():
            ticker = _canonical_ticker(raw_ticker)
            if ticker in bounds or not isinstance(raw_bounds, tuple) or len(raw_bounds) != 2:
                raise ValueError("fundamental state date bounds are invalid")
            start = pd.Timestamp(raw_bounds[0]).date()
            end = pd.Timestamp(raw_bounds[1]).date()
            if start > end:
                raise ValueError("fundamental state date bounds are invalid")
            if pd.Timestamp(end) > self.data_cutoff:
                raise ValueError("requested fundamental date exceeds point-in-time bundle cutoff")
            bounds[ticker] = (start, end)
        symbols = tuple(sorted(bounds))
        if not symbols:
            return
        cutoff = max(end for _start, end in bounds.values()).isoformat()
        placeholders = ",".join("?" for _ in symbols)
        rows = self._connection.execute(
            "SELECT ticker, statement_type, period_end, public_date, basic_eps, diluted_eps, "
            "total_revenue, net_income, common_stock, total_stockholders_equity, "
            "shares_outstanding, held_percent_institutions, institution_count, "
            "prev_institution_count FROM fundamentals "
            f"WHERE ticker IN ({placeholders}) AND public_date <= ? "
            "ORDER BY ticker, public_date, period_end",
            (*symbols, cutoff),
        )
        ticker_groups = iter(groupby(
            rows,
            key=lambda row: _canonical_ticker(row["ticker"]),
        ))
        next_group = next(ticker_groups, None)
        for ticker in symbols:
            ticker_rows: list[sqlite3.Row] = []
            if next_group is not None and next_group[0] == ticker:
                ticker_rows = list(next_group[1])
                next_group = next(ticker_groups, None)
            elif next_group is not None and next_group[0] < ticker:
                raise ValueError("fundamental state stream order is invalid")
            start, end = bounds[ticker]
            history: list[dict[str, Any]] = []
            baseline_emitted = False
            for public_date, visible_rows in groupby(
                ticker_rows,
                key=lambda row: date.fromisoformat(str(row["public_date"])),
            ):
                if public_date > end:
                    break
                if public_date > start and not baseline_emitted:
                    yield ticker, start, self._fundamental_snapshot(history)
                    baseline_emitted = True
                boundary_rows = [dict(row) for row in visible_rows]
                for record in boundary_rows:
                    if record["statement_type"] not in _STATEMENT_TYPES:
                        raise ValueError("fundamentals statement_type is invalid")
                history.extend(boundary_rows)
                if public_date > start:
                    yield ticker, public_date, self._fundamental_snapshot(history)
            if not baseline_emitted:
                yield ticker, start, self._fundamental_snapshot(history)

    @classmethod
    def _fundamental_snapshot(cls, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "quarterly_income": cls._statement_frame(records, "quarterly"),
            "annual_income": cls._statement_frame(records, "annual"),
            "balance_sheet": cls._statement_frame(records, "balance"),
            "company_info": cls._company_info(records),
        }

    @staticmethod
    def _statement_frame(records: list[dict[str, Any]], statement_type: str) -> pd.DataFrame:
        selected = [record for record in records if record["statement_type"] == statement_type]
        if not selected:
            return pd.DataFrame()
        frame = pd.DataFrame(selected)
        frame["period_end"] = pd.to_datetime(frame["period_end"], errors="raise")
        frame["public_date"] = pd.to_datetime(frame["public_date"], errors="raise")
        frame = frame.sort_values(["period_end", "public_date"], kind="stable").drop_duplicates(
            subset=["period_end"], keep="last"
        )
        frame = frame.set_index("period_end")[list(_FUNDAMENTAL_COLUMN_MAP)]
        frame = frame.rename(columns=_FUNDAMENTAL_COLUMN_MAP).transpose()
        frame = frame.dropna(axis="index", how="all").sort_index(axis="columns")
        return frame

    @staticmethod
    def _company_info(records: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        }
        institutional_pair_selected = False
        for record in reversed(records):
            for key in ("shares_outstanding", "held_percent_institutions"):
                value = record.get(key)
                if result[key] is None and value is not None:
                    result[key] = value
            institution_count = record.get("institution_count")
            prev_institution_count = record.get("prev_institution_count")
            if not institutional_pair_selected and (
                institution_count is not None or prev_institution_count is not None
            ):
                result["institution_count"] = institution_count
                result["prev_institution_count"] = prev_institution_count
                institutional_pair_selected = True
        return result

    def fundamentals_provider(self, symbol: str, as_of_date: pd.Timestamp) -> dict[str, Any]:
        return self.fundamentals_as_of(symbol, as_of_date)
