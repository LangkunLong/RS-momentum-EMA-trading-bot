from __future__ import annotations

import csv
import json
from pathlib import Path
import zipfile

import pytest

from tools.normalize_sec13f import normalize_13f


SUBMISSION_FIELDS = ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT")
COVER_FIELDS = ("ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "ISAMENDMENT", "AMENDMENTNO", "FILINGMANAGER_NAME")
INFO_FIELDS = ("ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL")


def _tsv(fields: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(fields)]
    lines.extend("\t".join(row.get(field, "") for field in fields) for row in rows)
    return "\n".join(lines) + "\n"


def _zip(path: Path, submissions: list[dict[str, str]], covers: list[dict[str, str]], infos: list[dict[str, str]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("2024q2/SUBMISSION.tsv", _tsv(SUBMISSION_FIELDS, submissions))
        archive.writestr("2024q2/COVERPAGE.tsv", _tsv(COVER_FIELDS, covers))
        archive.writestr("2024q2/INFOTABLE.tsv", _tsv(INFO_FIELDS, infos))


def _csv(path: Path, fields: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    zip_path = tmp_path / "2024q2_13f.zip"
    initial = "0000000001-24-000001"
    amendment = "0000000001-24-000002"
    second_manager = "0000000002-24-000001"
    submissions = [
        {"ACCESSION_NUMBER": initial, "FILING_DATE": "30-APR-2024", "SUBMISSIONTYPE": "13F-HR", "CIK": "0000000001", "PERIODOFREPORT": "31-MAR-2024"},
        {"ACCESSION_NUMBER": amendment, "FILING_DATE": "10-MAY-2024", "SUBMISSIONTYPE": "13F-HR/A", "CIK": "0000000001", "PERIODOFREPORT": "31-MAR-2024"},
        {"ACCESSION_NUMBER": second_manager, "FILING_DATE": "30-APR-2024", "SUBMISSIONTYPE": "13F-HR", "CIK": "0000000002", "PERIODOFREPORT": "31-MAR-2024"},
        {"ACCESSION_NUMBER": "0000000003-24-000001", "FILING_DATE": "30-APR-2024", "SUBMISSIONTYPE": "13F-NT", "CIK": "0000000003", "PERIODOFREPORT": "31-MAR-2024"},
    ]
    covers = [
        {"ACCESSION_NUMBER": initial, "REPORTCALENDARORQUARTER": "31-MAR-2024", "ISAMENDMENT": "N", "AMENDMENTNO": "", "FILINGMANAGER_NAME": "Initial"},
        {"ACCESSION_NUMBER": amendment, "REPORTCALENDARORQUARTER": "31-MAR-2024", "ISAMENDMENT": "Y", "AMENDMENTNO": "1", "FILINGMANAGER_NAME": "Initial"},
        {"ACCESSION_NUMBER": second_manager, "REPORTCALENDARORQUARTER": "31-MAR-2024", "ISAMENDMENT": "N", "AMENDMENTNO": "", "FILINGMANAGER_NAME": "Second"},
    ]
    infos = [
        {"ACCESSION_NUMBER": initial, "INFOTABLE_SK": "1", "CUSIP": "000000001", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": amendment, "INFOTABLE_SK": "1", "CUSIP": "000000001", "SSHPRNAMT": "250", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": second_manager, "INFOTABLE_SK": "1", "CUSIP": "000000001", "SSHPRNAMT": "100", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": amendment, "INFOTABLE_SK": "2", "CUSIP": "000000001", "SSHPRNAMT": "50", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": amendment, "INFOTABLE_SK": "3", "CUSIP": "999999999", "SSHPRNAMT": "900", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": amendment, "INFOTABLE_SK": "4", "CUSIP": "000000001", "SSHPRNAMT": "999", "SSHPRNAMTTYPE": "PRN", "PUTCALL": ""},
        {"ACCESSION_NUMBER": amendment, "INFOTABLE_SK": "5", "CUSIP": "000000001", "SSHPRNAMT": "999", "SSHPRNAMTTYPE": "SH", "PUTCALL": "PUT"},
    ]
    _zip(zip_path, submissions, covers, infos)
    mapping = tmp_path / "mapping.csv"
    _csv(mapping, ("cusip", "symbol", "effective_start", "effective_end", "evidence_ids"), [("000000001", "AAA", "2024-01-01", "2024-12-31", '["map:aaa"]')])
    shares = tmp_path / "shares.csv"
    _csv(shares, ("symbol", "as_of_date", "shares_outstanding", "evidence_ids"), [("AAA", "2024-04-01", "1000", '["shares:aaa"]')])
    trading = tmp_path / "trading.csv"
    _csv(trading, ("trade_date",), [("2024-04-30",), ("2024-05-01",), ("2024-05-13",)])
    return zip_path, mapping, shares, trading


def test_normalizer_selects_latest_amendment_and_emits_pit_contract(tmp_path: Path) -> None:
    zip_path, mapping, shares, trading = _inputs(tmp_path)
    output = tmp_path / "institutional.csv"

    result = normalize_13f(
        thirteenf_zip=zip_path,
        cusip_mapping_csv=mapping,
        shares_csv=shares,
        trading_days_csv=trading,
        output=output,
    )

    assert result.selected_filings == 2
    assert result.output_rows == 1
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    # The managers filed on different dates.  The normalizer exposes a single
    # conservative report-period snapshot at the latest selected filing date,
    # so all selected managers are public and the amendment replaces the
    # superseded initial filing without rewriting an earlier snapshot.
    assert [(row["symbol"], row["as_of_date"], row["ownership_percent"], row["holder_count"]) for row in rows] == [
        ("AAA", "2024-05-13", "0.4", "2"),
    ]
    evidence = set(json.loads(rows[0]["evidence_ids"]))
    assert "sec13f:0000000001-24-000002:coverpage" in evidence
    assert "sec13f:0000000001-24-000002:infotable:2" in evidence
    assert "shares:aaa" in evidence
    assert rows[0]["previous_holder_count"] == "0"


def test_normalizer_discards_older_report_periods_in_filing_window(tmp_path: Path) -> None:
    zip_path = tmp_path / "2024q2_13f.zip"
    latest = "0000000001-24-000001"
    older = "0000000009-24-000001"
    _zip(
        zip_path,
        [
            {"ACCESSION_NUMBER": latest, "FILING_DATE": "30-APR-2024", "SUBMISSIONTYPE": "13F-HR", "CIK": "0000000001", "PERIODOFREPORT": "31-MAR-2024"},
            {"ACCESSION_NUMBER": older, "FILING_DATE": "30-APR-2024", "SUBMISSIONTYPE": "13F-HR", "CIK": "0000000009", "PERIODOFREPORT": "31-DEC-2023"},
        ],
        [
            {"ACCESSION_NUMBER": latest, "REPORTCALENDARORQUARTER": "31-MAR-2024", "ISAMENDMENT": "N", "AMENDMENTNO": "", "FILINGMANAGER_NAME": "Latest"},
            {"ACCESSION_NUMBER": older, "REPORTCALENDARORQUARTER": "31-DEC-2023", "ISAMENDMENT": "N", "AMENDMENTNO": "", "FILINGMANAGER_NAME": "Older"},
        ],
        [
            {"ACCESSION_NUMBER": latest, "INFOTABLE_SK": "1", "CUSIP": "000000001", "SSHPRNAMT": "200", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
            {"ACCESSION_NUMBER": older, "INFOTABLE_SK": "1", "CUSIP": "000000001", "SSHPRNAMT": "900", "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        ],
    )
    mapping = tmp_path / "mapping.csv"
    _csv(mapping, ("cusip", "symbol", "effective_start", "effective_end", "evidence_ids"), [("000000001", "AAA", "2024-01-01", "2024-12-31", '["map:aaa"]')])
    shares = tmp_path / "shares.csv"
    _csv(shares, ("symbol", "as_of_date", "shares_outstanding", "evidence_ids"), [("AAA", "2024-04-01", "1000", '["shares:aaa"]')])
    trading = tmp_path / "trading.csv"
    _csv(trading, ("trade_date",), [("2024-04-30",), ("2024-05-01",)])

    output = tmp_path / "institutional.csv"
    result = normalize_13f(
        thirteenf_zip=zip_path,
        cusip_mapping_csv=mapping,
        shares_csv=shares,
        trading_days_csv=trading,
        output=output,
    )

    assert result.selected_filings == 1
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["symbol"], row["ownership_percent"], row["holder_count"]) for row in rows] == [("AAA", "0.2", "1")]


def test_overlapping_target_cusip_mapping_fails_closed(tmp_path: Path) -> None:
    zip_path, mapping, shares, trading = _inputs(tmp_path)
    _csv(mapping, ("cusip", "symbol", "effective_start", "effective_end", "evidence_ids"), [
        ("000000001", "AAA", "2024-01-01", "2024-06-30", '["map:a"]'),
        ("000000001", "BBB", "2024-06-01", "2024-12-31", '["map:b"]'),
    ])
    with pytest.raises(ValueError, match="ambiguous overlapping"):
        normalize_13f(thirteenf_zip=zip_path, cusip_mapping_csv=mapping, shares_csv=shares, trading_days_csv=trading, output=tmp_path / "out.csv")


def test_future_denominator_is_never_used(tmp_path: Path) -> None:
    zip_path, mapping, shares, trading = _inputs(tmp_path)
    _csv(shares, ("symbol", "as_of_date", "shares_outstanding", "evidence_ids"), [("AAA", "2024-06-01", "1000", '["future"]')])
    output = tmp_path / "institutional.csv"
    result = normalize_13f(thirteenf_zip=zip_path, cusip_mapping_csv=mapping, shares_csv=shares, trading_days_csv=trading, output=output)
    assert result.output_rows == 0
    assert result.skipped_missing_denominator == 1
    assert output.read_text(encoding="utf-8") == "symbol,as_of_date,ownership_percent,holder_count,previous_holder_count,evidence_ids\n"


def test_missing_trading_day_after_filing_fails_closed(tmp_path: Path) -> None:
    zip_path, mapping, shares, _ = _inputs(tmp_path)
    trading = tmp_path / "trading.csv"
    _csv(trading, ("trade_date",), [("2024-04-30",)])
    with pytest.raises(ValueError, match="strictly after filing date"):
        normalize_13f(thirteenf_zip=zip_path, cusip_mapping_csv=mapping, shares_csv=shares, trading_days_csv=trading, output=tmp_path / "out.csv")


def test_v5_bundle_export_keeps_missing_denominator_and_both_dates(tmp_path: Path) -> None:
    zip_path, mapping, shares, trading = _inputs(tmp_path)
    _csv(
        shares,
        ("symbol", "as_of_date", "shares_outstanding", "evidence_ids"),
        [("AAA", "2024-06-01", "1000", '["future-denominator"]')],
    )
    legacy_output = tmp_path / "institutional.csv"
    bundle_output = tmp_path / "institutional-v5.csv"

    result = normalize_13f(
        thirteenf_zip=zip_path,
        cusip_mapping_csv=mapping,
        shares_csv=shares,
        trading_days_csv=trading,
        output=legacy_output,
        pit_bundle_output=bundle_output,
    )

    assert result.output_rows == 0
    assert result.skipped_missing_denominator == 1
    assert legacy_output.read_text(encoding="utf-8") == (
        "symbol,as_of_date,ownership_percent,holder_count,previous_holder_count,evidence_ids\n"
    )
    with bundle_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["statement_type"] == "institutional"
    assert rows[0]["period_end"] == "2024-03-31"
    assert rows[0]["public_date"] == "2024-05-13"
    assert rows[0]["shares_outstanding"] == ""
    assert rows[0]["held_percent_institutions"] == ""
    assert rows[0]["institution_count"] == "2"
    assert rows[0]["prev_institution_count"] == ""



def test_v5_bundle_export_includes_denominator_when_available(tmp_path: Path) -> None:
    zip_path, mapping, shares, trading = _inputs(tmp_path)
    legacy_output = tmp_path / "institutional.csv"
    bundle_output = tmp_path / "institutional-v5.csv"

    result = normalize_13f(
        thirteenf_zip=zip_path,
        cusip_mapping_csv=mapping,
        shares_csv=shares,
        trading_days_csv=trading,
        output=legacy_output,
        pit_bundle_output=bundle_output,
    )

    assert result.pit_bundle_output == bundle_output.resolve()
    assert result.pit_bundle_rows == 1
    with bundle_output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["period_end"] == "2024-03-31"
    assert rows[0]["public_date"] == "2024-05-13"
    assert rows[0]["shares_outstanding"] == "1000"
    assert rows[0]["held_percent_institutions"] == "0.4"
    assert rows[0]["institution_count"] == "2"
    assert rows[0]["prev_institution_count"] == ""


def test_v5_dual_output_rolls_back_first_publish_when_second_link_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.normalize_sec13f as normalizer

    zip_path, mapping, shares, trading = _inputs(tmp_path)
    legacy_output = tmp_path / "institutional.csv"
    bundle_output = tmp_path / "institutional-v5.csv"
    original_link = normalizer.os.link
    calls = 0

    def fail_second_link(source, target) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second publication failure")
        original_link(source, target)

    monkeypatch.setattr(normalizer.os, "link", fail_second_link)
    with pytest.raises(OSError, match="injected second publication failure"):
        normalize_13f(
            thirteenf_zip=zip_path,
            cusip_mapping_csv=mapping,
            shares_csv=shares,
            trading_days_csv=trading,
            output=legacy_output,
            pit_bundle_output=bundle_output,
        )

    assert calls == 2
    assert not legacy_output.exists()
    assert not bundle_output.exists()
    assert not Path(f"{legacy_output}.partial").exists()
    assert not Path(f"{bundle_output}.partial").exists()
