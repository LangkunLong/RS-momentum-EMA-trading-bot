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
- Ruling: the missing authenticated bundle blocks Task 4 artifact preparation and Task 5 discovery
  execution, but not source-only Task 6 composition. Implement the confirmation adapter against
  closed synthetic authorities and direct fakes now; do not create an attempt, deserialize an actual
  confirmation panel, inspect held-out content, or run confirmation until a valid discovery archive,
  bundle, and explicit confirmation decision exist. Cost if wrong: source readiness may precede
  real-artifact compatibility, which must be reauthenticated before the one-use domain is opened.
- Ruling: the absent discovery archive and confirmation outcome block Task 7 attempt creation and
  execution, but not source-only detached-qualification composition. Implement against closed
  synthetic confirmation/retirement authorities; do not construct an actual attempt, deserialize
  the 2025 panel, inspect held-out content, or request an opening decision. Cost if wrong: source
  readiness can precede real-confirmation compatibility, which must be reauthenticated before the
  separate qualification decision.
- Ruling: the absent retired qualification outcome blocks Task 8 readiness emission, but not
  source-only read-only readiness composition. Implement against closed synthetic outcome graphs;
  do not create a real readiness record, inspect qualification content, render a runnable replay
  command, start Docker, or request replay approval. Cost if wrong: source readiness may precede
  real-outcome compatibility, which must be reauthenticated before any later full-replay decision.
- Ruling: source audit found the evaluator-truth plan's baseline-authority Task 6 was never landed,
  despite campaign manifests requiring its downstream authority. Implement those source contracts and
  strict CLI commands provider-free before restoring market artifacts; `capture-baseline` remains
  unavailable for real use until the authenticated bundle/panel/profiles exist, while sandbox-profile
  source composition must not build or inspect Docker in this source-first stage. Cost if wrong: the
  campaign would reach an artifact bundle only to discover it cannot create the required baseline
  authority, so real discovery must stay blocked until this gap is source-complete and reviewed.

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
- Task 6: fix round 1/5 (2 addressed, 0 open; commit 0e339c4 after initial 8bb788f review).
- Task 6: complete (commits 8bb788f..0e339c4, re-review clean). The source-only confirmation
  adapter freezes an exactly-one authentic final discovery champion, requires reducer-verified
  complete campaign closure, opens its one-use ledger before any held-out deserialization, retires
  every terminal/recovery path, and recovers confirmation-owned resources without discovery lookup.
  Direct synthetic authorities cover selection, identity binding, retirement, and cleanup truthfulness;
  no tests, actual artifacts, providers, Docker, market data, or confirmation run occurred. A real
  confirmation remains blocked on an authenticated discovery archive and bundle plus a separate
  operator decision. Early-budget discovery closure has no authority contract and fails closed.
- Task 7: fix round 1/5 (1 addressed, 0 open; commit fbb60b9 after initial 90c6cc5 review).
- Task 7: complete (commits 90c6cc5..fbb60b9, re-review clean). The detached qualification source
  adapter recomputes closed confirmation eligibility, freezes its candidate/target/shared identities,
  opens the qualification ledger before held-out deserialization, and gates success on active,
  causal-clean, target-reaching net annualized portfolio return that strictly beats the same-panel
  baseline. Cleanup-only recovery reconciles qualification-owned resources even with dirty source,
  without reopening/evaluation or changing the original outcome. Direct synthetic checks covered
  lifecycle and recovery cases; no tests, actual artifacts, providers, Docker, market data, or
  qualification run occurred. Real qualification remains blocked on valid confirmation evidence,
  the V5 bundle, and a separate operator decision.
- Task 8: fix round 1/5 (1 addressed, 0 open; commit 4757f38 after initial c6aaf79 review).
- Task 8: fix round 2/5 (2 addressed, 0 open; commit 2395ad5 after first re-review).
- Task 8: complete (commits c6aaf79..2395ad5, final re-review clean). Readiness authenticates a
  successful retired qualification graph, emits only create-only content-free non-executable
  projections, and has no replay launch path. Its clean-source gate requires a separately pinned
  trusted Windows Git authority, rejects repository-controlled/writable tools and unsafe checkout
  forms, disables executable Git behavior, and holds/revalidates source and tool pins through the
  output write. Direct synthetic checks covered graph/identity gates, malicious configuration,
  executable and source TOCTOU, and output refusal. No tests, real artifacts, providers, Docker,
  market data, or replay ran. A real readiness record remains blocked on a valid retired
  qualification outcome, the V5 bundle, and the separately required replay decision.
