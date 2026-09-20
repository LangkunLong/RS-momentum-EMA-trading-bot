"""Frozen, controller-authored behavior registry for the V5 study fixture.

The registry is deliberately independent of policy source execution.  Its
clients implement a small typed decision table and the source bundle stored
beside each entry is only parsed and AST validated.  This gives the study an
auditable source identity while keeping synthetic observations deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Literal

from core.pit_optimizer_v5.candidate_ir import (
    LiteralAxisV5,
    PolicyRevisionIdentityV5,
    SourceOperationV5,
    SourceBundleV5,
    SourceFileV5,
    StructuralTemplateV5,
    derive_policy_revision_identity_v5,
    validate_policy_source_ast_v5,
)
from core.pit_optimizer_v5.contracts import canonical_json_bytes_v5, canonical_primitive_v5
from core.pit_optimizer_v5.policy_scope import EDITABLE_POLICY_PATHS_V5
from core.pit_optimizer_v5.rendering import RenderedVariantV5, render_variants
from core.pit_optimizer_v5.search import annualized_return_pct as _annualized_return_pct
from core.pit_optimizer_v5.probes import (
    PROBE_METHODS_V5,
    PROBE_SUITE_ID_V5,
    ProbeObservationV5,
    SemanticFingerprintV5,
    canonical_probe_json_v5,
    fingerprint_policy_client_v5,
    policy_probe_suite_v1,
)
from core.strategy_policy import (
    POLICY_INTERFACE_VERSION_V3,
    StrategyPolicyClientV3,
    validate_allocation_decision,
    validate_capacity_decision,
    validate_eviction_decision,
    validate_exit_decision,
)
from core.strategy_policy.contracts import (
    AllocationDecision,
    CapacityDecision,
    EntryDecision,
    EvictionDecision,
    ExitDecision,
)
from core.strategy_policy.contracts_v3 import (
    AddOnDecisionV3,
    AddOnSnapshotV3,
    EntrySnapshotV3,
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EvictionSnapshotV3,
    ExitSnapshotV3,
    validate_add_on_decision,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REGISTRY_SCHEMA_VERSION_V1 = 1
_TRUSTED_RUNTIME_SHA256 = hashlib.sha256(b"v5-study-registered-policy-runtime-v1").hexdigest()
_IMMUTABLE_CONSTRAINTS_SHA256 = hashlib.sha256(b"v5-study-registered-policy-constraints-v1").hexdigest()
_SOURCE_COMMIT = "a" * 40


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _finite_decimal(value: object, label: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} is invalid")
    return value


def _canonical_json(raw: bytes, label: str) -> object:
    if type(raw) is not bytes or not raw or b"\n" in raw or b"\r" in raw:
        raise ValueError(f"{label} is invalid")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=_reject_constant)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if canonical_json_bytes_v5(value) != raw:
        raise ValueError(f"{label} is not canonical")
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"nonfinite JSON constant {value} is not allowed")


def _strict_fields(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _source_bundle_from_primitive(value: object) -> SourceBundleV5:
    raw = _strict_fields(value, {"files"}, "source bundle")
    files = raw["files"]
    if type(files) is not list:
        raise ValueError("source bundle files are invalid")
    parsed: list[SourceFileV5] = []
    for item in files:
        file_raw = _strict_fields(item, {"path", "source"}, "source file")
        parsed.append(SourceFileV5(path=file_raw["path"], source=file_raw["source"]))  # type: ignore[arg-type]
    return SourceBundleV5(files=tuple(parsed))


def _policy_revision_from_primitive(value: object) -> PolicyRevisionIdentityV5:
    raw = _strict_fields(
        value,
        {
            "policy_interface_version",
            "trusted_policy_runtime_sha256",
            "immutable_constraints_sha256",
            "editable_source_sha256",
        },
        "policy revision",
    )
    sources = raw["editable_source_sha256"]
    if type(sources) is not list:
        raise ValueError("policy revision source identities are invalid")
    parsed: list[tuple[str, str]] = []
    for item in sources:
        if type(item) is not list or len(item) != 2:
            raise ValueError("policy revision source identity is invalid")
        parsed.append((item[0], item[1]))  # type: ignore[arg-type]
    return PolicyRevisionIdentityV5(
        policy_interface_version=raw["policy_interface_version"],  # type: ignore[arg-type]
        trusted_policy_runtime_sha256=raw["trusted_policy_runtime_sha256"],  # type: ignore[arg-type]
        immutable_constraints_sha256=raw["immutable_constraints_sha256"],  # type: ignore[arg-type]
        editable_source_sha256=tuple(parsed),
    )


def _observation_from_primitive(value: object) -> ProbeObservationV5:
    raw = _strict_fields(value, {"probe_id", "method", "input_sha256", "decision_json_utf8"}, "probe output")
    decision = raw["decision_json_utf8"]
    if type(decision) is not str:
        raise ValueError("probe output decision is invalid")
    return ProbeObservationV5(
        probe_id=raw["probe_id"],  # type: ignore[arg-type]
        method=raw["method"],  # type: ignore[arg-type]
        input_sha256=raw["input_sha256"],  # type: ignore[arg-type]
        decision_json=decision.encode("utf-8"),
    )


def _snapshot_identity(snapshot: object, label: str = "snapshot") -> bytes:
    if not hasattr(snapshot, "to_canonical_json") or not callable(snapshot.to_canonical_json):
        raise ValueError(f"{label} is not a canonical policy snapshot")
    try:
        encoded = snapshot.to_canonical_json().encode("utf-8")
        _canonical_json(encoded, label)
    except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is not canonical") from exc
    return encoded


@dataclass(frozen=True, slots=True)
class RegistryEvaluatorInputV1:
    """One exact synthetic evaluator input, retaining full snapshot identity."""

    order: int
    method: Literal["evaluate_exit"]
    input_value: Decimal | None
    input_canonical_bytes: bytes
    input_identity_sha256: str
    snapshot_canonical_json: bytes
    snapshot_sha256: str
    applicable: bool

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("synthetic input order is invalid")
        if self.method != "evaluate_exit":
            raise ValueError("synthetic input method is invalid")
        value = _finite_decimal(self.input_value, "synthetic input value", nullable=True)
        if value is not None and not 0 <= value <= 1:
            raise ValueError("synthetic input value is outside bounds")
        if type(self.input_canonical_bytes) is not bytes or self.input_canonical_bytes != _canonical_probe_json(value):
            raise ValueError("synthetic input canonical bytes are invalid")
        if self.input_identity_sha256 != _sha256(self.input_canonical_bytes):
            raise ValueError("synthetic input identity does not reconcile")
        _digest(self.input_identity_sha256, "synthetic input identity")
        snapshot_json = self.snapshot_canonical_json
        _canonical_json(snapshot_json, "synthetic input snapshot")
        try:
            snapshot = ExitSnapshotV3.from_canonical_json(snapshot_json.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("synthetic input snapshot is invalid") from exc
        if _sha256(snapshot_json) != self.snapshot_sha256:
            raise ValueError("synthetic input snapshot identity does not reconcile")
        expected = None if value is None else float(value)
        if snapshot.features.atr_20_fraction != expected:
            raise ValueError("synthetic input snapshot ATR differs from recipe input")
        if type(self.applicable) is not bool:
            raise ValueError("synthetic input applicability is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "order": self.order,
            "method": self.method,
            "input_value": None if self.input_value is None else str(self.input_value),
            "input_canonical_bytes_utf8": self.input_canonical_bytes.decode("utf-8"),
            "input_identity_sha256": self.input_identity_sha256,
            "snapshot_canonical_json_utf8": self.snapshot_canonical_json.decode("utf-8"),
            "snapshot_sha256": self.snapshot_sha256,
            "applicable": self.applicable,
        }

    @classmethod
    def from_primitive(cls, value: object) -> "RegistryEvaluatorInputV1":
        raw = _strict_fields(
            value,
            {
                "order",
                "method",
                "input_value",
                "input_canonical_bytes_utf8",
                "input_identity_sha256",
                "snapshot_canonical_json_utf8",
                "snapshot_sha256",
                "applicable",
            },
            "synthetic input",
        )
        input_value = raw["input_value"]
        if input_value is not None:
            if type(input_value) is not str:
                raise ValueError("synthetic input value is not decimal text")
            try:
                parsed_value = Decimal(input_value)
            except Exception as exc:
                raise ValueError("synthetic input value is invalid") from exc
            if str(parsed_value) != input_value:
                raise ValueError("synthetic input value is not canonical")
            input_value = parsed_value
        if type(raw["input_canonical_bytes_utf8"]) is not str or type(raw["snapshot_canonical_json_utf8"]) is not str:
            raise ValueError("synthetic input bytes are invalid")
        return cls(
            order=raw["order"],  # type: ignore[arg-type]
            method=raw["method"],  # type: ignore[arg-type]
            input_value=input_value,  # type: ignore[arg-type]
            input_canonical_bytes=raw["input_canonical_bytes_utf8"].encode("utf-8"),  # type: ignore[union-attr]
            input_identity_sha256=raw["input_identity_sha256"],  # type: ignore[arg-type]
            snapshot_canonical_json=raw["snapshot_canonical_json_utf8"].encode("utf-8"),  # type: ignore[union-attr]
            snapshot_sha256=raw["snapshot_sha256"],  # type: ignore[arg-type]
            applicable=raw["applicable"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "RegistryEvaluatorInputV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _canonical_json(encoded, "synthetic input")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise ValueError("synthetic input encoding is not canonical")
        return parsed

    @property
    def snapshot_json(self) -> bytes:
        return self.snapshot_canonical_json

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class RegistryEvaluatorOutputV1:
    """One typed synthetic output compared with the neutral baseline."""

    input_identity_sha256: str
    snapshot_sha256: str
    decision_json: bytes
    changed: bool
    protected_control_unchanged: bool

    def __post_init__(self) -> None:
        _digest(self.input_identity_sha256, "synthetic output input identity")
        _digest(self.snapshot_sha256, "synthetic output snapshot identity")
        if type(self.decision_json) is not bytes or not self.decision_json:
            raise ValueError("synthetic output decision bytes are invalid")
        if _canonical_json(self.decision_json, "synthetic output decision") is None:
            raise ValueError("synthetic output decision is invalid")
        try:
            decision = ExitDecision.from_canonical_json(self.decision_json.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("synthetic output decision is invalid") from exc
        if type(decision) is not ExitDecision:
            raise ValueError("synthetic output decision is invalid")
        if type(self.changed) is not bool or type(self.protected_control_unchanged) is not bool:
            raise ValueError("synthetic output flags are invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "input_identity_sha256": self.input_identity_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "decision_json_utf8": self.decision_json.decode("utf-8"),
            "changed": self.changed,
            "protected_control_unchanged": self.protected_control_unchanged,
        }

    @classmethod
    def from_primitive(cls, value: object) -> "RegistryEvaluatorOutputV1":
        raw = _strict_fields(
            value,
            {
                "input_identity_sha256",
                "snapshot_sha256",
                "decision_json_utf8",
                "changed",
                "protected_control_unchanged",
            },
            "synthetic output",
        )
        if type(raw["decision_json_utf8"]) is not str:
            raise ValueError("synthetic output decision is invalid")
        return cls(
            input_identity_sha256=raw["input_identity_sha256"],  # type: ignore[arg-type]
            snapshot_sha256=raw["snapshot_sha256"],  # type: ignore[arg-type]
            decision_json=raw["decision_json_utf8"].encode("utf-8"),  # type: ignore[union-attr]
            changed=raw["changed"],  # type: ignore[arg-type]
            protected_control_unchanged=raw["protected_control_unchanged"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "RegistryEvaluatorOutputV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _canonical_json(encoded, "synthetic output")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise ValueError("synthetic output encoding is not canonical")
        return parsed

    @property
    def output_json(self) -> bytes:
        return self.decision_json

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


SyntheticEvaluatorInputV1 = RegistryEvaluatorInputV1
SyntheticEvaluatorOutputV1 = RegistryEvaluatorOutputV1


@dataclass(frozen=True, slots=True)
class SyntheticPortfolioEvaluatorInputV1:
    """Authenticated portfolio endpoints used by the synthetic evaluator.

    This is intentionally separate from the four supplemental ATR snapshots.
    The endpoint and scenario bytes are part of the registry commitment so a
    later evaluator cannot silently replace the supplied portfolio inputs.
    """

    round_index: Literal[1, 2]
    configuration_id: str
    starting_equity: Decimal
    ending_equity: Decimal
    gross_ending_equity: Decimal
    stress_ending_equity: Decimal
    days: int
    base_annualized_return_pct: Decimal
    gross_annualized_return_pct: Decimal
    stress_annualized_return_pct: Decimal

    def __post_init__(self) -> None:
        if type(self.round_index) is not int or self.round_index not in {1, 2}:
            raise ValueError("portfolio evaluator round index is invalid")
        _identifier(self.configuration_id, "portfolio evaluator configuration ID")
        endpoints = (
            self.starting_equity,
            self.ending_equity,
            self.gross_ending_equity,
            self.stress_ending_equity,
        )
        if any(type(value) is not Decimal or not value.is_finite() or value <= 0 for value in endpoints):
            raise ValueError("portfolio evaluator endpoints are invalid")
        if type(self.days) is not int or self.days <= 0:
            raise ValueError("portfolio evaluator duration is invalid")
        expected = (
            _annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.ending_equity,
                days=self.days,
            ),
            _annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.gross_ending_equity,
                days=self.days,
            ),
            _annualized_return_pct(
                starting_equity=self.starting_equity,
                ending_equity=self.stress_ending_equity,
                days=self.days,
            ),
        )
        if (
            self.base_annualized_return_pct,
            self.gross_annualized_return_pct,
            self.stress_annualized_return_pct,
        ) != expected:
            raise ValueError("portfolio evaluator annualized returns do not derive from endpoints")

    def to_primitive(self) -> dict[str, object]:
        return {
            "round_index": self.round_index,
            "configuration_id": self.configuration_id,
            "starting_equity": canonical_primitive_v5(self.starting_equity),
            "ending_equity": canonical_primitive_v5(self.ending_equity),
            "gross_ending_equity": canonical_primitive_v5(self.gross_ending_equity),
            "stress_ending_equity": canonical_primitive_v5(self.stress_ending_equity),
            "days": self.days,
            "base_annualized_return_pct": canonical_primitive_v5(self.base_annualized_return_pct),
            "gross_annualized_return_pct": canonical_primitive_v5(self.gross_annualized_return_pct),
            "stress_annualized_return_pct": canonical_primitive_v5(self.stress_annualized_return_pct),
        }

    @classmethod
    def from_primitive(cls, value: object) -> "SyntheticPortfolioEvaluatorInputV1":
        raw = _strict_fields(
            value,
            {
                "round_index",
                "configuration_id",
                "starting_equity",
                "ending_equity",
                "gross_ending_equity",
                "stress_ending_equity",
                "days",
                "base_annualized_return_pct",
                "gross_annualized_return_pct",
                "stress_annualized_return_pct",
            },
            "portfolio evaluator input",
        )

        def decimal_field(name: str) -> Decimal:
            encoded = raw[name]
            if type(encoded) is not str:
                raise ValueError(f"portfolio evaluator {name} is not decimal text")
            try:
                parsed = Decimal(encoded)
            except Exception as exc:
                raise ValueError(f"portfolio evaluator {name} is invalid") from exc
            if canonical_primitive_v5(parsed) != encoded:
                raise ValueError(f"portfolio evaluator {name} is not canonical")
            return parsed

        return cls(
            round_index=raw["round_index"],  # type: ignore[arg-type]
            configuration_id=raw["configuration_id"],  # type: ignore[arg-type]
            starting_equity=decimal_field("starting_equity"),
            ending_equity=decimal_field("ending_equity"),
            gross_ending_equity=decimal_field("gross_ending_equity"),
            stress_ending_equity=decimal_field("stress_ending_equity"),
            days=raw["days"],  # type: ignore[arg-type]
            base_annualized_return_pct=decimal_field("base_annualized_return_pct"),
            gross_annualized_return_pct=decimal_field("gross_annualized_return_pct"),
            stress_annualized_return_pct=decimal_field("stress_annualized_return_pct"),
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "SyntheticPortfolioEvaluatorInputV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _canonical_json(encoded, "portfolio evaluator input")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise ValueError("portfolio evaluator input encoding is not canonical")
        return parsed

    @property
    def cagr_pct(self) -> Decimal:
        return self.base_annualized_return_pct

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


PortfolioEvaluatorInputV1 = SyntheticPortfolioEvaluatorInputV1


@dataclass(frozen=True, slots=True)
class RegistryConfigurationV1:
    """One immutable registered behavior family/configuration."""

    configuration_id: str
    family: str
    parameters: tuple[tuple[str, object], ...]
    source_bundle: SourceBundleV5
    policy_revision: PolicyRevisionIdentityV5
    parent_configuration_id: str | None
    synthetic_evaluator_inputs: tuple[RegistryEvaluatorInputV1, ...]
    synthetic_evaluator_outputs: tuple[RegistryEvaluatorOutputV1, ...]
    portfolio_evaluator_inputs: tuple[SyntheticPortfolioEvaluatorInputV1, ...]
    fixed_suite_outputs: tuple[ProbeObservationV5, ...]
    fixed_suite_fingerprint: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        _identifier(self.configuration_id, "configuration ID")
        _identifier(self.family, "configuration family")
        if self.parent_configuration_id is not None:
            _identifier(self.parent_configuration_id, "parent configuration ID")
        if type(self.parameters) is not tuple or not self.parameters:
            raise ValueError("configuration parameters are invalid")
        parameter_names: list[str] = []
        for item in self.parameters:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("configuration parameter is invalid")
            name, value = item
            if type(name) is not str or not name.isidentifier() or name.startswith("_"):
                raise ValueError("configuration parameter name is invalid")
            _literal_parameter(value, f"configuration parameter {name}")
            if name == "threshold" and type(value) is not Decimal:
                raise ValueError("threshold configuration parameter must be Decimal")
            if name in {"operator", "rule"} and type(value) is not str:
                raise ValueError(f"{name} configuration parameter must be str")
            parameter_names.append(name)
        if parameter_names != sorted(parameter_names) or len(set(parameter_names)) != len(parameter_names):
            raise ValueError("configuration parameter names are not canonical")
        if type(self.source_bundle) is not SourceBundleV5 or type(self.policy_revision) is not PolicyRevisionIdentityV5:
            raise ValueError("configuration source authority is invalid")
        if self.policy_revision.editable_source_sha256 != tuple(
            (item.path, item.sha256) for item in self.source_bundle.files
        ):
            raise ValueError("configuration policy revision differs from source bundle")
        if type(self.synthetic_evaluator_inputs) is not tuple or type(self.synthetic_evaluator_outputs) is not tuple:
            raise ValueError("configuration synthetic vectors are invalid")
        if len(self.synthetic_evaluator_inputs) != len(self.synthetic_evaluator_outputs):
            raise ValueError("configuration synthetic vectors differ in length")
        if any(type(item) is not RegistryEvaluatorInputV1 for item in self.synthetic_evaluator_inputs):
            raise ValueError("configuration synthetic inputs are invalid")
        if any(type(item) is not RegistryEvaluatorOutputV1 for item in self.synthetic_evaluator_outputs):
            raise ValueError("configuration synthetic outputs are invalid")
        if tuple(item.order for item in self.synthetic_evaluator_inputs) != tuple(range(len(self.synthetic_evaluator_inputs))):
            raise ValueError("configuration synthetic inputs are not ordered")
        for input_item, output_item in zip(self.synthetic_evaluator_inputs, self.synthetic_evaluator_outputs, strict=True):
            if (
                output_item.input_identity_sha256 != input_item.input_identity_sha256
                or output_item.snapshot_sha256 != input_item.snapshot_sha256
            ):
                raise ValueError("configuration synthetic output identity differs from its input")
        if type(self.portfolio_evaluator_inputs) is not tuple or not self.portfolio_evaluator_inputs:
            raise ValueError("configuration portfolio evaluator inputs are invalid")
        if any(type(item) is not SyntheticPortfolioEvaluatorInputV1 for item in self.portfolio_evaluator_inputs):
            raise ValueError("configuration portfolio evaluator inputs are invalid")
        if any(item.configuration_id != self.configuration_id for item in self.portfolio_evaluator_inputs):
            raise ValueError("configuration portfolio evaluator identity differs")
        if tuple(item.round_index for item in self.portfolio_evaluator_inputs) != (1, 2):
            raise ValueError("configuration portfolio evaluator rounds are invalid")
        if type(self.fixed_suite_outputs) is not tuple or not self.fixed_suite_outputs:
            raise ValueError("configuration fixed outputs are invalid")
        if any(type(item) is not ProbeObservationV5 for item in self.fixed_suite_outputs):
            raise ValueError("configuration fixed outputs are invalid")
        expected_probe_shape = tuple(
            (item.probe_id, item.method, item.input_sha256) for item in policy_probe_suite_v1()
        )
        actual_probe_shape = tuple(
            (item.probe_id, item.method, item.input_sha256) for item in self.fixed_suite_outputs
        )
        if actual_probe_shape != expected_probe_shape:
            raise ValueError("configuration fixed output inputs differ from the frozen probe suite")
        _digest(self.fixed_suite_fingerprint, "configuration fixed fingerprint")
        if type(self.schema_version) is not int or self.schema_version != _REGISTRY_SCHEMA_VERSION_V1:
            raise ValueError("configuration schema version is invalid")

    def to_primitive(self) -> dict[str, object]:
        return {
            "configuration_id": self.configuration_id,
            "family": self.family,
            "parameters": [[name, _parameter_primitive(value)] for name, value in self.parameters],
            "source_bundle": self.source_bundle.to_primitive(),
            "policy_revision": self.policy_revision.to_primitive(),
            "parent_configuration_id": self.parent_configuration_id,
            "synthetic_evaluator_inputs": [item.to_primitive() for item in self.synthetic_evaluator_inputs],
            "synthetic_evaluator_outputs": [item.to_primitive() for item in self.synthetic_evaluator_outputs],
            "portfolio_evaluator_inputs": [item.to_primitive() for item in self.portfolio_evaluator_inputs],
            "fixed_suite_outputs": [item.to_primitive() for item in self.fixed_suite_outputs],
            "fixed_suite_fingerprint": self.fixed_suite_fingerprint,
            "schema_version": self.schema_version,
        }

    @property
    def fixed_suite(self) -> SemanticFingerprintV5:
        return SemanticFingerprintV5(
            suite_id=PROBE_SUITE_ID_V5,
            observations=self.fixed_suite_outputs,
            fingerprint_sha256=self.fixed_suite_fingerprint,
        )

    @property
    def fixed_suite_fingerprint_sha256(self) -> str:
        return self.fixed_suite_fingerprint

    @property
    def source_revision(self) -> PolicyRevisionIdentityV5:
        return self.policy_revision

    @property
    def evaluator_inputs(self) -> tuple[RegistryEvaluatorInputV1, ...]:
        return self.synthetic_evaluator_inputs

    @property
    def evaluator_outputs(self) -> tuple[RegistryEvaluatorOutputV1, ...]:
        return self.synthetic_evaluator_outputs

    @property
    def supplemental_evaluator_inputs(self) -> tuple[RegistryEvaluatorInputV1, ...]:
        return self.synthetic_evaluator_inputs

    @property
    def supplemental_evaluator_outputs(self) -> tuple[RegistryEvaluatorOutputV1, ...]:
        return self.synthetic_evaluator_outputs

    @property
    def synthetic_portfolio_evaluator_inputs(self) -> tuple[SyntheticPortfolioEvaluatorInputV1, ...]:
        return self.portfolio_evaluator_inputs

    @property
    def portfolio_inputs(self) -> tuple[SyntheticPortfolioEvaluatorInputV1, ...]:
        return self.portfolio_evaluator_inputs

    @classmethod
    def from_primitive(cls, value: object) -> "RegistryConfigurationV1":
        raw = _strict_fields(
            value,
            {
                "configuration_id",
                "family",
                "parameters",
                "source_bundle",
                "policy_revision",
                "parent_configuration_id",
                "synthetic_evaluator_inputs",
                "synthetic_evaluator_outputs",
                "portfolio_evaluator_inputs",
                "fixed_suite_outputs",
                "fixed_suite_fingerprint",
                "schema_version",
            },
            "registry configuration",
        )
        parameters = raw["parameters"]
        inputs = raw["synthetic_evaluator_inputs"]
        outputs = raw["synthetic_evaluator_outputs"]
        fixed_outputs = raw["fixed_suite_outputs"]
        portfolio_inputs = raw["portfolio_evaluator_inputs"]
        if not all(type(item) is list for item in (parameters, inputs, outputs, portfolio_inputs, fixed_outputs)):
            raise ValueError("registry configuration arrays are invalid")
        parsed_parameters: list[tuple[str, object]] = []
        for item in parameters:
            if type(item) is not list or len(item) != 2:
                raise ValueError("registry configuration parameter is invalid")
            parsed_parameters.append((item[0], _parameter_from_primitive(item[1])))  # type: ignore[arg-type]
        return cls(
            configuration_id=raw["configuration_id"],  # type: ignore[arg-type]
            family=raw["family"],  # type: ignore[arg-type]
            parameters=tuple(parsed_parameters),
            source_bundle=_source_bundle_from_primitive(raw["source_bundle"]),
            policy_revision=_policy_revision_from_primitive(raw["policy_revision"]),
            parent_configuration_id=raw["parent_configuration_id"],  # type: ignore[arg-type]
            synthetic_evaluator_inputs=tuple(RegistryEvaluatorInputV1.from_primitive(item) for item in inputs),
            synthetic_evaluator_outputs=tuple(RegistryEvaluatorOutputV1.from_primitive(item) for item in outputs),
            portfolio_evaluator_inputs=tuple(
                SyntheticPortfolioEvaluatorInputV1.from_primitive(item) for item in portfolio_inputs
            ),
            fixed_suite_outputs=tuple(_observation_from_primitive(item) for item in fixed_outputs),
            fixed_suite_fingerprint=raw["fixed_suite_fingerprint"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "RegistryConfigurationV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _canonical_json(encoded, "registry configuration")
        return cls.from_primitive(value)

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class FrozenBehaviorRegistryV1:
    """The ordered, immutable catalog committed before any model call."""

    configurations: tuple[RegistryConfigurationV1, ...]
    suite_id: str
    decision_function_sha256: str
    source_template_sha256: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _REGISTRY_SCHEMA_VERSION_V1:
            raise ValueError("registry schema version is invalid")
        if self.suite_id != PROBE_SUITE_ID_V5:
            raise ValueError("registry suite identity is invalid")
        if type(self.configurations) is not tuple or not self.configurations:
            raise ValueError("registry configurations are invalid")
        if any(type(item) is not RegistryConfigurationV1 for item in self.configurations):
            raise ValueError("registry configurations are invalid")
        ids = tuple(item.configuration_id for item in self.configurations)
        if len(set(ids)) != len(ids):
            raise ValueError("registry configuration IDs must be unique")
        by_id = set(ids)
        for index, item in enumerate(self.configurations):
            if item.parent_configuration_id is not None:
                if item.parent_configuration_id not in by_id:
                    raise ValueError("registry parent configuration is unknown")
                if item.parent_configuration_id == item.configuration_id:
                    raise ValueError("registry configuration cannot parent itself")
                if item.parent_configuration_id not in ids[:index]:
                    raise ValueError("registry configurations must be parent ordered")
        _digest(self.decision_function_sha256, "registry decision function identity")
        _digest(self.source_template_sha256, "registry source template identity")

    def to_primitive(self) -> dict[str, object]:
        return {
            "configurations": [item.to_primitive() for item in self.configurations],
            "suite_id": self.suite_id,
            "decision_function_sha256": self.decision_function_sha256,
            "source_template_sha256": self.source_template_sha256,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes_v5(self.to_primitive())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @property
    def digest(self) -> str:
        return self.sha256

    @property
    def frozen_canonical_bytes(self) -> bytes:
        return self.canonical_bytes()

    @property
    def template_sha256(self) -> str:
        return self.source_template_sha256

    def configuration(self, configuration_id: str) -> RegistryConfigurationV1:
        if type(configuration_id) is not str:
            raise ValueError("unknown configuration")
        for configuration in self.configurations:
            if configuration.configuration_id == configuration_id:
                return configuration
        raise ValueError(f"unknown configuration {configuration_id!r}")

    @classmethod
    def from_primitive(cls, value: object) -> "FrozenBehaviorRegistryV1":
        raw = _strict_fields(
            value,
            {
                "configurations",
                "suite_id",
                "decision_function_sha256",
                "source_template_sha256",
                "schema_version",
            },
            "behavior registry",
        )
        configurations = raw["configurations"]
        if type(configurations) is not list:
            raise ValueError("registry configurations are invalid")
        return cls(
            configurations=tuple(RegistryConfigurationV1.from_primitive(item) for item in configurations),
            suite_id=raw["suite_id"],  # type: ignore[arg-type]
            decision_function_sha256=raw["decision_function_sha256"],  # type: ignore[arg-type]
            source_template_sha256=raw["source_template_sha256"],  # type: ignore[arg-type]
            schema_version=raw["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_canonical_json(cls, raw: bytes | str) -> "FrozenBehaviorRegistryV1":
        encoded = raw.encode("utf-8") if type(raw) is str else raw
        value = _canonical_json(encoded, "behavior registry")
        parsed = cls.from_primitive(value)
        if parsed.canonical_bytes() != encoded:
            raise ValueError("behavior registry is not canonical")
        return parsed


def _parameter_primitive(value: object) -> object:
    _literal_parameter(value, "configuration parameter")
    if value is None:
        return {"type": "null", "value": None}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": value}
    if type(value) is str:
        return {"type": "str", "value": value}
    if type(value) is Decimal:
        return {"type": "decimal", "value": canonical_primitive_v5(value)}
    raise ValueError("configuration parameter type is not registered")


def _parameter_from_primitive(value: object) -> object:
    raw = _strict_fields(value, {"type", "value"}, "configuration parameter")
    kind = raw["type"]
    item = raw["value"]
    if kind == "null":
        if item is not None:
            raise ValueError("configuration null parameter is invalid")
        return None
    if kind == "bool":
        if type(item) is not bool:
            raise ValueError("configuration bool parameter is invalid")
        return item
    if kind == "int":
        if type(item) is not int:
            raise ValueError("configuration int parameter is invalid")
        return item
    if kind == "str":
        if type(item) is not str:
            raise ValueError("configuration string parameter is invalid")
        _literal_parameter(item, "configuration string parameter")
        return item
    if kind == "decimal":
        if type(item) is not str:
            raise ValueError("configuration decimal parameter is invalid")
        try:
            decimal = Decimal(item)
        except (ArithmeticError, ValueError):
            raise ValueError("configuration decimal parameter is invalid") from None
        if not decimal.is_finite() or canonical_primitive_v5(decimal) != item:
            raise ValueError("configuration decimal parameter is not canonical")
        return decimal
    raise ValueError("configuration parameter type tag is invalid")


def _literal_parameter(value: object, label: str) -> None:
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str and (not value or value.strip() != value):
            raise ValueError(f"{label} is invalid")
        return
    if type(value) is Decimal and value.is_finite():
        return
    raise ValueError(f"{label} is invalid")


def _canonical_probe_json(value: Decimal | None) -> bytes:
    return canonical_probe_json_v5(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parent_source_templates() -> SourceBundleV5:
    """Return the authenticated parent bundle used by the normal renderer."""

    exit_source = '''from __future__ import annotations

from core.strategy_policy.contracts import ExitDecision
from core.strategy_policy.contracts_v3 import ExitSnapshotV3

def evaluate_exit(snapshot: ExitSnapshotV3) -> ExitDecision:
    configuration_axis = (0)
    threshold = 0.05
    secondary_threshold = 0.50
    x = snapshot.features.atr_20_fraction
    parent_active = x is not None and x >= threshold
    if configuration_axis == 0:
        active = False
    elif configuration_axis == 1:
        active = x is not None and x < threshold
    elif configuration_axis == 2:
        active = parent_active
    elif configuration_axis == 3:
        active = parent_active
    elif configuration_axis == 4:
        active = not parent_active
    elif configuration_axis == 5:
        active = False
    elif configuration_axis == 6:
        active = parent_active and not (x is not None and x >= secondary_threshold)
    else:
        active = False
    return ExitDecision(
        actions=(),
        next_stop_price=None,
        early_winner_hold=snapshot.base.early_winner_hold != active,
        scale_out_tier=snapshot.base.scale_out_tier,
        breakeven_armed=snapshot.base.breakeven_armed,
        ema_trailing_active=snapshot.base.ema_trailing_active,
    )


__all__ = ("evaluate_exit",)
'''
    sources = {
        "core/strategy_policy/v3/entry.py": '''from __future__ import annotations

from core.strategy_policy.contracts import EntryDecision
from core.strategy_policy.contracts_v3 import EntrySnapshotV3


def evaluate_entry(snapshot: EntrySnapshotV3) -> EntryDecision:
    return EntryDecision(False, True, (None, None), ())


__all__ = ("evaluate_entry",)
''',
        "core/strategy_policy/v3/risk.py": '''from __future__ import annotations

from core.strategy_policy.contracts import AllocationDecision, CapacityDecision, EvictionDecision
from core.strategy_policy.contracts_v3 import AllocationSnapshotV3, CapacitySnapshotV3, EvictionSnapshotV3


def recommend_capacity(snapshot: CapacitySnapshotV3) -> CapacityDecision:
    return CapacityDecision(None, snapshot.base.configured_eviction_enabled)


def recommend_allocation(snapshot: AllocationSnapshotV3) -> AllocationDecision:
    return AllocationDecision(0.01, 0.01, None)


def select_eviction(snapshot: EvictionSnapshotV3) -> EvictionDecision:
    return EvictionDecision(None)


__all__ = ("recommend_allocation", "recommend_capacity", "select_eviction")
''',
        "core/strategy_policy/v3/position.py": '''from __future__ import annotations

from core.strategy_policy.contracts_v3 import AddOnDecisionV3, AddOnSnapshotV3


def evaluate_add_on(snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
    return AddOnDecisionV3(False, 0.0, None, "registered_no_add_on")


__all__ = ("evaluate_add_on",)
''',
        "core/strategy_policy/v3/exit.py": exit_source,
    }
    files = tuple(
        SourceFileV5(path=path, source=sources[path]) for path in EDITABLE_POLICY_PATHS_V5
    )
    return SourceBundleV5(files=files)


def _reviewed_exit_operation_source() -> str:
    """Return the one normal-operation function replacement with its axis marker."""

    return '''def evaluate_exit(snapshot: ExitSnapshotV3) -> ExitDecision:
    configuration_axis = PIT_AXIS("configuration_axis")
    threshold = 0.05
    secondary_threshold = 0.50
    x = snapshot.features.atr_20_fraction
    parent_active = x is not None and x >= threshold
    if configuration_axis == 0:
        active = False
    elif configuration_axis == 1:
        active = x is not None and x < threshold
    elif configuration_axis == 2:
        active = parent_active
    elif configuration_axis == 3:
        active = parent_active
    elif configuration_axis == 4:
        active = not parent_active
    elif configuration_axis == 5:
        active = False
    elif configuration_axis == 6:
        active = parent_active and not (x is not None and x >= secondary_threshold)
    else:
        active = False
    return ExitDecision(
        actions=(),
        next_stop_price=None,
        early_winner_hold=snapshot.base.early_winner_hold != active,
        scale_out_tier=snapshot.base.scale_out_tier,
        breakeven_armed=snapshot.base.breakeven_armed,
        ema_trailing_active=snapshot.base.ema_trailing_active,
    )
'''


_REVIEWED_SOURCE_PARENT_BUNDLE = _parent_source_templates()
_REVIEWED_SOURCE_PARENT_REVISION = derive_policy_revision_identity_v5(
    source_bundle=_REVIEWED_SOURCE_PARENT_BUNDLE,
    trusted_policy_runtime_sha256=_TRUSTED_RUNTIME_SHA256,
    immutable_constraints_sha256=_IMMUTABLE_CONSTRAINTS_SHA256,
)
_REVIEWED_SOURCE_TEMPLATE = StructuralTemplateV5(
    hypothesis_id="v5-study-registered-exit-axis",
    parent_revision_sha256=_REVIEWED_SOURCE_PARENT_REVISION.sha256,
    changed_symbols=("core.strategy_policy.v3.exit.evaluate_exit",),
    source_operations=(
        SourceOperationV5(
            path="core/strategy_policy/v3/exit.py",
            symbol="evaluate_exit",
            kind="replace_function",
            replacement_source=_reviewed_exit_operation_source(),
        ),
    ),
    axes=(LiteralAxisV5("configuration_axis", 0, (0, 1, 2, 3, 4, 5, 6)),),
    full_source_escape=None,
)
_REVIEWED_SOURCE_VARIANTS: tuple[RenderedVariantV5, ...] = render_variants(
    parent=_REVIEWED_SOURCE_PARENT_BUNDLE,
    parent_revision=_REVIEWED_SOURCE_PARENT_REVISION,
    template=_REVIEWED_SOURCE_TEMPLATE,
    maximum=7,
)
_REVIEWED_SOURCE_VARIANTS_BY_AXIS = {
    dict(variant.assignment.values)["configuration_axis"]: variant for variant in _REVIEWED_SOURCE_VARIANTS
}
if tuple(_REVIEWED_SOURCE_VARIANTS_BY_AXIS) != (0, 1, 2, 3, 4, 5, 6):
    raise RuntimeError("reviewed source renderer did not produce the complete finite axis")


def reviewed_source_parent_v1() -> tuple[SourceBundleV5, PolicyRevisionIdentityV5]:
    """Return the pure-rendering parent authority and its exact revision."""

    return _REVIEWED_SOURCE_PARENT_BUNDLE, _REVIEWED_SOURCE_PARENT_REVISION


def reviewed_source_template_v1() -> StructuralTemplateV5:
    """Return the reviewed normal-operation structural template."""

    return _REVIEWED_SOURCE_TEMPLATE


def reviewed_source_variants_v1() -> tuple[RenderedVariantV5, ...]:
    """Return the pure-rendered finite source variants used by the registry."""

    return _REVIEWED_SOURCE_VARIANTS


def _source_templates(*, axis: int) -> SourceBundleV5:
    if type(axis) is not int or axis not in _REVIEWED_SOURCE_VARIANTS_BY_AXIS:
        raise ValueError("registered source axis is outside the finite reviewed axis")
    return _REVIEWED_SOURCE_VARIANTS_BY_AXIS[axis].source_bundle


_SOURCE_TEMPLATE_SHA256 = _REVIEWED_SOURCE_TEMPLATE.sha256
_DECISION_RULES = {
    "interface": "strategy-policy-v3",
    "methods": {
        "evaluate_entry": "neutral_contract_valid",
        "recommend_capacity": "neutral_contract_valid",
        "recommend_allocation": "neutral_contract_valid",
        "select_eviction": "neutral_contract_valid",
        "evaluate_add_on": "neutral_contract_valid",
        "evaluate_exit": {
            "base": "preserve_all_fields_except_early_winner_hold",
            "thresholds": {"primary": "0.05", "secondary": "0.50"},
            "axes": {
                "0": "false",
                "1": "atr_lt_primary",
                "2": "atr_gte_primary",
                "3": "atr_gte_primary_xor_false",
                "4": "atr_gte_primary_xor_true",
                "5": "atr_gte_primary_xor_atr_gte_primary",
                "6": "atr_gte_primary_xor_atr_gte_secondary",
            },
        },
    },
}
_DECISION_FUNCTION_SOURCE = canonical_json_bytes_v5(_DECISION_RULES)
_DECISION_FUNCTION_SHA256 = _sha256(_DECISION_FUNCTION_SOURCE)

_CATALOG_ENTRIES: tuple[tuple[str, str, tuple[tuple[str, object], ...], str | None, int], ...] = (
    ("P0", "baseline", (("rule", "neutral"),), None, 0),
    ("A", "threshold", (("operator", "lt"), ("threshold", Decimal("0.05"))), "P0", 1),
    ("S", "threshold", (("operator", "gte"), ("threshold", Decimal("0.05"))), "P0", 2),
    ("S-inert", "xor", (("operator", "inert"),), "S", 3),
    ("S-always-on", "xor", (("operator", "always_on"),), "S", 4),
    ("S-gte-0.05", "xor", (("operator", "gte"), ("threshold", Decimal("0.05"))), "S", 5),
    ("S-gte-0.50", "xor", (("operator", "gte"), ("threshold", Decimal("0.50"))), "S", 6),
)

_PORTFOLIO_ENDPOINTS: dict[int, dict[str, tuple[Decimal, Decimal, Decimal, Decimal, int]]] = {
    1: {
        "P0": (Decimal("100"), Decimal("100"), Decimal("100.50"), Decimal("99.50"), 365),
        "A": (Decimal("100"), Decimal("101"), Decimal("101.50"), Decimal("100.50"), 365),
        "S": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
        "S-inert": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
        "S-always-on": (Decimal("100"), Decimal("101"), Decimal("101.50"), Decimal("100.50"), 365),
        "S-gte-0.05": (Decimal("100"), Decimal("100"), Decimal("100.50"), Decimal("99.50"), 365),
        "S-gte-0.50": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
    },
    2: {
        "P0": (Decimal("100"), Decimal("100"), Decimal("100.50"), Decimal("99.50"), 365),
        "A": (Decimal("100"), Decimal("101"), Decimal("101.50"), Decimal("100.50"), 365),
        "S": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
        "S-inert": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
        "S-always-on": (Decimal("100"), Decimal("101"), Decimal("101.50"), Decimal("100.50"), 365),
        "S-gte-0.05": (Decimal("100"), Decimal("100"), Decimal("100.50"), Decimal("99.50"), 365),
        "S-gte-0.50": (Decimal("100"), Decimal("102"), Decimal("102.50"), Decimal("101.50"), 365),
    },
}


def _portfolio_inputs_for_configuration(configuration_id: str) -> tuple[SyntheticPortfolioEvaluatorInputV1, ...]:
    """Return the two supplied portfolio endpoint rows for one frozen config."""

    rows: list[SyntheticPortfolioEvaluatorInputV1] = []
    for round_index in (1, 2):
        endpoints = _PORTFOLIO_ENDPOINTS.get(round_index, {}).get(configuration_id)
        if endpoints is None:
            raise ValueError("portfolio evaluator configuration is outside the frozen catalog")
        starting, ending, gross, stress, days = endpoints
        rows.append(
            SyntheticPortfolioEvaluatorInputV1(
                round_index=round_index,  # type: ignore[arg-type]
                configuration_id=configuration_id,
                starting_equity=starting,
                ending_equity=ending,
                gross_ending_equity=gross,
                stress_ending_equity=stress,
                days=days,
                base_annualized_return_pct=_annualized_return_pct(
                    starting_equity=starting, ending_equity=ending, days=days
                ),
                gross_annualized_return_pct=_annualized_return_pct(
                    starting_equity=starting, ending_equity=gross, days=days
                ),
                stress_annualized_return_pct=_annualized_return_pct(
                    starting_equity=starting, ending_equity=stress, days=days
                ),
            )
        )
    return tuple(rows)


def _catalog_source_revision(axis: int) -> tuple[SourceBundleV5, PolicyRevisionIdentityV5]:
    source_bundle = _source_templates(axis=axis)
    revision = derive_policy_revision_identity_v5(
        source_bundle=source_bundle,
        trusted_policy_runtime_sha256=_TRUSTED_RUNTIME_SHA256,
        immutable_constraints_sha256=_IMMUTABLE_CONSTRAINTS_SHA256,
    )
    return source_bundle, revision


def _parameter_map(configuration: RegistryConfigurationV1) -> dict[str, object]:
    return dict(configuration.parameters)


def _atr(snapshot: ExitSnapshotV3) -> Decimal | None:
    value = snapshot.features.atr_20_fraction
    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("exit snapshot ATR is invalid")
    return Decimal(str(value))


def _registered_toggle(configuration: RegistryConfigurationV1, value: Decimal | None) -> bool:
    """Return only one configuration's local boolean XOR axis."""

    parameters = _parameter_map(configuration)
    if configuration.configuration_id == "P0":
        return False
    if configuration.configuration_id == "A":
        return value is not None and value < Decimal("0.05")
    if configuration.configuration_id == "S":
        return value is not None and value >= Decimal("0.05")
    # Descendants are explicitly defined relative to S's registered result.
    # This helper returns the descendant's local XOR axis; the caller applies
    # it to the already registered S decision below.
    operator = parameters.get("operator")
    if operator == "inert":
        local = False
    elif operator == "always_on":
        local = True
    elif operator == "gte":
        threshold = parameters.get("threshold")
        if type(threshold) is not Decimal:
            raise ValueError("registered threshold is invalid")
        local = value is not None and value >= threshold
    else:
        raise ValueError("registered configuration operator is invalid")
    return local


