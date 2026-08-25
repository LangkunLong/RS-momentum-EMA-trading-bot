"""Atomic, aggregate-only publication for offline PIT diagnosis runs."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .experiments import DiagnosisContext, ExperimentResult, _result_payload_sha256
from .fact_cache import open_fact_cache
from .models import ExperimentCatalog, Rulebook
from .catalog import load_experiment_catalog
from .rulebook import load_rulebook


_ARTIFACTS = frozenset({
    "rulebook.json", "diagnosis_facts.sqlite3", "baseline_reproduction.json",
    "experiment_catalog.json", "rule_attribution.csv", "entry_funnel.csv",
    "execution_outcomes.csv", "exit_attribution.csv", "trade_statistics.json",
    "leader_recall.json", "performance.json", "ablation_results.csv",
    "agent_events.jsonl", "checkpoint.json", "report.md",
})
_RUN_FILES = _ARTIFACTS | {"manifest.json"}
_DIGEST = frozenset("0123456789abcdef")
_MANIFEST_KEYS = frozenset({
    "schema_version", "status", "source_commit", "source_fingerprint_sha256", "bundle_sha256",
    "baseline_manifest_sha256", "rulebook_sha256", "catalog_sha256", "fact_cache_sha256",
    "result_count", "fidelity_label", "promotion_eligible_candidates", "artifact_sha256",
})
_CSV_COLUMNS = {
    "rule_attribution.csv": ("experiment_id", "partition", "identity_sha256", "result_sha256", "rule_id", "evaluated", "survivors", "passed", "failed", "unavailable"),
    "entry_funnel.csv": ("experiment_id", "partition", "identity_sha256", "result_sha256", "evaluated", "qualified", "attempted", "executed", "rejected"),
    "execution_outcomes.csv": ("experiment_id", "partition", "identity_sha256", "result_sha256", "evaluated", "qualified", "attempted", "executed", "rejected", "rejection_counts"),
    "exit_attribution.csv": ("experiment_id", "partition", "identity_sha256", "result_sha256", "reason", "closed_positions", "wins", "win_rate_pct", "average_completed_position_return_pct"),
    "ablation_results.csv": ("experiment_id", "partition", "identity_sha256", "result_sha256", "fidelity_label", "promotion_eligible", "promotion_checks", "trade_path_sha256"),
}
_RAW_TERMS = frozenset({"ticker", "transaction", "price", "quantity", "action", "provider", "payload", "leader_label", "raw"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _write_bytes(path: Path, value: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, _canonical(value))


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(columns), lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in columns})
        handle.flush()
        os.fsync(handle.fileno())


def _rulebook_payload(rulebook: Rulebook) -> dict[str, object]:
    return {
        "version": rulebook.version,
        "sources": {
            key: {"title": item.title, "url": item.url, "source_location": item.source_location}
            for key, item in sorted(rulebook.sources.items())
        },
        "rules": {
            key: {
                "letter_or_domain": item.letter_or_domain, "requirement": item.requirement,
                "classification": item.classification.value, "observability": item.observability.value,
                "parameter_policy": dict(item.parameter_policy), "source_id": item.source_id,
                "source_location": item.source_location,
                "implementation_status": item.implementation_status.value,
                **({"satisfaction_logic": item.satisfaction_logic} if item.satisfaction_logic != "all" else {}),
            }
            for key, item in sorted(rulebook.rules.items())
        },
    }


def _catalog_payload(catalog: ExperimentCatalog) -> dict[str, object]:
    return {
        "version": catalog.version,
        "experiments": [
            {
                "experiment_id": item.experiment_id, "phase": item.phase, "domain": item.domain,
                "kind": item.kind.value, "changed_dimensions": list(item.changed_dimensions),
                "rule_ids": list(item.rule_ids), "promotion_eligible": item.promotion_eligible,
                "controller_composed": item.controller_composed, "requires_code": item.requires_code,
                "allowed_variant_ids": list(item.allowed_variant_ids),
            }
            for item in catalog.experiments.values()
        ],
    }


def _result_row(result: ExperimentResult) -> dict[str, object]:
    return {
        "experiment_id": result.experiment_id, "partition": result.partition.value,
        "identity_sha256": result.identity_sha256, "result_sha256": result.result_sha256,
    }


def _result_rows(results: Sequence[ExperimentResult]) -> tuple[ExperimentResult, ...]:
    if not results:
        raise ValueError("diagnosis publication requires at least one result")
    ordered = tuple(sorted(results, key=lambda item: (item.partition.value, item.experiment_id)))
    seen: set[tuple[str, str]] = set()
    for result in ordered:
        key = (result.partition.value, result.experiment_id)
        if key in seen:
            raise ValueError("diagnosis publication has duplicate experiment results")
        seen.add(key)
        if result.result_sha256 != _result_payload_sha256(result):
            raise ValueError("diagnosis result hash is stale")
    return ordered


def _fact_cache_path(context: DiagnosisContext) -> Path:
    path = getattr(context.fact_cache, "path", None)
    if not isinstance(path, Path) or not path.is_file() or path.is_symlink():
        raise ValueError("diagnosis publication requires a regular immutable fact-cache file")
    return path.resolve()


def _digest_value(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _DIGEST:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _context_identities(context: DiagnosisContext, facts: Path) -> dict[str, str]:
    if len(context.source_commit) != 40 or set(context.source_commit) - _DIGEST:
        raise ValueError("source commit is invalid")
    snapshot, reproduction = context.baseline_snapshot, context.baseline_reproduction
    if snapshot is None or reproduction is None or not reproduction.passed:
        raise ValueError("publication requires a verified baseline reproduction")
    if reproduction.authority_manifest_sha256 != snapshot.manifest_sha256:
        raise ValueError("baseline reproduction identity is stale")
    return {
        "source_fingerprint_sha256": _digest_value(context.source_fingerprint_sha256, "source fingerprint"),
        "bundle_sha256": _digest_value(snapshot.bundle_sha256, "bundle"),
        "baseline_manifest_sha256": _digest_value(snapshot.manifest_sha256, "baseline manifest"),
        "rulebook_sha256": _digest_value(context.rulebook.sha256, "rulebook"),
        "catalog_sha256": _digest_value(context.catalog.sha256, "catalog"),
        "fact_cache_sha256": _sha256(facts),
    }


def _publication_rows(results: Sequence[ExperimentResult]) -> dict[str, object]:
    rule_rows: list[dict[str, object]] = []
    funnel_rows: list[dict[str, object]] = []
    execution_rows: list[dict[str, object]] = []
    exit_rows: list[dict[str, object]] = []
    trade_rows: list[dict[str, object]] = []
    leader_rows: list[dict[str, object]] = []
    performance_rows: list[dict[str, object]] = []
    ablations: list[dict[str, object]] = []
    for result in results:
        base = _result_row(result)
        for item in result.rule_attribution:
            rule_rows.append({**base, "rule_id": item.rule_id, "evaluated": item.evaluated, "survivors": item.survivors, "passed": item.passed, "failed": item.failed, "unavailable": item.unavailable})
        funnel = result.entry_funnel
        funnel_row = {**base, "evaluated": funnel.evaluated, "qualified": funnel.qualified, "attempted": funnel.attempted, "executed": funnel.executed, "rejected": funnel.attempted - funnel.executed}
        funnel_rows.append(funnel_row)
        execution_rows.append({**funnel_row, "rejection_counts": json.dumps(dict(funnel.rejections), sort_keys=True, separators=(",", ":"))})
        for reason, item in result.exit_attribution.by_reason.items():
            exit_rows.append({**base, "reason": reason, "closed_positions": item.closed_positions, "wins": item.wins, "win_rate_pct": item.win_rate_pct, "average_completed_position_return_pct": item.average_completed_position_return_pct})
        trade_rows.append({**base, **result.trade_statistics.__dict__})
        leader_rows.append({**base, "labelled_leaders": result.leader_recall.labelled_leaders, "pit_exposed_leaders": result.leader_recall.pit_exposed_leaders, "recalled_leaders": result.leader_recall.recalled_leaders, "recall_pct": result.leader_recall.recall_pct})
        if result.performance is not None:
            performance_rows.append({**base, **{key: value for key, value in result.performance.__dict__.items() if key != "partition"}})
        ablations.append({**base, "fidelity_label": result.fidelity.label.value, "promotion_eligible": result.promotion_eligible, "promotion_checks": json.dumps(dict(result.promotion_checks), sort_keys=True, separators=(",", ":")), "trade_path_sha256": result.trade_path_sha256})
    return {
        "rule": rule_rows, "funnel": funnel_rows, "execution": execution_rows, "exit": exit_rows,
        "trade": trade_rows, "leader": leader_rows, "performance": performance_rows, "ablation": ablations,
    }


def _report(results: Sequence[ExperimentResult]) -> str:
    labels = sorted({item.fidelity.label.value for item in results})
    missing = sorted({rule for item in results for rule in item.fidelity.unavailable_required_rule_ids})
    average_cash = [item.performance.average_cash_pct for item in results if item.performance is not None]
    return "\n".join((
        "# Offline PIT CANSLIM diagnosis",
        "", "## Status", "", "- Publication is deterministic and aggregate-only.",
        f"- Results: {len(results)}", f"- Fidelity: {', '.join(labels)}",
        f"- Missing required fidelity: {', '.join(missing) if missing else 'none'}",
        f"- Average cash evidence: {sum(average_cash) / len(average_cash):.6f}%" if average_cash else "- Average cash evidence: unavailable",
        "- Promotion-eligible candidates: 0", "",
    ))


def publish_diagnosis(context: DiagnosisContext, results: Sequence[ExperimentResult], output_root: Path) -> Path:
    """Publish one new run atomically; never mutate an existing publication."""
    ordered = _result_rows(results)
    root = Path(output_root)
    if root.exists() and (root / "manifest.json").exists():
        raise FileExistsError("diagnosis run directory already exists")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("diagnosis output root must not be a symlink")
    facts = _fact_cache_path(context)
    identities = _context_identities(context, facts)
    fact_sha = identities["fact_cache_sha256"]
    declared_fact_sha = getattr(context.fact_cache, "content_sha256", None)
    if isinstance(declared_fact_sha, str) and declared_fact_sha != fact_sha:
        raise ValueError("fact cache content SHA-256 changed before publication")
    name = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{context.baseline_snapshot.bundle_sha256[:12] if context.baseline_snapshot else fact_sha[:12]}"
    destination = root / name
    if destination.exists():
        raise FileExistsError("diagnosis run directory already exists")
    stage = root / f"staging-{name}"
    stage.mkdir()
    try:
        rows = _publication_rows(ordered)
        _write_json(stage / "rulebook.json", _rulebook_payload(context.rulebook))
        shutil.copyfile(facts, stage / "diagnosis_facts.sqlite3")
        _write_json(stage / "baseline_reproduction.json", {
            "passed": bool(context.baseline_reproduction and context.baseline_reproduction.passed),
            "authority_manifest_sha256": context.baseline_reproduction.authority_manifest_sha256 if context.baseline_reproduction else "",
            "reproduced_manifest_sha256": context.baseline_reproduction.reproduced_manifest_sha256 if context.baseline_reproduction else "",
            "mismatch_codes": list(context.baseline_reproduction.mismatch_codes) if context.baseline_reproduction else [],
        })
        _write_json(stage / "experiment_catalog.json", _catalog_payload(context.catalog))
        _write_csv(stage / "rule_attribution.csv", ("experiment_id", "partition", "identity_sha256", "result_sha256", "rule_id", "evaluated", "survivors", "passed", "failed", "unavailable"), rows["rule"])
        _write_csv(stage / "entry_funnel.csv", ("experiment_id", "partition", "identity_sha256", "result_sha256", "evaluated", "qualified", "attempted", "executed", "rejected"), rows["funnel"])
        _write_csv(stage / "execution_outcomes.csv", ("experiment_id", "partition", "identity_sha256", "result_sha256", "evaluated", "qualified", "attempted", "executed", "rejected", "rejection_counts"), rows["execution"])
        _write_csv(stage / "exit_attribution.csv", ("experiment_id", "partition", "identity_sha256", "result_sha256", "reason", "closed_positions", "wins", "win_rate_pct", "average_completed_position_return_pct"), rows["exit"])
        _write_json(stage / "trade_statistics.json", {"results": rows["trade"]})
        _write_json(stage / "leader_recall.json", {"results": rows["leader"]})
        _write_json(stage / "performance.json", {"results": rows["performance"]})
        _write_csv(stage / "ablation_results.csv", ("experiment_id", "partition", "identity_sha256", "result_sha256", "fidelity_label", "promotion_eligible", "promotion_checks", "trade_path_sha256"), rows["ablation"])
        _write_bytes(stage / "agent_events.jsonl", b"")
        _write_json(stage / "checkpoint.json", {"schema_version": 1, "result_count": len(ordered), "results": [_result_row(item) for item in ordered]})
        _write_bytes(stage / "report.md", _report(ordered).encode("utf-8"))
        if _context_identities(context, facts) != identities:
            raise ValueError("source, bundle, rulebook, catalog, fact-cache, or baseline identity changed before publication")
        hashes = {name: _sha256(stage / name) for name in sorted(_ARTIFACTS)}
        fidelity_labels = sorted({item.fidelity.label.value for item in ordered})
        manifest = {
            "schema_version": 1, "status": "complete", "source_commit": context.source_commit,
            "source_fingerprint_sha256": context.source_fingerprint_sha256,
            "bundle_sha256": identities["bundle_sha256"], "baseline_manifest_sha256": identities["baseline_manifest_sha256"],
            "rulebook_sha256": identities["rulebook_sha256"], "catalog_sha256": identities["catalog_sha256"],
            "fact_cache_sha256": fact_sha, "result_count": len(ordered),
            "fidelity_label": "fidelity_incomplete" if "fidelity_incomplete" in fidelity_labels else fidelity_labels[0],
            "promotion_eligible_candidates": 0, "artifact_sha256": hashes,
        }
        _write_json(stage / "manifest.json", manifest)
        if {item.name for item in stage.iterdir()} != _RUN_FILES:
            raise ValueError("diagnosis staging artifacts are incomplete")
        os.replace(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite value: {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid diagnosis artifact: {path.name}") from exc


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("diagnosis artifact contains a non-finite number")
    if isinstance(value, Mapping):
        for item in value.values(): _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value: _reject_nonfinite(item)


def _reject_text(value: object) -> None:
    if isinstance(value, str):
        normalized = value.strip().lower().strip(".,:;()[]{}")
        if normalized in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            raise ValueError("diagnosis artifact contains a non-finite value")
        if normalized in _RAW_TERMS:
            raise ValueError("diagnosis artifact contains a raw field")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _RAW_TERMS:
                raise ValueError("diagnosis artifact contains a raw field")
            _reject_text(item)
    elif isinstance(value, list):
        for item in value:
            _reject_text(item)


def _csv_rows(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != expected:
                raise ValueError("diagnosis CSV schema is invalid")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError("diagnosis CSV is invalid") from exc
    for row in rows:
        if set(row) != set(expected) or any(key.lower() in _RAW_TERMS for key in row):
            raise ValueError("diagnosis CSV contains a raw field or malformed schema")
        _reject_text(row)
    return rows


def _verify_json_rows(path: Path, expected: frozenset[str]) -> list[Mapping[str, object]]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or set(payload) != {"results"} or not isinstance(payload["results"], list):
        raise ValueError("diagnosis JSON schema is invalid")
    rows = payload["results"]
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError("diagnosis JSON row schema is invalid")
        _reject_nonfinite(row); _reject_text(row)
    return rows


def verify_diagnosis_run(run_dir: Path) -> Mapping[str, object]:
    """Verify an aggregate-only, complete diagnosis publication fail-closed."""
    directory = Path(run_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("diagnosis run directory must be a regular directory")
    names = {item.name for item in directory.iterdir()}
    if names != _RUN_FILES:
        raise ValueError("diagnosis run has missing, extra, or sidecar artifacts")
    if any(not item.is_file() or item.is_symlink() for item in directory.iterdir()):
        raise ValueError("diagnosis run has a non-regular artifact")
    manifest = _load_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS or manifest.get("schema_version") != 1 or manifest.get("status") != "complete":
        raise ValueError("diagnosis manifest schema is incomplete")
    if not isinstance(manifest.get("source_commit"), str) or len(manifest["source_commit"]) != 40 or set(manifest["source_commit"]) - _DIGEST:
        raise ValueError("diagnosis manifest source identity is invalid")
    for name in ("source_fingerprint_sha256", "bundle_sha256", "baseline_manifest_sha256", "rulebook_sha256", "catalog_sha256", "fact_cache_sha256"):
        _digest_value(manifest.get(name), f"manifest {name}")
    hashes = manifest.get("artifact_sha256")
    if not isinstance(hashes, dict) or set(hashes) != _ARTIFACTS:
        raise ValueError("diagnosis manifest artifact set is invalid")
    for name, digest in hashes.items():
        if not isinstance(digest, str) or len(digest) != 64 or set(digest) - _DIGEST or _sha256(directory / name) != digest:
            raise ValueError(f"diagnosis artifact hash mismatch: {name}")
    if (directory / "agent_events.jsonl").read_bytes() != b"":
        raise ValueError("agent events must be the empty hash-chained genesis log")
    rulebook = load_rulebook(directory / "rulebook.json")
    if rulebook.sha256 != manifest["rulebook_sha256"]:
        raise ValueError("diagnosis rulebook identity is invalid")
    catalog = load_experiment_catalog(directory / "experiment_catalog.json", rulebook)
    if catalog.sha256 != manifest["catalog_sha256"]:
        raise ValueError("diagnosis catalog identity is invalid")
    try:
        with open_fact_cache(directory / "diagnosis_facts.sqlite3", manifest["fact_cache_sha256"]):
            pass
    except Exception as exc:
        raise ValueError("diagnosis fact-cache schema or identity is invalid") from exc
    baseline = _load_json(directory / "baseline_reproduction.json")
    if not isinstance(baseline, dict) or set(baseline) != {"passed", "authority_manifest_sha256", "reproduced_manifest_sha256", "mismatch_codes"} or baseline.get("passed") is not True or baseline.get("authority_manifest_sha256") != manifest["baseline_manifest_sha256"] or not isinstance(baseline.get("mismatch_codes"), list):
        raise ValueError("diagnosis baseline identity is invalid")
    _reject_nonfinite(baseline); _reject_text(baseline)
    for name, columns in _CSV_COLUMNS.items():
        _csv_rows(directory / name, columns)
    _verify_json_rows(directory / "trade_statistics.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "completed_positions", "wins", "losses", "win_rate_pct", "mean_return_pct", "median_return_pct", "mean_winner_pct", "mean_loser_pct", "expectancy_pct", "mean_calendar_hold_days", "median_calendar_hold_days", "mean_trading_session_hold_days", "median_trading_session_hold_days"}))
    _verify_json_rows(directory / "leader_recall.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "labelled_leaders", "pit_exposed_leaders", "recalled_leaders", "recall_pct"}))
    _verify_json_rows(directory / "performance.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "total_return_pct", "annualized_return_pct", "sharpe_ratio", "max_drawdown_pct", "average_cash_pct", "closed_positions", "benchmark_total_return_delta_pct", "benchmark_annualized_return_delta_pct"}))
    report = (directory / "report.md").read_text(encoding="utf-8")
    _reject_text(report.split())
    checkpoint = _load_json(directory / "checkpoint.json")
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"schema_version", "result_count", "results"} or checkpoint.get("schema_version") != 1 or type(checkpoint.get("result_count")) is not int or not isinstance(checkpoint.get("results"), list) or checkpoint.get("result_count") != len(checkpoint["results"]):
        raise ValueError("diagnosis checkpoint is stale or inconsistent")
    ablation_count = len(_csv_rows(directory / "ablation_results.csv", _CSV_COLUMNS["ablation_results.csv"]))
    if ablation_count != checkpoint["result_count"] or manifest.get("result_count") != ablation_count:
        raise ValueError("diagnosis result counts are inconsistent")
    if manifest.get("promotion_eligible_candidates") != 0:
        raise ValueError("diagnosis publication cannot declare promotable candidates")
    return {"status": manifest["status"], "fidelity_label": manifest["fidelity_label"], "artifact_sha256": dict(hashes), "result_count": manifest["result_count"], "run_dir": str(directory.resolve())}
