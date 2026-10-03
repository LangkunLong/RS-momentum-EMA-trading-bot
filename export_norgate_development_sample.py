"""Retain a bounded native Norgate sample and project valid inputs to V3 CSVs.

The capture command is a dry run unless ``--execute-read-only`` is supplied.
Projection is entirely offline and never constructs a PIT bundle or evaluator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

MAX_CALENDAR_DAYS = 31
EXPECTED_READS = 8
INDEXES = ("$SPX", "$NDX", "$RUT")
INDEX_TO_UNIVERSE = {"$SPX": "sp500", "$NDX": "nasdaq100", "$RUT": "russell2000"}
PRICE_COLUMNS_V3 = ("trade_date", "ticker", "open", "high", "low", "close", "volume")
MEMBERSHIP_COLUMNS_V3 = ("effective_date", "security_lineage_id", "universe_id", "member")
EVALUATOR_MISSING_INPUTS = [
    "financials_and_provenance",
    "industry_and_provenance",
    "feature_snapshot",
    "continuous_warmup",
    "benchmark_market_context",
    "matched_strategy_baseline",
]
_SESSION_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SESSION_TIMESTAMP_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<timezone>Z|[+-]\d{2}:\d{2})?\Z"
)


class CaptureFailure(RuntimeError):
    """A bounded capture stopped; the partial generation remains available."""

    def __init__(self, generation_dir: Path, message: str) -> None:
        super().__init__(message)
        self.generation_dir = generation_dir


def _iso_date(value: str, *, field: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO calendar date")
    return value


def _validate_window(start_date: str, end_date: str) -> tuple[str, str]:
    start = _iso_date(start_date, field="start_date")
    end = _iso_date(end_date, field="end_date")
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if span <= 0:
        raise ValueError("end_date must be on or after start_date")
    if span > MAX_CALENDAR_DAYS:
        raise ValueError("window may not exceed 31 calendar days")
    return start, end


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _as_native_frame(frame: Any) -> Any:
    """Preserve all rows and columns, making a date index explicit for CSV."""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("provider returned a non-DataFrame result")
    native = frame.copy()
    if isinstance(native.index, pd.MultiIndex):
        raise ValueError("multi-index native frames are unsupported for this sample")
    index_name = native.index.name or "index"
    if index_name in native.columns:
        index_name = "native_index"
    native.index.name = "session_date"
    native = native.reset_index()
    if index_name != "session_date" and "session_date" not in native.columns:
        native = native.rename(columns={index_name: "session_date"})
    if "session_date" not in native.columns:
        # reset_index uses the explicit index name, so this is defensive.
        native = native.rename(columns={native.columns[0]: "session_date"})
    return native


def _frame_metadata(frame: Any, path: Path, relative_path: str) -> dict[str, Any]:
    native = _as_native_frame(frame)
    native.to_csv(path, index=False, lineterminator="\n")
    return {
        "path": relative_path,
        "rows": int(len(native.index)),
        "columns": [str(column) for column in native.columns],
        "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        "sha256": _sha256(path),
    }


def capture_native_generation(
    provider: Any,
    *,
    start_date: str,
    end_date: str,
    private_root: str | Path,
    sdk_version: str,
    acquisition_metadata: dict[str, Any] | None = None,
    acquisition_metadata_bytes: bytes | None = None,
    acquisition_evidence_root: str | Path | None = None,
) -> Path:
    """Perform the fixed eight reads and retain every returned frame privately."""
    start, end = _validate_window(start_date, end_date)
    root = Path(private_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    generation_id = f"norgate-msft-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    generation_dir = root / generation_id
    generation_dir.mkdir(exist_ok=False)

    if acquisition_metadata is not None and acquisition_metadata_bytes is None:
        acquisition_metadata_bytes = (json.dumps(acquisition_metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")
    acquisition_context = _assess_acquisition_context(
        acquisition_metadata,
        acquisition_metadata_bytes,
        Path(acquisition_evidence_root).resolve() if acquisition_evidence_root is not None else None,
        start_date=start,
        end_date=end,
    )
    if acquisition_metadata_bytes is not None:
        (generation_dir / "acquisition_metadata.json").write_bytes(acquisition_metadata_bytes)

    source_class = str(getattr(provider, "source_class", "vendor_provider"))
    manifest: dict[str, Any] = {
        "schema": "norgate_native_sample_generation/v1",
        "generation_id": generation_id,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "capture_status": "in_progress",
        "source_class": source_class,
        "production_admission": False,
        "provider": "Norgate",
        "sdk_version": str(sdk_version),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "query": {
            "symbol_requested": "MSFT",
            "start_date": start,
            "end_date": end,
            "adjustment_setting": "CAPITAL",
            "padding_setting": "NONE",
            "frame_format": "pandas-dataframe",
            "membership_indexes": list(INDEXES),
            "membership_seed": "returned prices frame copy",
        },
        "sdk_reads_planned": EXPECTED_READS,
        "sdk_reads_attempted": 0,
        "sdk_reads_completed": 0,
        "retry_count": 0,
        "read_outcomes": [],
        "acquisition_context": acquisition_context,
        "native_frames": {"membership": {}},
    }
    manifest_path = generation_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    current_stage = "initialization"

    def read(stage: str, operation: Any) -> Any:
        nonlocal current_stage
        current_stage = stage
        manifest["sdk_reads_attempted"] += 1
        outcome: dict[str, Any] = {"read_number": manifest["sdk_reads_attempted"], "stage": stage, "status": "attempted"}
        manifest["read_outcomes"].append(outcome)
        _write_json(manifest_path, manifest)
        result = operation()
        manifest["sdk_reads_completed"] += 1
        outcome["status"] = "completed"
        _write_json(manifest_path, manifest)
        return result

    try:
        status_ok = bool(read("status", provider.status))
        manifest["provider_status_ok"] = status_ok
        _write_json(manifest_path, manifest)
        if not status_ok:
            raise RuntimeError("provider status check did not report ready")
        requested_asset_id = read("assetid_requested_symbol", lambda: provider.assetid("MSFT"))
        if type(requested_asset_id) is not int or requested_asset_id <= 0:
            raise ValueError("requested-symbol asset ID must be a positive exact integer")
        resolved_symbol = read("symbol_for_asset", lambda: provider.symbol(requested_asset_id))
        if not isinstance(resolved_symbol, str) or not resolved_symbol.strip():
            raise ValueError("resolved provider symbol must be nonempty text")
        roundtrip_asset_id = read("assetid_resolved_symbol", lambda: provider.assetid(resolved_symbol))
        if type(roundtrip_asset_id) is not int or roundtrip_asset_id <= 0:
            raise ValueError("round-trip asset ID must be a positive exact integer")
        manifest["identity_resolution"] = {
            "requested_symbol": "MSFT",
            "requested_symbol_asset_id": requested_asset_id,
            "resolved_symbol": str(resolved_symbol),
            "resolved_symbol_asset_id": roundtrip_asset_id,
            "roundtrip_matches": requested_asset_id == roundtrip_asset_id,
        }
        _write_json(manifest_path, manifest)
        if roundtrip_asset_id != requested_asset_id:
            raise ValueError("resolved-symbol asset ID does not match requested-symbol asset ID")

        adjustment = getattr(getattr(provider, "StockPriceAdjustmentType", object), "CAPITAL", "CAPITAL")
        padding = getattr(getattr(provider, "PaddingType", object), "NONE", "NONE")
        prices = read(
            "price_timeseries",
            lambda: provider.price_timeseries(
                requested_asset_id,
                stock_price_adjustment_setting=adjustment,
                padding_setting=padding,
                start_date=start,
                end_date=end,
                timeseriesformat="pandas-dataframe",
            ),
        )
        current_stage = "retain_prices"
        manifest["native_frames"]["prices"] = _frame_metadata(prices, generation_dir / "prices.csv", "prices.csv")
        _write_json(manifest_path, manifest)

        for index_name in INDEXES:
            safe_name = index_name.strip("$").lower()
            frame = read(
                f"membership:{index_name}",
                lambda index_name=index_name: provider.index_constituent_timeseries(
                    requested_asset_id,
                    index_name,
                    padding_setting=padding,
                    pandas_dataframe=prices.copy(),
                    timeseriesformat="pandas-dataframe",
                ),
            )
            current_stage = f"retain_membership:{index_name}"
            manifest["native_frames"]["membership"][index_name] = _frame_metadata(
                frame,
                generation_dir / f"membership_{safe_name}.csv",
                f"membership_{safe_name}.csv",
            )
            _write_json(manifest_path, manifest)

        manifest["capture_status"] = "completed"
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(manifest_path, manifest)
        return generation_dir
    except Exception as exc:
        manifest["capture_status"] = "failed"
        manifest["failure"] = {
            "stage": current_stage,
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        manifest["retry_count"] = 0
        _write_json(manifest_path, manifest)
        raise CaptureFailure(generation_dir, f"capture failed at {current_stage}: {exc}") from exc


def _safe_evidence(evidence_root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    root = evidence_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return candidate


def _verify_evidence(root: Path, relative: object, expected_hash: object) -> bool:
    path = _safe_evidence(root, relative)
    if path is None or not isinstance(expected_hash, str):
        return False
    try:
        return _sha256(path) == expected_hash
    except OSError:
        return False


def _assess_acquisition_context(
    metadata: dict[str, Any] | None,
    metadata_bytes: bytes | None,
    evidence_root: Path | None,
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Bind supplied setup facts without claiming independently observed coherence."""
    gaps: list[str] = []
    if metadata is None:
        return {
            "status": "unverified_missing_context",
            "source_class": "unknown",
            "metadata_path": None,
            "metadata_sha256": None,
            "capture_executable_sha256": _sha256(Path(__file__).resolve()),
            "capture_coherence": {
                "status": "unknown_unverified",
                "independent_verification": False,
                "source_claim": None,
            },
            "unresolved_fields": [
                "product_name",
                "norgate_data_updater_version",
                "database_identity",
                "entitlement_and_date_bounds",
                "update_identity_and_coherence",
                "retention_basis",
                "updater_executable_hash",
                "source_evidence",
            ],
        }
    if metadata_bytes is None:
        metadata_bytes = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")
    metadata_hash = hashlib.sha256(metadata_bytes).hexdigest()
    try:
        if json.loads(metadata_bytes.decode("utf-8")) != metadata:
            gaps.append("metadata_bytes_object_mismatch")
    except (UnicodeDecodeError, json.JSONDecodeError):
        gaps.append("metadata_bytes_not_json")
    if metadata.get("schema") != "norgate_acquisition_context/v1":
        gaps.append("schema")
    for field in ("product_name", "norgate_data_updater_version", "database_identity", "retention_basis"):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            gaps.append(field)

    entitlement = metadata.get("entitlement")
    if not isinstance(entitlement, dict):
        gaps.append("entitlement")
        entitlement = {}
    try:
        entitlement_start = _iso_date(entitlement.get("date_start"), field="entitlement date_start")
        entitlement_end = _iso_date(entitlement.get("date_end"), field="entitlement date_end")
        if entitlement.get("status") != "confirmed_active":
            gaps.append("entitlement_status")
        if entitlement_start > start_date or entitlement_end < end_date:
            gaps.append("entitlement_date_bounds_do_not_cover_query")
    except (TypeError, ValueError):
        gaps.append("entitlement_date_bounds")

    update = metadata.get("update")
    if not isinstance(update, dict):
        gaps.append("update")
        update = {}
    if update.get("status") != "completed":
        gaps.append("update_status")
    if not isinstance(update.get("generation_id"), str) or not update["generation_id"].strip():
        gaps.append("update_generation_id")
    try:
        completed_at = datetime.fromisoformat(str(update.get("completed_at_utc", "")).replace("Z", "+00:00"))
        if completed_at.tzinfo is None:
            gaps.append("update_completed_at_timezone")
    except ValueError:
        gaps.append("update_completed_at_utc")
    coherence_claim = update.get("coherence_status")
    if coherence_claim != "single_generation_confirmed":
        gaps.append("update_coherence_claim")

    evidence = metadata.get("source_evidence")
    if not isinstance(evidence, list) or not evidence:
        gaps.append("source_evidence")
        evidence = []
    evidence_results: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            gaps.append("source_evidence_record_invalid")
            continue
        verified = evidence_root is not None and _verify_evidence(
            evidence_root, item.get("path"), item.get("sha256")
        )
        if not verified or not isinstance(item.get("source_locator"), str) or not item["source_locator"].strip():
            gaps.append("source_evidence_unverified")
        evidence_results.append({
            "path": item.get("path"),
            "sha256": item.get("sha256"),
            "source_locator": item.get("source_locator"),
            "hash_verified": bool(verified),
        })
    updater_path = metadata.get("updater_executable_path")
    updater_hash = metadata.get("updater_executable_sha256")
    updater_hash_verified = evidence_root is not None and _verify_evidence(evidence_root, updater_path, updater_hash)
    if not updater_hash_verified:
        gaps.append("updater_executable_hash_unverified")
    # No source claim is promoted to an independently observed update fact.
    coherence_status = (
        "source_attested_single_generation"
        if coherence_claim == "single_generation_confirmed" and not gaps
        else "unknown_unverified"
    )
    return {
        "status": "hash_bound_context_complete" if not gaps else "unverified_incomplete_context",
        "source_class": str(metadata.get("source_class", "unknown")),
        "metadata_path": "acquisition_metadata.json",
        "metadata_sha256": metadata_hash,
        "capture_executable_sha256": _sha256(Path(__file__).resolve()),
        "product_name": metadata.get("product_name"),
        "norgate_data_updater_version": metadata.get("norgate_data_updater_version"),
        "database_identity": metadata.get("database_identity"),
        "entitlement": entitlement,
        "update": update,
        "retention_basis": metadata.get("retention_basis"),
        "updater_executable_path": updater_path,
        "updater_executable_sha256": updater_hash,
        "updater_executable_hash_verified": bool(updater_hash_verified),
        "source_evidence": evidence_results,
        "capture_coherence": {
            "status": coherence_status,
            "independent_verification": False,
            "source_claim": coherence_claim,
        },
        "unresolved_fields": sorted(set(gaps)),
    }


