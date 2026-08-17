"""Tests for FMP API resilience: graceful degradation on quota errors and disk cache."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import core.data_client as _dc_module
from core.data_client import (
    _fund_cache_get,
    _fund_cache_set,
    _fmp_get,
    fetch_quarterly_income_statement,
    fetch_annual_income_statement,
    fetch_company_info,
    _fetch_fmp_raw_history,
    _filter_records_as_of,
    clear_session_cache,
)
from core.canslim.c_current_earnings import evaluate_c
from core.fmp_provider import fetch_institutional_ownership_history, company_info_from_inst_history


@pytest.fixture(autouse=True)
def reset_fmp_session_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Reset FMP state and keep request accounting isolated from real usage."""
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "paid", raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_DAILY_REQUEST_BUDGET", 198, raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_FUND_CACHE_TTL_HOURS", 72, raising=False)
    monkeypatch.setattr(
        _dc_module.settings,
        "FMP_REQUEST_LEDGER_PATH",
        str(tmp_path / "fmp-request-usage.json"),
        raising=False,
    )
    _dc_module._fmp_quota_exhausted = False
    _dc_module._fmp_budget_warning_emitted = False
    _dc_module._fmp_unavailable_endpoints.clear()
    _dc_module._fmp_reported_endpoint_failures.clear()
    if hasattr(_dc_module, "reset_fmp_request_context"):
        _dc_module.reset_fmp_request_context()
    yield
    _dc_module._fmp_quota_exhausted = False
    _dc_module._fmp_budget_warning_emitted = False
    _dc_module._fmp_unavailable_endpoints.clear()
    _dc_module._fmp_reported_endpoint_failures.clear()


def _success_response(payload: object | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = [] if payload is None else payload
    return response


# ─── _fmp_get error handling ─────────────────────────────────────────────────


def test_fmp_get_returns_empty_on_402() -> None:
    """_fmp_get must return [] when the server responds with HTTP 402 (plan restriction)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 402

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on HTTP 402"


def test_fmp_get_returns_empty_on_403() -> None:
    """_fmp_get must return [] when the server responds with HTTP 403 (forbidden)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on HTTP 403"


def test_fmp_get_returns_empty_on_429_and_sets_session_skip() -> None:
    """HTTP 429 must degrade cleanly and set the session skip flag."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on HTTP 429"
    assert _dc_module._fmp_quota_exhausted is True


def test_fmp_get_returns_empty_on_retry_error() -> None:
    """_fmp_get must return [] when the retry adapter exhausts all 429 attempts.

    RetryError is raised before a response object exists, so it cannot be caught
    via status_code inspection — it must be caught at the requests.get() call site.
    """
    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.side_effect = requests.exceptions.RetryError("too many 429s")
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on RetryError"


def test_fmp_get_returns_empty_on_connection_error() -> None:
    """_fmp_get must return [] on network-level ConnectionError."""
    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.side_effect = requests.exceptions.ConnectionError("network down")
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == [], "Expected [] on ConnectionError"


def test_fmp_get_returns_empty_on_timeout() -> None:
    """A provider timeout must degrade like other network failures."""
    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        mock_session.get.side_effect = requests.exceptions.Timeout("timed out")
        result = _fmp_get("income-statement", {"symbol": "FAKE"})

    assert result == []


def test_fmp_get_does_not_inject_api_key_into_caller_params() -> None:
    """Credential injection must stay inside the HTTP request boundary."""
    caller_params = {"symbol": "FAKE"}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []

    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        mock_session.get.return_value = mock_resp
        result = _fmp_get("profile", caller_params)

    assert result == []
    assert caller_params == {"symbol": "FAKE"}
    request_params = mock_session.get.call_args.kwargs["params"]
    assert "apikey" in request_params


def test_fmp_get_402_logs_message(capsys) -> None:
    """HTTP 402 must print a plan-restriction message rather than silently returning []."""
    mock_resp = MagicMock()
    mock_resp.status_code = 402

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        _fmp_get("institutional-ownership/symbol-positions-summary", {"symbol": "FAKE"})

    captured = capsys.readouterr()
    assert "402" in captured.out, "Expected HTTP 402 to be mentioned in output"


def test_fmp_get_403_logs_message(capsys) -> None:
    """HTTP 403 must print an auth/permission message rather than silently returning []."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        _fmp_get("income-statement", {"symbol": "FAKE"})

    captured = capsys.readouterr()
    assert "403" in captured.out, "Expected HTTP 403 to be mentioned in output"


def test_fmp_get_404_logs_once_and_skips_repeated_endpoint_calls(capsys) -> None:
    """404 endpoints should be logged once, then suppressed for the rest of the session."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result_1 = _fmp_get("unavailable-endpoint", {"symbol": "FAKE"})
        result_2 = _fmp_get("unavailable-endpoint", {"symbol": "AGAIN"})

    assert result_1 == []
    assert result_2 == []
    assert mock_session.get.call_count == 1
    captured = capsys.readouterr()
    assert captured.out.count("HTTP 404") == 1


def test_clear_session_cache_resets_quota_circuit_breaker() -> None:
    """A new scan must retry FMP after an earlier scan exhausted its quota."""
    _dc_module._fmp_quota_exhausted = True

    clear_session_cache()

    assert _dc_module._fmp_quota_exhausted is False


def test_fmp_get_redacts_prepared_url_credentials(capsys) -> None:
    """HTTP errors must identify the endpoint without echoing query credentials."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = requests.HTTPError(
        "500 Server Error for url: https://example.invalid/profile?apikey=exposed-fixture-secret"
    )

    with patch("core.data_client._fmp_session") as mock_session:
        mock_session.get.return_value = mock_resp
        result = _fmp_get("profile", {"symbol": "AAPL"})

    output = capsys.readouterr().out
    assert result == []
    assert "profile" in output
    assert "500" in output
    assert "apikey=" not in output
    assert "exposed-fixture-secret" not in output


