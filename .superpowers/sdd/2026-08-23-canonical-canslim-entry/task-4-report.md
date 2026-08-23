# Task 4 — fundamental corrections (Steps 1–3)

Status: production fixes and narrow probes complete; Task 4 is **not complete**.

- `core/canslim/c_current_earnings.py` and
  `core/canslim/a_annual_earnings.py` now reject `NaN`, `+inf`, and `-inf`
  operands after float conversion. Existing EPS-first/Net-Income fallback and
  row-selection policy are unchanged.
- `core/sec_pit_fundamentals.py` adds the reviewed baseline mapping
  `XOM -> 0000034088`; its existing reviewed-baseline override therefore
  supersedes the current-ticker candidate before security-master emission.
- The existing PIT engine finite normalization defense was retained. The
  existing `pit_baseline.py` stale-holding comment remains factually accurate,
  so no behavior or comment changed there.

## Read-only real-data evidence

- Immutable bundle
  `five-year-public-pit/.artifacts/pit-baseline/pit_baseline.sqlite3` matched
  SHA-256 `8ca8242dd67db30d456a2b1861f7e7399f8ca418079738ab150d4e44865763c5`
  before and after an immutable SQLite URI query. CAH at `2021-05-13` returned
  `(c_score=0.0, c_growth=None, a_score=0.1,
  a_growth=-3.771428571428572, roe=-2.064804469273743)`: every available C/A
  result was finite and the unavailable current growth was `None`.
- Pinned SEC archives matched their recorded hashes before and after the
  read-only probe: submissions
  `928d67221c6e6183bc343e7234c1391448c15cd1dd644d36b425db2f99ba4350` and
  companyfacts
  `d7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca`.
  The actual current-ticker XOM issuer `0002115436` advertised `XOM`, but the
  resolver produced `0000034088:reviewed_baseline_cik`; the narrow extractor
  emitted 209 normalized XOM rows.

## Checks

- `py_compile` and direct import/finite-growth/XOM-mapping probe passed.
- Ruff passed for the three touched production files.
- `git diff --check` passed.
- No unit, broad, network, or normalized-regeneration run was started.

## Concern — Step 4 pending active replay completion

Do not begin immutable SEC-output/PIT-bundle regeneration until the active
multi-hour replay has completed and shared-machine capacity is available.
