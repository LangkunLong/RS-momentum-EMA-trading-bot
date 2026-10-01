# Lead B evaluator image closure: independent review

Review date: 2026-10-01. Exact reviewed range:
`ab385d792e19ff6db39d87f1123f47f660fc1e1d..a8662e85c4c06142183c707926e7c302c75e233d`.
HEAD matched the latter commit during review. The complete range changes only
`core/pit_optimizer_v5/contracts.py`,
`Dockerfile.pit-optimizer-v5.dockerignore`, and
`tests/test_pit_optimizer_v5_membership_admission.py` (22 additions, one deletion).

## Verdicts

- **Specification: PASS for the source correction; changed-image acceptance remains pending.**
  The accepted scheduler-observation integration introduced an eager dependency
  absent from the former 57-path image. The correction admits and authenticates
  that dependency, producing a 58-path closure. Actual changed-image build,
  isolated import, and matched probe evidence must still be retained and reviewed.
- **Code quality: PASS.** No actionable defect found in the complete reviewed
  diff or the relevant import/authentication boundaries. The change is narrow,
  and the regression meaningfully checks altered and missing dependency bytes.

These verdicts do not accept unobserved Docker outcomes. This reviewer executed
no Docker commands, provider/model/broker calls, production evaluation, or old
#82 rerun, and made no product changes.

## Closure and authentication findings

`core.alpaca_client_policy`, `core.data_client`, and
`core.index_ticker_fetcher` eagerly import `core.scheduler_observation`.
The evaluator entry imports the backtest engine; the resulting dependency
chain reaches these mapped modules even when no live provider is called.
`scheduler_observation.py` imports only standard-library modules (`re`,
`threading`, `contextlib`, `datetime`, and `typing`), so adding it does not
introduce another repository dependency.

The new map and Docker allowlist agree. Docker's existing `COPY core/` installs
the newly admitted file under the interpreter-owned site-packages directory.
The existing image-manifest build step recomputes the installed map before
applying the source label, and the panel/probe entry points reauthenticate that
map. Changing observation bytes changes the map; removing the file fails closed.
The added regression exercises both failures. It is a filesystem authentication
test, despite its `test_installed_image_...` name, and is not Docker evidence.

An AST scan of all 58 files at the exact new Git revision found these remaining
local imports outside the map, whose execution boundaries were examined:

- `core.fmp_provider` is imported inside three optional provider helper functions
  in `data_client.py`; these are outside the offline evaluator entry path.
- Legacy CLI/control imports in `pit_optimizer_evaluation.py` remain lazy. Its
  `PanelAggregateSummary.__post_init__` also lazily imports
  `core.pit_optimization`; the V5 container uses `summarize_panel_result` and its
  V5 diagnostics rather than constructing this legacy aggregate.
- `core.strategy_policy.v3.__init__` refers to the four intentionally excluded
  editable modules. The container candidate path authenticates their separate
  source bundle and uses worker overlays. The baseline in-process loader and
  worker dispatch without an overlay are separate paths and must not be
  mistaken for the candidate image's installed immutable closure.

The independent raw-Git map at the new source is:
`ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`.
The map was calculated from `git show <revision>:<path>` bytes and independently
reproduced by the product verifier against a fresh allowlist-only copy.

## Independently performed checks

1. Read the complete `git diff` for the exact range, the Dockerfile and ignore
   recipe, source-map and installed-verifier implementations, evaluator and
   probe entry points, worker overlay boundaries, and relevant transitive imports.
   `git diff --check <base> <head>` passed.
2. `python -m pytest -q tests/test_pit_optimizer_v5_membership_admission.py`
   passed: **8 tests**, 45.60 seconds. The repository's default coverage option
   was active. Warnings were the existing `websockets.legacy` deprecation and
   inability to write the pytest cache; neither changed the test outcomes.
