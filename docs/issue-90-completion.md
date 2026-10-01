# Research-11 / Issue 90 completion report

- **Issue:** #90, sequential use of bounded mechanism findings by the V5 controller.
- **Branch:** `codex/issue-90-controlled-controller`.
- **Source base:** `a7cb3f44c3cd1156a172cce6e52c0c17c6a88a78`.
- **Original evidence delivery:** `7617ad173d97087809480157930306ba045f9385`.
- **Current disposition:** the original controlled acceptance slice is evidenced; two P2 delivery corrections are implemented and locally verified. Principal re-review, normal integration/merge, and final acceptance remain pending.
- **Delivery boundary:** local commits only. No publication, merge, issue closure, external provider/model/broker call, or Docker/container execution was performed. Verification used local pytest with scripted roles and a synthetic evaluator.

## Implementation

The controlled V5 fixture builds the ordinary next investigator request from reopened persisted history, carries measured mechanism findings under an exact memory budget, invokes scripted investigator/author/critic replies, evaluates synthetic candidates, and preserves the typed terminal and recovery evidence. The actual round-2 request was constructed and persisted. Publication stopped with the public terminal `recovery/stage_failed` and no checkpoint; the independent internal diagnostic identifies `ArchiveCapacityInsufficientV5` for the deliberately capacity-one fixture. The report keeps the public failure and diagnostic cause distinct and does not claim round 2 completed.

The correction makes the controller fixture use the `tmp_path` supplied by `tests/conftest.py` whenever `ISSUE_90_EVIDENCE_DIR` is absent. That fixture honors the validated out-of-source `AGENT_LOOP_TEST_TMP_ROOT` override and removes each per-test directory during teardown. An explicit `ISSUE_90_EVIDENCE_DIR` is still normalized to an absolute path and retains numbered evidence runs. The full-controller fixture retains the Windows path-length and disjoint-root checks. A focused test checks default and explicit root selection plus run-name uniqueness.

Only tests and this report changed; no production module changed.

## Required inputs

| Input | Use and boundary |
| --- | --- |
| Supplied Research-11 / #90 charter and implementation criteria | Defines the ordinary controller request, bounded memory finding, truthful interruption/recovery, and evidence-class requirements. Its GitHub state was not fetched again during this correction. |
| Persisted V5 fixture campaign and authenticated mechanism report contract | Provides the reopened records, typed reports, and context consumed by the actual request factory. Fixture role responses and candidate/evaluator outcomes are deterministic and synthetic. |
| Accepted strategy and reproducibility contracts | `docs/strategy-policy-contract-v1.md` and `docs/research-reproducibility-index.md` provide the linked #80/#81 context. |
| #85 mechanism interface and receipts | `docs/issue-85-mechanism-feedback.md`, `docs/issue-85-completion.md`, `docs/issue-85-independent-review.md`, and `docs/issue-85-integration-receipt.md` describe the bounded report/projection interface and evidence boundary. Principal-supplied merge/closure receipts are recorded below. |
| Dated #82 image receipts | `docs/research-03-simulation-verification-2026-09-28.md` and the 2026-10-01 `docs/issue-82-local-8a-final-source-candidate-receipt-2026-10-01.md` retain distinct source-map and image identities. They contextualize dependency status; this #90 correction did not rerun them. |

No historical-real-data input was used. The role invoker supplied scripted fixture responses; evaluator and mechanism measurements were synthetic. There were zero provider calls and no broker or Docker use.

## Acceptance evidence

