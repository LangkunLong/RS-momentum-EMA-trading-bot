# Global settings and constants
START_DATE = '2024-01-01'
MIN_MARKET_CAP = 10e9  # Minimum market cap for stock screening
MIN_RS_SCORE = 5       # Minimum Relative Strength score
MIN_CANSLIM_SCORE = 70  # Minimum composite CAN SLIM score (0-100 scale
MAX_WORKERS = 3        # Maximum number of threads for parallel processing
USE_API = False        # Whether to use API for stock screening
SECTORS = None         # List of sectors to filter stocks
CUSTOM_LIST = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META']  # Custom stock list