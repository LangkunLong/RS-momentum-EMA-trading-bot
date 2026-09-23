from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

import agent_loop
from core.pit_optimizer_v5.provider import CompletionResultV5, ProviderFailureDiagnosticV5, ProviderResponseAccountingErrorV5
from core.pit_optimizer_v5.two_round_study.contracts import StudyAuthorityError, StudyContractError, StudyPendingAccounting
from core.pit_optimizer_v5.two_round_study.driver import prepare_two_round_study_v1
from core.pit_optimizer_v5.two_round_study.__main__ import _offline_ledger
from core.pit_optimizer_v5.two_round_study.live_calls import authenticate_fixture_preflight_v1
from core.pit_optimizer_v5.two_round_study.ledger import (
    StudyLedgerV1,
    _completion_from_metadata,
    _completion_metadata_primitive,
    run_study_call_v1,
)
from core.pit_optimizer_v5.two_round_study.store import StudyStoreV1
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5
from core.pit_optimizer_v5.two_round_study.transport import StudyOpenRouterGatewayV1
from core.pit_optimizer_v5.two_round_study.trace import export_study_trace_v1
from core.pit_optimizer_v5.two_round_study.verification import load_prepared_study_v1, verify_study_v1
from tests.test_pit_optimizer_v5_study_ledger import _response_for
from tests.test_pit_optimizer_v5_study_live_calls import _study_context
from tests.test_pit_optimizer_v5_study_trace import _long_files, _long_read_bytes


def _install_external_network_block(monkeypatch):
    """Block all external connects while precreating asyncio's local wakeup pair."""

    loopback_pair = socket.socketpair()
    consumed = False

    def reserved_socketpair(*_args, **_kwargs):
        nonlocal consumed
        if consumed:
            raise AssertionError("only the event loop's one precreated local socketpair is allowed")
        consumed = True
        return loopback_pair

    def blocked(*_args, **_kwargs):
        raise AssertionError("external networking is blocked in this test")

    monkeypatch.setattr(socket, "socketpair", reserved_socketpair)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    return loopback_pair


def _sdk_body(request, fixture_request, *, with_usage: bool = True) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "sdk-response-id-safe",
        "object": "chat.completion",
        "created": 1,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": _response_for(fixture_request).canonical_bytes().decode("utf-8"),
                },
                "finish_reason": "stop",
            }
        ],
    }
    if with_usage:
        body["usage"] = {
            "prompt_tokens": 7,
            "completion_tokens": 5,
            "total_tokens": 12,
            "cost": 0.000123,
        }
    else:
        body["usage"] = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
    return body


def _install_mock_async_openai(
    monkeypatch,
    handler,
    *,
    close_fails: bool = False,
    close_blocks: bool = False,
    strict_response_validation: bool = False,
):
    assert type(openai.__version__) is str and openai.__version__
    assert type(httpx.__version__) is str and httpx.__version__
    transport = httpx.MockTransport(handler)
    created = []
    real_async_openai = openai.AsyncOpenAI

    assert not (close_fails and close_blocks)

    class ClosingFailureClient(real_async_openai):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("sensitive close failure detail")

    class BlockingCloseClient(real_async_openai):
        async def close(self) -> None:
            self.close_started = time.monotonic()
            try:
                await asyncio.Future()
            finally:
                self.close_cancelled_at = time.monotonic()
                await super().close()

    client_type = (
        ClosingFailureClient
        if close_fails
        else BlockingCloseClient
        if close_blocks
        else real_async_openai
    )

    def construct(**kwargs):
        # The installed SDK otherwise consults OPENAI_WEBHOOK_SECRET even for
        # this ordinary chat-completion path. Pass an inert test-only value;
        # the startup hook keeps all real credential names unreadable.
        kwargs["webhook_secret"] = "synthetic-test-only-webhook-secret"
        if strict_response_validation:
            # Test-only divergence from the default SDK setting. This exercises
            # the installed SDK's typed Pydantic response-validation exception.
            kwargs["_strict_response_validation"] = True
        client = client_type(
            **kwargs,
            http_client=httpx.AsyncClient(transport=transport, trust_env=False),
        )
        created.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", construct)
    return created


def _all_artifact_bytes(root):
    return [_long_read_bytes(path) for _relative, path in _long_files(root)]