- Continuation prerequisite audit: all 48 non-test Python source deliverables named by the four V5
  plans exist, and all 60 commit references in their ledgers are ancestors of the current branch.
  Source stages previously recorded complete were not redispatched. Python 3.13 in-memory compilation
  of those 48 modules and the focused source diff check passed after repair commit `5cb81b7`.
- Runtime prerequisite status confirmed by the user and exact-path existence checks: all twelve
  required V5 input files and the authenticated evaluator sandbox profile remain absent. The user
  already inspected 34 checkout roots; older partial prices/fundamentals are not authenticated V5
  inputs, and no matching three-universe membership/provenance or industry/provenance set was found.
  Docker's engine is user-reported unreachable. No broad data search, content access, Docker
  inspection, fetch, synthesis, or artifact restoration was performed during this source repair.
  Runtime progress requires deliberate trusted-source acquisition/import or adaptation, provenance
  sealing, bundle build/verification, and immutable evaluator image/profile preparation. Discovery
  adapter composition and exact-manifest approval remain later Task 4 work.
- Evaluator-truth Task 6: fix round 1/5 (2 addressed, 0 open; commits `3b5666e..5cb81b7`).
  Independent reviewer approved spec compliance and task quality with no Critical, Important, or
  Minor findings. The consequential Windows path/descriptor timestamp pin repair was reviewed in
  the same diff; complete metadata checks and explicit device/inode binding remain enforced.
- Evaluator-truth Task 6: source implementation complete (commits `5dc4784..5cb81b7`, review clean).
  Baseline graph/profile/verification/panel paths now use exact no-relocation authentication. Each
  sandbox invocation mounts owned canonical LF policy bytes under retained Windows pins and cleans
  up truthfully across terminal failures. Direct synthetic checks covered missing JSON/raw-panel
  edges with zero scans/unrelated reads, the real CRLF source guard, all four canonical mounted
  files, actual write/delete denial, seven transport/cleanup cases, and metadata/content tampering.
  Python 3.13 compile/import, targeted Ruff, CLI help, diff checks, and all applicable normal commit
  hooks passed; no hook bypass was needed. Owned scratch is absent, and no tests, real artifacts,
  providers, Docker, market-data evaluation, confirmation, qualification, or replay were used.
  The repair report/review and prerequisite audits remain in this plan's local workspace. Actual
  baseline capture and deterministic-repeat/runtime evidence remain blocked on the required local
  inputs and image; this source completion establishes no production run or return improvement.
- Data/image continuation (2026-09-07): the user explicitly authorized obtaining the missing V5
  inputs and evaluator image, superseding the earlier source-only acquisition boundary. The user
  has Alpaca and FMP; FMP profile access succeeded but all four documented current/historical
  constituent endpoints returned HTTP 402, with subscription/upgrade restriction confirmed.
  Alpaca historical SIP access succeeded. Credentials stayed local and were not logged.
- Evaluator image preparation complete: local Docker Desktop was started non-destructively and
  the dedicated V5 recipe built from a verified exact 59-file context. Immutable local reference
  `pit-optimizer-v5-evaluator@sha256:663f1749ba91df9e501e9de705cca83ff1c46305ca5a2ad589380fbe1d9893aa`
  is a real RepoDigest. Runtime source identity is
  `5989471897bee94e6886493f71b525c931388e029e6eeaa626abc3678c3e2fd7`. Restricted source/import smoke
  passed with network disabled, read-only root, no host mounts, and UID/GID 65532. The authenticated
  resource leaf binds the default 1 CPU/1024 MiB/32 PID/64 MiB-output sandbox profile. The expected
  `evaluator/sandbox-profile.json` now exists with SHA-256
  `7a9c1e73046f7a00abcb5fc029350ea7e42e5841b179074a5ef43a94f5613483`; the digest-named original and
  image-preparation index preserve its source/resource/provenance chain. No evaluation occurred.
  The first worktree-root build failed its source guard because ignore handling admitted extra
  files; the accepted build used an independently verified exact context. Future root-context
  builds need that issue resolved. Linux-only SecretStorage/jeepney versions are recorded in image
  provenance; a Linux-complete dependency lock remains a reproducible-rebuild follow-up.
