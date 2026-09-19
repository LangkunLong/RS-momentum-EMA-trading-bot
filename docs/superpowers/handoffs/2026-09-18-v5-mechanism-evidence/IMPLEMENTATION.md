# V5 Mechanism Evidence: implementation and verification

**Documentation snapshot:** 2026-09-19, America/Toronto
**Historical source baseline:** `2b1c447dc37805edcd7a4f630244ae0267f99620`
**Source verified for this closeout:** the final-review fix atop `2b1c447dc37805edcd7a4f630244ae0267f99620`; the containing commit records its exact identity
**Scope:** Tasks 1–5 plus the final-review current-status fix, provider-free deterministic fixtures, focused offline verification
**Review state at this snapshot:** The final whole-branch review found one Important current-status issue. Its fix is implemented and verified below; the same reviewer’s scoped re-review remains pending.

This slice makes a measured, precommitted mechanism finding independently reconstructible and delivers it to the next investigator request after persistence, memory pressure, and restart. It also builds the current critic packet through the real supplied-port `_Runtime.run` path. The result establishes authenticated information delivery and bounded request construction. It says nothing about model adherence, provider transport, executor success, or strategy quality.

## What was implemented

Tasks 1–3 add closed versioned contracts, paired observations, and deterministic report reduction. Task 4 adds create-only authenticated sidecars, checkpoint-bound historical lookup, restart recovery, and the opt-in runtime extension. Task 5 adds the typed role wrapper and request adapter, preserves the existing investigator memory selection, and delivers historical and current findings to the investigator and critic paths.

The final Task5 fix covers four reviewed issues: retained non-testable or missing-sidecar records are visible as typed `not_authenticated` omissions; fixture chronology is investigator completion < saved intent/precommitment < author completion < critic completion; the augmented critic packet is admitted exactly once after composition; and legal projection counts are governed by existing measured packet limits rather than a fixed eight-item cap. Corruption, foreign authority, and authentication errors still fail closed.

The final-review fix keeps candidate evaluation status independent from supplemental observation execution. `MechanismRuntimeExtensionV1.finalize_report` and the historical/current projection gates reuse `is_testable_experiment_status_v5`, admitting the existing `quick_rejected`, `timed_out`, `cancelled`, `evaluation_failed`, `evaluated`, and `zero_trade` statuses without changing the legacy status contract. A bound/run with a completed local paired observation is finalized even when quick or discovery evaluator evidence is absent or failed; only exact authenticated evaluator contexts are matched, and absent context remains `evaluator_metric_missing`. The current runtime observes every behaviorally distinct candidate before quick rejection or discovery evaluation; for those post-observation candidates, missing current bound/run data is a protocol/configuration failure that remains typed and fail-closed rather than a fabricated absence. A pre-observation `semantic_probe_failed` can carry `evaluation_failed` without a supplemental run; the opt-in path fails closed there, while the extension-off critic already rejects the missing semantic fingerprint, with no mechanism worker/provider call or public round-failure change. Historical non-admitted or unretained rows remain explicit omission dispositions.

## Final verification provenance

The final Task5 fix report ran this command on the source snapshot above:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'; py -3.13 -B -m pytest -p no:cacheprovider --no-cov tests/test_pit_optimizer_v5_mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py -q
```

Result: **83 passed, 2 warnings in 46.28s**. The warnings were the known `cache_dir` pytest configuration warning and `websockets.legacy` deprecation warning. This remains historical evidence for source baseline `2b1c447`.

The final Task5 scoped lint was:

```powershell
py -3.13 -m ruff check core/pit_optimizer_v5/mechanism_artifacts.py core/pit_optimizer_v5/production_runtime.py core/pit_optimizer_v5/provider.py core/pit_optimizer_v5/runtime.py tests/test_pit_optimizer_v5_mechanism_artifacts.py
```

Ruff reported `All checks passed!`. The artifact implementation and artifact test were format-checked successfully. The three legacy-heavy adapters kept their pre-existing formatter shape. Syntax compilation and `git diff --check` both exited `0`; Git emitted only its known LF/CRLF conversion notices.

The final source-identity checks permit reuse of earlier scoped lint evidence without relabeling it as a final-fix run: contracts/reports/evidence tests are unchanged from `bc2420bb79045c33d2ef5b088d8fda4ce2e118c6`; probes are unchanged from `caf08def3b988ac01f1c34e35f12622484d1b79d`; `artifacts.py` and `tests/task4_legacy_fixture.py` are unchanged from `8b9f64bd8162920a4e282bddb1f0d21404da8c1a`; the remaining changed Python paths were checked at `2b1c447`. The controller reported all eleven path-specific reference checks clean. No broad suite, provider check, saved artifact, dataset, campaign, or held-out path was run.

The final-review fix verification ran:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'; py -3.13 -B -m pytest -p no:cacheprovider --no-cov tests/test_pit_optimizer_v5_mechanism_artifacts.py tests/test_pit_optimizer_v5_mechanism_evidence.py -q
```