| Criterion | Result and evidence |
| --- | --- |
| Build the next investigator request from persisted, reopened history through the ordinary request factory. | **Pass.** Two independent reopens produced the same canonical request/messages, and the ordinary round-2 controller built and persisted that same request. Request SHA-256: `e05e0c901d01238de4b7353d27bdc8d36e11352ebe4f42bc28b74523ab5a0432`. Full payload/messages: `.artifacts/issue-90/next-request-synthetic.json`. |
| Carry a measured finding under a controlled memory budget while retaining selected-parent lineage. | **Pass.** The derived budget is 3,191 canonical bytes: selected-parent lineage is 2,379 bytes; lineage plus sibling summary is 3,191; full sibling feedback would be 4,572. The request includes measured `exit.decision_changed_count` rows assessed `contradicted_on_cases` (0 of 3), unavailable `evaluator.exit_attribution_count` rows with `evaluator_metric_missing`, and `features.atr_20_fraction` applicability. The calibration artifact SHA-256 is `6cd3d2d578ffebe03f61edb0c412fd238de27d1edde61aec0382700be679a61e`; persisted report identities are `94063b5ecc085a214f4904a48e634d026a3018dfdcd5998cab31dd7d6dc8101c`, `c76b04bd2c6ee2db7ebf8059a80a691d8e7d92ac5da1b6ad48fc7863396e6676`, and `ddbe0976f9c3b1493c397ac8683f7923a23c3372f2a62705f38acddd6af860aa`. |
| Attempt the ordinary next controller round and preserve its exact outcome. | **Request pass; completion stopped at fixture archive capacity.** Investigator, author, and critic each completed once; no role reconciliations occurred; candidate counts were 3 quick and 12 discovery evaluations. The durable round contains 33 events and a typed `runtime_failed` terminal with public stage/code `recovery/stage_failed`; there is no checkpoint. The terminal event is sequence 31, SHA-256 `06c658553fd5531cd6abda306b3d0d43e8ca1d880d7e15fe28292655f6e23111`. The persisted request artifact SHA-256 is `09b9e9c1284795d72ff896dc9370eb84f231a51fafe5ffa5e36efee0c9b3fde3`. An isolated run on the pristine synthetic fixture asserts `ArchiveCapacityInsufficientV5` while reducing three accepted records into archive capacity one. See `.artifacts/issue-90/round2-controller-recovery.json`. |
| Recover scripted role, evaluator, and checkpoint interruptions without duplicate work or invented completion. | **Pass in the original controlled run.** Author and critic interruptions reopen to the same durable failure with zero new role invocations/reconciliations, unchanged event IDs, and unchanged evaluator counts. The synthetic evaluator interruption preserves its typed failure and absent episode evidence; reopening completes without repeated role/evaluator work. An interruption immediately after durable checkpoint publication recovers the committed checkpoint without replay. Evidence: `.artifacts/issue-90/recovery-role-author.json`, `recovery-role-critic.json`, and `recovery-evaluator-and-checkpoint.json`. These three cases were unchanged and not rerun for the temp-root correction. |
| Distinguish evidence classes and avoid unsupported outcome claims. | **Pass.** Scripted role outputs and synthetic measurement/evaluation are labeled separately. There is no historical-real-data, model-use, performance, qualification, or paper-execution claim. |

### Original source-bound execution

The original 4-case run passed **4 tests in 320.52s** (exit 0; one pytest `Unknown config option: cache_dir` warning). It is retained as pre-correction evidence from delivery commit `7617ad173d97087809480157930306ba045f9385`, where the exact working-file SHA-256 of `tests/test_pit_optimizer_v5_mechanism_artifacts.py` was `96e255c4c835170904fd6e0fa6b1356882f448d6bd893446f6cf8e9a9df764c1`. Its log is `.artifacts/issue-90/pytest-issue90-final-v3.log` (SHA-256 `740c55e5f6f48d71d9b7b4e6ecf0223dd599c61a2a9dd66428aae3bd13b3820b`). The request artifact hash above and full durable events are unchanged and preserved.

### P2 correction verification

At the corrected test-source SHA-256 `69b0a87f55722c9054ee88dcbbea094f554f35b0ed97b404cd6a474bf2a7a5eb`, the following focused run passed **2 tests in 223.12s**, with the same pytest configuration warning:

```text
py -3.13 -m pytest -q -p no:cacheprovider --no-cov tests\test_pit_optimizer_v5_mechanism_artifacts.py::test_issue90_evidence_root_selection_and_run_uniqueness tests\test_pit_optimizer_v5_mechanism_artifacts.py::test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence
2 passed, 1 warning in 223.12s; exit code 0
```

`ISSUE_90_EVIDENCE_DIR` was unset. The process set `AGENT_LOOP_TEST_TMP_ROOT` to a unique absolute directory under the OS temp directory, outside the source tree. The selection test checked the default `tmp_path`, explicit absolute evidence root, and non-overwriting numbered paths. The changed full-controller case completed under the default `tmp_path`; after pytest teardown the external root contained **zero entries** and was removed. The captured command result is `.artifacts/issue-90/pytest-correction-tmp-root.log` (SHA-256 `bb4ba755c78904e48e011fbff2bac7ba0927765ca42be837c51837170a53f99c`).

`ruff check`, `ruff format --check`, `git diff --check`, and `compileall` passed on the corrected test source. The original 4-case evidence and recovery artifacts were retained; no broader 93-test/history suite was rerun.

