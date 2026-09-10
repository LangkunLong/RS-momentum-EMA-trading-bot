# PIT optimizer V5 Luna recovery implementation plan

> **For agentic workers:** Use superpowers:executing-plans, one increment at a time. User instructions override generic test/commit defaults. Steps use checkboxes.

**Goal:** Give the investigator existing diagnostics and let known invalid source become authenticated feedback instead of terminating the optimizer.

**Architecture:** First extend host request projection. Then introduce explicit rendering rejection and journal authority, reuse invalid identities, and publish rejection-only rounds without fabricated evaluations or critics.

**Tech Stack:** Python3.13, existing V5 dataclasses/artifact repository, PowerShell and Ruff.

**Spec:** `docs/superpowers/plans/2026-09-07-pit-optimizer-v5-recovery-design.md`.

**Status:** Preparatory recovery plan. Campaign06 failed before evaluation. This is not the requested handoff after two completed evaluated rounds.

## Session setup

Use existing worktree `C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture`, branch `codex/pit-optimizer-v5-architecture`. The user creates the Luna session manually and asks the principal architect to review its changes.

```powershell
Set-Location -LiteralPath 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture'
git status --short
git branch --show-current
```

Read this plan, its design, and `.artifacts/pit-optimizer-v5/development/provider-runtime-06/{FAILURE-REPORT.md,terminal-verification.json}`. Consult the latest appended status in `development/STATUS-20260907.md`. Do not load the entire historical log/repository. Use `rg -n` for named functions and read relevant ranges; exclude tests.

## Global constraints

- No tests: do not read, create, modify or run them. No synthetic probes. Use actual archived requests/responses/reports, source review, scoped Ruff and in-memory compilation.
- No model/API calls, credential reads, candidate evaluation, purchases, trading, deployment, held-out work, commits, pushes, hook changes or broad cleanup.
- Preserve all existing edits. No new checkout, rebase/reset, or historical artifact rewriting.
- Never restart/resume terminal provider-runtime-01 through06. Session5015 is terminal; do not poll it.
- Five calls/868287 tokens remain, but replacement requires6 calls. Do not launch07 or silently replenish budget.
- No source/config/image changes while another campaign is live; confirm current state before editing.
- Development S&P500 inputs remain provisional; full production inputs remain incomplete.

## Fixed reference identities

| Reference | Identity |
| --- | --- |
| Evaluator runtime | `005064e57e80840f3b2a97d7db694b176832b8f982996e63efc8a35064505ed4` |
| Image | `pit-optimizer-v5-evaluator@sha256:4e85b19aa8c135362ae8796e4d33a410ec5af650d296c358ae6a7737d5b05b6a` |
| Explicit profile | `.artifacts/pit-optimizer-v5/evaluator/sandbox-profile-development-005064e57e80.json` |
| Parent source bundle | `109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e` |
| Parent policy revision | `5d2a95ba2572e71dab448934544cef22130415fcc1691c9a98deb820d40a1fa9` |
| Provider source before this increment | `d9b17d62a6c8b485ba6ae6abb22a9e87f9d908b3c96409ba6e21f86eb691e937` |
| Actual06 author request | `09564719a54cfd10a0b6edbc5b61996e7a68015866f1e0b538859c542d158c0f` |
| Actual06 response text | `2c6d37ce6e8bc4eb65f021d70e289ac6c53612d7faba45c8ab1208489dad3d93` |

Response text hash differs from enclosing JSON-file hash. The default sandbox-profile.json is historical. Use the explicit profile. Main `.env` contains credentials; leave it unread.

## Increment A: expose existing entry-funnel evidence

**First Luna session: implement only A1-A3, then stop for architect review.**

Production scope: `core/pit_optimizer_v5/production_runtime.py`; optionally a short description in `provider.py`. No schema/dataclass/evaluator changes.

### A1. Capture authority and locate the seam