Result: **89 passed, 2 warnings in 73.99s**. The warnings were the known `cache_dir` pytest configuration warning and `websockets.legacy` deprecation warning. The four direct supplied-port status cases cover `quick_rejected`, `timed_out`, `cancelled`, and `evaluation_failed`; the two full supplied-port `_Runtime.run` regressions cover a mixed quick-rejection critic batch and discovery failure. The discovery-failure case reopens the repository and projects all three persisted local reports into the next investigator request. Each full runtime case made exactly one fixture worker-factory call per candidate.

The final-review scoped lint was:

```powershell
py -3.13 -m ruff check core/pit_optimizer_v5/mechanism_artifacts.py core/pit_optimizer_v5/production_runtime.py tests/test_pit_optimizer_v5_mechanism_artifacts.py
```

Ruff reported `All checks passed!`. `git diff --check` exited `0`; Git emitted only its known LF/CRLF conversion notices. Raw hashes for the source verified for this closeout are:

```text
core/pit_optimizer_v5/mechanism_artifacts.py       C9813B9E77A326B43FB681E5A4EC5C26AFECA49447B22180B3B33845CEB94801
core/pit_optimizer_v5/production_runtime.py        8ED05B5A299B88B79F613258F3A664EFADF321E669F69BE0A258DD6ABEF7B894
tests/test_pit_optimizer_v5_mechanism_artifacts.py FA28A206F648B6C3247F44E9ED6285F6A6D4A3F081CED125C0F24025979317EE
```

Historical raw-file hashes at Task5 baseline `2b1c447` include `production_runtime.py 02D0FC589D6C543D602A5E0B398D7969D5134687C9F51FE1A399164E05E6A926`; this is preserved as the pre-fix identity.

The final 83-test Task5 fix report and its independent review remain historical baseline evidence. The original 82-test Task5 report is retained as historical API and trace provenance only; its earlier focused deselection count is not promoted to final evidence. The source verified for this closeout is covered by this final-fix report and the 89-test verification above.

Representative passing tests include:

- `test_factory_restarts_real_history_and_projects_summary_mechanism_finding`
- `test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence`
- `test_retained_non_testable_record_is_visible_as_omitted_memory`
- `test_mechanism_role_wrapper_keeps_typed_row_ids`

## Legacy compatibility provenance

The independent pre-edit baseline was captured at `bc2420bb79045c33d2ef5b088d8fda4ce2e118c6`, before Task5 edits, with these source hashes:

```text
provider.py            13597064cd1092f8aa2fc82df1b93196461bd2e52ca4c54a616334e249af1b6f
production_runtime.py  5758dd94d31557ffab98679b944385b186e3a0b95eeb780ef24fb358bc5ba661
```

The extension-off test reconstructs the pure current fixture and asserts the recorded legacy request, message, and schema values for each role:

| role | request bytes / SHA-256 | messages bytes / SHA-256 | schema SHA-256 | evidence |
| --- | ---: | ---: | --- | ---: |
| investigator | `6985` / `a766155d672fa59bf104a7257608a958607c1e13aa1f3eea97b3e2183b53cb02` | `3844` / `ca912c183de58a2ed669bfb91b43bee1b95449b7b9c1f5c7d7168847f115f3d3` | `1de79d44ac4755db99b3212f1d841f10cf42e7ec5d4fa6ce683ae945e601746b` | `16` |
| author | `14587` / `ad1ed91033ba04ce30ff28044427724b275b83b4029d641e8f5483e77f9249f7` | `13898` / `d193ee5550e5692f5268448d0f868e5341ddd886c0177b8846549beff11a6113` | `ee84efcb09011f876185d472be042165f25f2d97bd34b7a00b08399da98ce43e` | `1` |
| critic | `130134` / `d1c35a4616737a0021a6d2668c7e859bf147aa2c01887df1bb847e192de8a535` | `75405` / `c3961b57625bf3a11543416781ba85f6ebcbf0bec52be703a5c76e2a33874b89` | `3a5ea7a9487d4cd1fd1e8995d948b3d0aa430f3e3111fbb97d3920bf9c7861d7` | `340` |

