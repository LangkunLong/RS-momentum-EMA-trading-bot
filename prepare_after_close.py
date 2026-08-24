"""Create a read-only, completed-bar candidate snapshot after market close."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from config import settings
from core.trading_sessions import canonicalize_us_equity_history


def fetch_bulk_ohlcv(symbols: list[str], *, period: str, chunk_size: int):
    """Load the OHLCV boundary only when the command actually runs."""
    from core.data_client import fetch_bulk_ohlcv as fetch

    return fetch(symbols, period=period, chunk_size=chunk_size)


def evaluate_m(*, price_data):
    """Evaluate market regime from the already-downloaded SPY frame."""
    from core.canslim.m_market_direction import evaluate_m as evaluate

    return evaluate(price_data=price_data)


def prepare_after_close(
    *,
    sectors: str | None = None,
    custom_symbols: Sequence[str] | None = None,
    as_of: date | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Download one technical universe and persist an advisory snapshot."""
    universe_symbols = _resolve_symbols(sectors=sectors, custom_symbols=custom_symbols)
    download_symbols = list(universe_symbols)
    if "SPY" not in download_symbols:
        download_symbols.append("SPY")

    price_by_symbol = fetch_bulk_ohlcv(
        download_symbols,
        period=settings.RS_CALCULATION_PERIOD,
        chunk_size=100,
    )
    raw_spy_history = price_by_symbol.get("SPY")
    if raw_spy_history is None or raw_spy_history.empty:
        raise ValueError("SPY price history is required to determine the completed session")
    spy_history = canonicalize_us_equity_history(raw_spy_history)
    price_by_symbol = dict(price_by_symbol)
    price_by_symbol["SPY"] = spy_history
    observed_as_of = date.fromisoformat(str(spy_history.index[-1].date()))
    if as_of is not None and as_of != observed_as_of:
        raise ValueError(f"--as-of {as_of.isoformat()} does not match completed SPY session {observed_as_of.isoformat()}")

    from core.after_close_snapshot import build_after_close_snapshot, write_after_close_snapshot

    market = evaluate_m(price_data=spy_history)
    snapshot = build_after_close_snapshot(price_by_symbol, market=market, expected_symbols=universe_symbols)
    snapshot = _with_configured_symbols(snapshot, len(universe_symbols))
    target_dir = output_dir if output_dir is not None else Path(settings.SCAN_RESULTS_DIR) / "after_close"
    generated_at = datetime.now(timezone.utc)
    csv_path, json_path = write_after_close_snapshot(snapshot, target_dir, generated_at=generated_at)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    return (
        csv_path.replace(csv_path.with_stem(f"{csv_path.stem}_{timestamp}")),
        json_path.replace(json_path.with_stem(f"{json_path.stem}_{timestamp}")),
    )


def _resolve_symbols(*, sectors: str | None, custom_symbols: Sequence[str] | None) -> list[str]:
    if custom_symbols is not None:
        source_symbols = custom_symbols
    else:
        from quality_stocks import get_index_tickers

        source_symbols = [*get_index_tickers(sectors or settings.SECTORS), *settings.EXTRA_SYMBOLS]
    return list(dict.fromkeys(str(symbol).upper().strip() for symbol in source_symbols if str(symbol).strip()))


def _with_configured_symbols(snapshot, configured_symbols: int):
    summary = dict(snapshot.summary)
    summary["configured_symbols"] = configured_symbols
    return type(snapshot)(
        as_of_session=snapshot.as_of_session,
        market=snapshot.market,
        rows=snapshot.rows,
        summary=summary,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the read-only snapshot CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sectors", help="Configured index alias to prepare")
    parser.add_argument("--symbols", nargs="+", help="Explicit symbols instead of a configured universe")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Required completed session date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=Path, help="Directory for CSV and JSON artifacts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the advisory preparation command."""
    args = build_parser().parse_args(argv)
    try:
        csv_path, json_path = prepare_after_close(
            sectors=args.sectors,
            custom_symbols=args.symbols,
            as_of=args.as_of,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        print(f"After-close preparation failed: {exc}")
        return 2
    print(f"After-close snapshot written: {csv_path}")
    print(f"After-close snapshot metadata: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
