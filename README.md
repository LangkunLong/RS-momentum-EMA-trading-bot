# High Momentum Stock Scanner

A Python-based stock screening tool that identifies high-quality stocks with strong relative strength momentum and pullback entry opportunities. The Goal is to use William O'Neil's CANSLIM metodology to find fast growing stocks in a bull market.

## Features

### **Relative Strength (RS) Momentum Scoring**
- Calculates relative strength vs SPY benchmark over 63-day periods
- Identifies stocks outperforming the broader market
- Configurable minimum RS score thresholds

### **Trend Analysis**
- Analyzes 60+ day trends for EMA adherence patterns
- Validates higher highs and higher lows progression
- Requires sustained holding of either 8EMA (>70%) or 21EMA (>80%)

### **Entry Signal**
- **8EMA Retest**: Price pullback within 2% of 8EMA after trending above
- **21EMA Retest**: Price pullback within 3% of 21EMA after trending above  
- **8EMA Reclaim**: Temporary break below 8EMA while holding 21EMA, then reclaiming 8EMA

### **Stock Focus**
- Ideally high beta moving stocks
  
## Installation

### Prerequisites
- Python 3.7+
- Internet connection for stock data retrieval

### Required Packages
```bash
pip install yfinance ta pandas numpy python-dotenv requests urllib3
```


## Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `USE_API` | `False` | Use Finnhub API for broader scanning |
| `MIN_MARKET_CAP` | `10e9` | Minimum market capitalization ($10B) |
| `MIN_RS_SCORE` | `5` | Minimum relative strength score |
| `MAX_WORKERS` | `2` | Number of concurrent analysis threads |

## File Structure

```
├── enhanced_trading_algo.py    # Core analysis algorithms
├── enhanced_scanner.py         # Main scanner application
├── scanner.py                  # Original scanner (legacy)
├── trading_algo.py            # Original trading algo (legacy)
├── .env                       # API keys (optional)
└── README.md                  # This file
```

### Relative Strength (RS) Score
- **Positive values**: Stock outperforming SPY
- **>10**: Strong outperformance
- **>20**: Exceptional outperformance

### Trend Score
- **>70**: Strong trend with good EMA adherence
- **>80**: Very strong trend with excellent EMA adherence

### Entry Signal Types
- **8EMA_Retest**: Conservative pullback to 8-day moving average
- **21EMA_Retest**: Deeper pullback to 21-day moving average
- **8EMA_Reclaim**: Recovery pattern after temporary weakness