Current raw-file SHA-256 values at the Task5 snapshot are `provider.py E771EA1E53111B5726B09BD7C5B82A81E25D71619F993F05F32F11ECB03EA81C` and `production_runtime.py 02D0FC589D6C543D602A5E0B398D7969D5134687C9F51FE1A399164E05E6A926`. The baseline capture used frozen Task4 helpers only to establish provenance; the tracked extension-off regression has no ignored-snapshot runtime dependency.

## Changed-file and schema map

The implementation uses four new mechanism modules, four existing adapters, and three focused test/harness paths:

| Path | Role and key contracts |
| --- | --- |
| `core/pit_optimizer_v5/mechanism_contracts.py` | New closed canonical contracts, including `MechanismExperimentSpecV1`, `MechanismObservationBindingV1`, `MechanismEvidenceReportV1`, and `MechanismLearningProjectionV1`. |
| `core/pit_optimizer_v5/mechanism_probes.py` | New bounded paired-observation layer: `MechanismObservationCorpusV1`, `MechanismObservationRunV1`, and worker-port/failure semantics. |
| `core/pit_optimizer_v5/mechanism_reports.py` | New deterministic reduction and context binding, including `MechanismEvaluatorMatchV1`. |
| `core/pit_optimizer_v5/mechanism_artifacts.py` | New authenticated capability, create-only sidecars/index, read-only recovery, `MechanismExtensionCapabilityV1`, `MechanismArtifactRepositoryV5`, and `MechanismRuntimeExtensionV1`. |
| `core/pit_optimizer_v5/provider.py` | Existing role adapter extended with `MechanismEvidenceRowV1`, `MechanismRoleProjectionV1`, omission-only `MechanismMemoryDispositionV1`, and `MechanismRoleInputV1`; legacy role shapes remain intact. |
| `core/pit_optimizer_v5/production_runtime.py` | Existing factory adapter extended with historical/current projection composition and the single final augmented critic admission. |
| `core/pit_optimizer_v5/runtime.py` | Existing runtime adapter extended for validated mechanism unwrap and opt-in critic dispatch. |
| `core/pit_optimizer_v5/artifacts.py` | Existing persistence adapter carries canonical sidecar/index/checkpoint publication and recovery seams used by the mechanism repository. |
| `tests/test_pit_optimizer_v5_mechanism_evidence.py` | Focused contract, probe, report, evaluator-context, failure, and projection tests. |
| `tests/test_pit_optimizer_v5_mechanism_artifacts.py` | Focused artifact, role-factory, persistence, restart, memory-pressure, admission, and supplied-port runtime tests. |
| `tests/task4_legacy_fixture.py` | Extension-off compatibility harness for the existing candidate/runtime fixture. |

`SyntheticFixtureWorkerV1` is the only concrete worker used by this slice. `registered_sandbox` fails closed before a factory is invoked; no real CPU or peak-memory sandbox is implemented or claimed.

## API, schema, and accounting limits

