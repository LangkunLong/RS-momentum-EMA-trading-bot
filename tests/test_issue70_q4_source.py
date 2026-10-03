"""Source-backed fiscal Q4 extraction rules for issue 70."""

from __future__ import annotations

import csv
from datetime import date
import json
from pathlib import Path
import zipfile

import pytest

import core.sec_pit_fundamentals as sec
from tools import generate_issue70_q4_source_sample as issue70_v9


CIK = "0000000001"
TICKER = "AAA"
ANNUAL_END = "2024-12-31"


def _fact(
    accession: str,
    form: str,
    filed: str,
    start: str,
    end: str,
    fiscal_period: str,
    value: float,
    *,
    fiscal_year: str = "2024",
    decimals: str = "-6",
) -> dict[str, object]:
    return {
        "accn": accession,
        "form": form,
        "filed": filed,
        "start": start,
        "end": end,
        "fy": fiscal_year,
        "fp": fiscal_period,
        "val": value,
        "decimals": decimals,
    }


def _write_source_fixture(
    tmp_path: Path,
    *,
    omit_q3_revenue: bool = False,
    q2_revenue_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    q2_amendment_after_annual: bool = False,
    annual_restatement_unreconciled: bool = False,
    annual_restatement_reconciled: bool = False,
    annual_restatement_reconciled_same_session: bool = False,
    misaligned_q2_start: bool = False,
) -> tuple[Path, sec.SecurityMasterResult, Path]:
    filings = [
        ("0000000001-24-000001", "10-Q", "2024-04-30", "20240430220000"),
        ("0000000001-24-000002", "10-Q", "2024-08-01", "20240801220000"),
        ("0000000001-24-000003", "10-Q", "2024-11-01", "20241101220000"),
        ("0000000001-25-000001", "10-K", "2025-02-14", "20250214220000"),
    ]
    if q2_amendment_after_annual:
        filings.append(
            ("0000000001-25-000002", "10-Q/A", "2025-03-03", "20250303220000")
        )
    if annual_restatement_unreconciled or annual_restatement_reconciled or annual_restatement_reconciled_same_session:
        amended_filed, amended_acceptance = (
            ("2025-02-17", "20250217220000")
            if annual_restatement_reconciled_same_session
            else ("2025-03-10", "20250310220000")
        )
        filings.append(
            ("0000000001-25-000003", "10-K/A", amended_filed, amended_acceptance)
        )
    filings.append(
        ("0000000001-18-000001", "10-K", "2018-02-15", "20180215170000")
    )

    basic = [
        _fact(filings[0][0], "10-Q", filings[0][2], "2024-01-01", "2024-03-31", "Q1", 0.8),
        _fact(filings[1][0], "10-Q", filings[1][2], "2024-04-01", "2024-06-30", "Q2", 0.9),
        _fact(filings[2][0], "10-Q", filings[2][2], "2024-07-01", "2024-09-30", "Q3", 1.0),
        _fact(filings[3][0], "10-K", filings[3][2], "2024-01-01", ANNUAL_END, "FY", 4.2),
        _fact(filings[3][0], "10-K", filings[3][2], "2024-10-01", ANNUAL_END, "FY", 1.2),
        _fact(
            filings[-1][0],
            "10-K",
            filings[-1][2],
            "2017-01-01",
            "2017-12-31",
            "FY",
            2.5,
            fiscal_year="2017",
        ),
    ]
    diluted = [
        _fact(filings[3][0], "10-K", filings[3][2], "2024-01-01", ANNUAL_END, "FY", 4.0),
        _fact(filings[3][0], "10-K", filings[3][2], "2024-10-01", ANNUAL_END, "FY", 1.1),
    ]
    revenue = [
        _fact(filings[0][0], "10-Q", filings[0][2], "2024-01-01", "2024-03-31", "Q1", 10.0),
        _fact(filings[1][0], "10-Q", filings[1][2], "2024-04-01", "2024-06-30", "Q2", 20.0),
        _fact(filings[2][0], "10-Q", filings[2][2], "2024-07-01", "2024-09-30", "Q3", 30.0),
        _fact(filings[3][0], "10-K", filings[3][2], "2024-01-01", ANNUAL_END, "FY", 100.0),
    ]
    if annual_restatement_reconciled or annual_restatement_reconciled_same_session:
        # A direct Q4 fact in the original 10-K must not suppress a later
        # reconciled derived Q4 from the separate 10-K/A accession.
        revenue.append(
            _fact(
                filings[3][0],
                "10-K",
                filings[3][2],
                "2024-10-01",
                ANNUAL_END,
                "FY",
                40.0,
            )
        )
    q_starts = ["2024-01-01", "2024-04-04" if misaligned_q2_start else "2024-04-01", "2024-07-01"]
    for filing_period, (start, end, value) in zip(
        ("Q1", "Q2", "Q3"),
        zip(
            q_starts,
            ("2024-03-31", "2024-06-30", "2024-09-30"),
            (10.0, 20.0, 30.0),
            strict=True,
        ),
        strict=True,
    ):
        revenue.append(
            _fact(
                filings[3][0],
                "10-K",
                filings[3][2],
                start,
                end,
                filing_period,
                value,
            )
        )
    if annual_restatement_unreconciled:
        revenue.append(
            _fact(
                filings[4][0],
                "10-K/A",
                filings[4][2],
                "2024-01-01",
                ANNUAL_END,
                "FY",
                105.0,
            )
        )
    if annual_restatement_reconciled or annual_restatement_reconciled_same_session:
        revenue.append(
            _fact(
                filings[4][0],
                "10-K/A",
                filings[4][2],
                "2024-01-01",
                ANNUAL_END,
                "FY",
                105.0,
            )
        )
        for filing_period, start, end, value in (
            ("Q1", "2024-01-01", "2024-03-31", 10.0),
            ("Q2", "2024-04-01", "2024-06-30", 20.0),
            ("Q3", "2024-07-01", "2024-09-30", 30.0),
        ):
            revenue.append(
                _fact(
                    filings[4][0],
                    "10-K/A",
                    filings[4][2],
                    start,
                    end,
                    filing_period,
                    value,
                )
            )
    if omit_q3_revenue:
        revenue = [fact for fact in revenue if fact["fp"] != "Q3"]
    if q2_amendment_after_annual:
        revenue.append(
            _fact(
                filings[4][0],
                "10-Q/A",
                filings[4][2],
                "2024-04-01",
                "2024-06-30",
                "Q2",
                22.0,
            )
        )

    concepts = {
        "EarningsPerShareBasic": {"units": {"USD/shares": basic}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": diluted}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {
            "units": {"USD": revenue}
        },
    }
    if q2_revenue_concept != "RevenueFromContractWithCustomerExcludingAssessedTax":
        q2_facts = [fact for fact in revenue if fact["fp"] == "Q2"]
        concepts["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"] = [
            fact for fact in revenue if fact["fp"] != "Q2"
        ]
        concepts[q2_revenue_concept] = {"units": {"USD": q2_facts}}

    submissions = tmp_path / "submissions.zip"
    companyfacts = tmp_path / "companyfacts.zip"
    with zipfile.ZipFile(submissions, "x") as archive:
        archive.writestr(
            f"CIK{CIK}.json",
            json.dumps(
                {
                    "cik": int(CIK),
                    "name": "Example Issuer",
                    "tickers": [TICKER],
                    "formerNames": [],
                    "filings": {
                        "recent": {
                            "accessionNumber": [row[0] for row in filings],
                            "form": [row[1] for row in filings],
                            "filingDate": [row[2] for row in filings],
                            "acceptanceDateTime": [row[3] for row in filings],
                        },
                        "files": [],
                    },
                }
            ),
        )
    with zipfile.ZipFile(companyfacts, "x") as archive:
        archive.writestr(
            f"CIK{CIK}.json",
            json.dumps({"cik": int(CIK), "facts": {"us-gaap": concepts}}),
        )

    days = tmp_path / "spy.csv"
    with days.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["trade_date"])
        writer.writerows(
            (day,)
            for day in (
                "2024-05-01",
                "2024-08-02",
                "2024-11-04",
                "2025-02-18",
                "2025-03-04",
                "2025-03-11",
                "2025-12-31",
            )
        )
    acceptances, missing_fragments = sec._acceptances_for_ciks(
        submissions, (CIK,), max_json_member_bytes=1024 * 1024
    )
    master = sec.SecurityMasterResult(
        rows=(
            sec.SecurityMasterRow(
                TICKER,
                CIK,
                "Example Issuer",
                date(2024, 1, 1),
                date(2025, 12, 31),
                "exact_current_ticker",
            ),
        ),
        exclusions=(),
        acceptance_by_cik=acceptances,
        membership_union=(TICKER,),
        identity_manifest_sha256="0" * 64,
        submissions_archive_sha256=sec.sha256_file(submissions),
        missing_submission_fragments=missing_fragments,
    )
    return companyfacts, master, days


