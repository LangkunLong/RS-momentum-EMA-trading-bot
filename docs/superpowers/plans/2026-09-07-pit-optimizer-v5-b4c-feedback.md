# V5 B4c rejection feedback and progress accounting implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this bounded plan task by task in the user's manually created Luna session. Follow the user's no-tests, no-synthetic-probes, no-campaign and no-commit restrictions over generic skill defaults. Return for principal review after B4c.

**Goal:** Give the next investigator authenticated numeric source-rejection feedback and distinguish completed record lifecycles from real evaluated feedback rounds.

**Architecture:** Keep existing records, journal authority, runtime completion and novelty reduction unchanged. Project one closed cumulative count from checkpoint-authorized records into the existing evidence envelope. Add three clearly defined counts to the host operator summary; do not change its existing fields or status meanings.

**Tech stack:** Python 3.13, existing V5 typed records/repository, scoped Ruff and static source review.

**Spec:** docs/superpowers/plans/2026-09-07-pit-optimizer-v5-recovery-design.md and the B4 requirements in docs/superpowers/plans/2026-09-07-pit-optimizer-v5-luna-recovery.md.

## Global constraints and starting authority

Worktree: C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture.

Principal review: .artifacts/pit-optimizer-v5/development/luna-recovery-c1/principal-architect-review.md.
Independent hashes/evidence: .artifacts/pit-optimizer-v5/development/luna-recovery-c1/principal-architect-verification.json.

C1 is approved and the B4b hold is cleared. B4c is the next implementation scope only. No runtime, finalizer, rendering, identity or evaluator changes are authorized by this plan.

Only production files allowed to change:
- core/pit_optimizer_v5/production_runtime.py: one count helper on LocalRoleRequestFactoryV5 and one insertion in _investigator_parts.
- core/pit_optimizer_v5/summary.py: three defaulted summary counts, their validation, and their computation from existing authenticated records and cleanup evidence.

Preserve all other source hashes in the principal verification. In particular:
- memory.py: 37e3a04791d827edd0e5fe99e1d62cb9997163f66954f99727177248f8949d76
- runtime.py: d834eae8e3e120355b4479c77f87ebe46519a0b82a52cc072abe1dab60943aec
- artifacts.py: 2bab516e4945976d9bf6bb356934289cd14d9b87c25088521a043c8ac5cfa2c2
- provider.py: d9b17d62a6c8b485ba6ae6abb22a9e87f9d908b3c96409ba6e21f86eb691e937

Starting editable files:
- production_runtime.py: 78da0dc5efcf5cf94541a465e0a3c212221c843dade07f680ddc5e1508fdc476
- summary.py: 4eaef3424a6043a5ce0aeef60bf0a8abe444d4a24588d93d26804d44a3127258

Evaluator identity must remain 005064e57e80840f3b2a97d7db694b176832b8f982996e63efc8a35064505ed4 across 56 covered files. Reuse the pinned image and existing parent baselines. Do not rebuild, reseal, rebaseline or edit candidate_ir.py/contracts.py.

No tests may be read, created, modified or run. No synthetic candidates, records, journal events, wrappers, templates, provider responses, mocks or fabricated authority. No credentials, model/API calls, evaluations, campaign run/resume, commits, pushes, trading or deployment. Do not create a new Codex task automatically. Historical artifacts are read-only.

## Task 1: Capture the boundary and follow existing authority

**Files:** Create only the local review package .artifacts/pit-optimizer-v5/development/luna-recovery-b4c and documentation; read the two editable files and narrowly referenced consumers.

- [ ] Compare the starting hashes before any production edit. Save exact preimages under the new package. If a package already exists, preserve its contents and inspect the work instead of overwriting it.
- [ ] Read LocalRoleRequestFactoryV5._investigator_parts and provider.py's _role_citation_sequence, _ALLOWED_EVIDENCE_METRIC_PREFIXES and investigator input validation.
- [ ] Confirm failure.* is already permitted. The investigator citation sequence is aggregate_evaluator_evidence, archive families, critic directions, campaign directions, experiment summaries. Do not edit provider.py or add a provider schema field.
- [ ] Read SearchProjectionV5.__post_init__, _Runtime._recover_projection and LocalArtifactRepositoryV5.load_experiment. Projection references match its checkpoint; runtime recovery loads real records; load_experiment authenticates the complete B3b render-rejection event/author/intent bindings for the reserved code.
- [ ] Read summary.py's current record loading, verify_projection call, cleanup map and evaluated selection. Its existing evaluated_experiments counts only status evaluated with campaign evidence. A quick-screen zero_trade result is not a completed discovery evaluation.

