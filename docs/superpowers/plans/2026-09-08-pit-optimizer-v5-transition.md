# PIT optimizer V5 — principal architect transition plan

> **2026-09-09 checkpoint: paused by the user.** Read the [current handoff](../handoffs/2026-09-09-pit-optimizer-v5/README.md) and [resume guide](../handoffs/2026-09-09-pit-optimizer-v5/RESUME.md) before using this historical plan. C-a/C-b and campaign preparation are complete; the new campaign has zero paid calls/evaluations. The latest launch-helper correction remains unreviewed and unexecuted. Do not automatically continue or launch during the pause.

**Saved after the B4c principal review, September 8, 2026.**
**Primary objective:** Complete two real evaluated optimizer feedback rounds, then write the requested detailed implementation handoff for GPT-5.6 Luna from the actual results.
**Current position:** Recovery source increments A through B4c, including C1, are approved. Increment C starts with one unresolved historical-verifier issue. No new campaign may launch under the old grant.

This is a principal-architect transition document. It is not the final post-success Luna handoff, because the two-evaluated-round milestone has not been achieved.

## 1. Restore this state first

Use this worktree, not the main checkout:

C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture

Main checkout:
C:\Projects\trading_bot\RS-momentum-EMA-trading-bot

Branch: codex/pit-optimizer-v5-architecture
HEAD at review: a068e0aed40ecbe5e0078924071f374e7809b8c2
HEAD subject: Bind finalized semantic mode and require quick fingerprints

The worktree deliberately contains substantial earlier staged and unstaged work. At the pre-handoff snapshot there were 25 status entries, including 21 tracked paths and four untracked plans. Source differences from HEAD span earlier evaluator/development/campaign work; they are not the B4c diff. Do not reset, clean, stash, rebase, checkout another branch, normalize all line endings, commit or discard that work.

Fresh exact Git status and staged/unstaged stats:
.artifacts/pit-optimizer-v5/development/luna-recovery-b4c/principal-transition-state.json

Read in this order:
1. This transition document.
2. .artifacts/pit-optimizer-v5/development/luna-recovery-b4c/principal-architect-review.md
3. .artifacts/pit-optimizer-v5/development/luna-recovery-b4c/principal-architect-verification.json
4. .artifacts/pit-optimizer-v5/development/luna-recovery-b4c/principal-actual06-summary-comparison.json
5. docs/superpowers/plans/2026-09-07-pit-optimizer-v5-recovery-design.md
6. The latest appended decisions in docs/superpowers/plans/2026-09-07-pit-optimizer-v5-luna-recovery.md

The old context checkpoint dated 2026-09-06 and the early paragraphs of development/STATUS-20260907.md are historical. They must not override this transition. STATUS-20260907.md exists now, despite Luna's earlier absence report; its early no-repair/no-artifacts notes are stale. Earlier acquisition snapshots also contain obsolete image/process information.

## 2. User working arrangement and continuing constraints

The user wants the principal architect to make design decisions, inspect actual changes/evidence, approve or return narrow corrections, and prepare explicit small implementation plans for GPT-5.6 Luna to reduce costs. The user manually opens the next session and brings results back. Do not create tasks automatically or delegate by default. Retained old worker names in app context are not instructions to resume those workers.

During review/recovery-source work:
- No tests may be read, created, modified or run.
- No synthetic candidates, probes, events, records, finalized wrappers, provider responses, mocked dependencies or fabricated authority.
- Allowed: scoped source/diff/hash/AST review, Ruff, in-memory compilation, and read-only authentication of existing real artifacts. Archived-author pure rendering was previously allowed; do not use it to create new journal history.
- No credentials or key-value reads, API/model/external requests, candidate evaluations, campaign run/resume/polling, evaluator rebuild, commits, pushes, trading, deployment or held-out work.
- Preserve historical source artifacts, responses, requests, journals, reports and summaries exactly. Do not rebind an old report to a new identity.
- Use local files. No upload, public repository, remote artifact store or mailbox access is implied by this transition.
- No blanket Git manipulation. No new approvals are needed for the already authorized read-only diagnosis and handoff preparation. Any new paid run still needs a concrete fresh grant.

