# Model-Authored PIT Strategy Optimizer Design

**Date:** 2026-08-27
**Status:** Approved for implementation on 2026-08-27

## Objective

Replace the current one-iteration, controller-catalog PIT experiment with a genuine
evidence-guided optimization loop. DeepSeek R1 may investigate aggregate replay evidence,
choose whether to work on entry, exit, or risk/sizing behavior, and author bounded strategy
code. A local controller remains the sole authority for source isolation, causal invariants,
evaluation, accounting, incumbent selection, hidden validation, and promotion.

The optimizer's purpose is to discover strategy changes that improve simulated return while
remaining point-in-time correct and inside fixed exposure limits. It must learn from failed
hypotheses rather than treating every non-improving candidate as a terminal error. It must
never commit, merge, push, deploy, or modify the operator's working tree.

## Why the existing cycle must change

The existing PIT optimization cycle is intentionally not a general optimizer. It permits one
iteration, exactly three calls, and one controller-owned single-line alternative selected from
a twelve-item entry-threshold catalog. The coder reproduces a known replacement rather than
authoring strategy code. That design proved provider accounting, sandboxing, sealed-data
evaluation, and inert candidate handling, but it cannot provide cumulative feedback-driven
strategy improvement.

The new design preserves those proven trust boundaries while replacing the closed catalog and
single iteration with a model-authored incumbent loop.

## Decisions

- The configured OpenRouter model is DeepSeek R1 for all three roles.
- Each complete iteration has exactly three role calls: investigator, author, and critic.
- R1 may choose entry, exit, or risk/sizing work from the supplied diagnostics.
- R1 authors cohesive strategy code, not merely parameter selections.
- Candidates build on the best discovery incumbent, not always on the original baseline.
- Candidate ranking is lexicographic; there is no weighted composite fitness function.
- Discovery failures become feedback. Provider, audit, data-identity, and sandbox-integrity
  failures stop the run.
- Hidden validation is evaluated once after the discovery incumbent is frozen and is never
  returned to R1.
- A successful result is an inert patch and local evidence package for human review.
- All artifacts and the repository remain local and are not uploaded to GitHub or Codex Cloud.
  Each separately authorized live role call necessarily transmits its bounded prompt, including
  selected proprietary policy source, to OpenRouter.

## Goals

1. Let R1 inspect relevant strategy code and aggregate replay evidence, form a causal
   hypothesis, author a bounded patch, and reason about the observed result.
2. Carry critic feedback and rejected hypotheses into later iterations.
3. Permit meaningful structural changes to entry, exit, and risk/sizing policy without giving
   model-authored code authority over data, timing, accounting, evaluation, or infrastructure.
4. Rank candidates with a small, deterministic objective that primarily rewards simulated
   return.
5. Prove the complete feedback path with a small subset canary before any long replay.
6. Preserve exact call, token, and cost accounting with zero provider retries.

## Non-goals

- Automatic commits, merges, pushes, deployments, or live/paper-trading activation.
- Model access to raw PIT datasets, individual holdings, credentials, or hidden validation.
- Model changes to the replay evaluator, performance metrics, fold construction, audit ledger,
  sandbox, provider client, or optimizer controller.
- An unconstrained repository-editing agent.
- An exhaustive grid search over every numeric setting.
- Treating a profitable subset result as production evidence.

## Architecture

### Controller-owned orchestration

One local controller owns the complete run state:

- authenticated baseline and source identities;
- discovery-fold and hidden-validation definitions;
- provider call, token, and cost ceilings;
- immutable safety constraints;
- current incumbent and rejected-candidate history;
- candidate workspace creation and cleanup;
- diff validation and sandbox execution;
- deterministic metric computation and candidate ranking;
- audit records and terminal status.

R1 receives evidence and produces advisory artifacts. It cannot select its own data, run code,
alter the objective, accept a candidate, or promote a result.

### Pure strategy-policy boundary

The existing simulator mixes strategy decisions with protected execution and accounting. Before
model-authored candidates are enabled, implementation extracts the editable decisions into a
small pure-policy boundary while preserving current behavior:

- entry qualification and candidate ordering;
- position-risk and cash-allocation recommendations;
- capacity eviction selection;
- staged profit-taking, stagnation, breakeven, EMA, and early-winner exit decisions.

