# PIT Optimizer V5 Strategy and Three-Universe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give V5 a causal adaptive O'Neil policy interface, explicit winner pyramiding, and an authenticated point-in-time S&P 500/Nasdaq-100/Russell 2000 opportunity set.

**Architecture:** Add bundle schema V3 and policy interface V3 beside the legacy readers. The bundle stores per-universe membership events and derives a deduplicated active security-lineage union; trusted feature adapters expose causal leadership, growth, price/volume, volatility, and portfolio facts to four model-editable policy modules. Existing engine accounting remains authoritative while a new add-on lifecycle permits bounded O'Neil pyramiding.

**Tech Stack:** Python 3.13, frozen dataclasses, pandas, SQLite, canonical CSV/JSON provenance, existing PIT price/fundamental/industry builders, strategy-policy worker protocol, and focused pytest/Ruff checks.

**Spec:** `docs/superpowers/specs/2026-09-04-pit-optimizer-v5-hybrid-architecture-design.md`

## Global Constraints

- Preserve bundle schemas V1-V2 and policy interfaces V1-V2 as read-only compatible paths.
- Keep all source market data and built bundles below ignored `.artifacts/pit-optimizer-v5/data/`; commit only code and synthetic fixtures.
- Membership and features must be point-in-time; no current-constituent fallback is allowed.
- Treat SPY, QQQ, and IWM as observation-only reference symbols.
- Deduplicate securities shared by multiple universes while retaining their complete affiliation set.
- Keep O'Neil concepts central: earnings and sales growth, leadership, base/pivot/buy zone, volume confirmation, market direction, cutting losses, and preserving exceptional winners.
- Do not introduce leverage. The engine remains final authority for cash, aggregate position size, and risk.
- Give model-authored policy code broad logical freedom inside deterministic, pure interfaces.
- Use no opaque weighted strategy score.
- Do not make provider calls or open qualification in this plan.
- Update focused existing test modules only; do not create a broad new test suite.

## File Structure and Responsibility Map

- `core/pit_universe_v3.py`: immutable affiliation events and active-union queries.
- `normalize_pit_universe_membership.py`: merge three canonical local membership inputs and provenance sidecars.
- `build_pit_bundle.py`, `verify_pit_bundle.py`: build and verify schema V3 tables and metadata.
- `core/pit_data.py`: dispatch legacy and V3 bundle readers and expose affiliation queries.
- `core/pit_feature_snapshot.py`: compute symbol-neutral causal strategy features from the authenticated bundle.
- `core/strategy_policy/contracts_v3.py`: policy interface V3 snapshots and decisions.
- `core/strategy_policy/adapter_v3.py`: build V3 policy snapshots from trusted engine state.
- `core/strategy_policy/runtime.py`, `worker.py`: negotiate policy interface V3 without changing legacy framing.
- `core/strategy_policy/v3/entry.py`: entry qualification and ranking.
- `core/strategy_policy/v3/risk.py`: capacity, allocation, and eviction.
- `core/strategy_policy/v3/position.py`: existing-position add-on decisions.
- `core/strategy_policy/v3/exit.py`: winner retention and exits.
- `core/backtest_engine.py`: invoke V3 policy adapters and execute pending add-ons.
- `core/pit_optimizer_v5/policy_scope.py`: the exact four editable V5 policy paths and exported functions.

---

### Task 1: Add the Three-Universe Membership Domain

**Files:**

- Create: `core/pit_universe_v3.py`
- Modify: `core/pit_data.py:132-457`
- Modify: `tests/test_pit_data.py`

**Interfaces:**

- Consumes: canonical membership rows `(effective_date, security_lineage_id, universe_id, member)`
  plus the authenticated ticker-to-lineage transition contract.
- Produces: `UniverseMembershipEventV3`, ticker-compatible `members_at()`/`affiliations_at()`, and
  lineage-native `lineages_at()`/`lineage_affiliations_at()`/`ticker_for_lineage_at()` APIs.

- [ ] **Step 1: Add focused failing assertions for overlaps and historical removals**

Add cases to `tests/test_pit_data.py` that seed one lineage under ticker `DUAL` in both `sp500` and
`nasdaq100`, remove only its S&P affiliation, and assert that it remains in the active union with only
the Nasdaq affiliation. Add a later authenticated `DUAL -> DUAL2` same-lineage transition and assert
that panel identity remains the lineage while the tradable ticker changes.

