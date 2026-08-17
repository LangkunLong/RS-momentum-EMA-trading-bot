"""FMP institutional ownership provider.

Encapsulates the /institutional-ownership/symbol-ownership endpoint which
provides quarterly historical snapshots of institutional ownership.  This
endpoint is available on paid FMP plans and gives richer, time-series data
than the current-only /institutional-holder endpoint.

The module is a pure leaf: it has no imports from data_client and receives
the FMP HTTP callable as a parameter (dependency injection), avoiding any
circular dependency.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional


def fetch_institutional_ownership_history(
    symbol: str,
    fmp_get_fn: Callable[..., Any],
    limit: int = 8,
) -> List[dict]:
    """Fetch quarterly institutional ownership snapshots from FMP.

    Calls ``/institutional-ownership/symbol-ownership``.  Each record contains
    the quarter-end date, total number of 13-F filers, and the percentage of
    shares outstanding they hold.  Returns an empty list when the endpoint is
    unavailable (e.g. 402 plan restriction) so callers degrade gracefully.

    Args:
        symbol: Ticker symbol.
        fmp_get_fn: Callable matching ``data_client._fmp_get`` — takes
            ``(endpoint, params)`` and returns a list or empty list.
        limit: Number of quarterly periods to retrieve (default 8 = 2 years).

    Returns:
        List of dicts sorted newest-first.  Each dict contains at minimum:
            date (str), institution_count (int), ownership_percent (float).
        Empty list on any failure.
    """
    raw = fmp_get_fn(
        "institutional-ownership/symbol-ownership",
        {"symbol": symbol, "limit": limit},
    )
    if not isinstance(raw, list) or not raw:
        return []

    result: List[dict] = []
    for rec in raw:
        date_str = rec.get("date")
        if not date_str:
            continue

        entry: dict = {"date": date_str}

        investors = rec.get("investorsHolding")
        if investors is not None:
            entry["institution_count"] = int(investors)

        prev_investors = rec.get("lastInvestorsHolding")
        if prev_investors is not None:
            entry["prev_institution_count"] = int(prev_investors)

        pct = rec.get("ownershipPercent")
        if pct is not None:
            try:
                # FMP returns a percentage (e.g. 59.21); normalise to decimal.
                entry["ownership_percent"] = float(pct)
            except (TypeError, ValueError):
                pass

        prev_pct = rec.get("lastOwnershipPercent")
        if prev_pct is not None:
            try:
                entry["prev_ownership_percent"] = float(prev_pct)
            except (TypeError, ValueError):
                pass

        result.append(entry)

    # Sort newest-first so callers get [0] == most recent quarter.
    result.sort(key=lambda r: r.get("date", ""), reverse=True)
    return result


def company_info_from_inst_history(
    history: List[dict],
    shares_outstanding: Optional[int] = None,
) -> dict:
    """Derive institutional ownership fields from quarterly history.

    Uses the most recent record for the current-snapshot fields and exposes
    the quarter-over-quarter holder count change so the I-component scorer
    can compute a proper trend score.

    Args:
        history: Sorted newest-first list from
            ``fetch_institutional_ownership_history``.
        shares_outstanding: Unused — kept for API symmetry with the legacy
            ``/institutional-holder`` code path that computed ownership from
            raw share counts.

    Returns:
        Dict with keys:
            held_percent_institutions (float 0-1 | None)
            institution_count         (int | None)
            prev_institution_count    (int | None)   ← new field
    """
    if not history:
        return {
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        }

    latest = history[0]

    # Convert percentage to decimal fraction capped at 1.0.
    held_pct: Optional[float] = None
    raw_pct = latest.get("ownership_percent")
    if raw_pct is not None:
        try:
            held_pct = min(float(raw_pct) / 100.0, 1.0)
        except (TypeError, ValueError):
            held_pct = None

    return {
        "held_percent_institutions": held_pct,
        "institution_count": latest.get("institution_count"),
        # prev_institution_count comes from lastInvestorsHolding in the same
        # record — the change within the same snapshot period.
        "prev_institution_count": latest.get("prev_institution_count"),
    }