def _neutral_decision(method: str, snapshot: object) -> object:
    if method == "evaluate_entry":
        if type(snapshot) is not EntrySnapshotV3:
            raise ValueError("entry snapshot is invalid")
        return EntryDecision(False, True, (None, None), ())
    if method == "recommend_capacity":
        if type(snapshot) is not CapacitySnapshotV3:
            raise ValueError("capacity snapshot is invalid")
        return CapacityDecision(None, snapshot.base.configured_eviction_enabled)
    if method == "recommend_allocation":
        if type(snapshot) is not AllocationSnapshotV3:
            raise ValueError("allocation snapshot is invalid")
        return AllocationDecision(
            min(0.01, float(snapshot.base.maximum_position_risk_fraction)),
            min(0.01, float(snapshot.base.maximum_stop_fraction)),
            None,
        )
    if method == "select_eviction":
        if type(snapshot) is not EvictionSnapshotV3:
            raise ValueError("eviction snapshot is invalid")
        return EvictionDecision(None)
    if method == "evaluate_add_on":
        if type(snapshot) is not AddOnSnapshotV3:
            raise ValueError("add-on snapshot is invalid")
        return AddOnDecisionV3(False, 0.0, None, "registered_no_add_on")
    if method == "evaluate_exit":
        if type(snapshot) is not ExitSnapshotV3:
            raise ValueError("exit snapshot is invalid")
        return ExitDecision(
            actions=(),
            next_stop_price=None,
            early_winner_hold=snapshot.base.early_winner_hold,
            scale_out_tier=snapshot.base.scale_out_tier,
            breakeven_armed=snapshot.base.breakeven_armed,
            ema_trailing_active=snapshot.base.ema_trailing_active,
        )
    raise ValueError("unknown policy method")