```python
assert universe.members_at("2024-06-03") == frozenset({"DUAL"})
assert universe.affiliations_at("2024-06-03")["DUAL"] == frozenset({"nasdaq100"})
assert universe.lineages_at("2024-06-03") == frozenset({"lineage_dual"})
assert universe.ticker_for_lineage_at("lineage_dual", "2024-07-01") == "DUAL2"
```

- [ ] **Step 2: Run the focused test and verify the V3 type is absent**

```powershell
python -B -m pytest tests/test_pit_data.py -q
```

Expected: failure importing `PointInTimeUniverseV3`.

- [ ] **Step 3: Implement the closed affiliation model**

Use these public types in `core/pit_universe_v3.py`:

```python
UNIVERSE_IDS = frozenset({"sp500", "nasdaq100", "russell2000"})

@dataclass(frozen=True, slots=True)
class UniverseMembershipEventV3:
    effective_date: date
    security_lineage_id: str
    universe_id: str
    member: bool

class PointInTimeUniverseV3:
    def members_at(self, as_of: str | date) -> frozenset[str]: ...
    def affiliations_at(self, as_of: str | date) -> Mapping[str, frozenset[str]]: ...
    def lineages_at(self, as_of: str | date) -> frozenset[str]: ...
    def lineage_affiliations_at(
        self, as_of: str | date,
    ) -> Mapping[str, frozenset[str]]: ...
    def ticker_for_lineage_at(self, lineage_id: str, as_of: str | date) -> str: ...
    def all_lineage_ids(self) -> frozenset[str]: ...
    def all_tickers(self) -> frozenset[str]: ...
```

Reject duplicate transitions, a removal without an active membership, unknown universe IDs, and
non-canonical ordering. Resolve the active ticker only through the exact authenticated
`PriceIdentityTransitionContract`; reject a lineage with zero or two active price identities at a
queried post-transition instant. Compute `members_at` from the lineage union, never by concatenating
three lists.

- [ ] **Step 4: Make `PITDataBundle` dispatch schema V3 without changing V2 behavior**

When metadata `schema_version == "3"`, load `membership_v3`; otherwise keep the current
`membership` loader. Add:

```python
def affiliations_at(self, as_of: str | date) -> Mapping[str, frozenset[str]]:
    if self.metadata["schema_version"] != "3":
        return {ticker: frozenset({"sp500"}) for ticker in self.members_at(as_of)}
    return self.membership_v3.affiliations_at(as_of)

def security_lineages_at(self, as_of: str | date) -> frozenset[str]:
    if self.metadata["schema_version"] != "3":
        return frozenset(self.security_lineage_id(ticker) for ticker in self.members_at(as_of))
    return self.membership_v3.lineages_at(as_of)
```

Load and authenticate the price-identity contract before constructing `membership_v3`; V3 bundle
initialization fails if lineage bindings and price identities differ. For schema V3,
`load_price_identity_transition_contract()` reads the explicit authenticated transition rows already
sealed by prices provenance instead of inferring renames from ticker membership add/remove pairs.
Keep the current V1-V2 inference path byte-compatible.

- [ ] **Step 5: Run the focused data checks**

```powershell
python -B -m pytest tests/test_pit_data.py -q
python -B -m ruff check core/pit_universe_v3.py core/pit_data.py tests/test_pit_data.py
```

- [ ] **Step 6: Commit the membership domain**

```powershell
git add core/pit_universe_v3.py core/pit_data.py tests/test_pit_data.py
git commit -m "feat: add PIT three-universe membership domain"
```

### Task 2: Normalize and Seal Local Membership Inputs

**Files:**

- Create: `normalize_pit_universe_membership.py`
- Modify: `build_pit_bundle.py:16-553`
- Modify: `verify_pit_bundle.py`
- Modify: `tests/test_pit_data.py`

**Interfaces:**

- Consumes: three local CSVs with exact columns `effective_date,ticker,member`, three canonical
  provenance JSON objects, and the authenticated price-identity contract.
- Produces: `membership_v3.csv`, `membership_v3_provenance.json`, create-only V3 verification
  evidence, and bundle table
  `membership_v3(effective_date,security_lineage_id,universe_id,member)`.