def test_free_plan_budget_hard_stops_before_network_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bot must reserve persisted quota before making an HTTP request."""
    ledger_path = tmp_path / "usage.json"
    fixed_now = datetime(2026, 8, 17, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_DAILY_REQUEST_BUDGET", 2, raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_REQUEST_LEDGER_PATH", str(ledger_path), raising=False)
    monkeypatch.setattr(_dc_module, "_fmp_now_et", lambda: fixed_now, raising=False)

    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        mock_session.get.return_value = _success_response()
        assert _fmp_get("income-statement", {"symbol": "AAPL"}) == []
        assert _fmp_get("income-statement", {"symbol": "MSFT"}) == []
        assert _fmp_get("income-statement", {"symbol": "NVDA"}) == []
        assert _fmp_get("income-statement", {"symbol": "AMZN"}) == []

    assert mock_session.get.call_count == 2
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["count"] == 2
    assert capsys.readouterr().out.count("daily request budget") == 1
    assert _dc_module.fmp_request_was_deferred() is True


def test_free_plan_budget_resets_at_next_3pm_eastern_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A request after 3 p.m. Eastern belongs to a new FMP reset window."""
    ledger_path = tmp_path / "usage.json"
    moments = iter(
        [
            datetime(2026, 8, 17, 14, 59, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 8, 17, 15, 1, tzinfo=ZoneInfo("America/New_York")),
        ]
    )
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_DAILY_REQUEST_BUDGET", 1, raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_REQUEST_LEDGER_PATH", str(ledger_path), raising=False)
    monkeypatch.setattr(_dc_module, "_fmp_now_et", lambda: next(moments), raising=False)

    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        mock_session.get.return_value = _success_response()
        _fmp_get("income-statement", {"symbol": "AAPL"})
        _fmp_get("income-statement", {"symbol": "MSFT"})

    usage = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert mock_session.get.call_count == 2
    assert usage["window_start"] == "2026-08-17T15:00:00-04:00"
    assert usage["count"] == 1


