# PIT CANSLIM Strategy Diagnosis Loop Design

**Date:** 2026-08-24
**Branch:** `codex/pit-canslim-strategy-diagnosis`
**Status:** Architecture approved; implementation plan pending spec approval

## Objective

Build a deterministic, point-in-time strategy-diagnosis system that explains why the current
CANSLIM baseline underperforms before any parameter optimization. Reuse the existing multi-agent
backtest controller for quarantine, role sequencing, budgets, private evaluation, and audit
evidence, but add a separate PIT diagnosis gate whose measurements and pass/fail decisions remain
controller-owned.

CANSLIM fidelity is a hard constraint. A candidate that improves return by weakening a required
CANSLIM rule is ineligible for promotion. Diagnostic counterfactuals may temporarily remove a rule
to measure its causal effect, but they must be labeled `diagnostic_only` and can never become a
candidate proposal.

## Baseline authority

The diagnosis starts from the corrected immutable baseline already merged at `eb93437`:

- PIT bundle:
  `.artifacts/task-4-regeneration-20260823T223000Z/pit-bundle/pit_baseline.sqlite3`
- Bundle SHA-256:
  `1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb`
- Replay:
  `.artifacts/task-6-corrected-replay-20260824T100045Z/run-20260824T100213Z-1af306ef1e46/`
- Evaluation window: 2021-01-01 through 2025-12-31
- Total return: -9.994717769465932%
- Annualized return: -2.0874097904821753%
- Sharpe ratio: -0.2082076838233648
- Maximum drawdown: -13.664400600134604%
- Closed trades: 225
- Win rate: 39.111111111111114%
- Average cash: 67.31359377429541%
- Qualified entries: 286
- Executed entries: 225
- Next-open buy-zone rejections: 51
- Cash rejections: 10
- Capacity, invalid-price, invalid-risk, and missing-data execution rejections: zero
- PIT-exposed five-year leader recall: 42/72 signaled and 40/72 executed
- Strict-PIT quarterly-plus-annual coverage: 80.09732938082588%

The reproduction gate must match the immutable summary, entry-outcome ledger, and state hashes
before diagnosis begins. A mismatch is a controller error, not a strategy observation.

## Methodology authority and fidelity labels

The rulebook is grounded in versioned, locally stored citations from authoritative O'Neil/IBD
material. Initial sources are:

- [IBD 20 Rules for Investment Success](https://shop.investors.com/images/promotional/20-Rules_102808.pdf)
- [MarketSmith 2023 Q4 selling-strategy guide](https://marketsurge-files.investors.com/2023/12/StockGuide-Q4-2023.pdf)
- [IBD Top Stocks 2020 discussion of follow-through days](https://shop.investors.com/images/promotional/shop/assets/pdf/TopStocks-2020.pdf)
- [MarketSmith 2024 market-exposure guide](https://marketsurge-files.investors.com/2024/07/StockGuide-Mid-year-2024.pdf)
- [Innovator IBD 50 guide summarizing the licensed CANSLIM domains](https://www.innovatoretfs.com/pdf/ffty_investor_guide.pdf)

The controller never asks an agent to retrieve external knowledge. Agents receive only the
versioned rulebook records selected for the current experiment. A rule record contains:

```text
rule_id
letter_or_domain
requirement
classification
observability
parameter_policy
source_id
source_location
implementation_status
satisfaction_logic
```

`classification` is one of:

- `required`: cannot be weakened by an exportable candidate;
- `allowed_variant`: multiple cited implementations are faithful and may be compared;
- `diagnostic_only`: useful as a counterfactual or benchmark, never promotable.

`observability` is one of:

- `pit_observed`: directly supported by the immutable PIT bundle;
- `pit_proxy`: supported only by a documented quantitative proxy;
- `pit_unavailable`: unavailable in the approved five-year data.

Every run publishes one of three fidelity labels:

- `strict_canslim`: every required rule is `pit_observed` and passes;
- `quantitative_canslim_proxy`: all observed rules pass, while every proxy is explicitly listed;
- `fidelity_incomplete`: at least one required rule is unavailable or unimplemented.

Only `strict_canslim` or an explicitly approved `quantitative_canslim_proxy` candidate may advance
to locked evaluation. `fidelity_incomplete` results remain diagnosis evidence.

## CANSLIM rulebook v1

The first rulebook version includes these immutable domains:

| Rule ID | Requirement | Classification | Initial evidence status |
| --- | --- | --- | --- |
| `C.EPS_YOY` | Recent quarterly EPS growth meets the cited CANSLIM floor | required | PIT observed |
| `C.SALES_YOY` | Recent quarterly sales growth confirms current growth | required | PIT observed where reported |
| `C.ACCELERATION` | Recent quarterly earnings or sales acceleration is evaluated | allowed_variant | PIT observed where sufficient history exists |
| `A.EPS_MULTIYEAR` | Annual earnings growth is persistent over the cited multi-year window | required | PIT observed where sufficient history exists |
| `A.ROE` | Return on equity quality is evaluated when the cited rule requires it | required | PIT observed where balance-sheet facts exist |
| `N.NEWNESS` | At least one cited form of newness passes: a new product/service/management/condition, or a price breakout to a new high from a proper base | required | unimplemented one-of rule; new-high branch can be PIT observed |
| `N.CATALYST` | A new product, service, management, industry condition, or equivalent business catalyst exists | allowed_variant | PIT unavailable |
| `N.NEW_HIGH` | Price breaks to a new high from a properly formed base | allowed_variant | unimplemented; PIT observable after `E.PROPER_BASE` and `E.PIVOT` |
| `S.VOLUME_CONFIRMATION` | Breakout demand is confirmed by price and volume | required | PIT observed |
| `S.SUPPLY` | Share supply and accumulation/distribution evidence are evaluated | allowed_variant | partially observed |
| `L.RS` | Relative-strength leadership meets the cited threshold | required | PIT observed |
| `L.INDUSTRY_GROUP` | The stock belongs to a leading industry group | required | PIT observed only after PIT group ranking is verified |
| `I.SPONSORSHIP` | Quality institutional sponsorship is present and increasing | required | PIT unavailable in the current public bundle |
| `M.CONFIRMED_UPTREND` | New purchases require a confirmed market uptrend | required | PIT observed from SPY price/volume |
| `M.DISTRIBUTION_EXPOSURE` | Distribution evidence reduces exposure or blocks new risk | required | PIT observed from benchmark price/volume |
| `E.PROPER_BASE` | Entry comes from a recognized, objectively encoded base | required | unimplemented |
| `E.PIVOT` | The buy point is derived from the proper base, not a generic prior high | required | unimplemented |
| `E.BUY_ZONE` | Entry is no more than 5% above the pivot | required | PIT observed once pivot is valid |
| `E.NEXT_OPEN` | Execution uses the exact next market-session open without lookahead | required | PIT observed and implemented |
| `X.LOSS_LIMIT` | Losses are cut no later than 7-8% below purchase price | required | PIT observed and implemented at 8% |
| `X.PROFIT_ZONE` | The normal 20-25% profit zone is evaluated | required | PIT observed, current implementation differs |
| `X.EIGHT_WEEK_HOLD` | Fast 20% advances receive the cited eight-week hold treatment | required | PIT observed, existing implementation must be audited |
| `X.STRUCTURAL_SELL` | Moving-average or distribution sell rules use cited CANSLIM behavior | allowed_variant | current 21-day EMA rule is unverified |

The present public bundle does not prove the qualitative `N.CATALYST` branch or
`I.SPONSORSHIP`, and the current prior-high pivot does not prove `E.PROPER_BASE`, `E.PIVOT`, or the
alternative `N.NEW_HIGH` branch. The N requirement can become observable through a faithful
new-high-from-proper-base implementation; it does not require a qualitative catalyst when that
cited alternative passes. The diagnosis can proceed, but the existing baseline cannot be
described as fully CANSLIM-faithful.

## Architectural approach

Use a deterministic diagnosis harness plus a new multi-agent controller gate.

### Deterministic diagnosis harness

Create a standalone offline component responsible for:

- loading and hash-verifying the PIT bundle and canonical baseline run;
- reproducing the baseline before any experiment;
- evaluating rule compliance per completed symbol/session;
- running controller-defined experiments;
- computing entry, execution, trade, exit, cash, leader-recall, and performance evidence;
- partitioning discovery, validation, and locked-evaluation periods;
- checkpointing completed experiments by immutable identity;
- publishing canonical JSON/CSV/Markdown artifacts.

The harness is the only component allowed to calculate metrics. It must not import OpenRouter,
paper trading, broker code, scheduler code, or live provider clients.

### Multi-agent integration

Add a new `pit_diagnosis` gate alongside the existing `test` and technical-only `backtest` gates.
Do not change the semantics of either existing gate.

The new gate consumes a closed evidence schema containing:

- rulebook version and hash;
- bundle and source identities;
- baseline reproduction status;
- experiment ID and classification;
- rule-compliance counts;
- signal and execution funnel counts;
- trade expectancy and holding-period statistics;
- exit-reason attribution;
- cash utilization;
- raw and PIT-exposed leader recall;
- return, annualized return, Sharpe, drawdown, and benchmark deltas;
- discovery/validation partition identity.

Raw SEC facts, raw price histories, transaction rows, source secrets, and full replay journals are
never sent to a model.

### Incremental fact cache

The harness materializes `diagnosis_facts.sqlite3` once from the verified PIT bundle and then opens
it read-only for experiments. Its primary key is `(bundle_sha256, rulebook_schema_version, symbol,
session)` and each row contains only the raw PIT facts, market data, publication timestamps, and
deterministic rule inputs available at that session. It must never contain ex-post leader labels,
experiment outcomes, agent decisions, or future facts.

The controller records the fact-cache schema and content hashes in every experiment identity.
Session-level build checkpoints allow interrupted materialization to resume without reparsing
completed symbol/sessions. A source, bundle, rule-schema, calendar, or partial-output mismatch
invalidates the checkpoint. Once finalized, the cache is immutable and reusable by every
diagnostic experiment with the same identity.

## Agent responsibilities

The Python controller remains the state machine and security boundary.

### Controller

- Owns the rulebook, experiment catalog, commands, paths, budgets, data, partitions, metrics,
  fidelity decision, and pass/fail decision.
- Selects the exact source/configuration facts an agent may see.
- Rejects any proposal that weakens a required rule or changes measurement/accounting code.
- Runs all experiments and private evaluations in network-disabled disposable workers.
- Never applies an agent patch to the real checkout.

### Orchestrator

- Performs routing only.
- Selects one controller-enumerated domain from `data`, `entry`, `execution`, `exit`, `market`, or
  `portfolio`.
- Selects evidence IDs from the supplied list or aborts.
- Does not summarize causes, choose code changes, select parameters, or produce hypotheses.

The existing free-text `failure_summary` and `reasoning_focus` route is not reused for this gate.
The PIT route schema is closed and enum-based so orchestration cannot become strategy reasoning.

### Reasoner

- Receives closed metrics, rule records, selected source snapshots, and a controller-owned list of
  allowed experiments.
- Returns one falsifiable causal hypothesis, cited evidence IDs, cited rule IDs, preserved
  invariants, and exactly one experiment ID or a skip decision.
- Cannot invent a threshold, new rule, file, command, or external fact.
- Cannot recommend a diagnostic-only experiment for promotion.

### Coder

- Is not called for data-only or configuration-only experiments.
- Is called only after the reasoner selects an allowed experiment that requires a code change.
- Returns one bounded exact-line replacement in a controller-approved strategy path.
- Cannot edit the rulebook, diagnostics, metrics, PIT loaders, tests, configuration authority,
  controller, or live/paper trading files.

Core capabilities required to make fidelity measurable -- including the rulebook, fact cache,
proper-base and pivot detector, sponsorship-data adapter, and market-state detector -- are normal
deterministic implementation work under human-reviewed source changes. Loop agents do not author
or choose those definitions. The coder is available only for bounded strategy variants after the
corresponding methodology contract and measurement path already exist.

This makes a diagnosis sample normally two paid calls. A third paid call occurs only when a
controller-approved code experiment is justified.

## Diagnostic experiment catalog

Each experiment changes one causal dimension. Every experiment declares its rule classification,
inputs, expected affected metrics, reusable facts, and promotion eligibility before execution.

### Phase 0: Reproduction

`D0.BASELINE_REPRODUCTION` reruns or validates the canonical artifacts and requires exact ledger,
metric, and identity equivalence. No agent call is permitted until it passes.

### Phase 1: Data and observability

- `D1.FULL_FUNDAMENTAL_COHORT`: compare symbol-days with complete C/A facts to incomplete rows.
- `D1.N_CATALYST_GAP`: quantify the unavailable qualitative-catalyst branch separately from the
  observable new-high-from-proper-base alternative; proximity to a high alone does not pass N.
- `D1.I_SPONSORSHIP_GAP`: quantify entries and missed leaders for which institutional sponsorship
  is unavailable.
- `D1.INDUSTRY_GROUP_GAP`: verify whether the group rank is genuinely PIT and quantify absent
  group evidence.

These experiments do not change strategy behavior and are never sent to the coder.

### Phase 2: Entry attribution

- `D2.RULE_STAGE_FUNNEL`: report incremental survivors after each fixed rulebook stage.
- `D2.PROPER_BASE_COUNTERFACTUAL`: compare the current prior-high approximation with a
  controller-defined proper-base detector once implemented.
- `D2.RS_85_CONFORMANCE`: measure the cited RS floor against the current threshold.
- `D2.LEADING_GROUP_CONFORMANCE`: apply the verified PIT leading-group requirement.
- `D2.BUY_ZONE_ATTRIBUTION`: separate signal-day qualification from exact next-open extensions.
- `D2.LEADER_RANK_BENCHMARK`: compare rank-based leader exposure as a diagnostic-only benchmark;
  it is never an entry proposal by itself.

### Phase 3: Market direction

- `D3.M_CONFIRMED_UPTREND`: require confirmed market direction for new entries.
- `D3.M_DISTRIBUTION_EXPOSURE`: measure stepped exposure reduction under distribution pressure.
- `D3.M_BASELINE_OFF`: retain the current disabled M gate only as a diagnostic-only comparison.

Restoring M is mandatory for a promotable candidate. A return improvement produced by disabling M
cannot pass fidelity.

### Phase 4: Exit attribution

- `D4.CURRENT_EXIT_PACKAGE`: preserve the current 8% stop, scale-outs, 21-day EMA, and time stop as
  the comparison baseline.
- `D4.LOSS_LIMIT_ONLY`: diagnostic-only isolation of the 7-8% defensive rule.
- `D4.PROFIT_ZONE`: implement the cited 20-25% normal profit zone.
- `D4.EIGHT_WEEK_HOLD`: audit and implement the fast-gain hold rule exactly.
- `D4.STRUCTURAL_SELL`: compare only cited moving-average/distribution sell variants.
- `D4.REMOVE_UNVERIFIED_EXITS`: measure the effect of removing the current 21-day EMA and time stop;
  exportability depends on the replacement exit package passing the rulebook.

This phase must explain the observed 92 moving-average exits, their 15.22% win rate, and their
-2.86% average completed-position return before proposing a sell-rule change.

### Phase 5: Bounded interactions

Pairwise experiments are allowed only after single-factor attribution identifies two interacting
causes. The controller creates the pair explicitly; agents cannot compose arbitrary experiments.
No higher-order parameter search is part of diagnosis.

## Time partitions and leakage control

Use three chronological partitions:

- Discovery: 2021-01-01 through 2023-12-31.
- Validation: 2024-01-01 through 2024-12-31.
- Locked evaluation: 2025-01-01 through 2025-12-31.

The existing full-period baseline has already exposed 2025 outcomes, so 2025 is not described as
an unseen holdout. Agents may receive discovery and validation deltas only. A selected candidate is
run on locked evaluation once, and its 2025 candidate deltas are not fed back into that research
generation. Any revision after opening locked evaluation starts a new research generation; it
cannot reuse 2025 as unseen evidence.

A genuine production holdout requires newly acquired PIT data after 2025, expected to begin with
2026. Until that separately acquired period exists and is evaluated without iterative feedback,
successful candidates are research candidates, not production promotions.

Five-year ex-post leaders remain benchmarks only. They never enter a feature, rule decision,
experiment selection, or agent prompt. Rolling labels use membership at evaluation time and remain
diagnostic outputs.

## Experiment identity, checkpoints, and publication

An experiment identity hashes:

- source commit and clean-tree fingerprint;
- PIT bundle SHA-256;
- baseline run-manifest SHA-256;
- rulebook SHA-256;
- diagnosis fact-cache schema and content SHA-256;
- experiment schema and parameters;
- discovery/validation/locked-evaluation partition dates;
- strategy implementation identity;
- benchmark and universe identities.

Each completed experiment is append-only. A resumable checkpoint records the experiment identity,
completed session, state hash, and artifact hashes. Resume rejects a different source, bundle,
rulebook, partition, experiment, strategy, or partial public output. Publication uses one fresh
run directory and occurs only after all internal validations pass.

Required artifacts are:

```text
manifest.json
rulebook.json
diagnosis_facts.sqlite3
baseline_reproduction.json
experiment_catalog.json
rule_attribution.csv
entry_funnel.csv
execution_outcomes.csv
exit_attribution.csv
trade_statistics.json
leader_recall.json
performance.json
ablation_results.csv
agent_events.jsonl
checkpoint.json
report.md
```

`agent_events.jsonl` contains only sanitized event types, timestamps, role names, experiment IDs,
outcomes, and hash references. Provider payloads, provider call artifacts, and code proposals
remain under the existing controller audit root, not inside the deterministic diagnosis result.

## Fidelity-eligible reference

The corrected -9.99% run is reproduction and diagnostic authority only. It is not a faithful
performance reference because mandatory M rules are disabled, proper-base and pivot recognition
are unimplemented, and the N and I requirements do not currently pass.

Before performance comparison, the controller constructs one fixed reference by applying the hard
rulebook requirements without threshold fitting or agent input. The reference identity, fidelity
label, unavailable rules, and any human-approved proxies are frozen before experiments begin.

If `N.NEWNESS`, `I.SPONSORSHIP`, `E.PROPER_BASE`, or another required blocker remains unavailable or
unimplemented, diagnosis and counterfactual measurement may continue, but no result is
promotion-eligible. Promotion eligibility resumes only after the missing PIT evidence or
implementation is supplied, or after a human explicitly approves a versioned quantitative-proxy
rulebook amendment. Agents cannot make or imply that decision.

## Acceptance and promotion policy

Diagnosis acceptance means the system can reproduce and causally attribute the baseline. It does
not mean a strategy is profitable.

A candidate may be presented for human review only when:

1. every required observed CANSLIM rule passes;
2. every proxy and unavailable rule is explicitly listed in the fidelity label;
3. no required rule, PIT boundary, accounting path, or execution invariant changed;
4. discovery and validation workers are confined, offline, exit zero, and hash-bound;
5. validation total return, annualized return, Sharpe, and drawdown headroom are not worse than the
   fixed fidelity-eligible reference;
6. validation trade expectancy is positive;
7. completed-position evidence meets the predeclared floor of at least 60 discovery positions and
   at least 20 validation positions; a faithful, more selective strategy may have fewer trades than
   the reproduction baseline;
8. PIT-exposed leader recall is not worse;
9. at least one of return, Sharpe, expectancy, drawdown headroom, or leader recall strictly
   improves;
10. the proposal is unique and all artifacts reconcile;
11. a human explicitly selects it before locked evaluation is opened.

Average cash is diagnostic, not an independent reason to promote. Deploying more cash by weakening
entry quality or M cannot pass.

The loop never applies, commits, pushes, merges, schedules, paper-trades, or live-trades a candidate.
Those remain separate human-authorized workflows.

## Failure handling

- Missing, stale, malformed, non-finite, or hash-mismatched evidence fails closed.
- Missing rule evidence is `unavailable`, never a pass or neutral score.
- A baseline reproduction failure stops before agent or experiment execution.
- A diagnostic-only experiment cannot be converted into an exportable proposal.
- A reasoner rule citation or evidence citation outside the controller payload is rejected.
- An orchestrator domain outside the enum is rejected.
- A coder edit outside the exact approved experiment is rejected.
- Any lookahead, off-calendar evaluation, same-session open use, or locked-evaluation feedback is a
  terminal research-generation failure.
- Interrupted experiments resume only from an identity-matched checkpoint and never reprocess
  completed sessions.

## Verification strategy

Implementation verification must include:

- rulebook schema, citation, classification, and immutable-hash tests;
- explicit rejection of required-rule weakening;
- exact canonical-baseline reproduction;
- synthetic point-in-time and future-publication adversarial tests;
- exact next-session open and 5% buy-zone boundaries;
- discovery/validation/locked-evaluation isolation tests;
- no ex-post leader-label input tests;
- one-factor experiment enforcement and pairwise-controller authority tests;
- experiment checkpoint/resume and stale-identity rejection;
- empty, zero-denominator, missing-data, non-finite, duplicate, and off-calendar artifacts;
- closed PIT route/reasoner/coder protocol tests;
- proof that config-only experiments do not call the coder;
- agent-loop source, worker, credential, network, and paper/live immutability tests;
- full offline pytest, Ruff, compilation, CLI help, and `git diff --check` gates.

No verification step may contact OpenRouter. A paid canary is a later operational check requiring
separate authorization.

## Rollout sequence

1. Implement and verify the deterministic rulebook and diagnosis harness without any agent calls.
2. Publish the baseline attribution report and review the declared fidelity gaps.
3. Add the `pit_diagnosis` controller gate with fake-gateway tests only.
4. Run all deterministic ablations and select the first evidence-supported code experiment.
5. Obtain separate authorization for one inert paid canary on the same source and evidence hashes.
6. Review the route, cited rules/evidence, causal hypothesis, optional patch, private evaluation,
   accounting, cleanup, and source immutability.
7. Authorize a bounded proposal batch only if the canary is actionable.
8. Human-select at most one fidelity-eligible candidate for locked evaluation.
9. Acquire and seal a new post-2025 PIT period before making any production-promotion claim.
10. Treat any real-source application as a new, separately approved implementation task.

## Acceptance criteria for this design

- The corrected PIT baseline is the immutable starting point.
- CANSLIM fidelity has precedence over performance.
- Current fidelity gaps are reported rather than silently neutralized.
- The deterministic harness, not an LLM, computes every result.
- The orchestrator routes and does not reason.
- The reasoner can select only controller-owned experiments and local rulebook facts.
- The coder is skipped when no code is required.
- The existing test and technical-only backtest gates retain their behavior.
- 2025 is a locked, already-observed evaluation period and is never claimed as an unseen holdout.
- Production promotion requires a genuinely unseen post-2025 PIT period.
- No source application or trading path is added.
