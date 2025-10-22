# High Momentum Stock Scanner

A Python-based stock screening tool that identifies high-quality stocks with strong relative strength momentum and pullback entry opportunities. The scanner focuses on large-cap stocks (>$10B market cap) that are in sustained uptrends and showing technical pullback patterns for optimal entry points.

## Features

### 🚀 **Relative Strength (RS) Momentum Scoring**
- Calculates relative strength vs SPY benchmark over 63-day periods
- Identifies stocks outperforming the broader market
- Configurable minimum RS score thresholds

### 📈 **Advanced Trend Analysis**
- Analyzes 60+ day trends for EMA adherence patterns
- Validates higher highs and higher lows progression
- Requires sustained holding of either 8EMA (>70%) or 21EMA (>80%)

### 🎯 **Sophisticated Entry Signal Detection**
- **8EMA Retest**: Price pullback within 2% of 8EMA after trending above
- **21EMA Retest**: Price pullback within 3% of 21EMA after trending above  
- **8EMA Reclaim**: Temporary break below 8EMA while holding 21EMA, then reclaiming 8EMA

### 💎 **Quality Stock Focus**
- Filters for stocks with market cap >$10B (configurable)
- Curated list of 100+ high-quality large-cap stocks
- Optional Finnhub API integration for broader market scanning
  
## Installation

### Prerequisites
- Python 3.7+
- Internet connection for stock data retrieval

### Required Packages
```bash
pip install yfinance ta pandas numpy python-dotenv requests urllib3
```

### Optional: Finnhub API Setup
1. Sign up for a free account at [Finnhub.io](https://finnhub.io/)
2. Create a `.env` file in the project directory:
```
FINNHUB_API_KEY=your_api_key_here
```

## Quick Start

### Basic Usage (Curated Stock List)
```python
python enhanced_scanner.py
```

### Advanced Configuration
```python
from enhanced_scanner import scan_for_momentum_opportunities

# Customize parameters
opportunities = scan_for_momentum_opportunities(
    use_api=False,           # Use curated list vs API
    min_market_cap=10e9,     # $10B minimum market cap
    min_rs_score=10,         # Minimum RS momentum score
    max_workers=3            # Concurrent processing threads
)
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

## Output

### Console Output
The scanner provides real-time progress updates and displays results including:
- Stock symbol and RS momentum score
- Trend strength metrics (EMA adherence percentages)
- Current price and RSI levels
- Entry signal details with dates and types

### CSV Export
Automatically exports results to timestamped CSV files containing:
- All momentum and trend metrics
- Entry signal counts and details
- Latest signal information for each stock

## Understanding the Metrics

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

## Customization

### Adding Custom Stock Lists
Modify the `get_quality_stock_list()` function in `enhanced_scanner.py`:
```python
def get_quality_stock_list():
    custom_stocks = ['AAPL', 'MSFT', 'GOOGL']  # Add your symbols
    return custom_stocks
```

### Adjusting Entry Criteria
Modify parameters in `identify_pullback_entries()` function:
```python
# Tighten 8EMA retest criteria
if (abs(current_row['Distance_8EMA']) < 1.0 and  # Within 1% instead of 2%
    current_row['Above_8EMA']):
```

### Custom RS Calculation Periods
Adjust the calculation period in `calculate_rs_momentum()`:
```python
def calculate_rs_momentum(symbol, benchmark_symbol='SPY', period_days=126):  # 6 months instead of 3
```

## Troubleshooting

### Common Issues

**"No stocks found matching criteria"**
- Lower the `MIN_RS_SCORE` parameter
- Extend the analysis date range
- Check internet connection for data retrieval

**Rate limiting errors with Finnhub API**
- Reduce `MAX_WORKERS` to 1-2
- Add delays between API calls
- Consider using the curated stock list instead

**Memory issues with large datasets**
- Reduce the number of stocks analyzed
- Increase system memory allocation
- Process stocks in smaller batches

### Performance Tips

1. **Use curated list** for faster scanning without API limits
2. **Limit concurrent workers** to avoid overwhelming data sources
3. **Cache results** for repeated analysis of the same time periods
4. **Filter by volume** to focus on liquid stocks only

## Contributing

Contributions are welcome! Areas for enhancement:
- Additional technical indicators (MACD, Bollinger Bands, etc.)
- Alternative momentum calculations (Price vs. Volume, etc.)
- Real-time data integration
- Backtesting framework
- Web interface development

## Disclaimer

This tool is for educational and research purposes only. It is not financial advice. Always:
- Conduct your own research before making investment decisions
- Consider your risk tolerance and investment objectives  
- Consult with qualified financial professionals
- Test strategies with paper trading before using real money

Past performance does not guarantee future results. Stock trading involves substantial risk of loss.

## License

This project is open source and available under the MIT License.

---

**Happy Trading! 📈**
