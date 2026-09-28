# Issue #97 — Qualified policy deployment and compatibility contract

**Contract:** v1, accepted as a definition on 2026-09-28
**Source revision checked:** `c628a3af3c2c14dd684340d1b695ce91f8438842`
**Branch:** `codex/issue-97-qualified-policy-contract`
**Review state:** source checked; #80 confirmed canonical identity names and core defaults; #66/#80 missingness projection alignment is closed after #97 adopted the corrected rows. #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375` also records the V5 decision-snapshot `trade.qty` scale-out basis, including completed add-ons, frozen per tier intent as the paper-parity definition. Partial-fill/tier progression and lifecycle remain future adapter behavior; no current paper support is claimed. #107 runtime identity wording aligns with this proposal.

## Purpose and boundary

This contract says when a research policy is an identifiable, compatible paper artifact, which of its decisions the current paper path can execute, what the host must guard, and how activation, rollback, open holdings, and pending orders behave. The fixed baseline is enough to develop adapters. No winning candidate is needed for this contract or for fixed-policy integration. This document selects no production policy and authorizes no activation.

The proposal uses the published #97 criteria, the #80 shared strategy/evaluation proposal, the reviewed #66 feature specification, and targeted code checks at the source revision above. It defines requirements for later adapters; it does not claim those adapters, current provider access, or runtime readiness are complete.

## Current state and exact gap

The research system already identifies V5 policy revisions by SHA-256 and restricts candidate policy edits to four V3 modules and six exports. V3 contracts describe immutable snapshots and decisions. The paper runner (`auto_trader.py`) still uses its CANSLIM scanner/ranking, configured capacity and sizing, held-symbol exclusion, and fixed hard-stop/EMA full-exit behavior. The order manager and execution store/workflows submit and reconcile those existing entry/exit flows, record order ancestry, handle fills/failures, and maintain protective stops.

Those pieces are not a qualified-policy deployment adapter. The paper entry path does not generally load V3 entry, capacity, allocation, replacement, add-on, or exit decisions. Existing workflow/active-position identity is not a deployment generation with full policy state. In particular, paper execution does not provide policy-selected replacement, add-on, arbitrary partial exit, or complete evolving-stop semantics. A functioning fixed order lifecycle does not establish V3 compatibility.

The gap is a shared definition of the selected policy artifact and its feature/action needs; separation of policy intent from host constraints; and durable identity and transition rules for decisions, orders, and holdings through activation, rollback, and migration.

## 1. Identity and immutable selection

### 1.1 Policy artifact

Select exactly one immutable policy artifact and one immutable capability manifest for each deployment generation. `policy_artifact_id` identifies the exact policy revision. For a V5 candidate, reuse `PolicyRevisionIdentityV5.sha256` and its authenticated interface version, trusted policy runtime digest, immutable constraints digest, and exact four editable-source hashes. For a fixed integration policy, compute the equivalent digest over the canonical policy source paths/bytes, interface version, trusted policy runtime identity, and immutable constraints. `capability_manifest_id` separately hashes the declared feature/action capability manifest. A mutable branch, filename, label, or “latest” pointer is only a locator and never the identity. Verify both digests before loading and retain the bytes/manifest for the life of every decision or holding that uses them.

For an optimizer V5 candidate, reuse and record its authenticated policy revision digest and editable-source hashes; do not invent a second source identity for the same revision. For a fixed integration policy, compute the equivalent canonical source-revision digest. A fixed policy used for integration is not thereby a qualified candidate. Changing either the artifact or its capability manifest creates a new deployment generation.

Keep these dimensions separate:

