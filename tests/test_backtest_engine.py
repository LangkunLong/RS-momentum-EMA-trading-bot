from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from core.canslim.entry_contract import CanslimEntryFacts
from core.strategy_policy import (
    AllocationDecision,
    CapacityDecision,
    EntryDecision,
    EntrySnapshot,
    EvictionDecision,
    ExitAction,
    ExitDecision,
    ExitSnapshot,
)
from core.strategy_policy.runtime import InProcessPolicyClient
from core.backtest_engine import (
    CanslimStrategy,
    DataFetcher,
    PendingEntry,
    PerformanceReport,
    ProjectedEntryTransition,
    SimulationResult,
    _calculate_rs_snapshot,
    _resolve_universe,
    export_trade_charts,
)
from backtest_pnl import PortfolioSimulator, Trade
from backtest import _calculate_rs_at_date


class _ExplodingFetcher:
    def fetch_price_data(self, *_args, **_kwargs):
        raise RuntimeError("injected fetch failure")


class _CountingClient(InProcessPolicyClient):
    def __init__(self, closed: list[int]) -> None:
        self._closed_events = closed

    def close(self) -> None:
        self._closed_events.append(1)


def test_policy_client_factory_creates_and_closes_once_per_run() -> None:
    made: list[_CountingClient] = []
    closed: list[int] = []

    def factory() -> _CountingClient:
        client = _CountingClient(closed)
        made.append(client)
        return client

    simulator = PortfolioSimulator(
        data_fetcher=_ExplodingFetcher(),
        policy_client_factory=factory,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="injected fetch failure"):
            simulator.run(
                ["AAPL"],
                start_date="2021-06-25",
                end_date="2021-09-20",
            )
    assert len({id(client) for client in made}) == 2
    assert closed == [1, 1]
    assert simulator._policy_client is None


def _make_ohlcv(
    n: int = 10,
    *,
    start_price: float = 100.0,
    close_value: float | None = None,
    high_value: float | None = None,
    low_value: float | None = None,
) -> pd.DataFrame:
    dates = pd.date_range(end=datetime(2026, 4, 17).date(), periods=n, freq="B")
    close = close_value if close_value is not None else start_price
    high = high_value if high_value is not None else close * 1.01
    low = low_value if low_value is not None else close * 0.99
    return pd.DataFrame(
        {
            "Open": [close] * n,
            "High": [high] * n,
            "Low": [low] * n,
            "Close": [close] * n,
            "Volume": [1_000_000] * n,
        },
        index=dates,
    )


def _make_canonical_entry_ohlcv(event_close: float) -> pd.DataFrame:
    pivot = event_close / 1.02
    history = _make_ohlcv(n=60, close_value=pivot)
    history.loc[history.index[-1], ["Open", "High", "Close", "Volume"]] = [
        event_close,
        event_close * 1.01,
        event_close,
        1_300_000,
    ]
    return history


def _canonical_full_signal(
    symbol: str,
    *,
    rs_score: float,
    canslim_score: float,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "current_growth": 0.30,
        "annual_growth": 0.30,
        "rs_score": rs_score,
        "entry_composite_score": canslim_score,
        "canslim_score": canslim_score,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }


def _eligible_entry_facts() -> CanslimEntryFacts:
    """Return a canonical completed-session setup for strategy mocks."""
    return CanslimEntryFacts(
        event_close=150.0,
        prior_close=149.0,
        event_volume=1_950_000.0,
        prior_average_volume_50=1_500_000.0,
        pivot=147.0,
        volume_ratio=1.3,
        extension=150.0 / 147.0 - 1.0,
        price_advanced=True,
        has_volume_surge=True,
        in_buy_zone=True,
        eligible=True,
        blocking_reasons=(),
    )


def test_take_profit_scale_out_fires_all_three_tiers_on_gap_up() -> None:
    """When high clears all 3 tier thresholds in one bar, all 3 tiers fire."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-04-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 15  # becomes 16 after increment — outside 15-day 8-week-hold window
    sim._open_positions["NVDA"] = trade

    # high=121 clears tier1(110), tier2(115), tier3(120)
    ohlcv = _make_ohlcv(n=5, close_value=121.0, high_value=121.0, low_value=109.0)
    sim._check_exits("NVDA", ohlcv, ohlcv.index[-1])

    assert "NVDA" in sim._open_positions
    result = sim._open_positions["NVDA"]
    assert result.scale_out_tier == 3
    assert result.remaining_qty == pytest.approx(2.5)  # 25% of 10 remains
    # pnl: (110-100)*2.5 + (115-100)*2.5 + (120-100)*2.5 = 25+37.5+50 = 112.5
    assert result.realized_pnl == pytest.approx(112.5)
    sell_reasons = [tx["Reason"] for tx in sim._transactions if tx["Action"] == "SELL"]
    assert sell_reasons.count("take_profit_scale_out") == 3


def test_scale_out_tier1_only_when_gain_between_10_and_15_pct() -> None:
    """Only tier 1 fires when high is between 10% and 15% above entry."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="AAPL", entry_date="2026-04-01", entry_price=100.0, qty=8.0, stop_price=92.0)
    sim._open_positions["AAPL"] = trade

    # high=112 clears tier1(110) but NOT tier2(115)
    ohlcv = _make_ohlcv(n=3, close_value=112.0, high_value=112.0, low_value=109.0)
    sim._check_exits("AAPL", ohlcv, ohlcv.index[-1])

    assert "AAPL" in sim._open_positions
    result = sim._open_positions["AAPL"]
    assert result.scale_out_tier == 1
    assert result.remaining_qty == pytest.approx(6.0)  # sold 25% of 8 = 2 shares
    assert result.realized_pnl == pytest.approx(20.0)  # (110-100)*2
    sell_txns = [tx for tx in sim._transactions if tx["Action"] == "SELL"]
    assert len(sell_txns) == 1


