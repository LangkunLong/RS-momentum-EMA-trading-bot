# PIT I/L supplemental artifact

`tools/build_pit_supplemental.py` is an offline boundary between a normalized
data extraction job and the strict PIT CANSLIM fact cache. It does not download
SEC files, identify issuers, infer ownership, assign industry groups, or fill
missing observations. It only validates a supplied normalized export and
seals it into the read-only SQLite contract consumed by
`SQLiteSupplementalPITProvider`.

## Normalized CSV contract

The builder requires two UTF-8 CSV files with exactly these headers and in this
order:

```text
symbol,as_of_date,ownership_percent,holder_count,previous_holder_count,evidence_ids
symbol,as_of_date,group_id,group_rank,group_members,evidence_ids
```

The first header is `institutional.csv`; the second is `industry.csv`.
`symbol` values are uppercase and trimmed. Dates are exact `YYYY-MM-DD`
values and must be no later than `--data-cutoff`. `ownership_percent` is a
fraction in `[0,1]`, not a 0–100 percentage. Counts are canonical
non-negative integers. `evidence_ids` and `group_members` are non-empty JSON
arrays of unique strings. Every industry row must include its own symbol in
`group_members`; duplicate `(symbol, as_of_date)` rows are rejected.

`as_of_date` means the date the observation was publicly available to the
backtest, not merely the period end represented by the observation. An
extractor must therefore use the filing/publication date when a source has
both a period end and a later availability date. Rows after the declared
cutoff, malformed rows, missing evidence IDs, and empty inputs are rejected.

## SEC Form 13F normalization boundary

