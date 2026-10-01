# Issue #99 Implementation Report

**Report date:** 2026-10-01
**Branch:** `codex/issue-99-account-reconciliation`
**Original review baseline:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`
**Pure producer checkpoints:** `446d2e1ac906db9b09db8311aa207ddae4d861db` and approved descendant `aef51d0d4893db1049401bc37ea40f434ea60b56`
**Persistence construction dependency:** `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2`
**Durable-state construction correction:** `963a61f1dbc0641d50a4272e845447bf4e40abfa`
**Producer report-only disposition:** `d8e383ecf5cdae37f0a3828a056ff2d1b1e202af`
**Latest producer source correction:** `43a0820dea9688fd8583f45ccb9704f172553954`
**Latest producer review report:** `bfcaa5a8cfdcfc5e32edfd78e4dfd5bb65e5b2d5`

## Statuses

- **Implementation:** The consumer adapter preserves action origin clocks, attempt terminal states and aliases, resolution reasons, action/holding versions, and holding policy flags. Explicitly resolved attempts may be terminal or provably unissued under the producer predicate. Flat historical holdings retain confirmed stop history without requiring a live stop, and a later episode for the same security remains independently reconcilable. No producer-owned files were changed by #99.
- **Required inputs:** A focused integration test writes real #100 DTOs into a temporary SQLite store, reads the active pointer version, then obtains generation-A actions and holdings together with generation-B portfolio facts through `load_policy_execution_snapshot`.
- **Acceptance evidence:** Focused consumer regressions pass, including canonical temporary-store checks for holding flags, resolved unissued attempts, and flat-history/later-episode behavior. Earlier independent consumer approval of alias coalescing in `2cf4bdf` and origin-clock work in `6f225ee` remains scoped to those changes. The old broad non-integration run was interrupted at about 14% after reporting failures and is not acceptance evidence. Lead integrated-boundary review of this consumer continuation remains pending; full integration acceptance is not claimed.
- **Dependencies:** The pure producer checkpoint is approved and integrated as a construction dependency. Persistence source `d52fb22`, correction `963a61f`, and late-reference correction `43a0820` are integrated as producer construction dependencies. The latest producer review report `bfcaa5a` approves that producer source/report pair. Producer approval does not approve this integrated consumer boundary or replace the pending lead delta review.

## Changed paths owned by #99

- `core/strategy_policy/account_reconciliation.py`
- `tests/test_strategy_policy_account_reconciliation.py`
- `docs/strategy-policy-account-reconciliation-issue99.md`
- `docs/issue-99-implementation-report.md`

The following are inherited from the #100 dependency commit and are not #99-owned changes:

- `core/policy_execution_state.py`
- `tests/test_policy_execution_state.py`
- `docs/issue-100-state-interface-v1.md`
- `docs/issue-100-implementation-report.md`

## Acceptance mapping

- Reconciles account facts to stable security identity, deployment/store/account identity,
  session clock, broker positions, logical actions, attempt-scoped references, and protective
  orders.
- Preserves the completed-session feature cutoff separately from next-opportunity account
  valuation time.
- Reserves residual buy cash and canonical residual committed risk once; confirmed fills
  remain represented in broker cash/positions and are not subtracted a second time.
- Reports pending strategy sells separately from protective sells and current position risk.
- Coalesces broker rows using registered primary/alias references only when all references map to the same canonical attempt and quantity, status, symbol, side, and other material facts agree.
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

The branch includes persistence commit `d52fb22deedc74361f6e4ae4a0113dc4f215c3c2`, durable-state correction source `963a61f1dbc0641d50a4272e845447bf4e40abfa`, and latest source correction `43a0820dea9688fd8583f45ccb9704f172553954` for construction. Producer report-only history progressed from `d8e383ecf5cdae37f0a3828a056ff2d1b1e202af` to `bfcaa5a8cfdcfc5e32edfd78e4dfd5bb65e5b2d5`. The latest producer source/report pair is independently approved; the earlier finding about late references restoring the holding reconciliation conflict is cleared by `43a0820`. This does not approve the integrated consumer boundary. The #99 correction does not edit producer state/store files.

The focused temporary-store test reads the absent active pointer (`active_generation_id=None`, `pointer_version=None`) and supplies both observed values to the versioned setter. It then uses `PolicyExecutionStateStore.load_policy_execution_snapshot` as the source of both generations' records. A generation-A holding and partially filled scale-out action, with provider-scoped aliases and its original action clock, are returned in one read alongside generation-B's current portfolio snapshot. Current synthetic broker positions, balances, and protective-stop facts reconcile against that returned data.

The consumer conversion tests cover preservation of explicit resolution reasons, per-attempt aliases and terminal status, and state versions; readiness after a resolved terminal action; release of its reservations; acceptance of a matching older submitted action; and fail-closed outcomes for future/inconsistent provenance and expired unsent actions. The historical 35-test receipt above remains evidence from `e12a8a7` only.

The alias-deduplication follow-up resolves the remaining consumer review finding. For strategy orders with canonical attempts, each supplied broker/client ID must map uniquely to the same `(logical_action_id, attempt_number)` before rows are considered equivalent. Rows are coalesced only when symbol, side, status, purpose, holding ID, requested and filled quantities, and stop price agree. Unregistered or cross-attempt references are not eligible; conflicting material facts remain separate and block reservations. The consumer guide now describes resolved-action readiness and the limited temporary-store test correctly.

The independent consumer review approved the alias correction in `2cf4bdf` as spec-compliant with no remaining Important or Minor findings. That review preceded the producer `963a61f` merge and did not cover the pointer-version test adaptation below. This narrow adaptation is submitted separately; neither result closes the producer persistence review or combined restart acceptance gates.

Focused verification after the persistence integration:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_keeps_order_references_per_attempt tests/test_strategy_policy_account_reconciliation.py::test_matching_old_generation_holding_action_is_valid_under_new_active_generation tests/test_strategy_policy_account_reconciliation.py::test_duplicate_broker_rows_with_conflicting_alias_pairs_block_reservations tests/test_strategy_policy_account_reconciliation.py::test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio -q
```

