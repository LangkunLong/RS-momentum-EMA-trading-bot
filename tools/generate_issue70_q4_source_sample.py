"""Create a bounded, no-clobber Q4 sample from retained SEC bulk sources.

This tool does not download SEC data and does not hash either large ZIP. It
reuses the repository's retained SEC archive attestations after checking their
byte lengths and ZIP directory metadata. Only the four requested CIK members
and their referenced submissions fragments are decompressed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from core import sec_pit_fundamentals as sec
from fetch_sec_pit_fundamentals import (
    _audit_values,
    _fundamental_values,
    _json_bytes,
    _write_csv,
)
from tools.issue70_v9_coverage import (
    build_v8_source_window_comparison,
    build_v9_coverage_records,
    serialize_v9_coverage_records,
)


TICKERS = ("A", "AMZN", "MSFT", "KDP")
START_DATE = date(2020, 1, 1)
END_DATE = date(2025, 12, 31)
V9_HISTORY_START = date(2010, 1, 1)
V9_EVALUATION_START = date(2020, 1, 1)
V9_EVALUATION_END = date(2025, 12, 31)
V9_CALENDAR_SHA256 = "f658bbfff04b623a3e20afa0ed2909e15e33470da4ef8305bd811f811a3c8cb5"
V9_CALENDAR_PROVENANCE_SHA256 = "666e76d9a926b7c0dca602461cfb218c19a77f44a1b2657a8e5f8a93e54221b7"
V9_CALENDAR_MARKER_SHA256 = "8143e95025914ad40d2eba1de669128adb69926cc94152b92d0b7790f7966bbf"
V9_CALENDAR_DECISION_SHA256 = "5e50cfa5a8184c2646cfbdfa44f9487ff87497e53a459fee8e2c15a07790243e"
V9_CALENDAR_REVIEW_SHA256 = "6d1060e23a005030aeec3b0f5243a9e476f1a28de418797af3cc91b319afe6ee"
V9_REFERENCE_CALENDAR_SHA256 = "93d8ef415bd6be516fb32ebfa5986ad45cbc2077e5beaa9615943db8890be5b9"
V9_REVIEWED_COMMIT = "52b7c35d465bb0874161c751dccc590445695fee"
V9_REVIEWED_TREE = "11409278dd40f48083ea6324352dc7fb264c8157"
V9_WHEEL_SHA256 = "fc5a2ad0d61b5c3a6539a3061cd4cbb55c59f4a903455cec7926e4b798919996"
V9_V8_SOURCE_MANIFEST_SHA256 = "c5ecb3a97639861d6c79e5ab18aed49798b38eb4c9dcd9206f6bdabe08d3073f"
V9_EXPECTED_CIKS = {
    "A": "0001090872",
    "AMZN": "0001018724",
    "KDP": "0001418135",
    "MSFT": "0000789019",
}
V9_EXPECTED_CALENDAR_SESSIONS = 4024
V9_EXPECTED_RETAINED_OVERLAP = 1508
MAX_SELECTED_MEMBER_BYTES = 64 * 1024 * 1024
MAX_SELECTED_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_LEGACY_FUNDAMENTALS_BYTES = 11_000_000
MAX_ALTERNATE_AUDIT_BYTES = 148_000_000
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_RUNTIME_SECONDS = 10 * 60
MAX_FINANCIAL_AUDIT_ROWS = 10_000
MAX_ISSUER_SESSION_GRID = 6_032
MAX_COVERAGE_RECORDS = 250_000
MAX_COVERAGE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
CIK_MEMBER = re.compile(r"(?:^|/)CIK(?P<cik>\d{10})\.json$")


def _json_file(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(sec._regular_file(path, path.name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return value


def _archive_file_metadata(path: Path) -> Mapping[str, int]:
    info = sec._regular_file(path, path.name).stat()
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "byte_length": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _zip_info_signature(info: zipfile.ZipInfo) -> Mapping[str, int]:
    return {
        "expanded_bytes": int(info.file_size),
        "compressed_bytes": int(info.compress_size),
        "crc32": int(info.CRC),
    }


def _zip_directory_metadata(path: Path, selected_names: Sequence[str]) -> Mapping[str, Any]:
    with zipfile.ZipFile(sec._regular_file(path, path.name), "r") as archive:
        infos = archive.infolist()
        by_name = {info.filename: info for info in infos}
        missing = sorted(set(selected_names) - set(by_name))
        if missing:
            raise ValueError(f"selected ZIP members disappeared: {missing[:3]}")
        return {
            "zip_entry_count": len(infos),
            "zip_uncompressed_bytes": sum(info.file_size for info in infos),
            "selected_member_signatures": {
                name: dict(_zip_info_signature(by_name[name])) for name in sorted(set(selected_names))
            },
        }


def _digest(path: Path) -> str:
    return sec.sha256_file(path)


def _validate_bound_file(path: Path, expected_sha256: str, *, expected_bytes: int | None = None) -> str:
    info = sec._regular_file(path, path.name).stat()
    if expected_bytes is not None and info.st_size != expected_bytes:
        raise ValueError(f"retained input size differs from its receipt: {path.name}")
    digest = _digest(path)
    if digest != expected_sha256:
        raise ValueError(f"retained input digest differs from its receipt: {path.name}")
    return digest


def _read_bound_json(path: Path, expected_sha256: str) -> tuple[str, Mapping[str, Any]]:
    """Parse the exact small JSON bytes whose digest is part of the v9 contract."""
    try:
        checked_path = sec._regular_file(path, path.name)
        if checked_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError(f"bound JSON input exceeds the 4 MiB manifest cap: {path.name}")
        raw = checked_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"bound JSON input is unavailable: {path.name}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"retained input digest differs from its receipt: {path.name}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return digest, value


def _validate_v8_selected_member_hashes(
    manifest: Mapping[str, Any], actual_selected: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Mapping[str, Any]:
    """Bind measured `(archive, member, raw SHA, expanded bytes)` tuples to v8."""
    source_budgets = manifest.get("source_budgets")
    expected_selected = (
        source_budgets.get("selected_member_hashes")
        if isinstance(source_budgets, Mapping)
        else None
    )
    if not isinstance(expected_selected, Mapping):
        raise ValueError("hash-bound v8 source manifest lacks selected SEC member identities")
    if set(expected_selected) != {"submissions", "companyfacts"}:
        raise ValueError("hash-bound v8 source manifest has an unexpected selected-member group")
    if set(actual_selected) != {"submissions", "companyfacts"}:
        raise ValueError("measured SEC member identities have an unexpected group")

    matched_counts: dict[str, int] = {}
    expected_identities: dict[tuple[str, str], tuple[str, int]] = {}
    actual_identities: dict[tuple[str, str], tuple[str, int]] = {}

    def add_rows(
        target: dict[tuple[str, str], tuple[str, int]], rows: Sequence[Any], *,
        archive: str, source: str,
    ) -> None:
        member_pattern = (
            re.compile(r"^CIK\d{10}\.json$")
            if archive == "companyfacts"
            else re.compile(r"^CIK\d{10}(?:-submissions-\d{3})?\.json$")
        )
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"{source} selected SEC member record is malformed: {archive}")
            name = row.get("member_name")
            digest = row.get("sha256")
            expanded_bytes = row.get("expanded_bytes")
            if (
                not isinstance(name, str)
                or not member_pattern.fullmatch(name)
                or "\\" in name
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(expanded_bytes, int)
                or isinstance(expanded_bytes, bool)
                or expanded_bytes <= 0
            ):
                raise ValueError(f"{source} selected SEC member identity is malformed: {archive}")
            key = (archive, name)
            if key in target:
                raise ValueError(f"{source} selected SEC member identity is duplicated: {archive}/{name}")
            target[key] = (digest, expanded_bytes)

    for group in ("submissions", "companyfacts"):
        expected_rows = expected_selected[group]
        actual_rows = actual_selected[group]
        if not isinstance(expected_rows, list) or not isinstance(actual_rows, Sequence):
            raise ValueError(f"selected SEC member identities are invalid: {group}")
        add_rows(expected_identities, expected_rows, archive=group, source="v8 manifest")
        add_rows(actual_identities, actual_rows, archive=group, source="measured")
        matched_counts[group] = len(expected_rows)
    if len(expected_identities) != 14:
        raise ValueError("hash-bound v8 source contract must contain exactly 14 selected SEC members")
    if set(expected_identities) != set(actual_identities):
        raise ValueError("measured archive/member namespaces differ from the hash-bound v8 manifest")
    for archive, name in sorted(expected_identities):
        if actual_identities[(archive, name)] != expected_identities[(archive, name)]:
            raise ValueError(
                "measured archive/member digest or expanded length differs from the "
                f"hash-bound v8 manifest: {archive}/{name}"
            )
    return {
        "status": "matched",
        "selected_member_count": len(expected_identities),
        "selected_members_by_archive": matched_counts,
        "compared_fields": ["archive", "member_name", "sha256", "expanded_bytes"],
        "selected_members": [
            {
                "archive": archive,
                "member_name": name,
                "sha256": actual_identities[(archive, name)][0],
                "expanded_bytes": actual_identities[(archive, name)][1],
            }
            for archive, name in sorted(actual_identities)
        ],
    }


def _validate_combined_v9_evidence_caps(
    coverage_artifact: Mapping[str, Any], financial_summary: Mapping[str, Any], *,
    max_records: int = MAX_COVERAGE_RECORDS,
    max_uncompressed_bytes: int = MAX_COVERAGE_UNCOMPRESSED_BYTES,
) -> Mapping[str, int]:
    comparison = financial_summary.get("v8_source_window_comparison")
    if not isinstance(comparison, Mapping):
        raise ValueError("complete v9 financial summary lacks its source-window comparison")
    by_slot_id = comparison.get("by_slot_id")
    if not isinstance(by_slot_id, Mapping) or int(comparison.get("expected_slot_count", -1)) != len(by_slot_id):
        raise ValueError("v9 comparison slot count differs from its complete by-slot structure")
    annual_gap_slots = financial_summary.get("annual_fiscal_year_gap_slots")
    if (
        not isinstance(annual_gap_slots, list)
        or int(financial_summary.get("annual_fiscal_year_gap_slot_count", -1)) != len(annual_gap_slots)
    ):
        raise ValueError("v9 annual-gap diagnostic count differs from its complete summary structure")

    origin_decision_count = 0
    for slot_id, slot_detail in by_slot_id.items():
        if not isinstance(slot_id, str) or not isinstance(slot_detail, Mapping):
            raise ValueError("v9 comparison contains a malformed slot detail")
        origin_evidence = slot_detail.get("origin_evidence")
        if not isinstance(origin_evidence, Mapping):
            raise ValueError("v9 comparison slot lacks linked origin evidence")
        decisions = origin_evidence.get("window_candidates_by_origin_id")
        if not isinstance(decisions, Mapping):
            raise ValueError("v9 comparison slot lacks its origin window decisions")
        origin_decision_count += len(decisions)

    comparison_entry_count = len(by_slot_id)
    annual_gap_count = len(annual_gap_slots)
    total_records = (
        int(coverage_artifact["record_count"])
        + comparison_entry_count
        + origin_decision_count
        + annual_gap_count
    )
    if total_records > max_records:
        raise ValueError(
            "combined v9 coverage, comparison-slot, origin-decision, and annual-gap evidence "
            f"exceeds the configured {max_records:,}-record cap"
        )
    summary_bytes = len(_json_bytes(financial_summary))
    total_uncompressed = (
        int(coverage_artifact["uncompressed_byte_length"])
        + summary_bytes
    )
    if total_uncompressed > max_uncompressed_bytes:
        raise ValueError(
            "combined v9 coverage CSV and complete financial summary exceed the configured "
            f"{max_uncompressed_bytes:,}-byte canonical evidence cap"
        )
    return {
        "coverage_csv_record_count": int(coverage_artifact["record_count"]),
        "comparison_slot_count": comparison_entry_count,
        "origin_window_decision_count": origin_decision_count,
        "annual_gap_diagnostic_count": annual_gap_count,
        "combined_record_count": total_records,
        "coverage_csv_uncompressed_bytes": int(coverage_artifact["uncompressed_byte_length"]),
        "financial_summary_bytes": summary_bytes,
        "combined_canonical_uncompressed_bytes": total_uncompressed,
    }


def _complete_v9_coverage_summary(
    coverage_summary: Mapping[str, Any], coverage_artifact: Mapping[str, Any],
    comparison: Mapping[str, Any], fundamentals_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **coverage_summary,
        "compressed_evidence": dict(coverage_artifact),
        "v8_source_window_comparison": comparison,
        "prehistory_and_no_next_session_omissions": {
            "pre_window_filing_fact_candidates": int(
                fundamentals_coverage.get("pre_window_filing_omissions", 0)
            ),
            "no_next_session_fact_candidates": int(
                fundamentals_coverage.get("no_next_session_fact_omissions", 0)
            ),
            "post_cutoff_or_no_next_session_fact_candidates": int(
                fundamentals_coverage.get("post_cutoff_fact_omissions", 0)
            ),
            "mapped_after_cutoff_fact_candidates": int(
                fundamentals_coverage.get("mapped_after_cutoff_fact_omissions", 0)
            ),
            "unique_cik_accession_filing_event_counts": "unmeasured_from_retained_candidate_counters",
            "distinct_cik_period_metric_vintage_counts": "unmeasured_from_retained_candidate_counters",
        },
    }


def _calendar_dates(path: Path, *, expected_header: tuple[str, ...]) -> tuple[date, ...]:
    values: list[date] = []
    with sec._regular_file(path, path.name).open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != expected_header:
            raise ValueError(f"calendar CSV header is invalid: {path.name}")
        for row in reader:
            try:
                values.append(date.fromisoformat(row[expected_header[0]]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"calendar CSV contains an invalid date: {path.name}") from exc
    if not values or values != sorted(set(values)):
        raise ValueError(f"calendar CSV dates must be nonempty, unique, and sorted: {path.name}")
    return tuple(values)


def _validate_v9_calendar_inputs(
    *,
    session_calendar_path: Path,
    adoption_decision_path: Path,
    calendar_provenance_path: Path,
    candidate_marker_path: Path,
    retained_calendar_path: Path,
) -> Mapping[str, Any]:
    """Bind the exact principal-adopted calendar before any SEC archive access."""
    decision_path = sec._regular_file(adoption_decision_path, "calendar adoption decision")
    decision_sha256 = _validate_bound_file(decision_path, V9_CALENDAR_DECISION_SHA256)
    decision = _json_file(decision_path)
    if (
        decision.get("decision") != "ADOPT_EXACT_WHEEL_BOUND_DERIVED_DAILY_XNYS_SESSIONS_INPUT"
        or decision.get("reviewed_commit") != V9_REVIEWED_COMMIT
        or decision.get("reviewed_tree") != V9_REVIEWED_TREE
        or decision.get("calendar_path") != "docs/issue-70-calendar-candidate-v1/exchange_sessions.csv"
        or decision.get("calendar_bytes") != 44_275
        or decision.get("calendar_sha256") != V9_CALENDAR_SHA256
        or decision.get("provenance_sha256") != V9_CALENDAR_PROVENANCE_SHA256
        or decision.get("rows") != V9_EXPECTED_CALENDAR_SESSIONS
        or decision.get("requested_range") != ["2010-01-01", "2025-12-31"]
        or decision.get("actual_session_bounds") != ["2010-01-04", "2025-12-31"]
        or decision.get("source_label") != "derived_exchange_schedule"
        or decision.get("not_observed_price_history") is not True
        or decision.get("reference_overlap")
        != {"dates": V9_EXPECTED_RETAINED_OVERLAP, "derived_only": 0, "retained_only": 0, "full_interval": "2020–2025"}
        or decision.get("boundary_checks") != 14
        or decision.get("report_sha256") != V9_CALENDAR_REVIEW_SHA256
        or decision.get("root_full_read_and_report_hash_verified") is not True
        or V9_WHEEL_SHA256 not in str(decision.get("package_identity", ""))
    ):
        raise ValueError("calendar adoption decision does not match the accepted exact-input record")

    calendar_path = sec._regular_file(session_calendar_path, "adopted session calendar")
    try:
        calendar_sha256 = _validate_bound_file(
            calendar_path, V9_CALENDAR_SHA256, expected_bytes=44_275
        )
    except ValueError as exc:
        raise ValueError(f"calendar CSV digest or byte length differs from the adopted input: {exc}") from exc
    provenance_path = sec._regular_file(calendar_provenance_path, "calendar provenance")
    provenance_sha256 = _validate_bound_file(
        provenance_path, V9_CALENDAR_PROVENANCE_SHA256
    )
    provenance = _json_file(provenance_path)
    if (
        provenance.get("schema_version") != 1
        or provenance.get("status") != "research_validation_passed"
        or provenance.get("origin") != "derived_exchange_schedule"
        or provenance.get("not_observed_price_history") is not True
        or provenance.get("calendar") != "XNYS"
        or provenance.get("requested_start") != "2010-01-01"
        or provenance.get("requested_end") != "2025-12-31"
        or provenance.get("first_session") != "2010-01-04"
        or provenance.get("last_session") != "2025-12-31"
        or provenance.get("rows") != V9_EXPECTED_CALENDAR_SESSIONS
        or provenance.get("calendar_sha256") != calendar_sha256
        or provenance.get("retained_reference_sha256") != V9_REFERENCE_CALENDAR_SHA256
        or provenance.get("retained_rows") != V9_EXPECTED_RETAINED_OVERLAP
        or provenance.get("overlap_differences") != {"derived_only": [], "retained_only": []}
    ):
        raise ValueError("calendar provenance does not match the adopted derived schedule")
    strict_checks = provenance.get("strict_next_session_checks")
    if (
        not isinstance(strict_checks, dict)
        or len(strict_checks) != 14
        or any(
            not isinstance(check, dict) or check.get("expected") != check.get("actual")
            for check in strict_checks.values()
        )
    ):
        raise ValueError("calendar provenance strict-next-session checks are incomplete")
    packages = provenance.get("packages")
    if not isinstance(packages, list) or not any(
        isinstance(package, dict)
        and package.get("name") == "exchange_calendars"
        and package.get("version") == "4.13.2"
        and package.get("sha256") == V9_WHEEL_SHA256
        for package in packages
    ):
        raise ValueError("calendar provenance does not bind the inspected exchange_calendars wheel")

    marker_path = sec._regular_file(candidate_marker_path, "historical calendar publication marker")
    marker_sha256 = _validate_bound_file(marker_path, V9_CALENDAR_MARKER_SHA256)
    marker = _json_file(marker_path)
    marker_files = marker.get("files")
    if (
        marker.get("status") != "candidate_pending_independent_adoption"
        or not isinstance(marker_files, dict)
        or marker_files.get("exchange_sessions.csv") != calendar_sha256
        or marker_files.get("calendar_provenance.json") != provenance_sha256
    ):
        raise ValueError("historical calendar candidate marker changed or lost its original status")

    sessions = _calendar_dates(calendar_path, expected_header=("trade_date",))
    if (
        len(sessions) != V9_EXPECTED_CALENDAR_SESSIONS
        or (sessions[0].isoformat(), sessions[-1].isoformat()) != ("2010-01-04", "2025-12-31")
    ):
        raise ValueError("adopted calendar session count or bounds differ from the adoption decision")
    retained_path = sec._regular_file(retained_calendar_path, "retained comparison session calendar")
    retained_sha256 = _validate_bound_file(retained_path, V9_REFERENCE_CALENDAR_SHA256)
    retained_sessions = _calendar_dates(retained_path, expected_header=("trade_date",))
    if len(retained_sessions) != V9_EXPECTED_RETAINED_OVERLAP:
        raise ValueError("retained comparison calendar row count differs from its receipt")
    derived_window = tuple(
        session for session in sessions
        if V9_EVALUATION_START <= session <= V9_EVALUATION_END
    )
    derived_only = sorted(set(derived_window) - set(retained_sessions))
    retained_only = sorted(set(retained_sessions) - set(derived_window))
    if derived_only or retained_only or len(derived_window) != V9_EXPECTED_RETAINED_OVERLAP:
        raise ValueError("adopted calendar does not match the full retained 2020–2025 session set")
    return {
        "sessions": sessions,
        "calendar_sha256": calendar_sha256,
        "calendar_provenance_sha256": provenance_sha256,
        "adoption_decision_sha256": decision_sha256,
        "candidate_marker_sha256": marker_sha256,
        "independent_review_sha256": V9_CALENDAR_REVIEW_SHA256,
        "reviewed_commit": V9_REVIEWED_COMMIT,
        "reviewed_tree": V9_REVIEWED_TREE,
        "source_label": "derived_exchange_schedule",
        "wheel_sha256": V9_WHEEL_SHA256,
        "retained_reference_sha256": retained_sha256,
        "retained_window_overlap": {
            "dates": len(derived_window),
            "derived_only": 0,
            "retained_only": 0,
        },
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("xb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def _source_revision(repo_root: Path) -> Mapping[str, str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_root, check=True,
            capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("unable to bind a clean source revision for v9 provenance") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or status.strip():
        raise ValueError("v9 financial generation requires a clean committed source revision")
    return {"commit": commit, "worktree_status": "clean"}


def _build_v9_coverage_records(
    *,
    fundamentals: sec.FundamentalExportResult,
    security_rows: Sequence[sec.SecurityMasterRow],
    evaluation_sessions: Sequence[date],
    calendar_sessions: Sequence[date] | None = None,
    max_records: int = MAX_COVERAGE_RECORDS,
    normalized_origins_out: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if calendar_sessions is None:
        calendar_path = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "issue-70-calendar-candidate-v1"
            / "exchange_sessions.csv"
        )
        calendar_sessions = _calendar_dates(
            calendar_path, expected_header=("trade_date",)
        )
    return build_v9_coverage_records(
        fundamentals=fundamentals,
        security_rows=security_rows,
        evaluation_sessions=evaluation_sessions,
        calendar_sessions=calendar_sessions,
        max_records=max_records,
        normalized_origins_out=normalized_origins_out,
    )


def _submission_workset(
    archive_path: Path,
    ciks: Sequence[str],
) -> Mapping[str, Any]:
    """Preflight all selected main/fragment members before the extractor reads them."""
    with zipfile.ZipFile(sec._regular_file(archive_path, "SEC submissions archive"), "r") as archive:
        infos = archive.infolist()
        by_name = {info.filename: info for info in infos}
        by_basename: dict[str, list[zipfile.ZipInfo]] = defaultdict(list)
        main_by_cik: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            by_basename[PurePosixPath(info.filename).name].append(info)
            match = CIK_MEMBER.search(info.filename)
            if match:
                cik = match.group("cik")
                if cik in main_by_cik:
                    raise ValueError(f"submissions ZIP repeats CIK main member {cik}")
                main_by_cik[cik] = info

        selected_members: dict[str, zipfile.ZipInfo] = {}
        archive_member_count = len(infos)
        archive_uncompressed_bytes = sum(info.file_size for info in infos)
        main_bytes = 0
        referenced_bytes = 0
        main_preflight_reads: dict[str, Mapping[str, Any]] = {}
        for cik in sorted(set(ciks)):
            main = main_by_cik.get(cik)
            if main is None:
                raise ValueError(f"submissions ZIP lacks the selected CIK {cik}")
            if main.file_size > MAX_SELECTED_MEMBER_BYTES:
                raise ValueError(f"selected submissions member exceeds 64 MiB: {main.filename}")
            selected_members[main.filename] = main
            main_bytes += main.file_size
            try:
                raw_payload = archive.read(main)
                main_preflight_reads[main.filename] = {
                    "expanded_bytes": len(raw_payload),
                    "sha256": hashlib.sha256(raw_payload).hexdigest(),
                }
                payload = json.loads(raw_payload)
            except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                raise ValueError(f"selected submissions member is invalid: {main.filename}") from exc
            filings = payload.get("filings", {}) if isinstance(payload, dict) else {}
            references = filings.get("files", []) if isinstance(filings, dict) else []
            if not isinstance(references, list):
                raise ValueError(f"selected submissions references are invalid: {main.filename}")
            for reference in references:
                if not isinstance(reference, dict) or not isinstance(reference.get("name"), str):
                    raise ValueError(f"selected submissions reference is malformed: {main.filename}")
                name = reference["name"].strip()
                info = by_name.get(name)
                if info is None:
                    matches = by_basename.get(PurePosixPath(name).name, [])
                    if len(matches) > 1:
                        raise ValueError(f"selected submission fragment is ambiguous: {name}")
                    info = matches[0] if matches else None
                if info is None:
                    continue
                if info.file_size > MAX_SELECTED_MEMBER_BYTES:
                    raise ValueError(f"selected submissions fragment exceeds 64 MiB: {info.filename}")
                selected_members[info.filename] = info
                referenced_bytes += info.file_size

        # The main records are read once here for the preflight and once by the
        # acceptance parser. Count both passes against the hard source budget.
        worst_case_bytes = 2 * main_bytes + referenced_bytes
        if worst_case_bytes > MAX_SELECTED_UNCOMPRESSED_BYTES:
            raise ValueError("selected submissions decompression exceeds 512 MiB")
        return {
            "zip_entry_count": archive_member_count,
            "zip_uncompressed_bytes": archive_uncompressed_bytes,
            "selected_main_members": len(set(ciks)),
            "selected_fragment_members": len(selected_members) - len(set(ciks)),
            "unique_selected_member_count": len(selected_members),
            "selected_member_bytes": sum(info.file_size for info in selected_members.values()),
            "worst_case_decompressed_bytes_including_preflight": worst_case_bytes,
            "selected_member_signatures": {
                name: dict(_zip_info_signature(info)) for name, info in sorted(selected_members.items())
            },
            "main_preflight_reads": {
                name: dict(read) for name, read in sorted(main_preflight_reads.items())
            },
            "max_member_bytes": MAX_SELECTED_MEMBER_BYTES,
            "max_total_bytes": MAX_SELECTED_UNCOMPRESSED_BYTES,
        }


def _companyfacts_workset(archive_path: Path, ciks: Sequence[str]) -> Mapping[str, Any]:
    with zipfile.ZipFile(sec._regular_file(archive_path, "SEC companyfacts archive"), "r") as archive:
        infos = archive.infolist()
        selected: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            match = CIK_MEMBER.search(info.filename)
            if match and match.group("cik") in ciks:
                if match.group("cik") in selected:
                    raise ValueError(f"companyfacts ZIP repeats selected CIK {match.group('cik')}")
                selected[match.group("cik")] = info
        per_member = {cik: info.file_size for cik, info in sorted(selected.items())}
        if any(size > MAX_SELECTED_MEMBER_BYTES for size in per_member.values()):
            raise ValueError("selected companyfacts member exceeds 64 MiB")
        total = sum(per_member.values())
        if total > MAX_SELECTED_UNCOMPRESSED_BYTES:
            raise ValueError("selected companyfacts decompression exceeds 512 MiB")
        return {
            "zip_entry_count": len(infos),
            "zip_uncompressed_bytes": sum(info.file_size for info in infos),
            "selected_cik_members": len(selected),
            "missing_ciks": sorted(set(ciks) - set(selected)),
            "selected_member_bytes": per_member,
            "total_selected_member_bytes": total,
            "selected_member_signatures": {
                name: dict(_zip_info_signature(info))
                for name, info in sorted(
                    (info.filename, info) for info in selected.values()
                )
            },
            "max_member_bytes": MAX_SELECTED_MEMBER_BYTES,
            "max_total_bytes": MAX_SELECTED_UNCOMPRESSED_BYTES,
        }


def _load_security_rows(path: Path) -> tuple[sec.SecurityMasterRow, ...]:
    rows: list[sec.SecurityMasterRow] = []
    with sec._regular_file(path, "retained security master").open(
        "r", encoding="utf-8", newline=""
    ) as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != sec.SECURITY_MASTER_COLUMNS:
            raise ValueError("retained security master header is invalid")
        for record in reader:
            if record["ticker"] not in TICKERS:
                continue
            rows.append(
                sec.SecurityMasterRow(
                    ticker=record["ticker"],
                    cik=record["cik"],
                    company_name=record["company_name"],
                    first_membership_date=date.fromisoformat(record["first_membership_date"]),
                    last_membership_date=date.fromisoformat(record["last_membership_date"]),
                    mapping_basis=record["mapping_basis"],
                )
            )
    if set(row.ticker for row in rows) != set(TICKERS):
        raise ValueError("retained security master does not resolve all four requested tickers")
    return tuple(sorted(rows, key=lambda row: (row.ticker, row.first_membership_date, row.cik)))


def _stream_csv(
    path: Path,
    *,
    max_bytes: int,
    selector: set[str] | None = None,
) -> tuple[list[dict[str, str]], str, int]:
    """Hash and parse a CSV in one bounded pass, retaining only selected tickers."""
    if path.stat().st_size > max_bytes:
        raise ValueError(f"CSV exceeds its source-stream budget: {path.name}")
    digest = hashlib.sha256()
    selected: list[dict[str, str]] = []
    row_count = 0

    def decoded_lines(stream: Any) -> Iterable[str]:
        first_line = True
        for raw_line in stream:
            digest.update(raw_line)
            encoding = "utf-8-sig" if first_line else "utf-8"
            first_line = False
            yield raw_line.decode(encoding)

    with sec._regular_file(path, path.name).open("rb") as stream:
        text_lines = decoded_lines(stream)
        reader = csv.DictReader(text_lines)
        if not reader.fieldnames:
            raise ValueError(f"CSV lacks a header: {path.name}")
        for row in reader:
            row_count += 1
            if selector is None or row.get("ticker") in selector:
                selected.append({key: value or "" for key, value in row.items() if key is not None})
    return selected, digest.hexdigest(), row_count


def _verify_archive_receipt(archive_dir: Path) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    receipt_path = archive_dir / "sec_archives_provenance.json"
    receipt = _json_file(receipt_path)
    if receipt.get("schema_version") != 1 or not isinstance(receipt.get("archives"), dict):
        raise ValueError("retained SEC archive attestation schema is invalid")
    expected_urls = {
        "submissions.zip": "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
        "companyfacts.zip": "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
    }
    archives: dict[str, Any] = {}
    for name, url in expected_urls.items():
        metadata = receipt["archives"].get(name)
        if not isinstance(metadata, dict) or metadata.get("url") != url:
            raise ValueError(f"retained SEC archive attestation URL is invalid for {name}")
        if metadata.get("max_json_member_bytes") != 512 * 1024 * 1024:
            raise ValueError(f"retained SEC archive attestation member bound is invalid for {name}")
        archive_path = sec._regular_file(archive_dir / name, name)
        if archive_path.stat().st_size != metadata.get("byte_length"):
            raise ValueError(f"retained SEC archive byte length differs from attestation: {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256", ""))):
            raise ValueError(f"retained SEC archive digest attestation is malformed: {name}")
        archives[name] = metadata
    return receipt, archives


def _scan_existing_generations(
    *,
    older_fundamentals: Path,
    older_provenance: Mapping[str, Any],
    alternate_audit: Path,
    alternate_provenance: Mapping[str, Any],
    alternate_marker: Mapping[str, Any],
) -> Mapping[str, Any]:
    old_rows, old_hash, old_row_count = _stream_csv(
        older_fundamentals,
        max_bytes=MAX_LEGACY_FUNDAMENTALS_BYTES,
        selector=set(TICKERS),
    )
    expected_old_hash = str(older_provenance.get("fundamentals_sha256", ""))
    if old_hash != expected_old_hash:
        raise ValueError("142329-row legacy fundamentals CSV differs from its provenance")
    if old_row_count != int(older_provenance.get("fundamental_row_count", -1)):
        raise ValueError("142329-row legacy CSV record count differs from its provenance")

    audit_size = sec._regular_file(alternate_audit, "145010-row audit CSV").stat().st_size
    if audit_size > MAX_ALTERNATE_AUDIT_BYTES:
        raise ValueError("145010-row audit CSV exceeds the 148 MB stream budget")
    audit_rows, audit_hash, audit_row_count = _stream_csv(
        alternate_audit,
        max_bytes=MAX_ALTERNATE_AUDIT_BYTES,
        selector=set(TICKERS),
    )
    expected_audit_hash = str(alternate_provenance.get("fundamentals_audit_sha256", ""))
    marker_audit_hash = str(
        (alternate_marker.get("files") or {}).get("fundamentals_audit.csv", "")
    )
    audit_verified = audit_hash == expected_audit_hash == marker_audit_hash
    if not audit_verified:
        raise ValueError("145010-row audit digest differs from its provenance or publication marker")
    if audit_row_count != int(alternate_provenance.get("fundamental_row_count", -1)):
        raise ValueError("145010-row audit record count differs from its provenance")

    old_q4: Counter[str] = Counter()
    annual_periods = {
        (row.get("ticker", ""), row.get("period_end", ""))
        for row in old_rows
        if row.get("statement_type") == "annual"
        and (row.get("period_end", "")[:4] in {"2020", "2021", "2022", "2023", "2024", "2025"})
    }
    for row in old_rows:
        if row.get("statement_type") == "quarterly" and row.get("period_end", "")[:4] in {
            "2020", "2021", "2022", "2023", "2024", "2025"
        } and (
            row.get("ticker", ""), row.get("period_end", "")
        ) in annual_periods:
            old_q4[row["ticker"]] += 1
    alternate_q4: Counter[str] = Counter()
    alternate_metric_attribution: Counter[str] = Counter()
    for row in audit_rows:
        if (
            row.get("statement_type") != "quarterly"
            or row.get("fiscal_period", "").upper() != "Q4"
            or row.get("filed_date", "") < "2020-01-01"
            or row.get("public_date", "") < START_DATE.isoformat()
            or row.get("public_date", "") > END_DATE.isoformat()
        ):
            continue
        alternate_q4[row["ticker"]] += 1
        try:
            metric_sources = json.loads(row.get("metric_sources", "{}"))
        except json.JSONDecodeError:
            metric_sources = {}
        if isinstance(metric_sources, dict):
            for source in metric_sources.values():
                if not isinstance(source, dict):
                    continue
                attribution = source.get("q4_attribution")
                if attribution:
                    alternate_metric_attribution[str(attribution)] += 1
                derivation = source.get("derivation")
                if derivation:
                    alternate_metric_attribution[str(derivation)] += 1

    return {
        "legacy_142329_generation": {
            "provenance_row_count": older_provenance.get("fundamental_row_count"),
            "scanned_fundamentals_row_count": old_row_count,
            "fundamentals_sha256_verified": old_hash == expected_old_hash,
            "selected_ticker_row_counts": dict(Counter(row["ticker"] for row in old_rows)),
            "late_quarter_slots_by_ticker": dict(old_q4),
            "audit_retained": False,
        },
        "alternate_145010_generation": {
            "provenance_row_count": alternate_provenance.get("fundamental_row_count"),
            "audit_record_count": audit_row_count,
            "audit_file_size_bytes": audit_size,
            "audit_sha256_verified_against_provenance_and_marker": audit_verified,
            "selected_ticker_audit_row_counts": dict(Counter(row["ticker"] for row in audit_rows)),
            "late_quarter_slots_by_ticker": dict(alternate_q4),
            "metric_q4_attribution_counts": dict(alternate_metric_attribution),
            "publication_marker_status": alternate_marker.get("status"),
            "identity_manifest_expected_sha256": alternate_provenance.get(
                "identity_manifest_csv_sha256"
            ),
            "identity_manifest_available_in_retained_alternate_generation": False,
        },
        "interpretation": (
            "The 142329 and 145010 outputs remain separate generations. The alternate audit "
            "is comparison evidence only; its identity manifest digest is not available as a "
            "retained input. This Q4 sample is regenerated from the 142329 security-master "
            "and identity lineage plus the shared attested SEC archives."
        ),
    }


def generate(
    *,
    archive_dir: Path,
    legacy_root: Path,
    alternate_dir: Path,
    output_dir: Path,
    session_calendar_path: Path | None = None,
    calendar_adoption_decision_path: Path | None = None,
    history_start_date: date | None = None,
    evaluation_start_date: date | None = None,
    evaluation_end_date: date | None = None,
) -> Mapping[str, Any]:
    started = time.monotonic()
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = Path(os.path.abspath(output_dir))
    if not output_dir.is_relative_to(repo_root):
        raise ValueError("sample output directory must stay inside the current repository")
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError(f"refusing to overwrite existing sample directory: {output_dir}")
    archive_dir = Path(os.path.abspath(archive_dir))
    legacy_root = Path(os.path.abspath(legacy_root))
    alternate_dir = Path(os.path.abspath(alternate_dir))
    v9_values = (
        session_calendar_path,
        calendar_adoption_decision_path,
        history_start_date,
        evaluation_start_date,
        evaluation_end_date,
    )
    v9_mode = any(value is not None for value in v9_values)
    calendar_input: Mapping[str, Any] | None = None
    v8_manifest_sha256: str | None = None
    v8_source_manifest: Mapping[str, Any] | None = None
    v8_member_binding: Mapping[str, Any] | None = None
    source_revision: Mapping[str, str] | None = None
    v9_comparison: Mapping[str, Any] | None = None
    v9_coverage_totals: Mapping[str, int] | None = None
    v9_coverage_records: list[dict[str, str]] | None = None
    v9_coverage_summary: dict[str, Any] | None = None
    v9_coverage_gzip: bytes | None = None
    v9_coverage_artifact: Mapping[str, Any] | None = None
    if v9_mode:
        if any(value is None for value in v9_values):
            raise ValueError("v9 calendar and date inputs must be supplied together")
        if (
            history_start_date != V9_HISTORY_START
            or evaluation_start_date != V9_EVALUATION_START
            or evaluation_end_date != V9_EVALUATION_END
        ):
            raise ValueError("v9 history and evaluation windows must match the adjudicated plan")
        expected_output = repo_root / "docs" / "issue-70-q4-source-sample-v9"
        if output_dir != expected_output:
            raise ValueError(f"v9 output must use its owned no-clobber destination: {expected_output}")
        assert session_calendar_path is not None and calendar_adoption_decision_path is not None
        calendar_input = _validate_v9_calendar_inputs(
            session_calendar_path=Path(session_calendar_path),
            adoption_decision_path=Path(calendar_adoption_decision_path),
            calendar_provenance_path=Path(session_calendar_path).parent / "calendar_provenance.json",
            candidate_marker_path=Path(session_calendar_path).parent / "candidate-publication.json",
            retained_calendar_path=legacy_root / "prices" / "spy_trading_days.csv",
        )
        v8_manifest_path = repo_root / "docs" / "issue-70-q4-source-sample-v8" / "fundamentals_provenance.json"
        v8_manifest_sha256, v8_source_manifest = _read_bound_json(
            v8_manifest_path, V9_V8_SOURCE_MANIFEST_SHA256
        )
        source_revision = _source_revision(repo_root)
    else:
        history_start_date = START_DATE
        evaluation_start_date = START_DATE
        evaluation_end_date = END_DATE
    assert history_start_date is not None
    assert evaluation_start_date is not None
    assert evaluation_end_date is not None
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    legacy_fundamentals_dir = legacy_root / "fundamentals"
    prices_dir = legacy_root / "prices"
    import_provenance_path = legacy_root / "import-provenance.json"
    input_receipt = _json_file(import_provenance_path)
    old_provenance = _json_file(legacy_fundamentals_dir / "fundamentals_provenance.json")
    alternate_provenance = _json_file(alternate_dir / "fundamentals_provenance.json")
    alternate_marker = _json_file(alternate_dir / "fundamentals_publication.json")
    archive_receipt, archive_metadata = _verify_archive_receipt(archive_dir)
    if old_provenance.get("archive_manifest") != archive_receipt:
        raise ValueError("legacy fundamental provenance is not bound to the retained SEC archives")
    if alternate_provenance.get("archive_manifest") != archive_receipt:
        raise ValueError("alternate fundamental provenance is not bound to the retained SEC archives")
    for label, source_provenance in (
        ("legacy", old_provenance),
        ("alternate", alternate_provenance),
    ):
        for archive_name in ("submissions.zip", "companyfacts.zip"):
            key = "submissions_archive_sha256" if archive_name == "submissions.zip" else "companyfacts_archive_sha256"
            if source_provenance.get(key) != archive_receipt["archives"][archive_name]["sha256"]:
                raise ValueError(f"{label} provenance archive digest differs for {archive_name}")
    if input_receipt.get("admission_status") != "authenticated_previous_SP500_material_only_not_complete_V5_inputs":
        raise ValueError("legacy import receipt has an unexpected admission status")

    input_paths = {
        "fundamentals_csv": legacy_fundamentals_dir / "fundamentals.csv",
        "security_master_csv": legacy_fundamentals_dir / "security_master.csv",
        "security_names_csv": legacy_fundamentals_dir / "security_names.csv",
        "membership_csv": prices_dir / "membership.csv",
        "spy_trading_days_csv": prices_dir / "spy_trading_days.csv",
        "identity_manifest_csv": prices_dir / "pit_price_identity_map.csv",
    }
    relative_receipt_names = {
        "fundamentals_csv": "fundamentals/fundamentals.csv",
        "security_master_csv": "fundamentals/security_master.csv",
        "security_names_csv": "fundamentals/security_names.csv",
        "membership_csv": "prices/membership.csv",
        "spy_trading_days_csv": "prices/spy_trading_days.csv",
        "identity_manifest_csv": "prices/pit_price_identity_map.csv",
    }
    receipt_files = {
        row.get("file"): row for row in input_receipt.get("files", []) if isinstance(row, dict)
    }
    for key, path in input_paths.items():
        record = receipt_files.get(relative_receipt_names[key])
        if not isinstance(record, dict) or not record.get("bound_by_original_provenance"):
            raise ValueError(f"legacy import receipt does not bind {path.name}")
        if path.stat().st_size != record.get("bytes"):
            raise ValueError(f"legacy source byte size differs from import receipt: {path.name}")
        if key != "fundamentals_csv":
            _validate_bound_file(path, str(record.get("sha256", "")), expected_bytes=record["bytes"])

    if old_provenance.get("fundamentals_sha256") != receipt_files["fundamentals/fundamentals.csv"].get("sha256"):
        raise ValueError("legacy fundamentals digest differs between its two provenance records")
    if old_provenance.get("security_master_sha256") != receipt_files["fundamentals/security_master.csv"].get("sha256"):
        raise ValueError("legacy security-master digest differs between its two provenance records")
    for name, key in (
        ("membership_csv_sha256", "membership_csv"),
        ("security_names_csv_sha256", "security_names_csv"),
        ("spy_trading_days_csv_sha256", "spy_trading_days_csv"),
        ("identity_manifest_csv_sha256", "identity_manifest_csv"),
    ):
        if old_provenance.get(name) != receipt_files[relative_receipt_names[key]].get("sha256"):
            raise ValueError(f"legacy input hash differs from fundamental provenance: {name}")

    archive_paths = {name: archive_dir / name for name in archive_metadata}
    archive_file_baseline = {
        name: dict(_archive_file_metadata(path)) for name, path in archive_paths.items()
    }
    for name, path in archive_paths.items():
        if path.stat().st_size != archive_metadata[name]["byte_length"]:
            raise ValueError(f"retained SEC archive size differs from attestation: {name}")

    security_rows = _load_security_rows(input_paths["security_master_csv"])
    if v9_mode:
        actual_sample_ciks = {row.ticker: row.cik for row in security_rows}
        if actual_sample_ciks != V9_EXPECTED_CIKS:
            raise ValueError("v9 retained security master differs from the adjudicated four CIK sample")
    ciks = tuple(sorted({row.cik for row in security_rows}))
    if len(ciks) != len(TICKERS):
        raise ValueError("four-ticker sample unexpectedly resolves to a non-unique CIK count")
    submissions_budget = _submission_workset(archive_paths["submissions.zip"], ciks)
    companyfacts_budget = _companyfacts_workset(archive_paths["companyfacts.zip"], ciks)
    for name, budget in (
        ("submissions.zip", submissions_budget),
        ("companyfacts.zip", companyfacts_budget),
    ):
        metadata = archive_metadata[name]
        if budget["zip_entry_count"] != metadata["zip_entry_count"]:
            raise ValueError(f"retained SEC ZIP entry count differs from attestation: {name}")
        if budget["zip_uncompressed_bytes"] != metadata["zip_uncompressed_bytes"]:
            raise ValueError(f"retained SEC ZIP uncompressed size differs from attestation: {name}")

    submissions_member_usage: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    companyfacts_member_usage: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

    def record_submission_read(member_name: str, expanded_bytes: int, sha256: str) -> None:
        if expanded_bytes > MAX_SELECTED_MEMBER_BYTES:
            raise ValueError(f"decompressed submissions member exceeds 64 MiB: {member_name}")
        submissions_member_usage[member_name].append(
            {"expanded_bytes": expanded_bytes, "sha256": sha256}
        )

    def record_companyfacts_read(member_name: str, expanded_bytes: int, sha256: str) -> None:
        if expanded_bytes > MAX_SELECTED_MEMBER_BYTES:
            raise ValueError(f"decompressed companyfacts member exceeds 64 MiB: {member_name}")
        companyfacts_member_usage[member_name].append(
            {"expanded_bytes": expanded_bytes, "sha256": sha256}
        )

    acceptances, missing_fragments = sec._acceptances_for_ciks(
        archive_paths["submissions.zip"],
        ciks,
        max_json_member_bytes=512 * 1024 * 1024,
        member_use_callback=record_submission_read,
    )
    security_master = sec.SecurityMasterResult(
        rows=security_rows,
        exclusions=(),
        acceptance_by_cik=acceptances,
        membership_union=tuple(sorted(TICKERS)),
        identity_manifest_sha256=str(old_provenance["identity_manifest_csv_sha256"]),
        submissions_archive_sha256=str(archive_metadata["submissions.zip"]["sha256"]),
        missing_submission_fragments=missing_fragments,
    )
    parser_calendar_path = (
        Path(session_calendar_path)
        if v9_mode and session_calendar_path is not None
        else input_paths["spy_trading_days_csv"]
    )
    fundamentals = sec.extract_fundamentals(
        archive_paths["companyfacts.zip"],
        security_master,
        parser_calendar_path,
        start_date=history_start_date,
        end_date=evaluation_end_date,
        max_json_member_bytes=512 * 1024 * 1024,
        attested_companyfacts_archive_sha256=str(archive_metadata["companyfacts.zip"]["sha256"]),
        companyfacts_member_use_callback=record_companyfacts_read,
    )
    submission_preflight = submissions_budget["main_preflight_reads"]
    for member_name, preflight in submission_preflight.items():
        actual_reads = submissions_member_usage.get(member_name, [])
        if len(actual_reads) != 1 or dict(actual_reads[0]) != dict(preflight):
            raise ValueError(f"submissions main member changed between preflight and parsing: {member_name}")
    if set(submissions_member_usage) != set(submissions_budget["selected_member_signatures"]):
        raise ValueError("submissions reads do not match the preflight-selected member set")
    for member_name, actual_reads in submissions_member_usage.items():
        expected_size = submissions_budget["selected_member_signatures"][member_name]["expanded_bytes"]
        if not actual_reads or any(item["expanded_bytes"] != expected_size for item in actual_reads):
            raise ValueError(f"submissions expanded member size changed: {member_name}")
        if len({item["sha256"] for item in actual_reads}) != 1:
            raise ValueError(f"submissions member content changed between reads: {member_name}")
    if set(companyfacts_member_usage) != set(companyfacts_budget["selected_member_signatures"]):
        raise ValueError("companyfacts reads do not match the preflight-selected member set")
    for member_name, actual_reads in companyfacts_member_usage.items():
        expected_size = companyfacts_budget["selected_member_signatures"][member_name]["expanded_bytes"]
        if len(actual_reads) != 1 or actual_reads[0]["expanded_bytes"] != expected_size:
            raise ValueError(f"companyfacts expanded member size changed: {member_name}")

    submission_preflight_bytes = sum(item["expanded_bytes"] for item in submission_preflight.values())
    submission_parse_bytes = sum(
        item["expanded_bytes"]
        for reads in submissions_member_usage.values()
        for item in reads
    )
    companyfacts_parse_bytes = sum(
        item["expanded_bytes"]
        for reads in companyfacts_member_usage.values()
        for item in reads
    )
    total_selected_decompressed_bytes = (
        submission_preflight_bytes + submission_parse_bytes + companyfacts_parse_bytes
    )
    if total_selected_decompressed_bytes > MAX_SELECTED_UNCOMPRESSED_BYTES:
        raise ValueError("combined selected-member decompression exceeds 512 MiB")

    selected_member_hashes = {
        "submissions": [
            {
                "member_name": member_name,
                "expanded_bytes": submissions_budget["selected_member_signatures"][member_name]["expanded_bytes"],
                "sha256": submissions_member_usage[member_name][0]["sha256"],
                "preflight_sha256": submission_preflight.get(member_name, {}).get("sha256"),
                "read_passes": len(submissions_member_usage[member_name])
                + int(member_name in submission_preflight),
            }
            for member_name in sorted(submissions_member_usage)
        ],
        "companyfacts": [
            {
                "member_name": member_name,
                "expanded_bytes": companyfacts_budget["selected_member_signatures"][member_name]["expanded_bytes"],
                "sha256": companyfacts_member_usage[member_name][0]["sha256"],
                "read_passes": 1,
            }
            for member_name in sorted(companyfacts_member_usage)
        ],
    }
    if v9_mode:
        assert v8_source_manifest is not None
        v8_member_binding = _validate_v8_selected_member_hashes(
            v8_source_manifest, selected_member_hashes
        )

        assert calendar_input is not None
        assert v8_manifest_sha256 is not None
        assert source_revision is not None
        evaluation_sessions = tuple(
            session for session in calendar_input["sessions"]
            if evaluation_start_date <= session <= evaluation_end_date
        )
        if len(security_rows) * len(evaluation_sessions) > MAX_ISSUER_SESSION_GRID:
            raise ValueError("v9 issuer-session grid exceeds the 6,032-cell ceiling")
        if len(fundamentals.rows) != len(fundamentals.audit_rows):
            raise ValueError("v9 financial snapshot and audit rows must be one-to-one")
        if len(fundamentals.rows) > MAX_FINANCIAL_AUDIT_ROWS:
            raise ValueError("v9 paired financial and audit rows exceed the 10,000-row ceiling")

        normalized_origins: list[dict[str, Any]] = []
        v9_coverage_records, v9_coverage_summary = _build_v9_coverage_records(
            fundamentals=fundamentals,
            security_rows=security_rows,
            evaluation_sessions=evaluation_sessions,
            calendar_sessions=calendar_input["sessions"],
            normalized_origins_out=normalized_origins,
        )
        v9_coverage_gzip, v9_coverage_artifact = serialize_v9_coverage_records(
            v9_coverage_records,
            max_records=MAX_COVERAGE_RECORDS,
            max_uncompressed_bytes=MAX_COVERAGE_UNCOMPRESSED_BYTES,
        )
        v9_comparison = build_v8_source_window_comparison(
            v9_records=v9_coverage_records,
            normalized_origins=normalized_origins,
            evaluation_sessions=evaluation_sessions,
            calendar_sessions=calendar_input["sessions"],
            source_window_start=START_DATE,
            source_window_end=END_DATE,
            v8_manifest_sha256=v8_manifest_sha256,
            adopted_calendar_sha256=str(calendar_input["calendar_sha256"]),
            source_revision=source_revision,
            measured_member_binding=v8_member_binding,
        )
        v9_coverage_summary = _complete_v9_coverage_summary(
            v9_coverage_summary,
            v9_coverage_artifact,
            v9_comparison,
            fundamentals.coverage,
        )
        v9_coverage_totals = _validate_combined_v9_evidence_caps(
            v9_coverage_artifact, v9_coverage_summary,
            max_records=MAX_COVERAGE_RECORDS,
            max_uncompressed_bytes=MAX_COVERAGE_UNCOMPRESSED_BYTES,
        )

    def verify_archive_stability() -> Mapping[str, Any]:
        stability: dict[str, Any] = {}
        for name, path in archive_paths.items():
            after_file = dict(_archive_file_metadata(path))
            expected_budget = (
                submissions_budget if name == "submissions.zip" else companyfacts_budget
            )
            after_directory = _zip_directory_metadata(
                path,
                tuple(expected_budget["selected_member_signatures"]),
            )
            directory_before = {
                "zip_entry_count": expected_budget["zip_entry_count"],
                "zip_uncompressed_bytes": expected_budget["zip_uncompressed_bytes"],
                "selected_member_signatures": expected_budget["selected_member_signatures"],
            }
            if after_file != archive_file_baseline[name] or after_directory != directory_before:
                raise ValueError(f"SEC archive metadata changed during source extraction: {name}")
            stability[name] = {
                "before": archive_file_baseline[name],
                "after": after_file,
                "zip_directory_metadata_unchanged": True,
            }
        return stability

    archive_stability = verify_archive_stability()
    if time.monotonic() - started > MAX_RUNTIME_SECONDS:
        raise TimeoutError("bounded four-ticker extraction exceeded the ten-minute runtime cap")

    generation_reconciliation = _scan_existing_generations(
        older_fundamentals=input_paths["fundamentals_csv"],
        older_provenance=old_provenance,
        alternate_audit=alternate_dir / "fundamentals_audit.csv",
        alternate_provenance=alternate_provenance,
        alternate_marker=alternate_marker,
    )
    superseded_sample_dir = repo_root / "docs" / "issue-70-q4-source-sample-v1"
    if superseded_sample_dir.exists():
        superseded_marker = _json_file(superseded_sample_dir / "fundamentals_publication.json")
        generation_reconciliation = {
            **generation_reconciliation,
            "superseded_sample_attempt": {
                "path": "docs/issue-70-q4-source-sample-v1",
                "publication_status": superseded_marker.get("status"),
                "reason": (
                    "Preserved, but not a delivery: facts filed before 2020 had been assigned "
                    "the first 2020 trading day because the retained trading calendar starts in 2020."
                ),
            },
        }
    superseded_v2_dir = repo_root / "docs" / "issue-70-q4-source-sample-v2"
    if superseded_v2_dir.exists():
        superseded_v2_marker = _json_file(superseded_v2_dir / "fundamentals_publication.json")
        generation_reconciliation = {
            **generation_reconciliation,
            "superseded_sample_v2": {
                "path": "docs/issue-70-q4-source-sample-v2",
                "publication_status": superseded_v2_marker.get("status"),
                "reason": (
                    "Source rows are retained, but its summary omitted valid Q4 disclosures for "
                    "2019 fiscal periods filed during the 2020-2025 window."
                ),
            },
        }
    superseded_v3_dir = repo_root / "docs" / "issue-70-q4-source-sample-v3"
    if superseded_v3_dir.exists():
        superseded_v3_marker = _json_file(superseded_v3_dir / "fundamentals_publication.json")
        generation_reconciliation = {
            **generation_reconciliation,
            "superseded_sample_v3": {
                "path": "docs/issue-70-q4-source-sample-v3",
                "publication_status": superseded_v3_marker.get("status"),
                "reason": (
                    "Source rows are retained, but its annual-release status table included "
                    "comparative annual facts as if they were current-year annual periods."
                ),
            },
        }
    superseded_v4_dir = repo_root / "docs" / "issue-70-q4-source-sample-v4"
    if superseded_v4_dir.exists():
        superseded_v4_marker = _json_file(superseded_v4_dir / "fundamentals_publication.json")
        generation_reconciliation = {
            **generation_reconciliation,
            "superseded_sample_v4": {
                "path": "docs/issue-70-q4-source-sample-v4",
                "publication_status": superseded_v4_marker.get("status"),
                "reason": (
                    "Source rows and Q4 availability are retained, but it does not hash the exact "
                    "selected SEC JSON members consumed or record before/after ZIP metadata."
                ),
            },
        }
    if time.monotonic() - started > MAX_RUNTIME_SECONDS:
        raise TimeoutError("bounded generation reconciliation exceeded the ten-minute runtime cap")

    q4_by_ticker: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    rows_by_key = {
        (row.ticker, row.statement_type, row.period_end, row.public_date): row
        for row in fundamentals.rows
    }
    for audit in fundamentals.audit_rows:
        if (
            audit.statement_type != "quarterly"
            or audit.fiscal_period.upper() != "Q4"
            or not evaluation_start_date <= audit.public_date <= evaluation_end_date
        ):
            continue
        values = rows_by_key[(audit.ticker, audit.statement_type, audit.period_end, audit.public_date)]
        sources = json.loads(audit.metric_sources)
        metric_summary: dict[str, Any] = {}
        for metric in ("basic_eps", "diluted_eps", "total_revenue"):
            source = sources.get(metric, {})
            attribution = source.get("q4_attribution")
            if attribution:
                metric_summary[metric] = {
                    "attribution": attribution,
                    "value": getattr(values, metric),
                    "source_concept": source.get("source_concept"),
                    "accession_number": source.get("accession_number"),
                    "derivation": source.get("derivation"),
                    "available_from": source.get("available_from"),
                }
        q4_by_ticker[audit.ticker].append(
            {
                "period_end": audit.period_end.isoformat(),
                "public_date": audit.public_date.isoformat(),
                "metrics": metric_summary,
                "total_revenue_available": values.total_revenue is not None,
            }
        )
    annual_filing_by_year: dict[tuple[str, str, date], list[Any]] = defaultdict(list)
    for audit in fundamentals.audit_rows:
        try:
            fiscal_year_number = int(audit.fiscal_year)
        except ValueError:
            fiscal_year_number = -1
        if (
            audit.statement_type == "annual"
            and audit.fiscal_period.upper() == "FY"
            and audit.period_end.year == fiscal_year_number
            and 2019 <= fiscal_year_number <= evaluation_end_date.year
            and evaluation_start_date <= audit.public_date <= evaluation_end_date
        ):
            annual_filing_by_year[(audit.ticker, audit.fiscal_year, audit.period_end)].append(audit)
    q4_by_year: dict[tuple[str, str, date, date], list[Any]] = defaultdict(list)
    for audit in fundamentals.audit_rows:
        if (
            audit.statement_type == "quarterly"
            and audit.fiscal_period.upper() == "Q4"
            and evaluation_start_date <= audit.public_date <= evaluation_end_date
        ):
            q4_by_year[(audit.ticker, audit.fiscal_year, audit.period_end, audit.public_date)].append(audit)
    annual_release_q4_status: dict[str, list[Mapping[str, Any]]] = {
        ticker: [] for ticker in TICKERS
    }
    for (ticker, fiscal_year, period_end), annual_versions in sorted(annual_filing_by_year.items()):
        first_annual = min(annual_versions, key=lambda row: (row.public_date, row.accession_number))
        matching_q4 = q4_by_year.get(
            (ticker, fiscal_year, period_end, first_annual.public_date), []
        )
        if not matching_q4:
            annual_release_q4_status[ticker].append(
                {
                    "fiscal_year": fiscal_year,
                    "period_end": period_end.isoformat(),
                    "annual_public_date": first_annual.public_date.isoformat(),
                    "q4_status": "unavailable_at_annual_release",
                    "reason": (
                        "No source-attributed Q4 row was available from the retained filings "
                        "under the direct/basis-reconciled rules."
                    ),
                }
            )
            continue
        q4_audit = max(matching_q4, key=lambda row: row.accession_number)
        q4_row = rows_by_key[
            (ticker, "quarterly", q4_audit.period_end, q4_audit.public_date)
        ]
        metric_sources = json.loads(q4_audit.metric_sources)
        metric_attribution = {
            metric: str(
                metric_sources.get(metric, {}).get("q4_attribution")
                or ("unavailable" if getattr(q4_row, metric) is None else "source_value_unattributed")
            )
            for metric in ("basic_eps", "diluted_eps", "total_revenue")
        }
        annual_release_q4_status[ticker].append(
            {
                "fiscal_year": fiscal_year,
                "period_end": period_end.isoformat(),
                "annual_public_date": first_annual.public_date.isoformat(),
                "q4_public_date": q4_audit.public_date.isoformat(),
                "q4_status": "source_row_available",
                "metric_attribution": metric_attribution,
            }
        )
    q4_summary = {
        "window": [evaluation_start_date.isoformat(), evaluation_end_date.isoformat()],
        "sample_tickers": list(TICKERS),
        "q4_rows_by_ticker": {ticker: q4_by_ticker.get(ticker, []) for ticker in TICKERS},
        "annual_release_q4_status_by_ticker": annual_release_q4_status,
        "direct_q4_metric_count": fundamentals.coverage.get("q4_direct_fact_metric_count", 0),
        "derived_q4_revenue_candidate_count": fundamentals.coverage.get(
            "q4_revenue_derived_candidate_count", 0
        ),
        "unavailable_derivation_count": fundamentals.coverage.get(
            "q4_revenue_incompatible_basis_count", 0
        )
        + fundamentals.coverage.get("q4_revenue_missing_quarter_sequence_count", 0)
        + fundamentals.coverage.get("q4_revenue_ambiguous_quarter_sequence_count", 0),
        "annual_values_are_not_used_as_quarterly_fallbacks": True,
        "eps_additivity_is_not_assumed": True,
    }

    input_hashes = {
        key: _digest(path)
        for key, path in input_paths.items()
        if key != "fundamentals_csv"
    }
    input_hashes["legacy_fundamentals_csv_sha256"] = str(old_provenance["fundamentals_sha256"])
    generated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    archive_attestation_reuse = {
        "source": str(archive_dir),
        "attestation_file": "sec_archives_provenance.json",
        "digest_recomputed": False,
        "validation": "attested SHA-256 and ZIP metadata were reused; file byte lengths and selected ZIP member bounds were checked",
        "file_metadata_stability": archive_stability,
        "archives": {
            name: {
                "sha256": metadata["sha256"],
                "byte_length_attested": metadata["byte_length"],
                "byte_length_current": archive_paths[name].stat().st_size,
                "zip_entry_count_attested": metadata["zip_entry_count"],
                "zip_uncompressed_bytes_attested": metadata["zip_uncompressed_bytes"],
            }
            for name, metadata in archive_metadata.items()
        },
    }
    provenance = {
        "schema_version": 2 if v9_mode else 1,
        "source": (
            "SEC EDGAR official bulk archives; bounded retained-source Q4 sample"
            if not v9_mode
            else "SEC EDGAR official bulk archives; bounded retained-source v9 Q4 and historical-coverage sample"
        ),
        "generated_at_utc": generated_at_utc,
        "sample_tickers": list(TICKERS),
        "start_date": history_start_date.isoformat(),
        "end_date": evaluation_end_date.isoformat(),
        "membership_source_generation": "authenticated previous S&P 500 material; not complete V5 input set",
        "source_archive_attestation_reuse": archive_attestation_reuse,
        "input_hashes": input_hashes,
        "source_budgets": {
            "companyfacts_selected_members": dict(companyfacts_budget),
            "submissions_selected_members": dict(submissions_budget),
            "selected_member_hashes": selected_member_hashes,
            **({"v8_selected_member_binding": dict(v8_member_binding)} if v9_mode else {}),
            "decompressed_bytes": {
                "submissions_preflight_main_members": submission_preflight_bytes,
                "submissions_acceptance_parse": submission_parse_bytes,
                "companyfacts_parse": companyfacts_parse_bytes,
                "combined_total": total_selected_decompressed_bytes,
                "combined_limit": MAX_SELECTED_UNCOMPRESSED_BYTES,
            },
            "legacy_fundamentals_csv_max_bytes": MAX_LEGACY_FUNDAMENTALS_BYTES,
            "alternate_audit_max_bytes": MAX_ALTERNATE_AUDIT_BYTES,
            "sample_output_max_bytes": MAX_OUTPUT_BYTES,
            "runtime_max_seconds": MAX_RUNTIME_SECONDS,
            **({
                "financial_audit_row_pairs_max": MAX_FINANCIAL_AUDIT_ROWS,
                "issuer_session_grid_max": MAX_ISSUER_SESSION_GRID,
                "coverage_records_max": MAX_COVERAGE_RECORDS,
                "coverage_uncompressed_bytes_max": MAX_COVERAGE_UNCOMPRESSED_BYTES,
            } if v9_mode else {}),
        },
        "q4_policy": {
            "direct_fact": "Accept source-duration Q4 reported in a 10-K when its endpoint is the annual period end in the same accession, or the source explicitly labels Q4.",
            "revenue_derivation": "Derive FY minus Q1 minus Q2 minus Q3 only when all four facts share accession, exact US-GAAP concept, USD unit, base scale, and Q1-Q3 plus residual Q4 form a contiguous partition of the FY interval.",
            "unavailable": "Keep Q4 revenue unavailable when selected vintages or accounting-basis evidence do not reconcile.",
            "eps": "Never derive EPS by subtraction.",
            "annual_fallback": "Never place annual values in a quarterly Q4 slot.",
        },
        "coverage": dict(fundamentals.coverage),
        "limitations": [
            "This is a four-ticker research sample, not a full-universe or production-eligible export.",
            "The retained 142329 generation has documented exclusions and is not complete V5 lineage input.",
            "The 145010 generation remains a separate comparison population with an unavailable retained identity manifest input.",
            "Full financial production eligibility remains gated on principal issue-68 membership and reference-source acceptance.",
            "The v1 sample directory is preserved but superseded because pre-window filing dates were clipped to the first available 2020 trading day.",
            "The v2 sample directory is preserved but superseded because its summary omitted 2019 fiscal periods disclosed during the 2020-2025 filing window.",
            "The v3 sample directory is preserved but superseded because its annual-release status table included comparative annual facts.",
            "The v4 sample directory is preserved but superseded because it did not bind exact selected SEC JSON member bytes.",
        ],
    }
    if v9_mode:
        assert calendar_input is not None
        assert v8_manifest_sha256 is not None
        assert source_revision is not None
        assert v8_member_binding is not None
        assert v9_comparison is not None
        assert v9_coverage_summary is not None
        assert v9_coverage_totals is not None
        provenance.update({
            "history_window": [history_start_date.isoformat(), evaluation_end_date.isoformat()],
            "evaluation_window": [evaluation_start_date.isoformat(), evaluation_end_date.isoformat()],
            "source_revision": dict(source_revision),
            "v9_adopted_calendar": {
                "calendar_sha256": calendar_input["calendar_sha256"],
                "calendar_provenance_sha256": calendar_input["calendar_provenance_sha256"],
                "candidate_marker_sha256": calendar_input["candidate_marker_sha256"],
                "adoption_decision_sha256": calendar_input["adoption_decision_sha256"],
                "independent_review_sha256": calendar_input["independent_review_sha256"],
                "reviewed_commit": calendar_input["reviewed_commit"],
                "reviewed_tree": calendar_input["reviewed_tree"],
                "source_label": calendar_input["source_label"],
                "not_observed_price_history": True,
                "wheel_sha256": calendar_input["wheel_sha256"],
                "retained_reference_sha256": calendar_input["retained_reference_sha256"],
                "retained_window_overlap": dict(calendar_input["retained_window_overlap"]),
            },
            "v8_source_manifest_sha256": v8_manifest_sha256,
            "financial_coverage_evidence": dict(v9_coverage_artifact),
            "financial_coverage_evidence_totals": dict(v9_coverage_totals),
            "v8_selected_member_binding": dict(v8_member_binding),
            "financial_coverage_comparison": {
                "schema_version": v9_comparison["schema_version"],
                "label": v9_comparison["label"],
                "protocol_sha256": v9_comparison["protocol_sha256"],
                "complete_comparison_sha256": v9_comparison["complete_comparison_sha256"],
                "canonical_detail_byte_length": v9_comparison["canonical_detail_byte_length"],
                "summary_sha256": hashlib.sha256(_json_bytes(v9_coverage_summary)).hexdigest(),
                "source_identity": dict(v9_comparison["source_identity"]),
                "source_window": dict(v9_comparison["source_window"]),
            },
            "full_issue_70_acceptance": "open",
        })

    staging = output_dir.parent / f".{output_dir.name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        _write_csv(
            staging / "security_master.csv",
            sec.SECURITY_MASTER_COLUMNS,
            (
                (
                    row.ticker,
                    row.cik,
                    row.company_name,
                    row.first_membership_date.isoformat(),
                    row.last_membership_date.isoformat(),
                    row.mapping_basis,
                )
                for row in security_rows
            ),
        )
        _write_csv(
            staging / "fundamentals.csv",
            sec.FUNDAMENTAL_COLUMNS,
            (_fundamental_values(row) for row in fundamentals.rows),
        )
        _write_csv(
            staging / "fundamentals_audit.csv",
            sec.FUNDAMENTAL_AUDIT_COLUMNS,
            (_audit_values(row) for row in fundamentals.audit_rows),
        )
        _write_json(staging / "fundamentals_coverage.json", dict(fundamentals.coverage))
        _write_json(staging / "sample_q4_findings.json", q4_summary)
        _write_json(staging / "generation_reconciliation.json", generation_reconciliation)
        _write_json(staging / "fundamentals_provenance.json", provenance)
        if v9_mode:
            assert v9_coverage_summary is not None
            assert v9_coverage_gzip is not None
            assert session_calendar_path is not None
            assert calendar_adoption_decision_path is not None
            _write_json(staging / "financial_coverage_summary.json", v9_coverage_summary)
            with (staging / "financial_coverage_slots.csv.gz").open("xb") as stream:
                stream.write(v9_coverage_gzip)
                stream.flush()
                os.fsync(stream.fileno())
            calendar_source_dir = Path(session_calendar_path).parent
            for source_path, output_name in (
                (Path(session_calendar_path), "exchange_sessions.csv"),
                (calendar_source_dir / "calendar_provenance.json", "calendar_provenance.json"),
                (calendar_source_dir / "candidate-publication.json", "candidate-publication.json"),
                (Path(calendar_adoption_decision_path), "principal-adoption-decision.json"),
            ):
                with sec._regular_file(source_path, source_path.name).open("rb") as source, (
                    staging / output_name
                ).open("xb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            if (
                _digest(staging / "exchange_sessions.csv") != calendar_input["calendar_sha256"]
                or _digest(staging / "calendar_provenance.json") != calendar_input["calendar_provenance_sha256"]
                or _digest(staging / "candidate-publication.json") != calendar_input["candidate_marker_sha256"]
                or _digest(staging / "principal-adoption-decision.json") != calendar_input["adoption_decision_sha256"]
            ):
                raise ValueError("copied v9 calendar evidence differs from its adopted input hashes")
        for source_key, output_name in (
            ("membership_csv", "membership.csv"),
            ("security_names_csv", "security_names.csv"),
            ("spy_trading_days_csv", "spy_trading_days.csv"),
            ("identity_manifest_csv", "identity_manifest.csv"),
        ):
            with input_paths[source_key].open("rb") as source, (staging / output_name).open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        with (archive_dir / "sec_archives_provenance.json").open("rb") as source, (
            staging / "source_archive_attestation.json"
        ).open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        publication_files = sorted(path.name for path in staging.iterdir() if path.is_file())
        publication_marker = {
            "schema_version": 1,
            "status": "complete",
            "files": {name: _digest(staging / name) for name in publication_files},
        }
        _write_json(staging / "fundamentals_publication.json", publication_marker)
        total_output_bytes = sum(path.stat().st_size for path in staging.iterdir() if path.is_file())
        if total_output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("sample publication exceeds the 20 MiB output budget")
        if time.monotonic() - started > MAX_RUNTIME_SECONDS:
            raise TimeoutError("source extraction and publication exceeded the ten-minute runtime cap")
        if verify_archive_stability() != archive_stability:
            raise ValueError("SEC archive metadata changed before sample publication")
        os.rename(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "output_dir": str(output_dir),
        "publication_status": "complete",
        "publication_files": publication_marker["files"],
        "total_output_bytes": total_output_bytes,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "q4_findings": q4_summary,
        "generation_reconciliation": generation_reconciliation,
    }


def main(argv: Sequence[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    retained_root = Path(
        r"C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\data"
    )

    def parse_date(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=retained_root / "sec-fundamentals",
    )
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=retained_root / "acquisition" / "acquisition" / "raw" / "local-cache",
    )
    parser.add_argument(
        "--alternate-dir",
        type=Path,
        default=retained_root / "sec-fundamentals",
    )
    parser.add_argument("--session-calendar", type=Path)
    parser.add_argument("--calendar-adoption-decision", type=Path)
    parser.add_argument("--history-start-date", type=parse_date)
    parser.add_argument("--evaluation-start-date", type=parse_date)
    parser.add_argument("--evaluation-end-date", type=parse_date)
    parser.add_argument(
        "--output-dir",
        type=Path,
    )
    args = parser.parse_args(argv)
    v9_requested = any(
        value is not None
        for value in (
            args.session_calendar,
            args.calendar_adoption_decision,
            args.history_start_date,
            args.evaluation_start_date,
            args.evaluation_end_date,
        )
    )
    output_dir = args.output_dir or (
        repo_root / "docs" / (
            "issue-70-q4-source-sample-v9" if v9_requested else "issue-70-q4-source-sample-v8"
        )
    )
    try:
        result = generate(
            archive_dir=args.archive_dir,
            legacy_root=args.legacy_root,
            alternate_dir=args.alternate_dir,
            output_dir=output_dir,
            session_calendar_path=args.session_calendar,
            calendar_adoption_decision_path=args.calendar_adoption_decision,
            history_start_date=args.history_start_date,
            evaluation_start_date=args.evaluation_start_date,
            evaluation_end_date=args.evaluation_end_date,
        )
    except (OSError, ValueError, TimeoutError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
