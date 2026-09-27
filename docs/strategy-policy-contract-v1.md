# Shared strategy input, decision, and experiment contract

- **Document contract version:** 1
- **Applies to:** optimizer version 5; strategy policy input/decision interface version 3; historical point-in-time data bundle formats 2 and 3
- **Source revision inspected:** `c628a3af3c2c14dd684340d1b695ce91f8438842`
- **Scope:** Documentation contract only. This version does not change policy schemas, engine behavior, source data, qualification rules, or paper execution.

This contract brings the reviewed #66 historical feature specification together with the existing V3 policy surface and the future paper boundary described by #97. It uses three labels:

- **Current:** directly represented or enforced by source at the inspected revision.
- **#66 settled:** a historical input rule from the independently reviewed #66 specification and incorporated owner decisions.
- **Proposed:** a shared cross-runtime rule recommended here but not yet implemented or accepted by all owners.

A current code path is not evidence that the complete contract has been accepted. Where source and proposals differ, both are named.

## 1. Version and identity boundaries

These versions identify different things. They must remain separate in reports, candidate artifacts, and deployment records.

| Dimension | Contract value | Identifies | Current source evidence |
|---|---:|---|---|
| Document contract version | `1` | This written semantic agreement. A later incompatible documentation agreement gets a new document version. | This file only; not a wire/schema change. |
| Optimizer version | `5` | The research coordinator and its V5 workflow/authoring machinery. | `core/pit_optimizer_v5/`; `POLICY_INTERFACE_VERSION_V5` aliases policy interface V3. |
| Policy interface version | `3` | The inputs and outputs available to the strategy policy. | `core/strategy_policy/__init__.py`; V5 `PolicyRevisionIdentityV5` requires literal interface version 3. |
| Historical data format | `2` or `3` | PIT bundle schema and the associated supported provenance/universe structure. | `core/pit_data.py`; feature building accepts format 3 and permits format 2 only through an explicit development flag. |
| Other local schema versions | For example, market context V1 or V5 artifact `schema_version=5` | A particular serialized record shape, not the optimizer, policy interface, or data bundle. | `MarketContextV1`; V5 artifact contracts. |

Do not call a V5 artifact's `schema_version=5` a policy-interface version. Do not call a V3 policy snapshot a PIT data-format version. Legacy PIT format 1 is readable in `PITDataBundle`, but this compatibility contract's supported research comparisons name format 2 or 3 explicitly; format 2's feature use remains development-scoped under the #66/#79 rules.

### Canonical experiment and evidence identity vocabulary

For shared reporting, use the exact names coordinated with #81:

- `source_revision`
- `runtime_identity`
- `evaluator_image_digest`
- `evaluation_mode`
- `input_bundle_id`
- `evidence_root_id`

Each evidence item additionally carries its `path`, content `hash`, and `visibility` (for example, permitted discovery input, reserved/withheld evidence, or report-only evidence). These names are a reporting vocabulary; this document does not add a schema.

Keep these additional dimensions distinct and bind them to the canonical identities where applicable:

- `optimizer_version` (`5`)
- `policy_interface_version` (`3`)
- `policy_artifact_id` (V5 policy-revision identity, including the four editable source hashes, trusted policy runtime hash, and immutable-constraint hash)
- `feature_contract_id` / feature-calculator identity
- `historical_data_format_version` (`2` or `3`), exact PIT bundle digest, price provenance and identity-transition contract
- evaluator contract/source identity
- `execution_profile_id`, friction/cost scenario and benchmark
- paper deployment generation and runtime/environment identity when a policy is deployed

**Current V5 bindings:** `PolicyRevisionIdentityV5` pins interface 3, four editable source hashes, trusted runtime and immutable constraints. `EvaluatorContractV5` binds evaluator source, execution and sandbox profile hashes, PIT bundle, price provenance, identity transition, baseline source/revision, friction grid, selected scenario, SPY benchmark and daily signal cadence. `SandboxProfileV5` carries the image digest and runtime source hash. These typed hashes are stronger than labels alone.

**Proposed completeness rule:** two results are comparable as a policy-only experiment only when feature calculator, data bundle/format, evaluator, execution/cost assumptions, benchmark, mode and relevant source/runtime identities match. A change to any of these is a different experiment context and must be reported as such. Paper records must identify the exact policy and deployment generation plus current input-bundle/feature and runtime identity. Do not claim optimizer V5 currently serializes all six #81-friendly field names in one record; map existing authenticated V5 artifacts to this vocabulary.

## 2. Input truth and source-to-policy path

The trusted data/feature layer owns factual truth. Policy code may choose how to act on valid exposed facts; it cannot alter their source, public date, calculation, denominator, or account reconciliation.

### 2.1 Historical rules from the reviewed #66 contract

These rules are carried through unchanged:

