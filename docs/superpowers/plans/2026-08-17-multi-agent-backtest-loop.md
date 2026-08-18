# Multi-Agent Backtest Refinement Loop Implementation Plan

> **For Codex:** Execute this plan with `superpowers:subagent-driven-development`. Each task is
> test-first: observe the named RED behavior before production changes, then make the smallest
> implementation that satisfies the design.

**Goal:** Build a safe, auditable OpenRouter-powered three-agent loop that diagnoses and patches
offline tests or technical-only backtests in quarantine, never touching the paper-trading runtime.

**Architecture:** `agent_loop.py` is the trusted state machine. It exports tracked files into a
credential-free controller-owned candidate repository, executes each gate in a fresh no-`.git`,
network-denied sandbox worker, obtains strict JSON from an orchestrator/reasoner/coder, validates
one unified diff, and applies it only to the candidate repository. Optional promotion uses a
clean-HEAD compare-and-swap and never stages or commits.

**Tech stack:** Python 3.11+, stdlib, Git CLI, pytest, official `openai==2.54.0` SDK against
OpenRouter Chat Completions.

**Design authority:**
`docs/superpowers/specs/2026-08-17-multi-agent-backtest-loop-design.md`

## Global constraints

- Work only in `.worktrees/multi-agent-backtest-loop` on
  `codex/multi-agent-backtest-loop`; never edit the root checkout or paper runtime.
- `MAX_ITERATIONS = 10`; a CLI override may lower but never raise it.
- The three default model slugs and Reasoner `max_tokens=4096` are exact design values.
- Use the official OpenAI SDK with OpenRouter base URL, explicit timeout, `max_retries=0`, and
  non-streaming Chat Completions.
- Orchestrator and Reasoner use `response_format={"type": "json_object"}`; Coder does too.
- Static prompt/context messages remain the first two messages in identical order for every call.
- Never persist or forward raw reasoning, secrets, environment values, or unsanitized output.
- No model controls commands, paths, scope, budgets, pass/fail, Git, or filesystem writes.
- Local child commands are fixed argv lists with `shell=False`; the OpenRouter key and all broker
  credentials are absent from the child environment.
- Production candidate execution requires an attested Docker/Podman-compatible `--network none`
  sandbox. Unsafe local execution is explicit, cannot promote, and cannot report production-safe
  success.
- Unsafe local execution is mutually exclusive with `--apply`; it may run only trusted baseline
  code and obtain a dry proposal.
- The candidate Git repository is controller-only. Every child sees a disposable no-`.git` export,
  and the controller verifies its complete manifest before and after each child.
- Default editable scope excludes tests and permanently denies every live/paper execution surface.
- Shared strategy modules that paper trading imports may be edited only in quarantine and exported
  as proposals; automatic promotion is limited to backtest-only files.
- Metrics-based backtest runs additionally make the backtest engine/CLI read-only so candidates
  cannot pass by falsifying accounting or serialized metrics.
- Only existing tracked `100644` files can change. V1 rejects creates, deletes, renames, copies,
  mode changes, binary patches, symlinks, and submodules.
- No staging, commit, push, merge, PR, scheduler integration, or provider call in tests.
- All file edits during implementation use `apply_patch`.

## Task 1: Protocols, prompts, SDK gateway, and dependency lock

**Files:**

- Create: `agent_loop.py`
- Create: `tests/test_agent_loop.py`
- Modify: `requirements.txt`
- Modify: `requirements-lock.txt`
- Modify: `pyproject.toml`

### Required behavior

1. Pin `openai==2.54.0` in both install manifests and add its exact resolved plain-package
   transitive dependencies to `requirements-lock.txt`; select no extras.
2. Importing `agent_loop` must not construct a client, read a key, load `.env`, import any project
   execution module, or make a network call.
3. Define exact model/base constants, immutable system/static-context prompt constants, and
   `MAX_ITERATIONS = 10`.
