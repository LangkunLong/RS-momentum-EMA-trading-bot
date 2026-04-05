"""Backward-compatibility shim. Utilities now live in core.data_client."""

from core.data_client import (
    coerce_scalar,
    ensure_series,
    extract_float_series,
    normalize_price_dataframe,
)

__all__ = [
    "normalize_price_dataframe",
    "ensure_series",
    "coerce_scalar",
    "extract_float_series",
]
