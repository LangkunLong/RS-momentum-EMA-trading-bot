# #106 historical to fake-paper action comparison contract

This is an offline engineering comparison for the fixed policy and recorded inputs. It does not qualify a strategy, forecast returns, or authorize trading. The comparator is `compare_action_parity_cases` in `core/pit_policy_parity.py`. It accepts flattened `policy_identity`, `facts`, `decision`, `intent`, and `execution` mappings for the same action family. Nested values must first be reduced to stable field identities or digests.

## Classification

| Result | Meaning |
| --- | --- |
| `matched` | Every supplied field and every required field is present and equal. |
| `matched_with_execution_variance` | Fixed policy, facts, decision and intent match; only declared execution fields differ. |
| `incomplete` | A required fact is absent or null, or a supplied field has no counterpart. Unknown does not mean zero, market, success or parity. |
| `mismatch` | A known fixed fact, decision, order intention, or undeclared execution field differs. |
| `incompatible_policy` | A policy identity field differs; the decisions are not comparable as the same policy deployment. |

Policy identity includes artifact, capability manifest, interface and feature contracts, calculator, source revision, runtime, execution profile, paper account environment and store identity. An incompatible field takes precedence over other classifications. A fixed mismatch takes precedence over incomplete execution evidence, and incomplete evidence takes precedence over a known execution variance. The comparison result retains all matched, unknown, mismatch and execution variance field names even when the disposition has higher precedence.

## Required fixed evidence

All families require a decision ID, action family, exchange, completed decision session, as-of cutoff, next execution session, account valuation session and instant, input snapshot digest, category and subject. Required intent and fact fields are defined per family in `_ACTION_PARITY_REQUIRED_INTENT_FIELDS` and `_ACTION_PARITY_REQUIRED_FACT_FIELDS`; these sets are the machine-readable contract. Entry additionally requires recorded feature and account identities, entry and capacity snapshot digests, security/symbol, reference price and source, tick/lot precision, pending and open counts, effective capacity and slots. Its entry qualification, market permission, rank, capacity selection and allocation fractions are compared as decision outputs.

Replacement compares the evicted holding and candidate, sell and prospective buy terms, risk basis and sell order type. Addition compares the holding and add-on feature facts, policy decision, quantity, reservation and protection terms. Scale-out and close compare the holding, exact sell role/quantity, and, for scale-out, original quantity, fraction, tier and rounding rule. Order type is a required fixed intent field for every family. A missing type is `incomplete`, even when a nearby submit callback passes a price or a legacy action was later observed to fill. Never backfill an old action's type from a default. New controlled entry allocation plans and addition intentions record `limit`; a replacement plan may explicitly select `market` for its sell leg. The replacement buy limit is still a separate child action and is not mislabeled as the sell type.

The recorded-to-durable fixtures in `tests/test_issue106_action_parity.py` and `tests/test_issue106_replacement_parity.py` read the fake-paper decision and action after reopening the store. The entry fixture also checks the frozen policy's entry snapshot and durable capacity/entry/allocation decisions against the recorded inputs. The connected simulator/current-paper fixture in `tests/paper_verification/test_parity.py` proves the common frozen entry policy sees equal entry snapshots and decisions on two bounded panels, while retaining the known capacity queue and fill-model differences. These fixtures are evidence for their named synthetic cases, not a universal parity attestation.

## Equality and tolerances

| Field class | Rule |
| --- | --- |
| Policy identity, security, role, side, type, reason, status and other fixed strings | Exact value. Blank required strings are unknown. |
| Fixed quantities, prices, fractions and risk values | Exact finite decimal value. `10`, `10.0` and `10.00` compare equal; `10.001` differs. There is no tick, lot, price or quantity tolerance. |
| Booleans | Exact boolean; `True` is not numeric `1`. |
| Decision cutoff and account valuation instants | ISO-8601 aware timestamps represent the same UTC instant. Time zone spelling may vary; tolerance is zero seconds. Naive or invalid timestamps cannot match. |
| Execution valuation, submission and observation instants | Same UTC normalization; any nonzero difference is a declared execution variance when both values are known. |
| Fill price and fees | No numeric tolerance. A known difference is an execution variance, never a fixed decision match or mismatch. Missing observations remain unknown. |

The required execution fields are fill price, fees, pending state, order status, filled quantity, partial-fill, missed-fill, rejected and cancelled flags, and liquidity assumption. Declared optional execution variance fields also include rejection/cancellation reason, provider/client order references, and submission/observation/valuation instants. A known one-sided rejection or cancellation reason is a variance; a one-sided provider reference or timestamp and any missing required outcome are unknown. A known difference in an undeclared execution field is a mismatch. No profit or return difference is whitelisted as broker variance.

