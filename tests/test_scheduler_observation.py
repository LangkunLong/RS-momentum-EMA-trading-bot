"""Tests for the scheduler observation outcome contract."""

from core.scheduler_observation import SchedulerObservation


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