| Identity/dimension | Meaning |
| --- | --- |
| `policy_artifact_id` | Exact policy revision/source artifact under its interface, trusted policy runtime, and immutable-constraint identities. |
| `capability_manifest_id` | Exact required/optional/unsupported feature and action declarations for that deployment. |
| `policy_interface_version` | Typed policy input/output contract; the six-category adaptive surface is V3. |
| `feature_contract_id` | Names and meanings of policy-visible fields, including units, timing, lookbacks, missingness, and state scope. |
| `feature_calculator_identity` | Exact implementation/revision that creates values for that feature contract. |
| `historical_data_format_version` | Historical bundle schema (2 or 3); independent of optimizer, policy, feature, and runtime versions. |
| `execution_profile_id` and cost assumptions | Fill/timing/order and cost assumptions under which research results were evaluated. |
| `source_revision` | Exact application/repository revision. It is provenance, not a substitute for policy source digest. |
| `runtime_identity` | Canonical executable environment identity reported by the runtime checks, not a policy version. |

An accepted qualified-candidate promotion must reference the exact accepted confirmation, qualification, and full-replay evidence for the same `policy_artifact_id`. Issue closure, a discovery winner, or an artifact with a digest alone is insufficient.

### 1.2 Shared research/result identity

For research comparison and portable evidence, use the canonical #80 names unchanged:

- `source_revision`
- `runtime_identity`
- `evaluator_image_digest`
- `evaluation_mode`
- `input_bundle_id`
- `evidence_root_id`

Bind those to the policy and feature identities above, `historical_data_format_version`, `execution_profile_id`, and the explicit cost assumptions. Do not combine optimizer version 5, policy interface version 3, data format 2/3, feature calculation, evaluator, execution profile, and costs into a single version number. The six canonical names identify research/result context; they do not replace `policy_artifact_id` or `feature_contract_id`.

### 1.3 Deployment generation and records

Each prepared or active deployment is an immutable `deployment_generation_id` that references `policy_artifact_id`, `capability_manifest_id`, `source_revision`, `runtime_identity`, feature contract/calculator, selected guard profile, and (for a qualified candidate) its research evidence identity. The V5 `trusted_policy_runtime_sha256` authenticates the constrained policy execution component; `runtime_identity` identifies the actual application/runtime environment checked by #107. Keep them separate. Also record a non-secret `paper_account_environment_id` and persistent `store_identity`; never put credentials in identity material.

Every policy decision, execution intention, order workflow, pending action, and holding episode records at least `deployment_generation_id`, `policy_artifact_id`, `capability_manifest_id`, interface version, feature contract/calculator identity, `source_revision`, and `runtime_identity`. Decision evidence includes the policy-visible snapshot digest, decision session/as-of boundary, raw policy output, guard outcome, effective broker intention, and final/reconciled fill state. A replay can then distinguish policy output from host action and broker outcome.

## 2. Feature compatibility and decision timing

### 2.1 Capability declaration

The artifact manifest declares, by exact V3 field/type name, whether each supported input is `required`, `optional`, or unused by that policy. It also declares the expected unit/schema and required state snapshots for the six exports. Unknown fields/exports and unsupported interface versions are rejected. The host does not infer requirements from a policy filename or silently fill undeclared data.

Before activation, the feature adapter must prove it can produce every required field with the same meaning as the feature contract. If any required field is unavailable, stale under its declared rule, unsupported for the data source, or semantically incompatible, activation is rejected. For an optional field, preserve `None` when allowed and preserve source/calculation provenance and missingness reason. Never replace missing values with zero, a neutral score, today's profile, an alternate provider field, or a different lookback without a new feature contract and policy identity.

The current typed V3 feature surface is concrete: `EntrySnapshotV3` wraps `EntryFeaturesV3` (`affiliations`, `industry_group_rs`, `sector_rs`, `earnings_growth_acceleration`, `sales_growth_acceleration`, `fundamental_age_days`, `atr_20_fraction`, `breakout_gap_fraction`, `average_dollar_volume_50`, `distance_from_52_week_high_fraction`); holding snapshots use `HoldingFeaturesV3` (`current_rs_score`, `industry_group_rs`, `atr_20_fraction`, `volume_ratio`); portfolio context includes gross exposure, drawdown, open risk, pending-entry count, and sector/industry exposures. The policy also receives the corresponding typed base snapshots. These field names are not promises that every value is currently available: V3 sets `sector_rs=None` because dated sector taxonomy is absent, and ownership quantity, sponsor quality, and product/management event facts are not current V3 features. The base input's `institutional_data_available` boolean is a coarse presence flag, not ownership quantity, a dated denominator, or sponsor quality. Numeric `None` carries no #66 cause by itself; feature/coverage provenance must retain the detailed status. A policy that requires any unsupported/unavailable field is incompatible until an adapter with the correct contract supplies it. An optional V3 sector field may remain unknown only if that policy explicitly accepts `None`.