def test_scale_out_remaining_qty_is_25_pct_of_original_after_tier3() -> None:
    """After all 3 tiers, exactly 25% of original qty remains."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    qty = 20.0
    trade = Trade(symbol="MSFT", entry_date="2026-04-01", entry_price=50.0, qty=qty, stop_price=46.0)
    trade.days_held = 15  # becomes 16 after increment — outside 15-day 8-week-hold window
    sim._open_positions["MSFT"] = trade

    ohlcv = _make_ohlcv(n=3, close_value=62.0, high_value=62.0, low_value=51.0)
    sim._check_exits("MSFT", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["MSFT"]
    assert result.scale_out_tier == 3
    assert result.remaining_qty == pytest.approx(qty * 0.25)


def test_time_stop_exits_stagnant_position() -> None:
    sim = PortfolioSimulator(
        initial_capital=100_000.0,
        stagnation_days=20,
        stagnation_threshold_pct=0.05,
    )
    trade = Trade(symbol="MSFT", entry_date="2026-03-20", entry_price=100.0, qty=10.0, stop_price=93.0)
    trade.days_held = 19
    trade.peak_close = 103.0
    sim._open_positions["MSFT"] = trade

    ohlcv = _make_ohlcv(n=5, close_value=102.0, high_value=103.0, low_value=101.0)
    sim._check_exits("MSFT", ohlcv, ohlcv.index[-1])

    assert "MSFT" not in sim._open_positions
    assert sim._trades[-1].exit_reason == "time_stop"


def test_stop_moves_to_breakeven_after_eight_percent_gain() -> None:
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-04-01", entry_price=100.0, qty=10.0, stop_price=93.0)
    sim._open_positions["NVDA"] = trade

    dates = pd.date_range("2026-04-01", periods=2, freq="B")
    first = pd.DataFrame(
        {
            "Open": [100.0, 108.5],
            "High": [108.5, 109.0],
            "Low": [99.0, 99.5],
            "Close": [107.0, 100.5],
            "Volume": [1_000_000, 1_000_000],
        },
        index=dates,
    )

    sim._check_exits("NVDA", first, dates[0])
    assert sim._open_positions["NVDA"].stop_price == pytest.approx(100.0)
    assert sim._open_positions["NVDA"].breakeven_armed is True

    sim._check_exits("NVDA", first, dates[1])
    assert "NVDA" not in sim._open_positions
    assert sim._trades[-1].exit_reason == "stop_loss"
    assert sim._trades[-1].exit_price == pytest.approx(100.0)


def test_profitable_trade_trails_stop_with_21_day_ema() -> None:
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    dates = pd.date_range("2026-03-02", periods=23, freq="B")
    closes = [100.0 + i for i in range(23)]
    ohlcv = pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=dates,
    )

    trade = Trade(
        symbol="MSFT",
        entry_date=str(dates[0].date()),
        entry_price=100.0,
        qty=10.0,
        stop_price=93.0,
        breakeven_armed=True,
    )
    sim._open_positions["MSFT"] = trade

    sim._check_exits("MSFT", ohlcv, dates[21])
    raised_stop = sim._open_positions["MSFT"].stop_price
    assert raised_stop > 100.0
    assert sim._open_positions["MSFT"].ema_trailing_active is True

    exit_bar = ohlcv.copy()
    exit_bar.loc[dates[22], "Low"] = raised_stop - 0.5
    sim._check_exits("MSFT", exit_bar, dates[22])

    assert "MSFT" not in sim._open_positions
    assert sim._trades[-1].exit_reason == "stop_loss"
    assert sim._trades[-1].exit_price == pytest.approx(raised_stop)


def test_weekly_holdings_snapshot_records_open_positions() -> None:
    sim = PortfolioSimulator(initial_capital=100_000.0)
    sim._equity = 90_000.0
    sim._open_positions["AAPL"] = Trade("AAPL", "2026-04-01", 100.0, 5.0, 93.0)
    sim._open_positions["NVDA"] = Trade("NVDA", "2026-04-01", 100.0, 2.0, 93.0)

    friday = pd.Timestamp("2026-04-17")
    trading_days = pd.DatetimeIndex([pd.Timestamp("2026-04-16"), friday])
    ticker_ohlcv = {
        "AAPL": _make_ohlcv(n=2, close_value=110.0),
        "NVDA": _make_ohlcv(n=2, close_value=120.0),
    }

    sim._record_weekly_holdings(ticker_ohlcv, friday, trading_days)

    assert len(sim._weekly_snapshots) == 1
    snapshot = sim._weekly_snapshots[0]
    assert snapshot["Holding_Count"] == 2
    assert snapshot["Holdings"] == "AAPL,NVDA"


def test_data_fetcher_reuses_sqlite_cache(tmp_path: Path) -> None:
    fetcher = DataFetcher(db_path=str(tmp_path / "cache.sqlite3"))
    start = pd.Timestamp("2026-01-01")
    end = pd.Timestamp("2026-04-01")
    data = {"NVDA": _make_ohlcv(n=20)}

    with patch("core.backtest_engine._download_price_data", return_value=data) as mocked:
        first = fetcher.fetch_price_data(["NVDA"], start, end)
        second = fetcher.fetch_price_data(["NVDA"], start, end)

    assert mocked.call_count == 1
    assert list(first.keys()) == ["NVDA"]
    assert list(second.keys()) == ["NVDA"]


def test_data_fetcher_uses_bulk_price_path_for_large_universe(tmp_path: Path) -> None:
    fetcher = DataFetcher(db_path=str(tmp_path / "cache.sqlite3"))
    start = pd.Timestamp("2026-01-01")
    end = pd.Timestamp("2026-04-01")
    tickers = [f"T{i}" for i in range(30)]
    bulk_data = {ticker: _make_ohlcv(n=20) for ticker in tickers}

    with (
        patch("core.backtest_engine.fetch_bulk_ohlcv", return_value=bulk_data) as mocked_bulk,
        patch("core.backtest_engine._download_price_data") as mocked_single,
    ):
        result = fetcher.fetch_price_data(tickers, start, end)

    assert mocked_bulk.call_count == 1
    mocked_single.assert_not_called()
    assert set(result.keys()) == set(tickers)


def test_performance_report_computes_annualized_return() -> None:
    dates = pd.date_range("2023-04-03", "2026-04-01", freq="B")
    curve = pd.Series([100_000 + i * 50 for i in range(len(dates))], index=dates.astype(str))
    metrics = PerformanceReport.compute_metrics(curve)

    assert metrics["total_return_pct"] > 0
    assert metrics["annualized_return_pct"] > 0


def test_technical_only_mode_allows_buy_without_fundamentals() -> None:
    strategy = CanslimStrategy(technical_only=True, min_technical_score=70.0)
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    ticker_ohlcv = {
        "NVDA": pd.DataFrame(
            {
                "Open": [100.0] * 80,
                "High": [101.0] * 80,
                "Low": [99.0] * 80,
                "Close": [100.0 + i for i in range(80)],
                "Volume": [1_500_000] * 80,
            },
            index=dates,
        )
    }
    all_closes = pd.DataFrame({"NVDA": [100.0 + i for i in range(80)]}, index=dates)

    with (
        patch("core.backtest_engine._calculate_rs_at_date", return_value=95.0),
        patch("core.backtest_engine._evaluate_fundamentals_at_date", return_value={
            "c_score": 0.0, "a_score": 0.0, "i_score": 0.5, "current_growth": None, "annual_growth": None, "shares_outstanding": None
        }),
        patch("core.backtest_engine._evaluate_technical_at_date", return_value={
            "n_score": 0.95, "s_score": 0.85, "close": 150.0, "is_breakout": True, "has_volume_surge": True,
            "has_power_gap": False, "power_gap_details": {}, "entry_facts": _eligible_entry_facts(),
        }),
    ):
        row = strategy.evaluate_symbol(
            ticker="NVDA",
            ticker_ohlcv=ticker_ohlcv,
            all_closes=all_closes,
            eval_date=dates[-1],
            market_state={"m_score": 0.9, "market_is_bullish": True, "distribution_days": 0, "follow_through": True},
        )

    assert row is not None
    assert row["technical_score"] >= 70.0
    assert row["buy_signal"] is True


def test_technical_only_mode_skips_fundamental_fetch() -> None:
    strategy = CanslimStrategy(technical_only=True)
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    ticker_ohlcv = {
        "NVDA": pd.DataFrame(
            {
                "Open": [100.0] * 80,
                "High": [101.0] * 80,
                "Low": [99.0] * 80,
                "Close": [100.0 + i for i in range(80)],
                "Volume": [1_500_000] * 80,
            },
            index=dates,
        )
    }
    all_closes = pd.DataFrame({"NVDA": [100.0 + i for i in range(80)]}, index=dates)

    with (
        patch("core.backtest_engine._calculate_rs_at_date", return_value=95.0),
        patch("core.backtest_engine._evaluate_fundamentals_at_date") as mocked_fund,
        patch("core.backtest_engine._evaluate_technical_at_date", return_value={
            "n_score": 0.95, "s_score": 0.85, "close": 150.0, "is_breakout": True, "has_volume_surge": True,
            "has_power_gap": False, "power_gap_details": {}, "entry_facts": _eligible_entry_facts(),
        }),
    ):
        strategy.evaluate_symbol(
            ticker="NVDA",
            ticker_ohlcv=ticker_ohlcv,
            all_closes=all_closes,
            eval_date=dates[-1],
            market_state={"m_score": 0.9, "market_is_bullish": True, "distribution_days": 0, "follow_through": True},
        )

    mocked_fund.assert_not_called()


def test_entry_policy_canslim_strategy_receives_institutional_reweighted_snapshot() -> None:
    """Break caught: the built-in strategy could bypass policy after scoring facts."""
    snapshots: list[EntrySnapshot] = []

    class Client(InProcessPolicyClient):
        def evaluate_entry(self, snapshot: EntrySnapshot) -> EntryDecision:
            snapshots.append(snapshot)
            return EntryDecision(False, True, (77.0, 88.0), ("policy_block",))

    strategy = CanslimStrategy()
    strategy._policy_client_provider = Client
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    ticker_ohlcv = {"AAA": _make_ohlcv(n=80, close_value=100.0)}
    ticker_ohlcv["AAA"].index = dates
    all_closes = pd.DataFrame({"AAA": [100.0] * 80}, index=dates)

    with (
        patch("core.backtest_engine._calculate_rs_at_date", return_value=90.0),
        patch(
            "core.backtest_engine._evaluate_fundamentals_at_date",
            return_value={
                "c_score": 0.8,
                "a_score": 0.8,
                "i_score": 0.5,
                "current_growth": 0.30,
                "annual_growth": 0.30,
                "shares_outstanding": None,
                "institutional_data_available": False,
                "quarterly_income": pd.DataFrame(),
            },
        ),
        patch(
            "core.backtest_engine._evaluate_technical_at_date",
            return_value={
                "n_score": 0.8,
                "s_score": 0.8,
                "close": 102.0,
                "has_power_gap": False,
                "power_gap_details": {},
                "entry_facts": _eligible_entry_facts(),
            },
        ),
    ):
        row = strategy.evaluate_symbol(
            ticker="AAA",
            ticker_ohlcv=ticker_ohlcv,
            all_closes=all_closes,
            eval_date=dates[-1],
            market_state={
                "m_score": 0.8,
                "market_is_bullish": True,
                "distribution_days": 0,
                "follow_through": False,
            },
        )

    assert len(snapshots) == 1
    assert snapshots[0].institutional_data_available is False
    assert snapshots[0].canslim_score == pytest.approx(82.2222222222)
    assert not hasattr(snapshots[0], "symbol")
    assert not hasattr(snapshots[0], "signal_date")
    assert row is not None
    assert (row["buy_signal"], row["canslim_score"], row["rs_score"]) == (
        False,
        77.0,
        88.0,
    )


def test_export_trade_charts_writes_html(tmp_path: Path) -> None:
    trades = [Trade("NVDA", "2026-04-01", 100.0, 10.0, 93.0, exit_date="2026-04-10", exit_price=110.0, exit_reason="ma_violation")]
    tx = pd.DataFrame(
        [
            {"Date": "2026-04-01", "Ticker": "NVDA", "Action": "BUY", "Price": 100.0, "Quantity": 10.0, "Value": 1000.0, "Reason": "Volume Breakout"},
            {"Date": "2026-04-10", "Ticker": "NVDA", "Action": "SELL", "Price": 110.0, "Quantity": 10.0, "Value": 1100.0, "Reason": "ma_violation"},
        ]
    )
    result = SimulationResult(
        trades=trades,
        transaction_log=tx,
        config={"start_date": "2026-04-01", "end_date": "2026-04-15", "technical_only": True},
    )
    fetcher = DataFetcher(db_path=str(tmp_path / "cache.sqlite3"))
    ohlcv = _make_ohlcv(n=20, close_value=105.0)
    with patch.object(fetcher, "fetch_price_data", return_value={"NVDA": ohlcv}):
        files = export_trade_charts(result, output_dir=str(tmp_path / "charts"), data_fetcher=fetcher)

    assert len(files) == 1
    assert Path(files[0]).exists()


def test_rs_snapshot_matches_per_ticker_calculation() -> None:
    dates = pd.date_range("2025-01-01", periods=300, freq="B")
    closes = pd.DataFrame(
        {
            "AAA": [100.0 * (1.002 ** i) for i in range(300)],
            "BBB": [100.0 * (1.001 ** i) for i in range(300)],
            "CCC": [100.0 * (0.9995 ** i) for i in range(300)],
            "DDD": [100.0 * (1.0002 ** i) for i in range(300)],
            "EEE": [100.0 * (1.0008 ** i) for i in range(300)],
            "FFF": [100.0 * (1.0015 ** i) for i in range(300)],
            "GGG": [100.0 * (0.9998 ** i) for i in range(300)],
            "HHH": [100.0 * (1.0011 ** i) for i in range(300)],
            "III": [100.0 * (1.0004 ** i) for i in range(300)],
            "JJJ": [100.0 * (1.0018 ** i) for i in range(300)],
        },
        index=dates,
    )
    eval_date = dates[-1]

    snapshot = _calculate_rs_snapshot(closes, eval_date)
    direct = _calculate_rs_at_date(closes, "AAA", eval_date)

    assert "AAA" in snapshot
    assert snapshot["AAA"] == pytest.approx(direct)


def test_resolve_universe_supports_nasdaq100_and_russell2000() -> None:
    with patch("core.backtest_engine.get_all_index_tickers") as mocked:
        mocked.side_effect = lambda indices, force_refresh=True: [indices[0].upper()]

        nasdaq = _resolve_universe("nasdaq100")
        russell = _resolve_universe("russell2000")

    assert nasdaq == ["NASDAQ100"]
    assert russell == ["RUSSELL2000"]


def test_eight_week_hold_triggered_by_20pct_gain_in_3_weeks() -> None:
    """20%+ gain within 15 trading days sets eight_week_hold=True and suppresses tier exits."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="CRWD", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 13  # will become 14 after increment in _check_exits
    sim._open_positions["CRWD"] = trade

    # close=122 → 22% gain, within 15-day window → should trigger hold
    ohlcv = _make_ohlcv(n=20, close_value=122.0, high_value=122.0, low_value=109.0)
    sim._check_exits("CRWD", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["CRWD"]
    assert result.eight_week_hold is True
    assert result.scale_out_tier == 0  # no tiers fired
    assert result.remaining_qty == pytest.approx(10.0)  # nothing sold


def test_eight_week_hold_not_triggered_after_3_week_window() -> None:
    """20%+ gain after 15 trading days does NOT trigger the 8-week hold."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 15  # will become 16 after increment — outside window
    sim._open_positions["NVDA"] = trade

    # close=122 → 22% gain, but day 16 is outside the 15-day window
    ohlcv = _make_ohlcv(n=20, close_value=122.0, high_value=122.0, low_value=109.0)
    sim._check_exits("NVDA", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["NVDA"]
    assert result.eight_week_hold is False
    assert result.scale_out_tier == 3  # all 3 tiers fired normally


def test_eight_week_hold_releases_after_40_bars_and_tiers_resume() -> None:
    """Hold expires on bar 40; scale_out_tier resets so tiers can fire."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="MU", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 39  # will become 40 after increment — release fires
    trade.eight_week_hold = True
    trade.scale_out_tier = 0
    sim._open_positions["MU"] = trade

    # price at 25% gain — all 3 tiers would fire once hold releases
    ohlcv = _make_ohlcv(n=45, close_value=125.0, high_value=125.0, low_value=109.0)
    sim._check_exits("MU", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["MU"]
    assert result.eight_week_hold is False
    assert result.scale_out_tier == 3  # all 3 tiers fired after release
    assert result.remaining_qty == pytest.approx(2.5)


def test_stop_loss_fires_during_eight_week_hold() -> None:
    """Hard stop-loss is NEVER suppressed by the 8-week hold."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="VST", entry_date="2026-01-01", entry_price=100.0, qty=10.0, stop_price=92.0)
    trade.days_held = 5
    trade.eight_week_hold = True
    sim._open_positions["VST"] = trade

    # low drops below stop
    ohlcv = _make_ohlcv(n=10, close_value=90.0, high_value=91.0, low_value=89.0)
    sim._check_exits("VST", ohlcv, ohlcv.index[-1])

    assert "VST" not in sim._open_positions
    assert sim._trades[-1].exit_reason == "stop_loss"


def _make_full_sim(
    positions: dict,
    capital: float = 100_000.0,
) -> tuple:
    """Build a simulator with pre-loaded open positions and matching OHLCV data.
    positions: {symbol: (entry_price, rs_score, current_close)}
    """
    sim = PortfolioSimulator(
        initial_capital=capital,
        max_positions=5,
        stagnation_days=999,
    )
    ohlcv_map: dict = {}
    for sym, (entry_px, rs, close_px) in positions.items():
        trade = Trade(
            symbol=sym,
            entry_date="2026-01-01",
            entry_price=entry_px,
            qty=10.0,
            stop_price=entry_px * 0.92,
        )
        trade.rs_score = rs
        sim._open_positions[sym] = trade
        ohlcv_map[sym] = _make_ohlcv(n=10, close_value=close_px)
    return sim, ohlcv_map


def test_full_portfolio_signal_reaches_eviction_and_replaces_lower_rs_position() -> None:
    """A ranked buy signal must reach eviction even when there are no open slots."""
    sim, ohlcv_map = _make_full_sim({"MSFT": (100.0, 60.0, 95.0)})
    sim.max_positions = 1
    sim.enable_eviction = True
    sim._regime_tracker = SimpleNamespace(allows_entries=True)
    sim._ticker_industry = {}

    signal = _canonical_full_signal("GEV", rs_score=90.0, canslim_score=80.0)
    sim.strategy = SimpleNamespace(evaluate_symbol=lambda **_kwargs: signal)
    ohlcv_map["GEV"] = _make_canonical_entry_ohlcv(120.0)
    entry_date = ohlcv_map["GEV"].index[-1]

    signals = sim._evaluate_signals(
        tickers=["GEV"],
        ticker_ohlcv=ohlcv_map,
        all_closes=pd.DataFrame(index=ohlcv_map["GEV"].index),
        eval_date=entry_date,
        market_state={"market_is_bullish": True},
    )

    assert [pending.signal["symbol"] for pending in signals] == ["GEV"]
    sim._enter_position(signals[0], ohlcv_map, entry_date)
    assert set(sim._open_positions) == {"GEV"}
    assert [(trade.symbol, trade.exit_reason) for trade in sim._trades] == [("MSFT", "evicted")]


def test_full_portfolio_without_eviction_returns_no_candidates() -> None:
    """A disabled eviction policy must retain the hard capacity gate."""
    sim, ohlcv_map = _make_full_sim({"MSFT": (100.0, 60.0, 95.0)})
    sim.max_positions = 1
    sim.enable_eviction = False
    sim._regime_tracker = SimpleNamespace(allows_entries=True)
    sim._ticker_industry = {}
    sim.strategy = SimpleNamespace(
        evaluate_symbol=lambda **_kwargs: {
            **_canonical_full_signal("GEV", rs_score=90.0, canslim_score=80.0),
        }
    )
    ohlcv_map["GEV"] = _make_canonical_entry_ohlcv(120.0)

    signals = sim._evaluate_signals(
        tickers=["GEV"],
        ticker_ohlcv=ohlcv_map,
        all_closes=pd.DataFrame(index=ohlcv_map["GEV"].index),
        eval_date=ohlcv_map["GEV"].index[-1],
        market_state={"market_is_bullish": True},
    )

    assert signals == []


def test_open_slot_returns_only_best_ranked_candidate() -> None:
    """Normal capacity admits only the highest-ranked signal for one free slot."""
    sim, ohlcv_map = _make_full_sim({"MSFT": (100.0, 60.0, 95.0)})
    sim.max_positions = 2
    sim.enable_eviction = True
    sim._regime_tracker = SimpleNamespace(allows_entries=True)
    sim._ticker_industry = {}
    rows = {
        "LOW": _canonical_full_signal("LOW", rs_score=99.0, canslim_score=70.0),
        "BEST": _canonical_full_signal("BEST", rs_score=80.0, canslim_score=90.0),
    }
    sim.strategy = SimpleNamespace(evaluate_symbol=lambda **kwargs: rows[kwargs["ticker"]])
    ohlcv_map["LOW"] = _make_canonical_entry_ohlcv(50.0)
    ohlcv_map["BEST"] = _make_canonical_entry_ohlcv(60.0)
    eval_date = ohlcv_map["BEST"].index[-1]

    signals = sim._evaluate_signals(
        tickers=["LOW", "BEST"],
        ticker_ohlcv=ohlcv_map,
        all_closes=pd.DataFrame(index=ohlcv_map["BEST"].index),
        eval_date=eval_date,
        market_state={"market_is_bullish": True},
    )

    assert [pending.signal["symbol"] for pending in signals] == ["BEST"]


def test_eviction_skips_incumbent_with_missing_price_data() -> None:
    """A candidate cannot evict a holding whose current value is unknown."""
    sim, ohlcv_map = _make_full_sim({"MSFT": (100.0, 60.0, 95.0)})
    sim.max_positions = 1
    del ohlcv_map["MSFT"]
    ohlcv_map["GEV"] = _make_ohlcv(n=60, close_value=120.0)
    signal = {
        "symbol": "GEV",
        "rs_score": 90.0,
        "canslim_score": 80.0,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }

    sim._enter_position(signal, ohlcv_map, ohlcv_map["GEV"].index[-1])

    assert set(sim._open_positions) == {"MSFT"}
    assert sim._trades == []


def test_eviction_pass1_evicts_underwater_lower_rs_position() -> None:
    """Pass 1: evicts the underwater position with the lowest RS."""
    positions = {
        "AAPL": (100.0, 85.0, 105.0),  # profitable, rs=85
        "MSFT": (100.0, 70.0, 95.0),   # underwater, rs=70 ← should be evicted
        "NVDA": (100.0, 88.0, 110.0),  # profitable, rs=88
        "CRWD": (100.0, 72.0, 98.0),   # underwater, rs=72 (higher than MSFT)
        "MU":   (100.0, 90.0, 115.0),  # profitable, rs=90
    }
    sim, ohlcv_map = _make_full_sim(positions)
    assert len(sim._open_positions) == 5

    new_signal = {
        "symbol": "GEV",
        "rs_score": 80.0,
        "canslim_score": 75.0,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }
    ohlcv_map["GEV"] = _make_ohlcv(n=10, close_value=50.0)
    entry_date = ohlcv_map["GEV"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert "MSFT" not in sim._open_positions
    assert "GEV" in sim._open_positions
    evicted = next(t for t in sim._trades if t.symbol == "MSFT")
    assert evicted.exit_reason == "evicted"


def test_eviction_pass2_evicts_lowest_rs_when_no_underwater_positions() -> None:
    """Pass 2: when no underwater positions qualify, evicts the lowest RS (profitable)."""
    positions = {
        "AAPL": (100.0, 85.0, 110.0),  # profitable, rs=85
        "MSFT": (100.0, 70.0, 112.0),  # profitable, rs=70 ← lowest, evicted
        "NVDA": (100.0, 88.0, 115.0),  # profitable, rs=88
        "CRWD": (100.0, 78.0, 105.0),  # profitable, rs=78
        "MU":   (100.0, 90.0, 120.0),  # profitable, rs=90
    }
    sim, ohlcv_map = _make_full_sim(positions)

    new_signal = {
        "symbol": "VRT",
        "rs_score": 80.0,
        "canslim_score": 75.0,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }
    ohlcv_map["VRT"] = _make_ohlcv(n=10, close_value=60.0)
    entry_date = ohlcv_map["VRT"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert "MSFT" not in sim._open_positions
    assert "VRT" in sim._open_positions
    evicted = next(t for t in sim._trades if t.symbol == "MSFT")
    assert evicted.exit_reason == "evicted"


def test_eviction_skipped_when_new_signal_rs_lower_than_all_positions() -> None:
    """No eviction if new signal's RS is not higher than any open position."""
    positions = {
        "AAPL": (100.0, 85.0, 110.0),
        "MSFT": (100.0, 88.0, 112.0),
        "NVDA": (100.0, 90.0, 115.0),
        "CRWD": (100.0, 82.0, 105.0),
        "MU":   (100.0, 91.0, 120.0),
    }
    sim, ohlcv_map = _make_full_sim(positions)
    original = set(sim._open_positions.keys())

    new_signal = {
        "symbol": "GEV",
        "rs_score": 79.0,
        "canslim_score": 75.0,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }
    ohlcv_map["GEV"] = _make_ohlcv(n=10, close_value=40.0)
    entry_date = ohlcv_map["GEV"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert set(sim._open_positions.keys()) == original
    assert "GEV" not in sim._open_positions


def test_eviction_disabled_when_flag_is_false() -> None:
    """enable_eviction=False prevents all eviction logic."""
    positions = {
        "AAPL": (100.0, 60.0, 95.0),
        "MSFT": (100.0, 65.0, 96.0),
        "NVDA": (100.0, 68.0, 97.0),
        "CRWD": (100.0, 62.0, 94.0),
        "MU":   (100.0, 55.0, 93.0),
    }
    sim, ohlcv_map = _make_full_sim(positions)
    sim.enable_eviction = False
    original = set(sim._open_positions.keys())

    new_signal = {
        "symbol": "VST",
        "rs_score": 95.0,
        "canslim_score": 80.0,
        "signal_reason": "Volume Breakout",
        "buy_signal": True,
    }
    ohlcv_map["VST"] = _make_ohlcv(n=10, close_value=70.0)
    entry_date = ohlcv_map["VST"].index[-1]

    sim._enter_position(new_signal, ohlcv_map, entry_date)

    assert set(sim._open_positions.keys()) == original
    assert "VST" not in sim._open_positions


@pytest.mark.parametrize(
    ("configured_limit", "configured_eviction", "open_count", "expected"),
    [
        (None, True, 2, CapacityDecision(None, True)),
        (4, True, 2, CapacityDecision(4, True)),
        (2, False, 2, CapacityDecision(2, False)),
        (2, True, 2, CapacityDecision(2, True)),
    ],
)
def test_policy_capacity_resolves_validated_baseline_decisions(
    configured_limit: int | None,
    configured_eviction: bool,
    open_count: int,
    expected: CapacityDecision,
) -> None:
    """Break caught: capacity could remain an unvalidated simulator-only setting."""
    simulator = PortfolioSimulator(
        max_positions=configured_limit,
        enable_eviction=configured_eviction,
    )
    simulator._open_positions = {
        f"OLD{slot}": Trade(f"OLD{slot}", "2026-01-01", 100.0, 1.0, 92.0)
        for slot in range(open_count)
    }
    assert simulator._resolve_capacity(
        eligible_signal_count=3,
        cash_fraction=0.5,
    ) == expected


def test_policy_capacity_rejects_finite_limit_above_engine_ceiling() -> None:
    """Break caught: an injected policy could bypass the 25-position safety ceiling."""

    class Client(InProcessPolicyClient):
        def recommend_capacity(self, _snapshot):
            return CapacityDecision(26, True)

    simulator = PortfolioSimulator()
    simulator._policy_client = Client()
    with pytest.raises(ValueError, match="max_positions"):
        simulator._resolve_capacity(eligible_signal_count=1, cash_fraction=1.0)


def test_policy_capacity_pending_entry_round_trip_and_carried_capacity() -> None:
    """Break caught: next-session entry could silently reread changed defaults."""
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0},
        capacity=CapacityDecision(max_positions=1, eviction_enabled=False),
    )
    assert PendingEntry.from_primitive(pending.to_primitive()) == pending
    simulator = PortfolioSimulator(max_positions=None, enable_eviction=True)
    simulator._open_positions = {"OLD": object()}
    assert simulator._capacity_state(pending) == (True, False)


def test_policy_capacity_lowered_below_holdings_blocks_without_liquidation() -> None:
    """Break caught: lowering a policy cap could force unrequested liquidation."""
    simulator = PortfolioSimulator(max_positions=None, enable_eviction=True)
    positions = {
        "OLD1": Trade("OLD1", "2026-01-01", 100.0, 1.0, 92.0),
        "OLD2": Trade("OLD2", "2026-01-01", 100.0, 1.0, 92.0),
    }
    simulator._open_positions = dict(positions)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 99.0},
        capacity=CapacityDecision(1, False),
    )
    assert simulator._capacity_state(pending) == (True, False)
    assert simulator._open_positions == positions


