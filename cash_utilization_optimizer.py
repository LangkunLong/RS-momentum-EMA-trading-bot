"""Walk-forward selection for the backtest cash-deployment threshold.

This research CLI uses the approved local historical cache only.  It never
downloads data or changes the live/paper trading configuration.  Candidate
thresholds are selected on a training window, then evaluated once on a
trailing holdout window so a full-period winner cannot silently become the
default.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import pickle
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

import core.backtest_engine as engine
from config import settings


DEFAULT_BUNDLE = Path(".artifacts/cache/backtest/historical_data.sqlite3")
DEFAULT_TICKER_CACHE = Path("ticker_cache/index_tickers_cache.json")
DEFAULT_THRESHOLDS = (None, 0.75, 0.60, 0.50)


class FrozenFetcher:
    """Data fetcher backed by the approved local cache."""

    def __init__(self, price: dict[str, pd.DataFrame], closes: pd.DataFrame) -> None:
        self.price = price
        self.closes = closes

    def fetch_price_data(
        self,
        tickers: list[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> dict[str, pd.DataFrame]:
        del start_date, end_date
        return {symbol: self.price[symbol].copy() for symbol in tickers if symbol in self.price}

    def fetch_rs_universe_closes(
        self,
        tickers: list[str],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> pd.DataFrame:
        del tickers, start_date, end_date
        return self.closes.copy()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_payloads(bundle: Path) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, str]:
    """Load the largest approved price/closes payloads and return the bundle hash."""
    selected_price: dict[str, pd.DataFrame] | None = None
    selected_closes: pd.DataFrame | None = None
    with sqlite3.connect(bundle) as connection:
        rows = connection.execute(
            "SELECT cache_kind, payload FROM dataset_cache WHERE cache_kind IN ('price', 'closes')"
        ).fetchall()
    for kind, blob in rows:
        payload = pickle.loads(blob)
        if (
            kind == "price"
            and isinstance(payload, dict)
            and "SPY" in payload
            and 500 <= len(payload) < 1000
            and (selected_price is None or len(payload) > len(selected_price))
        ):
            selected_price = payload
        if (
            kind == "closes"
            and isinstance(payload, pd.DataFrame)
            and payload.shape[1] >= 2000
            and (selected_closes is None or payload.shape[1] > selected_closes.shape[1])
        ):
            selected_closes = payload
    if selected_price is None or selected_closes is None:
        raise RuntimeError("approved price and closes payloads were not found")
    return selected_price, selected_closes, _sha256(bundle)


def _cash_summary(result: engine.SimulationResult) -> dict[str, Any]:
    weekly = result.weekly_holdings
    if weekly.empty:
        avg_cash = median_cash = 100.0
    else:
        total = pd.to_numeric(weekly["Total_Equity"], errors="coerce")
        cash = pd.to_numeric(weekly["Cash"], errors="coerce")
        cash_pct = cash.div(total).mul(100).dropna()
        avg_cash = float(cash_pct.mean()) if not cash_pct.empty else 100.0
        median_cash = float(cash_pct.median()) if not cash_pct.empty else 100.0
    diagnostics = result.execution_diagnostics
    return {
        "total_return_pct": float(result.total_return_pct),
        "annualized_return_pct": float(result.annualized_return_pct),
        "sharpe_ratio": float(result.sharpe_ratio),
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "closed_trades": len(result.closed_trades),
        "avg_cash_pct": avg_cash,
        "median_cash_pct": median_cash,
        "entries_executed": int(diagnostics.get("entries_executed", 0)),
        "entry_rejected_no_cash": int(diagnostics.get("entry_rejected_no_cash", 0)),
        "cash_deployment_override_days": int(
            diagnostics.get("cash_deployment_override_days", 0)
        ),
    }


def _run_candidate(
    *,
    threshold: float | None,
    start_date: str,
    end_date: str,
    candidates: list[str],
    fetcher: FrozenFetcher,
    signal_days: int,
    risk_pct: float,
) -> dict[str, Any]:
    simulator = engine.PortfolioSimulator(
        initial_capital=100_000.0,
        max_positions=None,
        position_risk_pct=risk_pct,
        stop_loss_pct=float(settings.STOP_LOSS_PCT),
        signal_every_n_days=signal_days,
        min_rs_score=80,
        min_technical_score=70,
        require_bullish_market=True,
        use_stateful_regime_gate=False,
        cash_deployment_threshold_pct=threshold,
        technical_only=True,
        data_fetcher=fetcher,
        benchmark_symbol="SPY",
        enable_eviction=False,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        result = simulator.run(
            tickers=candidates,
            start_date=start_date,
            end_date=end_date,
            benchmark_symbol="SPY",
        )
    summary = _cash_summary(result)
    summary.update({"threshold": threshold, "start_date": start_date, "end_date": end_date})
    return summary


def _parse_thresholds(value: str) -> tuple[float | None, ...]:
    thresholds: list[float | None] = []
    for raw in value.split(","):
        token = raw.strip().lower()
        threshold = None if token in {"none", "off", "baseline"} else float(token)
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            raise ValueError("thresholds must be between 0 and 1")
        if threshold not in thresholds:
            thresholds.append(threshold)
    if not thresholds:
        raise ValueError("at least one threshold is required")
    return tuple(thresholds)


def _select_training_candidate(rows: list[dict[str, Any]], target_cash_pct: float) -> dict[str, Any]:
    """Select eligible candidates by Sharpe, then return, then drawdown."""
    eligible = [row for row in rows if row["avg_cash_pct"] <= target_cash_pct]
    pool = eligible or rows
    return max(
        pool,
        key=lambda row: (
            float(row["sharpe_ratio"]),
            float(row["annualized_return_pct"]),
            float(row["max_drawdown_pct"]),
            -float(row["avg_cash_pct"]),
        ),
    )


def _metric_delta(selected: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    """Return selected-minus-baseline holdout metrics for an auditable comparison."""
    fields = (
        "avg_cash_pct",
        "total_return_pct",
        "annualized_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "entries_executed",
    )
    return {field: float(selected[field]) - float(baseline[field]) for field in fields}


def _promotion_decision(
    selected: dict[str, Any],
    baseline: dict[str, Any],
    *,
    min_sharpe_delta: float,
    min_return_delta: float,
    max_drawdown_degradation_pct: float,
) -> dict[str, Any]:
    """Decide whether the selected threshold is safe to promote.

    This is deliberately an output-only decision.  The optimizer never edits
    the simulator or live/paper settings.  A threshold must lower cash while
    preserving the requested holdout return/Sharpe floors and staying within
    the permitted drawdown deterioration.
    """
    delta = _metric_delta(selected, baseline)
    checks = {
        "cash_improved": delta["avg_cash_pct"] < 0.0,
        "return_floor": delta["total_return_pct"] >= min_return_delta,
        "sharpe_floor": delta["sharpe_ratio"] >= min_sharpe_delta,
        "drawdown_limit": delta["max_drawdown_pct"] >= -max_drawdown_degradation_pct,
    }
    eligible = selected.get("threshold") is not None and all(checks.values())
    reasons: list[str] = []
    if selected.get("threshold") is None:
        reasons.append("training selected the bullish-only baseline")
    if not checks["cash_improved"]:
        reasons.append("holdout cash did not improve")
    if not checks["return_floor"]:
        reasons.append("holdout total return fell below the configured floor")
    if not checks["sharpe_floor"]:
        reasons.append("holdout Sharpe fell below the configured floor")
    if not checks["drawdown_limit"]:
        reasons.append("holdout drawdown deterioration exceeded the configured limit")
    if eligible:
        reasons.append("holdout constraints passed")
    return {
        "status": "promote" if eligible else "hold_baseline",
        "eligible": eligible,
        "threshold": selected.get("threshold"),
        "checks": checks,
        "constraints": {
            "min_sharpe_delta": min_sharpe_delta,
            "min_return_delta": min_return_delta,
            "max_drawdown_degradation_pct": max_drawdown_degradation_pct,
        },
        "reasons": reasons,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-data-bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--ticker-cache", type=Path, default=DEFAULT_TICKER_CACHE)
    parser.add_argument("--start-date", default="2023-04-01")
    parser.add_argument("--holdout-start-date", default="2025-04-01")
    parser.add_argument("--end-date", default="2026-04-01")
    parser.add_argument("--thresholds", default="none,0.75,0.60,0.50")
    parser.add_argument("--target-cash-pct", type=float, default=60.0)
    parser.add_argument(
        "--min-holdout-sharpe-delta",
        type=float,
        default=0.0,
        help="minimum selected-minus-baseline holdout Sharpe delta for promotion",
    )
    parser.add_argument(
        "--min-holdout-return-delta",
        type=float,
        default=0.0,
        help="minimum selected-minus-baseline holdout total-return delta for promotion",
    )
    parser.add_argument(
        "--max-holdout-drawdown-degradation-pct",
        type=float,
        default=0.0,
        help="maximum allowed holdout drawdown deterioration in percentage points",
    )
    parser.add_argument("--signal-days", type=int, default=5)
    parser.add_argument("--position-risk-pct", type=float, default=0.01)
    parser.add_argument("--output", type=Path, default=Path("cash-utilization-selection.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.0 <= args.target_cash_pct <= 100.0:
        raise SystemExit("--target-cash-pct must be between 0 and 100")
    if args.max_holdout_drawdown_degradation_pct < 0.0:
        raise SystemExit("--max-holdout-drawdown-degradation-pct must be non-negative")
    if args.signal_days < 1 or args.position_risk_pct <= 0:
        raise SystemExit("signal days must be positive and risk must be greater than zero")
    if not args.start_date < args.holdout_start_date < args.end_date:
        raise SystemExit("dates must satisfy start < holdout-start < end")

    thresholds = _parse_thresholds(args.thresholds)
    price, closes, bundle_sha256 = _load_payloads(args.historical_data_bundle)
    cache = json.loads(args.ticker_cache.read_text(encoding="utf-8"))
    candidates = [
        symbol
        for symbol in cache["tickers"]["sp500"]
        if symbol in price and symbol in closes.columns
    ]
    rs_universe = [str(symbol) for symbol in closes.columns if str(symbol) != "SPY"]
    fetcher = FrozenFetcher(price, closes)
    original_rs = engine.get_sp500_tickers
    engine.get_sp500_tickers = lambda: rs_universe
    try:
        training = [
            _run_candidate(
                threshold=threshold,
                start_date=args.start_date,
                end_date=args.holdout_start_date,
                candidates=candidates,
                fetcher=fetcher,
                signal_days=args.signal_days,
                risk_pct=args.position_risk_pct,
            )
            for threshold in thresholds
        ]
        selected = _select_training_candidate(training, args.target_cash_pct)
        holdout = _run_candidate(
            threshold=selected["threshold"],
            start_date=args.holdout_start_date,
            end_date=args.end_date,
            candidates=candidates,
            fetcher=fetcher,
            signal_days=args.signal_days,
            risk_pct=args.position_risk_pct,
        )
        baseline_holdout = _run_candidate(
            threshold=None,
            start_date=args.holdout_start_date,
            end_date=args.end_date,
            candidates=candidates,
            fetcher=fetcher,
            signal_days=args.signal_days,
            risk_pct=args.position_risk_pct,
        )
    finally:
        engine.get_sp500_tickers = original_rs

    output = {
        "bundle_sha256": bundle_sha256,
        "candidate_count": len(candidates),
        "rs_universe_count": len(rs_universe),
        "thresholds": list(thresholds),
        "target_cash_pct": args.target_cash_pct,
        "training_window": [args.start_date, args.holdout_start_date],
        "holdout_window": [args.holdout_start_date, args.end_date],
        "training_candidates": training,
        "selected_training_candidate": selected,
        "holdout_result": holdout,
        "holdout_baseline": baseline_holdout,
        "holdout_delta_selected_minus_baseline": _metric_delta(holdout, baseline_holdout),
        "promotion_decision": _promotion_decision(
            selected=holdout,
            baseline=baseline_holdout,
            min_sharpe_delta=args.min_holdout_sharpe_delta,
            min_return_delta=args.min_holdout_return_delta,
            max_drawdown_degradation_pct=args.max_holdout_drawdown_degradation_pct,
        ),
    }
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2, sort_keys=True))
    print(f"WROTE {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
