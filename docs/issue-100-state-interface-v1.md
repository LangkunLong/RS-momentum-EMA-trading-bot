# Issue #100 durable policy state interface, revision 1

Status: additive API and SQLite implementation. Pure state types live in
`core/policy_execution_state.py`; the independent persistence implementation
lives in `core/policy_execution_store.py`. Existing
`core/execution_store.py` and `core/execution_workflow.py` remain unchanged.

## Accepted contract sources

This interface follows the accepted #97 deployment contract, #80 strategy
policy contract, #66 historical feature contract, and the lead's shared
paper-policy ledger revision 1. It uses fixed policy outputs and synthetic
or recorded fixtures. It does not alter #71 calculators, invoke a provider,
load a broker, start the runtime, or change scheduler/order wiring.

## Canonical identity and clock

`PolicyDeploymentIdentity` freezes `policy_artifact_id`,
`capability_manifest_id`, `policy_interface_version`, `feature_contract_id`,
`feature_calculator_id`, `source_revision`, `runtime_identity`,
`execution_profile_id`, `paper_account_environment_id`, and `store_identity`.
The generation ID is the SHA-256 of those canonical fields. Credentials never
enter identity material. Decisions, holdings, actions, and portfolio snapshots
reference this immutable row through checked foreign keys.

`DecisionClock` retains the exchange, completed feature session, feature
cutoff, explicit next eligible execution session, and timezone-aware account
valuation session/time separately. The account valuation may occur at the next
execution opportunity after feature cutoff; its session must match that
declared opportunity. Clock timestamps are preserved in the decision payload,
but the account valuation time is excluded from the uniqueness key for a
decision slot.

`DecisionIdentity` requires an explicit decision category, stable subject
type/ID (candidate, security, holding, or portfolio), and optional explicit
sequence where the policy contract permits multiple decisions for that exact
subject/category/session. The slot ID hashes deployment + exchange + completed
session + category + subject + sequence. It deliberately excludes valuation
refresh and cutoff timestamps. The decision ID hashes that slot with the full
clock payload and policy-visible snapshot digest. Replaying identical input is
idempotent. Changed cutoff, valuation or snapshot in the same slot is a
conflict. A different category or subject remains a separate decision.

An action stays bound to its original next execution session. Evaluation
before that session is not due; evaluation afterward is a missed action that
must be explicitly resolved, never silently retargeted.

## Holding, action and fill semantics

`HoldingEpisode` is pinned to its opening generation, stable security ID,
broker symbol, opening action, and opening decision. It keeps first confirmed
opening quantity immutable; later fills of that same opening order increase
remaining quantity but never count as additions. Confirmed add-on actions
track per-action cumulative watermarks, add count, aggregate added quantity,
weighted cost basis when execution notional/fees are known, and remaining
quantity. Sells update remaining quantity and realized P&L only from confirmed
execution notional/fees; if those facts are unavailable, cost/P&L remains
`None`, never fabricated as zero.

The holding snapshot retains optional cost basis, realized P&L, open/committed
risk and explicit risk basis, proposed stop, proposed stop action identity,
broker-confirmed protective stop with client/broker order references and
observation time, holding peak, last accepted policy session, persistent
policy flags, policy tier, and pending action identities. Persisted holding
reads also expose the row's `state_version` for compare-and-set writers. A stop
proposal is not represented as broker protection until the matching
confirmation arrives.
Each new proposal is checked against both the latest proposal and the strongest
broker-confirmed stop, so a broker confirmation better than requested cannot
enable a weaker subsequent proposal. Unavailable prices/risk remain `None`.
Peaks and confirmed stops are monotonic under this contract.

`ActionIntent` has a stable logical identity separate from attempt identities.
Entry/add-on/replacement IDs include generation, decision, security, role and
subject. A scale-out tier ID is unique by generation + holding episode + tier
across decision sessions; its immutable origin decision, snapshot quantity,
requested fraction, rounded target, and rounding-rule ID remain attached. A
later decision cannot re-open an unresolved tier with a new quantity.

Each order attempt has a numbered identity and immutable client/broker order
references. The initial order is attempt 1; #97's conservative bounded
remainder is attempt 2 under the same logical action ID. Cancel request and
timeout remain pending. Only broker-confirmed terminal cancellation/rejection
allows the one remainder attempt. A second terminal underfill becomes
`partial_incomplete` and requires explicit reconciliation with a reason before
tier advancement. A proposal, cancel request, timeout, or underfill does not
advance a tier. A close cannot supersede an unresolved tier or unreconciled
position fact.