def _extract(
    tmp_path: Path,
    **fixture_options: object,
) -> sec.FundamentalExportResult:
    companyfacts, master, days = _write_source_fixture(tmp_path, **fixture_options)
    return sec.extract_fundamentals(
        companyfacts,
        master,
        days,
        start_date=date(2024, 1, 1),
        end_date=date(2025, 12, 31),
        max_json_member_bytes=1024 * 1024,
    )


def _write_amzn_actual_fy_context_fixture(
    tmp_path: Path,
    *,
    omit_direct_q4_counterfactual: bool = False,
) -> tuple[Path, sec.SecurityMasterResult, Path]:
    """Excerpt actual FY-labeled AMZN contexts; optional Q4 omission is counterfactual.

    Source: SEC companyfacts.zip SHA-256
    d7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca,
    member CIK0001018724.json SHA-256
    e6aadb0da7384597dbd78f74d1379ffb7fd0829a4d2e797b6d7e442b5e1da0d7,
    accession 0001018724-21-000004. The four quarter contexts and FY context
    retain their original fp=FY values. The counterfactual removes only the
    reported Q4 duration fact to exercise the derivation fallback.
    """
    cik = "0001018724"
    ticker = "AMZN"
    accession = "0001018724-21-000004"
    filed = "2021-02-03"
    acceptance = "2021-02-03T00:44:10Z"
    revenue_facts = [
        _fact(
            accession,
            "10-K",
            filed,
            "2020-01-01",
            "2020-12-31",
            "FY",
            386_064_000_000,
            fiscal_year="2020",
        ),
        _fact(
            accession,
            "10-K",
            filed,
            "2020-01-01",
            "2020-03-31",
            "FY",
            75_452_000_000,
            fiscal_year="2020",
        ),
        _fact(
            accession,
            "10-K",
            filed,
            "2020-04-01",
            "2020-06-30",
            "FY",
            88_912_000_000,
            fiscal_year="2020",
        ),
        _fact(
            accession,
            "10-K",
            filed,
            "2020-07-01",
            "2020-09-30",
            "FY",
            96_145_000_000,
            fiscal_year="2020",
        ),
        _fact(
            accession,
            "10-K",
            filed,
            "2020-10-01",
            "2020-12-31",
            "FY",
            125_555_000_000,
            fiscal_year="2020",
        ),
    ]
    revenue_facts[-1]["frame"] = "CY2020Q4"
    if omit_direct_q4_counterfactual:
        revenue_facts = [
            fact for fact in revenue_facts if fact["start"] != "2020-10-01"
        ]

    submissions = tmp_path / "amzn_submissions.zip"
    companyfacts = tmp_path / "amzn_companyfacts.zip"
    with zipfile.ZipFile(submissions, "x") as archive:
        archive.writestr(
            f"CIK{cik}.json",
            json.dumps(
                {
                    "cik": int(cik),
                    "name": "Amazon.com, Inc.",
                    "tickers": [ticker],
                    "formerNames": [],
                    "filings": {
                        "recent": {
                            "accessionNumber": [accession],
                            "form": ["10-K"],
                            "filingDate": [filed],
                            "acceptanceDateTime": [acceptance],
                        },
                        "files": [],
                    },
                }
            ),
        )
    with zipfile.ZipFile(companyfacts, "x") as archive:
        archive.writestr(
            f"CIK{cik}.json",
            json.dumps(
                {
                    "cik": int(cik),
                    "facts": {
                        "us-gaap": {
                            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                                "units": {"USD": revenue_facts}
                            }
                        }
                    },
                }
            ),
        )

    days = tmp_path / "amzn_spy.csv"
    with days.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["trade_date"])
        writer.writerows((("2020-01-02",), ("2021-02-04",), ("2025-12-31",)))
    acceptances, missing_fragments = sec._acceptances_for_ciks(
        submissions, (cik,), max_json_member_bytes=1024 * 1024
    )
    master = sec.SecurityMasterResult(
        rows=(
            sec.SecurityMasterRow(
                ticker,
                cik,
                "Amazon.com, Inc.",
                date(2020, 1, 1),
                date(2025, 12, 31),
                "exact_current_ticker",
            ),
        ),
        exclusions=(),
        acceptance_by_cik=acceptances,
        membership_union=(ticker,),
        identity_manifest_sha256="0" * 64,
        submissions_archive_sha256=sec.sha256_file(submissions),
        missing_submission_fragments=missing_fragments,
    )
    return companyfacts, master, days


