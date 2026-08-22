# Task 2 implementation report — SEC PIT fundamentals

Status: implementation and local end-to-end smoke complete; live SEC extraction
blocked on an operator-approved public contact declaration. No production files
have been committed and no SEC archive bytes were downloaded.

## Implemented files

- `core/sec_pit_fundamentals.py`
  - bounded, no-extract ZIP validation (regular non-link input, entry/expanded-byte/
    compression-ratio caps, duplicate/encrypted/traversal rejection);
  - strict recurrent membership intervals and membership-union name restriction;
  - exact SEC ticker/current-name/former-name resolution plus hash-bound reviewed
    identity chains;
  - explicit issuer boundaries for VIAC/PARA (`0000813828`) versus reset PSKY
    (`0002041610`), FISV/FI (`0000798354`), DOC (`0000765880`), and CTRA
    (`0000858470`);
  - recent and archive-present referenced historical submission-fragment joins;
  - canonical accession validation and form/filed metadata agreement;
  - 10-Q/10-Q-A discrete-quarter safeguards (70–115 days), 10-K/10-K-A annual
    safeguards (300–430 days), and instantaneous balance extraction;
  - fixed revenue priority:
    `RevenueFromContractWithCustomerExcludingAssessedTax`, then `Revenues`;
  - `EntityCommonStockSharesOutstanding` from the SEC `dei` namespace;
  - public date as the first supplied SPY day strictly after acceptance calendar
    date, with separately counted filed-date fallback and cutoff omissions;
  - one coherent row per visible period/public-date snapshot, deterministic
    same-day collision resolution, carried-forward unchanged amendment fields,
    and a lossless accession/form/filed/fy/fp audit record.
- `fetch_sec_pit_fundamentals.py`
  - exact official endpoints only:
    - `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
    - `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`
  - required caller-provided `--sec-user-agent` and `--max-archive-bytes`;
  - 30-second streaming request timeout and 0.2-second minimum between request
    starts (at most five requests/second);
  - same-host/path redirect validation, exact byte length/SHA-256/ZIP metadata,
    complete-pair reuse, and rollback of first-run archives if normalization fails;
  - atomic, no-overwrite publication of:
    `security_master.csv`, `security_master_exclusions.csv`, `fundamentals.csv`,
    `fundamentals_audit.csv`, `fundamentals_provenance.json`, and
    `fundamentals_coverage.json`;
  - exact 14-column `build_pit_bundle.py` fundamentals contract, with all three
    institutional fields blank.

The reviewed price-identity manifest is an additional CLI input because
`security_names.csv` intentionally propagates display names across ticker
history and cannot by itself distinguish the PARA/PSKY issuer reset. The exact
manifest digest is bound into fundamentals provenance.

## Ephemeral end-to-end smoke

An ignored temporary smoke driver generated two one-member SEC-shaped ZIPs and
all CSV inputs in an OS temporary directory, pre-bound them through the archive
provenance contract, invoked the real CLI `main(...)`, reloaded every output,
and was deleted afterward. The first sandboxed attempt could not create a
Windows Temp child directory. The identical no-network command was rerun with
approved OS-temp access and passed.

Exact result:

- membership issuer: AAPL / CIK `0000320193`;
- acceptance joins: 2 unique accessions, 4 emitted rows;
- output rows: 1 quarterly, 1 annual, 2 balance;
- public dates: `2021-04-21` and `2022-02-01`, each the first supplied SPY day
  strictly after acceptance;
- filed-date fallbacks: 0;
- institutional fields: blank in every row;
- all six normalized outputs present;
- exact `FUNDAMENTAL_COLUMNS` header reloaded successfully;
- smoke `fundamentals.csv` SHA-256:
  `43bb3c070e010ab467828b37cdfa52943deb854f0d363212ac339bbdbcd0e425`;
- provenance fundamental digest matched the published bytes;
- both source archive hashes/lengths/ZIP metadata revalidated;
- no `.sec-pit-*` staging directory remained.

Local implementation checks:

```text
python fetch_sec_pit_fundamentals.py --help                         PASS
python -m compileall -q core/sec_pit_fundamentals.py \
  fetch_sec_pit_fundamentals.py                                    PASS
python -m ruff check core/sec_pit_fundamentals.py \
  fetch_sec_pit_fundamentals.py                                    PASS
