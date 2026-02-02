"""
Configuration settings for CANSLIM Trading Bot
All configurable parameters are centralized here to avoid hardcoded values.

Parameters follow William O'Neil's CANSLIM methodology from
"How to Make Money in Stocks" as closely as possible.
"""

# ==============================================================================
# SCANNER SETTINGS
# ==============================================================================

# Stock screening thresholds
START_DATE = '2024-01-01'           # Analysis start date
MIN_MARKET_CAP = 10e9               # Minimum market cap ($10 billion)
MIN_RS_SCORE = 80                   # Minimum Relative Strength score (1-99) — top 20%
MIN_CANSLIM_SCORE = 70              # Minimum composite CANSLIM score (0-100)

# Performance settings
MAX_WORKERS = 3                     # Maximum threads for parallel processing
CHUNK_SIZE = 50                     # Batch size for downloading stock data

# Stock selection - Now fetches from major indices (S&P 500, Nasdaq 100, Russell 2000)
USE_API = False                     # Use API vs index-based lists
SECTORS = 'large_cap'               # Default to liquid large-cap stocks
                                    # Options: 'sp500', 'nasdaq100', 'russell2000',
                                    # 'large_cap' (S&P 500 + Nasdaq 100),
                                    # 'small_cap' (Russell 2000), 'all'
CUSTOM_LIST = None                  # Custom stock symbols (overrides sectors if set)

# Debugging
DEBUG = False                       # Enable verbose output


# ==============================================================================
# DATA DOWNLOAD SETTINGS
# ==============================================================================

# Historical data periods
RS_CALCULATION_PERIOD = '14mo'      # Period for RS score calculation
CANSLIM_DATA_PERIOD = '1y'          # Period for CANSLIM evaluation
MARKET_TREND_PERIOD = '1y'          # Period for market direction analysis
PRICE_HISTORY_BUFFER_DAYS = 120    # Extra days to download for indicators

# Caching
RS_CACHE_DIR = 'rs_score_cache'     # Directory for RS score cache
RS_CACHE_FILE = 'rs_scores_cache.csv'  # Cache filename
TICKER_CACHE_DIR = 'ticker_cache'   # Directory for index ticker cache
TICKER_CACHE_EXPIRY_HOURS = 24      # How often to refresh ticker lists


# ==============================================================================
# TECHNICAL INDICATOR PARAMETERS
# ==============================================================================

# Exponential Moving Averages
EMA_SHORT = 8                       # Short-term EMA period
EMA_LONG = 21                       # Long-term EMA period
EMA_MARKET_50 = 50                  # Market 50-day EMA
EMA_MARKET_200 = 200                # Market 200-day EMA

# Relative Strength Index
RSI_PERIOD = 14                     # RSI calculation period


# ==============================================================================
# CANSLIM COMPONENT WEIGHTS (for composite score)
# O'Neil emphasizes C, A, and L as the most critical factors.
# ==============================================================================

# Per-component weights for the final composite CANSLIM score (must sum to 1.0)
CANSLIM_WEIGHT_C = 0.20             # Current quarterly earnings — critical
CANSLIM_WEIGHT_A = 0.15             # Annual earnings — critical
CANSLIM_WEIGHT_N = 0.10             # New products / new highs
CANSLIM_WEIGHT_S = 0.10             # Supply and demand
CANSLIM_WEIGHT_L = 0.20             # Leader or laggard — critical
CANSLIM_WEIGHT_I = 0.10             # Institutional sponsorship
CANSLIM_WEIGHT_M = 0.15             # Market direction — critical (3/4 stocks follow market)

# Power Earnings Gap composite bonus (added on top of weighted score)
# O'Neil treats PEGs as one of the strongest buy signals in his methodology.
# A stock gapping up on massive volume after earnings shows overwhelming
# institutional demand. This bonus is scaled by the gap's volume intensity.
PEG_COMPOSITE_BONUS = 10.0          # Max +10 points on composite score for a PEG


# ==============================================================================
# C (CURRENT QUARTERLY EARNINGS) PARAMETERS
# ==============================================================================

C_GROWTH_TARGET = 0.25              # 25% YoY quarterly EPS growth target

# Sub-component weights within C score (must sum to 1.0)
C_GROWTH_WEIGHT = 0.60              # Weight for current quarter growth vs target
C_CONSISTENCY_WEIGHT = 0.20         # Weight for multiple quarters of 25%+ growth
C_ACCELERATION_WEIGHT = 0.20        # Weight for accelerating growth rates

# IPO / limited data handling
C_IPO_DATA_DISCOUNT = 0.85          # Discount factor for IPO stocks with < 5 quarters
                                    # Sequential comparison less reliable than YoY


# ==============================================================================
# A (ANNUAL EARNINGS) PARAMETERS
# ==============================================================================

A_GROWTH_TARGET = 0.25              # 25% annual EPS growth target
A_ROE_TARGET = 0.17                 # 17% ROE minimum per O'Neil
A_MIN_YEARS_GROWTH = 3              # Check last 3 years for consistency

# Sub-component weights within A score (must sum to 1.0)
A_GROWTH_WEIGHT = 0.50              # Weight for most recent year's growth
A_CONSISTENCY_WEIGHT = 0.30         # Weight for multi-year consistency
A_ROE_WEIGHT = 0.20                 # Weight for ROE check

# IPO / limited data handling
A_IPO_DATA_DISCOUNT = 0.80          # Discount factor for IPOs with < 3 years of data
                                    # Can't assess multi-year consistency, so discount more


