# Multi-Agent Backtest Refinement Loop Design

**Date:** 2026-08-17
**Branch:** `codex/multi-agent-backtest-loop`
**Status:** Implemented and hardened; paid rollout remains canary-gated

## Objective

Add a standalone `agent_loop.py` that coordinates three OpenRouter models to diagnose and
repair failures in this repository's offline test or technical-only backtest path. The loop
must stop when its deterministic gate passes or after ten iterations. It must not import,
invoke, modify, or schedule the paper-trading execution surface.

The Python controller, not an LLM, is the state machine and security boundary. Models return
strict data. They never select commands, access tools, write files, stage changes, commit,
push, or decide whether a gate passed.

## Non-goals

- No integration with `scheduler.py`, Windows Task Scheduler, or tomorrow's paper session.
- No live or paper broker orders.
- No autonomous Git commit, push, merge, or pull request.
- No agent-authored shell commands.
- No fundamental-data optimization or FMP calls from the default gate.
- No promise that Python process controls alone are an adversarial OS sandbox.

## Isolation model

The source checkout is the control plane. A run creates a temporary quarantine repository
outside the source repository's ancestor tree so `python-dotenv` cannot discover the source
`.env`. The controller-owned candidate repository begins as a tracked-file export of the exact
source commit. It has its own Git metadata and no ignored files, artifacts, credentials, or local
database. Candidate code is never executed in this repository. Each compile, test, or backtest
uses a fresh disposable no-`.git` export of the candidate state.

Every candidate test or backtest runs in a disposable worker with:

- `shell=False` and a controller-built argument vector;
- `ALPACA_PAPER=false`;
- FMP request budget zero;
- broker, OpenRouter, FMP, email, notification, cloud, and Git credential variables removed;
- operating-system/container egress disabled;
- `PYTHONNOUSERSITE=1`, a dedicated temporary home, and bounded stdout/stderr;
- a read-only root filesystem where supported, a single writable worker mount, bounded resources,
  and a process-tree timeout/kill boundary.

The OpenRouter API key remains only in the controller process and is never sent to a local
child. The quarantine path is never nested below a directory containing `.env`.

