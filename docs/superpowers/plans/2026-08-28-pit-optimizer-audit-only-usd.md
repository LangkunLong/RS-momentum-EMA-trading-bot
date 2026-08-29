# PIT Optimizer Audit-Only USD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove USD as an enforcing limit from the PIT optimizer while retaining exact call/token authorization, complete cost accounting, and a provider-free-to-live six-call subset proof.

**Architecture:** Migrate the active PIT optimizer authority graph to schema v3. Schema v3 omits every enforcing USD field, records pricing only as an advisory snapshot, and uses calls plus tokens for reservations and continuation; provider-reported cost remains mandatory audit evidence. The discovery canary runs two investigator/author/critic iterations over the existing two-fold, 25-symbol subset and stops before hidden validation.

**Tech Stack:** Python 3 dataclasses, canonical JSON/hash-chain records, pytest, OpenRouter chat completions, Git source identity, and network-disabled Docker policy workers.

**Spec:** `docs/superpowers/specs/2026-08-28-pit-optimizer-audit-only-usd-design.md`

## Global Constraints

- Active PIT optimizer manifests, readiness, authorization records, run artifacts, and public results use `schema_version=3`.
- Existing schema-v2 authorization ledgers remain verifiable as read-only history and cannot be resumed or appended.
- The subset contract is exactly six role calls, two iterations, and at most 448,000 input-plus-output tokens.
- The current role envelopes remain investigator `86,000 + 16,000`, author `72,000 + 14,000`, and critic `32,000 + 4,000` tokens per iteration, with their current byte/response bounds.
- `provider_retries=0`, `apply=false`, source transmission is separately authorized, and provider response healing/retry remains disabled.
- No `max_usd`, `additional_usd`, `reserved_usd`, or USD exhaustion comparison is permitted in the schema-v3 authority path. Do not use infinity, a large sentinel, or a nullable USD ceiling.
- Pricing lookup is best-effort. Available rates produce an advisory projection; unavailable rates do not block a role call.
- Every received role response must still contain complete, finite, non-negative provider token and cost accounting. Missing or conflicting accounting is terminal and fail-closed.
- The discovery canary uses the existing 25-symbol universe and two 60-session discovery folds. It must not open the later 60-session hidden fold or start the full replay.
- Candidate workers remain disposable, network-disabled, resource-bounded, and separate from the unchanged source checkout.
- Legacy diagnosis, legacy `pit_optimization`, proposal-batch, and generic agent-loop USD limits remain unchanged.
- Keep verification focused. Do not run the full repository suite or a full replay as part of this change.
- Keep all evidence local under ignored `.artifacts`; do not push, upload source/data, or make a provider call during Tasks 1-5.

## File Structure

- `core/pit_optimization_contract.py`: active schema-v3 manifest, call envelope, gate config, canonical parsing, and exact six-call profile.
- `core/pit_optimizer_evaluation.py`: provider-free manifest CLI without per-role USD arguments.
- `core/pit_optimizer_authorization.py`: schema-v3 grant/window/lease/reservation records, advisory pricing snapshot, call/token reconciliation, and read-only schema-v2 history reader.
- `agent_loop.py`: optimizer-only resource ledger, best-effort pricing, one-shot OpenRouter boundary, CLI routing, and closed summary projection.
- `core/pit_optimizer_controller.py`: schema-v3 run artifacts, call/token stop conditions, actual-cost summaries, and discovery-only termination.
- `core/pit_optimizer_artifacts.py`: schema-v3 artifact admission.
- `core/pit_optimization.py`: schema-v3 readiness construction/loading while legacy `pit_optimization` remains schema v1.
- `tests/test_pit_optimizer_v2.py`: migrate the existing optimizer contract/authorization/gateway fixtures to the active schema-v3 shapes; keep the filename to avoid a noisy file move.
- `tests/test_pit_optimizer_loop.py`: focused two-iteration feedback, accounting, hidden-isolation, and production-composition proof.
- `tests/test_agent_loop.py`: CLI and public-summary coexistence tests.

The test migration keeps three shared helper contracts stable across tasks:
`_v3_manifest() -> PitOptimizerRunManifest`,
`_authorized_v3_ledger(tmp_path, manifest) -> tuple[AuthorizationLedger, Path,
OperatorAuthorizationWindow]`, and
`_write_minimal_legacy_v2_history(tmp_path) -> Path`.

These are direct schema-v3 replacements for the existing `_v2_manifest` and
`_task6_authorized_ledger` builders. `_v3_manifest` uses the exact role profile
listed in Task 1. `_authorized_v3_ledger` records one six-call/448,000-token
grant and its matching window. `_write_minimal_legacy_v2_history` writes one
canonical, correctly hash-chained schema-v2 grant record for read-only parsing;
it never constructs an active v3 ledger.

---

### Task 1: Seal the Schema-v3 Call/Token Manifest

**Files:**

- Modify: `core/pit_optimization_contract.py:460-1000,1024-1085,1135-1560`
- Modify: `core/pit_optimizer_evaluation.py:1224-1355`
- Modify: `tests/test_pit_optimizer_v2.py:1335-1665,2250-2760`

**Interfaces:**

- Consumes: the existing source/fold/parity identity graph and current role byte/token envelopes.
- Produces: `PitOptimizerCallBudget` without `max_usd`; `AuthorizationRequirement` without `max_usd`; `PitOptimizerRunManifest(schema_version=3)`; `PitOptimizerGateConfig` without a USD field; a provider-free manifest CLI with no `--*-max-usd` arguments.

- [ ] **Step 1: Write the manifest regression test that fails under schema v2.**

  Update the shared fixture to construct the current token profile without USD, then add:

  ```python
  def test_schema_v3_manifest_authorizes_calls_and_tokens_without_usd() -> None:
      manifest = _v3_manifest()

      assert manifest.schema_version == 3
      assert manifest.authorization_requirement.max_calls == 6
      assert manifest.authorization_requirement.max_tokens == 448_000
      assert manifest.authorization_requirement.provider_retries == 0
      assert manifest.authorization_requirement.apply is False
      assert "max_usd" not in asdict(manifest.authorization_requirement)
      assert all("max_usd" not in asdict(plan) for plan in manifest.call_budgets)
      assert sum(
          plan.max_input_tokens + plan.max_output_tokens
          for plan in manifest.call_budgets
      ) == 448_000
  ```

  The fixture's exact per-role tuple is:

  ```python
  role_caps = {
      "investigator": (8_000, 78_000, 86_000, 16_000, 8 * 1024),
      "author": (12_000, 48_500, 72_000, 14_000, 16 * 1024),
      "critic": (8_000, 24_000, 32_000, 4_000, 8 * 1024),
  }
  ```

