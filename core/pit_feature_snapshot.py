"""Causal, symbol-neutral O'Neil feature snapshots for policy interface V3.

Every calculation uses an authenticated schema-V3 bundle and a price prefix
that contains no row after the completed ``session``.  Price formulas are:

* ATR20 fraction: mean of the last 20 true ranges divided by session close;
* breakout gap: ``(session open - prior close) / prior close``;
* ADV50: mean of ``close * volume`` over the 50 sessions before the event;
* 52-week-high distance: ``session close / max(high[-252:]) - 1``;
* volume ratio: session volume / mean volume over the prior 50 sessions.

Quarterly earnings/sales acceleration is the newest year-over-year growth rate
minus the immediately preceding quarter's year-over-year growth rate.  A
fundamental's age is measured from the newest visible quarterly public date,
never from its period end.  Missing observations become ``None``; observed
booleans, NaN, or infinities fail closed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date

import numpy as np
import pandas as pd

from core.canslim.fiscal_periods import match_fiscal_year_over_year_periods
from core.canslim.l_leader_laggard import (
    calculate_group_rs,
    finite_rs_snapshot,
)
from core.industry_group import load_pit_industry_assignments_as_of
from core.pit_data import PITDataBundle, PriceIdentityTransitionContract
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR
from core.pit_universe_v3 import UNIVERSE_IDS


_REFERENCE_SYMBOLS = frozenset({"SPY", "QQQ", "IWM"})
_EPS_LABELS = ("Diluted EPS", "Basic EPS", "Net Income")


@dataclass(frozen=True, slots=True)
class EntryFeaturesV3:
    """Causal O'Neil features available while evaluating a new entry."""

    affiliations: tuple[str, ...]
    industry_group_rs: float | None
    sector_rs: float | None
    earnings_growth_acceleration: float | None
    sales_growth_acceleration: float | None
    fundamental_age_days: int | None
    atr_20_fraction: float | None
    breakout_gap_fraction: float | None
    average_dollar_volume_50: float | None
    distance_from_52_week_high_fraction: float | None

    def __post_init__(self) -> None:
        _validate_affiliations(self.affiliations)
        _validate_feature_numbers(self)


@dataclass(frozen=True, slots=True)
class HoldingFeaturesV3:
    """Causal O'Neil features refreshed for an existing position."""

    current_rs_score: float | None
    industry_group_rs: float | None
    atr_20_fraction: float | None
    volume_ratio: float | None

    def __post_init__(self) -> None:
        _validate_feature_numbers(self)


def build_entry_features_v3(
    *,
    bundle: PITDataBundle,
    symbol: str,
    session: date,
    price_history: pd.DataFrame,
    rs_snapshot: Mapping[str, float],
    allow_schema_v2_development: bool = False,
) -> EntryFeaturesV3:
    """Build entry features from facts observable by ``session`` only."""
    symbol, active_symbols, validated_rs = _validated_context(
        bundle, symbol, session, rs_snapshot, require_active=True,
        allow_schema_v2_development=allow_schema_v2_development,
    )
    assignments = load_pit_industry_assignments_as_of(
        bundle,
        session=session,
        symbols=active_symbols,
        allow_schema_v2_development=allow_schema_v2_development,
    )
    groups = {ticker: assignment.group_id for ticker, assignment in assignments.items()}
    target_group = groups.get(symbol)
    group_rs = (
        calculate_group_rs(
            target_group,
            active_symbols=active_symbols,
            symbol_groups=groups,
            rs_snapshot=validated_rs,
        )
        if target_group is not None
        else None
    )

    history = _validated_price_history(price_history, session)
    fundamentals = bundle.fundamentals_as_of(
        symbol,
        pd.Timestamp(session),
        include_provenance=True,
    )
    quarterly = fundamentals.get("quarterly_income")
    if not isinstance(quarterly, pd.DataFrame):
        raise ValueError("PIT fundamentals quarterly_income must be a DataFrame")

    affiliations = tuple(sorted(bundle.affiliations_at(session).get(symbol, ())))
    return EntryFeaturesV3(
        affiliations=affiliations,
        industry_group_rs=group_rs,
        # Schema V3 seals GICS-like sub-industry IDs but no dated sector
        # taxonomy.  Inferring a sector from current profiles would be a leak.
        sector_rs=None,
        earnings_growth_acceleration=_earnings_acceleration(quarterly),
        sales_growth_acceleration=_growth_acceleration_for_label(
            quarterly, "Total Revenue"
        ),
        fundamental_age_days=_fundamental_age_days(quarterly, session),
        atr_20_fraction=_atr_20_fraction(history),
        breakout_gap_fraction=_breakout_gap_fraction(history),
        average_dollar_volume_50=_average_dollar_volume_50(history),
        distance_from_52_week_high_fraction=_distance_from_52_week_high(history),
    )


