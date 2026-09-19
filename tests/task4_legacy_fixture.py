"""Deterministic extension-absent runtime seam used for Task 4 provenance."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import time
from types import SimpleNamespace

from core.pit_optimizer_v5.artifacts import ArtifactRefV5
from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    RenderedVariantV5,
    SourceBundleV5,
    SourceFileV5,
    SourceOperationV5,
    StructuralTemplateV5,
    VariantAssignmentV5,
    derive_policy_revision_identity_v5,
    derive_experiment_identity_v5,
)
from core.pit_optimizer_v5.contracts import (
    EvaluationReportV5,
    PanelEvaluationV5,
    ScenarioPanelEvaluationV5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.memory import (
    CandidateStageResultPayloadV5,
    RenderedVariantPayloadV5,
    RoundIntentPayloadV5,
    round_event_payload_primitive_v5,
)
from core.pit_optimizer_v5.probes import (
    PROBE_SUITE_ID_V5,
    ProbeObservationV5,
    SemanticFingerprintV5,
    policy_probe_suite_v1,
)
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.runtime import MaterializedVariantV5, _Runtime
from core.pit_optimizer_v5.search import ParentCandidateV5, hypothesis_novelty_key_v5
from core.pit_optimizer_v5.selection import ScheduledHypothesisV5
from core.pit_optimizer_v5.contracts import HypothesisV5, MetricPredictionV5, ValidationResultV5


LEGACY_BASE_COMMIT_V5 = "bc2420bb79045c33d2ef5b088d8fda4ce2e118c6"
LEGACY_BASELINE_COMMAND_V5 = "PYTHON_DOTENV_DISABLED=1 py -3.13 -B -m tests.task4_legacy_fixture"
LEGACY_GOLDEN_JSON_SHA256_V5 = "6519c19f79818efed51776caceaea87254fe3df1b79d03f502a0804b9cf32c2a"


class _Persistence:
    def __init__(self) -> None:
        self.events = []
        self.payloads = {}

    def load_round_events(self, *, campaign_id: str, round_index: int):
        return tuple(self.events)

    def load_round_payload(self, reference: ArtifactRefV5, *, expected_kind: str):
        return self.payloads[reference.sha256]

    def append_round_payload(self, payload):
        digest = canonical_sha256_v5(round_event_payload_primitive_v5(payload))
        event_kind = {
            "RoundIntentPayloadV5": "round_intent",
            "RenderedVariantPayloadV5": "rendered_variant",
            "CandidateStageResultPayloadV5": "candidate_stage_result",
            "QuickEvidencePayloadV5": "quick_evidence",
        }[type(payload).__name__]
        reference = ArtifactRefV5(f"payloads/{event_kind}/{digest}.json", digest)
        self.payloads[digest] = payload
        return reference

    def append_round_event(self, event):
        self.events.append(event)
        return ArtifactRefV5(
            f"events/{event.campaign_id}/{event.round_index:04d}/{event.sequence:06d}.json",
            event.sha256,
        )

    def append_input(self, *, artifact_kind: str, value: object):
        digest = canonical_sha256_v5(value)
        return ArtifactRefV5(f"inputs/{artifact_kind}/{digest}.json", digest)


class _Clock:
    def __init__(self, value: float | None = None) -> None:
        self.value = value

    def monotonic(self) -> float:
        return time.monotonic() if self.value is None else self.value


class _Cancellation:
    def is_cancelled(self) -> bool:
        return False


class _CandidateRuntime:
    def __init__(
        self,
        fingerprint: SemanticFingerprintV5,
        source_ref: ArtifactRefV5,
        *,
        validation_valid: bool = True,
        changed_symbols: tuple[str, ...] = ("core.strategy_policy.v3.exit.evaluate_exit",),
        evaluator_contract_sha256: str = "a" * 64,
        sandbox_profile_sha256: str = "b" * 64,
        panel_sha256: str = "c" * 64,
        start_date: str = "2025-01-01",
        end_date: str = "2025-01-02",
    ) -> None:
        self.fingerprint_value = fingerprint
        self.source_ref = source_ref
        self.validation_valid = validation_valid
        self.changed_symbols = changed_symbols
        self.evaluator_contract_sha256 = evaluator_contract_sha256
        self.sandbox_profile_sha256 = sandbox_profile_sha256
        self.panel_sha256 = panel_sha256
        self.start_date = start_date
        self.end_date = end_date
        self.calls = {"materialize": 0, "validate": 0, "fingerprint": 0, "evaluate_quick": 0}

    def materialize(self, *, inputs, experiment_identity, variant, deadline):
        self.calls["materialize"] += 1
        return MaterializedVariantV5(
            variant=variant,
            source_bundle_ref=self.source_ref,
            leases=(),
            opaque_candidate="task4-supplied-candidate-handle",
        )

    def recover_materialized(self, **kwargs):
        kwargs.pop("leases", None)
        return self.materialize(**kwargs)

    def validate(self, materialized, *, deadline):
        self.calls["validate"] += 1
        return ValidationResultV5(self.validation_valid, None, self.changed_symbols)

    def fingerprint(self, materialized, *, deadline):
        self.calls["fingerprint"] += 1
        return self.fingerprint_value

    def evaluate_quick(self, materialized, *, deadline):
        self.calls["evaluate_quick"] += 1
        report = EvaluationReportV5(
            portfolio_annualized_return_pct=Decimal("0"),
            portfolio_total_return_pct=Decimal("0"),
            gross_annualized_return_pct=Decimal("0"),
            benchmark_annualized_return_pct=Decimal("0"),
            benchmark_total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=Decimal("0"),
            closed_trades=0,
            average_exposure_pct=Decimal("0"),
            average_cash_pct=Decimal("0"),
            turnover_pct=Decimal("0"),
            total_friction_usd=Decimal("0"),
            friction_drag_pct=Decimal("0"),
            win_rate_pct=None,
            average_win_pct=None,
            average_loss_pct=None,
            payoff_ratio=None,
            expectancy_pct=None,
            median_holding_sessions=None,
            invested_sleeve_annualized_return_pct=None,
            estimated_idle_cash_drag_pct=Decimal("0"),
            stop_gap_shortfall_usd=Decimal("0"),
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
        return PanelEvaluationV5(
            evaluator_contract_sha256=self.evaluator_contract_sha256,
            sandbox_profile_sha256=self.sandbox_profile_sha256,
            panel_sha256=self.panel_sha256,
            policy_identity_sha256=materialized.variant.policy_revision.sha256,
            start_date=self.start_date,
            end_date=self.end_date,
            elapsed_calendar_days=1,
            selection_scenario_id="base",
            scenarios=(ScenarioPanelEvaluationV5("base", Decimal("100"), Decimal("100"), report),),
        )


def _bundle(suffix: str = "") -> SourceBundleV5:
    root = Path(__file__).resolve().parents[1]
    files = []
    for index, path in enumerate(EDITABLE_POLICY_PATHS_V5):
        source = (root / path).read_text(encoding="utf-8")
        if suffix and index == len(EDITABLE_POLICY_PATHS_V5) - 1:
            source = source.rstrip("\n") + f"\n# {suffix}\n"
        files.append(SourceFileV5(path, source))
    return SourceBundleV5(tuple(files))


def _fingerprint() -> SemanticFingerprintV5:
    from core.strategy_policy.contracts import (
        AllocationDecision,
        CapacityDecision,
        EntryDecision,
        EvictionDecision,
        ExitDecision,
    )
    from core.strategy_policy.contracts_v3 import AddOnDecisionV3

    observations = []
    for case in policy_probe_suite_v1():
        if case.method == "evaluate_entry":
            decision = EntryDecision(False, True, (None, None), ())
        elif case.method == "recommend_capacity":
            decision = CapacityDecision(None, False)
        elif case.method == "recommend_allocation":
            decision = AllocationDecision(0.01, 0.01, None)
        elif case.method == "select_eviction":
            decision = EvictionDecision(None)
        elif case.method == "evaluate_add_on":
            decision = AddOnDecisionV3(False, 0.0, None, "hold")
        else:
            decision = ExitDecision(
                (),
                None,
                case.snapshot.base.early_winner_hold,
                case.snapshot.base.scale_out_tier,
                case.snapshot.base.breakeven_armed,
                case.snapshot.base.ema_trailing_active,
            )
        observations.append(
            ProbeObservationV5(case.probe_id, case.method, case.input_sha256, decision.to_canonical_json().encode())
        )
    digest = canonical_sha256_v5(
        {"suite_id": PROBE_SUITE_ID_V5, "observations": tuple(item.to_primitive() for item in observations)}
    )
    return SemanticFingerprintV5(PROBE_SUITE_ID_V5, tuple(observations), digest)


def _run(
    *,
    mechanism=None,
    round_intent: RoundIntentPayloadV5 | None = None,
    parent_candidate: ParentCandidateV5 | None = None,
    candidate_suffix: str = "candidate fixture",
    semantic_kind: str = "distinct",
    validation_valid: bool = True,
    recovered_semantic: bool = False,
    clock_value: float | None = None,
    authenticated_manifest=None,
    return_candidate: bool = False,
) -> dict[str, object]:
    if semantic_kind not in {"distinct", "equivalent", "sibling"}:
        raise ValueError("unsupported legacy fixture semantic kind")
    parent_bundle = _bundle()
    candidate_bundle = _bundle(candidate_suffix)
    parent_revision = derive_policy_revision_identity_v5(
        source_bundle=parent_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    candidate_revision = derive_policy_revision_identity_v5(
        source_bundle=candidate_bundle,
        trusted_policy_runtime_sha256="1" * 64,
        immutable_constraints_sha256="2" * 64,
    )
    parent_ref = ArtifactRefV5("parents/source.json", parent_bundle.sha256)
    if authenticated_manifest is not None:
        parent_ref = authenticated_manifest.baseline_authority.source_bundle_ref
    parent = parent_candidate or ParentCandidateV5(
        origin="baseline",
        policy_revision=parent_revision,
        semantic_fingerprint_sha256=_fingerprint().fingerprint_sha256,
        campaign_cagr_pct=Decimal("0.00"),
        source_bundle_ref=parent_ref,
        experiment_record_ref=None,
        primary_mechanism=None,
        admitted_round=None,
    )
    parent_revision = parent.policy_revision
    parent_ref = parent.source_bundle_ref
    hypothesis = HypothesisV5(
        hypothesis_id="hyp.exit.atr",
        rank=1,
        primary_mechanism="exit",
        causal_claim="ATR-aware exit behavior changes exit decisions.",
        predicted_changes=(
            MetricPredictionV5("exit.decision_changed_count", "increase", "Applicable cases should change."),
        ),
        evidence_ids=("v5.fixture.evidence",),
        author_instructions="Use the registered exit mechanism recipe.",
    )
    novelty_key = hypothesis_novelty_key_v5(parent_revision_sha256=parent.policy_identity_sha256, hypothesis=hypothesis)
    decision = ScheduledHypothesisV5(parent, hypothesis, novelty_key)
    operation = SourceOperationV5(
        path="core/strategy_policy/v3/exit.py",
        symbol="evaluate_exit",
        kind="replace_function",
        replacement_source="def evaluate_exit(*args, **kwargs):\n    return None\n",
    )
    template = StructuralTemplateV5(
        hypothesis_id=hypothesis.hypothesis_id,
        parent_revision_sha256=parent.policy_identity_sha256,
        changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
        source_operations=(operation,),
        axes=(LiteralAxisV5("atr_20_fraction", 0.20, (0.20,)),),
        full_source_escape=None,
    )
    variant = RenderedVariantV5(
        assignment=VariantAssignmentV5((("atr_20_fraction", 0.20),)),
        source_bundle=candidate_bundle,
        policy_revision=candidate_revision,
    )
    persistence = _Persistence()
    if authenticated_manifest is None:
        quick_evaluator_contract_sha256 = "a" * 64
        quick_sandbox_profile_sha256 = "b" * 64
        quick_panel_sha256 = "c" * 64
        quick_start_date = "2025-01-01"
        quick_end_date = "2025-01-02"
    else:
        quick_evaluator_contract_sha256 = authenticated_manifest.evaluator_contract.sha256
        quick_sandbox_profile_sha256 = authenticated_manifest.evaluator_contract.sandbox_profile_sha256
        quick_panel_sha256 = authenticated_manifest.panel_plan.quick.panel_ref.sha256
        quick_start_date = authenticated_manifest.panel_plan.quick.start_date
        quick_end_date = authenticated_manifest.panel_plan.quick.end_date
    candidate_runtime = _CandidateRuntime(
        _fingerprint(),
        ArtifactRefV5("candidates/source.json", candidate_bundle.sha256),
        validation_valid=validation_valid,
        changed_symbols=(() if candidate_suffix == "" else ("core.strategy_policy.v3.exit.evaluate_exit",)),
        evaluator_contract_sha256=quick_evaluator_contract_sha256,
        sandbox_profile_sha256=quick_sandbox_profile_sha256,
        panel_sha256=quick_panel_sha256,
        start_date=quick_start_date,
        end_date=quick_end_date,
    )
    # Keep the fixture behaviorally distinct while preserving the same static
    # candidate identity across base and extension-absent runs.
    observations = list(candidate_runtime.fingerprint_value.observations)
    for index, item in enumerate(observations):
        if item.probe_id == "exit_winner_scale_boundary":
            case = next(case for case in policy_probe_suite_v1() if case.probe_id == item.probe_id)
            from core.strategy_policy.contracts import ExitDecision

            decision_value = ExitDecision(
                (),
                case.snapshot.base.protective_stop_candidates[-1],
                case.snapshot.base.early_winner_hold,
                case.snapshot.base.scale_out_tier,
                case.snapshot.base.breakeven_armed,
                case.snapshot.base.ema_trailing_active,
            )
            observations[index] = ProbeObservationV5(
                item.probe_id,
                item.method,
                item.input_sha256,
                decision_value.to_canonical_json().encode(),
            )
    candidate_fingerprint = SemanticFingerprintV5(
        PROBE_SUITE_ID_V5,
        tuple(observations),
        canonical_sha256_v5(
            {"suite_id": PROBE_SUITE_ID_V5, "observations": tuple(item.to_primitive() for item in observations)}
        ),
    )
    if semantic_kind == "equivalent":
        candidate_fingerprint = _fingerprint()
    candidate_runtime.fingerprint_value = candidate_fingerprint
    if authenticated_manifest is None:
        manifest = SimpleNamespace(
            semantic_mode="required",
            pit_data_scope="production",
            resources=SimpleNamespace(
                round_wall_timeout_seconds=120,
                worker_startup_timeout_seconds=30,
                mechanics_timeout_seconds=60,
                quick_timeout_seconds=60,
                cleanup_timeout_seconds=30,
            ),
        )
        panel_plan = SimpleNamespace(
            discovery_plan_sha256=("e" * 64 if round_intent is None else round_intent.discovery_plan_sha256),
            quick=SimpleNamespace(
                panel_ref=ArtifactRefV5("panels/quick.json", quick_panel_sha256),
                start_date=quick_start_date,
                end_date=quick_end_date,
            ),
        )
        evaluator_contract = SimpleNamespace(
            sha256=quick_evaluator_contract_sha256,
            sandbox_profile_sha256=quick_sandbox_profile_sha256,
        )
        baseline = SimpleNamespace(semantic_fingerprint=_fingerprint())
        campaign_id = "task4-legacy"
    else:
        manifest = authenticated_manifest.manifest
        panel_plan = authenticated_manifest.panel_plan
        evaluator_contract = authenticated_manifest.evaluator_contract
        baseline = authenticated_manifest.baseline_authority
        campaign_id = manifest.campaign_id
    inputs = SimpleNamespace(
        campaign_id=campaign_id,
        round_index=1,
        owner_token_sha256="d" * 64,
        manifest=manifest,
        panel_plan=panel_plan,
        evaluator_contract=evaluator_contract,
        baseline=baseline,
    )
    dependencies = SimpleNamespace(
        persistence=persistence,
        candidates=candidate_runtime,
        clock=_Clock(clock_value),
        cancellation=_Cancellation(),
        mechanism=mechanism,
    )
    runtime = _Runtime(inputs, dependencies)
    intent = round_intent or RoundIntentPayloadV5(
        parent_revision_sha256=parent.policy_identity_sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        hypothesis=hypothesis,
        discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
    )
    runtime._round_intent(intent)
    if mechanism is not None:
        mechanism.before_authoring(
            round_intent=intent,
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
            parent_candidate=parent,
        )
    if recovered_semantic:
        identity = derive_experiment_identity_v5(
            policy_revision=variant.policy_revision,
            parent_revision_sha256=parent.policy_identity_sha256,
            hypothesis=decision.hypothesis,
            template=template,
            assignment=variant.assignment,
            round_index=inputs.round_index,
            discovery_plan_sha256=inputs.panel_plan.discovery_plan_sha256,
        )
        runtime.journal.append(
            RenderedVariantPayloadV5(variant=variant),
            experiment_id=identity.sha256,
        )
        runtime.journal.append(
            CandidateStageResultPayloadV5(
                experiment_id=identity.sha256,
                stage="validation",
                stage_index=1,
                outcome="validation_valid",
                validation=ValidationResultV5(
                    valid=True,
                    failure_code=None,
                    changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
                ),
            ),
            experiment_id=identity.sha256,
        )
        runtime.journal.append(
            CandidateStageResultPayloadV5(
                experiment_id=identity.sha256,
                stage="semantic_probe",
                stage_index=2,
                outcome="behaviorally_distinct",
                semantic_fingerprint=candidate_fingerprint,
            ),
            experiment_id=identity.sha256,
        )
    if semantic_kind == "sibling":
        runtime._seen_semantic_fingerprints.add(candidate_fingerprint.fingerprint_sha256)
    candidate = runtime._initial_candidate(template=template, variant=variant, parent=parent, decision=decision)
    events = [
        {
            "kind": event.event_kind,
            "sequence": event.sequence,
            "sha256": event.sha256,
            "payload_sha256": event.payload_ref.sha256,
        }
        for event in persistence.events
    ]
    result = {
        "messages": [
            canonical_sha256_v5(round_event_payload_primitive_v5(payload))
            for payload in persistence.payloads.values()
            if type(payload) is RoundIntentPayloadV5
        ],
        "records": events,
        "decision": {"status": candidate.status, "experiment_id": candidate.experiment_id},
        "fingerprints": {
            "parent": parent.semantic_fingerprint_sha256,
            "candidate": candidate.semantic_fingerprint.fingerprint_sha256 if candidate.semantic_fingerprint else None,
        },
        "call_counts": candidate_runtime.calls,
    }
    if return_candidate:
        result["_candidate"] = candidate
    return result


if __name__ == "__main__":
    print(json.dumps(_run(), sort_keys=True, separators=(",", ":")))
