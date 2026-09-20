"""Focused tests for the independent live-study call admission boundary."""

from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path
import time

import pytest

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import RoleEvidenceItemV5, RoleEvidenceV5
from core.pit_optimizer_v5.provider import (
    InvestigatorRoleInputV5,
    RoleBindingV5,
    RoleCallKeyV5,
    build_role_request_v5,
    role_schema_authority_from_manifest_v5,
)
from core.pit_optimizer_v5.two_round_study.fixtures import create_study_fixture_v1
from core.pit_optimizer_v5.two_round_study.contracts import (
    StudyAdmissionError,
    StudyAuthorityError,
    StudyManifestV1,
    StudyOfflineSettingsV1,
    StudyProviderSettingsV1,
)
from core.pit_optimizer_v5.two_round_study.fixtures import study_resource_budget_v1
from core.pit_optimizer_v5.search import CandidateArchiveReducerV5
from core.pit_optimizer_v5.two_round_study.live_calls import (
    FixturePreflightV1,
    StudyExecutionApprovalV1,
    StudyGrantV1,
    authorize_study_execution_v1,
    authorize_offline_fixture_v1,
    build_study_call_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1
from core.pit_optimizer_v5.two_round_study.transport import StudyOpenRouterGatewayV1


def _fixture_request(fixture) -> object:
    return build_role_request_v5(
        role="investigator",
        role_input=InvestigatorRoleInputV5(("v5.fixture.evidence",), (), ()),
        issued_evidence=RoleEvidenceV5(
            5,
            (RoleEvidenceItemV5("v5.fixture.evidence", "evaluator.exit_attribution_count", 1),),
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


def _preflight(
    tmp_path: Path,
    fixture,
    request,
    *,
    arm: str = "primary",
    mode: str = "offline_fixture",
) -> FixturePreflightV1:
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
    checkpoint_ref = ArtifactRefV5(
        "checkpoint.json",
        hashlib.sha256(__import__("core.pit_optimizer_v5.contracts", fromlist=["canonical_json_bytes_v5"]).canonical_json_bytes_v5(checkpoint.to_primitive())).hexdigest(),
    )
    snapshot_ref = ArtifactRefV5("archive.json", checkpoint.archive_sha256)
    checkpoint_bytes = repository.authenticate_exact(checkpoint_ref).content
    snapshot_bytes = repository.authenticate_exact(snapshot_ref).content
    request_ref = repository.append_role_request(
        call=RoleCallKeyV5(
            campaign_id=f"study-campaign-{arm}",
            round_index=2,
            role="investigator",
            role_position=1,
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=request.sha256,
        ),
        request=request,
    ).reference
    request_bytes = repository.authenticate_exact(request_ref).content
    return FixturePreflightV1(
        arm=arm,
        mode=mode,
        fixture_root_identity_sha256=repository.root_identity_sha256,
        checkpoint_ref=checkpoint_ref,
        snapshot_ref=snapshot_ref,
        call=RoleCallKeyV5(
            campaign_id=f"study-campaign-{arm}",
            round_index=2,
            role="investigator",
            role_position=1,
            attempt_kind="primary",
            attempt_index=1,
            request_sha256=request.sha256,
        ),
        request_ref=request_ref,
        request_bytes=request_bytes,
        schema_json=request.schema_authority.canonical_schema_json,
        evidence_refs=(),
        request_sha256=request.sha256,
        fixture_root_locator=str(repository.root),
        checkpoint_bytes=checkpoint_bytes,
        snapshot_bytes=snapshot_bytes,
        evidence_bytes=(),
    )


def _study_context(
    tmp_path: Path,
    *,
    mode: str = "offline_fixture",
    cumulative_token_ceiling: int = 1_000_000,
    one_attempt_shared_cap: bool = False,
):
    primary_fixture = create_study_fixture_v1(root=tmp_path / "primary-fixture")
    withheld_fixture = create_study_fixture_v1(root=tmp_path / "withheld-fixture")
    request = _fixture_request(primary_fixture)
    withheld_request = _fixture_request(withheld_fixture)
    preflight = _preflight(tmp_path, primary_fixture, request, arm="primary", mode=mode)
    withheld_preflight = _preflight(
        tmp_path,
        withheld_fixture,
        withheld_request,
        arm="withheld",
        mode=mode,
    )
    shared_root = tmp_path / "study-ledger"
    shared_root.mkdir()
    shared_store = StudyStoreV1(LocalArtifactRepositoryV5(shared_root))
    preflight_ref = shared_store.put(kind="preflights", key="primary", content=preflight.canonical_bytes())
    withheld_preflight_ref = shared_store.put(
        kind="preflights", key="withheld", content=withheld_preflight.canonical_bytes()
    )
    rubric_bytes = b"synthetic-task3-rubric-v1"
    shared_store.put(kind="rubrics", key="unit", content=rubric_bytes)
    registry = __import__(
        "core.pit_optimizer_v5.two_round_study.registry", fromlist=["build_study_registry_v1"]
    ).build_study_registry_v1()
    schema = __import__(
        "core.pit_optimizer_v5.two_round_study.schema", fromlist=["study_response_schema_v1"]
    ).study_response_schema_v1(
        fixture_request=request,
        configuration_ids=tuple(item.configuration_id for item in registry.configurations),
    )
    from core.pit_optimizer_v5.two_round_study.live_calls import (
        study_parser_authority_bytes_v1,
        study_prompt_bytes_v1,
    )

    provider_settings = (
        None
        if mode == "offline_fixture"
        else StudyProviderSettingsV1("openrouter", "openai/test-model", 128)
    )
    study_manifest = StudyManifestV1(
        schema_version=1,
        study_id="study-ledger",
        mode=mode,
        source_revision="a" * 40,
        fixture_sha256=primary_fixture.manifest.manifest_ref.sha256,
        registry_sha256=registry.sha256,
        rubric_sha256=hashlib.sha256(rubric_bytes).hexdigest(),
        schema_sha256=hashlib.sha256(schema).hexdigest(),
        parser_sha256=hashlib.sha256(study_parser_authority_bytes_v1()).hexdigest(),
        prompt_sha256=hashlib.sha256(study_prompt_bytes_v1(registry=registry)).hexdigest(),
        round_one_checkpoint_ref=preflight.checkpoint_ref,
        round_one_snapshot_ref=preflight.snapshot_ref,
        primary_preflight_ref=preflight_ref,
        withheld_preflight_ref=withheld_preflight_ref,
        resource_limits=study_resource_budget_v1(),
        provider_settings=provider_settings,
        offline_settings=(StudyOfflineSettingsV1("fixture-test") if mode == "offline_fixture" else None),
    )
    live_request = build_study_call_v1(
        preflight=preflight,
        manifest=study_manifest,
        fixture_request=request,
        registry=registry,
    )
    withheld_live_request = build_study_call_v1(
        preflight=withheld_preflight,
        manifest=study_manifest,
        fixture_request=withheld_request,
        registry=registry,
    )
    if one_attempt_shared_cap:
        cumulative_token_ceiling = live_request.input_bound_bytes + live_request.max_output_tokens
    store = shared_store
    grant = StudyGrantV1(
        study_id=study_manifest.study_id,
        manifest_sha256=study_manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain="study-audit",
        mode=mode,
        provider=("offline_fixture" if mode == "offline_fixture" else "openrouter"),
        model=("offline_fixture/study-v1" if mode == "offline_fixture" else "openai/test-model"),
        arm_slots=("primary", "withheld"),
        input_token_ceiling=1_000_000,
        output_token_ceiling=128,
        cumulative_token_ceiling=cumulative_token_ceiling,
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
        manifest=study_manifest,
        grant=grant,
        approval_reference=grant.operator_approval_reference,
    )
    ledger = StudyLedgerV1(store, study_manifest, grant, approval)
    return (
        primary_fixture,
        request,
        preflight,
        study_manifest,
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


def test_build_study_call_keeps_fixture_and_live_identities_distinct(tmp_path: Path) -> None:
    _fixture, request, preflight, _manifest, built, _store, _grant, _approval, _ledger, *_ = _study_context(tmp_path)
    assert preflight.request_sha256 == request.sha256
    assert built.sha256 != request.sha256
    assert built.fixture_request_sha256 == request.sha256


def test_legacy_fixture_request_and_schema_goldens_remain_unchanged(tmp_path: Path) -> None:
    _fixture, request, preflight, _manifest, _built, *_ = _study_context(tmp_path)
    assert hashlib.sha256(preflight.request_bytes).hexdigest() == (
        "5b0aa19f15244e70dad0d2dc4307161f1cef2168d7b34633e4546d650474addc"
    )
    assert hashlib.sha256(request.schema_authority.canonical_schema_json).hexdigest() == (
        "91be86e4ebdc8874e26a9f1f2f91485cab15ad9a13fe407c9ec4e74576a496a0"
    )


def test_grant_decoding_cannot_mint_ephemeral_execution_approval() -> None:
    assert StudyExecutionApprovalV1 is not None
    assert StudyGrantV1 is not None
    assert authorize_study_execution_v1 is not None
    assert build_study_call_v1 is not None
    with pytest.raises(StudyAuthorityError):
        StudyExecutionApprovalV1()
    with pytest.raises(StudyAuthorityError):
        StudyExecutionApprovalV1._issue(
            grant=object(),
            manifest=object(),
            approval_reference="operator:test",
            capability_kind="live",
            _controller_guard=object(),
        )


def test_offline_authorization_entry_cannot_issue_a_live_capability(tmp_path: Path) -> None:
    _fixture, _fixture_request_value, _preflight, manifest, _request, store, grant, _approval, _ledger, *_ = _study_context(
        tmp_path,
        mode="live_study",
    )
    with pytest.raises(StudyAuthorityError):
        authorize_offline_fixture_v1(
            store=store,
            manifest=manifest,
            grant=grant,
            approval_reference=grant.operator_approval_reference,
        )


def test_study_request_rejects_unsupported_settings_and_input_overflow(tmp_path: Path) -> None:
    from dataclasses import replace

    _fixture, _fixture_request_value, _preflight, _manifest, request, *_ = _study_context(tmp_path)
    with pytest.raises(StudyAdmissionError):
        replace(request, temperature=Decimal("0.2"))
    with pytest.raises(StudyAdmissionError):
        replace(request, seed=7)
    with pytest.raises(StudyAdmissionError):
        replace(request, projected_wire_messages=b"x" * (768 * 1024))


def test_fixture_preflight_rejects_a_forged_non_round_two_call(tmp_path: Path) -> None:
    fixture = create_study_fixture_v1(root=tmp_path / "fixture")
    request = _fixture_request(fixture)
    role_call = RoleCallKeyV5(
        campaign_id="study-campaign",
        round_index=1,
        role="investigator",
        role_position=1,
        attempt_kind="primary",
        attempt_index=1,
        request_sha256=request.sha256,
    )
    request_ref = fixture.repository.append_role_request(call=role_call, request=request).reference
    from core.pit_optimizer_v5.two_round_study.contracts import StudyContractError

    with pytest.raises(StudyContractError):
        FixturePreflightV1(
            arm="primary",
            mode="offline_fixture",
            fixture_root_identity_sha256=fixture.repository.root_identity_sha256,
            checkpoint_ref=ArtifactRefV5("checkpoint.json", "a" * 64),
            snapshot_ref=ArtifactRefV5("archive.json", "b" * 64),
            call=role_call,
            request_ref=request_ref,
            request_bytes=fixture.repository.authenticate_exact(request_ref).content,
            schema_json=request.schema_authority.canonical_schema_json,
            evidence_refs=(),
            request_sha256=request.sha256,
            fixture_root_locator=str(fixture.repository.root),
            checkpoint_bytes=b"{}",
            snapshot_bytes=b"{}",
            evidence_bytes=(),
        )


def test_live_transport_claims_one_dispatch_for_an_admitted_request(tmp_path: Path, monkeypatch) -> None:
    import agent_loop
    from core.pit_optimizer_v5.provider import CompletionResultV5

    _fixture, _fixture_request_value, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = _study_context(
        tmp_path, mode="live_study"
    )
    ledger.reserve(request)
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-study-credential")

    class CountingLowLevelGateway:
        calls = 0
        init_kwargs = None
        request_kwargs = None

        def __init__(self, **kwargs):
            type(self).init_kwargs = kwargs

        def request_pit_optimizer_v5_json_once(self, **kwargs):
            type(self).calls += 1
            type(self).request_kwargs = kwargs
            return CompletionResultV5(
                response_text="{}",
                accepted=True,
                input_tokens=1,
                output_tokens=1,
                provider_request_id="fake-transport",
                returned_model=request.model,
                cost_usd=Decimal("0"),
                external_attempt_count=1,
                response_received=True,
            )

    monkeypatch.setattr(agent_loop, "OpenRouterGateway", CountingLowLevelGateway)
    gateway = StudyOpenRouterGatewayV1(ledger=ledger)
    kwargs = {
        "request_sha256": request.sha256,
        "model": request.model,
        "messages": request.messages,
        "response_schema_json": request.schema_json,
        "max_output_tokens": request.max_output_tokens,
        "automatic_retries": 0,
        "schema_repair_calls": 0,
        "deadline_monotonic": time.monotonic() + 30.0,
    }
    changed_messages = kwargs["messages"][:-1] + (
        {"role": "user", "content": {"study_protocol": "changed-after-admission"}},
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(StudyAuthorityError):
        gateway.invoke_json_once(**{**kwargs, "messages": changed_messages})
    assert CountingLowLevelGateway.calls == 0
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-study-credential")
    with pytest.raises(StudyAuthorityError):
        gateway.invoke_json_once(**{**kwargs, "deadline_monotonic": time.monotonic() - 1.0})
    gateway.invoke_json_once(**kwargs)
    with pytest.raises(StudyAuthorityError):
        gateway.invoke_json_once(**kwargs)
    assert CountingLowLevelGateway.calls == 1
    assert CountingLowLevelGateway.init_kwargs["max_attempts"] == 1
    assert CountingLowLevelGateway.request_kwargs["allow_full_source_escape"] is False
    assert CountingLowLevelGateway.request_kwargs["request_sha256"] == request.sha256
    assert CountingLowLevelGateway.request_kwargs["model"] == request.model
    assert CountingLowLevelGateway.request_kwargs["messages"] == request.messages
    assert CountingLowLevelGateway.request_kwargs["response_schema_json"] == request.schema_json
    assert CountingLowLevelGateway.request_kwargs["max_output_tokens"] == request.max_output_tokens
    assert CountingLowLevelGateway.request_kwargs["wall_deadline"] == kwargs["deadline_monotonic"]
    assert gateway.transport_settings_sha256 == __import__(
        "core.pit_optimizer_v5.two_round_study.live_calls", fromlist=["study_transport_settings_sha256_v1"]
    ).study_transport_settings_sha256_v1()


def test_reader_cannot_claim_a_pending_live_dispatch_or_lookup_credentials(tmp_path: Path, monkeypatch) -> None:
    _fixture, _fixture_request_value, _preflight, manifest, request, store, grant, _approval, ledger, *_ = _study_context(
        tmp_path, mode="live_study"
    )
    ledger.reserve(request)
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    gateway = StudyOpenRouterGatewayV1(ledger=reader)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(StudyAuthorityError):
        gateway.invoke_json_once(
            request_sha256=request.sha256,
            model=request.model,
            messages=request.messages,
            response_schema_json=request.schema_json,
            max_output_tokens=request.max_output_tokens,
            automatic_retries=0,
            schema_repair_calls=0,
            deadline_monotonic=time.monotonic() + 30.0,
        )
    assert store.list_refs(kind="dispatches") == ()


def test_transport_construction_and_terminal_recovery_do_not_read_credentials(tmp_path: Path, monkeypatch) -> None:
    import core.pit_optimizer_v5.two_round_study.transport as transport_module
    from core.pit_optimizer_v5.two_round_study.ledger import run_study_call_v1
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for

    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = _study_context(tmp_path)
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                request.sha256: _completion(
                    request=request,
                    response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=1.0,
    )
    reader = StudyLedgerV1(store, manifest, grant, approval=None)

    def fail_credential_lookup(*_args, **_kwargs):
        raise AssertionError("credential lookup occurred during construction/recovery")

    monkeypatch.setattr(transport_module.os.environ, "get", fail_credential_lookup)
    StudyOpenRouterGatewayV1(ledger=reader)
    assert reader.recover(request) == terminal


@pytest.mark.parametrize("deadline_kind", ("expired", "outside_window"))
def test_invalid_live_deadline_is_rejected_before_reservation_or_credential_lookup(
    tmp_path: Path, monkeypatch, deadline_kind: str
) -> None:
    import core.pit_optimizer_v5.two_round_study.transport as transport_module
    from core.pit_optimizer_v5.two_round_study.ledger import run_study_call_v1

    context = _study_context(tmp_path, mode="live_study")
    _fixture, fixture_request, _preflight, _manifest, request, store, grant, _approval, ledger, *_ = context
    gateway = StudyOpenRouterGatewayV1(ledger=ledger)
    credential_reads = 0

    def fail_credential_lookup(*_args, **_kwargs):
        nonlocal credential_reads
        credential_reads += 1
        raise AssertionError("credential lookup occurred before deterministic deadline rejection")

    monkeypatch.setattr(transport_module.os.environ, "get", fail_credential_lookup)
    if deadline_kind == "expired":
        deadline = time.monotonic() - 1.0
    else:
        deadline = time.monotonic() + float(grant.per_call_deadline_seconds) + 1.0
    with pytest.raises((StudyAuthorityError, StudyAdmissionError)):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=deadline,
        )
    assert store.list_refs(kind="reservations") == ()
    assert store.list_refs(kind="dispatches") == ()
    assert credential_reads == 0
    withheld_request = context[-1]
    ledger.reserve(withheld_request)


def test_deadline_expiring_after_reservation_is_a_typed_no_provider_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    import types

    import core.pit_optimizer_v5.two_round_study.transport as transport_module
    from core.pit_optimizer_v5.two_round_study.ledger import run_study_call_v1

    context = _study_context(tmp_path, mode="live_study")
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_rest = context
    withheld_request = context[-1]
    gateway = StudyOpenRouterGatewayV1(ledger=ledger)
    deadline = time.monotonic() + 30.0

    # The caller's pre-reservation check sees a valid window.  The concrete
    # adapter observes expiry after the reservation but before claim/credential
    # lookup, modeling the deterministic race without a provider call.
    monkeypatch.setattr(
        transport_module,
        "time",
        types.SimpleNamespace(monotonic=lambda: deadline + 1.0),
    )
    with pytest.raises(StudyAuthorityError, match="deadline"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=deadline,
        )
    assert store.list_refs(kind="dispatches") == ()
    assert len(store.list_refs(kind="admission-rejections")) == 1
    reservation = ledger.reserve(withheld_request)
    assert reservation.arm == "withheld"
