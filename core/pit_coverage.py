"""Stage-aware historical feature coverage for authenticated PIT bundles.

The report is an observation about one exact bundle and source revision. It
does not alter bundle contents or promote a limited development bundle to a
production claim. Each emitted security/session row uses the bundle's dated
membership denominator and keeps source, publication, lookback, calculation,
policy-input, and consumer facts separate.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from config import settings
import core.pit_feature_snapshot as pit_feature_snapshot_module
from core.canslim.a_annual_earnings import (
    _calculate_roe,
    evaluate_a_with_trace,
)
from core.canslim.c_current_earnings import evaluate_c_with_trace
from core.canslim.fiscal_periods import match_fiscal_year_over_year_periods
from core.canslim.l_leader_laggard import calculate_group_rs
from core.canslim.n_new_products import evaluate_n
from core.industry_group import load_pit_industry_assignments_as_of
from core.momentum_analysis import calculate_rs_snapshot
from core.pit_data import PITDataBundle
from core.pit_feature_snapshot import (
    _earnings_acceleration,
    _growth_acceleration_for_label,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


REPORT_SCHEMA = "historical_feature_coverage_v2"
_FULL_WINDOW_REQUIRED_SLOTS = {
    "quarterly_eps_growth": 4,
    "annual_eps_growth": 3,
    "annual_revenue_growth": 3,
}
_DIGEST_LENGTH = 64
_SOURCE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "quarterly_eps_growth": {"quarterly": ("diluted_eps", "basic_eps", "net_income")},
    "quarterly_eps_growth_acceleration": {
        "quarterly": ("diluted_eps", "basic_eps", "net_income")
    },
    "quarterly_revenue_growth": {"quarterly": ("total_revenue",)},
    "quarterly_revenue_growth_acceleration": {"quarterly": ("total_revenue",)},
    "annual_eps_growth": {"annual": ("diluted_eps", "basic_eps", "net_income")},
    "annual_revenue_growth": {"annual": ("total_revenue",)},
    "annual_roe": {
        "annual": ("net_income",),
        "balance": ("total_stockholders_equity",),
    },
    "shares_outstanding": {
        "quarterly": ("shares_outstanding",),
        "annual": ("shares_outstanding",),
        "balance": ("shares_outstanding",),
        "institutional": ("shares_outstanding",),
    },
    "institutional_ownership_fraction": {
        "quarterly": ("held_percent_institutions",),
        "annual": ("held_percent_institutions",),
        "balance": ("held_percent_institutions",),
        "institutional": ("held_percent_institutions",),
    },
    "institution_count_trend": {
        "quarterly": ("institution_count", "prev_institution_count"),
        "annual": ("institution_count", "prev_institution_count"),
        "balance": ("institution_count", "prev_institution_count"),
        "institutional": ("institution_count", "prev_institution_count"),
    },
    "fundamental_age_days": {"quarterly": ("basic_eps", "diluted_eps", "total_revenue", "net_income")},
}
_PRICE_SPEC_METRICS = {
    "relative_strength_score": "relative_strength_score",
    "breadth_above_50": "above_50",
    "breadth_above_200": "above_200",
    "atr_20_fraction": "atr_20_fraction",
    "breakout_gap_fraction": "breakout_gap_fraction",
    "average_dollar_volume_50": "average_dollar_volume_50",
    "distance_from_52_week_high_fraction": "distance_from_52_week_high_fraction",
    "volume_ratio_50": "volume_ratio_50",
}


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    feature_id: str
    description: str
    calculator: str
    minimum_history: int | None
    policy_input: str
    declared_consumer_status: str
    consumer_paths: tuple[str, ...]


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "quarterly_eps_growth",
        "Latest visible fiscal-quarter EPS-family year-over-year growth (existing net-income fallback retained).",
        "core.canslim.c_current_earnings.evaluate_c_with_trace",
        2,
        "current_baseline_input",
        "consumed_by_current_baseline",
        ("core.canslim.core.evaluate_canslim:C", "core.strategy_policy.entry.evaluate_entry:current_growth"),
    ),
    FeatureSpec(
        "quarterly_revenue_growth",
        "Latest visible fiscal-quarter revenue year-over-year growth.",
        "core.canslim.n_new_products.evaluate_n",
        2,
        "current_baseline_input",
        "consumed_by_current_baseline",
        ("core.canslim.core.evaluate_canslim:N",),
    ),
    FeatureSpec(
        "quarterly_eps_growth_acceleration",
        "Difference between the two newest matched quarterly EPS-family growth rates.",
        "core.pit_feature_snapshot._earnings_acceleration",
        4,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.earnings_growth_acceleration",),
    ),
    FeatureSpec(
        "quarterly_revenue_growth_acceleration",
        "Difference between the two newest matched quarterly revenue growth rates.",
        "core.pit_feature_snapshot._growth_acceleration_for_label",
        4,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.sales_growth_acceleration",),
    ),
    FeatureSpec(
        "annual_eps_growth",
        "Latest visible annual EPS-family growth under the existing CANSLIM A evaluator.",
        "core.canslim.a_annual_earnings.evaluate_a_with_trace",
        2,
        "current_baseline_input",
        "consumed_by_current_baseline",
        ("core.canslim.core.evaluate_canslim:A", "core.strategy_policy.entry.evaluate_entry:annual_growth"),
    ),
    FeatureSpec(
        "annual_revenue_growth",
        "Latest annual revenue growth versus the immediately preceding reported annual observation.",
        "historical-feature-specification-v1:annual-revenue-growth-report-only-v1",
        2,
        "not_in_current_policy_interface",
        "intentionally_not_a_current_baseline_consumer",
        (),
    ),
    FeatureSpec(
        "annual_roe",
        "Latest visible annual net income divided by latest nonmissing positive stockholders' equity.",
        "core.canslim.a_annual_earnings._calculate_roe",
        1,
        "current_baseline_input",
        "consumed_by_current_baseline",
        ("core.canslim.core.evaluate_canslim:A",),
    ),
    FeatureSpec(
        "shares_outstanding",
        "Latest visible reported shares outstanding; a proxy denominator, not public float.",
        "core.pit_data.PITDataBundle._company_info",
        1,
        "current_baseline_input",
        "optional_input_consumed_by_current_baseline_when_present",
        ("core.canslim.core.evaluate_canslim:S",),
    ),
    FeatureSpec(
        "institutional_ownership_fraction",
        "Latest visible reported institutional ownership fraction.",
        "core.pit_data.PITDataBundle._company_info",
        1,
        "optional_current_baseline_input",
        "optional_input_consumed_by_current_baseline_when_present",
        ("core.canslim.core.evaluate_canslim:I",),
    ),
    FeatureSpec(
        "institution_count_trend",
        "Latest visible current/prior reported institutional counts.",
        "core.pit_data.PITDataBundle._company_info",
        1,
        "optional_current_baseline_input",
        "optional_input_consumed_by_current_baseline_when_present",
        ("core.canslim.core.evaluate_canslim:I",),
    ),
    FeatureSpec(
        "fundamental_age_days",
        "Calendar-day age from the newest visible quarterly public/available date.",
        "core.pit_feature_snapshot._fundamental_age_days",
        1,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.fundamental_age_days",),
    ),
    FeatureSpec(
        "relative_strength_score",
        "Cross-sectional weighted 12-month relative-strength score with the existing 60-close short-history fallback.",
        "core.momentum_analysis.calculate_rs_snapshot",
        60,
        "current_baseline_input",
        "consumed_by_current_baseline",
        ("core.canslim.core.evaluate_canslim:L", "core.strategy_policy.entry.evaluate_entry:rs_score"),
    ),
    FeatureSpec(
        "breadth_above_50",
        "Member close above its 50-observation simple moving average.",
        "core.strategy_policy.market_context.build_market_context",
        50,
        "market_context_input",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.strategy_policy.contracts.MarketContextV1.breadth_above_50_fraction",),
    ),
    FeatureSpec(
        "breadth_above_200",
        "Member close above its 200-observation simple moving average.",
        "core.strategy_policy.market_context.build_market_context",
        200,
        "market_context_input",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.strategy_policy.contracts.MarketContextV1.breadth_above_200_fraction",),
    ),
    FeatureSpec(
        "industry_group_rs",
        "Mean relative-strength score of active members with the same dated industry assignment.",
        "core.canslim.l_leader_laggard.calculate_group_rs",
        1,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.industry_group_rs",),
    ),
    FeatureSpec(
        "atr_20_fraction",
        "Mean 20-session true range divided by completed-session close.",
        "core.pit_feature_snapshot._atr_20_fraction",
        21,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.atr_20_fraction",),
    ),
    FeatureSpec(
        "breakout_gap_fraction",
        "Completed-session open gap versus the prior available close.",
        "core.pit_feature_snapshot._breakout_gap_fraction",
        2,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.breakout_gap_fraction",),
    ),
    FeatureSpec(
        "average_dollar_volume_50",
        "Mean close times volume for the prior 50 observations, excluding the event session.",
        "core.pit_feature_snapshot._average_dollar_volume_50",
        51,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.average_dollar_volume_50",),
    ),
    FeatureSpec(
        "distance_from_52_week_high_fraction",
        "Completed-session close divided by the highest high in 252 observations, minus one.",
        "core.pit_feature_snapshot._distance_from_52_week_high",
        252,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.EntryFeaturesV3.distance_from_52_week_high_fraction",),
    ),
    FeatureSpec(
        "volume_ratio_50",
        "Event-session volume divided by the prior 50-observation average volume.",
        "core.pit_feature_snapshot._volume_ratio_50",
        51,
        "v3_feature_exposed",
        "exposed_but_not_consumed_by_v3_parity_baseline",
        ("core.pit_feature_snapshot.HoldingFeaturesV3.volume_ratio",),
    ),
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_committed_source(source_revision: str) -> str:
    """Require the named source to be the clean checked-out commit.

    Return the canonical Git blob ID for the financial feature module so the
    report can bind its calculator identity independently of checkout line
    endings.
    """

    repository = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != source_revision:
        raise ValueError("source_revision does not match the checked-out commit")
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ValueError("coverage measurement requires a clean committed source tree")
    calculator_blob = subprocess.run(
        ["git", "rev-parse", f"{source_revision}:core/pit_feature_snapshot.py"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return calculator_blob


def resolve_asof_observation(
    observations: Iterable[Mapping[str, Any]],
    session: date,
    *,
    available_field: str = "available_from_session",
    identity_field: str = "period_end",
) -> Mapping[str, Any] | None:
    """Return the latest public revision per period as of ``session``.

    Input availability dates are already normalized to first eligible sessions.
    This helper intentionally does not shift them again.
    """

    if type(session) is not date:
        raise ValueError("coverage session must be a date")
    visible: list[Mapping[str, Any]] = []
    for observation in observations:
        raw_available = observation.get(available_field)
        raw_identity = observation.get(identity_field)
        if not isinstance(raw_available, str) or not isinstance(raw_identity, str):
            raise ValueError("coverage observation identity/date is invalid")
        try:
            available = date.fromisoformat(raw_available)
        except ValueError as exc:
            raise ValueError("coverage observation availability date is invalid") from exc
        if available <= session:
            visible.append(observation)
    if not visible:
        return None
    by_period: dict[str, Mapping[str, Any]] = {}
    for observation in sorted(
        visible,
        key=lambda value: (str(value[identity_field]), str(value[available_field])),
    ):
        by_period[str(observation[identity_field])] = observation
    return max(by_period.values(), key=lambda value: str(value[identity_field]))


def resolve_asof_classification(
    observations: Iterable[Mapping[str, Any]],
    session: date,
) -> Mapping[str, Any] | None:
    """Select the latest effective, publicly available classification.

    Fixtures and future bundle versions must carry both dates. ``public_date``
    is converted to a strict-next-session ``available_from_session`` upstream.
    """

    if type(session) is not date:
        raise ValueError("coverage session must be a date")
    eligible: list[Mapping[str, Any]] = []
    for observation in observations:
        effective = observation.get("effective_date")
        available = observation.get("available_from_session")
        group_id = observation.get("group_id")
        if not all(isinstance(value, str) for value in (effective, available, group_id)):
            raise ValueError("coverage classification record is invalid")
        try:
            effective_date = date.fromisoformat(effective)
            available_date = date.fromisoformat(available)
        except ValueError as exc:
            raise ValueError("coverage classification dates are invalid") from exc
        if effective_date <= session and available_date <= session:
            eligible.append(observation)
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda value: (
            str(value["effective_date"]),
            str(value["available_from_session"]),
        ),
    )


def classify_lookback(available: int, required: int) -> tuple[bool, str | None]:
    """Report a deterministic warm-up result without inventing a value."""

    if type(available) is not int or available < 0:
        raise ValueError("available lookback must be a nonnegative integer")
    if type(required) is not int or required <= 0:
        raise ValueError("required lookback must be a positive integer")
    return (True, None) if available >= required else (False, "insufficient_history")


def _read_json_object(path: str | Path, name: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"{name} must be a regular non-link file")
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return value


def _source_rows(bundle: PITDataBundle) -> dict[str, list[dict[str, Any]]]:
    rows = bundle._connection.execute(
        "SELECT ticker, statement_type, period_end, public_date, basic_eps, diluted_eps, "
        "total_revenue, net_income, total_stockholders_equity, shares_outstanding, "
        "held_percent_institutions, institution_count, prev_institution_count "
        "FROM fundamentals ORDER BY ticker, public_date, period_end"
    )
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row["ticker"])].append(dict(row))
    return dict(by_ticker)


def _row_has_values(row: Mapping[str, Any], fields: Iterable[str]) -> bool:
    return any(row.get(field) is not None for field in fields)


def _source_match(feature_id: str, row: Mapping[str, Any]) -> bool:
    requirements = _SOURCE_FIELDS[feature_id]
    statement_type = str(row.get("statement_type"))
    return statement_type in requirements and _row_has_values(
        row, requirements[statement_type]
    )


def _visible_source_rows(
    records: Iterable[Mapping[str, Any]], feature_id: str, session: date
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in records
        if _source_match(feature_id, row)
        and date.fromisoformat(str(row["public_date"])) <= session
    ]


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    return pd.Timestamp(value).date().isoformat()


def _feature_state(
    *,
    calculated: Any,
    reason: str | None,
    required_history: int | None,
    available_history: int,
    selected_periods: Iterable[Any] = (),
    selected_public_dates: Iterable[Any] = (),
    metric_family: str | None = None,
    lookback_ready: bool | None = None,
) -> dict[str, Any]:
    periods = tuple(value for value in selected_periods if value is not None)
    public_dates = tuple(value for value in selected_public_dates if value is not None)
    calculable = calculated is not None
    if calculable:
        try:
            number = float(calculated) if not isinstance(calculated, bool) else None
        except (TypeError, ValueError, OverflowError):
            number = None
        if number is not None and not math.isfinite(number):
            calculable = False
            reason = "invalid_nonfinite_calculation"
    if lookback_ready is None:
        lookback_ready = (
            calculable
            if required_history is None
            else available_history >= required_history
        )
    return {
        "calculable": calculable,
        "lookback_ready": bool(lookback_ready),
        "reason": None if calculable else (reason or "insufficient_history"),
        "required_history": required_history,
        "available_history": max(0, int(available_history)),
        "selected_period_ends": sorted({_date_text(value) for value in periods}),
        "selected_available_from_sessions": sorted({_date_text(value) for value in public_dates}),
        "metric_family": metric_family,
    }


def _series_row(frame: pd.DataFrame, patterns: tuple[str, ...]) -> pd.Series | None:
    if frame.empty:
        return None
    for pattern in patterns:
        matches = [
            label
            for label in frame.index
            if pattern.casefold() in str(label).casefold()
        ]
        if matches:
            row = frame.loc[matches[0]]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            if isinstance(row, pd.Series):
                return row.sort_index()
    return None


def _eps_series_for_family(
    frame: pd.DataFrame, metric_family: Any
) -> pd.Series | None:
    pattern_by_family = {
        "diluted_eps": "Diluted EPS",
        "basic_eps": "Basic EPS",
        "net_income": "Net Income",
    }
    pattern = pattern_by_family.get(str(metric_family))
    return None if pattern is None else _series_row(frame, (pattern,))


def _finite_number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _missing_full_window(
    required_slots: int, reason: str, *, annual: bool = False
) -> dict[str, Any]:
    slots = [
        {
            "slot": slot_number,
            "current_period_end": None,
            "comparison_period_end": None,
            "comparison_period_matched": False,
            "status": "missing",
            "reason": reason,
        }
        for slot_number in range(1, required_slots + 1)
    ]
    result = {
        "ready": False,
        "required_growth_slots": required_slots,
        "represented_period_slots": 0,
        "reported_level_count": 0,
        "matched_growth_slots": 0,
        "missing_growth_slots": required_slots,
        "missing_slot_reason_counts": {reason: required_slots},
        "slots": slots,
        "skipped_fiscal_year_gaps": [],
    }
    if annual:
        result["consecutive_fiscal_years_ready"] = False
    return result


def _growth_window_payload(
    *,
    required_slots: int,
    represented_period_slots: int,
    reported_level_count: int,
    slots: list[dict[str, Any]],
    skipped_fiscal_year_gaps: list[dict[str, Any]] | None = None,
    annual: bool = False,
) -> dict[str, Any]:
    matched = sum(slot["status"] == "matched" for slot in slots)
    missing_reasons = Counter(
        str(slot["reason"])
        for slot in slots
        if slot["status"] != "matched" and slot.get("reason") is not None
    )
    gaps = skipped_fiscal_year_gaps or []
    ready = matched == required_slots
    result = {
        "ready": ready,
        "required_growth_slots": required_slots,
        "represented_period_slots": represented_period_slots,
        "reported_level_count": reported_level_count,
        "matched_growth_slots": matched,
        "missing_growth_slots": required_slots - matched,
        "missing_slot_reason_counts": dict(sorted(missing_reasons.items())),
        "slots": slots,
        "skipped_fiscal_year_gaps": gaps,
    }
    if annual:
        result["consecutive_fiscal_years_ready"] = ready and not gaps
    return result


def _quarterly_eps_full_window(
    series: pd.Series | None, required_slots: int = 4
) -> dict[str, Any]:
    if series is None:
        return _missing_full_window(required_slots, "absent_source_observation")

    matches = match_fiscal_year_over_year_periods(series)
    slots: list[dict[str, Any]] = []
    for slot_number in range(1, required_slots + 1):
        if slot_number > len(matches):
            slots.append(
                {
                    "slot": slot_number,
                    "current_period_end": None,
                    "comparison_period_end": None,
                    "comparison_period_matched": False,
                    "status": "missing",
                    "reason": "insufficient_reported_periods",
                }
            )
            continue

        match = matches[slot_number - 1]
        current_period = match.current_period.isoformat()
        comparison_period = (
            None if match.prior_period is None else match.prior_period.isoformat()
        )
        reason = None
        current = _finite_number_or_none(match.current_value)
        prior = _finite_number_or_none(match.prior_value)
        if current is None:
            reason = (
                "missing_current_value"
                if pd.isna(match.current_value)
                else "invalid_nonfinite_current_value"
            )
        elif not match.matched:
            reason = "missing_comparable_period"
        elif prior is None:
            reason = (
                "missing_comparison_value"
                if pd.isna(match.prior_value)
                else "invalid_nonfinite_comparison_value"
            )
        elif prior <= 0 or bool(np.isclose(prior, 0.0)):
            reason = "invalid_nonpositive_or_near_zero_comparison_value"
        elif _finite_number_or_none((current / prior) - 1.0) is None:
            reason = "invalid_nonfinite_growth"

        slots.append(
            {
                "slot": slot_number,
                "current_period_end": current_period,
                "comparison_period_end": comparison_period,
                "comparison_period_matched": bool(match.matched),
                "status": "matched" if reason is None else "missing",
                "reason": reason,
            }
        )

    return _growth_window_payload(
        required_slots=required_slots,
        represented_period_slots=min(len(matches), required_slots),
        reported_level_count=int(series.notna().sum()),
        slots=slots,
    )


def _annual_growth_full_window(
    series: pd.Series | None,
    required_slots: int = 3,
    *,
    reject_near_zero_prior: bool = True,
) -> dict[str, Any]:
    if series is None:
        return _missing_full_window(required_slots, "absent_source_observation", annual=True)

    levels = series.dropna().sort_index()
    latest_levels = list(levels.iloc[-(required_slots + 1) :].items())
    slots: list[dict[str, Any]] = []
    skipped_gaps: list[dict[str, Any]] = []
    pair_count = min(max(0, len(latest_levels) - 1), required_slots)
    for slot_number in range(1, required_slots + 1):
        if slot_number > pair_count:
            slots.append(
                {
                    "slot": slot_number,
                    "current_period_end": None,
                    "comparison_period_end": None,
                    "comparison_period_matched": False,
                    "status": "missing",
                    "reason": "insufficient_reported_periods",
                    "skipped_fiscal_years": 0,
                }
            )
            continue

        current_label, current_raw = latest_levels[-slot_number]
        comparison_label, comparison_raw = latest_levels[-slot_number - 1]
        current_period = pd.Timestamp(current_label).date()
        comparison_period = pd.Timestamp(comparison_label).date()
        skipped_years = max(0, current_period.year - comparison_period.year - 1)
        current = _finite_number_or_none(current_raw)
        prior = _finite_number_or_none(comparison_raw)
        reason = None
        if current is None:
            reason = (
                "missing_current_value"
                if pd.isna(current_raw)
                else "invalid_nonfinite_current_value"
            )
        elif prior is None:
            reason = (
                "missing_comparison_value"
                if pd.isna(comparison_raw)
                else "invalid_nonfinite_comparison_value"
            )
        elif prior <= 0 or (reject_near_zero_prior and bool(np.isclose(prior, 0.0))):
            reason = (
                "invalid_nonpositive_or_near_zero_comparison_value"
                if reject_near_zero_prior
                else "invalid_nonpositive_comparison_value"
            )
        elif _finite_number_or_none((current / prior) - 1.0) is None:
            reason = "invalid_nonfinite_growth"

        slot = {
            "slot": slot_number,
            "current_period_end": current_period.isoformat(),
            "comparison_period_end": comparison_period.isoformat(),
            "comparison_period_matched": True,
            "status": "matched" if reason is None else "missing",
            "reason": reason,
            "skipped_fiscal_years": skipped_years,
        }
        slots.append(slot)
        if skipped_years:
            skipped_gaps.append(
                {
                    "current_period_end": current_period.isoformat(),
                    "comparison_period_end": comparison_period.isoformat(),
                    "skipped_fiscal_years": skipped_years,
                    "slot": slot_number,
                }
            )

    return _growth_window_payload(
        required_slots=required_slots,
        represented_period_slots=pair_count,
        reported_level_count=len(levels),
        slots=slots,
        skipped_fiscal_year_gaps=skipped_gaps,
        annual=True,
    )


def _selected_public_dates(frame: pd.DataFrame, periods: Iterable[Any]) -> list[str]:
    raw = frame.attrs.get(PIT_PUBLIC_DATES_ATTR, {})
    if not isinstance(raw, Mapping):
        return []
    dates: list[str] = []
    for period in periods:
        if period is None:
            continue
        key = _date_text(period)
        value = raw.get(key)
        if isinstance(value, str):
            dates.append(value)
    return sorted(set(dates))


def _latest_observed_quarterly_public_period(
    quarterly: pd.DataFrame,
) -> tuple[str, str] | None:
    """Return the latest public/period pair with at least one observed fact."""

    raw_public_dates = quarterly.attrs.get(PIT_PUBLIC_DATES_ATTR, {})
    candidates: list[tuple[date, date]] = []
    for column in quarterly.columns:
        period_timestamp = pd.Timestamp(column)
        if pd.isna(period_timestamp):
            raise ValueError("PIT quarterly period provenance is invalid")
        period = period_timestamp.date()
        values = quarterly.loc[:, column].to_numpy().ravel()
        if not any(not pd.isna(value) for value in values):
            continue
        public_text = raw_public_dates.get(period.isoformat())
        if not isinstance(public_text, str):
            raise ValueError("PIT quarterly fundamentals are missing public-date provenance")
        candidates.append((date.fromisoformat(public_text), period))
    if not candidates:
        return None
    public_date, period = max(candidates)
    return period.isoformat(), public_date.isoformat()


def _fundamental_age_state(quarterly: pd.DataFrame, session: date) -> dict[str, Any]:
    age = pit_feature_snapshot_module._fundamental_age_days(quarterly, session)
    latest = _latest_observed_quarterly_public_period(quarterly)
    return _feature_state(
        calculated=age,
        reason="absent_visible_quarterly_observation",
        required_history=1,
        available_history=int(age is not None),
        selected_periods=() if latest is None else (latest[0],),
        selected_public_dates=() if latest is None else (latest[1],),
        lookback_ready=age is not None,
    )


def _latest_matched_periods(series: pd.Series | None, limit: int) -> tuple[Any, ...]:
    if series is None:
        return ()
    matches = match_fiscal_year_over_year_periods(series)
    return tuple(
        match.current_period
        for match in matches[:limit]
        if match.matched
    )


def _quarterly_revenue_state(quarterly: pd.DataFrame) -> dict[str, Any]:
    series = _series_row(quarterly, ("Total Revenue", "Revenue"))
    if series is None:
        return _feature_state(
            calculated=None,
            reason="absent_required_source_field",
            required_history=2,
            available_history=0,
        )
    matches = match_fiscal_year_over_year_periods(series)
    latest = matches[0] if matches else None
    _score, growth = evaluate_n(quarterly, 1.0)
    if latest is None or not latest.matched:
        reason = "missing_comparable_period" if len(series.dropna()) >= 2 else "insufficient_history"
        periods = () if latest is None else (latest.current_period,)
        return _feature_state(
            calculated=None,
            reason=reason,
            required_history=2,
            available_history=int(series.notna().sum()),
            selected_periods=periods,
            selected_public_dates=_selected_public_dates(quarterly, periods),
            lookback_ready=False,
        )
    prior = latest.prior_value
    if prior is not None and float(prior) <= 0:
        reason = "invalid_nonpositive_comparison_value"
    elif growth is None:
        reason = "invalid_growth_calculation"
    else:
        reason = None
    periods = (latest.current_period, latest.prior_period)
    return _feature_state(
        calculated=growth,
        reason=reason,
        required_history=2,
        available_history=int(series.notna().sum()),
        selected_periods=periods,
        selected_public_dates=_selected_public_dates(quarterly, periods),
        lookback_ready=True,
    )


def _annual_revenue_state(annual: pd.DataFrame) -> dict[str, Any]:
    series = _series_row(annual, ("Total Revenue", "Revenue"))
    if series is None:
        return _feature_state(
            calculated=None,
            reason="absent_required_source_field",
            required_history=2,
            available_history=0,
        )
    visible = series.dropna().sort_index()
    if len(visible) < 2:
        periods = tuple(visible.index)
        return _feature_state(
            calculated=None,
            reason="insufficient_history",
            required_history=2,
            available_history=len(visible),
            selected_periods=periods,
            selected_public_dates=_selected_public_dates(annual, periods),
            lookback_ready=False,
        )
    current_period, prior_period = visible.index[-1], visible.index[-2]
    current, prior = float(visible.iloc[-1]), float(visible.iloc[-2])
    if not math.isfinite(current) or not math.isfinite(prior) or prior <= 0:
        growth = None
        reason = "invalid_nonpositive_or_nonfinite_comparison"
    else:
        growth = (current / prior) - 1.0
        reason = None
    periods = (current_period, prior_period)
    return _feature_state(
        calculated=growth,
        reason=reason,
        required_history=2,
        available_history=len(visible),
        selected_periods=periods,
        selected_public_dates=_selected_public_dates(annual, periods),
        lookback_ready=True,
    )


def _financial_states(
    bundle: PITDataBundle,
    tickers: Iterable[str],
    start: date,
    cutoff: date,
    source_rows: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[tuple[date, dict[str, dict[str, Any]]]]]:
    bounds = {ticker: (start, cutoff) for ticker in sorted(set(tickers))}
    states: dict[str, list[tuple[date, dict[str, dict[str, Any]]]]] = defaultdict(list)
    for ticker, boundary, snapshot in bundle.iter_fundamental_state_boundaries(
        bounds, include_provenance=True
    ):
        quarterly = snapshot["quarterly_income"]
        annual = snapshot["annual_income"]
        balance = snapshot["balance_sheet"]
        company = snapshot["company_info"]
        raw_records = source_rows.get(ticker, ())

        c_trace = evaluate_c_with_trace(quarterly)
        q_eps = _feature_state(
            calculated=c_trace.current_growth,
            reason=str(c_trace.terminal_reason),
            required_history=2,
            available_history=(0 if quarterly.empty else int(quarterly.notna().any(axis=0).sum())),
            selected_periods=(c_trace.current_period_end, c_trace.prior_period_end),
            selected_public_dates=(c_trace.current_public_date, c_trace.prior_public_date),
            metric_family=str(c_trace.metric_family),
            lookback_ready=(
                c_trace.current_growth is not None
                or str(c_trace.terminal_reason).casefold()
                not in {"no_visible_observation", "no_comparable_prior_period", "insufficient_annual_history"}
            ),
        )
        q_eps["full_window"] = _quarterly_eps_full_window(
            _eps_series_for_family(quarterly, c_trace.metric_family)
        )

        q_revenue = _quarterly_revenue_state(quarterly)
        eps_series = _series_row(quarterly, ("Diluted EPS", "Basic EPS", "Net Income"))
        eps_accel = _earnings_acceleration(quarterly)
        eps_periods = _latest_matched_periods(eps_series, 2)
        eps_dates = _selected_public_dates(quarterly, eps_periods)
        eps_accel_state = _feature_state(
            calculated=eps_accel,
            reason=(
                None
                if eps_accel is not None
                else ("missing_comparable_period" if eps_series is not None and int(eps_series.notna().sum()) >= 4 else "insufficient_history")
            ),
            required_history=4,
            available_history=0 if eps_series is None else int(eps_series.notna().sum()),
            selected_periods=eps_periods,
            selected_public_dates=eps_dates,
            metric_family=("diluted_eps" if eps_series is not None and "diluted" in str(eps_series.name).casefold() else None),
            lookback_ready=(eps_accel is not None),
        )

        revenue_series = _series_row(quarterly, ("Total Revenue", "Revenue"))
        revenue_accel = _growth_acceleration_for_label(quarterly, "Total Revenue")
        revenue_periods = _latest_matched_periods(revenue_series, 2)
        revenue_accel_state = _feature_state(
            calculated=revenue_accel,
            reason=(
                None
                if revenue_accel is not None
                else ("missing_comparable_period" if revenue_series is not None and int(revenue_series.notna().sum()) >= 4 else "insufficient_history")
            ),
            required_history=4,
            available_history=0 if revenue_series is None else int(revenue_series.notna().sum()),
            selected_periods=revenue_periods,
            selected_public_dates=_selected_public_dates(quarterly, revenue_periods),
            lookback_ready=(revenue_accel is not None),
        )

        a_trace = evaluate_a_with_trace(annual, balance_sheet=balance)
        a_state = _feature_state(
            calculated=a_trace.annual_growth,
            reason=str(a_trace.terminal_reason),
            required_history=2,
            available_history=(0 if annual.empty else int(annual.notna().any(axis=0).sum())),
            selected_periods=(a_trace.current_period_end, a_trace.prior_period_end),
            selected_public_dates=(a_trace.current_public_date, a_trace.prior_public_date),
            metric_family=str(a_trace.metric_family),
            lookback_ready=(
                a_trace.annual_growth is not None
                or (
                    int(annual.notna().any(axis=0).sum()) >= 2
                    and str(a_trace.terminal_reason).casefold()
                    not in {"no_visible_observation", "insufficient_annual_history"}
                )
            ),
        )
        a_state["full_window"] = _annual_growth_full_window(
            _eps_series_for_family(annual, a_trace.metric_family)
        )
        annual_revenue = _annual_revenue_state(annual)
        annual_revenue["full_window"] = _annual_growth_full_window(
            _series_row(annual, ("Total Revenue", "Revenue")),
            reject_near_zero_prior=False,
        )

        roe = _calculate_roe(annual, balance) if not annual.empty and not balance.empty else None
        net_income_series = _series_row(annual, ("Net Income",))
        equity_series = _series_row(balance, ("Total Stockholders Equity", "Stockholders Equity", "Total Equity"))
        roe_periods = tuple(
            value
            for value in (
                None if net_income_series is None or net_income_series.dropna().empty else net_income_series.dropna().index[-1],
                None if equity_series is None or equity_series.dropna().empty else equity_series.dropna().index[-1],
            )
            if value is not None
        )
        roe_dates = _selected_public_dates(annual, roe_periods[:1]) + _selected_public_dates(balance, roe_periods[1:])
        if roe is not None:
            roe_reason = None
        elif net_income_series is None or net_income_series.dropna().empty:
            roe_reason = "absent_annual_net_income"
        elif equity_series is None or equity_series.dropna().empty:
            roe_reason = "absent_stockholders_equity"
        else:
            roe_reason = "invalid_nonpositive_equity_or_roe"
        roe_state = _feature_state(
            calculated=roe,
            reason=roe_reason,
            required_history=1,
            available_history=int(net_income_series.notna().sum() if net_income_series is not None else 0)
            + int(equity_series.notna().sum() if equity_series is not None else 0),
            selected_periods=roe_periods,
            selected_public_dates=roe_dates,
            lookback_ready=(
                net_income_series is not None
                and not net_income_series.dropna().empty
                and equity_series is not None
                and not equity_series.dropna().empty
            ),
        )

        fundamental_age = _fundamental_age_state(quarterly, boundary)

        def company_state(
            feature: str,
            value: Any,
            *,
            source_rows: list[dict[str, Any]] = raw_records,
            asof: date = boundary,
        ) -> dict[str, Any]:
            fields = {
                "shares_outstanding": ("shares_outstanding",),
                "institutional_ownership_fraction": ("held_percent_institutions",),
                "institution_count_trend": ("institution_count", "prev_institution_count"),
            }[feature]
            selected = [
                row
                for row in source_rows
                if date.fromisoformat(str(row["public_date"])) <= asof
                and _row_has_values(row, fields)
            ]
            latest = selected[-1] if selected else None
            return _feature_state(
                calculated=value,
                reason="absent_visible_source_value",
                required_history=1,
                available_history=int(value is not None),
                selected_periods=(() if latest is None else (latest["period_end"],)),
                selected_public_dates=(() if latest is None else (latest["public_date"],)),
            )

        state = {
            "quarterly_eps_growth": q_eps,
            "quarterly_revenue_growth": q_revenue,
            "quarterly_eps_growth_acceleration": eps_accel_state,
            "quarterly_revenue_growth_acceleration": revenue_accel_state,
            "annual_eps_growth": a_state,
            "annual_revenue_growth": annual_revenue,
            "annual_roe": roe_state,
            "shares_outstanding": company_state("shares_outstanding", company.get("shares_outstanding")),
            "institutional_ownership_fraction": company_state(
                "institutional_ownership_fraction", company.get("held_percent_institutions")
            ),
            "institution_count_trend": company_state(
                "institution_count_trend",
                (company.get("institution_count"), company.get("prev_institution_count"))
                if company.get("institution_count") is not None
                and company.get("prev_institution_count") is not None
                else None,
            ),
            "fundamental_age_days": fundamental_age,
        }
        states[ticker].append((boundary, state))
    return dict(states)


def _price_metrics(frame: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(index=sessions)
    close = frame["Close"].astype(float)
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    open_price = frame["Open"].astype(float)
    volume = frame["Volume"].astype(float)
    prior_close = close.shift(1)
    true_range = pd.concat(
        (high - low, (high - prior_close).abs(), (low - prior_close).abs()),
        axis=1,
    ).max(axis=1)
    true_range.iloc[0] = np.nan
    dollar_volume = close * volume
    available = pd.Series(np.arange(1, len(frame) + 1), index=frame.index, dtype="int32")
    result = pd.DataFrame(index=frame.index)
    result["close"] = close
    result["available_bars"] = available
    sma_50 = close.rolling(50, min_periods=50).mean()
    sma_200 = close.rolling(200, min_periods=200).mean()
    result["above_50"] = (close > sma_50).where(sma_50.notna())
    result["above_200"] = (close > sma_200).where(sma_200.notna())
    result["atr_20_fraction"] = true_range.rolling(20, min_periods=20).mean() / close
    result["breakout_gap_fraction"] = (open_price - prior_close) / prior_close
    result["average_dollar_volume_50"] = dollar_volume.shift(1).rolling(50, min_periods=50).mean()
    result["distance_from_52_week_high_fraction"] = close / high.rolling(252, min_periods=252).max() - 1.0
    prior_average_volume = volume.shift(1).rolling(50, min_periods=50).mean()
    result["volume_ratio_50"] = volume / prior_average_volume.where(prior_average_volume > 0)
    result["bar_observed"] = True
    return result.reindex(sessions)


def _raw_price_counts(price_data: Mapping[str, pd.DataFrame]) -> dict[str, int]:
    return {ticker: len(frame) for ticker, frame in price_data.items()}


def _raw_classification_rows(bundle: PITDataBundle) -> dict[str, list[dict[str, Any]]]:
    tables = {
        str(row[0])
        for row in bundle._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "industry_group_snapshots" not in tables:
        return {}
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bundle._connection.execute(
        "SELECT symbol, as_of_date, group_id FROM industry_group_snapshots "
        "ORDER BY symbol, as_of_date"
    ):
        result[str(row["symbol"])].append(
            {
                "effective_date": str(row["as_of_date"]),
                "available_from_session": str(row["as_of_date"]),
                "group_id": str(row["group_id"]),
            }
        )
    return dict(result)


def _reason_for_unready(
    *, raw_count: int, visible_count: int, payload: Mapping[str, Any]
) -> str | None:
    if payload.get("calculable"):
        return None
    if raw_count == 0:
        detail = str(payload.get("reason") or "")
        if detail.startswith("absent_") or detail in {
            "classification_absent",
            "missing_current_session_price",
        }:
            return detail
        return "absent"
    if visible_count == 0:
        return "not_yet_public"
    return str(payload.get("reason") or "insufficient_history")


def _cell(
    *,
    spec: FeatureSpec,
    raw_count: int,
    visible_count: int,
    future_count: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    reason = _reason_for_unready(
        raw_count=raw_count, visible_count=visible_count, payload=payload
    )
    has_value = bool(payload.get("calculable"))
    input_ready = has_value and spec.policy_input != "not_in_current_policy_interface"
    policy_reason = None if input_ready else (
        "not_in_current_policy_interface" if has_value else reason
    )
    lookback_ready = bool(payload.get("lookback_ready"))
    stage_reason = reason or ""
    if stage_reason == "not_yet_public":
        unavailable_stage = "not_yet_public"
    elif stage_reason.startswith("absent"):
        unavailable_stage = "absent"
    elif stage_reason.startswith("invalid"):
        unavailable_stage = "invalid"
    else:
        unavailable_stage = "insufficient_history"
    result = {
        "source_observation_rows": int(raw_count),
        "public_observation_rows": int(visible_count),
        "not_yet_public_observation_rows": int(future_count),
        "required_history": payload.get("required_history", spec.minimum_history),
        "available_history": int(payload.get("available_history", visible_count)),
        "source_stage": "observed" if raw_count else "absent",
        "publication_stage": "observed" if visible_count else ("not_yet_public" if raw_count else "absent"),
        "lookback_stage": "observed" if lookback_ready else unavailable_stage,
        "calculation_stage": "observed" if has_value else unavailable_stage,
        "policy_input_stage": "observed" if input_ready else (
            "intentionally_not_exposed" if has_value else unavailable_stage
        ),
        "declared_policy_consumer_status": spec.declared_consumer_status,
        "actual_policy_consumption": "not_measured_no_policy_replay",
        "consumer_paths": list(spec.consumer_paths),
        "reason_codes": [] if reason is None else [reason],
        "policy_input_reason": policy_reason,
        "selected_period_ends": list(payload.get("selected_period_ends", ())),
        "selected_available_from_sessions": list(payload.get("selected_available_from_sessions", ())),
        "metric_family": payload.get("metric_family"),
    }
    if spec.feature_id in _FULL_WINDOW_REQUIRED_SLOTS:
        full_window = payload.get("full_window")
        if not isinstance(full_window, Mapping):
            missing_reason = (
                "not_yet_public"
                if visible_count == 0 and raw_count > 0
                else ("absent_source_observation" if raw_count == 0 else "no_visible_reported_period")
            )
            full_window = _missing_full_window(
                _FULL_WINDOW_REQUIRED_SLOTS[spec.feature_id],
                missing_reason,
                annual=spec.feature_id != "quarterly_eps_growth",
            )
        result["full_window"] = dict(full_window)
    return result


def _empty_state(reason: str, minimum_history: int | None) -> dict[str, Any]:
    return {
        "calculable": False,
        "reason": reason,
        "required_history": minimum_history,
        "available_history": 0,
        "selected_period_ends": [],
        "selected_available_from_sessions": [],
        "metric_family": None,
    }


def _fundamental_source_dates(
    records_by_ticker: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, list[tuple[str, str]]]]:
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for ticker, records in records_by_ticker.items():
        feature_dates: dict[str, list[tuple[str, str]]] = {}
        for spec in FEATURE_SPECS:
            if spec.feature_id not in _SOURCE_FIELDS:
                continue
            feature_dates[spec.feature_id] = sorted(
                (str(row["public_date"]), str(row["period_end"]))
                for row in records
                if _source_match(spec.feature_id, row)
            )
        result[ticker] = feature_dates
    return result


def _feature_source_counts(
    dated_rows: Iterable[tuple[str, str]], session: date
) -> tuple[int, int, int, int]:
    rows = tuple(dated_rows)
    dates = [item[0] for item in rows]
    visible = bisect.bisect_right(dates, session.isoformat())
    visible_periods = len({period for _public, period in rows[:visible]})
    future_periods = len({period for _public, period in rows[visible:]})
    return len(rows), visible, len(rows) - visible, visible_periods + future_periods


def _load_bundle(
    bundle_path: Path,
    manifest_path: Path,
    prices_provenance_path: Path,
) -> tuple[PITDataBundle, dict[str, Any], dict[str, str]]:
    manifest = _read_json_object(manifest_path, "bundle manifest")
    provenance = _read_json_object(prices_provenance_path, "prices provenance")
    digest = sha256_file(bundle_path)
    if manifest.get("bundle_sha256") != digest:
        raise ValueError("bundle file digest differs from its recorded manifest")
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("bundle manifest metadata is missing")
    if metadata.get("prices_provenance_sha256") != sha256_file(prices_provenance_path):
        raise ValueError("prices provenance digest differs from the bundle metadata")
    bundle = PITDataBundle(
        bundle_path,
        expected_sha256=digest,
        prices_provenance=prices_provenance_path,
    )
    if bundle.metadata != metadata:
        raise ValueError("bundle manifest metadata differs from the authenticated bundle")
    if provenance.get("bundle_sha256") not in (None, digest):
        raise ValueError("prices provenance names a different bundle")
    identities = {
        "bundle_sha256": digest,
        "bundle_manifest_sha256": sha256_file(manifest_path),
        "prices_provenance_sha256": sha256_file(prices_provenance_path),
    }
    return bundle, manifest, identities


def _price_payload(value: Any, *, required: int, available: int, reason: str | None = None) -> dict[str, Any]:
    calculable = value is not None
    if calculable and not isinstance(value, bool):
        try:
            calculable = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            calculable = False
    return {
        "calculable": bool(calculable),
        "lookback_ready": available >= required,
        "reason": None if calculable else (reason or ("insufficient_history" if available < required else "missing_current_session_price")),
        "required_history": required,
        "available_history": available,
        "selected_period_ends": [],
        "selected_available_from_sessions": [],
        "metric_family": None,
    }


def build_coverage_report(
    *,
    bundle_path: str | Path,
    bundle_manifest_path: str | Path,
    prices_provenance_path: str | Path,
    output_dir: str | Path,
    source_revision: str,
    feature_spec_path: str | Path,
) -> dict[str, Any]:
    """Measure one authenticated bundle and write deterministic report files."""

    if (
        not isinstance(source_revision, str)
        or len(source_revision) != 40
        or any(character not in "0123456789abcdef" for character in source_revision)
    ):
        raise ValueError("source_revision must be a full lowercase Git SHA-1")
    financial_calculator_blob = _verify_committed_source(source_revision)
    bundle_file = Path(bundle_path)
    manifest_file = Path(bundle_manifest_path)
    provenance_file = Path(prices_provenance_path)
    feature_spec_file = Path(feature_spec_path)
    bundle, recorded_manifest, identities = _load_bundle(
        bundle_file, manifest_file, provenance_file
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    evaluation_start = date.fromisoformat(str(bundle.metadata["evaluation_start"]))
    cutoff = bundle.data_cutoff.date()
    warmup_start = date.fromisoformat(str(bundle.metadata["warmup_start"]))
    all_symbols = bundle.price_symbols()
    price_data = bundle.fetch_price_data(
        all_symbols, pd.Timestamp(warmup_start), pd.Timestamp(cutoff)
    )
    closes = pd.DataFrame(
        {ticker: frame["Close"] for ticker, frame in sorted(price_data.items())}
    ).sort_index()
    if "SPY" not in closes:
        raise ValueError("authenticated bundle has no SPY decision-session calendar")
    sessions = pd.DatetimeIndex(
        closes.loc[
            (closes.index.date >= evaluation_start)
            & (closes.index.date <= cutoff),
            "SPY",
        ].dropna().index
    )
    if sessions.empty:
        raise ValueError("authenticated bundle has no evaluation decision sessions")
    metric_data = {
        ticker: _price_metrics(frame, sessions)
        for ticker, frame in price_data.items()
    }
    price_row_counts = _raw_price_counts(price_data)
    source_rows = _source_rows(bundle)
    fundamental_dates = _fundamental_source_dates(source_rows)
    financial_states = _financial_states(
        bundle,
        bundle.tradable_symbols(),
        evaluation_start,
        cutoff,
        source_rows,
    )
    classification_rows = _raw_classification_rows(bundle)
    schema_version = str(bundle.metadata["schema_version"])
    stable_identity_available = schema_version == "3"

    feature_summary: dict[str, dict[str, Any]] = {
        spec.feature_id: {
            "definition": {
                "description": spec.description,
                "calculator": spec.calculator,
                "minimum_history": spec.minimum_history,
                "policy_input": spec.policy_input,
                "declared_policy_consumer_status": spec.declared_consumer_status,
                "consumer_paths": list(spec.consumer_paths),
            },
            "overall": _new_accumulator(),
            "by_year": defaultdict(_new_accumulator),
        }
        for spec in FEATURE_SPECS
    }
    market_years: dict[str, dict[str, Any]] = defaultdict(_new_market_accumulator)
    benchmark_years: dict[str, dict[str, Any]] = defaultdict(_new_benchmark_accumulator)
    identity_counts: Counter[str] = Counter()
    security_unique: set[str] = set()
    member_security_session_count = 0
    row_count = 0
    excluded_nonmember_price_symbols = sorted(
        set(price_data).difference(set(bundle.tradable_symbols()), set(bundle.reference_symbols()))
    )
    missing_price_members = sorted(
        set(bundle.tradable_symbols()).difference(set(price_data))
    )
    refs = list(bundle.reference_symbols())

    rows_path = output / "security_session_coverage.jsonl.gz"
    with rows_path.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_output,
            mtime=0,
        ) as compressed:
            for timestamp in sessions:
                session = timestamp.date()
                members = tuple(sorted(bundle.members_at(session)))
                if not members:
                    continue
                member_security_session_count += len(members)
                year_bucket = market_years[str(session.year)]
                year_bucket["decision_sessions"] += 1
                year_bucket["member_security_session_denominator"] += len(members)
                closes_through = closes.loc[:timestamp]
                rs_scores = calculate_rs_snapshot(
                    closes_through,
                    timestamp,
                    eligible_tickers=members,
                )
                rs_score_symbols = frozenset(rs_scores)
                rs_complete = rs_score_symbols.issuperset(members)
                affiliations = bundle.affiliations_at(session)

                assignments: Mapping[str, Any]
                if schema_version == "2":
                    assignments = load_pit_industry_assignments_as_of(
                        bundle,
                        session=session,
                        symbols=members,
                        allow_schema_v2_development=True,
                    )
                else:
                    assignments = load_pit_industry_assignments_as_of(
                        bundle,
                        session=session,
                        symbols=members,
                    )
                group_by_ticker = {
                    ticker: assignment.group_id
                    for ticker, assignment in assignments.items()
                }
                group_values: dict[str, float | None] = {}
                if set(rs_scores).issuperset(members):
                    for group_id in sorted(set(group_by_ticker.values())):
                        group_values[group_id] = calculate_group_rs(
                            group_id,
                            active_symbols=members,
                            symbol_groups=group_by_ticker,
                            rs_snapshot=rs_scores,
                        )
                valid_50 = 0
                above_50 = 0
                valid_200 = 0
                above_200 = 0
                valid_rs = len(rs_scores)
                for benchmark in ("SPY", "QQQ", "IWM"):
                    metric = metric_data.get(benchmark)
                    ready = False
                    if metric is not None and timestamp in metric.index:
                        item = metric.loc[timestamp]
                        ready = (
                            pd.notna(item.get("close"))
                            and int(item.get("available_bars", 0)) >= 200
                        )
                    benchmark_bucket = benchmark_years[str(session.year)]
                    benchmark_bucket["expected_benchmark_sessions"] += 1
                    benchmark_bucket["by_symbol"][benchmark]["expected_sessions"] += 1
                    if ready:
                        benchmark_bucket["ready_benchmark_sessions"] += 1
                        benchmark_bucket["by_symbol"][benchmark]["ready_sessions"] += 1

                for ticker in members:
                    row_count += 1
                    security_unique.add(ticker)
                    identity_counts["security_sessions"] += 1
                    if stable_identity_available:
                        security_key = bundle.security_lineage_id(ticker)
                        identity_status = "stable_lineage_bound"
                    else:
                        security_key = ticker
                        identity_status = "ticker_only_schema_v2"
                        identity_counts["security_sessions_without_stable_lineage"] += 1

                    state_list = financial_states.get(ticker, ())
                    state_dates = [boundary for boundary, _payload in state_list]
                    boundary_index = bisect.bisect_right(state_dates, session) - 1
                    financial_payloads = (
                        state_list[boundary_index][1]
                        if boundary_index >= 0
                        else {}
                    )
                    dated_feature_rows = fundamental_dates.get(ticker, {})
                    feature_cells: dict[str, dict[str, Any]] = {}
                    price_frame = metric_data.get(ticker)
                    price_item = (
                        price_frame.loc[timestamp]
                        if price_frame is not None and timestamp in price_frame.index
                        else pd.Series(dtype=float)
                    )
                    visible_price_count = int(
                        price_data[ticker].index.searchsorted(timestamp, side="right")
                        if ticker in price_data
                        else 0
                    )

                    for spec in FEATURE_SPECS:
                        feature_id = spec.feature_id
                        if feature_id in _SOURCE_FIELDS:
                            dated_rows = dated_feature_rows.get(feature_id, ())
                            raw_count = len(dated_rows)
                            visible_count = bisect.bisect_right(
                                dated_rows,
                                (session.isoformat(), "\U0010ffff"),
                            )
                            future_count = raw_count - visible_count
                            payload = financial_payloads.get(
                                feature_id,
                                _empty_state("absent_visible_source_value", spec.minimum_history),
                            )
                            payload = dict(payload)
                        elif feature_id == "industry_group_rs":
                            class_rows = classification_rows.get(ticker, ())
                            raw_count = len(class_rows)
                            visible_count = sum(
                                date.fromisoformat(str(item["available_from_session"])) <= session
                                and date.fromisoformat(str(item["effective_date"])) <= session
                                for item in class_rows
                            )
                            future_count = raw_count - visible_count
                            assignment = assignments.get(ticker)
                            group_value = (
                                group_values.get(assignment.group_id)
                                if assignment is not None and rs_complete
                                else None
                            )
                            if assignment is None:
                                reason = "classification_absent" if not raw_count else ("not_yet_public" if not visible_count else "no_effective_classification")
                            elif not rs_complete:
                                reason = "incomplete_active_universe_rs"
                            else:
                                reason = None
                            payload = _price_payload(
                                group_value,
                                required=1,
                                available=int(assignment is not None and rs_complete),
                                reason=reason,
                            )
                            payload["selected_period_ends"] = (
                                [str(assignment.as_of_date)] if assignment is not None else []
                            )
                            payload["selected_available_from_sessions"] = (
                                [str(assignment.as_of_date)] if assignment is not None else []
                            )
                        else:
                            raw_count = price_row_counts.get(ticker, 0)
                            item = price_item
                            visible_count = visible_price_count
                            future_count = max(0, raw_count - visible_count)
                            if feature_id == "relative_strength_score":
                                value = rs_scores.get(ticker)
                                available = int(item.get("available_bars", 0)) if not item.empty and pd.notna(item.get("available_bars")) else 0
                                required = 60
                                reason = (
                                    "missing_current_session_price"
                                    if pd.isna(item.get("close"))
                                    else ("insufficient_history" if available < required else ("incomplete_rs_cross_section" if value is None else None))
                                )
                            else:
                                field = _PRICE_SPEC_METRICS[feature_id]
                                raw_value = item.get(field)
                                available = int(item.get("available_bars", 0)) if not item.empty and pd.notna(item.get("available_bars")) else 0
                                required = int(spec.minimum_history or 1)
                                value = raw_value if pd.notna(raw_value) else None
                                reason = (
                                    "missing_current_session_price"
                                    if pd.isna(item.get("close"))
                                    else ("insufficient_history" if available < required else ("incomplete_rs_cross_section" if feature_id in {"breadth_above_50", "breadth_above_200"} and value is None else None))
                                )
                            payload = _price_payload(
                                value,
                                required=required,
                                available=available,
                                reason=reason,
                            )

                        cell = _cell(
                            spec=spec,
                            raw_count=raw_count,
                            visible_count=visible_count,
                            future_count=future_count,
                            payload=payload,
                        )
                        feature_cells[feature_id] = cell
                        accumulator = feature_summary[feature_id]["overall"]
                        year_accumulator = feature_summary[feature_id]["by_year"][str(session.year)]
                        _accumulate_cell(accumulator, year_accumulator, ticker, cell)
                        if feature_id == "breadth_above_50" and cell["calculation_stage"] == "observed":
                            valid_50 += 1
                            above_50 += int(bool(item.get("above_50")))
                        elif feature_id == "breadth_above_200" and cell["calculation_stage"] == "observed":
                            valid_200 += 1
                            above_200 += int(bool(item.get("above_200")))

                    if ticker not in assignments:
                        identity_counts["members_without_public_dated_industry_assignment_security_sessions"] += 1
                    row = {
                        "decision_session": session.isoformat(),
                        "security_key": security_key,
                        "ticker": ticker,
                        "identity_status": identity_status,
                        "index_affiliations": sorted(affiliations.get(ticker, ())),
                        "denominator_included": True,
                        "features": feature_cells,
                    }
                    compressed.write(
                        (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
                    )
                year_bucket["breadth_50_valid_member_security_sessions"] += valid_50
                year_bucket["breadth_50_above_numerator"] += above_50
                year_bucket["breadth_200_valid_member_security_sessions"] += valid_200
                year_bucket["breadth_200_above_numerator"] += above_200
                year_bucket["rs_eligible_member_security_sessions"] += valid_rs

    # Freeze defaultdicts and attach numerators/rates with their actual denominators.
    years_out: dict[str, dict[str, Any]] = {}
    for year, values in sorted(market_years.items()):
        market_denominator = values["member_security_session_denominator"]
        for window in (50, 200):
            valid = values[f"breadth_{window}_valid_member_security_sessions"]
            above = values[f"breadth_{window}_above_numerator"]
            values[f"breadth_{window}_coverage_fraction"] = _ratio(valid, market_denominator)
            values[f"breadth_{window}_above_fraction_among_calculable"] = _ratio(above, valid)
        values["rs_coverage_fraction"] = _ratio(
            values["rs_eligible_member_security_sessions"], market_denominator
        )
        years_out[year] = values

    for values in feature_summary.values():
        values["overall"] = _finalize_accumulator(values["overall"])
        values["by_year"] = {
            year: _finalize_accumulator(accumulator)
            for year, accumulator in sorted(values["by_year"].items())
        }

    revision_rows = bundle._connection.execute(
        "SELECT COUNT(*) FROM fundamentals f WHERE EXISTS ("
        "SELECT 1 FROM fundamentals prior WHERE prior.ticker=f.ticker "
        "AND prior.statement_type=f.statement_type AND prior.period_end=f.period_end "
        "AND prior.public_date < f.public_date)"
    ).fetchone()[0]
    report = {
        "schema": REPORT_SCHEMA,
        "source_revision": source_revision,
        "feature_contract": "historical-feature-specification-v1",
        "feature_contract_sha256": sha256_file(feature_spec_file),
        "full_window_coverage_contract": {
            "separate_from_latest_value_stages": True,
            "raw_source_row_counts_are_not_matched_growth_slots": True,
            "features": {
                "quarterly_eps_growth": {
                    "required_growth_slots": 4,
                    "readiness": "all four newest fiscal-quarter slots have a valid same-period prior-year comparison",
                },
                "annual_eps_growth": {
                    "required_growth_slots": 3,
                    "level_basis": (
                        "four newest nonmissing annual values from the EPS family "
                        "selected by the existing A evaluator"
                    ),
                    "readiness": "all three adjacent reported-level growth slots are valid",
                },
                "annual_revenue_growth": {
                    "required_growth_slots": 3,
                    "level_basis": "four newest nonmissing reported annual revenue values",
                    "readiness": "all three adjacent reported-level growth slots are valid",
                },
            },
            "annual_fiscal_year_gaps": (
                "Adjacent available annual observations remain the comparison basis; "
                "skipped fiscal years are separately enumerated and do not alter the existing score."
            ),
        },
        "historical_data_format_version": schema_version,
        "optimizer_version": 5,
        "policy_interface_version": 3,
        "evaluation_mode": "development_sp500_v2" if schema_version == "2" else "production_candidate_unaccepted",
        "claim_scope": "limited_development_only" if schema_version == "2" else "not_accepted_as_production",
        "source_identity": {
            **identities,
            "bundle_kind": bundle.metadata.get("bundle_kind"),
            "data_cutoff": bundle.metadata.get("data_cutoff"),
            "evaluation_start": bundle.metadata.get("evaluation_start"),
            "warmup_start": bundle.metadata.get("warmup_start"),
            "membership_source_kind": bundle.metadata.get("membership_source_kind"),
            "membership_source_sha256": bundle.metadata.get("membership_source_sha256"),
            "membership_provenance_sha256": bundle.metadata.get("membership_provenance_sha256"),
            "prices_source_kind": bundle.metadata.get("prices_source_kind"),
            "prices_source_sha256": bundle.metadata.get("prices_source_sha256"),
            "fundamentals_source_kind": bundle.metadata.get("fundamentals_source_kind"),
            "fundamentals_source_sha256": bundle.metadata.get("fundamentals_source_sha256"),
            "fundamentals_provenance_sha256": bundle.metadata.get("fundamentals_provenance_sha256"),
            "feature_spec_sha256": sha256_file(feature_spec_file),
        },
        "denominators": {
            "decision_sessions": len(sessions),
            "decision_session_first": sessions[0].date().isoformat(),
            "decision_session_last": sessions[-1].date().isoformat(),
            "security_sessions": member_security_session_count,
            "unique_member_tickers": len(security_unique),
            "membership_scope": "unique dated S&P membership tickers per SPY decision session",
            "overlapping_index_memberships_deduplicated": schema_version == "3",
            "security_identity_basis": "authenticated stable lineage" if stable_identity_available else "ticker-only; schema V2 has no stable lineage ID",
            "benchmark_symbols_excluded": refs,
            "production_union_denominator_ready": False if schema_version == "2" else None,
        },
        "exclusions_and_gaps": {
            "nonmember_price_symbols_excluded": excluded_nonmember_price_symbols,
            "membership_tickers_without_any_price_rows": missing_price_members,
            "security_sessions_without_stable_lineage": identity_counts["security_sessions_without_stable_lineage"],
            "security_sessions_without_public_dated_industry_assignment": identity_counts["members_without_public_dated_industry_assignment_security_sessions"],
            "industry_data_input_status": "absent_from_schema_v2_development_bundle" if schema_version == "2" else "read_from_schema_v3_industry_group_snapshots",
            "industry_code_status": "implemented_in_current_v3_snapshot_path",
            "sector_rs": "intentionally_not_supported_by_current_feature_contract",
            "foreign_financial_scope": "not_measurable_from_this_bundle_without_domicile_classification; production contract defers foreign financials",
            "annual_revenue_consumer_scope": "in_source/calculation coverage; intentionally not a current baseline score consumer",
            "annual_revenue_basis_limitation": "V2 normalized total_revenue rows do not retain currency/concept/accounting-basis fields per observation; report-only growth is a development diagnostic and not source-metric parity evidence",
            "fundamental_public_date_semantics": "bundle public_date values used as already-normalized first eligible sessions per retained SEC provenance; no second session shift applied",
            "fundamental_revision_rows": int(revision_rows),
            "source_revision_identity_per_row": "bundle hashes bind source; schema V2 does not retain row-level SEC accession/vintage identity in the fundamentals table",
        },
        "calculator_identities": {
            "financial_feature_calculator_id": getattr(
                pit_feature_snapshot_module,
                "FINANCIAL_FEATURE_CALCULATOR_ID",
                "unversioned_current_source",
            ),
            "financial_feature_calculator_source_revision": source_revision,
            "financial_feature_calculator_git_blob": financial_calculator_blob,
            "financial_feature_calculator_source_path": "core/pit_feature_snapshot.py",
            "relative_strength_parameters": {
                "trading_days_per_quarter": settings.TRADING_DAYS_PER_QUARTER,
                "quarter_weights": [settings.RS_Q1_WEIGHT, settings.RS_Q2_WEIGHT, settings.RS_Q3_WEIGHT, settings.RS_Q4_WEIGHT],
                "short_history_fallback_minimum_closes": 60,
                "cross_section_scope": "active dated members for each decision session",
            },
            "financial_semantics_note": "Financial feature calculations use the recorded calculator identity, committed source revision, and canonical module Git blob above; #71 corrected semantics are included in this source revision. Issue acceptance remains a separate decision.",
            "annual_revenue_growth_note": "Report-only formula per #66: latest and immediately preceding reported annual revenue observations; not added to baseline policy.",
        },
        "four_status_assessment": {
            "implementation": "reporter_and_fixed_checks_implemented",
            "required_inputs": "authenticated_limited_development_bundle_measured; production_union_inputs_not_required_for_implementation_and_not_claimed",
            "acceptance_evidence": "development_bundle_measurement_only; production_acceptance_unverified",
            "dependencies": "#66 accepted prerequisite satisfied",
        },
        "policy_consumption_evidence": {
            "actual_policy_consumption": "not_measured_no_policy_replay",
            "declared_consumer_status_and_paths": "FeatureSpec records the current policy interface and known source consumer paths; these are declared code-path mappings, not per-decision observed reads.",
        },
        "market_context_by_year": years_out,
        "benchmarks_by_year": _finalize_benchmarks(benchmark_years),
        "features": feature_summary,
        "outputs": {
            "security_session_coverage": {
                "path": rows_path.name,
                "sha256": sha256_file(rows_path),
                "rows": row_count,
                "media_type": "application/x-ndjson+gzip",
            },
            "summary": {
                "path": "coverage_report.json",
                "sha256_excluded_from_self": True,
                "media_type": "application/json",
            },
        },
    }
    summary_path = output / "coverage_report.json"
    summary_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    report["outputs"]["summary"]["sha256"] = sha256_file(summary_path)
    # The summary self-hash is omitted from its own bytes to avoid recursion.
    return report


def _new_accumulator() -> dict[str, Any]:
    return {
        "denominator_security_sessions": 0,
        "unique_denominator_tickers": set(),
        "source_observed_security_sessions": 0,
        "unique_source_observed_tickers": set(),
        "publication_eligible_security_sessions": 0,
        "unique_publication_eligible_tickers": set(),
        "lookback_ready_security_sessions": 0,
        "unique_lookback_ready_tickers": set(),
        "calculable_security_sessions": 0,
        "unique_calculable_tickers": set(),
        "policy_input_ready_security_sessions": 0,
        "unique_policy_input_ready_tickers": set(),
        "reason_counts": Counter(),
        "unique_tickers_by_reason": defaultdict(set),
        "policy_input_reason_counts": Counter(),
        "unique_tickers_by_policy_input_reason": defaultdict(set),
        "full_window": _new_full_window_accumulator(),
    }


def _new_full_window_accumulator() -> dict[str, Any]:
    return {
        "denominator_security_sessions": 0,
        "ready_security_sessions": 0,
        "incomplete_security_sessions": 0,
        "unique_ready_tickers": set(),
        "unique_incomplete_tickers": set(),
        "required_growth_slots": 0,
        "matched_growth_slots": 0,
        "missing_growth_slots": 0,
        "missing_slot_reason_counts": Counter(),
        "unique_tickers_with_missing_slots": set(),
        "fiscal_year_gap_slots": 0,
        "skipped_fiscal_years": 0,
        "security_sessions_with_skipped_fiscal_years": 0,
        "unique_tickers_with_skipped_fiscal_years": set(),
        "consecutive_fiscal_years_ready_security_sessions": None,
    }


def _accumulate_full_window(
    accumulator: dict[str, Any], ticker: str, full_window: Mapping[str, Any]
) -> None:
    accumulator["denominator_security_sessions"] += 1
    ready = bool(full_window.get("ready"))
    ready_key = "ready_security_sessions" if ready else "incomplete_security_sessions"
    unique_key = "unique_ready_tickers" if ready else "unique_incomplete_tickers"
    accumulator[ready_key] += 1
    accumulator[unique_key].add(ticker)
    accumulator["required_growth_slots"] += int(full_window.get("required_growth_slots", 0))
    accumulator["matched_growth_slots"] += int(full_window.get("matched_growth_slots", 0))
    missing = int(full_window.get("missing_growth_slots", 0))
    accumulator["missing_growth_slots"] += missing
    if missing:
        accumulator["unique_tickers_with_missing_slots"].add(ticker)
        for reason, count in full_window.get("missing_slot_reason_counts", {}).items():
            accumulator["missing_slot_reason_counts"][str(reason)] += int(count)

    fiscal_gaps = full_window.get("skipped_fiscal_year_gaps", ())
    if fiscal_gaps:
        accumulator["fiscal_year_gap_slots"] += len(fiscal_gaps)
        accumulator["skipped_fiscal_years"] += sum(
            int(gap.get("skipped_fiscal_years", 0)) for gap in fiscal_gaps
        )
        accumulator["security_sessions_with_skipped_fiscal_years"] += 1
        accumulator["unique_tickers_with_skipped_fiscal_years"].add(ticker)

    if "consecutive_fiscal_years_ready" in full_window:
        if accumulator["consecutive_fiscal_years_ready_security_sessions"] is None:
            accumulator["consecutive_fiscal_years_ready_security_sessions"] = 0
        if bool(full_window["consecutive_fiscal_years_ready"]):
            accumulator["consecutive_fiscal_years_ready_security_sessions"] += 1


def _accumulate_cell(
    overall: dict[str, Any], year: dict[str, Any], ticker: str, cell: Mapping[str, Any]
) -> None:
    for accumulator in (overall, year):
        accumulator["denominator_security_sessions"] += 1
        accumulator["unique_denominator_tickers"].add(ticker)
        for stage, count_key, unique_key in (
            ("source_stage", "source_observed_security_sessions", "unique_source_observed_tickers"),
            ("publication_stage", "publication_eligible_security_sessions", "unique_publication_eligible_tickers"),
            ("lookback_stage", "lookback_ready_security_sessions", "unique_lookback_ready_tickers"),
            ("calculation_stage", "calculable_security_sessions", "unique_calculable_tickers"),
            ("policy_input_stage", "policy_input_ready_security_sessions", "unique_policy_input_ready_tickers"),
        ):
            if cell[stage] == "observed":
                accumulator[count_key] += 1
                accumulator[unique_key].add(ticker)
        for reason in cell["reason_codes"]:
            accumulator["reason_counts"][reason] += 1
            accumulator["unique_tickers_by_reason"][reason].add(ticker)
        policy_reason = cell["policy_input_reason"]
        if policy_reason is not None:
            accumulator["policy_input_reason_counts"][policy_reason] += 1
            accumulator["unique_tickers_by_policy_input_reason"][policy_reason].add(ticker)
        if isinstance(cell.get("full_window"), Mapping):
            _accumulate_full_window(
                accumulator["full_window"], ticker, cell["full_window"]
            )


def _finalize_accumulator(value: dict[str, Any]) -> dict[str, Any]:
    full_window = value.get("full_window")
    result = {key: item for key, item in value.items() if key != "full_window"}
    for key, item in tuple(result.items()):
        if isinstance(item, set):
            result[key] = len(item)
        elif isinstance(item, Counter):
            result[key] = dict(sorted(item.items()))
        elif isinstance(item, defaultdict):
            result[key] = {
                reason: len(tickers) for reason, tickers in sorted(item.items())
            }
    denominator = result["denominator_security_sessions"]
    for key in (
        "source_observed_security_sessions",
        "publication_eligible_security_sessions",
        "lookback_ready_security_sessions",
        "calculable_security_sessions",
        "policy_input_ready_security_sessions",
    ):
        result[key.replace("_security_sessions", "_fraction")] = _ratio(result[key], denominator)
    if (
        isinstance(full_window, Mapping)
        and int(full_window.get("denominator_security_sessions", 0)) > 0
    ):
        finalized_full_window: dict[str, Any] = {}
        for key, item in full_window.items():
            if isinstance(item, set):
                finalized_full_window[key] = len(item)
            elif isinstance(item, Counter):
                finalized_full_window[key] = dict(sorted(item.items()))
            else:
                finalized_full_window[key] = item
        full_denominator = int(finalized_full_window["denominator_security_sessions"])
        finalized_full_window["ready_fraction"] = _ratio(
            finalized_full_window["ready_security_sessions"], full_denominator
        )
        finalized_full_window["matched_growth_slot_fraction"] = _ratio(
            finalized_full_window["matched_growth_slots"],
            finalized_full_window["required_growth_slots"],
        )
        result["full_window_coverage"] = finalized_full_window
    return result


def _new_market_accumulator() -> dict[str, Any]:
    return {
        "decision_sessions": 0,
        "member_security_session_denominator": 0,
        "breadth_50_valid_member_security_sessions": 0,
        "breadth_50_above_numerator": 0,
        "breadth_200_valid_member_security_sessions": 0,
        "breadth_200_above_numerator": 0,
        "rs_eligible_member_security_sessions": 0,
    }


def _new_benchmark_accumulator() -> dict[str, Any]:
    return {
        "expected_benchmark_sessions": 0,
        "ready_benchmark_sessions": 0,
        "by_symbol": defaultdict(lambda: {"expected_sessions": 0, "ready_sessions": 0}),
    }


def _finalize_benchmarks(values: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for year, bucket in sorted(values.items()):
        expected = int(bucket["expected_benchmark_sessions"])
        by_symbol: dict[str, Any] = {}
        for symbol, counts in sorted(bucket["by_symbol"].items()):
            by_symbol[symbol] = {
                **counts,
                "coverage_fraction": _ratio(counts["ready_sessions"], counts["expected_sessions"]),
            }
        result[year] = {
            "expected_benchmark_sessions": expected,
            "ready_benchmark_sessions": int(bucket["ready_benchmark_sessions"]),
            "coverage_fraction": _ratio(bucket["ready_benchmark_sessions"], expected),
            "by_symbol": by_symbol,
        }
    return result


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return None if denominator == 0 else round(float(numerator) / float(denominator), 12)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--prices-provenance", required=True, type=Path)
    parser.add_argument("--feature-spec", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    arguments = parser.parse_args(argv)
    report = build_coverage_report(
        bundle_path=arguments.bundle,
        bundle_manifest_path=arguments.bundle_manifest,
        prices_provenance_path=arguments.prices_provenance,
        feature_spec_path=arguments.feature_spec,
        output_dir=arguments.output_dir,
        source_revision=arguments.source_revision,
    )
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "source_revision": report["source_revision"],
                "bundle_sha256": report["source_identity"]["bundle_sha256"],
                "decision_sessions": report["denominators"]["decision_sessions"],
                "security_sessions": report["denominators"]["security_sessions"],
                "report_dir": str(arguments.output_dir.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
