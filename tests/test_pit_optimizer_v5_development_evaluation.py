from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from core.backtest_fills import ExecutionProfileV5
from core.pit_data import PriceIdentityTransitionContract
from core.pit_optimizer_evaluation import EvaluationPanelSpec, PanelSecurityLineage
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import PolicyRevisionIdentityV5
from core.pit_optimizer_v5.container_protocol import PanelExecutionRequestV5
from core.pit_optimizer_v5.contracts import (
    EpisodePlanV5,
    EvaluatorContractV5,
    SandboxProfileV5,
    initial_friction_grid_v5,
)
from core.pit_optimizer_v5.development_evaluation import (
    DevelopmentEvaluationContextV5,
    authenticate_inprocess_development_evaluation_v5,
    persist_inprocess_development_evaluation_v5,
)
from core.pit_optimizer_v5.diagnostics import summarize_panel_result
from core.pit_optimizer_v5.evaluator import CandidateWorkerBindingV5, PitPanelEvaluatorV5
from core.pit_optimizer_v5.two_round_study.fixtures import create_study_fixture_v1
from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1
from core.pit_provenance import PIT_NON_TRADABLE_REFERENCE_SYMBOLS, pit_canonical_json_sha256
from core.strategy_policy.runtime import InProcessPolicyClientV3


