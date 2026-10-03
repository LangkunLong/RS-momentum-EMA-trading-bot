"""Build deterministic source and expected-slot evidence for Issue 70 v9.

This module accepts already-extracted in-memory rows. It never opens SEC
archives and does not perform policy or backtest replay.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import io
import json
import platform
import zlib
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from core import pit_coverage
from core.canslim.a_annual_earnings import _calculate_roe
from core.canslim.c_current_earnings import _find_earnings_row
from core.canslim.fiscal_periods import match_fiscal_year_over_year_periods


SOURCE_FIELDS = (
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "common_stock",
    "total_stockholders_equity",
    "shares_outstanding",
)
POLICY_CONSUMERS = {
    "basic_eps",
    "diluted_eps",
    "total_revenue",
    "net_income",
    "total_stockholders_equity",
    "shares_outstanding",
}
CSV_COLUMNS = (
    "record_kind", "slot_id", "origin_id", "origin_role", "ticker", "cik",
    "security_lineage_id", "session_date", "membership_status", "feature_id",
    "slot_metric", "source_metric", "slot_number", "expected_period_end",
    "matched_period_end", "comparison_period_end", "source_period_start",
    "source_period_end", "source_value", "source_form", "source_concept",
    "source_unit", "source_currency", "value_scale_multiplier", "status",
    "missing_reason", "consumer_status", "accession_number", "source_public_date",
    "public_date_basis", "available_from_session", "filed_date",
    "acceptance_datetime", "source_stage", "publication_stage", "lookback_stage",
    "calculation_stage", "policy_input_stage", "actual_policy_consumption",
)
ACTUAL_POLICY_CONSUMPTION = "not_measured_no_policy_replay"


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _decimal_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _tuple_sha256(values: Sequence[Any]) -> str:
    canonical = [None if value is None else str(value) for value in values]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slot_id(
    ticker: str,
    cik: str,
    lineage: str,
    session: date,
    feature: str,
    metric: str,
    slot_number: int,
    expected_end: date | None,
) -> str:
    return _tuple_sha256((
        ticker, cik, lineage, session.isoformat(), feature, metric,
        str(slot_number), None if expected_end is None else expected_end.isoformat(),
    ))


def _base_row(
    *, record_kind: str, ticker: str, cik: str, lineage: str, session: date,
    membership: str, feature: str, metric: str, source_metric: str | None = None,
    slot_number: int = 0, expected_end: date | None = None,
) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    row.update({
        "record_kind": record_kind,
        "ticker": ticker,
        "cik": cik,
        "security_lineage_id": lineage,
        "session_date": session.isoformat(),
        "membership_status": membership,
        "feature_id": feature,
        "slot_metric": metric,
        "source_metric": source_metric or "",
        "slot_number": str(slot_number),
        "expected_period_end": "" if expected_end is None else expected_end.isoformat(),
        "consumer_status": (
            "policy_consumer_defined" if source_metric in POLICY_CONSUMERS
            else "intentionally_not_exposed" if source_metric == "common_stock"
            else "not_measured"
        ),
        "actual_policy_consumption": ACTUAL_POLICY_CONSUMPTION,
    })
    row["slot_id"] = _slot_id(
        ticker, cik, lineage, session, feature, metric, slot_number, expected_end
    )
    return row


def _raw_public_date(detail: Mapping[str, Any]) -> date:
    explicit = detail.get("source_public_date")
    if explicit:
        return _date(explicit)  # type: ignore[return-value]
    acceptance = str(detail.get("acceptance_datetime") or "")
    if acceptance:
        return _date(acceptance)  # type: ignore[return-value]
    filed = detail.get("filed_date")
    if filed:
        return _date(filed)  # type: ignore[return-value]
    raise ValueError("metric origin is missing its own acceptance or filed date")


def _normalized_detail(
    detail: Mapping[str, Any], audit: Any, row: Any, calendar_sessions: Sequence[date],
    *, source_metric: str,
) -> dict[str, Any]:
    inputs = detail.get("inputs")
    nested = inputs[0] if isinstance(inputs, list) and inputs else {}
    raw_date = _raw_public_date(detail if detail.get("source_public_date") else nested or detail)
    available = _date(
        detail.get("public_date")
        or detail.get("available_from")
        or row.public_date
    )
    session_index = bisect.bisect_right(calendar_sessions, raw_date)
    if session_index >= len(calendar_sessions):
        raise ValueError("extracted fact has no strict next session on the adopted calendar")
    expected_available = calendar_sessions[session_index]
    if available != expected_available:
        raise ValueError(
            "extracted fact availability differs from one strict-next-session mapping: "
            f"{raw_date.isoformat()} -> {available} (expected {expected_available})"
        )
    start = detail.get("period_start") or detail.get("q4_residual_period_start")
    end = detail.get("period_end") or detail.get("q4_residual_period_end") or row.period_end
    source_value = detail.get("source_value", getattr(row, source_metric, None))
    return {
        "source_metric": source_metric,
        "source_public_date": raw_date,
        "available_from_session": available,
        "source_period_start": _date(start),
        "source_period_end": _date(end),
        "source_value": source_value,
        "source_form": str(detail.get("form") or audit.form),
        "source_concept": str(detail.get("source_concept") or ""),
        "source_unit": str(detail.get("unit") or ""),
        "source_currency": str(detail.get("currency") or ""),
        "value_scale_multiplier": detail.get("value_scale_multiplier", 1),
        "accession_number": str(detail.get("accession_number") or audit.accession_number),
        "public_date_basis": str(detail.get("public_date_basis") or audit.public_date_basis),
        "filed_date": _date(detail.get("filed_date") or audit.filed_date),
        # Acceptance is owned by the metric origin.  An empty value on a
        # filed-date fallback must not inherit the triggering snapshot's time.
        "acceptance_datetime": str(detail.get("acceptance_datetime") or ""),
        "statement_type": row.statement_type,
        "period_end": row.period_end,
        "fiscal_period": str(detail.get("fiscal_period") or audit.fiscal_period),
        "q4_attribution": str(detail.get("q4_attribution") or ""),
        "inputs": inputs if isinstance(inputs, list) else None,
        "detail": detail,
        "_audit": audit,
    }


def _origin_identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        item["source_metric"], item["accession_number"], item["source_form"],
        item["source_concept"], item["source_unit"], item["source_currency"],
        _decimal_text(item["value_scale_multiplier"]), _decimal_text(item["source_value"]),
        None if item["source_period_start"] is None else item["source_period_start"].isoformat(),
        None if item["source_period_end"] is None else item["source_period_end"].isoformat(),
        item["source_public_date"].isoformat(), item["public_date_basis"],
        item["available_from_session"].isoformat(),
    )


def _vintage_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    acceptance_text = str(item.get("acceptance_datetime") or "")
    if acceptance_text:
        try:
            acceptance = datetime.fromisoformat(acceptance_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("source origin has an invalid acceptance timestamp") from exc
        if acceptance.tzinfo is None:
            acceptance = acceptance.replace(tzinfo=timezone.utc)
    else:
        filed = item.get("filed_date") or item["source_public_date"]
        acceptance = datetime.combine(filed, time.min, tzinfo=timezone.utc)
    direct_q4 = bool(item.get("q4_attribution")) and item.get("q4_attribution") != "derived"
    return (
        item["available_from_session"], acceptance, item["accession_number"],
        direct_q4, item["source_form"],
    )


def _origin_row(
    *, slot_row: Mapping[str, str], item: Mapping[str, Any], role: str,
) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    for key in (
        "ticker", "cik", "security_lineage_id", "session_date", "membership_status",
        "feature_id", "slot_metric", "source_metric", "slot_number", "expected_period_end",
        "status", "consumer_status", "source_stage", "publication_stage", "lookback_stage",
        "calculation_stage", "policy_input_stage", "actual_policy_consumption",
    ):
        row[key] = slot_row.get(key, "")
    row.update({
        "record_kind": "source_origin",
        "slot_id": slot_row["slot_id"],
        "origin_role": role,
        "source_metric": str(item["source_metric"]),
        "source_period_start": "" if item["source_period_start"] is None else item["source_period_start"].isoformat(),
        "source_period_end": "" if item["source_period_end"] is None else item["source_period_end"].isoformat(),
        "source_value": _decimal_text(item["source_value"]) or "",
        "source_form": str(item["source_form"]),
        "source_concept": str(item["source_concept"]),
        "source_unit": str(item["source_unit"]),
        "source_currency": str(item["source_currency"]),
        "value_scale_multiplier": _decimal_text(item["value_scale_multiplier"]) or "",
        "accession_number": str(item["accession_number"]),
        "source_public_date": item["source_public_date"].isoformat(),
        "public_date_basis": str(item["public_date_basis"]),
        "available_from_session": item["available_from_session"].isoformat(),
        "filed_date": "" if item["filed_date"] is None else item["filed_date"].isoformat(),
        "acceptance_datetime": str(item["acceptance_datetime"]),
    })
    row["origin_id"] = _tuple_sha256((
        row["slot_id"], role, row["accession_number"], row["source_form"],
        row["source_metric"], row["source_concept"], row["source_unit"],
        row["source_currency"], row["value_scale_multiplier"], row["source_value"],
        row["source_period_start"] or None, row["source_period_end"] or None,
        row["source_public_date"], row["public_date_basis"], row["available_from_session"],
    ))
    return row


def _feature_stages(
    row: dict[str, str], *, raw_count: int, visible_count: int,
    ready: bool, reason: str | None = None,
) -> None:
    row["source_stage"] = "observed" if raw_count else "absent"
    row["publication_stage"] = (
        "observed" if visible_count else "not_yet_public" if raw_count else "absent"
    )
    unavailable = reason or "insufficient_history"
    if unavailable.startswith("absent"):
        unavailable = "absent"
    elif unavailable.startswith("no_direct_q4"):
        unavailable = "absent"
    elif unavailable.startswith("invalid"):
        unavailable = "invalid"
    elif unavailable == "not_yet_public":
        unavailable = "not_yet_public"
    elif not ready:
        unavailable = "insufficient_history"
    row["lookback_stage"] = "observed" if ready else unavailable
    row["calculation_stage"] = "observed" if ready else unavailable
    if row["consumer_status"] == "intentionally_not_exposed":
        row["policy_input_stage"] = "intentionally_not_exposed"
    else:
        row["policy_input_stage"] = "observed" if ready and row["consumer_status"] == "policy_consumer_defined" else unavailable


def _unique_origins(
    origins: Iterable[Mapping[str, Any]], *, visible_by: date,
) -> list[Mapping[str, Any]]:
    selected: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for item in origins:
        if item["available_from_session"] > visible_by:
            continue
        selected[_origin_identity(item)] = item
    return sorted(
        selected.values(),
        key=lambda item: (
            item["source_period_end"] or date.min,
            item["available_from_session"], item["accession_number"],
            item["source_concept"],
        ),
    )


def _period_selection(
    source_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]], *,
    ticker: str, metric: str, statement: str, session: date,
) -> dict[date, Mapping[str, Any]]:
    by_period: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for item in source_index.get((ticker, statement, metric), ()):
        by_period[item["period_end"]].append(item)
    return {
        period: max(
            (item for item in items if item["available_from_session"] <= session),
            key=_vintage_key,
        )
        for period, items in by_period.items()
        if any(item["available_from_session"] <= session for item in items)
    }


def _series(selected: Mapping[date, Mapping[str, Any]]) -> pd.Series:
    values = {
        pd.Timestamp(period): float(item["source_value"])
        for period, item in selected.items()
        if item["source_value"] is not None
    }
    return pd.Series(values, dtype="float64").sort_index()


def _quarterly_eps_family(
    selected: Mapping[tuple[str, str], Mapping[date, Mapping[str, Any]]],
) -> str | None:
    frame_rows: dict[str, pd.Series] = {}
    for metric, label in (
        ("diluted_eps", "Diluted EPS"),
        ("basic_eps", "Basic EPS"),
        ("net_income", "Net Income"),
    ):
        series = _series(selected[("quarterly", metric)])
        if not series.empty:
            frame_rows[label] = series
    if not frame_rows:
        return None
    chosen_label = _find_earnings_row(pd.DataFrame(frame_rows).T)
    if chosen_label is None:
        return None
    return (
        "diluted_eps" if "diluted" in str(chosen_label).casefold()
        else "basic_eps" if "basic" in str(chosen_label).casefold()
        else "net_income"
    )


def _latest_annual_period_end(
    selected: Mapping[tuple[str, str], Mapping[date, Mapping[str, Any]]],
) -> date | None:
    periods = [period for (statement, _), values in selected.items() if statement == "annual" for period in values]
    return max(periods) if periods else None


def _normalized_q4_basis_items(
    *, chosen: Mapping[str, Any], annual_period: date,
    calendar_sessions: Sequence[date], source_metric: str,
) -> list[dict[str, Any]]:
    basis_inputs = chosen.get("inputs") or []
    roles = ("q4_fy_basis", "q4_q1_basis", "q4_q2_basis", "q4_q3_basis")
    if len(basis_inputs) != len(roles):
        raise ValueError("derived Q4 revenue does not retain all four basis inputs")
    basis_items = [
        _normalized_detail(
            basis,
            chosen["_audit"],
            type("Q4BasisRow", (), {
                "public_date": chosen["available_from_session"],
                "period_end": annual_period,
                "statement_type": "annual",
                source_metric: basis.get("source_value"),
            })(),
            calendar_sessions,
            source_metric=source_metric,
        )
        for basis in basis_inputs
    ]
    first = basis_items[0]
    if len({item["accession_number"] for item in basis_items}) != 1 or any(
        item["accession_number"] != chosen["accession_number"]
        or item["source_concept"] != first["source_concept"]
        or item["source_unit"] != first["source_unit"]
        or item["source_currency"] != first["source_currency"]
        or item["value_scale_multiplier"] != first["value_scale_multiplier"]
        for item in basis_items
    ):
        raise ValueError("derived Q4 basis origins do not share the accepted accounting basis")
    return basis_items


def _slot_row(
    *, ticker: str, cik: str, lineage: str, session: date, membership: str,
    feature: str, metric: str, source_metric: str | None, slot_number: int,
    expected_end: date | None, status: str, reason: str | None,
    raw_count: int, visible_count: int, ready: bool,
    matched_end: date | None = None, comparison_end: date | None = None,
) -> dict[str, str]:
    row = _base_row(
        record_kind="expected_slot", ticker=ticker, cik=cik, lineage=lineage,
        session=session, membership=membership, feature=feature, metric=metric,
        source_metric=source_metric, slot_number=slot_number, expected_end=expected_end,
    )
    row.update({
        "status": status,
        "missing_reason": "" if reason is None else reason,
        "matched_period_end": "" if matched_end is None else matched_end.isoformat(),
        "comparison_period_end": "" if comparison_end is None else comparison_end.isoformat(),
    })
    _feature_stages(row, raw_count=raw_count, visible_count=visible_count, ready=ready, reason=reason)
    return row


def build_v9_coverage_records(
    *, fundamentals: Any, security_rows: Sequence[Any],
    evaluation_sessions: Sequence[date], calendar_sessions: Sequence[date],
    max_records: int = 250_000,
    normalized_origins_out: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build complete field, expected-slot, and source-origin rows in memory."""
    if tuple(sorted(set(calendar_sessions))) != tuple(calendar_sessions):
        raise ValueError("adopted calendar sessions must be strictly increasing and unique")
    if tuple(sorted(set(evaluation_sessions))) != tuple(evaluation_sessions):
        raise ValueError("evaluation sessions must be strictly increasing and unique")
    if any(session not in calendar_sessions for session in evaluation_sessions):
        raise ValueError("evaluation session is not in the adopted calendar")
    if not security_rows or len({row.ticker for row in security_rows}) != len(security_rows):
        raise ValueError("v9 source coverage requires unique security rows")
    if len(fundamentals.rows) != len(fundamentals.audit_rows):
        raise ValueError("fundamental snapshot and audit rows must be paired")

    security_by_ticker = {row.ticker: row for row in security_rows}
    source_index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    raw_facts: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    seen_global: set[tuple[Any, ...]] = set()
    for snapshot, audit in zip(fundamentals.rows, fundamentals.audit_rows, strict=True):
        if snapshot.ticker not in security_by_ticker:
            continue
        metric_sources = json.loads(audit.metric_sources or "{}")
        if not isinstance(metric_sources, dict):
            continue
        for metric in SOURCE_FIELDS:
            if getattr(snapshot, metric) is None:
                continue
            detail = metric_sources.get(metric)
            if not isinstance(detail, dict):
                continue
            item = _normalized_detail(detail, audit, snapshot, calendar_sessions, source_metric=metric)
            if item["inputs"]:
                basis = item["inputs"][0]
                item["source_public_date"] = _raw_public_date(basis)
                item["public_date_basis"] = str(basis.get("public_date_basis") or audit.public_date_basis)
            origin_key = (snapshot.ticker, *_origin_identity(item))
            if origin_key in seen_global:
                continue
            seen_global.add(origin_key)
            item["ticker"] = snapshot.ticker
            source_index[(snapshot.ticker, item["statement_type"], metric)].append(item)
            raw_facts[(snapshot.ticker, metric)].append(item)
            if normalized_origins_out is not None:
                normalized_origins_out.append(item)

    records: list[dict[str, str]] = []
    field_status_counts: Counter[str] = Counter()
    summary_by_year: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    summary_by_issuer: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    eligible_tickers_by_year: dict[str, set[str]] = defaultdict(set)
    eligible_tickers_by_year_metric: dict[tuple[str, str], set[str]] = defaultdict(set)
    summary_by_stage: dict[str, int] = Counter()
    summary_by_consumer: dict[str, int] = Counter()
    feature_status_counts: Counter[str] = Counter()
    missing_reason_counts: Counter[str] = Counter()
    annual_fiscal_year_gap_slots: list[dict[str, Any]] = []

    def append(row: dict[str, str]) -> None:
        records.append(row)
        if row["record_kind"] in {"field_session", "expected_slot"} and row["membership_status"] == "eligible_sample_membership":
            year = row["session_date"][:4]
            issuer_key = row["ticker"]
            if row["record_kind"] == "field_session":
                field_status_counts[f"{row['slot_metric']}:{row['status']}"] += 1
                summary_by_year[year][f"field:{row['slot_metric']}:denominator"] += 1
                summary_by_year[year][f"field:{row['slot_metric']}:{row['status']}"] += 1
                summary_by_year[year][f"field:{row['slot_metric']}:source:{row['source_stage']}"] += 1
                summary_by_year[year][f"field:{row['slot_metric']}:publication:{row['publication_stage']}"] += 1
                eligible_tickers_by_year_metric[(year, row["slot_metric"])].add(issuer_key)
            else:
                feature_key = f"{row['feature_id']}:{row['slot_metric']}:{row['status']}"
                feature_status_counts[feature_key] += 1
                summary_by_year[year][f"slot:{row['feature_id']}:{row['slot_metric']}:denominator"] += 1
                summary_by_year[year][f"slot:{row['feature_id']}:{row['slot_metric']}:{row['status']}"] += 1
            summary_by_issuer[issuer_key][f"{row['record_kind']}:{row['slot_metric']}:{row['status']}"] += 1
            for stage_name in (
                "source_stage", "publication_stage", "lookback_stage",
                "calculation_stage", "policy_input_stage",
            ):
                summary_by_stage[f"{stage_name}:{row[stage_name]}"] += 1
                summary_by_year[year][f"{row['record_kind']}:{stage_name}:{row[stage_name]}"] += 1
            summary_by_consumer[row["consumer_status"]] += 1
            summary_by_year[year][f"consumer:{row['consumer_status']}"] += 1
            eligible_tickers_by_year[year].add(issuer_key)
            if row["status"] in {"missing", "unavailable"} and row["missing_reason"]:
                missing_reason_counts[row["missing_reason"]] += 1
        if len(records) > max_records:
            raise ValueError("v9 field, slot, and origin records exceed the 250,000-row cap")

    for ticker in sorted(security_by_ticker):
        security = security_by_ticker[ticker]
        cik = str(security.cik)
        lineage = f"sample-cik:{cik}"
        for session in evaluation_sessions:
            eligible = security.first_membership_date <= session <= security.last_membership_date
            membership = "eligible_sample_membership" if eligible else "pre_membership_or_outside_sample"
            for metric in SOURCE_FIELDS:
                all_origins = raw_facts.get((ticker, metric), ())
                visible_origins = _unique_origins(all_origins, visible_by=session)
                future_origins = [item for item in all_origins if item["available_from_session"] > session]
                if eligible:
                    summary_by_year[str(session.year)][f"field:{metric}:raw_fact_candidate_count"] += len(all_origins)
                    summary_by_year[str(session.year)][f"field:{metric}:visible_origin_count"] += len(visible_origins)
                row = _base_row(
                    record_kind="field_session", ticker=ticker, cik=cik, lineage=lineage,
                    session=session, membership=membership, feature="raw_field_coverage",
                    metric=metric, source_metric=metric, slot_number=0, expected_end=None,
                )
                row["status"] = "observed" if visible_origins else "not_yet_public" if future_origins else "absent"
                row["missing_reason"] = "" if visible_origins else row["status"]
                _feature_stages(
                    row, raw_count=len(all_origins), visible_count=len(visible_origins),
                    ready=bool(visible_origins), reason=row["status"],
                )
                append(row)
                for item in visible_origins:
                    append(_origin_row(slot_row=row, item=item, role="field_origin"))

            if not eligible:
                continue

            # Build visible per-period source series once per metric and statement.
            selected = {
                (statement, metric): _period_selection(
                    source_index, ticker=ticker, metric=metric, statement=statement, session=session
                )
                for statement in ("quarterly", "annual")
                for metric in SOURCE_FIELDS
            }

            # Quarterly EPS family and its four fixed YoY slots use accepted calculator semantics.
            family = _quarterly_eps_family(selected)
            q_series = None if family is None else _series(selected[("quarterly", family)])
            latest_annual_end = _latest_annual_period_end(selected)
            q_window = pit_coverage._quarterly_eps_full_window(
                q_series, 4, latest_visible_fiscal_period_end=latest_annual_end
            )
            family_origins = selected[("quarterly", family)] if family else {}
            for slot in q_window["slots"]:
                current = _date(slot.get("current_period_end"))
                comparison = _date(slot.get("comparison_period_end"))
                reason = slot.get("reason")
                row = _slot_row(
                    ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                    feature="quarterly_eps_growth", metric="quarterly_eps_growth",
                    source_metric=family, slot_number=int(slot["slot"]), expected_end=current,
                    matched_end=current, comparison_end=comparison,
                    status="matched" if slot["status"] == "matched" else "missing",
                    reason=reason, raw_count=len(raw_facts.get((ticker, family or ""), ())),
                    visible_count=len(family_origins), ready=slot["status"] == "matched",
                )
                append(row)
                for period, role in ((current, "current_period"), (comparison, "comparison_period")):
                    origin = family_origins.get(period) if period else None
                    if origin is not None:
                        append(_origin_row(slot_row=row, item=origin, role=role))

            # Quarterly revenue uses the same newest-first fiscal-year matcher, retaining unmatched slots.
            revenue_selected = selected[("quarterly", "total_revenue")]
            revenue_series = _series(revenue_selected)
            revenue_window = pit_coverage._quarterly_eps_full_window(
                revenue_series if not revenue_series.empty else None, 2
            )
            revenue_matches = (
                match_fiscal_year_over_year_periods(revenue_series)
                if not revenue_series.empty else ()
            )
            revenue_match_by_current = {match.current_period: match for match in revenue_matches}
            for slot in revenue_window["slots"]:
                current = _date(slot.get("current_period_end"))
                comparison = _date(slot.get("comparison_period_end"))
                reason = slot.get("reason")
                if current is not None and current in revenue_match_by_current:
                    matched_pair = revenue_match_by_current[current]
                    if matched_pair.prior_period != comparison:
                        raise ValueError("quarterly revenue slot differs from accepted fiscal-year matcher")
                elif slot["status"] == "matched":
                    raise ValueError("quarterly revenue slot has no accepted fiscal-year match")
                row = _slot_row(
                        ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                        feature="quarterly_revenue_growth", metric="quarterly_revenue_growth",
                        source_metric="total_revenue", slot_number=int(slot["slot"]), expected_end=current,
                    matched_end=current if slot["status"] == "matched" else None,
                    comparison_end=comparison,
                    status="matched" if slot["status"] == "matched" else "missing", reason=reason,
                    raw_count=len(raw_facts.get((ticker, "total_revenue"), ())),
                    visible_count=len(revenue_selected), ready=slot["status"] == "matched",
                )
                append(row)
                for period, role in ((current, "current_period"), (comparison, "comparison_period")):
                    origin = revenue_selected.get(period) if period else None
                    if origin is not None:
                        append(_origin_row(slot_row=row, item=origin, role=role))

            # Annual EPS growth (three slots) and scalar levels (four slots) per normalized EPS field.
            for metric in ("basic_eps", "diluted_eps"):
                annual_selected = selected[("annual", metric)]
                annual_series = _series(annual_selected)
                growth = pit_coverage._annual_growth_full_window(annual_series if not annual_series.empty else None, 3)
                for slot in growth["slots"]:
                    current = _date(slot.get("current_period_end"))
                    comparison = _date(slot.get("comparison_period_end"))
                    reason = slot.get("reason")
                    slot_matched = slot["status"] == "matched"
                    row = _slot_row(
                        ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                        feature="annual_eps_growth", metric=f"annual_{metric}_growth",
                        source_metric=metric, slot_number=int(slot["slot"]), expected_end=current,
                        matched_end=current,
                        comparison_end=comparison,
                        status="matched" if slot_matched else "missing",
                        reason=reason, raw_count=len(raw_facts.get((ticker, metric), ())),
                        visible_count=len(annual_selected), ready=slot_matched,
                    )
                    append(row)
                    if slot.get("skipped_fiscal_years", 0):
                        annual_fiscal_year_gap_slots.append({
                            "slot_id": row["slot_id"],
                            "ticker": ticker,
                            "session_date": session.isoformat(),
                            "feature_id": "annual_eps_growth",
                            "slot_metric": f"annual_{metric}_growth",
                            "slot_number": int(slot["slot"]),
                            "current_period_end": None if current is None else current.isoformat(),
                            "comparison_period_end": None if comparison is None else comparison.isoformat(),
                            "skipped_fiscal_years": int(slot["skipped_fiscal_years"]),
                        })
                    for period, role in ((current, "current_period"), (comparison, "comparison_period")):
                        origin = annual_selected.get(period) if period else None
                        if origin is not None:
                            append(_origin_row(slot_row=row, item=origin, role=role))
                visible_periods = sorted(annual_selected, reverse=True)
                for slot_number in range(1, 5):
                    period = visible_periods[slot_number - 1] if len(visible_periods) >= slot_number else None
                    origin = annual_selected.get(period) if period else None
                    reason = None if origin is not None else "insufficient_reported_periods"
                    row = _slot_row(
                        ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                        feature="annual_eps_level", metric=f"annual_{metric}_level",
                        source_metric=metric, slot_number=slot_number, expected_end=period,
                        matched_end=period, comparison_end=None,
                        status="observed" if origin is not None else "missing", reason=reason,
                        raw_count=len(raw_facts.get((ticker, metric), ())),
                        visible_count=len(annual_selected), ready=origin is not None,
                    )
                    append(row)
                    if origin is not None:
                        append(_origin_row(slot_row=row, item=origin, role="annual_level"))

            # Annual revenue retains three accepted growth-window pairs.
            annual_revenue = selected[("annual", "total_revenue")]
            annual_revenue_series = _series(annual_revenue)
            annual_window = pit_coverage._annual_growth_full_window(
                annual_revenue_series if not annual_revenue_series.empty else None,
                3,
                reject_near_zero_prior=False,
            )
            for slot in annual_window["slots"]:
                current = _date(slot.get("current_period_end"))
                comparison = _date(slot.get("comparison_period_end"))
                reason = slot.get("reason")
                slot_matched = slot["status"] == "matched"
                row = _slot_row(
                    ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                    feature="annual_revenue_growth", metric="annual_revenue_growth",
                    source_metric="total_revenue", slot_number=int(slot["slot"]), expected_end=current,
                    matched_end=current,
                    comparison_end=comparison,
                    status="matched" if slot_matched else "missing",
                    reason=reason, raw_count=len(raw_facts.get((ticker, "total_revenue"), ())),
                    visible_count=len(annual_revenue), ready=slot_matched,
                )
                append(row)
                if slot.get("skipped_fiscal_years", 0):
                    annual_fiscal_year_gap_slots.append({
                        "slot_id": row["slot_id"],
                        "ticker": ticker,
                        "session_date": session.isoformat(),
                        "feature_id": "annual_revenue_growth",
                        "slot_metric": "annual_revenue_growth",
                        "slot_number": int(slot["slot"]),
                        "current_period_end": None if current is None else current.isoformat(),
                        "comparison_period_end": None if comparison is None else comparison.isoformat(),
                        "skipped_fiscal_years": int(slot["skipped_fiscal_years"]),
                    })
                for period, role in ((current, "current_period"), (comparison, "comparison_period")):
                    origin = annual_revenue.get(period) if period else None
                    if origin is not None:
                        append(_origin_row(slot_row=row, item=origin, role=role))

            # Latest annual ROE delegates its positive-equity and vintage semantics to the accepted calculator.
            annual_ni = selected[("annual", "net_income")]
            annual_equity = selected[("annual", "total_stockholders_equity")]
            ni_series = _series(annual_ni)
            equity_series = _series(annual_equity)
            income_frame = pd.DataFrame([ni_series], index=["Net Income"])
            balance_frame = pd.DataFrame([equity_series], index=["Total Stockholders Equity"])
            roe_value = _calculate_roe(income_frame, balance_frame) if not ni_series.empty and not equity_series.empty else None
            ni_period = None if ni_series.empty else pd.Timestamp(ni_series.index[-1]).date()
            equity_period = None if equity_series.empty else pd.Timestamp(equity_series.index[-1]).date()
            roe_reason = (
                None if roe_value is not None
                else "absent_annual_net_income" if ni_series.empty
                else "absent_stockholders_equity" if equity_series.empty
                else "invalid_nonpositive_equity_or_roe"
            )
            roe_row = _slot_row(
                ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                feature="annual_roe", metric="annual_roe", source_metric="net_income",
                slot_number=1, expected_end=ni_period, matched_end=ni_period,
                comparison_end=equity_period,
                status="observed" if roe_value is not None else "missing", reason=roe_reason,
                raw_count=len(raw_facts.get((ticker, "net_income"), ())) + len(raw_facts.get((ticker, "total_stockholders_equity"), ())),
                visible_count=len(annual_ni) + len(annual_equity), ready=roe_value is not None,
            )
            append(roe_row)
            if ni_period in annual_ni:
                append(_origin_row(slot_row=roe_row, item=annual_ni[ni_period], role="roe_net_income"))
            if equity_period in annual_equity:
                append(_origin_row(slot_row=roe_row, item=annual_equity[equity_period], role="roe_equity"))

            # Q4 coverage is retained for every annual period in the sample source, with typed status.
            annual_periods = sorted({
                item["period_end"] for metric in SOURCE_FIELDS
                for item in source_index.get((ticker, "annual", metric), ())
            })
            for annual_period in annual_periods:
                annual_public = any(
                    item["period_end"] == annual_period and item["available_from_session"] <= session
                    for metric in SOURCE_FIELDS
                    for item in source_index.get((ticker, "annual", metric), ())
                )
                for metric in ("basic_eps", "diluted_eps", "total_revenue"):
                    q4_candidates = [
                        item for item in source_index.get((ticker, "quarterly", metric), ())
                        if item["period_end"] == annual_period
                    ]
                    q4_visible = [item for item in q4_candidates if item["available_from_session"] <= session]
                    chosen = max(q4_visible, key=_vintage_key) if q4_visible else None
                    attribution = ""
                    if chosen is not None:
                        attribution = "derived" if chosen["q4_attribution"] == "derived" else "direct"
                        status = attribution
                        reason = None
                    elif annual_public:
                        status = "unavailable"
                        reason = (
                            "no_direct_q4_or_reconciled_revenue_basis"
                            if metric == "total_revenue"
                            else "no_direct_q4_fact"
                        )
                    else:
                        status = "unavailable"
                        reason = "not_yet_public"
                    source_metric = metric
                    q4_row = _slot_row(
                        ticker=ticker, cik=cik, lineage=lineage, session=session, membership=membership,
                        feature="q4_availability", metric=f"q4_{metric}", source_metric=source_metric,
                        slot_number=0, expected_end=annual_period,
                        matched_end=annual_period if chosen is not None else None,
                        comparison_end=None, status=status, reason=reason,
                        raw_count=len(q4_candidates) + int(not q4_candidates),
                        visible_count=len(q4_visible) + int(annual_public and not q4_visible),
                        ready=chosen is not None,
                    )
                    append(q4_row)
                    if chosen is not None:
                        if attribution == "derived":
                            roles = (
                                "q4_fy_basis", "q4_q1_basis", "q4_q2_basis", "q4_q3_basis"
                            )
                            normalized_basis = _normalized_q4_basis_items(
                                chosen=chosen,
                                annual_period=annual_period,
                                calendar_sessions=calendar_sessions,
                                source_metric=metric,
                            )
                            for role, basis_item in zip(
                                roles,
                                normalized_basis,
                                strict=True,
                            ):
                                append(_origin_row(slot_row=q4_row, item=basis_item, role=role))
                        else:
                            append(_origin_row(slot_row=q4_row, item=chosen, role="q4_direct"))

            summary_by_year[str(session.year)]["eligible_security_session_count"] += 1

    records.sort(key=lambda row: (
        row["session_date"], row["ticker"], row["record_kind"], row["feature_id"],
        row["slot_metric"], row["slot_number"], row["expected_period_end"],
        row["slot_id"], row["origin_role"], row["origin_id"],
    ))
    eligible_security_sessions = sum(
        security.first_membership_date <= session <= security.last_membership_date
        for security in security_rows for session in evaluation_sessions
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "claim_scope": (
            "four_issuer_development_sample_only"
            if len(security_rows) == 4
            else "bounded_source_fixture_only"
        ),
        "security_lineage_basis": "provisional_sample_cik_not_accepted_issue_68_lineage",
        "actual_policy_consumption": ACTUAL_POLICY_CONSUMPTION,
        "evaluation_session_count": len(evaluation_sessions),
        "maximum_issuer_session_grid_count": len(security_rows) * len(evaluation_sessions),
        "eligible_security_session_count": eligible_security_sessions,
        "field_session_denominator_count": sum(field_status_counts.values()),
        "field_status_counts": dict(sorted(field_status_counts.items())),
        "feature_status_counts": dict(sorted(feature_status_counts.items())),
        "missing_reason_counts": dict(sorted(missing_reason_counts.items())),
        "annual_fiscal_year_gap_slot_count": len(annual_fiscal_year_gap_slots),
        "annual_fiscal_year_gap_slots": sorted(
            annual_fiscal_year_gap_slots,
            key=lambda item: (
                item["session_date"], item["ticker"], item["feature_id"],
                item["slot_metric"], item["slot_number"], item["slot_id"],
            ),
        ),
        "stage_counts": dict(sorted(summary_by_stage.items())),
        "consumer_status_counts": dict(sorted(summary_by_consumer.items())),
        "by_issuer": {
            ticker: dict(sorted(counts.items()))
            for ticker, counts in sorted(summary_by_issuer.items())
        },
        "unique_eligible_tickers_by_evaluation_year": {
            year: len(tickers) for year, tickers in sorted(eligible_tickers_by_year.items())
        },
        "unique_eligible_tickers_by_evaluation_year_and_field": {
            f"{year}:{metric}": len(tickers)
            for (year, metric), tickers in sorted(eligible_tickers_by_year_metric.items())
        },
        "by_index": "not_measured_without_issue_68_membership_inputs",
        "by_evaluation_year": {
            year: dict(sorted(counts.items())) for year, counts in sorted(summary_by_year.items())
        },
        "record_counts": dict(sorted(Counter(row["record_kind"] for row in records).items())),
        "total_record_count": len(records),
        "all_expected_slots_retained": True,
        "pre_membership_rows_are_excluded_from_denominators": True,
    }
    return records, summary


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _csv_origin_identity(row: Mapping[str, str]) -> tuple[Any, ...]:
    return (
        row["source_metric"], row["accession_number"], row["source_form"],
        row["source_concept"], row["source_unit"], row["source_currency"],
        _decimal_text(row["value_scale_multiplier"]), _decimal_text(row["source_value"]),
        row["source_period_start"] or None, row["source_period_end"] or None,
        row["source_public_date"], row["public_date_basis"], row["available_from_session"],
    )