def test_policy_capacity_evaluate_signals_carries_one_batch_decision() -> None:
    """Break caught: ranking/truncation could discard the signal-session capacity."""

    class Client(InProcessPolicyClient):
        def __init__(self) -> None:
            self.capacity_calls = 0

        def recommend_capacity(self, _snapshot):
            self.capacity_calls += 1
            return CapacityDecision(1, False)

    simulator = PortfolioSimulator(max_positions=None, enable_eviction=True)
    client = Client()
    simulator._policy_client = client
    simulator._regime_tracker = SimpleNamespace(allows_entries=True)
    simulator._ticker_industry = {}
    frame = _make_canonical_entry_ohlcv(120.0)
    simulator.strategy = SimpleNamespace(
        evaluate_symbol=lambda **_kwargs: _canonical_full_signal(
            "NEW", rs_score=90.0, canslim_score=80.0
        )
    )

    pending = simulator._evaluate_signals(
        tickers=["NEW"],
        ticker_ohlcv={"NEW": frame},
        all_closes=pd.DataFrame(index=frame.index),
        eval_date=frame.index[-1],
        market_state={"market_is_bullish": True},
    )

    assert client.capacity_calls == 1
    assert pending == [
        PendingEntry(
            signal=pending[0].signal,
            capacity=CapacityDecision(1, False),
        )
    ]


