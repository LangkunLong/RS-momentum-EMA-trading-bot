# Issue #98 implementation report

**Issue:** #98, Build current-data feature and complete market-context adapters

**Base:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`

**Base tree:** `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`

**Implementation commit / tested source head:** `69d54e0a8a429fb50c1da7ad2d82295e171fb00d`

**Branch:** `codex/issue-98-current-feature-context-adapters`

**Worktree:** `C:\Users\llong\.codex\worktrees\855a\RS-momentum-EMA-trading-bot`

**Starting check:** clean at the required base commit and tree before edits.

## Scope and changed paths

Changed only these issue-scoped paths:

- `core/current_policy_inputs.py` — offline current feature/context adapter, clock and publication-date boundary records, complete-universe handoff checks, detailed missingness.
- `tests/test_current_feature_context_adapter.py` — focused offline adapter and acceptance tests using only explicit pytest temporary database paths.
- `tests/fixtures/issue98_recorded_current_snapshot.json` — synthetic, non-account input facts and date/missingness records.
- `docs/current-feature-context-adapter-issue98.md` — adapter contract, pre-policy handoff, timing, and limitations.
- `docs/issue98-implementation-report.md` — this criterion/evidence report.

`core/pit_feature_snapshot.py`, `core/strategy_policy/market_context.py`, and `core/stock_screening.py` were not changed. The adapter calls the accepted feature and context builders unchanged. It does not fetch data, activate a runtime, create policy decisions/actions, or perform account/broker work. The adapter is the callable pre-policy boundary; a runtime caller must invoke it before legacy scanner ranking. Runtime wiring is outside #98's assigned scope.

The brief named lead checkout 9558, which contains untracked lead documents and is outside this task's writable roots. The attached writable isolated checkout 855a was clean and matched the exact required base commit and tree; only 855a was edited.

## Acceptance criteria and evidence

### 1. Equivalent facts produce equivalent feature snapshots, breadth, and benchmark context

**Evidence:** `tests/test_current_feature_context_adapter.py::test_current_adapter_matches_historical_builders_and_keeps_legacy_excluded_candidates` compares every candidate's adapter `EntryFeaturesV3` with direct `build_entry_features_v3` output and compares canonical `MarketContextV1` JSON with direct `build_market_context` output. It checks the SPY/QQQ/IWM context through exact context equality, preserves the full 495-member S&P development denominator, and observes breadth/RS coverage fractions of `3/495`, `2/495`, and `3/495` with current inputs missing for the other 492 members.

**Limit:** this is deterministic parity on the same synthetic, authenticated development bundle. It is not independent real-provider acceptance or a production-universe measurement.

### 2. Legacy scanner prefilters do not silently remove candidates before policy decisions

**Evidence:** the synthetic `AAA` member has RS 35, below the scanner's `MIN_RS_SCORE`, and remains in `candidate_symbols` and the adapter output. `test_current_adapter_rejects_omitting_a_legacy_filtered_candidate_with_inputs` shows the adapter rejects an active member being labeled unavailable when the close and RS are both present. `test_current_adapter_rejects_unexplained_omissions_from_active_universe` requires a retained unavailable-input record for every active member omitted from feature evaluation.

Members with no current inputs are returned in `unavailable_members` with a detailed #66 state, reason, and source identity. They remain in the full context denominator and are not labeled policy rejections. This issue does not wire the adapter into runtime scanner code.

### 3. Unavailable fields stay explicit; current facts do not leak into historical evidence

**Evidence:** optional unavailable fields stay `None` and require a detailed missingness record. The test checks `sector_rs is None`, `unsupported_scope`, and the linked `unavailable` policy-boundary state; it also covers absent quarterly fields and insufficient 52-week history. The synthetic bundle contains a filing normalized to 2026-04-02 after the completed 2026-03-31 feature session. The adapter retains that raw/normalized date record, while feature age remains based on the last prior visible observation (2025-11-03), matching the unchanged PIT feature builder.

Raw source-public dates derive availability at the first supplied exchange session strictly after the public date, including an intraday publication and a holiday. Already-normalized availability is validated and returned unchanged, preventing a second shift.

## Fixture and identity hashes

All fixture calculations are synthetic and offline. The SQLite fixture is built under an explicit temporary path; its generated database was removed after hashing.

| Identity | SHA-256 |
| --- | --- |
| `tests/fixtures/issue98_recorded_current_snapshot.json` | `8709220431a30be7b21aa9de988a602569704dab11a5bfe564dccb666c3978d2` |
| Generated synthetic schema-V2 SQLite bundle (future filing included) | `28c3f130409dd8b41f4e72cc73f8fae11c3b480b1a2bbbb248fa94298ce6c317` |
| `core/current_policy_inputs.py` | `3d237da2e75404fee928392efd4c783443ef35c4cc211b91e722663519c164fb` |
| `tests/test_current_feature_context_adapter.py` | `a4f78547687973f08b834be49f4eb0d911aa971e6724165f134c98c6d55db10b` |
| `docs/current-feature-context-adapter-issue98.md` | `3420b8d6996b3af1c8dd48d9fd5aaa63f860d279fae4af7f3d166820536b4644` |

The input schema-V2 fixture is explicitly S&P-only development evidence. Schema V3 source-universe identifiers are preserved by the adapter, but the unchanged V3 feature builder requires an RS value for every active member and fails closed when that cross-section is incomplete. Schema V3 format alone does not establish production readiness or provider acceptance.

## Commands and results

Final focused verification, against implementation commit `69d54e0a8a429fb50c1da7ad2d82295e171fb00d`:

```text
python -m pytest -p no:cacheprovider --no-cov tests/test_current_feature_context_adapter.py tests/test_historical06_financial_semantics.py tests/test_strategy_policy.py -q
59 passed, 2 warnings in 10.10s

