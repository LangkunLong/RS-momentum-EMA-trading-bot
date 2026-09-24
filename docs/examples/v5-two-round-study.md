# V5 two-round study: prepare, verify, and export

The V5 two-round study is an append-only, provider-free example of an admitted
investigator study. It keeps the two arms, the historical round P0→A, and the
actual descendant round P1→B separate. A completed arm is evidence that the
persisted graph and its accounting records authenticate; it is not evidence of
real provider spend, a self-recursive optimizer, or a statistically reliable
market result.

This guide describes the V5 contract and its recovery boundaries. It does not
authorize a new study preparation, provider request, grant, retry, slot reuse,
or retained-study mutation. Offline verification and export of an already
authorized study are read-only. A future live attempt requires fresh, explicit
authorization for its exact manifest, request, grant, budget, and dispatch; a
new source revision by itself does not authorize dispatch or make an earlier
attempt safe to rerun.

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

## Future live execution requires separate authorization

A live study is a separate operation and is outside the offline workflow. Only
after separate user authorization may a caller use this sequence with a current
grant and an admitted gateway; the CLI has no live subcommand and never
discovers credentials implicitly:

This generic sequence is not admission to use any currently retained study.
The existing primary attempts remain pending, withheld arms remain
`not_started`, and consumed primary slots stay used. Do not reuse the
source-pinned wrapper from the stopped attempt. A future execution proposal
must separately bind its exact source/runtime and concrete manifest, request,
provider/model, limits, and authority; this guide grants none of them.

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

The old prepared attempts keep their original source provenance. Reviewing
corrected source does not authorize rerunning an old attempt wrapper or writing
through an existing study's one-shot slot.

The review step is separate from execution and binds exact artifacts rather
than treating citation presence as semantic proof.

## Diagnose and recover a pending call

A provider diagnostic is bounded metadata for an unresolved handoff. It stores
a safe phase and code, an optional HTTP status from 100 through 599, an optional
provider request ID of at most 512 UTF-8 bytes, and `accounting_status=pending`.
The record never includes exception text, response headers, or credentials.
For example, `transport/request_timeout` identifies the observed exception
class; it does not establish whether the provider received the request or what
it billed.

When both content extraction and usage accounting fail, a new diagnostic also
stores independent closed `content_failure` and `accounting_failure` codes.
Content categories distinguish an invalid choice structure, invalid message
structure, a missing or non-string content field, and a failed accessor.
Accounting categories preserve the existing safe inline usage validation code,
or use a generic accounting-failure code when the source cannot support more
precision. These are bounded adapter/SDK observations; the SDK may normalize a
wire-level omitted nullable content field to `None`. No raw exception details
are persisted. Existing diagnostic bytes without these optional fields remain
valid and are exported unchanged.

When the SDK returns a model content string but inline usage is missing or
invalid, `response-observations` authenticates the request, reservation,
dispatch claim, grant, and manifest. If response persistence was authorized,
`observed-raw-responses` contains the exact SDK model content encoded as UTF-8.
Those bytes are not a captured HTTP response body. The observation envelope is
written first, so a valid interruption after the envelope and before the raw
write reopens as pending. It does not create a usage receipt, terminal, or
import. Oversized content is represented only by a bounded diagnostic.

`verify` and `export` reopen the same study read-only. They authenticate and
preserve the diagnostic, observation envelope, and any observed content bytes
in the exact-byte trace while leaving pending accounting unresolved. They do
not retry, call a provider, or reconcile usage.

Content availability and accounting are separate facts. If the SDK did not
yield string content and accounting is incomplete, the diagnostic records the
bounded adapter/SDK extraction and accounting categories, while original
response bytes remain unavailable. The categories report what the adapter
observed; for example, the SDK may normalize a wire-level omitted nullable
content field to `None`. If content is a string but accounting is incomplete,
an authorized observation may retain those exact SDK model-content bytes. If
accounting is complete but content is missing or non-string, the adapter may
settle a rejected completion with normalized `response_text=""`; this sentinel
means no string content was extracted and does not recover the original HTTP
body or provider bytes.

