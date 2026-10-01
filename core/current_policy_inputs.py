"""Offline current-data adapter for accepted policy feature/context builders.

The module accepts a recorded, hash-verified PIT bundle and already-recorded
market frames.  It does not fetch market data, consult scanner rankings, or
change the historical feature and market-context calculators.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime
from numbers import Real
from types import MappingProxyType
from typing import Literal

import numpy as np
import pandas as pd

from core.pit_data import PITDataBundle
from core.pit_feature_snapshot import (
    FINANCIAL_FEATURE_CALCULATOR_ID,
    EntryFeaturesV3,
    build_entry_features_v3,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR
from core.strategy_policy.contracts import MarketContextV1
from core.strategy_policy.market_context import build_market_context


_DETAILED_STATES = frozenset(
    {
        "not_yet_public",
        "absent",
        "unsupported_scope",
        "insufficient_history",
        "stale_by_declared_rule",
        "invalid",
        "observed",
    }
)
_POLICY_STATES = {
    "not_yet_public": "not_yet_public",
    "absent": "unavailable",
    "unsupported_scope": "unavailable",
    "insufficient_history": "unavailable",
    "stale_by_declared_rule": "stale",
    "invalid": "unavailable",
    "observed": "present",
}
_REFERENCE_SYMBOLS = frozenset({"SPY", "QQQ", "IWM"})
FEATURE_CONTRACT_ID = "historical-feature-specification-v1+strategy-policy-contract-v1"
FEATURE_CALCULATOR_IDENTITY = (
    "core.pit_feature_snapshot:build_entry_features_v3"
    f"|{FINANCIAL_FEATURE_CALCULATOR_ID}"
    "|core.strategy_policy.market_context:MarketContextV1"
)


def _require_exchange_local_aware(value: object, *, name: str) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _exchange_sessions(value: Sequence[date]) -> tuple[date, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("exchange_sessions must be a non-empty ordered sequence")
    sessions = tuple(value)
    if any(type(item) is not date for item in sessions):
        raise ValueError("exchange sessions must be dates")
    if tuple(sorted(set(sessions))) != sessions:
        raise ValueError("exchange sessions must be unique and increasing")
    return sessions


def _availability_record(
    *,
    period_end: date,
    source_public_date: date,
    source_public_at: datetime | None,
    available_from_session: date,
    date_basis: Literal["derived_from_source_public_date", "already_normalized"],
) -> FundamentalAvailabilityV1:
    return FundamentalAvailabilityV1(
        period_end=period_end,
        source_public_date=source_public_date,
        source_public_at=source_public_at,
        available_from_session=available_from_session,
        date_basis=date_basis,
    )


def _expected_available_session(
    source_public_date: date,
    sessions: tuple[date, ...],
) -> date:
    try:
        return next(session for session in sessions if session > source_public_date)
    except StopIteration as exc:
        raise ValueError("exchange calendar has no eligible session after source-public date") from exc


@dataclass(frozen=True, slots=True)
class FundamentalAvailabilityV1:
    """Keep raw publication timing separate from normalized feature availability."""

    period_end: date
    source_public_date: date
    source_public_at: datetime | None
    available_from_session: date
    date_basis: Literal["derived_from_source_public_date", "already_normalized"]

    def __post_init__(self) -> None:
        if any(
            type(value) is not date
            for value in (self.period_end, self.source_public_date, self.available_from_session)
        ):
            raise ValueError("fundamental availability dates must be dates")
        if self.source_public_date <= self.period_end:
            raise ValueError("source-public date must be after the fiscal period end")
        if self.available_from_session <= self.source_public_date:
            raise ValueError("available-from session must be strictly after source-public date")
        if self.source_public_at is not None:
            _require_exchange_local_aware(self.source_public_at, name="source_public_at")
            if self.source_public_at.date() != self.source_public_date:
                raise ValueError("source_public_at must use the source-public local date")
        if self.date_basis not in {
            "derived_from_source_public_date",
            "already_normalized",
        }:
            raise ValueError("fundamental availability date basis is invalid")


def derive_available_from_source_date(
    *,
    period_end: date,
    source_public_date: date,
    source_public_at: datetime | None,
    exchange_sessions: Sequence[date],
) -> FundamentalAvailabilityV1:
    """Derive the first exchange session strictly after a raw public date.

    An intraday release remains unavailable on its public date under #66's
    completed-session contract.  The caller supplies the explicit recorded
    exchange calendar; this helper makes no provider or calendar request.
    """

    sessions = _exchange_sessions(exchange_sessions)
    if type(source_public_date) is not date or type(period_end) is not date:
        raise ValueError("period_end and source_public_date must be dates")
    available = _expected_available_session(source_public_date, sessions)
    if available not in sessions:
        raise ValueError("derived available-from date is not an exchange session")
    return _availability_record(
        period_end=period_end,
        source_public_date=source_public_date,
        source_public_at=source_public_at,
        available_from_session=available,
        date_basis="derived_from_source_public_date",
    )


def validate_normalized_availability(
    *,
    period_end: date,
    source_public_date: date,
    source_public_at: datetime | None,
    available_from_session: date,
    exchange_sessions: Sequence[date],
) -> FundamentalAvailabilityV1:
    """Validate and preserve an already-normalized session without shifting it."""

    sessions = _exchange_sessions(exchange_sessions)
    if (
        type(period_end) is not date
        or type(source_public_date) is not date
        or type(available_from_session) is not date
    ):
        raise ValueError("fundamental availability fields must use dates")
    expected = _expected_available_session(source_public_date, sessions)
    if available_from_session != expected:
        raise ValueError(
            "normalized available-from session disagrees with the source-public-date rule"
        )
    if available_from_session not in sessions:
        raise ValueError("normalized available-from date is not an exchange session")
    return _availability_record(
        period_end=period_end,
        source_public_date=source_public_date,
        source_public_at=source_public_at,
        available_from_session=available_from_session,
        date_basis="already_normalized",
    )


@dataclass(frozen=True, slots=True)
class CurrentDecisionClockV1:
    """Completed feature cutoff and later valuation observation as separate times."""

    completed_session: date
    as_of_cutoff: datetime
    next_eligible_session: date
    valuation_time: datetime

    def __post_init__(self) -> None:
        if type(self.completed_session) is not date or type(self.next_eligible_session) is not date:
            raise ValueError("decision clock sessions must be dates")
        _require_exchange_local_aware(self.as_of_cutoff, name="as_of_cutoff")
        _require_exchange_local_aware(self.valuation_time, name="valuation_time")
        if self.as_of_cutoff.date() != self.completed_session:
            raise ValueError("as-of cutoff must be on the completed feature session")
        if self.next_eligible_session <= self.completed_session:
            raise ValueError("next eligible session must follow the completed feature session")
        if self.valuation_time < self.as_of_cutoff:
            raise ValueError("valuation time cannot precede the completed-session cutoff")


@dataclass(frozen=True, slots=True)
class FeatureMissingnessV1:
    """Detailed #66 missingness and its linked concise #80 boundary state."""

    state: str
    reason: str

    def __post_init__(self) -> None:
        if self.state not in _DETAILED_STATES:
            raise ValueError("feature missingness state is invalid")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("feature missingness reason is required")

    @property
    def policy_boundary_state(self) -> str:
        return _POLICY_STATES[self.state]


