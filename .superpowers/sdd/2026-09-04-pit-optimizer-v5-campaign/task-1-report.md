# Task 1 report — deterministic V5 episode plans

## Status

Source-first Task 1 is complete. The implementation did not read or reuse any market artifact, did
not call a provider, and did not create, modify, or run tests. No real panel, ledger, confirmation,
or qualification artifact was emitted.

The designated worktree was clean at start, on `codex/pit-optimizer-v5-architecture`, with both
`HEAD` and merge-base equal to `3548febcb2c51cf5ed13c2dae6eadddeaa6ed5c8`.

## Implementation

- Added the canonical V5 controller-held contracts for confirmation and qualification plans,
  retirement-ledger locators, stage attempt commitments, and terminal outcomes.
- Outcome contracts reject caller-supplied gate drift. Confirmation eligibility is exactly
  `completed AND behaviorally_active AND candidate_cagr_pct > baseline_cagr_pct`.
  Qualification is exactly `completed AND candidate_cagr_pct >= target_pct AND
  candidate_cagr_pct > baseline_cagr_pct`. Non-completed results retain terminal retirement and
  cleanup references while forcing null metrics and false gates. Both stages require zero provider
  calls.
- Added `core/pit_optimizer_v5/panels.py` with a deterministic, affiliation-bitset-stratified
  allocator. Allocation protects held-out capacity first and produces exactly 24 mechanics, 96
  quick, four disjoint 150-lineage discovery cohorts, 400 confirmation lineages, and 500
  qualification lineages.
- Security aliases are grouped by immutable lineage before allocation. A lineage's affiliation
  stratum is the canonical union of its point-in-time three-universe affiliations, so overlapping
  S&P 500/Nasdaq-100/Russell-2000 membership remains one security.
- The authenticated bundle adapter requires schema V3, its exact price-identity provenance, the
  2021–2025 session calendar, active membership, price coverage for every active occurrence, and a
  causal price prefix before a lineage is eligible for an episode.
- The initial schedule is sealed as 2021 H1, 2021 H2, 2022, and 2023 discovery; 2024 confirmation;
  and 2025 qualification. Discovery ordinals are exactly 1–4. Mechanics and quick use the 2021 H1
  session authority and remain unnumbered.
- Retained the legacy raw `EvaluationPanelSpec` purpose vocabulary exactly as ruled: mechanics maps
  to raw `quick`, confirmation maps to raw `qualification`. Stage authority exists only in the typed
  V5 owner/commitment/command. Composition and verification require all typed owners and reject a
  raw-purpose substitution.
- Added create-or-authenticate immutable repository operations for canonical V5 dataclasses and the
  existing newline-bearing `EvaluationPanelSpec` encoding. Every complete panel is stored beneath
  `panels/specs/` and its digest-bearing reference is held by its episode.
- Added a V5 stage adapter over the established schema-V4 append-only retirement ledger. It derives
  distinct confirmation/qualification domains from stage plus authenticated bundle/provenance and
  lineage authorities, initializes only an unopened genesis, and seals an immutable V5 pre-open
  snapshot without reusing schema-V4 plan or target types.
- Discovery stores only canonical held-out plan digests. No held-out path or deserializer is added to
  discovery/search composition. The provider-facing discovery projection includes only digests,
  dates, counts, and Boolean separation facts—never paths, lineage IDs, membership, signals, trades,
  outcomes, source, or returns.
- Added typed attempt-binding helpers. Confirmation must bind the frozen typed owner and immutable
  pre-open snapshot. Qualification additionally requires a completed, recomputed-eligible
  confirmation outcome and a separate affirmative operator decision; approval cannot override an
  ineligible outcome.
- Registered `init-stage-ledgers`, `build-panels`, and `verify-panels` in the existing V5 CLI.
  Commands use an absolute artifact root plus canonical relative path/digest references. Build owns
  three distinct create-only outputs. Verify authenticates all owner files and raw specs, checks
  commitment/time/security separation, and emits only the content-free projection.
- Exported the new contracts and panel APIs from the package.

## Commands and observed output

No test command was run.

1. Worktree preflight:

   `git branch --show-current`, `git rev-parse HEAD`, and `git merge-base HEAD 3548feb...`

   Observed: branch `codex/pit-optimizer-v5-architecture`; HEAD and merge-base both
   `3548febcb2c51cf5ed13c2dae6eadddeaa6ed5c8`; clean starting status.

