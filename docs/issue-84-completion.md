# Issue #84 — bounded mechanism observations and adapter gap

**Starting revision:** `c5295851081ddd85cbff260554ec75baeff75b83`

**Status:** Fixture evidence is complete and an additive registered-sandbox request path is implemented; #84 acceptance remains open. No Docker build/start, candidate-source execution, production campaign, provider/model call, broker action, simulator-accounting change, confirmation/qualification result, or provider-boundary change was used. The exact source-run invocation still needs principal approval.

## Implementation

Added focused behavioral checks for the existing bounded observation, sidecar, report, and projection path. They cover a supported synthetic result, an unsupported case, invalid worker output, a simulated unexpected worker exception, timeout, cleanup failure, missing evaluator evidence, exact identity/budget retention, rejected candidates, and unchanged ranking.

The fixture worker returns pretyped decisions bound to known parent/candidate source identities and synthetic V3 snapshots. The simulated crash is an in-process `RuntimeError`, not an operating-system process crash. The fixture does not execute candidate source code or measure/enforce CPU and peak memory. The report projection carries those limitations. An unregistered metric is rejected by the closed #85 registry before it can produce a report; it is not converted into a fabricated measurement.

## Required inputs

- Accepted #80 strategy/decision/experiment contract and the accepted #85 closed experiment registry.
- Known parent and candidate policy source bundles, bound by their revision and source-bundle digests.
- The registered `evaluate_exit` / `features.atr_20_fraction` recipe and synthetic snapshots.
- The existing bounded worker and artifact repository interfaces.

## Acceptance evidence

Focused command on Python 3.13:

```powershell
py -3.13 -m pytest -q --no-cov -o cache_dir=.pytest_cache-issue84 tests/test_pit_optimizer_v5_mechanism_evidence.py::test_closed_vocab_rejects_unsupported_metric_unit_recipe_and_predicate tests/test_pit_optimizer_v5_mechanism_evidence.py::test_unavailable_measurements_preserve_declared_rows_without_fabricated_zeroes tests/test_pit_optimizer_v5_mechanism_evidence.py::test_report_separates_execution_coverage_and_assessment_and_projection_labels_criticism tests/test_pit_optimizer_v5_mechanism_evidence.py::test_protocol_timeout_and_cleanup_failures_are_execution_failures tests/test_pit_optimizer_v5_mechanism_evidence.py::test_observation_binding_rejects_worker_with_foreign_corpus_before_opening tests/test_pit_optimizer_v5_mechanism_evidence.py::test_resource_case_bound_is_execution_failure_before_worker_open tests/test_pit_optimizer_v5_mechanism_evidence.py::test_bounded_fixture_observations_are_paired_and_count_unique_relevant_cases tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_required_fixture_collects_and_reuses_completed_run tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_failed_worker_outcomes_survive_sidecar_restart_and_report_projection tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_missing_evaluator_evidence_survives_report_projection tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_runtime_rejected_candidates_do_not_open_supplemental_workers tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_factory_restarts_real_history_and_projects_summary_mechanism_finding tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_noncomplete_candidate_status_keeps_local_report_and_evaluator_absence
```

**Result:** `20 passed, 1 warning in 25.16s`. The warning is pytest's cache write being denied by the task workspace policy; it did not affect the test results. `git diff --check` passed.

Crash-case spot check:

```powershell
py -3.13 -m pytest -q --no-cov -o cache_dir=.pytest_cache-issue84 tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_failed_worker_outcomes_survive_sidecar_restart_and_report_projection -k crash
```

**Result:** `1 passed, 3 deselected, 1 warning in 2.15s`.

Resource-allocation and registered-deadline follow-up:

```powershell
python -m pytest --no-cov -p no:cacheprovider -q tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_registered_mechanism_case_builds_separate_bounded_docker_command tests/test_pit_optimizer_v5_mechanism_evidence.py::test_registered_sandbox_contract_accepts_only_concrete_docker_adapter
```

**Result:** `2 passed, 1 warning`. The request test checks aggregate-share arithmetic and serialized CPU, timeout, output, memory, and memory-swap arguments; its fake executor also verifies the caller deadline reduces the remaining Docker-wait timeout. The registered collector contract check verifies the run deadline reaches reset and evaluate. The warning is the same pytest `cache_dir` configuration warning.