- [ ] Confirm worktree, branch and absence of newer live work.
- [ ] Create a fresh `.artifacts/pit-optimizer-v5/development/luna-recovery-a/` review package. Do not overwrite an existing package.
- [ ] Save exact preimage bytes/SHA256 of potentially changed files; preserve newline conventions.
- [ ] Compute evaluator identity using existing `contracts.evaluator_source_map_v5(Path.cwd())` and `evaluator_source_sha256`. Compare with the fixed reference.
- [ ] Read `LocalRoleRequestFactoryV5._investigator_parts`, `_EvidenceBuilderV5.add`, `_parent_campaign`, `selected_scenario`, and provider `_validate_metric_id`/`_scan_aggregate_value`. Do not change validators.

### A2. Add a closed counter projection

- [ ] Declare this private tuple in production_runtime.py, preserving its exact order:

```python
_INVESTIGATOR_ENTRY_COUNTS_V5 = (
    "stage.evaluated_rows",
    "stage.buy_signal_count",
    "stage.market_pass",
    "stage.breakout_pass",
    "stage.buy_zone_pass",
    "stage.technical_score_pass",
    "stage.rs_pass",
    "stage.entry_block_current_growth_below_threshold",
    "stage.entry_block_current_growth_unavailable",
    "stage.entry_block_annual_growth_below_threshold",
    "stage.entry_block_annual_growth_unavailable",
    "stage.entry_block_rs_score_below_threshold",
    "stage.entry_block_rs_score_unavailable",
    "stage.entry_block_composite_score_below_threshold",
    "stage.entry_block_composite_score_unavailable",
    "episode.discovery.entries_executed",
)
```

- [ ] Retain the four current headline metrics per episode and all existing archive/history evidence.
- [ ] Immediately after the existing headline evaluator metrics and before any archive/history evidence is added, append the new counts to the same builder. The role citation sequence requires all evaluator evidence to precede archive/history evidence. IDs in future requests may change; stored historical requests must remain unchanged.
- [ ] Iterate authenticated parent campaign episodes in their existing order. Read only `selected_scenario(episode.evaluation).report.entry_funnel`.
- [ ] Create a metric-to-count lookup. Iterate the explicit tuple, not dictionary order. Omit missing metrics; never substitute zero.
- [ ] Emit `episode.{ordinal}.entry.{source_metric_id}` and append every issued ID to evaluator_ids. Values are existing integer counts only.
- [ ] Exclude source-universe labels, symbols, dates, arbitrary reason strings and raw rows. Do not export every report metric: provider validation rejects components such as `volume`; do not loosen it.
- [ ] If guidance is added, append to the existing investigator wire description: “Funnel counts overlap; absent counts are unavailable. Counts alone are not causal.” Do not add canonical role input/schema fields.

Local shape, adapted to existing variable names:

```python
for episode in campaign.episodes:
    report = selected_scenario(episode.evaluation).report
    counts = {item.metric_id: item.count for item in report.entry_funnel}
    for metric_id in _INVESTIGATOR_ENTRY_COUNTS_V5:
        if metric_id in counts:
            evaluator_ids.append(evidence.add(
                f"episode.{episode.episode_ordinal}.entry.{metric_id}",
                counts[metric_id],
            ))
```

### A3. Verify actual evidence and return the package

- [ ] Load actual06 baseline authority through LocalArtifactRepositoryV5, authenticating its references. Do not use an arbitrary parsed report as trusted input.
- [ ] First actual parent window expectations: evaluated_rows960, market_pass960, composite-score blocks932, buy_signal_count0, entries_executed0. Counts overlap.
- [ ] Construct a future investigator request locally with the existing factory and actual authenticated parent/manifest. Use the call-free construction sequence in `.superpowers/sdd/2026-09-04-pit-optimizer-v5-campaign/prepare-provider-development.py` as reference.
- [ ] Do not rerun the create-only helper on06 or persist a replacement request into06. Save the preview only in the recovery package, clearly marked future/unexecuted.
- [ ] Compare every added item to its report. Verify integer values, exact allowlist, deterministic IDs/order, inclusion in aggregate_evaluator_evidence and absence of raw identifiers.
- [ ] Compute prospective_role_usage_v5 for the actual preview. New request hashes should change; stored historical hashes must not.
- [ ] If wire guidance changed, verify the23 archived role schemas still fit their prior canonical byte bounds; see role-contract-guidance-repair/verification.json. Do not weaken historical ledger equality.
- [ ] Run scoped Ruff once. Compile changed files in memory with Python's compile(bytes,path,'exec'); do not execute their contents or create pyc files.

