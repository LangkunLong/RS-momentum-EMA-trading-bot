# PIT CANSLIM Strategy Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic five-year PIT CANSLIM diagnosis harness, publish the first fidelity-constrained signal/exit attribution report, and then connect its sealed evidence to the existing quarantined multi-agent controller.

**Architecture:** A focused `core/pit_diagnosis` package owns the versioned rulebook, incremental fact cache, proper-base evidence, experiment catalog, deterministic metrics, and immutable publication. `pit_diagnosis.py` produces the useful baseline diagnosis without any provider call; only after that works does `agent_loop.py --gate pit_diagnosis` reuse the existing source preflight, Docker confinement, budgets, audit chain, and inert proposal machinery with a closed PIT-specific protocol.

**Tech Stack:** Python 3.11+, pandas, SQLite, dataclasses/enums, JSON/CSV/hashlib, existing `PITDataBundle` and `PortfolioSimulator`, pytest, Ruff, existing OpenRouter/OpenAI gateway

**Spec:** `docs/superpowers/specs/2026-08-24-pit-canslim-strategy-diagnosis-design.md`

## Global Constraints

- CANSLIM fidelity has precedence over performance; a required rule cannot be weakened by a promotable candidate.
- The corrected bundle SHA-256 is exactly `1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb`.
- The corrected replay authority is `.artifacts/task-6-corrected-replay-20260824T100045Z/run-20260824T100213Z-1af306ef1e46` at source commit `eb93437`.
- Discovery is 2021-01-01 through 2023-12-31; validation is 2024-01-01 through 2024-12-31; 2025-01-01 through 2025-12-31 is locked evaluation and is not an unseen holdout.
- Do not expose raw SEC facts, raw price histories, transaction rows, secrets, source payloads, or ex-post leader labels to an agent.
- Five-year leader labels are diagnostic outputs only and never enter a feature, rule decision, experiment selection, or prompt.
- The deterministic harness is the only metrics authority and must not import OpenRouter, broker, scheduler, paper-trading, or live-provider code.
- Core fidelity definitions are human-reviewed implementation work; loop agents cannot author or alter the rulebook, fact cache, metrics, PIT loaders, partitions, accounting, or controller.
- The orchestrator routes only; the reasoner selects one controller-enumerated experiment from closed evidence; the coder is skipped unless that exact experiment has a controller-owned source replacement.
- No task calls OpenRouter. A paid canary remains a separately authorized operational step after all offline work and review.
- No task applies a candidate to the source checkout, promotes it, schedules it, paper-trades it, or live-trades it.
- Preserve existing `test` and technical-only `backtest` gate behavior byte-for-byte except for additive dispatch needed to recognize `pit_diagnosis`.
- Build artifacts remain under `.artifacts/` and are not committed; source, tests, rulebook, catalog, docs, and reports containing only bounded summaries are committed.

---

### Task 1: Version the CANSLIM Rulebook and Fidelity Types

**Files:**

- Create: `core/pit_diagnosis/__init__.py`
- Create: `core/pit_diagnosis/models.py`
- Create: `core/pit_diagnosis/rulebook.py`
- Create: `config/pit_canslim_rulebook_v1.json`
- Create: `tests/test_pit_diagnosis_rulebook.py`

**Interfaces:**

- Produces: `RuleClassification`, `Observability`, `FidelityLabel`, `ImplementationStatus`, `RuleSource`, `RuleRecord`, `RuleOutcome`, `Rulebook`, and `FidelityAssessment` frozen types.
- Produces: `load_canonical_json(path: Path) -> Mapping[str, object]`, `canonical_sha256(value: object) -> str`, `load_rulebook(path: Path) -> Rulebook`, and `evaluate_fidelity(rulebook: Rulebook, outcomes: Mapping[str, RuleOutcome], *, approved_proxy_rule_ids: frozenset[str] = frozenset()) -> FidelityAssessment`.
- Guarantees: canonical JSON hashing, duplicate-key rejection, closed enum values, exact source/rule references, one-of `N.NEWNESS` logic, and no silent pass for missing evidence.

- [ ] **Step 1: Write the failing schema and N one-of tests**

```python
from pathlib import Path

from core.pit_diagnosis.models import FidelityLabel, RuleOutcome
from core.pit_diagnosis.rulebook import evaluate_fidelity, load_rulebook


RULEBOOK = Path("config/pit_canslim_rulebook_v1.json")


def test_rulebook_v1_has_exact_required_domains_and_citations() -> None:
    book = load_rulebook(RULEBOOK)
    assert book.version == "pit-canslim-v1"
    assert set(book.rules) == {
        "C.EPS_YOY", "C.SALES_YOY", "C.ACCELERATION",
        "A.EPS_MULTIYEAR", "A.ROE",
        "N.NEWNESS", "N.CATALYST", "N.NEW_HIGH",
        "S.VOLUME_CONFIRMATION", "S.SUPPLY",
        "L.RS", "L.INDUSTRY_GROUP", "I.SPONSORSHIP",
        "M.CONFIRMED_UPTREND", "M.DISTRIBUTION_EXPOSURE",
        "E.PROPER_BASE", "E.PIVOT", "E.BUY_ZONE", "E.NEXT_OPEN",
        "X.LOSS_LIMIT", "X.PROFIT_ZONE", "X.EIGHT_WEEK_HOLD", "X.STRUCTURAL_SELL",
    }
    assert all(rule.source_id in book.sources for rule in book.rules.values())
    assert book.rules["N.NEWNESS"].satisfaction_logic == "one_of:N.CATALYST,N.NEW_HIGH"


def test_new_high_from_proper_base_can_satisfy_n_without_qualitative_catalyst() -> None:
    book = load_rulebook(RULEBOOK)
    outcomes = {
        rule_id: RuleOutcome.passed(rule_id)
        for rule_id in book.rules
    }
    outcomes["N.CATALYST"] = RuleOutcome.unavailable("N.CATALYST")
    outcomes["N.NEW_HIGH"] = RuleOutcome.passed("N.NEW_HIGH")
    assessment = evaluate_fidelity(book, outcomes)
    assert "N.NEWNESS" not in assessment.failed_required_rule_ids
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_rulebook.py`

Expected: FAIL because `core.pit_diagnosis` and the rulebook do not exist.

- [ ] **Step 3: Implement the closed immutable types**

Add these public shapes to `models.py`; validate every field in `__post_init__` and expose no mutable dictionaries:

```python
class RuleClassification(str, Enum):
    REQUIRED = "required"
    ALLOWED_VARIANT = "allowed_variant"
    DIAGNOSTIC_ONLY = "diagnostic_only"


class Observability(str, Enum):
    PIT_OBSERVED = "pit_observed"
    PIT_PROXY = "pit_proxy"
    PIT_UNAVAILABLE = "pit_unavailable"


class FidelityLabel(str, Enum):
    STRICT_CANSLIM = "strict_canslim"
    QUANTITATIVE_CANSLIM_PROXY = "quantitative_canslim_proxy"
    FIDELITY_INCOMPLETE = "fidelity_incomplete"


@dataclass(frozen=True)
class RuleOutcome:
    rule_id: str
    status: str  # exactly passed, failed, unavailable, or unimplemented
    evidence_ids: tuple[str, ...] = ()

    @classmethod
    def passed(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id=rule_id, status="passed", evidence_ids=tuple(evidence_ids))

    @classmethod
    def unavailable(cls, rule_id: str, *evidence_ids: str) -> "RuleOutcome":
        return cls(rule_id=rule_id, status="unavailable", evidence_ids=tuple(evidence_ids))


@dataclass(frozen=True)
class FidelityAssessment:
    label: FidelityLabel
    passed_required_rule_ids: tuple[str, ...]
    failed_required_rule_ids: tuple[str, ...]
    unavailable_required_rule_ids: tuple[str, ...]
    proxy_rule_ids: tuple[str, ...]
    promotion_eligible: bool
```

