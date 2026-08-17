"""Pure completed-daily-bar candidate snapshot calculations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from numbers import Real
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from config import settings
from core.canslim.m_market_direction import MarketTrend
from core.canslim.s_supply_demand import _detect_breakout, _detect_volume_surge
from core.momentum_analysis import calculate_weighted_performance


_ROW_FIELDS = (
    "symbol",
    "as_of_session",
    "technical_eligible",
    "tomorrow_executable",
    "blocking_reasons",
    "warnings",
    "rs_score",
    "weighted_performance",
    "close",
    "prior_close",
    "pivot",
    "extension",
    "proximity_to_52week_high",
    "volume_ratio_50d",
    "average_dollar_volume_50d",
    "atr_pct_20d",
    "realized_volatility_20d",
    "normalized_trigger_gap",
)


@dataclass(frozen=True)
class AfterCloseSnapshot:
    """Read-only technical state evaluated from completed daily bars."""

    as_of_session: date
    market: MarketTrend
    rows: tuple[dict[str, object], ...]
    summary: dict[str, int]


def build_after_close_snapshot(
    price_by_symbol: Mapping[str, pd.DataFrame],
    *,
    market: MarketTrend,
    expected_symbols: Sequence[str],
) -> AfterCloseSnapshot:
    """Build a deterministic, advisory snapshot from already-fetched OHLCV bars."""
    spy_history = price_by_symbol.get("SPY")
    if spy_history is None or spy_history.empty:
        raise ValueError("SPY price history is required to determine the completed session")

    as_of_session = _session_date(spy_history)
    normalized_prices = {str(symbol).upper(): history for symbol, history in price_by_symbol.items()}
    symbols = sorted({str(symbol).upper() for symbol in expected_symbols})
    preliminary = [_build_row(symbol, normalized_prices.get(symbol), as_of_session) for symbol in symbols]

    performances = pd.Series(
        {
            symbol: _weighted_performance(history)
            for symbol, history in normalized_prices.items()
            if _is_current_and_sufficient(history, as_of_session)
        },
        dtype="float64",
    ).dropna()
    rs_scores = performances.rank(pct=True) * settings.RS_PERCENTILE_MULTIPLIER + settings.RS_PERCENTILE_MIN

    rows = []
    for row in preliminary:
        score = rs_scores.get(str(row["symbol"]))
        row["weighted_performance"] = _safe_builtin(performances.get(str(row["symbol"])))
        row["rs_score"] = _safe_builtin(score)
        _apply_rs_gate(row, market=market)
        rows.append(_json_safe(row))

    rows.sort(key=_rank_key)
    frozen_rows = tuple(rows)
    summary = {
        "total_symbols": len(frozen_rows),
        "technical_eligible": sum(bool(row["technical_eligible"]) for row in frozen_rows),
        "tomorrow_executable": sum(bool(row["tomorrow_executable"]) for row in frozen_rows),
        "blocked": sum(bool(row["blocking_reasons"]) for row in frozen_rows),
    }
    return AfterCloseSnapshot(as_of_session=as_of_session, market=market, rows=frozen_rows, summary=summary)


def write_after_close_snapshot(
    snapshot: AfterCloseSnapshot,
    output_dir: Path,
    *,
    generated_at: datetime,
) -> tuple[Path, Path]:
    """Persist all snapshot rows as CSV and strict JSON, including empty snapshots."""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = snapshot.as_of_session.isoformat()
    csv_path = output_dir / f"after_close_snapshot_{suffix}.csv"
    json_path = output_dir / f"after_close_snapshot_{suffix}.json"

    pd.DataFrame(snapshot.rows, columns=_ROW_FIELDS).to_csv(csv_path, index=False)
    payload = {
        "as_of_session": snapshot.as_of_session.isoformat(),
        "generated_at": generated_at.isoformat(),
        "summary": snapshot.summary,
        "market": _market_payload(snapshot.market),
        "rules": {
            "min_rs_score": settings.MIN_RS_SCORE,
            "breakout_proximity": settings.S_BREAKOUT_PROXIMITY,
            "volume_surge_threshold": settings.S_VOLUME_SURGE_THRESHOLD,
            "buy_zone_extension_pct": settings.BUY_ZONE_EXTENSION_PCT,
            "buy_zone_undercut_tolerance_pct": settings.BUY_ZONE_UNDERCUT_TOLERANCE_PCT,
        },
        "shortlist": [row for row in snapshot.rows if row["tomorrow_executable"]],
        "near_misses": [row for row in snapshot.rows if not row["tomorrow_executable"]],
        "rows": snapshot.rows,
        "artifact_provenance": {"calculation": "completed_daily_bars", "advisory_only": True},
    }
    json_path.write_text(json.dumps(_json_safe(payload), allow_nan=False, sort_keys=True), encoding="utf-8")
    return csv_path, json_path


def _build_row(symbol: str, history: pd.DataFrame | None, as_of_session: date) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": symbol,
        "as_of_session": as_of_session.isoformat(),
        "technical_eligible": False,
        "tomorrow_executable": False,
        "blocking_reasons": "",
        "warnings": "",
        "rs_score": None,
        "weighted_performance": None,
        "close": None,
        "prior_close": None,
        "pivot": None,
        "extension": None,
        "proximity_to_52week_high": None,
        "volume_ratio_50d": None,
        "average_dollar_volume_50d": None,
        "atr_pct_20d": None,
        "realized_volatility_20d": None,
        "normalized_trigger_gap": None,
    }
    if history is None or history.empty:
        _add_blocker(row, "missing_price_history")
        return row
    if _session_date(history) != as_of_session:
        _add_blocker(row, "stale_price_history")
        return row
    if len(history) < 30:
        _add_blocker(row, "insufficient_price_history")
        return row
    if len(history) < 252:
        _add_warning(row, "limited_price_history")
    if not {"High", "Low", "Close", "Volume"}.issubset(history.columns):
        _add_blocker(row, "invalid_price_history")
        return row

    close = _float_series(history, "Close")
    high = _float_series(history, "High")
    low = _float_series(history, "Low")
    volume = _float_series(history, "Volume")
    latest_close = float(close.iloc[-1])
    prior_close = float(close.iloc[-2])
    pivot = float(close.iloc[-253:-1].max()) if len(close) >= 253 else float(close.iloc[:-1].max())
    high_52week = float(high.tail(252).max())
    average_volume = float(volume.iloc[-51:-1].mean())
    price_up = latest_close > prior_close
    has_volume_surge, volume_ratio = _detect_volume_surge(
        float(volume.iloc[-1]), average_volume, settings.S_VOLUME_SURGE_THRESHOLD, price_up=price_up
    )
    near_high, proximity = _detect_breakout(latest_close, high_52week, settings.S_BREAKOUT_PROXIMITY)
    extension = (latest_close / pivot) - 1 if pivot > 0 else None

    row.update(
        {
            "close": latest_close,
            "prior_close": prior_close,
            "pivot": pivot,
            "extension": extension,
            "proximity_to_52week_high": proximity,
            "volume_ratio_50d": volume_ratio,
            "average_dollar_volume_50d": float((close.iloc[-51:-1] * volume.iloc[-51:-1]).mean()),
            "atr_pct_20d": _atr_pct(high, low, close),
            "realized_volatility_20d": _realized_volatility(close),
        }
    )
    gaps = []
    if not near_high:
        _add_blocker(row, "below_52week_proximity")
        gaps.append((settings.S_BREAKOUT_PROXIMITY - proximity) / settings.S_BREAKOUT_PROXIMITY)
    if not has_volume_surge:
        _add_blocker(row, "no_up_day_volume_surge")
        gaps.append(max((settings.S_VOLUME_SURGE_THRESHOLD - volume_ratio) / settings.S_VOLUME_SURGE_THRESHOLD, 0.0))
    if pivot <= 0 or latest_close < pivot:
        _add_blocker(row, "below_pivot")
        gaps.append((pivot - latest_close) / pivot if pivot > 0 else 1.0)
    elif latest_close > pivot * (1 + settings.BUY_ZONE_EXTENSION_PCT):
        _add_blocker(row, "beyond_buy_zone")
        gaps.append((latest_close - pivot * (1 + settings.BUY_ZONE_EXTENSION_PCT)) / (pivot * (1 + settings.BUY_ZONE_EXTENSION_PCT)))
    row["normalized_trigger_gap"] = sum(gaps) if gaps else 0.0
    return row


def _apply_rs_gate(row: dict[str, object], *, market: MarketTrend) -> None:
    if row["blocking_reasons"]:
        return
    score = row["rs_score"]
    if score is None:
        _add_blocker(row, "rs_unavailable")
        return
    score_float = float(score)
    if score_float < settings.MIN_RS_SCORE:
        _add_blocker(row, "rs_below_threshold")
        gap = (settings.MIN_RS_SCORE - score_float) / settings.MIN_RS_SCORE
        existing_gap = row["normalized_trigger_gap"]
        row["normalized_trigger_gap"] = float(existing_gap or 0.0) + gap
    row["technical_eligible"] = not bool(row["blocking_reasons"])
    row["tomorrow_executable"] = bool(row["technical_eligible"]) and market.is_bullish


def _is_current_and_sufficient(history: pd.DataFrame, as_of_session: date) -> bool:
    return not history.empty and len(history) >= 30 and _session_date(history) == as_of_session and "Close" in history.columns


def _weighted_performance(history: pd.DataFrame) -> float | None:
    close = _float_series(history, "Close").dropna()
    weighted = calculate_weighted_performance(close)
    if weighted is not None and math.isfinite(float(weighted)):
        return float(weighted)
    if len(close) < 30 or close.iloc[0] <= 0:
        return None
    fallback = (close.iloc[-1] / close.iloc[0]) - 1
    return float(fallback) if math.isfinite(float(fallback)) else None


def _float_series(history: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(history[column], errors="coerce").astype(float)


def _atr_pct(high: pd.Series, low: pd.Series, close: pd.Series) -> float | None:
    prior = close.shift(1)
    true_range = pd.concat([high - low, (high - prior).abs(), (low - prior).abs()], axis=1).max(axis=1)
    value = true_range.tail(20).mean() / close.iloc[-1] if close.iloc[-1] else None
    return _safe_builtin(value)


def _realized_volatility(close: pd.Series) -> float | None:
    value = close.pct_change().tail(20).std() * math.sqrt(252)
    return _safe_builtin(value)


def _session_date(history: pd.DataFrame) -> date:
    return pd.Timestamp(history.index[-1]).date()


def _add_blocker(row: dict[str, object], reason: str) -> None:
    row["blocking_reasons"] = _append_reason(str(row["blocking_reasons"]), reason)


def _add_warning(row: dict[str, object], warning: str) -> None:
    row["warnings"] = _append_reason(str(row["warnings"]), warning)


def _append_reason(existing: str, reason: str) -> str:
    return f"{existing},{reason}" if existing else reason


def _rank_key(row: dict[str, object]) -> tuple[object, ...]:
    return (
        not bool(row["tomorrow_executable"]),
        not bool(row["technical_eligible"]),
        len(str(row["blocking_reasons"]).split(",")) if row["blocking_reasons"] else 0,
        _rank_number(row["normalized_trigger_gap"], default=math.inf),
        -_rank_number(row["rs_score"], default=-math.inf),
        -_rank_number(row["volume_ratio_50d"], default=-math.inf),
        str(row["symbol"]),
    )


def _rank_number(value: object, *, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else default


def _market_payload(market: MarketTrend) -> dict[str, object]:
    return {
        "symbol": market.symbol,
        "score": market.score,
        "is_bullish": market.is_bullish,
        "latest_close": market.latest_close,
        "indicators": market.indicators,
        "distribution_days": market.distribution_days,
        "follow_through": market.follow_through,
    }


def _safe_builtin(value: object) -> float | int | str | bool | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return value if isinstance(value, str) else None


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return _safe_builtin(value)
