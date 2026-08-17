"""FMP company profile and institutional ownership helpers.

Institutional data comes from the current, period-specific
``/institutional-ownership/symbol-positions-summary`` endpoint. FMP's older
``/institutional-ownership/symbol-ownership`` and ``/institutional-holder``
routes are legacy APIs and are not valid beneath the stable base URL.

This module is a pure leaf: it has no imports from ``data_client`` and receives
the FMP HTTP callable as a parameter, avoiding a circular dependency.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, List, Optional


# Form 13F reports are due up to 45 days after quarter end. Five additional
# calendar days keep weekend/holiday deadlines from leaking future data into
# point-in-time backtests.
_INSTITUTIONAL_REPORTING_LAG_DAYS = 50


def _quarter_end(year: int, quarter: int) -> date:
    """Return the calendar quarter-end date."""
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    month, day = month_day[quarter]
    return date(year, month, day)


def _previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    """Return the period immediately before ``year``/``quarter``."""
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def _latest_available_quarter(as_of_date: date) -> tuple[int, int]:
    """Return the latest quarter whose conservative reporting lag elapsed."""
    year = as_of_date.year
    quarter = ((as_of_date.month - 1) // 3) + 1
    period_end = _quarter_end(year, quarter)
    while period_end + timedelta(days=_INSTITUTIONAL_REPORTING_LAG_DAYS) > as_of_date:
        year, quarter = _previous_quarter(year, quarter)
        period_end = _quarter_end(year, quarter)
    return year, quarter


def fetch_company_profile(
    symbol: str,
    fmp_get_fn: Callable[..., Any],
) -> dict[str, str]:
    """Return normalized FMP industry and sector labels for a symbol."""
    raw = fmp_get_fn("profile", {"symbol": symbol})
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return {}

    record = raw[0]
    return {
        key: str(record[key]).strip()
        for key in ("industry", "sector")
        if record.get(key) and str(record[key]).strip()
    }


def fetch_institutional_ownership_history(
    symbol: str,
    fmp_get_fn: Callable[..., Any],
    limit: int = 8,
    as_of_date: date | datetime | None = None,
) -> List[dict]:
    """Fetch normalized quarterly institutional ownership snapshots from FMP.

    The stable Positions Summary API is called once per requested quarter. Its
    response omits dates, so the requested quarter end and a conservative
    assumed public-availability date are added to each record. An empty list is
    returned when the endpoint is unavailable, including plan restrictions.
    """
    if limit <= 0:
        return []

    if as_of_date is None:
        cutoff = date.today()
    elif isinstance(as_of_date, datetime):
        cutoff = as_of_date.date()
    else:
        cutoff = as_of_date

    year, quarter = _latest_available_quarter(cutoff)
    result: List[dict] = []
    for _ in range(limit):
        period_end = _quarter_end(year, quarter)
        assumed_available = period_end + timedelta(days=_INSTITUTIONAL_REPORTING_LAG_DAYS)
        raw = fmp_get_fn(
            "institutional-ownership/symbol-positions-summary",
            {"symbol": symbol, "year": year, "quarter": quarter},
        )
        if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
            break

        record = raw[0]
        entry: dict = {
            "date": period_end.isoformat(),
            "acceptedDate": assumed_available.isoformat(),
        }

        investors = record.get("investorsHolding")
        if investors is not None:
            entry["institution_count"] = int(investors)

        previous_investors = record.get("lastInvestorsHolding")
        if previous_investors is not None:
            entry["prev_institution_count"] = int(previous_investors)

        ownership_percent = record.get("ownershipPercent")
        if ownership_percent is not None:
            try:
                entry["ownership_percent"] = float(ownership_percent)
            except (TypeError, ValueError):
                pass

        result.append(entry)
        year, quarter = _previous_quarter(year, quarter)

    result.sort(key=lambda item: item.get("date", ""), reverse=True)
    return result


def company_info_from_inst_history(
    history: List[dict],
    shares_outstanding: Optional[int] = None,
) -> dict:
    """Derive company-level institutional fields from normalized history."""
    if not history:
        return {
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        }

    latest = history[0]
    held_percent: Optional[float] = None
    raw_percent = latest.get("ownership_percent")
    if raw_percent is not None:
        try:
            held_percent = min(float(raw_percent) / 100.0, 1.0)
        except (TypeError, ValueError):
            held_percent = None

    return {
        "held_percent_institutions": held_percent,
        "institution_count": latest.get("institution_count"),
        "prev_institution_count": latest.get("prev_institution_count"),
    }
