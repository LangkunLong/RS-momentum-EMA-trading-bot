# Issue 90 reviewed integration receipt

This delivery verifies controlled optimizer feedback through the ordinary V5 controller: completed round-one authoring, synthetic evaluation, critique and persistence; measured feedback restored after reopen and summary compaction in the actual next request; and truthful controlled interruption/recovery. No production module changed. Round two constructs and persists its request, then fails during the capacity-one fixture's archive publication. It is not a completed second round or evidence of model use.

## Integrated source and review

| Identity | Value |
| --- | --- |
| Owner base | `a7cb3f44c3cd1156a172cce6e52c0c17c6a88a78` |
| Original reviewed evidence delivery | `7617ad173d97087809480157930306ba045f9385` |
| Corrected test-source commit | `f6ac289827c78e33deb975535ad608228222cbb5` |
| Final independently approved owner head | `a16562903f64b0269598d063c49b973735e38e22` |
| Initial accepted integration base | `68b5358e55df1d8a93851550f421e594541dc74e` |
| Initial local normal merge | `ff072d0b65f1b3a10941bc1f40d9f67e35805c7a` |
| Initial merge and reviewed owner tree | `d1391e91d8d7c76b44586c74b9c68f79cd2bdff9` |
| Current accepted main, including normal PR117 merge | `cbcf9be878adf30453f5c0f022259f11de7b43b0` |
| Combined local integration | `3b209dc00a17e1c394c00ea0c7761c4780edfae0` |
| Combined tree before publication documents | `a048c2a1fce064d02706b3fe86a4d50b001b2a3a` |

The initial accepted integration base has the same tree as the owner's base. The initial local merge has the same complete tree as the approved owner head; `git diff a165629 ff072d0` is empty. Accepted main subsequently merged PR117, then the delivery branch incorporated that main through a normal merge. The combined branch differs from current accepted main only in `tests/test_pit_optimizer_v5_mechanism_artifacts.py` and `docs/issue-90-completion.md`, both byte-identical to the approved owner head. Publication adds only this receipt and `docs/issue-90-independent-review.md`.

Independent specification, code-quality and combined #85/#90 contract review PASS at the final owner head and at final integration `3b209dc`. Both prior P2 findings are resolved: ordinary tests honor the configured temporary root and cleanup, and the report accurately records accepted prerequisites and four statuses. The reviewer separately checked the accepted-main changes and found no relevant changed reachable behavior warranting more tests. See [the independent review](issue-90-independent-review.md), SHA-256 `915facf25d560f82f1f3f696f38f9b9e1389827a1b9477feb98be8e224c936ab`. Principal's separate original-source review independently confirmed the three substantive controlled criteria and the same two correction needs; final principal acceptance of this integrated delivery remains a separate gate.

## Actual validation

Two source-bound executions are retained, with an overlapping controller case. They must not be summed into an independent six-case suite.

1. Original working test SHA-256 `96e255c4c835170904fd6e0fa6b1356882f448d6bd893446f6cf8e9a9df764c1`: **4 passed in 320.52 seconds, exit 0**, on 2026-10-01 at 19:31:50 UTC. Explicit `ISSUE_90_EVIDENCE_DIR` retained the controller and recovery artifacts. Cases were the full ordinary controller, author and critic interruption parameters, and evaluator/checkpoint recovery. Log SHA-256 `740c55e5f6f48d71d9b7b4e6ecf0223dd599c61a2a9dd66428aae3bd13b3820b`.
2. Corrected working test SHA-256 `69b0a87f55722c9054ee88dcbbea094f554f35b0ed97b404cd6a474bf2a7a5eb`: **2 passed in 223.12 seconds, exit 0**, on 2026-10-01 at 20:43:34 UTC. `ISSUE_90_EVIDENCE_DIR` was unset and `AGENT_LOOP_TEST_TMP_ROOT` selected a unique external directory. The root-selection/uniqueness test and changed full-controller case passed; the actual command observed **zero entries after pytest teardown**, removed the empty root, and succeeded. Log SHA-256 `bb4ba755c78904e48e011fbff2bac7ba0927765ca42be837c51837170a53f99c`.

The actual commands used `py -3.13 -m pytest -q -p no:cacheprovider --no-cov` and the named cases in [the completion report](issue-90-completion.md). Each invocation emitted one unused `cache_dir` configuration warning. The actual process completion records and both logs were independently authenticated. The corrected source pins the original next-request digest. The three unchanged interruption cases and their original artifacts remain bound to the original execution; no broader suite or Docker run was repeated.

Final raw Git test SHA-256 is `36259182fb97d6c0bc119b20cf979f421fde9e65b6984374f92b307627b82c89`; the difference from tested Windows working bytes is line-ending normalization. Final completion-report SHA-256 is `bae0c5bbfe0887e127b9b17db8ab463b2ec9352a01690d59bb5816fa9c55bf38`. The refreshed local execution manifest SHA-256 is `4d527f8ee64ccfde8e215ccc755956c75b6db36da6a60d68905d7993ff6dedac`; independent correction review authenticated all 36 listed file entries. Original artifacts and failed diagnostic attempts remain preserved separately.

## Evidence and source closure

The actual next investigator request SHA-256 is `e05e0c901d01238de4b7353d27bdc8d36e11352ebe4f42bc28b74523ab5a0432`; its persisted artifact SHA-256 is `09b9e9c1284795d72ff896dc9370eb84f231a51fafe5ffa5e36efee0c9b3fde3`. The 3,191-byte history projection retains the selected parent completely and a sibling as a summary. The request carries measured contradictory findings, explicit unavailable diagnostics, comparator/context identities and limitations. That history budget is not the byte count of the entire augmented provider request.

The round-two public failure is `recovery/stage_failed`, with no new checkpoint, after three scripted role completions, three quick and twelve discovery evaluations. A separate pristine-copy diagnostic identifies `ArchiveCapacityInsufficientV5`; it does not replace the public terminal event. Controlled interruption evidence distinguishes truthful terminal stops, reuse of a completed round containing a failed candidate, and adoption of a checkpoint committed before an exception. It does not claim arbitrary process-kill or paid-call recovery coverage.

All **61 accepted evaluator-context files** at both initial `ff072d0` and combined `3b209dc0` integration were compared as raw Git bytes and lengths with the accepted context manifest. All match, including the 58 runtime files; map identity remains `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`. Lead A's accepted PR117 head `a6466749aa95ee04da87ce97fbbe75e632ebfc9a` was independently checked against the same 61 files and also matches. These source comparisons preserve the original PR115 image execution identity at `a8662e85c4c06142183c707926e7c302c75e233d`; they are not new image executions or acceptance of unseen later changes.

## Four statuses and remaining gate

| Dimension | Integrated delivery status |
| --- | --- |
| Implementation | Controlled tests and report complete; both P2 corrections independently approved; no production change. |
| Required inputs | Scripted roles and synthetic evaluator/measurement fixtures sufficient and available. No production historical dataset or provider campaign consumed. |
| Acceptance evidence | Source-bound original and correction executions, actual persisted request, recovery artifacts and independent combined contract review pass. Required exact-head CI and final principal acceptance/normal merge remain pending at receipt creation. |
| Dependencies | Eligible #80/#81/#82/#85 artifacts; PR115/116 accepted and merged, #85 closed. Dated #82 source/image receipts remain separate, and older historical costs remain unknown. |

No actual model attention/improvement, strategy performance, profitability, qualification, broker/paper execution, operational-store change or provider allowance is established. This receipt does not itself merge or close #90.