Production mode fails closed unless an attested sandbox backend is available. V1 supports a
fixed Docker backend whose controller-built command includes `--network none`,
`--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, PID/memory/CPU limits, a fixed
non-root UID/GID, a private 64 MiB `/dev/shm`, and an overridden Python entrypoint. It bind-mounts
source, the sealed gate, and approved historical data read-only; separate disposable tmp, home, and
output directories are writable. The image reference must include an operator-approved `@sha256:` digest. The engine
verifies the real executable, daemon, resolved repository digest, and created container
configuration before trusting its result. The production CLI has no local-execution escape hatch;
every gate requires the attested sandbox. `--apply` can mutate only the controller-owned candidate,
never this source checkout or the permanent paper runtime.

## Branch and source preconditions

Before any model call, the controller must:

1. Resolve the repository root and exact `HEAD`.
2. Require a clean tracked and untracked working tree.
3. Reject detached HEAD, `main`, `master`, and branches without the `codex/` prefix.
4. Reject the permanent paper runtime path and any source tree containing a running loop
   lock.
5. Record the source commit and status as immutable candidate provenance.

The controller resolves the lock with `git rev-parse --git-path agent-loop.lock`, which works for
normal and linked worktrees, and acquires a nonblocking OS file lock. A second loop must fail
rather than run concurrently.

## Deterministic gates

### Test gate

The default gate runs a fixed offline command equivalent to:

```text
<python> -m pytest -p no:cacheprovider --no-cov -q -m "not integration"
```

The controller may append operator-supplied test paths only after verifying they are tracked
files below `tests/`. No model output can alter this command. Exit code zero is pass.

### Backtest gate

The optional backtest gate invokes a hidden worker mode in the protected `agent_loop.py` with
explicit tickers, start date, end date, and `--technical-only --no-csv`. The worker calls the
canonical `core.backtest_engine.run_cli()` and emits one sentinel-prefixed JSON result.

Backtest mode requires `--historical-data-bundle` and `--historical-data-sha256`. The bundle is
an operator-approved regular SQLite cache file copied into each disposable worker after its hash,
SQLite header, expected table schema, requested-symbol-plus-benchmark coverage, and date range are
validated.
The untrusted worker receives only a private copy. This explicit approval is necessary because
the engine's cache contains pickled payloads. Missing or mismatched data fails closed. The hidden
worker neutralizes `settings.EXTRA_SYMBOLS` and overrides the engine's S&P 500 RS universe provider
to exactly the requested tickers plus the explicitly declared benchmark.

Pass/fail is computed locally from operator-supplied thresholds:

- minimum total return;
- minimum annualized return;
- minimum Sharpe ratio;
- maximum drawdown magnitude;
- minimum closed trades.

The LLM never decides whether the backtest passed. The backtest is serialized and uses only
data already available in the quarantine environment. Missing cache/data is a gate failure,
not permission to expose provider credentials.

## Models and message ordering

Default models are exactly:

- Orchestrator: `qwen/qwen3-next-80b-a3b-instruct`
- Reasoner: `qwen/qwen3-next-80b-a3b-instruct`
- Coder: `deepseek/deepseek-chat`

The controller uses the official OpenAI Python SDK pinned to `openai==2.54.0` and configured
with `base_url="https://openrouter.ai/api/v1"`. The conservative 2.x pin avoids adopting the
same-day 3.x HTTP transport migration without a separate upgrade review.

The controller accepts the key from `OPENROUTER_API_KEY` or the repository's existing
`OPENROUTER` alias. If both are present and differ it fails closed. For linked worktrees it may
read only those two names from the controller repository's common-worktree `.env`; it never calls
general `load_dotenv`, never exports the value, and redacts both names.

Each role has an immutable message prefix:

1. role-specific system prompt;
2. static repository/security policy context;
3. dynamic, delimited failure evidence and selected source files.

The prefix never changes order, supporting provider prompt caching. Whole-response caching is
not enabled because iteration inputs change. Calls are non-streaming. OpenRouter session IDs
are stable per run and role.

All roles are instructed to return one JSON object and no prose and use
`response_format={"type": "json_object"}`. The Coder returns typed exact-line replacements, not
diff grammar. Provider routing requires requested parameters where supported. Every response is
independently JSON-parsed and schema-validated.

The Reasoner is limited to `max_tokens=4096`. Raw chain-of-thought is neither requested nor
persisted; the response contains only a concise diagnosis and action plan. A truncated,
errored, empty, malformed, or schema-invalid response is unusable.

## Agent schemas

Unknown keys, duplicate JSON keys, blank required strings, invalid paths, and over-limit lists
are rejected.

### Orchestrator route

```json
{
  "action": "reason|abort",
  "failure_summary": "concise summary",
  "relevant_files": ["tracked/relative.py"],
  "reasoning_focus": "specific question for the reasoner"
}
```

### Reasoning plan

```json
{
  "diagnosis": "what failed",
  "root_cause": "causal explanation",
  "invariants": ["behavior that must remain true"],
  "files_to_change": ["allowed/relative.py"],
  "steps": ["ordered implementation step"],
  "skip": false,
  "skip_reason": ""
}
```

### Coding proposal

```json
{
  "summary": "what the patch changes",
  "replacements": [
    {
      "path": "allowed/relative.py",
      "start_line": 123,
      "old_lines": ["exact original source line"],
      "new_lines": ["replacement source line"]
    }
  ]
}
```

`start_line` is always a one-based coordinate in the immutable original snapshot. The model never
adjusts later coordinates for earlier edits. Both line arrays are nonempty, so insertion/deletion
uses an unchanged adjacent anchor. Paths, replacement order, overlap, adjacency, line counts,
aggregate bytes, and exact old text are controller-validated.

## Context construction

Repository files and logs are untrusted data, not instructions. Dynamic sections use explicit
delimiters, normalized line endings, control-character removal, secret redaction, and byte
caps. The controller sends only tracked regular text files selected from its allowlist.

Default editable files are:

- `backtest.py`
- `backtest_pnl.py`
- `core/backtest_engine.py`
- `core/momentum_analysis.py`
- `core/pivot_detector.py`

For a metrics-based backtest gate, `core/backtest_engine.py`, `backtest.py`, and
`backtest_pnl.py` become read-only so a patch cannot make itself pass by changing accounting,
threshold evaluation, or result serialization. Those files are editable only in the test gate,
where the unchanged offline suite is the deterministic oracle.

Tests are readable when needed but are not editable, preventing an agent from making the gate
pass by weakening assertions. Offline tests may inject another tracked non-denied path at the
controller service boundary, but the production CLI exposes no editable-path override; the model
cannot expand its own scope.

All candidate edits are proposal-only. Shared strategy modules are transitively imported by paper
trading, and arbitrary candidate Python can forge its own in-process test/backtest evidence.
Therefore the loop has no source-application or automatic-promotion path, even for backtest-only
files. It may test candidate changes in quarantine and export a sanitized diff/inert archive for
human review. Applying that diff to a real repository is an explicitly manual, out-of-band action.

Permanently denied paths include `agent_loop.py`, `.env*`, `.git`, `.github`, dependency
manifests, task/scheduler files, `auto_trader.py`, `fill_monitor.py`,
`paper_trading_console.py`, `config/settings.py`, `core/order_execution.py`, and
`core/order_manager.py`.

## Patch validation and application

Provider output never supplies diff headers, hunk coordinates, prefixes, or structural directives.
The controller parses the typed replacement object, binds every old span to the visible sealed
snapshot and current candidate hash, applies replacements in memory using original coordinates,
and rejects rewritten Python that does not parse. It also rejects every newly introduced defaulted
parameter in a function, async function, method, nested function, or lambda so proposals cannot
hide dormant optional controls behind unchanged callers.

The controller then renders the only accepted canonical zero-context unified Git diff. Before
calling Git, the existing diff-policy boundary reparses the controller-owned bytes and rejects:

- absolute, drive, UNC, backslash, NUL, parent-traversal, ADS, reserved-device, trailing-space,
  trailing-dot, quoted, or case-colliding paths;
- paths outside the editable allowlist or inside the permanent denylist;
- new files, deletions, renames, copies, mode changes, symlinks, submodules, combined diffs,
  and binary patches;
- malformed headers/hunks or mismatched hunk counts;
- more than 4 files, 25 hunks, 400 changed lines, or 256 KiB;
- added imports/references to live execution modules or `alpaca.trading`.

Every target must already be a tracked `100644` regular file. The typed replacement paths must
exactly equal the rendered diff's file set.

The controller then runs `git apply --check --whitespace=error-all`, snapshots target bytes and
hashes, applies the patch, verifies that only allowed tracked files changed, runs
`git diff --check`, and compiles changed Python files. Any validation/postcondition failure
restores the exact pre-patch bytes and records a rejected proposal.

Valid patches remain in the controller-owned candidate repository across iterations. Before and
after every worker execution the controller verifies the candidate Git tree/index/refs/config and
full tracked/untracked manifest are unchanged. Worker results are copied back only as sanitized
logs/metrics; worker filesystem changes are discarded. The source branch is always unchanged.

## State machine

```text
PREPARE
  -> RUN_PRIMARY_GATE
     -> PASS: RUN_FINAL_QUALITY
        -> ALL OBSERVED PASS: FINISH_GATE_OBSERVED
        -> ANY OBSERVED FAILURE: CALL_ORCHESTRATOR
     -> FAIL: CALL_ORCHESTRATOR
  -> ABORT: FINISH_AGENT_ABORTED
  -> REASON: CALL_REASONER
  -> SKIP/INVALID: RECORD_SKIP -> NEXT_ITERATION
  -> CALL_CODER
  -> INVALID/UNSAFE/UNAPPLICABLE: RECORD_REJECTION -> NEXT_ITERATION
  -> DRY RUN: EXPORT_DIFF -> FINISH_PROPOSAL_EXPORTED
  -> APPLY_TO_CANDIDATE
  -> NEXT_ITERATION -> RUN_PRIMARY_GATE
  -> ITERATION/CALL/TOKEN/USD/WALL LIMIT: FINISH_LIMITS_EXHAUSTED
