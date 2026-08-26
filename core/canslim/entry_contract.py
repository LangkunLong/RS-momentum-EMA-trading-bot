"""Canonical completed-session CANSLIM entry facts and qualification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from core.pit_diagnosis.patterns import BasePattern, BasePolicy, detect_proper_base


PRIOR_VOLUME_SESSIONS = 50
PIVOT_LOOKBACK_SESSIONS = 252
MIN_VOLUME_RATIO = 1.30
MAX_BUY_ZONE_EXTENSION = 0.05
MIN_CURRENT_GROWTH = 0.25
MIN_ANNUAL_GROWTH = 0.25
MIN_RS_SCORE = 80.0
MIN_COMPOSITE_SCORE = 70.0


@dataclass(frozen=True, slots=True)
class CanslimEntryFacts:
    """Immutable technical facts for the final completed session."""

    event_close: float | None
    prior_close: float | None
    event_volume: float | None
    prior_average_volume_50: float | None
    pivot: float | None
    volume_ratio: float | None
    extension: float | None
    price_advanced: bool
    has_volume_surge: bool
    in_buy_zone: bool
    eligible: bool
    blocking_reasons: tuple[str, ...]

    @property
    def technical_eligible(self) -> bool:
        """Return the completed-session technical setup result."""
        return self.eligible


@dataclass(frozen=True, slots=True)
class CanslimEntryDecision:
    """Immutable full CANSLIM entry decision, excluding market permission."""

    facts: CanslimEntryFacts
    current_growth: float | None
    annual_growth: float | None
    rs_score: float | None
    composite_score: float | None
    eligible: bool
    blocking_reasons: tuple[str, ...]

    @property
    def pivot(self) -> float | None:
        return self.facts.pivot

    @property
    def prior_average_volume_50(self) -> float | None:
        return self.facts.prior_average_volume_50

    @property
    def volume_ratio(self) -> float | None:
        return self.facts.volume_ratio

    @property
    def extension(self) -> float | None:
        return self.facts.extension

    @property
    def technical_eligible(self) -> bool:
        return self.facts.eligible


def build_entry_facts(
    closes: Iterable[object],
    volumes: Iterable[object],
    *,
    history_before_event: pd.DataFrame | None = None,
    event_session: object | None = None,
    require_proper_base: bool = False,
    precomputed_proper_base: BasePattern | None = None,
    proper_base_precomputed: bool = False,
) -> CanslimEntryFacts:
    """Build canonical facts for the final event bar in aligned history.

    Only observations used by the contract are inspected: the event and prior
    close, up to 252 closes before the event, and exactly 50 volumes before the
    event plus the event volume itself.  Strict PIT callers additionally supply
    pre-event OHLC history and the event session so the canonical proper-base
    detector, rather than a rolling-close high, supplies the pivot.  A caller
    that has already evaluated that exact causal prefix can pass its immutable
    result with ``proper_base_precomputed=True`` (including ``None``).
    """
    reasons: list[str] = []
    close_index = _explicit_index(closes)
    volume_index = _explicit_index(volumes)
    close_values = tuple(closes)
    volume_values = tuple(volumes)

    if len(close_values) != len(volume_values):
        reasons.append("close_volume_length_mismatch")
    if (close_index is None) != (volume_index is None) or (
        close_index is not None
        and volume_index is not None
        and not _indexes_equal(close_index, volume_index)
    ):
        reasons.append("close_volume_index_mismatch")

    event_close = _finite_at(close_values, -1)
    prior_close = _finite_at(close_values, -2)
    event_volume = _finite_at(volume_values, -1)
    pivot: float | None = None
    prior_average_volume: float | None = None
    volume_ratio: float | None = None
    extension: float | None = None

    if len(close_values) < 2:
        reasons.append("insufficient_close_history")
    else:
        relevant_closes = close_values[-(PIVOT_LOOKBACK_SESSIONS + 1) :]
        converted_closes = _finite_values(relevant_closes)
        if converted_closes is None:
            reasons.append("non_finite_close_input")
        else:
            event_close = converted_closes[-1]
            prior_close = converted_closes[-2]
            if not require_proper_base:
                pivot = max(converted_closes[:-1])
                if pivot <= 0:
                    reasons.append("non_positive_pivot")

    if require_proper_base:
        if proper_base_precomputed:
            pattern = precomputed_proper_base
        else:
            try:
                pattern = detect_proper_base(
                    history_before_event,
                    event_session=event_session,
                    policy=BasePolicy.canonical_v1(),
                )
            except Exception:
                pattern = None
        if pattern is None:
            reasons.append("proper_base_unavailable")
        else:
            pivot = pattern.pivot
            if pivot <= 0:
                reasons.append("non_positive_pivot")

    if len(volume_values) < PRIOR_VOLUME_SESSIONS + 1:
        reasons.append("insufficient_prior_volume_history")
    else:
        relevant_volumes = volume_values[-(PRIOR_VOLUME_SESSIONS + 1) :]
        converted_volumes = _finite_values(relevant_volumes)
        if converted_volumes is None:
            reasons.append("non_finite_volume_input")
        else:
            prior_average_volume = sum(converted_volumes[:-1]) / PRIOR_VOLUME_SESSIONS
            event_volume = converted_volumes[-1]
            if prior_average_volume <= 0:
                reasons.append("non_positive_prior_average_volume")
            else:
                volume_ratio = event_volume / prior_average_volume

    price_advanced = bool(
        event_close is not None and prior_close is not None and event_close > prior_close
    )
    has_volume_surge = bool(volume_ratio is not None and volume_ratio >= MIN_VOLUME_RATIO)
    in_buy_zone = bool(
        event_close is not None
        and pivot is not None
        and pivot > 0
        and pivot <= event_close <= pivot * (1 + MAX_BUY_ZONE_EXTENSION)
    )

    if event_close is not None and prior_close is not None and not price_advanced:
        reasons.append("close_not_above_prior_close")
    if volume_ratio is not None and not has_volume_surge:
        reasons.append("volume_ratio_below_threshold")
    if event_close is not None and pivot is not None and pivot > 0:
        extension = event_close / pivot - 1
        if event_close < pivot:
            reasons.append("close_below_pivot")
        elif event_close > pivot * (1 + MAX_BUY_ZONE_EXTENSION):
            reasons.append("close_above_buy_zone")

    ordered_reasons = tuple(reasons)
    return CanslimEntryFacts(
        event_close=event_close,
        prior_close=prior_close,
        event_volume=event_volume,
        prior_average_volume_50=prior_average_volume,
        pivot=pivot,
        volume_ratio=volume_ratio,
        extension=extension,
        price_advanced=price_advanced,
        has_volume_surge=has_volume_surge,
        in_buy_zone=in_buy_zone,
        eligible=not ordered_reasons,
        blocking_reasons=ordered_reasons,
    )


def _explicit_index(values: Iterable[object]) -> object | None:
    """Return an index carried by a Series-like input, excluding list.index."""
    index = getattr(values, "index", None)
    return None if index is None or callable(index) else index


def _indexes_equal(left: object, right: object) -> bool:
    """Compare index-bearing inputs without coercing or positionally aligning them."""
    equals = getattr(left, "equals", None)
    if callable(equals):
        try:
            return bool(equals(right))
        except (TypeError, ValueError):
            return False
    try:
        return tuple(left) == tuple(right)  # type: ignore[arg-type]
    except TypeError:
        return False


def evaluate_entry_contract(
    facts: CanslimEntryFacts,
    *,
    current_growth: object,
    annual_growth: object,
    rs_score: object,
    composite_score: object,
) -> CanslimEntryDecision:
    """Apply the fixed C/A/RS/composite gates to canonical technical facts."""
    reasons = list(facts.blocking_reasons)
    current = _finite_number(current_growth)
    annual = _finite_number(annual_growth)
    rs = _finite_number(rs_score)
    composite = _finite_number(composite_score)

    _append_threshold_reason(
        reasons,
        current,
        threshold=MIN_CURRENT_GROWTH,
        unavailable="current_growth_unavailable",
        below="current_growth_below_threshold",
    )
    _append_threshold_reason(
        reasons,
        annual,
        threshold=MIN_ANNUAL_GROWTH,
        unavailable="annual_growth_unavailable",
        below="annual_growth_below_threshold",
    )
    _append_threshold_reason(
        reasons,
        rs,
        threshold=MIN_RS_SCORE,
        unavailable="rs_score_unavailable",
        below="rs_score_below_threshold",
    )
    _append_threshold_reason(
        reasons,
        composite,
        threshold=MIN_COMPOSITE_SCORE,
        unavailable="composite_score_unavailable",
        below="composite_score_below_threshold",
    )

    ordered_reasons = tuple(reasons)
    return CanslimEntryDecision(
        facts=facts,
        current_growth=current,
        annual_growth=annual,
        rs_score=rs,
        composite_score=composite,
        eligible=not ordered_reasons,
        blocking_reasons=ordered_reasons,
    )


def _finite_at(values: tuple[object, ...], index: int) -> float | None:
    try:
        return _finite_number(values[index])
    except IndexError:
        return None


def _finite_values(values: Iterable[object]) -> tuple[float, ...] | None:
    converted = tuple(_finite_number(value) for value in values)
    if any(value is None for value in converted):
        return None
    return tuple(value for value in converted if value is not None)


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _append_threshold_reason(
    reasons: list[str],
    value: float | None,
    *,
    threshold: float,
    unavailable: str,
    below: str,
) -> None:
    if value is None:
        reasons.append(unavailable)
    elif value < threshold:
        reasons.append(below)