```powershell
py -3.13 -B -m ruff check core/pit_optimizer_v5/production_runtime.py core/pit_optimizer_v5/provider.py
py -3.13 -B -c "from pathlib import Path; compile(Path('core/pit_optimizer_v5/production_runtime.py').read_bytes(), 'production_runtime.py', 'exec'); compile(Path('core/pit_optimizer_v5/provider.py').read_bytes(), 'provider.py', 'exec')"
```

- [ ] Recompute full evaluator identity. It should remain unchanged. If not, report the unexpected covered file; do not rebuild automatically.
- [ ] Read-only summarize terminal06 using saved campaign-launch-input.json argv: command becomes `summarize`; remove `--owner-token-sha256` and its value. Keep original manifest/config and absolute artifact root. Run in the same elevated environment as preparation. Never run/resume.
- [ ] Save scoped diff, final hashes, preview hash, per-counter comparison, actual CLI result and evaluator identity before/after.

**Architect checkpoint A:** Return the package and stop. Principal architect reviews meanings, provenance, ordering, size and compatibility. Do not proceed to B or spend on a campaign without the next instruction.

## Increment B: durable source rejection

Separate session after A review. Files: rendering.py, runtime.py, memory.py, artifacts.py, production_runtime.py; provider.py only for bounded rejection evidence. Leave candidate_ir.py unchanged.

### B1. Rendering outcomes

- [ ] Add frozen `RejectedVariantV5(assignment: VariantAssignmentV5, failure_code)` with closed code `policy_source_unbounded_or_stateful`.
- [ ] Add `render_variant_outcomes_v5` with existing parent/revision/template/maximum inputs, returning ordered rendered variants or rejections.
- [ ] Share deterministic assignment enumeration, cap/marker handling and collision logic. Preserve strict render_variants API for old callers; they must not receive rejection objects.
- [ ] At `_bundle_from_sources(rendered_sources)` only, capture the exact observed ValueError message from the pinned AST validator. Parent input validation must already have succeeded. Unknown errors remain fatal.
- [ ] Rejections consume normal variant slots. Never execute/normalize rejected bytes, substitute parent bytes, invent a policy revision, or enumerate past the cap.
- [ ] Inspect unchanged actual06 author/parent through the new API: it yields the closed rejection. Strict legacy API still rejects. No model or evaluator call is involved.

### B2. Journal authority

- [ ] Add `RenderRejectionPayloadV5`: existing PreValidationInvalidExperimentIdentityV5, exact assignment, accepted-author ArtifactRefV5 and closed failure code.
- [ ] Register `render_rejected` in memory event/payload unions/maps, serialization, exact artifact decoding and runtime journal mapping.
- [ ] Append/replay must bind to this round's accepted author completion and artifact/template hash. Recompute identity from authenticated round hypothesis/template/assignment.
- [ ] Confirm assignment belongs to the same bounded enumeration. A self-consistent arbitrary hash is insufficient.
- [ ] Permit one rejection per invalid identity after author completion and before critic. Reject any subsequent render/validation/materialization/evaluation/critic for it; reject duplicates/conflicts.
- [ ] Preserve current validation_invalid predecessor requirement. It cannot represent this event: existing replay requires rendered_variant, and validation_invalid cannot carry failure_ref.
- [ ] Keep old payload decoding/identities unchanged; no new required fields on old serialized types.

### B3. Invalid records without materialization

- [ ] Add a separate host rejection carrier with invalid identity, actual template/assignment, ValidationResultV5(False,closed_code,()), and rejection reference. Keep CandidateEvidenceV5's materialization invariant.
- [ ] Valid variants take existing materialization/validation/evaluation path. Rejections create no workspace, container, execution or lease.
- [ ] Reuse ExperimentRecordV5(status='invalid'); policy revision, fingerprint, quick/campaign evidence, target gap and critic fields are None; episodes empty.
- [ ] Extend record-factory protocol and CanonicalExperimentRecordFactoryV5 for the explicit union. Retrieve rejected assignment directly, not through materialized.variant.
- [ ] Allow absent critic only for no-testable-candidate finalization. Testable candidates still require their exact critic. Never manufacture an empty critic artifact.
- [ ] Keep runtime _expected_records and concrete factory output equality checks aligned.
- [ ] Repository append/load must compare invalid record identity, assignment, template and validation code with referenced rejection authority. Checking only reference SHA is inadequate.
- [ ] _publish must access source/materialization authority only for evaluated/zero_trade records. Invalid proposals cannot enter archive/promotion/performance rankings.