1. **Universe and claims:** production research uses the dated historical union of the S&P 500, Nasdaq-100 and Russell 2000, with stable security lineage and overlapping index tags preserved. Count a security once in the pooled union denominator per session and publish per-index slices too. A reduced/S&P-only bundle is development-only; it does not establish production coverage or relax a gate. Foreign issuers stay in membership/price scope when membership requires them even when their financial features are unsupported.
2. **Public availability:** keep source-public timestamp/date distinct from `available_from_session`. A fact first becomes eligible on the first eligible exchange session strictly after its supported source-public date, including when an intraday source timestamp is known. A supported earnings-release record can precede a filing only when it proves the value, reporting period and public date; otherwise use supported filing publication/acceptance date. Provider ingestion time and fiscal-period end are not public dates. Never backdate restatements or use today's profile to fill a historical snapshot.
3. **Avoid double shifting:** if normalized bundle `public_date` already means the first eligible session, do not apply the next-session rule a second time. Preserve the underlying source-public date separately for lineage.
4. **Compact financial history:** full SEC filing text is not required. Initially supported filing-backed financials are U.S. domestic Forms 10-Q/10-K and amendments 10-Q/A/10-K/A. Other filing forms, including foreign 20-F/6-K, are outside the initial financial claim. Keep the agreed quarterly and annual EPS/revenue headline histories and annual ROE with fiscal period, exact metric basis, date/vintage and source identity. Annual revenue history is in scope but is not established as a consumer of the current baseline score. Annual ROE remains in scope for the existing score.
5. **Growth definitions:** quarterly comparisons match the same fiscal quarter to the closest prior-calendar-year period within 28 calendar days of the same month/day; unmatched periods, conflicting duplicates and equal-distance ties are unavailable. Do not skip a newest unmatched quarter to reuse an older one. Growth uses a valid positive prior value; zero/negative prior-value behavior remains unavailable under current baseline semantics. Annual EPS currently uses adjacent available annual observations in date order; if a missing year makes that differ from a direct one-year vendor growth series, record it for #71 rather than claiming equivalence. Acceleration is newest quarter YoY growth less the immediately previous quarter's YoY growth, in decimal fractions. Q4 may be explicit or derived from compatible annual minus Q1–Q3 values only after the annual value is public; otherwise unavailable.
6. **Feature scope:** preserve #66's definitions and scope for 12-month weighted RS (65-session periods, 0.40/0.20/0.20/0.20 weights, 1–99 cross-sectional rank, labeled short-history fallback); ATR20 fraction; breakout gap fraction; ADV50 currency/session; 52-week-high distance fraction; volume ratio; dated index affiliation; dated industry group and industry-group RS; distinct sector assignment, sector RS and portfolio sector exposure; aggregate institutional ownership quantity/counts; shares outstanding as a labeled proxy denominator, not public float; revenue growth plus price-high proximity as current catalyst/supply proxies. Sponsor quality and dated product/management events remain optional/deferred as stated in #66.
7. **Freshness and missingness:** #66's source/coverage vocabulary distinguishes `not_yet_public`, `absent`, `unsupported_scope`, `insufficient_history`, `stale_by_declared_rule`, `invalid`, and `observed`. Zero is observed zero, never missing. Numeric policy fields use `None` when their contracts permit it; preserve reason/provenance in coverage records. There is no new universal stale threshold in this document.
8. **Coverage:** use eligible member-security × scheduled-decision-session denominators; report unique securities too, keep the pooled union and per-index slices distinct, and report lineage, raw-source, public-date, lookback, calculation and policy-input readiness separately with reasons. Record the exact inspected bundle and retain existing production gates.

**#80 review of #97's detailed-to-concise status mapping:** preserve #66's detailed reason and its supporting provenance at the trusted feature/coverage boundary. A policy or deployment layer may derive a concise capability state for routing or display, but that projection is lossy by itself and must never replace, overwrite, or detach from the detailed reason and provenance.

| #66 detailed state | Derived concise capability state | Detail retained with the projection |
|---|---|---|
| `not_yet_public` | `not_yet_public` | Source-public date and first eligible session. |
| `absent` | `unavailable` | Source and scope searched. |
| `unsupported_scope` | `not_applicable` | The specific unsupported-scope reason. |
| `insufficient_history` | `unavailable` | Required versus observed lookback. |
| `stale_by_declared_rule` | `stale` | Declared rule and observed age. |
| `invalid` | `calculation_failure` | Original invalid state and exact failure reason. |
| `observed` | `present` | Value, units, period, revision, provenance and as-of time. |

Because `absent` and `insufficient_history` both project to `unavailable`, the concise state alone cannot reconstruct the source condition. The detailed state plus its provenance remains authoritative; the projection is a derived convenience and is not a new V3 wire field.

### 2.2 Source fields and exposed features

