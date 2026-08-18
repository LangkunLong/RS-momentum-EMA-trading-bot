# Multi-Agent Backtest Refinement Loop Design

**Date:** 2026-08-17
**Branch:** `codex/multi-agent-backtest-loop`
**Status:** Approved for implementation from the user's supplied mission

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
fixed Docker/Podman-compatible backend whose controller-built command includes `--network none`,
`--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, PID/memory/CPU limits, a fixed
non-root UID/GID, an overridden Python entrypoint, and only the disposable worker directory mounted
writable. The image reference must include an operator-approved `@sha256:` digest. The engine
verifies the real executable, daemon, resolved repository digest, and created container
configuration before trusting its result. An explicit `--unsafe-local-execution` development
escape hatch may run only the trusted baseline and produce a dry proposal. It is mutually exclusive
with `--apply` and `--promote`, so model-authored code is never executed locally. Applying model
patches requires both `--apply` and the attested sandbox.

## Branch and source preconditions

Before any model call, the controller must:

1. Resolve the repository root and exact `HEAD`.
2. Require a clean tracked and untracked working tree.
3. Reject detached HEAD, `main`, `master`, and branches without the `codex/` prefix.
4. Reject the permanent paper runtime path and any source tree containing a running loop
   lock.
5. Record the source commit and status for later compare-and-swap promotion.

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

- Orchestrator: `qwen/qwen-2.5-7b-instruct`
- Reasoner: `deepseek/deepseek-r1`
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

All roles are instructed to return one JSON object and no prose. The Orchestrator and Reasoner
must use `response_format={"type": "json_object"}`. The Coder uses the same mode so the unified
diff is carried as a JSON string. Provider routing requires requested parameters where
supported. Every response is independently JSON-parsed and schema-validated.

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
  "files": ["allowed/relative.py"],
  "unified_diff": "diff --git a/... b/...\n..."
}
```

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
- Python modules directly below `core/canslim/`

For a metrics-based backtest gate, `core/backtest_engine.py`, `backtest.py`, and
`backtest_pnl.py` become read-only so a patch cannot make itself pass by changing accounting,
threshold evaluation, or result serialization. Those files are editable only in the test gate,
where the unchanged offline suite is the deterministic oracle.

Tests are readable when needed but are not editable, preventing an agent from making the gate
pass by weakening assertions. The operator may add another tracked non-denied path with an
explicit CLI option; the model cannot expand its own scope.

Candidate-editable and automatically promotable scopes are intentionally different. Shared
strategy modules (`core/canslim/*`, `core/momentum_analysis.py`, and `core/pivot_detector.py`) are
transitively imported by the paper scanner, so any candidate diff containing them is proposal-only:
it may be tested in quarantine and exported for human review, but `--promote` must reject it.
Automatic promotion is limited to the backtest-only compatibility/engine files (`backtest.py`,
`backtest_pnl.py`, and `core/backtest_engine.py`) after all other gates pass. A trusted future
change may create a backtest-only strategy boundary; static import scanning cannot safely turn
shared production strategy code into an automatically promotable surface.

Permanently denied paths include `agent_loop.py`, `.env*`, `.git`, `.github`, dependency
manifests, task/scheduler files, `auto_trader.py`, `fill_monitor.py`,
`paper_trading_console.py`, `config/settings.py`, `core/order_execution.py`, and
`core/order_manager.py`.

## Patch validation and application

Only a single conventional unified Git diff is accepted. Before calling Git, the controller
parses the entire patch and rejects:

- absolute, drive, UNC, backslash, NUL, parent-traversal, ADS, reserved-device, trailing-space,
  trailing-dot, quoted, or case-colliding paths;
- paths outside the editable allowlist or inside the permanent denylist;
- new files, deletions, renames, copies, mode changes, symlinks, submodules, combined diffs,
  and binary patches;
- malformed headers/hunks or mismatched hunk counts;
- more than 4 files, 25 hunks, 400 changed lines, or 256 KiB;
- added imports/references to live execution modules or `alpaca.trading`.

Every target must already be a tracked `100644` regular file. The proposal's declared file
list must exactly equal the diff's file set.

The controller then runs `git apply --check --whitespace=error-all`, snapshots target bytes and
hashes, applies the patch, verifies that only allowed tracked files changed, runs
`git diff --check`, and compiles changed Python files. Any validation/postcondition failure
restores the exact pre-patch bytes and records a rejected proposal.

Valid patches remain in the controller-owned candidate repository across iterations. Before and
after every worker execution the controller verifies the candidate Git tree/index/refs/config and
full tracked/untracked manifest are unchanged. Worker results are copied back only as sanitized
logs/metrics; worker filesystem changes are discarded. The source branch is unchanged until
optional promotion.

