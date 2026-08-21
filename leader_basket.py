"""Run the point-in-time leader-basket benchmark."""

from __future__ import annotations

import argparse

from core.leader_basket import LeaderBasketConfig, LeaderBasketSimulator, print_leader_basket_report
from core.pit_data import PITDataBundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Point-in-time top-leader basket benchmark")
    parser.add_argument("--pit-bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--leader-count", type=int, default=50)
    parser.add_argument("--rebalance-days", type=int, default=20)
    parser.add_argument("--lookback-days", type=int, default=252)
    parser.add_argument("--min-history-days", type=int, default=60)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--export-holdings")
    parser.add_argument("--export-transactions")
    args = parser.parse_args()

    config = LeaderBasketConfig(
        leader_count=args.leader_count,
        rebalance_days=args.rebalance_days,
        lookback_days=args.lookback_days,
        min_history_days=args.min_history_days,
        initial_capital=args.capital,
    )
    with PITDataBundle(args.pit_bundle, expected_sha256=args.bundle_sha256) as bundle:
        result = LeaderBasketSimulator(bundle, config).run(
            start_date=args.start_date,
            end_date=args.end_date,
            benchmark_symbol=args.benchmark,
            tickers=args.tickers,
        )
    print_leader_basket_report(result)
    if args.export_holdings:
        result.holdings.to_csv(args.export_holdings, index=False)
        print(f"Holdings saved to {args.export_holdings}")
    if args.export_transactions:
        result.transactions.to_csv(args.export_transactions, index=False)
        print(f"Transactions saved to {args.export_transactions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