def test_policy_capacity_checkpoint_serializes_pending_carrier() -> None:
    """Break caught: a resumed run could lose the carried capacity decision."""
    simulator = PortfolioSimulator()
    simulator._reset_run_state()
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0},
        capacity=CapacityDecision(1, False),
    )
    payload = simulator._checkpoint_payload(
        fingerprint="a" * 64,
        code_identity="code",
        strategy_identity={"kind": "built_in"},
        next_day_index=1,
        total_days=2,
        state_log_offset=0,
        regime_tracker=simulator._regime_tracker,
        pending_entries=[pending],
        benchmark_start_price=100.0,
        origin_requested_min_rs_score=80.0,
        origin_requested_min_canslim_score=70.0,
    )
    assert payload["pending_entries"] == [pending.to_primitive()]
    assert PendingEntry.from_primitive(payload["pending_entries"][0]) == pending


def test_policy_capacity_next_session_enforces_carried_decision() -> None:
    """Break caught: entry execution could use permissive current defaults instead."""
    simulator = PortfolioSimulator(max_positions=None, enable_eviction=True)
    old = Trade("OLD", "2026-01-01", 100.0, 1.0, 92.0)
    simulator._open_positions = {"OLD": old}
    frame = _make_ohlcv(n=60, close_value=100.0)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 99.0, "canslim_score": 90.0},
        capacity=CapacityDecision(1, False),
    )

    simulator._enter_position(pending, {"NEW": frame, "OLD": frame}, frame.index[-1])

    assert simulator._open_positions == {"OLD": old}
    assert simulator._transactions == []
    assert simulator._entry_outcomes[-1].outcome == "entry_rejected_capacity"