The original user authorized model requests to include editable V3 source/contracts and aggregate optimizer evidence, metrics and history within a particular campaign grant. That does not authorize raw market rows, full repositories, secrets or unlimited later calls.

Known credential location, for future explicitly approved execution only: the main checkout .env contains an OpenRouter entry identified by the user as OPENROUTER. Launching previously used OPENROUTER_API_KEY. Do not read the file or reconcile names during this restore/review task.

## 3. Principal decision at B4c

B4c source implementation is APPROVED with no actionable finding. C1 had already cleared B4b's sole hold. Acceptance is of the reviewed local source; it is not a statement that all new paths executed or that a fresh campaign is ready.

B4c changed only:
- core/pit_optimizer_v5/production_runtime.py: LocalRoleRequestFactoryV5._render_rejection_count and an insertion in _investigator_parts.
- core/pit_optimizer_v5/summary.py: three defaulted fields, count validation and summary computation.

Principal checks freshly passed:
- All submitted protected hashes; expected changed/new function set.
- Scoped Ruff and in-memory compilation.
- Exact-preimage whitespace checks; ordinary CRLF advisories are recorded separately.
- Removing the two production-runtime insertions reconstructs its exact preimage.
- Increment A allowlist and entire episode projection loop are byte-identical.
- 56-file evaluator source identity unchanged.
- Read-only authenticated actual runtime-04 record/role/cleanup evidence.
- Read-only authenticated actual06 journal and a fresh full returned summary.

B4c count semantics:
- failure.rendering.policy_source_unbounded_or_stateful_count: one integer, cumulative over the current checkpoint's authenticated prior-round invalid records with exactly that reserved code. Omit it at zero.
- completed_lifecycle_rounds: distinct checkpoint-record rounds with successful cleanup. Rejection-only/duplicate-only rounds may count; no-record terminal novelty outcomes do not.
- evaluated_feedback_rounds: distinct such completed rounds containing at least one status=evaluated record with campaign evidence. Several candidates in one round count as one round.
- render_rejected_experiments: committed reserved-code invalid records, including those awaiting cleanup.

A completed rejection lifecycle is not an evaluated feedback round. A quick-screen zero_trade result is not a complete discovery evaluation. Keep the pre-existing evaluated_experiments definition unchanged.

## 4. Latest verification limitation — exact state

Luna reported both actual summary calls failing. Fresh principal verification on September 8 narrowed the outstanding issue:

### Actual06 now succeeds

The fresh call to summarize_repository_v5(repository=actual06, manifest=authenticated_manifest, command="summarize") returned:
- status failed; readiness runtime_failed
- role_calls 2; total_tokens 8596; reported cost_usd 0.04234
- experiments 0; evaluated_experiments 0; checkpoint_generation 0
- rounds_seen 1; terminal_rounds 1; cleanup_complete true
- typed_failures ["rendering:stage_failed"]
- new counts 0 / 0 / 0

It matches all 23 old terminal summary keys other than the command label: old run, fresh summarize. The three new keys are exactly the approved additions. This is an honest historical-value comparison, not a same-command before/after claim.

### Runtime-04 remains blocked at historical ownership verification

Direct authenticated record/cleanup facts derive 1 / 1 / 0, but the full summary fails before return:
- V5CliFailure("artifact_graph_invalid")
- caused by ValueError("container execution reservation is foreign")
- source: production_sandbox.py, LocalContainerExecutorV5._load_reservation, repair=False.
- actual command digest comparison passes.
- stored executor digest: 2849cde885cc8bdde38230bc77ecd17163db42db1b071cc5a8a4212ce4601444
- current principal verifier digest: a72e22fc6deeb4b23f65d82ef32abb148fea4ea6db5efecf26c4391e31605bb9