Use `MappingProxyType` for `Rulebook.sources`, `Rulebook.rules`, and each parameter policy. `load_canonical_json` parses with an `object_pairs_hook` that raises on duplicate keys; `canonical_sha256` serializes with `sort_keys=True`, `separators=(",", ":")`, `allow_nan=False`, and UTF-8 before SHA-256.

- [ ] **Step 4: Add the exact v1 source and rule records**

Populate `config/pit_canslim_rulebook_v1.json` with the five source URLs and every rule in the approved design. Use the fixed policies already present in this repository where the source permits a numeric policy: C/A growth `0.25`, RS floor `80`, breakout-volume ratio `1.30`, buy-zone maximum `0.05`, defensive loss maximum `0.08`, normal profit zone `[0.20, 0.25]`, and fast-gain/eight-week behavior `[0.20, 15, 40]`. Encode `N.NEWNESS` as `one_of:N.CATALYST,N.NEW_HIGH`; proximity to a high is not a passing N rule.

- [ ] **Step 5: Implement fail-closed fidelity evaluation**

`evaluate_fidelity` must resolve one-of rules before labeling the run, reject unknown/missing outcome IDs, require human-approved proxy IDs to be an exact subset of declared `pit_proxy` rules, and return `promotion_eligible=False` whenever a required rule is failed, unavailable, or unimplemented.

- [ ] **Step 6: Run focused verification and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_rulebook.py
python -m ruff check core/pit_diagnosis/models.py core/pit_diagnosis/rulebook.py tests/test_pit_diagnosis_rulebook.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/__init__.py core/pit_diagnosis/models.py core/pit_diagnosis/rulebook.py config/pit_canslim_rulebook_v1.json tests/test_pit_diagnosis_rulebook.py
git commit -m "feat: add PIT CANSLIM fidelity rulebook"
```

### Task 2: Freeze Partitions, Experiment Catalog, and Identities

**Files:**

- Modify: `core/pit_diagnosis/models.py`
- Create: `core/pit_diagnosis/catalog.py`
- Create: `config/pit_diagnosis_experiments_v1.json`
- Create: `tests/test_pit_diagnosis_catalog.py`

**Interfaces:**

- Consumes: `Rulebook` and its canonical SHA-256 from Task 1.
- Produces: `PartitionName`, `DatePartition`, `DatePartitions`, `ExperimentKind`, `ExperimentDefinition`, `ExperimentCatalog`, and `ExperimentIdentity`.
- Produces: `fixed_partitions() -> DatePartitions`, `load_experiment_catalog(path: Path, rulebook: Rulebook) -> ExperimentCatalog`, and the fully specified `build_experiment_identity` signature in Step 3.
- Guarantees: exactly one changed causal dimension for D0-D4, controller-only D5 composition, immutable dates, and no locked-evaluation feedback in discovery/validation evidence.

- [ ] **Step 1: Write failing date, catalog, and identity tests**

```python
def test_partitions_are_exact_and_2025_is_not_named_holdout() -> None:
    partitions = fixed_partitions()
    assert partitions.discovery.as_tuple() == ("2021-01-01", "2023-12-31")
    assert partitions.validation.as_tuple() == ("2024-01-01", "2024-12-31")
    assert partitions.locked_evaluation.as_tuple() == ("2025-01-01", "2025-12-31")
    assert "holdout" not in repr(partitions).lower()


def test_catalog_contains_only_the_approved_experiments() -> None:
    book = load_rulebook(Path("config/pit_canslim_rulebook_v1.json"))
    catalog = load_experiment_catalog(Path("config/pit_diagnosis_experiments_v1.json"), book)
    assert set(catalog.experiments) == {
        "D0.BASELINE_REPRODUCTION",
        "D1.FULL_FUNDAMENTAL_COHORT", "D1.N_CATALYST_GAP",
        "D1.I_SPONSORSHIP_GAP", "D1.INDUSTRY_GROUP_GAP",
        "D2.RULE_STAGE_FUNNEL", "D2.PROPER_BASE_COUNTERFACTUAL",
        "D2.RS_85_CONFORMANCE", "D2.LEADING_GROUP_CONFORMANCE",
        "D2.BUY_ZONE_ATTRIBUTION", "D2.LEADER_RANK_BENCHMARK",
        "D3.M_CONFIRMED_UPTREND", "D3.M_DISTRIBUTION_EXPOSURE", "D3.M_BASELINE_OFF",
        "D4.CURRENT_EXIT_PACKAGE", "D4.LOSS_LIMIT_ONLY", "D4.PROFIT_ZONE",
        "D4.EIGHT_WEEK_HOLD", "D4.STRUCTURAL_SELL", "D4.REMOVE_UNVERIFIED_EXITS",
        "D5.BOUNDED_PAIR",
    }
    assert catalog["D3.M_BASELINE_OFF"].promotion_eligible is False
    assert catalog["D5.BOUNDED_PAIR"].controller_composed is True
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_catalog.py`

Expected: FAIL because the catalog interfaces do not exist.

- [ ] **Step 3: Implement exact partition and catalog validation**

Use these signatures:

```python
def fixed_partitions() -> DatePartitions:
    return DatePartitions(
        discovery=DatePartition("discovery", "2021-01-01", "2023-12-31"),
        validation=DatePartition("validation", "2024-01-01", "2024-12-31"),
        locked_evaluation=DatePartition("locked_evaluation", "2025-01-01", "2025-12-31"),
    )


def load_experiment_catalog(path: Path, rulebook: Rulebook) -> ExperimentCatalog:
    payload = load_canonical_json(path)
    records = tuple(
        ExperimentDefinition.from_mapping(item, rulebook)
        for item in payload["experiments"]
    )
    return ExperimentCatalog.from_records(payload["version"], records, canonical_sha256(payload))


def build_experiment_identity(
    *,
    source_commit: str,
    source_fingerprint_sha256: str,
    bundle_sha256: str,
    baseline_manifest_sha256: str,
    rulebook_sha256: str,
    fact_cache_schema_sha256: str,
    fact_cache_content_sha256: str,
    catalog_sha256: str,
    experiment: ExperimentDefinition,
    partition: DatePartition,
    strategy_identity: str,
    benchmark_identity: str,
    universe_identity: str,
) -> ExperimentIdentity:
    return ExperimentIdentity.from_fields(locals())
```

Reject an experiment when `changed_dimensions` is not exactly one for D0-D4, a cited rule ID is absent, a `diagnostic_only` rule is marked promotable, an agent-selectable experiment has arbitrary parameters or commands, or D5 is pre-composed in JSON.

- [ ] **Step 4: Populate the catalog from the approved D0-D5 list**

Each JSON record has exactly: `experiment_id`, `phase`, `domain`, `kind`, `changed_dimensions`, `rule_ids`, `promotion_eligible`, `controller_composed`, `requires_code`, and `allowed_variant_ids`. Configuration/data experiments set `requires_code=false`; initial code replacements are empty, so the controller cannot call the coder merely to exercise it.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_catalog.py
python -m ruff check core/pit_diagnosis/models.py core/pit_diagnosis/catalog.py tests/test_pit_diagnosis_catalog.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/models.py core/pit_diagnosis/catalog.py config/pit_diagnosis_experiments_v1.json tests/test_pit_diagnosis_catalog.py
git commit -m "feat: define bounded PIT diagnosis experiments"
```

### Task 3: Produce Causal RS Ratings and Structured Proper-Base Evidence

**Files:**

- Modify: `core/momentum_analysis.py:42-230`
- Modify: `core/backtest_engine.py:121-164`
- Create: `core/pit_diagnosis/patterns.py`
- Create: `tests/test_pit_diagnosis_patterns.py`
- Modify: `tests/test_backtest_open_causality.py`