- [ ] **Step 2: Run the contract node and verify RED.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py::test_schema_v3_manifest_authorizes_calls_and_tokens_without_usd
  ```

  Expected: construction or assertions fail because current contracts require schema 2 and positive USD caps.

- [ ] **Step 3: Implement the minimal schema-v3 manifest shapes and exact profile.**

  The active dataclass signatures become:

  ```python
  @dataclass(frozen=True, slots=True)
  class PitOptimizerCallBudget(_V2Canonical):
      call_index: int
      iteration: int
      role: str
      model: str
      max_static_input_bytes: int
      max_dynamic_input_bytes: int
      max_input_tokens: int
      max_output_tokens: int
      max_response_bytes: int


  @dataclass(frozen=True, slots=True)
  class AuthorizationRequirement(_V2Canonical):
      window_id: str
      max_calls: int
      max_tokens: int
      policy_source_scope_sha256: str
      provider_retries: int
      apply: bool
  ```

  `PitOptimizerRunManifest.__post_init__` must require schema 3, exact call order, six calls, and exactly 448,000 authorized tokens. Replace the historical USD-varying profiles in `_require_first_call_plan` with the single five-value role profile above. Remove USD parsing, comparison, and command rendering from `_pit_optimizer_manifest_from_primitive`, `build_subset_manifest`, `build_prepare_command`, `_manifest_cli_parser`, and `_call_budgets_from_namespace`.

- [ ] **Step 4: Add mutation coverage for the remaining hard gates.**

  ```python
  def test_schema_v3_manifest_still_rejects_call_and_token_expansion() -> None:
      manifest = _v3_manifest()
      with pytest.raises(ValueError, match="call"):
          replace(
              manifest,
              authorization_requirement=replace(
                  manifest.authorization_requirement,
                  max_calls=5,
              ),
          )
      with pytest.raises(ValueError, match="tokens"):
          replace(
              manifest,
              call_budgets=(
                  replace(
                      manifest.call_budgets[0],
                      max_output_tokens=manifest.call_budgets[0].max_output_tokens + 1,
                  ),
                  *manifest.call_budgets[1:],
              ),
          )


  def test_schema_v3_manifest_rejects_legacy_usd_keys() -> None:
      primitive = asdict(_v3_manifest())
      primitive["authorization_requirement"]["max_usd"] = 0.40
      with pytest.raises(ValueError, match="closed contract"):
          contract._pit_optimizer_manifest_from_primitive(primitive)
  ```

- [ ] **Step 5: Run the focused manifest/CLI slice.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "schema_v3_manifest or manifest_builder or build_subset_manifest_cli"
  ```

  Expected: PASS; emitted prepare command contains calls/tokens/iterations but no `--max-usd` or role USD option.

- [ ] **Step 6: Commit the manifest migration.**

  ```powershell
  git add core/pit_optimization_contract.py core/pit_optimizer_evaluation.py tests/test_pit_optimizer_v2.py
  git commit -m "Migrate PIT optimizer manifest to resource-only v3"
  ```

---

### Task 2: Replace USD Authority with Advisory Pricing Records

**Files:**

- Modify: `core/pit_optimizer_authorization.py:140-450,612-2215,2275-3735`
- Modify: `tests/test_pit_optimizer_v2.py:1950-4300,5140-5585,8600-8690`

**Interfaces:**

- Consumes: schema-v3 manifest and its exact call/token authorization requirement.
- Produces: `OptimizerPricingSnapshot`; USD-free grant/window/lease/reservation dataclasses; schema-v3 hash-chain records; `AuthorizationLedger.reserve_call(lease, plan, projected_call_usd=projection)`; actual-cost reconciliation; `read_legacy_authorization_history(path)`.

  ```python
  def _optimizer_pricing_digest(
      model: str,
      lookup_status: str,
      prompt: Decimal | None,
      completion: Decimal | None,
  ) -> str:
      primitive = {
          "model": model,
          "lookup_status": lookup_status,
          "prompt_per_million": (
              None if prompt is None else _canonical_decimal_text(prompt)
          ),
          "completion_per_million": (
              None if completion is None else _canonical_decimal_text(completion)
          ),
      }
      return hashlib.sha256(_canonical_json_bytes(primitive)).hexdigest()


  @dataclass(frozen=True, slots=True)
  class OptimizerPricingSnapshot:
      model: str
      lookup_status: str
      prompt_per_million: Decimal | None
      completion_per_million: Decimal | None
      pricing_payload_sha256: str

      @classmethod
      def available(
          cls,
          *,
          model: str,
          prompt: Decimal,
          completion: Decimal,
      ) -> "OptimizerPricingSnapshot":
          return cls(
              model=model,
              lookup_status="available",
              prompt_per_million=prompt,
              completion_per_million=completion,
              pricing_payload_sha256=_optimizer_pricing_digest(
                  model,
                  "available",
                  prompt,
                  completion,
              ),
          )

      @classmethod
      def unavailable(cls, *, model: str) -> "OptimizerPricingSnapshot":
          return cls(
              model=model,
              lookup_status="unavailable",
              prompt_per_million=None,
              completion_per_million=None,
              pricing_payload_sha256=_optimizer_pricing_digest(
                  model,
                  "unavailable",
                  None,
                  None,
              ),
          )

      def projected_call_usd(
          self,
          prompt_bytes: int,
          output_tokens: int,
      ) -> Decimal | None:
          if self.lookup_status == "unavailable":
              return None
          assert self.prompt_per_million is not None
          assert self.completion_per_million is not None
          return (
              Decimal(prompt_bytes) * self.prompt_per_million
              + Decimal(output_tokens) * self.completion_per_million
          ) / Decimal(1_000_000)
  ```

  Available snapshots require both finite non-negative rates; unavailable snapshots require both rates to be `None`. The digest covers model, status, and canonical decimal-rate strings.

