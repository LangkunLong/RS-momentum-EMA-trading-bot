# Development execution scope wiring report

Implemented the first bounded source increment from `development-scope-brief.md` at base `cbd0d8dcec5a9830087b233cc4cbb87fd58dc07d`.

## Result

- Added validated `pit_data_scope` authority to `CampaignManifestV5` and `BaselineParentAuthorityV5`, with `production` as the compatibility default and `development_sp500_v2` as the only alternative.
- Development manifests reject provider capabilities and therefore require `provider=None`.
- Bound manifest scope to baseline-parent authority during manifest construction, authentication, CLI authority composition, feedback-round input construction, and confirmation dependency authentication.
- Propagated manifest scope into the existing `PanelExecutionRequestV5`, preserving its output schema and its development prohibition on baseline capture and held-out evaluation.
- Added `--pit-data-scope` to manifest construction and included the selected value in the manifest CLI projection.
- Production services, production round factories/compositions, and the direct production composition helper explicitly reject development manifests or parents.
- Fixture composition, invocation, and history verification explicitly require production scope, so development execution cannot enter through fixture provenance.

## Changed files

- `core/pit_optimizer_v5/contracts.py`
- `core/pit_optimizer_v5/search.py`
- `core/pit_optimizer_v5/sandbox.py`
- `core/pit_optimizer_v5/manifest.py`
- `core/pit_optimizer_v5/runtime.py`
- `core/pit_optimizer_v5/cli.py`
- `core/pit_optimizer_v5/baseline.py`
- `core/pit_optimizer_v5/selection.py`
- `core/pit_optimizer_v5/panels.py`
- `core/pit_optimizer_v5/fixture_runtime.py`
- `.superpowers/sdd/2026-09-04-pit-optimizer-v5-campaign/development-scope-report.md`

The pre-existing controller-owned `progress.md` modification was not touched or staged.

## Validation

- `git diff --check`: passed (only existing line-ending notices).
- In-memory Python compilation of all ten changed Python modules: `in-memory compile: 10 files OK`.
- Ruff over all ten changed Python modules: `All checks passed!`.
- `python -B -m core.pit_optimizer_v5.cli build-manifest --help`: passed and displayed `--pit-data-scope {production,development_sp500_v2}`.
- Narrow source diff and constructor/call-site review completed. No tests or synthetic probes were read, created, modified, or run. No Docker, network, or provider operation was performed.

## Identity migration

`pit_data_scope` participates in canonical dataclass serialization, so newly decoded legacy manifests and baseline parents receive the production default but their newly computed canonical identities include that explicit field. Any pre-existing reference whose digest was computed before this field existed must retain its original bytes/reference rather than be silently re-emitted under the old digest. There is no prior real production campaign to migrate.

## Remaining work

This increment intentionally stops before development composition. The next increments still need controller-response provenance and development composition, explicit semantic-skip persistence/replay semantics, four real development parent discovery evaluations, and the two-round real feedback-loop run. Production baseline capture, qualification, and full replay remain unchanged and unavailable to development scope.
