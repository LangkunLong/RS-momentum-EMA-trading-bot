"""Verify a point-in-time bundle read-only against exact sources and manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.pit_data import PITDataBundle, sha256_file


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


def _verify_exact_sources(bundle: PITDataBundle, args: argparse.Namespace) -> None:
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
        _verify_exact_sources(bundle, args)
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
