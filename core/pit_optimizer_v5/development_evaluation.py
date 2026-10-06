"""Separate content-authenticated authority for local development panel evaluations.

This contract is intentionally independent from Docker execution terminals and
resource lease records.  It is accepted only for explicitly development-scoped
V5 panel requests.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from .container_protocol import (
    PanelExecutionOutputV5,
    PanelExecutionRequestV5,
    decode_panel_execution_output_v5,
    decode_panel_execution_request_v5,
    panel_execution_output_bytes_v5,
    panel_execution_request_bytes_v5,
)
from .contracts import ArtifactRefV5, PanelEvaluationV5


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEVELOPMENT_RECEIPT_NAMESPACE = "candidate-development-evaluation-receipts"
_DEVELOPMENT_INPUT_NAMESPACE = "candidate-development-evaluation-inputs"
_DEVELOPMENT_OUTPUT_NAMESPACE = "candidate-development-evaluation-outputs"
_DEVELOPMENT_INPUT_MAX_BYTES = 8 * 1024 * 1024
_DEVELOPMENT_OUTPUT_MAX_BYTES = 64 * 1024 * 1024


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, *, maximum: int = 192) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DevelopmentEvaluationContextV5:
    """Study and campaign identity supplied by the local development controller."""

    campaign_id: str
    campaign_round_index: int
    study_id: str
    study_arm: Literal["primary", "withheld"]
    study_round_one_request_sha256: str
    study_round_one_terminal_sha256: str
    parsed_response_sha256: str
    candidate_sha256: str
    policy_identity_sha256: str

    def __post_init__(self) -> None:
        _text(self.campaign_id, "development evaluation campaign ID")
        if type(self.campaign_round_index) is not int or self.campaign_round_index < 1:
            raise ValueError("development evaluation campaign round is invalid")
        _text(self.study_id, "development evaluation study ID")
        if self.study_arm not in {"primary", "withheld"}:
            raise ValueError("development evaluation study arm is invalid")
        for value, label in (
            (self.study_round_one_request_sha256, "development study request"),
            (self.study_round_one_terminal_sha256, "development study terminal"),
            (self.parsed_response_sha256, "development parsed response"),
            (self.candidate_sha256, "development candidate"),
            (self.policy_identity_sha256, "development policy identity"),
        ):
            _digest(value, label)


@dataclass(frozen=True, slots=True)
class DevelopmentEvaluationReceiptV5:
    """Retained request/output lineage for one in-process development evaluation."""

    context: DevelopmentEvaluationContextV5
    request_ref: ArtifactRefV5
    output_ref: ArtifactRefV5
    request_sha256: str
    policy_identity_sha256: str
    input_sha256: str
    output_sha256: str
    evaluator_contract_sha256: str
    panel_sha256: str
    universe_sha256: str
    scope: Literal["development_sp500_v2"] = "development_sp500_v2"
    schema_version: Literal[5] = 5

    def __post_init__(self) -> None:
        if type(self.context) is not DevelopmentEvaluationContextV5:
            raise ValueError("development evaluation context is invalid")
        if (
            type(self.request_ref) is not ArtifactRefV5
            or type(self.output_ref) is not ArtifactRefV5
            or self.request_ref.relative_path
            != f"adapter-blobs/{_DEVELOPMENT_INPUT_NAMESPACE}/{self.input_sha256}.bin"
            or self.output_ref.relative_path
            != f"adapter-blobs/{_DEVELOPMENT_OUTPUT_NAMESPACE}/{self.output_sha256}.bin"
        ):
            raise ValueError("development evaluation retained references are invalid")
        if self.scope != "development_sp500_v2":
            raise ValueError("development evaluation scope is invalid")
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("development evaluation receipt schema is invalid")
        for value, label in (
            (self.request_sha256, "development evaluator request"),
            (self.policy_identity_sha256, "development evaluator policy identity"),
            (self.input_sha256, "development evaluator input"),
            (self.output_sha256, "development evaluator output"),
            (self.evaluator_contract_sha256, "development evaluator contract"),
            (self.panel_sha256, "development evaluator panel"),
            (self.universe_sha256, "development evaluator universe"),
        ):
            _digest(value, label)
        if self.request_ref.sha256 != self.input_sha256 or self.output_ref.sha256 != self.output_sha256:
            raise ValueError("development evaluation references differ from their hashes")

    def to_primitive(self) -> dict[str, object]:
        return {
            "context": {
                "campaign_id": self.context.campaign_id,
                "campaign_round_index": self.context.campaign_round_index,
                "study_id": self.context.study_id,
                "study_arm": self.context.study_arm,
                "study_round_one_request_sha256": self.context.study_round_one_request_sha256,
                "study_round_one_terminal_sha256": self.context.study_round_one_terminal_sha256,
                "parsed_response_sha256": self.context.parsed_response_sha256,
                "candidate_sha256": self.context.candidate_sha256,
                "policy_identity_sha256": self.context.policy_identity_sha256,
            },
            "request_ref": self.request_ref.to_primitive(),
            "output_ref": self.output_ref.to_primitive(),
            "request_sha256": self.request_sha256,
            "policy_identity_sha256": self.policy_identity_sha256,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "evaluator_contract_sha256": self.evaluator_contract_sha256,
            "panel_sha256": self.panel_sha256,
            "universe_sha256": self.universe_sha256,
            "scope": self.scope,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        from .contracts import canonical_json_bytes_v5

        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_json(cls, raw: bytes) -> "DevelopmentEvaluationReceiptV5":
        if type(raw) is not bytes:
            raise ValueError("development receipt bytes must be immutable")
        try:
            primitive = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("development receipt JSON is invalid") from exc
        expected = {
            "context", "request_ref", "output_ref", "request_sha256", "policy_identity_sha256", "input_sha256",
            "output_sha256", "evaluator_contract_sha256", "panel_sha256", "universe_sha256",
            "scope", "schema_version",
        }
        if type(primitive) is not dict or set(primitive) != expected:
            raise ValueError("development receipt fields are invalid")
        context_raw = primitive["context"]
        context_fields = {
            "campaign_id", "campaign_round_index", "study_id", "study_arm",
            "study_round_one_request_sha256", "study_round_one_terminal_sha256",
            "parsed_response_sha256", "candidate_sha256", "policy_identity_sha256",
        }
        if type(context_raw) is not dict or set(context_raw) != context_fields:
            raise ValueError("development receipt context fields are invalid")
        try:
            context = DevelopmentEvaluationContextV5(**context_raw)
            request_ref = ArtifactRefV5(**primitive["request_ref"])
            output_ref = ArtifactRefV5(**primitive["output_ref"])
            receipt = cls(
                context=context,
                request_ref=request_ref,
                output_ref=output_ref,
                request_sha256=primitive["request_sha256"],
                policy_identity_sha256=primitive["policy_identity_sha256"],
                input_sha256=primitive["input_sha256"],
                output_sha256=primitive["output_sha256"],
                evaluator_contract_sha256=primitive["evaluator_contract_sha256"],
                panel_sha256=primitive["panel_sha256"],
                universe_sha256=primitive["universe_sha256"],
                scope=primitive["scope"],
                schema_version=primitive["schema_version"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("development receipt schema is invalid") from exc
        if receipt.canonical_bytes() != raw:
            raise ValueError("development receipt is not canonical")
        return receipt


_DEVELOPMENT_AUTHENTICATION_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedInProcessDevelopmentEvaluationV5:
    receipt: DevelopmentEvaluationReceiptV5
    request: PanelExecutionRequestV5
    output: PanelExecutionOutputV5
    reference: ArtifactRefV5
    _token: object

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise ValueError("authenticated development evaluations are controller-issued")

    @classmethod
    def _issue(
        cls,
        *,
        receipt: DevelopmentEvaluationReceiptV5,
        request: PanelExecutionRequestV5,
        output: PanelExecutionOutputV5,
        reference: ArtifactRefV5,
    ) -> "AuthenticatedInProcessDevelopmentEvaluationV5":
        instance = object.__new__(cls)
        object.__setattr__(instance, "receipt", receipt)
        object.__setattr__(instance, "request", request)
        object.__setattr__(instance, "output", output)
        object.__setattr__(instance, "reference", reference)
        object.__setattr__(instance, "_token", _DEVELOPMENT_AUTHENTICATION_TOKEN)
        return instance

    def _is_controller_capability(self) -> bool:
        return self._token is _DEVELOPMENT_AUTHENTICATION_TOKEN


def persist_inprocess_development_evaluation_v5(
    *,
    repository: object,
    context: DevelopmentEvaluationContextV5,
    request: PanelExecutionRequestV5,
    evaluation: PanelEvaluationV5,
) -> ArtifactRefV5:
    """Persist canonical input/output bytes and a separate development receipt."""

    if (
        type(context) is not DevelopmentEvaluationContextV5
        or type(request) is not PanelExecutionRequestV5
        or type(evaluation) is not PanelEvaluationV5
        or request.pit_data_scope != "development_sp500_v2"
        or context.policy_identity_sha256 != request.policy_revision.sha256
    ):
        raise ValueError("development evaluation persistence authority is invalid")
    input_bytes = panel_execution_request_bytes_v5(request)
    output_bytes = panel_execution_output_bytes_v5(request=request, evaluation=evaluation)
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    receipt = DevelopmentEvaluationReceiptV5(
        context=context,
        request_ref=ArtifactRefV5(
            f"adapter-blobs/{_DEVELOPMENT_INPUT_NAMESPACE}/{input_sha256}.bin",
            input_sha256,
        ),
        output_ref=ArtifactRefV5(
            f"adapter-blobs/{_DEVELOPMENT_OUTPUT_NAMESPACE}/{output_sha256}.bin",
            output_sha256,
        ),
        request_sha256=request.request_sha256,
        policy_identity_sha256=request.policy_revision.sha256,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        evaluator_contract_sha256=request.evaluator_contract.sha256,
        panel_sha256=request.panel.sha256,
        universe_sha256=request.evaluator_contract.pit_bundle_sha256,
    )
    append = getattr(repository, "append_development_evaluation_receipt", None)
    if not callable(append):
        raise TypeError("candidate execution persistence boundary lacks development receipt support")
    reference = append(receipt, input_bytes=input_bytes, output_bytes=output_bytes)
    if (
        type(reference) is not ArtifactRefV5
        or reference.relative_path
        != f"adapter-blobs/{_DEVELOPMENT_RECEIPT_NAMESPACE}/{receipt.sha256}.bin"
        or reference.sha256 != receipt.sha256
    ):
        raise ValueError("development evaluation persistence returned a conflicting reference")
    return reference


def authenticate_inprocess_development_evaluation_v5(
    *,
    repository: object,
    reference: ArtifactRefV5,
    expected_context: DevelopmentEvaluationContextV5,
) -> AuthenticatedInProcessDevelopmentEvaluationV5:
    """Authenticate the retained request/output and every receipt identity."""

    if type(reference) is not ArtifactRefV5 or type(expected_context) is not DevelopmentEvaluationContextV5:
        raise ValueError("development evaluation authentication inputs are invalid")
    load = getattr(repository, "load_development_evaluation_receipt", None)
    if not callable(load):
        raise TypeError("candidate execution persistence boundary lacks development receipt reads")
    receipt, input_bytes, output_bytes = load(reference)
    if type(receipt) is not DevelopmentEvaluationReceiptV5 or receipt.context != expected_context:
        raise ValueError("development evaluation context identity differs")
    expected_ref_path = (
        f"adapter-blobs/{_DEVELOPMENT_RECEIPT_NAMESPACE}/{receipt.sha256}.bin"
    )
    if reference.relative_path != expected_ref_path or reference.sha256 != receipt.sha256:
        raise ValueError("development evaluation receipt reference differs")
    if (
        type(input_bytes) is not bytes
        or type(output_bytes) is not bytes
        or len(input_bytes) > _DEVELOPMENT_INPUT_MAX_BYTES
        or len(output_bytes) > _DEVELOPMENT_OUTPUT_MAX_BYTES
        or hashlib.sha256(input_bytes).hexdigest() != receipt.input_sha256
        or hashlib.sha256(output_bytes).hexdigest() != receipt.output_sha256
    ):
        raise ValueError("development evaluation retained bytes differ from their receipt hashes")
    request = decode_panel_execution_request_v5(input_bytes)
    output = decode_panel_execution_output_v5(output_bytes)
    if panel_execution_request_bytes_v5(request) != input_bytes:
        raise ValueError("development evaluation request bytes are not canonical")
    if panel_execution_output_bytes_v5(request=request, evaluation=output.evaluation) != output_bytes:
        raise ValueError("development evaluation output bytes are not canonical or request-bound")
    if (
        request.pit_data_scope != "development_sp500_v2"
        or request.sha256 != receipt.input_sha256
        or request.request_sha256 != receipt.request_sha256
        or request.policy_revision.sha256 != receipt.policy_identity_sha256
        or receipt.policy_identity_sha256 != receipt.context.policy_identity_sha256
        or request.evaluator_contract.sha256 != receipt.evaluator_contract_sha256
        or request.panel.sha256 != receipt.panel_sha256
        or request.evaluator_contract.pit_bundle_sha256 != receipt.universe_sha256
        or output.request_sha256 != request.request_sha256
        or output.input_sha256 != receipt.input_sha256
        or output.evaluation.policy_identity_sha256 != receipt.context.policy_identity_sha256
    ):
        raise ValueError("development evaluation request, output, or authority identity differs")
    return AuthenticatedInProcessDevelopmentEvaluationV5._issue(
        receipt=receipt,
        request=request,
        output=output,
        reference=reference,
    )


def development_evaluation_receipt_path_v5(receipt_sha256: str) -> str:
    _digest(receipt_sha256, "development evaluation receipt SHA-256")
    return f"adapter-blobs/{_DEVELOPMENT_RECEIPT_NAMESPACE}/{receipt_sha256}.bin"


__all__ = [
    "AuthenticatedInProcessDevelopmentEvaluationV5",
    "DevelopmentEvaluationContextV5",
    "DevelopmentEvaluationReceiptV5",
    "authenticate_inprocess_development_evaluation_v5",
    "development_evaluation_receipt_path_v5",
    "persist_inprocess_development_evaluation_v5",
]
