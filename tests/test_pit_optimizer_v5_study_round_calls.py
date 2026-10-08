from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import time

import pytest

from core.pit_optimizer_v5.two_round_study.contracts import StudyAdmissionError, StudyAuthorityError
from core.pit_optimizer_v5.two_round_study.ledger import StudyLedgerV1, run_study_call_v1
from core.pit_optimizer_v5.two_round_study.live_calls import (
    StudyCallRequestV1,
    StudyEvaluatorMetricV1,
    StudyRoundOneEvaluatorResultV1,
    StudyRoundCallSlotV1,
    authorize_study_round_call_admission_v1,
    bind_study_call_to_round_slot_v1,
    build_study_round_call_slot_v1,
)
from core.pit_optimizer_v5.two_round_study.transport import StudyOpenRouterGatewayV1


def _persist_round_one_evaluation(
    *,
    store,
    manifest,
    grant,
    base_request,
    first_request,
    first_terminal,
    response,
) -> tuple[StudyRoundOneEvaluatorResultV1, object]:
    result = StudyRoundOneEvaluatorResultV1(
        study_id=grant.study_id,
        manifest_sha256=manifest.sha256,
        grant_sha256=grant.sha256,
        arm=first_request.arm,
        parent_request_sha256=base_request.sha256,
        round_one_request_sha256=first_request.sha256,
        round_one_terminal_sha256=first_terminal.terminal.terminal_sha256,
        parsed_response_sha256=(
            first_terminal.terminal.parsed_ref.sha256
            if first_terminal.terminal.parsed_ref is not None
            else hashlib.sha256(b"unavailable-parsed-response").hexdigest()
        ),
        candidate_sha256=response.drafts[0].sha256,
        evaluator_sha256=manifest.rubric_sha256,
        universe_sha256=manifest.round_one_snapshot_ref.sha256,
        status="succeeded",
        metrics=(StudyEvaluatorMetricV1(metric_id="portfolio_return", value=Decimal("1.25"), unit="percent"),),
        failure_code=None,
    )
    reference = store.put(
        kind="round-one-evaluations",
        key=result.sha256,
        content=result.canonical_bytes(),
    )
    return result, reference


def test_round_slots_bind_feedback_and_dispatch_distinct_ledger_slots(tmp_path: Path) -> None:
    from tests.test_pit_optimizer_v5_study_ledger import _CountingFake, _completion, _response_for
    from tests.test_pit_optimizer_v5_study_live_calls import _study_context

    fixture, fixture_request, _preflight, manifest, base_request, store, grant, approval, _old_ledger, *_ = (
        _study_context(tmp_path)
    )
    original_grant_bytes = grant.canonical_bytes()
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
        execution_approval=approval,
        slots=(first_slot,),
        approval_reference="offline-fixture:round-one-contract-test",
    )
    ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        approval,
        round_call_admission=first_admission,
        round_call_approval=first_approval,
    )
    response = _response_for(fixture_request)
    response_text = response.canonical_bytes().decode("utf-8")
    first_gateway = _CountingFake(
        {first_request.sha256: _completion(request=first_request, response_text=response_text)}
    )
    first_terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=first_gateway,
        deadline_monotonic=1.0,
    )
    evaluator_result, evaluator_ref = _persist_round_one_evaluation(
        store=store,
        manifest=manifest,
        grant=grant,
        base_request=base_request,
        first_request=first_request,
        first_terminal=first_terminal,
        response=response,
    )
    authenticated_evaluation = ledger.authenticate_round_one_evaluator_result(
        request=first_request,
        reference=evaluator_ref,
    )
    second_slot = build_study_round_call_slot_v1(
        request=base_request,
        manifest=manifest,
        grant=grant,
        round_index=2,
        round_one_evaluation=authenticated_evaluation,
    )
    second_request = bind_study_call_to_round_slot_v1(request=base_request, slot=second_slot)
    second_admission, second_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=approval,
        slots=(first_slot, second_slot),
        approval_reference="offline-fixture:round-two-contract-test",
    )
    second_ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        approval,
        round_call_admission=second_admission,
        round_call_approval=second_approval,
    )
    second_gateway = _CountingFake(
        {second_request.sha256: _completion(request=second_request, response_text=response_text)}
    )
    second_terminal = run_study_call_v1(
        request=second_request,
        fixture_request=fixture_request,
        ledger=second_ledger,
        gateway=second_gateway,
        deadline_monotonic=1.0,
    )

    assert first_terminal.terminal.failure_code is None
    assert second_terminal.terminal.failure_code is None
    assert first_gateway.calls == second_gateway.calls == 1
    assert first_request.sha256 != second_request.sha256
    assert second_request.messages != first_request.messages
    assert second_request.messages[-1] == {
        "role": "user",
        "content": {
            "study_round_feedback_v1": {
                "round_one_request_sha256": first_request.sha256,
                "candidate_sha256": evaluator_result.candidate_sha256,
                "evaluator_sha256": manifest.rubric_sha256,
                "universe_sha256": manifest.round_one_snapshot_ref.sha256,
                "metrics": [{"metric_id": "portfolio_return", "unit": "percent", "value": "1.25"}],
            }
        },
    }
    assert second_request.round_slot.feedback_sha256 == evaluator_result.sha256
    assert StudyCallRequestV1.from_canonical_json(second_request.canonical_bytes()).canonical_bytes() == (
        second_request.canonical_bytes()
    )
    assert "round_slot" not in base_request.to_primitive()
    assert StudyCallRequestV1.from_canonical_json(base_request.canonical_bytes()).canonical_bytes() == (
        base_request.canonical_bytes()
    )
    assert grant.canonical_bytes() == original_grant_bytes
    assert first_admission.grant_sha256 == second_admission.grant_sha256
    assert second_admission.grant_sha256 == hashlib.sha256(original_grant_bytes).hexdigest()
    reservations = [item for _ref, item in second_ledger._reservations()]
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
    first_request = bind_study_call_to_round_slot_v1(request=base_request, slot=first_slot)
    admission, round_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=approval,
        slots=(first_slot,),
        approval_reference="offline-fixture:round-one-success-gate-test",
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
    response = _response_for(fixture_request)

    first_terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=first_gateway,
        deadline_monotonic=1.0,
    )

    assert first_terminal.terminal.failure_code is not None
    assert first_terminal.terminal.parsed_ref is None
    evaluator_result, evaluator_ref = _persist_round_one_evaluation(
        store=store,
        manifest=manifest,
        grant=grant,
        base_request=base_request,
        first_request=first_request,
        first_terminal=first_terminal,
        response=response,
    )
    with pytest.raises(StudyAdmissionError, match="successful parsed round one"):
        ledger.authenticate_round_one_evaluator_result(request=first_request, reference=evaluator_ref)
    with pytest.raises(StudyAdmissionError, match="authenticated round-one evaluator result"):
        build_study_round_call_slot_v1(
            request=base_request,
            manifest=manifest,
            grant=grant,
            round_index=2,
        )

    assert first_gateway.calls == 1
    assert evaluator_result.status == "succeeded"
    assert len(ledger._reservations()) == 1
    assert len(ledger._dispatch_claims()) == 1


