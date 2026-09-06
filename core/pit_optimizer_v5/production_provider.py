"""Concrete local provider authorization and reconciliation for PIT optimizer V5."""

from __future__ import annotations

from decimal import Context, Decimal, ROUND_CEILING, localcontext
import os
import re
import secrets
import time
from typing import Callable, Mapping

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5, RoleLedgerMigrationAuthorityV5
from core.pit_optimizer_v5.contracts import CampaignManifestV5, canonical_json_bytes_v5, canonical_sha256_v5
from core.pit_optimizer_v5.provider import (
    AuthorizedRoleSlotV5,
    CompletionResultV5,
    ExistingPersistedRoleRequestV5,
    LedgerRoleTerminalAuthorityV5,
    ParsedRoleArtifactV5,
    RecoveredRoleTerminalV5,
    RoleAttemptFactsV5,
    RoleFailureCode,
    RoleFailureV5,
    RoleInvocationPackageV5,
    RoleInvocationClaimV5,
    RoleLedgerReservationV5,
    RoleLedgerTerminalV5,
    RoleProviderResponseV5,
    RoleReconciliationFailureV5,
    RoleReconciliationResultV5,
    RoleRequestV5,
    RoleSlotRequestV5,
    RoleTerminalReceiptV5,
    RoleUsageFactsV5,
    canonical_role_response_sha256_v5,
    parse_and_bind_role_artifact,
    parsed_role_artifact_primitive_v5,
)


