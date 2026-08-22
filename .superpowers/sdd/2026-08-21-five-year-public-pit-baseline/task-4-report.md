# Task 4 report — audited PIT bundle construction

Date: 2026-08-22

## Outcome

Task 4 code is implemented and independently reviewable. The production five-year bundle is intentionally **not** built because the real Task 2 SEC fundamentals export remains unavailable pending an operator-approved SEC User-Agent contact. No generated bundle, manifest, or fundamentals fixture is included in the commit.

## Implemented contract

- `build_pit_bundle.py` requires evaluation/warm-up dates and all three provenance JSON files. It binds exact CSV and full-provenance SHA-256 digests, membership revision/source facts, SEC archive/identity digests, and the hash of the complete price-identity request contract into content-free schema-v1 metadata.
- Builder gates require membership price coverage or explicit exclusions, prices confined to membership plus SPY, fundamentals confined to the membership union, all public rows at or before `2025-12-31`, and the exact recomputed `2020-01-02..2025-12-31` SPY calendar/digest. It verifies provenance row/date/member-pair/coverage totals and 495–510 active members on every evaluation-period SPY session.
- Task 2 provenance must bind the exact membership CSV, reviewed security-names file, Task 3B SPY calendar, three SEC source digests, extraction dates, and public-date rule; these are no longer optional.
- Inputs are hashed before and after parsing and again before publication. Bundle and optional manifest are staged beside their targets and published with create-only hard links; a racing target causes identity-safe rollback rather than overwrite.
- The builder requires a manifest-last commit marker. `verify_pit_bundle.py` requires that manifest and all six exact source/provenance files, opens through the immutable read-only `PITDataBundle`, compares the manifest exactly, rehashes every input, and reloads the exact price-identity contract.
- `PITDataBundle._statement_frame` selects the latest public snapshot as a whole row for each period, places metric labels on the index and period-end dates on columns, and drops all-null metric rows.
- The price provenance's full-file digest, identity-map digest, and canonical request-contract digest bind holding transitions. Each boundary is one-to-one and the successor must be admitted on the effective date. Same-issuer FISV/FI transitions are explicit; successor reset PARA/PSKY has no implicit transfer; a skipped or ended identity fails closed.

## Focused offline evidence

An ignored ephemeral smoke used the real normalized membership and price artifacts and a four-row AAPL fundamentals fixture with matching fixture provenance. It did not publish or claim a five-year production bundle.

- Build after review fixes: PASS; digest `a767d57bc9f7c4426bfff5b3ce6d9c5ce2f0f547e34d3bdaa04c4b01238756e4`.
- Real input coverage observed: 711 membership events / 606 membership symbols; 866,025 price rows / 607 priced symbols; evaluation-session membership range 503–506; prices `2020-01-02..2025-12-31`.
- Read-only verification with manifest plus all six exact source files: PASS.
- Amendment/orientation probe: later AAPL quarterly snapshot yielded only `Basic EPS=0.74`; revenue from the earlier snapshot was not fieldwise inherited; index was metric labels and columns were period-end dates.
- Transition probe: `FISV -> FI` on 2023-06-07 and `FI -> FISV` on 2025-11-11 passed. PARA on the 2025-08-07 PSKY reset, FISV after a deliberately skipped transition, and FI before admission all raised fail-closed errors. Synthetic malformed future-admission and many-to-one transition contracts were rejected.
- Repeating the build against the existing target refused overwrite before parsing large inputs.
- `python -B -m py_compile build_pit_bundle.py verify_pit_bundle.py core/pit_data.py`: PASS.
- CLI help for builder and verifier: PASS.
- `python -m ruff check build_pit_bundle.py verify_pit_bundle.py core/pit_data.py`: PASS.
- `git diff --check`: PASS.

Independent fix-round review: PASS with no findings. Stable file SHA-256 values were `2BCE1F6A...` (`build_pit_bundle.py`), `6E8CBE51...` (`verify_pit_bundle.py`), and `0B961C58...` (`core/pit_data.py`). A focused negative probe also removed each of the three upstream input digest keys in turn; all three incomplete bundles were rejected.

## Remaining functional gate

The real Task 2 SEC fundamentals extraction must run first. Task 4 functional acceptance then requires building the immutable production bundle and exercising unchanged `evaluate_c`, `evaluate_a`, and `evaluate_n` against real filing/amendment boundaries. The fixture smoke validates plumbing and semantics only.