- [ ] **Step 1: Add a synthetic three-source normalization case**

Extend `tests/test_pit_data.py` with tiny inputs and require exact output columns:

```text
effective_date,security_lineage_id,universe_id,member
```

Assert deterministic ordering by `(effective_date, security_lineage_id, universe_id)` and explicit
overlap rows. When a source removes a predecessor and adds its authenticated successor on the same
date in the same universe, coalesce that same-lineage rename into no membership-state change and
record the coalescing count in provenance.

- [ ] **Step 2: Implement the normalizer**

The CLI arguments are:

```text
--sp500-membership --sp500-provenance
--nasdaq100-membership --nasdaq100-provenance
--russell2000-membership --russell2000-provenance
--prices-provenance
--output-csv --output-provenance
```

Resolve every source ticker through the price provenance's authenticated `chain_id`. The output
provenance binds each input byte identity, price-identity contract, declared source kind and
retrieval time, merged row count, universe/lineage counts, and merged CSV identity. Refuse output
overwrite or any ticker absent from the price identity contract.

- [ ] **Step 3: Extend the bundle builder**

For schema V3 create:

```sql
CREATE TABLE membership_v3 (
  effective_date TEXT NOT NULL,
  security_lineage_id TEXT NOT NULL,
  universe_id TEXT NOT NULL,
  member INTEGER NOT NULL,
  PRIMARY KEY (effective_date, security_lineage_id, universe_id)
);
```

Replace metadata `source_universe=sp500` with canonical
`source_universes_json=["nasdaq100","russell2000","sp500"]`. Require price identities to cover the
deduplicated membership union plus SPY, QQQ, and IWM.

- [ ] **Step 4: Extend verification**

Verify each universe independently, then verify the union, overlaps, reference separation, causal
seed state, provenance bindings, and exact price/fundamental symbol coverage. Do not impose the
S&P-specific 495-through-510 count on other universes. Extend `verify_pit_bundle.py` with
`--industry-csv`, `--industry-provenance`, and `--report-output`; the report is canonical,
create-only, and binds every checked input and the bundle identity.

- [ ] **Step 5: Run focused normalization and bundle checks**

```powershell
python -B -m pytest tests/test_pit_data.py -q
python -B -m ruff check normalize_pit_universe_membership.py build_pit_bundle.py verify_pit_bundle.py
```

- [ ] **Step 6: Commit the V3 builder**

```powershell
git add normalize_pit_universe_membership.py build_pit_bundle.py verify_pit_bundle.py tests/test_pit_data.py
git commit -m "feat: build authenticated three-universe PIT bundles"
```

### Task 3: Build Causal O'Neil Feature Snapshots

**Files:**

- Create: `core/pit_feature_snapshot.py`
- Modify: `core/industry_group.py`
- Modify: `core/canslim/l_leader_laggard.py`
- Modify: `tests/test_strategy_policy.py`

**Interfaces:**

- Consumes: `PITDataBundle`, completed session, active union, price history, RS snapshot, and public fundamentals as of that session.
- Produces: `EntryFeaturesV3` and `HoldingFeaturesV3` with no future data.

- [ ] **Step 1: Add causal feature assertions**

Add synthetic rows where a later fundamental filing and a later industry assignment exist. Assert
that the earlier snapshot excludes both later facts and that all numeric missingness is explicit.

- [ ] **Step 2: Define the feature records**

```python
@dataclass(frozen=True, slots=True)
class EntryFeaturesV3:
    affiliations: tuple[str, ...]
    industry_group_rs: float | None
    sector_rs: float | None
    earnings_growth_acceleration: float | None
    sales_growth_acceleration: float | None
    fundamental_age_days: int | None
    atr_20_fraction: float | None
    breakout_gap_fraction: float | None
    average_dollar_volume_50: float | None
    distance_from_52_week_high_fraction: float | None

@dataclass(frozen=True, slots=True)
class HoldingFeaturesV3:
    current_rs_score: float | None
    industry_group_rs: float | None
    atr_20_fraction: float | None
    volume_ratio: float | None
```

- [ ] **Step 3: Implement causal builders**

Expose:

```python
def build_entry_features_v3(*, bundle: PITDataBundle, symbol: str, session: date,
                            price_history: pd.DataFrame, rs_snapshot: Mapping[str, float]) -> EntryFeaturesV3: ...

def build_holding_features_v3(*, bundle: PITDataBundle, symbol: str, session: date,
                              price_history: pd.DataFrame, rs_snapshot: Mapping[str, float]) -> HoldingFeaturesV3: ...
```

Use only rows dated or publicly available on `session`. Industry/group ranks use the complete active
PIT union, not the candidate panel.

- [ ] **Step 4: Run focused causality checks**

```powershell
python -B -m pytest tests/test_strategy_policy.py -q
python -B -m ruff check core/pit_feature_snapshot.py core/industry_group.py core/canslim/l_leader_laggard.py
```

- [ ] **Step 5: Commit causal feature snapshots**

```powershell
git add core/pit_feature_snapshot.py core/industry_group.py core/canslim/l_leader_laggard.py tests/test_strategy_policy.py
git commit -m "feat: expose causal ONeil leadership features"
```

### Task 4: Add Policy Interface V3

**Files:**

- Create: `core/strategy_policy/contracts_v3.py`
- Create: `core/strategy_policy/adapter_v3.py`
- Modify: `core/strategy_policy/runtime.py`
- Modify: `core/strategy_policy/worker.py`
- Modify: `core/strategy_policy/__init__.py`
- Modify: `tests/test_strategy_policy.py`

**Interfaces:**

- Consumes: V2 snapshots, `EntryFeaturesV3`, `HoldingFeaturesV3`, and portfolio state.
- Produces: `EntrySnapshotV3`, `AllocationSnapshotV3`, `EvictionSnapshotV3`, `AddOnSnapshotV3`, `ExitSnapshotV3`, and their decisions.

- [ ] **Step 1: Add V3 round-trip checks to the existing strategy-policy tests**

Require canonical JSON round trips and reject unknown fields, NaN/infinity, non-canonical
affiliations, future timestamps, and invalid add-on quantities.

- [ ] **Step 2: Define the V3 contracts**

Use composition instead of copying every V2 field:

```python
@dataclass(frozen=True, slots=True)
class EntrySnapshotV3:
    base: EntrySnapshot
    features: EntryFeaturesV3

@dataclass(frozen=True, slots=True)
class PortfolioFeaturesV3:
    gross_exposure_fraction: float
    drawdown_fraction: float
    open_risk_fraction: float
    pending_entry_count: int
    sector_exposures: tuple[tuple[str, float], ...]
    industry_exposures: tuple[tuple[str, float], ...]

@dataclass(frozen=True, slots=True)
class CapacitySnapshotV3:
    base: CapacitySnapshot
    portfolio: PortfolioFeaturesV3

@dataclass(frozen=True, slots=True)
class AllocationSnapshotV3:
    base: AllocationSnapshot
    candidate: EntryFeaturesV3
    portfolio: PortfolioFeaturesV3

@dataclass(frozen=True, slots=True)
class EvictionPositionV3:
    base: EvictionPosition
    features: HoldingFeaturesV3
    unrealized_return_fraction: float
    days_held: int
    notional_fraction: float

@dataclass(frozen=True, slots=True)
class EvictionSnapshotV3:
    base: EvictionSnapshot
    candidate: EntryFeaturesV3
    positions: tuple[EvictionPositionV3, ...]
    portfolio: PortfolioFeaturesV3

@dataclass(frozen=True, slots=True)
class AddOnSnapshotV3:
    market: MarketContextV1
    entry_price: float
    current_price: float
    current_quantity: float
    current_notional_fraction: float
    unrealized_return_fraction: float
    days_held: int
    add_on_count: int
    maximum_favorable_excursion_fraction: float
    maximum_adverse_excursion_fraction: float
    remaining_cash_fraction: float
    open_position_risk_fraction: float
    features: HoldingFeaturesV3

@dataclass(frozen=True, slots=True)
class AddOnDecisionV3:
    add: bool
    risk_fraction: float
    notional_fraction_cap: float | None
    reason_code: str

@dataclass(frozen=True, slots=True)
class ExitSnapshotV3:
    base: ExitSnapshot
    features: HoldingFeaturesV3
    unrealized_return_fraction: float
    maximum_favorable_excursion_fraction: float
    maximum_adverse_excursion_fraction: float
    position_notional_fraction: float
```

