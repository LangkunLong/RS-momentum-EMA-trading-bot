"""Offline accession/public-date regressions for the SEC PIT normalizer."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import zipfile

import core.sec_pit_fundamentals as sec


def _quarterly_fact(accession: str, form: str, filed: str, value: float) -> dict[str, object]:
    return {
        "accn": accession,
        "form": form,
        "filed": filed,
        "end": "2024-03-31",
        "start": "2024-01-01",
        "fy": "2024",
        "fp": "Q1",
        "frame": "CY2024Q1",
        "val": value,
    }


def test_submissions_accessions_control_as_of_visibility_and_amendment_timing(tmp_path: Path) -> None:
    """Break caught: a 10-Q was visible at period end or before its accepted public session."""
    first = "0000000001-24-000001"
    amendment = "0000000001-24-000002"
    fallback = "0000000001-24-000003"
    submissions = tmp_path / "submissions.zip"
    companyfacts = tmp_path / "companyfacts.zip"
    submission_payload = {
        "filings": {"recent": {
            "accessionNumber": [first, amendment, fallback],
            "form": ["10-Q", "10-Q/A", "10-Q"],
            "filingDate": ["2024-04-30", "2024-05-10", "2024-05-13"],
            "acceptanceDateTime": ["20240430220000", "20240510220000", ""],
        }, "files": []},
    }
    facts_payload = {"cik": "1", "facts": {"us-gaap": {"EarningsPerShareBasic": {"units": {"USD/shares": [
        _quarterly_fact(first, "10-Q", "2024-04-30", 1.0),
        _quarterly_fact(amendment, "10-Q/A", "2024-05-10", 1.1),
        _quarterly_fact(fallback, "10-Q", "2024-05-13", 1.2),
    ]}}}}}
    with zipfile.ZipFile(submissions, "x") as handle:
        handle.writestr("CIK0000000001.json", json.dumps(submission_payload))
    with zipfile.ZipFile(companyfacts, "x") as handle:
        handle.writestr("CIK0000000001.json", json.dumps(facts_payload))
    acceptances, missing_fragments = sec._acceptances_for_ciks(
        submissions, ("0000000001",), max_json_member_bytes=1024 * 1024,
    )
    spy_days = tmp_path / "spy.csv"
    spy_days.write_text(
        "trade_date\n2024-04-30\n2024-05-01\n2024-05-10\n2024-05-13\n2024-05-14\n",
        encoding="utf-8",
    )
    master = sec.SecurityMasterResult(
        rows=(sec.SecurityMasterRow("AAA", "0000000001", "Alpha", date(2024, 1, 1), date(2024, 5, 14), "ticker"),),
        exclusions=(),
        acceptance_by_cik=acceptances,
        membership_union=("AAA",),
        identity_manifest_sha256="0" * 64,
        submissions_archive_sha256=sec.sha256_file(submissions),
        missing_submission_fragments=missing_fragments,
    )
    result = sec.extract_fundamentals(
        companyfacts, master, spy_days, start_date=date(2024, 4, 1), end_date=date(2024, 5, 14),
    )
    as_of = lambda when: [row for row in result.rows if row.public_date <= when]
    assert as_of(date(2024, 3, 31)) == []
    assert as_of(date(2024, 4, 30)) == []
    assert [row.basic_eps for row in as_of(date(2024, 5, 1))] == [1.0]
    assert [row.basic_eps for row in as_of(date(2024, 5, 13))] == [1.0, 1.1]
    assert [row.basic_eps for row in as_of(date(2024, 5, 14))] == [1.0, 1.1, 1.2]
    assert result.coverage["filed_date_fallback_count"] == 1