def _origin_reference_index(
    records: Sequence[Mapping[str, str]],
) -> dict[tuple[Any, ...], str]:
    candidates: dict[tuple[Any, ...], list[tuple[int, str, str]]] = defaultdict(list)
    for row in records:
        if row["record_kind"] != "source_origin" or not row["origin_id"]:
            continue
        key = (row["ticker"], *_csv_origin_identity(row))
        priority = 0 if row["origin_role"] == "field_origin" else 1
        candidates[key].append((priority, row["session_date"], row["origin_id"]))
    return {
        key: sorted(refs)[0][2]
        for key, refs in candidates.items()
    }


def _origin_reference(
    item: Mapping[str, Any], references: Mapping[tuple[Any, ...], str],
) -> str:
    key = (str(item["ticker"]), *_origin_identity(item))
    reference = references.get(key)
    if reference is None:
        raise ValueError("comparison candidate has no origin reference in frozen v9 detail")
    return reference


def _source_window_decision(
    item: Mapping[str, Any], *, start: date, end: date,
) -> str:
    raw_date = item["source_public_date"]
    if raw_date < start:
        return "source_public_date_before_v8_window"
    if raw_date > end:
        return "source_public_date_after_v8_window"
    if item.get("q4_attribution") == "derived" and item.get("inputs"):
        basis_dates = [_raw_public_date(basis) for basis in item["inputs"]]
        if any(basis_date < start or basis_date > end for basis_date in basis_dates):
            return "derived_q4_basis_outside_v8_window"
    return "admitted_to_v8_source_window"


