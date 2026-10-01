# Four-issue acceptance packet (working, 2026-10-01)

This packet reviews #67, #71, #82, and #68 against the issue bodies rechecked
on 2026-10-01. All four GitHub issues remain open. The accepted #66, #80,
#81, and #97 contracts remain unchanged at Git blobs `9ab5efcd9e5ac94d1cb301bc43d20dc9edc41026`,
`7eefe196f4de1a93ebaa84eff33684aba209edf9`,
`532a20fd25f01c9783db215418f7b744497dfd32`, and
`0c9eb19a118cf5bb1ad882c42374c6fc3a7cabe1`, respectively. #97's
qualified-policy-to-paper boundary and #107's separate review are outside
these four issues. No operational database, scheduler, broker, or paper runtime
was changed.

The corrected reporter was measured from clean integration source
`af77c86ce00de0b8b89fd94900139360a7adb0e3` on
`codex/four-issue-integration`. It has calculator ID
`pit-financial-features-v3` at Git blob
`71e562ffc00dc4e617f1cd625e3f13ea5fe3f81a`. The earlier #82 local
Docker image binds the 56-path map
`3d57003a79ae5cc85cb3e4320568217e8b793a642363cca2c504caaba0aa82cf`
at `d764c87`; current main integration changed that source closure. The
corrected 57-path map at `8a0896a` is
`f6dee0745545308088887a924b9839efedd8f1beff1dfbfac82fd889935819f0`.
A canonical local build and import smoke exist for this map; matched probes
remain pending.
The selected cross-issue suite passed **138 tests** at the measured source;
after the current-main merge and 57-path image closure fix
(`8a0896ac55dce180a14f3e06b08a1baaa1fd64e1`), the cross-issue suite
plus tests for the newly merged data-client policy passed **152 tests**.
Ruff and `git diff --check` passed. The broader offline suite
is informational. Its three selected parity cases still fail exactly as
described below.
An independent #67 review identified a terminal-Q4 slot-placement case under
the accepted #66 contract. The complete `d764c87` report is retained under
its original definition; the `af77c86` correction and newly measured report
supersede it for #67 acceptance.

## #67 — historical feature availability

The implementation is a staged, 20-field point-in-time coverage reporter.
Each field has explicit source, publication, lookback, calculation, and
declared policy-input stages. `consumer_paths` are static interface mappings;
`actual_policy_consumption=not_measured_no_policy_replay` means there was no
decision-by-decision policy read or strategy replay. The development bundle is
S&P-only schema V2, not the three-index production universe.
The full report command used the local `python` executable at
`C:/Users/llong/AppData/Local/Microsoft/WindowsApps/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/python.exe`;
an immediate post-run environment snapshot reports CPython 3.13.14,
pandas 3.0.1, NumPy 2.4.2, SQLite 3.50.4, and Windows
`11-10.0.26200-SP0`. Its pinned manifest and price-provenance SHA-256 values
are `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`
and `97dad8197d1fcc0d7a8cedb1a0bc13b76d7169b56a8b9c0a0e836c0a25c4f8a2`.
The checked-out #66 feature-specification bytes hash to
`21fa9c9826d1ade7bebb3ccd17f8c40bfaea3136e4bab53dbd7065fa12b46c8a2`;
the portable Git blob is named above.

