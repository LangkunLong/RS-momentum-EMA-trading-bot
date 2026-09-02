"""Trusted in-process adapter for the pure baseline strategy policy."""

from __future__ import annotations

from . import POLICY_INTERFACE_VERSION
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


class InProcessPolicyClient:
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


class JsonLinePolicyClient:
    """Typed evaluator adapter over one authenticated worker session."""

    def __init__(self, *, session: object, interface_version: int) -> None:
        if type(interface_version) is not int or interface_version <= 0:
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
        return value

    def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision:
        return self._call(  # type: ignore[return-value]
            "evaluate_entry", snapshot, EntrySnapshot, EntryDecision
        )

    def recommend_capacity(self, snapshot: CapacitySnapshot) -> CapacityDecision:
        return self._call(  # type: ignore[return-value]
            "recommend_capacity", snapshot, CapacitySnapshot, CapacityDecision
        )

    def recommend_allocation(
        self, snapshot: AllocationSnapshot
    ) -> AllocationDecision:
        return self._call(  # type: ignore[return-value]
            "recommend_allocation", snapshot, AllocationSnapshot, AllocationDecision
        )

    def select_eviction(self, snapshot: EvictionSnapshot) -> EvictionDecision:
        return self._call(  # type: ignore[return-value]
            "select_eviction", snapshot, EvictionSnapshot, EvictionDecision
        )

    def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision:
        return self._call(  # type: ignore[return-value]
            "evaluate_exit", snapshot, ExitSnapshot, ExitDecision
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()  # type: ignore[attr-defined]
