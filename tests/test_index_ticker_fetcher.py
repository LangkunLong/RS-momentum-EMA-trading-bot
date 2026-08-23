"""Resilience tests for index-universe source fallbacks and cache quality."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
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
