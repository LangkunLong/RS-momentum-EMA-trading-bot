"""Backward-compatibility shim. Utilities now live in core.data_client."""
from core.data_client import (
    normalize_price_dataframe,
    ensure_series,
    coerce_scalar,
    extract_float_series,
)

__all__ = [
    "normalize_price_dataframe",
    "ensure_series",
    "coerce_scalar",
    "extract_float_series",
]