The simulator supplies causally available immutable inputs to these functions, validates their
outputs against hard constraints, and remains responsible for fills, cash, quantities,
mark-to-market equity, transaction recording, and next-session execution. The extraction must
pass a baseline-parity replay before any provider call.

Fact acquisition stays protected. The model-editable policy receives only controller-built
immutable scalar/dataclass snapshots for the current decision: CANSLIM component scores and
entry facts, the current market state, current-session price facts, open-position state, and
portfolio capacity. It never receives a bundle handle, provider object, filesystem path, full
DataFrame, unsliced history, evaluator callback, or validation-fold state. History slicing,
proper-base detection, component fact calculation, dates, and membership remain trusted engine
responsibilities. A later request to optimize feature construction would require a separate
design and validation boundary.

### Model-editable surface

Candidate changes may affect:

- how existing CANSLIM scores and causally constructed entry facts are combined and gated;
- how existing proper-base and market facts influence entry permission and ranking;
- entry ranking and market-permission policy;
- exit sequencing and rules;
- risk-based allocation and capacity/eviction policy inside fixed ceilings.

Candidates edit only controller-declared strategy paths and symbols. The preferred target is the
new pure strategy-policy package. Fact construction, `core/canslim/` data access, the simulator,
and execution/accounting code are not candidate-editable.

Each candidate is limited to:

- at most three files;
- at most 200 added or removed lines;
- at most 64 KiB of unified diff text;
- no binary changes, renames, symlinks, submodules, generated files, or test edits;
- no new external dependency, network, process, environment, filesystem, or dynamic-code I/O.

The controller may lower these bounds for a specific run. R1 cannot raise them.

Both the incremental incumbent-to-candidate diff and the cumulative original-baseline-to-
candidate diff must satisfy the same bounds on every iteration. The controller derives the
cumulative diff itself and rejects gradual scope expansion. A run may contain at most eight
complete iterations, so an incumbent has at most eight accepted generations even when a larger
provider budget is available.

## Immutable constraints

The following remain controller- or engine-owned even when R1 changes risk logic:

- completed-session-only signal facts;
- exact next-session-open execution;
- next-open buy-zone revalidation;
- sealed PIT membership, fundamentals, prices, and identity transitions;
- cash-only long positions and no leverage;
- gross exposure no greater than portfolio equity;
- position risk no greater than 1% of portfolio equity;
- stop-loss distance no greater than 8%;
- every requested allocation validated against the cash and exposure ceilings before execution;
- deterministic portfolio accounting and performance metrics;
- discovery/validation dates and symbol universe;
- provider, audit, sandbox, optimizer, and promotion code.

R1 may recommend or implement tighter risk, different capacity, or conditional allocation within
these ceilings. Static violations are rejected before replay; any invalid runtime decision is
rejected before it can mutate portfolio state.

## Candidate identity and policy attestation

The existing effective-policy digest cannot by itself describe arbitrary structural source
edits. Model-authored runs therefore add a controller-derived candidate identity containing:

- original authenticated source commit;
- pure-policy interface/schema version;
- cumulative unified-diff digest;
- final hashes of every editable policy file;
- exact changed paths and Python symbols derived from the parsed diff and AST;
- immutable-constraint digest;
- discovery-fold manifest digest.

The author's change manifest is advisory and must match this controller-derived identity. Replay
results bind to both the existing engine-policy digest and the new candidate identity. This
attests the exact executable policy and permitted code delta; it does not claim that a digest can
infer the semantic meaning of arbitrary Python.

Before the subset canary, the extraction from the existing simulator into the pure-policy package
is checked without a provider on the two discovery folds. Canonical transactions, entry outcomes,
fold equity, funnel counts, and effective baseline policy must match exactly. This subset parity
is sufficient to start the subset canary. A corresponding provider-free full-window parity replay
is required only before a full-scale optimizer or long replay, so the architectural canary does
not quietly trigger the expensive run it is intended to precede.

## Optimization objective

There is no weighted robust score and no model-editable scoring coefficient. For each discovery
fold, the controller computes candidate excess total return relative to the authenticated fixed
baseline for that fold. It then creates this lexicographic ordering tuple:

1. median discovery-fold excess total return, higher is better;
2. worst discovery-fold excess total return, higher is better;
3. maximum drawdown magnitude across discovery folds, lower is better.

For the two-fold canary, the median is the arithmetic midpoint of the two fold excess returns;
for more folds, it is the conventional middle value or midpoint of the two middle values. The
controller computes the tuple, then uses decimal `ROUND_HALF_EVEN` quantization to 0.01 percentage
point on each component before comparison so floating-point dust cannot replace an incumbent. A
candidate must close at least one trade in every discovery fold before it can be ranked. It
replaces the current incumbent only when its quantized tuple is strictly better and all integrity
and safety constraints pass. This ordering deliberately permits a material median-return gain to
trade off against a weaker worst fold; the latter remains the second ordering key and is always
reported rather than hidden in a weighted score.

Sharpe ratio, trade count, turnover, exposure, and exit attribution remain diagnostics supplied
to R1; they are not independent performance gates during discovery.

The original baseline remains the fixed reference for every candidate. The incumbent is simply
the best strictly ordered candidate observed so far.

A run must declare at least two non-overlapping chronological discovery folds and one strictly
later hidden-validation fold. Their symbol universe, warmup convention, session boundaries, and
baseline identities are sealed before the first paid call.

The first subset canary reclassifies both previously observed 60-session verification windows as
discovery data: 2021-06-25 through 2021-09-20 and 2021-09-21 through 2021-12-14. Its hidden fold is
the immediately following 60 benchmark sessions, 2021-12-15 through 2022-03-11, over the same
existing 25-symbol verification universe. That choice is chronological and does not depend on a
candidate outcome. The hidden dates and baseline slice stay out of all provider prompts.

Every fold is an independent simulation: it starts with the same normalized initial capital,
empty positions, no pending entries, and no performance carryover. Trusted causal prehistory may
warm indicators through the session immediately before the fold, but cannot create trades or
equity before the fold start. Positions still open on the final fold session are liquidated by
the existing deterministic end-of-test rule, and signals lacking an in-fold next session cannot
execute. Baseline and candidate use identical fold semantics.

A local append-only `pit_optimizer_validation_ledger.jsonl` in the configured private runtime
root identifies a holdout only by immutable data identity, symbol universe, warmup, and exact
session boundaries. Source commit, baseline policy, and candidate identity are stored as metadata
but never participate in uniqueness, so changing code cannot make a consumed data window appear
fresh. The controller reserves the data-window identity atomically before hidden evaluation and
permanently marks it consumed even if evaluation later fails. A window previously used for
candidate validation or exposed to a provider cannot become hidden again; it may only become
discovery data in a new experiment with a later unconsumed holdout. Later full-scale optimizer
runs may declare more discovery folds without changing the objective.

## Roles and response contracts

All three calls use the configured DeepSeek R1 model but have separate closed JSON schemas.

### Investigator

The investigator receives the allowlisted source bundle, CANSLIM rule summary, discovery
diagnostics, incumbent summary, and prior iteration history. It returns:

- `hypothesis_id`;
- one family: `entry`, `exit`, or `risk_sizing`;
- cited evidence identifiers;
- causal rationale;
- target paths and symbols;
- expected diagnostic changes;
- known risks;
- bounded instructions for the author.

The investigator chooses the family from evidence. The controller does not impose an
entry-first sequence.

### Author

The author receives the investigator artifact, relevant allowlisted source, immutable
constraints, and candidate bounds. It returns:

- the same `hypothesis_id`;
- a concise behavioral summary;
- exact changed paths and symbols;
- one unified diff;
- assumptions;
- focused validation suggestions.

The controller, not the author, parses, validates, and applies the diff to a disposable copy of
the incumbent.

### Critic

After local candidate validation and discovery replay, the critic receives the hypothesis,
change manifest, sanitized validation outcome, and aggregate candidate-versus-baseline and
candidate-versus-incumbent evidence. It returns:

- the same `hypothesis_id`;
- prediction-versus-observation analysis;
- causal explanation;
- cited evidence identifiers;
- one advisory disposition: `refine`, `abandon`, or `change_family`;
- one bounded next-direction recommendation.

