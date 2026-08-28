"""Execution-boundary regressions for injected backtest strategies."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

import core.backtest_engine as backtest_engine
from core.backtest_engine import PortfolioSimulator


def _entry_history(*, volume_ratio: float = 1.3) -> pd.DataFrame:
    sessions = pd.bdate_range("2024-01-02", periods=60)
    closes = [100.0] * len(sessions)
    closes[-1] = 102.0
    volumes = [1_000_000.0] * len(sessions)
    volumes[-1] = 1_000_000.0 * volume_ratio
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [close + 1.0 for close in closes],
            "Low": [close - 1.0 for close in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=sessions,
    )


def _full_signal(
    *,
    rs_score: float = 85.0,
    entry_composite_score: float = 75.0,
) -> dict[str, object]:
    return {
        "symbol": "LIED",
        "signal_date": "1900-01-01",
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "rs_score": rs_score,
        "entry_composite_score": entry_composite_score,
        "canslim_score": entry_composite_score,
        "signal_reason": "fabricated custom reason",
        "buy_signal_without_market": True,
        "buy_signal": True,
        "technical_setup_eligible": True,
        "technical_blocking_reasons": "",
        "entry_contract_eligible": True,
        "entry_blocking_reasons": "",
        "has_breakout": True,
        "has_volume_surge": True,
        "in_buy_zone": True,
        "price_advanced": True,
        "pivot": 999.0,
        "prior_close": 999.0,
        "event_volume": 999.0,
        "prior_average_volume_50": 1.0,
        "entry_volume_ratio": 999.0,
        "entry_extension": -0.99,
    }


class _StaticSignalStrategy:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def evaluate_market(
        self,
        _benchmark: pd.DataFrame,
        _eval_date: pd.Timestamp,
    ) -> dict[str, bool]:
        return {"market_is_bullish": True}

    def evaluate_symbol(self, **_kwargs: object) -> dict[str, object] | None:
        return None if self._row is None else dict(self._row)


class _CheckpointStrategy(_StaticSignalStrategy):
    def __init__(self, identity: object) -> None:
        super().__init__(None)
        self.checkpoint_identity = identity


class _StaticFetcher:
    def __init__(self, prices: dict[str, pd.DataFrame], closes: pd.DataFrame) -> None:
        self._prices = prices
        self._closes = closes

    def fetch_price_data(
        self,
        _tickers: list[str],
        _start_date: pd.Timestamp,
        _end_date: pd.Timestamp,
    ) -> dict[str, pd.DataFrame]:
        return self._prices

    def fetch_rs_universe_closes(
        self,
        _tickers: list[str],
        _start_date: pd.Timestamp,
        _end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        return self._closes


def _evaluate_custom_row(
    row: dict[str, object],
    history: pd.DataFrame,
    *,
    technical_only: bool = False,
    require_bullish_market: bool = False,
    market_is_bullish: bool = True,
) -> tuple[PortfolioSimulator, list[dict]]:
    simulator = PortfolioSimulator(
        strategy=_StaticSignalStrategy(row),  # type: ignore[arg-type]
        technical_only=technical_only,
        require_bullish_market=require_bullish_market,
        data_fetcher=object(),  # type: ignore[arg-type]
    )
    simulator._regime_tracker = SimpleNamespace(allows_entries=True)
    simulator._ticker_industry = {}
    signals = simulator._evaluate_signals(
        tickers=["AAA"],
        ticker_ohlcv={"AAA": history},
        all_closes=history[["Close"]].rename(columns={"Close": "AAA"}),
        eval_date=history.index[-1],
        market_state={"market_is_bullish": market_is_bullish},
    )
    return simulator, signals


@pytest.mark.parametrize(
    ("rs_score", "entry_composite_score", "reason"),
    [
        (79.9, 75.0, "rs_score_below_threshold"),
        (85.0, 69.9, "composite_score_below_threshold"),
    ],
)
def test_injected_buy_cannot_bypass_full_canonical_score_gates(
    rs_score: float,
    entry_composite_score: float,
    reason: str,
) -> None:
    """Break caught: an injected True boolean bypasses the fixed 80/70 decision."""
    simulator, signals = _evaluate_custom_row(
        _full_signal(
            rs_score=rs_score,
            entry_composite_score=entry_composite_score,
        ),
        _entry_history(),
    )

    assert signals == []
    logged = simulator._signal_rows[-1]
    assert logged["entry_contract_eligible"] is False
    assert logged["buy_signal"] is False
    assert reason in logged["entry_blocking_reasons"].split(",")


def test_injected_technical_claims_are_recomputed_from_exact_session_history() -> None:
    """Break caught: fabricated technical booleans and facts reach the buy queue."""
    simulator, signals = _evaluate_custom_row(
        _full_signal(),
        _entry_history(volume_ratio=1.0),
    )

    assert signals == []
    logged = simulator._signal_rows[-1]
    assert logged["symbol"] == "AAA"
    assert logged["signal_date"] == "2024-03-25"
    assert logged["pivot"] == 100.0
    assert logged["prior_close"] == 100.0
    assert logged["entry_volume_ratio"] == 1.0
    assert logged["entry_extension"] == pytest.approx(0.02)
    assert logged["has_breakout"] is True
    assert logged["has_volume_surge"] is False
    assert logged["technical_setup_eligible"] is False
    assert logged["technical_blocking_reasons"] == "volume_ratio_below_threshold"
    assert logged["entry_contract_eligible"] is False
    assert logged["buy_signal_without_market"] is False
    assert logged["buy_signal"] is False
    assert logged["signal_reason"] == "No Breakout"


def test_injected_full_mode_signal_with_missing_decision_fields_fails_closed() -> None:
    """Break caught: absent C/A/composite values inherit a custom True decision."""
    simulator, signals = _evaluate_custom_row(
        {
            "symbol": "AAA",
            "rs_score": 85.0,
            "canslim_score": 75.0,
            "buy_signal": True,
        },
        _entry_history(),
    )

    assert signals == []
    logged = simulator._signal_rows[-1]
    assert logged["entry_contract_eligible"] is False
    assert logged["entry_blocking_reasons"].split(",") == [
        "current_growth_unavailable",
        "annual_growth_unavailable",
        "composite_score_unavailable",
    ]


def test_injected_technical_only_signal_uses_recomputed_technical_facts() -> None:
    """Break caught: technical-only mode still trusts a custom False boolean."""
    simulator, signals = _evaluate_custom_row(
        {
            "symbol": "WRONG",
            "rs_score": 5.0,
            "canslim_score": 1.0,
            "buy_signal": False,
        },
        _entry_history(),
        technical_only=True,
    )

    assert [pending.signal["symbol"] for pending in signals] == ["AAA"]
    logged = simulator._signal_rows[-1]
    assert logged["technical_setup_eligible"] is True
    assert logged["entry_contract_eligible"] is True
    assert logged["buy_signal_without_market"] is True
    assert logged["buy_signal"] is True


def test_simulator_market_permission_still_blocks_canonical_custom_entry() -> None:
    """Break caught: canonicalizing a custom row accidentally bypasses the EOD market gate."""
    simulator, signals = _evaluate_custom_row(
        _full_signal(),
        _entry_history(),
        require_bullish_market=True,
        market_is_bullish=False,
    )

    assert signals == []
    logged = simulator._signal_rows[-1]
    assert logged["entry_contract_eligible"] is True
    assert logged["buy_signal_without_market"] is True
    assert logged["buy_signal"] is False
    assert simulator._execution_diagnostics["potential_buy_signal_rows_blocked_by_market"] == 1


def _checkpoint_fixture() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    sessions = pd.bdate_range("2024-01-02", periods=35)
    history = pd.DataFrame(
        {
            "Open": [100.0] * len(sessions),
            "High": [101.0] * len(sessions),
            "Low": [99.0] * len(sessions),
            "Close": [100.0] * len(sessions),
            "Volume": [1_000_000.0] * len(sessions),
        },
        index=sessions,
    )
    return {"AAA": history.copy(), "SPY": history.copy()}, pd.DataFrame(
        {"AAA": history["Close"]}, index=sessions
    )


def _checkpoint_simulator(strategy: object) -> PortfolioSimulator:
    prices, closes = _checkpoint_fixture()
    return PortfolioSimulator(
        signal_every_n_days=5,
        technical_only=True,
        data_fetcher=_StaticFetcher(prices, closes),
        strategy=strategy,  # type: ignore[arg-type]
    )


def _checkpoint_run_args(checkpoint: Path) -> dict[str, Any]:
    _, closes = _checkpoint_fixture()
    return {
        "tickers": ["AAA"],
        "start_date": str(closes.index[0].date()),
        "end_date": str(closes.index[-1].date()),
        "checkpoint_path": checkpoint,
        "checkpoint_every_days": 1,
        "checkpoint_code_identity": "custom-strategy-contract",
    }


def _direct_checkpoint_write(path: Path, payload: object) -> None:
    """Keep resume tests independent of intermittent Windows replace locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(backtest_engine._checkpoint_bytes(payload))