The digests were read from the real exception frame without patching any code or replacing objects. Full traceback function/file/line data is in principal-architect-verification.json.

Source at LocalContainerExecutorV5.__init__ (around line 1521) hashes:
1. manifest SHA
2. sandbox-profile SHA
3. owner SHA
4. mount-factory SHA
5. Docker executable identity
6. control-root directory identity
7. control environment: PATH, SYSTEMROOT, WINDIR, COMSPEC, PATHEXT, SYSTEMDRIVE
8. explicit runtime contract
9. repository-root identity SHA

The exact differing component has not been isolated. Do not assume PATH is responsible, that a reservation is corrupt, or that the two hosts are equivalent. Current identity can differ across legitimate execution contexts; reproducing one context must be based on saved authority, not guessed values.

No environment variable, credential, reservation, record, config or source was altered. No bypass was introduced. The runtime-04 full-summary comparison remains a required Increment C readiness issue.

## 5. Approved implementation history — do not redo it

| Increment | Accepted behavior | Key invariant |
| --- | --- | --- |
| A | Closed 16-entry-funnel-count allowlist projected per parent episode | Four headlines then available integer counts, all evaluator evidence before archive/history; 64 real counts authenticated across four parent episodes. |
| B1 | Frozen RejectedVariantV5 and outcome renderer | Catch only the exact known AST ValueError at candidate bundle construction; strict render_variants still raises; unknown failures remain fatal. |
| B2 | render_rejected payload/event, decoding and journal fold | Accepted author plus complete hypothesis/intent/template/parent/plan and exact bounded assignment authenticate the rejection. Rejected identity is terminal. |
| B2 correction | Full author-hypothesis equality and critic exclusion | Any accepted or failed critic invocation bound to a rejected identity is excluded on append/replay. Other testable identities remain legal. |
| B3a | Separate immutable rejected carrier and aligned record factories | No materialized source/revision for rejection; untestable-only batches need no fabricated critic; testable candidates retain exact critic requirements. |
| B3b | Rejected-record append/load authentication and publication guard | Canonical referenced event, identity, assignment, author, intent and scope must match; only evaluated/zero_trade candidates get source/archive authority. |
| B4a | Runtime outcome routing, durable rejection event creation/reuse | Preserve ordered bounded assignments; rejected carrier bypasses materialization/evaluation; journal references point to actual event files. |
| B4b | Common no-testable finalization and finalized-round authority | Complete origin/record bijection, exact validation/stage evidence and exact required role requests/packages; no hidden critic in no-testable rounds. |
| C1 correction | Parent-identical exact-duplicate record validation | Empty actual changed symbols require both exact_duplicate status and parent-equal identity; all other post-validation records retain declared-symbol equality. |
| B4c | One cumulative rejection metric and three progress counts | Authenticated checkpoint/records only; bounded diagnostic, retained novelty/history, honest lifecycle/evaluation distinction. |

C1 is a correction name from the earlier B4b review. Do not confuse it with the upcoming Increment C tasks, which this document calls C-a through C-e.

Important source relationships:
- StructuralTemplateV5.changed_symbols remains nonempty declared edit scope.
- The real validator derives actual changed symbols from before/after sources. Parent-identical exact duplicates produce () and short-circuit before quick screening.
- ExperimentRecordV5._validate_post_validation_identity permits () only for that conjunctive duplicate case. Full policy binding/identity derivation and _validate_status_evidence remain.
- Invalid records retain pre-validation identity; materialized validation-invalid records may have a different ID than their rendered event. Finalized origin mapping uses assignments/actual validation, not an ID-set shortcut.
- CandidateArchiveReducerV5.apply records novelty and advances next_round_index for all records. Invalid proposals are retained history but ineligible for archive/promotion.
- _completed_result has no independent critic requirement; required critic authority is enforced earlier. Do not add unnecessary changes based on old broad notes.
- Runtime recovery adopts checkpoint-completed rounds before constructing fresh requests. Historical terminal06 must never be restarted or repurposed.
- Resource lease events are campaign-level; do not invent a same-experiment-ID lease issue.

