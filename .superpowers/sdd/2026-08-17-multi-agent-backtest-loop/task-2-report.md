# Task 2 report: quarantine, deterministic gates, and patch policy

## Scope and files

- Modified `agent_loop.py`: source preflight/locking, tracked quarantine export, child environment,
  bounded processes, attested sandbox runner, deterministic gates, historical-bundle validation,
  hidden backtest worker, unified-diff policy, and transactional patch application/rollback.
- Modified `tests/test_agent_loop.py`: real temporary Git repositories and high-impact Task 2
  behavior tests. External sandbox execution is represented only at the process boundary.
- Added `Dockerfile.agent-loop`: digest-pinned Python 3.13.14 slim base, exact
  `requirements-lock.txt`, UID/GID 65532, and Python entrypoint.
- No provider calls, credential reads/prints, live execution imports, source-checkout execution, or
  changes outside Task 2 files were made.

## Named breaks covered

- `test_preflight_rejects_unsafe_source_state`: unsafe dirty/detached/protected state becomes a
  promotion baseline.
- `test_preflight_captures_head_and_uses_worktree_git_lock_path`: linked worktree locks resolve to
  the wrong Git path or exact HEAD is lost.
- `test_preflight_exclusive_lock_and_permanent_runtime_fail_closed`: concurrent loops or the
  permanent paper checkout become a controller.
- `test_quarantine_exports_only_exact_tracked_commit_and_private_git`: ignored credentials or
  artifacts leak into the private candidate.
- `test_child_environment_is_allowlisted_scrubbed_and_parent_is_unchanged`: provider, broker,
  cloud, proxy, or Git credentials reach a child, or parent environment is mutated.
- `test_unsafe_local_mode_can_never_apply_or_promote`: the explicit local escape hatch executes
  model-authored code or permits promotion.
- `test_patch_policy_rejects_windows_and_traversal_paths`: traversal, drive, UNC, backslash, ADS,
  reserved, trailing-dot, or quoted paths escape the approved scope.
- `test_patch_policy_rejects_structural_diff_features`: create/delete/rename/mode/binary/combined
  patch features reach Git.
- `test_patch_policy_checks_hunk_counts_declared_files_deny_precedence_and_live_imports`: malformed
  hunks, declaration mismatch, denylist override, or live reference reaches Git.
- `test_backtest_gate_makes_engine_files_read_only_and_requires_mode_100644`: a metrics proposal
  rewrites its oracle or targets a non-100644 entry.
- `test_patch_apply_rolls_back_exact_bytes_on_compile_failure`: failed compilation leaves candidate
  bytes mutated.
- `test_patch_application_accumulates_valid_iterations_without_rejecting_prior_changes`: a second
  valid iteration is rejected because the first valid patch remains.
- `test_worker_export_has_no_git_and_hostile_runner_cannot_change_candidate`: worker execution sees
  candidate Git metadata or changes the candidate manifest.
- `test_sandbox_command_and_inspection_contract_is_fail_closed`: absent or weakly configured
  container execution is trusted.
- `test_data_bundle_validates_hash_schema_exact_keys_and_copies_privately`: wrong hash, schema, or
  coverage introduces an unapproved pickle cache.
- `test_backtest_gate_copies_approved_bundle_and_fails_closed_on_missing_sentinel`: process exit 0
  without a trusted `SimulationResult` sentinel passes.
- `test_backtest_threshold_boundaries_are_deterministic`: an LLM or off-by-one condition decides
  total/annualized return, Sharpe, drawdown, or trade thresholds.
- `test_backtest_gate_hidden_worker_uses_exact_tickers_and_neutralizes_extra_symbols`: hidden
  settings or S&P expansion widens the approved ticker-plus-benchmark universe.
- `test_fixed_test_gate_accepts_only_tracked_tests_selectors`: a pytest selector injects an option
  or addresses an untracked/non-test path.

## TDD evidence

Initial required focused RED, before Task 2 production code:

```text
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "preflight or quarantine or child or gate or patch or rollback"
24 failed, 32 passed, 36 deselected, 1 warning in 0.48s
```

