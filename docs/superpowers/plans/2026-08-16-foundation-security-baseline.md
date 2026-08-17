# Foundation and Security Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a credential-safe, deterministic, lint-clean offline baseline with complete runtime dependency declarations.

**Architecture:** Provider session state remains centralized in `core.data_client`; the reset function clears every session-scoped circuit breaker, and provider errors are rendered from sanitized endpoint metadata. Industry labels move from Yahoo to FMP while preserving the existing seven-day disk cache.

**Tech Stack:** Python 3.11+, pytest, pandas, requests, Ruff, Alpaca, FMP, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-16-paper-trading-stabilization-design.md`

## Global Constraints

- Python support is `>=3.11`.
- Tests and dry runs must not submit broker orders.
- Secrets must not appear in tracked files, prepared URLs, logs, or test output.
- Active risk defaults remain an 8% stop and 12.5% maximum position weight.
- Production market data comes from Alpaca and production fundamental/company-profile data comes from FMP.

---

### Task 1: Deterministic Test and Ruff Baseline

**Files:**
- Modify: `tests/test_backtest_pnl.py`
- Modify: `core/notifier.py`
- Modify: `backtest_pnl.py`
- Modify: `tests/test_e2e_flow.py`
- Modify: `tests/test_fill_monitor.py`
- Modify: `tests/test_hourly_monitor.py`
- Modify: `tests/test_notifier.py`
- Modify: `tests/test_order_execution.py`
- Modify: `tests/test_regression.py`

**Interfaces:**
- Consumes: pandas business-date generation and the existing notifier/backtest public APIs.
- Produces: deterministic synthetic OHLCV fixtures and Python 3.11-valid, Ruff-clean source.

- [ ] **Step 1: Reproduce the deterministic-date failures**

Run: `python -m pytest tests/test_backtest_pnl.py -q --no-cov`

Expected: 14 failures where `pd.date_range(..., freq="B")` returns one fewer row when the wall-clock end date is a weekend.

- [ ] **Step 2: Replace wall-clock fixture dates with a fixed business-day end**

Use one literal fixture boundary in `tests/test_backtest_pnl.py`:

```python
_FIXTURE_END = pd.Timestamp("2026-07-31")


def _business_dates(periods: int) -> pd.DatetimeIndex:
    return pd.bdate_range(end=_FIXTURE_END, periods=periods)
```

Every synthetic price helper must call `_business_dates(n)` instead of deriving dates from `datetime.now()`.

- [ ] **Step 3: Verify the backtest test module passes**

Run: `python -m pytest tests/test_backtest_pnl.py -q --no-cov`

Expected: all tests in the module pass.

- [ ] **Step 4: Reproduce and correct Ruff findings without changing behavior**

Run: `python -m ruff check . --no-cache --exclude .artifacts`

Apply only the reported mechanical corrections: remove unused test imports/variables, make notifier optional lines separate local strings instead of nested f-strings containing backslashes, and remove or rename the unreachable duplicate legacy `Trade` and `SimulationResult` definitions so the imported production types have one meaning.

- [ ] **Step 5: Verify lint and the full offline suite**

Run: `python -m ruff check . --no-cache --exclude .artifacts`

Run: `python -m pytest -q --no-cov`

Expected: Ruff exits zero; any remaining pytest failure must be a separately diagnosed product defect, not the date fixture.

- [ ] **Step 6: Commit the deterministic baseline**

```bash
git add tests/test_backtest_pnl.py core/notifier.py backtest_pnl.py tests/test_e2e_flow.py tests/test_fill_monitor.py tests/test_hourly_monitor.py tests/test_notifier.py tests/test_order_execution.py tests/test_regression.py
git commit -m "test: restore deterministic Python 3.11 baseline"
```

### Task 2: Reset the FMP Circuit Breaker and Sanitize Errors

**Files:**
- Modify: `tests/test_fmp_resilience.py`
- Modify: `core/data_client.py`

**Interfaces:**
- Consumes: `clear_session_cache() -> None` and `_fmp_get(endpoint: str, params: dict | None) -> Any`.
- Produces: a full session reset and endpoint-only provider error messages.

- [ ] **Step 1: Write the failing session-reset test**

```python
def test_clear_session_cache_resets_quota_circuit_breaker() -> None:
    data_client._fmp_quota_exhausted = True

    clear_session_cache()

    assert data_client._fmp_quota_exhausted is False
```

The test catches a new scan remaining permanently disabled after an earlier 429 or retry exhaustion.

- [ ] **Step 2: Run the reset test and observe the failure**

Run: `python -m pytest tests/test_fmp_resilience.py::test_clear_session_cache_resets_quota_circuit_breaker -q --no-cov`

Expected: failure because the flag remains `True`.

- [ ] **Step 3: Implement the complete session reset**

Declare the module-level flag global inside `clear_session_cache()` and set it to `False` after clearing caches:

```python
def clear_session_cache() -> None:
    global _fmp_quota_exhausted
    with _cache_lock:
        _session_cache.clear()
    _fmp_unavailable_endpoints.clear()
    _fmp_reported_endpoint_failures.clear()
    _fmp_quota_exhausted = False
