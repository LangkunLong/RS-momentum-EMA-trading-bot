from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import site
import ssl
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

import agent_loop
from core.pit_optimizer_v5.artifacts import ArtifactRefV5
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
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
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
    assert diagnostic["content_failure"] == "choice_structure_invalid"
    assert diagnostic["accounting_failure"] == "inline_usage_missing"
    assert diagnostic["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    assert store.list_refs(kind="response-observations") == ()
    assert store.list_refs(kind="observed-raw-responses") == ()


@pytest.mark.parametrize(
    ("content_shape", "usage_shape", "expected_content_failure", "expected_accounting_failure"),
    (
        ("missing", "missing", "content_missing", "inline_usage_missing"),
        ("non_string", "invalid_total", "content_non_string", "inline_total_mismatch"),
        ("accessor", "accessor", "content_accessor_failed", "response_accounting_failed"),
    ),
)
def test_dual_response_extraction_and_accounting_failures_keep_bounded_categories(
    tmp_path,
    monkeypatch,
    content_shape,
    usage_shape,
    expected_content_failure,
    expected_accounting_failure,
) -> None:
    pair = _install_external_network_block(monkeypatch)
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, _grant, _ledger, gateway = context
    requests = []

    def handler(http_request):
        requests.append(http_request)
        body = _sdk_body(request, _fixture_request, with_usage=usage_shape != "missing")
        message = body["choices"][0]["message"]
        if content_shape == "missing":
            message.pop("content")
        elif content_shape == "non_string":
            message["content"] = None
        if usage_shape == "invalid_total":
            body["usage"]["total_tokens"] -= 1
        return httpx.Response(200, headers={"x-request-id": "http-request-id-distinct"}, json=body)

    if content_shape == "missing":
        # The SDK model normalizes omitted nullable `content` to None. Inject
        # an absent post-SDK field here to test the adapter's distinct missing
        # branch without claiming the SDK preserves the wire distinction.
        present_field = agent_loop._present_field

        def missing_sdk_content(value, name):
            if name == "content":
                return agent_loop._MISSING_FIELD
            return present_field(value, name)

        monkeypatch.setattr(agent_loop, "_present_field", missing_sdk_content)
    elif content_shape == "accessor":
        read_field = agent_loop._read_field

        def fail_choices_accessor(value, *path):
            if path in {("choices",), ("usage",)}:
                raise RuntimeError("sensitive response accessor detail")
            return read_field(value, *path)

        monkeypatch.setattr(agent_loop, "_read_field", fail_choices_accessor)

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
        "response_content_unavailable",
    )
    assert diagnostic["content_failure"] == expected_content_failure
    assert diagnostic["accounting_failure"] == expected_accounting_failure
    assert diagnostic["provider_request_id"] == "sdk-response-id-safe"
    http_observation = next(
        json.loads(store.read(ref))
        for ref in store.list_refs(kind="transport-observations")
        if ref.relative_path.endswith("-payload.bin")
    )
    assert http_observation["observation"]["http_request_id"] == "http-request-id-distinct"
    assert http_observation["observation"]["response_id"] == "sdk-response-id-safe"
    assert store.list_refs(kind="responses") == ()
    assert store.list_refs(kind="raw-responses") == ()
    assert store.list_refs(kind="response-observations") == ()
    assert store.list_refs(kind="observed-raw-responses") == ()
    _assert_no_sensitive_artifact_bytes(store.repository.root, b"sensitive response accessor detail")
    reopened = StudyLedgerV1(store, manifest, _grant, approval=None)
    with pytest.raises(StudyPendingAccounting, match="no authenticated provider usage"):
        reopened.recover(request)


