"""Resilience tests for index-universe source fallbacks and cache quality."""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
import requests

from core.scheduler_observation import (
    IndexRequestBudgetExceeded,
    SchedulerObservation,
    activate_scheduler_observation,
)
from core.index_ticker_fetcher import (
    IndexTickerFetcher,
    _fetch_index_from_wikipedia,
    _parse_wikipedia_tickers,
)


def test_wikipedia_parser_normalizes_share_class_symbols() -> None:
    html = """
    <table class="wikitable">
      <tr><th>Symbol</th><th>Security</th></tr>
      <tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr>
      <tr><td>AAPL</td><td>Apple</td></tr>
    </table>
    """

    assert _parse_wikipedia_tickers(html) == ["BRK.B", "AAPL"]


def test_ishares_403_uses_index_specific_fallback(tmp_path) -> None:
    response = requests.Response()
    response.status_code = 403
    response.url = "https://example.invalid/index"
    expected = [f"T{i}" for i in range(500)]
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)

    with (
        patch("core.index_ticker_fetcher.requests.get", return_value=response),
        patch.object(
            fetcher,
            "_fetch_index_tickers_fallback",
            return_value=expected,
        ) as fallback,
    ):
        result = fetcher._fetch_index_tickers("sp500", "S&P 500")

    assert result == expected
    fallback.assert_called_once_with("sp500", "S&P 500")


def test_valid_wikipedia_fallback_is_recorded_after_ishares_refusal(tmp_path) -> None:
    response = requests.Response()
    response.status_code = 403
    response.url = "https://example.invalid/index"
    expected = [f"T{i}" for i in range(100)]
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    observation = SchedulerObservation("index-alternate-source")

    with (
        patch("core.index_ticker_fetcher.requests.get", return_value=response),
        patch.object(fetcher, "_fetch_index_tickers_fallback", return_value=expected),
        activate_scheduler_observation(observation),
    ):
        result = fetcher._fetch_index_tickers("nasdaq100", "Nasdaq 100")

    assert result == expected
    receipt = observation.to_receipt()
    assert receipt["service_health"] == "healthy"
    assert any(
        event["kind"] == "index_request"
        and event["key"] == "ishares_page"
        and event["details"]["http_status"] == 403
        for event in receipt["events"]
    )
    assert any(
        event["kind"] == "index_universe"
        and event["status"] == "fallback_selected"
        and event["details"]["source"] == "wikipedia"
        for event in receipt["events"]
    )


def test_total_index_source_failure_marks_full_universe_unavailable(tmp_path) -> None:
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    observation = SchedulerObservation("index-no-source")

    with (
        patch(
            "core.index_ticker_fetcher.requests.get",
            side_effect=requests.Timeout("offline"),
        ) as get,
        activate_scheduler_observation(observation),
    ):
        result = fetcher._fetch_index_tickers("russell2000", "Russell 2000")

    receipt = observation.to_receipt()
    assert result == ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
    get.assert_called_once()
    assert receipt["service_health"] == "failed"
    assert receipt["required_input_coverage"] == "failed"
    assert any(
        event["kind"] == "index_universe" and event["status"] == "unavailable"
        for event in receipt["events"]
    )
    assert receipt["input_gaps"] == [
        {
            "symbol": "russell2000",
            "endpoint": "index-universe",
            "reason": "no_complete_index_source",
            "coverage_status": "failed",
        }
    ]


def test_observation_source_failure_does_not_overwrite_complete_index_cache(tmp_path) -> None:
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    original = {
        "timestamp": datetime.now().isoformat(),
        "indices": ["russell2000"],
        "tickers": {"russell2000": [f"R{i}" for i in range(2000)]},
    }
    fetcher.cache_file.write_text(json.dumps(original), encoding="utf-8")
    before = fetcher.cache_file.read_bytes()
    observation = SchedulerObservation("preserve-index-cache")

    with (
        patch(
            "core.index_ticker_fetcher.requests.get",
            side_effect=requests.Timeout("offline"),
        ),
        activate_scheduler_observation(observation),
    ):
        fetcher.get_all_tickers(indices=["russell2000"], force_refresh=True)

    receipt = observation.to_receipt()
    assert observation.to_receipt()["service_health"] == "failed"
    assert fetcher.cache_file.read_bytes() == before
    assert any(
        event["kind"] == "index_cache"
        and event["status"] == "write_skipped"
        and event["details"]["indices"] == ["russell2000"]
        for event in receipt["events"]
    )


