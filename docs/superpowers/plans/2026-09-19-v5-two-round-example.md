# V5 Two-Round Evidence Study Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible synthetic two-round trace and an independently admitted model-investigator study that separately assesses evidence use, experimental outcome, and optimization improvement.

**Architecture:** Add an opt-in `two_round_study` package beside the existing V5 runtime. A frozen behavior registry supplies synthetic execution; an independent study ledger admits the two investigator calls and authenticates their imports into provider-free runtime arms. Existing selection, memory, runtime authority checks, mechanism reports, and legacy role schemas remain unchanged; a study verifier checks the additional provenance and per-case contrasts.

**Tech Stack:** Existing Python 3.13, frozen dataclasses, `Decimal`, canonical JSON/SHA-256, V5 local artifact repository and runtime protocols, pytest and Ruff. Use `py -3.13` on this Windows checkout; its default launcher currently points to a missing Python 3.14 executable. No new dependencies.

**Spec:** [Approved revision 2 design](../specs/2026-09-19-v5-two-round-example-design.md), committed at `0420516`, approved by the user on 2026-09-19.

## Global Constraints

- Planning only in this document. No implementation or study execution is claimed by checking the document into Git.
- Source prerequisite: `7fcc6af767ddde2756415b2c1f2ede61753ee995`, including the local-only evaluator-metric, final-deadline, and unavailable-worker delivery fixes. The recorded baseline is 93 focused tests passing; run the prerequisite check when implementation starts.
- Runtime manifests retain `provider=None`, `pit_data_scope="production"`, and required semantics. Every runtime role uses `FixtureRoleTerminalAuthorityV5`; the scope string does not authorize market access.
- Target `evaluate_exit`; recipe `evaluate_exit_atr20_fraction_v1`; field `features.atr_20_fraction`. Round one inputs: `0.20`, `0.50`, `0.80`, missing; applicability `is_present`; minimum relevant cases `2`.
- Prediction: `exit.decision_changed_count`, increase, zero tolerance, `relevant_cases`. Control: `exit.protected_control_unchanged_count`, unchanged, zero tolerance, `control_cases`. Omit `evaluator.exit_attribution_count` from this local-only study.
- There are at most **two live investigator attempts total**, one per arm, in one shared ledger outside both fixture descendants. Automatic retries and schema-repair calls are `0`. A later attempt requires a new study identity and authorization.
- A live call needs a separate explicit grant covering provider/model, call/token/cost limits and response persistence. Planning approval and an offline fixture opt-in are not this grant. Do not read credentials or invoke a provider during implementation verification.
- No market datasets, backtests, saved campaign resumption, candidate-source execution, held-out evaluation, trading, deployment, or dependency installation. Synthetic deadlines remain cooperative; `registered_sandbox` remains unavailable.
- Preserve legacy role/schema bytes, runtime authority checks, production metric definitions, selector/admission rules and memory limits. Never manufacture a distinct fingerprint, force a parent, repair a generated answer, or increase a limit after a result.
- Evidence use, experiment outcome and optimization improvement are separate fields and separate acceptance gates. Synthetic or mocked completion cannot establish real-model evidence use. This study always reports optimization improvement and execution of model-authored code as `not_established`.
- Execution preference already selected: fresh **Luna agents with max reasoning**, task by task, with spec-compliance and code-quality review between tasks. Do not ask the user to choose that workflow again. Planning completion does not start implementation.

---

## Source seams and implementation boundary

Read these definitions at the checked-out revision before editing; line numbers can move:

| Existing source | Reuse and boundary |
| --- | --- |
| `provider.py`: `OneShotJsonCompletionV5`, `RoleCallKeyV5`, `FixtureRoleRunnerV5`, `parse_and_bind_role_artifact` | Reuse the one-shot transport protocol and strict fixture parser. `ProviderCompletionRequestV5` accepts an exact legacy `RoleRequestV5`; it cannot carry the new live schema. |
| `production_provider.py`: `LocalRoleAuthorizationLedgerV5`, `_authenticate_role_invocations_v5`, `OpenRouterOneShotJsonCompletionV5` | Reference full ledger/audit authentication and accounting semantics. These concrete adapters are tied to a production manifest and legacy request/parser; do not subclass them to disguise a study envelope. |
| `artifacts.py`: `append_binary_state`, `load_binary_state`, `authenticate_raw_artifact`, `adapter_state_transition`, `root_identity_sha256` | Reuse safe create-only storage, byte authentication and locking for a separately named study journal. Add only bounded read-only enumeration of adapter blobs so verification can detect unreferenced ledger records. |
| `runtime.py`: `FeedbackRoundDependenciesV5`, `_Runtime._role`, `_valid_terminal_authority`, `run_feedback_round_v5` | Inject study ports. Existing completions can skip the invoker on recovery, so verify imported packages independently before resuming. |
| `production_runtime.py`: `LocalRoleRequestFactoryV5`, `MechanismRoleRequestAdapterV1`, `SelectionNoveltyResolverV5`, `LocalArchiveReducerFactoryV5` | Build actual requests, preserve final admission, recover scheduling, and retain the real parent and memory selections. |
| `mechanism_artifacts.py`: `MechanismExtensionCapabilityV1`, `MechanismArtifactRepositoryV5`, `MechanismRuntimeExtensionV1` | Construct fresh authority after the persisted selected intent; delegate observation, report persistence and role evidence. |
| `probes.py`: `policy_probe_suite_v1`, `fingerprint_policy_client_v5`, `SemanticFingerprintV5` | Derive fingerprints through repeated/interleaved calls to the same frozen synthetic decision function used by supplemental workers. |
| `fixture_runtime.py`: `SyntheticCandidateRuntimeV5`, `verify_fixture_run_v5` | Reuse evaluator arithmetic/lease conventions where applicable, not its entry-specific fake fingerprints or canned response verifier. Keep the stock verifier unchanged. |

New production-facing exports are unnecessary. The package is opt-in; importing `core.pit_optimizer_v5` must not import it or initialize a provider. Do not import test helpers into application code. Use the existing synthetic authority graph in `tests/test_pit_optimizer_v5_mechanism_artifacts.py::_authenticated_fixture` as a construction reference, not a runtime dependency.

## File map and task order

All paths below are relative to the repository. `P` in explanatory prose means `core/pit_optimizer_v5/two_round_study`; file lists use full paths.

| Task | New modules | Responsibility |
| --- | --- | --- |
| 1 | `contracts.py`, `schema.py`, `store.py`, `__init__.py` | Closed study/draft/result types, opt-in schema, immutable typed study bytes |
| 2 | `registry.py`, `fixtures.py` | Frozen behavior/source/evaluator catalog and fresh synthetic authority graphs |
| 3 | `ledger.py`, `live_calls.py`, `transport.py` | Independent admission, durable one-shot calls, receipts and recovery |
| 4 | `imports.py` | Authenticated F/L/T translation and exact fixture replay |
| 5 | `compiler.py`, `contrast.py` | Selected-draft binding, fresh precommitment and all-case verification |
| 6 | `runtime_ports.py` | Role routing, scripted roles, registry candidate runtime and late-bound extension |
| 7 | `driver.py`, `comparison.py` | Real round one, disk restart, two isolated alternative round twos |
| 8 | `verification.py`, `trace.py`, `__main__.py` | Independent verification, explicit verdicts, local export and offline entry point |

Tasks 1–8 are sequential integration gates. An implementer may delegate bounded read-only source checks, but must not run another task's edits concurrently in the same files. Each task ends in a scoped commit after its focused tests and review. All test files below are new; retain the existing tests intact.

## Shared contract conventions