**Interfaces:**

- Produces: `calculate_rs_snapshot(all_closes: pd.DataFrame, eval_date: pd.Timestamp, eligible_tickers: Iterable[str] | None = None) -> dict[str, float]` in `core.momentum_analysis`.
- Preserves: `core.backtest_engine._calculate_rs_snapshot` as a compatibility delegate with identical results.
- Produces: `BasePolicy`, `BaseKind`, `BasePattern`, `detect_proper_base(history_before_event: pd.DataFrame, *, event_session: str, policy: BasePolicy) -> BasePattern | None`, and `evaluate_new_high_entry(pattern: BasePattern | None, event_close: float, event_volume_ratio: float, *, max_extension_pct: float = 0.05, minimum_volume_ratio: float = 1.30) -> RuleOutcome`.
- Guarantees: all pattern inputs end before the event session; no base means a blocking/unimplemented entry fact, never pass-through; “near high” without a proper base cannot pass N or E.

- [ ] **Step 1: Write RED causality and structure tests**

```python
def test_proper_base_excludes_event_bar_and_returns_auditable_shape() -> None:
    index = pd.bdate_range("2024-01-02", periods=29)
    closes = pd.Series([100.0] * 5 + [98.0, 97.0, 99.0, 100.0] * 6, index=index)
    before = pd.DataFrame({"High": closes + 0.25, "Low": closes - 0.25, "Close": closes})
    event_session = (index[-1] + pd.offsets.BDay()).date().isoformat()
    pattern = detect_proper_base(
        before,
        event_session=event_session,
        policy=BasePolicy.canonical_v1(),
    )
    assert pattern is not None
    assert pattern.end_session == before.index[-1].date().isoformat()
    base = before.loc[pattern.start_session:pattern.end_session]
    assert pattern.pivot == pytest.approx(base["High"].max())
    assert pattern.depth_pct <= 0.15


def test_near_high_without_proper_base_fails_newness() -> None:
    outcome = evaluate_new_high_entry(None, 100.0, 2.0)
    assert outcome.status == "unimplemented"
    assert outcome.rule_id == "N.NEW_HIGH"


def test_public_rs_snapshot_matches_backtest_compatibility_delegate() -> None:
    closes = causal_rs_fixture()
    day = closes.index[-1]
    assert calculate_rs_snapshot(closes, day) == _calculate_rs_snapshot(closes, day)
```

- [ ] **Step 2: Run and confirm RED**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_patterns.py tests/test_backtest_open_causality.py -k "proper_base or near_high or public_rs"
```

Expected: FAIL because the public RS function and structured base detector are absent.

- [ ] **Step 3: Extract the existing RS implementation without changing behavior**

Move the body of `core.backtest_engine._calculate_rs_snapshot` into the public `calculate_rs_snapshot` function in `core.momentum_analysis`. Keep the private engine function as this exact delegate so checkpoint and caller names remain compatible:

```python
def _calculate_rs_snapshot(all_closes, eval_date, eligible_tickers=None):
    return calculate_rs_snapshot(all_closes, eval_date, eligible_tickers)
```

- [ ] **Step 4: Implement structured flat-base and cup-with-handle detection**

`BasePolicy.canonical_v1()` freezes the repository’s reviewed boundaries: flat base 25-65 sessions and at most 15% deep; cup 35-130 sessions and 15-33% deep; handle at most 12% deep; right lip within 5% of the left lip. The detector searches only OHLC history ending before `event_session`, returns the most recent valid base, uses the appropriate pre-event High as the pivot, and records kind, start/end sessions, pivot, low, depth, duration, handle bounds, and the input SHA-256. Reject missing OHLC columns, non-finite values, duplicate/unsorted indexes, fewer-than-25-session history, and any input row on or after `event_session`.

- [ ] **Step 5: Implement the proper-base/new-high contract**

`evaluate_new_high_entry` passes only when a pattern exists, event close is at or above pivot and no more than 5% above it, and event volume ratio is at least 1.30. Return explicit evidence IDs for `E.PROPER_BASE`, `E.PIVOT`, `E.BUY_ZONE`, `S.VOLUME_CONFIRMATION`, and `N.NEW_HIGH`; return failed/unimplemented outcomes instead of booleans for every rejected path.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_patterns.py tests/test_backtest_open_causality.py
python -m ruff check core/momentum_analysis.py core/backtest_engine.py core/pit_diagnosis/patterns.py tests/test_pit_diagnosis_patterns.py
git diff --check
```

Expected: PASS with unchanged legacy RS results.

Commit:

```powershell
git add core/momentum_analysis.py core/backtest_engine.py core/pit_diagnosis/patterns.py tests/test_pit_diagnosis_patterns.py tests/test_backtest_open_causality.py
git commit -m "feat: add causal proper-base evidence"
```

### Task 4: Build the Incremental Immutable Diagnosis Fact Cache

**Files:**

- Create: `core/pit_diagnosis/fact_cache.py`
- Create: `core/pit_diagnosis/supplemental.py`
- Create: `tests/test_pit_diagnosis_fact_cache.py`

**Interfaces:**

- Consumes: `PITDataBundle`, `Rulebook`, `DatePartitions`, public `calculate_rs_snapshot`, and proper-base interfaces.
- Produces: `InstitutionalSnapshot`, `IndustryGroupSnapshot`, `SupplementalPITProvider`, and `UnavailableSupplementalPITProvider` in `supplemental.py`.
- Produces: `FactCacheIdentity`, `FactCacheBuildResult`, `SessionFact`, `FactCacheBuilder`, the exact `build_fact_cache` signature below, `open_fact_cache(path: Path, expected_content_sha256: str) -> FactCache`, and `FactCache.session_fact(symbol: str, session: str) -> SessionFact`.
- Exact builder signature:

```python
def build_fact_cache(
    *,
    bundle: PITDataBundle,
    rulebook: Rulebook,
    partitions: DatePartitions,
    output_path: Path,
    checkpoint_path: Path,
    progress_path: Path,
    resume: bool,
    supplemental_provider: SupplementalPITProvider | None = None,
    checkpoint_every_sessions: int = 5,
) -> FactCacheBuildResult:
    builder = FactCacheBuilder(
        bundle=bundle,
        rulebook=rulebook,
        partitions=partitions,
        output_path=output_path,
        checkpoint_path=checkpoint_path,
        progress_path=progress_path,
        supplemental_provider=supplemental_provider or UnavailableSupplementalPITProvider(),
        checkpoint_every_sessions=checkpoint_every_sessions,
    )
    return builder.build(resume=resume)
```

- Guarantees: one active-member row per symbol/session, immutable finalized SQLite, append-only progress, atomic checkpoints, no future fact, and no ex-post label or agent field.

- [ ] **Step 1: Write failing schema, causality, and resume tests**

```python
def test_fact_cache_contains_only_pit_session_inputs(mini_pit_bundle, tmp_path: Path) -> None:
    result = build_fact_cache(
        bundle=mini_pit_bundle,
        rulebook=rulebook_v1(),
        partitions=mini_partitions(),
        output_path=tmp_path / "facts.sqlite3",
        checkpoint_path=tmp_path / "facts.checkpoint.json",
        progress_path=tmp_path / "facts.progress.jsonl",
        resume=False,
        checkpoint_every_sessions=1,
    )
    with open_fact_cache(result.path, result.content_sha256) as cache:
        row = cache.session_fact("AAA", "2024-01-05")
        assert row.session == "2024-01-05"
        assert row.latest_fundamental_public_date <= row.session
        assert "leader" not in set(cache.column_names)
        assert "agent" not in set(cache.column_names)


def test_resume_skips_completed_sessions_and_rejects_identity_change(
    mini_pit_bundle, tmp_path: Path, interrupt_after_first_session
) -> None:
    paths = cache_paths(tmp_path)
    with pytest.raises(InterruptedError):
        interrupt_after_first_session(lambda: build_cache(mini_pit_bundle, paths, resume=False))
    resumed = build_cache(mini_pit_bundle, paths, resume=True)
    assert resumed.resumed is True
    assert resumed.reprocessed_sessions == 0
    with pytest.raises(ValueError, match="identity"):
        build_cache(mutated_bundle(mini_pit_bundle), paths, resume=True)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_fact_cache.py`