@pytest.mark.parametrize(
    ("case", "error", "message"),
    (
        ("failed", StudyAdmissionError, "failed evaluator result"),
        ("cross_arm", StudyAuthorityError, "same arm"),
        ("request", StudyAuthorityError, "round-one request"),
        ("candidate", StudyAuthorityError, "candidate"),
        ("evaluator", StudyAuthorityError, "evaluator"),
        ("universe", StudyAuthorityError, "universe"),
    ),
)
def test_round_two_rejects_unbound_evaluator_result_before_request(
    tmp_path: Path,
    case: str,
    error: type[Exception],
    message: str,
) -> None:
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
    first_request = bind_study_call_to_round_slot_v1(request=base_request, slot=first_slot)
    admission, round_approval = authorize_study_round_call_admission_v1(
        store=store,
        manifest=manifest,
        grant=grant,
        execution_approval=approval,
        slots=(first_slot,),
        approval_reference="offline-fixture:round-one-lineage-test",
    )
    ledger = StudyLedgerV1(
        store,
        manifest,
        grant,
        approval,
        round_call_admission=admission,
        round_call_approval=round_approval,
    )
    response = _response_for(fixture_request)
    gateway = _CountingFake(
        {first_request.sha256: _completion(request=first_request, response_text=response.canonical_bytes().decode("utf-8"))}
    )
    first_terminal = run_study_call_v1(
        request=first_request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=gateway,
        deadline_monotonic=1.0,
    )
    result, _reference = _persist_round_one_evaluation(
        store=store,
        manifest=manifest,
        grant=grant,
        base_request=base_request,
        first_request=first_request,
        first_terminal=first_terminal,
        response=response,
    )
    if case == "failed":
        result = replace(result, status="failed", metrics=(), failure_code="evaluator_failed")
    elif case == "cross_arm":
        result = replace(result, arm="withheld")
    elif case == "request":
        result = replace(result, round_one_request_sha256=hashlib.sha256(b"different-request").hexdigest())
    elif case == "candidate":
        result = replace(result, candidate_sha256=hashlib.sha256(b"different-candidate").hexdigest())
    elif case == "evaluator":
        result = replace(result, evaluator_sha256=hashlib.sha256(b"different-evaluator").hexdigest())
    elif case == "universe":
        result = replace(result, universe_sha256=hashlib.sha256(b"different-universe").hexdigest())
    reference = store.put(
        kind="round-one-evaluations",
        key=result.sha256,
        content=result.canonical_bytes(),
    )

    with pytest.raises(error, match=message):
        ledger.authenticate_round_one_evaluator_result(request=first_request, reference=reference)

    assert gateway.calls == 1
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
        round_index=1,
        parent_request_sha256=base_request.sha256,
        feedback_sha256=None,
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