def test_free_plan_caps_limit_at_five_without_mutating_caller_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-tier requests must never ask FMP for more than five records."""
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    caller_params = {"symbol": "AAPL", "limit": 80}

    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        mock_session.get.return_value = _success_response()
        _fmp_get("income-statement", caller_params)

    assert caller_params == {"symbol": "AAPL", "limit": 80}
    assert mock_session.get.call_args.kwargs["params"]["limit"] == 5


@pytest.mark.parametrize("ledger_contents", ["not-json", "[]"])
def test_free_plan_corrupt_usage_ledger_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ledger_contents: str,
) -> None:
    """Unreadable accounting must block network use rather than reset usage to zero."""
    ledger_path = tmp_path / "usage.json"
    ledger_path.write_text(ledger_contents, encoding="utf-8")
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_REQUEST_LEDGER_PATH", str(ledger_path), raising=False)

    with (
        patch("core.data_client._fmp_session") as mock_session,
        patch("core.data_client._fmp_api_key", return_value="test-key"),
    ):
        result = _fmp_get("income-statement", {"symbol": "AAPL", "limit": 5})

    assert result == []
    mock_session.get.assert_not_called()
    assert "usage ledger" in capsys.readouterr().out
    assert _dc_module.fmp_request_was_deferred() is True


# ─── Disk fundamentals cache ─────────────────────────────────────────────────


def test_fund_cache_round_trip(tmp_path: Path) -> None:
    """A DataFrame stored via _fund_cache_set must be retrievable via _fund_cache_get."""
    df = pd.DataFrame({"Diluted EPS": [1.0, 2.0]}, index=["2024-01-01", "2024-04-01"])
    key = ("quarterly_income", "TESTCACHE", 5)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(key, df)
        result = _fund_cache_get(key)

    assert result is not None, "Expected cache HIT after writing"
    pd.testing.assert_frame_equal(result, df)


def test_fund_cache_miss_on_nonexistent_key(tmp_path: Path) -> None:
    """_fund_cache_get must return None when no cached file exists."""
    key = ("quarterly_income", "NOCACHE", 5)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        result = _fund_cache_get(key)

    assert result is None, "Expected cache MISS for key that was never written"


def test_fund_cache_miss_on_expired_file(tmp_path: Path) -> None:
    """_fund_cache_get must return None when the cached file is older than the TTL."""
    df = pd.DataFrame({"col": [1, 2]})
    key = ("quarterly_income", "EXPIREDCACHE", 5)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(key, df)
        from core.data_client import _fund_cache_path
        with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
            cache_file = Path(str(tmp_path)) / Path(_fund_cache_path(key)).name
        if cache_file.exists():
            stale_mtime = time.time() - (73 * 3600)
            import os
            os.utime(str(cache_file), (stale_mtime, stale_mtime))
            with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
                result = _fund_cache_get(key)
            assert result is None, "Expected cache MISS for stale file (>72h old)"


def test_free_plan_fund_cache_remains_fresh_for_seven_days(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A four-day-old successful statement should remain cached on the free plan."""
    df = pd.DataFrame({"Diluted EPS": [2.5]})
    key = ("quarterly_income", "AAPL", 5)
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    monkeypatch.setattr(_dc_module.settings, "FMP_FUND_CACHE_TTL_HOURS", 168, raising=False)

    with patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)):
        _fund_cache_set(key, df)
        cache_file = Path(_dc_module._fund_cache_path(key))
        four_days_ago = time.time() - (4 * 24 * 3600)
        os.utime(cache_file, (four_days_ago, four_days_ago))
        cached = _fund_cache_get(key)

    assert cached is not None
    pd.testing.assert_frame_equal(cached, df)


def test_fetch_quarterly_uses_disk_cache_on_second_call(tmp_path: Path) -> None:
    """Second call to fetch_quarterly_income_statement must use disk cache, not the API."""
    sample_records = [
        {
            "date": "2024-12-31",
            "epsDiluted": 2.5,
            "eps": 2.5,
            "revenue": 100_000_000,
            "netIncome": 20_000_000,
            "grossProfit": 50_000_000,
            "operatingIncome": 30_000_000,
            "costOfRevenue": 50_000_000,
        }
    ]

    with (
        patch("core.data_client._FUND_CACHE_DIR", str(tmp_path)),
        patch("core.data_client._fmp_get", return_value=sample_records) as mock_api,
    ):
        df1 = fetch_quarterly_income_statement("TESTSTOCK", limit=5)
        assert mock_api.call_count == 1

        clear_session_cache()

        df2 = fetch_quarterly_income_statement("TESTSTOCK", limit=5)
        assert mock_api.call_count == 1, "API was called again despite disk cache being populated"
        pd.testing.assert_frame_equal(df1, df2)


# ─── Richer quarterly history (paid plan) ────────────────────────────────────


