"""Negative controls for the bounded #70 CompanyFacts source trace."""

import json
import zipfile

import pytest

from tools.assess_issue70_retained_source import (
    analyze_bounded_archives,
    qualify_companyfacts_match,
)


def fact(*, value=2.0, unit="USD/shares", period_end="2023-12-31", period_start="2023-01-01"):
    return {
        "accn": "0000000000-24-000001",
        "form": "10-K",
        "filed": "2024-02-01",
        "fy": 2023,
        "fp": "FY",
        "unit": unit,
        "value": value,
        "val": value,
        "period_end": period_end,
        "period_start": period_start,
        "end": period_end,
        "start": period_start,
    }


def test_exact_period_duration_family_and_unit_produce_a_unique_match():
    result = qualify_companyfacts_match(
        [fact()],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "matched_unique"
    assert result["qualifying_candidate_count"] == 1


def test_same_value_from_wrong_period_is_not_a_source_match():
    result = qualify_companyfacts_match(
        [fact(period_end="2020-12-31", period_start="2020-01-01")],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "no_exact_period_end"
    assert result["qualifying_candidate_count"] == 0


def test_correct_period_with_wrong_duration_family_is_rejected():
    result = qualify_companyfacts_match(
        [fact(period_start="2023-10-01")],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "no_compatible_duration_family"
    assert result["qualifying_candidate_count"] == 0


def test_correct_period_and_family_with_wrong_unit_is_rejected():
    result = qualify_companyfacts_match(
        [fact(unit="USD")],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "no_expected_unit"
    assert result["qualifying_candidate_count"] == 0


def test_multiple_qualifying_facts_are_disposed_as_ambiguous():
    result = qualify_companyfacts_match(
        [fact(), fact(value=2.0)],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "ambiguous_multiple_qualifying_facts"
    assert result["qualifying_candidate_count"] == 2


def test_one_qualified_fact_with_different_value_is_not_a_match():
    result = qualify_companyfacts_match(
        [fact(value=2.1)],
        csv_value=2.0,
        metric="basic_eps",
        statement_type="annual",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "source_value_mismatch"
    assert result["qualifying_candidate_count"] == 1


def test_balance_sheet_metric_requires_an_instant_fact_and_expected_share_unit():
    result = qualify_companyfacts_match(
        [fact(unit="shares", period_start="")],
        csv_value=2.0,
        metric="shares_outstanding",
        statement_type="balance",
        export_period_end="2023-12-31",
    )

    assert result["disposition"] == "matched_unique"


def trace_disposition(
    tmp_path, observations, units=None, statement_type="annual", origin_present=True
):
    accession = "0000000000-24-000001"
    origin = {
        "accession_number": accession,
        "source_concept": "us-gaap:EarningsPerShareBasic",
        "form": "10-K",
        "filed_date": "2024-02-01",
        "fiscal_year": "2023",
        "fiscal_period": "FY",
    }
    fact_units = units or {"USD/shares": observations}
    facts_member = {
        "facts": {
            "us-gaap": {
                "EarningsPerShareBasic": {"units": fact_units},
            }
        }
    }
    member_name = "CIK0000000001.json"
    for archive_name, payload in (
        ("companyfacts.zip", facts_member),
        ("submissions.zip", {"filings": {"recent": {}}}),
    ):
        with zipfile.ZipFile(tmp_path / archive_name, "w") as archive:
            archive.writestr(member_name, json.dumps(payload))

    audit_row = {
        "ticker": "A",
        "statement_type": statement_type,
        "period_end": "2023-12-31",
        "public_date": "2024-02-02",
        "metric_sources": json.dumps(
            {"basic_eps": origin} if origin_present else {}
        ),
        "inherited_metrics": "[]",
    }
    export_row = {
        "ticker": "A",
        "statement_type": statement_type,
        "period_end": "2023-12-31",
        "public_date": "2024-02-02",
        "values": {"basic_eps": 2.0},
    }
    trace = analyze_bounded_archives(
        tmp_path,
        {"A": {"cik": "1", "company_name": "Example"}},
        {"A": [audit_row]},
        {"A": [export_row]},
    )["symbols"]["A"]["exact_export_audit_companyfacts_trace"]
    counts = trace["metric_source_link_counts"]["basic_eps"]
    example = next(
        (
            row
            for row in trace["sample_origin_trace_examples"]
            if row["metric"] == "basic_eps"
        ),
        None,
    )
    return counts, example


@pytest.mark.parametrize(
    ("observations", "units", "statement_type", "expected_disposition"),
    [
        (
            [fact(period_end="2020-12-31", period_start="2020-01-01")],
            None,
            "annual",
            "no_exact_period_end",
        ),
        (
            [fact(period_start="2023-10-01")],
            None,
            "annual",
            "no_compatible_duration_family",
        ),
        ([fact()], {"USD": [fact()]}, "annual", "no_expected_unit"),
        ([fact(), fact()], None, "annual", "ambiguous_multiple_qualifying_facts"),
        ([fact(value=2.1)], None, "annual", "source_value_mismatch"),
        ([fact(period_start="")], None, "balance", "no_compatible_statement_family"),
    ],
)
def test_bounded_archive_trace_keeps_each_negative_control_in_denominator(
    tmp_path, observations, units, statement_type, expected_disposition
):
    counts, example = trace_disposition(
        tmp_path, observations, units, statement_type=statement_type
    )

    assert counts["exported_nonempty_values"] == 1
    assert counts["metric_source_links"] == 1
    assert counts["csv_value_not_found_in_origin_facts"] == 1
    assert counts[f"disposition_{expected_disposition}"] == 1
    assert example["match_disposition"] == expected_disposition


def test_review_probe_origin_only_match_is_rejected_by_public_trace(tmp_path):
    prior_style_candidates = [fact(period_end="2020-12-31", period_start="2020-01-01")]
    assert any(candidate["value"] == 2.0 for candidate in prior_style_candidates)

    counts, example = trace_disposition(tmp_path, prior_style_candidates)

    assert example["export_period_end"] == "2023-12-31"
    assert example["match_disposition"] == "no_exact_period_end"
    assert counts["exported_nonempty_values"] == 1
    assert counts["csv_value_not_found_in_origin_facts"] == 1


def test_missing_origin_link_remains_in_nonempty_value_denominator(tmp_path):
    counts, _ = trace_disposition(tmp_path, [], origin_present=False)

    assert counts["exported_nonempty_values"] == 1
    assert counts["no_metric_source_link"] == 1