### B4. Completion and recovery

- [ ] Replace all-invalid no_testable_experiments abort with explicit invalid-record publication, checkpoint, cleanup and completed lifecycle result.
- [ ] Mixed batches retain critic only for testable candidates in binding order; publish both valid and rejected records honestly.
- [ ] Recovery reuses accepted author/rejection evidence with exact bindings; no duplicate paid role, deadline refresh or historical terminal replay.
- [ ] Audit _completed_result and recovery reducers for critic assumptions. Preserve mandatory critic evidence for evaluated records.
- [ ] The next investigator gets closed numeric rejection evidence from authenticated invalid records, e.g. failure.rendering.policy_source_unbounded_or_stateful_count. Do not export raw exception text or claim a more specific cause.
- [ ] Verify novelty/parent selection retains the failed hypothesis as history; do not erase it to force another try.
- [ ] Rejection-only completion is not an evaluated experiment. One rejected round plus one evaluated round does not fulfill two evaluated rounds.

**Architect checkpoint B:** Return exact diff, event/state transition account, actual06 reproduction, historical compatibility evidence and unresolved assumptions. Full new journal behavior remains unexercised until a real authorized campaign; say so. Do not launch it automatically.

## Increment C: architect-led verification and future campaign

- [ ] Run scoped Ruff/compilation, actual archived-response/report checks and exact serialization review. No synthetic responses or fabricated evaluation reports.
- [ ] Confirm original06 response/journal/hash/terminal summary unchanged. Recompute evaluator closure. Host-only changes should reuse current image and four parent discovery baselines plus the quick evaluation.
- [ ] If covered source changed, return for architectural review before rebuilding/sealing and regenerating affected real evidence. Never rebind old reports.
- [ ] Prepare a distinct future campaign only after review. Fresh IDs/paths and exact outbound packet/caps are required; preparation is not execution.
- [ ] Report current budget and sufficient proposed call/token scope. Replacement07 is currently ineligible. Obtain a new grant/amendment for the concrete future run; do not silently raise limits.
- [ ] During any future authorized run, use its exact saved launch and same elevated environment. Keep AC/lid open; awake guard cannot prevent explicit lid sleep. Poll the same actual session; observation timeout is not permission to duplicate a start.
- [ ] Require two real evaluated rounds with critic/checkpoint/cleanup evidence for the original success milestone. Report rejected and lifecycle rounds separately.
- [ ] Only then write the final requested post-campaign Luna handoff from actual results. User creates the next session manually.

## Luna return format

Link the review package and report completed tasks, changed files, actual verification/results, evaluator identity before/after, unverified behavior and architect decisions needed. Confirm no tests, external calls, credential reads, campaign restarts or commits. Do not paste full history/diff or claim optimizer completion from static checks.

## Ready-to-paste first Luna session prompt

```text
Work in C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture.
Read docs/superpowers/plans/2026-09-07-pit-optimizer-v5-luna-recovery.md and its linked design.
Implement Increment A only, tasks A1-A3. Stop at Architect checkpoint A and return the review package.
Do not implement B or launch a campaign. Preserve existing work and historical artifacts.
No tests or synthetic probes, model calls, credential reads, candidate evaluation, commits, pushes or deployment.
Use actual archived reports/requests, scoped Ruff and in-memory compilation for verification.
The principal architect will review exact changes before the next increment.
```


## Architect checkpoint A decision

Increment A is APPROVED at source SHA0e0dbb7e5dcac8ebd4716dbbaa124c83f1d3e79aec24f41e09976b8acdc8102b. Independent actual06 preview reconstruction matched SHA3dfce23e0e540fd072c9ad482be0e907e62de992eaf65df23a2ed31df81e9afa;64 counts/80 total evidence items verified. Evaluator identity unchanged. Review: .artifacts/pit-optimizer-v5/development/luna-recovery-a/architect-review.md.

