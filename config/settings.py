# Global settings and constants
START_DATE = '2025-01-01'
MIN_MARKET_CAP = 10e9  # Minimum market cap for stock screening
MIN_RS_SCORE = 5       # Minimum Relative Strength score
MAX_WORKERS = 3        # Maximum number of threads for parallel processing
USE_API = False        # Whether to use API for stock screening
SECTORS = None         # List of sectors to filter stocks
CUSTOM_LIST = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META']  # Custom stock list