### Source and evidence identities

| Evidence | Identity |
| --- | --- |
| Original controller and recovery fixture source | Delivery commit `7617ad173d97087809480157930306ba045f9385`; source-file SHA-256 `96e255c4c835170904fd6e0fa6b1356882f448d6bd893446f6cf8e9a9df764c1`. |
| Corrected test source | SHA-256 `69b0a87f55722c9054ee88dcbbea094f554f35b0ed97b404cd6a474bf2a7a5eb`. |
| Actual investigator request | SHA-256 `e05e0c901d01238de4b7353d27bdc8d36e11352ebe4f42bc28b74523ab5a0432`; persisted request artifact SHA-256 `09b9e9c1284795d72ff896dc9370eb84f231a51fafe5ffa5e36efee0c9b3fde3`. |
| Full request artifact | `.artifacts/issue-90/next-request-synthetic.json`; SHA-256 `b235fab222650c32c816890b55b61f31cfc9ac34b33079a4c0eeb12544882d11`. |
| Controlled memory calibration | `.artifacts/issue-90/memory-budget-calibration.json`; SHA-256 `6cd3d2d578ffebe03f61edb0c412fd238de27d1edde61aec0382700be679a61e`. |
| Round-2 public and isolated diagnostic record | `.artifacts/issue-90/round2-controller-recovery.json`; SHA-256 `3a258987770395d860107a92db59194fdb634b18173a7e927c631b7dddd7d885`. |
| Author interruption recovery | `.artifacts/issue-90/recovery-role-author.json`; SHA-256 `c2f3be935922e885316191bfd632a00d93bd3b692407db3da983ec6475d9f850`. |
| Critic interruption recovery | `.artifacts/issue-90/recovery-role-critic.json`; SHA-256 `cef298aaed7d1a29a203c4b68c2425346a4f63f538faded3c6b319beccdd25f6`. |
| Evaluator and checkpoint recovery | `.artifacts/issue-90/recovery-evaluator-and-checkpoint.json`; SHA-256 `bffb2400da4909658c1df817823ab05f9004659c4fce1a810c39c0a94e4b612e`. |
| Local manifest | `.artifacts/issue-90/execution-record.json` records the original evidence hashes and the correction run. |

The request/evaluation receipts remain bound to the original controller source hash and commit. The correction source changes temporary-root selection and pins the same request digest; its current full-controller run passed under the default temporary root. This report does not relabel the earlier receipt as a fresh execution at the corrected source.

## Dependencies and statuses

| Item | Status and provenance |
| --- | --- |
| #80 and #81 | Accepted prerequisite receipts are the basis identified by the supplied Research-11 charter and the checked-in strategy/reproducibility documents. Their live issue states were not independently fetched for this correction. |
| #82 | Dated source/image evidence is retained under its own identities. The 2026-09-28 receipt records image `pit-optimizer-v5-evaluator@sha256:bf842ff5e0dc741f95e96129d224c24d7834feef23c4c20fdfe1d5297d54b527` at evaluator-source map `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`, with matched 11-observation fixed-suite fingerprints. The 2026-10-01 integrated-source receipt records commit `8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`, map `f6dee0745545308088887a924b9839efedd8f1beff1dfbfac82fd889935819f0`, and image/backend digest `sha256:1e0e6327f48e96d4f9754c6bae47969f0d8cf57f3c2b6cb8a10ab2ece28af0df`; see the dated receipt for its trace and output qualifications. These bounded synthetic image receipts are not a historical portfolio run. The old historical costs recorded with #89 remain unknown and unreconciled. No #82 image was rerun here. |
| PR #115 | Principal-supplied receipt: merged as `ed42aac14852e745219a347576796a249dd9b7b1` at `2026-10-01T17:38:10Z`. |
| PR #116 and #85 | Principal-supplied receipt: PR #116 merged as `68b5358e55df1d8a93851550f421e594541dc74e` at `2026-10-01T18:17:05Z`; #85 closed at `2026-10-01T18:17:07Z`. These statuses were attributed to that supplied receipt, not freshly fetched from GitHub by this offline task. |
| #90 | The corrected local package is ready for principal re-review. Integration/normal merge and principal acceptance remain pending; this task did not publish, merge, or close #90. |

The report and execution manifest are prepared for the normal independent read/re-review. The exact final correction commit and current working-file hashes are recorded in the local manifest after commit.