| Source facts | Trusted calculation and timing | Policy-visible V3 field(s) | Status at `c628a3a` |
|---|---|---|---|
| Dated, stable-identity OHLCV and SPY/QQQ/IWM price histories | Exact completed-session prefix; split-adjusted price basis; the formulas and warmups above. | `EntryFeaturesV3`: ATR20, breakout gap, ADV50, 52-week-high distance; `HoldingFeaturesV3`: current RS, ATR20, volume ratio. `MarketContextV1`: benchmark-to-SMA fractions, realized-volatility fractions, breadth and RS coverage. | Formulas exist. Production history/corporate-action coverage is not thereby accepted. |
| Fiscal-period EPS, revenue, net income, equity, supported source-public date and revision/source identity | Apply #66's eligibility session and metric-family/period matching; keep provenance and period. | V2-base scores/growth (`current_growth`, `annual_growth`, CANSLIM sub-scores); V3 quarterly earnings/sales acceleration and `fundamental_age_days`. | Code builds causal features; data coverage, full accepted-source bridge and #66's complete scope integration remain separate evidence. |
| Dated industry assignments and active-universe RS | Assign only using effective and public dates; calculate classified-group mean RS under #66's denominator rule. | `EntryFeaturesV3.industry_group_rs`, `HoldingFeaturesV3.industry_group_rs`. | Calculation path exists; available values depend on accepted assignment inputs. |
| Dated sector assignments and constituent/portfolio facts | Separate sector-relative-strength aggregation from portfolio exposure aggregation. | `sector_rs` and `PortfolioFeaturesV3.sector_exposures`. | `sector_rs` is nullable; #66 says current `None` is unavailable, not neutral zero. The simulator currently places all sector notional under `unclassified` because dated sector assignments are not integrated; arithmetic reconciliation is not a meaningful sector breakdown. Bundle/calculation/coverage are not established by the type alone. |
| Dated reported institutional holdings/counts and dated denominator if a fraction is derived | Expose quantity/trend only, preserve filing periods and denominator. | The current V3 feature tuple does not contain raw ownership quantity or sponsor quality; older base scores/flags exist. | #66 saved audit found ownership fields empty in the development bundle; this is saved evidence, not a new measurement. No sponsor-quality claim. |
| Dated headline revenue and price highs | Apply #66's fiscal/public-date rules and 252-session high calculation. | Sales growth/acceleration plus distance from 52-week high. | Proxy combination only; it does not represent a product launch or management event. |

**Boundary note:** a field's existence, its source being available, its correct public-time mapping, calculation coverage, and baseline consumption are five separate facts. Annual revenue can be in the data contract without a baseline score consumer. A sector field can exist and be `None` without sector coverage. An aggregate institutional score does not identify high-quality sponsors.

## 3. Policy boundary and shared units

V3 snapshots are frozen, strictly validated, scalar/canonical contracts. `StrategyPolicyAdapterV3` accepts trusted engine scalars and feature objects, not a bundle, DataFrame, database row or file path. It reconciles cash/notional/risk/classification totals before deriving policy-facing fractions. Policy source authoring remains closed to exactly:

- `core/strategy_policy/v3/entry.py`: `evaluate_entry`
- `core/strategy_policy/v3/risk.py`: `recommend_capacity`, `recommend_allocation`, `select_eviction`
- `core/strategy_policy/v3/position.py`: `evaluate_add_on`
- `core/strategy_policy/v3/exit.py`: `evaluate_exit`

### 3.1 Unit conventions

| Value family | Meaning / unit |
|---|---|
| Price fields (`entry_price`, OHLC, stop, EMA, pivot, benchmark prices) | Currency per share in the declared portfolio/security currency and execution price basis. Historical and paper comparison must declare any currency conversion and adjusted/unadjusted basis. |
| Quantity fields (`original_qty`, `remaining_qty`, `current_quantity`, sell quantity) | Shares (possibly fractional in simulator data); broker share precision and minimum increments are execution constraints, not policy units. |
| Monetary totals (`equity`, `cash`, gross notional, realized P&L, ADV) | Currency amounts; use one declared account/portfolio currency for portfolio amounts. ADV50 is currency per session. `realized_pnl` is currency, not a fraction. |
| `*_fraction`, exposure, risk, return, price distance, growth and allocation inputs | Decimal fraction: `0.01` means 1%. Every field must state its denominator: typically portfolio equity for exposure/risk, entry price for holding return/price movement, and previous-period value for growth. Growth acceleration is the difference between decimal growth rates (e.g. `0.15` = 15 percentage points). |
| `*_pct` policy/settings fields | Despite the suffix, the current contracts validate these as decimal fractions in `[0,1]`; e.g. `configured_stop_loss_pct=0.08` means 8%, not 0.08%. |
| RS, CANSLIM and composite scores | Score scale defined by the named calculation. RS is 1–99 under #66. The contract validator checks finite numbers, not the full scoring scale or comparable meaning across feature versions. |
| Counts and tiers | Nonnegative integers. `days_held` in the simulator counts completed trading sessions; `fundamental_age_days` is calendar days under #66. Explicitly name calendar/session unit for any new field. |
| Market breadth/coverage/exposures | Fractions in `[0,1]`; denominators are active constituent count, portfolio equity or mapped group exposure as specified by their field. Market context V1 validates count/fraction reconciliation. |
| Missing scalar features | `None` means the policy has no usable numeric value. It does not encode why; coverage/provenance at the trusted feature boundary must retain the #66 detailed reason and evidence even when a derived concise state is also supplied. Finite validation rejects bool/NaN/infinity. |

