from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import time

import pytest

from core.pit_optimizer_v5.two_round_study.contracts import StudyAdmissionError, StudyAuthorityError
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
from core.pit_optimizer_v5.two_round_study.live_calls import (
    StudyCallRequestV1,
    StudyRoundCallSlotV1,
    authorize_study_round_call_admission_v1,
    bind_study_call_to_round_slot_v1,
    build_study_round_call_slot_v1,
)
from core.pit_optimizer_v5.two_round_study.transport import StudyOpenRouterGatewayV1


def test_round_slots_bind_feedback_and_dispatch_distinct_ledger_slots(tmp_path: Path) -> None:
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    fixture, fixture_request, _preflight, manifest, base_request, store, grant, approval, _old_ledger, *_ = (
        _study_context(tmp_path)
    )
    original_grant_bytes = grant.canonical_bytes()
    feedback_sha256 = hashlib.sha256(b"measured-round-one-feedback").hexdigest()
    first_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=1,
    )
    second_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=2,
        feedback_sha256=feedback_sha256,
    )
    first_request = bind_study_call_to_round_slot_v1(request=base_request, slot=first_slot)
    second_request = bind_study_call_to_round_slot_v1(request=base_request, slot=second_slot)
    admission, round_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=approval,
        slots=(first_slot, second_slot),
        approval_reference="offline-fixture:round-call-contract-test",
    )
    ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        approval,
        round_call_admission=admission,
        round_call_approval=round_approval,
    )
    response_text = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    first_gateway = _CountingFake(
        {first_request.sha256: _completion(request=first_request, response_text=response_text)}
    )
    second_gateway = _CountingFake(
        {second_request.sha256: _completion(request=second_request, response_text=response_text)}
    )

    first_terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=first_gateway,
        deadline_monotonic=1.0,
    )
    second_terminal = run_study_call_v1(
        request=second_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=second_gateway,
        deadline_monotonic=1.0,
    )

    assert first_terminal.terminal.failure_code is None
    assert second_terminal.terminal.failure_code is None
    assert first_gateway.calls == second_gateway.calls == 1
    assert first_request.sha256 != second_request.sha256
    assert second_request.round_slot.feedback_sha256 == feedback_sha256
    assert StudyCallRequestV1.from_canonical_json(second_request.canonical_bytes()).canonical_bytes() == (
        second_request.canonical_bytes()
    )
    assert "round_slot" not in base_request.to_primitive()
    assert StudyCallRequestV1.from_canonical_json(base_request.canonical_bytes()).canonical_bytes() == (
        base_request.canonical_bytes()
    )
    assert grant.canonical_bytes() == original_grant_bytes
    assert admission.grant_sha256 == hashlib.sha256(original_grant_bytes).hexdigest()
    reservations = [item for _ref, item in ledger._reservations()]
    assert [item.slot_id for item in reservations] == [
        f"{grant.study_id}:primary:round:1",
        f"{grant.study_id}:primary:round:2",
    ]
    assert [item.request_sha256 for item in reservations] == [first_request.sha256, second_request.sha256]
    assert len(reservations) <= min(2, len(grant.arm_slots))
    with pytest.raises(FrozenInstanceError):
        second_slot.round_index = 1


def test_round_two_requires_successful_parsed_round_one(tmp_path: Path) -> None:
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    fixture, fixture_request, _preflight, manifest, base_request, store, grant, approval, _old_ledger, *_ = (
        _study_context(tmp_path)
    )
    first_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=1,
    )
    second_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=2,
        feedback_sha256=hashlib.sha256(b"feedback-without-successful-output").hexdigest(),
    )
    first_request = bind_study_call_to_round_slot_v1(request=base_request, slot=first_slot)
    second_request = bind_study_call_to_round_slot_v1(request=base_request, slot=second_slot)
    admission, round_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=approval,
        slots=(first_slot, second_slot),
        approval_reference="offline-fixture:round-two-success-gate-test",
    )
    ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        approval,
        round_call_admission=admission,
        round_call_approval=round_approval,
    )
    first_gateway = _CountingFake(
        {first_request.sha256: _completion(request=first_request, response_text="not-json")}
    )
    successful_response = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    second_gateway = _CountingFake(
        {second_request.sha256: _completion(request=second_request, response_text=successful_response)}
    )

    first_terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=first_gateway,
        deadline_monotonic=1.0,
    )

    assert first_terminal.terminal.failure_code is not None
    assert first_terminal.terminal.parsed_ref is None
    with pytest.raises(StudyAdmissionError, match="successful parsed round one"):
        run_study_call_v1(
            request=second_request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=second_gateway,
            deadline_monotonic=1.0,
        )

    assert first_gateway.calls == 1
    assert second_gateway.calls == 0
    assert len(ledger._reservations()) == 1
    assert len(ledger._dispatch_claims()) == 1


def test_round_slot_without_admission_fails_before_credential_lookup(tmp_path: Path, monkeypatch) -> None:
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    (_fixture, fixture_request, _preflight, manifest, base_request, store, grant, approval, _old_ledger, *_rest) = (
        _study_context(tmp_path, mode="live_study")
    )
    slot = StudyRoundCallSlotV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        arm=base_request.arm,
        round_index=2,
        parent_request_sha256=base_request.sha256,
        feedback_sha256=hashlib.sha256(b"measured-feedback").hexdigest(),
    )
    request = bind_study_call_to_round_slot_v1(request=base_request, slot=slot)
    ledger = StudyLedgerV1(store, manifest, grant, approval)
    live_admission_slot = StudyRoundCallSlotV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        arm=base_request.arm,
        round_index=1,
        parent_request_sha256=base_request.sha256,
        feedback_sha256=None,
    )
    with pytest.raises(StudyAdmissionError, match="offline-fixture only"):
        authorize_study_round_call_admission_v1(
            store=store,
            manifest=manifest,
            grant=grant,
            execution_approval=approval,
            slots=(live_admission_slot,),
            approval_reference="manual:live-round-call-must-be-rejected",
        )
    gateway = StudyOpenRouterGatewayV1(
        ledger=ledger,
        credential_environment_variable="V5_ROUND_SLOT_MUST_NOT_READ_TOKEN",
    )
    original_get = os.environ.get
    credential_lookups: list[str] = []

    def guarded_get(key, default=None):
        if key == "V5_ROUND_SLOT_MUST_NOT_READ_TOKEN":
            credential_lookups.append(key)
            raise AssertionError("credential access preceded round-slot admission")
        return original_get(key, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    with pytest.raises(StudyAuthorityError, match="round-call admission"):
        run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=time.monotonic() + 60.0,
        )

    assert credential_lookups == []
    assert store.list_refs(kind="reservations") == ()
    assert store.list_refs(kind="dispatches") == ()