For lower implementation risk, split Increment B at an additional architect checkpoint: next implement **B1 only**, then return the exact diff and actual-response verification before B2. B2-B4 remain unimplemented. This is local implementation scope, not a new external-call grant.

### Next Luna session prompt

```text
Work in C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture.
Read docs/superpowers/plans/2026-09-07-pit-optimizer-v5-luna-recovery.md and its linked design.
Increment A is architect-approved; preserve it unchanged.
Implement B1 only: the host-side RejectedVariantV5 carrier and render_variant_outcomes_v5 API.
Keep render_variants strict/backward compatible, deterministic enumeration/caps/collision behavior, and the exact known AST-error boundary.
Do not change candidate_ir.py, journal/event schemas, runtime finalization, provider guidance, data or evaluator source.
Use the unchanged actual06 accepted author response and authenticated four-file parent for local verification. Do not modify or resume the terminal campaign.
No tests/synthetic probes, model/API calls, credential reads, candidate evaluations, commits, pushes or deployment.
Save preimages, narrow diff, final hashes, scoped Ruff/in-memory compile results, evaluator identity and actual06 outcome verification in a fresh luna-recovery-b1 review package.
Stop at the new B1 architect checkpoint before B2 and return the review package.
```


## Architect checkpoint B1 decision

B1 is APPROVED at rendering.py SHA a74a513b42bb65e8c68492ee342dab9c049939e4d11dee2c09365425c69e65dd. Actual06 rejection and strict original ValueError independently verified, with response/artifact equality and unchanged evaluator identity. Review: .artifacts/pit-optimizer-v5/development/luna-recovery-b1/architect-review.md.

Next session is **B2 only**, then a journal-authority architect checkpoint before B3/B4. Keep B1 and IncrementA intact. B2 may touch memory.py, artifacts.py and runtime.py's journal registration/authentication seam; it must not yet switch the campaign renderer, publish invalid records or change finalization. A minimal bounded assignment helper in rendering.py is allowed only if needed to share existing enumeration semantics; document that deviation and preserve the B1 APIs.

### B2 implementation clarifications

- Payload must bind an existing pre-validation invalid identity, its exact typed assignment, the accepted author artifact reference and the one closed rejection code. Do not duplicate identity fields unnecessarily or invent a source revision.
- Dataclass validation handles shape; repository/journal context must establish that the author was accepted in this campaign/round and recompute the hypothesis/template/assignment identity. A valid hash alone does not establish that relationship.
- Assignment membership must honor the original maximum and deterministic order. Do not enumerate an uncapped Cartesian product or merely validate that individual values belong to their axes.
- Rejection takes the candidate from absent to terminal, once. Preserve the old rendered_variant -> validation chain for materialized candidates. Reject conflicting/duplicate/subsequent events, and preserve old payload bytes and decoding.
- B2 does not manufacture a new historical06 event. Keep original06 terminal evidence immutable. Actual06 inputs may be used for local read-only payload derivation/inspection, clearly marked unexecuted. No synthetic journal/campaign or fake provider response is required by this checkpoint.
- Explain separately which append/replay invariants were reviewed statically and which existing historical artifacts were actually loaded. Do not claim end-to-end rejection replay was exercised if no such real event exists yet.

### Next Luna session prompt: B2

```text
Work in C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture.
Read docs/superpowers/plans/2026-09-07-pit-optimizer-v5-luna-recovery.md and its linked design, including the latest B1 approval and B2 clarifications.
A and B1 are architect-approved. Implement B2 only: the dedicated render_rejected payload/event, exact decoding/registration, and authenticated journal append/replay rules.
Preserve existing validation event ordering, strict rendering behavior, historical artifacts, evaluator source and approved A code.
Do not implement B3/B4, switch the campaign renderer, publish invalid experiment records, or finalize all-invalid rounds yet.
No tests/synthetic probes, model/API calls, credential reads, candidate evaluations, campaign restarts, commits, pushes or deployment.
Save exact preimages, narrow diff, final hashes, actual-artifact verification, scoped Ruff/in-memory compilation and evaluator identity in a fresh luna-recovery-b2 review package.
Return the event/state-transition explanation and distinguish static review from executed verification.
Stop at the B2 architect checkpoint before B3/B4.
```


