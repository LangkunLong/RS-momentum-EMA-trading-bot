from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from core.backtest_engine import PendingEntry, PortfolioSimulator, Trade, _calculate_rs_snapshot
from core.momentum_analysis import calculate_rs_snapshot
from core.strategy_policy import BenchmarkContextV1, CapacityDecision, MarketContextV1


_CONTEXT_SYMBOLS = tuple(f"CTX{number:02d}" for number in range(10))


def _ohlcv(
    dates: pd.DatetimeIndex,
    *,
    opens: list[float],
    closes: list[float],
    lows: list[float] | None = None,
) -> pd.DataFrame:
    effective_lows = (
        lows if lows is not None else [min(open_, close) for open_, close in zip(opens, closes, strict=True)]
    )
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(open_, close) for open_, close in zip(opens, closes, strict=True)],
            "Low": effective_lows,
            "Close": closes,
            "Volume": [1_000_000.0] * len(dates),
        },
        index=dates,
    )


def _with_warmup_and_canonical_entry(
    history: pd.DataFrame,
    signal_date: pd.Timestamp | None,
) -> pd.DataFrame:
    first_session = pd.Timestamp(history.index[0])
    warmup_dates = pd.bdate_range(
        end=first_session - pd.offsets.BDay(1),
        periods=260,
    )
    base_close = float(history["Close"].iloc[0])
    warmup = _ohlcv(
        warmup_dates,
        opens=[base_close] * len(warmup_dates),
        closes=[base_close] * len(warmup_dates),
    )
    combined = pd.concat([warmup, history])
    if signal_date is not None:
        event_close = base_close * 1.02
        combined.loc[signal_date, "Close"] = event_close
        combined.loc[signal_date, "High"] = max(
            float(combined.loc[signal_date, "High"]),
            event_close,
        )
        combined.loc[signal_date, "Volume"] = 1_300_000.0
    return combined


def _signal(symbol: str, signal_date: pd.Timestamp, *, rs_score: float = 90.0) -> dict:
    return {
        "symbol": symbol,
        "signal_date": str(signal_date.date()),
        "rs_score": rs_score,
        "canslim_score": 80.0,
        "signal_reason": "causality regression",
        "buy_signal": True,
    }


def _market_context(session: pd.Timestamp) -> MarketContextV1:
    return MarketContextV1(
        schema_version=1,
        session=session.date().isoformat(),
        oneil_regime="confirmed_uptrend",
        distribution_days=0,
        follow_through=False,
        benchmarks=tuple(
            BenchmarkContextV1(symbol, 0.0, 0.0, 0.0)
            for symbol in ("SPY", "QQQ", "IWM")
        ),
        active_constituent_count=10,
        breadth_above_50_fraction=0.0,
        breadth_50_coverage_fraction=1.0,
        breadth_above_200_fraction=0.0,
        breadth_200_coverage_fraction=1.0,
        median_rs_score=55.0,
        rs_at_least_80_fraction=0.0,
        rs_coverage_fraction=1.0,
    )


def _pending(signal: dict[str, object], capacity: CapacityDecision) -> PendingEntry:
    signal_session = pd.Timestamp(signal["signal_date"])
    return PendingEntry(signal, capacity, _market_context(signal_session))


def _context_closes(dates: pd.DatetimeIndex) -> dict[str, pd.Series]:
    return {
        symbol: pd.Series(
            [100.0 * (1.0 + (offset + 1) * 0.0001) ** day for day in range(len(dates))],
            index=dates,
        )
        for offset, symbol in enumerate((*_CONTEXT_SYMBOLS, "SPY", "QQQ", "IWM"))
    }


