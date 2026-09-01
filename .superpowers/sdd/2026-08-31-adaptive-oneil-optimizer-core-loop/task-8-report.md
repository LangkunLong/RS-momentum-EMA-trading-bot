# Task 8 report

Implemented and committed the initial schema-v4 adaptive optimizer state loop in
`b9c4371`, then completed the production composition and durability review fixes.

The fix round authenticates every retained diff and exact three-file source bundle
before role projection, cleans all materialized capabilities on exceptional paths,
publishes imported seed state only after its artifacts and initial checkpoint are
durable, and reports checkpoint presence only after an actual successful write.
Schema-v4 CLI execution now selects a concrete provider-gated live builder, publishes
the audit manifest before pricing or lease authority, uses an explicit authorization
window bound to the sealed authoring scope and full call plan, and closes the one-shot
lease once with exact attempt/skip accounting. Existing readiness may be reused only
when its canonical bytes are identical; resumed candidates are reconstructed,
authenticated, deterministically re-parented, reminted, and reevaluated with the
Decimal-safe panel artifact representation.

Verification:

- Compileall for the five changed production modules: passed.
- Ruff for the five changed production modules: passed (`All checks passed!`).
- Retained-parent tamper rejection and workspace cleanup smoke: passed.
- Seed checkpoint-write failure absent/aborted smoke: passed.
- Schema-v4 authorization open/close smoke: passed (9 calls, 2,340,000-token authority).
- Readiness reuse smoke: identical bytes accepted; differing evidence rejected.
- Injected canary dispatch smoke: live branch selected, 3 invalid investigator
  attempts and 6 dependent skips settled, finalizer/lease close invoked once with
  `iteration_limit`.

No provider request, Docker execution, Task 9 qualification/replay, remote operation,
secret access, source apply, or progress-file edit was performed.