4. Add frozen validated protocol dataclasses for route, reasoning plan, coding proposal, usage, and
   agent completion. Parsing must reject duplicate keys, unknown keys, wrong types, blank required
   values, invalid actions, overlong lists/strings, and invalid relative paths.
5. Add an injectable `OpenRouterGateway`. Default construction lazily imports `OpenAI`, accepts
   `OPENROUTER_API_KEY` or `OPENROUTER` (different simultaneous values fail closed), uses
   `base_url="https://openrouter.ai/api/v1"`, explicit timeouts, `max_retries=0`, and optional
   identifying headers. For a linked worktree, read only those names from the common repository's
   `.env`; never call general `load_dotenv`.
6. Each request sends exactly three messages: immutable system, immutable static policy, then
   dynamic delimited input. It sets the exact model, JSON response format, non-streaming mode,
   stable per-run/role session ID, and role token cap; Reasoner is exactly 4096.
7. Accept only a single `finish_reason == "stop"` text response that parses and validates. Inspect
   embedded errors. Capture token/cache/reasoning/cost fields defensively without assuming a
   provider shape.
8. Implement one bounded malformed-output repair attempt and bounded retry classification:
   connection/timeout, 408, 409, 429, 500, 502, 503, 504, 524, 529 may retry; 400, 401, 402, 403,
   404, and 422 may not. SDK retries remain disabled.
9. Add a call/token/USD ledger. Load current numeric OpenRouter pricing before billable calls,
   reserve a UTF-8-byte prompt upper bound plus full completion allowance, and reconcile reported
   cost; missing cost retains the reservation.

### TDD sequence