- New persisted types use suffix `V1`, `schema_version=1`, exact fields, frozen values, strict `from_primitive`, `to_primitive`, `canonical_bytes` and `sha256`. Reject duplicate JSON keys, unknown fields, nonfinite numbers, noncanonical persisted encodings and wrong literal types. Raw provider JSON may have ordinary whitespace/key ordering; preserve its exact bytes and separately canonicalize the strictly parsed value. Decimal wire values are strings; missing ATR is JSON null.
- A stored `ArtifactRefV5` identifies bytes, not permission. `StudyStoreV1` authenticates bytes before decoding; higher layers authenticate the full graph before accepting authority. Do not claim a local hash is an operator signature.
- `StudyArmV1 = Literal["primary", "withheld"]`; `StudyModeV1 = Literal["offline_fixture", "live_study"]`. Offline grants and mock receipts are explicitly tagged and cannot be imported as live authority. Offline mock usage/receipts exercise accounting contracts but represent simulated usage; the trace reports zero actual provider calls/spend and displays any simulated accounting separately.
- Exceptions are `StudyContractError`, `StudyAuthorityError`, `StudyAdmissionError`, `StudyPendingAccounting` and `StudyPrecommitmentError`, all defined in Task 1. Persist the actual failure stage/reason; do not translate a failure into a positive verdict.
- Existing types named below are imported from their defining V5 modules. New types are defined in the task that produces them. Tests use fresh `tmp_path` roots; no default path points to a saved repository or datasets.

### Task 1: Closed schema, claim axes and immutable study storage

**Files:**
- Modify: `core/pit_optimizer_v5/artifacts.py` beside `load_binary_state`/`has_binary_state`: bounded read-only blob enumeration only.
- Create: `core/pit_optimizer_v5/two_round_study/__init__.py`
- Create: `core/pit_optimizer_v5/two_round_study/contracts.py`
- Create: `core/pit_optimizer_v5/two_round_study/schema.py`
- Create: `core/pit_optimizer_v5/two_round_study/store.py`
- Test: `tests/test_pit_optimizer_v5_study_contracts.py`

**Interfaces:**
- `ExperimentDraftV1`: `hypothesis_id`, `cited_evidence_ids`, existing `MechanismPredicateV1`/`MechanismRecipeV1`/metric/disconfirming types, `expected_changed: tuple[bool, ...]`, `rivals: tuple[RivalPatternV1, ...]`, `configuration_id`, and `claim_kind: Literal["threshold", "general"]`. `RivalPatternV1` contains a unique registered rival name and ordered boolean pattern. No caller-supplied parent, path, source, spec hash or authority fields.
- `StudyResponseV1`: ordinary `InvestigatorArtifactV5` plus one draft per hypothesis. Its wire envelope contains `schema_version`, the exact fixture `binding`, `artifact`, and `drafts`. The investigator artifact's canonical projection remains unchanged.
- `StudyManifestV1`: study ID/mode, source revision, fixture/registry/rubric/schema/parser/prompt hashes, round-one checkpoint and snapshot refs, both arm preflight refs, fixed resource limits, pinned provider settings or explicit offline settings. Final manifest is create-only before either attempt.
- `StudyVerdictsV1`: `trace_integrity`, `evidence_delivery`, `evidence_use`, `production_assessment`, `case_contrast`, `experiment_completion`, `feedback_attribution`, `optimization_improvement`, `authored_code_execution`, and artifact-backed reasons. Evidence use is `not_assessed|supported|not_supported|inconclusive`; production assessment preserves the production report vocabulary; contrast is `matched_on_cases|contradicted_on_cases|unavailable`; improvement/execution accept only `not_established` in V1. Other gates use `verified|failed|incomplete|not_assessed`, except attribution `supported|inconclusive|not_assessed`.
- `study_response_schema_v1(*, fixture_request: RoleRequestV5, configuration_ids: tuple[str, ...]) -> bytes`; `parse_study_response_v1(*, raw: bytes, fixture_request: RoleRequestV5, configuration_ids: tuple[str, ...]) -> StudyResponseV1`.
- `StudyStoreV1(repository: LocalArtifactRepositoryV5)`: `put(*, kind: str, key: str, content: bytes) -> ArtifactRefV5`, `read(reference: ArtifactRefV5) -> bytes`, `put_contract(*, kind: str, key: str, value: object) -> ArtifactRefV5`. The contract method accepts only an explicit type registry, not arbitrary dataclasses. Paths are controller-owned `study-v1-<kind>` namespaces with validated keys. Freeze implementation ceilings of 4 MiB per blob, 4096 blobs and 64 MiB per namespace; reject oversize preparation instead of raising these after a live attempt.
- `LocalArtifactRepositoryV5.list_binary_state_refs(*, namespace: str, maximum_entries: int, maximum_bytes: int) -> tuple[ArtifactRefV5, ...]`: sorted read-only enumeration of exactly one adapter-blob namespace, using the repository's safe directory/read primitives. Enforce entry and total-byte limits before returning; missing namespace returns empty, unexpected entries/links/corruption reject. It neither creates files nor repairs missing state.

- [ ] **Write failing storage and schema tests.** Cover create-only identical reuse/conflicting bytes, strict decoding, wrong binding/citation, duplicate/missing hypothesis draft, arbitrary code/path/metric field, and distinct claim axes. Build the fixture request with `build_role_request_v5` and its ordinary investigator schema; retain its original schema/request bytes before study parsing. Use this exact storage assertion:

```python
def test_study_store_is_create_only(tmp_path):
    repository = LocalArtifactRepositoryV5(tmp_path)
    store = StudyStoreV1(repository)
    reference = store.put(kind="test", key="one", content=b"first")
    assert store.read(reference) == b"first"
    assert store.put(kind="test", key="one", content=b"first") == reference
    with pytest.raises(StudyAuthorityError):
        store.put(kind="test", key="one", content=b"changed")
```

- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_contracts.py -q`. Expected collection failure for the missing study package, then focused assertion failures until implemented; an unrelated environment failure is not RED evidence.
- [ ] **Implement strict schema projection and parsing.** Embed the ordinary investigator artifact/binding schema inside the new envelope without changing the legacy authority. Validate the full study schema, project `{binding, artifact}` using the original parsed values, and call `parse_and_bind_role_artifact(request=fixture_request, response_text=...)`. Reject unsupported `authoring_mode`; require the configured hypothesis count and one draft for every returned hypothesis. Do not infer missing draft fields from prose.

```python
translated = canonical_json_bytes_v5({
    "binding": envelope["binding"],
    "artifact": envelope["artifact"],
})
# Parsing validates the original values; it must not rewrite them.
ordinary = parse_and_bind_role_artifact(
    request=fixture_request, response_text=translated.decode("utf-8")
)
```

- [ ] **Implement storage through safe repository primitives.** `put` uses `append_binary_state`; bounded `read` uses `load_binary_state` with the exact namespace/key/ref. Add the enumeration method without exposing private filesystem helpers to the study package. Test nonexistent namespace, sorted refs, unknown entries, links, exceeded limits and no repair. Translate storage conflict/corruption to `StudyAuthorityError` with the original exception chained. Check each verdict field independently: a supported aggregate cannot set evidence use or optimization improvement.
- [ ] **Run GREEN:** the Task 1 command must pass, including tests that the legacy schema still rejects the new envelope and its byte hash is unchanged.
- [ ] **Commit:** stage only `artifacts.py`, the four new modules and this task's test; commit `feat: add closed V5 study contracts and storage`.

### Task 2: Freeze a consistent synthetic behavior and evaluator registry

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/registry.py`
- Create: `core/pit_optimizer_v5/two_round_study/fixtures.py`
- Test: `tests/test_pit_optimizer_v5_study_registry.py`

