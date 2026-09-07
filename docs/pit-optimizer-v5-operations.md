# PIT optimizer V5 production operations

The production entry point is `python -B -m core.pit_optimizer_v5.cli` on the Windows host. Run it from the repository checkout containing these commands. `prepare-production` and `render-command` do not call a model or evaluate a candidate. `run`, `resume`, `run-campaign`, and `resume-campaign` can call the manifest-authorized provider and run the pinned Docker evaluator.

## Required campaign inputs

Start with a real, authenticated discovery manifest and every artifact it references. The manifest fixes the source commit, baseline, discovery panels, evaluator, sandbox image, provider limits, and resource limits. Setup does not invent these artifacts or modify the manifest. A separately persisted adapter configuration supplies the local host paths and adapter identities.

The source, workspace, data, output, and control directories must already exist and be mutually disjoint. For example, use sibling directories under `C:\pit-v5`, with a clean source checkout at `C:\pit-v5\source`. The data directory must contain exactly `pit_bundle.sqlite3` and `prices_provenance.json`, matching the manifest's evaluator digests. Keep the artifact repository outside the data directory. Git and Docker must be installed; their paths can be discovered from `PATH` or supplied explicitly.

Preparation checks the clean source commit and policy bytes, hashes the two data files without querying market or held-out domains, and constructs the actual production adapters to capture their identities. It does not start Docker or check daemon/image readiness. Before execution, make the manifest-pinned image available locally and configure `OPENROUTER_API_KEY` in the launch environment. The runtime uses the pinned image with pulling disabled.

If no real manifest exists yet, first complete the upstream data and panel work. The baseline input descriptor must reference the real evaluator contract, execution profile, sandbox profile, discovery panel plan, baseline policy revision, source bundle, policy scope, evaluator source, identity transitions, and resource capabilities. `prepare-baseline-inputs` authenticates that complete exact-path graph before publishing the descriptor create-only. It performs no evaluation, Docker action, provider call, directory scan, or held-out panel read. A provisional single-panel evaluation does not establish the full baseline or discovery campaign authority.

## Prepare and capture the baseline

Supply each existing artifact as a path relative to the canonical artifact root plus its SHA-256 digest. The command does not accept unverified identity placeholders. The final V3 PIT bundle, prices provenance, and discovery panels must already be reachable from the supplied discovery panel plan. The source bundle, policy revision, policy scope, evaluator contract, evaluator source map, identity-transition contract, and resource authority also need to have been built and persisted upstream; no safe production builder currently derives those artifacts from the panel plan alone.

```powershell
Set-Location 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture'
$python = (Get-Command python.exe).Source
$artifactRoot = Read-Host 'Absolute artifact repository directory'
$baselineArgs = @(
    '--artifact-root', $artifactRoot,
    '--output-path', 'evaluator/baseline-capture-inputs.json',
    '--evaluator-contract-path', '<path>', '--evaluator-contract-sha256', '<sha256>',
    '--execution-profile-path', '<path>', '--execution-profile-sha256', '<sha256>',
    '--sandbox-profile-path', '<path>', '--sandbox-profile-sha256', '<sha256>',
    '--panel-plan-path', '<path>', '--panel-plan-sha256', '<sha256>',
    '--baseline-policy-revision-path', '<path>', '--baseline-policy-revision-sha256', '<sha256>',
    '--source-bundle-path', '<path>', '--source-bundle-sha256', '<sha256>',
    '--policy-scope-path', '<path>', '--policy-scope-sha256', '<sha256>',
    '--evaluator-source-path', '<path>', '--evaluator-source-sha256', '<sha256>',
    '--identity-transition-path', '<path>', '--identity-transition-sha256', '<sha256>',
    '--resources-path', '<path>', '--resources-sha256', '<sha256>'
)
$preparedLine = & $python -B -m core.pit_optimizer_v5.cli prepare-baseline-inputs @baselineArgs
if ($LASTEXITCODE -ne 0) { $preparedLine; throw 'Baseline input preparation failed' }
$prepared = ($preparedLine -replace '^PIT_OPTIMIZER_V5_BASELINE=', '') | ConvertFrom-Json

# Fill the explicit host paths and executable hashes shown as placeholders in
# $prepared.capture_argv, then run that argv. Equivalently:
& $python -B -m core.pit_optimizer_v5.cli capture-baseline `
    --artifact-root $artifactRoot `
    --capture-inputs-path $prepared.artifact_ref.relative_path `
    --capture-inputs-sha256 $prepared.artifact_ref.sha256 `
    --output-path 'evaluator/baseline-authority.json' `
    --source-root '<absolute-clean-source-root>' `
    --scratch-root '<absolute-disjoint-scratch-root>' `
    --trusted-git-executable '<absolute-git-executable>' `
    --trusted-git-sha256 '<git-sha256>' `
    --docker-executable '<absolute-docker-executable>' `
    --docker-sha256 '<docker-sha256>'
```

