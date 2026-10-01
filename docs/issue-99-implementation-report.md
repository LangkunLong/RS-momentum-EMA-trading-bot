# Issue #99 Implementation Report

**Report date:** 2026-10-01
**Branch:** `codex/issue-99-account-reconciliation`
**Original review baseline:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
**Pure producer checkpoints:** `446d2e1ac906db9b09db8311aa207ddae4d861db` and approved descendant `aef51d0d4893db1049401bc37ea40f434ea60b56`
**Persistence construction dependency:** `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2`

## Statuses

- **Implementation:** The consumer adapter preserves action origin clocks, attempt terminal states and aliases, resolution reasons, and action/holding versions. Reconciliation accepts older issued actions when they match current account facts, and rejects future or stale unsent actions. No producer-owned files were changed by #99.
- **Required inputs:** A focused integration test writes real #100 DTOs into a temporary SQLite store, then obtains generation-A actions and holdings together with generation-B portfolio facts through `load_policy_execution_snapshot`.
- **Acceptance evidence:** Focused consumer regressions pass, including the canonical temp-store case. The old broad non-integration run was interrupted at about 14% after reporting failures and is not acceptance evidence. Persistence producer acceptance and independent review remain open.
- **Dependencies:** The pure producer checkpoint is approved and integrated as a construction dependency. Persistence commit `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2` is also integrated as a construction dependency, but is not independently accepted; its lead has seven Important review fixes underway.

## Changed paths owned by #99

- `core/strategy_policy/account_reconciliation.py`
- `tests/test_strategy_policy_account_reconciliation.py`
- `docs/strategy-policy-account-reconciliation-issue99.md`
- `docs/issue-99-implementation-report.md`

The following are inherited from the #100 dependency commit and are not #99-owned changes:

- `core/policy_execution_state.py`
- `tests/test_policy_execution_state.py`
- `docs/issue-100-state-interface-v1.md`

## Acceptance mapping

- Reconciles account facts to stable security identity, deployment/store/account identity,
  session clock, broker positions, logical actions, attempt-scoped references, and protective
  orders.
- Preserves the completed-session feature cutoff separately from next-opportunity account
  valuation time.
- Reserves residual buy cash and canonical residual committed risk once; confirmed fills
  remain represented in broker cash/positions and are not subtracted a second time.
- Reports pending strategy sells separately from protective sells and current position risk.
- Blocks readiness for unresolved or conflicting actions/orders, missing mappings, stale
  marks, unknown risk bases, missing classifications, or incomplete account snapshots.
- Builds `PortfolioFeaturesV3` only after complete reconciliation.

The focused test file includes the synthetic records and exercises restart idempotency,
partial fills, duplicate/conflicting references, explicit symbol mapping, Decimal-to-V3
conversion, per-attempt references, conservative statuses, protective sells, time-domain
separation, and missing-data behavior. The source fixture test uses the committed #100 DTO
classes directly.

## Current consumer continuation

The branch inherits pure producer commit `446d2e1ac906db9b09db8311aa207ddae4d861db` and its approved descendant `aef51d0d4893db1049401bc37ea40f434ea60b56`. The approved pure checkpoint is a construction dependency only; it is not an acceptance decision for persistence or this consumer integration.

The branch also includes persistence commit `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2` for construction. That commit has not passed independent review; seven Important fixes remain with the producer lead. The consumer change does not modify producer store/state files.

The focused temporary-store test uses `PolicyExecutionStateStore.load_policy_execution_snapshot` as the source of both generations' records. It confirms that a generation-A holding and partially filled scale-out action, with provider-scoped aliases and its original action clock, are returned in one read alongside generation-B's current portfolio snapshot. Current synthetic broker positions, balances, and protective-stop facts then reconcile successfully against that returned data.

The consumer conversion tests cover preservation of explicit resolution reasons, per-attempt aliases and terminal status, and state versions; readiness after a resolved terminal action; release of its reservations; acceptance of a matching older submitted action; and fail-closed outcomes for future/inconsistent provenance and expired unsent actions. The historical 35-test receipt above remains evidence from `e12a8a7` only.

