# Issue 86 implementation and evidence

## Scope and source identity

Issue 86 adds the missing observation and report path for declined add-on decisions. The starting checkout was `C:\Users\llong\.codex\worktrees\210d\RS-momentum-EMA-trading-bot`, branch `codex/issue-86-report-evidence`, at commit `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab` and tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`. The principal assigned `core/backtest_engine.py` to this work for the minimum additive observation only. No accounting, policy choice, fill behavior, or historical report identity changes are in scope.

Initial implementation bytes in commit `6b1b6b7d2342da896565fce5d0f551b836f10764` were:

| File | SHA-256 |
| --- | --- |
| `core/backtest_engine.py` | `691BCE3078ECEA82C18818C93EA2A29A71946C49F4044439D1993C12FE337664` |
| `core/pit_optimizer_v5/artifacts.py` | `65D4D2CF7487C2F205B72FD274E33544142C8CA7BD62D9EC37FA316E22B2645B` |
| `core/pit_optimizer_v5/contracts.py` | `03648E3640D766AAF4B8871E541657A9993771BD1BF76103A40604DF613DEB38` |
| `core/pit_optimizer_v5/diagnostics.py` | `D542D39A027822F21390EBCDF56084C1910B521F88737D0B601F409CEA06356C` |
| `core/pit_optimizer_v5/production_runtime.py` | `2813B3D89512551FE86EA36E36A3F1913C3C1AAA315324ECF129CD6959A8C511` |
| `tests/test_issue86_report_evidence.py` | `777782C5318DDA9210578C3F52406F079D1D5E65CE6779CC733F5FA72BBBCC74` |

## Input identities

| Input | SHA-256 | Use |
| --- | --- | --- |
| `.artifacts/research-wave/issue-86-brief.md` in the research lead checkout | `9B2706B463E7DEBAFB2CA744B3763964955CE47D9694B4F1FF479B31535B0B70` | Original acceptance criteria and scope. |
| `.artifacts/coordination/director-offline-wave-activation-2026-10-03.md` in `C:\Projects\trading_bot\RS-momentum-EMA-trading-bot` | `81056DFBA63AA3F023CA76CC40E139DD3B1E14CCA457BE1BD13B6733648E7BBD` | Human-authorized offline workstream activation and boundaries. |
| `docs/strategy-policy-contract-v1.md` | `5A31BDD9015776F4B782097CFB07F0142202B90FAD9C3C7A1784E54FDA45C2A9` | Accepted #80 decision/schema contract. |
| `docs/research-reproducibility-index.md` | `5365F7533154BDDC7CBD5E10D424CD16F152BFD151C4F67462CBFE7EEE8933AF` | #81 canonical source, runtime, evaluator, mode, input, and evidence identity fields. |
| `.artifacts/issue-87/source-corrections.md` in the #87 worktree | `55CE8E3F065FA812F621E7E026A50D862EDFEBE6D3675322D96BF4AF20ED0811` | Required metric meaning and limitation corrections consumed by this implementation. |
| `.artifacts/issue-87/integration-label-check.md` in the research lead checkout | `C8555862B075CB5835D3E13BA65D14A94E51ECDA0986F415FD479933AAF621D0` | Read-only draft wording cross-check; it predates the final telemetry-label mapping and is not final acceptance. |
| `.artifacts/research-wave/review-issue86.md` in the research lead checkout | `BCED5C2C49FEFD0C769453140517F1DF6B9C10F7CF80FBAC54591C7EE14511A4` | Independent review findings fixed by the follow-up commit below. |

## Acceptance coverage

| Issue criterion | Evidence and result |
| --- | --- |
| Additional-purchase outcomes reach reports and role evidence. | `test_actual_engine_observation_reaches_v2_report` calls the actual add-on decision/queue/fill path, then the simulator's result assembly. It verifies queued/executed outcomes in semantics-v2 reporting. `test_persisted_report_reaches_next_investigator_request` serializes a typed evaluation with the report, reloads it, and constructs the actual next `RoleRequestV5` through `LocalRoleRequestFactoryV5.investigator_request`; assertions inspect the constructed `request.messages` payload as well as role evidence. |
| A candidate-authored rejection reason is retained or explicitly represented, and unknown reasons are not silently counted as zero. | The engine records exact validated reason-code counts on decline. The report preserves exact codes and separates unregistered codes. The investigator request carries the code count with a hash-bound metric identity and a provider-safe description in message schema v2. Unsafe reason prose is withheld and hash-bound. Role evidence projects at most 64 reason categories and carries an explicit omitted-category count with a hash manifest description. The issue test covers an unregistered reason and 66-category truncation. |
| Missing legacy observation is unavailable rather than a fabricated zero. | A completed fresh simulator checkpoint is serialized, its new telemetry fields are removed to emulate a legacy checkpoint, and the actual result recovery path marks telemetry `incomplete_legacy_checkpoint`. The report maps that internal state to `unavailable_legacy_checkpoint`, omits the declined count, and does not project a declined zero. Explicit `null` status is rejected as malformed rather than treated as an old checkpoint. `unavailable_unspecified` likewise cannot carry reason counts. |
| New semantics are versioned without retroactively changing old reports. | New summaries use report semantics v2 with explicit metric definitions and friction calibration limitations. `test_legacy_v1_report_bytes_and_decode_remain_unchanged` pins the existing v1 canonical SHA-256 to `5cccfb2c87faf7b6ec91ce3cf0b38945ff0cff04af1f5ab082090281604aa706` and covers decoding the old shape. V1 serialization omits v2-only fields. |

## Metric meanings and limits

- Gross annualized return is a same-path gross-of-configured-friction estimate: configured friction is added back to each observed equity point before calculating returns. It is not a zero-cost re-simulation or counterfactual.
- Estimated idle-cash drag scales net annualized return by the arithmetic mean of observed-session `gross_long_notional / total_equity` exposure fractions. Each observed session has equal weight; terminally liquidated sessions remain included. Interest and redeployment are not modeled.
- Scale-out opportunity cost is a hindsight gap from a scale-out execution price to the episode maximum completed-bar price. That maximum has no timestamp, so its order relative to a sale is unknown. The metric is not a realized loss or missed future upside.
- Friction bps are configured scenario assumptions: gross `(0, 0, 0)`, base `(2, 3, 0)`, and stress `(5, 10, 1)` for half-spread, market impact, and commission. Empirical calibration and provenance were not supplied; quote, volume, ADV, order-size, and participation inputs are absent.
- A legacy checkpoint cannot recover declines that happened before checkpointing began. Counts after resuming may be present, but the result remains explicitly incomplete.

## Follow-up review corrections

The independent review at `.artifacts/research-wave/review-issue86.md` identified two Important findings; both are fixed in the follow-up commit.

1. `RoleRequestV5.messages` now includes each non-null evidence description in its evidence payload and marks that shape with `evidence_schema_version: 2`. Every description passes the existing provider `_validate_safe_text` checks, and the existing aggregate request-byte bound still covers the added text. Requests without descriptions keep their exact legacy message shape and hashes; `test_extension_off_factory_requests_and_messages_match_recorded_baseline` verifies this. No path, symbol, or credential restriction was relaxed. Provider-visible digest wording is lowercase `sha256`; calibration wording expands average daily volume so it passes the same symbol filter.
2. `_project_v2_add_on_and_friction_evidence` is the single bounded projection used by both investigator and detailed report paths. The caller supplies its existing prefix, preserving distinct metric IDs and evidence order. A parity assertion compares both projections.

`test_persisted_report_reaches_next_investigator_request` now checks the actual serialized message for the unregistered candidate reason and digest, `unavailable_legacy_checkpoint` status, gross/idle-cash/scale-out definitions, and friction calibration caveat. It also verifies that unsafe reason prose is withheld and hash-bound, and that an unsanitized unsafe description is rejected at request construction.

Final follow-up source bytes before commit:

| File | SHA-256 |
| --- | --- |
| `core/backtest_engine.py` | `691BCE3078ECEA82C18818C93EA2A29A71946C49F4044439D1993C12FE337664` |
| `core/pit_optimizer_v5/artifacts.py` | `65D4D2CF7487C2F205B72FD274E33544142C8CA7BD62D9EC37FA316E22B2645B` |
| `core/pit_optimizer_v5/contracts.py` | `03648E3640D766AAF4B8871E541657A9993771BD1BF76103A40604DF613DEB38` |
| `core/pit_optimizer_v5/diagnostics.py` | `4AAF8E96D5ACEA279A26D1F22AF6CA1C0F89DE8AE8CC19FBAA6CED3B14BB4211` |
| `core/pit_optimizer_v5/production_runtime.py` | `2AE8ED82F18DACA57ED567A42EDB4458CD3049B67B6E7EC9CB09C3016406AF31` |
| `core/pit_optimizer_v5/provider.py` | `D3D21AFED930C649C94EBB41CA484555ABFA81B32436CBE09CC836EA921D4357` |
| `tests/test_issue86_report_evidence.py` | `02F06A8A3619C27D3BB6E45926A0C03A07CCBB91915AEFEEAC6B0A1C46373F7F` |

## Initial verification record (commit `6b1b6b7`)

Final selected command:

```text
py -3.13 -m pytest -q -p no:cacheprovider --no-cov tests/test_issue86_report_evidence.py tests/test_pit_optimizer_v5_evaluator_assumptions.py tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_extension_off_factory_requests_and_messages_match_recorded_baseline
```

Result: **6 passed, 2 warnings in 2.57s**. The warnings were an existing unrecognized pytest `cache_dir` option and a `websockets.legacy` deprecation warning. This run covers all four issue-specific tests, evaluator input assumptions, and the recorded extension-off role-request baseline. This is historical evidence for the initial implementation, not the follow-up bytes.

Additional checks passed with exit code 0:

- `git diff --check` (Git emitted only existing LF-to-CRLF worktree notices).
- `py -3.13 -m py_compile core\backtest_engine.py core\pit_optimizer_v5\contracts.py core\pit_optimizer_v5\diagnostics.py core\pit_optimizer_v5\artifacts.py core\pit_optimizer_v5\production_runtime.py tests\test_issue86_report_evidence.py`.

Negative outcomes are retained here rather than hidden:

- An earlier focused run exposed that the engine's `incomplete_legacy_checkpoint` label had not yet been translated to the report contract's `unavailable_legacy_checkpoint`. The mapping and assertion were corrected; the final selected run above passed.
- The first commit-hook pass removed five unused test imports and stopped because it had modified the staged test file. The formatted file was reviewed; static checks and the selected tests passed again before retrying the commit.
- The integrated test-selection note lists `tests/test_issue87_diagnostic_meanings.py`, which is absent from this isolated checkout. That selection stopped with “file or directory not found”; the lead confirmed #87's independently reviewed tests will run against the integrated branch. No #87 files were copied into this worktree.
- The lead supplied the unchanged starting baseline: 98 passed and one pre-existing failure in 323 seconds, `test_full_runtime_run_supplied_ports_publishes_after_current_critic_evidence`, caused by that existing fixture reaching a 260-character persisted path. This broad baseline was not rerun for #86.

## Follow-up verification

Command:

```text
py -3.13 -m pytest -q -p no:cacheprovider --no-cov tests/test_issue86_report_evidence.py tests/test_pit_optimizer_v5_mechanism_artifacts.py::test_extension_off_factory_requests_and_messages_match_recorded_baseline tests/test_pit_optimizer_v5_evaluator_assumptions.py
```

Result: **6 passed, 2 warnings in 3.34s**. It covers all issue-specific tests, exact legacy provider message compatibility, and evaluator-input assumptions. Warnings are the same unrecognized pytest option and `websockets.legacy` deprecation.

`git diff --check` and `py -3.13 -m py_compile core\backtest_engine.py core\pit_optimizer_v5\contracts.py core\pit_optimizer_v5\diagnostics.py core\pit_optimizer_v5\artifacts.py core\pit_optimizer_v5\production_runtime.py core\pit_optimizer_v5\provider.py tests\test_issue86_report_evidence.py` both exited 0.

TDD checks first failed because the request lacked `evidence_schema_version`; after adding descriptions, the existing provider filter surfaced uppercase `SHA` and `ADV` in explanatory text. Those terms were rendered as lowercase `sha256` and expanded “average daily volume”; the safety filter itself was not relaxed. The parity check then exposed the old paths' different unavailable-status wording; the shared helper now supplies the same bounded projection to both.

The fixture and request construction are local and synthetic. No provider, model, broker, market-data acquisition, campaign, migration, deployment, or issue-status operation was performed. Independent lead review and integrated-branch acceptance remain separate from these scoped results.