def _assert_no_sensitive_artifact_bytes(root, *extra_values: bytes) -> None:
    forbidden = (
        b"synthetic-test-only-credential",
        b"synthetic-test-only-webhook-secret",
        *extra_values,
    )
    assert all(not any(value in raw for value in forbidden) for raw in _all_artifact_bytes(root))


def _test_gateway_context(tmp_path, monkeypatch):
    # This is a disposable offline-fixture grant. The test seam replaces only
    # the adapter's live-mode admission gate; dispatch still goes through the
    # real ledger claim and the real provider adapter/SDK transport below.
    context = _study_context(tmp_path, mode="offline_fixture")
    fixture, fixture_request, _preflight, manifest, request, store, grant, _approval, ledger, *_ = context
    gateway = StudyOpenRouterGatewayV1(
        ledger=ledger,
        credential_environment_variable="V5_TRANSPORT_TEST_TOKEN",
    )

    def test_only_admission(*, request_sha256, model, messages, response_schema_json, max_output_tokens):
        return ledger.claim_dispatch(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )

    gateway._admitted = test_only_admission
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("V5_TRANSPORT_TEST_TOKEN", "synthetic-test-only-credential")
    return fixture, fixture_request, manifest, request, store, grant, ledger, gateway


def _run(context, gateway, *, deadline_seconds: float = 60.0):
    fixture, fixture_request, _manifest, request, _store, _grant, ledger, _adapter = context
    return run_study_call_v1(
        request=request,
        fixture_request=fixture_request,
        ledger=ledger,
        gateway=gateway,
        deadline_monotonic=time.monotonic() + deadline_seconds,
    )