def _selected_origins_for_window(
    *, ticker: str, selected: Mapping[tuple[str, str], Mapping[date, Mapping[str, Any]]],
    feature_id: str, row: Mapping[str, str], session: date,
    calendar_sessions: Sequence[date],
    all_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    source_index: Mapping[tuple[str, str, str], Sequence[Mapping[str, Any]]],
    origins_by_period: Mapping[tuple[str, str, str, date], Sequence[Mapping[str, Any]]],
    source_window_start: date, source_window_end: date,
) -> dict[str, Any]:
    target = _date(row["expected_period_end"])
    slot_number = int(row["slot_number"] or 0)
    feature = feature_id
    result: dict[str, Any] = {
        "status": "missing",
        "missing_reason": "absent_source_observation",
        "source_metric": row["source_metric"] or None,
        "expected_period_end": target,
        "matched_period_end": None,
        "comparison_period_end": None,
        "selected_items": [],
        "family": None,
    }

    def unavailable_reason(metric_names: Sequence[str], statements: Sequence[str]) -> str:
        candidates = (
            [
                item
                for metric in metric_names
                for statement in statements
                for item in all_index.get((ticker, statement, metric), ())
            ]
            if target is None
            else [
                item
                for metric in metric_names
                for statement in statements
                for item in origins_by_period.get((ticker, statement, metric, target), ())
            ]
        )
        if any(
            _source_window_decision(item, start=date(2020, 1, 1), end=date(2025, 12, 31))
            == "admitted_to_v8_source_window"
            and item["available_from_session"] > session
            for item in candidates
        ):
            return "not_yet_public"
        if any(
            _source_window_decision(item, start=date(2020, 1, 1), end=date(2025, 12, 31))
            != "admitted_to_v8_source_window"
            for item in candidates
        ):
            return "source_window_excluded_observation"
        return "absent_source_observation"

    if feature == "quarterly_eps_growth":
        family = _quarterly_eps_family(selected)
        result["family"] = family
        result["source_metric"] = family
        if family is None:
            result["missing_reason"] = unavailable_reason(
                ("diluted_eps", "basic_eps", "net_income"), ("quarterly",)
            )
            return result
        periods = selected[("quarterly", family)]
        latest_annual_end = _latest_annual_period_end(selected)
        visible = periods if target is None else {period: item for period, item in periods.items() if period <= target}
        series = _series(visible)
        window = pit_coverage._quarterly_eps_full_window(
            series if not series.empty else None,
            4,
            latest_visible_fiscal_period_end=latest_annual_end,
        )
        slot = (
            next((item for item in window["slots"] if item.get("current_period_end") == target.isoformat()), None)
            if target is not None
            else window["slots"][slot_number - 1]
        )
        if slot is None:
            result["missing_reason"] = unavailable_reason((family,), ("quarterly",))
            return result
        result.update({
            "status": "matched" if slot["status"] == "matched" else "missing",
            "missing_reason": slot.get("reason") or "",
            "matched_period_end": _date(slot.get("current_period_end")),
            "comparison_period_end": _date(slot.get("comparison_period_end")),
        })
        if target is not None and target in periods and result["matched_period_end"] is None:
            result["matched_period_end"] = target
        for period in (result["matched_period_end"], result["comparison_period_end"]):
            if period in periods:
                result["selected_items"].append(periods[period])
        return result

    if feature == "quarterly_revenue_growth":
        periods = selected[("quarterly", "total_revenue")]
        visible = periods if target is None else {period: item for period, item in periods.items() if period <= target}
        series = _series(visible)
        window = pit_coverage._quarterly_eps_full_window(
            series if not series.empty else None, 2
        )
        slot = (
            next((item for item in window["slots"] if item.get("current_period_end") == target.isoformat()), None)
            if target is not None
            else window["slots"][slot_number - 1]
        )
        if slot is None:
            result["missing_reason"] = unavailable_reason(("total_revenue",), ("quarterly",))
            return result
        current_period = _date(slot.get("current_period_end"))
        comparison_period = _date(slot.get("comparison_period_end"))
        matches = match_fiscal_year_over_year_periods(series) if not series.empty else ()
        matcher = next((match for match in matches if match.current_period == (target or current_period)), None)
        if slot["status"] == "matched":
            if matcher is None:
                raise ValueError("v8 source-window revenue slot lacks accepted fiscal-year match")
            if matcher.prior_period != comparison_period:
                raise ValueError("v8 source-window revenue slot differs from accepted fiscal-year matcher")
        result.update({
            "status": "matched" if slot["status"] == "matched" else "missing",
            "missing_reason": slot.get("reason") or "",
            "matched_period_end": current_period,
            "comparison_period_end": comparison_period,
        })
        if target is not None and target in periods and result["matched_period_end"] is None:
            result["matched_period_end"] = target
        for period in (result["matched_period_end"], result["comparison_period_end"]):
            if period in periods:
                result["selected_items"].append(periods[period])
        return result

    if feature in {"annual_eps_growth", "annual_revenue_growth"}:
        metric = row["source_metric"]
        periods = selected[("annual", metric)]
        reject_near_zero = feature != "annual_revenue_growth"
        visible = periods if target is None else {period: item for period, item in periods.items() if period <= target}
        series = _series(visible)
        window = pit_coverage._annual_growth_full_window(
            series if not series.empty else None,
            3 if target is None else 1,
            reject_near_zero_prior=reject_near_zero,
        )
        slot = (
            next((item for item in window["slots"] if item.get("current_period_end") == target.isoformat()), None)
            if target is not None
            else window["slots"][slot_number - 1]
        )
        if slot is None and target is not None and target in periods:
            slot = window["slots"][0]
        if slot is None:
            result["missing_reason"] = unavailable_reason((metric,), ("annual",))
            return result
        current_period = _date(slot.get("current_period_end"))
        comparison_period = _date(slot.get("comparison_period_end"))
        if target is not None and target in periods and current_period is None:
            current_period = target
        result.update({
            "status": "matched" if slot["status"] == "matched" else "missing",
            "missing_reason": slot.get("reason") or "",
            "matched_period_end": current_period,
            "comparison_period_end": comparison_period,
        })
        for period in (result["matched_period_end"], result["comparison_period_end"]):
            if period in periods:
                result["selected_items"].append(periods[period])
        return result

    if feature == "annual_eps_level":
        metric = row["source_metric"]
        periods = selected[("annual", metric)]
        item = periods.get(target) if target is not None else None
        if item is not None:
            result.update({"status": "observed", "missing_reason": "", "matched_period_end": target, "selected_items": [item]})
        elif target is None:
            result["missing_reason"] = "insufficient_reported_periods"
        else:
            result["missing_reason"] = unavailable_reason((metric,), ("annual",))
        return result

    if feature == "annual_roe":
        income = selected[("annual", "net_income")]
        equity = selected[("annual", "total_stockholders_equity")]
        income_series = _series(income)
        equity_series = _series(equity)
        income_frame = pd.DataFrame([income_series], index=["Net Income"])
        balance_frame = pd.DataFrame([equity_series], index=["Total Stockholders Equity"])
        roe = _calculate_roe(income_frame, balance_frame) if not income_series.empty and not equity_series.empty else None
        income_period = None if income_series.empty else pd.Timestamp(income_series.index[-1]).date()
        equity_period = None if equity_series.empty else pd.Timestamp(equity_series.index[-1]).date()
        reason = (
            None if roe is not None
            else "absent_annual_net_income" if income_series.empty
            else "absent_stockholders_equity" if equity_series.empty
            else "invalid_nonpositive_equity_or_roe"
        )
        result.update({
            "status": "observed" if roe is not None else "missing",
            "missing_reason": reason or "",
            "expected_period_end": income_period,
            "matched_period_end": income_period,
            "comparison_period_end": equity_period,
            "selected_items": [
                item for item in (income.get(income_period), equity.get(equity_period))
                if item is not None
            ],
        })
        return result

    if feature == "q4_availability":
        metric = row["source_metric"]
        if target is None:
            result["status"] = "unavailable"
            result["missing_reason"] = "no_expected_annual_period"
            return result
        annual_public = any(
            item["period_end"] == target and item["available_from_session"] <= session
            for (source_ticker, statement, _), items in source_index.items()
            if source_ticker == ticker and statement == "annual"
            for item in items
        )
        q4_candidates = [
            item for item in source_index.get((ticker, "quarterly", metric), ())
            if item["period_end"] == target
        ]
        visible_q4 = [item for item in q4_candidates if item["available_from_session"] <= session]
        chosen = max(visible_q4, key=_vintage_key) if visible_q4 else None
        if chosen is not None:
            attribution = "derived" if chosen["q4_attribution"] == "derived" else "direct"
            selected_items = (
                _normalized_q4_basis_items(
                    chosen=chosen,
                    annual_period=target,
                    calendar_sessions=calendar_sessions,
                    source_metric=metric,
                )
                if attribution == "derived"
                else [chosen]
            )
            for selected_item in selected_items:
                selected_item["ticker"] = ticker
            result.update({
                "status": attribution,
                "missing_reason": "",
                "matched_period_end": target,
                "selected_items": selected_items,
            })
        else:
            source_candidates = [
                *all_index.get((ticker, "annual", metric), ()),
                *all_index.get((ticker, "quarterly", metric), ()),
            ]
            excluded_by_window = any(
                _source_window_decision(
                    item, start=source_window_start, end=source_window_end
                ) != "admitted_to_v8_source_window"
                for item in source_candidates
            )
            result.update({
                "status": "unavailable",
                "missing_reason": (
                    "no_direct_q4_or_reconciled_revenue_basis"
                    if annual_public and metric == "total_revenue"
                    else "no_direct_q4_fact" if annual_public
                    else "source_window_excluded_observation" if excluded_by_window
                    else "not_yet_public"
                ),
            })
        return result

    raise ValueError(f"v8 source-window projection does not support frozen feature key: {feature}")