For #66 compatibility, retain its detailed source/coverage states: `not_yet_public`, `absent`, `unsupported_scope`, `insufficient_history`, `stale_by_declared_rule`, `invalid`, and `observed`. A policy boundary may additionally expose a concise `#80` category, but the adapter must retain the original #66 state and map it losslessly enough to recover its reason:

| #66 source/coverage state | Proposed concise policy-boundary state | Required detail retained |
| --- | --- | --- |
| `not_yet_public` | `not_yet_public` | Source-public date and calculated first-eligible session. |
| `absent` | `unavailable` | Source and observation scope searched. |
| `unsupported_scope` | `unavailable` | Retain reason `unsupported_scope`; use `not_applicable` only under an explicit versioned applicability rule that excludes this entity/decision and says the field is not required. |
| `insufficient_history` | `unavailable` | Required versus observed lookback. |
| `stale_by_declared_rule` | `stale` | Declared freshness rule and age. |
| `invalid` source fact or provenance | `unavailable` | Retain reason `invalid` and the validation failure; never coerce it to missing zero. |
| `observed` | `present` | Value, units, fiscal period, revision, provenance, and as-of date. |

Reserve `calculation_failure` for a transform that fails after valid source inputs and provenance have been established. Use `not_applicable` only when a versioned feature contract has an explicit rule for the entity/decision and states the field is not required; otherwise `unsupported_scope` projects to `unavailable`. A required feature with any non-`present` state blocks activation unless an explicit applicability rule excludes the case. Optional `None` remains allowed only when the selected policy accepts it, with detailed reason and provenance retained. The concise label is a lossy derived convenience; it must stay linked to the original #66 state/reason and must not replace it or become a new V3 wire field. #66 supplied these corrections; #80 confirmed the revised rows and closed the mapping review in commit `e951f676a4639a6e87c8a27c2caffc64a5913375`.

### 2.2 Timing

Use the #66 rule exactly: keep `source_public_at/date` separate from `available_from_session`; a fact is first eligible on the first eligible exchange session **strictly after** its source-public date, including when an intraday timestamp exists. If a bundle already stores normalized `available_from_session`, or a legacy `public_date` that is documented as the first usable session, do not shift it a second time. Revisions become available on their own public dates and do not rewrite prior snapshots.

The shared policy clock is a completed-session snapshot followed by action at the next eligible execution opportunity, subject to the declared execution profile. Bind decisions to the session, input cutoff, and intended effective opportunity. Missed sessions and duplicate/restarted cycles must be resolved by session-bound idempotency, not by replaying an old decision as a new one.

Do not run an additional hourly strategy exit rule on a V3 policy generation unless that behavior is part of its declared policy/execution contract. A continuous broker protective stop is a host execution guard with its own identity and evidence; it is not an extra policy decision. Any emergency host liquidation rule must be separately declared, observable, and applied consistently to the position.

## 3. Six decision categories and current paper support

Status below describes the paper source checked at `c628a3a`. “Partial/fixed” describes only the current legacy behavior. No general adaptive V3 category is fully paper-supported by this check.

