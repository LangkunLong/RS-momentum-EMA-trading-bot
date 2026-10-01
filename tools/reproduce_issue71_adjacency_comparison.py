"""Reproduce the Issue #71 v2-to-v3 acceleration output comparison.

The script opens the retained schema-V2 PIT bundle in read-only mode, verifies
its bundle and manifest digests, and evaluates each quarterly public-date
state window against the v2 calendar-quarter adjacency guard and current v3
calculator. It does not write to the bundle or call external services.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from core.canslim.fiscal_periods import match_fiscal_year_over_year_periods
from core.pit_data import PITDataBundle
from core.pit_feature_snapshot import (
    FINANCIAL_FEATURE_CALCULATOR_ID,
    _EPS_LABELS,
    _earnings_acceleration,
    _growth,
    _growth_acceleration_for_label,
)


EXPECTED_BUNDLE_SHA256 = (
    "cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de"
)
EXPECTED_MANIFEST_SHA256 = (
    "3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c"
)
V2_REFERENCE_REVISION = "3d8a0edbc793b1a435849daead96ffaa9c165d97"
EXPECTED_CALCULATOR_ID = "pit-financial-features-v3"
REPRODUCTION_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "historical-financial-validation-issue71-adjacency-v3.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _git_worktree_dirty() -> bool | None:
    try:
        changes = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(changes.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _v2_growth_acceleration(series: pd.Series) -> float | None:
    """Use the v2 calendar-quarter guard with the shared #66 fiscal matcher."""

    matches = match_fiscal_year_over_year_periods(series)
    if len(matches) < 2 or not matches[0].matched or not matches[1].matched:
        return None
    newest = pd.Period(matches[0].current_period, freq="Q")
    previous = pd.Period(matches[1].current_period, freq="Q")
    if newest.ordinal - previous.ordinal != 1:
        return None
    newest_growth = _growth(matches[0].current_value, matches[0].prior_value)
    previous_growth = _growth(matches[1].current_value, matches[1].prior_value)
    if newest_growth is None or previous_growth is None:
        return None
    return newest_growth - previous_growth


def _v2_earnings_acceleration(quarterly: pd.DataFrame) -> float | None:
    for label in _EPS_LABELS:
        matching = [
            index for index in quarterly.index if str(index).casefold() == label.casefold()
        ]
        if not matching:
            continue
        series = quarterly.loc[matching[0]]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[0]
        if not isinstance(series, pd.Series):
            raise ValueError("PIT quarterly earnings row is invalid")
        comparisons = match_fiscal_year_over_year_periods(series)
        if comparisons and comparisons[0].matched:
            return _v2_growth_acceleration(series)
    return None


def _v2_revenue_acceleration(quarterly: pd.DataFrame) -> float | None:
    matching = [
        index
        for index in quarterly.index
        if str(index).casefold() == "total revenue"
    ]
    if not matching:
        return None
    series = quarterly.loc[matching[0]]
    if isinstance(series, pd.DataFrame):
        series = series.iloc[0]
    if not isinstance(series, pd.Series):
        raise ValueError("PIT quarterly revenue row is invalid")
    return _v2_growth_acceleration(series)


def _outputs_differ(old: float | None, new: float | None) -> bool:
    if old is None or new is None:
        return old is not new
    return old != new


def _quarterly_boundaries(
    connection: sqlite3.Connection, evaluation_end: date
) -> dict[str, list[date]]:
    rows = connection.execute(
        """
        SELECT DISTINCT ticker, public_date
        FROM fundamentals
        WHERE statement_type = 'quarterly' AND public_date <= ?
        ORDER BY ticker, public_date
        """,
        (evaluation_end.isoformat(),),
    )
    boundaries: dict[str, list[date]] = defaultdict(list)
    for ticker, raw_public_date in rows:
        boundaries[str(ticker)].append(date.fromisoformat(str(raw_public_date)))
    return dict(boundaries)