A fill watermark update does not clear `cancel_requested` or
`reconciliation_required`; only explicit terminal/reconciliation evidence may
clear those states. A late fill observation after broker-terminal evidence is
retained and marks the attempt/action `reconciliation_required`, preserving the
terminal fact separately. The initial and attempt-numbered fill APIs share
these rules. If a remainder was already issued, its requested quantity stays
fixed when an earlier attempt's cumulative fill is corrected. Aggregate fills
above the logical target remain visible as reconciliation-required exposure;
they cannot silently reset the remainder or become ready for another order. A
logical action cannot become filled or resolved while any issued attempt remains
live. An `INTENDED` attempt with no client/broker references and no fill is
durably known as unissued and can be explicitly resolved without inventing a
broker terminal result. Any attempt with an order reference, fill, or
non-`INTENDED` live status remains blocked until terminal evidence is recorded.
After every issued attempt is terminal and holdings reconcile, an explicit
resolution may retain an above-target confirmed quantity and reason while the
original requested target remains unchanged.

Every confirmed cumulative-fill update carries attempt number, stable provider
event ID and immutable payload digest, cumulative quantity, cumulative notional
and cumulative fees (each may remain explicitly unknown). Replaying an
identical event applies zero delta; reusing the event ID for a different
payload fails closed. Attempt-level watermarks are monotonic and aggregate
action fills are their sum. One SQLite transaction must record the receipt,
update attempt/action residual cash and risk, apply holding quantity/cost/P&L
or opening-entry continuation, append history, and apply any resulting
completion/tier transition. Reads composing those rows use one read
transaction. No projection may expose a partial fill transaction.

The store preserves all observed client/broker aliases, attempt ancestry, and
provider/account namespace. Within a logical action, a client or broker
reference cannot identify two attempts, including through aliases. A scoped
external reference already bound to another action/attempt is rejected. Fill
identity is scoped by provider + paper account environment + store identity +
external fill-event ID, rather than assuming a broker ID is globally unique.

## Read-only #99 projection

Import from `core.policy_execution_state`:

```python
from core.policy_execution_state import ActionStateProjection
from core.policy_execution_store import PolicyExecutionStateStore
```

`PolicyExecutionStateStore.load_action_projection(logical_action_id)` returns
one `ActionStateProjection` with the exact immutable `deployment_identity`,
store/account/generation/decision/clock/holding/action identities, completed
decision and next execution sessions, separate feature cutoff and account
valuation time, stable security ID and broker symbol, action role/status, the
originating snapshot/tier/attempt numbers, requested and action-wide
cumulative confirmed quantities, residual target, cash reservation price and
basis, optional reservation stop price, per-unit and residual committed risk
with explicit basis, and all client/broker references by attempt.

Persisted action projections carry `state_version`; pure projections leave it
`None` because they have no database row version. Persisted holding rows expose
the same `state_version` through `HoldingEpisode`, so a consumer can restart,
load the combined read below, and pass the returned version to a CAS writer.

The projection includes sell/cancel/reconciliation/partial-incomplete records
even when they reserve no buy cash. Unknown pending risk stays `None` and
blocks risk-increasing readiness. #99 joins each logical reservation once
across matching local and broker references; it reserves only residual
executable quantity. Cash already reflects confirmed fills, so those fills
are not reserved/subtracted again. Residual cash and risk are separate from
actual cash, gross exposure, and open-position risk; the existing cash + gross
exposure = equity invariant remains unchanged.

## Additive SQLite schema v1

**Files:** `core/policy_execution_state.py`,
`core/policy_execution_store.py`, `tests/test_policy_execution_state.py`,
`tests/test_policy_execution_store.py`, this interface document, and
`docs/issue-100-implementation-report.md`.
**Store constructor:** `PolicyExecutionStateStore(db_path, *, store_identity)`;
both arguments are required. It does not read environment settings, a default
path, or a singleton. `store_identity` is bound durably to the database and a
mismatch fails closed. Path relocation is not inferred; an intentional
relocation must preserve and verify the original store identity using a
separately reviewed full database copy.
**Schema:** independent version 1 in `policy_state_schema_migrations`; do not
use the existing database's `PRAGMA user_version`.

