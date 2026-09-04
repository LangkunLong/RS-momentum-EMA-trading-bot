"""Parity-first exit policy for policy interface V3."""

from __future__ import annotations

from core.strategy_policy.contracts import ExitDecision
from core.strategy_policy.contracts_v3 import ExitSnapshotV3
from core.strategy_policy.exit import evaluate_exit as _evaluate_exit_v2


def evaluate_exit(snapshot: ExitSnapshotV3) -> ExitDecision:
    """Apply the deterministic V2 exit baseline to the V3 base snapshot."""

    return _evaluate_exit_v2(snapshot.base)


__all__ = ["evaluate_exit"]
