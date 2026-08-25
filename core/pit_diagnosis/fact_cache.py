"""Incremental, immutable, point-in-time diagnosis fact materialization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import pandas as pd

from core.pit_data import PITDataBundle
from .models import DatePartitions, Rulebook
from .patterns import BasePolicy, detect_proper_base
from .rs import calculate_pit_rs_snapshot
from .rulebook import canonical_sha256
from .supplemental import (
    IndustryGroupSnapshot,
    InstitutionalSnapshot,
    SupplementalPITProvider,
    UnavailableSupplementalPITProvider,
)

_SCHEMA_VERSION = "2"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ZERO_DIGEST = "0" * 64
_FACT_COLUMNS = (
    "bundle_sha256", "rulebook_schema_version", "symbol", "session", "member",
    "open", "high", "low", "close", "volume", "prior_close", "prior_average_volume_50", "event_volume_ratio",
    "current_eps", "prior_year_eps", "current_sales", "prior_year_sales", "annual_eps_1", "annual_eps_2", "annual_eps_3", "annual_eps_4",
    "net_income", "total_stockholders_equity", "current_eps_yoy", "sales_yoy", "roe", "shares_outstanding",
    "institutional_ownership_percent", "institutional_holder_count", "institutional_previous_holder_count", "institutional_as_of_date", "institutional_evidence_ids",
    "rs_rating", "industry_group_id", "industry_rank", "industry_as_of_date", "industry_members", "industry_evidence_ids",
    "base_kind", "base_start_session", "base_end_session", "base_duration_sessions", "base_low", "base_depth_pct", "base_handle_start_session", "base_handle_end_session", "base_input_sha256", "pivot", "extension_pct",
    "market_regime", "distribution_count", "follow_through_session", "latest_fundamental_public_date", "availability_bitset", "row_sha256",
)
_HASHED_ROW_COLUMNS = tuple(column for column in _FACT_COLUMNS if column != "row_sha256")
_SCHEMA_SHA256 = canonical_sha256({"schema_version": _SCHEMA_VERSION, "columns": _FACT_COLUMNS, "primary_key": ["bundle_sha256", "rulebook_schema_version", "symbol", "session"]})
_V1_SCHEMA_SHA256 = canonical_sha256({"schema_version": "1", "columns": _FACT_COLUMNS, "primary_key": ["bundle_sha256", "rulebook_schema_version", "symbol", "session"]})
_PRICE_EVIDENCE = 1
_SCHEMA_IDENTITY_FIELDS = frozenset({"fact_cache_schema_version", "fact_cache_schema_sha256"})


def _session_facts_create_sql(schema_version: str) -> str:
    if schema_version not in {"1", _SCHEMA_VERSION}:
        raise ValueError("unrecognized fact-cache schema version")
    prices = "REAL NOT NULL" if schema_version == "1" else "REAL"
    definitions = {
        "bundle_sha256": "TEXT NOT NULL", "rulebook_schema_version": "TEXT NOT NULL", "symbol": "TEXT NOT NULL", "session": "TEXT NOT NULL", "member": "INTEGER NOT NULL CHECK(member IN (0,1))",
        "open": prices, "high": prices, "low": prices, "close": prices, "volume": prices,
        "prior_close": "REAL", "prior_average_volume_50": "REAL", "event_volume_ratio": "REAL",
        "current_eps": "REAL", "prior_year_eps": "REAL", "current_sales": "REAL", "prior_year_sales": "REAL", "annual_eps_1": "REAL", "annual_eps_2": "REAL", "annual_eps_3": "REAL", "annual_eps_4": "REAL",
        "net_income": "REAL", "total_stockholders_equity": "REAL", "current_eps_yoy": "REAL", "sales_yoy": "REAL", "roe": "REAL", "shares_outstanding": "REAL",
        "institutional_ownership_percent": "REAL", "institutional_holder_count": "INTEGER", "institutional_previous_holder_count": "INTEGER", "institutional_as_of_date": "TEXT", "institutional_evidence_ids": "TEXT NOT NULL",
        "rs_rating": "REAL", "industry_group_id": "TEXT", "industry_rank": "INTEGER", "industry_as_of_date": "TEXT", "industry_members": "TEXT NOT NULL", "industry_evidence_ids": "TEXT NOT NULL",
        "base_kind": "TEXT", "base_start_session": "TEXT", "base_end_session": "TEXT", "base_duration_sessions": "INTEGER", "base_low": "REAL", "base_depth_pct": "REAL", "base_handle_start_session": "TEXT", "base_handle_end_session": "TEXT", "base_input_sha256": "TEXT", "pivot": "REAL", "extension_pct": "REAL",
        "market_regime": "TEXT NOT NULL", "distribution_count": "INTEGER", "follow_through_session": "TEXT", "latest_fundamental_public_date": "TEXT", "availability_bitset": "INTEGER NOT NULL", "row_sha256": "TEXT NOT NULL",
    }
    columns = ", ".join(f"{name} {definitions[name]}" for name in _FACT_COLUMNS)
    return f"CREATE TABLE session_facts({columns}, PRIMARY KEY(bundle_sha256, rulebook_schema_version, symbol, session))"


_METADATA_CREATE_SQL = "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_V1_SESSION_FACTS_CREATE_SQL = _session_facts_create_sql("1")
_SESSION_FACTS_CREATE_SQL = _session_facts_create_sql(_SCHEMA_VERSION)


@dataclass(frozen=True)
class FactCacheIdentity:
    fields: Mapping[str, object]
    sha256: str

    @classmethod
    def from_inputs(cls, *, bundle: Any, rulebook: Rulebook, partitions: DatePartitions, supplemental_provider: SupplementalPITProvider) -> "FactCacheIdentity":
        provider_sha = _provider_identity(supplemental_provider)
        fields = {
            "bundle_sha256": _bundle_digest(bundle),
            "bundle_schema_version": str(getattr(bundle, "metadata", {}).get("schema_version", "")),
            "bundle_metadata": dict(sorted(getattr(bundle, "metadata", {}).items())),
            "rulebook_version": rulebook.version,
            "rulebook_sha256": rulebook.sha256,
            "partitions": {
                "discovery": partitions.discovery.as_tuple(), "validation": partitions.validation.as_tuple(),
                "locked_evaluation": partitions.locked_evaluation.as_tuple(),
            },
            "supplemental_content_identity_sha256": provider_sha,
            "fact_cache_schema_version": _SCHEMA_VERSION,
            "fact_cache_schema_sha256": _SCHEMA_SHA256,
        }
        return cls(MappingProxyType(fields), canonical_sha256(fields))


@dataclass(frozen=True)
class FactCacheBuildResult:
    path: Path
    content_sha256: str
    schema_sha256: str
    identity_sha256: str
    resumed: bool
    reprocessed_sessions: int


@dataclass(frozen=True)
class SessionFact:
    """A read-only normalized row, with column names available as attributes."""

    values: Mapping[str, object]

    def __getattr__(self, name: str) -> object:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, name: str) -> object:
        return self.values[name]


class FactCache:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection
        self.column_names = tuple(str(row[1]) for row in connection.execute("PRAGMA table_info(session_facts)"))

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "FactCache":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def session_fact(self, symbol: str, session: str) -> SessionFact:
        row = self._connection.execute(
            "SELECT * FROM session_facts WHERE symbol=? AND session=?", (str(symbol).upper(), _session_text(session)),
        ).fetchone()
        if row is None:
            raise KeyError(f"no fact for {symbol} at {session}")
        return SessionFact(MappingProxyType({key: row[key] for key in row.keys()}))


def build_fact_cache(
    *,
    bundle: PITDataBundle,
    rulebook: Rulebook,
    partitions: DatePartitions,
    output_path: Path,
    checkpoint_path: Path,
    progress_path: Path,
    resume: bool,
    supplemental_provider: SupplementalPITProvider | None = None,
    checkpoint_every_sessions: int = 5,
) -> FactCacheBuildResult:
    builder = FactCacheBuilder(
        bundle=bundle,
        rulebook=rulebook,
        partitions=partitions,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        progress_path=progress_path,
        supplemental_provider=supplemental_provider or UnavailableSupplementalPITProvider(),
        checkpoint_every_sessions=checkpoint_every_sessions,
    )
    return builder.build(resume=resume)


class FactCacheBuilder:
    def __init__(
        self, *, bundle: PITDataBundle, rulebook: Rulebook, partitions: DatePartitions, output_path: Path,
        checkpoint_path: Path, progress_path: Path, supplemental_provider: SupplementalPITProvider,
        checkpoint_every_sessions: int = 5,
    ) -> None:
        if type(checkpoint_every_sessions) is not int or checkpoint_every_sessions <= 0:
            raise ValueError("checkpoint_every_sessions must be a positive integer")
        self.bundle, self.rulebook, self.partitions = bundle, rulebook, partitions
        self.output_path, self.checkpoint_path, self.progress_path = Path(output_path), Path(checkpoint_path), Path(progress_path)
        self.partial_path = Path(f"{self.output_path}.partial")
        self.supplemental_provider = supplemental_provider
        self.checkpoint_every_sessions = checkpoint_every_sessions
        self.identity = FactCacheIdentity.from_inputs(bundle=bundle, rulebook=rulebook, partitions=partitions, supplemental_provider=supplemental_provider)

    def build(self, *, resume: bool) -> FactCacheBuildResult:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        sessions = self._sessions()
        if not sessions:
            raise ValueError("PIT bundle has no exact SPY sessions in the requested partitions")
        if self.output_path.exists():
            if not resume:
                raise ValueError("fact cache output already exists")
            cache = open_fact_cache(self.output_path, _sha256_file(self.output_path))
            try:
                metadata = _metadata(cache._connection)
                if metadata.get("identity_sha256") != self.identity.sha256:
                    raise ValueError("fact cache identity mismatch")
                return FactCacheBuildResult(self.output_path, _sha256_file(self.output_path), metadata["schema_sha256"], self.identity.sha256, True, 0)
            finally:
                cache.close()
        partial_state = any(path.exists() for path in (self.partial_path, self.checkpoint_path, self.progress_path))
        if partial_state and not resume:
            raise ValueError("fact cache partial state already exists; use resume=True")
        if resume and partial_state:
            if not self.partial_path.exists():
                raise ValueError("fact cache partial state is incomplete")
            conn = sqlite3.connect(self.partial_path)
            try:
                self._verify_partial_metadata(conn)
            except ValueError as verification_error:
                conn.close()
                # A building v1 cache cannot represent a PIT member with no
                # exact price bar because OHLCV was NOT NULL.  It is not a
                # usable immutable artifact, so restart it under v2 rather
                # than carry stale, structurally incompatible checkpoints.
                if not self._is_migratable_v1_partial(sessions):
                    raise verification_error
                for path in (self.partial_path, self.checkpoint_path, self.progress_path):
                    path.unlink(missing_ok=True)
                conn = sqlite3.connect(self.partial_path)
                self._create_schema(conn)
                next_index, resumed = 0, False
            else:
                if self.checkpoint_path.exists():
                    self._validate_checkpoint(_load_checkpoint(self.checkpoint_path), sessions)
                if self.progress_path.exists():
                    self._validate_progress(sessions)
                next_index = self._reconcile_completed_sessions(conn, sessions)
                resumed = True
        else:
            for path in (self.partial_path, self.checkpoint_path, self.progress_path):
                if path.exists():
                    raise ValueError("fact cache partial state already exists; use resume=True")
            conn = sqlite3.connect(self.partial_path)
            self._create_schema(conn)
            next_index, resumed = 0, False
        try:
            fundamental_states = self._fundamental_states(sessions)
            for index in range(next_index, len(sessions)):
                session = sessions[index]
                rows = self._materialize_session(session, fundamental_states)
                self._insert_rows(conn, rows)
                conn.commit()
                if (index + 1) % self.checkpoint_every_sessions == 0 or index + 1 == len(sessions):
                    self._write_progress(session, len(rows), rows[-1]["symbol"] if rows else "", index + 1)
                    self._write_checkpoint(index + 1)
                self._after_session(session)
            return self._finalize(conn, sessions, resumed=resumed)
        except Exception:
            conn.commit()
            raise
        finally:
            conn.close()

    def _after_session(self, session: str) -> None:
        """A narrow test seam after durable session state has been written."""
        del session

    def _sessions(self) -> list[str]:
        start = self.partitions.discovery.start
        end = self.partitions.locked_evaluation.end
        spy = self.bundle.fetch_price_data(("SPY",), pd.Timestamp(start), pd.Timestamp(end)).get("SPY")
        if spy is None or spy.empty:
            return []
        all_sessions = [_session_text(item) for item in pd.DatetimeIndex(spy.index)]
        ranges = (self.partitions.discovery, self.partitions.validation, self.partitions.locked_evaluation)
        return [session for session in all_sessions if any(partition.start <= session <= partition.end for partition in ranges)]

    def _fundamental_states(self, sessions: list[str]) -> dict[str, list[tuple[str, Mapping[str, Any]]]]:
        symbols = tuple(symbol for symbol in self.bundle.symbols() if symbol != "SPY")
        bounds = {symbol: (pd.Timestamp(sessions[0]), pd.Timestamp(sessions[-1])) for symbol in symbols}
        result: dict[str, list[tuple[str, Mapping[str, Any]]]] = {symbol: [] for symbol in symbols}
        for symbol, as_of, snapshot in self.bundle.iter_fundamental_state_boundaries(bounds):
            text = _session_text(as_of)
            if text > sessions[-1]:
                raise ValueError("future-dated fundamental state")
            result.setdefault(str(symbol).upper(), []).append((text, snapshot))
        for states in result.values():
            states.sort(key=lambda item: item[0])
        return result

    def _materialize_session(self, session: str, states: Mapping[str, list[tuple[str, Mapping[str, Any]]]]) -> list[dict[str, object]]:
        members = tuple(sorted(self.bundle.members_at(session)))
        if not members:
            return []
        warmup = str(getattr(self.bundle, "metadata", {}).get("warmup_start", session))
        closes = self.bundle.fetch_closes(members, pd.Timestamp(warmup), pd.Timestamp(session))
        rs = calculate_pit_rs_snapshot(closes, pd.Timestamp(session), eligible_tickers=members)
        price_data = self.bundle.fetch_price_data(members, pd.Timestamp(warmup), pd.Timestamp(session))
        spy_data = self.bundle.fetch_price_data(("SPY",), pd.Timestamp(warmup), pd.Timestamp(session)).get("SPY")
        market = _market_facts(spy_data, session)
        rows: list[dict[str, object]] = []
        for symbol in members:
            prices = price_data.get(symbol)
            state, state_date = _state_at(states.get(symbol, []), session)
            row = self._row(symbol, session, prices, state, state_date, rs.get(symbol), market)
            rows.append(row)
        return rows

    def _row(self, symbol: str, session: str, prices: pd.DataFrame | None, state: Mapping[str, Any] | None, state_date: str | None, rs_rating: float | None, market: Mapping[str, object]) -> dict[str, object]:
        if prices is None or pd.Timestamp(session) not in prices.index:
            return self._unpriced_row(symbol, session, state, state_date, market)
        history = prices.loc[prices.index <= pd.Timestamp(session)].copy()
        event = history.loc[pd.Timestamp(session)]
        prior = history.iloc[:-1]
        prior_close = _number(prior["Close"].iloc[-1]) if not prior.empty else None
        prior_avg = _number(prior["Volume"].tail(50).mean()) if not prior.empty else None
        ratio = _number(float(event["Volume"]) / prior_avg) if prior_avg not in (None, 0.0) else None
        fundamentals = _fundamental_values(state)
        institutional = self.supplemental_provider.institutional_snapshot(symbol, session)
        industry = self.supplemental_provider.industry_group_snapshot(symbol, session)
        _validate_supplemental(institutional, industry, session)
        base = None
        if len(prior) >= BasePolicy.canonical_v1().flat_min_sessions:
            base = detect_proper_base(prior[["High", "Low", "Close"]], event_session=session, policy=BasePolicy.canonical_v1())
        pivot = None if base is None else _number(base.pivot)
        extension = _number((float(event["Close"]) / pivot) - 1.0) if pivot not in (None, 0.0) else None
        availability = _PRICE_EVIDENCE | (2 if state is not None else 0) | (4 if institutional.available else 0) | (8 if industry.available else 0) | (16 if rs_rating is not None else 0) | (32 if base is not None else 0)
        row: dict[str, object] = {
            "bundle_sha256": _bundle_digest(self.bundle), "rulebook_schema_version": self.rulebook.version, "symbol": symbol, "session": session, "member": 1,
            "open": _number(event["Open"]), "high": _number(event["High"]), "low": _number(event["Low"]), "close": _number(event["Close"]), "volume": _number(event["Volume"]),
            "prior_close": prior_close, "prior_average_volume_50": prior_avg, "event_volume_ratio": ratio,
            **fundamentals,
            "institutional_ownership_percent": institutional.ownership_percent, "institutional_holder_count": institutional.holder_count, "institutional_previous_holder_count": institutional.previous_holder_count,
            "institutional_as_of_date": institutional.as_of_date, "institutional_evidence_ids": _json_tuple(institutional.evidence_ids),
            "rs_rating": _number(rs_rating), "industry_group_id": industry.group_id, "industry_rank": industry.group_rank, "industry_as_of_date": industry.as_of_date,
            "industry_members": _json_tuple(industry.group_members), "industry_evidence_ids": _json_tuple(industry.evidence_ids),
            "base_kind": None if base is None else base.kind.value, "base_start_session": None if base is None else base.start_session, "base_end_session": None if base is None else base.end_session,
            "base_duration_sessions": None if base is None else base.duration_sessions, "base_low": None if base is None else _number(base.low), "base_depth_pct": None if base is None else _number(base.depth_pct),
            "base_handle_start_session": None if base is None else base.handle_start_session, "base_handle_end_session": None if base is None else base.handle_end_session,
            "base_input_sha256": None if base is None else base.input_sha256, "pivot": pivot, "extension_pct": extension,
            **market, "latest_fundamental_public_date": state_date, "availability_bitset": availability,
        }
        _ensure_finite_row(row)
        row["row_sha256"] = canonical_sha256({column: row[column] for column in _HASHED_ROW_COLUMNS})
        return row

    def _unpriced_row(self, symbol: str, session: str, state: Mapping[str, Any] | None, state_date: str | None, market: Mapping[str, object]) -> dict[str, object]:
        """Keep a PIT member visible when no exact as-of bar exists; invent nothing."""
        fundamentals = _fundamental_values(state)
        institutional = self.supplemental_provider.institutional_snapshot(symbol, session)
        industry = self.supplemental_provider.industry_group_snapshot(symbol, session)
        _validate_supplemental(institutional, industry, session)
        availability = (2 if state is not None else 0) | (4 if institutional.available else 0) | (8 if industry.available else 0)
        row: dict[str, object] = {
            "bundle_sha256": _bundle_digest(self.bundle), "rulebook_schema_version": self.rulebook.version, "symbol": symbol, "session": session, "member": 1,
            "open": None, "high": None, "low": None, "close": None, "volume": None,
            "prior_close": None, "prior_average_volume_50": None, "event_volume_ratio": None,
            **fundamentals,
            "institutional_ownership_percent": institutional.ownership_percent, "institutional_holder_count": institutional.holder_count, "institutional_previous_holder_count": institutional.previous_holder_count,
            "institutional_as_of_date": institutional.as_of_date, "institutional_evidence_ids": _json_tuple(institutional.evidence_ids),
            "rs_rating": None, "industry_group_id": industry.group_id, "industry_rank": industry.group_rank, "industry_as_of_date": industry.as_of_date,
            "industry_members": _json_tuple(industry.group_members), "industry_evidence_ids": _json_tuple(industry.evidence_ids),
            "base_kind": None, "base_start_session": None, "base_end_session": None, "base_duration_sessions": None, "base_low": None, "base_depth_pct": None,
            "base_handle_start_session": None, "base_handle_end_session": None, "base_input_sha256": None, "pivot": None, "extension_pct": None,
            **market, "latest_fundamental_public_date": state_date, "availability_bitset": availability,
        }
        _ensure_finite_row(row)
        row["row_sha256"] = canonical_sha256({column: row[column] for column in _HASHED_ROW_COLUMNS})
        return row

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(_METADATA_CREATE_SQL)
        conn.execute(_SESSION_FACTS_CREATE_SQL)
        metadata = {"status": "building", "identity_sha256": self.identity.sha256, "identity": json.dumps(dict(self.identity.fields), sort_keys=True, separators=(",", ":")), "schema_version": _SCHEMA_VERSION, "schema_sha256": _SCHEMA_SHA256}
        conn.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", metadata.items())
        conn.commit()

    def _verify_partial_metadata(self, conn: sqlite3.Connection) -> None:
        metadata = _metadata(conn)
        if metadata.get("status") != "building" or metadata.get("identity_sha256") != self.identity.sha256:
            raise ValueError("fact cache identity mismatch")
        if metadata.get("schema_sha256") != _SCHEMA_SHA256:
            raise ValueError("fact cache schema mismatch")

    def _is_migratable_v1_partial(self, sessions: list[str]) -> bool:
        """Accept only a same-input v1 builder state for destructive migration.

        The v1-to-v2 restart deletes resumable state, so the old metadata must
        authenticate every immutable input rather than merely advertise its
        historical schema version.
        """
        old = sqlite3.connect(self.partial_path)
        try:
            if not _has_exact_v1_partial_schema(old):
                return False
            metadata = _metadata(old)
        except Exception:
            return False
        finally:
            old.close()
        if set(metadata) != {"status", "identity_sha256", "identity", "schema_version", "schema_sha256"}:
            return False
        if metadata.get("status") != "building" or metadata.get("schema_version") != "1":
            return False
        identity_sha256 = metadata.get("identity_sha256")
        schema_sha256 = metadata.get("schema_sha256")
        if not isinstance(identity_sha256, str) or not _DIGEST.fullmatch(identity_sha256):
            return False
        if schema_sha256 != _V1_SCHEMA_SHA256:
            return False
        try:
            identity = json.loads(str(metadata["identity"]), parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(identity, Mapping) or set(identity) != set(self.identity.fields):
            return False
        if canonical_sha256(identity) != identity_sha256:
            return False
        if identity.get("fact_cache_schema_version") != "1" or identity.get("fact_cache_schema_sha256") != _V1_SCHEMA_SHA256:
            return False
        expected = {key: value for key, value in self.identity.fields.items() if key not in _SCHEMA_IDENTITY_FIELDS}
        observed = {key: value for key, value in identity.items() if key not in _SCHEMA_IDENTITY_FIELDS}
        if canonical_sha256(observed) != canonical_sha256(expected):
            return False
        if self.checkpoint_path.exists() and not self.progress_path.exists():
            return False
        try:
            records = _load_progress(self.progress_path) if self.progress_path.exists() else []
            if records and not _valid_v1_progress(records, identity_sha256, sessions, self.checkpoint_every_sessions):
                return False
            if records and records[-1].get("state_sha256") != _state_sha256(self.partial_path):
                return False
            if self.checkpoint_path.exists():
                checkpoint = _load_checkpoint(self.checkpoint_path)
                if not _valid_v1_checkpoint(checkpoint, identity, identity_sha256, sessions):
                    return False
                if checkpoint["next_session_index"] != records[-1]["next_session_index"]:
                    return False
        except ValueError:
            return False
        return True

    def _validate_checkpoint(self, checkpoint: Mapping[str, object], sessions: list[str]) -> None:
        checkpoint_identity = checkpoint.get("identity")
        next_index = checkpoint.get("next_session_index")
        if (
            checkpoint.get("identity_sha256") != self.identity.sha256
            or not isinstance(checkpoint_identity, Mapping)
            or canonical_sha256(checkpoint_identity) != canonical_sha256(dict(self.identity.fields))
        ):
            raise ValueError("fact cache identity mismatch")
        if type(next_index) is not int or not 0 <= next_index <= len(sessions):
            raise ValueError("fact cache checkpoint index is invalid")

    def _validate_progress(self, sessions: list[str]) -> None:
        records = _load_progress(self.progress_path)
        previous_index = 0
        for record in records:
            next_index = record.get("next_session_index")
            if record.get("identity_sha256") != self.identity.sha256:
                raise ValueError("fact cache identity mismatch")
            if type(next_index) is not int or not previous_index < next_index <= len(sessions):
                raise ValueError("fact cache progress index is invalid")
            if record.get("session") != sessions[next_index - 1]:
                raise ValueError("fact cache progress session is invalid")
            previous_index = next_index

    def _reconcile_completed_sessions(self, conn: sqlite3.Connection, sessions: list[str]) -> int:
        for index, session in enumerate(sessions):
            expected_symbols = tuple(sorted(self.bundle.members_at(session)))
            rows = conn.execute(
                "SELECT symbol,member FROM session_facts WHERE bundle_sha256=? AND rulebook_schema_version=? AND session=? ORDER BY symbol",
                (_bundle_digest(self.bundle), self.rulebook.version, session),
            ).fetchall()
            if not rows:
                return index
            actual_symbols = tuple(str(row[0]) for row in rows)
            if actual_symbols != expected_symbols or any(int(row[1]) != 1 for row in rows):
                raise ValueError("fact cache membership coverage mismatch")
        return len(sessions)

    def _insert_rows(self, conn: sqlite3.Connection, rows: Iterable[Mapping[str, object]]) -> None:
        placeholders = ",".join("?" for _ in _FACT_COLUMNS)
        conn.executemany(f"INSERT INTO session_facts({','.join(_FACT_COLUMNS)}) VALUES ({placeholders})", ([row[column] for column in _FACT_COLUMNS] for row in rows))

    def _write_progress(self, session: str, rows: int, last_symbol: str, next_index: int) -> None:
        state_sha = _state_sha256(self.partial_path)
        record = {"phase": "session_complete", "session": session, "rows": rows, "last_symbol": last_symbol, "identity_sha256": self.identity.sha256, "state_sha256": state_sha, "next_session_index": next_index}
        with self.progress_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _write_checkpoint(self, next_index: int) -> None:
        payload = {"identity_sha256": self.identity.sha256, "identity": dict(self.identity.fields), "next_session_index": next_index}
        temp = self.checkpoint_path.with_name(f".{self.checkpoint_path.name}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.checkpoint_path)

    def _finalize(self, conn: sqlite3.Connection, sessions: list[str], *, resumed: bool) -> FactCacheBuildResult:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("fact cache SQLite integrity check failed")
        if self._reconcile_completed_sessions(conn, sessions) != len(sessions):
            raise ValueError("fact cache membership coverage mismatch")
        logical = hashlib.sha256("".join(row[0] for row in conn.execute("SELECT row_sha256 FROM session_facts ORDER BY session,symbol")).encode("ascii")).hexdigest()
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('content_sha256',?)", (logical,))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('status','complete')")
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        self.checkpoint_path.unlink(missing_ok=True)
        self.progress_path.unlink(missing_ok=True)
        os.replace(self.partial_path, self.output_path)
        os.chmod(self.output_path, 0o444)
        content = _sha256_file(self.output_path)
        return FactCacheBuildResult(self.output_path, content, _SCHEMA_SHA256, self.identity.sha256, resumed, 0)


def open_fact_cache(path: Path, expected_content_sha256: str) -> FactCache:
    target = Path(path)
    if not target.is_file() or not _DIGEST.fullmatch(expected_content_sha256) or _sha256_file(target) != expected_content_sha256:
        raise ValueError("fact cache content SHA-256 does not match expected digest")
    sidecars = (Path(f"{target}.partial"), target.with_suffix(".checkpoint.json"), target.with_suffix(".progress.jsonl"))
    if any(item.exists() for item in sidecars):
        raise ValueError("finalized fact cache has build sidecars")
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        metadata = _metadata(conn)
        if metadata.get("status") != "complete" or metadata.get("schema_sha256") != _SCHEMA_SHA256:
            raise ValueError("fact cache is not a complete recognized schema")
        columns = tuple(str(row[1]) for row in conn.execute("PRAGMA table_info(session_facts)"))
        if columns != _FACT_COLUMNS:
            raise ValueError("fact cache schema columns are invalid")
        return FactCache(target, conn)
    except Exception:
        conn.close()
        raise


def _bundle_digest(bundle: Any) -> str:
    digest = getattr(bundle, "sha256", None)
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("PIT bundle must expose a lowercase content SHA-256")
    return digest


def _provider_identity(provider: SupplementalPITProvider) -> str:
    digest = getattr(provider, "content_identity_sha256", getattr(provider, "content_sha256", None))
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("supplemental provider must expose a lowercase content identity")
    if not isinstance(provider, UnavailableSupplementalPITProvider) and digest == _ZERO_DIGEST:
        raise ValueError("real supplemental provider content identity must be nonzero")
    return digest


def _session_text(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _has_exact_v1_partial_schema(conn: sqlite3.Connection) -> bool:
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        objects = tuple(
            (str(row[0]), str(row[1]), row[2])
            for row in conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
        )
    except sqlite3.Error:
        return False
    return (
        integrity is not None and integrity[0] == "ok"
        and objects == (("table", "metadata", _METADATA_CREATE_SQL), ("table", "session_facts", _V1_SESSION_FACTS_CREATE_SQL))
    )


def _valid_v1_checkpoint(checkpoint: Mapping[str, object], identity: Mapping[str, object], identity_sha256: str, sessions: list[str]) -> bool:
    if set(checkpoint) != {"identity_sha256", "identity", "next_session_index"}:
        return False
    checkpoint_identity = checkpoint.get("identity")
    next_index = checkpoint.get("next_session_index")
    return (
        checkpoint.get("identity_sha256") == identity_sha256
        and isinstance(checkpoint_identity, Mapping)
        and canonical_sha256(checkpoint_identity) == canonical_sha256(identity)
        and type(next_index) is int
        and 0 <= next_index <= len(sessions)
    )


def _valid_v1_progress(records: list[Mapping[str, object]], identity_sha256: str, sessions: list[str], checkpoint_every_sessions: int) -> bool:
    previous_index = 0
    for record in records:
        if set(record) != {"phase", "session", "rows", "last_symbol", "identity_sha256", "state_sha256", "next_session_index"}:
            return False
        next_index = record.get("next_session_index")
        if (
            record.get("phase") != "session_complete"
            or record.get("identity_sha256") != identity_sha256
            or not isinstance(record.get("state_sha256"), str)
            or not _DIGEST.fullmatch(record["state_sha256"])
            or type(next_index) is not int
            or next_index != min(previous_index + checkpoint_every_sessions, len(sessions))
            or record.get("session") != sessions[next_index - 1]
            or type(record.get("rows")) is not int
            or record["rows"] < 0
            or not isinstance(record.get("last_symbol"), str)
        ):
            return False
        previous_index = next_index
    return True


def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in conn.execute("SELECT key,value FROM metadata")}


def _load_checkpoint(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("fact cache checkpoint is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("fact cache checkpoint is invalid")
    return value


def _load_progress(path: Path) -> list[Mapping[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("fact cache progress is invalid") from exc
    if not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("fact cache progress is invalid")
    return records


def _state_sha256(path: Path) -> str:
    return _sha256_file(path)


def _state_at(states: Iterable[tuple[str, Mapping[str, Any]]], session: str) -> tuple[Mapping[str, Any] | None, str | None]:
    latest: tuple[Mapping[str, Any] | None, str | None] = (None, None)
    for state_date, state in states:
        if state_date > session:
            break
        latest = state, state_date
    return latest


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        raise ValueError("fact cache number is non-finite")
    return number


def _frame_values(snapshot: Mapping[str, Any] | None, name: str, row: str, count: int) -> list[float | None]:
    if snapshot is None or not isinstance(snapshot.get(name), pd.DataFrame):
        return [None] * count
    frame = snapshot[name]
    if row not in frame.index:
        return [None] * count
    values = [_number(value) for value in frame.loc[row].sort_index(ascending=False).tolist()]
    return (values + [None] * count)[:count]


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return _number((numerator / denominator) - 1.0) if numerator is not None and denominator not in (None, 0.0) else None


def _fundamental_values(snapshot: Mapping[str, Any] | None) -> dict[str, object]:
    eps = _frame_values(snapshot, "quarterly_income", "Diluted EPS", 5)
    sales = _frame_values(snapshot, "quarterly_income", "Total Revenue", 5)
    annual = _frame_values(snapshot, "annual_income", "Diluted EPS", 4)
    net_income = _frame_values(snapshot, "balance_sheet", "Net Income", 1)[0]
    equity = _frame_values(snapshot, "balance_sheet", "Total Stockholders Equity", 1)[0]
    info = {} if snapshot is None else snapshot.get("company_info", {})
    shares = _number(info.get("shares_outstanding")) if isinstance(info, Mapping) else None
    return {
        "current_eps": eps[0], "prior_year_eps": eps[4], "current_sales": sales[0], "prior_year_sales": sales[4],
        "annual_eps_1": annual[0], "annual_eps_2": annual[1], "annual_eps_3": annual[2], "annual_eps_4": annual[3],
        "net_income": net_income, "total_stockholders_equity": equity, "current_eps_yoy": _ratio(eps[0], eps[4]), "sales_yoy": _ratio(sales[0], sales[4]),
        "roe": _number(net_income / equity) if net_income is not None and equity not in (None, 0.0) else None, "shares_outstanding": shares,
    }


def _validate_supplemental(institutional: InstitutionalSnapshot, industry: IndustryGroupSnapshot, session: str) -> None:
    if not isinstance(institutional, InstitutionalSnapshot) or not isinstance(industry, IndustryGroupSnapshot):
        raise ValueError("supplemental provider returned an invalid snapshot")
    for as_of in (institutional.as_of_date, industry.as_of_date):
        if as_of is not None and as_of > session:
            raise ValueError("supplemental snapshot is future-dated")


def _json_tuple(values: tuple[str, ...]) -> str:
    return json.dumps(values, separators=(",", ":"), ensure_ascii=False)


def _ensure_finite_row(row: Mapping[str, object]) -> None:
    for name, value in row.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"fact cache row has non-finite value: {name}")


def _market_facts(spy: pd.DataFrame | None, session: str) -> dict[str, object]:
    if spy is None or spy.empty or pd.Timestamp(session) not in spy.index:
        return {"market_regime": "unavailable", "distribution_count": None, "follow_through_session": None}
    history = spy.loc[spy.index <= pd.Timestamp(session)]
    close = _number(history["Close"].iloc[-1])
    mean_50 = _number(history["Close"].tail(50).mean()) if len(history) >= 50 else None
    regime = "uptrend" if close is not None and mean_50 is not None and close >= mean_50 else "downtrend" if mean_50 is not None else "unavailable"
    prior = history.iloc[:-1]
    if prior.empty:
        distribution = 0
    else:
        declines = (history["Close"].pct_change() <= -0.002) & (history["Volume"] > history["Volume"].shift(1))
        distribution = int(declines.tail(25).sum())
    return {"market_regime": regime, "distribution_count": distribution, "follow_through_session": None}