## State machine

```text
PREPARE
  -> RUN_GATE
  -> PASS: FINISH_SUCCESS
  -> FAIL: CALL_ORCHESTRATOR
  -> ABORT: FINISH_ABORTED
  -> REASON: CALL_REASONER
  -> SKIP/INVALID: RECORD_SKIP -> NEXT_ITERATION
  -> CALL_CODER
  -> INVALID/UNSAFE: RECORD_REJECTION -> NEXT_ITERATION
  -> DRY RUN: RECORD_PROPOSAL -> FINISH_DRY_RUN
  -> APPLY_PATCH
  -> NEXT_ITERATION -> RUN_GATE
  -> ITERATION/CALL/TOKEN/WALL LIMIT: FINISH_EXHAUSTED
```

`MAX_ITERATIONS` is exactly 10. A CLI value may lower but never increase it. The controller also
enforces per-call timeouts, a maximum API-call count, a total token ceiling, a hard USD ceiling,
and a total wall-clock deadline. Before a model call it loads current OpenRouter model pricing,
requires numeric prompt/completion rates, and reserves a worst-case charge using the prompt's
UTF-8 byte count plus the full completion-token allowance. Reported authoritative cost reconciles
the reservation afterward; absent cost retains the reservation. A call that cannot fit within the
remaining USD budget is never sent.

SDK automatic retries are disabled. The controller retries only connection/timeout, 408, 409,
429, and selected 5xx failures within its own bounded attempt/deadline budget. It never retries
400, 401, 402, 403, or 422. A malformed/truncated response receives at most one repair attempt;
if still invalid, the iteration is skipped. OpenRouter embedded errors and non-`stop`
`finish_reason` values are treated as failure.

## Audit and redaction

Sanitized run artifacts are written atomically under
`.artifacts/agent_loop/<UTC-run-id>/`:

- manifest and fixed policy/model/config values;
- hash-chained state events in JSONL;
- redacted/truncated gate output with full-stream hashes;
- validated route, plan, proposal summary, and diff;
- patch validation result and changed-file hashes;
- API finish reason, provider/model IDs, token/cache/reasoning usage, and cost when supplied;
- final result and optional promotion result.

The key, raw environment, raw chain-of-thought, unredacted logs, and full API response are never
persisted. Redaction covers exact known secret values and credential-shaped tokens before any
model prompt, terminal output, or artifact write.

## Promotion

Promotion is off by default. `--promote` requires `--apply`, an attested sandbox (never the unsafe
local backend), and a passing gate.
It also requires every changed file to be on the separate backtest-only promotion allowlist;
shared paper-reachable strategy changes remain proposal-only regardless of test results.
The controller recomputes source status and `HEAD`; both must exactly match the recorded clean
base. It validates the final quarantine diff again against the same policy, snapshots exact source
bytes/modes/index state and expected patched hashes, runs `git apply --check` in the source
checkout, applies it, and verifies only the declared files changed. It does not stage or commit.
On post-apply failure it restores a target only when its current hash still equals the expected
patched hash; a concurrent mismatch is preserved and reported for manual recovery rather than
overwritten.

Any pre-apply compare-and-swap failure leaves the source untouched. Any post-apply verification
failure performs the conditional exact rollback above and preserves a recovery manifest plus the
candidate diff for manual review.

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
- successful apply, exact rollback on postcondition failure, candidate-manifest invariance after
  hostile worker writes, and accumulated iterations;
- deterministic test/backtest pass decisions;
- source checkout unchanged by default;
- compare-and-swap promotion refusal after concurrent source change;
- import safety proving no live execution modules load merely by importing `agent_loop`.

The full existing offline suite, Ruff, compileall, and `git diff --check` remain the final gate.
Every future candidate must pass those same four runtime quality checks before promotion. A metrics
backtest passing its thresholds is necessary but not sufficient; any one quality-gate failure
blocks promotion. No unit test may contact OpenRouter, Alpaca, FMP, or any other provider.

## Operational invocation

The documented safe progression is:

1. Install the pinned SDK in the feature environment and provision the documented sandbox image.
2. Set `OPENROUTER_API_KEY` or `OPENROUTER` in the controller shell/private `.env` only.
3. Run without `--apply` to obtain and audit one proposal.
4. Run with `--apply` to refine inside quarantine.
5. Add `--promote` only after reviewing the audit artifacts and using the attested sandbox.

The command must always print the quarantine/audit path and a final machine-readable summary.
Absence of both accepted key names is an immediate configuration error before any billable call.

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
  <https://hub.docker.com/layers/library/python/3.13.14-slim/images/sha256-69e18bd8d831d88e0ef70239dc7771ab7c28bc296ae78ac75cde71e60aa4434f>
