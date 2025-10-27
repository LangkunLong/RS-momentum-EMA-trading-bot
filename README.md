# High Momentum Stock Scanner

A stock screening tool that identifies high-quality stocks with strong relative strength momentum and pullback entry opportunities. The Goal is to use William O'Neil's CANSLIM metodology to find fast growing stocks in a bull market.

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

### Sample Output:
```
Stock Scanner Configuration:
- Use API: False
- Min Market Cap: $10B
- Min RS Score: 5
- Max Workers: 2
- Sectors: ['growth_high_beta', 'crypto_fintech']
- Custom List: No
============================================================
HIGH MOMENTUM PULLBACK SCANNER
============================================================
Using curated stock list for sectors: ['growth_high_beta', 'crypto_fintech']
Scanning 20 stocks for momentum opportunities...
Minimum RS Score: 5
Minimum CAN SLIM Score: 70
C:\Users\langk\OneDrive\Desktop\Side projects\trading bot\core\canslim.py:83: FutureWarning: YF.download() has changed argument auto_adjust default to True
  data = yf.download(
Market Trend (SPY): Bullish | Score: 100%
Validating tickers with Yahoo Finance...
20 valid tickers will be scanned.
✗ DDOG - No signals (1/20)
✗ FSLY - No signals (2/20)
✗ SOFI - No signals (3/20)
✗ NET - No signals (4/20)
✗ AFRM - No signals (5/20)
✗ MSTR - No signals (6/20)
✗ CRWV - No signals (7/20)
✗ OKTA - No signals (8/20)
✗ ZM - No signals (9/20)
✗ HOOD - No signals (10/20)
✗ ROKU - No signals (11/20)
✗ COIN - No signals (12/20)
✗ OKLO - No signals (13/20)
✗ DOCU - No signals (14/20)
✗ TWLO - No signals (15/20)
✗ LC - No signals (16/20)
✗ NBIS - No signals (17/20)
✗ CRWD - No signals (18/20)
✗ UPST - No signals (19/20)
✓ IREN - RS: 294.7 (20/20)

Scan complete!
Analyzed: 20 stocks
Failed: 0 stocks
Opportunities found: 1 stocks

================================================================================
HIGH MOMENTUM CAN SLIM OPPORTUNITIES (1 stocks found)
================================================================================
Market Direction (SPY): Bullish | Score: 100%
Latest Close: $685.24 | 21 EMA: $667.98 | 50 EMA: $657.42 | 200 EMA: $622.22

1. IREN
   Price: $64.99 | RSI: 63.6
   RS Score: 294.7 | Trend Score: 91.7
   Trend Details: 8EMA 85.0% | 21EMA 98.3% | Higher Highs True | Higher Lows True
   CAN SLIM Composite Score: 71.0
     C - Current earnings: 100%
     A - Annual earnings: 100%
     N - New product/price leadership: 97%
     S - Supply & demand: 0%
     L - Leader vs laggard: 100%
     I - Institutional sponsorship: 0%
     M - Market direction: 100%
   Fundamentals: Quarterly EPS Growth 2.94 | Annual EPS Growth 4.01 | Revenue Growth 2.55
   Liquidity: Avg Volume (50d) 39708430 | Turnover Ratio n/a | 52w High Proximity 0.93
   Recent Entry Signals:
     2025-10-21: 21EMA_Retest | Close $55.19 | RSI 54.5
     2025-10-23: 21EMA_Retest | Close $55.86 | RSI 54.8
Results exported to momentum_opportunities_20251027_172557.csv

Scan completed!
```