def _extract_amzn_actual_fy_context_fixture(
    tmp_path: Path,
    *,
    omit_direct_q4_counterfactual: bool = False,
) -> sec.FundamentalExportResult:
    companyfacts, master, days = _write_amzn_actual_fy_context_fixture(
        tmp_path,
        omit_direct_q4_counterfactual=omit_direct_q4_counterfactual,
    )
    return sec.extract_fundamentals(
        companyfacts,
        master,
        days,
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
        max_json_member_bytes=1024 * 1024,
    )


def test_actual_amzn_fy_labeled_context_keeps_direct_q4_preferred(tmp_path: Path) -> None:
    result = _extract_amzn_actual_fy_context_fixture(tmp_path)
    q4 = [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if row.statement_type == "quarterly" and row.period_end == date(2020, 12, 31)
    ]

    assert len(q4) == 1
    row, audit = q4[0]
    assert row.total_revenue == 125_555_000_000
    source = json.loads(audit.metric_sources)["total_revenue"]
    assert source["q4_attribution"] == "direct_10k_quarter_duration"
    assert source["fiscal_period"] == "FY"


def test_counterfactual_removal_of_actual_direct_q4_derives_from_fy_intervals(
    tmp_path: Path,
) -> None:
    result = _extract_amzn_actual_fy_context_fixture(
        tmp_path,
        omit_direct_q4_counterfactual=True,
    )
    q4 = [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if row.statement_type == "quarterly" and row.period_end == date(2020, 12, 31)
    ]

    assert len(q4) == 1
    row, audit = q4[0]
    assert row.total_revenue == 125_555_000_000
    source = json.loads(audit.metric_sources)["total_revenue"]
    assert source["q4_attribution"] == "derived"
    assert source["input_values"] == [
        386_064_000_000,
        75_452_000_000,
        88_912_000_000,
        96_145_000_000,
    ]
    quarter_sources = source["inputs"][1:]
    assert [item["fiscal_period"] for item in quarter_sources] == ["FY"] * 3
    assert [item["normalized_fiscal_quarter"] for item in quarter_sources] == [
        "Q1",
        "Q2",
        "Q3",
    ]
    assert all(
        item["fiscal_period_inference_method"] == "contiguous_interval_partition_within_fy"
        for item in quarter_sources
    )