Reuse the existing entry, capacity, allocation, eviction, and exit decision contracts; V3 enriches
their inputs and introduces only the add-on decision. Canonicalize exposure tuples by key and require
their totals and all risk/cash/notional fractions to reconcile with trusted engine state. This makes
candidate ATR/liquidity, portfolio drawdown/open risk/industry exposure, current RS, return, holding
age, group rank, concentration, and excursion values concrete rather than prose-only inputs.

- [ ] **Step 3: Add runtime and worker negotiation**

Accept interface versions 2 and 3. Version 3 imports exactly the four V3 policy modules and exposes
`evaluate_entry`, `recommend_capacity`, `recommend_allocation`, `select_eviction`, `evaluate_add_on`,
and `evaluate_exit`. Keep V2 framing byte-compatible.

- [ ] **Step 4: Implement the trusted adapter**

`StrategyPolicyAdapterV3` receives trusted engine state and constructs V3 snapshots. Candidate code
never receives the bundle, pandas objects, raw transaction rows, or filesystem handles.

- [ ] **Step 5: Run focused policy checks**

```powershell
python -B -m pytest tests/test_strategy_policy.py -q
python -B -m ruff check core/strategy_policy/contracts_v3.py core/strategy_policy/adapter_v3.py core/strategy_policy/runtime.py core/strategy_policy/worker.py
```

- [ ] **Step 6: Commit policy interface V3**

```powershell
git add core/strategy_policy/contracts_v3.py core/strategy_policy/adapter_v3.py core/strategy_policy/runtime.py core/strategy_policy/worker.py core/strategy_policy/__init__.py tests/test_strategy_policy.py
git commit -m "feat: add adaptive ONeil policy interface v3"
```

### Task 5: Add O'Neil Pyramiding to the Engine

**Files:**

- Modify: `core/backtest_engine.py:2940-3475`
- Modify: `tests/test_backtest_open_causality.py`
- Modify: `tests/test_backtest_engine.py`

**Interfaces:**

- Consumes: `AddOnSnapshotV3` and `AddOnDecisionV3` for an already-held symbol.
- Produces: `PendingAddOn`, next-open add-on fills, and reconciled position/risk state.

- [ ] **Step 1: Add one focused add-on lifecycle case**

The case must prove that an end-of-session add-on decision does not fill at that close, executes at
the next eligible open, increases weighted-average entry price and quantity, and cannot spend more
cash or risk than the engine permits.

- [ ] **Step 2: Add the pending record**

```python
@dataclass(frozen=True, slots=True)
class PendingAddOn:
    symbol: str
    signal_date: str
    target_entry_date: str
    risk_fraction: float
    notional_fraction_cap: float | None
    reason_code: str
```

Persist pending add-ons in checkpoint schema V5 only. Legacy checkpoint schemas remain unchanged.

- [ ] **Step 3: Evaluate existing holdings instead of skipping them under interface V3**

Keep the current V2 `ticker in self._open_positions: continue` behavior. Under V3, build an add-on
snapshot after the completed session and queue at most one pending add-on per symbol.

- [ ] **Step 4: Execute at the next eligible open**

Reuse trusted cash, no-leverage, position-risk, and notional-cap calculations. Record distinct
`ADD` transactions and terminal outcomes including already-pending, invalid price, risk, and cash.
Follow the evaluator-truth V5 open lifecycle: an opening-gap stop or pending full exit cancels an
add-on before any buy fill; only a still-open surviving position may receive the add-on.

- [ ] **Step 5: Run the focused engine checks**

```powershell
python -B -m pytest tests/test_backtest_open_causality.py tests/test_backtest_engine.py -q
python -B -m ruff check core/backtest_engine.py
```

- [ ] **Step 6: Commit pyramiding support**

```powershell
git add core/backtest_engine.py tests/test_backtest_open_causality.py tests/test_backtest_engine.py
git commit -m "feat: add causal ONeil position pyramiding"
```

### Task 6: Create the Baseline V3 Policy Bundle

**Files:**

