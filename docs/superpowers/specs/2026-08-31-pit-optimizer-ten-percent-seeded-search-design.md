# PIT Optimizer Ten-Percent Seeded Search Design

**Date:** 2026-08-31
**Status:** Design approved in chat; written specification ready for user review

## Objective

Find and independently verify a model-authored strategy candidate whose simulated total
return exceeds the authenticated fixed baseline by at least **10.00 percentage points** before
starting any full replay.

The optimizer must continue from its strongest discovery-selected local incumbent across
bounded runs instead of repeatedly restarting from baseline. DeepSeek R1 may receive the three
editable strategy-policy files, aggregate discovery feedback, the current discovery incumbent,
and the remaining distance to the target. It must not receive hidden-validation results, raw PIT
data, credentials, local paths, or provider audit material.

The full replay remains a separate, later action. Reaching the discovery target alone does not
authorize it.

## Evidence motivating the change

The current implementation completed ten end-to-end three-role loops and correctly carried an
incumbent inside each run. Across 64 locally evaluated historical candidates, however, the best
fixed-baseline discovery score was only +3.72 percentage points median excess return. The
current source always initializes a new run from the original baseline, so the best candidate
cannot become the authenticated starting incumbent for another bounded batch.

The existing discovery baseline is sparse: its two folds contain one and zero closed trades.
This made small entry changes look attractive and produced a repeatedly rediscovered candidate
that improved the discovery score by +1.70 points but made no trades on its holdout. The
strongest discovery candidate has materially better support, with 13 and 16 closed trades, and
is therefore the appropriate seed based on discovery evidence alone.

More unseeded calls would repeat explored mechanisms. Replacing the subset would sacrifice
comparability. The required change is an authenticated cross-run discovery seed plus an explicit
target supplied to the feedback loop.

## Success definition

All return comparisons use percentage-point differences, not a relative percentage divided by
the baseline. This avoids unstable ratios when a baseline fold is near zero.

### Discovery target

A candidate reaches the discovery target only when all of the following are true:

- its median excess total return across the two fixed discovery folds is at least +10.00 points;
- its weakest-fold excess total return is greater than 0.00 points;
- it is rankable under the existing deterministic discovery evaluator;
- its code, source identity, replay, determinism, accounting, and cleanup evidence are complete.

Closed-trade counts remain visible feedback rather than a new discovery ranking parameter. The
positive weakest-fold condition prevents an inactive or losing fold from being hidden by a
large result in the other fold.

### Pre-replay verification target

After discovery is frozen, one newly sealed provider-free holdout is opened exactly once. The
candidate passes only when:

- excess total return on that holdout is at least +10.00 points;
- at least three trades are closed;
- safety, integrity, accounting, determinism, and cleanup evidence are complete;
- the candidate was selected without holdout feedback reaching any optimizer role.

Only this result satisfies the goal. It makes the candidate eligible for a separately initiated
full replay; it does not start that replay automatically.

## Design decisions

- Use the strongest prior candidate selected by discovery score as the first seed.
- Authenticate the seed from local artifacts and re-evaluate it against the current source and
  evaluator before any provider call.
- Do not use, load, summarize, or transmit the seed's prior holdout result during discovery.
- Continue using exactly three roles per completed iteration: investigator, author, and critic.
- Run at most eight iterations per manifest. A later batch can authenticate the last closed
  discovery winner as its next seed.
- Keep each batch finitely bounded even though the operator authorized all OpenRouter calls.
- Preserve `apply=false`, zero provider retries, local-only artifacts, and disposable evaluation
  workspaces.
- Do not add tests for this change. Validate through existing checks, parity, preflight, live
  canaries, artifact audit, and provider-free evaluation.

## Architecture

### Authenticated discovery seed

The manifest gains an optional controller-owned discovery-seed requirement. It binds:

- the closed source run and summary identities;
- the source commit and editable-policy source identities used by that run;
- the selected candidate identity and cumulative-diff identity;
- the discovery-fold definition and fixed-baseline identity;
- the recorded discovery fold aggregates and score;
- the local artifact index proving the selected diff is durable.

The requirement contains identities and aggregate discovery evidence, never a path to be trusted
at runtime. Command-line paths are locator capabilities only; their contents must match the
manifest identities before they are consumed.

