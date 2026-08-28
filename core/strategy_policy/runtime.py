"""Trusted in-process adapter for the pure baseline strategy policy."""

from __future__ import annotations

from . import POLICY_INTERFACE_VERSION
from . import entry, exit, risk


class InProcessPolicyClient:
    interface_version = POLICY_INTERFACE_VERSION

    def evaluate_entry(self, snapshot):
        return entry.evaluate_entry(snapshot)

    def recommend_capacity(self, snapshot):
        return risk.recommend_capacity(snapshot)

    def recommend_allocation(self, snapshot):
        return risk.recommend_allocation(snapshot)

    def select_eviction(self, snapshot):
        return risk.select_eviction(snapshot)

    def evaluate_exit(self, snapshot):
        return exit.evaluate_exit(snapshot)

    def close(self) -> None:
        return None
