# Paper-Trading Stabilization Design

**Date:** 2026-08-16

**Status:** Approved for implementation by the user's instruction to execute the recommended stabilization order.

## Objective

Turn the current local CANSLIM scanner, backtester, and Alpaca paper-execution work into a secure, deterministic, reviewable paper-trading system. The system must preserve the existing strategy work, make the offline quality gates green on Python 3.11+, and prove recovery behavior before any supervised broker-side validation.

## Current State

The local branch contains 35 commits not present on `origin/main`, plus a large paper-execution implementation that originally existed only as dirty and untracked files. The recovery snapshot is preserved at commit `c526fea` and in `.artifacts/recovery/2026-08-16-pre-stabilization/`. The implementation branch is `codex/stabilize-paper-trading` in the linked worktree `.worktrees/stabilize-paper-trading`.

The deterministic baseline has 327 passing tests, 14 failing tests, two deselected integration tests, and 20 Ruff findings. The failures are caused by wall-clock-dependent business-day fixtures under pandas 3.0.1. The Ruff findings include Python 3.11-invalid nested f-strings, duplicate legacy backtest definitions, and unused test imports.

An FMP API credential was committed in a generated scan CSV in public Git history. Deleting the current file does not revoke the credential or remove it from existing commits.

## Architecture Decision

`core.execution_workflow.ExecutionWorkflow` is the only owner of order lifecycle transitions. `core.execution_store.ExecutionStore` persists workflow snapshots, transition history, broker order references, and active-position cost basis. `core.order_manager.OrderManager` is the single orchestration boundary used by the auto-trader and fill monitor.

Alpaca is the source of truth for submitted orders, fills, open positions, and protective orders. SQLite is a local audit and recovery view. On startup or after a missing in-memory workflow, the application resolves state from durable broker references and active-position records, then reconciles it against Alpaca. No second portfolio subsystem will be introduced during stabilization.

The transition history is append-only. Snapshot rows may be updated for fast lookup, but every lifecycle change must retain its transition record. Workflow resolution follows the strongest reference order: explicit workflow id, client order id, broker order id, active symbol ownership, then latest symbol workflow.

## Behavioral Contracts

### Security

- FMP credentials come only from environment-backed settings.
- FMP credentials must not appear in tracked files, exception strings, generated CSVs, logs, test output, or recovery artifacts intended for publication.
- Provider errors log the endpoint name and sanitized status, never a prepared URL containing query parameters.
- `clear_session_cache()` starts a genuinely new provider session by clearing cached data, unavailable endpoint state, reported failures, and the quota-exhausted circuit breaker.
- The exposed FMP credential must be rotated at FMP. Local code cannot perform provider-side rotation.
- Public history cleanup is prepared and verified locally before any coordinated force-push; rewriting the public branch is not part of an ordinary local commit.

### Backtesting

- Tests use fixed business dates and cannot change result length based on the day they run.
- Signal ranking may consider new candidates when the portfolio is full and eviction is enabled.
- A candidate can evict only a lower-RS existing position under the documented two-pass rule; when eviction is disabled, a full portfolio admits no new position.
- A signal row remains recorded even when market regime prevents entry.
- Strategy defaults remain an 8% hard stop and 12.5% maximum position weight, derived from 1% portfolio risk divided by an 8% stop.
- `core.backtest_engine` is the production implementation. The root `backtest_pnl.py` remains a compatibility CLI/import surface and must not define a second competing engine.

### Paper Execution

- `run_auto_trader()` returns an immutable cycle result containing the exact entry and exit symbols acted upon.
- The scheduler passes that result to `notify_cycle_summary()`; it does not manufacture empty lists.
- A sell fill notification obtains entry cost basis from the resolved workflow or persisted active-position record. It must never silently substitute the sell fill price as the entry price.
- When cost basis cannot be recovered, the notification clearly marks P&L unavailable rather than reporting a false zero.
- Buy fills place or reconcile a protective stop from the actual fill price.
- Partial fills update protection without sending a final-fill notification.

### Data Providers and Dependencies

- Alpaca provides market price and broker data.
- FMP provides fundamental and company-profile data, including industry labels.
- The industry map no longer depends on `yfinance`.
- Both dependency manifests describe the same runtime dependencies. `plotly` is a runtime dependency because production visualization modules import it.
- A deterministic lock or constraints artifact records the verified environment used for CI and operations.

### Operations

- Offline tests, lint, compilation, and dry-run commands must not submit broker orders.
- A GitHub Actions workflow runs Ruff and the non-integration pytest suite on supported Python versions.
- The README explains setup, required environment variables, paper/live safety boundaries, dry-run verification, execution storage, and recovery.
- A supervised one-share Alpaca paper order and Windows scheduled-task installation require explicit approval immediately before those external state changes.
- Live trading remains out of scope.

## Implementation Boundaries

The work is divided into four independently reviewable plans:

1. `2026-08-16-foundation-security-baseline.md` — credential-safe provider behavior, deterministic baseline, dependencies, and CI.
2. `2026-08-16-backtest-correctness.md` — eviction reachability and compatibility-layer consolidation.
3. `2026-08-16-paper-execution-recovery.md` — cycle result contract and sell-fill cost-basis recovery.
4. `2026-08-16-operations-readiness.md` — risk documentation, runbooks, full verification, and supervised external validation gates.

## Acceptance Criteria

- The complete non-integration pytest suite passes from a clean worktree.
- Ruff passes with target version Python 3.11, and all project Python files compile under the available interpreter.
- Regression tests prove session reset, secret-safe errors, full-portfolio eviction, scheduler result propagation, and recovered sell P&L.
- Runtime dependency manifests are consistent and include every production import.
- No credential-shaped FMP value exists in the final tracked tree.
- The branch is organized into reviewable commits and can be proposed as a pull request without generated artifacts or machine-local configuration.
- Broker order submission and scheduled-task installation remain pending until the user explicitly authorizes each supervised external action.