def _decision_from_configuration(
    registry: FrozenBehaviorRegistryV1,
    configuration: RegistryConfigurationV1,
    method: str,
    snapshot: object,
    *,
    atr: Decimal | None,
) -> object:
    decision = _neutral_decision(method, snapshot)
    if method != "evaluate_exit":
        return decision
    assert type(snapshot) is ExitSnapshotV3
    if configuration.parent_configuration_id is None:
        return decision
    # Resolve the declared parent through the authenticated registry. This
    # makes S descendants compose the actual registered S decision rather than
    # duplicating S's predicate or inferring a parent from an ID prefix.
    parent = registry.configuration(configuration.parent_configuration_id)
    parent_decision = _decision_from_configuration(registry, parent, method, snapshot, atr=atr)
    return replace(
        parent_decision,
        early_winner_hold=parent_decision.early_winner_hold ^ _registered_toggle(configuration, atr),
    )


def _protected_exit_control_unchanged(parent: ExitDecision, candidate: ExitDecision) -> bool:
    return (
        candidate.actions == parent.actions
        and candidate.next_stop_price == parent.next_stop_price
        and candidate.scale_out_tier == parent.scale_out_tier
        and candidate.breakeven_armed == parent.breakeven_armed
        and candidate.ema_trailing_active == parent.ema_trailing_active
    )


