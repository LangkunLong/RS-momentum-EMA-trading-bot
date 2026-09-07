# Baseline preparation fix round 1

Resolved both independent-review P1 findings.

- Preparation now accepts real source/scratch roots and Git/Docker executable paths, hashes the actual executable bytes, optionally verifies supplied digests, and emits a fully populated capture argv plus correctly literal-quoted PowerShell command.
- `--compose-source` keeps the clean-source inspection pins live while reading and canonicalizing the four policy files, derives `SourceBundleV5`, `PolicyRevisionIdentityV5`, `PolicyScopeDescriptorV5`, and `EvaluatorContractV5`, pre-authenticates all pending canonical bytes with the complete baseline graph, publishes them create-only, and revalidates source and graph identities.
- Explicit ten-reference preparation remains available.
- No evaluator closure, runtime image source, controller ledger, or operator script changed.

## Remaining genuine inputs

- A final authenticated discovery panel plan whose graph reaches the final V3 PIT bundle, prices provenance, and six development panels.
- Existing execution profile, sandbox profile, evaluator source map, identity-transition contract, resource authority, and canonical immutable-constraints configuration.
- Existing canonical source and scratch directories plus actual Git and Docker executables. Capture additionally requires the already available pinned evaluator image.

## Verification

- In-memory compilation and import of both owned Python modules.
- Ruff lint and format checks on both owned Python modules.
- Real `prepare-baseline-inputs --help` invocation showing both composition and executable-authority arguments.
- Scoped diff and `git diff --check` review.

No tests were read, created, modified, or run. No source capture, Docker action, evaluator execution, provider call, or network call was performed.