def test_quarterly_twelve_records_all_loaded() -> None:
    """fetch_quarterly_income_statement with 12 records must produce 12 columns."""
    records = [
        {"date": f"202{y}-{q:02d}-30", "epsDiluted": float(y) + q * 0.1}
        for y, q in [(1, 3), (1, 6), (1, 9), (1, 12),
                     (2, 3), (2, 6), (2, 9), (2, 12),
                     (3, 3), (3, 6), (3, 9), (3, 12)]
    ]
    with patch("core.data_client._fmp_get", return_value=records):
        df = fetch_quarterly_income_statement("RICH", limit=12)

    assert not df.empty, "DataFrame must not be empty for 12 records"
    assert df.shape[1] == 12, f"Expected 12 period columns, got {df.shape[1]}"
    assert "Diluted EPS" in df.index


def test_quarterly_default_limit_is_paid_plan_value() -> None:
    """Default limit for quarterly statements must be FMP_QUARTERLY_LIMIT (12)."""
    from config import settings
    import inspect
    from core.data_client import fetch_quarterly_income_statement as fn

    sig = inspect.signature(fn)
    default = sig.parameters["limit"].default
    assert default == settings.FMP_QUARTERLY_LIMIT, (
        f"Expected default limit={settings.FMP_QUARTERLY_LIMIT}, got {default}"
    )


# ─── Richer annual history (paid plan) ───────────────────────────────────────


def test_annual_ten_records_all_loaded() -> None:
    """fetch_annual_income_statement with 10 records must produce 10 columns."""
    records = [
        {"date": f"{2015 + i}-12-31", "epsDiluted": 1.0 + i * 0.2}
        for i in range(10)
    ]
    with patch("core.data_client._fmp_get", return_value=records):
        df = fetch_annual_income_statement("RICH", limit=10)

    assert not df.empty
    assert df.shape[1] == 10, f"Expected 10 annual columns, got {df.shape[1]}"


def test_annual_default_limit_is_paid_plan_value() -> None:
    """Default limit for annual statements must be FMP_ANNUAL_LIMIT (10)."""
    from config import settings
    import inspect
    from core.data_client import fetch_annual_income_statement as fn

    sig = inspect.signature(fn)
    default = sig.parameters["limit"].default
    assert default == settings.FMP_ANNUAL_LIMIT


# ─── Institutional ownership history ─────────────────────────────────────────


def test_fetch_institutional_history_uses_stable_period_endpoint() -> None:
    """Institutional snapshots must use FMP's current period-specific stable API."""
    calls: list[tuple[str, dict]] = []
    period_data = {
        (2024, 4): [
            {
                "symbol": "AAPL",
                "investorsHolding": 4500,
                "lastInvestorsHolding": 4400,
                "ownershipPercent": 58.5,
            }
        ],
        (2024, 3): [
            {
                "symbol": "AAPL",
                "investorsHolding": 4400,
                "lastInvestorsHolding": 4300,
                "ownershipPercent": 57.1,
            }
        ],
    }

    def fake_fmp_get(endpoint, params=None):
        calls.append((endpoint, dict(params or {})))
        return period_data.get((params["year"], params["quarter"]), [])

    result = fetch_institutional_ownership_history(
        "AAPL",
        fmp_get_fn=fake_fmp_get,
        limit=2,
        as_of_date=date(2025, 4, 1),
    )

    assert calls == [
        (
            "institutional-ownership/symbol-positions-summary",
            {"symbol": "AAPL", "year": 2024, "quarter": 4},
        ),
        (
            "institutional-ownership/symbol-positions-summary",
            {"symbol": "AAPL", "year": 2024, "quarter": 3},
        ),
    ]
    assert [record["date"] for record in result] == ["2024-12-31", "2024-09-30"]
    assert result[0]["acceptedDate"] == "2025-02-19"
    assert result[0]["institution_count"] == 4500
    assert result[0]["prev_institution_count"] == 4400
    assert abs(result[0]["ownership_percent"] - 58.5) < 0.01


def test_live_institutional_snapshot_needs_one_successful_period_call() -> None:
    """A live snapshot should not multiply API traffic across historical quarters."""
    calls: list[dict] = []

    def fake_fmp_get(endpoint, params=None):
        calls.append(dict(params or {}))
        return [{"investorsHolding": 100, "lastInvestorsHolding": 90, "ownershipPercent": 40.0}]

    result = fetch_institutional_ownership_history(
        "AAPL",
        fmp_get_fn=fake_fmp_get,
        limit=1,
        as_of_date=date(2025, 4, 1),
    )

    assert len(calls) == 1
    assert len(result) == 1