| Surface | Verified boundary |
| --- | --- |
| `MechanismEvidenceRowV1` | Closed typed row objects carry bound identities/context, a `MechanismPredictionResultV1` containing metric, units, measured values, denominator, availability, and assessment, and request-local evidence IDs. Registered metrics only; no raw source, paths, arbitrary text authority, or unregistered evaluator field. |
| `MechanismRoleProjectionV1` | Reissues evidence IDs from the exact request, retains claim/prediction/qualifier/scope, and carries projection-level execution, controls, applicability, coverage, and limitations. It rejects stale, foreign, held-out, wrong-parent, unissued, or omitted declared evidence. |
| `MechanismRoleInputV1` | Closed opt-in union member. It supports a projection-bearing wrapper or an omission-only wrapper; projection identities, omission identities, and their intersection remain validated. The accidental fixed eight-projection and fixed omission-count limits were removed. |
| `MechanismMemoryDispositionV1` | Omission-only typed disposition with `disposition="omitted"` and the registered reasons `not_authenticated` or `not_retained_by_memory_budget`. Complete or summary selection is recorded by `MechanismRoleProjectionV1.memory_selection`. Omitted findings are recorded separately in `MechanismRoleInputV1.omitted` as `MechanismMemoryDispositionV1` entries; corrupt or foreign authority still raises the typed failure. |
| `MechanismArtifactRepositoryV5.load_existing_evidence_for_record(...)` | Read-only lookup authenticates manifest, checkpoint-authorized record, exact source and reference, precommitment/index, round intent, parent/hypothesis, and sidecar. It cannot mint capability, repair, or invoke a worker. |
| `MechanismRuntimeExtensionV1.role_request_evidence(...)` | Authenticated current-round in-memory projection. It does not perform historical discovery or reconstruct a capability. |
| `MechanismRoleRequestAdapterV1` / `critic_request_with_mechanism(...)` | Composes authenticated historical/current projections through the existing request factory. The complete augmented critic request is built before one final `_guarded_request` call. The author retains the unchanged `AuthorRoleInputV5`. |
| Legacy and wire shape | The extension is opt-in; legacy role constructors remain unchanged, legacy-off creates no extension messages/files, and the six-field `RoleRequestV5` shape remains compatible. IDs are request-local and never copied from an earlier request. |
| Admission and budgets | Existing canonical request-byte, wire-message, schema, prospective overhead, token, cost, campaign-envelope, and provider-lifecycle authorities remain in force. Removing the fixed count cap does not raise any budget or add a retry/call. |

The fixture’s admission assertions use actual canonical serialized wire/schema bytes, a `4096` input-overhead bound, `$0.01` per million input and output prices, and a `$10` synthetic ceiling. Reducing the prospective total-token envelope by one fails with `exceeds the campaign admission envelope`; increasing provider overhead by one increases the input bound by one and fails the same predicate. These are exact serialization and prospective accounting checks. They are not measured provider usage, billed cost, a live admission grant, or a successful provider call.

## Worked trace from the passing Task5 fixture

The target and a higher-return sibling are two distinct typed fixed exit-only candidates. The unchanged selector chooses the sibling with the higher campaign return, while the target is placed in the real summary projection and its authenticated sidecar is reissued later.

| Trace item | Recorded value |
| --- | --- |
| Original claim | `ATR-aware exit behavior changes exit decisions.` |
| Declared prediction | Metric `exit.decision_changed_count`, direction `increase`, condition `Applicable cases should change.` |
| Target identity | Experiment `ca3a1e80da561c379394846590b67b854e80b9c4ba390de128b946a21e0f98a5`; policy revision `d5c88451e5275beb0990a0f1659feaaaf029d50ab760cde6a07f3eaaa17a3799`; report hash `a9235e808a9a745f9240a79538f509739b4f4e7e00fe993f801aa6c0b8148a71`. |
| Exact measured target result | Parent `0`, candidate `0`, numerator `0`, denominator `3`, delta `0`; execution `completed`, availability `measured`, assessment `contradicted_on_cases`. Coverage was `total_cases=4`, `relevant_cases=3`, `decision_changed_cases=0`, `protected_control_cases=4`, `protected_control_unchanged_cases=4`, `unsupported_cases=0`. |
| Supplied synthetic campaign CAGR (%) | Deterministic fixture values: authenticated parent `0`; target `517.465278343124580758...`; higher-CAGR sibling `3678.343433288715887761...`. The displayed rounded values are `0`, `517.465278`, and `3678.343433`; these are not observed market results. |
| Real memory disposition | The target is in `memory.summaries`; the selected higher-CAGR sibling remains complete. The target summary retains its claim, prediction, finding, qualifier, scope, and grouping. |
| Critic omission | The original critic artifact cites none of the reissued mechanism IDs and contains no `exit.decision_changed_count`. The independent next-investigator projection still carries the measured target row. |
| Reissued request after restart | The first target row is `v5.investigator.26.exit.decision_changed_count.parent.e024c8ddb892`; the related row IDs are `...candidate.863db688ad1a`, `...numerator.735e51eefc7d`, `...denominator.79b6763fda5d`, and `...delta.3256306b28d8`. They are grouped by the authenticated experiment, stage, episode ordinal, scenario, parent/candidate identity, and report; after repository reopen, recovery, selection, projection, and factory reconstruction, canonical request bytes, request hash, and message bytes match the first restarted request. |

