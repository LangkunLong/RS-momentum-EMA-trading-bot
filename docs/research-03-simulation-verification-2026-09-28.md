# Research-03 simulation verification — 2026-09-28

This receipt records the bounded verification for issue [#82](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/82), matching the published Research-03 issue body. The issue is open. The live issue body was checked against the saved register; it has no comments. Current upstream `main` was confirmed at `2c01e76a878a338ab2b743c38c4f1310aab3f75b`, which contains the #110 integration. This worktree started from that commit and its source tree `c40c6cb4b7b297559d24d40bb5fd55789c66691f` on `codex/issue-82-simulation-contract`.

## Four statuses

| Dimension | Status | Evidence and limits |
| --- | --- | --- |
| Implementation | Existing V5 simulator, evaluator, and worker-binding paths are present. This task adds tests and this receipt; it makes no production engine or worker changes. | `PortfolioSimulator` was exercised with deterministic synthetic bars. `PitPanelEvaluatorV5` forwarding and policy identity binding were exercised through a capturing simulator. |
| Required inputs | Available for bounded synthetic verification. | Inputs are artificial bars, a deterministic close-at-next-opportunity policy, a parent policy bundle, a comment-only candidate identity, an explicit execution profile, and a synthetic friction scenario. No production PIT bundle or empirical cost data was used. |
| Acceptance evidence | The historical focused checks pass, and actual parent plus comment-only candidate semantic probes passed through the V5 Docker executor. | 15 selected host checks passed earlier. The current semantic probes each exited 0 on the pinned image, produced the same 11-observation fixed-suite fingerprint, and completed owned-container and workspace cleanup. See the tracked machine-readable receipt linked below. |
| Dependencies | The #80 policy contract and #81 reproducibility-index prerequisites are satisfied in the accepted #110 main integration. | The pinned evaluator image and a Docker Engine were available for the completed run. GitHub Actions was not used because the current workflow has no candidate-container job and the available local GitHub CLI credentials are invalid. |

## Simulation input identity

The declared fixture is `_RESEARCH_03_V5_EXECUTION_INPUTS` in [`tests/test_backtest_open_causality.py`](../tests/test_backtest_open_causality.py). Its compact canonical JSON SHA-256 is `81b5afd19b90eff4d2684d9e7c6813f955ed58dd84131eccbb4c7f992856f580`. The fixture specifies symbol `LEAD`, 260 warm-up sessions, 30 evaluation sessions, $10,000 initial capital, 1% risk, an 8% stop, an entry reference of $100, and two exit paths: a next-open policy close at $103 and a gap through the stop to an $80 open. The static fixture also supplies the market context and reference series required by the simulator.

Execution is declared as V5 next-open policy exits, open-then-stop gap handling, last-session-close end handling, and half-spread plus impact plus commission. The scenario is synthetic `fixture-10-20-10-bps`: 10 bp half-spread, 20 bp impact, and 10 bp commission. These values are test assumptions, not measured trading costs.

The expected entry sizing is calculated independently in the test from the declared inputs. Risk budget is `$10,000 × 1% = $100`; target notional is `$100 / 8% = $1,250`. Entry execution is `$100 × (1 + 30/10,000) = $100.30`, and cash per share including commission is `$100.30 × 1.001 = $100.4003`. This yields about `12.450162` shares. The rounded stop is `$92.28`, allowing about `12.468828` shares under the risk ceiling, so the target-notional calculation binds. The entry cash debit rounds to `$1,250.00`. Applying the declared sell-side friction and commission yields final cash of `$10,027.24` on the next-open path and `$9,742.03` on the gap-stop path. The test checks both the independently derived quantity and cash arithmetic against the simulation fills.

## Evidence by stage

### Historical portfolio simulation

The parameterized test `test_v5_portfolio_fills_at_next_open_and_gap_stops_preempt_policy_exit` runs the actual V5 `PortfolioSimulator` twice. It checks transaction dates and reasons, reference and execution prices, quantity, stop reference, policy-intent outcomes, friction, and final cash. In the ordinary path, the close decision queued after the first session executes at the following open. In the gap path, the opening gap stop preempts that pending policy close. The policy is a deterministic V2 in-process test client; this fixture is a historical simulation contract check, not an optimizer panel result or strategy-performance claim.

### Fixed behavior probes

`test_fixed_and_supplemental_observations_use_full_snapshot_identity` passed. It checks fixed P0/A/S decision outputs against a declared truth table, and checks the supplemental synthetic input identities separately. `test_bounded_fixture_observations_are_paired_and_count_unique_relevant_cases` also passed using bounded deterministic fixture workers. Related selected checks confirmed exact parent/candidate context binding and failed-closed behavior when sandbox enforcement or resource limits are unavailable. These observations remain separate from the portfolio result.

### Supplemental mechanism observations

Selected checks passed for protocol timeout, cleanup failure, caller-owned deadlines, no retry, typed evaluator matching, and runtime paths that skip workers for disabled development, rejected candidates, missing executors, or expired deadlines. These are mechanism and stage-gating checks; they are not merged into historical portfolio results or presented as enabled candidate evidence.

### Matched evaluator assumptions

[`tests/test_pit_optimizer_v5_evaluator_assumptions.py`](../tests/test_pit_optimizer_v5_evaluator_assumptions.py) calls the actual `PitPanelEvaluatorV5` baseline and candidate entry points with a capturing simulator. It checks that both calls receive the same PIT bundle, identity-transition contract, execution profile, friction scenario, data scope, benchmark, signal cadence, symbols, dates, and warm-up start. It also checks separate parent and candidate policy identity bindings. Its simulator and worker clients are fixtures: it checks evaluator forwarding and binding only, and does not invoke policy methods, start a candidate worker process, or execute candidate source.

## Runtime and reproducibility

- Host: Windows 11; Python `3.13.14`.
- The #81 receipt records the Windows checked-out `requirements-lock.txt` SHA-256 as `9f1f1f14f18c86a2471a3d81bb5d103b3a26bb37da7f753daf90bfd9b5b312b6`. The actual image build used canonical Git blob bytes with SHA-256 `dd9801c36f86e4853079ca0a77989ce6e4149cba5401b4a574bb6f3833743efd` (Git blob `69df1ab4c04282aec47076c5437d77215f413738`); the difference is Windows checkout line-ending conversion.
- Portable tracked test identities (Git blob IDs after clean filters): `tests/test_backtest_open_causality.py` is `432440258c512d7e4bb28d4989b16d4b83df8999`; `tests/test_pit_optimizer_v5_evaluator_assumptions.py` is `79dc727233e4540d6ea0e154b5d8075c70401be2`. These IDs identify the committed source independently of Windows checkout line endings.
- Focused run: the two parameter cases in `test_v5_portfolio_fills_at_next_open_and_gap_stops_preempt_policy_exit`, the evaluator-assumption test, eleven existing mechanism-evidence checks, and `test_fixed_and_supplemental_observations_use_full_snapshot_identity`. Result: **15 passed in 11.94s**.
- No provider, model, broker, or live-data calls were made. The full offline suite and historical provider attempts were not rerun.
- `git diff --check` passed. Shell fetch and remote-ref checks were unavailable in this worktree; the live `main` pointer and issue body were independently checked through the team lead's GitHub read.
- During the initial host attempt, `docker version` and image listing failed because `npipe:////./pipe/dockerDesktopLinuxEngine` was unavailable. Docker Desktop later recovered; Engine `29.7.2` on Linux/amd64 and the newly built pinned image were used for the successful probes below.

## Initial local Docker startup attempt (superseded)

This section preserves the initial bounded startup failure. No container was created during that attempt. Docker Desktop and the engine later recovered, and actual parent/candidate probes completed as recorded in the final section below. No provider, model, or broker call was made.

### Host state and exact startup failure

- Docker CLI: `29.7.2`, context `desktop-linux`; Docker Desktop is installed in the user-local program directory. Podman is not installed.
- WSL reports version `2.6.3.0` and kernel `6.6.87.2`. Unprivileged distro enumeration returned `Wsl/EnumerateDistros/Service/E_ACCESSDENIED`. A scoped read-only enumeration succeeded and showed the Docker-managed `docker-desktop` distro exists, but its state is `Stopped` (WSL version 2). This is a stopped host distro, not evidence that the distro is absent.
- `docker desktop start -d --timeout 120` started Docker Desktop processes. The Docker API server never appeared at `npipe:////./pipe/dockerDesktopLinuxEngine`; `docker version`, `docker info`, filtered image listing, and inspection of the recorded digest all failed at that pipe. Docker Desktop reported “already running” at the app layer while the WSL distro remained stopped. Its status command could not retrieve engine status.
- The team lead's targeted backend-log read found the startup failure at `2026-09-28T07:17:08Z`: `starting services: initializing Ingest server: listening on unix://C:/Users/llong/AppData/Local/Docker/run/sailor-ingest.sock: remove ... The file cannot be accessed by the system`. A scoped inspection found that path as a zero-byte `Archive+ReparsePoint`, created Aug 19 and last written Sep 13; `Get-Acl` failed with Windows error 1920. A same-directory rename of only that entry was attempted by the lead and failed with the same file-access error. No path was moved and no Docker data was reset.
- Docker Desktop's stop command also failed while the engine/status channel was unavailable. The exact Desktop/backend processes created by this task's start attempt were then stopped. No WSL distro was manually started and no container cleanup or image deletion was performed.

### Source and image identities available during the initial attempt

The integrated source snapshot used for this comparison was commit `ea9cf2ffaf84a03fc8b73a2770f26c8ab8c0e147`, tree `d2999acfe54df0d4a1d6c5730d77fe6fab3038d2`. Its complete V5 evaluator-source-map SHA-256 is `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.

The retained image record pins `pit-optimizer-v5-evaluator@sha256:663f1749ba91df9e501e9de705cca83ff1c46305ca5a2ad589380fbe1d9893aa`, built from source commit `de6e1b888d3658cb7f83842d49eb1441140ad19d`, with runtime-source SHA-256 `5989471897bee94e6886493f71b525c931388e029e6eeaa626abc3678c3e2fd7`. All seven retained image records carry runtime-source identities different from `a55875…`. The `663f…` smoke record explicitly says `evaluations_executed: false`. It is not a usable current-source image identity, and local image presence could not be queried because the engine never started.

The recorded V5 profile is Linux amd64, UID/GID 65532, 1 CPU, 1024 MiB memory, 32 PIDs, 67,108,864 output bytes, one second per policy method, a 60-second mechanics timeout, disabled networking, read-only root/source/data, and bounded write-only output. Historical build inputs record base image `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6` and requirements-lock content SHA-256 `9f1f1f14f18c86a2471a3d81bb5d103b3a26bb37da7f753daf90bfd9b5b312b6`.

For the integrated source commit above, Git blob IDs are `10ae5c560bcca9e2294fc7caae05a5b72acfb97d` for `Dockerfile.pit-optimizer-v5`, `a9f5f11a580d702a7cdce0e9fbe176e5bbd9266b` for its `.dockerignore`, and `69df1ab4c04282aec47076c5437d77215f413738` for `requirements-lock.txt`. The Dockerfile's build argument is `PIT_V5_RUNTIME_SOURCE_SHA256`; its build-time `image_manifest` check rejects installed evaluator bytes that do not reproduce the supplied value.

### Rerun recipe prepared before the successful run

1. At that point, use an isolated Linux Docker host or repair this host so `docker-desktop` is `Running`, `docker version` has a Server section, and the selected `desktop-linux` pipe works. The initial workflow path had no candidate-container job and the available local GitHub CLI credentials were invalid, so no Actions run was started. The successful run later used the recovered local Docker Engine.
2. From the exact integrated source commit `ea9cf2f…`, first verify that the pinned base image `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6` is available locally. `--pull=false` prevents an automatic pull; it does not make the image available. The current Dockerfile installs `requirements-lock.txt` with `pip --no-cache-dir` and has no wheelhouse input. Use either:

   - A matching prepopulated Docker build layer cache that contains the pinned base and the requirements install layer, then build offline with `--network=none`. Record the cache provenance and verify the resulting image identities. A wheelhouse route requires a separately reviewed Dockerfile/build recipe change and updated recipe hash.
   - A separately authorized, networked dependency build on a controlled runner. Resolve only `requirements-lock.txt`, record exact package versions and hashes plus the pinned base digest, and produce the evaluator image from those recorded inputs. Network access is limited to this build step; candidate execution must still use `--network none`.

   With all required inputs already present, the offline build command is:

   ```powershell
   docker build --pull=false --network=none --platform linux/amd64 `
     --build-arg PIT_V5_RUNTIME_SOURCE_SHA256=a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4 `
     -f Dockerfile.pit-optimizer-v5 `
     -t pit-optimizer-v5-evaluator:research-03 .
   ```

   The build must use the exact `Dockerfile`, `.dockerignore`, lock, and source tree identified above. With the offline path, missing base or dependency inputs cause a build failure rather than a fetch. Inspect the result; require Linux/amd64 and label `io.trading-bot.pit-v5.runtime-source-sha256=a55875…`. Record and use an immutable local digest accepted by `SandboxProfileV5`; do not substitute the historical `663f…` digest.
3. Use a local synthetic campaign/artifact fixture and a comment-only candidate derived from the exact four `EDITABLE_POLICY_PATHS_V5` files. At the integrated source snapshot, the parent source-bundle SHA-256 is `109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e`; the comment-only candidate bundle SHA-256 is `cc50fd4992e9a55add36492a875f372b186ef496f34a1f4e153bb2f3b8e038d7`, with fixture policy-revision SHA-256 `c9aecca10e447b6a502ffec244b475b3b4a56f833b3fdf34281365cff0c8739c`. These fixture identities use the test's declared synthetic runtime/constraint identities; they are not production authority.
4. Create a `DockerPanelRequestV5` for stage `semantic_probe`, with `scenario_ids=()`, no data mount, the candidate source mount read-only, and the output mount bound to 67,108,864 bytes. Run it through `DockerPanelEvaluatorV5` and `LocalContainerExecutorV5`; `build_docker_argv_v5` supplies the actual image-owned `core.pit_optimizer_v5.probe_entry` command with `--network none`, read-only root, dropped capabilities, no-new-privileges, 1 CPU, 1024 MiB, 32 PIDs, a one-second policy-call timeout, and a 60-second mechanics deadline. This path runs the actual bounded V3 candidate worker on the fixed `pit-policy-v3-probes-v1` suite without mounting PIT data or calling a model/provider.
5. Retain the exact source commit/tree and runtime-map digest, immutable image digest and runtime label, parent/candidate source-bundle and policy-revision identities, request/command hashes, `SemanticFingerprintV5` hash and per-probe input identities, exit status, bounded output size, resource limits, and owned-container cleanup/absence evidence. The comment-only candidate should have a distinct policy identity and the same fixed-suite fingerprint as its parent. Treat a missing immutable local digest, nonzero exit, identity mismatch, or incomplete cleanup as a failed/unverified container run.

At the time this recipe was written, actual candidate container execution remained unverified. The subsequent run below supersedes that status. The historical portfolio simulation and 15-test focused set remain separate host/test evidence.

## Actual local V5 semantic-probe verification

The parent and comment-only candidate were executed through `DockerPanelEvaluatorV5` and `LocalContainerExecutorV5` using the exact immutable image reference below. Issue #82 remains open pending team-lead review of this receipt.

### Source and image provenance

- The policy source used for the probe was the clean detached Git worktree at commit `8fa8c118eb86a63fb142b1366381071e11952a46`, tree `ca158802e4da51cc1279d1702b2aa5bbf3ae3b84`, with `core.autocrlf=false`. The latest integrated branch head when this receipt was updated was `5ca8da2a134ee14ab15e06f5e7679becdaee6cb7`, with later packet/evidence and checkout-attribute changes outside the V5 source closure; its V5 evaluator source map and four editable policy blobs match the probe source. The runtime-source-map SHA-256 is `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.
- The image is `pit-optimizer-v5-evaluator@sha256:bf842ff5e0dc741f95e96129d224c24d7834feef23c4c20fdfe1d5297d54b527`, Linux/amd64, image config ID `sha256:41eb525bde9ba64fb9f2c54de880ed6a719cd4d824424f7f22a3a93f6665fa63`. It carries runtime kind `evaluator-probe-v5` and source-map label `a55875…`. The pinned base is `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6`.
- The first offline image build stopped at the uncached `alpaca-py==0.44.0` dependency. The lead authorized one networked dependency build from the exact canonical lock and pinned base; that build succeeded. Network access was limited to the image dependency build. Candidate containers still used `network=none` and `--pull=never`.
- The canonical lock has 71 exact version pins and no wheel or distribution `--hash` entries. The build log also showed unpinned `SecretStorage==3.5.0` and `jeepney==0.9.0` transitives from `keyring`; no artifact hashes are claimed. The complete pinned version list and Dockerfile/ignore/lock Git blob IDs are in the [machine-readable receipt](research-03-candidate-container-evidence-2026-09-28.json).

### Synthetic parent and candidate

Both requests used stage `semantic_probe`, `scenario_ids=()`, and the fixed suite `pit-policy-v3-probes-v1`. The candidate source was made by appending only `# Issue 82 synthetic comment-only candidate fixture` to `core/strategy_policy/v3/exit.py`. The parent source bundle SHA-256 is `109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e`, with synthetic policy-revision SHA-256 `76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9`. The candidate source bundle SHA-256 is `7c033b6436ece98b9b0bc0b39b578ac0067e332dfde35ef791e59ea7c3ab3bc4`, with distinct synthetic policy-revision SHA-256 `6d3008e8eaba6153b84cdba4c916df184eae06d5b7dbab2a872bced0595ed606`. The fixture used placeholder trusted-runtime and immutable-constraint identities (`1…1` and `2…2`); these are synthetic identities, not production authority.

The request had four read-only policy-file mounts, no PIT/scenario data mount, and bounded write-only output. The runtime profile was 1 CPU, 1024 MiB memory, 32 PIDs, UID/GID 65532, a one-second policy-method timeout, a 60-second mechanics timeout, and a 67,108,864-byte output bound on a read-only root filesystem. No production PIT data, provider, model, or broker was used.

### Container outcomes

- **Parent:** request `b0003f4d956c1d1e5367f6e0a911d0e5b3cacc8fa28c16ad16c1a9d58772cd4c`; command `f2d97f2f5e0dda4a67e66084a18e309a1dd9ace8216388fde14b6e44a3e68bf4`; container `fe224274ab520219dbf2af4d70a91fca6c847cee642388be767f5cc91e3e5992`. It exited 0, returned 3,584 bytes (`c171e760f24810a236c9e99ad66aaa3eba29dc8fc4b519ad49caf5b68a737007`), and produced 11 observations with fingerprint `671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.
- **Comment-only candidate:** request `69d4d016375d0b5bf9af56342a01b82ffaa540bb90664c83a7325d91a93cda13`; command `376b10a2b87f084ec0c068a2b5b4c41aa38a6a5316d11251b4a56a77a14886b7`; container `7d442409be08bdf44ca2768848b8d42f7f82251e66fbaa1e0c7e2060fb3602d8`. It exited 0, returned 3,584 bytes (`22b3fdc31e313dee24ac48c8fbc50b29f92d210da374fda3da7357d72b17a3a7`), and produced the same 11-observation fingerprint `671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.

The output byte hashes differ because `probe_entry` wraps the semantic fingerprint with each request's `request_sha256` and `policy_revision_sha256`; those identities differ. The inner fixed-suite semantic fingerprint matches exactly.

For both runs, `DockerPanelEvaluatorV5.cleanup` reported `cleanup_complete=true` with one owned evaluator and one owned container. The exact-name `docker ps -a` checks returned no containers, and each candidate workspace cleanup returned true. No unrelated Docker container was stopped, restarted, or removed.

### Host harness setup failures (not candidate results)

Before the successful runs, the temporary host harness first pointed at the ordinary Windows checkout, whose CRLF policy files were rejected by the LF-only V5 source validator. A later pre-Docker execution attempt then failed inside the pinned control transaction with `ModuleNotFoundError: No module named 'agent_loop'` because the temporary harness omitted the host process adapter. That attempt returned `driver_failed`, zero output, and no container ID; it is not a candidate result. The harness was corrected with an LF-preserving detached source worktree and exact `agent_loop.py` Git blob `b038453c3a343dc6548b2b6f0c21b4d30b4b1312` (SHA-256 `582b0b694017473673460b7a51f4283c35fd6000d38e1dadff5379f955969d6e`). No production source or image code changed.

The raw local capture was retained outside the repository; the tracked JSON contains the exact identities, package pins, outcome hashes, limits, cleanup results, and a SHA-256 binding to that capture. The earlier Actions route did not run: the existing workflow has no candidate-container job and the local GitHub CLI credentials are invalid. Actions supplies the host runner; the current V5 sandbox still requires Docker Engine on that host.

## Disposition

The bounded parent and comment-only candidate semantic probes now execute successfully through the actual V5 Docker evaluator path, with matching fixed-suite fingerprints and complete owned cleanup. The receipt does not establish portfolio-level candidate performance, empirical friction realism, production market coverage, or actual historical provider costs. Issue #82 remains open for team-lead review; this evidence does not close the issue or estimate or reconcile the costs recorded in the accepted #89 incident index.
