# Issue #99 Implementation Report

**Report date:** 2026-10-01
**Branch:** `codex/issue-99-account-reconciliation`
**Original review baseline:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
**Producer dependency checkpoint:** `6bbf20ab2de0f177628bf623c8fa3c138679d868` (parented by the original baseline)

## Statuses

- **Implementation:** The synthetic account reconciler and direct #100 DTO conversion are implemented in this branch. No store, runtime workflow, provider, or broker code was changed.
- **Required inputs:** Available as deterministic inline test fixtures using the real #100 `ActionStateProjection`, `HoldingEpisode`, and `PortfolioStateSnapshot` types plus synthetic account facts.
- **Acceptance evidence:** The focused reconciliation regressions for the independent review findings pass; Ruff and `compileall` pass. The broad non-integration suite was interrupted at about 14% after reporting failures, so it is not acceptance evidence.
- **Dependencies:** The pure #100 producer checkpoint is integrated and tested. Follow-up #100 transition corrections and the `resolution_reason` projection field are not yet integrated; persistence/store work and combined restart validation are also outstanding.

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

SHA-256 evidence hashes (the synthetic records are inline in the test source):

| Artifact | SHA-256 |
| --- | --- |
| `core/strategy_policy/account_reconciliation.py` | `1b67c78ee2ea6b32f5f5b19721c3d91fcbd2e00651cca9281e5db9bbf465edab` |
| `tests/test_strategy_policy_account_reconciliation.py` | `5961150b5040bbdb0bc29879920a89f9ac3833cb7db6c18b28caaff9cf6ac3ac6` |

## Verification record

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

- The current `ActionStateProjection` omits the source action's `resolution_reason`. A
  resolved action therefore remains unready in the adapter. The producer-side field and
  preservation test are an assigned integration fix; readiness after resolution remains
  pending that producer update and its test against terminal broker/account facts.
- The separately reviewed #100 pure transition correction checkpoint (`cd16eca`) has not
  been propagated into this branch.
- The producer checkpoint is pure state only. Loading from durable storage and verifying
  restart behavior against that store are not yet possible in this branch.
- Decimal source values are normalized to finite floats at the existing V3 feature boundary;
  this follows the current `PortfolioFeaturesV3` numeric contract.
- Combined restart acceptance and independent review remain pending.