def test_observation_preserves_cache_for_rejected_max_sized_ishares_slice(tmp_path) -> None:
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    original = {
        "timestamp": datetime.now().isoformat(),
        "indices": ["nasdaq100"],
        "tickers": {"nasdaq100": [f"OLD{i}" for i in range(100)]},
    }
    fetcher.cache_file.write_text(json.dumps(original), encoding="utf-8")
    before = fetcher.cache_file.read_bytes()
    page = Mock(status_code=200)
    page.text = '<a href="/us/products/239696/etf.ajax?fileType=csv&fileName=fund_holdings.csv">CSV</a>'
    page.raise_for_status.return_value = None
    csv = Mock(status_code=200, text="unused")
    csv.raise_for_status.return_value = None
    observation = SchedulerObservation("oversized-index-slice")

    with (
        patch("core.index_ticker_fetcher.requests.get", side_effect=[page, csv]),
        patch(
            "core.index_ticker_fetcher._parse_ishares_csv",
            return_value=[f"NEW{i}" for i in range(116)],
        ),
        patch.object(fetcher, "_fetch_index_tickers_fallback", return_value=[]),
        patch.object(fetcher, "_save_cache") as save_cache,
        activate_scheduler_observation(observation),
    ):
        result = fetcher.get_all_tickers(indices=["nasdaq100"], force_refresh=True)

    assert len(result) == 115
    save_cache.assert_not_called()
    assert fetcher.cache_file.read_bytes() == before
    receipt = observation.to_receipt()
    assert receipt["required_input_coverage"] == "failed"
    assert any(
        event["kind"] == "index_cache"
        and event["status"] == "write_skipped"
        and event["details"]["indices"] == ["nasdaq100"]
        for event in receipt["events"]
    )


def test_ishares_page_and_csv_attempts_are_counted(tmp_path) -> None:
    tickers = [f"T{chr(65 + index // 26)}{chr(65 + index % 26)}" for index in range(100)]
    page = Mock(status_code=200)
    page.text = '<a href="/us/products/239696/etf.ajax?fileType=csv&fileName=fund_holdings.csv">CSV</a>'
    page.raise_for_status.return_value = None
    csv = Mock(status_code=200, text="Ticker\n" + "\n".join(tickers))
    get = Mock(side_effect=[page, csv])
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    observation = SchedulerObservation("index-pages")

    with patch("core.index_ticker_fetcher.requests.get", get), activate_scheduler_observation(observation):
        result = fetcher._fetch_index_tickers("nasdaq100", "Nasdaq 100")

    assert result == tickers
    assert get.call_count == 2
    assert get.call_args_list[1].args[0].endswith("fund_holdings.csv")
    assert observation.to_receipt()["resource_counters"]["index_attempts"] == 2


def test_wikipedia_fallback_attempt_is_counted_after_ishares_refusal(tmp_path) -> None:
    tickers = [f"T{chr(65 + index // 26)}{chr(65 + index % 26)}" for index in range(100)]
    html = (
        '<table class="wikitable"><tr><th>Symbol</th><th>Security</th></tr>'
        + "".join(f"<tr><td>{ticker}</td><td>Company</td></tr>" for ticker in tickers)
        + "</table>"
    )
    refused = Mock(status_code=403)
    refused.raise_for_status.side_effect = requests.HTTPError("forbidden")
    wikipedia = Mock(status_code=200, text=html)
    wikipedia.raise_for_status.return_value = None
    get = Mock(side_effect=[refused, wikipedia])
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    observation = SchedulerObservation("index-fallback")

    with patch("core.index_ticker_fetcher.requests.get", get), activate_scheduler_observation(observation):
        result = fetcher._fetch_index_tickers("nasdaq100", "Nasdaq 100")

    assert result == tickers
    assert get.call_count == 2
    assert observation.to_receipt()["resource_counters"]["index_attempts"] == 2


def test_failed_index_request_consumes_one_attempt(tmp_path) -> None:
    get = Mock(side_effect=requests.Timeout("offline"))
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    observation = SchedulerObservation("index-failed")

    with patch("core.index_ticker_fetcher.requests.get", get), activate_scheduler_observation(observation):
        result = fetcher._fetch_index_tickers("russell2000", "Russell 2000")

    assert result == ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
    get.assert_called_once()
    assert observation.to_receipt()["resource_counters"]["index_attempts"] == 1


def test_seventh_index_request_is_denied_before_io() -> None:
    response = Mock(status_code=500)
    response.raise_for_status.side_effect = requests.HTTPError("unavailable")
    get = Mock(return_value=response)
    observation = SchedulerObservation("index-cap")

    with patch("core.index_ticker_fetcher.requests.get", get), activate_scheduler_observation(observation):
        for _ in range(6):
            _fetch_index_from_wikipedia("sp500", "S&P 500")
        with pytest.raises(IndexRequestBudgetExceeded):
            _fetch_index_from_wikipedia("sp500", "S&P 500")

    receipt = observation.to_receipt()
    assert get.call_count == 6
    assert receipt["resource_counters"]["index_attempts"] == 6
    assert receipt["resource_denials"] == {"index_cap_denied": 1}
    assert receipt["service_health"] == "failed"