@pytest.mark.parametrize("close_fails", (False, True))
def test_actual_openai_mocktransport_success_persists_known_usage_and_cleanup_state(
    tmp_path, monkeypatch, close_fails
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, _ledger, gateway = context
    requests = []

    def handler(http_request):
        requests.append(http_request)
        assert http_request.headers["authorization"] == "Bearer synthetic-test-only-credential"
        return httpx.Response(200, json=_sdk_body(request, fixture_request))

    clients = _install_mock_async_openai(monkeypatch, handler, close_fails=close_fails)
    try:
        terminal = _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == 1
    assert len(clients) == 1
    assert clients[0].max_retries == 0
    assert terminal.terminal.usage.input_tokens == 7
    assert terminal.terminal.usage.output_tokens == 5
    assert terminal.terminal.usage.cost_usd == Decimal("0.000123")
    response_ref = store.list_refs(kind="responses")[0]
    response_metadata = json.loads(store.read(response_ref))
    if close_fails:
        assert response_metadata["completion_metadata"]["cleanup_diagnostic"] == {
            "phase": "client_cleanup",
            "code": "client_cleanup_failed",
        }
    else:
        assert "cleanup_diagnostic" not in response_metadata["completion_metadata"]
    raw_ref = store.list_refs(kind="raw-responses")[0]
    assert store.read(raw_ref).decode("utf-8") == _response_for(fixture_request).canonical_bytes().decode("utf-8")

    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reopened.recover(request) == terminal

    _assert_no_sensitive_artifact_bytes(
        store.repository.root,
        b"sensitive close failure detail",
    )


def test_owned_client_close_respects_deadline_without_losing_accounted_response(tmp_path, monkeypatch) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, _ledger, gateway = context
    calls = []
    deadline = time.monotonic() + 30.0
    response_returned = False
    real_monotonic = time.monotonic

    def handler(http_request):
        nonlocal response_returned
        calls.append(http_request)
        response_returned = True
        return httpx.Response(200, json=_sdk_body(request, fixture_request))

    clients = _install_mock_async_openai(monkeypatch, handler, close_blocks=True)

    def adapter_clock():
        # Keep local request preparation and the SDK call within a generous
        # admitted deadline, then leave only 100ms for owned-client cleanup.
        # The asyncio loop, SDK, ledger, and test timing retain the real clock.
        if response_returned:
            return deadline - 0.1
        return real_monotonic()

    monkeypatch.setattr(agent_loop, "time", SimpleNamespace(monotonic=adapter_clock))
    try:
        terminal = run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=context[6],
            gateway=gateway,
            deadline_monotonic=deadline,
        )
    finally:
        pair[0].close()
        pair[1].close()

    assert len(calls) == len(clients) == 1
    assert clients[0].max_retries == 0
    assert clients[0].close_started is not None
    assert clients[0].close_cancelled_at is not None
    assert 0.05 <= clients[0].close_cancelled_at - clients[0].close_started < 0.5
    assert terminal.terminal.usage.input_tokens == 7
    assert terminal.terminal.usage.output_tokens == 5
    response_ref = store.list_refs(kind="responses")[0]
    response_metadata = json.loads(store.read(response_ref))
    assert response_metadata["completion_metadata"]["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    raw_ref = store.list_refs(kind="raw-responses")[0]
    assert store.read(raw_ref).decode("utf-8") == _response_for(fixture_request).canonical_bytes().decode("utf-8")
    assert StudyLedgerV1(store, manifest, grant, approval=None).recover(request) == terminal
    _assert_no_sensitive_artifact_bytes(store.repository.root, b"sensitive close failure detail")


@pytest.mark.parametrize(
    ("http_error", "expected_code"),
    [
        (httpx.ConnectError, "api_connection_error"),
        (httpx.ReadTimeout, "api_timeout"),
    ],
)
def test_actual_openai_mocktransport_connection_and_timeout_are_typed_pending_failures(
    tmp_path, monkeypatch, http_error, expected_code
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, ledger, gateway = context
    attempts = []

    def handler(http_request):
        attempts.append(http_request)
        raise http_error("sensitive SDK transport detail", request=http_request)

    clients = _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(attempts) == 1
    assert clients[0].max_retries == 0
    assert len(store.list_refs(kind="reservations")) == 1
    assert len(store.list_refs(kind="dispatches")) == 1
    assert store.list_refs(kind="responses") == ()
    assert store.list_refs(kind="terminals") == ()
    diagnostic = json.loads(store.read(store.list_refs(kind="provider-diagnostics")[0]))
    assert diagnostic["phase"] == "transport"
    assert diagnostic["code"] == expected_code
    assert diagnostic["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    _assert_no_sensitive_artifact_bytes(store.repository.root, b"sensitive SDK transport detail")
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_actual_openai_mocktransport_invalid_json_is_response_decode_failure_without_body_persistence(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, _ledger, gateway = context
    requests = []
    secret_body = b'{"private-provider-body":"never persist this"'

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(
            200,
            content=secret_body,
            headers={"content-type": "application/json", "x-request-id": "decode-request-id"},
        )

    clients = _install_mock_async_openai(monkeypatch, handler)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == len(clients) == 1
    assert clients[0].max_retries == 0
    diagnostic = json.loads(store.read(store.list_refs(kind="provider-diagnostics")[0]))
    assert (diagnostic["phase"], diagnostic["code"]) == (
        "response_extraction",
        "response_decoding_failed",
    )
    # The SDK raises JSONDecodeError without exposing the HTTP response object;
    # its header ID and malformed body therefore are not available to persist.
    assert diagnostic["provider_request_id"] is None
    _assert_no_sensitive_artifact_bytes(store.repository.root, secret_body)
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_actual_openai_mocktransport_response_validation_is_typed_and_keeps_only_safe_id(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, _ledger, gateway = context
    requests = []
    private_body = b'{"private-provider-body":"never persist this"}'

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(
            200,
            content=private_body,
            headers={"content-type": "application/json", "x-request-id": "validation-request-id"},
        )

    clients = _install_mock_async_openai(
        monkeypatch,
        handler,
        strict_response_validation=True,
    )
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == len(clients) == 1
    assert clients[0].max_retries == 0
    diagnostic = json.loads(store.read(store.list_refs(kind="provider-diagnostics")[0]))
    assert (diagnostic["phase"], diagnostic["code"]) == (
        "response_extraction",
        "response_validation_failed",
    )
    assert diagnostic["http_status"] == 200
    assert diagnostic["provider_request_id"] == "validation-request-id"
    _assert_no_sensitive_artifact_bytes(store.repository.root, private_body)
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_missing_usage_and_cleanup_failure_preserve_primary_observation_and_exact_content(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, ledger, gateway = context
    content = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    requests = []

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(200, json=_sdk_body(request, fixture_request, with_usage=False))

    _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == 1
    observations = store.list_refs(kind="response-observations")
    raw_refs = store.list_refs(kind="observed-raw-responses")
    assert len(observations) == len(raw_refs) == 1
    observation = json.loads(store.read(observations[0]))
    assert observation["phase"] == "response_accounting"
    assert observation["code"] == "inline_usage_missing"
    assert observation["provider_request_id"] == "sdk-response-id-safe"
    assert observation["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    assert store.read(raw_refs[0]).decode("utf-8") == content
    _assert_no_sensitive_artifact_bytes(
        store.repository.root,
        b"synthetic-test-only-webhook-secret",
    )
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_typed_accounting_failure_from_sdk_boundary_survives_cleanup_failure(tmp_path, monkeypatch) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, fixture_request, manifest, request, store, grant, _ledger, gateway = context
    content = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    captured = []

    async def fail_with_typed_accounting(**_kwargs):
        raise ProviderResponseAccountingErrorV5(
            response_text=content,
            provider_request_id="typed-accounting-safe-id",
            phase="response_accounting",
            code="inline_usage_missing",
        )

    class SyntheticClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail_with_typed_accounting))
            self.max_retries = 0

        async def close(self):
            raise RuntimeError("sensitive typed-path cleanup detail")

    def construct(**kwargs):
        captured.append(kwargs)
        return SyntheticClient()

    monkeypatch.setattr(openai, "AsyncOpenAI", construct)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(captured) == 1
    assert captured[0]["max_retries"] == 0
    observation_ref = store.list_refs(kind="response-observations")[0]
    observation = json.loads(store.read(observation_ref))
    assert (observation["phase"], observation["code"]) == (
        "response_accounting",
        "inline_usage_missing",
    )
    assert observation["provider_request_id"] == "typed-accounting-safe-id"
    assert observation["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    raw_ref = store.list_refs(kind="observed-raw-responses")[0]
    assert store.read(raw_ref).decode("utf-8") == content
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        StudyLedgerV1(store, manifest, grant, approval=None).recover(request)
    _assert_no_sensitive_artifact_bytes(
        store.repository.root,
        b"sensitive typed-path cleanup detail",
        b"synthetic-test-only-webhook-secret",
    )


@pytest.mark.parametrize(
    ("failure_stage", "expected_phase", "expected_code"),
    [
        ("gateway_init", "gateway_init", "gateway_initialization_failed"),
        ("gateway_import", "gateway_init", "gateway_initialization_failed"),
        ("client_init", "client_init", "client_initialization_failed"),
        ("request_preparation", "request_preparation", "request_preparation_failed"),
    ],
)
def test_actual_adapter_classifies_gateway_sdk_client_and_request_preparation_failures(
    tmp_path, monkeypatch, failure_stage, expected_phase, expected_code
) -> None:
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, _ledger, gateway = context
    import agent_loop

    if failure_stage == "gateway_init":
        def fail_gateway(**_kwargs):
            raise RuntimeError("sensitive gateway construction detail")

        monkeypatch.setattr(agent_loop, "OpenRouterGateway", fail_gateway)
    elif failure_stage == "gateway_import":
        monkeypatch.setitem(sys.modules, "agent_loop", None)
    elif failure_stage == "client_init":
        def fail_client(**_kwargs):
            raise RuntimeError("sensitive SDK client initialization detail")

        monkeypatch.setattr(openai, "AsyncOpenAI", fail_client)
    else:
        import core.pit_optimizer_v5.provider as provider_module

        def fail_schema(*_args, **_kwargs):
            raise RuntimeError("sensitive request preparation detail")

        monkeypatch.setattr(provider_module, "wire_role_schema_v5", fail_schema)

    with pytest.raises(StudyPendingAccounting):
        _run(context, gateway)

    assert len(store.list_refs(kind="reservations")) == 1
    assert len(store.list_refs(kind="dispatches")) == 1
    diagnostic_ref = store.list_refs(kind="provider-diagnostics")[0]
    diagnostic = json.loads(store.read(diagnostic_ref))
    assert (diagnostic["phase"], diagnostic["code"]) == (expected_phase, expected_code)
    _assert_no_sensitive_artifact_bytes(store.repository.root, b"sensitive")
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_response_extraction_without_string_content_keeps_safe_id_and_cleanup_diagnostic(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, _ledger, gateway = context
    requests = []

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(
            200,
            json={
                "id": "sdk-response-id-safe",
                "object": "chat.completion",
                "created": 1,
                "model": request.model,
                "choices": [],
            },
        )

    clients = _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == len(clients) == 1
    assert len(store.list_refs(kind="provider-diagnostics")) == 1
    diagnostic_ref = store.list_refs(kind="provider-diagnostics")[0]
    diagnostic = json.loads(store.read(diagnostic_ref))
    assert diagnostic["phase"] == "response_extraction"
    assert diagnostic["code"] == "response_content_unavailable"
    assert diagnostic["provider_request_id"] == "sdk-response-id-safe"
    assert diagnostic["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    assert store.list_refs(kind="response-observations") == ()
    assert store.list_refs(kind="observed-raw-responses") == ()


def test_nonstring_content_with_complete_usage_settles_rejected_without_http_body_bytes(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, _ledger, gateway = context
    attempts = []

    def handler(http_request):
        attempts.append(http_request)
        body = _sdk_body(request, fixture_request)
        body["choices"][0]["message"]["content"] = None
        return httpx.Response(200, json=body)

    clients = _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    try:
        terminal = _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(attempts) == len(clients) == 1
    assert clients[0].max_retries == 0
    assert terminal.terminal.failure_code is not None
    assert terminal.terminal.usage.input_tokens == 7
    assert terminal.terminal.usage.output_tokens == 5
    assert terminal.terminal.usage.cost_usd == Decimal("0.000123")
    assert store.list_refs(kind="provider-diagnostics") == ()
    assert store.list_refs(kind="response-observations") == ()
    response_payload = json.loads(store.read(store.list_refs(kind="responses")[0]))
    assert response_payload["response_length"] == 0
    assert response_payload["completion_metadata"]["accepted"] is False
    assert response_payload["completion_metadata"]["response_received"] is True
    assert response_payload["completion_metadata"]["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    raw_ref = store.list_refs(kind="raw-responses")[0]
    assert store.read(raw_ref) == b""
    assert StudyLedgerV1(store, manifest, grant, approval=None).recover(request) == terminal
    _assert_no_sensitive_artifact_bytes(store.repository.root)


def test_actual_mocktransport_cleanup_metadata_reopens_and_exports_exact_bytes(tmp_path, monkeypatch) -> None:
    pair = _install_external_network_block(monkeypatch)
    from core.pit_optimizer_v5.two_round_study import driver as study_driver

    boundary_path = tmp_path / "actual-sdk-export-boundaries.jsonl"
    boundary_path.touch(exist_ok=False)
    events = []

    def record(event, **fields):
        item = {"event": event, "monotonic": time.monotonic(), **fields}
        events.append(item)
        with boundary_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()

    original_run = subprocess.run
    original_collect = study_driver.gc.collect
    original_reopen = study_driver._fresh_process_reopen
    child_index = 0

    def guarded_reopen_process(args, *positional, **kwargs):
        nonlocal child_index
        command = list(args) if isinstance(args, (tuple, list)) else [str(args)]
        if command == ["git", "rev-parse", "HEAD"]:
            started = time.monotonic()
            result = original_run(args, *positional, **kwargs)
            record(
                "local_git_revision",
                elapsed_seconds=round(time.monotonic() - started, 4),
                returncode=result.returncode,
                stdout_sha256=hashlib.sha256(str(result.stdout or "").encode("utf-8")).hexdigest(),
                stderr_length=len(str(result.stderr or "")),
            )
            return result
        if len(command) < 4 or command[0].casefold() != "py" or command[1:3] != ["-3.13", "-c"]:
            raise AssertionError("unexpected subprocess during offline fixture preparation")

        child_index += 1
        child_dir = tmp_path / "guarded-fresh-reopen" / f"child-{child_index}"
        child_dir.mkdir(parents=True, exist_ok=False)
        installed_marker = child_dir / "guard-installed.marker"
        tripwire_marker = child_dir / "tripwire.marker"
        child_env = dict(kwargs.get("env") or {})
        credential_names = (
            "OPENROUTER_API_KEY", "OPENROUTER", "OPENROUTER_MANAGEMENT_KEY",
            "OPENROUTER_MANAGEMENT_API_KEY", "OPENAI_API_KEY", "OPENAI_WEBHOOK_SECRET",
            "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "AZURE_OPENAI_API_KEY",
            "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FMP_API_KEY", "NOTIFY_EMAIL_PASSWORD",
        )
        if any(child_env.get(name) for name in credential_names):
            raise AssertionError("provider credential crossed the fresh-reopen boundary")
        hook = Path(__file__).resolve().parent / "fixtures" / "v5_transport_child_guard"
        if not (hook / "sitecustomize.py").is_file():
            raise AssertionError("tracked fresh-reopen child guard fixture is missing")
        child_env["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(hook), child_env.get("PYTHONPATH", "")) if value
        )
        child_env.update(
            {
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "V5_TRANSPORT_GUARD_INSTALLED_MARKER": str(installed_marker),
                "V5_TRANSPORT_TRIPWIRE_MARKER": str(tripwire_marker),
            }
        )
        kwargs["env"] = child_env
        kwargs["timeout"] = 30
        started = time.monotonic()
        record("fresh_reopen_child_start", child=child_index)
        try:
            result = original_run(args, *positional, **kwargs)
        except subprocess.TimeoutExpired:
            record(
                "fresh_reopen_child_timeout",
                child=child_index,
                elapsed_seconds=round(time.monotonic() - started, 4),
                guard_installed=installed_marker.is_file(),
                tripwire_fired=tripwire_marker.exists(),
            )
            raise
        stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode("utf-8")
        stderr = result.stderr if isinstance(result.stderr, bytes) else str(result.stderr or "").encode("utf-8")
        record(
            "fresh_reopen_child_end",
            child=child_index,
            elapsed_seconds=round(time.monotonic() - started, 4),
            returncode=result.returncode,
            guard_installed=installed_marker.is_file(),
            tripwire_fired=tripwire_marker.exists(),
            stdout_length=len(stdout),
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_length=len(stderr),
            stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        )
        if not installed_marker.is_file() or tripwire_marker.exists():
            raise AssertionError("fresh-reopen child guard missing or tripped")
        return result

    def timed_collect(*args, **kwargs):
        started = time.monotonic()
        record("gc_collect_start")
        try:
            return original_collect(*args, **kwargs)
        finally:
            record("gc_collect_end", elapsed_seconds=round(time.monotonic() - started, 4))

    def timed_reopen(*args, **kwargs):
        started = time.monotonic()
        root = args[0] if args else kwargs.get("root")
        arm = kwargs.get("arm")
        record("fresh_reopen_start", arm=arm, root_name=Path(root).name if root is not None else None)
        try:
            result = original_reopen(*args, **kwargs)
        except BaseException as exc:
            record(
                "fresh_reopen_error",
                arm=arm,
                elapsed_seconds=round(time.monotonic() - started, 4),
                error_type=type(exc).__name__,
            )
            raise
        record("fresh_reopen_end", arm=arm, elapsed_seconds=round(time.monotonic() - started, 4))
        return result

    monkeypatch.setattr(subprocess, "run", guarded_reopen_process)
    monkeypatch.setattr(study_driver.gc, "collect", timed_collect)
    monkeypatch.setattr(study_driver, "_fresh_process_reopen", timed_reopen)
    record("prepare_start")
    prepare_started = time.monotonic()
    prepared = prepare_two_round_study_v1(
        root=tmp_path / "prepared-cleanup-export-study",
        mode="offline_fixture",
        provider_settings=None,
    )
    record(
        "prepare_end",
        elapsed_seconds=round(time.monotonic() - prepare_started, 4),
        mode=prepared.mode,
        child_count=child_index,
    )
    assert child_index == 3
    git_revision_events = [item for item in events if item["event"] == "local_git_revision"]
    assert git_revision_events
    assert all(item["returncode"] == 0 for item in git_revision_events)
    assert len({item["stdout_sha256"] for item in git_revision_events}) == 1
    assert all(
        item.get("guard_installed") is True and item.get("tripwire_fired") is False
        for item in events
        if item["event"] == "fresh_reopen_child_end"
    )
    ledger = _offline_ledger(prepared)
    request = prepared.live_call_for("primary")
    fixture_request = authenticate_fixture_preflight_v1(
        prepared.preflight_for("primary"),
        require_current=True,
    )
    store = ledger.store
    gateway = StudyOpenRouterGatewayV1(
        ledger=ledger,
        credential_environment_variable="V5_TRANSPORT_TEST_TOKEN",
    )

    def test_only_admission(*, request_sha256, model, messages, response_schema_json, max_output_tokens):
        return ledger.claim_dispatch(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )

    # This authority seam is confined to the disposable offline fixture. The
    # SDK adapter and its installed HTTP stack remain the code under test.
    gateway._admitted = test_only_admission
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("V5_TRANSPORT_TEST_TOKEN", "synthetic-test-only-credential")
    requests = []

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(200, json=_sdk_body(request, fixture_request))

    clients = _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    record("sdk_request_start")
    try:
        terminal = run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=time.monotonic() + 60.0,
        )
    finally:
        pair[0].close()
        pair[1].close()
    record(
        "sdk_request_end",
        request_count=len(requests),
        client_count=len(clients),
        local_retries=clients[0].max_retries if clients else None,
        known_usage=(terminal.terminal.usage.input_tokens, terminal.terminal.usage.output_tokens),
    )

    assert len(requests) == len(clients) == 1
    assert clients[0].max_retries == 0
    response_ref = store.list_refs(kind="responses")[0]
    response_bytes = store.read(response_ref)
    response_payload = json.loads(response_bytes)
    assert response_payload["completion_metadata"]["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }

    reloaded = load_prepared_study_v1(root=prepared.root)
    reloaded_store = StudyStoreV1(reloaded.store_repository())
    read_ledger = StudyLedgerV1(reloaded_store, reloaded.manifest, ledger.grant, approval=None)
    assert reloaded_store.read(response_ref) == response_bytes
    assert read_ledger.recover(request) == terminal
    verification = verify_study_v1(prepared=reloaded, store=reloaded_store, ledger=read_ledger)
    output = tmp_path / "cleanup-metadata-export"
    record("reopen_verify_export_start")
    export_study_trace_v1(prepared=reloaded, verification=verification, store=reloaded_store, output=output)
    index = json.loads(_long_read_bytes(output / "artifact-index.json"))
    matching = [item for item in index["artifacts"] if item["source_sha256"] == response_ref.sha256]
    assert len(matching) == 1
    exported_response = output / Path(*str(matching[0]["relative_path"]).split("/"))
    assert _long_read_bytes(exported_response) == response_bytes

    raw_ref = store.list_refs(kind="raw-responses")[0]
    raw_bytes = store.read(raw_ref)
    raw_entries = [item for item in index["artifacts"] if item["source_sha256"] == raw_ref.sha256]
    assert len(raw_entries) == 1
    exported_raw = output / Path(*str(raw_entries[0]["relative_path"]).split("/"))
    assert _long_read_bytes(exported_raw) == raw_bytes
    assert StudyLedgerV1(store, prepared.manifest, ledger.grant, approval=None).recover(request) == terminal
    _assert_no_sensitive_artifact_bytes(prepared.root)
    record(
        "reopen_verify_export_end",
        matching_response_entries=len(matching),
        matching_raw_entries=len(raw_entries),
        response_sha256=hashlib.sha256(response_bytes).hexdigest(),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def test_response_processing_failure_after_content_preserves_exact_observation_and_cleanup(
    tmp_path, monkeypatch
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, _ledger, gateway = context
    content = _response_for(fixture_request).canonical_bytes().decode("utf-8")
    requests = []

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(200, json=_sdk_body(request, fixture_request))

    _install_mock_async_openai(monkeypatch, handler, close_fails=True)
    import agent_loop

    def fail_accounting(_response, **_kwargs):
        raise RuntimeError("sensitive post-response accounting detail")

    monkeypatch.setattr(agent_loop, "_usage_from_response", fail_accounting)
    try:
        with pytest.raises(StudyPendingAccounting):
            _run(context, gateway)
    finally:
        pair[0].close()
        pair[1].close()

    assert len(requests) == 1
    observation = json.loads(store.read(store.list_refs(kind="response-observations")[0]))
    assert observation["phase"] == "response_accounting"
    assert observation["code"] == "response_accounting_failed"
    assert observation["provider_request_id"] == "sdk-response-id-safe"
    assert observation["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    assert store.read(store.list_refs(kind="observed-raw-responses")[0]).decode("utf-8") == content
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


def test_stage_classifier_maps_sdk_timeout_without_importing_sdk_at_provider_module_load() -> None:
    error = type("APITimeoutError", (Exception,), {"__module__": "openai"})("sensitive message")
    diagnostic = ProviderFailureDiagnosticV5.from_exception(error, stage="transport")
    assert (diagnostic.phase, diagnostic.code) == ("transport", "api_timeout")
    serialized = f"{diagnostic.phase}:{diagnostic.code}"
    assert "sensitive" not in serialized


def test_explicit_cleanup_stage_wins_over_credential_like_status() -> None:
    error = type("APIStatusError", (Exception,), {"__module__": "openai", "status_code": 401})()
    diagnostic = ProviderFailureDiagnosticV5.from_exception(error, stage="client_cleanup")
    assert (diagnostic.phase, diagnostic.code, diagnostic.http_status) == (
        "client_cleanup",
        "client_cleanup_failed",
        401,
    )


@pytest.mark.parametrize(
    "bad_cleanup",
    [
        None,
        {"phase": "client_cleanup", "code": "client_cleanup_failed", "extra": "not-allowed"},
        {"phase": "transport", "code": "api_timeout"},
    ],
)
def test_cleanup_diagnostic_validation_is_strict_for_all_three_envelope_shapes(
    tmp_path, monkeypatch, bad_cleanup
) -> None:

    legacy_completion = CompletionResultV5(
        response_text="{}",
        accepted=True,
        input_tokens=1,
        output_tokens=1,
        provider_request_id=None,
        returned_model="offline_fixture/study-v1",
        cost_usd=Decimal("0"),
        external_attempt_count=1,
        response_received=True,
    )
    legacy_metadata = _completion_metadata_primitive(legacy_completion)
    assert "cleanup_diagnostic" not in legacy_metadata
    assert canonical_json_bytes_v5(
        _completion_metadata_primitive(_completion_from_metadata(legacy_metadata, "{}"))
    ) == canonical_json_bytes_v5(legacy_metadata)
    completion_with_cleanup = _completion_metadata_primitive(
        CompletionResultV5(
            response_text="{}",
            accepted=True,
            input_tokens=1,
            output_tokens=1,
            provider_request_id=None,
            returned_model="offline_fixture/study-v1",
            cost_usd=Decimal("0"),
            external_attempt_count=1,
            response_received=True,
            cleanup_diagnostic=("client_cleanup", "client_cleanup_failed"),
        )
    )
    completion_with_cleanup["cleanup_diagnostic"] = bad_cleanup
    with pytest.raises(StudyContractError, match="cleanup diagnostic"):
        _completion_from_metadata(completion_with_cleanup, "{}")

    context = _test_gateway_context(tmp_path / "provider-diagnostic", monkeypatch)
    _fixture, _fixture_request, _manifest, request, store, _grant, ledger, _gateway = context
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    ledger.persist_provider_diagnostic_for_pending_accounting(
        request,
        ProviderFailureDiagnosticV5(
            phase="transport",
            code="api_timeout",
            cleanup_diagnostic=("client_cleanup", "client_cleanup_failed"),
        ),
    )
    diagnostic_ref = store.list_refs(kind="provider-diagnostics")[0]
    original_read = store.read

    def tampered_diagnostic_read(reference):
        raw = original_read(reference)
        if reference == diagnostic_ref:
            payload = json.loads(raw)
            payload["cleanup_diagnostic"] = bad_cleanup
            return canonical_json_bytes_v5(payload)
        return raw

    monkeypatch.setattr(store, "read", tampered_diagnostic_read)
    with pytest.raises(StudyAuthorityError, match="provider diagnostic is invalid"):
        ledger._provider_diagnostic_records()
    monkeypatch.setattr(store, "read", original_read)

    observed_context = _test_gateway_context(tmp_path / "observed-response", monkeypatch)
    _fixture, _fixture_request, _manifest, request, store, _grant, ledger, _gateway = observed_context
    ledger.reserve(request)
    ledger.claim_dispatch(
        request_sha256=request.sha256,
        model=request.model,
        messages=request.messages,
        response_schema_json=request.schema_json,
        max_output_tokens=request.max_output_tokens,
    )
    ledger.persist_observed_response_for_pending_accounting(
        request,
        ProviderResponseAccountingErrorV5(
            response_text='{"private":"synthetic"}',
            provider_request_id=None,
            phase="response_accounting",
            code="inline_usage_missing",
            cleanup_diagnostic=("client_cleanup", "client_cleanup_failed"),
        ),
    )
    observation_ref = store.list_refs(kind="response-observations")[0]
    original_read = store.read

    def tampered_observation_read(reference):
        raw = original_read(reference)
        if reference == observation_ref:
            payload = json.loads(raw)
            payload["cleanup_diagnostic"] = bad_cleanup
            return canonical_json_bytes_v5(payload)
        return raw

    monkeypatch.setattr(store, "read", tampered_observation_read)
    with pytest.raises(StudyAuthorityError, match="response observation is invalid"):
        ledger._observed_response_records()
