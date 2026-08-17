"""Index Ticker Fetcher Module.

Fetches stock tickers from major indices (Russell 2000, Nasdaq 100, S&P 500)
and caches them daily to avoid repeated API calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
import re
from bs4 import BeautifulSoup

from config import settings

# Cache configuration
CACHE_DIR = Path(settings.TICKER_CACHE_DIR)
CACHE_FILE = CACHE_DIR / "index_tickers_cache.json"
CACHE_EXPIRY_HOURS = settings.TICKER_CACHE_EXPIRY_HOURS

# Candidate column names for ticker identification
_TICKER_COLUMN_CANDIDATES = ["Ticker", "ticker", "Symbol", "symbol", "Constituent Symbol"]

# Fallback tickers when fetching fails
_FALLBACK_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]

# Sanity-check limits: iShares product URLs can occasionally point at broader
# funds than intended.  If parsed ticker count exceeds these limits, the fetcher
# falls back to an alternative source rather than sending thousands of invalid
# symbols through Alpaca validation.
_MAX_TICKERS_PER_INDEX: dict[str, int] = {
    "sp500": 550,       # S&P 500 has 503 members; allow headroom for changes
    "nasdaq100": 115,   # Nasdaq 100 has exactly 101 members; cap prevents URL drift
    "russell2000": 2100,  # Russell 2000 has ~2000 members
}

_MIN_TICKERS_PER_INDEX: dict[str, int] = {
    "sp500": 450,
    "nasdaq100": 90,
    "russell2000": 1500,
}

# Wikipedia URL for Nasdaq 100 constituents — used as fallback when the iShares
# URL returns an unexpectedly large set (iShares product 239696 appears to track
# a broader universe than strictly the Nasdaq-100 index).
_WIKIPEDIA_NASDAQ100_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
_WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _parse_wikipedia_tickers(response_text: str) -> List[str]:
    """Extract and normalize ticker symbols from a Wikipedia index table."""
    soup = BeautifulSoup(response_text, "html.parser")
    for table in soup.find_all("table", {"class": "wikitable"}):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        ticker_col_idx = next(
            (idx for idx, header in enumerate(headers) if header in ("ticker", "symbol")),
            None,
        )
        if ticker_col_idx is None:
            continue

        tickers: List[str] = []
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= ticker_col_idx:
                continue
            ticker = cells[ticker_col_idx].get_text(strip=True).upper()
            normalized_letters = ticker.replace(".", "").replace("-", "")
            if 1 <= len(ticker) <= 8 and normalized_letters.isalpha():
                tickers.append(ticker)
        if tickers:
            return list(dict.fromkeys(tickers))
    return []


def _fetch_index_from_wikipedia(index_key: str, display_name: str) -> List[str]:
    """Fetch a validated index universe from its Wikipedia component table."""
    urls = {
        "sp500": _WIKIPEDIA_SP500_URL,
        "nasdaq100": _WIKIPEDIA_NASDAQ100_URL,
    }
    url = urls.get(index_key)
    if not url:
        return []

    try:
        response = requests.get(
            url,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
        )
        response.raise_for_status()
        tickers = _parse_wikipedia_tickers(response.text)
        minimum = _MIN_TICKERS_PER_INDEX[index_key]
        maximum = _MAX_TICKERS_PER_INDEX[index_key]
        if minimum <= len(tickers) <= maximum:
            print(f"Fetched {len(tickers)} {display_name} tickers from Wikipedia")
            return tickers
        print(
            f"Wikipedia {display_name} fallback returned {len(tickers)} tickers; "
            f"expected {minimum}-{maximum}."
        )
    except Exception as exc:
        print(f"Wikipedia {display_name} fallback failed: {exc}")
    return []


def _find_ticker_column(df: pd.DataFrame) -> Optional[str]:
    """Find a valid ticker column from a list of known candidates.

    Args:
        df: DataFrame parsed from iShares CSV.

    Returns:
        The matching column name, or None if no candidate matches.
    """
    for candidate in _TICKER_COLUMN_CANDIDATES:
        if candidate in df.columns:
            return candidate
    # Fallback: case-insensitive substring search across all columns
    for col in df.columns:
        col_lower = col.strip().lower()
        if col_lower in ("ticker", "symbol") or "ticker" in col_lower or "symbol" in col_lower:
            return col
    return None


def _fetch_nasdaq100_from_wikipedia() -> List[str]:
    """Fetch Nasdaq 100 constituents from Wikipedia as a fallback source.

    Uses BeautifulSoup to parse the HTML table directly, avoiding the lxml
    dependency that pd.read_html() would require.

    Returns:
        List of Nasdaq 100 ticker symbols, or empty list on failure.
    """
    return _fetch_index_from_wikipedia("nasdaq100", "Nasdaq 100")


def _parse_ishares_csv(response_text: str, index_name: str) -> List[str]:
    """Parse an iShares CSV response into a list of ticker strings.

    Args:
        response_text: Raw CSV text from iShares.
        index_name: Human-readable index name for log messages.

    Returns:
        List of cleaned ticker symbols, or _FALLBACK_TICKERS on failure.
    """
    try:
        lines = response_text.split("\n")
        # Find the header row — look for any candidate column name
        header_idx = 0
        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(c.lower() in line_lower for c in _TICKER_COLUMN_CANDIDATES):
                header_idx = i
                break

        csv_content = "\n".join(lines[header_idx:])
        df = pd.read_csv(StringIO(csv_content))

        ticker_col = _find_ticker_column(df)
        if ticker_col is None:
            print(
                f"Error: Could not find a ticker column in {index_name} CSV. "
                f"Available columns: {list(df.columns)}. Using fallback tickers."
            )
            return list(_FALLBACK_TICKERS)

        raw = df[ticker_col].dropna().tolist()
        tickers = []
        for t in raw:
            if not isinstance(t, str):
                continue
            t = t.strip()
            # Alpaca accepts class-share symbols with a dot (for example BRK.B).
            # Remove any legal-text disclaimers or trailing hyphens
            if not t or len(t) > 8 or " " in t:
                continue
            t = t.upper()
            if t.endswith("-"):
                t = t[:-1]
            normalized_letters = t.replace(".", "").replace("-", "")
            if normalized_letters.isalpha():
                tickers.append(t)

        if not tickers:
            print(f"Warning: Parsed 0 tickers from {index_name} CSV. Using fallback tickers.")
            return list(_FALLBACK_TICKERS)

        print(f"Fetched {len(tickers)} {index_name} tickers from iShares")
        return tickers
    except Exception as e:
        print(f"Error parsing {index_name} CSV: {e}. Using fallback tickers.")
        return list(_FALLBACK_TICKERS)


class IndexTickerFetcher:
    """Fetches and caches stock tickers from major market indices."""

    # iShares ETF CSV download URLs
    ISHARES_URL = {
        "sp500": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/",
        "nasdaq100": "https://www.ishares.com/us/products/239696/ishares-nasdaq-100-etf/",
        "russell2000": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/",
    }

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        """Initialise the fetcher with an optional custom cache directory."""
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_file = self.cache_dir / "index_tickers_cache.json"
        self._ensure_cache_dir()

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _is_cache_valid(self) -> bool:
        if not self.cache_file.exists():
            return False

        try:
            with open(self.cache_file, encoding="utf-8") as f:
                cache_data = json.load(f)

            cache_time = datetime.fromisoformat(cache_data.get("timestamp", ""))
            expiry_time = cache_time + timedelta(hours=CACHE_EXPIRY_HOURS)
            return datetime.now() < expiry_time
        except (json.JSONDecodeError, ValueError, KeyError):
            return False

    def _load_cache(self) -> Optional[Dict]:
        if not self._is_cache_valid():
            return None

        try:
            with open(self.cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return None

    def _save_cache(self, data: Dict) -> None:
        data["timestamp"] = datetime.now().isoformat()
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _fetch_index_tickers(self, index_key: str, display_name: str) -> List[str]:
        """Fetch tickers for a given index from iShares CSV.

        Args:
            index_key: Key into ISHARES_URL dict (e.g. 'sp500').
            display_name: Human-readable name for logging.

        Returns:
            List of ticker symbols. Falls back to _FALLBACK_TICKERS on failure.
        """
        try:
            fund_url = self.ISHARES_URL[index_key]

            # 1. Fetch the main fund page
            page_resp = requests.get(
                fund_url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                },
            )
            page_resp.raise_for_status()

            # 2. Extract dynamic CSV link
            csv_url = None
            soup = BeautifulSoup(page_resp.text, "html.parser")
            download_link = soup.find("a", href=re.compile(r"\.ajax\?fileType=csv.*fileName=.*_holdings"))

            if download_link:
                csv_url = "https://www.ishares.com" + download_link["href"]
            else:
                # Fallback to regex search through whole payload or scripts just in case
                match = re.search(
                    r"(/us/products/[^\"]+\.ajax\?fileType=csv[^\"]*fileName=[^\"]*_holdings[^\"]*)", page_resp.text
                )
                if match:
                    csv_url = "https://www.ishares.com" + match.group(1)

            if not csv_url:
                raise ValueError(f"Could not locate CSV download link on {fund_url}")

            # 3. Fetch the CSV
            response = requests.get(
                csv_url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                },
            )

            if response.status_code == 200:
                tickers = _parse_ishares_csv(response.text, display_name)
                minimum = _MIN_TICKERS_PER_INDEX.get(index_key)
                if minimum and len(tickers) < minimum:
                    print(
                        f"[WARN] {display_name}: iShares returned only {len(tickers)} tickers "
                        f"(expected >={minimum}). Attempting alternative source."
                    )
                    return self._fetch_index_tickers_fallback(index_key, display_name) or tickers
                # Sanity-check: if iShares returns far more tickers than the index
                # has members, the product URL may have drifted to a broader fund.
                max_expected = _MAX_TICKERS_PER_INDEX.get(index_key)
                if max_expected and len(tickers) > max_expected:
                    print(
                        f"[WARN] {display_name}: iShares returned {len(tickers)} tickers "
                        f"(expected <={max_expected}). The product URL may point to a broader "
                        f"fund. Attempting alternative source."
                    )
                    return self._fetch_index_tickers_fallback(index_key, display_name) or tickers[:max_expected]
                return tickers
            else:
                print(
                    f"Error: iShares returned status {response.status_code} for {display_name}. "
                    "Attempting alternative source."
                )
                return self._fetch_index_tickers_fallback(index_key, display_name) or list(_FALLBACK_TICKERS)

        except Exception as e:
            print(f"Error fetching {display_name} from iShares: {e}. Attempting alternative source.")
            return self._fetch_index_tickers_fallback(index_key, display_name) or list(_FALLBACK_TICKERS)

    def _fetch_index_tickers_fallback(self, index_key: str, display_name: str) -> List[str]:
        """Attempt an alternative data source when the iShares URL misbehaves.

        Implemented for the S&P 500 and Nasdaq 100 (Wikipedia).
        Returns an empty list when no fallback is available.
        """
        if index_key in {"sp500", "nasdaq100"}:
            return _fetch_index_from_wikipedia(index_key, display_name)
        return []

    def fetch_sp500_tickers(self) -> List[str]:
        """Fetch S&P 500 tickers from iShares."""
        return self._fetch_index_tickers("sp500", "S&P 500")

    def fetch_nasdaq100_tickers(self) -> List[str]:
        """Fetch Nasdaq 100 tickers from iShares."""
        return self._fetch_index_tickers("nasdaq100", "Nasdaq 100")

    def fetch_russell2000_tickers(self) -> List[str]:
        """Fetch Russell 2000 tickers from iShares."""
        return self._fetch_index_tickers("russell2000", "Russell 2000")

    def fetch_all_index_tickers(self, indices: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """Fetch tickers for multiple indices and return as a keyed dict.

        Args:
            indices: Index keys to fetch (default: all three indices).

        Returns:
            Dict mapping index key → list of tickers.
        """
        if indices is None:
            indices = ["sp500", "nasdaq100", "russell2000"]

        result: Dict[str, List[str]] = {}

        for index in indices:
            index_lower = index.lower()
            if index_lower == "sp500":
                result["sp500"] = self.fetch_sp500_tickers()
            elif index_lower == "nasdaq100":
                result["nasdaq100"] = self.fetch_nasdaq100_tickers()
            elif index_lower == "russell2000":
                result["russell2000"] = self.fetch_russell2000_tickers()
            else:
                print(f"Unknown index: {index}")

        return result

    def get_all_tickers(
        self,
        indices: Optional[List[str]] = None,
        deduplicate: bool = True,
        force_refresh: bool = False,
    ) -> List[str]:
        """Return tickers for requested indices, using the cache when valid.

        Args:
            indices: Index keys to include (default: all three indices).
            deduplicate: Remove duplicate tickers across indices.
            force_refresh: Skip the cache and re-fetch from iShares.

        Returns:
            Flat list of ticker strings, optionally deduplicated.
        """
        # Check cache first (unless force refresh)
        if not force_refresh:
            cache_data = self._load_cache()
            if cache_data and "tickers" in cache_data:
                cached_indices = cache_data.get("indices", [])
                requested_indices = indices or ["sp500", "nasdaq100", "russell2000"]

                if set(requested_indices).issubset(set(cached_indices)):
                    cache_is_complete = all(
                        len(cache_data["tickers"].get(idx, []))
                        >= _MIN_TICKERS_PER_INDEX.get(idx, 1)
                        for idx in requested_indices
                    )
                    if cache_is_complete:
                        print(f"Using cached tickers from {cache_data.get('timestamp', 'unknown')}")
                        all_tickers: List[str] = []
                        for idx in requested_indices:
                            all_tickers.extend(cache_data["tickers"].get(idx, []))

                        if deduplicate:
                            return list(dict.fromkeys(all_tickers))
                        return all_tickers
                    print("[WARN] Cached ticker universe is incomplete; refreshing it.")

        # Fetch fresh data
        print("Fetching fresh ticker data from indices...")
        index_tickers = self.fetch_all_index_tickers(indices)

        self._save_cache(
            {
                "indices": list(index_tickers.keys()),
                "tickers": index_tickers,
            }
        )

        all_tickers = []
        for tickers in index_tickers.values():
            all_tickers.extend(tickers)

        if deduplicate:
            return list(dict.fromkeys(all_tickers))

        return all_tickers

    def get_tickers_by_index(self, index_name: str, force_refresh: bool = False) -> List[str]:
        """Return tickers for a single named index (no deduplication).

        Args:
            index_name: Index key (e.g. ``'sp500'``).
            force_refresh: Skip the cache.

        Returns:
            List of ticker strings for the requested index.
        """
        return self.get_all_tickers(indices=[index_name], deduplicate=False, force_refresh=force_refresh)

    def clear_cache(self) -> None:
        """Delete the on-disk ticker cache file."""
        if self.cache_file.exists():
            self.cache_file.unlink()
            print("Ticker cache cleared")


# ─── Module-level convenience functions ──────────────────────────────────────

_fetcher_instance: Optional[IndexTickerFetcher] = None


def get_fetcher() -> IndexTickerFetcher:
    """Return the module-level singleton IndexTickerFetcher instance."""
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = IndexTickerFetcher()
    return _fetcher_instance


def get_all_index_tickers(
    indices: Optional[List[str]] = None,
    force_refresh: bool = False,
) -> List[str]:
    """Return deduplicated tickers for all (or specified) indices.

    Args:
        indices: Index keys to fetch (default: sp500, nasdaq100, russell2000).
        force_refresh: Bypass the cache.

    Returns:
        Flat, deduplicated list of ticker strings.
    """
    return get_fetcher().get_all_tickers(indices=indices, force_refresh=force_refresh)


def get_sp500_tickers(force_refresh: bool = False) -> List[str]:
    """Return S&P 500 tickers, using the cache when valid."""
    return get_fetcher().get_tickers_by_index("sp500", force_refresh=force_refresh)


def get_nasdaq100_tickers(force_refresh: bool = False) -> List[str]:
    """Return Nasdaq 100 tickers, using the cache when valid."""
    return get_fetcher().get_tickers_by_index("nasdaq100", force_refresh=force_refresh)


def get_russell2000_tickers(force_refresh: bool = False) -> List[str]:
    """Return Russell 2000 tickers, using the cache when valid."""
    return get_fetcher().get_tickers_by_index("russell2000", force_refresh=force_refresh)


def clear_ticker_cache() -> None:
    """Clear the ticker cache."""
    get_fetcher().clear_cache()


if __name__ == "__main__":
    print("Testing Index Ticker Fetcher...")
    print("=" * 60)

    fetcher = IndexTickerFetcher()

    all_tickers = fetcher.get_all_tickers()
    print(f"\nTotal unique tickers: {len(all_tickers)}")

    sp500 = fetcher.get_tickers_by_index("sp500")
    nasdaq100 = fetcher.get_tickers_by_index("nasdaq100")
    russell2000 = fetcher.get_tickers_by_index("russell2000")

    print(f"S&P 500: {len(sp500)} tickers")
    print(f"Nasdaq 100: {len(nasdaq100)} tickers")
    print(f"Russell 2000: {len(russell2000)} tickers")

    print("\nSample tickers:")
    print(f"S&P 500: {sp500[:10]}")
    print(f"Nasdaq 100: {nasdaq100[:10]}")
    print(f"Russell 2000: {russell2000[:10]}")