**Interfaces:**
- `RegistryConfigurationV1`: immutable configuration ID, finite family/parameters, source bundle/revision identity, parent configuration identity, synthetic evaluator inputs and full fixed-suite output vector/fingerprint. Separate family configuration from a model's prediction.
- `FrozenBehaviorRegistryV1`: ordered configurations, suite identity, decision-function/source/template hashes, frozen canonical bytes and digest.
- `build_study_registry_v1() -> FrozenBehaviorRegistryV1`; `registry_client_v1(registry, configuration_id) -> StrategyPolicyClientV3`; `registry_decision_v1(*, registry, configuration_id: str, method: str, snapshot: object) -> object`; `verify_registry_v1(registry) -> None`.
- `StudyFixtureV1` contains fresh `LocalArtifactRepositoryV5`, `AuthenticatedCampaignManifestV5`, `FrozenBehaviorRegistryV1`, and the baseline configuration ID. `create_study_fixture_v1(*, root: Path, registry: FrozenBehaviorRegistryV1) -> StudyFixtureV1` creates only a new synthetic graph; `reopen_study_fixture_v1(*, root: Path, manifest_ref: ArtifactRefV5, registry: FrozenBehaviorRegistryV1) -> StudyFixtureV1` authenticates an existing graph without repairing it.
- `study_resource_budget_v1() -> MechanismResourceBudgetV1`: implementation constants `max_cases=8`, `max_repetitions=2`, `timeout_ms=1000`, `cpu_seconds=Decimal("1")`, `memory_mib=128`, `output_bytes=65536`. These are declared synthetic bounds, not an enforcement claim. Freeze them in the manifest; reject exceeding drafts.

- [ ] **Write failing registry tests.** Test determinism, fixed/supplemental overlap on full snapshot identities, tampered output vectors, genuine parent/sibling equivalence, unknown source/configuration and no source execution. Pin this concrete initial catalog: baseline `P0` preserves a contract-valid neutral exit decision; `A` toggles `early_winner_hold` only for present ATR `<0.05`; `S` toggles it only for present ATR `>=0.05`. Non-exit decisions stay identical across all configurations. For `S` descendants, use XOR against S's registered decision: inert, always-on (including missing), and `gte` with thresholds `0.05` and `0.50`. All preserve next-stop and other exit fields. Freeze this catalog before any model call, including equivalent entries used for negative cases.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_registry.py -q`; expected missing registry implementation or failed expected decision/fingerprint assertions.
- [ ] **Implement the decision function and registered client.** It accepts only configuration, method and canonical snapshot. It cannot inspect experiment labels, probe IDs, outcomes, predictions or desired admission. Validate each returned decision against its snapshot. For exits, use the snapshot's current scale-out tier and a valid unchanged next-stop; the changed boolean does not authorize an exit action. Restrict source materialization to a reviewed `evaluate_exit` function template under `core/strategy_policy/v3/exit.py`; integer finite configuration selection is a literal axis. Store the source bytes, but never import or execute them.

```python
# x is the canonical snapshot's ATR value, converted once to Decimal or None.
active = x is not None and x >= threshold
candidate = replace(parent_decision,
                    early_winner_hold=parent_decision.early_winner_hold ^ active)
assert candidate.next_stop_price == parent_decision.next_stop_price
```

- [ ] **Derive every fingerprint through `fingerprint_policy_client_v5`.** Freeze `policy_probe_suite_v1()` inputs and the resulting ordered canonical outputs. Recompute on reopen. A differs at fixed ATR `0.025` but not at corpus `0.20/0.50/0.80/None`; S differs at fixed `0.075`. The S-relative `gte 0.50` option is honestly equivalent on the fixed suite and remains rejectable; `gte 0.05` differs. Check overlap using method plus full canonical snapshot bytes, not ATR alone. No `_static_fingerprint` or variant-only fabricated observations.
- [ ] **Build the standalone synthetic authority graph.** Use the construction in `_authenticated_fixture` as a reference, replacing its static baseline fingerprint with the registry fingerprint. Pin `hypotheses_per_investigator=1`, `max_variants_per_template=2`, `max_discovery_survivors_per_template=2`, `archive_capacity=1`, `max_feedback_rounds=2`, `investigator_memory_max_bytes=3072`, symbol edits only. Use existing fixture resource limits (one evaluation, 60-second mechanics/quick/episode, 120-second round, 240-second campaign). Synthetic return inputs use starting equity `100`, baseline ending equity `100`, A base `101`, S base `102`; derive gross/base/stress consistently and compute annualized returns through `annualized_return_pct`. Round-two registered outcomes are frozen supplied inputs too, never derived from the expected contrast. The later integration gate must confirm these settings really summarize A; fix and review the fixture before freezing any live manifest if they do not.
- [ ] **Run GREEN:** the Task 2 command passes. Assert exact corpus pattern A=`False,False,False,False`, fixed A/P0 fingerprints differ, A/S differ, equivalent options retain equality, and report inputs produce `P0 < A < S` without overriding CAGR.
- [ ] **Commit:** stage the two modules and this test; commit `feat: freeze consistent synthetic V5 study behaviors`.