def build_holding_features_v3(
    *,
    bundle: PITDataBundle,
    symbol: str,
    session: date,
    price_history: pd.DataFrame,
    rs_snapshot: Mapping[str, float],
    allow_schema_v2_development: bool = False,
    identity_transition_contract: PriceIdentityTransitionContract | None = None,
) -> HoldingFeaturesV3:
    """Build refreshed holding features from facts observable by ``session``."""
    symbol, active_symbols, validated_rs = _validated_context(
        bundle, symbol, session, rs_snapshot, require_active=False,
        allow_schema_v2_development=allow_schema_v2_development,
        identity_transition_contract=identity_transition_contract,
    )
    assignment_symbols = active_symbols.union((symbol,))
    assignments = load_pit_industry_assignments_as_of(
        bundle,
        session=session,
        symbols=assignment_symbols,
        allow_schema_v2_development=allow_schema_v2_development,
    )
    groups = {ticker: assignment.group_id for ticker, assignment in assignments.items()}
    target_group = groups.get(symbol)
    group_rs = (
        calculate_group_rs(
            target_group,
            active_symbols=active_symbols,
            symbol_groups=groups,
            rs_snapshot=validated_rs,
        )
        if target_group is not None
        else None
    )
    history = _validated_price_history(price_history, session)
    return HoldingFeaturesV3(
        current_rs_score=validated_rs.get(symbol),
        industry_group_rs=group_rs,
        atr_20_fraction=_atr_20_fraction(history),
        volume_ratio=_volume_ratio_50(history),
    )


def _validated_context(
    bundle: PITDataBundle,
    symbol: str,
    session: date,
    rs_snapshot: Mapping[str, float],
    *,
    require_active: bool,
    allow_schema_v2_development: bool = False,
    identity_transition_contract: PriceIdentityTransitionContract | None = None,
) -> tuple[str, frozenset[str], Mapping[str, float]]:
    if type(session) is not date:
        raise ValueError("feature session must be a date")
    if type(allow_schema_v2_development) is not bool:
        raise ValueError("feature development flag must be a bool")
    schema_version = bundle.metadata.get("schema_version")
    if schema_version != "3" and not (
        allow_schema_v2_development and schema_version == "2"
    ):
        raise ValueError("V3 features require a schema-V3 PIT bundle")
    if session > bundle.data_cutoff.date():
        raise ValueError("feature session exceeds the authenticated bundle cutoff")
    if (
        not isinstance(symbol, str)
        or not symbol
        or symbol.strip() != symbol
        or symbol.upper() != symbol
        or symbol in _REFERENCE_SYMBOLS
    ):
        raise ValueError("feature symbol must be a canonical tradable symbol")
    active_symbols = frozenset(bundle.members_at(session))
    if active_symbols.intersection(_REFERENCE_SYMBOLS):
        raise ValueError("market reference symbols must not enter the active PIT union")
    if require_active and symbol not in active_symbols:
        raise ValueError("entry feature symbol is not active in the PIT union")
    if not require_active:
        if schema_version == "2":
            if (
                type(identity_transition_contract) is not PriceIdentityTransitionContract
                or identity_transition_contract.prices_provenance_sha256
                != bundle.metadata["prices_provenance_sha256"]
                or identity_transition_contract.request_contracts_sha256
                != bundle.metadata["price_identity_request_contracts_sha256"]
            ):
                raise ValueError("development holding features require the authenticated identity transition contract")
            resolved_symbol = identity_transition_contract.resolve_open_holding(symbol, session)
        else:
            lineage_id = bundle.security_lineage_id(symbol)
            resolved_symbol = bundle.membership_v3.ticker_for_lineage_at(
                lineage_id, session
            )
        if resolved_symbol != symbol:
            raise ValueError(
                "holding feature symbol is not the active authenticated price identity"
            )
    # Legacy membership can include a ticker before its admitted price history
    # is available (for example BBWI in early 2021). Preserve the causal engine's
    # omissions; holding RS stays None and no industry mean is computed in V2.
    validated_rs = finite_rs_snapshot(
        rs_snapshot, required_symbols=() if schema_version == "2" else active_symbols
    )
    return symbol, active_symbols, validated_rs


