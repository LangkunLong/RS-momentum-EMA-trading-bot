# PIT optimizer V5 production operations

The production entry point is `python -B -m core.pit_optimizer_v5.cli` on the Windows host. Run it from the repository checkout containing these commands. `prepare-production` and `render-command` do not call a model or evaluate a candidate. `run`, `resume`, `run-campaign`, and `resume-campaign` can call the manifest-authorized provider and run the pinned Docker evaluator.

## Required campaign inputs

Start with a real, authenticated discovery manifest and every artifact it references. The manifest fixes the source commit, baseline, discovery panels, evaluator, sandbox image, provider limits, and resource limits. Setup does not invent these artifacts or modify the manifest. A separately persisted adapter configuration supplies the local host paths and adapter identities.

Current manifests and baseline-parent authorities must contain an explicit `pit_data_scope`. The Python constructor default preserves source-call compatibility; it is not a migration for persisted JSON because typed artifact decoding requires exact keys. Pre-scope bytes remain readable and content-authenticatable under their original references, but typed manifest or parent loading reports that the graph must be rebuilt. Retain those artifacts unchanged, create a replacement manifest and parent with explicit scope, and use the newly computed content-addressed references throughout the rebuilt graph. Never edit old bytes in place or reuse their digests for re-emitted content.

The source, workspace, data, output, and control directories must already exist and be mutually disjoint. For example, use sibling directories under `C:\pit-v5`, with a clean source checkout at `C:\pit-v5\source`. The data directory must contain exactly `pit_bundle.sqlite3` and `prices_provenance.json`, matching the manifest's evaluator digests. Keep the artifact repository outside the data directory. Git and Docker must be installed; their paths can be discovered from `PATH` or supplied explicitly.

Preparation checks the clean source commit and policy bytes, hashes the two data files without querying market or held-out domains, and constructs the actual production adapters to capture their identities. It does not start Docker or check daemon/image readiness. Before execution, make the manifest-pinned image available locally and configure `OPENROUTER_API_KEY` in the launch environment. The runtime uses the pinned image with pulling disabled.

If no real manifest exists yet, first complete the upstream data and panel work. The baseline input descriptor must reference the real evaluator contract, execution profile, sandbox profile, discovery panel plan, baseline policy revision, source bundle, policy scope, evaluator source, identity transitions, and resource capabilities. `prepare-baseline-inputs` authenticates that complete exact-path graph before publishing the descriptor create-only. It performs no evaluation, Docker action, provider call, directory scan, or held-out panel read. A provisional single-panel evaluation does not establish the full baseline or discovery campaign authority.

## Prepare and capture the baseline

The recommended `--compose-source` mode derives the source bundle, baseline policy revision, policy scope, and evaluator contract. It pins a clean checkout at the declared commit and binds the derived identities to the authenticated sandbox runtime, immutable-constraints configuration, execution profile, evaluator source map, transition contract, resources, and discovery panel plan. The final V3 PIT bundle, prices provenance, and six development panels must already be reachable from that plan.

```powershell
Set-Location 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture'
$python = (Get-Command python.exe).Source
$artifactRoot = Read-Host 'Absolute artifact repository directory'
$sourceRoot = 'C:\pit-v5\source'
$scratchRoot = 'C:\pit-v5\baseline-scratch'
$git = (Get-Command git.exe).Source
$docker = (Get-Command docker.exe).Source
$baselineArgs = @(
    '--artifact-root', $artifactRoot,
    '--output-path', 'evaluator/baseline-capture-inputs.json',
    '--compose-source',
    '--source-commit', '<clean-source-commit-sha1>',
    '--source-root', $sourceRoot, '--scratch-root', $scratchRoot,
    '--git-executable', $git, '--docker-executable', $docker,
    '--immutable-constraints-path', '<path>', '--immutable-constraints-sha256', '<sha256>',
    '--source-bundle-output-path', 'evaluator/baseline-source-bundle.json',
    '--policy-revision-output-path', 'evaluator/baseline-policy-revision.json',
    '--policy-scope-output-path', 'evaluator/baseline-policy-scope.json',
    '--evaluator-contract-output-path', 'evaluator/baseline-evaluator-contract.json',
    '--execution-profile-path', '<path>', '--execution-profile-sha256', '<sha256>',
    '--sandbox-profile-path', '<path>', '--sandbox-profile-sha256', '<sha256>',
    '--panel-plan-path', '<path>', '--panel-plan-sha256', '<sha256>',
    '--evaluator-source-path', '<path>', '--evaluator-source-sha256', '<sha256>',
    '--identity-transition-path', '<path>', '--identity-transition-sha256', '<sha256>',
    '--resources-path', '<path>', '--resources-sha256', '<sha256>'
)
$preparedLine = & $python -B -m core.pit_optimizer_v5.cli prepare-baseline-inputs @baselineArgs
if ($LASTEXITCODE -ne 0) { $preparedLine; throw 'Baseline input preparation failed' }
$prepared = ($preparedLine -replace '^PIT_OPTIMIZER_V5_BASELINE=', '') | ConvertFrom-Json

$prepared.powershell_command
Invoke-Expression $prepared.powershell_command
```

Preparation hashes the actual Git and Docker executable bytes; optional `--git-sha256` and `--docker-sha256` arguments verify independently supplied digests. The emitted command is fully populated but is not executed by preparation. Baseline capture requires the locally available digest-pinned evaluator image and runs two fresh network-disabled full-grid evaluations. Existing prebuilt source, revision, scope, and evaluator artifacts remain supported by omitting `--compose-source` and supplying their four path/digest pairs.

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

### Controller-authored development role responses

For a provider-free manifest with `pit_data_scope=development_sp500_v2`, construct
`FileBackedControllerRoleInvokerV5` with an absolute response directory. For each
durable role request, place one file named `<call-key-sha256>.json` in that
directory. Its JSON object has exactly these fields:

```json
{
  "schema_version": 5,
  "artifact_type": "controller_role_response",
  "call_key_sha256": "<exact call key from the pending result>",
  "request_sha256": "<exact request digest from the pending result>",
  "response": {"artifact": {}, "binding": {}}
}
```

`response` is the existing investigator, author, or critic response envelope and
must satisfy that request's schema and evidence binding. The placeholders above
describe the contract; they are not execution identities or a valid role result.

When the file is absent, `run_feedback_round_v5` returns
`awaiting_controller_response` with the role, call key, request digest, and durable
request reference. It writes no role completion, round outcome, experiment, or
checkpoint. Resume with the same repository and inputs after writing the file.
The first bytes found for that exact call are sealed create-only in the repository;
all later recovery reads those sealed bytes. A malformed or mismatched submitted
response therefore remains a truthful failed terminal attempt and cannot be
silently replaced by editing the input file.

This path records exact zero external-provider usage. It is controller-authored
development input, distinct from deterministic synthetic fixtures, and it does
not run an automatic external-model campaign. Production provider-free execution
continues to require fixture authority, while paid production continues to require
ledger authority.
