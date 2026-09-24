"""Study-bound one-shot OpenRouter transport.

Construction and verification are intentionally credential-free.  The selected
credential environment handle is read only after the ledger confirms an
admitted live reservation.
"""

from __future__ import annotations

import math
import os
import re
import time
from typing import Mapping

from core.pit_optimizer_v5.provider import (
    CompletionResultV5,
    ProviderFailureDiagnosticV5,
    ProviderHttpObservationCollectorV1,
)
from core.pit_optimizer_v5.transport_diagnostic import invoke_v5_json_once_observed

from .contracts import StudyAuthorityError, StudyContractError
from .ledger import StudyDispatchClaimV1, StudyLedgerV1
from .live_calls import study_transport_settings_sha256_v1


_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class StudyOpenRouterGatewayV1:
    """Lazy adapter around ``agent_loop.OpenRouterGateway`` for one study call."""

    def __init__(
        self,
        *,
        ledger: StudyLedgerV1,
        credential_environment_variable: str = "OPENROUTER_API_KEY",
        run_id: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if type(ledger) is not StudyLedgerV1:
            raise StudyContractError("study gateway ledger is invalid")
        if type(credential_environment_variable) is not str or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,63}", credential_environment_variable
        ):
            raise StudyContractError("study credential environment handle is invalid")
        if run_id is not None and (type(run_id) is not str or not run_id.strip()):
            raise StudyContractError("study gateway run ID is invalid")
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise StudyContractError("study gateway timeout is invalid")
        self.ledger = ledger
        self.credential_environment_variable = credential_environment_variable
        self.run_id = run_id or f"study-{ledger.study_id}"
        self.timeout_seconds = float(timeout_seconds)
        self.transport_settings_sha256 = study_transport_settings_sha256_v1()
        self.last_http_observation: Mapping[str, object] | None = None

    def _admitted(
        self,
        *,
        request_sha256: str,
        model: str,
        messages: tuple[Mapping[str, object], ...],
        response_schema_json: bytes,
        max_output_tokens: int,
    ) -> StudyDispatchClaimV1:
        if self.ledger.grant.mode != "live_study":
            raise StudyAuthorityError("the OpenRouter gateway is unavailable in offline fixture mode")
        if type(request_sha256) is not str or _DIGEST_RE.fullmatch(request_sha256) is None:
            raise StudyContractError("study gateway request identity is invalid")
        if model != self.ledger.grant.model:
            raise StudyAuthorityError("study gateway model differs from the grant")
        return self.ledger.claim_dispatch(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )

    def invoke_json_once(
        self,
        *,
        request_sha256: str,
        model: str,
        messages: tuple[Mapping[str, object], ...],
        response_schema_json: bytes,
        max_output_tokens: int,
        automatic_retries: int,
        schema_repair_calls: int,
        deadline_monotonic: float,
    ) -> CompletionResultV5:
        # This gateway can be reused for another arm; an early validation or
        # admission failure must not expose the previous call's snapshot.
        self.last_http_observation = None
        if automatic_retries != 0 or schema_repair_calls != 0:
            raise StudyContractError("study transport retries and schema repair are permanently disabled")
        if type(deadline_monotonic) is not float or not math.isfinite(deadline_monotonic):
            raise StudyContractError("study gateway deadline is invalid")
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0 or remaining > float(self.ledger.grant.per_call_deadline_seconds):
            raise StudyAuthorityError("study gateway deadline exceeds the admitted per-call window")
        if type(messages) is not tuple or type(response_schema_json) is not bytes or type(max_output_tokens) is not int:
            raise StudyContractError("study gateway request shape is invalid")
        self._admitted(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )
        stored_request = self.ledger._stored_request(request_sha256)
        observer = ProviderHttpObservationCollectorV1(
            sink=lambda exchange_index, observation: self.ledger.persist_transport_observation_for_request(
                stored_request,
                exchange_index=exchange_index,
                observation=observation,
            )
        )
        credential = os.environ.get(self.credential_environment_variable)
        if not credential:
            diagnostic = ProviderFailureDiagnosticV5(phase="credential", code="credential_unavailable")
            observer.record_no_response_failure(diagnostic)
            self.last_http_observation = observer.snapshot()
            raise diagnostic
        # Import the legacy gateway only after admission and credential lookup.
        # Supplying api_key explicitly prevents its constructor from consulting
        # dotenv/environment state again.
        try:
            from agent_loop import OpenRouterGateway

            gateway = OpenRouterGateway(
                api_key=credential,
                run_id=self.run_id,
                timeout_seconds=self.timeout_seconds,
                max_attempts=1,
            )
        except ProviderFailureDiagnosticV5 as diagnostic:
            observer.record_no_response_failure(diagnostic)
            self.last_http_observation = observer.snapshot()
            raise
        except BaseException as exc:
            diagnostic = ProviderFailureDiagnosticV5.from_exception(exc, stage="gateway_init")
            observer.record_no_response_failure(diagnostic)
            self.last_http_observation = observer.snapshot()
            raise diagnostic from None
        try:
            result = invoke_v5_json_once_observed(
                gateway,
                request_sha256=request_sha256,
                model=model,
                messages=messages,
                response_schema_json=response_schema_json,
                max_output_tokens=max_output_tokens,
                wall_deadline=deadline_monotonic,
                allow_full_source_escape=False,
                observer=observer,
            )
        finally:
            self.last_http_observation = observer.snapshot()
        if type(result) is not CompletionResultV5:
            raise StudyContractError("study gateway returned an invalid completion")
        return result


__all__ = ["StudyOpenRouterGatewayV1"]
