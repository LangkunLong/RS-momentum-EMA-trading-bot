from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from core.backtest_engine import PortfolioSimulator, Trade


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


def _signal(symbol: str, signal_date: pd.Timestamp, *, rs_score: float = 90.0) -> dict:
    return {
        "symbol": symbol,
        "signal_date": str(signal_date.date()),
        "rs_score": rs_score,
        "canslim_score": 80.0,
        "signal_reason": "causality regression",
        "buy_signal": True,
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


def test_pending_open_buy_cannot_spend_same_day_exit_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break caught: close/stop proceeds were available to a buy at that day's open."""
    dates = pd.date_range("2026-01-02", periods=31, freq="B")
    old_lows = [99.0] * len(dates)
    old_lows[2] = 90.0
    old = _ohlcv(
        dates,
        opens=[100.0] * len(dates),
        closes=[100.0] * len(dates),
        lows=old_lows,
    )
    new = _ohlcv(dates, opens=[50.0] * len(dates), closes=[50.0] * len(dates))
    spy = _ohlcv(dates, opens=[100.0] * len(dates), closes=[100.0] * len(dates))
    prices = {"OLD": old, "NEW": new, "SPY": spy}
    closes = pd.DataFrame({"OLD": old["Close"], "NEW": new["Close"]}, index=dates)
    strategy = _DatedSignals(
        {
            ("OLD", dates[0]): _signal("OLD", dates[0]),
            ("NEW", dates[1]): _signal("NEW", dates[1]),
        }
    )
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.08,
        stop_loss_pct=0.08,
        signal_every_n_days=1,
        technical_only=True,
        stagnation_days=999,
        data_fetcher=_StaticFetcher(prices, closes),
        strategy=strategy,
    )
    monkeypatch.setattr("core.backtest_engine.get_sp500_tickers", lambda: [])

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
    new = _ohlcv(
        dates,
        opens=[50.0] * len(dates),
        closes=[50.0] * len(dates),
        lows=lows,
    )
    spy = _ohlcv(dates, opens=[100.0] * len(dates), closes=[100.0] * len(dates))
    prices = {"NEW": new, "SPY": spy}
    strategy = _DatedSignals({("NEW", dates[0]): _signal("NEW", dates[0])})
    simulator = PortfolioSimulator(
        initial_capital=1_000.0,
        position_risk_pct=0.08,
        stop_loss_pct=0.08,
        signal_every_n_days=1,
        technical_only=True,
        stagnation_days=999,
        data_fetcher=_StaticFetcher(prices, pd.DataFrame({"NEW": new["Close"]}, index=dates)),
        strategy=strategy,
    )
    monkeypatch.setattr("core.backtest_engine.get_sp500_tickers", lambda: [])

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
        position_risk_pct=0.01,
        stop_loss_pct=0.10,
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

    simulator._enter_position(_signal("NEW", dates[1]), prices, dates[2])

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
        position_risk_pct=0.01,
        stop_loss_pct=0.10,
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

    simulator._enter_position(_signal("NEW", dates[0], rs_score=80.0), prices, dates[1])

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

    simulator._enter_position(_signal("LEAD", dates[0]), {"LEAD": history}, dates[1])

    assert simulator._transactions == []
    assert simulator._entry_outcomes[-1].outcome == "entry_rejected_missing_data"