Write down these definitions before implementation:
- Count unit for rejection feedback: one committed, authenticated invalid experiment record whose validation.failure_code is exactly policy_source_unbounded_or_stateful.
- Scope: all records in the current checkpoint used by this investigator request, including records omitted from the bounded text history.
- Do not count a template, role failure, raw exception, attempted assignment, pure renderer preview, historical terminal rendering/stage_failed, or uncommitted event.
- Missing rejection records means zero. To preserve existing request bytes where the new diagnostic is unnecessary, omit the new investigator metric when the count is zero.

## Task 2: Project one authenticated rejection metric

**File:** core/pit_optimizer_v5/production_runtime.py.
**Consumes:** FeedbackRoundInputV5, SearchProjectionV5, LocalArtifactRepositoryV5.load_checkpoint/load_experiment.
**Produces:** A host-derived integer diagnostic inside the existing investigator evidence envelope.

- [ ] Add this small method to LocalRoleRequestFactoryV5, adjacent to _investigator_parts. Keep error text fixed and content-free. Equivalent scoped lint formatting is allowed.

~~~python
    def _render_rejection_count(
        self,
        inputs: FeedbackRoundInputV5,
        projection: SearchProjectionV5,
    ) -> int:
        if self._repository.load_checkpoint() != projection.checkpoint:
            raise ValueError("investigator rejection checkpoint differs")
        count = 0
        for stored in projection.stored_records:
            record = self._repository.load_experiment(stored.reference)
            if (
                record != stored.record
                or record.round_index >= inputs.round_index
                or record.experiment_identity.discovery_plan_sha256
                != inputs.panel_plan.discovery_plan_sha256
                or (record.pit_data_scope, record.semantic_mode)
                != (inputs.manifest.pit_data_scope, inputs.manifest.semantic_mode)
            ):
                raise ValueError("investigator rejection record authority differs")
            if (
                record.status == "invalid"
                and record.validation.failure_code
                == "policy_source_unbounded_or_stateful"
            ):
                count += 1
        return count
~~~

The existing typed SearchProjectionV5 enforces checkpoint reference membership/order; the helper checks the actual current checkpoint and reloads every referenced record. The repository, rather than a duplicated event decoder here, proves the reserved-code record's rejection authority. Loading failures must propagate; do not convert authentication failures into zero or skip a malformed record.

The prior-round check applies when constructing a fresh investigator or author request through _investigator_parts. Existing runtime recovery adopts completed rounds and returns historical terminal outcomes before constructing new requests. Preserve that flow. Do not add an alternate checkpoint-prefix path or regenerate requests for old completed rounds.

- [ ] In _investigator_parts, after the entire parent episode loop and before the families list, insert:

~~~python
        rejection_count = self._render_rejection_count(inputs, projection)
        if rejection_count:
            evaluator_ids.append(
                evidence.add(
                    "failure.rendering.policy_source_unbounded_or_stateful_count",
                    rejection_count,
                )
            )
~~~

This uses the existing aggregate evidence citation lane. The failure.* namespace identifies a host rejection diagnostic; it is not evaluator performance or a claim about a particular forbidden syntax construct. Keep its value an integer and its ID a literal. Export no source, path, symbol, exception text or timestamp.

- [ ] Preserve the Increment A 16-metric allowlist and the entire existing episode loop byte-for-byte. Each episode still has its four headlines followed by its available funnel counts. The new item appears only after every episode, before any archive/history evidence. Do not splice it into an individual episode or append it after summaries.
- [ ] Leave all old evidence.add calls and their relative ordering unchanged. Positive-history future requests gain one deterministic ID and later IDs shift consistently. A zero-count request gains no item. Never update historical saved requests to match future IDs.
- [ ] Keep the author path unchanged: it calls _investigator_parts and reissues selected hypothesis evidence from the same authenticated checkpoint. Preserve exact hypothesis citations and their binding.
- [ ] Leave existing byte budgets and memory projection policy unchanged. This adds at most one evidence item regardless of history length. If an existing real preview exceeds a configured request limit, report the exact size and limit; do not raise it or remove required history.
- [ ] Do not add per-record rejection diagnostics, new evidence arrays, provider prompts or any other metric in this increment.

This explicitly permits the new helper/insertion within the previously protected production-runtime region. The old whole-region SHA will change; separately prove the allowlist, episode loop and all unrelated functions are preserved. C1 and B3a record construction remain untouched.

## Task 3: Add explicit operator progress counts

