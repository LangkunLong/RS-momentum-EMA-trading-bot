"""Convert sparse point-in-time membership snapshots into event rows.

Input CSVs must contain at least ``as_of`` and ``ticker`` columns.  Each
``as_of`` date represents the complete membership state at that date.  The
converter emits the strict ``effective_date,ticker,member`` format consumed by
``build_pit_bundle.py`` and never infers membership between snapshots except
by carrying the last observed state forward in the resolver.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

from core.pit_data import sha256_file

_OUTPUT_COLUMNS = ("effective_date", "ticker", "member")
_TICKER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ.-")


def _input(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"snapshot input must be a regular non-link file: {value}")
    return value.resolve()


def _ticker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be text")
    normalized = value.strip().upper()
    if not normalized or len(normalized) > 8 or any(char not in _TICKER_CHARS for char in normalized):
        raise ValueError(f"invalid ticker: {value!r}")
    return normalized


def _date(value: object) -> str:
    text = str(value).strip()
    if len(text) < 10:
        raise ValueError("as_of must be an ISO date")
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date") from exc


def convert(input_path: Path, output_path: Path) -> dict[str, object]:
    if output_path.exists() or output_path.is_symlink():
        raise ValueError(f"refusing to overwrite existing output: {output_path}")
    snapshots: dict[str, set[str]] = {}
    with input_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = set(reader.fieldnames or ())
        if not {"as_of", "ticker"}.issubset(headers):
            raise ValueError("snapshot CSV must contain as_of and ticker columns")
        for row_number, row in enumerate(reader, start=2):
            as_of = _date(row.get("as_of"))
            ticker = _ticker(row.get("ticker"))
            members = snapshots.setdefault(as_of, set())
            if ticker in members:
                raise ValueError(f"duplicate ticker in snapshot at row {row_number}")
            members.add(ticker)
    if not snapshots or any(not members for members in snapshots.values()):
        raise ValueError("snapshot CSV must contain non-empty snapshots")

    events: list[tuple[str, str, int]] = []
    previous: set[str] = set()
    for as_of in sorted(snapshots):
        current = snapshots[as_of]
        if not previous:
            events.extend((as_of, ticker, 1) for ticker in sorted(current))
        else:
            events.extend((as_of, ticker, 1) for ticker in sorted(current - previous))
            events.extend((as_of, ticker, 0) for ticker in sorted(previous - current))
        previous = current
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"temporary output already exists: {temporary}")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(_OUTPUT_COLUMNS)
            writer.writerows(events)
        temporary.replace(output_path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return {
        "source_sha256": sha256_file(input_path),
        "event_sha256": sha256_file(output_path),
        "snapshot_count": len(snapshots),
        "event_count": len(events),
        "symbol_count": len(set().union(*snapshots.values())),
        "first_snapshot": min(snapshots),
        "last_snapshot": max(snapshots),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert PIT membership snapshots to event CSV")
    parser.add_argument("--snapshots-csv", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = convert(_input(args.snapshots_csv), Path(args.output).resolve())
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