Preparation authenticates the closed run using the same fail-closed artifact rules used by the
holdout loader. It then applies the cumulative diff to a disposable checkout of the current
authenticated source. The diff must still:

- apply exactly and only to declared editable policy files and symbols;
- satisfy the current patch bounds, AST policy, import policy, and purity rules;
- produce the expected complete policy source bundle;
- leave the operator's checkout unchanged.

Because optimizer implementation commits can change the repository commit without changing the
three policy files, preparation mints a fresh candidate identity bound to the current clean
source. It does not silently preserve an obsolete source-commit identity.

Before any provider call, the trusted evaluator replays both discovery folds for the reconstructed
seed. The result must match the seed's authenticated aggregate evidence under the current parity
attestation. A mismatch stops preparation. A successful reconstruction becomes a fresh,
controller-authenticated seed readiness artifact.

### Seeded run initialization

`_RunState` is initialized from the authenticated seed when one is present:

- `incumbent_workspace` is the disposable reconstructed seed workspace;
- `incumbent_identity` is the freshly minted identity;
- `incumbent_cumulative_diff` is the authenticated cumulative diff;
- `incumbent_discovery` is the freshly repeated discovery evidence;
- provider iteration counters still begin at one;
- seed provenance is recorded separately from `incumbent_updates` produced by the new run.

Without a seed, initialization remains byte-for-byte equivalent to the current baseline path.
This keeps existing canaries and recovery behavior compatible.

Every new author candidate continues to build from `incumbent_cumulative_diff`. Incremental and
cumulative scope validation remains unchanged, so cross-run continuation does not expand the
editable surface or bypass the cumulative diff bound.

### Explicit optimization target

The manifest gains a closed `DiscoveryTarget` value with:

- metric: median fixed-baseline excess total return;
- threshold: +10.00 percentage points;
- weakest-fold floor: strictly greater than 0.00 points;
- comparison baseline: the authenticated original fixed baseline.

The controller derives a target-progress summary after every evaluation. The investigator sees
only:

- current incumbent discovery aggregates;
- current median and weakest-fold excess returns;
- the remaining median gap to +10.00 points;
- prior discovery-only iteration summaries;
- validation and critic feedback;
- the bounded editable source bundle and immutable constraints.

The author sees the approved investigator hypothesis and current incumbent source. The critic
sees local validation and aggregate discovery comparisons. No role receives a holdout metric,
holdout trade count, hidden fold definition, prior holdout disposition, or full-replay result.

The investigator prompt explicitly asks for a materially new causal mechanism that can close the
remaining gap. It may coordinate cumulative work across entry, risk sizing, and exit policy over
successive iterations. The controller still limits each author response to one targeted policy
file and derives all code metadata itself.

### Termination and batching

An iteration is always completed through the critic and durable decision record before a target
decision is made. After the decision, the controller checks the accepted incumbent against the
closed target.

Possible discovery terminal states include:

- `discovery_target_reached`: the incumbent satisfies both return thresholds;
- `iteration_limit`: the batch completed without reaching the target;
- the existing provider, accounting, context, identity, evaluator, sandbox, audit, or cleanup
  failures.

An `iteration_limit` result with a valid winner is not discarded. Once accounting is fully
reconciled and cleanup is complete, its winner can seed the next manifest. Aborted runs may seed
a later batch only through an already durable, safely recoverable discovery winner; incomplete
role attempts or uncommitted candidate state are never used.

Each batch uses at most eight iterations and therefore at most 24 role calls. New local grants
are recorded per batch against the user's standing authorization. There is no USD ceiling, but
call and token reservations remain finite so accounting can close deterministically.

### Untouched holdout construction

Before the first target-search provider call, preparation deterministically selects and seals a
new holdout definition from the authenticated PIT bundle. It must be disjoint from the two
discovery folds and from holdout windows previously opened during this optimizer campaign.
If the authenticated bundle cannot supply such a window with complete inputs, preparation stops
before any provider call rather than reusing a contaminated holdout.

The sealed definition is committed by identity in the target-search manifest but omitted from
all role inputs and discovery artifacts. The local holdout controller can resolve it only after
a target-reaching discovery summary, candidate identity, source identity, accounting closure,
and cleanup proof are authenticated.

The holdout controller reconstructs the final cumulative diff in a new disposable checkout and
runs the trusted evaluator in the existing network-disabled Docker sandbox. The result is
recorded locally and cannot feed another discovery batch. If the candidate fails, the next
search campaign must use a separately sealed holdout; the failed hidden result cannot become
role feedback.

