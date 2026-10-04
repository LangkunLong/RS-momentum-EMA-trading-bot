# Issue #93: confirmation, qualification, and replay readiness

## Implementation

Issue #93 exposed two defects in the existing stage path. Confirmation and qualification used the generic JSON walker on evaluation-panel artifacts, although those artifacts use a dedicated raw serializer. The walker now authenticates those panel references as raw artifacts; the stage-specific panel loaders still validate the typed panel content when the stage consumes it. The initial implementation also decoded the sealed confirmation plan before walking the graph. Final review found that this decoded held-out data before the durable open-ledger boundary; the correction below now authenticates the raw plan before opening and decodes it only after the ledger is open.

The full-replay readiness path also omitted the required `pit_data_scope` argument when revalidating manifest bindings. It now supplies the scope from the authenticated manifest.

The bounded implementation and tests are committed as `109c10bad7e5dcfb66e54749574d5c65c96474e1` (`fix: verify research stage boundaries`). The tested source tree is `013f12e41242ef0d7fcef8a7b46921501cfa12b7`. Work began from commit `c6c5b715f77a1225ff9f6e456c8cf3d5145654ab`, tree `62685d7d5ce9d4851051ed5ec98be4b81a5cc757`, on `codex/issue-93-stage-boundaries`.

## Required inputs

The stage APIs were exercised with authenticated synthetic campaign, panel, policy, and retirement artifacts. No production market history, profitable winner, external model, provider, broker, or Docker execution was used. Evaluation workers returned controlled in-memory reports; a temporary Git repository supplied the four-file source identity required by readiness.

The retained run is in `.artifacts/issue-93/chain-run/`:

- `stage-chain-summary.json` records the artifact references, hashes, readiness flags, and synthetic source revision.
- `r/` contains the artifact repository, including the create-only readiness record.
- `s/` contains the synthetic policy-source Git checkout.
- `../pytest-results.txt` records the focused test command output.

The retained repository has root identity `43746b7adcef8ef8d03f32fcdf46407a9cb340c9ff14dd4704384af028932892`. Its readiness record hash is `9f0fa6d01c255ef53055798eafa10c7ed6ee09ee7f6cd1551602bbf0a8fbdf6a`; its authenticated artifact graph hash is `b46729f0b0fa9da610b8260c1e4c8cfc1bef7e4e62bff940eb836d76fb1f6178`.

Key input identities from the retained run:

| Identity | SHA-256 / value |
| --- | --- |
| Source revision tested | `109c10bad7e5dcfb66e54749574d5c65c96474e1` |
| Readiness policy-source commit / tree | `623ac1ed241de04f8955a02f1f46466dff4e45fe` / `e39300c294e54e47aab7755f5e00e438ac817f98` |
| Readiness source snapshot | `93bd45188fb3211e836c609c21e48e41fc28f1a8669ccfb04d64a63650eaa5c4` |
| Candidate policy artifact | `4d644e03af75e3cf2ad9b696e4cff61c2ce78c6c70404c9e1e4b28c5f1ecd30a` |
| Candidate source bundle | `034284abdc0dfbd830edb0e859acd8b0bb7f7cf6a2ba79f678766a0529f54661` |
| Synthetic PIT input bundle | `90370219a8db350e80e8ea0a8956e4a3360ec2ab472a0fcca043205aba776a39` |
| Synthetic price provenance | `d3b3ef14a8dd1f57b522e9211e5ab9438ec25d591e9d6910b1fa3e80db8e0966` |
| Evaluator contract / sandbox profile | `c0a681715eeab6c4d543ef7343390f9202dbadd78e4811d2d84237e9095da9bb` / `8ae84b189acaf3d028212dfe0a417c6ec026b915b77d937612fca96a9d7b80dc` |
| Synthetic evaluator image / runtime source | `issue-93-synthetic@sha256:4444444444444444444444444444444444444444444444444444444444444444` / `66fa38dac6ff545362d503503b5f34eb1f3ce6f578f315d6eb4336afa4411457` |

The full per-stage identity inventory is in `stage-chain-summary.json` and the readiness record in `r/full-replay-readiness.json`.

## Final review correction: held-out confirmation decode boundary

The final-review correction keeps the confirmation plan opaque while building and authenticating an attempt. The runner authenticates the raw plan reference before ledger use, then opens the durable retirement ledger before typed plan or panel decoding. A decode interruption after that boundary leaves a durable canceled outcome and retired ledger; retry returns the outcome without another evaluation.

The correction and regression are committed as `5e911f37ec3cc907df4def654d615ace66f6300b` (`fix: defer held-out confirmation decoding`), tree `80b9a9cda62da851dc5f307d1c046286a904ad44`. This commit follows `ef647ccc920d14dc2d40c8c159f132983ac3acb8`. The five focused stage-boundary cases passed on that exact commit. The retained run is in `.artifacts/issue-93/final-review-i1/`; its log and summary hashes are:

| Receipt | SHA-256 |
| --- | --- |
| `pytest-results.txt` | `4a9581483558d0cf4b28cf719162cc4a00b30fcb508d4edd596c58938454904d` |
| `positive/stage-chain-summary.json` | `728e2da809dfb01375f9d574dfd95acb472acbba60362b243ebbca31a5b257e9` |
| `heldout/heldout-ordering-summary.json` | `aa776e78a5b8785985475af9d54de1f8d2f00f7387b011f1c375db5b47ab6dfe` |

The positive summary binds the same source revision to the synthetic source commit `13897d2156726bfaeebf4c10b0fd4ff2ce70d226`, records readiness as non-executable with zero provider calls, and retains the create-only readiness artifact. The held-out summary records raw plan authentication in the builder and runner, raw authentication before ledger use, typed plan and panel decoding after opening, zero worker evaluations, and final retirement. The pytest output was `5 passed, 1 warning in 140.20s`; the warning is the existing unrecognized `cache_dir` pytest option.

The lead reran the bounded integration set on `48fca70e603dfaaa05cc73c8160fc38449f31b48`, tree `f1e8af247239a989013b88b26e7389acac296e7a`. The retained `integrated-4` focused log has SHA-256 `21f0bcc4d9af0dc4fcde0f5e35997d3244209b75fe0c0774eb5f59e1233f7d63`; it reports `15 passed, 2 warnings in 211.76s`. Its `verification.json` has SHA-256 `21c1dcbe74ea26f139f1b865f96d266c1cea9a1afa8ac4e70797f0f23afdcacf` and records focused, Ruff, and compile checks passing with the integrated checkout unchanged. The readiness observer completed source inspection and the public chain passed its snapshot and final-pin checks. The earlier `integrated-3` run remains recorded separately: it completed 14 cases and failed readiness with `source_not_clean_or_matching`, while its wrapper left checks empty. The synthetic source checkout for that run was clean at fixture commit `81af7e50a93096adaabebe005845064423b852fb`, tree `e39300c294e54e47aab7755f5e00e438ac817f98`. The original exception was not reproduced in `integrated-4`; its cause remains unknown, with no code fix attributed to it.

## Acceptance evidence

| Acceptance criterion | Evidence | Result |
| --- | --- | --- |
| Each stage requires the correct prior-stage evidence and preserves data separation. | `test_public_stage_chain_binds_policy_panels_and_non_executable_readiness` rejects a discovery finalization as qualification evidence, rejects a changed champion identity before ledger use, requires eligible confirmation for qualification, and checks disjoint confirmation/discovery lineages and nonoverlapping dates. | Pass |
| Tampered identities, reuse, and interrupted cleanup follow the contract. | The public-chain test rejects reused qualification authority. `test_tampered_panel_report_identity_retires_ineligible_and_cannot_unlock_qualification` verifies a tampered report produces a failed, ineligible confirmation. `test_confirmation_terminal_interruption_recovers_without_reevaluation` recovers after a durable terminal write without re-evaluation. `test_qualification_cleanup_recovery_preserves_failed_outcome_and_does_not_rerun` preserves the original failed result while recording supplemental cleanup and avoiding re-execution. `test_confirmation_keeps_heldout_plan_opaque_until_open_and_retires_decode_interruption` verifies raw authentication before open, typed decoding after open, retirement on decode interruption, and no reevaluation on retry. | Pass |
| Replay readiness remains distinct from replay execution and broker deployment. | The public-chain test verifies the readiness record is source-clean, cleanup-complete, and permanently retired while `executable=false`, `replay_started=false`, and `provider_calls=0`; its projection requires a separate explicit decision. | Pass |

Initial focused verification on the initial implementation commit:

```text
ISSUE93_EVIDENCE_ROOT=.artifacts/issue-93/chain-run
py -3.13 -m pytest -q tests/test_pit_optimizer_v5_issue93_stage_boundaries.py -p no:cacheprovider -o addopts= --tb=short
4 passed, 1 warning in 128.45s
```

The warning is pytest reporting the existing, unrecognized `cache_dir` option in `pyproject.toml`; it did not affect the run. Ruff and the commit hooks passed.

## Dependencies

The issue brief names #80’s strategy input/decision/experiment contracts as the start prerequisite; those contracts were inspected for this work. The brief also requires final acceptance on the integrated revision established by #81. The lead-owned `integrated-4` run at that revision passed the bounded focused set, Ruff, and compile checks; its source commit, tree, and retained log hashes are recorded above.

This result verifies synthetic stage behavior only. It does not establish production-data coverage, strategy profitability, a full replay, deployment, or broker readiness.
