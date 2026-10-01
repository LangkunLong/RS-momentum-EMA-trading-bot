# Lead C paper-policy integration ledger

Goal active; not achieved. Lead chat `01a0f851-dfb4-7081-90c1-81131013fee2`, host local. Principal `01a0f613-6fa9-71d0-ada5-743cfa159a31` owns final independent acceptance and normal eligible merges. No completion notification sent.

## Source and authority

Clean starting HEAD `ab385d792e19ff6db39d87f1123f47f660fc1e1d`, tree `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`, verified 2026-10-01. Accepted #66/#80/#97 and final #71 semantics are integrated. Original launch packet and direct human authority receipt are under `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/coordination/`. The actual activation supersedes copied planning-only wording. Project verified `f88e96b1-dd01-4a5a-895b-b5494706a6e1`; existing registry and visible chats inspected, no #98/#99/#100 owners found before dispatch.

## Shared semantic contract, revision 1

- Decision clock is a completed exchange session with explicit as-of cutoff and next eligible execution session; retain timezone-aware observation/valuation times. Never use wall-clock now as implicit historical or restart identity. Reject future/incompatible facts and explicit missed/expired session actions rather than silently retargeting.
- #98 reuses historical feature and market-context builders with the same eligible universe, input facts, fiscal/public/availability interpretation and calculator identity. Do not fork #71 formulas or shift already normalized availability again. Detailed #66 missingness remains alongside optional `None`; required unavailable context must fail closed with a reason. No new policy wire fields are presumed.
- #99 declares account cash semantics: settled/current broker cash already reflects confirmed fills; reserve only residual executable buy quantity, once per logical action, merging local intention and matching broker order identity. Never subtract confirmed fills again or subtract the same reservation from already-net buying power. Unmatched/uncertain/conflicting facts remain explicit and block risk-increasing readiness.
- #100 owns durable deployment, decision, holding-episode, action and reservation identities. Stable logical action identity binds deployment, decision clock/snapshot, security/holding and action role/tier; broker/client IDs are separately retained mappings. Retries/restarts keep logical identity. Fill updates are cumulative, monotonic and idempotent, preserving pending residuals.
- State retains immutable initial quantity, current/remaining quantity, completed additions, stops, peaks, pending intentions and exit tiers. Freeze V3 decision-snapshot `original_qty` (including completed additions) for each tier intent; do not advance a tier merely on proposal/submission. Unknown stop/classification/account facts never become zero risk.
- #100 exposes a documented read-only state/reservation projection for #99 with clock, deployment/holding/action IDs, requested and confirmed quantities, reservation price/basis, status and order references. #99 can develop pure synthetic-record reconciliation against these semantics while #100 settles concrete names; only #100 edits schema/store/workflow. Lead resolves any shape discrepancy before integration.
- All databases are explicit temporary paths. No default store singleton, runtime launch, providers, broker, migration of actual store, scheduler activation, quota reset, #101–106 scope or policy promotion.

## Ownership and preflight

| Scope | Producer/consumer and permitted files | Finding/ruling |
| --- | --- | --- |
| #98 | New current-data adapter, narrow `core/stock_screening.py` feature boundary if needed, issue-specific tests/docs | Consume builders unchanged; request ownership before changing historical calculator/context contract. No scanner runtime rewiring. |
| #99 | New account snapshot adapter, narrow `adapter_v3.py` extension only if necessary, issue-specific tests/docs | Consume #100 projection; no execution store/workflow edits. Existing strict numeric V3 contract means incomplete accounts must yield explicit unready result, not fabricated fields. |
| #100 | New policy-state types/helpers, `core/execution_store.py`, necessary narrow `core/execution_workflow.py`, tests/docs | Sole schema/migration owner. Preserve legacy workflow behavior; scheduler/action execution wiring excluded. Coordinate D before overlapping edits. |
| #98/#99 | Same completed session and explicit valuation/snapshot identities | Combined test must reject mismatched clocks and retain missingness. |
| #99/#100 | Residual reservations, cumulative fills, logical action identity | Provider integrates before consumer; both implement independently against this agreed semantic contract. |
| #100/D | Store/workflow compatibility | D solely controls actual runtime; communicate interface and scope, no deployed migration. |

