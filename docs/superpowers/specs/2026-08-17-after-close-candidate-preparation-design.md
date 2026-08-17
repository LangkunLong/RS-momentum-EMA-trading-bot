# After-Close Candidate Preparation Design

## Objective

Build a deterministic, read-only preparation command that downloads one completed daily OHLCV dataset after the market closes, eliminates technically ineligible symbols, ranks near-trigger candidates, and persists a full audit trail for the next paper session.

## Why this is the next unit

The 09:31 scheduler evaluates daily breakout and volume evidence that is already fixed at the prior close. Recomputing the broad technical universe during the order session adds latency and can spend scarce fundamentals quota before cheap technical failures are known. A frozen completed-bar snapshot makes the evidence reproducible and observable without changing any order behavior.

## Functional requirements

1. Fetch the configured universe plus SPY through exactly one `fetch_bulk_ohlcv(..., period=settings.RS_CALCULATION_PERIOD)` call.
2. Treat the most recent SPY bar as the authoritative completed session. An optional `--as-of YYYY-MM-DD` must fail if it differs from that session.
3. Mark a symbol stale when its latest bar does not match SPY rather than silently scoring old data.
4. Calculate from the supplied OHLCV only:
   - weighted 12-month performance and cross-sectional RS percentile;
   - 52-week proximity;
   - up-day volume ratio against the 50-day average;
   - prior-window breakout pivot and 0–5% buy-zone position;
   - 50-day average dollar volume;
   - 20-day ATR as a percentage of close;
   - 20-day annualized realized volatility.
5. Preserve the current live technical thresholds exactly:
   - `MIN_RS_SCORE` (currently 80);
   - `S_BREAKOUT_PROXIMITY` (currently 0.95);
   - `S_VOLUME_SURGE_THRESHOLD` (currently 1.30);
   - `BUY_ZONE_EXTENSION_PCT` (currently 0.05);
   - `BUY_ZONE_UNDERCUT_TOLERANCE_PCT` (currently 0.0).
6. Use fewer than 30 completed bars as a blocker. Treat fewer than 252 bars as a warning, not a new live-entry gate.
7. Record exact blocking reasons and warnings for every configured symbol, including missing/stale data.
8. `technical_eligible` means every price/volume gate passed. `tomorrow_executable` additionally requires the existing completed-bar market regime to be bullish. Neither value is an order authorization.
9. Rank deterministically: executable first, technical-eligible second, fewer blockers, smaller normalized trigger gap, higher RS, higher volume ratio, then symbol ascending.
10. Always write:
    - a CSV with every universe member;
    - valid JSON with summary, market regime, rules, shortlist, near misses, and artifact provenance;
    - both files even when the shortlist is empty.
11. The command must never import or call FMP functions, instantiate `TradingClient`, inspect the paper account, or submit/cancel orders.

## Interfaces

`core/after_close_snapshot.py` owns pure calculation:

```python
@dataclass(frozen=True)
class AfterCloseSnapshot:
    as_of_session: date
    market: MarketTrend
    rows: tuple[dict[str, object], ...]
    summary: dict[str, int]

def build_after_close_snapshot(
    price_by_symbol: Mapping[str, pd.DataFrame],
    *,
    market: MarketTrend,
    expected_symbols: Sequence[str],
) -> AfterCloseSnapshot:
    ...

def write_after_close_snapshot(
    snapshot: AfterCloseSnapshot,
    output_dir: Path,
    *,
    generated_at: datetime,
) -> tuple[Path, Path]:
    ...
```

`prepare_after_close.py` owns orchestration:

```python
def prepare_after_close(
    *,
    sectors: str | None = None,
    custom_symbols: Sequence[str] | None = None,
    as_of: date | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    ...
```

CLI:

```text
python prepare_after_close.py [--sectors large_cap] [--symbols AAPL MSFT] [--as-of YYYY-MM-DD] [--output-dir PATH]
```

## Artifact fields

Every CSV row includes: symbol, as-of date, technical eligibility, tomorrow executability, blockers, warnings, RS, weighted performance, close, prior close, pivot, extension, 52-week proximity, 50-day volume ratio, 50-day average dollar volume, 20-day ATR percentage, and 20-day realized volatility.

JSON replaces non-finite values with `null`; it must never emit `NaN` or `Infinity`.

## Non-goals

- No automatic consumption by the scheduler in this unit.
- No overnight order creation.
- No SEC/FMP live eligibility fallback.
- No threshold changes, technical-only buy path, or portfolio sizing changes.
- No hard liquidity or volatility filters until paper evidence supports them.

## Verification

- Unit fixtures cover eligible, low-RS, stale, below-pivot, extended, no-volume, short-history, and bearish-market cases.
- Persistence tests prove empty snapshots still create parseable CSV and strict JSON.
- Orchestration tests prove one bulk download and no FMP/broker boundary calls.
- Full offline project suite, Ruff, compilation, and `git diff --check` must pass with dead proxies and FMP budget zero.
