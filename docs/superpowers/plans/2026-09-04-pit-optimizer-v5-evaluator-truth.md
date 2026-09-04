# PIT Optimizer V5 Evaluator Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create one versioned, causal, cost-aware evaluator and baseline authority that every V5 baseline, candidate, confirmation, and qualification comparison uses.

**Architecture:** Add pure fill/friction primitives and a V5 execution profile while leaving legacy simulator defaults reproducible. V5 queues close-derived policy exits to the next eligible open, models gap-through stops and declared friction, injects authenticated ticker transitions, and returns a rich scenario-bound report. Capture a new baseline authority only after these semantics are deterministic.

**Tech Stack:** Python 3.13, frozen dataclasses, Literal and Protocol typing, pandas, existing `PortfolioSimulator`, SQLite PIT bundles, canonical JSON identities, pytest, Ruff, and ignored local `.artifacts` evidence.

**Spec:** `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`

## Global Constraints

- Preserve legacy execution as the default for V2-V4 artifact reproduction; V5 must opt into its execution profile explicitly.
- Use the same exact execution-profile and friction-scenario identity for baseline and candidate comparisons.
- Apply ticker-identity transitions before entries, exits, stops, marking, or terminal liquidation on each session.
- A decision derived from a completed close cannot fill at that close.
- A long stop gaps at the session open when `open <= stop`; otherwise it fills at the stop only when `low <= stop`.
- Include friction in affordability, position cost basis, proceeds, realized P&L, equity, and diagnostics.
- Retain no-leverage and exact cash/accounting authority in the engine.
- Keep gross zero-friction evidence for diagnosis, but select using the declared base-cost scenario.
- Do not reuse either legacy baseline authority as V5 performance evidence.
- Keep full evidence local; provider-facing reports contain bounded aggregates only.
- Add only narrow evaluator contract checks and one provider-free subset proof, not a broad regression expansion.
- Do not make provider calls, open qualification, or start full replay.

## File Structure and Responsibility Map

- `core/backtest_fills.py`: pure stop-reference, friction, and V5 execution-profile records.
- `core/backtest_engine.py`: versioned pending-exit and fill lifecycle.
- `core/pit_optimizer_v5/contracts.py`: execution and report identities shared with later plans.
- `core/pit_optimizer_v5/evaluator.py`: the only V5 panel evaluator.
- `core/pit_optimizer_v5/diagnostics.py`: return, trade, exposure, funnel, excursion, and cost attribution.
- `core/pit_optimizer_v5/baseline.py`: create-only V5 baseline authority and deterministic repeat.
- `core/pit_policy_parity.py`: inject authenticated identity transitions in parity evaluation.
- `agent_loop.py`: thin V5 evaluator construction; no evaluation logic.
- `tests/test_backtest_fills.py`: pure fill primitives.
- `tests/test_pit_optimizer_evaluator_v5.py`: authority/evaluator boundary.

---

### Task 1: Implement Pure Fill and Friction Primitives

**Files:**

- Create: `core/backtest_fills.py`
- Create: `tests/test_backtest_fills.py`

**Interfaces:**

- Consumes: side, reference price, quantity, stop/open/low prices, and `FrictionScenario`.
- Produces: `ExecutionFill` and an optional gap/intraday stop reference.

- [ ] **Step 1: Write focused failing fill tests**

Cover zero friction, adverse buy/sell slippage, commission, exact stop touch, and an opening gap:

```python
def test_long_stop_uses_open_when_session_gaps_below_stop() -> None:
    resolved = resolve_long_stop(open_price=91.0, low_price=89.0, stop_price=95.0)
    assert resolved == StopReference(kind="gap_stop", price=91.0)

def test_sell_friction_reduces_cash_proceeds() -> None:
    fill = apply_friction(
        side="SELL", reference_price=100.0, quantity=10.0,
        scenario=FrictionScenario("base", 2, 3, 0),
    )
    assert fill.execution_price == 99.95
    assert fill.cash_delta == 999.5
```

- [ ] **Step 2: Run the tests and verify the module is absent**

```powershell
python -B -m pytest tests/test_backtest_fills.py -q
```

