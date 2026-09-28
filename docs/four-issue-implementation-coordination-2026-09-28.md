# Four-issue implementation coordination — 2026-09-28

This record coordinates GitHub issues [#67](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/67), [#82](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/82), [#68](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/68), and [#71](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/71). It is a work record, not issue acceptance. The original checkout retains ignored input and evidence files at `C:/Projects/trading_bot/RS-momentum-EMA-trading-bot/.artifacts/`.

## Source and accepted contracts

- Integration worktree: `C:/Users/llong/.codex/worktrees/a9c1/RS-momentum-EMA-trading-bot`; branch `codex/four-issue-integration`.
- Starting commit: `2c01e76a878a338ab2b743c38c4f1310aab3f75b`. A fresh GitHub branch API read on 2026-09-28 returned this exact SHA for `main`. The worktree `HEAD` and local `origin/main` matched it. Direct `git fetch` could not write the external linked gitdir in the sandbox; the GitHub API read established the remote identity without changing files.
- Accepted foundation: `docs/historical-feature-specification-v1.md` (#66), `docs/strategy-policy-contract-v1.md` (#80), `docs/research-reproducibility-index.md` (#81), `docs/qualified-policy-deployment-contract.md` (#97), and `docs/architecture-issue-acceptance-2026-09-28.md`. PR #110 merged these definitions/indexes; it did not accept the four issues here.
- Live GitHub issue reads on 2026-09-28 showed all four issues open, with bodies matching the saved issue packet. Historical observations and prior source-bound passes remain historical; each new result needs its own source/input/runtime identity.

## Workers and ownership

All five tasks are user-created Codex tasks on host `local`. The permanent task IDs, not the client IDs, are used for coordination.

| Role | Permanent task ID | Owned files and work | Shared-file boundary |
| --- | --- | --- | --- |
| Lead | `01a0e67f-9969-7230-8a0c-f1c433a84ad8` | Integration, independent review, this record, cross-issue identity and acceptance accounting | Owns integration order and approves overlapping edits |
| #67 | `01a0e67f-b51f-74e2-99c2-da49cd479ec6` | Coverage/reporting module, fixtures, measured development-bundle report and evidence | Reads existing data/calculators; proposes reader/snapshot edits before making them; does not own financial formulas |
| #82 | `01a0e67f-c84d-74b0-b9e3-120a32043e48` | Focused historical simulator and worker contract tests/evidence | Shared engine, fill, evaluator or worker code changes only after a concrete reproduction and lead coordination |
| #68 | `01a0e67f-e622-76c0-9e72-74ef26579911` | Membership source inventory, dated lineage normalization/artifacts, provenance and fixtures | Coordinates any `pit_data.py` or `pit_feature_snapshot.py` edit; no ETF holdings substituted for index history |
| #71 | `01a0e67f-f999-7380-a36e-4438f207b46e` | Financial checks and bounded corrections in `core/canslim/a_annual_earnings.py`, financial parts of `core/pit_feature_snapshot.py`, focused fixtures/evidence | Sole owner of these shared financial functions; informs #67 before coverage interpretation |

Each worker starts at `2c01e76` in a separate worktree. Worker branch and final commit identities will be recorded from actual receipts, rather than inferred from task creation. The lead branch is for coordination and integration, not an alternate worker session.

## Integration order and review

1. Workers implement independent fixtures/modules. #67 may build the report against existing readers while #71 validates formula semantics. #68 prepares source-backed lineage independently. #82 uses the existing shared simulator and workers.
2. Review #71's concrete formula cases and measured impact first. If semantics change, bind a new calculator/feature identity and let #67 report both the input bundle and that identity. Historical reports remain under their original identities.
3. Integrate non-overlapping #68, #82 and #71 changes after focused review; integrate #67 after its dependence on #71's feature meaning is explicit. Re-run only focused cross-issue checks for actual shared changes.
4. Each issue's acceptance is assessed against its published criteria and exact evidence. A clean branch, passing fixture, or task completion is not by itself acceptance. #79 production-bundle acceptance, #83 baseline evaluation, #107 runtime work, live model studies and paper trading remain separate.

## Four status dimensions at dispatch

| Issue | Implementation | Required inputs | Acceptance evidence | Dependencies |
| --- | --- | --- | --- | --- |
| #67 | Reporter and measured result in progress; readers and calculators exist | Fixed examples and original-checkout limited development bundle located; production acquisition not required for reporter | No new measured report or criterion-level acceptance yet | #66 accepted; ready to start. #71 semantics may affect final feature identity |
| #82 | Existing simulator/workers; focused verification in progress | Artificial bars/policies available; selected worker/runtime availability still to be checked for execution claims | No new #82 acceptance package yet; old passes are source-bound | #80 and #81 accepted; ready to start and eligible for bounded acceptance work |
| #68 | Existing dated membership/lineage types; source inventory and normalization in progress | Legitimate historical three-index source access, usage rights and coverage to be established; reduced fixture inputs available | No complete source-backed three-index lineage artifact accepted yet | #66 accepted; ready to start. Later #69/#70 and other production joins consume accepted lineage |
| #71 | Existing calculators; fixed semantic validation in progress | Fixed financial histories available; complete vendor history unnecessary | No newly measured mismatch/impact disposition or corrected identity accepted yet | #66 accepted; ready to start. #67 consumes any resulting calculator identity |

## Evidence and blockers to update

- #67 must report source-observed, public-visible, lookback-ready, calculable and policy-consumed stages; field/security-session denominators; exclusions; reason codes; breadth, price RS and industry leadership; exact development bundle and calculator identities. The manifest-declared bundle ID `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de` is an identity to verify, not a new measurement.
- #82 must separate historical portfolio simulation, fixed probes and supplemental observations; show parent/candidate assumption equality, enabled/skipped stages, actual worker/runtime availability, bounded failure/timeout cleanup and concrete passing or failing paths. An image digest alone is only a recorded identity.
- #68 must retain source and effective dates, source evidence and usage basis, stable security lineage, overlaps/renames/short-lived memberships and explicit uncovered intervals. Missing source input remains a Required inputs gap, not a code defect or failed fixture.
- #71 must reproduce skipped-period, adjacent-quarter, matched income/equity, restatement and freshness cases. Measure affected old output before retrospective error claims; record correction or explicit modeling disposition; version changed semantics and preserve old report identities.

No provider/model/broker calls, paper environment mutation, historical cost-goal restart, lowered production gate or scheduled check is authorized by this work. #107 remains with task `01a0e4f0-ab3b-7b52-a933-3b6779300819`.
