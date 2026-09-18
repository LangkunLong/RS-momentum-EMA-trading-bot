# Codex Start Here: V5 Mechanism Evidence

**Date:** 2026-09-18  
**Revision:** 2 — current-source review and evidence reuse acceptance.  
**Status:** Documentation only. Implementation and proposed execution checks remain pending.  
**Latest source inspected:** `15ba962743da2c2ca73becdf85bc639c4f670dfa` on `main`.  
**Original planning base:** `9aa52976f898c27f78c1857ceecde6a7b22a5aab`.

## Requested work and correction

The owner requested a review of the current V5 optimizer against the published GLM infrastructure
process, followed by any necessary update to the implementation/testing plan.

**V5 already has evidence memory and already feeds it back into hypothesis generation.** Do not
build a second generic memory store or treat this work as connecting a previously disconnected
loop. The current source reloads authenticated critic and investigator packages, reissues their
evidence for the next request, and supplies previous hypotheses and directions to the investigator.

The needed extension is narrower: make mechanism findings independently measurable, retain their
conditions and counterevidence, and verify that those qualified findings reach the actual next
investigator request. A successful critic summary alone is no longer the integration endpoint.

## Reading order

1. Read `CLAUDE.md` and the [preserved September 9 handoff](../2026-09-09-pit-optimizer-v5/README.md).
2. Read the [current-source review and design addendum](source-review.md). It separates verified
   wiring, design limitations, and proposed verification, and records the GLM comparison source.
3. Read the original [mechanism evidence design](../../specs/2026-09-18-v5-mechanism-evidence-design.md).
   Its boundaries still apply; the review adds explicit investigator reuse requirements.
4. Follow the revised [implementation plan](../../plans/2026-09-18-v5-mechanism-evidence.md), starting
   with Task 0. Tasks 0–6 remain pending for the implementing agent, including Task 5A–5C.

## Authorization remains separate from planning

The saved development campaign is paused and uses `semantic_mode="disabled_development"`.
Standing restrictions on reading/running tests and synthetic trials are not lifted by this
revision. Do not execute proposed verification, resume the saved campaign, reuse its old grant,
read confirmation/qualification data, make paid calls, or invoke broker/order workflows.

The review read selected active source and documentation through GitHub. It did not inspect test
file contents, datasets, raw campaign artifacts, or the user's local worktree. It did not run code,
tests, backtests, synthetic trials, provider calls, or campaigns. Source wiring is not a passing
runtime result, and the dated handoff is not a fresh observation of the local campaign state.

## Architectural decisions to preserve

- Extend existing records/projections with versioned evidence sidecars; no parallel memory database.
- Freeze predictions and measurement rules before authoring; bind exact bytes before observations.
- Compare against the exact authored parent, not an unlabeled campaign baseline.
- Separate same-input mechanism observations from downstream portfolio consequences.
- Keep measured findings separate from critic interpretation and future-performance claims.
- Preserve contrary, mixed, unexercised, missing, skipped, and failed evidence with its scope.
- Distinguish stable report/lesson identities from request-local reissued evidence IDs.
- Keep fixed-suite IDs, legacy canonical bytes, early rejection, semantic modes, and CAGR ranking.
- Keep disabled legacy operation unchanged; enabled learning may change future proposals, not the
  scoring algorithm or the evaluator's meaning.
- Review schema, admission, context bounds, and sanitization together before adding role inputs.
- Do not equate a two-round fixture test with proof that an LLM reasons better or returns improve.

## Delegation and starting instruction

One agent reconciles source/authorization and owns contracts. Probe and report work may proceed
separately only after contract review. One integration owner handles shared runtime/provider/memory
adapters. An independent reviewer checks evidence integrity and compatibility. No parallel edits
to shared adapters and no broad test discovery under the existing restriction.

> Read this handoff, source-review.md, the original design, and the revised plan. Begin with Task 0:
> reconcile active source, proposed file ownership, current checkout, and authorization. Reuse V5's
> existing evidence-memory-to-investigator path. Implement only an explicitly authorized slice.
> Keep strategy, evaluator, ranking, qualification, broker behavior, and the paused campaign unchanged.
> After separate authorization, demonstrate report reproducibility and the Task 5 two-round evidence
> round-trip with bounded local fixtures. Record unperformed checks as unverified; do not launch a
> campaign or a model call merely because implementation is complete.

Publication status and the documentation commit are recorded by the accompanying pull request.
This handoff does not authorize merging a later PR or bypassing required checks.
