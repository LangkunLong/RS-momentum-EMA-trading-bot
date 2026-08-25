from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from core.pit_diagnosis.catalog import fixed_partitions, load_experiment_catalog
from core.pit_diagnosis.experiments import DiagnosisContext, run_experiment
from core.pit_diagnosis.fact_cache import SessionFact
from core.pit_diagnosis.models import FidelityLabel, PartitionName
from core.pit_diagnosis.rulebook import load_rulebook


class _Facts:
    def __init__(self, rows: tuple[SessionFact, ...]) -> None:
        self.rows = rows
        self.content_sha256 = "a" * 64
        self.schema_sha256 = "b" * 64

    def session_facts(self, start: str, end: str) -> tuple[SessionFact, ...]:
        return tuple(row for row in self.rows if start <= str(row.session) <= end)


def _fact(symbol: str, session: str, *, market: str = "uptrend") -> SessionFact:
    return SessionFact(
        MappingProxyType(
            {
                "symbol": symbol,
                "session": session,
                "row_sha256": ("a" if symbol == "AAA" else "b") * 64,
                "bundle_sha256": "c" * 64,
                "current_eps_yoy": 0.30,
                "sales_yoy": 0.30,
                "annual_eps_1": 4.0,
                "annual_eps_2": 3.0,
                "annual_eps_3": 2.0,
                "annual_eps_4": 1.0,
                "roe": 0.20,
                "base_kind": "flat",
                "pivot": 100.0,
                "close": 103.0,
                "open": 103.0,
                "event_volume_ratio": 1.5,
                "rs_rating": 90.0,
                "extension_pct": 0.03,
                "market_regime": market,
                "distribution_count": 0,
                "institutional_evidence_ids": "[]",
                "industry_evidence_ids": "[]",
            }
        )
    )


@pytest.fixture
def diagnosis_context() -> DiagnosisContext:
    rulebook = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), rulebook)
    return DiagnosisContext(
        rulebook=rulebook,
        catalog=catalog,
        fact_cache=_Facts(
            (
                _fact("AAA", "2021-01-04"),
                _fact("BBB", "2021-01-05"),
                _fact("AAA", "2024-01-02"),
            )
        ),
        partitions=fixed_partitions(),
        diagnostic_leader_labels=("AAA",),
        source_commit="d" * 40,
        source_fingerprint_sha256="e" * 64,
        strategy_identity="cached-diagnosis-v1",
    )


def reversed_labels() -> tuple[str, ...]:
    return ("BBB",)


def test_rule_stage_funnel_is_monotone_and_missing_i_never_passes(
    diagnosis_context: DiagnosisContext,
) -> None:
    result = run_experiment(diagnosis_context, "D2.RULE_STAGE_FUNNEL", PartitionName.DISCOVERY)
    survivor_counts = [stage.survivors for stage in result.rule_attribution]
    assert survivor_counts == sorted(survivor_counts, reverse=True)
    assert result.fidelity.label is FidelityLabel.FIDELITY_INCOMPLETE
    assert "I.SPONSORSHIP" in result.fidelity.unavailable_required_rule_ids
    assert result.promotion_eligible is False


def test_current_exit_package_explains_the_known_ma_loss_cluster(
    diagnosis_context: DiagnosisContext,
) -> None:
    result = run_experiment(diagnosis_context, "D4.CURRENT_EXIT_PACKAGE", PartitionName.DISCOVERY)
    ma = result.exit_attribution.by_reason["ma_violation"]
    assert ma.closed_positions > 0
    assert ma.win_rate_pct < result.trade_statistics.win_rate_pct
    assert ma.average_completed_position_return_pct < 0.0


def test_ex_post_leader_labels_cannot_change_a_trade_path(
    diagnosis_context: DiagnosisContext,
) -> None:
    first = run_experiment(diagnosis_context, "D3.M_CONFIRMED_UPTREND", PartitionName.VALIDATION)
    second = run_experiment(
        diagnosis_context.with_replaced_diagnostic_leader_labels(reversed_labels()),
        "D3.M_CONFIRMED_UPTREND",
        PartitionName.VALIDATION,
    )
    assert first.trade_path_sha256 == second.trade_path_sha256
    assert first.leader_recall != second.leader_recall