@pytest.mark.parametrize("mutation", ("unknown_content", "missing_accounting", "null_categories"))
def test_persisted_dual_failure_categories_reject_malformed_envelopes(
    tmp_path, monkeypatch, mutation
) -> None:
    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, request, store, grant, ledger, _gateway = context
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
            phase="response_extraction",
            code="response_content_unavailable",
            provider_request_id="safe-dual-failure-id",
            content_failure="content_non_string",
            accounting_failure="inline_usage_missing",
        ),
    )
    source_ref = store.list_refs(kind="provider-diagnostics")[0]
    payload = json.loads(store.read(source_ref))
    if mutation == "unknown_content":
        payload["content_failure"] = "raw accessor exception"
    elif mutation == "missing_accounting":
        payload.pop("accounting_failure")
    else:
        payload["content_failure"] = None
        payload["accounting_failure"] = None
    forged_bytes = canonical_json_bytes_v5(payload)
    forged_ref = ArtifactRefV5(source_ref.relative_path, hashlib.sha256(forged_bytes).hexdigest())
    reader = StudyLedgerV1(store, manifest, grant, approval=None)
    original_refs = StudyLedgerV1._refs
    original_read = StudyStoreV1.read

    def forged_refs(self, kind):
        if self is reader and kind == "provider-diagnostics":
            return (forged_ref,)
        return original_refs(self, kind)

    def forged_read(self, ref):
        if self is store and ref == forged_ref:
            return forged_bytes
        return original_read(self, ref)

    monkeypatch.setattr(StudyLedgerV1, "_refs", forged_refs)
    monkeypatch.setattr(StudyStoreV1, "read", forged_read)
    with pytest.raises(StudyAuthorityError):
        reader._provider_diagnostic_records()


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
                # Fresh reopen uses the accepted interpreter below and this
                # synthetic explicit user-site base, never inherited profile
                # or environment values.
                "PYTHONUSERBASE": site.USER_BASE,
                "V5_TRANSPORT_GUARD_INSTALLED_MARKER": str(installed_marker),
                "V5_TRANSPORT_TRIPWIRE_MARKER": str(tripwire_marker),
            }
        )
        kwargs["env"] = child_env
        kwargs["timeout"] = 30
        started = time.monotonic()
        record("fresh_reopen_child_start", child=child_index)
        try:
            # Validate the production launcher request above, then run
            # its same -c program with the pinned accepted interpreter.
            # The production py launcher itself remains outside this
            # offline test's credential/network surface.
            accepted_child_command = [sys.executable, *command[2:]]
            result = original_run(accepted_child_command, *positional, **kwargs)
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
    transport_refs = store.list_refs(kind="transport-observations")
    assert transport_refs
    for transport_ref in transport_refs:
        transport_bytes = store.read(transport_ref)
        matching_transport = [
            item for item in index["artifacts"] if item["source_sha256"] == transport_ref.sha256
        ]
        assert len(matching_transport) == 1
        exported_transport = output / Path(*str(matching_transport[0]["relative_path"]).split("/"))
        assert _long_read_bytes(exported_transport) == transport_bytes
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
    monkeypatch.setattr(store, "read", original_read)


def _install_external_network_block_many(monkeypatch, count: int):
    pairs = [socket.socketpair() for _ in range(count)]
    available_pairs = list(pairs)

    def reserved_socketpair(*_args, **_kwargs):
        if not available_pairs:
            raise AssertionError("unexpected event-loop socketpair")
        return available_pairs.pop(0)

    def blocked(*_args, **_kwargs):
        raise AssertionError("external networking and DNS are blocked in this test")

    monkeypatch.setattr(socket, "socketpair", reserved_socketpair)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    return pairs


def _set_synthetic_sdk_environment(monkeypatch):
    synthetic_environment = {
        "PYTHON_DOTENV_DISABLED": "1",
        "OPENAI_WEBHOOK_SECRET": "synthetic-test-only-webhook-secret",
        "OPENAI_API_KEY": "synthetic-test-only-openai-key",
        "OPENROUTER_API_KEY": "synthetic-test-only-credential",
        "V5_TRANSPORT_TEST_TOKEN": "synthetic-test-only-credential",
        "HTTPS_PROXY": "http://127.0.0.1:18881",
        "HTTP_PROXY": "http://127.0.0.1:18882",
        "ALL_PROXY": "http://127.0.0.1:18883",
        "NO_PROXY": "",
        "https_proxy": "http://127.0.0.1:18881",
        "http_proxy": "http://127.0.0.1:18882",
        "all_proxy": "http://127.0.0.1:18883",
        "no_proxy": "",
    }
    monkeypatch.setattr(os, "environ", synthetic_environment)
    return synthetic_environment