| Exact published criterion | Current evidence and disposition |
| --- | --- |
| Explicit denominator and exclusions for every feature and calculation | The [corrected development report](historical-feature-coverage-report-v3.md) links the exact-source local `.artifacts/evidence/issue-67-v5-af77c86/coverage_report.json`: JSON SHA-256 `2a5a5394ae2d29c22d84a4cb63eea16339efd0bb40c03a790df49ad84788064d`; 631,965-row gzip SHA-256 `1a329025fdff9db156f6d2049ce3a6318712049cb12504bd937ae4e5dcce7019`. All 20 fields have member-session and year denominators, source/publication/lookback/calculation/policy-input stages and reasons, ticker counts, and nonmember/benchmark exclusions. Independent row-stream reconciliation found zero mismatches across 120 overall/year feature summaries and 1,895,895 full-window row partitions. Earlier v1 and `d764c87` evidence retain their original definitions. |
| Missing periods, late classifications, restatements, early warm-up | Fixed cases cover late publication, restatement, absent prior-year comparisons, warm-up, missing dated classification, and the absent newest fiscal quarter. The 84–105-day fiscal cadence rule reports an omitted long-gap quarter separately from an untrusted short-gap placeholder; an 83-day case confirms three matched plus one unavailable slot without falsely inferring a missing quarter. The C scorer is unchanged. |
| Newly measured development denominator | The corrected reporter schema v3 run exited zero at clean source `af77c86` and measured 1,255 SPY decision sessions, 631,965 member security-sessions, and 606 ticker symbols for 2021-01-04–2025-12-31 on bundle SHA-256 `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`. The original v2-calculator report and complete pre-Q4-correction `d764c87` report remain under their own definitions. The stopped `issue-67-v2-cb13f00` and `issue-67-v3-503ecdd` attempts are `INCOMPLETE` and excluded. |
| Fixed-history breadth, price RS, industry and missing market input denominators | The reporter's fixture covers 50/200-session breadth, price relative strength, SPY/QQQ/IWM benchmark sessions, missing price/benchmark history, and absent dated industry classification. The corrected report has 631,215 valid 50-session breadth cells, 629,600 valid 200-session breadth cells, 631,104 price-RS-ready cells, and 3,765/3,765 ready benchmark sessions; the independent reviewer recomputed these from raw bundle rows with zero mismatches. The development bundle has no industry snapshots, so all 631,965 member sessions lack dated industry assignment. |

| Status | Assessment |
| --- | --- |
| Implementation | Terminal-Q4 correction integrated at `af77c86`; the report/calculator blobs and offline calculation helpers remain unchanged after current-main merge `cc034ab`; 152 selected integrated tests pass. |
| Required inputs | Fixed histories and the retained development bundle, manifest and price provenance exist; a production three-index bundle is not needed for the bounded reporter measurement. |
| Acceptance evidence | The new `af77c86` exact-source development report and hashes are retained and independently reconciled: 631,965 included rows, all 20 fields on every row, zero duplicate security/session pairs, zero mismatches across 120 feature summaries and 1,895,895 full-window partitions, and zero market/benchmark mismatches. A separate post-merge review found unchanged reporter/calculator Git blobs and offline calculation helpers at `cc034ab`; the report remains comparable, while its actual execution identity remains `af77c86`. Its quarterly EPS four-growth-slot window is ready for **0/631,965** member sessions, with 604,048 `missing_fiscal_quarter_period` slots and 253 untrusted short-cadence placeholders across sessions. Ticker A on 2021-01-04 has an unavailable terminal 2020-10-31 Q4 slot followed by three matched slots. A read-only source sweep of distinct 2021–2025 EPS quarterly period ends found 2,192 gaps over 105 days among 7,982 period rows, including 1,452 exact 182-day gaps; 548/549 tickers with EPS period rows have at least one long gap. Source gaps differ from repeated security-session slot missingness. Static consumer mappings and policy-input availability are **not** actual consumption. |
| Dependencies | Accepted #66 and corrected #71 calculator identity; production-universe claims separately depend on #68. |

**Recommendation:** ready for bounded #67 reporter acceptance at the named
development source, with unchanged calculation semantics at the integrated
source. No production
coverage or replayed policy-consumption claim follows from that measurement.

## #71 — financial calculation validation

The versioned calculator applies the accepted #66 nearest prior-calendar-year
fiscal matcher within 28 days. V2 corrected observed-period freshness and
calendar-adjacent acceleration; V3 corrected fiscal quarter adjacency to an
inclusive 84–105-day endpoint gap. The input has period-end dates, not fiscal
quarter sequence IDs, so this is an explicit cadence proxy. Annual growth
across missing years and income/equity alignment for return on equity remain
documented modeling decisions rather than inferred observations.