- Data acquisition materially advanced: the S&P source pair now passes the actual V5 source
  validators (711 events, 606 source tickers, 505 initial share-class members). Original immutable
  HTML, reviewed map, names and spot-check hashes authenticate; original membership replayed byte
  for byte, and the only adaptation is the seed date from 2021-01-01 to 2021-01-04. Both official
  SEC bulk archives (2.97 GB) were copied into V5 raw acquisition storage and source/copy hashes
  verified. Older S&P price/fundamental material (54 MB) was copied with original provenance and
  remains explicitly partial, including legacy security-master exclusions.
- With the user-supplied SEC contact, downloaded and hashed all 25 official financial-statement
  archives from 2019 Q4 through 2025 Q4 (2.61 GB). Reverified all archive/SUB hashes and extracted
  172,551 accession/CIK records across 10,567 CIKs into dated SIC evidence, retaining 1,951 unknown
  SIC rows. Availability is the first supplied session strictly after SEC acceptance calendar
  date. This is raw classification evidence, not final industry membership/ranks. The legacy S&P
  seed-master subset covers 469 symbols; 464 have a preceding latest-filing SIC and five lack a
  preceding filing under that old CIK mapping. Complete dated identities remain required.
- Acquired all three non-tradable reference price series through the existing Alpaca SIP helpers:
  1,508 sessions each for IWM/QQQ/SPY over 2020-01-02..2025-12-31, 4,524 rows per SPLIT and RAW
  snapshot, with verified cutoff factors of 1.0 for all three. These remain authenticated reference
  material pending the complete tradable lineage union and final price provenance composition.
- Nasdaq public acquisition obtained 16 source documents, a contemporaneous 102-ticker anchor,
  and four primary-source-supported corrections. The 91 corrected event claims reconcile to the
  same 101-member year-end set as an independent endpoint backcast. The source pair was correctly
  withheld: SOLS and omitted GRAIL transient inclusion/removal dates remain unresolved, and endpoint
  agreement cannot establish complete transient coverage. Complete Russell 2000 daily membership
  is still unestablished. Neither current ETF holdings nor reconstructed market-cap proxies were
  substituted for index membership.
- Current create-only preparation status is recorded at
  `.artifacts/pit-optimizer-v5/data/acquisition/acquisition-status-20260907.json`, SHA-256
  `d34e0ea7501bdb2e64e495d1b34d25ee8c1c81c818262b24a4c60f15071e4b7c`: 2 of 12 final input files
  present, image ready, full bundle not ready. Exact raw acquisition receipts/provenance, helper
  scripts, image and Nasdaq reports, and the independent data-source audit remain local. Initial
  independent review approved S&P/image preparation without new Critical/Important findings. The
  follow-up material review also found no new Critical/Important defect after checking all 172,551
  SIC date gates, unknown/cutoff retention, reference bytes/source hashes, 11 cache files, 16 Nasdaq
  receipts, and final status/input existence bindings. Findings remain in the same audit report.
- Vendor coverage/quote inquiry drafts for LSEG and Nasdaq are prepared locally. On being asked
  whether to send them, the user requested a recommendation. Recommended quotes only for missing
  membership, retaining Alpaca/SEC sources, with coverage/retention/price review before purchase.
  Explicit send approval has not been received; no inquiry, subscription, purchase, upload, push,
  model-provider role call, test, market evaluation, baseline, held-out stage, or replay occurred.
  Next dependency is complete membership/identities, then final price/fundamental/industry export,
  V3 bundle verification and downstream baseline/campaign preparation under their existing gates.