- [ ] **Step 1: Write RED tests for USD-free authorization and large advisory projections.**

  ```python
  def test_schema_v3_authorization_ignores_advisory_cost_but_enforces_resources(
      tmp_path: Path,
  ) -> None:
      manifest = _v3_manifest()
      ledger, _ledger_path, window = _authorized_v3_ledger(tmp_path, manifest)
      pricing = OptimizerPricingSnapshot.available(
          model=manifest.model,
          prompt=Decimal("2000"),
          completion=Decimal("8000"),
      )
      projections = tuple(
          pricing.projected_call_usd(
              plan.max_input_tokens,
              plan.max_output_tokens,
          )
          for plan in manifest.call_budgets
      )
      assert all(value is not None for value in projections)
      projected = sum(
          (value for value in projections if value is not None),
          Decimal("0"),
      )
      lease = ledger.open_run_lease(
          window_id=window.window_id,
          authorization_requirement_sha256=manifest.authorization_requirement.sha256,
          run_manifest_sha256=manifest.sha256,
          pricing_snapshot=pricing,
          projected_plan_usd=projected,
      )

      assert "max_usd" not in asdict(lease)
      assert lease.projected_plan_usd is not None
      assert Decimal(lease.projected_plan_usd) > Decimal("100")
      reservation = ledger.reserve_call(
          lease,
          manifest.call_budgets[0],
          projected_call_usd=Decimal("50"),
      )
      assert "reserved_usd" not in asdict(reservation)
      assert reservation.projected_call_usd == "50"
  ```

- [ ] **Step 2: Run the authorization node and verify RED.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py::test_schema_v3_authorization_ignores_advisory_cost_but_enforces_resources
  ```

  Expected: imports/signatures fail because schema-v2 grants, leases, and reservations require USD caps.

- [ ] **Step 3: Implement schema-v3 grant/window/lease/reservation records.**

  Use these exact active shapes:

  ```python
  @dataclass(frozen=True, slots=True)
  class OperatorAuthorizationGrant:
      grant_id: str
      additional_calls: int
      additional_tokens: int
      policy_source_scope_sha256: str


  @dataclass(frozen=True, slots=True)
  class OperatorAuthorizationWindow:
      window_id: str
      grant_ids: tuple[str, ...]
      authorization_requirement_sha256: str
      max_calls: int
      max_tokens: int
      policy_source_scope_sha256: str


  @dataclass(frozen=True, slots=True)
  class AuthorizationRunLease:
      lease_id: str
      one_shot_key_sha256: str
      window_id: str
      run_manifest_sha256: str
      pricing_snapshot_sha256: str
      pricing_status: str
      projected_plan_usd: str | None
      max_calls: int
      max_tokens: int


  @dataclass(frozen=True, slots=True)
  class AuthorizationCallReservation:
      reservation_id: str
      lease_id: str
      call_index: int
      iteration: int
      role: str
      reserved_tokens: int
      projected_call_usd: str | None


  @dataclass(frozen=True, slots=True)
  class PitOptimizerProviderFacts:
      call_index: int
      iteration: int
      role: str
      requested_model: str
      returned_model: str | None
      pricing_snapshot_sha256: str
      outcome: str
      request_started: bool
      response_received: bool
      finish_reason: str | None
      response_schema_valid: bool
      accounting_complete: bool
      prompt_tokens: int | None
      completion_tokens: int | None
      total_tokens: int | None
      cost_usd: float | None
      retained_reservation_tokens: int
      audit_sha256: str
  ```

  `open_run_lease` atomically appends a `pricing_snapshot` record and `lease_open` record. It compares only planned calls/tokens against grant, window, and requirement capacity. `reserve_call` compares only calls/tokens; it canonicalizes a supplied projection for audit and never branches on its magnitude.

- [ ] **Step 4: Make reconciliation persist actual cost without treating it as capacity.**

  Replace `charged_usd`/USD-overage logic with explicit audit state:

  ```python
  if not provider_facts.request_started:
      charged_calls = 0
      charged_tokens = 0
      actual_cost_usd = 0.0
      cost_accounting_status = "not_started"
  elif provider_facts.accounting_complete:
      charged_calls = 1
      charged_tokens = provider_facts.total_tokens
      actual_cost_usd = provider_facts.cost_usd
      cost_accounting_status = "authoritative"
  else:
      charged_calls = 1
      charged_tokens = reservation.reserved_tokens
      actual_cost_usd = None
      cost_accounting_status = "unavailable"

  overage = (
      charged_tokens > reservation.reserved_tokens
      or prior_calls + charged_calls > lease.max_calls
      or prior_tokens + charged_tokens > lease.max_tokens
  )
  ```

  Keep incomplete accounting terminal. Remove `retained_reservation_usd`; retain the full token reservation and call count only. Update terminal receipt cross-checks, recovery, and hash-chain key validation to the exact schema-v3 fields.

- [ ] **Step 5: Preserve schema-v2 records as non-resumable history.**

  Add a reader that verifies canonical JSON, record indices, and the hash chain, then returns immutable record mappings without constructing an active ledger:

  ```python
  def test_schema_v2_authorization_history_is_readable_but_not_resumable(
      tmp_path: Path,
  ) -> None:
      path = _write_minimal_legacy_v2_history(tmp_path)
      records = read_legacy_authorization_history(path)
      assert tuple(record["schema_version"] for record in records) == (2,)
      with pytest.raises(AuthorizationError, match="schema-v2.*not resumable"):
          AuthorizationLedger(path, _v3_manifest())
  ```

  The active `_read_records` accepts only schema 3. Do not translate, rewrite, or append to a schema-v2 file.

- [ ] **Step 6: Remove USD from the operator grant CLI and run focused authorization tests.**

  `record-grant` accepts `--additional-calls`, `--additional-tokens`, source-scope identity, and approval reference. It does not accept `--additional-usd`.

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py -k "schema_v3_authorization or record_grant or authorization_window or run_lease or budget_reservation or provider_call_overage or legacy_v2_authorization_history"
  ```

  Expected: PASS; a high authoritative cost is recorded, while a token or call overage is committed and then rejected.