| Category and V3 responsibility | Strategy decision meaning | Current paper support | Required behavior for policy deployment |
| --- | --- | --- | --- |
| **Entry and ranking** (`evaluate_entry`) | Evaluate a candidate from the declared entry/market/features and return eligibility, score/rank meaning, and reason. | **Partial: legacy scanner only.** Scanner prefilters/ranks candidates; policy isn't generally loaded. Arbitrary V3 entry decisions are unsupported. | Build equivalent policy snapshots; evaluate all eligible universe members before any declared host guard; retain rank and reason; do not let a legacy scanner silently filter policy candidates. |
| **Capacity** (`recommend_capacity`) | Recommend desired maximum positions/replacement permission within policy scope. | **Fixed configuration only.** Slot and per-cycle ceilings are used; policy recommendation isn't connected. | Pass reconciled portfolio and pending commitments; apply host absolute caps. A rejected/capped recommendation records requested and effective values. |
| **Allocation** (`recommend_allocation`) | Recommend risk fraction, stop distance, optional notional fraction cap for a candidate. | **Fixed sizing only.** Configured position percentage and stop determine plans; adaptive allocation is unsupported. | Size using policy output and reconciled equity/cash/open risk; enforce account/risk/exposure and precision constraints. Persist the requested stop through fills and recovery. |
| **Replacement** (`select_eviction`) | Choose which held slot, if any, policy would give up for a candidate. | **Unsupported as a policy transaction.** Whole-position sell and failure recovery exist, but no durable policy-selected sell-then-buy replacement orchestration. | Bind selection to a holding episode; persist intent; submit/reconcile sell; revalidate candidate, capacity, cash, and risk before any buy; do not buy while sell status is uncertain. |
| **Additions** (`evaluate_add_on`) | Add or decline, and size a permitted addition against current holding/risk state. | **Unsupported as execution.** Entry path skips already-held symbols. V3 baseline intentionally declines additions; this is intentional baseline policy scope, not absence of the V3 interface. | Track original and remaining quantity/cost, add count, risk budget, and pending add intent; dedupe across restart; resize protective coverage only from confirmed fills. |
| **Exits and holding management** (`evaluate_exit`) | Close or scale out by fraction of original quantity, propose next protective stop and next policy state. | **Partial: fixed full exits only.** Hard-stop/EMA exits and order lifecycle work for the current baseline. General policy scale-outs, evolving stops, and persistent exit state are unsupported. | Translate scale fraction using original quantity, reconcile actual partial fills and broker precision, advance durable tier/state according to agreed fill semantics, and protect the remaining quantity. |

Policy outputs are intent, not broker orders. Replacement, additions, and exits also need state transitions and idempotent action identity. The current V3 add-on baseline returning `add=False` is a deliberate policy choice; the paper add-on adapter remains missing. Do not describe that baseline output as proof the adapter supports additions.

## 4. Strategy authority and host execution guards

The policy chooses among valid strategy alternatives: candidate eligibility/rank, preferred capacity within declared bounds, risk/stop/notional preference, a replacement target, whether/how much to add, and close/scale-out/stop-state decisions. It does not set the facts, account arithmetic, simulator assumptions, execution machinery, or safety ceilings.

The host owns and enforces: paper account/mode; current reconciled broker and store state; feature/data validity and freshness; price/quantity increments and order validity; buying power and cash; configured maximum positions, risk, and exposure; supported order types/session rules; duplicate prevention and idempotency; safe behavior for rejected/uncertain submissions and partial fills; and adequate broker protection.

Use these default responses:

1. Unknown interface, invalid/malformed output, unsupported action, inconsistent state, or missing required data: reject the output and fail closed for new risk-increasing actions. Record a reason. Do not fall back silently to the legacy scanner or another policy.
2. A valid policy request that violates a hard account/host ceiling: veto the action with a stable guard reason. Do not silently alter policy semantics. If a specific cap is permitted by the declared guard profile, record requested value, effective capped value, cap, and reason as separate fields.
3. Mechanical broker tick/lot rounding: apply only the declared deterministic precision rule and record requested versus executable values. If rounding violates a policy minimum or changes the action beyond the declared tolerance, reject it.
4. Keep protective broker orders active during policy/runtime failure. An unresolved broker/local state blocks conflicting actions until reconciliation; do not treat a timeout as rejection or cancellation.

