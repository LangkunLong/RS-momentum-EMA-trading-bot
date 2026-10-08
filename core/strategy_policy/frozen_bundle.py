"""Loader for exact, immutable V3 paper-policy source bundles."""

from __future__ import annotations

import ast
from dataclasses import dataclass, fields
import hashlib
import json
from pathlib import Path
from types import CodeType, ModuleType
from typing import Callable, Mapping

from core.pit_feature_snapshot import EntryFeaturesV3

from . import POLICY_INTERFACE_VERSION_V3
from .contracts import (
    AllocationDecision,
    CapacityDecision,
    EntryDecision,
    EvictionDecision,
    ExitDecision,
    MarketContextV1,
)
from .contracts_v3 import (
    AddOnDecisionV3,
    AddOnSnapshotV3,
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EntrySnapshotV3,
    EvictionSnapshotV3,
    ExitSnapshotV3,
    validate_add_on_decision,
)
from .runtime_identity import current_paper_runtime_identity


_MODULE_FILES = (("entry", "entry.py"), ("risk", "risk.py"), ("position", "position.py"), ("exit", "exit.py"))
_MODULE_EXPORTS = {
    "entry": ("evaluate_entry",),
    "risk": ("recommend_capacity", "recommend_allocation", "select_eviction"),
    "position": ("evaluate_add_on",),
    "exit": ("evaluate_exit",),
}
_EXPORT_ORDER = (
    "evaluate_entry",
    "recommend_capacity",
    "recommend_allocation",
    "select_eviction",
    "evaluate_add_on",
    "evaluate_exit",
)
_EXPECTED_CAPABILITIES = frozenset(
    {
        "required_entry_features",
        "optional_entry_features",
        "unsupported_entry_features",
        "supported_actions",
        "unsupported_actions",
        "exports",
    }
)
_ACTIONS = frozenset({"entry", "capacity", "allocation", "replacement", "addition", "exit"})
_ALLOWED_IMPORTS = frozenset(
    {
        "core.strategy_policy.contracts",
        "core.strategy_policy.contracts_v3",
        "core.pit_feature_snapshot",
        "math",
    }
)
_SAFE_BUILTINS = {
    name: getattr(__import__("builtins"), name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "Exception", "filter", "float",
        "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min",
        "next", "pow", "range", "reversed", "round", "set", "slice", "sorted", "str",
        "sum", "tuple", "ValueError", "zip",
    )
}


def _policy_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):
    if level or name not in _ALLOWED_IMPORTS and name != "__future__":
        raise ImportError("policy import is not allowed")
    return __import__(name, globals, locals, fromlist, level)


_SAFE_BUILTINS["__import__"] = _policy_import