- [ ] **Step 7: Commit the durable authorization migration.**

  ```powershell
  git add core/pit_optimizer_authorization.py tests/test_pit_optimizer_v2.py
  git commit -m "Make PIT optimizer USD accounting audit-only"
  ```

---

### Task 3: Isolate the Optimizer Resource Ledger and Best-Effort Pricing

**Files:**

- Modify: `agent_loop.py:1600-2420,2926-4590,12200-13390,18210-19090`
- Modify: `tests/test_pit_optimizer_v2.py:3890-4300,5140-6415,7200-8700`
- Modify: `tests/test_agent_loop.py:6140-6685`

**Interfaces:**

- Consumes: `OptimizerPricingSnapshot`, schema-v3 authorization lease/reservation, and strict `Usage` accounting.
- Produces: `PitOptimizerResourceReservation`; `PitOptimizerResourceLedger`; token-only `preflight_pit_optimizer_call`; best-effort `freeze_pit_optimizer_pricing`; one-shot gateway behavior with actual USD auditing.

- [ ] **Step 1: Write RED tests proving high cost cannot stop an optimizer call.**

  ```python
  def test_optimizer_resource_ledger_records_high_cost_without_a_cost_gate() -> None:
      ledger = PitOptimizerResourceLedger(max_calls=1, max_tokens=100)
      reservation = ledger.reserve_pit_optimizer(
          rendered_prompt_bytes=20,
          max_output_tokens=30,
          projected_cost_usd=Decimal("500"),
      )
      ledger.reconcile_pit_optimizer(
          reservation,
          Usage(
              prompt_tokens=20,
              completion_tokens=30,
              total_tokens=50,
              cost_usd=900.0,
          ),
          request_started=True,
      )

      assert ledger.calls == 1
      assert ledger.total_tokens == 50
      assert ledger.authoritative_usd == pytest.approx(900.0)
      assert not hasattr(ledger, "max_usd")
      assert not hasattr(ledger, "reserved_usd")
  ```

  ```python
  def test_optimizer_resource_ledger_still_rejects_token_reservation_overflow() -> None:
      ledger = PitOptimizerResourceLedger(max_calls=1, max_tokens=49)
      with pytest.raises(BudgetExceededError, match="token"):
          ledger.reserve_pit_optimizer(
              rendered_prompt_bytes=20,
              max_output_tokens=30,
              projected_cost_usd=None,
          )


  def test_optimizer_resource_ledger_rejects_invalid_actual_cost() -> None:
      ledger = PitOptimizerResourceLedger(max_calls=1, max_tokens=100)
      reservation = ledger.reserve_pit_optimizer(
          rendered_prompt_bytes=20,
          max_output_tokens=30,
          projected_cost_usd=None,
      )
      with pytest.raises(ResponseValidationError, match="accounting"):
          ledger.reconcile_pit_optimizer(
              reservation,
              Usage(
                  prompt_tokens=20,
                  completion_tokens=30,
                  total_tokens=50,
                  cost_usd=float("nan"),
              ),
              request_started=True,
          )
  ```