### Task 3: Admit and account for independent live study calls

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/ledger.py`
- Create: `core/pit_optimizer_v5/two_round_study/live_calls.py`
- Create: `core/pit_optimizer_v5/two_round_study/transport.py`
- Test: `tests/test_pit_optimizer_v5_study_ledger.py`
- Test: `tests/test_pit_optimizer_v5_study_live_calls.py`

**Interfaces:**
- `FixturePreflightV1`: arm/mode, fixture root identity, checkpoint/snapshot refs, exact intended `RoleCallKeyV5`, persisted F bytes/schema/evidence refs and F hash. Define this in `live_calls.py`; it contains no runtime completion.
- `StudyCallRequestV1` (L): study/arm/attempt (`1`), `role="investigator"`, `role_position=1`, `attempt_kind="primary"`, `preflight_ref`, `fixture_request_sha256`, `messages`, `schema_json`, projected wire messages/schema, pinned `model`/settings, `max_output_tokens` and all corresponding hashes. No aliasing of L's identity with F's identity.
- `StudyGrantV1`: study manifest/root/audit identities, mode, provider/model and supported settings, two arm slots, input/output/cumulative token and USD ceilings, per-call deadline, price upper bounds, response-persistence consent, operator approval reference. A typed runtime authorization must match this persisted grant; a digest/JSON file alone cannot mint authorization. Offline tests use `mode="offline_fixture"` and injected fake transport.
- `StudyExecutionApprovalV1` is an ephemeral controller capability bound to one grant/manifest/root and the current explicit user approval reference. `authorize_study_execution_v1(*, store: StudyStoreV1, manifest: StudyManifestV1, grant: StudyGrantV1, approval_reference: str) -> StudyExecutionApprovalV1` is called by the controller only after the corresponding approval, never by grant decoding or recovery. It records that provenance, not a claim of cryptographic operator identity. Offline mode uses a separately tagged explicit fixture opt-in; it cannot construct a live capability.
- `StudyReservationV1`, `StudyCallTerminalV1`, `AuthenticatedStudyTerminalV1`: reserve binds grant, L, slot, invocation owner and prospective usage; terminal binds prior journal digest, sequence, request/response/parse/attempt refs and cumulative usage. Authenticated terminal is reconstructed by ledger verification, never accepted directly from JSON. Reuse `RoleAttemptFactsV5`, `RoleUsageFactsV5` and `RoleTerminalReceiptV5` inside the study terminal record, which adds explicit study/manifest/audit identities. The receipt's self-hash alone is insufficient. Use the existing `RoleFailureCode`/outcome mapping for attempt facts; keep study-specific reason text separately.
- `StudyLedgerV1(store: StudyStoreV1, manifest: StudyManifestV1, grant: StudyGrantV1, approval: StudyExecutionApprovalV1 | None = None)` with `reserve(request: StudyCallRequestV1) -> StudyReservationV1`, `settle(reservation: StudyReservationV1, *, completion: CompletionResultV5, response_ref: ArtifactRefV5, parsed_ref: ArtifactRefV5 | None, failure_code: RoleFailureCode | None) -> AuthenticatedStudyTerminalV1`, `recover(request: StudyCallRequestV1) -> AuthenticatedStudyTerminalV1 | None`, and `verify_terminal(reference: ArtifactRefV5) -> AuthenticatedStudyTerminalV1`. `None` from recovery means no reservation; an unresolved reservation raises `StudyPendingAccounting`. A new live reservation requires the exact current approval capability; read-only verification/recovery does not.
- `build_study_call_v1(*, preflight: FixturePreflightV1, manifest: StudyManifestV1, fixture_request: RoleRequestV5, registry: FrozenBehaviorRegistryV1) -> StudyCallRequestV1`.
- `run_study_call_v1(*, request: StudyCallRequestV1, fixture_request: RoleRequestV5, ledger: StudyLedgerV1, gateway: OneShotJsonCompletionV5, deadline_monotonic: float) -> AuthenticatedStudyTerminalV1`; recovery is `recover_study_call_v1(*, request, ledger) -> AuthenticatedStudyTerminalV1 | None`, with no gateway argument.
- `StudyOpenRouterGatewayV1` implements `OneShotJsonCompletionV5`, is bound to this concrete study ledger, and resolves only the explicitly selected credential environment handle inside an admitted invocation. Constructor, readiness, import, offline CLI and verifier never resolve it.

- [ ] **Write failing accounting tests with a counting fake gateway.** Cover one primary plus one withheld attempt, duplicate invoke/reopen returning the original terminal with no new call, shared caps across arms, reservation exclusivity, returned-model mismatch, negative/excess usage, unsupported settings, schema/prompt size overflow, invalid JSON with accounted usage, transport failure, interruption after reservation/response/terminal, and pending usage blocking the other arm. The fake implements the exact `invoke_json_once` signature and returns a typed `CompletionResultV5`; its counter increments only on that call.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_ledger.py tests/test_pit_optimizer_v5_study_live_calls.py -q`; expected missing study admission/caller, then the specific failing invariants.
- [ ] **Implement deterministic F→L construction and admission.** Preserve F's evidence and binding in the actual message envelope, add pinned instructions and closed vocabulary only, and retain the full translation diff. Never expose expected study answers, scoring patterns or evaluator outcome tables in instructions. Expose candidate family semantics/parameters so selection is meaningful. Model settings are restricted to those the transport actually sends; record seed as unsupported when absent. Use the same supported settings in both arms; do not silently drop requested temperature/seed/reasoning fields.

```python
wire_messages = canonical_json_bytes_v5(wire_role_messages_v5(request.messages))
wire_schema = canonical_json_bytes_v5(wire_role_schema_v5(schema))
input_bound = len(wire_messages) + max(len(request.schema_json), len(wire_schema)) + overhead
cost_bound = (Decimal(input_bound) * input_price
              + Decimal(request.max_output_tokens) * output_price) / Decimal(1_000_000)
# Check per-call and shared remaining bounds before reserving; bytes are a
# conservative prospective bound, never reported as actual provider tokens.
```

- [ ] **Implement the independent append-only study ledger.** Use `StudyStoreV1` plus `repository.adapter_state_transition(namespace="study-v1-ledger", key=study_id)` for serialized local transitions. Persist immutable grant/request/reservation/response/terminal records under deterministic keys. Enumerate all records through Task 1's bounded method; reject orphan records, duplicate/forked or missing sequences, unauthorized slots and unreferenced terminals. Verify previous-record hashes from genesis, recompute cumulative usage and authenticate request/schema/raw bytes and attempt facts at every reopen. Identity includes the exact repository root, study manifest, audit domain and grant; a copied ledger in an arm root is not authority. Reserve before transport; never hold an unbounded lock through the network call. Persist invocation ownership so a second process cannot resend a pending call. This is local provenance/accounting, not cryptographic proof against an operator replacing the entire store.
- [ ] **Implement one-shot transport and crash recovery.** Call the existing `agent_loop.OpenRouterGateway.request_pit_optimizer_v5_json_once` through a new study-bound adapter with `max_attempts=1`; the existing `OpenRouterOneShotJsonCompletionV5` cannot be constructed with this ledger. Preserve actual wire bytes from `wire_role_messages_v5`/`wire_role_schema_v5`, the response text including whitespace, canonical parsed response separately, returned model and usage receipt. Provider defaults in that method (`stream=False`, required parameters, excluded private reasoning, no configured seed) are pinned in the transport settings identity. Unit tests patch this low-level method; never load credentials or make a real request.

```python
reservation = ledger.reserve(request)
result = gateway.invoke_json_once(
    request_sha256=request.sha256, model=request.model,
    messages=request.messages, response_schema_json=request.schema_json,
    max_output_tokens=request.max_output_tokens,
    automatic_retries=0, schema_repair_calls=0,
    deadline_monotonic=deadline_monotonic,
)
# Persist result first, parse second, settle on both parse success and failure.
# An exception with unknown usage remains pending; never record invented zero use.
```

- [ ] **Complete reconciliation rules.** A durable provider response can be parsed/settled locally once after a crash. A response missing usage or an attempt with uncertain dispatch stays pending until an explicit authenticated usage reconciliation is supplied; do not free a slot on timeout. Persist a separate reconciliation event, never edit reservation/response bytes. Failed parsing or provider rejection cannot yield an import, even if terminal accounting is complete. Counters count external attempts once in the live ledger, never from fixture replay.
- [ ] **Run GREEN:** both Task 3 test files pass, including a patched transport assertion of one low-level call, no retry/repair, exact L schema bytes, and no secret lookup during construction/recovery. Freeze a legacy request/schema golden in tests to demonstrate this work changes neither.
- [ ] **Commit:** stage the three new modules and two tests; commit `feat: admit and account for isolated V5 study calls`.

### Task 4: Authenticate translation and replay the exact investigator import

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/imports.py`
- Test: `tests/test_pit_optimizer_v5_study_imports.py`

**Interfaces:**
- `StudyImportV1`: immutable study/arm/mode/preflight/terminal references; separate live and fixture call identities; F, L, original response, parsed envelope, translated T, ordinary artifact and draft refs/hashes; schema/parser/translator versions. No eventual round-intent field.
- `create_study_import_v1(*, store: StudyStoreV1, ledger: StudyLedgerV1, preflight: FixturePreflightV1, terminal: AuthenticatedStudyTerminalV1) -> StudyImportV1`.
- `authenticate_study_import_v1(*, store: StudyStoreV1, ledger: StudyLedgerV1, record: StudyImportV1, fixture_repository: LocalArtifactRepositoryV5, request: RoleRequestV5, call: RoleCallKeyV5) -> bytes` returns only authenticated T bytes.
- `StudyImportInvokerV1` implements `RecoverableRoleInvokerV5` with the existing `invoke_once(FreshPersistedRoleRequestV5, *, deadline_monotonic: float) -> RoleInvocationPackageV5` and `reconcile_once(ExistingPersistedRoleRequestV5) -> RoleInvocationPackageV5 | RoleReconciliationFailureV5` signatures. Its constructor receives exact fixture manifest, import/store/ledger and a scripted-role delegate. The delegate is a `RecoverableRoleInvokerV5` for author/critic; no live transport is accepted by the import invoker.
- `verify_imported_package_v1(*, package: RoleInvocationPackageV5, record: StudyImportV1, store: StudyStoreV1, ledger: StudyLedgerV1, fixture_repository: LocalArtifactRepositoryV5) -> None` is also used when the runtime returns an already-completed package without invoking the invoker.

- [ ] **Write failing provenance/recovery tests.** Parameterize tampering of root/manifest/ledger/audit/grant identity, role position/kind/index, F/L schema, raw response, authorization hash, receipt sequence/cumulative usage, translated artifact and arm. Include missing, failed and pending terminals; stale preflight checkpoint; a live import backed only by offline receipts; identical evidence ID string resolving to a different value; interrupted import followed by idempotent recovery. Require no gateway calls during fixture replay.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_imports.py -q`; expect missing import/invoker or a failing provenance assertion.
- [ ] **Implement lossless ordinary-artifact translation.** Reload the full verified terminal chain, reparse the raw response against L/F, then encode T using the returned exact binding and artifact. The only removed fields are the study wrapper/drafts/version. Check the ordinary artifact bytes against the legacy parser result; save T separately from raw response. Do not normalize ranks, citations, parameter choices or claims. Invalid responses retain failure/usage without a T/import.