Existing review packages are under:
.artifacts/pit-optimizer-v5/development/luna-recovery-a
luna-recovery-b1
luna-recovery-b2
luna-recovery-b2-correction
luna-recovery-b3a
luna-recovery-b3b
luna-recovery-b4a
luna-recovery-b4b
luna-recovery-c1
luna-recovery-b4c

Use the principal review file within each package when present; implementer architect-verification.json is not automatically principal verification.

## 6. Fixed evaluator, parent and current source identities

Evaluator source:
005064e57e80840f3b2a97d7db694b176832b8f982996e63efc8a35064505ed4
Covered files: 56

Image:
pit-optimizer-v5-evaluator@sha256:4e85b19aa8c135362ae8796e4d33a410ec5af650d296c358ae6a7737d5b05b6a

Profile:
.artifacts/pit-optimizer-v5/evaluator/sandbox-profile-development-005064e57e80.json
File SHA:
0923e93f56e06fc982c9db00815329d1cdc936c0a0b9a140d287b7611ad5b1c9

Parent source bundle:
109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e
Parent policy revision:
5d2a95ba2572e71dab448934544cef22130415fcc1691c9a98deb820d40a1fa9
Development data bundle:
cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de

Four discovery parent baselines plus the quick evaluation are authenticated and cleaned. All four discovery parent episodes had zero trades/return. Host-only recovery edits have not invalidated the evaluator image or those reports. Do not regenerate them merely to review host code. Docker daemon liveness/current inventory was not checked in this review.

Current key source SHA-256 values:
- production_runtime.py a757a0ca89979be9453129321a04b96498fb06a9f37438034b46e407ad8b72eb
- summary.py e4ccb8a2077ca28d2ece2e541839265b3f08218e1b66e93b635772e600270865
- memory.py 37e3a04791d827edd0e5fe99e1d62cb9997163f66954f99727177248f8949d76
- runtime.py d834eae8e3e120355b4479c77f87ebe46519a0b82a52cc072abe1dab60943aec
- artifacts.py 2bab516e4945976d9bf6bb356934289cd14d9b87c25088521a043c8ac5cfa2c2
- provider.py d9b17d62a6c8b485ba6ae6abb22a9e87f9d908b3c96409ba6e21f86eb691e937
- rendering.py bd726b9a32ddaf675a9883aade2099747a8682ab6be5f36d465de77542e93464
- candidate_ir.py a437d9536fb57d48c7e9c9c6fab2f8a29ac9bea0666be61caeb2baf583b13f1b
- contracts.py 5ec3278af501f4abf285716bbd4d211fe5cd34c873e1644b63e3d43daa3de398
- selection.py 208bf033ffbb2222cf5df4d35741f4b562e0c359a9cb5504dce8e846205085d9
- search.py 2f756453123ce834d03cf5fcd2eb57c82781fe228d77e7d4218b600c1b124e3c

The complete principal verification also records current hashes of production_sandbox.py, production_fs.py, production_workspace.py, cli.py, development_preparation.py, operations.py and manifest.py for the next diagnostic boundary. Recompute evaluator_source_map_v5/evaluator_source_sha256 rather than assuming file coverage.

## 7. Real campaign state and remaining budget

The immediate model-driven two-evaluated-round milestone is incomplete. No provider-driven candidate has been evaluated. All six provider campaigns are terminal and cleaned.