def _install_default_sdk_transport_mock(monkeypatch, handler, *, close_fails: bool = False):
    """Patch the SDK-created transports only after default client construction."""

    # Replace the mapping without copying, enumerating, or querying the real
    # process environment. SDK and HTTPX constructor reads see only synthetic
    # test values, including when they consult optional environment settings.
    synthetic_environment = _set_synthetic_sdk_environment(monkeypatch)

    created = []
    requests = []
    transport_ids = set()
    real_constructor = openai.AsyncOpenAI

    if close_fails:
        class ClosingFailureClient(real_constructor):
            async def close(self) -> None:
                await super().close()
                raise RuntimeError("synthetic sensitive cleanup detail")

        client_type = ClosingFailureClient
    else:
        client_type = real_constructor

    def construct(**kwargs):
        assert os.environ is synthetic_environment
        assert "http_client" not in kwargs
        client = client_type(**kwargs)
        http_client = client._client
        transports = {http_client._transport}
        transports.update(transport for transport in http_client._mounts.values() if transport is not None)
        transport_ids.update(id(transport) for transport in transports)
        timeout = http_client.timeout
        pools = tuple(getattr(transport, "_pool", None) for transport in transports)
        tls_verify_modes = tuple(
            getattr(getattr(pool, "_ssl_context", None), "verify_mode", None)
            for pool in pools
        )
        created.append(
            {
                "client": client,
                "http_client": http_client,
                "default_transport": http_client._transport,
                "mounts": tuple(transport for transport in http_client._mounts.values() if transport is not None),
                "trust_env": http_client._trust_env,
                "response_hooks": tuple(http_client.event_hooks.get("response", ())),
                "settings": {
                    "configured_timeout": kwargs.get("timeout"),
                    "timeout": tuple(
                        (name, getattr(timeout, name, None))
                        for name in ("connect", "read", "write", "pool")
                    ),
                    "max_retries": client.max_retries,
                    "follow_redirects": http_client.follow_redirects,
                    "limits": tuple(
                        (
                            getattr(pool, "_max_connections", None),
                            getattr(pool, "_max_keepalive_connections", None),
                        )
                        for pool in pools
                    ),
                    "tls_verify_modes": tls_verify_modes,
                },
            }
        )

        for transport_type in {type(transport) for transport in transports}:
            original = transport_type.handle_async_request

            async def intercepted(transport, request, _original=original):
                if id(transport) in transport_ids:
                    requests.append(request)
                    response = handler(request)
                    if asyncio.iscoroutine(response):
                        response = await response
                    return response
                return await _original(transport, request)

            monkeypatch.setattr(transport_type, "handle_async_request", intercepted)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", construct)
    return created, requests


def _direct_v5_request(gateway, *, observer=None):
    return gateway.request_pit_optimizer_v5_json_once(
        request_sha256=hashlib.sha256(b"synthetic-v5-diagnostic-request").hexdigest(),
        model="openai/gpt-5.4",
        messages=({"role": "user", "content": {"probe": "synthetic diagnostic"}},),
        response_schema_json=(
            b'{"type":"object","properties":{"status":{"type":"string"}},'
            b'"required":["status"],"additionalProperties":false}'
        ),
        max_output_tokens=32,
        wall_deadline=time.monotonic() + 15.0,
        allow_full_source_escape=False,
        http_observation=observer,
    )


def _gateway_for_default_sdk_test():
    return agent_loop.OpenRouterGateway(
        api_key="synthetic-test-only-credential",
        run_id="integrated-transport-test",
        timeout_seconds=10.0,
        max_attempts=1,
    )


def _minimal_sdk_completion_body(*, response_id: str = "body-generation-id-safe"):
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": 1,
        "model": "openai/gpt-5.4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"status":"ok"}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7, "cost": 0.000123},
    }