Expected: FAIL because the builder does not exist.

- [ ] **Step 3: Create the exact normalized SQLite schema**

Create `metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)` and `session_facts` with composite primary key `(bundle_sha256, rulebook_schema_version, symbol, session)`. Store member flag; OHLCV; prior close; prior-50 average volume and event ratio; current/prior-year EPS and sales; four annual EPS values; latest net income/equity; computed C/A/sales/ROE; shares; institutional fields; RS rating; nullable industry rank; structured base fields; pivot/extension; market regime/distribution/follow-through; latest fundamental public date; availability bitset; and canonical row SHA-256. Use strict integer booleans and reject non-finite floats.

- [ ] **Step 4: Implement session-at-a-time materialization and logging**

Drive the calendar from exact SPY sessions. For each session, call `bundle.members_at(session)`, slice prices through that session, use `iter_fundamental_state_boundaries` rather than reparsing every filing for every day, compute RS across that day’s PIT membership, and compute base inputs from bars strictly before the event. After each completed session, commit SQLite, append this closed progress record, fsync it, and atomically replace the checkpoint:

```json
{"phase":"session_complete","session":"2024-01-05","rows":501,"last_symbol":"ZTS","identity_sha256":"0000000000000000000000000000000000000000000000000000000000000000","state_sha256":"1111111111111111111111111111111111111111111111111111111111111111"}
```

The checkpoint contains exact source/bundle/rulebook/partition/schema identities plus `next_session_index`; resume truncates neither finalized output nor completed rows and rejects any identity mismatch. `resume=True` with no partial cache or checkpoint starts a new build, while `resume=False` rejects any pre-existing partial state.

- [ ] **Step 5: Keep missing supplemental evidence explicit**

`UnavailableSupplementalPITProvider` returns unavailable institutional and group snapshots with a fixed all-zero identity hash. Any real provider must expose a nonzero content identity, exact as-of dates, group memberships/ranks, institutional ownership/holder counts, and source evidence IDs; future-dated snapshots fail closed. The initial five-year run uses the unavailable provider, so I and leading-group gaps are measured and cannot silently pass.

- [ ] **Step 6: Finalize immutably**

Build at `diagnosis_facts.sqlite3.partial`, run `PRAGMA integrity_check`, verify row counts against PIT membership for every session, remove build-only state, set metadata `status=complete`, compute schema/content hashes, close the connection, and atomically rename to `diagnosis_facts.sqlite3`. `open_fact_cache` uses `mode=ro`, rehashes the file, validates `status=complete`, and rejects sidecars or writable fallback.

- [ ] **Step 7: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_fact_cache.py
python -m ruff check core/pit_diagnosis/fact_cache.py core/pit_diagnosis/supplemental.py tests/test_pit_diagnosis_fact_cache.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/fact_cache.py core/pit_diagnosis/supplemental.py tests/test_pit_diagnosis_fact_cache.py
git commit -m "feat: materialize resumable PIT diagnosis facts"
```

### Task 5: Verify and Reproduce the Corrected Baseline Authority

**Files:**

- Create: `core/pit_diagnosis/baseline.py`
- Create: `tests/test_pit_diagnosis_baseline.py`

**Interfaces:**

- Consumes: corrected replay `run_manifest.json`, `summary.json`, `canslim_signals.csv`, `entry_attempt_outcomes.csv`, `transactions.csv`, `equity_curve.csv`, and `leader_recall.csv`.
- Produces: `BaselineAuthority`, `BaselineSnapshot`, `BaselineReproduction`, `canonical_authority() -> BaselineAuthority`, `verify_baseline_run(run_dir: Path, authority: BaselineAuthority) -> BaselineSnapshot`, and `compare_reproduction(authority: BaselineSnapshot, reproduced: BaselineSnapshot) -> BaselineReproduction`.
- Guarantees: exact bundle/source/artifact identities; exact entry/outcome/transaction row hashes; numeric comparison only for CSV round-trip tolerance; no “strategy observation” when reproduction fails.

- [ ] **Step 1: Write RED authority and tamper tests**

```python
def test_corrected_baseline_authority_is_exact() -> None:
    authority = canonical_authority()
    assert authority.bundle_sha256 == "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb"
    assert authority.total_return_pct == pytest.approx(-9.994717769465932)
    assert authority.closed_trades == 225
    assert authority.qualified_entries == 286
    assert authority.executed_entries == 225
    assert authority.next_open_buy_zone_rejections == 51
    assert authority.average_cash_pct == pytest.approx(67.31359377429541)


def test_baseline_verifier_rejects_one_changed_transaction(
    mini_verified_run: tuple[Path, BaselineAuthority],
) -> None:
    run_dir, authority = mini_verified_run
    frame = pd.read_csv(run_dir / "transactions.csv")
    frame.loc[0, "Price"] += 0.01
    frame.to_csv(run_dir / "transactions.csv", index=False)
    with pytest.raises(ValueError, match="artifact hash"):
        verify_baseline_run(run_dir, authority)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_baseline.py`

Expected: FAIL because the baseline verifier does not exist.

- [ ] **Step 3: Implement content and semantic verification**

`verify_baseline_run` requires regular non-symlink files, stream-validates every manifest artifact hash before parsing, requires source commit `eb93437`, checks the fixed date contract, rejects NaN/Infinity, and reconciles `daily_entry_funnel.csv`, `entry_attempt_outcomes.csv`, and `transactions.csv`. It does not deserialize the 343 MB signal log or 710 MB checkpoint journal after their manifest hashes pass. Freeze these baseline facts: -9.994717769465932% total return, -2.0874097904821753% annualized return, -0.2082076838233648 Sharpe, -13.664400600134604% maximum drawdown, 225 trades, 39.111111111111114% win rate, 67.31359377429541% average cash, 286 qualified entries, 225 executions, 51 next-open buy-zone rejects, 10 cash rejects, and zero capacity/invalid/missing execution rejects.

- [ ] **Step 4: Add optional fresh reproduction comparison**

`compare_reproduction` compares a fresh `pit_baseline.run_baseline` result with the verified authority: identities and row/order hashes are exact; JSON numbers are exact; CSV floats use only the existing normalized round-trip rule from `tests/test_task6_corrected_replay_audit.py`. Return a frozen object with `passed`, `mismatch_codes`, and both manifest hashes; do not return raw rows.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_baseline.py tests/test_task6_corrected_replay_audit.py
python -m ruff check core/pit_diagnosis/baseline.py tests/test_pit_diagnosis_baseline.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/baseline.py tests/test_pit_diagnosis_baseline.py
git commit -m "feat: verify corrected PIT diagnosis authority"
```

### Task 6: Run Deterministic Rule, Entry, Market, and Exit Experiments

**Files:**

- Create: `core/pit_diagnosis/metrics.py`
- Create: `core/pit_diagnosis/strategy.py`
- Create: `core/pit_diagnosis/experiments.py`
- Create: `tests/test_pit_diagnosis_experiments.py`

**Interfaces:**

