#!/usr/bin/env python3
"""Offline, hash-pinned assessment of the retained Issue #69 price inputs.

This script reads only the retained local artifacts listed in EXPECTED_INPUTS.
It does not fetch, mutate, normalize, or export source data. Coverage derived
from the retained S&P 500 membership and investigation maps is explicitly a
bounded diagnostic, not a production-admission claim.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import math
import platform
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_DATA_ROOT = Path(
    r"C:\Projects\trading_bot\RS-momentum-EMA-trading-bot"
    r"\.artifacts\data\acquisition\acquisition\raw"
)

EXPECTED_INPUTS = {
    "local-cache/prices/prices.csv": "dd18e38d14356df2be9aea79bc777407d40750305dcf327a2f3552815c39c376",
    "local-cache/prices/prices_provenance.json": "7fca2de6d408e232bca560f1df10aeae88433f3a58e1bb64f7bef20944f95ca5",
    "local-cache/prices/spy_trading_days.csv": "93d8ef415bd6be516fb32ebfa5986ad45cbc2077e5beaa9615943db8890be5b9",
    "local-cache/prices/pit_price_identity_map.csv": "6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955",
    "local-cache/prices/membership.csv": "a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389",
    "local-sp500/membership.csv": "a12413a9134623b51350ad4671d4a678eeed8edf2241a9985d0d7d51957f5389",
    "local-sp500/membership_provenance.json": "16fdda33dc102c3e8bfbcd2f683812666a892a0fb28bdc5347ba90c1e9206518",
    "local-sp500/pit_membership_symbol_map.csv": "6284214a6a4cefd766b3c52e84be57ac7e087cbf76d642d22abad131d61d8fa4",
    "local-cache/import-provenance.json": "357fd624fb7087b0c97bc180cc1f4bc3c70aeba5c644d8d5ebaff12e246a6bd7",
    "alpaca-references/acquisition.json": "e9397323b511e91200509013ba54f1fca537c5d3bed9fb7b266b7d3602026f50",
    "alpaca-references/sip-raw.csv": "4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f",
    "alpaca-references/sip-split.csv": "4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f",
    "alpaca-references/references-cutoff.csv": "4991b8f524986a8e7d5e956c04920b06a63a9d1d5dffe3a942a170c798a33f9f",
}

REFERENCE_FILES = [
    "alpaca-references/sip-raw.csv",
    "alpaca-references/sip-split.csv",
    "alpaca-references/references-cutoff.csv",
]
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
LOOKBACKS = (60, 200, 252, 260)
EVALUATION_START = "2021-01-04"
EVALUATION_END = "2025-12-31"
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_verified_bytes(data_root: Path) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Read and hash every source before parsing any source."""
    contents: dict[str, bytes] = {}
    inventory: list[dict[str, Any]] = []
    for relative_path, expected_hash in EXPECTED_INPUTS.items():
        path = data_root / relative_path
        raw = path.read_bytes()
        actual_hash = sha256(raw)
        if actual_hash != expected_hash:
            raise SystemExit(
                f"input hash mismatch before measurement: {relative_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        contents[relative_path] = raw
        inventory.append(
            {
                "path": relative_path,
                "bytes": len(raw),
                "sha256": actual_hash,
                "hash_check": "matched",
            }
        )
    return contents, inventory


def parse_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def parse_json(raw: bytes) -> dict[str, Any]:
    return json.loads(raw.decode("utf-8-sig"))


def in_interval(day: str, start: str, end: str) -> bool:
    return start <= day <= end


def missing_ranges(missing: list[str], session_index: dict[str, int]) -> list[dict[str, str]]:
    if not missing:
        return []
    ranges: list[dict[str, str]] = []
    first = previous = missing[0]
    for day in missing[1:]:
        if session_index[day] != session_index[previous] + 1:
            ranges.append({"start": first, "end": previous})
            first = day
        previous = day
    ranges.append({"start": first, "end": previous})
    return ranges


def numeric_record_quality(rows: list[dict[str, str]], calendar: set[str]) -> dict[str, Any]:
    bad_fields: Counter[str] = Counter()
    zero_volume_by_ticker: Counter[str] = Counter()
    zero_volume_flat_rows = 0
    zero_volume_samples: list[dict[str, str]] = []
    invalid_ohlc = 0
    nonpositive_prices = 0
    negative_volume = 0
    dates_outside_calendar = 0
    for row in rows:
        values: dict[str, float] = {}
        for field in PRICE_FIELDS:
            try:
                value = float(row[field])
                if not math.isfinite(value):
                    raise ValueError("non-finite")
                values[field] = value
            except (KeyError, TypeError, ValueError):
                bad_fields[field] += 1
        if row.get("trade_date") not in calendar:
            dates_outside_calendar += 1
        if len(values) == len(PRICE_FIELDS):
            if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
                nonpositive_prices += 1
            if values["volume"] < 0:
                negative_volume += 1
            if values["volume"] == 0:
                zero_volume_by_ticker[row.get("ticker", "")] += 1
                if len({values["open"], values["high"], values["low"], values["close"]}) == 1:
                    zero_volume_flat_rows += 1
                if len(zero_volume_samples) < 20:
                    zero_volume_samples.append(
                        {
                            "trade_date": row["trade_date"],
                            "ticker": row["ticker"],
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "volume": row["volume"],
                        }
                    )
            if (
                values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"], values["high"])
            ):
                invalid_ohlc += 1
    return {
        "rows_with_invalid_or_missing_numeric_field": dict(sorted(bad_fields.items())),
        "rows_with_nonpositive_ohlc": nonpositive_prices,
        "rows_with_negative_volume": negative_volume,
        "rows_with_zero_volume": sum(zero_volume_by_ticker.values()),
        "zero_volume_rows_with_flat_ohlc": zero_volume_flat_rows,
        "zero_volume_tickers": [
            {"ticker": ticker, "rows": count}
            for ticker, count in sorted(
                zero_volume_by_ticker.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "zero_volume_row_sample_first_20": zero_volume_samples,
        "rows_with_invalid_ohlc_order": invalid_ohlc,
        "rows_outside_spy_calendar": dates_outside_calendar,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/issue-69-bounded-source-assessment-receipt.json"),
    )
    args = parser.parse_args()
    data_root = args.data_root.resolve()

    # Hash all declared inputs first, then parse those exact in-memory bytes.
    blobs, inventory = load_verified_bytes(data_root)
    by_path = {entry["path"]: entry for entry in inventory}
    duplicate_hash_groups: dict[str, list[str]] = defaultdict(list)
    for entry in inventory:
        duplicate_hash_groups[entry["sha256"]].append(entry["path"])

    prices = parse_csv(blobs["local-cache/prices/prices.csv"])
    spy_days_rows = parse_csv(blobs["local-cache/prices/spy_trading_days.csv"])
    membership = parse_csv(blobs["local-sp500/membership.csv"])
    membership_map = parse_csv(blobs["local-sp500/pit_membership_symbol_map.csv"])
    identity_map = parse_csv(blobs["local-cache/prices/pit_price_identity_map.csv"])
    reference_rows = {
        path: parse_csv(blobs[path]) for path in REFERENCE_FILES
    }
    price_provenance = parse_json(blobs["local-cache/prices/prices_provenance.json"])
    membership_provenance = parse_json(blobs["local-sp500/membership_provenance.json"])
    import_provenance = parse_json(blobs["local-cache/import-provenance.json"])
    acquisition = parse_json(blobs["alpaca-references/acquisition.json"])

    spy_days = sorted(row["trade_date"] for row in spy_days_rows)
    spy_calendar = set(spy_days)
    session_index = {day: index for index, day in enumerate(spy_days)}
    evaluation_days = [
        day for day in spy_days if EVALUATION_START <= day <= EVALUATION_END
    ]
    if not evaluation_days or evaluation_days[0] != EVALUATION_START:
        raise SystemExit(
            "declared evaluation start must be an exact SPY session; "
            f"got first evaluation date {evaluation_days[0] if evaluation_days else None}"
        )
    warmup_days = [day for day in spy_days if day < EVALUATION_START]
    if not warmup_days:
        raise SystemExit("no retained SPY sessions precede the declared evaluation window")

    identity_by_symbol: dict[str, dict[str, Any]] = {}
    identity_rows_by_chain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in identity_map:
        identity = dict(row)
        identity_by_symbol[row["provider_symbol"]] = identity
        identity_rows_by_chain[row["chain_id"]].append(identity)

    membership_map_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in membership_map:
        membership_map_by_source[row["source_ticker"]].append(row)

    def canonical_member_ticker(source_ticker: str, day: str) -> str:
        candidates = [
            row
            for row in membership_map_by_source.get(source_ticker, [])
            if in_interval(day, row["effective_start"], row["effective_end"])
        ]
        if len(candidates) > 1:
            raise SystemExit(
                f"overlapping retained membership-map segments for {source_ticker} on {day}"
            )
        return candidates[0]["canonical_ticker"] if candidates else source_ticker

    def admitted_price_symbol(ticker: str, day: str) -> bool:
        identity = identity_by_symbol.get(ticker)
        return (
            True
            if identity is None
            else in_interval(day, identity["admitted_start"], identity["admitted_end"])
        )

    price_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    duplicate_pairs: Counter[tuple[str, str]] = Counter()
    dates_by_symbol_raw: dict[str, list[str]] = defaultdict(list)
    for row in prices:
        pair = (row["ticker"], row["trade_date"])
        if pair in price_by_pair:
            duplicate_pairs[pair] += 1
        else:
            price_by_pair[pair] = row
        dates_by_symbol_raw[row["ticker"]].append(row["trade_date"])
    if duplicate_pairs:
        # Keep measuring using the first exact row, but make duplication visible.
        pass
    dates_by_symbol_raw = {
        ticker: sorted(set(days)) for ticker, days in dates_by_symbol_raw.items()
    }
    valid_dates_by_symbol = {
        ticker: [
            day for day in days
            if day in spy_calendar and admitted_price_symbol(ticker, day)
        ]
        for ticker, days in dates_by_symbol_raw.items()
    }

    chain_overlap_dates: dict[str, list[str]] = {}
    chain_overlap_date_sets: dict[str, set[str]] = {}
    chain_price_dates: dict[str, list[str]] = {}
    chain_overlap_row_details: dict[str, list[dict[str, Any]]] = {}
    for chain_id, chain_rows in identity_rows_by_chain.items():
        symbols = sorted({row["provider_symbol"] for row in chain_rows})
        admitted_symbols_by_day: dict[str, list[str]] = defaultdict(list)
        for symbol in symbols:
            for day in valid_dates_by_symbol.get(symbol, []):
                admitted_symbols_by_day[day].append(symbol)
        overlaps = {
            day: sorted(symbols_for_day)
            for day, symbols_for_day in admitted_symbols_by_day.items()
            if len(symbols_for_day) > 1
        }
        chain_overlap_dates[chain_id] = sorted(overlaps)
        chain_overlap_date_sets[chain_id] = set(overlaps)
        chain_overlap_row_details[chain_id] = [
            {"trade_date": day, "provider_symbols": symbols_for_day}
            for day, symbols_for_day in sorted(overlaps.items())
        ]
        if overlaps:
            chain_price_dates[chain_id] = []
        else:
            chain_price_dates[chain_id] = sorted(admitted_symbols_by_day)

    def has_price_identity_overlap(ticker: str, day: str) -> bool:
        chain_id = identity_by_symbol.get(ticker, {}).get("chain_id")
        return bool(
            chain_id
            and day in chain_overlap_date_sets.get(chain_id, set())
        )

    # Retained membership is an event log: initialize from the 2021-01-01
    # snapshot, then apply each effective-date event before measuring that date.
    events_by_day: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    duplicate_events: Counter[tuple[str, str, str]] = Counter()
    for row in membership:
        key = (row["effective_date"], row["ticker"], row["member"])
        duplicate_events[key] += 1
        if duplicate_events[key] == 1:
            events_by_day[row["effective_date"]].append(
                (row["ticker"], row["member"] == "1")
            )
    duplicate_membership_event_rows = sum(
        count - 1 for count in duplicate_events.values() if count > 1
    )
    ordered_event_days = sorted(events_by_day)
    event_index = 0
    active_source_members: dict[str, bool] = {}
    daily_expected: dict[str, list[dict[str, str]]] = {}
    per_date: list[dict[str, Any]] = []
    per_security_sessions: dict[str, list[str]] = defaultdict(list)
    per_security_covered: dict[str, list[str]] = defaultdict(list)
    projection_collisions: list[dict[str, Any]] = []
    initial_source_members: list[str] | None = None
    price_rows_by_date = set(price_by_pair)
    admitted_covered_pairs: set[tuple[str, str]] = set()
    direct_covered_pairs: set[tuple[str, str]] = set()

    for day in evaluation_days:
        while (
            event_index < len(ordered_event_days)
            and ordered_event_days[event_index] <= day
        ):
            event_day = ordered_event_days[event_index]
            for ticker, is_member in events_by_day[event_day]:
                active_source_members[ticker] = is_member
            event_index += 1

        active = sorted(
            ticker for ticker, is_member in active_source_members.items() if is_member
        )
        if day == evaluation_days[0]:
            initial_source_members = list(active)
        projected: dict[str, list[str]] = defaultdict(list)
        for source_ticker in active:
            projected[canonical_member_ticker(source_ticker, day)].append(source_ticker)
        day_expected: list[dict[str, str]] = []
        for canonical in sorted(projected):
            sources = sorted(projected[canonical])
            if len(sources) > 1:
                projection_collisions.append(
                    {
                        "trade_date": day,
                        "canonical_ticker": canonical,
                        "source_membership_tickers": sources,
                    }
                )
            day_expected.append(
                {
                    "canonical_ticker": canonical,
                    "source_membership_tickers": ",".join(sources),
                }
            )
            pair = (canonical, day)
            per_security_sessions[canonical].append(day)
            if pair in price_rows_by_date:
                direct_covered_pairs.add(pair)
            if pair in price_rows_by_date and admitted_price_symbol(canonical, day):
                admitted_covered_pairs.add(pair)
                per_security_covered[canonical].append(day)

        daily_expected[day] = day_expected
        expected_count = len(day_expected)
        covered_count = sum(
            1
            for expected in day_expected
            if (expected["canonical_ticker"], day) in admitted_covered_pairs
        )
        per_date.append(
            {
                "trade_date": day,
                "expected_member_securities": expected_count,
                "covered_member_securities": covered_count,
                "uncovered_member_securities": expected_count - covered_count,
                "covered_pairs_on_price_identity_overlap_dates": sum(
                    1
                    for expected in day_expected
                    if (expected["canonical_ticker"], day) in admitted_covered_pairs
                    and has_price_identity_overlap(expected["canonical_ticker"], day)
                ),
                "coverage_pct": round(100 * covered_count / expected_count, 6)
                if expected_count
                else None,
            }
        )

    expected_pairs = sum(row["expected_member_securities"] for row in per_date)
    direct_covered_count = len(direct_covered_pairs)
    admitted_covered_count = len(admitted_covered_pairs)
    identity_overlap_covered_pairs = sorted(
        (ticker, day)
        for ticker, day in admitted_covered_pairs
        if has_price_identity_overlap(ticker, day)
    )

    # Count prior retained price rows only. This does not require a price on the
    # current evaluation date or a consecutive session window. Non-overlapping
    # retained identity chains remain investigation-map diagnostics.
    def history_dates_for(ticker: str) -> tuple[list[str], str]:
        identity = identity_by_symbol.get(ticker)
        if identity is None:
            return valid_dates_by_symbol.get(ticker, []), "direct_ticker"
        chain_id = identity["chain_id"]
        if chain_overlap_dates.get(chain_id):
            return (
                valid_dates_by_symbol.get(ticker, []),
                "direct_ticker_only_identity_chain_overlap",
            )
        return chain_price_dates.get(chain_id, []), "retained_nonoverlap_identity_map_chain"

    per_security: list[dict[str, Any]] = []
    for ticker in sorted(per_security_sessions):
        eligible_days = sorted(set(per_security_sessions[ticker]))
        covered_days = sorted(set(per_security_covered[ticker]))
        covered_set = set(covered_days)
        missing_days = [day for day in eligible_days if day not in covered_set]
        history_dates, history_basis = history_dates_for(ticker)
        count_sufficient_days_by_threshold: dict[str, int] = {}
        first_count_sufficient_day_by_threshold: dict[str, str | None] = {}
        for lookback in LOOKBACKS:
            count_sufficient_days = [
                day for day in eligible_days
                if bisect.bisect_left(history_dates, day) >= lookback
            ]
            count_sufficient_days_by_threshold[str(lookback)] = len(count_sufficient_days)
            first_count_sufficient_day_by_threshold[str(lookback)] = (
                count_sufficient_days[0] if count_sufficient_days else None
            )
        per_security.append(
            {
                "canonical_ticker": ticker,
                "first_member_session": eligible_days[0],
                "last_member_session": eligible_days[-1],
                "expected_member_sessions": len(eligible_days),
                "covered_member_sessions": len(covered_days),
                "uncovered_member_sessions": len(missing_days),
                "coverage_pct": round(100 * len(covered_days) / len(eligible_days), 6),
                "missing_session_ranges": missing_ranges(missing_days, session_index),
                "lookback_history_basis": history_basis,
                "price_identity_chain_overlap_session_count_in_input": len(
                    chain_overlap_dates.get(
                        identity_by_symbol.get(ticker, {}).get("chain_id", ""), []
                    )
                ),
            "member_sessions_with_prior_price_row_count_at_or_above_threshold": (
                count_sufficient_days_by_threshold
            ),
            "first_member_session_with_prior_price_row_count_at_or_above_threshold": (
                first_count_sufficient_day_by_threshold
            ),
            }
        )

    # The warm-up window is all retained SPY sessions before the first evaluation
    # session. Its expected denominator is the as-of-first-session S&P snapshot.
    first_eval_day = evaluation_days[0]
    if initial_source_members is None:
        raise SystemExit("failed to capture the first evaluation membership state")
    initial_canonical = sorted(
        {canonical_member_ticker(ticker, first_eval_day) for ticker in initial_source_members}
    )
    initial_warmup_by_security: list[dict[str, Any]] = []
    warmup_per_date: list[dict[str, Any]] = []
    initial_expected_pairs = len(initial_canonical) * len(warmup_days)
    initial_direct_covered_pairs = 0
    initial_admitted_covered_pairs = 0
    for ticker in initial_canonical:
        direct_days = [
            day for day in warmup_days
            if (ticker, day) in price_rows_by_date
        ]
        admitted_days = [
            day for day in direct_days if admitted_price_symbol(ticker, day)
        ]
        admitted_day_set = set(admitted_days)
        chain_days, history_basis = history_dates_for(ticker)
        prior_price_rows = bisect.bisect_left(chain_days, first_eval_day)
        initial_direct_covered_pairs += len(direct_days)
        initial_admitted_covered_pairs += len(admitted_days)
        initial_warmup_by_security.append(
            {
                "canonical_ticker": ticker,
                "expected_warmup_sessions": len(warmup_days),
                "direct_price_sessions": len(direct_days),
                "admitted_price_sessions": len(admitted_days),
                "missing_admitted_price_sessions": len(warmup_days) - len(admitted_days),
                "missing_admitted_price_session_ranges": missing_ranges(
                    [day for day in warmup_days if day not in admitted_day_set],
                    session_index,
                ),
                "prior_price_row_count_at_first_evaluation_session": prior_price_rows,
                "prior_price_row_count_at_or_above_threshold_at_first_evaluation_session": {
                    str(lookback): prior_price_rows >= lookback for lookback in LOOKBACKS
                },
                "history_basis": history_basis,
            }
        )
    for day in warmup_days:
        covered = sum(
            1
            for ticker in initial_canonical
            if (ticker, day) in price_rows_by_date and admitted_price_symbol(ticker, day)
        )
        warmup_per_date.append(
            {
                "trade_date": day,
                "expected_initial_member_securities": len(initial_canonical),
                "covered_member_securities": covered,
                "uncovered_member_securities": len(initial_canonical) - covered,
            }
        )

    # Verify the retained acquisition's declared successor warm-up copies against
    # the predecessor rows; do not treat a ticker or chain label alone as proof.
    price_rows_by_symbol_date = {
        (row["ticker"], row["trade_date"]): row for row in prices
    }
    warmup_validation_declared = price_provenance.get(
        "price_identity_warmup_validation", {}
    )
    declared_copy_counts = warmup_validation_declared.get(
        "copied_warmup_rows_by_symbol", {}
    )
    declared_successor_audits = warmup_validation_declared.get(
        "successor_audits", {}
    )
    successor_copy_checks: list[dict[str, Any]] = []
    for ticker, target in sorted(identity_by_symbol.items()):
        predecessor = target.get("warmup_predecessor", "")
        if not predecessor:
            continue
        target_rows = [
            row for row in prices
            if row["ticker"] == ticker and row["trade_date"] < target["admitted_start"]
        ]
        matches = 0
        mismatches = 0
        mismatch_samples: list[dict[str, Any]] = []
        for row in target_rows:
            prior = price_rows_by_symbol_date.get((predecessor, row["trade_date"]))
            if prior and all(row.get(field) == prior.get(field) for field in PRICE_FIELDS):
                matches += 1
            else:
                mismatches += 1
                if len(mismatch_samples) < 5:
                    mismatch_samples.append(
                        {
                            "trade_date": row["trade_date"],
                            "successor_close": row.get("close"),
                            "predecessor_close": prior.get("close") if prior else None,
                            "predecessor_row_present": prior is not None,
                        }
                    )
        predecessor_identity = identity_by_symbol.get(predecessor)
        same_chain = bool(
            predecessor_identity
            and predecessor_identity["chain_id"] == target["chain_id"]
        )
        admitted_overlap_days = sorted(
            set(valid_dates_by_symbol.get(ticker, []))
            & set(valid_dates_by_symbol.get(predecessor, []))
        )
        admitted_overlap_exact_rows = 0
        for day in admitted_overlap_days:
            successor_row = price_rows_by_symbol_date.get((ticker, day))
            predecessor_row = price_rows_by_symbol_date.get((predecessor, day))
            if successor_row and predecessor_row and all(
                successor_row.get(field) == predecessor_row.get(field)
                for field in PRICE_FIELDS
            ):
                admitted_overlap_exact_rows += 1
        source_declared_audit = declared_successor_audits.get(ticker, {})
        successor_copy_checks.append(
            {
                "successor": ticker,
                "predecessor": predecessor,
                "chain_id_matches": same_chain,
                "source_provenance_declared_successor_audit": source_declared_audit,
                "measured_admitted_overlap_sessions": len(admitted_overlap_days),
                "measured_admitted_overlap_exact_ohlcv_rows": admitted_overlap_exact_rows,
                "declared_exact_overlap_count_matches_measurement": (
                    source_declared_audit.get("exact_overlap_row_count")
                    == admitted_overlap_exact_rows
                ),
                "declared_copy_rows": int(declared_copy_counts.get(ticker, 0)),
                "observed_pre_admission_rows": len(target_rows),
                "exact_field_match_rows": matches,
                "nonmatching_or_unpaired_rows": mismatches,
                "first_mismatch_samples": mismatch_samples,
                "copy_count_matches_declaration": (
                    len(target_rows) == int(declared_copy_counts.get(ticker, 0))
                ),
                "diagnostic_status": (
                    "copy rows reconcile; warm-up-only diagnostic"
                    if same_chain
                    and len(target_rows) == int(declared_copy_counts.get(ticker, 0))
                    and matches == len(target_rows)
                    and not mismatches
                    else "unresolved; do not use as admitted predecessor history"
                ),
            }
        )

    # Reference rows are verified as distinct logical inputs only if their bytes
    # actually differ. Exact byte equality means adjustment labels do not create
    # independent evidence.
    reference_summaries: dict[str, dict[str, Any]] = {}
    reference_pair_sets: dict[str, set[tuple[str, str]]] = {}
    reference_raw_values: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
    for path, rows in reference_rows.items():
        pair_set = {(row["ticker"], row["trade_date"]) for row in rows}
        reference_pair_sets[path] = pair_set
        reference_raw_values[path] = {
            (row["ticker"], row["trade_date"]): row for row in rows
        }
        per_ticker: dict[str, Any] = {}
        for ticker in ("SPY", "QQQ", "IWM"):
            dates = sorted(day for symbol, day in pair_set if symbol == ticker)
            per_ticker[ticker] = {
                "sessions": len(dates),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "missing_spy_calendar_sessions": len(spy_calendar - set(dates)),
                "extra_dates_outside_spy_calendar": len(set(dates) - spy_calendar),
            }
        reference_summaries[path] = {
            "rows": len(rows),
            "unique_symbol_date_pairs": len(pair_set),
            "duplicate_symbol_session_rows": len(rows) - len(pair_set),
            "per_ticker": per_ticker,
        }
    ref_pair_intersection = set.intersection(*reference_pair_sets.values())
    reference_value_differences: dict[str, int] = {}
    for left_index, left_path in enumerate(REFERENCE_FILES):
        for right_path in REFERENCE_FILES[left_index + 1 :]:
            differing = 0
            for pair in ref_pair_intersection:
                left = reference_raw_values[left_path][pair]
                right = reference_raw_values[right_path][pair]
                if any(left.get(field) != right.get(field) for field in PRICE_FIELDS):
                    differing += 1
            reference_value_differences[f"{left_path} <> {right_path}"] = differing

    def ticker_summary(ticker: str) -> dict[str, Any]:
        raw_days = dates_by_symbol_raw.get(ticker, [])
        admitted_days = valid_dates_by_symbol.get(ticker, [])
        return {
            "raw_row_count": len(raw_days),
            "raw_first_date": raw_days[0] if raw_days else None,
            "raw_last_date": raw_days[-1] if raw_days else None,
            "admitted_row_count": len(admitted_days),
            "out_of_admission_segment_rows": len(raw_days) - len(admitted_days),
        }

    action_pairs = [
        {
            "case": "FISV to FI and returned FISV",
            "tickers": ["FISV", "FI"],
            "transition_dates": ["2023-06-07", "2025-11-11"],
            "retained_status": "unresolved",
            "identity_treatment_status": "supported_by_retained_membership_map_only",
            "price_continuity_status": "unresolved",
            "reason": (
                "Membership map declares the same-issuer ticker episode, but the "
                "price identity map admits overlapping FISV and FI segments and "
                "the raw file carries both symbols across the same dates."
            ),
        },
        {
            "case": "COG to CTRA after Cimarex transaction",
            "tickers": ["COG", "CTRA", "XEC"],
            "transition_dates": ["2021-10-04"],
            "retained_status": "supported",
            "identity_treatment_status": "supported_by_retained_maps_only",
            "price_continuity_status": "bounded_nonoverlap_segment_and_copy_check_support",
            "reason": (
                "Retained maps describe COG as Coterra's legacy-survivor chain, "
                "mark the COG-to-CTRA segment, and explicitly reject substituting "
                "XEC history; copied successor warm-up rows are checked below. "
                "This remains bounded map evidence, not production admission."
            ),
        },
        {
            "case": "PEAK to DOC after merger",
            "tickers": ["PEAK", "DOC"],
            "transition_dates": ["2024-03-04"],
            "retained_status": "unresolved",
            "identity_treatment_status": "supported_by_retained_maps_only",
            "price_continuity_status": "unresolved",
            "reason": (
                "Retained maps identify PEAK as accounting acquirer and exclude "
                "pre-merger DOC (Physicians Realty) from Healthpeak history; "
                "copied successor warm-up rows are checked below. The effective-date "
                "DOC row is flat at 11.24 with zero volume while the prior PEAK close "
                "is 17.10 and the next DOC close is 17.04, so the retained price path "
                "remains unresolved. This remains bounded map evidence, not production "
                "admission."
            ),
        },
        {
            "case": "VIAC/PARA to PSKY successor reset",
            "tickers": ["VIAC", "PARA", "PSKY"],
            "transition_dates": ["2022-02-17", "2025-08-07"],
            "retained_status": "excluded",
            "predecessor_price_continuity_status": "excluded",
            "successor_standalone_segment_status": "supported_from_2025-08-07",
            "reason": (
                "Retained maps keep the pre-reset VIAC/PARA same-issuer chain "
                "separate from PSKY successor_reset; no predecessor alias or "
                "pre-reset PSKY price history is admitted."
            ),
        },
    ]
    action_evidence: list[dict[str, Any]] = []
    for case in action_pairs:
        case_tickers = case["tickers"]
        entries = [
            {key: value for key, value in identity_by_symbol[ticker].items()}
            for ticker in case_tickers
            if ticker in identity_by_symbol
        ]
        ticker_data = {
            ticker: ticker_summary(ticker)
            for ticker in case_tickers
        }
        member_source_tickers = set(case_tickers)
        membership_segments = [
            row
            for row in membership_map
            if row["source_ticker"] in member_source_tickers
        ]
        transition_samples: list[dict[str, Any]] = []
        for transition_day in case["transition_dates"]:
            transition_index = session_index.get(transition_day)
            if transition_index is None:
                transition_samples.append(
                    {
                        "transition_date": transition_day,
                        "error": "transition date is not a retained SPY session",
                    }
                )
                continue
            sample_days = [
                spy_days[index]
                for index in (
                    transition_index - 1,
                    transition_index,
                    transition_index + 1,
                )
                if 0 <= index < len(spy_days)
            ]
            sample_rows = []
            for sample_day in sample_days:
                for ticker in case_tickers:
                    row = price_rows_by_symbol_date.get((ticker, sample_day))
                    sample_rows.append(
                        {
                            "trade_date": sample_day,
                            "ticker": ticker,
                            "price_row_present": row is not None,
                            "admitted_for_date_by_price_identity_map": (
                                admitted_price_symbol(ticker, sample_day)
                            ),
                            "ohlcv": (
                                {field: row.get(field) for field in PRICE_FIELDS}
                                if row
                                else None
                            ),
                        }
                    )
            transition_samples.append(
                {
                    "transition_date": transition_day,
                    "session_rows": sample_rows,
                }
            )
        comparison = None
        if len(case_tickers) >= 2:
            left, right = case_tickers[0], case_tickers[1]
            left_days = set(dates_by_symbol_raw.get(left, []))
            right_days = set(dates_by_symbol_raw.get(right, []))
            overlap = sorted(left_days & right_days)
            exact = 0
            for day in overlap:
                lrow = price_rows_by_symbol_date.get((left, day))
                rrow = price_rows_by_symbol_date.get((right, day))
                if lrow and rrow and all(
                    lrow.get(field) == rrow.get(field) for field in PRICE_FIELDS
                ):
                    exact += 1
            admitted_overlap = sorted(
                day for day in overlap
                if admitted_price_symbol(left, day) and admitted_price_symbol(right, day)
            )
            admitted_exact = 0
            for day in admitted_overlap:
                lrow = price_rows_by_symbol_date.get((left, day))
                rrow = price_rows_by_symbol_date.get((right, day))
                if lrow and rrow and all(
                    lrow.get(field) == rrow.get(field) for field in PRICE_FIELDS
                ):
                    admitted_exact += 1
            comparison = {
                "raw_symbol_overlap_sessions": len(overlap),
                "exact_ohlcv_overlap_sessions": exact,
                "nonidentical_overlap_sessions": len(overlap) - exact,
                "first_overlap_date": overlap[0] if overlap else None,
                "last_overlap_date": overlap[-1] if overlap else None,
                "admitted_segment_overlap_sessions": len(admitted_overlap),
                "exact_ohlcv_admitted_overlap_sessions": admitted_exact,
                "first_admitted_overlap_date": (
                    admitted_overlap[0] if admitted_overlap else None
                ),
                "last_admitted_overlap_date": (
                    admitted_overlap[-1] if admitted_overlap else None
                ),
            }
        action_evidence.append(
            {
                **case,
                "identity_segments": entries,
                "membership_symbol_map_segments": membership_segments,
                "raw_price_ticker_ranges": ticker_data,
                "raw_pair_overlap": comparison,
                "transition_price_samples": transition_samples,
            }
        )

    # Count membership removals as removals only; they are not equivalent to
    # delisting events without a corporate-action reason source.
    removal_rows = [
        row for row in membership if row["member"] == "0"
    ]
    action_kinds = Counter(
        row.get("continuity_kind", "") for row in identity_map
    )
    chain_summaries = []
    for chain_id, rows in sorted(identity_rows_by_chain.items()):
        chain_summaries.append(
            {
                "chain_id": chain_id,
                "segments": len(rows),
                "symbols": sorted(row["provider_symbol"] for row in rows),
                "continuity_kinds": sorted({row["continuity_kind"] for row in rows}),
                "admitted_overlap_session_count": len(chain_overlap_dates[chain_id]),
                "overlap_session_sample": chain_overlap_dates[chain_id][:5],
            }
        )

    price_pairs = [(row["ticker"], row["trade_date"]) for row in prices]
    distinct_symbols = sorted({row["ticker"] for row in prices})
    price_dates = sorted({row["trade_date"] for row in prices})
    price_quality = numeric_record_quality(prices, spy_calendar)
    zero_volume_pairs = {
        (row["ticker"], row["trade_date"])
        for row in prices
        if float(row["volume"]) == 0
    }
    evaluation_zero_volume_pairs = admitted_covered_pairs & zero_volume_pairs
    warmup_zero_volume_pairs = {
        (ticker, day)
        for ticker in initial_canonical
        for day in warmup_days
        if (ticker, day) in zero_volume_pairs and admitted_price_symbol(ticker, day)
    }
    reference_hashes = {
        path: by_path[path]["sha256"] for path in REFERENCE_FILES
    }
    reference_files_byte_identical = len(set(reference_hashes.values())) == 1

    measured = {
        "assessment": "issue-69-bounded-retained-source-price-assessment",
        "scope": {
            "evaluation": {
                "start": evaluation_days[0],
                "end": evaluation_days[-1],
                "session_count": len(evaluation_days),
                "definition": (
                    "SPY sessions from the first session after the retained "
                    "2021-01-01 local S&P 500 snapshot through the last retained "
                    "price/calendar session; S&P-only development diagnostic."
                ),
            },
            "warmup": {
                "start": warmup_days[0],
                "end": warmup_days[-1],
                "session_count": len(warmup_days),
                "definition": (
                    "A lookback cohort defined by the local S&P 500 state on "
                    "2021-01-04, measured over earlier retained SPY sessions. This "
                    "does not assert historical constituent membership in 2020."
                ),
            },
            "membership_scope": {
                "source_file": "local-sp500/membership.csv",
                "first_effective_date": membership_provenance["first_effective_date"],
                "last_effective_date": membership_provenance["last_effective_date"],
                "event_count": len(membership),
                "duplicate_event_rows": duplicate_membership_event_rows,
                "symbol_count_in_provenance": membership_provenance["symbol_count"],
                "projection": (
                    "Apply only retained, dated pit_membership_symbol_map.csv "
                    "source_ticker-to-canonical_ticker intervals; no inferred "
                    "ticker continuity. Count a canonical security once per session."
                ),
                "production_union_claim": False,
            },
        },
        "input_inventory": inventory,
        "identical_hash_groups": [
            sorted(paths) for _, paths in sorted(duplicate_hash_groups.items()) if len(paths) > 1
        ],
        "price_inventory": {
            "rows": len(prices),
            "unique_symbol_session_pairs": len(set(price_pairs)),
            "duplicate_symbol_session_pairs": sum(duplicate_pairs.values()),
            "symbols": len(distinct_symbols),
            "first_date": price_dates[0],
            "last_date": price_dates[-1],
            "rows_outside_spy_calendar": price_quality["rows_outside_spy_calendar"],
            "quality": price_quality,
        },
        "membership_coverage": {
            "denominator_member_security_sessions": expected_pairs,
            "unique_canonical_ticker_labels_over_evaluation": len(per_security),
            "stable_issuer_lineage_count": None,
            "ticker_label_identity_caveat": (
                "The 606 canonical ticker labels produced over the evaluation are "
                "not a deduplicated stable-security or issuer-lineage count."
            ),
            "uncovered_member_security_sessions": [
                {
                    "canonical_ticker": ticker,
                    "trade_date": day,
                    "source_membership_tickers": daily_expected[day][
                        next(
                            index
                            for index, row in enumerate(daily_expected[day])
                            if row["canonical_ticker"] == ticker
                        )
                    ]["source_membership_tickers"],
                }
                for ticker, dates in sorted(per_security_sessions.items())
                for day in sorted(set(dates) - set(per_security_covered[ticker]))
            ],
            "direct_ticker_pairs_present_before_identity_admission_filter": direct_covered_count,
            "admitted_dated_segment_pairs_present": admitted_covered_count,
            "covered_pairs_on_price_identity_overlap_dates": len(
                identity_overlap_covered_pairs
            ),
            "price_identity_overlap_member_security_sessions": [
                {
                    "canonical_ticker": ticker,
                    "trade_date": day,
                    "chain_id": identity_by_symbol[ticker]["chain_id"],
                    "overlapping_provider_symbols": next(
                        row["provider_symbols"]
                        for row in chain_overlap_row_details[
                            identity_by_symbol[ticker]["chain_id"]
                        ]
                        if row["trade_date"] == day
                    ),
                }
                for ticker, day in identity_overlap_covered_pairs
            ],
            "coverage_pct_if_overlap_pairs_excluded_identity_sensitivity_only": round(
                100
                * (admitted_covered_count - len(identity_overlap_covered_pairs))
                / expected_pairs,
                8,
            )
            if expected_pairs
            else None,
            "missing_admitted_segment_pairs": expected_pairs - admitted_covered_count,
            "coverage_pct": round(100 * admitted_covered_count / expected_pairs, 8)
            if expected_pairs
            else None,
            "covered_pairs_with_zero_volume_row": len(evaluation_zero_volume_pairs),
            "covered_member_security_sessions_with_zero_volume_row": [
                {
                    "canonical_ticker": ticker,
                    "trade_date": day,
                    "ohlcv": {
                        field: price_by_pair[(ticker, day)].get(field)
                        for field in PRICE_FIELDS
                    },
                }
                for ticker, day in sorted(evaluation_zero_volume_pairs)
            ],
            "volume_positive_row_sensitivity_only": {
                "covered_pairs": admitted_covered_count - len(evaluation_zero_volume_pairs),
                "missing_pairs_if_zero_volume_rows_are_excluded": (
                    expected_pairs - admitted_covered_count + len(evaluation_zero_volume_pairs)
                ),
                "coverage_pct": round(
                    100
                    * (admitted_covered_count - len(evaluation_zero_volume_pairs))
                    / expected_pairs,
                    8,
                )
                if expected_pairs
                else None,
                "policy_note": (
                    "Sensitivity only. Row-presence coverage retains zero-volume "
                    "records; no retained policy defines them as valid or invalid."
                ),
            },
            "projection_collisions": projection_collisions,
            "per_security": per_security,
            "per_evaluation_date": per_date,
            "source_provenance_declared_comparison": {
                "member_trading_day_pairs": price_provenance.get("member_trading_day_pairs"),
                "covered_member_trading_day_pairs": price_provenance.get(
                    "covered_member_trading_day_pairs"
                ),
                "remaining_member_pair_gap_count": price_provenance.get(
                    "remaining_member_pair_gap_count"
                ),
                "coverage_pct": price_provenance.get("coverage_pct"),
                "measured_denominator_matches_declared": (
                    expected_pairs == price_provenance.get("member_trading_day_pairs")
                ),
                "measured_covered_count_matches_declared": (
                    admitted_covered_count
                    == price_provenance.get("covered_member_trading_day_pairs")
                ),
            },
        },
        "warmup_coverage": {
            "initial_2021_snapshot_ticker_label_count": len(initial_canonical),
            "denominator_initial_member_sessions": initial_expected_pairs,
            "direct_ticker_pairs_present": initial_direct_covered_pairs,
            "admitted_dated_segment_pairs_present": initial_admitted_covered_pairs,
            "missing_admitted_dated_segment_pairs": (
                initial_expected_pairs - initial_admitted_covered_pairs
            ),
            "covered_pairs_with_zero_volume_row": len(warmup_zero_volume_pairs),
            "covered_initial_cohort_sessions_with_zero_volume_row": [
                {
                    "canonical_ticker": ticker,
                    "trade_date": day,
                    "ohlcv": {
                        field: price_by_pair[(ticker, day)].get(field)
                        for field in PRICE_FIELDS
                    },
                }
                for ticker, day in sorted(warmup_zero_volume_pairs)
            ],
            "coverage_pct": round(
                100 * initial_admitted_covered_pairs / initial_expected_pairs, 8
            )
            if initial_expected_pairs
            else None,
            "per_security": initial_warmup_by_security,
            "per_warmup_date": warmup_per_date,
            "lookback_thresholds": list(LOOKBACKS),
            "prior_price_row_count_metric_limits": {
                "requires_current_session_price": False,
                "requires_consecutive_session_window": False,
                "is_feature_or_evaluator_readiness": False,
                "note": (
                    "Counts prior retained price rows only, including zero-volume "
                    "rows, using exact admitted price "
                    "identity segments where non-overlapping. Ambiguous chains fall "
                    "back to the exact admitted provider symbol. The 2021 snapshot "
                    "cohort projected over 2020 is not historical 2020 membership "
                    "evidence; identity maps are investigation evidence, not "
                    "production admission."
                ),
            },
        },
        "price_row_validation": price_quality,
        "reference_histories": {
            "files_byte_identical": reference_files_byte_identical,
            "sha256_by_file": reference_hashes,
            "rows_by_file": reference_summaries,
            "pairwise_ohlcv_value_difference_counts": reference_value_differences,
            "acquisition_declaration": {
                "source_kind": acquisition.get("source_kind"),
                "admission_status": acquisition.get("admission_status"),
                "reference_roles": acquisition.get("reference_roles"),
                "adjustment_cutoff": acquisition.get("adjustment_cutoff"),
                "all_three_reference_calendars_exact_declared": acquisition.get(
                    "all_three_reference_calendars_exact"
                ),
                "raw_calibration_adjustment_declared": acquisition.get(
                    "raw_calibration", {}
                ).get("adjustment"),
                "split_snapshot_adjustment_declared": acquisition.get(
                    "split_snapshot", {}
                ).get("adjustment"),
                "credential_persisted": acquisition.get("credential_persisted"),
            },
            "sufficiency_for_history_length": (
                "Each file has calendar-complete SPY/QQQ/IWM sessions across the "
                "retained 2020-01-02 to 2025-12-31 period. The 2021-01-04 first "
                "evaluation session has only 253 prior SPY sessions for its initial "
                "2021 S&P snapshot cohort, so no member has 260 prior price rows then; "
                "the count reaches 260 on 2021-01-13. This is a prior-bar count, "
                "not a feature-ready or consecutive-window check. Duplicate bytes "
                "do not independently reconcile raw versus split versus cutoff basis."
            ),
        },
        "source_adjustment_and_use_basis": {
            "price_provenance": {
                "source_kind": price_provenance.get("source_kind"),
                "feed": price_provenance.get("alpaca_feed"),
                "adjustment": price_provenance.get("alpaca_adjustment"),
                "request_start": price_provenance.get("alpaca_request_start_date"),
                "request_end": price_provenance.get("alpaca_request_end_date"),
                "cutoff_adjustment_date": price_provenance.get("cutoff_adjustment_date"),
                "cutoff_factor_count": price_provenance.get("cutoff_factor_count"),
                "prices_sha256_declared": price_provenance.get("prices_sha256"),
                "price_row_count_declared": price_provenance.get("price_row_count"),
                "identity_map_sha256_declared": price_provenance.get(
                    "price_identity_map_sha256"
                ),
                "membership_sha256_declared": price_provenance.get("membership_sha256"),
                "source_composition": {
                    key: price_provenance.get(key)
                    for key in (
                        "cache_source_row_count",
                        "alpaca_sip_source_row_count",
                        "alpaca_split_source_row_count",
                        "alpaca_sip_fill_row_count",
                        "member_pair_fill_count",
                        "remaining_member_pair_gap_count",
                    )
                },
                "cache_basis_counts": price_provenance.get("cache_basis_counts"),
                "basis_audit_claims": {
                    name: {
                        "overlap_row_count": audit.get("overlap_row_count"),
                        "sampled_row_count": audit.get("sampled_row_count"),
                        "incompatible_symbol_count": audit.get("incompatible_symbol_count"),
                        "incompatible_symbols": audit.get("incompatible_symbols"),
                    }
                    for name, audit in price_provenance.get(
                        "cache_basis_audits", {}
                    ).items()
                },
            },
            "import_admission_status": import_provenance.get("admission_status"),
            "reference_admission_status": acquisition.get("admission_status"),
            "split_only_vs_total_return": (
                "The price provenance declares SPLIT adjustment. No retained "
                "evidence reviewed here declares dividend/total-return adjustment."
            ),
            "source_use_rights": (
                "The reviewed files identify SIP/source roles and local admission "
                "limitations, but do not establish data-license or production-use rights."
            ),
            "independent_reconciliation_limits": [
                "The SIP raw, SIP split, and references-cutoff CSVs have identical bytes.",
                "The full original acquisition chunks/source rows are not among the verified inputs.",
                "Declared sampled basis audits cannot be recomputed from these retained final files alone.",
            ],
        },
        "identity_and_action_evidence": {
            "price_identity_map_rows": len(identity_map),
            "price_identity_map_chains": len(identity_rows_by_chain),
            "continuity_kind_row_counts": dict(sorted(action_kinds.items())),
            "admitted_chain_overlap_sessions": [
                row for row in chain_summaries if row["admitted_overlap_session_count"]
            ],
            "chain_inventory": chain_summaries,
            "successor_warmup_copy_checks": successor_copy_checks,
            "sample_cases": action_evidence,
            "membership_removal_events": {
                "rows": len(removal_rows),
                "unique_tickers": len({row["ticker"] for row in removal_rows}),
                "interpretation": (
                    "These are membership removals, not proven delistings. No retained "
                    "corporate-action reason ledger is present to classify exits."
                ),
            },
        },
        "bounded_statuses": {
            "implementation": "complete_for_bounded_offline_assessment",
            "required_inputs": "insufficient_for_full_production_claim",
            "acceptance_evidence": "bounded_evidence_complete_full_issue_not_satisfied",
            "dependencies": ["#68 complete eligible historical three-index union", "#72 identity/export bridge"],
        },
        "limitations": [
            "The measured universe is retained local S&P 500 membership only; it is not the S&P 500 + Nasdaq-100 + Russell 2000 production union.",
            "The retained identity maps are investigation artifacts; they do not by themselves authenticate production issuer continuity.",
            "Membership removals do not establish delisting, merger, or spin-off treatment.",
            "No external source calls, network acquisition, provider calls, or product-data transformations were performed.",
        ],
    }

    output_path = args.output.resolve()
    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[1]

    def git_output(*git_args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *git_args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise SystemExit(
                f"read-only git identity command failed: {git_args}: "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip()

    measured["execution"] = {
        "script_path": str(script_path),
        "script_sha256": sha256(script_path.read_bytes()),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "git_head_commit": git_output("rev-parse", "HEAD"),
        "git_head_tree": git_output("rev-parse", "HEAD^{tree}"),
        "git_status_short_branch_before_receipt_write": git_output(
            "status", "--short", "--branch"
        ),
        "git_tracked_diff_paths_before_receipt_write": git_output(
            "diff", "--name-only"
        ).splitlines(),
        "git_untracked_paths_before_receipt_write": git_output(
            "ls-files", "--others", "--exclude-standard"
        ).splitlines(),
        "command_line": [str(arg) for arg in sys.argv],
        "data_root": str(data_root),
        "output_path": str(output_path),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(measured, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "receipt": str(output_path),
                "inputs_hash_verified": len(inventory),
                "price_rows": len(prices),
                "price_symbols": len(distinct_symbols),
                "evaluation_sessions": len(evaluation_days),
                "evaluation_member_security_session_denominator": expected_pairs,
                "evaluation_covered_pairs": admitted_covered_count,
                "evaluation_missing_pairs": expected_pairs - admitted_covered_count,
                "evaluation_coverage_pct": measured["membership_coverage"]["coverage_pct"],
                "covered_pairs_on_price_identity_overlap_dates": len(
                    identity_overlap_covered_pairs
                ),
                "covered_evaluation_pairs_with_zero_volume_row": len(
                    evaluation_zero_volume_pairs
                ),
                "initial_warmup_sessions": len(warmup_days),
                "initial_2021_snapshot_ticker_label_count": len(initial_canonical),
                "initial_warmup_pairs_with_zero_volume_row": len(
                    warmup_zero_volume_pairs
                ),
                "reference_files_byte_identical": reference_files_byte_identical,
                "duplicate_price_pairs": sum(duplicate_pairs.values()),
                "duplicate_membership_event_rows": duplicate_membership_event_rows,
                "invalid_price_rows": (
                    sum(price_quality["rows_with_invalid_or_missing_numeric_field"].values())
                    + price_quality["rows_with_nonpositive_ohlc"]
                    + price_quality["rows_with_negative_volume"]
                    + price_quality["rows_with_invalid_ohlc_order"]
                ),
                "zero_volume_rows": price_quality["rows_with_zero_volume"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