Baseline capture requires a clean checkout at the exact source commit named by the policy scope, a disjoint empty scratch root, the locally available digest-pinned evaluator image, and authenticated Git and Docker executable hashes. It runs two fresh network-disabled full-grid evaluations and publishes authority only after byte-identical results and cleanup. Verify the resulting reference with `verify-baseline`, then pass that authority to `build-manifest`.

## Prepare the local adapter configuration

These PowerShell commands prompt for the real artifact repository and manifest path. All `*-path` artifact arguments are relative to that repository; host directory arguments are absolute. Substitute the existing host directory paths in `$prepareArgs` if different.

```powershell
Set-Location 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture'
$python = (Get-Command python.exe).Source
$artifactRoot = Read-Host 'Absolute artifact repository directory'
$manifestPath = Read-Host 'Discovery manifest path relative to the artifact repository'
$manifestSha = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $artifactRoot $manifestPath)).Hash.ToLowerInvariant()
$manifestArgs = @('--artifact-root', $artifactRoot, '--manifest-path', $manifestPath, '--manifest-sha256', $manifestSha)

& $python -B -m core.pit_optimizer_v5.cli verify-manifest @manifestArgs
if ($LASTEXITCODE -ne 0) { throw 'Manifest authentication failed' }

$prepareArgs = @(
    '--source-root', 'C:\pit-v5\source',
    '--workspace-root', 'C:\pit-v5\workspaces',
    '--data-root', 'C:\pit-v5\data',
    '--output-root', 'C:\pit-v5\output',
    '--control-root', 'C:\pit-v5\control',
    '--round-index', '1',
    '--output-path', 'host/adapter-config-round-1.json'
)
$preparedLine = & $python -B -m core.pit_optimizer_v5.cli prepare-production @manifestArgs @prepareArgs
if ($LASTEXITCODE -ne 0) { $preparedLine; throw 'Production preparation failed' }
$preparedLine | Set-Content -LiteralPath (Join-Path $artifactRoot 'production-preparation.txt')
$prepared = ($preparedLine -replace '^PIT_OPTIMIZER_V5_OPERATIONS=', '') | ConvertFrom-Json
$configArgs = @(
    '--adapter-config-path', $prepared.adapter_config_ref.relative_path,
    '--adapter-config-sha256', $prepared.adapter_config_ref.sha256,
    '--owner-token-sha256', $prepared.owner_token_sha256
)
```

To choose executable paths explicitly, add `--git-executable 'C:\Program Files\Git\cmd\git.exe'` and the real `--docker-executable` path to the preparation invocation. Setup emits a generated owner token when omitted. Save its output: the config identities include the round and owner, so changing either requires preparing the corresponding configuration. For a repeated preparation at the same output path, supply the saved `--owner-token-sha256`; an existing different artifact is never overwritten.

## Render and execute