# ==============================================================================
# N (NEW PRODUCTS / NEW HIGHS) PARAMETERS
# ==============================================================================

N_REVENUE_GROWTH_WEIGHT = 0.50      # Weight for revenue growth (was 0.7)
N_PROXIMITY_TO_HIGH_WEIGHT = 0.50   # Weight for proximity to 52-week high (was 0.3)
N_REVENUE_GROWTH_TARGET = 0.25      # 25% quarterly revenue growth target


# ==============================================================================
# S (SUPPLY AND DEMAND) PARAMETERS
# ==============================================================================

S_VOLUME_SURGE_THRESHOLD = 1.5      # Volume multiplier for surge detection (1.5 = 50% above avg)
S_BREAKOUT_PROXIMITY = 0.98         # Proximity to 52-week high for breakout (0.98 = within 2%)
S_POWER_GAP_LOOKBACK = 10           # Days to look back for Power Earnings Gaps
S_TURNOVER_CAP = 1.0                # Legacy: max turnover ratio for scoring

# Sub-component weights within S score (must sum to 1.0)
S_FLOAT_WEIGHT = 0.25               # Weight for float / shares outstanding
S_UP_DOWN_VOL_WEIGHT = 0.25         # Weight for up/down volume ratio
S_SURGE_BREAKOUT_WEIGHT = 0.30      # Weight for volume surge + breakout
S_POWER_GAP_WEIGHT = 0.20           # Weight for Power Earnings Gap


# ==============================================================================
# I (INSTITUTIONAL SPONSORSHIP) PARAMETERS
# ==============================================================================

I_INSTITUTIONAL_CAP = 1.0           # Legacy: max institutional ownership for scoring

# Sub-component weights within I score (must sum to 1.0)
I_LEVEL_WEIGHT = 0.60               # Weight for ownership level (sweet-spot curve)
I_TREND_WEIGHT = 0.40               # Weight for ownership trend (increasing/decreasing)


# ==============================================================================
# MARKET TREND SCORING PARAMETERS
# ==============================================================================

# EMA-based trend component weights (within the EMA portion of M score)
M_PRICE_ABOVE_200EMA_WEIGHT = 0.45  # Weight if price > 200-EMA
M_EMA_ALIGNMENT_WEIGHT = 0.25       # Weight if 21-EMA > 50-EMA > 200-EMA
M_50EMA_RISING_WEIGHT = 0.20        # Weight if 50-EMA is rising
M_PRICE_ABOVE_21EMA_WEIGHT = 0.10   # Weight if price > 21-EMA

# Market trend thresholds
M_BULLISH_THRESHOLD = 0.6           # Minimum score for bullish market
M_50EMA_RISING_LOOKBACK = 20        # Days to check if 50-EMA is rising

# O'Neil's Distribution Day parameters
M_DISTRIBUTION_LOOKBACK = 25        # Trading days to count distribution days
M_DISTRIBUTION_MIN_DECLINE = 0.002  # 0.2% minimum decline for distribution day
M_MAX_DISTRIBUTION_DAYS = 5         # 5+ distribution days = market top

# O'Neil's Follow-Through Day parameters
M_FOLLOW_THROUGH_MIN_PCT = 0.015    # 1.5% minimum gain for follow-through day
M_FOLLOW_THROUGH_MIN_DAY = 4        # Earliest day of rally for follow-through

# M score component weights (distribution/follow-through vs EMA trend)
M_DISTRIBUTION_WEIGHT = 0.30        # Weight for distribution day analysis
M_FOLLOW_THROUGH_WEIGHT = 0.15      # Weight for follow-through day detection
# Remaining weight (0.55) goes to EMA-based trend analysis


# ==============================================================================
# RS SCORE CALCULATION PARAMETERS
# ==============================================================================

# Quarterly performance weights (must sum to 1.0)
# O'Neil's IBD RS rating gives most recent quarter 2x weight
RS_Q1_WEIGHT = 0.40                 # Most recent quarter (40%)
RS_Q2_WEIGHT = 0.20                 # 2nd quarter (20%)
RS_Q3_WEIGHT = 0.20                 # 3rd quarter (20%)
RS_Q4_WEIGHT = 0.20                 # Oldest quarter (20%)

# RS score scaling
RS_PERCENTILE_MIN = 1               # Minimum RS score
RS_PERCENTILE_MAX = 99              # Maximum RS score
RS_PERCENTILE_MULTIPLIER = 98       # Scaling multiplier (max - min)

# Trading days per quarter (for RS calculation)
TRADING_DAYS_PER_QUARTER = 65       # Approximate trading days in a quarter


# ==============================================================================
# API SETTINGS (OPTIONAL - Only if USE_API=True)
# ==============================================================================

# Finnhub API (requires FINNHUB_API_KEY environment variable)
FINNHUB_BASE_URL = "https://finnhub.io/api/v1/stock/symbol"
FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
FINNHUB_EXCHANGE = "US"
FINNHUB_MAX_STOCKS = 500            # Limit API calls

# HTTP Retry settings
HTTP_RETRY_TOTAL = 5                # Maximum retry attempts
HTTP_RETRY_BACKOFF = 2              # Exponential backoff factor
HTTP_RETRY_STATUS_CODES = [429, 500, 502, 503, 504]  # Retry on these codes
HTTP_MAX_WORKERS = 5                # Parallel API requests
