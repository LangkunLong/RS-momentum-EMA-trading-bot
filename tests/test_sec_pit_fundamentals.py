"""Offline accession/public-date regressions for the SEC PIT normalizer."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
from pathlib import Path
import zipfile

import core.sec_pit_fundamentals as sec


def test_companyfacts_accession_dates_are_next_session_and_amendments_are_later(tmp_path: Path) -> None:
    """Break caught: period end or filed date leaked a 10-Q before it was public."""
    payload = {
        "cik": "1",
        "facts": {"us-gaap": {"EarningsPerShareBasic": {"units": {"USD/shares": [
            {"accn": "0000000001-24-000001", "form": "10-Q", "filed": "2024-04-30", "end": "2024-03-31", "start": "2024-01-01", "fy": "2024", "fp": "Q1", "frame": "CY2024Q1", "val": 1.0},
            {"accn": "0000000001-24-000002", "form": "10-Q/A", "filed": "2024-05-10", "end": "2024-03-31", "start": "2024-01-01", "fy": "2024", "fp": "Q1", "frame": "CY2024Q1", "val": 1.1},
            {"accn": "0000000001-24-000003", "form": "10-Q", "filed": "2024-05-13", "end": "2024-03-31", "start": "2024-01-01", "fy": "2024", "fp": "Q1", "frame": "CY2024Q1", "val": 1.2},
        ]}}}},
    }
    archive = tmp_path / "companyfacts.zip"
    with zipfile.ZipFile(archive, "x") as handle:
        handle.writestr("CIK0000000001.json", json.dumps(payload))
    handle, members = sec._zip_members(archive)
    try:
        fact_payload = sec._json_member(handle, members["CIK0000000001.json"])
    finally:
        handle.close()
    acceptances = {
        "000000000124000001": sec.FilingAcceptance("000000000124000001", "10-Q", date(2024, 4, 30), datetime(2024, 4, 30, 22, tzinfo=timezone.utc)),
        "000000000124000002": sec.FilingAcceptance("000000000124000002", "10-Q/A", date(2024, 5, 10), datetime(2024, 5, 10, 22, tzinfo=timezone.utc)),
    }
    counters: Counter[str] = Counter()
    candidates = sec._candidates_for_cik(fact_payload, cik="0000000001", acceptances=acceptances,
        spy_days=(date(2024, 4, 30), date(2024, 5, 1), date(2024, 5, 10), date(2024, 5, 13), date(2024, 5, 14)),
        start_date=date(2024, 4, 1), end_date=date(2024, 5, 14), counters=counters)
    dates = {item.accession: item.public_date for item in candidates}
    assert dates == {"000000000124000001": date(2024, 5, 1), "000000000124000002": date(2024, 5, 13), "000000000124000003": date(2024, 5, 14)}
    assert counters["filed_date_fallback_fact_count"] == 1
