"""Industry group RS ranking for O'Neil-style group-strength filtering."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from config import settings
from core.data_client import fetch_company_profile

logger = logging.getLogger(__name__)


class _AuthenticatedBundle(Protocol):
    """Narrow authenticated-bundle surface needed by PIT classification reads."""

    metadata: Mapping[str, str]
    _connection: sqlite3.Connection

    def security_lineage_ids(self) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class PITIndustryAssignment:
    """One classification that was publicly effective by a completed session."""

    as_of_date: date
    group_id: str
    sector_id: str | None = None


def load_pit_industry_assignments_as_of(
    bundle: _AuthenticatedBundle,
    *,
    session: date,
    symbols: Iterable[str],
    allow_schema_v2_development: bool = False,
) -> Mapping[str, PITIndustryAssignment]:
    """Return each requested symbol's latest authenticated classification.

    The schema-V3 bundle stores public/effective classification dates in
    ``industry_group_snapshots.as_of_date``.  This adapter deliberately queries
    only rows on or before ``session`` and selects the latest row per symbol; it
    never consults the current provider/cache used by :func:`load_industry_map`.

    Args:
        bundle: Open, authenticated, query-only PIT bundle.
        session: Completed session whose public information set is requested.
        symbols: Canonical active-union symbols (plus an optional held symbol).
        allow_schema_v2_development: Explicitly permit schema-V2 development
            data, which has no dated classifications and returns no assignments.

    Returns:
        Mapping from symbols with an available assignment to immutable records.

    Raises:
        ValueError: If the request or stored classification row is malformed.
    """
    if type(session) is not date:
        raise ValueError("industry as-of session must be a date")
    if type(allow_schema_v2_development) is not bool:
        raise ValueError("PIT industry development flag must be a bool")
    schema_version = bundle.metadata.get("schema_version")
    if schema_version == "3" and bundle.metadata.get("bundle_stage") == "industry_preparation":
        raise ValueError("industry-preparation bundles cannot supply policy assignments")
    if schema_version != "3" and not (
        allow_schema_v2_development and schema_version == "2"
    ):
        raise ValueError("PIT industry assignments require a schema-V3 bundle")

    requested = frozenset(_pit_symbol(symbol) for symbol in symbols)
    if schema_version == "2":
        if session > date.fromisoformat(bundle.metadata["data_cutoff"]):
            raise ValueError("industry session exceeds the authenticated bundle cutoff")
        # Schema V2 has no authenticated dated classifications. Keep missing
        # facts missing so portfolio exposure remains explicitly unclassified.
        return MappingProxyType({})
    if not requested:
        return MappingProxyType({})

    lineage_reader = getattr(bundle, "security_lineage_ids", None)
    if callable(lineage_reader):
        lineage_by_symbol = lineage_reader()
        missing_lineages = requested.difference(lineage_by_symbol)
        if missing_lineages:
            raise ValueError("requested PIT industry symbols lack authenticated lineages")
    else:
        # Compatibility for narrow unit adapters predating the lineage API.
        lineage_by_symbol = {symbol: symbol for symbol in requested}
    requested_lineages = {lineage_by_symbol[symbol] for symbol in requested}
    try:
        columns = {
            str(row[1])
            for row in bundle._connection.execute(
                "PRAGMA table_info(industry_group_snapshots)"
            ).fetchall()
        }
        sector_column = "sector_id" if "sector_id" in columns else "NULL"
        rows = bundle._connection.execute(
            f"SELECT symbol, as_of_date, group_id, {sector_column} "
            "FROM industry_group_snapshots WHERE as_of_date <= ? "
            "ORDER BY as_of_date, symbol",
            (session.isoformat(),),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise ValueError("schema-V3 bundle has no readable PIT industry snapshots") from exc

    latest_by_lineage: dict[str, PITIndustryAssignment] = {}
    seen_symbol_dates: set[tuple[str, date]] = set()
    for row in rows:
        symbol = _pit_symbol(row[0])
        lineage = lineage_by_symbol.get(symbol)
        if lineage not in requested_lineages:
            continue
        try:
            as_of_date = date.fromisoformat(str(row[1]))
        except (TypeError, ValueError) as exc:
            raise ValueError("PIT industry assignment date is invalid") from exc
        if as_of_date > session:
            raise ValueError("PIT industry assignment is after the requested session")
        key = (symbol, as_of_date)
        if key in seen_symbol_dates:
            raise ValueError("PIT industry assignments are not uniquely ordered")
        seen_symbol_dates.add(key)
        group_id = row[2]
        if (
            not isinstance(group_id, str)
            or not group_id
            or group_id.strip() != group_id
            or any(ord(character) < 32 for character in group_id)
        ):
            raise ValueError("PIT industry group_id is invalid")
        raw_sector = row[3]
        sector_id = _pit_sector(raw_sector)
        assignment = PITIndustryAssignment(as_of_date, group_id, sector_id)
        previous = latest_by_lineage.get(str(lineage))
        if previous is not None and as_of_date < previous.as_of_date:
            continue
        if previous is not None and as_of_date == previous.as_of_date:
            if assignment != previous:
                raise ValueError("PIT industry lineage has ambiguous same-date assignments")
            continue
        latest_by_lineage[str(lineage)] = assignment

    result = {
        symbol: latest_by_lineage[lineage_by_symbol[symbol]]
        for symbol in requested
        if lineage_by_symbol[symbol] in latest_by_lineage
    }
    return MappingProxyType(result)


def _pit_sector(value: object) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or re.fullmatch(r"gics-sector:[a-z0-9]+(?:_[a-z0-9]+)*", value) is None
    ):
        raise ValueError("PIT industry sector_id is invalid")
    return value


def _pit_symbol(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or value.upper() != value:
        raise ValueError("PIT industry symbol must be canonical uppercase text")
    return value


def get_top_groups(
    rs_snapshot: dict[str, float],
    ticker_industry: dict[str, str],
    top_n: int = settings.INDUSTRY_GROUP_TOP_N,
    min_size: int = settings.INDUSTRY_GROUP_MIN_SIZE,
) -> set[str]:
    """Return the top-N industry groups by average RS score.

    Groups with fewer than min_size members are excluded from ranking.
    Tickers absent from ticker_industry are silently ignored.

    Args:
        rs_snapshot: Mapping of ticker → current RS score (0–100).
        ticker_industry: Mapping of ticker → industry label string.
        top_n: Number of top groups to return.
        min_size: Minimum member count for a group to qualify.

    Returns:
        Set of industry label strings that rank in the top N.
    """
    group_scores: dict[str, list[float]] = {}
    for ticker, rs in rs_snapshot.items():
        group = ticker_industry.get(ticker)
        if group is None:
            continue
        group_scores.setdefault(group, []).append(rs)

    ranked = [
        (group, sum(scores) / len(scores))
        for group, scores in group_scores.items()
        if len(scores) >= min_size
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return {group for group, _ in ranked[:top_n]}


def load_industry_map(tickers: list[str]) -> dict[str, str]:
    """Load ticker → industry label map, fetching from FMP and caching to disk.

    Uses profile["industry"] with fallback to profile["sector"]. Tickers with neither
    are omitted from the returned map. Cache TTL is 7 days. Free-plan mode reuses
    fresh cached labels but never spends quota to fetch missing profiles.

    Args:
        tickers: List of ticker symbols to look up.

    Returns:
        Mapping of ticker → industry label string (partial — missing tickers omitted).
    """
    cache_path = Path(settings.INDUSTRY_GROUP_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cached_map: dict[str, str] = {}
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text())
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            age_days = (datetime.now(timezone.utc) - fetched_at.replace(tzinfo=timezone.utc)).days
            if age_days < 7:
                cached_map = payload.get("map", {})
        except Exception:
            cached_map = {}

    missing = [t for t in tickers if t not in cached_map]
    if missing and settings.FMP_PLAN != "free":
        logger.info("Fetching industry labels for %d tickers from FMP", len(missing))
        for sym in missing:
            try:
                profile = fetch_company_profile(sym)
                label = (profile.get("industry") or "").strip() or (profile.get("sector") or "").strip()
                if label:
                    cached_map[sym] = label
            except Exception:
                pass

        cache_path.write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "map": cached_map})
        )

    return {t: cached_map[t] for t in tickers if t in cached_map}