def test_degraded_cached_universe_is_refetched(tmp_path) -> None:
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    cached = {
        "timestamp": "2026-08-16T10:00:00",
        "indices": ["sp500"],
        "tickers": {"sp500": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]},
    }
    refreshed = {"sp500": [f"T{i}" for i in range(500)]}

    with (
        patch.object(fetcher, "_load_cache", return_value=cached),
        patch.object(fetcher, "fetch_all_index_tickers", return_value=refreshed) as fetch,
    ):
        result = fetcher.get_all_tickers(indices=["sp500"])

    assert len(result) == 500
    fetch.assert_called_once_with(["sp500"])


def test_sequential_index_refreshes_preserve_complete_cached_indices(tmp_path) -> None:
    """Refreshing a second index must not evict a complete first index from disk."""
    sp500 = [f"S{i}" for i in range(500)]
    nasdaq100 = [f"N{i}" for i in range(100)]
    universes = {"sp500": sp500, "nasdaq100": nasdaq100}
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)

    def fetch_requested(indices: list[str] | None = None) -> dict[str, list[str]]:
        assert indices is not None
        return {index: universes[index] for index in indices}

    with patch.object(fetcher, "fetch_all_index_tickers", side_effect=fetch_requested):
        assert fetcher.get_tickers_by_index("sp500") == sp500
        assert fetcher.get_tickers_by_index("nasdaq100") == nasdaq100

    reloaded = IndexTickerFetcher(cache_dir=tmp_path)
    with patch.object(
        reloaded,
        "fetch_all_index_tickers",
        side_effect=AssertionError("complete cached indices must not be fetched again"),
    ):
        assert reloaded.get_tickers_by_index("sp500") == sp500
        assert reloaded.get_tickers_by_index("nasdaq100") == nasdaq100


def test_partial_refresh_does_not_extend_retained_index_cache_ttl(tmp_path) -> None:
    """Merging fresh symbols must retain the oldest cached index timestamp."""
    cached_at = (datetime.now() - timedelta(hours=1)).isoformat()
    sp500 = [f"S{i}" for i in range(500)]
    nasdaq100 = [f"N{i}" for i in range(100)]
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    fetcher.cache_file.write_text(
        json.dumps(
            {
                "timestamp": cached_at,
                "indices": ["sp500"],
                "tickers": {"sp500": sp500},
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        fetcher,
        "fetch_all_index_tickers",
        return_value={"nasdaq100": nasdaq100},
    ):
        assert fetcher.get_tickers_by_index("nasdaq100", force_refresh=True) == nasdaq100

    saved = json.loads(fetcher.cache_file.read_text(encoding="utf-8"))
    assert saved["timestamp"] == cached_at
    assert saved["tickers"] == {"sp500": sp500, "nasdaq100": nasdaq100}


def test_complete_refresh_renews_index_cache_ttl(tmp_path) -> None:
    """Refreshing every cached index must timestamp the fully fresh snapshot."""
    cached_at = (datetime.now() - timedelta(hours=23)).isoformat()
    old_sp500 = [f"OLD{i}" for i in range(500)]
    fresh_sp500 = [f"NEW{i}" for i in range(500)]
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)
    fetcher.cache_file.write_text(
        json.dumps(
            {
                "timestamp": cached_at,
                "indices": ["sp500"],
                "tickers": {"sp500": old_sp500},
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        fetcher,
        "fetch_all_index_tickers",
        return_value={"sp500": fresh_sp500},
    ):
        assert fetcher.get_tickers_by_index("sp500", force_refresh=True) == fresh_sp500

    saved = json.loads(fetcher.cache_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(saved["timestamp"]) > datetime.fromisoformat(cached_at)
    assert saved["tickers"] == {"sp500": fresh_sp500}


@pytest.mark.integration
def test_live_large_cap_sources_return_complete_universes(tmp_path) -> None:
    """A live source or its live fallback must return realistic large-cap universes."""
    fetcher = IndexTickerFetcher(cache_dir=tmp_path)

    sp500 = fetcher.get_tickers_by_index("sp500", force_refresh=True)
    nasdaq100 = fetcher.get_tickers_by_index("nasdaq100", force_refresh=True)

    assert len(sp500) >= 450
    assert len(nasdaq100) >= 90
    assert all(isinstance(ticker, str) and ticker for ticker in sp500 + nasdaq100)
