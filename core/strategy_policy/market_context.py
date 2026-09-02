"""Trusted causal construction of immutable strategy market context."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from .contracts import BenchmarkContextV1, MarketContextV1


BENCHMARK_CONTEXT_SYMBOLS = ("SPY", "QQQ", "IWM")


def _causal_closes(
    closes: pd.DataFrame,
    symbol: str,
    session: pd.Timestamp,
) -> pd.Series:
    if symbol not in closes.columns:
        return pd.Series(dtype=float)
    values = pd.to_numeric(closes.loc[:session, symbol], errors="coerce").astype(float)
    if (
        values.empty
        or pd.Timestamp(values.index[-1]).normalize() != session
        or not math.isfinite(float(values.iloc[-1]))
        or float(values.iloc[-1]) <= 0
    ):
        return pd.Series(dtype=float)
    return values


def build_market_context(
    *,
    session: pd.Timestamp,
    oneil_regime: str,
    distribution_days: int,
    follow_through: bool,
    closes: pd.DataFrame,
    active_constituents: Iterable[str],
    rs_scores: Mapping[str, float],
) -> MarketContextV1:
    """Build one complete context using data known at ``session`` close."""

    normalized_session = pd.Timestamp(session).normalize()
    active = tuple(dict.fromkeys(str(symbol).upper() for symbol in active_constituents))
    if not active:
        raise ValueError("market context active constituent cross-section is empty")

    benchmarks: list[BenchmarkContextV1] = []
    for symbol in BENCHMARK_CONTEXT_SYMBOLS:
        history = _causal_closes(closes, symbol, normalized_session)
        window_200 = history.iloc[-200:]
        if (
            len(window_200) != 200
            or not np.isfinite(window_200).all()
            or bool((window_200 <= 0).any())
        ):
            raise ValueError(f"market context {symbol} reference window is incomplete")
        returns = window_200.iloc[-21:].pct_change().dropna()
        if len(returns) != 20:
            raise ValueError(f"market context {symbol} volatility window is incomplete")
        close = float(window_200.iloc[-1])
        volatility = float(returns.std(ddof=1) * math.sqrt(252.0))
        if not math.isfinite(volatility):
            raise ValueError(f"market context {symbol} volatility is invalid")
        benchmarks.append(
            BenchmarkContextV1(
                symbol=symbol,
                close_to_sma_50_fraction=close / float(window_200.iloc[-50:].mean()) - 1.0,
                close_to_sma_200_fraction=close / float(window_200.mean()) - 1.0,
                realized_volatility_20d_fraction=volatility,
            )
        )

    above_50: list[bool] = []
    above_200: list[bool] = []
    eligible_rs: list[float] = []
    for symbol in active:
        raw_history = _causal_closes(closes, symbol, normalized_session)
        history = raw_history[np.isfinite(raw_history) & (raw_history > 0)]
        if len(history) >= 50:
            above_50.append(float(history.iloc[-1]) > float(history.iloc[-50:].mean()))
        if len(history) >= 200:
            above_200.append(float(history.iloc[-1]) > float(history.iloc[-200:].mean()))
        raw_rs = rs_scores.get(symbol)
        if (
            type(raw_rs) is not bool
            and isinstance(raw_rs, (int, float))
            and math.isfinite(float(raw_rs))
        ):
            eligible_rs.append(float(raw_rs))
    if not above_50 or not above_200 or not eligible_rs:
        raise ValueError("market context eligible constituent cross-section is empty")

    active_count = len(active)
    return MarketContextV1(
        schema_version=1,
        session=normalized_session.date().isoformat(),
        oneil_regime=oneil_regime,
        distribution_days=distribution_days,
        follow_through=follow_through,
        benchmarks=tuple(benchmarks),
        active_constituent_count=active_count,
        breadth_above_50_fraction=sum(above_50) / len(above_50),
        breadth_50_coverage_fraction=len(above_50) / active_count,
        breadth_above_200_fraction=sum(above_200) / len(above_200),
        breadth_200_coverage_fraction=len(above_200) / active_count,
        median_rs_score=float(np.median(eligible_rs)),
        rs_at_least_80_fraction=sum(value >= 80.0 for value in eligible_rs) / len(eligible_rs),
        rs_coverage_fraction=len(eligible_rs) / active_count,
    )


__all__ = ["BENCHMARK_CONTEXT_SYMBOLS", "build_market_context"]
