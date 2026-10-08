"""Recording adapter for the explicitly admitted synthetic engineering fixture.

The caller must obtain the descriptor and bundle through the real inspector and
identity-bound loader. This adapter does not load sources or replace decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TypeVar

from core.strategy_policy import POLICY_INTERFACE_VERSION_V3
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
    AllocationSnapshotV3,
    CapacitySnapshotV3,
    EntrySnapshotV3,
    EvictionSnapshotV3,
    ExitSnapshotV3,
)
from core.strategy_policy.frozen_bundle import (
    FrozenPolicyBundle,
    FrozenPolicyBundleDescriptor,
    FrozenPolicyClient,
)


@dataclass(frozen=True)
class RecordedPolicyCall:
    method: str
    snapshot: object
    decision: object


_Decision = TypeVar("_Decision")


class RecordingSimulatorPolicy:
    """Translate version representation only; delegate all six typed methods."""

    interface_version = POLICY_INTERFACE_VERSION_V3

    def __init__(
        self, descriptor: FrozenPolicyBundleDescriptor, bundle: FrozenPolicyBundle,
    ) -> None:
        if type(descriptor) is not FrozenPolicyBundleDescriptor or type(bundle) is not FrozenPolicyBundle:
            raise ValueError("parity requires the original frozen descriptor and loaded bundle")
        if type(bundle.client) is not FrozenPolicyClient:
            raise ValueError("parity requires the original authenticated frozen client")
        if any(type(version) is not str or version != "3" for version in (
            descriptor.interface_version, bundle.interface_version, bundle.client.interface_version,
        )):
            raise ValueError("parity requires frozen interface version string 3")
        for name in (
            "root", "policy_artifact_id", "capability_manifest_id", "feature_contract_id",
            "feature_calculator_identity", "runtime_identity", "capabilities", "source_sha256",
        ):
            if getattr(descriptor, name) != getattr(bundle, name):
                raise ValueError(f"parity descriptor and loaded bundle disagree: {name}")
        self.descriptor = descriptor
        self.client = bundle.client
        self.calls: list[RecordedPolicyCall] = []
        self.canonical_records: list[dict[str, object]] = []
        self.recorded_bytes = 0

    def _record(self, method: str, snapshot: object, decision: _Decision) -> _Decision:
        snapshot_json = snapshot.to_canonical_json()
        decision_json = decision.to_canonical_json()
        record = {"method": method, "snapshot": json.loads(snapshot_json), "decision": json.loads(decision_json),
                  "snapshot_sha256": hashlib.sha256(snapshot_json.encode()).hexdigest(),
                  "decision_sha256": hashlib.sha256(decision_json.encode()).hexdigest()}
        size = len(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        if self.recorded_bytes + size > 1024 * 1024:
            raise AssertionError("parity raw policy call trace exceeds 1 MiB; no cropping")
        self.recorded_bytes += size
        self.calls.append(RecordedPolicyCall(method, snapshot, decision))
        self.canonical_records.append(record)
        return decision

    def evaluate_entry(self, snapshot: EntrySnapshotV3) -> EntryDecision:
        return self._record("evaluate_entry", snapshot, self.client.evaluate_entry(snapshot))

    def recommend_capacity(self, snapshot: CapacitySnapshotV3) -> CapacityDecision:
        return self._record("recommend_capacity", snapshot, self.client.recommend_capacity(snapshot))

    def recommend_allocation(self, snapshot: AllocationSnapshotV3) -> AllocationDecision:
        return self._record("recommend_allocation", snapshot, self.client.recommend_allocation(snapshot))

    def select_eviction(self, snapshot: EvictionSnapshotV3) -> EvictionDecision:
        return self._record("select_eviction", snapshot, self.client.select_eviction(snapshot))

    def evaluate_add_on(self, snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
        return self._record("evaluate_add_on", snapshot, self.client.evaluate_add_on(snapshot))

    def evaluate_exit(self, snapshot: ExitSnapshotV3) -> ExitDecision:
        return self._record("evaluate_exit", snapshot, self.client.evaluate_exit(snapshot))

    def close(self) -> None:
        self.client.close()