def _price_sessions(
    connection: sqlite3.Connection, evaluation_start: date, evaluation_end: date
) -> dict[str, list[date]]:
    rows = connection.execute(
        """
        SELECT ticker, trade_date
        FROM price
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY ticker, trade_date
        """,
        (evaluation_start.isoformat(), evaluation_end.isoformat()),
    )
    sessions: dict[str, list[date]] = defaultdict(list)
    for ticker, raw_trade_date in rows:
        sessions[str(ticker)].append(date.fromisoformat(str(raw_trade_date)))
    return dict(sessions)


def _count_sessions(
    sessions: list[date], start: date, end_exclusive: date
) -> int:
    return bisect.bisect_left(sessions, end_exclusive) - bisect.bisect_left(
        sessions, start
    )


def _empty_metric() -> dict[str, Any]:
    return {
        "state_windows": 0,
        "changed_windows": 0,
        "v2_available_to_v3_unavailable": 0,
        "v2_unavailable_to_v3_available": 0,
        "affected_symbols": set(),
        "changed_priced_security_sessions": 0,
    }


def _public_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "state_windows": metric["state_windows"],
        "changed_windows": metric["changed_windows"],
        "v2_available_to_v3_unavailable": metric[
            "v2_available_to_v3_unavailable"
        ],
        "v2_unavailable_to_v3_available": metric[
            "v2_unavailable_to_v3_available"
        ],
        "affected_symbols": len(metric["affected_symbols"]),
        "changed_priced_security_sessions": metric[
            "changed_priced_security_sessions"
        ],
    }


def _validate_recorded_counts(
    reproduced_scan: dict[str, Any], recorded_scan: dict[str, Any]
) -> None:
    """Fail closed if this run does not reproduce the committed addendum."""

    expected_scalars = {
        "quarterly_records": recorded_scan["quarterly_records"],
        "price_security_sessions_all_symbols": recorded_scan[
            "price_security_sessions_all_symbols"
        ],
    }
    mismatches: list[str] = []
    for field, expected in expected_scalars.items():
        actual = reproduced_scan[field]
        if actual != expected:
            mismatches.append(f"{field}: expected {expected}, got {actual}")

    expected_windows = recorded_scan["state_window_denominator_per_metric"]
    for metric_name in ("earnings_acceleration", "revenue_acceleration"):
        actual_metric = reproduced_scan[metric_name]
        if actual_metric["state_windows"] != expected_windows:
            mismatches.append(
                f"{metric_name}.state_windows: expected {expected_windows}, "
                f"got {actual_metric['state_windows']}"
            )
        recorded_metric = recorded_scan[metric_name]
        expected_metric_fields = {
            "changed_windows": recorded_metric["changed_windows"],
            "affected_symbols": recorded_metric["affected_symbols"],
            "changed_priced_security_sessions": recorded_metric[
                "changed_priced_security_sessions"
            ],
            "v2_available_to_v3_unavailable": recorded_metric.get(
                "changed_v2_available_to_v3_unavailable", 0
            ),
            "v2_unavailable_to_v3_available": recorded_metric.get(
                "changed_v2_unavailable_to_v3_available", 0
            ),
        }
        for field, expected in expected_metric_fields.items():
            actual = actual_metric[field]
            if actual != expected:
                mismatches.append(
                    f"{metric_name}.{field}: expected {expected}, got {actual}"
                )

    if mismatches:
        raise ValueError(
            "reproduced measurement differs from the committed v3 addendum: "
            + "; ".join(mismatches)
        )


def _validate_recorded_identity(
    recorded_report: dict[str, Any],
    *,
    bundle_sha256: str,
    manifest_sha256: str,
) -> None:
    """Bind this run to the input hashes recorded in the committed addendum."""

    recorded_input = recorded_report["input_bundle"]
    if (
        recorded_report.get("calculator_identity") != EXPECTED_CALCULATOR_ID
        or recorded_input.get("sha256") != bundle_sha256
        or recorded_input.get("manifest_sha256") != manifest_sha256
    ):
        raise ValueError(
            "reproduction inputs or calculator identity differ from the v3 addendum"
        )


