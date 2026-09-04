# PIT Optimizer V5 Hybrid Architecture Design

**Date:** 2026-09-04
**Status:** Approved for implementation planning

## Objective

Build a local-first, model-guided optimizer that can discover and independently validate an
adaptive O'Neil/CAN SLIM portfolio with double-digit annualized return as its first research
milestone. Later campaigns must be able to request 20% or 50% targets without changing the
architecture.

The milestone is an empirical research result, not a promise of future market returns. A target is
met only by an untouched, point-in-time, out-of-time and out-of-security evaluation using declared
execution costs. Discovery-panel improvement alone never establishes success.

## Why V4 Is Being Replaced

V4 proved that the investigator, author, evaluator, and critic can complete authenticated loops,
but it does not learn or search efficiently enough:

- the critic's causal explanation and next direction are not carried into the next investigation;
- one three-role iteration produces only one source candidate;
- search retains one champion and one replaceable branch;
- a fixed discovery panel is reused until it behaves like training data;
- promotion uses one endpoint CAGR and can accept behaviorally inert or zero-activity changes;
- aggregate evidence does not explain cash drag, trade expectancy, missed winners, regime effects,
  or exit opportunity cost;
- the candidate can edit only three narrow policies whose snapshots omit important causal O'Neil
  and portfolio facts;
- PIT mode disables industry leadership and the engine cannot add to an existing winner;
- the current bundle is S&P-only, while full qualification requires S&P 500, Nasdaq-100, and
  Russell 2000 coverage;
- the optimizer evaluator omits authenticated ticker-identity transitions and uses optimistic exit
  and stop-fill semantics; and
- the orchestration, contracts, provider, Git, sandbox, evaluation, and search state are coupled in
  several very large legacy modules.

## Design Principles

### Search freedom and evaluator authority are separate

The model may explore material strategy changes. Causality, accounting, data identity, fill
semantics, and artifact integrity remain trusted local responsibilities. These invariants prevent
the optimizer from exploiting the simulator; they do not prescribe trading thresholds.

### CAGR remains the primary objective

The primary selection value is absolute annualized portfolio return. V5 does not introduce an
opaque weighted robustness score. Drawdown, activity, exposure, turnover, costs, and regime slices
are explanatory evidence. They may reject only an invalid or untestable result, such as a
  behaviorally identical candidate, a zero-activity complete discovery campaign that merely beats a negative baseline,
non-causal execution, or broken accounting.

### The model chooses mechanisms; local search explores nearby values

The investigator selects causal hypotheses. The author writes the exact strategy structure and
may declare bounded tunable constants. The trusted local variant generator creates several
provider-free variants around that structure. Full-source authoring remains available as an escape
hatch for structural changes, but unchanged files are not retransmitted by default.

### Learning is durable and structured

Every experiment records its parent, semantic behavior, hypothesis, predictions, source operation,
validation result, quick and discovery evidence, critic explanation, and next direction. This
record survives checkpoints and campaign restarts. Model prose supplements measured facts; it does
not replace them.

### Discovery is adaptive; qualification is untouched

Discovery uses multiple chronological market episodes and security cohorts. The primary discovery
value is a transparent CAGR compounded from the declared, non-overlapping episode returns. The
model receives each episode's diagnostics so it can learn regime-dependent behavior. A separate
qualification schedule remains sealed and is never used for iteration feedback.

## Architecture

### 1. Truthful evaluation layer

V5 introduces a versioned execution profile without rewriting historical V2-V4 artifacts.

The profile must:

- inject the bundle's authenticated price-identity transition contract into every simulation;
- execute decisions based on a completed close no earlier than the next eligible session open;
- fill a long protective stop at the opening price when the market gaps below the stop, otherwise
  at the stop when the session trades through it;
- distinguish intraday price-triggered scale-outs from close-derived policy exits;
- apply separately declared commission, half-spread, and market-impact/slippage costs to every fill;
- retain exact no-leverage and cash accounting; and
- seal the execution-profile identity into baseline, candidate, panel, and checkpoint artifacts.

