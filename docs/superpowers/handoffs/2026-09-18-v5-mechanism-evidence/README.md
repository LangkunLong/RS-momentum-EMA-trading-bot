# Codex Start Here: V5 Mechanism Evidence

**Date:** 2026-09-18  
**Status:** Documentation-only planning handoff. No implementation or verification results.  
**Source inspected:** `9aa52976f898c27f78c1857ceecde6a7b22a5aab`.

## Requested work

The owner wants the V5 agent loop to learn from focused experiments: commit a hypothesis, compare
parent/candidate decisions on the same causal inputs, and give the critic measured support,
contradiction or insufficient evidence rather than an explanation based only on a backtest score.

The immediate request was to publish plans in the repository for Codex agents. The delivered
artifacts are this handoff, a design and a staged implementation plan; no strategy or executable
code is changed by the planning commit.

## Reading order

1. Read repository guidance and the [preserved September 9 handoff](../2026-09-09-pit-optimizer-v5/README.md).
2. Read the [mechanism evidence design](../../specs/2026-09-18-v5-mechanism-evidence-design.md).
3. Follow the [implementation plan](../../plans/2026-09-18-v5-mechanism-evidence.md), starting with Task 0.

The inspected base has a paused development campaign, `semantic_mode="disabled_development"`,
and standing restrictions on reading/running tests and synthetic trials. Those are not lifted by
this documentation. Resolve authorization for later implementation and the affected checks first.
Do not resume the saved campaign, use its old grant, read heldout data, make paid calls, or run orders.

## Architectural decisions to preserve

- Build an additive evidence extension, not a replacement optimizer or another agent framework.
- Freeze experiment predictions before authoring; bind candidate bytes before measuring them.
- Compare against the exact authored parent, separately from the campaign baseline.
- Keep identical-input mechanism measurements separate from downstream portfolio effects.
- Retain fixed-suite IDs, legacy hashes, early rejection, semantic modes and CAGR ranking.
- Use bounded change-aware probes only under explicit authority; disabled development stays disabled.
- Persist local versioned sidecars; send only issued, sanitized aggregate evidence to the critic.
- Keep unsupported, unexercised, skipped, failed and contradicted results distinct and visible.
- Never equate a finite probe match with universal policy equivalence, or local support with proof
  of future trading performance.

## Delegation

One agent reconciles current source and finalizes contracts. After contract review, a probe agent
and a report agent may work on their separate new files. A single integration owner handles existing
runtime/provider/memory/artifact seams. An independent reviewer checks compatibility and evidence
integrity. No parallel edits to shared adapter files.

All proposed implementation/test filenames are identified in the plan. They were not created by
this documentation commit. Existing test coverage was not inspected or assumed.

## Suggested owner-to-Codex instruction

> Read this handoff, the linked design and the implementation plan. Begin with Task 0 and report
> source drift, the scoped file map and any remaining authorization restrictions. Treat this as an
> evidence-only extension: no strategy, evaluator, ranking, qualification, campaign or broker changes.
> Do not execute tests, synthetic trials, provider calls or saved campaigns merely because the plan
> describes them. Once the owner explicitly authorizes implementation and the necessary bounded
> checks, work in the documented dependency order and retain exact verification evidence.

## What the planning change verified

Repository source and selected design/handoff material were read through GitHub at the inspected
base. Test files, raw datasets and campaign runtime artifacts were not inspected for this work.
The planning change does not claim passing tests, corrected bugs, completed implementation, actual
mechanism observations, backtest results or improved returns. Publication details and the exact
resulting documentation commit are recorded by the pull request.
