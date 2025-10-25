"""Utilities for evaluating CAN SLIM components.

This module focuses on translating readily-available fundamentals and
technicals from Yahoo Finance into the seven CAN SLIM pillars:

* **C**urrent quarterly earnings growth
* **A**nnual earnings growth
* **N**ew products/price leadership (approximated via revenue growth and
  proximity to 52-week highs)
* **S**upply and demand dynamics
* **L**eader or laggard (relative strength versus the benchmark)
* **I**nstitutional sponsorship
* **M**arket direction (SPY trend proxy)

Each component is normalised to a 0-1 range so the composite score can be
expressed on a 0-100 scale
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from core.momentum_analysis import calculate_rs_momentum

#Light-weight representation of the general market trend
@dataclass
class MarketTrend:

    symbol: str
    score: float
    is_bullish: bool
    latest_close: Optional[float]
    indicators: Dict[str, float]