The full two-module run under the repository's default custom `tmp_path` root completed with `104 passed, 1 failed, 1 warning in 325.80s`. The outlier, `test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence`, stopped at its Windows path-limit assertion because an actual fixture path was exactly 260 characters. With that test's `ISSUE_90_EVIDENCE_DIR` set to a unique Windows temporary directory, it passed: `1 passed, 1 warning in 189.47s`. The warning was pytest's unknown `cache_dir` option.

After the final request-allocation and deadline-control changes, the artifact module reran with that one test deselected: `47 passed, 1 deselected, 1 warning in 205.14s`. The full mechanism evidence module passed: `57 passed, 1 warning in 2.56s`. The focused final registered-request/deadline tests also passed: `2 passed, 1 warning in 1.48s`.

`python -m py_compile` passed for the changed runtime and test modules. `git diff --check` passed; Git printed only its existing LF-to-CRLF working-copy warnings.

The persisted run/report identities below were reloaded through a fresh `MechanismArtifactRepositoryV5` and validated by SHA-256. The same synthetic spec, corpus, parent, and candidate inputs were used in each isolated fixture store.

| Outcome | Experiment ID | Binding SHA-256 | Run sidecar SHA-256 | Report sidecar SHA-256 |
| --- | --- | --- | --- | --- |
| Completed local observations; evaluator evidence explicitly missing | `eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` | `e12000d9b110a2765b7b809651478f4d6e7b7d9cbeccb9a87218c2c720330646` | `d5459f161322eab726ac7f547a1a3f6057c7b57ea03a8b39fe09593c5ad61289` | `b20f0302dabb84d13e78170609f0cfc12f402e343a93ea69148099cdfe6a4d1a` |
| Invalid worker result (`protocol_failure`) | `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` | `1e7a63d41a96ac23207d1e852fa80b1a211a5855881ae0342bd17923fd032137` | `cbe464e2029151399d579f1781be451b11ca01e513d7422457d2dc5d086c7d50` | `3484ea435d79f5b431605c418060a1de17e35c16674b98d4eeda361203d3fdb4` |
| Unexpected fixture worker exception (`execution_failed`, simulated) | `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` | `1e7a63d41a96ac23207d1e852fa80b1a211a5855881ae0342bd17923fd032137` | `27b018ea1d0c29650727d792a8c9f4bad42cdd6bdf2173a5c4909799d8fa0e88` | `cbb47bf2c02ea5c68cb371227dc2d6675f2b8caaf06c61ba0e0e22ef3b311b52` |
| Timeout (`timeout`) | `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` | `1e7a63d41a96ac23207d1e852fa80b1a211a5855881ae0342bd17923fd032137` | `5e5f6380ca44a18a086841b739cc265f230a68fc994b6bd468e2d723bc77060a` | `a2b4c6bf77c6cda1db81d24104e38571196e352c19f06d5f00bd6dca893bec4a` |
| Cleanup failure (`execution_failed`) | `ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` | `1e7a63d41a96ac23207d1e852fa80b1a211a5855881ae0342bd17923fd032137` | `b2e5636a0de52baa43313b706449e29b34472ff7636594d012893eb74ac72af2` | `d300e81c410496a6ab78eda5df3331b61fb2cde7e9485ca991bfb4ad8cfd3908` |

Shared input identities: spec `44fb57d8abd9d7b5ad971d947f2b740126cfbedfc08b590eab2604293f4f2cee`; corpus `0155ae6f543d37bfd36fd9192a8954a43a6118037b2262a1a09d5dd49cefebad`; parent revision `76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9`; candidate source bundle `a1c1897b16cc34a09fc64f6a186c28fe1039d175ce60482cd1a248c488ba0f0e`.

The reloaded failure reports preserve unavailable predictions with null measurements, count units and the declared denominators. Their projections preserve experiment/spec/corpus/parent/candidate identities, the full resource budget, `synthetic_offline` provenance, report digest, and fixture limitations. The successful local observations retain measured local rows while the missing evaluator row remains `evaluator_metric_missing` with null values.

Unsupported vocabulary is covered by `test_closed_vocab_rejects_unsupported_metric_unit_recipe_and_predicate`; the `made_up.metric` question is refused by the closed registry. Rejected-candidate isolation is covered by `test_runtime_rejected_candidates_do_not_open_supplemental_workers`. The accepted ranking witness `test_factory_restarts_real_history_and_projects_summary_mechanism_finding` still selects the higher-CAGR sibling while retaining the target's contradictory supplemental finding.

