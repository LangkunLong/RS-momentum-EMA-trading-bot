"""Resilience tests for index-universe source fallbacks and cache quality."""

from unittest.mock import patch

import requests

from core.index_ticker_fetcher import (
    IndexTickerFetcher,
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

    assert _parse_wikipedia_tickers(html) == ["BRK-B", "AAPL"]


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