- Consumes: `Rulebook`, `ExperimentCatalog`, read-only `FactCache`, `BaselineSnapshot`, `PortfolioSimulator`, and leader labels only at the output-metrics boundary.
- Produces: `RuleAttribution`, `EntryFunnel`, `ExitAttribution`, `TradeStatistics`, `PerformanceEvidence`, `LeaderRecallEvidence`, `ExperimentResult`, `DiagnosisContext`, `ExperimentRunner`, and `ExperimentCheckpointStore`.
- Produces: `evaluate_session_rules(fact: SessionFact, rulebook: Rulebook, experiment: ExperimentDefinition) -> tuple[RuleOutcome, ...]`, `run_experiment(context: DiagnosisContext, experiment_id: str, partition: PartitionName) -> ExperimentResult`, and `run_catalog(context: DiagnosisContext, experiment_ids: Sequence[str], partitions: Sequence[PartitionName], checkpoint_root: Path, *, resume: bool) -> tuple[ExperimentResult, ...]`.
- Produces: `CachedDiagnosisStrategy(CanslimStrategy)` and `DiagnosisPortfolioSimulator(PortfolioSimulator)`; neither imports or calls a provider.
- Guarantees: D0 exact reproduction, one causal dimension per D1-D4 result, no performance promotion while I/group evidence is unavailable, and no locked-evaluation run through these default APIs.

- [ ] **Step 1: Write RED rule-funnel and exit-attribution tests**

```python
def test_rule_stage_funnel_is_monotone_and_missing_i_never_passes(diagnosis_context) -> None:
    result = run_experiment(
        diagnosis_context,
        "D2.RULE_STAGE_FUNNEL",
        PartitionName.DISCOVERY,
    )
    survivor_counts = [stage.survivors for stage in result.rule_attribution]
    assert survivor_counts == sorted(survivor_counts, reverse=True)
    assert result.fidelity.label is FidelityLabel.FIDELITY_INCOMPLETE
    assert "I.SPONSORSHIP" in result.fidelity.unavailable_required_rule_ids
    assert result.promotion_eligible is False


def test_current_exit_package_explains_the_known_ma_loss_cluster(diagnosis_context) -> None:
    result = run_experiment(
        diagnosis_context,
        "D4.CURRENT_EXIT_PACKAGE",
        PartitionName.DISCOVERY,
    )
    ma = result.exit_attribution.by_reason["ma_violation"]
    assert ma.closed_positions > 0
    assert ma.win_rate_pct < result.trade_statistics.win_rate_pct
    assert ma.average_completed_position_return_pct < 0.0


def test_ex_post_leader_labels_cannot_change_a_trade_path(diagnosis_context) -> None:
    first = run_experiment(diagnosis_context, "D3.M_CONFIRMED_UPTREND", PartitionName.VALIDATION)
    second = run_experiment(
        diagnosis_context.with_replaced_diagnostic_leader_labels(reversed_labels()),
        "D3.M_CONFIRMED_UPTREND",
        PartitionName.VALIDATION,
    )
    assert first.trade_path_sha256 == second.trade_path_sha256
    assert first.leader_recall != second.leader_recall
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_experiments.py`

Expected: FAIL because experiment execution and evidence types do not exist.

- [ ] **Step 3: Implement closed metric records**

All constructors reject NaN/Infinity, negative counts, percentages outside their domain, inconsistent totals, unknown exit/rejection codes, or unsorted evidence IDs. `TradeStatistics` contains completed positions, wins, losses, win rate, mean/median return, mean winner, mean loser, expectancy, mean/median calendar hold, and mean/median trading-session hold. `PerformanceEvidence` contains total/annualized return, Sharpe, drawdown, average cash, closed positions, benchmark deltas, and partition identity.

- [ ] **Step 4: Implement fact-backed entry and market evaluation**

`CachedDiagnosisStrategy.evaluate_symbol` reads only the exact `(symbol, eval_date)` row and emits the signal dictionary expected by `PortfolioSimulator`; it never recalculates against future rows. The fixed observed-rule reference requires C 25%, A 25%, proper base/pivot, N through `N.NEW_HIGH`, S volume 1.30x, RS 80, buy zone 0-5%, exact next-open execution, and confirmed-uptrend M. Missing I and leading-group facts remain explicit unavailable outcomes and make the reference `fidelity_incomplete`; diagnostic performance may still be measured but not promoted.

- [ ] **Step 5: Implement exit variants outside the production engine**

`DiagnosisPortfolioSimulator._check_exits` delegates to the production implementation for `D4.CURRENT_EXIT_PACKAGE`, and implements only catalog variants for the other D4 experiments. Every variant preserves the 8% maximum loss. `D4.PROFIT_ZONE` exits the normal position in the 20-25% zone unless the existing fast-20%-within-15-sessions rule activates the 40-session hold; `D4.REMOVE_UNVERIFIED_EXITS` disables only `time_stop` and the 21-day EMA and remains non-promotable until a cited replacement package passes. Record each decision with rule and evidence IDs.

The current-package report must also reconstruct the immutable full-period baseline’s 97 stop-loss, 92 moving-average, 30 time-stop, and 6 end-of-test final exits from `BaselineSnapshot`, including the known 15.22% MA-exit win rate and -2.86% mean completed-position return. Discovery/validation experiment deltas remain separately partitioned and never use 2025 for iteration.

- [ ] **Step 6: Implement the D0-D4 experiment dispatcher**

Use an explicit mapping, not dynamic imports or commands:

```python
_RUNNERS: Mapping[str, ExperimentRunner] = MappingProxyType({
    "D0.BASELINE_REPRODUCTION": run_baseline_reproduction,
    "D1.FULL_FUNDAMENTAL_COHORT": run_full_fundamental_cohort,
    "D1.N_CATALYST_GAP": run_n_gap,
    "D1.I_SPONSORSHIP_GAP": run_i_gap,
    "D1.INDUSTRY_GROUP_GAP": run_industry_gap,
    "D2.RULE_STAGE_FUNNEL": run_rule_stage_funnel,
    "D2.PROPER_BASE_COUNTERFACTUAL": run_proper_base_counterfactual,
    "D2.RS_85_CONFORMANCE": run_rs_85_conformance,
    "D2.LEADING_GROUP_CONFORMANCE": run_leading_group_conformance,
    "D2.BUY_ZONE_ATTRIBUTION": run_buy_zone_attribution,
    "D2.LEADER_RANK_BENCHMARK": run_leader_rank_benchmark,
    "D3.M_CONFIRMED_UPTREND": run_confirmed_uptrend,
    "D3.M_DISTRIBUTION_EXPOSURE": run_distribution_exposure,
    "D3.M_BASELINE_OFF": run_market_off_counterfactual,
    "D4.CURRENT_EXIT_PACKAGE": run_current_exit_package,
    "D4.LOSS_LIMIT_ONLY": run_loss_limit_only,
    "D4.PROFIT_ZONE": run_profit_zone,
    "D4.EIGHT_WEEK_HOLD": run_eight_week_hold,
    "D4.STRUCTURAL_SELL": run_structural_sell,
    "D4.REMOVE_UNVERIFIED_EXITS": run_remove_unverified_exits,
})
```

Reject D5 unless the controller supplies exactly two completed single-factor parent result hashes, both identify a measured interaction, and the composed definition is generated in memory and marked controller-owned.

`run_catalog` reads all scalar facts once, materializes each unique session/symbol entry mask once, reuses it across attribution experiments, and performs portfolio-only replay for variants that can change transactions. After each experiment/partition pair, `ExperimentCheckpointStore` writes identity, result hash, and artifact hash atomically; `resume=True` skips an exact completed identity and rejects stale source, cache, rulebook, partition, or strategy hashes.

- [ ] **Step 7: Apply promotion checks as evidence, not optimization**

For validation, report the predeclared 60-discovery/20-validation completed-position floors, positive expectancy, non-worse return/annualized/Sharpe/drawdown headroom, non-worse PIT-exposed recall, and strict improvement. Do not label any current result promotable while `I.SPONSORSHIP`, `L.INDUSTRY_GROUP`, or another required rule is unavailable. Average cash is reported but never independently satisfies improvement.