## Architect checkpoint B2 decision

B2 requires two corrections before approval: bind the complete authenticated author-input hypothesis hash to the round intent, and exclude rejected identities from the bound experiment IDs of any subsequent critic completion (accepted or failed) during both append and replay. Preserve critics for other testable candidates in mixed rounds.

The submitted source hashes and archived actual06 authority were independently verified; scoped Ruff, compilation and diff checks passed, with the evaluator identity unchanged. Actual06 has no rejection events, so new rejection append/replay and critic exclusion remain unexecuted. See `.artifacts/pit-optimizer-v5/development/luna-recovery-b2/architect-review.md` and `architect-verification.json`.

The next Luna session must implement **B2 corrections only**, preferably in artifacts.py's existing shared repository context validator. Detailed steps: `.artifacts/pit-optimizer-v5/development/luna-recovery-b2/next-luna-prompt.md`. Save a fresh correction package and return to the architect checkpoint. B3/B4, campaign activity and the final two-evaluated-round milestone remain pending. This is local correction scope and grants no new external-call budget.


## Architect checkpoint B2 correction decision

Corrected B2 is APPROVED at artifacts.py SHA `135e006e892c6f32221d542bf5b9150c379d1a37713b3aa0e80307fe444174e1`. Both previous findings are resolved. This supersedes the prior corrections-required decision. Review and independent verification: `.artifacts/pit-optimizer-v5/development/luna-recovery-b2-correction/architect-review.md` and `architect-verification.json`. New rejection/critic branches remain statically reviewed, not executed; actual06 has neither event type. The evaluator identity is unchanged.

Split B3 into two architect-reviewed sessions. **B3a next** adds only a separate rejected-candidate carrier and aligned protocol/concrete/expected record construction, including an absent critic only for nonempty no-testable batches. **B3b later** authenticates rejected records against their durable event and adds publication safeguards. B4 then activates runtime rendering/completion/recovery/feedback. Preserve the current runtime behavior during B3a.

Use a durable RoundEventV5 event-file reference in the new carrier and existing record artifact_refs, so B3b can authenticate journal membership and campaign/round authority without changing the serialized ExperimentRecordV5 schema. Do not invent an event reference for actual06.

Detailed next-session instructions: `.artifacts/pit-optimizer-v5/development/luna-recovery-b2-correction/next-luna-prompt-b3a.md`. Expected B3a production scope is runtime.py's carrier/protocol/expected-record construction and production_runtime.py's record factory/imports only. Preserve approved A projection bytes even though production_runtime.py's whole-file hash will change. Save a fresh B3a review package and stop before B3b/B4. No new campaign or external-call budget is granted.


## Architect checkpoint B3a decision

B3a is APPROVED at runtime.py SHA `d331fd540b84a8bad160bd2dd85dc4a01803362511668503411eb7f44abd17df` and production_runtime.py SHA `78da0dc5efcf5cf94541a465e0a3c212221c843dade07f680ddc5e1508fdc476`. No actionable findings remain. Independent verification confirms the narrow function changes, 47,615 byte-identical approved-A projection bytes, archived actual06 B1 rejection, scoped checks and unchanged 56-file evaluator identity. New carrier/record paths remain statically reviewed and unexecuted.

Next implement **B3b only**: shared append/load rejected-record authentication against the exact durable event and complete journal/intent/author authority; minimal _publish union/type safeguards keeping invalid records out of source authority and performance/promotion. Preserve legacy materialized-invalid and evaluated records. Checkpoint before B4.

Review and detailed Luna steps: `.artifacts/pit-optimizer-v5/development/luna-recovery-b3a/architect-review.md` and `next-luna-prompt-b3b.md`. Actual historical evaluated record compatibility input is documented there. Do not manufacture rejection events or records from terminal actual06.

Explicit B4 follow-up: `_verify_finalized_discovery_rounds` still assumes rendered_variant-only candidate IDs and investigator/author/critic completion; `_completed_result` and recovery require a matching audit. Keep these out of B3b while recording the remaining requirement. No new campaign, evaluator rebuild, or external-call budget is authorized by this implementation scope.


## Principal architect checkpoint B3b decision

