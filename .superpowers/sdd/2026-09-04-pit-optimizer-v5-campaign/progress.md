# SDD ledger — plan: docs/superpowers/plans/2026-09-04-pit-optimizer-v5-campaign.md

## Controller constraints and rulings

- User override: do not create, modify, or run tests until the optimizer goal is reached. Every test
  step in this plan is replaced by bounded deterministic direct fakes plus compile, Ruff, import,
  coupling, and diff verification. Cost if wrong: less conventional regression coverage before the
  first real provider-free campaign, offset by end-to-end contract fakes and later runtime evidence.
- Keep this work local and confidential. No push, merge, upload, live provider call, Docker run,
  Git materialization, PIT market-data evaluation, confirmation/qualification opening, or replay in
  source implementation tasks.
- The currently available worktree artifact cache does not contain a V5 authenticated three-universe
  bundle. Implement Tasks 1–3 against closed contracts and synthetic direct authorities; actual local
  panel emission/mechanics evidence waits for the bundle and does not authorize hidden-data reuse.
- Current accepted V5 identity and CLI conventions supersede stale example spellings in this plan:
  commands consume canonical artifact roots plus authenticated relative path/digest references, and
  typed raw data edges remain selected only by exact manifest/plan authority.
- Task 4 must stop for approval of the exact rendered discovery manifest even though earlier broad
  call allowances exist. Tasks 6, 7, and 8 retain their separate confirmation, qualification, and
  full-replay decisions.
- Ruling: retain the legacy `EvaluationPanelSpec` purpose vocabulary. Mechanics serializes through
  the existing `quick` purpose and confirmation through the existing `qualification` purpose; the
  typed owning V5 campaign/stage plan, authenticated commitment, and command determine the actual
  stage. This avoids widening a legacy allocation/evaluator surface solely for V5 labels. Cost if
  wrong: a consumer that reads a raw panel without its required V5 owner could mislabel its stage,
  so V5 composition must require and verify the owning stage plan rather than infer stage from the
  legacy panel alone.
- Ruling: the accepted `BaselineParentAuthorityV5.policy_revision_ref` remains the baseline-policy
  revision descriptor at its exact existing path, because the evaluator and baseline authority bind
  that reference/digest directly. The new policy-scope descriptor carries the clean source commit,
  exact ordered editable paths/digests, and a child reference to that existing revision instead of
  inserting an incompatible wrapper. Cost if wrong: a future baseline descriptor needing fields not
  present in `PolicyRevisionIdentityV5` requires a schema migration rather than silently changing
  the established reference semantics.
- Ruling: Task 2 `render-command` is a manifest-only authorization/readiness projection, not a
  runnable production `run` invocation, because the accepted `CampaignManifestV5` has no adapter
  configuration reference and production execution rejects an undisclosed adapter. It must report
  that adapter composition is pending rather than inventing command arguments or defaults. Cost if
  wrong: a later explicit adapter authority must be added to a versioned manifest/command contract
  before live execution; rendering cannot by itself launch a campaign.
- Ruling: the provider-free Task 3 proof uses an explicit synthetic V3-contract baseline containing
  a declared fixture entry constant, and fixture composition fails closed when that declared
  authority is absent. The current production V3 entry wrapper has no editable entry constant, so
  the proof may not invent one or mutate production policy source. Cost if wrong: the fixture proves
  orchestration and variant mechanics rather than a production strategy threshold; real discovery
  still requires an authenticated policy-authoring path with a valid declared edit.

## Pre-flight dependency and conflict scan