```powershell
$renderedLine = & $python -B -m core.pit_optimizer_v5.cli render-command @manifestArgs @configArgs --round-index 1 --execution-command run-campaign
if ($LASTEXITCODE -ne 0) { $renderedLine; throw 'Command rendering failed' }
$rendered = ($renderedLine -replace '^PIT_OPTIMIZER_V5_MANIFEST=', '') | ConvertFrom-Json
$rendered.authorization.powershell_command

# This command begins provider and evaluator execution.
& $python -B -m core.pit_optimizer_v5.cli run-campaign @manifestArgs @configArgs
```

Rendering returns an `argv` array, working directory, and PowerShell command with literal quoting, the manifest/config digests, and the owner/round values. Execute the displayed command from its stated working directory, or use the explicit invocation above. Without `--adapter-config-path` and `--adapter-config-sha256`, `render-command` retains the manifest-only preview with `executable: false`.

To execute one prepared round instead, use `run @manifestArgs @configArgs --round-index 1`; use `resume` with those same arguments for an existing round. Single-round commands retain their existing behavior. Use campaign commands when the persisted campaign and per-round deadlines must span process restarts.

## Resume and bounded stopping

```powershell
# In a new shell, reconstruct $manifestArgs from the same manifest and reload:
$prepared = ((Get-Content -Raw -LiteralPath (Join-Path $artifactRoot 'production-preparation.txt')) -replace '^PIT_OPTIMIZER_V5_OPERATIONS=', '') | ConvertFrom-Json
$configArgs = @(
    '--adapter-config-path', $prepared.adapter_config_ref.relative_path,
    '--adapter-config-sha256', $prepared.adapter_config_ref.sha256,
    '--owner-token-sha256', $prepared.owner_token_sha256
)
& $python -B -m core.pit_optimizer_v5.cli resume-campaign @manifestArgs @configArgs

$verificationArgs = @(
    '--adapter-config-path', $prepared.adapter_config_ref.relative_path,
    '--adapter-config-sha256', $prepared.adapter_config_ref.sha256
)
& $python -B -m core.pit_optimizer_v5.cli verify-run @manifestArgs @verificationArgs
& $python -B -m core.pit_optimizer_v5.cli summarize @manifestArgs @verificationArgs
```

The campaign launcher serializes controllers for the same manifest using the repository's existing state lock. It persists launch/config/start records through the repository's existing create-only state API and calls `run_feedback_round_v5` sequentially. Subsequent rounds use derived configs under `campaigns/<manifest-sha256>/adapter-config-round-N.json`, with unchanged host paths and limits. Checkpoint-owned experiment records and terminal/cleanup journal entries remain the authority for completed work; there is no second optimizer or completion ledger.

Completed rounds with complete cleanup are reused. `no_novel_hypothesis` advances according to the existing scheduler; novelty exhaustion, critic unavailability, runtime failure, incomplete cleanup, exhausted provider budget, or campaign expiry stops the loop. The role ledger still enforces prospective token/cost/call bounds before each paid attempt, including when the remaining budget cannot fund the next request. Such a refusal can surface through the existing runtime failure diagnostics.

Campaign and round start times persist in epoch milliseconds. Resume subtracts elapsed wall time, including downtime, and passes the smaller remaining allowance to the runtime's monotonic deadline. It never resets those allowances or requires a full round's time to remain. This assumes an accurate host wall clock; a clock before a saved start is rejected. Cleanup retains its separate manifest timeout and can finish after the work deadline. Runtime calls remain responsible for honoring their supplied stage deadlines; this is not an external process watchdog.

Resume uses the original config and owner. A round with a saved start but no round journal is reported as uncertain and is not restarted automatically; inspect its paid-request/reservation state using the existing recovery boundary before taking further action. Existing journaled work uses the runtime's reconciliation path. Do not delete launch records or create a new campaign to evade unsettled work or reset budgets. Held-out confirmation, qualification, full replay, and live application remain separate explicit stages.