class _RegisteredPolicyClientV1:
    interface_version = POLICY_INTERFACE_VERSION_V3

    def __init__(self, registry: FrozenBehaviorRegistryV1, configuration: RegistryConfigurationV1) -> None:
        self._registry = registry
        self._configuration = configuration
        self._closed = False

    def _call(self, method: str, snapshot: object) -> object:
        if self._closed:
            raise RuntimeError("registered policy client is closed")
        return _validated_registered_decision(self._registry, self._configuration, method, snapshot)

    def evaluate_entry(self, snapshot: EntrySnapshotV3) -> EntryDecision:
        return self._call("evaluate_entry", snapshot)  # type: ignore[return-value]

    def recommend_capacity(self, snapshot: CapacitySnapshotV3) -> CapacityDecision:
        return self._call("recommend_capacity", snapshot)  # type: ignore[return-value]

    def recommend_allocation(self, snapshot: AllocationSnapshotV3) -> AllocationDecision:
        return self._call("recommend_allocation", snapshot)  # type: ignore[return-value]

    def select_eviction(self, snapshot: EvictionSnapshotV3) -> EvictionDecision:
        return self._call("select_eviction", snapshot)  # type: ignore[return-value]

    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
        return self._call("evaluate_add_on", snapshot)  # type: ignore[return-value]

    def evaluate_exit(self, snapshot: ExitSnapshotV3) -> ExitDecision:
        return self._call("evaluate_exit", snapshot)  # type: ignore[return-value]

    def close(self) -> None:
        self._closed = True


