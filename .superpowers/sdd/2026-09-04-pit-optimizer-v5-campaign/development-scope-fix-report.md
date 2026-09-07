# Development scope review fix

Resolved the migration-documentation finding without changing generic exact-key decoding or artifact identities.

- Corrected `development-scope-report.md`: dataclass defaults apply to source construction, not persisted wire decoding.
- Documented in `docs/pit-optimizer-v5-operations.md` that pre-scope bytes remain readable and content-authenticatable under their original references, while typed use requires an explicitly rebuilt manifest/parent graph with new content-addressed references.
- Added a narrow manifest authentication diagnostic for missing `pit_data_scope` on the manifest or baseline parent. It tells the operator to retain the old artifact and rebuild the graph; it does not inject a field, reinterpret bytes, reuse an old digest, or weaken unknown-key rejection.
- Applied the same parent diagnostic to manifest building when an old parent reference is supplied.

Validation: source diff inspection, `git diff --check`, in-memory compilation of `core/pit_optimizer_v5/manifest.py`, and Ruff on that module. No tests or synthetic probes were read, created, modified, or run. No Docker, network, or provider operation was performed.