## Simulated registered-adapter failure path

A focused offline regression now exercises the production \`MechanismDockerCaseWorkerV1.reset\` and \`_execute_case\` methods, its typed candidate case-0 crash request, the collector's \`execution_failed\` mapping, and run/report sidecar reload plus unavailable projection. Its fake executor returns a typed \`nonzero_exit\` with code 137. The test replaces the generated limitation with an explicit \`TEST ONLY\` notice before persisting the temporary artifact.

Command on Python 3.13.14, with \`AGENT_LOOP_TEST_TMP_ROOT\` set to a unique temporary directory that was removed after the run:

\`\`\`powershell
py -3.13 -m pytest -q --no-cov -p no:cacheprovider tests\\test_pit_optimizer_v5_mechanism_artifacts.py::test_registered_nonzero_adapter_exit_survives_sidecar_restart_and_projection
\`\`\`

**Result:** \`1 passed, 1 warning in 2.67s\`. The warning is pytest's existing unknown \`cache_dir\` configuration option. An initial invocation without \`--no-cov\` ran the test body but coverage's final SQLite write was denied and pytest returned an internal error; the recorded successful result is the corrected \`--no-cov\` invocation above.

**Scope limitation:** This is simulated adapter-to-ledger coverage only. The test constructs the concrete adapter without calling its authority-checking initializer and supplies fake mount/executor objects; it does not start Docker, launch candidate source, create or kill an OS process, enforce container limits, or prove that a real nonzero container exit survives reload. #84 remains open for the actual source-bound worker and crash acceptance described below.



## Dependencies

`#80` is the start prerequisite used here. `#81` remains the distinct acceptance prerequisite; its acceptance is recorded in the existing #85 receipt/review. The #85 experiment meanings were taken from the accepted receipt and registry, without repeating #86/#87/#93 evidence.

The consolidated independent principal review dated 2026-10-04 was evaluated against the starting revision. It passed quality for the claimed fixture-only behavior but found #84 acceptance unmet because the registered nonfixture adapter was then unavailable. It confirmed the separate #81 prerequisite at the accepted-artifact level; current remote issue status was not independently fetched. That implementation gap has since been addressed by the typed Docker request/worker path described below, but the path remains unexecuted. The review’s substantive acceptance limitations therefore remain: no actual source-bound decisions or runtime enforcement have been observed. The fixture crash test uses an in-process synthetic RuntimeError; it does not demonstrate OS process termination through the Docker route. Keep #84 open pending source-bound decisions under the current authenticated limits and actual process-crash persistence, reload, and projection evidence. CPU and peak-memory usage are not measured.

## Historical registered-sandbox path assessment at the starting revision

At starting revision `c5295851081ddd85cbff260554ec75baeff75b83`, before the adapter changes documented below, the typed worker port contract was not reachable from the production runtime:

- `core/pit_optimizer_v5/mechanism_artifacts.py`, `MechanismRuntimeExtensionV1.observe_candidate`, persists `worker_unavailable` for `registered_sandbox` before it invokes `worker_factory`.
- `core/pit_optimizer_v5/mechanism_probes.py`, `collect_mechanism_observations_v1`, returns `not_run/worker_unavailable` for every nonfixture port before `open`, `reset`, or `evaluate`.
- `core/pit_optimizer_v5/sandbox.py`, `DockerPanelRequestV5` and `build_docker_argv_v5`, represent only fixed semantic-suite or panel requests. The semantic branch launches `probe_entry`, whose probe suite is fixed and does not accept a mechanism case snapshot; the other branch launches `container_entry` for a panel.
- `core/pit_optimizer_v5/production_sandbox.py`, `LocalContainerExecutorV5._authenticate_command`, requires an exact `DockerPanelRequestV5` and its exact generated argv. `LocalSandboxMountFactoryV5.mounts_for` accepts an owned `MaterializedVariantV5` candidate workspace; it cannot issue a mount for the archived parent `SourceBundleV5` supplied to the mechanism capability.
- The executor's persisted input/output handling and reservation mounts cover panel input or the fixed semantic fingerprint. They have no canonical one-case decision request/receipt, source-bundle mount authority for both roles, or mechanism output decoder.

That historical assessment identified the implementation gap. The registered route is now wired through a separate one-case request/entrypoint and remains fail-closed unless the exact concrete Docker worker factory is supplied. A synthetic worker cannot satisfy the registered adapter gate. No existing fixed-probe behavior was changed.