All 24 failures were expected missing Task 2 imports/APIs; the 32 existing focused tests remained
green. Subsequent targeted RED cases found real gaps:

```text
test_patch_application_accumulates_valid_iterations_without_rejecting_prior_changes
1 failed, 92 deselected, 1 warning in 0.95s
Failure: second patch saw the prior valid modification as out-of-scope.

test_backtest_gate_copies_approved_bundle_and_fails_closed_on_missing_sentinel
1 failed, 94 deselected, 1 warning in 0.18s
Failure: run_backtest_gate was not yet implemented.

exact-ticker mutation check (benchmark deliberately removed from hidden provider)
1 failed, 93 deselected, 1 warning in 0.15s
Failure: expected ["MSFT", "AAPL", "SPY"], got ["MSFT", "AAPL"].
```

Final required focused GREEN:

```text
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "preflight or quarantine or child or gate or patch or rollback"
60 passed, 36 deselected, 1 warning in 16.40s

python -m ruff check agent_loop.py tests/test_agent_loop.py
All checks passed!
```

Fresh complete Task 2 verification:

```text
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py
96 passed, 1 warning in 18.63s

python -m py_compile agent_loop.py tests/test_agent_loop.py
exit 0, no output

git diff --check
exit 0; Git emitted only the host core.autocrlf LF-to-CRLF advisory.
```