- [ ] **Step 2: Run both nodes and verify RED.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py -k "optimizer_resource_ledger"
  ```

- [ ] **Step 3: Move optimizer reservations out of the legacy `BudgetLedger`.**

  Keep `BudgetLedger` and `BudgetWindow` unchanged for legacy workflows. Introduce:

  ```python
  @dataclass(frozen=True)
  class PitOptimizerResourceReservation:
      reservation_id: str
      projected_cost_usd: Decimal | None
      prompt_bytes: int
      completion_allowance: int
      token_upper_bound: int


  class PitOptimizerResourceLedger:
      def __init__(self, *, max_calls: int, max_tokens: int) -> None:
          self.max_calls = max_calls
          self.max_tokens = max_tokens
          self.calls = 0
          self.prompt_tokens = 0
          self.completion_tokens = 0
          self.total_tokens = 0
          self.reserved_tokens = 0
          self.authoritative_usd = 0.0
          self.retained_reservation_tokens = 0
          self.incomplete_accounting_calls = 0
  ```

  Add `reserve_pit_optimizer(rendered_prompt_bytes, max_output_tokens,
  projected_cost_usd) -> PitOptimizerResourceReservation`,
  `reconcile_pit_optimizer(reservation, usage, request_started) -> None`, and
  `verify_pit_optimizer_reconciliation(reservation, usage, request_started) ->
  None` with the call/token checks from the RED tests.
  Before-send failures release calls/tokens; complete usage validates
  `prompt + completion == total`, commits actual tokens/cost, and raises only
  after committing a token overage; missing accounting retains calls/tokens,
  marks incomplete, and never invents an actual USD value. Move
  `_pit_optimizer_reservation_primitive`, recovery snapshot validation, and
  `_restore_pit_optimizer_recovery_state` to this class with
  `projected_cost_usd` serialized as a canonical decimal string or `null`.
  Remove the optimizer-only methods from `BudgetLedger` after all callers use
  the dedicated class.

- [ ] **Step 4: Make pricing advisory and best-effort.**

  `preflight_pit_optimizer_call` validates static/dynamic bytes, conservative input tokens, model, and pricing-snapshot identity, then returns `Decimal | None` without a cost comparison:

  ```python
  projected = pricing.projected_call_usd(
      prompt_bytes,
      call_budget.max_output_tokens,
  )
  return projected
  ```

  In `freeze_pit_optimizer_pricing`, preserve deadline/cancellation failures, but map pricing-loader failures to a sealed unavailable snapshot:

  ```python
  try:
      value = self.pricing_loader(model)
  except Exception:
      if _remaining_wall_seconds(float(wall_deadline), monotonic) <= 0:
          raise BudgetExceededError("PIT optimizer pricing deadline reached")
      frozen = OptimizerPricingSnapshot.unavailable(model=model)
  else:
      frozen = freeze_pricing_record(model, value)
  ```

  Do not catch `BaseException`. Cache and bind the available/unavailable snapshot to one run and one authorization ledger exactly as today.

- [ ] **Step 5: Remove runtime USD rejection from the OpenRouter role boundary.**

  The response check becomes:

  ```python
  if (
      usage.prompt_tokens > plan_snapshot.max_input_tokens
      or usage.completion_tokens > plan_snapshot.max_output_tokens
      or usage.total_tokens
      > plan_snapshot.max_input_tokens + plan_snapshot.max_output_tokens
      or prospective_ledger_tokens > self.ledger.max_tokens
  ):
      facts = provider_facts(
          outcome="budget_exceeded",
          request_started=True,
          response_received=True,
          returned_model=returned_model,
          finish_reason=finish_reason,
          response_schema_valid=False,
          usage=usage,
      )
      finalize(facts, usage, terminal_code="budget_exhausted")
      raise BudgetExceededError("optimizer per-call provider token cap exceeded")
  ```

  Pass the advisory projection into both resource and authorization reservations. Keep `max_attempts=1`, response schema validation, response byte bounds, source binding, durable audit publication, and exact accounting unchanged.

- [ ] **Step 6: Add gateway coverage for unavailable pricing and high actual cost.**

  ```python
  def test_optimizer_gateway_calls_once_when_pricing_is_unavailable(
      tmp_path: Path,
      v3_manifest: contract.PitOptimizerRunManifest,
  ) -> None:
      authorization, _ledger_path, window = _authorized_v3_ledger(
          tmp_path,
          v3_manifest,
      )
      resources = PitOptimizerResourceLedger(
          max_calls=6,
          max_tokens=448_000,
      )
      audit = AuditTrail(
          tmp_path / "audit-unavailable-pricing",
          v3_manifest.run_id,
      )
      client = _Task6FakeClient(
          [
              _task6_fake_response(
                  _canonical_text(_investigator_payload()),
                  cost=250.0,
              )
          ]
      )
      gateway = OpenRouterGateway(
          client=client,
          run_id=v3_manifest.run_id,
          pricing_loader=lambda _model: (_ for _ in ()).throw(OSError("offline")),
          ledger=resources,
          authorization_ledger=authorization,
          audit_trail=audit,
          max_attempts=1,
      )
      pricing = gateway.freeze_pit_optimizer_pricing(
          model=v3_manifest.model,
          wall_deadline=10.0,
          monotonic=lambda: 1.0,
      )
      lease = authorization.open_run_lease(
          window_id=window.window_id,
          authorization_requirement_sha256=(
              v3_manifest.authorization_requirement.sha256
          ),
          run_manifest_sha256=v3_manifest.sha256,
          pricing_snapshot=pricing,
          projected_plan_usd=None,
      )
      result = gateway.request_pit_optimizer_once(
          "investigator",
          _task6_manifest_investigator_input(v3_manifest),
          _task6_investigator_parser,
          call_budget=v3_manifest.call_budgets[0],
          authorization_lease=lease,
          frozen_pricing=pricing,
          wall_deadline=10.0,
          monotonic=lambda: 1.0,
      )

      assert pricing.lookup_status == "unavailable"
      assert len(client.completions.calls) == 1
      assert result.facts.cost_usd == pytest.approx(250.0)
  ```

- [ ] **Step 7: Run the focused runtime slice and commit.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_agent_loop.py tests/test_pit_optimizer_v2.py -k "optimizer_resource_ledger or optimizer_gateway or pricing or provider_call or token_reservation"
  git add agent_loop.py tests/test_agent_loop.py tests/test_pit_optimizer_v2.py
  git commit -m "Use call-token ledger for PIT optimizer"
  ```

---

### Task 4: Propagate Schema v3 Through Controller, Artifacts, and CLI

**Files:**

- Modify: `core/pit_optimizer_controller.py:60-240,430-1020,1840-1910,2260-2440`
- Modify: `core/pit_optimizer_artifacts.py:550-610`
- Modify: `core/pit_optimization.py:3245-3350`
- Modify: `agent_loop.py:9949-9988,17920-18195,19090-19435,20395-20575`
- Modify: `tests/test_pit_optimizer_loop.py:1600-2400,3040-3410`
- Modify: `tests/test_agent_loop.py:180-620,13640-13710`

**Interfaces:**

- Consumes: schema-v3 manifest, authorization lease, pricing snapshot, resource ledger, and provider facts.
- Produces: schema-v3 readiness/result/artifacts; cost audit summaries without USD authorization; optimizer-specific CLI limits; `prepare_pit_optimizer_v3`, `load_pit_optimizer_v3_readiness`, `run_pit_optimizer_v3`, and matching `_build/_dispatch/_summary` agent-loop entrypoints; discovery-only canary completion.

  Remove the old live schema-v2 entrypoints instead of aliasing them to schema
  v3. The CLI gate name remains `pit_optimizer`; only its active Python
  implementation names and serialized contracts advance to v3.

- [ ] **Step 1: Write RED accounting-artifact and stop-condition tests.**

  ```python
  def test_schema_v3_accounting_has_no_usd_authority_or_reservation(v3_run_state) -> None:
      artifact = controller._current_accounting_artifact(v3_run_state)
      assert artifact["schema_version"] == 3
      assert set(artifact["authorized_totals"]) == {"calls", "tokens"}
      assert set(artifact["reserved_totals"]) == {"calls", "tokens"}
      assert set(artifact["pricing_advisory"]) == {
          "status",
          "projected_plan_usd",
      }
      assert artifact["actual_totals"]["usd"] >= 0
  ```

  ```python
  def test_pre_iteration_stop_ignores_cost_and_preserves_call_token_gates(
      v3_readiness,
      v3_state_after_one_iteration,
      v3_services,
  ) -> None:
      state = v3_state_after_one_iteration
      assert sum(
          attempt.facts.cost_usd or 0.0
          for attempt in state.provider_attempts
      ) > 0.40
      assert controller._pre_iteration_stop(v3_readiness, state, v3_services) is None
      state.authorization_lease = replace(state.authorization_lease, max_calls=3)
      assert controller._pre_iteration_stop(v3_readiness, state, v3_services) == (
          "budget_exhausted",
          "call_budget_exhausted",
      )
  ```

  Build `v3_state_after_one_iteration` from the existing run-state fixture with
  three accepted `_ProviderAttemptRecord` values in investigator/author/critic
  order, each with complete token accounting and `cost_usd=1.0`; set
  `next_iteration=2` and retain a six-call/448,000-token lease.

