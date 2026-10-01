# Lead C store/workflow interface proposal

Status: exact additive proposal retained for principal-owned overlap review; not implementation acceptance or deployment authority. Starting application source `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.

Owner #100: chat `01a0f861-76d9-7b11-9722-724643c52221`, worktree2286. Its mutable proposal is `docs/issue-100-state-interface-v1.md`; frozen lead copy `.artifacts/lead-c/issue-100-schema-proposal-b96ea401.md`, SHA256 `b96ea4012d18fb7fa2353bc64d7b84b053233eea39bfc246bffd1aeac62f8a17`.

## Paths and ownership

- New `core/policy_execution_state.py`: immutable identity/state types and pure transitions; no providers or settings.
- New `core/policy_execution_store.py`: `PolicyExecutionStateStore(db_path: str | Path)`, explicit caller-supplied path; no default singleton or operational sidecar path.
- New dedicated tests and interface/evidence documentation.
- Existing `core/execution_store.py`, `core/execution_workflow.py`, scheduler, order manager and actual store remain unchanged. Any later entry-point edit requires a separate exact overlap record and principal resolution.

## Proposed schema and transaction effects

Only `policy_state_*` tables/indexes: schema_migrations, deployments, active_pointers, deployment_events, decisions, holdings, actions, order_attempts, fill_receipts, holding_history and portfolio_snapshots. Independent migration version1; no PRAGMA user_version change. Foreign keys enabled; transactional DDL and ledger insertion in BEGIN IMMEDIATE. Legacy workflow tables/rows must remain equivalent on representative temporary databases.

Uniqueness covers decision slots and immutable payloads, generation/holding/tier across sessions, broker/client attempt references, external fill event identities and holding episodes. Attempt watermarks are cumulative/monotonic; expected-version updates use transaction-scoped compare-and-set. Logical action IDs persist across at most one remainder attempt.

## Proposed API

`migrate`, `rollback_schema_v1`, `put_deployment_identity`, `set_active_generation`, `load_active_generation`, `record_decision`, `create_holding_episode`, `record_action_intent`, `record_attempt_transition`, `record_cumulative_fill`, `create_single_remainder_attempt`, `record_explicit_action_resolution`, `advance_exit_tier`, `load_holding_episode`, `load_action_projection`, `load_deployment_chain`, `load_portfolio_snapshot`.

Rollback of schema is distinct from rollback of the active generation. The former must reject unsafe loss of dependent state; the latter changes future-entry selection without reassigning existing holdings/actions. Proposed export-receipt recovery needs independent review before any implementation reliance. Failed migration and restart/concurrent-update behavior require actual tests; prose is not evidence.

## Consumers and overlap disposition

#99 consumes the typed projection with stable action/security identity, explicit clocks, cumulative fills, residual cash/risk and all pending states. It must reconcile broker/client references and explicit security-to-symbol mapping without assuming type/status aliases.

D's currently inspected `docs/next-phase-runtime-operations-ledger.md` lists existing store/workflow methods as consumption-only and says no D method/schema edit proposed or authorized yet. This is a dated no-current-overlap observation, not a release for C to edit existing entry points. D's real runtime remains separately pinned; this proposal is never an instruction to migrate it.

B's final import/map/image owner will receive principal-collected final changed-path/import inventory. New modules and imports require exact source review before image acceptance; old image proof remains dated.

The independent schema review and actual owner implementation/source-bound test receipts are pending. Only temporary database fixtures are authorized. No provider/broker/runtime operations or policy promotion follows.

## Independent design review disposition

Reviewer retained `.artifacts/lead-c/issue-100-schema-preflight-review.md`: changes requested, no implementation acceptance. All six findings returned to #100. Lead rulings supersede the frozen proposal where they differ:

- Decision slots include category and stable candidate/holding/portfolio subject, plus explicit sequence if needed, not one global record per session. Refreshable account valuation is not slot uniqueness material; immutable payload preserves original clocks and snapshot for conflict detection.
- A fill's receipt, attempt watermark, action residuals, holding quantity/cost/add state, history and resulting terminal/tier state change in one transaction. Opening-entry partial fills are not additions. Missing notional/fees never become invented P&L.
- Transactions enforce relational generation/security/account/store identity; the DB binds its durable identity and history retention. File location is not the store identity.
- Version1 schema rollback rejects any dependent state. The unspecified export-receipt deletion escape hatch is removed; deployment-pointer rollback remains separately supported.
- Order/fill references have explicit account/provider namespace, immutable ancestry and conflicting-replay rejection; per-attempt cumulative quantities remain distinct from action aggregates.
- Portfolio/holding/proposed-versus-confirmed-stop writes require named versioned APIs/history. Active-pointer CAS and pending-entry/readiness checks are atomic. Scope is offline state evidence, not operational activation.

The report also requires actual migration failure-injection, consistent read projections, concurrency, stale-version, oversell and late-fact handling tests. No pass is inferred from the design. Updated owner source/proposal and final full-range independent review remain due.
