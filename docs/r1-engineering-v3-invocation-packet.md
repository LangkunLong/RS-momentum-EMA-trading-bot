# R1 schema-V3 engineering invocation packet (pre-admission)

Status: composition only. No baseline or candidate evaluation has run. This
packet is not a production research or qualification authorization.

## Fixed input

The D1 fixture is at
`C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\coordination\fresh-team-2026-10-04\r1-d1-engineering-v3-bce3ac8`.
Its handoff is `r1-d1-engineering-fixture-handoff-2026-10-08.md` in the parent
coordination directory. The fixture was built from source commit
`bce3ac825a3de91081d11db7ad811a827e9f24bc` and is labeled
`synthetic_fixture_only`, with `production_source_coverage=false`.

| Input | SHA-256 |
| --- | --- |
| `data/pit_bundle.sqlite3` | `6b2156d7260353ab3be8df6a91cbc7b234a5983d7acb704aab9d25102cf24332` |
| `data/prices_provenance.json` | `d68da731115ca45c43849f01a1013a10d0ebb93e3b8543605ebf325a304511a2` |
| `fixture_manifest.json` | `a10d3098147e53cdbbe8882827f425be19357763589980ce5fd9079d4de16722` |
| Discovery panel 1 | `0697f03e823b6e41ec473b638455353f54246582cc466924b9478d38a71fa0f8` |
| Discovery panel 2 | `4083ab14be19391b525e53a9d371eb3690393379f52071c2589bd495658952d2` |
| Discovery panel 3 | `931de064cbd87653cb58baa3e29e9838217a19ca565cf54c494f935f1c5ab456` |
| Discovery panel 4 | `019350e21ac1c5e505b94669ec9cb9bc0f2a2a415478a76366e3301e147c8499` |

The four panels cover March 24–25, March 26–27, March 30–31, and April 1–2,
2026. The selection scenario is `base`; the full evaluator grid is `gross`,
`base`, `stress` with the initial V5 friction settings.

The create-only importer `core.pit_optimizer_v5.engineering_inputs` has now
materialized an isolated graph under this worktree's
`.artifacts/pit-optimizer-v5-engineering-r1`, using an explicitly configured
10.00% engineering target. The plan reference is
`panels/engineering-discovery-plan.json` SHA-256
`366ed19f4fb575b29460e61aace9ed8bd85a3a3fc228d759bf02eb7fa30a7834`.
It imports the four discovery panels byte for byte, and derives the following
two baseline panels without opening any held-out data:

| Panel | Date window | SHA-256 |
| --- | --- | --- |
| `panels/mechanics.json` | March 24–25, 2026 | `ffeb17e9dea05f723171af23ac58405af6afd1f85602053c823ae62ad70783ed` |
| `panels/quick.json` | March 26–27, 2026 | `4675018748ed61fb3ac802af9d3ab3cf36da17ad98b8edc41300109306d35ea7` |

This is an authenticated input graph, not a measured baseline. Its target is
an engineering manifest value and does not assert that the 10% research target
was reached.

## Bounded route

1. Bind the now prepared six-panel plan to a clean source snapshot, execution
   profile, resource limits, evaluator contract, and exact baseline inputs.
2. Pin a current-source V5 image and sandbox profile. The evaluator source
   closure includes the edited `contracts.py`, `container_protocol.py`,
   `evaluator.py`, and `backtest_engine.py`; the old image digest is stale.
3. Capture the unchanged baseline with `capture_baseline_v5(...,
   pit_data_scope="engineering_v3")` and
   `LocalBaselineCaptureFactoryV5(..., pit_data_scope="engineering_v3")`.
   Verify it with `verify_baseline_v5(...,
   pit_data_scope="engineering_v3")`. The default verification remains
   production. Capture requires two fresh isolated workers, six panel outputs
   per run, exact byte repeat, a semantic fingerprint, and source stability.
4. Build a manifest with `pit_data_scope="engineering_v3"`,
   `semantic_mode="required"`, `provider=None`, and finite search limits:
   two rounds, one investigator hypothesis, one variant, one survivor, and
   no full-source escape. Reopen the repository before round two.
5. Invoke `python -m core.pit_optimizer_v5.engineering_operations inspect`
   with the exact manifest and measured baseline capture references and the
   eight absolute host paths named by `--help`. This reauthenticates the
   baseline and manifest and composes the existing isolated Docker candidate
   graph. It performs no evaluation. The returned source/image/data/resource
   identities are the review input for the Principal.
6. After the Principal reviews the finite manifest, host paths, image/profile
   identity, controller proposal artifacts, source revision, and input/output
   contract, invoke the same command with `run` instead of `inspect`. This
   calls the ordinary `run_feedback_round_v5`. Its JSON output reports a
   pending role request or terminal result. When pending, author a proposal
   JSON file with exactly `role` and `artifact` fields, then call
   `python -m core.pit_optimizer_v5.engineering_controller` with the returned
   request path/digest, proposal path, artifact root, and response directory.
   That command validates the proposal against the persisted request and
   creates `<call_key_sha256>.json`; rerun the identical round command.
7. Reopen the repository in a new process for round two. Pass the actual
   persisted round-one experiment ID with `--feedback-experiment-id`. The
   driver requires round-one cleanup and a measured checkpoint record. The
   role request rederives its actual campaign CAGR and source/report identities
   even if parent selection stayed at the baseline.

The runner persists immutable campaign and per-round start times in the V5
artifact repository. Repeated controller resumes share the original wall
limits and host path identity. No response can silently reset those limits.

No provider, broker, trading action, confirmation, qualification, or replay is
authorized by this packet. The production operations and held-out builders
reject the engineering scope.

## Missing execution authorities

- Docker Desktop's Linux daemon became reachable on 2026-10-08: Docker Desktop
  4.87.0, Engine 29.7.2, Linux/amd64. The local image listing was empty.
  No image build, pull, or evaluator run followed from that status check.
- No current-source V5 evaluator image is cached. Its digest and exact sandbox
  profile must be built or otherwise admitted and inspected before any run.
- Mechanics/quick panels and their six-panel plan are published in the isolated
  engineering artifact root. Baseline input descriptors and measured baseline
  authority have not been published.
- No round-one experiment, round-two feedback, candidate metric, or Principal
  execution admission exists yet.

The previously proposed host in-process candidate worker was rejected by
automatic approval review because compiling and executing candidate source
inside the evaluator process would grant that source arbitrary host process
and filesystem effects. It was not added. Candidate source must execute only
through the existing isolated worker architecture.