def _validated_price_history(price_history: pd.DataFrame, session: date) -> pd.DataFrame:
    if not isinstance(price_history, pd.DataFrame):
        raise ValueError("price_history must be a DataFrame")
    if price_history.empty:
        return price_history.copy()
    if not isinstance(price_history.index, pd.DatetimeIndex):
        raise ValueError("price_history must use a DatetimeIndex")
    if price_history.index.tz is not None:
        raise ValueError("price_history index must be timezone-naive completed sessions")
    if not price_history.index.is_monotonic_increasing or not price_history.index.is_unique:
        raise ValueError("price_history sessions must be unique and increasing")
    if any(timestamp != timestamp.normalize() for timestamp in price_history.index):
        raise ValueError("price_history must contain normalized daily sessions")
    if any(timestamp.date() > session for timestamp in price_history.index):
        raise ValueError("price_history contains a row after the feature session")
    if price_history.index[-1].date() != session:
        raise ValueError("price_history does not end on the completed feature session")

    history = price_history.copy(deep=False)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in history.columns:
            continue
        for value in history[column].array:
            number = _optional_number(value, field=f"price_history {column}")
            if number is None:
                continue
            if column in {"Open", "High", "Low", "Close"} and number <= 0:
                raise ValueError("price_history OHLC values must be positive")
            if column == "Volume" and number < 0:
                raise ValueError("price_history volume must be nonnegative")
    if {"Open", "High", "Low", "Close"}.issubset(history.columns):
        for row in history[["Open", "High", "Low", "Close"]].itertuples(
            index=False, name=None
        ):
            open_price, high, low, close = (
                _optional_number(value, field="price_history OHLC") for value in row
            )
            if high is not None and low is not None and high < low:
                raise ValueError("price_history high is below low")
            if high is not None:
                observed = tuple(
                    value for value in (open_price, close) if value is not None
                )
                if observed and high < max(observed):
                    raise ValueError("price_history high does not contain open/close")
            if low is not None:
                observed = tuple(
                    value for value in (open_price, close) if value is not None
                )
                if observed and low > min(observed):
                    raise ValueError("price_history low does not contain open/close")
    return history


def _history_is_current(history: pd.DataFrame) -> bool:
    return not history.empty


def _atr_20_fraction(history: pd.DataFrame) -> float | None:
    if not _history_is_current(history) or len(history) < 21:
        return None
    if not {"High", "Low", "Close"}.issubset(history.columns):
        return None
    high = _finite_window(history, "High", start=-20)
    low = _finite_window(history, "Low", start=-20)
    close = _finite_window(history, "Close", start=-21)
    if high is None or low is None or close is None:
        return None
    true_range = np.maximum.reduce(
        (
            high - low,
            np.abs(high - close[:-1]),
            np.abs(low - close[:-1]),
        )
    )
    value = float(true_range.mean()) / close[-1]
    return _finite_result(value, field="ATR20 fraction")


def _breakout_gap_fraction(history: pd.DataFrame) -> float | None:
    if not _history_is_current(history) or len(history) < 2:
        return None
    if not {"Open", "Close"}.issubset(history.columns):
        return None
    event_open = _finite_scalar(history["Open"].iloc[-1], field="event open")
    prior_close = _finite_scalar(history["Close"].iloc[-2], field="prior close")
    if event_open is None or prior_close is None:
        return None
    value = (event_open - prior_close) / prior_close
    return _finite_result(value, field="breakout gap fraction")


def _average_dollar_volume_50(history: pd.DataFrame) -> float | None:
    if not _history_is_current(history) or len(history) < 51:
        return None
    if not {"Close", "Volume"}.issubset(history.columns):
        return None
    close = _finite_window(history.iloc[:-1], "Close", start=-50)
    volume = _finite_window(history.iloc[:-1], "Volume", start=-50)
    if close is None or volume is None:
        return None
    value = (close * volume).mean()
    return _finite_result(value, field="average dollar volume 50")


def _distance_from_52_week_high(history: pd.DataFrame) -> float | None:
    if not _history_is_current(history) or len(history) < 252:
        return None
    if not {"High", "Close"}.issubset(history.columns):
        return None
    highs = _finite_window(history, "High", start=-252)
    close = _finite_scalar(history["Close"].iloc[-1], field="event close")
    if highs is None or close is None:
        return None
    high = float(highs.max())
    value = close / high - 1.0
    return _finite_result(value, field="distance from 52-week high")


def _volume_ratio_50(history: pd.DataFrame) -> float | None:
    if not _history_is_current(history) or len(history) < 51 or "Volume" not in history.columns:
        return None
    prior_volume = _finite_window(history.iloc[:-1], "Volume", start=-50)
    event_volume = _finite_scalar(history["Volume"].iloc[-1], field="event volume")
    if prior_volume is None or event_volume is None:
        return None
    prior_average = prior_volume.mean()
    if prior_average <= 0:
        return None
    value = event_volume / prior_average
    return _finite_result(value, field="volume ratio 50")