class _SyntheticDevelopmentPitBundle:
    """Small deterministic in-memory PIT fixture; the simulator computes all metrics."""

    def __init__(self, prices_provenance: Path) -> None:
        self.metadata = {"schema_version": "2", "warmup_start": "2019-01-02", "data_cutoff": "2020-04-30"}
        self.sha256 = hashlib.sha256(b"synthetic local development PIT bundle").hexdigest()
        self.data_cutoff = pd.Timestamp("2020-04-30")
        self._tradables = ("AAA", "AAB", "AAC", "AAD", "AAE", "AAF", "AAG", "AAH", "AAI", "AAJ")
        self._references = tuple(PIT_NON_TRADABLE_REFERENCE_SYMBOLS)
        self._symbols = tuple(sorted((*self._tradables, *self._references)))
        self._prices = self._build_prices(self._symbols)
        provenance_sha = hashlib.sha256(prices_provenance.read_bytes()).hexdigest()
        identities = {
            symbol: {
                "provider_symbol": symbol,
                "identity_asof": "2020-04-30",
                "admitted_start": "2019-01-02",
                "admitted_end": "2020-04-30",
                "chain_id": f"lineage_{index}",
                "continuity_kind": "synthetic_fixture",
                "warmup_predecessor": None,
                "factor_anchor": None,
            }
            for index, symbol in enumerate(self._symbols)
        }
        self.identity_transition_contract = PriceIdentityTransitionContract(
            provenance_sha,
            pit_canonical_json_sha256(identities),
            identities,
            (),
        )

    @staticmethod
    def _build_prices(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
        sessions = pd.bdate_range("2019-01-02", "2020-04-30")
        result: dict[str, pd.DataFrame] = {}
        for symbol_index, symbol in enumerate(symbols):
            closes = [100.0 + symbol_index * 3.0 + index * 0.15 for index in range(len(sessions))]
            result[symbol] = pd.DataFrame(
                {
                    "Open": [value * 0.999 for value in closes],
                    "High": [value * 1.01 for value in closes],
                    "Low": [value * 0.99 for value in closes],
                    "Close": closes,
                    "Volume": [100_000.0] * len(closes),
                },
                index=sessions,
            )
        return result

    def load_price_identity_transition_contract(self, _path: str | Path) -> PriceIdentityTransitionContract:
        return self.identity_transition_contract

    def tradable_symbols(self) -> tuple[str, ...]:
        return self._tradables

    def reference_symbols(self) -> tuple[str, ...]:
        return self._references

    def price_symbols(self) -> tuple[str, ...]:
        return self._symbols

    def members_at(self, _when: object) -> frozenset[str]:
        return frozenset(self._tradables)

    def affiliations_at(self, _when: object) -> dict[str, tuple[str, ...]]:
        return {}

    def manifest(self) -> dict[str, object]:
        return {
            "bundle_sha256": self.sha256,
            "schema_version": self.metadata["schema_version"],
            "data_cutoff": str(self.data_cutoff.date()),
            "evaluation_start": "2020-03-02",
            "warmup_start": self.metadata["warmup_start"],
            "membership_events": 0,
            "symbol_count": len(self._symbols),
            "metadata": dict(sorted(self.metadata.items())),
            "coverage": {},
        }

    def fetch_price_data(self, tickers, start_date: pd.Timestamp, end_date: pd.Timestamp):
        return {
            symbol: frame.loc[(frame.index >= start_date) & (frame.index <= end_date)].copy()
            for symbol, frame in self._prices.items()
            if symbol in set(tickers)
        }

    def fetch_closes(self, tickers, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
        return pd.DataFrame(
            {
                symbol: frame.loc[(frame.index >= start_date) & (frame.index <= end_date), "Close"]
                for symbol, frame in self._prices.items()
                if symbol in set(tickers)
            }
        ).sort_index()

    def fundamentals_provider(self, _symbol: str, _when: pd.Timestamp, **_kwargs: object) -> dict[str, object]:
        return {
            "quarterly_income": pd.DataFrame(),
            "annual_income": pd.DataFrame(),
            "balance_sheet": pd.DataFrame(),
            "company_info": {},
        }


def _real_local_evaluation(tmp_path: Path):
    authority = create_study_fixture_v1(root=tmp_path / "fixture-authority")
    registry = build_study_registry_v1()
    policy: PolicyRevisionIdentityV5 = registry.configuration("S-gte-0.50").policy_revision
    sandbox: SandboxProfileV5 = authority.manifest.sandbox_profile
    execution: ExecutionProfileV5 = authority.manifest.execution_profile
    provenance = tmp_path / "prices-provenance.json"
    provenance.write_bytes(b'{"source":"synthetic-local-test"}')
    bundle = _SyntheticDevelopmentPitBundle(provenance)
    contract = EvaluatorContractV5(
        schema_version=5,
        execution_profile_sha256=execution.sha256,
        sandbox_profile_sha256=sandbox.sha256,
        evaluator_source_sha256=sandbox.runtime_source_sha256,
        pit_bundle_sha256=bundle.sha256,
        prices_provenance_sha256=hashlib.sha256(provenance.read_bytes()).hexdigest(),
        identity_transition_contract_sha256=bundle.identity_transition_contract.request_contracts_sha256,
        baseline_source_bundle_sha256=authority.manifest.evaluator_contract.baseline_source_bundle_sha256,
        baseline_policy_revision_sha256=authority.manifest.evaluator_contract.baseline_policy_revision_sha256,
        friction_grid=initial_friction_grid_v5(),
        selection_scenario_id="base",
    )
    panel = EvaluationPanelSpec.from_lineages(
        purpose="quick",
        sessions=tuple(day.date().isoformat() for day in pd.bdate_range("2020-03-02", "2020-04-10")),
        lineages=(PanelSecurityLineage("lineage_0", ("AAA",), ("sp500",)),),
    )
    episode = EpisodePlanV5(
        episode_id="development-round-one",
        episode_ordinal=None,
        purpose="quick",
        start_date=panel.start_date,
        end_date=panel.end_date,
        lineage_ids=("lineage_0",),
        panel_ref=ArtifactRefV5("panels/development-quick.json", panel.sha256),
    )
    request = PanelExecutionRequestV5(
        schema_version=5,
        request_sha256=hashlib.sha256(b"development campaign round 1 input").hexdigest(),
        evaluator_contract=contract,
        sandbox_profile=sandbox,
        execution_profile=execution,
        policy_revision=policy,
        episode=episode,
        panel=panel,
        scenario_ids=("base",),
        policy_method_timeout_seconds=30,
        worker_startup_timeout_seconds=30,
        output_limit_bytes=sandbox.output_limit_bytes,
        pit_data_scope="development_sp500_v2",
    )
    evaluator = PitPanelEvaluatorV5(
        contract=contract,
        sandbox_profile=sandbox,
        resource_manifest=authority.manifest.manifest.resources,
        execution_profile=execution,
        pit_bundle=bundle,
        prices_provenance=provenance,
        report_builder=summarize_panel_result,
        candidate_policy_authority=policy,
        pit_data_scope="development_sp500_v2",
    )
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    evaluation = evaluator.evaluate_candidate(
        candidate_root=candidate_root.resolve(),
        panel=panel,
        policy_revision=policy,
        worker_factory=lambda *, candidate_root, policy_revision: CandidateWorkerBindingV5(
            candidate_root,
            policy_revision,
            InProcessPolicyClientV3(),
        ),
        scenario_ids=("base",),
    )
    return request, evaluation, policy


def _repository(tmp_path: Path) -> LocalArtifactRepositoryV5:
    root = tmp_path / "receipt-repository"
    root.mkdir()
    return LocalArtifactRepositoryV5(root)


def _context(policy: PolicyRevisionIdentityV5) -> DevelopmentEvaluationContextV5:
    return DevelopmentEvaluationContextV5(
        campaign_id="development-study-ledger",
        campaign_round_index=1,
        study_id="study-ledger",
        study_arm="primary",
        study_round_one_request_sha256="a" * 64,
        study_round_one_terminal_sha256="b" * 64,
        parsed_response_sha256="c" * 64,
        candidate_sha256="d" * 64,
        policy_identity_sha256=policy.sha256,
    )


def test_receipt_retains_and_authenticates_real_local_panel_output(tmp_path: Path) -> None:
    request, evaluation, policy = _real_local_evaluation(tmp_path)
    repository = _repository(tmp_path)
    context = _context(policy)

    reference = persist_inprocess_development_evaluation_v5(
        repository=repository,
        context=context,
        request=request,
        evaluation=evaluation,
    )
    authenticated = authenticate_inprocess_development_evaluation_v5(
        repository=repository,
        reference=reference,
        expected_context=context,
    )

    assert authenticated.output.evaluation == evaluation
    assert authenticated.receipt.scope == "development_sp500_v2"
    assert authenticated.receipt.evaluator_contract_sha256 == request.evaluator_contract.sha256
    assert authenticated.receipt.panel_sha256 == request.panel.sha256
    assert authenticated.receipt.universe_sha256 == request.evaluator_contract.pit_bundle_sha256
    assert authenticated.receipt.policy_identity_sha256 == policy.sha256


def test_receipt_rejects_retained_output_tampering(tmp_path: Path) -> None:
    request, evaluation, policy = _real_local_evaluation(tmp_path)
    repository = _repository(tmp_path)
    context = _context(policy)
    reference = persist_inprocess_development_evaluation_v5(
        repository=repository,
        context=context,
        request=request,
        evaluation=evaluation,
    )
    receipt, _input, _output = repository.load_development_evaluation_receipt(reference)
    output_path = repository.root.joinpath(*receipt.output_ref.relative_path.split("/"))
    output_path.write_bytes(output_path.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="digest|canonical|output"):
        authenticate_inprocess_development_evaluation_v5(
            repository=repository,
            reference=reference,
            expected_context=context,
        )


def test_receipt_rejects_identity_mismatch(tmp_path: Path) -> None:
    request, evaluation, policy = _real_local_evaluation(tmp_path)
    repository = _repository(tmp_path)
    context = _context(policy)
    reference = persist_inprocess_development_evaluation_v5(
        repository=repository,
        context=context,
        request=request,
        evaluation=evaluation,
    )

    wrong = DevelopmentEvaluationContextV5(
        campaign_id=context.campaign_id,
        campaign_round_index=context.campaign_round_index,
        study_id=context.study_id,
        study_arm=context.study_arm,
        study_round_one_request_sha256=context.study_round_one_request_sha256,
        study_round_one_terminal_sha256=context.study_round_one_terminal_sha256,
        parsed_response_sha256=context.parsed_response_sha256,
        candidate_sha256="e" * 64,
        policy_identity_sha256=context.policy_identity_sha256,
    )
    with pytest.raises(ValueError, match="identity|candidate"):
        authenticate_inprocess_development_evaluation_v5(
            repository=repository,
            reference=reference,
            expected_context=wrong,
        )


def test_study_ledger_derives_dev_feedback_from_retained_panel_result(tmp_path: Path) -> None:
    from core.pit_optimizer_v5.two_round_study.contracts import StudyAuthorityError
    from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
    from core.pit_optimizer_v5.two_round_study.live_calls import (
        StudyCallRequestV1,
        authorize_study_round_call_admission_v1,
        bind_study_call_to_development_round_slot_v1,
        bind_study_call_to_round_slot_v1,
        build_study_development_round_call_slot_v1,
        build_study_round_call_slot_v1,
    )
    from core.pit_optimizer_v5.contracts import selected_scenario
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    study_root = tmp_path / "study"
    study_root.mkdir()
    (
        _fixture,
        fixture_request,
        _preflight,
        manifest,
        base_request,
        store,
        grant,
        execution_approval,
        _initial_ledger,
        *_rest,
    ) = _study_context(study_root)
    first_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=1,
    )
    first_request = bind_study_call_to_round_slot_v1(request=base_request, slot=first_slot)
    first_admission, first_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=execution_approval,
        slots=(first_slot,),
        approval_reference="offline-fixture:development-round-one",
    )
    ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        execution_approval,
        round_call_admission=first_admission,
        round_call_approval=first_approval,
    )
    from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1

    study_registry = build_study_registry_v1()
    store.put(kind="registry", key="v1", content=study_registry.canonical_bytes())
    response = _response_for(fixture_request)
    terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {first_request.sha256: _completion(request=first_request, response_text=response.canonical_bytes().decode("utf-8"))}
        ),
        deadline_monotonic=1.0,
    )

    evaluation_root = tmp_path / "evaluation"
    evaluation_root.mkdir()
    request, evaluation, policy = _real_local_evaluation(evaluation_root)
    context = DevelopmentEvaluationContextV5(
        campaign_id="development-study-ledger",
        campaign_round_index=1,
        study_id=grant.study_id,
        study_arm=first_request.arm,
        study_round_one_request_sha256=first_request.sha256,
        study_round_one_terminal_sha256=terminal.terminal.terminal_sha256,
        parsed_response_sha256=terminal.terminal.parsed_ref.sha256,
        candidate_sha256=response.drafts[0].sha256,
        policy_identity_sha256=policy.sha256,
    )
    development_reference = persist_inprocess_development_evaluation_v5(
        repository=store.repository,
        context=context,
        request=request,
        evaluation=evaluation,
    )
    authenticated = ledger.authenticate_round_one_development_evaluation(
        request=first_request,
        development_reference=development_reference,
    )

    report = selected_scenario(evaluation).report
    expected = {
        "portfolio_annualized_return_pct": report.portfolio_annualized_return_pct,
        "portfolio_total_return_pct": report.portfolio_total_return_pct,
        "max_drawdown_pct": report.max_drawdown_pct,
        "sharpe_ratio": report.sharpe_ratio,
    }
    assert {item.metric_id: item.value for item in authenticated.result.metrics} == expected
    assert authenticated.result.panel_evaluation_sha256 == evaluation.sha256
    assert authenticated.result.development_receipt_ref == development_reference

    second_slot = build_study_development_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=2,
        round_one_evaluation=authenticated,
    )
    second_request = bind_study_call_to_development_round_slot_v1(
        request=base_request,
        slot=second_slot,
    )
    assert StudyCallRequestV1.from_canonical_json(second_request.canonical_bytes()).canonical_bytes() == (
        second_request.canonical_bytes()
    )
    assert second_request.messages[-1] == authenticated.result.feedback_message()
    assert second_slot.feedback_sha256 == authenticated.result.sha256

    second_admission, second_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=execution_approval,
        slots=(first_slot, second_slot),
        approval_reference="offline-fixture:development-round-two",
    )
    second_ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        execution_approval,
        round_call_admission=second_admission,
        round_call_approval=second_approval,
    )
    second_ledger._validate_request(second_request)

    from core.pit_optimizer_v5.two_round_study.verification import (
        _reject_development_feedback_v1,
        _reject_development_round_slot_v1,
    )

    with pytest.raises(StudyAuthorityError, match="development"):
        _reject_development_round_slot_v1(second_request)
    with pytest.raises(StudyAuthorityError, match="development"):
        _reject_development_feedback_v1(store)