@dataclass(frozen=True, slots=True)
class UnavailableUniverseMemberV1:
    """Retain an active member that cannot enter feature evaluation due to input gaps."""

    state: str
    reason: str
    source_identity: str

    def __post_init__(self) -> None:
        if self.state not in _DETAILED_STATES or self.state == "observed":
            raise ValueError("unavailable universe member state is invalid")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError("unavailable universe member reason is required")
        if type(self.source_identity) is not str or not self.source_identity.strip():
            raise ValueError("unavailable universe member source identity is required")

    @property
    def policy_boundary_state(self) -> str:
        return _POLICY_STATES[self.state]


@dataclass(frozen=True, slots=True)
class CurrentFeatureContextSnapshotV1:
    decision_clock: CurrentDecisionClockV1
    candidate_symbols: tuple[str, ...]
    entry_features: Mapping[str, EntryFeaturesV3]
    market_context: MarketContextV1
    missingness: Mapping[str, Mapping[str, FeatureMissingnessV1]]
    fundamental_availability: Mapping[str, tuple[FundamentalAvailabilityV1, ...]]
    unavailable_members: Mapping[str, UnavailableUniverseMemberV1]
    source_revision: str
    data_bundle_sha256: str
    feature_contract_id: str
    feature_calculator_identity: str
    universe_ids: tuple[str, ...]
    development_only: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entry_features",
            MappingProxyType(dict(self.entry_features)),
        )
        object.__setattr__(
            self,
            "missingness",
            MappingProxyType(
                {
                    symbol: MappingProxyType(dict(records))
                    for symbol, records in self.missingness.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "fundamental_availability",
            MappingProxyType(
                {
                    symbol: tuple(records)
                    for symbol, records in self.fundamental_availability.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "unavailable_members",
            MappingProxyType(dict(self.unavailable_members)),
        )


def _canonical_candidate_symbols(
    raw_symbols: Iterable[str],
    active_symbols: frozenset[str],
) -> tuple[str, ...]:
    if isinstance(raw_symbols, (str, bytes)):
        raise ValueError("candidate_symbols must be a sequence of symbols")
    symbols = tuple(raw_symbols)
    if not symbols:
        raise ValueError("candidate_symbols must not be empty")
    for symbol in symbols:
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol.strip() != symbol
            or symbol.upper() != symbol
            or symbol in _REFERENCE_SYMBOLS
        ):
            raise ValueError("candidate symbol is not canonical")
        if symbol not in active_symbols:
            raise ValueError(f"candidate is not active in the as-of universe: {symbol}")
    if len(symbols) != len(set(symbols)):
        raise ValueError("candidate_symbols must be unique")
    return symbols


def _unavailable_universe_members(
    raw: Mapping[str, object],
    *,
    candidates: tuple[str, ...],
    active_symbols: frozenset[str],
    market_closes: pd.DataFrame,
    rs_snapshot: Mapping[str, float],
    price_history_by_symbol: Mapping[str, pd.DataFrame],
    session: date,
) -> Mapping[str, UnavailableUniverseMemberV1]:
    if not isinstance(raw, Mapping):
        raise ValueError("unavailable_members must be a mapping")
    expected = active_symbols.difference(candidates)
    actual = set(raw)
    if actual != expected:
        missing = sorted(map(str, expected.difference(actual)))
        unexpected = sorted(map(str, actual.difference(expected)))
        raise ValueError(
            "every omitted active member requires an explicit unavailable-input disposition; "
            f"missing={missing[:5]}, unexpected={unexpected[:5]}"
        )
    result: dict[str, UnavailableUniverseMemberV1] = {}
    for symbol in sorted(expected):
        has_current_close = False
        if symbol in market_closes.columns:
            raw_close = market_closes[symbol].iloc[-1]
            if pd.notna(raw_close) and not isinstance(raw_close, (bool,)):
                try:
                    close = float(raw_close)
                    has_current_close = math.isfinite(close) and close > 0
                except (TypeError, ValueError, OverflowError):
                    has_current_close = False
        history = price_history_by_symbol.get(symbol)
        if (
            isinstance(history, pd.DataFrame)
            and not history.empty
            and isinstance(history.index, pd.DatetimeIndex)
            and history.index[-1].date() == session
        ):
            has_current_close = True
        raw_rs = rs_snapshot.get(symbol)
        has_rs = (
            type(raw_rs) is not bool
            and isinstance(raw_rs, (int, float))
            and math.isfinite(float(raw_rs))
        )
        if has_current_close and has_rs:
            raise ValueError(
                f"active member {symbol} has current price and RS inputs and cannot be omitted"
            )
        raw_record = raw[symbol]
        if isinstance(raw_record, UnavailableUniverseMemberV1):
            record = raw_record
        elif isinstance(raw_record, Mapping):
            if set(raw_record) != {"state", "reason", "source_identity"}:
                raise ValueError(f"unavailable-member record shape is invalid for {symbol}")
            record = UnavailableUniverseMemberV1(
                state=raw_record["state"],  # type: ignore[arg-type]
                reason=raw_record["reason"],  # type: ignore[arg-type]
                source_identity=raw_record["source_identity"],  # type: ignore[arg-type]
            )
        else:
            raise ValueError(f"unavailable-member record is invalid for {symbol}")
        result[symbol] = record
    return MappingProxyType(result)


def _validated_market_closes(
    closes: pd.DataFrame,
    *,
    session: date,
) -> pd.DataFrame:
    if not isinstance(closes, pd.DataFrame) or closes.empty:
        raise ValueError("market_closes must be a non-empty DataFrame")
    if not isinstance(closes.index, pd.DatetimeIndex) or closes.index.tz is not None:
        raise ValueError("market_closes must use a timezone-naive DatetimeIndex")
    if not closes.index.is_monotonic_increasing or not closes.index.is_unique:
        raise ValueError("market_closes sessions must be unique and increasing")
    if not closes.columns.is_unique:
        raise ValueError("market_closes symbols must be unique")
    for symbol in closes.columns:
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol.strip() != symbol
            or symbol.upper() != symbol
        ):
            raise ValueError("market_closes symbols must be canonical uppercase text")
        for value in closes[symbol].array:
            if pd.isna(value):
                continue
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
                raise ValueError(f"market_closes values must be numeric for {symbol}")
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"market_closes values must be finite and positive for {symbol}")
    if any(stamp != stamp.normalize() for stamp in closes.index):
        raise ValueError("market_closes must use normalized daily sessions")
    if any(stamp.date() > session for stamp in closes.index):
        raise ValueError("market_closes contains a row after the feature session")
    if closes.index[-1].date() != session:
        raise ValueError("market_closes does not end on the completed feature session")
    missing_refs = sorted(_REFERENCE_SYMBOLS.difference(closes.columns))
    if missing_refs:
        raise ValueError(f"market_closes is missing reference columns: {missing_refs}")
    return closes


def _coerce_missingness(
    raw: Mapping[str, Mapping[str, object]],
    *,
    symbol: str,
    features: EntryFeaturesV3,
) -> Mapping[str, FeatureMissingnessV1]:
    if not isinstance(raw, Mapping):
        raise ValueError("missingness must be a mapping")
    raw_records = raw.get(symbol, {})
    if not isinstance(raw_records, Mapping):
        raise ValueError("symbol missingness records must be a mapping")
    known_fields = {field.name for field in fields(EntryFeaturesV3)}
    unknown_fields = set(raw_records).difference(known_fields)
    if unknown_fields:
        raise ValueError(f"missingness contains unknown feature fields: {sorted(unknown_fields)}")
    normalized: dict[str, FeatureMissingnessV1] = {}
    for name, raw_record in raw_records.items():
        if isinstance(raw_record, FeatureMissingnessV1):
            record = raw_record
        elif isinstance(raw_record, Mapping):
            if set(raw_record) != {"state", "reason"}:
                raise ValueError(f"missingness record shape is invalid for {name}")
            record = FeatureMissingnessV1(
                state=raw_record["state"],  # type: ignore[arg-type]
                reason=raw_record["reason"],  # type: ignore[arg-type]
            )
        else:
            raise ValueError(f"missingness record is invalid for {name}")
        if getattr(features, name) is None and record.state == "observed":
            raise ValueError(f"unavailable feature {name} cannot be marked observed")
        if getattr(features, name) is not None and record.state != "observed":
            raise ValueError(f"present feature {name} cannot carry an unavailable state")
        normalized[name] = record
    for field in fields(EntryFeaturesV3):
        if getattr(features, field.name) is None and field.name not in normalized:
            raise ValueError(f"missingness reason is required for unavailable feature {field.name}")
    return MappingProxyType(normalized)


def _availability_records(
    raw: Mapping[str, Sequence[FundamentalAvailabilityV1]],
    *,
    candidates: tuple[str, ...],
    bundle: PITDataBundle,
    session: date,
) -> Mapping[str, tuple[FundamentalAvailabilityV1, ...]]:
    if not isinstance(raw, Mapping):
        raise ValueError("fundamental_availability must be a mapping")
    unknown = set(raw).difference(candidates)
    if unknown:
        raise ValueError(f"fundamental availability contains non-candidates: {sorted(unknown)}")
    result: dict[str, tuple[FundamentalAvailabilityV1, ...]] = {}
    statement_keys = {"quarterly_income": "quarterly"}
    for symbol in candidates:
        records = tuple(raw.get(symbol, ()))
        if any(type(record) is not FundamentalAvailabilityV1 for record in records):
            raise ValueError(f"fundamental availability records are invalid for {symbol}")
        if len(
            {
                (record.period_end, record.available_from_session)
                for record in records
            }
        ) != len(records):
            raise ValueError(f"fundamental availability records are duplicated for {symbol}")
        # The source date identity is independent of whether the fact is
        # visible in this completed-session snapshot.  Visible bundle dates
        # are checked below; future records remain provenance only.
        visible = bundle.fundamentals_provider(
            symbol,
            pd.Timestamp(session),
            include_provenance=True,
        )
        visible_dates: dict[tuple[str, date], str] = {}
        for output_key, statement_type in statement_keys.items():
            frame = visible.get(output_key)
            if not isinstance(frame, pd.DataFrame):
                raise ValueError("PIT fundamental provider returned an invalid statement frame")
            for raw_period, raw_public_date in frame.attrs.get(PIT_PUBLIC_DATES_ATTR, {}).items():
                visible_dates[(statement_type, date.fromisoformat(raw_period))] = str(raw_public_date)
        declared_dates: dict[tuple[str, date], date] = {}
        for record in records:
            if record.available_from_session > session:
                continue
            key = ("quarterly", record.period_end)
            previous = declared_dates.get(key)
            if previous is None or record.available_from_session > previous:
                declared_dates[key] = record.available_from_session
        for key, raw_public_date in visible_dates.items():
            declared = declared_dates.get(key)
            actual = date.fromisoformat(raw_public_date)
            if declared != actual:
                raise ValueError(
                    f"source-public availability is missing or disagrees with PIT public date for {symbol} {key[1]}"
                )
        result[symbol] = records
    return MappingProxyType(result)


def build_current_feature_context_snapshot(
    *,
    bundle: PITDataBundle,
    decision_clock: CurrentDecisionClockV1,
    candidate_symbols: Iterable[str],
    price_history_by_symbol: Mapping[str, pd.DataFrame],
    rs_snapshot: Mapping[str, float],
    market_closes: pd.DataFrame,
    oneil_regime: str,
    distribution_days: int,
    follow_through: bool,
    missingness: Mapping[str, Mapping[str, object]],
    fundamental_availability: Mapping[str, Sequence[FundamentalAvailabilityV1]],
    unavailable_members: Mapping[str, object],
    source_revision: str,
    allow_schema_v2_development: bool = False,
) -> CurrentFeatureContextSnapshotV1:
    """Build entry features and complete market context from recorded inputs.

    ``candidate_symbols`` is the upstream candidate set before any legacy
    scanner ranking or RS floor. Low RS does not remove a supplied candidate.
    Every other active member needs an explicit data-unavailability record;
    members with both a current close and RS cannot be declared unavailable.
    Market context always uses the complete dated active universe as its
    denominator, including members without current price or RS observations.
    """

    if type(bundle) is not PITDataBundle:
        raise ValueError("bundle must be a hash-verified PITDataBundle")
    if type(decision_clock) is not CurrentDecisionClockV1:
        raise ValueError("decision_clock must be CurrentDecisionClockV1")
    if type(allow_schema_v2_development) is not bool:
        raise ValueError("allow_schema_v2_development must be a bool")
    if not isinstance(source_revision, str) or not source_revision.strip():
        raise ValueError("source_revision is required")
    session = decision_clock.completed_session
    if session > bundle.data_cutoff.date():
        raise ValueError("feature session exceeds the authenticated bundle cutoff")
    schema_version = bundle.metadata.get("schema_version")
    if schema_version not in {"2", "3"}:
        raise ValueError("current feature adapter requires a schema-V2 or schema-V3 bundle")
    if schema_version == "2" and not allow_schema_v2_development:
        raise ValueError("schema-V2 current features require explicit development scope")

    active_symbols = frozenset(bundle.members_at(session))
    candidates = _canonical_candidate_symbols(candidate_symbols, active_symbols)
    if not isinstance(rs_snapshot, Mapping):
        raise ValueError("rs_snapshot must be a mapping")
    if not isinstance(missingness, Mapping):
        raise ValueError("missingness must be a mapping")
    if not isinstance(price_history_by_symbol, Mapping):
        raise ValueError("price_history_by_symbol must be a mapping")
    missing_histories = tuple(
        symbol for symbol in candidates if symbol not in price_history_by_symbol
    )
    if missing_histories:
        raise ValueError(f"price history is missing for candidates: {missing_histories}")
    closes = _validated_market_closes(market_closes, session=session)
    unavailable = _unavailable_universe_members(
        unavailable_members,
        candidates=candidates,
        active_symbols=active_symbols,
        market_closes=closes,
        rs_snapshot=rs_snapshot,
        price_history_by_symbol=price_history_by_symbol,
        session=session,
    )
    if set(missingness).difference(candidates):
        raise ValueError("missingness contains symbols outside the candidate set")
    features_by_symbol: dict[str, EntryFeaturesV3] = {}
    missingness_by_symbol: dict[str, Mapping[str, FeatureMissingnessV1]] = {}
    for symbol in candidates:
        features = build_entry_features_v3(
            bundle=bundle,
            symbol=symbol,
            session=session,
            price_history=price_history_by_symbol[symbol],
            rs_snapshot=rs_snapshot,
            allow_schema_v2_development=allow_schema_v2_development,
        )
        features_by_symbol[symbol] = features
        missingness_by_symbol[symbol] = _coerce_missingness(
            missingness,
            symbol=symbol,
            features=features,
        )

    availability = _availability_records(
        fundamental_availability,
        candidates=candidates,
        bundle=bundle,
        session=session,
    )
    context = build_market_context(
        session=pd.Timestamp(session),
        oneil_regime=oneil_regime,
        distribution_days=distribution_days,
        follow_through=follow_through,
        closes=closes,
        active_constituents=tuple(sorted(active_symbols)),
        rs_scores=rs_snapshot,
    )
    source_universes = (
        ("sp500",)
        if schema_version == "2"
        else ("nasdaq100", "russell2000", "sp500")
    )
    return CurrentFeatureContextSnapshotV1(
        decision_clock=decision_clock,
        candidate_symbols=candidates,
        entry_features=features_by_symbol,
        market_context=context,
        missingness=missingness_by_symbol,
        fundamental_availability=availability,
        unavailable_members=unavailable,
        source_revision=source_revision,
        data_bundle_sha256=bundle.sha256,
        feature_contract_id=FEATURE_CONTRACT_ID,
        feature_calculator_identity=FEATURE_CALCULATOR_IDENTITY,
        universe_ids=source_universes,
        development_only=schema_version == "2",
    )


__all__ = [
    "CurrentDecisionClockV1",
    "CurrentFeatureContextSnapshotV1",
    "FEATURE_CALCULATOR_IDENTITY",
    "FEATURE_CONTRACT_ID",
    "FeatureMissingnessV1",
    "FundamentalAvailabilityV1",
    "UnavailableUniverseMemberV1",
    "build_current_feature_context_snapshot",
    "derive_available_from_source_date",
    "validate_normalized_availability",
]
