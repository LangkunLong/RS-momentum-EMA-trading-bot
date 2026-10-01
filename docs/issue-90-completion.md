# Issue 90 controlled controller execution

- **Issue:** Research-11 / #90, sequential use of bounded mechanism findings by the V5 controller.
- **Branch:** `codex/issue-90-controlled-controller`
- **Source base:** `a7cb3f44c3cd1156a172cce6e52c0c17c6a88a78`
- **Scope delivered:** provider-free controller and recovery fixtures plus their execution record. No production module was changed.
- **Delivery boundary:** local branch only; no PR, publication, merge, or issue closure.

## Result

The fixture exercised the ordinary V5 controller through the next investigator request, author and critic responses, candidate execution, and attempted checkpoint publication. The controller constructed and durably persisted the exact investigator request reconstructed from the reopened repository. The public result then stopped truthfully with `failed`, stage `recovery`, and failure code `stage_failed`; no round-2 checkpoint was produced.

An isolated run of the same synthetic history through the internal runtime exposed the cause: `ArchiveCapacityInsufficientV5` while the archive reducer selected entries for the fixture's capacity-one archive. The fixture supplies three accepted records for that publication. The public controller's `recovery/stage_failed` is a generic wrapper result; the independently captured exception is diagnostic evidence and is not substituted for the public terminal event. The run does not claim round 2 completed. The published issue brief permits stopping after the actual next request when the fixture prevents completion; this record preserves both that request and the exact terminal result.

## Acceptance evidence

| Criterion | Result and evidence |
| --- | --- |
| Build the next request from persisted, reopened history through the normal request factory. | **Pass.** The request reconstructed twice after reopen matches the request produced and persisted by the ordinary round-2 controller, including canonical payload and messages. SHA-256: `e05e0c901d01238de4b7353d27bdc8d36e11352ebe4f42bc28b74523ab5a0432`. The full payload and messages are retained in `.artifacts/issue-90/next-request-synthetic.json`. |
| Carry a finding under a controlled memory budget without dropping selected-parent lineage. | **Pass.** The measured budget is 3,191 canonical bytes: selected-parent lineage uses 2,379 bytes, lineage plus a sibling summary uses 3,191, and full sibling feedback would use 4,572. The persisted request includes the selected parent, sibling summary, measured `exit.decision_changed_count` rows assessed `contradicted_on_cases` (0 of 3), unavailable `evaluator.exit_attribution_count` rows marked `evaluator_metric_missing`, and `features.atr_20_fraction` applicability. |
| Attempt the ordinary next controller round and preserve its exact outcome. | **Request pass; round completion blocked by fixture capacity.** Investigator, author, and critic each completed once; there were no role reconciliations, 3 quick evaluations, and 12 discovery evaluations. The store contains 33 round events and a typed terminal `runtime_failed` event with `recovery/stage_failed`. The investigator request is persisted at `roles/requests/c3f0e26640044f23bb0da2d823e22dd6a4129b30d0f08f33263f96a203bfc73e.json`, artifact SHA-256 `09b9e9c1284795d72ff896dc9370eb84f231a51fafe5ffa5e36efee0c9b3fde3`. The unwrapped diagnostic on a pristine copy asserts the exact `ArchiveCapacityInsufficientV5` exception. |
| Recover interruptions without duplicate work or invented completion. | **Pass for controlled interruptions.** Author and critic interruptions reopen to the same durable failure with zero new role invocations, zero reconciliations, unchanged event IDs, and unchanged evaluator counts. A synthetic evaluator interruption preserves the failed evaluation and absent episode evidence; reopen completes with no repeated role or evaluator work. A synthetic interruption immediately after checkpoint publication recovers the committed checkpoint without repeating work. Details and durable event payloads are in the three `recovery-*.json` artifacts. |
| Keep evidence classes explicit. | **Pass.** Role outputs are scripted fixture responses; evaluator and measurement results are synthetic. No provider, broker, Docker image, or historical-real-data input was used. The actual request artifact is evidence of request construction and delivery only; it is not evidence of model use, trading performance, qualification, or paper execution. |

## Verification

Focused execution passed:

```text
$env:ISSUE_90_EVIDENCE_DIR = '.artifacts/issue-90'; py -3.13 -m pytest -q -p no:cacheprovider --no-cov tests\test_pit_optimizer_v5_mechanism_artifacts.py::test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence tests\test_pit_optimizer_v5_mechanism_artifacts.py::test_issue90_role_interruption_reopens_to_the_same_truthful_stop tests\test_pit_optimizer_v5_mechanism_artifacts.py::test_issue90_evaluator_interruption_and_checkpoint_recovery_do_not_repeat_work > .artifacts\issue-90\pytest-issue90-final-v3.log 2>&1
4 passed, 1 warning in 320.52s; exit code 0
```

The warning is pytest's `Unknown config option: cache_dir`; it did not affect the passing tests. `ruff check`, `ruff format --check`, `git diff --check`, and Python bytecode compilation passed for the changed test file. The earlier full focused invocation could not use pytest's configured system temp root due to access denial; the final run used the repository-local evidence directory and completed.

Test source: `tests/test_pit_optimizer_v5_mechanism_artifacts.py`, including the provider-free full controller request and interruption/reopen scenarios. Exact command, result, relevant source hashes, and evidence artifact hashes are recorded in `.artifacts/issue-90/execution-record.json` (local-only). The JSON event records preserve command inputs, request messages, typed payloads, request references, and SHA-256 identities.

## Dependencies and remaining gates

The supplied Research-11 brief treats the previously accepted #80, #81, #82, and #85 receipts as the input basis. Their status was not re-fetched from GitHub during this offline run. Lead B previously diagnosed the archive-capacity limitation; independent review of this final source and evidence package remains pending. Principal PRs #115 and #116 remain pending their separate merge gate. This branch has not been published, merged, or used to close an issue.