def test_policy_eviction_snapshot_uses_stable_opaque_slots() -> None:
    """Break caught: eviction policy could receive symbols or unstable slot order."""
    simulator = PortfolioSimulator(max_positions=2, enable_eviction=True)
    simulator._open_positions = {
        "BBB": Trade("BBB", "2026-01-01", 100.0, 1.0, 92.0, rs_score=60.0),
        "AAA": Trade("AAA", "2026-01-01", 100.0, 1.0, 92.0, rs_score=70.0),
    }
    frame = _make_ohlcv(n=2, close_value=95.0)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0},
        capacity=CapacityDecision(2, True),
    )

    snapshot = simulator._build_eviction_snapshot(
        pending=pending,
        ticker_ohlcv={"BBB": frame, "AAA": frame, "NEW": frame},
        entry_date=frame.index[-1],
    )

    assert [position.slot for position in snapshot.positions] == [0, 1]
    assert [position.rs_score for position in snapshot.positions] == [60.0, 70.0]
    assert all(not hasattr(position, "symbol") for position in snapshot.positions)


def test_policy_eviction_rejects_unknown_slot_without_mutation() -> None:
    """Break caught: a policy-selected unknown slot could evict an arbitrary holding."""
    simulator = PortfolioSimulator(max_positions=1, enable_eviction=True)
    old = Trade("OLD", "2026-01-01", 100.0, 1.0, 92.0, rs_score=60.0)
    simulator._open_positions = {"OLD": old}
    frame = _make_ohlcv(n=2, close_value=95.0)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0},
        capacity=CapacityDecision(1, True),
    )
    before = (simulator._equity, dict(simulator._open_positions), list(simulator._transactions))

    with pytest.raises(ValueError, match="slot"):
        simulator._project_entry_transition(
            pending=pending,
            ticker_ohlcv={"OLD": frame, "NEW": frame},
            entry_date=frame.index[-1],
            eviction=EvictionDecision(9),
        )

    assert (simulator._equity, simulator._open_positions, simulator._transactions) == before