def test_direct_q4_and_additive_revenue_require_same_accession_basis(tmp_path: Path) -> None:
    result = _extract(tmp_path, q2_amendment_after_annual=True)

    q4 = [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if row.ticker == TICKER
        and row.statement_type == "quarterly"
        and row.period_end == date.fromisoformat(ANNUAL_END)
    ]

    assert [(row.public_date.isoformat(), row.basic_eps, row.diluted_eps, row.total_revenue) for row, _ in q4] == [
        ("2025-02-18", 1.2, 1.1, 40.0),
    ]
    revenue_sources = [json.loads(audit.metric_sources)["total_revenue"] for _, audit in q4]
    assert revenue_sources[0]["derivation"] == "annual_minus_q1_q2_q3"
    assert revenue_sources[0]["input_values"] == [100.0, 10.0, 20.0, 30.0]
    assert {item["accession_number"] for item in revenue_sources[0]["inputs"]} == {
        "0000000001-25-000001"
    }
    assert all(source["unit"] == "USD" for source in revenue_sources)

    annual = next(
        row
        for row in result.rows
        if row.statement_type == "annual" and row.period_end == date.fromisoformat(ANNUAL_END)
    )
    assert annual.basic_eps == 4.2
    assert all(row.period_end >= date(2020, 1, 1) for row in result.rows)