| Exact published criterion | Current evidence and disposition |
| --- | --- |
| Skipped periods, adjacent acceleration, income/equity alignment, older restatements | Fixed cases in `tests/test_historical06_financial_semantics.py` and the [v2 validation](historical-financial-validation-issue71.md) cover these paths. The [v3 addendum](historical-financial-validation-issue71-adjacency-v3.md) adds a 91-day adjacent pair accepted for both EPS/revenue acceleration and a 182-day skipped pair rejected. |
| Reproducible mismatch and bounded correction or explicit modeling decision | The [v3 reproduction script](../tools/reproduce_issue71_adjacency_comparison.py) and [receipt](historical-financial-validation-issue71-adjacency-v3-reproduction.json) hash-check the unchanged development bundle and manifest, read V2 data without mutation, and compare V2/V3 on 2021-01-01–2025-12-31. Annual growth and ROE limits are recorded in the v2 validation. |
| Old output measured before error claims; changed definitions versioned | The earlier V2 comparison recorded 138,038 EPS and 158,883 revenue priced sessions becoming unavailable under the first correction; it remains under its original definition. The V3 pinned reproduction measured 8,696 priced quarterly-state windows per metric: EPS changed in 68 windows, 47 symbols, 6,007 priced sessions (24 available→unavailable, 44 unavailable→available); revenue changed in 77 windows, 54 symbols, 6,876 priced sessions (27 and 50). The original V3 zero-revenue statement was wrong and is explicitly superseded; its one-off scan was not retained, so the specific cause is unknown. These are feature-output differences, not trade errors. |
| Explicit source/input identity, comparable results, old reports preserved | Input bundle SHA-256 `cf729b47d762ae86287bb8a87c28a1664e7475c74ef1aca736fbc240111349de`; manifest SHA-256 `3c9edd0158dcb93f9de5edbfcf6f281dd8b25bcdd1ded6ad21473af5fd37be7c`. The successful reproduction's script SHA-256 is `bfec250376d6e03208ee83664513911f45adda271cb1522da918efc0166a5d50`; runtime CPython 3.13.14, pandas 3.0.1, SQLite 3.50.4, 799.531 seconds. Calculator V3 Git blob is named above. Original V2 reports are unchanged. |

The [baseline parity audit](historical-financial-parity-baseline-audit-issue71.md)
directly ran the same three broader cases at accepted main
`2c01e76a878a338ab2b743c38c4f1310aab3f75b` and an earlier integration
revision. At both earlier integration source `d764c87` and current-main
integration `cc034ab`, the same three failures persist:
two `ValueError: entry market context is invalid` at
`core/backtest_engine.py:1365` and one `KeyError: 'schema_version'` at
`core/pit_data.py:1264`. They predate #71, are unresolved, and do not turn the
versioned #71 correction into a passing broad parity suite.

| Status | Assessment |
| --- | --- |
| Implementation | V3 acceleration/freshness semantics and modeled annual-growth/ROE decisions integrated; no correction is pending. |
| Required inputs | Fixed histories and authenticated retained development bundle/manifest available. |
| Acceptance evidence | Fixed cases, corrected pinned before/after counts and runtime receipt, and baseline-versus-integrated parity disposition available. The final #67 report binds this same calculator Git blob. |
| Dependencies | Accepted #66 matcher; #67 uses calculator V3. Broader parity failures are separately unresolved. |

**Recommendation:** ready for bounded #71 review, with the superseded V3
revenue claim and unresolved pre-existing broad parity failures stated plainly.

## #82 — historical evaluator and candidate execution

