#!/usr/bin/env python3
"""Measure retained SEC-fundamental coverage without acquiring or exporting data.

This is a bounded offline assessment for Historical-05 / issue #70. It validates
the supplied export and archive digests before reading data, evaluates two
historical membership snapshots, and opens only three named company members from
each SEC archive. It never writes source records or calculates a strategy score.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from core.canslim.fiscal_periods import (  # noqa: E402
    FISCAL_YOY_TOLERANCE_DAYS,
    match_fiscal_year_over_year_periods,
)


SNAPSHOT_DATES = ("2021-01-04", "2025-12-31")
SAMPLE_TICKERS = ("A", "AMZN", "MSFT")
METRIC_FIELDS = (
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "common_stock",
    "total_stockholders_equity",
    "shares_outstanding",
)
Q4_CAPABLE_FIELDS = ("basic_eps", "diluted_eps", "total_revenue", "net_income")
EXPECTED_FACT_UNITS = {
    "basic_eps": {"USD/shares"},
    "diluted_eps": {"USD/shares"},
    "total_revenue": {"USD"},
    "net_income": {"USD"},
    "common_stock": {"USD"},
    "total_stockholders_equity": {"USD"},
    "shares_outstanding": {"shares"},
}
DURATION_METRICS = {
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
}
INSTANT_METRICS = {"common_stock", "total_stockholders_equity", "shares_outstanding"}
FACT_CONCEPTS = {
    "basic_eps": (("us-gaap", "EarningsPerShareBasic"),),
    "diluted_eps": (("us-gaap", "EarningsPerShareDiluted"),),
    "net_income": (
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    ),
    "total_revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
    ),
    "common_stock": (("us-gaap", "CommonStockValue"),),
    "total_stockholders_equity": (("us-gaap", "StockholdersEquity"),),
    "shares_outstanding": (("dei", "EntityCommonStockSharesOutstanding"),),
}
GROWTH_PROFILES = (
    ("quarterly", "basic_eps", 4),
    ("quarterly", "diluted_eps", 4),
    ("quarterly", "total_revenue", 2),
    ("annual", "basic_eps", 3),
    ("annual", "diluted_eps", 3),
    ("annual", "total_revenue", 3),
    ("annual", "net_income", 3),
)
EXPECTED_EXPORTS = {
    "fundamentals.csv": "fundamentals_sha256",
    "fundamentals_audit.csv": "fundamentals_audit_sha256",
    "fundamentals_coverage.json": "fundamentals_coverage_sha256",
    "security_master.csv": "security_master_sha256",
    "security_master_exclusions.csv": "security_master_exclusions_sha256",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        yield from csv.DictReader(source)


def git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def qualify_companyfacts_match(
    candidates: list[dict[str, Any]],
    *,
    csv_value: float,
    metric: str,
    statement_type: str,
    export_period_end: str,
) -> dict[str, Any]:
    """Require an exact period, compatible statement duration, and expected unit."""
    expected_units = EXPECTED_FACT_UNITS.get(metric)
    if expected_units is None:
        return {
            "disposition": "unsupported_metric",
            "qualifying_candidate_count": 0,
            "matched_candidate": None,
        }

    target_end = parse_date(export_period_end)
    exact_period = [
        candidate
        for candidate in candidates
        if target_end is not None
        and parse_date(str(candidate.get("period_end", ""))) == target_end
    ]
    if not exact_period:
        disposition = "no_exact_period_end"
        family_candidates: list[dict[str, Any]] = []
        unit_candidates: list[dict[str, Any]] = []
    else:
        if metric in DURATION_METRICS:
            compatible_statement = statement_type in {"quarterly", "annual"}
        else:
            compatible_statement = metric in INSTANT_METRICS and statement_type == "balance"
        if not compatible_statement:
            disposition = "no_compatible_statement_family"
            family_candidates = []
            unit_candidates = []
        else:
            family_candidates = []
            for candidate in exact_period:
                start = parse_date(str(candidate.get("period_start", "")))
                if statement_type == "balance":
                    compatible_duration = not candidate.get("period_start")
                elif start is None or target_end is None:
                    compatible_duration = False
                else:
                    days = (target_end - start).days
                    compatible_duration = (
                        70 <= days <= 115
                        if statement_type == "quarterly"
                        else 300 <= days <= 430
                    )
                if compatible_duration:
                    family_candidates.append(candidate)
            if not family_candidates:
                disposition = "no_compatible_duration_family"
                unit_candidates = []
            else:
                unit_candidates = [
                    candidate
                    for candidate in family_candidates
                    if candidate.get("unit") in expected_units
                ]
                if not unit_candidates:
                    disposition = "no_expected_unit"
                elif len(unit_candidates) > 1:
                    disposition = "ambiguous_multiple_qualifying_facts"
                elif unit_candidates[0].get("value") is None:
                    disposition = "no_numeric_source_value"
                elif not math.isclose(
                    unit_candidates[0]["value"],
                    csv_value,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    disposition = "source_value_mismatch"
                else:
                    disposition = "matched_unique"

    matched_candidate = (
        unit_candidates[0] if disposition == "matched_unique" else None
    )
    return {
        "disposition": disposition,
        "qualifying_candidate_count": len(unit_candidates),
        "matched_candidate": matched_candidate,
    }


def parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_string_set(value: str | None) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(parsed, list):
        return {str(part) for part in parsed}
    if isinstance(parsed, dict):
        return {str(part) for part in parsed}
    return set()


def verify_inputs(
    source_dir: Path,
    membership_path: Path,
    trading_days_path: Path,
    comparison_manifest_path: Path | None,
    reuse_archive_digests_from: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    provenance_path = source_dir / "fundamentals_provenance.json"
    provenance = read_json(provenance_path)
    archive_provenance = read_json(source_dir / "sec_archives_provenance.json")
    publication_path = source_dir / "fundamentals_publication.json"
    publication = read_json(publication_path)

    checks: list[dict[str, Any]] = []
    reused_archive_hashes: dict[str, dict[str, Any]] = {}
    archive_digest_reuse: dict[str, Any] | None = None
    if reuse_archive_digests_from is not None:
        prior_path = reuse_archive_digests_from.resolve()
        required_prior_path = (
            REPO_ROOT / "docs/issue-70-bounded-source-assessment-receipt.json"
        ).resolve()
        if os.path.normcase(str(prior_path)) != os.path.normcase(
            str(required_prior_path)
        ):
            raise ValueError("Archive digest reuse is limited to the committed original receipt")
        try:
            committed_prior_bytes = subprocess.check_output(
                ["git", "show", "HEAD:docs/issue-70-bounded-source-assessment-receipt.json"],
                cwd=REPO_ROOT,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("Cannot verify the prior receipt against the current commit") from exc
        prior_bytes_for_commit_check = prior_path.read_bytes().replace(b"\r\n", b"\n")
        if prior_bytes_for_commit_check != committed_prior_bytes.replace(b"\r\n", b"\n"):
            raise ValueError("Prior receipt content differs from the committed original receipt")
        prior_receipt = read_json(prior_path)
        prior_source = prior_receipt.get("source_identity", {})
        prior_source_dir = Path(prior_source.get("source_directory", "")).resolve()
        if os.path.normcase(str(prior_source_dir)) != os.path.normcase(
            str(source_dir.resolve())
        ):
            raise ValueError("Prior receipt source_directory does not match current source directory")
        prior_checks = {
            item.get("file"): item
            for item in prior_source.get("verified_inputs", [])
            if isinstance(item, dict)
        }
        archive_manifest = provenance["archive_manifest"]["archives"]
        for filename, provenance_key in (
            ("companyfacts.zip", "companyfacts_archive_sha256"),
            ("submissions.zip", "submissions_archive_sha256"),
        ):
            previous = prior_checks.get(filename, {})
            expected = str(provenance[provenance_key]).lower()
            declared = archive_manifest[filename]
            sidecar = archive_provenance["archives"][filename]
            current_bytes = (source_dir / filename).stat().st_size
            valid_prior_identity = (
                previous.get("matches_export_provenance") is True
                and str(previous.get("actual_sha256", "")).lower() == expected
                and str(previous.get("expected_sha256", "")).lower() == expected
                and int(previous.get("bytes", -1)) == current_bytes
                and expected == str(declared["sha256"]).lower()
                == str(sidecar["sha256"]).lower()
                and current_bytes == int(declared["byte_length"])
                == int(sidecar["byte_length"])
            )
            if not valid_prior_identity:
                raise ValueError(
                    f"Prior receipt cannot authenticate unchanged {filename}"
                )
            reused_archive_hashes[filename] = previous
        archive_digest_reuse = {
            "verification_mode": "reused prior receipt digest attestation",
            "prior_receipt_path": str(prior_path),
            "prior_receipt_sha256": sha256_file(prior_path),
            "prior_repository_head": prior_receipt.get("code_identity", {}).get(
                "repository_head"
            ),
            "current_archive_content_rehashed": False,
            "same_path_and_same_size_assumption": "The current archive bytes are assumed unchanged because source_directory, byte size, prior actual/expected SHA-256, current provenance hash, and archive-sidecar hashes agree. The selected ZIP members are read again for the corrected trace.",
        }

    for filename, key in EXPECTED_EXPORTS.items():
        path = source_dir / filename
        expected = str(provenance[key]).lower()
        actual = sha256_file(path)
        checks.append(
            {
                "file": filename,
                "bytes": path.stat().st_size,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches_export_provenance": actual == expected,
            }
        )

    archive_manifest = provenance["archive_manifest"]["archives"]
    for filename, provenance_key in (
        ("companyfacts.zip", "companyfacts_archive_sha256"),
        ("submissions.zip", "submissions_archive_sha256"),
    ):
        path = source_dir / filename
        expected = str(provenance[provenance_key]).lower()
        declared = archive_manifest[filename]
        archive_sidecar = archive_provenance["archives"][filename]
        consistent_metadata = (
            expected == str(declared["sha256"]).lower()
            == str(archive_sidecar["sha256"]).lower()
            and path.stat().st_size == int(declared["byte_length"])
            == int(archive_sidecar["byte_length"])
        )
        reused = reused_archive_hashes.get(filename)
        actual = (
            str(reused["actual_sha256"]).lower()
            if reused is not None
            else sha256_file(path)
        )
        checks.append(
            {
                "file": filename,
                "bytes": path.stat().st_size,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches_export_provenance": actual == expected,
                "digest_verification_mode": (
                    "reused_prior_receipt_attestation"
                    if reused is not None
                    else "current_sha256_recomputed"
                ),
                "current_archive_content_rehashed": reused is None,
                "archive_sidecars_agree": consistent_metadata,
                "zip_entry_count_declared": declared["zip_entry_count"],
                "zip_uncompressed_bytes_declared": declared[
                    "zip_uncompressed_bytes"
                ],
            }
        )

    for path, key, label in (
        (membership_path, "membership_csv_sha256", "membership.csv"),
        (trading_days_path, "spy_trading_days_csv_sha256", "spy_trading_days.csv"),
    ):
        actual = sha256_file(path)
        expected = str(provenance[key]).lower()
        checks.append(
            {
                "file": label,
                "bytes": path.stat().st_size,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches_export_provenance": actual == expected,
            }
        )

    actual_provenance_hash = sha256_file(provenance_path)
    marker_expected_provenance = publication.get("files", {}).get(
        "fundamentals_provenance.json"
    )
    publication_marker = {
        "file": publication_path.name,
        "actual_sha256": sha256_file(publication_path),
        "declared_provenance_sha256": marker_expected_provenance,
        "actual_provenance_sha256": actual_provenance_hash,
        "provenance_hash_matches_publication_marker": (
            actual_provenance_hash == marker_expected_provenance
        ),
        "publication_marker_file_hash": publication.get("files", {}).get(
            "fundamentals_publication.json"
        ),
        "actual_publication_marker_file_hash": sha256_file(publication_path),
    }
    publication_file_refs: dict[str, Any] = {}
    marker_files = publication.get("files", {})
    for item in checks:
        filename = item["file"]
        if filename in marker_files:
            publication_expected = str(marker_files[filename]).lower()
            publication_file_refs[filename] = {
                "publication_marker_sha256": publication_expected,
                "actual_sha256": item["actual_sha256"],
                "matches_publication_marker": item["actual_sha256"] == publication_expected,
            }
    publication_file_refs["fundamentals_provenance.json"] = {
        "publication_marker_sha256": marker_expected_provenance,
        "actual_sha256": actual_provenance_hash,
        "matches_publication_marker": actual_provenance_hash == marker_expected_provenance,
    }
    publication_marker["referenced_file_hash_checks"] = publication_file_refs
    marker_bound_provenance = dict(provenance)
    marker_bound_provenance.pop("net_income_concept_priority", None)
    reconstructed_marker_bound_hash = sha256_bytes(
        (json.dumps(marker_bound_provenance, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )
    publication_marker["marker_bound_provenance_reconstruction"] = {
        "excluded_current_metadata_field": "net_income_concept_priority",
        "reconstructed_sha256": reconstructed_marker_bound_hash,
        "reconstructed_hash_matches_publication_marker": (
            reconstructed_marker_bound_hash == marker_expected_provenance
        ),
        "interpretation": "byte reconstruction only; it does not establish when, by whom, or why the current provenance field was added",
    }

    if not all(item["matches_export_provenance"] for item in checks):
        failures = [item["file"] for item in checks if not item["matches_export_provenance"]]
        raise ValueError(f"Refusing to read data; provenance digest mismatch: {failures}")
    if not all(item.get("archive_sidecars_agree", True) for item in checks):
        raise ValueError("Refusing archive reads; archive sidecar metadata disagrees")

    comparison: dict[str, Any] | None = None
    if comparison_manifest_path is not None:
        old_manifest = read_json(comparison_manifest_path)
        old_files = {item["file"]: item for item in old_manifest.get("files", [])}
        names = {
            "fundamentals.csv": "fundamentals/fundamentals.csv",
            "fundamentals_audit.csv": "fundamentals/fundamentals_audit.csv",
            "fundamentals_coverage.json": "fundamentals/fundamentals_coverage.json",
            "security_master.csv": "fundamentals/security_master.csv",
            "security_master_exclusions.csv": "fundamentals/security_master_exclusions.csv",
            "membership.csv": "prices/membership.csv",
        }
        generation_comparison: dict[str, Any] = {}
        for short_name, manifest_name in names.items():
            old = old_files.get(manifest_name)
            new_check = next((item for item in checks if item["file"] == short_name), None)
            generation_comparison[short_name] = {
                "acquisition_generation_sha256": old.get("sha256") if old else None,
                "alternate_generation_sha256": (
                    new_check["actual_sha256"] if new_check else None
                ),
                "same_identity": (
                    old.get("sha256") == new_check["actual_sha256"]
                    if old and new_check
                    else None
                ),
                "acquisition_generation_bytes": old.get("bytes") if old else None,
                "alternate_generation_bytes": new_check.get("bytes") if new_check else None,
            }
        old_source_dir = comparison_manifest_path.parent / "fundamentals"
        old_provenance_path = old_source_dir / "fundamentals_provenance.json"
        old_provenance: dict[str, Any] | None = None
        if old_provenance_path.is_file():
            old_provenance = read_json(old_provenance_path)
        old_file_checks: dict[str, Any] = {}
        for filename in (
            "fundamentals.csv",
            "fundamentals_audit.csv",
            "fundamentals_coverage.csv",
            "fundamentals_coverage.json",
            "fundamentals_provenance.json",
            "security_master.csv",
            "security_master_exclusions.csv",
        ):
            path = old_source_dir / filename
            manifest_name = f"fundamentals/{filename}"
            declared = old_files.get(manifest_name)
            if path.is_file():
                actual = sha256_file(path)
                old_file_checks[filename] = {
                    "bytes": path.stat().st_size,
                    "actual_sha256": actual,
                    "import_manifest_sha256": declared.get("sha256") if declared else None,
                    "matches_import_manifest": (
                        actual == declared.get("sha256") if declared else None
                    ),
                    "bound_by_original_provenance": (
                        declared.get("bound_by_original_provenance") if declared else None
                    ),
                }
            else:
                old_file_checks[filename] = {"present": False}
        comparison = {
            "acquisition_import_manifest": str(comparison_manifest_path),
            "admission_status": old_manifest.get("admission_status"),
            "imported_at_utc": old_manifest.get("imported_at_utc"),
            "file_generations": generation_comparison,
            "retained_acquisition_files": old_file_checks,
            "acquisition_fundamental_row_count_from_retained_provenance": (
                old_provenance.get("fundamental_row_count") if old_provenance else None
            ),
            "acquisition_declared_audit_coverage_hashes": (
                {
                    "fundamentals_audit_sha256": old_provenance.get(
                        "fundamentals_audit_sha256"
                    ),
                    "fundamentals_coverage_sha256": old_provenance.get(
                        "fundamentals_coverage_sha256"
                    ),
                }
                if old_provenance
                else None
            ),
        }

    return (
        {
            "source_directory": str(source_dir),
            "source": provenance.get("source"),
            "source_window": {
                "start_date": provenance.get("start_date"),
                "end_date": provenance.get("end_date"),
            },
            "declared_fundamental_row_count": provenance.get("fundamental_row_count"),
            "declared_security_master_row_count": provenance.get(
                "security_master_row_count"
            ),
            "declared_security_master_exclusion_row_count": provenance.get(
                "security_master_exclusion_row_count"
            ),
            "source_membership_sha256": provenance.get("membership_csv_sha256"),
            "fundamentals_provenance_sha256": actual_provenance_hash,
            "verified_inputs": checks,
            "archive_digest_reuse": archive_digest_reuse,
            "publication_marker": publication_marker,
            "public_date_rule": provenance.get("public_date_rule"),
            "annual_duration_days": provenance.get("annual_duration_days"),
            "quarterly_duration_days": provenance.get("quarterly_duration_days"),
            "filed_date_fallback_count_declared": provenance.get(
                "filed_date_fallback_count"
            ),
            "institutional_fields_policy": provenance.get("institutional_fields"),
            "identity_manifest_expected_sha256": provenance.get(
                "identity_manifest_csv_sha256"
            ),
            "identity_manifest_csv_present_in_source_directory": (
                source_dir / "identity_manifest.csv"
            ).is_file(),
        },
        provenance,
        comparison,
    )


def read_security_master(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    rows = list(read_csv_rows(path))
    rows_by_ticker: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        rows_by_ticker[row["ticker"]].append(row)
    by_ticker = {ticker: ticker_rows[-1] for ticker, ticker_rows in rows_by_ticker.items()}
    repeated_ticker_rows = {
        ticker: [
            {
                "cik": row.get("cik"),
                "company_name": row.get("company_name"),
                "first_membership_date": row.get("first_membership_date"),
                "last_membership_date": row.get("last_membership_date"),
                "mapping_basis": row.get("mapping_basis"),
            }
            for row in ticker_rows
        ]
        for ticker, ticker_rows in rows_by_ticker.items()
        if len(ticker_rows) > 1
    }
    return by_ticker, {
        "row_count": len(rows),
        "unique_ticker_count": len(by_ticker),
        "repeated_ticker_row_count": sum(
            len(ticker_rows) - 1 for ticker_rows in rows_by_ticker.values()
        ),
        "repeated_ticker_interval_examples": repeated_ticker_rows,
        "unique_cik_count": len({row.get("cik") for row in rows if row.get("cik")}),
        "rows_missing_cik": sum(not row.get("cik") for row in rows),
        "mapping_basis_counts": dict(Counter(row.get("mapping_basis", "") for row in rows)),
        "foreign_or_country_status_column_present": any(
            key.lower() in {"country", "domicile", "form_family"}
            for row in rows[:1]
            for key in row
        ),
    }


def read_membership(path: Path) -> dict[str, list[tuple[date, bool]]]:
    events: dict[str, list[tuple[date, bool]]] = defaultdict(list)
    for row in read_csv_rows(path):
        event_date = parse_date(row.get("effective_date"))
        if event_date is None:
            continue
        events[row["ticker"]].append(
            (event_date, row.get("member", "").strip().lower() in {"1", "true", "yes"})
        )
    for ticker in events:
        events[ticker].sort(key=lambda item: item[0])
    return events


def member_set_at(
    events: dict[str, list[tuple[date, bool]]], snapshot_date: date
) -> set[str]:
    current: set[str] = set()
    for ticker, ticker_events in events.items():
        eligible = [event for event in ticker_events if event[0] <= snapshot_date]
        if eligible and eligible[-1][1]:
            current.add(ticker)
    return current


def next_session(source_date: date, trading_days: list[date]) -> date | None:
    for day in trading_days:
        if day > source_date:
            return day
    return None


def analyze_export(
    source_dir: Path,
    membership_path: Path,
    trading_days_path: Path,
    security_master: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    field_rows = Counter()
    field_tickers: dict[str, set[str]] = defaultdict(set)
    field_statement_rows: dict[str, Counter[str]] = defaultdict(Counter)
    row_count = 0
    period_public_date_pairs = 0
    public_date_not_after_period_end = 0
    ticker_set: set[str] = set()
    statement_rows: Counter[str] = Counter()
    period_dates: list[date] = []
    public_dates: list[date] = []
    version_rows: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sample_export_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in read_csv_rows(source_dir / "fundamentals.csv"):
        row_count += 1
        ticker = row["ticker"]
        ticker_set.add(ticker)
        statement = row.get("statement_type", "")
        statement_rows[statement] += 1
        period = parse_date(row.get("period_end"))
        public = parse_date(row.get("public_date"))
        if period:
            period_dates.append(period)
        if public:
            public_dates.append(public)
        if period and public:
            period_public_date_pairs += 1
            if public <= period:
                public_date_not_after_period_end += 1
        version_rows[(ticker, statement, row.get("period_end", ""))].append(
            row.get("public_date", "")
        )
        converted = {
            "ticker": ticker,
            "statement_type": statement,
            "period_end": period,
            "public_date": public,
            "raw_period_end": row.get("period_end", ""),
            "raw_public_date": row.get("public_date", ""),
            "values": {},
        }
        for field in METRIC_FIELDS:
            value = parse_number(row.get(field))
            converted["values"][field] = value
            if value is not None:
                field_rows[field] += 1
                field_tickers[field].add(ticker)
                field_statement_rows[field][statement] += 1
        rows_by_ticker[ticker].append(converted)
        if ticker in SAMPLE_TICKERS:
            sample_export_rows[ticker].append(
                {
                    "ticker": ticker,
                    "statement_type": statement,
                    "period_end": row.get("period_end", ""),
                    "public_date": row.get("public_date", ""),
                    "values": dict(converted["values"]),
                }
            )

    trading_days = sorted(
        day
        for row in read_csv_rows(trading_days_path)
        if (day := parse_date(row.get("trade_date"))) is not None
    )
    trading_day_set = set(trading_days)

    audit_summary, audit_data = analyze_audit(
        source_dir / "fundamentals_audit.csv",
        source_dir / "fundamentals.csv",
        SAMPLE_TICKERS,
    )
    audit_timing = Counter()
    lag_days: list[int] = []
    timing_mismatches = 0
    timing_missing_or_invalid = 0
    for row in read_csv_rows(source_dir / "fundamentals_audit.csv"):
        public = parse_date(row.get("public_date"))
        basis = row.get("public_date_basis", "")
        if basis == "acceptance_datetime":
            source_day = parse_date(row.get("acceptance_datetime"))
        elif basis == "filed_date_fallback":
            source_day = parse_date(row.get("filed_date"))
        else:
            source_day = None
        audit_timing[basis] += 1
        if public and source_day:
            expected = next_session(source_day, trading_days)
            if expected == public and public in trading_day_set:
                lag_days.append((public - source_day).days)
            else:
                timing_mismatches += 1
        else:
            timing_missing_or_invalid += 1

    snapshots: dict[str, Any] = {}
    membership_events = read_membership(membership_path)
    for label in SNAPSHOT_DATES:
        as_of = date.fromisoformat(label)
        members = member_set_at(membership_events, as_of)
        snapshot_data: dict[str, dict[tuple[str, date], list[dict[str, Any]]]] = {}
        latest_rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
        members_with_any = 0
        unresolved = sorted(ticker for ticker in members if ticker not in security_master)
        for ticker in sorted(members):
            selected: dict[tuple[str, date], tuple[date, list[dict[str, Any]]]] = {}
            for row in rows_by_ticker.get(ticker, []):
                public = row["public_date"]
                period = row["period_end"]
                if public is None or period is None or public > as_of:
                    continue
                key = (row["statement_type"], period)
                previous = selected.get(key)
                if previous is None or public > previous[0]:
                    selected[key] = (public, [row])
                elif public == previous[0]:
                    previous[1].append(row)
            selected_rows = [row for _, tied in selected.values() for row in tied]
            latest_rows_by_ticker[ticker] = selected_rows
            snapshot_data[ticker] = {
                key: tied for key, (_, tied) in selected.items()
            }
            if selected_rows:
                members_with_any += 1

        profile_results: dict[str, Any] = {}
        per_security_sample: dict[str, dict[str, Any]] = {
            ticker: {
                "asof_export_period_record_count": len(latest_rows_by_ticker.get(ticker, [])),
                "profile_coverage": {},
            }
            for ticker in SAMPLE_TICKERS
            if ticker in members
        }
        for statement, field, required_slots in GROWTH_PROFILES:
            slot_histogram = Counter()
            ready_tickers = 0
            matched_pair_total = 0
            nonempty_tickers = 0
            available_observations = 0
            for ticker in sorted(members):
                period_values: list[tuple[date, list[float | None]]] = []
                for (row_statement, period), tied_rows in snapshot_data[ticker].items():
                    if row_statement != statement:
                        continue
                    period_values.append(
                        (period, [row["values"].get(field) for row in tied_rows])
                    )
                if not period_values:
                    slot_histogram[0] += 1
                    if ticker in per_security_sample:
                        per_security_sample[ticker]["profile_coverage"][
                            f"{statement}.{field}"
                        ] = {
                            "current_value_observation_count": 0,
                            "period_observation_count": 0,
                            "eligible_yoy_pair_count_in_recent_slots": 0,
                            "required_yoy_slots": required_slots,
                            "ready_for_full_lookback": False,
                        }
                    continue
                current_values = [
                    value for _, values in period_values for value in values if value is not None
                ]
                if current_values:
                    nonempty_tickers += 1
                    available_observations += len(current_values)
                if statement == "quarterly":
                    series_values = []
                    for _, values in period_values:
                        if not values or not any(value is not None for value in values):
                            series_values.append(None)
                        elif all(value == values[0] for value in values):
                            series_values.append(values[0])
                        else:
                            series_values.append(None)
                    series_dates = [pd.Timestamp(period) for period, _ in period_values]
                    matches = match_fiscal_year_over_year_periods(
                        pd.Series(series_values, index=series_dates),
                        tolerance_days=FISCAL_YOY_TOLERANCE_DAYS,
                    )
                    recent_slots = matches[:required_slots]
                    eligible_slots = sum(
                        1
                        for item in recent_slots
                        if item.matched
                        and parse_number(str(item.current_value)) is not None
                        and parse_number(str(item.prior_value)) is not None
                        and float(item.prior_value) > 0
                    )
                else:
                    # The accepted annual policy uses adjacent available annual
                    # observations (after missing values are dropped), not the
                    # quarterly calendar-year ±28-day matcher.
                    annual_values: list[tuple[date, float]] = []
                    for period, values in period_values:
                        present_values = [value for value in values if value is not None]
                        if present_values and all(value == present_values[0] for value in present_values):
                            annual_values.append((period, present_values[0]))
                    annual_values.sort(key=lambda item: item[0], reverse=True)
                    recent_slots = list(
                        zip(annual_values, annual_values[1:], strict=False)
                    )[:required_slots]
                    eligible_slots = sum(
                        1
                        for (current_period, current), (prior_period, prior) in recent_slots
                        if current_period > prior_period
                        and math.isfinite(current)
                        and math.isfinite(prior)
                        and prior > 0
                    )
                slot_histogram[eligible_slots] += 1
                matched_pair_total += eligible_slots
                if len(recent_slots) == required_slots and eligible_slots == required_slots:
                    ready_tickers += 1
                if ticker in per_security_sample:
                    per_security_sample[ticker]["profile_coverage"][
                        f"{statement}.{field}"
                        ] = {
                        "current_value_observation_count": len(current_values),
                        "period_observation_count": len(period_values),
                        "eligible_yoy_pair_count_in_recent_slots": eligible_slots,
                        "required_yoy_slots": required_slots,
                        "ready_for_full_lookback": (
                            len(recent_slots) == required_slots
                            and eligible_slots == required_slots
                        ),
                    }
            profile_key = f"{statement}.{field}"
            profile_results[profile_key] = {
                "required_yoy_slots": required_slots,
                "members_with_any_current_value": nonempty_tickers,
                "current_observation_count_within_member_asof_rows": available_observations,
                "eligible_yoy_pair_count_in_recent_slots": matched_pair_total,
                "members_by_eligible_slot_count": {
                    str(key): slot_histogram[key] for key in range(required_slots + 1)
                },
                "members_ready_for_full_lookback": ready_tickers,
            }

        acceleration_by_field: dict[str, int] = {}
        for field in ("basic_eps", "diluted_eps", "total_revenue"):
            acceleration_candidates = 0
            for ticker in sorted(members):
                values, indexes = [], []
                for (row_statement, period), tied_rows in snapshot_data[ticker].items():
                    if row_statement == "quarterly":
                        field_values = [row["values"].get(field) for row in tied_rows]
                        if not field_values or not any(value is not None for value in field_values):
                            field_value = None
                        elif all(value == field_values[0] for value in field_values):
                            field_value = field_values[0]
                        else:
                            field_value = None
                        indexes.append(pd.Timestamp(period))
                        values.append(field_value)
                if not indexes:
                    continue
                matches = match_fiscal_year_over_year_periods(
                    pd.Series(values, index=indexes),
                    tolerance_days=FISCAL_YOY_TOLERANCE_DAYS,
                )
                if len(matches) < 2 or not matches[0].matched or not matches[1].matched:
                    continue
                if (
                    parse_number(str(matches[0].current_value)) is None
                    or parse_number(str(matches[1].current_value)) is None
                    or parse_number(str(matches[0].prior_value)) is None
                    or parse_number(str(matches[1].prior_value)) is None
                    or float(matches[0].prior_value) <= 0
                    or float(matches[1].prior_value) <= 0
                ):
                    continue
                gap = (matches[0].current_period - matches[1].current_period).days
                if 84 <= gap <= 105:
                    acceleration_candidates += 1
            acceleration_by_field[field] = acceleration_candidates

        roe_candidates = 0
        positive_equity = 0
        latest_annual_net_income = 0
        share_value_members = 0
        for ticker in sorted(members):
            available_annual = [
                row
                for row in latest_rows_by_ticker[ticker]
                if row["statement_type"] == "annual"
            ]
            annual_ni = [
                row for row in available_annual if row["values"].get("net_income") is not None
            ]
            annual_equity = [
                row
                for row in latest_rows_by_ticker[ticker]
                if row["statement_type"] == "balance"
                and row["values"].get("total_stockholders_equity") is not None
            ]
            if annual_ni:
                latest_annual_net_income += 1
            if annual_equity:
                latest_equity = max(annual_equity, key=lambda row: row["period_end"])
                if latest_equity["values"]["total_stockholders_equity"] > 0:
                    positive_equity += 1
                    if annual_ni:
                        roe_candidates += 1
            if any(
                row["values"].get("shares_outstanding") is not None
                for row in latest_rows_by_ticker[ticker]
            ):
                share_value_members += 1

        snapshots[label] = {
            "membership_count": len(members),
            "members_with_any_exported_fact_asof": members_with_any,
            "members_without_security_master_row": len(unresolved),
            "unresolved_member_examples": unresolved[:20],
            "asof_statement_period_records": sum(
                len(rows) for rows in latest_rows_by_ticker.values()
            ),
            "profile_coverage": profile_results,
            "quarterly_revenue_v3_adjacent_acceleration_candidates": {
                "members_ready_for_latest_two_matched_growth_slots_with_84_to_105_day_gap_by_field": acceleration_by_field,
                "cadence_days_inclusive": [84, 105],
                "calculator_identity": "pit-financial-features-v3",
            },
            "roe_input_availability": {
                "members_with_latest_annual_net_income": latest_annual_net_income,
                "members_with_positive_latest_nonmissing_balance_equity_and_annual_net_income": roe_candidates,
                "members_with_positive_latest_nonmissing_balance_equity": positive_equity,
                "definition": "availability only; latest nonmissing annual net income and latest nonmissing balance-sheet equity by period_end, as visible at the snapshot; no ROE score is calculated",
            },
            "members_with_any_shares_outstanding_value": share_value_members,
            "sample_security_asof_coverage": per_security_sample,
        }

    distinct_vintage_keys = sum(
        len(set(public_values)) > 1 for public_values in version_rows.values()
    )
    exact_duplicate_public_version_keys = sum(
        len(public_values) > len(set(public_values)) for public_values in version_rows.values()
    )
    export = {
        "row_count": row_count,
        "unique_ticker_count": len(ticker_set),
        "rows_with_period_end_and_public_date": period_public_date_pairs,
        "rows_with_public_date_not_after_period_end": public_date_not_after_period_end,
        "statement_row_counts": dict(statement_rows),
        "period_end_range": [min(period_dates).isoformat(), max(period_dates).isoformat()],
        "public_date_range": [min(public_dates).isoformat(), max(public_dates).isoformat()],
        "field_coverage": {
            field: {
                "nonempty_row_count": field_rows[field],
                "distinct_ticker_count": len(field_tickers[field]),
                "statement_type_nonempty_rows": dict(field_statement_rows[field]),
            }
            for field in METRIC_FIELDS
        },
        "revision_vintage_candidates": {
            "ticker_statement_period_keys_with_multiple_public_dates": distinct_vintage_keys,
            "keys_with_duplicate_rows_at_same_public_date": exact_duplicate_public_version_keys,
            "interpretation": "retained public-date versions; audit can trace filings, but row multiplicity alone is not labeled a confirmed amendment",
        },
    }
    security_master_rows, security_master_summary = read_security_master(
        source_dir / "security_master.csv"
    )
    exclusion_rows = list(read_csv_rows(source_dir / "security_master_exclusions.csv"))
    coverage_manifest = read_json(source_dir / "fundamentals_coverage.json")
    master_tickers = set(security_master_rows)
    excluded_tickers = {row.get("ticker", "") for row in exclusion_rows}
    membership_tickers = set(membership_events)
    summary = {
        "snapshot_coverage": snapshots,
        "security_master": security_master_summary,
        "security_master_exclusion_row_count": len(exclusion_rows),
        "security_master_exclusion_reason_counts": dict(
            Counter(row.get("reason", "") for row in exclusion_rows)
        ),
        "security_master_membership_accounting": {
            "membership_union_ticker_count_from_events": len(membership_tickers),
            "unique_security_master_ticker_count": len(master_tickers),
            "unique_exclusion_ticker_count": len(excluded_tickers),
            "security_master_exclusion_ticker_intersection_count": len(
                master_tickers & excluded_tickers
            ),
            "membership_tickers_without_master_or_exclusion": len(
                membership_tickers - (master_tickers | excluded_tickers)
            ),
            "master_or_exclusion_tickers_outside_membership": len(
                (master_tickers | excluded_tickers) - membership_tickers
            ),
            "coverage_manifest_resolved_count": coverage_manifest.get(
                "resolved_symbol_count"
            ),
            "coverage_manifest_exclusion_count": coverage_manifest.get(
                "explicitly_excluded_symbol_count"
            ),
            "coverage_manifest_membership_union_count": coverage_manifest.get(
                "membership_union_symbol_count"
            ),
        },
        "membership_event_row_count": sum(len(rows) for rows in membership_events.values()),
        "membership_union_ticker_count": len(membership_events),
        "export": export,
        "audit": audit_summary,
        "publication_timing": {
            "public_date_basis_row_counts": dict(audit_timing),
            "rows_matching_first_spy_session_strictly_after_acceptance_or_filed_day": len(
                lag_days
            ),
            "rows_checked": sum(audit_timing.values()),
            "rows_with_unreproduced_normalization": timing_mismatches,
            "rows_with_missing_or_invalid_timing": timing_missing_or_invalid,
            "calendar_day_span_to_first_supplied_session": {
                "min": min(lag_days) if lag_days else None,
                "median": statistics.median(lag_days) if lag_days else None,
                "max": max(lag_days) if lag_days else None,
            },
            "rule": "public_date is already the first supplied SPY session strictly after the acceptance calendar date, with filed-date fallback only; snapshot code does not shift it again",
        },
        "coverage_manifest": coverage_manifest,
        "sample_symbols_present": [
            ticker for ticker in SAMPLE_TICKERS if ticker in security_master_rows
        ],
        "sample_security_master": {
            ticker: security_master_rows[ticker]
            for ticker in SAMPLE_TICKERS
            if ticker in security_master_rows
        },
    }
    return summary, sample_export_rows, audit_data


def analyze_audit(
    path: Path, export_path: Path, sample_tickers: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
    row_count = 0
    tickers: set[str] = set()
    accession_ids: set[str] = set()
    forms: Counter[str] = Counter()
    fiscal_periods: Counter[str] = Counter()
    fiscal_years: set[str] = set()
    date_bases: Counter[str] = Counter()
    source_concepts: dict[str, Counter[str]] = defaultdict(Counter)
    inherited_fields: Counter[str] = Counter()
    missing = Counter()
    q4_tag_row_count_any_statement = 0
    direct_q4_quarterly_row_count = 0
    direct_q4_forms: Counter[str] = Counter()
    direct_q4_statement_types: Counter[str] = Counter()
    direct_q4_export_metric_values: Counter[str] = Counter()
    direct_q4_quarterly_examples: list[dict[str, Any]] = []
    export_audit_exact_pairs = 0
    export_audit_key_mismatches = 0
    trailing_export_rows_without_audit = 0
    key_mismatch_examples: list[dict[str, Any]] = []
    export_iter = iter(read_csv_rows(export_path))
    source_year_presence: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    source_year_concepts: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    sample_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    date_min: date | None = None
    date_max: date | None = None

    for row in read_csv_rows(path):
        row_count += 1
        export_row = next(export_iter, None)
        audit_key = tuple(
            row.get(key, "")
            for key in ("ticker", "statement_type", "period_end", "public_date")
        )
        export_key = (
            tuple(
                export_row.get(key, "")
                for key in ("ticker", "statement_type", "period_end", "public_date")
            )
            if export_row is not None
            else None
        )
        exact_pair = audit_key == export_key
        if exact_pair:
            export_audit_exact_pairs += 1
        else:
            export_audit_key_mismatches += 1
            if len(key_mismatch_examples) < 10:
                key_mismatch_examples.append(
                    {"audit_key": audit_key, "export_key": export_key}
                )
        ticker = row.get("ticker", "")
        tickers.add(ticker)
        if row.get("accession_number"):
            accession_ids.add(row["accession_number"])
        if not row.get("form"):
            missing["form"] += 1
        forms[row.get("form", "") or "<missing>"] += 1
        fp = row.get("fiscal_period", "")
        fiscal_periods[fp or "<missing>"] += 1
        if row.get("fiscal_year"):
            fiscal_years.add(row["fiscal_year"])
        basis = row.get("public_date_basis", "")
        date_bases[basis or "<missing>"] += 1
        for column in (
            "accession_number",
            "filed_date",
            "acceptance_datetime",
            "source_concepts",
        ):
            if not row.get(column):
                missing[column] += 1
        filed = parse_date(row.get("filed_date"))
        if filed:
            date_min = filed if date_min is None else min(date_min, filed)
            date_max = filed if date_max is None else max(date_max, filed)

        concepts = parse_json_object(row.get("source_concepts"))
        metric_sources = parse_json_object(row.get("metric_sources"))
        inherited = parse_string_set(row.get("inherited_metrics"))
        exported_nonempty_metrics = {
            field
            for field in METRIC_FIELDS
            if export_row is not None and parse_number(export_row.get(field)) is not None
        }
        for field, concept in concepts.items():
            source_concepts[field][str(concept)] += 1
            if field in inherited:
                inherited_fields[field] += 1
        if fp.upper() == "Q4":
            q4_tag_row_count_any_statement += 1
            direct_q4_forms[row.get("form", "") or "<missing>"] += 1
            direct_q4_statement_types[row.get("statement_type", "") or "<missing>"] += 1
            if exact_pair and row.get("statement_type") == "quarterly":
                direct_q4_quarterly_row_count += 1
                direct_fields = sorted(
                    exported_nonempty_metrics & set(Q4_CAPABLE_FIELDS)
                )
                for field in direct_fields:
                    if field in metric_sources and field not in inherited:
                        direct_q4_export_metric_values[field] += 1
                if len(direct_q4_quarterly_examples) < 10:
                    direct_q4_quarterly_examples.append(
                        {
                            "ticker": ticker,
                            "fiscal_year": row.get("fiscal_year", ""),
                            "period_end": row.get("period_end", ""),
                            "form": row.get("form", ""),
                            "nonempty_metric_source_fields": [
                                field
                                for field in direct_fields
                                if field in metric_sources and field not in inherited
                            ],
                            "source_accession_present": bool(row.get("accession_number")),
                            "raw_numeric_values_written": False,
                        }
                    )
        if ticker in sample_tickers:
            sample_rows[ticker].append(
                {
                    key: row.get(key, "")
                    for key in (
                        "ticker",
                        "statement_type",
                        "period_end",
                        "public_date",
                        "accession_number",
                        "form",
                        "filed_date",
                        "fiscal_year",
                        "fiscal_period",
                        "acceptance_datetime",
                        "public_date_basis",
                        "inherited_metrics",
                        "metric_sources",
                    )
                }
            )

        statement = row.get("statement_type", "")
        normalized_fp = fp.upper()
        supported_fiscal_period = (
            (normalized_fp == "FY" and statement == "annual")
            or (normalized_fp in {"Q1", "Q2", "Q3", "Q4"} and statement == "quarterly")
        )
        if exact_pair and supported_fiscal_period and row.get("fiscal_year"):
            for field, concept in concepts.items():
                if (
                    field in Q4_CAPABLE_FIELDS
                    and field not in inherited
                    and field in metric_sources
                    and field in exported_nonempty_metrics
                ):
                    source_year_presence[(ticker, row["fiscal_year"], field)].add(normalized_fp)
                    source_year_concepts[(ticker, row["fiscal_year"], field, normalized_fp)].add(
                        str(concept)
                    )

    trailing_export_rows_without_audit = sum(1 for _ in export_iter)

    arithmetic_candidates: Counter[str] = Counter()
    arithmetic_candidate_groups: dict[str, list[str]] = defaultdict(list)
    direct_groups: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for (ticker, fiscal_year, field), periods in source_year_presence.items():
        concepts_by_period = {
            period: source_year_concepts[(ticker, fiscal_year, field, period)]
            for period in periods
        }
        if "Q4" in periods:
            direct_groups[field].add((ticker, fiscal_year))
        if {"FY", "Q1", "Q2", "Q3"}.issubset(periods):
            all_concepts = set().union(
                *(concepts_by_period[period] for period in ("FY", "Q1", "Q2", "Q3"))
            )
            each_has_one = all(
                len(concepts_by_period[period]) == 1
                for period in ("FY", "Q1", "Q2", "Q3")
            )
            if each_has_one and len(all_concepts) == 1:
                arithmetic_candidates[field] += 1
                if len(arithmetic_candidate_groups[field]) < 10:
                    arithmetic_candidate_groups[field].append(f"{ticker}:{fiscal_year}")

    summary = {
        "row_count": row_count,
        "unique_ticker_count": len(tickers),
        "unique_accession_count": len(accession_ids),
        "filed_date_range": [date_min.isoformat(), date_max.isoformat()],
        "forms": dict(forms),
        "recognized_domestic_form_rows": {
            form: forms.get(form, 0) for form in ("10-K", "10-K/A", "10-Q", "10-Q/A")
        },
        "not_emitted_form_rows": {
            form: forms.get(form, 0)
            for form in ("8-K", "10-KT", "10-QT", "20-F", "6-K")
        },
        "fiscal_periods": dict(fiscal_periods),
        "fiscal_year_range": [min(fiscal_years), max(fiscal_years)] if fiscal_years else [],
        "public_date_basis_counts": dict(date_bases),
        "missing_audit_field_counts": dict(missing),
        "source_concept_occurrence_counts_by_metric": {
            field: dict(counts) for field, counts in source_concepts.items()
        },
        "inherited_metric_occurrence_counts": dict(inherited_fields),
        "q4_evidence_in_retained_audit": {
            "rows_with_fiscal_period_q4_tag_any_statement_type": q4_tag_row_count_any_statement,
            "q4_rows_by_form": dict(direct_q4_forms),
            "q4_rows_by_statement_type": dict(direct_q4_statement_types),
            "rows_with_fiscal_period_q4_tag_and_quarterly_export_statement": direct_q4_quarterly_row_count,
            "quarterly_q4_row_examples": direct_q4_quarterly_examples,
            "q4_exported_nonempty_metric_source_values": dict(
                direct_q4_export_metric_values
            ),
            "symbol_years_with_explicit_q4_metric_concept": {
                field: len(groups) for field, groups in direct_groups.items()
            },
            "symbol_years_with_fy_q1_q2_q3_same_single_concept": dict(
                arithmetic_candidates
            ),
            "concept_consistent_candidate_examples": dict(arithmetic_candidate_groups),
            "arithmetic_values_emitted": False,
            "limitation": "candidate count uses audit fiscal-period/concept metadata; source units, currency, and accounting-basis reconciliation are not proved by this audit summary",
        },
        "export_audit_exact_row_join": {
            "exact_ticker_statement_period_public_date_pairs_in_same_order": export_audit_exact_pairs,
            "rows_checked_from_audit": row_count,
            "key_mismatch_or_export_missing_count": export_audit_key_mismatches,
            "trailing_export_rows_without_audit_count": trailing_export_rows_without_audit,
            "key_mismatch_examples": key_mismatch_examples,
        },
    }
    return summary, sample_rows


def parse_submission_member(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    recent = payload.get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    indexed: dict[str, dict[str, str]] = {}
    for index, accession in enumerate(accessions):
        item = {
            key: str(values[index])
            for key, values in recent.items()
            if isinstance(values, list) and index < len(values)
        }
        indexed[str(accession)] = item
    return indexed


def iso_timestamp(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def analyze_bounded_archives(
    source_dir: Path,
    master: dict[str, dict[str, str]],
    audit_samples: dict[str, list[dict[str, str]]],
    sample_export_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    samples: dict[str, Any] = {}
    with zipfile.ZipFile(source_dir / "companyfacts.zip") as facts_zip, zipfile.ZipFile(
        source_dir / "submissions.zip"
    ) as submissions_zip:
        for ticker in SAMPLE_TICKERS:
            if ticker not in master:
                continue
            cik = str(master[ticker]["cik"]).zfill(10)
            member_name = f"CIK{cik}.json"
            facts_info = facts_zip.getinfo(member_name)
            submissions_info = submissions_zip.getinfo(member_name)
            max_member_bytes = 64 * 1024 * 1024
            if max(facts_info.file_size, submissions_info.file_size) > max_member_bytes:
                raise ValueError(f"Bounded sample member exceeds 64 MiB: {member_name}")
            facts_bytes = facts_zip.read(member_name)
            submissions_bytes = submissions_zip.read(member_name)
            facts = json.loads(facts_bytes)
            submissions = json.loads(submissions_bytes)
            submissions_by_accession = parse_submission_member(submissions)

            sample_period_start = date(2020, 1, 1)
            sample_period_end = date(2025, 12, 31)
            form_counts: Counter[str] = Counter()
            fact_observations = 0
            explicit_q4_fiscal_period_records = 0
            calendar_q4_frame_records = 0
            ten_k_quarter_duration_records = 0
            duration_period_counts: Counter[str] = Counter()
            fact_index: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for _metric, concepts in FACT_CONCEPTS.items():
                for namespace, concept in concepts:
                    item = facts.get("facts", {}).get(namespace, {}).get(concept, {})
                    for unit, unit_records in item.get("units", {}).items():
                        for observation in unit_records:
                            origin_key = (
                                str(observation.get("accn", "") or ""),
                                f"{namespace}:{concept}",
                                str(observation.get("form", "") or ""),
                                str(observation.get("filed", "") or ""),
                                str(observation.get("fy", "") or ""),
                                str(observation.get("fp", "") or ""),
                            )
                            fact_index[origin_key].append(
                                {
                                    "unit": unit,
                                    "value": parse_number(str(observation.get("val", ""))),
                                    "period_end": str(observation.get("end", "") or ""),
                                    "period_start": str(observation.get("start", "") or ""),
                                }
                            )
                            end = parse_date(observation.get("end"))
                            if end is None or not sample_period_start <= end <= sample_period_end:
                                continue
                            form = str(observation.get("form", ""))
                            form_counts[form or "<missing>"] += 1
                            fact_observations += 1
                            fp = str(observation.get("fp", "")).upper()
                            frame = str(observation.get("frame", "")).upper()
                            if fp == "Q4":
                                explicit_q4_fiscal_period_records += 1
                            if "Q4" in frame:
                                calendar_q4_frame_records += 1
                            start = parse_date(observation.get("start"))
                            if start:
                                duration = (end - start).days
                                if 70 <= duration <= 115:
                                    duration_period_counts["quarterly_duration"] += 1
                                    if form.startswith("10-K"):
                                        ten_k_quarter_duration_records += 1
                                if 300 <= duration <= 430:
                                    duration_period_counts["annual_duration"] += 1

            recent = submissions.get("filings", {}).get("recent", {})
            recent_count = len(recent.get("accessionNumber", []))
            recent_form_counts: Counter[str] = Counter()
            recent_window_accessions: dict[str, dict[str, str]] = {}
            for accession, record in submissions_by_accession.items():
                filed = parse_date(record.get("filingDate"))
                if filed is None or not sample_period_start <= filed <= sample_period_end:
                    continue
                form = record.get("form", "")
                recent_form_counts[form or "<missing>"] += 1
                recent_window_accessions[accession] = record

            audit_rows = sorted(
                audit_samples.get(ticker, []),
                key=lambda row: (row.get("public_date", ""), row.get("period_end", "")),
                reverse=True,
            )
            unique_audit_accessions: dict[str, dict[str, str]] = {}
            for row in audit_rows:
                accession = row.get("accession_number", "")
                if accession:
                    unique_audit_accessions.setdefault(accession, row)
            chosen_accessions = list(unique_audit_accessions.items())[:25]
            found = 0
            form_matches = 0
            filed_matches = 0
            acceptance_date_matches = 0
            for accession, audit in chosen_accessions:
                source = submissions_by_accession.get(accession)
                if source is None:
                    continue
                found += 1
                if source.get("form") == audit.get("form"):
                    form_matches += 1
                if source.get("filingDate") == audit.get("filed_date"):
                    filed_matches += 1
                source_acceptance = (
                    source.get("acceptanceDateTime")
                    or source.get("acceptanceDate")
                    or ""
                )
                audit_acceptance = audit.get("acceptance_datetime", "")
                if source_acceptance and audit_acceptance:
                    source_text = iso_timestamp(source_acceptance)
                    audit_text = iso_timestamp(audit_acceptance)
                    if source_text == audit_text or source_text[:10] == audit_text[:10]:
                        acceptance_date_matches += 1

            def audit_key(row: dict[str, str]) -> tuple[str, str, str, str]:
                return (
                    row.get("ticker", ""),
                    row.get("statement_type", ""),
                    row.get("period_end", ""),
                    row.get("public_date", ""),
                )

            export_by_key = {
                (
                    row["ticker"],
                    row["statement_type"],
                    row["period_end"],
                    row["public_date"],
                ): row
                for row in sample_export_rows.get(ticker, [])
            }
            audit_by_key = {audit_key(row): row for row in audit_samples.get(ticker, [])}
            exact_keys = sorted(set(export_by_key) & set(audit_by_key))
            trace_by_metric: dict[str, Counter[str]] = defaultdict(Counter)
            matched_units: dict[str, Counter[str]] = defaultdict(Counter)
            trace_examples: list[dict[str, Any]] = []
            for key in exact_keys:
                export_row = export_by_key[key]
                audit_row = audit_by_key[key]
                origins = parse_json_object(audit_row.get("metric_sources"))
                inherited = parse_string_set(audit_row.get("inherited_metrics"))
                for metric in METRIC_FIELDS:
                    csv_value = export_row["values"].get(metric)
                    if csv_value is None:
                        continue
                    trace_by_metric[metric]["exported_nonempty_values"] += 1
                    if metric in inherited:
                        trace_by_metric[metric]["inherited_source_links"] += 1
                    origin = origins.get(metric)
                    if not isinstance(origin, dict):
                        trace_by_metric[metric]["no_metric_source_link"] += 1
                        continue
                    trace_by_metric[metric]["metric_source_links"] += 1
                    origin_key = (
                        str(origin.get("accession_number", "") or ""),
                        str(origin.get("source_concept", "") or ""),
                        str(origin.get("form", "") or ""),
                        str(origin.get("filed_date", "") or ""),
                        str(origin.get("fiscal_year", "") or ""),
                        str(origin.get("fiscal_period", "") or ""),
                    )
                    candidates = fact_index.get(origin_key, [])
                    trace_by_metric[metric]["origin_key_companyfacts_candidates"] += len(
                        candidates
                    )
                    match = qualify_companyfacts_match(
                        candidates,
                        csv_value=csv_value,
                        metric=metric,
                        statement_type=key[1],
                        export_period_end=key[2],
                    )
                    disposition = match["disposition"]
                    trace_by_metric[metric][f"disposition_{disposition}"] += 1
                    trace_by_metric[metric]["qualifying_candidate_count"] += match[
                        "qualifying_candidate_count"
                    ]
                    matched_candidate = match["matched_candidate"]
                    if matched_candidate is not None:
                        trace_by_metric[metric]["csv_value_found_in_origin_facts"] += 1
                        matched_units[metric][matched_candidate["unit"]] += 1
                    else:
                        trace_by_metric[metric]["csv_value_not_found_in_origin_facts"] += 1
                    if len(trace_examples) < 12:
                        trace_examples.append(
                            {
                                "statement_type": key[1],
                                "export_period_end": key[2],
                                "public_date": key[3],
                                "metric": metric,
                                "source_concept": origin_key[1],
                                "source_form": origin_key[2],
                                "source_filed_date": origin_key[3],
                                "source_accession_present": bool(origin_key[0]),
                                "inherited_metric": metric in inherited,
                                "companyfacts_origin_candidate_count": len(candidates),
                                "qualifying_candidate_count": match[
                                    "qualifying_candidate_count"
                                ],
                                "match_disposition": disposition,
                                "csv_value_found_in_origin_facts": (
                                    matched_candidate is not None
                                ),
                                "matched_unit_names": (
                                    [matched_candidate["unit"]]
                                    if matched_candidate is not None
                                    else []
                                ),
                                "matched_source_period_ends": (
                                    [matched_candidate["period_end"]]
                                    if matched_candidate is not None
                                    else []
                                ),
                            }
                        )

            samples[ticker] = {
                "cik": cik,
                "company_name": master[ticker].get("company_name"),
                "companyfacts_member": {
                    "name": member_name,
                    "uncompressed_bytes": facts_info.file_size,
                    "crc32": f"{facts_info.CRC:08x}",
                    "sha256_of_bounded_member_bytes": sha256_bytes(facts_bytes),
                },
                "submissions_member": {
                    "name": member_name,
                    "uncompressed_bytes": submissions_info.file_size,
                    "crc32": f"{submissions_info.CRC:08x}",
                    "sha256_of_bounded_member_bytes": sha256_bytes(submissions_bytes),
                    "recent_record_count": recent_count,
                },
                "companyfacts_2020_2025": {
                    "selected_concept_observation_count": fact_observations,
                    "form_counts": dict(form_counts),
                    "quarterly_duration_observation_count": duration_period_counts[
                        "quarterly_duration"
                    ],
                    "annual_duration_observation_count": duration_period_counts[
                        "annual_duration"
                    ],
                    "explicit_companyfacts_fiscal_period_q4_observation_count": explicit_q4_fiscal_period_records,
                    "calendar_year_frame_contains_q4_observation_count": calendar_q4_frame_records,
                    "10k_quarter_duration_observation_count": ten_k_quarter_duration_records,
                    "values_written_to_receipt": False,
                },
                "submissions_2020_2025_recent_member": {
                    "form_counts": dict(recent_form_counts),
                    "sampled_audit_accession_count": len(chosen_accessions),
                    "sampled_accessions_found_in_recent_member": found,
                    "form_matches": form_matches,
                    "filed_date_matches": filed_matches,
                    "acceptance_timestamp_or_date_matches": acceptance_date_matches,
                    "fragment_members_opened": 0,
                },
                "exact_export_audit_companyfacts_trace": {
                    "export_row_count_for_symbol": len(export_by_key),
                    "audit_row_count_for_symbol": len(audit_by_key),
                    "exact_ticker_statement_period_public_date_pairs": len(exact_keys),
                    "export_rows_without_exact_audit_pair": len(set(export_by_key) - set(audit_by_key)),
                    "audit_rows_without_exact_export_pair": len(set(audit_by_key) - set(export_by_key)),
                    "metric_source_link_counts": {
                        metric: dict(counts) for metric, counts in trace_by_metric.items()
                    },
                    "matched_companyfacts_units_by_metric": {
                        metric: dict(counts) for metric, counts in matched_units.items()
                    },
                    "sample_origin_trace_examples": trace_examples,
                },
            }
    return {
        "bounded_company_count": len(samples),
        "symbols": samples,
        "scope": "For each of A, AMZN, and MSFT, opened only its single CIK JSON member in each already hash-verified ZIP. No archive-wide extraction or other archive member reads were performed.",
        "foreign_coverage_limit": "This deterministic US issuer sample does not measure foreign issuers. The exporter/master has no country column; form counts are evidence only for the sampled CIKs.",
    }


def make_receipt(args: argparse.Namespace) -> dict[str, Any]:
    source_dir = args.input_dir.resolve()
    membership_path = args.membership_csv.resolve()
    trading_days_path = args.trading_days_csv.resolve()
    comparison_path = (
        args.comparison_import_provenance.resolve()
        if args.comparison_import_provenance
        else None
    )
    identity, provenance, generation_comparison = verify_inputs(
        source_dir,
        membership_path,
        trading_days_path,
        comparison_path,
        args.reuse_archive_digests_from,
    )
    master = {row["ticker"]: row for row in read_csv_rows(source_dir / "security_master.csv")}
    analysis, sample_export_rows, audit_samples = analyze_export(
        source_dir, membership_path, trading_days_path, master
    )
    archive_samples = analyze_bounded_archives(
        source_dir, master, audit_samples, sample_export_rows
    )
    analysis.pop("sample_audit_rows", None)
    analysis.pop("sample_security_master", None)

    comparison_export_rows = None
    if generation_comparison:
        old = generation_comparison["file_generations"].get("fundamentals.csv", {})
        new = next(
            item
            for item in identity["verified_inputs"]
            if item["file"] == "fundamentals.csv"
        )
        old_row_count = generation_comparison.get(
            "acquisition_fundamental_row_count_from_retained_provenance"
        )
        alternate_row_count = provenance.get("fundamental_row_count")
        comparison_export_rows = {
            "acquisition_generation_declared_row_count": old_row_count,
            "alternate_generation_declared_row_count": alternate_row_count,
            "row_count_delta": (
                int(alternate_row_count) - int(old_row_count)
                if old_row_count is not None and alternate_row_count is not None
                else None
            ),
            "fundamentals_sha256s_match": old.get("same_identity"),
            "alternate_generation_csv_bytes": new["bytes"],
        }

    script_path = Path(__file__).resolve()
    membership_snapshots = analysis.pop("snapshot_coverage")
    receipt = {
        "schema_version": 1,
        "assessment_id": "historical-05-issue-70-bounded-retained-source-assessment",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "assessment_scope": "offline retained-export measurement and three-issuer source trace; no provider calls, re-acquisition, extraction, order logic, or strategy-score computation",
        "code_identity": {
            "repository_head": git_output("rev-parse", "HEAD"),
            "script_sha256": sha256_file(script_path),
            "extractor_git_blob": git_output("rev-parse", "HEAD:core/sec_pit_fundamentals.py"),
            "bundle_builder_git_blob": git_output("rev-parse", "HEAD:build_pit_bundle.py"),
            "fiscal_matcher_git_blob": git_output("rev-parse", "HEAD:core/canslim/fiscal_periods.py"),
            "feature_calculator_git_blob": git_output("rev-parse", "HEAD:core/pit_feature_snapshot.py"),
            "financial_calculator_identity": "pit-financial-features-v3",
            "final_issue_71_calculator_blob": "71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a",
        },
        "runtime": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "platform": platform.platform(),
            "timezone": "UTC timestamps in source fields are parsed as UTC; membership dates remain calendar dates",
        },
        "source_identity": identity,
        "generation_comparison": generation_comparison,
        "generation_row_count_comparison": comparison_export_rows,
        "measurement": {
            "export": analysis["export"],
            "audit": analysis["audit"],
            "publication_timing": analysis["publication_timing"],
            "coverage_manifest": analysis["coverage_manifest"],
            "security_master": analysis["security_master"],
            "security_master_exclusion_row_count": analysis[
                "security_master_exclusion_row_count"
            ],
            "security_master_exclusion_reason_counts": analysis[
                "security_master_exclusion_reason_counts"
            ],
            "security_master_membership_accounting": analysis[
                "security_master_membership_accounting"
            ],
            "membership_event_row_count": analysis["membership_event_row_count"],
            "membership_union_ticker_count": analysis["membership_union_ticker_count"],
            "snapshots": membership_snapshots,
            "bounded_archive_sample": archive_samples,
        },
        "interpretation_limits": [
            "The assessed SEC-fundamentals export is a distinct generation from the acquisition cache used by the prior material-only packet; no claim is made that this alternate generation was consumed by that packet.",
            "The current fundamentals_provenance.json hash differs from fundamentals_publication.json; removing only the net_income_concept_priority field reconstructs the marker-bound hash, but this byte reconstruction does not establish when or why that field was added or bind the alternate export atomically to the acquisition generation.",
            "The aggregate exporter omits country, currency/unit, and source-origin columns; audit concepts and three bounded archive traces cannot establish universe-wide foreign or unit reconciliation.",
            "Rows with more than one public-date vintage are revision candidates, not confirmed amendments solely from row multiplicity.",
            "Q4 concept-consistent symbol-year candidates are not derived values and do not prove source-unit, currency, or accounting-basis reconciliation.",
            "Snapshot coverage is reported for the two named member dates and the retained S&P membership seed only; it is not full V5 three-universe coverage or a benchmark score evaluation.",
        ],
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--membership-csv", type=Path, required=True)
    parser.add_argument("--trading-days-csv", type=Path, required=True)
    parser.add_argument("--comparison-import-provenance", type=Path)
    parser.add_argument(
        "--reuse-archive-digests-from",
        type=Path,
        help="Reuse authenticated SEC archive SHA-256 values from a prior receipt after identity and byte-size checks.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output_path = args.output.resolve()
    if not output_path.is_relative_to(REPO_ROOT):
        parser.error("--output must be inside the repository workspace")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = make_receipt(args)
    output_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {output_path}")
    print(
        "Verified source inputs: "
        + str(len(receipt["source_identity"]["verified_inputs"]))
    )
    print(
        "Export rows / audit rows / snapshots: "
        + str(receipt["measurement"]["export"]["row_count"])
        + " / "
        + str(receipt["measurement"]["audit"]["row_count"])
        + " / "
        + ", ".join(receipt["measurement"]["snapshots"].keys())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