The critic cannot accept a candidate. The deterministic controller updates the incumbent after
the critic artifact is safely persisted.

## Information boundary

OpenRouter receives only the material needed for the three roles:

- whitelisted strategy source or bounded excerpts;
- the strategy rule summary and immutable constraint identifiers;
- aggregate discovery metrics;
- aggregate entry-funnel, exit-attribution, exposure, and trade statistics;
- sanitized validation failure codes;
- prior hypotheses, patch summaries, and critic conclusions;
- current incumbent source/diff context required to author the next patch.

OpenRouter does not receive:

- credentials, environment variables, provider headers, or secret-bearing files;
- raw market/fundamental datasets or sealed bundle contents;
- individual holdings or raw trade rows;
- hidden-validation dates, inputs, metrics, or outcomes;
- unrelated repository source;
- raw provider-audit internals.

The live canary therefore sends bounded proprietary strategy source and aggregate optimizer
context to OpenRouter. It does not upload the repository or dataset as a whole.

Approval of this architecture does not launch or authorize a provider run. Before a live canary,
the operator must explicitly authorize the bounded policy-source transmission and state fresh
cumulative call, token, and USD ceilings for that run. Those ceilings are jointly enforced; a
larger call allowance does not expand source scope, retries, tokens, or cost.

The sealed run manifest must also assign explicit input-token, output-token, and USD caps to each
of the investigator, author, and critic calls. A manifest with a missing or nonpositive role cap
is invalid, and the sum of all planned per-call caps must fit inside the operator's cumulative
ceilings. Before one call, the ledger reserves only that call's sealed maxima. After authoritative
provider accounting arrives, the reservation is released and replaced with actual usage before
the next call can be reserved.

Every prompt section and response field has a closed size limit. Later investigators receive all
bounded hypothesis/result/critic summaries from the current run, but only the current incumbent
source and cumulative diff; rejected full source trees and raw provider responses are not copied
forward. The controller refuses the next call if the resulting payload would exceed the run's
token ceiling rather than silently dropping feedback.

## Iteration data flow

### Readiness

Before a paid call, the controller:

1. authenticates the source commit, PIT bundle, baseline, folds, and effective policy;
2. verifies the strategy-policy extraction is behaviorally identical to the baseline;
3. verifies at least two chronological discovery folds and one strictly later hidden-validation
   fold are sealed under one symbol-universe and warmup contract;
4. calculates the maximum complete iterations as `floor(authorized_calls / 3)`;
5. refuses a feedback-loop canary when fewer than six calls are available;
6. verifies the cumulative token and cost ceilings can cover the configured run;
7. creates an atomic local run directory and provider ledger;
8. evaluates and caches the fixed baseline discovery folds.

Any call-budget remainder below three remains unused.

Authorization is evaluated against both the cumulative operator allowance and the proposed
per-run ceiling; the effective call/token/USD limit is the most restrictive remaining value.
Every call reserves its sealed per-call token and USD maxima before transmission and closes with
actual accounting afterward. As of this design review, the latest reconciled current pool records
only one unused call, so the six-call live canary is blocked pending a fresh explicit
authorization. Implementation and provider-free verification may proceed without that
authorization.

### One iteration

For iteration `N`:

1. Create a fresh disposable candidate from the authenticated incumbent.
2. Reserve and perform the investigator call.
3. Validate and persist the investigator artifact.
4. Reserve and perform the author call.
5. Validate the author schema and unified diff.
6. Apply the diff only to the disposable candidate.
7. Run fast candidate checks and, if valid, the discovery-fold replays.
8. Build a sanitized result package.
9. Reserve and perform the critic call.
10. Validate and persist the critic artifact.
11. Apply the fixed lexicographic comparison.
12. Retain the candidate as the new in-memory incumbent or discard it while preserving its
    evidence.

Rejected hypotheses and critic feedback are included in later investigator context. Candidate
source never touches the operator's worktree.

After every nonterminal author outcome, the critic call is mandatory, including a safely rejected
diff, syntax failure, candidate exception, timeout, or underperforming replay. A malformed or
unauthenticated provider response is different: it is a terminal integrity failure and leaves a
partial iteration. Thus every complete iteration has three calls, while a terminal provider
failure may stop after one or two conservatively counted calls.

