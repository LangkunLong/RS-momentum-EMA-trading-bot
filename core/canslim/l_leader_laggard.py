"""L - Leader or Laggard.

Evaluates whether a stock is a market leader or laggard using Relative Strength (RS).
RS compares a stock's performance against the market benchmark over multiple quarters.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from types import MappingProxyType

import numpy as np
import pandas as pd

from core.momentum_analysis import calculate_rs_momentum


def finite_rs_snapshot(
    rs_snapshot: Mapping[str, float],
    *,
    required_symbols: Iterable[str],
) -> Mapping[str, float]:
    """Validate RS inputs and require the complete active PIT union.

    Extra observations (for example an already-held former constituent) are
    retained, but SPY/QQQ/IWM can never substitute for a missing active member.
    Booleans and non-finite values are rejected instead of being coerced into a
    score or silently treated as missing.
    """
    validated: dict[str, float] = {}
    for symbol, value in rs_snapshot.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol.strip() != symbol
            or symbol.upper() != symbol
        ):
            raise ValueError("RS snapshot symbol must be canonical uppercase text")
        if isinstance(value, (bool, np.bool_)):
            raise ValueError("RS snapshot values must be finite numbers, not booleans")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("RS snapshot values must be finite numbers") from exc
        if not math.isfinite(number):
            raise ValueError("RS snapshot values must be finite numbers")
        if not 0.0 <= number <= 100.0:
            raise ValueError("RS snapshot values must be within [0, 100]")
        validated[symbol] = number

    required = frozenset(required_symbols)
    if any(not isinstance(symbol, str) for symbol in required):
        raise ValueError("required RS symbols must be strings")
    missing = sorted(required.difference(validated))
    if missing:
        raise ValueError(
            "RS snapshot does not cover the complete active PIT union: "
            f"{missing[:5]}"
        )
    return MappingProxyType(validated)


def calculate_group_rs(
    group_id: str,
    *,
    active_symbols: Iterable[str],
    symbol_groups: Mapping[str, str],
    rs_snapshot: Mapping[str, float],
) -> float | None:
    """Return the arithmetic mean RS of active members in ``group_id``.

    The caller supplies the complete active PIT union.  Symbols outside that
    union (candidate panels, market references, and inactive identities) do not
    enter the denominator.  Missing group assignments are explicit omissions;
    a group with no classified active member is unavailable (``None``).
    """
    active = tuple(sorted(set(active_symbols)))
    validated = finite_rs_snapshot(rs_snapshot, required_symbols=active)
    members = tuple(
        symbol
        for symbol in active
        if symbol_groups.get(symbol) == group_id
    )
    if not members:
        return None
    values = tuple(validated[symbol] for symbol in members)
    return sum(values) / len(values)


def evaluate_l(symbol: str, rs_scores_df: pd.DataFrame) -> tuple[float, float]:
    """Evaluate L (Leader or Laggard) score based on Relative Strength.

    Args:
        symbol: Stock ticker symbol
        rs_scores_df: DataFrame containing pre-calculated RS scores for all symbols

    Returns:
        tuple: (score, rs_score) where score is 0-1 and rs_score is 1-99
    """
    rs_score = calculate_rs_momentum(symbol, rs_scores_df)
    score = rs_score / 100.0  # Normalize to 0-1 range

    return score, rs_score
