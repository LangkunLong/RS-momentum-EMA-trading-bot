# Issue #98 implementation report

**Issue:** #98, Build current-data feature and complete market-context adapters

**Base:** `ab385d792e19ff6db39d87f1123f47f660fc1e1d`

**Base tree:** `46a4b97a29f5fe05b395fc4ff5e36d6451db9bdc`

**Initial implementation commit / tested source head:** `69d54e0a8a429fb50c1da7ad2d82295e171fb00d`

**Round 1 correction commit / tested source head:** `51053ad3129a06675975f9fc80488fde545abc1d`

**Prior report commit:** `43b4cd9b97dd17ce50d3f194d15ad698a4113a5c`

**Branch:** `codex/issue-98-current-feature-context-adapters`

**Worktree:** `C:\Users\llong\.codex\worktrees\855a\RS-momentum-EMA-trading-bot`

**Starting check:** clean at the required base commit and tree before edits.

## Scope and changed paths

Changed only these issue-scoped paths:

- `core/current_policy_inputs.py` — offline current feature/context adapter, exchange-close evidence and timezone checks, complete recorded-input identity, complete-universe handoff checks, and detailed missingness.
- `tests/test_current_feature_context_adapter.py` — focused offline adapter and acceptance tests, including controlled V3 multi-universe parity and identity/timing rejections.
- `tests/fixtures/issue98_recorded_current_snapshot.json` — synthetic, non-account input facts, clock evidence, and date/missingness records.
- `docs/current-feature-context-adapter-issue98.md` — adapter contract, pre-policy handoff, timing, recorded-input identity, and limitations.
- `docs/issue98-implementation-report.md` — this criterion/evidence report.

`core/pit_feature_snapshot.py`, `core/strategy_policy/market_context.py`, and `core/stock_screening.py` were not changed. The adapter calls the accepted feature and context builders unchanged. It does not fetch data, activate a runtime, create policy decisions/actions, or perform account/broker work. The adapter is the callable pre-policy boundary; a runtime caller must invoke it before legacy scanner ranking. Runtime wiring is outside #98's assigned scope.

The brief named lead checkout 9558, which contains untracked lead documents and is outside this task's writable roots. The attached writable isolated checkout 855a was clean and matched the exact required base commit and tree; only 855a was edited.

## Acceptance criteria and evidence

### 1. Equivalent facts produce equivalent feature snapshots, breadth, and benchmark context

**Evidence:** `tests/test_current_feature_context_adapter.py::test_current_adapter_matches_historical_builders_and_keeps_legacy_excluded_candidates` compares every candidate's adapter `EntryFeaturesV3` with direct `build_entry_features_v3` output and compares canonical `MarketContextV1` JSON with direct `build_market_context` output. It checks the SPY/QQQ/IWM context through exact context equality, preserves the full 495-member S&P development denominator, and observes breadth/RS coverage fractions of `3/495`, `2/495`, and `3/495` with current inputs missing for the other 492 members.

`test_schema_v3_three_universe_fixture_matches_historical_builders` opens a valid generated schema-V3 PIT bundle whose controlled members span Nasdaq-100, Russell 2000, and S&P 500; AAA overlaps the first and third, so the active union remains three members. It compares every current adapter feature and the canonical market context against the unchanged historical builders. The result reports all three universe IDs and is not labeled development-only.

**Limit:** both tests are deterministic parity on synthetic, hash-verified local bundles. They are not independent real-provider acceptance or a production-universe measurement.

### 2. Legacy scanner prefilters do not silently remove candidates before policy decisions

**Evidence:** the synthetic `AAA` member has RS 35, below the scanner's `MIN_RS_SCORE`, and remains in `candidate_symbols` and the adapter output. `test_current_adapter_rejects_omitting_a_legacy_filtered_candidate_with_inputs` shows the adapter rejects an active member being labeled unavailable when the close and RS are both present. `test_current_adapter_rejects_unexplained_omissions_from_active_universe` requires a retained unavailable-input record for every active member omitted from feature evaluation.

Members with no current inputs are returned in `unavailable_members` with a detailed #66 state, reason, and source identity. They remain in the full context denominator and are not labeled policy rejections. This issue does not wire the adapter into runtime scanner code.

### 3. Unavailable fields stay explicit; current facts do not leak into historical evidence

**Evidence:** optional unavailable fields stay `None` and require a detailed missingness record. The test checks `sector_rs is None`, `unsupported_scope`, and the linked `unavailable` policy-boundary state; it also covers absent quarterly fields and insufficient 52-week history. The synthetic bundle contains a filing normalized to 2026-04-02 after the completed 2026-03-31 feature session. The adapter retains that raw/normalized date record, while feature age remains based on the last prior visible observation (2025-11-03), matching the unchanged PIT feature builder.