| Campaign | Terminal failure | Calls | Tokens | Reported USD |
| --- | --- | ---: | ---: | ---: |
| provider-runtime-01 | Transport failure; original exception unavailable | 1 | 18497 reserved | unconfirmed |
| provider-runtime-02 | Investigator HTTP400 | 1 | 18497 reserved | unconfirmed |
| provider-runtime-03 | Author bare symbol name | 2 | 8115 | 0.036675 |
| provider-runtime-04 | Investigator chose disabled source mode | 1 | 2564 | 0.01921 |
| provider-runtime-05 | Author source lacked final newline | 2 | 8438 | 0.04007 |
| provider-runtime-06 | Accepted author rejected by AST in rendering | 2 | 8596 | 0.04234 |

Campaigns03–06 total 7 calls, 27713 tokens and reported USD 0.138295 under the relevant continuation. These are historical actuals, not current price quotes.

Remaining old grant: 5 calls and 868287 tokens.
The approved conditional replacement required at least 6 calls; therefore replacement07 is ineligible.
No fresh external call or campaign is authorized by remaining tokens alone.

Actual06:
- campaign ID development-provider-two-rounds-20260907-06
- terminal execution session 5015, exit1; never poll or resume it.
- Accepted author used reflection and a generator; the exact pinned AST validator rejected at source bundle construction.
- It has no render_rejected events, experiment records, critic or evaluated candidate. New code does not retroactively alter that fact.

Provider-free runtime-04 is a different repository from provider-runtime-04:
- campaign ID development-controller-two-rounds-20260907-04
- one actual evaluated record round with four discovery episodes and critic/checkpoint/cleanup.
- second round stopped at the deadline following lid sleep.
- It is not two completed evaluated feedback rounds and is not model-driven campaign success.

No current process has been started or resumed by this review. Historical session IDs in plans are not live-process authority.

## 8. Exact real artifacts for permitted verification

### Runtime-04

Root .artifacts/pit-optimizer-v5/development/runtime-04

Manifest:
campaigns/development-controller-two-rounds-20260907-04/manifest.json
SHA 8fcae0423117f3a234e64391c277bd7efe5cbb775ff61ec879d8cbeb5659d37c

Record:
records/9e2a2dfdf35ce55653a3b9ec0d39ce87740a69a940ad5f3e473fd38ea629390d.json
SHA af6c73e632a3dc47cb344ecf3d1a55f9f58ba3e755e29fe992e0c03d0da103ac

Use repository.load_experiment(ArtifactRefV5(...)).
Use record.canonical_json_bytes() and record.sha256. Generic dataclass serialization is not this record's durable encoding.
Round1 has 19 events and three authenticated accepted role packages.
Do not invent a finalized wrapper to exercise the B4b finalizer.

### Actual06

Root .artifacts/pit-optimizer-v5/development/provider-runtime-06

Manifest:
campaigns/development-provider-two-rounds-20260907-06/manifest.json
SHA d90ed717c0d6b34d4cfa6bf7e2dd1f753e68c43b1127b200a66d2a9f930dfdf6

Author request SHA:
09564719a54cfd10a0b6edbc5b61996e7a68015866f1e0b538859c542d158c0f
Accepted author artifact:
roles/artifacts/author/98445c0e22a90e95482d40a77706b77780d2dd1e8e097092a81f9363c020e9f7.json
SHA ad979279cdf09db2bd4ee6250d7b40103db2f5b89749e898dcd6ac234d5f06f2

Round1 event sequence:
role_completion, round_intent, role_completion, round_outcome, cleanup_result.

Important archive files:
- terminal-verification.json — final actual execution, response digests, budget, summary.
- FAILURE-REPORT.md and actual-render-failure.txt — source diagnosis.
- run-01-result.json — actual terminal result.
- preparation-verification.json, CAMPAIGN-REVIEW.md, launch-review.json, campaign-launch-input.json — historical reviewed composition; inspect actual filenames before use.
- post-guidance-summary.json is a wrapper with empty stdout in the inspected state, not an available summary-object baseline. Do not JSON-decode empty stdout.

The old read-only helper luna-recovery-b2/verify_actual06_authority.py has constants useful for inspection, but running its main creates exclusive outputs in the old package. Do not rerun main blindly. Use runpy with a non-main name if loading those constants is needed.

