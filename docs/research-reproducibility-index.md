# Research reproducibility and evidence index

**Checked:** 2026-09-27. **Purpose:** locate canonical source, runtime identities, retained inputs and evidence for issue #81. This is an index, not a claim that historical studies were rerun or that the evaluator is currently runnable.

## Start with the source identity

| Item | Identity and meaning |
| --- | --- |
| Integrated research source | `1888016ccd6eac98aae946a47f9abb2752a01a8b`, reviewed V5 integration. Main was fast-forwarded from `2ed31460a44a59ba293ed3e614d33788b694d359`; see original-checkout local-only `.artifacts/evidence/main-integration/README.md`. |
| Current post-merge source at the start of this index | `c628a3af3c2c14dd684340d1b695ce91f8438842`, cleanup/documentation commit after integration. This issue branch is based on it. Its cleanup verification is recorded in original-checkout local-only `.artifacts/evidence/cleanup/verification.json`. |
| Historical V5 offline transport acceptance | Reviewed source commit `1888016ccd6eac98aae946a47f9abb2752a01a8b`, source tree `b604c12cb6bd278b1952195dccb5a8715c9fd4c8`, branch then `codex/v5-two-round-design`. See original-checkout local-only `.artifacts/evidence/integrated-transport-review/principal-acceptance.md` and `.artifacts/evidence/integrated-transport-review/principal-commit-verification.json`. Its bounded offline PASS applies only to that reviewed source and listed checks. It is not acceptance of `c628a3a`, live provider connectivity, a completed study, strategy improvement or paper trading. |

The issue worktree started at `c628a3a`; the source integration was already done. A documentation-only commit on this issue branch changes repository history but does not change the code source identities above. New experiment receipts must record the exact source revision they actually execute.

## Shared identity vocabulary

Issue #80 confirmed these shared names for result and evidence references:

| Canonical field | What to bind |
| --- | --- |
| `source_revision` | Full Git commit for the source actually used; include a tree or dirty-state identity when relevant. |
| `runtime_identity` | Interpreter/runtime version, platform and environment/lock identity. Identify host orchestration and contained evaluator runtimes separately. |
| `evaluator_image_digest` | Immutable image digest, not a mutable tag. A recorded digest does not prove the image is locally present or obtainable. |
| `evaluation_mode` | Explicit operation/study mode; never infer it from a directory or image label. |
| `input_bundle_id` | Stable identity of the input bundle plus its manifest/content hashes and scope. |
| `evidence_root_id` | Identity of the retained evidence set/root; each referenced item carries path, hash and visibility. |

Keep these separate result dimensions where applicable: `optimizer_version=5`, `policy_interface_version=3`, `policy_artifact_id`, `feature_contract_id` and calculator identity, `historical_data_format_version` (2 or 3), `execution_profile_id`, and cost assumptions. These identify different components and must not be collapsed into one generic version.

As of this index review, the shared identity tuple reads:

| Field | Current value or explicit limit |
| --- | --- |
| `source_revision` | `c628a3af3c2c14dd684340d1b695ce91f8438842` for the post-merge code inspected; no evaluator run was made against it. |
| `runtime_identity` | Host check: Windows launcher `C:\WINDOWS\py.exe`, Python `3.13.14`. Historical Linux evaluator receipts also use Python `3.13.14`; they are separate runtime identities. |
| `evaluator_image_digest` | Historical receipts only (seven values below); none is shown to have been built from `c628a3a`. Current local image presence could not be checked. |
| `evaluation_mode` | `not applicable: image build/smoke receipt only`; its smoke explicitly says `evaluations_executed=false`. Study and semantic modes are separate axes below. |
| `input_bundle_id` | `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de` (limited development S&P 500 bundle). |
| `evidence_root_id` | `retained-artifacts-2026-09-26`, bound to cleanup retention-manifest SHA-256 `64976d89c6dfc356cf7c46d8ddcb307330b9ffb4687b8919dd449338a019127f`. |

## Runtime and evaluator receipts

### Runtime available for this bounded check

The Windows Python launcher at `C:\WINDOWS\py.exe` resolved `py -3.13 --version` to **Python 3.13.14** on 2026-09-27. This version-only check did not import application modules or inspect credentials. It establishes that a host interpreter is available; it does not establish a matching virtual environment or dependency set.

### Recorded evaluator image identities