def _validated_registered_decision(
    registry: FrozenBehaviorRegistryV1,
    configuration: RegistryConfigurationV1,
    method: str,
    snapshot: object,
) -> object:
    if method not in PROBE_METHODS_V5:
        raise ValueError("unknown policy method")
    _snapshot_identity(snapshot, "policy snapshot")
    atr = _atr(snapshot) if method == "evaluate_exit" and type(snapshot) is ExitSnapshotV3 else None
    decision = _decision_from_configuration(registry, configuration, method, snapshot, atr=atr)
    if method == "evaluate_entry":
        if type(snapshot) is not EntrySnapshotV3 or type(decision) is not EntryDecision:
            raise ValueError("registered entry decision is invalid")
    elif method == "recommend_capacity":
        if type(snapshot) is not CapacitySnapshotV3 or type(decision) is not CapacityDecision:
            raise ValueError("registered capacity decision is invalid")
        validate_capacity_decision(snapshot.base, decision)
    elif method == "recommend_allocation":
        if type(snapshot) is not AllocationSnapshotV3 or type(decision) is not AllocationDecision:
            raise ValueError("registered allocation decision is invalid")
        validate_allocation_decision(snapshot.base, decision)
    elif method == "select_eviction":
        if type(snapshot) is not EvictionSnapshotV3 or type(decision) is not EvictionDecision:
            raise ValueError("registered eviction decision is invalid")
        validate_eviction_decision(snapshot.base, decision)
    elif method == "evaluate_add_on":
        if type(snapshot) is not AddOnSnapshotV3 or type(decision) is not AddOnDecisionV3:
            raise ValueError("registered add-on decision is invalid")
        validate_add_on_decision(snapshot, decision)
    elif method == "evaluate_exit":
        if type(snapshot) is not ExitSnapshotV3 or type(decision) is not ExitDecision:
            raise ValueError("registered exit decision is invalid")
        validate_exit_decision(snapshot.base, decision)
    try:
        encoded = decision.to_canonical_json().encode("utf-8")  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("registered decision is not canonical") from exc
    _canonical_json(encoded, "registered decision")
    return decision


