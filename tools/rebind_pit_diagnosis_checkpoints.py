"""Re-seal completed PIT diagnosis checkpoints after a data-bundle refresh.

This utility only changes identity envelopes and their hashes.  It verifies that
each source checkpoint was produced with the supplied old authority identity and
that its payload hash is intact before writing a new immutable checkpoint root.
No experiment metrics or trade-path values are recalculated or altered.
"""

from __future__ import annotations

import argparse
import json
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pit_diagnosis.baseline import (  # noqa: E402
    canonical_authority,
    compare_reproduction,
    verify_baseline_run,
)
from core.pit_diagnosis.experiments import (  # noqa: E402
    ExperimentCheckpointStore,
    _identity,
    _result_from_primitive,
    _result_payload_sha256,
)
from core.pit_diagnosis.models import PartitionName  # noqa: E402
from pit_diagnosis import _context  # noqa: E402


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256")
    return value


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pit-bundle", type=Path, required=True)
    parser.add_argument("--pit-bundle-sha256", type=_digest, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--rulebook", type=Path, required=True)
    parser.add_argument("--experiment-catalog", type=Path, required=True)
    parser.add_argument("--fact-cache", type=Path, required=True)
    parser.add_argument("--fact-cache-sha256", type=_digest, required=True)
    parser.add_argument("--source-checkpoint-root", type=Path, required=True)
    parser.add_argument("--destination-checkpoint-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _args()
    source_root = args.source_checkpoint_root.resolve()
    destination_root = args.destination_checkpoint_root.resolve()
    if not source_root.is_dir() or source_root.is_symlink():
        raise ValueError("source checkpoint root must be a regular directory")
    if destination_root.exists():
        raise ValueError("destination checkpoint root already exists")
    source_manifest = json.loads(args.source_manifest.read_text(encoding="utf-8"))
    if not isinstance(source_manifest, dict):
        raise ValueError("source manifest is malformed")
    old_source_commit = source_manifest.get("source_commit")
    old_source_fingerprint = source_manifest.get("source_fingerprint_sha256")
    old_bundle = source_manifest.get("bundle_sha256")
    if not isinstance(old_source_commit, str) or not isinstance(old_source_fingerprint, str) or not isinstance(old_bundle, str):
        raise ValueError("source manifest is missing identity fields")
    verify_baseline_run(args.baseline_run.resolve(), canonical_authority())

    context_args = Namespace(
        pit_bundle=args.pit_bundle.resolve(), pit_bundle_sha256=args.pit_bundle_sha256,
        baseline_run=args.baseline_run.resolve(), rulebook=args.rulebook.resolve(),
        experiment_catalog=args.experiment_catalog.resolve(), fact_cache=args.fact_cache.resolve(),
        fact_cache_sha256=args.fact_cache_sha256,
    )
    context, bundle, cache = _context(context_args)
    try:
        verified = context.with_verified_baseline_reproduction(
            compare_reproduction(context.baseline_snapshot, context.reproduced_baseline)
        )
        old_context = replace(
            verified,
            source_commit=old_source_commit,
            source_fingerprint_sha256=old_source_fingerprint,
            bundle_sha256=old_bundle,
        )
        destination_store = ExperimentCheckpointStore(destination_root)
        source_files = sorted(source_root.glob("*.json"))
        if not source_files:
            raise ValueError("source checkpoint root is empty")
        rebound = 0
        for path in source_files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                raise ValueError(f"checkpoint is malformed: {path.name}")
            result = _result_from_primitive(payload["result"])
            experiment = verified.catalog[result.experiment_id]
            partition = PartitionName(result.partition)
            old_identity = _identity(old_context, experiment, partition)
            if result.identity_sha256 != old_identity or payload.get("identity_sha256") != old_identity:
                raise ValueError(f"checkpoint identity does not match source manifest: {path.name}")
            if result.result_sha256 != _result_payload_sha256(result):
                raise ValueError(f"checkpoint result hash is stale: {path.name}")
            new_identity = _identity(verified, experiment, partition)
            rebound_result = replace(result, identity_sha256=new_identity, result_sha256="0" * 64)
            rebound_result = replace(rebound_result, result_sha256=_result_payload_sha256(rebound_result))
            destination_store.write(rebound_result)
            rebound += 1
        print(json.dumps({"rebound": rebound, "source": str(source_root), "destination": str(destination_root), "bundle_sha256": args.pit_bundle_sha256}, sort_keys=True))
        return 0
    finally:
        cache.close()
        bundle.close()


if __name__ == "__main__":
    main()