**Current V3 missingness limitation:** nullable numeric fields serialize missing as `None`, so the policy snapshot cannot distinguish `not_yet_public`, `absent`, `unsupported_scope`, `insufficient_history`, `stale_by_declared_rule`, or an upstream invalid/calculation failure. `EntrySnapshotV3.institutional_data_available` is only a coarse boolean. `EntryFeaturesV3` / `HoldingFeaturesV3.sector_rs` is nullable and carries no reason; the current feature builder emits `None`, while the simulator maps unclassified sector notional to `unclassified` for gross reconciliation. That label is not evidence of sector availability or a detailed missingness cause. Invalid/nonfinite values can be rejected by validators, but the rejection is not serialized as the #66 reason in the policy snapshot. These are lossy or unsupported V3 representations: retain the detailed reason and provenance in the trusted feature/coverage record and link any policy/deployment projection to that record. Do not add a schema field under this documentation-only contract.

**Other current limitation:** not every V2-base numeric field carries unit or denominator metadata at runtime. This document supplies the semantic dictionary; it does not silently change V3 serialization. If a required meaning cannot be established from the feature contract or source lineage, the host should not substitute zero or a guessed unit.

### 3.2 Input and state inventory

- `MarketContextV1`: ISO decision-session date; regime label; distribution-day count; follow-through boolean; SPY/QQQ/IWM benchmark context; active constituent count; breadth-above-50/200 fractions and their coverage; median RS; fraction at RS ≥ 80 and RS coverage. Coverage numerators must reconcile to active constituents. Market context schema V1 is separate from policy interface V3 and historical data format 2/3.
- `EntrySnapshotV3`: complete V2 entry facts plus causal `EntryFeaturesV3`. The V2 base includes CANSLIM/component/composite scores and nullable growth/RS/technical price-volume values, technical gates/reasons, market booleans and switches. The V3 feature tuple includes affiliations, nullable industry/sector RS, earnings/sales acceleration, fundamental age, ATR20, breakout gap, ADV50 and 52-week-high distance.
- `PortfolioFeaturesV3`: gross exposure, drawdown and open risk as equity fractions; pending entry count; sector and industry exposure tuples. The adapter requires long-only no-leverage reconciliation: cash + gross = equity; open risk ≤ gross; grouped exposures reconcile to gross. With zero gross, exposure tuples are empty; with positive gross, they must cover/reconcile.
- `CapacitySnapshotV3`: V2 configured and maximum policy position counts, current open count, eligible signal count, cash fraction and eviction setting, plus reconciled portfolio features.
- `AllocationSnapshotV3`: V2 equity/cash/projected post-eviction amounts, current/projected gross notional, entry-open price, pending entry slots, configured/max risk and stop fractions and optional scores, plus candidate entry features and portfolio features.
- `EvictionSnapshotV3`: candidate RS and capacity/full/permission facts, candidate features, each position's snapshot `slot`, entry/current causal price when available, holding features, unrealized return, `days_held`, notional/equity fraction, and reconciled portfolio state.
- `AddOnSnapshotV3`: completed-session market context, entry/current price and current shares, current notional/equity fraction, unrealized/favorable/adverse return fractions, holding-session count, add-on count, remaining-cash fraction, open-risk/equity fraction, and refreshed holding features.
- `ExitSnapshotV3`: V2 exit inputs plus refreshed holding features, unrealized/favorable/adverse return fractions and position-notional/equity fraction. V2 exit state carries entry/original/remaining shares; stop and OHLC/EMA prices; realized P&L; holding-session and history counts; peak close; tier and stop-state flags; protective-stop candidates; and the active exit thresholds/tiers.

## 4. Six policy decision categories

The **policy** selects a strategy preference from eligible choices. The **trusted host** calculates features and portfolio facts, validates policy output, applies hard account/configuration/resource/order constraints, models or submits execution, accounts for fills/costs, preserves state, and explains rejection/capping. A hard guard may prevent an action, but it must not be disguised as the policy's own preference.

