# Provider-backed development implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for the scoped implementation and independent review. Steps use checkbox syntax for tracking.

**Goal:** Connect the existing paid role gateway to the explicit provisional development optimizer so real investigator, author and critic calls can drive the existing local evaluation loop after separate external-call authorization.

**Architecture:** Reuse the current ledger-backed invoker and shared local runtime. Distinguish controller-backed development from provider-backed development by authenticated manifest/config authority, preserving old artifact hashes and paid uncertainty semantics. Prepare source changes in a separate staging tree while runtime-04 continues; apply only after its terminal cleanup and exact source preimage checks.

**Tech Stack:** Python 3.13, existing canonical dataclasses/artifact repository, OpenRouter one-shot gateway and durable ledger, current Git/Docker local adapters.

**Spec:** `.superpowers/sdd/2026-09-04-pit-optimizer-v5-campaign/provider-backed-development-proposal.md`, including its independent review correction for manifest.py:617.

## Global constraints

- The existing user goal authorizes local implementation toward an end-to-end loop and requests looser wiring. No external model call, policy-source transmission, purchase or provider spend is authorized by this plan.
- Do not read, create, change or run tests. Do not run synthetic probes. Use exact source review, scoped Ruff, in-memory compilation, CLI help, call-free preparation, and later actual authorized execution.
- No live source/config/image changes while runtime-04 is active. Only documentation and separate staging files may be written now.
- Preserve development_sp500_v2, disabled_development, policy-only exports, source/data/evaluator identity checks, existing controller behavior and production restrictions unrelated to this new supported combination.
- Preserve zero retries and zero schema repairs for the new provider composition; no repeat call after an uncertain accepted outcome. Keep original start times and deadlines on resume.
- USD enforcement is optional; audited-cost-only behavior remains supported. Do not invent a user monetary limit, model or pricing authority.
- Preserve staged/unstaged user work. No commit, hook changes, push, upload, new provider transport or unrelated refactor.

## Task 1: Prepare the integrated wiring patch in staging

**Files:** Staged copies of `core/pit_optimizer_v5/contracts.py`, `manifest.py`, `development_preparation.py`, `cli.py`, `operations.py`, `runtime.py`, and `summary.py`. Inspect `production_provider.py`, `production_preparation.py`, `provider.py` and direct consumers for integration; change a direct consumer only if the actual authority path requires it and document why. Keep helper decomposition small and compatible with the existing module structure.

**Interfaces:** Consume the existing `ProviderCapabilitiesV5`, `CampaignManifestV5`, `build_campaign_manifest_v5`, `LocalRoleAuthorizationLedgerV5`, `OpenRouterOneShotJsonCompletionV5`, `AuthorizedRoleRunnerV5`, `LedgerBackedRoleInvokerV5`, and `_compose_round_from_paths_v5`. Produce a provider-development configuration/factory accepted by the same prepare/run/resume/verify/summary workflow, while existing provider-free configurations still select `FileBackedControllerRoleInvokerV5`. Reuse actual existing signatures; do not add a parallel campaign execution engine.

- [x] Snapshot the exact current bytes of every changed source under `.superpowers/sdd/2026-09-04-pit-optimizer-v5-campaign/provider-staging/originals/`, with repository-relative paths and SHA-256 in `source-preimages.json`; create corresponding proposed copies under `proposed/`. Do not modify or import a proposed file into the active campaign.
- [x] Replace the independent provider-free prohibitions in CampaignManifestV5 and build_campaign_manifest_v5 with the explicit supported development combination. Existing type/scope/semantic and apply/held-out prohibitions remain; require the new paid development composition to have zero automatic retries and repair calls.
- [x] Separate development data/semantic validation from controller-only response validation. Keep controller-request, controller-response and controller terminal provenance restricted to provider-free manifests.
- [x] Add explicit provider-development configuration/factory selection. Bind existing host identities plus gateway/ledger identities and credential-handle name; do not reinterpret old configs or require response_directory for provider-backed mode. Select policy_only workspace export even with a provider.
- [x] Factor/reuse existing paid invoker construction at the shared CLI composition seam. Preparation must construct and authenticate local adapters without making a completion call or reading/exporting credential values.
- [x] Update operations and round validation to dispatch by authenticated provider authority. Keep started-with-no-journal controller recovery separate from uncertain paid calls; preserve reservations, receipt reconciliation, original deadlines and owner/config identity. Never fabricate controller receipts for paid calls.
- [x] Update runtime terminal provenance, run verification and summary/history authentication so paid development requires the same exact ledger-backed request/package accounting as paid production. Keep provider-free history and prior failed controller campaigns readable.
- [x] Search only relevant non-test source for remaining development/provider-free assumptions and document each resolved or intentionally retained guard. Compile proposed changed bytes in memory and run scoped Ruff on them; do not run CLI through a mixed staged/live import graph.
- [x] Write a unified patch, a complete proposed-file hash inventory, an implementation report and source-review checklist. Independent review must inspect proposed versus original bytes, configuration composition, paid uncertainty handling, history separation, outbound role packet bounds and scope fidelity. Correct findings in staging.