The same guard profile and response semantics are recorded for historical/paper comparison where applicable. Host guards may veto an action but must never create a different discretionary buy, replacement, add, or exit and attribute it to the policy.

## 5. Activation, holdings, pending orders, rollback, and migration

### 5.1 State lifecycle and activation barrier

Represent each generation with immutable states `prepared`, `eligible`, `active`, `draining`, and terminal `retired` or `rejected`. Activation validates the artifact digest, interface and exports, manifest, required feature availability/semantics, compatible state handler, selected guard profile, `#107` runtime/account/store readiness, and a no-order decision/dry-run record. Runtime/account connectivity alone is insufficient, and this contract does not claim #107 readiness.

Before switching the active generation:

- Reconcile local workflows with broker positions and orders; unknown state blocks activation.
- Prove that every non-flat generation-pinned holding can still be managed by its original compatible policy/runtime handler, or name a reviewed emergency safety mode that preserves existing broker protection.
- Cancel and confirm terminal status for unfilled entry orders from the outgoing generation. For a partially filled entry, reconcile the filled shares into a holding episode owned by the outgoing generation and confirm cancellation of the unfilled remainder. Block the switch on uncertain order state; no timeout is assumed to be a cancellation.
- Bind retained pending sell/protection actions to their holding's generation; reconcile them before any conflicting new action.
- Commit the generation pointer atomically only after these checks. The new pointer governs new entries; it does not rewrite current holding ownership.

### 5.2 Conservative default: pin each holding to its opening generation

An opening fill creates a holding episode bound to that `deployment_generation_id`, `policy_artifact_id`, runtime/feature identity, and the initial policy state. Until the holding is flat, its exit/stop/add-on decisions continue under that generation and its compatible state handler. A new generation receives only new-entry decisions. This allows an old and new generation to coexist temporarily; the extra state and retained old code are the cost of clear behavior and reversible rollout.

If the old policy/runtime cannot be loaded or its required current feature contract cannot be produced, do not silently run the new policy against the old holding. Keep confirmed broker protective orders in place, block conflicting risk actions, mark the holding as requiring the named emergency/manual host mode, and surface that as degraded readiness. Activation of the new generation requires that this failure mode be explicitly accepted and operationally supported.

### 5.3 Pending-action rules

Every policy action has an idempotent action ID derived from deployment generation, holding episode, session, category, tier/sequence, and candidate where applicable. A scale-out has one durable logical intent keyed by generation + holding episode + tier, with separately identified submission attempts. The durable state machine distinguishes at least `planned`, `submitted`, `partially_filled`, `filled`, `cancel_requested`, `cancelled`, `rejected`, `expired`, `partial_incomplete`, and `unresolved`; only reconciled broker facts move a submitted action to a terminal state. Clear `partial_incomplete` only after an explicit resolution is recorded against reconciled broker position/order facts; never silently skip or replay the tier.

- Accepted pending entry orders reserve their committed cash and worst-case risk once for policy snapshots. On fill, reconcile the reservation into actual holding facts without counting it twice. Release it only after confirmed cancel/reject/expiry. The #80 owner confirmed this rule aligns with its conservative proposal; #99 must still implement and evidence reservation/fill reconciliation.
- A policy replacement is sell-first. Do not submit the replacement buy until the sell is terminal/reconciled and new candidate, buying power, capacity, and risk checks pass. A partial sell keeps the residual holding with its original generation.
- A pending add or scale-out survives an active-pointer switch and remains tied to its holding generation. If cancellation is appropriate, confirm it at broker and local state before issuing a conflicting action.
- Protective-stop amendment/cancel recovery must preserve or restore safe coverage. Policy stop changes are proposed intent; broker-confirmed stop state is separately recorded.

