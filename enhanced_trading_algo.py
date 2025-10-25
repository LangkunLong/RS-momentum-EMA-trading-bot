from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")

from config.settings import CUSTOM_LIST, MIN_CANSLIM_SCORE, MIN_RS_SCORE, START_DATE
from core.stock_screening import print_analysis_results, screen_stocks_canslim

# Run the CAN SLIM momentum screen using configuration defaults
def main() -> None:

    results, market_trend = screen_stocks_canslim(
        symbols=CUSTOM_LIST,
        start_date=START_DATE,
        min_rs_score=MIN_RS_SCORE,
        min_canslim_score=MIN_CANSLIM_SCORE,
    )

    print_analysis_results(results, market_trend)

if __name__ == "__main__":
    print("Screening for CAN SLIM momentum opportunities...")
    print(f"Universe: {', '.join(CUSTOM_LIST)}")
    main()