| Category | Current input → output and units | Current source semantics and constraints | Proposed shared/paper meaning and remaining point |
|---|---|---|---|
| **Entry eligibility and ranking** | `EntrySnapshotV3` → `EntryDecision(qualified, market_permitted, rank=(score, RS), blocking_codes)`. Rank values are nullable finite scores; RS is 1–99, base CANSLIM score uses its documented score scale. | Baseline thresholds require current/annual growth ≥ 0.25, RS ≥ 80, composite ≥ 70 unless technical-only; `qualified` and market permission are separate. Baseline rank is `(canslim_score, rs_score)`; simulator sorts both descending and treats unavailable sort values as lowest. The decision validator checks tuple length/types, not ordering semantics. | Policy owns qualification/rank within trustworthy facts. Host enforces active universe, freshness/causality, per-account limits and configured market safeguards, with each veto/code retained separately. Paper scanner gates must not silently replace the selected policy decision. Confirm rank-score identity when feature/calculation version changes. |
| **Capacity** | `CapacitySnapshotV3` → `CapacityDecision(max_positions: positive integer or None, eviction_enabled)`. Counts are whole positions/signals; cash/exposure is equity fractions. | Output cannot exceed `maximum_policy_positions`. Baseline returns configured max/eviction. In the simulator, `None` means uncapped by policy position count; a separate engine ceiling and account constraints still apply. | Policy owns desired portfolio breadth and whether it is willing to replace. Host applies stricter configured/account/order limits. Define cap/veto as requested/effective values with reason. |
| **Allocation** | `AllocationSnapshotV3` → `AllocationDecision(risk_fraction, stop_distance_fraction, notional_fraction_cap?)`. Percent-suffixed inputs and outputs are decimal fractions; money is portfolio currency, `entry_open` is currency/share. | Validators enforce risk/stop not above provided maxima, but permit zero. V5 simulator uses notional = equity × risk fraction ÷ stop-distance fraction, applies optional notional cap and engine cap, then constrains to risk budget/cash/gross exposure and friction. Source ceilings include 1% equity risk and 8% stop distance for this path. A zero stop can fail during sizing; zero risk yields unusable notional. | Policy owns preferred risk budget, stop distance and optional equity-notional cap. Host must reject invalid/nonpositive buy sizing and enforce actual buying power, aggregate risk, cash, leverage, precision, cost and account limits. It must state whether a guard rejects or caps and record requested/effective amounts. |
| **Replacement / eviction** | `EvictionSnapshotV3` → `EvictionDecision(slot?)`. Slot is a current snapshot index, not a durable position identifier; price is currency/share, return/notional are fractions, `days_held` is sessions. | Baseline selects an underperforming held slot only when full and eviction is enabled; it prefers underwater eligible positions, then lowest RS with slot tie-break. V5 simulator projects liquidation of the selected position's remaining quantity at the next entry open (with scenario friction) before sizing the candidate. This is portfolio replacement, not broker-order amendment. | Policy chooses whether and which holding to replace. Host maps a slot to durable holding identity, sells the selected remaining position, reconciles actual proceeds/fills, then revalidates candidate capacity/cash/risk before buying. Paper partial-fill/cancel/restart behavior is proposed runtime work (#97/#100–105), not proven by the simulator path. |
| **Addition to an existing holding** | `AddOnSnapshotV3` → `AddOnDecisionV3(add, risk_fraction, notional_fraction_cap?, reason_code)`. Prices/shares are native units; risks, notional and cash are fractions of equity. | A decline must have zero risk and no cap; an add must have positive risk. Validator checks cap is above current notional and no higher than funded notional, requires remaining cash, and bounds combined risk by equity. V5 simulator calls on completed-session state, limits per-position add count and risk, then queues accepted adds for the next eligible session open. Baseline deliberately declines all additions. | Policy chooses to add and proposes incremental risk/cap. Host reconciles current position, buying power, open risk and pending actions, prevents duplicate adds and observes account limits. Pending-entry capital/risk reservation is **proposed** below; `pending_entry_count` alone is not such a reservation. Paper support remains an action capability to verify. |
| **Exit and stop management** | `ExitSnapshotV3` → ordered `ExitAction` tuple (scale-out or close), `next_stop_price?`, and persistent hold/tier/breakeven/trailing state. Prices are currency/share; realized P&L currency; quantities shares; returns/thresholds fractions of entry price; tier/counts are integers. | Scale-outs name a positive trigger-gain fraction and a fraction of **original** shares. Validator requires the next sequential declared tiers to have been crossed by `current_high`, total original-share quantity not exceed `remaining_qty`, and `scale_out_tier` increment by number of scale-outs. A close has an approved reason, carries no scale-out amounts, and nothing follows it. A proposed next stop must be one of the trusted candidates and cannot loosen a long stop. The V5 path evaluates after the completed bar and queues policy exit actions for the next eligible open; protective stops are separately resolved as execution behavior. The legacy path has distinct same-session close/stop behavior. | Policy owns holding decision and stop proposal. Host owns protective order placement, tick/lot precision, order acceptance, actual fills, durable tier/remaining quantity/stop state and restart recovery. Preserve original-share fractions. Keep requested tier/action pending, suppress duplicate submissions, and derive actual filled shares from reconciliation. Whether tier state advances at decision, full fill or a documented partial-fill threshold remains an explicit paper integration decision; the V3 decision validator currently advances it at decision time. |

### Current baseline behavior is intentional policy scope

