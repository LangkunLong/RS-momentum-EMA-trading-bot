"""Export point-in-time shares denominators from the SEC fundamentals export.

This is an offline preparation boundary for :mod:`tools.normalize_sec13f`.  It
joins the normalized fundamentals rows to their audit rows, chooses the latest
period-end observation publicly available on each normalized public date, and
emits the exact dated shares contract required by the 13F normalizer.  It does
not fill missing symbols or infer shares from prices.  Coverage is recorded as
partial when requested target symbols are absent.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping


_FUNDAMENTALS_FIELDS = (
    "ticker", "statement_type", "period_end", "public_date", "basic_eps",
    "diluted_eps", "total_revenue", "net_income", "common_stock",
    "total_stockholders_equity", "shares_outstanding",
    "held_percent_institutions", "institution_count", "prev_institution_count",
)
_AUDIT_FIELDS = (
    "ticker", "statement_type", "period_end", "public_date",
    "accession_number", "form", "filed_date", "fiscal_year", "fiscal_period",
    "acceptance_datetime", "public_date_basis", "source_concepts",
    "inherited_metrics", "metric_sources",
)
_SHARES_FIELDS = ("symbol", "as_of_date", "shares_outstanding", "evidence_ids")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,7}\Z")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")


@dataclass(frozen=True)
class ExportResult:
    output: Path
    metadata_output: Path | None
    output_rows: int
    covered_symbols: tuple[str, ...]
    uncovered_symbols: tuple[str, ...]
    conflicting_keys: tuple[tuple[str, str], ...]


def _regular_file(path: str | Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    return candidate


def _new_output(path: str | Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(f"{field} already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _symbol(value: str, field: str = "ticker") -> str:
    if not isinstance(value, str) or value != value.strip() or _SYMBOL.fullmatch(value) is None:
        raise ValueError(f"{field} must be uppercase and trimmed")
    return value


def _decimal(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (AttributeError, InvalidOperation) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _fmt(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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


def _row_key(row: Mapping[str, str], label: str) -> tuple[str, str, str, str]:
    return (
        _symbol(row["ticker"], f"{label} ticker"),
        row["statement_type"],
        _iso(row["period_end"], f"{label} period_end"),
        _iso(row["public_date"], f"{label} public_date"),
    )


def _acceptance(value: str, field: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _audit_public_timestamp(row: Mapping[str, str]) -> tuple[datetime, str]:
    raw = row["acceptance_datetime"].strip()
    if raw:
        return _acceptance(raw, "audit acceptance_datetime"), "acceptance"
    filed = _iso(row["filed_date"], "audit filed_date")
    return datetime.combine(date.fromisoformat(filed), time.min, tzinfo=timezone.utc), "filed"


def _audit_evidence(row: Mapping[str, str]) -> tuple[str, ...]:
    accession = row["accession_number"].strip()
    if _ACCESSION.fullmatch(accession) is None:
        raise ValueError("audit accession_number is invalid")
    parsed, basis = _audit_public_timestamp(row)
    prefix = "acceptance" if basis == "acceptance" else "filed"
    evidence = {
        f"sec-fundamentals:{accession}:shares_outstanding",
        f"sec-fundamentals:{prefix}:{parsed.isoformat().replace('+00:00', 'Z')}",
    }
    return tuple(sorted(evidence))


def _write_csv(path: Path, rows: Iterable[tuple[str, str, str, str]]) -> None:
    partial = Path(f"{path}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial shares output already exists")
    try:
        with partial.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(_SHARES_FIELDS)
            writer.writerows(rows)
        partial.replace(path)
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def export_pit_shares(
    *,
    fundamentals_csv: str | Path,
    fundamentals_audit_csv: str | Path,
    output: str | Path,
    metadata_output: str | Path | None = None,
    data_cutoff: str | None = None,
    target_symbols: Iterable[str] | None = None,
) -> ExportResult:
    fundamentals_path = _regular_file(fundamentals_csv, "fundamentals_csv")
    audit_path = _regular_file(fundamentals_audit_csv, "fundamentals_audit_csv")
    output_path = _new_output(output, "output")
    metadata_path = None if metadata_output is None else _new_output(metadata_output, "metadata_output")
    if metadata_path == output_path:
        raise ValueError("output and metadata_output must differ")
    cutoff = None if data_cutoff is None else _iso(data_cutoff, "data_cutoff")
    target = None if target_symbols is None else tuple(sorted({_symbol(item, "target symbol") for item in target_symbols}))

    fundamentals = _read_csv(fundamentals_path, _FUNDAMENTALS_FIELDS, "fundamentals")
    audits = _read_csv(audit_path, _AUDIT_FIELDS, "fundamentals audit")
    fundamental_map: dict[tuple[str, str, str, str], dict[str, str]] = {}
    audit_map: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in fundamentals:
        key = _row_key(row, "fundamentals")
        if key in fundamental_map:
            raise ValueError(f"duplicate fundamentals row: {key}")
        if cutoff is not None and key[3] > cutoff:
            continue
        fundamental_map[key] = row
    for row in audits:
        key = _row_key(row, "audit")
        if key in audit_map:
            raise ValueError(f"duplicate audit row: {key}")
        if cutoff is not None and key[3] > cutoff:
            continue
        audit_map[key] = row
    if set(fundamental_map) != set(audit_map):
        raise ValueError("fundamentals and audit rows do not match")

    candidates: dict[tuple[str, str], list[tuple[str, Decimal, datetime, tuple[str, ...]]]] = {}
    for key, row in fundamental_map.items():
        raw_shares = row["shares_outstanding"].strip()
        if not raw_shares or raw_shares.casefold() in {"nan", "null", "none"}:
            continue
        symbol, _statement, period_end, public_date = key
        shares = _decimal(raw_shares, "shares_outstanding")
        if shares <= 0:
            continue
        audit = audit_map[key]
        acceptance, _basis = _audit_public_timestamp(audit)
        evidence = _audit_evidence(audit)
        candidates.setdefault((symbol, public_date), []).append((period_end, shares, acceptance, evidence))

    selected: list[tuple[str, str, Decimal, set[str]]] = []
    conflicts: list[tuple[str, str]] = []
    for (symbol, public_date), rows in sorted(candidates.items()):
        latest_period = max(row[0] for row in rows)
        latest = [row for row in rows if row[0] == latest_period]
        values = {row[1] for row in latest}
        if len(values) != 1:
            conflicts.append((symbol, public_date))
            continue
        shares = next(iter(values))
        evidence = set().union(*(row[3] for row in latest))
        selected.append((symbol, public_date, shares, evidence))
    if conflicts:
        raise ValueError(f"conflicting shares_outstanding observations: {conflicts[:5]}")

    target_set = None if target is None else set(target)
    covered = tuple(sorted({row[0] for row in selected} & (target_set if target_set is not None else {row[0] for row in selected})))
    uncovered = () if target_set is None else tuple(sorted(target_set - set(covered)))
    output_rows = tuple(
        (symbol, public_date, _fmt(shares), json.dumps(sorted(evidence), separators=(",", ":")))
        for symbol, public_date, shares, evidence in selected
        if target_set is None or symbol in target_set
    )
    try:
        _write_csv(output_path, output_rows)
        if metadata_path is not None:
            manifest = {
                "coverage_status": "complete" if not uncovered else "partial",
                "fundamentals_csv_sha256": _sha256(fundamentals_path),
                "fundamentals_audit_csv_sha256": _sha256(audit_path),
                "data_cutoff": cutoff,
                "target_symbol_count": None if target is None else len(target),
                "covered_symbols": list(covered),
                "uncovered_symbols": list(uncovered),
                "output_row_count": len(output_rows),
                "schema_version": 1,
            }
            partial = Path(f"{metadata_path}.partial")
            if partial.exists() or partial.is_symlink():
                raise ValueError("partial metadata output already exists")
            partial.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
            partial.replace(metadata_path)
    except Exception:
        output_path.unlink(missing_ok=True)
        if metadata_path is not None:
            metadata_path.unlink(missing_ok=True)
        raise
    return ExportResult(output_path, metadata_path, len(output_rows), covered, uncovered, tuple(conflicts))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fundamentals-csv", type=Path, required=True)
    parser.add_argument("--fundamentals-audit-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--data-cutoff")
    parser.add_argument("--target-symbol", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = export_pit_shares(
        fundamentals_csv=args.fundamentals_csv,
        fundamentals_audit_csv=args.fundamentals_audit_csv,
        output=args.output,
        metadata_output=args.metadata_output,
        data_cutoff=args.data_cutoff,
        target_symbols=tuple(args.target_symbol) if args.target_symbol else None,
    )
    print(json.dumps({
        "covered_symbols": list(result.covered_symbols),
        "output": str(result.output),
        "output_rows": result.output_rows,
        "uncovered_symbols": list(result.uncovered_symbols),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