Every portfolio evaluation runs through one authenticated, digest-pinned sandbox profile. Candidate
networking is absent; the root filesystem, source, and data are read-only; CPU, memory, runtime, and
bounded output limits are explicit; and the sandbox identity is carried by evaluator evidence. A
production campaign cannot silently fall back to an in-process or unbounded evaluator.

One new V5 baseline authority is captured from this profile. Earlier positive and corrected
baseline authorities remain readable history but cannot be mixed with V5 comparisons.

### 2. Causal evaluation report

`EvaluationReportV5` contains the primary CAGR plus enough evidence to explain it:

- total and benchmark return, drawdown, Sharpe, exposure, cash, turnover, and closed trades;
- gross and net return under the declared cost profile;
- win rate, average win, average loss, payoff ratio, expectancy, and holding duration;
- maximum favorable and adverse excursion;
- entry funnel by stage, market regime, episode, and source universe;
- invested-sleeve return and estimated idle-cash drag;
- exit attribution and foregone return after scale-outs or policy exits;
- capacity, no-cash, next-open, liquidity, and already-held outcomes; and
- calendar-year and rolling 12-, 24-, and 36-month returns when the panel is long enough.

The report contains aggregate or symbol-neutral evidence for provider roles. Raw market rows,
qualification membership, credentials, local paths, and provider response content remain local.

### 3. Pure search domain

The pure-domain modules inside `core/pit_optimizer_v5/` own infrastructure-free search types and
rules:

- `HypothesisV5`, `StructuralTemplateV5`, and `RenderedVariantV5`;
- `SemanticFingerprintV5` and typed probe outcomes;
- `CampaignEvidenceV5` and `ExperimentRecordV5`;
- `CandidateArchiveV5` with a configurable top-K and one best candidate per behavior family;
- `SearchStateV5`, deterministic search scheduling, and manifest-bound termination; and
- typed commands and events consumed by external adapters.

Those pure modules must not import the CLI, OpenRouter gateway, Git implementation, Docker runner,
or artifact filesystem implementation. Infrastructure adapters may live in sibling V5 modules with
one-way dependencies on the pure domain.

### 4. Candidate authoring and local variants

The normal author response contains only changed functions or module constants, their parent
identity, and optional tunable axes. A deterministic renderer applies those operations to the
parent and emits the complete strategy source bundle locally.

Before a portfolio run, the validator must:

- parse and compile the rendered policy;
- validate allowed imports and deterministic/pure behavior;
- require a non-empty changed-symbol set;
- execute a fixed vector of policy snapshots against parent and candidate;
- reject identical decision traces; and
- derive a semantic fingerprint from canonical decision traces.

The local variant generator substitutes only author-declared literal constants or enumerated
choices. It never rewrites arbitrary source or invents new strategy semantics.

### 5. Search and selection

One model hypothesis may yield several local candidates. Candidates progress through:

1. source and semantic validation;
2. deterministic behavior probes;
3. a small quick panel;
4. chronological discovery episodes for survivors; and
5. archive insertion and critic review.

Champion selection is lexicographic and transparent:

1. valid causal/accounting result;
2. behaviorally distinct and non-zero activity;
3. highest declared discovery campaign CAGR.

Other metrics do not become a hidden weighted objective. The archive also retains the strongest
candidate from distinct hypothesis families so exploration is not erased by one incumbent.

### 6. Durable feedback

`ExperimentRecordV5` carries forward:

- the investigator hypothesis, mechanism, prediction, evidence IDs, and author instructions;
- parent and candidate semantic fingerprints and changed symbols;
- validation or typed failure result;
- quick and per-episode discovery deltas;
- target gap;
- critic prediction-versus-observation, causal explanation, evidence IDs, disposition, and next
  direction; and
- a novelty key used to avoid repeating equivalent experiments.

Checkpoints restore the archive and compacted experiment memory, not just source candidates. Old
records are compacted by semantic family while preserving measured outcomes and critic direction.

### 7. Provider boundary

The provider adapter owns strict role schemas, evidence-ID grounding, transport accounting, and
typed failures. Provider retries and schema-repair calls are explicit run-manifest capabilities,
separate from search iterations, and default to disabled unless the operator authorizes them.

