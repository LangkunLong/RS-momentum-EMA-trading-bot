"""Concrete local provider authorization and reconciliation for PIT optimizer V5."""

from __future__ import annotations

from decimal import Decimal
import re
from typing import Mapping

from core.pit_optimizer_v5.artifacts import LocalArtifactRepositoryV5
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
    RoleLedgerReservationV5,
    RoleLedgerTerminalV5,
    RoleProviderResponseV5,
    RoleReconciliationFailureV5,
    RoleReconciliationResultV5,
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
        "audit_store_identity_sha256",
        "gateway_identity_sha256",
        "ledger_identity_sha256",
    )

    def __init__(self, *, ledger: LocalRoleAuthorizationLedgerV5) -> None:
        if type(ledger) is not LocalRoleAuthorizationLedgerV5:
            raise ValueError("OpenRouter V5 gateway requires the concrete local role ledger")
        self._ledger = ledger
        self.ledger_identity_sha256 = ledger.ledger_identity_sha256
        self.audit_store_identity_sha256 = ledger.audit_store_identity_sha256
        self.gateway_identity_sha256 = canonical_sha256_v5(
            {
                "domain": "pit-optimizer-v5-openrouter-one-shot-v1",
                "transport": "agent_loop.OpenRouterGateway.request_pit_optimizer_v5_json_once",
                "ledger_identity_sha256": self.ledger_identity_sha256,
                "audit_store_identity_sha256": self.audit_store_identity_sha256,
            }
        )

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

        gateway = OpenRouterGateway(
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
        reference = self._ledger._repository.append_role_provider_response(response)
        recovered = self._ledger._repository.load_typed_artifact(
            reference,
            value_type=RoleProviderResponseV5,
        )
        if recovered != response:
            raise ValueError("OpenRouter V5 response publication differs")
        return recovered.completion


class LocalRoleAuthorizationLedgerV5:
    """Create-only local ledger implementing reserve, settle, recovery, and reconciliation."""

    def __init__(self, *, repository: LocalArtifactRepositoryV5, manifest: CampaignManifestV5) -> None:
        if type(repository) is not LocalArtifactRepositoryV5 or type(manifest) is not CampaignManifestV5:
            raise ValueError("local V5 role-ledger authority is invalid")
        if manifest.provider is None:
            raise ValueError("local V5 role ledger requires provider capabilities")
        self._repository = repository
        self._manifest = manifest
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

    def _load_verified(
        self,
    ) -> tuple[
        tuple[RoleLedgerReservationV5, ...],
        tuple[RoleLedgerTerminalV5, ...],
    ]:
        reservations, terminals = self._repository.load_role_ledger_records(
            campaign_id=self._manifest.campaign_id
        )
        by_reservation = {item.sha256: item for item in reservations}
        if len(by_reservation) != len(reservations):
            raise ValueError("local V5 role ledger has duplicate reservations")
        by_slot = {item.slot.slot_id: item for item in reservations}
        if len(by_slot) != len(reservations):
            raise ValueError("local V5 role ledger has duplicate slots")
        terminal_slots: set[str] = set()
        for reservation in reservations:
            if (
                reservation.campaign_id != self._manifest.campaign_id
                or reservation.campaign_manifest_sha256 != self.campaign_manifest_sha256
                or reservation.ledger_identity_sha256 != self.ledger_identity_sha256
                or reservation.audit_store_identity_sha256 != self.audit_store_identity_sha256
            ):
                raise ValueError("local V5 role-ledger reservation authority differs")
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
        ordered_reservations = tuple(sorted(reservations, key=lambda item: item.slot.request.attempt_index))
        if tuple(item.slot.request.attempt_index for item in ordered_reservations) != tuple(
            range(1, len(ordered_reservations) + 1)
        ):
            raise ValueError("local V5 role-ledger reservation sequence is discontinuous")
        for index, reservation in enumerate(ordered_reservations):
            slot = reservation.slot
            prior = ordered[index - 1].receipt if index else None
            if (
                slot.prior_external_attempts
                != (0 if prior is None else prior.cumulative_external_attempts)
                or slot.prior_total_tokens != (0 if prior is None else prior.cumulative_total_tokens)
                or slot.prior_cost_usd != (Decimal("0") if prior is None else prior.cumulative_cost_usd)
                or slot.prior_terminal_sequence != index
            ):
                raise ValueError("local V5 role-ledger reservation prior totals differ")
            if index < len(ordered):
                terminal = ordered[index]
                if (
                    terminal.reservation_sha256 != reservation.sha256
                    or terminal.facts.role != slot.request.role
                    or terminal.facts.attempt_kind != slot.request.attempt_kind
                    or terminal.facts.attempt_index != slot.request.attempt_index
                    or terminal.facts.request_sha256 != slot.request.request_sha256
                    or terminal.facts.slot_id != slot.slot_id
                ):
                    raise ValueError("local V5 role-ledger terminal differs from its slot")
        return ordered_reservations, ordered

    def reserve_role_slot(self, request: RoleSlotRequestV5) -> AuthorizedRoleSlotV5:
        if type(request) is not RoleSlotRequestV5:
            raise ValueError("local V5 role-ledger slot request is invalid")
        reservations, terminals = self._load_verified()
        if any(item.slot.request == request for item in reservations):
            raise ValueError("local V5 role-ledger slot is already reserved")
        if len(terminals) != len(reservations) or request.attempt_index != len(reservations) + 1:
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
        record = RoleLedgerReservationV5(
            5,
            self._manifest.campaign_id,
            self.campaign_manifest_sha256,
            self.ledger_identity_sha256,
            self.audit_store_identity_sha256,
            slot,
        )
        self._repository.append_role_ledger_reservation(record)
        self.verify_role_slot(slot)
        return slot

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
        self.verify_role_slot_receipt(slot, facts, receipt)
        return receipt

    def settle_unreported_role_slot(
        self,
        slot: AuthorizedRoleSlotV5,
        failure_code: RoleFailureCode,
    ) -> RecoveredRoleTerminalV5:
        outcomes = {
            RoleFailureCode.TRANSPORT: "transport_failure",
            RoleFailureCode.RESPONSE_SCHEMA: "response_schema_failure",
            RoleFailureCode.EVIDENCE_BINDING: "evidence_binding_failure",
            RoleFailureCode.AUTHORIZATION: "authorization_failure",
            RoleFailureCode.ACCOUNTING: "accounting_failure",
        }
        if failure_code not in outcomes:
            raise ValueError("local V5 role-ledger failure code is invalid")
        provider = self._manifest.provider
        assert provider is not None
        remaining_tokens = provider.maximum_total_tokens - slot.prior_total_tokens
        if remaining_tokens < 0:
            raise ValueError("local V5 role-ledger prior token accounting exceeds authority")
        remaining_cost = (
            Decimal("0")
            if provider.maximum_usd is None
            else provider.maximum_usd - slot.prior_cost_usd
        )
        if remaining_cost < 0:
            raise ValueError("local V5 role-ledger prior cost accounting exceeds authority")
        # Once control crossed the transport boundary without a complete result,
        # exhaust the remaining bounded authorization. This never understates a
        # possibly-started request and prevents a silent replacement call.
        conservative_usage = RoleUsageFactsV5(
            1,
            True,
            False,
            remaining_tokens,
            0,
            remaining_tokens,
            remaining_cost,
            provider.model,
            "unreported",
            None,
        )
        facts = RoleAttemptFactsV5(
            slot.request.role,
            slot.request.attempt_kind,
            slot.request.attempt_index,
            slot.request.request_sha256,
            slot.slot_id,
            "accounting_failure",
            RoleFailureCode.ACCOUNTING,
            conservative_usage,
            None,
            None,
        )
        receipt = self.settle_role_slot(slot, facts, None)
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
        )
        if len(matching_reservations) != 1:
            return RoleReconciliationFailureV5(persisted_request.call, "authority_unavailable")
        slot = matching_reservations[0].slot
        matching_terminals = tuple(item for item in terminals if item.receipt.slot_id == slot.slot_id)
        if not matching_terminals:
            response = self._repository.load_role_provider_response(
                campaign_id=self._manifest.campaign_id,
                request_sha256=persisted_request.request.sha256,
            )
            if response is not None:
                try:
                    self._settle_recovered_response(
                        slot=slot,
                        request=persisted_request.request,
                        response=response,
                    )
                    _, terminals = self._load_verified()
                    matching_terminals = tuple(
                        item for item in terminals if item.receipt.slot_id == slot.slot_id
                    )
                except BaseException:
                    return RoleReconciliationFailureV5(
                        persisted_request.call,
                        "authority_unavailable",
                    )
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
                or terminal.facts.response_sha256
                != canonical_sha256_v5(response_envelope)
                or terminal.facts.artifact_sha256
                != canonical_sha256_v5(artifact)
            ):
                return RoleReconciliationFailureV5(persisted_request.call, "terminal_incomplete")
        return RoleInvocationPackageV5(
            persisted_request.call,
            persisted_request.request,
            terminal.facts,
            LedgerRoleTerminalAuthorityV5(persisted_request.call.sha256, terminal.receipt),
            artifact,
        )

    def _settle_recovered_response(
        self,
        *,
        slot: AuthorizedRoleSlotV5,
        request: object,
        response: RoleProviderResponseV5,
    ) -> None:
        from core.pit_optimizer_v5.provider import RoleRequestV5

        provider = self._manifest.provider
        if (
            type(request) is not RoleRequestV5
            or provider is None
            or response.campaign_id != self._manifest.campaign_id
            or response.campaign_manifest_sha256 != self.campaign_manifest_sha256
            or response.ledger_identity_sha256 != self.ledger_identity_sha256
            or response.audit_store_identity_sha256 != self.audit_store_identity_sha256
            or response.request_sha256 != request.sha256
        ):
            raise ValueError("recovered provider response authority differs")
        completion = response.completion
        usage = RoleUsageFactsV5(
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
        exceeds = (
            completion.returned_model != provider.model
            or completion.output_tokens > request.max_output_tokens
            or slot.prior_external_attempts + usage.external_attempt_count > provider.maximum_role_calls
            or slot.prior_total_tokens + usage.total_tokens > provider.maximum_total_tokens
            or (
                provider.maximum_usd is not None
                and slot.prior_cost_usd + usage.cost_usd > provider.maximum_usd
            )
        )
        artifact: ParsedRoleArtifactV5 | None = None
        failure_code: RoleFailureCode | None
        if exceeds:
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
        facts = RoleAttemptFactsV5(
            request.role,
            slot.request.attempt_kind,
            slot.request.attempt_index,
            request.sha256,
            slot.slot_id,
            outcome,  # type: ignore[arg-type]
            failure_code,
            usage,
            canonical_role_response_sha256_v5(completion.response_text),
            None if artifact is None else canonical_sha256_v5(artifact),
        )
        self.settle_role_slot(slot, facts, artifact)


__all__ = ["LocalRoleAuthorizationLedgerV5", "OpenRouterOneShotJsonCompletionV5"]