The additive implementation now provides `MechanismDockerCaseRequestV1`, a durable request mount, a separate `mechanism_entry`, strict request/receipt decoding, a per-case host adapter over `LocalContainerExecutorV5`, and an injected runtime factory. The request binds role, source bundle/revision, experiment/spec/binding, corpus and case/snapshot identity, resource budget, per-request allocation, sandbox profile, output authority, and failure mode. The entrypoint stages the bound policy sources in `/tmp`, verifies the installed evaluator source map, starts one `evaluate_exit` worker, and emits a canonical decision receipt. Each reset reserves, starts, collects, and cleans up exactly one role/case container. The optional study runtime factory defaults to the existing synthetic fixture path.

The fixed `probe_entry.py` bytes match the starting revision; the mechanism worker controls live in `mechanism_worker.py`. The registered contract test patches only the typed adapter methods with deterministic fixture decisions and proves the collector accepts that exact adapter type without invoking Docker. The request/argv test verifies the role/source/snapshot authority, bounded allocation shares, and that runtime `--memory` and `--memory-swap` both equal the effective minimum of binding, manifest, and profile memory limits. Local container inspection now checks both values against the command-bound `--memory` cap.

The resource audit found that `cpu_seconds`, `timeout_ms`, and `output_bytes` are run totals. Each case request now gets a static share over the maximum bound lattice, `2 × max_cases × max_repetitions`: CPU seconds round down as `Decimal`, timeout milliseconds and output bytes use integer division. Each canonical request records the allocation count and shares; the command passes only its share to the child CPU limit and output-file cap. Its wall timeout is bounded by its timeout share, the manifest mechanism timeout, and `CPU share / evaluation CPU quota`. The collector combines its total run timeout with the caller deadline and passes that deadline to `reset` and `evaluate`. The adapter carries the resulting absolute runtime deadline into Docker create/start/wait control calls; each control call is capped by the remaining time, including the actual Docker wait after reservation and start. A fixed lattice keeps each request share safe when fewer cases or repetitions are collected.

Memory is different: `memory_mib` is a per-container ceiling, and run-level `peak_memory_mib` is a maximum across containers, not their sum. Both Docker `--memory` and `--memory-swap` equal the effective per-container cap. CPU and peak memory usage are not measured. The container output file includes a receipt envelope, so its per-request cap can fail earlier than run-total decision-JSON accounting; it cannot let the run exceed that accounting limit.

The intended injected failure path is present in source: the request validator restricts terminate_child_after_ready to the candidate role, case order 0, repetition 0; the ready worker session sends SIGKILL to its process group before attempting an evaluate_exit decision. This is source-path evidence only, not an executed crash. No source bundle has been run through the entrypoint, and no actual nonzero container exit, mechanism sidecar reload, or unavailable-outcome projection has been exercised together through the registered adapter. Fixture crash tests remain separate and do not prove that chain.

## Held one-shot execution proposal

The proposal below is finite and source-bound. It is not authorized to run yet.

Environment check on 2026-10-04: the Docker CLI selects `desktop-linux`, but `docker info` cannot reach `npipe:////./pipe/dockerDesktopLinuxEngine` (permission denied). No image build, container start, or source execution was attempted.

| Role | Registry source | Source bundle SHA-256 | Policy revision SHA-256 |
| --- | --- | --- | --- |
| Parent | P0 / reviewed source parent | `0da6b0c4e63f2f4f4e710ef7a2226858d7da918c4f692f21d5919ed2ff593215` | `7bab9d8437022ae935df433d9cc95c83ac7ecfe0ebdbf053a9e6da8195e489c9` |
| Candidate | S / axis 2 | `25d76a20bcae925de71b4ce3c047b084f96c9bf8a82313d375369035c4708a03` | `ba7592c0bd06b8083fc9ec814d2cdb54ccfbedff6969c4b22c9de0239861c16f` |

The accepted #85 recipe is `evaluate_exit_atr20_fraction_v1`, input `features.atr_20_fraction`, ordered values `0.20`, `0.50`, `0.80`, and missing (`None`). Its recipe SHA-256 is `b9cb4f0d9eb63f37fd1bc42c6bbfac7358e66b5932e61cf6901da111eecf15a5`. The P0-seeded four-case corpus SHA-256 is `0155ae6f543d37bfd36fd9192a8954a43a6118037b2262a1a09d5dd49cefebad`.

