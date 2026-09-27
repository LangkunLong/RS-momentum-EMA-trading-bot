# Local inputs and evidence

The September 26 cleanup leaves three useful groups in the ignored `.artifacts` directory. These files exist only in the prepared local checkout; cloning the repository does not download them. Start with `.artifacts/README.md` and its relocation manifest when assigning an issue.

| Location under `.artifacts` | Purpose | Related issues |
| --- | --- | --- |
| `data/acquisition` | Dated membership, source receipts, industry classification inputs and original acquisition provenance | #66–79 |
| `data/development-sp500-v2` | Existing limited development bundle, prices and matching manifest | #67, #81–83 |
| `data/sec-fundamentals` | Official financial source archives, exported fundamentals, identity and publication records | #70–72 |
| `data/sec-institutional` | Institutional ownership source inputs | #73–74 |
| `data/historical-price-source` | Original price cache used by the development bundle | #69 |
| `evidence/unresolved-studies` | Four historical study roots and eight retained exports | #89 |
| `evidence/incidents` | Historical grants, attempt diagnostics, exact lookup responses and closeout records | #89 |
| `evidence/integrated-transport-review` | Final source-bound offline review and preservation checks | #81, #88–89 |
| `evidence/evaluator-environment` | Historical evaluator image, source and configuration identities | #81–82 |
| `evidence/source-history` | One compact source recovery bundle and branch-cleanup receipts | #81 |
| `evidence/main-integration` | Integration identity and pre-existing test-failure evidence | #81 |
| `evidence/cleanup` | Retained-file hashes, old-to-current path mappings and cleanup verification | All |
| `reviews/2026-09-26-github-issue-plan` | Three parent registers, 44 published child bodies and dependency guides | #63–109 |
| `reviews/2026-09-26-architecture-gap-map` and `reviews/2026-09-26-project-audit` | Dated architecture findings underlying the issue registers | #63–109 |

The current development bundle covers the Standard and Poor's 500 research scope. It is not the accepted production bundle: the historical data issues still own missing coverage, publication rules, institutional ownership and industry/sector integration.

Retained evidence was relocated without changing its contents. Original absolute paths inside immutable records were not rewritten. Use the relocation manifest to find the corresponding bytes; relocation alone does not establish that a historical launcher or study can be reopened from a different path. Portable execution and source/environment acceptance remain work for issue #81.

The four historical attempts remain incomplete with unknown actual usage and cost. Keeping their evidence does not authorize another call, reconciliation or trading, and does not establish optimization improvement. The fourth study and its two exports were copied from temporary directories; those external originals were left untouched.

The previous broad artifact archive, copied worktree outputs, superseded experiments, obsolete recovery copies and disposable caches were deleted. Future work should use a bounded issue-specific output directory, retain inputs and evidence needed by its acceptance criteria, and remove scratch outputs when that work finishes. Do not delete a source dataset or unresolved accounting record merely because it is old.
