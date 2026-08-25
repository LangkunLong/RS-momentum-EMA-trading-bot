from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tools.export_pit_shares import export_pit_shares


FUNDAMENTALS_FIELDS = (
    "ticker",
    "statement_type",
    "period_end",
    "public_date",
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "common_stock",
    "total_stockholders_equity",
    "shares_outstanding",
    "held_percent_institutions",
    "institution_count",
    "prev_institution_count",
)
AUDIT_FIELDS = (
    "ticker",
    "statement_type",
    "period_end",
    "public_date",
    "accession_number",
    "form",
    "filed_date",
    "fiscal_year",
    "fiscal_period",
    "acceptance_datetime",
    "public_date_basis",
    "source_concepts",
    "inherited_metrics",
    "metric_sources",
)


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fundamental(
    ticker: str,
    period_end: str,
    public_date: str,
    shares: str,
    statement_type: str = "balance",
) -> dict[str, str]:
    return {
        "ticker": ticker,
        "statement_type": statement_type,
        "period_end": period_end,
        "public_date": public_date,
        "basic_eps": "",
        "diluted_eps": "",
        "total_revenue": "",
        "net_income": "",
        "common_stock": "",
        "total_stockholders_equity": "",
        "shares_outstanding": shares,
        "held_percent_institutions": "",
        "institution_count": "",
        "prev_institution_count": "",
    }


def _audit(
    ticker: str,
    period_end: str,
    public_date: str,
    accession: str,
    acceptance: str,
    statement_type: str = "balance",
) -> dict[str, str]:
    return {
        "ticker": ticker,
        "statement_type": statement_type,
        "period_end": period_end,
        "public_date": public_date,
        "accession_number": accession,
        "form": "10-Q",
        "filed_date": acceptance[:10],
        "fiscal_year": "2024",
        "fiscal_period": "Q1",
        "acceptance_datetime": acceptance,
        "public_date_basis": "acceptance_datetime",
        "source_concepts": '{"shares_outstanding":"dei:EntityCommonStockSharesOutstanding"}',
        "inherited_metrics": "",
        "metric_sources": json.dumps(
            {
                "shares_outstanding": {
                    "accession_number": accession,
                    "acceptance_datetime": acceptance,
                    "source_concept": "dei:EntityCommonStockSharesOutstanding",
                }
            },
            separators=(",", ":"),
        ),
    }


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    fundamentals = tmp_path / "fundamentals.csv"
    audit = tmp_path / "fundamentals_audit.csv"
    _write_csv(
        fundamentals,
        FUNDAMENTALS_FIELDS,
        [
            _fundamental("AAA", "2024-03-31", "2024-05-03", "1.0E3"),
            _fundamental("AAA", "2024-06-30", "2024-08-02", "1200"),
            _fundamental("BBB", "2024-03-31", "2024-05-03", ""),
        ],
    )
    _write_csv(
        audit,
        AUDIT_FIELDS,
        [
            _audit("AAA", "2024-03-31", "2024-05-03", "0000000001-24-000001", "2024-05-02T17:00:00Z"),
            _audit("AAA", "2024-06-30", "2024-08-02", "0000000001-24-000002", "2024-08-01T17:00:00Z"),
            _audit("BBB", "2024-03-31", "2024-05-03", "0000000002-24-000001", "2024-05-02T17:01:00Z"),
        ],
    )
    return fundamentals, audit


def test_export_selects_positive_rows_and_emits_audit_evidence(tmp_path: Path) -> None:
    fundamentals, audit = _inputs(tmp_path)
    output = tmp_path / "shares.csv"
    metadata = tmp_path / "shares.metadata.json"

    result = export_pit_shares(
        fundamentals_csv=fundamentals,
        fundamentals_audit_csv=audit,
        output=output,
        metadata_output=metadata,
        data_cutoff="2024-12-31",
        target_symbols=("AAA", "BBB", "CCC"),
    )

    assert result.output_rows == 2
    assert result.covered_symbols == ("AAA",)
    assert result.uncovered_symbols == ("BBB", "CCC")
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["symbol"], row["as_of_date"], row["shares_outstanding"]) for row in rows] == [
        ("AAA", "2024-05-03", "1000"),
        ("AAA", "2024-08-02", "1200"),
    ]
    evidence = set(json.loads(rows[0]["evidence_ids"]))
    assert "sec-fundamentals:0000000001-24-000001:shares_outstanding" in evidence
    assert "sec-fundamentals:acceptance:2024-05-02T17:00:00Z" in evidence
    manifest = json.loads(metadata.read_text(encoding="utf-8"))
    assert manifest["coverage_status"] == "partial"
    assert manifest["uncovered_symbols"] == ["BBB", "CCC"]
    assert manifest["target_symbol_count"] == 3
    assert manifest["output_row_count"] == 2


def test_export_rejects_conflicting_same_public_key(tmp_path: Path) -> None:
    fundamentals, audit = _inputs(tmp_path)
    with fundamentals.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FUNDAMENTALS_FIELDS, lineterminator="\n")
        writer.writerow(_fundamental("AAA", "2024-06-30", "2024-08-02", "1300", "quarterly"))
    with audit.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_FIELDS, lineterminator="\n")
        writer.writerow(_audit("AAA", "2024-06-30", "2024-08-02", "0000000001-24-000003", "2024-08-01T18:00:00Z", "quarterly"))

    with pytest.raises(ValueError, match="conflicting shares_outstanding observations"):
        export_pit_shares(
            fundamentals_csv=fundamentals,
            fundamentals_audit_csv=audit,
            output=tmp_path / "shares.csv",
            metadata_output=tmp_path / "shares.metadata.json",
            data_cutoff="2024-12-31",
            target_symbols=("AAA",),
        )


def test_export_requires_matching_fundamental_audit_rows(tmp_path: Path) -> None:
    fundamentals, audit = _inputs(tmp_path)
    lines = audit.read_text(encoding="utf-8").splitlines()
    audit.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fundamentals and audit rows do not match"):
        export_pit_shares(
            fundamentals_csv=fundamentals,
            fundamentals_audit_csv=audit,
            output=tmp_path / "shares.csv",
            metadata_output=tmp_path / "shares.metadata.json",
            data_cutoff="2024-12-31",
            target_symbols=("AAA",),
        )
