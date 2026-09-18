# V5 evidence-loop source review and design addendum

**Reviewed:** 2026-09-18  
**Pinned active source:** `15ba962743da2c2ca73becdf85bc639c4f670dfa` on `main`.  
**Scope:** Selected V5 source paths, repository guidance, and the existing mechanism plan/design.  
**Verification level:** Static source review only. No test-file contents, datasets, campaign runtime
artifacts, executable checks, synthetic trials, backtests, model calls, or orders were used.

This addendum supplements the [original design](../../specs/2026-09-18-v5-mechanism-evidence-design.md)
and motivates the revised [implementation/testing plan](../../plans/2026-09-18-v5-mechanism-evidence.md).
It does not supersede the [paused handoff](../2026-09-09-pit-optimizer-v5/README.md), its restrictions,
or V5's existing selection and authorization rules. All new symbols below are proposed, not present
APIs. The reviewed source is not a complete audit of every file or proof of runtime effectiveness.

## 1. The important correction: memory already feeds hypothesis generation

The earlier suggestion that V5 lacked hypothesis memory was incorrect. There is a concrete
storage-to-request path, not merely fields reserved in a design document:

```text
Prior ExperimentRecordV5 and authenticated role packages
  → project_investigator_memory_v5
      selected-parent lineage + relevant complete feedback + bounded summaries
  → LocalRoleRequestFactoryV5._investigator_parts
      authenticates prior critic/investigator packages
      reissues their measured evidence into the current request
  → InvestigatorRoleInputV5 / RoleRequestV5.messages
      prior hypotheses, critic directions, campaign directions, current diagnostics
  → investigator artifact containing ranked hypotheses
  → select_novel_hypothesis_v5
  → author_request with the selected hypothesis and its cited evidence
```

### Pinned source evidence

Links below target the inspected commit, not a moving branch. The ranges identify reviewed areas;
they are not a claim that tests covering those areas passed.