def test_custom_checkpoint_run_rejects_absent_explicit_strategy_identity(
    tmp_path: Path,
) -> None:
    """Break caught: resumable custom behavior has only an ambiguous class name."""
    simulator = _checkpoint_simulator(_StaticSignalStrategy(None))

    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        pytest.raises(ValueError, match="custom strategy checkpointing requires.*checkpoint_identity"),
    ):
        simulator.run(**_checkpoint_run_args(tmp_path / "checkpoint.json"))


def test_custom_checkpoint_run_rejects_non_json_strategy_identity(
    tmp_path: Path,
) -> None:
    """Break caught: an unstable executable object enters checkpoint provenance."""
    simulator = _checkpoint_simulator(_CheckpointStrategy({"features": {"a", "b"}}))

    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        pytest.raises(ValueError, match="checkpoint_identity must be JSON-safe"),
    ):
        simulator.run(**_checkpoint_run_args(tmp_path / "checkpoint.json"))


def test_non_checkpoint_custom_run_uses_class_identity_only() -> None:
    """Break caught: checkpoint-only identity validation blocks an ordinary custom run."""
    simulator = _checkpoint_simulator(
        _CheckpointStrategy({"not_json_safe": {"a", "b"}})
    )
    _, closes = _checkpoint_fixture()

    with patch("core.backtest_engine.get_sp500_tickers", return_value=[]):
        result = simulator.run(
            ["AAA"],
            start_date=str(closes.index[0].date()),
            end_date=str(closes.index[-1].date()),
        )

    assert result.config["start_date"] == "2024-01-02"