For exits, V3 validation sets `scale_out_tier` in the *decision* to the snapshot tier plus the number of scale-out actions proposed. This proposed next tier is not proof that any shares filled. Recommended conservative default for the future adapter: persist each scale-out before submission with its generation, holding episode, tier, exact `fraction_of_original_quantity`, original filled shares, target cumulative sell quantity (`original filled shares × requested fraction`, rounded only by the declared broker-lot rule), cumulative confirmed fills, and residual target. While the order is nonterminal, keep that tier pending, preserve protection, and block conflicting strategy actions for the holding. After confirmed terminal underfill, reconcile broker order and position facts before submitting only `target quantity − confirmed cumulative fills` as a remainder attempt under the same logical action ID. Bound automatic remainder retries to one; if still short, mark `partial_incomplete` and require an explicit resolution with a recorded reason before advancing the durable holding tier. Advance the durable tier after the target is fully filled or explicitly resolved; never at proposal or submission alone. A close may supersede only after related scale-out orders are terminal and the position is reconciled. Never count requested shares as filled or change the fraction-of-original-quantity meaning to compensate for partial fills. This may delay later policy actions or require review after a shortfall, while preventing duplicate sales and preserving auditable intent. This support is not present in current paper code. #80 keeps this as a separate proposed adapter rule; its open fill/tier decision does not block acceptance of the #97 definition.

**Quantity-basis review point:** At the checked V5 source revision `c628a3a`, `ExitSnapshot.original_qty` is populated from `trade.qty`; completed add-on fills update `trade.qty` and weighted entry; immediate and queued policy scale-outs calculate quantity from `trade.qty × fraction_of_original_quantity`. Thus the current V5 denominator is the decision-snapshot quantity, including add-on fills reflected at that decision, and is not necessarily the immutable first-entry quantity. For a paper adapter seeking V5 parity, use that decision's `ExitSnapshot.original_qty`, captured when the tier intent is created and frozen through submission, reconciliation, and any remainder attempt. The preceding phrase “original filled shares” is not intended to settle this denominator as first-entry quantity. #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375` records this as the shared paper-parity definition. The fill/tier transition remains a separate future adapter question.

### 5.4 Rollback example and rules

Suppose generation A is active, generation B is activated, and B opens holding H. On a B incident: first stop B from creating new entries; request cancellation of B's unfilled entry orders and wait for broker-confirmed terminal states; restore A as the active generation for future entries; keep H and its pending exit/stop actions attached to B and manage them with B's compatible handler until flat. If that handler is unsafe/unavailable, preserve the broker stop and enter the named emergency/manual mode; do not reinterpret H under A automatically. Reconcile all broker/store state before resuming risk-increasing actions.

This rollback costs temporary mixed generations. A single global switch is operationally simpler, but changes open positions' policy meaning and can orphan pending state, so it is not the default.

### 5.5 Migration rules

Default migration rule: let holdings become flat under their opening generation, then retire that generation. An explicit non-flat migration is an optional later capability, never an implicit side effect of activation or rollback. It requires: no unresolved/pending actions; reconciled broker/store state; a versioned state mapping for original/remaining quantity, cost basis, add count, risk, current protective stop, completed exit tiers and persistent policy flags; a compatibility check against the receiving policy's semantics; dry-run decisions under both old and receiving generations; named owner approval in deployment evidence; and a recovery plan that preserves broker protection and records the one-way point after new-generation decisions/fills. If any field cannot be mapped without changing policy meaning, defer migration until flat.

## 6. Alignment with #66, #80, and #107

- **#66 owns historical feature truth.** This contract reuses its field definitions, source-publication timing, units, periods/lookbacks, missingness detail, and coverage meanings. It does not add data or reinterpret the policy-facing value. The exact timing correction from the earlier #97 brief is recorded in §2.2.
- **#80 owns shared policy input/decision semantics.** This contract uses the same six category names and preserves `fraction_of_original_quantity` for scale-outs. Pending-reservation accounting, fill-based tier advancement, guard response vocabulary, and decision clock require joint semantic sign-off before adapters treat them as shared rules.
- **#107 owns runtime truth.** `runtime_identity`, `source_revision`, account/environment and store identify where and how a policy ran. Ready checks must state which runtime/data/account checks actually passed; they do not imply the policy is qualified or compatible.
- **#98–105 own adapters/state; #106 owns historical-to-paper intent comparison; #109 owns actual qualified-policy promotion and supervised operation.** Those issues consume the accepted contract. They do not block drafting this definition, and their completion cannot be claimed here.