```python
assert live_request.fixture_request_sha256 == fixture_request.sha256
assert terminal.request_sha256 == live_request.sha256
assert fixture_call.request_sha256 == fixture_request.sha256
translated = canonical_json_bytes_v5({
    "binding": fixture_request.expected_binding.to_primitive(),
    "artifact": response.artifact,
})
artifact = parse_and_bind_role_artifact(
    request=fixture_request, response_text=translated.decode("utf-8")
)
assert artifact == response.artifact
```

- [ ] **Implement exact fixture replay and independent package verification.** Require F/call/root equality against the actual persisted request. Replay T with `FixtureRoleRunnerV5(responses={"investigator": (T_text,)})`; `campaign_fixture=True` is mutually exclusive with supplied responses and must not be set. Construct zero-external-attempt `FixtureRoleTerminalAuthorityV5` using the same package fields as `FixtureRoleInvokerV5._invoke`. Recovery reloads the same authenticated import, never `FixtureRoleInvokerV5.reconcile_once`'s default canned response. For an existing package, verify its response/artifact hashes, zero fixture usage, terminal/call bindings and full live import chain. Mismatch aborts and preserves the paid record; no retranslation against a new F and no new slot.
- [ ] **Run GREEN:** Task 4 tests pass. Retain a negative test that stock `verify_fixture_run_v5` rejects non-canned T; do not patch or suppress that check. Mixed fixture/ledger authority remains rejected by the unchanged runtime manifest discriminator.
- [ ] **Commit:** stage the module and test; commit `feat: authenticate study investigator imports into fixture rounds`.

### Task 5: Compile the selected draft and verify the entire case pattern

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/compiler.py`
- Create: `core/pit_optimizer_v5/two_round_study/contrast.py`
- Test: `tests/test_pit_optimizer_v5_study_compiler.py`
- Test: `tests/test_pit_optimizer_v5_study_contrast.py`

**Interfaces:**
- `StudyDraftBindingV1`: import/draft refs, exact selected hypothesis hash, fixture role-package ref, persisted `RoundIntentPayloadV5` ref/hash, parent revision/source refs and checkpoint authority. Create-only, separate from the earlier import.
- `StudyContrastV1`: draft-binding ref/hash, parent identity, corpus/spec hashes and deterministic precommitment ID, ordered case IDs/input identities, expected boolean pattern, rival patterns. The later mechanism precommitment index references the same spec/corpus; avoid a circular hash by recording its reference in a subsequent create-only link, not changing the contrast.
- `CompiledStudyExperimentV1`: `MechanismExperimentSpecV1`, `MechanismObservationCorpusV1`, `StudyContrastV1`, `StudyDraftBindingV1` and selected configuration ID.
- `compile_study_experiment_v1(*, store: StudyStoreV1, imported: StudyImportV1, request: RoleRequestV5, round_intent: RoundIntentPayloadV5, parent: ParentCandidateV5, seed_snapshot: ExitSnapshotV3, registry: FrozenBehaviorRegistryV1) -> CompiledStudyExperimentV1`. Caller first authenticates the import/package and saved intent; compiler reloads the referenced persisted draft and checks every binding, not a supplied replacement draft.
- `CaseContrastResultV1`: contract/run refs, `status`, complete observed pattern or null, mismatched case IDs, missing case IDs and limitations. `evaluate_case_contrast_v1(*, contrast: StudyContrastV1, run: MechanismObservationRunV1) -> CaseContrastResultV1`.
- Pure helper `compare_case_patterns_v1(*, expected: tuple[bool, ...], observed: tuple[bool, ...] | None) -> tuple[str, tuple[int, ...]]` checks exact length and strict booleans; returns mismatch indices for a complete observation, or unavailable with no observed pattern.

- [ ] **Write failing compiler/contrast tests.** Reject forged hypothesis/parent/intent, foreign request citations, out-of-catalog configuration, evaluator metric, relaxed controls, fewer than two applicable cases, no negative case, duplicate Decimal inputs, distinct Decimal strings collapsing to the same snapshot, unsupported predicate/observable, identical claimed/rival patterns and a threshold claim missing inert/always-on rivals. Test immutable precommitment and parameter changes after authoring. Include:

```python
@pytest.mark.parametrize("observed,status,mismatches", [
    ((False, True, True, False), "matched_on_cases", ()),
    ((False, False, False, False), "contradicted_on_cases", (1, 2)),
    ((True, True, True, True), "contradicted_on_cases", (0, 3)),
    (None, "unavailable", ()),
])
def test_all_case_pattern(observed, status, mismatches):
    assert compare_case_patterns_v1(
        expected=(False, True, True, False), observed=observed
    ) == (status, mismatches)
```

- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_compiler.py tests/test_pit_optimizer_v5_study_contrast.py -q`; expect missing compiler/contrast or failed exact-case assertions.
- [ ] **Implement compilation from authoritative fields.** Resolve the selected hypothesis from the ordinary imported artifact using the saved intent, require exact equality, and resolve each cited ID against F. Match configuration's registered parent to the actual selected parent. Derive parent/spec/intent identities in controller code. Build the existing corpus with `build_mechanism_observation_corpus_v1`, validate uniqueness after snapshot conversion, and count applicability from those snapshots. Require exactly the two local metrics and protected-next-stop control in the global constraints. The compiler does not reject an allowed configuration just because it will later fail equivalence; it preserves that honest runtime outcome.
- [ ] **Freeze the contrast independently of outcomes.** Persist the draft-binding, spec/corpus and contrast before the author request exists. Expected and rival patterns come from the model draft; do not derive/repair the expected pattern from the registry's actual candidate decisions. Require one differing case for each rival. For threshold claims, require the named inert/all-false and always-on/all-true rivals; allow human semantic assessment to reject a misleading claim even when typed patterns parse. Precommitment verification checks journal order and immutable refs, not a boolean flag or wall-clock timestamp alone.
- [ ] **Implement raw-decision verification.** Reauthenticate the run's corpus/binding; require completed execution and the full ordered case set, with matching identities and repeated observations. Derive each actual boolean by comparing canonical parent/candidate decision bytes, including nonapplicable and missing cases. Report exact mismatch IDs. Never write the contrast result into `MechanismEvidenceReportV1` or change its aggregate assessment.