def _read_calendar(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["session_date"]:
            raise ValueError("independent calendar must contain exactly session_date")
        dates = [_iso_date(row["session_date"], field="calendar session_date") for row in reader]
    if not dates or dates != sorted(set(dates)):
        raise ValueError("independent calendar must be nonempty, unique, and sorted")
    return dates


def _identity_rows(
    binding: dict[str, Any],
    evidence_root: Path,
    provider_asset_id: Any,
    resolved_symbol: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    if binding.get("schema") != "norgate_asset_identity_binding/v1" or binding.get("provider") != "Norgate":
        return [], ["identity_binding_schema_invalid"]
    rows = binding.get("asset_bindings")
    if not isinstance(rows, list) or not rows:
        return [], ["identity_binding_empty"]
    if len(rows) != 1:
        return [], ["identity_multiple_segments_not_supported"]
    source_class = binding.get("source_class")
    if not isinstance(source_class, str) or not source_class.strip():
        return [], ["identity_source_class_missing"]
    review_status = binding.get("assertion_review_status")
    if source_class == "synthetic_test_fixture":
        accepted_review = review_status == "synthetic_fixture_only"
    else:
        accepted_review = review_status == "accepted"
    if not accepted_review:
        return [], ["identity_assertion_not_reviewed"]
    parsed: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append("identity_binding_row_invalid")
            continue
        try:
            asset_id = row.get("provider_asset_id")
            start = _iso_date(row.get("effective_start"), field="identity effective_start")
            end = _iso_date(row.get("effective_end"), field="identity effective_end")
        except (TypeError, ValueError):
            errors.append("identity_binding_dates_invalid")
            continue
        if asset_id != provider_asset_id or date.fromisoformat(start) > date.fromisoformat(end):
            errors.append("identity_binding_asset_or_interval_invalid")
            continue
        ticker = row.get("ticker")
        lineage = row.get("security_lineage_id")
        symbol = row.get("provider_symbol")
        locator = row.get("source_locator")
        share_class = row.get("share_class")
        source_url = row.get("source_url")
        raw_hash = row.get("source_raw_sha256")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (ticker, lineage, symbol, locator, share_class, source_url)
        ):
            errors.append("identity_binding_fields_missing")
            continue
        try:
            document_date = _iso_date(row.get("source_document_date"), field="identity source document date")
        except (TypeError, ValueError):
            errors.append("identity_source_document_date_invalid")
            continue
        parsed_url = urlparse(source_url)
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
            errors.append("identity_source_url_invalid")
            continue
        if row.get("same_security_assertion") is not True:
            errors.append("identity_same_security_assertion_missing")
            continue
        if (
            len(ticker) > 15
            or not ticker[0].isalnum()
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for char in ticker)
            or len(lineage) > 48
            or not lineage[0].islower()
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in lineage)
        ):
            errors.append("identity_binding_fields_noncanonical")
            continue
        if symbol != resolved_symbol:
            errors.append("identity_provider_symbol_mismatch")
            continue
        if not isinstance(raw_hash, str) or raw_hash != row.get("source_evidence_sha256"):
            errors.append("identity_source_raw_hash_missing_or_mismatched")
            continue
        if not _verify_evidence(evidence_root, row.get("source_evidence_path"), row.get("source_evidence_sha256")):
            errors.append("identity_source_evidence_invalid")
            continue
        parsed.append({
            "start": start,
            "end": end,
            "ticker": ticker,
            "lineage": lineage,
            "symbol": symbol,
            "share_class": share_class,
            "document_date": document_date,
            "source_url": source_url,
            "source_locator": locator,
            "source_raw_sha256": raw_hash,
            "source_evidence_path": row.get("source_evidence_path"),
            "same_security_assertion": "true",
        })
    parsed.sort(key=lambda row: (row["start"], row["end"], row["lineage"]))
    return parsed, errors


