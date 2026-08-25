"""Atomic, aggregate-only publication for offline PIT diagnosis runs."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .experiments import DiagnosisContext, ExperimentResult, _result_payload_sha256
from .fact_cache import _FACT_COLUMNS, _HASHED_ROW_COLUMNS, _SCHEMA_SHA256, _SCHEMA_VERSION, open_fact_cache
from .models import ExperimentCatalog, Rulebook
from .catalog import load_experiment_catalog
from .rulebook import canonical_sha256, load_rulebook


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
_PARTITIONS = frozenset({"discovery", "validation", "locked_evaluation"})
_FIDELITY_LABELS = frozenset({"strict_canslim", "quantitative_canslim_proxy", "fidelity_incomplete"})
_EXIT_REASONS = frozenset({"stop_loss", "ma_violation", "time_stop", "end_of_test", "profit_zone", "structural_sell", "eight_week_hold"})
_FACT_INTEGER_COLUMNS = frozenset({"member", "institutional_holder_count", "institutional_previous_holder_count", "industry_rank", "base_duration_sessions", "distribution_count", "availability_bitset"})
_FACT_REAL_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "prior_close", "prior_average_volume_50", "event_volume_ratio", "current_eps", "prior_year_eps", "current_sales", "prior_year_sales", "annual_eps_1", "annual_eps_2", "annual_eps_3", "annual_eps_4", "net_income", "total_stockholders_equity", "current_eps_yoy", "sales_yoy", "roe", "shares_outstanding", "institutional_ownership_percent", "rs_rating", "base_low", "base_depth_pct", "pivot", "extension_pct"})
_PRICE_COLUMNS = frozenset({"open", "high", "low", "close", "volume"})
_PRICE_DERIVED_COLUMNS = frozenset({"prior_close", "prior_average_volume_50", "event_volume_ratio", "rs_rating", "base_kind", "base_start_session", "base_end_session", "base_duration_sessions", "base_low", "base_depth_pct", "base_handle_start_session", "base_handle_end_session", "base_input_sha256", "pivot", "extension_pct"})
_AVAILABILITY_BITS = frozenset({1, 2, 4, 8, 16, 32})
_AVAILABILITY_MASK = sum(_AVAILABILITY_BITS)
_FUNDAMENTAL_COLUMNS = frozenset({"current_eps", "prior_year_eps", "current_sales", "prior_year_sales", "annual_eps_1", "annual_eps_2", "annual_eps_3", "annual_eps_4", "net_income", "total_stockholders_equity", "current_eps_yoy", "sales_yoy", "roe", "shares_outstanding"})
_INSTITUTIONAL_COLUMNS = frozenset({"institutional_ownership_percent", "institutional_holder_count", "institutional_previous_holder_count", "institutional_as_of_date"})
_INDUSTRY_COLUMNS = frozenset({"industry_group_id", "industry_rank", "industry_as_of_date"})
_BASE_REQUIRED_COLUMNS = frozenset({"base_kind", "base_start_session", "base_end_session", "base_duration_sessions", "base_low", "base_depth_pct", "base_input_sha256", "pivot", "extension_pct"})
_BASE_HANDLE_COLUMNS = frozenset({"base_handle_start_session", "base_handle_end_session"})
_FACT_TYPES = {column: ("INTEGER" if column in _FACT_INTEGER_COLUMNS else "REAL" if column in _FACT_REAL_COLUMNS else "TEXT") for column in _FACT_COLUMNS}
_FACT_IDENTITY_KEYS = frozenset({"bundle_sha256", "bundle_schema_version", "bundle_metadata", "rulebook_version", "rulebook_sha256", "partitions", "supplemental_content_identity_sha256", "fact_cache_schema_version", "fact_cache_schema_sha256"})


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
        # The authority replay may be backed by an older immutable bundle.  A
        # diagnosis publication must identify the current PIT bundle used by
        # the fact cache; the baseline manifest remains separately bound below.
        "bundle_sha256": _digest_value(context.bundle_sha256 or snapshot.bundle_sha256, "bundle"),
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
    name = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{identities['bundle_sha256'][:12]}"
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
        if set(re.findall(r"[a-z]+", normalized)) & _RAW_TERMS:
            raise ValueError("diagnosis artifact contains a raw field")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _RAW_TERMS:
                raise ValueError("diagnosis artifact contains a raw field")
            _reject_text(item)
    elif isinstance(value, list):
        for item in value:
            _reject_text(item)


def _integer(value: object, field: str, *, minimum: int = 0, allow_text: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        number = value
    elif allow_text and isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        number = int(value)
    else:
        raise ValueError(f"{field} must be an integer")
    if number < minimum:
        raise ValueError(f"{field} is out of range")
    return number


def _finite_number(value: object, field: str, *, lower: float | None = None, upper: float | None = None, allow_text: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) and not (allow_text and isinstance(value, str)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    if lower is not None and number < lower or upper is not None and number > upper:
        raise ValueError(f"{field} is out of range")
    return number


def _identity_row(row: Mapping[str, object]) -> None:
    if not isinstance(row.get("experiment_id"), str) or not row["experiment_id"]:
        raise ValueError("diagnosis artifact experiment id is invalid")
    if row.get("partition") not in _PARTITIONS:
        raise ValueError("diagnosis artifact partition is invalid")
    _digest_value(row.get("identity_sha256"), "diagnosis artifact identity")
    _digest_value(row.get("result_sha256"), "diagnosis artifact result")


def _json_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    _reject_nonfinite(value); _reject_text(value)
    return value


def _evidence_ids(value: object, field: str) -> list[str]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be JSON")
    try:
        parsed = json.loads(value, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item for item in parsed):
        raise ValueError(f"{field} is invalid")
    return parsed


def _availability_evidence(row: Mapping[str, object]) -> None:
    """Validate the bitset as a complete, bidirectional evidence contract."""
    bitset = row["availability_bitset"]
    if type(bitset) is not int or bitset < 0 or bitset & ~_AVAILABILITY_MASK:
        raise ValueError("fact-cache availability is invalid")

    def has(bit: int) -> bool:
        return bitset & bit == bit

    price_available = has(1)
    if price_available != all(row[field] is not None for field in _PRICE_COLUMNS):
        raise ValueError("fact-cache price evidence is incomplete")
    if not price_available and any(row[field] is not None for field in _PRICE_DERIVED_COLUMNS):
        raise ValueError("fact-cache unavailable price evidence is not explicit")
    if not price_available and (has(16) or has(32)):
        raise ValueError("fact-cache price-derived availability is invalid")

    fundamentals_available = has(2)
    fundamental_represented = row["latest_fundamental_public_date"] is not None
    if fundamentals_available != fundamental_represented:
        raise ValueError("fact-cache fundamental availability is inconsistent")
    if not fundamentals_available and any(row[field] is not None for field in _FUNDAMENTAL_COLUMNS):
        raise ValueError("fact-cache unavailable fundamentals are not explicit")

    institutional_available = has(4)
    institutional_represented = all(row[field] is not None for field in _INSTITUTIONAL_COLUMNS)
    institutional_ids = _evidence_ids(row["institutional_evidence_ids"], "institutional evidence")
    if institutional_available != (institutional_represented and bool(institutional_ids)):
        raise ValueError("fact-cache institutional availability is inconsistent")
    if not institutional_available and (any(row[field] is not None for field in _INSTITUTIONAL_COLUMNS) or institutional_ids):
        raise ValueError("fact-cache unavailable institutional evidence is not explicit")

    industry_available = has(8)
    industry_represented = all(row[field] is not None for field in _INDUSTRY_COLUMNS)
    industry_members = _evidence_ids(row["industry_members"], "industry members")
    industry_ids = _evidence_ids(row["industry_evidence_ids"], "industry evidence")
    if industry_available != (industry_represented and bool(industry_members) and bool(industry_ids)):
        raise ValueError("fact-cache industry availability is inconsistent")
    if not industry_available and (any(row[field] is not None for field in _INDUSTRY_COLUMNS) or industry_members or industry_ids):
        raise ValueError("fact-cache unavailable industry evidence is not explicit")

    if has(16) != (row["rs_rating"] is not None):
        raise ValueError("fact-cache RS availability is inconsistent")

    base_available = has(32)
    base_represented = all(row[field] is not None for field in _BASE_REQUIRED_COLUMNS)
    handle_present = [row[field] is not None for field in _BASE_HANDLE_COLUMNS]
    if handle_present[0] != handle_present[1]:
        raise ValueError("fact-cache base handle evidence is incomplete")
    if base_available != base_represented:
        raise ValueError("fact-cache base availability is inconsistent")
    if not base_available and any(row[field] is not None for field in _BASE_REQUIRED_COLUMNS | _BASE_HANDLE_COLUMNS):
        raise ValueError("fact-cache unavailable base evidence is not explicit")


def _csv_json_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be JSON")
    try:
        return _json_object(json.loads(value, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item))), field)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be JSON") from exc


def _verify_csv_row(name: str, row: Mapping[str, str]) -> None:
    _identity_row(row)
    if name == "rule_attribution.csv":
        if not row["rule_id"]:
            raise ValueError("rule attribution rule id is invalid")
        evaluated, survivors, passed, failed, unavailable = (_integer(row[key], key, allow_text=True) for key in ("evaluated", "survivors", "passed", "failed", "unavailable"))
        if survivors > evaluated or passed + failed + unavailable != evaluated:
            raise ValueError("rule attribution counts are inconsistent")
    elif name in {"entry_funnel.csv", "execution_outcomes.csv"}:
        evaluated, qualified, attempted, executed, rejected = (_integer(row[key], key, allow_text=True) for key in ("evaluated", "qualified", "attempted", "executed", "rejected"))
        if not evaluated >= qualified >= attempted >= executed or rejected != attempted - executed:
            raise ValueError("entry funnel counts are inconsistent")
        if name == "execution_outcomes.csv":
            counts = _csv_json_object(row["rejection_counts"], "rejection_counts")
            if any(not isinstance(key, str) or not key or _integer(value, "rejection_counts") < 0 for key, value in counts.items()) or sum(_integer(value, "rejection_counts") for value in counts.values()) != rejected:
                raise ValueError("execution rejection counts are inconsistent")
    elif name == "exit_attribution.csv":
        if row["reason"] not in _EXIT_REASONS:
            raise ValueError("exit attribution reason is invalid")
        closed, wins = (_integer(row[key], key, allow_text=True) for key in ("closed_positions", "wins"))
        if wins > closed:
            raise ValueError("exit attribution wins are inconsistent")
        _finite_number(row["win_rate_pct"], "win_rate_pct", lower=0, upper=100, allow_text=True)
        _finite_number(row["average_completed_position_return_pct"], "average_completed_position_return_pct", lower=-100, upper=100, allow_text=True)
    else:
        if row["fidelity_label"] not in _FIDELITY_LABELS or row["promotion_eligible"] not in {"True", "False"}:
            raise ValueError("ablation domain is invalid")
        checks = _csv_json_object(row["promotion_checks"], "promotion_checks")
        if not checks or any(not isinstance(key, str) or type(value) is not bool for key, value in checks.items()):
            raise ValueError("promotion checks are invalid")
        _digest_value(row["trade_path_sha256"], "trade path")


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
        if set(row) != set(expected) or any(value is None for value in row.values()) or any(key.lower() in _RAW_TERMS for key in row):
            raise ValueError("diagnosis CSV contains a raw field or malformed schema")
        _reject_text(row)
        _verify_csv_row(path.name, row)
    return rows


def _verify_json_rows(path: Path, expected: frozenset[str], numeric: frozenset[str], counts: frozenset[str]) -> list[Mapping[str, object]]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or set(payload) != {"results"} or not isinstance(payload["results"], list):
        raise ValueError("diagnosis JSON schema is invalid")
    rows = payload["results"]
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError("diagnosis JSON row schema is invalid")
        _reject_nonfinite(row); _reject_text(row)
        _identity_row(row)
        for field in numeric:
            bounded = field in {"win_rate_pct", "recall_pct", "average_cash_pct"}
            _finite_number(row.get(field), field, lower=0 if bounded else None, upper=100 if bounded else None)
        for field in counts:
            _integer(row.get(field), field)
    return rows


def _verify_fact_cache(path: Path, manifest: Mapping[str, object]) -> None:
    try:
        with open_fact_cache(path, str(manifest["fact_cache_sha256"])) as cache:
            connection = cache._connection
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("fact-cache integrity check failed")
            table_info = tuple((str(row[1]), str(row[2]).upper()) for row in connection.execute("PRAGMA table_info(session_facts)"))
            if table_info != tuple((column, _FACT_TYPES[column]) for column in _FACT_COLUMNS):
                raise ValueError("fact-cache column types are invalid")
            metadata = {str(row[0]): str(row[1]) for row in connection.execute("SELECT key,value FROM metadata")}
            if set(metadata) != {"status", "identity_sha256", "identity", "schema_version", "schema_sha256", "content_sha256"} or metadata.get("status") != "complete" or metadata.get("schema_version") != _SCHEMA_VERSION or metadata.get("schema_sha256") != _SCHEMA_SHA256:
                raise ValueError("fact-cache metadata is invalid")
            _digest_value(metadata.get("identity_sha256"), "fact-cache identity")
            _digest_value(metadata.get("content_sha256"), "fact-cache logical content")
            identity = _json_object(json.loads(metadata["identity"], parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item))), "fact-cache identity")
            if set(identity) != _FACT_IDENTITY_KEYS or not isinstance(identity.get("bundle_schema_version"), str) or not isinstance(identity.get("rulebook_version"), str) or not isinstance(identity.get("fact_cache_schema_version"), str) or not isinstance(identity.get("bundle_metadata"), Mapping) or not isinstance(identity.get("partitions"), Mapping):
                raise ValueError("fact-cache identity schema is invalid")
            if canonical_sha256(identity) != metadata["identity_sha256"]:
                raise ValueError("fact-cache identity digest is stale")
            for field in ("bundle_sha256", "rulebook_sha256", "supplemental_content_identity_sha256", "fact_cache_schema_sha256"):
                _digest_value(identity.get(field), f"fact-cache {field}")
            partitions = identity["partitions"]
            if set(partitions) != _PARTITIONS or any(not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value) for value in partitions.values()):
                raise ValueError("fact-cache partition identity is invalid")
            if identity.get("bundle_sha256") != manifest["bundle_sha256"] or identity.get("rulebook_sha256") != manifest["rulebook_sha256"]:
                raise ValueError("fact-cache identities are inconsistent")
            row_hashes: list[str] = []
            for record in connection.execute("SELECT * FROM session_facts ORDER BY session,symbol"):
                row = dict(record)
                _reject_text(row)
                if row["bundle_sha256"] != manifest["bundle_sha256"] or row["member"] != 1:
                    raise ValueError("fact-cache row identity or membership is invalid")
                _digest_value(row["bundle_sha256"], "fact-cache bundle")
                _digest_value(row["row_sha256"], "fact-cache row")
                if canonical_sha256({field: row[field] for field in _HASHED_ROW_COLUMNS}) != row["row_sha256"]:
                    raise ValueError("fact-cache row digest is stale")
                row_hashes.append(row["row_sha256"])
                if not all(isinstance(row[field], str) and row[field] for field in ("rulebook_schema_version", "symbol", "session", "market_regime")):
                    raise ValueError("fact-cache text row value is invalid")
                _availability_evidence(row)
                for field in _FACT_REAL_COLUMNS:
                    if row[field] is not None:
                        _finite_number(row[field], f"fact-cache {field}")
                for field in _FACT_INTEGER_COLUMNS:
                    if row[field] is not None:
                        _integer(row[field], f"fact-cache {field}")
            logical = hashlib.sha256("".join(row_hashes).encode("ascii")).hexdigest()
            if logical != metadata["content_sha256"]:
                raise ValueError("fact-cache logical content digest is stale")
    except Exception as exc:
        raise ValueError("diagnosis fact-cache schema, content, or identity is invalid") from exc


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
    result_count = _integer(manifest.get("result_count"), "manifest result_count")
    if manifest.get("fidelity_label") not in _FIDELITY_LABELS:
        raise ValueError("diagnosis manifest fidelity label is invalid")
    promotion_candidates = _integer(manifest.get("promotion_eligible_candidates"), "manifest promotion candidates")
    if promotion_candidates != 0:
        raise ValueError("diagnosis publication promotion candidates must be zero")
    if (directory / "agent_events.jsonl").read_bytes() != b"":
        raise ValueError("agent events must be the empty hash-chained genesis log")
    rulebook = load_rulebook(directory / "rulebook.json")
    if rulebook.sha256 != manifest["rulebook_sha256"]:
        raise ValueError("diagnosis rulebook identity is invalid")
    catalog = load_experiment_catalog(directory / "experiment_catalog.json", rulebook)
    if catalog.sha256 != manifest["catalog_sha256"]:
        raise ValueError("diagnosis catalog identity is invalid")
    _verify_fact_cache(directory / "diagnosis_facts.sqlite3", manifest)
    baseline = _load_json(directory / "baseline_reproduction.json")
    if not isinstance(baseline, dict) or set(baseline) != {"passed", "authority_manifest_sha256", "reproduced_manifest_sha256", "mismatch_codes"} or baseline.get("passed") is not True or baseline.get("authority_manifest_sha256") != manifest["baseline_manifest_sha256"] or baseline.get("reproduced_manifest_sha256") != manifest["baseline_manifest_sha256"] or not isinstance(baseline.get("mismatch_codes"), list) or baseline["mismatch_codes"]:
        raise ValueError("diagnosis baseline identity is invalid")
    _digest_value(baseline["authority_manifest_sha256"], "baseline authority manifest")
    _digest_value(baseline["reproduced_manifest_sha256"], "baseline reproduced manifest")
    _reject_nonfinite(baseline); _reject_text(baseline)
    for name, columns in _CSV_COLUMNS.items():
        _csv_rows(directory / name, columns)
    trade_rows = _verify_json_rows(directory / "trade_statistics.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "completed_positions", "wins", "losses", "win_rate_pct", "mean_return_pct", "median_return_pct", "mean_winner_pct", "mean_loser_pct", "expectancy_pct", "mean_calendar_hold_days", "median_calendar_hold_days", "mean_trading_session_hold_days", "median_trading_session_hold_days"}), frozenset({"win_rate_pct", "mean_return_pct", "median_return_pct", "mean_winner_pct", "mean_loser_pct", "expectancy_pct", "mean_calendar_hold_days", "median_calendar_hold_days", "mean_trading_session_hold_days", "median_trading_session_hold_days"}), frozenset({"completed_positions", "wins", "losses"}))
    for row in trade_rows:
        if _integer(row["wins"], "wins") + _integer(row["losses"], "losses") != _integer(row["completed_positions"], "completed_positions"):
            raise ValueError("trade statistic counts are inconsistent")
    leader_rows = _verify_json_rows(directory / "leader_recall.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "labelled_leaders", "pit_exposed_leaders", "recalled_leaders", "recall_pct"}), frozenset({"recall_pct"}), frozenset({"labelled_leaders", "pit_exposed_leaders", "recalled_leaders"}))
    for row in leader_rows:
        if _integer(row["pit_exposed_leaders"], "pit exposed leaders") > _integer(row["labelled_leaders"], "labelled leaders") or _integer(row["recalled_leaders"], "recalled leaders") > _integer(row["pit_exposed_leaders"], "pit exposed leaders"):
            raise ValueError("leader recall counts are inconsistent")
    _verify_json_rows(directory / "performance.json", frozenset({"experiment_id", "partition", "identity_sha256", "result_sha256", "total_return_pct", "annualized_return_pct", "sharpe_ratio", "max_drawdown_pct", "average_cash_pct", "closed_positions", "benchmark_total_return_delta_pct", "benchmark_annualized_return_delta_pct"}), frozenset({"total_return_pct", "annualized_return_pct", "sharpe_ratio", "max_drawdown_pct", "average_cash_pct", "benchmark_total_return_delta_pct", "benchmark_annualized_return_delta_pct"}), frozenset({"closed_positions"}))
    report = (directory / "report.md").read_text(encoding="utf-8")
    _reject_text(report.split())
    checkpoint = _load_json(directory / "checkpoint.json")
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"schema_version", "result_count", "results"} or checkpoint.get("schema_version") != 1 or not isinstance(checkpoint.get("results"), list) or _integer(checkpoint.get("result_count"), "checkpoint result_count") != len(checkpoint["results"]):
        raise ValueError("diagnosis checkpoint is stale or inconsistent")
    checkpoint_ids: set[tuple[str, str, str, str]] = set()
    for row in checkpoint["results"]:
        if not isinstance(row, Mapping) or set(row) != {"experiment_id", "partition", "identity_sha256", "result_sha256"}:
            raise ValueError("diagnosis checkpoint result is invalid")
        _identity_row(row)
        checkpoint_ids.add((str(row["experiment_id"]), str(row["partition"]), str(row["identity_sha256"]), str(row["result_sha256"])))
    if len(checkpoint_ids) != len(checkpoint["results"]):
        raise ValueError("diagnosis checkpoint has duplicate results")
    ablation_count = len(_csv_rows(directory / "ablation_results.csv", _CSV_COLUMNS["ablation_results.csv"]))
    if ablation_count != checkpoint["result_count"] or result_count != ablation_count:
        raise ValueError("diagnosis result counts are inconsistent")
    return {"status": manifest["status"], "fidelity_label": manifest["fidelity_label"], "artifact_sha256": dict(hashes), "result_count": result_count, "run_dir": str(directory.resolve())}