@pytest.mark.parametrize(
    ("projection", "recommendation", "message"),
    [
        (
            ProjectedEntryTransition(None, None, None, 100.0, 10_000.0, 5_000.0, 5_000.0),
            AllocationDecision(0.011, 0.08, None),
            "risk_fraction",
        ),
        (
            ProjectedEntryTransition(None, None, None, 100.0, 10_000.0, 5_000.0, 5_000.0),
            AllocationDecision(0.01, 0.081, None),
            "stop_distance_fraction",
        ),
        (
            ProjectedEntryTransition(None, None, None, 100.0, 10_000.0, 100.0, 5_000.0),
            AllocationDecision(0.01, 0.08, None),
            "projected cash",
        ),
        (
            ProjectedEntryTransition(None, None, None, 100.0, 10_000.0, 5_000.0, 9_500.0),
            AllocationDecision(0.01, 0.08, None),
            "gross long notional",
        ),
    ],
)
def test_policy_allocation_rejects_unsafe_projected_transition(
    projection: ProjectedEntryTransition,
    recommendation: AllocationDecision,
    message: str,
) -> None:
    """Break caught: unsafe risk/cash/leverage could reach the mutating apply phase."""
    simulator = PortfolioSimulator()
    with pytest.raises(ValueError, match=message):
        simulator._validate_entry_transition(projection, recommendation)


