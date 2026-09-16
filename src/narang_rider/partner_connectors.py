from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from .compatibility import CanonicalOrder, CompatibilityGateway, LegacyStatus, PartnerOrder
from .connector import CallerType, RiderCall, RiderCallService


class PartnerCapability(StrEnum):
    ORDER_CREATE = "ORDER_CREATE"
    STATUS_CALLBACK = "STATUS_CALLBACK"
    MERCHANT_RIDER_CALL = "MERCHANT_RIDER_CALL"
    COMPANY_RIDER_CALL = "COMPANY_RIDER_CALL"
    CANCEL = "CANCEL"


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True)
class PartnerConnectorConfig:
    partner_id: str
    display_name: str
    auth_ref: str
    callback_ref: str
    capabilities: frozenset[PartnerCapability]
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.auth_ref.startswith("vault:"):
            raise ValueError("PARTNER_AUTH_VAULT_REQUIRED")
        if not self.callback_ref.startswith("vault:"):
            raise ValueError("PARTNER_CALLBACK_VAULT_REQUIRED")
        if self.max_attempts < 1:
            raise ValueError("INVALID_MAX_ATTEMPTS")
        required = {PartnerCapability.ORDER_CREATE, PartnerCapability.STATUS_CALLBACK}
        if not required.issubset(self.capabilities):
            raise ValueError("REQUIRED_PARTNER_CAPABILITY_MISSING")


