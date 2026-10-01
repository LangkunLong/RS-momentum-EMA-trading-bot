"""Tests for the scheduler observation outcome contract."""

from types import SimpleNamespace
from unittest.mock import patch

from auto_trader import run_auto_trader
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation


def test_local_ledger_deferral_is_degraded_input_not_service_failure():
    observation = SchedulerObservation(run_id="test")
    observation.record_input_gap(
        "AAPL", "income-quarterly", "local_ledger_exhausted"
    )

    receipt = observation.to_receipt()

    assert receipt["service_health"] == "healthy"
    assert receipt["required_input_coverage"] == "degraded"
    assert receipt["overall_readiness"] == "unverified"


def test_latched_cap_denial_survives_later_work_and_never_passes_readiness():
    observation = SchedulerObservation(run_id="test")
    observation.latch_service_issue("alpaca_cap_denied", {"attempt": 257})
    observation.record_event("scan", "large_cap", "completed", {})

    receipt = observation.to_receipt()

    assert receipt["service_health"] == "failed"
    assert receipt["overall_readiness"] == "fail"


def test_healthy_observation_cannot_claim_issue_readiness_pass():
    receipt = SchedulerObservation(run_id="test").to_receipt()

    assert receipt["overall_readiness"] == "unverified"


def test_receipt_preserves_every_large_cap_candidate_identity_and_reason():
    observation = SchedulerObservation(run_id="large-candidate-receipt")
    observation.record_scan_coverage(
        requested=51,
        validated=51,
        candidate_outcomes=[
            {
                "symbol": f"T{index:03d}",
                "category": "rejected",
                "reasons": [f"reason_{index:03d}"],
                "analyzed": False,
                "fundamental_coverage": {},
            }
            for index in range(51)
        ],
    )

    candidates = observation.to_receipt()["scan_coverage"]["candidate_outcomes"]

    assert len(candidates) == 51
    assert candidates[0]["symbol"] == "T000"
    assert candidates[-1]["symbol"] == "T050"
    assert candidates[-1]["reasons"] == ["reason_050"]


def test_general_service_issue_is_not_a_resource_denial():
    observation = SchedulerObservation(run_id="test")
    observation.latch_service_issue("clock_unknown", {"source": "alpaca"})

    receipt = observation.to_receipt()

    assert receipt["service_health"] == "failed"
    assert receipt["resource_denials"] == {}


def test_resource_denial_is_counted_and_latched_once():
    observation = SchedulerObservation(run_id="test")
    observation.record_resource_denial("alpaca_cap_denied", {"attempt": 257})

    receipt = observation.to_receipt()

    assert receipt["service_health"] == "failed"
    assert receipt["resource_denials"] == {"alpaca_cap_denied": 1}


def test_required_input_coverage_can_be_unverified_or_failed():
    observation = SchedulerObservation(run_id="test")
    observation.record_input_gap(
        "AAPL", "income-quarterly", "read_outcome_unknown", coverage_status="unverified"
    )
    observation.record_input_gap(
        "MSFT", "income-quarterly", "required_input_failed", coverage_status="failed"
    )

    receipt = observation.to_receipt()

    assert receipt["required_input_coverage"] == "failed"
    assert receipt["service_health"] == "healthy"


def test_scan_phase_exit_work_is_recorded_with_no_open_positions():
    observation = SchedulerObservation(run_id="empty-exit-pass")
    market = SimpleNamespace(is_bullish=True, score=1.0, distribution_days=0, follow_through=False)
    with (
        activate_scheduler_observation(observation),
        patch("auto_trader.require_paper_mode"),
        patch("auto_trader.monitor_and_exit_positions", return_value=[]) as monitor,
        patch("auto_trader.scan_for_canslim_stocks", return_value=([], [], market)),
    ):
        result = run_auto_trader(dry_run=True, skip_entries=True)

    assert result.exited == ()
    monitor.assert_called_once_with(dry_run=True)
    phase_events = [
        (event["status"], event["key"])
        for event in observation.to_receipt()["events"]
        if event["kind"] == "scheduler_work"
    ]
    assert phase_events == [
        ("due", "exit_check:scan_phase"),
        ("started", "exit_check:scan_phase"),
        ("completed", "exit_check:scan_phase"),
    ]