Ruling: use approved visible isolated implementation chats with Luna/xhigh and durable documents rather than the skill's ephemeral implementation agents/workspace cleanup; this follows the explicit launch charter. Independent reviewers remain supporting subagents. No acceptance criterion is waived by review iteration limits.

## Four statuses

| Issue | Implementation | Required inputs | Acceptance evidence | Dependencies |
| --- | --- | --- | --- | --- |
| #98 | Dispatch preparation | Recorded/synthetic non-account fixtures available; provider acceptance excluded | None for new implementation | Accepted #66/#80/#97 and final #71 eligible |
| #99 | Dispatch preparation | Synthetic account/order records available | None for new implementation | Accepted #80/#97; #100 projection agreed semantically, implementation due |
| #100 | Dispatch preparation | Fixed outputs and temporary databases available | None for new implementation | Accepted #80/#97 eligible; D collision coordination due |

## Remaining steps

Register all three permanent chat/worktree/base identities; implement in parallel; retain per-criterion source/input-bound receipts; independent full base-to-head spec/quality review for each; fix through owner; integrate #100 before #99; combined #98/#99/#100 pending/partial-fill/restart test and final independent review; publish eligible PR/evidence for principal inspection; await actual normal merges and accurate issue disposition; then send one achieved-goal report only.

## Dispatch register and current gate

All three missing visible issue chats were requested with `gpt-6-luna` / `xhigh`, isolated worktrees starting at exact `ab385d792e19ff6db39d87f1123f47f660fc1e1d`. Creation returned queued client IDs; permanent IDs below were resolved from actual session metadata and verified with the app status tool. No duplicate creation.

| Issue | Permanent chat | Worktree key | Creation client ID | State |
| --- | --- | --- | --- | --- |
| #98 | 01a0f85b-8d62-7251-a841-4013d3d84f82 | 855a | client-new-thread:1aeaf97c-7740-4af3-8e14-2cc4f17561dc | Active; owner verified exact base/tree; branch codex/issue-98-current-feature-context-adapters |
| #99 | 01a0f85f-4e04-7a70-87d7-fc5a0457aa71 | cc0f | client-new-thread:18afa54b-3f91-4243-b3bf-994436545c69 | Active; base ab385d7; local branch creation encountered shared Git metadata permission and owner is resolving |
| #100 | 01a0f861-76d9-7b11-9722-724643c52221 | 2286 | client-new-thread:99f96cc7-10c3-4b0c-91c3-21e74e1b7bf6 | Metadata resolved; exact base ab385d7; independent contract/new-module work dispatched, store/workflow editing held for D coordination |

Worktree root prefix: `C:/Users/llong/.codex/worktrees/`; each key contains `RS-momentum-EMA-trading-bot`. Briefs: lead worktree `.artifacts/lead-c/issue-{98,99,100}-brief.md`; each contains complete exact published issue body. Live GitHub connector reads confirm all three open with unchanged criteria. CLI network was unavailable; no mutation was attempted there.

Lead branch: `codex/next-phase-paper-policy-integration`; shared Git metadata branch creation completed through the approved escalation after initial sandbox denial. No source edits, publication or merges yet.

Automatic approval review rejected peer `send_message_to_thread` calls to A/D and later B/D, stating no trusted user authorization in this transcript. The original human journal was independently read: ordinal12/line13, role=user, timestamp2026-10-01T06:08:00.582Z, message msg_01a0f613-b046-7211-9164-0a40c3857197 explicitly activates the goal and named coordination. The locally parsed no-newline record hashes to ade00e44c90737ae312fb178e100b595ccf030261a7c4a71310c7c0c98c6335e; do not substitute this for the principal's raw-record hash8564f5f. A justified retry was rejected too. No alternative messaging route used. A direct asynchronous user authorization question is pending for A/B/D, issue owners and final principal reporting. D collision gate remains unsatisfied; do not release store/workflow edits until actual coordination occurs.