```python
if run.execution.status != "completed" or observed_case_ids != frozen_case_ids:
    status, mismatch_indices = "unavailable", ()
else:
    observed = tuple(parent_bytes != candidate_bytes
                     for parent_bytes, candidate_bytes in canonical_pairs)
    status, mismatch_indices = compare_case_patterns_v1(
        expected=contrast.expected_changed, observed=observed
    )
```

- [ ] **Run GREEN:** both Task 5 files pass. Add a reducer-level test with `.49/.50/.51/None`: threshold and always-on produce the same supported `2/2` relevant-case production metric and unchanged next-stop controls, but only threshold passes the study pattern. Run failure/partial coverage must be unavailable, never an all-false fabricated observation.
- [ ] **Commit:** stage the two modules and two tests; commit `feat: precommit and verify V5 study case contrasts`.

### Task 6: Compose fixture runtime ports and explicit critic routing

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/runtime_ports.py`
- Test: `tests/test_pit_optimizer_v5_study_runtime_ports.py`

**Interfaces:**
- `StudyRequestRouterV1(arm, base: LocalRoleRequestFactoryV5, enabled: LocalRoleRequestFactoryV5)` implements the three exact `RoleRequestFactoryV5` methods and `critic_request_with_mechanism(inputs, projection, decision, candidates, *, mechanism_evidence: tuple[object, ...]) -> RoleRequestV5`.
- `StudyScriptedRoleInvokerV1` implements `RecoverableRoleInvokerV5`; `scripted_response_v1(*, request: RoleRequestV5, round_index: int, registry: FrozenBehaviorRegistryV1, selected: CompiledStudyExperimentV1 | None) -> str` constructs deterministic request-bound seed-investigator/author/critic responses. Round-two investigator is rejected here; it must come from the import invoker.
- `StudyCandidateRuntimeV1` implements `CandidateRuntimeV5` and `OwnedCleanupV5` with the existing method signatures; it uses only frozen source/registry/evaluator inputs. `study_workers_v1(*, bound: MechanismBoundCandidateV1, registry: FrozenBehaviorRegistryV1) -> tuple[MechanismWorkerPortV1, MechanismWorkerPortV1]` builds registered `SyntheticFixtureWorkerV1` tables from the same decision function.
- `StudyMechanismComposerV1` implements `MechanismRuntimeExtensionV5`, plus `role_request_evidence(experiment_id: str) -> tuple[MechanismExtensionCapabilityV1, MechanismBoundCandidateV1, MechanismObservationRunV1, MechanismEvidenceReportV1, RoundIntentPayloadV5]`. Before authoring it authenticates the actual saved role/intent, creates the new capability/extension and delegates subsequent methods. Round one uses its pinned seed draft; round two requires an authenticated import and selected-draft binding. It never mutates a prior capability.
- `compose_study_round_v1(*, fixture: StudyFixtureV1, round_index: int, arm: StudyArmV1, store: StudyStoreV1, ledger: StudyLedgerV1 | None, imported: StudyImportV1 | None) -> tuple[FeedbackRoundInputV5, FeedbackRoundDependenciesV5]`. Round one permits no import/ledger; round two requires the pair. Its `requests` member is the router itself.

- [ ] **Write failing routing and authority tests.** Assert primary I/A use the enabled factory, withheld I/A use base, both current critics use enabled plus their exact current bundles, wrong/cross-arm bundles reject, and author remains `AuthorRoleInputV5`. Spy on the final guard to require one admission per completed request, not one for base plus another for augmented. Test that a primary-only mechanism citation is rejected through the withheld author path. No fallback after an enabled critic failure.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_runtime_ports.py -q`; expect missing ports or the explicit route/admission failures.
- [ ] **Implement only delegation in the request router.** Factories share the same repository object, authenticated fixture manifest and output cap. Do not rebuild requests, edit evidence IDs, suppress augmented critic dispatch or attach a mechanism wrapper to author input.

```python
def investigator_request(self, inputs, projection, parent):
    factory = self.enabled if self.arm == "primary" else self.base
    return factory.investigator_request(inputs, projection, parent)

def critic_request_with_mechanism(self, inputs, projection, decision, candidates,
                                  *, mechanism_evidence):
    return self.enabled.critic_request_with_mechanism(
        inputs, projection, decision, candidates,
        mechanism_evidence=mechanism_evidence,
    )
```

- [ ] **Implement scripted roles and candidate ports.** Seed hypothesis predicts increased local decision changes and cites current issued evidence. Round-one author yields the registered A/S configurations through the normal source-template renderer; round-two author materializes only the generated selected configuration. Critics use truthful synthetic portfolio findings and deliberately omit mechanism findings in round-one prose/citations. On recovery, regenerate scripted expectations from the exact request and frozen response-function hash. Candidate validation checks materialized source/revision/configuration match; fingerprint uses the registry client; evaluator uses frozen inputs and existing arithmetic; leases/cleanup follow the fixture ownership convention. No `exec`, source import, Git materialization or external evaluator.
- [ ] **Implement late-bound precommitment.** In `before_authoring`, locate and authenticate the saved `RoundIntentPayloadV5` and investigator package from the actual journal. In round one, construct the pinned seed `MechanismExperimentSpecV1` directly from the global constants and actual seed hypothesis/intent/P0 identities; its authority is the authenticated scripted package and fixture opt-in, not a fabricated live import. In round two, verify the import even when `_Runtime._role` reused its stored completion, then compile Task 5's selected draft. For archived P1, load the checkpoint-authorized stored record, source bundle/revision and campaign evidence; never substitute baseline P0 or target A. Construct a fresh `MechanismExtensionCapabilityV1` and `MechanismRuntimeExtensionV1`, call its `before_authoring`, then save the round-two contrast/precommitment link before returning. Delegate `observe_candidate`, `finalize_report` and `role_request_evidence` unchanged. Reopen repeats authentication and reuses exact create-only records.
- [ ] **Run GREEN:** Task 6 tests pass, including full round-one and valid withheld round-two runtime calls in fresh synthetic repositories, then a reopen. Assert withheld investigator lacks historical mechanism rows; current critic receives this arm's own candidate/report bundles; primary current reports never enter withheld. Equivalent candidates open no supplemental workers. Retain prerequisite unavailable/deadline behavior rather than bypassing failed evidence.
- [ ] **Commit:** stage the module and test; commit `feat: compose V5 study runtime ports and critic routing`.