def test_fetch_institutional_history_returns_empty_on_402() -> None:
    """fetch_institutional_ownership_history must return [] when endpoint unavailable."""
    def fake_fmp_get(endpoint, params=None):
        return []  # simulates 402 graceful degradation

    result = fetch_institutional_ownership_history("FAKE", fmp_get_fn=fake_fmp_get)
    assert result == []


def test_company_info_from_inst_history_converts_pct_to_decimal() -> None:
    """company_info_from_inst_history must convert ownership_percent to 0-1 decimal."""
    history = [
        {
            "date": "2024-09-30",
            "institution_count": 4500,
            "prev_institution_count": 4400,
            "ownership_percent": 62.3,
        }
    ]
    info = company_info_from_inst_history(history)

    assert info["held_percent_institutions"] is not None
    assert abs(info["held_percent_institutions"] - 0.623) < 0.001
    assert info["institution_count"] == 4500
    assert info["prev_institution_count"] == 4400


def test_fetch_company_info_populates_prev_institution_count() -> None:
    """fetch_company_info must populate prev_institution_count from quarterly history."""
    clear_session_cache()

    # Normalized records as returned by fetch_institutional_ownership_history
    fake_inst_history = [
        {
            "date": "2024-09-30",
            "institution_count": 3800,
            "prev_institution_count": 3700,
            "ownership_percent": 55.0,
        }
    ]

    with (
        patch("core.data_client._fmp_get", return_value=[{"marketCap": 1_000_000_000, "price": 100.0}]),
        patch("core.data_client._fetch_inst_ownership_history", return_value=fake_inst_history),
    ):
        info = fetch_company_info("TEST")

    assert info["prev_institution_count"] == 3700, (
        "prev_institution_count must be populated from quarterly ownership history"
    )
    assert info["institution_count"] == 3800
    assert info["held_percent_institutions"] is not None
    assert abs(info["held_percent_institutions"] - 0.55) < 0.001


def test_free_plan_company_info_uses_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live free-tier scoring skips profile and premium institutional calls."""
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    clear_session_cache()

    with (
        patch("core.data_client._fmp_get") as mock_get,
        patch("core.data_client._fetch_inst_ownership_history") as mock_institutional,
    ):
        info = fetch_company_info("AAPL")

    assert info == {
        "shares_outstanding": None,
        "held_percent_institutions": None,
        "institution_count": None,
        "prev_institution_count": None,
    }
    mock_get.assert_not_called()
    mock_institutional.assert_not_called()


# ─── Point-in-time institutional data filtering ───────────────────────────────


def test_institutional_pit_filters_future_quarters() -> None:
    """_filter_records_as_of must exclude institutional snapshots after as_of_date."""
    from datetime import datetime

    records = [
        {"date": "2024-03-31", "ownershipPercent": 55.0, "investorsHolding": 4000},
        {"date": "2024-06-30", "ownershipPercent": 57.0, "investorsHolding": 4200},
        {"date": "2024-09-30", "ownershipPercent": 59.0, "investorsHolding": 4500},
    ]
    as_of = datetime(2024, 7, 1)
    filtered = _filter_records_as_of(records, as_of)

    dates = [r["date"] for r in filtered]
    assert "2024-09-30" not in dates, "Q3 2024 data must be excluded when as_of is 2024-07-01"
    assert "2024-06-30" in dates, "Q2 2024 data must be included"
    assert "2024-03-31" in dates, "Q1 2024 data must be included"
    # newest-first order
    assert filtered[0]["date"] == "2024-06-30"


def test_institutional_pit_respects_assumed_public_availability_date() -> None:
    """A quarter-end snapshot must not be visible before its reporting lag."""
    from datetime import datetime

    records = [
        {
            "date": "2024-12-31",
            "acceptedDate": "2025-02-19",
            "ownership_percent": 58.5,
            "institution_count": 4500,
        }
    ]

    assert _filter_records_as_of(records, datetime(2025, 2, 18)) == []
    assert _filter_records_as_of(records, datetime(2025, 2, 19)) == records


def test_raw_history_includes_inst_ownership_key() -> None:
    """_fetch_fmp_raw_history result must include inst_ownership_raw key."""
    clear_session_cache()

    with patch("core.data_client._fmp_get", return_value=[]):
        raw = _fetch_fmp_raw_history("TESTKEY")

    assert "inst_ownership_raw" in raw, "raw history dict must contain inst_ownership_raw"


def test_free_plan_raw_history_skips_profile_and_institutional_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fundamental backtests on free mode use only the three statement requests."""
    monkeypatch.setattr(_dc_module.settings, "FMP_PLAN", "free", raising=False)
    clear_session_cache()
    calls: list[str] = []

    def fake_get(endpoint: str, params: dict | None = None) -> list:
        calls.append(endpoint)
        return []

    with (
        patch("core.data_client._fmp_get", side_effect=fake_get),
        patch("core.data_client._fetch_inst_ownership_history") as mock_institutional,
    ):
        raw = _fetch_fmp_raw_history("AAPL")

    assert calls == ["income-statement", "income-statement", "balance-sheet-statement"]
    assert raw["profile_raw"] == []
    assert raw["inst_ownership_raw"] == []
    mock_institutional.assert_not_called()


