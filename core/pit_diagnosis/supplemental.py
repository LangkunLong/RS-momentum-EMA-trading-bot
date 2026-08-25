"""Point-in-time supplemental evidence contracts for diagnosis fact caches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Protocol

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ZERO_DIGEST = "0" * 64
_SUPPLEMENTAL_SCHEMA_VERSION = "1"
_SUPPLEMENTAL_METADATA_KEYS = frozenset({"schema_version", "source_kind", "data_cutoff", "provenance_sha256"})
_INSTITUTIONAL_TABLE = "institutional_snapshots"
_INDUSTRY_TABLE = "industry_group_snapshots"
_SUPPLEMENTAL_TABLES = frozenset({"metadata", _INSTITUTIONAL_TABLE, _INDUSTRY_TABLE})
_INSTITUTIONAL_COLUMNS = (
    "symbol", "as_of_date", "ownership_percent", "holder_count", "previous_holder_count", "evidence_ids",
)
_INDUSTRY_COLUMNS = (
    "symbol", "as_of_date", "group_id", "group_rank", "group_members", "evidence_ids",
)


def _date_or_none(value: str | None, field: str) -> None:
    if value is not None:
        from datetime import date

        try:
            date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO date or None") from exc


def _finite_or_none(value: float | None, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
        raise ValueError(f"{field} must be finite or None")


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _symbol(value: object, field: str = "symbol") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty symbol")
    normalized = value.strip().upper()
    if normalized != value:
        raise ValueError(f"{field} must be uppercase and trimmed")
    return normalized


def _digest(value: object, field: str, *, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    if not allow_zero and value == _ZERO_DIGEST:
        raise ValueError(f"{field} must be nonzero")
    return value


def _json_strings(value: object, field: str, *, require_nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON array")
    try:
        parsed = json.loads(value, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be a JSON array") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item for item in parsed):
        raise ValueError(f"{field} must contain non-empty strings")
    if require_nonempty and not parsed:
        raise ValueError(f"{field} must not be empty")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(parsed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValueError("supplemental input cannot be read") from exc
    return digest.hexdigest()


@dataclass(frozen=True)
class InstitutionalSnapshot:
    """Institutional ownership facts available at one exact as-of date."""

    as_of_date: str | None
    ownership_percent: float | None
    holder_count: int | None
    previous_holder_count: int | None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _date_or_none(self.as_of_date, "as_of_date")
        _finite_or_none(self.ownership_percent, "ownership_percent")
        for field in ("holder_count", "previous_holder_count"):
            value = getattr(self, field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field} must be a non-negative integer or None")
        if not isinstance(self.evidence_ids, tuple) or any(not isinstance(item, str) or not item for item in self.evidence_ids):
            raise ValueError("evidence_ids must be a tuple of non-empty strings")
        values = (self.ownership_percent, self.holder_count, self.previous_holder_count)
        if self.as_of_date is None:
            if any(value is not None for value in values) or self.evidence_ids:
                raise ValueError("unavailable institutional snapshots must not contain evidence")
        elif any(value is None for value in values) or not self.evidence_ids:
            raise ValueError("available institutional snapshots require ownership, holder counts, and evidence")

    @property
    def available(self) -> bool:
        return self.as_of_date is not None


@dataclass(frozen=True)
class IndustryGroupSnapshot:
    """Industry membership/rank facts available at one exact as-of date."""

    as_of_date: str | None
    group_id: str | None
    group_rank: int | None
    group_members: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _date_or_none(self.as_of_date, "as_of_date")
        if self.group_id is not None and (not isinstance(self.group_id, str) or not self.group_id):
            raise ValueError("group_id must be a non-empty string or None")
        if self.group_rank is not None and (type(self.group_rank) is not int or self.group_rank <= 0):
            raise ValueError("group_rank must be a positive integer or None")
        for field in ("group_members", "evidence_ids"):
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{field} must be a tuple of non-empty strings")
        if self.as_of_date is None:
            if self.group_id is not None or self.group_rank is not None or self.group_members or self.evidence_ids:
                raise ValueError("unavailable industry snapshots must not contain evidence")
        elif self.group_id is None or self.group_rank is None or not self.group_members or not self.evidence_ids:
            raise ValueError("available industry snapshots require group membership, rank, and evidence")

    @property
    def available(self) -> bool:
        return self.as_of_date is not None


class SupplementalPITProvider(Protocol):
    """Offline-only supplemental facts with a stable content identity."""

    @property
    def content_identity_sha256(self) -> str: ...

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot: ...

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot: ...


class SQLiteSupplementalPITProvider:
    """Read-only, hash-pinned supplemental PIT evidence from SQLite.

    The input is intentionally separate from the public PIT bundle: the bundle
    can remain a reproducible SEC/price artifact while optional institutional
    and industry evidence is versioned independently.  A valid input contains
    exactly the metadata and snapshot tables documented by ``_validate`` below.
    No provider or network fallback exists; a missing as-of row is represented
    by the same unavailable snapshots as ``UnavailableSupplementalPITProvider``.
    """

    content_identity_sha256: str

    def __init__(self, path: Path, expected_sha256: str) -> None:
        candidate = Path(path)
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("supplemental input must be a regular file")
        expected = _digest(expected_sha256, "supplemental input SHA-256")
        actual = _sha256_file(candidate)
        if actual != expected:
            raise ValueError("supplemental input SHA-256 does not match the expected identity")
        self.path = candidate.resolve()
        self.content_identity_sha256 = actual
        try:
            self._connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            self._connection.row_factory = sqlite3.Row
            self._validate()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "SQLiteSupplementalPITProvider":
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot:
        row = self._latest(_INSTITUTIONAL_TABLE, symbol, session)
        if row is None:
            return InstitutionalSnapshot(None, None, None, None)
        return InstitutionalSnapshot(
            str(row["as_of_date"]),
            float(row["ownership_percent"]),
            int(row["holder_count"]),
            int(row["previous_holder_count"]),
            _json_strings(row["evidence_ids"], "institutional evidence_ids"),
        )

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot:
        normalized_symbol = _symbol(symbol)
        row = self._latest(_INDUSTRY_TABLE, normalized_symbol, session)
        if row is None:
            return IndustryGroupSnapshot(None, None, None)
        members = _json_strings(row["group_members"], "industry group_members")
        if normalized_symbol not in members:
            raise ValueError("industry snapshot does not include its symbol in group_members")
        return IndustryGroupSnapshot(
            str(row["as_of_date"]),
            str(row["group_id"]),
            int(row["group_rank"]),
            members,
            _json_strings(row["evidence_ids"], "industry evidence_ids"),
        )

    def _latest(self, table: str, symbol: str, session: str) -> sqlite3.Row | None:
        if table not in {_INSTITUTIONAL_TABLE, _INDUSTRY_TABLE}:
            raise ValueError("unrecognized supplemental table")
        normalized_symbol = _symbol(symbol)
        session_date = _date(session, "session")
        return self._connection.execute(
            f"SELECT * FROM {table} WHERE symbol=? AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1",
            (normalized_symbol, session_date),
        ).fetchone()

    def _validate(self) -> None:
        connection = self._connection
        if connection is None:
            raise ValueError("supplemental provider is closed")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError("supplemental input integrity check failed")
        objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        tables = {name for kind, name in objects if kind == "table"}
        if tables != _SUPPLEMENTAL_TABLES:
            raise ValueError("supplemental input tables are invalid")
        if any(kind not in {"table", "index"} for kind, _name in objects):
            raise ValueError("supplemental input contains unsupported SQLite objects")

        self._validate_table(
            "metadata",
            ("key", "value"),
            {"key": "TEXT", "value": "TEXT"},
            (1,),
            not_null=(0, 1),
        )
        metadata_rows = connection.execute("SELECT key,value FROM metadata").fetchall()
        metadata = {str(row[0]): str(row[1]) for row in metadata_rows}
        if len(metadata) != len(metadata_rows) or set(metadata) != _SUPPLEMENTAL_METADATA_KEYS:
            raise ValueError("supplemental metadata keys are invalid")
        if metadata["schema_version"] != _SUPPLEMENTAL_SCHEMA_VERSION:
            raise ValueError("supplemental schema version is unsupported")
        if not metadata["source_kind"].strip():
            raise ValueError("supplemental source_kind is empty")
        _date(metadata["data_cutoff"], "supplemental data_cutoff")
        _digest(metadata["provenance_sha256"], "supplemental provenance_sha256")

        self._validate_table(
            _INSTITUTIONAL_TABLE,
            _INSTITUTIONAL_COLUMNS,
            {"symbol": "TEXT", "as_of_date": "TEXT", "ownership_percent": "REAL", "holder_count": "INTEGER", "previous_holder_count": "INTEGER", "evidence_ids": "TEXT"},
            (1, 2),
        )
        self._validate_table(
            _INDUSTRY_TABLE,
            _INDUSTRY_COLUMNS,
            {"symbol": "TEXT", "as_of_date": "TEXT", "group_id": "TEXT", "group_rank": "INTEGER", "group_members": "TEXT", "evidence_ids": "TEXT"},
            (1, 2),
        )
        cutoff = metadata["data_cutoff"]
        for row in connection.execute(
            "SELECT symbol,as_of_date,ownership_percent,holder_count,previous_holder_count,evidence_ids FROM institutional_snapshots"
        ):
            symbol = _symbol(row["symbol"])
            as_of = _date(row["as_of_date"], "institutional as_of_date")
            if as_of > cutoff:
                raise ValueError("institutional snapshot is after data_cutoff")
            ownership = row["ownership_percent"]
            _finite_or_none(ownership, "institutional ownership_percent")
            if ownership is None or not 0.0 <= float(ownership) <= 1.0:
                raise ValueError("institutional ownership_percent must be within [0,1]")
            holder_count = row["holder_count"]
            previous_holder_count = row["previous_holder_count"]
            if type(holder_count) is not int or holder_count < 0 or type(previous_holder_count) is not int or previous_holder_count < 0:
                raise ValueError("institutional holder counts must be non-negative integers")
            evidence_ids = _json_strings(row["evidence_ids"], "institutional evidence_ids")
            InstitutionalSnapshot(as_of, float(ownership), holder_count, previous_holder_count, evidence_ids)
        for row in connection.execute(
            "SELECT symbol,as_of_date,group_id,group_rank,group_members,evidence_ids FROM industry_group_snapshots"
        ):
            symbol = _symbol(row["symbol"])
            as_of = _date(row["as_of_date"], "industry as_of_date")
            if as_of > cutoff:
                raise ValueError("industry snapshot is after data_cutoff")
            group_id = row["group_id"]
            if not isinstance(group_id, str) or not group_id.strip():
                raise ValueError("industry group_id must be non-empty")
            rank = row["group_rank"]
            if type(rank) is not int or rank <= 0:
                raise ValueError("industry group_rank must be a positive integer")
            members = _json_strings(row["group_members"], "industry group_members")
            if symbol not in members or any(_symbol(member, "industry group member") != member for member in members):
                raise ValueError("industry group_members must contain uppercase symbols including symbol")
            evidence_ids = _json_strings(row["evidence_ids"], "industry evidence_ids")
            IndustryGroupSnapshot(as_of, group_id.strip(), rank, members, evidence_ids)

    def _validate_table(
        self,
        table: str,
        columns: tuple[str, ...],
        types: dict[str, str],
        primary_key: tuple[int, ...],
        not_null: tuple[int, ...] | None = None,
    ) -> None:
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)
        required_not_null = not_null or tuple(1 for _ in columns)
        expected = tuple(
            (column, types[column], required_not_null[index], primary_key.index(index + 1) + 1 if index + 1 in primary_key else 0)
            for index, column in enumerate(columns)
        )
        if actual != expected:
            raise ValueError(f"supplemental {table} schema is invalid")


class UnavailableSupplementalPITProvider:
    """Explicitly records unavailable I/group evidence; it never fills from live data."""

    content_identity_sha256 = _ZERO_DIGEST

    def institutional_snapshot(self, symbol: str, session: str) -> InstitutionalSnapshot:
        del symbol, session
        return InstitutionalSnapshot(None, None, None, None)

    def industry_group_snapshot(self, symbol: str, session: str) -> IndustryGroupSnapshot:
        del symbol, session
        return IndustryGroupSnapshot(None, None, None)