3. `python -m ruff check core/pit_optimizer_v5/contracts.py
   tests/test_pit_optimizer_v5_membership_admission.py` passed.
4. A bounded Python AST audit read all 58 mapped files directly from the pinned
   Git revision and enumerated local imports outside that map, including lazy
   imports and the intentional editable-module exclusions described above.
5. An independent host import check copied exactly the 58 raw Git files into
   `.artifacts/evidence/lead-b-independent-import-a8662e8/`. A fresh
   `python -I -B -c <harness> <copy-root> <host-user-site-packages>` process used
   that directory as its working directory, inserted only that source root and
   the explicit host dependency directory, and blocked socket connection APIs.
   It successfully imported `core.alpaca_client_policy`, `core.data_client`,
   `core.index_ticker_fetcher`, the image manifest, container entry, probe entry,
   and backtest engine. The verifier reproduced the raw-Git map; 57 local modules
   were loaded. Optional FMP, legacy optimization, and V3 candidate modules were
   absent from `sys.modules`.
6. Removing only `core/scheduler_observation.py` from that isolated copy caused
   the fresh process to fail with `ModuleNotFoundError` at
   `alpaca_client_policy.py:11`. The copy was restored afterward. This confirms
   the omission and correction without importing the surrounding repository.

The final two host-process results are retained at
`.artifacts/evidence/lead-b-independent-import-a8662e8-results.json`.
This host check uses installed Windows dependencies, not the locked Linux image;
it is corroborating closure evidence, not image acceptance. An initial temporary
directory attempt encountered a filesystem permission error, and the first
isolated process lacked `dotenv` because `-I` excluded user site packages.
The final check resolved those harness issues using the workspace copy and an
explicit dependency directory; no dependencies were installed.

## Evidence still required for changed-image acceptance

- A retained canonical build-context manifest that ties all 58 mapped files and
  the three recipe/lock files to raw Git bytes at the new source, plus the
  successful build log, installed-map verification, image inspect, source label,
  and unambiguous runnable image identity.
- Actual isolated import evidence for that image, including the observation
  dependency and evaluator/probe entry points, with no repository bind mount
  capable of supplying an omitted source file. Retain container exit status,
  runtime restrictions, and the created container's image identity.
- Actual parent and comment-only candidate probe receipts at this same new map
  and image, with separate policy/request identities, all fixed observations,
  matching semantic fingerprints, and complete owned-container/workspace cleanup.

The accepted dated #82 evidence remains source-specific and unchanged:
source `8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`, map
`f6dee0745545308088887a924b9839efedd8f1beff1dfbfac82fd889935819f0`, image
`sha256:1e0e6327f48e96d4f9754c6bae47969f0d8cf57f3c2b6cb8a10ab2ece28af0df`.
The existing dated build/import and final-source candidate receipts were read
for their boundaries, not rerun or replaced. Their outcomes cannot establish
the new source/image's acceptance.

## Retained changed-image evidence review — 2026-10-01

**Integration-evidence verdict: PASS for the bounded changed-source image build,
installed authentication, isolated imports, and matched synthetic fixed probes.**
The evidence requirements listed above were subsequently supplied and independently
reviewed. This supersedes the earlier pending-image status for source
`a8662e85c4c06142183c707926e7c302c75e233d` only. Source-specification and
code-quality verdicts remain PASS. No new actionable defect was found.

Reviewed [changed-image receipt](lead-b-evaluator-a866-receipt.md),
[public context manifest](lead-b-evaluator-a866-context-manifest.json), retained
files under `.artifacts/lead-b-image/`, and final probe evidence at
`C:/Users/llong/.codex/visualizations/2026/10/01/01a0f851-c7ac-7b81-87ab-3de389d47510/lead-b-image/probes-a8662e8-run3/evidence.json`.
This was a read-only evidence audit, with no Docker or probe execution.