def _identity_for_day(rows: list[dict[str, str]], session_date: str) -> dict[str, str] | None:
    matches = [row for row in rows if row["start"] <= session_date <= row["end"]]
    if len(matches) != 1:
        return None
    return matches[0]


def _member_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1) and math.isfinite(float(value)):
            return int(value)
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true"}:
            return 1
        if normalized in {"0", "false"}:
            return 0
    return None


def _read_native_frame(generation: Path, record: object) -> tuple[list[dict[str, str]], Path | None, str | None]:
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        return [], None, "native_frame_record_missing"
    path = generation / record["path"]
    try:
        path.resolve().relative_to(generation.resolve())
    except ValueError:
        return [], None, "native_frame_path_invalid"
    if path.is_symlink() or not path.is_file():
        return [], None, "native_frame_missing"
    if _sha256(path) != record.get("sha256"):
        return [], path, "native_frame_hash_mismatch"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error):
        return [], path, "native_frame_unreadable"
    if len(rows) != record.get("rows"):
        return rows, path, "native_frame_row_count_mismatch"
    return rows, path, None


def _frame_date(row: dict[str, str]) -> str | None:
    raw = row.get("session_date")
    if raw is None:
        return None
    candidate = raw.strip()
    if _SESSION_DATE_RE.fullmatch(candidate):
        try:
            return _iso_date(candidate, field="native session_date")
        except ValueError:
            return None
    match = _SESSION_TIMESTAMP_RE.fullmatch(candidate)
    if match is None:
        return None
    offset = match.group("timezone")
    if offset and offset != "Z":
        offset_hour = int(offset[1:3])
        offset_minute = int(offset[4:6])
        if offset_hour > 23 or offset_minute > 59:
            return None
    try:
        timestamp_date = _iso_date(match.group("date"), field="native session_date")
        fraction = (match.group("fraction") or "")[:6]
        timestamp = (
            f"{timestamp_date}T{match.group('hour')}:{match.group('minute')}:{match.group('second')}"
            f"{'.' + fraction if fraction else ''}"
            f"{'+00:00' if offset == 'Z' else offset or ''}"
        )
        return datetime.fromisoformat(timestamp).date().isoformat()
    except ValueError:
        return None