- Create: `core/strategy_policy/v3/__init__.py`
- Create: `core/strategy_policy/v3/entry.py`
- Create: `core/strategy_policy/v3/risk.py`
- Create: `core/strategy_policy/v3/position.py`
- Create: `core/strategy_policy/v3/exit.py`
- Create: `core/pit_optimizer_v5/policy_scope.py`
- Modify: `tests/test_strategy_policy.py`

**Interfaces:**

- Consumes: policy interface V3 snapshots.
- Produces: a deterministic baseline and exact V5 authoring scope.

- [ ] **Step 1: Implement parity-first V3 entry, risk, and exit policies**

Delegate to the existing V2 baseline logic after extracting `snapshot.base`. This provides a known
starting point while exposing new fields to later candidates.

- [ ] **Step 2: Implement an inert baseline add-on policy**

```python
def evaluate_add_on(snapshot: AddOnSnapshotV3) -> AddOnDecisionV3:
    return AddOnDecisionV3(
        add=False,
        risk_fraction=0.0,
        notional_fraction_cap=None,
        reason_code="baseline_no_add_on",
    )
```

- [ ] **Step 3: Define the exact authoring scope**

`EDITABLE_POLICY_PATHS_V5` contains the four files `entry.py`, `risk.py`, `position.py`, and
`exit.py`. Bind each path to its required exported functions and interface version 3. Full-source
escape mode may replace all four; normal operation changes only named functions/constants.

- [ ] **Step 4: Verify deterministic parity and new scope**

```powershell
python -B -m pytest tests/test_strategy_policy.py -q
python -B -m compileall -q core/strategy_policy/v3 core/pit_optimizer_v5/policy_scope.py
python -B -m ruff check core/strategy_policy/v3 core/pit_optimizer_v5/policy_scope.py
```

- [ ] **Step 5: Commit the V3 baseline policy**

```powershell
git add core/strategy_policy/v3 core/pit_optimizer_v5/policy_scope.py tests/test_strategy_policy.py
git commit -m "feat: seed optimizer v5 policy bundle"
```

### Task 7: Build and Verify the Local Three-Universe Bundle

**Files:**

- Create locally only: `.artifacts/pit-optimizer-v5/data/source/*.csv`
- Create locally only: `.artifacts/pit-optimizer-v5/data/source/*.json`
- Create locally only: `.artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3`
- Create locally only: `.artifacts/pit-optimizer-v5/data/verification_v3.json`

**Interfaces:**

- Consumes: authenticated local S&P 500, Nasdaq-100, and Russell 2000 historical membership inputs plus existing admitted PIT prices/fundamentals and group data.
- Produces: one immutable schema-V3 bundle and verification report.

- [ ] **Step 1: Preflight every local-only input**

```powershell
$pitV5Inputs = @(
  '.artifacts/pit-optimizer-v5/data/source/sp500_membership.csv',
  '.artifacts/pit-optimizer-v5/data/source/sp500_provenance.json',
  '.artifacts/pit-optimizer-v5/data/source/nasdaq100_membership.csv',
  '.artifacts/pit-optimizer-v5/data/source/nasdaq100_provenance.json',
  '.artifacts/pit-optimizer-v5/data/source/russell2000_membership.csv',
  '.artifacts/pit-optimizer-v5/data/source/russell2000_provenance.json',
  '.artifacts/pit-optimizer-v5/data/prices.csv',
  '.artifacts/pit-optimizer-v5/data/prices_provenance.json',
  '.artifacts/pit-optimizer-v5/data/fundamentals.csv',
  '.artifacts/pit-optimizer-v5/data/fundamentals_provenance.json',
  '.artifacts/pit-optimizer-v5/data/industry.csv',
  '.artifacts/pit-optimizer-v5/data/industry_provenance.json'
)
$pitV5Missing = @($pitV5Inputs | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($pitV5Missing.Count -ne 0) { throw ('Missing local PIT V5 inputs: ' + ($pitV5Missing -join ', ')) }
```

Expected: no output. If an input is absent, stop only this local artifact-build task and report the
missing paths; do not fetch, upload, or synthesize market data. Source-code implementation can
continue independently.

- [ ] **Step 2: Verify and normalize the local inputs before merging them**