class FrozenPolicyBundleError(ValueError):
    """Raised when a selected policy bundle is absent, altered, or incompatible."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise FrozenPolicyBundleError("policy manifest is not canonical JSON data") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrozenPolicyBundleError("policy manifest contains duplicate keys")
        result[key] = value
    return result


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise FrozenPolicyBundleError(f"policy {name} is invalid")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise FrozenPolicyBundleError(f"policy capability {name} is invalid")
    items = tuple(value)
    if items != tuple(sorted(set(items))):
        raise FrozenPolicyBundleError(f"policy capability {name} must be sorted and unique")
    return items


def _validate_capabilities(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _EXPECTED_CAPABILITIES:
        raise FrozenPolicyBundleError("policy capability manifest fields are incompatible")
    required = _string_list(value["required_entry_features"], "required_entry_features")
    optional = _string_list(value["optional_entry_features"], "optional_entry_features")
    unsupported_features = _string_list(value["unsupported_entry_features"], "unsupported_entry_features")
    supported_actions = _string_list(value["supported_actions"], "supported_actions")
    unsupported_actions = _string_list(value["unsupported_actions"], "unsupported_actions")
    allowed_features = {field.name for field in fields(EntryFeaturesV3)} | {
        f"market.{field.name}" for field in fields(MarketContextV1)
    }
    feature_groups = (set(required), set(optional), set(unsupported_features))
    if any(not group <= allowed_features for group in feature_groups):
        raise FrozenPolicyBundleError("policy capability manifest names an unknown feature")
    if any(feature_groups[left] & feature_groups[right] for left in range(3) for right in range(left + 1, 3)):
        raise FrozenPolicyBundleError("policy feature capabilities overlap")
    action_groups = (set(supported_actions), set(unsupported_actions))
    if any(not group <= _ACTIONS for group in action_groups) or action_groups[0] & action_groups[1]:
        raise FrozenPolicyBundleError("policy action capabilities overlap or name an unknown action")
    if action_groups[0] | action_groups[1] != _ACTIONS:
        raise FrozenPolicyBundleError("policy action capabilities must declare every action category")
    expected_exports = {name: list(exports) for name, exports in _MODULE_EXPORTS.items()}
    if value["exports"] != expected_exports:
        raise FrozenPolicyBundleError("policy exports do not match the V3 interface")
    return {
        "required_entry_features": list(required),
        "optional_entry_features": list(optional),
        "unsupported_entry_features": list(unsupported_features),
        "supported_actions": list(supported_actions),
        "unsupported_actions": list(unsupported_actions),
        "exports": expected_exports,
    }


def _validate_source(source: bytes, name: str, expected_exports: tuple[str, ...]) -> str:
    if not source or len(source) > 131_072:
        raise FrozenPolicyBundleError(f"policy source {name} is empty or exceeds its size bound")
    try:
        text = source.decode("utf-8", errors="strict")
        tree = ast.parse(text, filename=name, mode="exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise FrozenPolicyBundleError(f"policy source {name} is invalid Python") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name not in _ALLOWED_IMPORTS for alias in node.names):
            raise FrozenPolicyBundleError(f"policy source {name} imports an unsupported module")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "__future__" and tuple(alias.name for alias in node.names) == ("annotations",):
                continue
            if module not in _ALLOWED_IMPORTS or node.level:
                raise FrozenPolicyBundleError(f"policy source {name} imports an unsupported module")
        if isinstance(node, ast.Name) and node.id in {
            "__builtins__", "__import__", "eval", "exec", "compile", "open", "input", "globals", "locals", "vars",
        }:
            raise FrozenPolicyBundleError(f"policy source {name} uses an unsupported builtin")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise FrozenPolicyBundleError(f"policy source {name} uses private attribute access")
    future_annotations = any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and tuple(alias.name for alias in statement.names) == ("annotations",)
        for statement in tree.body
    )
    declared_exports: set[str] = set()
    for index, statement in enumerate(tree.body):
        if index == 0 and isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and type(statement.value.value) is str:
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(statement, ast.FunctionDef)
            and not statement.name.startswith("__")
            and not statement.decorator_list
            and not statement.args.defaults
            and not any(default is not None for default in statement.args.kw_defaults)
        ):
            if not future_annotations and any(
                argument.annotation is not None
                for argument in (*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs)
            ):
                raise FrozenPolicyBundleError(f"policy source {name} has executable annotations")
            if not future_annotations and (statement.args.vararg and statement.args.vararg.annotation is not None):
                raise FrozenPolicyBundleError(f"policy source {name} has executable annotations")
            if not future_annotations and (statement.args.kwarg and statement.args.kwarg.annotation is not None):
                raise FrozenPolicyBundleError(f"policy source {name} has executable annotations")
            if not future_annotations and statement.returns is not None:
                raise FrozenPolicyBundleError(f"policy source {name} has executable annotations")
            declared_exports.add(statement.name)
            continue
        if isinstance(statement, ast.Assign) and all(isinstance(target, ast.Name) for target in statement.targets):
            try:
                ast.literal_eval(statement.value)
            except (ValueError, TypeError, SyntaxError):
                pass
            else:
                continue
        raise FrozenPolicyBundleError(f"policy source {name} contains a top-level initializer")
    if declared_exports.intersection(expected_exports) != set(expected_exports):
        raise FrozenPolicyBundleError(f"policy source {name} is missing a required export")
    return text


def _canonical_decision(value: object, expected_type: type[object], method: str) -> object:
    if type(value) is not expected_type:
        raise FrozenPolicyBundleError(f"policy response type mismatch for {method}")
    try:
        canonical = expected_type.from_canonical_json(value.to_canonical_json())  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise FrozenPolicyBundleError(f"policy response schema mismatch for {method}") from exc
    if canonical != value:
        raise FrozenPolicyBundleError(f"policy response schema mismatch for {method}")
    return value


@dataclass(frozen=True, slots=True)
class FrozenPolicyClient:
    """Typed dispatcher over four source modules verified by the loader."""

    _dispatch: Mapping[str, Callable[[object], object]]
    interface_version: str = "3"
    exports: tuple[str, ...] = _EXPORT_ORDER

    def _call(self, name: str, snapshot: object, expected_snapshot: type[object], expected_result: type[object]) -> object:
        if type(snapshot) is not expected_snapshot:
            raise FrozenPolicyBundleError(f"policy snapshot type mismatch for {name}")
        result = self._dispatch[name](snapshot)
        result = _canonical_decision(result, expected_result, name)
        if name == "evaluate_add_on":
            validate_add_on_decision(snapshot, result)  # type: ignore[arg-type]
        return result

    def evaluate_entry(self, snapshot: EntrySnapshotV3) -> EntryDecision:
        return self._call("evaluate_entry", snapshot, EntrySnapshotV3, EntryDecision)  # type: ignore[return-value]

    def recommend_capacity(self, snapshot: CapacitySnapshotV3) -> CapacityDecision:
        return self._call("recommend_capacity", snapshot, CapacitySnapshotV3, CapacityDecision)  # type: ignore[return-value]

    def recommend_allocation(self, snapshot: AllocationSnapshotV3) -> AllocationDecision:
        return self._call("recommend_allocation", snapshot, AllocationSnapshotV3, AllocationDecision)  # type: ignore[return-value]

    def select_eviction(self, snapshot: EvictionSnapshotV3) -> EvictionDecision:
        return self._call("select_eviction", snapshot, EvictionSnapshotV3, EvictionDecision)  # type: ignore[return-value]

    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
        return self._call("evaluate_add_on", snapshot, AddOnSnapshotV3, AddOnDecisionV3)  # type: ignore[return-value]

    def evaluate_exit(self, snapshot: ExitSnapshotV3) -> ExitDecision:
        return self._call("evaluate_exit", snapshot, ExitSnapshotV3, ExitDecision)  # type: ignore[return-value]

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class FrozenPolicyBundle:
    """Validated artifact, capability, and feature identities plus its client."""

    root: Path
    policy_artifact_id: str
    capability_manifest_id: str
    interface_version: str
    feature_contract_id: str
    feature_calculator_identity: str
    runtime_identity: str
    capabilities: Mapping[str, object]
    source_sha256: tuple[tuple[str, str], ...]
    client: FrozenPolicyClient


@dataclass(frozen=True, slots=True)
class FrozenPolicyBundleDescriptor:
    """No-execution view of one selected bundle's persisted identity."""

    root: Path
    policy_artifact_id: str
    capability_manifest_id: str
    interface_version: str
    feature_contract_id: str
    feature_calculator_identity: str
    runtime_identity: str
    capabilities: Mapping[str, object]
    source_sha256: tuple[tuple[str, str], ...]