Full parent authority comes from authenticate_campaign_manifest_v5(...).baseline_authority.source_bundle and policy_revision; the author request's editable_sources can be only a subset. Do not reconstruct a four-file parent from that subset.

Prior actual pure rendering produced one RejectedVariantV5 and preserved the strict legacy ValueError. Derived pre-validation ID:
18ea7a802fed21683eef0d31354f6d656e07af576199c27819bdacde25faeb7f
This is inspection evidence, not a committed experiment.

The bounded real record inventory has four entries across runtime-02/03/04: three evaluation_failed and one evaluated, no exact_duplicate or render-rejected record. Do not fabricate one to execute a positive branch.

## 9. Broader data acquisition and vendor branch

The user has Alpaca and FMP, not a licensed historical-constituent provider. The user supplied project contact details for SEC/vendor inquiries. Do not repeat personal contact details in public artifacts; they remain in the private acquisition record.

LSEG/FTSE Russell and Nasdaq coverage/quote inquiries were explicitly authorized and submitted through official forms, with visible receipt/success confirmations. No purchase or subscription occurred. Receipt:
.artifacts/pit-optimizer-v5/data/acquisition/vendor-inquiry-submissions-20260907.json
SHA 087d00327a603fb24802b5b36503d8c3c4cf985e5b82318956434a88cad2f801

Replies have not been checked. A previous automatic reviewer rejected Gmail access because mailbox access had not been separately authorized. Do not turn inquiry authorization into mailbox access or monitoring permission. No reply-monitoring automation exists.

Fresh September8 existence/hash inspection still finds only 2 of 12 final required inputs:
- present: data/source/sp500_membership.csv and data/source/sp500_provenance.json
- absent: Nasdaq100 membership/provenance, Russell2000 membership/provenance, final prices/provenance, fundamentals/provenance and industry/provenance.

Relative to .artifacts/pit-optimizer-v5, the full path list and fresh hashes are in principal-transition-state.json. The target production bundle is not ready.

Outstanding production-data gaps from the acquisition record:
- Complete Russell2000 daily effective membership and dated identifiers.
- Nasdaq100 transient SOLS/GRAL membership dates and complete historical event coverage.
- Full union price/fundamental coverage and lineage; older SEC exports had exclusions/seed gaps.
- Dated SEC SIC lineage and industry/group-rank composition.
- Public ETF holdings must not be substituted for index membership or used to claim complete transient coverage.

The usable provisional development input is development/data/sp500-v2 under the V5 artifact root, scope development_sp500_v2. It enables real simulator/policy work without claiming final three-universe production validity. Earlier recorded contents: 606 membership tickers, 609 priced symbols, 869041 prices, 142329 fundamentals and exact reference calendars. Those size facts come from the prior source-bound integration record, not a new full row scan in this review.

The user explicitly prioritized optimizer wiring while vendor data is pending. Continue that development scope rather than block all engineering on vendors. Keep unavailable industry explicit; do not relabel development data as production.

Original long-term plans:
- docs/superpowers/plans/2026-09-04-pit-optimizer-v5-strategy-universe.md
- docs/superpowers/plans/2026-09-04-pit-optimizer-v5-evaluator-truth.md
- docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md
- docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md

Confirmation, qualification and replay-readiness source were implemented earlier, but no successful production promotion/held-out qualification/replay/performance claim follows from the current development evidence. They remain separate later work.

## 10. Increment C-a — next session, read-only diagnosis first

**Owner:** principal architect in the new session. If work is handed to Luna later, issue a bounded task after the diagnosis identifies the exact correction. Do not start broad autonomous source edits.

**Deliverable:** an authenticated historical-verifier identity comparison and a concrete decision for runtime-04; zero campaign/model/evaluation calls.

