"""Normalize one immutable S&P 500 Wikipedia revision into PIT classifications.

This utility is intentionally offline.  It accepts a local JSON or HTML export
of one immutable MediaWiki revision and writes the four-column classification
CSV consumed by :mod:`tools.build_pit_industry`::

    symbol,as_of_date,group_id,evidence_ids

The revision must carry a numeric revision ID and a timezone-aware timestamp.
The output date is the first supplied ``trade_date`` strictly after that
timestamp.  A local trading-session map is therefore mandatory; the normalizer
never guesses a session calendar or falls back to the same/previous session.

JSON exports may use the compact contract ``{"revid": ..., "timestamp":
..., "rows": [...]}``, where each row has ``Symbol``, ``GICS Sub-Industry``,
and optional ``CIK`` fields.  A MediaWiki API-shaped object containing one
revision and either ``rows`` or an HTML ``content``/``html`` value is also
accepted.  HTML exports must carry revision metadata in ``meta`` tags (or a
canonical ``oldid`` URL) and contain a table with the same symbol and GICS
sub-industry columns.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import io
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qs, urlparse
from html.parser import HTMLParser

import pandas as pd

from core.public_membership import canonical_ticker


_CLASSIFICATION_FIELDS = ("symbol", "as_of_date", "group_id", "evidence_ids")
_SESSION_FIELDS = ("trade_date",)
_MEMBERSHIP_FIELDS = ("effective_date", "ticker", "member")
_MAX_ROWS = 10_000
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,7}\Z")
_CIK_RE = re.compile(r"[0-9]{1,10}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class RevisionMetadata:
    revid: int
    timestamp: datetime


@dataclass(frozen=True)
class RevisionClassification:
    symbol: str
    group_id: str
    cik: str | None = None


@dataclass(frozen=True)
class NormalizedIndustryResult:
    output: Path
    revid: int
    revision_timestamp: str
    as_of_date: str
    rows: int


@dataclass(frozen=True)
class _MembershipEvent:
    effective_date: str
    ticker: str
    member: bool


def _regular_file(path: str | Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    return candidate


def _new_output(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError("output already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _text(value: object, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    try:
        if bool(pd.isna(value)):
            raise ValueError(f"{field} is required")
    except (TypeError, ValueError):
        # Lists/arrays are not valid scalar fields; the following conversion
        # will make the error explicit and deterministic.
        if not isinstance(value, str):
            raise ValueError(f"{field} must be scalar text") from None
    result = str(value).replace("\xa0", " ")
    if not result or not result.strip() or _CONTROL.search(result):
        raise ValueError(f"{field} must be non-empty text without control characters")
    return result.strip()


def _label(value: object) -> str:
    if isinstance(value, tuple):
        parts = [str(part) for part in value if str(part).casefold() != "nan"]
        result = " ".join(parts)
        if not result.strip():
            raise ValueError("column must be non-empty text")
    else:
        result = _text(value, "column")
    return re.sub(r"\s+", " ", result).strip().casefold()


def _column(mapping: Mapping[object, object], *names: str) -> object | None:
    labels = {_label(key): key for key in mapping}
    for name in names:
        if name.casefold() in labels:
            return labels[name.casefold()]
    return None


def _canonical_symbol(value: object, field: str = "symbol") -> str:
    raw = _text(value, field)
    if raw != raw.upper():
        raise ValueError(f"{field} must be uppercase and trimmed")
    if _SYMBOL_RE.fullmatch(raw) is None:
        raise ValueError(f"{field} is not a canonical PIT symbol")
    try:
        # This only applies the two reviewed share-class aliases already used
        # by the PIT membership boundary (BRK.B/BF.B).  Unknown punctuation is
        # rejected by canonical_ticker rather than guessed.
        return canonical_ticker(raw)
    except ValueError as exc:
        raise ValueError(f"{field} is not a reviewed canonical PIT symbol") from exc


def _canonical_group(value: object) -> str:
    raw = _text(value, "GICS Sub-Industry")
    cleaned = re.sub(r"\[[^\]]*\]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError("GICS Sub-Industry must be non-empty")
    if _CONTROL.search(cleaned):
        raise ValueError("GICS Sub-Industry contains control characters")
    return f"gics-subindustry:{cleaned}"


def _canonical_cik(value: object) -> str | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    if _CIK_RE.fullmatch(raw) is None:
        raise ValueError("CIK must be one through ten decimal digits")
    return raw.zfill(10)


def _parse_timestamp(value: object) -> datetime:
    raw = _text(value, "revision timestamp")
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("revision timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("revision timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_revid(value: object) -> int:
    raw = _text(value, "revision ID")
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("revision ID must be a positive integer")
    return int(raw)


def _metadata_from_mapping(mapping: Mapping[object, object]) -> RevisionMetadata | None:
    id_keys = ("revid", "revision_id", "revisionId", "revision-id")
    timestamp_keys = ("timestamp", "revision_timestamp", "revisionTimestamp", "revision-timestamp")
    revision_id = next((mapping[key] for key in id_keys if key in mapping), None)
    timestamp = next((mapping[key] for key in timestamp_keys if key in mapping), None)
    if revision_id is None or timestamp is None:
        return None
    return RevisionMetadata(_parse_revid(revision_id), _parse_timestamp(timestamp))


def _single_api_revision(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    query = payload.get("query")
    if not isinstance(query, Mapping):
        return None
    pages = query.get("pages")
    if isinstance(pages, Mapping):
        values = list(pages.values())
    elif isinstance(pages, list):
        values = pages
    else:
        return None
    if len(values) != 1 or not isinstance(values[0], Mapping):
        raise ValueError("revision JSON must contain exactly one page")
    revisions = values[0].get("revisions")
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], Mapping):
        raise ValueError("revision JSON must contain exactly one revision")
    return revisions[0]


def _json_content(mapping: Mapping[object, object]) -> str | None:
    for key in ("html", "content", "wikitext"):
        value = mapping.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for nested_key in ("*", "content", "main"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    return nested
                if isinstance(nested, Mapping) and isinstance(nested.get("content"), str):
                    return str(nested["content"])
    return None


def _json_rows(mapping: Mapping[object, object]) -> list[Mapping[object, object]] | None:
    for key in ("rows", "table_rows", "classifications"):
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
            raise ValueError("revision JSON rows must be a list of objects")
        return list(value)
    return None


class _HTMLMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.revision_ids: list[str] = []
        self.timestamps: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() == "meta":
            key = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content")
            if content is not None:
                if key in {"revid", "revision-id", "mw:pagerevisionid", "mw:revisionid"}:
                    self.revision_ids.append(content)
                if key in {"timestamp", "revision-timestamp", "dc:modified", "article:modified_time"}:
                    self.timestamps.append(content)
        if tag.casefold() == "link" and values.get("rel", "").casefold() == "canonical":
            href = values.get("href")
            if href:
                oldids = parse_qs(urlparse(href).query).get("oldid", [])
                self.revision_ids.extend(oldids)


def _html_metadata(raw: str) -> RevisionMetadata:
    parser = _HTMLMetadataParser()
    try:
        parser.feed(raw)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise ValueError("revision HTML metadata is malformed") from exc
    ids = tuple(dict.fromkeys(parser.revision_ids))
    timestamps = tuple(dict.fromkeys(parser.timestamps))
    if len(ids) != 1 or len(timestamps) != 1:
        raise ValueError("revision HTML must contain exactly one revid and timestamp")
    return RevisionMetadata(_parse_revid(ids[0]), _parse_timestamp(timestamps[0]))


def _find_table_rows(raw_html: str) -> list[Mapping[object, object]]:
    try:
        tables = pd.read_html(io.StringIO(raw_html))
    except (OSError, ValueError) as exc:
        raise ValueError("revision HTML does not contain a readable table") from exc
    for table in tables:
        columns = {_label(column) for column in table.columns}
        if {"symbol", "gics sub-industry"}.issubset(columns) or {"symbol", "gics sub industry"}.issubset(columns):
            return [dict(row) for row in table.to_dict("records")]
    raise ValueError("revision HTML table must contain Symbol and GICS Sub-Industry")


def _parse_revision_file(path: Path) -> tuple[RevisionMetadata, list[Mapping[object, object]]]:
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("revision export must be UTF-8") from exc
    if path.suffix.casefold() == ".json" or decoded.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise ValueError("revision JSON is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("revision JSON must contain an object")
        revision = _single_api_revision(payload)
        source = revision or payload
        metadata = _metadata_from_mapping(source)
        if metadata is None and source is not payload:
            metadata = _metadata_from_mapping(payload)
        if metadata is None:
            raise ValueError("revision JSON must contain revid and timestamp")
        rows = _json_rows(source) or _json_rows(payload)
        if rows is None:
            content = _json_content(source) or _json_content(payload)
            if content is None:
                raise ValueError("revision JSON must contain rows or HTML content")
            rows = _find_table_rows(content)
        return metadata, rows
    return _html_metadata(decoded), _find_table_rows(decoded)


def _normalize_rows(rows: Iterable[Mapping[object, object]]) -> tuple[RevisionClassification, ...]:
    normalized: list[RevisionClassification] = []
    seen: set[str] = set()
    for row in rows:
        symbol_key = _column(row, "symbol", "ticker")
        group_key = _column(row, "gics sub-industry", "gics sub industry", "sub-industry", "sub industry")
        cik_key = _column(row, "cik")
        if symbol_key is None or group_key is None:
            raise ValueError("revision row must contain Symbol and GICS Sub-Industry")
        symbol = _canonical_symbol(row[symbol_key])
        if symbol in seen:
            raise ValueError(f"duplicate or ambiguous revision symbol: {symbol}")
        seen.add(symbol)
        cik = _canonical_cik(row[cik_key]) if cik_key is not None else None
        normalized.append(RevisionClassification(symbol, _canonical_group(row[group_key]), cik))
    if not normalized:
        raise ValueError("revision table is empty")
    return tuple(sorted(normalized, key=lambda row: row.symbol))


def _read_sessions(path: Path, *, max_rows: int = _MAX_ROWS) -> tuple[str, ...]:
    sessions: list[str] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != _SESSION_FIELDS:
                raise ValueError("session CSV header must be exactly trade_date")
            for row in reader:
                if row.get(None) is not None or any(value is None for value in row.values()):
                    raise ValueError(f"session CSV row {reader.line_num} has the wrong number of fields")
                if len(sessions) >= max_rows:
                    raise ValueError(f"session CSV exceeds max_rows={max_rows}")
                sessions.append(_iso_date(row["trade_date"], "trade_date"))
    except UnicodeDecodeError as exc:
        raise ValueError("session CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"session CSV is malformed: {exc}") from exc
    if not sessions or len(set(sessions)) != len(sessions):
        raise ValueError("session CSV must contain unique non-empty dates")
    return tuple(sorted(sessions))


def _read_membership(path: Path) -> tuple[_MembershipEvent, ...]:
    events: list[_MembershipEvent] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != _MEMBERSHIP_FIELDS:
                raise ValueError("membership CSV header must be exactly effective_date,ticker,member")
            for row in reader:
                if row.get(None) is not None or any(value is None for value in row.values()):
                    raise ValueError(f"membership CSV row {reader.line_num} has the wrong number of fields")
                effective = _iso_date(row["effective_date"], "membership effective_date")
                ticker = _canonical_symbol(row["ticker"], "membership ticker")
                if row["member"] not in {"0", "1"}:
                    raise ValueError("membership member must be 0 or 1")
                events.append(_MembershipEvent(effective, ticker, row["member"] == "1"))
    except UnicodeDecodeError as exc:
        raise ValueError("membership CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"membership CSV is malformed: {exc}") from exc
    ordered = tuple(sorted(events, key=lambda event: (event.effective_date, event.ticker)))
    if not ordered:
        raise ValueError("membership CSV is empty")
    seen: set[tuple[str, str]] = set()
    state: dict[str, bool] = {}
    for event in ordered:
        key = (event.effective_date, event.ticker)
        if key in seen:
            raise ValueError(f"duplicate membership transition: {key}")
        seen.add(key)
        if event.ticker in state and state[event.ticker] == event.member:
            raise ValueError(f"membership transition is not a state change: {event.ticker}")
        state[event.ticker] = event.member
    return ordered


def _active_members(events: tuple[_MembershipEvent, ...], as_of_date: str) -> frozenset[str]:
    active: set[str] = set()
    for event in events:
        if event.effective_date > as_of_date:
            break
        if event.member:
            active.add(event.ticker)
        else:
            active.discard(event.ticker)
    return frozenset(active)


def _first_session_after(timestamp: datetime, sessions: tuple[str, ...]) -> str:
    for session in sessions:
        session_instant = datetime.combine(date.fromisoformat(session), time.min, tzinfo=timezone.utc)
        if session_instant > timestamp:
            return session
    raise ValueError("revision timestamp has no later supplied trading session")


def _write_output(path: Path, rows: Iterable[Mapping[str, str]]) -> int:
    output_rows = tuple(rows)
    if not output_rows:
        raise ValueError("normalized classification output is empty")
    partial = Path(f"{path}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial normalized output already exists")
    try:
        with partial.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=_CLASSIFICATION_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(output_rows)
        partial.replace(path)
        return len(output_rows)
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def normalize_revision(
    *,
    revision_export: str | Path,
    sessions: Iterable[str],
    output: str | Path,
    membership_csv: str | Path | None = None,
) -> NormalizedIndustryResult:
    """Normalize one local revision export into the ranker's classification CSV."""

    source_path = _regular_file(revision_export, "revision_export")
    output_path = _new_output(output)
    metadata, raw_rows = _parse_revision_file(source_path)
    normalized = _normalize_rows(raw_rows)
    session_values = tuple(sorted({_iso_date(value, "trade_date") for value in sessions}))
    if not session_values:
        raise ValueError("sessions must contain at least one unique date")
    as_of_date = _first_session_after(metadata.timestamp, session_values)
    membership = None if membership_csv is None else _read_membership(_regular_file(membership_csv, "membership_csv"))
    symbols = {row.symbol for row in normalized}
    if membership is not None:
        active = set(_active_members(membership, as_of_date))
        missing = sorted(active - symbols)
        extra = sorted(symbols - active)
        if missing or extra:
            raise ValueError(
                f"revision symbols do not exactly match PIT membership on {as_of_date}; "
                f"missing={missing[:5]} extra={extra[:5]}"
            )
    evidence = json.dumps((f"wikipedia:revid:{metadata.revid}",), separators=(",", ":"))
    output_rows = tuple(
        {
            "symbol": row.symbol,
            "as_of_date": as_of_date,
            "group_id": row.group_id,
            "evidence_ids": evidence,
        }
        for row in normalized
    )
    try:
        count = _write_output(output_path, output_rows)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return NormalizedIndustryResult(
        output=output_path,
        revid=metadata.revid,
        revision_timestamp=metadata.timestamp.isoformat().replace("+00:00", "Z"),
        as_of_date=as_of_date,
        rows=count,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision-export", type=Path, required=True)
    parser.add_argument("--sessions-csv", type=Path, required=True)
    parser.add_argument("--membership-csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sessions = _read_sessions(_regular_file(args.sessions_csv, "sessions_csv"))
    result = normalize_revision(
        revision_export=args.revision_export,
        sessions=sessions,
        membership_csv=args.membership_csv,
        output=args.output,
    )
    print(
        json.dumps(
            {
                "as_of_date": result.as_of_date,
                "output": str(result.output),
                "revid": result.revid,
                "revision_timestamp": result.revision_timestamp,
                "rows": result.rows,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