Write focused tests first using complete fake SDK response objects and injected fake clients.
Observe failures for: lazy import safety, duplicate/unknown JSON keys, malformed and truncated
responses, exact message ordering, exact Reasoner cap, retry/no-retry status classes, and missing
usage charging. Then implement and run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "protocol or gateway or budget or import"
python -m ruff check agent_loop.py tests/test_agent_loop.py
```

Commit: `feat: add multi-agent protocols and OpenRouter gateway`

## Task 2: Quarantine, child execution, deterministic gates, and patch policy

**Files:**

- Modify: `agent_loop.py`
- Modify: `tests/test_agent_loop.py`
- Create: `Dockerfile.agent-loop`

### Required behavior

1. Add source preflight: clean tree including untracked files, non-detached `codex/*` branch,
   protected-branch rejection, exact HEAD capture, permanent-runtime rejection, and exclusive lock
   resolved with `git rev-parse --git-path agent-loop.lock` for linked worktrees.
2. Export the exact tracked commit into a temp controller candidate directory outside every source
   ancestor that has a `.env`; initialize a private Git repository there. The export contains no
   ignored/untracked source file. Never execute candidate code there.
3. Build a child environment from an allowlisted minimal base, not by mutating the parent mapping.
   Remove OpenRouter, Alpaca, FMP, mail, notification, cloud, proxy, and Git credential variables;
   set `ALPACA_PAPER=false`, FMP budget zero, dead proxies, `PYTHONNOUSERSITE=1`, and dedicated
   temporary home/cache paths.
4. Add a sandbox runner that creates a fresh no-`.git` worker export per child. Production backend
   uses a digest-pinned image, overridden Python entrypoint, fixed non-root UID/GID, no network,
   read-only root, all capabilities dropped, no-new-privileges, bounded PID/memory/CPU, one writable
   worker mount, repository-digest verification, and post-create inspection. Add bounded
   output/hash/timeouts. Local backend requires `--unsafe-local-execution`, is mutually exclusive
   with `--apply`, has explicit unsafe status, and can never promote.
5. Add `Dockerfile.agent-loop` as the reproducible worker-image recipe using
   `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6`,
   exact `requirements-lock.txt`, fixed non-root UID/GID, no key/config copy, and Python entrypoint.
   The runtime still accepts only an inspected immutable image ID/digest.
6. Add the fixed offline pytest gate and optional tracked `tests/` selectors.
7. Add the hidden technical-only backtest worker and deterministic threshold evaluation. Require a
   regular operator-approved historical SQLite data bundle plus exact SHA-256; validate header,
   schema, exact requested-symbol-plus-benchmark/date coverage, then copy it privately into each
   worker. Neutralize `settings.EXTRA_SYMBOLS` and override the S&P 500 RS universe provider to
   exactly requested tickers plus benchmark. Worker output is one sentinel JSON object derived
   from `SimulationResult`; malformed/missing sentinel fails closed.
8. Implement the complete unified-diff policy from the design: canonical path parser, Windows edge
   rejection, exact allow/deny precedence, structural feature rejection, hunk-count verification,
   declared-file equality, caps, mode check, live-import added-line scan, and gate-specific
   read-only backtest-engine paths for metrics runs.
9. Validate with `git apply --check --whitespace=error-all`; snapshot bytes/hashes; apply; verify
   only allowed tracked changes; run `git diff --check`. Compile in a disposable sandbox worker,
   then prove the candidate Git metadata/full manifest did not change. Restore exact bytes after
   any apply/postcondition failure.

### TDD sequence

Use real temporary Git repositories and real Git apply behavior. Mock only the external process
boundary when testing timeout/tree handling. Observe RED for:

- dirty/detached/protected source;
- export accidentally containing `.env` or ignored files;
- key/credential inheritance;
- arbitrary command injection;
- traversal, drive, UNC, ADS, reserved-name, quoted/case-collision paths;
- create/delete/rename/mode/binary/combined/malformed/oversized diffs;
- permanently denied target despite an explicit allow;
- target mode not `100644`;
- compile failure rollback preserving exact bytes;
- sandbox command hardening/inspection and fail-closed absence;
- candidate metadata/manifest unchanged after a hostile worker creates files;
- data-bundle hash/schema/coverage rejection and exact-ticker behavior;
- deterministic test and backtest pass/fail boundaries.

Then run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "preflight or quarantine or child or gate or patch or rollback"
python -m ruff check agent_loop.py tests/test_agent_loop.py
```

Commit: `feat: add quarantined gates and safe patch application`

## Task 3: State machine, audit trail, CLI, and compare-and-swap promotion

**Files:**

- Modify: `agent_loop.py`
- Modify: `tests/test_agent_loop.py`

### Required behavior

1. Implement the exact state machine in the design:
   gate -> route -> reason -> code -> validate/apply -> repeat; pass/abort/dry-run/exhausted are
   explicit terminal states.
2. Orchestrator receives sanitized failure evidence and selects only `reason` or `abort` plus
   relevant readable paths. The controller intersects all paths with its policy.
3. Reasoner receives failure evidence and current selected file text and returns a plan. Coder
   receives that validated plan plus the same current file state and returns one diff.
4. `--apply` controls quarantine mutation. Without it, record one validated proposal and exit
   without writing candidate or source files. Invalid API output/unsafe patches skip an iteration;
   no unchanged infinite loop can exceed the hard limits.
5. Enforce maximum iterations, API calls, total tokens, hard USD cost, per-child/API timeout, and
   total monotonic wall deadline at each transition. A call without a current usable price or
   enough reserved budget is not sent.
6. Write atomic sanitized artifacts to `.artifacts/agent_loop/<run-id>/`: manifest, hash-chained
   JSONL events, redacted gate logs, validated role payloads, diffs, validation reports, usage, and
   final summary. Never store raw API objects, raw reasoning, keys, or environment values.
7. `--promote` requires `--apply`, the attested sandbox, and a passing final gate. Recheck source
   HEAD/status against preflight, revalidate final diff, snapshot bytes/modes/index, compute
   expected patched hashes, `git apply --check`, apply, and verify only declared files changed.
   Never stage or commit. Pre-apply races leave source untouched; post-apply failures restore only
   targets still equal to expected patched hashes and preserve concurrent mismatches.
   Reject promotion unless every changed file is one of `backtest.py`, `backtest_pnl.py`, or
   `core/backtest_engine.py`; quarantine-tested shared strategy diffs remain proposal-only.
8. Before promotion, run the full offline pytest command, Ruff, compileall, and `git diff --check`
   against the final candidate through the sandbox/controller-safe boundary. A backtest threshold
   pass alone never permits promotion; any one quality-gate failure blocks it.
9. Add `build_parser()`, `main(argv: Sequence[str] | None = None) -> int`, and
   `raise SystemExit(main())`. Validate dates, tickers, thresholds, model names, budgets, and path
   overrides before any API call. Do not expose the key as a CLI option.
10. Print concise human progress plus one final `AGENT_LOOP_SUMMARY=<json>` line containing no
   secrets and the audit/quarantine paths.

### TDD sequence

Drive the real controller with fake gateway responses, real temp Git repos, and small fixed Python
gate scripts. Observe RED for:

- gate pass makes zero model calls;
- exact orchestrator -> reasoner -> coder order after failure;
- abort, malformed skip, unsafe patch rejection, dry proposal, two-iteration repair, and hard stop;
- call/token/wall limits at exact boundaries;
- redaction and event hash-chain verification;
- source unchanged without promotion;
- promotion success leaving unstaged allowed files only;
- changed HEAD, dirty source, or pre-apply race refusing promotion with exact source bytes intact;
- post-apply failure conditionally rolling back without overwriting a concurrent edit;
- each of pytest, Ruff, compileall, and diff-check independently blocking runtime promotion;
- a passing shared-strategy patch remaining proposal-only because paper automation imports it;
- parser help causing no side effects and absent key failing before a billable call.

Then run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py
python -m ruff check agent_loop.py tests/test_agent_loop.py
python -m compileall -q agent_loop.py
```

Commit: `feat: complete audited multi-agent refinement loop`

## Task 4: Operator documentation and full verification

**Files:**

- Modify: `README.md`
- Modify: `.env.example`
- Modify: `tests/test_agent_loop.py` only if a genuine uncovered operator contract is found

### Required behavior

1. Document architecture, exact models, both accepted key names, install command, sandbox image,
   safe dry proposal command,
   quarantine apply command, optional promotion command, test/backtest gate examples, audit paths,
   budgets, and exit statuses.
2. State prominently that the loop is separate from paper trading, does not run in the scheduler,
   strips broker/provider credentials from children, and cannot claim OS confinement without an
   external network/filesystem sandbox.
3. Add only placeholders/comments for `OPENROUTER_API_KEY` and the supported `OPENROUTER` alias
   to `.env.example`; never add a real value.
4. Confirm the paper runtime/root worktrees have not changed.
5. Run final offline verification with dead proxies and FMP budget zero:

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
$env:HTTP_PROXY='http://127.0.0.1:9'
$env:HTTPS_PROXY='http://127.0.0.1:9'
python -m pytest -p no:cacheprovider --no-cov -q
python -m ruff check .
python -m compileall -q .
git diff --check
```

6. Run one CLI help/configuration smoke with no key and prove it performs no network/provider call.
   If a key is available in the controller environment, run at most one explicitly authorized
   dry-proposal smoke; otherwise record that live provider integration remains unexecuted.

Commit: `docs: document isolated multi-agent backtest loop`

## Final review and completion audit

Dispatch a whole-branch reviewer with the design spec, plan, implementation reports, complete
merge-base diff, and these exact review questions:

- Does every supplied mission requirement have direct code/test evidence?
- Can any model-controlled value become a command, scope expansion, unvalidated path, or source
  write?
- Can a child inherit or discover the source `.env` through normal dotenv search?
- Can any path touch live/paper trading code or runtime state?
- Does malformed/truncated JSON, API failure, timeout, budget exhaustion, patch failure, or Git
  race fail closed?
- Are the static prompt prefixes stable and the Reasoner hard-limited to 4096 tokens?
- Are audit artifacts useful without retaining secrets or raw reasoning?

Resolve all Critical/Important findings through the subagent fix loop, rerun the affected tests,
then rerun the full final gate. Do not merge to `main`; leave the reviewed commits on
`codex/multi-agent-backtest-loop` for the user's decision.