def inspect_frozen_policy_bundle(root: str | Path) -> FrozenPolicyBundleDescriptor:
    """Inspect hashes and identities without parsing or executing policy source."""

    directory = Path(root)
    if directory.is_symlink() or not directory.is_dir():
        raise FrozenPolicyBundleError("selected frozen policy bundle directory is missing or invalid")
    expected_names = {"manifest.json", *(file_name for _name, file_name in _MODULE_FILES)}
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise FrozenPolicyBundleError("selected frozen policy bundle cannot be read") from exc
    if {entry.name for entry in entries} != expected_names or any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise FrozenPolicyBundleError("selected frozen policy bundle must contain exactly its manifest and four modules")
    try:
        manifest_raw = (directory / "manifest.json").read_bytes()
        if len(manifest_raw) > 65_536:
            raise FrozenPolicyBundleError("policy manifest exceeds its size bound")
        manifest = json.loads(manifest_raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys)
    except FrozenPolicyBundleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenPolicyBundleError("policy manifest is missing or malformed") from exc
    expected_fields = {
        "schema_version", "interface_version", "feature_contract_id", "feature_calculator_identity",
        "module_sha256", "capabilities", "immutable_constraints",
    }
    if type(manifest) is not dict or set(manifest) != expected_fields:
        raise FrozenPolicyBundleError("policy manifest fields are incompatible")
    if _canonical_bytes(manifest) != manifest_raw:
        raise FrozenPolicyBundleError("policy manifest is not canonical JSON")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise FrozenPolicyBundleError("policy manifest schema version is unsupported")
    interface_version = str(POLICY_INTERFACE_VERSION_V3)
    if manifest["interface_version"] != interface_version:
        raise FrozenPolicyBundleError("policy interface version is incompatible")
    feature_contract_id = _identifier(manifest["feature_contract_id"], "feature_contract_id")
    feature_calculator_identity = _identifier(manifest["feature_calculator_identity"], "feature_calculator_identity")
    constraints = manifest["immutable_constraints"]
    if type(constraints) is not dict:
        raise FrozenPolicyBundleError("policy immutable constraints are invalid")
    capabilities = _validate_capabilities(manifest["capabilities"])
    hashes = manifest["module_sha256"]
    if type(hashes) is not dict or set(hashes) != {name for name, _file_name in _MODULE_FILES}:
        raise FrozenPolicyBundleError("policy module hash set is incompatible")
    source_hashes: list[tuple[str, str]] = []
    for module_name, file_name in _MODULE_FILES:
        expected_hash = hashes[module_name]
        if type(expected_hash) is not str or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise FrozenPolicyBundleError(f"policy source hash for {module_name} is invalid")
        try:
            source = (directory / file_name).read_bytes()
        except OSError as exc:
            raise FrozenPolicyBundleError(f"policy source {module_name} is missing") from exc
        if not source or len(source) > 131_072:
            raise FrozenPolicyBundleError(f"policy source {module_name} is empty or exceeds its size bound")
        observed_hash = _sha256(source)
        if observed_hash != expected_hash:
            raise FrozenPolicyBundleError(f"policy source hash mismatch for {module_name}")
        source_hashes.append((module_name, observed_hash))
    try:
        runtime_identity = current_paper_runtime_identity()
    except RuntimeError as exc:
        raise FrozenPolicyBundleError("trusted paper runtime identity is unavailable") from exc
    identity_material = {
        "schema_version": 1,
        "interface_version": interface_version,
        "runtime_identity": runtime_identity,
        "immutable_constraints_sha256": _sha256(_canonical_bytes(constraints)),
        "module_sha256": [[name, digest] for name, digest in source_hashes],
    }
    return FrozenPolicyBundleDescriptor(
        root=directory.resolve(),
        policy_artifact_id=f"policy-artifact:sha256:{_sha256(_canonical_bytes(identity_material))}",
        capability_manifest_id=f"capability-manifest:sha256:{_sha256(_canonical_bytes(capabilities))}",
        interface_version=interface_version,
        feature_contract_id=feature_contract_id,
        feature_calculator_identity=feature_calculator_identity,
        runtime_identity=runtime_identity,
        capabilities=capabilities,
        source_sha256=tuple(source_hashes),
    )


