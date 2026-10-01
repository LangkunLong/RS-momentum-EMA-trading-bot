# Issue 82 integrated-source local candidate receipt — 2026-10-01

This is the bounded actual Docker verification of the integrated V5 evaluator at
`8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`. It supplements the
[source-matched image build and import receipt](issue-82-local-8a-build-import-receipt-2026-10-01.md)
and the earlier [d764 candidate receipt](issue-82-local-d764-final-source-receipt-2026-10-01.md).
Those earlier outputs retain their own source and image identities.

## Exact source, inputs, and runtime

- Evaluator commit `8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`, tree
  `e084b7f18bc5e0b07ecf94cd1b51a0592b03f0c5`, 57-path raw-Git evaluator
  map `f6dee0745545308088887a924b9839efedd8f1beff1dfbfac82fd889935819f0`.
  The 60-file canonical context was independently matched to raw Git bytes in
  the linked image receipt. The calculator Git blob was
  `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`.
- Docker Engine `29.7.2 linux/amd64`. The locally built evaluator image was
  `pit-optimizer-v5-evaluator:issue82-8a0896a-f6dee074-canonical`. Docker
  image/OCI index and pre-inspected backend ID were
  `sha256:1e0e6327f48e96d4f9754c6bae47969f0d8cf57f3c2b6cb8a10ab2ece28af0df`;
  the separate BuildKit exported config digest was
  `sha256:283a97f89d0689d5780f91784d4d944bd4ce55ff83e80df0dd50e4648cc62704`.
  The image label held the exact evaluator map. The executor inspected the image
  and required each created container's `.Image` to equal the inspected Docker
  backend ID before starting it (`core/pit_optimizer_v5/production_sandbox.py`).
  The JSON stores the container-inspect output hashes and successful guard
  outcome, rather than the literal observed `.Image` field for each container.
- Host command: `python .artifacts/evidence/issue82_run_probe_8a0896a.py` from
  the integration worktree. The executed local harness SHA-256 was
  `6cf0404ac7dd32b969232879cde8b2c8d3ed3d9d05dd5df5dc50e2e673472104`.
  The host used CPython 3.13.14 on Windows 11. The harness read the pinned
  detached source checkout, computed its source map before execution, and
  derived both policies from the four raw-Git editable policy files. The
  candidate differs only by one appended comment in `exit.py`.
- Retained ignored files in this worktree:
  `.artifacts/evidence/issue82-local-8a-candidate-20261001/evidence.json`,
  SHA-256 `859c65a7e413118ff71f4001857233ae25d0a65273aaa7f5b806f520f7357c7c`;
  `.artifacts/evidence/issue82-local-8a-candidate-probe.log`, SHA-256
  `d2c19ce0f86b61b9814dfd92ad1c6c23243b97364e86096f5b718292c35fad01`.
  The final JSON parsed successfully and contains exactly two probes.

## Actual matched probes

Both runs used the same pinned evaluator image and fixed
`pit-policy-v3-probes-v1` suite. The four policy sources were separate
read-only mounts. No PIT/scenario data was mounted. Each container ran with
`--network none`, `--pull never`, read-only root, all capabilities dropped,
`no-new-privileges`, UID/GID `65532:65532`, one CPU, 1024 MiB memory and swap,
32 PIDs, a 60-second mechanics deadline, and a 64 MiB output bound.

| Case | Policy revision / source bundle SHA-256 | Request / command SHA-256 | Result |
| --- | --- | --- | --- |
| Parent | `76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9` / `109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e` | `ab22d453fce7782713579d4f2c7dc426572b0d7232bd1b5ad7267db5b50570e4` / `6f1cbaed85b4ab33817d6caefe2fc5d91c5c75d6dd7966231b7679622b3a0d1b` | Exit 0; 11 observations; 3,584 output bytes; output SHA-256 `bf27efc0eba7c465bdba00ebbceaebfe261656aa3dd9214785460bcb57a31f4b`. |
| Comment-only candidate | `6d3008e8eaba6153b84cdba4c916df184eae06d5b7dbab2a872bced0595ed606` / `7c033b6436ece98b9b0bc0b39b578ac0067e332dfde35ef791e59ea7c3ab3bc4` | `7841a80a4436b6f71827b6153714e320dbd93d9f8d1b4c6462814444e220d2c1` / `714dee153c4ea7bd403aaeeae32d757eb4b3a990d89b68b5ee0db8de93beeb16` | Exit 0; 11 observations; 3,584 output bytes; output SHA-256 `748e7fd6ad4f6dad75a5dd16597463ce5143f91cf833ea7e72e6d58756728162`. |

Both runs returned the identical semantic fingerprint
`671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`.
The receipt records different policy and request identities, valid runtime
profiles, no scenario mount, all 11 observations, and zero outcome failures.
For each run, evaluator cleanup was complete, the candidate workspace was
removed, and its targeted container-absence query returned exit 0 with no
names. An independent `docker ps -a --filter name=pit-v5-` query after both
runs returned no container names.

The local harness assigned one mutable Docker control-trace list into both
probe records, so the final JSON's trace arrays are the same combined 52-entry
chronology. The streamed log preserves the sequential pre-cleanup and final
per-case snapshots; each probe's request, command, result, and cleanup fields
remain separate. This trace-presentation limit does not change either actual
container outcome.

The executed harness writes an interim checkpoint with a literal `\n`; a
failure between probes could leave that interim file invalid JSON. This run
completed and rewrote the final file with a real newline; the final JSON was
parsed and audited. The focused failure/timeout/cleanup mechanism tests are
separate evidence and should not be inferred from this passing two-case run.

## Acceptance boundary

The [simulation receipt](research-03-simulation-verification-2026-09-28.md)
separates next-open/gap-stop simulation, fixed probes, and supplemental
mechanics. This local run verifies only the fixed 11-probe suite on synthetic
policies. It does not establish global behavior equivalence, production PIT
data coverage, empirical strategy returns, or paper-trading qualification.
The prepared GitHub Actions runner package remains unpublished and
undispatched; no hosted execution is claimed.
