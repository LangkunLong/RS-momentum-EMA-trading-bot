"""Compatibility CLI for the canonical CANSLIM backtest engine.

The simulation implementation lives in :mod:`core.backtest_engine`.  This
module keeps the historical ``python backtest_pnl.py`` entry point and the
patch points used by downstream tests without maintaining a second engine.

Active risk defaults come from ``config.settings``: 1% portfolio risk, an 8%
hard stop, and a derived 12.5% maximum position weight.
"""

from __future__ import annotations

from backtest import (
    _calculate_rs_at_date,
    _compute_canslim_score,
    _download_bulk_closes,
    _download_price_data,
    _evaluate_fundamentals_at_date,
    _evaluate_market_at_date,
    _evaluate_technical_at_date,
)
from core import backtest_engine as _engine
from core.data_client import clear_session_cache
from core.index_ticker_fetcher import get_sp500_tickers

DataFetcher = _engine.DataFetcher
CanslimStrategy = _engine.CanslimStrategy
PerformanceReport = _engine.PerformanceReport
Trade = _engine.Trade
SimulationResult = _engine.SimulationResult
print_pnl_report = _engine.print_pnl_report
export_equity_curve = _engine.export_equity_curve
export_pnl_to_csv = _engine.export_pnl_to_csv
export_weekly_holdings = _engine.export_weekly_holdings
export_trade_charts = _engine.export_trade_charts
run_cli = _engine.run_cli

DEFAULT_TICKERS = _engine.DEFAULT_TICKERS
DEFAULT_CAPITAL = _engine.DEFAULT_CAPITAL
DEFAULT_LOOKBACK_WEEKS = _engine.DEFAULT_LOOKBACK_WEEKS
DEFAULT_SIGNAL_EVERY_N_DAYS = _engine.DEFAULT_SIGNAL_EVERY_N_DAYS
DEFAULT_MA_EXIT_PERIOD = _engine.DEFAULT_MA_EXIT_PERIOD
DEFAULT_MA_CONSECUTIVE = _engine.DEFAULT_MA_CONSECUTIVE
DEFAULT_START_DATE = _engine.DEFAULT_START_DATE
DEFAULT_END_DATE = _engine.DEFAULT_END_DATE
BENCHMARK = _engine.BENCHMARK


class PortfolioSimulator(_engine.PortfolioSimulator):
    """Delegate simulation while preserving this module's legacy patch points."""

    def run(self, *args, **kwargs):  # type: ignore[override]
        from core.index_ticker_fetcher import get_all_index_tickers as _get_all_index_tickers

        _engine._download_price_data = _download_price_data
        _engine._download_bulk_closes = _download_bulk_closes
        _engine._calculate_rs_at_date = _calculate_rs_at_date
        _engine._evaluate_market_at_date = _evaluate_market_at_date
        _engine._evaluate_fundamentals_at_date = _evaluate_fundamentals_at_date
        _engine._evaluate_technical_at_date = _evaluate_technical_at_date
        _engine._compute_canslim_score = _compute_canslim_score
        _engine.clear_session_cache = clear_session_cache
        _engine.get_sp500_tickers = get_sp500_tickers
        _engine.get_all_index_tickers = _get_all_index_tickers
        return super().run(*args, **kwargs)


if __name__ == "__main__":
    run_cli()