No model name, call count, token ceiling, or USD ceiling is hard-coded into the search engine. A
run manifest supplies those limits. `apply=false` remains mandatory throughout discovery.

The manifest is also the root of a resolvable authenticated dependency graph. Each evaluator,
baseline, panel, policy, sandbox, source, and stage dependency is a canonical relative artifact
reference with its digest, not a digest that requires a hidden path lookup. Commands authenticate
referenced bytes before parsing and fail closed on a missing, relocated, or mismatched artifact.

### 8. Adaptive O'Neil policy interface

Policy interface V3 retains the O'Neil identity while increasing usable causal information.

Entry and ranking add:

- source-universe affiliations;
- causal industry-group and sector relative strength;
- earnings and sales growth acceleration and data freshness;
- distance from highs, ATR fraction, breakout gap, and volume/dollar-volume context; and
- existing base, pivot, buy-zone, CAN SLIM, RS, and market context.

Allocation and capacity add:

- candidate ATR and liquidity;
- current gross exposure, portfolio drawdown, open risk, and market regime;
- sector and industry exposure; and
- pending-entry competition.

Holding management adds refreshed RS, group leadership, ATR, volume behavior, unrealized return,
MFE/MAE, holding age, and position concentration.

An explicit add-on decision allows O'Neil-style pyramiding into an existing winner while the
trusted engine enforces cash, no-leverage, total-position risk, and maximum aggregate position
size. The optimizer may also change scale-out and winner-retention logic using measured
opportunity-cost evidence.

### 9. Three-universe PIT bundle

Bundle schema V3 represents a security lineage separately from one or more dated affiliations:
`sp500`, `nasdaq100`, and `russell2000`. Overlaps are explicit and never duplicated in a panel.
SPY, QQQ, and IWM remain non-tradable reference series.

The build accepts only locally supplied, authenticated historical membership sources. It seals
provenance, identity transitions, delistings, corporate actions, prices, fundamentals, and group
classification. Market data and artifacts remain ignored local files and are never uploaded to
GitHub.

### 10. Staged campaign

The V5 campaign proceeds in increasing cost:

- deterministic policy probes;
- tiny mechanics panel;
- quick portfolio screen;
- rotating chronological discovery episodes;
- one frozen, candidate-bound, permanently retired medium-panel confirmation;
- one-use out-of-time and out-of-security three-universe qualification; and
- full replay only after qualification.

Ten feedback rounds should evaluate dozens of behaviorally distinct local candidates rather than
ten model source bundles. A subset can prove mechanics and improvement, but only locked
qualification can establish the 10% research milestone.

## Module Boundaries

New focused modules live under `core/pit_optimizer_v5/`. Existing V2-V4 modules remain compatible
readers and adapters during migration. `agent_loop.py` becomes a composition root that wires the
provider, artifact repository, campaign-owned disposable workspace, and digest-pinned Docker
evaluator implementations into the pure V5 controller.

V5 does not delete or reinterpret legacy manifests, ledgers, checkpoints, candidates, or holdout
results. It uses new schema versions and new artifact roots.

## Verification Philosophy

Verification is narrow and evidence-oriented. Each boundary receives a small deterministic contract
check, followed by provider-free subset evidence. The implementation does not add another broad
legacy regression suite or delay progress to maximize test count.

No OpenRouter call, qualification opening, or full replay is part of implementation tasks unless a
later task explicitly receives the necessary operator authorization.

## Definition of Success

The architecture is ready for an optimization campaign when:

- V5 baseline and candidate evaluations use the same authenticated execution profile;
- all ticker identity transitions and fills reconcile;
- a subsequent investigator can reproduce the previous critic's full next direction from restored
  experiment memory;
- behaviorally identical source candidates are rejected before portfolio evaluation;
- one hypothesis can produce and evaluate multiple local variants;
- the archive preserves several distinct strategy families;
- discovery reports explain return, activity, cash drag, and winner opportunity cost by episode;
- targets such as 10%, 20%, and 50% are manifest configuration rather than production constants;
- the full point-in-time three-universe pool validates locally; and
- a provider-free subset run completes end to end before any long replay.
