"""Verify a point-in-time bundle read-only against exact sources and manifest."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from core.pit_data import PITDataBundle, sha256_file
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_sha256,
)


def _regular_file(path: str | Path, *, label: str) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"{label} must be a regular non-link file")
    return value.resolve()


def _compare_manifest(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    normalized = dict(actual)
    normalized["symbols"] = normalized.pop("symbol_count")
    if expected != normalized:
        differing = sorted(key for key in set(expected).union(normalized) if expected.get(key) != normalized.get(key))
        raise ValueError(f"manifest does not exactly match the bundle: {differing}")


def _verify_exact_sources(
    bundle: PITDataBundle,
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> None:
    fields = {
        "membership_csv": "membership_source_sha256",
        "prices_csv": "prices_source_sha256",
        "fundamentals_csv": "fundamentals_source_sha256",
        "membership_provenance": "membership_provenance_sha256",
        "prices_provenance": "prices_provenance_sha256",
        "fundamentals_provenance": "fundamentals_provenance_sha256",
    }
    for name, metadata_key in fields.items():
        raw_path = getattr(args, name)
        path = _regular_file(raw_path, label=name.replace("_", " "))
        if sha256_file(path) != bundle.metadata[metadata_key]:
            raise ValueError(f"exact source digest mismatch: {name}")
    bundle.load_price_identity_transition_contract(args.prices_provenance)
    if bundle.metadata["schema_version"] == "1":
        return
    prices_path = _regular_file(args.prices_csv, label="prices csv")
    reference_dates: dict[str, set[str]] = {
        reference: set() for reference in PIT_NON_TRADABLE_REFERENCE_SYMBOLS
    }
    with prices_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            ticker = row.get("ticker")
            if ticker in reference_dates:
                reference_dates[ticker].add(str(row.get("trade_date", "")))
    source_coverage = {
        reference: {
            "first_date": days[0],
            "last_date": days[-1],
            "session_count": len(days),
        }
        for reference, observed in reference_dates.items()
        for days in (sorted(observed),)
        if days
    }
    expected_references = list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    if (
        manifest.get("non_tradable_reference_symbols") != expected_references
        or manifest.get("coverage", {}).get("references") != source_coverage
    ):
        raise ValueError("bundle manifest reference coverage differs from prices source")
    provenance_path = _regular_file(
        args.prices_provenance, label="prices provenance"
    )
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prices provenance JSON is invalid") from exc
    if (
        not isinstance(provenance, dict)
        or provenance.get("reference_symbol_coverage") != source_coverage
        or provenance.get("non_tradable_reference_symbols_json")
        != pit_canonical_json(expected_references)
        or provenance.get("non_tradable_reference_symbols_sha256")
        != pit_canonical_json_sha256(expected_references)
    ):
        raise ValueError("prices provenance reference coverage differs from prices source")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a strict point-in-time SQLite bundle")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--manifest", required=True, help="required manifest-last commit marker")
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--prices-csv", required=True)
    parser.add_argument("--fundamentals-csv", required=True)
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--fundamentals-provenance", required=True)
    args = parser.parse_args()

    with PITDataBundle(args.bundle, expected_sha256=args.sha256) as bundle:
        actual = bundle.manifest()
        _verify_exact_sources(bundle, args, actual)
    manifest_path = _regular_file(args.manifest, label="manifest")
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest JSON is invalid") from exc
    if not isinstance(expected, dict):
        raise ValueError("manifest must contain a JSON object")
    _compare_manifest(actual, expected)
    print(json.dumps(actual, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
