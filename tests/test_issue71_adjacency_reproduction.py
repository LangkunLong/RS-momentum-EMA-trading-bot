from __future__ import annotations

import pytest

from tools.reproduce_issue71_adjacency_comparison import _validate_recorded_counts


def _recorded_scan() -> dict[str, object]:
    return {
        "quarterly_records": 39_470,
        "price_security_sessions_all_symbols": 720_785,
        "state_window_denominator_per_metric": 8_696,
        "earnings_acceleration": {
            "changed_windows": 68,
            "changed_v2_available_to_v3_unavailable": 24,
            "changed_v2_unavailable_to_v3_available": 44,
            "affected_symbols": 47,
            "changed_priced_security_sessions": 6_007,
        },
        "revenue_acceleration": {
            "changed_windows": 0,
            "affected_symbols": 0,
            "changed_priced_security_sessions": 0,
        },
    }


def _reproduced_scan() -> dict[str, object]:
    return {
        "quarterly_records": 39_470,
        "price_security_sessions_all_symbols": 720_785,
        "earnings_acceleration": {
            "state_windows": 8_696,
            "changed_windows": 68,
            "v2_available_to_v3_unavailable": 24,
            "v2_unavailable_to_v3_available": 44,
            "affected_symbols": 47,
            "changed_priced_security_sessions": 6_007,
        },
        "revenue_acceleration": {
            "state_windows": 8_696,
            "changed_windows": 0,
            "v2_available_to_v3_unavailable": 0,
            "v2_unavailable_to_v3_available": 0,
            "affected_symbols": 0,
            "changed_priced_security_sessions": 0,
        },
    }


def test_recorded_counts_check_accepts_the_issue71_addendum_counts() -> None:
    _validate_recorded_counts(_reproduced_scan(), _recorded_scan())


def test_recorded_counts_check_reports_any_measurement_drift() -> None:
    reproduced = _reproduced_scan()
    reproduced["earnings_acceleration"]["changed_priced_security_sessions"] = 6_006  # type: ignore[index]

    with pytest.raises(ValueError, match="expected 6007, got 6006"):
        _validate_recorded_counts(reproduced, _recorded_scan())
