# Task 4 implementation report

## Changed files

- `core/canslim/c_current_earnings.py`: preferred C rows are now considered in documented order, selecting the first row whose newest fiscal-year-over-year match has both values available. Invalid growth remains with that selected row and is handled by the existing `_safe_growth` fail-closed behavior.
- `core/canslim/a_annual_earnings.py`: preferred A rows are now considered in documented order, selecting the first row whose newest original annual value and preceding available value are present. Annual evaluation uses the available ordered observations while preserving existing growth and ROE calculations.

No data sources, thresholds, cadence, optimizer/live-engine behavior, or tests were changed.

## Verification

Command:

```text
python -m py_compile core/canslim/c_current_earnings.py core/canslim/a_annual_earnings.py
python -c 'exec("import json\\nfrom core.pit_data import PITDataBundle\\nfrom core.canslim.c_current_earnings import evaluate_c\\nfrom core.canslim.a_annual_earnings import evaluate_a\\nm=json.load(open(\".artifacts/pit-baseline-roe2/pit_baseline.manifest.json\"))\\nsyms=(\"GD\",\"HSY\",\"VRSN\",\"HWM\",\"NVDA\",\"SMCI\")\\nbounds={s:(\"2025-12-31\",\"2025-12-31\") for s in syms}\\nwith PITDataBundle(\".artifacts/pit-baseline-roe2/pit_baseline.sqlite3\", expected_sha256=m[\"bundle_sha256\"]) as b:\\n    for s,d,f in b.iter_fundamental_state_boundaries(bounds):\\n        c=evaluate_c(f[\"quarterly_income\"])[1]\\n        a=evaluate_a(f[\"annual_income\"], balance_sheet=f[\"balance_sheet\"])[1]\\n        print(f\"{s} {d} C={c!r} A={a!r}\")")'
```

Output:

```text
GD 2025-12-31 C=0.13870967741935483 A=0.13394342762063238
HSY 2025-12-31 C=-0.38086627634712894 A=0.19306827257897924
HWM 2025-12-31 C=0.17283950617283939 A=0.53551912568306
NVDA 2025-12-31 C=0.6666666666666666 A=1.4705882352941178
SMCI 2025-12-31 C=-0.6119402985074627 A=-0.125
VRSN 2025-12-31 C=0.057128663686040734 A=-0.03901663405088063
```

The reviewer comparison against the pre-fallback evaluator is:

```text
GD   C None -> 0.1387
HSY  C None -> -0.3809
VRSN C None -> 0.0571, A None -> -0.0390
HWM  unchanged
NVDA unchanged
SMCI unchanged
```

## Concern

The bounded probe uses the available local `pit-baseline-roe2` SQLite bundle at the 2025-12-31 boundary; no full replay or downloads were run.
