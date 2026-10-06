# M3 P2 (#104) addition dependencies

Status: the durable addition adapter and bounded fake-broker coverage are implemented on `codex/issue-103-portfolio-replacement`. The existing #103 changes in `tests/test_policy_replacement.py` remain untouched. No live broker, provider, runtime, or deployment was used.

## Resolved rules

- The adapter uses the latest valid decision-time position mark, rejects missing/stale/invalid prices and stops, and floors quantity to whole shares under reconciled available cash, the fixed add-on risk fraction, the remaining 1% per-position stop-risk allowance calculated from weighted aggregate cost basis versus the confirmed stop, and the optional aggregate notional cap. The original entry price remains the basis for entry-return and excursion semantics.
- Confirmed fill watermarks update holding quantity, weighted cost basis, addition quantity/count, and reservation exactly once. Duplicate receipts do not change state.
- A partial addition is not considered complete until the residual is terminally reconciled against broker order and position facts. Only then does the adapter resolve the addition and use the accepted stop-update store API.
- Submission, cancel, and stop-replacement uncertainty retain the durable action/reservation and return a waiting state on replay. A later cancel result can be applied through `confirm_addition_cancel` without resending cancel. A later stop observation can be applied through `confirm_addition_protection` without repeating replacement.

## Required inputs and integration dependency

The adapter needs an authoritative paper-account snapshot with its account/deployment identity, clock, equity, cash, the holding's exact position quantity and recent mark, and open-order facts. Terminal cancel reconciliation additionally needs the final cumulative fill quantity/notional/fees and a terminal order fact matching the durable attempt references, requested quantity, symbol, side, and holding. Missing or mismatched facts keep the action waiting and retain its reservation/protection state.

The caller-provided `replace_stop` callback must perform an atomic broker-side replacement that keeps the old stop active until the new stop is accepted, then return a fresh complete account snapshot showing the exact holding quantity and one active replacement stop at the preserved price. If the configured broker path cannot provide that atomic guarantee or observation, the caller must not execute the resize; the adapter leaves the old confirmed stop recorded and the holding frozen. This repository slice validates the contract with a fake broker only; provider/runtime wiring remains outside this scope.

## Verification scope

`python -m pytest -q --no-cov --tb=short tests/test_policy_addition.py` covers whole-share sizing including a confirmed prior add at a different price, exact-once reservation/fill handling, restart replay, definitive rejection, uncertain cancel and stop replacement freeze/recovery, and exact-quantity stop confirmation. It does not verify live broker behavior.
