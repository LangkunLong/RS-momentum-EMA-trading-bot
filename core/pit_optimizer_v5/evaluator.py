"""Single authenticated panel-evaluation boundary for PIT optimizer V5."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from core.backtest_engine import PortfolioSimulator, SimulationResultV5
from core.backtest_fills import ExecutionProfileV5, FrictionScenario
from core.pit_data import PriceIdentityTransitionContract
from core.pit_optimizer_evaluation import EvaluationPanelSpec
from core.pit_provenance import pit_canonical_json_sha256
from core.strategy_policy import StrategyPolicyClient, StrategyPolicyClientFactory
from core.strategy_policy.runtime import InProcessPolicyClient

from .contracts import (
    EvaluationReportV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    SandboxProfileV5,
    SandboxResourceManifestV5,
    ScenarioPanelEvaluationV5,
    validate_sandbox_profile_resources_v5,
)


class SimulationRunnerV5(Protocol):
    def run(
        self,
        tickers: list[str],
        *,
        start_date: str,
        end_date: str,
        history_start_date: str,
        benchmark_symbol: str,
    ) -> SimulationResultV5: ...


SimulatorFactoryV5 = Callable[..., SimulationRunnerV5]


class EvaluationReportBuilderV5(Protocol):
    """Task-4 adapter that derives reconciled evidence from engine truth."""

    def __call__(
        self,
        *,
        panel: EvaluationPanelSpec,
        scenario: FrictionScenario,
        result: SimulationResultV5,
    ) -> EvaluationReportV5: ...


class AuthenticatedPitBundleV5(Protocol):
    sha256: str
    metadata: Mapping[str, object]

    def load_price_identity_transition_contract(
        self, prices_provenance: str | Path
    ) -> PriceIdentityTransitionContract: ...


@dataclass(frozen=True, slots=True)
class CandidateWorkerBindingV5:
    """Trusted binding between one candidate source tree and one worker."""

    candidate_root: Path
    policy_identity_sha256: str
    client: StrategyPolicyClient

    def __post_init__(self) -> None:
        root = _canonical_candidate_root(self.candidate_root)
        if type(self.candidate_root) is not type(root) or self.candidate_root != root:
            raise ValueError("candidate worker root is not canonical")
        _require_digest(self.policy_identity_sha256, "candidate worker policy identity")
        if not _is_policy_client(self.client):
            raise TypeError("candidate worker binding contains an invalid client")

    @property
    def candidate_root_sha256(self) -> str:
        return hashlib.sha256(self.candidate_root.as_posix().encode("utf-8")).hexdigest()

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            {
                "candidate_root_sha256": self.candidate_root_sha256,
                "policy_identity_sha256": self.policy_identity_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def result_provenance(self) -> dict[str, str]:
        return {
            "candidate_root_sha256": self.candidate_root_sha256,
            "policy_identity_sha256": self.policy_identity_sha256,
            "binding_sha256": self.sha256,
        }


class CandidateWorkerFactoryV5(Protocol):
    """Authenticate candidate source and create a worker bound to that source."""

    def __call__(
        self, *, candidate_root: Path, policy_identity_sha256: str
    ) -> CandidateWorkerBindingV5: ...


class _SingleUseWorkerFactory:
    """Make worker allocation observable and reject object reuse across scenarios."""

    def __init__(
        self,
        delegate: StrategyPolicyClientFactory,
        allocated_workers: list[StrategyPolicyClient],
    ) -> None:
        self._delegate = delegate
        self._allocated_workers = allocated_workers
        self.calls = 0

    def __call__(self) -> StrategyPolicyClient:
        if self.calls:
            raise ValueError("a V5 simulator may allocate exactly one policy worker")
        self.calls += 1
        worker = self._delegate()
        if not _is_policy_client(worker):
            raise TypeError("policy worker factory returned an invalid client")
        if any(worker is previous for previous in self._allocated_workers):
            try:
                worker.close()
            finally:
                raise ValueError("policy worker factory reused a worker instance")
        self._allocated_workers.append(worker)
        return worker


def _is_policy_client(value: object) -> bool:
    return (
        type(getattr(value, "interface_version", None)) is int
        and all(
            callable(getattr(value, method, None))
            for method in (
                "evaluate_entry",
                "recommend_capacity",
                "recommend_allocation",
                "select_eviction",
                "evaluate_exit",
                "close",
            )
        )
    )


def _canonical_candidate_root(value: object) -> Path:
    if type(value) is not Path and not isinstance(value, Path):
        raise ValueError("candidate root must be a Path")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("candidate root must be an absolute non-link directory")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("candidate root cannot be resolved") from exc


def _decimal_number(value: object, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError(f"{label} must be finite numeric evidence")
    return number


def _scenario_primitive(scenario: FrictionScenario) -> dict[str, int | str]:
    return {
        "scenario_id": scenario.scenario_id,
        "half_spread_bps": scenario.half_spread_bps,
        "market_impact_bps": scenario.market_impact_bps,
        "commission_bps": scenario.commission_bps,
    }


def _panel_tickers(panel: EvaluationPanelSpec) -> list[str]:
    return [
        ticker
        for lineage in panel.lineages
        for ticker in lineage.executable_tickers
    ]


def _equity_endpoints(result: SimulationResultV5) -> tuple[Decimal, Decimal]:
    curve = result.equity_curve
    try:
        if len(curve) < 2:
            raise ValueError("V5 simulation produced insufficient equity evidence")
        starting = curve.iloc[0]
        ending = curve.iloc[-1]
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError("V5 simulation equity evidence is invalid") from exc
    start_decimal = _decimal_number(starting, "starting equity", positive=True)
    end_decimal = _decimal_number(ending, "ending equity")
    if end_decimal <= 0:
        raise ValueError("ending equity must be positive")
    return start_decimal, end_decimal


class PitPanelEvaluatorV5:
    """Evaluate baseline and candidate policies under one authenticated truth."""

    def __init__(
        self,
        *,
        contract: EvaluatorContractV5,
        sandbox_profile: SandboxProfileV5,
        resource_manifest: SandboxResourceManifestV5,
        execution_profile: ExecutionProfileV5,
        pit_bundle: AuthenticatedPitBundleV5,
        prices_provenance: Path,
        report_builder: EvaluationReportBuilderV5,
        baseline_worker_factory: StrategyPolicyClientFactory | None = None,
        simulator_factory: SimulatorFactoryV5 = PortfolioSimulator,
    ) -> None:
        if type(contract) is not EvaluatorContractV5:
            raise ValueError("evaluator contract must use schema V5")
        if type(sandbox_profile) is not SandboxProfileV5:
            raise ValueError("sandbox profile must use schema V5")
        if type(execution_profile) is not ExecutionProfileV5:
            raise ValueError("execution profile must use schema V5")
        validate_sandbox_profile_resources_v5(sandbox_profile, resource_manifest)
        if sandbox_profile.sha256 != contract.sandbox_profile_sha256:
            raise ValueError("sandbox profile identity differs from evaluator contract")
        if execution_profile.sha256 != contract.execution_profile_sha256:
            raise ValueError("execution profile identity differs from evaluator contract")
        if getattr(pit_bundle, "sha256", None) != contract.pit_bundle_sha256:
            raise ValueError("PIT bundle identity differs from evaluator contract")
        if not callable(report_builder):
            raise TypeError("V5 evaluation report builder is invalid")
        if not callable(simulator_factory):
            raise TypeError("V5 simulator factory is invalid")
        baseline_factory = (
            InProcessPolicyClient
            if baseline_worker_factory is None
            else baseline_worker_factory
        )
        if not callable(baseline_factory):
            raise TypeError("baseline worker factory is invalid")

        provenance = Path(prices_provenance)
        if not provenance.is_absolute() or not provenance.is_file() or provenance.is_symlink():
            raise ValueError("prices provenance must be an absolute regular non-link file")
        provenance_sha256 = hashlib.sha256(provenance.read_bytes()).hexdigest()
        if provenance_sha256 != contract.prices_provenance_sha256:
            raise ValueError("prices provenance identity differs from evaluator contract")
        loader = getattr(pit_bundle, "load_price_identity_transition_contract", None)
        if not callable(loader):
            raise TypeError("PIT bundle cannot load price identity transitions")
        transition = loader(provenance)
        if type(transition) is not PriceIdentityTransitionContract:
            raise ValueError("price identity transition contract is invalid")
        if transition.prices_provenance_sha256 != provenance_sha256:
            raise ValueError("prices provenance identity differs from evaluator contract")
        canonical_identities = {
            ticker: dict(identity)
            for ticker, identity in transition.identities.items()
        }
        if (
            transition.request_contracts_sha256
            != contract.identity_transition_contract_sha256
            or pit_canonical_json_sha256(canonical_identities)
            != contract.identity_transition_contract_sha256
        ):
            raise ValueError("identity transition contract identity differs")

        metadata = getattr(pit_bundle, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise ValueError("PIT bundle metadata is unavailable")
        warmup_start = metadata.get("warmup_start")
        if type(warmup_start) is not str:
            raise ValueError("PIT bundle warmup start is unavailable")
        try:
            date.fromisoformat(warmup_start)
        except ValueError as exc:
            raise ValueError("PIT bundle warmup start is invalid") from exc

        self._contract = contract
        self._sandbox_profile = sandbox_profile
        self._execution_profile = execution_profile
        self._pit_bundle = pit_bundle
        self._transition_contract = transition
        self._warmup_start = warmup_start
        self._report_builder = report_builder
        self._baseline_worker_factory = baseline_factory
        self._simulator_factory = simulator_factory

    @property
    def transition_contract(self) -> PriceIdentityTransitionContract:
        return self._transition_contract

    def evaluate_baseline(
        self,
        panel: EvaluationPanelSpec,
        *,
        scenario_ids: tuple[str, ...],
    ) -> PanelEvaluationV5:
        return self._evaluate(
            panel=panel,
            policy_identity_sha256=self._contract.baseline_source_bundle_sha256,
            worker_factory=self._baseline_worker_factory,
            scenario_ids=scenario_ids,
        )

    def evaluate_candidate(
        self,
        *,
        candidate_root: Path,
        panel: EvaluationPanelSpec,
        policy_identity_sha256: str,
        worker_factory: CandidateWorkerFactoryV5,
        scenario_ids: tuple[str, ...],
    ) -> PanelEvaluationV5:
        root = _canonical_candidate_root(candidate_root)
        if Path(candidate_root) != root:
            raise ValueError("candidate root must be supplied in canonical form")
        return self._evaluate(
            panel=panel,
            policy_identity_sha256=policy_identity_sha256,
            worker_factory=None,
            candidate_root=root,
            candidate_worker_factory=worker_factory,
            scenario_ids=scenario_ids,
        )

    def _evaluate(
        self,
        *,
        panel: EvaluationPanelSpec,
        policy_identity_sha256: str,
        worker_factory: StrategyPolicyClientFactory | None,
        candidate_root: Path | None = None,
        candidate_worker_factory: CandidateWorkerFactoryV5 | None = None,
        scenario_ids: tuple[str, ...],
    ) -> PanelEvaluationV5:
        if type(panel) is not EvaluationPanelSpec:
            raise ValueError("evaluation panel must use the authenticated panel schema")
        _require_digest(policy_identity_sha256, "policy identity")
        if (worker_factory is None) == (candidate_worker_factory is None):
            raise TypeError("evaluation requires exactly one worker factory kind")
        if worker_factory is not None and not callable(worker_factory):
            raise TypeError("policy worker factory is invalid")
        if candidate_worker_factory is not None and not callable(
            candidate_worker_factory
        ):
            raise TypeError("candidate worker factory is invalid")
        if (candidate_root is None) is not (candidate_worker_factory is None):
            raise ValueError("candidate worker construction is incompletely bound")
        scenarios = self._requested_scenarios(scenario_ids)
        panel_start = date.fromisoformat(panel.start_date)
        if date.fromisoformat(self._warmup_start) > panel_start:
            raise ValueError("PIT bundle warmup begins after the panel")

        allocated_workers: list[StrategyPolicyClient] = []
        scenario_evidence: list[ScenarioPanelEvaluationV5] = []
        for scenario in scenarios:
            candidate_binding: CandidateWorkerBindingV5 | None = None
            scenario_worker_factory = worker_factory
            if candidate_worker_factory is not None:
                assert candidate_root is not None
                binding = candidate_worker_factory(
                    candidate_root=candidate_root,
                    policy_identity_sha256=policy_identity_sha256,
                )
                if type(binding) is not CandidateWorkerBindingV5:
                    raise TypeError("candidate worker factory returned an invalid binding")
                if (
                    binding.candidate_root != candidate_root
                    or binding.policy_identity_sha256 != policy_identity_sha256
                ):
                    try:
                        binding.client.close()
                    finally:
                        raise ValueError("candidate worker binding differs from the request")
                candidate_binding = binding
                scenario_worker_factory = lambda binding=binding: binding.client
            assert scenario_worker_factory is not None
            single_worker = _SingleUseWorkerFactory(
                scenario_worker_factory, allocated_workers
            )
            simulator = self._simulator_factory(
                pit_bundle=self._pit_bundle,
                benchmark_symbol=self._contract.benchmark,
                signal_every_n_days=self._contract.signal_every_n_days,
                identity_transition_contract=self._transition_contract,
                policy_client_factory=single_worker,
                execution_profile=self._execution_profile,
                friction_scenario=scenario,
            )
            if not callable(getattr(simulator, "run", None)):
                raise TypeError("V5 simulator factory returned an invalid simulator")
            result = simulator.run(
                _panel_tickers(panel),
                start_date=panel.start_date,
                end_date=panel.end_date,
                history_start_date=self._warmup_start,
                benchmark_symbol=self._contract.benchmark,
            )
            if single_worker.calls != 1:
                raise ValueError("V5 simulation did not allocate exactly one policy worker")
            self._bind_candidate_result(result, candidate_binding)
            self._validate_result(
                result,
                panel=panel,
                scenario=scenario,
                candidate_binding=candidate_binding,
            )
            report = self._report_builder(
                panel=panel,
                scenario=scenario,
                result=result,
            )
            if type(report) is not EvaluationReportV5:
                raise ValueError("report builder returned non-V5 evidence")
            starting_equity, ending_equity = _equity_endpoints(result)
            scenario_evidence.append(
                ScenarioPanelEvaluationV5(
                    scenario_id=scenario.scenario_id,
                    starting_equity=starting_equity,
                    ending_equity=ending_equity,
                    report=report,
                )
            )

        return PanelEvaluationV5(
            evaluator_contract_sha256=self._contract.sha256,
            sandbox_profile_sha256=self._sandbox_profile.sha256,
            panel_sha256=panel.sha256,
            policy_identity_sha256=policy_identity_sha256,
            start_date=panel.start_date,
            end_date=panel.end_date,
            elapsed_calendar_days=(
                date.fromisoformat(panel.end_date) - date.fromisoformat(panel.start_date)
            ).days,
            selection_scenario_id=self._contract.selection_scenario_id,
            scenarios=tuple(scenario_evidence),
        )

    def _requested_scenarios(
        self, scenario_ids: tuple[str, ...]
    ) -> tuple[FrictionScenario, ...]:
        if type(scenario_ids) is not tuple or not scenario_ids or any(
            type(item) is not str for item in scenario_ids
        ):
            raise ValueError("requested scenarios must be a non-empty tuple")
        grid_by_id = {
            scenario.scenario_id: scenario for scenario in self._contract.friction_grid
        }
        if len(set(scenario_ids)) != len(scenario_ids) or any(
            scenario_id not in grid_by_id for scenario_id in scenario_ids
        ):
            raise ValueError("requested scenarios are not a subset of the contract grid")
        if self._contract.selection_scenario_id not in scenario_ids:
            raise ValueError("requested scenarios must contain the selection scenario")
        canonical = tuple(
            scenario.scenario_id
            for scenario in self._contract.friction_grid
            if scenario.scenario_id in scenario_ids
        )
        if scenario_ids != canonical:
            raise ValueError("requested scenarios are not in canonical grid order")
        return tuple(grid_by_id[scenario_id] for scenario_id in scenario_ids)

    def _validate_result(
        self,
        result: object,
        *,
        panel: EvaluationPanelSpec,
        scenario: FrictionScenario,
        candidate_binding: CandidateWorkerBindingV5 | None,
    ) -> None:
        if type(result) is not SimulationResultV5:
            raise ValueError("simulator returned non-V5 aggregate evidence")
        config = result.config
        if not isinstance(config, Mapping):
            raise ValueError("V5 result configuration is absent")
        expected = {
            "pit_bundle_sha256": self._contract.pit_bundle_sha256,
            "execution_profile_sha256": self._contract.execution_profile_sha256,
            "friction_scenario": _scenario_primitive(scenario),
            "signal_every_n_days": self._contract.signal_every_n_days,
            "start_date": panel.start_date,
            "end_date": panel.end_date,
        }
        if any(config.get(key) != value for key, value in expected.items()):
            raise ValueError("V5 simulation result identity differs from evaluator inputs")
        expected_binding = (
            None
            if candidate_binding is None
            else candidate_binding.result_provenance()
        )
        if config.get("candidate_worker_binding") != expected_binding:
            raise ValueError("V5 result candidate worker provenance differs")
        if result.benchmark_symbol != self._contract.benchmark:
            raise ValueError("V5 simulation benchmark identity differs")
        _equity_endpoints(result)

    @staticmethod
    def _bind_candidate_result(
        result: object, binding: CandidateWorkerBindingV5 | None
    ) -> None:
        if type(result) is not SimulationResultV5:
            return
        config = result.config
        if not isinstance(config, Mapping):
            return
        expected = None if binding is None else binding.result_provenance()
        existing = config.get("candidate_worker_binding")
        if existing is not None and existing != expected:
            raise ValueError("simulator emitted conflicting candidate worker provenance")
        result.config = dict(config)
        if expected is None:
            result.config.pop("candidate_worker_binding", None)
        else:
            result.config["candidate_worker_binding"] = expected


def _require_digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "AuthenticatedPitBundleV5",
    "CandidateWorkerBindingV5",
    "CandidateWorkerFactoryV5",
    "EvaluationReportBuilderV5",
    "PitPanelEvaluatorV5",
    "SimulationRunnerV5",
    "SimulatorFactoryV5",
]