Expected: import failure for `core.backtest_fills`.

- [ ] **Step 3: Implement immutable primitives**

```python
@dataclass(frozen=True, slots=True)
class FrictionScenario:
    scenario_id: str
    half_spread_bps: int
    market_impact_bps: int
    commission_bps: int

@dataclass(frozen=True, slots=True)
class StopReference:
    kind: Literal["gap_stop", "intraday_stop"]
    price: float

@dataclass(frozen=True, slots=True)
class ExecutionFill:
    side: Literal["BUY", "SELL"]
    reference_price: float
    execution_price: float
    quantity: float
    gross_value: float
    commission_usd: float
    spread_cost_usd: float
    market_impact_cost_usd: float
    cash_delta: float

def resolve_long_stop(*, open_price: float, low_price: float,
                      stop_price: float) -> StopReference | None: ...

def apply_friction(*, side: Literal["BUY", "SELL"], reference_price: float,
                   quantity: float, scenario: FrictionScenario) -> ExecutionFill: ...
```

Reject Boolean numerics, non-finite or non-positive price/quantity, negative basis points, and
unknown sides. Round only at the final currency/accounting boundary; do not round execution prices
to cents internally.

- [ ] **Step 4: Run focused checks**

```powershell
python -B -m pytest tests/test_backtest_fills.py -q
python -B -m ruff check core/backtest_fills.py tests/test_backtest_fills.py
```

- [ ] **Step 5: Commit fill primitives**

```powershell
git add core/backtest_fills.py tests/test_backtest_fills.py
git commit -m "feat: add causal backtest fill model"
```

### Task 2: Introduce the V5 Execution Profile and Pending Policy Exits

**Files:**

- Modify: `core/backtest_fills.py`
- Modify: `core/backtest_engine.py:185-405`
- Modify: `core/backtest_engine.py:1213-1489`
- Modify: `core/backtest_engine.py:1549-2218`
- Modify: `core/backtest_engine.py:3292-3907`
- Modify: `tests/test_backtest_engine.py:1467-1510`
- Modify: `tests/test_backtest_open_causality.py:289-380`
- Modify: `tests/test_backtest_pnl.py:428-500`

**Interfaces:**

- Consumes: `ExecutionProfileV5` from `core/backtest_fills.py`, queued `PendingPolicyExit`, and `FrictionScenario`.
- Produces: causal fills, cost rows, persisted pending state, and V5 simulation results while preserving legacy defaults.

- [ ] **Step 1: Replace the focused same-close expectation**

Update the existing policy-exit test to assert that a close-derived exit on session `t` remains open
at that close and fills at session `t+1` open. Add a second case where a gap stop on `t+1` takes
priority over the queued policy exit.

- [ ] **Step 2: Add execution-profile and pending-exit records**

Define `ExecutionProfileV5` beside `FrictionScenario` in `core/backtest_fills.py` and define
`PendingPolicyExit` beside the engine's other pending-action records:

```python
@dataclass(frozen=True, slots=True)
class ExecutionProfileV5:
    schema_version: Literal[5]
    close_policy_exit_timing: Literal["next_open"]
    gap_stop_rule: Literal["open_then_stop"]
    end_of_test_rule: Literal["last_session_close"]
    friction_model: Literal["half_spread_plus_market_impact_plus_commission_bps"]

@dataclass(frozen=True, slots=True)
class PendingPolicyExit:
    symbol: str
    signal_date: str
    target_entry_date: str
    actions: tuple[ExitAction, ...]
```

Add `execution_profile: ExecutionProfileV5 | None = None` and
`friction_scenario: FrictionScenario | None = None` to `PortfolioSimulator.__init__`. Both must be
present for V5; both absent preserves the legacy V2-V4 path. Reject partial combinations.
Derive `execution_profile_sha256` only from canonical serialization of every profile field; never
accept a caller-supplied identity label.

- [ ] **Step 3: Reorder the V5 session lifecycle**

For V5 only, process each session in this exact order:

1. apply identity transitions and remap pending symbols;
2. apply opening-gap stops to positions held before the open and cancel their pending exits/add-ons;
3. execute surviving prior-session policy exits at the current open and cancel conflicting add-ons;
4. execute pending new entries and surviving add-ons at the current open;
5. apply remaining intraday stop touches, including stops on positions opened that session;
6. after the close, update position state and queue policy exits; and
7. evaluate new entries and add-ons after the close.

Price-triggered intraday scale-outs may fill when their declared threshold is actually touched;
time-stop, moving-average, and other close-derived actions queue to the next open.

- [ ] **Step 4: Apply friction to every buy and sell**

Use `ExecutionFill.cash_delta` for cash accounting. Entry sizing must include expected friction and
use actual fill price for stop-distance risk. Record the reference price, actual fill, commission,
and slippage in a V5-only fill log while keeping the existing transaction columns readable.

- [ ] **Step 5: Version checkpoint state**

Bump only the V5 checkpoint schema and bind execution-profile identity. Persist pending exits and
fill-cost totals through `_portfolio_checkpoint_fingerprint`, `_checkpoint_payload`, and restore.
Legacy checkpoint schemas must retain their old parser and behavior.

- [ ] **Step 6: Run focused engine checks**

```powershell
python -B -m pytest tests/test_backtest_engine.py -k "policy_exit or stop" -q
python -B -m pytest tests/test_backtest_open_causality.py tests/test_backtest_pnl.py -k "exit or stop or gap" -q
python -B -m ruff check core/backtest_fills.py core/backtest_engine.py
```

- [ ] **Step 7: Commit V5 execution semantics**

```powershell
git add core/backtest_fills.py core/backtest_engine.py tests/test_backtest_engine.py tests/test_backtest_open_causality.py tests/test_backtest_pnl.py
git commit -m "feat: add optimizer v5 execution profile"
```

### Task 3: Define V5 Evaluator and Evidence Contracts

**Files:**

- Create: `core/pit_optimizer_v5/__init__.py`
- Create: `core/pit_optimizer_v5/contracts.py`
- Create: `core/pit_optimizer_v5/evaluator.py`
- Create: `tests/test_pit_optimizer_evaluator_v5.py`

**Interfaces:**

- Consumes: authenticated bundle/provenance, panel, policy identity, execution profile, and worker factory.
- Produces: `EvaluatorContractV5`, `PanelEvaluationV5`, and scenario-bound evidence.

- [ ] **Step 1: Add failing contract tests**

Require a V5 evaluator contract to bind bundle, prices provenance, identity-transition contract,
baseline policy, execution profile, digest-pinned sandbox profile, scenarios, and selected scenario.
Individual panel evaluations bind their own panel and sandbox identities. Reject a selected scenario
absent from the grid, a resource/sandbox mismatch, and any schema-V4 aggregate passed as V5 evidence.

- [ ] **Step 2: Define evaluator contracts**

