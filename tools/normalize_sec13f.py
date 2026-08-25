"""Normalize one official SEC Form 13F quarterly data-set ZIP.

This is an offline, deliberately bounded extraction boundary.  It consumes the
official ``SUBMISSION.tsv``, ``COVERPAGE.tsv`` and ``INFOTABLE.tsv`` members of
one quarterly 13F data set and emits the institutional CSV contract consumed by
``tools.build_pit_supplemental``.

The extractor does not resolve issuers, fetch data, or carry state between
quarters.  CUSIP-to-symbol mappings are therefore an explicit, dated input.
The effective mapping date is the public ``as_of_date`` (the first supplied
trading day strictly after the filing date).  Rows whose CUSIP is not declared
in the mapping are ignored.  Overlapping mappings for a CUSIP are rejected.

The output is an isolated-quarter observation.  ``previous_holder_count`` is
set to zero by design; a multi-quarter caller must replace it from the prior
quarter's normalized state before publishing a strict CANSLIM artifact.
"""

from __future__ import annotations

import argparse
import bisect
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import zipfile
from collections import defaultdict
from typing import Iterable, Mapping


_INSTITUTIONAL_FIELDS = (
    "symbol",
    "as_of_date",
    "ownership_percent",
    "holder_count",
    "previous_holder_count",
    "evidence_ids",
)
_MAPPING_FIELDS = ("cusip", "symbol", "effective_start", "effective_end", "evidence_ids")
_SHARES_FIELDS = ("symbol", "as_of_date", "shares_outstanding", "evidence_ids")
_TRADING_FIELDS = ("trade_date",)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,7}\Z")
_CUSIP = re.compile(r"[A-Z0-9]{9}\Z")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")


@dataclass(frozen=True)
class _Mapping:
    cusip: str
    symbol: str
    start: date
    end: date
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Shares:
    symbol: str
    as_of: date
    shares: Decimal
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Filing:
    accession: str
    filing_date: date
    as_of_date: date
    cik: str
    report_period: date
    amendment_number: int


@dataclass(frozen=True)
class NormalizationResult:
    """Bounded metadata for a successfully written normalized CSV."""

    output: Path
    selected_filings: int
    position_groups: int
    output_rows: int
    ignored_non_target_rows: int
    skipped_missing_denominator: int


def _iso(value: str, field: str) -> date:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{field} must be a date")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            parsed = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        return parsed
    raise ValueError(f"{field} must be YYYY-MM-DD or DD-MON-YYYY")


def _symbol(value: str, field: str = "symbol") -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise ValueError(f"{field} must be uppercase and trimmed")
    return value


def _cusip(value: str) -> str:
    if not isinstance(value, str) or _CUSIP.fullmatch(value) is None:
        raise ValueError("cusip must be exactly nine uppercase letters or digits")
    return value


def _strings(value: str, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be a JSON array of strings") from exc
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(item, str) or not item or item.strip() != item for item in parsed)
        or len(set(parsed)) != len(parsed)
    ):
        raise ValueError(f"{field} must be a non-empty JSON array of unique strings")
    return tuple(sorted(parsed))


