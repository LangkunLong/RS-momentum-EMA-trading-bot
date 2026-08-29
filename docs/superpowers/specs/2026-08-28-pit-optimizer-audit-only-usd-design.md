# PIT Optimizer Audit-Only USD Design

**Date:** 2026-08-28
**Status:** Approved
**Supersedes:** The independently enforced USD ceilings in
`2026-08-27-model-authored-pit-optimizer-design.md`

## Objective

Let the PIT optimizer use its complete authorized reasoning and code-generation
token envelopes without rejecting a promising hypothesis because a projected
USD amount exceeds an independently chosen per-call or per-run dollar cap.

Calls, tokens, source scope, retries, candidate bounds, sandboxing, and hidden-
fold isolation remain hard controls. USD remains fully accounted and reported,
but it is not an authorization or continuation gate.

## Decision

The optimizer will use calls and tokens as its resource authorization contract:

- a run has an explicit maximum call count and token count;
- every role retains explicit input- and output-token envelopes;
- provider retries remain zero;
- the subset canary remains `apply=false`;
- every authoritative provider cost is persisted and included in run totals;
- no role call, iteration, candidate, or replay is rejected solely because of
  projected or actual USD cost.

The maximum monetary exposure remains indirectly bounded by the authorized
token envelope and the provider's prices. The controller reports that exposure
rather than asking an independently configured dollar value to duplicate the
token authorization.

## Alternatives considered

### Recommended: USD is derived and audit-only

Keep pricing snapshots and actual provider costs in the evidence package, but
remove USD from the grant, window, lease, reservation, and stop-condition
comparisons. This preserves transparency and complete accounting while ensuring
that cost cannot shrink an otherwise authorized token envelope.

### Rejected: use a very high fixed USD ceiling

A high ceiling remains arbitrary. It can still unexpectedly block a future
model or become so loose that it communicates no useful authorization intent.

### Rejected: remove USD accounting entirely

Removing cost records would make runs harder to audit and compare. The goal is
to stop USD from controlling model behavior, not to hide spend.

## Authorization and accounting contract

New PIT optimizer manifests and authorization records use
`schema_version=3`. They contain:

- `max_calls` and `max_tokens` as enforced run limits;
- per-role input/output token and byte bounds;
- `provider_retries=0` and `apply=false`;
- a pricing snapshot when available, marked as advisory;
- projected maximum USD when pricing is available, marked as advisory;
- authoritative per-call and cumulative USD totals after responses arrive.

The following USD fields stop participating in authorization decisions:

- per-role `max_usd`;
- grant `additional_usd`;
- authorization-window `max_usd`;
- run-lease `max_usd`;
- budget-ledger USD reservation and exhaustion checks.

Schema v3 does not preserve these ceilings by substituting infinity, a large
sentinel, or a nullable limit. Enforcing `max_usd` fields are absent from the
new authorization path. Any `projected_max_usd` value is derived evidence, not
authority, and cannot affect readiness, reservation, continuation, or result
selection.

Existing schema-v2 records remain readable as immutable audit history. They are
not rewritten or resumed under the new semantics.

Pricing discovery is best-effort. Failure to obtain the provider's model-list
pricing must not block a role call that fits the authorized call/token plan.
Authoritative response accounting remains mandatory: missing, partial,
negative, conflicting, or otherwise invalid usage/cost data is still a terminal
integrity failure. A high but valid reported cost is recorded and surfaced, not
converted into budget exhaustion.

## Small subset proof before full replay

The existing verification universe is the smallest meaningful end-to-end
subset because it contains known entry activity:

- 25 symbols;
- discovery fold 1: 60 benchmark sessions;
- discovery fold 2: the following 60 benchmark sessions;
- one later 60-session hidden fold, kept sealed during optimizer iteration;
- two complete investigator/author/critic iterations;
- exactly six role calls unless an integrity failure stops the run;
- at most 448,000 tokens across the complete plan;
- no provider retries and `apply=false`.

Each candidate runs in a disposable, network-disabled Docker worker. The
controller evaluates both discovery folds, sends only bounded aggregate
feedback to the critic, and carries the first iteration's investigator,
candidate outcome, and critic direction into the second investigator call.
The source checkout remains unchanged.

A smaller universe or shorter fold would be useful only as a plumbing smoke
test; it is likely to produce no trades and therefore cannot demonstrate that
feedback improves or rejects a trading hypothesis.

## Subset success criteria

Architectural success requires all of the following:

1. The six-call/token plan passes readiness without a USD gate.
2. Investigator, author, and critic artifacts validate for two iterations.
3. At least one model-authored candidate is either evaluated or rejected with
   a deterministic bounded validation result.
4. First-iteration aggregate feedback reaches the second investigator.
5. Incumbent comparison is deterministic and independent of hidden data.
6. Provider usage and actual USD are completely reconciled.
7. Source identity is unchanged and all disposable workers are removed.
8. Hidden validation and the full replay remain unopened.

Performance success is separate. If a discovery candidate improves the fixed
objective, it becomes eligible for the existing one-time hidden evaluation.
Only a qualifying untouched hidden result may be proposed for the full replay.

## Failure behavior

The run still stops on:

- call or token exhaustion;
- malformed or unauthenticated provider output;
- incomplete or inconsistent provider accounting;
- source, data, fold, baseline, or policy identity drift;
- sandbox, audit, deterministic-evaluation, or cleanup failure;
- operator cancellation.

Projected or actual USD cost never appears in the stop-condition list.

## Implementation scope

The focused change touches the existing optimizer contract and no trading
policy behavior:

- `core/pit_optimization_contract.py`: schema and role-plan validation;
- `core/pit_optimizer_authorization.py`: grant/window/lease and reconciliation;
- `core/pit_optimizer_evaluation.py`: provider-free manifest CLI envelopes;
- `core/pit_optimizer_controller.py`: call/token stop conditions and accounting
  summaries;
- `core/pit_optimizer_artifacts.py`: schema-v3 artifact admission;
- `core/pit_optimization.py`: schema-v3 readiness construction and loading;
- `agent_loop.py`: pricing preflight, budget ledger, CLI summaries, and live
  service composition;
- optimizer-focused tests and fixture builders that encode schema-v2 USD gates.

Candidate patch bounds, editable policy paths, discovery scoring, hidden
eligibility criteria, and replay evaluation logic do not change. The subset
controller defers hidden-evaluation invocation so the architecture canary ends
with discovery eligibility and keeps the hidden fold sealed.

This is a PIT optimizer schema-v3 change only. Legacy diagnosis and proposal
routes retain their existing cost controls; shared ledger code must not make
USD audit-only for unrelated workflows.

## Focused verification

Testing remains narrow:

1. A new manifest without an enforcing USD ceiling fails under current code,
   proving the contract test is meaningful.
2. The same high-projected-cost plan passes after the change while call/token
   overflow still fails.
3. Complete authoritative USD is recorded without stopping the loop; invalid
   accounting still fails closed.
4. The existing mocked two-iteration flow passes with USD audit-only.
5. Provider-free readiness and parity are regenerated against a clean commit.
6. After separate live-run authorization, run the six-call subset canary and
   inspect its closed local evidence before considering hidden or full replay.

No full repository test suite or full replay is part of this change.
