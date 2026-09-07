# Production wiring fix report — I1/P1, 2026-09-07

Implemented only the paid-request recovery ordering fix requested by `loop-wiring-review.md` and `loop-wiring-fix-brief.md`, on top of controller commit `9af42df`. No tests were read, created, modified, or run. No provider, Docker, network, push, or merge operation occurred.

## Changes

- `production_provider.py`: `LocalRoleAuthorizationLedgerV5.existing_paid_role_requests(round_index=...)` authenticates the existing paid ledger and resolves each selected persisted call/request into an `ExistingPersistedRoleRequestV5`. It does not create requests, reserve slots, or call a transport.
- `runtime.py`: `reconcile_existing_paid_roles_v5` skips already-journaled completions, reconciles missing paid completions through `LedgerBackedRoleInvokerV5.reconcile_once`, and persists/appends their existing authoritative packages. `run_feedback_round_v5` executes this step before constructing `_Runtime` and outside its abort-to-terminal handler. `PaidRoleRecoveryRequiredV5` preserves pending/unavailable accounting without a new round terminal or cleanup shortcut.
- `operations.py`: runs the same reconciliation step before each round's completed/terminal reuse branch, using the original campaign's concrete provider ledger. The existing persisted epoch times and deadline calculation are untouched.
- `cli.py`: preserves the recovery-required exception across the single-round service boundary and emits a structured `PIT_OPTIMIZER_V5_RECOVERY` diagnostic before the existing unavailable summary. Campaign dispatch already includes the actionable exception message in its diagnostic.
- Updated the main wiring report to correct the original recovery claim. This report is the only additional file.

## Source-review trace

For the reviewed interruption window, the investigator and round intent already exist; the paid author's completion is missing. The concrete ledger supplies that exact existing request. Its existing reconciler either returns the durable terminal, settles the saved response, settles an expired unreported claim, or reports the claim pending. No code in the reconciliation step calls `invoke_once` or `reserve_role_slot`.

When reconciliation succeeds for an open journal, the same request/attempt/terminal/artifact references are persisted through `persist_role_invocation` and appended through the existing journal fold. A retry sees the same completion and reuses it. The runtime then reloads that journal and reaches the original expired deadline. It can now record deadline failure and perform ordinary idempotent cleanup with the paid completion already represented in ledger-versus-journal accounting. It cannot begin new optimization work after expiry.

If the existing claim is pending or its local authority cannot be recovered, the recovery-required exception escapes before `_Runtime` exists. Neither `_failure_result` nor `_cleanup` is called by that path. A later resume can retry local reconciliation without a fresh request, replacement reservation, or reset allowance.

For an already-terminal affected journal, automatic append is prohibited by the existing `append_round_event` contract: only cleanup can follow a terminal outcome, and complete cleanup closes the journal. The fix settles any recoverable paid ledger work, then emits `journal_closed` before campaign terminal reuse or runtime terminal handling. The diagnostic explicitly calls for preserving the original request, ledger and journal for an explicit repair/migration. This state cannot be made fully verifiable by this fix without changing immutable history; it is now reported honestly instead of being silently skipped and failing opaque cardinality verification. No journal migration or new authority schema was added.

Existing provider-free/injected runtime paths are unchanged. The production factory binds the exact concrete ledger that supplies the new request-discovery capability. The controller's seven evaluator/engine/development files are untouched.

## Verification

Run from `C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.worktrees\pit-optimizer-v5-architecture`:

```powershell
python -B -m ruff format core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/production_provider.py core/pit_optimizer_v5/cli.py
python -B -m ruff check core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/production_provider.py core/pit_optimizer_v5/cli.py
python -B -m ruff format --check core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/production_provider.py core/pit_optimizer_v5/cli.py
python -B -m core.pit_optimizer_v5.cli resume --help
python -B -m core.pit_optimizer_v5.cli resume-campaign --help
git diff --check -- core/pit_optimizer_v5/operations.py core/pit_optimizer_v5/runtime.py core/pit_optimizer_v5/production_provider.py core/pit_optimizer_v5/cli.py
```

All passed. In-memory compilation also passed:

```powershell
@'
from pathlib import Path
for name in ('operations.py', 'runtime.py', 'production_provider.py', 'cli.py'):
    p = Path('core/pit_optimizer_v5') / name
    compile(p.read_text(), str(p), 'exec')
    print('compiled ' + str(p))
'@ | python -B -
```

Verification is limited to the actual source, imports/CLI routing, compilation, formatting/lint, and whitespace. No paid interrupted campaign was executed or fabricated. The real final campaign manifest/baseline authority remains a separate integration dependency. A local scoped commit is authorized; hooks are disabled for that commit to honor the no-tests constraint.
