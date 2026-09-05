"""Canonical image-bound request and result protocol for PIT optimizer V5 panels."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from typing import Literal, TypeVar, get_args, get_origin, get_type_hints

from core.backtest_fills import ExecutionProfileV5
from core.pit_optimizer_evaluation import EvaluationPanelSpec

from .candidate_ir import PolicyRevisionIdentityV5
from .contracts import (
    EpisodePlanV5,
    EvaluatorContractV5,
    PanelEvaluationV5,
    SandboxProfileV5,
    canonical_json_bytes_v5,
    canonical_primitive_v5,
    canonical_sha256_v5,
    validate_episode_plan_panel_v5,
)


_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_PANEL_INPUT_MAXIMUM_BYTES_V5 = 8 * 1024 * 1024
_PANEL_OUTPUT_MAXIMUM_BYTES_V5 = 64 * 1024 * 1024


def _digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PanelExecutionRequestV5:
    """Complete immutable input consumed by the image-owned panel evaluator."""

    schema_version: Literal[5]
    request_sha256: str
    evaluator_contract: EvaluatorContractV5
    sandbox_profile: SandboxProfileV5
    execution_profile: ExecutionProfileV5
    policy_revision: PolicyRevisionIdentityV5
    episode: EpisodePlanV5
    panel: EvaluationPanelSpec
    scenario_ids: tuple[str, ...]
    policy_method_timeout_seconds: int
    worker_startup_timeout_seconds: int
    output_limit_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 5
            or type(self.evaluator_contract) is not EvaluatorContractV5
            or type(self.sandbox_profile) is not SandboxProfileV5
            or type(self.execution_profile) is not ExecutionProfileV5
            or type(self.policy_revision) is not PolicyRevisionIdentityV5
            or type(self.episode) is not EpisodePlanV5
            or type(self.panel) is not EvaluationPanelSpec
        ):
            raise ValueError("panel execution request schema is invalid")
        _digest(self.request_sha256, "panel request SHA-256")
        validate_episode_plan_panel_v5(self.episode, self.panel)
        if self.episode.purpose != self.panel.purpose:
            raise ValueError("panel execution purpose differs from its authenticated panel")
        if (
            self.execution_profile.sha256
            != self.evaluator_contract.execution_profile_sha256
            or self.sandbox_profile.sha256
            != self.evaluator_contract.sandbox_profile_sha256
            or self.sandbox_profile.runtime_source_sha256
            != self.evaluator_contract.evaluator_source_sha256
        ):
            raise ValueError("panel execution dependencies differ from evaluator authority")
        declared_scenarios = tuple(
            item.scenario_id for item in self.evaluator_contract.friction_grid
        )
        quick = self.episode.episode_ordinal is None and self.panel.purpose == "quick"
        discovery = (
            self.episode.episode_ordinal in {1, 2, 3, 4}
            and self.panel.purpose == "discovery"
        )
        expected_scenarios = (
            (self.evaluator_contract.selection_scenario_id,)
            if quick
            else declared_scenarios
            if discovery
            else None
        )
        if type(self.scenario_ids) is not tuple or self.scenario_ids != expected_scenarios:
            raise ValueError("panel execution stage or scenario scope is invalid")
        if (
            type(self.policy_method_timeout_seconds) is not int
            or self.policy_method_timeout_seconds <= 0
            or type(self.worker_startup_timeout_seconds) is not int
            or self.worker_startup_timeout_seconds <= 0
            or type(self.output_limit_bytes) is not int
            or self.output_limit_bytes != self.sandbox_profile.output_limit_bytes
        ):
            raise ValueError("panel execution resource bounds are invalid")

    @property
    def sha256(self) -> str:
        return canonical_sha256_v5(self)


@dataclass(frozen=True, slots=True)
class PanelExecutionOutputV5:
    """Request-bound canonical output emitted by the image-owned evaluator."""

    schema_version: Literal[5]
    request_sha256: str
    input_sha256: str
    evaluation: PanelEvaluationV5

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 5:
            raise ValueError("panel execution output schema is invalid")
        _digest(self.request_sha256, "panel output request SHA-256")
        _digest(self.input_sha256, "panel output input SHA-256")
        if type(self.evaluation) is not PanelEvaluationV5:
            raise ValueError("panel execution output evaluation is invalid")


T = TypeVar("T")


def _strict_json(raw: bytes, *, maximum_bytes: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ValueError("panel protocol payload is absent or exceeds its bound")
    duplicate = False

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("panel protocol payload is invalid JSON") from None
    if duplicate or canonical_json_bytes_v5(value) != raw:
        raise ValueError("panel protocol payload is not canonical JSON")
    return value


def _decode_value(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if annotation is Decimal:
        if type(value) is not str:
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or canonical_primitive_v5(number) != value:
            raise ValueError
        return number
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in arguments):
            raise ValueError
        return value
    if origin in {types.UnionType, __import__("typing").Union}:
        if value is None and type(None) in arguments:
            return None
        decoded: list[object] = []
        for argument in arguments:
            if argument is type(None):
                continue
            try:
                decoded.append(_decode_value(argument, value))
            except (TypeError, ValueError, ArithmeticError):
                pass
        if len(decoded) != 1:
            raise ValueError
        return decoded[0]
    if origin is tuple:
        if type(value) is not list:
            raise ValueError
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode_value(arguments[0], item) for item in value)
        if len(arguments) != len(value):
            raise ValueError
        return tuple(
            _decode_value(expected, item)
            for expected, item in zip(arguments, value, strict=True)
        )
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _decode_dataclass(annotation, value)
    if annotation in {str, int, bool}:
        if type(value) is not annotation:
            raise ValueError
        return value
    raise TypeError


def _decode_dataclass(cls: type[T], value: object) -> T:
    expected = {item.name for item in fields(cls) if item.init}
    if type(value) is not dict or set(value) != expected:
        raise ValueError
    hints = get_type_hints(cls)
    return cls(
        **{
            item.name: _decode_value(hints[item.name], value[item.name])
            for item in fields(cls)
            if item.init
        }
    )


def panel_execution_request_bytes_v5(request: PanelExecutionRequestV5) -> bytes:
    if type(request) is not PanelExecutionRequestV5:
        raise ValueError("panel execution input must use the V5 protocol")
    content = canonical_json_bytes_v5(request)
    if len(content) > _PANEL_INPUT_MAXIMUM_BYTES_V5:
        raise ValueError("panel execution input exceeds its canonical byte bound")
    return content


def decode_panel_execution_request_v5(raw: bytes) -> PanelExecutionRequestV5:
    try:
        return _decode_dataclass(
            PanelExecutionRequestV5,
            _strict_json(raw, maximum_bytes=_PANEL_INPUT_MAXIMUM_BYTES_V5),
        )
    except (TypeError, ValueError, ArithmeticError, RecursionError):
        raise ValueError("panel execution request schema is invalid") from None


def panel_execution_output_bytes_v5(
    *,
    request: PanelExecutionRequestV5,
    evaluation: PanelEvaluationV5,
) -> bytes:
    if type(request) is not PanelExecutionRequestV5 or type(evaluation) is not PanelEvaluationV5:
        raise ValueError("panel execution output inputs are invalid")
    if (
        evaluation.evaluator_contract_sha256 != request.evaluator_contract.sha256
        or evaluation.sandbox_profile_sha256 != request.sandbox_profile.sha256
        or evaluation.panel_sha256 != request.panel.sha256
        or evaluation.policy_identity_sha256 != request.policy_revision.sha256
        or evaluation.start_date != request.panel.start_date
        or evaluation.end_date != request.panel.end_date
        or evaluation.selection_scenario_id
        != request.evaluator_contract.selection_scenario_id
        or tuple(item.scenario_id for item in evaluation.scenarios)
        != request.scenario_ids
    ):
        raise ValueError("panel execution output differs from its request")
    content = canonical_json_bytes_v5(
        PanelExecutionOutputV5(
            schema_version=5,
            request_sha256=request.request_sha256,
            input_sha256=request.sha256,
            evaluation=evaluation,
        )
    )
    if len(content) > _PANEL_OUTPUT_MAXIMUM_BYTES_V5:
        raise ValueError("panel execution output exceeds its canonical byte bound")
    return content


def decode_panel_execution_output_v5(raw: bytes) -> PanelExecutionOutputV5:
    try:
        return _decode_dataclass(
            PanelExecutionOutputV5,
            _strict_json(raw, maximum_bytes=_PANEL_OUTPUT_MAXIMUM_BYTES_V5),
        )
    except (TypeError, ValueError, ArithmeticError, RecursionError):
        raise ValueError("panel execution output schema is invalid") from None


__all__ = [
    "PanelExecutionOutputV5",
    "PanelExecutionRequestV5",
    "decode_panel_execution_output_v5",
    "decode_panel_execution_request_v5",
    "panel_execution_output_bytes_v5",
    "panel_execution_request_bytes_v5",
]