```

- [ ] **Step 4: Write a failing secret-redaction test**

Create a fake response whose `raise_for_status()` raises `requests.HTTPError` containing a prepared URL with `apikey=credential-value`. Patch `_fmp_session.get`, call `_fmp_get("profile", {"symbol": "AAPL"})`, capture stdout, and assert `credential-value` and `apikey=` are absent while `profile` and the HTTP status remain visible.

- [ ] **Step 5: Run the redaction test and observe the failure**

Run: `python -m pytest tests/test_fmp_resilience.py -k "redact or reset" -q --no-cov`

Expected: the current exception interpolation exposes the prepared URL.

- [ ] **Step 6: Render sanitized provider errors**

Replace exception interpolation with endpoint and response-status metadata. Do not stringify a `requests` exception that can contain the prepared URL:

```python
status = getattr(resp, "status_code", "unknown")
print(f"[FMP] HTTP {status} on '{endpoint}'.")
```

- [ ] **Step 7: Verify FMP resilience behavior**

Run: `python -m pytest tests/test_fmp_resilience.py -q --no-cov`

Expected: all tests pass and captured logs contain no credential value.

- [ ] **Step 8: Commit provider security behavior**

```bash
git add core/data_client.py tests/test_fmp_resilience.py
git commit -m "fix: reset and sanitize FMP provider sessions"
```

### Task 3: Replace Yahoo Industry Lookup with FMP

**Files:**
- Modify: `tests/test_industry_group.py`
- Modify: `core/fmp_provider.py`
- Modify: `core/data_client.py`
- Modify: `core/industry_group.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `_fmp_get(endpoint, params)` through dependency injection.
- Produces: `fetch_company_profile(symbol, fmp_get_fn) -> dict[str, str]` and `load_industry_map(tickers) -> dict[str, str]` with the existing cache contract.

- [ ] **Step 1: Write the failing FMP profile normalization tests**

Add literal cases to `tests/test_industry_group.py` proving that `fetch_company_profile()` returns `{"industry": "Semiconductors", "sector": "Technology"}` from a one-record FMP response, returns `{}` for an empty/malformed response, and prefers a non-empty industry label over sector.

- [ ] **Step 2: Run the provider tests and observe the missing function**

Run: `python -m pytest tests/test_industry_group.py -q --no-cov`

Expected: import or attribute failure for `fetch_company_profile`.

- [ ] **Step 3: Implement the pure FMP profile adapter**

```python
def fetch_company_profile(symbol: str, fmp_get_fn: Callable[..., Any]) -> dict[str, str]:
    raw = fmp_get_fn("profile", {"symbol": symbol})
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return {}
    record = raw[0]
    return {
        key: str(record[key]).strip()
        for key in ("industry", "sector")
        if record.get(key) and str(record[key]).strip()
    }
```

- [ ] **Step 4: Write the failing industry-map cache test**

Patch the data-client profile function at the external provider boundary, request two symbols, and assert the returned map uses industry first, sector second, omits empty profiles, and writes the same seven-day cache schema. A second call must use the cache and make no provider request.

- [ ] **Step 5: Replace `yfinance` in `load_industry_map()`**

Remove the `yfinance` import. Expose a data-client wrapper that calls the pure FMP adapter, and have `load_industry_map()` call that wrapper for cache misses. Keep partial-map and exception-degradation behavior unchanged.

- [ ] **Step 6: Align runtime dependency manifests**

Make `requirements.txt` and `[project].dependencies` contain the same runtime packages: `alpaca-py`, `requests`, `pandas`, `numpy`, `python-dateutil`, `python-dotenv`, `beautifulsoup4`, `cachetools`, and `plotly`. Keep pytest, pytest-cov, Ruff, and pre-commit in a clearly marked development section or optional dependency group.

- [ ] **Step 7: Verify provider, dependency, and import behavior**

Run: `python -m pytest tests/test_industry_group.py tests/test_data_client.py tests/test_fmp_resilience.py -q --no-cov`

Run: `python -m ruff check core/industry_group.py core/fmp_provider.py core/data_client.py --no-cache`

Run: `python -m pip check`

Expected: all commands exit zero and `rg -n "yfinance" --glob "*.py"` returns no production import.

- [ ] **Step 8: Commit the provider consolidation**

```bash
git add core/industry_group.py core/fmp_provider.py core/data_client.py tests/test_industry_group.py pyproject.toml requirements.txt
git commit -m "refactor: source industry profiles from FMP"
```

### Task 4: Add Offline CI and Credential Scanning

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Ruff and pytest commands established above.
- Produces: a pull-request and push quality gate with no broker credentials or integration calls.

- [ ] **Step 1: Add the CI workflow**

Create a workflow for Python 3.11 and 3.13 that installs the project plus dev tools, runs `python -m ruff check .`, compiles project Python files, and runs `python -m pytest -q --no-cov -m "not integration"`. Do not define Alpaca or FMP secrets in the workflow.

- [ ] **Step 2: Keep local-only and generated state ignored**

Ensure `.gitignore` includes `.claude/settings.local.json`, `.worktrees/`, `.artifacts/`, execution databases/directories, scan results, and backtest result files.

- [ ] **Step 3: Verify workflow syntax and tracked-tree secret hygiene**

Run: `python -c "import pathlib, yaml; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text())"`

Run a filename-only credential scan over tracked content and verify it reports zero credential-shaped assignments or query parameters.

- [ ] **Step 4: Run the exact CI commands locally**

Run: `python -m ruff check . --no-cache --exclude .artifacts`

Run: `python -m compileall -q -x "[\\/](\.git|\.worktrees|\.artifacts|\.venv)[\\/]" .`

Run: `python -m pytest -q --no-cov -m "not integration"`

Expected: all commands exit zero.

- [ ] **Step 5: Commit CI**

```bash
git add .github/workflows/ci.yml .gitignore
git commit -m "ci: add offline Python quality gates"
```
