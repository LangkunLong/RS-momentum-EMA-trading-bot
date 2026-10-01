# Issue #72: financial lineage bridge

## Local disposition

The bridge, SEC exporter integration, schema-V3 builder checks, focused evidence, and this receipt are implemented on `codex/issue-72-financial-lineage-bridge`. The work has not been merged or published. Independent final review is still pending.

## Data flow

`python -m core.financial_lineage_bridge` reads the exact schema-V3 membership CSV, its canonical provenance, and the prices provenance/identity contract. It retains four artifacts: ticker membership for the SEC membership window, a lineage-to-ticker projection ledger, an identity extraction-history CSV, and canonical bridge provenance that hashes and relatively references those inputs.

Ticker membership represents index membership only. Events before the SEC membership start are resolved to the actual active state at the window boundary; any seed event is recorded as `membership_window_seed` and cites its earlier V3 source events. Separately, the identity extraction-history CSV carries authenticated ticker/segment date windows so SEC can look up issuer filings before index membership. That sidecar does not add historical membership and does not rewrite or shift financial values or public dates. Repeated ticker episodes remain separate windows.

`fetch_sec_pit_fundamentals.py --membership-lineage-projection-provenance <projection.json>` verifies that its membership input is the retained projection, consumes the referenced extraction-history bytes in the existing `build_security_master` and `extract_fundamentals` flow, and publishes the resolved history plus a `financial_lineage_bridge_v1` object in `fundamentals_provenance.json`. The security-master CSV continues to contain actual membership episodes only.

The schema-V3 builder resolves the retained references, rehashes the actual membership, security-master, identity-history, fundamentals, and audit files it consumes, and independently recomputes the membership projection and identity history from the destination V3 membership and identity contract. It checks that security-master membership episodes account for the exact projected intervals and that audit rows pair exactly with the financial rows. The production-source guard remains fail-closed. Synthetic fundamentals marked `synthetic fixture` bypass the SEC bridge checks only when the existing explicit `--allow-nonproduction-fixture` flag is present; SEC-source provenance always requires the bridge.

## Acceptance evidence

The local end-to-end fixture uses deterministic synthetic ZIP archives and the actual SEC submission parser, security-master builder, companyfacts extractor, publisher, and schema-V3 builder. The authenticated V3 fixture begins membership on 2021-01-04 after the 2020-01-03 OLD-to-NEW rename. Extracted facts remain:

- OLD: basic EPS `0.75`, public date `2020-01-02`.
- NEW: basic EPS `1.25`, public date `2020-01-06`.

The builder SQLite assertions preserve those values and dates. A separate test loads the validated segmented FISV→FI→FISV contract and verifies windows `FISV 2020-01-01–2023-06-06`, `FI 2023-06-07–2025-11-10`, and `FISV 2025-11-11–2025-12-31`.

Focused verification:

```text
python -m pytest tests/test_financial_lineage_bridge.py tests/test_sec_pit_fundamentals.py tests/test_normalize_pit_universe_membership.py tests/test_pit_identity_segments.py tests/test_verify_pit_bundle.py -q -p no:cacheprovider -o addopts=''
68 passed
python -m ruff check build_pit_bundle.py core/financial_lineage_bridge.py core/sec_pit_fundamentals.py fetch_sec_pit_fundamentals.py tests/test_financial_lineage_bridge.py
All checks passed
```

Negative controls cover foreign/relabelled V3 membership and exporter provenance, tampered fundamental values/public dates, audit rows, security-master rows, and projection ledger, plus the required fixture opt-in. The `pytest.ini` currently emits one pre-existing warning because the installed pytest does not recognize `cache_dir`.

## Input and dependency status

| Status | #72 disposition |
| --- | --- |
| Implementation | Local bridge, SEC extractor interface, provenance validation, and tests are complete. |
| Required inputs | Tests use synthetic V3 inputs and deterministic synthetic SEC archives only. No production membership or SEC source archives were acquired for this task. |
| Acceptance evidence | Focused synthetic and adjacent regressions pass; real production input acceptance is not claimed. |
| Dependencies | Existing V3 identity/segment contracts are consumed. The #68 production admission guard is unchanged and continues to reject unapproved production membership evidence. |

SEC archive digests in exporter provenance are declarations produced by the SEC exporter after its own archive verification. The lineage bridge does not claim to independently rehash or revalidate archive ZIP source bytes in the bundle builder.

## Review handoff

The required Lead A completion message was rejected by the desktop automatic review because the destination thread was treated as unverified and the message was judged to disclose internal implementation details. No alternate message route was used. The commit and test details are available in the task result; delivery to Lead A requires the user's explicit approval through a trusted message.
