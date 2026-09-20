"""Task 6 runtime-port and explicit critic-routing checks."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from core.pit_optimizer_artifacts import _windows_extended_path
from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import (
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    canonical_json_bytes_v5,
    canonical_sha256_v5,
)
from core.pit_optimizer_v5.mechanism_artifacts import (
    MechanismArtifactRepositoryV5,
    MechanismCapabilityError,
)
from core.pit_optimizer_v5.mechanism_contracts import MechanismExecutionV1, MechanismPredictionResultV1
from core.pit_optimizer_v5.memory import (
    NoNovelHypothesisAuthorityV5,
    NoveltyExhaustedAuthorityV5,
    ResourceLeasePayloadV5,
    RoleCompletionPayloadV5,
    RoundOutcomePayloadV5,
    RoundIntentPayloadV5,
    RuntimeFailureAuthorityV5,
    SchedulingCursorV5,
    StoredExperimentRecordV5,
)
from core.pit_optimizer_v5.provider import (
    AuthorRoleInputV5,
    FixtureRoleRunnerV5,
    FixtureRoleTerminalAuthorityV5,
    MechanismRoleInputV1,
    RoleAttemptFactsV5,
    RoleCallKeyV5,
    RoleFailureCode,
    RoleInvocationPackageV5,
    RoleUsageFactsV5,
)
from core.pit_optimizer_v5.provider import ControllerRoleResponsePendingV5
from core.pit_optimizer_v5.production_runtime import LocalArchiveReducerFactoryV5, LocalRoleRequestFactoryV5
from core.pit_optimizer_v5.runtime import FeedbackRoundInputV5, SearchProjectionV5, run_feedback_round_v5
from core.pit_optimizer_v5.search import annualized_return_pct, baseline_parent_candidate_v5
from core.pit_optimizer_v5.selection import select_parent_v5
from core.pit_optimizer_v5.two_round_study.fixtures import create_study_fixture_v1
from core.pit_optimizer_v5.two_round_study.contracts import (
    StudyManifestV1,
    StudyOfflineSettingsV1,
    StudyResponseV1,
)
from core.pit_optimizer_v5.two_round_study.compiler import StudyCommitmentIndexV1
from core.pit_optimizer_v5.two_round_study.imports import create_study_import_v1
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
from core.pit_optimizer_v5.two_round_study.live_calls import (
    FixturePreflightV1,
    StudyGrantV1,
    authorize_study_execution_v1,
    build_study_call_v1,
    study_parser_authority_bytes_v1,
    study_prompt_bytes_v1,
)
from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1
from core.pit_optimizer_v5.two_round_study.schema import study_response_schema_v1
from core.pit_optimizer_v5.two_round_study.runtime_ports import (
    StudyRequestRouterV1,
    _investigator_artifact,
    scripted_response_v1,
    verify_scripted_role_authority_v1,
    prepare_scripted_role_authority_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1

from tests.test_pit_optimizer_v5_study_contracts import _draft
from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion


def _round_two_import_for_withheld_arm(tmp_path: Path, fixture, primary_fixture, inputs, projection, parent):
    """Build one authenticated offline F/L import against the round-one checkpoint."""

    factory = LocalRoleRequestFactoryV5(repository=fixture.repository, manifest=inputs.manifest)
    fixture_request = factory.investigator_request(inputs, projection, parent)
    checkpoint = projection.checkpoint
    assert checkpoint is not None
    checkpoint_bytes = canonical_json_bytes_v5(checkpoint.to_primitive())
    checkpoint_ref = ArtifactRefV5("checkpoint.json", hashlib.sha256(checkpoint_bytes).hexdigest())
    snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    snapshot_bytes = fixture.repository.authenticate_exact(snapshot_ref).content
    call = RoleCallKeyV5(
        campaign_id=inputs.campaign_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=fixture_request.sha256,
    )
    prepare_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=fixture.manifest,
        registry=fixture.registry,
        round_index=2,
    )
    assert verify_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=fixture.manifest,
        registry=fixture.registry,
        round_index=2,
    )
    request_ref = fixture.repository.append_role_request(call=call, request=fixture_request).reference
    preflight = FixturePreflightV1(
        arm="withheld",
        mode="offline_fixture",
        fixture_root_identity_sha256=fixture.repository.root_identity_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        call=call,
        request_ref=request_ref,
        request_bytes=fixture.repository.authenticate_exact(request_ref).content,
        schema_json=fixture_request.schema_authority.canonical_schema_json,
        evidence_refs=(),
        request_sha256=fixture_request.sha256,
        fixture_root_locator=str(fixture.repository.root),
        checkpoint_bytes=checkpoint_bytes,
        snapshot_bytes=snapshot_bytes,
        evidence_bytes=(),
    )
    primary_repository = primary_fixture.repository
    prepare_scripted_role_authority_v1(
        repository=primary_repository,
        manifest=primary_fixture.manifest,
        registry=primary_fixture.registry,
        round_index=2,
    )
    primary_request_ref = primary_repository.append_role_request(call=call, request=fixture_request).reference
    primary_preflight = replace(
        preflight,
        arm="primary",
        fixture_root_identity_sha256=primary_repository.root_identity_sha256,
        request_ref=primary_request_ref,
        request_bytes=primary_repository.authenticate_exact(primary_request_ref).content,
        fixture_root_locator=str(primary_repository.root),
    )
    study_root = tmp_path / "study-ledger"
    study_root.mkdir()
    store = StudyStoreV1(LocalArtifactRepositoryV5(study_root))
    primary_ref = store.put(kind="preflights", key="primary", content=primary_preflight.canonical_bytes())
    withheld_ref = store.put(kind="preflights", key="withheld", content=preflight.canonical_bytes())
    rubric = b"synthetic-task6-rubric-v1"
    store.put(kind="rubrics", key="unit", content=rubric)
    registry = build_study_registry_v1()
    schema = study_response_schema_v1(
        fixture_request=fixture_request,
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    manifest = StudyManifestV1(
        schema_version=1,
        study_id="study-runtime-ports",
        mode="offline_fixture",
        source_revision="a" * 40,
        fixture_sha256=fixture.manifest.manifest_ref.sha256,
        registry_sha256=registry.sha256,
        rubric_sha256=hashlib.sha256(rubric).hexdigest(),
        schema_sha256=hashlib.sha256(schema).hexdigest(),
        parser_sha256=hashlib.sha256(study_parser_authority_bytes_v1()).hexdigest(),
        prompt_sha256=hashlib.sha256(study_prompt_bytes_v1(registry=registry)).hexdigest(),
        round_one_checkpoint_ref=checkpoint_ref,
        round_one_snapshot_ref=snapshot_ref,
        primary_preflight_ref=primary_ref,
        withheld_preflight_ref=withheld_ref,
        resource_limits=fixture.resource_budget,
        provider_settings=None,
        offline_settings=StudyOfflineSettingsV1("fixture-test"),
    )
    live_request = build_study_call_v1(
        preflight=preflight,
        manifest=manifest,
        fixture_request=fixture_request,
        registry=registry,
    )
    grant = StudyGrantV1(
        study_id=manifest.study_id,
        manifest_sha256=manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="study-audit",
        mode="offline_fixture",
        provider="offline_fixture",
        model="offline_fixture/study-v1",
        arm_slots=("primary", "withheld"),
        input_token_ceiling=1_000_000,
        output_token_ceiling=4096,
        cumulative_token_ceiling=1_000_000,
        per_call_usd_ceiling=Decimal("1"),
        cumulative_usd_ceiling=Decimal("2"),
        per_call_deadline_seconds=Decimal("120"),
        input_price_upper_bound=Decimal("1"),
        output_price_upper_bound=Decimal("1"),
        response_persistence_consent=True,
        operator_approval_reference="offline-fixture:test",
    )
    approval = authorize_study_execution_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    ledger = StudyLedgerV1(store, manifest, grant, approval)
    artifact = _investigator_artifact(fixture_request)
    hypothesis = artifact.hypotheses[0]
    draft = replace(
        _draft(),
        hypothesis_id=hypothesis.hypothesis_id,
        cited_evidence_ids=hypothesis.evidence_ids,
        configuration_id="S-gte-0.05",
        expected_changed=(True, True, True, False),
    )
    response = StudyResponseV1(1, fixture_request.expected_binding, artifact, (draft,))
    terminal = run_study_call_v1(
        request=live_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                live_request.sha256: _completion(
                    request=live_request,
                    response_text=response.canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    return store, ledger, imported


def _fixture_role_failure_package(persisted) -> RoleInvocationPackageV5:
    usage = RoleUsageFactsV5(
        external_attempt_count=0,
        request_started=False,
        response_received=False,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=Decimal("0"),
        requested_model=None,
        returned_model=None,
        provider_request_id=None,
    )
    attempt = RoleAttemptFactsV5(
        role=persisted.call.role,
        attempt_kind=persisted.call.attempt_kind,
        attempt_index=persisted.call.attempt_index,
        request_sha256=persisted.request.sha256,
        slot_id=None,
        outcome="response_schema_failure",
        failure_code=RoleFailureCode.RESPONSE_SCHEMA,
        usage=usage,
        response_sha256=None,
        artifact_sha256=None,
    )
    authority = FixtureRoleTerminalAuthorityV5(
        "study-runtime-v1",
        persisted.call.sha256,
        persisted.request.sha256,
        attempt.sha256,
        None,
    )
    return RoleInvocationPackageV5(
        persisted.call,
        persisted.request,
        attempt,
        authority,
        None,
    )
from core.pit_optimizer_v5.two_round_study.runtime_ports import compose_study_round_v1


def test_compose_round_uses_the_explicit_router_and_runs_round_one(tmp_path: Path, monkeypatch) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert isinstance(dependencies.requests, StudyRequestRouterV1)
    guarded_critics = []
    original_guard = LocalRoleRequestFactoryV5._guarded_request
    investigator_factories = []
    author_factories = []
    original_investigator_request = LocalRoleRequestFactoryV5.investigator_request
    original_author_request = LocalRoleRequestFactoryV5.author_request

    def record_investigator(factory, *args, **kwargs):
        investigator_factories.append(factory)
        return original_investigator_request(factory, *args, **kwargs)

    def record_author(factory, *args, **kwargs):
        author_factories.append(factory)
        return original_author_request(factory, *args, **kwargs)

    def record_guard(factory, guarded_inputs, request):
        if request.role == "critic":
            guarded_critics.append(factory)
        return original_guard(factory, guarded_inputs, request)

    monkeypatch.setattr(LocalRoleRequestFactoryV5, "_guarded_request", record_guard)
    monkeypatch.setattr(LocalRoleRequestFactoryV5, "investigator_request", record_investigator)
    monkeypatch.setattr(LocalRoleRequestFactoryV5, "author_request", record_author)
    result = run_feedback_round_v5(inputs, dependencies)
    assert result.status == "completed"
    assert investigator_factories == [dependencies.requests.enabled]
    assert author_factories == [dependencies.requests.enabled]
    assert guarded_critics == [dependencies.requests.enabled]
    assert result.checkpoint is not None
    events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=1,
    )
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in events
    )
    completions = {
        item.role: fixture.repository.load_role_invocation(item)
        for item in payloads
        if type(item) is RoleCompletionPayloadV5
    }
    assert isinstance(completions["author"].request.role_input, AuthorRoleInputV5)
    critic_request = completions["critic"].request
    assert isinstance(critic_request.role_input, MechanismRoleInputV1)
    mechanism_ids = {
        evidence_id
        for projection in critic_request.role_input.projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    }
    assert mechanism_ids
    assert set(completions["critic"].artifact.evidence_ids).isdisjoint(mechanism_ids)
    assert "paired local observations" not in completions["critic"].artifact.comparative_assessment

    current_experiment_id = next(iter(completions["critic"].request.expected_binding.experiment_ids))
    current_bundle = dependencies.mechanism.role_request_evidence(current_experiment_id)
    decision = SimpleNamespace(hypothesis=current_bundle[-1].hypothesis)
    candidates = (object(),)
    router = dependencies.requests
    monkeypatch.setattr(router.enabled, "critic_request_with_mechanism", lambda *args, **kwargs: "accepted")
    assert (
        router.critic_request_with_mechanism(
            inputs,
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle,),
        )
        == "accepted"
    )
    foreign_router = StudyRequestRouterV1("withheld", router.base, router.enabled)
    with pytest.raises(MechanismCapabilityError, match="foreign"):
        foreign_router.critic_request_with_mechanism(
            inputs,
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle,),
        )
    with pytest.raises(MechanismCapabilityError, match="shape"):
        router.critic_request_with_mechanism(
            inputs,
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle[:4],),
        )
    with pytest.raises(MechanismCapabilityError, match="foreign"):
        router.critic_request_with_mechanism(
            SimpleNamespace(
                campaign_id="foreign-campaign",
                round_index=inputs.round_index,
                manifest=inputs.manifest,
            ),
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle,),
        )
    with pytest.raises(MechanismCapabilityError, match="foreign"):
        router.critic_request_with_mechanism(
            replace(inputs, round_index=2),
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle,),
        )
    wrong_hypothesis = replace(decision.hypothesis, causal_claim="A changed authenticated hypothesis.")
    with pytest.raises(MechanismCapabilityError, match="foreign"):
        router.critic_request_with_mechanism(
            inputs,
            object(),
            SimpleNamespace(hypothesis=wrong_hypothesis),
            candidates,
            mechanism_evidence=(current_bundle,),
        )

    def no_fallback(*args, **kwargs):
        raise RuntimeError("enabled critic failed")

    monkeypatch.setattr(router.enabled, "critic_request_with_mechanism", no_fallback)
    monkeypatch.setattr(router.base, "critic_request_with_mechanism", lambda *args, **kwargs: pytest.fail("critic fallback"))
    with pytest.raises(RuntimeError, match="enabled critic failed"):
        router.critic_request_with_mechanism(
            inputs,
            object(),
            decision,
            candidates,
            mechanism_evidence=(current_bundle,),
        )

    assert result.cleanup is not None and result.cleanup.cleanup_complete
    assert dependencies.candidates.active == {}
    lease_payloads = []
    for event in fixture.repository.load_round_events(
        campaign_id=inputs.campaign_id,
        round_index=inputs.round_index,
    ):
        payload = fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        if type(payload) is ResourceLeasePayloadV5:
            lease_payloads.append(payload)
    assert lease_payloads
    with pytest.raises(ValueError, match="foreign"):
        dependencies.candidates.recover_lease(
            replace(lease_payloads[0], owner_token_sha256="0" * 64),
            round_index=inputs.round_index,
        )

    extension = dependencies.mechanism._extension
    assert extension is not None
    worker_calls = []
    extension.worker_factory = lambda bound: worker_calls.append(bound)
    equivalent = build_study_registry_v1().configuration("S-gte-0.50")
    assert (
        extension.observe_candidate(
            experiment_id="c" * 64,
            candidate_revision=equivalent.policy_revision,
            candidate_source_bundle=equivalent.source_bundle,
            semantic_outcome="behaviorally_equivalent",
            semantic_recovered=False,
            deadline_monotonic=time.monotonic() + 30.0,
        )
        is None
    )
    assert worker_calls == []
    unavailable = build_study_registry_v1().configuration("S-gte-0.05")
    recovered_run = extension.observe_candidate(
        experiment_id="d" * 64,
        candidate_revision=unavailable.policy_revision,
        candidate_source_bundle=unavailable.source_bundle,
        semantic_outcome="behaviorally_distinct",
        semantic_recovered=True,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    assert recovered_run is not None
    assert recovered_run.execution.status == "not_run"
    assert recovered_run.execution.reason == "worker_unavailable"

    registry = build_study_registry_v1()
    baseline_evaluation = fixture.manifest.baseline_authority.campaign.episodes[0].evaluation
    p0 = registry.configuration("P0").portfolio_evaluator_inputs[0]
    expected_scenarios = {
        "base": (p0.ending_equity, p0.base_annualized_return_pct),
        "gross": (p0.gross_ending_equity, p0.gross_annualized_return_pct),
        "stress": (p0.stress_ending_equity, p0.stress_annualized_return_pct),
    }
    for scenario in baseline_evaluation.scenarios:
        expected_ending, expected_annualized = expected_scenarios[scenario.scenario_id]
        assert scenario.ending_equity == expected_ending
        assert scenario.report.portfolio_total_return_pct == (
            (scenario.ending_equity / scenario.starting_equity) - Decimal("1")
        ) * Decimal("100")
        assert scenario.report.portfolio_annualized_return_pct == expected_annualized
    assert tuple(item.scenario_id for item in baseline_evaluation.scenarios) == ("gross", "base", "stress")

    observed_configuration_ids = set()
    for stored in fixture.repository.load_experiment_journal():
        if stored.record.round_index != 1 or stored.record.campaign_evidence is None:
            continue
        matching_configurations = tuple(
            configuration
            for configuration in registry.configurations
            if configuration.policy_revision == stored.record.policy_revision
        )
        assert len(matching_configurations) == 1
        observed_configuration_ids.add(matching_configurations[0].configuration_id)
        assert tuple(
            item.scenario_id for item in stored.record.campaign_evidence.episodes[0].evaluation.scenarios
        ) == ("gross", "base", "stress")
        for episode in stored.record.campaign_evidence.episodes:
            for scenario in episode.evaluation.scenarios:
                assert scenario.report.portfolio_total_return_pct == (
                    (scenario.ending_equity / scenario.starting_equity) - Decimal("1")
                ) * Decimal("100")
                assert scenario.report.portfolio_annualized_return_pct == annualized_return_pct(
                    starting_equity=scenario.starting_equity,
                    ending_equity=scenario.ending_equity,
                    days=episode.evaluation.elapsed_calendar_days,
                )
    assert {"A", "S"}.issubset(observed_configuration_ids)

    def scenario_endpoints(evaluation):
        return tuple(
            (item.scenario_id, item.ending_equity, item.report.portfolio_annualized_return_pct)
            for item in evaluation.scenarios
        )

    actual_evaluated_outputs = {"P0": scenario_endpoints(baseline_evaluation)}
    for stored in fixture.repository.load_experiment_journal():
        if stored.record.round_index != 1 or stored.record.campaign_evidence is None:
            continue
        configuration = next(
            item
            for item in registry.configurations
            if item.policy_revision == stored.record.policy_revision
        )
        actual_evaluated_outputs[configuration.configuration_id] = scenario_endpoints(
            stored.record.campaign_evidence.episodes[0].evaluation
        )

    def registry_endpoints(configuration_id):
        endpoint = registry.configuration(configuration_id).portfolio_evaluator_inputs[0]
        return (
            ("gross", endpoint.gross_ending_equity, endpoint.gross_annualized_return_pct),
            ("base", endpoint.ending_equity, endpoint.base_annualized_return_pct),
            ("stress", endpoint.stress_ending_equity, endpoint.stress_annualized_return_pct),
        )

    expected_evaluated_outputs = tuple(
        (configuration_id, registry_endpoints(configuration_id))
        for configuration_id in ("P0", "A", "S")
    )
    assert tuple(
        (configuration_id, actual_evaluated_outputs[configuration_id])
        for configuration_id in ("P0", "A", "S")
    ) == expected_evaluated_outputs


def test_composed_round_delivers_worker_unavailable_mechanism_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    original_before_authoring = dependencies.mechanism.before_authoring

    def disable_workers(*, round_intent, campaign_id=None, round_index=None, parent_candidate=None):
        original_before_authoring(
            round_intent=round_intent,
            campaign_id=campaign_id,
            round_index=round_index,
            parent_candidate=parent_candidate,
        )
        extension = dependencies.mechanism._extension
        assert extension is not None
        extension.worker_factory = None

    monkeypatch.setattr(dependencies.mechanism, "before_authoring", disable_workers)
    result = run_feedback_round_v5(inputs, dependencies)
    assert result.status == "completed"
    critic_package = next(
        fixture.repository.load_role_invocation(payload)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        for payload in (
            fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),
        )
        if type(payload) is RoleCompletionPayloadV5 and payload.role == "critic"
    )
    assert isinstance(critic_package.request.role_input, MechanismRoleInputV1)
    assert critic_package.request.role_input.projections
    for projection in critic_package.request.role_input.projections:
        assert projection.execution.status == "not_run"
        assert projection.execution.reason == "worker_unavailable"
        assert all(
            row.prediction.availability == "unavailable"
            and row.prediction.unavailable_reason == "not_run"
            for row in projection.rows
        )


def test_composed_round_admits_registered_contradicted_mechanism_experiment(tmp_path: Path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"
    critic_package = next(
        fixture.repository.load_role_invocation(payload)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        for payload in (
            fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),
        )
        if type(payload) is RoleCompletionPayloadV5 and payload.role == "critic"
    )
    assert isinstance(critic_package.request.role_input, MechanismRoleInputV1)
    registry = build_study_registry_v1()
    candidates = {}
    for projection in critic_package.request.role_input.projections:
        configuration = next(
            item
            for item in registry.configurations
            if item.policy_revision.sha256 == projection.candidate_revision_sha256
        )
        candidates[configuration.configuration_id] = projection
    assert "A" in candidates
    a_projection = candidates["A"]
    a_bundle = dependencies.mechanism.role_request_evidence(a_projection.experiment_id)
    a_report = a_bundle[3]
    assert a_report.execution.status == "completed"
    decision_changed = next(
        item for item in a_report.predictions if item.metric_id == "exit.decision_changed_count"
    )
    assert decision_changed.assessment == "contradicted_on_cases"
    assert a_projection.execution == a_report.execution


def test_round_one_rejects_import_authority(tmp_path: Path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    with pytest.raises(ValueError, match="round one"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=object(),  # type: ignore[arg-type]
            imported=None,
        )


def test_withheld_author_construction_rejects_primary_mechanism_citation(
    tmp_path: Path,
) -> None:
    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary")
    primary_store = StudyStoreV1(primary_fixture.repository)
    primary_inputs, primary_dependencies = compose_study_round_v1(
        fixture=primary_fixture,
        round_index=1,
        arm="primary",
        store=primary_store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(primary_inputs, primary_dependencies).status == "completed"
    primary_critic = next(
        primary_fixture.repository.load_role_invocation(payload)
        for event in primary_fixture.repository.load_round_events(
            campaign_id=primary_inputs.campaign_id,
            round_index=primary_inputs.round_index,
        )
        for payload in (
            primary_fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind),
        )
        if type(payload) is RoleCompletionPayloadV5 and payload.role == "critic"
    )
    assert isinstance(primary_critic.request.role_input, MechanismRoleInputV1)
    primary_mechanism_ids = tuple(
        evidence_id
        for projection in primary_critic.request.role_input.projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    )
    assert primary_mechanism_ids

    withheld_fixture = create_study_fixture_v1(root=tmp_path / "withheld")
    withheld_store = StudyStoreV1(withheld_fixture.repository)
    withheld_inputs, withheld_dependencies = compose_study_round_v1(
        fixture=withheld_fixture,
        round_index=1,
        arm="withheld",
        store=withheld_store,
        ledger=None,
        imported=None,
    )
    reducer = LocalArchiveReducerFactoryV5(withheld_fixture.repository).recovery_reducer(withheld_inputs)
    checkpoint, state = withheld_fixture.repository.recover_projection(reducer)
    projection = SearchProjectionV5(checkpoint, state, ())
    parent = baseline_parent_candidate_v5(
        authority=withheld_fixture.manifest.baseline_authority,
        discovery_plan=withheld_inputs.panel_plan,
        evaluator_contract=withheld_inputs.evaluator_contract,
        pit_data_scope=withheld_inputs.manifest.pit_data_scope,
    )
    investigator_request = withheld_dependencies.requests.investigator_request(withheld_inputs, projection, parent)
    investigator = _investigator_artifact(investigator_request)
    decision = SimpleNamespace(hypothesis=investigator.hypotheses[0], parent=parent)
    valid_author = withheld_dependencies.requests.author_request(
        withheld_inputs,
        projection,
        decision,
        investigator,
    )
    assert isinstance(valid_author.role_input, AuthorRoleInputV5)
    foreign_hypothesis = replace(
        valid_author.role_input.hypothesis,
        evidence_ids=(primary_mechanism_ids[0],),
    )
    foreign_intent = RoundIntentPayloadV5(
        parent_revision_sha256=parent.policy_identity_sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        pit_data_scope=withheld_inputs.manifest.pit_data_scope,
        semantic_mode=withheld_inputs.manifest.semantic_mode,
        hypothesis=foreign_hypothesis,
        discovery_plan_sha256=withheld_inputs.panel_plan.discovery_plan_sha256,
    )
    foreign_investigator = replace(investigator, hypotheses=(foreign_hypothesis,))
    foreign_decision = SimpleNamespace(hypothesis=foreign_intent.hypothesis, parent=parent)
    with pytest.raises(ValueError, match="author hypothesis cites unavailable evidence"):
        withheld_dependencies.requests.author_request(
            withheld_inputs,
            projection,
            foreign_decision,
            foreign_investigator,
        )


def test_scripted_response_authority_is_portable_and_rejects_changed_manifest(tmp_path: Path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, _dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    reference = verify_scripted_role_authority_v1(
        repository=fixture.repository,
        manifest=inputs.manifest,
        registry=fixture.registry,
        round_index=1,
    )
    assert reference.relative_path == f"adapter-state/study-scripted-role/{inputs.campaign_id}-1.json"
    assert str(fixture.repository.root) not in reference.relative_path
    changed_manifest = replace(inputs.manifest, source_commit="b" * 40)
    with pytest.raises(ValueError, match="digest|authority|missing"):
        verify_scripted_role_authority_v1(
            repository=fixture.repository,
            manifest=changed_manifest,
            registry=fixture.registry,
            round_index=1,
        )


@pytest.mark.parametrize("missing_component", ("index", "value"))
def test_missing_scripted_authority_is_not_recreated_for_request_only_round(
    tmp_path: Path,
    monkeypatch,
    missing_component: str,
) -> None:
    """A persisted F request makes the slot historical before completion."""

    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    original_invoke = dependencies.mechanism.scripted.invoke_once

    def hold_investigator(persisted_request, *, deadline_monotonic):
        if persisted_request.call.role == "investigator":
            raise ControllerRoleResponsePendingV5(persisted_request.call)
        return original_invoke(persisted_request, deadline_monotonic=deadline_monotonic)

    with monkeypatch.context() as pending_patch:
        pending_patch.setattr(dependencies.mechanism.scripted, "invoke_once", hold_investigator)
        pending = run_feedback_round_v5(inputs, dependencies)
    assert pending.status == "awaiting_controller_response"
    assert fixture.repository.load_round_events(
        campaign_id=inputs.campaign_id,
        round_index=inputs.round_index,
    ) == ()
    assert fixture.repository.load_authenticated_role_requests(
        campaign_id=inputs.campaign_id,
        round_index=inputs.round_index,
    )

    authority_index = (
        fixture.repository.root
        / "adapter-state-authority"
        / "study-scripted-role"
        / f"{inputs.campaign_id}-{inputs.round_index}.json"
    )
    authority_value = (
        fixture.repository.root
        / "adapter-state"
        / "study-scripted-role"
        / f"{inputs.campaign_id}-{inputs.round_index}.json"
    )
    missing_path = authority_index if missing_component == "index" else authority_value
    assert missing_path.exists()
    missing_path.unlink()
    assert not missing_path.exists()
    with pytest.raises(ValueError, match="authority|missing|pin"):
        verify_scripted_role_authority_v1(
            repository=fixture.repository,
            manifest=fixture.manifest,
            registry=fixture.registry,
            round_index=inputs.round_index,
        )
    with pytest.raises(ValueError, match="authority|missing|pin"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )
    assert not missing_path.exists()


@pytest.mark.parametrize("missing_component", ("index", "value"))
def test_completed_round_one_authenticates_missing_mechanism_authority_without_repair(
    tmp_path: Path,
    missing_component: str,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"
    mechanism_index = (
        fixture.repository.root
        / "adapter-state-authority"
        / "mechanism-v5"
        / f"{inputs.campaign_id}-{inputs.round_index:04d}.json"
    )
    mechanism_value = (
        fixture.repository.root
        / "adapter-state"
        / "mechanism-v5"
        / f"{inputs.campaign_id}-{inputs.round_index:04d}.json"
    )
    missing_path = mechanism_index if missing_component == "index" else mechanism_value
    assert missing_path.exists()
    missing_path.unlink()
    assert not missing_path.exists()
    with pytest.raises((ValueError, MechanismCapabilityError), match="mechanism|missing|authority|index"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )
    assert not missing_path.exists()


@pytest.mark.parametrize("missing_component", ("index", "value"))
def test_completed_round_one_authenticates_missing_scripted_authority_without_repair(
    tmp_path: Path,
    missing_component: str,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"
    authority_index = (
        fixture.repository.root
        / "adapter-state-authority"
        / "study-scripted-role"
        / f"{inputs.campaign_id}-{inputs.round_index}.json"
    )
    authority_value = (
        fixture.repository.root
        / "adapter-state"
        / "study-scripted-role"
        / f"{inputs.campaign_id}-{inputs.round_index}.json"
    )
    missing_path = authority_index if missing_component == "index" else authority_value
    assert missing_path.exists()
    missing_path.unlink()
    assert not missing_path.exists()
    with pytest.raises((ValueError, MechanismCapabilityError), match="authority|missing|pin"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )
    assert not missing_path.exists()


def test_round_one_reopens_after_investigator_completion_before_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def reject_persistence(*args, **kwargs):
        raise RuntimeError("injected crash before round intent")

    with monkeypatch.context() as stop_patch:
        def stop_before_intent(*args, **kwargs):
            stop_patch.setattr(fixture.repository, "append_round_payload", reject_persistence)
            raise RuntimeError("injected crash before round intent")

        stop_patch.setattr(dependencies.novelty, "resolve", stop_before_intent)
        first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    assert tuple(item.role for item in payloads if type(item) is RoleCompletionPayloadV5) == ("investigator",)
    assert not any(type(item).__name__ == "RoundIntentPayloadV5" for item in payloads)
    investigator_payload = next(item for item in payloads if type(item) is RoleCompletionPayloadV5)
    saved_package = fixture.repository.load_role_invocation(investigator_payload)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    calls = []
    original_invoke = reopened_dependencies.mechanism.scripted.invoke_once

    def record_reopened_role(persisted, *, deadline_monotonic):
        calls.append(persisted.call.role)
        return original_invoke(persisted, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(
        reopened_dependencies.mechanism.scripted,
        "invoke_once",
        record_reopened_role,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "completed", (reopened.failure, reopened.terminal_outcome, reopened.cleanup_failure)
    assert "investigator" not in calls
    investigator_after = next(
        item
        for item in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        if fixture.repository.load_round_payload(item.payload_ref, expected_kind=item.event_kind)
        == investigator_payload
    )
    assert fixture.repository.load_role_invocation(
        fixture.repository.load_round_payload(
            investigator_after.payload_ref,
            expected_kind=investigator_after.event_kind,
        )
    ) == saved_package


def test_round_one_reopens_after_persisted_novelty_terminal_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def fail_novelty(*args, **kwargs):
        raise RuntimeError("injected novelty failure after investigator")

    monkeypatch.setattr(dependencies.novelty, "resolve", fail_novelty)
    first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    assert tuple(item.role for item in payloads if type(item) is RoleCompletionPayloadV5) == ("investigator",)
    assert any(type(item).__name__ == "RoundOutcomePayloadV5" for item in payloads)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "failed"
    assert reopened.terminal_outcome is not None


def test_round_one_reopens_after_persisted_investigator_rejection_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def reject_investigator(persisted, *, deadline_monotonic):
        return _fixture_role_failure_package(persisted)

    monkeypatch.setattr(dependencies.invoker, "invoke_once", reject_investigator)
    first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    investigator_payload = next(item for item in payloads if type(item) is RoleCompletionPayloadV5)
    investigator_package = fixture.repository.load_role_invocation(investigator_payload)
    assert not investigator_package.accepted
    assert any(type(item).__name__ == "RoundOutcomePayloadV5" for item in payloads)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "failed"
    assert reopened.terminal_outcome is not None


def test_round_one_rejects_wrong_fixture_identity_on_investigator_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def reject_investigator(persisted, *, deadline_monotonic):
        return _fixture_role_failure_package(persisted)

    monkeypatch.setattr(dependencies.invoker, "invoke_once", reject_investigator)
    first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    original_load = fixture.repository.load_role_invocation

    def load_tampered_package(payload):
        package = original_load(payload)
        if package.call.role != "investigator":
            return package
        return replace(
            package,
            terminal_authority=replace(package.terminal_authority, fixture_id="foreign-study-runtime-v1"),
        )

    monkeypatch.setattr(fixture.repository, "load_role_invocation", load_tampered_package)
    with pytest.raises(MechanismCapabilityError, match="fixture terminal"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )


def test_round_one_rejects_phase_incoherent_persisted_novelty_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def fail_novelty(*args, **kwargs):
        raise RuntimeError("injected novelty failure after investigator")

    monkeypatch.setattr(dependencies.novelty, "resolve", fail_novelty)
    first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    terminal_event = next(
        event
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        if event.event_kind == "round_outcome"
    )
    original_load = fixture.repository.load_round_payload

    def load_tampered_terminal(reference, *, expected_kind):
        payload = original_load(reference, expected_kind=expected_kind)
        if reference != terminal_event.payload_ref:
            return payload
        assert type(payload) is RoundOutcomePayloadV5
        assert type(payload.authority) is RuntimeFailureAuthorityV5
        return replace(payload, authority=replace(payload.authority, failure_code="role_rejected"))

    monkeypatch.setattr(fixture.repository, "load_round_payload", load_tampered_terminal)
    with pytest.raises(MechanismCapabilityError, match="novelty terminal|phase"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )


def test_round_one_rejects_schema_valid_but_incoherent_novelty_authorities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def fail_novelty(*args, **kwargs):
        raise RuntimeError("injected novelty failure after investigator")

    monkeypatch.setattr(dependencies.novelty, "resolve", fail_novelty)
    first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    terminal_event = next(
        event
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        if event.event_kind == "round_outcome"
    )
    original_load = fixture.repository.load_round_payload
    zero = "0" * 64
    before = SchedulingCursorV5(1, zero, ())
    after = SchedulingCursorV5(2, zero, ())
    no_novel = NoNovelHypothesisAuthorityV5(
        "no_novel_hypothesis",
        zero,
        zero,
        zero,
        zero,
        zero,
        zero,
        (zero,),
        zero,
        zero,
        before,
        after,
    )
    forged_authorities = (
        no_novel,
        NoveltyExhaustedAuthorityV5("novelty_exhausted", zero, zero, zero, (no_novel,)),
    )
    for forged_authority in forged_authorities:
        def load_tampered_terminal(reference, *, expected_kind, authority=forged_authority):
            payload = original_load(reference, expected_kind=expected_kind)
            if reference != terminal_event.payload_ref:
                return payload
            assert type(payload) is RoundOutcomePayloadV5
            return replace(payload, authority=authority)

        monkeypatch.setattr(fixture.repository, "load_round_payload", load_tampered_terminal)
        with pytest.raises(MechanismCapabilityError, match="novelty terminal|phase"):
            compose_study_round_v1(
                fixture=fixture,
                round_index=1,
                arm="primary",
                store=store,
                ledger=None,
                imported=None,
            )


def test_round_two_reopens_after_persisted_novelty_terminal_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary-fixture")
    primary_store = StudyStoreV1(primary_fixture.repository)
    primary_inputs, primary_dependencies = compose_study_round_v1(
        fixture=primary_fixture,
        round_index=1,
        arm="primary",
        store=primary_store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(primary_inputs, primary_dependencies).status == "completed"

    fixture = create_study_fixture_v1(root=tmp_path / "withheld-fixture")
    store = StudyStoreV1(fixture.repository)
    inputs_one, dependencies_one = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="withheld",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs_one, dependencies_one).status == "completed"
    owner_token = canonical_sha256_v5({"fixture": inputs_one.manifest.sha256, "round": 2})
    inputs_two = FeedbackRoundInputV5(
        inputs_one.manifest,
        inputs_one.panel_plan,
        inputs_one.evaluator_contract,
        inputs_one.baseline,
        2,
        owner_token,
    )
    reducer = LocalArchiveReducerFactoryV5(fixture.repository).recovery_reducer(inputs_two)
    checkpoint, state = fixture.repository.recover_projection(reducer)
    stored = tuple(
        StoredExperimentRecordV5(reference, fixture.repository.load_experiment(reference))
        for reference in checkpoint.record_refs
    )
    projection = SearchProjectionV5(checkpoint, state, stored)
    parent = select_parent_v5(
        state=state,
        baseline=inputs_two.baseline,
        discovery_plan=inputs_two.panel_plan,
        evaluator_contract=inputs_two.evaluator_contract,
        stored_records=stored,
    )
    store, ledger, imported = _round_two_import_for_withheld_arm(
        tmp_path,
        fixture,
        primary_fixture,
        inputs_two,
        projection,
        parent,
    )
    inputs_two, dependencies_two = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )

    def fail_novelty(*args, **kwargs):
        raise RuntimeError("injected round-two novelty failure after imported investigator")

    monkeypatch.setattr(dependencies_two.novelty, "resolve", fail_novelty)
    first = run_feedback_round_v5(inputs_two, dependencies_two)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs_two.campaign_id,
            round_index=inputs_two.round_index,
        )
    )
    assert tuple(item.role for item in payloads if type(item) is RoleCompletionPayloadV5) == ("investigator",)
    assert not any(type(item) is RoundIntentPayloadV5 for item in payloads)
    assert any(type(item).__name__ == "RoundOutcomePayloadV5" for item in payloads)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "failed"
    assert reopened.terminal_outcome is not None


def test_round_two_accepts_and_reopens_genuine_novelty_exhaustion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary-fixture")
    primary_store = StudyStoreV1(primary_fixture.repository)
    primary_inputs, primary_dependencies = compose_study_round_v1(
        fixture=primary_fixture,
        round_index=1,
        arm="primary",
        store=primary_store,
        ledger=None,
        imported=None,
    )
    def unavailable_validation(*args, **kwargs):
        raise RuntimeError("injected fixture validation unavailability")

    monkeypatch.setattr(primary_dependencies.candidates, "validate", unavailable_validation)
    assert run_feedback_round_v5(primary_inputs, primary_dependencies).status == "completed"

    fixture = create_study_fixture_v1(root=tmp_path / "withheld-fixture")
    store = StudyStoreV1(fixture.repository)
    inputs_one, dependencies_one = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="withheld",
        store=store,
        ledger=None,
        imported=None,
    )
    monkeypatch.setattr(dependencies_one.candidates, "validate", unavailable_validation)
    assert run_feedback_round_v5(inputs_one, dependencies_one).status == "completed"

    owner_token = canonical_sha256_v5({"fixture": inputs_one.manifest.sha256, "round": 2})
    inputs_two = FeedbackRoundInputV5(
        inputs_one.manifest,
        inputs_one.panel_plan,
        inputs_one.evaluator_contract,
        inputs_one.baseline,
        2,
        owner_token,
    )
    reducer = LocalArchiveReducerFactoryV5(fixture.repository).recovery_reducer(inputs_two)
    checkpoint, state = fixture.repository.recover_projection(reducer)
    stored = tuple(
        StoredExperimentRecordV5(reference, fixture.repository.load_experiment(reference))
        for reference in checkpoint.record_refs
    ) if checkpoint is not None else ()
    projection = SearchProjectionV5(checkpoint, state, stored)
    parent = select_parent_v5(
        state=state,
        baseline=inputs_two.baseline,
        discovery_plan=inputs_two.panel_plan,
        evaluator_contract=inputs_two.evaluator_contract,
        stored_records=stored,
    )
    assert parent.origin == "baseline"
    store, ledger, imported = _round_two_import_for_withheld_arm(
        tmp_path,
        fixture,
        primary_fixture,
        inputs_two,
        projection,
        parent,
    )
    inputs_two, dependencies_two = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    first = run_feedback_round_v5(inputs_two, dependencies_two)
    assert first.status == "novelty_exhausted"
    assert isinstance(first.terminal_outcome.authority, NoveltyExhaustedAuthorityV5)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "novelty_exhausted"
    assert isinstance(reopened.terminal_outcome.authority, NoveltyExhaustedAuthorityV5)


def test_round_one_reopens_after_author_completion_during_materialization_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def reject_persistence(*args, **kwargs):
        raise RuntimeError("injected crash during materialization")

    with monkeypatch.context() as stop_patch:
        def stop_during_materialization(*args, **kwargs):
            stop_patch.setattr(fixture.repository, "append_round_payload", reject_persistence)
            raise RuntimeError("injected crash after owned materialization")

        stop_patch.setattr(dependencies.candidates, "validate", stop_during_materialization)
        first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    assert {item.role for item in payloads if type(item) is RoleCompletionPayloadV5} == {"investigator", "author"}
    assert any(type(item).__name__ == "RoundIntentPayloadV5" for item in payloads)
    assert any(type(item) is ResourceLeasePayloadV5 for item in payloads)
    assert fixture.repository.load_checkpoint() is None

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    calls = []
    original_invoke = reopened_dependencies.mechanism.scripted.invoke_once

    def record_reopened_role(persisted, *, deadline_monotonic):
        calls.append(persisted.call.role)
        return original_invoke(persisted, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(
        reopened_dependencies.mechanism.scripted,
        "invoke_once",
        record_reopened_role,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "completed", (reopened.failure, reopened.terminal_outcome, reopened.cleanup_failure)
    assert calls == ["critic"]


def test_round_one_reopens_after_critic_completion_before_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    def reject_persistence(*args, **kwargs):
        raise RuntimeError("injected crash before publication")

    with monkeypatch.context() as stop_patch:
        def stop_before_publication(*args, **kwargs):
            stop_patch.setattr(fixture.repository, "append_round_payload", reject_persistence)
            raise RuntimeError("injected crash before publication")

        stop_patch.setattr(dependencies.records, "build_records", stop_before_publication)
        first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    assert {item.role for item in payloads if type(item) is RoleCompletionPayloadV5} == {
        "investigator",
        "author",
        "critic",
    }
    assert fixture.repository.load_checkpoint() is None

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    calls = []
    monkeypatch.setattr(
        reopened_dependencies.mechanism.scripted,
        "invoke_once",
        lambda *args, **kwargs: calls.append((args, kwargs)) or pytest.fail("reopen made an external role call"),
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "completed", (reopened.failure, reopened.terminal_outcome, reopened.cleanup_failure)
    assert calls == []


def test_round_one_recovery_rejects_foreign_lease_without_releasing_owned_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )

    with monkeypatch.context() as stop_patch:
        def reject_persistence(*args, **kwargs):
            raise RuntimeError("injected crash while recording validation")

        def stop_materialization(*args, **kwargs):
            stop_patch.setattr(fixture.repository, "append_round_payload", reject_persistence)
            raise RuntimeError("injected crash after owned materialization")

        stop_patch.setattr(dependencies.candidates, "validate", stop_materialization)
        first = run_feedback_round_v5(inputs, dependencies)
    assert first.status == "failed"
    lease_payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    lease_payload = next(item for item in lease_payloads if type(item) is ResourceLeasePayloadV5)

    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    original_recover_lease = reopened_dependencies.candidates.recover_lease

    def return_foreign_lease(payload, *, round_index):
        return original_recover_lease(
            replace(payload, owner_token_sha256="0" * 64),
            round_index=round_index,
        )

    monkeypatch.setattr(reopened_dependencies.candidates, "recover_lease", return_foreign_lease)
    foreign = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert foreign.status == "failed"
    assert foreign.failure is not None and foreign.failure.code == "foreign_lease"
    assert lease_payload in tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
    )
    assert reopened_dependencies.candidates.active == {}


def test_round_one_projection_authentication_is_read_only_and_cross_binds_f_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"

    archive_path = fixture.repository.root / "archive.json"
    original_archive = archive_path.read_bytes()
    archive_path.write_bytes(original_archive + b" ")
    mutated_archive = archive_path.read_bytes()
    with pytest.raises((ValueError, MechanismCapabilityError), match="checkpoint|archive|projection"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )
    assert archive_path.read_bytes() == mutated_archive

    fixture = create_study_fixture_v1(root=tmp_path / "mismatched-fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"
    original_load = fixture.repository.load_round_payload

    def mismatched_intent(reference, *, expected_kind):
        payload = original_load(reference, expected_kind=expected_kind)
        if type(payload).__name__ == "RoundIntentPayloadV5":
            return replace(payload, hypothesis=replace(payload.hypothesis, causal_claim="tampered persisted seed"))
        return payload

    monkeypatch.setattr(fixture.repository, "load_round_payload", mismatched_intent)
    with pytest.raises(MechanismCapabilityError, match="hypothesis|intent|investigator"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=1,
            arm="primary",
            store=store,
            ledger=None,
            imported=None,
        )


def test_round_one_sidecar_tampering_rejects_replay_without_repair(tmp_path: Path, monkeypatch) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    store = StudyStoreV1(fixture.repository)
    inputs, dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="primary",
        store=store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs, dependencies).status == "completed"
    critic_payload = next(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs.campaign_id,
            round_index=inputs.round_index,
        )
        if type(fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind))
        is RoleCompletionPayloadV5
        and fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind).role == "critic"
    )
    critic_package = fixture.repository.load_role_invocation(critic_payload)
    experiment_id = next(iter(critic_package.request.expected_binding.experiment_ids))
    repository = MechanismArtifactRepositoryV5(fixture.repository)
    complete = repository._load_experiment_index(experiment_id, "complete", repair=False)
    assert complete is not None
    for reference_name in ("binding_ref", "run_ref", "report_ref"):
        reference = getattr(complete, reference_name)
        assert reference is not None
        path = fixture.repository.root.joinpath(*reference.relative_path.split("/"))
        with open(_windows_extended_path(path), "rb") as handle:
            original = handle.read()
        tampered = original[:-1] + bytes((original[-1] ^ 1,))
        with open(_windows_extended_path(path), "wb") as handle:
            handle.write(tampered)
        with open(_windows_extended_path(path), "rb") as handle:
            assert handle.read() == tampered
        with pytest.raises((ValueError, MechanismCapabilityError)):
            compose_study_round_v1(
                fixture=fixture,
                round_index=1,
                arm="primary",
                store=store,
                ledger=None,
                imported=None,
            )
        with open(_windows_extended_path(path), "rb") as handle:
            assert handle.read() == tampered
        with open(_windows_extended_path(path), "wb") as handle:
            handle.write(original)


def test_withheld_round_two_replays_authenticated_import_and_reopens(tmp_path: Path, monkeypatch) -> None:
    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary-fixture")
    round_one_store = StudyStoreV1(primary_fixture.repository)
    inputs_one, dependencies_one = compose_study_round_v1(
        fixture=primary_fixture,
        round_index=1,
        arm="primary",
        store=round_one_store,
        ledger=None,
        imported=None,
    )
    assert run_feedback_round_v5(inputs_one, dependencies_one).status == "completed"

    fixture = create_study_fixture_v1(root=tmp_path / "withheld-fixture")
    withheld_store = StudyStoreV1(fixture.repository)
    withheld_inputs_one, withheld_dependencies_one = compose_study_round_v1(
        fixture=fixture,
        round_index=1,
        arm="withheld",
        store=withheld_store,
        ledger=None,
        imported=None,
    )
    withheld_investigator_factories = []
    withheld_author_factories = []
    original_investigator_request = LocalRoleRequestFactoryV5.investigator_request
    original_author_request = LocalRoleRequestFactoryV5.author_request

    def record_withheld_investigator(factory, *args, **kwargs):
        withheld_investigator_factories.append(factory)
        return original_investigator_request(factory, *args, **kwargs)

    def record_withheld_author(factory, *args, **kwargs):
        withheld_author_factories.append(factory)
        return original_author_request(factory, *args, **kwargs)

    with monkeypatch.context() as arm_patch:
        arm_patch.setattr(LocalRoleRequestFactoryV5, "investigator_request", record_withheld_investigator)
        arm_patch.setattr(LocalRoleRequestFactoryV5, "author_request", record_withheld_author)
        assert run_feedback_round_v5(withheld_inputs_one, withheld_dependencies_one).status == "completed"
    assert withheld_investigator_factories == [withheld_dependencies_one.requests.base]
    assert withheld_author_factories == [withheld_dependencies_one.requests.base]
    withheld_round_one_payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=withheld_inputs_one.campaign_id,
            round_index=withheld_inputs_one.round_index,
        )
    )
    withheld_round_one_completions = {
        item.role: fixture.repository.load_role_invocation(item)
        for item in withheld_round_one_payloads
        if type(item) is RoleCompletionPayloadV5
    }
    assert not isinstance(withheld_round_one_completions["investigator"].request.role_input, MechanismRoleInputV1)
    assert isinstance(withheld_round_one_completions["author"].request.role_input, AuthorRoleInputV5)
    assert isinstance(withheld_round_one_completions["critic"].request.role_input, MechanismRoleInputV1)

    owner_token = canonical_sha256_v5({"fixture": withheld_inputs_one.manifest.sha256, "round": 2})
    inputs_two = FeedbackRoundInputV5(
        withheld_inputs_one.manifest,
        withheld_inputs_one.panel_plan,
        withheld_inputs_one.evaluator_contract,
        withheld_inputs_one.baseline,
        2,
        owner_token,
    )
    reducer = LocalArchiveReducerFactoryV5(fixture.repository).recovery_reducer(inputs_two)
    checkpoint, state = fixture.repository.recover_projection(reducer)
    stored = tuple(
        StoredExperimentRecordV5(reference, fixture.repository.load_experiment(reference))
        for reference in checkpoint.record_refs
    )
    projection = SearchProjectionV5(checkpoint, state, stored)
    parent = select_parent_v5(
        state=state,
        baseline=inputs_two.baseline,
        discovery_plan=inputs_two.panel_plan,
        evaluator_contract=inputs_two.evaluator_contract,
        stored_records=stored,
    )
    assert parent.origin == "archive"
    store, ledger, imported = _round_two_import_for_withheld_arm(
        tmp_path,
        fixture,
        primary_fixture,
        inputs_two,
        projection,
        parent,
    )
    inputs_two, dependencies_two = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    def reject_intent_persistence(*args, **kwargs):
        raise RuntimeError("injected crash before round-two intent")

    with monkeypatch.context() as stop_patch:
        def stop_round_two_before_intent(*args, **kwargs):
            stop_patch.setattr(fixture.repository, "append_round_payload", reject_intent_persistence)
            raise RuntimeError("injected crash before round-two intent")

        stop_patch.setattr(dependencies_two.novelty, "resolve", stop_round_two_before_intent)
        first_round_two = run_feedback_round_v5(inputs_two, dependencies_two)
    assert first_round_two.status == "failed"
    first_round_two_payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in fixture.repository.load_round_events(
            campaign_id=inputs_two.campaign_id,
            round_index=inputs_two.round_index,
        )
    )
    assert tuple(
        item.role for item in first_round_two_payloads if type(item) is RoleCompletionPayloadV5
    ) == ("investigator",)
    assert not any(type(item).__name__ == "RoundIntentPayloadV5" for item in first_round_two_payloads)

    inputs_two, dependencies_two = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    scripted = dependencies_two.mechanism.scripted
    original_scripted_invoke = scripted.invoke_once

    def hold_before_author(persisted_request, *, deadline_monotonic):
        if persisted_request.call.role == "author":
            raise ControllerRoleResponsePendingV5(persisted_request.call)
        return original_scripted_invoke(persisted_request, deadline_monotonic=deadline_monotonic)

    with monkeypatch.context() as pending_patch:
        pending_patch.setattr(scripted, "invoke_once", hold_before_author)
        pending = run_feedback_round_v5(inputs_two, dependencies_two)
    assert pending.status == "awaiting_controller_response"
    assert pending.pending_controller_role is not None
    assert pending.pending_controller_role.role == "author"
    pending_events = fixture.repository.load_round_events(
        campaign_id=inputs_two.campaign_id,
        round_index=inputs_two.round_index,
    )
    pending_payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in pending_events
    )
    assert not any(type(item) is RoleCompletionPayloadV5 and item.role == "author" for item in pending_payloads)
    assert store.list_refs(kind="commitments")

    # Recomposition authenticates F, the journaled intent, and the exact
    # pre-author graph before the legacy runtime resumes the pending request.
    inputs_two, dependencies_two = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    withheld_guarded_critics = []
    original_guard = LocalRoleRequestFactoryV5._guarded_request

    def record_withheld_guard(factory, guarded_inputs, request):
        if request.role == "critic":
            withheld_guarded_critics.append(factory)
        return original_guard(factory, guarded_inputs, request)

    original_evidence_loader = dependencies_two.mechanism.role_request_evidence

    def timeout_evidence(experiment_id):
        capability, bound, run, report, intent = original_evidence_loader(experiment_id)
        execution = MechanismExecutionV1(status="failed", reason="timeout")
        unavailable_predictions = tuple(
            MechanismPredictionResultV1.unavailable(
                metric,
                minimum_relevant_cases=capability.spec.minimum_relevant_cases,
                reason="execution_failed",
            )
            for metric in capability.spec.metrics
        )
        return (
            capability,
            bound,
            replace(run, execution=execution),
            replace(
                report,
                execution=execution,
                predictions=unavailable_predictions,
                consequence_contexts=(),
            ),
            intent,
        )

    with monkeypatch.context() as route_patch:
        route_patch.setattr(LocalRoleRequestFactoryV5, "_guarded_request", record_withheld_guard)
        route_patch.setattr(dependencies_two.mechanism, "role_request_evidence", timeout_evidence)
        result = run_feedback_round_v5(inputs_two, dependencies_two)
    assert result.status == "completed"
    assert withheld_guarded_critics == [dependencies_two.requests.enabled]
    assert result.checkpoint is not None
    round_two_events_before_reopen = fixture.repository.load_round_events(
        campaign_id=inputs_two.campaign_id,
        round_index=inputs_two.round_index,
    )
    round_two_payloads_before_reopen = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in round_two_events_before_reopen
    )
    current_critic_payload = next(
        item
        for item in round_two_payloads_before_reopen
        if type(item) is RoleCompletionPayloadV5 and item.role == "critic"
    )
    assert current_critic_payload is not None
    current_critic_package = fixture.repository.load_role_invocation(current_critic_payload)
    assert "timeout" in current_critic_package.artifact.reviews[0].causal_explanation
    assert current_critic_package.artifact.reviews[0].disposition == "refine"
    current_id = next(iter(current_critic_package.request.expected_binding.experiment_ids))
    current_bundle = dependencies_two.mechanism.role_request_evidence(current_id)
    decision = SimpleNamespace(hypothesis=current_bundle[-1].hypothesis)
    monkeypatch.setattr(dependencies_two.requests.enabled, "critic_request_with_mechanism", lambda *args, **kwargs: "accepted")
    assert (
        dependencies_two.requests.critic_request_with_mechanism(
            inputs_two,
            object(),
            decision,
            (object(),),
            mechanism_evidence=(current_bundle,),
        )
        == "accepted"
    )
    primary_router = StudyRequestRouterV1(
        "primary",
        dependencies_two.requests.base,
        dependencies_two.requests.enabled,
    )
    with pytest.raises(MechanismCapabilityError, match="foreign"):
        primary_router.critic_request_with_mechanism(
            inputs_two,
            object(),
            decision,
            (object(),),
            mechanism_evidence=(current_bundle,),
        )
    reopened_inputs, reopened_dependencies = compose_study_round_v1(
        fixture=fixture,
        round_index=2,
        arm="withheld",
        store=store,
        ledger=ledger,
        imported=imported,
    )
    reopened = run_feedback_round_v5(reopened_inputs, reopened_dependencies)
    assert reopened.status == "completed"
    assert reopened.checkpoint == result.checkpoint
    link_ref = next(iter(store.list_refs(kind="mechanism-links")))
    link_path = store.repository.root.joinpath(*link_ref.relative_path.split("/"))
    with open(_windows_extended_path(link_path), "rb") as handle:
        original_link = handle.read()
    tampered_link = original_link[:-1] + bytes((original_link[-1] ^ 1,))
    with open(_windows_extended_path(link_path), "wb") as handle:
        handle.write(tampered_link)
    with open(_windows_extended_path(link_path), "rb") as handle:
        assert handle.read() == tampered_link
    with pytest.raises((ValueError, MechanismCapabilityError), match="link|graph|authority|canonical"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=2,
            arm="withheld",
            store=store,
            ledger=ledger,
            imported=imported,
        )
    with open(_windows_extended_path(link_path), "rb") as handle:
        assert handle.read() == tampered_link
    with open(_windows_extended_path(link_path), "wb") as handle:
        handle.write(original_link)
    commitment_matches = []
    for reference in store.list_refs(kind="commitments"):
        index = StudyCommitmentIndexV1.from_canonical_json(store.read(reference))
        if (
            index.storage_ref == reference
            and index.arm == "withheld"
            and index.campaign_id == inputs_two.campaign_id
            and index.round_index == inputs_two.round_index
        ):
            commitment_matches.append((reference, index))
    assert len(commitment_matches) == 1
    commitment_ref, _commitment_index = commitment_matches[0]
    commitment_path = store.repository.root.joinpath(*commitment_ref.relative_path.split("/"))
    with open(_windows_extended_path(commitment_path), "rb") as handle:
        commitment_bytes = bytearray(handle.read())
    commitment_bytes[-1] ^= 1
    with open(_windows_extended_path(commitment_path), "wb") as handle:
        handle.write(bytes(commitment_bytes))
    with open(_windows_extended_path(commitment_path), "rb") as handle:
        assert handle.read() == bytes(commitment_bytes)
    with pytest.raises((ValueError, MechanismCapabilityError), match="commitment|graph|canonical|reference"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=2,
            arm="withheld",
            store=store,
            ledger=ledger,
            imported=imported,
        )
    with open(_windows_extended_path(commitment_path), "rb") as handle:
        assert handle.read() == bytes(commitment_bytes)
    changed_import = replace(imported, ordinary_artifact_sha256="0" * 64)
    with pytest.raises((ValueError, MechanismCapabilityError), match="import|persisted|authority|digest"):
        compose_study_round_v1(
            fixture=fixture,
            round_index=2,
            arm="withheld",
            store=store,
            ledger=ledger,
            imported=changed_import,
        )

    round_two_events = fixture.repository.load_round_events(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
    )
    round_two_payloads = tuple(
        fixture.repository.load_round_payload(event.payload_ref, expected_kind=event.event_kind)
        for event in round_two_events
    )
    round_two_completions = {
        item.role: fixture.repository.load_role_invocation(item)
        for item in round_two_payloads
        if type(item) is RoleCompletionPayloadV5
    }
    round_two_critic = round_two_completions["critic"]
    assert isinstance(round_two_completions["author"].request.role_input, AuthorRoleInputV5)
    assert not isinstance(round_two_completions["investigator"].request.role_input, MechanismRoleInputV1)
    round_two_mechanism_ids = {
        evidence_id
        for projection in round_two_critic.request.role_input.projections
        for row in projection.rows
        for evidence_id in row.evidence_ids
    }
    assert round_two_mechanism_ids.intersection(round_two_critic.artifact.evidence_ids)
    assert all(
        round_two_mechanism_ids.intersection(review.evidence_ids)
        for review in round_two_critic.artifact.reviews
    )
    assert {
        item.evidence_id for item in round_two_completions["author"].request.role_evidence.items
    }.isdisjoint(round_two_mechanism_ids)

    # The composed timeout route above is the public-runtime evidence check.
    # The cases below retain focused formatter behavior checks for contradicted
    # and unavailable projections using the authenticated request shape.
    critic_request = round_two_critic.request
    assert isinstance(critic_request.role_input, MechanismRoleInputV1)
    selected = dependencies_two.mechanism.scripted.selected
    assert selected is not None
    capability = dependencies_two.mechanism._capability
    assert capability is not None
    metrics = {metric.metric_id: metric for metric in capability.spec.metrics}

    def render_critic(first_projection):
        source_projections = (first_projection, *critic_request.role_input.projections[1:])
        extension_ids = {
            evidence_id
            for projection in critic_request.role_input.projections
            for row in projection.rows
            for evidence_id in row.evidence_ids
        }
        normalized_projections = []
        normalized_items = [
            item for item in critic_request.role_evidence.items if item.evidence_id not in extension_ids
        ]
        for projection_index, projection in enumerate(source_projections):
            normalized_rows = []
            for row_index, row in enumerate(projection.rows):
                prediction = row.prediction
                if prediction.availability == "unavailable":
                    values = (("unavailable", None),)
                else:
                    values = (
                        ("parent", prediction.parent_value),
                        ("candidate", prediction.candidate_value),
                        ("numerator", prediction.numerator),
                        ("denominator", prediction.denominator),
                        ("delta", prediction.paired_delta),
                    )
                evidence_ids = []
                for suffix, value in values:
                    evidence_id = "v5.task6-fix3-formatter-" + canonical_sha256_v5(
                        {
                            "experiment_id": projection.experiment_id,
                            "projection_index": projection_index,
                            "row_index": row_index,
                            "metric_id": prediction.metric_id,
                            "suffix": suffix,
                        }
                    )[:40]
                    evidence_ids.append(evidence_id)
                    normalized_items.append(
                        RoleEvidenceItemV5(
                            evidence_id,
                            f"{prediction.metric_id}.{suffix}",
                            value,
                        )
                    )
                normalized_rows.append(replace(row, evidence_ids=tuple(evidence_ids)))
            normalized_projections.append(replace(projection, rows=tuple(normalized_rows)))
        projections = tuple(normalized_projections)
        evidence = RoleEvidenceV5(5, tuple(normalized_items))
        modified = replace(
            critic_request,
            role_evidence=evidence,
            role_input=replace(
                critic_request.role_input,
                projection=projections[0] if len(projections) == 1 else projections,
            ),
        )
        response = scripted_response_v1(
            request=modified,
            round_index=2,
            registry=fixture.registry,
            selected=selected,
        )
        return FixtureRoleRunnerV5(responses={"critic": (response,)}).invoke_once(modified)

    def contradicted_projection(projection):
        rows = tuple(
            replace(
                row,
                evidence_ids=(),
                prediction=MechanismPredictionResultV1.from_measurement(
                    metrics[row.prediction.metric_id],
                    parent_value=Decimal("1"),
                    candidate_value=Decimal("0"),
                    numerator=Decimal("0"),
                    denominator=2,
                    minimum_relevant_cases=capability.spec.minimum_relevant_cases,
                ),
            )
            for row in projection.rows
        )
        return replace(
            projection,
            execution=MechanismExecutionV1(status="completed", reason="completed"),
            rows=rows,
        )

    contradicted = render_critic(contradicted_projection(critic_request.role_input.projections[0]))
    assert contradicted.reviews[0].disposition == "refine"
    assert "contradicts" in contradicted.reviews[0].causal_explanation

    inconclusive_rows = tuple(
        replace(
            row,
            evidence_ids=(),
            prediction=MechanismPredictionResultV1.from_measurement(
                metrics[row.prediction.metric_id],
                parent_value=Decimal("0"),
                candidate_value=Decimal("0"),
                numerator=Decimal("0"),
                denominator=0,
                minimum_relevant_cases=capability.spec.minimum_relevant_cases,
            ),
        )
        for row in critic_request.role_input.projections[0].rows
    )
    inconclusive_projection = replace(
        critic_request.role_input.projections[0],
        execution=MechanismExecutionV1(status="completed", reason="completed"),
        rows=inconclusive_rows,
    )
    inconclusive = render_critic(inconclusive_projection)
    assert inconclusive.reviews[0].disposition == "refine"
    assert "inconclusive" in inconclusive.reviews[0].causal_explanation

    for execution, expected_text, unavailable_reason in (
        (MechanismExecutionV1(status="failed", reason="timeout"), "timeout", "execution_failed"),
        (MechanismExecutionV1(status="not_run", reason="worker_unavailable"), "worker_unavailable", "not_run"),
    ):
        unavailable_rows = tuple(
            replace(
                row,
                prediction=MechanismPredictionResultV1.unavailable(
                    metrics[row.prediction.metric_id],
                    minimum_relevant_cases=capability.spec.minimum_relevant_cases,
                    reason=unavailable_reason,
                ),
            )
            for row in critic_request.role_input.projections[0].rows
        )
        unavailable_projection = replace(
            critic_request.role_input.projections[0],
            execution=execution,
            rows=unavailable_rows,
        )
        unavailable = render_critic(unavailable_projection)
        assert unavailable.reviews[0].disposition == "refine"
        assert expected_text in unavailable.reviews[0].causal_explanation