Result: **6 passed**. Ruff passed for the reconciliation module and its focused test file; `compileall` passed for the reconciliation module; `git diff --check` reported no whitespace errors. No broad suite was run for this continuation.

## Registered-alias review follow-up

Focused tests cover three equivalent-row shapes: a primary pair plus a registered alias pair, a shared primary broker ID with an alias client ID, and a shared primary client ID with an alias broker ID. Each positive case confirms one residual cash reservation, one residual risk reservation, and one pending entry. Negative controls vary quantity, status, symbol, or side and confirm readiness and reservations remain unavailable. Existing wrong-reference and contradictory-pair controls remain in place; the per-attempt regression also verifies that an order combining references from separate attempts is rejected.

Focused regression command:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_keeps_order_references_per_attempt tests/test_strategy_policy_account_reconciliation.py::test_duplicate_broker_rows_with_conflicting_alias_pairs_block_reservations tests/test_strategy_policy_account_reconciliation.py::test_conflicting_duplicate_broker_order_reference_blocks_reservations tests/test_strategy_policy_account_reconciliation.py::test_duplicate_local_and_broker_references_do_not_double_reserve tests/test_strategy_policy_account_reconciliation.py::test_unavailable_security_mapping_with_matched_order_returns_unready tests/test_strategy_policy_account_reconciliation.py::test_matching_old_generation_holding_action_is_valid_under_new_active_generation tests/test_strategy_policy_account_reconciliation.py::test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio tests/test_strategy_policy_account_reconciliation.py::test_registered_attempt_alias_rows_reserve_once tests/test_strategy_policy_account_reconciliation.py::test_registered_attempt_alias_rows_with_conflicting_facts_block_reservations -q
```

Result: **17 passed** after integrating producer source `963a61f` and adapting the consumer test to its pointer-version API. Ruff passed on the reconciliation module and focused test file; `compileall` passed for the reconciliation module; `git diff --check` found no whitespace errors. No broad suite was run.

After the producer merge, the canonical-store API adaptation was also run by itself:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio -q
```

Result: **1 passed**. The test reads `ActiveGenerationPointer`, then supplies both its observed generation ID and pointer version to `set_active_generation`.

Lead-provided chain evidence is separate from the local consumer run. The earlier expanded `tests/test_paper_policy_chain.py` run reported **5 passed, 2 warnings**, including both store alias/restart duplicate cases and the later-session mixed-generation case. Warning details were not supplied, so the receipt remains attributed to the lead and is not classified as warning-free. At that time, report `d8e383e` and lead status kept producer review open; that status was superseded by the independently approved producer source/report pair `43a0820`/`bfcaa5a`.

## Latest integrated-boundary consumer corrections (2026-10-01)

The lead's boundary review against producer source `43a0820` reproduced three consumer gaps. The #99 changes now preserve holding `policy_flags` and make only `position_reconciliation_required` block readiness with its reason; accept explicitly resolved attempts only when terminal or matching the producer's exact unissued predicate; and preserve flat holding stop history without demanding a live stop. An active protective sell against a flat episode remains a conflict, while a later same-security episode is reconciled by its own quantity and stop.

