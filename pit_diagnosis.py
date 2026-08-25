"""Explicit, offline-only command line for PIT CANSLIM diagnosis evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from core.pit_data import PITDataBundle, sha256_file
from core.pit_diagnosis.baseline import canonical_authority, compare_reproduction, verify_baseline_run
from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.experiments import DiagnosisContext, run_catalog, run_experiment
from core.pit_diagnosis.fact_cache import build_fact_cache, open_fact_cache
from core.pit_diagnosis.models import PartitionName
from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run
from core.pit_diagnosis.rulebook import load_rulebook


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _source_commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    value = result.stdout.strip()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("Git HEAD is not a lowercase SHA-1")
    return value


def _source_fingerprint() -> str:
    files = sorted(Path("core/pit_diagnosis").glob("*.py")) + [Path("pit_diagnosis.py")]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _context(args: argparse.Namespace) -> tuple[DiagnosisContext, PITDataBundle, object]:
    rulebook = load_rulebook(args.rulebook)
    catalog = load_experiment_catalog(args.experiment_catalog, rulebook)
    bundle = PITDataBundle(args.pit_bundle, expected_sha256=args.pit_bundle_sha256)
    try:
        cache = open_fact_cache(args.fact_cache, args.fact_cache_sha256)
    except Exception:
        bundle.close()
        raise
    metadata = dict(cache._connection.execute("SELECT key,value FROM metadata").fetchall())
    cache.content_sha256 = args.fact_cache_sha256
    cache.schema_sha256 = str(metadata["schema_sha256"])
    try:
        snapshot = verify_baseline_run(args.baseline_run, canonical_authority())
    except Exception:
        cache.close()
        bundle.close()
        raise
    context = DiagnosisContext(
        rulebook=rulebook, catalog=catalog, fact_cache=cache, partitions=fixed_partitions(),
        diagnostic_leader_labels=(), source_commit=_source_commit(),
        source_fingerprint_sha256=_source_fingerprint(), strategy_identity="cached-diagnosis-v1",
        baseline_snapshot=snapshot, reproduced_baseline=snapshot,
    )
    return context, bundle, cache


def _close(bundle: PITDataBundle | None, cache: object | None) -> None:
    if cache is not None:
        cache.close()
    if bundle is not None:
        bundle.close()


def _add_run_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pit-bundle", required=True, type=_absolute_path)
    parser.add_argument("--pit-bundle-sha256", required=True, type=_digest)
    parser.add_argument("--baseline-run", required=True, type=_absolute_path)
    parser.add_argument("--rulebook", required=True, type=_absolute_path)
    parser.add_argument("--experiment-catalog", required=True, type=_absolute_path)
    parser.add_argument("--fact-cache", required=True, type=_absolute_path)
    parser.add_argument("--fact-cache-sha256", required=True, type=_digest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic offline PIT CANSLIM diagnosis")
    commands = parser.add_subparsers(dest="command", required=True)
    facts = commands.add_parser("build-facts", help="build or resume immutable scalar PIT facts")
    facts.add_argument("--pit-bundle", required=True, type=_absolute_path)
    facts.add_argument("--pit-bundle-sha256", required=True, type=_digest)
    facts.add_argument("--rulebook", required=True, type=_absolute_path)
    facts.add_argument("--output", required=True, type=_absolute_path)
    facts.add_argument("--checkpoint", required=True, type=_absolute_path)
    facts.add_argument("--progress", required=True, type=_absolute_path)
    facts.add_argument("--resume", action="store_true")

    run = commands.add_parser("run", help="run D0-D4 discovery and validation diagnosis")
    _add_run_inputs(run)
    run.add_argument("--output-root", required=True, type=_absolute_path)
    run.add_argument("--checkpoint-root", required=True, type=_absolute_path)
    run.add_argument("--partition", choices=("discovery", "validation", "locked_evaluation"), action="append")
    run.add_argument("--human-selection-id")
    run.add_argument("--research-generation-id")
    run.add_argument("--resume", action="store_true")

    one = commands.add_parser("run-experiment", help="run exactly one approved deterministic experiment")
    _add_run_inputs(one)
    one.add_argument("--experiment-id", required=True)
    one.add_argument("--partition", required=True, choices=("discovery", "validation"))
    one.add_argument("--checkpoint-root", required=True, type=_absolute_path)
    one.add_argument("--resume", action="store_true")

    verify = commands.add_parser("verify-result", help="verify a completed diagnosis publication")
    verify.add_argument("--run-dir", required=True, type=_absolute_path)
    return parser


def _build_facts(args: argparse.Namespace) -> int:
    rulebook = load_rulebook(args.rulebook)
    with PITDataBundle(args.pit_bundle, expected_sha256=args.pit_bundle_sha256) as bundle:
        result = build_fact_cache(
            bundle=bundle, rulebook=rulebook, partitions=fixed_partitions(), output_path=args.output,
            checkpoint_path=args.checkpoint, progress_path=args.progress, resume=args.resume,
        )
    print(json.dumps({"path": str(result.path), "content_sha256": result.content_sha256, "schema_sha256": result.schema_sha256, "resumed": result.resumed}, sort_keys=True))
    return 0


def _run(args: argparse.Namespace) -> int:
    selected = tuple(args.partition or ("discovery", "validation"))
    if "locked_evaluation" in selected:
        if not args.human_selection_id or not args.research_generation_id:
            raise ValueError("locked_evaluation requires --human-selection-id and --research-generation-id")
        raise ValueError("locked_evaluation requires the separately approved locked worker API")
    context: DiagnosisContext | None = None
    bundle: PITDataBundle | None = None
    cache: object | None = None
    try:
        context, bundle, cache = _context(args)
        d0 = run_experiment(context, "D0.BASELINE_REPRODUCTION", PartitionName.DISCOVERY)
        reproduction = compare_reproduction(context.baseline_snapshot, context.reproduced_baseline)
        context = context.with_verified_baseline_reproduction(reproduction)
        experiment_ids = tuple(item.experiment_id for item in context.catalog.experiments.values() if item.phase in {"D1", "D2", "D3", "D4"})
        results = (d0, *run_catalog(context, experiment_ids, tuple(PartitionName(item) for item in selected), args.checkpoint_root, resume=args.resume))
        source_before = context.source_fingerprint_sha256
        if _source_fingerprint() != source_before or sha256_file(args.pit_bundle) != args.pit_bundle_sha256 or sha256_file(args.fact_cache) != args.fact_cache_sha256:
            raise ValueError("source, bundle, or fact-cache identity changed during diagnosis")
        run_dir = publish_diagnosis(context, results, args.output_root)
        print(json.dumps(verify_diagnosis_run(run_dir), sort_keys=True))
        return 0
    finally:
        _close(bundle, cache)


def _run_experiment(args: argparse.Namespace) -> int:
    context: DiagnosisContext | None = None
    bundle: PITDataBundle | None = None
    cache: object | None = None
    try:
        context, bundle, cache = _context(args)
        if args.experiment_id not in context.catalog.experiments:
            raise ValueError("experiment ID is not present in the approved catalog")
        if args.experiment_id != "D0.BASELINE_REPRODUCTION":
            context = context.with_verified_baseline_reproduction(compare_reproduction(context.baseline_snapshot, context.reproduced_baseline))
        results = run_catalog(context, (args.experiment_id,), (PartitionName(args.partition),), args.checkpoint_root, resume=args.resume)
        print(json.dumps({"experiment_id": results[0].experiment_id, "partition": results[0].partition.value, "identity_sha256": results[0].identity_sha256, "result_sha256": results[0].result_sha256}, sort_keys=True))
        return 0
    finally:
        _close(bundle, cache)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-facts":
            return _build_facts(args)
        if args.command == "run":
            return _run(args)
        if args.command == "run-experiment":
            return _run_experiment(args)
        if args.command == "verify-result":
            print(json.dumps(verify_diagnosis_run(args.run_dir), sort_keys=True))
            return 0
    except Exception as exc:
        print(f"PIT diagnosis failed closed: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