@pytest.mark.parametrize("completed", [False, True])
def test_different_custom_strategy_identity_cannot_resume_checkpoint(
    tmp_path: Path,
    completed: bool,
) -> None:
    """Break caught: distinct custom behavior resumes the same partial or final state."""
    checkpoint = tmp_path / "checkpoint.json"
    run_args = _checkpoint_run_args(checkpoint)
    origin = _checkpoint_simulator(
        _CheckpointStrategy({"name": "static", "version": 1, "config": {"side": "long"}})
    )

    if completed:
        with (
            patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
            patch(
                "core.backtest_engine._write_checkpoint_json",
                side_effect=_direct_checkpoint_write,
            ),
        ):
            origin.run(**run_args)
    else:
        class StopAfterCheckpoint(RuntimeError):
            pass

        def write_then_stop(path: Path, payload: object) -> None:
            _direct_checkpoint_write(path, payload)
            if isinstance(payload, dict) and not payload.get("completed"):
                raise StopAfterCheckpoint

        with (
            patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
            patch("core.backtest_engine._write_checkpoint_json", side_effect=write_then_stop),
            pytest.raises(StopAfterCheckpoint),
        ):
            origin.run(**run_args)

    incompatible = _checkpoint_simulator(
        _CheckpointStrategy({"name": "static", "version": 2, "config": {"side": "long"}})
    )
    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        pytest.raises(ValueError, match="different strategy identity"),
    ):
        incompatible.run(**run_args, resume=True)


def test_same_custom_strategy_identity_can_resume_partial_checkpoint(tmp_path: Path) -> None:
    """Break caught: stable custom identity is rejected even when behavior matches."""
    checkpoint = tmp_path / "checkpoint.json"
    run_args = _checkpoint_run_args(checkpoint)
    identity = {"name": "static", "version": 1, "config": {"side": "long"}}
    origin = _checkpoint_simulator(_CheckpointStrategy(identity))
    class StopAfterCheckpoint(RuntimeError):
        pass

    def write_then_stop(path: Path, payload: object) -> None:
        _direct_checkpoint_write(path, payload)
        if isinstance(payload, dict) and not payload.get("completed"):
            raise StopAfterCheckpoint

    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        patch("core.backtest_engine._write_checkpoint_json", side_effect=write_then_stop),
        pytest.raises(StopAfterCheckpoint),
    ):
        origin.run(**run_args)

    resumer = _checkpoint_simulator(_CheckpointStrategy(dict(identity)))
    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        patch(
            "core.backtest_engine._write_checkpoint_json",
            side_effect=_direct_checkpoint_write,
        ),
    ):
        result = resumer.run(**run_args, resume=True)

    checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result.config["start_date"] == "2024-01-02"
    assert checkpoint_payload["schema_version"] == 3
    assert checkpoint_payload["completed"] is True
    assert checkpoint_payload["strategy_identity"] == {
        "kind": "custom",
        "module": __name__,
        "qualname": "_CheckpointStrategy",
        "checkpoint_identity": identity,
    }


def test_builtin_strategy_checkpoint_has_stable_versioned_identity(tmp_path: Path) -> None:
    """Break caught: hardening custom checkpoints also rejects the built-in baseline."""
    prices, closes = _checkpoint_fixture()
    simulator = PortfolioSimulator(
        signal_every_n_days=5,
        technical_only=True,
        data_fetcher=_StaticFetcher(prices, closes),
    )
    checkpoint = tmp_path / "checkpoint.json"

    with (
        patch("core.backtest_engine.get_sp500_tickers", return_value=[]),
        patch(
            "core.backtest_engine._write_checkpoint_json",
            side_effect=_direct_checkpoint_write,
        ),
    ):
        result = simulator.run(**_checkpoint_run_args(checkpoint))

    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result.config["start_date"] == "2024-01-02"
    assert payload["schema_version"] == 3
    assert payload["strategy_identity"] == {
        "kind": "built_in",
        "module": "core.backtest_engine",
        "qualname": "CanslimStrategy",
        "version": 1,
    }