1. Confirm worktree/branch/HEAD and key source hashes above. Do not rereview every approved increment.
2. Read the B4c principal failure chain and current executor-identity composition. Re-run one read-only summary only if needed to establish the new session's actual result; executor hashes are context-sensitive.
3. Preserve separate observations for runtime-04 and actual06. Actual06 passed in the principal context; do not keep claiming both are blocked based only on Luna's older report.
4. Follow authenticate_development_history_v5 to the original saved CampaignLaunchV5, adapter config and _verify_local_run. Authenticate referenced files before interpreting them.
5. Compare the exact stored reservation command/owner/executor bindings with the verifier inputs. Record identity digests and component-equality results, not credential values or full environment dumps.
6. Look for existing saved launch/environment/host metadata and original execution helpers. Compare manifest, profile, owner, mounts, tool file identity, directory/repository identity and runtime-contract versions. The identity digest is not reversible; absence of a captured component must be documented.
7. Do not repair a reservation, substitute the current executor digest, delete a foreign-state guard, monkeypatch verification, invent owner tokens, migrate an archive or adjust environment variables until a documented legitimate reproduction or reviewed design exists.
8. If the original authenticated execution context is available, prepare the exact read-only reproduction command using it. Preserve all original immutable manifest/config references. Run only the read-only summary; no campaign resume or cleanup/reconciliation mutation.
9. If original context cannot be reconstructed, write a narrow proposed historical-verification design that preserves ownership and live execution guards. State the evidence required to authenticate historical completion independently of a newly constructed executor. Return for an architect decision before implementing; do not weaken _load_reservation globally.
10. Save the diagnostic under a fresh .artifacts/pit-optimizer-v5/development/increment-c-history-verifier package. Include checked hashes, actual success/failure, unresolved component(s), scope of any proposed correction and why it is necessary.

Exit criteria: either a full runtime-04 read-only summary returns and its old values/new counts are verified, or a precise architectural correction is prepared for review. Until then, campaign readiness remains held. This plan explicitly preserves B4c source acceptance while carrying the verification gap.

## 11. Increment C-b — readiness consolidation after C-a

1. Apply only any separately reviewed correction, preferably with a narrow Luna prompt and checkpoint; no tests/synthetic evidence under current restrictions.
2. Recompute the full evaluator map. If a covered file changes, stop and decide whether a new image/baseline is necessary; never rebind old reports. Host-only changes should reuse the fixed image/baselines.
3. Re-authenticate the exact parent four discovery episodes and quick result references, development bundle, execution profile and policy source. Use existing sealed artifacts, not new evaluations.
4. Inspect the real archived author through pure rendering only if new changes justify it; the expected closed rejection is already established.
5. Verify next-investigator metric order and request-size bounds using only existing authenticated real inputs. Do not construct a fake future round merely to obtain execution coverage.
6. Reconcile runtime completion, novelty exhaustion, completed_lifecycle_rounds and evaluated_feedback_rounds. operations.py's max_feedback_rounds currently limits lifecycle attempts; it is not automatic proof of two evaluated rounds.
7. List remaining unexecuted positive branches honestly. No hidden test/synthetic run is allowed to fill the gap.
8. Produce a readiness review with exact source/data/image/profile/parent hashes, current summary results, permitted evidence and material unresolved risks. Do not claim readiness merely because static checks pass.

## 12. Increment C-c — concrete future campaign proposal, not execution

The original remaining grant cannot cover its approved minimum. Prepare a new explicit proposal after readiness, then obtain a fresh user grant as the final step.

