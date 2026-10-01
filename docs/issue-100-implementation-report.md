# Issue #100 implementation report

## Source checkpoint

- Base: `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
- Policy-state source head: `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2`
- Review range: `ab385d792e19ff6db39d87f1123f47f660fc1e1d..d52fb22deedc74361f6e4ae4a0113dc4f215c3c2`
- Commits in the Issue #100 range: `6bbf20a`, `cd16eca`, `65472df`, `243cf59`, `2557184`, `446d2e1`, `aef51d0`, `704cdb8`, `d52fb22`.

The report is a docs-only follow-up to the source head above. It does not
change the source range or the checksums below.

## Delivered interface

`core/policy_execution_state.py` defines provider-independent deployment,
decision clock, action, attempt, holding, portfolio, stop-update and #99
projection types. It retains logical action identity and immutable target
facts through partial fills, terminal observations, late fills, cancel
requests, bounded remainders and explicit reconciliation. An unissued intent
with no references or fills can be resolved with a reason; an issued attempt
must have terminal evidence before resolution. Replaying terminal evidence
does not clear a later reconciliation-required state. Order aliases cannot
identify another attempt within one logical action. Stored projections carry
action and holding row versions for compare-and-set calls.

`core/policy_execution_store.py` is an independent SQLite store. Callers must
provide both `db_path` and `store_identity`; it reads no configured/default
database path and has no dependency on the existing execution workflow. It
uses a checksum-bound schema version 1, additive transactional DDL, explicit
database identity binding, scoped foreign keys with `ON DELETE RESTRICT`, and
`BEGIN IMMEDIATE` for writes. Migration failure rolls back all policy tables;
schema rollback is allowed only while policy tables are empty.

The public combined consumer read is:

```python
PolicyExecutionStateStore.load_policy_execution_snapshot(
    *, deployment_generation_id: str, portfolio_snapshot_id: str
) -> PolicyExecutionReadSnapshot
```

The requested generation must own the portfolio snapshot. One read transaction
returns its deployment identity and portfolio facts, all same-account/store
holding episodes across generations, all open actions across those generations,
and terminal actions linked to a returned holding through the holding ID,
opening action, pending IDs, fill watermarks or completed addition IDs.
Unrelated terminal actions without a holding link are omitted. Per-action
generation, original decision clock and order references remain attached.
Every persisted holding and action includes `state_version`, so a restarted
consumer can use the returned values with the public CAS writers. The mixed
generation fixture confirms an older generation's holding and action remain
visible alongside the newer generation's portfolio.

## Acceptance criteria and evidence

| Area | Evidence |
| --- | --- |
| Identity, decision clocks, decision-slot conflict handling and stable action identity | Pure state suite; explicit deployment/account/store identity and clock values; durable decision and tier uniqueness tests. |
| Additive schema, exact-path isolation, rollback and legacy preservation | Temporary database tests compare representative legacy table schemas and rows before/after migration and rollback; injected intermediate DDL failure leaves no policy tables; rollback refuses nonempty policy state. |
| Fill receipts, cumulative quantities, accounting, history and idempotency | Temporary database tests verify atomic rollback on injected action-write failure, opening holding creation/continuation, same-event replay, conflicting payload rejection and same-watermark zero-delta behavior. |
| Issued-order uncertainty, remainders, late fills, aliases and explicit resolution | Pure transition tests cover cancel/terminal/remainder gates, live remainder fills, late terminal fills, above-target reconciliation, unissued resolution and cross-attempt alias rejection. Store tests cover issued versus unissued resolution and provider-scoped alias persistence/reload. |
| Versioned stop proposal and broker confirmation | Temporary database test replays a proposal using its old holding version, rejects changed proposal facts, persists a better confirmed stop price, and replays confirmation idempotently. |
| Combined snapshot with generation-pinned ancestry | Temporary database test loads a generation B portfolio alongside generation A's holding/action, checks both row versions and the original action clock, omits an unrelated terminal action, then updates the holding through a fresh store instance using the returned version. |
| Runtime containment | Tests create databases only under pytest `tmp_path`; the fixture seeds a synthetic legacy workflow row. No broker/provider, scheduler, runtime, configured database, or operational migration is invoked. `core/execution_store.py` and `core/execution_workflow.py` are unchanged. |

## Synthetic fixture identities and digests

All values below are fixed synthetic test identities, not deployment
credentials or live account data.

| Fixture item | Value |
| --- | --- |
| Store identity | `synthetic-policy-store-v1` |
| Account environment | `synthetic-paper-account` |
| Database namespace ID | `db-namespace:sha256:46e8177cbb49354327001553f248d2a566ec21e9bb0885ef40d2cadab8dba578` |
| Generation A ID | `deployment:sha256:8a21a6ac53dd451ea49133bec9ec73ee091f712ac932cd75bb216c15148157c0` |
| Generation B ID | `deployment:sha256:9457d3f04f79b231c60d0ad59e120956492aa3f8c3632a687a52075afdfc9d47` |
| Historical decision ID | `decision:sha256:b68d9ad1d2c3e40408f5b6e3fd1ea445ba0778a4b4a815dd7dcdbf91ead6eeef` |
| Historical action ID | `action:sha256:0cc49571be7e5d7f68c8a68dad8a59869c2a3538b2a85f2d1f530fabee25bc2b` |
| Historical holding ID | `holding:sha256:39cdb4fa261ed6cc47b45a74d3aef60d93c93b5bcbd134d13545b6ab0dfc622d` |
| Portfolio source namespace / account snapshot ID | `synthetic-account-source` / `snapshot-generation-b` |
| Portfolio snapshot ID | `portfolio:sha256:b1c899f05f7907e5895b8745547bd7f3533c0c9193fe8fdec00e9a81ebc59245` |
| Historical policy-visible snapshot SHA-256 | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |
| Schema v1 migration SHA-256 | `09301def3abe2391194b0c03adbef7e05b6cfad2e5a38b8dabbf54f835bee67e` |

Content digests at the source head:

| File | SHA-256 |
| --- | --- |
| `core/policy_execution_state.py` | `fb96fdfe86709508b0c348538a3b6add5ad7b09092e8ca516dfd7036dc6a0b67` |
| `core/policy_execution_store.py` | `d385e0c85e79ded5fcb209a908ba212969fc7e963820aa5c2a931148ee11ddbd` |
| `tests/test_policy_execution_state.py` | `744966f0b5e34c8dad154b7a04de435f8875a00dcd18f155a363f8062ea26d39` |
| `tests/test_policy_execution_store.py` | `ecf347b4121b2a0eddd709106dc4c8b3a985441016883d42f6e2b3c574be53d7` |

## Verification

Commands run at the source head:

```text
py -3.13 -m pytest -p no:cacheprovider -o addopts='' tests/test_policy_execution_state.py tests/test_policy_execution_store.py
31 passed

py -3.13 -m ruff check core/policy_execution_state.py core/policy_execution_store.py tests/test_policy_execution_state.py tests/test_policy_execution_store.py
All checks passed

py -3.13 -m compileall -q core/policy_execution_state.py core/policy_execution_store.py tests/test_policy_execution_state.py tests/test_policy_execution_store.py
Passed
```

Pytest emits one configuration warning because disabling the cache plugin also
disables the repository's configured `cache_dir` option. No broader test suite
was run.

## Remaining integration boundary

This is the producer-side additive state contract. Adoption by the #99 consumer
and any #97 runtime activation/readiness integration require their own review
and acceptance. This change does not enable live execution, broker/provider
calls, scheduler wiring, or an operational database migration. No external
review or downstream acceptance is recorded by this report.
