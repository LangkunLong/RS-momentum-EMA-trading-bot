"""CANSLIM Stock Scanner.

Main scanning function for CANSLIM stock opportunities.
This simplified scanner focuses purely on CANSLIM criteria without pullback entries.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from config import settings
from core.data_client import validate_ticker, validate_tickers_bulk
from core.stock_screening import print_analysis_results, screen_stocks_canslim_detailed
from quality_stocks import get_index_tickers, get_quality_stock_list


def is_valid_ticker(symbol: str, retries: int = 1) -> bool:
    """Check if ticker is available on Alpaca."""
    for attempt in range(retries):
        try:
            if validate_ticker(symbol):
                return True
        except Exception as e:
            if attempt == retries - 1:
                print(f"Failed to validate ticker '{symbol}' after {retries} attempts: {e}")
    return False


def scan_for_canslim_stocks(
    min_rs_score: Optional[float] = None,
    min_canslim_score: Optional[float] = None,
    sectors: Optional[str] = None,
    custom_list: Optional[list[str]] = None,
    start_date: Optional[str] = None,
    debug: Optional[bool] = None,
    watchlist_min_score: Optional[float] = None,
    require_bullish_market_for_buys: Optional[bool] = None,
) -> tuple[list[dict], list[dict], object]:
    """Run the full CANSLIM scan and return qualifying opportunities.

    Args:
        min_rs_score: Minimum relative strength score (overrides settings).
        min_canslim_score: Minimum composite CANSLIM score (overrides settings).
        sectors: Index/sector name to scan (e.g. 'nasdaq100', 'large_cap').
        custom_list: Explicit list of tickers to evaluate instead of an index.
        start_date: Start date for historical analysis (overrides settings).
        debug: Enable verbose per-stock output (overrides settings).

    Returns:
        Tuple of (actionable_buys, watchlist_candidates, market_trend).

    """
    # Load defaults from configuration
    min_rs_score = min_rs_score if min_rs_score is not None else settings.MIN_RS_SCORE
    min_canslim_score = min_canslim_score if min_canslim_score is not None else settings.MIN_CANSLIM_SCORE
    watchlist_min_score = (
        watchlist_min_score if watchlist_min_score is not None else settings.WATCHLIST_MIN_CANSLIM_SCORE
    )
    require_bullish_market_for_buys = (
        require_bullish_market_for_buys
        if require_bullish_market_for_buys is not None
        else settings.REQUIRE_BULLISH_MARKET_FOR_BUYS
    )
    sectors = sectors if sectors is not None else settings.SECTORS
    custom_list = custom_list if custom_list is not None else settings.CUSTOM_LIST
    start_date = start_date if start_date is not None else settings.START_DATE
    debug = debug if debug is not None else settings.DEBUG

    print("=" * 60)
    print("CANSLIM STOCK SCANNER")
    print("=" * 60)

    # Get stock list
    if custom_list:
        print("Using custom stock list...")
        symbols = custom_list
    elif sectors:
        print(f"Using curated stock list for sectors: {sectors}")
        symbols = get_index_tickers(index_name=sectors)
    else:
        print("Using default curated quality stock list...")
        symbols = get_quality_stock_list()

    print(f"Scanning {len(symbols)} stocks for CANSLIM opportunities...")
    print(f"Minimum RS Score: {min_rs_score}")
    print(f"Minimum CANSLIM Score: {min_canslim_score}")
    print(f"Watchlist CANSLIM Floor: {watchlist_min_score}")
    print(f"Require Bullish Market For Buys: {require_bullish_market_for_buys}")

    # Filter out invalid/delisted tickers before scanning
    print("Validating tickers with Alpaca...")
    valid_symbols = validate_tickers_bulk(symbols)

    missing = set(symbols) - set(valid_symbols)
    if missing:
        print(f"Skipped {len(missing)} invalid/delisted tickers.")

    print(f"{len(valid_symbols)} valid tickers will be scanned.")

    actionable_buys, watchlist_candidates, market_trend = screen_stocks_canslim_detailed(
        symbols=valid_symbols,
        start_date=start_date,
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        debug=debug,
        watchlist_min_score=watchlist_min_score,
        require_bullish_market=require_bullish_market_for_buys,
    )

    print("\nScan complete!")
    print(f"Analyzed: {len(symbols)} stocks")
    print(f"Actionable buys found: {len(actionable_buys)} stocks")
    print(f"Watchlist candidates found: {len(watchlist_candidates)} stocks")

    return actionable_buys, watchlist_candidates, market_trend


def export_results_to_csv(opportunities: list[dict], filename: Optional[str] = None) -> Optional[str]:
    """Export CANSLIM results to CSV file and return the path."""
    if not opportunities:
        print("No opportunities to export.")
        return None

    if filename is None:
        os.makedirs(settings.RESULTS_DIR, exist_ok=True)
        filename = os.path.join(
            settings.RESULTS_DIR,
            f"canslim_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )

    # Flatten the data for CSV export
    csv_data = []
    for opp in opportunities:
        metrics = opp["metrics"]
        market = opp.get("market_trend")
        availability = opp.get("data_availability", {})
        s_metrics = metrics.get("s_metrics", {})
        row = {
            "Symbol": opp["symbol"],
            "Scanner_Category": opp.get("scanner_category"),
            "Scanner_Notes": ",".join(opp.get("scanner_notes", [])),
            "RS_Score": opp["rs_score"],
            "CANSLIM_Score": opp["total_score"],
            "C_Score": opp["scores"]["C"] * 100,
            "A_Score": opp["scores"]["A"] * 100,
            "N_Score": opp["scores"]["N"] * 100,
            "S_Score": opp["scores"]["S"] * 100,
            "L_Score": opp["scores"]["L"] * 100,
            "I_Score": opp["scores"]["I"] * 100,
            "M_Score": opp["scores"]["M"] * 100,
            "Current_Growth": metrics["current_growth"],
            "Annual_Growth": metrics["annual_growth"],
            "Revenue_Growth": metrics["revenue_growth"],
            "ROE": metrics.get("roe"),
            "Shares_Outstanding": metrics["shares_outstanding"],
            "Proximity_to_High": metrics["proximity_to_high"],
            "Avg_Volume_50": metrics["avg_volume_50"],
            "UpDown_Volume_Ratio": s_metrics.get("up_down_volume_ratio"),
            "Volume_Ratio": s_metrics.get("volume_ratio"),
            "Is_Breakout": opp.get("is_breakout"),
            "Has_Volume_Surge": opp.get("has_volume_surge"),
            "Market_Bullish": getattr(market, "is_bullish", None),
            "Market_Score": getattr(market, "score", None),
            "Distribution_Days": getattr(market, "distribution_days", None),
            "Follow_Through": getattr(market, "follow_through", None),
            "Quarterly_Income_Available": metrics.get("quarterly_income_available"),
            "Annual_Income_Available": metrics.get("annual_income_available"),
            "Balance_Sheet_Available": metrics.get("balance_sheet_available"),
            "Current_Earnings_Available": metrics.get("current_earnings_available"),
            "Annual_Earnings_Available": metrics.get("annual_earnings_available"),
            "Revenue_Growth_Available": metrics.get("revenue_growth_available"),
            "Institutional_Data_Available": metrics.get("institutional_data_available"),
            "Has_Fundamentals": metrics.get("has_fundamentals"),
            "Data_Availability_C": availability.get("C"),
            "Data_Availability_A": availability.get("A"),
            "Data_Availability_N_Revenue": availability.get("N_revenue"),
            "Data_Availability_I_Level": availability.get("I_level"),
            "Data_Availability_I_Trend": availability.get("I_trend"),
            "Income_Statement_Error": metrics.get("income_statement_error"),
            "Balance_Sheet_Error": metrics.get("balance_sheet_error"),
        }
        csv_data.append(row)

    df = pd.DataFrame(csv_data)
    df.to_csv(filename, index=False)
    print(f"Results exported to {filename}")
    return filename


def print_result_quality_summary(results: list[dict]) -> None:
    """Print a compact summary of missing fundamental fields in the result set."""
    if not results:
        return

    total = len(results)
    missing_current = sum(1 for row in results if not row["metrics"].get("current_earnings_available"))
    missing_annual = sum(1 for row in results if not row["metrics"].get("annual_earnings_available"))
    missing_revenue = sum(1 for row in results if not row["metrics"].get("revenue_growth_available"))
    missing_fundamentals = sum(1 for row in results if not row["metrics"].get("has_fundamentals"))
    market_blocked = sum(
        1 for row in results if "market_not_bullish" in set(row.get("scanner_notes", []))
    )

    print("\nResult Quality Summary:")
    print(
        f"- Missing current earnings: {missing_current}/{total} | "
        f"Missing annual earnings: {missing_annual}/{total} | "
        f"Missing revenue growth: {missing_revenue}/{total}"
    )
    print(
        f"- Missing fundamentals overall: {missing_fundamentals}/{total} | "
        f"Blocked by market regime: {market_blocked}/{total}"
    )


if __name__ == "__main__":
    # You can override default settings here or edit config/settings.py

    # Example: Override specific settings (uncomment to use)
    # MIN_RS_SCORE = 10
    # MIN_CANSLIM_SCORE = 75
    # SECTORS = ['growth_high_beta', 'crypto_fintech']
    # CUSTOM_LIST = None
    # DEBUG = True

    # Or just use defaults from config/settings.py
    MIN_RS_SCORE = None  # Uses settings.MIN_RS_SCORE
    MIN_CANSLIM_SCORE = None  # Uses settings.MIN_CANSLIM_SCORE
    WATCHLIST_MIN_SCORE = None  # Uses settings.WATCHLIST_MIN_CANSLIM_SCORE
    REQUIRE_BULLISH_MARKET_FOR_BUYS = None  # Uses settings.REQUIRE_BULLISH_MARKET_FOR_BUYS
    MAX_TERMINAL_RESULTS = settings.MAX_TERMINAL_RESULTS
    # Available indices: 'sp500', 'nasdaq100', 'russell2000', 'large_cap', 'small_cap', 'all'
    # Set to None to scan all major indices (S&P 500 + Nasdaq 100 + Russell 2000)
    SECTORS = "nasdaq100"  # Uses all indices from major markets
    CUSTOM_LIST = None
    DEBUG = True  # Override default

    print("CANSLIM Stock Scanner Configuration:")
    print(f"- Min RS Score: {MIN_RS_SCORE or settings.MIN_RS_SCORE}")
    print(f"- Min CANSLIM Score: {MIN_CANSLIM_SCORE or settings.MIN_CANSLIM_SCORE}")
    print(f"- Watchlist Min Score: {WATCHLIST_MIN_SCORE or settings.WATCHLIST_MIN_CANSLIM_SCORE}")
    print(
        "- Require Bullish Market For Buys: "
        f"{REQUIRE_BULLISH_MARKET_FOR_BUYS if REQUIRE_BULLISH_MARKET_FOR_BUYS is not None else settings.REQUIRE_BULLISH_MARKET_FOR_BUYS}"
    )
    print(f"- Max Terminal Results Per Section: {MAX_TERMINAL_RESULTS}")
    indices_label = SECTORS or settings.SECTORS or "All (S&P 500 + Nasdaq 100 + Russell 2000)"
    print(f"- Indices: {indices_label}")
    print(f"- Custom List: {'Yes' if CUSTOM_LIST else 'No'}")
    print(f"- Debug Mode: {DEBUG or settings.DEBUG}")

    # Run the scan
    actionable_buys, watchlist_candidates, market_trend = scan_for_canslim_stocks(
        min_rs_score=MIN_RS_SCORE,
        min_canslim_score=MIN_CANSLIM_SCORE,
        sectors=SECTORS,
        custom_list=CUSTOM_LIST,
        debug=DEBUG,
        watchlist_min_score=WATCHLIST_MIN_SCORE,
        require_bullish_market_for_buys=REQUIRE_BULLISH_MARKET_FOR_BUYS,
    )
    combined_results = actionable_buys + watchlist_candidates

    print_result_quality_summary(combined_results)

    if actionable_buys:
        print_analysis_results(
            actionable_buys,
            market_trend,
            title="ACTIONABLE CANSLIM BUYS",
            max_results=MAX_TERMINAL_RESULTS,
        )
    else:
        print("No actionable CANSLIM buys met the current market and score gates.")

    if watchlist_candidates:
        print_analysis_results(
            watchlist_candidates,
            market_trend,
            title="WATCHLIST CANDIDATES",
            max_results=MAX_TERMINAL_RESULTS,
        )
    else:
        print("No watchlist candidates met the current RS and watchlist-score floors.")

    if settings.AUTO_EXPORT_RESULTS and combined_results:
        export_results_to_csv(combined_results)

    print("\nScan completed!")