## Task 2: Apply only after the current campaign is terminal

**Files:** The exact reviewed proposed source set from Task 1 and this plan's progress record.

**Interfaces:** Consume `source-preimages.json`, proposed hashes and the reviewed patch. Produce the same reviewed implementation at the real source paths without overwriting intervening changes.

- [x] Authenticate runtime-04 terminal campaign outcome, checkpoint/records, cleanup and stopped evaluator handles. Actual round two ended with discovery_evaluation:deadline_exceeded after host standby; no second critic was issued. Preserve failure and checkpoint generation one; do not fabricate a critic or resume the terminal campaign.
- [x] Rehash every original source and compare with its preimage. If a path changed, reconcile that actual difference and re-review; never overwrite based only on file names.
- [x] Apply the exact reviewed bytes, verify resulting hashes, run scoped Ruff/in-memory compilation and the relevant actual CLI help. No tests, probes or remote calls.
- [ ] Verify both explicit development modes through source authority review and actual call-free preparation. Do not treat static checks as proof of a live provider campaign.

## Task 3: Rebuild and prepare a reviewable real campaign

**Files:** New immutable evaluator profile/contract and baseline reports; a separately named development campaign package; actual outbound packet/schema disclosure and launch/resume commands.

**Interfaces:** Consume the reviewed implementation and existing authenticated provisional data/panel selection. Produce new exact evaluator/baseline/campaign authority with a concrete external-call authorization request.

- [x] Determine the actual evaluator closure after the changes. Contracts/runtime are image-covered, so rebuild and seal a new local image/profile through the existing authorized elevated execution path. Never rebind old baseline reports to a new evaluator identity.
- [x] Run and authenticate the four real parent baseline panels under that new evaluator with cleanup receipts; preserve existing records as historical evidence.
- [x] Choose and document a concrete recommended exact model and finite call/token/wall bounds using verified public provider information and existing user preferences. Preserve optional USD policy; include price upper bounds only when needed by the chosen ceiling. No provider calls during selection/preparation.
- [x] Create the new manifest, provider capabilities, adapter configuration, owner, exact command, source/data/evaluator references and conservative outbound input/output bounds. Include exact initial serialized request bytes and the closed rules for later evidence-dependent packets. Disclose editable strategy source and aggregate performance/history plainly; no raw dataset, credentials or full repository export.
- [x] Present the complete reviewable packet for explicit external-call approval. Do not invoke the gateway before that approval names the model/destination/caps/outgoing scope.
- [ ] After approval, execute the bounded real campaign and inspect actual roles, candidate evaluation, feedback, checkpoint/recovery and cleanup evidence. Stop at its authorized limits; no apply or held-out expansion occurs automatically.

## Coverage check

The spec's authority, config, gateway reuse, recovery, history, source export, optional monetary policy, image identity and reviewable external authorization requirements each map to the tasks above. No test step or commit is included because the user's instructions and current approval state override those generic workflow steps. This plan does not claim a working provider development mode before real implementation and execution.

Task 1 completion evidence: provider-staging/root-review-receipt.json, independent-review.md, exact source inventories, patch and static verification receipts. Eight files include the narrowly reviewed provider.py false-positive fix supported by the original failed role response. No live application or external call has occurred.