### Shared missingness mapping: corrected and cross-review closed

The #66 reviewed specification retains detailed source/coverage states (`not_yet_public`, `absent`, `unsupported_scope`, `insufficient_history`, `stale_by_declared_rule`, `invalid`, `observed`). The policy boundary may use concise categories (`not_yet_public`, `unavailable`, `stale`, `not_applicable`, `calculation_failure`, `present`), but must retain the detailed #66 state and provenance. Following the #66 historical-owner review, `unsupported_scope` maps to `unavailable` by default; `not_applicable` requires an explicit versioned applicability rule for the entity/decision that says the field is not required. Invalid source facts/provenance map to `unavailable` with reason `invalid`; `calculation_failure` is reserved for a transform failing on valid source inputs. `absent` and `insufficient_history` both map to `unavailable`, so the concise label alone cannot recover the cause. This projection does not amend #66's feature decisions. #97 and #80 now record the corrected rows; the cross-owner mapping review is closed, as recorded in #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375`.

## 7. Acceptance evidence and review record

Accept #97's definition when a reviewer can verify at a named revision that:

1. The contract defines immutable policy artifact and deployment/runtime identities, keeps the six canonical #80 result identity fields and version dimensions separate, and binds identity to every decision/order/holding episode.
2. The capability manifest declares exact required/optional/unsupported inputs and exports; #66 timing and missingness semantics are preserved; required unavailable data and unsupported outputs fail closed.
3. All six categories have explicit current support status, V3 meaning, intentional baseline behavior, and downstream adapter requirements.
4. Policy authority, host invariants, veto/cap/rounding behavior, and diagnostic records are explicit.
5. Activation, holding-generation ownership, pending-order reconciliation, rollback, emergency handling, and optional migration have concrete state transitions and examples.
6. #66/#80 owners and #107/runtime owner have reviewed the overlapping definitions, with all disagreements resolved or explicitly retained as open; independent review maps each published #97 acceptance criterion to a section and records any remaining acceptance evidence.

No model/broker call, winning policy, real candidate selection, or activation is needed to accept this contract. Actual feature availability, runtime readiness, adapter lifecycle evidence and qualified-policy promotion remain for their owning issues.

### Published #97 acceptance-criteria crosswalk

Independent review against the published Trading-01 criteria at the named contract revision (`c1eadc7ab866bd349e86cebfbea8339162d07341`), checked against #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375` and #66 §6.1:

| Published criterion | Contract evidence | Review |
| --- | --- | --- |
| All six policy action categories have explicit support/unsupported status. | §3, “Action-category support matrix,” gives the source-checked paper status and required adapter behavior for entry/ranking, capacity, allocation, replacement, additions, and exits/holding management. | Satisfied for the definition. Legacy/fixed behavior is labeled partial or fixed; unsupported policy adapters are not presented as available. |
| Host execution guards are distinguished from strategy decisions. | §4 assigns strategy preferences to the policy and account/data/execution invariants to the host; it specifies fail-closed behavior, veto/cap/rounding records, and preservation of protective orders. | Satisfied. Host constraints cannot silently become discretionary policy choices. |
| Policy selection and rollback address open holdings/pending orders; definitions align with Research-01 without requiring a winner. | §§1.1 and 1.3 bind the immutable policy artifact to a deployment generation; §§5.1–5.5 define activation, generation-pinned holdings, pending-order reconciliation, rollback, emergency handling, and migration; §6 records #80 identity, timing, and corrected feature/missingness alignment. The opening scope statement in §7 says no winning candidate is needed. §5.3 records the V5-compatible decision-snapshot denominator frozen per tier intent. | Satisfied at the definition layer. No winner is selected or activation authorized; partial-fill/tier lifecycle remains future adapter behavior. |