The unissued-attempt test writes a never-issued addition and a scale-out whose first issued attempt is cancelled and whose remainder attempt remains unissued. Both are resolved with explicit reasons and no active matching order, then read through a reopened SQLite store and the canonical converter. Its negative controls add a primary reference, an alias reference, a nonzero fill, or an active matching broker order and confirm readiness stays blocked. The flag lifecycle similarly persists the evidence-backed flag and its evidence-backed clear across separate canonical reads; account balances and position quantity remain unchanged. The flat-history case closes the original episode, reads it with no active stop, blocks an unexpected active old stop, then opens a later episode for the same stable security and reconciles both records.

Focused regression command:

```text
python -m pytest -p no:cacheprovider -o addopts='' -W ignore::pytest.PytestConfigWarning --tb=short tests/test_strategy_policy_account_reconciliation.py::test_canonical_holding_reconciliation_flag_survives_restart_until_evidenced_clear tests/test_strategy_policy_account_reconciliation.py::test_canonical_resolved_unissued_addition_and_remainder_release_reservations_after_restart tests/test_strategy_policy_account_reconciliation.py::test_flat_holding_history_needs_no_live_stop_and_allows_a_later_same_security_episode tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_preserves_decimal_residual_attempts_and_identity tests/test_strategy_policy_account_reconciliation.py::test_policy_execution_state_conversion_keeps_order_references_per_attempt tests/test_strategy_policy_account_reconciliation.py::test_canonical_store_read_reconciles_old_generation_state_with_current_portfolio tests/test_strategy_policy_account_reconciliation.py::test_partial_sell_restart_reconciles_position_and_keeps_sell_pending_visible tests/test_strategy_policy_account_reconciliation.py::test_protective_sell_is_matched_to_holding_and_not_counted_as_strategy_sell tests/test_strategy_policy_account_reconciliation.py::test_registered_attempt_alias_rows_reserve_once tests/test_strategy_policy_account_reconciliation.py::test_registered_attempt_alias_rows_with_conflicting_facts_block_reservations -q
```

Result: **15 passed**. Ruff passed for the consumer module and its focused tests; `compileall` passed for the consumer module; `git diff --check` reported no whitespace errors. This was a selected offline consumer run only. No broad suite, runtime, provider, broker, or real-store test was run. Producer source/report approval at `43a0820`/`bfcaa5a` is separate; lead delta review of these consumer changes remains outstanding, so integrated acceptance is not claimed.

Latest consumer artifact SHA-256 hashes (the synthetic records are inline in the test source):

| Artifact | SHA-256 |
| --- | --- |
| `core/strategy_policy/account_reconciliation.py` | `595dc4e4742780667dab536d14c71ae8c8c27ec4e5cdb7901585881c806a84e1` |
| `tests/test_strategy_policy_account_reconciliation.py` | `d7a5d0850db4b3b9d9eb62b4bd1d87b2440c07833e544cf16967d932473e6887` |
| `docs/strategy-policy-account-reconciliation-issue99.md` | `f3b52d3c935b371b75a3f4c8c7d8a9b0c86f4d3c214e50b6114f0ca4478b4ae1` |

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

- Producer source/report `43a0820`/`bfcaa5a` are approved construction dependencies. The integrated consumer delta review remains pending.
- Combined restart acceptance with the durable store, live account facts, and deployment workflow remains a separate gate; the current temporary-store test is synthetic and consumer-focused.
- This work does not establish real provider/broker behavior or runtime deployment acceptance.
- Decimal source values are normalized to finite floats at the existing V3 feature boundary;
  this follows the current `PortfolioFeaturesV3` numeric contract.

## Post-commit integrated review and evidence correction (2026-10-01)

The final local integrated review at lead application revision `d2c4082746017dd4ecb14d709d71a7c12b1fc2d5` approves the specification and quality for all nine bounded offline criteria and closes the three consumer findings. The lead-reported six-module run completed with **107 passed, 2 warnings in 13.95 seconds**; Ruff passed, and 312 committed Python files compiled with Python 3.13. This local integrated approval supersedes the earlier status above that said the consumer delta review was pending. Principal review, publication, remote CI, and merge remain pending.

The earlier artifact table is preserved as written. Its `595dc4e4742780667dab536d14c71ae8c8c27ec4e5cdb7901585881c806a84e1` entry is not the SHA-256 of the committed consumer module blob. The raw Git blob for `core/strategy_policy/account_reconciliation.py` at commit `d2a0677d53be5ff6bd5037b153f54f1bbf69acc3` was independently verified as `a5d28579374cfdbfb3528c53aca0fd217b6b3cc386ce437fe59490f1f7604f7f`. This correction is report-only; it does not change the source or tests.