2. Bounded fake-only deterministic/separation/gate command supplied on Python stdin (1,700 synthetic
   lineages; no test file):

   `synthetic-task1: deterministic=yes seed-sensitive=yes disjoint=1620 projection=content-free owner-bypass=rejected gates=recomputed`

3. Bounded fake-only attempt-binding command supplied on Python stdin:

   `synthetic-attempts: typed-owner-bindings=yes immutable-snapshots=yes approval-cannot-override=yes`

4. Bounded fake-only repository encoding command, run outside the managed filesystem sandbox only
   because the established repository intentionally obtains Win32 no-delete-share directory handles:

   `synthetic-create-only: typed=idempotent panel=idempotent authenticated=yes`

   Its temporary directory was inside this worktree and was removed. Two empty temporary directories
   left by the preceding sandbox-denied attempts were also removed by exact literal path.

5. Bounded fake-only end-to-end artifact round trip through the real repository codec:

   `synthetic-artifact-e2e: specs=8 owner-plans=3 decode=authenticated separation=verified`

6. CLI ownership:

   - `python -B -m core.pit_optimizer_v5.cli init-stage-ledgers --help`
   - `python -B -m core.pit_optimizer_v5.cli build-panels --help`
   - `python -B -m core.pit_optimizer_v5.cli verify-panels --help`

   Observed: all three parsers loaded successfully and displayed only their closed command arguments.

7. Static/import verification:

   - `python -B -m compileall -q core/pit_optimizer_v5`
   - `python -B -c "import core.pit_optimizer_v5.panels; import core.pit_optimizer_v5.cli; import core.pit_optimizer_v5"`
   - `python -B -m ruff check core/pit_optimizer_v5/panels.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/artifacts.py core/pit_optimizer_v5/__init__.py`
   - `git diff --check`

   Observed: compile and imports succeeded; Ruff reported `All checks passed!`; diff checking passed.

## Commands now owned by Task 1

- `init-stage-ledgers`: authenticates the schema-V3 bundle/provenance over the requested 2021–2025
  calendar, derives two distinct retirement domains, creates/authenticates their genesis ledgers, and
  seals immutable pre-open snapshots.
- `build-panels`: reauthenticates the same inputs and unopened ledgers; accepts seed, exact V5 target,
  date range, and three distinct output paths; then creates complete raw specs and typed owner plans.
- `verify-panels --keep-held-out-sealed`: authenticates explicit relative-path/digest references,
  verifies all owner/spec bindings and pairwise stage separation, and emits only content-free facts.

## Remaining runtime blocker

The exact non-blocking runtime blocker is unchanged: this isolated worktree does not contain the
authenticated V5 schema-V3 three-universe PIT bundle and matching authenticated price-identity
provenance. Therefore Task 1 cannot legitimately initialize the real stage domains or emit/verify the
real discovery, confirmation, and qualification plan artifacts. Those actions must wait for that
exact authenticated bundle; artifacts from the parent/main worktree must not be substituted.

## Concerns

- Actual bundle-derived capacity and causal-coverage counts have not been observed. The builder will
  fail closed if fewer than 1,620 disjoint eligible lineages satisfy their assigned episodes.
- Stage opening/terminal retirement execution remains intentionally owned by later confirmation and
  qualification tasks. Task 1 seals the domain, immutable pre-open snapshot, typed attempt contract,
  and binding rules but does not open either held-out plan.

## Fix round 1 — authority-gap correction (2026-09-05)

### Changed scope

- Bound the confirmation and qualification retirement domains to their single canonical mutable
  ledger locations, respectively `panels/confirmation-retirement.json` and
  `panels/qualification-retirement.json`. The location is now included in the domain preimage and
  immutable snapshot, and is reauthenticated by initialization, panel build, owner verification,
  confirmation-attempt construction, and qualification-attempt construction. Substituting an
  alternate relative path fails before bundle loading or ledger creation.
- Initialization now authenticates both raw bundle authorities inside the public operation rather
  than relying on its CLI caller. Panel build likewise authenticates the raw inputs before reading
  their structure. Confirmation-attempt construction resolves its plan, manifest, discovery owner,
  immutable snapshot, and live unopened ledger through the supplied repository.
- Replaced the former one-prefix-row eligibility test with an explicit causal feature-history
  contract. Every active occurrence must have its current ticker row and a continuous 252-session
  lineage window over authenticated aliases; this includes the event plus 50 prior rows required by
  ADV50 and the 252 rows required by distance from the annual high. A missing session anywhere in
  the window fails eligibility before allocation.
