# Lead C store/workflow interface record

Status: frozen implementation checkpoint and earlier proposal retained for principal-owned overlap review; not implementation acceptance or deployment authority. Starting application source `ab385d792e19ff6db39d87f1123f47f660fc1e1d`.

## Approved producer checkpoint43a0820

Current producer source is `43a0820dea9688fd8583f45ccb9704f172553954`, with report-only head `bfcaa5a8cfdcfc5e32edfd78e4dfd5bb65e5b2d5`. Independent producer specification and quality review approves all seven original findings; `.artifacts/lead-c/issue-100-store-fix3-review-43a0820.md` retains the final disposition. This producer approval does not close the three later integrated consumer findings or grant migration/runtime authority.

The complete committed interface is frozen in `.artifacts/lead-c/store-api-schema-43a0820.json`, SHA256 `f8c6a06bdd3fb17a853e750b3cf2847d503da291c671323c8958bd15b2f94c14`. The static extractor reads Git blobs without importing application code or opening a database. It records 28 public constructor/method signatures, all three read DTOs, and every statement for 15 tables plus one unique index. Schema version1 now has checksum `b1eead321b213c54e3eca905b603cf00d7237d9b2d84f74085dda909b91b7190`. Existing `core/execution_store.py` and `core/execution_workflow.py` remain unchanged from the accepted base. This supersedes the earlier development definition below; an old development database checksum mismatch fails closed and is not an approved upgrade path.

The corrected pointer API requires both `expected_generation_id` and `expected_pointer_version`; callers obtain the observed generation/version/readiness through `load_active_generation_pointer(account)`. The general holding writer requires a version and evidence reference and permits only the named reconciliation-flag transition on an existing holding. Quantity, protection and action transitions retain their named versioned APIs. Canonical reads retain same-account/store holdings, including flat history, and relevant actions across their original generations. The consumer must preserve those origin identities while reconciling the current valuation.

Producer verification is 43 focused cases (19 pure and 24 store), with one disabled-cache configuration warning. A six-module lead checkpoint passed101cases before three additional consumer regressions were added. Final integrated source inventory, corrected consumer evidence, independent integrated review, required CI and principal acceptance remain due. No operational store was opened or migrated.

Later integrated candidate `d2c4082746017dd4ecb14d709d71a7c12b1fc2d5` includes corrected consumerd2a0677 and passes107cases across the six selected offline modules. Its exact matching interface artifact is `.artifacts/lead-c/store-api-schema-d2c4082.json`, SHA256 `28cd7fc40cbc88f2220aba7d8a34a542d7e2bb6ad0e18aaf511948be7be39d6b`; schema and producer bytes remain unchanged from approved43a0820. Source/import inventory and full warning-aware validation are now frozen at this candidate. Independent integrated review, remote CI/publication, principal acceptance and normal merges remain pending.

## Historical implemented checkpoint1847869

Source d52fb22deedc74361f6e4ae4a0113dc4f215c3c2/report1847869884103f86c3053579dfb1b82145a93096 implements the additive store. Exact committed method signatures, DTO fields and every migration statement are extracted in `.artifacts/lead-c/store-api-schema-1847869.json`, SHA256 `8e549f6c18c3cf960747ffea44d0ad2099730782e305a66e6ab7ecf3974b2a39`. The repeatable extractor `build_store_interface_inventory.py` reads Git blobs and AST literals without importing application code or opening a database. Store raw Git SHA256 is `d385e0c85e79ded5fcb209a908ba212969fc7e963820aa5c2a931148ee11ddbd`.

The checkpoint has 27 public constructor/method signatures and 16 schema objects: 15 tables plus the unique `policy_state_one_tier_intent` index. In addition to the original proposal below, the implemented schema includes `policy_state_action_history` and `policy_state_stop_updates`. Schema version1 checksum, computed by the implementation's newline-joined statement rule, is `09301def3abe2391194b0c03adbef7e05b6cfad2e5a38b8dabbf54f835bee67e`. This records a definition; no operational migration was performed. Existing `core/execution_store.py` and `core/execution_workflow.py` have no source changes from the accepted base.

Implemented naming replaces proposed `put_deployment_identity` with `register_deployment_identity`, `update_portfolio_snapshot` with `record_portfolio_snapshot`, and exposes `record_action_intent` plus named cumulative-fill transitions for creation of holdings. Public readers include the individual action, holding, deployment chain, active generation, order aliases, stop intent and portfolio readers. The consumer composes them through `load_policy_execution_snapshot(*, deployment_generation_id, portfolio_snapshot_id)` to obtain one consistent transaction: current portfolio/deployment plus same-account/store holdings and relevant actions across their original pinned generations. Persisted holding and action projections expose `state_version` for CAS callers. The snapshot does not rewrite origin clocks to the current valuation clock.

Full independent review requires seven fixes before acceptance: unknown aggregate risk, uncertainty retention during reference binding, protected holding transitions, late-fill tier recovery, provider-scoped aliases/versioning, partial-entry protection ingestion, and caller-version pointer CAS. The last may change the public pointer signature; protection correction may also add or constrain a writer. Regenerate the exact inventory after corrected source freezes. The 31 focused owner tests and lead's two passing feature-identity tests do not imply those defects are resolved; two first combined tests currently fail at partial-entry protection. Full report: `.artifacts/lead-c/issue-100-full-independent-review-1847869.md`.

