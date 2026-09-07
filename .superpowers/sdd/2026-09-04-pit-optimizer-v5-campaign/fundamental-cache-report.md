# Fundamental provenance cache implementation report

## Change

- Extended `PITDataBundle.fundamentals_provider` with the existing `include_provenance=False` default, preserving current callers.
- Separated chronological provider state by `(ticker, include_provenance)` so provenance-bearing and provenance-free snapshots cannot share DataFrames or metadata.
- Reused the existing exact public-date boundary advancement. Snapshot reconstruction still occurs only after one or more complete public-date boundaries become visible; earlier out-of-order requests still use `fundamentals_as_of` with the requested provenance mode.
- Returned deep DataFrame copies, copied `PIT_PUBLIC_DATES_ATTR` mappings, and copied company-info dictionaries for provenance-bearing cached results. Caller mutation therefore cannot alter later cached feature inputs.
- Routed `build_entry_features_v3` through the provenance-aware provider. Schema-V3 and explicit schema-V2-development validation and feature calculations are unchanged.
- Existing per-bundle cache cleanup in `close()` clears both cache modes.

## Local verification

- `python -m compileall -q core/pit_data.py core/pit_feature_snapshot.py`: passed.
- `python -m ruff check core/pit_data.py core/pit_feature_snapshot.py`: passed.
- `git diff --check -- core/pit_data.py core/pit_feature_snapshot.py`: passed.
- Direct source/diff inspection confirmed exact public-date comparison, atomic same-date boundary application, requested-mode authoritative fallback for out-of-order dates, separate mode keys, isolated provenance returns, unchanged cutoff validation, and the V3 call-site switch.
- `python -m ruff format --check core/pit_data.py core/pit_feature_snapshot.py`: reports both base files would be reformatted. Applying the workspace Ruff formatter caused broad unrelated churn (377 lines in `pit_data.py` and 57 lines in `pit_feature_snapshot.py`), so that mechanical rewrite was reverted and is not part of this scoped change. The narrow diff passes Ruff lint and `git diff --check`.
- Per instruction, no tests, synthetic market/probe calls, Docker commands, network calls, external-provider calls, or real panel executions were performed.

## Remaining controller verification

Rebuild and pin the evaluator image from this commit, run the unchanged actual 32-lineage/61-session S&P candidate against the same authenticated bundle/panel, and compare every real report field with the prior completed candidate report. Record wall-clock and profiling evidence that the 1,940 V3 entry calls reuse provenance snapshots between exact public-date changes, and confirm the evaluation completes successfully. This real report parity and end-to-end timing check is intentionally left to the controller.