def _finite_window(
    history: pd.DataFrame,
    column: str,
    *,
    start: int,
) -> np.ndarray | None:
    values: list[float] = []
    for raw_value in history[column].iloc[start:].array:
        value = _optional_number(raw_value, field=f"price_history {column}")
        if value is None:
            return None
        values.append(value)
    return np.asarray(values, dtype=float)


def _finite_scalar(value: object, *, field: str) -> float | None:
    return _optional_number(value, field=field)


def _earnings_acceleration(quarterly: pd.DataFrame) -> float | None:
    for label in _EPS_LABELS:
        matching = [index for index in quarterly.index if str(index).casefold() == label.casefold()]
        if not matching:
            continue
        series = quarterly.loc[matching[0]]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[0]
        if not isinstance(series, pd.Series):
            raise ValueError("PIT quarterly earnings row is invalid")
        _validate_optional_series(series, field=label)
        comparisons = match_fiscal_year_over_year_periods(series)
        if comparisons and comparisons[0].matched:
            return _growth_acceleration(series)
    return None


def _growth_acceleration_for_label(
    quarterly: pd.DataFrame, label: str
) -> float | None:
    matching = [index for index in quarterly.index if str(index).casefold() == label.casefold()]
    if not matching:
        return None
    series = quarterly.loc[matching[0]]
    if isinstance(series, pd.DataFrame):
        series = series.iloc[0]
    if not isinstance(series, pd.Series):
        raise ValueError(f"PIT quarterly {label} row is invalid")
    _validate_optional_series(series, field=label)
    return _growth_acceleration(series)


def _growth_acceleration(series: pd.Series) -> float | None:
    matches = match_fiscal_year_over_year_periods(series)
    if len(matches) < 2 or not matches[0].matched or not matches[1].matched:
        return None
    newest = _growth(matches[0].current_value, matches[0].prior_value)
    previous = _growth(matches[1].current_value, matches[1].prior_value)
    if newest is None or previous is None:
        return None
    return _finite_result(newest - previous, field="growth acceleration")


def _growth(current: object, prior: object) -> float | None:
    current_number = _optional_number(current, field="growth current value")
    prior_number = _optional_number(prior, field="growth prior value")
    if current_number is None or prior_number is None or prior_number <= 0:
        return None
    return _finite_result(
        (current_number - prior_number) / abs(prior_number),
        field="year-over-year growth",
    )


def _fundamental_age_days(quarterly: pd.DataFrame, session: date) -> int | None:
    raw_public_dates = quarterly.attrs.get(PIT_PUBLIC_DATES_ATTR)
    if raw_public_dates is None:
        if quarterly.empty:
            return None
        raise ValueError("PIT quarterly fundamentals are missing public-date provenance")
    if not isinstance(raw_public_dates, Mapping):
        raise ValueError("PIT quarterly public-date provenance is invalid")
    public_dates: list[date] = []
    for raw_period, raw_public_date in raw_public_dates.items():
        if not isinstance(raw_period, str) or not isinstance(raw_public_date, str):
            raise ValueError("PIT quarterly public-date provenance is invalid")
        try:
            period = date.fromisoformat(raw_period)
            public_date = date.fromisoformat(raw_public_date)
        except ValueError as exc:
            raise ValueError("PIT quarterly public-date provenance is invalid") from exc
        if public_date <= period or public_date > session:
            raise ValueError("PIT quarterly public date is outside the causal session")
        public_dates.append(public_date)
    if not public_dates:
        return None
    return (session - max(public_dates)).days


def _validate_optional_series(series: pd.Series, *, field: str) -> None:
    for value in series.array:
        _optional_number(value, field=field)


def _optional_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{field} must be a finite number or None, not bool")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number or None") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _finite_result(value: object, *, field: str) -> float:
    number = _optional_number(value, field=field)
    if number is None:
        raise ValueError(f"{field} calculation unexpectedly produced missing data")
    return number


def _validate_affiliations(affiliations: tuple[str, ...]) -> None:
    if (
        type(affiliations) is not tuple
        or affiliations != tuple(sorted(affiliations))
        or len(set(affiliations)) != len(affiliations)
        or any(not isinstance(value, str) or not value for value in affiliations)
        or not set(affiliations).issubset(UNIVERSE_IDS)
    ):
        raise ValueError("affiliations must be a canonical sorted tuple")


def _validate_feature_numbers(instance: object) -> None:
    for field in fields(instance):
        if field.name == "affiliations":
            continue
        value = getattr(instance, field.name)
        if field.name == "fundamental_age_days":
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("fundamental_age_days must be a nonnegative integer or None")
            continue
        _optional_number(value, field=field.name)
