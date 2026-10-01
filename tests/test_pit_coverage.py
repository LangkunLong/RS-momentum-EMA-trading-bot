from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import core.pit_coverage as coverage
from core.pit_coverage import (
    FEATURE_SPECS,
    _annual_revenue_state,
    _cell,
    _quarterly_revenue_state,
    _accumulate_cell,
    _finalize_accumulator,
    _fundamental_age_state,
    _new_accumulator,
    _price_metrics,
    build_coverage_report,
    classify_lookback,
    resolve_asof_classification,
    resolve_asof_observation,
)
from core.pit_provenance import PIT_PUBLIC_DATES_ATTR


def test_next_session_availability_and_restatement_keep_earlier_vintage() -> None:
    observations = (
        {
            "period_end": "2023-12-31",
            "source_public_date": "2024-01-02",
            "available_from_session": "2024-01-03",
            "value": 10.0,
            "revision": "original",
        },
        {
            "period_end": "2023-12-31",
            "source_public_date": "2024-04-01",
            "available_from_session": "2024-04-02",
            "value": 12.0,
            "revision": "restatement",
        },
    )

    assert resolve_asof_observation(observations, date(2024, 1, 2)) is None
    original = resolve_asof_observation(observations, date(2024, 1, 3))
    before_restatement = resolve_asof_observation(observations, date(2024, 4, 1))
    after_restatement = resolve_asof_observation(observations, date(2024, 4, 2))

    assert original is not None and original["value"] == 10.0
    assert before_restatement is not None and before_restatement["revision"] == "original"
    assert after_restatement is not None and after_restatement["value"] == 12.0


def test_late_classification_requires_public_and_effective_dates() -> None:
    classifications = (
        {
            "effective_date": "2024-01-01",
            "source_public_date": "2024-01-09",
            "available_from_session": "2024-01-10",
            "group_id": "industry-a",
        },
        {
            "effective_date": "2024-02-01",
            "source_public_date": "2024-02-05",
            "available_from_session": "2024-02-06",
            "group_id": "industry-b",
        },
    )

    assert resolve_asof_classification(classifications, date(2024, 1, 9)) is None
    jan_assignment = resolve_asof_classification(classifications, date(2024, 1, 10))
    assert jan_assignment is not None and jan_assignment["group_id"] == "industry-a"
    feb_assignment = resolve_asof_classification(classifications, date(2024, 2, 6))
    assert feb_assignment is not None and feb_assignment["group_id"] == "industry-b"


def test_warmup_status_uses_required_history_without_imputation() -> None:
    assert classify_lookback(49, 50) == (False, "insufficient_history")
    assert classify_lookback(50, 50) == (True, None)


def test_atr20_requires_a_prior_close_and_twenty_true_ranges() -> None:
    sessions = pd.bdate_range("2024-01-02", periods=21)
    close = [100.0 + index for index in range(21)]
    prices = pd.DataFrame(
        {
            "Open": close,
            "High": [value + 1.0 for value in close],
            "Low": [value - 1.0 for value in close],
            "Close": close,
            "Volume": [1000.0] * len(close),
        },
        index=sessions,
    )

    twenty_bar_metrics = _price_metrics(prices.iloc[:20], sessions[:20])
    twenty_one_bar_metrics = _price_metrics(prices, sessions)

    assert pd.isna(twenty_bar_metrics["atr_20_fraction"].iloc[-1])
    assert twenty_one_bar_metrics["atr_20_fraction"].iloc[-1] == pytest.approx(
        2.0 / close[-1]
    )


def test_quarterly_eps_full_window_counts_matched_growth_slots_not_source_rows() -> None:
    periods = pd.to_datetime(
        [
            "2022-03-31",
            "2022-09-30",
            "2022-12-31",
            "2023-03-31",
            "2023-06-30",
            "2023-09-30",
            "2023-12-31",
            "2024-03-31",
        ]
    )
    eps = pd.Series([1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.2, 1.3], index=periods)

    window = coverage._quarterly_eps_full_window(eps)

    assert window["ready"] is False
    assert window["required_growth_slots"] == 4
    assert window["represented_period_slots"] == 4
    assert window["reported_level_count"] == 8
    assert window["matched_growth_slots"] == 3
    assert window["missing_growth_slots"] == 1
    assert window["missing_slot_reason_counts"] == {"missing_comparable_period": 1}
    assert window["slots"][3]["current_period_end"] == "2023-06-30"
    assert window["slots"][3]["reason"] == "missing_comparable_period"


