"""Offline tests for the per-operation finite cap contract."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from config import settings

from core.operation_limits import (
    COMMON_HTTP_CATEGORIES,
    LIFECYCLE_DIMENSIONS,
    LIFECYCLE_DEADLINES,
    OBSERVER_DIMENSIONS,
    OBSERVER_DEADLINES,
    OperationBudget,
    OperationCapExceeded,
    OperationManifest,
    OperationManifestError,
    SAFETY_HTTP_CATEGORIES,
    activate_operation_budget,
    budgeted_standard_output,
    market_trend_data_request,
)
from core.alpaca_client_policy import configure_alpaca_rest_client
from core.scheduler_observation import SchedulerObservation, activate_scheduler_observation


def _limits_for(kind: str, *, admitted: bool = False) -> dict[str, object]:
    now = datetime.now().astimezone() - timedelta(minutes=1)
    dimensions = (
        {name: 2 for name in LIFECYCLE_DIMENSIONS}
        if kind == "lifecycle"
        else {name: 2 for name in OBSERVER_DIMENSIONS}
    )
    deadlines = (
        {name: 30.0 for name in LIFECYCLE_DEADLINES}
        if kind == "lifecycle"
        else {name: 30.0 for name in OBSERVER_DEADLINES}
    )
    categories = {name: 0 for name in COMMON_HTTP_CATEGORIES}
    if kind == "lifecycle":
        dimensions.update(
            {
                "eligible_order_ids": 3,
                "replacement_chain_length": 2,
                "cancel_attempts_per_order": 2,
                "exact_order_lookups_per_order": 2,
            }
        )
        categories.update(
            {
                "account_read": 1,
                "clock_read": 2,
                "position_read": 6,
                "open_order_read": 6,
                "closed_order_read": 4,
                "market_data_read": 2,
                "entry_submit": 1,
                "stop_submit": 2,
                "exit_submit": 2,
                "stop_replace": 1,
                "order_cancel": 6,
                "exact_order_lookup": 6,
            }
        )
        scope: dict[str, object] = {
            "symbol": "SPY",
            "quantity": 1,
            "max_entry_notional": 1000,
            "data_source": "alpaca",
        }
    else:
        dimensions.update(
            {
                "scheduler_ticks": 791,
                "clock_reads": 792,
                "scan_cycles": 2,
                "exit_check_cycles": 0,
                "symbols_per_scan": 1,
                "market_data_pages_per_symbol": 2,
                "market_trend_pages_per_scan": 1,
                "fmp_requests_per_symbol": 2,
                "index_requests_per_scan": 0,
                "receipt_events": 2000,
                "output_bytes": 4096,
            }
        )
        deadlines.update({"session_seconds": 23700.0})
        categories.update(
            {
                "account_read": 1,
                "clock_read": 792,
                "position_read": 0,
                "open_order_read": 0,
                "market_data_read": 6,
                "fmp_read": 4,
            }
        )
        scope = {"symbols": ["SPY"], "market_trend_symbol": "SPY"}
    return {
        "schema": "paper-operation-limits/v1",
        "operation": kind,
        "admission_status": "admitted" if admitted else "unadmitted",
        "admission_ref": "offline-principal-approval" if admitted else "",
        "binding": {
            "source_commit": "4" * 40,
            "runtime_id": "offline-test-runtime",
            "store_id": "offline-test-store",
            "lockfile_sha256": "a" * 64,
            "account_id": "paper-test-account",
            "allowance_owner": "offline-test-owner",
            "allowance_remaining_http_attempts": 1000,
            "session_start": now.isoformat(),
            "session_stop": (now + timedelta(hours=6, minutes=35)).isoformat(),
        },
        "scope": scope,
        "dimensions": dimensions,
        "deadlines_seconds": deadlines,
        "http_category_limits": categories,
    }


def _short_observer_limits() -> dict[str, object]:
    payload = _limits_for("observer", admitted=True)
    now = datetime.now().astimezone()
    binding = dict(payload["binding"])
    binding["session_start"] = (now - timedelta(seconds=1)).isoformat()
    binding["session_stop"] = (now + timedelta(seconds=29)).isoformat()
    payload["binding"] = binding
    dimensions = dict(payload["dimensions"])
    dimensions.update(
        {
            "scheduler_ticks": 2,
            "clock_reads": 3,
            "scan_cycles": 1,
            "stream_connections": 1,
        }
    )
    payload["dimensions"] = dimensions
    deadlines = dict(payload["deadlines_seconds"])
    deadlines.update({"request_timeout_seconds": 1.0, "session_seconds": 30.0})
    payload["deadlines_seconds"] = deadlines
    categories = dict(payload["http_category_limits"])
    categories.update({"clock_read": 3, "market_data_read": 3, "fmp_read": 2})
    payload["http_category_limits"] = categories
    return payload


def test_lifecycle_manifest_derives_total_and_safety_reserve() -> None:
    manifest = OperationManifest.from_mapping(_limits_for("lifecycle"))

    assert manifest.total_http_attempts == sum(
        manifest.http_category_limits.values()
    )
    assert manifest.safety_reserve_http_attempts == sum(
        manifest.http_category_limits[name] for name in SAFETY_HTTP_CATEGORIES
    )
    assert manifest.total_http_attempts <= manifest.allowance_remaining_http_attempts


def test_lifecycle_manifest_rejects_missing_required_dimensions() -> None:
    payload = _limits_for("lifecycle")
    dimensions = dict(payload["dimensions"])
    del dimensions["final_clear_polls"]
    payload["dimensions"] = dimensions

    with pytest.raises(OperationManifestError, match="final_clear_polls"):
        OperationManifest.from_mapping(payload)


def test_unadmitted_manifest_cannot_create_an_operation_budget() -> None:
    manifest = OperationManifest.from_mapping(_limits_for("lifecycle"))

    with pytest.raises(OperationManifestError, match="not admitted"):
        manifest.require_admitted()


def test_budget_denial_happens_before_the_next_request_and_marks_incomplete() -> None:
    payload = _limits_for("lifecycle", admitted=True)
    categories = dict(payload["http_category_limits"])
    categories["account_read"] = 1
    payload["http_category_limits"] = categories
    manifest = OperationManifest.from_mapping(payload)
    budget = OperationBudget(manifest)

    budget.reserve_http("account_read")
    with pytest.raises(OperationCapExceeded, match="account_read"):
        budget.reserve_http("account_read")

    assert budget.snapshot()["evidence_complete"] is False
    assert budget.snapshot()["http_attempts"] == 1


def test_lifecycle_client_order_lookup_is_classified_and_counted_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("lifecycle", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions["exact_order_lookups_per_order"] = 2
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    request = Mock(return_value="response")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        client._session.request(
            "GET",
            "https://paper-api.alpaca.markets/v2/orders:by_client_order_id",
            params={"client_order_id": "cslm-spy-20261004120000-abcdef"},
        )
        budget.link_client_order_identity(
            "cslm-spy-20261004120000-abcdef", "broker-order-1"
        )
        client._session.request(
            "GET",
            "https://paper-api.alpaca.markets/v2/orders/broker-order-1",
        )
        with pytest.raises(OperationCapExceeded, match="exact_order_lookups_per_order"):
            client._session.request(
                "GET",
                "https://paper-api.alpaca.markets/v2/orders:by_client_order_id",
                params={"client_order_id": "cslm-spy-20261004120000-abcdef"},
            )

    assert request.call_count == 2
    assert budget.snapshot()["http_attempts"] == 2


def test_order_cancel_lookup_event_and_output_dimensions_are_finite() -> None:
    payload = _limits_for("lifecycle", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions.update(
        {
            "eligible_order_ids": 3,
            "replacement_chain_length": 1,
            "cancel_attempts_per_order": 1,
            "exact_order_lookups_per_order": 1,
            "partial_fill_events": 1,
            "stream_connections": 1,
            "stream_events": 1,
        }
    )
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))

    budget.admit_order_id("entry-1")
    budget.admit_order_id("stop-1")
    budget.admit_order_id("exit-1")
    with pytest.raises(OperationCapExceeded, match="eligible_order_ids"):
        budget.admit_order_id("entry-2")

    budget.consume_order_attempt("cancel", "entry-1")
    with pytest.raises(OperationCapExceeded, match="cancel_attempts_per_order"):
        budget.consume_order_attempt("cancel", "entry-1")

    budget.consume("partial_fill_events")
    with pytest.raises(OperationCapExceeded, match="partial_fill_events"):
        budget.consume("partial_fill_events")

    observer_payload = _limits_for("observer", admitted=True)
    observer_dimensions = dict(observer_payload["dimensions"])
    observer_dimensions["output_bytes"] = 4
    observer_payload["dimensions"] = observer_dimensions
    output_budget = OperationBudget(
        OperationManifest.from_mapping(observer_payload)
    )
    output_budget.consume_output(4)
    with pytest.raises(OperationCapExceeded, match="output_bytes"):
        output_budget.consume_output(1)


def test_observer_legacy_256_cap_cannot_cover_792_clock_read_floor() -> None:
    payload = _limits_for("observer")
    dimensions = dict(payload["dimensions"])
    dimensions.update({"scheduler_ticks": 791, "clock_reads": 792})
    payload["dimensions"] = dimensions
    categories = dict(payload["http_category_limits"])
    categories["clock_read"] = 792
    payload["http_category_limits"] = categories
    binding = dict(payload["binding"])
    binding["allowance_remaining_http_attempts"] = 256
    payload["binding"] = binding

    with pytest.raises(OperationManifestError, match="allowance"):
        OperationManifest.from_mapping(payload)


def test_observer_order_mutation_is_rejected_before_alpaca_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("observer", admitted=True)
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    request = Mock(return_value="unexpected")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        with pytest.raises(OperationCapExceeded, match="entry_submit"):
            client._session.request(
                "POST",
                "https://paper-api.alpaca.markets/v2/orders",
                json={"symbol": "SPY", "qty": 1, "side": "buy", "type": "limit"},
            )

    request.assert_not_called()


def test_observer_market_data_is_symbol_scoped_and_page_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = OperationBudget(
        OperationManifest.from_mapping(_limits_for("observer", admitted=True))
    )
    request = Mock(return_value="response")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        for _ in range(4):
            client._session.request(
                "GET",
                "https://data.alpaca.markets/v2/stocks/SPY/bars",
                params={"symbols": ["SPY"]},
            )
        with pytest.raises(OperationCapExceeded, match="market_data_pages_per_symbol"):
            client._session.request(
                "GET",
                "https://data.alpaca.markets/v2/stocks/SPY/bars",
                params={"symbols": ["SPY"]},
            )
        with pytest.raises(OperationCapExceeded, match="market_data_read"):
            client._session.request(
                "GET",
                "https://data.alpaca.markets/v2/stocks/AAPL/bars",
                params={"symbols": ["AAPL"]},
            )

    assert request.call_count == 4


def test_observer_benchmark_pages_use_the_separate_trend_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = OperationBudget(
        OperationManifest.from_mapping(_limits_for("observer", admitted=True))
    )
    request = Mock(return_value="response")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        for _ in range(2):
            with market_trend_data_request():
                client._session.request(
                    "GET",
                    "https://data.alpaca.markets/v2/stocks/SPY/bars",
                    params={"symbols": ["SPY"]},
                )
        with market_trend_data_request(), pytest.raises(
            OperationCapExceeded, match="market_trend_pages_per_scan"
        ):
            client._session.request(
                "GET",
                "https://data.alpaca.markets/v2/stocks/SPY/bars",
                params={"symbols": ["SPY"]},
            )

    assert request.call_count == 2


def test_observer_receipt_event_and_byte_caps_latch_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("observer", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions.update({"receipt_events": 1, "output_bytes": 1})
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    observation = SchedulerObservation("offline")

    with activate_scheduler_observation(observation), activate_operation_budget(budget):
        with pytest.raises(OperationCapExceeded, match="output_bytes"):
            observation.record_event("scan", "SPY", "completed", {})

    assert budget.snapshot()["evidence_complete"] is False
    assert observation.to_receipt()["events"] == []
    assert observation.to_receipt()["service_health"] == "failed"


def test_observer_receipt_event_cap_latches_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("observer", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions.update({"receipt_events": 1, "output_bytes": 4096})
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    observation = SchedulerObservation("offline")

    with activate_scheduler_observation(observation), activate_operation_budget(budget):
        observation.record_event("scan", "SPY", "completed", {})
        with pytest.raises(OperationCapExceeded, match="receipt_events"):
            observation.record_event("scan", "SPY", "completed", {})
        budget.snapshot()

    assert len(observation.to_receipt()["events"]) == 1
    assert observation.to_receipt()["service_health"] == "failed"
    assert budget.snapshot()["evidence_complete"] is False


def test_observer_partial_snapshot_byte_cap_marks_health_failed() -> None:
    payload = _limits_for("observer", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions["output_bytes"] = 512
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    snapshot_path = Path(tempfile.gettempdir()) / f"observer-snapshot-{id(budget)}.json"
    observation = SchedulerObservation("offline", snapshot_path=snapshot_path)

    with activate_scheduler_observation(observation), activate_operation_budget(budget):
        observation.record_event("scan", "SPY", "completed", {})

    receipt = observation.to_receipt()
    assert receipt["service_health"] == "failed"
    assert receipt["snapshot_error"] == "OperationCapExceeded"
    assert not snapshot_path.exists()
    assert budget.snapshot()["evidence_complete"] is False


def test_direct_observer_scheduler_call_requires_admission_before_client_or_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scheduler

    now = datetime.now(scheduler._ET)
    client_factory = Mock(side_effect=AssertionError("client access before admission"))
    market_clock = Mock(side_effect=AssertionError("clock access before admission"))
    locked_run = Mock(side_effect=AssertionError("scheduler loop before admission"))
    monkeypatch.setattr(scheduler, "require_paper_mode", lambda: None)
    monkeypatch.setattr(scheduler, "_get_trading_client", client_factory)
    monkeypatch.setattr(scheduler, "_market_clock_is_open", market_clock)
    monkeypatch.setattr(scheduler, "_run_scheduler_locked", locked_run)

    with pytest.raises(OperationManifestError, match="active admitted observer operation budget"):
        scheduler.run_scheduler(
            dry_run=True,
            run_now=True,
            stop_after_session=True,
            observe_health=True,
            observation_stop_at=now + timedelta(minutes=1),
            observation_hard_deadline_at=now + timedelta(minutes=2),
        )

    client_factory.assert_not_called()
    market_clock.assert_not_called()
    locked_run.assert_not_called()


def test_observer_account_mismatch_stops_before_clock_or_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scheduler

    budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    observation = SchedulerObservation("offline")
    account_client = Mock()

    def get_account() -> SimpleNamespace:
        budget.reserve_http("account_read")
        return SimpleNamespace(id="another-paper-account")

    account_client.get_account.side_effect = get_account
    market_clock = Mock(side_effect=AssertionError("clock must follow account verification"))
    locked_run = Mock(side_effect=AssertionError("scan must follow account verification"))
    manifest = budget.manifest
    stop_at = datetime.fromisoformat(str(manifest.binding["session_stop"])).astimezone(
        scheduler._ET
    )
    start_at = datetime.fromisoformat(str(manifest.binding["session_start"])).astimezone(
        scheduler._ET
    )
    hard_deadline = stop_at + timedelta(
        seconds=manifest.deadlines_seconds["hard_deadline_grace_seconds"]
    )
    monkeypatch.setattr(scheduler, "require_paper_mode", lambda: None)
    monkeypatch.setattr(scheduler, "validate_runtime_binding", lambda *_: None)
    monkeypatch.setattr(settings, "load_runtime_credentials", lambda: None)
    monkeypatch.setattr(scheduler, "_get_trading_client", lambda: account_client)
    monkeypatch.setattr(scheduler, "_market_clock_is_open", market_clock)
    monkeypatch.setattr(scheduler, "_run_scheduler_locked", locked_run)

    with activate_scheduler_observation(observation), activate_operation_budget(budget):
        with pytest.raises(OperationManifestError, match="does not match"):
            scheduler.run_scheduler(
                dry_run=True,
                run_now=True,
                stop_after_session=True,
                observe_health=True,
                observation_stop_at=stop_at,
                observation_hard_deadline_at=hard_deadline,
            )

    account_client.get_account.assert_called_once_with()
    market_clock.assert_not_called()
    locked_run.assert_not_called()
    assert budget.snapshot()["http_category_attempts"]["account_read"] == 1
    assert observation.to_receipt()["service_health"] == "failed"
    assert start_at < stop_at


def test_budget_denial_does_not_invert_observation_and_budget_locks() -> None:
    budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    observation = SchedulerObservation("offline")
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def observation_then_budget() -> None:
        try:
            with observation._lock:
                barrier.wait(timeout=2)
                budget.consume("scheduler_ticks")
        except BaseException as exc:  # collected for assertion in the main thread
            failures.append(exc)

    def budget_then_denial() -> None:
        try:
            with budget._lock:
                barrier.wait(timeout=2)
                with pytest.raises(OperationCapExceeded):
                    budget.consume("clock_reads", amount=100)
        except BaseException as exc:  # collected for assertion in the main thread
            failures.append(exc)

    with activate_scheduler_observation(observation), activate_operation_budget(budget):
        threads = [
            threading.Thread(target=observation_then_budget, daemon=True),
            threading.Thread(target=budget_then_denial, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert budget.snapshot()["evidence_complete"] is False


def test_lifecycle_order_pagination_requires_bounded_page_and_counts_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("lifecycle", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions.update({"order_page_size": 2, "order_pages": 1})
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    request = Mock(return_value="page")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        client._session.request(
            "GET",
            "https://paper-api.alpaca.markets/v2/orders",
            params={"status": "open", "limit": 2},
        )
        with pytest.raises(OperationCapExceeded, match="order_pages"):
            client._session.request(
                "GET",
                "https://paper-api.alpaca.markets/v2/orders",
                params={"status": "open", "limit": 2},
            )

    request.assert_called_once()
    snapshot = budget.snapshot()
    assert snapshot["http_attempts"] == 1
    assert snapshot["http_category_attempts"]["open_order_read"] == 1


def test_lifecycle_order_page_size_rejection_precedes_transport_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _limits_for("lifecycle", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions["order_page_size"] = 2
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))
    request = Mock(return_value="unexpected")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)

    with activate_operation_budget(budget):
        with pytest.raises(OperationCapExceeded, match="order_page_size"):
            client._session.request(
                "GET",
                "https://paper-api.alpaca.markets/v2/orders",
                params={"status": "open", "limit": 3},
            )

    request.assert_not_called()
    assert budget.snapshot()["http_attempts"] == 0


def test_fmp_fake_transport_charges_one_allowed_attempt_and_denies_the_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib3.util.retry import Retry

    from core import data_client

    payload = _short_observer_limits()
    dimensions = dict(payload["dimensions"])
    dimensions["fmp_requests_per_symbol"] = 1
    payload["dimensions"] = dimensions
    categories = dict(payload["http_category_limits"])
    categories["fmp_read"] = 1
    payload["http_category_limits"] = categories
    budget = OperationBudget(OperationManifest.from_mapping(payload))

    initial_retries = Retry(total=3, connect=3, read=3, redirect=3, status=3)
    adapter = SimpleNamespace(max_retries=initial_retries)
    response = SimpleNamespace(
        status_code=200,
        raise_for_status=Mock(),
        json=Mock(return_value=[{"symbol": "SPY"}]),
    )
    transport = Mock(return_value=response)
    session = SimpleNamespace(
        get=transport,
        get_adapter=Mock(return_value=adapter),
    )
    data_client.clear_session_cache()
    monkeypatch.setattr(data_client, "_fmp_session", session)
    monkeypatch.setattr(data_client, "_fmp_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(settings, "FMP_PLAN", "paid")

    with activate_operation_budget(budget), data_client.fmp_request_budget(2):
        assert data_client._fmp_get("profile", {"symbol": "SPY"}) == [{"symbol": "SPY"}]
        with pytest.raises(OperationCapExceeded, match="fmp_requests_per_symbol"):
            data_client._fmp_get("profile", {"symbol": "SPY"})

    assert transport.call_count == 1
    assert transport.call_args.kwargs["allow_redirects"] is False
    assert transport.call_args.kwargs["timeout"] == 1.0
    assert adapter.max_retries is initial_retries
    snapshot = budget.snapshot()
    assert snapshot["http_attempts"] == 1
    assert snapshot["http_category_attempts"]["fmp_read"] == 1
    assert snapshot["symbol_dimension_counts"]["fmp_requests_per_symbol:SPY"] == 1


def test_observer_scan_tick_and_clock_caps_stop_before_extra_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scheduler

    scan_budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    auto_trader = Mock()
    monkeypatch.setattr(scheduler, "run_auto_trader", auto_trader)
    with activate_operation_budget(scan_budget):
        scheduler._run_observer_scan(["SPY"])
        with pytest.raises(OperationCapExceeded, match="scan_cycles"):
            scheduler._run_observer_scan(["SPY"])
    auto_trader.assert_called_once()

    tick_budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    with activate_operation_budget(tick_budget):
        scheduler.consume_operation_dimension("scheduler_ticks")
        scheduler.consume_operation_dimension("scheduler_ticks")
        with pytest.raises(OperationCapExceeded, match="scheduler_ticks"):
            scheduler.consume_operation_dimension("scheduler_ticks")

    clock_budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    request = Mock(return_value="clock")
    client = SimpleNamespace(
        _retry=0,
        _retry_wait=0,
        _session=SimpleNamespace(request=request),
    )
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(settings, "ALPACA_SDK_RETRY_WAIT_SECONDS", 0)
    configure_alpaca_rest_client(client)
    with activate_operation_budget(clock_budget):
        for _ in range(3):
            client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock")
        with pytest.raises(OperationCapExceeded, match="clock_reads"):
            client._session.request("GET", "https://paper-api.alpaca.markets/v2/clock")

    assert request.call_count == 3
    assert clock_budget.snapshot()["http_attempts"] == 3


def test_observer_stream_connection_and_event_caps_use_fake_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    import core.bounded_trading_stream as bounded_stream

    budget = OperationBudget(OperationManifest.from_mapping(_short_observer_limits()))
    connect_calls: list[int] = []

    async def fake_connect(_stream) -> None:
        connect_calls.append(1)

    monkeypatch.setattr(bounded_stream.TradingStream, "_connect", fake_connect)
    stream = object.__new__(bounded_stream.BudgetedTradingStream)
    stream._operation_budget = budget
    stream._should_run = True
    with activate_operation_budget(budget):
        asyncio.run(stream._connect())
        with pytest.raises(OperationCapExceeded, match="stream_connections"):
            asyncio.run(stream._connect())
    assert len(connect_calls) == 1
    assert stream._should_run is False

    callbacks: list[object] = []

    class FakeStream:
        def __init__(self, _budget, **_kwargs) -> None:
            self._should_run = True

        def subscribe_trade_updates(self, callback) -> None:
            callbacks.append(callback)

    monkeypatch.setattr(bounded_stream, "BudgetedTradingStream", FakeStream)
    event_payload = _short_observer_limits()
    event_dimensions = dict(event_payload["dimensions"])
    event_dimensions["stream_events"] = 1
    event_payload["dimensions"] = event_dimensions
    event_budget = OperationBudget(
        OperationManifest.from_mapping(event_payload)
    )
    event_observation = SchedulerObservation("offline")
    with (
        activate_scheduler_observation(event_observation),
        activate_operation_budget(event_budget),
    ):
        observer = bounded_stream.ReadOnlyTradeUpdateObserver(event_budget)
        order = SimpleNamespace(symbol="SPY", side="buy", type="limit")
        update = SimpleNamespace(event="fill", order=order)
        asyncio.run(callbacks[0](update))
        asyncio.run(callbacks[0](update))

    assert len(event_observation.to_receipt()["events"]) == 1
    assert observer._handler_fault is True
    assert observer._stream._should_run is False
    assert event_budget.snapshot()["evidence_complete"] is False


@pytest.mark.parametrize(
    ("output_limit", "force_output_denial", "expected_exit_code"),
    [(20000, False, 0), (128, True, 1)],
)
def test_observer_cli_result_carries_budget_state_and_fails_when_receipt_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_limit: int,
    force_output_denial: bool,
    expected_exit_code: int,
) -> None:
    import json

    import scheduler

    from core.operation_limits import current_operation_budget

    payload = _short_observer_limits()
    dimensions = dict(payload["dimensions"])
    dimensions["output_bytes"] = output_limit
    payload["dimensions"] = dimensions
    manifest = OperationManifest.from_mapping(payload)
    fixed_now = datetime.now(scheduler._ET)
    stop_at = datetime.fromisoformat(str(manifest.binding["session_stop"])).astimezone(
        scheduler._ET
    )
    hard_deadline = stop_at + timedelta(
        seconds=manifest.deadlines_seconds["hard_deadline_grace_seconds"]
    )
    observations: list[SchedulerObservation] = []

    class CapturingObservation(SchedulerObservation):
        def __init__(self, run_id: str, *, snapshot_path=None) -> None:
            super().__init__(run_id, snapshot_path=snapshot_path)
            observations.append(self)

    def stubbed_scheduler(**_kwargs) -> None:
        if force_output_denial:
            budget = current_operation_budget()
            assert budget is not None
            budget.consume_output(output_limit + 1)

    monkeypatch.setattr(scheduler, "SchedulerObservation", CapturingObservation)
    monkeypatch.setattr(scheduler, "load_operation_manifest", lambda *_a, **_kw: manifest)
    monkeypatch.setattr(scheduler, "validate_runtime_binding", lambda *_: None)
    monkeypatch.setattr(scheduler, "_now_et", lambda: fixed_now)
    monkeypatch.setattr(scheduler, "_record_observation_resource_snapshots", lambda *_: None)
    monkeypatch.setattr(scheduler, "run_scheduler", stubbed_scheduler)
    monkeypatch.setattr(scheduler, "fmp_observation_request_limit", lambda _cap: 0)
    monkeypatch.setattr(settings, "load_runtime_credentials", lambda: None)
    for name in ("NOTIFY_EMAIL_FROM", "NOTIFY_EMAIL_TO", "NOTIFY_EMAIL_PASSWORD"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.delenv("SCHEDULER_OBSERVATION_SNAPSHOT_PATH", raising=False)

    result = scheduler.main(
        [
            "--dry-run",
            "--now",
            "--session",
            "--observe-health",
            "--operation-manifest",
            "offline-test-manifest",
            "--observe-stop-at",
            stop_at.isoformat(),
            "--observe-hard-deadline-at",
            hard_deadline.isoformat(),
        ]
    )

    output = capsys.readouterr().out
    assert result == expected_exit_code, f"output={output!r}; receipt={observations[0].to_receipt() if observations else None}"
    assert len(observations) == 1
    receipt = observations[0].to_receipt()
    if force_output_denial:
        assert "SCHEDULER_OBSERVATION_RECEIPT=" not in output
        assert receipt["service_health"] == "failed"
        assert any(issue["code"] == "operation_output_cap_denied" for issue in receipt["issues"])
    else:
        line = next(
            item for item in output.splitlines()
            if item.startswith("SCHEDULER_OBSERVATION_RECEIPT=")
        )
        final_receipt = json.loads(line.split("=", 1)[1])
        assert final_receipt["evidence_complete"] is True
        assert final_receipt["operation_budget"]["evidence_complete"] is True
        assert final_receipt["service_health"] == "healthy"


def test_observer_console_output_is_charged_before_emission(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _limits_for("observer", admitted=True)
    dimensions = dict(payload["dimensions"])
    dimensions["output_bytes"] = 5
    payload["dimensions"] = dimensions
    budget = OperationBudget(OperationManifest.from_mapping(payload))

    with activate_operation_budget(budget), budgeted_standard_output(budget):
        print("abc")
        print("123")

    captured = capsys.readouterr()
    assert captured.out == "abc\n"
    assert budget.snapshot()["dimension_counts"]["output_bytes"] == 4
    assert budget.snapshot()["evidence_complete"] is False


def assert_protective_stop_recovery_obeys_operation_limits() -> None:
    """Exercise stop recovery, deadline, pass, poll, and cancel limits."""
    from core import order_execution as oe

    def make_budget(
        *,
        exit_wait: float,
        cancel_verify: float,
        passes: int,
        cancels: int,
        cancel_polls: int = 2,
    ) -> OperationBudget:
        payload = _limits_for("lifecycle", admitted=True)
        dimensions = dict(payload["dimensions"])
        dimensions.update(
            {
                "safety_recovery_passes": passes,
                "cancel_attempts_per_order": cancels,
                "eligible_order_ids": 3,
                "cancel_verification_polls": cancel_polls,
            }
        )
        payload["dimensions"] = dimensions
        deadlines = dict(payload["deadlines_seconds"])
        deadlines.update(
            {"exit_wait_seconds": exit_wait, "cancel_verify_seconds": cancel_verify}
        )
        payload["deadlines_seconds"] = deadlines
        return OperationBudget(OperationManifest.from_mapping(payload))

    pending = oe.ProtectiveStopResult(
        success=False,
        order_id="",
        symbol="SPY",
        qty=1.0,
        stop_price=93.0,
        action="pending_buy",
    )

    # Without a durable entry identifier, the recovery-pass cap must stop a
    # second broker inventory and reconciliation cycle.
    pass_budget = make_budget(
        exit_wait=2.0, cancel_verify=0.5, passes=1, cancels=1
    )
    pass_orders = Mock(return_value=[])
    pass_reconcile = Mock(return_value=pending)
    pass_sleep = Mock()
    pass_clock = iter((0.0, 0.1))
    with (
        patch(
            "core.order_execution.time.monotonic",
            side_effect=lambda: next(pass_clock),
        ),
        patch("core.order_execution.time.sleep", pass_sleep),
        patch("core.order_execution.get_open_orders", pass_orders),
        patch("core.order_execution.get_workflow", return_value=None),
        patch(
            "core.order_execution.reconcile_symbol_after_exit_failure",
            pass_reconcile,
        ),
        activate_operation_budget(pass_budget),
    ):
        pass_result = oe.ensure_protective_stop(
            "SPY",
            qty=1.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            workflow_id="wf-spy-budget-pass-cap",
        )

    assert pass_result.action == "pending_buy"
    assert pass_budget.snapshot()["dimension_counts"]["safety_recovery_passes"] == 1
    pass_orders.assert_called_once()
    pass_reconcile.assert_called_once()
    pass_sleep.assert_not_called()

    # A durable entry keeps reconciliation pending until the exit-wait
    # deadline, which must stop the next recovery inventory pass.
    deadline_budget = make_budget(
        exit_wait=0.5, cancel_verify=0.5, passes=2, cancels=1
    )
    no_orders = Mock(return_value=[])
    deadline_reconcile = Mock(return_value=pending)
    deadline_clock = iter((0.0, 0.1, 0.2, 0.6))
    with (
        patch(
            "core.order_execution.time.monotonic",
            side_effect=lambda: next(deadline_clock),
        ),
        patch("core.order_execution.time.sleep"),
        patch("core.order_execution.get_open_orders", no_orders),
        patch("core.order_execution.get_workflow", return_value=None),
        patch(
            "core.order_execution._wait_for_terminal_buy_order_chain",
            return_value=1.0,
        ) as wait_chain,
        patch("core.order_execution.reconcile_symbol_after_exit_failure", deadline_reconcile),
        activate_operation_budget(deadline_budget),
    ):
        deadline_result = oe.ensure_protective_stop(
            "SPY",
            qty=1.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            workflow_id="wf-spy-budget-deadline",
            entry_order_id="entry-deadline",
        )

    assert deadline_result.action == "reconciliation_failed"
    assert "exit_wait_seconds cap exceeded" in deadline_result.error
    deadline_snapshot = deadline_budget.snapshot()
    assert deadline_snapshot["dimension_counts"]["safety_recovery_passes"] == 1
    assert deadline_snapshot["denials"] == 1
    no_orders.assert_called_once()
    wait_chain.assert_called_once()
    deadline_reconcile.assert_called_once()

    # Carry an explicit stop override through ensure_protective_stop, its real
    # reconciliation path, and the real submit_stop_loss wrapper while the
    # lifecycle budget is active. Broker/persistence lookup boundaries,
    # stable broker snapshots, and UUID generation are faked; ensure,
    # reconciliation, submit_stop_loss, and budget accounting stay real.
    override_budget = make_budget(
        exit_wait=2.0, cancel_verify=0.5, passes=1, cancels=1
    )
    override_workflow_id = "wf-spy-budget-override"
    override_workflow = SimpleNamespace(
        workflow_id=override_workflow_id,
        transitions=[],
        mark_protective_stop=Mock(),
    )
    override_position = oe.PositionSummary("SPY", 1.0, 100.0, 100.0, 0.0)
    override_stop = SimpleNamespace(
        id="stop-override-budget",
        symbol="SPY",
        side="sell",
        type="stop",
        status="new",
        time_in_force="gtc",
        qty="1.0",
        filled_qty="0",
        stop_price="93.25",
        client_order_id="",
    )
    stop_requests = []

    def submit_override_request(request):
        stop_requests.append(request)
        override_stop.client_order_id = request.client_order_id
        override_stop.stop_price = str(request.stop_price)
        override_stop.qty = str(request.qty)
        return override_stop

    override_open_orders = Mock(side_effect=[[], [override_stop]])
    override_samples = Mock(
        side_effect=[
            (override_position, []),
            (override_position, [override_stop]),
        ]
    )
    with (
        patch("core.order_execution.get_open_orders", override_open_orders),
        patch("core.order_execution.get_workflow", return_value=override_workflow),
        patch(
            "core.order_execution._latest_unknown_stop_submission",
            return_value=(override_workflow, None, ""),
        ),
        patch("core.order_execution._sample_stable_symbol_state", override_samples),
        patch(
            "core.order_execution.get_execution_store",
            return_value=SimpleNamespace(
                load_pending_submission_intents=lambda **_: []
            ),
        ),
        patch(
            "core.order_execution._get_trading_client",
            return_value=SimpleNamespace(submit_order=submit_override_request),
        ),
        patch(
            "core.order_execution.uuid4",
            return_value=SimpleNamespace(hex="abcdef123456"),
        ),
        activate_operation_budget(override_budget),
    ):
        override_result = oe.ensure_protective_stop(
            "SPY",
            qty=1.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            workflow_id=override_workflow_id,
            stop_price_override=93.25,
        )

    assert override_result.success is True
    assert override_result.action == "submitted"
    assert override_result.order_id == "stop-override-budget"
    assert override_result.stop_price == 93.25
    assert len(stop_requests) == 1
    assert stop_requests[0].symbol == "SPY"
    assert stop_requests[0].qty == 1.0
    assert stop_requests[0].stop_price == 93.25
    assert override_stop.client_order_id == override_result.client_order_id
    assert override_open_orders.call_count == 2
    assert override_samples.call_count == 2
    override_snapshot = override_budget.snapshot()
    assert override_snapshot["dimension_counts"]["safety_recovery_passes"] == 1
    assert override_snapshot["eligible_order_ids"] == 1
    assert override_snapshot["denials"] == 0

    # A repeated BUY reaches the per-order cancel cap. The broker then reports
    # the order absent twice, proving no second cancel mutation was required.
    attempt_budget = make_budget(
        exit_wait=2.0,
        cancel_verify=2.0,
        passes=2,
        cancels=1,
        cancel_polls=4,
    )
    buy_order = SimpleNamespace(id="entry-cancel-cap", side="buy", status="new")
    open_buy = Mock(side_effect=([buy_order], [buy_order], [buy_order], [], [], []))
    cancel = Mock()
    recovered_protection = oe.ProtectiveStopResult(
        success=True,
        order_id="stop-existing",
        symbol="SPY",
        qty=1.0,
        stop_price=93.0,
        action="reused",
    )
    with (
        patch("core.order_execution.time.monotonic", return_value=0.0),
        patch("core.order_execution.time.sleep"),
        patch("core.order_execution.get_open_orders", open_buy),
        patch("core.order_execution.get_workflow", return_value=None),
        patch(
            "core.order_execution._wait_for_terminal_buy_order_chain",
            return_value=1.0,
        ),
        patch(
            "core.order_execution.reconcile_symbol_after_exit_failure",
            return_value=recovered_protection,
        ),
        patch(
            "core.order_execution._get_trading_client",
            return_value=SimpleNamespace(cancel_order_by_id=cancel),
        ),
        activate_operation_budget(attempt_budget),
    ):
        cancel_result = oe.ensure_protective_stop(
            "SPY",
            qty=1.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            workflow_id="wf-spy-budget-cancel",
        )

    assert cancel_result.success
    assert cancel_result.action == "reused"
    assert open_buy.call_count == 6
    cancel.assert_called_once_with("entry-cancel-cap")
    cancel_snapshot = attempt_budget.snapshot()
    assert cancel_snapshot["dimension_counts"]["safety_recovery_passes"] == 1
    assert cancel_snapshot["dimension_counts"]["cancel_verification_polls"] == 4
    assert cancel_snapshot["denials"] == 1

    # A separate one-poll budget must deny the next broker poll before it
    # reads the still-open order or attempts another cancellation.
    poll_budget = make_budget(
        exit_wait=2.0,
        cancel_verify=2.0,
        passes=2,
        cancels=1,
        cancel_polls=1,
    )
    poll_order = SimpleNamespace(id="entry-poll-cap", side="buy", status="new")
    poll_orders = Mock(return_value=[poll_order])
    poll_cancel = Mock()
    with (
        patch("core.order_execution.time.monotonic", return_value=0.0),
        patch("core.order_execution.time.sleep"),
        patch("core.order_execution.get_open_orders", poll_orders),
        patch(
            "core.order_execution._get_trading_client",
            return_value=SimpleNamespace(cancel_order_by_id=poll_cancel),
        ),
        activate_operation_budget(poll_budget),
    ):
        poll_result = oe.ensure_protective_stop(
            "SPY",
            qty=1.0,
            fill_price=100.0,
            stop_loss_pct=0.07,
            workflow_id="wf-spy-budget-poll-cap",
        )

    assert poll_result.action == "reconciliation_failed"
    assert "cancel_verification_polls cap exceeded" in poll_result.error
    assert poll_orders.call_count == 2
    poll_cancel.assert_called_once_with("entry-poll-cap")
    poll_snapshot = poll_budget.snapshot()
    assert poll_snapshot["dimension_counts"]["cancel_verification_polls"] == 1
    assert poll_snapshot["denials"] == 1



def test_protective_stop_recovery_obeys_operation_limits() -> None:
    assert_protective_stop_recovery_obeys_operation_limits()
