# Baseline preparation wiring report

Implemented explicit and automatic source-composition modes. Automatic mode pins a clean checkout and derives the source bundle, policy revision, policy scope, and evaluator contract from actual source bytes and authenticated configuration. Both modes authenticate the complete graph before create-only publication and return a concrete capture argv plus literal-quoted PowerShell command populated with hashed Git and Docker executable identities.

The preparation path performs no evaluation, Docker operation, provider call, artifact-wide scan, or confirmation/qualification panel access. It uses the existing canonical serializer and artifact repository boundaries. The evaluator closure was not changed.

## Remaining real upstream inputs

- Final V3 PIT bundle and prices provenance, reachable through a real discovery panel plan.
- Six real mechanics, quick, and discovery panel artifacts reachable through that plan.
- Persisted execution profile, sandbox profile, evaluator source map, identity-transition contract, resource capabilities, and immutable-constraints configuration.
- For capture only: a clean checkout at that commit, disjoint scratch root, locally available pinned evaluator image, and explicit authenticated Git and Docker executables.

Preparation now derives the source bundle, policy revision, policy scope, and evaluator contract while preserving the real upstream data/panel, resource, runtime-source, transition, sandbox, execution, and immutable-constraints dependencies.

## Verification

- In-memory `compile()` of `core/pit_optimizer_v5/cli.py` and `core/pit_optimizer_v5/baseline_preparation.py`.
- `python -B -m ruff check` on both owned Python files.
- `python -B -m ruff format --check` on both owned Python files.
- Real `python -B -m core.pit_optimizer_v5.cli prepare-baseline-inputs --help` parser/entry-point invocation.
- `git diff --check` and scoped status/diff review.

No tests were read, created, modified, or run. No real preparation was attempted because the final authenticated upstream artifacts remain external inputs owned by the controller.