| Table | Required columns, keys and constraints |
| --- | --- |
| `policy_state_database_identity` | Singleton row with `store_identity`, database namespace ID and schema binding digest. A second/different identity is rejected. |
| `policy_state_schema_migrations` | Version integer primary key, migration SHA-256, aware applied timestamp. |
| `policy_state_deployments` | Generation ID primary key, full immutable identity columns listed above, account environment, store identity, lifecycle, handler/guard identity and canonical `identity_json`; unique `(generation_id, account_environment_id, store_identity)`. |
| `policy_state_active_pointers` | `(account_environment_id, store_identity)` primary key, active generation and expected pointer version; composite FK requires matching account/store on target deployment. |
| `policy_state_deployment_events` | Append-only event ID, scoped account/store, old/new generation IDs, activate/rollback/degraded kind, readiness evidence reference, reason, expected/resulting pointer versions and aware event time. FKs use RESTRICT. |
| `policy_state_decisions` | Decision ID primary key; unique slot ID; deployment/account/store; exchange, decision session, category, subject type/ID, sequence; cutoff, next session, account valuation session/time; snapshot digest and canonical decision, policy, guard and effective-action JSON payloads. Unique `(generation, exchange, session, category, subject_type, subject_id, sequence)`. Composite deployment FK. |
| `policy_state_holdings` | Holding ID primary key; generation/account/store/security/broker symbol; opening decision/action; immutable initial quantity, remaining quantity, confirmed additions and add count; optional entry/average cost, realized P&L, committed risk/basis; proposed and broker-confirmed stop/action/ref/time; peak and valuation time; last accepted session; policy flags, pending IDs, action fill watermarks, completed additions, last tier and state version. Unique `(generation, security, opening_action_id)`. Composite FKs tie generation and opening decision/action together. |
| `policy_state_actions` | Logical action ID primary key; generation/account/store/decision/holding/security; role/side; originating snapshot; tier/fraction/frozen original quantity/rounded target/rounding rule; cumulative confirmed quantity/residual; reservation cash price/basis/stop; incremental risk per-unit/basis; state/version/resolution. A partial unique index on `(generation, holding, tier)` where role is `scale_out`; `CHECK` requires all tier fields and holding ID for scale-outs. Composite FKs require matching generation/security/account/store. |
| `policy_state_order_attempts` | `(action_id, attempt_number)` primary key; attempt 1 or 2; request and cumulative quantities/notional/fees; status and preserved terminal status; primary client/broker IDs; state version. FK uses RESTRICT. |
| `policy_state_order_reference_aliases` | Provider + account environment + store + reference kind + external ID primary key, action + attempt, first-seen time and source payload digest. An alias cannot identify another attempt within an action; a scoped external ID cannot be rebound to another action/attempt. |
| `policy_state_fill_receipts` | Provider + account + fill-event ID unique key; action/attempt, immutable payload digest, cumulative quantity/notional/fees, observation time. Same ID/same digest is idempotent; same ID/different digest conflicts. Composite FK to attempt. |
| `policy_state_holding_history` | Holding + monotonically increasing state version primary key; action/event identity, canonical complete state digest/payload, observed time. FK RESTRICT. |
| `policy_state_stop_updates` | Stop action ID, generation/account/store, holding/security/decision, requested stop, proposal/confirmation status, client/broker references, confirmed stop price, observation time and state version. Composite deployment, holding and decision FKs use RESTRICT. |
| `policy_state_portfolio_snapshots` | Snapshot ID, generation/account/store, completed and valuation sessions/time, source account snapshot identity, equity/cash/gross/open risk, peak equity, last accepted session, flags and digest. Composite generation/account/store FK; `(generation, source namespace, account snapshot ID)` is unique. |

All identity/history foreign keys use `ON DELETE RESTRICT`; no cascade may
erase evidence. Foreign keys are enabled on every connection before its
transaction. DDL is issued statement by statement, never through
`executescript`, inside `BEGIN IMMEDIATE`; one checksum-checked migration row
commits with all v1 tables/indexes. A failure after any intermediate DDL rolls
back the schema and leaves legacy workflow tables/data unchanged. Conflicting
version/checksum or database identity fails closed.

### Public API and transaction rules

