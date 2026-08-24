"""Regressions for fixed CANSLIM entry-threshold authority."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import cash_utilization_optimizer
import enhanced_scanner
import core.backtest_engine as backtest_engine
from core.backtest_engine import (
    PortfolioSimulator,
    SimulationResult,
    _portfolio_checkpoint_fingerprint,
    run_cli,
)
from core.canslim.entry_contract import (
    CanslimEntryFacts,
    evaluate_entry_contract,
)
from core.canslim.m_market_direction import MarketTrend
from core.after_close_snapshot import AfterCloseSnapshot, write_after_close_snapshot
from core.leader_evaluation import FiveYearLeader
from core.pit_baseline_report import build_leader_recall_frame
from core.stock_screening import _classify_canslim_candidate, screen_stocks_canslim_detailed


def _eligible_facts() -> CanslimEntryFacts:
    return CanslimEntryFacts(
        event_close=102.0,
        prior_close=99.0,
        event_volume=1_300_000.0,
        prior_average_volume_50=1_000_000.0,
        pivot=100.0,
        volume_ratio=1.3,
        extension=0.02,
        price_advanced=True,
        has_volume_surge=True,
        in_buy_zone=True,
        eligible=True,
        blocking_reasons=(),
    )


def _market(*, bullish: bool = True) -> MarketTrend:
    return MarketTrend(
        symbol="SPY",
        score=0.8 if bullish else 0.2,
        is_bullish=bullish,
        latest_close=500.0,
        indicators={},
        distribution_days=0,
        follow_through=bullish,
    )


def _view(*, rs_score: float, composite_score: float) -> dict[str, object]:
    decision = evaluate_entry_contract(
        _eligible_facts(),
        current_growth=0.25,
        annual_growth=0.25,
        rs_score=rs_score,
        composite_score=composite_score,
    )
    return {
        "entry_decision": decision,
        "entry_composite_score": composite_score,
        "rs_score": rs_score,
        "total_score": composite_score,
        "metrics": {},
        "market_trend": _market(),
    }


def _result_config(simulator: PortfolioSimulator) -> dict[str, object]:
    return simulator._result_config(
        tickers=["AAA"],
        benchmark="SPY",
        all_closes=pd.DataFrame(columns=["AAA"]),
        start_ts=pd.Timestamp("2024-01-02"),
        end_ts=pd.Timestamp("2024-01-31"),
    )


def test_tighter_legacy_requests_cannot_block_an_exact_canonical_entry() -> None:
    """Break caught: an adapter silently raises the fixed 80/70 entry floors."""
    category, notes = _classify_canslim_candidate(
        _view(rs_score=80.0, composite_score=70.0),
        min_rs_score=90.0,
        min_canslim_score=90.0,
        require_bullish_market=True,
    )

    assert category == "actionable_buy"
    assert notes == []


def test_looser_legacy_requests_cannot_admit_below_canonical_scores() -> None:
    """Break caught: an adapter substitutes caller floors for canonical authority."""
    category, notes = _classify_canslim_candidate(
        _view(rs_score=79.9, composite_score=69.9),
        min_rs_score=0.0,
        min_canslim_score=0.0,
        require_bullish_market=True,
    )

    assert category == "watchlist_candidate"
    assert notes == ["rs_score_below_threshold", "composite_score_below_threshold"]


def test_bulk_scanner_prefilter_uses_canonical_rs_despite_tighter_request() -> None:
    """Break caught: a legacy request prevents RS-85 from reaching canonical evaluation."""
    scores = pd.DataFrame(
        [
            {"Ticker": "ELIGIBLE", "RS_Score": 85.0},
            {"Ticker": "BELOW", "RS_Score": 79.9},
        ]
    )
    evaluated: list[str] = []

    def record_evaluation(*, symbol: str, **_kwargs: object) -> None:
        evaluated.append(symbol)
        return None

    with (
        patch("core.stock_screening.evaluate_market_direction", return_value=_market()),
        patch("core.stock_screening.calculate_rs_scores_for_tickers", return_value=scores),
        patch("core.stock_screening.evaluate_stock_canslim", side_effect=record_evaluation),
        patch("core.stock_screening.MAX_WORKERS", 1),
    ):
        screen_stocks_canslim_detailed(
            symbols=["BELOW", "ELIGIBLE"],
            start_date="2024-01-02",
            min_rs_score=90.0,
            min_canslim_score=90.0,
        )

    assert evaluated == ["ELIGIBLE"]


def test_simulator_config_separates_effective_and_requested_entry_floors() -> None:
    """Break caught: result metadata reports an ignored request as effective policy."""
    simulator = PortfolioSimulator(
        min_rs_score=99.0,
        min_canslim_score=90.0,
        data_fetcher=object(),  # type: ignore[arg-type]
    )

    config = _result_config(simulator)

    assert config["min_rs_score"] == 80.0
    assert config["min_canslim_score"] == 70.0
    assert config["requested_min_rs_score"] == 99.0
    assert config["requested_min_canslim_score"] == 90.0
    assert config["entry_threshold_requests_advisory_only"] is True


def test_nonfinite_advisory_requests_are_inert_and_strict_json_safe() -> None:
    """Break caught: NaN/Infinity advisory values poison result/checkpoint JSON."""
    simulator = PortfolioSimulator(
        min_rs_score=float("nan"),
        min_canslim_score=float("inf"),
        data_fetcher=object(),  # type: ignore[arg-type]
    )

    config = _result_config(simulator)

    assert config["min_rs_score"] == 80.0
    assert config["min_canslim_score"] == 70.0
    assert config["requested_min_rs_score"] is None
    assert config["requested_min_canslim_score"] is None
    json.dumps(config, allow_nan=False)


def _strategy_signal(simulator: PortfolioSimulator) -> dict[str, object] | None:
    sessions = pd.bdate_range("2024-01-02", periods=60)
    history = pd.DataFrame(
        {
            "Open": [100.0] * 60,
            "High": [103.0] * 60,
            "Low": [99.0] * 60,
            "Close": [102.0] * 60,
            "Volume": [1_300_000.0] * 60,
        },
        index=sessions,
    )
    fundamentals = {
        "c_score": 0.9,
        "a_score": 0.9,
        "i_score": 0.8,
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "shares_outstanding": None,
        "quarterly_income": pd.DataFrame(),
    }
    technical = {
        "n_score": 0.9,
        "s_score": 0.9,
        "close": 102.0,
        "has_power_gap": False,
        "power_gap_details": {},
        "entry_facts": _eligible_facts(),
    }
    with (
        patch("core.backtest_engine._evaluate_fundamentals_at_date", return_value=fundamentals),
        patch("core.backtest_engine._evaluate_technical_at_date", return_value=technical),
        patch("core.backtest_engine._compute_canslim_score", return_value=75.0),
        patch("core.backtest_engine._compute_entry_composite_score", return_value=75.0),
    ):
        return simulator.strategy.evaluate_symbol(
            ticker="AAA",
            ticker_ohlcv={"AAA": history},
            all_closes=history[["Close"]].rename(columns={"Close": "AAA"}),
            eval_date=sessions[-1],
            market_state={"m_score": 0.8, "market_is_bullish": True},
            rs_score=85.0,
        )


def test_requested_entry_floors_do_not_change_fingerprint_or_signals() -> None:
    """Break caught: an advisory request forks resume identity or signal output."""
    canonical_request = PortfolioSimulator(
        min_rs_score=80.0,
        min_canslim_score=70.0,
        data_fetcher=object(),  # type: ignore[arg-type]
    )
    tighter_request = PortfolioSimulator(
        min_rs_score=99.0,
        min_canslim_score=99.0,
        data_fetcher=object(),  # type: ignore[arg-type]
    )
    fingerprint_args = {
        "bundle_sha256": None,
        "code_identity": "fixed-code",
        "start_date": pd.Timestamp("2024-01-02"),
        "end_date": pd.Timestamp("2024-01-31"),
        "benchmark": "SPY",
        "universe": ["AAA"],
    }

    canonical_fingerprint = _portfolio_checkpoint_fingerprint(
        simulator=canonical_request, **fingerprint_args
    )
    tighter_fingerprint = _portfolio_checkpoint_fingerprint(
        simulator=tighter_request, **fingerprint_args
    )

    assert canonical_fingerprint == tighter_fingerprint
    assert _strategy_signal(canonical_request) == _strategy_signal(tighter_request)
    assert _strategy_signal(tighter_request)["buy_signal"] is True  # type: ignore[index]


class _ConventionalThresholdStrategy:
    """Test double whose signal decision reads the supported threshold attrs."""

    def __init__(self, *, min_rs_score: float, min_canslim_score: float) -> None:
        self.min_rs_score = min_rs_score
        self.min_canslim_score = min_canslim_score

    def evaluate_symbol(self, **row: float) -> dict[str, bool]:
        return {
            "buy_signal": (
                row["rs_score"] >= self.min_rs_score
                and row["composite_score"] >= self.min_canslim_score
            )
        }


def test_supplied_strategy_is_bound_to_canonical_entry_floors() -> None:
    """Break caught: injected strategy reactivates inert caller thresholds."""
    canonical_strategy = _ConventionalThresholdStrategy(
        min_rs_score=80.0,
        min_canslim_score=70.0,
    )
    tighter_strategy = _ConventionalThresholdStrategy(
        min_rs_score=99.0,
        min_canslim_score=99.0,
    )
    canonical_request = PortfolioSimulator(
        min_rs_score=80.0,
        min_canslim_score=70.0,
        data_fetcher=object(),  # type: ignore[arg-type]
        strategy=canonical_strategy,  # type: ignore[arg-type]
    )
    tighter_request = PortfolioSimulator(
        min_rs_score=99.0,
        min_canslim_score=99.0,
        data_fetcher=object(),  # type: ignore[arg-type]
        strategy=tighter_strategy,  # type: ignore[arg-type]
    )
    fingerprint_args = {
        "bundle_sha256": None,
        "code_identity": "fixed-code",
        "start_date": pd.Timestamp("2024-01-02"),
        "end_date": pd.Timestamp("2024-01-31"),
        "benchmark": "SPY",
        "universe": ["AAA"],
    }

    assert tighter_strategy.min_rs_score == 80.0
    assert tighter_strategy.min_canslim_score == 70.0
    assert tighter_strategy.entry_threshold_requests_advisory_only is True  # type: ignore[attr-defined]
    assert _portfolio_checkpoint_fingerprint(
        simulator=canonical_request, **fingerprint_args
    ) == _portfolio_checkpoint_fingerprint(simulator=tighter_request, **fingerprint_args)
    assert canonical_strategy.evaluate_symbol(
        rs_score=85.0, composite_score=75.0
    ) == tighter_strategy.evaluate_symbol(rs_score=85.0, composite_score=75.0)
    assert tighter_strategy.evaluate_symbol(
        rs_score=85.0, composite_score=75.0
    )["buy_signal"] is True


def _checkpoint_fixture() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    sessions = pd.bdate_range("2024-01-02", periods=35)
    prices = pd.DataFrame(
        {
            "Open": [100.0] * len(sessions),
            "High": [101.0] * len(sessions),
            "Low": [99.0] * len(sessions),
            "Close": [100.0] * len(sessions),
            "Volume": [1_000_000.0] * len(sessions),
        },
        index=sessions,
    )
    return {"AAA": prices.copy(), "SPY": prices.copy()}, pd.DataFrame(
        {"AAA": prices["Close"]}, index=sessions
    )


def _checkpoint_simulator(
    *,
    min_rs_score: float,
    min_canslim_score: float,
    price_data: dict[str, pd.DataFrame],
    closes: pd.DataFrame,
) -> PortfolioSimulator:
    fetcher = MagicMock()
    fetcher.fetch_price_data.return_value = price_data
    fetcher.fetch_rs_universe_closes.return_value = closes
    strategy = MagicMock()
    strategy.checkpoint_identity = {
        "name": "threshold-authority-no-signals",
        "version": 1,
    }
    strategy.evaluate_market.return_value = {"market_is_bullish": True}
    strategy.evaluate_symbol.return_value = None
    return PortfolioSimulator(
        min_rs_score=min_rs_score,
        min_canslim_score=min_canslim_score,
        signal_every_n_days=5,
        technical_only=True,
        data_fetcher=fetcher,
        strategy=strategy,
    )


def test_partial_resume_preserves_origin_advisory_request_metadata(tmp_path: Path) -> None:
    """Break caught: partial resume rewrites origin provenance with inert requests."""
    prices, closes = _checkpoint_fixture()
    checkpoint = tmp_path / "portfolio_checkpoint.json"
    origin = _checkpoint_simulator(
        min_rs_score=91.0,
        min_canslim_score=92.0,
        price_data=prices,
        closes=closes,
    )
    real_write = backtest_engine._write_checkpoint_json

    class StopAfterCheckpoint(RuntimeError):
        pass

    def write_then_stop(path: Path, payload: object) -> None:
        real_write(path, payload)
        if isinstance(payload, dict) and not payload.get("completed"):
            raise StopAfterCheckpoint

    run_args = {
        "tickers": ["AAA"],
        "start_date": str(closes.index[0].date()),
        "end_date": str(closes.index[-1].date()),
        "checkpoint_path": checkpoint,
        "checkpoint_every_days": 1,
        "checkpoint_code_identity": "threshold-authority",
    }
    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        patch("core.backtest_engine.clear_session_cache"),
        patch("core.backtest_engine._write_checkpoint_json", side_effect=write_then_stop),
        pytest.raises(StopAfterCheckpoint),
    ):
        origin.run(**run_args)

    resumer = _checkpoint_simulator(
        min_rs_score=1.0,
        min_canslim_score=2.0,
        price_data=prices,
        closes=closes,
    )
    resumed_checkpoints: list[dict[str, object]] = []

    def capture_resumed_checkpoint(_path: Path, payload: object) -> None:
        assert isinstance(payload, dict)
        resumed_checkpoints.append(payload)

    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        patch("core.backtest_engine.clear_session_cache"),
        patch(
            "core.backtest_engine._write_checkpoint_json",
            side_effect=capture_resumed_checkpoint,
        ),
    ):
        result = resumer.run(**run_args, resume=True)

    final_checkpoint = resumed_checkpoints[-1]
    assert all(
        payload["origin_requested_min_rs_score"] == 91.0
        and payload["origin_requested_min_canslim_score"] == 92.0
        for payload in resumed_checkpoints
    )
    assert final_checkpoint["origin_requested_min_rs_score"] == 91.0
    assert final_checkpoint["origin_requested_min_canslim_score"] == 92.0
    assert result.config["requested_min_rs_score"] == 91.0
    assert result.config["requested_min_canslim_score"] == 92.0


def test_signal_funnel_rs_pass_is_canonical_despite_legacy_config() -> None:
    """Break caught: funnel attribution applies a stale requested RS floor."""
    result = SimulationResult(
        signal_log=pd.DataFrame(
            [{"symbol": "AAA", "signal_date": "2024-01-02", "rs_score": 85.0}]
        ),
        config={"min_rs_score": 99.0},
    )

    assert result.signal_funnel["rs_pass"] == 1


def test_leader_recall_rs_attribution_ignores_legacy_threshold_arguments() -> None:
    """Break caught: recall blames RS-85 because a caller requested RS-99."""
    leader = FiveYearLeader(
        "AAA",
        date(2024, 1, 2),
        date(2024, 1, 31),
        50.0,
        1,
        True,
        date(2024, 1, 2),
    )
    signals = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "signal_date": "2024-01-02",
                "buy_signal": False,
                "current_growth": 0.30,
                "annual_growth": 0.30,
                "rs_score": 85.0,
                "has_breakout": True,
                "has_volume_surge": True,
                "in_buy_zone": True,
                "entry_composite_score": 75.0,
            },
            {
                "symbol": "AAA",
                "signal_date": "2024-01-03",
                "buy_signal": False,
                "current_growth": 0.30,
                "annual_growth": 0.30,
                "rs_score": 79.9,
                "has_breakout": True,
                "has_volume_surge": True,
                "in_buy_zone": True,
                "entry_composite_score": 75.0,
            },
        ]
    )

    recall = build_leader_recall_frame(
        (leader,),
        signals,
        pd.DataFrame(),
        start_date=date(2024, 1, 2),
        min_c_a_growth=0.25,
        min_rs_score=99.0,
        min_canslim_score=99.0,
    )

    assert recall.loc[0, "rs_fail_count"] == 1


def test_after_close_artifact_declares_technical_ranking_only(tmp_path: Path) -> None:
    """Break caught: after-close metadata implies full CANSLIM qualification."""
    snapshot = AfterCloseSnapshot(
        as_of_session=date(2024, 1, 31),
        market=_market(),
        rows=(),
        summary={
            "total_symbols": 0,
            "technical_eligible": 0,
            "tomorrow_executable": 0,
            "blocked": 0,
        },
    )

    _csv_path, json_path = write_after_close_snapshot(
        snapshot, tmp_path, generated_at=datetime(2024, 1, 31, 22, 0)
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["artifact_provenance"] == {
        "advisory_only": True,
        "calculation": "completed_daily_bars",
        "full_canslim_entry": False,
        "signal_scope": "technical_ranking_only",
    }


def test_backtest_help_labels_legacy_entry_floor_options_as_ignored(capsys) -> None:
    """Break caught: CLI help presents fixed entry floors as tunable strategy knobs."""
    with pytest.raises(SystemExit) as exc_info:
        run_cli(["--help"])

    help_text = capsys.readouterr().out.lower()
    assert exc_info.value.code == 0
    assert "--min-rs" in help_text
    assert "deprecated advisory" in help_text
    assert "fixed canonical rs 80" in help_text
    assert "fixed canonical composite 70" in help_text


def test_scanner_help_labels_python_threshold_inputs_as_advisory() -> None:
    """Break caught: scanner help suggests legacy Python inputs control entries."""
    help_text = " ".join(enhanced_scanner.build_parser().format_help().lower().split())

    assert "legacy min_rs_score/min_canslim_score" in help_text
    assert "deprecated advisory" in help_text
    assert "fixed canonical 80/70" in help_text


def test_cash_optimizer_help_limits_thresholds_to_cash_deployment() -> None:
    """Break caught: optimizer help can be read as CANSLIM-floor optimization."""
    help_text = cash_utilization_optimizer.build_parser().format_help().lower()

    assert "cash-deployment thresholds only" in help_text
    assert "never tunes the fixed canslim entry floors" in help_text