B3b is APPROVED at artifacts.py SHA `33a124043338d834fe3b60508b533c777df5c4fefbbdfb52ac812ab5eda0ca4f` and runtime.py SHA `1c4c4dd2a3c7082982866e6ed820336e5afeb9755b0005fb80e65224bcc0d462`. No actionable findings. The principal independently verified exact hashes/scope, scoped checks, unchanged evaluator identity and read-only canonical compatibility of the actual runtime-04 evaluated record. Rejection append/load/publication branches remain statically reviewed, not executed.

Review and independent evidence: `.artifacts/pit-optimizer-v5/development/luna-recovery-b3b/principal-architect-review.md` and `principal-architect-verification.json`. The package's earlier architect-verification.json remains the implementer's evidence and is preserved.

Split B4 into bounded architect-reviewed work: **B4a next** connects outcome rendering, durable rejection event/carrier creation or recovery, and explicit union routing through screening/discovery/critic filtering. **B4b later** completes no-testable rounds and audits finalized-campaign/recovery authority. **B4c later** projects bounded rejection feedback and verifies retained novelty/history. Detailed next Luna instructions: `.artifacts/pit-optimizer-v5/development/luna-recovery-b3b/next-luna-prompt-b4a.md`.

Expected B4a source scope is runtime.py only. It preserves the current no_testable_experiments abort until B4b; no campaign may be launched at this intermediate checkpoint. Preserve historical terminal06 and all paid-role/deadline authority. _completed_result currently has no direct critic check; audit rather than changing it based on the earlier broad note. _verify_finalized_discovery_rounds has explicit rendered-only and three-role assumptions for B4b. Stop after B4a and return for review. No additional campaign or external-call budget is granted.


## Principal architect checkpoint B4a decision

B4a is APPROVED at runtime.py SHA `097b5c1e67e2f4c2852a8033e1a3f30d14254f0e2d18b4a899589d555e61d1f3`. No actionable findings. Principal verification confirmed exact scope/hashes, protected source bytes, scoped checks, unchanged evaluator identity, actual06 pure rejection/invalid identity, and canonical runtime-04 evaluated-record compatibility. New runtime/journal branches remain statically reviewed and unexecuted.

Review/evidence: `.artifacts/pit-optimizer-v5/development/luna-recovery-b4a/principal-architect-review.md` and `principal-architect-verification.json`. Next Luna instructions: `.artifacts/pit-optimizer-v5/development/luna-recovery-b4a/next-luna-prompt-b4b.md`.

Implement **B4b only**: common finalization for nonempty no-testable batches with no fabricated critic; finalized-round event/record/role authority and necessary narrow recovery audit. Preserve required accepted critic authority for every testable candidate and all old terminal outcomes. Expected source scope is runtime.py and artifacts.py. A materialized validation-invalid record has a pre-validation ID different from its rendered event ID; finalized authority must map exact assignments and validation evidence instead of merely unioning event ID sets. Preserve the distinct rendering-rejection form and full bounded candidate completeness.

Return for review before B4c feedback/history work. No campaign, evaluation, new runtime event/record/checkpoint, external call, or evaluator rebuild may be executed in this implementation session. The two real evaluated feedback rounds remain unfinished; lifecycle completion is a separate count and grants no new paid-call budget.


## Principal architect B4b / C1 decision

B4b remains HELD pending C1 correction, at runtime.py SHA `d834eae8e3e120355b4479c77f87ebe46519a0b82a52cc072abe1dab60943aec` and artifacts.py SHA `2bab516e4945976d9bf6bb356934289cd14d9b87c25088521a043c8ac5cfa2c2`. No additional blocker was identified in the principal source review. Scoped checks, exact hashes, unchanged evaluator identity and real runtime-04/actual06 compatibility were independently verified. New lifecycle/finalized branches remain unexecuted.

Principal decision: correct C1, preserving the intended exact-duplicate completion guarantee. For a post-validation record with exact_duplicate status AND candidate policy identity equal to the parent, require actual validation.changed_symbols == (). Retain template equality for every other post-validation record and preserve complete identity/status/evidence checks. Do not alter the nonempty template declaration, actual validation evidence, identity hashing or serialized schemas.