def test_annual_full_window_flags_skipped_fiscal_year_separately() -> None:
    periods = pd.to_datetime(
        ["2020-12-31", "2021-12-31", "2023-12-31", "2024-12-31"]
    )
    eps = pd.Series([100.0, 120.0, 90.0, 150.0], index=periods)

    window = coverage._annual_growth_full_window(eps)

    assert window["ready"] is True
    assert window["matched_growth_slots"] == 3
    assert window["missing_growth_slots"] == 0
    assert window["consecutive_fiscal_years_ready"] is False
    assert window["skipped_fiscal_year_gaps"] == [
        {
            "current_period_end": "2023-12-31",
            "comparison_period_end": "2021-12-31",
            "skipped_fiscal_years": 1,
            "slot": 2,
        }
    ]


def test_full_window_summary_tracks_session_slots_reasons_and_distinct_tickers() -> None:
    periods = pd.to_datetime(
        [
            "2022-03-31",
            "2022-09-30",
            "2022-12-31",
            "2023-03-31",
            "2023-06-30",
            "2023-09-30",
            "2023-12-31",
            "2024-03-31",
        ]
    )
    window = coverage._quarterly_eps_full_window(
        pd.Series([1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.2, 1.3], index=periods)
    )
    spec = next(spec for spec in FEATURE_SPECS if spec.feature_id == "quarterly_eps_growth")
    cell = _cell(
        spec=spec,
        raw_count=8,
        visible_count=8,
        future_count=0,
        payload={
            "calculable": True,
            "lookback_ready": True,
            "required_history": 2,
            "available_history": 8,
            "reason": None,
            "selected_period_ends": [],
            "selected_available_from_sessions": [],
            "metric_family": "diluted_eps",
            "full_window": window,
        },
    )
    assert cell["full_window"]["missing_growth_slots"] == 1
    overall = _new_accumulator()
    year = _new_accumulator()

    _accumulate_cell(overall, year, "ABCD", cell)
    _accumulate_cell(overall, year, "ABCD", cell)
    summary = _finalize_accumulator(overall)["full_window_coverage"]

    assert summary["denominator_security_sessions"] == 2
    assert summary["ready_security_sessions"] == 0
    assert summary["matched_growth_slots"] == 6
    assert summary["missing_growth_slots"] == 2
    assert summary["missing_slot_reason_counts"] == {"missing_comparable_period": 2}
    assert summary["unique_tickers_with_missing_slots"] == 1


def test_fundamental_age_ignores_a_newer_all_missing_quarter_and_keeps_provenance() -> None:
    history = pd.DataFrame(
        [[1.2, None], [100.0, None]],
        index=["Diluted EPS", "Total Revenue"],
        columns=pd.to_datetime(["2024-03-31", "2024-06-30"]),
    )
    history.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2024-03-31": "2024-05-01",
        "2024-06-30": "2024-08-01",
    }

    state = _fundamental_age_state(history, date(2024, 9, 2))

    assert coverage.pit_feature_snapshot_module._fundamental_age_days(
        history, date(2024, 9, 2)
    ) == 124
    assert state["calculable"] is True
    assert state["available_history"] == 1
    assert state["selected_period_ends"] == ["2024-03-31"]
    assert state["selected_available_from_sessions"] == ["2024-05-01"]


