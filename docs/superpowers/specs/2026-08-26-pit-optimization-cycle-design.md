# PIT Optimization Cycle Design

**Date:** 2026-08-26
**Status:** Approved for implementation

## Objective

Add one narrow, auditable optimization gate that can complete exactly one full point-in-time
CANSLIM parameter experiment. The gate must expose the engine's actual policy and complete
performance evidence, preserve all causal invariants, keep the source worktree unchanged, and
produce an inert candidate diff. It is not a general optimizer and does not alter the legacy
`test`, `backtest`, or `pit_diagnosis` gates.

CANSLIM remains the strategy basis and candidate vocabulary. The optimization objective is higher
risk-adjusted return, not literal adherence to every historical CANSLIM threshold. Point-in-time
visibility, completed-session facts, next-session execution, immutable inputs, deterministic
accounting, and no leverage remain hard constraints.

## Fixed authority

The only admissible baseline is the sealed Task 11 replay:

- window: `2021-01-01` through `2025-12-31`;
- holdout: the `2025-01-01` through `2025-12-31` slice of the same full-window run;
- PIT bundle SHA-256: `1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb`;
- baseline manifest SHA-256: `f99eb6aade8b567b319accb95dadd80789064aacf1cb8b85d0cc31379caf6382`;
- baseline source commit: `515cb1e50d051e2ee4253603608f2fd3920004bc`;
- baseline strategy: total return `10.466720690872911`, annualized return
  `2.014176669879464`, maximum drawdown `-12.039961219836394`, Sharpe
  `0.3429824494606095`, closed trades `176`, and average weekly cash
  `74.84827271765748` percent;
- baseline entry funnel: `598696` evaluated, `233` qualified and attempted, `176` executed,
  and `57` next-open buy-zone rejections.

The controller receives absolute operator-supplied bundle and baseline paths plus those exact
hashes. It verifies regular non-link files, all manifest artifact hashes needed for evaluation,
the bundle digest and manifest identity, the fixed date contract, baseline metrics, current
effective policy, and policy digest before any provider call. The sealed Task 11 artifacts are
read-only and are never rewritten.

## Two phases

`--optimization-phase prepare` performs only offline readiness. It authenticates inputs, derives
the full and holdout baseline observations, verifies the 12-candidate catalog against the live
source and effective policy, writes a canonical readiness artifact, and reports the exact canary
command. It initializes neither OpenRouter nor the paid-call ledger.

`--optimization-phase canary` requires the same readiness identity, exactly one iteration, at most
three provider calls and USD 0.50, `--apply` absent, and no retry or second sample. It performs this
closed sequence:

1. Orchestrator returns only `continue` or `abort`, a bounded diagnosis domain, and cited evidence
   IDs. It cannot select a parameter, value, file, or edit.
2. Reasoner selects exactly one candidate ID from the controller-owned catalog and cites the
   supplied evidence and invariant IDs. It cannot invent a value, edit, file, or external fact.
3. Coder reproduces the exact controller-owned one-line replacement associated with that selected
   candidate. It cannot choose or alter the replacement.
4. Controller validates every schema and citation, exact replacement, one-leaf policy delta, and
   unchanged causal invariants.
5. Controller evaluates the candidate in a disposable source copy against the full PIT window.
   The 2025 holdout is sliced from that same full-window equity/trade/weekly/funnel evidence so it
   inherits the full warmup and cannot see later data.
6. Controller applies deterministic acceptance gates and writes only private audit records,
   aggregate comparison artifacts, and an inert diff. The source worktree remains unchanged.

## Candidate catalog

The editable scope is exactly `core/canslim/entry_contract.py`. Each candidate changes one existing
constant line and nothing else:

| Candidate IDs | Baseline | Alternatives |
|---|---:|---:|
| `min_current_growth_020`, `min_current_growth_030` | `MIN_CURRENT_GROWTH = 0.25` | `0.20`, `0.30` |
| `min_annual_growth_020`, `min_annual_growth_030` | `MIN_ANNUAL_GROWTH = 0.25` | `0.20`, `0.30` |
| `min_rs_score_075`, `min_rs_score_085` | `MIN_RS_SCORE = 80.0` | `75.0`, `85.0` |
| `min_composite_score_065`, `min_composite_score_075` | `MIN_COMPOSITE_SCORE = 70.0` | `65.0`, `75.0` |
| `min_volume_ratio_120`, `min_volume_ratio_140` | `MIN_VOLUME_RATIO = 1.30` | `1.20`, `1.40` |
| `max_buy_zone_extension_003`, `max_buy_zone_extension_007` | `MAX_BUY_ZONE_EXTENSION = 0.05` | `0.03`, `0.07` |

Preparation fails if any original line is absent or duplicated, if the corresponding effective
policy field is not active and marked as an optimizer candidate, or if the policy source differs
from the canonical entry-contract constant.

## Observations and privacy

The canonical observation contains only aggregate, finite data:

- source, bundle, baseline-manifest, and effective-policy identities;
- strategy and SPY return, annualized return, drawdown, Sharpe, excess return, ending equity,
  and total P&L;
- closed/open trades, wins, losses, win rate, mean/median return, mean win, mean loss,
  expectancy, and mean/median calendar and trading-session holding periods;
- terminal exit attribution and separate scale-out SELL count, quantity, and proceeds;
- weekly average/minimum/maximum cash, average exposure, and average/maximum holding count;
- full entry funnel, rejection outcomes, and stage counts for technical setup, C, A, RS,
  composite, qualified, attempted, and executed entries;
- the unchanged leader-basket benchmark metrics from the sealed authority.

No ticker, price row, filing fact, transaction row, raw source snapshot, provider response, or
secret is placed in provider evidence or the public summary. Provider payloads receive only the
bounded aggregate observation, stable evidence/invariant IDs, catalog IDs, and one controller-owned
replacement after the reasoner has selected it.

## Objective and acceptance

For either window:

```text
objective = annualized_return_pct - abs(min(max_drawdown_pct, 0))
```

The full-window candidate passes only when all conditions hold:

- objective delta is at least `+0.25` percentage points;
- total return is no more than `0.50` points below baseline;
- drawdown magnitude deteriorates by no more than `0.50` points;
- Sharpe is no more than `0.05` below baseline; and
- closed trades are at least `132`.

The 2025 holdout passes only when all conditions hold:

- objective delta is nonnegative;
- total return is no more than `0.50` points below baseline;
- drawdown magnitude deteriorates by no more than `0.50` points;
- Sharpe is no more than `0.05` below baseline; and
- closed trades are at least `max(5, floor(0.5 * baseline_holdout_closed_trades))`.

Acceptance is the conjunction of the full-window and holdout gates. Rejection is a useful
completed cycle and still exports the inert diff and evidence; it never mutates the source.

## Failure behavior

Malformed or duplicate JSON, an unknown ID, ungrounded citation, policy mismatch, changed input,
non-finite metric, wrong date, extra replacement, missing accounting, provider failure, budget
overflow, candidate mutation, subprocess failure, or artifact mismatch terminates the one cycle
closed. There are no automatic provider retries and no fallback to live market or fundamental data.

## Verification

Focused tests cover catalog closure, strict role schemas, effective-policy and causal-invariant
delta checks, baseline authentication, holdout slicing, metric aggregation, objective boundaries,
and prepare-mode no-provider behavior. Offline readiness must also compile the changed modules,
run the focused suite, verify the real sealed inputs, and emit a canonical readiness artifact.