```powershell
python -B normalize_pit_universe_membership.py --sp500-membership .artifacts/pit-optimizer-v5/data/source/sp500_membership.csv --sp500-provenance .artifacts/pit-optimizer-v5/data/source/sp500_provenance.json --nasdaq100-membership .artifacts/pit-optimizer-v5/data/source/nasdaq100_membership.csv --nasdaq100-provenance .artifacts/pit-optimizer-v5/data/source/nasdaq100_provenance.json --russell2000-membership .artifacts/pit-optimizer-v5/data/source/russell2000_membership.csv --russell2000-provenance .artifacts/pit-optimizer-v5/data/source/russell2000_provenance.json --prices-provenance .artifacts/pit-optimizer-v5/data/prices_provenance.json --output-csv .artifacts/pit-optimizer-v5/data/membership_v3.csv --output-provenance .artifacts/pit-optimizer-v5/data/membership_v3_provenance.json
```

Expected: all inputs authenticate, all three universes seed the evaluation start, and output is
create-only.

- [ ] **Step 3: Build the bundle using the exact authenticated exports**

```powershell
python -B build_pit_bundle.py --schema-version 3 --membership-csv .artifacts/pit-optimizer-v5/data/membership_v3.csv --prices-csv .artifacts/pit-optimizer-v5/data/prices.csv --fundamentals-csv .artifacts/pit-optimizer-v5/data/fundamentals.csv --industry-csv .artifacts/pit-optimizer-v5/data/industry.csv --data-cutoff 2025-12-31 --evaluation-start 2021-01-04 --warmup-start 2020-01-02 --membership-provenance .artifacts/pit-optimizer-v5/data/membership_v3_provenance.json --prices-provenance .artifacts/pit-optimizer-v5/data/prices_provenance.json --fundamentals-provenance .artifacts/pit-optimizer-v5/data/fundamentals_provenance.json --industry-provenance .artifacts/pit-optimizer-v5/data/industry_provenance.json --output .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3 --manifest-output .artifacts/pit-optimizer-v5/data/pit_three_universe_manifest.json
```

Task 2 adds `--schema-version`, `--industry-csv`, and `--industry-provenance` to the builder and the
corresponding industry/report arguments to the verifier. Expected: a create-only schema-V3 bundle
and manifest bound to every listed input.

- [ ] **Step 4: Verify coverage and affiliations**

```powershell
$pitV5BundleSha = (Get-FileHash -Algorithm SHA256 .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3).Hash.ToLowerInvariant()
python -B verify_pit_bundle.py --bundle .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3 --sha256 $pitV5BundleSha --manifest .artifacts/pit-optimizer-v5/data/pit_three_universe_manifest.json --membership-csv .artifacts/pit-optimizer-v5/data/membership_v3.csv --prices-csv .artifacts/pit-optimizer-v5/data/prices.csv --fundamentals-csv .artifacts/pit-optimizer-v5/data/fundamentals.csv --industry-csv .artifacts/pit-optimizer-v5/data/industry.csv --membership-provenance .artifacts/pit-optimizer-v5/data/membership_v3_provenance.json --prices-provenance .artifacts/pit-optimizer-v5/data/prices_provenance.json --fundamentals-provenance .artifacts/pit-optimizer-v5/data/fundamentals_provenance.json --industry-provenance .artifacts/pit-optimizer-v5/data/industry_provenance.json --report-output .artifacts/pit-optimizer-v5/data/verification_v3.json
```

Expected: schema 3, all three source universes, explicit overlaps, SPY/QQQ/IWM reference-only,
complete identity transitions, and no future-dated membership or fundamentals.

- [ ] **Step 5: Confirm that no data entered Git**

```powershell
git status --short
git check-ignore .artifacts/pit-optimizer-v5/data/pit_three_universe.sqlite3
```

Expected: no market-data paths in `git status`; the bundle is ignored.

## Completion Criteria

- Legacy bundle and policy readers still load their historical artifacts.
- Bundle schema V3 represents all three universes and overlaps without duplicate tradable symbols.
- Industry/group leadership and enhanced features use only completed-session PIT facts.
- Policy interface V3 round-trips through the existing worker protocol.
- The engine supports next-open add-ons without leverage or accounting drift.
- The V3 baseline policy is deterministic and the exact four-file V5 scope is sealed.
- The local three-universe bundle verifies and remains outside Git.
