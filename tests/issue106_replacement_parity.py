"""Compatibility exports for the first #106 replacement-parity callers."""

from core.pit_policy_parity import (
    ActionParityComparison as ParityComparison,
    ActionParityDisposition as ParityDisposition,
    compare_action_parity_cases as compare_replacement_cases,
)

__all__ = ("ParityComparison", "ParityDisposition", "compare_replacement_cases")