def reproduce(
    *,
    bundle_path: Path,
    manifest_path: Path,
    expected_bundle_sha256: str,
    expected_manifest_sha256: str,
    evaluation_start: date,
    evaluation_end: date,
) -> dict[str, Any]:
    if evaluation_start > evaluation_end:
        raise ValueError("evaluation start must not be after evaluation end")
    bundle_path = bundle_path.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    actual_bundle_sha256 = _sha256(bundle_path)
    if actual_bundle_sha256 != expected_bundle_sha256:
        raise ValueError("PIT bundle SHA-256 does not match the expected digest")
    actual_manifest_sha256 = _sha256(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ValueError("bundle manifest SHA-256 does not match the expected digest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("bundle_sha256") != actual_bundle_sha256:
        raise ValueError("bundle manifest does not declare the actual bundle digest")
    if FINANCIAL_FEATURE_CALCULATOR_ID != EXPECTED_CALCULATOR_ID:
        raise ValueError(
            "this reproduction recipe expects the v3 financial calculator identity"
        )
    recorded_report = json.loads(
        REPRODUCTION_REPORT_PATH.read_text(encoding="utf-8")
    )
    _validate_recorded_identity(
        recorded_report,
        bundle_sha256=actual_bundle_sha256,
        manifest_sha256=actual_manifest_sha256,
    )

    v2_metrics = {
        "earnings_acceleration": _empty_metric(),
        "revenue_acceleration": _empty_metric(),
    }
    v3_metrics = {
        "earnings_acceleration": _empty_metric(),
        "revenue_acceleration": _empty_metric(),
    }
    quarterly_record_count = 0
    quarterly_symbol_count = 0
    all_price_row_count = 0

    with PITDataBundle(
        bundle_path, expected_sha256=expected_bundle_sha256
    ) as bundle:
        if bundle.metadata.get("schema_version") != "2":
            raise ValueError("reproduction requires the retained schema-V2 bundle")
        connection = _readonly_connection(bundle_path)
        try:
            quarterly_record_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM fundamentals WHERE statement_type='quarterly'"
                ).fetchone()[0]
            )
            quarterly_symbol_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT ticker) FROM fundamentals "
                    "WHERE statement_type='quarterly' AND public_date <= ?",
                    (evaluation_end.isoformat(),),
                ).fetchone()[0]
            )
            all_price_row_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM price WHERE trade_date >= ? AND trade_date <= ?",
                    (
                        evaluation_start.isoformat(),
                        evaluation_end.isoformat(),
                    ),
                ).fetchone()[0]
            )
            boundaries_by_symbol = _quarterly_boundaries(
                connection, evaluation_end
            )
            prices_by_symbol = _price_sessions(
                connection, evaluation_start, evaluation_end
            )
        finally:
            connection.close()

        evaluation_end_exclusive = evaluation_end + timedelta(days=1)
        for symbol in sorted(boundaries_by_symbol):
            boundaries = boundaries_by_symbol[symbol]
            prices = prices_by_symbol.get(symbol, [])
            for index, boundary in enumerate(boundaries):
                next_boundary = (
                    boundaries[index + 1]
                    if index + 1 < len(boundaries)
                    else evaluation_end_exclusive
                )
                window_start = max(boundary, evaluation_start)
                window_end = min(next_boundary, evaluation_end_exclusive)
                if window_start >= window_end:
                    continue
                priced_sessions = _count_sessions(prices, window_start, window_end)
                if priced_sessions == 0:
                    continue

                snapshot = bundle.fundamentals_provider(
                    symbol,
                    pd.Timestamp(window_start),
                    include_provenance=True,
                )
                quarterly = snapshot.get("quarterly_income")
                if not isinstance(quarterly, pd.DataFrame):
                    raise ValueError("PIT quarterly_income snapshot is not a DataFrame")

                outputs = (
                    (
                        "earnings_acceleration",
                        _v2_earnings_acceleration(quarterly),
                        _earnings_acceleration(quarterly),
                    ),
                    (
                        "revenue_acceleration",
                        _v2_revenue_acceleration(quarterly),
                        _growth_acceleration_for_label(quarterly, "Total Revenue"),
                    ),
                )
                for key, old_output, new_output in outputs:
                    v2_metrics[key]["state_windows"] += 1
                    v3_metrics[key]["state_windows"] += 1
                    if not _outputs_differ(old_output, new_output):
                        continue
                    for metric, _value in (
                        (v2_metrics[key], old_output),
                        (v3_metrics[key], new_output),
                    ):
                        metric["changed_windows"] += 1
                        metric["affected_symbols"].add(symbol)
                        metric["changed_priced_security_sessions"] += priced_sessions
                    if old_output is not None and new_output is None:
                        v3_metrics[key]["v2_available_to_v3_unavailable"] += 1
                    elif old_output is None and new_output is not None:
                        v3_metrics[key]["v2_unavailable_to_v3_available"] += 1

    if any(
        v2_metrics[key]["state_windows"] != v3_metrics[key]["state_windows"]
        for key in v2_metrics
    ):
        raise AssertionError("v2/v3 state-window denominators diverged")

    output_scan = {
            "evaluation_start": evaluation_start.isoformat(),
            "evaluation_end": evaluation_end.isoformat(),
            "quarterly_records": quarterly_record_count,
            "quarterly_symbols": quarterly_symbol_count,
            "price_security_sessions_all_symbols": all_price_row_count,
            "earnings_acceleration": _public_metric(
                v3_metrics["earnings_acceleration"]
            ),
            "revenue_acceleration": _public_metric(
                v3_metrics["revenue_acceleration"]
            ),
        }
    _validate_recorded_counts(output_scan, recorded_report["scan"])

    return {
        "recorded_counts_check": "passed",
        "scan": output_scan,
        "identity": {
            "measurement_kind": "development feature-output comparison",
            "calculator_identity": FINANCIAL_FEATURE_CALCULATOR_ID,
            "v2_reference_revision": V2_REFERENCE_REVISION,
            "source_revision_at_runtime": _git_revision(),
            "source_worktree_dirty": _git_worktree_dirty(),
            "reproduction_script_sha256": _sha256(Path(__file__).resolve()),
            "v3_calculator_source_sha256": _sha256(
                Path(__file__).resolve().parents[1]
                / "core"
                / "pit_feature_snapshot.py"
            ),
            "bundle_sha256": actual_bundle_sha256,
            "manifest_sha256": actual_manifest_sha256,
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "pandas_version": pd.__version__,
            "sqlite_version": sqlite3.sqlite_version,
        },
        "method": {
            "quarterly_state_boundary": "distinct quarterly fundamental public_date",
            "snapshot_date": "state boundary clipped to evaluation start",
            "state_window": "[snapshot_date, next quarterly public_date), clipped to evaluation period",
            "window_counting": "one state window when the ticker has at least one price row in the interval",
            "changed_sessions": "count all ticker price rows in each changed state window",
            "fiscal_matcher": "shared #66 matcher with 28-day prior-calendar-year tolerance",
            "v2_adjacency": "newest two matched current periods differ by one pandas calendar-quarter ordinal",
            "v3_adjacency": "newest two matched current period ends are 84 through 105 days apart inclusive",
            "read_only": True,
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--expected-bundle-sha256", default=EXPECTED_BUNDLE_SHA256
    )
    parser.add_argument(
        "--expected-manifest-sha256", default=EXPECTED_MANIFEST_SHA256
    )
    parser.add_argument("--evaluation-start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--evaluation-end", type=date.fromisoformat, default=date(2025, 12, 31))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    started = time.perf_counter()
    result = reproduce(
        bundle_path=args.bundle,
        manifest_path=args.manifest,
        expected_bundle_sha256=args.expected_bundle_sha256,
        expected_manifest_sha256=args.expected_manifest_sha256,
        evaluation_start=args.evaluation_start,
        evaluation_end=args.evaluation_end,
    )
    result["runtime_seconds"] = round(time.perf_counter() - started, 3)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