The pytest warning in all runs is the repository's existing `PytestConfigWarning: Unknown config
option: cache_dir`; it is not introduced by these changes.

## Sandbox and data evidence

The container contract test exercises the real controller-built command/inspection behavior:

- approved `repository@sha256:<64 hex>` resolves to a valid immutable image ID;
- `create` uses that inspected image ID plus `--pull never`, non-root `65532:65532`, overridden
  Python entrypoint, `--network none`, read-only root, `--cap-drop ALL`, no-new-privileges,
  PID/memory/CPU caps, with protected inputs read-only and narrow writable scratch binds;
- post-create inspection verifies the image ID, user, entrypoint, network, root mode, capability,
  security, resource, and mount fields before `start --attach`;
- child environment and argv are reconstructed from fixed allowlists; arbitrary Python commands
  and non-private historical paths are rejected;
- real-engine execution rejects an absent/non-regular executable before trusting output.

The data validator uses the repository's real `dataset_cache` contract without importing or
unpickling the payload:

```text
cache_key  TEXT PRIMARY KEY
cache_kind TEXT NOT NULL
created_at TEXT NOT NULL
payload    BLOB NOT NULL
```

It requires the SQLite header, exact SHA-256, no symlink/reparse point, no unexpected schema
objects, and both exact cache keys:

```text
price::<period>::<start>::<end>::<sorted requested tickers + benchmark>
closes::<period>::<start>::<end>::<sorted requested tickers + benchmark>
```

Validation opens SQLite read-only/immutable with `trusted_schema=OFF` and `query_only=ON`; only key,
kind, and payload length are selected. The opaque BLOB is never deserialized in the trusted
controller. The approved file is copied once with exclusive creation into a controller-owned
immutable snapshot, validated there, mounted read-only, and rehash-verified after use. The hidden
worker clears `settings.EXTRA_SYMBOLS`, points the engine at that snapshot, and replaces the engine's
S&P provider with exactly the requested symbols plus benchmark.

## Self-review

- Candidate code is never run from its private Git repository. Compile, pytest, and backtest paths
  receive fresh tracked-only no-`.git` exports.
- Patch application has no local compile fallback. Its runner must be explicitly supplied; the
  production helper binds compilation to the attested sandbox.
- Candidate snapshots include files, links/reparse points, directories, modes, hashes, and bytes.
  Worker exceptions and hostile writes both trigger manifest comparison and exact restoration.
- Diff validation completes path, structure, hunk, cap, deny/allow, mode, gate-read-only, and added
  live-reference checks before the first `git apply` call.
- Existing valid candidate changes may accumulate, while pre-existing untracked, structural, denied,
  or gate-read-only state fails closed. Apply/check/diff/compile failures restore the full snapshot.
- Git calls disable system/global config and prompts; candidate initialization disables templates.
- The source lock uses the linked-worktree-aware `git rev-parse --git-path agent-loop.lock` path.

## Limitations

- This host has neither Docker nor Podman (`NO_SANDBOX_ENGINE`). The production path was therefore
  not weakened or run locally: command creation and post-create attestation are covered with an
  injected purpose-built process boundary, while the real backend's absence is tested fail-closed.
- The worker image was not built on this host. `Dockerfile.agent-loop` pins the exact approved base
  digest and lock file, but a later environment with the approved engine must build/inspect it.
- No operator-approved real historical bundle was supplied. Tests use a structurally exact SQLite
  cache whose BLOBs remain deliberately opaque, plus a fake engine boundary to prove exact ticker
  and sentinel behavior.
- Task 3 still owns the public CLI/state-machine wiring. Task 2 exposes enforceable APIs and the
  hidden worker function/argv contract for that integration.

## Review fix round 1/5 — proposal-only quarantine

### Files

- `agent_loop.py`: candidate capability, non-mutating source fingerprint/recheck, sanitized Git,
  immutable data snapshot, six-mount sandbox contract, host-sealed completion envelopes, AST live
  import rejection, observational results, and exact static/hidden argv grammars.
- `tests/test_agent_loop.py`: deterministic regression coverage for all round findings and migrated
  legacy tests to candidate-only and observational APIs.
- `Dockerfile.agent-loop`: fixed numeric UID/GID; mutable build identity arguments removed.

### Named breaks covered

- `test_patch_policy_ast_rejects_multiline_core_live_import_alias`: multiline/aliased
  `ImportFrom` bypassed the line regex.
- `test_patch_application_allows_same_file_full_revert`: accumulated-scope equality rejected a
  complete later revert.
- `test_git_subprocess_environment_is_minimal_and_disables_extension_points`: Git inherited keys,
  credentials, proxy/config/worktree controls, hooks, fsmonitor, pager, or external diff.
- `test_quarantine_force_tracks_commit_files_even_when_ignore_rule_matches`: tracked-but-ignored
  oracle input disappeared from the candidate index.
- `test_unsafe_local_commit_export_ignores_mutable_worktree_bytes`: unsafe-local copied mutable
  checkout bytes instead of the captured commit.
- `test_data_bundle_is_streamed_once_to_immutable_controller_snapshot`: hash/use raced the operator
  database and whole-file hashing bypassed the size bound.
- `test_hidden_backtest_argv_grammar_rejects_reordering_duplicates_and_unknown_values`: an option
  allowlist admitted ambiguous hidden-worker commands.
- `test_candidate_apply_api_rejects_source_checkout_paths`: a generic `Path` apply surface could
  target source instead of a controller-issued candidate.
- `test_execution_mode_has_no_promotion_surface_and_gate_results_are_observational`: stale promotion
  authority and `passed` vocabulary treated spoofable evidence as authorization.
- `test_worker_dockerfile_uses_fixed_numeric_identity_without_build_args`: image identity could vary
  through build arguments.
- `test_worker_completion_envelope_is_host_sealed_chained_and_observational`: candidate output lacked
  a controller-only nonce/run/image/config/manifest/argv/time/result/stream/cleanup record and chain.
- `test_worker_mounts_candidate_gate_and_data_read_only_with_narrow_writable_dirs`: candidate,
  protected gate, and data shared a writable bind.
- `test_container_attestation_rejects_every_privilege_env_command_and_mount_weakening` (10 cases):
  inspection admitted Privileged, CapAdd, Devices, DeviceRequests, extra SecurityOpt/Env, wrong
  WorkingDir/Cmd, a writable protected mount, or a non-bind mount.
- `test_engine_path_is_resolved_once_and_every_call_uses_canonical_absolute_path`: engine lookup
  could change between inspection and execution.
- `test_malformed_create_output_still_cleans_deterministic_name_and_verifies_absence`: malformed
  create output stranded a named container.
- `test_cleanup_failure_is_fatal_and_never_returns_observation`: successful stdout escaped failed
  removal/absence confirmation.
- `test_source_completion_recheck_reports_external_change_without_restoring_it`: completion cleanup
  could overwrite a concurrent source edit.
- `test_data_bundle_rejects_sidecars_size_overflow_and_post_run_tampering`: WAL/SHM/journal,
  oversized input, or daemon-side mutation escaped the approved hash.

### Exact TDD and verification output

First focused RED, before the review fixes:

```text
10 failed, 96 deselected, 1 warning in 4.44s
```

The failures were the AST import, full revert, Git environment, tracked-ignore export,
captured-commit unsafe-local, streaming snapshot, hidden grammar, candidate-only apply, observational
API, and fixed Docker identity tests above.

Second focused RED, for the engine/envelope/data lifecycle:

```text
17 failed, 106 deselected, 1 warning in 21.71s
```

The failures were the envelope, six mounts, ten strict inspection mutations, canonical engine,
deterministic cleanup, cleanup failure, source recheck, and data lifecycle cases above.

Focused GREEN:

```text
26 passed, 97 deselected, 3 warnings in 37.64s
```

Complete GREEN after legacy API migration:

```text
123 passed, 8 warnings in 65.17s (0:01:05)
```

Final static verification:

```text
python -m ruff check agent_loop.py tests/test_agent_loop.py
All checks passed!

