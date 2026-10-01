# Issue #72: financial lineage bridge

## Local disposition

The bridge, SEC exporter integration, schema-V3 builder checks, and follow-up corrections are committed locally on `codex/issue-72-financial-lineage-bridge`. The work has not been merged or published. The latest date-window correction is documented in [the window-binding receipt](issue-72-financial-lineage-bridge-window-binding-receipt.json). [The earlier follow-up receipt](issue-72-financial-lineage-bridge-followup-receipt.json) and [the original receipt](issue-72-financial-lineage-bridge-receipt.json) remain unchanged.

## Data flow

`python -m core.financial_lineage_bridge` reads the exact schema-V3 membership CSV, its canonical provenance, and the prices provenance/identity contract. It retains four artifacts: ticker membership for the SEC membership window, a lineage-to-ticker projection ledger, an identity extraction-history CSV, and canonical bridge provenance that hashes and relatively references those inputs.

Ticker membership represents index membership only. Events before the SEC membership start are resolved to the actual active state at the window boundary; any seed event is recorded as `membership_window_seed` and cites its earlier V3 source events. Separately, the identity extraction-history CSV carries authenticated ticker/segment date windows so SEC can look up issuer filings before index membership. Segmented and ordinary identities are both retained in mixed universes. That sidecar does not add historical membership and does not rewrite or shift financial values or public dates. Repeated ticker episodes remain separate windows.

`fetch_sec_pit_fundamentals.py --membership-lineage-projection-provenance <projection.json>` verifies that its membership input is the retained projection, consumes the referenced extraction-history bytes in the existing `build_security_master` and `extract_fundamentals` flow, and publishes the resolved history plus a `financial_lineage_bridge_v1` object in `fundamentals_provenance.json`. The security-master CSV continues to contain actual membership episodes only.

The schema-V3 builder resolves the retained references, rehashes the actual membership, security-master, identity-history, fundamentals, and audit files it consumes, and independently recomputes the membership projection and identity history from the destination V3 membership and identity contract. It checks that security-master membership episodes account for the exact projected intervals and that audit rows pair exactly with the financial rows. The production-source guard remains fail-closed. Synthetic fundamentals marked `synthetic fixture` bypass the SEC bridge checks only when the existing explicit `--allow-nonproduction-fixture` flag is present, and their provenance must directly bind the destination schema-V3 membership hash. SEC-source provenance always requires the bridge.

The window contract now binds projection extraction start/end to the referenced, hash-bound prices provenance and the SEC export/bridge declarations. The projection membership start must equal the exporter and bridge starts; its membership end must equal the prices and exporter cutoff. Each date must be a canonical ISO date, and each window must be ordered. Extraction and membership starts remain independent. Builder reconstruction uses the authenticated prices bounds and exporter membership start, so rehashing a shortened projection history cannot change its declared meaning.

## Acceptance evidence

The local end-to-end fixture uses deterministic synthetic ZIP archives and the actual SEC submission parser, security-master builder, companyfacts extractor, publisher, and schema-V3 builder. The authenticated V3 fixture begins membership on 2021-01-04 after the 2020-01-03 OLD-to-NEW rename. The same CIK's complete filing history remains available under both aliases, with its original public dates and values:

- OLD: basic EPS `0.75` on `2020-01-02` and `1.25` on `2020-01-06`.
- NEW: basic EPS `0.75` on `2020-01-02` and `1.25` on `2020-01-06`.

The builder SQLite assertions preserve those values and dates. A mixed-universe regression runs validated FISV→FI→FISV segment history alongside an ordinary `ORD` identity through the security-master builder and fundamentals extractor. A second extraction regression confirms that the later FISV episode retains filings from the intervening FI period. A separate reused-ticker case confirms that multiple CIKs remain separated by their issuer windows.

Focused verification:

```text
python -m pytest tests/test_financial_lineage_bridge.py tests/test_sec_pit_fundamentals.py tests/test_normalize_pit_universe_membership.py tests/test_pit_identity_segments.py tests/test_verify_pit_bundle.py -q -p no:cacheprovider -o addopts=''
71 passed
python -m ruff check build_pit_bundle.py core/financial_lineage_bridge.py core/sec_pit_fundamentals.py fetch_sec_pit_fundamentals.py tests/test_financial_lineage_bridge.py
All checks passed
python -m compileall -q build_pit_bundle.py core/financial_lineage_bridge.py core/sec_pit_fundamentals.py fetch_sec_pit_fundamentals.py
exit 0
```

Negative controls cover foreign/relabelled V3 membership and exporter provenance, tampered fundamental values/public dates, audit rows, security-master rows, and projection ledger. The fixture-path regression rejects a zero membership hash without the SEC bridge and accepts the exact destination hash with explicit fixture opt-in. Pytest emits one pre-existing warning because `pyproject.toml` configures `cache_dir`, which the installed pytest does not recognize.

The later P2 window-binding regression set uses the same coherent synthetic builder fixture. It rejects a shortened extraction end with regenerated history and updated projection/export hashes, a changed extraction start, projection and exporter bounds changed together away from authenticated prices, an event-free membership-start relabel whose ticker/ledger bytes stay identical, and a membership end shortened across an event-free tail. It also covers malformed, missing, noncanonical, and inverted dates. The positive builder control still preserves the 2020 extraction start, 2021 membership start, four alias rows, their original values, and their public dates. The principal counterexample and its original result are preserved under `.artifacts/coordination/principal-reviews/issue72-32a529-evidence/`; the new verification record is under `.artifacts/coordination/principal-reviews/issue72-window-binding-correction-32a529/`.

The default project-wide `python -m pytest` run was started but stopped at 9% on Lead A's instruction after early failure markers appeared, including in `tests/test_agent_loop.py`. It produced no completed summary or failure count and is informational only; the focused corrected-source suite above is the acceptance run. The captured partial status is in `.artifacts/coordination/issue72-followup-full-suite-partial.txt`.

## Input and dependency status

| Status | #72 disposition |
| --- | --- |
| Implementation | Local bridge, SEC extractor interface, provenance validation, mixed-lineage and alias-history corrections, and regressions are complete. |
| Required inputs | Tests use synthetic V3 inputs and deterministic synthetic SEC archives only. No production membership or SEC source archives were acquired for this task. |
| Acceptance evidence | Corrected focused synthetic and adjacent regressions pass; real production input acceptance is not claimed. |
| Dependencies | Existing V3 identity/segment contracts are consumed. The #68 production admission guard is unchanged and continues to reject unapproved production membership evidence. |

SEC archive digests in exporter provenance are declarations produced by the SEC exporter after its own archive verification. The lineage bridge does not claim to independently rehash or revalidate archive ZIP source bytes in the bundle builder.

## Review handoff

The earlier outbound completion message to Lead A was rejected by desktop automatic review because the destination was treated as unverified and the message was judged to disclose internal implementation details. Lead A independently retrieved the original package and sent the three corrections through trusted task delegation. This local follow-up documents and commits those corrections; no second cross-thread message was sent.
