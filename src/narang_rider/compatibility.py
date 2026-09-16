from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LegacyStatus(StrEnum):
    RECEIVED = "RECEIVED"
    DISPATCHED = "DISPATCHED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class PartnerOrder:
    provider: str
    external_order_id: str
    merchant_id: str
    amount_won: int
    pickup_ref: str
    dropoff_ref: str
    status: LegacyStatus

    def __post_init__(self) -> None:
        if self.amount_won < 0:
            raise ValueError("INVALID_PARTNER_AMOUNT")
        if not self.pickup_ref.startswith("vault:") or not self.dropoff_ref.startswith("vault:"):
            raise ValueError("PARTNER_LOCATION_VAULT_REQUIRED")


@dataclass(frozen=True)
class CanonicalOrder:
    canonical_id: str
    merchant_id: str
    amount_won: int
    pickup_ref: str
    dropoff_ref: str
    source_refs: tuple[str, ...]
    status: LegacyStatus


class CompatibilityGateway:
    def __init__(self) -> None:
        self._orders: dict[str, CanonicalOrder] = {}
        self._source_index: dict[str, str] = {}

    def ingest(self, value: PartnerOrder, *, canonical_id: str) -> CanonicalOrder:
        source = f"{value.provider}:{value.external_order_id}"
        existing_id = self._source_index.get(source)
        if existing_id:
            existing = self._orders[existing_id]
            if (existing.merchant_id, existing.amount_won, existing.pickup_ref, existing.dropoff_ref) != (
                value.merchant_id, value.amount_won, value.pickup_ref, value.dropoff_ref
            ):
                raise ValueError("PARTNER_ORDER_CONFLICT")
            return existing
        fingerprint = (value.merchant_id, value.pickup_ref, value.dropoff_ref, value.amount_won)
        for existing in self._orders.values():
            if (existing.merchant_id, existing.pickup_ref, existing.dropoff_ref, existing.amount_won) == fingerprint:
                merged = CanonicalOrder(
                    existing.canonical_id,
                    existing.merchant_id,
                    existing.amount_won,
                    existing.pickup_ref,
                    existing.dropoff_ref,
                    tuple(sorted((*existing.source_refs, source))),
                    existing.status,
                )
                self._orders[existing.canonical_id] = merged
                self._source_index[source] = existing.canonical_id
                return merged
        order = CanonicalOrder(
            canonical_id, value.merchant_id, value.amount_won, value.pickup_ref,
            value.dropoff_ref, (source,), value.status
        )
        self._orders[canonical_id] = order
        self._source_index[source] = canonical_id
        return order

    def callback(self, canonical_id: str, status: LegacyStatus) -> tuple[tuple[str, LegacyStatus], ...]:
        order = self._orders[canonical_id]
        return tuple((source, status) for source in order.source_refs)
