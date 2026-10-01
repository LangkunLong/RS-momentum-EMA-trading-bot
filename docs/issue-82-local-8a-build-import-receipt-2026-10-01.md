# Issue 82 integrated-image build and import smoke — partial receipt

This records completed local Docker evidence at clean integrated source
`8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`. It does not replace the
[older `d764c87` parent/candidate receipt](issue-82-local-d764-final-source-receipt-2026-10-01.md)
or establish final #82 acceptance at the changed map: **matched parent and
candidate probes have not been run at this source**.

The 57-path raw-Git evaluator source map is
`f6dee0745545308088887a924b9839efedd8f1beff1dfbfac82fd889935819f0`.
The canonical context contains those 57 files plus Dockerfile, Dockerfile
ignore file, and requirements lock: 60 files and 1,435,447 bytes. An
independent read of every manifest entry found zero mismatches against Git
blobs at the pinned commit. The retained local context manifest SHA-256 is
`a8920b301055ea9277ebd260adc2a68bb6aa688942350e42af640bd4c1ad10b3`.

Docker Engine was 29.7.2, Linux/amd64. The networked dependency build from
this context completed; candidate containers use `network=none`. The image
inspect record reports an OCI index descriptor and Docker backend image ID
`sha256:1e0e6327f48e96d4f9754c6bae47969f0d8cf57f3c2b6cb8a10ab2ece28af0df`,
with the expected runtime-source label. BuildKit separately exported a
platform image-config digest
`sha256:283a97f89d0689d5780f91784d4d944bd4ce55ff83e80df0dd50e4648cc62704`.
The two digests are distinct; the config digest was not a resolvable local
Docker image reference on this host.

The first import-smoke attempt did not create a container. A retained
diagnostic `docker container create` call using the exported config digest
returned `No such image`, exit 1, with no owned container left. These are
setup failures, not import results. The successful smoke used the unique
locally built tag with `--pull never`; the created container's recorded
`.Image` exactly matched the pre-inspected Docker backend image ID above.
It imported `core.data_client`, the new
`core.alpaca_client_policy.configure_alpaca_rest_client`, the V5 container
entry, and `core.backtest_engine.PortfolioSimulator`. Create, start, and
container exits were zero. It used `network=none`, no mounts, a read-only
root, UID/GID 65532, one CPU, 1024 MiB memory and swap, and 32 PIDs. Owned
cleanup and targeted absence checks completed. The `20261002` substring in
its unique container name is an opaque evidence identifier; this execution
occurred on 2026-10-01 UTC.

| Local artifact under `issue82-local-8a0896a-20261001/` | SHA-256 |
| --- | --- |
| `docker-build-networked.log` | `e935948a0579d933b848a8cfb69e7d8f271c9df5633c44de89c178b582c4c964` |
| `docker-image-inspect.json` | `ff89b5012bc6bf60ae4761902137fd93a62f700283d6c1f6b838874fda7d0a1d` |
| `docker-server-version.txt` | `2ec6d0108bbc2983eb7df64ef4a9eea41919d2dd630188e9fd91eb478c4f4f56` |
| `import-smoke.json` (failed setup) | `d1fdf9156cb1b78439788bc1b14b05c2213f4eaf34d38899a0dcc6e8f26d6ed6` |
| `docker-create-config-id-diagnostic.json` | `a39219cf97ab2c79034441f14cb8116a2694ece777a1ede771181db51f455456` |
| `import-smoke-success.json` | `08cb2894ab4a2d0cbd62c7d0681c1fd2858fbd23f4a079626d7f2ccbd7e7fb34` |

The raw files are retained in the local Codex visualization output directory
`C:/Users/llong/.codex/visualizations/2026/09/28/01a0e67f-c84d-74b0-b9e3-120a32043e48/issue82-local-8a0896a-20261001/`.
This is actual build/import evidence, not a hosted Actions run, parent/candidate
comparison, historical PIT evaluation, or production admission.
