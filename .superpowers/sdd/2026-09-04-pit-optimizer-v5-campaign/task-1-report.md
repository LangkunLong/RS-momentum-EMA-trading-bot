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
