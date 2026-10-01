# Issue #100 implementation report

## Source checkpoint

- Base: `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
- Policy-state source head: `963a61f1dbc0641d50a4272e845447bf4e40abfa`
- Review range: `ab385d792e19ff6db39d87f1123f47f660fc1e1d..963a61f1dbc0641d50a4272e845447bf4e40abfa`
- Full binary patch SHA-256 for that range: `8c593463d1b9686a4e8172d93c358441339a8f35ffb7a2ee3eaeb036c549d30b`
- Commits in the Issue #100 range: `6bbf20a`, `cd16eca`, `65472df`, `243cf59`, `2557184`, `446d2e1`, `aef51d0`, `704cdb8`, `d52fb22`, `67ad856`, `c2b6111`, `b786a08`, `963a61f`.

This report is a docs-only follow-up to the source head above. It does not
change the source range or the source checksums below.

## Delivered interface

`core/policy_execution_state.py` defines provider-independent deployment,
decision clock, action, attempt, holding, portfolio, stop-update and #99
projection types. It retains logical action identity and immutable target
facts through partial fills, terminal observations, late fills, cancel
requests, bounded remainders and explicit reconciliation. An unissued intent
with no references or fills can be resolved with a reason; an issued attempt
must have terminal evidence before resolution. Replaying terminal evidence
does not clear a later reconciliation-required state. Late fills after a
completed scale-out stay pending for reconciliation, and resolving one does
not advance its already-completed tier again. Provider-scoped order-reference
ancestry stays in the projection and action history; flattened attempt aliases
deduplicate the same external value across providers. Stored projections carry
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

The active-generation write compares both the expected generation and pointer
version, so an A → B → A transition rejects a stale A/version-1 writer.
`record_holding_episode` is restricted to evidence-backed changes of the
position-reconciliation flag on an existing fill-created holding; it cannot
change opening quantity or protection state. A same-quantity fill observation
can add a receipt but cannot refine previously unknown notional or fees; that
limitation is explicit in the interface contract.

## Acceptance criteria and evidence

| Area | Evidence |
| --- | --- |
| Identity, decision clocks, decision-slot conflict handling and stable action identity | Pure state suite; explicit deployment/account/store identity and clock values; durable decision and tier uniqueness tests. |
| Additive schema, exact-path isolation, rollback and legacy preservation | Temporary database tests compare representative legacy table schemas and rows before/after migration and rollback; injected intermediate DDL failure leaves no policy tables; rollback refuses nonempty policy state. Holding flag reconciliation evidence is included in history. |
| Fill receipts, cumulative quantities, accounting, history and idempotency | Temporary database tests verify atomic rollback on injected action-write failure, opening holding creation/continuation, same-event replay, conflicting payload rejection and same-watermark zero-delta behavior. An explicit test confirms known monetary facts at an unchanged quantity watermark remain receipt-only. |
| Issued-order uncertainty, remainders, late fills, aliases and explicit resolution | Pure transition tests cover cancel/terminal/remainder gates, live remainder fills, late terminal fills, above-target reconciliation, unissued resolution and cross-attempt alias rejection. Store tests cover late references retaining uncertainty, completed-tier late-fill resolution, provider-scoped alias ancestry/version/history, and issued versus unissued resolution. |
| Versioned stop proposal and broker confirmation | Temporary database test replays a proposal using its old holding version, rejects changed proposal facts, persists a better confirmed stop price, and replays confirmation idempotently. A partially filled entry can coexist with stop protection under the constrained helper path; lead's scoped review of finding 6 approved it. |
| Combined snapshot with generation-pinned ancestry | Temporary database test loads a generation B portfolio alongside generation A's holding/action, checks both row versions and the original action clock, omits an unrelated terminal action, then updates the holding through a fresh store instance using the returned version. The snapshot API docstrings now describe the account/store scope. |
| Runtime containment | Tests create databases only under pytest `tmp_path`; the fixture seeds a synthetic legacy workflow row. No broker/provider, scheduler, runtime, configured database, or operational migration is invoked. `core/execution_store.py` and `core/execution_workflow.py` are unchanged. |

### Independent-review corrections

| Finding | Current disposition |
| --- | --- |
| Addition fills must preserve unknown aggregate risk | Corrected in `c2b6111`; lead's scoped independent re-review approved finding 1 with no new Critical/Important regression. |
| Late reference binding must preserve action uncertainty | Corrected in `b786a08`; covered by late-reference and remainder-fill persistence cases. Independent re-review of this correction is pending. |
| Public holding writer must not change confirmed quantity or protection state | Corrected in `963a61f`; writer now accepts only evidence-backed reconciliation-flag changes and compares immutable opening quantity; two focused store tests cover bypass rejection and history evidence. Independent re-review is pending. |
| Late fill after a completed scale-out must remain explicitly resolvable | Corrected in `963a61f`; extra fill registers a pending reconciliation action, explicit resolution clears it without advancing the tier, and restart reads preserve the result. Independent re-review is pending. |
| Provider-scoped aliases must remain readable, auditable and versioned | Corrected in `963a61f`; same external ID from multiple providers appears once in flattened attempt aliases, retains each scoped record in projections/history, and increments state version. Independent re-review is pending. |
| Partial-entry stop protection must coexist with the pending entry | Corrected in `67ad856`; lead's scoped independent re-review approved finding 6 with no new Critical/Important regression. The integrated combined-chain fixture also passed its two earlier cases; a separate later-session case depends on the #99 clock correction. |
| Active pointer CAS must reject an A → B → A stale writer | Corrected in `963a61f`; pointer load exposes version, writes compare generation and version, and the dedicated ABA test rejects A/version-1 against A/version-3. Independent re-review is pending. |

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
| Schema v1 migration SHA-256 | `b1eead321b213c54e3eca905b603cf00d7237d9b2d84f74085dda909b91b7190` |

Content digests at the source head:

| File | SHA-256 |
| --- | --- |
| `core/policy_execution_state.py` | `739a8a1b1fcc0000d0db027bcf813dddfed069693160cc66a367bf6fc7913032` |
| `core/policy_execution_store.py` | `3191d12289345666e0ef5c051164588f80060b663d85081150b0615f0f7d3354` |
| `tests/test_policy_execution_state.py` | `1f3129eb146a4419eea9fb6393d05a817361b034ce5a820763d5d944d1a74d16` |
| `tests/test_policy_execution_store.py` | `f7eeb9214be0860f63afceb2427d040e89a7677c818db00440887d0f30f53c8a` |
| `docs/issue-100-state-interface-v1.md` | `e8ba0892d275dfaa9a80a037d936217e0956c496525b4c82d09207fb4eadd1b7` |

## Verification

Commands run at the source head `963a61f1dbc0641d50a4272e845447bf4e40abfa`:

```text
py -3.13 -m pytest -p no:cacheprovider -o addopts='' tests/test_policy_execution_state.py tests/test_policy_execution_store.py
42 passed

