"""Frozen PIT diagnosis partitions, experiment catalog, and run identities."""

from pathlib import Path

from .models import (
    DatePartition,
    DatePartitions,
    ExperimentCatalog,
    ExperimentDefinition,
    ExperimentIdentity,
    Rulebook,
)
from .rulebook import canonical_sha256, load_canonical_json


def fixed_partitions() -> DatePartitions:
    return DatePartitions(
        discovery=DatePartition("discovery", "2021-01-01", "2023-12-31"),
        validation=DatePartition("validation", "2024-01-01", "2024-12-31"),
        locked_evaluation=DatePartition("locked_evaluation", "2025-01-01", "2025-12-31"),
    )


def load_experiment_catalog(path: Path, rulebook: Rulebook) -> ExperimentCatalog:
    payload = load_canonical_json(path)
    records = tuple(
        ExperimentDefinition.from_mapping(item, rulebook)
        for item in payload["experiments"]
    )
    return ExperimentCatalog.from_records(payload["version"], records, canonical_sha256(payload))


def build_experiment_identity(
    *,
    source_commit: str,
    source_fingerprint_sha256: str,
    bundle_sha256: str,
    baseline_manifest_sha256: str,
    rulebook_sha256: str,
    fact_cache_schema_sha256: str,
    fact_cache_content_sha256: str,
    catalog_sha256: str,
    experiment: ExperimentDefinition,
    partition: DatePartition,
    strategy_identity: str,
    benchmark_identity: str,
    universe_identity: str,
) -> ExperimentIdentity:
    return ExperimentIdentity.from_fields(locals())