### Task 7: Drive two real rounds across restart and compare isolated arms

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/driver.py`
- Create: `core/pit_optimizer_v5/two_round_study/comparison.py`
- Test: `tests/test_pit_optimizer_v5_study_driver.py`
- Test: `tests/test_pit_optimizer_v5_study_comparison.py`

**Interfaces:**
- `PreparedStudyV1`: common round-one snapshot/checkpoint, two fixture-root identities/manifest refs, registry/rubric refs, both `FixturePreflightV1`/F/L refs, immutable `StudyManifestV1` and a request-comparison ref. Preparation contains no live attempt or grant.
- `StudyArmResultV1`: arm/mode, imported response/draft or terminal failure refs, selected parent, `FeedbackRoundResultV5` or explicit not-started reason, recovered checkpoint refs, production report/contrast refs and state (`completed|incomplete|rejected`). It carries no combined success boolean.
- `prepare_two_round_study_v1(*, root: Path, mode: StudyModeV1, provider_settings: Mapping[str, object] | None) -> PreparedStudyV1`: run seeded round one, close/reopen, create both descendants, prepare requests and preregistration. Require a fresh root and explicit mode; no provider gateway parameter.
- `execute_study_arm_v1(*, prepared: PreparedStudyV1, arm: StudyArmV1, ledger: StudyLedgerV1, gateway: OneShotJsonCompletionV5) -> StudyArmResultV1`: one admitted attempt/import, real round two, reopen. Refuse an unverified grant or a gateway mode inconsistent with the study.
- `resume_study_arm_v1(*, prepared: PreparedStudyV1, arm: StudyArmV1, ledger: StudyLedgerV1) -> StudyArmResultV1`: disk recovery/import replay only; cannot call a provider.
- `RequestComparisonV1`: base-input equality, both complete F/L hashes and byte/row/projection/retention/usage-bound counts, exact allowed/actual differences, leak-check observations, confounds and eligible/inconclusive status. `compare_study_requests_v1(*, primary: FixturePreflightV1, withheld: FixturePreflightV1, store: StudyStoreV1) -> RequestComparisonV1`.

- [ ] **Write failing two-round tests with deterministic admitted mock responses.** Use Task 2 fixtures and Task 3's fake gateway, explicitly labeled offline. Primary response acknowledges the local contradiction and selects a registered discriminating experiment; withheld supplies a valid independently authored base-evidence response. Assert actual `run_feedback_round_v5` invocations and durable role/evaluation/publication events rather than a manually assembled history. Include invalid proposal, equivalent proposal, unavailable worker, second contradiction and equal primary/withheld revisions.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_driver.py tests/test_pit_optimizer_v5_study_comparison.py -q`; expected missing driver or exact persistence/comparison failures.
- [ ] **Implement round one and its hard preparation gates.** Run the real runtime with scripted seed and registry A/S. Require published A and S records, actual `P0 < A < S` results, A's exact `0/3` contradiction and `4/4` controls, and no contradiction in critic prose/citations. Reconstruct the next request through real recovery/selection/memory; require A actually in `memory.summaries`. Stop setup if any gate fails; adjust only the documented pre-study fixture, with tests and review, before a live manifest exists.
- [ ] **Implement a genuine restart and descendants.** Finish all workers/leases, discard repository/factory/extension/runtime instances, and reconstruct from disk in a fresh process for the integration regression. `LocalArtifactRepositoryV5` has no `close()` API; do not invent one. Copy only the verified closed round-one artifact snapshot into two new empty descendant directories, record their distinct root identities and identical common-ancestor bytes/checkpoint hashes, then independently authenticate/recover them. Do not copy a live ledger, mutable process object, response from the other arm, or any saved real campaign. No deletion or overwrite of a pre-existing root.
- [ ] **Build exact preflight requests without faking completion.** Recover projection with `LocalArchiveReducerFactoryV5`, select parent with `select_parent_v5`, then call the actual arm router. Construct the exact investigator `RoleCallKeyV5` (`round_index=2`, position `1`, primary attempt `1`, F hash). Store F, schema, evidence map, selected parent, checkpoint and snapshot refs. Rebuild F after reopening and require byte equality. Preserve P0→A as historical evidence and P1→B as the next comparison, even when P1 is S. Before freezing the live manifest, verify the catalog has at least one genuine fixed-suite-distinct discriminating configuration for this actual P1; if no such configuration exists, fail setup without substituting another parent. Keep equivalent options in the catalog and preserve their rejection if selected.
- [ ] **Preregister both requests, rubric and comparison before either live attempt.** Normalize only the known input wrapper to compare the underlying base history; list every remaining difference rather than silently deleting it. Assert same base history, parent, task wording, allowed candidate domain, model/settings, schema shape and caps. Report evidence-row/ID changes and wrapper/length confounds. Audit both exact live prompts for leaked contradiction text through critic, labels, summaries or answer examples; do not include the rubric's expected answer in model instructions. If independently visible source/results explicitly reveal the contradiction, mark feedback attribution inconclusive; do not falsify ordinary history to hide it.
- [ ] **Run the imported second round and reopen.** After authenticated import, inject the Task 6 ports and invoke the real runtime. Require actual selected intent, before-authoring precommitment, materialization, admission, observation/evaluation when admitted, current critic, publication and checkpoint. Reopen and verify package/import provenance before any resume. Preserve original raw invalid answers and accurate incomplete/rejected outcomes. A contradicted experiment can be completed; it cannot become evidence of improvement.

```python
inputs, dependencies = compose_study_round_v1(
    fixture=fixture, round_index=2, arm=arm,
    store=store, ledger=ledger, imported=imported,
)
result = run_feedback_round_v5(inputs=inputs, dependencies=dependencies)
# Verification reloads the checkpoint and all referenced evidence from disk;
# the result object alone does not establish two-round trace integrity.
```

- [ ] **Run GREEN:** both Task 7 files pass. Fresh-process restart must preserve primary contradiction delivery despite summary/critic omission; withheld completes/reopens via enabled current critic; replay adds zero external calls; shared ledger retains both attempts once. An equivalent selection causes a genuine runtime rejection and no supplemental worker call. No test asserts that withheld must fail or primary must obtain a favorable experiment outcome.
- [ ] **Commit:** stage the two modules and two tests; commit `feat: run linked V5 study rounds with an isolated ablation`.

### Task 8: Verify, export and document distinct scientific claims

**Files:**
- Create: `core/pit_optimizer_v5/two_round_study/verification.py`
- Create: `core/pit_optimizer_v5/two_round_study/trace.py`
- Create: `core/pit_optimizer_v5/two_round_study/__main__.py`
- Create: `docs/examples/v5-two-round-study.md`
- Test: `tests/test_pit_optimizer_v5_study_trace.py`
- Test: `tests/test_pit_optimizer_v5_study_cli.py`

**Interfaces:**
- `StudyVerificationV1`: immutable gate-level measurements, errors, warnings/confounds and exact artifact refs; includes `StudyVerdictsV1`, no opaque score.
- `HumanEvidenceReviewV1`: reviewer identifier, frozen rubric hash, exact arm response/draft refs, separate evidence-interpretation/revision-quality/claim-pattern judgments with reasons and artifact citations. User-supplied semantic review, never inferred from fixture completion.
- `verify_study_v1(*, prepared: PreparedStudyV1, store: StudyStoreV1, ledger: StudyLedgerV1 | None, human_review: HumanEvidenceReviewV1 | None = None) -> StudyVerificationV1` authenticates stored evidence without provider/candidate execution or repair.
- `export_study_trace_v1(*, prepared: PreparedStudyV1, verification: StudyVerificationV1, store: StudyStoreV1, output: Path) -> ArtifactRefV5` produces a create-only self-contained bundle and returns its index reference. Export metadata/relative references use a separate export store rooted at the new output directory; it is not the original live authority root.
- CLI commands: `python -m core.pit_optimizer_v5.two_round_study offline --root PATH`, `... verify --root PATH`, `... export --root PATH --output PATH`. `offline` uses explicit mock admission and labels evidence use not assessed. No default live command or implicit provider setup; the documented live Python API requires a supplied current grant and study gateway.