def test_actual_study_gateway_uses_same_sdk_owned_client_settings_as_diagnostic(
    tmp_path, monkeypatch
) -> None:
    from core.pit_optimizer_v5.transport_diagnostic import run_transport_diagnostic_once_v1

    _set_synthetic_sdk_environment(monkeypatch)
    pairs = _install_external_network_block_many(monkeypatch, 2)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, ledger, _unused_gateway = context
    study_gateway = StudyOpenRouterGatewayV1(
        ledger=ledger,
        credential_environment_variable="V5_TRANSPORT_TEST_TOKEN",
        timeout_seconds=10.0,
    )

    def test_only_admission(*, request_sha256, model, messages, response_schema_json, max_output_tokens):
        return ledger.claim_dispatch(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )

    study_gateway._admitted = test_only_admission
    requests = []

    def handler(http_request):
        requests.append(http_request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"x-request-id": "diagnostic-http-id"},
                json=_minimal_sdk_completion_body(response_id="diagnostic-body-id"),
                request=http_request,
            )
        return httpx.Response(
            200,
            headers={"x-request-id": "study-http-id"},
            json=_sdk_body(request, fixture_request),
            request=http_request,
        )

    created, intercepted_requests = _install_default_sdk_transport_mock(monkeypatch, handler)
    try:
        diagnostic = run_transport_diagnostic_once_v1(
            _gateway_for_default_sdk_test(),
            request_sha256=hashlib.sha256(b"synthetic-v5-diagnostic-equivalence").hexdigest(),
            model="openai/gpt-5.4",
            messages=({"role": "user", "content": {"probe": "synthetic diagnostic"}},),
            response_schema_json=(
                b'{"type":"object","properties":{"status":{"type":"string"}},'
                b'"required":["status"],"additionalProperties":false}'
            ),
            max_output_tokens=32,
            wall_deadline=time.monotonic() + 15.0,
            redactions=("synthetic-test-only-credential", "synthetic-test-only-webhook-secret"),
            allow_full_source_escape=False,
        )
        assert diagnostic.outcome == "completion_received", (
            diagnostic.failure_phase,
            diagnostic.failure_code,
            diagnostic.observation,
            len(intercepted_requests),
            len(requests),
        )
        terminal = run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=study_gateway,
            deadline_monotonic=time.monotonic() + 60.0,
        )
    finally:
        for pair in pairs:
            pair[0].close()
            pair[1].close()

    assert terminal.terminal.usage.input_tokens == 7
    assert terminal.terminal.usage.output_tokens == 5
    assert len(created) == len(intercepted_requests) == len(requests) == 2
    first, second = created
    for item in created:
        assert item["trust_env"] is True
        assert item["default_transport"] is item["http_client"]._transport
        assert item["mounts"]
        assert item["settings"]["max_retries"] == 0
        assert item["settings"]["follow_redirects"] == first["settings"]["follow_redirects"]
        assert item["settings"]["tls_verify_modes"]
        assert all(mode == ssl.CERT_REQUIRED for mode in item["settings"]["tls_verify_modes"])
    assert first["settings"] == second["settings"]
    assert first["trust_env"] == second["trust_env"] is True
    assert len(first["mounts"]) == len(second["mounts"])
    assert study_gateway.last_http_observation["capture_status"] == "observed"
    observation_refs = store.list_refs(kind="transport-observations")
    assert len(observation_refs) == 2
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reopened.recover(request) == terminal


