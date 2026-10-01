# Issue #100 implementation report

## Source checkpoint

- Base: `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
- Policy-state source head: `2881118393ab99bf02037230cfe76d4160f69bae`
- Review range: `ab385d792e19ff6db39d87f1123f47f660fc1e1d..2881118393ab99bf02037230cfe76d4160f69bae`
- Full binary patch SHA-256 for that range: `c4fc129e8de89da10d3c50e8a78147c7dd551ab77075e6a6eb3276accb27ec2f`
- Commits in the Issue #100 range: `6bbf20a`, `cd16eca`, `65472df`, `243cf59`, `2557184`, `446d2e1`, `aef51d0`, `704cdb8`, `d52fb22`, `1847869`, `67ad856`, `c2b6111`, `b786a08`, `963a61f`, `d8e383e`, `43a0820`, `bfcaa5a`, `2881118`.

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
Binding late order references to a formerly finalized holding action reopens
the action and restores its pending conflict on the holding in the same
transaction, recording a new holding version and history event while retaining
any other pending action IDs.

`REPLACEMENT` is an opening buy role. Its logical action identity stays fixed
when the first confirmed fill creates and attaches its holding. First partial
and full fills create a generation/security/action-pinned holding; later
cumulative fills continue that opening quantity, cost basis and committed risk
without counting as an addition. Residual cash/risk and pending state remain
on the action, partial replacement exposure can receive protective-stop
updates, and unrelated existing holdings cannot be attached to the action.

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
| Fill receipts, cumulative quantities, accounting, history and idempotency | Temporary database tests verify atomic rollback on injected action-write failure, opening holding creation/continuation, same-event replay, conflicting payload rejection and same-watermark zero-delta behavior. Replacement tests cover first partial/full fills, a later cumulative fill, residual cash/risk, stable action/holding IDs, cost basis, committed risk, canonical restart reads, exact receipt replay/conflict, stale action/holding CAS, and unrelated-holding rejection. An explicit test confirms known monetary facts at an unchanged quantity watermark remain receipt-only. |
| Issued-order uncertainty, remainders, late fills, aliases and explicit resolution | Pure transition tests cover cancel/terminal/remainder gates, live remainder fills, late terminal fills, above-target reconciliation, unissued resolution and cross-attempt alias rejection. Store tests cover late references retaining uncertainty, completed-tier late-fill resolution, provider-scoped alias ancestry/version/history, and issued versus unissued resolution. |
| Versioned stop proposal and broker confirmation | Temporary database test replays a proposal using its old holding version, rejects changed proposal facts, persists a better confirmed stop price, and replays confirmation idempotently. Partially filled entries and replacements can coexist with stop protection under the constrained helper path. The replacement lifecycle also rejects stale holding versions before accepting protection against the reloaded quantity; lead's scoped review of finding 6 covered entry protection only. |
| Combined snapshot with generation-pinned ancestry | Temporary database test loads a generation B portfolio alongside generation A's holding/action, checks both row versions and the original action clock, omits an unrelated terminal action, then updates the holding through a fresh store instance using the returned version. The snapshot API docstrings now describe the account/store scope. |
| Runtime containment | Tests create databases only under pytest `tmp_path`; the fixture seeds a synthetic legacy workflow row. No broker/provider, scheduler, runtime, configured database, or operational migration is invoked. `core/execution_store.py` and `core/execution_workflow.py` are unchanged. |

### Independent-review corrections

| Finding | Current disposition |
| --- | --- |
| Addition fills must preserve unknown aggregate risk | Corrected in `c2b6111`; lead's scoped independent re-review approved finding 1 with no new Critical/Important regression. |
| Late reference binding must preserve action uncertainty and holding conflicts | Corrected in `b786a08` and `43a0820`; finalized holding actions reopen with the reconciliation action restored to the holding in one transaction. The new resolved-addition test preserves another pending ID, blocks a competing action, records terminal evidence and explicit resolution, then checks restart state. An earlier report left re-review pending; the principal's later full review lists C-R1 below as the sole remaining source finding. |
| Public holding writer must not change confirmed quantity or protection state | Corrected in `963a61f`; the independent review approved this finding. The writer accepts only evidence-backed reconciliation-flag changes and compares immutable opening quantity; focused store tests cover bypass rejection and history evidence. |
| Late fill after a completed scale-out must remain explicitly resolvable | Corrected in `963a61f`; the independent review approved this finding. Extra fills register a pending reconciliation action, explicit resolution clears it without advancing the tier, and restart reads preserve the result. |
| Provider-scoped aliases must remain readable, auditable and versioned | Corrected in `963a61f`; the independent review approved this finding. Duplicate external IDs across providers appear once in flattened aliases, retain each scoped record in projections/history, and increment state version. |
| Partial-entry stop protection must coexist with the pending entry | Corrected in `67ad856`; the independent review approved finding 6. Lead reports all five expanded combined-chain cases passed at source map `549f8d3` (`963a61f` plus consumer `2cf4bdf`), including alias restart. This run predates the final `43a0820` correction. |
| Active pointer CAS must reject an A → B → A stale writer | Corrected in `963a61f`; the independent review approved this finding. Pointer load exposes version, writes compare generation and version, and the ABA test rejects A/version-1 against A/version-3. |
| C-R1: replacement opening fills were not persisted or linked to a holding | Corrected in `2881118`. Replacement keeps its pre-fill action ID as the holding attaches, creates a holding on the first positive partial or full fill, applies later opening quantity/cost/risk, retains residual reservations and pending protection, and appears in canonical restart reads. Pure and temporary-store tests cover these cases and reject unrelated holding attachment. The principal classified this finding Important/P2 in the frozen review `2026-10-01-lead-c-ea2e145-independent-review.md` (SHA-256 `a301f76a217f8ff970513d6ea7429c4386c14656bf0450a464a1ef3f287d6c49`); re-review of this correction and the combined chain remain pending. |

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
| `core/policy_execution_state.py` | `0aab4b15cd5418dbefab4892d941ac8317afe00abcac6e846c8bf853dff90f4c` |
| `core/policy_execution_store.py` | `1f71dc3d45d1e6b447b2f88c4d1d17df6817ce78d54c6f7d8ec9988dfcd6819d` |
| `tests/test_policy_execution_state.py` | `7dd06766131ac98cb02aa363da556a8abaa1eaf2abf2f811cbb6d3e29bdeafb4` |
| `tests/test_policy_execution_store.py` | `cdee3e3a771fde1b1945f35b9977579a3620e320351a02d4b9e264c8b546d449` |
| `docs/issue-100-state-interface-v1.md` | `8b647d16da4097878b5be22dc9cc4798229b0f3526adcf5fc13d57e019f7bf0d` |

## Verification

The earlier source checkpoint `43a0820dea9688fd8583f45ccb9704f172553954`
had 43 focused tests passing and a reported patch SHA-256 of
`4a4cfc9fbaf8e541a54c14453e1ec719efbd7ad8b570cadde859d49b23ce4734`. That
checkpoint predates replacement-fill coverage and is retained as historical
evidence only. The replacement TDD baseline failed on all five selected cases
before the correction: identity changed after attachment, partial replacement
protection was rejected, and both first partial/full replacement fills failed
to create holdings. The unrelated-holding fixture first hit a conflicting
decision slot before reaching the store guard; its corrected fixture passes
in the final focused run.

Commands run at source head `2881118393ab99bf02037230cfe76d4160f69bae`:

```text
py -3.13 -m pytest -p no:cacheprovider -o addopts='' tests/test_policy_execution_state.py tests/test_policy_execution_store.py
48 passed