git diff --check                                                    PASS
```

Broad/unit tests remain deferred by the plan until the functional baseline is
complete.

## Live SEC blocker

Both required official endpoints were probed with the approved project-URL-only
declaration:

```text
RS-momentum-EMA-trading-bot/1.0
(https://github.com/LangkunLong/RS-momentum-EMA-trading-bot)
```

Both streaming GETs returned HTTP 403 with the SEC page title “Your Request
Originates from an Undeclared Automated Tool” and asked for company-specific
traffic declaration. No archive response bytes were accepted or written.

The implementation does not embed, infer, log, or publish a private git email.
The first functional extraction can start immediately after either:

1. the operator supplies an approved public contact/company User-Agent string;
   or
2. the exact two official SEC archives are supplied locally with their
   digest/length/retrieval provenance.

No live extraction, >=95% resolved-or-closed coverage check, output digest, or
Task 2 commit should be claimed until that blocker is resolved.

## Independent review fix round 1

The first independent review found one Critical and eight Important issues.
All were addressed as one focused round without a network call:

1. The 30-row price identity manifest is now treated as the reviewed exception
   overlay it is. Standalone identity bounds are synthesized from validated
   membership intervals for the other 576 tickers. The production inputs now
   produce exactly 606 identities: 576 standalone plus all 30 explicit
   rename/reuse/reset/historical exceptions. Overlay SHA-256 remains
   `6a9ec69bc0fe05decea1b832cac8e26a611d706cce831d5687fa5424f9544955`.
2. CIK decisions are keyed to each `_MembershipInterval`; ticker reuse cannot
   flatten disjoint membership intervals into one master row. Unprovable reuse
   remains a closed per-interval exclusion.
3. SEC name evidence now normalizes only Unicode, case, and whitespace. It does
   not remove punctuation or legal suffixes and therefore cannot turn an
   inexact display name into purported exact evidence.
4. Every reviewed same-issuer chain must resolve to exactly one CIK. A conflict
   closes the entire chain; an already-resolved member may no longer survive a
   conflicting peer. Successor-reset chains remain separate.
5. SEC redirects are disabled. Any 3xx response is rejected without following
   an unpaced or unvalidated Location.
6. Archive and normalized publication now use same-filesystem hard links as
   atomic create-if-absent operations. Rollback removes a target only while it
   is still the same file identity as owned staging. A digest-bound
   `fundamentals_publication.json` commit marker is linked last and binds all
   six normalized outputs.
7. All four CSV/manifest inputs and both SEC archives are hashed before
   consumption, checked against archive provenance and result-object hashes,
   rehashed after extraction, and rehashed immediately before publication.
8. `fundamentals_audit.csv` now includes `metric_sources` JSON. Every visible
   metric retains its own accession/form/filed/fy/fp/acceptance/basis/concept,
   including inherited metrics merged from a different same-day accession.
9. ZIP validation now enforces a per-member expanded-byte ceiling derived from
   the caller archive cap and bounded at 512 MiB. The exact ceiling is recorded
   and revalidated in archive provenance before any JSON parse.

Focused production-overlay check:

```text
membership union identities: 606
synthesized standalone:       576
explicit reviewed identities: 30
overlay SHA-256:               6a9ec69b...9544955
```

The updated no-network smoke used an empty exception overlay for AAPL, proving
standalone synthesis rather than a one-row full manifest. It also split one
quarterly snapshot across two same-day accessions. The final audit retained EPS
origins at accession `0000320193-21-000009` (18:00Z) and revenue/net-income
origins at accession `0000320193-21-000010` (20:00Z), while still publishing one
coherent quarterly row. It emitted four rows, a complete marker binding six
files, no fallback dates, and no staging residue. The strict fundamentals CSV
digest remained:
`43bb3c070e010ab467828b37cdfa52943deb854f0d363212ac339bbdbcd0e425`.

Post-fix focused checks all pass: CLI help, compileall, Ruff, `git diff
--check`, production 606-symbol overlay expansion, and the updated ephemeral
end-to-end smoke. The ignored smoke driver was removed again.

Re-review found that interval-keyed output was still seeded by one globally
name-narrowed ticker decision when SEC metadata exposed two CIK candidates.
That final gap is closed: a display name is not date-bounded evidence, so every
interval of a multiply claimed ticker now receives the closed
`ambiguous_ticker_reuse` exclusion unless an explicit reviewed same-issuer chain
proves one consistent CIK. A focused two-interval/two-CIK probe now returns zero
resolved intervals and two closed exclusions; compileall, Ruff, and diff checks
remain clean.