Raw source-public dates derive availability at the first supplied exchange session strictly after the public date, including an intraday publication and a holiday. Already-normalized availability is validated and returned unchanged, preventing a second shift.

`RecordedExchangeSessionCompletionV1` requires a dated IANA timezone, a recorded close instant, source identity, and a verified canonical record digest. `CurrentDecisionClockV1` rejects a pre-close cutoff and cutoffs with the wrong exchange timezone or exchange-local date. A controlled 13:00 shortened-session record is accepted without hardcoding a standard close time. Later valuation remains a separate aware timestamp.

The returned `recorded_input_manifest_sha256` binds the bundle digest, declared cutoff and completion record, source revision, candidate set, each supplied OHLCV frame, market-close frame, RS snapshot, regime inputs, missingness, fundamental-availability records, unavailable-member dispositions, and the V2 development flag. `test_recorded_input_identity_changes_with_same_session_market_data_and_cutoff` verifies deterministic identity for unchanged inputs and a changed digest when OHLCV, benchmark closes, RS, regime, distribution-day count, follow-through, or same-session cutoff changes. The existing bundle hash alone therefore no longer identifies independent values used by a snapshot.

## Fixture and identity hashes

All fixture calculations are synthetic and offline. The SQLite fixture is built under an explicit temporary path; its generated database was removed after hashing.

### Initial implementation identities (C1; preserved)

| Identity | SHA-256 |
| --- | --- |
| `tests/fixtures/issue98_recorded_current_snapshot.json` | `8709220431a30be7b21aa9de988a602569704dab11a5bfe564dccb666c3978d2` |
| Generated synthetic schema-V2 SQLite bundle (future filing included) | `28c3f130409dd8b41f4e72cc73f8fae11c3b480b1a2bbbb248fa94298ce6c317` |
| `core/current_policy_inputs.py` | `3d237da2e75404fee928392efd4c783443ef35c4cc211b91e722663519c164fb` |
| `tests/test_current_feature_context_adapter.py` | `a4f78547687973f08b834be49f4eb0d911aa971e6724165f134c98c6d55db10b` |
| `docs/current-feature-context-adapter-issue98.md` | `3420b8d6996b3af1c8dd48d9fd5aaa63f860d279fae4af7f3d166820536b4644` |

### Round 1 correction identities (C3)

| Identity | SHA-256 |
| --- | --- |
| `core/current_policy_inputs.py` | `a1af15fd7885857535a63d9c7b12b3736fdf0b9179f950c29bc1b5976b41ab6b` |
| `tests/test_current_feature_context_adapter.py` | `818d49477f2496d0782ef0b0a0afea75e724870a21bf9c1708b6ea65aa1b2e5e` |
| `tests/fixtures/issue98_recorded_current_snapshot.json` | `364e45c4db41104af4a42ee9156abed23aed35e17c17ee92c2d6f6468d9bd3ad` |
| Generated controlled schema-V3 SQLite bundle (three universes, one overlapping member) | `a7f93488f3b15dd2b6b56654bac29f09cddb67f36346139eb0c049926f9019cf` |
| `docs/current-feature-context-adapter-issue98.md` | `26576d583a6935020e512e5cd9cb07ff4eac18e9f07477515c6f50cffb940854` |

The input schema-V2 fixture is explicitly S&P-only development evidence. Schema V3 source-universe identifiers are preserved by the adapter, but the unchanged V3 feature builder requires an RS value for every active member and fails closed when that cross-section is incomplete. Schema V3 format alone does not establish production readiness or provider acceptance.

## Commands and results

### Initial implementation verification (C1; preserved)

```text
python -m pytest -p no:cacheprovider --no-cov tests/test_current_feature_context_adapter.py tests/test_historical06_financial_semantics.py tests/test_strategy_policy.py -q
59 passed, 2 warnings in 10.10s

python -m pytest -p no:cacheprovider --no-cov tests/test_stock_screening.py -q
23 passed, 2 warnings in 3.62s

python -m ruff check core/current_policy_inputs.py tests/test_current_feature_context_adapter.py
All checks passed!
```

Those two pytest warnings were the cache configuration warning caused by disabling the cache plugin and the installed `websockets.legacy` deprecation. That cache warning is resolved in the round 1 run below.

### Round 1 correction verification (C3)

```text
python -m pytest --no-cov --basetemp=.artifacts/pytest/issue98-r1-final-tmp -o cache_dir=.artifacts/pytest/issue98-r1-final-cache tests/test_current_feature_context_adapter.py tests/test_historical06_financial_semantics.py tests/test_strategy_policy.py tests/test_stock_screening.py -q
88 passed, 1 warning in 15.75s

py -3.13 -m ruff check .
All checks passed!

py -3.13 -m compileall -q -x '[/\\](\.git|\.worktrees|\.artifacts|\.venv)[/\\]' .
exit code 0; emitted "Can't list .\\.artifacts\\pytest\\pytest-cache-files-*" warnings for pytest cache temp directories.

py -3.13 -m compileall -q core/current_policy_inputs.py tests/test_current_feature_context_adapter.py
exit code 0; no diagnostics.
```