The [earlier actual container receipt](research-03-candidate-container-evidence-2026-09-28.json)
records a Linux/amd64 Docker Engine 29.7.2 image with immutable config ID
`sha256:bf842ff5e0dc741f95e96129d224c24d7834feef23c4c20fdfe1d5297d54b527`
and raw-Git V5 map `a55875a2ea021ec27a9fd18d8e09b180ce9186fcaf1b3bbb14036db871e11fb4`.
At that earlier map, actual V5 parent and comment-only candidate containers
exited zero, each returned 11 fixed probes, shared fingerprint
`671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1`,
and had bounded output and owned cleanup. That image **does not** verify the
later `d764c87` evaluator map `3d5700…`. The [later local receipt](issue-82-local-d764-final-source-receipt-2026-10-01.md)
does verify that map: its 59-file canonical Docker build context matched raw Git blobs at
`d764c87`; image OCI index digest
`sha256:5a63234fc2b3e4771b0fccce6cd87fafb663cf343dc8d5efc2f61433c0276ea6`
and separate config digest
`sha256:e275320f8637e90d0a60ef3fc563f5596d29c4a11d59f1e0edcb381471cc33ad`
are bound to map `3d57003a79ae5cc85cb3e4320568217e8b793a642363cca2c504caaba0aa82cf`.
The local Docker Engine was 29.7.2, Linux/amd64. The retained `d764c87` local
`evidence.json` SHA-256 is
`33f331ece8a7b1e2b562860a0edf8a90ae9a0f1eb4cdecdaa6f7d80c35cbad56`,
and `probe.log` SHA-256 is
`4f49774aa112a6361e15cc91f5be50688ba8beb804c33c5bdea5c2c6efcae96b`.
The [new integrated-image build/import receipt](issue-82-local-8a-build-import-receipt-2026-10-01.md)
binds a 60-file raw-Git context, the corrected map, Docker backend image ID
`sha256:1e0e6327f48e96d4f9754c6bae47969f0d8cf57f3c2b6cb8a10ab2ece28af0df`,
and a successful no-network/no-mount panel import smoke with owned cleanup.
It retains a failed config-digest reference attempt separately. It is partial
evidence, not a matched parent/candidate run at `8a0896a`.

| Exact published criterion | Current evidence and disposition |
| --- | --- |
| Distinguish simulation, fixed probes and supplemental observations | [Simulation receipt](research-03-simulation-verification-2026-09-28.md) separates synthetic next-open/gap-stop portfolio simulation, fixed 11-probe worker results, and supplemental mechanism checks. No historical PIT panel or empirical strategy campaign was run. |
| Match parent/candidate assumptions; identify enabled/skipped stages | The `d764c87` Windows-host `DockerPanelEvaluatorV5 → LocalContainerExecutorV5` run mounted the same four-policy-file scope into separate parent and comment-only candidate containers from one immutable image. Policy revisions `76f67d16…` and `6d3008e8…`, source bundles `109a6199…` and `7c033b64…`, separate request/command/output hashes, and one matching 11-probe fingerprint `671a50c126b412b90fcaa466a034cade8d151686a90a8c1c1103c4f621ab4fc1` are in the receipt. Historical PIT panel and strategy campaign stages were skipped; no scenario data was mounted. The new integrated image imports its full panel path but has no matched parent/candidate output yet. |
| Bounded failures, cleanup, existing passing path | Focused failure/timeout/cleanup mechanisms and synthetic next-open/gap-stop simulator paths pass in selected tests. The `d764c87` actual parent/candidate containers each exited zero, emitted 3,584 bytes below the 64 MiB bound, used `network=none`, `--pull never`, read-only root and policy mounts, and completed owned evaluator/workspace cleanup. Targeted checks found neither owned run container after cleanup; an independent daemon query found no container with the `pit-v5-` name prefix. The failed full-checkout and offline build setup attempts are retained as setup failures, not candidate results. |

