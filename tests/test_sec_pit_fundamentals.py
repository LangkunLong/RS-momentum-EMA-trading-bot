"""Offline accession/public-date regressions for the SEC PIT normalizer."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
from pathlib import Path
import zipfile

import core.sec_pit_fundamentals as sec
import pytest


def test_balance_only_payload_does_not_use_income_quarter_context_locals() -> None:
    accession = "0000000001-24-000001"
    fact = {
        "accn": accession,
        "form": "10-K",
        "filed": "2024-04-30",
        "end": "2024-03-31",
        "fy": "2024",
        "fp": "Q1",
        "val": 100.0,
    }
    payload = {
        "cik": "1",
        "facts": {
            "us-gaap": {"CommonStockValue": {"units": {"USD": [fact]}}},
        },
    }

    candidates = sec._candidates_for_cik(
        payload,
        cik="0000000001",
        acceptances={},
        spy_days=(date(2024, 5, 1),),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        counters=Counter(),
    )

    assert len(candidates) == 1
    assert candidates[0].statement_type == "balance"
    assert "fiscal_period_interpretation" not in candidates[0].metric_details["common_stock"]


def test_fact_without_a_strict_next_session_gets_a_distinct_omission_counter() -> None:
    counters: Counter[str] = Counter()

    candidate = sec._candidate_metadata(
        {
            "accn": "0000000001-25-000001",
            "form": "10-K",
            "filed": "2025-12-31",
            "end": "2025-11-30",
            "val": 10.0,
        },
        cik="0000000001",
        acceptances={},
        spy_days=(date(2025, 12, 31),),
        start_date=date(2010, 1, 1),
        end_date=date(2025, 12, 31),
        counters=counters,
    )

    assert candidate is None
    assert counters["no_next_session_fact_omissions"] == 1
    assert counters["post_cutoff_fact_omissions"] == 1
    assert counters["mapped_after_cutoff_fact_omissions"] == 0


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


_Q4_CIK = "0000000001"
_Q4_YEAR = "2024"
_Q4_END = date(2024, 12, 28)
_Q3_END = date(2024, 9, 28)
_REVENUE_NS, _REVENUE_TAG, _REVENUE_UNIT = sec._REVENUE_PRIORITY[0]
_NET_INCOME_NS, _NET_INCOME_TAG, _NET_INCOME_UNIT = sec._INCOME_CONCEPTS["net_income"][0]


def _q4_fact(
    accession: str,
    form: str,
    filed: str,
    fiscal_year: str,
    fiscal_period: str,
    period_start: str,
    period_end: str,
    value: float,
) -> dict[str, object]:
    return {
        "accn": accession,
        "form": form,
        "filed": filed,
        "start": period_start,
        "end": period_end,
        "fy": fiscal_year,
        "fp": fiscal_period,
        "val": value,
    }


def _base_q4_facts() -> list[tuple[str, str, str, dict[str, object]]]:
    q3_accession = "0000000001-24-000001"
    annual_accession = "0000000001-25-000001"
    return [
        (
            _REVENUE_NS,
            _REVENUE_TAG,
            _REVENUE_UNIT,
            _q4_fact(
                q3_accession, "10-Q", "2024-11-08", _Q4_YEAR, "Q3",
                "2024-01-01", _Q3_END.isoformat(), 730.10,
            ),
        ),
        (
            _REVENUE_NS,
            _REVENUE_TAG,
            _REVENUE_UNIT,
            _q4_fact(
                annual_accession, "10-K", "2025-02-14", _Q4_YEAR, "FY",
                "2024-01-01", _Q4_END.isoformat(), 1000.25,
            ),
        ),
        (
            _NET_INCOME_NS,
            _NET_INCOME_TAG,
            _NET_INCOME_UNIT,
            _q4_fact(
                q3_accession, "10-Q", "2024-11-08", _Q4_YEAR, "Q3",
                "2024-01-01", _Q3_END.isoformat(), 101.40,
            ),
        ),
        (
            _NET_INCOME_NS,
            _NET_INCOME_TAG,
            _NET_INCOME_UNIT,
            _q4_fact(
                annual_accession, "10-K", "2025-02-14", _Q4_YEAR, "FY",
                "2024-01-01", _Q4_END.isoformat(), 80.05,
            ),
        ),
    ]


def _extract_q4_facts(
    tmp_path: Path,
    fact_specs: list[tuple[str, str, str, dict[str, object]]],
) -> sec.FundamentalExportResult:
    concepts: dict[str, dict[str, dict[str, dict[str, list[dict[str, object]]]]]] = {}
    acceptances: dict[str, sec.FilingAcceptance] = {}
    for namespace, concept, unit, fact in fact_specs:
        concepts.setdefault(namespace, {}).setdefault(concept, {}).setdefault("units", {}).setdefault(unit, []).append(fact)
        accession = str(fact["accn"])
        filed = date.fromisoformat(str(fact["filed"]))
        accepted = datetime.combine(filed, datetime.min.time().replace(hour=22), tzinfo=timezone.utc)
        acceptances[accession] = sec.FilingAcceptance(
            accession,
            str(fact["form"]),
            filed,
            accepted,
        )

    archive = tmp_path / "q4-companyfacts.zip"
    with zipfile.ZipFile(archive, "x") as handle:
        handle.writestr(
            "CIK0000000001.json",
            json.dumps({"cik": "1", "facts": concepts}),
        )
    calendar = tmp_path / "q4-spy.csv"
    calendar.write_text(
        "trade_date\n"
        "2024-11-12\n"
        "2025-02-18\n"
        "2025-03-04\n"
        "2025-03-11\n"
        "2025-03-20\n",
        encoding="utf-8",
    )
    master = sec.SecurityMasterResult(
        rows=(
            sec.SecurityMasterRow(
                "AAA", _Q4_CIK, "Alpha", date(2024, 1, 1), date(2025, 3, 20), "fixture"
            ),
        ),
        exclusions=(),
        acceptance_by_cik={_Q4_CIK: acceptances},
        membership_union=("AAA",),
        identity_manifest_sha256="0" * 64,
        submissions_archive_sha256="0" * 64,
        missing_submission_fragments=0,
    )
    return sec.extract_fundamentals(
        archive,
        master,
        calendar,
        start_date=date(2024, 1, 1),
        end_date=date(2025, 3, 20),
        attested_companyfacts_archive_sha256="0" * 64,
    )


def _q4_pairs(result: sec.FundamentalExportResult):
    return [
        (row, audit)
        for row, audit in zip(result.rows, result.audit_rows, strict=True)
        if audit.statement_type == "quarterly"
        and audit.fiscal_period.upper() == "Q4"
        and audit.period_end == _Q4_END
    ]


def test_q4_q3_ytd_inputs_derive_visible_flow_metrics_with_two_fact_lineage(tmp_path: Path) -> None:
    result = _extract_q4_facts(tmp_path, _base_q4_facts())

    q4 = _q4_pairs(result)
    assert len(q4) == 1
    row, audit = q4[0]
    assert audit.public_date == date(2025, 2, 18)
    assert audit.public_date_basis == "later_of_input_public_dates"
    assert row.public_date == audit.public_date
    assert row.total_revenue == pytest.approx(270.15)
    assert row.net_income == pytest.approx(-21.35)
    assert row.basic_eps is None
    assert not any(
        audit.statement_type == "cumulative_input"
        for audit in result.audit_rows
    )

    metric_sources = json.loads(audit.metric_sources)
    assert audit.accession_number == "--"
    assert audit.form == "DERIVED_Q4"
    assert audit.fiscal_period == "Q4"
    for metric, expected in (("total_revenue", 270.15), ("net_income", -21.35)):
        source = metric_sources[metric]
        assert source["kind"] == "derived"
        assert source["schema_version"] == 1
        assert source["rule_id"] == "sec-q4-flow-fy-minus-q3-ytd/v1"
        assert source["source_value"] == pytest.approx(expected)
        assert len(source["inputs"]) == 2
        assert source["inputs"][0]["public_date"] == "2025-02-18"
        assert {item["source_role"] for item in source["inputs"]} == {"FY", "Q3_YTD"}
        assert {item["accession_number"] for item in source["inputs"]} == {
            "0000000001-24-000001",
            "0000000001-25-000001",
        }
        for input_fact in source["inputs"]:
            assert {
                "cik", "accession_number", "form", "filed_date", "acceptance_datetime",
                "public_date", "public_date_basis", "source_concept", "unit",
                "period_start", "period_end", "fiscal_year", "fiscal_period", "source_value",
            } <= input_fact.keys()


@pytest.mark.parametrize("incompatibility", ("tag", "unit", "fiscal_year", "start", "q4_span"))
def test_q4_q3_ytd_rejects_incompatible_metric_inputs(
    tmp_path: Path,
    incompatibility: str,
) -> None:
    facts = _base_q4_facts()
    q3_fact = dict(facts[0][3])
    q3_namespace, q3_tag, q3_unit = facts[0][:3]
    if incompatibility == "tag":
        q3_namespace, q3_tag = _REVENUE_NS, "Revenues"
    elif incompatibility == "unit":
        q3_unit = "CAD"
    elif incompatibility == "fiscal_year":
        q3_fact["fy"] = "2023"
    elif incompatibility == "start":
        q3_fact["start"] = "2024-01-02"
    elif incompatibility == "q4_span":
        q3_fact["end"] = "2024-11-01"
    facts[0] = (q3_namespace, q3_tag, q3_unit, q3_fact)

    q4 = _q4_pairs(_extract_q4_facts(tmp_path, facts))

    assert len(q4) == 1
    assert q4[0][0].total_revenue is None
    assert q4[0][0].net_income == pytest.approx(-21.35)


def test_q4_q3_ytd_incompatible_later_revision_clears_only_affected_metric(tmp_path: Path) -> None:
    facts = _base_q4_facts()
    amendment = _q4_fact(
        "0000000001-25-000002", "10-Q/A", "2025-03-03", _Q4_YEAR, "Q3",
        "2024-01-02", _Q3_END.isoformat(), 731.10,
    )
    facts.append((_REVENUE_NS, _REVENUE_TAG, _REVENUE_UNIT, amendment))

    q4 = _q4_pairs(_extract_q4_facts(tmp_path, facts))

    assert [(audit.public_date, row.total_revenue) for row, audit in q4] == [
        (date(2025, 2, 18), pytest.approx(270.15)),
        (date(2025, 3, 4), None),
    ]
    assert q4[0][0].net_income == pytest.approx(-21.35)
    assert q4[1][0].net_income == pytest.approx(-21.35)
    invalidation = json.loads(q4[1][1].metric_sources)["total_revenue"]
    assert invalidation["kind"] == "unavailable"
    assert invalidation["rule_id"] == "sec-q4-flow-fy-minus-q3-ytd/v1"
    assert "start_mismatch" in invalidation["reason_codes"]


def test_q4_q3_ytd_direct_fact_wins_and_records_conflicting_derivation(tmp_path: Path) -> None:
    facts = _base_q4_facts()
    direct = _q4_fact(
        "0000000001-25-000001", "10-K", "2025-02-14", _Q4_YEAR, "Q4",
        "2024-09-29", _Q4_END.isoformat(), 270.50,
    )
    facts.append((_REVENUE_NS, _REVENUE_TAG, _REVENUE_UNIT, direct))

    q4 = _q4_pairs(_extract_q4_facts(tmp_path, facts))

    assert len(q4) == 1
    row, audit = q4[0]
    assert row.total_revenue == pytest.approx(270.50)
    assert audit.accession_number == "0000000001-25-000001"
    assert audit.form == "10-K"
    source = json.loads(audit.metric_sources)["total_revenue"]
    assert source["kind"] == "direct_with_derived_comparison"
    assert source["direct_source"]["accession_number"] == "0000000001-25-000001"
    comparison = source["derived_comparison"]
    assert comparison["rule_id"] == "sec-q4-flow-fy-minus-q3-ytd/v1"
    assert comparison["source_value"] == pytest.approx(270.15)
    assert comparison["conflict"] is True
    assert comparison["difference"] == pytest.approx(0.35)
    assert len(comparison["inputs"]) == 2
    assert {item["source_role"] for item in comparison["inputs"]} == {"FY", "Q3_YTD"}
    assert {item["accession_number"] for item in comparison["inputs"]} == {
        "0000000001-24-000001",
        "0000000001-25-000001",
    }
    assert {
        (item["source_role"], item["source_value"])
        for item in comparison["inputs"]
    } == {("FY", 1000.25), ("Q3_YTD", 730.10)}
    for input_fact in comparison["inputs"]:
        assert {
            "cik", "accession_number", "form", "filed_date", "acceptance_datetime",
            "public_date", "public_date_basis", "source_concept", "unit",
            "period_start", "period_end", "fiscal_year", "fiscal_period", "source_value",
        } <= input_fact.keys()