def _reference_prices(dates: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    return {
        symbol: _ohlcv(dates, opens=[100.0] * len(dates), closes=[100.0] * len(dates))
        for symbol in ("SPY", "QQQ", "IWM")
    }


@dataclass
class _StaticFetcher:
    prices: dict[str, pd.DataFrame]
    closes: pd.DataFrame

    def fetch_price_data(
        self,
        _tickers: list[str],
        _start_date: pd.Timestamp,
        _end_date: pd.Timestamp,
    ) -> dict[str, pd.DataFrame]:
        return self.prices

    def fetch_rs_universe_closes(
        self,
        _tickers: list[str],
        _start_date: pd.Timestamp,
        _end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        return self.closes


@dataclass
class _RecordingFetcher(_StaticFetcher):
    price_windows: list[tuple[pd.Timestamp, pd.Timestamp]]
    close_windows: list[tuple[pd.Timestamp, pd.Timestamp]]

    def fetch_price_data(
        self,
        tickers: list[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> dict[str, pd.DataFrame]:
        self.price_windows.append((start_date, end_date))
        return {
            symbol: frame.loc[start_date:end_date]
            for symbol, frame in super().fetch_price_data(tickers, start_date, end_date).items()
        }

    def fetch_rs_universe_closes(
        self,
        tickers: list[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        self.close_windows.append((start_date, end_date))
        return super().fetch_rs_universe_closes(tickers, start_date, end_date).loc[
            start_date:end_date
        ]


@dataclass
class _DatedSignals:
    signals: dict[tuple[str, pd.Timestamp], dict]

    def evaluate_market(self, _spy: pd.DataFrame, _eval_date: pd.Timestamp) -> dict[str, bool]:
        return {"market_is_bullish": True}

    def evaluate_symbol(
        self,
        *,
        ticker: str,
        eval_date: pd.Timestamp,
        **_kwargs: object,
    ) -> dict | None:
        return self.signals.get((ticker, eval_date))


def _causal_rs_fixture() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=80)
    return pd.DataFrame(
        {
            f"T{number:02d}": [100.0 * (1.0 + (number + 1) * 0.0005) ** day for day in range(len(dates))]
            for number in range(10)
        },
        index=dates,
    )


def test_public_rs_snapshot_matches_backtest_compatibility_delegate() -> None:
    """Break caught: the public PIT RS snapshot diverged from the legacy engine checkpoint."""
    closes = _causal_rs_fixture()
    day = closes.index[-1]

    assert calculate_rs_snapshot(closes, day) == _calculate_rs_snapshot(closes, day)


def test_fold_history_is_visible_without_emitting_prefold_state_and_runs_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: warmup either leaks outputs or disappears on an isolated fold."""
    evaluation_dates = pd.bdate_range("2026-05-04", periods=30)
    warmup_dates = pd.bdate_range(
        end=evaluation_dates[0] - pd.offsets.BDay(1),
        periods=260,
    )
    all_dates = warmup_dates.append(evaluation_dates)
    lead = _ohlcv(
        all_dates,
        opens=[100.0] * len(all_dates),
        closes=[100.0] * len(all_dates),
    )
    lead.loc[evaluation_dates[0], "Close"] = 102.0
    lead.loc[evaluation_dates[0], "High"] = 102.0
    lead.loc[evaluation_dates[0], "Volume"] = 1_300_000.0
    lead.loc[evaluation_dates[-1], "Close"] = 102.0
    lead.loc[evaluation_dates[-1], "High"] = 102.0
    lead.loc[evaluation_dates[-1], "Volume"] = 1_300_000.0
    fetcher = _RecordingFetcher(
        prices={"LEAD": lead, **_reference_prices(all_dates)},
        closes=pd.DataFrame({"LEAD": lead["Close"], **_context_closes(all_dates)}),
        price_windows=[],
        close_windows=[],
    )
    strategy = _DatedSignals(
        {
            ("LEAD", evaluation_dates[0]): _signal("LEAD", evaluation_dates[0]),
            ("LEAD", evaluation_dates[-1]): _signal("LEAD", evaluation_dates[-1]),
        }
    )
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.008,
        stop_loss_pct=0.08,
        ma_consecutive=999,
        signal_every_n_days=1,
        technical_only=True,
        stagnation_days=999,
        data_fetcher=fetcher,
        strategy=strategy,
    )
    monkeypatch.setattr("core.backtest_engine.get_sp500_tickers", lambda: list(_CONTEXT_SYMBOLS))

    results = [
        simulator.run(
            ["LEAD"],
            start_date=str(evaluation_dates[0].date()),
            end_date=str(evaluation_dates[-1].date()),
            history_start_date=str(warmup_dates[0].date()),
        )
        for _ in range(2)
    ]

    expected_window = (warmup_dates[0], evaluation_dates[-1])
    assert fetcher.price_windows == [expected_window, expected_window]
    assert fetcher.close_windows == [expected_window, expected_window]
    for result in results:
        assert tuple(result.equity_curve.index) == tuple(
            value.date().isoformat() for value in evaluation_dates
        )
        assert result.transaction_log["Date"].tolist() == [
            str(evaluation_dates[1].date()),
            str(evaluation_dates[-1].date()),
        ]
        assert result.transaction_log["Action"].tolist() == ["BUY", "SELL"]
        assert result.transaction_log["Reason"].tolist()[-1] == "end_of_test"
        assert len(result.entry_outcomes) == 1
        assert result.entry_outcomes[0].signal_date == str(evaluation_dates[0].date())
        assert all(
            pd.Timestamp(value) >= evaluation_dates[0]
            for value in result.transaction_log["Date"]
        )
        assert result.config["start_date"] == str(evaluation_dates[0].date())
        assert result.config["history_start_date"] == str(warmup_dates[0].date())
    assert results[0].transaction_log.to_dict("records") == results[1].transaction_log.to_dict("records")
    assert results[0].entry_outcomes == results[1].entry_outcomes
    assert results[0].equity_curve.to_dict() == results[1].equity_curve.to_dict()


def test_pending_open_buy_cannot_spend_same_day_exit_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: close/stop proceeds were available to a buy at that day's open."""
    dates = pd.date_range("2026-01-02", periods=31, freq="B")
    old_lows = [99.0] * len(dates)
    old_lows[2] = 90.0
    old = _with_warmup_and_canonical_entry(
        _ohlcv(
            dates,
            opens=[100.0] * len(dates),
            closes=[100.0] * len(dates),
            lows=old_lows,
        ),
        dates[0],
    )
    new = _with_warmup_and_canonical_entry(
        _ohlcv(dates, opens=[50.0] * len(dates), closes=[50.0] * len(dates)),
        dates[1],
    )
    full_dates = pd.DatetimeIndex(old.index)
    prices = {"OLD": old, "NEW": new, **_reference_prices(full_dates)}
    closes = pd.DataFrame(
        {"OLD": old["Close"], "NEW": new["Close"], **_context_closes(full_dates)}
    )
    strategy = _DatedSignals(
        {
            ("OLD", dates[0]): _signal("OLD", dates[0]),
            ("NEW", dates[1]): _signal("NEW", dates[1]),
        }
    )
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.01,
        stop_loss_pct=0.0101,
        signal_every_n_days=1,
        technical_only=True,
        stagnation_days=999,
        data_fetcher=_StaticFetcher(prices, closes),
        strategy=strategy,
    )
    monkeypatch.setattr("core.backtest_engine.get_sp500_tickers", lambda: list(_CONTEXT_SYMBOLS))

    result = simulator.run(
        ["OLD", "NEW"],
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )

    new_buys = result.transaction_log.loc[
        (result.transaction_log["Ticker"] == "NEW") & (result.transaction_log["Action"] == "BUY")
    ]
    assert new_buys.empty
    assert any(
        outcome.symbol == "NEW" and outcome.outcome == "entry_rejected_no_cash" for outcome in result.entry_outcomes
    )
    assert any(trade.symbol == "OLD" and trade.exit_reason == "stop_loss" for trade in result.trades)


def test_new_open_position_still_observes_same_day_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: moving opening entries ahead of exits could skip entry-day stops."""
    dates = pd.date_range("2026-01-02", periods=31, freq="B")
    lows = [49.0] * len(dates)
    lows[1] = 40.0
    new = _with_warmup_and_canonical_entry(
        _ohlcv(
            dates,
            opens=[50.0] * len(dates),
            closes=[50.0] * len(dates),
            lows=lows,
        ),
        dates[0],
    )
    full_dates = pd.DatetimeIndex(new.index)
    prices = {"NEW": new, **_reference_prices(full_dates)}
    strategy = _DatedSignals({("NEW", dates[0]): _signal("NEW", dates[0])})
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.01,
        stop_loss_pct=0.08,
        signal_every_n_days=1,
        technical_only=True,
        stagnation_days=999,
        data_fetcher=_StaticFetcher(
            prices,
            pd.DataFrame({"NEW": new["Close"], **_context_closes(full_dates)}),
        ),
        strategy=strategy,
    )
    monkeypatch.setattr("core.backtest_engine.get_sp500_tickers", lambda: list(_CONTEXT_SYMBOLS))

    result = simulator.run(
        ["NEW"],
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )

    transactions = result.transaction_log.loc[result.transaction_log["Ticker"] == "NEW"]
    assert transactions["Action"].tolist() == ["BUY", "SELL"]
    assert transactions["Date"].tolist() == [str(dates[1].date()), str(dates[1].date())]
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == pytest.approx(46.0)


def test_entry_sizing_marks_holdings_at_open_then_strictly_prior_close() -> None:
    """Break caught: entry sizing used same-day closes that were unknown at the open."""
    dates = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-04-15"),
            pd.Timestamp("2026-04-16"),
            pd.Timestamp("2026-04-17"),
        ]
    )
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.008,
        stop_loss_pct=0.08,
        technical_only=True,
    )
    simulator._equity = 1_000.0
    simulator._open_positions = {
        "OPEN_MARK": Trade("OPEN_MARK", "2026-04-01", 100.0, 10.0, 90.0),
        "PRIOR_MARK": Trade("PRIOR_MARK", "2026-04-01", 100.0, 10.0, 90.0),
    }
    prices = {
        "OPEN_MARK": _ohlcv(dates, opens=[90.0, 95.0, 100.0], closes=[90.0, 95.0, 500.0]),
        "PRIOR_MARK": _ohlcv(
            dates,
            opens=[80.0, 85.0, float("nan")],
            closes=[80.0, float("nan"), 800.0],
        ),
        "NEW": _ohlcv(dates, opens=[10.0, 10.0, 10.0], closes=[10.0, 10.0, 10.0]),
    }

    simulator._enter_position(
        _pending(
            _signal("NEW", dates[1]),
            CapacityDecision(simulator.max_positions, simulator.enable_eviction),
        ),
        prices,
        dates[2],
    )

    buy = next(row for row in simulator._transactions if row["Action"] == "BUY")
    assert buy["Ticker"] == "NEW"
    assert buy["Value"] == pytest.approx(280.0)
    assert buy["Quantity"] == pytest.approx(28.0)


def test_capped_eviction_selects_and_sells_using_causal_open_price() -> None:
    """Break caught: capped replacement used same-day closes to choose and price eviction."""
    dates = pd.DatetimeIndex([pd.Timestamp("2026-04-16"), pd.Timestamp("2026-04-17")])
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        max_positions=2,
        position_risk_pct=0.008,
        stop_loss_pct=0.08,
        technical_only=True,
        enable_eviction=True,
    )
    simulator._equity = 0.0
    open_winner = Trade("OPEN_WINNER", "2026-04-01", 100.0, 10.0, 90.0)
    open_winner.rs_score = 60.0
    open_loser = Trade("OPEN_LOSER", "2026-04-01", 100.0, 10.0, 90.0)
    open_loser.rs_score = 70.0
    simulator._open_positions = {
        "OPEN_WINNER": open_winner,
        "OPEN_LOSER": open_loser,
    }
    prices = {
        "OPEN_WINNER": _ohlcv(dates, opens=[100.0, 120.0], closes=[100.0, 80.0]),
        "OPEN_LOSER": _ohlcv(
            dates,
            opens=[100.0, float("nan")],
            closes=[90.0, 150.0],
        ),
        "NEW": _ohlcv(dates, opens=[50.0, 50.0], closes=[50.0, 50.0]),
    }

    simulator._enter_position(
        _pending(
            _signal("NEW", dates[0], rs_score=80.0),
            CapacityDecision(simulator.max_positions, simulator.enable_eviction),
        ),
        prices,
        dates[1],
    )

    evicted = next(trade for trade in simulator._trades if trade.exit_reason == "evicted")
    sell = next(row for row in simulator._transactions if row["Action"] == "SELL")
    assert evicted.symbol == "OPEN_LOSER"
    assert evicted.exit_price == pytest.approx(90.0)
    assert sell["Ticker"] == "OPEN_LOSER"
    assert sell["Price"] == pytest.approx(90.0)
    assert set(simulator._open_positions) == {"OPEN_WINNER", "NEW"}


@pytest.mark.parametrize("bar_state", ["missing", "missing_open", "nan_open"])
def test_pivotless_pending_entry_requires_exact_next_session_open(bar_state: str) -> None:
    """Break caught: a pending entry without a pivot fell back to a prior or same-day close."""
    dates = pd.DatetimeIndex([pd.Timestamp("2026-08-21"), pd.Timestamp("2026-08-24")])
    history = _ohlcv(dates, opens=[100.0, 101.0], closes=[100.0, 102.0])
    if bar_state == "missing":
        history = history.iloc[:1]
    elif bar_state == "missing_open":
        history = history.drop(columns="Open")
    else:
        history.loc[dates[1], "Open"] = float("nan")
    simulator = PortfolioSimulator(initial_capital=1_000.0, technical_only=True)

    simulator._enter_position(
        _pending(
            _signal("LEAD", dates[0]),
            CapacityDecision(simulator.max_positions, simulator.enable_eviction),
        ),
        {"LEAD": history},
        dates[1],
    )

    assert simulator._transactions == []
    assert simulator._entry_outcomes[-1].outcome == "entry_rejected_missing_data"