The proposal must include:
1. Distinct fresh campaign ID and artifact root; no reuse/restart/resume of terminal01–06 and no automatically presumed replacement07.
2. Exact manifest, adapter config, outbound source paths/contracts and aggregate evidence/history packets, with hashes and size accounting. No raw market rows or secrets.
3. Finite lifecycle-attempt cap and a success rule requiring at least two distinct real evaluated feedback rounds with critic, checkpoint and cleanup evidence.
4. Explicit call/token reserve for investigator/author/critic across those rounds, plus how many rejection-only attempts are affordable. Rejected rounds consume investigator/author calls even when no critic runs.
5. A single coherent total-call and token budget, repair/retry behavior and dollar estimate based on verified then-current pricing if a cost quote is given. Historical spend is not a future price guarantee.
6. Exact saved launch command and execution environment; local tool identities, persistent session handling, wall deadline and cleanup authority.
7. Stop conditions: no further calls past cap/deadline, unavailable credit/credentials, unexpected authority mismatch, cleanup failure, novelty exhaustion or insufficient reserve to finish the promised work.
8. Explicit separate user approval for that concrete paid run. Do not repeatedly ask about already authorized packet categories; ask only for the new execution/budget grant actually needed.

Prepare everything reviewable before requesting approval. Do not send a paid request while preparing the proposal.

## 13. Increment C-d — only after fresh explicit run authorization

Use the exact approved launch once, in its approved environment. Keep the machine on AC and lid open; the awake guard does not prevent explicit lid sleep. Track the single actual execution session. Observation timeout does not authorize a duplicate launch.

After each completed lifecycle:
- confirm durable intent/accepted roles/origin events and actual record bindings;
- report rejected versus evaluated counts separately;
- verify critic only for testable candidates and truthful absence for rejected-only rounds;
- check actual executed candidate episodes and checkpoint/cleanup;
- verify the next investigator receives authenticated rejection/history feedback if a real rejection occurred;
- ensure the original deadline and remaining paid budget are not silently refreshed.

An authorized real campaign may finally exercise rejection/duplicate/finalizer paths, but it is not permission to generate synthetic cases. Do not count a failed or rejection-only lifecycle toward the two-evaluated-round milestone.

## 14. Increment C-e — requested post-success Luna implementation handoff

Only after two real evaluated feedback rounds are evidenced:
1. Summarize actual candidate changes, evaluated windows, returns/trades/exposure and critic findings with precise provenance.
2. Separate development evidence from production/three-universe claims.
3. Prioritize the next improvements using real diagnostics; the parent zero-trade funnel suggests investigation but does not establish causality.
4. Produce a detailed small-step Luna plan with exact file/method scope, contracts, preconditions, check commands permitted by the user, stop conditions and architect checkpoints.
5. Keep architectural decisions with the principal; the user continues to create sessions manually and returns changes for review.

This is the original requested final handoff. Do not confuse it with the preparatory recovery documents already saved.

## 15. Practical tool and review notes

- Tools run PowerShell; use explicit workdir and absolute CLI --artifact-root.
- Prefer rg/rg --files, excluding tests. Windows shell expansion does not turn core/.../*.py into rg file arguments; use rg over a directory with --glob.
- Use py -3.13 -B -m ruff and in-memory compile. No package install is needed.
- Read/write text explicitly as UTF-8; CP1252 read_text() can fail on the plans.
- Source contains mixed CRLF/LF. Compare AST/functions with normalized text where appropriate but preserve/hash exact bytes. Avoid blanket formatting.
- Exact preimages in C1/B4c end with .py.preimage, not simply .py.
- Use canonical record encoding rather than generic dataclass encoding.
- Before/after summary comparison must use real returned objects and preserve the command-label distinction. Archive-derived counts are not a substitute for returned-summary evidence.
- Capture only compact relevant JSON fields; some artifacts are huge single lines.
- Do not execute old verification helpers that create files in old packages or old scripts that launch a campaign.
- The current principal review changed documentation only; all production source hashes remained pinned.

## 16. Resume instruction

Read this file and the linked B4c principal review/verification in the architecture worktree. Continue as principal architect with Increment C-a only: diagnose runtime-04's historical executor-identity mismatch read-only, preserving B4c approval and all authority guards. Return the concrete finding or narrow correction design before production changes or campaign preparation. No tests, synthetic artifacts, credentials, external calls, evaluations, campaigns or commits.