- Added the immutable `ScenarioGridV5` artifact contract for the attempt commitments' existing
  `scenario_grid_ref`, plus the repository's public authenticated raw-edge adapter.
- Qualification-attempt construction now resolves the confirmation outcome, confirmation attempt,
  campaign manifest, discovery plan, held-out confirmation plan and raw panel, discovery champion
  experiment/policy, evaluator, execution profile, sandbox, scenario grid, raw bundle/provenance,
  baseline authority/policy, both same-panel evaluations, retirement terminal, and cleanup evidence.
  It proves campaign, policy, panel, scenario, date, evaluator, sandbox, stage-domain, and snapshot
  bindings. The confirmation baseline/candidate CAGRs are recomputed from authenticated equity
  endpoints, the activity and strict-excess gate is rebuilt, and the stored outcome must equal that
  rebuilt outcome exactly. Missing, relocated, cross-campaign, digest-substituted, or inconsistent
  evidence fails closed.
- Kept the ruled legacy raw-panel purpose vocabulary unchanged. Confirmation's raw spec remains
  `qualification`; the authenticated `ConfirmationPanelPlanV5`, confirmation attempt, canonical
  stage domain, and confirmation command remain the only stage authority. The confirmation evidence
  graph now authenticates the raw spec only through that typed owner, so raw purpose cannot bypass
  stage separation.
- No test file was created, modified, read, or run. No provider, Docker, market-data evaluation,
  Git materialization, replay, network, stage opening, or real artifact creation occurred.

### Commands and observed output

1. Fix-base preflight:

   `git rev-parse HEAD` and `git branch --show-current`

   Observed: `9a7665cb7273203b91641bdc0e16ce39cf1987df` on
   `codex/pit-optimizer-v5-architecture`, with a clean starting tree.

2. Bounded direct synthetic path/history command (in-memory values only; no test file):

   `synthetic-fix1: alternate-ledger=rejected feature-history=252-continuous alias-continuity=yes gap-and-prefix=rejected`

   This exercised alternate build-ledger and snapshot-path rejection, successful continuous coverage
   across two aliases, and rejection of both one-prior-row and internally gapped histories.

3. Bounded direct synthetic authenticated-ancestry command (1,700 synthetic lineages and in-memory
   repository fake; no test file):

   `synthetic-fix1-ancestry: valid-graph=accepted panel-owner=authenticated cross-campaign=rejected substituted-evidence=rejected gate=recomputed`

   This exercised the complete qualification precondition graph, including typed confirmation panel
   ownership, then independently substituted the discovery campaign identity and candidate evidence.

