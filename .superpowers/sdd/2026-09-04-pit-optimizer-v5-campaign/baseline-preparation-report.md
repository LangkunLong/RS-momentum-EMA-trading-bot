# Baseline preparation wiring report

Implemented a host-only `prepare-baseline-inputs` command and composition function. It accepts all ten existing `BaselineCaptureInputsV5` references as explicit relative-path/SHA-256 pairs, authenticates the complete graph through the existing exact-path baseline authenticator before publication, persists the descriptor create-only, reauthenticates the published graph, and returns its reference plus a complete `capture-baseline` argv template.

The preparation path performs no evaluation, Docker operation, provider call, artifact-wide scan, or confirmation/qualification panel access. It uses the existing canonical serializer and artifact repository boundaries. The evaluator closure was not changed.

## Remaining real upstream inputs

- Final V3 PIT bundle and prices provenance, reachable through a real discovery panel plan.
- Six real mechanics, quick, and discovery panel artifacts reachable through that plan.
- Persisted evaluator contract, execution profile, sandbox profile, evaluator source map, identity-transition contract, and resource capabilities.
- Persisted four-file source bundle, baseline policy revision, and policy scope, mutually bound to the evaluator and a clean source commit.
- For capture only: a clean checkout at that commit, disjoint scratch root, locally available pinned evaluator image, and explicit authenticated Git and Docker executables.

The current production source contains builders for execution and sandbox profiles and can authenticate a clean policy digest snapshot. It does not contain sufficient builders to create the evaluator contract, resource authority, source bundle, policy revision, or baseline policy scope from the panel plan and checkout without additional upstream authority. Preparation therefore requires those exact references rather than manufacturing identities or accepting redundant unverified hashes.

## Verification

- In-memory `compile()` of `core/pit_optimizer_v5/cli.py` and `core/pit_optimizer_v5/baseline_preparation.py`.
- `python -B -m ruff check` on both owned Python files.
- `python -B -m ruff format --check` on both owned Python files.
- Real `python -B -m core.pit_optimizer_v5.cli prepare-baseline-inputs --help` parser/entry-point invocation.
- `git diff --check` and scoped status/diff review.

No tests were read, created, modified, or run. No real preparation was attempted because the final authenticated upstream artifacts remain external inputs owned by the controller.
