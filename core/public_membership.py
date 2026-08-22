"""Acquire a reproducible S&P 500 membership-event export from a pinned page.

The public table is an acquisition input, not a live-trading dependency.  This
module deliberately keeps its parsing and symbol normalization conservative:
an unfamiliar punctuation conversion is an error that requires review.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import pandas as pd

from core.leader_evaluation import MembershipEvent

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_USER_AGENT = "canslim-pit-membership/1.0 (+https://github.com/)"
_ALIASES = {"BRK.B": "BRK-B", "BF.B": "BF-B"}
_CANONICAL_TICKER = re.compile(r"^[A-Z][A-Z0-9-]{0,7}$")
_MAP_HEADER = ("source_ticker", "canonical_ticker", "effective_start", "effective_end", "reason")


@dataclass(frozen=True)
class MembershipChange:
    effective_date: date
    added_ticker: str | None
    removed_ticker: str | None
    added_company: str | None
    removed_company: str | None


@dataclass(frozen=True)
class MembershipExport:
    seed_date: date
    events: tuple[MembershipEvent, ...]
    company_names: Mapping[str, str]
    source_sha256: str
    source_url: str
    revision_id: str
    exclusions: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class _ReviewedMapping:
    canonical_ticker: str
    effective_start: date
    effective_end: date


class _SameHostRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            raise HTTPError(req.full_url, code, "redirect changed host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _revision_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("revision URL must be HTTPS")
    values = parse_qs(parsed.query).get("oldid", [])
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
        raise ValueError("revision URL must include a numeric immutable oldid")
    return values[0]


def fetch_revision(url: str, *, timeout_seconds: float = 30.0) -> bytes:
    """Fetch one immutable HTTPS page revision without cross-host redirects."""
    _revision_id(url)
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 300:
        raise ValueError("timeout_seconds must be between zero and 300")
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    opener = build_opener(_SameHostRedirects())
    with opener.open(request, timeout=float(timeout_seconds)) as response:
        if urlparse(response.geturl()).hostname != urlparse(url).hostname:
            raise ValueError("redirect changed host")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_RESPONSE_BYTES:
            raise ValueError("revision response exceeds 10 MiB cap")
        chunks: list[bytes] = []
        received = 0
        while chunk := response.read(64 * 1024):
            received += len(chunk)
            if received > _MAX_RESPONSE_BYTES:
                raise ValueError("revision response exceeds 10 MiB cap")
            chunks.append(chunk)
    return b"".join(chunks)


def canonical_ticker(
    source_ticker: object,
    *,
    mappings: Mapping[str, tuple[_ReviewedMapping, ...]] | None = None,
    when: date | None = None,
) -> str:
    """Return a known canonical ticker, refusing unreviewed punctuation changes."""
    if not isinstance(source_ticker, str):
        raise ValueError("source ticker must be text")
    source = source_ticker.strip().upper()
    if mappings and source in mappings:
        if when is None:
            raise ValueError(f"reviewed mapping needs an effective date: {source_ticker!r}")
        matches = [item for item in mappings[source] if item.effective_start <= when <= item.effective_end]
        if len(matches) != 1:
            raise ValueError(f"no unique reviewed mapping at {when}: {source_ticker!r}")
        source = matches[0].canonical_ticker
    canonical = _ALIASES.get(source, source)
    if "." in source and source not in _ALIASES:
        raise ValueError(f"unreviewed punctuation alias: {source_ticker!r}")
    if not _CANONICAL_TICKER.fullmatch(canonical):
        raise ValueError(f"invalid canonical ticker: {source_ticker!r}")
    return canonical


def load_symbol_map(path: Path | None) -> Mapping[str, tuple[_ReviewedMapping, ...]]:
    """Load a reviewed, date-bounded source-symbol mapping CSV."""
    if path is None:
        return {}
    if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
        raise ValueError("symbol map CSV must be a regular file")
    result: dict[str, list[_ReviewedMapping]] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != _MAP_HEADER:
            raise ValueError("symbol map CSV header is invalid")
        for row_number, row in enumerate(reader, start=2):
            try:
                start = date.fromisoformat(str(row["effective_start"]).strip())
                end = date.fromisoformat(str(row["effective_end"]).strip())
            except ValueError as exc:
                raise ValueError(f"symbol map row {row_number} has invalid dates") from exc
            if end < start:
                raise ValueError(f"symbol map row {row_number} has an invalid date range")
            source = str(row["source_ticker"]).strip().upper()
            canonical = canonical_ticker(str(row["canonical_ticker"]))
            if not source or not str(row["reason"]).strip():
                raise ValueError(f"symbol map row {row_number} is invalid")
            result.setdefault(source, []).append(_ReviewedMapping(canonical, start, end))
    normalized: dict[str, tuple[_ReviewedMapping, ...]] = {}
    for source, entries in result.items():
        ordered = tuple(sorted(entries, key=lambda item: item.effective_start))
        if any(
            previous.effective_end >= current.effective_start
            for previous, current in zip(ordered, ordered[1:], strict=False)
        ):
            raise ValueError(f"symbol map contains overlapping ranges for {source}")
        normalized[source] = ordered
    return normalized


def _label(column: object) -> str:
    if isinstance(column, tuple):
        column = " ".join(str(part) for part in column if str(part).lower() != "nan")
    return re.sub(r"\s+", " ", str(column).replace("\n", " ")).strip().lower()


def _text(value: object) -> str | None:
    if pd.isna(value):
        return None
    normalized = re.sub(r"\[[^]]*\]", "", str(value)).strip()
    return normalized or None


def _find_column(frame: pd.DataFrame, *candidates: str) -> object | None:
    labels = {_label(column): column for column in frame.columns}
    for candidate in candidates:
        if candidate in labels:
            return labels[candidate]
    return None


def _constituents_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    for table in tables:
        if _find_column(table, "symbol", "ticker") is not None and _find_column(table, "security", "company") is not None:
            return table
    raise ValueError("could not locate current constituents table")


def _changes_table(tables: list[pd.DataFrame]) -> pd.DataFrame:
    for table in tables:
        if _find_column(table, "date", "effective date", "effective date effective date") is None:
            continue
        labels = {_label(column) for column in table.columns}
        if {"added ticker", "removed ticker"}.issubset(labels):
            return table
        # pandas versions that flatten a two-tier heading differently.
        if {"added", "removed"}.issubset(labels):
            return table
    raise ValueError("could not locate dated changes table")


def _parse_date(value: object) -> date:
    try:
        parsed = pd.to_datetime(_text(value), errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid change effective date: {value!r}") from exc
    return parsed.date()


def _change_columns(table: pd.DataFrame) -> tuple[object, object | None, object | None, object | None, object | None]:
    date_column = _find_column(table, "date", "effective date", "effective date effective date")
    assert date_column is not None
    added_ticker = _find_column(table, "added ticker", "added")
    removed_ticker = _find_column(table, "removed ticker", "removed")
    added_company = _find_column(table, "added security", "added company", "added.1")
    removed_company = _find_column(table, "removed security", "removed company", "removed.1")
    if added_ticker is None or removed_ticker is None:
        raise ValueError("changes table does not expose added and removed tickers")
    return date_column, added_ticker, removed_ticker, added_company, removed_company


def _parse_page(raw: bytes, *, mappings: Mapping[str, tuple[_ReviewedMapping, ...]]) -> tuple[frozenset[str], dict[str, str], tuple[MembershipChange, ...]]:
    try:
        tables = pd.read_html(io.BytesIO(raw))
    except (ValueError, OSError) as exc:
        raise ValueError("could not parse membership page HTML") from exc
    constituents = _constituents_table(tables)
    symbol_column = _find_column(constituents, "symbol", "ticker")
    company_column = _find_column(constituents, "security", "company")
    assert symbol_column is not None and company_column is not None
    current_names: dict[str, str] = {}
    for row in constituents.itertuples(index=False):
        source = row[constituents.columns.get_loc(symbol_column)]
        company = _text(row[constituents.columns.get_loc(company_column)])
        if company is None:
            raise ValueError("constituent company name is missing")
        ticker = canonical_ticker(source)
        if ticker in current_names:
            raise ValueError(f"duplicate current constituent ticker: {ticker}")
        current_names[ticker] = company

    names = dict(current_names)

    changes_frame = _changes_table(tables)
    date_column, added_column, removed_column, added_company_column, removed_company_column = _change_columns(changes_frame)
    changes: list[MembershipChange] = []
    previous_date: date | None = None
    for row in changes_frame.to_dict("records"):
        raw_date = _text(row[date_column])
        effective = _parse_date(raw_date) if raw_date is not None else previous_date
        if effective is None:
            raise ValueError("first change row has no effective date")
        previous_date = effective
        added_source = _text(row[added_column])
        removed_source = _text(row[removed_column])
        added = canonical_ticker(added_source, mappings=mappings, when=effective) if added_source else None
        removed = canonical_ticker(removed_source, mappings=mappings, when=effective) if removed_source else None
        if added is None and removed is None:
            continue
        added_company = _text(row[added_company_column]) if added_company_column is not None else None
        removed_company = _text(row[removed_company_column]) if removed_company_column is not None else None
        if added and added_company:
            names.setdefault(added, added_company)
        if removed and removed_company:
            names.setdefault(removed, removed_company)
        changes.append(MembershipChange(effective, added, removed, added_company, removed_company))
    if not names or not changes:
        raise ValueError("membership page has no constituents or dated changes")
    return frozenset(current_names), names, tuple(changes)


def _normalize(
    raw: bytes,
    revision_url: str,
    start_date: date,
    end_date: date,
    *,
    mappings: Mapping[str, tuple[_ReviewedMapping, ...]],
) -> MembershipExport:
    if not isinstance(start_date, date) or not isinstance(end_date, date) or end_date < start_date:
        raise ValueError("date range is invalid")
    revision_id = _revision_id(revision_url)
    current_tickers, names, changes = _parse_page(raw, mappings=mappings)
    state = set(current_tickers)
    exclusions: list[Mapping[str, str]] = []
    for change in sorted((item for item in changes if item.effective_date > start_date), key=lambda item: item.effective_date, reverse=True):
        if change.added_ticker:
            if change.added_ticker not in state:
                exclusions.append({"effective_date": change.effective_date.isoformat(), "ticker": change.added_ticker, "reason": "reverse addition was not active"})
            state.discard(change.added_ticker)
        if change.removed_ticker:
            if change.removed_ticker in state:
                exclusions.append({"effective_date": change.effective_date.isoformat(), "ticker": change.removed_ticker, "reason": "reverse removal was already active"})
            state.add(change.removed_ticker)
    if exclusions:
        raise ValueError("unresolvable historical membership transition; provide a reviewed symbol map CSV")

    events = [MembershipEvent(start_date, ticker, True) for ticker in sorted(state)]
    active = set(state)
    for change in sorted((item for item in changes if start_date < item.effective_date <= end_date), key=lambda item: item.effective_date):
        for ticker, member in ((change.added_ticker, True), (change.removed_ticker, False)):
            if ticker is None:
                continue
            if member and ticker in active:
                raise ValueError(f"addition already active at {change.effective_date}: {ticker}")
            if not member and ticker not in active:
                raise ValueError(f"removal not active at {change.effective_date}: {ticker}")
            active.add(ticker) if member else active.remove(ticker)
            events.append(MembershipEvent(change.effective_date, ticker, member))
    events.sort(key=lambda item: (item.effective_date, item.ticker))
    if len({(event.effective_date, event.ticker) for event in events}) != len(events):
        raise ValueError("multiple same-day transitions require a reviewed mapping CSV")
    return MembershipExport(
        seed_date=start_date,
        events=tuple(events),
        company_names=dict(sorted(names.items())),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_url=revision_url,
        revision_id=revision_id,
        exclusions=tuple(exclusions),
    )


def fetch_membership(revision_url: str, start_date: date, end_date: date) -> MembershipExport:
    """Fetch and normalize one pinned public membership-page revision."""
    return _normalize(fetch_revision(revision_url), revision_url, start_date, end_date, mappings={})
