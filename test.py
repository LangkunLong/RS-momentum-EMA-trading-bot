import yfinance as yf

# Try downloading recent data for a well-known ticker
df = yf.download("SQ", period="5d", progress=False, auto_adjust=True)
print(df)