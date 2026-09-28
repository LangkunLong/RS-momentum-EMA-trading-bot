# Research-03 simulation verification — 2026-09-28

This receipt records the bounded verification for issue [#82](https://github.com/LangkunLong/RS-momentum-EMA-trading-bot/issues/82), matching the published Research-03 issue body. The issue is open. The live issue body was checked against the saved register; it has no comments. Current upstream `main` was confirmed at `2c01e76a878a338ab2b743c38c4f1310aab3f75b`, which contains the #110 integration. This worktree started from that commit and its source tree `c40c6cb4b7b297559d24d40bb5fd55789c66691f` on `codex/issue-82-simulation-contract`.

## Four statuses

| Dimension | Status | Evidence and limits |
| --- | --- | --- |
| Implementation | Existing V5 simulator, evaluator, and worker-binding paths are present. This task adds tests and this receipt; it makes no production engine or worker changes. | `PortfolioSimulator` was exercised with deterministic synthetic bars. `PitPanelEvaluatorV5` forwarding and policy identity binding were exercised through a capturing simulator. |
| Required inputs | Available for bounded synthetic verification. | Inputs are artificial bars, a deterministic close-at-next-opportunity policy, a parent policy bundle, a comment-only candidate identity, an explicit execution profile, and a synthetic friction scenario. No production PIT bundle or empirical cost data was used. |
| Acceptance evidence | Focused checks pass for simulation behavior, matched evaluator inputs, paired fixed observations, and supplemental mechanism gates. Actual candidate sandbox execution remains unverified. | 15 selected tests passed. Docker CLI is present, but the Docker Engine pipe is absent; image availability is unknown. The evaluator-boundary test does not evaluate candidate policy methods or run candidate source. |
| Dependencies | The #80 policy contract and #81 reproducibility-index prerequisites are satisfied in the accepted #110 main integration. | No additional issue-level start prerequisite was identified. Candidate container verification still depends on an available Docker Engine and the pinned evaluator image. |

## Simulation input identity

The declared fixture is `_RESEARCH_03_V5_EXECUTION_INPUTS` in [`tests/test_backtest_open_causality.py`](../tests/test_backtest_open_causality.py). Its compact canonical JSON SHA-256 is `81b5afd19b90eff4d2684d9e7c6813f955ed58dd84131eccbb4c7f992856f580`. The fixture specifies symbol `LEAD`, 260 warm-up sessions, 30 evaluation sessions, $10,000 initial capital, 1% risk, an 8% stop, an entry reference of $100, and two exit paths: a next-open policy close at $103 and a gap through the stop to an $80 open. The static fixture also supplies the market context and reference series required by the simulator.

Execution is declared as V5 next-open policy exits, open-then-stop gap handling, last-session-close end handling, and half-spread plus impact plus commission. The scenario is synthetic `fixture-10-20-10-bps`: 10 bp half-spread, 20 bp impact, and 10 bp commission. These values are test assumptions, not measured trading costs.

The expected entry sizing is calculated independently in the test from the declared inputs. Risk budget is `$10,000 × 1% = $100`; target notional is `$100 / 8% = $1,250`. Entry execution is `$100 × (1 + 30/10,000) = $100.30`, and cash per share including commission is `$100.30 × 1.001 = $100.4003`. This yields about `12.450162` shares. The rounded stop is `$92.28`, allowing about `12.468828` shares under the risk ceiling, so the target-notional calculation binds. The entry cash debit rounds to `$1,250.00`. Applying the declared sell-side friction and commission yields final cash of `$10,027.24` on the next-open path and `$9,742.03` on the gap-stop path. The test checks both the independently derived quantity and cash arithmetic against the simulation fills.

## Evidence by stage

### Historical portfolio simulation

The parameterized test `test_v5_portfolio_fills_at_next_open_and_gap_stops_preempt_policy_exit` runs the actual V5 `PortfolioSimulator` twice. It checks transaction dates and reasons, reference and execution prices, quantity, stop reference, policy-intent outcomes, friction, and final cash. In the ordinary path, the close decision queued after the first session executes at the following open. In the gap path, the opening gap stop preempts that pending policy close. The policy is a deterministic V2 in-process test client; this fixture is a historical simulation contract check, not an optimizer panel result or strategy-performance claim.

### Fixed behavior probes

`test_fixed_and_supplemental_observations_use_full_snapshot_identity` passed. It checks fixed P0/A/S decision outputs against a declared truth table, and checks the supplemental synthetic input identities separately. `test_bounded_fixture_observations_are_paired_and_count_unique_relevant_cases` also passed using bounded deterministic fixture workers. Related selected checks confirmed exact parent/candidate context binding and failed-closed behavior when sandbox enforcement or resource limits are unavailable. These observations remain separate from the portfolio result.

### Supplemental mechanism observations

Selected checks passed for protocol timeout, cleanup failure, caller-owned deadlines, no retry, typed evaluator matching, and runtime paths that skip workers for disabled development, rejected candidates, missing executors, or expired deadlines. These are mechanism and stage-gating checks; they are not merged into historical portfolio results or presented as enabled candidate evidence.

### Matched evaluator assumptions

[`tests/test_pit_optimizer_v5_evaluator_assumptions.py`](../tests/test_pit_optimizer_v5_evaluator_assumptions.py) calls the actual `PitPanelEvaluatorV5` baseline and candidate entry points with a capturing simulator. It checks that both calls receive the same PIT bundle, identity-transition contract, execution profile, friction scenario, data scope, benchmark, signal cadence, symbols, dates, and warm-up start. It also checks separate parent and candidate policy identity bindings. Its simulator and worker clients are fixtures: it checks evaluator forwarding and binding only, and does not invoke policy methods, start a candidate worker process, or execute candidate source.

## Runtime and reproducibility

- Host: Windows 11; Python `3.13.14`.
- `requirements-lock.txt` SHA-256: `9f1f1f14f18c86a2471a3d81bb5d103b3a26bb37da7f753daf90bfd9b5b312b6`, matching the #81 index.
- Portable tracked test identities (Git blob IDs after clean filters): `tests/test_backtest_open_causality.py` is `432440258c512d7e4bb28d4989b16d4b83df8999`; `tests/test_pit_optimizer_v5_evaluator_assumptions.py` is `79dc727233e4540d6ea0e154b5d8075c70401be2`. These IDs identify the committed source independently of Windows checkout line endings.
- Focused run: the two parameter cases in `test_v5_portfolio_fills_at_next_open_and_gap_stops_preempt_policy_exit`, the evaluator-assumption test, eleven existing mechanism-evidence checks, and `test_fixed_and_supplemental_observations_use_full_snapshot_identity`. Result: **15 passed in 11.94s**.
- No provider, model, broker, or live-data calls were made. The full offline suite and historical provider attempts were not rerun.
- `git diff --check` passed. Shell fetch and remote-ref checks were unavailable in this worktree; the live `main` pointer and issue body were independently checked through the team lead's GitHub read.
- Docker CLI is installed, but `docker version` and image listing fail because `npipe:////./pipe/dockerDesktopLinuxEngine` is unavailable. The current pinned evaluator image digest may exist in historical records; those records do not prove that the image is available to this runtime.

## Disposition

No concrete production engine or evaluator failure was reproduced, so shared production modules remain unchanged. The focused acceptance evidence supports deterministic fill timing, stop precedence, declared-cost accounting, evaluator assumption forwarding, and the separation of simulation, probes, and supplemental observations. It does not establish candidate container execution, empirical friction realism, production market coverage, or strategy performance. Actual historical provider costs remain unknown as recorded in the accepted #89 incident index; this work does not estimate or reconcile them.