- [ ] **Step 2: Run the controller nodes and verify RED.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py -k "schema_v3_accounting or pre_iteration_stop_ignores_cost"
  ```

- [ ] **Step 3: Update controller summaries and remove the cost stop.**

  Use this budget result shape:

  ```python
  @dataclass(frozen=True, slots=True)
  class OptimizerBudgetSummary:
      api_calls: int
      prompt_tokens: int
      completion_tokens: int
      total_tokens: int
      authoritative_usd: float
      projected_plan_usd: str | None
      pricing_status: str
      retained_reservation_tokens: int
      incomplete_accounting_calls: int
      accounting_complete: bool
  ```

  Change `_budget_summary_from_calls` to accept the attempt sequence plus the
  active or snapshotted `AuthorizationRunLease`; source `projected_plan_usd`
  and `pricing_status` only from that lease. Remove `required_usd`,
  `charged_usd`, and `cost_budget_exhausted` from `_pre_iteration_stop`. Emit
  `authorized_totals={calls,tokens}`, `reserved_totals={calls,tokens}`,
  advisory pricing, authoritative actual cost, and an explicit
  accounting-complete flag. Update every optimizer JSON artifact and
  `PitOptimizerReadiness`/`PitOptimizerResult` to schema 3; make
  `IncrementalArtifactStore.write_json_artifact` accept schema 3 for this
  closed layout.

- [ ] **Step 4: Make the subset controller stop before hidden validation.**

  Add:

  ```python
  def test_subset_canary_never_opens_hidden_after_discovery_improvement(
      v3_readiness,
      v3_services,
  ) -> None:
      result = controller.run_pit_optimizer_v3(
          readiness=v3_readiness,
          services=v3_services,
      )
      assert result.discovery_winner is not None
      assert result.hidden_validation_opened is False
      assert result.long_replay_eligible is None
      assert v3_services.hidden_calls == 0
  ```

  In `run_pit_optimizer_v3`, retain `_finish_discovery` determinism confirmation, but do not call `_run_hidden_once`. A discovery winner is only eligible for a later separately authorized local hidden-evaluation stage.

- [ ] **Step 5: Remove USD from the optimizer CLI without weakening legacy routes.**

  Add a dedicated common-limit shape with no USD member:

  ```python
  @dataclass(frozen=True)
  class PitOptimizerLoopLimits:
      max_iterations: int
      max_api_calls: int
      max_tokens: int
      api_timeout_seconds: float
      child_timeout_seconds: float
      wall_timeout_seconds: float
      output_limit_bytes: int
  ```

  Make global `--max-usd` optional at parse time. Require it for every existing non-`pit_optimizer` route; reject it for `pit_optimizer`. Construct `PitOptimizerLoopLimits` only for the schema-v3 gate and preserve the current `LoopLimits` construction everywhere else. Remove `max_usd` from `PitOptimizerGateConfig`, `_build_pit_optimizer_v3_config`, `_pit_optimizer_v3_prepare_lines`, and the emitted canary command.

- [ ] **Step 6: Prove CLI coexistence and closed summaries.**

  ```python
  def test_pit_optimizer_cli_rejects_usd_but_legacy_gate_still_requires_it(
      optimizer_argv: list[str],
      legacy_argv: list[str],
  ) -> None:
      config, _docker, _image = _build_cli_config(
          _argument_parser().parse_args(optimizer_argv)
      )
      assert isinstance(config.limits, PitOptimizerLoopLimits)
      with pytest.raises(ConfigurationError, match="does not accept --max-usd"):
          _build_cli_config(
              _argument_parser().parse_args([*optimizer_argv, "--max-usd", "1"])
          )
      with pytest.raises(ConfigurationError, match="requires --max-usd"):
          _build_cli_config(_argument_parser().parse_args(legacy_argv))
  ```

  `optimizer_argv` is the existing complete optimizer-canary CLI fixture with
  no USD option. `legacy_argv` is the existing complete diagnosis CLI fixture
  with its `--max-usd` pair removed.

  Public summaries must still exclude provider content, credentials, raw hashes, container IDs, raw trades, and hidden identities.

- [ ] **Step 7: Run the focused controller/CLI slice and commit.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py tests/test_agent_loop.py -k "schema_v3 or pit_optimizer_v3_summary or optimizer_cli or pre_iteration_stop or hidden_boundary or subset_canary"
  git add agent_loop.py core/pit_optimization.py core/pit_optimizer_artifacts.py core/pit_optimizer_controller.py tests/test_agent_loop.py tests/test_pit_optimizer_loop.py
  git commit -m "Expose audit-only PIT optimizer v3 flow"
  ```

---

### Task 5: Prove the Complete Two-Iteration Subset Flow Without a Provider

**Files:**

- Modify: `tests/test_pit_optimizer_loop.py:1640-2400`
- Modify: `tests/test_pit_optimizer_v2.py:2250-2760,4400-4900`
- Verify: all production files listed above

**Interfaces:**

- Consumes: production composition with fake OpenRouter transport and fake Docker worker boundary.
- Produces: six accepted role records, two complete feedback-linked iterations, at least one deterministic discovery evaluation, complete high-cost accounting, unchanged source, complete cleanup, and zero hidden/full replay access.

- [ ] **Step 1: Extend the production-composition test with high costs and feedback lineage.**

  The fake responses must report complete costs greater than the former per-call and total ceilings:

  ```python
  response_costs = (0.25, 0.50, 0.25, 0.25, 0.50, 0.25)
  assert sum(response_costs) == pytest.approx(2.0)
  ```

  Assert the completed result:

  ```python
  assert result.schema_version == 3
  assert result.iterations_started == 2
  assert result.iterations_completed == 2
  assert result.budget.api_calls == 6
  assert result.budget.authoritative_usd == pytest.approx(2.0)
  assert result.budget.accounting_complete is True
  assert result.hidden_validation_opened is False
  assert result.long_replay_eligible is None
  assert result.source_modified is False
  assert result.cleanup_complete is True
  assert second_investigator_input.prior_iterations[0].critic_direction
  ```

- [ ] **Step 2: Run the single mocked end-to-end node and verify RED, then make only fixture/shape corrections.**

  ```powershell
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_loop.py::test_dispatch_with_production_composition_completes_mocked_two_iteration_canary
  ```

  Expected initial failure: old fixture USD fields or schema-2 artifact expectations. Do not weaken lineage, identity, cleanup, response validation, or sandbox assertions to make it pass.

