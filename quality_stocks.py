"""
Quality Stock Lists Configuration
=================================

This file contains curated lists of high-quality stocks organized by sector and market cap.
Edit these lists to customize your scanning universe.
"""

# Mega Cap Technology (>$500B)
MEGA_CAP_TECH = [
    'AAPL',   # Apple Inc
    'MSFT',   # Microsoft Corporation
    'GOOGL',  # Alphabet Inc Class A
    'GOOG',   # Alphabet Inc Class C
    'AMZN',   # Amazon.com Inc
    'NVDA',   # NVIDIA Corporation
    'META',   # Meta Platforms Inc
    'TSLA',   # Tesla Inc
]

# Large Cap Technology ($50B - $500B)
LARGE_CAP_TECH = [
    'NFLX',   # Netflix Inc
    'ADBE',   # Adobe Inc
    'CRM',    # Salesforce Inc
    'ORCL',   # Oracle Corporation
    'AVGO',   # Broadcom Inc
    'INTC',   # Intel Corporation
    'AMD',    # Advanced Micro Devices
    'QCOM',   # Qualcomm Incorporated
    'TXN',    # Texas Instruments
    'CSCO',   # Cisco Systems Inc
    'ACN',    # Accenture plc
    'IBM',    # International Business Machines
    'INTU',   # Intuit Inc
    'NOW',    # ServiceNow Inc
    'PANW',   # Palo Alto Networks Inc
    'PLTR',   # Palantir Technologies Inc
    'SNOW',   # Snowflake Inc
    'ZM',     # Zoom Video Communications
]

# Financial Services
FINANCIAL_SERVICES = [
    'JPM',    # JPMorgan Chase & Co
    'BAC',    # Bank of America Corporation
    'WFC',    # Wells Fargo & Company
    'C',      # Citigroup Inc
    'GS',     # Goldman Sachs Group Inc
    'MS',     # Morgan Stanley
    'AXP',    # American Express Company
    'V',      # Visa Inc
    'MA',     # Mastercard Incorporated
    'PYPL',   # PayPal Holdings Inc
    'BRK.B',  # Berkshire Hathaway Inc Class B
    'USB',    # U.S. Bancorp
    'TFC',    # Truist Financial Corporation
    'PNC',    # PNC Financial Services Group
    'SCHW',   # Charles Schwab Corporation
    'BLK',    # BlackRock Inc
    'SPGI',   # S&P Global Inc
    'CME',    # CME Group Inc
    'ICE',    # Intercontinental Exchange Inc
]

# Healthcare & Pharmaceuticals
HEALTHCARE = [
    'JNJ',    # Johnson & Johnson
    'UNH',    # UnitedHealth Group Incorporated
    'PFE',    # Pfizer Inc
    'ABBV',   # AbbVie Inc
    'MRK',    # Merck & Co Inc
    'TMO',    # Thermo Fisher Scientific Inc
    'ABT',    # Abbott Laboratories
    'DHR',    # Danaher Corporation
    'BMY',    # Bristol-Myers Squibb Company
    'AMGN',   # Amgen Inc
    'GILD',   # Gilead Sciences Inc
    'CVS',    # CVS Health Corporation
    'LLY',    # Eli Lilly and Company
    'MDT',    # Medtronic plc
    'ISRG',   # Intuitive Surgical Inc
    'REGN',   # Regeneron Pharmaceuticals Inc
    'VRTX',   # Vertex Pharmaceuticals Incorporated
    'BIIB',   # Biogen Inc
]

# Consumer Discretionary
CONSUMER_DISCRETIONARY = [
    'AMZN',   # Amazon.com Inc (also in tech)
    'TSLA',   # Tesla Inc (also in tech)
    'HD',     # Home Depot Inc
    'MCD',    # McDonald's Corporation
    'NKE',    # Nike Inc
    'SBUX',   # Starbucks Corporation
    'TJX',    # TJX Companies Inc
    'LOW',    # Lowe's Companies Inc
    'TGT',    # Target Corporation
    'DIS',    # Walt Disney Company
    'BKNG',   # Booking Holdings Inc
    'ABNB',   # Airbnb Inc
    'UBER',   # Uber Technologies Inc
    'DASH',   # DoorDash Inc
    'SHOP',   # Shopify Inc
    'ETSY',   # Etsy Inc
    'LULU',   # Lululemon Athletica Inc
    'ROST',   # Ross Stores Inc
]

# Consumer Staples
CONSUMER_STAPLES = [
    'PG',     # Procter & Gamble Company
    'KO',     # Coca-Cola Company
    'PEP',    # PepsiCo Inc
    'WMT',    # Walmart Inc
    'COST',   # Costco Wholesale Corporation
    'CL',     # Colgate-Palmolive Company
    'KMB',    # Kimberly-Clark Corporation
    'GIS',    # General Mills Inc
    'K',      # Kellogg Company
    'HSY',    # Hershey Company
    'MDLZ',   # Mondelez International Inc
    'KHC',    # Kraft Heinz Company
    'CLX',    # Clorox Company
    'SYY',    # Sysco Corporation
    'TSN',    # Tyson Foods Inc
]

