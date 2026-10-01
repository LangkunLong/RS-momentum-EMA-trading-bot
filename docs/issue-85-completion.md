# Issue 85 completion report

- **Issue:** Research-06 / #85, “Define supported mechanism experiments and verify feedback projection”
- **Branch:** `codex/issue-85-mechanism-feedback`
- **Source base:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
- **Verified source head:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d` (mechanism/runtime source and tests at the base already satisfy this bounded slice; no source correction was needed)
- **Issue documentation checkpoint:** `441d0c2d9e217f85eb76976b1e02d9baece97291`
- **Delivery:** documentation-only commits on the branch; no PR, issue publication, merge, or closure performed.

## Implementation

The four acceptance criteria are covered by the existing bounded V5 mechanism implementation and its focused fixtures. This task adds the explicit support boundary and completion/evidence record. Review found no necessary change to the allowed source modules or test fixtures, so their accepted behavior remains unchanged.

The new [issue-85-mechanism-feedback.md](issue-85-mechanism-feedback.md) documents the exact parent comparator, registered cases and units, all three supported observables and their disconfirmations, unavailable questions, persistence/restart behavior, and the capability boundary. It also names the typed contract that a sequential #90 consumer may read.

| Acceptance criterion | Exact focused tests used | Source and result |
| --- | --- | --- |
| Each supported experiment declares comparator, relevant cases, observable units, and disconfirmation. | `tests/test_pit_optimizer_v5_mechanism_evidence.py::test_closed_vocab_rejects_unsupported_metric_unit_recipe_and_predicate`; `tests/test_pit_optimizer_v5_mechanism_evidence.py::test_conditional_decision_metric_requires_relevant_case_denominator`; `tests/test_pit_optimizer_v5_mechanism_evidence.py::test_spec_hypothesis_and_disconfirming_metrics_must_match_declared_meaning`. | `mechanism_contracts.py::MechanismExperimentSpecV1`, `MechanismMetricSpecV1`, and `MechanismDisconfirmingObservationV1`; `mechanism_reports.py::build_mechanism_evidence_report_v1`. The support document records the three closed-registry observables. |
| Unsupported questions are unavailable; supplemental evidence does not change ranking. | `tests/test_pit_optimizer_v5_mechanism_evidence.py::test_unavailable_measurements_preserve_declared_rows_without_fabricated_zeroes`; `tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_factory_restarts_real_history_and_projects_summary_mechanism_finding`. | `selection.py::select_parent_v5` and `parent_schedule_v5` receive no mechanism report. The ranking fixture keeps the higher-CAGR sibling selected while the lower-CAGR target's finding is contradicted. |
| Evidence survives persistence, compaction, and restart with limitations, with no inferred model-use claim. | `tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_complete_evidence_restarts_through_read_only_record_loader`; `tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_factory_restarts_real_history_and_projects_summary_mechanism_finding`; `tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_worker_unavailable_limitation_survives_typed_role_request`. | `mechanism_artifacts.py` restores authenticated sidecars; `memory.py::project_investigator_memory_v5` summarizes under budget; `production_runtime.py::MechanismRoleRequestAdapterV1` projects into the actual request. The restart fixture verifies identical request digest, canonical payload, and messages; it demonstrates delivery and makes no model-use claim. |
| A capability table distinguishes bounded memory/selected findings from arbitrary diagnostics and cross-campaign learning. | `tests/test_pit_optimizer_v5_mechanism_evidence.py::test_closed_vocab_rejects_unsupported_metric_unit_recipe_and_predicate`; `tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_factory_restarts_real_history_and_projects_summary_mechanism_finding`. | The new support document contains the requested table. `memory.py::project_investigator_memory_v5` and current-campaign-bound sidecar loading support the bounded capabilities; the table labels arbitrary diagnostics and cross-campaign learning as deferred. |

No historical facts, retained study data, provider service, broker, paid service, or Docker image was used or run. Fixtures use known policy inputs and synthetic role responses.

### Changed files

| File | Visibility | Change |
| --- | --- | --- |
| `docs/issue-85-mechanism-feedback.md` | Tracked | Supported measurement, limitation, persistence, capability, and downstream contract documentation. |
| `docs/issue-85-completion.md` | Tracked | This four-criterion completion report with hashes and focused verification results. |
| `.artifacts/issue-85/verification.json` | Local-only | Test result, source/input hash manifest, and test-session locator for review. |

## Required inputs

The supplied #85 brief and published Research-06 issue-body snapshot were read. The #66 definitions and #80 contract were read in `docs/strategy-policy-contract-v1.md`; `docs/research-reproducibility-index.md` and the existing V5 mechanism evidence design were also inspected. The activated brief states that #66, #80, and #81 are accepted/closed and their current artifacts are integrated at this base. This was an offline implementation/evidence pass; current GitHub issue state was not independently re-fetched.

## Acceptance evidence

Focused verification passed:

```text
py -3.13 -m pytest -q tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_artifacts.py
93 passed, 1 warning in 741.51s (0:12:21)
exit code: 0
```

The warning is pytest's cache provider failing to create its configured cache path under `.artifacts/pytest` due to access denied. All tests completed and passed. The source/input hashes, result summary, and tool-session locator are retained at `.artifacts/issue-85/verification.json` (local-only). The evidence record distinguishes tracked synthetic fixtures from retained private inputs; no private study or market-data input was used.

### Source and input hashes

Each SHA-256 below is over the exact working-file bytes read at verification time, including the checkout's line-ending representation. Source hashes identify the inspected base bytes; fixture files are tracked synthetic tests. Published input text is represented by its supplied local copy.

| Kind | Path | SHA-256 |
| --- | --- | --- |
| Source | `core/pit_optimizer_v5/mechanism_contracts.py` | `89c79369df8ae8624c00f29035cf63d1a63451a8605c4d0c938347c22cf1b432` |
| Source | `core/pit_optimizer_v5/mechanism_reports.py` | `f3cf256344584ca1cb59c06e80b8dc840c29489272a42afafd2d1a620da3220e` |
| Source | `core/pit_optimizer_v5/mechanism_artifacts.py` | `887d16f85424c2f0f223c0cd43f52f7160406cc50333ff1a4cc22c21b7ba2059` |
| Source | `core/pit_optimizer_v5/mechanism_probes.py` | `776219ea23ac3ea144e87baccf2c5217406ca669cd96dc701c7ec8664bef7347` |
| Source | `core/pit_optimizer_v5/memory.py` | `c660278cafafe32452b43ac89361d2c516969ea10b1ab71682c4000cdb278140` |
| Source | `core/pit_optimizer_v5/production_runtime.py` | `85d1193ad57b48e9dc1c83f73c1746f4d56054f2b1217706febbfcd21e157ba6` |
| Source | `core/pit_optimizer_v5/selection.py` | `208bf033ffbb2222cf5df4d35741f4b562e0c359a9cb5504dce8e846205085d9` |
| Source | `core/pit_optimizer_v5/provider.py` | `38125e39d3f9756206b98d8bdad07c0f2cb25a33c889575dff8992f07251decf` |
| Public fixture | `tests/test_pit_optimizer_v5_mechanism_evidence.py` | `3afddb6488e24831ed035609e36563b1ca1762d4999958687d844b3306d744c6` |
| Public fixture | `tests/test_pit_optimizer_v5_mechanism_artifacts.py` | `d90d4f32cb7d83a8a60edc42e2da39b668297453f4adf6af49de0ccf34c46494` |
| Input | `docs/strategy-policy-contract-v1.md` | `5a31bdd9015776f4b782097cfb07f0142202b90fad9c3c7a1784e54fda45c2a9` |
| Input | `docs/research-reproducibility-index.md` | `5365f7533154bddc7cbd5e10d424cd16f152bfd151c4f67462cbfe7eee8933af` |
| Input | `docs/superpowers/specs/2026-09-18-v5-mechanism-evidence-design.md` | `e33be18187f57cc181baf7543ef565a036e3503361bd5fe42bd5321f919c2c38` |
| Input | Supplied `docs/issue-85-implementation-brief.md` | `e94eee0a3340a349cdc6b15fc35c630344e9caa5fd930ef353a64bcf6211d91a` |
| Input | Published Research-06 issue-body snapshot `Research-06.md` | `3be6a02bc40d6251c809bfda0b90f189119a1836af3cc001c01b87b1b6854fef` |
| New evidence document | `docs/issue-85-mechanism-feedback.md` | `8461022273ca1d626728b51fd4cd6fa0d9bcdf837695250373fa9a93b041f8a8` |
| Local-only verification record | `.artifacts/issue-85/verification.json` | `7d9217293faa8ba86d98a7ab8dd17bd28dbcd44618559462f21a03ecbc7aed5b` |

The completion report itself is the second new tracked document and is covered by the delivery commit. Its hash is intentionally not embedded in itself.

## Dependencies and downstream contract

The activated brief identifies #66, #80, and #81 as accepted/closed prerequisites integrated at the source base. #90 remains sequential and was not started. Its eligible input contract is the persisted `MechanismEvidenceReportV1` plus the bounded `MechanismRoleProjectionV1` carried by `MechanismRoleInputV1`, built through the authenticated request adapter. That projection contains source/experiment/context identity, case coverage, typed measurement and availability/assessment rows, and limitations. It does not confer ranking authority or prove model use.

The changed-source evaluator image closure for #107 remains with the lead. No old/new Docker image was executed and no #82 receipt was relabeled. Principal acceptance, normal integration, issue publication, merge, and closure remain separate lead/human steps.