- [ ] **Step 3: Add an unavailable-pricing variant of the same composition test.**

  ```python
  @pytest.mark.parametrize("pricing_available", [True, False])
  def test_dispatch_with_production_composition_completes_mocked_two_iteration_canary(
      pricing_available: bool,
      tmp_path: Path,
      request: pytest.FixtureRequest,
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      pricing_loader = (
          (lambda _model: {"prompt": Decimal("2"), "completion": Decimal("8")})
          if pricing_available
          else (lambda _model: (_ for _ in ()).throw(OSError("pricing unavailable")))
      )
      # In the existing gateway_factory inside this test:
      gateway = agent_loop.OpenRouterGateway(
          client=client,
          pricing_loader=pricing_loader,
          **kwargs,
      )
      ambient_marker = _forbid_ambient_git(tmp_path, monkeypatch, git_executable)
      result = agent_loop._dispatch_pit_optimizer_v3(
          canary_config,
          prepare=lambda _config: pytest.fail("canary dispatched prepare"),
          build_live_services=lambda config: agent_loop._build_pit_optimizer_v3_live_run(
              config,
              source_state=source_state,
              git_capability=git_capability,
              api_timeout_seconds=30.0,
              wall_timeout_seconds=600.0,
              gateway_factory=gateway_factory,
              worker_runner_factory=worker_runner_factory,
              evaluator_data_factory=evaluator_data_factory,
          ),
      )
      assert result.budget.api_calls == 6
      assert result.budget.pricing_status == (
          "available" if pricing_available else "unavailable"
      )
  ```

- [ ] **Step 4: Compile changed production modules and run only the optimizer architecture suite.**

  ```powershell
  python -B -m py_compile agent_loop.py core/pit_optimization.py core/pit_optimization_contract.py core/pit_optimizer_artifacts.py core/pit_optimizer_authorization.py core/pit_optimizer_controller.py core/pit_optimizer_evaluation.py
  python -B -m pytest -p no:cacheprovider --no-cov -q tests/test_pit_optimizer_v2.py tests/test_pit_optimizer_loop.py tests/test_agent_loop.py -k "pit_optimizer or schema_v3"
  ```

  Expected: PASS. These commands use fake transports/synthetic local data only and make zero provider calls.

- [ ] **Step 5: Commit the integration proof.**

  ```powershell
  git add tests/test_agent_loop.py tests/test_pit_optimizer_loop.py tests/test_pit_optimizer_v2.py
  git commit -m "Verify two-iteration audit-only optimizer flow"
  ```

---

### Task 6: Regenerate Clean Local Readiness and Stop at the Live Boundary

**Files:**

- Verify: committed source files from Tasks 1-5
- Create locally only: ignored `.artifacts/pit-policy-parity-v10/**`
- Create locally only: ignored `.artifacts/pit-optimizer-v10/**`

**Interfaces:**

- Consumes: clean committed source, sealed local PIT bundle/baseline, prior authenticated subset readiness, digest-pinned Docker image, and local parity reference.
- Produces: a schema-v3 manifest, schema-v3 readiness package, and an inert six-call canary command. It consumes zero role calls.

- [ ] **Step 1: Require a clean feature worktree before deriving identities.**

  ```powershell
  git diff --check
  git status --porcelain
  ```

  Expected: both commands print no source changes. Ignored local artifacts do not appear.

- [ ] **Step 2: Generate a new two-fold parity attestation without hidden data.**

  ```powershell
  $sourceRoot = (Get-Location).Path
  $sealedEvidenceRoot = 'C:\Projects\trading_bot\RS-momentum-EMA-trading-bot\.artifacts'
  $parityRoot = Join-Path $sourceRoot '.artifacts\pit-policy-parity-v10'
  $optimizerRoot = Join-Path $sourceRoot '.artifacts\pit-optimizer-v10'
  $runtimeRoot = Join-Path $optimizerRoot 'runtime'
  $runArtifactRoot = Join-Path $optimizerRoot 'runs'
  $controllerTemp = Join-Path ([System.IO.Path]::GetTempPath()) 'pit-optimizer-v10-candidates'
  New-Item -ItemType Directory -Force -Path $parityRoot,$optimizerRoot,$runtimeRoot,$runArtifactRoot,$controllerTemp | Out-Null
  python -B -m core.pit_policy_parity verify --reference (Join-Path $sourceRoot '.artifacts\pit-policy-parity-v2\reference.json') --pit-bundle (Join-Path $sealedEvidenceRoot 'task-4-regeneration-20260823T223000Z\pit-bundle\pit_baseline.sqlite3') --output (Join-Path $parityRoot 'verified-final.json')
  ```

  Expected: `PIT_POLICY_PARITY matched=true`; no provider, hidden-fold, or Docker process starts.

- [ ] **Step 3: Build the schema-v3 manifest with the exact six-call profile.**

  ```powershell
  $priorManifest = Get-Content -Raw -LiteralPath (Join-Path $sourceRoot '.artifacts\pit-optimizer-v7\manifest.json') | ConvertFrom-Json
  $sandboxImage = [string]$priorManifest.sandbox_image
  $manifestPath = Join-Path $optimizerRoot 'manifest.json'
  $manifestBuildOutput = python -B -m core.pit_optimizer_evaluation build-subset-manifest --readiness (Join-Path $sealedEvidenceRoot 'pit-optimizer-subset-performance-20260827T200714Z\pit-optimization-readiness-d3cbfcb22900\readiness.json') --verified-parity (Join-Path $parityRoot 'verified-final.json') --pit-bundle (Join-Path $sealedEvidenceRoot 'task-4-regeneration-20260823T223000Z\pit-bundle\pit_baseline.sqlite3') --baseline-run (Join-Path $sealedEvidenceRoot 'task-11-prefix-replay-20260826T001500Z\run-20260826T002913Z-1af306ef1e46') --source-root $sourceRoot --permanent-runtime-root $runtimeRoot --controller-temp-parent $controllerTemp --artifact-root $runArtifactRoot --git-executable 'C:\Program Files\Git\cmd\git.exe' --docker-executable 'C:\Users\llong\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe' --sandbox-image $sandboxImage --iterations 2 --investigator-static-bytes 8000 --investigator-dynamic-bytes 78000 --investigator-input-tokens 86000 --investigator-output-tokens 16000 --investigator-response-bytes 8192 --author-static-bytes 12000 --author-dynamic-bytes 48500 --author-input-tokens 72000 --author-output-tokens 14000 --author-response-bytes 16384 --critic-static-bytes 8000 --critic-dynamic-bytes 24000 --critic-input-tokens 32000 --critic-output-tokens 4000 --critic-response-bytes 8192 --max-files 3 --max-hunks 12 --max-changed-lines 80 --max-diff-bytes 8192 --output $manifestPath
  $manifestBuildOutput
  ```

  No role has a USD argument. Expected output reports schema 3, six calls, 448,000 tokens, zero retries, `apply=false`, and an exact provider-free prepare command.

