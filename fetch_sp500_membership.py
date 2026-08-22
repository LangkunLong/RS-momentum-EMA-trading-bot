"""Create immutable, point-in-time S&P 500 membership acquisition artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core.public_membership import _normalize, fetch_revision, load_symbol_map

_OUTPUTS = (
    "membership.csv",
    "security_names.csv",
    "membership_provenance.json",
    "membership_raw.html",
    "membership_spot_checks.json",
)
_REQUIRED_START_DATE = date(2021, 1, 1)
_REQUIRED_END_DATE = date(2025, 12, 31)
_SPOT_CHECKS = (
    {"effective_date": "2021-07-21", "addition": "MRNA", "removal": "ALXN", "official_announcement_url": "https://press.spglobal.com/2021-07-15-Moderna-Set-to-Join-S-P-500"},
    {"effective_date": "2022-02-15", "addition": "NDSN", "removal": "XLNX", "official_announcement_url": "https://www.spglobal.com/spdji/en/documents/indexnews/announcements/20220210-1449702/1449702_xlnx5.pdf"},
    {"effective_date": "2023-03-15", "addition": "BG", "removal": "SBNY", "official_announcement_url": "https://press.spglobal.com/2023-03-13-Bunge-Set-to-Join-S-P-500"},
    {"effective_date": "2024-06-24", "addition": "KKR", "removal": "RHI", "official_announcement_url": "https://www.spglobal.com/spdji/en/documents/indexnews/announcements/20240607-1472747/1472747_finaljuneshuffle546.pdf"},
    {"effective_date": "2025-08-28", "addition": "IBKR", "removal": "WBA", "official_announcement_url": "https://press.spglobal.com/2025-08-25-Interactive-Brokers-Group-Set-to-Join-S-P-500%2C-Talen-Energy-to-Join-S-P-MidCap-400-and-Kinetik-Holdings-to-Join-S-P-SmallCap-600"},
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _output_dir(value: str) -> Path:
    path = Path(value).resolve()
    if path.exists() and (not path.is_dir() or path.is_symlink()):
        raise ValueError("output directory must be a regular directory")
    if path.exists():
        entries = {item.name for item in path.iterdir()}
        if entries != {".gitkeep"}:
            raise ValueError("existing output directory must contain exactly .gitkeep")
    return path


def _assert_targets_absent(output_dir: Path) -> None:
    for name in _OUTPUTS:
        for candidate in (output_dir / name, output_dir / f"{name}.tmp"):
            if candidate.exists() or candidate.is_symlink():
                raise ValueError(f"refusing to overwrite existing output: {candidate}")


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    temporary.replace(path)


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _symbol_map_provenance(path: Path | None, mappings: Any) -> dict[str, Any]:
    rows = [
        {
            "source_ticker": source,
            "canonical_ticker": item.canonical_ticker,
            "effective_start": item.effective_start.isoformat(),
            "effective_end": item.effective_end.isoformat(),
            "reason": item.reason,
        }
        for source in sorted(mappings)
        for item in mappings[source]
    ]
    return {
        "symbol_map_sha256": _sha256_file(path) if path is not None else None,
        "reviewed_symbol_mappings": rows,
    }


def _require_baseline_window(start_date: date, end_date: date) -> None:
    if start_date != _REQUIRED_START_DATE or end_date != _REQUIRED_END_DATE:
        raise ValueError("membership window must be exactly 2021-01-01 through 2025-12-31")


def _csv_text(rows: list[tuple[Any, ...]], header: tuple[str, ...]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def _spot_checks(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    event_set = {(event.effective_date.isoformat(), event.ticker, event.member) for event in events}
    checked: list[dict[str, Any]] = []
    for check in _SPOT_CHECKS:
        matched = (check["effective_date"], check["addition"], True) in event_set and (check["effective_date"], check["removal"], False) in event_set
        if not matched:
            raise ValueError(f"official spot check does not match membership transitions: {check['effective_date']}")
        checked.append({**check, "matched": True})
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch an immutable five-year S&P 500 membership export")
    parser.add_argument("--revision-url", required=True)
    parser.add_argument("--start-date", type=_date, default=_REQUIRED_START_DATE)
    parser.add_argument("--end-date", type=_date, default=_REQUIRED_END_DATE)
    parser.add_argument("--symbol-map-csv", default=None)
    parser.add_argument("--output-dir", default="exports/pit")
    args = parser.parse_args()

    try:
        _require_baseline_window(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    output_dir = _output_dir(args.output_dir)
    raw = fetch_revision(args.revision_url)
    symbol_map_path = Path(args.symbol_map_csv).resolve() if args.symbol_map_csv else None
    mappings = load_symbol_map(symbol_map_path)
    export = _normalize(raw, args.revision_url, args.start_date, args.end_date, mappings=mappings)
    checks = _spot_checks(export.events)
    output_dir.mkdir(parents=True, exist_ok=True)
    _assert_targets_absent(output_dir)
    provenance = {
        "source_url": export.source_url,
        "revision_id": export.revision_id,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "raw_sha256": export.source_sha256,
        "first_effective_date": export.events[0].effective_date.isoformat(),
        "last_effective_date": export.events[-1].effective_date.isoformat(),
        "event_count": len(export.events),
        "symbol_count": len({event.ticker for event in export.events}),
        "exclusions": list(export.exclusions),
        **_symbol_map_provenance(symbol_map_path, mappings),
    }
    _atomic_text(
        output_dir / "membership.csv",
        _csv_text([(event.effective_date.isoformat(), event.ticker, int(event.member)) for event in export.events], ("effective_date", "ticker", "member")),
    )
    _atomic_text(output_dir / "security_names.csv", _csv_text(sorted(export.company_names.items()), ("ticker", "company_name")))
    _atomic_bytes(output_dir / "membership_raw.html", raw)
    _atomic_text(output_dir / "membership_spot_checks.json", json.dumps(checks, indent=2, sort_keys=True) + "\n")
    provenance.update(
        {
            "membership_sha256": _sha256_file(output_dir / "membership.csv"),
            "security_names_sha256": _sha256_file(output_dir / "security_names.csv"),
            "membership_spot_checks_sha256": _sha256_file(output_dir / "membership_spot_checks.json"),
        }
    )
    _atomic_text(output_dir / "membership_provenance.json", json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    print(json.dumps(provenance, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