- [ ] **Step 8: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_experiments.py
python -m ruff check core/pit_diagnosis/metrics.py core/pit_diagnosis/strategy.py core/pit_diagnosis/experiments.py tests/test_pit_diagnosis_experiments.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/metrics.py core/pit_diagnosis/strategy.py core/pit_diagnosis/experiments.py tests/test_pit_diagnosis_experiments.py
git commit -m "feat: run deterministic CANSLIM diagnosis experiments"
```

### Task 7: Publish and Run the First Deterministic Diagnosis Deliverable

**Files:**

- Create: `core/pit_diagnosis/publication.py`
- Create: `pit_diagnosis.py`
- Create: `tests/test_pit_diagnosis_cli.py`

**Interfaces:**

- Consumes: all deterministic interfaces from Tasks 1-6 and the approved artifact paths.
- Produces: `publish_diagnosis(context: DiagnosisContext, results: Sequence[ExperimentResult], output_root: Path) -> Path`, `verify_diagnosis_run(run_dir: Path) -> Mapping[str, object]`, `build_parser() -> argparse.ArgumentParser`, and `main(argv: Sequence[str] | None = None) -> int`.
- CLI subcommands: `build-facts`, `run`, `run-experiment`, and `verify-result`; all data paths and SHA-256 values are explicit.
- Guarantees: fresh immutable run directories, atomic artifact publication, exact manifest reconciliation, resumable fact/experiment work, and no provider/broker imports.

- [ ] **Step 1: Write failing CLI and publication tests**

```python
def test_help_has_no_data_or_provider_side_effects(monkeypatch, capsys) -> None:
    monkeypatch.setattr("core.pit_data.PITDataBundle", forbidden_call)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "OpenRouter" not in capsys.readouterr().out


def test_publication_is_complete_hash_bound_and_refuses_reuse(mini_completed_context, tmp_path) -> None:
    run_dir = publish_diagnosis(
        mini_completed_context,
        mini_completed_context.results,
        tmp_path,
    )
    verified = verify_diagnosis_run(run_dir)
    assert verified["status"] == "complete"
    assert verified["fidelity_label"] == "fidelity_incomplete"
    assert set(verified["artifact_sha256"]) == {
        "rulebook.json", "diagnosis_facts.sqlite3", "baseline_reproduction.json",
        "experiment_catalog.json", "rule_attribution.csv", "entry_funnel.csv",
        "execution_outcomes.csv", "exit_attribution.csv", "trade_statistics.json",
        "leader_recall.json", "performance.json", "ablation_results.csv",
        "agent_events.jsonl", "checkpoint.json", "report.md",
    }
    with pytest.raises(FileExistsError):
        publish_diagnosis(mini_completed_context, mini_completed_context.results, run_dir)
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_cli.py`

Expected: FAIL because publication and CLI do not exist.

- [ ] **Step 3: Implement atomic canonical publication**

Write every required artifact to a private staging directory, serialize JSON canonically, write CSV with fixed column order and newline policy, create an initially empty hash-chained `agent_events.jsonl`, recheck source/bundle/rulebook/catalog/fact-cache/baseline identities, then write `manifest.json` last and atomically rename the run. `verify_diagnosis_run` rejects extra/missing files, sidecars, symlinks, hash mismatches, stale checkpoints, inconsistent counts, raw provider fields, and non-finite values.

- [ ] **Step 4: Implement the explicit offline CLI**

The parser requires absolute data/output/checkpoint paths, lowercase SHA-256 values, and the fixed partitions. `run` defaults to D0-D4 on discovery and validation only. `--partition locked_evaluation` additionally requires `--human-selection-id` and `--research-generation-id`, writes a separate locked result, and never merges its metrics back into a discovery/validation result. `run-experiment` accepts exactly one catalog experiment ID and is the future Docker worker boundary.

- [ ] **Step 5: Run the focused CLI tests and commit code before the long run**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_cli.py
python -m ruff check core/pit_diagnosis/publication.py pit_diagnosis.py tests/test_pit_diagnosis_cli.py
python -m compileall -q core/pit_diagnosis pit_diagnosis.py
git diff --check
```

Expected: PASS.

Commit:

```powershell
git add core/pit_diagnosis/publication.py pit_diagnosis.py tests/test_pit_diagnosis_cli.py
git commit -m "feat: publish offline PIT diagnosis runs"
```

- [ ] **Step 6: Build or resume the full five-year fact cache**

Run from the repository root, using the exact approved paths:

```powershell
$repoRoot = (Get-Location).Path
$pitBundle = (Resolve-Path -LiteralPath ".artifacts/task-4-regeneration-20260823T223000Z/pit-bundle/pit_baseline.sqlite3").Path
$rulebook = (Resolve-Path -LiteralPath "config/pit_canslim_rulebook_v1.json").Path
$factOutput = Join-Path $repoRoot ".artifacts/pit-diagnosis/facts/diagnosis_facts.sqlite3"
$factCheckpoint = Join-Path $repoRoot ".artifacts/pit-diagnosis/facts/facts.checkpoint.json"
$factProgress = Join-Path $repoRoot ".artifacts/pit-diagnosis/facts/facts.progress.jsonl"
python -B pit_diagnosis.py build-facts `
  --pit-bundle $pitBundle `
  --pit-bundle-sha256 "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb" `
  --rulebook $rulebook `
  --output $factOutput `
  --checkpoint $factCheckpoint `
  --progress $factProgress `
  --resume
```

Expected: incremental session progress; interrupted reruns resume without reprocessing completed sessions; final output reports cache schema/content SHA-256.

- [ ] **Step 7: Run the first discovery/validation diagnosis**

Use the fact-cache SHA printed by Step 6:

```powershell
$baselineRun = (Resolve-Path -LiteralPath ".artifacts/task-6-corrected-replay-20260824T100045Z/run-20260824T100213Z-1af306ef1e46").Path
$experimentCatalog = (Resolve-Path -LiteralPath "config/pit_diagnosis_experiments_v1.json").Path
$diagnosisOutputRoot = Join-Path $repoRoot ".artifacts/pit-diagnosis/runs"
$diagnosisCheckpointRoot = Join-Path $repoRoot ".artifacts/pit-diagnosis/checkpoints"
$factCacheSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $factOutput).Hash.ToLowerInvariant()
python -B pit_diagnosis.py run `
  --pit-bundle $pitBundle `
  --pit-bundle-sha256 "1af306ef1e46797473cd186fc48938ed6694ae25f5943c9f1905b528307cc2eb" `
  --baseline-run $baselineRun `
  --rulebook $rulebook `
  --experiment-catalog $experimentCatalog `
  --fact-cache $factOutput `
  --fact-cache-sha256 $factCacheSha `
  --output-root $diagnosisOutputRoot `
  --checkpoint-root $diagnosisCheckpointRoot
```

Expected: D0 reproduces exactly; D1-D4 publish discovery and validation evidence; fidelity remains `fidelity_incomplete` with I and any absent leading-group evidence named; no 2025 candidate run and no agent call occurs.

- [ ] **Step 8: Verify the real result before agent integration**

Run:

```powershell
$diagnosisRun = Get-ChildItem -LiteralPath $diagnosisOutputRoot -Directory |
  Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json") } |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1
python -B pit_diagnosis.py verify-result --run-dir $diagnosisRun.FullName
```

Expected: exit 0 and a summary containing baseline reproduction, rule funnel, proper-base impact, M impact, exit attribution, trade expectancy, average cash, PIT-exposed recall, missing-fidelity rules, and zero promotion-eligible candidates.

Record the exact run path, manifest SHA-256, fact-cache SHA-256, baseline result, and top causal findings in the task report; do not commit `.artifacts`.