py -3.13 -m ruff check core/policy_execution_state.py core/policy_execution_store.py tests/test_policy_execution_state.py tests/test_policy_execution_store.py
All checks passed

git diff --check
Passed

Commit hooks at `963a61f`
ruff, trailing whitespace, end-of-file and merge-conflict checks passed
```

Pytest emits one configuration warning because disabling the cache plugin also
disables the repository's configured `cache_dir` option. Git also warns that
the four changed source/test/docs files use LF in the worktree and will be
converted to CRLF on a future Git touch. No broader test suite was run.

## Issue assessment

- **Implementation:** Producer-side #100 implementation and the corrections in
  findings 2–5 and 7 are integrated at `963a61f`; findings 1 and 6 remain in
  the integrated source and have scoped lead review approval.
- **Required inputs:** Synthetic identities, fixed decisions, and explicit
  temporary SQLite databases were available. The combined later-session
  policy-chain case still needs the #99 clock correction.
- **Acceptance evidence:** The 42 focused pure-state/store tests, Ruff, diff
  check, and commit hooks pass. Scoped independent review is recorded only for
  findings 1 and 6; findings 2–5 and 7 await re-review.
- **Dependencies:** Independent review of the remaining corrections and the
  combined #99/#100 chain after its clock fix remain outstanding. No #99
  consumer acceptance or #97 runtime activation/readiness acceptance is
  claimed.

## Remaining integration boundary

This is the producer-side additive state contract. Adoption by the #99 consumer
and any #97 runtime activation/readiness integration require their own review
and acceptance. This change does not enable live execution, broker/provider
calls, scheduler wiring, or an operational database migration. No external
downstream acceptance is recorded by this report.