def test_study_observation_sink_failure_keeps_accounting_and_final_snapshot(tmp_path, monkeypatch) -> None:
    _set_synthetic_sdk_environment(monkeypatch)
    pairs = _install_external_network_block_many(monkeypatch, 1)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, ledger, _unused_gateway = context
    gateway = StudyOpenRouterGatewayV1(
        ledger=ledger,
        credential_environment_variable="V5_TRANSPORT_TEST_TOKEN",
        timeout_seconds=10.0,
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
    original_persist = ledger.persist_transport_observation_for_request

    def fail_optional_observation(*_args, **_kwargs):
        raise OSError("synthetic observation sink interruption")

    monkeypatch.setattr(ledger, "persist_transport_observation_for_request", fail_optional_observation)
    created, requests = _install_default_sdk_transport_mock(
        monkeypatch,
        lambda http_request: httpx.Response(
            200,
            json=_sdk_body(request, fixture_request),
            request=http_request,
        ),
    )
    try:
        terminal = run_study_call_v1(
            request=request,
            fixture_request=fixture_request,
            ledger=ledger,
            gateway=gateway,
            deadline_monotonic=time.monotonic() + 60.0,
        )
    finally:
        for pair in pairs:
            pair[0].close()
            pair[1].close()

    assert len(created) == len(requests) == 1
    assert terminal.terminal.usage.input_tokens == 7
    assert terminal.terminal.usage.output_tokens == 5
    assert gateway.last_http_observation["persistence_failures"] == 2
    assert gateway.last_http_observation["capture_status"] == "observed"
    assert store.list_refs(kind="transport-observations") == ()
    monkeypatch.setattr(ledger, "persist_transport_observation_for_request", original_persist)
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reopened.recover(request) == terminal


def test_execute_study_arm_transport_receipt_wraps_once_and_falls_back_safely(
    tmp_path, monkeypatch
) -> None:
    from core.pit_optimizer_v5.transport_diagnostic import (
        execute_study_arm_with_transport_receipt_v1,
    )

    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, _manifest, _request, _store, _grant, _ledger, gateway = context
    gateway.last_http_observation = {
        "capture_status": "observed",
        "persistence_failures": 2,
        "observations": [{"stage": "headers", "http_status": 200}],
    }
    prepared = SimpleNamespace(manifest=SimpleNamespace(study_id="synthetic-study-id"))
    sentinel = object()
    calls = []

    def execute_once(**kwargs):
        calls.append(kwargs)
        gateway.last_http_observation = {
            "capture_status": "observed",
            "persistence_failures": 2,
            "observations": [{"stage": "headers", "http_status": 200}],
        }
        return sentinel

    import core.pit_optimizer_v5.two_round_study.driver as driver_module

    monkeypatch.setattr(driver_module, "execute_study_arm_v1", execute_once)
    fallbacks = []

    def interrupted_receipt_sink(_raw):
        raise OSError("synthetic receipt sink interruption")

    result = execute_study_arm_with_transport_receipt_v1(
        prepared=prepared,
        arm="primary",
        ledger=context[6],
        gateway=gateway,
        receipt_sink=interrupted_receipt_sink,
        fallback_stdout=fallbacks.append,
    )

    assert result is sentinel
    assert len(calls) == 1
    assert calls[0] == {
        "prepared": prepared,
        "arm": "primary",
        "ledger": context[6],
        "gateway": gateway,
    }
    assert len(fallbacks) == 1
    assert fallbacks[0].endswith("\n")
    receipt = json.loads(fallbacks[0])
    assert receipt == {
        "schema_version": 1,
        "study_id": "synthetic-study-id",
        "arm": "primary",
        "outcome": "returned",
        "observation": gateway.last_http_observation,
    }


def test_execute_study_arm_receipt_does_not_reuse_prior_gateway_observation(
    tmp_path, monkeypatch
) -> None:
    from core.pit_optimizer_v5.transport_diagnostic import (
        execute_study_arm_with_transport_receipt_v1,
    )

    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, _manifest, _request, _store, _grant, ledger, gateway = context
    gateway.last_http_observation = {
        "capture_status": "observed",
        "persistence_failures": 0,
        "observations": [{"stage": "payload", "http_status": 200}],
    }
    prepared = SimpleNamespace(manifest=SimpleNamespace(study_id="synthetic-study-id"))
    captured = []

    def fail_before_dispatch(**_kwargs):
        raise StudyAuthorityError("synthetic local pre-dispatch rejection")

    import core.pit_optimizer_v5.two_round_study.driver as driver_module

    monkeypatch.setattr(driver_module, "execute_study_arm_v1", fail_before_dispatch)
    with pytest.raises(StudyAuthorityError):
        execute_study_arm_with_transport_receipt_v1(
            prepared=prepared,
            arm="withheld",
            ledger=ledger,
            gateway=gateway,
            receipt_sink=captured.append,
        )

    assert len(captured) == 1
    receipt = json.loads(captured[0])
    assert receipt["outcome"] == "raised"
    assert receipt["observation"] == {"capture_status": "not_available"}
    assert gateway.last_http_observation is None


def test_default_sdk_owned_transport_callable_runner_returns_authoritative_receipt(tmp_path, monkeypatch) -> None:
    from core.pit_optimizer_v5.transport_diagnostic import run_transport_diagnostic_once_v1

    pairs = _install_external_network_block_many(monkeypatch, 1)
    created, requests = _install_default_sdk_transport_mock(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"x-request-id": "http-request-id-safe"},
            json=_minimal_sdk_completion_body(),
            request=request,
        ),
    )
    gateway = _gateway_for_default_sdk_test()
    receipt = run_transport_diagnostic_once_v1(
        gateway,
        request_sha256=hashlib.sha256(b"synthetic-v5-diagnostic-request").hexdigest(),
        model="openai/gpt-5.4",
        messages=({"role": "user", "content": {"probe": "synthetic diagnostic"}},),
        response_schema_json=(
            b'{"type":"object","properties":{"status":{"type":"string"}},'
            b'"required":["status"],"additionalProperties":false}'
        ),
        max_output_tokens=32,
        wall_deadline=time.monotonic() + 15.0,
        redactions=("synthetic-test-only-credential", "synthetic-test-only-webhook-secret"),
        expected_response_text='{"status":"ok"}',
        allow_full_source_escape=False,
    )
    assert len(created) == len(requests) == 1
    assert created[0]["trust_env"] is True
    assert created[0]["default_transport"] is created[0]["http_client"]._transport
    assert created[0]["mounts"]
    assert created[0]["client"].max_retries == 0
    assert created[0]["response_hooks"] == tuple(created[0]["http_client"].event_hooks.get("response", ()))
    assert receipt.outcome == "completion_received"
    assert receipt.authoritative_cost_usd == "0.000123"
    assert receipt.accounting_status == "complete"
    assert receipt.response_matches_expected is True
    assert receipt.completion is not None
    assert receipt.completion.response_text == '{"status":"ok"}'
    assert receipt.completion.provider_request_id == "body-generation-id-safe"
    assert receipt.completion.returned_model == "openai/gpt-5.4"
    assert receipt.completion.response_received is True
    assert receipt.observation["capture_status"] == "observed"
    observed = receipt.observation["observations"]
    assert [item["stage"] for item in observed] == ["headers", "payload"]
    payload_observation = observed[-1]
    assert payload_observation["http_request_id"] == "http-request-id-safe"
    assert payload_observation["response_id"] == "body-generation-id-safe"
    encoded_receipt = json.dumps(receipt.to_primitive(), sort_keys=True)
    assert "synthetic-test-only-credential" not in encoded_receipt
    assert "synthetic-test-only-webhook-secret" not in encoded_receipt
    assert '{"status":"ok"}' not in encoded_receipt
    for pair in pairs:
        pair[0].close()
        pair[1].close()