| Tasks | Producer → consumer / shared surface | Finding or ruling |
|---|---|---|
| 1 → 2 | `CampaignPanelPlanV5`, held-out commitments; shared contracts/CLI | Compatible; Task 2 references exact Task 1 artifacts. |
| 1 → 3 | panels and CLI | Compatible; fixture composition must use exact serialized panel specs. |
| 1 → 4 | discovery and sealed held-out plan commitments | Compatible; verification exposes only content-free commitments. |
| 1 → 5 | four discovery episodes/security cohorts | Compatible; runtime canonicalizes episode ordinal. |
| 1 → 6 | confirmation plan and retirement domain | Compatible; opening stays outside search memory. |
| 1 → 7 | qualification plan and retirement domain | Compatible; one-use stage remains sealed. |
| 1 → 8 | panel/evaluator identity ancestry | Compatible through qualification outcome graph. |
| 2 → 3 | provider-free manifest; shared CLI | Compatible; no provider capability in fixture mode. |
| 2 → 4 | discovery manifest builder/verification | Compatible; exact operator allowance is manifest data. |
| 2 → 5 | manifest search/provider/resource limits | Compatible; runtime may not inflate limits. |
| 2 → 6 | evaluator/baseline/scenario/sandbox references | Compatible; confirmation reuses exact authorities. |
| 2 → 7 | evaluator/baseline/scenario/sandbox references | Compatible; qualification reuses exact authorities. |
| 2 → 8 | manifest and dependency graph | Compatible; readiness is content-free and read-only. |
| 3 → 4 | completed provider-free proof gate | Compatible; Task 4 cannot bypass missing proof. |
| 3 → 5 | shared runtime/summary; restored critic learning | Compatible; fixture and live runners share strict bindings. |
| 3 → 6 | authenticated archive/champion mechanics | Compatible; Task 6 freezes rather than mutates it. |
| 3 → 7 | candidate reconstruction/evaluator adapters | Compatible through confirmation commitment. |
| 3 → 8 | content-free summary conventions | Compatible; readiness adds no source/content fields. |
| 4 → 5 | exact authorized discovery command | Compatible; execution stops at manifest caps. |
| 4 → 6 | discovery manifest/held-out commitment ancestry | Compatible; champion is bound only after discovery closes. |
| 4 → 7 | qualification remains forbidden in discovery manifest | Compatible; later explicit attempt required. |
| 4 → 8 | full replay remains forbidden in discovery manifest | Compatible. |
| 5 → 6 | frozen final discovery champion | Compatible; confirmation cannot feed search. |
| 5 → 7 | discovery ancestry through confirmation | Compatible; no direct qualification shortcut. |
| 5 → 8 | campaign evidence ancestry | Compatible through retired qualification. |
| 6 → 7 | eligible `ConfirmationOutcomeV5`; shared CLI/holdout checks | Compatible; operator approval cannot override failed evidence. |
| 6 → 8 | confirmation ancestry; shared holdout checks | Compatible through qualification. |
| 7 → 8 | successful retired `QualificationOutcomeV5`; shared CLI/checks | Compatible; readiness renders but never runs replay. |

## Per-task self-consistency scan

| Task | Internal consistency | Ruling |
|---|---|---|
| 1 | Contracts, builder, commands, local artifacts, tests | Source/CLI/direct-fake implementation first; real artifacts await the bundle; tests overridden. |
| 2 | Reuses Task 1 + accepted V5 contracts; examples use older CLI spelling | Preserve accepted authenticated reference grammar; do not duplicate contracts; tests overridden. |
| 3 | Fixture runner must exercise normal runtime and several distinct variants | Use accepted provider-neutral invocation/parser path and direct synthetic evaluator; no shortcut runtime. |
| 4 | Explicit readiness then STOP | Source preparation only until exact manifest authorization. |
| 5 | Ten live rounds and accounting | Requires Task 4 exact approval and local bundle; no silent use of historical authorization. |
| 6 | Confirmation source implementation plus one-use execution | Implement adapter provider-free; actual opening requires eligible champion and separate decision. |
| 7 | Qualification implementation plus one-use execution | Implement adapter provider-free; actual opening requires explicit decision. |
| 8 | Readiness implementation and STOP | May render only after successful retired qualification; never starts replay. |

## Task status

- Task 1: fix round 1/5 (1 addressed, 2 open; new Important: confirmation commitment did not bind
  every evaluator dependency; commits 9a7665c..31c063b).
- Task 1: fix round 2/5 (1 addressed, 2 open; commits 31c063b..89664ee).
- Task 1: fix round 3/5 (2 addressed, 0 open; commits 89664ee..f3f79b5).
- Task 1: complete (commits 3548feb..f3f79b5, review clean). Deterministic V5 episode plans,
  typed held-out owners, create-only stage-ledger initialization, attempt/outcome contracts,
  content-free verification, and CLI ownership are implemented and directly verified with synthetic
  authorities. No tests were created, modified, or run. Actual panel emission remains deferred
  because this worktree has no authenticated V5 three-universe bundle.
- Task 2: fix round 1/5 (2 addressed, 0 open; commits 3aa0c7d..9b99c12).
- Task 2: complete (commits 6ab4a90..9b99c12, review clean). Canonical campaign/resource manifest
  composition, complete graph-before-parse verification, clean-source capture, and content-free
  readiness rendering are source-complete; real manifest emission awaits the authenticated bundle
  and explicit adapter composition. Tests remain overridden by the user.
- Task 3: complete (commit 9f68679, review clean). The provider-free fixture uses the normal V5
  recoverable role/runtime path with strict bindings, three distinct variants per round, durable
  critic-feedback restoration, exact zero-external-usage accounting, content-free summaries, and
  verified cleanup. Two synthetic rounds were directly exercised; the fixture entry fails closed
  without its declared synthetic authority. No tests were created, modified, or run, and no
  provider, Docker, market-data, or replay action occurred. This proves V5 orchestration and
  variant mechanics, not production strategy performance; real discovery remains blocked on the
  authenticated V5 three-universe bundle and the Task 4 exact-manifest approval gate.
