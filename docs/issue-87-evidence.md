# Issue #87: diagnostic meanings and execution-cost assumptions

**Prepared:** 2026-10-03 (America/Toronto). **Branch:** `codex/issue-87-diagnostic-meanings`. **Inspected source revision:** commit `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab`, tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`. **Evidence type:** fixed synthetic arithmetic check; no external data or policy worker was used.

## Implementation

The report formulas already exist in `core/pit_optimizer_v5/diagnostics.py`. This work adds a focused test of their public `EvaluationReportV5` values and records the meanings their present calculations support. It does not change shared diagnostics, contracts, simulator accounting, or report projection. The concise requests for the shared report owner are in [`source-corrections.md`](../.artifacts/issue-87/source-corrections.md); source-semantic integration and independent lead review remain outstanding.

The report exposes these fields:

| Public field | Calculation in the inspected source | Supported description |
| --- | --- | --- |
| `gross_annualized_return_pct` | Add each date's cumulative configured fill friction back to the observed equity curve, then calculate annualized return on that reconstructed curve. | Same realized path with configured friction restored. A path-derived gross estimate; it does not rerun decisions, orders, quantities, or cash under zero friction. |
| `estimated_idle_cash_drag_pct` | `net annualized return / mean(observed-session gross long notional / observed-session total equity) - net annualized return`. | Exposure-scaled estimate. It does not accrue cash yield or simulate redeployment and changed actions. |
| `scale_out_opportunity_cost_pct` | For scale-out rows, sum `max(episode maximum completed-bar price - scale-out execution price, 0) × shares sold`; divide by scale-out execution notional. | Hindsight price gap to the episode-wide maximum. The maximum has no timestamp in `PositionEpisodeV5`, so it may precede the scale-out. It is not realized loss or a simulated hold-to-maximum counterfactual. |

`portfolio_total_return_pct` and `portfolio_annualized_return_pct` use the first and last observed equity values in the panel. `friction_drag_pct` is the difference between gross and net total-return percentages. The cost-added-back curve preserves the simulated path. None of these metrics separately simulates changed fills or decisions under another friction assumption.

### Independent fixed arithmetic

The owned test, [`test_issue87_diagnostic_meanings.py`](../tests/test_issue87_diagnostic_meanings.py), builds one typed `EvaluationPanelSpec` and a fixed `SimulationResultV5`. It uses a $1,000 initial account; three synthetic checkpoints on 2025-01-02, 2025-07-02, and 2026-01-02; five shares bought at a $100 reference price; two shares scaled out at $110; and three shares exited at $120. The date range is 365 calendar days so the annualized example is easy to inspect. These are sparse synthetic checkpoints, not a continuous market-session panel or historical evidence.

The configured `base` scenario applies a 2 bp half-spread, 3 bp market-impact assumption, and 0 bp commission on each side:

| Fill | Reference × quantity | Execution price | Cash delta | Configured friction |
| --- | ---: | ---: | ---: | ---: |
| Buy 5 | $100 × 5 | $100.05 | −$500.25 | $0.25 |
| Scale out 2 | $110 × 2 | $109.945 | +$219.89 | $0.11 |
| Exit 3 | $120 × 3 | $119.94 | +$359.82 | $0.18 |
| **Total** |  |  | **+$79.46** | **$0.54** |

The observed net equity checkpoints are `$999.75 → $1,049.64 → $1,079.46`. Adding cumulative configured friction produces `$1,000.00 → $1,050.00 → $1,080.00`. The public report therefore returns:

- Net first-to-last return and annualized return: **7.972993%**.
- Same-path gross annualized return: **8.000000%**.
- Total configured friction: **$0.54**.
- Mean observed-session marked exposure: **27.150618%**. The three observed-session long-notional fractions are `$500 / $999.75`, `$330 / $1,049.64`, and `$0 / $1,079.46`. This is an equal-weight mean of the supplied checkpoints, not a time-weighted or daily-density measure.
- Exposure-scaled invested-sleeve annualized return: **29.365788%**.
- Estimated idle-cash drag: **21.392795 percentage points** (`29.365788 − 7.972993`). This is the formula's scale-up proxy, not the simulated result of investing idle cash.
- Scale-out gap: the synthetic scenario assumes the **$130** episode maximum occurred before the later scale-out. `maximum_completed_bar_price` has no timestamp, so the stored metadata cannot establish that ordering. Under the scenario premise, the formula counts `($130 − $109.945) × 2 = $40.11` over `$109.945 × 2 = $219.89`, producing **18.240939%**. The fixture does not establish that holding the sold shares would have gained $40.11 after the sale.

The fixture confirms arithmetic and current public field values. Its `LEAD` lineage, dates, prices, execution events, and account observations are synthetic and do not establish actual trading results, historical coverage, strategy performance, or execution quality.

## Required inputs

**Available for the fixed check:** the formulas, source contracts, static scenario values, and exact synthetic observations in the test. The existing #80 interfaces inspected are `PanelSecurityLineage` / `EvaluationPanelSpec`, the fixed V5 `ExecutionProfileV5`, and the typed policy decisions in `core/strategy_policy/contracts.py`. The existing evaluator-assumptions test shows the baseline and candidate receive the same declared panel and simulation inputs. This issue #87 fixture exercises the report boundary; it does not revalidate policy decision behavior.

For source identity, the inspected #81 contracts include `EvaluatorContractV5`, which binds execution and sandbox profile identities, evaluator source identity, PIT bundle and price-provenance identities, the price-identity transition contract, baseline policy identity, friction grid, and selected scenario. `evaluator_source_map_v5` hashes its closed source set. This report pins the exact local source revision and per-file hashes below. The agreed final integrated revision and portable evidence index are not selected in this branch; this fixture result must be rebound and reviewed after that integration.

**Empirical execution calibration:** none was supplied with this evidence. The inspected friction contract carries scenario ID and configured basis-point rates. `apply_friction` receives side, reference price, quantity, and those rates. It does not consume observed quotes, order size as a fraction of ADV, market volume/depth, broker fills, or empirical calibration provenance. The canonical grid is explicitly:

| Scenario | Half-spread (bp) | Market impact (bp) | Commission (bp) |
| --- | ---: | ---: | ---: |
| `gross` | 0 | 0 | 0 |
| `base` | 2 | 3 | 0 |
| `stress` | 5 | 10 | 1 |

These are configured assumptions. The fixed check verifies their arithmetic, not calibration or realism. No empirical basis for the values is evidenced in the reviewed V5 scenario, fill, or evaluator contract.

## Acceptance evidence

The original issue acceptance criteria map as follows:

| Original criterion | Evidence and result |
| --- | --- |
| Gross-return, idle-cash, and exit-opportunity labels match actual calculations. | The test asserts exact outputs from independent fixed arithmetic. The source field names alone do not explain that gross is same-path, idle cash is exposure-scaled, or the scale-out maximum can predate the sale. The minimal definition/label changes are requested in `source-corrections.md`; this criterion awaits #86 integration and review. |
| Missing empirical calibration is explicit rather than represented as realistic execution proved. | The source grid and fill inputs show configured assumptions only; no calibration record or empirical input is attached. This report explicitly marks calibration unavailable/not supplied. The report-semantics integration request asks #86 to carry that status in v2. No realistic-execution claim is supported. |
| Stronger counterfactual or calibration work is separately specified with required inputs and bounded scope. | A bounded follow-up is specified below. It was not started. |

The source snapshot and fixture identities for this evidence are:

| Item | Identity |
| --- | --- |
| Starting Git commit / tree | `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab` / `62685d7d5ce9d4851051ed5ec98be4b81a5cc757` |
| `core/pit_optimizer_v5/diagnostics.py` SHA-256 | `BA350E2BCC71C9FD67D48419D7F24AE6C15EE53E89F575B4398E9AA80BCC3F35` |
| `core/backtest_fills.py` SHA-256 | `90E77530244435804AE081935306F760925A9FCD6DB9740F76020F53F29B67B2` |
| `core/pit_optimizer_v5/contracts.py` SHA-256 | `872A1BE1043EF5A2519E81D6FCC10DF3FFE4FD5F7FB04022AA82C36DA18F1728` |
| `core/pit_optimizer_evaluation.py` SHA-256 | `4EA6F9CAB65759C3AA75F60535ED623682E0733E805F409A5B285C8D2141C2E1` |
| Fixture input/test source | `tests/test_issue87_diagnostic_meanings.py` SHA-256 `83337A094696735D14A0DA2AF477BAF53997EC073BE8C2C2B3BBC0CD79639A25`; fixed values enumerated above; no market-data artifact or external provider input |

### Scoped local checks

- `py -3.13 -B -m pytest -p no:cacheprovider --no-cov -q tests/test_issue87_diagnostic_meanings.py` — **2 passed**. Pytest reported two environment warnings: unrecognized `cache_dir` config option and the installed `websockets.legacy` deprecation.
- `py -3.13 -m ruff check tests/test_issue87_diagnostic_meanings.py` — **passed**.
- Python 3.13 source compilation of the new test with `compile(...)` — **passed**.
- Python 3.11 was not run. Its only registered path points into a retired checkout and the binary is absent; the lead directed not to restore or install that runtime. The final 3.11 CI matrix remains required.

### Bounded follow-up proposal (not started)

Run one calibration and paired-replay study for one named broker/venue, one frozen policy revision, and one predeclared equity-universe/date interval. Bound the collection to at most 1,000 order intents across one 12-month calibration interval and one subsequent three-month held-out interval. Report per-side and order-size/liquidity strata; any stratum without the predeclared minimum sample remains uncalibrated. Do not broaden the universe or date interval to fill empty strata.

Required inputs:

- Broker order-intent and fill records: timestamps/time zone, symbol identity, side, order type, limit/market terms, requested and filled quantities, partial/cancel/reject outcomes, reference-price definition, execution prices, commissions/fees, and route/venue.
- Point-in-time market evidence around each intent and fill: bid/ask quotes, trades, volume, and the ADV/depth window used to classify order size and participation. Include corporate-action and symbol-identity lineage.
- Frozen #80 policy decision/input contracts and one immutable policy revision; the exact point-in-time price/universe panels, source licenses, and #81 source/evidence index for the held-out comparison.
- Predeclared calibration method, training/held-out boundary, size/liquidity buckets, minimum usable observations per bucket, missing-data behavior, and uncertainty reporting.

The follow-up would estimate costs on the calibration interval, report held-out fit and coverage, then replay the same frozen policy on the same fixed panel with calibrated execution. The replay must let changed fills affect cash, quantities, stops, exposure, and later order eligibility. Compare it with the observed configured-cost path as a paired result. This bounded study would supply a true simulator counterfactual only for its declared source, policy, broker/venue, universe, interval, and order limit. It does not require rewriting the existing fill engine or claiming coverage outside those limits.

## Dependencies

- **#80 start prerequisite:** treated as eligible per the activation and lead handoff. The local input, decision, and experiment contracts used by this fixture were inspected. A direct GitHub issue-body fetch failed because the configured API proxy was unavailable; the local issue brief and source contracts remain the inspected authority for this work.
- **#81 additional acceptance prerequisite:** pending the principal's selected integrated source revision and portable evidence index. The evidence here is source-bound to the exact starting revision and must not be reused as acceptance evidence for changed integrated source bytes.
- **Shared source integration:** #86 owns report/diagnostic contract and projection edits. It can apply the requests in `source-corrections.md` while preserving v1 serialization. This branch does not edit those shared files.
- **Review:** the lead supplies the independent review after integration. No helper or reviewer was dispatched from this implementation task.

The full offline suite was not run. The scoped issue #87 fixture, lint, and compilation checks are recorded in the completion report; production calibration, historical simulation, and acceptance on the final integrated revision remain unverified.