# ─── C score 4-quarter YoY fallback ─────────────────────────────────────────


def _make_quarterly_df(eps_values: list[float], dates: list[str]) -> pd.DataFrame:
    """Build a minimal quarterly income DataFrame matching the parser's output format."""
    data = {pd.Timestamp(d): {"Diluted EPS": v} for d, v in zip(dates, eps_values, strict=True)}
    return pd.DataFrame(data)


def test_c_score_four_quarter_fallback_valid_yoy() -> None:
    """evaluate_c must compute a score when exactly 4 quarters span >= 330 days (free-tier path)."""
    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.42],
        dates=["2023-12-31", "2024-03-31", "2024-06-30", "2024-12-31"],  # ~366-day span
    )

    score, growth = evaluate_c(df)

    assert score > 0, "Expected non-zero C score for valid 4-quarter data with 42% YoY growth"
    assert growth is not None
    assert abs(growth - 0.42) < 0.01, f"Expected ~42% growth, got {growth:.2%}"


def test_c_score_four_quarter_fallback_too_short_span() -> None:
    """evaluate_c must return 0 when 4 quarters span < 330 days (no valid YoY)."""
    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.30],
        dates=["2024-01-31", "2024-04-30", "2024-07-31", "2024-10-31"],  # ~274-day span
    )

    score, growth = evaluate_c(df)

    assert score == 0.0, "Expected 0 score when 4-quarter span is too short for valid YoY"


def test_c_score_missing_data_returns_zero() -> None:
    """evaluate_c must return (0.0, None) for an empty DataFrame."""
    score, growth = evaluate_c(pd.DataFrame())
    assert score == 0.0
    assert growth is None


def test_c_score_four_quarter_fallback_boundary_at_330_days() -> None:
    """evaluate_c should accept a span of exactly 330 days as a valid YoY."""
    import datetime

    start = datetime.date(2024, 1, 1)
    end = start + datetime.timedelta(days=330)

    df = _make_quarterly_df(
        eps_values=[1.00, 1.10, 1.20, 1.50],
        dates=[str(start), "2024-04-01", "2024-07-01", str(end)],
    )

    score, growth = evaluate_c(df)

    assert score > 0, "Expected non-zero score at exactly 330-day boundary"


def test_c_score_full_twelve_quarters_uses_yoy() -> None:
    """evaluate_c with 12 quarters must use proper YoY (not the 4-quarter fallback)."""
    # 12 quarters of data (3 years), each quarter up ~30% YoY
    dates = [
        "2022-03-31", "2022-06-30", "2022-09-30", "2022-12-31",
        "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    ]
    # EPS grows ~30% each year
    eps = [1.00, 1.05, 1.10, 1.15, 1.30, 1.37, 1.43, 1.50, 1.69, 1.78, 1.86, 1.95]
    df = _make_quarterly_df(eps_values=eps, dates=dates)

    score, growth = evaluate_c(df)

    assert score > 0, "Expected positive C score for 12 quarters with ~30% YoY growth"
    assert growth is not None
    # Most recent quarter: 1.95 / 1.50 - 1 ≈ 30%
    assert abs(growth - 0.30) < 0.02, f"Expected ~30% YoY growth, got {growth:.2%}"