def test_callable_runner_preserves_dual_failure_categories_and_cleanup(tmp_path, monkeypatch) -> None:
    from core.pit_optimizer_v5.transport_diagnostic import run_transport_diagnostic_once_v1

    pairs = _install_external_network_block_many(monkeypatch, 1)
    body = _minimal_sdk_completion_body()
    body["choices"][0]["message"].pop("content")
    body["usage"].pop("cost")
    created, requests = _install_default_sdk_transport_mock(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"x-request-id": "http-request-id-dual"},
            json=body,
            request=request,
        ),
        close_fails=True,
    )
    gateway = _gateway_for_default_sdk_test()
    receipt = run_transport_diagnostic_once_v1(
        gateway,
        request_sha256=hashlib.sha256(b"synthetic-v5-dual-failure").hexdigest(),
        model="openai/gpt-5.4",
        messages=({"role": "user", "content": {"probe": "synthetic diagnostic"}},),
        response_schema_json=(
            b'{"type":"object","properties":{"status":{"type":"string"}},'
            b'"required":["status"],"additionalProperties":false}'
        ),
        max_output_tokens=32,
        wall_deadline=time.monotonic() + 15.0,
        redactions=("synthetic-test-only-credential", "synthetic sensitive cleanup detail"),
        allow_full_source_escape=False,
    )
    assert len(created) == len(requests) == 1
    assert receipt.outcome == "provider_failure"
    assert receipt.accounting_status == "pending"
    assert receipt.failure_phase == "response_extraction"
    assert receipt.failure_code == "response_content_unavailable"
    assert receipt.content_failure == "content_non_string"
    assert receipt.accounting_failure == "inline_usage_missing"
    assert receipt.cleanup_diagnostic == ("client_cleanup", "client_cleanup_failed")
    assert receipt.failure is not None
    primitive = receipt.to_primitive()
    assert primitive["content_failure"] == "content_non_string"
    assert primitive["accounting_failure"] == "inline_usage_missing"
    assert primitive["cleanup_diagnostic"] == {
        "phase": "client_cleanup",
        "code": "client_cleanup_failed",
    }
    encoded = json.dumps(primitive, sort_keys=True)
    assert "synthetic sensitive cleanup detail" not in encoded
    assert "synthetic-test-only-credential" not in encoded
    for pair in pairs:
        pair[0].close()
        pair[1].close()


@pytest.mark.parametrize("case", ("connection", "partial_read_timeout"))
def test_default_sdk_observer_on_off_preserves_failure_category(case, monkeypatch) -> None:
    from core.pit_optimizer_v5.provider import ProviderHttpObservationCollectorV1

    pairs = _install_external_network_block_many(monkeypatch, 2)
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        if case == "connection":
            raise httpx.ConnectError("synthetic connect detail", request=request)

        class PartialTimeoutStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"id":"partial-safe",'
                raise httpx.ReadTimeout("synthetic partial body timeout", request=request)

            async def aclose(self):
                return None

        return httpx.Response(200, stream=PartialTimeoutStream(), request=request)

    created, requests = _install_default_sdk_transport_mock(monkeypatch, handler)
    outcomes = []
    snapshots = []
    for observed in (False, True):
        gateway = _gateway_for_default_sdk_test()
        collector = ProviderHttpObservationCollectorV1() if observed else None
        try:
            _direct_v5_request(gateway, observer=collector)
        except ProviderFailureDiagnosticV5 as diagnostic:
            outcomes.append(("provider_failure", diagnostic.phase, diagnostic.code))
        except ProviderResponseAccountingErrorV5 as diagnostic:
            outcomes.append(("accounting_failure", diagnostic.phase, diagnostic.code))
        else:
            outcomes.append(("completion",))
        snapshots.append(None if collector is None else collector.snapshot())
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == "provider_failure"
    assert len(created) == 2
    assert len(requests) == call_count == 2
    assert all(item["trust_env"] is True for item in created)
    assert all(item["client"].max_retries == 0 for item in created)
    assert all(item["response_hooks"] == tuple(item["http_client"].event_hooks.get("response", ())) for item in created)
    if case == "connection":
        assert snapshots[1]["capture_status"] == "no_http_response_observed"
        assert snapshots[1]["observations"][0]["stage"] == "no_response"
    else:
        assert snapshots[1]["capture_status"] == "observed"
        assert [item["stage"] for item in snapshots[1]["observations"]] == ["headers"]
    snapshot_json = json.dumps(snapshots[1], sort_keys=True)
    assert "synthetic connect detail" not in snapshot_json
    assert "synthetic partial body timeout" not in snapshot_json
    for pair in pairs:
        pair[0].close()
        pair[1].close()