Applied-source evidence: provider-staging/application-receipt.json and applied-ruff.txt; actual archived response passed without provider calls. New local build005064e57e80 completed; evaluator/builds/005064e57e80/profile-record.json seals image sha256:4e85b19aa8c135362ae8796e4d33a410ec5af650d296c358ae6a7737d5b05b6a and profile0923e93f56e06fc982c9db00815329d1cdc936c0a0b9a140d287b7611ad5b1c9. New real quick/baseline refresh remains pending.

Actual provider-runtime-01 preparation and read-only summary passed with zero calls/rounds/tokens/cost; all four 005064e57e80 baselines and quick are verified/cleaned. Exact outbound/cap disclosure in provider-runtime-01/CAMPAIGN-REVIEW.md. External-call approval requested and pending; execution step remains incomplete. Existing controller history verified via actual summarize; no new controller campaign was created merely for verification.

Latest execution status (2026-09-07): user approved campaign01, actual launch ended at first investigator transport_failure; zero evaluated candidates, cleanup complete. Original exception unavailable. Bounded local error diagnostics added and reviewed in provider.py without changing evaluator closure or settlement. Fresh campaign02 prepared and actual CLI ready; six additional calls await explicit grant. See provider-runtime-01/FAILURE-REPORT.md and provider-runtime-02/CAMPAIGN-REVIEW.md. Two-round success and final Luna handoff remain incomplete; previous pending-approval notes are historical.

Campaign02 execution (approved): first request rejected HTTP400; cleanup complete. Added shared provider-facing schema projection without changing canonical authority or evaluator closure. Scoped static checks, actual archived investigator/author schema checks, root and independent review passed. Corrected campaign03 is prepared and CLI ready with zero calls; real provider acceptance and two-round completion remain unproven. See wire-schema-repair/report.md and provider-runtime-03/CAMPAIGN-REVIEW.md. Proposed bounded continuation is pending user grant.

Latest continuation: campaign03 investigator accepted; author failed on bare changed_symbols, repaired with wire-only qualified symbol rule. Single approvedreplacement04 selected disabled full_source_escape and stopped before authorcall, repaired by passing realmanifestpermission towireprojector (not author-specific schemaflag). Both terminal/cleaned; combined3calls10679tokens$0.055885, no evaluatedcandidate. Root+independentreview and static/actualarchivedpreflight passed; imageunchanged. Campaign05 callfreeprepared/CLIready; oneadditionalcampaign extension proposed withinoriginal12call896000tokenbudget, pendingusergrant. Two-roundcompletion and post-completionLunahandoff remainunfinished.


## September 7 latest status: campaign05 terminal,06 prepared

Campaign05 failed before evaluation because author source omitted final LF. Two calls,8,438 tokens,$0.04007; cleanup complete. Provider-only contract guidance repaired and independently reviewed at SHA d9b17d62a6c8b485ba6ae6abb22a9e87f9d908b3c96409ba6e21f86eb691e937;23 actual archived schemas preserve accounting bounds. Canonical parsing and saved responses unchanged. Evaluator and completed baselines unchanged.

Fresh provider-runtime-06 is verified ready with no calls. Its CAMPAIGN-REVIEW.md and continuation-extension-proposal.json propose06 and at most one conditional replacement07 within the existing remaining7calls/876,883tokens. All currently authorized attempt slots are consumed; this proposal is pending approval. No06 launch has occurred. Detailed Luna handoff remains after actual two-round completion. See development/STATUS-20260907.md and role-contract-guidance-repair for current evidence; earlier plan status is historical.


## Latest execution: campaign06 terminal rendering failure

After explicit payload/destination authorization, campaign06 ran and accepted investigator/author outputs, then failed on forbidden AST constructs before evaluation.2 calls,8596 tokens,$0.04234;cleanup complete. Remaining5 calls do not meet the conditional replacement's6-call prerequisite, so no07 launch is eligible. Source-format/schema repairs passed this actual attempt; rejection feedback remains an architectural gap. See provider-runtime-06/FAILURE-REPORT.md and the new preparatory recovery-design/luna-recovery plans. No runtime change has yet been made for that gap; no two-evaluated-round completion is claimed.