| Case | Input | Input identity SHA-256 | Snapshot SHA-256 | Applicable |
| --- | --- | --- | --- | --- |
| 0 | `0.20` | `d14f82e366e7cfc5b27fe0a1bd1df134a7c9de71ae3190d45a20c0409f46fa1c` | `01f2821fb78a3b0693c164bddaee4997b06333387fcb1d9e9bec9c089450a6f0` | yes |
| 1 | `0.50` | `24cae6f73bbd334c1292802221221e60334bc6bb1889c96fab0faad61fd9869a` | `a74e4720b2f7c7da86175dbcdb4df95754b063436c609fd1e4cf9e871d71c535` | yes |
| 2 | `0.80` | `42ddb4f39748627019217cbdef2ac3034585945cd16a86a2dd14d803550aabad` | `0dc4b6026db362d26ca586e3df7f3de560603fdfa8156bb7ad8aa8b9dc4226e7` | yes |
| 3 | `None` | `5acef923ee71bc5e25db9887343298cd16c035a802923db872c03dbf8a417caf` | `cd1adfa3649965f6300312e571ac35e7ae302fef5cd33f71dbd3a8e357b1579b` | no |

The decision scope is four cases × one repetition × two roles: eight source decisions. A separate crash scope uses only candidate S and case 0 above, with a hard cap of one actual child-process termination after worker readiness and before a decision response. That failed run must persist as `execution_failed`; a fresh artifact repository must reload it and project the unavailable outcome. It is a separate uniquely bound run and does not replace the eight-decision result. The combined cap is eight returned decisions and one worker termination attempt.

When authorized, run containers strictly serially: one source/case container at a time, with no live parent and candidate containers together. The request emits the authenticated profile's `--network none`, read-only root, dropped capabilities, `no-new-privileges`, PID limit, CPU quota, no-swap memory cap, and bounded output mount. For `N = 2 × max_cases × max_repetitions`, each request receives `floor_decimal(cpu_seconds / N)`, `floor(timeout_ms / N)`, and `floor(output_bytes / N)`. Each CPU, timeout, and output share must fit its per-container manifest/profile ceiling; `memory_mib` is itself a per-container ceiling. The collector deadline is the earlier of the caller deadline and run timeout; each Docker wait is capped by its remaining time. Per-request wall time is also capped by that request's timeout share, the manifest mechanism timeout, and `CPU share / evaluation_cpu_limit`. Peak-memory usage is a run-level maximum, while CPU and output are run-level totals. Report configured enforcement only: `ContainerExecutionResultV5` provides no CPU-time or peak-memory telemetry. The exact current manifest/profile values and resulting effective numeric limits remain unbound in this worktree and must be part of the eventual authorized invocation.

The only retained image receipt suitable for comparison is stale: image `sha256:663f1749ba91df9e501e9de705cca83ff1c46305ca5a2ad589380fbe1d9893aa`, source commit `de6e1b888d3658cb7f83842d49eb1441140ad19d`, runtime-source SHA-256 `5989471897bee94e6886493f71b525c931388e029e6eeaa626abc3678c3e2fd7`; that receipt says `evaluations_executed: false`. Its image presence was unknown because the Docker Linux-engine pipe was absent at the retained check. It cannot authenticate the current code or a new mechanism entrypoint. The current image/runtime digest needed for a later run must be produced from the reviewed implementation and authenticated manifest/profile; no image was built or started here.

The dynamic experiment/spec/binding identities (including intent, experiment ID, evaluator contract, manifest/profile, source-mount authority, and per-run output authority) must be derived from the exact retained runtime record immediately before any approved invocation. This held proposal does not fabricate those authorities. The worktree search found no current authenticated `AuthenticatedCampaignManifestV5`/`SandboxProfileV5` pair and no current persisted bound run with its exact binding, `WorkspaceOwnerV5`/controller lease, and mount-root reservations. The two retained context-manifest documents cited above are stale build-context receipts, not authenticated runtime authority. Thus the effective numeric CPU, timeout, output, and per-container memory limits cannot be resolved here.

The eight per-case request SHA-256 values are also unbound: computing them requires that exact binding and authenticated runtime record, including effective limits and output-mount authorities. The static part of the eight-row source-role/case matrix is available from the known source bundles and corpus; request hashes remain unbound:

| Role | Case | Source bundle SHA-256 | Policy revision SHA-256 | Input identity SHA-256 | Snapshot SHA-256 | Request SHA-256 |
| --- | ---: | --- | --- | --- | --- | --- |
| Parent | 0 | `0da6b0c4e63f2f4f4e710ef7a2226858d7da918c4f692f21d5919ed2ff593215` | `7bab9d8437022ae935df433d9cc95c83ac7ecfe0ebdbf053a9e6da8195e489c9` | `d14f82e366e7cfc5b27fe0a1bd1df134a7c9de71ae3190d45a20c0409f46fa1c` | `01f2821fb78a3b0693c164bddaee4997b06333387fcb1d9e9bec9c089450a6f0` | unbound |
| Candidate | 0 | `25d76a20bcae925de71b4ce3c047b084f96c9bf8a82313d375369035c4708a03` | `ba7592c0bd06b8083fc9ec814d2cdb54ccfbedff6969c4b22c9de0239861c16f` | `d14f82e366e7cfc5b27fe0a1bd1df134a7c9de71ae3190d45a20c0409f46fa1c` | `01f2821fb78a3b0693c164bddaee4997b06333387fcb1d9e9bec9c089450a6f0` | unbound |
| Parent | 1 | `0da6b0c4e63f2f4f4e710ef7a2226858d7da918c4f692f21d5919ed2ff593215` | `7bab9d8437022ae935df433d9cc95c83ac7ecfe0ebdbf053a9e6da8195e489c9` | `24cae6f73bbd334c1292802221221e60334bc6bb1889c96fab0faad61fd9869a` | `a74e4720b2f7c7da86175dbcdb4df95754b063436c609fd1e4cf9e871d71c535` | unbound |
| Candidate | 1 | `25d76a20bcae925de71b4ce3c047b084f96c9bf8a82313d375369035c4708a03` | `ba7592c0bd06b8083fc9ec814d2cdb54ccfbedff6969c4b22c9de0239861c16f` | `24cae6f73bbd334c1292802221221e60334bc6bb1889c96fab0faad61fd9869a` | `a74e4720b2f7c7da86175dbcdb4df95754b063436c609fd1e4cf9e871d71c535` | unbound |
| Parent | 2 | `0da6b0c4e63f2f4f4e710ef7a2226858d7da918c4f692f21d5919ed2ff593215` | `7bab9d8437022ae935df433d9cc95c83ac7ecfe0ebdbf053a9e6da8195e489c9` | `42ddb4f39748627019217cbdef2ac3034585945cd16a86a2dd14d803550aabad` | `0dc4b6026db362d26ca586e3df7f3de560603fdfa8156bb7ad8aa8b9dc4226e7` | unbound |
| Candidate | 2 | `25d76a20bcae925de71b4ce3c047b084f96c9bf8a82313d375369035c4708a03` | `ba7592c0bd06b8083fc9ec814d2cdb54ccfbedff6969c4b22c9de0239861c16f` | `42ddb4f39748627019217cbdef2ac3034585945cd16a86a2dd14d803550aabad` | `0dc4b6026db362d26ca586e3df7f3de560603fdfa8156bb7ad8aa8b9dc4226e7` | unbound |
| Parent | 3 | `0da6b0c4e63f2f4f4e710ef7a2226858d7da918c4f692f21d5919ed2ff593215` | `7bab9d8437022ae935df433d9cc95c83ac7ecfe0ebdbf053a9e6da8195e489c9` | `5acef923ee71bc5e25db9887343298cd16c035a802923db872c03dbf8a417caf` | `cd1adfa3649965f6300312e571ac35e7ae302fef5cd33f71dbd3a8e357b1579b` | unbound |
| Candidate | 3 | `25d76a20bcae925de71b4ce3c047b084f96c9bf8a82313d375369035c4708a03` | `ba7592c0bd06b8083fc9ec814d2cdb54ccfbedff6969c4b22c9de0239861c16f` | `5acef923ee71bc5e25db9887343298cd16c035a802923db872c03dbf8a417caf` | `cd1adfa3649965f6300312e571ac35e7ae302fef5cd33f71dbd3a8e357b1579b` | unbound |