V3 policy modules are parity-first wrappers for entry, capacity, allocation, replacement and exits; the V3 add-on module returns a decline. This is not a claim that the six hooks are missing. Nor does a research hook prove the paper runtime supports it. The closed candidate-authoring scope remains the four paths in §3; input truth, accounting, evaluation criteria and execution invariants stay outside candidate edits.

## 5. Shared state, timing, and host invariants

### 5.1 Decision and action timing

**#66 settled input timing:** facts first become visible on the first eligible exchange session strictly after their source-public date. Historical V3 feature calculation uses an authenticated PIT bundle and a price prefix no later than the completed policy session. Keep `source_public_at/date` distinct from an already shifted `available_from_session`.

**Current V5 simulation path:** entry signals are formed from a completed session and candidate entries execute at the next eligible entry open; allocation uses the entry-open price and portfolio facts at that opportunity. Add-on decisions are explicitly built from completed-session facts and queued for next-session open. V5 policy exits are evaluated after the completed bar and queued to the next eligible open. Protective stops are an execution guard processed separately, including opening gaps. A pending action may be delayed when required next-open data is missing; terminal/no-next-session behavior is reported by the simulator. The non-V5 legacy simulator uses a separate exit path and is not equivalent by implication.

**Proposed paper contract:** bind each decision to a session/as-of cutoff and a target opportunity. Execute at the next eligible opportunity after revalidating current account/broker state. Record delayed, expired, rejected and duplicate-suppressed actions; do not silently retarget an old order using later features. A protective stop already resting with the broker is distinct from a new after-close policy exit. If policy decisions use same-session high/low, those facts are only available after the session close; a resulting discretionary policy action cannot be backdated to an earlier intraday price.

### 5.2 Durable state

**Current historical state:** V3 engine state includes original and remaining shares, entry basis, stop, realized P&L, holding-session count, scale-out tier, breakeven/trailing/early-hold flags, and V5 position-episode/add-on state. The simulator is not a broker-connected paper engine.

**Proposed paper state:** persist policy artifact and deployment generation, stable security/holding-episode identity, original and remaining filled shares, cost basis, realized P&L, active stop and protective-order identity, scale-out decision/tier and fill progress, add-on count/risk, portfolio peak/account facts, each pending action's idempotency identity, reservation and lifecycle, and last accepted decision session. Bind each open episode and pending action to its opening policy generation by default; a policy cutover needs an explicit migration or safety protocol. This aligns with #97's proposed generation-pinned behavior; it does not assert those records already exist.

### 5.3 Pending capital and risk (proposed, not current V3 contract behavior)

Reserve the maximum committed cash and incremental risk once for each accepted, nonterminal entry/add-on/replacement intention. When partially filled, replace the corresponding reservation with reconciled filled notional/risk and keep a reservation only for the unfilled remainder. Release the remainder only when the order is terminally canceled, rejected, expired or otherwise closed. On restart, reconcile broker orders/fills before recomputing reservations. Never count a fill and its former reservation twice.

This conservative rule can reduce concurrent capacity; it prevents two concurrent decisions from spending the same cash or risk budget. Current V3 exposes a pending-entry **count**, not reserved dollars/risk. The historical simulator constrains sequential pending-entry allocations through available/projected cash and per-entry notional caps, but that is not evidence of a durable paper reservation ledger. #97 must decide exact reserve amount (including fees, stop risk and replacement proceeds), partial-fill conversion and terminal states before implementation.

### 5.4 Invariants the policy cannot change

- **Causality:** only facts available by the named decision session; exact security identity and authenticated lineage; no future row, current-profile backfill or hidden evaluation panel.
- **Feature integrity:** trusted host calculates features from declared source, formula, units, warmup, public date and feature-contract version. Invalid provenance/units/nonfinite values fail closed. Missing is not zero.
- **Accounting:** one portfolio currency and timestamp; cash, equity, gross exposure, open risk, sector/industry totals and reservations reconcile. V3 long-only snapshots prohibit leverage and require risk ≤ gross. No candidate-authored bookkeeping.
- **Risk/capacity:** V5 simulator validates configured and maximum policy position caps, 1% entry/add risk ceilings and 8% stop ceiling for the declared V5 path; current account buying power, configured caps and aggregate risk may impose stricter limits. Any applicable cap/veto is visible and attributable to the host.
- **Execution:** valid positive quantity/price, order precision, broker minimums, available buying power, current position/order state, friction/cost model, fill accounting, stop behavior, idempotency and durable recovery belong to the host. A policy output cannot promise a fill.
- **Decision audit:** retain input identity, policy output, validation result, host guard/cap with requested/effective values, intended action, broker lifecycle and actual fill separately.

Current validators implement several of these constraints but not all paper lifecycle/observability requirements. This list does not authorize changing the simulator or paper engine in this documentation issue.

## 6. Source version, identity, and comparability record