def _slot_origin_candidates(
    row: Mapping[str, str], *,
    origin_by_identity: Mapping[tuple[Any, ...], Mapping[str, Any]],
    origins_by_period: Mapping[tuple[str, str, str, date], Sequence[Mapping[str, Any]]],
    origins_by_source_end: Mapping[tuple[str, str, str], Mapping[date, Sequence[Mapping[str, Any]]]],
    projection: Mapping[str, Any], current_slot_origins: Sequence[Mapping[str, str]],
    calendar_sessions: Sequence[date],
) -> list[Mapping[str, Any]]:
    ticker = row["ticker"]
    feature = row["feature_id"]
    expected = _date(row["expected_period_end"])
    comparison = _date(row["comparison_period_end"])
    periods = {period for period in (expected, comparison) if period is not None}
    metrics = [row["source_metric"]] if row["source_metric"] else []
    statements = {"annual", "quarterly"} if feature == "q4_availability" else {
        "quarterly" if feature in {"quarterly_eps_growth", "quarterly_revenue_growth"} else "annual"
    }
    if feature == "quarterly_eps_growth":
        metrics = ["diluted_eps", "basic_eps", "net_income"]
    elif feature == "annual_roe":
        metrics = ["net_income", "total_stockholders_equity"]

    candidates: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for metric in metrics:
        for statement in statements:
            for period in periods:
                for item in origins_by_period.get((ticker, statement, metric, period), ()):
                    candidates[(ticker, *_origin_identity(item))] = item

    if feature == "q4_availability" and expected is not None:
        metric = str(row["source_metric"])
        q4_parents = origins_by_period.get((ticker, "quarterly", metric, expected), ())
        fiscal_starts = [
            item["source_period_start"]
            for item in origins_by_period.get((ticker, "annual", metric, expected), ())
            if item["source_period_start"] is not None
        ]
        fiscal_start = min(fiscal_starts) if fiscal_starts else date(expected.year - 1, expected.month, expected.day)
        for statement in ("annual", "quarterly"):
            by_source_end = origins_by_source_end.get((ticker, statement, metric), {})
            for source_end, items in by_source_end.items():
                if fiscal_start <= source_end <= expected:
                    for item in items:
                        candidates[(ticker, *_origin_identity(item))] = item
        for parent in q4_parents:
            if parent.get("q4_attribution") == "derived":
                basis_items = _normalized_q4_basis_items(
                    chosen=parent,
                    annual_period=expected,
                    calendar_sessions=calendar_sessions,
                    source_metric=str(row["source_metric"]),
                )
                for basis in basis_items:
                    basis["ticker"] = ticker
                    candidates[(ticker, *_origin_identity(basis))] = origin_by_identity.get(
                        (ticker, *_origin_identity(basis)), basis
                    )

    for origin_row in current_slot_origins:
        item = origin_by_identity.get((ticker, *_csv_origin_identity(origin_row)))
        if item is not None:
            candidates[(ticker, *_origin_identity(item))] = item
    for item in projection.get("selected_items", ()):
        candidates[(str(item["ticker"]), *_origin_identity(item))] = item
    return sorted(
        candidates.values(),
        key=lambda item: (
            item["source_metric"], item["source_period_end"] or date.min,
            item["available_from_session"], item["accession_number"],
            item["source_concept"],
        ),
    )


