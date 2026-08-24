"""Differential regression for semantics-preserving PIT finalization batching."""

from __future__ import annotations

from datetime import date
import sqlite3
from pathlib import Path

import pandas as pd

from core.backtest_engine import EntryAttemptOutcome, SimulationResult
from core.leader_evaluation import PointInTimeUniverse
from core.pit_data import PITDataBundle
import pit_baseline


def _fundamental_row(
    statement_type: str,
    period_end: str,
    public_date: str,
    *,
    basic_eps: float | None = None,
    net_income: float | None = None,
    equity: float | None = None,
    shares: float | None = None,
) -> tuple[object, ...]:
    return (
        "AAA",
        statement_type,
        period_end,
        public_date,
        basic_eps,
        None,
        None,
        net_income,
        None,
        equity,
        shares,
        None,
        None,
        None,
    )


def test_finalization_batching_preserves_as_of_coverage_and_daily_funnel(
    tmp_path: Path,
) -> None:
    """Break caught: batching skipped same-day/restated facts or changed daily totals."""
    database = tmp_path / "pit.sqlite3"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fundamentals (ticker TEXT NOT NULL, statement_type TEXT NOT NULL, "
        "period_end TEXT NOT NULL, public_date TEXT NOT NULL, basic_eps REAL, "
        "diluted_eps REAL, total_revenue REAL, net_income REAL, common_stock REAL, "
        "total_stockholders_equity REAL, shares_outstanding REAL, "
        "held_percent_institutions REAL, institution_count INTEGER, "
        "prev_institution_count INTEGER)"
    )
    pre_window_rows = [
        _fundamental_row("quarterly", "2023-03-31", "2024-04-01", basic_eps=0.5),
        _fundamental_row("quarterly", "2023-06-30", "2024-04-01", basic_eps=0.6),
        _fundamental_row("quarterly", "2023-09-30", "2024-04-01", basic_eps=0.7),
        _fundamental_row("quarterly", "2023-12-31", "2024-04-01", basic_eps=0.8),
        _fundamental_row("quarterly", "2024-03-31", "2024-04-01", basic_eps=1.0),
        _fundamental_row(
            "annual", "2022-12-31", "2024-04-01", basic_eps=0.5, net_income=50.0
        ),
        _fundamental_row(
            "annual", "2023-12-31", "2024-04-01", basic_eps=1.0, net_income=100.0
        ),
    ]
    same_day_rows = [
        _fundamental_row("quarterly", "2023-03-31", "2024-05-01", basic_eps=1.0),
        _fundamental_row("quarterly", "2023-06-30", "2024-05-01", basic_eps=1.1),
        _fundamental_row("quarterly", "2023-09-30", "2024-05-01", basic_eps=1.2),
        _fundamental_row("quarterly", "2023-12-31", "2024-05-01", basic_eps=1.3),
        _fundamental_row("quarterly", "2024-03-31", "2024-05-01", basic_eps=2.0),
        _fundamental_row(
            "annual", "2022-12-31", "2024-05-01", basic_eps=1.0, net_income=100.0
        ),
        _fundamental_row(
            "annual", "2023-12-31", "2024-05-01", basic_eps=2.0, net_income=200.0
        ),
        _fundamental_row("balance", "2023-12-31", "2024-05-01", equity=1_000.0),
        _fundamental_row(
            "institutional", "2024-03-31", "2024-05-01", shares=10_000.0
        ),
    ]
    repeated_rows = [
        _fundamental_row("quarterly", "2024-03-31", "2024-05-03", basic_eps=2.0),
        _fundamental_row(
            "annual", "2023-12-31", "2024-05-03", basic_eps=2.0, net_income=200.0
        ),
    ]
    changed_rows = [
        _fundamental_row("quarterly", "2024-03-31", "2024-05-04", basic_eps=3.0),
        _fundamental_row(
            "annual", "2023-12-31", "2024-05-04", basic_eps=3.0, net_income=300.0
        ),
    ]
    connection.executemany(
        "INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [*pre_window_rows, *same_day_rows, *repeated_rows, *changed_rows],
    )
    connection.commit()

    bundle = PITDataBundle.__new__(PITDataBundle)
    bundle._connection = connection
    bundle.metadata = {"data_cutoff": "2024-12-31"}
    bundle.membership = PointInTimeUniverse.from_rows(
        [
            {"effective_date": "2024-01-01", "ticker": "AAA", "member": True},
            {"effective_date": "2024-01-01", "ticker": "BBB", "member": True},
        ]
    )

    try:
        no_history = bundle.fundamentals_as_of("BBB", pd.Timestamp("2024-05-04"))
        assert no_history["quarterly_income"].empty
        assert no_history["annual_income"].empty
        assert no_history["balance_sheet"].empty
        assert no_history["company_info"] == {
            "shares_outstanding": None,
            "held_percent_institutions": None,
            "institution_count": None,
            "prev_institution_count": None,
        }

        states = list(
            bundle.iter_fundamental_state_boundaries(
                {
                    "BBB": (date(2024, 5, 4), date(2024, 5, 4)),
                    "AAA": (date(2024, 4, 30), date(2024, 5, 4)),
                }
            )
        )
        assert [(symbol, public_date) for symbol, public_date, _facts in states] == [
            ("AAA", date(2024, 4, 30)),
            ("AAA", date(2024, 5, 1)),
            ("AAA", date(2024, 5, 3)),
            ("AAA", date(2024, 5, 4)),
            ("BBB", date(2024, 5, 4)),
        ]
        for symbol, public_date, facts in states:
            direct = bundle.fundamentals_as_of(symbol, pd.Timestamp(public_date))
            for field in ("quarterly_income", "annual_income", "balance_sheet"):
                pd.testing.assert_frame_equal(facts[field], direct[field])
            assert facts["company_info"] == direct["company_info"]
        assert states[0][2]["quarterly_income"].loc[
            "Basic EPS", pd.Timestamp("2024-03-31")
        ] == 1.0
        assert states[1][2]["quarterly_income"].loc[
            "Basic EPS", pd.Timestamp("2024-03-31")
        ] == 2.0
        assert states[1][2]["annual_income"].loc[
            "Basic EPS", pd.Timestamp("2023-12-31")
        ] == 2.0
        assert states[1][2]["company_info"]["shares_outstanding"] == 10_000.0
        pd.testing.assert_frame_equal(
            states[2][2]["quarterly_income"], states[1][2]["quarterly_income"]
        )
        pd.testing.assert_frame_equal(
            states[2][2]["annual_income"], states[1][2]["annual_income"]
        )
        assert states[3][2]["quarterly_income"].loc[
            "Basic EPS", pd.Timestamp("2024-03-31")
        ] == 3.0
        assert states[3][2]["annual_income"].loc[
            "Basic EPS", pd.Timestamp("2023-12-31")
        ] == 3.0
        assert states[4][2]["quarterly_income"].empty
        assert states[4][2]["annual_income"].empty

        signal_log = pd.DataFrame(
            [
                {"symbol": "AAA", "signal_date": "2024-04-30", "current_growth": 1.0, "annual_growth": 1.0},
                {"symbol": "AAA", "signal_date": "2024-05-01", "current_growth": 1.0, "annual_growth": 1.0},
                {"symbol": "AAA", "signal_date": "2024-05-02", "current_growth": 1.0, "annual_growth": 1.0},
                {"symbol": "AAA", "signal_date": "2024-05-03", "current_growth": 1.0, "annual_growth": 1.0},
                {"symbol": "AAA", "signal_date": "2024-05-04", "current_growth": 2.0, "annual_growth": 2.0},
                {"symbol": "BBB", "signal_date": "2024-05-04", "current_growth": None, "annual_growth": None},
            ]
        )
        signal_log.index = pd.Index([7, 7, 8, 9, 9, 10], name="symbol")
        real_members_at = bundle.members_at
        membership_calls: list[str] = []

        def counted_members_at(when: str) -> frozenset[str]:
            membership_calls.append(when)
            return real_members_at(when)

        bundle.members_at = counted_members_at

        def reject_point_queries(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("coverage regressed to one as-of query per signal row")

        bundle.fundamentals_as_of = reject_point_queries
        assert pit_baseline._evaluated_coverage(signal_log, bundle) == {
            "evaluated_symbol_date_count": 6,
            "usable_current_quarterly_count": 5,
            "usable_annual_count": 5,
            "usable_current_quarterly_and_annual_count": 5,
            "current_quarterly_and_annual_pct": 83.33333333333334,
            "coverage_basis": (
                "unique strict-PIT signal-log symbol/date rows independently recomputed from "
                "hash-bound as-of quarterly/annual frames with fiscal-date-matched "
                "evaluate_c and unchanged evaluate_a"
            ),
        }
        assert membership_calls == [
            "2024-04-30",
            "2024-05-01",
            "2024-05-02",
            "2024-05-03",
            "2024-05-04",
        ]

        sessions = pd.to_datetime(
            ["2024-05-01", "2024-05-02", "2024-05-03", "2024-05-04"]
        )
        funnel_signals = pd.DataFrame(
            [
                {"symbol": "AAA", "signal_date": "2024-05-01", "entry_contract_eligible": True, "buy_signal": True},
                {"symbol": "BBB", "signal_date": "2024-05-01", "entry_contract_eligible": False, "buy_signal": False},
                {"symbol": "AAA", "signal_date": "2024-05-03", "entry_contract_eligible": True, "buy_signal": True},
                {"symbol": "BBB", "signal_date": "2024-05-03", "entry_contract_eligible": True, "buy_signal": True},
                {"symbol": "AAA", "signal_date": "2024-05-04", "entry_contract_eligible": True, "buy_signal": True},
            ]
        )
        funnel_signals.index = pd.Index([20, 20, 21, 22, 22], name="signal_date")
        result = SimulationResult(
            signal_log=funnel_signals,
            entry_outcomes=(
                EntryAttemptOutcome("AAA", "2024-05-01", "2024-05-02", 100.0, 100.0, 105.0, 101.0, "entries_executed"),
                EntryAttemptOutcome("AAA", "2024-05-03", "2024-05-04", 110.0, 110.0, 115.5, 111.0, "entry_rejected_no_cash"),
                EntryAttemptOutcome("BBB", "2024-05-03", "2024-05-04", 50.0, 50.0, 52.5, 51.0, "entries_executed"),
            ),
            execution_diagnostics={
                "entry_attempts": 3,
                "entries_executed": 2,
                "capacity_truncated_signals": 0,
                "entry_rejected_capacity": 0,
            },
        )
        expected_funnel = pd.DataFrame(
            [
                {"signal_date": "2024-05-01", "evaluated_count": 2, "qualified_count": 1, "attempted_count": 1, "executed_count": 1, "rejected_count": 0},
                {"signal_date": "2024-05-02", "evaluated_count": 0, "qualified_count": 0, "attempted_count": 0, "executed_count": 0, "rejected_count": 0},
                {"signal_date": "2024-05-03", "evaluated_count": 2, "qualified_count": 2, "attempted_count": 2, "executed_count": 1, "rejected_count": 1},
                {"signal_date": "2024-05-04", "evaluated_count": 1, "qualified_count": 1, "attempted_count": 0, "executed_count": 0, "rejected_count": 0},
            ]
        )
        pd.testing.assert_frame_equal(
            pit_baseline._daily_entry_funnel_frame(result, sessions),
            expected_funnel,
        )

        final_only = SimulationResult(
            signal_log=pd.DataFrame(
                [
                    {
                        "symbol": "AAA",
                        "signal_date": "2024-05-02",
                        "entry_contract_eligible": True,
                        "buy_signal": True,
                    }
                ]
            ),
            entry_outcomes=(),
            execution_diagnostics={
                "entry_attempts": 0,
                "entries_executed": 0,
                "capacity_truncated_signals": 0,
                "entry_rejected_capacity": 0,
            },
        )
        pd.testing.assert_frame_equal(
            pit_baseline._daily_entry_funnel_frame(
                final_only, pd.to_datetime(["2024-05-01", "2024-05-02"])
            ),
            pd.DataFrame(
                [
                    {"signal_date": "2024-05-01", "evaluated_count": 0, "qualified_count": 0, "attempted_count": 0, "executed_count": 0, "rejected_count": 0},
                    {"signal_date": "2024-05-02", "evaluated_count": 1, "qualified_count": 1, "attempted_count": 0, "executed_count": 0, "rejected_count": 0},
                ]
            ),
        )
    finally:
        connection.close()