def _atomic_csv(path: Path, headers: tuple[str, ...], rows: Iterable[Iterable[Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
    temporary.replace(path)


def project_development_sample(
    generation: str | Path,
    *,
    calendar_csv: str | Path,
    calendar_provenance: dict[str, Any],
    identity_binding: dict[str, Any],
    evidence_root: str | Path,
    member_column: str,
    output_dir: str | Path,
) -> dict[str, Path | None]:
    """Verify retained inputs and make partial, development-only V3 projections."""
    generation_dir = Path(generation).resolve()
    output = Path(output_dir)
    if output.exists():
        if not output.is_dir() or next(output.iterdir(), None) is not None:
            raise ValueError("output_dir must be a new or empty directory; refusing stale projection files")
    else:
        output.mkdir(parents=True, exist_ok=False)
    report_path = output / "native_quality_report.json"
    result: dict[str, Path | None] = {
        "quality_report_path": report_path,
        "prices_v3_path": None,
        "membership_v3_path": None,
    }
    report: dict[str, Any] = {
        "schema": "norgate_native_quality_report/v1",
        "projection_status": "blocked",
        "production_admission": False,
        "source_classes": [],
        "blockers": [],
        "identity": {"status": "unresolved_source_backed_mapping", "unresolved_dates": []},
        "basis": {
            "setting": None,
            "status": "unknown",
            "limitation": "CAPITAL adjustment is not established as split-only or as execution price basis; values are retained unchanged.",
        },
        "missingness": {"price_session_dates": [], "member_without_price_sessions": {}},
        "membership_by_universe": {},
        "native_frames": {},
        "evaluator_readiness": {"status": "NOT_READY", "missing_inputs": EVALUATOR_MISSING_INPUTS},
    }
    blockers: list[str] = report["blockers"]

    manifest_path = generation_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        blockers.append("generation_manifest_invalid")
        manifest = {}
    capture_valid = manifest.get("capture_status") == "completed"
    if not capture_valid:
        blockers.append("native_capture_incomplete")
    if manifest.get("production_admission") is not False:
        capture_valid = False
        blockers.append("production_admission_not_explicitly_denied")
    source_classes = {str(manifest.get("source_class", "unknown"))}
    native_frames = manifest.get("native_frames", {})
    price_rows, price_file, price_error = _read_native_frame(generation_dir, native_frames.get("prices")) if isinstance(native_frames, dict) else ([], None, "native_frame_record_missing")
    if price_error:
        blockers.append(price_error)
    price_record = native_frames.get("prices", {}) if isinstance(native_frames, dict) else {}
    report["native_frames"]["prices"] = {
        "rows": len(price_rows),
        "sha256": price_record.get("sha256") if isinstance(price_record, dict) else None,
        "status": "verified" if not price_error else price_error,
    }
    for index_name in INDEXES:
        record = native_frames.get("membership", {}).get(index_name) if isinstance(native_frames, dict) and isinstance(native_frames.get("membership"), dict) else None
        rows, path, error = _read_native_frame(generation_dir, record)
        if error:
            blockers.append(f"{error}:{index_name}")
        report["native_frames"][f"membership:{index_name}"] = {
            "rows": len(rows),
            "sha256": record.get("sha256") if isinstance(record, dict) else None,
            "status": "verified" if not error else error,
        }
        if isinstance(rows, list):
            source_rows_key = f"membership:{index_name}"
            report.setdefault("_native_membership_rows", {})[source_rows_key] = rows

    query = manifest.get("query", {}) if isinstance(manifest.get("query"), dict) else {}
    evidence = Path(evidence_root).resolve()
    stored_context = manifest.get("acquisition_context", {})
    metadata: dict[str, Any] | None = None
    metadata_bytes: bytes | None = None
    if isinstance(stored_context, dict) and stored_context.get("metadata_path") == "acquisition_metadata.json":
        metadata_path = generation_dir / "acquisition_metadata.json"
        try:
            metadata_bytes = metadata_path.read_bytes()
            if hashlib.sha256(metadata_bytes).hexdigest() == stored_context.get("metadata_sha256"):
                loaded_metadata = json.loads(metadata_bytes.decode("utf-8"))
                if isinstance(loaded_metadata, dict):
                    metadata = loaded_metadata
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            metadata = None
    acquisition_context = _assess_acquisition_context(
        metadata,
        metadata_bytes,
        evidence,
        start_date=str(query.get("start_date", "")),
        end_date=str(query.get("end_date", "")),
    )
    if not isinstance(stored_context, dict) or stored_context.get("status") != "hash_bound_context_complete":
        acquisition_context["unresolved_fields"] = sorted(
            set(acquisition_context.get("unresolved_fields", [])) | {"capture_context_not_complete"}
        )
        acquisition_context["status"] = "unverified_incomplete_context"
    if metadata is None:
        acquisition_context["status"] = "unverified_incomplete_context"
    acquisition_valid = acquisition_context.get("status") == "hash_bound_context_complete"
    report["acquisition_context"] = acquisition_context
    if acquisition_context.get("source_class") and acquisition_context["source_class"] != "unknown":
        source_classes.add(str(acquisition_context["source_class"]))
    if not acquisition_valid:
        blockers.append("acquisition_context_unverified")

    basis = query.get("adjustment_setting")
    report["basis"]["setting"] = basis
    if basis == "CAPITAL":
        report["basis"]["status"] = "known_but_unsupported_by_v3_schema"
        report["basis"]["v3_price_output_allowed"] = False
        report["basis"]["limitation"] = (
            "CAPITAL adjustment is not established as split-only or execution basis, and generic V3 prices have no basis field. "
            "No equivalent basis is inferred; native prices remain available."
        )
        blockers.append("v3_price_basis_not_supported")
    else:
        report["basis"]["v3_price_output_allowed"] = False
        report["basis"]["limitation"] = (
            "The native price adjustment setting is unknown; values are not converted or relabeled."
        )
        blockers.append("unknown_price_basis")

    cal_path = Path(calendar_csv).resolve()
    try:
        cal_path.relative_to(cal_path.anchor)
        calendar_hash_ok = cal_path.is_file() and not cal_path.is_symlink() and _sha256(cal_path) == calendar_provenance.get("calendar_csv_sha256")
    except (OSError, ValueError):
        calendar_hash_ok = False
    cal_evidence_ok = _verify_evidence(evidence, calendar_provenance.get("source_evidence_path"), calendar_provenance.get("source_evidence_sha256"))
    calendar_source_class = calendar_provenance.get("source_class")
    calendar_review_status = calendar_provenance.get("coverage_review_status")
    calendar_review_accepted = (
        calendar_review_status == "synthetic_fixture_only"
        if calendar_source_class == "synthetic_test_fixture"
        else calendar_review_status == "accepted"
    )
    calendar_valid = (
        calendar_provenance.get("schema") == "independent_calendar_provenance/v1"
        and calendar_source_class
        and calendar_provenance.get("calendar_id")
        and calendar_provenance.get("source_locator")
        and calendar_provenance.get("market_id")
        and calendar_provenance.get("window_start_date") == query.get("start_date")
        and calendar_provenance.get("window_end_date") == query.get("end_date")
        and calendar_provenance.get("full_session_coverage") is True
        and calendar_provenance.get("coverage_status") == "full_market_sessions"
        and calendar_review_accepted
        and calendar_hash_ok
        and cal_evidence_ok
    )
    if calendar_valid:
        source_classes.add(str(calendar_provenance["source_class"]))
        try:
            calendar_dates = _read_calendar(cal_path)
        except (OSError, ValueError, csv.Error):
            calendar_dates = []
            calendar_valid = False
    else:
        calendar_dates = []
    expected_calendar_dates = calendar_provenance.get("declared_session_dates")
    declared_count = calendar_provenance.get("declared_session_count")
    if calendar_valid and (
        type(declared_count) is not int
        or declared_count != len(calendar_dates)
        or not isinstance(expected_calendar_dates, list)
        or expected_calendar_dates != calendar_dates
    ):
        calendar_valid = False
    if calendar_valid and any(
        session < str(query.get("start_date", "")) or session > str(query.get("end_date", ""))
        for session in calendar_dates
    ):
        calendar_valid = False
    report["calendar"] = {
        "status": (
            "synthetic_fixture_calendar"
            if calendar_valid and calendar_source_class == "synthetic_test_fixture"
            else "reviewed_source_calendar_claim"
            if calendar_valid
            else "unresolved_independent_calendar"
        ),
        "calendar_id": calendar_provenance.get("calendar_id"),
        "session_dates": calendar_dates,
        "market_id": calendar_provenance.get("market_id"),
        "window_start_date": calendar_provenance.get("window_start_date"),
        "window_end_date": calendar_provenance.get("window_end_date"),
        "full_session_coverage": calendar_provenance.get("full_session_coverage"),
        "coverage_review_status": calendar_review_status,
        "declared_session_count": declared_count,
        "source_evidence_hash_verified": bool(cal_evidence_ok),
        "source_evidence_sha256": calendar_provenance.get("source_evidence_sha256"),
    }
    if not calendar_valid:
        blockers.append("independent_calendar_unverified")
        calendar_dates = []
        report["calendar"]["session_dates"] = []

    resolution = manifest.get("identity_resolution", {})
    provider_asset_id = resolution.get("requested_symbol_asset_id") if isinstance(resolution, dict) else None
    resolved_symbol = resolution.get("resolved_symbol") if isinstance(resolution, dict) else None
    identity_rows, identity_errors = _identity_rows(identity_binding, evidence, provider_asset_id, resolved_symbol)
    if isinstance(resolution, dict) and not resolution.get("roundtrip_matches", False):
        identity_errors.append("provider_asset_id_roundtrip_mismatch")
    if identity_binding.get("source_class"):
        source_classes.add(str(identity_binding["source_class"]))
    if identity_errors:
        blockers.extend(identity_errors)
    if not identity_rows:
        blockers.append("identity_source_mapping_unavailable")
    identity_source_valid = bool(identity_rows) and not identity_errors

    price_dates: list[str] = []
    price_projection_rows: list[list[Any]] = []
    unresolved_price_dates: list[str] = []
    price_columns = {name.lower(): name for name in (price_rows[0].keys() if price_rows else [])}
    required = {name: price_columns.get(name) for name in ("open", "high", "low", "close", "volume")}
    if any(value is None for value in required.values()):
        blockers.append("native_price_columns_missing")
    allowed_dates = set(calendar_dates)
    invalid_price_values = False
    invalid_price_session_values: list[str] = []
    seen_price_keys: set[tuple[str, str]] = set()
    for row in price_rows:
        session = _frame_date(row)
        if session is None:
            invalid_price_session_values.append(row.get("session_date", ""))
            blockers.append("native_price_session_date_invalid")
            continue
        if session not in allowed_dates or not (query.get("start_date", "") <= session <= query.get("end_date", "")):
            blockers.append("native_price_date_outside_independent_sample_calendar")
            continue
        price_dates.append(session)
        identity = _identity_for_day(identity_rows, session) if identity_source_valid else None
        if identity is None:
            unresolved_price_dates.append(session)
            continue
        ticker = identity["ticker"]
        values = [row.get(required[field]) for field in ("open", "high", "low", "close", "volume")]
        try:
            numeric = [float(value) for value in values]
            valid_values = all(math.isfinite(value) for value in numeric)
            valid_values = valid_values and all(value > 0 for value in numeric[:4]) and numeric[4] >= 0
            valid_values = valid_values and numeric[1] >= max(numeric[0], numeric[3])
            valid_values = valid_values and numeric[2] <= min(numeric[0], numeric[3])
        except (TypeError, ValueError):
            valid_values = False
        key = (session, ticker)
        if not valid_values or key in seen_price_keys:
            invalid_price_values = True
            continue
        seen_price_keys.add(key)
        price_projection_rows.append([
            session,
            ticker,
            *values,
        ])
    unresolved_price_dates = sorted(set(unresolved_price_dates))
    if identity_source_valid and not unresolved_price_dates:
        identity_status = (
            "synthetic_fixture_identity_assertion"
            if identity_binding.get("source_class") == "synthetic_test_fixture"
            else "reviewed_source_identity_assertion"
        )
    else:
        identity_status = "unresolved_source_identity_assertion"
    report["identity"] = {
        "status": identity_status,
        "source_evidence_hashes_verified": identity_source_valid,
        "assertion_review_status": identity_binding.get("assertion_review_status"),
        "assertion": identity_rows[0] if identity_rows else None,
        "unresolved_dates": unresolved_price_dates,
    }
    if unresolved_price_dates:
        blockers.append("identity_mapping_gap_on_price_dates")
    if invalid_price_values:
        blockers.append("native_price_values_invalid_or_duplicate")
    report["price_date_validation"] = {
        "recognized_session_dates": sorted(set(price_dates)),
        "invalid_values": sorted(set(invalid_price_session_values)),
    }
    if invalid_price_session_values:
        blockers.append("native_price_session_date_invalid")

    calendar_dates_set = set(calendar_dates)
    report["missingness"]["price_session_dates"] = sorted(calendar_dates_set - set(price_dates)) if calendar_valid else []

    membership_events: list[list[Any]] = []
    membership_frames_valid = all(
        report["native_frames"].get(f"membership:{index}", {}).get("status") == "verified"
        for index in INDEXES
    )
    membership_complete = bool(
        capture_valid and acquisition_valid and calendar_valid and identity_source_valid and member_column and membership_frames_valid
    )
    price_date_set = set(price_dates)
    member_without_price: dict[str, list[str]] = {}
    membership_rows_by_index: dict[str, list[dict[str, str]]] = report.pop("_native_membership_rows", {})
    price_seed_dates = sorted({session for row in price_rows if (session := _frame_date(row)) is not None})
    report["membership_capture_semantics"] = {
        "seeded_from_price_frame": query.get("membership_seed") == "returned prices frame copy",
        "seed_session_dates": price_seed_dates,
        "independent_calendar_retrieval": False,
        "calendar_is_used_to_mark_unobserved_membership_as_unknown": True,
        "offline_fixture_annotation": manifest.get("offline_fixture_annotation"),
    }
    for index_name in INDEXES:
        universe = INDEX_TO_UNIVERSE[index_name]
        rows = membership_rows_by_index.get(f"membership:{index_name}", [])
        observations: dict[str, int] = {}
        invalid_dates: list[str] = []
        for row in rows:
            session = _frame_date(row)
            if session is None:
                invalid_dates.append(row.get("session_date", ""))
                continue
            if session not in calendar_dates_set:
                invalid_dates.append(session)
                continue
            if member_column not in row:
                invalid_dates.append(session)
                continue
            value = _member_value(row[member_column])
            if value is None or session in observations:
                invalid_dates.append(session)
                continue
            observations[session] = value
        unknown_dates = sorted(calendar_dates_set - set(observations))
        if invalid_dates:
            membership_complete = False
            blockers.append(f"membership_observations_invalid:{index_name}")
        if unknown_dates:
            membership_complete = False
        if not member_column:
            unknown_dates = list(calendar_dates)
            membership_complete = False
        identity_gap_dates: list[str] = []
        if not capture_valid:
            state_status = "blocked_invalid_capture_generation"
        elif not acquisition_valid:
            state_status = "blocked_acquisition_context_unverified"
        elif report["native_frames"].get(f"membership:{index_name}", {}).get("status") != "verified":
            state_status = "blocked_native_membership_frame_invalid"
        elif not calendar_valid:
            state_status = "blocked_unknown_calendar"
        elif not identity_source_valid:
            state_status = "blocked_unresolved_identity"
        elif invalid_dates:
            state_status = "blocked_invalid_native_membership_observations"
        elif unknown_dates:
            state_status = "blocked_unknown_calendar_sessions"
        else:
            state_status = "complete_observed_calendar"
        report["membership_by_universe"][universe] = {
            "status": state_status,
            "observed_session_dates": sorted(observations),
            "unknown_session_dates": unknown_dates,
            "invalid_observation_dates": sorted(set(invalid_dates)),
            "unresolved_identity_session_dates": [],
        }
        true_dates: list[str] = []
        if not unknown_dates:
            prior: dict[str, int] = {}
            for session in calendar_dates:
                identity = _identity_for_day(identity_rows, session)
                if identity is None:
                    identity_gap_dates.append(session)
                    membership_complete = False
                    continue
                lineage = identity["lineage"]
                state = observations[session]
                old_state = prior.get(lineage, 0)
                if state != old_state:
                    membership_events.append([session, lineage, universe, state])
                prior[lineage] = state
                if state:
                    true_dates.append(session)
            if identity_gap_dates:
                blockers.append(f"membership_identity_mapping_gap:{index_name}")
                report["membership_by_universe"][universe]["status"] = "blocked_unresolved_identity"
                report["membership_by_universe"][universe]["unresolved_identity_session_dates"] = sorted(identity_gap_dates)
            elif unknown_dates:
                report["membership_by_universe"][universe]["status"] = "blocked_unknown_calendar_sessions"
                report["membership_by_universe"][universe]["unknown_session_dates"] = sorted(set(unknown_dates))
        if true_dates:
            without_price = sorted(set(true_dates) - price_date_set)
            if without_price:
                member_without_price[universe] = without_price
    report["missingness"]["member_without_price_sessions"] = member_without_price

    # A basis label and all identity/calendar evidence must be verified before prices are emitted.
    core_blockers = {
        "generation_manifest_invalid",
        "native_capture_incomplete",
        "production_admission_not_explicitly_denied",
        "native_frame_record_missing",
        "native_frame_path_invalid",
        "native_frame_missing",
        "native_frame_hash_mismatch",
        "native_frame_row_count_mismatch",
        "native_frame_unreadable",
        "unknown_price_basis",
        "v3_price_basis_not_supported",
        "independent_calendar_unverified",
        "identity_binding_schema_invalid",
        "identity_binding_empty",
        "identity_binding_dates_invalid",
        "identity_binding_asset_or_interval_invalid",
        "identity_binding_fields_missing",
        "identity_source_evidence_invalid",
        "identity_source_mapping_unavailable",
        "identity_mapping_gap_on_price_dates",
        "native_price_columns_missing",
        "native_price_values_invalid_or_duplicate",
        "native_price_date_outside_independent_sample_calendar",
        "native_price_session_date_invalid",
        "independent_calendar_outside_query_window",
        "duplicate_native_price_session",
        "provider_asset_id_roundtrip_mismatch",
        "acquisition_context_unverified",
    }
    # Index-frame problems affect membership only; price output needs the retained price frame.
    price_core_blocked = any(
        blocker in core_blockers or blocker == "native_frame_hash_mismatch" or blocker == "native_frame_row_count_mismatch"
        for blocker in blockers
    )
    if not price_core_blocked and price_projection_rows:
        prices_path = output / "prices_v3.csv"
        price_projection_rows.sort(key=lambda row: (row[0], row[1]))
        _atomic_csv(prices_path, PRICE_COLUMNS_V3, price_projection_rows)
        result["prices_v3_path"] = prices_path
    elif not price_projection_rows and not any(blocker in blockers for blocker in core_blockers):
        blockers.append("no_projectable_native_price_rows")

    if membership_complete and membership_events:
        membership_path = output / "membership_v3.csv"
        membership_events.sort(key=lambda row: (row[0], row[1], row[2]))
        _atomic_csv(membership_path, MEMBERSHIP_COLUMNS_V3, membership_events)
        result["membership_v3_path"] = membership_path
    elif membership_complete and not membership_events:
        blockers.append("membership_has_no_state_change_events")
    if result["prices_v3_path"] is not None and result["membership_v3_path"] is not None:
        report["projection_status"] = "projected_development_sample"
    elif result["prices_v3_path"] is not None or result["membership_v3_path"] is not None:
        report["projection_status"] = "partial_development_projection"
    else:
        report["projection_status"] = "blocked"
    report["source_classes"] = sorted(source_classes)
    report["blockers"] = sorted(set(blockers))
    report["native_row_counts"] = {
        "prices": len(price_rows),
        "membership": {
            index: len(membership_rows_by_index.get(f"membership:{index}", [])) for index in INDEXES
        },
    }
    report["capture_generation_id"] = manifest.get("generation_id")
    report["window"] = {"start_date": query.get("start_date"), "end_date": query.get("end_date")}
    _write_json(report_path, report)
    return result


def _load_sdk() -> tuple[Any, str]:
    # Deliberately isolated behind an explicit execution flag.
    import importlib.metadata
    import norgatedata

    try:
        version = importlib.metadata.version("norgatedata")
    except importlib.metadata.PackageNotFoundError:
        version = str(getattr(norgatedata, "__version__", "unknown"))
    return norgatedata, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="retain the bounded MSFT native sample")
    capture_parser.add_argument("--start-date", required=True)
    capture_parser.add_argument("--end-date", required=True)
    capture_parser.add_argument("--private-root", required=True)
    capture_parser.add_argument("--acquisition-metadata", help="finite setup/update context JSON input")
    capture_parser.add_argument("--acquisition-evidence-root", help="directory containing metadata evidence and updater executable")
    capture_parser.add_argument("--execute-read-only", action="store_true", help="explicitly execute the eight read-only SDK calls")
    project_parser = subparsers.add_parser("project", help="offline projection of a retained generation")
    project_parser.add_argument("--generation", required=True)
    project_parser.add_argument("--calendar-csv", required=True)
    project_parser.add_argument("--calendar-provenance", required=True)
    project_parser.add_argument("--identity-binding", required=True)
    project_parser.add_argument("--evidence-root", required=True)
    project_parser.add_argument("--member-column", required=True)
    project_parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    if args.command == "project":
        try:
            calendar_provenance = json.loads(Path(args.calendar_provenance).read_text(encoding="utf-8"))
            identity_binding = json.loads(Path(args.identity_binding).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read projection JSON input: {exc}")
        if not isinstance(calendar_provenance, dict) or not isinstance(identity_binding, dict):
            parser.error("calendar provenance and identity binding files must contain JSON objects")
        result = project_development_sample(
            args.generation,
            calendar_csv=args.calendar_csv,
            calendar_provenance=calendar_provenance,
            identity_binding=identity_binding,
            evidence_root=args.evidence_root,
            member_column=args.member_column,
            output_dir=args.output_dir,
        )
        report = json.loads(result["quality_report_path"].read_text(encoding="utf-8"))
        print(f"Offline projection status: {report['projection_status']}")
        print(f"Native quality report: {result['quality_report_path']}")
        print("Evaluator readiness: NOT_READY")
        return 0

    start, end = _validate_window(args.start_date, args.end_date)
    if not args.execute_read_only:
        print("DRY RUN: no Norgate SDK import or calls will be made.")
        print(f"Planned fixed query: MSFT {start} through {end}; CAPITAL adjustment; NONE padding; {EXPECTED_READS} SDK reads.")
        return 0
    private_root = Path(args.private_root).expanduser().resolve()
    repository_root = Path(__file__).resolve().parent
    try:
        private_root.relative_to(repository_root)
    except ValueError:
        pass
    else:
        parser.error("--private-root must be outside the repository to keep native rows private")
    acquisition_metadata = None
    acquisition_metadata_bytes = None
    acquisition_evidence_root = None
    if args.acquisition_metadata:
        metadata_path = Path(args.acquisition_metadata).expanduser().resolve()
        try:
            acquisition_metadata_bytes = metadata_path.read_bytes()
            acquisition_metadata = json.loads(acquisition_metadata_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read acquisition metadata before SDK execution: {exc}")
        if not isinstance(acquisition_metadata, dict):
            parser.error("acquisition metadata must contain a JSON object")
        acquisition_evidence_root = args.acquisition_evidence_root or metadata_path.parent
    provider, sdk_version = _load_sdk()
    try:
        generation = capture_native_generation(
            provider,
            start_date=start,
            end_date=end,
            private_root=private_root,
            sdk_version=sdk_version,
            acquisition_metadata=acquisition_metadata,
            acquisition_metadata_bytes=acquisition_metadata_bytes,
            acquisition_evidence_root=acquisition_evidence_root,
        )
    except CaptureFailure as exc:
        print(f"Capture failed; partial native generation retained at {exc.generation_dir}: {exc}", file=sys.stderr)
        return 2
    print(f"Native sample retained at {generation}")
    print("This bounded native capture does not establish production admission or evaluator readiness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
