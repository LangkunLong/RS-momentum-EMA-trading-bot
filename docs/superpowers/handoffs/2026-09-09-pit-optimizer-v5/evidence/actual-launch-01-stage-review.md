# Actual launch 01 — independent stopping-stage and spend review

The actual failure occurred during initial native adapter-graph validation, before campaign enrollment, launch publication, feedback-round execution, or any paid role reservation. Source ordering and the exact current durable-file difference support **zero provider calls, zero role tokens/provider spend, and zero evaluations from this failed launch**. This conclusion does not rely solely on directory names.

## Actual result and durable evidence

`actual-launch-01-result.json` retains actual chunk `51b914`, process exit 1 after 2.568 seconds. Its diagnostic ends in `development adapter graph differs from its authority`. The awake helper reports active then restored. No live process/session remains according to the principal's actual execution result. The credential helper had already run before CLI dispatch, so the configured credential handle was resolved for this actual invocation; that is distinct from a provider call. This reviewer did not read any credential/environment values.

The current root `.artifacts/pit-optimizer-v5/development/provider-admission-20260908-01` contains 32 files. All 31 reviewed preparation files, including report `7b3695cb8206135c02ffbeacffac64f94b8db26e0cd952e825e00bb31c585fce`, independently match their prelaunch hashes. The sole added file is:

`adapter-state-locks/campaign-launch/d6ecfcf650ba705015f11220a5152ff24bbec56faf33bab8399d4a5fad0ead72.lock`

It is one byte, SHA-256 `6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d` (the native single-NUL-byte lock marker). There is no new admission binding/enrollment, campaign-launch state, round-start/deadline, role request/output, paid authorization reservation/terminal ledger, record, critic, checkpoint, or mutable archive state. The immutable prepared admission policy remains unchanged and is not an enrollment record.

## Source stopping proof

1. Operations CLI authenticates the manifest and dispatches `run_development_campaign_v5` to `_run_campaign_v5`.
2. `core/pit_optimizer_v5/operations.py:500` enters the `campaign-launch` adapter-state transition. `artifacts.py:2069-2143` creates the exact one-byte lock marker, acquires its lock, then unlocks/closes it on exit. The marker's continued presence is expected and is not a campaign-launch record.
3. `operations.py:503` calls the initial `DevelopmentRoundFactoryV5.compose_round`. That method builds the native composition, then calls `result.validate_for` at `development_preparation.py:328` before returning.
4. `DevelopmentRoundCompositionV5.validate_for` raises the exact observed diagnostic at `development_preparation.py:251` when the initial development graph/input/persistence predicate fails. The paid adapter-graph structural check precedes this predicate. The diagnostic alone does not identify which member of the compound predicate differs; that remains a separate identity diagnosis.
5. This exception prevents return to the later `first.validate_for` at `operations.py:510` and the admission/launch branch beginning at line 517. Explicit-policy enrollment publication at lines 615-619 and launch publication through line 646 are unreachable. The feedback runtime call is much later at line 911. No original campaign launch time/deadline was durably established.
6. The dispatch error handler formats this failure and unwinds through the existing credential and awake helpers. It does not invoke runtime or targeted cleanup in this path.

## Why composition does not imply spend

`cli.py:538-566` constructs a ledger, gateway, authorized role runner, and invoker. Gateway initialization at `production_provider.py:553-573` records references and identity digests; transport invocation is a separate `invoke_json_once` method. Ledger initialization at lines 648-686 verifies existing records; reservation is a separate `reserve_role_slot` method at line 795. `artifacts.py:2275-2352` returns empty groups for absent authorization directories and raises for legacy migration requirements rather than performing migration.

The native `_compose_round_from_paths_v5` constructs owner/root/driver/materializer/mount/executor/request/reducer/cleanup objects and identity references. It does not call role execution, reservation, candidate materialization, Docker evaluation, or cleanup. Those operational methods are reached only in the later feedback lifecycle, which this failure never entered. The actual empty authorization/runtime state and unchanged preparation hashes corroborate that control-flow result.

## Narrow diagnosis boundary

Source inspection identifies no mutation or paid-call blocker to a genuine **composition-only** comparison using the exact saved manifest/config/owner and current actual roots. `compose_development_round_v5` itself constructs the real graph without the factory's final validation step. It may read ordinary resource metadata and internal native environment controls and create in-memory nonces; diagnostics must publish only component digests/booleans. The existing bytecode-suppressed interpreter is required. Do not invoke the factory's validation path to execute, or call runtime/start/reserve/materialize/evaluate/cleanup methods. This diagnostic allowance is not permission to bypass native validation for a launch or to refresh authority.

No restart, cleanup, operational import/execution, provider/evaluator call, test, synthetic probe, platform audit, or production edit was performed by this reviewer. No launch retry is authorized by this report. The separately retained prepared-package review remains historical evidence of the reviewed bytes, not evidence that this actual launch succeeded.