def test_derived_q4_basis_inputs_retain_raw_public_dates_and_intervals(tmp_path: Path) -> None:
    result = _extract(tmp_path)
    q4_audit = next(
        audit
        for audit in result.audit_rows
        if audit.statement_type == "quarterly"
        and audit.period_end == date.fromisoformat(ANNUAL_END)
        and json.loads(audit.metric_sources)["total_revenue"].get("q4_attribution") == "derived"
    )

    revenue = json.loads(q4_audit.metric_sources)["total_revenue"]
    assert all("source_public_date" not in item for item in revenue["inputs"])
    assert [
        (
            item["source_value"],
            item["period_start"],
            item["period_end"],
            (
                item["acceptance_datetime"][:10]
                if item["acceptance_datetime"]
                else item["filed_date"]
            ),
            item["public_date_basis"],
            item["public_date"],
        )
        for item in revenue["inputs"]
    ] == [
        (100.0, "2024-01-01", "2024-12-31", "2025-02-14", "acceptance_datetime", "2025-02-18"),
        (10.0, "2024-01-01", "2024-03-31", "2025-02-14", "acceptance_datetime", "2025-02-18"),
        (20.0, "2024-04-01", "2024-06-30", "2025-02-14", "acceptance_datetime", "2025-02-18"),
        (30.0, "2024-07-01", "2024-09-30", "2025-02-14", "acceptance_datetime", "2025-02-18"),
    ]


def test_v9_coverage_keeps_all_field_denominators_and_q4_basis_origins(tmp_path: Path) -> None:
    result = _extract(tmp_path)
    synthetic_calendar_sessions = (
        date(2024, 5, 1),
        date(2024, 8, 2),
        date(2024, 11, 4),
        date(2025, 2, 18),
        date(2025, 3, 4),
        date(2025, 3, 11),
        date(2025, 3, 12),
        date(2025, 12, 31),
    )
    security = sec.SecurityMasterRow(
        TICKER,
        CIK,
        "Example Issuer",
        date(2025, 3, 12),
        date(2025, 12, 31),
        "exact_current_ticker",
    )
    records, summary = issue70_v9._build_v9_coverage_records(
        fundamentals=result,
        security_rows=(security,),
        evaluation_sessions=(date(2025, 3, 11), date(2025, 3, 12)),
        calendar_sessions=synthetic_calendar_sessions,
    )

    field_rows = [row for row in records if row["record_kind"] == "field_session"]
    assert {row["slot_metric"] for row in field_rows} == {
        "basic_eps",
        "diluted_eps",
        "total_revenue",
        "net_income",
        "common_stock",
        "total_stockholders_equity",
        "shares_outstanding",
    }
    common_stock = next(row for row in field_rows if row["slot_metric"] == "common_stock")
    assert common_stock["consumer_status"] == "intentionally_not_exposed"
    assert common_stock["actual_policy_consumption"] == "not_measured_no_policy_replay"
    assert summary["eligible_security_session_count"] == 1
    assert summary["field_session_denominator_count"] == 7
    assert sum(row["membership_status"] == "pre_membership_or_outside_sample" for row in field_rows) == 7
    assert summary["pre_membership_rows_are_excluded_from_denominators"] is True
    assert summary["by_evaluation_year"]["2025"]["field:total_revenue:raw_fact_candidate_count"] >= 1
    assert summary["by_evaluation_year"]["2025"]["field:total_revenue:visible_origin_count"] >= 1
    assert summary["unique_eligible_tickers_by_evaluation_year_and_field"]["2025:total_revenue"] == 1

    revenue_slot = next(
        row
        for row in records
        if row["record_kind"] == "expected_slot"
        and row["slot_metric"] == "q4_total_revenue"
        and row["expected_period_end"] == ANNUAL_END
    )
    assert revenue_slot["status"] == "derived"
    basis = [
        row
        for row in records
        if row["record_kind"] == "source_origin"
        and row["slot_id"] == revenue_slot["slot_id"]
    ]
    assert [row["origin_role"] for row in basis] == [
        "q4_fy_basis",
        "q4_q1_basis",
        "q4_q2_basis",
        "q4_q3_basis",
    ]
    assert [row["source_value"] for row in basis] == ["100", "10", "20", "30"]
    assert [(row["source_period_start"], row["source_period_end"]) for row in basis] == [
        ("2024-01-01", "2024-12-31"),
        ("2024-01-01", "2024-03-31"),
        ("2024-04-01", "2024-06-30"),
        ("2024-07-01", "2024-09-30"),
    ]
    assert {row["source_public_date"] for row in basis} == {"2025-02-14"}
    assert {row["available_from_session"] for row in basis} == {"2025-02-18"}
    assert all(row["public_date_basis"] == "acceptance_datetime" for row in basis)
    assert all(row["origin_id"] for row in basis)

    quarterly_eps = [
        row
        for row in records
        if row["record_kind"] == "expected_slot"
        and row["slot_metric"] == "quarterly_eps_growth"
    ]
    assert [row["slot_number"] for row in quarterly_eps] == ["1", "2", "3", "4"]
    assert any(row["status"] == "missing" for row in quarterly_eps)

    with pytest.raises(ValueError, match="250,000-row cap"):
        issue70_v9._build_v9_coverage_records(
            fundamentals=result,
            security_rows=(security,),
            evaluation_sessions=(date(2025, 3, 11),),
            calendar_sessions=synthetic_calendar_sessions,
            max_records=1,
        )


