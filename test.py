import yfinance as yf

# Try downloading recent data for a well-known ticker
df = yf.download("AAPL", period="5d", progress=False)
print(df)