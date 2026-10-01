# Changed-source evaluator image verification — 2026-10-01

Actual bounded local build, isolated import and matched fixed probes passed at source `a8662e85c4c06142183c707926e7c302c75e233d`, tree `249375f4e6f4f162df9ebd9575b5d35144bfa4ea`. This is new evidence for the scheduler-observation dependency closure; the accepted dated #82 8a receipts remain unchanged.

## Source and image identity

[Canonical context manifest](lead-b-evaluator-a866-context-manifest.json) records all 58 runtime files and three recipe/lock files from raw Git bytes. Runtime map: `ea44827dc78dcc3f4a4293929e4897345d1f0daa7faef6fbd50cdbb00757d2af`. Docker's existing installed-map verification succeeded before labeling the image. The added module has only standard-library imports; [independent source review](issue-b-image-closure-independent-review.md) records complete relevant closure and lazy excluded dependencies.

Docker Engine was 29.7.2, Linux/amd64; host CPython 3.13.14 on Windows. Pinned base and requirements lock are unchanged. Image tag `pit-optimizer-v5-evaluator:lead-b-a8662e8-ea44827d` resolves to backend/OCI index ID `sha256:e2e756560b528c6d57c6630e21c615f50bb4026d3f6412c66135ef015d530349`. Separately exported image-config digest is `sha256:a15d7824ae02ee9548235d0f60cfcaed65b960b1f2783c1ee129be243272d552`; it is not substituted for the runnable backend identity.

The first successful build used the selected docker-container builder and retained its result only in cache. A subsequent `--load` build exported the same pinned context locally. No image was published. Both build logs are retained. Dependency installation used network access during image building; evaluator containers had `network=none`.

## Actual execution

The import smoke had no mounts and imported observation, data/index clients, container and probe entries, and `PortfolioSimulator`. It independently recomputed the installed source map. The inspected created container image equaled the recorded backend ID, exit was zero, and owned cleanup/absence checks passed.

The production `LocalContainerExecutorV5` and `DockerPanelEvaluatorV5` then ran the fixed `pit-policy-v3-probes-v1` suite for an exact raw-Git parent and a comment-only candidate. Both returned 11 observations, 3,584 output bytes and zero failures. Their identical semantic fingerprint is `671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.

| Case | Policy revision | Request | Output SHA-256 |
| --- | --- | --- | --- |
| Parent | `76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9` | `8e4d9928c3f4b4c2e59264198ec6aa40995640219e69954e589a924ef0318090` | `aebc56fa07bc4790a49aa2c3bb165fa5e9938009bd7bdb88bebc6eb56caeddfa` |
| Comment-only candidate | `fe2ab3efb2c69de70224101c5812fd41e1220fc572c555c0b308f5f8dcc499a7` | `c4c2b8f403aa5f5d39a3eae71474167bf117e30dc902f4e257468689e0aa1f3f` | `da70fadf2face0cc925b35640957fad9339753f6ed8e9b00d5fd993e5d7f1d4a` |

Every container used `--pull never`, network none, read-only root, all capabilities dropped, no-new-privileges, UID/GID65532, one CPU, 1024MiB memory/swap and 32 PIDs. Candidate probes mounted only four read-only policy sources plus bounded output (64MiB), with a 60-second mechanics deadline and no scenario/PIT data. Each actual container `.Image` was additionally retained and matched the authenticated backend ID. Both evaluator cleanup and candidate-workspace cleanup completed; targeted absence checks returned no owned containers.

The adapted finite harness copies separate 15-entry pre-cleanup control traces per case and writes valid interim JSON. It preserves explicit per-case cleanup results. Two setup failures preceded execution: nested disposable/source roots were rejected, then the normal Windows checkout's CRLF policy bytes were rejected. Neither reached container creation. The final run used a clean detached checkout created with `core.autocrlf=false` at the exact commit, and fresh disjoint scratch paths. These failures and logs remain retained; they are not passing runs.

## Retained evidence

Base directory: `C:/Users/llong/.codex/worktrees/b367/RS-momentum-EMA-trading-bot/.artifacts/lead-b-image/`. These are local-only artifacts; the context manifest above is the public copy. Hashes below are full-file SHA-256.

| Artifact | SHA-256 |
| --- | --- |
| `context-manifest-a8662e8.json` | `6b64ebfaa432af0af4010bc4c6d5f5f7be59e79ecf7e8bdd854d6be5b844d115` |
| `build-a8662e8.log` | `77ba348e01609177ae0af3a470ff93db2c0ab95152c2ad5165364ffef7af6df6` |
| `load-a8662e8.log` | `2aaeadd776b36fdd534406c4306dc85eb9e4a776bd449026566e8fe905a27ffb` |
| `import-smoke-a8662e8.json` | `b5bddda2ec43a36df87352ef4f040ade046160230d5ca0f3a7e311d3ae0c277b` |
| `run_probes_a8662e8.py` | `20aa9f06f89a0858f6715eccb10107aeb60ad9cc186904608efe8ee7ff8dac8a` |
| `probes-a8662e8-run3.log` | `1909cde1e5f145a01851a2a97ec1d278089fba2bca1ce8c460319c9e656c2490` |
| Final probe evidence below | `95943aed5131c8b47a53c784dc07f5a44361b566be293ae7150a2658f2f325f7` |

Final evidence: `C:/Users/llong/.codex/visualizations/2026/10/01/01a0f851-c7ac-7b81-87ab-3de389d47510/lead-b-image/probes-a8662e8-run3/evidence.json`. Canonical detached source: same visualization root `/.source-a8662e8`. The harness derives from retained #82 helper SHA `6cf0404ac7dd32b969232879cde8b2c8d3ed3d9d05dd5df5dc50e2e673472104`, with explicit new source/image/output identities and the trace/JSON improvements above.

This is synthetic fixed-probe integration evidence, not historical-real-data evaluation, model use, portfolio improvement, production data admission or paper qualification. No provider or broker calls occurred. It does not accept future changed evaluator bytes or complete #85/#90 by itself.