def _decimal(value: str, field: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (AttributeError, InvalidOperation) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        raise ValueError(f"{field} must be a finite {'positive' if positive else 'non-negative'} decimal")
    return parsed


def _read_csv(path: Path, fields: tuple[str, ...], label: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != fields:
                raise ValueError(f"{label} header must be exactly {','.join(fields)}")
            for row in reader:
                if row.get(None) is not None or any(value is None for value in row.values()):
                    raise ValueError(f"{label} row {reader.line_num} has the wrong number of fields")
                rows.append({field: row[field] for field in fields})
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"{label} is malformed: {exc}") from exc
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows


def _read_mapping(path: Path) -> dict[str, tuple[_Mapping, ...]]:
    by_cusip: dict[str, list[_Mapping]] = defaultdict(list)
    for row in _read_csv(path, _MAPPING_FIELDS, "CUSIP mapping CSV"):
        cusip = _cusip(row["cusip"])
        symbol = _symbol(row["symbol"])
        start = _iso(row["effective_start"], "mapping effective_start")
        end = date.max if not row["effective_end"] else _iso(row["effective_end"], "mapping effective_end")
        if end < start:
            raise ValueError("mapping effective_end precedes effective_start")
        evidence = _strings(row["evidence_ids"], "mapping evidence_ids")
        by_cusip[cusip].append(_Mapping(cusip, symbol, start, end, evidence))
    normalized: dict[str, tuple[_Mapping, ...]] = {}
    for cusip, entries in by_cusip.items():
        ordered = sorted(entries, key=lambda item: (item.start, item.end, item.symbol, item.evidence_ids))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.end >= current.start:
                raise ValueError(f"ambiguous overlapping target CUSIP mapping: {cusip}")
        normalized[cusip] = tuple(ordered)
    return normalized


def _read_shares(path: Path) -> dict[str, tuple[_Shares, ...]]:
    by_symbol: dict[str, list[_Shares]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    for row in _read_csv(path, _SHARES_FIELDS, "PIT shares CSV"):
        symbol = _symbol(row["symbol"])
        as_of = _iso(row["as_of_date"], "shares as_of_date")
        key = (symbol, as_of)
        if key in seen:
            raise ValueError(f"duplicate PIT shares row: {symbol} {as_of.isoformat()}")
        seen.add(key)
        by_symbol[symbol].append(
            _Shares(symbol, as_of, _decimal(row["shares_outstanding"], "shares_outstanding", positive=True), _strings(row["evidence_ids"], "shares evidence_ids"))
        )
    return {symbol: tuple(sorted(rows, key=lambda item: item.as_of)) for symbol, rows in by_symbol.items()}


def _read_trading_days(path: Path) -> tuple[date, ...]:
    rows = _read_csv(path, _TRADING_FIELDS, "trading-days CSV")
    result: list[date] = []
    seen: set[date] = set()
    for row in rows:
        parsed = _iso(row["trade_date"], "trade_date")
        if parsed in seen:
            raise ValueError(f"duplicate trade_date: {parsed.isoformat()}")
        seen.add(parsed)
        result.append(parsed)
    return tuple(sorted(result))


def _members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    found: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        base = name.rsplit("/", 1)[-1].upper()
        if base in {"SUBMISSION.TSV", "COVERPAGE.TSV", "INFOTABLE.TSV"}:
            if info.is_dir() or base in found:
                raise ValueError(f"13F ZIP has duplicate or directory member: {info.filename}")
            found[base] = info
    required = {"SUBMISSION.TSV", "COVERPAGE.TSV", "INFOTABLE.TSV"}
    if set(found) != required:
        raise ValueError("13F ZIP must contain SUBMISSION.tsv, COVERPAGE.tsv, and INFOTABLE.tsv")
    return found


def _tsv_rows(zf: zipfile.ZipFile, info: zipfile.ZipInfo, required: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with zf.open(info, "r") as binary:
            stream = binary.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"13F member {info.filename} is not valid UTF-8") from exc
    reader = csv.DictReader(stream.splitlines(), delimiter="\t", strict=True)
    fields = tuple(reader.fieldnames or ())
    if len(set(fields)) != len(fields) or any(field not in fields for field in required):
        raise ValueError(f"13F member {info.filename} lacks required fields")
    rows: list[dict[str, str]] = []
    try:
        for row in reader:
            if row.get(None) is not None or any(value is None for value in row.values()):
                raise ValueError(f"13F member {info.filename} has a malformed row")
            rows.append(dict(row))
    except csv.Error as exc:
        raise ValueError(f"13F member {info.filename} is malformed: {exc}") from exc
    return rows


def _first_after(trading_days: tuple[date, ...], filed: date) -> date:
    index = bisect.bisect_right(trading_days, filed)
    if index >= len(trading_days):
        raise ValueError(f"no trading day strictly after filing date {filed.isoformat()}")
    return trading_days[index]


def _amendment_number(form: str, raw: str) -> int:
    if raw:
        if not raw.isdigit():
            raise ValueError("AMENDMENTNO must be a non-negative integer")
        return int(raw)
    return 1 if form.upper().endswith("/A") else 0


def _select_filings(
    submissions: Iterable[Mapping[str, str]],
    covers: Iterable[Mapping[str, str]],
    trading_days: tuple[date, ...],
) -> dict[str, _Filing]:
    cover_by_accession: dict[str, Mapping[str, str]] = {}
    for cover in covers:
        accession = cover.get("ACCESSION_NUMBER", "")
        if not _ACCESSION.fullmatch(accession):
            raise ValueError("COVERPAGE ACCESSION_NUMBER is invalid")
        if accession in cover_by_accession:
            raise ValueError(f"duplicate COVERPAGE accession: {accession}")
        cover_by_accession[accession] = cover

    candidates: dict[tuple[str, date], tuple[tuple[int, date, str], _Filing]] = {}
    seen_accessions: set[str] = set()
    for submission in submissions:
        accession = submission.get("ACCESSION_NUMBER", "")
        if not _ACCESSION.fullmatch(accession):
            raise ValueError("SUBMISSION ACCESSION_NUMBER is invalid")
        if accession in seen_accessions:
            raise ValueError(f"duplicate SUBMISSION accession: {accession}")
        seen_accessions.add(accession)
        form = submission.get("SUBMISSIONTYPE", "").strip().upper()
        if form not in {"13F-HR", "13F-HR/A"}:
            continue
        if accession not in cover_by_accession:
            raise ValueError(f"selected filing lacks COVERPAGE: {accession}")
        filing_date = _iso(submission.get("FILING_DATE", ""), "SUBMISSION FILING_DATE")
        report_period = _iso(submission.get("PERIODOFREPORT", ""), "SUBMISSION PERIODOFREPORT")
        cover = cover_by_accession[accession]
        cover_period = _iso(cover.get("REPORTCALENDARORQUARTER", ""), "COVERPAGE REPORTCALENDARORQUARTER")
        if cover_period != report_period:
            raise ValueError(f"submission/cover report period mismatch: {accession}")
        cik = submission.get("CIK", "").strip()
        if not cik or not cik.isdigit() or len(cik) > 10:
            raise ValueError(f"SUBMISSION CIK is invalid: {accession}")
        amendment = _amendment_number(form, cover.get("AMENDMENTNO", "").strip())
        as_of = _first_after(trading_days, filing_date)
        filing = _Filing(accession, filing_date, as_of, cik, report_period, amendment)
        key = (cik, report_period)
        rank = (amendment, filing_date, accession)
        previous = candidates.get(key)
        if previous is None or rank > previous[0]:
            candidates[key] = (rank, filing)
    return {filing.accession: filing for _, filing in candidates.values()}


def _mapping_for(mapping: Mapping[str, tuple[_Mapping, ...]], cusip: str, as_of: date) -> _Mapping | None:
    for entry in mapping.get(cusip, ()):
        if entry.start <= as_of <= entry.end:
            return entry
    return None


def _fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalize_13f(
    *,
    thirteenf_zip: Path,
    cusip_mapping_csv: Path,
    shares_csv: Path,
    trading_days_csv: Path,
    output: Path,
) -> NormalizationResult:
    """Normalize one quarterly ZIP into the strict institutional CSV contract."""

    mapping = _read_mapping(cusip_mapping_csv)
    shares = _read_shares(shares_csv)
    trading_days = _read_trading_days(trading_days_csv)
    with zipfile.ZipFile(thirteenf_zip, "r") as zf:
        members = _members(zf)
        submissions = _tsv_rows(zf, members["SUBMISSION.TSV"], ("ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"))
        covers = _tsv_rows(zf, members["COVERPAGE.TSV"], ("ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "AMENDMENTNO"))
        selected = _select_filings(submissions, covers, trading_days)
        selected_accessions = set(selected)
        info_rows = _tsv_rows(zf, members["INFOTABLE.TSV"], ("ACCESSION_NUMBER", "INFOTABLE_SK", "CUSIP", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"))

    positions: dict[tuple[str, date, str], Decimal] = defaultdict(Decimal)
    evidence: dict[tuple[str, date, str], set[str]] = defaultdict(set)
    ignored = 0
    seen_info_keys: set[tuple[str, str]] = set()
    for row in info_rows:
        accession = row.get("ACCESSION_NUMBER", "")
        if accession not in selected_accessions:
            continue
        row_id = row.get("INFOTABLE_SK", "").strip()
        info_key = (accession, row_id)
        if not row_id or info_key in seen_info_keys:
            raise ValueError(f"duplicate or empty INFOTABLE_SK: {accession}")
        seen_info_keys.add(info_key)
        cusip = row.get("CUSIP", "").strip().upper()
        if cusip not in mapping:
            ignored += 1
            continue
        filing = selected[accession]
        mapped = _mapping_for(mapping, cusip, filing.as_of_date)
        if mapped is None:
            ignored += 1
            continue
        if row.get("SSHPRNAMTTYPE", "").strip().upper() != "SH" or row.get("PUTCALL", "").strip().upper():
            ignored += 1
            continue
        quantity = _decimal(row.get("SSHPRNAMT", ""), "INFOTABLE SSHPRNAMT")
        if quantity == 0:
            ignored += 1
            continue
        key = (mapped.symbol, filing.as_of_date, filing.cik)
        positions[key] += quantity
        evidence[key].update((f"sec13f:{accession}:coverpage", f"sec13f:{accession}:infotable:{row_id}"))

    grouped: dict[tuple[str, date], dict[str, Decimal]] = defaultdict(dict)
    grouped_evidence: dict[tuple[str, date], set[str]] = defaultdict(set)
    for (symbol, as_of, cik), quantity in positions.items():
        grouped[(symbol, as_of)][cik] = quantity
        grouped_evidence[(symbol, as_of)].update(evidence[(symbol, as_of, cik)])

    output_rows: list[tuple[str, str, str, str, str, str]] = []
    skipped_missing = 0
    for (symbol, as_of), manager_positions in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        available = shares.get(symbol, ())
        index = bisect.bisect_right([entry.as_of for entry in available], as_of) - 1
        if index < 0:
            skipped_missing += 1
            continue
        denominator = available[index]
        ownership = sum(manager_positions.values(), Decimal(0)) / denominator.shares
        if ownership > 1:
            raise ValueError(f"derived ownership exceeds 100% for {symbol} at {as_of.isoformat()}")
        evidence_ids = sorted({*grouped_evidence[(symbol, as_of)], *denominator.evidence_ids})
        output_rows.append((symbol, as_of.isoformat(), _fmt_decimal(ownership), str(len(manager_positions)), "0", json.dumps(evidence_ids, separators=(",", ":"))))

    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(f"{output}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial output already exists")
    try:
        with partial.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(_INSTITUTIONAL_FIELDS)
            writer.writerows(output_rows)
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise
    return NormalizationResult(output, len(selected), len(grouped), len(output_rows), ignored, skipped_missing)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--13f-zip", dest="thirteenf_zip", type=Path, required=True)
    parser.add_argument("--cusip-mapping-csv", "--cusip-mapping", dest="cusip_mapping_csv", type=Path, required=True)
    parser.add_argument("--shares-csv", "--pit-shares-csv", dest="shares_csv", type=Path, required=True)
    parser.add_argument("--trading-days-csv", "--trading-days", dest="trading_days_csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = normalize_13f(
        thirteenf_zip=args.thirteenf_zip,
        cusip_mapping_csv=args.cusip_mapping_csv,
        shares_csv=args.shares_csv,
        trading_days_csv=args.trading_days_csv,
        output=args.output,
    )
    print(json.dumps({
        "ignored_non_target_rows": result.ignored_non_target_rows,
        "output": str(result.output),
        "output_rows": result.output_rows,
        "position_groups": result.position_groups,
        "selected_filings": result.selected_filings,
        "skipped_missing_denominator": result.skipped_missing_denominator,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