@dataclass(frozen=True)
class PartnerEnvelope:
    event_id: str
    external_order_id: str
    merchant_id: str
    amount_won: int
    pickup_ref: str
    dropoff_ref: str
    status: LegacyStatus = LegacyStatus.RECEIVED

    def __post_init__(self) -> None:
        if not self.event_id or not self.external_order_id:
            raise ValueError("PARTNER_EVENT_ID_REQUIRED")

    def digest(self) -> str:
        value = {
            "amount_won": self.amount_won,
            "dropoff_ref": self.dropoff_ref,
            "external_order_id": self.external_order_id,
            "merchant_id": self.merchant_id,
            "pickup_ref": self.pickup_ref,
            "status": self.status.value,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class OutboundEvent:
    event_id: str
    canonical_order_id: str
    status: LegacyStatus
    idempotency_key: str
    attempt_count: int = 0
    state: DeliveryState = DeliveryState.PENDING
    last_error_code: str | None = None


class IsolatedPartnerRuntime:
    """One failure domain per partner; no queue or circuit state is shared."""

    def __init__(self, config: PartnerConnectorConfig) -> None:
        self.config = config
        self._inbound: dict[str, tuple[str, CanonicalOrder]] = {}
        self._outbound: dict[str, OutboundEvent] = {}

    def remember_inbound(
        self, event_id: str, payload_digest: str, order: CanonicalOrder
    ) -> CanonicalOrder:
        previous = self._inbound.get(event_id)
        if previous:
            if previous[0] != payload_digest:
                raise ValueError("INBOUND_IDEMPOTENCY_CONFLICT")
            return previous[1]
        self._inbound[event_id] = (payload_digest, order)
        return order

    def enqueue(self, event: OutboundEvent) -> OutboundEvent:
        previous = self._outbound.get(event.idempotency_key)
        if previous:
            if (previous.canonical_order_id, previous.status) != (
                event.canonical_order_id,
                event.status,
            ):
                raise ValueError("OUTBOUND_IDEMPOTENCY_CONFLICT")
            return previous
        self._outbound[event.idempotency_key] = event
        return event

    def record_attempt(
        self, idempotency_key: str, *, delivered: bool, error_code: str | None = None
    ) -> OutboundEvent:
        event = self._outbound[idempotency_key]
        if event.state is not DeliveryState.PENDING:
            return event
        attempts = event.attempt_count + 1
        state = DeliveryState.DELIVERED if delivered else DeliveryState.PENDING
        if not delivered and attempts >= self.config.max_attempts:
            state = DeliveryState.DEAD_LETTER
        updated = replace(
            event,
            attempt_count=attempts,
            state=state,
            last_error_code=None if delivered else (error_code or "DELIVERY_FAILED"),
        )
        self._outbound[idempotency_key] = updated
        return updated

    def dead_letters(self) -> tuple[OutboundEvent, ...]:
        return tuple(
            event for event in self._outbound.values()
            if event.state is DeliveryState.DEAD_LETTER
        )


class IndependentConnectorHub:
    """Adds NARANG contracts without replacing existing partner delivery flows."""

    def __init__(self, configs: tuple[PartnerConnectorConfig, ...]) -> None:
        if len({config.partner_id for config in configs}) != len(configs):
            raise ValueError("DUPLICATE_PARTNER_ID")
        self._runtimes = {
            config.partner_id: IsolatedPartnerRuntime(config) for config in configs
        }
        self._gateway = CompatibilityGateway()
        self._rider_calls = RiderCallService()

    def capabilities(self, partner_id: str) -> frozenset[PartnerCapability]:
        return self._runtime(partner_id).config.capabilities

    def ingest(
        self, partner_id: str, envelope: PartnerEnvelope, *, canonical_id: str
    ) -> CanonicalOrder:
        runtime = self._runtime(partner_id)
        existing = runtime._inbound.get(envelope.event_id)
        digest = envelope.digest()
        if existing:
            if existing[0] != digest:
                raise ValueError("INBOUND_IDEMPOTENCY_CONFLICT")
            return existing[1]
        order = self._gateway.ingest(
            PartnerOrder(
                provider=partner_id,
                external_order_id=envelope.external_order_id,
                merchant_id=envelope.merchant_id,
                amount_won=envelope.amount_won,
                pickup_ref=envelope.pickup_ref,
                dropoff_ref=envelope.dropoff_ref,
                status=envelope.status,
            ),
            canonical_id=canonical_id,
        )
        return runtime.remember_inbound(envelope.event_id, digest, order)

    def request_rider(
        self,
        partner_id: str,
        *,
        call_id: str,
        order_id: str,
        caller_type: CallerType,
        caller_id: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> RiderCall:
        capability = (
            PartnerCapability.MERCHANT_RIDER_CALL
            if caller_type is CallerType.MERCHANT
            else PartnerCapability.COMPANY_RIDER_CALL
        )
        if capability not in self.capabilities(partner_id):
            raise ValueError("PARTNER_CAPABILITY_NOT_SUPPORTED")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self._rider_calls.create(
            RiderCall(
                call_id=call_id,
                order_id=order_id,
                caller_type=caller_type,
                caller_id=caller_id,
                idempotency_key=idempotency_key,
                payload_digest=digest,
                created_at=created_at or datetime.now(UTC),
            )
        )

    def publish_status(
        self,
        partner_id: str,
        *,
        event_id: str,
        canonical_order_id: str,
        status: LegacyStatus,
        idempotency_key: str,
    ) -> OutboundEvent:
        return self._runtime(partner_id).enqueue(
            OutboundEvent(event_id, canonical_order_id, status, idempotency_key)
        )

    def record_delivery(
        self,
        partner_id: str,
        idempotency_key: str,
        *,
        delivered: bool,
        error_code: str | None = None,
    ) -> OutboundEvent:
        return self._runtime(partner_id).record_attempt(
            idempotency_key, delivered=delivered, error_code=error_code
        )

    def dead_letters(self, partner_id: str) -> tuple[OutboundEvent, ...]:
        return self._runtime(partner_id).dead_letters()

    def _runtime(self, partner_id: str) -> IsolatedPartnerRuntime:
        try:
            return self._runtimes[partner_id]
        except KeyError as exc:
            raise ValueError("UNKNOWN_PARTNER") from exc


def ai_baebi_config() -> PartnerConnectorConfig:
    return PartnerConnectorConfig(
        partner_id="ai-baebi",
        display_name="ai배비",
        auth_ref="vault:partners/ai-baebi/auth",
        callback_ref="vault:partners/ai-baebi/callback",
        capabilities=frozenset(
            {
                PartnerCapability.ORDER_CREATE,
                PartnerCapability.STATUS_CALLBACK,
                PartnerCapability.MERCHANT_RIDER_CALL,
                PartnerCapability.COMPANY_RIDER_CALL,
                PartnerCapability.CANCEL,
            }
        ),
    )


def dosirak_store_config() -> PartnerConnectorConfig:
    return PartnerConnectorConfig(
        partner_id="dosirak-store",
        display_name="도시락.store",
        auth_ref="vault:partners/dosirak-store/auth",
        callback_ref="vault:partners/dosirak-store/callback",
        capabilities=frozenset(
            {
                PartnerCapability.ORDER_CREATE,
                PartnerCapability.STATUS_CALLBACK,
                PartnerCapability.MERCHANT_RIDER_CALL,
                PartnerCapability.COMPANY_RIDER_CALL,
                PartnerCapability.CANCEL,
            }
        ),
    )
