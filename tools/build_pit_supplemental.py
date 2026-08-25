"""Build a deterministic, hash-pinned PIT I/L supplemental SQLite artifact.

The inputs are already-normalized, offline CSV exports.  This utility only
validates their declared as-of dates and evidence fields; it does not fetch,
infer, or manufacture institutional or industry data.  The resulting SQLite
file conforms to :class:`SQLiteSupplementalPITProvider` and is written
atomically.  A separate canonical provenance manifest records the input file
digests and row counts; its digest is sealed in the SQLite metadata table.

Example::

    python -m tools.build_pit_supplemental \
        --institutional-csv institutional.csv \
        --industry-csv industry.csv \
        --source-kind offline-13f-industry-export \
        --data-cutoff 2025-12-31 \
        --output supplemental.sqlite3 \
        --provenance-output supplemental.provenance.json

The CSV contracts are intentionally strict.  ``ownership_percent`` is a
fraction in ``[0, 1]`` (not a percentage in ``[0, 100]``), and both
``evidence_ids`` and ``group_members`` are JSON arrays of strings.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from core.pit_diagnosis.rulebook import canonical_sha256
from core.pit_diagnosis.supplemental import SQLiteSupplementalPITProvider


_SCHEMA_VERSION = "1"
_MAX_ROWS = 1_000_000
_INSTITUTIONAL_FIELDS = (
    "symbol",
    "as_of_date",
    "ownership_percent",
    "holder_count",
    "previous_holder_count",
    "evidence_ids",
)
_INDUSTRY_FIELDS = (
    "symbol",
    "as_of_date",
    "group_id",
    "group_rank",
    "group_members",
    "evidence_ids",
)
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")


@dataclass(frozen=True)
class _InstitutionalRow:
    symbol: str
    as_of_date: str
    ownership_percent: float
    holder_count: int
    previous_holder_count: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class _IndustryRow:
    symbol: str
    as_of_date: str
    group_id: str
    group_rank: int
    group_members: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class BuildResult:
    """Identity and bounded counts for a successfully built artifact."""

    output: Path
    provenance_output: Path | None
    sha256: str
    provenance_sha256: str
    data_cutoff: str
    institutional_rows: int
    industry_rows: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file(path: Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{field} must be a regular, non-symlink file")
    return candidate


def _output_path(path: Path, field: str) -> Path:
    candidate = Path(path).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise ValueError(f"{field} already exists")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _iso_date(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _symbol(value: str, field: str = "symbol") -> str:
    if not isinstance(value, str) or not value or value.strip() != value or value.upper() != value:
        raise ValueError(f"{field} must be uppercase and trimmed")
    return value


def _integer(value: str, field: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a non-negative integer")
    return int(value)


def _json_strings(value: str, field: str, *, sort_values: bool = True) -> tuple[str, ...]:
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be a JSON array of strings") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{field} must be a non-empty JSON array")
    if any(not isinstance(item, str) or not item or item.strip() != item for item in parsed):
        raise ValueError(f"{field} must contain non-empty trimmed strings")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(sorted(parsed) if sort_values else parsed)


def _json_array(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _read_csv(path: Path, fields: tuple[str, ...], *, max_rows: int, kind: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if tuple(reader.fieldnames or ()) != fields:
                raise ValueError(f"{kind} CSV header must be exactly {','.join(fields)}")
            for row in reader:
                if row.get(None) is not None or any(value is None for value in row.values()):
                    raise ValueError(f"{kind} CSV row {reader.line_num} has the wrong number of fields")
                if len(rows) >= max_rows:
                    raise ValueError(f"{kind} CSV exceeds max_rows={max_rows}")
                rows.append({field: row[field] for field in fields})
    except UnicodeDecodeError as exc:
        raise ValueError(f"{kind} CSV must be UTF-8") from exc
    except csv.Error as exc:
        raise ValueError(f"{kind} CSV is malformed: {exc}") from exc
    return rows


def _parse_institutional(rows: list[dict[str, str]], cutoff: str) -> list[_InstitutionalRow]:
    parsed: list[_InstitutionalRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        symbol = _symbol(row["symbol"])
        as_of = _iso_date(row["as_of_date"], "institutional as_of_date")
        if as_of > cutoff:
            raise ValueError("institutional row is after data_cutoff")
        key = (symbol, as_of)
        if key in seen:
            raise ValueError(f"duplicate institutional snapshot: {symbol} {as_of}")
        seen.add(key)
        try:
            ownership = float(row["ownership_percent"])
        except (TypeError, ValueError) as exc:
            raise ValueError("institutional ownership_percent must be a finite fraction") from exc
        if not math.isfinite(ownership) or not 0.0 <= ownership <= 1.0:
            raise ValueError("institutional ownership_percent must be a finite fraction in [0,1]")
        holder_count = _integer(row["holder_count"], "institutional holder_count")
        previous_holder_count = _integer(row["previous_holder_count"], "institutional previous_holder_count")
        evidence_ids = _json_strings(row["evidence_ids"], "institutional evidence_ids")
        parsed.append(
            _InstitutionalRow(
                symbol,
                as_of,
                ownership,
                holder_count,
                previous_holder_count,
                evidence_ids,
            )
        )
    return sorted(parsed, key=lambda item: (item.symbol, item.as_of_date))


def _parse_industry(rows: list[dict[str, str]], cutoff: str) -> list[_IndustryRow]:
    parsed: list[_IndustryRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        symbol = _symbol(row["symbol"])
        as_of = _iso_date(row["as_of_date"], "industry as_of_date")
        if as_of > cutoff:
            raise ValueError("industry row is after data_cutoff")
        key = (symbol, as_of)
        if key in seen:
            raise ValueError(f"duplicate industry snapshot: {symbol} {as_of}")
        seen.add(key)
        group_id = row["group_id"]
        if not group_id or group_id.strip() != group_id:
            raise ValueError("industry group_id must be non-empty and trimmed")
        group_rank = _integer(row["group_rank"], "industry group_rank")
        if group_rank <= 0:
            raise ValueError("industry group_rank must be positive")
        group_members = _json_strings(row["group_members"], "industry group_members")
        normalized_members = tuple(sorted(_symbol(member, "industry group member") for member in group_members))
        if symbol not in normalized_members:
            raise ValueError("industry group_members must include symbol")
        evidence_ids = _json_strings(row["evidence_ids"], "industry evidence_ids")
        parsed.append(
            _IndustryRow(
                symbol,
                as_of,
                group_id,
                group_rank,
                normalized_members,
                evidence_ids,
            )
        )
    return sorted(parsed, key=lambda item: (item.symbol, item.as_of_date))


def _provenance(
    *,
    source_kind: str,
    data_cutoff: str,
    institutional_path: Path,
    industry_path: Path,
    institutional_rows: int,
    industry_rows: int,
    source_references: tuple[str, ...],
) -> dict[str, Any]:
    """Return the canonical, path-independent provenance manifest."""

    return {
        "data_cutoff": data_cutoff,
        "inputs": [
            {
                "kind": "institutional",
                "row_count": institutional_rows,
                "sha256": _sha256_file(institutional_path),
            },
            {
                "kind": "industry_group",
                "row_count": industry_rows,
                "sha256": _sha256_file(industry_path),
            },
        ],
        "schema_version": _SCHEMA_VERSION,
        "source_references": list(source_references),
        "source_kind": source_kind,
    }


def _write_sqlite(
    path: Path,
    *,
    source_kind: str,
    data_cutoff: str,
    provenance_sha256: str,
    institutional: list[_InstitutionalRow],
    industry: list[_IndustryRow],
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA page_size=4096;
            PRAGMA auto_vacuum=NONE;
            PRAGMA journal_mode=DELETE;
            BEGIN;
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE institutional_snapshots(
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                ownership_percent REAL NOT NULL,
                holder_count INTEGER NOT NULL,
                previous_holder_count INTEGER NOT NULL,
                evidence_ids TEXT NOT NULL,
                PRIMARY KEY(symbol, as_of_date)
            );
            CREATE TABLE industry_group_snapshots(
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                group_id TEXT NOT NULL,
                group_rank INTEGER NOT NULL,
                group_members TEXT NOT NULL,
                evidence_ids TEXT NOT NULL,
                PRIMARY KEY(symbol, as_of_date)
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            sorted(
                (
                    ("data_cutoff", data_cutoff),
                    ("provenance_sha256", provenance_sha256),
                    ("schema_version", _SCHEMA_VERSION),
                    ("source_kind", source_kind),
                )
            ),
        )
        connection.executemany(
            "INSERT INTO institutional_snapshots VALUES (?,?,?,?,?,?)",
            [
                (
                    row.symbol,
                    row.as_of_date,
                    row.ownership_percent,
                    row.holder_count,
                    row.previous_holder_count,
                    _json_array(row.evidence_ids),
                )
                for row in institutional
            ],
        )
        connection.executemany(
            "INSERT INTO industry_group_snapshots VALUES (?,?,?,?,?,?)",
            [
                (
                    row.symbol,
                    row.as_of_date,
                    row.group_id,
                    row.group_rank,
                    _json_array(row.group_members),
                    _json_array(row.evidence_ids),
                )
                for row in industry
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def build_artifact(
    *,
    institutional_csv: Path,
    industry_csv: Path,
    source_kind: str,
    data_cutoff: str,
    output: Path,
    provenance_output: Path | None = None,
    source_references: tuple[str, ...] = (),
    max_rows: int = _MAX_ROWS,
) -> BuildResult:
    """Validate normalized CSVs and atomically build a sealed SQLite artifact.

    ``source_kind`` describes the user-supplied source and is recorded as
    metadata; this function does not assert that the source is authoritative.
    Every row must carry its own evidence IDs and an as-of date at or before
    ``data_cutoff``.  Existing outputs are never overwritten.
    """

    if not isinstance(source_kind, str) or not source_kind or source_kind.strip() != source_kind:
        raise ValueError("source_kind must be non-empty and trimmed")
    if any(ord(char) < 32 for char in source_kind):
        raise ValueError("source_kind must not contain control characters")
    cutoff = _iso_date(data_cutoff, "data_cutoff")
    if not isinstance(source_references, tuple) or any(
        not isinstance(reference, str)
        or not reference
        or reference.strip() != reference
        or any(ord(char) < 32 for char in reference)
        for reference in source_references
    ):
        raise ValueError("source_references must be a tuple of non-empty, trimmed references")
    if len(set(source_references)) != len(source_references):
        raise ValueError("source_references must not contain duplicates")
    if type(max_rows) is not int or not 0 < max_rows <= _MAX_ROWS:
        raise ValueError(f"max_rows must be between 1 and {_MAX_ROWS}")

    institutional_path = _regular_file(institutional_csv, "institutional_csv")
    industry_path = _regular_file(industry_csv, "industry_csv")
    output_path = _output_path(output, "output")
    provenance_path = None if provenance_output is None else _output_path(provenance_output, "provenance_output")
    if output_path in {institutional_path, industry_path} or provenance_path in {institutional_path, industry_path}:
        raise ValueError("output paths must differ from CSV inputs")
    if provenance_path is not None and provenance_path == output_path:
        raise ValueError("output and provenance_output must differ")

    institutional = _parse_institutional(
        _read_csv(institutional_path, _INSTITUTIONAL_FIELDS, max_rows=max_rows, kind="institutional"),
        cutoff,
    )
    industry = _parse_industry(
        _read_csv(industry_path, _INDUSTRY_FIELDS, max_rows=max_rows, kind="industry"),
        cutoff,
    )
    if not institutional or not industry:
        raise ValueError("both CSV inputs must contain at least one validated row")

    manifest = _provenance(
        source_kind=source_kind,
        data_cutoff=cutoff,
        institutional_path=institutional_path,
        industry_path=industry_path,
        institutional_rows=len(institutional),
        industry_rows=len(industry),
        source_references=tuple(sorted(source_references)),
    )
    provenance_sha256 = canonical_sha256(manifest)
    partial = Path(f"{output_path}.partial")
    if partial.exists() or partial.is_symlink():
        raise ValueError("partial output already exists")
    provenance_partial = None if provenance_path is None else Path(f"{provenance_path}.partial")
    if provenance_partial is not None and (provenance_partial.exists() or provenance_partial.is_symlink()):
        raise ValueError("partial provenance output already exists")

    try:
        _write_sqlite(
            partial,
            source_kind=source_kind,
            data_cutoff=cutoff,
            provenance_sha256=provenance_sha256,
            institutional=institutional,
            industry=industry,
        )
        os.replace(partial, output_path)
        os.chmod(output_path, 0o444)
        artifact_sha256 = _sha256_file(output_path)
        with SQLiteSupplementalPITProvider(output_path, artifact_sha256):
            pass
        if provenance_path is not None:
            provenance_partial.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(provenance_partial, provenance_path)
            os.chmod(provenance_path, 0o444)
        return BuildResult(
            output=output_path,
            provenance_output=provenance_path,
            sha256=artifact_sha256,
            provenance_sha256=provenance_sha256,
            data_cutoff=cutoff,
            institutional_rows=len(institutional),
            industry_rows=len(industry),
        )
    except Exception:
        partial.unlink(missing_ok=True)
        if provenance_partial is not None:
            provenance_partial.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        if provenance_path is not None:
            provenance_path.unlink(missing_ok=True)
        raise


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--institutional-csv", type=Path, required=True)
    parser.add_argument("--industry-csv", type=Path, required=True)
    parser.add_argument("--source-kind", required=True)
    parser.add_argument("--data-cutoff", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path)
    parser.add_argument(
        "--source-reference",
        action="append",
        default=[],
        help="Stable archive or manifest reference; repeat for multiple source records.",
    )
    parser.add_argument("--max-rows", type=int, default=_MAX_ROWS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the offline builder and print a bounded canonical result."""

    args = _parser().parse_args(argv)
    result = build_artifact(
        institutional_csv=args.institutional_csv,
        industry_csv=args.industry_csv,
        source_kind=args.source_kind,
        data_cutoff=args.data_cutoff,
        output=args.output,
        provenance_output=args.provenance_output,
        source_references=tuple(args.source_reference),
        max_rows=args.max_rows,
    )
    print(
        json.dumps(
            {
                "data_cutoff": result.data_cutoff,
                "industry_rows": result.industry_rows,
                "institutional_rows": result.institutional_rows,
                "output": str(result.output),
                "provenance_output": None if result.provenance_output is None else str(result.provenance_output),
                "provenance_sha256": result.provenance_sha256,
                "sha256": result.sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
