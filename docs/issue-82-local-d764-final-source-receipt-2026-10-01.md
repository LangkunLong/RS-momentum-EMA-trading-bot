# Issue 82 local final-source receipt — 2026-10-01

This addendum records a local execution of the final integrated evaluator source
for issue #82. It supplements the earlier [simulation receipt](research-03-simulation-verification-2026-09-28.md)
and [candidate-container evidence](research-03-candidate-container-evidence-2026-09-28.json);
it does not relabel or replace their evaluator map
`a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.

## Source and build context

- Source commit: `d764c8791e27c6b0fc42bb21e9c2d74ca040b5ff`; tree:
  `99efa7740ba1c215c5f5992ce6947394b3a3a82b`.
- The 56-path evaluator-source map is
  `3d57003a79ae5cc85cb3e4320568217e8b793a642363cca2c504caaba0aa82cf`.
- The canonical Docker context contained 59 files (1,429,625 bytes): those 56
  source paths plus the Dockerfile, its Dockerfile-specific ignore file, and
  `requirements-lock.txt`. Every context file matched the raw Git blob at the
  pinned commit. The file-by-file manifest is
  [`issue-82-local-d764-build-context-manifest.json`](issue-82-local-d764-build-context-manifest.json),
  canonical Git-blob-byte SHA-256
  `f2e2c898f143c7f8d13598ee4c2cabeddf4808e19fdc5b9faf5eca695ef4ad46`
  (Git blob `8bf6946aa9674fb370e7a93dbe2c0c11911d6d92`). The Windows
  integration checkout has CRLF bytes and hashes to
  `19a910fb77b662532eef336b3f89fef5dca2bc6bb996394dea3fad8376d48455`;
  its normalized Git content is the same manifest.
- Relevant pinned objects: calculator blob `71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`;
  policy-scope blob `9e7591ad4d506ce57c2f52031c12c125624a76f0`; Dockerfile blob
  `10ae5c560bcca9e2294fc7caae05a5b72acfb97d`; Docker ignore blob
  `a9f5f11a580d702a7cdce0e9fbe176e5bbd9266b`; lockfile blob
  `69df1ab4c04282aec47076c5437d77215f413738`, SHA-256
  `dd9801c36f86e4853079ca0a77989ce6e4149cba5401b4a574bb6f3833743efd`.
- Base image: `python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6`.

## Build and execution stages

1. A first build from the full checkout stopped at the Dockerfile's composite
   required-source and editable-policy exclusion guard. The log does not
   isolate which conjunct failed. It produced no candidate image and was a
   context setup failure.
2. The canonical context's offline build stopped during installation of
   `alpaca-py==0.44.0`: the log records PyPI DNS retries and resolution failure.
   This build used `--no-cache-dir`, so cache contents do not establish the
   failure's cause. It produced no candidate image.
3. The canonical context then built successfully with dependency network access.
   Only the resulting image was used for the two probes below. The Docker daemon
   used for this run was version `29.7.2`, Linux/amd64. The two candidate
   containers themselves ran with `network=none`.
4. The Windows-host `DockerPanelEvaluatorV5 -> LocalContainerExecutorV5` path
   executed the pinned parent and synthetic comment-only candidate. The helper
   verified the isolation profile before starting each container and recorded
   container removal/absence and workspace cleanup.

The image reference was
`pit-optimizer-v5-evaluator@sha256:5a63234fc2b3e4771b0fccce6cd87fafb663cf343dc8d5efc2f61433c0276ea6`.
This is the OCI index/manifest digest (`image_digest` in the evidence). The
separate image configuration digest is
`sha256:e275320f8637e90d0a60ef3fc563f5596d29c4a11d59f1e0edcb381471cc33ad`
(`image_config_id`). The image inspect record reports Linux/amd64 and the
expected source-map label.

| Probe | Input identities | Request SHA-256 | Command SHA-256 | Runtime argv SHA-256 | Output SHA-256 |
| --- | --- | --- | --- | --- | --- |
| Parent | Policy `76f67d16b97d91f99d757fdbfc513f64c2b0d8f6cb09f57a45e14ff67ac217f9`; source bundle `109a61992ac79317446861c4cb2053f2704239eb1f8eacfe2b2d27ca2cf7456e` | `ae087f482bfb8f48ab6b4faefc8db1440bb709d43b24d592d0541f03afc5eecc` | `0c894ce29f26cc0f49eefe5e50eab6be4a7cd43e575926dc2b62b32e4cdda219` | `dc6c86a01bb7ab74695ffc35e24db33aa09d8137418dcb5518e5f4ea1e60cfbe` | `65a102e60c8907c65ac7b3cfefd6bf76cc303e1e98b9ba892d2bb67875751456` |
| Comment-only candidate | Policy `6d3008e8eaba6153b84cdba4c916df184eae06d5b7dbab2a872bced0595ed606`; source bundle `7c033b6436ece98b9b0bc0b39b578ac0067e332dfde35ef791e59ea7c3ab3bc4` | `fff9250e3d3ebd0d78b72685f1d25fdb4e7138da53e92f594d80041d123239e1` | `973086176684cf3156040a80afee30b0993fadb211cfa14de5b90755e3ea2724` | `e91843f890fcc90aecafff870a0b6b8797e43dc22a7dbdde3452d9ab86b7a21e` | `d0b98aed89d92bebd10cd415bc3e3a9e2474e8637c06064701a047aaf75220e9` |

Both `SemanticFingerprintV5` executions used suite `pit-policy-v3-probes-v1`,
completed all 11 observations with exit code 0, and produced the same fingerprint:
`671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`. Each
output was 3,584 bytes. The candidate fixture appends one comment to
`core/strategy_policy/v3/exit.py`; the evidence marks comment-only semantics as
unchanged.

The verified runtime profile was network disabled, pull never, read-only root,
all Linux capabilities dropped, no-new-privileges, UID/GID `65532:65532`, one
CPU, 1024 MiB memory and swap, and 32 PIDs. Each run had four read-only policy
source mounts, a bounded 64 MiB output mount, and no scenario-data mount.
Both evaluator cleanups completed; targeted absence checks found neither of
the two owned run containers, and both workspace-cleanup flags are true.

## Retained local artifacts and limits

The raw files remain in the local Codex visualization output directory
`issue82-local-d764-20261001/`. Their SHA-256 values are:

- `evidence.json`: `33f331ece8a7b1e2b562860a0edf8a90ae9a0f1eb4cdecdaa6f7d80c35cbad56`
- `probe.log`: `4f49774aa112a6361e15cc91f5be50688ba8beb804c33c5bdea5c2c6efcae96b`
- `docker-image-inspect.json`: `a7d6d042a1d948720be1ef63da88206edb5c5af3210795780d4d02cc5296cd37`
- Successful networked canonical build log:
  `61dfd8c6c68294dcabb6517e00fef9fb6026aac158b74a544f48ac6359b43fce`
- Full-checkout context setup build log:
  `373f426643451f4078f0dcb5cde0e23d0a7cde9c2dd3a15b9ebf0f3054961f9d`
- Offline canonical build log:
  `12df4a8fbaa7c29838af861895635ffc9d319c944d0d3281b36c430ab09a7d1d`

This is a fixed synthetic 11-probe comparison, not a full behavior-equivalence
claim or production evaluation. It used no production PIT data, model/provider,
broker, or paper-trading state. It is local Docker evidence, not evidence from a
hosted GitHub Actions run; the runner package remains unpublished and
undispatched.