# Industrial
INDUSTRIAL = [
    'BA',     # Boeing Company
    'CAT',    # Caterpillar Inc
    'GE',     # General Electric Company
    'MMM',    # 3M Company
    'HON',    # Honeywell International Inc
    'UPS',    # United Parcel Service Inc
    'RTX',    # Raytheon Technologies Corporation
    'LMT',    # Lockheed Martin Corporation
    'DE',     # Deere & Company
    'NOC',    # Northrop Grumman Corporation
    'FDX',    # FedEx Corporation
    'WM',     # Waste Management Inc
    'EMR',    # Emerson Electric Co
    'ETN',    # Eaton Corporation plc
    'ITW',    # Illinois Tool Works Inc
    'PH',     # Parker-Hannifin Corporation
    'CMI',    # Cummins Inc
]

# Energy
ENERGY = [
    'XOM',    # Exxon Mobil Corporation
    'CVX',    # Chevron Corporation
    'COP',    # ConocoPhillips
    'EOG',    # EOG Resources Inc
    'SLB',    # Schlumberger Limited
    'PSX',    # Phillips 66
    'VLO',    # Valero Energy Corporation
    'MPC',    # Marathon Petroleum Corporation
    'OXY',    # Occidental Petroleum Corporation
    'KMI',    # Kinder Morgan Inc
    'WMB',    # Williams Companies Inc
    'HAL',    # Halliburton Company
    'BKR',    # Baker Hughes Company
    'DVN',    # Devon Energy Corporation
    'FANG',   # Diamondback Energy Inc
]

# Communication Services
COMMUNICATION_SERVICES = [
    'GOOGL',  # Alphabet Inc (also in tech)
    'META',   # Meta Platforms Inc (also in tech)
    'NFLX',   # Netflix Inc (also in tech)
    'DIS',    # Walt Disney Company (also in consumer disc)
    'CMCSA',  # Comcast Corporation
    'T',      # AT&T Inc
    'VZ',     # Verizon Communications Inc
    'TMUS',   # T-Mobile US Inc
    'CHTR',   # Charter Communications Inc
    'PARA',   # Paramount Global
    'WBD',    # Warner Bros Discovery Inc
    'SPOT',   # Spotify Technology SA
    'PINS',   # Pinterest Inc
    'SNAP',   # Snap Inc
    'TWTR',   # Twitter Inc (if still public)
]

# Utilities
UTILITIES = [
    'NEE',    # NextEra Energy Inc
    'DUK',    # Duke Energy Corporation
    'SO',     # Southern Company
    'D',      # Dominion Energy Inc
    'AEP',    # American Electric Power Company Inc
    'EXC',    # Exelon Corporation
    'XEL',    # Xcel Energy Inc
    'PEG',    # Public Service Enterprise Group Inc
    'ED',     # Consolidated Edison Inc
    'EIX',    # Edison International
    'PPL',    # PPL Corporation
    'WEC',    # WEC Energy Group Inc
    'AWK',    # American Water Works Company Inc
]

# Real Estate Investment Trusts (REITs)
REAL_ESTATE = [
    'AMT',    # American Tower Corporation
    'PLD',    # Prologis Inc
    'CCI',    # Crown Castle International Corp
    'EQIX',   # Equinix Inc
    'WELL',   # Welltower Inc
    'DLR',    # Digital Realty Trust Inc
    'PSA',    # Public Storage
    'EXR',    # Extended Stay America Inc
    'CBRE',   # CBRE Group Inc
    'AVB',    # AvalonBay Communities Inc
    'EQR',    # Equity Residential
    'SPG',    # Simon Property Group Inc
    'O',      # Realty Income Corporation
    'SBAC',   # SBA Communications Corporation
]

# Materials
MATERIALS = [
    'LIN',    # Linde plc
    'APD',    # Air Products and Chemicals Inc
    'SHW',    # Sherwin-Williams Company
    'FCX',    # Freeport-McMoRan Inc
    'NUE',    # Nucor Corporation
    'DOW',    # Dow Inc
    'DD',     # DuPont de Nemours Inc
    'PPG',    # PPG Industries Inc
    'ECL',    # Ecolab Inc
    'NEM',    # Newmont Corporation
    'VMC',    # Vulcan Materials Company
    'MLM',    # Martin Marietta Materials Inc
    'PKG',    # Packaging Corporation of America
    'IP',     # International Paper Company
    'CF',     # CF Industries Holdings Inc
]