def load_frozen_policy_bundle(
    root: str | Path,
    *,
    expected_identity: object,
) -> FrozenPolicyBundle:
    """Load one exact V3 bundle; every source and identity mismatch fails closed."""
    from core.policy_execution_state import PolicyDeploymentIdentity

    if type(expected_identity) is not PolicyDeploymentIdentity:
        raise FrozenPolicyBundleError("expected deployment identity is invalid")
    descriptor = inspect_frozen_policy_bundle(root)
    if (
        expected_identity.policy_artifact_id != descriptor.policy_artifact_id
        or expected_identity.capability_manifest_id != descriptor.capability_manifest_id
        or expected_identity.policy_interface_version != descriptor.interface_version
        or expected_identity.feature_contract_id != descriptor.feature_contract_id
        or expected_identity.feature_calculator_id != descriptor.feature_calculator_identity
        or expected_identity.runtime_identity != descriptor.runtime_identity
    ):
        raise FrozenPolicyBundleError("selected policy bundle does not match persisted deployment identity")

    directory = Path(root)
    if directory.is_symlink() or not directory.is_dir():
        raise FrozenPolicyBundleError("selected frozen policy bundle directory is missing or invalid")
    expected_names = {"manifest.json", *(file_name for _name, file_name in _MODULE_FILES)}
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise FrozenPolicyBundleError("selected frozen policy bundle cannot be read") from exc
    if {entry.name for entry in entries} != expected_names or any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise FrozenPolicyBundleError("selected frozen policy bundle must contain exactly its manifest and four modules")
    manifest_path = directory / "manifest.json"
    try:
        manifest_raw = manifest_path.read_bytes()
        if len(manifest_raw) > 65_536:
            raise FrozenPolicyBundleError("policy manifest exceeds its size bound")
        manifest = json.loads(manifest_raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys)
    except FrozenPolicyBundleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrozenPolicyBundleError("policy manifest is missing or malformed") from exc
    expected_fields = {
        "schema_version",
        "interface_version",
        "feature_contract_id",
        "feature_calculator_identity",
        "module_sha256",
        "capabilities",
        "immutable_constraints",
    }
    if type(manifest) is not dict or set(manifest) != expected_fields:
        raise FrozenPolicyBundleError("policy manifest fields are incompatible")
    if _canonical_bytes(manifest) != manifest_raw:
        raise FrozenPolicyBundleError("policy manifest is not canonical JSON")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise FrozenPolicyBundleError("policy manifest schema version is unsupported")
    if manifest["interface_version"] != str(POLICY_INTERFACE_VERSION_V3):
        raise FrozenPolicyBundleError("policy interface version is incompatible")
    feature_contract_id = _identifier(manifest["feature_contract_id"], "feature_contract_id")
    feature_calculator_identity = _identifier(manifest["feature_calculator_identity"], "feature_calculator_identity")
    constraints = manifest["immutable_constraints"]
    if type(constraints) is not dict:
        raise FrozenPolicyBundleError("policy immutable constraints are invalid")
    capabilities = _validate_capabilities(manifest["capabilities"])
    hashes = manifest["module_sha256"]
    if type(hashes) is not dict or set(hashes) != {name for name, _file_name in _MODULE_FILES}:
        raise FrozenPolicyBundleError("policy module hash set is incompatible")

    module_source: dict[str, bytes] = {}
    source_hashes: list[tuple[str, str]] = []
    for module_name, file_name in _MODULE_FILES:
        expected_hash = hashes[module_name]
        if type(expected_hash) is not str or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash):
            raise FrozenPolicyBundleError(f"policy source hash for {module_name} is invalid")
        try:
            source = (directory / file_name).read_bytes()
        except OSError as exc:
            raise FrozenPolicyBundleError(f"policy source {module_name} is missing") from exc
        if not source or len(source) > 131_072:
            raise FrozenPolicyBundleError(f"policy source {module_name} is empty or exceeds its size bound")
        observed_hash = _sha256(source)
        if observed_hash != expected_hash:
            raise FrozenPolicyBundleError(f"policy source hash mismatch for {module_name}")
        module_source[module_name] = source
        source_hashes.append((module_name, observed_hash))

    identity_material = {
        "schema_version": 1,
        "interface_version": str(POLICY_INTERFACE_VERSION_V3),
        "runtime_identity": descriptor.runtime_identity,
        "immutable_constraints_sha256": _sha256(_canonical_bytes(constraints)),
        "module_sha256": [[name, digest] for name, digest in source_hashes],
    }
    policy_artifact_id = f"policy-artifact:sha256:{_sha256(_canonical_bytes(identity_material))}"
    capability_manifest_id = f"capability-manifest:sha256:{_sha256(_canonical_bytes(capabilities))}"

    if (
        policy_artifact_id != descriptor.policy_artifact_id
        or capability_manifest_id != descriptor.capability_manifest_id
        or tuple(source_hashes) != descriptor.source_sha256
        or feature_contract_id != descriptor.feature_contract_id
        or feature_calculator_identity != descriptor.feature_calculator_identity
    ):
        raise FrozenPolicyBundleError("selected frozen policy bundle changed during identity validation")

    compiled_modules: dict[str, CodeType] = {}
    for module_name, file_name in _MODULE_FILES:
        module_text = _validate_source(module_source[module_name], file_name, _MODULE_EXPORTS[module_name])
        try:
            compiled_modules[module_name] = compile(
                module_text,
                str(directory / file_name),
                "exec",
                dont_inherit=True,
            )
        except (SyntaxError, ValueError, TypeError) as exc:
            raise FrozenPolicyBundleError(f"policy source {file_name} cannot be compiled") from exc

    modules: dict[str, ModuleType] = {}
    for module_name, file_name in _MODULE_FILES:
        module = ModuleType(f"_paper_policy_{policy_artifact_id[-16:]}.{module_name}")
        module.__file__ = str(directory / file_name)
        module.__package__ = "core.strategy_policy.v3"
        module.__dict__["__builtins__"] = _SAFE_BUILTINS
        try:
            exec(compiled_modules[module_name], module.__dict__)
        except Exception as exc:  # policy module initialization is untrusted input
            raise FrozenPolicyBundleError(f"policy module {module_name} failed to load") from exc
        modules[module_name] = module
    dispatch: dict[str, Callable[[object], object]] = {}
    for module_name, exports in _MODULE_EXPORTS.items():
        for export in exports:
            function = getattr(modules[module_name], export, None)
            if not callable(function):
                raise FrozenPolicyBundleError(f"policy export {module_name}.{export} is missing")
            dispatch[export] = function
    if tuple(dispatch) != _EXPORT_ORDER:
        raise FrozenPolicyBundleError("policy exports do not match the V3 interface")
    client = FrozenPolicyClient(dispatch)

    return FrozenPolicyBundle(
        root=directory.resolve(),
        policy_artifact_id=policy_artifact_id,
        capability_manifest_id=capability_manifest_id,
        interface_version=str(POLICY_INTERFACE_VERSION_V3),
        feature_contract_id=feature_contract_id,
        feature_calculator_identity=feature_calculator_identity,
        runtime_identity=descriptor.runtime_identity,
        capabilities=capabilities,
        source_sha256=tuple(source_hashes),
        client=client,
    )


__all__ = [
    "FrozenPolicyBundle",
    "FrozenPolicyBundleDescriptor",
    "FrozenPolicyBundleError",
    "FrozenPolicyClient",
    "inspect_frozen_policy_bundle",
    "load_frozen_policy_bundle",
]
