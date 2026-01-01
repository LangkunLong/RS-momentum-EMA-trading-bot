"""
Configuration settings for CANSLIM Trading Bot
All configurable parameters are centralized here to avoid hardcoded values
"""

# ==============================================================================
# SCANNER SETTINGS
# ==============================================================================

# Stock screening thresholds
START_DATE = '2024-01-01'           # Analysis start date
MIN_MARKET_CAP = 10e9               # Minimum market cap ($10 billion)
MIN_RS_SCORE = 5                    # Minimum Relative Strength score (1-99)
MIN_CANSLIM_SCORE = 70              # Minimum composite CANSLIM score (0-100)

# Performance settings
MAX_WORKERS = 3                     # Maximum threads for parallel processing
CHUNK_SIZE = 50                     # Batch size for downloading stock data

# Stock selection
USE_API = False                     # Use API vs curated lists
SECTORS = None                      # List of sectors to filter (None = all)
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
# CANSLIM SCORING WEIGHTS & THRESHOLDS
# ==============================================================================

# Target growth rates for scoring (as decimal: 0.25 = 25%)
C_GROWTH_TARGET = 0.25              # Current quarter earnings growth target
A_GROWTH_TARGET = 0.25              # Annual earnings growth target

# N (New) component weights
N_REVENUE_GROWTH_WEIGHT = 0.7       # Weight for revenue growth
N_PROXIMITY_TO_HIGH_WEIGHT = 0.3    # Weight for proximity to 52-week high

# S (Supply/Demand) - Volume Surge & Breakout Detection
S_VOLUME_SURGE_THRESHOLD = 1.5      # Volume multiplier for surge detection (1.5 = 50% above avg)
S_BREAKOUT_PROXIMITY = 0.98         # Proximity to 52-week high for breakout (0.98 = within 2%)
S_POWER_GAP_LOOKBACK = 10           # Days to look back for Power Earnings Gaps

# I (Institutional) threshold
I_INSTITUTIONAL_CAP = 1.0           # Maximum institutional ownership for scoring

# Trading days per quarter (for RS calculation)
TRADING_DAYS_PER_QUARTER = 65       # Approximate trading days in a quarter


# ==============================================================================
# MARKET TREND SCORING PARAMETERS
# ==============================================================================

# Market trend component weights
M_PRICE_ABOVE_200EMA_WEIGHT = 0.4   # Weight if price > 200-EMA
M_EMA_ALIGNMENT_WEIGHT = 0.3        # Weight if 21-EMA > 50-EMA > 200-EMA
M_50EMA_RISING_WEIGHT = 0.2         # Weight if 50-EMA is rising
M_PRICE_ABOVE_21EMA_WEIGHT = 0.1    # Weight if price > 21-EMA

# Market trend thresholds
M_BULLISH_THRESHOLD = 0.6           # Minimum score for bullish market
M_50EMA_RISING_LOOKBACK = 20        # Days to check if 50-EMA is rising


# ==============================================================================
# RS SCORE CALCULATION PARAMETERS
# ==============================================================================

# Quarterly performance weights (must sum to 1.0)
RS_Q1_WEIGHT = 0.40                 # Most recent quarter (40%)
RS_Q2_WEIGHT = 0.20                 # 2nd quarter (20%)
RS_Q3_WEIGHT = 0.20                 # 3rd quarter (20%)
RS_Q4_WEIGHT = 0.20                 # 4th quarter (20%)

# RS score scaling
RS_PERCENTILE_MIN = 1               # Minimum RS score
RS_PERCENTILE_MAX = 99              # Maximum RS score
RS_PERCENTILE_MULTIPLIER = 98       # Scaling multiplier (max - min)


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