Recovery uses the exact persisted manifest, grant, and request:

1. Call `recover_study_call_v1` or `StudyLedgerV1.recover` only to settle a
   response and usage record that are already fully authenticated. A diagnostic
   by itself, or an envelope with no raw-content write, cannot be settled by
   automatic recovery into a terminal.
2. After independently authenticating an authoritative provider usage record,
   pass its `CompletionResultV5` and provider reference to
   `StudyLedgerV1.reconcile_pending_usage`. Its content and provider request
   ID must agree with any preserved observation. If the observation envelope
   exists but its raw-content write was interrupted, this explicit authorized
   path restores the missing observed blob only when the UTF-8 response bytes'
   SHA256 and length and the provider request ID exactly match the authenticated
   envelope. It rechecks current authority, reservation, dispatch claim, and any
   prior usage receipt while holding the repository transition lock; a mismatch
   or missing approval publishes no replacement bytes or terminal. The normal
   usage receipt and settlement path then continues. This local reconciliation
   does not make a provider call.
3. If the pending diagnostic retains a nonempty provider request ID, every
   incoming response, observation, or reconciled completion must use that same
   ID. An absent legacy ID adds no identity constraint. An identity mismatch is
   rejected before response metadata, raw bytes, observed bytes, reconciliation
   events, or terminals are written. Reopened verification cross-checks any
   diagnostic against observations and accounted completions; long-lived
   recovery reauthenticates the current graph under the transition lock.
   Legacy `provider_request_id=null` remains unbound; an empty-string diagnostic
   ID remains invalid under the existing schema.
4. If authoritative accounting is available for a diagnostic that says no
   string content was extracted, the failed completion may use the adapter's
   empty-text sentinel only when its provider request ID matches the retained
   diagnostic. It settles an accounted failed completion with no import and no
   automatic retry. It does not claim recovered original response bytes or
   make a scientific result successful.
5. If no authoritative usage is available, leave the reservation pending.
   Its one-shot slot stays used and the study must not dispatch a retry, reuse
   the grant, or infer zero spend. A successful authenticated terminal can
   then be imported through the existing live sequence; a terminal failure
   does not imply an import.

`recover`, `resume_study_arm_v1`, and `reconcile_pending_usage` are recovery
operations that may publish local records. Only verification and export are
read-only.

## Scientific interpretation

Diagnostics, response availability, authoritative usage, and terminal outcome
describe different parts of an attempt. None alone establishes evidence use,
feedback attribution, experiment completion, authored-code execution, or
optimization improvement. An accounted failed completion remains a failed
attempt and does not validate a hypothesis or satisfy a successful-primary
gate.

## Grant price units

`StudyGrantV1.input_price_upper_bound` and
`StudyGrantV1.output_price_upper_bound` are **USD per million tokens**. Enter
the approved provider rates in these units; do not pass USD-per-token values.
The reservation's conservative prospective cost is
`(input_bound_bytes × input_price_upper_bound + max_output_tokens × output_price_upper_bound) / 1,000,000`
USD. The request byte bound is used as the input-side cap. Grant fields such as
`per_call_usd_ceiling` and `cumulative_usd_ceiling` remain total USD amounts.
Check the persisted reservation's prospective cost against the approved
proposal before dispatch.

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

For original artifact entries, each `upstream_refs` value is an authenticated
content-match alias based on the same relative path and SHA-256 digest. A
matching value may identify the same bytes under another authenticated root;
it does not establish a causal dependency or owning authority edge. The entry's
`authority`, `source_relative_path`, and `source_sha256` remain the owning
authenticated identity. For derivative `rubric-results.json` and `trace.md`,
`upstream_refs` are authenticated input references used to construct those
derivatives. The machine-readable `provenance_semantics` block in
`artifact-index.json` records these meanings.

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