def registry_decision_v1(
    *,
    registry: FrozenBehaviorRegistryV1,
    configuration_id: str,
    method: str,
    snapshot: object,
) -> object:
    """Evaluate one registered configuration on one exact typed snapshot."""

    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("registry is invalid")
    verify_registry_v1(registry)
    configuration = registry.configuration(configuration_id)
    return _validated_registered_decision(registry, configuration, method, snapshot)


def registry_client_v1(
    registry: FrozenBehaviorRegistryV1,
    configuration_id: str,
) -> StrategyPolicyClientV3:
    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("registry is invalid")
    verify_registry_v1(registry)
    configuration = registry.configuration(configuration_id)
    return _RegisteredPolicyClientV1(registry, configuration)


def _synthetic_inputs() -> tuple[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3], ...]:
    seed = next(
        case.snapshot for case in policy_probe_suite_v1() if case.probe_id == "exit_winner_scale_boundary"
    )
    values: tuple[Decimal | None, ...] = (Decimal("0.20"), Decimal("0.50"), Decimal("0.80"), None)
    rows: list[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3]] = []
    for order, value in enumerate(values):
        features = replace(seed.features, atr_20_fraction=None if value is None else float(value))
        snapshot = replace(seed, features=features)
        snapshot_json = _snapshot_identity(snapshot, "synthetic input snapshot")
        input_json = _canonical_probe_json(value)
        rows.append(
            (
                RegistryEvaluatorInputV1(
                    order=order,
                    method="evaluate_exit",
                    input_value=value,
                    input_canonical_bytes=input_json,
                    input_identity_sha256=_sha256(input_json),
                    snapshot_canonical_json=snapshot_json,
                    snapshot_sha256=_sha256(snapshot_json),
                    applicable=value is not None,
                ),
                snapshot,
            )
        )
    return tuple(rows)