| Status | Assessment |
| --- | --- |
| Implementation | Existing simulator, evaluator, worker and focused mechanisms integrated; the new Alpaca helper is in the 57-path source and Docker allowlists. The corrected image built and imported the full panel path. |
| Required inputs | Synthetic bars/policies, canonical 60-file source context, pinned base/lockfile, and local Docker Engine were available. The requested hosted route also needs temporary manual-workflow publication on GitHub's default branch; that publication is held. No production PIT data or provider input is needed for the bounded probe. |
| Acceptance evidence | The earlier and `d764c87` local image/run receipts remain valid under their original source maps. The `8a0896a` local image and import-smoke receipt are retained; actual matched parent/candidate probe outputs and cleanup at this map remain missing. The unpushed manual-only Actions package was blocked by automatic publication review and was not dispatched. No hosted run is claimed. |
| Dependencies | Accepted #80/#81; historical production PIT data and empirical costs are outside the bounded synthetic verification. |

**Recommendation:** not ready for final integrated-source #82 acceptance until
matched parent/candidate containers execute at the corrected map and their
output/cleanup evidence is reviewed. The user prefers the hosted runner;
workflow publication and dispatch remain an explicit approval gate here.
The `d764c87` run remains a valid dated synthetic result and does not accept a
production PIT evaluation, historical strategy result, or global policy
equivalence.

## #68 — historical membership and security lineage

The [source receipt](historical-membership-source-receipt-v1.md),
[acquisition and transition plan](historical-membership-acquisition-plan-v1.md),
[partial identity ledger](price-identity-transition-evidence-ledger-v1.json),
and [segment decision](price-identity-segment-contract-decision-v1.md) define
the exact gap. Integrated code has an opt-in, hash-bound V3 segment contract,
normalizer and builder/verifier forwarding, plus a V5 production-admission
guard with an **empty approved evidence-pair set**. Synthetic FISV→FI→FISV
fixtures exercise representation and rejection. They do not authenticate
production source bytes or add union membership rows.

| Exact published criterion | Current evidence and disposition |
| --- | --- |
| Effective date and retained source evidence for every event | **Missing production input.** The retained S&P stream has 711 secondary-source events/606 tickers and five official spot checks, without a verified opening/ending state or complete primary ledger. Nasdaq has selected notices and a nonadmitted secondary extraction, without an admitted seed/event pair. Russell 2000 has no retained historical membership file. |
| Overlaps, renames and short-lived events reconcile by security lineage | Synthetic normalizer and integrity fixtures pass; production overlap/short-lived counts and authenticated transition replay are missing. FISV→FI (2023-06-07)→FISV (2025-11-11) needs the three segments tied to original issuer/SEC source bytes, source locators, final price request-contract hash, and complete member events. Thirteen other one-way leads remain evidence-only. |
| Coverage/exclusions explicit; fund holdings are not membership | Inventory/exclusions are explicit; current constituents and fund holdings are excluded as historical evidence. A source-backed three-index union and per-index/pooled coverage report do not exist. |

| Status | Assessment |
| --- | --- |
| Implementation | V3/V5 representation and fail-closed integration complete for synthetic inputs; provider-native production adapter/artifact remains pending exact source files. |
| Required inputs | Rights-cleared 2020-12-31 (or earlier complete) seeds and every effective 2021–2025 change/correction for all three indices; stable security/share-class IDs; retained provider bytes and permitted use/derivation/retention; authenticated price transitions/segments. |
| Acceptance evidence | Only inventory, spot checks, partial ledger and synthetic tests; **no production three-index artifact or accepted coverage**. |
| Dependencies | Accepted #66; provider/source rights and data owner decision. V5 production membership admission remains closed. |

**Recommendation:** keep #68 open. Prefer an already licensed package meeting
the exact [handoff checklist](historical-membership-acquisition-plan-v1.md#licensed-input-handoff-packet-not-executed).
If none exists, request product/file/rights/price quotes for S&P DJI,
Nasdaq GIW/GIFFD or equivalent historical NDX composition, and FTSE Russell
DDS/historical RUT. Incremental cost, depth and derivation rights are unknown
until checked. No purchase, provider contact, or license commitment has been
made.

## Publication and closure

Draft [PR #112](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/pull/112)
holds the integration. Issues #67/#71/#82/#68 stay open until their own
criteria and evidence are reviewed and closure is authorized. A clean branch,
passing focused tests, fixture coverage, or worker completion alone does not
establish issue acceptance.