All three published #97 criteria are satisfied by the definition and cross-contract evidence reviewed here. The #66 status projection and scale-out denominator are aligned with #80; the partial-fill/tier retry transition remains future adapter behavior and does not block this definition. This review does not change the saved issue fields or claim runtime readiness, paper-adapter completion, or qualified-policy promotion.

**Review performed in this worktree:** targeted source check at `c628a3a`; comparison against the published #97/#80/#66 bodies; comparison against the reviewed #66 feature specification and #80 shared contract at commit `e951f676a4639a6e87c8a27c2caffc64a5913375`. The earlier #97 timing sentence was broader than #66's rule; this contract adopts #66's unconditional next-eligible-session rule and no-double-shift rule. #97 adopts #66's corrected status projection and #80 records the cross-review closed. The V5 decision-snapshot scale-out basis, including completed add-ons and frozen per tier intent, is confirmed in #80; partial-fill/tier lifecycle remains adapter work. Current V3 representation limits remain an integration gap, not a feature-definition disagreement.

The #80 owner confirmed the canonical six identity names and said the reservation, completed-session timing, fraction-of-original-quantity action vocabulary, observable guard, and generation-pinned holding defaults align with the #80 proposal. The #66 historical owner identified corrections to the missingness projection; #97 adopted them, and #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375` records the cross-review closed. The same #80 commit defines the V5 decision-snapshot `trade.qty` denominator, including completed add-ons reflected then, frozen per scale-out tier intent; this review confirms it matches #97 §5.3. The partial-fill/tier transition remains future adapter behavior. The #66 strict-next-session rule remains documented; final #66 criteria-to-evidence review and issue acceptance are separate. The #107 runtime owner describes `source_revision` and `runtime_identity` separately and binds runtime identity to checkout/interpreter, dependency and configuration profiles, persistent store, and paper endpoint/account fingerprint, matching this proposal; final acceptance by #107 is not claimed.

## 8. Four separate issue statuses

| Dimension | Published/saved status | Current work result |
| --- | --- | --- |
| **Implementation** | **Not assessed** | Definition artifact completed and accepted. No policy loader, state store, action adapter, runtime change, or deployment implementation was made or accepted. |
| **Required inputs** | **Not assessed** | Existing V3 contracts and #66/#80 definition material were available for this design. Current provider/account/runtime readiness remains unverified and is not an input to accept this definition. |
| **Acceptance evidence** | **Not assessed** | An independent review mapped all three published criteria and found the definition and #66/#80 cross-contract points aligned at #80 commit `e951f676a4639a6e87c8a27c2caffc64a5913375`. Saved status remains unchanged as requested. Paper-adapter lifecycle, runtime readiness and policy promotion are not claimed; fill/tier execution remains downstream work. |
| **Dependencies** | **Ready to start** | #97 has no registered start prerequisite. Shared-contract alignment is acceptance coordination; downstream adapter issues retain their own prerequisites and inputs. |

Do not collapse these fields into one completion status. A known missing adapter, an unavailable provider field, unverified runtime, an intentional fixed-baseline choice, and acceptance of this definition are different states.

## Source references checked

- `core/pit_optimizer_v5/policy_scope.py` — four editable V3 modules and required policy exports.
- `core/strategy_policy/contracts.py`, `contracts_v3.py`, `adapter_v3.py`, and `core/strategy_policy/v3/*.py` — typed actions, validation, and fixed V3 baseline behavior.
- `core/pit_optimizer_v5/candidate_ir.py` and `evaluator.py` — authenticated policy/runtime/source identity use in research.
- `auto_trader.py`, `core/order_manager.py`, `core/execution_workflow.py`, and `core/execution_store.py` — present paper scan/exit/order/fill/store paths.
- Handoff `gap-registers.md` and published `Trading-01.md`, `Research-01.md`, `Historical-01.md`, and `Trading-11.md`.
- Reviewed cross-issue drafts: #66 `feature-specification.md`; #80 `docs/strategy-policy-contract-v1.md` and its canonical identity naming note.