**File:** core/pit_optimizer_v5/summary.py.
**Consumes:** Existing authenticated checkpoint records, existing cleanup payload map, successful verify_projection.
**Produces:** Three additive host-summary integers. This explicitly authorizes this ephemeral output extension; it does not change any durable record/event/request schema.

- [ ] Append these defaulted fields after the current last field in OptimizerSummaryV5. Existing positional parameters must retain their positions.

~~~python
    completed_lifecycle_rounds: int = 0
    evaluated_feedback_rounds: int = 0
    render_rejected_experiments: int = 0
~~~

- [ ] Add the three values to the existing strict nonnegative-int validation tuple. Reject bool as the current count validator already does.
- [ ] Add only these cross-count consistency checks with one fixed error message:

~~~python
        if (
            self.completed_lifecycle_rounds > self.rounds_seen
            or self.completed_lifecycle_rounds > self.experiments
            or self.evaluated_feedback_rounds > self.completed_lifecycle_rounds
            or self.evaluated_feedback_rounds > self.evaluated_experiments
            or self.render_rejected_experiments > self.experiments
        ):
            raise ValueError("optimizer summary progress counts are inconsistent")
~~~

Zero defaults keep unavailable_summary_v5 and older positional construction valid. Do not require all existing evaluated_experiments to have cleanup; a checkpoint may exist before cleanup succeeds.

- [ ] In summarize_repository_v5, after its existing record authentication and verify_projection have succeeded, compute:

~~~python
    record_rounds = {record.round_index for record in records}
    completed_record_rounds = {
        round_index
        for round_index in record_rounds
        if round_index in cleanups and cleanups[round_index].cleanup_complete
    }
    evaluated_record_rounds = {record.round_index for record in evaluated}
    render_rejected_experiments = sum(
        1
        for record in records
        if record.status == "invalid"
        and record.validation.failure_code
        == "policy_source_unbounded_or_stateful"
    )
~~~

- [ ] Pass the new fields as keywords at the end of the existing return constructor:

~~~python
        completed_lifecycle_rounds=len(completed_record_rounds),
        evaluated_feedback_rounds=len(
            completed_record_rounds & evaluated_record_rounds
        ),
        render_rejected_experiments=render_rejected_experiments,
~~~

Definitions:
- completed_lifecycle_rounds counts distinct checkpoint-record rounds with successful cleanup. It includes rejection-only, duplicate-only and mixed record rounds. A terminal no-novel outcome with no records does not increment this count; the existing terminal_rounds retains its separate meaning.
- evaluated_feedback_rounds counts distinct such completed record rounds containing at least one actual status=evaluated record with campaign evidence. Multiple evaluated candidates in one round still count as one round. Record decoding preserves mandatory critic and campaign evidence; no status-only fabricated evidence is admitted.
- render_rejected_experiments counts the committed reserved-code invalid records, even if cleanup is pending. It does not relabel a rejected record as evaluated or mark the whole campaign failed.

- [ ] Preserve all existing summary fields, status/readiness rules, typed_failures taxonomy, evaluated_experiments definition, performance/champion selection, role-call/token/cost totals and cleanup semantics. Do not change operations.py or claim that its existing max_feedback_rounds lifecycle stop proves two evaluated rounds.
- [ ] Preserve canonical serialization of all historical authority files. Newly generated summaries contain three additional keys; old saved summary files stay byte-identical. No schema-version bump or migration.

## Task 4: Review retained history and novelty without changing it

**Read-only source:** memory.py project_investigator_memory_v5/feedback constructors; search.py CandidateArchiveReducerV5.apply and hypothesis_novelty_key_v5; production_runtime.py _authorities/_scheduling_history/_investigator_parts; selection.py parent and hypothesis selection.

- [ ] Trace every record through CandidateArchiveReducerV5.apply: next_round_index advances and attempted_novelty_keys retains the hypothesis/parent novelty key even when its record is invalid.
- [ ] Confirm _authorities still skips invalid and duplicate records; they get no materialized source authority, performance ranking, archive promotion or selected-parent lineage.
- [ ] Confirm existing memory retains invalid status and the failed hypothesis when it fits its established complete-feedback budget; compact/omitted text uses the existing bounded rules. The new cumulative diagnostic does not depend on whether a particular text summary fits.
- [ ] Confirm untestable records never demand a critic in _investigator_parts; existing review-is-None skips remain. Other testable records retain exact authenticated critic packages and reissued citations.
- [ ] Do not delete history, reset novelty, change parent-selection rules, expand budgets or erase proposals to force a retry.

