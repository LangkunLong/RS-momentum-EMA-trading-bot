"""Point-in-time leader-basket benchmark.

This is deliberately separate from tactical CANSLIM.  It answers one narrow
question: what would an equal-weight basket of the top-ranked, historically
eligible leaders have done when rebalanced on a fixed schedule?  Rankings use
only closes through the rebalance date and orders execute at the following
trading day's open, so neither membership nor price data can look ahead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from core.momentum_analysis import calculate_weighted_performance
from core.pit_data import PITDataBundle, PriceIdentityTransitionContract


@dataclass(frozen=True)
class LeaderBasketConfig:
    leader_count: int = 50
    rebalance_days: int = 20
    lookback_days: int = 252
    min_history_days: int = 60
    initial_capital: float = 100_000.0

    def __post_init__(self) -> None:
        if self.leader_count < 1:
            raise ValueError("leader_count must be positive")
        if self.rebalance_days < 1:
            raise ValueError("rebalance_days must be positive")
        if self.lookback_days < 20:
            raise ValueError("lookback_days must be at least 20")
        if self.min_history_days < 20:
            raise ValueError("min_history_days must be at least 20")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")


@dataclass
class LeaderBasketResult:
    equity_curve: pd.Series
    benchmark_curve: pd.Series
    holdings: pd.DataFrame
    transactions: pd.DataFrame
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def total_return_pct(self) -> float:
        return _total_return(self.equity_curve)

    @property
    def benchmark_return_pct(self) -> float:
        return _total_return(self.benchmark_curve)

    @property
    def annualized_return_pct(self) -> float:
        return _annualized_return(self.equity_curve)

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        curve = self.equity_curve.astype(float)
        return float(((curve / curve.cummax()) - 1.0).min() * 100.0)

    @property
    def sharpe_ratio(self) -> float:
        returns = self.equity_curve.astype(float).pct_change().dropna()
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(252))

    @property
    def average_cash_pct(self) -> float:
        if self.holdings.empty or "cash" not in self.holdings:
            return 0.0
        equity = self.holdings["equity"].replace(0, np.nan)
        return float((self.holdings["cash"] / equity).dropna().mean() * 100.0)

    @property
    def rebalance_count(self) -> int:
        if self.transactions.empty:
            return 0
        return int(self.transactions.loc[self.transactions["Reason"] == "scheduled_rebalance", "Date"].nunique())


def _total_return(curve: pd.Series) -> float:
    if curve is None or len(curve) < 2 or float(curve.iloc[0]) == 0:
        return 0.0
    return float((curve.iloc[-1] / curve.iloc[0] - 1.0) * 100.0)


def _annualized_return(curve: pd.Series) -> float:
    if curve is None or len(curve) < 2 or float(curve.iloc[0]) == 0:
        return 0.0
    days = max((pd.Timestamp(curve.index[-1]) - pd.Timestamp(curve.index[0])).days, 1)
    return float(((curve.iloc[-1] / curve.iloc[0]) ** (365.0 / days) - 1.0) * 100.0)


def _rank_leaders(
    closes: pd.DataFrame,
    *,
    eval_date: pd.Timestamp,
    eligible_tickers: Iterable[str],
    config: LeaderBasketConfig,
) -> list[str]:
    eligible = {str(ticker).upper() for ticker in eligible_tickers}
    window_start = eval_date - pd.Timedelta(days=config.lookback_days)
    window = closes.loc[window_start:eval_date]
    scores: list[tuple[str, float]] = []
    for ticker in sorted(eligible):
        if ticker not in window:
            continue
        series = window[ticker].dropna()
        if len(series) < config.min_history_days:
            continue
        score = calculate_weighted_performance(series)
        if score is None:
            first = float(series.iloc[0])
            last = float(series.iloc[-1])
            if first <= 0:
                continue
            score = (last / first) ** (252.0 / len(series)) - 1.0
        scores.append((ticker, float(score)))
    scores.sort(key=lambda item: (-item[1], item[0]))
    return [ticker for ticker, _score in scores[: config.leader_count]]


class LeaderBasketSimulator:
    """Equal-weight, scheduled-rebalance leader basket using a PIT bundle."""

    def __init__(
        self,
        bundle: PITDataBundle,
        config: LeaderBasketConfig | None = None,
        identity_transition_contract: PriceIdentityTransitionContract | None = None,
    ) -> None:
        self.bundle = bundle
        self.config = config or LeaderBasketConfig()
        self.identity_transition_contract = identity_transition_contract

    def run(
        self,
        *,
        start_date: str | pd.Timestamp,
        end_date: str | pd.Timestamp,
        benchmark_symbol: str = "SPY",
        tickers: Optional[Iterable[str]] = None,
    ) -> LeaderBasketResult:
        start = pd.Timestamp(start_date).normalize()
        end = pd.Timestamp(end_date).normalize()
        if end < start:
            raise ValueError("end_date must not precede start_date")

        benchmark_symbol = benchmark_symbol.upper()
        tradable_tickers = tuple(self.bundle.tradable_symbols())
        market_reference_tickers = tuple(self.bundle.reference_symbols())
        requested = (
            tradable_tickers
            if tickers is None
            else tuple(str(ticker).upper() for ticker in tickers)
        )
        references = set(market_reference_tickers)
        if references.intersection(requested):
            raise ValueError(
                "leader basket reference symbols are observation-only and cannot be traded"
            )
        invalid = set(requested).difference(tradable_tickers)
        if invalid:
            raise ValueError("leader basket tickers are not tradable PIT membership symbols")
        universe = tuple(sorted(set(requested)))
        if not universe:
            raise ValueError("leader basket universe cannot contain only the benchmark")
        symbols = tuple(dict.fromkeys([*universe, *market_reference_tickers, benchmark_symbol]))
        # Pull a pre-window buffer so the first rebalance can rank leaders
        # using history that predates the evaluation window.  The output
        # remains clipped to [start, end].
        history_buffer_days = max(self.config.lookback_days * 2, self.config.min_history_days * 2)
        data_start = start - pd.Timedelta(days=history_buffer_days)
        price_data = self.bundle.fetch_price_data(symbols, data_start, end)
        benchmark = price_data.get(benchmark_symbol)
        if benchmark is None or benchmark.empty:
            raise ValueError(f"benchmark {benchmark_symbol} is missing from the PIT bundle")
        closes = self.bundle.fetch_closes(universe, data_start, end)
        trading_days = benchmark.loc[start:end].index
        if len(trading_days) < self.config.min_history_days:
            raise ValueError("not enough trading days for the leader basket")

        cash = self.config.initial_capital
        holdings: dict[str, float] = {}
        equity_rows: list[dict[str, Any]] = []
        transactions: list[dict[str, Any]] = []
        holding_rows: list[dict[str, Any]] = []
        pending_target: Optional[list[str]] = None

        for day_idx, eval_date in enumerate(trading_days):
            transfer_rows = self._apply_identity_transitions(price_data, eval_date, holdings)
            transactions.extend(transfer_rows)
            if pending_target is not None:
                cash, holdings, rebalance_rows = self._rebalance(
                    pending_target,
                    price_data,
                    eval_date,
                    cash,
                    holdings,
                )
                transactions.extend(rebalance_rows)
                pending_target = None

            close_values = {
                ticker: float(frame.loc[:eval_date]["Close"].iloc[-1])
                for ticker, frame in price_data.items()
                if ticker != benchmark_symbol and not frame.loc[:eval_date].empty
            }
            holdings_value = sum(qty * close_values[ticker] for ticker, qty in holdings.items() if ticker in close_values)
            equity = cash + holdings_value
            date_key = str(pd.Timestamp(eval_date).date())
            equity_rows.append({"date": date_key, "equity": equity})
            holding_rows.append(
                {
                    "date": date_key,
                    "cash": cash,
                    "equity": equity,
                    "holdings_count": len(holdings),
                    "leaders": ",".join(sorted(holdings)),
                }
            )
            if day_idx == len(trading_days) - 1:
                continue
            if day_idx % self.config.rebalance_days == 0:
                eligible = self.bundle.members_at(eval_date).intersection(universe)
                pending_target = _rank_leaders(
                    closes,
                    eval_date=eval_date,
                    eligible_tickers=eligible,
                    config=self.config,
                )

        equity_curve = pd.Series(
            [row["equity"] for row in equity_rows],
            index=pd.to_datetime([row["date"] for row in equity_rows]),
            dtype=float,
        )
        benchmark_curve = benchmark.loc[trading_days, "Close"].astype(float)
        if not benchmark_curve.empty:
            benchmark_curve = benchmark_curve / float(benchmark_curve.iloc[0]) * self.config.initial_capital
        return LeaderBasketResult(
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            holdings=pd.DataFrame(holding_rows),
            transactions=pd.DataFrame(transactions),
            config={
                "mode": "leader_basket",
                "leader_count": self.config.leader_count,
                "rebalance_days": self.config.rebalance_days,
                "lookback_days": self.config.lookback_days,
                "min_history_days": self.config.min_history_days,
                "initial_capital": self.config.initial_capital,
                "benchmark_symbol": benchmark_symbol,
                "pit_bundle_sha256": self.bundle.sha256,
                "pit_data_cutoff": str(self.bundle.data_cutoff.date()),
                "pit_manifest": self.bundle.manifest(),
                "universe_count": len(universe),
            },
        )

    def _apply_identity_transitions(
        self,
        price_data: dict[str, pd.DataFrame],
        eval_date: pd.Timestamp,
        holdings: dict[str, float],
    ) -> list[dict[str, Any]]:
        contract = self.identity_transition_contract
        if contract is None:
            return []
        rows: list[dict[str, Any]] = []
        effective = eval_date.date()
        for transition in contract.transitions:
            if transition.effective_date != effective or transition.predecessor not in holdings:
                continue
            if transition.successor in holdings:
                raise ValueError("identity transition successor is already held")
            frame = price_data.get(transition.successor)
            if frame is None:
                raise ValueError("identity transition successor has no price data")
            bar = frame.loc[eval_date:eval_date]
            if bar.empty or float(bar["Open"].iloc[0]) <= 0:
                raise ValueError("identity transition successor lacks a valid transition bar")
            quantity = holdings.pop(transition.predecessor)
            holdings[transition.successor] = quantity
            rows.append({
                "Date": str(eval_date.date()),
                "Ticker": transition.successor,
                "FromTicker": transition.predecessor,
                "Action": "TRANSFER",
                "Price": float(bar["Open"].iloc[0]),
                "Quantity": quantity,
                "Reason": "pit_identity_transfer",
            })
        return rows

    @staticmethod
    def _rebalance(
        target: list[str],
        price_data: dict[str, pd.DataFrame],
        trade_date: pd.Timestamp,
        cash: float,
        holdings: dict[str, float],
    ) -> tuple[float, dict[str, float], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        for ticker, quantity in list(holdings.items()):
            frame = price_data[ticker].loc[trade_date:trade_date]
            if frame.empty:
                frame = price_data[ticker].loc[:trade_date].tail(1)
            if frame.empty:
                continue
            price = float(frame["Open"].iloc[0])
            cash += quantity * price
            rows.append({"Date": str(trade_date.date()), "Ticker": ticker, "Action": "SELL", "Price": price, "Quantity": quantity, "Reason": "scheduled_rebalance"})
        holdings = {}
        tradable = []
        for ticker in target:
            frame = price_data.get(ticker)
            if frame is None:
                continue
            bar = frame.loc[trade_date:trade_date]
            if not bar.empty and float(bar["Open"].iloc[0]) > 0:
                tradable.append((ticker, float(bar["Open"].iloc[0])))
        if tradable:
            allocation = cash / len(tradable)
            for index, (ticker, price) in enumerate(tradable):
                quantity = (cash if index == len(tradable) - 1 else allocation) / price
                holdings[ticker] = quantity
                if index == len(tradable) - 1:
                    # Spend the exact remaining balance on the final
                    # allocation.  Repeated binary-float subtraction can
                    # otherwise leave a tiny negative cash residue and make
                    # an otherwise valid basket fail accounting validation.
                    cash = 0.0
                else:
                    cash -= quantity * price
                rows.append({"Date": str(trade_date.date()), "Ticker": ticker, "Action": "BUY", "Price": price, "Quantity": quantity, "Reason": "scheduled_rebalance"})
        return cash, holdings, rows


def print_leader_basket_report(result: LeaderBasketResult) -> None:
    """Print a compact report suitable for CLI and archived backtest logs."""

    print("\nLeader basket (point-in-time)")
    print(f"  Return: {result.total_return_pct:.2f}% (benchmark {result.benchmark_return_pct:.2f}%)")
    print(f"  Annualized: {result.annualized_return_pct:.2f}%")
    print(f"  Max drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe: {result.sharpe_ratio:.2f}")
    print(f"  Average cash: {result.average_cash_pct:.2f}%")
    print(f"  Rebalances: {result.rebalance_count}")