### Task 8: Add Closed PIT-Specific Agent Protocols Without Changing Existing Roles

**Files:**

- Create: `pit_diagnosis_agent.py`
- Modify: `agent_loop.py:1801-2135`
- Create: `tests/test_pit_diagnosis_agent.py`

**Interfaces:**

- Consumes: existing `OpenRouterGateway`, budget/accounting records, sanitization, `TypedCodingProposal`, and deterministic diagnosis summaries.
- Produces: `PitDomain`, `PitRoute`, `PitReasoningPlan`, `PitAgentEvidence`, `PitAgentEvent`, and `OpenRouterGateway.request_pit_diagnosis_once` with the same arguments and budget/deadline keywords as `request_once`.
- Exact route schema example: `{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"]}`; `action=abort` requires an empty evidence list.
- Exact reasoner schema example: `{"causal_hypothesis":"The current MA exit closes weak positions before a cited structural sell rule can distinguish repair from failure.","evidence_ids":["EXIT.MA_001"],"rule_ids":["X.STRUCTURAL_SELL"],"invariant_ids":["INV.LOSS_LIMIT"],"experiment_id":"D4.STRUCTURAL_SELL","skip":false,"skip_reason":""}`.
- Guarantees: the orchestrator cannot summarize, hypothesize, choose parameters, or name files; the reasoner cannot invent facts/rules/experiments; conceptual roles/models remain orchestrator, reasoner, coder.

- [ ] **Step 1: Write RED protocol and prompt tests**

```python
def test_pit_orchestrator_can_only_route_closed_ids() -> None:
    route = PitRoute.from_json(
        '{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"]}'
    )
    assert route.domain is PitDomain.EXIT
    with pytest.raises(ProtocolValidationError):
        PitRoute.from_json(
            '{"action":"reason","domain":"exit","evidence_ids":["EXIT.MA_001"],'
            '"reasoning":"change the moving average"}'
        )


def test_reasoner_must_choose_one_supplied_experiment() -> None:
    evidence = pit_agent_evidence(experiment_ids=("D4.PROFIT_ZONE",))
    plan = PitReasoningPlan.from_json(valid_pit_plan_json("D4.PROFIT_ZONE"))
    validate_pit_reasoning_plan(plan, evidence)
    with pytest.raises(ProtocolValidationError, match="experiment"):
        validate_pit_reasoning_plan(
            PitReasoningPlan.from_json(valid_pit_plan_json("D4.INVENTED")),
            evidence,
        )


def test_pit_gateway_uses_same_models_with_distinct_closed_prompts(fake_client) -> None:
    gateway = gateway_with(fake_client)
    gateway.request_pit_diagnosis_once("orchestrator", "{}", PitRoute.from_json)
    request = fake_client.chat.completions.calls[0]
    assert request["model"] == ORCHESTRATOR_MODEL
    assert "failure_summary" not in request["messages"][0]["content"]
    assert '"domain"' in request["messages"][0]["content"]
```

- [ ] **Step 2: Run and confirm RED**

Run: `python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_agent.py`

Expected: FAIL because the PIT protocol family does not exist.

- [ ] **Step 3: Implement strict PIT protocol dataclasses and grounding**

Use duplicate-key rejection and the existing UTF-8/count bounds. `PitAgentEvidence` contains only run/rulebook/catalog/experiment hashes, closed numeric metrics, sorted evidence/rule/invariant/experiment IDs, fidelity label, and promotion status. It rejects any keys named `raw`, `rows`, `transactions`, `prices`, `fundamentals`, `payload`, `secret`, `path`, or `source_text`.

- [ ] **Step 4: Add a separate immutable gateway prompt family**

Keep `request` and `request_once` unchanged. Add `request_pit_diagnosis_once` that uses fixed `PIT_DIAGNOSIS_SYSTEM_PROMPTS`, fixed JSON schemas, the same three model constants/token caps/accounting path, and the same immutable static context. The orchestrator prompt says “route only”; the reasoner prompt permits one falsifiable hypothesis and one supplied experiment ID; the coder prompt permits exactly one controller-owned replacement. None permits retrieval, commands, arbitrary thresholds, or chain-of-thought output.

- [ ] **Step 5: Verify existing and new protocols and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_agent.py tests/test_agent_loop.py -k "prompt or protocol or gateway"
python -m ruff check pit_diagnosis_agent.py agent_loop.py tests/test_pit_diagnosis_agent.py
git diff --check
```

Expected: PASS; existing prompt bytes and role request behavior remain unchanged.

Commit:

```powershell
git add pit_diagnosis_agent.py agent_loop.py tests/test_pit_diagnosis_agent.py
git commit -m "feat: add closed PIT diagnosis agent protocols"
```

### Task 9: Add the Quarantined `pit_diagnosis` Controller Gate

**Files:**

- Modify: `pit_diagnosis_agent.py`
- Modify: `agent_loop.py:11005-11746`
- Modify: `tests/test_pit_diagnosis_agent.py`
- Modify: `tests/test_agent_loop.py`

**Interfaces:**

- Consumes: sealed deterministic diagnosis run plus SHA-256, bundle plus SHA-256, fact cache plus SHA-256, rulebook/catalog hashes, existing source preflight/candidate/Docker/audit/budget capabilities, and Task 8 protocols.
- Produces: `PitDiagnosisGateConfig`, `PitDiagnosisLoopServices`, `PitDiagnosisLoopResult`, the exact `run_pit_diagnosis_loop` signature defined in Step 4, and additive CLI choice `--gate pit_diagnosis`.
- Produces hidden worker boundary: `pit_diagnosis.py run-experiment` returning the prefix `PIT_DIAGNOSIS_RESULT=` followed by one canonical JSON object.
- Guarantees: normal samples use orchestrator then reasoner; coder is called only for a selected `requires_code=true` experiment with a nonempty controller-owned replacement list; all source patches remain inert and quarantined.

- [ ] **Step 1: Write RED two-call, three-call, and role-boundary tests**

```python
def test_config_experiment_uses_two_calls_and_never_calls_coder(pit_loop_fixture) -> None:
    result = pit_loop_fixture.run(selected_experiment="D3.M_CONFIRMED_UPTREND")
    assert pit_loop_fixture.gateway.roles == ["orchestrator", "reasoner"]
    assert result.selected_experiment_id == "D3.M_CONFIRMED_UPTREND"
    assert result.coder_called is False
    assert result.source_modified is False


def test_code_experiment_requires_exact_controller_replacement(pit_loop_fixture) -> None:
    fixture = pit_loop_fixture.with_synthetic_code_experiment(
        experiment_id="D4.TEST_STRUCTURAL_VARIANT",
        replacement=approved_exact_replacement(),
    )
    result = fixture.run(selected_experiment="D4.TEST_STRUCTURAL_VARIANT")
    assert fixture.gateway.roles == ["orchestrator", "reasoner", "coder"]
    assert result.exported_diff_sha256 == fixture.expected_diff_sha256
    assert result.source_modified is False


def test_orchestrator_route_is_rejected_before_reasoner_when_it_contains_unknown_evidence(
    pit_loop_fixture,
) -> None:
    pit_loop_fixture.gateway.route_evidence_ids = ("UNKNOWN",)
    result = pit_loop_fixture.run()
    assert result.terminal_status == "protocol_rejected"
    assert pit_loop_fixture.gateway.roles == ["orchestrator"]
```

- [ ] **Step 2: Run and confirm RED**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_agent.py tests/test_agent_loop.py -k "pit_diagnosis"
```

Expected: FAIL because the PIT gate and dispatch do not exist.

- [ ] **Step 3: Validate and snapshot all deterministic inputs**

