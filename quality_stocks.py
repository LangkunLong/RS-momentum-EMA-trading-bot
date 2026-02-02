from typing import List, Optional

from core.index_ticker_fetcher import (
    get_all_index_tickers,
    get_sp500_tickers,
    get_nasdaq100_tickers,
    get_russell2000_tickers,
    clear_ticker_cache,
)


# Index aliases for user-friendly sector/index selection
INDEX_ALIASES = {
    # Standard index names
    "sp500": ["sp500"],
    "s&p500": ["sp500"],
    "s&p 500": ["sp500"],
    "nasdaq100": ["nasdaq100"],
    "nasdaq 100": ["nasdaq100"],
    "nasdaq": ["nasdaq100"],
    "russell2000": ["russell2000"],
    "russell 2000": ["russell2000"],
    "russell": ["russell2000"],
    "small_cap": ["russell2000"],
    "smallcap": ["russell2000"],

    # Combined categories
    "large_cap": ["sp500", "nasdaq100"],
    "largecap": ["sp500", "nasdaq100"],
    "all": ["sp500", "nasdaq100", "russell2000"],

    # Legacy sector aliases (map to indices for backward compatibility)
    "mega_cap_tech": ["nasdaq100"],
    "growth_high_beta": ["nasdaq100"],
    "crypto_fintech": ["nasdaq100"],
    "healthcare": ["sp500"],
    "energy": ["sp500"],
    "financials": ["sp500"],
    "industrials": ["sp500"],
}


def get_quality_stock_list(
    sectors: Optional[List[str]] = None,
    force_refresh: bool = False
) -> List[str]:
    # If no sectors specified, return all indices
    if sectors is None:
        return get_all_index_tickers(force_refresh=force_refresh)


def get_index_tickers(index_name: str, force_refresh: bool = False) -> List[str]:
    """Resolve an index name (or alias like 'large_cap') to a list of tickers."""
    index_lower = index_name.lower().strip()

    # Look up in the alias map first — handles combined categories like large_cap
    indices = INDEX_ALIASES.get(index_lower)
    if indices is None:
        raise ValueError(
            f"Unknown index: {index_name}. "
            f"Available: {', '.join(sorted(INDEX_ALIASES.keys()))}"
        )

    return get_all_index_tickers(indices=indices, force_refresh=force_refresh)


def refresh_ticker_cache() -> None:
    """Force refresh of the ticker cache."""
    clear_ticker_cache()
    print("Ticker cache cleared. Fresh data will be fetched on next request.")


def get_available_indices() -> List[str]:
    """Get list of available index names."""
    return ["sp500", "nasdaq100", "russell2000"]


def get_available_categories() -> dict:
    return {
        "sp500": "S&P 500 - Large-cap US stocks",
        "nasdaq100": "Nasdaq 100 - Large-cap tech-focused stocks",
        "russell2000": "Russell 2000 - Small-cap US stocks",
        "large_cap": "Large-cap (S&P 500 + Nasdaq 100)",
        "small_cap": "Small-cap (Russell 2000)",
        "all": "All indices combined",
    }


if __name__ == "__main__":
    # Test the module
    print("Testing Quality Stocks Module...")
    print("=" * 60)

    # Get all tickers
    all_tickers = get_quality_stock_list()
    print(f"Total tickers (all indices): {len(all_tickers)}")

    # Get specific indices
    sp500 = get_quality_stock_list(sectors=["sp500"])
    print(f"S&P 500 tickers: {len(sp500)}")

    nasdaq = get_quality_stock_list(sectors=["nasdaq100"])
    print(f"Nasdaq 100 tickers: {len(nasdaq)}")

    russell = get_quality_stock_list(sectors=["russell2000"])
    print(f"Russell 2000 tickers: {len(russell)}")

    large_cap = get_quality_stock_list(sectors=["large_cap"])
    print(f"Large-cap tickers: {len(large_cap)}")

    print("\nAvailable categories:")
    for name, desc in get_available_categories().items():
        print(f"  {name}: {desc}")
