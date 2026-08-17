# After-Close Candidate Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a zero-FMP, zero-broker after-close command that produces a completed-bar technical shortlist and full rejection audit for the next paper session.

**Architecture:** A pure `core.after_close_snapshot` module calculates and serializes the snapshot from caller-supplied OHLCV. A thin `prepare_after_close.py` command resolves the universe, performs exactly one bulk download, evaluates SPY from that same dataset, and calls the pure module. The scheduler and order path are unchanged.

**Tech Stack:** Python 3.11/3.13, pandas, numpy, existing Alpaca historical-data client, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-17-after-close-candidate-preparation-design.md`

## Global Constraints

- The installed scheduler and permanent runtime must remain untouched.
- No FMP request, `TradingClient`, account query, or broker mutation is allowed in this feature.
- Preserve `MIN_RS_SCORE`, `S_BREAKOUT_PROXIMITY`, `S_VOLUME_SURGE_THRESHOLD`, `BUY_ZONE_EXTENSION_PCT`, and `BUY_ZONE_UNDERCUT_TOLERANCE_PCT` exactly from settings.
- The latest SPY bar is the authoritative completed session; stale symbols fail closed.
- Fewer than 30 bars blocks evaluation; fewer than 252 bars is only a warning.
- CSV and strict JSON are written even when there are zero eligible candidates.
- Tests run with `FMP_DAILY_REQUEST_BUDGET=0` and dead HTTP(S) proxies.

---

### Task 1: Pure completed-bar snapshot engine

**Files:**
- Create: `core/after_close_snapshot.py`
- Create: `tests/test_after_close_snapshot.py`

**Interfaces:**
- Consumes: `Mapping[str, pd.DataFrame]`, `MarketTrend`, and expected symbols.
- Produces: `AfterCloseSnapshot`, `build_after_close_snapshot(...)`, and `write_after_close_snapshot(...)` with the exact signatures in the spec.

- [ ] **Step 1: Write failing calculation tests**

Create literal 260-session OHLCV fixtures whose last two bars make each outcome independently observable. Tests must name the production break they catch and assert:

```python
snapshot = build_after_close_snapshot(
    {"SPY": bullish_spy, "LEAD": qualifying_leader, "LOW": low_rs},
    market=bullish_market,
    expected_symbols=["LEAD", "LOW", "MISSING"],
)
rows = {row["symbol"]: row for row in snapshot.rows}
assert rows["LEAD"]["technical_eligible"] is True
assert rows["LOW"]["blocking_reasons"] == "rs_below_threshold"
assert rows["MISSING"]["blocking_reasons"] == "missing_price_history"
```

Add separate tests for stale session, no up-day volume surge, below pivot, beyond buy zone, under 30 bars, 30–251 bar warning, bearish market, and deterministic ordering.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider --no-cov -q tests/test_after_close_snapshot.py
```

Expected: collection fails because `core.after_close_snapshot` does not exist.

- [ ] **Step 3: Implement the pure engine minimally**

Implement:

```python
@dataclass(frozen=True)
class AfterCloseSnapshot:
    as_of_session: date
    market: MarketTrend
    rows: tuple[dict[str, object], ...]
    summary: dict[str, int]
```

Use `calculate_weighted_performance`, `_detect_breakout`, `_detect_volume_surge`, and the prior 252-bar maximum excluding the latest close. Derive cross-sectional RS with:

```python
rs = performance.rank(pct=True) * settings.RS_PERCENTILE_MULTIPLIER + settings.RS_PERCENTILE_MIN
```

Normalize trigger gap by the failed threshold. Replace all pandas/numpy scalars with JSON-safe builtins and every non-finite number with `None` before strict `json.dumps(..., allow_nan=False)`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all snapshot tests pass.

- [ ] **Step 5: Run Ruff and compile checks**

```powershell
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\ruff.exe' check core/after_close_snapshot.py tests/test_after_close_snapshot.py
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\python.exe' -m py_compile core/after_close_snapshot.py tests/test_after_close_snapshot.py
```

- [ ] **Step 6: Commit**

```powershell
git add core/after_close_snapshot.py tests/test_after_close_snapshot.py
git commit -m "feat: add completed-bar snapshot engine"
```

### Task 2: One-download preparation CLI and durable artifacts

**Files:**
- Create: `prepare_after_close.py`
- Create: `tests/test_prepare_after_close.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1's `build_after_close_snapshot` and `write_after_close_snapshot`.
- Produces: `prepare_after_close(...) -> tuple[Path, Path]`, `build_parser()`, and `main(argv=None) -> int` with the CLI in the spec.

- [ ] **Step 1: Write failing orchestration tests**

Use a fake bulk download that returns full Alpaca-shaped OHLCV mappings and assert the observable result, not the mock itself:

```python
csv_path, json_path = prepare_after_close(
    custom_symbols=["LEAD", "MISS"],
    as_of=date(2026, 8, 17),
    output_dir=tmp_path,
)
assert csv_path.exists()
payload = json.loads(json_path.read_text(encoding="utf-8"))
assert payload["as_of_session"] == "2026-08-17"
assert payload["summary"]["configured_symbols"] == 2
```

Add tests proving: one bulk boundary invocation contains SPY and all deduplicated symbols; mismatched `--as-of` exits nonzero without artifacts; empty shortlist still writes both artifacts; JSON contains no `NaN`; parser accepts `--symbols`; and no FMP/broker module is imported as a side effect.

- [ ] **Step 2: Run CLI tests and verify RED**

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider --no-cov -q tests/test_prepare_after_close.py
```

Expected: collection fails because `prepare_after_close` does not exist.

- [ ] **Step 3: Implement the thin orchestrator**

Resolve symbols with `get_index_tickers(sectors or settings.SECTORS)` unless `custom_symbols` is supplied, append `settings.EXTRA_SYMBOLS` only for configured-universe runs, deduplicate, and append SPY for the bulk request. Call:

```python
price_by_symbol = fetch_bulk_ohlcv(
    download_symbols,
    period=settings.RS_CALCULATION_PERIOD,
    chunk_size=100,
)
market = evaluate_m(price_data=price_by_symbol.get("SPY"))
snapshot = build_after_close_snapshot(
    price_by_symbol,
    market=market,
    expected_symbols=universe_symbols,
)
```

Default output directory is `Path(settings.SCAN_RESULTS_DIR) / "after_close"`. File names include the as-of session and generation timestamp. Print concise counts and artifact paths; do not print every row.

- [ ] **Step 4: Document the command**

Add a README section with:

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
.\.venv\Scripts\python.exe -u .\prepare_after_close.py
```

State that it downloads completed daily prices, never submits orders, never consumes FMP quota, and its shortlist requires fresh execution-time safety/price revalidation.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all CLI tests pass.

- [ ] **Step 6: Run the complete offline gate**

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
$env:HTTP_PROXY='http://127.0.0.1:9'
$env:HTTPS_PROXY='http://127.0.0.1:9'
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider --no-cov -q
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\ruff.exe' check core/after_close_snapshot.py prepare_after_close.py tests/test_after_close_snapshot.py tests/test_prepare_after_close.py
& 'C:\Projects\trading_bot\paper-trading-runtime\.venv\Scripts\python.exe' -m py_compile core/after_close_snapshot.py prepare_after_close.py tests/test_after_close_snapshot.py tests/test_prepare_after_close.py
git diff --check
```

Expected: all project tests pass offline; lint, compilation, and diff checks exit zero.

- [ ] **Step 7: Commit**

```powershell
git add prepare_after_close.py tests/test_prepare_after_close.py README.md
git commit -m "feat: add after-close preparation command"
```