The ignored local-only `evaluator-environment` directory contains seven image/source identities. The six `builds/<source-prefix>/` records explicitly identify local image builds; the seventh is a separate prepared/smoke image receipt. Full references and corresponding runtime-source hashes follow. All build receipts identify the base image as `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6` on Linux amd64.

| Recorded source commit | Runtime source SHA-256 | `evaluator_image_digest` | Local-only record |
| --- | --- | --- | --- |
| `a068e0aed40ecbe5e0078924071f374e7809b8c2` | `005064e57e80840f3b2a97d7db694b176832b8f982996e63efc8a35064505ed4` | `sha256:4e85b19aa8c135362ae8796e4d33a410ec5af650d296c358ae6a7737d5b05b6a` | `.artifacts/evidence/evaluator-environment/builds/005064e57e80/image-record.json` |
| `cbd0d8dcec5a9830087b233cc4cbb87fd58dc07d` | `045ec48532c2827b94c006fa43e9414c8cb6d1279cd298e56b7a48af976525a6` | `sha256:4df1517fddafae31944f1943d2223bdc80ff4bf39d21768ddb1a04fca5b62caa` | `.artifacts/evidence/evaluator-environment/builds/045ec48532c2/image-record.json` |
| `a068e0aed40ecbe5e0078924071f374e7809b8c2` | `244f746cbf6eea3802f1caf5fb4cc3e80fa279c82b363f979a4aa78a575e2a3c` | `sha256:6773fb279b9ffa0babc8b0a6622d56d73ca1f1ab0838e2809daf88fb902f9168` | `.artifacts/evidence/evaluator-environment/builds/244f746cbf6e/image-record.json` |
| `a068e0aed40ecbe5e0078924071f374e7809b8c2` | `4acebbc4abd42399f7c2b2b02a9493f581e53d21c9d9f04653a27f03daf1d428` | `sha256:51ea3ca6ea117fb5c6d8f8d21489c0c0b8ddfb7de10b49329dcac6d94a96b4a3` | `.artifacts/evidence/evaluator-environment/builds/4acebbc4abd4/image-record.json` |
| `e54498b755818e9bdba08c36704ef9ca359e71de` | `9b0904dca8f4b3b8d476826d0f046105432a0a821d6e2ad2959395dc19b12050` | `sha256:70cd3c72296058dab4085856feb08ded7e796eb702113a469fbffb0f415da4fa` | `.artifacts/evidence/evaluator-environment/builds/9b0904dca8f4/image-record.json` |
| `a068e0aed40ecbe5e0078924071f374e7809b8c2` | `b24bebd9870c78084666491cd0b6c926a8e0b88b258e7ffb11c923e6eda275b5` | `sha256:c92300936afbd67dd7767420767224d4eed06f599fe9a8c433c1c9c06a8a153c` | `.artifacts/evidence/evaluator-environment/builds/b24bebd9870c/image-record.json` |
| `de6e1b888d3658cb7f83842d49eb1441140ad19d` | `5989471897bee94e6886493f71b525c931388e029e6eeaa626abc3678c3e2fd7` | `sha256:663f1749ba91df9e501e9de705cca83ff1c46305ca5a2ad589380fbe1d9893aa` | `.artifacts/evidence/evaluator-environment/image-provenance-663f1749ba91.json` |

The last receipt records Python 3.13.14 in the image smoke, Linux amd64, a read-only root/source, read-only data, bounded write-only output, no host mounts and network mode `none`. It records imported module names and `evaluations_executed: false`. The image-preparation index separately binds the runtime-source file, default resource limits and sandbox profile. Recipe hashes recorded for that prepared image are Dockerfile `9161715e3ba1c964384b9b685848279f24b54c41813395514fed1fbd97129413`, dockerignore `a05b3e5aa9ca064074e53fc727d8a11f399c046c41744cc6bcd83d5d079b3afd`, and requirements lock `9f1f1f14f18c86a2471a3d81bb5d103b3a26bb37da7f753daf90bfd9b5b312b6`. Two Linux transitive packages were not pinned in that recorded build: `SecretStorage 3.5.0` and `jeepney 0.9.0`.

These are **recorded identities**, not current availability. The bounded `docker image inspect` check could not connect to Docker Desktop’s Linux engine (`npipe:////./pipe/dockerDesktopLinuxEngine` was absent). No container was launched. Current local presence or reproducibility of any listed image is therefore unknown; do not pull, rebuild or start one implicitly.

## Modes: record each axis explicitly

`evaluation_mode` must say what the run did. Keep these existing, distinct axes:

| Axis | Values/evidence | Interpretation |
| --- | --- | --- |
| Image lifecycle | build, smoke, evaluator execution | The recorded `663f…` image receipt is build plus smoke; no evaluation ran. The image digest alone is not evaluation evidence. |
| Study/provider mode | `offline_fixture` or `live_study` (`core/pit_optimizer_v5/two_round_study/contracts.py`) | The former uses synthetic fixture responses/accounting; the latter is provider-backed and requires its own authorization and accounting evidence. This issue did not run either mode. |
| Semantic data mode | `required` or `disabled_development` (`core/pit_optimizer_v5/contracts.py`) | `disabled_development` is restricted to `development_sp500_v2`; record it separately from provider/study mode. |
| Input scope | e.g. `development_sp500_v2`; record the named production scope only when actually supplied | Bundle scope is not an evaluator mode or an acceptance level. |

For every future receipt, state all applicable axes and use an explicit `not applicable` with reason for an operation such as image preparation. Do not label a smoke as an offline evaluation, or a fixture study as live model-authored evidence.

## Retained input bundle and other local-only inputs

The limited development bundle is at `.artifacts/data/development-sp500-v2/` in the original checkout. Its `input_bundle_id` is the manifest-declared bundle SHA-256 `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`; the `bundle_manifest.json` file itself hashes to `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`. The manifest identifies schema 2 (`canslim_pit_v2`), S&P 500 scope, evaluation dates 2021–2025, warmup/data beginning 2020, 606 membership symbols, 609 price symbols including reference funds, 869,041 price rows, and 142,329 fundamental rows across 565 symbols. These are manifest-recorded counts, not fresh coverage acceptance. This bundle is not the complete production universe or a qualified strategy dataset.

Other retained local-only input locations, indexed by [docs/local-artifacts.md](local-artifacts.md), are:

| Relative location under `.artifacts` | Content and scope |
| --- | --- |
| `data/acquisition/` | Dated membership/source inputs and industry classification provenance. |
| `data/sec-fundamentals/` | SEC financial archives, normalized records, identity/publication and coverage receipts. |
| `data/sec-institutional/` | Institutional ownership seed/archive inputs. |
| `data/historical-price-source/` | Historical price cache used by development inputs. |
| `evidence/unresolved-studies/` | Four study roots and eight exports. Study three’s historical names are `new-study`, `new-time-zero-export`, and `new-incomplete-export`; the other roots/exports retain their recorded names. |

Retained studies are immutable and unresolved. Four historical study costs remain unknown; no reopening, cost reconciliation, rerun or deletion was performed for this index. That unknown accounting does not block independent indexing.

## Evidence map and visibility

Paths below are relative to a repository/artifact root. `committed` means ordinary Git checkout material. `local-only` means retained ignored files; a normal clone does not contain them.

| Item | Visibility | What it supports / does not support |
| --- | --- | --- |
| `docs/research-reproducibility-index.md`, `docs/local-artifacts.md` | committed | This index and local artifact map. They are locators, not the local data themselves. |
| `.artifacts/README.md` | local-only | Current retained artifact catalog and cleanup notes. |
| `.artifacts/evidence/cleanup/retention-manifest.json` | local-only | Original-checkout file with old-to-current path mappings and per-file SHA-256 values; see verification hash below. |
| `.artifacts/evidence/cleanup/verification.json` | local-only | Original-checkout record: 3,843 retained files in 110 paths, all retained hashes matched then; 22,558 stale files deleted; studies not reconciled; no provider/broker calls. Saved checkpoint, not a new full-root rehash in this turn. |
| `.artifacts/evidence/evaluator-environment/` | local-only | Original-checkout receipts with seven recorded image/runtime identities, profiles, resource caps and builds/smoke; not a current available image or evaluation result. |
| `.artifacts/evidence/integrated-transport-review/principal-acceptance.md` | local-only | Original-checkout report of bounded offline source-specific PASS on `1888016` with declared limits. Not live connectivity, trading or acceptance on `c628a3a`. |
| `.artifacts/evidence/main-integration/README.md` | local-only | Original-checkout integration, source validation and pre-existing test-failure account. Does not say the full suite passed. |
| `.artifacts/evidence/source-history/` | local-only | Original-checkout compact recovery bundle and branch/path cleanup receipts; use only for the specific historical references described in the artifact guide. |
| `.artifacts/evidence/incidents/` | local-only | Original-checkout historical provider authorizations and unresolved diagnostics; not authorization for new calls or proof of cost. |

