from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .network import BranchRegistry, BranchStatus


class OrderChannel(StrEnum):
    DIRECT = "DIRECT"
    PARTNER_API = "PARTNER_API"
    POS = "POS"
    CSV = "CSV"


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    branch_id: str
    active: bool


@dataclass(frozen=True)
class IntakeOrder:
    order_id: str
    merchant_id: str
    servicing_branch_id: str
    channel: OrderChannel
    external_order_id: str
    order_amount_won: int
    customer_contact_ref: str
    delivery_address_ref: str
    service_area_code: str
    received_at: datetime
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            not all(
                value.strip()
                for value in (
                    self.order_id,
                    self.merchant_id,
                    self.external_order_id,
                    self.customer_contact_ref,
                    self.delivery_address_ref,
                    self.service_area_code,
                )
            )
            or self.order_amount_won < 0
            or self.received_at.tzinfo is None
            or len(self.payload_digest) != 64
        ):
            raise ValueError("VALID_ORDER_INTAKE_REQUIRED")
        if not self.customer_contact_ref.startswith("vault:"):
            raise ValueError("RAW_CUSTOMER_CONTACT_FORBIDDEN")
        if not self.delivery_address_ref.startswith("vault:"):
            raise ValueError("RAW_DELIVERY_ADDRESS_FORBIDDEN")


class OrderIntakeService:
    def __init__(self, *, branches: BranchRegistry) -> None:
        self._branches = branches
        self._merchants: dict[str, Merchant] = {}
        self._orders: dict[str, IntakeOrder] = {}
        self._channel_keys: dict[tuple[OrderChannel, str], str] = {}

    def register_merchant(self, merchant: Merchant) -> Merchant:
        branch = self._branches.get(merchant.branch_id)
        if branch.status is not BranchStatus.ACTIVE:
            raise ValueError("ACTIVE_MERCHANT_BRANCH_REQUIRED")
        if merchant.merchant_id in self._merchants:
            raise ValueError("DUPLICATE_MERCHANT")
        self._merchants[merchant.merchant_id] = merchant
        return merchant

    def ingest(self, order: IntakeOrder) -> IntakeOrder:
        try:
            merchant = self._merchants[order.merchant_id]
        except KeyError as exc:
            raise ValueError("MERCHANT_NOT_FOUND") from exc
        if not merchant.active:
            raise ValueError("INACTIVE_MERCHANT")
        servicing_branch = self._branches.branch_for_area(order.service_area_code)
        if servicing_branch.status is not BranchStatus.ACTIVE:
            raise ValueError("ACTIVE_SERVICING_BRANCH_REQUIRED")
        if servicing_branch.branch_id != order.servicing_branch_id:
            raise ValueError("SERVICE_AREA_BRANCH_MISMATCH")
        key = (order.channel, order.external_order_id)
        existing_id = self._channel_keys.get(key)
        if existing_id is not None:
            existing = self._orders[existing_id]
            if existing.payload_digest != order.payload_digest:
                raise ValueError("CHANNEL_ORDER_IDEMPOTENCY_CONFLICT")
            return existing
        if order.order_id in self._orders:
            raise ValueError("DUPLICATE_INTERNAL_ORDER")
        self._orders[order.order_id] = order
        self._channel_keys[key] = order.order_id
        return order

    def get(self, order_id: str) -> IntakeOrder:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise ValueError("INTAKE_ORDER_NOT_FOUND") from exc


class AvailabilityStatus(StrEnum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    SAFETY_STOP = "SAFETY_STOP"


@dataclass(frozen=True)
class RiderAvailability:
    rider_id: str
    branch_id: str
    status: AvailabilityStatus
    available_since: datetime | None
    vehicle_type: str
    insurance_ref: str
    updated_at: datetime
    safety_stop_reason: str | None = None
    penalty_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            not all(value.strip() for value in (self.rider_id, self.branch_id, self.vehicle_type))
            or not self.insurance_ref.startswith("vault:")
            or self.updated_at.tzinfo is None
        ):
            raise ValueError("VALID_RIDER_AVAILABILITY_REQUIRED")
        if self.status is AvailabilityStatus.AVAILABLE and self.available_since is None:
            raise ValueError("AVAILABLE_SINCE_REQUIRED")
        if self.status is AvailabilityStatus.SAFETY_STOP and not self.safety_stop_reason:
            raise ValueError("SAFETY_STOP_REASON_REQUIRED")
        if self.penalty_allowed:
            raise ValueError("AVAILABILITY_CANNOT_AUTHORIZE_PENALTY")


class RiderAvailabilityService:
    def __init__(self, *, branches: BranchRegistry) -> None:
        self._branches = branches
        self._states: dict[str, RiderAvailability] = {}

    def register(self, value: RiderAvailability) -> RiderAvailability:
        branch = self._branches.get(value.branch_id)
        if branch.status is not BranchStatus.ACTIVE:
            raise ValueError("ACTIVE_RIDER_BRANCH_REQUIRED")
        if value.rider_id in self._states:
            raise ValueError("DUPLICATE_RIDER_AVAILABILITY")
        self._states[value.rider_id] = value
        return value

    def update(
        self,
        *,
        rider_id: str,
        status: AvailabilityStatus,
        now: datetime,
        safety_stop_reason: str | None = None,
    ) -> RiderAvailability:
        current = self.get(rider_id)
        updated = replace(
            current,
            status=status,
            available_since=now if status is AvailabilityStatus.AVAILABLE else None,
            updated_at=now,
            safety_stop_reason=safety_stop_reason,
            penalty_allowed=False,
        )
        self._states[rider_id] = updated
        return updated

    def eligible_riders(self, *, branch_id: str) -> tuple[RiderAvailability, ...]:
        self._branches.get(branch_id)
        return tuple(
            sorted(
                (
                    state
                    for state in self._states.values()
                    if state.branch_id == branch_id
                    and state.status is AvailabilityStatus.AVAILABLE
                ),
                key=lambda state: (state.available_since, state.rider_id),
            )
        )

    def get(self, rider_id: str) -> RiderAvailability:
        try:
            return self._states[rider_id]
        except KeyError as exc:
            raise ValueError("RIDER_AVAILABILITY_NOT_FOUND") from exc