# Cryptocurrency & Fintech
CRYPTO_FINTECH = [
    'COIN',   # Coinbase Global Inc
    'MSTR',   # MicroStrategy Incorporated
    'HOOD',   # Robinhood Markets Inc
    'AFRM',   # Affirm Holdings Inc
    'SOFI',   # SoFi Technologies Inc
    'UPST',   # Upstart Holdings Inc
    'LC',     # LendingClub Corporation
]

# Emerging Growth & High Beta
GROWTH_HIGH_BETA = [
    'ROKU',   # Roku Inc
    'ZM',     # Zoom Video Communications (also in tech)
    'DOCU',   # DocuSign Inc
    'CRWD',   # CrowdStrike Holdings Inc
    'OKTA',   # Okta Inc
    'DDOG',   # Datadog Inc
    'NET',    # Cloudflare Inc
    'FSLY',   # Fastly Inc
    'TWLO',   # Twilio Inc
]

# Defensive Dividend Stocks
DIVIDEND_DEFENSIVE = [
    'JNJ',    # Johnson & Johnson (also in healthcare)
    'PG',     # Procter & Gamble (also in staples)
    'KO',     # Coca-Cola (also in staples)
    'PEP',    # PepsiCo (also in staples)
    'T',      # AT&T (also in communication)
    'VZ',     # Verizon (also in communication)
    'XOM',    # Exxon Mobil (also in energy)
    'CVX',    # Chevron (also in energy)
    'ABBV',   # AbbVie (also in healthcare)
    'MO',     # Altria Group Inc
    'PM',     # Philip Morris International Inc
    'BTI',    # British American Tobacco p.l.c.
]

# Predefined Stock Lists
STOCK_LISTS = {
    'mega_cap_tech': MEGA_CAP_TECH,
    'large_cap_tech': LARGE_CAP_TECH,
    'financial_services': FINANCIAL_SERVICES,
    'healthcare': HEALTHCARE,
    'consumer_discretionary': CONSUMER_DISCRETIONARY,
    'consumer_staples': CONSUMER_STAPLES,
    'industrial': INDUSTRIAL,
    'energy': ENERGY,
    'communication_services': COMMUNICATION_SERVICES,
    'utilities': UTILITIES,
    'real_estate': REAL_ESTATE,
    'materials': MATERIALS,
    'crypto_fintech': CRYPTO_FINTECH,
    'growth_high_beta': GROWTH_HIGH_BETA,
    'dividend_defensive': DIVIDEND_DEFENSIVE,
}

def get_quality_stock_list(sectors=None, exclude_sectors=None):
    """
    Get a combined list of quality stocks from specified sectors.
    
    Args:
        sectors (list): List of sector names to include. If None, includes all sectors.
        exclude_sectors (list): List of sector names to exclude.
    
    Returns:
        list: Combined list of unique stock symbols
    """
    if sectors is None:
        # Default comprehensive list
        sectors = [
            'mega_cap_tech', 'large_cap_tech', 'financial_services', 
            'healthcare', 'consumer_discretionary', 'consumer_staples',
            'industrial', 'energy', 'communication_services', 'utilities',
            'real_estate', 'materials'
        ]
    
    if exclude_sectors is None:
        exclude_sectors = []
    
    # Filter sectors
    sectors = [s for s in sectors if s not in exclude_sectors]
    
    # Combined list
    combined_stocks = []
    for sector in sectors:
        if sector in STOCK_LISTS:
            combined_stocks.extend(STOCK_LISTS[sector])
    
    # Remove duplicates and return
    return list(set(combined_stocks))

def get_sector_stocks(sector_name):
    """
    Get stocks from a specific sector.
    
    Args:
        sector_name (str): Name of the sector
    
    Returns:
        list: List of stock symbols in the sector
    """
    return STOCK_LISTS.get(sector_name, [])

def get_available_sectors():
    """
    Get list of available sector names.
    
    Returns:
        list: List of available sector names
    """
    return list(STOCK_LISTS.keys())

def get_custom_watchlist():

    custom_list = [
        'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA',
        'TSLA', 'META', 'NFLX', 'CRM',
    ]
    return custom_list

# Usage examples:
if __name__ == "__main__":
    print("Available sectors:", get_available_sectors())
    print("\nTech stocks count:", len(get_sector_stocks('mega_cap_tech')))
    
    # Example: Get only tech and healthcare stocks
    tech_health = get_quality_stock_list(['mega_cap_tech', 'healthcare'])
    print(f"\nTech + Healthcare stocks: {len(tech_health)} symbols")
    
    # Example: Get all except utilities and real estate
    most_sectors = get_quality_stock_list(exclude_sectors=['utilities', 'real_estate'])
    print(f"Most sectors (excluding utilities/REIT): {len(most_sectors)} symbols")
    
    # Example: Get custom watchlist
    custom = get_custom_watchlist()
    print(f"\nCustom watchlist: {len(custom)} symbols")