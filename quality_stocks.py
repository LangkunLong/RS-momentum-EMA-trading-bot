"""
Quality Stocks Module

Provides stock lists from major indices for CANSLIM screening.
Fetches tickers from S&P 500, Nasdaq 100, and Russell 2000 indices
with daily caching to minimize API calls.
"""

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
    """
    Get a list of quality stocks from major indices.

    This function fetches tickers from S&P 500, Nasdaq 100, and Russell 2000
    indices. Results are cached daily to avoid repeated fetches.

    Args:
        sectors: Optional list of indices/categories to include.
                 Options: 'sp500', 'nasdaq100', 'russell2000', 'large_cap',
                          'small_cap', 'all', or legacy sector names.
                 If None, returns all tickers from all major indices.
        force_refresh: If True, bypasses cache and fetches fresh data.

    Returns:
        List of unique stock ticker symbols.

    Examples:
        >>> # Get all tickers from all indices
        >>> tickers = get_quality_stock_list()

        >>> # Get only S&P 500 tickers
        >>> sp500 = get_quality_stock_list(sectors=['sp500'])

        >>> # Get large-cap stocks (S&P 500 + Nasdaq 100)
        >>> large_caps = get_quality_stock_list(sectors=['large_cap'])

        >>> # Get small-cap stocks (Russell 2000)
        >>> small_caps = get_quality_stock_list(sectors=['small_cap'])
    """
    # If no sectors specified, return all indices
    if sectors is None:
        return get_all_index_tickers(force_refresh=force_refresh)

    # Resolve sector aliases to actual index names
    indices_to_fetch = set()
    for sector in sectors:
        sector_lower = sector.lower().strip()
        if sector_lower in INDEX_ALIASES:
            indices_to_fetch.update(INDEX_ALIASES[sector_lower])
        else:
            # Try to use as-is if it's a valid index name
            valid_indices = {"sp500", "nasdaq100", "russell2000"}
            if sector_lower in valid_indices:
                indices_to_fetch.add(sector_lower)
            else:
                print(f"Warning: Unknown sector/index '{sector}', skipping...")

    if not indices_to_fetch:
        print("No valid indices found, returning all tickers...")
        return get_all_index_tickers(force_refresh=force_refresh)

    return get_all_index_tickers(
        indices=list(indices_to_fetch),
        force_refresh=force_refresh
    )


def get_index_tickers(index_name: str, force_refresh: bool = False) -> List[str]:
    """
    Get tickers for a specific index.

    Args:
        index_name: One of 'sp500', 'nasdaq100', 'russell2000'
        force_refresh: If True, bypasses cache

    Returns:
        List of ticker symbols for the specified index
    """
    index_lower = index_name.lower().strip()

    if index_lower in ["sp500", "s&p500", "s&p 500"]:
        return get_sp500_tickers(force_refresh=force_refresh)
    elif index_lower in ["nasdaq100", "nasdaq 100", "nasdaq"]:
        return get_nasdaq100_tickers(force_refresh=force_refresh)
    elif index_lower in ["russell2000", "russell 2000", "russell"]:
        return get_russell2000_tickers(force_refresh=force_refresh)
    else:
        raise ValueError(f"Unknown index: {index_name}. Use 'sp500', 'nasdaq100', or 'russell2000'.")


def refresh_ticker_cache() -> None:
    """Force refresh of the ticker cache."""
    clear_ticker_cache()
    print("Ticker cache cleared. Fresh data will be fetched on next request.")


def get_available_indices() -> List[str]:
    """Get list of available index names."""
    return ["sp500", "nasdaq100", "russell2000"]


def get_available_categories() -> dict:
    """Get available categories and their descriptions."""
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
# TBI