`PitDiagnosisGateConfig` requires absolute regular non-symlink paths and lowercase SHA-256 values for the complete diagnosis run manifest, PIT bundle, fact cache, rulebook, and experiment catalog. Snapshot them under the controller temp root before any paid call, validate `verify_diagnosis_run`, require D0 pass, reject locked-evaluation metrics in provider evidence, and mount raw inputs read-only only inside the network-disabled worker. Models receive only `PitAgentEvidence.to_provider_payload()`.

- [ ] **Step 4: Implement the closed route/reason/optional-code state machine**

The controller enumerates domains/evidence/experiments; intersects route evidence IDs with the selected domain; sends that bounded subset plus matching rule/invariant records to the reasoner; validates the one selected experiment; and runs it deterministically. For `requires_code=false`, skip coder and source snapshots. For `requires_code=true`, require an exact catalog replacement, obtain one typed coder replacement, validate equality, apply only to a disposable candidate, and run the experiment plus focused quality gate inside the attested worker.

Use this public boundary:

```python
def run_pit_diagnosis_loop(
    config: PitDiagnosisGateConfig,
    source_state: SourceState,
    candidate: Candidate,
    audit: AuditTrail,
    services: PitDiagnosisLoopServices,
) -> PitDiagnosisLoopResult:
    controller = PitDiagnosisController(config, source_state, candidate, audit, services)
    return controller.run()
```

- [ ] **Step 5: Publish sanitized agent linkage without mutating deterministic results**

Create a fresh derivative result directory containing `diagnosis_link.json`, sanitized `agent_events.jsonl`, selected experiment output/hash, and final summary. `agent_events.jsonl` includes only event type, timestamp, role, experiment ID, outcome, call-record hash, and deterministic-result hash. Validated role payloads, call accounting, provider records, and diffs remain solely under the existing audit root.

- [ ] **Step 6: Add side-effect-free CLI dispatch**

Extend `--gate` choices to `("test", "backtest", "pit_diagnosis")` and add these PIT-only options: `--diagnosis-run`, `--diagnosis-manifest-sha256`, `--pit-bundle`, `--pit-bundle-sha256`, `--fact-cache`, `--fact-cache-sha256`, `--rulebook`, `--rulebook-sha256`, `--experiment-catalog`, and `--experiment-catalog-sha256`. Reject PIT options for existing gates and existing gate options for PIT. Reject `--proposal-samples` for PIT until a separate bounded PIT batch is explicitly designed.

- [ ] **Step 7: Prove existing gates retain behavior**

Snapshot parser defaults and representative `LoopConfig` values for `test` and `backtest` before the change; assert they are identical afterward. Run the existing state-machine, proposal-batch, quarantine, sandbox, accounting, and CLI tests with fake providers only.

- [ ] **Step 8: Verify and commit**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_agent.py
python -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "state_machine or proposal_batch or cli or sandbox or accounting"
python -m ruff check pit_diagnosis_agent.py agent_loop.py tests/test_pit_diagnosis_agent.py tests/test_agent_loop.py
python -m compileall -q pit_diagnosis_agent.py agent_loop.py
git diff --check
```

Expected: PASS with zero provider calls.

Commit:

```powershell
git add pit_diagnosis_agent.py agent_loop.py tests/test_pit_diagnosis_agent.py tests/test_agent_loop.py
git commit -m "feat: add quarantined PIT diagnosis gate"
```

### Task 10: Document, Fully Verify, and Review the Deliverable

**Files:**

- Modify: `README.md:119-160`
- Modify: `README.md:480-end`
- Create: `docs/pit-canslim-strategy-diagnosis.md`
- Modify: tests only if full verification exposes a genuine contract gap

**Interfaces:**

- Documents: deterministic build/run/resume/verify commands, artifact meanings, fidelity labels/blockers, locked-evaluation rule, agent role boundaries, offline fake-gateway verification, and separately authorized canary workflow.
- Produces: a committed bounded summary of the first real deterministic diagnosis; no raw rows, private paths outside the repository, or provider data.

- [ ] **Step 1: Document the operator workflow and first result**

Add the exact commands from Task 7, explain that the fact cache is incremental and immutable after finalization, define every required artifact, and record the actual Task 7 manifest/fact-cache hashes and closed headline findings. State prominently that current results are research diagnostics and `fidelity_incomplete` until required I/group/base evidence passes; do not market 2025 as unseen.

- [ ] **Step 2: Document the three conceptual agents and deterministic controller**

Use this role table verbatim in meaning:

| Component | Authority |
| --- | --- |
| Controller | Data, rulebook, experiments, commands, metrics, fidelity, budgets, pass/fail |
| Orchestrator | Select one supplied domain and supplied evidence IDs, or abort |
| Reasoner | State one falsifiable hypothesis and select one supplied experiment ID |
| Coder | Return one exact supplied source replacement only when code is required |

Explain that external methodology knowledge is versioned locally by humans; agents cannot browse or retrieve it.

- [ ] **Step 3: Run focused deterministic verification**

Run:

```powershell
python -m pytest -p no:cacheprovider --no-cov -q `
  tests/test_pit_diagnosis_rulebook.py `
  tests/test_pit_diagnosis_catalog.py `
  tests/test_pit_diagnosis_patterns.py `
  tests/test_pit_diagnosis_fact_cache.py `
  tests/test_pit_diagnosis_baseline.py `
  tests/test_pit_diagnosis_experiments.py `
  tests/test_pit_diagnosis_cli.py `
  tests/test_pit_diagnosis_agent.py
```

Expected: PASS.

- [ ] **Step 4: Run the full offline project gate**

Run with no provider credentials forwarded to children:

```powershell
$env:FMP_DAILY_REQUEST_BUDGET='0'
$env:HTTP_PROXY='http://127.0.0.1:9'
$env:HTTPS_PROXY='http://127.0.0.1:9'
python -m pytest -p no:cacheprovider --no-cov -q
python -m ruff check .
python -m compileall -q .
git diff --check
```

Expected: PASS. Do not invoke an OpenRouter canary.

- [ ] **Step 5: Reverify the immutable real diagnosis after the test suite**

Resolve the latest completed run exactly as in Task 7 Step 8, run `python -B pit_diagnosis.py verify-result --run-dir $diagnosisRun.FullName`, and compare its manifest SHA-256 with the Task 7 task report. Expected: unchanged hash and exit 0.

- [ ] **Step 6: Dispatch two final subagent reviews**

Give the first reviewer the spec, this plan, merge-base diff, test outputs, and real diagnosis summary; ask only whether every fidelity/data/leakage/experiment/publication requirement has direct evidence. Give the second reviewer the same inputs and ask only whether any agent-controlled value can become a command, scope expansion, arbitrary experiment, methodology change, metrics change, raw-data disclosure, source write, or promotion. Resolve every Critical/Important finding, rerun the affected focused tests, then rerun Steps 4-5.

- [ ] **Step 7: Commit documentation and final review fixes**

```powershell
git add README.md docs/pit-canslim-strategy-diagnosis.md
git commit -m "docs: document PIT CANSLIM diagnosis workflow"
```

Commit any review fix separately with only the exact affected source/test paths named in that review. Never add `.artifacts`.

## Completion Evidence

Before declaring implementation complete, report:

- clean branch and commit list;
- corrected baseline reproduction hash and exact pass result;
- fact-cache path, schema hash, content hash, row/session counts, and resume evidence;
- deterministic diagnosis run path and manifest hash;
- discovery/validation rule funnel, proper-base attribution, M attribution, exit attribution, expectancy, average cash, PIT-exposed recall, and performance;
- explicit unavailable/unimplemented fidelity rules and `promotion_eligible=false` when any remain;
- proof that locked evaluation was not run;
- proof that tests made zero provider calls and source/paper/live paths were unchanged;
- focused/full pytest, Ruff, compileall, and `git diff --check` results;
- both final subagent review outcomes.

Do not push, merge, run a paid canary, or apply any inert proposal without a new explicit user instruction.