### Stop conditions

The discovery loop stops on the first applicable condition:

- configured complete-iteration limit;
- call, token, or cost budget exhaustion;
- three valid evaluated iterations without incumbent improvement;
- user cancellation;
- terminal integrity failure.

Invalid patches and candidate execution failures consume their already-issued calls but do not
count as valid evaluated iterations.

## Hidden validation and promotion boundary

After discovery stops, the controller freezes the best incumbent. If no candidate beat the
baseline, it reports no discovery winner and does not open hidden validation.

For a discovery winner, the controller first repeats one discovery fold to confirm deterministic
results. It then opens one atomic hidden-validation event: mark the fold identity consumed, run
the fixed baseline from an independent reset, and run the frozen candidate from the same reset.
The candidate is therefore evaluated on the hidden fold exactly once, and the baseline and
candidate use identical fold semantics. No subsequent R1 call may receive either holdout result.

A subset candidate becomes `long_replay_eligible` only when:

- it remains point-in-time and deterministic;
- its holdout total return exceeds the fixed holdout baseline by at least 0.10 percentage point
  after the same 0.01-point quantization used in discovery;
- it closes at least three holdout trades, including deterministic end-of-fold liquidation;
- it remains inside the immutable exposure and stop-loss ceilings;
- the evidence and accounting packages close successfully.

There is no separate Sharpe threshold. Holdout drawdown, Sharpe, trades, turnover, and exposure
are reported for judgment but are not converted into a weighted gate.

Every terminal outcome produces an inert cumulative patch when one exists, plus rationale and an
evidence package for human review. `long_replay_eligible` is not acceptance or promotion; it only
permits the separate long replay to be proposed. It never causes an automatic source edit or git
operation. The long replay remains blocked until the subset loop passes both the architectural
and performance conditions. Full replay and any eventual source application remain separate
human-approved actions.

## Failure handling

### Feedback failures

The following leave the incumbent unchanged, continue to the mandatory critic call, and may then
continue to the next iteration:

- disallowed, oversized, malformed, no-op, or non-applicable candidate diff;
- syntax, import, or focused strategy-check failure;
- policy-purity or repeated-identical-snapshot failure;
- candidate exception or timeout inside the confined replay;
- no trades, worse performance, or a contradicted hypothesis;
- author assumptions not supported by available inputs.

Only bounded failure codes and diagnostics are sent to the critic. A protected-path edit is
rejected before execution and treated as untrusted candidate output, not as authorization to
expand the scope.

### Terminal integrity failures

The run stops immediately on:

- malformed or unauthenticated provider response;
- uncertain, absent, or inconsistent provider accounting;
- failure to persist the audit trail atomically;
- source, bundle, baseline, fold, or effective-policy identity drift;
- trusted baseline/evaluator nondeterminism under identical authenticated inputs after the
  candidate worker is excluded;
- sandbox escape, protected host mutation, or evidence tampering;
- any next call that would exceed an authorized ceiling.

Provider requests have zero automatic retries. An uncertain call is counted conservatively. A
new iteration cannot be used to disguise a retry of a failed provider/protocol operation.

## Sandbox and audit

Candidate code executes as an untrusted, stateless policy worker outside the trusted evaluator.
For each decision, the evaluator streams one closed-schema causal snapshot to the worker and
receives one closed-schema policy decision. The worker never imports the evaluator and cannot
request another date, symbol, history range, or fold. Every discovery evaluation uses a fresh
worker. After the incumbent is frozen, hidden validation uses another separate fresh worker.

Statelessness is enforced rather than trusted. Candidate-editable modules may contain pure
functions plus immutable scalar/tuple constants only. The AST validator rejects candidate
classes, mutable module objects, mutable default arguments, `global`/`nonlocal`, writes to input
objects, function attributes, reflection, clocks, and randomness. Inputs cross the worker boundary
by serialization, so the worker cannot mutate evaluator state. Each fold starts a fresh
interpreter, and validation repeats identical snapshots both before and after unrelated calls;
any different decision is a candidate validation failure supplied to the mandatory critic.

