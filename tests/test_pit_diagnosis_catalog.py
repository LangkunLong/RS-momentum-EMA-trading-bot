from pathlib import Path

from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
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