The evaluator metric `evaluator.exit_attribution_count` remains unavailable with reason `evaluator_metric_missing` and denominator `0`, separately grouped for each report, discovery episode, and scenario. The synthetic fixture limitation that processor time and peak memory were not measured or enforced remains visible. No row in this trace is a claim that the model followed the request.

The final-review status matrix retains that same local/evaluator separation. The direct status seam asserts a measured local `exit.decision_changed_count` row plus an unavailable evaluator row for all four non-complete statuses. The full runtime quick-rejection case confirms mixed current critic projections, while the full discovery-failure case confirms three persisted `evaluation_failed` records retain measured local rows and project after repository reopen. No extra worker call is made for finalization or restart.

## Durable rulings and their costs

These eight decisions are carried from `progress.md` and `closeout-rulings.md` so the closeout remains self-contained:

1. **Signed paired count deltas remain valid, and `evaluator_cases` means matched report observations rather than an invented exit-opportunity denominator.** One evaluator report can contain many exits, so per-case counts remain bounded by the case denominator while evaluator counts are not forced under that bound. The cost if wrong is that an alternate interpretation could have rejected a valid measurement or allowed an invalid aggregate.
2. **Completed report usage must fit binding limits, while failed resource-limit or timeout reports may preserve truthful overrun usage.** Rejecting all overrun records would erase failure evidence; absolute structural bounds still apply and no scientific assessment follows a failed execution. The cost if wrong is retaining usage from a failure that should have been discarded, which is controlled by the separate execution-failure state and structural limits.
3. **The controller creates task commits only after independent task review.** This keeps exact diff packaging and Windows hooks under one owner. The cost is one controller commit step per task.
4. **All implementers and reviewers use Luna/max, including final review.** The explicit user model preference overrides skill model-tier suggestions. The cost is potentially longer review iterations.
5. **Prefer a versioned role-input wrapper over optional fields on legacy `RoleRequestV5` or legacy input classes.** The constructor serializer and strict role-input union provide a narrow additive seam with byte-identical legacy encoding; if exact decoding reveals a mismatch, revise the adapter before broadening legacy serializers silently. The cost is adapter revision if decoding exposes a mismatch.
6. **Restrict the registered `exit.decision_changed_count` metric to `relevant_cases` in the first slice.** All/control denominators could let unrelated negative cases satisfy a conditional hypothesis; protected controls remain measurable outside applicability. The cost is rejecting ambiguous new-spec combinations until a separately defined observable exists.
7. **The existing user grant authorizes a narrowly bound ephemeral local fixture capability.** No new human signature or provider-role grant is required because no mechanism-specific signature API exists and source hashes prove identity rather than consent. The cost is that this slice cannot claim cryptographic operator authentication or authorize a future nonfixture executor.
8. **Candidate evaluation status and supplemental observation execution status remain separate.** An authenticated completed local paired observation may remain visible when quick or discovery evaluation is rejected or fails; exact evaluator context stays unavailable when it is absent, and an absent authenticated supplemental result is represented as scoped absence/omission. The cost is carrying a local measurement alongside an incomplete evaluator outcome and requiring consumers to read the two statuses independently rather than collapsing them into one failure label.

## Authorization boundaries and remaining empirical questions

The explicit grant covered only focused offline tests and deterministic fixtures using temporary paths, supplied synthetic data, and injected or mocked external ports. No credentials, `.env`, provider/network/model call, market dataset, backtest, saved campaign or grant, held-out data, candidate source execution, order, trade, deployment, dependency installation, or broad legacy suite was used. No source/test/index/HEAD change was made for this documentation closeout, and no source concern was found that requires changing implementation; any concrete source defect belongs with the controller.

Open questions require later design and authorization: does a model use the delivered evidence correctly; does the mechanism generalize beyond the finite fixture; does it improve hypothesis quality, CAGR, or investment outcomes; can a real bounded executor and provider satisfy the same controls; can a live admission/grant succeed; and how should hostile journal permutations, held-out evaluation, and real resource enforcement be tested? None is implied by this wiring proof. Reproducing GLM’s unpublished harness, resuming the September 9 campaign, and deployment remain outside this task.

The September 18 source review’s limited path inspection and its incomplete access to GLM’s unpublished experiment harness remain historical claims. The September 9 saved checkpoint remains unchanged. The final whole-branch review found one Important current-status issue; this companion records its fix and verification, and does not preclaim the pending scoped re-review.
