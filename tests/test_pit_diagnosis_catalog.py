from pathlib import Path
from dataclasses import replace

import pytest

from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.models import ExperimentCatalog, ExperimentDefinition
from core.pit_diagnosis.rulebook import load_rulebook


def test_partitions_are_exact_and_2025_is_not_named_holdout() -> None:
    partitions = fixed_partitions()
    assert partitions.discovery.as_tuple() == ("2021-01-01", "2023-12-31")
    assert partitions.validation.as_tuple() == ("2024-01-01", "2024-12-31")
    assert partitions.locked_evaluation.as_tuple() == ("2025-01-01", "2025-12-31")
    assert "holdout" not in repr(partitions).lower()


def test_catalog_contains_only_the_approved_experiments() -> None:
    book = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), book)
    assert set(catalog.experiments) == {
        "D0.BASELINE_REPRODUCTION", "D1.FULL_FUNDAMENTAL_COHORT", "D1.N_CATALYST_GAP",
        "D1.I_SPONSORSHIP_GAP", "D1.INDUSTRY_GROUP_GAP", "D2.RULE_STAGE_FUNNEL",
        "D2.PROPER_BASE_COUNTERFACTUAL", "D2.RS_85_CONFORMANCE", "D2.LEADING_GROUP_CONFORMANCE",
        "D2.BUY_ZONE_ATTRIBUTION", "D2.LEADER_RANK_BENCHMARK", "D3.M_CONFIRMED_UPTREND",
        "D3.M_DISTRIBUTION_EXPOSURE", "D3.M_BASELINE_OFF", "D4.CURRENT_EXIT_PACKAGE",
        "D4.LOSS_LIMIT_ONLY", "D4.PROFIT_ZONE", "D4.EIGHT_WEEK_HOLD", "D4.STRUCTURAL_SELL",
        "D4.REMOVE_UNVERIFIED_EXITS", "D5.BOUNDED_PAIR",
    }
    assert catalog["D3.M_BASELINE_OFF"].promotion_eligible is False
    assert catalog["D5.BOUNDED_PAIR"].controller_composed is True


def test_identity_includes_experiment_schema_and_nested_fields_are_immutable() -> None:
    book = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), book)
    baseline = catalog["D0.BASELINE_REPRODUCTION"]
    changed = replace(baseline, changed_dimensions=("different_dimension",))
    from core.pit_diagnosis.catalog import build_experiment_identity
    kwargs = dict(source_commit="a" * 40, source_fingerprint_sha256="b" * 64, bundle_sha256="c" * 64,
                  baseline_manifest_sha256="d" * 64, rulebook_sha256=book.sha256, fact_cache_schema_sha256="e" * 64,
                  fact_cache_content_sha256="f" * 64, catalog_sha256=catalog.sha256, partition=fixed_partitions().discovery,
                  strategy_identity="strategy", benchmark_identity="benchmark", universe_identity="universe")
    first = build_experiment_identity(experiment=baseline, **kwargs)
    second = build_experiment_identity(experiment=changed, **kwargs)
    assert first.sha256 != second.sha256
    with pytest.raises(TypeError):
        first.fields["experiment"]["experiment_id"] = "changed"


def test_invalid_phase_d5_shape_and_catalog_metadata_are_rejected() -> None:
    common = dict(experiment_id="D9.TEST", domain="test", kind="data", changed_dimensions=("x",),
                  rule_ids=(), promotion_eligible=False, controller_composed=False, requires_code=False,
                  allowed_variant_ids=())
    with pytest.raises(ValueError, match="D0-D5"):
        ExperimentDefinition(phase="D9", **common)
    with pytest.raises(ValueError, match="D5"):
        ExperimentDefinition(phase="D5", **{**common, "experiment_id": "D5.TEST", "changed_dimensions": ("x",), "controller_composed": True})
    valid = ExperimentDefinition(phase="D0", **common)
    with pytest.raises(ValueError, match="sha256"):
        ExperimentCatalog("v1", {valid.experiment_id: valid}, "bad")
    with pytest.raises(ValueError, match="version"):
        ExperimentCatalog("", {valid.experiment_id: valid}, "0" * 64)
