"""Recoverable, file-backed controller responses for development feedback roles."""

from __future__ import annotations

from decimal import Decimal
import json
import math
from pathlib import Path

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
from core.pit_optimizer_v5.contracts import ArtifactRefV5, CampaignManifestV5, canonical_sha256_v5
from core.pit_optimizer_v5.provider import (
    ControllerRoleResponsePendingV5,
    ControllerRoleTerminalAuthorityV5,
    ExistingPersistedRoleRequestV5,
    FreshPersistedRoleRequestV5,
    PersistedRoleRequestV5,
    RoleAttemptFactsV5,
    RoleEvidenceBindingFailureV5,
    RoleFailureCode,
    RoleInvocationPackageV5,
    RoleReconciliationResultV5,
    RoleResponseSchemaFailureV5,
    RoleUsageFactsV5,
    parse_and_bind_role_artifact,
)


_MAX_CONTROLLER_RESPONSE_BYTES = 4 * 1024 * 1024


def _zero_usage() -> RoleUsageFactsV5:
    return RoleUsageFactsV5(
        external_attempt_count=0,
        request_started=False,
        response_received=False,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=Decimal("0"),
        requested_model=None,
        returned_model=None,
        provider_request_id=None,
    )


def _decode_unique_json(raw: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate controller response key")
            result[key] = value
        return result

    if len(raw) > _MAX_CONTROLLER_RESPONSE_BYTES:
        raise ValueError("controller response exceeds its byte bound")
    decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if type(decoded) is not dict:
        raise ValueError("controller response envelope must be an object")
    return decoded


def _classify_controller_response_v5(
    *,
    persisted: PersistedRoleRequestV5,
    reference: ArtifactRefV5,
    raw: bytes,
) -> RoleInvocationPackageV5:
    response_sha256 = reference.sha256
    artifact = None
    outcome = "response_schema_failure"
    failure_code = RoleFailureCode.RESPONSE_SCHEMA
    try:
        envelope = _decode_unique_json(raw)
        if set(envelope) != {
            "schema_version",
            "artifact_type",
            "call_key_sha256",
            "request_sha256",
            "response",
        }:
            raise ValueError("controller response envelope keys are invalid")
        if envelope["schema_version"] != 5 or envelope["artifact_type"] != "controller_role_response":
            raise ValueError("controller response envelope schema is invalid")
        if (
            envelope["call_key_sha256"] != persisted.call.sha256
            or envelope["request_sha256"] != persisted.request.sha256
        ):
            raise RoleEvidenceBindingFailureV5(role=persisted.call.role)
        if type(envelope["response"]) is not dict:
            raise ValueError("controller response payload is invalid")
        response_text = json.dumps(envelope["response"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        artifact = parse_and_bind_role_artifact(request=persisted.request, response_text=response_text)
        outcome = "accepted"
        failure_code = None
    except RoleEvidenceBindingFailureV5:
        outcome = "evidence_binding_failure"
        failure_code = RoleFailureCode.EVIDENCE_BINDING
    except (RoleResponseSchemaFailureV5, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        pass
    attempt = RoleAttemptFactsV5(
        role=persisted.call.role,
        attempt_kind=persisted.call.attempt_kind,
        attempt_index=persisted.call.attempt_index,
        request_sha256=persisted.request.sha256,
        slot_id=None,
        outcome=outcome,
        failure_code=failure_code,
        usage=_zero_usage(),
        response_sha256=response_sha256,
        artifact_sha256=None if artifact is None else canonical_sha256_v5(artifact),
    )
    authority = ControllerRoleTerminalAuthorityV5(
        call_key_sha256=persisted.call.sha256,
        request_sha256=persisted.request.sha256,
        attempt_facts_sha256=attempt.sha256,
        raw_response_ref=reference,
        artifact_sha256=None if artifact is None else canonical_sha256_v5(artifact),
    )
    return RoleInvocationPackageV5(persisted.call, persisted.request, attempt, authority, artifact)


class FileBackedControllerRoleInvokerV5:
    """Consume exact local JSON envelopes without any external-provider activity.

    The response directory contains ``<call-key-sha256>.json`` files with exactly
    ``schema_version``, ``artifact_type``, ``call_key_sha256``,
    ``request_sha256``, and ``response``. ``response`` is the existing role
    response object accepted by :func:`parse_and_bind_role_artifact`.
    """

    def __init__(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        manifest: CampaignManifestV5,
        response_directory: Path,
    ) -> None:
        directory = Path(response_directory)
        if (
            type(repository) is not LocalArtifactRepositoryV5
            or type(manifest) is not CampaignManifestV5
            or manifest.pit_data_scope != "development_sp500_v2"
            or manifest.provider is not None
            or not directory.is_absolute()
        ):
            raise ValueError("controller role invoker requires provider-free development scope and an absolute directory")
        self._repository = repository
        self._response_directory = directory

    def _incoming_path(self, persisted: PersistedRoleRequestV5) -> Path:
        return self._response_directory / f"{persisted.call.sha256}.json"

    def _sealed(self, persisted: PersistedRoleRequestV5) -> tuple[ArtifactRefV5, bytes]:
        prior = self._repository.load_controller_role_response(call=persisted.call)
        if prior is not None:
            return prior
        incoming = self._incoming_path(persisted)
        try:
            with incoming.open("rb") as stream:
                raw = stream.read(_MAX_CONTROLLER_RESPONSE_BYTES + 1)
        except FileNotFoundError:
            raise ControllerRoleResponsePendingV5(persisted.call) from None
        if len(raw) > _MAX_CONTROLLER_RESPONSE_BYTES:
            raise ValueError("controller response exceeds its byte bound")
        reference = self._repository.append_controller_role_response(call=persisted.call, content=raw)
        return reference, raw

    def _terminal(self, persisted: PersistedRoleRequestV5) -> RoleInvocationPackageV5:
        reference, raw = self._sealed(persisted)
        return _classify_controller_response_v5(persisted=persisted, reference=reference, raw=raw)

    def invoke_once(
        self,
        persisted_request: FreshPersistedRoleRequestV5,
        *,
        deadline_monotonic: float,
    ) -> RoleInvocationPackageV5:
        if (
            type(persisted_request) is not FreshPersistedRoleRequestV5
            or type(deadline_monotonic) is not float
            or not math.isfinite(deadline_monotonic)
        ):
            raise ValueError("controller invocation requires a fresh request and finite deadline")
        return self._terminal(persisted_request)

    def reconcile_once(
        self,
        persisted_request: ExistingPersistedRoleRequestV5,
    ) -> RoleReconciliationResultV5:
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise ValueError("controller reconciliation requires an existing request")
        return self._terminal(persisted_request)


__all__ = ["FileBackedControllerRoleInvokerV5"]