4. Static/import checks:

   - `python -m compileall -q core/pit_optimizer_v5`
   - `python -c "import core.pit_optimizer_v5.panels; import core.pit_optimizer_v5.cli; import core.pit_optimizer_v5"`
   - `python -m ruff check core/pit_optimizer_v5/panels.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/artifacts.py core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/__init__.py`
   - `git diff --check`

   Observed: compile/import succeeded without output; Ruff reported `All checks passed!`; diff check
   exited zero (Git emitted only the repository's existing LF-to-CRLF checkout notices).

5. CLI ownership checks:

   - `python -B -m core.pit_optimizer_v5.cli init-stage-ledgers --help`
   - `python -B -m core.pit_optimizer_v5.cli build-panels --help`
   - `python -B -m core.pit_optimizer_v5.cli verify-panels --help`

   Observed: all three parsers loaded and returned exit zero. Initialization now accepts only
   authenticated bundle/provenance references and the two canonical stage-ledger path arguments;
   build and verify retain their prior closed V5 ownership arguments.

### Remaining actual-bundle blocker and concerns

The exact non-blocking runtime blocker remains unchanged: this isolated worktree does not contain
the authenticated V5 schema-V3 three-universe PIT bundle and its matching authenticated
price-identity provenance. The real ledgers and panel artifacts therefore were not initialized,
built, or verified, and no parent/main-worktree artifact was accessed or substituted.

Actual bundle-derived capacity and complete causal-history counts remain unobserved. The source now
fails closed for fewer than 1,620 eligible disjoint lineages, for any active lineage lacking its full
252-session feature window, or for any absent/inconsistent evidence edge in the confirmation graph.

## Fix round 2 — non-creating reads and confirmation activity authority (2026-09-06)

### Changed scope

- Added a strict existing-ledger resolver for every non-initialization path. Panel build and live
  confirmation/qualification attempt authentication now require the canonical ledger path to exist
  as a regular, non-symlink file before calling the legacy retirement-ledger constructor. Only
  `init-stage-ledgers` retains authority to invoke that constructor for an absent path, so deletion
  of a retired canonical ledger cannot silently recreate a matching deterministic genesis during a
  later build or attempt.
- Replaced scenario-object inequality as the confirmation activity signal. Qualification gating now
  derives `behaviorally_active` exclusively from authenticated selected/base-scenario trade evidence:
  `selected_scenario(candidate).report.closed_trades != 0`. The same-panel baseline and candidate
  CAGRs remain recomputed from authenticated equity endpoints and compared strictly. The rebuilt
  outcome must still equal the stored outcome exactly, so missing or caller-mismatched trade/gate
  evidence fails closed.
- Expanded confirmation-attempt construction to resolve and cross-bind every carried dependency
  before returning the commitment: confirmation owner and raw panel, discovery manifest/plan,
  champion policy and experiment, raw PIT bundle/provenance, execution profile, evaluator contract,
  immutable scenario grid, baseline authority/policy, policy scope, sandbox profile, pre-open
  snapshot, retirement domain, and canonical ledger location. The live unopened ledger comparison
  remains the final pre-opening check.
- No tests or test files were created, modified, read, or run. No real market artifact, provider,
  Docker, market-data evaluation, network, Git materialization, replay, push, merge, or stage opening
  was used.

### Commands and observed output

1. Preflight:

   `git status --short --branch`, `git rev-parse HEAD`, `git branch --show-current`

   Observed: clean `codex/pit-optimizer-v5-architecture` at
   `31c063b35db6be8b73cf791445fd2fc0c83ec8b4`.

2. Bounded missing-ledger direct fake:

   `synthetic-fix2-ledger: missing-canonical=rejected constructor-before-check=never-invoked`

   The fake replaced the legacy constructor with an assertion bomb and proved the absent canonical
   path was rejected before constructor invocation. The first attempt used Python's system temporary
   directory and its cleanup hit a Windows sandbox ACL (`PermissionError: [WinError 5]`); it produced
   no pass result. The check was rerun in an exact disposable directory inside this worktree, passed,
   and that directory was removed. The exact inaccessible system temporary directory from the failed
   attempt was then removed with elevated permission and verified absent.

3. Bounded in-memory confirmation/qualification graph fake:

   `synthetic-fix2-graph: confirmation-dependencies=authenticated substitution=rejected zero-trade-beats-losing-baseline=ineligible active-selected-trades=eligible`

   The first run correctly rejected the substituted evaluator through manifest validation, but the
   command's assertion expected a later error spelling and therefore exited nonzero. The corrected
   assertion accepted either fail-closed consistency layer; the complete rerun then passed. It
   authenticated a valid confirmation dependency graph, rejected a different authenticated evaluator,
   rejected a zero-trade candidate even though it beat a losing baseline, and accepted a one-trade
   candidate whose same-panel CAGR strictly beat baseline.

4. Final source verification:

   - `python -m compileall -q core/pit_optimizer_v5`
   - `python -c "import core.pit_optimizer_v5.panels; import core.pit_optimizer_v5.cli; import core.pit_optimizer_v5"`
   - `python -m ruff check core/pit_optimizer_v5/panels.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/artifacts.py core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/__init__.py`
   - `python -B -m core.pit_optimizer_v5.cli init-stage-ledgers --help`
   - `python -B -m core.pit_optimizer_v5.cli build-panels --help`
   - `python -B -m core.pit_optimizer_v5.cli verify-panels --help`
   - `git diff --check`

   Observed: compile/import and all three CLI parsers exited zero; Ruff reported
   `All checks passed!`; diff checking exited zero with only the repository's LF-to-CRLF checkout
   notice.

### Remaining blocker and concerns

The exact non-blocking runtime blocker remains the missing authenticated V5 schema-V3 three-universe
PIT bundle and matching price-identity provenance in this isolated worktree. No real ledger or panel
artifact was created and no parent/main-worktree artifact was inspected or reused. Actual eligible
capacity/history counts and real filesystem ledger lifecycle remain unobserved until that bundle is
available under the authorized V5 artifact root.