## Data flow

1. Authenticate the clean source, PIT bundle, fixed discovery baseline, parity attestation, and
   strongest discovery-selected seed artifacts.
2. Reconstruct and re-evaluate the seed in a disposable, network-disabled worker.
3. Seal a fresh target-search manifest, finite role-call plan, and unopened holdout identity.
4. Record a local authorization grant for one bounded batch.
5. Provide the investigator with current discovery-only target progress and policy source.
6. Validate the author's full-source envelope and derive the bounded cumulative diff locally.
7. Replay both discovery folds, compute the fixed-baseline score, and send aggregate feedback to
   the critic.
8. Accept a candidate only when it lexicographically improves the current discovery incumbent.
9. Stop the batch when the +10.00-point target is reached or the finite iteration limit ends.
10. If needed, authenticate the closed winner as the seed for another bounded batch.
11. Once the discovery target is reached, open the newly sealed holdout exactly once.
12. Preserve the local evidence, remove disposable resources, and stop before full replay.

## Failure handling

The process fails closed before provider use when seed artifacts, source files, folds, parity,
candidate identity, cumulative diff, or reconstructed discovery evidence differ.

During a run:

- provider protocol or accounting failures stop the batch with zero automatic retries;
- invalid model-authored code becomes bounded discovery feedback when local validation can close
  safely;
- source, AST, purity, import, determinism, evaluator, sandbox, audit, or cleanup failures stop
  the batch;
- a stale or held source lock stops preparation;
- a partially accounted call prevents the winner from being reused until reconciliation closes;
- a previously opened or mismatched holdout identity prevents validation;
- no failure automatically starts, applies, commits, merges, pushes, or replays a candidate.

## Code changes

Implementation is expected to touch only the existing optimizer architecture:

- `core/pit_optimization_contract.py`: closed seed and target schemas, manifest binding, and
  target-progress role context;
- `core/pit_optimizer_evaluation.py`: seed authentication inputs, provider-free seed replay,
  fresh holdout sealing, and manifest construction;
- `core/pit_optimizer_candidate.py`: reusable authenticated cumulative-diff reconstruction and
  fresh candidate identity derivation;
- `core/pit_optimizer_controller.py`: seeded `_RunState`, target progress, target termination,
  and seed provenance in summaries;
- `core/pit_optimizer_holdout.py` and `pit_optimizer_holdout.py`: consume the newly sealed
  target holdout and apply the +10.00-point decision threshold;
- local ignored orchestration scripts under `.artifacts`: fresh manifest, grant, canary, and
  holdout commands.

No strategy-policy source is modified by the architecture implementation. Strategy edits remain
model-authored candidate artifacts under `apply=false`.

## Validation strategy

No new tests are added. Implementation validation uses:

- Python compilation and Ruff on touched optimizer modules;
- existing targeted optimizer checks where they already cover changed interfaces;
- current policy-parity replay proving unchanged baseline behavior;
- provider-free seed preflight proving exact reconstruction and repeated discovery aggregates;
- one live bounded canary proving all three roles receive target-aware cumulative feedback;
- audit inspection proving reservations equal reconciliations and every lease closes;
- source, process, container, and cleanup inspection after each batch;
- one provider-free sealed holdout only after the discovery target is reached.

Passing a narrow check is not evidence of the 10% goal. The authoritative completion evidence is
the final candidate's fixed-baseline discovery comparison plus its newly sealed holdout decision,
both bound to the same candidate identity and showing at least +10.00 percentage points.

## Privacy and operational boundaries

- All PIT data, manifests, diffs, ledgers, and replay artifacts stay local.
- Only the bounded role context and the three editable policy files may be sent to OpenRouter.
- Credentials and provider response bodies are never included in reports.
- The repository working tree remains unchanged by candidates.
- No GitHub push, cloud task, or full replay occurs as part of this design.

## Completion and handoff

The implementation phase is complete only when the seeded optimizer can run multiple bounded
batches without losing its incumbent or leaking holdout feedback. The active goal is complete
only when one candidate proves at least +10.00 points on both the closed discovery target and the
new provider-free holdout.

At that point the controller preserves the qualified candidate and evidence locally, cleans all
disposable resources, and reports readiness for a separately authorized full replay.