@pytest.mark.parametrize("case", ("missing_content", "incomplete_accounting", "dual_failure", "malformed_finish_reason"))
def test_default_sdk_observer_on_off_preserves_response_extraction_outcomes(case, monkeypatch) -> None:
    from core.pit_optimizer_v5.provider import ProviderHttpObservationCollectorV1

    pairs = _install_external_network_block_many(monkeypatch, 2)

    def handler(request):
        body = _minimal_sdk_completion_body()
        if case in {"missing_content", "dual_failure"}:
            body["choices"][0]["message"].pop("content")
        if case in {"incomplete_accounting", "dual_failure"}:
            body["usage"].pop("cost")
        if case == "malformed_finish_reason":
            body["choices"][0]["finish_reason"] = {"unexpected": "shape"}
        return httpx.Response(200, json=body, request=request)

    created, requests = _install_default_sdk_transport_mock(monkeypatch, handler)
    outcomes = []
    snapshots = []
    for observed in (False, True):
        gateway = _gateway_for_default_sdk_test()
        collector = ProviderHttpObservationCollectorV1() if observed else None
        try:
            completion = _direct_v5_request(gateway, observer=collector)
            outcomes.append(("completion", completion.accepted, completion.response_text, completion.cost_usd))
        except ProviderFailureDiagnosticV5 as diagnostic:
            outcomes.append(
                (
                    "provider_failure",
                    diagnostic.phase,
                    diagnostic.code,
                    diagnostic.content_failure,
                    diagnostic.accounting_failure,
                )
            )
        except ProviderResponseAccountingErrorV5 as diagnostic:
            outcomes.append(("accounting_failure", diagnostic.phase, diagnostic.code, diagnostic.response_text))
        snapshots.append(None if collector is None else collector.snapshot())
    assert outcomes[0] == outcomes[1]
    assert len(created) == len(requests) == 2
    assert all(item["trust_env"] is True for item in created)
    assert all(item["client"].max_retries == 0 for item in created)
    if case == "missing_content":
        assert outcomes[0][0:3] == ("completion", False, "")
    elif case == "incomplete_accounting":
        assert outcomes[0][:3] == ("accounting_failure", "response_accounting", "inline_usage_missing")
    elif case == "dual_failure":
        assert outcomes[0] == (
            "provider_failure",
            "response_extraction",
            "response_content_unavailable",
            "content_non_string",
            "inline_usage_missing",
        )
    else:
        # OpenAI 2.54.0 normalizes an unexpected finish_reason object to its
        # nullable field instead of rejecting the otherwise valid response.
        assert outcomes[0][0] == "completion"
        assert outcomes[0][1] is False
        assert outcomes[0][2] == '{"status":"ok"}'
        assert snapshots[1]["observations"][-1]["response_id"] == "body-generation-id-safe"
        assert snapshots[1]["observations"][-1]["prompt_tokens"] == 4
        assert snapshots[1]["observations"][-1]["finish_reasons"] == ["missing"]
    assert snapshots[1]["observations"][-1]["response_id"] == "body-generation-id-safe"
    for pair in pairs:
        pair[0].close()
        pair[1].close()


def test_atomic_transport_sidecar_fault_before_publication_is_ignored_on_reopen(tmp_path, monkeypatch) -> None:
    import core.pit_optimizer_v5.artifacts as artifacts_module

    context = _test_gateway_context(tmp_path, monkeypatch)
    _fixture, _fixture_request, manifest, _request, store, grant, _ledger, _gateway = context
    original_publish = artifacts_module._publish_create_only_link

    def interrupted_before_publication(*_args, **_kwargs):
        raise OSError("synthetic interruption before sidecar publication")

    monkeypatch.setattr(artifacts_module, "_publish_create_only_link", interrupted_before_publication)
    with pytest.raises(StudyAuthorityError):
        store.put_atomic(
            kind="transport-observations",
            key=f"{'a' * 64}-00-headers",
            content=b'{"synthetic":"partial-stage"}',
        )
    monkeypatch.setattr(artifacts_module, "_publish_create_only_link", original_publish)
    assert store.list_refs(kind="transport-observations") == ()
    reopened = StudyLedgerV1(store, manifest, grant, approval=None)
    assert reopened.manifest == manifest
    stage_namespace = "staging-study-v1-transport-observations"
    store.repository.append_binary_state(namespace=stage_namespace, key="interrupted", content=b'{"partial":')
    assert StudyLedgerV1(store, manifest, grant, approval=None).manifest == manifest


