"""Task 5 authoritative compiler contract tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time
from collections.abc import Callable

import pytest

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, RoleEvidenceItemV5, RoleEvidenceV5
from core.pit_optimizer_v5.memory import RoundEventV5, RoundIntentPayloadV5
from core.pit_optimizer_v5.mechanism_contracts import (
    MechanismDiagnosticSelectorV1,
    MechanismPredicateV1,
    MechanismRecipeV1,
)
from core.pit_optimizer_v5.probes import policy_probe_suite_v1
from core.pit_optimizer_v5.provider import (
    AuthorPolicyContractsV5,
    AuthorRoleInputV5,
    FreshPersistedRoleRequestV5,
    InvestigatorRoleInputV5,
    RoleCallKeyV5,
    RoleBindingV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.search import ParentCandidateV5, baseline_parent_candidate_v5
from core.pit_optimizer_v5.two_round_study.imports import StudyImportInvokerV1, create_study_import_v1
from core.pit_optimizer_v5.two_round_study.ledger import run_study_call_v1
from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1
from core.pit_optimizer_v5.two_round_study.compiler import (
    CompiledStudyExperimentV1,
    StudyCommitmentIndexV1,
    StudyDraftBindingV1,
    _validate_parent_authority,
    compile_study_experiment_v1,
)
from core.pit_optimizer_v5.two_round_study.contracts import (
    ExperimentDraftV1,
    RivalPatternV1,
    StudyAdmissionError,
    StudyAuthorityError,
    StudyContractError,
    StudyPrecommitmentError,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1

from tests.test_pit_optimizer_v5_study_imports import (
    _CountingFake,
    _StudyFixtureAuthorDelegate,
    _task4_honest_study_context,
)
from tests.test_pit_optimizer_v5_study_ledger import _completion


def _binding() -> StudyDraftBindingV1:
    return StudyDraftBindingV1(
        import_ref=ArtifactRefV5("adapter-blobs/study-v1-imports/i.bin", "a" * 64),
        draft_ref=ArtifactRefV5("adapter-blobs/study-v1-import-drafts/d.bin", "b" * 64),
        hypothesis_id="hyp.exit.atr",
        hypothesis_sha256="c" * 64,
        fixture_role_package_ref=ArtifactRefV5("payloads/role_completion/p.bin", "d" * 64),
        fixture_role_package_sha256="d" * 64,
        round_intent_ref=ArtifactRefV5("payloads/round_intent/i.bin", "e" * 64),
        round_intent_sha256="e" * 64,
        parent_revision_sha256="f" * 64,
        parent_source_bundle_ref=ArtifactRefV5("parents/source.json", "1" * 64),
        parent_experiment_record_ref=None,
        checkpoint_ref=ArtifactRefV5("checkpoint.json", "2" * 64),
        snapshot_ref=ArtifactRefV5("archive.json", "3" * 64),
        fixture_manifest_ref=ArtifactRefV5("campaign-manifest.json", "4" * 64),
        fixture_root_identity_sha256="5" * 64,
        journal_campaign_id="fixture-campaign",
        journal_round_index=2,
        investigator_sequence=0,
        intent_sequence=1,
    )


def test_draft_binding_is_canonical_and_create_only(tmp_path: Path) -> None:
    binding = _binding()
    assert StudyDraftBindingV1.from_canonical_json(binding.canonical_bytes()) == binding

    store = StudyStoreV1(LocalArtifactRepositoryV5(tmp_path))
    store.put_contract(kind="draft-bindings", key="immutable", value=binding)
    changed = replace(binding, hypothesis_id="hyp.exit.other")
    with pytest.raises(StudyAuthorityError):
        store.put(kind="draft-bindings", key="immutable", content=changed.canonical_bytes())


def test_draft_binding_rejects_intent_before_investigator_completion() -> None:
    with pytest.raises(StudyAuthorityError):
        replace(_binding(), investigator_sequence=2, intent_sequence=2)


def test_compiler_rejects_unauthenticated_inputs_before_loading_any_graph(tmp_path: Path) -> None:
    store = StudyStoreV1(LocalArtifactRepositoryV5(tmp_path))
    with pytest.raises(StudyContractError):
        compile_study_experiment_v1(
            store=store,  # type: ignore[arg-type]
            imported=object(),  # type: ignore[arg-type]
            request=object(),  # type: ignore[arg-type]
            round_intent=object(),  # type: ignore[arg-type]
            parent=object(),  # type: ignore[arg-type]
            seed_snapshot=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
        )


def _compile_context(
    tmp_path: Path,
    *,
    configuration_id: str = "P0",
    applicability: MechanismPredicateV1 | None = None,
    relaxed_metrics: bool = False,
    draft_transform: Callable[[ExperimentDraftV1], ExperimentDraftV1] | None = None,
) -> SimpleNamespace:
    (
        fixture,
        fixture_request,
        preflight,
        _manifest,
        live_request,
        store,
        _grant,
        _approval,
        ledger,
        *_rest,
    ) = _task4_honest_study_context(tmp_path)
    from tests.test_pit_optimizer_v5_study_ledger import _response_for

    response = _response_for(fixture_request)
    draft = replace(response.drafts[0], configuration_id=configuration_id)
    if applicability is not None:
        draft = replace(draft, applicability=applicability)
    if relaxed_metrics:
        draft = replace(draft, metrics=(replace(draft.metrics[0], direction="decrease"), draft.metrics[1]))
    if draft_transform is not None:
        draft = draft_transform(draft)
    response = replace(response, drafts=(draft,))
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
    delegate = _StudyFixtureAuthorDelegate(fixture.manifest.manifest)
    invoker = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=delegate,
    )
    persisted_request = FreshPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=fixture_request,
    )
    package = invoker.invoke_once(persisted_request, deadline_monotonic=time.monotonic() + 30.0)
    persisted_package = fixture.repository.persist_role_invocation(
        call=preflight.call,
        request_ref=preflight.request_ref,
        package=package,
    )
    completion_ref = fixture.repository.append_round_payload(persisted_package.payload)
    completion_event = RoundEventV5(
        campaign_id=preflight.call.campaign_id,
        round_index=preflight.call.round_index,
        sequence=0,
        prior_event_sha256=None,
        event_kind="role_completion",
        experiment_id=None,
        payload_ref=completion_ref,
    )
    fixture.repository.append_round_event(completion_event)

    parent = baseline_parent_candidate_v5(
        authority=fixture.manifest.baseline_authority,
        discovery_plan=fixture.manifest.panel_plan,
        evaluator_contract=fixture.manifest.evaluator_contract,
        pit_data_scope="production",
    )
    intent = RoundIntentPayloadV5(
        parent_revision_sha256=parent.policy_revision.sha256,
        parent_semantic_fingerprint_sha256=parent.semantic_fingerprint_sha256,
        hypothesis=response.artifact.hypotheses[0],
        discovery_plan_sha256=fixture.manifest.panel_plan.discovery_plan_sha256,
    )
    intent_ref = fixture.repository.append_round_payload(intent)
    fixture.repository.append_round_event(
        RoundEventV5(
            campaign_id=preflight.call.campaign_id,
            round_index=preflight.call.round_index,
            sequence=1,
            prior_event_sha256=completion_event.sha256,
            event_kind="round_intent",
            experiment_id=None,
            payload_ref=intent_ref,
        )
    )
    author_request = build_role_request_v5(
        role="author",
        role_input=AuthorRoleInputV5(
            fixture.manifest.baseline_authority.source_bundle.files,
            False,
            response.artifact.hypotheses[0],
            AuthorPolicyContractsV5(parent.policy_revision, fixture.manifest.manifest.policy_scope_ref.sha256),
        ),
        issued_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
        ),
        expected_binding=RoleBindingV5(
            parent.policy_revision.sha256,
            response.artifact.hypotheses[0].hypothesis_id,
            (),
            fixture.manifest.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="author",
            manifest=fixture.manifest.manifest,
            author_policy_paths=tuple(item.path for item in fixture.manifest.baseline_authority.source_bundle.files),
        ),
        max_output_tokens=128,
    )
    registry = build_study_registry_v1()
    seed_snapshot = next(item.snapshot for item in policy_probe_suite_v1() if item.method == "evaluate_exit")
    return SimpleNamespace(
        fixture=fixture,
        fixture_request=fixture_request,
        preflight=preflight,
        store=store,
        imported=imported,
        parent=parent,
        intent=intent,
        author_request=author_request,
        registry=registry,
        seed_snapshot=seed_snapshot,
        ledger=ledger,
        completion_ref=completion_ref,
        intent_ref=intent_ref,
    )


def _compile(context: SimpleNamespace):
    compiled = compile_study_experiment_v1(
        store=context.store,
        imported=context.imported,
        request=context.fixture_request,
        round_intent=context.intent,
        parent=context.parent,
        seed_snapshot=context.seed_snapshot,
        registry=context.registry,
    )
    return compiled


def _with_context(context: SimpleNamespace, **changes: object) -> SimpleNamespace:
    values = vars(context).copy()
    values.update(changes)
    return SimpleNamespace(**values)


def test_compiler_reloads_persisted_f_graph_and_freezes_selected_draft(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    compiled = _compile(context)
    assert compiled.selected_configuration_id == "P0"
    assert compiled.draft_binding.fixture_role_package_ref == context.completion_ref
    assert compiled.draft_binding.round_intent_ref == context.intent_ref
    assert compiled.draft_binding.intent_sequence == 1
    assert compiled.spec.frozen_before_authoring is True
    assert CompiledStudyExperimentV1.from_canonical_json(compiled.canonical_bytes()) == compiled
    context.store.put_contract(kind="compiled", key=compiled.sha256, value=compiled)


def test_compiler_rejects_forged_saved_intent_and_parent(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    with pytest.raises(StudyAuthorityError):
        _compile(
            _with_context(
                context,
                intent=replace(context.intent, discovery_plan_sha256="9" * 64),
            )
        )
    registered_s = context.registry.configuration("S")
    with pytest.raises(StudyAuthorityError):
        _compile(
            _with_context(
                context,
                parent=replace(
                    context.parent,
                    policy_revision=registered_s.policy_revision,
                    source_bundle_ref=ArtifactRefV5("forged/source.json", registered_s.source_bundle.sha256),
                ),
            )
        )
    with pytest.raises(StudyAuthorityError, match="fingerprint"):
        _compile(
            _with_context(
                context,
                parent=replace(context.parent, semantic_fingerprint_sha256="9" * 64),
            )
        )


def test_archived_p1_parent_authority_accepts_exact_ancestry_and_rejects_scope_or_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _compile_context(tmp_path)
    configuration = context.registry.configuration("S-gte-0.05")
    selected = context.registry.configuration("S")
    source_ref = ArtifactRefV5(
        f"adapter-state/policy-source/{selected.source_bundle.sha256}.json",
        selected.source_bundle.sha256,
    )
    record_ref = ArtifactRefV5(f"records/{'a' * 64}.json", "b" * 64)
    checkpoint = context.fixture.repository.load_checkpoint()
    assert checkpoint is not None
    monkeypatch.setattr(
        context.fixture.repository,
        "load_checkpoint",
        lambda: replace(checkpoint, record_refs=(record_ref,)),
    )
    monkeypatch.setattr(
        context.fixture.repository,
        "load_typed_artifact",
        lambda reference, value_type: selected.source_bundle,
    )
    monkeypatch.setattr(
        context.fixture.repository,
        "load_experiment",
        lambda reference: SimpleNamespace(
            policy_revision=selected.policy_revision,
            artifact_refs=(source_ref,),
            status="evaluated",
            hypothesis=SimpleNamespace(primary_mechanism="exit"),
            semantic_fingerprint=SimpleNamespace(fingerprint_sha256="c" * 64),
            pit_data_scope="production",
            semantic_mode="required",
        ),
    )
    parent = ParentCandidateV5(
        origin="archive",
        policy_revision=selected.policy_revision,
        semantic_fingerprint_sha256="c" * 64,
        campaign_cagr_pct=Decimal("1.00"),
        source_bundle_ref=source_ref,
        experiment_record_ref=record_ref,
        primary_mechanism="exit",
        admitted_round=1,
    )
    _validate_parent_authority(
        parent=parent,
        registry=context.registry,
        configuration_id=configuration.configuration_id,
        repository=context.fixture.repository,
        preflight=context.preflight,
        fixture_manifest_ref=context.imported.fixture_manifest_ref,
    )
    with pytest.raises(StudyAuthorityError, match="saved checkpoint ancestry"):
        _validate_parent_authority(
            parent=replace(parent, experiment_record_ref=ArtifactRefV5(f"records/{'d' * 64}.json", "e" * 64)),
            registry=context.registry,
            configuration_id=configuration.configuration_id,
            repository=context.fixture.repository,
            preflight=context.preflight,
            fixture_manifest_ref=context.imported.fixture_manifest_ref,
        )
    with pytest.raises(StudyAuthorityError, match="checkpoint-authenticated"):
        _validate_parent_authority(
            parent=replace(parent, semantic_fingerprint_sha256="d" * 64),
            registry=context.registry,
            configuration_id=configuration.configuration_id,
            repository=context.fixture.repository,
            preflight=context.preflight,
            fixture_manifest_ref=context.imported.fixture_manifest_ref,
        )
    with pytest.raises(StudyAuthorityError, match="production"):
        _validate_parent_authority(
            parent=replace(
                parent,
                semantic_fingerprint_sha256=None,
                pit_data_scope="development_sp500_v2",
                semantic_mode="disabled_development",
            ),
            registry=context.registry,
            configuration_id=configuration.configuration_id,
            repository=context.fixture.repository,
            preflight=context.preflight,
            fixture_manifest_ref=context.imported.fixture_manifest_ref,
        )


def test_authenticated_author_request_rejects_foreign_citations_before_compiler(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    with pytest.raises(ValueError, match="citations"):
        build_role_request_v5(
            role="author",
            role_input=context.author_request.role_input,
            issued_evidence=RoleEvidenceV5(
                5,
                (RoleEvidenceItemV5("v5.foreign.evidence", "evaluator.exit_attribution_count", 1),),
            ),
            expected_binding=context.author_request.expected_binding,
            schema_authority=context.author_request.schema_authority,
            max_output_tokens=context.author_request.max_output_tokens,
        )


def test_compiler_rejects_registry_and_frozen_metric_drift(tmp_path: Path, monkeypatch) -> None:
    context = _compile_context(tmp_path)
    from core.pit_optimizer_v5.two_round_study.contracts import ExperimentDraftV1

    forged = ExperimentDraftV1.from_canonical_json(context.store.read(context.imported.draft_refs[0]))
    forged = replace(forged, configuration_id="provider-invented")
    forged_raw = forged.canonical_bytes()
    forged_ref = ArtifactRefV5(context.imported.draft_refs[0].relative_path, hashlib.sha256(forged_raw).hexdigest())
    imported = replace(context.imported, draft_refs=(forged_ref,), draft_hashes=(forged_ref.sha256,))
    original_read = context.store.read

    def read(reference):
        if reference == forged_ref:
            return forged_raw
        return original_read(reference)

    monkeypatch.setattr(context.store, "read", read)
    with pytest.raises(StudyAuthorityError):
        _compile(_with_context(context, imported=imported))
    with pytest.raises(StudyPrecommitmentError):
        _compile(_compile_context(tmp_path / "metrics", relaxed_metrics=True))


def test_strict_mechanism_contract_rejects_unsupported_observable_and_predicate() -> None:
    with pytest.raises(ValueError, match="predicate operator"):
        MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="unsupported",  # type: ignore[arg-type]
            value=Decimal("0.50"),
        )
    with pytest.raises(ValueError, match="metric ID"):
        MechanismDiagnosticSelectorV1(
            section="exit_attribution",
            metric_id="evaluator.foreign_metric",
        )


def test_compiler_rejects_fewer_than_two_applicable_cases(tmp_path: Path) -> None:
    with pytest.raises(StudyAdmissionError):
        _compile(
            _compile_context(
                tmp_path / "predicate",
                applicability=MechanismPredicateV1(
                    field="features.atr_20_fraction",
                    operator="gte",
                    value=Decimal("0.90"),
                ),
            )
        )


def _dynamic_threshold_draft(draft: ExperimentDraftV1) -> ExperimentDraftV1:
    return replace(
        draft,
        applicability=MechanismPredicateV1(
            field="features.atr_20_fraction",
            operator="gte",
            value=Decimal("0.50"),
        ),
        recipe=MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.49"), Decimal("0.50"), Decimal("0.51"), None),
        ),
        expected_changed=(False, True, True, False),
        rivals=(
            RivalPatternV1("always_on", (True, True, True, True)),
            RivalPatternV1("inert", (False, False, False, False)),
        ),
        claim_kind="threshold",
    )


def _compile_before_authoring(context: SimpleNamespace):
    return compile_study_experiment_v1(
        store=context.store,
        imported=context.imported,
        request=context.fixture_request,
        round_intent=context.intent,
        parent=context.parent,
        seed_snapshot=context.seed_snapshot,
        registry=context.registry,
    )


def _append_author_completion(context: SimpleNamespace) -> None:
    call = RoleCallKeyV5(
        campaign_id=context.preflight.call.campaign_id,
        round_index=context.preflight.call.round_index,
        role="author",
        role_position=2,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=context.author_request.sha256,
    )
    persisted_request = FreshPersistedRoleRequestV5(
        reference=context.fixture.repository.append_role_request(call=call, request=context.author_request).reference,
        call=call,
        request=context.author_request,
    )
    invoker = StudyImportInvokerV1(
        fixture_manifest=context.fixture.manifest,
        record=context.imported,
        store=context.store,
        ledger=context.ledger,
        delegate=_StudyFixtureAuthorDelegate(context.fixture.manifest.manifest),
    )
    package = invoker.invoke_once(persisted_request, deadline_monotonic=time.monotonic() + 30.0)
    persisted_package = context.fixture.repository.persist_role_invocation(
        call=call,
        request_ref=persisted_request.reference,
        package=package,
    )
    payload_ref = context.fixture.repository.append_round_payload(persisted_package.payload)
    events = context.fixture.repository.load_round_events(
        campaign_id=context.preflight.call.campaign_id,
        round_index=context.preflight.call.round_index,
    )
    context.fixture.repository.append_round_event(
        RoundEventV5(
            campaign_id=context.preflight.call.campaign_id,
            round_index=context.preflight.call.round_index,
            sequence=len(events),
            prior_event_sha256=events[-1].sha256,
            event_kind="role_completion",
            experiment_id=None,
            payload_ref=payload_ref,
        )
    )


def test_compiler_runs_at_before_authoring_with_authenticated_investigator(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    assert context.fixture.repository.load_authenticated_role_requests(
        campaign_id=context.preflight.call.campaign_id,
        round_index=context.preflight.call.round_index,
        role="author",
    ) == ()
    compiled = _compile_before_authoring(context)
    assert compiled.spec.frozen_before_authoring is True


def test_compiler_accepts_dynamic_round_two_threshold_draft(tmp_path: Path) -> None:
    context = _compile_context(tmp_path, configuration_id="S", draft_transform=_dynamic_threshold_draft)
    compiled = _compile_before_authoring(context)
    assert compiled.selected_configuration_id == "S"
    assert compiled.spec.applicability.operator == "gte"
    assert compiled.spec.recipe.input_values == (Decimal("0.49"), Decimal("0.50"), Decimal("0.51"), None)
    assert sum(case.applicable for case in compiled.corpus.cases) == 2
    assert compiled.contrast.expected_changed == (False, True, True, False)
    assert tuple((item.metric_id, item.direction, item.denominator, item.tolerance) for item in compiled.spec.metrics) == (
        ("exit.decision_changed_count", "increase", "relevant_cases", Decimal("0")),
        ("exit.protected_control_unchanged_count", "unchanged", "control_cases", Decimal("0")),
    )


def test_compiler_rejects_wrong_authenticated_f_request(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    foreign_request = replace(
        context.fixture_request,
        role_input=InvestigatorRoleInputV5(("v5.foreign.evidence",), (), ()),
        role_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.foreign.evidence", "evaluator.exit_attribution_count", 1),),
        ),
    )
    with pytest.raises(StudyAuthorityError, match="investigator request"):
        compile_study_experiment_v1(
            store=context.store,
            imported=context.imported,
            request=foreign_request,
            round_intent=context.intent,
            parent=context.parent,
            seed_snapshot=context.seed_snapshot,
            registry=context.registry,
        )


def test_compiler_rejects_foreign_draft_citations_at_compiler_boundary(tmp_path: Path, monkeypatch) -> None:
    context = _compile_context(tmp_path)
    original_draft = ExperimentDraftV1.from_canonical_json(context.store.read(context.imported.draft_refs[0]))
    foreign_draft = replace(original_draft, cited_evidence_ids=("v5.foreign.evidence",))
    foreign_raw = foreign_draft.canonical_bytes()
    foreign_ref = ArtifactRefV5(
        context.imported.draft_refs[0].relative_path,
        hashlib.sha256(foreign_raw).hexdigest(),
    )
    imported = replace(context.imported, draft_refs=(foreign_ref,), draft_hashes=(foreign_ref.sha256,))
    imported_raw = imported.canonical_bytes()
    original_read = context.store.read

    def read(reference):
        if reference == imported.storage_ref:
            return imported_raw
        if reference == foreign_ref:
            return foreign_raw
        return original_read(reference)

    monkeypatch.setattr(context.store, "read", read)
    with pytest.raises(StudyAuthorityError, match="evidence"):
        _compile_before_authoring(_with_context(context, imported=imported))


def test_compiler_rejects_no_negative_case_and_one_applicable_case(tmp_path: Path) -> None:
    no_negative = _compile_context(
        tmp_path / "no-negative",
        draft_transform=lambda draft: replace(
            draft,
            applicability=MechanismPredicateV1(
                field="features.atr_20_fraction",
                operator="gte",
                value=Decimal("0.00"),
            ),
            recipe=MechanismRecipeV1(
                recipe_id="evaluate_exit_atr20_fraction_v1",
                input_field="features.atr_20_fraction",
                input_values=(Decimal("0.20"), Decimal("0.50"), Decimal("0.80")),
            ),
            expected_changed=(True, True, True),
            rivals=(
                RivalPatternV1("always_on", (True, True, True)),
                RivalPatternV1("inert", (False, False, False)),
            ),
        ),
    )
    with pytest.raises(StudyAdmissionError, match="negative"):
        _compile_before_authoring(no_negative)

    one_applicable = _compile_context(
        tmp_path / "one-applicable",
        draft_transform=lambda draft: replace(
            draft,
            applicability=MechanismPredicateV1(
                field="features.atr_20_fraction",
                operator="gte",
                value=Decimal("0.80"),
            ),
        ),
    )
    with pytest.raises(StudyAdmissionError, match="fewer"):
        _compile_before_authoring(one_applicable)


def test_decimal_and_converted_snapshot_uniqueness_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unique"):
        MechanismRecipeV1(
            recipe_id="evaluate_exit_atr20_fraction_v1",
            input_field="features.atr_20_fraction",
            input_values=(Decimal("0.50"), Decimal("0.500")),
        )
    collision = _compile_context(
        tmp_path,
        draft_transform=lambda draft: replace(
            draft,
            applicability=MechanismPredicateV1(
                field="features.atr_20_fraction",
                operator="is_present",
                value=None,
            ),
            recipe=MechanismRecipeV1(
                recipe_id="evaluate_exit_atr20_fraction_v1",
                input_field="features.atr_20_fraction",
                input_values=(Decimal("0.100000000000000005"), Decimal("0.1"), Decimal("0.50"), None),
            ),
            expected_changed=(False, False, True, False),
            rivals=(
                RivalPatternV1("always_on", (True, True, True, True)),
                RivalPatternV1("inert", (False, False, False, False)),
            ),
        ),
    )
    with pytest.raises(StudyPrecommitmentError, match="corpus"):
        _compile_before_authoring(collision)


def test_compiler_rejects_durable_author_request_before_new_graph(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    call = RoleCallKeyV5(
        campaign_id=context.preflight.call.campaign_id,
        round_index=context.preflight.call.round_index,
        role="author",
        role_position=2,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=context.author_request.sha256,
    )
    context.fixture.repository.append_role_request(call=call, request=context.author_request)
    with pytest.raises(StudyAuthorityError, match="author request already exists"):
        _compile_before_authoring(context)


def test_exact_commitment_replay_is_read_only_after_author_request(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    first = _compile_before_authoring(context)
    before = {
        kind: context.store.list_refs(kind=kind)
        for kind in ("commitments", "draft-bindings", "mechanism-specs", "mechanism-corpora", "contrasts")
    }
    _append_author_completion(context)
    replay = _compile_before_authoring(context)
    after = {
        kind: context.store.list_refs(kind=kind)
        for kind in ("commitments", "draft-bindings", "mechanism-specs", "mechanism-corpora", "contrasts")
    }
    assert replay == first
    assert after == before
    commitment_refs = context.store.list_refs(kind="commitments")
    assert len(commitment_refs) == 1
    index = StudyCommitmentIndexV1.from_canonical_json(context.store.read(commitment_refs[0]))
    assert index.storage_ref == commitment_refs[0]


def test_changed_graph_after_authoring_cannot_acquire_another_stable_slot(tmp_path: Path) -> None:
    context = _compile_context(tmp_path)
    _compile_before_authoring(context)
    changed_seed = next(
        item.snapshot
        for item in policy_probe_suite_v1()
        if item.method == "evaluate_exit" and item.snapshot != context.seed_snapshot
    )
    before = {kind: context.store.list_refs(kind=kind) for kind in ("commitments", "mechanism-corpora")}
    with pytest.raises(StudyPrecommitmentError, match="stable authenticated graph"):
        compile_study_experiment_v1(
            store=context.store,
            imported=context.imported,
            request=context.fixture_request,
            round_intent=context.intent,
            parent=context.parent,
            seed_snapshot=changed_seed,
            registry=context.registry,
        )
    assert {kind: context.store.list_refs(kind=kind) for kind in before} == before


def test_orphaned_graph_cannot_acquire_a_new_stable_slot(tmp_path: Path, monkeypatch) -> None:
    context = _compile_context(tmp_path)
    _compile_before_authoring(context)
    changed_seed = next(
        item.snapshot
        for item in policy_probe_suite_v1()
        if item.method == "evaluate_exit" and item.snapshot != context.seed_snapshot
    )
    original_list_refs = context.store.list_refs

    def list_refs(*, kind: str, maximum_entries: int = 4096):
        if kind == "commitments":
            return ()
        return original_list_refs(kind=kind, maximum_entries=maximum_entries)

    monkeypatch.setattr(context.store, "list_refs", list_refs)
    with pytest.raises(StudyPrecommitmentError, match="orphaned graph bytes"):
        compile_study_experiment_v1(
            store=context.store,
            imported=context.imported,
            request=context.fixture_request,
            round_intent=context.intent,
            parent=context.parent,
            seed_snapshot=changed_seed,
            registry=context.registry,
        )


def test_partial_stable_commitment_graph_fails_closed_on_exact_replay(tmp_path: Path, monkeypatch) -> None:
    context = _compile_context(tmp_path)
    _compile_before_authoring(context)
    index_ref = context.store.list_refs(kind="commitments")[0]
    index = StudyCommitmentIndexV1.from_canonical_json(context.store.read(index_ref))
    original_read = context.store.read

    def read(reference):
        if reference == index.binding_ref:
            return b"{}"
        return original_read(reference)

    monkeypatch.setattr(context.store, "read", read)
    with pytest.raises(StudyAuthorityError, match="binding"):
        _compile_before_authoring(context)