The disposable policy-worker container has:

- network disabled;
- a non-root user;
- no PIT bundle, baseline, hidden-fold, repository, or artifact mount;
- no credentials or host secret environment;
- candidate policy source mounted read-only and only a bounded temporary directory writable;
- closed stdin/stdout decision schemas with all other file descriptors closed;
- CPU, memory, process, and wall-time limits;
- per-decision and per-fold time limits;
- AST/diff rejection of reflection, dynamic code, filesystem, environment, process, and network
  access before execution;
- authenticated causal inputs and policy outputs;
- unconditional cleanup of transient execution state.

Every role call is reserved, attempted, and closed in the provider ledger before the next call.
Accounting includes call index, role, token usage, cost, status, and bounded evidence without
exposing credentials or provider response contents in user-facing reports.

The local evidence package contains:

```text
run.json
baseline.json
accounting.json
iterations/
  001/
    investigator.json
    author.json
    candidate.diff
    validation.json
    discovery.json
    critic.json
    decision.json
incumbent.diff
holdout.json              # only when hidden validation is opened
summary.json
```

Files are written atomically and use canonical closed schemas. Cleanup removes disposable
containers and candidate roots but preserves this local evidence package.

## Focused verification

Verification is intentionally narrow and reuses existing sandbox and replay coverage.

### Pre-provider checks

Add focused checks for only the new risks:

- strict role response schemas;
- incremental and cumulative strategy path, symbol, file-count, line-count, byte-count, and AST
  enforcement;
- causal snapshot/decision worker schemas and runtime constraint rejection;
- pure-policy baseline parity;
- lexicographic incumbent replacement;
- rejected-feedback persistence into the next investigator context;
- independent fold resets, hidden-validation blindness, and consumed-fold ledger behavior;
- exact three-call iteration and cumulative accounting behavior;
- one mocked two-iteration integration flow.

The full repository test suite does not run for every candidate. Candidate fast checks are limited
to allowlist inspection, syntax compilation, imports, and existing tests directly relevant to
the changed strategy symbols.

### Live subset canary

The first live proof uses the extended local 25-symbol subset fold contract defined above and
`apply=false`. It requires at least two complete iterations, so it consumes exactly six role
calls unless an integrity failure stops it earlier. This is the minimum run that can prove
first-iteration critic feedback reaches a later investigator.

Architectural success requires:

- all three roles produce valid structured artifacts;
- R1 selects a strategy family from aggregate evidence;
- R1 authors executable bounded strategy code;
- the controller evaluates or safely rejects the patch;
- the critic explains the observed result;
- the second investigator receives first-iteration feedback;
- incumbent selection behaves deterministically;
- call, token, cost, sandbox, and audit records close exactly.

Performance success separately requires a discovery winner and a `long_replay_eligible` untouched
holdout result within the immutable safety constraints. If architectural success passes but no
candidate qualifies, the result is `loop_verified_no_long_replay_candidate`, not an optimizer
failure.

## Implementation impact

The implementation plan should cover these focused areas:

1. Extract pure strategy-policy decisions and execute them through the causal snapshot worker,
   with provider-free subset parity.
2. Add closed investigator, author, and critic contracts for model-authored patches.
3. Add bounded source packaging, incremental and cumulative diff validation, and controller-
   derived candidate attestation.
4. Add independent fold resets, the new subset fold manifest, and the persistent consumed-
   validation ledger.
5. Replace the single-candidate path with persistent incumbent-loop state.
6. Add quantized lexicographic discovery comparison and final-only hidden validation.
7. Extend local artifacts and provider accounting for multiple iterations.
8. Add the minimal focused checks and mocked two-iteration flow.
9. Obtain fresh provider/source/budget authorization and run one six-call subset canary before
   considering the long replay.

The existing one-line catalog artifacts remain readable for audit history. New model-authored runs
use a new schema/version and do not reinterpret old records.

## Acceptance summary

The design is complete when the implementation can demonstrate a local, bounded, two-iteration
R1 feedback loop in which the model investigates, authors strategy code, receives replay
feedback, and adapts its next hypothesis; the controller alone enforces causality, risk,
accounting, validation, and promotion; and the final output remains an inert local patch for human
review.