```python
@dataclass(frozen=True, slots=True)
class SandboxProfileV5:
    schema_version: Literal[5]
    image_name: str
    image_digest: str
    runtime_source_sha256: str
    network_mode: Literal["none"]
    root_filesystem: Literal["read_only"]
    source_mount_mode: Literal["read_only"]
    data_mount_mode: Literal["read_only"]
    output_mode: Literal["bounded_write_only"]
    cpu_limit: Decimal
    memory_limit_mib: int
    output_limit_bytes: int

@dataclass(frozen=True, slots=True)
class EvaluatorContractV5:
    schema_version: Literal[5]
    execution_profile_sha256: str
    sandbox_profile_sha256: str
    evaluator_source_sha256: str
    pit_bundle_sha256: str
    prices_provenance_sha256: str
    identity_transition_contract_sha256: str
    baseline_source_bundle_sha256: str
    friction_grid: tuple[FrictionScenario, ...]
    selection_scenario_id: str
    benchmark: Literal["SPY"] = "SPY"
    signal_every_n_days: Literal[1] = 1

@dataclass(frozen=True, slots=True)
class MetricCountV5:
    metric_id: str
    count: int

@dataclass(frozen=True, slots=True)
class SliceMetricsV5:
    annualized_return_pct: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    closed_trades: int
    average_exposure_pct: Decimal

@dataclass(frozen=True, slots=True)
class EvaluationSliceV5:
    dimension: Literal["regime", "episode", "calendar_year"]
    label: str
    metrics: SliceMetricsV5

@dataclass(frozen=True, slots=True)
class RollingReturnV5:
    window_months: Literal[12, 24, 36]
    end_date: str
    total_return_pct: Decimal
    annualized_return_pct: Decimal

@dataclass(frozen=True, slots=True)
class DistributionSummaryV5:
    minimum: Decimal
    p25: Decimal
    median: Decimal
    p75: Decimal
    maximum: Decimal

@dataclass(frozen=True, slots=True)
class EvaluationReportV5:
    portfolio_annualized_return_pct: Decimal
    portfolio_total_return_pct: Decimal
    gross_annualized_return_pct: Decimal
    benchmark_annualized_return_pct: Decimal
    benchmark_total_return_pct: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal
    closed_trades: int
    average_exposure_pct: Decimal
    average_cash_pct: Decimal
    turnover_pct: Decimal
    total_friction_usd: Decimal
    friction_drag_pct: Decimal
    win_rate_pct: Decimal | None
    average_win_pct: Decimal | None
    average_loss_pct: Decimal | None
    payoff_ratio: Decimal | None
    expectancy_pct: Decimal | None
    median_holding_sessions: Decimal | None
    invested_sleeve_annualized_return_pct: Decimal | None
    estimated_idle_cash_drag_pct: Decimal
    stop_gap_shortfall_usd: Decimal
    identity_transition_count: int
    scale_out_opportunity_cost_pct: Decimal | None
    maximum_favorable_excursion_pct: DistributionSummaryV5 | None
    maximum_adverse_excursion_pct: DistributionSummaryV5 | None
    entry_funnel: tuple[MetricCountV5, ...]
    exit_attribution: tuple[MetricCountV5, ...]
    policy_intent_outcomes: tuple[MetricCountV5, ...]
    regime_slices: tuple[EvaluationSliceV5, ...]
    episode_slices: tuple[EvaluationSliceV5, ...]
    calendar_year_slices: tuple[EvaluationSliceV5, ...]
    rolling_returns: tuple[RollingReturnV5, ...]

@dataclass(frozen=True, slots=True)
class ScenarioPanelEvaluationV5:
    scenario_id: str
    starting_equity: Decimal
    ending_equity: Decimal
    report: EvaluationReportV5

@dataclass(frozen=True, slots=True)
class PanelEvaluationV5:
    evaluator_contract_sha256: str
    sandbox_profile_sha256: str
    panel_sha256: str
    policy_identity_sha256: str
    start_date: str
    end_date: str
    elapsed_calendar_days: int
    selection_scenario_id: str
    scenarios: tuple[ScenarioPanelEvaluationV5, ...]
```

Derive `sandbox_profile_sha256` from canonical serialization of every field, require the image
reference to be digest-pinned, and require its CPU/memory/output values to equal the campaign
resource manifest. Networking and read-only invariants are not configurable. Reject duplicate
scenario IDs and require `selection_scenario_id` to resolve exactly once. Add a
pure `selected_scenario(evaluation: PanelEvaluationV5) -> ScenarioPanelEvaluationV5` helper so later
search code never guesses a tuple position.

The initial grid is explicit manifest data: `gross=(0,0,0)`, `base=(2,3,0)`, and
`stress=(5,10,1)` basis points for half-spread, market impact/slippage, and commission. Thus the
base scenario applies five adverse basis points per side and stress applies fifteen. `base` is the
selection scenario until paper fills support a calibrated replacement.

Derive `evaluator_source_sha256` from a canonical path-to-byte-digest map covering the backtest
engine/fill primitives, PIT data and feature adapters, policy worker/runtime contracts, and V5
evaluator/diagnostic modules. Documentation and unrelated repository files do not change this
semantic identity; any listed source byte does.

- [ ] **Step 3: Implement the single evaluator service**

