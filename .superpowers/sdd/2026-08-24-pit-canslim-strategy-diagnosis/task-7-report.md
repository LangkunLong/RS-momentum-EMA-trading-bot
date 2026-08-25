# Task 7 — Publish and Run the First Deterministic Diagnosis Deliverable

## Delivered implementation

- Added `core/pit_diagnosis/publication.py` with atomic staging publication,
  canonical JSON/CSV output, manifest-last hash binding, aggregate-only artifacts,
  and a fail-closed verifier.
- Added `pit_diagnosis.py` with offline `build-facts`, `run`, `run-experiment`,
  and `verify-result` commands.  Paths are absolute, SHA-256 arguments are
  lowercase, and the default run selects discovery plus validation only.
- Added the human/research-identifier-gated locked-evaluation boundary.  It uses
  a selection-bound checkpoint namespace and a separate output root.
- Corrected D4 holdings validation to reject non-finite `Cash` and
  `Total_Equity` before performance evidence can be emitted.
- Corrected fact-cache handling of SQLite NULL fundamentals: pandas represents
  those as NaN, and they are now recorded as unavailable rather than treated as
  non-finite source facts.  `_number` continues to reject genuine NaN/Infinity.

## Commits

- `3242386 feat: publish offline PIT diagnosis runs`
- `6861330 feat: gate locked PIT diagnosis publication`
- `5625505 fix: preserve missing PIT fundamentals as unavailable`

## Verification performed

```text
python -m pytest -p no:cacheprovider --no-cov -q \
  tests/test_pit_diagnosis_experiments.py tests/test_pit_diagnosis_cli.py
26 passed, 2 warnings

python -m ruff check core/pit_diagnosis/publication.py pit_diagnosis.py \
  tests/test_pit_diagnosis_cli.py tests/test_pit_diagnosis_experiments.py \
  core/pit_diagnosis/experiments.py
All checks passed!

python -m compileall -q core/pit_diagnosis pit_diagnosis.py
exit 0

python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_fact_cache.py
10 passed, 1 warning

python -m ruff check core/pit_diagnosis/fact_cache.py tests/test_pit_diagnosis_fact_cache.py
All checks passed!

git diff --check
exit 0
```

The only warnings were the repository's existing pytest configuration warning
for `cache_dir` and a third-party `websockets.legacy` deprecation warning.

## Real-data CLI attempt

The approved real bundle, baseline, rulebook, and catalog paths were present.
The first exact `build-facts --resume` invocation reached the cache build but
failed closed with:

```text
PIT diagnosis failed closed: fact cache number is non-finite
```

This was traced to database NULL fundamental cells becoming pandas NaN during
frame materialization; the narrow unavailable-fundamental fix above was added
and tested.  The resumed build was restarted against:

`C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts\pit-diagnosis\facts\diagnosis_facts.sqlite3`

The worker was deliberately stopped at parent handoff before finalization.  No
final fact-cache SHA-256, diagnosis run, manifest SHA-256, or performance
metrics exist yet.  Consequently `run` and `verify-result` were not executed.

## Artifact state and concerns

- Generated files are under `.artifacts/pit-diagnosis/facts/` and are not
  committed.  The partial cache is resumable with the exact prescribed
  `build-facts --resume` command.
- The cache construction is memory-intensive during its initial fundamental
  state materialization.  It must be allowed to finish before running the
  discovery/validation diagnosis.
- No agent event, raw provider data, transaction rows, price rows, secrets, or
  ex-post leader labels were published.  The first completed run still needs
  its manifest hash and causal metrics recorded here.

## Review fix round 1

- Hardened the verifier with an exact manifest schema, digest/type checks,
  canonical rulebook/catalog reconstruction, exact CSV/JSON schemas,
  non-finite/raw-field rejection, fact-cache reopening, and report screening.
- Publication and the CLI final guard now reconcile source, bundle, rulebook,
  catalog, fact-cache, and baseline identities.
- Preserved SQLite NULL at the PIT-data frame boundary; genuine supplied NaN
  remains fail-closed.
- Added regression coverage for rehashed malformed/non-finite/raw CSV/report
  and SQLite artifacts, ex-post leader-label injection, manifest completeness,
  SQL NULL versus NaN, and locked checkpoint isolation.

```text
python -m pytest -p no:cacheprovider --no-cov -q \
  tests/test_pit_diagnosis_cli.py tests/test_pit_diagnosis_fact_cache.py \
  tests/test_pit_diagnosis_experiments.py
43 passed, 2 warnings
```

## Review fix round 2

- The publication verifier now validates every CSV and JSON row against its
  exact schema, finite numeric domains, count relationships, closed enums, and
  forbidden raw/provider/transaction/leader terms.  Missing trailing CSV cells,
  numeric strings that overflow to infinity, and incorrect JSON value types are
  rejected after a manifest rehash.
- Reopened fact caches now require SQLite `integrity_check=ok`, exact metadata,
  identity and partition schema, exact column order/type affinities, finite
  numeric row values, and no forbidden labels.  The cache bundle/rulebook
  identities are reconciled to the publication manifest.
- SQLite NULL masking is now explicitly limited to original `None` values
  before pandas construction.  A genuine NaN entering the real
  `_statement_frame` path remains NaN and is rejected by `_number`.
- Manifest `result_count` and promotion count must be non-boolean,
  non-negative integers; fidelity is a closed enum and promotion candidates
  must be zero.

Focused verification (all source-only; generated pytest artifacts remain
uncommitted):

```text
python -m pytest tests/test_pit_diagnosis_fact_cache.py -q
11 passed, 1 warning

python -m pytest tests/test_pit_diagnosis_cli.py -k "publication_is_complete or incomplete_or_nonfinite" -q
4 passed, 14 deselected, 2 warnings

python -m pytest tests/test_pit_diagnosis_cli.py -k manifest_type -q
5 passed, 13 deselected, 2 warnings

python -m pytest tests/test_pit_diagnosis_cli.py -k "entry_funnel and raw_or_malformed" -q
1 passed, 17 deselected, 2 warnings

python -m ruff check core/pit_diagnosis/publication.py core/pit_data.py tests/test_pit_diagnosis_cli.py tests/test_pit_diagnosis_fact_cache.py
All checks passed.

python -m compileall -q core/pit_diagnosis/publication.py core/pit_data.py
exit 0

git diff --check
exit 0
```

One grouped retry encountered a transient Windows `os.replace` access denial
inside the repository-owned pytest artifact directory; the identical isolated
publication test immediately passed.  The cache/run/manifest deliverable is
still pending the previously noted real cache completion.

Final rerun after the report update:

```text
python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_fact_cache.py
11 passed, 1 warning

python -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_diagnosis_cli.py
18 passed, 2 warnings

python -m ruff check core/pit_diagnosis/publication.py core/pit_data.py tests/test_pit_diagnosis_cli.py tests/test_pit_diagnosis_fact_cache.py
All checks passed!

python -m compileall -q core/pit_diagnosis/publication.py core/pit_data.py
exit 0

git diff --check
exit 0
```
