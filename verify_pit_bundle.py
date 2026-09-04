"""Verify a point-in-time bundle read-only against exact sources and manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Mapping

import build_pit_bundle as bundle_builder
from core.pit_data import PITDataBundle, sha256_file
from core.pit_provenance import (
    PIT_NON_TRADABLE_REFERENCE_SYMBOLS,
    pit_canonical_json,
    pit_canonical_json_bytes,
    pit_canonical_json_sha256,
)


def _regular_file(path: str | Path, *, label: str) -> Path:
    value = Path(path)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"{label} must be a regular non-link file")
    return value.resolve()


def _compare_manifest(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    normalized = dict(actual)
    normalized["symbols"] = normalized.pop("symbol_count")
    if expected != normalized:
        differing = sorted(key for key in set(expected).union(normalized) if expected.get(key) != normalized.get(key))
        raise ValueError(f"manifest does not exactly match the bundle: {differing}")


def _verify_exact_sources(
    bundle: PITDataBundle,
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> None:
    fields = {
        "membership_csv": "membership_source_sha256",
        "prices_csv": "prices_source_sha256",
        "fundamentals_csv": "fundamentals_source_sha256",
        "membership_provenance": "membership_provenance_sha256",
        "prices_provenance": "prices_provenance_sha256",
        "fundamentals_provenance": "fundamentals_provenance_sha256",
    }
    for name, metadata_key in fields.items():
        raw_path = getattr(args, name)
        path = _regular_file(raw_path, label=name.replace("_", " "))
        if sha256_file(path) != bundle.metadata[metadata_key]:
            raise ValueError(f"exact source digest mismatch: {name}")
    bundle.load_price_identity_transition_contract(args.prices_provenance)
    if bundle.metadata["schema_version"] == "1":
        return
    prices_path = _regular_file(args.prices_csv, label="prices csv")
    reference_dates: dict[str, set[str]] = {
        reference: set() for reference in PIT_NON_TRADABLE_REFERENCE_SYMBOLS
    }
    with prices_path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            ticker = row.get("ticker")
            if ticker in reference_dates:
                reference_dates[ticker].add(str(row.get("trade_date", "")))
    source_coverage = {
        reference: {
            "first_date": days[0],
            "last_date": days[-1],
            "session_count": len(days),
        }
        for reference, observed in reference_dates.items()
        for days in (sorted(observed),)
        if days
    }
    expected_references = list(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
    if (
        manifest.get("non_tradable_reference_symbols") != expected_references
        or manifest.get("coverage", {}).get("references") != source_coverage
    ):
        raise ValueError("bundle manifest reference coverage differs from prices source")
    provenance_path = _regular_file(
        args.prices_provenance, label="prices provenance"
    )
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prices provenance JSON is invalid") from exc
    if (
        not isinstance(provenance, dict)
        or provenance.get("reference_symbol_coverage") != source_coverage
        or provenance.get("non_tradable_reference_symbols_json")
        != pit_canonical_json(expected_references)
        or provenance.get("non_tradable_reference_symbols_sha256")
        != pit_canonical_json_sha256(expected_references)
    ):
        raise ValueError("prices provenance reference coverage differs from prices source")


def _database_rows(bundle_path: Path, table: str, columns: str, order: str) -> list[tuple[Any, ...]]:
    uri = f"file:{bundle_path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return [tuple(row) for row in connection.execute(
            f"SELECT {columns} FROM {table} ORDER BY {order}"
        ).fetchall()]
    finally:
        connection.close()


def _verify_v3(
    *,
    bundle: PITDataBundle,
    bundle_path: Path,
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> tuple[
    dict[str, object],
    list[tuple[Any, ...]],
    dict[str, object],
    dict[str, Path],
]:
    paths = {
        name: _regular_file(getattr(args, name), label=name.replace("_", " "))
        for name in (
            "membership_csv",
            "prices_csv",
            "fundamentals_csv",
            "industry_csv",
            "membership_provenance",
            "prices_provenance",
            "fundamentals_provenance",
            "industry_provenance",
        )
    }
    metadata_keys = {
        "membership_csv": "membership_source_sha256",
        "prices_csv": "prices_source_sha256",
        "fundamentals_csv": "fundamentals_source_sha256",
        "industry_csv": "industry_source_sha256",
        "membership_provenance": "membership_provenance_sha256",
        "prices_provenance": "prices_provenance_sha256",
        "fundamentals_provenance": "fundamentals_provenance_sha256",
        "industry_provenance": "industry_provenance_sha256",
    }
    before = {name: sha256_file(path) for name, path in paths.items()}
    for name, metadata_key in metadata_keys.items():
        if before[name] != bundle.metadata.get(metadata_key):
            raise ValueError(f"exact source digest mismatch: {name}")

    membership_provenance_path, membership_provenance = bundle_builder._json_input_v3(
        paths["membership_provenance"], label="membership provenance"
    )
    prices_provenance_path, prices_provenance = bundle_builder._json_input_v3(
        paths["prices_provenance"], label="prices provenance"
    )
    fundamentals_provenance_path, fundamentals_provenance = bundle_builder._json_input_v3(
        paths["fundamentals_provenance"], label="fundamentals provenance"
    )
    industry_provenance_path, industry_provenance = bundle_builder._json_input_v3(
        paths["industry_provenance"], label="industry provenance"
    )
    cutoff = bundle.metadata["data_cutoff"]
    evaluation_start = bundle.metadata["evaluation_start"]
    warmup_start = bundle.metadata["warmup_start"]
    membership = bundle_builder._load_membership_v3(paths["membership_csv"], cutoff)
    prices = bundle_builder._load_prices(
        paths["prices_csv"], cutoff, ticker_parser=bundle_builder._ticker_v3
    )
    fundamentals = bundle_builder._load_fundamentals(
        paths["fundamentals_csv"], cutoff, ticker_parser=bundle_builder._ticker_v3
    )
    industry = bundle_builder._load_industry(paths["industry_csv"], cutoff)
    (
        expected_metadata,
        exclusions,
        identities,
        transitions,
    ) = bundle_builder._v3_provenance_metadata(
        membership_path=paths["membership_csv"],
        prices_path=paths["prices_csv"],
        fundamentals_path=paths["fundamentals_csv"],
        industry_path=paths["industry_csv"],
        cutoff=cutoff,
        evaluation_start=evaluation_start,
        warmup_start=warmup_start,
        membership=membership,
        prices=prices,
        fundamentals=fundamentals,
        industry=industry,
        membership_provenance_path=membership_provenance_path,
        membership_provenance=membership_provenance,
        prices_provenance_path=prices_provenance_path,
        prices_provenance=prices_provenance,
        fundamentals_provenance_path=fundamentals_provenance_path,
        fundamentals_provenance=fundamentals_provenance,
        industry_provenance_path=industry_provenance_path,
        industry_provenance=industry_provenance,
    )
    if any(bundle.metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError("bundle metadata differs from recomputed schema-V3 provenance")
    bundle_builder._integrity_gate_v3(
        cutoff=cutoff,
        evaluation_start=evaluation_start,
        warmup_start=warmup_start,
        membership=membership,
        prices=prices,
        fundamentals=fundamentals,
        industry=industry,
        price_exclusions=exclusions,
        identities=identities,
        transitions=transitions,
    )
    exact_tables = (
        (
            "membership_v3",
            "effective_date,security_lineage_id,universe_id,member",
            "effective_date,security_lineage_id,universe_id",
            membership,
        ),
        (
            "price",
            "trade_date,ticker,open,high,low,close,volume",
            "trade_date,ticker",
            prices,
        ),
        (
            "fundamentals",
            "ticker,statement_type,period_end,public_date,basic_eps,diluted_eps,total_revenue,net_income,common_stock,total_stockholders_equity,shares_outstanding,held_percent_institutions,institution_count,prev_institution_count",
            "ticker,statement_type,public_date,period_end",
            fundamentals,
        ),
        (
            "industry_group_snapshots",
            "symbol,as_of_date,group_id,group_rank,group_members,evidence_ids",
            "symbol,as_of_date",
            industry,
        ),
    )
    for table, columns, order, expected_rows in exact_tables:
        if _database_rows(bundle_path, table, columns, order) != expected_rows:
            raise ValueError(f"bundle table does not exactly match source: {table}")

    active_by_universe = bundle_builder._v3_active_lineages(membership, evaluation_start)
    union = set().union(*active_by_universe.values())
    affiliations: dict[str, set[str]] = {}
    for universe, lineages in active_by_universe.items():
        for lineage in lineages:
            affiliations.setdefault(lineage, set()).add(universe)
    overlaps = sum(len(universes) > 1 for universes in affiliations.values())
    if bundle.security_lineages_at(evaluation_start) != frozenset(union):
        raise ValueError("bundle active union differs from independent universe states")
    if set(bundle.members_at(evaluation_start)).intersection(
        PIT_NON_TRADABLE_REFERENCE_SYMBOLS
    ):
        raise ValueError("reference symbols entered schema-V3 membership")
    overlap_lineages: set[str] = set()
    check_dates = sorted({evaluation_start, *(row[0] for row in membership)})
    for as_of in check_dates:
        state = bundle_builder._v3_active_lineages(membership, as_of)
        if as_of >= evaluation_start and any(
            not state[universe] for universe in bundle_builder._V3_SOURCE_UNIVERSES
        ):
            raise ValueError("a source universe has no active causal state")
        expected_affiliations: dict[str, frozenset[str]] = {}
        for lineage in set().union(*state.values()):
            expected_affiliations[lineage] = frozenset(
                universe
                for universe, lineages in state.items()
                if lineage in lineages
            )
            if len(expected_affiliations[lineage]) > 1:
                overlap_lineages.add(lineage)
        if dict(bundle.membership_v3.lineage_affiliations_at(as_of)) != dict(
            sorted(expected_affiliations.items())
        ):
            raise ValueError("bundle universe affiliations differ from source chronology")
    if not overlap_lineages:
        raise ValueError("schema-V3 membership contains no explicit universe overlap")
    augmented_manifest = bundle_builder._manifest_with_v3_industry(manifest, industry)
    summary: dict[str, object] = {
        "evaluation_start": evaluation_start,
        "lineage_union_count": len(union),
        "evaluation_start_overlap_lineage_count": overlaps,
        "historical_overlap_lineage_count": len(overlap_lineages),
        "universe_active_lineage_counts": {
            universe: len(active_by_universe[universe])
            for universe in bundle_builder._V3_SOURCE_UNIVERSES
        },
    }
    if before != {name: sha256_file(path) for name, path in paths.items()}:
        raise ValueError("a checked input changed during schema-V3 verification")
    return summary, industry, augmented_manifest, paths


def _write_report(path: str | Path, report: Mapping[str, object]) -> None:
    target = Path(path).resolve()
    if target.exists() or target.is_symlink():
        raise ValueError(f"refusing to overwrite existing verification report: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(pit_canonical_json_bytes(report))
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ValueError(
                f"refusing to overwrite existing verification report: {target}"
            ) from exc
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a strict point-in-time SQLite bundle")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--manifest", required=True, help="required manifest-last commit marker")
    parser.add_argument("--membership-csv", required=True)
    parser.add_argument("--prices-csv", required=True)
    parser.add_argument("--fundamentals-csv", required=True)
    parser.add_argument("--industry-csv")
    parser.add_argument("--membership-provenance", required=True)
    parser.add_argument("--prices-provenance", required=True)
    parser.add_argument("--fundamentals-provenance", required=True)
    parser.add_argument("--industry-provenance")
    parser.add_argument("--report-output")
    args = parser.parse_args()

    bundle_path = _regular_file(args.bundle, label="bundle")
    report_path = None
    if args.report_output is not None:
        report_path = Path(args.report_output).resolve()
        if report_path.exists() or report_path.is_symlink():
            raise ValueError(
                f"refusing to overwrite existing verification report: {report_path}"
            )
    with PITDataBundle(
        bundle_path,
        expected_sha256=args.sha256,
        prices_provenance=args.prices_provenance,
    ) as bundle:
        actual = bundle.manifest()
        schema_version = bundle.metadata["schema_version"]
        if schema_version == "3":
            if (
                args.industry_csv is None
                or args.industry_provenance is None
                or args.report_output is None
            ):
                parser.error(
                    "schema-V3 verification requires --industry-csv, "
                    "--industry-provenance, and --report-output"
                )
            summary, _industry, actual, checked_paths = _verify_v3(
                bundle=bundle,
                bundle_path=bundle_path,
                args=args,
                manifest=actual,
            )
        else:
            if (
                args.industry_csv is not None
                or args.industry_provenance is not None
                or args.report_output is not None
            ):
                parser.error("industry and report arguments are schema-V3-only")
            _verify_exact_sources(bundle, args, actual)
    manifest_path = _regular_file(args.manifest, label="manifest")
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest JSON is invalid") from exc
    if not isinstance(expected, dict):
        raise ValueError("manifest must contain a JSON object")
    _compare_manifest(actual, expected)
    if schema_version == "3":
        assert report_path is not None
        if sha256_file(bundle_path) != args.sha256:
            raise ValueError("bundle changed during schema-V3 verification")
        input_digests = {
            name: sha256_file(path) for name, path in sorted(checked_paths.items())
        }
        input_digests["manifest"] = sha256_file(manifest_path)
        report = {
            "bundle_sha256": sha256_file(bundle_path),
            "checked_inputs": input_digests,
            "kind": "pit_bundle_verification_v3",
            "membership_summary": summary,
            "schema_version": 3,
            "status": "verified",
        }
        _write_report(report_path, report)
    print(json.dumps(actual, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