python -m compileall -q agent_loop.py tests/test_agent_loop.py
exit 0, no output

git diff --check
exit 0; only host core.autocrlf LF-to-CRLF advisories
```

Warnings are the repository's existing unknown `cache_dir` pytest option plus Python resource
warnings from short-lived test-created SQLite fixtures; no production connection remains open.

### Sandbox/data/schema evidence

Each run receives a deterministic controller name and six exact binds: `/workspace/src`,
`/workspace/gate`, and the empty data directory or dedicated approved SQLite file are read-only;
`/workspace/tmp`, `/workspace/home`, and `/workspace/output` are the only writable binds. The image
repository digest resolves to an immutable ID. Before and after start, inspection requires exact
user, Python entrypoint, working directory, argv, base-plus-controller environment, bind sources and
destinations/options, network none, read-only root, `CapDrop=[ALL]`, empty CapAdd/devices/device
requests, exact no-new-privileges, and PID/memory/CPU limits. Cleanup is by name on every post-create
path and success requires a subsequent inspect miss. Injected engines always report
`worker_confined=false` and `security_attestation=false`; the daemon remains TCB and all outcomes are
`gate_observation` only.

The controller creates one private snapshot with exclusive creation while streaming SHA-256 under
an 8 GiB hard limit, rejects database sidecars, validates and later mounts that same snapshot, uses
a percent-encoded `file:...?mode=ro&immutable=1` SQLite URI, and rehashes after the worker. Validation
still requires the exact `HistoricalDataCache` table/schema and exact price/closes cache keys for
requested tickers plus benchmark without selecting or unpickling payload bytes.

### Self-review and limitations

- Source is never reset, cleaned, restored, patched, or promoted. `SourceFingerprint` covers
  HEAD/branch, normalized index entries, tracked modes/content, and nonignored untracked names;
  recheck only reports mismatch. Unsafe-local exports `SourceState.head` before local observation.
- Only a live controller capability registered to a `Candidate` can reach `apply_candidate_patch`.
  Worker callbacks receive tracked-only no-`.git` exports; candidate rollback authority never
  extends to source.
- Git children receive a minimal allowlist and explicit hook/fsmonitor/diff/pager disabling. Exact
  tracked files are force-staged and paths/modes compared to the captured commit.
- Completion envelopes are created in host memory after verified cleanup, HMAC sealed with a
  controller-only ephemeral key, and chained to the previous envelope. They deliberately do not
  convert same-interpreter pytest/backtest semantics into authorization.
- Docker/Podman remains absent, so the production backend was only exercised fail-closed; a faithful
  injected Docker-shaped boundary verifies the command/inspect lifecycle but can never produce
  production-attested provenance.
- Controller-owned approved snapshots persist for the caller's run lifetime; Task 3 owns lifecycle
  disposal and inert diff/archive export wiring. No source-checkout apply or promotion API remains.
