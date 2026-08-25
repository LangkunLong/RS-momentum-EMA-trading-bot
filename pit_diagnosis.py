"""Explicit, offline-only command line for PIT CANSLIM diagnosis evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Mapping, Sequence

from core.pit_data import PITDataBundle, sha256_file
from core.pit_diagnosis.baseline import canonical_authority, compare_reproduction, verify_baseline_run
from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.experiments import DiagnosisContext, run_catalog, run_experiment, run_locked_catalog
from core.pit_diagnosis.fact_cache import build_fact_cache, open_fact_cache
from core.pit_diagnosis.models import PartitionName
from core.pit_diagnosis.publication import publish_diagnosis, verify_diagnosis_run
from core.pit_diagnosis.rulebook import load_rulebook
from core.pit_diagnosis.supplemental import SQLiteSupplementalPITProvider


PIT_DIAGNOSIS_SENTINEL = "PIT_DIAGNOSIS_RESULT="
PIT_EVIDENCE_SENTINEL = "PIT_DIAGNOSIS_EVIDENCE="
_PIT_DIAGNOSIS_RESULT_KEYS = frozenset(
    {"experiment_id", "partition", "identity_sha256", "result_sha256"}
)
_PIT_EXPERIMENT_ID_RE = re.compile(r"[A-Z][A-Z0-9_.-]{0,127}")
_PIT_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")
_PIT_EVIDENCE_KEYS = frozenset(
    {
        "diagnosis_run_sha256",
        "pit_bundle_sha256",
        "fact_cache_sha256",
        "rulebook_sha256",
        "experiment_catalog_sha256",
        "experiment_result_sha256s",
        "experiment_partition_result_sha256s",
        "metrics",
        "evidence_ids",
        "rule_ids",
        "invariant_ids",
        "experiment_ids",
        "fidelity_label",
        "promotion_eligible",
        "partition",
    }
)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _digest(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def _source_commit_arg(value: str) -> str:
    if _PIT_SOURCE_COMMIT_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("source commit must be a lowercase exact Git commit")
    return value


def parse_pit_diagnosis_result(output: str) -> dict[str, str]:
    """Parse the one-line hidden-worker result without accepting free-form output."""
    if not isinstance(output, str):
        raise ValueError("PIT worker output must be text")
    matches = [line for line in output.splitlines() if line.startswith(PIT_DIAGNOSIS_SENTINEL)]
    if len(matches) != 1:
        raise ValueError("PIT worker must emit exactly one result sentinel")
    encoded = matches[0][len(PIT_DIAGNOSIS_SENTINEL) :]
    try:
        value = json.loads(encoded)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("PIT worker result sentinel is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _PIT_DIAGNOSIS_RESULT_KEYS:
        raise ValueError("PIT worker result fields are not closed")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if encoded != canonical:
        raise ValueError("PIT worker result is not canonical JSON")
    if (
        not isinstance(value["experiment_id"], str)
        or _PIT_EXPERIMENT_ID_RE.fullmatch(value["experiment_id"]) is None
        or not isinstance(value["partition"], str)
        or value["partition"] not in {"discovery", "validation"}
        or not isinstance(value["identity_sha256"], str)
        or not isinstance(value["result_sha256"], str)
        or not all(len(value[name]) == 64 and set(value[name]) <= set("0123456789abcdef") for name in ("identity_sha256", "result_sha256"))
    ):
        raise ValueError("PIT worker result identity is invalid")
    return value


def parse_pit_diagnosis_evidence(output: str) -> dict[str, object]:
    """Parse one aggregate-only evidence envelope from the hidden worker."""
    if not isinstance(output, str):
        raise ValueError("PIT evidence worker output must be text")
    matches = [line for line in output.splitlines() if line.startswith(PIT_EVIDENCE_SENTINEL)]
    if len(matches) != 1:
        raise ValueError("PIT evidence worker must emit exactly one result sentinel")
    encoded = matches[0][len(PIT_EVIDENCE_SENTINEL) :]
    try:
        value = json.loads(encoded)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("PIT evidence sentinel is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != _PIT_EVIDENCE_KEYS:
        raise ValueError("PIT evidence fields are not closed")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if encoded != canonical:
        raise ValueError("PIT evidence is not canonical JSON")
    for field in (
        "diagnosis_run_sha256",
        "pit_bundle_sha256",
        "fact_cache_sha256",
        "rulebook_sha256",
        "experiment_catalog_sha256",
    ):
        if not isinstance(value[field], str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise ValueError("PIT evidence identity is invalid")
    if value["partition"] not in {"discovery", "validation"}:
        raise ValueError("PIT evidence partition is invalid")
    if not isinstance(value["experiment_result_sha256s"], dict) or not isinstance(
        value["experiment_partition_result_sha256s"], dict
    ):
        raise ValueError("PIT evidence result hashes are invalid")
    for experiment_id, digest in value["experiment_result_sha256s"].items():
        if _PIT_EXPERIMENT_ID_RE.fullmatch(str(experiment_id)) is None or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("PIT evidence experiment result is invalid")
    for key, digest in value["experiment_partition_result_sha256s"].items():
        if (
            not isinstance(key, str)
            or key.count("@") != 1
            or _PIT_EXPERIMENT_ID_RE.fullmatch(key.rsplit("@", 1)[0]) is None
            or key.rsplit("@", 1)[1] not in {"discovery", "validation"}
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("PIT evidence partition result is invalid")
    if not isinstance(value["metrics"], dict) or any(
        not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
        or type(metric) not in {int, float}
        or (isinstance(metric, float) and not math.isfinite(metric))
        for key, metric in value["metrics"].items()
    ):
        raise ValueError("PIT evidence metrics are invalid")
    for field in ("evidence_ids", "rule_ids", "invariant_ids", "experiment_ids"):
        if not isinstance(value[field], list) or value[field] != sorted(value[field]) or len(set(value[field])) != len(value[field]) or any(not isinstance(item, str) or _PIT_EXPERIMENT_ID_RE.fullmatch(item) is None for item in value[field]):
            raise ValueError("PIT evidence identifier list is invalid")
    if value["experiment_ids"] != sorted(value["experiment_result_sha256s"]):
        raise ValueError("PIT evidence experiment projection is inconsistent")
    if value["promotion_eligible"] is not False or value["fidelity_label"] not in {"strict_canslim", "quantitative_canslim_proxy", "fidelity_incomplete"}:
        raise ValueError("PIT evidence promotion fields are invalid")
    return value


def _source_git(root: Path, *arguments: str) -> bytes:
    """Run the same read-only Git shape used by the controller fingerprint."""
    if not root.is_absolute():
        raise ValueError("source root must be absolute")
    null_device = "NUL" if os.name == "nt" else "/dev/null"
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": null_device,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    fixed = (
        "--no-lazy-fetch",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={null_device}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        "core.pager=cat",
        "-c",
        "pager.status=false",
    )
    try:
        result = subprocess.run(
            ["git", *fixed, *arguments],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("source checkout Git metadata is unavailable") from exc
    return result.stdout


def _source_commit(root: Path | None = None) -> str:
    root = Path.cwd() if root is None else Path(root)
    value = _source_git(root, "rev-parse", "--verify", "HEAD").decode("utf-8").strip()
    if _PIT_SOURCE_COMMIT_RE.fullmatch(value) is None:
        raise ValueError("Git HEAD is not a lowercase exact commit")
    return value


def _source_fingerprint(root: Path | None = None) -> str:
    """Match ``agent_loop.source_fingerprint`` across the whole checkout.

    The hidden worker never calls this function: ``run-experiment`` receives the
    controller-sealed values.  A normal ``run`` still verifies the complete Git
    checkout, including index, tracked modes/bytes, and untracked names.
    """
    root = Path.cwd() if root is None else Path(root)
    head = _source_commit(root)
    branch = _source_git(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode("utf-8").strip()
    index = _source_git(root, "ls-files", "-s", "-z")
    tracked_hash = hashlib.sha256()
    tracked_raw = _source_git(root, "ls-files", "-z")
    for raw in tracked_raw.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        if not relative or "\\" in relative or "\x00" in relative:
            raise ValueError("tracked source path is not canonical")
        path = root / relative
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError("tracked source path is unavailable") from exc
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
        ):
            raise ValueError("tracked source path is not a regular file")
        tracked_hash.update(relative.encode("utf-8") + b"\0")
        tracked_hash.update(f"{stat.S_IMODE(info.st_mode):o}".encode("ascii") + b"\0")
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    tracked_hash.update(chunk)
        except OSError as exc:
            raise ValueError("tracked source path cannot be read") from exc
        tracked_hash.update(b"\0")
    untracked_raw = _source_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = tuple(sorted(value.decode("utf-8") for value in untracked_raw.split(b"\0") if value))
    payload = {
        "head": head,
        "branch": branch,
        "index_sha256": hashlib.sha256(index).hexdigest(),
        "tracked_manifest_sha256": tracked_hash.hexdigest(),
        "untracked_names": list(untracked),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
    try:
        cache_identity = json.loads(metadata["identity"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        cache.close()
        bundle.close()
        raise ValueError("fact cache identity is malformed") from exc
    if not isinstance(cache_identity, dict) or cache_identity.get("bundle_sha256") != args.pit_bundle_sha256:
        cache.close()
        bundle.close()
        raise ValueError("fact cache bundle identity does not match the supplied PIT bundle")
    if cache_identity.get("rulebook_sha256") != rulebook.sha256:
        cache.close()
        bundle.close()
        raise ValueError("fact cache rulebook identity does not match the supplied rulebook")
    cache.content_sha256 = args.fact_cache_sha256
    cache.schema_sha256 = str(metadata["schema_sha256"])
    try:
        snapshot = verify_baseline_run(args.baseline_run, canonical_authority())
    except Exception:
        cache.close()
        bundle.close()
        raise
    sealed_source_commit = getattr(args, "source_commit", None)
    if sealed_source_commit is not None and _PIT_SOURCE_COMMIT_RE.fullmatch(sealed_source_commit) is None:
        raise ValueError("sealed source commit is invalid")
    sealed_source_fingerprint = getattr(args, "source_fingerprint_sha256", None)
    if sealed_source_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", sealed_source_fingerprint):
        raise ValueError("sealed source fingerprint is invalid")
    context = DiagnosisContext(
        rulebook=rulebook, catalog=catalog, fact_cache=cache, partitions=fixed_partitions(),
        diagnostic_leader_labels=(),
        source_commit=(
            sealed_source_commit
            if sealed_source_commit is not None
            else _source_commit()
        ),
        source_fingerprint_sha256=(
            sealed_source_fingerprint
            if sealed_source_fingerprint is not None
            else _source_fingerprint()
        ),
        strategy_identity="cached-diagnosis-v2-fidelity-cash",
        bundle_sha256=args.pit_bundle_sha256,
        strict_canslim=bool(getattr(args, "strict_canslim", False)),
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
    parser.add_argument(
        "--strict-canslim",
        action="store_true",
        help="require PIT I/L evidence for entry eligibility; unavailable evidence fails closed",
    )


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
    facts.add_argument("--supplemental-input", type=_absolute_path)
    facts.add_argument("--supplemental-sha256", type=_digest)
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
    one.add_argument("--source-commit", required=True, type=_source_commit_arg)
    one.add_argument("--source-fingerprint-sha256", required=True, type=_digest)
    one.add_argument("--experiment-id", required=True)
    one.add_argument("--partition", required=True, choices=("discovery", "validation"))
    one.add_argument("--checkpoint-root", required=True, type=_absolute_path)
    one.add_argument("--resume", action="store_true")

    evidence = commands.add_parser("emit-evidence", help="emit one aggregate-only sealed evidence envelope")
    evidence.add_argument("--diagnosis-run", required=True, type=_absolute_path)
    evidence.add_argument("--diagnosis-manifest-sha256", required=True, type=_digest)
    evidence.add_argument("--pit-bundle-sha256", required=True, type=_digest)
    evidence.add_argument("--fact-cache-sha256", required=True, type=_digest)
    evidence.add_argument("--rulebook", required=True, type=_absolute_path)
    evidence.add_argument("--rulebook-sha256", required=True, type=_digest)
    evidence.add_argument("--experiment-catalog", required=True, type=_absolute_path)
    evidence.add_argument("--experiment-catalog-sha256", required=True, type=_digest)
    evidence.add_argument("--partition", required=True, choices=("discovery", "validation"))

    verify = commands.add_parser("verify-result", help="verify a completed diagnosis publication")
    verify.add_argument("--run-dir", required=True, type=_absolute_path)
    return parser


def _build_facts(args: argparse.Namespace) -> int:
    rulebook = load_rulebook(args.rulebook)
    if (args.supplemental_input is None) != (args.supplemental_sha256 is None):
        raise ValueError("--supplemental-input and --supplemental-sha256 must be supplied together")
    supplemental = (
        SQLiteSupplementalPITProvider(args.supplemental_input, args.supplemental_sha256)
        if args.supplemental_input is not None
        else None
    )
    try:
        with PITDataBundle(args.pit_bundle, expected_sha256=args.pit_bundle_sha256) as bundle:
            result = build_fact_cache(
                bundle=bundle, rulebook=rulebook, partitions=fixed_partitions(), output_path=args.output,
                checkpoint_path=args.checkpoint, progress_path=args.progress, resume=args.resume,
                supplemental_provider=supplemental,
            )
    finally:
        if supplemental is not None:
            supplemental.close()
    print(json.dumps({
        "path": str(result.path), "content_sha256": result.content_sha256, "schema_sha256": result.schema_sha256,
        "resumed": result.resumed,
        "supplemental_content_identity_sha256": "0" * 64 if supplemental is None else supplemental.content_identity_sha256,
    }, sort_keys=True))
    return 0


def _run(args: argparse.Namespace) -> int:
    selected = tuple(args.partition or ("discovery", "validation"))
    if "locked_evaluation" in selected:
        if not args.human_selection_id or not args.research_generation_id:
            raise ValueError("locked_evaluation requires --human-selection-id and --research-generation-id")
        if selected != ("locked_evaluation",):
            raise ValueError("locked_evaluation must be published separately from discovery and validation")
    context: DiagnosisContext | None = None
    bundle: PITDataBundle | None = None
    cache: object | None = None
    try:
        context, bundle, cache = _context(args)
        input_identities = {
            "rulebook": sha256_file(args.rulebook), "catalog": sha256_file(args.experiment_catalog),
            "baseline_manifest": sha256_file(args.baseline_run / "run_manifest.json"),
        }
        reproduction = compare_reproduction(context.baseline_snapshot, context.reproduced_baseline)
        context = context.with_verified_baseline_reproduction(reproduction)
        experiment_ids = tuple(item.experiment_id for item in context.catalog.experiments.values() if item.phase in {"D1", "D2", "D3", "D4"})
        if selected == ("locked_evaluation",):
            locked_identity = hashlib.sha256(
                f"{args.human_selection_id}\0{args.research_generation_id}".encode("utf-8")
            ).hexdigest()
            results = run_locked_catalog(
                context, experiment_ids, args.checkpoint_root / "locked" / locked_identity,
                human_selection_id=args.human_selection_id,
                research_generation_id=args.research_generation_id,
                resume=args.resume,
            )
            output_root = args.output_root / "locked_evaluation" / locked_identity
        else:
            d0 = tuple(run_experiment(context, "D0.BASELINE_REPRODUCTION", PartitionName(item)) for item in selected)
            results = (*d0, *run_catalog(context, experiment_ids, tuple(PartitionName(item) for item in selected), args.checkpoint_root, resume=args.resume))
            output_root = args.output_root
        source_before = context.source_fingerprint_sha256
        if (
            _source_fingerprint() != source_before
            or sha256_file(args.pit_bundle) != args.pit_bundle_sha256
            or sha256_file(args.fact_cache) != args.fact_cache_sha256
            or sha256_file(args.rulebook) != input_identities["rulebook"]
            or sha256_file(args.experiment_catalog) != input_identities["catalog"]
            or sha256_file(args.baseline_run / "run_manifest.json") != input_identities["baseline_manifest"]
            or verify_baseline_run(args.baseline_run, canonical_authority()).manifest_sha256 != context.baseline_snapshot.manifest_sha256
        ):
            raise ValueError("source, bundle, rulebook, catalog, fact-cache, or baseline identity changed during diagnosis")
        run_dir = publish_diagnosis(context, results, output_root)
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
        payload = {
            "experiment_id": results[0].experiment_id,
            "partition": results[0].partition.value,
            "identity_sha256": results[0].identity_sha256,
            "result_sha256": results[0].result_sha256,
        }
        print(
            PIT_DIAGNOSIS_SENTINEL
            + json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        return 0
    finally:
        _close(bundle, cache)


def _emit_evidence(args: argparse.Namespace) -> int:
    """Read and verify publication inputs only inside the hidden worker.

    The controller receives this compact aggregate envelope, never publication CSV
    rows or reports.  This command intentionally does not calculate alternate metrics.
    """
    manifest_path = args.diagnosis_run / "manifest.json"
    if sha256_file(manifest_path) != args.diagnosis_manifest_sha256:
        raise ValueError("diagnosis manifest identity changed")
    verified = verify_diagnosis_run(args.diagnosis_run)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("diagnosis manifest is malformed")
    for field, expected in (
        ("rulebook_sha256", args.rulebook_sha256),
        ("catalog_sha256", args.experiment_catalog_sha256),
        ("fact_cache_sha256", args.fact_cache_sha256),
        ("bundle_sha256", args.pit_bundle_sha256),
    ):
        if manifest.get(field) != expected:
            raise ValueError(f"diagnosis publication {field} identity differs")
    # Load the closed catalog/rulebook from the explicitly mounted copies and ensure
    # they match the verified publication identities before extracting IDs.
    rulebook = load_rulebook(args.rulebook)
    catalog = load_experiment_catalog(args.experiment_catalog, rulebook)
    if rulebook.sha256 != args.rulebook_sha256 or catalog.sha256 != args.experiment_catalog_sha256:
        raise ValueError("mounted rulebook/catalog identity differs")
    result_hashes: dict[str, str] = {}
    partition_hashes: dict[str, str] = {}
    import csv

    with (args.diagnosis_run / "ablation_results.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            experiment_id = str(row.get("experiment_id", ""))
            partition = str(row.get("partition", ""))
            digest = str(row.get("result_sha256", ""))
            if partition not in {"discovery", "validation"} or experiment_id not in catalog.experiments:
                continue
            if _PIT_EXPERIMENT_ID_RE.fullmatch(experiment_id) is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("diagnosis publication experiment result is malformed")
            key = f"{experiment_id}@{partition}"
            if key in partition_hashes and partition_hashes[key] != digest:
                raise ValueError("diagnosis publication contains conflicting partition results")
            partition_hashes[key] = digest
            if partition == args.partition:
                result_hashes[experiment_id] = digest
    if not result_hashes:
        raise ValueError("diagnosis publication has no selected-partition results")
    metrics: dict[str, int] = {}
    with (args.diagnosis_run / "entry_funnel.csv").open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("partition") == args.partition]
    for field in ("evaluated", "qualified", "attempted", "executed", "rejected"):
        metrics[f"funnel_{field}"] = sum(int(row.get(field, "0")) for row in rows)
    payload = {
        "diagnosis_run_sha256": args.diagnosis_manifest_sha256,
        "pit_bundle_sha256": args.pit_bundle_sha256,
        "fact_cache_sha256": args.fact_cache_sha256,
        "rulebook_sha256": args.rulebook_sha256,
        "experiment_catalog_sha256": args.experiment_catalog_sha256,
        "experiment_result_sha256s": dict(sorted(result_hashes.items())),
        "experiment_partition_result_sha256s": dict(sorted(partition_hashes.items())),
        "metrics": metrics,
        "evidence_ids": sorted(rulebook.rules),
        "rule_ids": sorted(rulebook.rules),
        "invariant_ids": ["INV.D0_REPRODUCTION"],
        "experiment_ids": sorted(result_hashes),
        "fidelity_label": manifest["fidelity_label"],
        "promotion_eligible": False,
        "partition": args.partition,
    }
    # ``verified`` is deliberately consumed only as a proof that the source files
    # were checked; no raw verifier payload crosses the worker boundary.
    if not isinstance(verified, Mapping):
        raise ValueError("diagnosis publication verifier returned malformed facts")
    print(PIT_EVIDENCE_SENTINEL + json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build-facts":
            return _build_facts(args)
        if args.command == "run":
            return _run(args)
        if args.command == "run-experiment":
            return _run_experiment(args)
        if args.command == "emit-evidence":
            return _emit_evidence(args)
        if args.command == "verify-result":
            print(json.dumps(verify_diagnosis_run(args.run_dir), sort_keys=True))
            return 0
    except Exception as exc:
        print(f"PIT diagnosis failed closed: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