The one pytest warning is the installed `websockets.legacy` deprecation. The full-repository Python 3.13 compile command exited 0 despite the unreadable pytest cache temp directories; the changed files also compiled directly without diagnostics. Python 3.11 could not be run: `py -3.11` reported no suitable runtime, and the custom-tag path shown by `py -0p` was not executable. No Python 3.11 pass is claimed.

### Interrupted default-suite attempt

The full default pytest command was started once:

```text
python -m pytest --no-cov --basetemp=.artifacts/pytest/issue98-r1-full-tmp -o cache_dir=.artifacts/pytest/issue98-r1-full-cache -q
```

At the first 30-second yield, no pytest output or exit code had been captured. The nested execution handle was not retained. A later exact command-line lookup identified the Python 3.13 process as PID 27900, parent 38972, running this command. It was interrupted by the lead after verification and was confirmed absent. My own stop attempt found PID 27900 already gone; there is no completion output or test result. This run is not evidence of a pass. The broad default suite may include runtime/store or other out-of-scope fixtures, so its fixture exposure and any side effects are unverified; it was not restarted.

During recovery, I also mistakenly attempted `Stop-Process -Id 22384 -ErrorAction SilentlyContinue` after a local Python process listing. The lead's read-only process inspection identified PID 22384 as a pre-existing Chroma memory service, not this test run; it remained listed after the attempted stop, and the lead later verified it was still alive. After that identification, I made no further service-directed call or termination attempt. This attempt is disclosed here; no claim is made that it had zero transient effect.

### Preserved red/intermediate evidence

- Test-first red: `python -m pytest tests/test_current_feature_context_adapter.py -q` failed during collection with the expected `ModuleNotFoundError: No module named 'core.current_policy_inputs'` before the adapter existed. That invocation also emitted pytest-cache permission warnings.
- First adapter run: three tests failed because the synthetic test tickers used digits (`X000`), which the repository's canonical PIT ticker validator correctly rejects. The fixture was changed to canonical alphabetic tickers.
- Next focused run: one test failed from a test-helper `NameError` (`active` was unpacked as `_active`). The helper binding was corrected.
- The later full focused rerun passed all 59 tests listed above, including the expanded omitted-member and source-date cases.
- Round 1 test-first red: the pre-close clock and UTC-date boundary tests were accepted by the old clock, and the altered-input identity test failed because the snapshot had no manifest digest (`3 failed, 6 deselected`). Those failures drove the C3 clock and identity changes.
- A later pytest-cache setup initially emitted cache-directory warnings; pre-creating the explicit worktree cache directory produced the final 88-test run with only the unrelated dependency warning.
- An initial generated-bundle hash attempt using Windows system temp was denied by the filesystem sandbox before a database could be created. Sandbox access also prevented inspecting/removing that helper directory. The successful hash above was computed under `.artifacts/pytest` inside the writable worktree; that temporary database and directory were removed.

## Remaining limitations

- No real current-provider records or provider acceptance were available; all retained positive test inputs are synthetic and offline.
- Positive evidence includes S&P-only schema V2 in explicit development mode and a controlled three-member schema-V3 bundle. It does not claim full S&P 500/Nasdaq-100/Russell 2000 current-data coverage or production readiness.
- No scanner/runtime action wiring, broker/account access, store migration, deployment, or policy promotion was performed.
- The default full suite attempt was interrupted without a captured result, and its broader fixture exposure is unverified as described above.
- Independent re-review and integration/publication remain with the lead. This report is implementation evidence, not independent acceptance.

## Four separate statuses

| Status | Assessment |
| --- | --- |
| **Implementation** | Round 1 clock, input-identity, and V3-evidence corrections are committed at `51053ad3129a06675975f9fc80488fde545abc1d`; lead re-review pending. |
| **Required inputs** | Synthetic, hash-verified offline fixtures are available. Real recorded-provider evidence and provider acceptance are not supplied. |
| **Acceptance evidence** | The 88-test focused suite, Ruff, and Python 3.13 compilation passed within offline scope. Python 3.11 was unavailable locally. The default suite attempt has no result and is excluded from acceptance claims; independent acceptance and production data evidence remain pending. |
| **Dependencies** | #66, #80, and #97 start prerequisites were confirmed eligible by the lead brief. No #100 or runtime wiring dependency blocks this adapter preparation. Lead re-review/integration remains outstanding; #101–106/runtime work was not performed. |
