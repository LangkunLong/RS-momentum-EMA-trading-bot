# V5 two-round study: prepare, verify, and export

The V5 two-round study is an append-only, provider-free example of an admitted
investigator study. It keeps the two arms, the historical round P0→A, and the
actual descendant round P1→B separate. A completed arm is evidence that the
persisted graph and its accounting records authenticate; it is not evidence of
real provider spend, a self-recursive optimizer, or a statistically reliable
market result.

## Offline CLI

The CLI creates a fresh synthetic repository only when the root is absent. An
occupied root is rejected, including an occupied root containing an empty
child. The environment used for an offline run has
`PYTHON_DOTENV_DISABLED=1` and blank credential variables. It does not read a
`.env` file, use a saved grant, contact a provider, load market data, or run a
live backtest.

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
$env:OPENROUTER_API_KEY = ""
$env:OPENROUTER = ""
$env:ALPACA_API_KEY = ""
$env:ALPACA_SECRET_KEY = ""
$env:FMP_API_KEY = ""
$env:NOTIFY_EMAIL_PASSWORD = ""

py -3.13 -m core.pit_optimizer_v5.two_round_study offline `
  --root C:\absolute\path\to\new-study
py -3.13 -m core.pit_optimizer_v5.two_round_study verify `
  --root C:\absolute\path\to\new-study
py -3.13 -m core.pit_optimizer_v5.two_round_study export `
  --root C:\absolute\path\to\new-study `
  --output C:\absolute\path\to\new-export
```

`offline` uses an explicit fake transport and records synthetic accounting.
The CLI reports `evidence_use=not_assessed`, while
`optimization_improvement` and `authored_code_execution` remain
`not_established`. Those labels do not claim real model use or provider
execution. `verify` and `export` reopen the persisted graph read-only. They do
not prepare, compose, resume, recover, reconcile, repair, or make a gateway
call. A completed arm can still have an unsuccessful or contradictory typed
outcome; the verifier retains its arm-level measurements and does not replace
them with a combined success flag.

## Explicit live Python sequence

A live study is a separate, user-authorized operation. The caller must execute
this sequence with a current grant and an admitted gateway; the CLI has no live
subcommand and never discovers credentials implicitly:

1. Call `prepare_two_round_study_v1` with a new absolute root and inspect both
   exact F preflight requests, schemas, selected parent, registry, rubric, and
   request comparison.
2. Freeze the prepared manifest and both F/L identities after inspection.
3. Obtain a fresh `StudyGrantV1` for this exact manifest, repository identity,
   provider/model, persistence scope, two arm slots, token/cost ceilings, and
   deadline. A planning approval, offline opt-in, or reused grant is not this
   authorization.
4. Issue the fresh authorization with `authorize_study_execution_v1`, then
   construct an approval-bound `StudyLedgerV1` and the exact
   `StudyOpenRouterGatewayV1` transport. Call `execute_study_arm_v1` once for
   each arm. Each arm has one investigator dispatch for its lifetime, reserves
   before transport, and settles one terminal. Provider-internal retries and
   any interruption must be disclosed in persisted accounting facts; study
   retries, schema-repair calls, and extra dispatches are zero.
5. If a transport was interrupted after reservation or response persistence,
   `resume_study_arm_v1` and `StudyLedgerV1.recover` are local recovery paths,
   not read-only verification: they can publish runtime records and recover
   settles accounting under a lock. Use them only with the same persisted
   request and grant, then inspect the exact imported response and drafts for
   both arms. Only `verify_study_v1` and `export_study_trace_v1` are read-only.
6. Obtain a semantic `HumanEvidenceReviewV1` bound to the frozen
   rubric, exact arm response/draft references, separate reasons and citations
   for evidence interpretation, revision quality, and claim pattern.
7. Call `verify_study_v1` and then `export_study_trace_v1` with the current
   authenticated graph. Verification and export are read-only and must not be
   used to recover an interrupted call.

For a live manifest, `provider_settings` must be caller-supplied and contain
exactly `provider`, `model`, `max_output_tokens`, `temperature`, and `seed`.
The admitted transport rejects non-`None` `temperature` and `seed`; the
pinned request uses both values as `None`. Retries and schema repair are zero,
and `max_attempts` is one. The grant's per-call and cumulative token/USD
ceilings, deadline, response-persistence consent, and two-arm lifetime limit
are explicit approved values chosen before dispatch. Unknown usage remains
pending for `reconcile_pending_usage`; that recovery helper cannot dispatch a
new call or turn unknown usage into a zero-cost receipt.

The review step is separate from execution and binds exact artifacts rather
than treating citation presence as semantic proof.

If a reservation, response, terminal, import, or round-two publication is
partial, stop transport and inspect the recorded state. Do not prepare a new
manifest or reuse a grant to make a preferred result complete. Verification
keeps `pending`, `failed`, `local_reject`, `not_reserved`, and incomplete
states distinct, including empty raw response bytes and typed failure records.

## What the trace contains

The exported directory is a fresh portable root with a separate filesystem
identity. `artifact-index.json` maps each allowlisted original byte to its
source path, source hash, authority root, artifact type, and upstream links;
derivative `rubric-results.json` and `trace.md` have their own hashes. The
export includes the manifest, request comparison, behavior registry, round-one
ancestor, each arm's round-two graph, live-study calls, imports, and case
contrasts. It preserves exact canonical JSON, binary blobs, raw responses,
failures, empty bytes, and size markers when they exist. It does not copy a
directory wholesale, relocate an original root, or reserialize bytes under an
old hash. The original root remains the authority for device/path identity;
the export proves the bytes carried into the portable bundle.

`trace.md` shows the generated investigator/author/critic artifacts per arm,
P0→A beside the actual P1→B records, raw parent/candidate case and control
decisions, fixed and live request sizes and caps, admission/equivalence
details, usage and cost accounting, pending/recovery facts, failures, and
limitations. F and L are separate identities. Scripted, synthetic,
live-generated, imported, unavailable, and human-reviewed values are labelled
separately.

The verifier computes production reports and case contrasts from authenticated
round-two observations. It keeps eligibility, evidence use, experiment
outcome, feedback attribution, improvement, and authored-code execution as
separate dimensions. Eligibility does not establish attribution. Human review
requires semantic reasons and artifact bindings; the presence of a citation or
a rubric hash alone cannot prove a claim. In the offline study,
evidence use is `not_assessed`; improvement and authored-code execution remain
`not_established`.