Once the current authenticated manifest/profile, bound experiment/spec/corpus/budget, owner/controller lease, and output-mount authority are supplied, one call through the existing typed request builder derives these eight request digests; the injected crash request is a separate candidate/case-0 identity. #84 remains open pending principal approval of the complete finite invocation and actual source-bound decision plus process-crash persistence evidence.

## Consolidated review corrections

The fresh read-only package review found that `mechanism_entry.py` imported `sandbox.py` only to recompute authenticated per-request resource shares; that import pulled in host modules outside the image source map. The share helper now lives in the already-authenticated `mechanism_contracts.py`, and the entrypoint imports it there. A regression test copies exactly the 63 evaluator source-map files into a temporary install root and successfully imports `mechanism_entry.py`. The Docker build-context allowlist includes every source-map file, including the mechanism runtime modules, and the inventory test checks exact build-context coverage.

`LocalContainerExecutorV5.start` and `.collect` now persist a `TimeoutError` from a deadline-capped control operation as a `timed_out` terminal result. Start also preserves the timed-out status when Docker create or start returns an explicit timeout result. If inspection finds a container, the executor first persists its exact identity and observed lifecycle phase so lease cleanup can remove it. Regression tests cover deadline expiry during image inspection, create timeout with and without an observed container, start timeout with and without an observed running container, and collection timeout. Other control exceptions remain `failed`.

The review also confirmed accepted `SourceBundleV5` policy files cannot perform arbitrary filesystem writes. The `/pit/output` mount itself has no filesystem quota; request validation, receipt writing, and post-run reads remain byte-bounded, so total mount usage is a defense-in-depth limitation rather than an arbitrary-write path demonstrated for valid bundles.

Focused verification on Python 3.13.14 used `AGENT_LOOP_TEST_TMP_ROOT` set to a unique temporary directory and ran `test_pit_optimizer_v5_mechanism_evidence.py`, `test_pit_optimizer_v5_mechanism_artifacts.py`, and `test_pit_optimizer_v5_membership_admission.py`: **120 passed, 2 warnings in 438.80s**. The warnings were pytest's unknown `cache_dir` option and the installed `websockets.legacy` deprecation. The temporary root was removed after the run. No Docker build/start or candidate-source execution was performed.

## Working-tree package identity

The reviewed base is `c5295851081ddd85cbff260554ec75baeff75b83`. Offline evaluator-source SHA-256 for the current working-tree source map: `b101cf6e1059b87c6e4f9da43cd2c4db1bced229bb907dc7cbf09e2043eaea08`. This identifies the current local evaluator source map only; it is not an installed-image receipt or a substitute for the missing authenticated manifest/profile pair.

The package now includes the separate request, entrypoint, worker session, executor adapter, collector/runtime wiring, sandbox mount/execution support, focused tests, and this report. `probe_entry.py` is byte-identical to the starting revision. No installed-image digest was produced because no image was built.

## Principal review of the offline adapter test — 2026-10-05

The principal's review examined a historical snapshot of this report with SHA-256 `40b7947cf33e14bb4fefefba51433092f269ec5376aef463745dd418cadad86e`; that digest is not the current report digest. The review accepted `test_registered_nonzero_adapter_exit_survives_sidecar_restart_and_projection` only as bounded offline evidence: a typed fake `nonzero_exit` (137) maps to `execution_failed`, persists through a fresh artifact-repository object, reloads, and projects an unavailable outcome. The fake executor created no OS process or Docker container. The historical review/test snapshot's source-map SHA-256 `e0dca01db5e2154abb157c7041d833df8be17ee0cab4efe41b0a39beabddfa8f` identifies its local source only, not an installed image. The test was not rerun for this review.

Overall #84 remains open and incomplete. Acceptance still requires:

1. Bind a current authenticated manifest/profile; complete experiment, spec, corpus, and resource budget; parent/candidate source identities; controller/owner lease; and source/output mount authorities.
2. Build and identify an image from the reviewed current source map and retain its image/runtime receipt.
3. After separate exact principal admission, run the finite scope serially: eight actual parent/candidate source decisions over four fixed cases, plus one separate candidate case-0 termination after worker readiness.
4. Demonstrate a real Docker worker nonzero exit, `execution_failed` persistence, reload across a fresh process/repository boundary, and unavailable projection.
5. Retain request, resource, and cleanup receipts; report configured limits only because CPU and peak-memory telemetry are unavailable.

Docker engine access is still denied and the required authorities are absent. This review admits no invocation; no Docker query or test rerun was performed for this disposition.