- [ ] **Write failing verifier/export/CLI tests.** Reject changed raw observation, spec/corpus/precommitment, registry/fingerprint vector, orphan import, modified existing runtime package, wrong checkpoint/parent, duplicate cost attribution, swapped arm report and post-author contrast. Verify exact original bytes/hashes in exported artifacts; derivatives get separate hashes. Test CLI on a fresh temporary root with secret lookup/network entry points patched to raise.
- [ ] **Run RED:** `py -3.13 -m pytest tests/test_pit_optimizer_v5_study_trace.py tests/test_pit_optimizer_v5_study_cli.py -q`; expect missing verifier/export/entry point or the particular violated gate.
- [ ] **Implement independent trace verification.** Reauthenticate the repository graphs, all study ledger records and import links, real journal ordering, publication/checkpoints, actual selected parent, summary delivery, citation resolution, fixed/supplemental consistency, all-case control/contrast and shared spend. Recompute derived assertions; do not trust a previously serialized success field. For scripted roles, regenerate the pinned scripted response; for round-two investigator, match the authenticated T/import. Retain the stock fixture verifier's negative case; this study verifier is explicit additional authority, not a bypass of its mismatch.
- [ ] **Implement the three claim axes and human rubric.** A live generated answer needs a human review for evidence interpretation and revision quality: accurately explain `0/3` despite higher synthetic return, retain finite-case/synthetic limitations, justify a changed claim/test, respect actual P1, and match the typed contrast. Citation presence alone cannot pass. Automated checks can establish delivery and typed discrimination, not semantic evidence use. Without a human review, live evidence use remains `not_assessed`; any automated grader is advisory with saved prompt/output. Offline mode always leaves model evidence use `not_assessed`. Paired attribution records the observed difference and confounds; equal, invalid, leaked or nondiscriminating pairs are inconclusive. Even a favorable pair does not establish statistical reliability or optimization improvement.

```python
assert verification.verdicts.optimization_improvement == "not_established"
assert verification.verdicts.authored_code_execution == "not_established"
if prepared.manifest.mode == "offline_fixture":
    assert verification.verdicts.evidence_use == "not_assessed"
# experiment_completion="verified" can coexist with
# case_contrast="contradicted_on_cases" and evidence_use="supported".
```

- [ ] **Export exact bytes and a readable trace.** Create `study-manifest.json`, `artifact-index.json`, `live-study-calls/`, `imports/`, `behavior-registry/`, `round-1/`, `round-2-primary/`, `round-2-withheld/`, `request-comparison.json`, `case-contrasts/`, `rubric-results.json` and `trace.md`. Index each artifact's content hash/type/upstream refs, including failures. Explain root-bound original authority separately from portable exported byte verification. Preserve canonical bytes and raw provider text; redact only into explicitly separate derivative artifacts, never under an original hash. Export through an allowlist of referenced study artifacts, not directory-wide copying or credential scanning.
- [ ] **Make the trace readable without hiding failures.** Show exact generated proposals side by side, original P0→A versus actual P1→B, original claim and frozen spec, raw decisions and controls, unchanged production aggregate versus study contrast, candidate admission, request-scoped citations, F/L sizes and schema bounds, actual usage versus zero-cost replay, recovery and limitations. Label scripted, synthetic, live-generated, imported, unavailable and human-reviewed values. Never title this result a fully working self-recursive optimizer.
- [ ] **Document and test the offline CLI.** It creates fresh local synthetic repositories and artifacts, prints separate verdicts, and refuses occupied roots. `verify` and `export` are read-only with respect to the input study, no reconciliation transport. Document the explicit live API sequence (`prepare` → freeze/inspect both requests → authorize a concrete grant → construct gateway → execute each arm → human review → verify/export), provider-internal retry disclosure, interruption/reconciliation, and exact limits requiring authorization. No `.env` access or saved grant reuse.
- [ ] **Run GREEN:** Task 8 tests pass, including one CLI-created offline trace reopened by `verify` and exported byte-for-byte. Run the final focused suite and lint below once; after success, broaden/repeat only for a new change or identified concern.
- [ ] **Commit:** stage the three modules, example documentation and two tests; commit `feat: verify and export separate V5 study verdicts`.

## Verification commands and implementation gates

Before implementation changes, run:

```powershell
py -3.13 -m pytest tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_artifacts.py -q
```

Expected prerequisite: the existing 93 focused tests pass (the historical run had two known warnings). Stop and investigate regressions before proceeding; do not update expected findings to hide a failure.

After Task 8, collect the exact new test files through PowerShell and run them with the existing focused suite:

```powershell
$studyTests = @(Get-ChildItem -LiteralPath tests -Filter 'test_pit_optimizer_v5_study_*.py' | ForEach-Object { $_.FullName })
py -3.13 -m pytest @studyTests tests/test_pit_optimizer_v5_mechanism_evidence.py tests/test_pit_optimizer_v5_mechanism_artifacts.py -q
py -3.13 -m ruff check core/pit_optimizer_v5/two_round_study core/pit_optimizer_v5/artifacts.py @studyTests
git diff --check
```

The existing `test_pit_optimizer_v5_mechanism_artifacts.py` coverage exercises binary adapter state, corruption and orphan-run handling; Task 1 adds enumeration coverage in its new study-contract test file without changing existing expectations. Record exact selectors/results in the task closeout. Do not run market/evaluator integration campaigns under the label of unit verification. Normal commit hooks remain enabled.

The four reviewed risks have mandatory regression gates:

| Risk | Tasks and decisive verification |
| --- | --- |
| Mixed authority and unauthenticated import | 1, 3, 4, 8: strict separate schema/admission; complete chain and usage; tamper/cross-arm/recovery rejection; stock verifier still rejects arbitrary T |
| Withheld critic routing | 6, 7: actual dependency router, base I/A, enabled current critic, exact own bundles, one final guard, completed and reopened arm |
| Aggregate support mistaken for specific contrast | 5, 8: threshold and always-on both support 2/2; negative/missing cases contradict always-on; failed observations unavailable |
| Invented fixed-suite distinction | 2, 6, 8: recomputed shared-function outputs, full snapshot overlap, honest A fixed divergence, genuine parent/sibling rejection without workers |

## Spec coverage and acceptance handoff

| Approved design requirement | Plan coverage |
| --- | --- |
| Sections 1–2: bounded study, missing components, no authored-code execution | Global constraints, file map, Tasks 1–8 |
| Section 3: seeded contradiction, return ordering, actual summary, critic omission | Tasks 2, 6, 7 |
| Section 4: restart, actual parent, scoped IDs, request sizes/admission | Tasks 3, 4, 7, 8 |
| Section 5.1: F/L/T chain, independent shared ledger, authenticated replay/recovery | Tasks 1, 3, 4, 6, 8 |
| Section 5.2: generated draft, selected intent, fresh spec and all-case contrast | Tasks 1, 5, 6, 8 |
| Section 6: frozen registry, fixed/supplemental agreement, honest equivalence and second round | Tasks 2, 5, 6, 7 |
| Section 7: isolated ablation, explicit critic route, own citations, leak/confound reporting | Tasks 3, 6, 7, 8 |
| Section 8: independent rubric gates, human semantics, no combined success claim | Tasks 1, 5, 8 |
| Section 9: self-contained exact-byte trace and actual spend | Tasks 3, 4, 8 |
| Sections 10–12: prerequisite fixes, offline verification, separate live grant, design/implementation/execution distinction | Global constraints, Task 8 documentation, verification commands and gates below |

Implementation is complete only after focused tests, task reviews and an independently reopened offline trace pass. Report that result as **implemented and verified offline**, with real-model evidence use **not assessed**, experiment outcomes labeled synthetic, and optimization improvement **not established**.

Before any live study, present the concrete prepared requests/schema and comparison, frozen registry/rubric, selected provider/model/settings, exact two-call/token/cost bounds and persistence scope for the user's separate execution grant. This is the execution gate required by the approved design, not another request to approve the implementation workflow. Do not call a provider while waiting. If the live study later runs, report its actual separate verdicts, including failed/incomplete arms, without changing the preregistration or retrying for a preferred result.

Planning self-review: all design sections are mapped above. The author checked interface names, persisted identity domains, immutable chronology, strict schema projection and claim-axis terminology, and corrected the static fixture-runner construction to omit the mutually exclusive `campaign_fixture=True` option. A bounded Luna/max read-only source map confirmed that legacy paid-call adapters cannot admit L unchanged and identified safe adapter storage/locking seams. No implementation, tests or live calls occurred as part of planning.
