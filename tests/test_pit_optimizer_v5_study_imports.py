"""Focused tests for authenticated live investigator import and fixture replay."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.candidate_ir import SourceBundleV5
from core.pit_optimizer_v5.contracts import (
    MetricPredictionV5,
    RoleEvidenceItemV5,
    RoleEvidenceV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
)
from core.pit_optimizer_v5.manifest import AuthenticatedCampaignManifestV5
from core.pit_optimizer_v5.provider import (
    ExistingPersistedRoleRequestV5,
    ControllerRoleTerminalAuthorityV5,
    FreshPersistedRoleRequestV5,
    AuthorPolicyContractsV5,
    AuthorRoleInputV5,
    CriticRoleInputV5,
    ExperimentEvaluationAggregateV5,
    ExperimentFailureAggregateV5,
    ExperimentPredictionAggregateV5,
    ScenarioAggregateV5,
    FixtureRoleTerminalAuthorityV5,
    FixtureRoleRunnerV5,
    InvestigatorRoleInputV5,
    LedgerRoleTerminalAuthorityV5,
    RoleBindingV5,
    RoleCallKeyV5,
    RoleInvocationPackageV5,
    RoleTerminalReceiptV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.fixture_runtime import FixtureRoleInvokerV5, verify_fixture_run_v5
from core.pit_optimizer_v5.memory import RoundEventV5
from core.pit_optimizer_v5.search import ArchiveRecordAuthorityV5, CandidateArchiveReducerV5
from core.pit_optimizer_v5.two_round_study.live_calls import FixturePreflightV1
from core.pit_optimizer_v5.two_round_study import imports as study_imports
from core.pit_optimizer_v5.two_round_study.imports import (
    StudyImportInvokerV1,
    authenticate_study_import_v1,
    create_study_import_v1,
    verify_imported_package_v1,
)
from core.pit_optimizer_v5.two_round_study.contracts import (
    StudyAuthorityError,
    StudyManifestV1,
    StudyOfflineSettingsV1,
    StudyPendingAccounting,
    StudyProviderSettingsV1,
)
from core.pit_optimizer_v5.two_round_study.fixtures import create_study_fixture_v1, study_resource_budget_v1
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
from core.pit_optimizer_v5.two_round_study.live_calls import (
    StudyGrantV1,
    authorize_study_execution_v1,
    build_study_call_v1,
    study_parser_authority_bytes_v1,
    study_prompt_bytes_v1,
)
from core.pit_optimizer_v5.two_round_study.registry import build_study_registry_v1
from core.pit_optimizer_v5.two_round_study.schema import study_response_schema_v1
from core.pit_optimizer_v5.two_round_study.schema import parse_study_response_v1
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1

from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for
from tests.test_pit_optimizer_v5_study_live_calls import _study_context


class _ForbiddenScriptedDelegate:
    def invoke_once(self, *_args, **_kwargs):
        raise AssertionError("the investigator import must not use the scripted delegate")

    def reconcile_once(self, *_args, **_kwargs):
        raise AssertionError("the investigator import must not use the scripted delegate")


class _StudyFixtureAuthorDelegate(FixtureRoleInvokerV5):
    """Use the provider-free delegate boundary with this fixture's limits."""

    def _invoke(self, persisted):
        if persisted.request.role != "author":
            return super()._invoke(persisted)
        from core.pit_optimizer_v5.candidate_ir import LiteralAxisV5, SourceOperationV5, StructuralTemplateV5
        from core.pit_optimizer_v5.provider import parsed_role_artifact_primitive_v5

        template = StructuralTemplateV5(
            hypothesis_id=persisted.request.expected_binding.hypothesis_id,
            parent_revision_sha256=persisted.request.expected_binding.parent_revision_sha256,
            changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
            source_operations=(
                SourceOperationV5(
                    path="core/strategy_policy/v3/exit.py",
                    symbol="evaluate_exit",
                    kind="replace_function",
                    replacement_source=(
                        "def evaluate_exit(snapshot: ExitSnapshotV3) -> ExitDecision:\n"
                        '    return PIT_AXIS("atr_20_fraction")\n'
                    ),
                ),
            ),
            axes=(LiteralAxisV5("atr_20_fraction", 0.20, (0.20, 0.40)),),
            full_source_escape=None,
        )
        response = canonical_json_bytes_v5(
            {
                "binding": persisted.request.expected_binding.to_primitive(),
                "artifact": parsed_role_artifact_primitive_v5(template)["artifact"],
            }
        ).decode()
        original = self.runner
        self.runner = FixtureRoleRunnerV5(responses={"author": (response,)})
        try:
            return super()._invoke(persisted)
        finally:
            self.runner = original

    def reconcile_once(self, persisted_request):
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise ValueError("fixture recovery requires an existing role request")
        return self._invoke(persisted_request)


