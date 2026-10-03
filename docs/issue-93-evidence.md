# Issue #93: confirmation, qualification, and replay readiness

## Implementation

Issue #93 exposed two defects in the existing stage path. Confirmation and qualification used the generic JSON walker on evaluation-panel artifacts, although those artifacts use a dedicated raw serializer. The walker now authenticates those panel references as raw artifacts; the stage-specific panel loaders still validate the typed panel content when the stage consumes it. Confirmation loads its sealed panel plan before walking the graph so it can authenticate the committed episode reference.

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

## Acceptance evidence

| Acceptance criterion | Evidence | Result |
| --- | --- | --- |
| Each stage requires the correct prior-stage evidence and preserves data separation. | `test_public_stage_chain_binds_policy_panels_and_non_executable_readiness` rejects a discovery finalization as qualification evidence, rejects a changed champion identity before ledger use, requires eligible confirmation for qualification, and checks disjoint confirmation/discovery lineages and nonoverlapping dates. | Pass |
| Tampered identities, reuse, and interrupted cleanup follow the contract. | The public-chain test rejects reused qualification authority. `test_tampered_panel_report_identity_retires_ineligible_and_cannot_unlock_qualification` verifies a tampered report produces a failed, ineligible confirmation. `test_confirmation_terminal_interruption_recovers_without_reevaluation` recovers after a durable terminal write without re-evaluation. `test_qualification_cleanup_recovery_preserves_failed_outcome_and_does_not_rerun` preserves the original failed result while recording supplemental cleanup and avoiding re-execution. | Pass |
| Replay readiness remains distinct from replay execution and broker deployment. | The public-chain test verifies the readiness record is source-clean, cleanup-complete, and permanently retired while `executable=false`, `replay_started=false`, and `provider_calls=0`; its projection requires a separate explicit decision. | Pass |

Focused verification on the tested commit:

```text
ISSUE93_EVIDENCE_ROOT=.artifacts/issue-93/chain-run
py -3.13 -m pytest -q tests/test_pit_optimizer_v5_issue93_stage_boundaries.py -p no:cacheprovider -o addopts= --tb=short
4 passed, 1 warning in 128.45s
```

The warning is pytest reporting the existing, unrecognized `cache_dir` option in `pyproject.toml`; it did not affect the run. Ruff and the commit hooks passed.

## Dependencies

The issue brief names #80’s strategy input/decision/experiment contracts as the start prerequisite; those contracts were inspected for this work. The brief separately requires final acceptance against the integrated revision established by #81. That integrated revision and independent lead review are outside this worker branch and remain acceptance dependencies.

This result verifies synthetic stage behavior only. It does not establish production-data coverage, strategy profitability, a full replay, deployment, or broker readiness.