- [ ] **Step 4: Run only the emitted prepare command.**

  ```powershell
  $prepareLine = $manifestBuildOutput | Where-Object { $_ -like 'PIT_OPTIMIZER_PREPARE_COMMAND=*' } | Select-Object -First 1
  if (-not $prepareLine) { throw 'Manifest build did not emit a prepare command.' }
  $prepareCommand = $prepareLine.Substring('PIT_OPTIMIZER_PREPARE_COMMAND='.Length)
  $prepareOutput = & cmd.exe /d /s /c $prepareCommand
  $prepareOutput
  ```

  The prepare command reauthenticates source, parity, bundle, baseline, folds, policy scope, and image; writes readiness; and prints an inert canary command. It must not construct a provider client, look up live pricing, record a grant, start Docker, evaluate hidden, or make a role call.

- [ ] **Step 5: Inspect the closed local evidence and stop.**

  Confirm:

  ```text
  schema_version=3
  max_calls=6
  max_tokens=448000
  provider_retries=0
  apply=false
  enforcing USD fields absent
  hidden result absent
  provider records absent
  source_modified=false
  ```

  Report the aggregate scope and inert canary command without printing credentials, provider content, raw hashes, or container IDs. Do not record a grant or execute the command yet.

---

### Task 7: Run the Live Six-Call Subset Only After Fresh Authorization

**Files:**

- Create locally only: schema-v3 authorization ledger and run artifacts under ignored `.artifacts/pit-optimizer-v10/**`
- Modify source: none

**Interfaces:**

- Consumes: a fresh explicit authorization for the exact three editable policy files, six calls, 448,000 tokens, two iterations, `apply=false`, zero retries, and USD audit-only semantics.
- Produces: one closed discovery-only canary result or one reconciled terminal failure. It never opens hidden validation or the full replay.

- [ ] **Step 1: Obtain fresh live-run authorization.**

  The previous `$0.40` authorization is not reusable because schema v3 changes its terms. Stop until the user explicitly authorizes the exact source scope and six-call/token envelope with no enforcing USD ceiling.

- [ ] **Step 2: Record one calls/tokens-only grant.**

  ```powershell
  $sourceRoot = (Get-Location).Path
  $optimizerRoot = Join-Path $sourceRoot '.artifacts\pit-optimizer-v10'
  $runtimeRoot = Join-Path $optimizerRoot 'runtime'
  $manifestPath = Join-Path $optimizerRoot 'manifest.json'
  $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
  $manifestSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash.ToLowerInvariant()
  $scopeSha256 = [string]$manifest.authorization_requirement.policy_source_scope_sha256
  python -B -m core.pit_optimizer_authorization record-grant --ledger-path (Join-Path $runtimeRoot 'pit_optimizer_authorization_ledger.jsonl') --manifest-path $manifestPath --manifest-sha256 $manifestSha256 --grant-id 'grant_v3_subset_20260828' --additional-calls 6 --additional-tokens 448000 --policy-source-scope-sha256 $scopeSha256 --operator-approval-reference 'user-approved-v3-six-call-audit-only-canary'
  ```

  The command has no `--additional-usd` option.

- [ ] **Step 3: Execute the inert canary command exactly once.**

  ```powershell
  $canaryLine = $prepareOutput | Where-Object { $_ -like 'PIT_OPTIMIZER_CANARY_COMMAND=*' } | Select-Object -First 1
  if (-not $canaryLine) { throw 'Prepare did not emit a canary command.' }
  $canaryCommand = $canaryLine.Substring('PIT_OPTIMIZER_CANARY_COMMAND='.Length)
  & cmd.exe /d /s /c $canaryCommand
  ```

  If execution resumes in a new shell, rerun the provider-free Task 6 manifest
  and prepare commands first to repopulate `$prepareOutput`; do not reconstruct
  or edit the authenticated canary command by hand.

  Expect exactly six OpenRouter role calls unless an identity, accounting, provider-schema, token, sandbox, audit, cancellation, or cleanup failure terminates earlier. Provider-level automatic retries remain zero. Do not start another canary automatically after failure.

- [ ] **Step 4: Verify the terminal evidence before discussing performance.**

  Require two complete feedback-linked iterations, exact call/token accounting, authoritative actual USD totals, deterministic candidate comparison, unchanged source, cleanup complete, and no hidden/full replay artifacts. Separate architecture success from return improvement; report discovery-fold return deltas only from local aggregate artifacts.

- [ ] **Step 5: Stop for a separate hidden-evaluation decision.**

  If and only if a discovery candidate strictly improves the fixed objective, present it as eligible for a later one-time local hidden evaluation. Do not open hidden data, run the full replay, apply the candidate to source, push, or upload anything in this task.

## Completion Criteria

- Active optimizer authority and artifacts are schema 3 and contain no enforcing USD ceiling fields.
- Calls and tokens remain exact, fail-closed hard controls; provider retries remain zero.
- Pricing failure cannot stop an otherwise authorized role call.
- High valid provider cost is recorded and surfaced but cannot reject a call, iteration, candidate, or run.
- Missing/invalid provider accounting remains terminal and leaves durable evidence.
- The two-iteration mocked production composition passes with high costs and with unavailable pricing.
- The clean local subset readiness is provider-free and exposes no hidden result.
- A live canary, if separately authorized, stops after discovery and never starts the long replay.