This explicitly permits the next Luna session to change only memory.py's ExperimentRecordV5._validate_post_validation_identity changed-symbol guard. It supersedes that one earlier protection restriction; all other source and authority restrictions remain. The principal recomputed the evaluator map and confirmed memory.py is outside its 56 covered files. There is no evaluator rebuild requirement. The bounded worktree inventory has no stored exact_duplicate among its four experiment records; no positive C1 execution evidence or migration is claimed.

Review/evidence: `.artifacts/pit-optimizer-v5/development/luna-recovery-b4b/principal-architect-review.md` and `principal-architect-verification.json`. Next Luna instructions: `.artifacts/pit-optimizer-v5/development/luna-recovery-b4b/next-luna-prompt-c1.md`. Implement C1 only, then return for principal review before B4c. No campaign, tests, synthetic probes, runtime evidence writes or external calls are authorized by this correction scope.


## Principal architect C1 acceptance and B4c scope

C1 is APPROVED at memory.py SHA 37e3a04791d827edd0e5fe99e1d62cb9997163f66954f99727177248f8949d76. The earlier B4b HELD disposition is superseded: its sole C1 blocker is resolved, and B4b plus C1 is accepted at this checkpoint. Runtime.py and artifacts.py retain their reviewed B4b hashes.

Principal verification freshly confirmed the exact one-method AST change, all submitted hashes, scoped Ruff/compilation/diff checks, unchanged 56-file evaluator identity and read-only runtime-04/actual06 compatibility. The positive exact-duplicate/rejection/lifecycle branches remain static-reviewed and unexecuted; the four-record inventory contains no positive C1 record.

Review/evidence: .artifacts/pit-optimizer-v5/development/luna-recovery-c1/principal-architect-review.md and principal-architect-verification.json.

Implement B4c only using docs/superpowers/plans/2026-09-07-pit-optimizer-v5-b4c-feedback.md. It authorizes production_runtime.py's one authenticated cumulative closed rejection metric and summary.py's three additive progress counts. Preserve all earlier authority, identity, runtime/finalizer and novelty/history behavior. The existing Increment A allowlist and episode projection must remain unchanged, while the newly authorized insertion makes the old whole-region hash inapplicable.

Next manual Luna session prompt: .artifacts/pit-optimizer-v5/development/luna-recovery-c1/next-luna-prompt-b4c.md. Return for principal review after B4c; no Increment C or campaign launch yet. Two real evaluated feedback rounds and the requested post-success handoff remain unfinished. This scope grants no tests, synthetic probes, external calls or renewed campaign budget.


## Principal architect B4c acceptance and transition — September 8, 2026

B4c source is APPROVED at production_runtime.py SHA a757a0ca89979be9453129321a04b96498fb06a9f37438034b46e407ad8b72eb and summary.py SHA e4ccb8a2077ca28d2ece2e541839265b3f08218e1b66e93b635772e600270865. No actionable source finding; A/B1–B4c plus C1 are accepted at source checkpoints. Scoped checks, exact preimages/function scope, protected hashes, Increment A block bytes and 56-file evaluator identity were independently verified.

Fresh principal archive inspection derives runtime-04 counts 1/1/0. Its full summary remains blocked by the historical executor-identity check: the command digest matches, the stored/current executor digests differ, and the exact differing component is not isolated. Do not bypass or rewrite ownership authority.

Actual06 now returns a full authenticated summary with counts 0/0/0. It matches all 23 old terminal summary fields other than the expected historical run/current summarize command label. This supersedes Luna's earlier report that both current summary calls fail, without changing its historical record. Positive rejection/duplicate/mixed lifecycle paths remain static-only.

The remaining runtime-04 full-summary verification gap is explicitly transferred to Increment C-a; campaign readiness is not approved. User requested a detailed transition and will open a new principal session manually. Start with docs/superpowers/plans/2026-09-08-pit-optimizer-v5-transition.md. Principal review and evidence are in .artifacts/pit-optimizer-v5/development/luna-recovery-b4c/principal-architect-review.md and principal-architect-verification.json.

No new campaign, paid-call budget, tests, synthetic artifacts, credentials, evaluator rebuild or production edits are authorized by this transition. The original two real evaluated feedback rounds and final post-success Luna handoff remain unfinished.
