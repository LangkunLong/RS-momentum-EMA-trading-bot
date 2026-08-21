"""Verify a point-in-time bundle and its optional persisted manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.pit_data import PITDataBundle


def _compare_manifest(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    scalar_keys = ("bundle_sha256", "schema_version", "data_cutoff", "membership_events")
    for key in scalar_keys:
        if expected.get(key) != actual.get(key):
            raise ValueError(f"manifest mismatch for {key}")
    if expected.get("symbols") != actual.get("symbol_count"):
        raise ValueError("manifest mismatch for symbols")
    expected_sources = {
        key: value
        for key, value in expected.items()
        if key.endswith("_source_sha256")
    }
    actual_sources = actual.get("metadata", {})
    for key, value in expected_sources.items():
        if actual_sources.get(key) != value:
            raise ValueError(f"manifest mismatch for {key}")
    expected_coverage = expected.get("coverage")
    if expected_coverage is not None and expected_coverage != actual.get("coverage"):
        raise ValueError("manifest mismatch for coverage")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a strict point-in-time SQLite bundle")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--manifest")
    args = parser.parse_args()

    with PITDataBundle(args.bundle, expected_sha256=args.sha256) as bundle:
        actual = bundle.manifest()
    if args.manifest:
        manifest_path = Path(args.manifest)
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError("manifest must be a regular non-link file")
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            raise ValueError("manifest must contain a JSON object")
        _compare_manifest(actual, expected)
    print(json.dumps(actual, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