def _live_import_context(
    tmp_path: Path,
    *,
    mode: str = "live_study",
    primary_campaign_id: str | None = None,
    persist_evidence: bool = False,
    evidence_value: int = 1,
    study_fixture_sha256: str | None = None,
):
    context = _task4_honest_study_context(
        tmp_path,
        mode=mode,
        primary_campaign_id=primary_campaign_id,
        persist_evidence=persist_evidence,
        evidence_value=evidence_value,
        study_fixture_sha256=study_fixture_sha256,
    )
    fixture, fixture_request, preflight, manifest, live_request, store, _grant, _approval, ledger, *_ = context
    terminal = run_study_call_v1(
        request=live_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                live_request.sha256: _completion(
                    request=live_request,
                    response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    return fixture, fixture_request, preflight, manifest, live_request, store, ledger, terminal


def _task4_honest_study_context(
    tmp_path: Path,
    *,
    mode: str = "offline_fixture",
    primary_campaign_id: str | None = None,
    withheld_campaign_id: str | None = None,
    persist_evidence: bool = False,
    evidence_value: int = 1,
    study_fixture_sha256: str | None = None,
):
    """Rebuild Task4's study graph with F calls bound to each fixture manifest."""

    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary-fixture")
    withheld_fixture = create_study_fixture_v1(root=tmp_path / "withheld-fixture")

    def fixture_request(fixture):
        return build_role_request_v5(
            role="investigator",
            role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
            issued_evidence=RoleEvidenceV5(
                5,
                (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", evidence_value),),
            ),
            expected_binding=RoleBindingV5(
                fixture.manifest.baseline_policy_revision.sha256,
                None,
                (),
                fixture.manifest.panel_plan.discovery_plan_sha256,
            ),
            schema_authority=role_schema_authority_from_manifest_v5(
                role="investigator", manifest=fixture.manifest.manifest
            ),
            max_output_tokens=128,
        )

    request = fixture_request(primary_fixture)
    withheld_request = fixture_request(withheld_fixture)

    def preflight_for(fixture, fixture_request, *, arm: str, campaign_id: str | None = None):
        repository = fixture.repository
        reducer = CandidateArchiveReducerV5(
            discovery_plan=fixture.manifest.panel_plan,
            evaluator_contract=fixture.manifest.evaluator_contract,
            target=fixture.manifest.manifest.target,
            authorities=(),
            capacity=fixture.manifest.manifest.search.archive_capacity,
            pit_data_scope="production",
            semantic_mode="required",
        )
        checkpoint = repository.publish_projection(record_refs=(), reducer=reducer, generation=1)
        checkpoint_bytes = canonical_json_bytes_v5(checkpoint.to_primitive())
        checkpoint_ref = ArtifactRefV5("checkpoint.json", hashlib.sha256(checkpoint_bytes).hexdigest())
        snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
        snapshot_bytes = repository.authenticate_exact(snapshot_ref).content
        call = RoleCallKeyV5(
            campaign_id=campaign_id or fixture.manifest.manifest.campaign_id,
            round_index=2,
            role="investigator",
            role_position=1,
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=fixture_request.sha256,
        )
        request_ref = repository.append_role_request(call=call, request=fixture_request).reference
        evidence_refs: tuple[ArtifactRefV5, ...] = ()
        evidence_bytes: tuple[bytes, ...] = ()
        if persist_evidence:
            frozen_evidence = canonical_json_bytes_v5(
                {
                    "evidence_id": "v5.fixture.evidence",
                    "metric_id": "evaluator.exit_attribution_count",
                    "value": evidence_value,
                }
            )
            evidence_ref = repository.append_binary_state(
                namespace="study-evidence",
                key="v5.fixture.evidence",
                content=frozen_evidence,
            )
            evidence_refs = (evidence_ref,)
            evidence_bytes = (frozen_evidence,)
        return FixturePreflightV1(
            arm=arm,
            mode=mode,
            fixture_root_identity_sha256=repository.root_identity_sha256,
            checkpoint_ref=checkpoint_ref,
            snapshot_ref=snapshot_ref,
            call=call,
            request_ref=request_ref,
            request_bytes=repository.authenticate_exact(request_ref).content,
            schema_json=fixture_request.schema_authority.canonical_schema_json,
            evidence_refs=evidence_refs,
            request_sha256=fixture_request.sha256,
            fixture_root_locator=str(repository.root),
            checkpoint_bytes=checkpoint_bytes,
            snapshot_bytes=snapshot_bytes,
            evidence_bytes=evidence_bytes,
        )

    primary_preflight = preflight_for(
        primary_fixture,
        request,
        arm="primary",
        campaign_id=primary_campaign_id,
    )
    withheld_preflight = preflight_for(
        withheld_fixture,
        withheld_request,
        arm="withheld",
        campaign_id=withheld_campaign_id,
    )
    root = tmp_path / "task4-honest-study-ledger"
    root.mkdir()
    store = StudyStoreV1(LocalArtifactRepositoryV5(root))
    primary_ref = store.put(kind="preflights", key="primary", content=primary_preflight.canonical_bytes())
    withheld_ref = store.put(kind="preflights", key="withheld", content=withheld_preflight.canonical_bytes())
    rubric = b"synthetic-task3-rubric-v1"
    store.put(kind="rubrics", key="unit", content=rubric)
    registry = build_study_registry_v1()
    schema = study_response_schema_v1(
        fixture_request=request,
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    manifest = StudyManifestV1(
        schema_version=1,
        study_id="study-ledger",
        mode=mode,
        source_revision="a" * 40,
        fixture_sha256=(
            primary_fixture.manifest.manifest_ref.sha256
            if study_fixture_sha256 is None
            else study_fixture_sha256
        ),
        registry_sha256=registry.sha256,
        rubric_sha256=hashlib.sha256(rubric).hexdigest(),
        schema_sha256=hashlib.sha256(schema).hexdigest(),
        parser_sha256=hashlib.sha256(study_parser_authority_bytes_v1()).hexdigest(),
        prompt_sha256=hashlib.sha256(study_prompt_bytes_v1(registry=registry)).hexdigest(),
        round_one_checkpoint_ref=primary_preflight.checkpoint_ref,
        round_one_snapshot_ref=primary_preflight.snapshot_ref,
        primary_preflight_ref=primary_ref,
        withheld_preflight_ref=withheld_ref,
        resource_limits=study_resource_budget_v1(),
        provider_settings=(
            None if mode == "offline_fixture" else StudyProviderSettingsV1("openrouter", "openai/test-model", 128)
        ),
        offline_settings=(StudyOfflineSettingsV1("fixture-test") if mode == "offline_fixture" else None),
    )
    live_request = build_study_call_v1(
        preflight=primary_preflight,
        manifest=manifest,
        fixture_request=request,
        registry=registry,
    )
    withheld_live_request = build_study_call_v1(
        preflight=withheld_preflight,
        manifest=manifest,
        fixture_request=withheld_request,
        registry=registry,
    )
    grant = StudyGrantV1(
        study_id=manifest.study_id,
        manifest_sha256=manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="study-audit",
        mode=mode,
        provider=("offline_fixture" if mode == "offline_fixture" else "openrouter"),
        model=("offline_fixture/study-v1" if mode == "offline_fixture" else "openai/test-model"),
        arm_slots=("primary", "withheld"),
        input_token_ceiling=1_000_000,
        output_token_ceiling=128,
        cumulative_token_ceiling=1_000_000,
        per_call_usd_ceiling=Decimal("1"),
        cumulative_usd_ceiling=Decimal("2"),
        per_call_deadline_seconds=Decimal("120"),
        input_price_upper_bound=Decimal("1"),
        output_price_upper_bound=Decimal("1"),
        response_persistence_consent=True,
        operator_approval_reference=("offline-fixture:test" if mode == "offline_fixture" else "operator:test"),
    )
    approval = authorize_study_execution_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    ledger = StudyLedgerV1(store, manifest, grant, approval)
    return (
        primary_fixture,
        request,
        primary_preflight,
        manifest,
        live_request,
        store,
        grant,
        approval,
        ledger,
        withheld_fixture,
        withheld_request,
        withheld_preflight,
        withheld_live_request,
    )


def _delegated_author_context(tmp_path: Path):
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _hypothesis

    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(
        tmp_path
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    invoker = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=_StudyFixtureAuthorDelegate(fixture.manifest.manifest),
    )
    baseline = fixture.manifest.baseline_policy_revision
    hypothesis = _hypothesis()
    request = build_role_request_v5(
        role="author",
        role_input=AuthorRoleInputV5(
            fixture.manifest.baseline_authority.source_bundle.files,
            False,
            hypothesis,
            AuthorPolicyContractsV5(baseline, fixture.manifest.manifest.policy_scope_ref.sha256),
        ),
        issued_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
        ),
        expected_binding=RoleBindingV5(
            baseline.sha256,
            hypothesis.hypothesis_id,
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
    return fixture, invoker, request


def _delegated_critic_context(tmp_path: Path):
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _hypothesis

    fixture, invoker, _author_request = _delegated_author_context(tmp_path)
    baseline = fixture.manifest.baseline_policy_revision
    hypothesis = _hypothesis()
    experiment_id = "a" * 64
    request = build_role_request_v5(
        role="critic",
        role_input=CriticRoleInputV5(
            (
                ExperimentEvaluationAggregateV5(
                    experiment_id,
                    "zero_trade",
                    (ScenarioAggregateV5("quick", None, "base", ("v5.fixture.eval",)),),
                ),
            ),
            (
                ExperimentPredictionAggregateV5(
                    experiment_id,
                    (MetricPredictionV5("exit.decision_changed_count", "increase", "fixture prediction"),),
                ),
            ),
            (),
            (ExperimentFailureAggregateV5(experiment_id, "none", None, ("v5.fixture.failure",)),),
            True,
        ),
        issued_evidence=RoleEvidenceV5(
            5,
            (
                RoleEvidenceItemV5("v5.fixture.eval", "evaluator.exit_attribution_count", 1),
                RoleEvidenceItemV5("v5.fixture.failure", "failure.typed_count", 0),
            ),
        ),
        expected_binding=RoleBindingV5(
            baseline.sha256,
            hypothesis.hypothesis_id,
            (experiment_id,),
            fixture.manifest.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="critic",
            manifest=fixture.manifest.manifest,
        ),
        max_output_tokens=128,
    )
    return fixture, invoker, request


def _persist_delegated_author(
    fixture,
    request,
    *,
    existing: bool,
):
    call = RoleCallKeyV5(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
        role=request.role,
        role_position={"author": 2, "critic": 3}[request.role],
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    reference = fixture.repository.append_role_request(call=call, request=request).reference
    persisted_type = ExistingPersistedRoleRequestV5 if existing else FreshPersistedRoleRequestV5
    return persisted_type(reference=reference, call=call, request=request)


def _unbound_parent_context(tmp_path: Path):
    """Build a fully persisted study chain whose F parent is unauthorized."""

    context = _task4_honest_study_context(tmp_path, mode="live_study")
    (
        fixture,
        valid_request,
        valid_preflight,
        study_manifest,
        _valid_live_request,
        store,
        grant,
        _approval,
        _ledger,
        _withheld_fixture,
        _withheld_request,
        withheld_preflight,
        _withheld_live_request,
    ) = context
    bad_request = replace(
        valid_request,
        expected_binding=RoleBindingV5(
            "f" * 64,
            None,
            (),
            fixture.manifest.panel_plan.discovery_plan_sha256,
        ),
    )
    bad_call = RoleCallKeyV5(
        campaign_id=valid_preflight.call.campaign_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=bad_request.sha256,
    )
    bad_request_ref = fixture.repository.append_role_request(call=bad_call, request=bad_request).reference
    bad_preflight = replace(
        valid_preflight,
        call=bad_call,
        request_ref=bad_request_ref,
        request_bytes=fixture.repository.authenticate_exact(bad_request_ref).content,
        request_sha256=bad_request.sha256,
        schema_json=bad_request.schema_authority.canonical_schema_json,
    )
    unbound_root = tmp_path / "unbound-study-ledger"
    unbound_root.mkdir()
    store = StudyStoreV1(LocalArtifactRepositoryV5(unbound_root))
    bad_preflight_ref = store.put(kind="preflights", key="primary", content=bad_preflight.canonical_bytes())
    withheld_preflight_ref = store.put(
        kind="preflights",
        key="withheld",
        content=withheld_preflight.canonical_bytes(),
    )
    store.put(kind="rubrics", key="unit", content=b"synthetic-task3-rubric-v1")
    bad_manifest = replace(
        study_manifest,
        study_id="study-unbound-parent",
        primary_preflight_ref=bad_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
    )
    registry = build_study_registry_v1()
    bad_live_request = build_study_call_v1(
        preflight=bad_preflight,
        manifest=bad_manifest,
        fixture_request=bad_request,
        registry=registry,
    )
    bad_grant = replace(
        grant,
        study_id=bad_manifest.study_id,
        manifest_sha256=bad_manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
    )
    bad_approval = authorize_study_execution_v1(
        store=store,
        manifest=bad_manifest,
        grant=bad_grant,
        approval_reference=bad_grant.operator_approval_reference,
    )
    bad_ledger = StudyLedgerV1(store, bad_manifest, bad_grant, bad_approval)
    bad_terminal = run_study_call_v1(
        request=bad_live_request,
        fixture_request=bad_request,
        ledger=bad_ledger,
        gateway=_CountingFake(
            {
                bad_live_request.sha256: _completion(
                    request=bad_live_request,
                    response_text=_response_for(bad_request).canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    return fixture, bad_request, bad_preflight, bad_preflight_ref, bad_live_request, store, bad_ledger, bad_terminal


def _persist_malformed_import(
    *,
    fixture,
    request,
    preflight,
    preflight_ref,
    live_request,
    store,
    ledger,
    terminal,
):
    verified_terminal = ledger.verify_terminal(terminal.reference)
    raw_response = store.read(verified_terminal.terminal.response_ref)
    registry = build_study_registry_v1()
    parsed = parse_study_response_v1(
        raw=raw_response,
        fixture_request=request,
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    translated = canonical_json_bytes_v5(
        {
            "binding": request.expected_binding.to_primitive(),
            "artifact": canonical_primitive_v5(parsed.artifact),
        }
    )
    ordinary_artifact = canonical_json_bytes_v5(canonical_primitive_v5(parsed.artifact))
    live_request_ref = next(
        ref
        for ref in store.list_refs(kind="requests")
        if ref.relative_path == f"adapter-blobs/study-v1-requests/{live_request.sha256}.bin"
    )
    materials = study_imports._ImportMaterials(
        fixture_request=request,
        live_request=live_request,
        live_request_ref=live_request_ref,
        fixture_preflight=preflight,
        fixture_preflight_ref=preflight_ref,
        fixture_manifest_ref=fixture.manifest.manifest_ref,
        terminal=verified_terminal,
        raw_response=raw_response,
        parsed_response=parsed,
        translated=translated,
        ordinary_artifact=ordinary_artifact,
        drafts=parsed.drafts,
    )
    return study_imports._record_from_materials(store=store, ledger=ledger, materials=materials)


def test_authenticated_live_import_replays_exact_t_with_zero_fixture_usage(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, manifest, live_request, store, ledger, terminal = _live_import_context(
        tmp_path
    )

    imported = create_study_import_v1(
        store=store,
        ledger=ledger,
        preflight=preflight,
        terminal=terminal,
    )
    translated = authenticate_study_import_v1(
        store=store,
        ledger=ledger,
        record=imported,
        fixture_repository=fixture.repository,
        request=fixture_request,
        call=preflight.call,
    )

    assert translated == store.read(imported.translated_ref)
    assert translated != store.read(terminal.terminal.response_ref)
    persisted = ExistingPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=fixture_request,
    )
    invoker = StudyImportInvokerV1(
        manifest=fixture.manifest,
        imported=imported,
        store=store,
        ledger=ledger,
        scripted_role_delegate=_ForbiddenScriptedDelegate(),
    )
    fresh = FreshPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=fixture_request,
    )
    fresh_package = invoker.invoke_once(fresh, deadline_monotonic=time.monotonic() + 30.0)
    assert fresh_package.accepted
    assert fresh_package.attempt.usage.external_attempt_count == 0
    package = invoker.reconcile_once(persisted)

    assert type(package) is RoleInvocationPackageV5
    assert package.accepted
    assert type(package.terminal_authority) is FixtureRoleTerminalAuthorityV5
    assert package.attempt.usage.external_attempt_count == 0
    assert package.artifact == _response_for(fixture_request).artifact
    verify_imported_package_v1(
        package=package,
        record=imported,
        store=store,
        ledger=ledger,
        fixture_repository=fixture.repository,
    )


def test_public_import_paths_reject_an_unbound_parent_including_persisted_package(tmp_path: Path) -> None:
    (
        fixture,
        request,
        preflight,
        preflight_ref,
        live_request,
        store,
        ledger,
        terminal,
    ) = _unbound_parent_context(tmp_path)
    with pytest.raises(StudyAuthorityError, match="parent|checkpoint-authorized"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)

    # Simulate an import persisted by the pre-fix implementation.  The record
    # is internally consistent and is then checked through the public recovery
    # and independent-package paths, without stubbing either verifier.
    malformed = _persist_malformed_import(
        fixture=fixture,
        request=request,
        preflight=preflight,
        preflight_ref=preflight_ref,
        live_request=live_request,
        store=store,
        ledger=ledger,
        terminal=terminal,
    )
    with pytest.raises(StudyAuthorityError, match="parent|checkpoint-authorized"):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=malformed,
            fixture_repository=fixture.repository,
            request=request,
            call=preflight.call,
        )
    runner = FixtureRoleRunnerV5(
        responses={"investigator": (store.read(malformed.translated_ref).decode("utf-8"),)},
        campaign_fixture=False,
    )
    artifact = runner.invoke_once(request)
    attempt = runner.attempts[-1]
    package = RoleInvocationPackageV5(
        preflight.call,
        request,
        attempt,
        FixtureRoleTerminalAuthorityV5(
            "study-import-v1",
            preflight.call.sha256,
            request.sha256,
            attempt.sha256,
            attempt.artifact_sha256,
        ),
        artifact,
    )
    with pytest.raises(StudyAuthorityError, match="parent|checkpoint-authorized"):
        verify_imported_package_v1(
            package=package,
            record=malformed,
            store=store,
            ledger=ledger,
            fixture_repository=fixture.repository,
        )


def test_authenticated_offline_import_preserves_simulated_study_mode(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, live_request, store, ledger, terminal = _live_import_context(
        tmp_path,
        mode="offline_fixture",
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    assert imported.mode == "offline_fixture"
    assert terminal.terminal.usage.external_attempt_count == 1
    fake_delegate = _ForbiddenScriptedDelegate()
    persisted = ExistingPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=fixture_request,
    )
    package = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=fake_delegate,
    ).reconcile_once(persisted)
    assert package.attempt.usage.external_attempt_count == 0
    assert package.artifact == _response_for(fixture_request).artifact


def test_public_import_rejects_foreign_fixture_campaign_but_keeps_study_l_distinct(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, live_request, store, ledger, terminal = _live_import_context(
        tmp_path,
        primary_campaign_id="foreign-fixture-campaign",
    )
    assert live_request.study_id != preflight.call.campaign_id
    with pytest.raises(StudyAuthorityError, match="campaign manifest"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)


def test_runtime_provider_free_authority_discriminator_rejects_mixed_packages(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    package = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=_ForbiddenScriptedDelegate(),
    ).reconcile_once(
        ExistingPersistedRoleRequestV5(
            reference=preflight.request_ref,
            call=preflight.call,
            request=fixture_request,
        )
    )
    runtime = object.__new__(__import__("core.pit_optimizer_v5.runtime", fromlist=["_Runtime"])._Runtime)
    runtime.inputs = SimpleNamespace(manifest=fixture.manifest.manifest)
    controller_authority = ControllerRoleTerminalAuthorityV5(
        package.call.sha256,
        package.request.sha256,
        package.attempt.sha256,
        ArtifactRefV5(
            f"adapter-blobs/controller-role-responses/{package.call.sha256}.bin",
            package.attempt.response_sha256,  # type: ignore[arg-type]
        ),
        package.attempt.artifact_sha256,
    )
    controller_package = RoleInvocationPackageV5(
        package.call,
        package.request,
        package.attempt,
        controller_authority,
        package.artifact,
    )
    assert runtime._valid_terminal_authority(package)
    assert not runtime._valid_terminal_authority(controller_package)
    runtime.inputs = SimpleNamespace(
        manifest=replace(
            fixture.manifest.manifest,
            pit_data_scope="development_sp500_v2",
            semantic_mode="disabled_development",
        )
    )
    assert runtime._valid_terminal_authority(controller_package)
    assert not runtime._valid_terminal_authority(package)


def test_invoker_delegates_valid_author_and_critic_slots_and_rejects_wrong_slot(tmp_path: Path) -> None:
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _hypothesis

    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    invoker = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=_StudyFixtureAuthorDelegate(fixture.manifest.manifest),
    )
    baseline = fixture.manifest.baseline_policy_revision
    evidence = RoleEvidenceV5(
        5,
        (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
    )
    hypothesis = _hypothesis()
    author_request = build_role_request_v5(
        role="author",
        role_input=AuthorRoleInputV5(
            fixture.manifest.baseline_authority.source_bundle.files,
            False,
            hypothesis,
            AuthorPolicyContractsV5(baseline, fixture.manifest.manifest.policy_scope_ref.sha256),
        ),
        issued_evidence=evidence,
        expected_binding=RoleBindingV5(
            baseline.sha256,
            hypothesis.hypothesis_id,
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
    experiment_id = "a" * 64
    critic_evidence = RoleEvidenceV5(
        5,
        (
            RoleEvidenceItemV5("v5.fixture.eval", "evaluator.exit_attribution_count", 1),
            RoleEvidenceItemV5("v5.fixture.failure", "failure.typed_count", 0),
        ),
    )
    critic_request = build_role_request_v5(
        role="critic",
        role_input=CriticRoleInputV5(
            (
                ExperimentEvaluationAggregateV5(
                    experiment_id,
                    "zero_trade",
                    (ScenarioAggregateV5("quick", None, "base", ("v5.fixture.eval",)),),
                ),
            ),
            (
                ExperimentPredictionAggregateV5(
                    experiment_id,
                    (MetricPredictionV5("exit.decision_changed_count", "increase", "fixture prediction"),),
                ),
            ),
            (),
            (ExperimentFailureAggregateV5(experiment_id, "none", None, ("v5.fixture.failure",)),),
            True,
        ),
        issued_evidence=critic_evidence,
        expected_binding=RoleBindingV5(
            baseline.sha256,
            hypothesis.hypothesis_id,
            (experiment_id,),
            fixture.manifest.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="critic",
            manifest=fixture.manifest.manifest,
        ),
        max_output_tokens=128,
    )

    def fresh(request, *, role: str, position: int, attempt_kind: str = "primary"):
        call = RoleCallKeyV5(
            campaign_id=fixture.manifest.manifest.campaign_id,
            round_index=2,
            role=role,
            role_position=position,
            attempt_kind=attempt_kind,
            attempt_index=1,
            request_sha256=request.sha256,
        )
        reference = fixture.repository.append_role_request(call=call, request=request).reference
        return FreshPersistedRoleRequestV5(reference=reference, call=call, request=request)

    author_persisted = fresh(author_request, role="author", position=2)
    critic_persisted = fresh(critic_request, role="critic", position=3)
    author_package = invoker.invoke_once(author_persisted, deadline_monotonic=time.monotonic() + 30.0)
    critic_package = invoker.invoke_once(critic_persisted, deadline_monotonic=time.monotonic() + 30.0)
    assert author_package.accepted
    assert critic_package.accepted
    assert author_package.attempt.usage.external_attempt_count == 0
    assert critic_package.attempt.usage.external_attempt_count == 0

    invalid_slot = fresh(author_request, role="author", position=2, attempt_kind="retry")
    with pytest.raises(StudyAuthorityError, match="exact fixture manifest slot"):
        invoker.invoke_once(invalid_slot, deadline_monotonic=time.monotonic() + 30.0)


@pytest.mark.parametrize("persisted_kind", ("fresh", "existing"))
@pytest.mark.parametrize("authority_kind", ("schema", "discovery"))
@pytest.mark.parametrize("role", ("author", "critic"))
def test_delegated_request_authority_is_authenticated_before_dispatch(
    tmp_path: Path,
    persisted_kind: str,
    authority_kind: str,
    role: str,
) -> None:
    context_builder = _delegated_author_context if role == "author" else _delegated_critic_context
    fixture, invoker, request = context_builder(tmp_path)
    if authority_kind == "schema":
        altered_manifest = replace(
            fixture.manifest.manifest,
            search=replace(fixture.manifest.manifest.search, max_variants_per_template=13),
        )
        altered_schema = role_schema_authority_from_manifest_v5(
            role=role,
            manifest=altered_manifest,
            author_policy_paths=(
                tuple(item.path for item in fixture.manifest.baseline_authority.source_bundle.files)
                if role == "author"
                else ()
            ),
        )
        request = replace(request, schema_authority=altered_schema)
    else:
        request = replace(
            request,
            expected_binding=replace(request.expected_binding, discovery_plan_sha256="f" * 64),
        )
    persisted = _persist_delegated_author(
        fixture,
        request,
        existing=persisted_kind == "existing",
    )
    with pytest.raises(StudyAuthorityError, match="manifest|schema|discovery|authority"):
        if persisted_kind == "fresh":
            invoker.invoke_once(persisted, deadline_monotonic=time.monotonic() + 30.0)
        else:
            invoker.reconcile_once(persisted)


@pytest.mark.parametrize("persisted_kind", ("fresh", "existing"))
def test_delegated_request_must_be_persisted_in_the_authenticated_fixture_root(
    tmp_path: Path,
    persisted_kind: str,
) -> None:
    fixture, invoker, _request = _delegated_author_context(tmp_path / "origin")
    other_fixture, _other_invoker, other_request = _delegated_author_context(tmp_path / "foreign")
    foreign_persisted = _persist_delegated_author(
        other_fixture,
        other_request,
        existing=persisted_kind == "existing",
    )
    with pytest.raises(StudyAuthorityError, match="persisted|authenticated|request"):
        if persisted_kind == "fresh":
            invoker.invoke_once(foreign_persisted, deadline_monotonic=time.monotonic() + 30.0)
        else:
            invoker.reconcile_once(foreign_persisted)


@pytest.mark.parametrize("persisted_kind", ("fresh", "existing"))
@pytest.mark.parametrize(
    "tampered_field",
    (
        "fixture_manifest_ref",
        "fixture_root_identity_sha256",
        "ledger_root_identity_sha256",
    ),
)
def test_delegated_replay_reauthenticates_import_record_before_dispatch(
    tmp_path: Path,
    persisted_kind: str,
    tampered_field: str,
) -> None:
    fixture, valid_invoker, request = _delegated_author_context(tmp_path)
    imported = valid_invoker._record
    if tampered_field == "fixture_manifest_ref":
        foreign_manifest = replace(fixture.manifest.manifest, campaign_id="foreign-fixture-campaign")
        tampered = replace(
            imported,
            fixture_manifest_ref=ArtifactRefV5(
                fixture.manifest.manifest_ref.relative_path,
                foreign_manifest.sha256,
            ),
        )
    else:
        tampered = replace(imported, **{tampered_field: "f" * 64})

    class _NoDispatchDelegate(_StudyFixtureAuthorDelegate):
        def _invoke(self, persisted):
            raise AssertionError("delegated role was invoked before import authentication")

    invoker = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=tampered,
        store=valid_invoker._store,
        ledger=valid_invoker._ledger,
        delegate=_NoDispatchDelegate(fixture.manifest.manifest),
    )
    persisted = _persist_delegated_author(
        fixture,
        request,
        existing=persisted_kind == "existing",
    )
    with pytest.raises(StudyAuthorityError, match="import|manifest|root|ledger|authority"):
        if persisted_kind == "fresh":
            invoker.invoke_once(persisted, deadline_monotonic=time.monotonic() + 30.0)
        else:
            invoker.reconcile_once(persisted)


@pytest.mark.parametrize("persisted_kind", ("fresh", "existing"))
@pytest.mark.parametrize("authority_kind", ("controller", "ledger"))
def test_delegated_nonfixture_authority_is_rejected_after_request_authentication(
    tmp_path: Path,
    persisted_kind: str,
    authority_kind: str,
) -> None:
    class _NonFixtureDelegate(_StudyFixtureAuthorDelegate):
        def _invoke(self, persisted):
            package = super()._invoke(persisted)
            if authority_kind == "controller":
                authority = ControllerRoleTerminalAuthorityV5(
                    package.call.sha256,
                    package.request.sha256,
                    package.attempt.sha256,
                    ArtifactRefV5(
                        f"adapter-blobs/controller-role-responses/{package.call.sha256}.bin",
                        package.attempt.response_sha256,
                    ),
                    package.attempt.artifact_sha256,
                )
            else:
                receipt_payload = {
                    "slot_id": package.attempt.slot_id,
                    "slot_request_sha256": package.request.sha256,
                    "authorization_sha256": "a" * 64,
                    "attempt_facts_sha256": package.attempt.sha256,
                    "cumulative_external_attempts": 0,
                    "cumulative_total_tokens": 0,
                    "cumulative_cost_usd": Decimal("0"),
                    "terminal_sequence": 1,
                }
                authority = LedgerRoleTerminalAuthorityV5(
                    package.call.sha256,
                    RoleTerminalReceiptV5(
                        **receipt_payload,
                        receipt_sha256=hashlib.sha256(canonical_json_bytes_v5(receipt_payload)).hexdigest(),
                    ),
                )
            return replace(package, terminal_authority=authority)

    fixture, invoker, request = _delegated_author_context(tmp_path)
    invoker._delegate = _NonFixtureDelegate(fixture.manifest.manifest)
    persisted = _persist_delegated_author(
        fixture,
        request,
        existing=persisted_kind == "existing",
    )
    with pytest.raises(StudyAuthorityError, match="fixture authority"):
        if persisted_kind == "fresh":
            invoker.invoke_once(persisted, deadline_monotonic=time.monotonic() + 30.0)
        else:
            invoker.reconcile_once(persisted)


def test_fixture_replay_accepts_a_checkpoint_authorized_nonbaseline_parent(tmp_path: Path) -> None:
    # Build a real archived candidate and frozen checkpoint using the existing
    # synthetic authority graph.  This guards the round-two parent seam without
    # pinning F to the campaign baseline revision.
    from tests.test_pit_optimizer_v5_mechanism_artifacts import _capability, _persist_evaluated_record

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    mechanism_repository, capability, candidate_bundle, candidate_revision = _capability(fixture_root)
    _persist_evaluated_record(mechanism_repository, capability, candidate_bundle, candidate_revision)
    repository = mechanism_repository.repository
    authenticated_manifest: AuthenticatedCampaignManifestV5 = capability.authenticated_manifest
    assert candidate_revision.sha256 != authenticated_manifest.baseline_policy_revision.sha256
    request = build_role_request_v5(
        role="investigator",
        role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
        issued_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
        ),
        expected_binding=RoleBindingV5(
            candidate_revision.sha256,
            None,
            (),
            authenticated_manifest.panel_plan.discovery_plan_sha256,
        ),
        schema_authority=role_schema_authority_from_manifest_v5(
            role="investigator", manifest=authenticated_manifest.manifest
        ),
        max_output_tokens=128,
    )
    checkpoint = repository.load_checkpoint()
    assert checkpoint is not None and checkpoint.record_refs
    checkpoint_bytes = canonical_json_bytes_v5(checkpoint.to_primitive())
    checkpoint_ref = ArtifactRefV5("checkpoint.json", hashlib.sha256(checkpoint_bytes).hexdigest())
    snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    snapshot_bytes = repository.authenticate_exact(snapshot_ref).content
    call = RoleCallKeyV5(
        campaign_id=authenticated_manifest.manifest.campaign_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    request_ref = repository.append_role_request(call=call, request=request).reference
    preflight = FixturePreflightV1(
        arm="primary",
        mode="live_study",
        fixture_root_identity_sha256=repository.root_identity_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        call=call,
        request_ref=request_ref,
        request_bytes=repository.authenticate_exact(request_ref).content,
        schema_json=request.schema_authority.canonical_schema_json,
        evidence_refs=(),
        request_sha256=request.sha256,
        fixture_root_locator=str(repository.root),
        checkpoint_bytes=checkpoint_bytes,
        snapshot_bytes=snapshot_bytes,
        evidence_bytes=(),
    )
    study_imports.StudyImportInvokerV1._verify_fixture_parent(
        repository=repository,
        preflight=preflight,
        request=request,
        authenticated_manifest=authenticated_manifest,
    )
    stored_record = repository.load_experiment(checkpoint.record_refs[0])
    source_ref = next(
        item
        for item in stored_record.artifact_refs
        if item.relative_path == f"adapter-state/policy-source/{candidate_revision.sha256}.json"
    )
    source_bundle = repository.load_typed_artifact(source_ref, value_type=SourceBundleV5)
    reducer = CandidateArchiveReducerV5(
        discovery_plan=authenticated_manifest.panel_plan,
        evaluator_contract=authenticated_manifest.evaluator_contract,
        target=authenticated_manifest.manifest.target,
        authorities=(
            ArchiveRecordAuthorityV5(
                experiment_id=stored_record.experiment_id,
                experiment_record_ref=checkpoint.record_refs[0],
                source_bundle=source_bundle,
                source_bundle_ref=source_ref,
            ),
        ),
        capacity=authenticated_manifest.manifest.search.archive_capacity,
        pit_data_scope=authenticated_manifest.manifest.pit_data_scope,
        semantic_mode=authenticated_manifest.manifest.semantic_mode,
    )
    repository.publish_projection(
        record_refs=checkpoint.record_refs,
        reducer=reducer,
        generation=checkpoint.generation + 1,
    )
    # F remains authorized by its frozen checkpoint after the live repository
    # advances to a later projection.
    study_imports.StudyImportInvokerV1._verify_fixture_parent(
        repository=repository,
        preflight=preflight,
        request=request,
        authenticated_manifest=authenticated_manifest,
    )
    with pytest.raises(StudyAuthorityError, match="checkpoint-authorized"):
        study_imports.StudyImportInvokerV1._verify_fixture_parent(
            repository=repository,
            preflight=preflight,
            request=__import__("dataclasses").replace(
                request,
                expected_binding=RoleBindingV5(
                    "f" * 64,
                    None,
                    (),
                    authenticated_manifest.panel_plan.discovery_plan_sha256,
                ),
            ),
            authenticated_manifest=authenticated_manifest,
        )


def test_public_invoker_replays_a_persisted_nonbaseline_parent_import(tmp_path: Path) -> None:
    """Exercise nonbaseline parent authentication through the real import replay."""

    from tests.test_pit_optimizer_v5_mechanism_artifacts import _capability, _persist_evaluated_record
    from core.pit_optimizer_v5.two_round_study.live_calls import FixturePreflightV1

    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    mechanism_repository, capability, _candidate_bundle, candidate_revision = _capability(fixture_root)
    _persist_evaluated_record(mechanism_repository, capability, _candidate_bundle, candidate_revision)
    fixture_repository = mechanism_repository.repository
    withheld_fixture_root = tmp_path / "withheld-fixture"
    withheld_fixture_root.mkdir()
    withheld_mechanism_repository, withheld_capability, withheld_bundle, withheld_revision = _capability(
        withheld_fixture_root
    )
    _persist_evaluated_record(
        withheld_mechanism_repository,
        withheld_capability,
        withheld_bundle,
        withheld_revision,
    )
    withheld_fixture_repository = withheld_mechanism_repository.repository
    authenticated_manifest: AuthenticatedCampaignManifestV5 = capability.authenticated_manifest

    def request_for(evidence_value: int):
        return build_role_request_v5(
            role="investigator",
            role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
            issued_evidence=RoleEvidenceV5(
                5,
                (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", evidence_value),),
            ),
            expected_binding=RoleBindingV5(
                candidate_revision.sha256,
                None,
                (),
                authenticated_manifest.panel_plan.discovery_plan_sha256,
            ),
            schema_authority=role_schema_authority_from_manifest_v5(
                role="investigator", manifest=authenticated_manifest.manifest
            ),
            max_output_tokens=128,
        )

    checkpoint = fixture_repository.load_checkpoint()
    assert checkpoint is not None and checkpoint.record_refs
    checkpoint_bytes = canonical_json_bytes_v5(checkpoint.to_primitive())
    checkpoint_ref = ArtifactRefV5("checkpoint.json", hashlib.sha256(checkpoint_bytes).hexdigest())
    snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)

    def preflight_for(repository, request, *, arm: str, campaign_id: str):
        local_checkpoint = repository.load_checkpoint()
        assert local_checkpoint is not None and local_checkpoint.record_refs
        local_checkpoint_bytes = canonical_json_bytes_v5(local_checkpoint.to_primitive())
        local_checkpoint_ref = ArtifactRefV5(
            "checkpoint.json",
            hashlib.sha256(local_checkpoint_bytes).hexdigest(),
        )
        local_snapshot_ref = ArtifactRefV5("archive.json", local_checkpoint.archive_sha256)
        local_snapshot_bytes = repository.authenticate_exact(local_snapshot_ref).content
        call = RoleCallKeyV5(
            campaign_id=campaign_id,
            round_index=2,
            role="investigator",
            role_position=1,
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=request.sha256,
        )
        request_ref = repository.append_role_request(call=call, request=request).reference
        return FixturePreflightV1(
            arm=arm,
            mode="live_study",
            fixture_root_identity_sha256=repository.root_identity_sha256,
            checkpoint_ref=local_checkpoint_ref,
            snapshot_ref=local_snapshot_ref,
            call=call,
            request_ref=request_ref,
            request_bytes=repository.authenticate_exact(request_ref).content,
            schema_json=request.schema_authority.canonical_schema_json,
            evidence_refs=(),
            request_sha256=request.sha256,
            fixture_root_locator=str(repository.root),
            checkpoint_bytes=local_checkpoint_bytes,
            snapshot_bytes=local_snapshot_bytes,
            evidence_bytes=(),
        )

    request = request_for(1)
    withheld_request = request_for(2)
    preflight = preflight_for(
        fixture_repository,
        request,
        arm="primary",
        campaign_id=authenticated_manifest.manifest.campaign_id,
    )
    withheld_preflight = preflight_for(
        withheld_fixture_repository,
        withheld_request,
        arm="withheld",
        campaign_id=withheld_capability.authenticated_manifest.manifest.campaign_id,
    )
    stored_record = fixture_repository.load_experiment(checkpoint.record_refs[0])
    source_ref = next(
        item
        for item in stored_record.artifact_refs
        if item.relative_path == f"adapter-state/policy-source/{candidate_revision.sha256}.json"
    )
    source_bundle = fixture_repository.load_typed_artifact(source_ref, value_type=SourceBundleV5)
    reducer = CandidateArchiveReducerV5(
        discovery_plan=authenticated_manifest.panel_plan,
        evaluator_contract=authenticated_manifest.evaluator_contract,
        target=authenticated_manifest.manifest.target,
        authorities=(
            ArchiveRecordAuthorityV5(
                experiment_id=stored_record.experiment_id,
                experiment_record_ref=checkpoint.record_refs[0],
                source_bundle=source_bundle,
                source_bundle_ref=source_ref,
            ),
        ),
        capacity=authenticated_manifest.manifest.search.archive_capacity,
        pit_data_scope=authenticated_manifest.manifest.pit_data_scope,
        semantic_mode=authenticated_manifest.manifest.semantic_mode,
    )

    registry = build_study_registry_v1()
    schema = study_response_schema_v1(
        fixture_request=request,
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    from core.pit_optimizer_v5.two_round_study.live_calls import (
        study_parser_authority_bytes_v1,
        study_prompt_bytes_v1,
    )

    study_manifest = StudyManifestV1(
        schema_version=1,
        study_id="study-nonbaseline-import",
        mode="live_study",
        source_revision="a" * 40,
        fixture_sha256=authenticated_manifest.manifest_ref.sha256,
        registry_sha256=registry.sha256,
        rubric_sha256=hashlib.sha256(b"synthetic-task3-rubric-v1").hexdigest(),
        schema_sha256=hashlib.sha256(schema).hexdigest(),
        parser_sha256=hashlib.sha256(study_parser_authority_bytes_v1()).hexdigest(),
        prompt_sha256=hashlib.sha256(study_prompt_bytes_v1(registry=registry)).hexdigest(),
        round_one_checkpoint_ref=checkpoint_ref,
        round_one_snapshot_ref=snapshot_ref,
        primary_preflight_ref=ArtifactRefV5("pending", "0" * 64),
        withheld_preflight_ref=ArtifactRefV5("pending-withheld", "1" * 64),
        resource_limits=study_resource_budget_v1(),
        provider_settings=StudyProviderSettingsV1("openrouter", "openai/test-model", 128),
        offline_settings=None,
    )
    study_root = tmp_path / "study-ledger"
    study_root.mkdir()
    store = StudyStoreV1(LocalArtifactRepositoryV5(study_root))
    primary_preflight_ref = store.put(kind="preflights", key="primary", content=preflight.canonical_bytes())
    withheld_preflight_ref = store.put(kind="preflights", key="withheld", content=withheld_preflight.canonical_bytes())
    store.put(kind="rubrics", key="unit", content=b"synthetic-task3-rubric-v1")
    study_manifest = StudyManifestV1(
        schema_version=study_manifest.schema_version,
        study_id=study_manifest.study_id,
        mode=study_manifest.mode,
        source_revision=study_manifest.source_revision,
        fixture_sha256=study_manifest.fixture_sha256,
        registry_sha256=study_manifest.registry_sha256,
        rubric_sha256=study_manifest.rubric_sha256,
        schema_sha256=study_manifest.schema_sha256,
        parser_sha256=study_manifest.parser_sha256,
        prompt_sha256=study_manifest.prompt_sha256,
        round_one_checkpoint_ref=study_manifest.round_one_checkpoint_ref,
        round_one_snapshot_ref=study_manifest.round_one_snapshot_ref,
        primary_preflight_ref=primary_preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        resource_limits=study_manifest.resource_limits,
        provider_settings=study_manifest.provider_settings,
        offline_settings=study_manifest.offline_settings,
    )
    live_request = build_study_call_v1(
        preflight=preflight,
        manifest=study_manifest,
        fixture_request=request,
        registry=registry,
    )
    grant = StudyGrantV1(
        study_id=study_manifest.study_id,
        manifest_sha256=study_manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="study-audit",
        mode="live_study",
        provider="openrouter",
        model="openai/test-model",
        arm_slots=("primary", "withheld"),
        input_token_ceiling=1_000_000,
        output_token_ceiling=128,
        cumulative_token_ceiling=1_000_000,
        per_call_usd_ceiling=Decimal("1"),
        cumulative_usd_ceiling=Decimal("2"),
        per_call_deadline_seconds=Decimal("120"),
        input_price_upper_bound=Decimal("1"),
        output_price_upper_bound=Decimal("1"),
        response_persistence_consent=True,
        operator_approval_reference="operator:test",
    )
    approval = authorize_study_execution_v1(
        store=store,
        manifest=study_manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    ledger = StudyLedgerV1(store, study_manifest, grant, approval)
    terminal = run_study_call_v1(
        request=live_request,
        fixture_request=request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                live_request.sha256: _completion(
                    request=live_request,
                    response_text=_response_for(request).canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    persisted = ExistingPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=request,
    )
    current_projection = fixture_repository.load_checkpoint()
    assert current_projection is not None
    fixture_repository.publish_projection(
        record_refs=current_projection.record_refs,
        reducer=reducer,
        generation=current_projection.generation + 1,
    )
    authenticate_study_import_v1(
        store=store,
        ledger=ledger,
        record=imported,
        fixture_repository=fixture_repository,
        request=request,
        call=preflight.call,
    )
    package = StudyImportInvokerV1(
        fixture_manifest=authenticated_manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=_ForbiddenScriptedDelegate(),
    ).reconcile_once(persisted)
    assert type(package) is RoleInvocationPackageV5
    assert package.attempt.usage.external_attempt_count == 0
    assert package.artifact == _response_for(request).artifact
    verify_imported_package_v1(
        package=package,
        record=imported,
        store=store,
        ledger=ledger,
        fixture_repository=fixture_repository,
    )


def test_import_is_idempotent_after_a_translation_write_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    _fixture, _fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    original_put = store.put
    calls = 0

    def interrupt_after_first(*, kind: str, key: str, content: bytes):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StudyAuthorityError("simulated import interruption")
        return original_put(kind=kind, key=key, content=content)

    monkeypatch.setattr(store, "put", interrupt_after_first)
    with pytest.raises(StudyAuthorityError, match="interruption"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    monkeypatch.setattr(store, "put", original_put)
    recovered = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    assert recovered == create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)


def test_failed_or_missing_study_terminals_cannot_produce_an_import(tmp_path: Path) -> None:
    _fixture, fixture_request, preflight, _manifest, live_request, store, _grant, _approval, ledger, *_ = _study_context(
        tmp_path,
        mode="live_study",
    )
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion

    failed_terminal = run_study_call_v1(
        request=live_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                live_request.sha256: _completion(request=live_request, response_text="{}"),
            }
        ),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    assert failed_terminal.terminal.failure_code is not None
    with pytest.raises(StudyAuthorityError, match="failed|non-importable"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=failed_terminal)
    with pytest.raises((StudyAuthorityError, ValueError)):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=None)  # type: ignore[arg-type]

    pending_context = _study_context(tmp_path / "pending", mode="live_study")
    _pending_fixture, pending_f, _pending_preflight, _m, pending_l, pending_store, _g, _a, pending_ledger, *_ = pending_context
    with pytest.raises(StudyPendingAccounting):
        run_study_call_v1(
            request=pending_l,
            fixture_request=pending_f,
            ledger=pending_ledger,
            gateway=_CountingFake({pending_l.sha256: RuntimeError("transport interrupted")}),
            deadline_monotonic=time.monotonic() + 30.0,
        )
    assert pending_store.list_refs(kind="terminals") == ()


def test_live_import_rejects_an_offline_only_terminal_authority(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, live_request, store, ledger, terminal = _live_import_context(
        tmp_path,
        mode="offline_fixture",
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    live_context = _study_context(tmp_path / "live", mode="live_study")
    live_ledger = live_context[8]
    with pytest.raises(StudyAuthorityError, match="root|mode|admitted"):
        authenticate_study_import_v1(
            store=store,
            ledger=live_ledger,
            record=imported,
            fixture_repository=fixture.repository,
            request=fixture_request,
            call=preflight.call,
        )


def test_import_rejects_fixture_slot_and_request_local_evidence_tampering(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    for tampered_call in (
        replace(preflight.call, campaign_id="foreign-fixture-campaign"),
        replace(preflight.call, attempt_kind="retry"),
        replace(preflight.call, attempt_index=2),
        replace(preflight.call, role="author", role_position=2),
    ):
        with pytest.raises(StudyAuthorityError):
            authenticate_study_import_v1(
                store=store,
                ledger=ledger,
                record=imported,
                fixture_repository=fixture.repository,
                request=fixture_request,
                call=tampered_call,
            )
    changed_evidence = replace(
        fixture_request,
        role_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 2),),
        ),
    )
    with pytest.raises(StudyAuthorityError):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=imported,
            fixture_repository=fixture.repository,
            request=changed_evidence,
            call=preflight.call,
        )


def test_persisted_same_id_evidence_mutation_is_rejected_by_all_public_import_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(
        tmp_path,
        persist_evidence=True,
    )
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    frozen_ref = preflight.evidence_refs[0]
    changed_evidence = canonical_json_bytes_v5(
        {
            "evidence_id": "v5.fixture.evidence",
            "metric_id": "evaluator.exit_attribution_count",
            "value": 2,
        }
    )
    original_authenticate_exact = LocalArtifactRepositoryV5.authenticate_exact

    def tampered_evidence(repository, reference):
        authenticated = original_authenticate_exact(repository, reference)
        if repository.root == fixture.repository.root and reference == frozen_ref:
            return SimpleNamespace(content=changed_evidence)
        return authenticated

    monkeypatch.setattr(LocalArtifactRepositoryV5, "authenticate_exact", tampered_evidence)
    with pytest.raises(StudyAuthorityError, match="evidence"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)

    monkeypatch.setattr(LocalArtifactRepositoryV5, "authenticate_exact", original_authenticate_exact)

    persisted = ExistingPersistedRoleRequestV5(
        reference=preflight.request_ref,
        call=preflight.call,
        request=fixture_request,
    )
    package = StudyImportInvokerV1(
        fixture_manifest=fixture.manifest,
        record=imported,
        store=store,
        ledger=ledger,
        delegate=_ForbiddenScriptedDelegate(),
    ).reconcile_once(persisted)
    assert type(package) is RoleInvocationPackageV5

    monkeypatch.setattr(LocalArtifactRepositoryV5, "authenticate_exact", tampered_evidence)
    with pytest.raises(StudyAuthorityError, match="evidence"):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=imported,
            fixture_repository=fixture.repository,
            request=fixture_request,
            call=preflight.call,
        )
    with pytest.raises(StudyAuthorityError, match="evidence"):
        verify_imported_package_v1(
            package=package,
            record=imported,
            store=store,
            ledger=ledger,
            fixture_repository=fixture.repository,
        )


def test_study_manifest_fixture_digest_binds_actual_manifest_on_all_public_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(
        tmp_path / "creation",
        study_fixture_sha256="f" * 64,
    )
    with pytest.raises(StudyAuthorityError, match="fixture digest|fixture authority"):
        create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)

    original_authority = study_imports._authenticate_fixture_authority

    def seed_without_fixture_digest(**kwargs):
        kwargs["expected_fixture_sha256"] = None
        return original_authority(**kwargs)

    monkeypatch.setattr(study_imports, "_authenticate_fixture_authority", seed_without_fixture_digest)
    materials = study_imports._prepare_materials(
        store=store,
        ledger=ledger,
        preflight=preflight,
        terminal=terminal,
    )
    malformed = study_imports._record_from_materials(store=store, ledger=ledger, materials=materials)
    monkeypatch.setattr(study_imports, "_authenticate_fixture_authority", original_authority)

    with pytest.raises(StudyAuthorityError, match="fixture digest|fixture authority"):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=malformed,
            fixture_repository=fixture.repository,
            request=fixture_request,
            call=preflight.call,
        )

    runner = FixtureRoleRunnerV5(
        responses={"investigator": (store.read(malformed.translated_ref).decode("utf-8"),)},
        campaign_fixture=False,
    )
    artifact = runner.invoke_once(fixture_request)
    attempt = runner.attempts[-1]
    package = RoleInvocationPackageV5(
        preflight.call,
        fixture_request,
        attempt,
        FixtureRoleTerminalAuthorityV5(
            "study-import-v1",
            preflight.call.sha256,
            fixture_request.sha256,
            attempt.sha256,
            attempt.artifact_sha256,
        ),
        artifact,
    )
    with pytest.raises(StudyAuthorityError, match="fixture digest|fixture authority"):
        verify_imported_package_v1(
            package=package,
            record=malformed,
            store=store,
            ledger=ledger,
            fixture_repository=fixture.repository,
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("arm", "withheld"),
        ("mode", "offline_fixture"),
        ("manifest_sha256", "a" * 64),
        ("grant_sha256", "b" * 64),
        ("ledger_root_identity_sha256", "c" * 64),
        ("audit_domain", "other-audit"),
        ("response_schema_sha256", "d" * 64),
        ("original_response_sha256", "e" * 64),
        ("translated_sha256", "f" * 64),
        ("ordinary_artifact_sha256", "0" * 64),
    ),
)
def test_import_record_tampering_is_rejected_without_retranslation(tmp_path: Path, field: str, value: object) -> None:
    _fixture, _fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    tampered = __import__("dataclasses").replace(imported, **{field: value})
    with pytest.raises((StudyAuthorityError, ValueError)):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=tampered,
            fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
            request=_fixture_request,
            call=preflight.call,
        )


def test_import_binds_the_discovered_fixture_manifest_edge(tmp_path: Path) -> None:
    _fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    tampered = replace(
        imported,
        fixture_manifest_ref=ArtifactRefV5(
            "evaluator/foreign-manifest.json",
            imported.fixture_manifest_ref.sha256,
        ),
    )
    with pytest.raises(StudyAuthorityError, match="persisted authority|fixture campaign manifest"):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=tampered,
            fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
            request=fixture_request,
            call=preflight.call,
        )


def test_import_rejects_a_store_with_a_different_ledger_root(tmp_path: Path) -> None:
    _fixture, _fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    copied_root = tmp_path / "copied-study-ledger"
    copied_root.mkdir()
    copied_store = __import__("core.pit_optimizer_v5.two_round_study.store", fromlist=["StudyStoreV1"]).StudyStoreV1(
        LocalArtifactRepositoryV5(copied_root)
    )
    with pytest.raises(StudyAuthorityError):
        create_study_import_v1(store=copied_store, ledger=ledger, preflight=preflight, terminal=terminal)


@pytest.mark.parametrize("target", ("live_request_ref", "original_response_ref", "terminal_ref", "translated_ref"))
def test_persisted_authority_chain_tampering_is_rejected(target: str, tmp_path: Path, monkeypatch) -> None:
    _fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    reference = getattr(imported, target)
    original_read = store.read

    def tampered_read(candidate):
        raw = original_read(candidate)
        return raw + b" " if candidate == reference else raw

    monkeypatch.setattr(store, "read", tampered_read)
    with pytest.raises(StudyAuthorityError):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=imported,
            fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
            request=fixture_request,
            call=preflight.call,
        )


def test_persisted_terminal_receipt_tampering_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    original_read = store.read
    for field, value in (
        ("authorization_sha256", "a" * 64),
        ("terminal_sequence", 2),
        ("cumulative_external_attempts", 9),
    ):
        raw = json.loads(original_read(imported.terminal_ref).decode("utf-8"))
        raw["receipt"][field] = value
        tampered_bytes = canonical_json_bytes_v5(raw)

        def tampered_read(candidate, *, _tampered=tampered_bytes):
            return _tampered if candidate == imported.terminal_ref else original_read(candidate)

        monkeypatch.setattr(store, "read", tampered_read)
        with pytest.raises(StudyAuthorityError):
            authenticate_study_import_v1(
                store=store,
                ledger=ledger,
                record=imported,
                fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
                request=fixture_request,
                call=preflight.call,
            )
        monkeypatch.setattr(store, "read", original_read)


def test_missing_persisted_terminal_bytes_are_rejected(tmp_path: Path, monkeypatch) -> None:
    _fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    original_read = store.read

    def missing_read(candidate):
        if candidate == imported.terminal_ref:
            raise StudyAuthorityError("persisted terminal bytes are missing")
        return original_read(candidate)

    monkeypatch.setattr(store, "read", missing_read)
    with pytest.raises(StudyAuthorityError, match="missing|terminal"):
        authenticate_study_import_v1(
            store=store,
            ledger=ledger,
            record=imported,
            fixture_repository=LocalArtifactRepositoryV5(Path(preflight.fixture_root_locator)),
            request=fixture_request,
            call=preflight.call,
        )


def test_stock_fixture_verifier_rejects_imported_t_as_non_canned(tmp_path: Path) -> None:
    fixture, fixture_request, preflight, _manifest, _live_request, store, ledger, terminal = _live_import_context(tmp_path)
    imported = create_study_import_v1(store=store, ledger=ledger, preflight=preflight, terminal=terminal)
    runner = FixtureRoleRunnerV5(responses={"investigator": (store.read(imported.translated_ref).decode("utf-8"),)})
    artifact = runner.invoke_once(fixture_request)
    attempt = runner.attempts[-1]
    call = RoleCallKeyV5(
        campaign_id=fixture.manifest.manifest.campaign_id,
        round_index=2,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=fixture_request.sha256,
    )
    package = RoleInvocationPackageV5(
        call,
        fixture_request,
        attempt,
        FixtureRoleTerminalAuthorityV5("study-import-v1", call.sha256, fixture_request.sha256, attempt.sha256, attempt.artifact_sha256),
        artifact,
    )
    request_ref = fixture.repository.append_role_request(call=call, request=fixture_request).reference
    persisted = fixture.repository.persist_role_invocation(call=call, request_ref=request_ref, package=package)
    payload_ref = fixture.repository.append_round_payload(persisted.payload)
    event = RoundEventV5(
        campaign_id=call.campaign_id,
        round_index=call.round_index,
        sequence=0,
        prior_event_sha256=None,
        event_kind="role_completion",
        experiment_id=None,
        payload_ref=payload_ref,
    )
    fixture.repository.append_round_event(event)
    with pytest.raises(ValueError, match="deterministic responses"):
        verify_fixture_run_v5(repository=fixture.repository, manifest=fixture.manifest.manifest)
