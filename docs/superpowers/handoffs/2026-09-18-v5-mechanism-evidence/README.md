# Codex Start Here: V5 Mechanism Evidence

**Date:** 2026-09-18  
**Revision:** 2 — incorporate latest-main feedback-loop review  
**Status:** Documentation-only handoff. Implementation and runtime verification remain pending.  
**Reviewed source:** `15ba962743da2c2ca73becdf85bc639c4f670dfa`.

## What changed after PR #60

The owner requested a deeper review of latest main and comparison with GLM's experiment-driven infrastructure work. That review confirms that V5 already retains experiment evidence and actually sends authenticated critic feedback into later investigator requests. The earlier conversational claim that a new generic hypothesis-memory subsystem was missing was incorrect.

The revised work improves the content of that feedback and proves its round-to-round delivery. It does not replace the investigator, author, critic, archive, or evaluator.

The biggest acceptance change is in Task 5: a critic report alone no longer completes the evidence work. The measured finding, applicability, and limitations must also appear in the next investigator's actual bounded request, including after memory compression and restart.

## Reading order

1. Read repository guidance and the [preserved September 9 checkpoint](../2026-09-09-pit-optimizer-v5/README.md).
2. Read the [source review and GLM comparison](../../reviews/2026-09-18-v5-glm-loop-review.md). It separates code findings, design risks, and unverified claims.
3. Read the revised [design](../../specs/2026-09-18-v5-mechanism-evidence-design.md).
4. Follow the revised [implementation plan](../../plans/2026-09-18-v5-mechanism-evidence.md), beginning with Task 0.

The reviewed main includes archived local-history snapshots. Treat active `core/pit_optimizer_v5/` code as the implementation, not similarly named archived `.reference` files.

## Existing components to reuse

```text
ExperimentRecordV5 + checkpoint
    -> project_investigator_memory_v5
    -> LocalRoleRequestFactoryV5._investigator_parts
    -> authenticated reissued evidence + critic/campaign directions
    -> InvestigatorRoleInputV5
    -> model-ranked hypotheses
    -> deterministic novelty selection
    -> persisted RoundIntentPayloadV5 before authoring
    -> bounded author/variants/evaluation/critic
    -> next record and request
```

Do not add another generic memory system. Bind the new operational mechanism spec at the existing hypothesis boundary and project conditional measured findings through the existing request path.

## Boundaries and priorities

Preserve strategy logic, canonical entry rules, evaluator truth, cost assumptions, CAGR ranking, fingerprints, early rejection, old serialized artifacts, and provider limits. Keep exact authored-parent comparisons separate from campaign-baseline comparisons.

Keep measured support/contradiction/insufficiency distinct from critic interpretation and execution failure. Retain low-frequency measurements named by a hypothesis, explicit missingness, and scenario/episode context. Do not lose a negative qualifier when compacting a finding.

The saved development campaign remains paused with semantic checks explicitly disabled. This review does not enable them, run tests, invoke providers, inspect market datasets, access qualification, or resume old grants. Later implementation must resolve the relevant execution/test restrictions explicitly. No orders or deployment are in scope.

The first implementation slice uses supplied authorized evidence and bounded offline checks. It does not require an LLM call. Information reaching a request is not proof that the model uses it well; that is a separate future experiment.

## Delegation

One owner reconciles current source, permissions and contracts. Separate probe and report agents may then work on their own new files. A single integrator owns existing runtime/provider/memory/artifact seams and Task 5's end-to-end trace. An independent reviewer checks compatibility, provenance, final-packet budgets and the exact diff. Avoid parallel edits to shared adapters.

## Suggested owner-to-Codex instruction

> Read this revision-2 handoff, the source review, design, and implementation plan. Start with Task 0 and reconcile the actual checkout, scoped file map, and remaining authorization restrictions. Reuse V5's existing hypothesis memory and feedback pipeline. Build the evidence-only extension in the documented dependency order. Completion must demonstrate the measured finding in the next investigator request after persistence, compaction and restart, not only in a critic report. Do not change strategy, evaluator, ranking, qualification, or broker behavior. Do not run tests, synthetic trials, providers, or saved campaigns merely because this plan describes them; obtain the necessary bounded authorization first.

## Review limitations

The review traced selected active source paths and read the three original planning documents. It did not inspect project test files, run project code, verify campaign outcomes, or measure model/strategy performance. The GLM comparison uses accessible primary-source material; the full blog and unpublished experiment harness were not available for complete reproduction. See the review's source limitations.

Publication details belong to the new documentation PR. PR #60 remains the historical initial plan; this revision is not evidence that its implementation has started or completed.