def test_q4_revenue_rejects_noncontiguous_quarter_starts(tmp_path: Path) -> None:
    result = _extract(tmp_path, misaligned_q2_start=True)
    q4 = [
        row
        for row in result.rows
        if row.ticker == TICKER
        and row.statement_type == "quarterly"
        and row.period_end == date.fromisoformat(ANNUAL_END)
    ]

    assert len(q4) == 1
    assert q4[0].basic_eps == 1.2
    assert q4[0].total_revenue is None


def test_restated_annual_with_unmatched_quarters_does_not_create_new_q4(tmp_path: Path) -> None:
    result = _extract(tmp_path, annual_restatement_unreconciled=True)
    q4 = [
        row
        for row in result.rows
        if row.ticker == TICKER
        and row.statement_type == "quarterly"
        and row.period_end == date.fromisoformat(ANNUAL_END)
    ]

    assert [(row.public_date, row.total_revenue) for row in q4] == [
        (date(2025, 2, 18), 40.0)
    ]


def test_later_reconciled_annual_vintage_derives_new_q4_after_older_direct_fact(
    tmp_path: Path,
) -> None:
    result = _extract(tmp_path, annual_restatement_reconciled=True)
    q4 = [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if row.statement_type == "quarterly" and row.period_end == date.fromisoformat(ANNUAL_END)
    ]

    assert [(row.public_date, row.total_revenue) for row, _ in q4] == [
        (date(2025, 2, 18), 40.0),
        (date(2025, 3, 11), 45.0),
    ]
    original_sources = json.loads(q4[0][1].metric_sources)["total_revenue"]
    assert original_sources["q4_attribution"] == "direct_10k_quarter_duration"
    revised_sources = json.loads(q4[-1][1].metric_sources)["total_revenue"]
    assert revised_sources["q4_attribution"] == "derived"
    assert [
        item["accession_number"] for item in revised_sources["inputs"]
    ] == ["0000000001-25-000003"] * 4


def test_later_derived_q4_wins_same_session_as_older_direct_fact(tmp_path: Path) -> None:
    result = _extract(tmp_path, annual_restatement_reconciled_same_session=True)
    q4 = [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if row.statement_type == "quarterly" and row.period_end == date.fromisoformat(ANNUAL_END)
    ]

    assert len(q4) == 1
    row, audit = q4[0]
    assert row.public_date == date(2025, 2, 18)
    assert row.basic_eps == 1.2
    assert row.total_revenue == 45.0
    source = json.loads(audit.metric_sources)["total_revenue"]
    assert source["q4_attribution"] == "derived"
    assert source["inputs"][0]["accession_number"] == "0000000001-25-000003"


def test_missing_or_incompatible_quarter_is_not_filled_from_annual_revenue(tmp_path: Path) -> None:
    result = _extract(
        tmp_path,
        omit_q3_revenue=True,
        q2_revenue_concept="Revenues",
    )

    q4 = [
        row
        for row in result.rows
        if row.ticker == TICKER
        and row.statement_type == "quarterly"
        and row.period_end == date.fromisoformat(ANNUAL_END)
    ]
    assert len(q4) == 1
    assert q4[0].public_date == date(2025, 2, 18)
    assert q4[0].basic_eps == 1.2
    assert q4[0].total_revenue is None
    annual = next(
        row
        for row in result.rows
        if row.statement_type == "annual" and row.period_end == date.fromisoformat(ANNUAL_END)
    )
    assert annual.total_revenue == 100.0
