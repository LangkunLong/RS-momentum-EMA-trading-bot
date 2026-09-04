"""Typed in-process and JSON-line clients for policy interfaces V2 and V3."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Callable

from . import POLICY_INTERFACE_VERSION, POLICY_INTERFACE_VERSION_V3
from . import entry, exit, risk
from .contracts import (
    AllocationDecision,
    AllocationSnapshot,
    CapacityDecision,
    CapacitySnapshot,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    EvictionSnapshot,
    ExitDecision,
    ExitSnapshot,
)
if TYPE_CHECKING:
    from .contracts_v3 import (
        AddOnDecisionV3,
        AddOnSnapshotV3,
        AllocationSnapshotV3,
        CapacitySnapshotV3,
        EntrySnapshotV3,
        EvictionSnapshotV3,
        ExitSnapshotV3,
    )


def _v3_contract(name: str) -> type[object]:
    module = import_module(".contracts_v3", __package__)
    value = getattr(module, name, None)
    if not isinstance(value, type):
        raise TypeError(f"V3 policy contract {name} is invalid")
    return value


_V3_EXPORTS = {
    "entry": ("evaluate_entry",),
    "risk": ("recommend_capacity", "recommend_allocation", "select_eviction"),
    "position": ("evaluate_add_on",),
    "exit": ("evaluate_exit",),
}


def _v3_dispatch() -> dict[str, Callable[[object], object]]:
    modules: dict[str, ModuleType] = {}
    for module_name in _V3_EXPORTS:
        module = import_module(f".v3.{module_name}", __package__)
        if not isinstance(module, ModuleType):
            raise TypeError("V3 policy module is invalid")
        modules[module_name] = module
    dispatch: dict[str, Callable[[object], object]] = {}
    for module_name, exports in _V3_EXPORTS.items():
        module = modules[module_name]
        for export in exports:
            function = getattr(module, export, None)
            if not callable(function):
                raise TypeError(f"V3 policy export {module_name}.{export} is invalid")
            dispatch[export] = function
    if tuple(dispatch) != (
        "evaluate_entry",
        "recommend_capacity",
        "recommend_allocation",
        "select_eviction",
        "evaluate_add_on",
        "evaluate_exit",
    ):
        raise TypeError("V3 policy exports do not match the interface")
    return dispatch


def _canonical_decision(value: object, expected_type: type[object]) -> object:
    if type(value) is not expected_type:
        raise TypeError("policy response type mismatch")
    try:
        canonical = expected_type.from_canonical_json(  # type: ignore[attr-defined]
            value.to_canonical_json()  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise TypeError("policy response schema mismatch") from exc
    if canonical != value:
        raise TypeError("policy response schema mismatch")
    return value


class InProcessPolicyClient:
    """Legacy V2 baseline client; behavior and imports remain unchanged."""

    interface_version = POLICY_INTERFACE_VERSION

    def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision:
        return entry.evaluate_entry(snapshot)

    def recommend_capacity(self, snapshot: CapacitySnapshot) -> CapacityDecision:
        return risk.recommend_capacity(snapshot)

    def recommend_allocation(self, snapshot: AllocationSnapshot) -> AllocationDecision:
        return risk.recommend_allocation(snapshot)

    def select_eviction(self, snapshot: EvictionSnapshot) -> EvictionDecision:
        return risk.select_eviction(snapshot)

    def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision:
        return exit.evaluate_exit(snapshot)

    def close(self) -> None:
        return None


class InProcessPolicyClientV3:
    """Strict V3 client over exactly four adaptive policy modules."""

    interface_version = POLICY_INTERFACE_VERSION_V3

    def __init__(self) -> None:
        self._dispatch = _v3_dispatch()

    def _call(
        self,
        method: str,
        snapshot: object,
        snapshot_type: type[object],
        expected_type: type[object],
    ) -> object:
        if type(snapshot) is not snapshot_type:
            raise TypeError("policy snapshot type mismatch")
        return _canonical_decision(self._dispatch[method](snapshot), expected_type)

    def evaluate_entry(self, snapshot: EntrySnapshotV3) -> EntryDecision:
        snapshot_type = _v3_contract("EntrySnapshotV3")
        return self._call(  # type: ignore[return-value]
            "evaluate_entry", snapshot, snapshot_type, EntryDecision
        )

    def recommend_capacity(self, snapshot: CapacitySnapshotV3) -> CapacityDecision:
        snapshot_type = _v3_contract("CapacitySnapshotV3")
        return self._call(  # type: ignore[return-value]
            "recommend_capacity", snapshot, snapshot_type, CapacityDecision
        )

    def recommend_allocation(
        self, snapshot: AllocationSnapshotV3
    ) -> AllocationDecision:
        snapshot_type = _v3_contract("AllocationSnapshotV3")
        return self._call(  # type: ignore[return-value]
            "recommend_allocation", snapshot, snapshot_type, AllocationDecision
        )

    def select_eviction(self, snapshot: EvictionSnapshotV3) -> EvictionDecision:
        snapshot_type = _v3_contract("EvictionSnapshotV3")
        return self._call(  # type: ignore[return-value]
            "select_eviction", snapshot, snapshot_type, EvictionDecision
        )

    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
        from .contracts_v3 import validate_add_on_decision

        snapshot_type = _v3_contract("AddOnSnapshotV3")
        decision_type = _v3_contract("AddOnDecisionV3")
        decision = self._call(
            "evaluate_add_on", snapshot, snapshot_type, decision_type
        )
        return validate_add_on_decision(snapshot, decision)  # type: ignore[arg-type]

    def evaluate_exit(self, snapshot: ExitSnapshotV3) -> ExitDecision:
        snapshot_type = _v3_contract("ExitSnapshotV3")
        return self._call(  # type: ignore[return-value]
            "evaluate_exit", snapshot, snapshot_type, ExitDecision
        )

    def close(self) -> None:
        return None


class JsonLinePolicyClient:
    """Typed evaluator adapter over one authenticated worker session."""

    def __init__(self, *, session: object, interface_version: int) -> None:
        if type(interface_version) is not int or interface_version not in {
            POLICY_INTERFACE_VERSION,
            POLICY_INTERFACE_VERSION_V3,
        }:
            raise ValueError("policy interface version is invalid")
        if not callable(getattr(session, "call", None)) or not callable(
            getattr(session, "close", None)
        ):
            raise TypeError("policy worker session is invalid")
        self.interface_version = interface_version
        self._session = session
        self._closed = False

    def _call(
        self,
        method: str,
        snapshot: object,
        snapshot_type: type[object],
        expected_type: type[object],
    ) -> object:
        if self._closed:
            raise RuntimeError("policy client is closed")
        if type(snapshot) is not snapshot_type:
            raise TypeError("policy snapshot type mismatch")
        value = self._session.call(method, snapshot)  # type: ignore[attr-defined]
        if type(value) is not expected_type:
            raise TypeError("policy response type mismatch")
        if self.interface_version == POLICY_INTERFACE_VERSION_V3:
            return _canonical_decision(value, expected_type)
        return value

    def evaluate_entry(
        self, snapshot: EntrySnapshot | EntrySnapshotV3
    ) -> EntryDecision:
        snapshot_type = (
            _v3_contract("EntrySnapshotV3")
            if self.interface_version == POLICY_INTERFACE_VERSION_V3
            else EntrySnapshot
        )
        return self._call(  # type: ignore[return-value]
            "evaluate_entry", snapshot, snapshot_type, EntryDecision
        )

    def recommend_capacity(
        self, snapshot: CapacitySnapshot | CapacitySnapshotV3
    ) -> CapacityDecision:
        snapshot_type = (
            _v3_contract("CapacitySnapshotV3")
            if self.interface_version == POLICY_INTERFACE_VERSION_V3
            else CapacitySnapshot
        )
        return self._call(  # type: ignore[return-value]
            "recommend_capacity", snapshot, snapshot_type, CapacityDecision
        )

    def recommend_allocation(
        self, snapshot: AllocationSnapshot | AllocationSnapshotV3
    ) -> AllocationDecision:
        snapshot_type = (
            _v3_contract("AllocationSnapshotV3")
            if self.interface_version == POLICY_INTERFACE_VERSION_V3
            else AllocationSnapshot
        )
        return self._call(  # type: ignore[return-value]
            "recommend_allocation", snapshot, snapshot_type, AllocationDecision
        )

    def select_eviction(
        self, snapshot: EvictionSnapshot | EvictionSnapshotV3
    ) -> EvictionDecision:
        snapshot_type = (
            _v3_contract("EvictionSnapshotV3")
            if self.interface_version == POLICY_INTERFACE_VERSION_V3
            else EvictionSnapshot
        )
        return self._call(  # type: ignore[return-value]
            "select_eviction", snapshot, snapshot_type, EvictionDecision
        )

    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
        from .contracts_v3 import validate_add_on_decision

        if self.interface_version != POLICY_INTERFACE_VERSION_V3:
            raise RuntimeError("evaluate_add_on requires policy interface V3")
        snapshot_type = _v3_contract("AddOnSnapshotV3")
        decision_type = _v3_contract("AddOnDecisionV3")
        decision = self._call(
            "evaluate_add_on", snapshot, snapshot_type, decision_type
        )
        return validate_add_on_decision(snapshot, decision)  # type: ignore[arg-type]

    def evaluate_exit(self, snapshot: ExitSnapshot | ExitSnapshotV3) -> ExitDecision:
        snapshot_type = (
            _v3_contract("ExitSnapshotV3")
            if self.interface_version == POLICY_INTERFACE_VERSION_V3
            else ExitSnapshot
        )
        return self._call(  # type: ignore[return-value]
            "evaluate_exit", snapshot, snapshot_type, ExitDecision
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()  # type: ignore[attr-defined]
