"""Acquire official SEC bulk archives and publish PIT fundamental exports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import requests

from core.sec_pit_fundamentals import (
    FUNDAMENTAL_AUDIT_COLUMNS,
    FUNDAMENTAL_COLUMNS,
    SECURITY_MASTER_COLUMNS,
    SECURITY_MASTER_EXCLUSION_COLUMNS,
    FundamentalAuditRow,
    FundamentalRow,
    SecurityMasterExclusion,
    SecurityMasterRow,
    build_security_master,
    extract_fundamentals,
    sha256_file,
    validate_sec_archive,
)


_SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
_COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
_OFFICIAL_ARCHIVES = (
    ("submissions.zip", _SUBMISSIONS_URL),
    ("companyfacts.zip", _COMPANYFACTS_URL),
)
_ARCHIVE_MANIFEST = "sec_archives_provenance.json"
_NORMALIZED_OUTPUTS = (
    "security_master.csv",
    "security_master_exclusions.csv",
    "fundamentals.csv",
    "fundamentals_audit.csv",
    "fundamentals_provenance.json",
    "fundamentals_coverage.json",
)
_PUBLICATION_MARKER = "fundamentals_publication.json"
_BASELINE_START = date(2020, 1, 1)
_MEMBERSHIP_START = date(2021, 1, 1)
_BASELINE_END = date(2025, 12, 31)
_REQUEST_TIMEOUT_SECONDS = 30.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.2


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _positive_bytes(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("maximum archive bytes must be an integer") from exc
    if not 1 <= parsed <= 100 * 1024 * 1024 * 1024:
        raise argparse.ArgumentTypeError("maximum archive bytes must be between 1 byte and 100 GiB")
    return parsed


def _output_dir(value: str) -> Path:
    absolute = Path(os.path.abspath(value))
    if absolute.exists():
        info = absolute.lstat()
        if not stat.S_ISDIR(info.st_mode) or absolute.is_symlink() or absolute.resolve() != absolute:
            raise ValueError("output directory must be a regular non-link directory")
    else:
        absolute.mkdir(parents=True)
    return absolute


def _require_user_agent(value: str) -> str:
    user_agent = value.strip()
    if not 20 <= len(user_agent) <= 512 or "RS-momentum-EMA-trading-bot" not in user_agent:
        raise ValueError("SEC user-agent must identify the RS-momentum-EMA-trading-bot project")
    if "@" not in user_agent and "https://" not in user_agent:
        raise ValueError("SEC user-agent must contain an operator contact email or HTTPS contact URL")
    if "\r" in user_agent or "\n" in user_agent:
        raise ValueError("SEC user-agent cannot contain line breaks")
    return user_agent


def _regular_file(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    info = absolute.lstat()
    resolved = absolute.resolve(strict=True)
    if not stat.S_ISREG(info.st_mode) or absolute.is_symlink() or resolved != absolute:
        raise ValueError(f"{label} must be a regular non-link file")
    return resolved


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes_exclusive(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_sec_response_url(value: str, expected_url: str) -> None:
    actual = urlparse(value)
    expected = urlparse(expected_url)
    if (
        actual.scheme != "https"
        or actual.hostname != expected.hostname
        or actual.path != expected.path
        or actual.query
        or actual.fragment
    ):
        raise ValueError(f"SEC archive request redirected outside its fixed endpoint: {value}")


def _stream_archive(
    session: requests.Session,
    *,
    url: str,
    target: Path,
    max_bytes: int,
    max_json_member_bytes: int,
) -> Mapping[str, Any]:
    digest = hashlib.sha256()
    received = 0
    with session.get(url, stream=True, timeout=_REQUEST_TIMEOUT_SECONDS, allow_redirects=False) as response:
        _validate_sec_response_url(response.url, url)
        if response.status_code != 200:
            raise RuntimeError(f"official SEC archive request failed with HTTP {response.status_code}: {url}")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                expected_length = int(content_length)
            except ValueError as exc:
                raise ValueError("SEC archive returned an invalid Content-Length") from exc
            if expected_length < 1 or expected_length > max_bytes:
                raise ValueError("SEC archive Content-Length exceeds the caller byte cap")
        with target.open("xb") as stream:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError("SEC archive stream exceeds the caller byte cap")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    if received < 1:
        raise ValueError("SEC archive response is empty")
    entry_count, uncompressed_bytes = validate_sec_archive(
        target, max_member_bytes=max_json_member_bytes
    )
    return {
        "url": url,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "byte_length": received,
        "sha256": digest.hexdigest(),
        "zip_entry_count": entry_count,
        "zip_uncompressed_bytes": uncompressed_bytes,
        "max_json_member_bytes": max_json_member_bytes,
    }


def _verify_archive_manifest(
    output_dir: Path,
    *,
    max_archive_bytes: int,
    max_json_member_bytes: int,
) -> Mapping[str, Any]:
    manifest_path = _regular_file(output_dir / _ARCHIVE_MANIFEST, "SEC archive provenance")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("SEC archive provenance is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("SEC archive provenance schema is invalid")
    archives = manifest.get("archives")
    if not isinstance(archives, dict) or set(archives) != {name for name, _ in _OFFICIAL_ARCHIVES}:
        raise ValueError("SEC archive provenance does not bind both fixed archives")
    for name, url in _OFFICIAL_ARCHIVES:
        metadata = archives[name]
        if not isinstance(metadata, dict) or metadata.get("url") != url:
            raise ValueError(f"SEC archive provenance URL is invalid for {name}")
        path = _regular_file(output_dir / name, f"SEC {name}")
        if path.stat().st_size > max_archive_bytes:
            raise ValueError(f"existing SEC archive exceeds the caller byte cap: {name}")
        if metadata.get("max_json_member_bytes") != max_json_member_bytes:
            raise ValueError(f"SEC archive member-size bound differs from provenance for {name}")
        if path.stat().st_size != metadata.get("byte_length") or sha256_file(path) != metadata.get("sha256"):
            raise ValueError(f"SEC archive bytes differ from provenance for {name}")
        entry_count, uncompressed_bytes = validate_sec_archive(
            path, max_member_bytes=max_json_member_bytes
        )
        if entry_count != metadata.get("zip_entry_count") or uncompressed_bytes != metadata.get("zip_uncompressed_bytes"):
            raise ValueError(f"SEC archive ZIP metadata differs from provenance for {name}")
    return manifest


def acquire_archives(
    output_dir: Path,
    *,
    sec_user_agent: str,
    max_archive_bytes: int,
    max_json_member_bytes: int,
) -> Mapping[str, Any]:
    """Download both fixed archives once, or verify and reuse the complete pair."""
    archive_targets = [output_dir / name for name, _ in _OFFICIAL_ARCHIVES]
    manifest_target = output_dir / _ARCHIVE_MANIFEST
    existing = [path.exists() or path.is_symlink() for path in (*archive_targets, manifest_target)]
    if any(existing):
        if not all(existing):
            raise ValueError("refusing a partial pre-existing SEC archive transaction")
        return _verify_archive_manifest(
            output_dir,
            max_archive_bytes=max_archive_bytes,
            max_json_member_bytes=max_json_member_bytes,
        )

    token = uuid.uuid4().hex
    temporary = {name: output_dir / f".{name}.{token}.tmp" for name, _ in _OFFICIAL_ARCHIVES}
    manifest_temporary = output_dir / f".{_ARCHIVE_MANIFEST}.{token}.tmp"
    created_targets: list[tuple[Path, Path]] = []
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _require_user_agent(sec_user_agent),
            "Accept": "application/zip,application/octet-stream",
            "Accept-Encoding": "identity",
        }
    )
    try:
        archives: dict[str, Any] = {}
        previous_start: float | None = None
        for name, url in _OFFICIAL_ARCHIVES:
            if previous_start is not None:
                wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - previous_start)
                if wait > 0:
                    time.sleep(wait)
            previous_start = time.monotonic()
            archives[name] = dict(
                _stream_archive(
                    session,
                    url=url,
                    target=temporary[name],
                    max_bytes=max_archive_bytes,
                    max_json_member_bytes=max_json_member_bytes,
                )
            )
        manifest = {"schema_version": 1, "archives": archives}
        _write_bytes_exclusive(manifest_temporary, _json_bytes(manifest))
        for name, _ in _OFFICIAL_ARCHIVES:
            target = output_dir / name
            try:
                os.link(temporary[name], target)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite SEC archive: {target}") from exc
            created_targets.append((temporary[name], target))
        try:
            os.link(manifest_temporary, manifest_target)
        except FileExistsError as exc:
            raise ValueError("refusing to overwrite SEC archive provenance") from exc
        created_targets.append((manifest_temporary, manifest_target))
        return manifest
    except Exception:
        for source, target in reversed(created_targets):
            try:
                if os.path.samefile(source, target):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        session.close()
        for path in (*temporary.values(), manifest_temporary):
            path.unlink(missing_ok=True)


def _number(value: float | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "0"
    return format(value, ".17g")


def _security_master_values(row: SecurityMasterRow) -> tuple[str, ...]:
    return (
        row.ticker,
        row.cik,
        row.company_name,
        row.first_membership_date.isoformat(),
        row.last_membership_date.isoformat(),
        row.mapping_basis,
    )


def _exclusion_values(row: SecurityMasterExclusion) -> tuple[str, ...]:
    return (
        row.ticker,
        row.company_name,
        row.first_membership_date.isoformat(),
        row.last_membership_date.isoformat(),
        row.reason,
        row.details,
    )


def _fundamental_values(row: FundamentalRow) -> tuple[str, ...]:
    return (
        row.ticker,
        row.statement_type,
        row.period_end.isoformat(),
        row.public_date.isoformat(),
        _number(row.basic_eps),
        _number(row.diluted_eps),
        _number(row.total_revenue),
        _number(row.net_income),
        _number(row.common_stock),
        _number(row.total_stockholders_equity),
        _number(row.shares_outstanding),
        "",
        "",
        "",
    )


def _audit_values(row: FundamentalAuditRow) -> tuple[str, ...]:
    return (
        row.ticker,
        row.statement_type,
        row.period_end.isoformat(),
        row.public_date.isoformat(),
        row.accession_number,
        row.form,
        row.filed_date.isoformat(),
        row.fiscal_year,
        row.fiscal_period,
        row.acceptance_datetime,
        row.public_date_basis,
        row.source_concepts,
        row.inherited_metrics,
        row.metric_sources,
    )


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def _assert_normalized_targets_absent(output_dir: Path) -> None:
    for name in (*_NORMALIZED_OUTPUTS, _PUBLICATION_MARKER):
        target = output_dir / name
        if target.exists() or target.is_symlink():
            raise ValueError(f"refusing to overwrite existing normalized output: {target}")


def _consumed_hashes(
    *,
    membership_csv: Path,
    security_names_csv: Path,
    spy_trading_days_csv: Path,
    identity_manifest_csv: Path,
    output_dir: Path,
) -> dict[str, str]:
    return {
        "membership_csv_sha256": sha256_file(membership_csv),
        "security_names_csv_sha256": sha256_file(security_names_csv),
        "spy_trading_days_csv_sha256": sha256_file(spy_trading_days_csv),
        "identity_manifest_csv_sha256": sha256_file(identity_manifest_csv),
        "submissions_archive_sha256": sha256_file(output_dir / "submissions.zip"),
        "companyfacts_archive_sha256": sha256_file(output_dir / "companyfacts.zip"),
    }


def _unlink_same_file(source: Path, target: Path) -> None:
    try:
        if os.path.samefile(source, target):
            target.unlink()
    except FileNotFoundError:
        pass


def _file_identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def _unlink_owned_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        if _file_identity(path) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def publish_normalized_outputs(
    output_dir: Path,
    *,
    security_master: Any,
    fundamentals: Any,
    archive_manifest: Mapping[str, Any],
    membership_csv: Path,
    security_names_csv: Path,
    spy_trading_days_csv: Path,
    identity_manifest_csv: Path,
    consumed_hashes: Mapping[str, str],
    start_date: date,
    end_date: date,
) -> Mapping[str, Any]:
    """Publish the six normalized outputs as one no-clobber transaction."""
    _assert_normalized_targets_absent(output_dir)
    current_hashes = _consumed_hashes(
        membership_csv=membership_csv,
        security_names_csv=security_names_csv,
        spy_trading_days_csv=spy_trading_days_csv,
        identity_manifest_csv=identity_manifest_csv,
        output_dir=output_dir,
    )
    if current_hashes != dict(consumed_hashes):
        raise ValueError("consumed Task 2 input changed before publication")
    if security_master.submissions_archive_sha256 != consumed_hashes["submissions_archive_sha256"]:
        raise ValueError("security master is not bound to the consumed submissions archive")
    if security_master.identity_manifest_sha256 != consumed_hashes["identity_manifest_csv_sha256"]:
        raise ValueError("security master is not bound to the consumed identity overlay")
    if fundamentals.companyfacts_archive_sha256 != consumed_hashes["companyfacts_archive_sha256"]:
        raise ValueError("fundamentals are not bound to the consumed companyfacts archive")
    staging = output_dir / f".sec-pit-publish-{uuid.uuid4().hex}"
    staging.mkdir()
    published: list[tuple[Path, Path]] = []
    try:
        _write_csv(
            staging / "security_master.csv",
            SECURITY_MASTER_COLUMNS,
            (_security_master_values(row) for row in security_master.rows),
        )
        _write_csv(
            staging / "security_master_exclusions.csv",
            SECURITY_MASTER_EXCLUSION_COLUMNS,
            (_exclusion_values(row) for row in security_master.exclusions),
        )
        _write_csv(
            staging / "fundamentals.csv",
            FUNDAMENTAL_COLUMNS,
            (_fundamental_values(row) for row in fundamentals.rows),
        )
        _write_csv(
            staging / "fundamentals_audit.csv",
            FUNDAMENTAL_AUDIT_COLUMNS,
            (_audit_values(row) for row in fundamentals.audit_rows),
        )
        coverage = dict(fundamentals.coverage)
        _write_bytes_exclusive(staging / "fundamentals_coverage.json", _json_bytes(coverage))
        source_hashes = dict(consumed_hashes)
        normalized_hashes = {
            f"{name.removesuffix('.csv')}_sha256": sha256_file(staging / name)
            for name in (
                "security_master.csv",
                "security_master_exclusions.csv",
                "fundamentals.csv",
                "fundamentals_audit.csv",
            )
        }
        normalized_hashes["fundamentals_coverage_sha256"] = sha256_file(
            staging / "fundamentals_coverage.json"
        )
        provenance = {
            "schema_version": 1,
            "source": "SEC EDGAR official bulk archives",
            "archive_manifest": archive_manifest,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "public_date_rule": "first supplied SPY trading day strictly after SEC acceptance calendar date; filed date fallback only",
            "quarterly_duration_days": [70, 115],
            "annual_duration_days": [300, 430],
            "revenue_concept_priority": [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
            ],
            "institutional_fields": "omitted_blank",
            "security_master_row_count": len(security_master.rows),
            "security_master_exclusion_row_count": len(security_master.exclusions),
            "fundamental_row_count": len(fundamentals.rows),
            "filed_date_fallback_count": coverage.get("filed_date_fallback_count", 0),
            **source_hashes,
            **normalized_hashes,
        }
        _write_bytes_exclusive(staging / "fundamentals_provenance.json", _json_bytes(provenance))
        publication_hashes = {
            name: sha256_file(staging / name)
            for name in _NORMALIZED_OUTPUTS
        }
        marker = {
            "schema_version": 1,
            "status": "complete",
            "files": publication_hashes,
        }
        _write_bytes_exclusive(staging / _PUBLICATION_MARKER, _json_bytes(marker))
        final_hashes = _consumed_hashes(
            membership_csv=membership_csv,
            security_names_csv=security_names_csv,
            spy_trading_days_csv=spy_trading_days_csv,
            identity_manifest_csv=identity_manifest_csv,
            output_dir=output_dir,
        )
        if final_hashes != dict(consumed_hashes):
            raise ValueError("consumed Task 2 input changed during normalization")
        _assert_normalized_targets_absent(output_dir)
        for name in _NORMALIZED_OUTPUTS:
            source = staging / name
            target = output_dir / name
            try:
                os.link(source, target)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite normalized output: {target}") from exc
            published.append((source, target))
        marker_source = staging / _PUBLICATION_MARKER
        marker_target = output_dir / _PUBLICATION_MARKER
        try:
            os.link(marker_source, marker_target)
        except FileExistsError as exc:
            raise ValueError("refusing to overwrite fundamentals publication marker") from exc
        published.append((marker_source, marker_target))
        return provenance
    except Exception:
        for source, target in reversed(published):
            _unlink_same_file(source, target)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build filing-time SEC fundamentals for the five-year PIT baseline")
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--security-names-csv", required=True)
    parser.add_argument("--spy-trading-days-csv", required=True)
    parser.add_argument("--start-date", type=_date, default=_BASELINE_START)
    parser.add_argument("--end-date", type=_date, default=_BASELINE_END)
    parser.add_argument("--sec-user-agent", required=True)
    parser.add_argument("--max-archive-bytes", type=_positive_bytes, required=True)
    parser.add_argument("--identity-manifest-csv", default="config/pit_price_identity_map.csv")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    output_dir: Path | None = None
    archives_created = False
    owned_archive_identities: dict[str, tuple[int, int]] = {}
    try:
        if args.start_date != _BASELINE_START or args.end_date != _BASELINE_END:
            raise ValueError("Task 2 window must be exactly 2020-01-01 through 2025-12-31")
        user_agent = _require_user_agent(args.sec_user_agent)
        output_dir = _output_dir(args.output_dir)
        membership_csv = _regular_file(Path(args.membership_csv), "membership CSV")
        security_names_csv = _regular_file(Path(args.security_names_csv), "security names CSV")
        spy_days_csv = _regular_file(Path(args.spy_trading_days_csv), "SPY trading-days CSV")
        identity_manifest_csv = _regular_file(Path(args.identity_manifest_csv), "reviewed identity manifest")
        _assert_normalized_targets_absent(output_dir)
        max_json_member_bytes = min(args.max_archive_bytes, 512 * 1024 * 1024)
        archives_preexisting = all(
            (output_dir / name).is_file() and not (output_dir / name).is_symlink()
            for name in (*[name for name, _ in _OFFICIAL_ARCHIVES], _ARCHIVE_MANIFEST)
        )
        archive_manifest = acquire_archives(
            output_dir,
            sec_user_agent=user_agent,
            max_archive_bytes=args.max_archive_bytes,
            max_json_member_bytes=max_json_member_bytes,
        )
        archives_created = not archives_preexisting
        if archives_created:
            owned_archive_identities = {
                name: _file_identity(output_dir / name)
                for name in (*[name for name, _ in _OFFICIAL_ARCHIVES], _ARCHIVE_MANIFEST)
            }
        input_hashes = _consumed_hashes(
            membership_csv=membership_csv,
            security_names_csv=security_names_csv,
            spy_trading_days_csv=spy_days_csv,
            identity_manifest_csv=identity_manifest_csv,
            output_dir=output_dir,
        )
        archive_metadata = archive_manifest["archives"]
        if input_hashes["submissions_archive_sha256"] != archive_metadata["submissions.zip"]["sha256"]:
            raise ValueError("submissions archive changed after archive verification")
        if input_hashes["companyfacts_archive_sha256"] != archive_metadata["companyfacts.zip"]["sha256"]:
            raise ValueError("companyfacts archive changed after archive verification")
        security_master = build_security_master(
            membership_csv,
            security_names_csv,
            output_dir / "submissions.zip",
            identity_manifest_csv,
            start_date=_MEMBERSHIP_START,
            end_date=args.end_date,
            max_json_member_bytes=max_json_member_bytes,
        )
        fundamentals = extract_fundamentals(
            output_dir / "companyfacts.zip",
            security_master,
            spy_days_csv,
            start_date=args.start_date,
            end_date=args.end_date,
            max_json_member_bytes=max_json_member_bytes,
        )
        if _consumed_hashes(
            membership_csv=membership_csv,
            security_names_csv=security_names_csv,
            spy_trading_days_csv=spy_days_csv,
            identity_manifest_csv=identity_manifest_csv,
            output_dir=output_dir,
        ) != input_hashes:
            raise ValueError("Task 2 input changed while it was being consumed")
        provenance = publish_normalized_outputs(
            output_dir,
            security_master=security_master,
            fundamentals=fundamentals,
            archive_manifest=archive_manifest,
            membership_csv=membership_csv,
            security_names_csv=security_names_csv,
            spy_trading_days_csv=spy_days_csv,
            identity_manifest_csv=identity_manifest_csv,
            consumed_hashes=input_hashes,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        if archives_created and output_dir is not None:
            for name, identity in owned_archive_identities.items():
                _unlink_owned_identity(output_dir / name, identity)
        parser.error(str(exc))
    print(json.dumps(provenance, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
