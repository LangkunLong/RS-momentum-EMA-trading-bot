from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from core.backtest_engine import SimulationResultV5
from core.backtest_fills import ExecutionProfileV5
from core.pit_data import PriceIdentityTransitionContract
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.candidate_ir import (
    SourceBundleV5,
    SourceFileV5,
    derive_policy_revision_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    EvaluationReportV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.evaluator import (
    BaselineWorkerBindingV5,
    CandidateWorkerBindingV5,
    PitPanelEvaluatorV5,
)
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_provenance import pit_canonical_json_sha256
from core.strategy_policy.runtime import InProcessPolicyClientV3


def _source_bundle(*, candidate: bool = False) -> SourceBundleV5:
    root = Path(__file__).resolve().parents[1]
    files = []
    for path in EDITABLE_POLICY_PATHS_V5:
        source = (root / path).read_text(encoding="utf-8")
        if candidate and path == EDITABLE_POLICY_PATHS_V5[-1]:
            source = source.rstrip("\n") + "\n# Research-03 candidate fixture\n"
        files.append(SourceFileV5(path=path, source=source))
    return SourceBundleV5(files=tuple(files))


def _zero_report() -> EvaluationReportV5:
    zero = Decimal("0")
    return EvaluationReportV5(
        portfolio_annualized_return_pct=zero,
        portfolio_total_return_pct=zero,
        gross_annualized_return_pct=zero,
        benchmark_annualized_return_pct=zero,
        benchmark_total_return_pct=zero,
        max_drawdown_pct=zero,
        sharpe_ratio=zero,
        closed_trades=0,
        average_exposure_pct=zero,
        average_cash_pct=zero,
        turnover_pct=zero,
        total_friction_usd=zero,
        friction_drag_pct=zero,
        win_rate_pct=None,
        average_win_pct=None,
        average_loss_pct=None,
        payoff_ratio=None,
        expectancy_pct=None,
        median_holding_sessions=None,
        invested_sleeve_annualized_return_pct=None,
        estimated_idle_cash_drag_pct=zero,
        stop_gap_shortfall_usd=zero,
        identity_transition_count=0,
        scale_out_opportunity_cost_pct=None,
        maximum_favorable_excursion_pct=None,
        maximum_adverse_excursion_pct=None,
        entry_funnel=(),
        exit_attribution=(),
        policy_intent_outcomes=(),
        regime_slices=(),
        episode_slices=(),
        calendar_year_slices=(),
        rolling_returns=(),
    )


def test_baseline_and_candidate_receive_the_same_declared_simulation_inputs(
    tmp_path: Path,
) -> None:
    """Verify evaluator forwarding and identity binding without executing candidate source."""
    prices_provenance = tmp_path / "prices-provenance.json"
    prices_provenance.write_text("{}\n", encoding="utf-8")
    prices_sha = hashlib.sha256(prices_provenance.read_bytes()).hexdigest()
    transition_sha = pit_canonical_json_sha256({})
    transition = PriceIdentityTransitionContract(
        prices_provenance_sha256=prices_sha,
        request_contracts_sha256=transition_sha,
        identities={},
        transitions=(),
    )

    class PitBundle:
        sha256 = "b" * 64
        metadata = {"schema_version": "3", "warmup_start": "2024-01-01"}

        def load_price_identity_transition_contract(self, _path: Path) -> PriceIdentityTransitionContract:
            return transition

    bundle = PitBundle()
    parent_revision = derive_policy_revision_identity_v5(
        source_bundle=_source_bundle(),
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    candidate_revision = derive_policy_revision_identity_v5(
        source_bundle=_source_bundle(candidate=True),
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    execution = ExecutionProfileV5(
        schema_version=5,
        close_policy_exit_timing="next_open",
        gap_stop_rule="open_then_stop",
        end_of_test_rule="last_session_close",
        friction_model="half_spread_plus_market_impact_plus_commission_bps",
    )
    resources = SimpleNamespace(
        evaluation_cpu_limit=Decimal("1"),
        evaluation_memory_mib=128,
        evaluation_output_limit_bytes=65_536,
        evaluation_pid_limit=4,
    )
    sandbox = SandboxProfileV5(
        schema_version=5,
        image_name="research-03-fixture",
        image_digest="sha256:" + "d" * 64,
        runtime_source_sha256="c" * 64,
        network_mode="none",
        root_filesystem="read_only",
        source_mount_mode="read_only",
        data_mount_mode="read_only",
        output_mode="bounded_write_only",
        cpu_limit=resources.evaluation_cpu_limit,
        memory_limit_mib=resources.evaluation_memory_mib,
        output_limit_bytes=resources.evaluation_output_limit_bytes,
        pid_limit=resources.evaluation_pid_limit,
    )
    contract = EvaluatorContractV5(
        schema_version=5,
        execution_profile_sha256=execution.sha256,
        sandbox_profile_sha256=sandbox.sha256,
        evaluator_source_sha256="a" * 64,
        pit_bundle_sha256=bundle.sha256,
        prices_provenance_sha256=prices_sha,
        identity_transition_contract_sha256=transition_sha,
        baseline_source_bundle_sha256=_source_bundle().sha256,
        baseline_policy_revision_sha256=parent_revision.sha256,
        friction_grid=initial_friction_grid_v5(),
        selection_scenario_id="base",
    )
    panel = EvaluationPanelSpec.from_lineages(
        purpose="quick",
        sessions=("2025-01-02", "2025-01-03"),
        lineages=(PanelSecurityLineage("fixture-lead", ("LEAD",), ("sp500",)),),
    )
    simulator_calls: list[dict[str, object]] = []

    class CapturingSimulator:
        def __init__(self, **kwargs: object) -> None:
            self.inputs = kwargs
            self.policy_client_factory = kwargs["policy_client_factory"]
            simulator_calls.append({"constructor": kwargs})

        def run(self, tickers: list[str], **kwargs: str) -> SimulationResultV5:
            assert tickers == ["LEAD"]
            assert kwargs == {
                "start_date": panel.start_date,
                "end_date": panel.end_date,
                "history_start_date": "2024-01-01",
                "benchmark_symbol": "SPY",
            }
            client = self.policy_client_factory()
            client.close()
            simulator_calls[-1]["run"] = {"tickers": tickers, **kwargs}
            scenario = self.inputs["friction_scenario"]
            return SimulationResultV5(
                equity_curve=pd.Series([10_000.0, 10_000.0]),
                benchmark_symbol="SPY",
                config={
                    "pit_data_scope": "production",
                    "pit_bundle_sha256": bundle.sha256,
                    "execution_profile_sha256": execution.sha256,
                    "friction_scenario": {
                        "scenario_id": scenario.scenario_id,
                        "half_spread_bps": scenario.half_spread_bps,
                        "market_impact_bps": scenario.market_impact_bps,
                        "commission_bps": scenario.commission_bps,
                    },
                    "signal_every_n_days": 1,
                    "start_date": panel.start_date,
                    "end_date": panel.end_date,
                    "candidate_worker_binding": None,
                },
            )

    common = {
        "contract": contract,
        "sandbox_profile": sandbox,
        "resource_manifest": resources,
        "execution_profile": execution,
        "pit_bundle": bundle,
        "prices_provenance": prices_provenance,
        "report_builder": lambda **_kwargs: _zero_report(),
        "simulator_factory": CapturingSimulator,
    }

    def baseline_worker_factory() -> BaselineWorkerBindingV5:
        return BaselineWorkerBindingV5(
            policy_revision=parent_revision,
            client=InProcessPolicyClientV3(),
        )

    baseline = PitPanelEvaluatorV5(
        **common,
        baseline_policy_revision=parent_revision,
        baseline_worker_factory=baseline_worker_factory,
    ).evaluate_baseline(panel, scenario_ids=("base",))

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()

    def candidate_worker_factory(
        *, candidate_root: Path, policy_revision: PolicyRevisionIdentityV5
    ) -> CandidateWorkerBindingV5:
        return CandidateWorkerBindingV5(
            candidate_root=candidate_root,
            policy_revision=policy_revision,
            client=InProcessPolicyClientV3(),
        )

    candidate = PitPanelEvaluatorV5(
        **common,
        candidate_policy_authority=candidate_revision,
    ).evaluate_candidate(
        candidate_root=candidate_root.resolve(),
        panel=panel,
        policy_revision=candidate_revision,
        worker_factory=candidate_worker_factory,
        scenario_ids=("base",),
    )

    assert baseline.policy_identity_sha256 == parent_revision.sha256
    assert candidate.policy_identity_sha256 == candidate_revision.sha256
    assert parent_revision.sha256 != candidate_revision.sha256
    assert len(simulator_calls) == 2
    baseline_call, candidate_call = simulator_calls
    for field in (
        "pit_bundle",
        "benchmark_symbol",
        "signal_every_n_days",
        "identity_transition_contract",
        "execution_profile",
        "friction_scenario",
        "pit_data_scope",
    ):
        assert baseline_call["constructor"][field] == candidate_call["constructor"][field]
    assert baseline_call["constructor"]["pit_bundle"] is bundle
    assert baseline_call["constructor"]["identity_transition_contract"] is transition
    assert baseline_call["constructor"]["execution_profile"] is execution
    assert baseline_call["constructor"]["friction_scenario"].scenario_id == "base"
    assert baseline_call["constructor"]["pit_data_scope"] == "production"
    assert baseline_call["run"] == candidate_call["run"]
    assert candidate_call["constructor"]["policy_client_factory"] is not baseline_call["constructor"]["policy_client_factory"]