- `migrate()` and `rollback_schema_v1()` handle additive schema only.
- `register_deployment_identity(identity, *, lifecycle, handler_identity, guard_id)`
  inserts immutable generation content idempotently or rejects a conflict.
- `record_decision(decision, *, policy_payload, guard_payload,
  effective_action_payload)` uses the canonical slot rules above.
- `record_action_intent(intent, *, expected_version)` persists immutable
  action identity and registers the action as pending on an existing holding.
- `record_cumulative_fill(action_id, attempt_number, *, provider_id,
  fill_event_id, cumulative_quantity, cumulative_notional, cumulative_fees,
  payload_sha256, observed_at, expected_action_version,
  expected_holding_version)` atomically records the receipt, attempt/action
  state, holding quantity/accounting update, and history. It creates the
  opening holding on its first confirmed entry fill; later fills increase its
  quantity without changing initial fill quantity or add count. A transaction
  failure rolls back every write.
- `record_holding_episode(holding, *, expected_version)` is the versioned
  holding state writer used by offline reconciliation.
- `request_order_cancel(...)`, `confirm_order_terminal(...)`,
  `create_single_remainder_attempt(...)`, `record_explicit_action_resolution(...)`,
  and `bind_attempt_order_refs(...)` enforce cancel/remainder/tier gates with
  compare-and-set action versions. Unissued intents can be resolved with a
  reason; issued attempts require terminal evidence.
- `update_holding_marks(...)`, `propose_stop_update(...)`, and
  `confirm_protective_stop(...)` are versioned holding writers. Each appends
  canonical state history in the same transaction. `record_portfolio_snapshot(...)`
  records immutable account facts.
- `load_action_projection(...)`, `load_holding_episode(...)`,
  `load_holding_episode_for_action(...)`, `load_deployment_chain(...)`,
  `load_active_generation(...)`, `load_order_reference_aliases(...)`,
  `load_stop_update_intent(...)`, and `load_portfolio_snapshot(...)` are
  individual read APIs. The combined consumer API below guarantees one SQLite
  read transaction for portfolio, action and holding facts.
- The exact combined read signature is:

  ```python
  PolicyExecutionStateStore.load_policy_execution_snapshot(
      *, deployment_generation_id: str, portfolio_snapshot_id: str
  ) -> PolicyExecutionReadSnapshot
  ```

  `deployment_generation_id` must own `portfolio_snapshot_id`; the result's
  `deployment_identity` and `portfolio_snapshot` describe that generation.
  `holding_episodes` include every holding for the same paper account and store
  across generations, retaining its original generation and returned
  `state_version`. `action_projections` include every open action for the
  account/store across generations, plus terminal actions linked to a returned
  holding through its holding ID, opening action, pending IDs, fill watermarks,
  or completed addition IDs. Unrelated terminal actions without a holding link
  are omitted from pending reconstruction. Each action retains its own original
  generation, decision clock, references and `state_version`. Thus a pointer
  switch does not hide prior-generation exposure or rewrite action ancestry.
- `set_active_generation(account_environment_id, *, expected_generation_id,
  new_generation_id, readiness_evidence_ref, outgoing_entries_reconciled)`
  atomically compare-and-sets the pointer, checks scoped identity and no
  uncertain outgoing entry, and records the event/evidence reference. This
  API is limited to synthetic/offline/prepared-state evidence in this issue;
  it does not establish #97 activation eligibility or operational readiness.
  Rolling A → B → A changes only the future-entry pointer; B holdings/actions
  stay B-pinned. Missing compatible B handler yields explicit degraded state;
  default non-flat migration is rejected/deferred.

Schema rollback v1 is allowed only if all dependent policy-state tables are
empty. No export-and-drop escape hatch is defined. This schema operation is
separate from generation pointer rollback. Tests compare canonical legacy
table schemas/rows before and after migration/rollback, not whole SQLite file
bytes. Every database test uses pytest's explicit temporary path; no configured
operational database is opened, copied, or modified.

## Store integration boundary

Existing execution store/workflow files and public entry points stay unchanged
until principal resolves any collision with D. The new store will be proven
against an explicit temporary database containing representative legacy
workflow schema and rows. #107 legacy workflow behavior and existing tables
remain unchanged. No live activation, broker/provider call, deployment, or
operational migration is part of this work.
