# Issue #87 source correction requests

**For:** #86, sole editor of the shared report/diagnostic contracts. **Inspected source:** `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab` / tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`. **Scope:** Correct the meanings exposed for existing V5 metrics. Keep the calculations, simulator, and v1 serialized bytes unchanged.

## Minimal report-semantics changes

1. **`report.gross_annualized_return_pct`** — describe as a *same-path gross-of-configured-friction estimate*. `diagnostics.py` adds cumulative costs from the selected friction scenario back to each observed equity point, then calculates annualized return on that reconstructed curve. Positions, fill quantities, order dates, and exits remain those of the cost-bearing run. This is not a zero-cost policy re-simulation or a true counterfactual. Preserve the existing field ID; add this definition to the v2 display/role semantics.

2. **`report.estimated_idle_cash_drag_pct`** — describe as an *exposure-scaled proxy*: `net portfolio annualized return / mean(session gross-long-notional / session total-equity) - net portfolio annualized return`. The mean uses each observed session, including terminally liquidated sessions. It assumes proportional scaling of the realized return; it does not earn interest on cash or simulate redeployment and changed decisions. Keep `estimated` in the human-facing label.

3. **`report.scale_out_opportunity_cost_pct`** — describe as a *hindsight scale-out-to-episode-maximum price gap*, with the denominator equal to total scale-out execution notional. The numerator uses `(episode maximum completed-bar price - scale-out execution price, floored at zero) × shares sold`. `maximum_completed_bar_price` is episode-wide and carries no timestamp, so it may precede a scale-out. In the fixed example the $130 maximum precedes the $110 reference-price scale-out (executed at $109.945); the report still counts that earlier maximum. Do not label this a realized loss, future upside missed, or a counterfactual. Preserve the field ID; expose the whole-episode timing limitation in v2 display/role semantics.

4. **Execution realism/calibration status** — state that the canonical `gross/base/stress` bps are configured scenario assumptions only: `(0,0,0)`, `(2,3,0)`, `(5,10,1)` for half-spread, market impact, and commission. `FrictionScenario` and the fill API carry no empirical sample/provenance or calibration identity; the fill API has no observed quote, volume, order-size/ADV, or participation input. Mark empirical calibration `unavailable/not supplied` for this evidence. Do not emit `calibrated`, `realistic execution`, or equivalent as established facts.

The fixed check in [the evidence report](../../docs/issue-87-evidence.md) exercises these three exact definitions. This report requests semantics only; it does not ask #86 to change the underlying formulas or add a fill engine.
