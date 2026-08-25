"""Normalize official SEC Form 13F data-set ZIPs into PIT institutional CSVs.

This is an offline, deliberately bounded extraction boundary.  It consumes the
official ``SUBMISSION.tsv``, ``COVERPAGE.tsv`` and ``INFOTABLE.tsv`` members of
one quarterly 13F data set and emits the institutional CSV contract consumed by
``tools.build_pit_supplemental``.

The extractor does not resolve issuers, fetch data, or carry state between
quarters.  CUSIP-to-symbol mappings are therefore an explicit, dated input.
The effective mapping date is the public ``as_of_date`` (the first supplied
trading day strictly after the filing date).  Rows whose CUSIP is not declared
in the mapping are ignored.  Overlapping mappings for a CUSIP are rejected.

The output of the single-quarter path is an isolated report-period observation
and sets ``previous_holder_count`` to zero by design.  When a ZIP contains
staggered manager filing dates, all selected managers for a report period are
consolidated at the latest selected filing-visible session.  This is
deliberately conservative: the snapshot is not exposed until every included
manager's selected filing is public, and amendments cannot rewrite an earlier
snapshot.  The manifest path assembles those immutable quarter outputs without
reopening SEC data: it computes the prior available holder count strictly by
symbol and public snapshot date.
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
_MANIFEST_FIELDS = ("quarter", "institutional_csv", "source_reference", "evidence_ids")
_MAPPING_FIELDS = ("cusip", "symbol", "effective_start", "effective_end", "evidence_ids")
_SHARES_FIELDS = ("symbol", "as_of_date", "shares_outstanding", "evidence_ids")
_TRADING_FIELDS = ("trade_date",)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,7}\Z")
_CUSIP = re.compile(r"[A-Z0-9]{9}\Z")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_QUARTER = re.compile(r"(?P<year>[0-9]{4})Q(?P<number>[1-4])\Z")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")


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


@dataclass(frozen=True)
class AssemblyResult:
    """Bounded metadata for a successfully assembled institutional CSV."""

    output: Path
    quarter_count: int
    input_rows: int
    output_rows: int
    source_references: tuple[str, ...]


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


def _latest_report_period_filings(selected: Mapping[str, _Filing]) -> dict[str, _Filing]:
    """Keep the latest report period represented by a filing-window archive.

    SEC quarterly data sets are filing-window archives, so a late amendment or
    delinquent filing can carry an older report period alongside the primary
    quarter.  The single-quarter output contract is one consolidated report
    period; retaining older periods would create duplicate symbol/as-of rows
    when their filing dates share a public session with the primary quarter.
    """

    if not selected:
        return {}
    latest = max(filing.report_period for filing in selected.values())
    return {
        accession: filing
        for accession, filing in selected.items()
        if filing.report_period == latest
    }


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
        selected = _latest_report_period_filings(_select_filings(submissions, covers, trading_days))
        selected_accessions = set(selected)
        info_rows = _tsv_rows(zf, members["INFOTABLE.TSV"], ("ACCESSION_NUMBER", "INFOTABLE_SK", "CUSIP", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"))

    report_period_as_of: dict[date, date] = {}
    for filing in selected.values():
        report_period_as_of[filing.report_period] = max(
            report_period_as_of.get(filing.report_period, filing.as_of_date),
            filing.as_of_date,
        )

    positions: dict[tuple[str, date, date, str], Decimal] = defaultdict(Decimal)
    evidence: dict[tuple[str, date, date, str], set[str]] = defaultdict(set)
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
        as_of = report_period_as_of[filing.report_period]
        mapped = _mapping_for(mapping, cusip, as_of)
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
        key = (mapped.symbol, as_of, filing.report_period, filing.cik)
        positions[key] += quantity
        evidence[key].update((f"sec13f:{accession}:coverpage", f"sec13f:{accession}:infotable:{row_id}"))

    grouped: dict[tuple[str, date, date], dict[str, Decimal]] = defaultdict(dict)
    grouped_evidence: dict[tuple[str, date, date], set[str]] = defaultdict(set)
    for (symbol, as_of, report_period, cik), quantity in positions.items():
        grouped[(symbol, as_of, report_period)][cik] = quantity
        grouped_evidence[(symbol, as_of, report_period)].update(evidence[(symbol, as_of, report_period, cik)])

    output_rows: list[tuple[str, str, str, str, str, str]] = []
    emitted_keys: set[tuple[str, date]] = set()
    skipped_missing = 0
    for (symbol, as_of, report_period), manager_positions in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])):
        if (symbol, as_of) in emitted_keys:
            raise ValueError(
                f"multiple report periods produce the same symbol snapshot: {symbol} {as_of.isoformat()}"
            )
        available = shares.get(symbol, ())
        index = bisect.bisect_right([entry.as_of for entry in available], as_of) - 1
        if index < 0:
            skipped_missing += 1
            continue
        denominator = available[index]
        ownership = sum(manager_positions.values(), Decimal(0)) / denominator.shares
        if ownership > 1:
            raise ValueError(f"derived ownership exceeds 100% for {symbol} at {as_of.isoformat()}")
        evidence_ids = sorted({*grouped_evidence[(symbol, as_of, report_period)], *denominator.evidence_ids})
        output_rows.append((symbol, as_of.isoformat(), _fmt_decimal(ownership), str(len(manager_positions)), "0", json.dumps(evidence_ids, separators=(",", ":"))))
        emitted_keys.add((symbol, as_of))

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


@dataclass(frozen=True)
class _ManifestEntry:
    quarter: str
    quarter_key: tuple[int, int]
    institutional_csv: Path
    source_reference: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _InstitutionalInput:
    symbol: str
    as_of: date
    ownership: Decimal
    holder_count: int
    evidence_ids: tuple[str, ...]


def _regular_file(path: Path, field: str) -> Path:
    raw = Path(path)
    if raw.is_symlink() or not raw.is_file():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    candidate = raw.resolve()
    if candidate.is_symlink():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    return candidate


def _safe_reference(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} must be non-empty, trimmed, and free of control characters")
    return value


def _quarter(value: object, field: str) -> tuple[str, tuple[int, int]]:
    if not isinstance(value, str) or value.strip() != value:
        raise ValueError(f"{field} must be YYYYQn")
    match = _QUARTER.fullmatch(value.upper())
    if match is None:
        raise ValueError(f"{field} must be YYYYQn")
    key = (int(match.group("year")), int(match.group("number")))
    return f"{key[0]:04d}Q{key[1]}", key


def _manifest_evidence(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return _strings(value, field)
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array of strings")
    return _strings(json.dumps(value, ensure_ascii=False, separators=(",", ":")), field)


def _manifest_entry(raw: Mapping[str, object], *, manifest_dir: Path, row_label: str) -> _ManifestEntry:
    if set(raw) != set(_MANIFEST_FIELDS):
        raise ValueError(f"{row_label} fields must be exactly {','.join(_MANIFEST_FIELDS)}")
    quarter, quarter_key = _quarter(raw["quarter"], f"{row_label} quarter")
    source_reference = _safe_reference(raw["source_reference"], f"{row_label} source_reference")
    evidence_ids = _manifest_evidence(raw["evidence_ids"], f"{row_label} evidence_ids")
    file_value = raw["institutional_csv"]
    if not isinstance(file_value, str) or not file_value or file_value.strip() != file_value:
        raise ValueError(f"{row_label} institutional_csv must be a non-empty trimmed path")
    candidate = Path(file_value)
    if not candidate.is_absolute():
        candidate = manifest_dir / candidate
    institutional_csv = _regular_file(candidate, f"{row_label} institutional_csv")
    return _ManifestEntry(quarter, quarter_key, institutional_csv, source_reference, evidence_ids)


def _read_quarter_manifest(path: Path) -> tuple[_ManifestEntry, ...]:
    manifest_path = _regular_file(path, "quarter manifest")
    manifest_dir = manifest_path.parent
    if manifest_path.suffix.casefold() == ".json":
        try:
            value = json.loads(
                manifest_path.read_text(encoding="utf-8-sig"),
                parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            )
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("quarter manifest JSON is invalid") from exc
        if isinstance(value, list):
            raw_entries = value
        elif isinstance(value, dict) and set(value) == {"schema_version", "quarters"} and value.get("schema_version") == 1:
            raw_entries = value["quarters"]
        else:
            raise ValueError("quarter manifest JSON must be a list or schema-version-1 object")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise ValueError("quarter manifest must contain at least one quarter")
        entries = tuple(
            _manifest_entry(item, manifest_dir=manifest_dir, row_label=f"manifest entry {index + 1}")
            for index, item in enumerate(raw_entries)
            if isinstance(item, Mapping)
        )
        if len(entries) != len(raw_entries):
            raise ValueError("quarter manifest entries must be objects")
    else:
        try:
            with manifest_path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                if tuple(reader.fieldnames or ()) != _MANIFEST_FIELDS:
                    raise ValueError(f"quarter manifest CSV header must be exactly {','.join(_MANIFEST_FIELDS)}")
                raw_entries: list[dict[str, object]] = []
                for row in reader:
                    if row.get(None) is not None or any(value is None for value in row.values()):
                        raise ValueError(f"quarter manifest row {reader.line_num} has the wrong number of fields")
                    raw_entries.append(dict(row))
        except UnicodeDecodeError as exc:
            raise ValueError("quarter manifest CSV must be UTF-8") from exc
        except csv.Error as exc:
            raise ValueError(f"quarter manifest CSV is malformed: {exc}") from exc
        if not raw_entries:
            raise ValueError("quarter manifest must contain at least one quarter")
        entries = tuple(
            _manifest_entry(item, manifest_dir=manifest_dir, row_label=f"manifest row {index + 2}")
            for index, item in enumerate(raw_entries)
        )

    seen_quarters: set[str] = set()
    seen_paths: set[Path] = set()
    seen_sources: set[str] = set()
    previous_key: tuple[int, int] | None = None
    for entry in entries:
        if entry.quarter in seen_quarters:
            raise ValueError(f"duplicate quarter in manifest: {entry.quarter}")
        if entry.institutional_csv in seen_paths:
            raise ValueError(f"duplicate institutional CSV in manifest: {entry.institutional_csv}")
        if entry.source_reference in seen_sources:
            raise ValueError(f"duplicate source_reference in manifest: {entry.source_reference}")
        if previous_key is not None and entry.quarter_key <= previous_key:
            raise ValueError("quarter manifest must be strictly chronological")
        seen_quarters.add(entry.quarter)
        seen_paths.add(entry.institutional_csv)
        seen_sources.add(entry.source_reference)
        previous_key = entry.quarter_key
    return entries


def _read_institutional_csv(path: Path, *, label: str, data_cutoff: date | None) -> list[_InstitutionalInput]:
    rows: list[_InstitutionalInput] = []
    try:
        with _regular_file(path, label).open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != _INSTITUTIONAL_FIELDS:
                raise ValueError(f"{label} header must be exactly {','.join(_INSTITUTIONAL_FIELDS)}")
            for row in reader:
                if row.get(None) is not None or any(value is None for value in row.values()):
                    raise ValueError(f"{label} row {reader.line_num} has the wrong number of fields")
                symbol = _symbol(row["symbol"], f"{label} symbol")
                as_of = _iso(row["as_of_date"], f"{label} as_of_date")
                if data_cutoff is not None and as_of > data_cutoff:
                    raise ValueError(f"{label} contains a row after data_cutoff")
                ownership = _decimal(row["ownership_percent"], f"{label} ownership_percent")
                if ownership > 1:
                    raise ValueError(f"{label} ownership_percent must be in [0,1]")
                holder_value = row["holder_count"]
                if _INTEGER.fullmatch(holder_value) is None:
                    raise ValueError(f"{label} holder_count must be a non-negative integer")
                rows.append(_InstitutionalInput(symbol, as_of, ownership, int(holder_value), _strings(row["evidence_ids"], f"{label} evidence_ids")))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"{label} is malformed: {exc}") from exc
    return rows


def _write_assembled(path: Path, rows: Iterable[tuple[str, str, str, str, str, str]]) -> None:
    raw_output = Path(path)
    if raw_output.is_symlink():
        raise ValueError("output must not be a symlink")
    output = raw_output.resolve()
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
            writer.writerows(rows)
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise


def assemble_institutional_csv(
    *,
    manifest: Path,
    output: Path,
    data_cutoff: str | None = None,
) -> AssemblyResult:
    """Merge quarter-normalized institutional CSVs in an explicit manifest order.

    The manifest order is authoritative and must contain strictly increasing
    ``YYYYQn`` labels.  Each input file must be one aligned public snapshot date;
    staggered filing dates are rejected because this isolated-quarter CSV does
    not carry manager-level state needed to reconstruct a complete snapshot.  A
    symbol's previous holder count is taken from its latest earlier observation,
    never from a same-day row or from an input file's precomputed previous value.
    ``source_reference`` and manifest ``evidence_ids`` are unioned into every
    row from that quarter so provenance is retained in the strict CSV contract.
    """

    cutoff = None if data_cutoff is None else _iso(data_cutoff, "data_cutoff")
    entries = _read_quarter_manifest(manifest)
    output_path = Path(output).resolve()
    if any(output_path == entry.institutional_csv for entry in entries):
        raise ValueError("output must differ from every manifest input")

    by_quarter: list[tuple[_ManifestEntry, list[_InstitutionalInput]]] = []
    input_rows = 0
    for entry in entries:
        rows = _read_institutional_csv(entry.institutional_csv, label=f"{entry.quarter} institutional CSV", data_cutoff=cutoff)
        dates = [row.as_of for row in rows]
        if dates:
            if len(set(dates)) != 1:
                raise ValueError(
                    f"{entry.quarter} institutional CSV must contain one aligned as_of_date; "
                    "staggered filing snapshots require a manager-level intermediate"
                )
        input_rows += len(rows)
        by_quarter.append((entry, rows))

    observations: dict[tuple[str, date], tuple[Decimal, int, set[str]]] = {}
    for entry, rows in by_quarter:
        for row in rows:
            key = (row.symbol, row.as_of)
            if key in observations:
                raise ValueError(f"duplicate institutional snapshot: {row.symbol} {row.as_of.isoformat()}")
            evidence = {*row.evidence_ids, *entry.evidence_ids, entry.source_reference}
            observations[key] = (row.ownership, row.holder_count, evidence)

    previous_max: date | None = None
    for _entry, rows in by_quarter:
        if not rows:
            continue
        minimum, maximum = min(row.as_of for row in rows), max(row.as_of for row in rows)
        if previous_max is not None and minimum <= previous_max:
            raise ValueError("quarter input date ranges must be strictly chronological")
        previous_max = maximum

    prior_by_symbol: dict[str, int] = {}
    assembled: list[tuple[str, str, str, str, str, str]] = []
    for (symbol, as_of), (ownership, holder_count, evidence) in sorted(observations.items(), key=lambda item: item[0]):
        previous_holder_count = prior_by_symbol.get(symbol, 0)
        prior_by_symbol[symbol] = holder_count
        assembled.append(
            (
                symbol,
                as_of.isoformat(),
                _fmt_decimal(ownership),
                str(holder_count),
                str(previous_holder_count),
                json.dumps(sorted(evidence), ensure_ascii=False, separators=(",", ":")),
            )
        )
    _write_assembled(output_path, assembled)
    return AssemblyResult(
        output=output_path,
        quarter_count=len(entries),
        input_rows=input_rows,
        output_rows=len(assembled),
        source_references=tuple(entry.source_reference for entry in entries),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--13f-zip", dest="thirteenf_zip", type=Path)
    source.add_argument("--quarter-manifest", "--manifest", dest="manifest", type=Path)
    parser.add_argument("--cusip-mapping-csv", "--cusip-mapping", dest="cusip_mapping_csv", type=Path)
    parser.add_argument("--shares-csv", "--pit-shares-csv", dest="shares_csv", type=Path)
    parser.add_argument("--trading-days-csv", "--trading-days", dest="trading_days_csv", type=Path)
    parser.add_argument("--data-cutoff")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.manifest is not None:
        if any(value is not None for value in (args.cusip_mapping_csv, args.shares_csv, args.trading_days_csv)):
            raise ValueError("quarter-manifest mode does not accept ZIP normalization inputs")
        result = assemble_institutional_csv(manifest=args.manifest, output=args.output, data_cutoff=args.data_cutoff)
        print(json.dumps({
            "input_rows": result.input_rows,
            "output": str(result.output),
            "output_rows": result.output_rows,
            "quarter_count": result.quarter_count,
            "source_references": list(result.source_references),
        }, sort_keys=True))
        return 0
    if any(value is None for value in (args.cusip_mapping_csv, args.shares_csv, args.trading_days_csv)):
        raise ValueError("single-quarter mode requires mapping, shares, and trading-days CSVs")
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