```python
class PitPanelEvaluatorV5:
    def evaluate_baseline(
        self, panel: EvaluationPanelSpec, *, scenario_ids: tuple[str, ...],
    ) -> PanelEvaluationV5: ...
    def evaluate_candidate(
        self, *, candidate_root: Path, panel: EvaluationPanelSpec,
        policy_identity_sha256: str, worker_factory: StrategyPolicyClientFactory,
        scenario_ids: tuple[str, ...],
    ) -> PanelEvaluationV5: ...
```

Load the authenticated price-identity transition contract once during evaluator construction.
Create a fresh simulator and policy worker for each panel/scenario. Pass the transition contract and
execution profile explicitly. Refuse missing or mismatched provenance before simulation. Require the
requested scenario IDs to be a canonical non-empty subset of the contract grid containing the
selection scenario. Quick screens request only `base`; discovery and authority capture request all
three scenarios.

- [ ] **Step 4: Run focused evaluator checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_evaluator_v5.py -q
python -B -m ruff check core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/evaluator.py
```

- [ ] **Step 5: Commit evaluator contracts**

```powershell
git add core/pit_optimizer_v5/__init__.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/evaluator.py tests/test_pit_optimizer_evaluator_v5.py
git commit -m "feat: add optimizer v5 evaluator contract"
```

### Task 4: Add Rich Causal Diagnostics

**Files:**

- Create: `core/pit_optimizer_v5/diagnostics.py`
- Modify: `core/backtest_engine.py:405-475`
- Modify: `core/pit_optimizer_v5/contracts.py`
- Modify: `tests/test_pit_optimizer_evaluator_v5.py`

**Interfaces:**

- Consumes: panel, `SimulationResult`, V5 fill log, entry outcomes, position episodes, regime sessions, and friction scenario.
- Produces: `EvaluationReportV5` with exact reconciliation and bounded provider projection.

- [ ] **Step 1: Add reconciliation assertions**

Use one synthetic winner and one loser. Assert that win/loss expectancy, MFE/MAE, holding duration,
cost drag, entry/exit attribution, and cash/exposure observations reconcile exactly to the result.

- [ ] **Step 2: Record position excursions in `SimulationResult`**

Add V5-only immutable `PositionEpisode` rows containing entry/exit sessions and prices, maximum and
minimum marked price, realized return, quantity path, add-on count, and exit reason. Update extrema
from completed bars while the position is open.

- [ ] **Step 3: Populate the report contract declared in Task 3**

Populate every declared report/slice field from reconciled engine evidence. Also include
invested-sleeve return, cash drag, stop-gap shortfall, identity-transition count,
scale-out opportunity cost, MFE/MAE distributions, and policy-intent delay/unexecuted counts.

- [ ] **Step 4: Implement aggregate and provider projections**

`summarize_panel_result()` returns the complete local report. `to_role_evidence()` removes raw
symbol/session rows and returns bounded aggregate slices with stable evidence IDs.

- [ ] **Step 5: Run focused diagnostics checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_evaluator_v5.py -k "diagnostic or reconcile or role_evidence" -q
python -B -m ruff check core/pit_optimizer_v5/diagnostics.py core/pit_optimizer_v5/contracts.py
```

- [ ] **Step 6: Commit rich diagnostics**

```powershell
git add core/pit_optimizer_v5/diagnostics.py core/pit_optimizer_v5/contracts.py core/backtest_engine.py tests/test_pit_optimizer_evaluator_v5.py
git commit -m "feat: explain optimizer v5 portfolio returns"
```

### Task 5: Inject Identity Transitions into Every Evaluator Path

**Files:**

- Modify: `core/pit_policy_parity.py:1486-1575`
- Modify: `agent_loop.py:21033-21089`
- Modify: `core/pit_optimizer_v5/evaluator.py`
- Modify: `tests/test_pit_policy_parity.py`
- Modify: `tests/test_pit_optimizer_evaluator_v5.py`

**Interfaces:**

- Consumes: authenticated prices provenance and `PriceIdentityTransitionContract`.
- Produces: identical transition handling and evidence identity in parity, baseline, candidate, and later qualification.

- [ ] **Step 1: Add a predecessor-to-successor evaluation case**