def test_latest_unmatched_quarter_is_not_replaced_by_an_older_match() -> None:
    history = pd.DataFrame(
        [[100.0, 115.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2023-06-30", "2024-09-30"]),
    )
    history.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2023-06-30": "2023-08-02",
        "2024-09-30": "2024-11-01",
    }

    state = _quarterly_revenue_state(history)

    assert state["calculable"] is False
    assert state["lookback_ready"] is False
    assert state["reason"] == "missing_comparable_period"


def test_annual_revenue_report_calculates_adjacent_reported_periods() -> None:
    history = pd.DataFrame(
        [[100.0, 150.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2020-12-31", "2022-12-31"]),
    )
    history.attrs[PIT_PUBLIC_DATES_ATTR] = {
        "2020-12-31": "2021-02-15",
        "2022-12-31": "2023-02-15",
    }

    state = _annual_revenue_state(history)

    assert state["calculable"] is True
    assert state["lookback_ready"] is True
    assert state["selected_period_ends"] == ["2020-12-31", "2022-12-31"]
    assert state["selected_available_from_sessions"] == ["2021-02-15", "2023-02-15"]


def test_nonpositive_annual_revenue_comparator_is_invalid_not_zero_growth() -> None:
    history = pd.DataFrame(
        [[-10.0, 20.0]],
        index=["Total Revenue"],
        columns=pd.to_datetime(["2022-12-31", "2023-12-31"]),
    )

    state = _annual_revenue_state(history)

    assert state["calculable"] is False
    assert state["lookback_ready"] is True
    assert state["reason"] == "invalid_nonpositive_or_nonfinite_comparison"


def test_available_annual_revenue_is_not_misreported_as_a_baseline_policy_input() -> None:
    spec = next(spec for spec in FEATURE_SPECS if spec.feature_id == "annual_revenue_growth")
    cell = _cell(
        spec=spec,
        raw_count=2,
        visible_count=2,
        future_count=0,
        payload={
            "calculable": True,
            "lookback_ready": True,
            "required_history": 2,
            "available_history": 2,
            "reason": None,
            "selected_period_ends": ["2022-12-31", "2023-12-31"],
            "selected_available_from_sessions": ["2023-02-15", "2024-02-15"],
            "metric_family": None,
        },
    )

    assert cell["calculation_stage"] == "observed"
    assert cell["policy_input_stage"] == "intentionally_not_exposed"
    assert cell["policy_input_reason"] == "not_in_current_policy_interface"
    assert cell["declared_policy_consumer_status"] == "intentionally_not_a_current_baseline_consumer"
    assert cell["actual_policy_consumption"] == "not_measured_no_policy_replay"


def test_coverage_aggregates_reasons_as_sessions_and_distinct_tickers() -> None:
    overall = _new_accumulator()
    year = _new_accumulator()
    cell = {
        "source_stage": "observed",
        "publication_stage": "not_yet_public",
        "lookback_stage": "not_yet_public",
        "calculation_stage": "not_yet_public",
        "policy_input_stage": "not_yet_public",
        "reason_codes": ["not_yet_public"],
        "policy_input_reason": "not_yet_public",
    }

    _accumulate_cell(overall, year, "ABCD", cell)
    _accumulate_cell(overall, year, "ABCD", cell)

    result = _finalize_accumulator(overall)
    assert result["denominator_security_sessions"] == 2
    assert result["reason_counts"] == {"not_yet_public": 2}
    assert result["unique_tickers_by_reason"] == {"not_yet_public": 1}
    assert result["policy_input_reason_counts"] == {"not_yet_public": 2}
    assert result["unique_tickers_by_policy_input_reason"] == {"not_yet_public": 1}


def test_small_fixed_history_report_keeps_market_rs_and_industry_denominators(
    monkeypatch, tmp_path: Path
) -> None:
    sessions = pd.bdate_range("2020-01-02", periods=205)
    decision_sessions = sessions[-3:]
    members = tuple(
        (
            "AAA",
            "AAB",
            "AAC",
            "AAD",
            "AAE",
            "AAF",
            "AAG",
            "AAH",
            "AAI",
            "AAJ",
            "AAK",
            "AAL",
            "AAM",
        )
    )

    def price_frame(index: pd.DatetimeIndex, offset: float) -> pd.DataFrame:
        closes = [100.0 + offset + step * (0.02 + offset / 1000) for step in range(len(index))]
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [value + 1.0 for value in closes],
                "Low": [value - 1.0 for value in closes],
                "Close": closes,
                "Volume": [1000.0] * len(index),
            },
            index=index,
        )

    price_data = {
        "SPY": price_frame(sessions, 1.0),
        "QQQ": price_frame(sessions, 2.0),
        **{
            ticker: price_frame(sessions, float(index + 3))
            for index, ticker in enumerate(members[:10])
        },
        # 55 bars gives breadth50 history, while remaining below the 60-bar RS rule.
        members[10]: price_frame(sessions[-55:], 20.0),
        # This member has a stale last bar before every decision session.
        members[11]: price_frame(sessions[-6:-3], 30.0),
        # The final member has no price observations at all.
    }

    class SyntheticBundle:
        metadata = {
            "bundle_kind": "canslim_pit_v2",
            "schema_version": "2",
            "data_cutoff": sessions[-1].date().isoformat(),
            "evaluation_start": decision_sessions[0].date().isoformat(),
            "warmup_start": sessions[0].date().isoformat(),
            "membership_source_kind": "fixed_test_history",
            "membership_source_sha256": "1" * 64,
            "membership_provenance_sha256": "2" * 64,
            "prices_source_kind": "fixed_test_history",
            "prices_source_sha256": "3" * 64,
            "prices_provenance_sha256": "4" * 64,
            "fundamentals_source_kind": "fixed_test_history",
            "fundamentals_source_sha256": "5" * 64,
            "fundamentals_provenance_sha256": "6" * 64,
            "membership_revision_id": "fixed-test-v1",
        }

        def __init__(self) -> None:
            self.data_cutoff = pd.Timestamp(self.metadata["data_cutoff"])
            self._connection = sqlite3.connect(":memory:")
            self._connection.execute(
                "CREATE TABLE fundamentals "
                "(ticker TEXT, statement_type TEXT, period_end TEXT, public_date TEXT, "
                "basic_eps REAL, diluted_eps REAL, total_revenue REAL, net_income REAL, "
                "total_stockholders_equity REAL, shares_outstanding REAL, "
                "held_percent_institutions REAL, institution_count INTEGER, "
                "prev_institution_count INTEGER)"
            )

        def price_symbols(self) -> tuple[str, ...]:
            return tuple(sorted(price_data))

        def fetch_price_data(self, *_args, **_kwargs):
            return price_data

        def tradable_symbols(self) -> tuple[str, ...]:
            return members

        def reference_symbols(self) -> tuple[str, ...]:
            return ("SPY", "QQQ", "IWM")

        def members_at(self, _session: date) -> tuple[str, ...]:
            return members

        def affiliations_at(self, _session: date):
            return {}

    bundle = SyntheticBundle()
    monkeypatch.setattr(coverage, "_verify_committed_source", lambda _sha: "a" * 40)
    monkeypatch.setattr(
        coverage,
        "_load_bundle",
        lambda *_args: (bundle, {}, {"bundle_sha256": "b" * 64}),
    )
    monkeypatch.setattr(coverage, "_financial_states", lambda *_args: {})

    report = build_coverage_report(
        bundle_path=tmp_path / "synthetic.sqlite3",
        bundle_manifest_path=tmp_path / "manifest.json",
        prices_provenance_path=tmp_path / "prices.json",
        feature_spec_path=Path(__file__).parents[1] / "docs" / "historical-feature-specification-v1.md",
        output_dir=tmp_path / "report",
        source_revision="c" * 40,
    )

    assert report["calculator_identities"]["financial_feature_calculator_id"] == (
        "pit-financial-features-v2"
    )
    assert report["calculator_identities"]["financial_feature_calculator_source_revision"] == (
        "c" * 40
    )
    assert report["calculator_identities"]["financial_feature_calculator_git_blob"] == (
        "a" * 40
    )
    market = report["market_context_by_year"][str(decision_sessions[-1].year)]
    assert report["denominators"]["security_sessions"] == 39
    assert report["outputs"]["security_session_coverage"]["rows"] == 39
    assert market["member_security_session_denominator"] == 39
    assert market["breadth_50_valid_member_security_sessions"] == 33
    assert market["breadth_200_valid_member_security_sessions"] == 30
    assert market["rs_eligible_member_security_sessions"] == 30
    assert report["benchmarks_by_year"][str(decision_sessions[-1].year)][
        "expected_benchmark_sessions"
    ] == 9
    assert report["benchmarks_by_year"][str(decision_sessions[-1].year)][
        "ready_benchmark_sessions"
    ] == 6
    assert report["benchmarks_by_year"][str(decision_sessions[-1].year)]["by_symbol"]["IWM"][
        "ready_sessions"
    ] == 0
    assert report["features"]["relative_strength_score"]["overall"][
        "calculable_security_sessions"
    ] == 30
    assert report["features"]["relative_strength_score"]["overall"]["reason_counts"][
        "insufficient_history"
    ] == 3
    quarterly_full_window = report["features"]["quarterly_eps_growth"]["overall"][
        "full_window_coverage"
    ]
    assert quarterly_full_window["denominator_security_sessions"] == 39
    assert quarterly_full_window["ready_security_sessions"] == 0
    assert quarterly_full_window["required_growth_slots"] == 156
    assert quarterly_full_window["missing_growth_slots"] == 156
    assert quarterly_full_window["missing_slot_reason_counts"] == {
        "absent_source_observation": 156
    }
    assert report["features"]["breadth_above_50"]["overall"][
        "calculable_security_sessions"
    ] == 33
    assert report["features"]["breadth_above_200"]["overall"][
        "calculable_security_sessions"
    ] == 30
    industry = report["features"]["industry_group_rs"]["overall"]
    assert industry["denominator_security_sessions"] == 39
    assert industry["calculable_security_sessions"] == 0
    assert industry["reason_counts"] == {"classification_absent": 39}
    assert industry["unique_tickers_by_reason"] == {"classification_absent": 13}
    assert report["exclusions_and_gaps"][
        "security_sessions_without_public_dated_industry_assignment"
    ] == 39
    assert report["features"]["breadth_above_50"]["overall"]["reason_counts"][
        "missing_current_session_price"
    ] == 6
    assert report["exclusions_and_gaps"]["membership_tickers_without_any_price_rows"] == [
        members[12]
    ]

    with coverage.gzip.open(
        tmp_path / "report" / "security_session_coverage.jsonl.gz", "rt", encoding="utf-8"
    ) as row_file:
        first_row = json.loads(next(row_file))
        assert first_row["features"]["quarterly_eps_growth"]["full_window"][
            "required_growth_slots"
        ] == 4
        assert first_row["features"]["quarterly_eps_growth"]["full_window"]["ready"] is False
        assert sum(1 for _line in row_file) == 38
    bundle._connection.close()