| Source | Verified observation |
| --- | --- |
| [memory.py: record bindings/serialization](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/memory.py#L350-L635) | `ExperimentRecordV5` retains hypothesis, template/variant, validation, quick/discovery/campaign evidence, critic review, and artifact identities. Testable statuses require critic bindings; untestable statuses cannot carry them. |
| [memory.py: feedback and projection](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/memory.py#L1735-L2130) | Full feedback and compact summaries are distinct. Selected-parent lineage is mandatory; an insufficient lineage budget fails rather than silently truncating it. Other records may be summarized or omitted. |
| [production_runtime.py: investigator construction](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/production_runtime.py#L633-L849) | `_investigator_parts` loads authenticated past role packages, reissues cited evidence, includes complete critic directions and original hypotheses for retained full records, and separately retains the latest reviewed campaign direction. |
| [production_runtime.py: author and critic measurements](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/production_runtime.py#L860-L1120) | The author receives the selected hypothesis and available citations with exact parent source. Critic reports expose wider measurements, parent deltas, and bounded fixed-probe differences. |
| [provider.py: prediction and review schemas](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/provider.py#L500-L653) | Predictions declare a metric, direction, and rationale. Review comparison/explanation/next direction are text. These fields alone do not implement numerical mechanism assessment. |
| [provider.py: role inputs](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/provider.py#L920-L1027) | Investigator input includes evaluator evidence, archive families, critic directions, campaign directions, and experiment summaries. Complete and compact provider memory differ. |
| [provider.py: actual request boundary](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/provider.py#L1394-L1575) | Closed typed inputs, exact citation order, payload digests, immutable messages, and final request size are checked. Memory bytes alone do not represent the whole request. |
| [selection.py: next hypothesis](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/selection.py#L610-L664) | The controller picks the first ranked hypothesis whose derived novelty key has not been attempted for that parent. This is not a measured causal-support test. |
| [runtime.py: evaluator identity checks](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/blob/15ba962743da2c2ca73becdf85bc639c4f670dfa/core/pit_optimizer_v5/runtime.py#L1899-L1950) | Quick/discovery results are checked against panel, evaluator, policy, dates, and scenario identities. New evidence should extend these bindings, not invent a second authority. |

Key Git blob identities for reconciliation:

| Active path | Git blob SHA |
| --- | --- |
| `core/pit_optimizer_v5/memory.py` | `0409de1e47247416a8546bbca42da82c6b49c7ec` |
| `core/pit_optimizer_v5/production_runtime.py` | `0a39d37fe11c27e0e4e3c15f3110af3794840b42` |
| `core/pit_optimizer_v5/provider.py` | `8277b2ada92c12d3621ab578e84b894ecf094c17` |
| `core/pit_optimizer_v5/selection.py` | `9b7b940fde89b65d20e3b339c538dcd5340b04ab` |
| `core/pit_optimizer_v5/runtime.py` | `a656de6400af8cb32b454b832eb785a5f8a1a575` |

Do not confuse archived `.reference` copies with executable paths. The repository-wide comparison
included a large local-history archive; its returned file list is not evidence of a complete diff.
This review makes no claim that every V5 file changed, or remained unchanged, since the original plan.

## 2. Comparison source and its limits

Z.ai's [September 17 account](https://z.ai/blog/glm-built-its-inference-infrastructure) describes
engineer-set goals/boundaries, agent-proposed hypotheses/edits, and local plus end-to-end feedback.
It describes experiment-derived optimization skeletons with applicability conditions and validation
evidence, reused and rechecked in later work. Validation is chosen for the hypothesis. The company
explicitly stops short of claiming achieved recursive self-improvement.

The full page did not render consistently in the research browser; indexed passages of the primary
article were inspected. This is a company account, not independently reproduced agent trajectories,
complete prompts, or validation of its performance claims. The requirements below are our proposed
adaptation to V5, not claims about Z.ai's undisclosed implementation.

## 3. What should change—and what should not

### A. Improve what memory means, not whether memory exists

Current V5 can pass a previous explanation back to an agent. The new report should let the next
agent distinguish a measured result from that explanation. A proposed conditional lesson view is a
projection of authenticated sidecars, not a second source of truth or a separate memory service.

A useful unit contains: source experiment/report/spec identities; exact comparison scope; condition
and intervention; observed counts/deltas; protected-case results; contrary and insufficient findings;
applicability limits; and separately labeled interpretation. The next hypothesis cites current
reissued evidence and can refine, challenge, or depart from that lesson. It is not required to
pretend an untested idea is already supported.

Preserve the existing distinction between report identities and request-local evidence IDs. Old
IDs cannot be replayed as though issued with a new request; new IDs need an auditable payload binding.
A historical narrative without a frozen experiment contract remains retrospective.

### B. Test the measurement bottleneck

The investigator directly receives four headline metrics per selected-parent episode and available
members of a 16-field entry-count list. The critic receives a broader report projection, but its
categorical exit/entry/intent detail is limited to four highest-count items per category and three
regime slices. See `_INVESTIGATOR_ENTRY_COUNTS_V5`, `_investigator_parts`, and `_report_evidence` in
the pinned `production_runtime.py` above.

That is not proof of a faulty hypothesis. It is a reason to inventory which proposed prediction can
actually be assessed from available measurements. A rare relevant outcome can be absent from a
bounded summary. Absence must not be interpreted as zero or as evidence that a branch did not run.

The first slice reuses existing authorized observations. If a metric requires new instrumentation,
record it as unavailable and propose separate work. Do not use a larger backtest score to fill the
measurement gap, or silently read raw discovery/qualification records.

### C. Test compaction and the final request, not just persistence

`project_investigator_memory_v5` sorts records by round and experiment ID. It retains lineage first,
then tries relevant-mechanism complete feedback, falls back to summaries, and may omit records under
budget; other mechanisms are summarized subject to the same limit. `_investigator_parts` then adds
reissued evidence and campaign directions. Its separate latest campaign direction does not imply
that every latest experiment's full hypothesis and limits survived.

This motivates context-pressure fixtures and a local retrieval manifest. Do not silently replace
existing priority with recency, a learned ranker, or an unbounded retriever. The enabled extension
must disclose selected/summary/omitted findings and preserve each retained lesson's qualifications.
Mandatory lineage behavior stays unchanged. If a required qualified unit cannot fit, the extension
must not advertise a complete, positively supported lesson.

Check the actual request bytes and current admission limits after all additions. Passing the
intermediate memory budget does not establish final request admissibility. This review does not
claim a demonstrated overflow in an actual campaign.

### D. Preserve the research boundary

A mechanism result answers what happened on the evaluated inputs. It does not establish a general
market law or future returns. Repeatedly used discovery observations remain discovery evidence.
Engineering changes to a trusted evaluator need separate correctness/equivalence benchmarks; an
agent must not improve its strategy score by changing that evaluator or its acceptance tests.

This work keeps existing fixed-suite deduplication, novelty selection, and CAGR ranking. A supplemental
probe can document a missed behavioral witness without changing rejection. Any rescue path or new
retrieval/scheduling policy needs separate authorization and versioned evaluation.

## 4. Proposed focused verification matrix

**Every row is proposed and unexecuted.** Test files were not inspected; these are not claims of
existing or missing test coverage. Implement only after the relevant owner authorization. Use the
two proposed focused files in the plan, temporary storage, and mocked/injected external ports.

| ID | Fixture/question | Required observable result |
| --- | --- | --- |
| V01 | Existing full memory record with accepted historical role packages | Original hypothesis, critic comparison/direction, and measured payloads reach the actual next request with current IDs. |
| V02 | Foreign record, report, parent, critic artifact, or old request ID | Binding is rejected by the relevant boundary; no fabricated evidence or substitute parent. |
| V03 | Preregistered prediction versus later changed observations | Assessment changes as specified; the pre-authoring spec never changes retroactively. |
| V04 | Two hypotheses consistent with the same aggregate score | A committed discriminating control distinguishes their local predictions; aggregate improvement alone supports neither explanation. |
| V05 | Fixed suite agrees but bounded boundary cases differ | A reproducible witness is reported; legacy admission and fixed-suite identities remain unchanged. |
| V06 | No relevant cases, missing denominator, unavailable metric, or too few cases | Insufficient evidence remains distinct from contradiction, zero, and positive support. |
| V07 | Crash, timeout, unsupported recipe, or disabled-development mode | Truthful execution/coverage status; disabled mode makes zero supplemental runner calls. |
| V08 | Same-input protected decisions unchanged but portfolio paths diverge | Local controls and portfolio consequences remain separate; no false whole-portfolio invariance requirement. |
| V09 | Positive finding plus contrary or mixed evidence | All preregistered outcomes and limitations survive report-to-lesson-to-request projection. |
| V10 | A lesson applied to a new parent, recipe, corpus, or evaluator | Scope is checked; unknown/mismatched applicability is not silently claimed as validated transfer. |
| V11 | Tight memory budget, older relevant records, newer counterevidence | Retrieval disposition is explicit; mandatory lineage cannot truncate; no incomplete unconditional positive lesson is manufactured. |
| V12 | Final request expands after reissued evidence and campaign directions | Complete request/citation order/digests and admission limits are checked, not just memory size. |
| V13 | Rare diagnostic omitted by top-count summaries | Missing remains unavailable, not zero; prediction-to-observable coverage identifies the limitation. |
| V14 | Critic asserts support contrary to measured status | Narrative cannot rewrite measured facts or become a new promotion authority. |
| V15 | No previous lessons but valid current diagnostics | Novel hypotheses remain possible; evidence provenance is required, historical success is not. |
| V16 | Round N persisted, N+1 request built, fake ranked artifact selected and sent to author | The actual request, selected hypothesis, and allowed citations form one auditable chain through existing selection. |
| V17 | Same V16 chain after restart/interrupted write | Complete evidence is reused, incomplete evidence is not success, and no duplicate calls or new deadlines appear. |
| V18 | Extension absent plus attempted sensitive/qualification projection | Legacy bytes/decisions/calls remain unchanged; disallowed content cannot enter either role's context. |

For V16, a predeclared fake investigator response exercises data plumbing and selection contracts.
It cannot demonstrate that a real model used evidence well. Changing one prior observation and
observing the derived lesson/request digest change proves dependency, not improved reasoning.
A later equal-budget real-agent study would require separate approval and frozen evaluation rules.

## 5. Delivery and closeout criteria

Task 4 can establish a deterministic report-only milestone. Full Task 5 additionally requires
qualified evidence in the actual next investigator request and the selected-hypothesis handoff.
No extra agent role, paid call, generic memory database, strategy modification, new reward function,
or automatic campaign restart is necessary to verify that path.

The documentation update changes the handoff and implementation plan and adds this review. The
original design remains available and its restrictions still apply. Codex should reconcile any
later code changes before implementation and report exactly which checks remain unauthorized or
unverified. No passing tests, improved returns, independently replicated GLM results, or completed
implementation are claimed here.