A result/deployment record should resolve this minimum identity set (field names in the first column are the agreed shared vocabulary; IDs/digests map to current authenticated artifacts where they exist):

| Canonical field / dimension | Required content |
|---|---|
| `source_revision` | Exact repository commit used to produce the policy/evaluator/build. If only source artifacts are available, include their exact hashes and do not invent a commit. |
| `runtime_identity` | Trusted policy/evaluator runtime source identity and immutable constraint identity. Preserve V5's exact trusted runtime/constraint hashes; paper records add the deployed application/runtime identity. |
| `evaluator_image_digest` | Immutable evaluator sandbox image digest; V5 `SandboxProfileV5.image_digest`. Not a mutable image tag alone. |
| `evaluation_mode` | Explicit `production`, `development_sp500_v2`, or `synthetic_fixture` where applicable; also identify panel/stage (discovery, confirmation, qualification, replay) as a distinct dimension. Do not imply these modes are interchangeable. |
| `input_bundle_id` | Exact PIT bundle digest plus `historical_data_format_version`, universe/date range, price provenance, security-identity transition and feature-contract/calculator identity. A path alone is not an identity. |
| `evidence_root_id` | Authenticated/hashed root identity for retained evidence. Per artifact include path, hash, visibility and role; distinguish allowed candidate input from reserved/withheld evidence. |
| `optimizer_version` / `policy_interface_version` | Separate literal values `5` and `3`. |
| `policy_artifact_id` | Exact V5 `PolicyRevisionIdentityV5` digest and its 4 source hashes, runtime hash, constraint hash and interface version. |
| `evaluator_contract_id` | V5 evaluator contract/source and execution/sandbox profile hashes. |
| `execution_profile_id` and cost assumptions | Exact fill/friction grid, selected scenario, benchmark and execution assumptions; preserve unknown cost inputs as unknown. |
| deployment generation (paper only) | Selected policy identity, feature/action capability contract, runtime/environment/store identity and activation/rollback record. |

**Comparison rule:** for a matched baseline/candidate policy experiment, hold `source_revision` context, runtime/constraints, evaluator/image, input bundle/feature calculations, evaluation mode/panel, execution profile, friction/cost scenario, benchmark and qualification criteria fixed; vary the policy artifact and its declared hypothesis. When any fixed dimension changes, retain a new experiment identity and explain the change. A paper deployment comparison is input → decision → host guard → execution intent → actual fill; identical historical and paper profit is not the compatibility criterion.

## 7. Cross-issue alignment and evidence

### Coordination record

- **#66 historical features:** source/publication rules, metric bases, financial scope, growth matching, Q4, units, missingness reasons, lookbacks, data status and coverage denominators are taken from its independently reviewed feature contract. This document does not replace its choices. In particular, facts first become visible on the next eligible session; domestic 10-Q/10-K plus amendments is the initial filing-backed scope; foreign financial features are deferred; quarterly/annual EPS and revenue history plus annual ROE are retained; ownership is quantity/trend, not sponsor quality; industry and sector remain distinct.
- **#97 paper deployment:** Following #97's finalized deployment contract, the #80 cross-contract review point for its detailed-to-concise missingness mapping is **closed**. #66's detailed provenance remains authoritative at the trusted feature/coverage boundary; any concise capability state is a derived projection that retains a link to that detail. The current V3 lossiness and unsupported representation are recorded in §3.1. The canonical identity vocabulary and proposed defaults of generation-bound holdings/actions, one-time pending reservation, original-quantity partial exits, completed-session to next-opportunity timing, fail-closed unsupported actions and visible host caps align with this contract. The exact reservation lifecycle and policy-exit tier handling through partial fills/restart remain implementation-level decisions and must not be marked as current support.
- **#81 reproducibility:** uses the six canonical identity names listed in §1; per-item evidence includes path/hash/visibility. No competing names or data schema are introduced here.
- **#71 / historical metric acceptance:** annual growth behavior when an intervening fiscal year is missing must be reported as the reviewed #66 draft specifies; resolve against the existing baseline metric before claiming direct-source parity.
- **Historical evaluator/simulator:** one shared `PortfolioSimulator` is used by V5; the paper runtime remains a separate execution path. Review must specifically confirm timing, fill/cost, account arithmetic and stop behavior for the selected mode.

### Acceptance evidence proposal

Accept this contract when the owners record review at a named source revision and evidence includes:

1. The field/source map from §2 and input/state inventory from §3 reviewed against the #66 feature specification without changing its decisions.
2. A six-category matrix from §4 with units/denominators, state, decision timing, policy authority, host invariants, and current/proposed support status. Fixtures/examples cover one capacity decision, one allocation in risk/stop/notional fractions, replacement ordering, a pending reservation across a partial fill/cancel, an add-on, and an original-quantity scale-out through partial fill/restart.
3. Identity records showing optimizer 5, policy interface 3 and PIT formats 2/3 as separate fields and mapping the six agreed #81 names to V5 artifacts without inventing absent code fields.
4. The recorded #66/#97 alignment on canonical identities, strict-next-session timing and the detailed-to-concise missingness mapping, plus #97's action defaults, is coordination evidence only; it does not constitute #66 acceptance or full #80 contract adoption. Remaining evidence includes full #80 owner adoption if not separately recorded, independent historical simulator/evaluator review, deterministic cross-runtime fixtures, and explicit disposition of the remaining public-date normalization and fill/tier questions.
5. Source check that optimizer candidate authoring remains exactly the four policy files; no schema/engine change is part of this acceptance artifact.