Focused verification after the persistence integration:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_keeps_order_references_per_attempt tests/test_strategy_policy_account_reconciliation.py::test_matching_old_generation_holding_action_is_valid_under_new_active_generation tests/test_strategy_policy_account_reconciliation.py::test_duplicate_broker_rows_with_conflicting_alias_pairs_block_reservations tests/test_strategy_policy_account_reconciliation.py::test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio -q
```

Result: **6 passed**. Ruff passed for the reconciliation module and its focused test file; `compileall` passed for the reconciliation module; `git diff --check` reported no whitespace errors. No broad suite was run for this continuation.

SHA-256 evidence hashes (the synthetic records are inline in the test source):

| Artifact | SHA-256 |
| --- | --- |
| `core/strategy_policy/account_reconciliation.py` | `7b1530fe0aea8f5a030675d5c59515f0734388a7d87ccc0c6d4987fa5b416ea6` |
| `tests/test_strategy_policy_account_reconciliation.py` | `503ee5d7718268014b2f078f98576be1592ceee3d826455759f51914d5505e14` |

## Prior verification record (`e12a8a7`, report `f89172d`)

The following 35-test receipt is from commit `e12a8a7`, before the independent-review fixes committed as `f9575a5`. It is historical evidence for that parent only; it was not rerun on `f9575a5`. The focused review-finding regressions for `f9575a5` are recorded below.

Command:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning tests/test_strategy_policy_account_reconciliation.py tests/test_policy_execution_state.py -q
```

Result: **35 passed**. The command overrides repository coverage options and disables the
cache plugin so the focused run stays within the scoped test boundary.

```text
python -m ruff check core\strategy_policy\account_reconciliation.py tests\test_strategy_policy_account_reconciliation.py
```

Result: **All checks passed**.

```text
python -m pytest -c .pytest-issue99.ini -p no:cacheprovider -o addopts='' -m "not integration" -q
```

This broader run was stopped at approximately 14% under the owner's instruction after
failing tests appeared. The quiet partial output did not identify test names or fixtures.
I cannot confirm whether an unmarked provider, broker, or store fixture was entered, so this
run is reported as **interrupted with failures observed**, not as a pass or as proof that no
such fixture ran. The scratch `.pytest-issue99.ini` contained `[pytest]` and
`testpaths = tests`; it was removed after the run. No provider or broker was called directly
by the implementation.

## Independent review follow-up (`f89172d`)

The four Important findings in the independent review were addressed in the #99 reconciliation boundary:

1. Duplicate broker rows with a shared ID but contradictory broker/client aliases are retained as conflicting facts. Every supplied alias must resolve to the same owner, and a canonical attempt must match every supplied ID.
2. Addition, scale-out, and close actions must join to exactly one consistent holding episode by ID, security, and deployment generation. Their generation is checked against the holding episode, so an older but internally consistent generation remains valid while the active generation has advanced.
3. The adapter retains canonical equity, cash, gross exposure, open risk, and peak equity. Reconciliation compares each with the same-snapshot broker or recomputed fact and emits an `absent` finding when either side is unknown.
4. An unavailable security-symbol mapping now produces an unready reconciliation result for matched buy and sell orders without raising an exception.

Focused regression command and result:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity tests/test_strategy_policy_account_reconciliation.py::test_duplicate_broker_rows_with_conflicting_alias_pairs_block_reservations tests/test_strategy_policy_account_reconciliation.py::test_unavailable_security_mapping_with_matched_order_returns_unready tests/test_strategy_policy_account_reconciliation.py::test_holding_specific_action_requires_a_matching_holding_episode tests/test_strategy_policy_account_reconciliation.py::test_holding_specific_action_security_must_match_its_episode tests/test_strategy_policy_account_reconciliation.py::test_holding_specific_action_generation_must_match_its_episode tests/test_strategy_policy_account_reconciliation.py::test_matching_old_generation_holding_action_is_valid_under_new_active_generation tests/test_strategy_policy_account_reconciliation.py::test_duplicate_local_and_broker_references_do_not_double_reserve tests/test_strategy_policy_account_reconciliation.py::test_partial_sell_restart_reconciles_position_and_keeps_sell_pending_visible -q
```

Result: **11 passed**. Ruff passed on the reconciliation module and its focused test file; `compileall` passed for the reconciliation module. No producer or #100 files were changed for this follow-up.

Three adjacent offline regression controls also passed (**3 passed**): `test_policy_execution_state_conversion_keeps_order_references_per_attempt`, `test_conflicting_duplicate_broker_order_reference_blocks_reservations`, and `test_missing_security_mapping_never_assumes_security_id_is_a_ticker`.

## Known dependency and precision limits

- Persistence checkpoint acceptance remains open until the producer lead's seven Important review findings are fixed and independently re-reviewed.
- Combined restart acceptance with the durable store, live account facts, and deployment workflow remains a separate gate; the current temporary-store test is synthetic and consumer-focused.
- This work does not establish real provider/broker behavior or runtime deployment acceptance.
- Decimal source values are normalized to finite floats at the existing V3 feature boundary;
  this follows the current `PortfolioFeaturesV3` numeric contract.
