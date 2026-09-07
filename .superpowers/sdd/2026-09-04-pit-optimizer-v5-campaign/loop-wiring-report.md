# Production loop wiring report — 2026-09-07

Implemented the production setup/render/sequential-campaign path. No model calls, evaluator launches, held-out domain openings, market-authority fabrication, or test-file activity occurred in this task. No tests were created, read, modified, or run.

## Changed files

- `core/pit_optimizer_v5/cli.py`: adds `prepare-production`, `run-campaign`, and `resume-campaign`; extends `render-command` with an explicit adapter config, round, owner, and execution-command selection. Extracts the existing factory construction into `_compose_production_round_from_paths_v5`, shared by preparation and `ProductionRoundFactoryV5`. The factory validates the resulting exact adapter graph. Existing manifest-only rendering stays a non-executable preview. Rendering failures now include a local actionable diagnostic.
- `core/pit_optimizer_v5/operations.py`: new host-only wiring. Preparation verifies the manifest-selected data digests and clean source, discovers or accepts Git/Docker paths, derives every existing config identity from the concrete production adapters, and persists `ProductionAdapterConfigV5`. Rendering emits argv, working directory, and a literal-quoted PowerShell command. Campaign orchestration uses the existing repository state lock/create-only records, real journal/checkpoint state, concrete round factory, and `run_feedback_round_v5`.
- `core/pit_optimizer_v5/runtime.py`: controller explicitly authorized this additional small boundary. `run_feedback_round_v5(..., campaign_deadline_monotonic=...)` can only shorten the existing round deadline. Terminal recovery can still perform owned cleanup after that deadline; it cannot start new optimization work.
- `docs/pit-optimizer-v5-operations.md`: PowerShell commands for real manifest verification, preparation, rendering, launch, resume, and verification; input prerequisites and practical limits.
- This report. No manifest schema change, evaluator runtime/image change, controller ledger change, or data artifact change.

## Execution and recovery behavior

Adapter identities include round and owner. Preparation therefore reports both with the saved config digest. Campaign launch uses the original round-1 config/owner and derives each subsequent round's config from the same paths and concrete adapters. It revalidates the original host identities when launching/resuming. Configs are separately hash-bound; no manifest migration is required.

Campaign and round start epoch times are durable. Resume includes process downtime in elapsed time and supplies the smaller remaining round/campaign allowance to the runtime's monotonic deadline. No manifest call, token, cost, round-count, or wall-time limit is increased. Cleanup retains the existing separately bounded timeout. This is cooperative runtime deadline enforcement, not an external watchdog, and assumes the host wall clock is accurate. A clock before a persisted start is rejected.

Completed checkpoint-owned rounds and terminal rounds with complete cleanup are reused. Existing `no_novel_hypothesis` scheduling can advance; novelty exhaustion, critic unavailability, runtime failure, incomplete cleanup, explicit budget exhaustion, or campaign expiry stops. The real provider ledger remains the authority for each prospective paid reservation, so an unaffordable next request can also stop through the existing runtime failure path.

Pending journaled rounds use runtime reconciliation. Complete ledger-versus-journal verification is deliberately deferred until recovery can reconcile a paid reservation. A saved start with no round journal is reported as uncertain and never automatically restarted. Its existing single-round/provider recovery boundary needs operator inspection; the launcher does not delete state or reset authority. Fresh campaign launch rejects prior journal/checkpoint state and unjournaled paid reservations.

## Verification performed

All commands ran in `C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture`.

```powershell
python -B -m core.pit_optimizer_v5.cli prepare-production --help
python -B -m core.pit_optimizer_v5.cli render-command --help
python -B -m core.pit_optimizer_v5.cli run-campaign --help
python -B -m core.pit_optimizer_v5.cli resume-campaign --help
python -B -m ruff format core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py
python -B -m ruff check core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py
git diff --check -- core/pit_optimizer_v5/cli.py core/pit_optimizer_v5/runtime.py
```

In-memory compilation, with no bytecode or test execution:

```powershell
@'
from pathlib import Path
for name in ('cli.py', 'operations.py', 'runtime.py'):
    p = Path('core/pit_optimizer_v5') / name
    compile(p.read_text(), str(p), 'exec')
    print('compiled ' + str(p))
'@ | python -B -
```

CLI help exposed the expected actual arguments. Compilation and Ruff passed. An initial format check requested formatting; the formatter was then applied to the three owned Python files. The final whitespace check passed. Git and Docker executables were discovered on this host at `C:\Program Files\Git\cmd\git.exe` and `C:\Users\llong\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`.

Source review checked owner/round identity derivation, original-config validation on resume, durable start-before-execution records, no automatic restart of uncertain work, no replay of completed work, paid-request reconciliation ordering, elapsed time across resumes, terminal cleanup after expiry, and command literal quoting. This is source/CLI verification; it does not establish successful provider execution or a completed production campaign.

## Remaining practical integration dependencies

1. **No real final campaign manifest is available.** Controller confirmed this. Successful authenticated preparation and end-to-end production campaign execution could not be exercised. No synthetic authority was created to claim success.
2. **Baseline input construction remains upstream.** `capture-baseline` consumes an existing `BaselineCaptureInputsV5`, but no CLI currently constructs/persists that input bundle. Its exact references are evaluator contract, execution profile, sandbox profile, discovery panel plan, baseline policy revision, source bundle, policy scope, evaluator source, identity transitions, and resources. The next call boundary is `authenticate_baseline_inputs_v5(repository=..., inputs_ref=...)`, then the existing `capture_baseline_v5`/`capture-baseline`, verification, and `build-manifest`. Once that genuine graph exists, the new `prepare-production` path is ready to connect it.
3. **The available provisional legacy-data evaluation is not production campaign authority.** Controller independently found that `core/backtest_engine.py:2454` and V3 feature/industry helpers require schema-3 data; the legacy evaluation currently fails. Controller owns a separate development compatibility task and retained the production schema-3 gate. This task made no evaluator or data-contract changes.
4. **Host launch prerequisites still apply.** Supply real disjoint directories, the two manifest-bound data files, the clean source checkout, the installed pinned image/working Docker daemon, and the configured `OPENROUTER_API_KEY`. Preparation validates local paths, source and data identities; it intentionally does not launch Docker or contact the provider.

Controller authorized a local commit of these source/documentation files after review. No push or merge is part of this task.