The `tests/paper_verification/test_action_outcome_comparison.py` control matrix runs each of the five families through rejection, cancellation, partial and missed fill, liquidity difference, changed order type, decimal quantity precision and exact decision-time checks. It verifies classification of deliberately changed facts; it does not manufacture broker receipts.

## Replay and limits

New entry plans record `entry_plan.order_type=limit` in the immutable allocation decision. Pending-entry reload authenticates that type, and `OrderManager.submit_policy_entry` checks the linked allocation decision, action ID, symbol, quantity, price and type before calling the existing limit request path. New addition decisions record `order_type=limit`; the addition submit owner checks the linked action ID and sends the selected type and limit price to its injected fake-paper submit callback. A new replacement parent may record `sell_order_type=market` beside its sell action ID; its public order owner verifies the unchanged plan on replay, and exit dispatch verifies the parent sell link before sending the type to the fake broker. Scale-out and close likewise record explicit `market` choices in their linked decisions. Fake sell order facts retain the selected type through snapshot/restart. Changed immutable decision facts or changed selected replacement plans fail replay. Legacy records lacking a selected type remain unknown; no schema migration or backfill is performed. The existing action owner tests cover restart, duplicate callbacks, rejection, uncertain submission, cancellation, partial fills and fresh reconciliation at their respective boundaries; this comparator does not execute those transitions itself.

Historical next-open full fills and asynchronous fake-paper partial fills are different execution models. Available synthetic inputs do not establish exchange depth, queue position, actual slippage or a live provider's liquidity. The comparison records those assumptions as execution evidence or unknowns. A missing order type or liquidity assumption prevents `matched`, even if fixed policy fields agree.

## Bounded acceptance evidence

The implementation branch started from merged main `663a872facf170a7337b515f9c47fc5362bf5194`, after the #101–#105 adapter work. The connected entry case uses `tests/fixtures/paper_parity_input.json` (SHA-256 `6085a26ec832039aaa8dd906801142bdf174107ad0cfe6d0f62283be2834d051`) and the pinned policy source hashes in `tests/fixtures/paper_parity_policy/expected-inputs.json` (SHA-256 `54f18562531e04d2d2a1b3ed33d190acf9598a8d11a818077689a9a51194c463`); its fixture asserts the fixture input and frozen policy source hashes at runtime. Other action cases construct fixed offline feature, account and holding identities through `tests/paper_policy_fixtures.py` and the action-owner fixture helpers. No broker provider is contacted.

| #106 acceptance condition | Named evidence |
| --- | --- |
| Every action's facts, decision and intention | Entry, addition, scale-out and close recorded-to-durable checks in `tests/test_issue106_action_parity.py`; replacement's historical/durable sell comparison in `tests/test_issue106_replacement_parity.py`; common comparator controls for all five families in `tests/paper_verification/test_action_outcome_comparison.py`. The connected simulator/current-paper entry fixture is `tests/paper_verification/test_parity.py`. `tests/test_order_execution.py::TestSubmitBracketBuy::test_limit_entry_order_has_no_embedded_stop` confirms the existing broker request constructor receives a `LimitOrderRequest`. |
| Restart and duplicate protection | Entry workflow recovery and duplicate fill tests in `tests/test_order_manager_policy.py`; replacement sell, addition and exit one-use/replay tests in `tests/test_policy_replacement_execution.py`, `tests/test_policy_addition.py` and `tests/test_policy_exit_execution.py`. |
| Rejection, cancellation and partial fill | Replacement rejection and delayed cancellation in `tests/test_policy_replacement.py`; addition rejection, duplicate partial fill and uncertain cancellation in `tests/test_policy_addition.py`; scale-out/close rejection, partial fill and cancellation in `tests/test_policy_exit_execution.py`. Entry callback partial fill and uncertain submission recovery are in `tests/test_order_manager_policy.py`. The five-family comparator matrix separately classifies all broker outcome differences, including missed fills. |
| Tolerances and modeled differences | The equality table above and the five-family outcome matrix cover fixed order type, decision/action time, exact numeric quantity, rejection/cancellation, partial/missed fill, liquidity and unregistered execution fields. The connected entry fixture retains its capacity queue and full-versus-partial fill differences. |

On the exact starting revision, two broader `tests/test_issue_101_paper_policy.py` cases fail before this change: repeated selection reports `('BBB', 'AAA')` against a fixed `('AAA', 'BBB')` assertion, and a case-directory SQLite path is not created before `migrate()`. The #106 branch reproduces the same failures. They do not alter the focused #106 action comparison results and are not represented as passing evidence.
