from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from core.backtest_engine import (
    CanslimStrategy,
    DataFetcher,
    PerformanceReport,
    SimulationResult,
    _calculate_rs_snapshot,
    _resolve_universe,
    export_trade_charts,
)
from backtest_pnl import PortfolioSimulator, Trade
from backtest import _calculate_rs_at_date


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


def test_take_profit_scale_out_fires_all_three_tiers_on_gap_up() -> None:
    """When high clears all 3 tier thresholds in one bar, all 3 tiers fire."""
    sim = PortfolioSimulator(initial_capital=100_000.0, stagnation_days=999)
    trade = Trade(symbol="NVDA", entry_date="2026-04-01", entry_price=100.0, qty=10.0, stop_price=92.0)
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
    sim._open_positions["MSFT"] = trade

    ohlcv = _make_ohlcv(n=3, close_value=62.0, high_value=62.0, low_value=51.0)
    sim._check_exits("MSFT", ohlcv, ohlcv.index[-1])

    result = sim._open_positions["MSFT"]
    assert result.scale_out_tier == 3
    assert result.remaining_qty == pytest.approx(qty * 0.25)


def test_time_stop_exits_stagnant_position() -> None:
    sim = PortfolioSimulator(
        initial_capital=100_000.0,
        take_profit_pct=0.50,
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
    strategy = CanslimStrategy(technical_only=True, min_rs_score=87.0, min_technical_score=70.0)
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
            "has_power_gap": False, "power_gap_details": {}
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
            "has_power_gap": False, "power_gap_details": {}
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