py -3.13 -m ruff check core/policy_execution_state.py core/policy_execution_store.py tests/test_policy_execution_state.py tests/test_policy_execution_store.py
All checks passed

git diff --check
Passed

Commit hooks at `2881118`
ruff, trailing whitespace, end-of-file and merge-conflict checks passed
```

Pytest emits one configuration warning because disabling the cache plugin also
disables the repository's configured `cache_dir` option. Git also warns that
the five changed source/test/interface files use LF in the worktree and will
be converted to CRLF on a future Git touch. No broader test suite or combined
#99/#100 chain was run.

## Issue assessment

- **Implementation:** The seven earlier producer-side findings have code
  fixes through `43a0820`. The principal's later full review names C-R1 below
  as the sole remaining producer-side source finding. Its replacement
  correction is at `2881118` and awaits principal re-review.
- **Required inputs:** Synthetic identities, fixed decisions, and explicit
  temporary SQLite databases were available. Lead reports the five-case
  combined chain passed at the earlier `963a61f` producer / `2cf4bdf` consumer
  source map, including alias restart.
- **Acceptance evidence:** The two focused producer modules pass 48 tests,
  including replacement first partial/full fills, cumulative continuation,
  residual cash/risk, protection, receipt replay/conflict and canonical
  restart selection. Ruff, diff check, and source commit hooks pass. The new
  combined #99/#100 chain has not been rerun at `2881118`.
- **Dependencies:** Principal review of the C-R1 correction and a combined
  #99/#100 rerun at `2881118` remain outstanding. No overall issue acceptance or #97 runtime
  activation/readiness acceptance is claimed.

## Remaining integration boundary

This is the producer-side additive state contract. Adoption by the #99 consumer
and any #97 runtime activation/readiness integration require their own review
and acceptance. This change does not enable live execution, broker/provider
calls, scheduler wiring, or an operational database migration. No external
downstream acceptance is recorded by this report.

## Erratum for source checkpoint `2881118`

The `docs/issue-100-state-interface-v1.md` digest in the original report was
listed as `79c8244200f25912d0ae179e7f521514f1f846976c90558559a60d991813b3fc`.
Directly hashing the raw Git blob from `2881118` with `git show` gives
`8b647d16da4097878b5be22dc9cc4798229b0f3526adcf5fc13d57e019f7bf0d`; the
content-digest table above is corrected to that value. This erratum changes
only the report and preserves the source checkpoint and its verification
record.
