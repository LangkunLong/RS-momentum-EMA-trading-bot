"""Industry group RS ranking for O'Neil-style group-strength filtering."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core.data_client import fetch_company_profile

logger = logging.getLogger(__name__)


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
    are omitted from the returned map. Cache TTL is 7 days.

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
    if missing:
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