def test_projected_entry_transition_failed_allocation_is_byte_stable() -> None:
    """Break caught: eviction could occur before allocation validation completes."""

    class Client(InProcessPolicyClient):
        def recommend_allocation(self, _snapshot):
            return AllocationDecision(0.011, 0.08, None)

    simulator = PortfolioSimulator(max_positions=1, enable_eviction=True)
    simulator._policy_client = Client()
    old = Trade("OLD", "2026-01-01", 100.0, 10.0, 92.0, rs_score=60.0)
    simulator._open_positions = {"OLD": old}
    simulator._equity = 1_000.0
    frame = _make_ohlcv(n=60, close_value=95.0)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0, "canslim_score": 80.0},
        capacity=CapacityDecision(1, True),
    )
    before = (
        simulator._equity,
        dict(simulator._open_positions),
        list(simulator._trades),
        list(simulator._transactions),
    )

    with pytest.raises(ValueError, match="risk_fraction"):
        simulator._enter_position(
            pending,
            {"OLD": frame, "NEW": frame},
            frame.index[-1],
        )

    assert (
        simulator._equity,
        simulator._open_positions,
        simulator._trades,
        simulator._transactions,
    ) == before


def test_policy_allocation_uncapped_batch_preserves_cash_for_remaining_entries() -> None:
    """Break caught: early uncapped entries could starve a same-session batch."""
    simulator = PortfolioSimulator(initial_capital=1_000.0, max_positions=None)
    simulator._pending_entries_remaining = 10
    frame = _make_ohlcv(n=2, close_value=100.0)
    pending = PendingEntry(
        signal={"symbol": "NEW", "rs_score": 90.0, "canslim_score": 80.0},
        capacity=CapacityDecision(None, True),
    )

    simulator._enter_position(pending, {"NEW": frame}, frame.index[-1])

    buy = simulator._transactions[-1]
    assert buy["Action"] == "BUY"
    assert buy["Value"] == 100.0


