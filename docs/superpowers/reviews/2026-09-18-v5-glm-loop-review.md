# V5 recursive experiment loop: source review and GLM comparison

**Date:** 2026-09-18  
**Reviewed main:** `15ba962743da2c2ca73becdf85bc639c4f670dfa`  
**Review type:** Static source and design review. No implementation, project tests, model calls, backtests, or campaign execution.

## Conclusion

V5 already has an implemented evidence-memory-to-hypothesis feedback path. It does not merely save reports. The earlier conversational suggestion that a separate generic hypothesis memory was missing was incorrect.

The useful improvement is narrower: make the mechanism measurements, their applicability, and their limitations survive the complete path from an experiment to the next investigator request. The original mechanism-evidence plan was useful but its acceptance criterion stopped too early, at a critic request. The revised plan explicitly includes the next investigator's actual bounded request.

This review examines the relevant paths, not every repository file or every possible runtime composition. Source wiring establishes that information can be supplied; it does not establish that an LLM uses it well or that a strategy generalizes.

## 1. Evidence from the current source

All links below are pinned to the reviewed main, not archived local-history copies.

| Source inspected | Finding |
| --- | --- |
| [memory.py](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/memory.py) | `ExperimentRecordV5` retains the hypothesis, template, evidence, status, and bound critic review. `project_investigator_memory_v5` builds complete feedback and compact summaries from checkpoint-authorized records. |
| [production_runtime.py, role construction](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/production_runtime.py#L630-L850) | `_investigator_parts` loads authenticated prior investigator/critic artifacts, reissues cited measurements, and constructs critic directions, campaign directions, and experiment summaries. |
| [provider.py, investigator inputs](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/provider.py#L950-L1050) | `InvestigatorRoleInputV5` already includes evaluator evidence, archive families, critic directions, campaign directions, and experiment summaries. |
| [provider.py, request and response binding](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/provider.py#L1320-L1530) | Requests authenticate issued evidence and enforce exact citation order. Hypotheses contain a causal claim, directional predictions, evidence IDs, author instructions, and authoring mode. |
| [runtime.py, round sequence](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/runtime.py#L2980-L3180) | The investigator proposes; novelty selection chooses; `RoundIntentPayloadV5` records the hypothesis before authoring; local evaluation precedes the batch critic and checkpoint publication. |
| [runtime.py, recovery](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/runtime.py#L1250-L1350) | Recovery reconstructs stored experiments from checkpoint references rather than accepting an arbitrary memory directory. |
| [production_runtime.py, critic evidence](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/production_runtime.py#L900-L1200) | The critic receives richer metrics, matched parent deltas, scenario groupings, predictions, typed failures, and bounded semantic differences when enabled. |
| [selection.py](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/selection.py#L600-L670) | `select_novel_hypothesis_v5` selects the first model-ranked hypothesis whose controller novelty key has not been attempted for that parent. |
| [production_provider.py](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/production_provider.py#L1-L240) | Provider accounting and response reconciliation are separate from scientific interpretation; they enforce request, model, usage, and budget identities. |

### Actual round-to-round path

```text
checkpoint-authorized experiments + exact selected parent
    -> project_investigator_memory_v5
    -> _investigator_parts
       (authenticated prior hypotheses, critic directions, reissued evidence)
    -> InvestigatorRoleInputV5 / bound request
    -> model generates and ranks hypotheses
    -> first controller-novel hypothesis
    -> persisted RoundIntentPayloadV5
    -> author produces bounded StructuralTemplateV5
    -> local variants / validation / semantic stage / quick / discovery
    -> batch critic compares predictions with supplied results
    -> immutable ExperimentRecordV5 + checkpoint
    -> next investigator request
```

The model generates the hypothesis content and ranking. The controller supplies evidence, validates output and citations, applies deterministic novelty rules, and controls execution. A valid citation proves the measurement was supplied; it does not prove the hypothesis correctly interprets that measurement.

## 2. What can be established about GLM

Primary sources consulted on 2026-09-18:

- [Z.ai's article](https://z.ai/blog/glm-built-its-inference-infrastructure).
- [Z.ai's official announcement](https://www.linkedin.com/posts/zdotai_toward-recursive-self-improvement-how-glm-activity-7506247487133892608-VAsS).

The full article did not render in the retrieval environment. The comparison therefore uses indexed passages from Z.ai and the readable official announcement, not a recovered complete experiment log. The indexed account assigns objectives and boundaries to engineers and analysis, hypotheses, and code changes to the agent. It also describes incremental and ablation experiments used to derive reusable optimization skeletons with applicability conditions. The announcement emphasizes local correctness checks, execution traces, microbenchmarks, and end-to-end measurements instead of aggregate performance alone.

Those sources support borrowing experimental discipline. They do not reveal an exact reusable prompt, a complete hypothesis-ranking algorithm, every human intervention, or the full agent harness. This review does not claim an exact replication of that system or demonstrate autonomous model-weight self-improvement.

### Comparison and proposed adaptation

| Dimension | V5 source finding | Adaptation, not a claim about unpublished GLM internals |
| --- | --- | --- |
| Hypothesis generation | An investigator model already proposes ranked, cited hypotheses. | Improve the evidence it receives; do not replace it with a human-authored list of all possible strategy changes. |
| Focused experiments | Fixed semantic probes and bounded quick/discovery evaluation exist. | Add precommitted, change-specific paired observations and negative controls alongside them. |
| Feedback reuse | Authenticated critic feedback already reaches later requests. | Preserve measured mechanism results even when the critic does not choose to cite every relevant observation. |
| Reusable knowledge | Full records, lineage, summaries, and author templates exist. | Project conditional findings from these records. An unevaluated author template is not automatically a validated lesson. |
| Iteration granularity | The reviewed runtime evaluates a bounded candidate batch before its critic. | Do not describe it as an unrestricted, interactive diagnostic tool loop. A later diagnostic-request protocol would require separate design and budgets. |
| Objective | V5 evaluates historical portfolio behavior. | Keep simulator truth fixed. Better explanation of historical behavior is not evidence of future returns. |

## 3. Findings requiring plan changes

### R1. Reuse memory and precommitment; do not build them again

**Established:** `ExperimentRecordV5` and `_investigator_parts` implement actual feedback reuse. The selected hypothesis is already journaled before authoring.

**Change:** Bind the new mechanism specification at the existing round-intent boundary. Extend the evidence supplied through the existing request factory. Keep old records, memory storage, request history, and checkpoint identities intact. No new generic memory service or model role is needed.

### R2. The evidence shown to the investigator is narrower than the critic's

**Established:** Fresh investigator evidence includes four headline metrics per episode and selected entry-funnel counts. `_report_evidence` gives the critic more detail, but selects only the four most frequent entries from each exit/funnel/intent category and three regime slices. Some other measurements return through critic citations.

**Risk, not a demonstrated failure:** A rare event directly relevant to the proposed mechanism can disappear from the displayed evidence. The model may then reason from a headline result or an explanation without its most discriminating measurement.

**Change:** For the enabled extension, reserve bounded coverage for the named prediction, comparator, negative control, and missingness before generic contextual metrics. Existing measurements outside a top-count summary must be accessible by registered identity or explicitly unavailable. Do not increase all packets indiscriminately or invent missing evaluator data.

### R3. Reissuing evidence needs to preserve its experimental context

**Established:** `reissue` reconstructs new evidence IDs from an old cited item's `metric_id` and `value`. The original critic packet separately groups measurements by experiment, episode, and cost scenario. Several episode measurements use the same metric name.

**Risk, not a confirmed misattribution:** A scalar plus prose is weaker than a measurement whose original experiment/scenario grouping remains directly inspectable in the next packet.

**Change:** New mechanism learning projections must bind the report, hypothesis, exact parent, experiment, episode ordinal, scenario, units, relevant-case counts, validity, and assessment. Test equal metric names with different values and scenarios. Do not infer context from the new evidence ID's ordinal or the critic's wording.

### R4. Bounded memory is real, but compression can limit useful learning

**Established:** The projector preserves complete selected-parent lineage or raises if it cannot fit. Other relevant records are considered in chronological order, then summarized or omitted as the byte budget fills. Other mechanisms receive summaries. `_investigator_parts` additionally retains the latest reviewed campaign direction, even when its individual record was not selected as complete feedback.

**Risk:** The latest batch direction is not equivalent to retaining every recent negative individual result. A hash-only summary does not give the model the content of that finding. An internal projection byte limit is also not a measurement of the final provider envelope after evidence reissuance.

**Change:** Record which new evidence is complete, summarized, or omitted and why. Never retain a claim while dropping its qualifiers. Bound the final request, including schema/transport overhead and admission limits. Preserve legacy projection order in this workstream; any change to legacy selection requires an explicit versioned proposal.

### R5. Critic prose is not a deterministic mechanism verdict

**Established:** Existing predictions specify a metric, direction, and rationale. The critic supplies prediction-versus-observation text and dispositions.

**Change:** Add registered units, tolerances, denominators, applicability predicates, and minimum evidence requirements in a separate precommitted specification. Compute local support/contradiction/insufficiency from those rules. Keep execution failure and disabled semantics distinct. Neither the critic nor a higher CAGR can rewrite those results.

### R6. The original Task 5 ended too early

**Established:** The first plan's explicit acceptance checked whether the critic request could explain the mechanism. That did not require proving the next investigator receives the measured lesson after persistence, compaction, and restart.

**Change:** The revised Task 5 must demonstrate the complete record-to-next-request path without a real model call. A separate, future equal-budget experiment would be needed to evaluate whether models actually generate better hypotheses because of that context.

### R7. Supplemental diagnostics cannot silently change search or enable disabled execution

**Established:** Required semantic mode uses finite-suite fingerprints; development can explicitly disable semantic evidence. These are different declared operating modes.

**Change:** Preserve both modes and existing admission. Supplemental witnesses remain explanatory and cannot rescue a rejected candidate in this slice. Required-mode implementation must identify a concrete registered bounded executor before invocation; no controller-process fallback. A skipped stage must remain visibly skipped.

## 4. Revised scope and completion criteria

Use the revised [design](../specs/2026-09-18-v5-mechanism-evidence-design.md), [implementation plan](../plans/2026-09-18-v5-mechanism-evidence.md), and [handoff](../handoffs/2026-09-18-v5-mechanism-evidence/README.md).

The immediate deliverable is an evidence extension that makes one complete learning cycle inspectable. It preserves V5's investigator, author, critic, candidate archive, evaluator, and selection objective. A conditional finding is a bounded projection of authenticated existing records plus mechanism reports, not a second memory database or an asserted universal trading rule.

Three separate claims must remain separate:

1. **Wiring:** relevant measured evidence reaches the next exact request. Offline deterministic checks can establish this.
2. **Hypothesis quality:** the model uses that evidence to produce better experiments. This requires a separately authorized, controlled model comparison.
3. **Strategy generalization:** a selected policy works beyond repeatedly used discovery data. This is a separate qualification question, not an outcome of this documentation change.

Deferred work includes an agent-selected pre-authoring diagnostic-request protocol, new hypothesis ranking, reusable cross-campaign knowledge, automatic test generation, fixed-probe rescue, evaluator performance optimization, and paid memory-ablation studies. None is a prerequisite for the evidence-only implementation slice, and none is authorized to execute by this review.

No test coverage, runtime success, experiment result, profitability, or complete GLM replication is claimed. The saved campaign remains paused and unchanged.