def _placeholder_synthetic_outputs(
    synthetic_inputs: tuple[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3], ...],
) -> tuple[RegistryEvaluatorOutputV1, ...]:
    outputs: list[RegistryEvaluatorOutputV1] = []
    for input_item, snapshot in synthetic_inputs:
        decision = ExitDecision(
            actions=(),
            next_stop_price=None,
            early_winner_hold=snapshot.base.early_winner_hold,
            scale_out_tier=snapshot.base.scale_out_tier,
            breakeven_armed=snapshot.base.breakeven_armed,
            ema_trailing_active=snapshot.base.ema_trailing_active,
        )
        outputs.append(
            RegistryEvaluatorOutputV1(
                input_identity_sha256=input_item.input_identity_sha256,
                snapshot_sha256=input_item.snapshot_sha256,
                decision_json=decision.to_canonical_json().encode("utf-8"),
                changed=False,
                protected_control_unchanged=True,
            )
        )
    return tuple(outputs)


def _placeholder_fixed_outputs() -> tuple[ProbeObservationV5, ...]:
    outputs: list[ProbeObservationV5] = []
    for case in policy_probe_suite_v1():
        decision = _neutral_decision(case.method, case.snapshot)
        outputs.append(
            ProbeObservationV5(
                probe_id=case.probe_id,
                method=case.method,
                input_sha256=case.input_sha256,
                decision_json=decision.to_canonical_json().encode("utf-8"),  # type: ignore[attr-defined]
            )
        )
    return tuple(outputs)


