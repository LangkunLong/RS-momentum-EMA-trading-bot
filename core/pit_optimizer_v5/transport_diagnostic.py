"""Safe callable diagnostic runner for the production V5 provider path.

The runner accepts an already-configured gateway and already-prepared request.
It does not load credentials, create study authority, or choose a provider
route. Study transport calls use the same single invocation helper below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sys
from typing import Callable, Mapping

from core.pit_optimizer_v5.provider import (
    CompletionResultV5,
    ProviderFailureDiagnosticV5,
    ProviderHttpObservationCollectorV1,
    ProviderHttpObservationV1,
    ProviderResponseAccountingErrorV5,
    _safe_http_identifier_v1,
)


_REQUEST_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")


@dataclass(frozen=True, slots=True)
class TransportDiagnosticReceiptV1:
    """Closed summary returned by one real production-adapter invocation."""

    outcome: str
    accepted: bool | None
    input_tokens: int | None
    output_tokens: int | None
    failure_phase: str | None
    failure_code: str | None
    observation: Mapping[str, object]
    request_sha256: str
    requested_model: str
    max_output_tokens: int
    authoritative_cost_usd: str | None = None
    accounting_status: str = "unavailable"
    response_received: bool | None = None
    cleanup_diagnostic: tuple[str, str] | None = None
    content_failure: str | None = None
    accounting_failure: str | None = None
    response_matches_expected: bool | None = None
    provider_request_id: str | None = None
    returned_model: str | None = None
    failure_http_status: int | None = None
    completion: CompletionResultV5 | None = field(default=None, repr=False)
    failure: ProviderFailureDiagnosticV5 | ProviderResponseAccountingErrorV5 | None = field(default=None, repr=False)

    def to_primitive(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "outcome": self.outcome,
            "accepted": self.accepted,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "failure_phase": self.failure_phase,
            "failure_code": self.failure_code,
            "request_sha256": self.request_sha256,
            "requested_model": self.requested_model,
            "max_output_tokens": self.max_output_tokens,
            "authoritative_cost_usd": self.authoritative_cost_usd,
            "accounting_status": self.accounting_status,
            "response_received": self.response_received,
            "cleanup_diagnostic": (
                None
                if self.cleanup_diagnostic is None
                else {"phase": self.cleanup_diagnostic[0], "code": self.cleanup_diagnostic[1]}
            ),
            "content_failure": self.content_failure,
            "accounting_failure": self.accounting_failure,
            "response_matches_expected": self.response_matches_expected,
            "provider_request_id": self.provider_request_id,
            "returned_model": self.returned_model,
            "failure_http_status": self.failure_http_status,
            "observation": dict(self.observation),
        }


def invoke_v5_json_once_observed(
    gateway: object,
    *,
    request_sha256: str,
    model: str,
    messages: tuple[Mapping[str, object], ...],
    response_schema_json: bytes,
    max_output_tokens: int,
    wall_deadline: float,
    allow_full_source_escape: bool | None = None,
    observer: ProviderHttpObservationCollectorV1,
) -> CompletionResultV5:
    """Invoke the actual one-shot OpenRouter adapter once with its observer."""

    method = getattr(gateway, "request_pit_optimizer_v5_json_once", None)
    if not callable(method) or type(observer) is not ProviderHttpObservationCollectorV1:
        raise TypeError("V5 observed transport inputs are invalid")
    result = method(
        request_sha256=request_sha256,
        model=model,
        messages=messages,
        response_schema_json=response_schema_json,
        max_output_tokens=max_output_tokens,
        wall_deadline=wall_deadline,
        allow_full_source_escape=allow_full_source_escape,
        http_observation=observer,
    )
    if type(result) is not CompletionResultV5:
        raise TypeError("V5 provider returned an invalid completion")
    return result


def run_transport_diagnostic_once_v1(
    gateway: object,
    *,
    request_sha256: str,
    model: str,
    messages: tuple[Mapping[str, object], ...],
    response_schema_json: bytes,
    max_output_tokens: int,
    wall_deadline: float,
    sink: Callable[[int, ProviderHttpObservationV1], object] | None = None,
    redactions: tuple[str, ...] = (),
    allow_full_source_escape: bool | None = None,
    expected_response_text: str | None = None,
) -> TransportDiagnosticReceiptV1:
    """Run one already-authorized request through the same production adapter.

    Only a closed summary is returned. Provider text and exception strings are
    never included in the diagnostic receipt.
    """

    if (
        type(request_sha256) is not str
        or _REQUEST_SHA_RE.fullmatch(request_sha256) is None
        or type(model) is not str
        or _SAFE_MODEL_RE.fullmatch(model) is None
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 1_000_000_000
        or (expected_response_text is not None and type(expected_response_text) is not str)
    ):
        raise ValueError("diagnostic request identity is invalid")

    # The diagnostic entry must exercise the same SDK-owned default client
    # construction used by the study path. Reject a borrowed/prebuilt client
    # before it can dispatch through an alternate transport.
    try:
        from agent_loop import OpenRouterGateway

        if (
            not isinstance(gateway, OpenRouterGateway)
            or getattr(gateway, "_client", None) is not None
            or getattr(gateway, "max_attempts", None) != 1
        ):
            raise ValueError("diagnostic requires a fresh one-shot OpenRouter gateway")
    except ImportError:
        raise ValueError("production OpenRouter gateway is unavailable") from None

    observer = ProviderHttpObservationCollectorV1(sink=sink, redactions=redactions)
    try:
        completion = invoke_v5_json_once_observed(
            gateway,
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
            wall_deadline=wall_deadline,
            allow_full_source_escape=allow_full_source_escape,
            observer=observer,
        )
        return TransportDiagnosticReceiptV1(
            outcome="completion_received",
            accepted=completion.accepted,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            failure_phase=None,
            failure_code=None,
            observation=observer.snapshot(),
            request_sha256=request_sha256,
            requested_model=model,
            max_output_tokens=max_output_tokens,
            authoritative_cost_usd=format(completion.cost_usd, "f"),
            accounting_status="complete",
            response_received=completion.response_received,
            cleanup_diagnostic=completion.cleanup_diagnostic,
            response_matches_expected=(
                None if expected_response_text is None else completion.response_text == expected_response_text
            ),
            provider_request_id=_safe_http_identifier_v1(
                completion.provider_request_id,
                redactions=observer.redactions,
            ),
            returned_model=_safe_http_identifier_v1(
                completion.returned_model,
                redactions=observer.redactions,
            ),
            completion=completion,
        )
    except (ProviderFailureDiagnosticV5, ProviderResponseAccountingErrorV5) as failure:
        if isinstance(failure, ProviderFailureDiagnosticV5):
            observer.note_failure(failure)
        return TransportDiagnosticReceiptV1(
            outcome="provider_failure" if isinstance(failure, ProviderFailureDiagnosticV5) else "accounting_failure",
            accepted=None,
            input_tokens=None,
            output_tokens=None,
            failure_phase=failure.phase,
            failure_code=failure.code,
            observation=observer.snapshot(),
            request_sha256=request_sha256,
            requested_model=model,
            max_output_tokens=max_output_tokens,
            accounting_status="pending",
            response_received=(True if isinstance(failure, ProviderResponseAccountingErrorV5) else None),
            cleanup_diagnostic=failure.cleanup_diagnostic,
            content_failure=(failure.content_failure if isinstance(failure, ProviderFailureDiagnosticV5) else None),
            accounting_failure=(
                failure.accounting_failure
                if isinstance(failure, ProviderFailureDiagnosticV5)
                else failure.code
            ),
            response_matches_expected=(
                None
                if expected_response_text is None
                else failure.response_text == expected_response_text
                if isinstance(failure, ProviderResponseAccountingErrorV5)
                else None
            ),
            provider_request_id=_safe_http_identifier_v1(
                failure.provider_request_id,
                redactions=observer.redactions,
            ),
            failure_http_status=(
                failure.http_status
                if isinstance(failure, ProviderFailureDiagnosticV5)
                and type(failure.http_status) is int
                and 100 <= failure.http_status <= 599
                else None
            ),
            failure=failure,
        )


def execute_study_arm_with_transport_receipt_v1(
    *,
    prepared,
    arm: str,
    ledger,
    gateway,
    receipt_sink: Callable[[bytes], object] | None = None,
    fallback_stdout: Callable[[str], object] | None = None,
):
    """Run the normal study-arm lifecycle and emit its final safe snapshot once.

    A failed optional observation sink is retained in the gateway's in-memory
    snapshot. The final receipt sink is attempted once; if it fails, one
    bounded JSON line is sent to stdout as a fallback. Receipt I/O errors never
    replace the study result or pending-accounting exception. The wrapped
    ``execute_study_arm_v1`` call runs once, preserving its normal reopen,
    verification, import, and runtime preparation flow.
    """

    if receipt_sink is not None and not callable(receipt_sink):
        raise ValueError("transport receipt sink is invalid")
    stdout_writer = fallback_stdout or sys.stdout.write
    if not callable(stdout_writer):
        raise ValueError("transport receipt fallback is invalid")
    from core.pit_optimizer_v5.two_round_study.driver import execute_study_arm_v1
    from core.pit_optimizer_v5.two_round_study.transport import StudyOpenRouterGatewayV1

    study_gateway = type(gateway) is StudyOpenRouterGatewayV1
    if study_gateway:
        # The same gateway may serve the next arm. Clear the prior call's
        # process-local snapshot so an early pre-dispatch failure cannot
        # misattribute the previous arm's HTTP evidence to this receipt.
        gateway.last_http_observation = None

    try:
        return execute_study_arm_v1(
            prepared=prepared,
            arm=arm,
            ledger=ledger,
            gateway=gateway,
        )
    finally:
        snapshot = (
            gateway.last_http_observation
            if study_gateway
            else None
        )
        receipt = {
            "schema_version": 1,
            "study_id": getattr(getattr(prepared, "manifest", None), "study_id", None),
            "arm": arm,
            "outcome": "raised" if sys.exc_info()[0] is not None else "returned",
            "observation": snapshot
            if isinstance(snapshot, Mapping)
            else {"capture_status": "not_available"},
        }
        try:
            encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
            receipt_bytes = encoded.encode("utf-8")
        except BaseException:
            receipt_bytes = json.dumps(
                {
                    "schema_version": 1,
                    "study_id": getattr(getattr(prepared, "manifest", None), "study_id", None),
                    "arm": arm,
                    "outcome": "receipt_serialization_failed",
                    "observation": {"capture_status": "not_available"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = receipt_bytes.decode("utf-8")
        try:
            if receipt_sink is None:
                stdout_writer(encoded + "\n")
            else:
                receipt_sink(receipt_bytes)
        except BaseException:
            try:
                stdout_writer(encoded + "\n")
            except BaseException:
                pass


__all__ = [
    "TransportDiagnosticReceiptV1",
    "invoke_v5_json_once_observed",
    "run_transport_diagnostic_once_v1",
    "execute_study_arm_with_transport_receipt_v1",
]
