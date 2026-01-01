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
    WIKIPEDIA_URLS = {
        "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
        "russell2000": "https://en.wikipedia.org/wiki/Russell_2000_Index",
    }

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize the fetcher with optional custom cache directory."""
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_file = self.cache_dir / "index_tickers_cache.json"
        self._ensure_cache_dir()

    def _ensure_cache_dir(self) -> None:
        """Create cache directory if it doesn't exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _is_cache_valid(self) -> bool:
        """Check if cache exists and is not expired."""
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
        """Load cached ticker data."""
        if not self._is_cache_valid():
            return None

        try:
            with open(self.cache_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return None

    def _save_cache(self, data: Dict) -> None:
        """Save ticker data to cache."""
        data["timestamp"] = datetime.now().isoformat()
        with open(self.cache_file, "w") as f:
            json.dump(data, f, indent=2)

    def fetch_sp500_tickers(self) -> List[str]:
        """
        Fetch S&P 500 constituent tickers from Wikipedia.

        Returns:
            List of ticker symbols
        """
        try:
            tables = pd.read_html(self.WIKIPEDIA_URLS["sp500"])
            # First table contains the S&P 500 constituents
            df = tables[0]
            # Symbol column may be named 'Symbol' or 'Ticker'
            symbol_col = "Symbol" if "Symbol" in df.columns else "Ticker"
            tickers = df[symbol_col].tolist()
            # Clean up tickers (remove any BRK.B -> BRK-B format issues)
            tickers = [t.replace(".", "-") for t in tickers]
            print(f"Fetched {len(tickers)} S&P 500 tickers")
            return tickers
        except Exception as e:
            print(f"Error fetching S&P 500 tickers: {e}")
            return []

    def fetch_nasdaq100_tickers(self) -> List[str]:
        """
        Fetch Nasdaq 100 constituent tickers from Wikipedia.

        Returns:
            List of ticker symbols
        """
        try:
            tables = pd.read_html(self.WIKIPEDIA_URLS["nasdaq100"])
            # Find the table with ticker symbols (usually has 'Ticker' or 'Symbol' column)
            for table in tables:
                if "Ticker" in table.columns:
                    tickers = table["Ticker"].tolist()
                    tickers = [t.replace(".", "-") for t in tickers]
                    print(f"Fetched {len(tickers)} Nasdaq 100 tickers")
                    return tickers
                elif "Symbol" in table.columns:
                    tickers = table["Symbol"].tolist()
                    tickers = [t.replace(".", "-") for t in tickers]
                    print(f"Fetched {len(tickers)} Nasdaq 100 tickers")
                    return tickers

            # Fallback: try the 5th table which often has the components
            if len(tables) >= 5:
                df = tables[4]
                for col in df.columns:
                    if df[col].dtype == object and df[col].str.match(r"^[A-Z]+$").any():
                        tickers = df[col].dropna().tolist()
                        tickers = [t for t in tickers if isinstance(t, str) and t.isupper()]
                        if len(tickers) > 50:  # Likely the ticker column
                            print(f"Fetched {len(tickers)} Nasdaq 100 tickers")
                            return tickers

            print("Could not find Nasdaq 100 tickers table")
            return []
        except Exception as e:
            print(f"Error fetching Nasdaq 100 tickers: {e}")
            return []

    def fetch_russell2000_tickers(self) -> List[str]:
        """
        Fetch Russell 2000 constituent tickers.

        Note: Russell 2000 constituents are not freely available on Wikipedia.
        We use the iShares Russell 2000 ETF (IWM) holdings as a proxy,
        or fall back to fetching from other sources.

        Returns:
            List of ticker symbols
        """
        try:
            # Try to get from iShares IWM holdings page
            # This is a common proxy for Russell 2000
            url = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"

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

        # Fallback: Use a curated list of representative small-cap stocks
        # This is a subset but covers key small-cap names
        print("Using fallback Russell 2000 representative list")
        return self._get_russell2000_fallback()

    def _get_russell2000_fallback(self) -> List[str]:
        """
        Return a fallback list of representative Russell 2000 stocks.
        This is used when live fetching fails.
        """
        # Representative small-cap stocks (top holdings of IWM)
        return [
            "SMCI", "ONTO", "FN", "TGTX", "ANF", "CELH", "DOCS", "BOOT", "LNTH",
            "PIPR", "CSWI", "POWL", "MC", "ACIW", "CRVL", "ESNT", "SPSC", "CALM",
            "BGC", "CVLT", "EXPO", "SFM", "SBCF", "PRGS", "IIPR", "ITRI", "ABG",
            "ALKS", "VCYT", "AMRX", "GKOS", "CTRE", "KBH", "VCTR", "COOP", "CNMD",
            "AGIO", "STEP", "MMSI", "RDNT", "HWKN", "CERT", "GPI", "JANX", "PINC",
            "LPG", "NVEE", "OII", "PTEN", "VBTX", "CADE", "SSTK", "WDFC", "SIG",
            "PAYO", "TDS", "MSGS", "RELY", "CORT", "CVBF", "ARLO", "HLIT", "ATGE",
            "NBTB", "BCPC", "WSBC", "SHOO", "KLIC", "INDB", "LBRT", "ASO", "INVA",
            "TRMK", "PTGX", "STNG", "LBPH", "AROC", "LAUR", "CATY", "AMPH", "CPK",
            "AWR", "BLFS", "CNXN", "INSM", "PECO", "ROIC", "TOWN", "VECO", "WHD",
        ]

    def fetch_all_index_tickers(self, indices: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """
        Fetch tickers from specified indices or all major indices.

        Args:
            indices: List of index names to fetch. Options: 'sp500', 'nasdaq100', 'russell2000'
                    If None, fetches all indices.

        Returns:
            Dict mapping index name to list of tickers
        """
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
        """
        Get all tickers from specified indices, with caching.

        Args:
            indices: List of index names to fetch. Options: 'sp500', 'nasdaq100', 'russell2000'
                    If None, fetches all indices.
            deduplicate: If True, removes duplicate tickers across indices
            force_refresh: If True, bypasses cache and fetches fresh data

        Returns:
            List of unique ticker symbols
        """
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
        """
        Get tickers for a specific index.

        Args:
            index_name: Index name ('sp500', 'nasdaq100', 'russell2000')
            force_refresh: If True, bypasses cache

        Returns:
            List of ticker symbols for the specified index
        """
        return self.get_all_tickers(indices=[index_name], deduplicate=False, force_refresh=force_refresh)

    def clear_cache(self) -> None:
        """Clear the ticker cache."""
        if self.cache_file.exists():
            self.cache_file.unlink()
            print("Ticker cache cleared")


# Convenience functions for module-level access
_fetcher_instance: Optional[IndexTickerFetcher] = None


def get_fetcher() -> IndexTickerFetcher:
    """Get or create the singleton fetcher instance."""
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = IndexTickerFetcher()
    return _fetcher_instance


def get_all_index_tickers(
    indices: Optional[List[str]] = None,
    force_refresh: bool = False
) -> List[str]:
    """
    Get all tickers from major indices with daily caching.

    Args:
        indices: Optional list of indices to fetch ('sp500', 'nasdaq100', 'russell2000')
        force_refresh: If True, fetch fresh data ignoring cache

    Returns:
        List of unique ticker symbols from all specified indices
    """
    return get_fetcher().get_all_tickers(indices=indices, force_refresh=force_refresh)


def get_sp500_tickers(force_refresh: bool = False) -> List[str]:
    """Get S&P 500 constituent tickers."""
    return get_fetcher().get_tickers_by_index("sp500", force_refresh=force_refresh)


def get_nasdaq100_tickers(force_refresh: bool = False) -> List[str]:
    """Get Nasdaq 100 constituent tickers."""
    return get_fetcher().get_tickers_by_index("nasdaq100", force_refresh=force_refresh)


def get_russell2000_tickers(force_refresh: bool = False) -> List[str]:
    """Get Russell 2000 constituent tickers."""
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
