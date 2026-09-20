"""Focused tests for append-only two-arm study accounting."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.pit_optimizer_v5.artifacts import ArtifactRefV5, LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import InvestigatorArtifactV5, canonical_json_bytes_v5
from core.pit_optimizer_v5.provider import CompletionResultV5, RoleTerminalReceiptV5, wire_role_messages_v5
from core.pit_optimizer_v5.two_round_study.contracts import (
    StudyAdmissionError,
    StudyAuthorityError,
    StudyPendingAccounting,
    StudyResponseV1,
)
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
from core.pit_optimizer_v5.provider import RoleFailureCode


def _context(tmp_path: Path, *, mode: str = "offline_fixture"):
    # Keep the authority setup in one focused live-call fixture.  The ledger
    # tests exercise the accounting boundary after admission, rather than
    # manufacturing an unbound request record here.
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    return _study_context(tmp_path, mode=mode)[:9]


def _advance_fixture_projection(fixture, *, generation: int = 2) -> None:
    from core.pit_optimizer_v5.search import CandidateArchiveReducerV5

    reducer = CandidateArchiveReducerV5(
        discovery_plan=fixture.manifest.panel_plan,
        evaluator_contract=fixture.manifest.evaluator_contract,
        target=fixture.manifest.manifest.target,
        authorities=(),
        capacity=fixture.manifest.manifest.search.archive_capacity,
        pit_data_scope="production",
        semantic_mode="required",
    )
    fixture.repository.publish_projection(record_refs=(), reducer=reducer, generation=generation)


def test_fresh_reservation_rejects_a_stale_checkpoint_before_mutation(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, _fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    _advance_fixture_projection(fixture)
    with pytest.raises(StudyAuthorityError, match="current projection"):
        ledger.reserve(request)
    assert store.list_refs(kind="reservations") == ()
    assert store.list_refs(kind="dispatches") == ()


def test_dispatch_claim_rejects_projection_advance_after_reservation(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, _fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    ledger.reserve(request)
    _advance_fixture_projection(fixture)
    with pytest.raises(StudyAuthorityError, match="current projection"):
        ledger.claim_dispatch(
            request_sha256=request.sha256,
            model=request.model,
            messages=request.messages,
            response_schema_json=request.schema_json,
            max_output_tokens=request.max_output_tokens,
        )
    assert store.list_refs(kind="dispatches") == ()


def test_runner_claim_rejection_after_reservation_is_a_bounded_no_provider_event(tmp_path, monkeypatch) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_rest = context
    withheld_call = context[-1]
    original_claim = ledger.claim_dispatch

    def advance_then_claim(**kwargs):
        _advance_fixture_projection(fixture)
        return original_claim(**kwargs)

    monkeypatch.setattr(ledger, "claim_dispatch", advance_then_claim)
    fake = _CountingFake({})
    with pytest.raises(StudyAuthorityError, match="current projection"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 0
    assert len(store.list_refs(kind="reservations")) == 1
    assert len(store.list_refs(kind="admission-rejections")) == 1
    assert store.list_refs(kind="dispatches") == ()
    assert store.list_refs(kind="responses") == ()
    assert store.list_refs(kind="raw-responses") == ()
    assert store.list_refs(kind="terminals") == ()
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyAdmissionError, match="pre-dispatch rejection"):
        reader.recover(request)
    withheld_reservation = ledger.reserve(withheld_call)
    assert withheld_reservation.arm == "withheld"


def test_response_and_raw_prefix_without_dispatch_is_not_recoverable(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    ledger.reserve(request)
    completion = _completion(
        request=request,
        response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
    )
    ledger._persist_response(request, completion)
    ledger._persist_raw_response(request, completion)
    with pytest.raises(StudyAuthorityError, match="dispatch claim"):
        ledger.recover(request)
    with pytest.raises(StudyAuthorityError, match="dispatch claim"):
        StudyLedgerV1(store, manifest, grant, approval=None)


def test_fresh_reader_keeps_a_reservation_only_prefix_pending(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_rest = context
    withheld_call = context[-1]
    reservation = ledger.reserve(request)
    before = {
        kind: store.list_refs(kind=kind)
        for kind in ("reservations", "dispatches", "responses", "raw-responses", "parsed", "terminals")
    }
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reader.recover(request)
    fake = _CountingFake({request.sha256: RuntimeError("must not replay a reservation-only prefix")})
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        run_study_call_v1(
            request=request,
            fixture_request=_fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    with pytest.raises(StudyPendingAccounting, match="unresolved study arm reservation"):
        ledger.reserve(withheld_call)
    assert fake.calls == 0
    assert reservation.request_sha256 == request.sha256
    assert {
        kind: store.list_refs(kind=kind)
        for kind in ("reservations", "dispatches", "responses", "raw-responses", "parsed", "terminals")
    } == before


def test_reservation_plus_parsed_prefix_fails_with_typed_authority_error(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    store.put(kind="parsed", key=request.sha256, content=_response_for(fixture_request).canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="complete response accounting pair"):
        StudyLedgerV1(store, manifest, grant, approval=None)


def test_marker_plus_parsed_prefix_fails_closed(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, approval, ledger, *_ = context
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(request=request, response_text="x" * (4 * 1024 * 1024 + 1), output_tokens=7)
    ledger.persist_response_for_request(request, completion)
    store.put(kind="parsed", key=request.sha256, content=_response_for(fixture_request).canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="non-importable|marker"):
        StudyLedgerV1(store, manifest, grant, approval=approval)


def test_valid_parsed_prefix_remains_recoverable_after_reader_reopen(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(
        request=request,
        response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
    )
    ledger.persist_response_for_request(request, completion)
    raw_ref = ledger._raw_response_reference(request)
    assert raw_ref is not None
    parsed_ref, parse_code, parse_reason = ledger._parse_and_persist(request, completion, raw_ref)
    assert parsed_ref is not None
    assert parse_code is None
    assert parse_reason is None
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    terminal = reader.recover(request)
    assert terminal is not None
    assert terminal.terminal.parsed_ref == parsed_ref


def test_reservation_journal_rejects_a_missing_sequence(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    from dataclasses import replace as dc_replace
    import hashlib

    payload = reservation.authenticated_payload()
    payload["request_sha256"] = "f" * 64
    payload["sequence"] = 3
    forged = dc_replace(
        reservation,
        request_sha256="f" * 64,
        sequence=3,
        reservation_sha256=hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest(),
    )
    store.put(kind="reservations", key=forged.reservation_sha256, content=forged.canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="sequence has a gap"):
        ledger._reservations()


def test_reservation_journal_rejects_a_forked_arm_slot(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    from dataclasses import replace as dc_replace
    import hashlib

    payload = reservation.authenticated_payload()
    payload["request_sha256"] = "e" * 64
    payload["sequence"] = 2
    forged = dc_replace(
        reservation,
        request_sha256="e" * 64,
        sequence=2,
        reservation_sha256=hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest(),
    )
    store.put(kind="reservations", key=forged.reservation_sha256, content=forged.canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="duplicate arm slot"):
        ledger._reservations()


@pytest.mark.parametrize("case", ("arm", "foreign_study"))
def test_reservation_graph_rejects_request_cross_binding(tmp_path, case) -> None:
    import hashlib

    from core.pit_optimizer_v5.two_round_study.ledger import StudyReservationV1

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, manifest, _request, store, grant, approval, _ledger, *_rest = context
    withheld_call = context[-1]
    request = withheld_call if case == "arm" else replace(withheld_call, study_id="foreign-study")
    arm = "primary" if case == "arm" else "withheld"
    owner = "forged-owner"
    prospective_input = request.input_bound_bytes
    prospective_output = request.max_output_tokens
    payload = {
        "study_id": grant.study_id,
        "arm": arm,
        "request_sha256": request.sha256,
        "grant_sha256": grant.sha256,
        "manifest_sha256": manifest.sha256,
        "repository_root_identity_sha256": store.repository.root_identity_sha256,
        "audit_domain": grant.audit_domain,
        "slot_id": f"{grant.study_id}:{arm}",
        "invocation_owner": owner,
        "prospective_input_tokens": prospective_input,
        "prospective_output_tokens": prospective_output,
        "prospective_cost_usd": "0",
        "sequence": 1,
    }
    reservation_sha256 = hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest()
    forged = StudyReservationV1(
        study_id=grant.study_id,
        arm=arm,
        request_sha256=request.sha256,
        grant_sha256=grant.sha256,
        manifest_sha256=manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain=grant.audit_domain,
        slot_id=f"{grant.study_id}:{arm}",
        invocation_owner=owner,
        prospective_input_tokens=prospective_input,
        prospective_output_tokens=prospective_output,
        prospective_cost_usd=Decimal("0"),
        sequence=1,
        reservation_sha256=reservation_sha256,
    )
    store.put(kind="requests", key=request.sha256, content=request.canonical_bytes())
    store.put(kind="reservations", key=forged.reservation_sha256, content=forged.canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="reservation|request|study|arm"):
        StudyLedgerV1(store, manifest, grant, approval=approval)
    with pytest.raises(StudyAuthorityError, match="reservation|request|study|arm"):
        StudyLedgerV1(store, manifest, grant, approval=None)


def test_reservation_graph_rejects_same_arm_request_with_opposite_preflight(tmp_path) -> None:
    import hashlib

    from core.pit_optimizer_v5.two_round_study.ledger import StudyReservationV1

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, primary_fixture_request, _preflight, manifest, primary_request, store, grant, approval, _ledger, *_rest = context
    malformed_request = replace(primary_request, preflight_ref=manifest.withheld_preflight_ref)
    payload = {
        "study_id": grant.study_id,
        "arm": "primary",
        "request_sha256": malformed_request.sha256,
        "grant_sha256": grant.sha256,
        "manifest_sha256": manifest.sha256,
        "repository_root_identity_sha256": store.repository.root_identity_sha256,
        "audit_domain": grant.audit_domain,
        "slot_id": f"{grant.study_id}:primary",
        "invocation_owner": "forged-owner",
        "prospective_input_tokens": malformed_request.input_bound_bytes,
        "prospective_output_tokens": malformed_request.max_output_tokens,
        "prospective_cost_usd": "0",
        "sequence": 1,
    }
    reservation = StudyReservationV1(
        study_id=grant.study_id,
        arm="primary",
        request_sha256=malformed_request.sha256,
        grant_sha256=grant.sha256,
        manifest_sha256=manifest.sha256,
        repository_root_identity_sha256=store.repository.root_identity_sha256,
        audit_domain=grant.audit_domain,
        slot_id=f"{grant.study_id}:primary",
        invocation_owner="forged-owner",
        prospective_input_tokens=malformed_request.input_bound_bytes,
        prospective_output_tokens=malformed_request.max_output_tokens,
        prospective_cost_usd=Decimal("0"),
        sequence=1,
        reservation_sha256=hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest(),
    )
    store.put(kind="requests", key=malformed_request.sha256, content=malformed_request.canonical_bytes())
    store.put(kind="reservations", key=reservation.reservation_sha256, content=reservation.canonical_bytes())
    before = {
        kind: store.list_refs(kind=kind)
        for kind in ("requests", "reservations", "dispatches", "responses", "raw-responses", "parsed", "terminals")
    }

    with pytest.raises(StudyAuthorityError, match="preflight"):
        StudyLedgerV1(store, manifest, grant, approval=approval)
    with pytest.raises(StudyAuthorityError, match="preflight"):
        StudyLedgerV1(store, manifest, grant, approval=None)
    assert {
        kind: store.list_refs(kind=kind)
        for kind in ("requests", "reservations", "dispatches", "responses", "raw-responses", "parsed", "terminals")
    } == before
    assert primary_request.sha256 != malformed_request.sha256
    assert primary_fixture_request.sha256 == malformed_request.fixture_request_sha256


def test_untyped_usage_from_a_counting_fake_stays_pending_at_caller_boundary(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context

    class MissingUsageFake:
        calls = 0

        def invoke_json_once(self, **_kwargs):
            type(self).calls += 1
            return object()

    fake = MissingUsageFake()
    with pytest.raises(StudyPendingAccounting, match="untyped completion"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 1
    assert store.list_refs(kind="terminals") == ()
    assert len(store.list_refs(kind="dispatches")) == 1


def test_negative_usage_from_a_counting_fake_stays_pending_at_caller_boundary(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_rest = context
    withheld_call = context[-1]

    class NegativeUsageFake:
        calls = 0

        def invoke_json_once(self, **_kwargs):
            type(self).calls += 1
            CompletionResultV5(
                response_text="",
                accepted=True,
                input_tokens=-1,
                output_tokens=0,
                provider_request_id="fake-negative-usage",
                returned_model=request.model,
                cost_usd=Decimal("0"),
                external_attempt_count=1,
                response_received=True,
            )

    fake = NegativeUsageFake()
    with pytest.raises(StudyPendingAccounting, match="unresolved accounting"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        ledger.recover(request)
    with pytest.raises(StudyPendingAccounting, match="unresolved study arm reservation"):
        ledger.reserve(withheld_call)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 1
    assert len(store.list_refs(kind="reservations")) == 1
    assert len(store.list_refs(kind="dispatches")) == 1
    assert store.list_refs(kind="responses") == ()
    assert store.list_refs(kind="raw-responses") == ()
    assert store.list_refs(kind="terminals") == ()


def test_concurrent_dispatch_owners_have_one_durable_winner(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    ledger.reserve(request)

    def claim_once() -> str:
        try:
            ledger.claim_dispatch(
                request_sha256=request.sha256,
                model=request.model,
                messages=request.messages,
                response_schema_json=request.schema_json,
                max_output_tokens=request.max_output_tokens,
            )
        except StudyAuthorityError:
            return "rejected"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: claim_once(), (1, 2)))
    assert sorted(outcomes) == ["claimed", "rejected"]
    assert len(store.list_refs(kind="dispatches")) == 1


def test_reopening_a_ledger_in_reader_mode_does_not_publish_authority(tmp_path, monkeypatch) -> None:
    _fixture, _request, _preflight, manifest, _call, store, grant, _approval, _ledger = _context(tmp_path)

    def reject_put(*_args, **_kwargs):
        pytest.fail("reader-mode ledger construction mutated the authority store")

    monkeypatch.setattr(type(store), "put", reject_put)
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reopened.study_id == grant.study_id


def test_reader_verification_does_not_create_a_lock_for_a_missing_authority(tmp_path) -> None:
    _fixture, _request, _preflight, _manifest, _call, _store, _grant, _approval, ledger = _context(tmp_path)
    root = ledger.store.repository.root
    before = tuple(sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()))
    missing = ArtifactRefV5(
        "adapter-blobs/study-v1-terminals/" + "0" * 64 + ".bin",
        "0" * 64,
    )
    with pytest.raises(StudyAuthorityError):
        ledger.verify_terminal(missing)
    after = tuple(sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()))
    assert after == before


def test_reader_reopen_rejects_missing_authority_without_creating_records(tmp_path) -> None:
    _fixture, _request, _preflight, manifest, _call, _store, grant, _approval, _ledger = _context(tmp_path)
    empty_root = tmp_path / "reader-empty"
    empty_root.mkdir()
    empty_store = __import__(
        "core.pit_optimizer_v5.two_round_study.store",
        fromlist=["StudyStoreV1"],
    ).StudyStoreV1(LocalArtifactRepositoryV5(empty_root))
    empty_grant = replace(
        grant,
        repository_root_identity_sha256=empty_store.repository.root_identity_sha256,
    )
    root = empty_store.repository.root
    before = tuple(sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()))
    with pytest.raises(StudyAuthorityError):
        StudyLedgerV1(empty_store, manifest, empty_grant, approval=None)
    after = tuple(sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()))
    assert after == before


def test_constructor_rejects_orphan_accounting_blob_before_admission(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, approval, _ledger, *_ = context
    orphan = _response_for(fixture_request).canonical_bytes()
    store.put(kind="parsed", key=request.sha256, content=orphan)
    with pytest.raises(StudyAuthorityError):
        StudyLedgerV1(store, manifest, grant, approval)


def test_reader_rejects_extra_authority_record_before_admission(tmp_path) -> None:
    _fixture, _fixture_request, _preflight, manifest, _request, store, grant, _approval, _ledger = _context(tmp_path)
    store.put(kind="grants", key="foreign-study", content=grant.canonical_bytes())
    with pytest.raises(StudyAuthorityError, match="authority is ambiguous"):
        StudyLedgerV1(store, manifest, grant, approval=None)


def test_copied_study_root_cannot_reuse_the_original_grant_authority(tmp_path) -> None:
    _fixture, _fixture_request, _preflight, manifest, _request, store, grant, _approval, _ledger = _context(tmp_path)
    copied_root = tmp_path / "copied-study-root"
    copied_root.mkdir()
    copied_store = __import__(
        "core.pit_optimizer_v5.two_round_study.store",
        fromlist=["StudyStoreV1"],
    ).StudyStoreV1(LocalArtifactRepositoryV5(copied_root))
    with pytest.raises(StudyAuthorityError, match="repository root"):
        StudyLedgerV1(copied_store, manifest, grant, approval=None)


def test_unknown_response_vocabulary_is_not_allowed_by_the_frozen_request(tmp_path) -> None:
    from tests.test_pit_optimizer_v5_study_contracts import _draft, _hypothesis
    from core.pit_optimizer_v5.two_round_study.ledger import run_study_call_v1

    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger = _context(tmp_path)
    forged = replace(_draft(), configuration_id="provider-invented-configuration")
    response = StudyResponseV1(
        1,
        fixture_request.expected_binding,
        InvestigatorArtifactV5((_hypothesis(),)),
        (forged,),
    )

    class OneShotFake:
        def invoke_json_once(self, **_kwargs):
            return CompletionResultV5(
                response_text=response.canonical_bytes().decode("utf-8"),
                accepted=True,
                input_tokens=1,
                output_tokens=1,
                provider_request_id="fake-vocabulary",
                returned_model=request.model,
                cost_usd=Decimal("0"),
                external_attempt_count=1,
                response_received=True,
            )

    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=OneShotFake(),
        deadline_monotonic=1.0,
    )
    assert terminal.terminal.failure_code is RoleFailureCode.RESPONSE_SCHEMA


def test_task_three_ledger_public_surface_is_present() -> None:
    assert StudyLedgerV1 is not None
    result = CompletionResultV5(
        response_text="{}",
        accepted=True,
        input_tokens=1,
        output_tokens=1,
        provider_request_id="fake-1",
        returned_model="openai/test",
        cost_usd=Decimal("0"),
        external_attempt_count=1,
        response_received=True,
    )
    assert result.external_attempt_count == 1
    with pytest.raises(ValueError):
        CompletionResultV5(
            response_text="{}",
            accepted=True,
            input_tokens=-1,
            output_tokens=1,
            provider_request_id="fake-negative",
            returned_model="openai/test",
            cost_usd=Decimal("0"),
            external_attempt_count=1,
            response_received=True,
        )


def _response_for(fixture_request) -> StudyResponseV1:
    from tests.test_pit_optimizer_v5_study_contracts import _draft, _hypothesis

    return StudyResponseV1(
        1,
        fixture_request.expected_binding,
        InvestigatorArtifactV5((_hypothesis(),)),
        (replace(_draft(), configuration_id="S-gte-0.50"),),
    )


class _CountingFake:
    def __init__(self, completions):
        self.completions = dict(completions)
        self.calls = 0
        self.call_arguments = []

    def invoke_json_once(
        self,
        *,
        request_sha256,
        model,
        messages,
        response_schema_json,
        max_output_tokens,
        automatic_retries,
        schema_repair_calls,
        deadline_monotonic,
    ):
        assert automatic_retries == 0
        assert schema_repair_calls == 0
        assert isinstance(messages, tuple)
        assert isinstance(response_schema_json, bytes)
        assert max_output_tokens > 0
        assert isinstance(deadline_monotonic, float)
        self.calls += 1
        self.call_arguments.append(
            {
                "request_sha256": request_sha256,
                "model": model,
                "messages": messages,
                "response_schema_json": response_schema_json,
                "max_output_tokens": max_output_tokens,
                "automatic_retries": automatic_retries,
                "schema_repair_calls": schema_repair_calls,
            }
        )
        value = self.completions[request_sha256]
        if isinstance(value, BaseException):
            raise value
        return value


def _completion(*, request, response_text, returned_model=None, output_tokens=1, accepted=True, response_received=True):
    return CompletionResultV5(
        response_text=response_text,
        accepted=accepted,
        input_tokens=1,
        output_tokens=output_tokens,
        provider_request_id="fake-counted",
        returned_model=request.model if returned_model is None else returned_model,
        cost_usd=Decimal("0"),
        external_attempt_count=1,
        response_received=response_received,
    )


def test_spoofed_gateway_dispatch_marker_cannot_bypass_the_runner_claim(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context

    class SpoofedGateway(_CountingFake):
        _study_transport_owns_dispatch = True

    fake = SpoofedGateway(
        {
            request.sha256: _completion(
                request=request,
                response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
            )
        }
    )
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.terminal.failure_code is None
    assert fake.calls == 1
    assert len(store.list_refs(kind="dispatches")) == 1


def test_settle_rejects_a_forged_parser_failure_for_an_accepted_response(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(
        request=request,
        response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
    )
    ledger.persist_response_for_request(request, completion)
    raw_ref = ledger._raw_response_reference(request)
    assert raw_ref is not None
    parsed_ref, _parse_code, _parse_reason = ledger._parse_and_persist(request, completion, raw_ref)
    assert parsed_ref is not None
    with pytest.raises(StudyAuthorityError, match="parser|failure"):
        ledger.settle(
            reservation,
            completion=completion,
            response_ref=raw_ref,
            parsed_ref=parsed_ref,
            failure_code=RoleFailureCode.EVIDENCE_BINDING,
            failure_reason="forged parser label",
        )


def test_settle_rejects_accounting_without_authenticated_usage_overage(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(
        request=request,
        response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"),
    )
    ledger.persist_response_for_request(request, completion)
    raw_ref = ledger._raw_response_reference(request)
    assert raw_ref is not None
    parsed_ref, parse_code, parse_reason = ledger._parse_and_persist(request, completion, raw_ref)
    assert parsed_ref is not None and parse_code is None and parse_reason is None
    with pytest.raises(StudyAuthorityError, match="accounting|usage"):
        ledger.settle(
            reservation,
            completion=completion,
            response_ref=raw_ref,
            parsed_ref=parsed_ref,
            failure_code=RoleFailureCode.ACCOUNTING,
            failure_reason="forged accounting",
        )


def test_terminal_verification_rejects_a_forged_parser_failure_code_and_reason(tmp_path, monkeypatch) -> None:
    import hashlib
    from dataclasses import replace as dc_replace

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request)
    forged_response = dc_replace(response, binding=dc_replace(response.binding, hypothesis_id="forged"))
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake(
            {
                request.sha256: _completion(
                    request=request,
                    response_text=forged_response.canonical_bytes().decode("utf-8"),
                )
            }
        ),
        deadline_monotonic=1.0,
    )
    assert terminal.terminal.parsed_ref is None
    assert terminal.terminal.failure_code is RoleFailureCode.EVIDENCE_BINDING
    payload = terminal.terminal.authenticated_payload()
    payload["failure_code"] = RoleFailureCode.AUTHORIZATION.value
    payload["failure_reason"] = "forged parser reason"
    forged_sha = hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest()
    forged = dc_replace(
        terminal.terminal,
        failure_code=RoleFailureCode.AUTHORIZATION,
        failure_reason="forged parser reason",
        terminal_sha256=forged_sha,
    )
    forged_ref = ArtifactRefV5(
        f"adapter-blobs/study-v1-terminals/{forged_sha}.bin",
        hashlib.sha256(forged.canonical_bytes()).hexdigest(),
    )
    original_read = type(ledger.store).read
    original_list_refs = type(ledger.store).list_refs

    def read(store, reference):
        if reference == forged_ref:
            return forged.canonical_bytes()
        return original_read(store, reference)

    def list_refs(store, *, kind, maximum_entries=4096):
        if kind == "terminals":
            return (forged_ref,)
        return original_list_refs(store, kind=kind, maximum_entries=maximum_entries)

    monkeypatch.setattr(type(ledger.store), "read", read)
    monkeypatch.setattr(type(ledger.store), "list_refs", list_refs)
    with pytest.raises(StudyAuthorityError, match="parser|terminal"):
        ledger.verify_terminal(forged_ref)


def test_two_arm_accounting_and_terminal_recovery_are_once_only(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, _withheld_fixture, withheld_request, _withheld_preflight, withheld_call = context
    primary_response = _response_for(fixture_request)
    withheld_response = _response_for(withheld_request)
    fake = _CountingFake(
        {
            request.sha256: _completion(request=request, response_text=primary_response.canonical_bytes().decode("utf-8")),
            withheld_call.sha256: _completion(request=withheld_call, response_text=withheld_response.canonical_bytes().decode("utf-8")),
        }
    )
    first = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    repeated = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    second = run_study_call_v1(
        request=withheld_call,
        fixture_request=withheld_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert fake.calls == 2
    assert repeated == first
    assert first.arm == "primary"
    assert second.arm == "withheld"
    assert second.terminal.terminal_sequence == 2
    assert ledger.verify_terminal(second.reference) == second
    assert fake.call_arguments[0] == {
        "request_sha256": request.sha256,
        "model": request.model,
        "messages": request.messages,
        "response_schema_json": request.schema_json,
        "max_output_tokens": request.max_output_tokens,
        "automatic_retries": 0,
        "schema_repair_calls": 0,
    }
    assert fake.call_arguments[1]["request_sha256"] == withheld_call.sha256
    assert fake.call_arguments[1]["messages"] == withheld_call.messages
    assert fake.call_arguments[1]["response_schema_json"] == withheld_call.schema_json


def test_settled_arm_cannot_accept_a_new_request_identity(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, _withheld_fixture, withheld_request, _withheld_preflight, withheld_call = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=response)})
    first = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    with pytest.raises(StudyAdmissionError, match="terminal"):
        ledger.reserve(request)
    assert fake.calls == 1
    assert first.arm == "primary"
    assert withheld_call.arm == "withheld"


def test_offline_request_cannot_change_the_frozen_F_output_bound_before_admission(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    changed = replace(request, max_output_tokens=request.max_output_tokens - 1)
    fake = _CountingFake({changed.sha256: RuntimeError("must not dispatch")})
    with pytest.raises(StudyAuthorityError, match="authenticated F request"):
        run_study_call_v1(
            request=changed,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 0


def test_wrong_fixture_request_is_rejected_before_gateway(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, _withheld_fixture, withheld_request, _withheld_preflight, _withheld_call = context
    wrong_fixture_request = replace(withheld_request, max_output_tokens=withheld_request.max_output_tokens - 1)
    fake = _CountingFake({request.sha256: RuntimeError("must not dispatch")})
    with pytest.raises(StudyAuthorityError):
        run_study_call_v1(
            request=request,
            fixture_request=wrong_fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 0


def test_inserted_f_message_is_rejected_before_gateway(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    inserted = replace(
        request,
        messages=request.messages[:-1]
        + ({"role": "user", "content": {"injected": True}}, request.messages[-1]),
    )
    inserted = replace(inserted, projected_wire_messages=canonical_json_bytes_v5(wire_role_messages_v5(inserted.messages)))
    fake = _CountingFake({inserted.sha256: RuntimeError("must not dispatch")})
    with pytest.raises(StudyAuthorityError):
        run_study_call_v1(
            request=inserted,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 0


def test_recovery_uses_the_strict_parser_after_raw_response_is_durable(tmp_path) -> None:
    from tests.test_pit_optimizer_v5_study_contracts import _hypothesis

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    bad_hypothesis = replace(_hypothesis(), authoring_mode="full_source_escape")
    from core.pit_optimizer_v5.two_round_study.contracts import StudyResponseV1
    bad_response = StudyResponseV1(
        1,
        fixture_request.expected_binding,
        InvestigatorArtifactV5((bad_hypothesis,)),
        (_response_for(fixture_request).drafts[0],),
    )
    reservation = ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(request=request, response_text=bad_response.canonical_bytes().decode("utf-8"))
    ledger.persist_response_for_request(request, completion)
    terminal = ledger.recover(request)
    assert terminal is not None
    assert terminal.terminal.failure_code is RoleFailureCode.AUTHORIZATION
    assert "authoring mode" in (terminal.terminal.failure_reason or "")
    assert reservation.request_sha256 == request.sha256


@pytest.mark.parametrize("rejection", ("count", "authoring_mode", "citations", "legacy_projection"))
def test_recovery_derives_each_dynamic_parser_rejection_class(tmp_path, rejection, monkeypatch) -> None:
    import json

    from tests.test_pit_optimizer_v5_study_contracts import _hypothesis

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request)
    if rejection == "count":
        primitive = response.to_primitive()
        primitive["artifact"]["hypotheses"] = []
        raw_response = json.dumps(primitive).encode("utf-8")
    elif rejection == "authoring_mode":
        raw_response = replace(
            response,
            artifact=InvestigatorArtifactV5((replace(_hypothesis(), authoring_mode="full_source_escape"),)),
        ).canonical_bytes()
    elif rejection == "citations":
        raw_response = replace(
            response,
            drafts=(replace(response.drafts[0], cited_evidence_ids=("v5.not-issued",)),),
        ).canonical_bytes()
    else:
        import core.pit_optimizer_v5.two_round_study.schema as study_schema

        original_projection = study_schema.parse_and_bind_role_artifact

        def altered_projection(*args, **kwargs):
            parsed = original_projection(*args, **kwargs)
            return replace(parsed, hypotheses=())

        monkeypatch.setattr(study_schema, "parse_and_bind_role_artifact", altered_projection)
        raw_response = response.canonical_bytes()
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    ledger.persist_response_for_request(
        request,
        _completion(request=request, response_text=raw_response.decode("utf-8")),
    )
    terminal = ledger.recover(request)
    assert terminal is not None
    expected_code = {
        "count": RoleFailureCode.RESPONSE_SCHEMA,
        "authoring_mode": RoleFailureCode.AUTHORIZATION,
        "citations": RoleFailureCode.EVIDENCE_BINDING,
        "legacy_projection": RoleFailureCode.AUTHORIZATION,
    }[rejection]
    assert terminal.terminal.failure_code is expected_code
    assert terminal.terminal.failure_reason


def test_fresh_reader_recovers_terminal_without_current_approval_or_gateway(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=response)})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reader.recover(request) == terminal
    assert fake.calls == 1


def test_shared_cumulative_cap_exhaustion_leaves_the_other_arm_unreserved(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(
        tmp_path,
        one_attempt_shared_cap=True,
    )
    fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, _withheld_fixture, withheld_request, _withheld_preflight, withheld_call = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=response)})
    run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    with pytest.raises(StudyAdmissionError, match="cumulative token ceiling"):
        ledger.reserve(withheld_call)
    assert fake.calls == 1


def test_parser_failure_taxonomy_is_preserved(tmp_path) -> None:
    from dataclasses import replace as dc_replace

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request)
    forged_binding = dc_replace(response, binding=dc_replace(response.binding, hypothesis_id="other-hypothesis"))
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=forged_binding.canonical_bytes().decode("utf-8"))})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.terminal.failure_code is RoleFailureCode.EVIDENCE_BINDING
    assert "binding" in (terminal.terminal.failure_reason or "")


def test_pending_unknown_usage_blocks_the_other_arm_and_never_resends(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, _withheld_fixture, _withheld_request, _withheld_preflight, withheld_call = context
    fake = _CountingFake({request.sha256: RuntimeError("dispatch status unknown")})
    with pytest.raises(StudyPendingAccounting):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    with pytest.raises(StudyPendingAccounting):
        ledger.reserve(withheld_call)
    with pytest.raises(StudyPendingAccounting):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    assert fake.calls == 1


def test_reader_cannot_settle_a_persisted_reservation_without_its_ephemeral_owner(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyAuthorityError, match="owner capability"):
        reader.settle(
            reservation,
            completion=_completion(request=request, response_text=""),
            response_ref=ArtifactRefV5(
                f"adapter-blobs/study-v1-raw-responses/{request.sha256}.bin",
                "0" * 64,
            ),
            parsed_ref=None,
            failure_code=RoleFailureCode.RESPONSE_SCHEMA,
        )


def test_authenticated_reconciliation_settles_unknown_usage_without_a_gateway(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    with pytest.raises(StudyPendingAccounting):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=_CountingFake({request.sha256: RuntimeError("unknown dispatch")}),
            deadline_monotonic=1.0,
        )
    completion = _completion(
        request=request,
        response_text="",
        accepted=False,
        response_received=False,
    )
    terminal = ledger.reconcile_pending_usage(
        request,
        completion=completion,
        provider_reference="provider-reconciliation-1",
    )
    assert terminal.failure_code is RoleFailureCode.TRANSPORT
    assert terminal.usage.external_attempt_count == 1
    assert terminal.usage.input_tokens == 1
    assert ledger.verify_terminal(terminal.reference) == terminal
    with pytest.raises(StudyAuthorityError):
        ledger.reconcile_pending_usage(
            request,
            completion=completion,
            provider_reference="provider-reconciliation-tampered",
        )


def test_reopen_after_usage_metadata_before_raw_bytes_stays_pending(tmp_path, monkeypatch) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, approval, ledger, *_ = context
    reservation = ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    completion = _completion(request=request, response_text=_response_for(fixture_request).canonical_bytes().decode("utf-8"))
    original_put = type(store).put

    def interrupt_before_raw(self, *, kind, key, content):
        if kind == "raw-responses":
            raise StudyPendingAccounting("simulated interruption before raw response persistence")
        return original_put(self, kind=kind, key=key, content=content)

    monkeypatch.setattr(type(store), "put", interrupt_before_raw)
    with pytest.raises(StudyPendingAccounting, match="simulated interruption"):
        ledger.persist_response_for_request(request, completion)
    monkeypatch.setattr(type(store), "put", original_put)
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reader.recover(request)
    assert reservation.request_sha256 == request.sha256


def test_known_model_mismatch_and_usage_overage_remain_accounted_as_failure(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=response, returned_model="other/model", output_tokens=129)})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.failure_code is RoleFailureCode.ACCOUNTING
    assert terminal.usage.output_tokens == 129
    assert terminal.receipt.cumulative_total_tokens == 130
    assert ledger.verify_terminal(terminal.reference) == terminal


def test_provider_response_over_bound_settles_non_importable_with_known_usage(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    raw = "x" * (4 * 1024 * 1024 + 1)
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=raw, output_tokens=7)})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.failure_code is RoleFailureCode.ACCOUNTING
    assert terminal.usage.output_tokens == 7
    failure_refs = store.list_refs(kind="raw-response-failures")
    assert len(failure_refs) == 1
    marker = __import__("json").loads(store.read(failure_refs[0]))
    assert marker["raw_length"] == len(raw.encode("utf-8"))
    assert marker["raw_sha256"] == __import__("hashlib").sha256(raw.encode("utf-8")).hexdigest()
    assert "unavailable" in terminal.terminal.failure_reason.lower()


def test_fresh_reader_recovers_oversized_marker_after_terminal_write_interrupt(tmp_path, monkeypatch) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    raw = "x" * (4 * 1024 * 1024 + 1)
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=raw, output_tokens=7)})
    original_put = type(store).put

    def interrupt_before_terminal(self, *, kind, key, content):
        if kind == "terminals":
            raise StudyPendingAccounting("simulated interruption before terminal publication")
        return original_put(self, kind=kind, key=key, content=content)

    monkeypatch.setattr(type(store), "put", interrupt_before_terminal)
    with pytest.raises(StudyPendingAccounting, match="terminal publication"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=fake,
            deadline_monotonic=1.0,
        )
    monkeypatch.setattr(type(store), "put", original_put)

    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    terminal = reader.recover(request)
    assert terminal is not None
    assert terminal.failure_code is RoleFailureCode.ACCOUNTING
    assert terminal.usage.output_tokens == 7
    assert reader.verify_terminal(terminal.reference) == terminal
    assert fake.calls == 1


def test_prospective_cost_rounds_conservatively_upward(tmp_path) -> None:
    from decimal import localcontext

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, _fixture_request, _preflight, _manifest, request, _store, grant, _approval, ledger, *_ = context
    high_precision = Decimal("0." + "1" * 80)
    object.__setattr__(ledger, "grant", replace(grant, input_price_upper_bound=high_precision))
    with localcontext() as decimal_context:
        decimal_context.prec = 200
        exact = (Decimal(request.input_bound_bytes) * high_precision + Decimal(request.max_output_tokens) * grant.output_price_upper_bound) / Decimal(1_000_000)
    assert ledger._prospective_cost(request) >= exact


def test_invalid_json_keeps_exact_raw_response_and_accounted_usage(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, store, _grant, _approval, ledger, *_ = context
    raw = "  { invalid json }\n"
    fake = _CountingFake({request.sha256: _completion(request=request, response_text=raw)})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.failure_code is RoleFailureCode.RESPONSE_SCHEMA
    raw_ref = next(ref for ref in store.list_refs(kind="raw-responses") if request.sha256 in ref.relative_path)
    assert store.read(raw_ref).decode("utf-8") == raw
    assert ledger.verify_terminal(terminal.reference) == terminal


def test_transport_failure_with_known_rejection_usage_settles_truthfully(tmp_path) -> None:
    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    fake = _CountingFake({request.sha256: _completion(request=request, response_text="", accepted=False, response_received=False)})
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=fake,
        deadline_monotonic=1.0,
    )
    assert terminal.failure_code is RoleFailureCode.TRANSPORT
    assert terminal.usage.input_tokens == 1
    assert ledger.verify_terminal(terminal.reference) == terminal


def test_terminal_verification_rederives_usage_and_receipt_instead_of_trusting_self_hash(tmp_path, monkeypatch) -> None:
    from dataclasses import replace
    import hashlib
    import core.pit_optimizer_v5.two_round_study.ledger as ledger_module

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    _fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake({request.sha256: _completion(request=request, response_text=response)}),
        deadline_monotonic=1.0,
    )
    forged_usage = replace(terminal.usage, output_tokens=99, total_tokens=terminal.usage.input_tokens + 99)
    forged_attempt = replace(terminal.terminal.attempt, usage=forged_usage)
    receipt_payload = terminal.receipt.authenticated_payload()
    receipt_payload["attempt_facts_sha256"] = forged_attempt.sha256
    receipt_payload["cumulative_total_tokens"] = terminal.receipt.cumulative_total_tokens + 98
    forged_receipt = RoleTerminalReceiptV5(
        slot_id=terminal.receipt.slot_id,
        slot_request_sha256=terminal.receipt.slot_request_sha256,
        authorization_sha256=terminal.receipt.authorization_sha256,
        attempt_facts_sha256=forged_attempt.sha256,
        cumulative_external_attempts=terminal.receipt.cumulative_external_attempts,
        cumulative_total_tokens=terminal.receipt.cumulative_total_tokens + 98,
        cumulative_cost_usd=terminal.receipt.cumulative_cost_usd,
        terminal_sequence=terminal.receipt.terminal_sequence,
        receipt_sha256=hashlib.sha256(canonical_json_bytes_v5(receipt_payload)).hexdigest(),
    )
    payload = terminal.terminal.authenticated_payload()
    payload["usage"] = ledger_module._usage_primitive(forged_usage)
    payload["attempt"] = ledger_module._attempt_primitive(forged_attempt)
    payload["receipt"] = ledger_module._receipt_primitive(forged_receipt)
    forged_sha = hashlib.sha256(canonical_json_bytes_v5(payload)).hexdigest()
    forged = replace(
        terminal.terminal,
        usage=forged_usage,
        attempt=forged_attempt,
        receipt=forged_receipt,
        terminal_sha256=forged_sha,
    )
    forged_ref = ArtifactRefV5(
        f"adapter-blobs/study-v1-terminals/{forged_sha}.bin",
        hashlib.sha256(forged.canonical_bytes()).hexdigest(),
    )
    original_read = type(ledger.store).read
    original_list_refs = type(ledger.store).list_refs

    def read(store, reference):
        if reference == forged_ref:
            return forged.canonical_bytes()
        return original_read(store, reference)

    def list_refs(store, *, kind, maximum_entries=4096):
        if kind == "terminals":
            return (forged_ref,)
        return original_list_refs(store, kind=kind, maximum_entries=maximum_entries)

    monkeypatch.setattr(type(ledger.store), "read", read)
    monkeypatch.setattr(type(ledger.store), "list_refs", list_refs)
    with pytest.raises(StudyAuthorityError):
        ledger.verify_terminal(forged_ref)
    with pytest.raises(StudyAuthorityError):
        StudyLedgerV1(_store, _manifest, _grant, approval=None)


def test_terminal_verification_uses_frozen_checkpoint_after_projection_advances(tmp_path) -> None:
    from core.pit_optimizer_v5.search import CandidateArchiveReducerV5

    context = __import__("tests.test_pit_optimizer_v5_study_live_calls", fromlist=["_study_context"])._study_context(tmp_path)
    fixture, fixture_request, _preflight, _manifest, request, _store, _grant, _approval, ledger, *_ = context
    response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    terminal = run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=_CountingFake({request.sha256: _completion(request=request, response_text=response)}),
        deadline_monotonic=1.0,
    )
    reducer = CandidateArchiveReducerV5(
        discovery_plan=fixture.manifest.panel_plan,
        evaluator_contract=fixture.manifest.evaluator_contract,
        target=fixture.manifest.manifest.target,
        authorities=(),
        capacity=fixture.manifest.manifest.search.archive_capacity,
        pit_data_scope="production",
        semantic_mode="required",
    )
    fixture.repository.publish_projection(record_refs=(), reducer=reducer, generation=2)
    assert ledger.verify_terminal(terminal.reference) == terminal
