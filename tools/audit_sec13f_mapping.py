"""Build a review-only CUSIP-to-symbol candidate queue from a 13F archive.

This tool is intentionally not a normalizer input.  SEC Form 13F data is
as-filed and contains issuer/class/CUSIP, but not a ticker symbol.  The output
therefore labels name-based matches as candidates and never writes the strict
``cusip,symbol,effective_start,effective_end,evidence_ids`` mapping contract.
Production mappings still require dated instrument evidence (for example, a
SEC issuer filing with ``dei:TradingSymbol``) and human review of every
ambiguous class or historical-name case.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
import zipfile

from tools.normalize_sec13f import _amendment_number, _members, _tsv_rows


_SECURITY_FIELDS = (
    "ticker",
    "cik",
    "company_name",
    "first_membership_date",
    "last_membership_date",
    "mapping_basis",
)
_SHARES_FIELDS = ("symbol", "as_of_date", "shares_outstanding", "evidence_ids")
_OUTPUT_FIELDS = (
    "symbol",
    "cik",
    "company_name",
    "candidate_status",
    "candidate_cusips",
    "candidate_issuer_names",
    "candidate_titles",
    "selected_filing_count",
    "evidence_ids",
)
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_LEGAL_SUFFIXES = {
    "ADR",
    "CLASS",
    "COM",
    "CORP",
    "CORPORATION",
    "INC",
    "INCORPORATED",
    "LTD",
    "PLC",
    "PUBLIC",
    "SA",
    "THE",
    "CO",
    "COMPANY",
}


def _date(value: str, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be YYYY-MM-DD or DD-MON-YYYY")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field} must be YYYY-MM-DD or DD-MON-YYYY")


def _read_csv(path: Path, fields: tuple[str, ...], label: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f"{label} header must be exactly {','.join(fields)}")
        rows: list[dict[str, str]] = []
        for row in reader:
            if row.get(None) is not None or any(value is None for value in row.values()):
                raise ValueError(f"{label} row {reader.line_num} has the wrong number of fields")
            rows.append({field: row[field] for field in fields})
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _name_tokens(value: str) -> tuple[str, ...]:
    text = _NON_ALNUM.sub(" ", value.upper()).strip()
    return tuple(token for token in text.split() if token not in _LEGAL_SUFFIXES)


def _nonzero(value: str) -> bool:
    try:
        return Decimal(value.strip()) != 0
    except (AttributeError, InvalidOperation) as exc:
        raise ValueError("INFOTABLE SSHPRNAMT must be numeric") from exc


def _target_symbols(shares_rows: Iterable[Mapping[str, str]], report_period: date) -> set[str]:
    symbols: set[str] = set()
    for row in shares_rows:
        as_of = _date(row["as_of_date"], "shares as_of_date")
        if as_of <= report_period:
            symbols.add(row["symbol"].strip().upper())
    return symbols


def _selected_filings(
    submissions: Iterable[Mapping[str, str]],
    covers: Iterable[Mapping[str, str]],
    report_period: date,
) -> dict[str, Mapping[str, str]]:
    cover_by_accession = {row["ACCESSION_NUMBER"]: row for row in covers}
    selected: dict[str, tuple[tuple[int, date, str], Mapping[str, str]]] = {}
    for row in submissions:
        accession = row.get("ACCESSION_NUMBER", "")
        if row.get("SUBMISSIONTYPE", "").strip().upper() not in {"13F-HR", "13F-HR/A"}:
            continue
        if _date(row.get("PERIODOFREPORT", ""), "SUBMISSION PERIODOFREPORT") != report_period:
            continue
        cover = cover_by_accession.get(accession)
        if cover is None:
            raise ValueError(f"selected filing lacks COVERPAGE: {accession}")
        if _date(cover.get("REPORTCALENDARORQUARTER", ""), "COVERPAGE REPORTCALENDARORQUARTER") != report_period:
            raise ValueError(f"submission/cover report period mismatch: {accession}")
        filing_date = _date(row.get("FILING_DATE", ""), "SUBMISSION FILING_DATE")
        cik = row.get("CIK", "").strip()
        rank = (_amendment_number(row.get("SUBMISSIONTYPE", ""), cover.get("AMENDMENTNO", "").strip()), filing_date, accession)
        previous = selected.get(cik)
        if previous is None or rank > previous[0]:
            selected[cik] = (rank, row)
    return {row["ACCESSION_NUMBER"]: row for _, row in selected.values()}


def build_review_queue(
    *,
    thirteenf_zip: Path,
    security_master_csv: Path,
    shares_csv: Path,
    report_period: date,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    security_rows = _read_csv(security_master_csv, _SECURITY_FIELDS, "security master CSV")
    shares_rows = _read_csv(shares_csv, _SHARES_FIELDS, "PIT shares CSV")
    target_symbols = _target_symbols(shares_rows, report_period)
    security_by_symbol = {
        row["ticker"].strip().upper(): row
        for row in security_rows
        if row["ticker"].strip().upper() in target_symbols
    }
    by_name: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for symbol, row in security_by_symbol.items():
        by_name[_name_tokens(row["company_name"])].add(symbol)

    candidates: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"cusips": set(), "issuers": set(), "titles": set(), "accessions": set()})
    with zipfile.ZipFile(thirteenf_zip, "r") as archive:
        members = _members(archive)
        submissions = _tsv_rows(archive, members["SUBMISSION.TSV"], ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"))
        covers = _tsv_rows(archive, members["COVERPAGE.TSV"], ("ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "AMENDMENTNO"))
        selected = _selected_filings(submissions, covers, report_period)
        info_rows = _tsv_rows(archive, members["INFOTABLE.TSV"], ("ACCESSION_NUMBER", "INFOTABLE_SK", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"))

    for row in info_rows:
        if row.get("ACCESSION_NUMBER") not in selected:
            continue
        if row.get("SSHPRNAMTTYPE", "").strip().upper() != "SH" or row.get("PUTCALL", "").strip() or not _nonzero(row.get("SSHPRNAMT", "")):
            continue
        issuer = row.get("NAMEOFISSUER", "").strip()
        symbols = by_name.get(_name_tokens(issuer), set())
        if len(symbols) != 1:
            continue
        symbol = next(iter(symbols))
        cusip = row.get("CUSIP", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{9}", cusip):
            continue
        entry = candidates[symbol]
        entry["cusips"].add(cusip)
        entry["issuers"].add(issuer)
        entry["titles"].add(row.get("TITLEOFCLASS", "").strip())
        entry["accessions"].add(row["ACCESSION_NUMBER"])

    output: list[dict[str, str]] = []
    counts = {"target_symbols": len(target_symbols), "security_master_symbols": len(security_by_symbol), "matched_symbols": 0, "unique_cusip_symbols": 0, "ambiguous_symbols": 0, "unresolved_symbols": 0}
    for symbol in sorted(target_symbols):
        security = security_by_symbol.get(symbol)
        entry = candidates.get(symbol)
        cusips = sorted(entry["cusips"]) if entry else []
        issuers = sorted(entry["issuers"]) if entry else []
        titles = sorted(entry["titles"]) if entry else []
        if not entry:
            status = "unresolved"
            counts["unresolved_symbols"] += 1
        elif len(cusips) == 1 and len(titles) == 1:
            status = "candidate_unique"
            counts["matched_symbols"] += 1
            counts["unique_cusip_symbols"] += 1
        else:
            status = "ambiguous"
            counts["matched_symbols"] += 1
            counts["ambiguous_symbols"] += 1
        evidence = [f"sec13f:{accession}:report-period:{report_period.isoformat()}" for accession in sorted(entry["accessions"])] if entry else []
        output.append({
            "symbol": symbol,
            "cik": security["cik"] if security else "",
            "company_name": security["company_name"] if security else "",
            "candidate_status": status,
            "candidate_cusips": json.dumps(cusips, separators=(",", ":")),
            "candidate_issuer_names": json.dumps(issuers, separators=(",", ":")),
            "candidate_titles": json.dumps(titles, separators=(",", ":")),
            "selected_filing_count": str(len(entry["accessions"]) if entry else 0),
            "evidence_ids": json.dumps(evidence, separators=(",", ":")),
        })
    return output, counts


def _write_csv(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    path = path.resolve()
    if path.exists() or path.is_symlink():
        raise ValueError("output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(f"{path}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial output already exists")
    try:
        with partial.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=_OUTPUT_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        partial.replace(path)
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--13f-zip", dest="thirteenf_zip", type=Path, required=True)
    parser.add_argument("--security-master-csv", type=Path, required=True)
    parser.add_argument("--shares-csv", type=Path, required=True)
    parser.add_argument("--report-period", type=lambda value: _date(value, "report-period"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows, counts = build_review_queue(
        thirteenf_zip=args.thirteenf_zip,
        security_master_csv=args.security_master_csv,
        shares_csv=args.shares_csv,
        report_period=args.report_period,
    )
    _write_csv(args.output, rows)
    summary = {"report_period": args.report_period.isoformat(), "warning": "review-only candidates; not a strict normalizer mapping", **counts}
    summary_path = args.summary_output.resolve()
    if summary_path.exists() or summary_path.is_symlink():
        raise ValueError("summary output already exists")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
