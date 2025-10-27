"""Utility helpers for working with pandas price data.

These helpers normalise the various structures returned by yfinance so the
rest of the codebase can operate on predictable Series objects and Python
scalars.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


"""Return a frame with flattened columns for single-ticker downloads.

yfinance sometimes returns a MultiIndex when historical data is fetched
even for a single symbol. Indicator libraries expect one-dimensional
Series, so we strip any redundant ticker level here.
"""
def normalize_price_dataframe(df: pd.DataFrame) -> pd.DataFrame:

    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()

        if df.columns.nlevels == 2:
            level0 = df.columns.get_level_values(0)
            level1 = df.columns.get_level_values(1)

            price_like = {"open", "high", "low", "close", "adj close", "volume"}
            if any(str(val).lower() in price_like for val in level0):
                df.columns = level0
            else:
                df.columns = level1
        else:
            df.columns = ["_".join(str(part) for part in col if part)
                          for col in df.columns]

    return df

# convert to 1D series
def ensure_series(data: pd.Series | pd.DataFrame) -> pd.Series:

    if isinstance(data, pd.DataFrame):
        if data.shape[1] == 0:
            raise ValueError("Cannot coerce an empty DataFrame into a Series")
        data = data.squeeze("columns")

    if not isinstance(data, pd.Series):
        raise TypeError(f"Expected pandas Series, received {type(data)!r}")

    return data

# convert numpy to float
def coerce_scalar(value: Any) -> float:

    if isinstance(value, pd.DataFrame):
        if value.shape[1] == 0:
            raise ValueError("Cannot extract a scalar from an empty DataFrame")
        value = value.iloc[:, 0]

    if isinstance(value, pd.Series):
        if value.empty:
            raise ValueError("Cannot extract a scalar from an empty Series")
        value = value.iloc[-1]

    if isinstance(value, np.ndarray):
        if value.size == 0:
            raise ValueError("Cannot extract a scalar from an empty ndarray")
        value = value.item()

    return float(value)


# extract as float
def extract_float_series(df: pd.DataFrame, column: str) -> pd.Series:

    if column not in df:
        raise KeyError(f"Column '{column}' not found in dataframe")

    series = ensure_series(df[column])
    return series.astype(float)