def test_sdk_owned_redirect_intermediate_id_remains_advisory_to_completion_identity(
    tmp_path, monkeypatch
) -> None:
    _set_synthetic_sdk_environment(monkeypatch)
    pairs = _install_external_network_block_many(monkeypatch, 1)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture_request, request, manifest, store, grant, gateway = (
        context[1], context[3], context[2], context[4], context[5], context[7]
    )
    requests = []

    def handler(http_request):
        requests.append(http_request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "Location": "https://openrouter.ai/api/v1/chat/completions?redirected=1",
                    "x-request-id": "redirect-http-id",
                },
                json={"id": "redirect-generation-id"},
                request=http_request,
            )
        return httpx.Response(
            200,
            headers={"x-request-id": "final-http-id"},
            json=_sdk_body(request, fixture_request),
            request=http_request,
        )

    created, intercepted = _install_default_sdk_transport_mock(monkeypatch, handler)
    try:
        terminal = _run(context, gateway)
    finally:
        for pair in pairs:
            pair[0].close()
            pair[1].close()
    assert len(created) == 1
    assert len(requests) == len(intercepted) == 2
    assert created[0]["settings"]["follow_redirects"] is True
    assert created[0]["client"].max_retries == 0
    assert terminal.terminal.usage.input_tokens == 7
    payloads = [
        json.loads(store.read(ref))
        for ref in store.list_refs(kind="transport-observations")
        if ref.relative_path.endswith("-payload.bin")
    ]
    assert [item["exchange_index"] for item in payloads] == [0, 1]
    assert [item["observation"]["response_id"] for item in payloads] == [
        "redirect-generation-id",
        "sdk-response-id-safe",
    ]
    assert [item["observation"]["http_request_id"] for item in payloads] == [
        "redirect-http-id",
        "final-http-id",
    ]
    assert StudyLedgerV1(store, manifest, grant, approval=None).recover(request) == terminal


def test_distinct_http_and_generation_ids_remain_advisory_to_completion_identity(
    tmp_path, monkeypatch
) -> None:
    _set_synthetic_sdk_environment(monkeypatch)
    pairs = _install_external_network_block_many(monkeypatch, 1)
    context = _test_gateway_context(tmp_path, monkeypatch)
    fixture, fixture_request, manifest, request, store, grant, ledger, gateway = context
    created, requests = _install_default_sdk_transport_mock(
        monkeypatch,
        lambda http_request: httpx.Response(
            200,
            headers={"x-request-id": "http-request-id-domain"},
            json=_sdk_body(request, fixture_request),
            request=http_request,
        ),
    )
    try:
        terminal = _run(context, gateway)
    finally:
        for pair in pairs:
            pair[0].close()
            pair[1].close()
    assert len(requests) == len(created) == 1
    assert created[0]["trust_env"] is True
    assert created[0]["default_transport"] is created[0]["http_client"]._transport
    assert terminal.terminal.usage.input_tokens == 7
    transport_payloads = [
        json.loads(store.read(ref))
        for ref in store.list_refs(kind="transport-observations")
        if ref.relative_path.endswith("-payload.bin")
    ]
    assert [item["exchange_index"] for item in transport_payloads] == [0]
    assert [item["observation"]["response_id"] for item in transport_payloads] == ["sdk-response-id-safe"]
    assert [item["observation"]["http_request_id"] for item in transport_payloads] == [
        "http-request-id-domain"
    ]
    assert StudyLedgerV1(store, manifest, grant, approval=None).recover(request) == terminal


def test_transport_observation_invalid_decimal_is_a_value_error() -> None:
    from core.pit_optimizer_v5.provider import ProviderHttpObservationV1

    valid = ProviderHttpObservationV1(
        stage="payload",
        http_status=200,
        http_request_id=None,
        response_id=None,
        returned_model=None,
        choice_count=0,
        finish_reasons=(),
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cost_usd=None,
        payload_length=0,
        payload_sha256=None,
        projection_state="captured",
        error_category="none",
    )
    invalid = valid.to_primitive()
    invalid["cost_usd"] = "not-a-decimal"
    with pytest.raises(ValueError, match="provider HTTP observation cost is invalid"):
        ProviderHttpObservationV1.from_primitive(invalid)
