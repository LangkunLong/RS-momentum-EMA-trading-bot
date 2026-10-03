# Research evidence and stage integrity: issues 86, 87 and 93

This is the bounded offline wave activated on 2026-10-02 Toronto. It covers report and role evidence, diagnostic meanings, and authenticated stage boundaries. It does not run production discovery, replay, broker deployment, or a model-authored improvement study.

## Source and ownership

The selected starting source is `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab`, tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`. Final integrated checks passed on `ed820aff0f86d82778927f7935ef8f5be177414b`, tree `5b0801f2366bded96507a8da7919859767d8c407`. This report and its verification index are subsequent documentation-only additions.

| Issue | Owner | Isolated worktree | File ownership |
| --- | --- | --- | --- |
| 86 | `01a0ffe7-a6c8-7b83-b11e-c8e1476c137e`, GPT-6 Luna/xhigh | `210d` | Diagnostics, report contracts and persistence, role projection; principal-approved minimal engine observation |
| 87 | `01a0ffe8-7181-7d70-8a11-8812b5a7b40b`, GPT-6 Luna/xhigh | `b782` | Independent formula tests and definitions; shared source corrections applied by 86 |
| 93 | `01a0ffe9-552d-7931-b033-59b62d45ab0e`, GPT-6 Luna/xhigh | `4ac9` | Stage verification and scoped confirmation/qualification/recovery fixes |

The lead integration checkout is `dd38`, branch `codex/research-evidence-stage-integrity`. Full creation receipts, exact paths, arbitration and baseline logs are retained locally under `.artifacts/research-wave/`. Those ignored coordination files are not part of a normal clone. Completed historical workers were not restored.

## Original acceptance criteria

| Issue | Original criterion | Integrated evidence |
| --- | --- | --- |
| 86 | Additional-purchase outcomes reach relevant report and role evidence. | Tests cover actual engine result assembly, typed report persistence/reload, and the next investigator request including its serialized messages. Independent task review and two scoped fix reviews approved. |
| 86 | New candidate rejection reasons are registered or explicitly unrepresented rather than silently zero. | Issue-source tests cover an unregistered reason, bounded reason projection, and explicit incomplete-checkpoint status. |
| 86 | Changed report semantics are versioned and old reports receive no invented counts. | New reports use semantics v2. The legacy canonical hash is pinned and the old shape is decoded; old checkpoints omit unavailable declined counts. |
| 87 | Gross-return, idle-cash and exit-opportunity labels match actual calculations. | Both independent fixed-example tests pass on the integrated source; 86 tests also inspect definitions in actual serialized role messages. |
| 87 | Missing empirical calibration is explicit. | Configured assumptions and absent calibration are documented and asserted in integrated report and role-message checks. |
| 87 | Stronger counterfactual/calibration work is a separate bounded follow-up. | A finite proposal is documented in `issue-87-evidence.md`; no follow-up executed. |
| 93 | Every stage requires correct prior evidence and preserves data separation. | The public synthetic chain rejects wrong-stage and changed-policy evidence and checks disjoint panels. All four stage cases pass on the integrated source; independent task review approved. |
| 93 | Tampered identities, reuse and interrupted cleanup follow the contract. | Four focused cases cover tampering, authority reuse, terminal interruption and failed-cleanup recovery without reevaluation. |
| 93 | Replay readiness stays distinct from replay execution and broker deployment. | Public-chain assertions require `executable=false`, `replay_started=false`, and zero provider calls. |

## Separate issue statuses

| Issue | Implementation | Required inputs | Acceptance evidence | Dependencies |
| --- | --- | --- | --- | --- |
| 86 | Versioned report observation/projection and both compatibility fixes integrated | Fixed policies and engine observations available | All issue tests pass in the 70-test combined selection; initial provider-description and duplicate-projection findings plus the integration empty-map regression were fixed and independently re-reviewed. | Existing 80 contracts preserved; final tested-source binding in the linked 81-compatible index; principal acceptance and remote CI pending |
| 87 | Fixed arithmetic tests and definitions integrated; shared source work belongs to 86 | Current formulas and fixed examples available; empirical calibration absent | Independent task review approved; integrated arithmetic and actual role-message checks pass | Same 80/81 boundary; no production dataset prerequisite |
| 93 | Scoped loading and manifest-binding fixes integrated | Authenticated synthetic artifacts available | Independent task review approved; all four combined stage cases pass | Same 80/81 boundary; no production winner prerequisite |

## Final integrated verification

On the exact integrated source above, Windows Python **3.13.14** completed **70 passed, 2 warnings in 182.07 seconds**. The selection includes all issue 86/87/93 tests, evaluator assumptions, mechanism evidence, the recorded legacy request/message baseline, and actual supplied-port runtime publication. The two warnings are the pre-existing unknown pytest `cache_dir` setting and `websockets.legacy` deprecation. Ruff and compileall both exited zero. Compileall reported two inaccessible ignored pytest cache directories; no source compilation error was reported.

The [verification receipt and evidence index](research-wave-verification-2026-10-03.json) records the exact commands, source/tree and clean tracked state before/after, test/fixture hashes, interpreter, dependency and environment identities, and 262 retained local evidence files with their hashes. Its six shared identity keys follow the existing #80/#81 vocabulary. Container identity is explicitly not applicable to these in-process fixtures. The synthetic readiness source repository has its own identity recorded in the chain summary, separate from the source running the checks.

The two modules with 13 failures reproduced on the untouched base were excluded from this final targeted selection. Their failed integration and comparison receipts remain indexed below; the full suite is not claimed to pass. Fresh whole-branch review, required remote CI, and principal acceptance remain separate gates.

## Baseline and limits

Issue-source details are in [the 86 report](issue-86-evidence.md), [the 87 report](issue-87-evidence.md), and [the 93 report](issue-93-evidence.md). They preserve the source identities actually checked in each isolated checkout. Their results are inputs to integration, not automatic acceptance of subsequently changed bytes.

Independent task reviews for 87 and 93 approved their scoped changes. The 87 review required two wording corrections: the exposure mean is over observed sessions, and a proposed pre-sale peak is a scenario assumption rather than timestamped evidence. Both were corrected and re-reviewed. The 86 review required descriptions to reach actual provider messages and a shared projection helper; both were corrected and independently re-reviewed. The 93 review deferred one minor scratch-management issue: each run leaves an ignored synthetic template directory. Existing environment warnings remain recorded; no unrelated cleanup was included.

The first combined run on `93e4a3c154ef3d78a908df0f4d14f1078dbfaf9e` produced **85 passed, 14 failed, 2 warnings in 250.92 seconds**. One new failure exposed the existing empty `SimulationResultV5.add_on_outcomes` default being rejected by new report validation. The corrected implementation preserves absent counts as unavailable, with regression coverage and independent scoped approval. The other 13 failures reproduced on an archive of the untouched selected base: **18 passed, 13 failed, 2 warnings in 1.88 seconds**. Those fixtures omit the required market argument or reference windows, or assert superseded policy schema/version values. They remain pre-existing test debt. The apparent download messages come from a static fixture fetcher; these checks made no data-provider requests. Ruff and compilation exited zero on the integrated source. The compilation log retained two inaccessible ignored pytest cache-directory notices.

Exact command, source, fixture, environment and output identities for that first combined run are retained locally in `.artifacts/research-wave/integrated/verification.json`; the comparison run is in `.artifacts/research-wave/legacy-baseline/verification.json`. Neither failed run is represented as a passing suite.

At the selected source, Python 3.13.14 ran the evaluator-assumption, mechanism-evidence and mechanism-artifact test files: **98 passed, 1 failed, 3 warnings in 323.02 seconds**. The existing failing fixture reached a longest persisted Windows path of 260 characters; its path guard correctly rejected that boundary. The warnings were a third-party deprecation and denied pytest cache writes. The exact command, test/dependency hashes and log hash are in the local `baseline-identity.json`; no passing baseline is claimed.

The registered local Python 3.11 interpreter points to a retired checkout. Local availability is unverified. Required CI lint/compile checks run on Python 3.11 and 3.13; the full offline suite is informational.

Fixed and synthetic evidence proves only the declared offline behavior. It does not establish empirical execution calibration, production coverage, profitability, a qualified production policy, actual replay, or deployment. This wave preserves the four candidate-edit modules, engineering-owned truth/features/simulator/qualification, fiscal/public-date semantics, the historical universe, intentional deferrals, and the separately owned Norgate and Monday paper-runtime operations.