def test_policy_exit_hard_stop_precedes_any_policy_call() -> None:
    """Break caught: policy code could delay or override the engine hard stop."""
    calls: list[ExitSnapshot] = []

    class Client(InProcessPolicyClient):
        def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision:
            calls.append(snapshot)
            return super().evaluate_exit(snapshot)

    simulator = PortfolioSimulator()
    simulator._policy_client = Client()
    trade = Trade("AAA", "2026-01-01", 100.0, 10.0, 92.0)
    simulator._open_positions = {"AAA": trade}
    frame = _make_ohlcv(n=3, close_value=90.0, high_value=91.0, low_value=89.0)

    simulator._check_exits("AAA", frame, frame.index[-1])

    assert calls == []
    assert simulator._trades[-1].exit_reason == "stop_loss"
    assert simulator._trades[-1].exit_price == 92.0


def test_policy_exit_close_uses_trusted_current_close() -> None:
    """Break caught: an exit decision could fail to route or acquire fill authority."""

    class Client(InProcessPolicyClient):
        def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision:
            return ExitDecision(
                actions=(ExitAction("close", None, None, "policy_exit"),),
                next_stop_price=None,
                early_winner_hold=snapshot.early_winner_hold,
                scale_out_tier=snapshot.scale_out_tier,
                breakeven_armed=snapshot.breakeven_armed,
                ema_trailing_active=snapshot.ema_trailing_active,
            )

    simulator = PortfolioSimulator(stagnation_days=999)
    simulator._policy_client = Client()
    simulator._open_positions = {
        "AAA": Trade("AAA", "2026-01-01", 100.0, 10.0, 92.0)
    }
    frame = _make_ohlcv(n=3, close_value=103.0, high_value=104.0, low_value=99.0)

    simulator._check_exits("AAA", frame, frame.index[-1])

    assert simulator._trades[-1].exit_reason == "policy_exit"
    assert simulator._trades[-1].exit_price == 103.0


def test_policy_exit_rejects_uncrossed_scale_out_before_mutation() -> None:
    """Break caught: an invalid action plan could partially sell before rejection."""

    class Client(InProcessPolicyClient):
        def evaluate_exit(self, snapshot: ExitSnapshot) -> ExitDecision:
            return ExitDecision(
                actions=(
                    ExitAction(
                        "scale_out",
                        0.10,
                        0.25,
                        "take_profit_scale_out",
                    ),
                ),
                next_stop_price=None,
                early_winner_hold=False,
                scale_out_tier=1,
                breakeven_armed=False,
                ema_trailing_active=False,
            )

    simulator = PortfolioSimulator(stagnation_days=999)
    simulator._policy_client = Client()
    trade = Trade("AAA", "2026-01-01", 100.0, 10.0, 92.0)
    simulator._open_positions = {"AAA": trade}
    frame = _make_ohlcv(n=3, close_value=105.0, high_value=109.0, low_value=99.0)
    before = (trade.remaining_qty, simulator._equity, list(simulator._transactions))

    with pytest.raises(ValueError, match="crossed"):
        simulator._check_exits("AAA", frame, frame.index[-1])

    assert (trade.remaining_qty, simulator._equity, simulator._transactions) == before
