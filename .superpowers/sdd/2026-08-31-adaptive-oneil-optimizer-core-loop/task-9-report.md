# Task 9 report

## Qualification ledger slice

Added the schema-v4 `QualificationPanelIdentity`, reservation, and terminal
outcome contracts. The identity binds the exact bundle, qualification plan and
panel, sorted lineage set, sessions, warmup contract, engine policy, target, and
the provenance-only retirement domain. The permanent hash-chained ledger now
authenticates historical snapshot ancestry, retires all panel lineages when the
reservation is durably appended before evaluation, and records exactly one
terminal outcome for successful or failed evaluation. Configured annualized
targets may select authenticated 10%, 20%, 50%, or additional positive declared
milestones while the manifest continues to bind the active run target.

Verification: compileall and Ruff passed for
`core/pit_optimizer_evaluation.py`. No provider or Docker call was made.

## Frozen champion artifact slice

Added a strict schema-v4 checkpoint loader that accepts only a branch-free
frozen champion. It authenticates canonical checkpoint bytes and digest, the
candidate identity self-digest and source/discovery bindings, the referenced
cumulative diff, the exact three policy sources and their identity-bound
digests, and both referenced panel-evidence artifacts before returning inputs
for disposable reconstruction.

Verification: compileall and Ruff passed for
`core/pit_optimizer_artifacts.py`. No provider or Docker call was made.

## Readiness qualification closure

Schema-v4 gate configuration now requires the explicit qualification plan and
its digest plus the append-only qualification ledger and sealed ancestor head.
Production preparation authenticates the plan pair, proves the sealed plan
snapshot and the readiness-recorded head are ancestors of the current chain,
and rejects any current retirement intersecting the committed quick, discovery,
or qualification panels. Canonical readiness binds both the qualification plan
and its authenticated ledger head while allowing unrelated later retirements.

Verification: compileall and Ruff passed for `agent_loop.py`,
`core/pit_optimization.py`, and `core/pit_optimizer_controller.py`. No provider
or Docker call was made.

## Provider-free qualification core

Added the injected one-use qualification runner. It authenticates the identity
against the exact qualification plan, durably reserves and retires the panel
before the first evaluator callback, evaluates baseline and candidate on the
same panel, derives the existing strict annualized-return decision, and records
either the completed decision or a closed evaluation-failure outcome in a
`finally` path. Cleanup is also guaranteed. The result reports authenticated
coverage and can set `full_replay_ready` only for a qualified result over the
exact approved S&P 500/Nasdaq-100/Russell 2000 union; replay is always unstarted.

Verification: compileall and Ruff passed for
`core/pit_optimizer_holdout.py`. No provider, replay, or Docker call was made.

## Provider-free qualification CLI

Added early `preflight-qualification` and `execute-qualification` actions while
leaving the legacy v3 parser intact. The v4 path authenticates the manifest,
both panel plans, ledger ancestry and current retirements, frozen branch-free
checkpoint champion and referenced artifacts, clean source identity, and a
disposable reconstruction of the exact retained diff and policy sources.
Execution evaluates the unchanged baseline and reconstructed candidate
continuously on the exact qualification panel, persists identity/reservation,
baseline, candidate, decision, terminal ledger outcome, cleanup, and a
content-free summary, and never writes a role checkpoint. Summary replay
readiness is forced false on abort or incomplete cleanup and replay is always
reported unstarted.

Verification: compileall and Ruff passed across all Task 9 production files.
A focused injected S&P-only smoke proved reservation precedes evaluation,
baseline and candidate use the same panel, the strict decision and terminal
outcome are recorded, cleanup runs, and `full_replay_ready`/`started` remain
false. No provider, replay, or Docker call was made.