def _provisional_configuration(
    *,
    configuration_id: str,
    family: str,
    parameters: tuple[tuple[str, object], ...],
    parent_configuration_id: str | None,
    axis: int,
    synthetic_inputs: tuple[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3], ...],
) -> RegistryConfigurationV1:
    source_bundle, revision = _catalog_source_revision(axis)
    placeholder_fixed = _placeholder_fixed_outputs()
    placeholder_outputs = _placeholder_synthetic_outputs(synthetic_inputs)
    portfolio_inputs = _portfolio_inputs_for_configuration(configuration_id)
    return RegistryConfigurationV1(
        configuration_id=configuration_id,
        family=family,
        parameters=parameters,
        source_bundle=source_bundle,
        policy_revision=revision,
        parent_configuration_id=parent_configuration_id,
        synthetic_evaluator_inputs=tuple(item[0] for item in synthetic_inputs),
        synthetic_evaluator_outputs=placeholder_outputs,
        portfolio_evaluator_inputs=portfolio_inputs,
        fixed_suite_outputs=placeholder_fixed,
        fixed_suite_fingerprint="0" * 64,
    )


def _finalize_configuration(
    *,
    provisional: RegistryConfigurationV1,
    registry: FrozenBehaviorRegistryV1,
    synthetic_inputs: tuple[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3], ...],
) -> RegistryConfigurationV1:
    """Bind outputs only after every registered parent is available."""

    client = _RegisteredPolicyClientV1(registry, provisional)
    fingerprint = fingerprint_policy_client_v5(client)
    baseline = registry.configuration("P0")
    outputs = _expected_synthetic_outputs(
        provisional,
        synthetic_inputs,
        registry,
        baseline,
    )
    return replace(
        provisional,
        synthetic_evaluator_outputs=outputs,
        fixed_suite_outputs=fingerprint.observations,
        fixed_suite_fingerprint=fingerprint.fingerprint_sha256,
    )


def build_study_registry_v1() -> FrozenBehaviorRegistryV1:
    """Build the deterministic catalog frozen before a model call."""

    synthetic_inputs = _synthetic_inputs()
    provisional_configurations = tuple(
        _provisional_configuration(
            configuration_id=configuration_id,
            family=family,
            parameters=parameters,
            parent_configuration_id=parent,
            axis=axis,
            synthetic_inputs=synthetic_inputs,
        )
        for configuration_id, family, parameters, parent, axis in _CATALOG_ENTRIES
    )
    provisional_registry = FrozenBehaviorRegistryV1(
        configurations=provisional_configurations,
        suite_id=PROBE_SUITE_ID_V5,
        decision_function_sha256=_DECISION_FUNCTION_SHA256,
        source_template_sha256=_SOURCE_TEMPLATE_SHA256,
    )
    configurations = tuple(
        _finalize_configuration(
            provisional=provisional,
            registry=provisional_registry,
            synthetic_inputs=synthetic_inputs,
        )
        for provisional in provisional_configurations
    )
    registry = FrozenBehaviorRegistryV1(
        configurations=configurations,
        suite_id=PROBE_SUITE_ID_V5,
        decision_function_sha256=_DECISION_FUNCTION_SHA256,
        source_template_sha256=_SOURCE_TEMPLATE_SHA256,
    )
    verify_registry_v1(registry)
    return registry


def _validate_registry_structure(registry: FrozenBehaviorRegistryV1) -> None:
    if type(registry) is not FrozenBehaviorRegistryV1:
        raise ValueError("registry is invalid")
    if registry.suite_id != PROBE_SUITE_ID_V5:
        raise ValueError("registry suite identity is invalid")
    if registry.decision_function_sha256 != _DECISION_FUNCTION_SHA256:
        raise ValueError("registry decision function identity differs")
    if registry.source_template_sha256 != _SOURCE_TEMPLATE_SHA256:
        raise ValueError("registry source template identity differs")
    for configuration in registry.configurations:
        if configuration.policy_revision.editable_source_sha256 != tuple(
            (item.path, item.sha256) for item in configuration.source_bundle.files
        ):
            raise ValueError("registry source identity differs")
        for source_file in configuration.source_bundle.files:
            validate_policy_source_ast_v5(path=source_file.path, source=source_file.source)


def _expected_synthetic_outputs(
    configuration: RegistryConfigurationV1,
    inputs: tuple[tuple[RegistryEvaluatorInputV1, ExitSnapshotV3], ...],
    registry: FrozenBehaviorRegistryV1,
    baseline: RegistryConfigurationV1,
) -> tuple[RegistryEvaluatorOutputV1, ...]:
    expected: list[RegistryEvaluatorOutputV1] = []
    for input_item, snapshot in inputs:
        parent = _validated_registered_decision(registry, baseline, "evaluate_exit", snapshot)
        candidate = _validated_registered_decision(registry, configuration, "evaluate_exit", snapshot)
        parent_json = parent.to_canonical_json().encode("utf-8")  # type: ignore[attr-defined]
        candidate_json = candidate.to_canonical_json().encode("utf-8")  # type: ignore[attr-defined]
        expected.append(
            RegistryEvaluatorOutputV1(
                input_identity_sha256=input_item.input_identity_sha256,
                snapshot_sha256=input_item.snapshot_sha256,
                decision_json=candidate_json,
                changed=candidate_json != parent_json,
                protected_control_unchanged=_protected_exit_control_unchanged(parent, candidate),
            )
        )
    return tuple(expected)


def verify_registry_v1(registry: FrozenBehaviorRegistryV1) -> None:
    """Recompute every frozen vector and source identity, failing closed."""

    _validate_registry_structure(registry)
    expected_ids = tuple(item[0] for item in _CATALOG_ENTRIES)
    if tuple(item.configuration_id for item in registry.configurations) != expected_ids:
        raise ValueError("registry catalog differs from the frozen initial catalog")
    for configuration, (configuration_id, family, parameters, parent, axis) in zip(
        registry.configurations,
        _CATALOG_ENTRIES,
        strict=True,
    ):
        expected_source_bundle, expected_revision = _catalog_source_revision(axis)
        if (
            configuration.configuration_id != configuration_id
            or configuration.family != family
            or configuration.parameters != parameters
            or configuration.parent_configuration_id != parent
            or configuration.source_bundle != expected_source_bundle
            or configuration.policy_revision != expected_revision
            or configuration.portfolio_evaluator_inputs != _portfolio_inputs_for_configuration(configuration_id)
        ):
            raise ValueError(f"configuration {configuration_id} differs from frozen catalog metadata")
    inputs = _synthetic_inputs()
    by_id = {item.configuration_id: item for item in registry.configurations}
    baseline = by_id["P0"]
    for configuration in registry.configurations:
        client = _RegisteredPolicyClientV1.__new__(_RegisteredPolicyClientV1)
        client._registry = registry
        client._configuration = configuration
        client._closed = False
        fingerprint = fingerprint_policy_client_v5(client)
        if (
            configuration.fixed_suite_fingerprint != fingerprint.fingerprint_sha256
            or configuration.fixed_suite_outputs != fingerprint.observations
        ):
            raise ValueError(f"configuration {configuration.configuration_id} fixed output or fingerprint differs")
        expected_inputs = tuple(item[0] for item in inputs)
        if configuration.synthetic_evaluator_inputs != expected_inputs:
            raise ValueError(f"configuration {configuration.configuration_id} synthetic inputs differ")
        expected_outputs = _expected_synthetic_outputs(configuration, inputs, registry, baseline)
        if configuration.synthetic_evaluator_outputs != expected_outputs:
            raise ValueError(f"configuration {configuration.configuration_id} synthetic outputs differ")
        if configuration.fixed_suite_fingerprint != fingerprint.fingerprint_sha256:
            raise ValueError("registry fixed fingerprint differs from recomputation")


__all__ = [
    "FrozenBehaviorRegistryV1",
    "RegistryConfigurationV1",
    "RegistryEvaluatorInputV1",
    "RegistryEvaluatorOutputV1",
    "SyntheticPortfolioEvaluatorInputV1",
    "PortfolioEvaluatorInputV1",
    "SyntheticEvaluatorInputV1",
    "SyntheticEvaluatorOutputV1",
    "build_study_registry_v1",
    "reviewed_source_parent_v1",
    "reviewed_source_template_v1",
    "reviewed_source_variants_v1",
    "registry_client_v1",
    "registry_decision_v1",
    "verify_registry_v1",
]