If source review finds a concrete contradiction requiring changes outside the two allowed production files, document the exact path and stop for a principal decision. Do not silently widen the implementation.

## Task 5: Verify using permitted real evidence and return

- [ ] Run:
~~~powershell
py -3.13 -B -m ruff check core/pit_optimizer_v5/production_runtime.py core/pit_optimizer_v5/summary.py
py -3.13 -B -c "from pathlib import Path; [compile(Path(p).read_bytes(), p, 'exec') for p in ('core/pit_optimizer_v5/production_runtime.py', 'core/pit_optimizer_v5/summary.py')]"
~~~
- [ ] Generate exact-preimage diffs with repository-relative labels and run git diff --no-index --check against each preimage. Distinguish ordinary CRLF advisories from actual whitespace errors. No blanket formatter.
- [ ] Compare AST/function scopes. Expected changed existing methods: LocalRoleRequestFactoryV5._investigator_parts, OptimizerSummaryV5.__post_init__, summarize_repository_v5; new helper LocalRoleRequestFactoryV5._render_rejection_count and the three appended dataclass fields. No unrelated production functions change.
- [ ] Recompute every protected hash and the 56-file evaluator map/digest. Hash the preserved Increment A allowlist/episode block separately against the exact preimage. Do not claim the entire old projection region is byte-identical after this authorized insertion.
- [ ] Read-only authenticate runtime-04's real evaluated record:
  - root .artifacts/pit-optimizer-v5/development/runtime-04
  - manifest campaigns/development-controller-two-rounds-20260907-04/manifest.json, SHA 8fcae0423117f3a234e64391c277bd7efe5cbb775ff61ec879d8cbeb5659d37c
  - record records/9e2a2dfdf35ce55653a3b9ec0d39ce87740a69a940ad5f3e473fd38ea629390d.json, SHA af6c73e632a3dc47cb344ecf3d1a55f9f58ba3e755e29fe992e0c03d0da103ac
  - use record.canonical_json_bytes()/record.sha256, not generic dataclass serialization; this record has a custom durable encoding.
  - preserve evaluated status, four discovery episodes and exact critic binding.
- [ ] Read-only authenticate actual06:
  - root .artifacts/pit-optimizer-v5/development/provider-runtime-06
  - manifest campaigns/development-provider-two-rounds-20260907-06/manifest.json, SHA d90ed717c0d6b34d4cfa6bf7e2dd1f753e68c43b1127b200a66d2a9f930dfdf6
  - its five events remain role_completion, round_intent, role_completion, round_outcome, cleanup_result; rendering/stage_failed, cleanup complete.
  - it has no checkpoint records or render_rejected events. Do not turn the archived author response into new evidence.
- [ ] Obtain before/after summaries from summarize_repository_v5 only for those two authenticated real repositories; command='summarize'. This function is a local read-only projection. Do not use run, resume, run-fixture or a provider constructor. Save the resulting review previews under the B4c package, without overwriting historical summaries.
  - Every pre-existing summary key/value must match its before image.
  - actual06: all three new counts zero; its old runtime failure label persists.
  - runtime-04: derive counts from its actual checkpoint/cleanup. Its one completed evaluated record round should give lifecycle=1, evaluated-feedback=1, render-rejected=0. Report a mismatch from actual evidence instead of adjusting it.
- [ ] No real render-rejected record or exact-duplicate record exists in the principal's bounded four-record inventory. Unless a newly discovered real committed record can be authenticated, the positive rejection metric, rejection-only/mixed summary branches and C1 positive branch remain static-reviewed, unexecuted.
- [ ] Do not manufacture SearchProjectionV5, FeedbackRoundInputV5, records, finalized wrappers or a fake later round to exercise the helper. An existing authenticated real input may be inspected or projected locally if already available; no live runtime or write-producing recovery method may be invoked.
- [ ] Include a static decision table covering no records, ordinary invalid, closed render rejection, duplicate-only, mixed evaluated/rejected, several evaluated candidates in one round, pending cleanup and old actual06 terminal failure. Label this table as source reasoning, not executed cases.

Save preimages, narrow diffs, final hashes, scope comparison, preserved-A evidence, real summary before/after comparisons, static history trace and verification limits. Return architect-handoff.md and architect-verification.json in the B4c package.

Stop at the principal checkpoint after B4c. Increment C readiness review and any new campaign proposal remain later work. The old remaining budget is five calls / 868287 tokens, below the replacement's six-call minimum; this implementation instruction grants no new external calls. The original goal still requires two real evaluated feedback rounds and the requested post-success Luna handoff remains after that evidence exists.