For an official SEC Form 13F extraction, the normalized institutional file may
be derived from the SEC quarterly [`Form 13F Data Sets`](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets)
(`SUBMISSION`, `COVERPAGE`, and `INFOTABLE`; available from 2020 Q1 onward;
see the [format specification](https://www.sec.gov/files/form_13f.pdf)). The
extraction job—not this builder—
must perform the following explicitly documented transformations:

* use the filing's public filing/acceptance date as `as_of_date`; retain the
  reported quarter end inside the source evidence record, never as a shortcut
  for availability;
* aggregate unique reporting managers and their positions by issuer and
  period to produce `holder_count` and `previous_holder_count`;
* calculate `ownership_percent` only when the issuer-share denominator and its
  point-in-time source are present; otherwise omit the row rather than infer a
  value; and
* put stable accession/table/row identifiers in `evidence_ids`, for example
  `sec13f:<accession>:coverpage` and
  `sec13f:<accession>:infotable:<row-id>`.

The SEC 13F tables alone do not establish a complete industry-group ranking or
an issuer-share denominator for every observation. The industry CSV must carry
its own dated source evidence and source reference; the builder will not derive
industry labels from ticker names, current classifications, or future
membership lists.

For a single quarterly 13F ZIP, the offline normalizer is:

```powershell
python -m tools.normalize_sec13f `
  --13f-zip 2021q1_form13f.zip `
  --cusip-mapping-csv cusip_mapping.csv `
  --shares-csv pit_shares.csv `
  --trading-days-csv spy_trading_days.csv `
  --output institutional.csv
```

The mapping input must be dated and explicit:

```text
cusip,symbol,effective_start,effective_end,evidence_ids
```

The shares input must contain PIT denominators:

```text
symbol,as_of_date,shares_outstanding,evidence_ids
```

The normalizer selects the latest filing/amendment per manager and reporting
period inside that ZIP, maps each filing to the first supplied trading session
strictly afterward, and consolidates all selected managers for a report period
at the latest of those public sessions. This conservative alignment prevents
partial/staggered holdings and prevents an amendment from rewriting an earlier
snapshot. It aggregates common-stock (`SH`) positions by manager and derives
ownership only when a prior PIT denominator exists. Unmapped CUSIPs,
options/principal positions, ambiguous mappings, future denominators, and
missing dates are excluded or rejected fail-closed. Its isolated report-period
output sets `previous_holder_count` to zero; the multi-quarter assembly step
replaces that field with the prior snapshot's holder count before strict
I-gating.

### Multi-quarter assembly

Use an explicit ordered manifest to combine the isolated-quarter outputs. The
manifest is either a UTF-8 CSV with this exact header:

```text
quarter,institutional_csv,source_reference,evidence_ids
```

or a JSON object with `schema_version: 1` and a `quarters` array containing
those same four fields. Paths are relative to the manifest file. `quarter`
must be strictly increasing `YYYYQn`; `source_reference` is a stable archive
or evidence reference, and `evidence_ids` is a JSON array of strings.

```powershell
python -m tools.normalize_sec13f `
  --quarter-manifest quarters.csv `
  --data-cutoff 2025-12-31 `
  --output institutional.csv
```

The assembler is deliberately limited to post-quarter consolidated snapshots:
each listed CSV must contain exactly one distinct `as_of_date`. Staggered
filing-event rows are rejected because the isolated CSV does not retain
manager-level state needed to reconstruct a complete as-of snapshot; use a
manager-level extraction for that event-time analysis. Quarter date ranges must
be strictly chronological, and duplicate `(symbol, as_of_date)` rows are
rejected. For each symbol, `previous_holder_count` is taken from the latest
available earlier quarter snapshot, strictly before the current date, and is
zero only for that symbol's first observation. Existing ownership, holder
count, and row evidence are preserved; manifest evidence and the source
reference are unioned into the row evidence array in canonical order.

Pass stable archive identifiers or URLs with one or more repeated
`--source-reference` options. References are stored in a separate canonical
provenance manifest (not in raw fact rows); its SHA-256 is sealed in the
SQLite `metadata.provenance_sha256` value. Input file SHA-256 values and row
counts are also recorded there. The SQLite artifact itself is hash-pinned by
the digest printed by the builder.

## Offline build

```powershell
python -m tools.build_pit_supplemental `
  --institutional-csv institutional.csv `
  --industry-csv industry.csv `
  --source-kind sec-13f-plus-dated-industry-export `
  --source-reference sec-13f-quarterly-2020q1-through-2025q4 `
  --source-reference industry-export:<immutable-id> `
  --data-cutoff 2025-12-31 `
  --output supplemental.sqlite3 `
  --provenance-output supplemental.provenance.json
```

The command is intentionally offline. After it completes, pass the printed
SQLite `sha256` to `pit_diagnosis.py build-facts --supplemental-input ...
--supplemental-sha256 ...`. The resulting fact-cache identity includes that
artifact hash, so changing source rows or provenance creates a new cache
identity rather than silently reusing prior facts.

## Building the industry CSV from PIT prices

When the classification source contains only dated symbol-to-group observations,
`tools/build_pit_industry.py` derives the remaining fields without consulting a
current profile provider. Its input contract is the exact UTF-8 header:

```text
symbol,as_of_date,group_id,evidence_ids
```

`as_of_date` is the public/available date of the classification observation. It
must be no later than the PIT bundle cutoff and must be an exact completed SPY
price session; no adjacent-session fallback is permitted. Every symbol must be
present in the bundle and active in the bundle's historical membership state on
that date, and every active PIT member must have exactly one classification row
at each snapshot date. Duplicate `(symbol, as_of_date)` rows, unknown symbols,
inactive symbols, incomplete snapshots, empty evidence, and future-dated rows
are rejected.

For every snapshot date, the utility groups the supplied classifications and
computes each group's score as the mean of member ratings from the repository's
causal PIT RS implementation. The input prices passed to each RS calculation
end at the classification session. Groups are ranked by descending score with
a canonical `group_id` tie-break, and one output row is emitted for every input
symbol snapshot. `group_members` is the sorted, dated classified member set
and `evidence_ids` is carried through from the classification source.

```powershell
python -m tools.build_pit_industry `
  --pit-bundle pit.sqlite3 `
  --bundle-sha256 <bundle-sha256> `
  --classification-csv classifications.csv `
  --output industry.csv
```

The resulting `industry.csv` can be supplied directly as the
`--industry-csv` input to `tools.build_pit_supplemental.py`. The classification
export must contain enough historical price for the existing PIT RS calculation
to produce a rating for every classified group member; the ranker fails closed
when a group cannot be ranked causally.

## Offline normalization of an archived Wikipedia revision

`tools/normalize_pit_industry.py` is the offline acquisition boundary for a
pinned S&P 500 Wikipedia revision. It never fetches the network or consults a
current classification map. Supply a local UTF-8 MediaWiki JSON/HTML export,
and a local trading-session map with the exact header:

The Wikipedia table is a community-maintained, GICS-like classification view;
it is not an official S&P Global licensed GICS data feed. Treat the pinned
revision and its evidence ID as an explicit research input, and replace it
with a licensed or issuer-verified source if production classification
fidelity requires that guarantee.

```text
trade_date
```

The compact JSON fixture contract is:

```json
{
  "revid": 123456,
  "timestamp": "2024-01-02T15:00:00Z",
  "rows": [
    {"Symbol": "AAA", "GICS Sub-Industry": "Application Software", "CIK": "0000123456"}
  ]
}
```

An API-shaped object containing exactly one page and revision is also accepted.
HTML must contain exactly one numeric revision ID (a revision meta tag or a
canonical `oldid` URL), one timezone-aware revision timestamp, and a table with
`Symbol` and `GICS Sub-Industry` columns. Symbols and groups are canonicalized
strictly; duplicate symbols, malformed fields, and unknown punctuation aliases
are rejected.

The normalizer assigns `as_of_date` to the first supplied `trade_date` strictly
after the revision timestamp. A same-day session is therefore never used for a
revision published later that day, and a missing later session is an error.
Evidence is emitted as the JSON array `["wikipedia:revid:<id>"]` in the
classification CSV contract. When a PIT membership CSV is supplied, the
revision symbols must exactly equal the active members on the derived date;
missing and extra symbols fail closed.

```powershell
python -m tools.normalize_pit_industry `
  --revision-export wikipedia-revision.json `
  --sessions-csv trading-sessions.csv `
  --membership-csv pit-membership.csv `
  --output classifications.csv
```

The resulting `classifications.csv` is the input to
`tools/build_pit_industry.py`. Keep the immutable revision export, its revid,
and the session/membership inputs alongside the generated artifact so the
classification snapshot can be reproduced and audited.
