# PIT optimizer V5 — paused development handoff

**Status: paused at the user's request on 2026-09-09. Engineering work is saved; the two evaluated feedback rounds and the results-based Luna handoff are not complete. Do not launch a campaign from this document.**

The user requested: “wrap up everything that you have, write clear handoff documents, commit and push to remote origin main, i will work on something else for now”. This supersedes the earlier instruction to keep working until the transition is complete. Resume only when the user returns to this work.

## Start here

- This document explains the saved state and remaining work.
- [RESUME.md](RESUME.md) gives the next bounded steps and the exact local locations.
- [evidence/INDEX.json](evidence/INDEX.json) inventories selected exact-byte reference snapshots. These are evidence, not a portable runnable campaign package.
- [Transition plan](../../plans/2026-09-08-pit-optimizer-v5-transition.md) preserves the full earlier plan. Its original starting instructions are historical; this handoff is the current checkpoint.
- [Admission plan](../../plans/2026-09-08-pit-optimizer-v5-campaign-admission.md) and [design](../../specs/2026-09-08-pit-optimizer-v5-campaign-admission-design.md) describe the completed admission work.

## What is implemented

The accumulated architecture branch contains the V5 policy, evaluator, provider, runtime, campaign, and development-data work. The latest uncommitted increment, saved with this handoff, adds or completes:

1. Development campaign composition and provider execution using the shared native feedback loop.
2. Closed rendering rejection and truthful accounting of completed lifecycle rounds, evaluated feedback rounds, and render-rejected experiments.
3. Historical verification against stored execution evidence, without substituting the current machine's executor identity for an old run's identity.
4. Campaign admission policy: exact per-role request bounds, reserves for the evaluated-round target, finite attempts/calls/tokens, original deadlines, and guards before paid reservation. Completed-history compliance requires an explicitly pinned policy reference.
5. A concrete, independently reviewed development campaign proposal, prepared once from existing real baseline evidence.

Primary source entry points are `core/pit_optimizer_v5/campaign_admission.py`, `historical_verification.py`, `development_preparation.py`, `operations.py`, `controller_roles.py`, `production_provider.py`, `production_runtime.py`, `production_sandbox.py`, `rendering.py`, and `summary.py`. CLI routing is in `core/pit_optimizer_v5/cli.py`.

P1–P4 admission work and C-a/C-b readiness were independently approved. The final 22-file source authority is retained in `evidence/reviewed-source-authority.json`; the approval is in `evidence/admission-p4-independent-review.md`.

## Actual campaign state

Campaign ID: `development-provider-two-evaluated-admission-20260908-01`.

Preparation succeeded once. The user approved the exact proposed run. However, **this campaign has made zero paid role calls, consumed zero provider tokens, and produced zero evaluations**. It has no initialized campaign launch, round deadline, or role ledger in the last authenticated observation. There are 31 preparation files plus the one-byte lock file created by the first failed launch.

| Milestone | Actual result |
| --- | --- |
| C-a / C-b historical verification and readiness | Complete; existing runtime-04 and actual06 histories verified without running them again |
| C-c concrete preparation and paid grant | Complete; prepared once and approved |
| First approved launch | Exited before initialization: execution-context identity mismatch |
| Context reproduction design and first helper | Reviewed; first helper then failed before pipe creation |
| Capture compatibility correction | Implemented and frozen; **independent review pending, never executed** |
| C-d two evaluated feedback rounds | Not started |
| C-e actual-results analysis and Luna implementation handoff | Not started; depends on C-d |

Historical actual06 spend (two calls, 8,596 tokens, USD 0.04234) belongs to an older campaign. It is not spend by this prepared campaign. Historical runtime-04's one evaluated round also does not satisfy this new campaign's two-round target.

## Why the launch stopped

“Resource authentication” is the project's consistency check for the saved code, data, configuration, ownership, and execution settings. It is not a new login or credential request.

The original preparation context reproduced all four saved component identities. The network-enabled launch context changed the executor identity and the dependent candidate-base identity; workspace and mount identities still matched. Both real diagnostics preserved all 32 campaign files. An actual provider-catalog request from the preparation context failed with connection refused; it did not establish model availability.

A reviewed local named-pipe design carries only the native six control-environment entries into the network-enabled child process, without writing their values to disk/output or changing the parent environment. The child must reproduce all four original identities and the full native graph before invoking the exact saved launch once.

The first helper failed during diagnostic import because it redirected stdout to `StringIO`, while `backtest.py` calls `sys.stdout.reconfigure`. No pipe, descriptor, receiver claim, receiver, or campaign was started. The proposed correction uses real `TextIOWrapper`/`BytesIO` streams. Work stopped before its independent review.

| Helper state | SHA-256 |
| --- | --- |
| Old reviewed helper; failed before pipe | `4e801fc0cf381b520ef147b07aa9f91bbc6edec62c058c9a9d2e3d6acfeb80ff` |
| Old independent approval; **does not approve the correction** | `86364488012427bfb06a31e5479e35444015086844dea0600b85d8a755574172` |
| Current corrected helper; unreviewed/unexecuted | `d0c53e05c4f9cabf636a0df634b34d42473c460dccd3dd478d6fa29c59634e7f` |

## Prepared limits and evidence

The original grant is recorded, remains unspent, and is not a new instruction to run during this pause. It covered model `openai/gpt-5.4`, target two real evaluated feedback rounds, three lifecycle attempts, eight role calls, 600,000 total tokens, a 75,000-token per-role envelope, 8,192 maximum output tokens, zero retries/repairs, and a six-hour campaign limit. **There is no hard USD ceiling or current price quote.** Completion was conditional on actual outcomes.

The initial real investigator request contains 80 ordered authenticated aggregate metrics: 28,549 input-bound tokens plus 8,192 output tokens = 36,741 prospective tokens. Later role packets have not been fabricated or sent.

The usable scope is `development_sp500_v2`, with development semantic checks explicitly disabled. The fixed evaluator image and real baseline results were reused. Final three-universe data is incomplete, and no production promotion, held-out qualification, replay-readiness performance result, or trading deployment is claimed.

## Verification and publication scope

During this wrap-up, scoped Ruff and in-memory Python compilation passed for all 22 changed/new Python source files. Ordinary Git diff whitespace checking passed. Tests were neither read nor run, in accordance with the standing user restriction; no synthetic provider, evaluator, or campaign trials were performed. Static approval is not evidence that the corrected handoff or the campaign succeeds at runtime.

The commit includes source, plans, progress notes, launch wrappers, and a curated reference snapshot of the current handoff evidence. Raw market datasets, campaign runtime trees, credentials, and the full ignored `.artifacts` directory remain local. The original architecture worktree must be retained because the prepared campaign binds its absolute paths and exact local bytes.

The branch was 119 commits ahead of `origin/main`, with no commits unique to `origin/main`, at wrap-up inspection. Publication is intended as a normal fast-forward of the accumulated V5 work plus the final handoff commit; no force push or history rewrite is needed. The publishing task reports the actual resulting commit separately.