Assert that a position opened under a predecessor ticker transfers on its effective date, remains
marked under the successor, and later exits normally. Assert that omitting or substituting the
transition contract fails before simulation.

- [ ] **Step 2: Repair parity and V4 audit evaluation**

Load the transition contract from the exact authenticated prices provenance already bound by the
panel plan and pass it to `PortfolioSimulator`. Add its identity to parity evidence. This corrects
future audit reruns without reinterpreting historical V4 artifacts.

- [ ] **Step 3: Enforce the same binding in V5**

`PitPanelEvaluatorV5` must compare the loaded transition-contract identity to
`EvaluatorContractV5.identity_transition_contract_sha256` before constructing any simulator.

- [ ] **Step 4: Run focused identity checks**

```powershell
python -B -m pytest tests/test_pit_policy_parity.py tests/test_pit_optimizer_evaluator_v5.py -k "transition or identity" -q
python -B -m ruff check core/pit_policy_parity.py core/pit_optimizer_v5/evaluator.py agent_loop.py
```

- [ ] **Step 5: Commit transition parity**

```powershell
git add core/pit_policy_parity.py core/pit_optimizer_v5/evaluator.py agent_loop.py tests/test_pit_policy_parity.py tests/test_pit_optimizer_evaluator_v5.py
git commit -m "fix: bind optimizer evaluations to ticker transitions"
```

### Task 6: Capture One V5 Baseline Authority

**Prerequisite:** Complete strategy/universe Tasks 1-7, learning/search contracts and CLI, and
campaign Task 1. Those tasks produce the V3 baseline policy, authenticated three-universe bundle,
and canonical campaign panel plan that this authority binds.

**Files:**

- Create: `core/pit_optimizer_v5/baseline.py`
- Modify: `core/pit_optimizer_v5/contracts.py`
- Modify: `core/pit_optimizer_v5/cli.py`
- Modify: `tests/test_pit_optimizer_evaluator_v5.py`
- Create locally only: `.artifacts/pit-optimizer-v5/evaluator/`

**Interfaces:**

- Consumes: exact evaluator contract, baseline policy sources, panel plan, and authenticated bundle.
- Produces: `BaselineAuthorityV5` after two byte-identical evaluations.

- [ ] **Step 1: Add authority identity checks**

Assert that changing any execution profile, evaluator source set, friction scenario, transition,
bundle, panel, or baseline-policy identity invalidates the authority and that a legacy authority
cannot deserialize as V5.

- [ ] **Step 2: Define the authority**

```python
@dataclass(frozen=True, slots=True)
class BaselineAuthorityV5:
    schema_version: Literal[5]
    evaluator_contract_ref: ArtifactRefV5
    execution_profile_ref: ArtifactRefV5
    sandbox_profile_ref: ArtifactRefV5
    panel_plan_ref: ArtifactRefV5
    baseline_policy_revision_ref: ArtifactRefV5
    mechanics_evidence_ref: ArtifactRefV5
    quick_evidence_ref: ArtifactRefV5
    discovery_evidence_ref: ArtifactRefV5
    deterministic_repeat_ref: ArtifactRefV5
```

- [ ] **Step 3: Implement create-only capture**

Evaluate the unchanged V5 baseline twice with fresh simulator/worker instances. Canonical reports
must be byte-identical. Every dependency and evidence field is an `ArtifactRefV5` under the
canonical V5 artifact root; `verify-baseline --authority` recursively authenticates this complete
graph before parsing, without sibling-name lookup or directory scanning. The policy-revision
descriptor binds the clean source commit and exact source bundle. Write reports and authority with
create-only semantics; never overwrite or bless an existing mismatch.

- [ ] **Step 4: Add CLI commands**

```powershell
python -B -m core.pit_optimizer_v5.cli write-execution-profile --help
python -B -m core.pit_optimizer_v5.cli build-sandbox-profile --help
python -B -m core.pit_optimizer_v5.cli capture-baseline --help
python -B -m core.pit_optimizer_v5.cli verify-baseline --help
```

`write-execution-profile` emits the one canonical V5 execution profile with create-only semantics;
`build-sandbox-profile` builds/resolves the digest-pinned evaluator image and emits its canonical
sandbox contract. Capture and verification authenticate every input before parsing evidence;
verification is read-only.