These items prove contract clarity and identity accounting, not production data coverage, strategy improvement, complete paper adapter support, broker readiness or profitability.

## 8. Four separate statuses and current limitations

The published #80 assessment remains: **Implementation: Not assessed; Required inputs: Not assessed; Acceptance evidence: Not assessed; Dependencies: Ready to start.** Do not update those Project values from this local draft alone.

- **Implementation:** Current code has substantial V3 snapshot/decision contracts, validators and a closed four-file authoring scope. This document is the proposed shared semantic layer. V3 fields do not carry all source missingness reasons; the paper runtime does not thereby gain support for all six policy actions.
- **Required inputs:** Reviewed #66 feature-spec draft, V3 source contracts and V5 artifact contracts were available. Cross-owner review evidence and deterministic cross-runtime fixtures remain needed for full acceptance. Production data or a winning strategy is not required to define this contract.
- **Acceptance evidence:** Source checks at `c628a3a` substantiate the current/proposed distinctions below. The recorded #66/#97 coordination covers canonical identities, strict-next-session timing, the detailed-to-concise missingness mapping, and #97's action defaults; this does not imply #66 acceptance or full #80 contract adoption. Full #80 owner adoption if not separately recorded, independent historical simulator/evaluator review, deterministic cross-runtime fixtures, and disposition of remaining public-date normalization and fill/tier questions remain outstanding. No #80 independent reviewer was assigned in this task. This file does not count as full acceptance.
- **Dependencies:** #80 is ready to start under its published register. #66/#97 agreement is acceptance coordination, not a start blocker. No external provider, model, broker, or trading operation is required.

### Explicit remaining decisions / source-contract gaps

1. **Availability-date field mapping:** #66 distinguishes `source_public_at/date` from `available_from_session` and warns against double-shifting. `core/pit_feature_snapshot.py` validates the PIT bundle's `public_date` provenance against the completed feature session. Record whether the normalized `public_date` attribute at every boundary carries source date or first-eligible session; adapters must shift exactly once. Do not change #66's next-session rule.
2. **Scale-out state:** V3 decision validation increments `scale_out_tier` when it proposes each crossed tier; V5 historical execution assumes its simulator action resolution. A broker may partially fill. #97 must separate pending decision/tier from reconciled filled shares and specify terminal/partial retry behavior without changing the action's fraction-of-original-quantity meaning.
3. **Reservation lifecycle:** V3 exposes a count, not reserved capital/risk. The reservation behavior in §5.3 is proposed and requires #97 paper-state review; historical simulator sequential cash caps are not a durable reservation ledger.
4. **Rank and score identity:** score fields are finite numeric values but their scale/version is not embedded in all V2-base snapshots. Bind the exact feature/scoring calculator and preserve the simulator's current descending ordering; a different score recipe is a new feature/policy experiment context.
5. **Guard response and observability:** source validators reject some bad values and cap some sizes at engine boundaries; specify per-decision whether a host guard rejects, caps, or suppresses, and retain requested/effective output plus reason. Do not silently count a host veto as policy rejection.
6. **Paper action support:** #97 must mark each action category supported, partial or unsupported for a specific deployable runtime and policy. Research contract presence is not a paper capability claim.

## Source references checked

- `core/pit_optimizer_v5/policy_scope.py`
- `core/pit_optimizer_v5/candidate_ir.py` (`PolicyRevisionIdentityV5`, `ExperimentIdentityV5`)
- `core/pit_optimizer_v5/contracts.py` (`SandboxProfileV5`, `EvaluatorContractV5`)
- `core/pit_optimizer_v5/manifest.py`
- `core/pit_data.py`
- `core/pit_feature_snapshot.py`
- `core/strategy_policy/__init__.py`
- `core/strategy_policy/adapter_v3.py`
- `core/strategy_policy/contracts.py`
- `core/strategy_policy/contracts_v3.py`
- `core/strategy_policy/entry.py`, `risk.py`, `exit.py`, `v3/entry.py`, `v3/risk.py`, `v3/position.py`, `v3/exit.py`
- `core/backtest_engine.py` (V5 entry sizing/replacement, V5 after-close exits and add-ons, legacy exit path, protective stops)
- Independently reviewed #66 `feature-specification.md` in the #66 issue artifact; #97 identity/action proposal; #81 identity vocabulary message.

No source, schema, test, configuration, data or retained historical evidence was changed by this contract draft.
