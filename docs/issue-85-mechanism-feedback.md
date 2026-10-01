# Issue 85: Supported mechanism findings and feedback boundary

**Scope:** Research-06 / GitHub #85, mechanism measurements and delivery into the next investigator request. This document records the supported V5 slice present at source base `ab385d792e19ff6db39d87f1123f47f660fc1e1d` and the focused evidence checked for this issue. It does not qualify a strategy, change its rank, establish profitability, or show that a model used a delivered finding.

## Supported experiment contract

Every accepted `MechanismExperimentSpecV1` is frozen before authoring and bound to the selected hypothesis, round intent, and exact authored parent revision. That parent revision is the local comparator; campaign baseline comparisons are separate evidence. The candidate and parent receive the same validated snapshot for each paired case. A matched evaluator consequence additionally requires the same evaluator, sandbox, panel, selected scenario, and date range. Discovery comparisons retain the exact episode identity.

The V1 registry is intentionally limited to `evaluate_exit`, the symbol `core.strategy_policy.v3.exit.evaluate_exit`, the `features.atr_20_fraction` input, and the versioned `evaluate_exit_atr20_fraction_v1` case recipe. The recipe binds its ordered finite fraction inputs before observation. The spec declares applicability, case denominator, expected direction and tolerance, protected controls, resource limits, and disconfirming observations. A repetition does not increase the unique-case denominator.

| Registered observable | Comparator and relevant cases | Unit and denominator | Interpretation and disconfirmation |
| --- | --- | --- | --- |
| `exit.decision_changed_count` | Exact authored parent versus candidate on cases satisfying the declared applicability predicate. | Count of changed decisions over `relevant_cases`. | The spec records the expected direction and tolerance. Zero changed decisions on applicable cases is the registered `decision_unchanged_when_applicable` disconfirmation. |
| `exit.protected_control_unchanged_count` | Exact parent/candidate pair on cases selected by the declared denominator. `relevant_cases` keeps applicable cases; `control_cases` selects the corpus control population, including negative cases; `all_cases` includes the full corpus. The V1 protected field is the next stop price. | Count of cases with an unchanged protected next-stop price over that non-evaluator denominator. | The expected direction is unchanged. A changed protected control is the registered `protected_control_changed` disconfirmation. |
| `evaluator.exit_attribution_count` | Matched parent/candidate evaluator reports for one declared scenario and panel/episode context. | Count selected from registered `exit_attribution` diagnostics; one `evaluator_case` per available matched report context. | Only registered exit reason selectors are supported. A missing diagnostic is explicitly unavailable (`evaluator_metric_missing`) and is retained as `diagnostic_metric_unavailable`; report trade counts do not replace the evaluator-context denominator. |

Disconfirmation applies only to the declared finite cases and contexts. A zero or too-small denominator is insufficient evidence. Execution failures and unavailable measurements do not refute the hypothesis. Applicability coverage is not code branch coverage. Supplemental report findings remain explanatory evidence; they are not a pass gate, rescue rule, or ranking input.

The input is a V3 policy snapshot, not a raw historical source record. `features.atr_20_fraction` uses fraction units and corresponds to the ATR20 fraction feature named by the accepted #66/#80 contract. This issue's fixtures use fixed synthetic values and make no claim about production PIT coverage. If a future authorized discovery case uses historical facts, the #66 source-public-date, first-eligible-session, fiscal-period, and missingness rules continue to govern that input; this mechanism contract does not change them.

## Unavailable and unsupported questions

The closed registry rejects undeclared metrics, arbitrary pre-authoring diagnostics, unsupported policy methods or symbols, unregistered recipe inputs, and unregistered diagnostic selectors. It does not infer a result from a nearby metric. Reports preserve declared but missing evaluator observations as unavailable with a reason, including `evaluator_metric_missing`, `not_run`, `execution_failed`, `mixed_context`, and `unsupported_case` where applicable.

Branch coverage is unavailable without trusted branch instrumentation. The synthetic fixture port does not establish processor-time or peak-memory measurement/enforcement. The local paired observation does not establish portfolio-level consequences; only the registered matched evaluator diagnostic is projected. Arbitrary portfolio statistics, unrestricted search over cases, cross-campaign generalization, and any model-use or strategy-quality claim are outside this slice.

Parent selection is computed by `select_parent_v5` from the campaign search state, baseline, plan, evaluator contract, and stored experiment records. It receives no mechanism report or supplemental-observation object. The persisted report is separately authenticated and projected to the investigator role. In the focused ranking witness, the higher-CAGR sibling remains selected while the lower-CAGR target's measured mechanism finding is contradicted; this demonstrates that the supplemental finding did not silently reorder those candidates.

## Persistence and next-request evidence

Mechanism reports and bindings are retained in versioned, content-addressed sidecars linked to the canonical experiment. Existing experiment and checkpoint records remain unchanged. On the next investigator request, the adapter authenticates the record, sidecar, manifest, exact parent/candidate source identities, and evaluator context before issuing fresh request-local evidence IDs. The projection carries the hypothesis claim, applicability, case coverage, comparator identities, per-metric values and assessment/availability, evaluator episode/scenario, and report limitations.

The focused restart fixture forces the target finding into summary memory under a 3,072-byte budget, reloads the sidecar from authenticated history, and exercises `LocalRoleRequestFactoryV5.investigator_request`. It asserts the exact contradicted finding, denominators, missing evaluator diagnostic, synthetic-port limitation, and summary-restoration limitation in the projected request. A second repository reopen reconstructs identical request digest, canonical payload, and messages. This proves durable delivery and reconstruction of the request object; it does not prove that a model was called, attended to the evidence, or changed its reasoning.

## Capability boundary

| Capability | Available in this slice | Deferred boundary |
| --- | --- | --- |
| Bounded campaign memory | Complete selected-parent lineage plus bounded complete feedback/summaries under the existing byte budget. Authenticated selected mechanism findings can be restored from sidecars when a record is retained as a summary. | No unbounded memory or new knowledge database. Mandatory projection that cannot fit must fail explicitly; omitted records keep an omission reason. |
| Selected mechanism findings | The closed V1 exit registry above can reach the actual next investigator request with exact context and limitations after persistence, summary projection, and restart. | No new policy method, metric registry, optional diagnostic expansion, or mechanism-based ranking/qualification change. |
| Arbitrary diagnostic requests | Unsupported and rejected by the closed schema; unavailable registered diagnostics retain an explicit status/reason. | Interactive pre-authoring diagnostic requests and dynamically authored measurement code. |
| Cross-campaign learning | Not available. Findings are recovered from authenticated records in the current campaign and remain bound to their source, parent, evaluator, scenario, cases, and limitations. | Retrieval or aggregation across campaigns and transfer of one campaign's finding as a rule for another context. |

**Downstream interface:** a sequential consumer such as #90 may consume the typed `MechanismEvidenceReportV1` and the bounded `MechanismRoleProjectionV1` carried by `MechanismRoleInputV1`, including identity, cases/coverage, predictions, availability/assessment, and limitations. It must retain the experiment and context bindings. It cannot treat delivery as model-use evidence or a ranking/qualification instruction.