python -m pytest -p no:cacheprovider --no-cov tests/test_stock_screening.py -q
23 passed, 2 warnings in 3.62s

python -m ruff check core/current_policy_inputs.py tests/test_current_feature_context_adapter.py
All checks passed!
```

The two pytest warnings were the existing `cache_dir` setting being unrecognized because the cache plugin was disabled to avoid writes outside the worktree, and the installed `websockets.legacy` deprecation warning. The implementation commit's pre-commit hooks also passed: Ruff, trailing whitespace, end-of-file, and merge-conflict checks. The full repository suite was not run; verification stayed within the assigned focused evidence and did not make provider requests.

### Preserved red/intermediate evidence

- Test-first red: `python -m pytest tests/test_current_feature_context_adapter.py -q` failed during collection with the expected `ModuleNotFoundError: No module named 'core.current_policy_inputs'` before the adapter existed. That invocation also emitted pytest-cache permission warnings.
- First adapter run: three tests failed because the synthetic test tickers used digits (`X000`), which the repository's canonical PIT ticker validator correctly rejects. The fixture was changed to canonical alphabetic tickers.
- Next focused run: one test failed from a test-helper `NameError` (`active` was unpacked as `_active`). The helper binding was corrected.
- The later full focused rerun passed all 59 tests listed above, including the expanded omitted-member and source-date cases.
- An initial generated-bundle hash attempt using Windows system temp was denied by the filesystem sandbox before a database could be created. Sandbox access also prevented inspecting/removing that helper directory. The successful hash above was computed under `.artifacts/pytest` inside the writable worktree; that temporary database and directory were removed.

## Remaining limitations

- No real current-provider records or provider acceptance were available; all retained test inputs are synthetic and offline.
- The evidence uses S&P-only schema V2 in explicit development mode. It does not claim the full S&P 500/Nasdaq-100/Russell 2000 current-data coverage or production readiness.
- No scanner/runtime action wiring, broker/account access, store migration, deployment, or policy promotion was performed.
- Independent full-diff review and integration/publication remain with the lead. This report is implementation evidence, not independent acceptance.

## Four separate statuses

| Status | Assessment |
| --- | --- |
| **Implementation** | Implemented and locally committed at `69d54e0a8a429fb50c1da7ad2d82295e171fb00d`; independent review pending. |
| **Required inputs** | Synthetic offline non-account fixture available. Real recorded-provider acceptance is separate and not supplied. |
| **Acceptance evidence** | Focused local evidence maps to all three criteria within synthetic development scope; independent acceptance and production data evidence remain pending. |
| **Dependencies** | #66, #80, and #97 start prerequisites were confirmed eligible by the lead brief. No #100 or runtime integration dependency blocks this independent adapter preparation. Lead review/integration remains outstanding. |