class OpenRouterOneShotJsonCompletionV5:
    """Exact lazy OpenRouter transport bound to one concrete local V5 ledger."""

    __slots__ = (
        "_ledger",
        "api_key_environment_variable",
        "audit_store_identity_sha256",
        "gateway_identity_sha256",
        "ledger_identity_sha256",
    )

    def __init__(
        self,
        *,
        ledger: LocalRoleAuthorizationLedgerV5,
        api_key_environment_variable: str = "OPENROUTER_API_KEY",
    ) -> None:
        if (
            type(ledger) is not LocalRoleAuthorizationLedgerV5
            or api_key_environment_variable != "OPENROUTER_API_KEY"
        ):
            raise ValueError("OpenRouter V5 gateway requires the concrete local role ledger")
        self._ledger = ledger
        self.api_key_environment_variable = api_key_environment_variable
        self.ledger_identity_sha256 = ledger.ledger_identity_sha256
        self.audit_store_identity_sha256 = ledger.audit_store_identity_sha256
        self.gateway_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-openrouter-one-shot-v1",
                "transport": "agent_loop.OpenRouterGateway.request_pit_optimizer_v5_json_once",
                "api_key_environment_variable": api_key_environment_variable,
                "ledger_identity_sha256": self.ledger_identity_sha256,
                "audit_store_identity_sha256": self.audit_store_identity_sha256,
            }
        )

    @property
    def ledger(self) -> LocalRoleAuthorizationLedgerV5:
        return self._ledger

    def invoke_json_once(
        self,
        *,
        request_sha256: str,
        model: str,
        messages: tuple[Mapping[str, object], ...],
        response_schema_json: bytes,
        max_output_tokens: int,
        automatic_retries: int,
        schema_repair_calls: int,
    ) -> CompletionResultV5:
        capabilities = self._ledger._manifest.provider
        if (
            capabilities is None
            or type(request_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", request_sha256) is None
            or model != capabilities.model
            or type(max_output_tokens) is not int
            or max_output_tokens < 1
            or max_output_tokens > capabilities.maximum_output_tokens_per_role
            or automatic_retries != 0
            or schema_repair_calls != 0
        ):
            raise ValueError("OpenRouter V5 completion exceeds its manifest authority")
        # The legacy transport resolves the controller-local secret lazily here.
        # Construction and readiness therefore remain provider-free and secret-free.
        from agent_loop import OpenRouterGateway

        api_key = os.environ.get(self.api_key_environment_variable)
        if type(api_key) is not str or not api_key:
            raise ValueError("OpenRouter V5 API key environment handle is unavailable")
        gateway = OpenRouterGateway(
            api_key=api_key,
            run_id=f"pit-optimizer-v5-{self._ledger._manifest.campaign_id}",
            max_attempts=1,
        )
        result = gateway.request_pit_optimizer_v5_json_once(
            request_sha256=request_sha256,
            model=model,
            messages=messages,
            response_schema_json=response_schema_json,
            max_output_tokens=max_output_tokens,
        )
        if type(result) is not CompletionResultV5:
            raise ValueError("OpenRouter V5 gateway returned an invalid completion")
        response = RoleProviderResponseV5(
            5,
            self._ledger._manifest.campaign_id,
            self._ledger.campaign_manifest_sha256,
            self.ledger_identity_sha256,
            self.audit_store_identity_sha256,
            request_sha256,
            result,
        )
        recovered = self._ledger.publish_role_provider_response(response)
        if recovered is None:
            raise RuntimeError("OpenRouter V5 response arrived after terminal settlement")
        return recovered.completion


class LocalRoleAuthorizationLedgerV5:
    """Create-only local ledger implementing reserve, settle, recovery, and reconciliation."""

    def __init__(
        self,
        *,
        repository: LocalArtifactRepositoryV5,
        manifest: CampaignManifestV5,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(repository) is not LocalArtifactRepositoryV5 or type(manifest) is not CampaignManifestV5:
            raise ValueError("local V5 role-ledger authority is invalid")
        if manifest.provider is None:
            raise ValueError("local V5 role ledger requires provider capabilities")
        self._repository = repository
        self._manifest = manifest
        self._clock_ms = (lambda: time.time_ns() // 1_000_000) if clock_ms is None else clock_ms
        if not callable(self._clock_ms):
            raise ValueError("local V5 role-ledger clock is invalid")
        self._invocation_owner_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-role-invocation-owner-v1",
                "nonce": secrets.token_hex(32),
            }
        )
        self.campaign_manifest_sha256 = manifest.sha256
        self.audit_store_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-role-audit-v1",
                "repository_root_identity_sha256": repository.root_identity_sha256,
                "campaign_id": manifest.campaign_id,
            }
        )
        self.ledger_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-role-ledger-v1",
                "campaign_manifest_sha256": manifest.sha256,
                "audit_store_identity_sha256": self.audit_store_identity_sha256,
                "provider_capabilities_sha256": canonical_sha256_v5(manifest.provider),
            }
        )
        self._load_verified()

    @property
    def repository(self) -> LocalArtifactRepositoryV5:
        return self._repository

    @property
    def manifest(self) -> CampaignManifestV5:
        return self._manifest

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or value < 0:
            raise ValueError("local V5 role-ledger clock returned an invalid time")
        return value

    def _conservative_unreported_usage(self, slot: AuthorizedRoleSlotV5) -> RoleUsageFactsV5:
        """Derive the deterministic charge for one possibly-launched request."""

        provider = self._manifest.provider
        assert provider is not None
        remaining_attempts = provider.maximum_role_calls - slot.prior_external_attempts
        remaining_tokens = provider.maximum_total_tokens - slot.prior_total_tokens
        if remaining_attempts < 1 or remaining_tokens < 0:
            raise ValueError("local V5 role-ledger prior accounting exceeds authority")
        fair_token_reservation = (remaining_tokens + remaining_attempts - 1) // remaining_attempts
        reserved_tokens = min(
            remaining_tokens,
            max(slot.request.max_output_tokens, fair_token_reservation),
        )
        if provider.maximum_usd is None:
            reserved_cost = Decimal("0")
        else:
            remaining_cost = provider.maximum_usd - slot.prior_cost_usd
            if remaining_cost < 0:
                raise ValueError("local V5 role-ledger prior cost accounting exceeds authority")
            with localcontext(Context(prec=50, rounding=ROUND_CEILING)):
                reserved_cost = remaining_cost / Decimal(remaining_attempts)
        return RoleUsageFactsV5(
            1,
            True,
            False,
            reserved_tokens,
            0,
            reserved_tokens,
            reserved_cost,
            provider.model,
            "unreported",
            None,
        )

    def _completion_usage(self, response: RoleProviderResponseV5) -> RoleUsageFactsV5:
        provider = self._manifest.provider
        assert provider is not None
        completion = response.completion
        return RoleUsageFactsV5(
            completion.external_attempt_count,
            True,
            completion.response_received,
            completion.input_tokens,
            completion.output_tokens,
            completion.input_tokens + completion.output_tokens,
            completion.cost_usd,
            provider.model,
            completion.returned_model,
            completion.provider_request_id,
        )

    def _derive_reported_terminal(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        request: RoleRequestV5,
        response: RoleProviderResponseV5,
    ) -> tuple[RoleAttemptFactsV5, ParsedRoleArtifactV5 | None]:
        """Re-run the live runner's exact reported-response classification."""

        provider = self._manifest.provider
        assert provider is not None
        if (
            type(request) is not RoleRequestV5
            or response.campaign_id != self._manifest.campaign_id
            or response.campaign_manifest_sha256 != self.campaign_manifest_sha256
            or response.ledger_identity_sha256 != self.ledger_identity_sha256
            or response.audit_store_identity_sha256 != self.audit_store_identity_sha256
            or response.request_sha256 != request.sha256
            or slot.request.request_sha256 != request.sha256
            or slot.request.role != request.role
            or slot.request.max_output_tokens != request.max_output_tokens
            or slot.request.response_schema_sha256 != request.response_schema_sha256
        ):
            raise ValueError("local V5 reported role response authority differs")
        usage = self._completion_usage(response)
        completion = response.completion
        overage = (
            completion.returned_model != slot.request.model
            or completion.output_tokens > slot.request.max_output_tokens
            or slot.prior_external_attempts + usage.external_attempt_count > provider.maximum_role_calls
            or slot.prior_total_tokens + usage.total_tokens > provider.maximum_total_tokens
            or (provider.maximum_usd is not None and slot.prior_cost_usd + usage.cost_usd > provider.maximum_usd)
        )
        artifact: ParsedRoleArtifactV5 | None = None
        failure_code: RoleFailureCode | None
        if overage:
            failure_code = RoleFailureCode.ACCOUNTING
        elif not completion.accepted:
            failure_code = RoleFailureCode.TRANSPORT
        else:
            try:
                artifact = parse_and_bind_role_artifact(
                    request=request,
                    response_text=completion.response_text,
                )
            except RoleFailureV5 as failure:
                failure_code = failure.code
            except BaseException:
                failure_code = RoleFailureCode.RESPONSE_SCHEMA
            else:
                failure_code = None
        outcome = {
            None: "accepted",
            RoleFailureCode.TRANSPORT: "transport_failure",
            RoleFailureCode.RESPONSE_SCHEMA: "response_schema_failure",
            RoleFailureCode.EVIDENCE_BINDING: "evidence_binding_failure",
            RoleFailureCode.AUTHORIZATION: "authorization_failure",
            RoleFailureCode.ACCOUNTING: "accounting_failure",
        }[failure_code]
        try:
            response_sha256 = canonical_role_response_sha256_v5(completion.response_text)
        except ValueError:
            response_sha256 = None
        facts = RoleAttemptFactsV5(
            request.role,
            slot.request.attempt_kind,
            slot.request.attempt_index,
            request.sha256,
            slot.slot_id,
            outcome,  # type: ignore[arg-type]
            failure_code,
            usage,
            response_sha256,
            None if artifact is None else canonical_sha256_v5(artifact),
        )
        return facts, artifact

    def _verify_terminal_facts(
        self,
        *,
        reservation: RoleLedgerReservationV5,
        terminal: RoleLedgerTerminalV5,
        request: RoleRequestV5,
    ) -> None:
        """Recompute all provider-derived facts and the exact parsed artifact."""

        slot = reservation.slot
        facts = terminal.facts
        response = self._repository.load_role_provider_response(
            campaign_id=self._manifest.campaign_id,
            request_sha256=slot.request.request_sha256,
        )
        if response is None:
            if facts.usage.external_attempt_count == 1:
                if (
                    facts.usage != self._conservative_unreported_usage(slot)
                    or facts.outcome not in {"transport_failure", "accounting_failure"}
                    or facts.response_sha256 is not None
                    or terminal.artifact is not None
                ):
                    raise ValueError("local V5 unreported role terminal facts differ")
                return
            if (
                facts.usage.external_attempt_count != 0
                or facts.outcome not in {"authorization_failure", "accounting_failure"}
                or facts.response_sha256 is not None
                or terminal.artifact is not None
            ):
                raise ValueError("local V5 provider-free role terminal facts differ")
            return
        expected_facts, expected_artifact = self._derive_reported_terminal(
            slot=slot,
            request=request,
            response=response,
        )
        if terminal.facts != expected_facts or terminal.artifact != expected_artifact:
            raise ValueError("local V5 reported role terminal classification differs")

    def _load_verified(
        self,
    ) -> tuple[
        tuple[RoleLedgerReservationV5, ...],
        tuple[RoleLedgerTerminalV5, ...],
    ]:
        provider = self._manifest.provider
        assert provider is not None
        reservations, terminals = self._repository.load_role_ledger_records(
            campaign_id=self._manifest.campaign_id,
            migration_authority=RoleLedgerMigrationAuthorityV5(
                campaign_id=self._manifest.campaign_id,
                campaign_manifest_sha256=self.campaign_manifest_sha256,
                ledger_identity_sha256=self.ledger_identity_sha256,
                audit_store_identity_sha256=self.audit_store_identity_sha256,
                model=provider.model,
                maximum_output_tokens_per_role=provider.maximum_output_tokens_per_role,
            ),
        )
        by_reservation = {item.sha256: item for item in reservations}
        if len(by_reservation) != len(reservations):
            raise ValueError("local V5 role ledger has duplicate reservations")
        by_slot = {item.slot.slot_id: item for item in reservations}
        if len(by_slot) != len(reservations):
            raise ValueError("local V5 role ledger has duplicate slots")
        requests_by_slot: dict[str, RoleRequestV5] = {}
        terminal_slots: set[str] = set()
        for reservation in reservations:
            request = self._repository.load_unique_role_request_by_sha256(reservation.slot.request.request_sha256)
            if (
                reservation.campaign_id != self._manifest.campaign_id
                or reservation.campaign_manifest_sha256 != self.campaign_manifest_sha256
                or reservation.ledger_identity_sha256 != self.ledger_identity_sha256
                or reservation.audit_store_identity_sha256 != self.audit_store_identity_sha256
                or reservation.slot.request.model != provider.model
                or reservation.slot.request.max_output_tokens != provider.maximum_output_tokens_per_role
                or reservation.slot.request.role != request.role
                or reservation.slot.request.max_output_tokens != request.max_output_tokens
                or reservation.slot.request.response_schema_sha256 != request.response_schema_sha256
                or (
                    reservation.record_schema_revision == 3
                    and reservation.invocation_claim.lease_deadline_epoch_ms
                    - reservation.invocation_claim.lease_started_epoch_ms
                    != self._manifest.resources.role_call_timeout_seconds * 1000
                )
            ):
                raise ValueError("local V5 role-ledger reservation authority differs")
            requests_by_slot[reservation.slot.slot_id] = request
            expected_slot_id = canonical_sha256_v5(
                {
                    "domain": "pit-optimizer-v5-role-slot-id-v1",
                    "campaign_manifest_sha256": self.campaign_manifest_sha256,
                    "request_sha256": reservation.slot.request.sha256,
                }
            )
            expected_authorization = canonical_sha256_v5(
                {
                    "domain": "pit-optimizer-v5-role-slot-v1",
                    "ledger_identity_sha256": self.ledger_identity_sha256,
                    "request_sha256": reservation.slot.request.sha256,
                    "terminal_sequence": reservation.slot.prior_terminal_sequence + 1,
                }
            )
            if (
                reservation.slot.slot_id != expected_slot_id
                or reservation.slot.authorization_sha256 != expected_authorization
            ):
                raise ValueError("local V5 role-ledger reservation binding differs")
        for terminal in terminals:
            reservation = by_reservation.get(terminal.reservation_sha256)
            if reservation is None or terminal.receipt.slot_id != reservation.slot.slot_id:
                raise ValueError("local V5 role-ledger terminal lacks its reservation")
            if terminal.receipt.slot_id in terminal_slots:
                raise ValueError("local V5 role ledger has duplicate terminals")
            terminal_slots.add(terminal.receipt.slot_id)
        ordered = tuple(sorted(terminals, key=lambda item: item.receipt.terminal_sequence))
        if tuple(item.receipt.terminal_sequence for item in ordered) != tuple(range(1, len(ordered) + 1)):
            raise ValueError("local V5 role-ledger terminal chain is discontinuous")
        ordered_reservations = tuple(sorted(reservations, key=lambda item: item.ledger_ordinal))
        if tuple(item.ledger_ordinal for item in ordered_reservations) != tuple(
            range(1, len(ordered_reservations) + 1)
        ):
            raise ValueError("local V5 role-ledger reservation sequence is discontinuous")
        for index, reservation in enumerate(ordered_reservations):
            slot = reservation.slot
            prior = ordered[index - 1].receipt if index else None
            if (
                slot.prior_external_attempts != (0 if prior is None else prior.cumulative_external_attempts)
                or slot.prior_total_tokens != (0 if prior is None else prior.cumulative_total_tokens)
                or slot.prior_cost_usd != (Decimal("0") if prior is None else prior.cumulative_cost_usd)
                or slot.prior_terminal_sequence != index
            ):
                raise ValueError("local V5 role-ledger reservation prior totals differ")
            if index < len(ordered):
                terminal = ordered[index]
                expected_receipt_payload = {
                    "slot_id": slot.slot_id,
                    "slot_request_sha256": slot.request.sha256,
                    "authorization_sha256": slot.authorization_sha256,
                    "attempt_facts_sha256": terminal.facts.sha256,
                    "cumulative_external_attempts": (
                        slot.prior_external_attempts + terminal.facts.usage.external_attempt_count
                    ),
                    "cumulative_total_tokens": slot.prior_total_tokens + terminal.facts.usage.total_tokens,
                    "cumulative_cost_usd": slot.prior_cost_usd + terminal.facts.usage.cost_usd,
                    "terminal_sequence": slot.prior_terminal_sequence + 1,
                }
                expected_receipt = RoleTerminalReceiptV5(
                    **expected_receipt_payload,
                    receipt_sha256=canonical_sha256_v5(expected_receipt_payload),
                )
                if (
                    terminal.reservation_sha256 != reservation.sha256
                    or terminal.facts.role != slot.request.role
                    or terminal.facts.attempt_kind != slot.request.attempt_kind
                    or terminal.facts.attempt_index != slot.request.attempt_index
                    or terminal.facts.request_sha256 != slot.request.request_sha256
                    or terminal.facts.slot_id != slot.slot_id
                    or terminal.receipt != expected_receipt
                ):
                    raise ValueError("local V5 role-ledger terminal differs from its slot")
                self._verify_terminal_facts(
                    reservation=reservation,
                    terminal=terminal,
                    request=requests_by_slot[slot.slot_id],
                )
        return ordered_reservations, ordered

    def reserve_role_slot(self, request: RoleSlotRequestV5) -> AuthorizedRoleSlotV5:
        if type(request) is not RoleSlotRequestV5:
            raise ValueError("local V5 role-ledger slot request is invalid")
        with self._repository.role_provider_transition(campaign_id=self._manifest.campaign_id):
            reservations, terminals = self._load_verified()
            if any(item.slot.request == request for item in reservations):
                raise ValueError("local V5 role-ledger slot is already reserved")
            if len(terminals) != len(reservations):
                raise ValueError("local V5 role-ledger prior reservation is incomplete")
            prior_attempts = terminals[-1].receipt.cumulative_external_attempts if terminals else 0
            prior_tokens = terminals[-1].receipt.cumulative_total_tokens if terminals else 0
            prior_cost = terminals[-1].receipt.cumulative_cost_usd if terminals else Decimal("0")
            prior_sequence = terminals[-1].receipt.terminal_sequence if terminals else 0
            authorization = canonical_sha256_v5(
                {
                    "domain": "pit-optimizer-v5-role-slot-v1",
                    "ledger_identity_sha256": self.ledger_identity_sha256,
                    "request_sha256": request.sha256,
                    "terminal_sequence": prior_sequence + 1,
                }
            )
            slot = AuthorizedRoleSlotV5(
                slot_id=canonical_sha256_v5(
                    {
                        "domain": "pit-optimizer-v5-role-slot-id-v1",
                        "campaign_manifest_sha256": self.campaign_manifest_sha256,
                        "request_sha256": request.sha256,
                    }
                ),
                request=request,
                authorization_sha256=authorization,
                prior_external_attempts=prior_attempts,
                prior_total_tokens=prior_tokens,
                prior_cost_usd=prior_cost,
                prior_terminal_sequence=prior_sequence,
            )
            lease_started = self._now_ms()
            record = RoleLedgerReservationV5(
                schema_version=5,
                record_schema_revision=3,
                ledger_ordinal=len(reservations) + 1,
                campaign_id=self._manifest.campaign_id,
                campaign_manifest_sha256=self.campaign_manifest_sha256,
                ledger_identity_sha256=self.ledger_identity_sha256,
                audit_store_identity_sha256=self.audit_store_identity_sha256,
                slot=slot,
                invocation_claim=RoleInvocationClaimV5(
                    owner_sha256=self._invocation_owner_sha256,
                    lease_started_epoch_ms=lease_started,
                    lease_deadline_epoch_ms=(lease_started + self._manifest.resources.role_call_timeout_seconds * 1000),
                ),
            )
            self._repository.append_role_ledger_reservation(record)
            self._reservation(slot)
            return slot

    def publish_role_provider_response(
        self,
        response: RoleProviderResponseV5,
    ) -> RoleProviderResponseV5 | None:
        """Atomically publish only while this owner still has no terminal."""

        if type(response) is not RoleProviderResponseV5:
            raise ValueError("local V5 role response publication is invalid")
        with self._repository.role_provider_transition(campaign_id=self._manifest.campaign_id):
            reservations, terminals = self._load_verified()
            matches = tuple(
                item for item in reservations if item.slot.request.request_sha256 == response.request_sha256
            )
            if len(matches) != 1:
                raise ValueError("local V5 role response lacks one reservation")
            reservation = matches[0]
            slot_terminals = tuple(item for item in terminals if item.receipt.slot_id == reservation.slot.slot_id)
            existing_response = self._repository.load_role_provider_response(
                campaign_id=self._manifest.campaign_id,
                request_sha256=response.request_sha256,
            )
            if slot_terminals:
                if len(slot_terminals) != 1:
                    raise ValueError("local V5 role response terminal is ambiguous")
                return existing_response
            if reservation.invocation_claim.owner_sha256 != self._invocation_owner_sha256:
                return None
            if existing_response is not None:
                request = self._repository.load_unique_role_request_by_sha256(response.request_sha256)
                self._derive_reported_terminal(
                    slot=reservation.slot,
                    request=request,
                    response=existing_response,
                )
                return existing_response
            authoritative_now_ms = self._now_ms()
            if authoritative_now_ms > reservation.invocation_claim.lease_deadline_epoch_ms:
                self._settle_unreported_role_slot_locked(
                    slot=reservation.slot,
                    failure_code=RoleFailureCode.ACCOUNTING,
                    authoritative_now_ms=authoritative_now_ms,
                )
                return None
            request = self._repository.load_unique_role_request_by_sha256(response.request_sha256)
            self._derive_reported_terminal(slot=reservation.slot, request=request, response=response)
            reference = self._repository.append_role_provider_response(response)
            recovered = self._repository.load_typed_artifact(
                reference,
                value_type=RoleProviderResponseV5,
            )
            if recovered != response:
                raise ValueError("local V5 role response publication differs")
            return recovered

    def _reservation(self, slot: AuthorizedRoleSlotV5) -> RoleLedgerReservationV5:
        reservations, _ = self._load_verified()
        matches = tuple(item for item in reservations if item.slot.slot_id == slot.slot_id)
        if len(matches) != 1 or matches[0].slot != slot:
            raise ValueError("local V5 role-ledger slot is absent or different")
        return matches[0]

    def verify_role_slot(self, slot: AuthorizedRoleSlotV5) -> None:
        if type(slot) is not AuthorizedRoleSlotV5:
            raise ValueError("local V5 role-ledger slot is invalid")
        self._reservation(slot)

    def settle_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        artifact: ParsedRoleArtifactV5 | None,
    ) -> RoleTerminalReceiptV5:
        with self._repository.role_provider_transition(campaign_id=self._manifest.campaign_id):
            return self._settle_role_slot_locked(
                slot=slot,
                facts=facts,
                artifact=artifact,
                allow_foreign_owner=False,
            )

    def _settle_role_slot_locked(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        artifact: ParsedRoleArtifactV5 | None,
        allow_foreign_owner: bool,
    ) -> RoleTerminalReceiptV5:
        reservation = self._reservation(slot)
        if type(facts) is not RoleAttemptFactsV5 or facts.slot_id != slot.slot_id:
            raise ValueError("local V5 role-ledger settlement facts differ")
        if (facts.outcome == "accepted") != (artifact is not None):
            raise ValueError("local V5 role-ledger settlement artifact differs")
        _, terminals = self._load_verified()
        existing = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if existing:
            if len(existing) != 1 or existing[0].facts != facts or existing[0].artifact != artifact:
                raise ValueError("local V5 role-ledger settlement conflicts")
            return existing[0].receipt
        if not allow_foreign_owner and reservation.invocation_claim.owner_sha256 != self._invocation_owner_sha256:
            raise ValueError("local V5 role-ledger settlement owner differs")
        receipt_payload = {
            "slot_id": slot.slot_id,
            "slot_request_sha256": slot.request.sha256,
            "authorization_sha256": slot.authorization_sha256,
            "attempt_facts_sha256": facts.sha256,
            "cumulative_external_attempts": slot.prior_external_attempts + facts.usage.external_attempt_count,
            "cumulative_total_tokens": slot.prior_total_tokens + facts.usage.total_tokens,
            "cumulative_cost_usd": slot.prior_cost_usd + facts.usage.cost_usd,
            "terminal_sequence": slot.prior_terminal_sequence + 1,
        }
        receipt = RoleTerminalReceiptV5(
            **receipt_payload,
            receipt_sha256=canonical_sha256_v5(receipt_payload),
        )
        terminal = RoleLedgerTerminalV5(5, reservation.sha256, facts, receipt, artifact)
        self._repository.append_role_ledger_terminal(
            self._manifest.campaign_id,
            slot.slot_id,
            terminal,
        )
        _, persisted = self._load_verified()
        matches = tuple(item for item in persisted if item.receipt.slot_id == slot.slot_id)
        if len(matches) != 1 or matches[0] != terminal:
            raise ValueError("local V5 role-ledger terminal publication differs")
        return receipt

    def settle_unreported_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
        failure_code: RoleFailureCode,
    ) -> RecoveredRoleTerminalV5:
        with self._repository.role_provider_transition(campaign_id=self._manifest.campaign_id):
            return self._settle_unreported_role_slot_locked(slot=slot, failure_code=failure_code)

    def _settle_unreported_role_slot_locked(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        failure_code: RoleFailureCode,
        authoritative_now_ms: int | None = None,
    ) -> RecoveredRoleTerminalV5:
        outcomes = {
            RoleFailureCode.TRANSPORT: "transport_failure",
            RoleFailureCode.ACCOUNTING: "accounting_failure",
        }
        if failure_code not in outcomes:
            raise ValueError("local V5 role-ledger failure code is invalid")
        reservation = self._reservation(slot)
        _, terminals = self._load_verified()
        existing = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if existing:
            if len(existing) != 1:
                raise ValueError("local V5 role-ledger terminal is ambiguous")
            return RecoveredRoleTerminalV5(existing[0].facts, existing[0].receipt)
        owner = reservation.invocation_claim.owner_sha256 == self._invocation_owner_sha256
        observed_now_ms = self._now_ms() if authoritative_now_ms is None else authoritative_now_ms
        if type(observed_now_ms) is not int or observed_now_ms < 0:
            raise ValueError("local V5 role invocation settlement time is invalid")
        expired = reservation.invocation_claim.lease_deadline_epoch_ms < observed_now_ms
        if not owner and not expired:
            raise ValueError("local V5 role invocation is still live")
        # Crossing the transport boundary without a complete result accounts
        # exactly one possibly-started request; an expired foreign claim is
        # the only reconciler path allowed to publish this terminal.
        conservative_usage = self._conservative_unreported_usage(slot)
        facts = RoleAttemptFactsV5(
            slot.request.role,
            slot.request.attempt_kind,
            slot.request.attempt_index,
            slot.request.request_sha256,
            slot.slot_id,
            outcomes[failure_code],  # type: ignore[arg-type]
            failure_code,
            conservative_usage,
            None,
            None,
        )
        receipt = self._settle_role_slot_locked(
            slot=slot,
            facts=facts,
            artifact=None,
            allow_foreign_owner=expired,
        )
        return RecoveredRoleTerminalV5(facts, receipt)

    def recover_role_slot(self, slot: AuthorizedRoleSlotV5) -> RecoveredRoleTerminalV5 | None:
        self._reservation(slot)
        _, terminals = self._load_verified()
        matches = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if len(matches) > 1:
            raise ValueError("local V5 role-ledger terminal is ambiguous")
        return None if not matches else RecoveredRoleTerminalV5(matches[0].facts, matches[0].receipt)

    def verify_role_slot_receipt(
        self,
        slot: AuthorizedRoleSlotV5,
        facts: RoleAttemptFactsV5,
        receipt: RoleTerminalReceiptV5,
    ) -> None:
        self._reservation(slot)
        _, terminals = self._load_verified()
        matches = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if len(matches) != 1 or matches[0].facts != facts or matches[0].receipt != receipt:
            raise ValueError("local V5 role-ledger receipt is absent or different")

    def reconcile_paid_role(
        self,
        persisted_request: ExistingPersistedRoleRequestV5,
    ) -> RoleReconciliationResultV5:
        if type(persisted_request) is not ExistingPersistedRoleRequestV5:
            raise ValueError("local V5 role reconciliation request is invalid")
        reservations, terminals = self._load_verified()
        matching_reservations = tuple(
            item
            for item in reservations
            if item.slot.request.request_sha256 == persisted_request.request.sha256
            and item.slot.request.role == persisted_request.call.role
            and item.slot.request.attempt_kind == persisted_request.call.attempt_kind
            and item.slot.request.attempt_index == persisted_request.call.attempt_index
            and item.slot.request.max_output_tokens == persisted_request.request.max_output_tokens
            and item.slot.request.response_schema_sha256 == persisted_request.request.response_schema_sha256
        )
        if len(matching_reservations) != 1:
            return RoleReconciliationFailureV5(persisted_request.call, "authority_unavailable")
        reservation = matching_reservations[0]
        slot = reservation.slot
        matching_terminals = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if not matching_terminals:
            try:
                with self._repository.role_provider_transition(campaign_id=self._manifest.campaign_id):
                    reservations, terminals = self._load_verified()
                    current = tuple(item for item in reservations if item.slot.slot_id == slot.slot_id)
                    if len(current) != 1 or current[0] != reservation:
                        raise ValueError("local V5 role reconciliation reservation changed")
                    matching_terminals = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
                    if not matching_terminals:
                        response = self._repository.load_role_provider_response(
                            campaign_id=self._manifest.campaign_id,
                            request_sha256=persisted_request.request.sha256,
                        )
                        if response is not None:
                            self._settle_recovered_response_locked(
                                slot=slot,
                                request=persisted_request.request,
                                response=response,
                            )
                        else:
                            authoritative_now_ms = self._now_ms()
                            if reservation.invocation_claim.lease_deadline_epoch_ms >= authoritative_now_ms:
                                return RoleReconciliationFailureV5(persisted_request.call, "pending")
                            self._settle_unreported_role_slot_locked(
                                slot=slot,
                                failure_code=RoleFailureCode.ACCOUNTING,
                                authoritative_now_ms=authoritative_now_ms,
                            )
            except BaseException:
                return RoleReconciliationFailureV5(
                    persisted_request.call,
                    "authority_unavailable",
                )
            _, terminals = self._load_verified()
            matching_terminals = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if len(matching_terminals) != 1:
            return RoleReconciliationFailureV5(persisted_request.call, "terminal_unavailable")
        terminal = matching_terminals[0]
        artifact = terminal.artifact
        if terminal.facts.outcome == "accepted":
            if artifact is None:
                return RoleReconciliationFailureV5(persisted_request.call, "terminal_incomplete")
            persisted_artifact = parsed_role_artifact_primitive_v5(artifact)
            response_envelope = {
                "artifact": persisted_artifact["artifact"],
                "binding": persisted_request.request.expected_binding.to_primitive(),
            }
            response_text = canonical_json_bytes_v5(response_envelope).decode("utf-8")
            try:
                recovered_artifact = parse_and_bind_role_artifact(
                    request=persisted_request.request,
                    response_text=response_text,
                )
            except BaseException:
                return RoleReconciliationFailureV5(persisted_request.call, "terminal_incomplete")
            if (
                recovered_artifact != artifact
                or terminal.facts.response_sha256 != canonical_sha256_v5(response_envelope)
                or terminal.facts.artifact_sha256 != canonical_sha256_v5(artifact)
            ):
                return RoleReconciliationFailureV5(persisted_request.call, "terminal_incomplete")
        return RoleInvocationPackageV5(
            persisted_request.call,
            persisted_request.request,
            terminal.facts,
            LedgerRoleTerminalAuthorityV5(persisted_request.call.sha256, terminal.receipt),
            artifact,
        )

    def _settle_recovered_response_locked(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        request: RoleRequestV5,
        response: RoleProviderResponseV5,
    ) -> None:
        facts, artifact = self._derive_reported_terminal(
            slot=slot,
            request=request,
            response=response,
        )
        self._settle_role_slot_locked(
            slot=slot,
            facts=facts,
            artifact=artifact,
            allow_foreign_owner=True,
        )


__all__ = ["LocalRoleAuthorizationLedgerV5", "OpenRouterOneShotJsonCompletionV5"]