The following sections retain the earlier proposal and design rulings as history; implemented names and the frozen artifact above take precedence for this checkpoint. Final API/schema acceptance remains pending corrections and combined review.

Owner #100: chat `01a0f861-76d9-7b11-9722-724643c52221`, worktree2286. Its mutable proposal is `docs/issue-100-state-interface-v1.md`. The revised frozen lead copy is `.artifacts/lead-c/issue-100-schema-proposal-d4c32c84.md`, SHA256 `d4c32c84fce585b9a08349dd4d3f2e920663b2e3904736c678a926a7a8bfc857`. It incorporates the design-review rulings below. The earlier b96ea401 proposal remains retained as review input; it is superseded.

## Paths and ownership

- New `core/policy_execution_state.py`: immutable identity/state types and pure transitions; no providers or settings.
- New `core/policy_execution_store.py`: `PolicyExecutionStateStore(db_path, *, store_identity)`, both required and the identity durably bound to the database; no default singleton or operational sidecar path.
- New dedicated tests and interface/evidence documentation.
- Existing `core/execution_store.py`, `core/execution_workflow.py`, scheduler, order manager and actual store remain unchanged. Any later entry-point edit requires a separate exact overlap record and principal resolution.

## Proposed schema and transaction effects

Only `policy_state_*` tables/indexes: database_identity, schema_migrations, deployments, active_pointers, deployment_events, decisions, holdings, actions, order_attempts, order_reference_aliases, fill_receipts, holding_history and portfolio_snapshots. Independent migration version1; no PRAGMA user_version change. Foreign keys enabled on each connection before the transaction; statement-by-statement transactional DDL and ledger insertion in BEGIN IMMEDIATE. Legacy workflow schemas/rows must remain equivalent on representative temporary databases.

Uniqueness covers decision slots and immutable payloads, generation/holding/tier across sessions, broker/client attempt references, external fill event identities and holding episodes. Attempt watermarks are cumulative/monotonic; expected-version updates use transaction-scoped compare-and-set. Logical action IDs persist across at most one remainder attempt.

## Proposed API

The revised proposal explicitly names `migrate`, `rollback_schema_v1`, `put_deployment_identity`, `record_decision`, `open_holding_from_entry_fill`, `record_cumulative_fill`, `request_order_cancel`, `confirm_order_terminal`, `create_single_remainder_attempt`, `record_explicit_action_resolution`, `advance_exit_tier`, `update_holding_state`, `propose_stop_update`, `confirm_protective_stop`, `update_portfolio_snapshot`, `load_action_projection`, `load_holding_episode`, `load_deployment_chain`, `load_portfolio_snapshot`, and `set_active_generation`. Exact implemented signatures and the creation/read paths for intents, attempts and active pointers remain to be frozen and reviewed with source.

Rollback of schema is distinct from rollback of the active generation. The former rejects any dependent policy-state data with no export-and-drop escape hatch; the latter changes future-entry selection without reassigning existing holdings/actions. Failed migration and restart/concurrent-update behavior require actual tests; prose is not evidence.

## Consumers and overlap disposition

#99 consumes the typed projection with stable action/security identity, explicit clocks, cumulative fills, residual cash/risk and all pending states. It must reconcile broker/client references and explicit security-to-symbol mapping without assuming type/status aliases.

D's currently inspected `docs/next-phase-runtime-operations-ledger.md` lists existing store/workflow methods as consumption-only and says no D method/schema edit proposed or authorized yet. This is a dated no-current-overlap observation, not a release for C to edit existing entry points. D's real runtime remains separately pinned; this proposal is never an instruction to migrate it.

B's final import/map/image owner will receive principal-collected final changed-path/import inventory. New modules and imports require exact source review before image acceptance; old image proof remains dated.

The initial independent schema review is complete; actual owner implementation/source-bound test receipts and independent code review are pending. Only temporary database fixtures are authorized. No provider/broker/runtime operations or policy promotion follows.

## Independent design review disposition

Reviewer retained `.artifacts/lead-c/issue-100-schema-preflight-review.md`: changes requested, no implementation acceptance. All six findings returned to #100. Lead rulings supersede the frozen proposal where they differ:

- Decision slots include category and stable candidate/holding/portfolio subject, plus explicit sequence if needed, not one global record per session. Refreshable account valuation is not slot uniqueness material; immutable payload preserves original clocks and snapshot for conflict detection.
- A fill's receipt, attempt watermark, action residuals, holding quantity/cost/add state, history and resulting terminal/tier state change in one transaction. Opening-entry partial fills are not additions. Missing notional/fees never become invented P&L.
- Transactions enforce relational generation/security/account/store identity; the DB binds its durable identity and history retention. File location is not the store identity.
- Version1 schema rollback rejects any dependent state. The unspecified export-receipt deletion escape hatch is removed; deployment-pointer rollback remains separately supported.
- Order/fill references have explicit account/provider namespace, immutable ancestry and conflicting-replay rejection; per-attempt cumulative quantities remain distinct from action aggregates.
- Portfolio/holding/proposed-versus-confirmed-stop writes require named versioned APIs/history. Active-pointer CAS and pending-entry/readiness checks are atomic. Scope is offline state evidence, not operational activation.

The report also requires actual migration failure-injection, consistent read projections, concurrency, stale-version, oversell and late-fact handling tests. No pass is inferred from the design. Updated owner source/proposal and final full-range independent review remain due.