def build_v8_source_window_comparison(
    *, v9_records: Sequence[Mapping[str, str]],
    normalized_origins: Sequence[Mapping[str, Any]],
    evaluation_sessions: Sequence[date], calendar_sessions: Sequence[date],
    source_window_start: date, source_window_end: date,
    v8_manifest_sha256: str, adopted_calendar_sha256: str,
    source_revision: Mapping[str, str], measured_member_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the original V8 raw source window onto frozen V9 expected-slot IDs."""
    references: dict[tuple[Any, ...], str] = _origin_reference_index(v9_records)
    all_index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    window_index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    origin_by_identity: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    origins_by_period: dict[tuple[str, str, str, date], list[Mapping[str, Any]]] = defaultdict(list)
    origins_by_source_end: dict[
        tuple[str, str, str], dict[date, list[Mapping[str, Any]]]
    ] = defaultdict(lambda: defaultdict(list))
    for item in normalized_origins:
        key = (str(item["ticker"]), str(item["statement_type"]), str(item["source_metric"]))
        all_index[key].append(item)
        origin_key = (str(item["ticker"]), *_origin_identity(item))
        if origin_key in origin_by_identity:
            raise ValueError("normalized source-origin identity is duplicated in the comparison population")
        origin_by_identity[origin_key] = item
        if item["period_end"] is not None:
            origins_by_period[(*key, item["period_end"])].append(item)
        if item["source_period_end"] is not None:
            origins_by_source_end[key][item["source_period_end"]].append(item)
        if _source_window_decision(item, start=source_window_start, end=source_window_end) == "admitted_to_v8_source_window":
            window_index[key].append(item)

    slots_by_session: dict[tuple[str, date], dict[tuple[str, str], Mapping[date, Mapping[str, Any]]]] = {}
    family_by_session: dict[tuple[str, date], str | None] = {}
    current_origins_by_slot: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for record in v9_records:
        if record["record_kind"] == "source_origin":
            current_origins_by_slot[record["slot_id"]].append(record)

    current_slots = [
        row for row in v9_records
        if row["record_kind"] == "expected_slot"
        and row["membership_status"] == "eligible_sample_membership"
    ]
    by_slot_id: dict[str, Any] = {}
    classification_counts: Counter[str] = Counter()
    for row in current_slots:
        session = date.fromisoformat(row["session_date"])
        cache_key = (row["ticker"], session)
        selected = slots_by_session.get(cache_key)
        if selected is None:
            selected = {
                (statement, metric): _period_selection(
                    window_index,
                    ticker=row["ticker"], metric=metric, statement=statement, session=session,
                )
                for statement in ("quarterly", "annual")
                for metric in SOURCE_FIELDS
            }
            slots_by_session[cache_key] = selected
            family_by_session[cache_key] = _quarterly_eps_family(selected)

        projection = _selected_origins_for_window(
            ticker=row["ticker"],
            selected=selected,
            feature_id=row["feature_id"],
            row=row,
            session=session,
            calendar_sessions=calendar_sessions,
            all_index=all_index,
            source_index=window_index,
            origins_by_period=origins_by_period,
            source_window_start=source_window_start,
            source_window_end=source_window_end,
        )
        current_slot_origins = current_origins_by_slot.get(row["slot_id"], ())
        candidates = _slot_origin_candidates(
            row,
            origin_by_identity=origin_by_identity,
            origins_by_period=origins_by_period,
            origins_by_source_end=origins_by_source_end,
            projection=projection,
            current_slot_origins=current_slot_origins,
            calendar_sessions=calendar_sessions,
        )
        origin_decisions = {
            _origin_reference(item, references): (
                _source_window_decision(item, start=source_window_start, end=source_window_end)
                if _source_window_decision(item, start=source_window_start, end=source_window_end)
                != "admitted_to_v8_source_window"
                else "admitted_visible" if item["available_from_session"] <= session
                else "admitted_not_yet_public"
            )
            for item in candidates
        }
        selected_origin_ids = sorted({
            _origin_reference(item, references)
            for item in projection.get("selected_items", ())
        })
        current_origin_ids = sorted({origin["origin_id"] for origin in current_slot_origins if origin["origin_id"]})
        selected_identity = {
            (str(item["ticker"]), *_origin_identity(item))
            for item in projection.get("selected_items", ())
        }
        current_identity = {
            (row["ticker"], *_csv_origin_identity(origin))
            for origin in current_slot_origins
        }
        family_changed = (
            row["feature_id"] == "quarterly_eps_growth"
            and projection.get("family") is not None
            and projection.get("family") != row["source_metric"]
        )
        current_comparison = row["comparison_period_end"]
        baseline_comparison = (
            "" if projection.get("comparison_period_end") is None
            else projection["comparison_period_end"].isoformat()
        )
        current_matched = row["matched_period_end"]
        baseline_matched = (
            "" if projection.get("matched_period_end") is None
            else projection["matched_period_end"].isoformat()
        )
        changed_anchor_fields = [
            field for field, current_endpoint, baseline_endpoint in (
                ("matched_period_end", current_matched, baseline_matched),
                ("comparison_period_end", current_comparison, baseline_comparison),
            )
            if current_endpoint and baseline_endpoint and current_endpoint != baseline_endpoint
        ]
        anchor_changed = bool(changed_anchor_fields)
        current_ready = row["status"] in {"matched", "observed", "direct", "derived"}
        baseline_ready = projection["status"] in {"matched", "observed", "direct", "derived"}
        current_prehistory_ids = [
            origin["origin_id"] for origin in current_slot_origins
            if origin["origin_id"] and origin["source_public_date"]
            and _date(origin["source_public_date"]) < source_window_start
        ]
        if family_changed:
            classification = "baseline_family_changed"
            classification_reason = "accepted quarterly EPS family differs after the V8 source-window projection"
        elif anchor_changed:
            classification = "natural_anchor_changed"
            if row["feature_id"] == "annual_roe":
                endpoint_labels = {
                    "matched_period_end": "latest annual income endpoint",
                    "comparison_period_end": "latest annual equity endpoint",
                }
                changed_endpoints = [endpoint_labels[field] for field in changed_anchor_fields]
                classification_reason = (
                    "accepted latest-income/latest-equity endpoints differ after the V8 source-window projection: "
                    + ", ".join(changed_endpoints)
                )
            else:
                classification_reason = (
                    "fiscal anchor endpoints differ after the V8 source-window projection: "
                    + ", ".join(changed_anchor_fields)
                )
        elif current_ready and baseline_ready and selected_identity == current_identity:
            classification = "available_in_both_unchanged"
            classification_reason = "baseline and current status, fiscal endpoints, family, and selected origins agree"
        elif current_ready and baseline_ready:
            classification = "admitted_vintage_changed"
            classification_reason = "both projections are ready on the same frozen key but select different retained origins"
        elif current_ready and not baseline_ready and current_prehistory_ids:
            classification = "recovered_by_prehistory"
            classification_reason = "current slot consumes a retained origin dated before the V8 raw source window"
        elif current_ready and not baseline_ready:
            classification = "ready_without_pre_window_cause"
            classification_reason = "baseline is unready but the current slot has no linked pre-window origin"
        elif not current_ready and baseline_ready:
            classification = "current_missing_baseline_ready"
            classification_reason = row["missing_reason"] or row["status"]
        else:
            classification = "still_missing"
            classification_reason = row["missing_reason"] or projection["missing_reason"] or row["status"]

        by_slot_id[row["slot_id"]] = {
            "ticker": row["ticker"],
            "session_date": row["session_date"],
            "feature_id": row["feature_id"],
            "slot_metric": row["slot_metric"],
            "slot_number": row["slot_number"],
            "expected_period_end": row["expected_period_end"],
            "current": {
                "status": row["status"],
                "missing_reason": row["missing_reason"],
                "source_metric": row["source_metric"],
                "matched_period_end": row["matched_period_end"],
                "comparison_period_end": current_comparison,
                "lookback_stage": row["lookback_stage"],
                "calculation_stage": row["calculation_stage"],
            },
            "v8_source_window_projection": {
                "status": projection["status"],
                "missing_reason": projection["missing_reason"],
                "source_metric": projection.get("source_metric") or "",
                "family": projection.get("family") or "",
                "matched_period_end": (
                    "" if projection.get("matched_period_end") is None
                    else projection["matched_period_end"].isoformat()
                ),
                "comparison_period_end": baseline_comparison,
            },
            "classification": classification,
            "classification_reason": classification_reason,
            "origin_evidence": {
                "current_slot_origin_ids": current_origin_ids,
                "baseline_selected_origin_ids": selected_origin_ids,
                "window_candidates_by_origin_id": dict(sorted(origin_decisions.items())),
            },
        }
        classification_counts[classification] += 1

    if len(by_slot_id) != len(current_slots):
        raise ValueError("frozen V9 expected-slot IDs are not unique")

    origin_population = sorted(
        _canonical_json_bytes({
            "ticker": item["ticker"],
            "statement_type": item["statement_type"],
            "origin_identity": [None if value is None else str(value) for value in _origin_identity(item)],
            "q4_attribution": item.get("q4_attribution", ""),
            "basis_inputs": item.get("inputs") or [],
        })
        for item in normalized_origins
    )
    origin_population_sha256 = hashlib.sha256(b"\n".join(origin_population)).hexdigest()
    eligible_membership_rows = sorted({
        (row["ticker"], row["session_date"])
        for row in v9_records
        if row["record_kind"] == "field_session"
        and row["membership_status"] == "eligible_sample_membership"
    })
    eligible_membership_sha256 = hashlib.sha256(
        _canonical_json_bytes(eligible_membership_rows)
    ).hexdigest()
    selected_member_identity_sha256 = hashlib.sha256(
        _canonical_json_bytes(measured_member_binding)
    ).hexdigest()
    protocol = {
        "schema_version": 1,
        "label": "v8_source_window_projection_not_regenerated_v8",
        "slot_key": "existing frozen V9 expected-slot slot_id; expected period ends are preserved",
        "source_window": {
            "source_public_date_start_inclusive": source_window_start.isoformat(),
            "source_public_date_end_inclusive": source_window_end.isoformat(),
            "date_basis": "each origin acceptance date, with filed-date fallback only when acceptance is empty",
            "normalized_availability": "use the existing once-mapped available_from_session; never remap or shift it",
            "derived_q4_admission": "independently require FY, Q1, Q2, and Q3 basis source-public dates inside the window",
        },
        "selection_and_calculators": {
            "vintages": "reselect after raw source-window and as-of availability filters using the accepted _vintage_key",
            "calculators": [
                "core.pit_coverage._quarterly_eps_full_window",
                "core.pit_coverage._annual_growth_full_window",
                "core.canslim.fiscal_periods.match_fiscal_year_over_year_periods",
                "core.canslim.c_current_earnings._find_earnings_row",
                "core.canslim.a_annual_earnings._calculate_roe",
            ],
            "evaluation": "same adopted calendar, exact evaluation sessions, and eligible sample membership rows",
        },
        "classifications": [
            "available_in_both_unchanged", "admitted_vintage_changed", "recovered_by_prehistory",
            "still_missing", "natural_anchor_changed", "baseline_family_changed",
            "ready_without_pre_window_cause", "current_missing_baseline_ready",
        ],
        "origin_reference_schema": {
            "current_slot_origin_ids": "existing source-origin IDs linked to this slot_id",
            "baseline_selected_origin_ids": "existing frozen detail origin IDs for baseline-selected inputs",
            "window_candidates_by_origin_id": "origin reference to admitted-visible, admitted-not-yet-public, or typed window exclusion",
        },
        "identity_hashes": {
            "normalized_origin_population_sha256": (
                "SHA-256 of lexicographically sorted canonical JSON lines for the exact normalized origin population and retained Q4 basis inputs"
            ),
            "eligible_membership_denominator": (
                "SHA-256 of compact canonical JSON for sorted distinct eligible (ticker, session_date) rows from frozen field_session records"
            ),
            "measured_approved14_binding_sha256": (
                "SHA-256 of compact canonical JSON for the full validated namespace/member/hash/expanded-byte binding"
            ),
        },
        "by_slot_id_schema": {
            "identity": [
                "ticker", "session_date", "feature_id", "slot_metric",
                "slot_number", "expected_period_end",
            ],
            "current": [
                "status", "missing_reason", "source_metric", "matched_period_end",
                "comparison_period_end", "lookback_stage", "calculation_stage",
            ],
            "v8_source_window_projection": [
                "status", "missing_reason", "source_metric", "family",
                "matched_period_end", "comparison_period_end",
            ],
            "classification": "one typed class and explicit classification_reason per frozen expected-slot ID",
            "origin_evidence": [
                "current_slot_origin_ids", "baseline_selected_origin_ids",
                "window_candidates_by_origin_id",
            ],
        },
        "result_hash_scope": (
            "complete_comparison_sha256 is SHA-256 of canonical comparison JSON before adding "
            "complete_comparison_sha256 and canonical_detail_byte_length"
        ),
    }
    protocol_sha256 = hashlib.sha256(_canonical_json_bytes(protocol)).hexdigest()
    comparison = {
        "schema_version": 1,
        "label": "v8_source_window_projection_not_regenerated_v8",
        "source_window": dict(protocol["source_window"]),
        "evaluation_sessions": [session.isoformat() for session in evaluation_sessions],
        "protocol": protocol,
        "protocol_sha256": protocol_sha256,
        "source_identity": {
            "v8_source_manifest_sha256": v8_manifest_sha256,
            "adopted_calendar_sha256": adopted_calendar_sha256,
            "source_revision": dict(source_revision),
            "measured_approved14_binding": dict(measured_member_binding),
            "measured_approved14_binding_sha256": selected_member_identity_sha256,
            "normalized_origin_population_sha256": origin_population_sha256,
            "eligible_membership_denominator": {
                "eligible_security_session_count": len(eligible_membership_rows),
                "ticker_session_rows_sha256": eligible_membership_sha256,
            },
        },
        "expected_slot_count": len(by_slot_id),
        "classification_counts": dict(sorted(classification_counts.items())),
        "by_slot_id": dict(sorted(by_slot_id.items())),
    }
    result_sha256 = hashlib.sha256(_canonical_json_bytes(comparison)).hexdigest()
    comparison["complete_comparison_sha256"] = result_sha256
    comparison["canonical_detail_byte_length"] = 0
    for _ in range(8):
        detail_length = len(_canonical_json_bytes(comparison))
        if comparison["canonical_detail_byte_length"] == detail_length:
            break
        comparison["canonical_detail_byte_length"] = detail_length
    else:
        raise ValueError("canonical comparison byte length did not reach a stable serialization")
    return comparison


def serialize_v9_coverage_records(
    records: Sequence[Mapping[str, str]], *,
    max_records: int = 250_000,
    max_uncompressed_bytes: int = 128 * 1024 * 1024,
) -> tuple[bytes, dict[str, Any]]:
    """Serialize fixed-column CSV and deterministic gzip evidence."""
    if len(records) > max_records:
        raise ValueError("v9 field, slot, and origin records exceed the 250,000-row cap")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in records:
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    canonical = buffer.getvalue().encode("utf-8")
    if len(canonical) > max_uncompressed_bytes:
        raise ValueError("canonical v9 financial coverage evidence exceeds the 128 MiB cap")
    compressed_buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=compressed_buffer, mtime=0) as compressed:
        compressed.write(canonical)
    compressed_bytes = compressed_buffer.getvalue()
    return compressed_bytes, {
        "record_count": len(records),
        "csv_columns": list(CSV_COLUMNS),
        "compression": "gzip",
        "compression_level": 6,
        "gzip_filename": "",
        "gzip_mtime": 0,
        "python_version": platform.python_version(),
        "zlib_version": zlib.ZLIB_VERSION,
        "uncompressed_byte_length": len(canonical),
        "compressed_byte_length": len(compressed_bytes),
        "uncompressed_sha256": hashlib.sha256(canonical).hexdigest(),
        "compressed_sha256": hashlib.sha256(compressed_bytes).hexdigest(),
    }