```

`MAX_ITERATIONS` is exactly 10. A CLI value may lower but never increase it. The controller also
enforces per-call timeouts, a maximum API-call count, a total token ceiling, a hard USD ceiling,
and a total wall-clock deadline. Before a model call it loads current OpenRouter model pricing,
requires numeric prompt/completion rates, and reserves a worst-case charge using the prompt's
UTF-8 byte count plus the full completion-token allowance. Reported authoritative cost reconciles
the reservation afterward; absent cost retains the reservation. A call that cannot fit within the
remaining USD budget is never sent.

SDK automatic retries are disabled. The ordinary iterative loop retries only connection/timeout,
408, 409, 429, and selected 5xx failures within its own bounded attempt/deadline budget and may
make one explicit JSON-repair request. The proposal-batch path makes exactly one paid completion
per role, requests same-call provider response healing, and stops or records a closed safe rejection
without retrying the chat. It never retries 400, 401, 402, 403, or 422. OpenRouter embedded errors,
non-`stop` finish reasons, model mismatches, incomplete accounting, and invalid schemas fail closed.

## Audit and redaction

Sanitized run artifacts are written atomically under the explicit
`--artifact-root/<UTC-run-id>/`:

- manifest and fixed policy/model/config values;
- hash-chained state events in JSONL;
- redacted/truncated gate output with full-stream hashes;
- validated route, plan, typed coder payload, controller-rendered diff, and proposal metadata;
- patch validation result and changed-file hashes;
- API finish reason, provider/model IDs, token/cache/reasoning usage, and cost when supplied;
- final observational result and exported candidate artifact hashes.

The key, raw environment, raw chain-of-thought, unredacted logs, and full API response are never
persisted. Redaction covers exact known secret values and credential-shaped tokens before any
model prompt, terminal output, or artifact write.

## Source immutability and manual handoff

`--apply` means apply to the controller-owned candidate repository only. `agent_loop.py` contains
no `--promote` option and no source-checkout apply function. The source `HEAD`, index, tracked bytes,
and untracked set are rechecked at the end and must equal preflight; a mismatch is reported as an
external concurrent change, never cleaned or overwritten.

The loop exports a canonical sanitized unified diff and optional inert source archive with hashes,
but never executes either artifact after export. A human may inspect and apply the diff manually in
a separate workflow. A worker gate pass is recorded as an observed deterministic result, not proof
of correctness, a security attestation, or merge approval.

## Verification strategy

`tests/test_agent_loop.py` uses temporary Git repositories and fake OpenRouter clients only.
High-value behavior tests cover:

- protected/dirty/detached source refusal and clean feature-branch acceptance;
- exact fixed subprocess argv, environment scrubbing, timeout, and output bounds;
- JSON duplicate/unknown-key/malformed/truncated response rejection;
- stable static prompt prefix and role-specific token/JSON settings;
- API retry classification and call/token/iteration limits;
- path traversal, Windows path edge cases, structural diff attacks, denylist precedence, and
  size caps;
- successful candidate apply, exact rollback on postcondition failure, candidate-manifest invariance after
  hostile worker writes, and accumulated iterations;
- deterministic test/backtest pass decisions;
- source checkout unchanged even when `--apply` mutates the candidate;
- no parser option or callable API that applies a candidate diff to the source;
- import safety proving no live execution modules load merely by importing `agent_loop`.

The full existing offline suite, Ruff, compileall, and `git diff --check` remain the ordinary
iterative loop's final gate. Every applied candidate in that mode must run those four runtime
quality checks before the controller records a terminal pass observation. Proposal-batch mode is
non-applying: exit `0` means only that its sealed primary backtest already passed, so no proposal
was needed. A metrics backtest passing its thresholds never authorizes source mutation. Worker
output and exit status remain untrusted observations. No unit test may contact OpenRouter, Alpaca,
FMP, or any other provider.

## Operational invocation

The documented safe progression is:

1. Install the pinned SDK in the feature environment and provision the documented sandbox image.
2. Set `OPENROUTER_API_KEY` or `OPENROUTER` in the controller shell/private `.env` only.
3. Run one explicitly authorized `--proposal-samples 1` backtest canary with
   `--max-iterations 1 --max-api-calls 3 --max-usd 0.50 --canary-max-usd 0.50`, omit `--apply`,
   and audit the complete chain, accounting, cleanup, source immutability, and proposal behavior.
4. Only after an actionable same-commit canary, separately authorize up to 50 inert attempts with
   `--max-api-calls 150 --canary-max-usd 0.50`, a $2 ceiling, two million tokens, and no `--apply`.
   The controller may consume fewer than 150 calls when a sample aborts or skips before the coder.
5. Review every exported diff manually; any backtest, source application, or promotion is a
   separate out-of-band workflow.

A fully initialized run prints a final machine-readable `AGENT_LOOP_SUMMARY`. Ordinary-loop
summaries can include audit and quarantine paths; proposal-batch summaries include the audit path
and always dispose the candidate. Parser/configuration failures exit `2`, while initialization
failures return `22` with a closed stderr stage; those early failures may have no summary or audit
artifact. Absence of both accepted key names is an immediate configuration error before any
billable call.

## Primary implementation references

- OpenRouter OpenAI-SDK quickstart:
  <https://openrouter.ai/docs/quickstart#using-the-openai-sdk>
- OpenRouter structured outputs:
  <https://openrouter.ai/docs/guides/features/structured-outputs>
- OpenRouter reasoning-token controls:
  <https://openrouter.ai/docs/guides/best-practices/reasoning-tokens>
- OpenRouter prompt caching:
  <https://openrouter.ai/docs/guides/best-practices/prompt-caching>
- OpenRouter error contract:
  <https://openrouter.ai/docs/api/reference/errors-and-debugging>
- Official OpenAI Python SDK release metadata for the conservative pin:
  <https://pypi.org/pypi/openai/2.54.0/json>
- Official Python 3.13.14 slim image and index digest used by the worker recipe:
  <https://hub.docker.com/layers/library/python/3.13.14-slim/images/sha256-9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6>
