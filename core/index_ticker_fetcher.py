"""
Index Ticker Fetcher Module

Fetches stock tickers from major indices (Russell 2000, Nasdaq 100, S&P 500)
and caches them daily to avoid repeated API calls.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import pandas as pd
import requests

# Cache configuration
CACHE_DIR = Path("ticker_cache")
CACHE_FILE = CACHE_DIR / "index_tickers_cache.json"
CACHE_EXPIRY_HOURS = 24  # Cache expires after 24 hours


class IndexTickerFetcher:
    """Fetches and caches stock tickers from major market indices."""

    # Wikipedia URLs for index constituents
    ISHARES_URL = {
        "sp500": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
        "nasdaq100": "https://www.ishares.com/us/products/239696/ishares-nasdaq-100-etf/1467271812596.ajax?fileType=csv&fileName=QQQ_holdings&dataType=fund",
        "russell2000": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    }

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_file = self.cache_dir / "index_tickers_cache.json"
        self._ensure_cache_dir()

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _is_cache_valid(self) -> bool:
        if not self.cache_file.exists():
            return False

        try:
            with open(self.cache_file, "r") as f:
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
            with open(self.cache_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return None

    def _save_cache(self, data: Dict) -> None:
        data["timestamp"] = datetime.now().isoformat()
        with open(self.cache_file, "w") as f:
            json.dump(data, f, indent=2)

    def fetch_sp500_tickers(self) -> List[str]:
        try:
            url = self.ISHARES_URL["sp500"]

            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })

            if response.status_code == 200:
                # Parse CSV content
                from io import StringIO
                # Skip metadata rows at the top
                lines = response.text.split("\n")
                # Find the header row
                header_idx = 0
                for i, line in enumerate(lines):
                    if "Ticker" in line or "ticker" in line.lower():
                        header_idx = i
                        break

                csv_content = "\n".join(lines[header_idx:])
                df = pd.read_csv(StringIO(csv_content))

                # Find ticker column
                ticker_col = None
                for col in df.columns:
                    if "ticker" in col.lower():
                        ticker_col = col
                        break

                if ticker_col:
                    tickers = df[ticker_col].dropna().tolist()
                    tickers = [str(t).replace(".", "-") for t in tickers if isinstance(t, str) and t.strip()]
                    # Filter out non-ticker values
                    tickers = [t for t in tickers if t.isalpha() or "-" in t]
                    print(f"Fetched {len(tickers)} sp 500 tickers from iShares")
                    return tickers

        except Exception as e:
            print(f"Error fetching sp 500 from iShares: {e}")

    def fetch_nasdaq100_tickers(self) -> List[str]:
        try:
            url = self.ISHARES_URL["nasdaq100"]

            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })

            if response.status_code == 200:
                # Parse CSV content
                from io import StringIO
                # Skip metadata rows at the top
                lines = response.text.split("\n")
                # Find the header row
                header_idx = 0
                for i, line in enumerate(lines):
                    if "Ticker" in line or "ticker" in line.lower():
                        header_idx = i
                        break

                csv_content = "\n".join(lines[header_idx:])
                df = pd.read_csv(StringIO(csv_content))

                # Find ticker column
                ticker_col = None
                for col in df.columns:
                    if "ticker" in col.lower():
                        ticker_col = col
                        break

                if ticker_col:
                    tickers = df[ticker_col].dropna().tolist()
                    tickers = [str(t).replace(".", "-") for t in tickers if isinstance(t, str) and t.strip()]
                    # Filter out non-ticker values
                    tickers = [t for t in tickers if t.isalpha() or "-" in t]
                    print(f"Fetched {len(tickers)} Nasdaq 100 tickers from iShares")
                    return tickers

        except Exception as e:
            print(f"Error fetching Nasdaq 100 from iShares: {e}")
            

    def fetch_russell2000_tickers(self) -> List[str]:
        try:
            url = self.ISHARES_URL["russell2000"]

            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })

            if response.status_code == 200:
                # Parse CSV content
                from io import StringIO
                # Skip metadata rows at the top
                lines = response.text.split("\n")
                # Find the header row
                header_idx = 0
                for i, line in enumerate(lines):
                    if "Ticker" in line or "ticker" in line.lower():
                        header_idx = i
                        break

                csv_content = "\n".join(lines[header_idx:])
                df = pd.read_csv(StringIO(csv_content))

                # Find ticker column
                ticker_col = None
                for col in df.columns:
                    if "ticker" in col.lower():
                        ticker_col = col
                        break

                if ticker_col:
                    tickers = df[ticker_col].dropna().tolist()
                    tickers = [str(t).replace(".", "-") for t in tickers if isinstance(t, str) and t.strip()]
                    # Filter out non-ticker values
                    tickers = [t for t in tickers if t.isalpha() or "-" in t]
                    print(f"Fetched {len(tickers)} Russell 2000 tickers from iShares")
                    return tickers

        except Exception as e:
            print(f"Error fetching Russell 2000 from iShares: {e}")

    def fetch_all_index_tickers(self, indices: Optional[List[str]] = None) -> Dict[str, List[str]]:
        if indices is None:
            indices = ["sp500", "nasdaq100", "russell2000"]

        result = {}

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
        force_refresh: bool = False
    ) -> List[str]:
        # Check cache first (unless force refresh)
        if not force_refresh:
            cache_data = self._load_cache()
            if cache_data and "tickers" in cache_data:
                cached_indices = cache_data.get("indices", [])
                requested_indices = indices or ["sp500", "nasdaq100", "russell2000"]

                # If cached data covers all requested indices, use it
                if set(requested_indices).issubset(set(cached_indices)):
                    print(f"Using cached tickers from {cache_data.get('timestamp', 'unknown')}")
                    all_tickers = []
                    for idx in requested_indices:
                        all_tickers.extend(cache_data["tickers"].get(idx, []))

                    if deduplicate:
                        return list(dict.fromkeys(all_tickers))  # Preserve order
                    return all_tickers

        # Fetch fresh data
        print("Fetching fresh ticker data from indices...")
        index_tickers = self.fetch_all_index_tickers(indices)

        # Save to cache
        self._save_cache({
            "indices": list(index_tickers.keys()),
            "tickers": index_tickers,
        })

        # Combine all tickers
        all_tickers = []
        for tickers in index_tickers.values():
            all_tickers.extend(tickers)

        if deduplicate:
            return list(dict.fromkeys(all_tickers))  # Preserve order

        return all_tickers

    def get_tickers_by_index(self, index_name: str, force_refresh: bool = False) -> List[str]:
        return self.get_all_tickers(indices=[index_name], deduplicate=False, force_refresh=force_refresh)

    def clear_cache(self) -> None:
        if self.cache_file.exists():
            self.cache_file.unlink()
            print("Ticker cache cleared")


# Convenience functions for module-level access
_fetcher_instance: Optional[IndexTickerFetcher] = None


def get_fetcher() -> IndexTickerFetcher:
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = IndexTickerFetcher()
    return _fetcher_instance


def get_all_index_tickers(
    indices: Optional[List[str]] = None,
    force_refresh: bool = False
) -> List[str]:
    return get_fetcher().get_all_tickers(indices=indices, force_refresh=force_refresh)


def get_sp500_tickers(force_refresh: bool = False) -> List[str]:
    return get_fetcher().get_tickers_by_index("sp500", force_refresh=force_refresh)


def get_nasdaq100_tickers(force_refresh: bool = False) -> List[str]:
    return get_fetcher().get_tickers_by_index("nasdaq100", force_refresh=force_refresh)


def get_russell2000_tickers(force_refresh: bool = False) -> List[str]:
    return get_fetcher().get_tickers_by_index("russell2000", force_refresh=force_refresh)


def clear_ticker_cache() -> None:
    """Clear the ticker cache."""
    get_fetcher().clear_cache()


if __name__ == "__main__":
    # Test the fetcher
    print("Testing Index Ticker Fetcher...")
    print("=" * 60)

    fetcher = IndexTickerFetcher()

    # Test fetching all indices
    all_tickers = fetcher.get_all_tickers()
    print(f"\nTotal unique tickers: {len(all_tickers)}")

    # Show breakdown by index
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