For all new records, use the six shared #80 keys above. Each evidence item should also carry `{path, sha256, visibility, evidence_kind, source_revision, input_bundle_id, availability_status, acceptance_status}` as applicable. `availability_status` should distinguish present-at-check from identity-only or unavailable-to-check; `acceptance_status` should name the exact source/check rather than a bare pass. Preserve old absolute paths inside immutable documents; resolve them with the retention manifest instead of editing those documents.

## Original-checkout and worktree path resolution

The retained files are physically in the original checkout:

`C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\`

This issue’s Git worktree is:

`C:\Users\llong\.codex\worktrees\4939\RS-momentum-EMA-trading-bot`

Its `.artifacts/evidence/evaluator-environment/` does not exist. The equivalent directory does exist under the original checkout. A Git worktree checks out tracked files and Git metadata; ignored `.artifacts` data/evidence is not automatically copied. Use `.artifacts/evidence/cleanup/retention-manifest.json` in the original checkout to map each historical `original` path to its current `destination`; retained destinations are repository-relative under the original checkout’s artifact root. The manifest itself has SHA-256 `64976d89c6dfc356cf7c46d8ddcb307330b9ffb4687b8919dd449338a019127f`.

To reopen from another worktree or machine, provide an authorized copy or explicit mount of the retained artifact root, resolve relative entries against that root, and verify each listed file hash before use. Then provide the exact historical source reference if the claim is source-bound and the specified interpreter/image if execution is needed. A manifest, hash or old absolute pathname alone cannot supply a missing file. Portable reopening of the studies and evaluator is not claimed here.

Use repo-relative paths plus hashes for new references and label ignored artifact paths `local-only`. Do not put machine-specific junctions into the contract, rewrite historical evidence, or restore deleted broad archives as a shortcut.

## Bounded verification performed for this index

Read-only checks on 2026-09-27, without application imports, study execution, tests, image builds or containers:

- Confirmed current source checkout was clean at the start and `HEAD=c628a3af3c2c14dd684340d1b695ce91f8438842`; both that revision and historical commits `1888016…` and `de6e1b8…` resolve in the local Git object database.
- Recomputed SHA-256 against the retained relocation manifest for every recorded file under three targeted roots: all **59** evaluator-environment files, **44** integrated-transport-review files, and **6** development-bundle files were present and matched their recorded hashes; zero missing files or mismatches. This confirms these local bytes against the manifest now, not that their old runs can be replayed.
- Verified `py -3.13 --version` reports Python 3.13.14. No package/import check was run.
- Attempted read-only `docker image inspect` for recorded digest `663f1749…`; it could not connect because Docker Desktop’s Linux-engine pipe is absent. Image availability is unknown, not established as present or absent.
- Source-text inspection confirmed study modes `offline_fixture` / `live_study` and semantic modes `required` / `disabled_development`; no project module was imported.

No full artifact-tree verification was repeated: the saved cleanup receipt already records its earlier all-retained-hash result. No historical studies were opened. No experiments, container runs, installations, provider/model/broker calls, credential reads, or application imports were performed.

## Four separate issue status fields

The published issue/register snapshot remains **Implementation: Not assessed; Required inputs: Not assessed; Acceptance evidence: Not assessed; Dependencies: Ready to start**. This index and its checks are deliverables for review, not an unrecorded GitHub status change.

| Field | Current evidence for review | Remaining qualification |
| --- | --- | --- |
| Implementation | The integrated source is already on the starting revision; this tracked index is the documentation deliverable on the issue branch. | Review/merge the document. It does not integrate source or add execution support. |
| Required inputs | Targeted retained input and evidence files are present in the original checkout and 109 targeted files match the relocation hashes; host Python 3.13.14 is available. | Artifacts are not in this worktree/ordinary clone. The local Docker engine was unavailable to inspect images. Complete production data is outside this issue’s input scope. |
| Acceptance evidence | Historical source-specific acceptance remains linked to `1888016`; targeted current byte/hash and interpreter checks are recorded above. | No evaluator execution/acceptance on current `c628a3a`, no current image availability, and no portable study reopening were established. Do not transfer the historical PASS to current source. |
| Dependencies | Registered start was independent; shared identity names are aligned with #80. | No remaining start dependency. Later evaluator/data acceptance can be referenced by #81 but belongs to #82–83. |

Unknown historical costs and incomplete studies are intentional unresolved evidence, not missing implementation for this index. They remain owned by their incident/study work and do not block the independent reproducibility documentation.