- All **61 context files** independently matched both raw Git bytes at the pinned
  revision and retained context-file sizes/SHA-256s: 58 runtime files plus the
  Dockerfile, Docker ignore file, and requirements lock. The public and local
  manifests parsed identically. The runtime map independently recomputed to
  `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`.
- All six named local-artifact hashes in the receipt were recomputed successfully,
  including the executed helper, both build logs, smoke JSON and final probe log.
  The final probe evidence hash also matched:
  `95943aed5131c8b47a53c784dc07f5a44361b566be293ae7150a2658f2f325f7`.
  The helper hash is
  `20aa9f06f89a0858f6715eccb10107aeb60ad9cc186904608efe8ee7ff8dac8a`.
- Both build logs show successful installed-map verification. The first result
  remained in the docker-container builder cache. The later load log identifies
  the desktop-linux Docker driver, executes the same pinned recipe/context, and
  exports/unpacks backend/OCI-index identity
  `sha256:e2e756560b528c6d57c6630e21c615f50bb4026d3f6412c66135ef015d530349`.
  Its config digest is separately recorded as
  `sha256:a15d7824ae02ee9548235d0f60cfcaed65b960b1f2783c1ee129be243272d552`.
  The evidence supports a successful local load; it does not require or claim
  that the second build reused the first builder's cache.
- The smoke JSON contains actual image inspect and before/after container inspect
  output. The image label and created container `.Image` match the new map/image;
  `Mounts` is empty and `HostConfig.NetworkMode` is `none`. The attached process
  output reports successful imports, installed-map verification, and observation
  origin under `/usr/local/lib/python3.13/site-packages/core/`. Start and final
  container exit are zero; remove and targeted absence-query results succeed.
- Both final probe records exactly match their streamed post-cleanup log records.
  Separate 15-entry control traces show successful image inspect, create, start,
  wait and final inspect, with no timeouts. The initial missing named-container
  checks are expected pre-creation ownership checks, not failed executions.
  Literal created-container image checks match the new backend identity in both
  cases. Retained collected-state records report exited lifecycle and empty
  network namespace; cleanup/absence state records agree with the final report.
- The two retained `semantic-fingerprint.json` files independently match their
  reported SHA-256s and **3,584-byte** sizes. Their request and policy identities
  match their respective records, each contains **11 observations**, and both
  carry fingerprint
  `671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.
  Parent output hash is
  `aebc56fa07bc4790a49aa2c3bb165fa5e9938009bd7bdb88bebc6eb56caeddfa`;
  candidate output hash is
  `da70fadf2face0cc925b35640957fad9339753f6ed8e9b00d5fd993e5d7f1d4a`.
- Retained parent and candidate source artifacts match their reported hashes.
  All four parent source strings exactly reproduce raw Git bytes. Candidate
  `entry.py`, `risk.py`, and `position.py` are identical; its only difference is
  the documented appended comment in `exit.py`. The detached source HEAD is the
  pinned revision. The two earlier logs show root-overlap and source-identity
  setup rejection before evaluation; neither is counted as execution evidence.

The helper was inspected, not executed. It makes exactly two fixed-probe requests
through the production container executor/evaluator, with synthetic authority
fixtures, a 60-second per-case mechanics deadline, one CPU, 1024 MiB memory/swap,
32 PIDs, a 64 MiB output limit, no scenario/PIT data, and four read-only candidate
source mounts. Its wrappers record production results rather than replacing
execution. It uses fresh per-case trace lists and valid JSON checkpoint newlines.
The trace lists are deliberately captured before cleanup and contain hashes of
control stdout, not full Docker transcripts; explicit cleanup outcomes, targeted
absence checks, and retained adapter cleanup records provide the cleanup evidence.

This acceptance is finite synthetic integration evidence. It establishes neither
historical strategy performance nor production data admission, failure-path
coverage, or paper-trading qualification. The older accepted #82 source/map/image
receipt remains unchanged and independently scoped.