- [ ] **Step 5: Run focused authority checks**

```powershell
python -B -m pytest tests/test_pit_optimizer_evaluator_v5.py -k "baseline or deterministic" -q
python -B -m compileall -q core/pit_optimizer_v5
python -B -m ruff check core/pit_optimizer_v5
```

- [ ] **Step 6: Commit baseline authority**

```powershell
git add core/pit_optimizer_v5/baseline.py core/pit_optimizer_v5/contracts.py core/pit_optimizer_v5/cli.py tests/test_pit_optimizer_evaluator_v5.py
git commit -m "feat: add optimizer v5 baseline authority"
```

### Task 7: Prove Provider-Free Evaluator Determinism

**Files:**

- Create locally only: `.artifacts/pit-optimizer-v5/evaluator/`

**Interfaces:**

- Consumes: local authenticated PIT bundle and unchanged baseline sources.
- Produces: canonical execution/sandbox profiles, V5 authority, gross/base/stress reports,
  deterministic repeat, and content-free summary.

- [ ] **Step 1: Create the canonical execution and sandbox-profile artifacts**

```powershell
python -B -m core.pit_optimizer_v5.cli write-execution-profile --output .artifacts/pit-optimizer-v5/evaluator/execution-profile.json
python -B -m core.pit_optimizer_v5.cli build-sandbox-profile --dockerfile Dockerfile.agent-loop --cpu-limit 1 --memory-mib 1024 --output-limit-bytes 67108864 --output .artifacts/pit-optimizer-v5/evaluator/sandbox-profile.json
```

Expected: two create-only canonical artifacts whose digests are derived from their complete contents.
The sandbox builder records the built image's immutable digest and evaluator runtime-source digest;
readiness refuses a mutable tag, host-network mode, writable source/data mount, or unbounded output.

- [ ] **Step 2: Capture the V5 baseline on mechanics, quick, and all four discovery episodes**

```powershell
python -B -m core.pit_optimizer_v5.cli capture-baseline --bundle .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3 --prices-provenance .artifacts/pit-optimizer-v5/data/prices_provenance.json --panel-plan .artifacts/pit-optimizer-v5/panels/discovery-plan.json --policy-root core/strategy_policy/v3 --execution-profile .artifacts/pit-optimizer-v5/evaluator/execution-profile.json --sandbox-profile .artifacts/pit-optimizer-v5/evaluator/sandbox-profile.json --output-root .artifacts/pit-optimizer-v5/evaluator
```

Expected: fresh `gross`, `base`, and `stress` scenario evidence for mechanics, quick, and the four
canonical discovery episodes plus an authenticated deterministic repeat; existing outputs are never
overwritten. Confirmation and qualification remain unevaluated.

- [ ] **Step 3: Verify the authority independently**

```powershell
python -B -m core.pit_optimizer_v5.cli verify-baseline --authority .artifacts/pit-optimizer-v5/evaluator/baseline-authority.json
```

Expected: transition identity, execution profile, sandbox/image/runtime identity, all three friction
scenarios, source, mechanics, panel, and deterministic repeat agree.

- [ ] **Step 4: Review the content-free diagnostics**

```powershell
python -B -m core.pit_optimizer_v5.cli summarize --artifact-root .artifacts/pit-optimizer-v5/evaluator
```

Expected: gross/base/stress CAGR and cost drag, activity, cash/exposure, expectancy, stops, identity
transfers, and exit opportunity cost; zero provider calls and no candidate evaluation.

## Completion Criteria

- Legacy execution remains reproducible when no V5 profile is supplied.
- V5 close-derived exits execute at the next open and gap stops fill at the adverse open.
- Friction reconciles through cash, positions, realized P&L, and equity.
- Every V5 panel evaluator injects and authenticates ticker transitions.
- Rich reports explain return, activity, exposure, costs, excursions, regimes, and exit opportunity cost.
- One create-only V5 baseline authority reproduces byte-for-byte under fresh simulator instances.
- No model call, qualification access, or full replay occurs.