Incoming authorized peer information retained: A confirms no shared calculator/feature semantic changes; #72 owns sec_pit_fundamentals/build_pit_bundle, #69/#70 assessments read-only. B is `01a0f851-c7ac-7b81-87ab-3de389d47510`, worktree b367, owns trusted map/recipe and changed-source image verification; send exact final mapped-file/import changes before merging. Relevant mapped surfaces include adapter_v3, market_context, pit_feature_snapshot and providers. Incoming information does not itself authorize an outbound message; pending review limitation remains explicit.

Current four-status update for all three: Implementation active/in progress; Required inputs locally available offline; Acceptance evidence not yet delivered/reviewed; Dependencies accepted baseline eligible, with #99 concrete #100 projection and #100 D collision gate outstanding.

## Independent boundary preflight and rulings

Supporting reviewer `/root/boundary_review` retained `.artifacts/lead-c/boundary-preflight-review.md`: specification changes requested; preparation quality suitable, final acceptance checklist incomplete. No source implementation reviewed or tests executed. All seven findings accepted for final review. These rulings clarify the accepted contracts; they are not evidence the owners received a follow-up. Messaging remains blocked; no file-based instruction workaround is used.

1. Committed incremental risk is reserved once alongside committed cash for accepted nonterminal entry/add-on/replacement intentions; partial fills convert the corresponding commitment to actual position risk/notional. Expose separate residual cash/risk basis in the projection. Preserve V3 cash + gross = equity; available allocation budget is separate. Missing pending risk basis blocks readiness.
2. Decision identity links immutable originating session/snapshot, while logical exit-tier uniqueness is generation + holding episode + tier across sessions. A later snapshot cannot create a duplicate still-pending tier; broker/remainder attempts remain separate references under the same logical tier intent.
3. Adopt #97's conservative one-remainder-attempt state rule for offline fixtures. Persist requested fraction, frozen decision-snapshot original quantity, rounded cumulative target, aggregate confirmed fills, residual, per-attempt IDs. Cancel request/timeout is not terminal. Terminal underfill permits one bounded remainder; another underfill becomes partial_incomplete until explicit reconciled resolution with reason. No tier advance or conflicting close while uncertain.
4. Deployment rollback and database rollback are separate acceptance cases. A → B → A pointer restoration affects future entries only; B holdings/pending state remain B-owned. Uncertain outgoing cancellation blocks switch; missing compatible handler yields explicit degraded/emergency state. Default non-flat schema migration is rejected/deferred; failed migration must preserve recoverable old state.
5. Durable immutable record chain must retain cost basis, realized P&L, add count/risk, proposed versus broker-confirmed stops/protective-order identity, flags, last session, portfolio peak/account facts, policy/capability/interface/feature/calculator/source/runtime/account/store identities. Compare all across restart and two episodes for one security.
6. Explicit scanner boundary case must show a legacy-filtered eligible member reaches adapter or receives an explicit authorized exclusion. Missing member data cannot shrink expected universe denominator silently.
7. Feature cutoff and execution-time account valuation are distinct domains. Valid next-opportunity account valuation can follow feature cutoff; enforce declared compatibility/freshness rules without inventing thresholds. Separate raw public-date strict-next-session (holiday/intraday) and already-normalized availability tests.

Latest verified owner state: #99 branch `codex/issue-99-account-reconciliation` exists; #100 exact HEAD/tree ab385d7/46a4b97 confirmed and active. All owner cursors are revision2: #98 `b7a94023-2f49-45f0-8549-26e0a6376b28:2`, #99 `c1d20203-52e0-4d1d-98f9-2b809b0c8229:2`, #100 `c32cb4c8-dc72-4583-b997-638c39564d10:2`. #99's attempt to request concrete #100 projection via messaging failed; requirement captured by read-only inspection. Do not mark shape integration complete.
