"""Configuration settings for CANSLIM Trading Bot
All configurable parameters are centralized here to avoid hardcoded values.

Parameters follow William O'Neil's CANSLIM methodology from
"How to Make Money in Stocks" as closely as possible.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# API KEYS (loaded from environment — see .env.example)
# ==============================================================================
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_STOCK_FEED = os.environ.get("ALPACA_STOCK_FEED", "iex").strip().lower()
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
# ALPACA_PAPER is read directly from os.environ in order_execution.py
# (default: "true" — paper trading). Set to "false" in .env for live trading.

# Email notifications (Gmail SMTP with App Password)
# NOTIFY_EMAIL_FROM: your Gmail address (sender)
# NOTIFY_EMAIL_TO: recipient address (can be the same Gmail)
# NOTIFY_EMAIL_PASSWORD: Gmail App Password (not your login password).
#   Generate at: https://myaccount.google.com/apppasswords
NOTIFY_EMAIL_FROM = os.environ.get("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", "")
NOTIFY_EMAIL_PASSWORD = os.environ.get("NOTIFY_EMAIL_PASSWORD", "")


# ==============================================================================
# SCANNER SETTINGS
# ==============================================================================

ARTIFACTS_DIR = ".artifacts"
CACHE_DIR = os.path.join(ARTIFACTS_DIR, "cache")
PYTEST_ARTIFACTS_DIR = os.path.join(ARTIFACTS_DIR, "pytest")
SCAN_RESULTS_DIR = os.path.join(ARTIFACTS_DIR, "scan_results")
BACKTEST_RESULTS_DIR = os.path.join(ARTIFACTS_DIR, "backtests")
BACKTEST_CACHE_DIR = os.path.join(CACHE_DIR, "backtest")
BACKTEST_DATA_CACHE_DB_PATH = os.path.join(BACKTEST_CACHE_DIR, "historical_data.sqlite3")
FUNDAMENTALS_CACHE_DIR = os.path.join(CACHE_DIR, "fundamentals")

# Stock screening thresholds
START_DATE = "2024-01-01"  # Analysis start date
MIN_MARKET_CAP = 10e9  # Minimum market cap ($10 billion)
# O'Neil's IBD RS rating requires 80+ for institutional-quality leaders.
# 75 was an earlier default when FMP fundamentals were incomplete; now that fundamentals
# are available, 80 is the appropriate live-entry floor.
# Tradeoff: raising to 85 further reduces false positives but also filters out
# early-stage leaders that have not yet attracted broad attention.
MIN_RS_SCORE = 80
MIN_CANSLIM_SCORE = 70  # Minimum composite CANSLIM score for actionable entries
# Watchlist floor is intentionally low so that high-RS stocks worth monitoring during
# market corrections still surface even when M (market direction) is dragging composite
# scores down.  With a bearish M score (~0.15) and no FMP fundamentals, an RS-90 stock
# near 82% of its 52-week high scores ~44 — a floor of 30 catches these without diluting
# the list with genuinely weak names.
WATCHLIST_MIN_CANSLIM_SCORE = 30  # Lowered from 45 — bear-market watchlist must still populate
REQUIRE_BULLISH_MARKET_FOR_BUYS = True  # O'Neil-style market gate for actionable entries
REQUIRE_FUNDAMENTALS_FOR_BUYS = True  # Do not upgrade missing-C/A names into actionable buys
STRICT_BREAKOUT_FOR_BUYS = True  # Actionable buys must be real breakout setups, not generic high scorers
MAX_TERMINAL_RESULTS = 12  # Limit terminal detail; full output is exported to CSV
AUTO_EXPORT_RESULTS = True  # Save scanner output to CSV by default
RESULTS_DIR = SCAN_RESULTS_DIR  # Directory for exported scanner CSV files
EXECUTION_STORE_DB_PATH = os.environ.get(
    "EXECUTION_STORE_DB_PATH",
    os.path.join(os.environ.get("TEMP", "."), "trading_bot", "execution_store.sqlite3"),
)  # Durable SQLite execution store

# Performance settings
MAX_WORKERS = 3  # Maximum threads for parallel processing
CHUNK_SIZE = 50  # Batch size for downloading stock data

# Stock selection - Now fetches from major indices (S&P 500, Nasdaq 100, Russell 2000)
USE_API = False  # Use API vs index-based lists
SECTORS = "large_cap"  # Default to liquid large-cap stocks
# Options: 'sp500', 'nasdaq100', 'russell2000',
# 'large_cap' (S&P 500 + Nasdaq 100),
# 'small_cap' (Russell 2000), 'all'
CUSTOM_LIST = None  # Custom stock symbols (overrides sectors if set)
EXTRA_SYMBOLS: list[str] = [
    # Manually tracked — evaluated alongside the index scan regardless of index membership.
    # Add stocks here when the ground-truth model surfaces them outside our standard universe.
    "YSS",   # micro-cap, not in any standard index
    "WATT",  # micro-cap, not in any standard index
    "BKSY",  # Russell 2000 (excluded from large_cap scan) — +436% 12m, at 52w high 2025-04-15
    "PL",    # Russell 2000 (excluded from large_cap scan) — +1116% 12m, at 52w high 2025-04-15
    "UMAC",  # micro-cap drone play, below all standard indices — ground truth 2025-04-15
    "RNG",   # dropped from Nasdaq 100 — 96% proximity to 52w high, ground truth 2025-04-15
]

# Debugging
DEBUG = False  # Enable verbose output
MAX_NEW_ENTRIES_PER_CYCLE = 2  # Focus live/paper execution on only the best fresh setups each scan


# ==============================================================================
# ORDER EXECUTION SETTINGS (O'Neil rules)
# ==============================================================================

# Risk management
STOP_LOSS_PCT = 0.08  # 8% hard stop below buy price (O'Neil: never exceed 7-8%)
POSITION_RISK_PCT = 0.01  # Risk 1% of portfolio equity per trade (risk-based sizing)
POSITION_SIZE_PCT = 0.125  # Derived: 1% risk / 8% stop = 12.5% position weight
MAX_OPEN_POSITIONS = 5  # Maximum simultaneous positions (concentration guard)

# Entry behaviour
# When True: only submit entries during confirmed market-hours (09:30–16:00 ET).
# When False: orders queue for next open (useful for after-hours scanner runs).
ENTRY_MARKET_HOURS_ONLY = True

# Buy-zone enforcement (O'Neil: only enter within 5% above the pivot/buy point)
# Stocks that have already run more than BUY_ZONE_EXTENSION_PCT beyond their pivot
# are "too extended" and should not be chased.  O'Neil's rule is strict: entries
# beyond the buy zone have materially worse risk/reward because the stop is now
# far below the natural support level of the base.
BUY_ZONE_EXTENSION_PCT = 0.05  # 5% max extension above the pivot before entry is rejected
BUY_ZONE_UNDERCUT_TOLERANCE_PCT = 0.0  # Require entries at/above the pivot by default

# Staged scale-out tiers — (gain_target_pct, fraction_of_original_qty_to_sell)
# Remaining 25% after tier 3 is held on EMA trailing stop.
SCALE_OUT_TIERS: list[tuple[float, float]] = [
    (0.10, 0.25),
    (0.15, 0.25),
    (0.20, 0.25),
]

# Two-pass eviction: when portfolio is full, compare new signal RS vs open positions.
ENABLE_EVICTION: bool = True


# ==============================================================================
# DATA DOWNLOAD SETTINGS
# ==============================================================================

# Historical data periods
RS_CALCULATION_PERIOD = "14mo"  # Period for RS score calculation
CANSLIM_DATA_PERIOD = "1y"  # Period for CANSLIM evaluation
MARKET_TREND_PERIOD = "14mo"  # Period for market direction analysis
PRICE_HISTORY_BUFFER_DAYS = 120  # Extra days to download for indicators

# Caching
RS_CACHE_DIR = os.path.join(CACHE_DIR, "rs_score_cache")  # Directory for RS score cache
RS_CACHE_FILE = "rs_scores_cache.csv"  # Cache filename
TICKER_CACHE_DIR = os.path.join(CACHE_DIR, "ticker_cache")  # Directory for index ticker cache
TICKER_CACHE_EXPIRY_HOURS = 24  # How often to refresh ticker lists


# ==============================================================================
# TECHNICAL INDICATOR PARAMETERS
# ==============================================================================

# Exponential Moving Averages
EMA_SHORT = 8  # Short-term EMA period
EMA_LONG = 21  # Long-term EMA period
EMA_MARKET_50 = 50  # Market 50-day EMA
EMA_MARKET_200 = 200  # Market 200-day EMA

# Relative Strength Index
RSI_PERIOD = 14  # RSI calculation period


# ==============================================================================
# CANSLIM COMPONENT WEIGHTS (for composite score)
# O'Neil emphasizes C, A, and L as the most critical factors.
# ==============================================================================

# Per-component weights for the final composite CANSLIM score (must sum to 1.0)
CANSLIM_WEIGHT_C = 0.20  # Current quarterly earnings — critical
CANSLIM_WEIGHT_A = 0.15  # Annual earnings — critical
CANSLIM_WEIGHT_N = 0.10  # New products / new highs
CANSLIM_WEIGHT_S = 0.10  # Supply and demand
CANSLIM_WEIGHT_L = 0.20  # Leader or laggard — critical
CANSLIM_WEIGHT_I = 0.10  # Institutional sponsorship
CANSLIM_WEIGHT_M = 0.15  # Market direction — critical (3/4 stocks follow market)


# ==============================================================================
# C (CURRENT QUARTERLY EARNINGS) PARAMETERS
# ==============================================================================

C_GROWTH_TARGET = 0.25  # 25% YoY quarterly EPS growth target

# Sub-component weights within C score (must sum to 1.0)
C_GROWTH_WEIGHT = 0.60  # Weight for current quarter growth vs target
C_CONSISTENCY_WEIGHT = 0.20  # Weight for multiple quarters of 25%+ growth
C_ACCELERATION_WEIGHT = 0.20  # Weight for accelerating growth rates


# ==============================================================================
# A (ANNUAL EARNINGS) PARAMETERS
# ==============================================================================

A_GROWTH_TARGET = 0.25  # 25% annual EPS growth target
A_ROE_TARGET = 0.17  # 17% ROE minimum per O'Neil
A_MIN_YEARS_GROWTH = 3  # Check last 3 years for consistency

# Sub-component weights within A score (must sum to 1.0)
A_GROWTH_WEIGHT = 0.50  # Weight for most recent year's growth
A_CONSISTENCY_WEIGHT = 0.30  # Weight for multi-year consistency
A_ROE_WEIGHT = 0.20  # Weight for ROE check


# ==============================================================================
# N (NEW PRODUCTS / NEW HIGHS) PARAMETERS
# ==============================================================================

N_REVENUE_GROWTH_WEIGHT = 0.50  # Weight for revenue growth (was 0.7)
N_PROXIMITY_TO_HIGH_WEIGHT = 0.50  # Weight for proximity to 52-week high (was 0.3)
N_REVENUE_GROWTH_TARGET = 0.25  # 25% quarterly revenue growth target


# ==============================================================================
# S (SUPPLY AND DEMAND) PARAMETERS
# ==============================================================================

S_VOLUME_SURGE_THRESHOLD = 1.3  # Volume multiplier for surge detection (1.3 = 30% above avg)
S_BREAKOUT_PROXIMITY = 0.95  # Proximity to 52-week high for breakout (0.95 = within 5%)
S_POWER_GAP_LOOKBACK = 10  # Days to look back for Power Earnings Gaps
S_PEG_MIN_PROXIMITY = 0.85  # Min 52w proximity for a PEG to count (O'Neil: gap must be from a base near highs)
S_TURNOVER_CAP = 1.0  # Legacy: max turnover ratio for scoring

# Sub-component weights within S score (must sum to 1.0)
S_FLOAT_WEIGHT = 0.25  # Weight for float / shares outstanding
S_UP_DOWN_VOL_WEIGHT = 0.25  # Weight for up/down volume ratio
S_SURGE_BREAKOUT_WEIGHT = 0.30  # Weight for volume surge + breakout
S_POWER_GAP_WEIGHT = 0.20  # Weight for Power Earnings Gap


# ==============================================================================
# I (INSTITUTIONAL SPONSORSHIP) PARAMETERS
# ==============================================================================

I_INSTITUTIONAL_CAP = 1.0  # Legacy: max institutional ownership for scoring

# Sub-component weights within I score (must sum to 1.0)
I_LEVEL_WEIGHT = 0.60  # Weight for ownership level (sweet-spot curve)
I_TREND_WEIGHT = 0.40  # Weight for ownership trend (increasing/decreasing)


# ==============================================================================
# MARKET TREND SCORING PARAMETERS
# ==============================================================================

# EMA-based trend component weights (within the EMA portion of M score)
M_PRICE_ABOVE_200EMA_WEIGHT = 0.45  # Weight if price > 200-EMA
M_EMA_ALIGNMENT_WEIGHT = 0.25  # Weight if 21-EMA > 50-EMA > 200-EMA
M_50EMA_RISING_WEIGHT = 0.20  # Weight if 50-EMA is rising
M_PRICE_ABOVE_21EMA_WEIGHT = 0.10  # Weight if price > 21-EMA

# Market trend thresholds
M_BULLISH_THRESHOLD = 0.6  # Minimum score for bullish market
M_50EMA_RISING_LOOKBACK = 20  # Days to check if 50-EMA is rising

# O'Neil's Distribution Day parameters
M_DISTRIBUTION_LOOKBACK = 25  # Trading days to count distribution days
M_DISTRIBUTION_MIN_DECLINE = 0.002  # 0.2% minimum decline for distribution day
M_MAX_DISTRIBUTION_DAYS = 5  # 5+ distribution days = market top

# O'Neil's Follow-Through Day parameters
M_FOLLOW_THROUGH_MIN_PCT = 0.015  # 1.5% minimum gain for follow-through day
M_FOLLOW_THROUGH_MIN_DAY = 4  # Earliest day of rally for follow-through

# M score component weights (distribution/follow-through vs EMA trend)
M_DISTRIBUTION_WEIGHT = 0.30  # Weight for distribution day analysis
M_FOLLOW_THROUGH_WEIGHT = 0.15  # Weight for follow-through day detection
# Remaining weight (0.55) goes to EMA-based trend analysis


# ==============================================================================
# RS SCORE CALCULATION PARAMETERS
# ==============================================================================

# Quarterly performance weights (must sum to 1.0)
# O'Neil's IBD RS rating gives most recent quarter 2x weight
RS_Q1_WEIGHT = 0.40  # Most recent quarter (40%)
RS_Q2_WEIGHT = 0.20  # 2nd quarter (20%)
RS_Q3_WEIGHT = 0.20  # 3rd quarter (20%)
RS_Q4_WEIGHT = 0.20  # Oldest quarter (20%)

# RS score scaling
RS_PERCENTILE_MIN = 1  # Minimum RS score
RS_PERCENTILE_MAX = 99  # Maximum RS score
RS_PERCENTILE_MULTIPLIER = 98  # Scaling multiplier (max - min)

# Trading days per quarter (for RS calculation)
TRADING_DAYS_PER_QUARTER = 65  # Approximate trading days in a quarter


# ==============================================================================
# API SETTINGS
# ==============================================================================

# FMP (Financial Modeling Prep) configuration
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

# FMP fetch limits — sized for a paid plan with fuller data history.
# Callers can override by passing an explicit limit= argument.
FMP_QUARTERLY_LIMIT = 12           # 3 years of quarterly income statements
FMP_ANNUAL_LIMIT = 10              # 10 years of annual income statements
FMP_BALANCE_SHEET_LIMIT = 10       # 10 years of balance sheets
FMP_INSTITUTIONAL_HISTORY_LIMIT = 8  # quarterly institutional ownership snapshots (live scan)

# HTTP Retry settings
HTTP_RETRY_TOTAL = 5  # Maximum retry attempts
HTTP_RETRY_BACKOFF = 2  # Exponential backoff factor
HTTP_RETRY_STATUS_CODES = [429, 500, 502, 503, 504]  # Retry on these codes
FMP_SUPPRESS_REPEATED_ENDPOINT_ERRORS = True  # Log plan/404 endpoint failures once per session, then skip
HTTP_MAX_WORKERS = 5  # Parallel API requests
