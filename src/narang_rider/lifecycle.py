from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .pricing import PublicQuote


class OrderState(StrEnum):
    CREATED = "CREATED"
    QUOTED = "QUOTED"
    OFFERING = "OFFERING"
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


_ALLOWED_TRANSITIONS = {
    OrderState.CREATED: frozenset({OrderState.QUOTED, OrderState.CANCELLED}),
    OrderState.QUOTED: frozenset({OrderState.OFFERING, OrderState.CANCELLED}),
    OrderState.OFFERING: frozenset({OrderState.ASSIGNED, OrderState.CANCELLED}),
    OrderState.ASSIGNED: frozenset({OrderState.PICKED_UP, OrderState.OFFERING, OrderState.CANCELLED}),
    OrderState.PICKED_UP: frozenset({OrderState.DELIVERED}),
    OrderState.DELIVERED: frozenset({OrderState.SETTLED}),
    OrderState.SETTLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class OrderEvent:
    event_id: str
    order_id: str
    from_state: OrderState | None
    to_state: OrderState
    occurred_at: datetime
    reason_code: str
    version: int


@dataclass(frozen=True)
class Order:
    order_id: str
    state: OrderState
    version: int

    def transition(
        self,
        *,
        event_id: str,
        to_state: OrderState,
        occurred_at: datetime,
        reason_code: str,
        expected_version: int,
    ) -> tuple[Order, OrderEvent]:
        if expected_version != self.version:
            raise ValueError("ORDER_VERSION_CONFLICT")
        if to_state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError("ILLEGAL_ORDER_TRANSITION")
        if not event_id or not reason_code:
            raise ValueError("EVENT_ID_AND_REASON_REQUIRED")
        next_order = replace(self, state=to_state, version=self.version + 1)
        event = OrderEvent(
            event_id=event_id,
            order_id=self.order_id,
            from_state=self.state,
            to_state=to_state,
            occurred_at=occurred_at,
            reason_code=reason_code,
            version=next_order.version,
        )
        return next_order, event


class OrderRepository:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._events: dict[str, OrderEvent] = {}

    def create(self, *, order_id: str, event_id: str, occurred_at: datetime) -> Order:
        if order_id in self._orders:
            raise ValueError("DUPLICATE_ORDER")
        if event_id in self._events:
            raise ValueError("DUPLICATE_EVENT")
        order = Order(order_id=order_id, state=OrderState.CREATED, version=1)
        self._orders[order_id] = order
        self._events[event_id] = OrderEvent(
            event_id, order_id, None, OrderState.CREATED, occurred_at, "ORDER_CREATED", 1
        )
        return order

    def get(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise ValueError("ORDER_NOT_FOUND") from exc

    def transition(
        self,
        *,
        order_id: str,
        event_id: str,
        to_state: OrderState,
        occurred_at: datetime,
        reason_code: str,
        expected_version: int,
    ) -> Order:
        existing = self._events.get(event_id)
        if existing is not None:
            if existing.order_id != order_id or existing.to_state != to_state:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return self.get(order_id)
        order, event = self.get(order_id).transition(
            event_id=event_id,
            to_state=to_state,
            occurred_at=occurred_at,
            reason_code=reason_code,
            expected_version=expected_version,
        )
        self._orders[order_id] = order
        self._events[event_id] = event
        return order

    def events(self, order_id: str) -> tuple[OrderEvent, ...]:
        return tuple(
            sorted(
                (event for event in self._events.values() if event.order_id == order_id),
                key=lambda event: event.version,
            )
        )


@dataclass(frozen=True)
class QuoteSnapshot:
    quote_id: str
    order_id: str
    policy_id: str
    rider_pay_won: int
    merchant_delivery_share_won: int
    customer_delivery_fee_won: int
    expected_net_per_order_won: int
    expected_net_hourly_won: int
    explanation_codes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    digest: str


class QuoteRepository:
    def __init__(self) -> None:
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._by_order: dict[str, list[str]] = {}

    @staticmethod
    def _digest(order_id: str, quote: PublicQuote, expires_at: datetime) -> str:
        payload = "|".join(
            (
                order_id,
                quote.policy_id,
                str(quote.rider_pay.won),
                str(quote.merchant_delivery_share.won),
                str(quote.customer_delivery_fee.won),
                str(quote.expected_net_per_order.won),
                str(quote.expected_net_hourly.won),
                ",".join(quote.explanation_codes),
                expires_at.isoformat(),
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def save(
        self,
        *,
        quote_id: str,
        order_id: str,
        quote: PublicQuote,
        created_at: datetime,
        expires_at: datetime,
    ) -> QuoteSnapshot:
        if expires_at <= created_at:
            raise ValueError("QUOTE_EXPIRY_REQUIRED")
        digest = self._digest(order_id, quote, expires_at)
        existing = self._quotes.get(quote_id)
        if existing is not None:
            if existing.digest != digest:
                raise ValueError("QUOTE_IDEMPOTENCY_CONFLICT")
            return existing
        snapshot = QuoteSnapshot(
            quote_id=quote_id,
            order_id=order_id,
            policy_id=quote.policy_id,
            rider_pay_won=quote.rider_pay.won,
            merchant_delivery_share_won=quote.merchant_delivery_share.won,
            customer_delivery_fee_won=quote.customer_delivery_fee.won,
            expected_net_per_order_won=quote.expected_net_per_order.won,
            expected_net_hourly_won=quote.expected_net_hourly.won,
            explanation_codes=quote.explanation_codes,
            created_at=created_at,
            expires_at=expires_at,
            digest=digest,
        )
        self._quotes[quote_id] = snapshot
        self._by_order.setdefault(order_id, []).append(quote_id)
        return snapshot

    def get(self, quote_id: str) -> QuoteSnapshot:
        try:
            return self._quotes[quote_id]
        except KeyError as exc:
            raise ValueError("QUOTE_NOT_FOUND") from exc


class OfferStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class OfferLease:
    offer_id: str
    order_id: str
    rider_id: str
    quote_id: str
    issued_at: datetime
    expires_at: datetime
    status: OfferStatus
    token_hash: str
    decision_at: datetime | None = None


class OfferLeaseService:
    def __init__(self) -> None:
        self._offers: dict[str, OfferLease] = {}
        self._active_by_order: dict[str, str] = {}
        self._commands: dict[str, OfferLease] = {}

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(
        self,
        *,
        offer_id: str,
        order: Order,
        rider_id: str,
        quote: QuoteSnapshot,
        issued_at: datetime,
        expires_at: datetime,
    ) -> tuple[OfferLease, str]:
        if order.state is not OrderState.OFFERING:
            raise ValueError("ORDER_NOT_OFFERING")
        if quote.order_id != order.order_id:
            raise ValueError("QUOTE_ORDER_MISMATCH")
        if quote.expires_at <= issued_at:
            raise ValueError("QUOTE_EXPIRED")
        if expires_at <= issued_at or expires_at > quote.expires_at:
            raise ValueError("INVALID_OFFER_EXPIRY")
        if offer_id in self._offers:
            raise ValueError("DUPLICATE_OFFER")
        if order.order_id in self._active_by_order:
            raise ValueError("ACTIVE_OFFER_EXISTS")
        token = secrets.token_urlsafe(32)
        lease = OfferLease(
            offer_id=offer_id,
            order_id=order.order_id,
            rider_id=rider_id,
            quote_id=quote.quote_id,
            issued_at=issued_at,
            expires_at=expires_at,
            status=OfferStatus.ACTIVE,
            token_hash=self._hash_token(token),
        )
        self._offers[offer_id] = lease
        self._active_by_order[order.order_id] = offer_id
        return lease, token

    def _authenticate(self, offer_id: str, rider_id: str, token: str) -> OfferLease:
        try:
            lease = self._offers[offer_id]
        except KeyError as exc:
            raise ValueError("OFFER_NOT_FOUND") from exc
        if lease.rider_id != rider_id:
            raise ValueError("OFFER_RIDER_MISMATCH")
        if not hmac.compare_digest(lease.token_hash, self._hash_token(token)):
            raise ValueError("INVALID_OFFER_TOKEN")
        return lease

    def decide(
        self,
        *,
        command_id: str,
        offer_id: str,
        rider_id: str,
        token: str,
        accept: bool,
        now: datetime,
    ) -> OfferLease:
        prior = self._commands.get(command_id)
        if prior is not None:
            if prior.offer_id != offer_id:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return prior
        lease = self._authenticate(offer_id, rider_id, token)
        if lease.status is not OfferStatus.ACTIVE:
            raise ValueError("OFFER_NOT_ACTIVE")
        status = OfferStatus.ACCEPTED if accept else OfferStatus.DECLINED
        if now >= lease.expires_at:
            status = OfferStatus.EXPIRED
        decided = replace(lease, status=status, decision_at=now)
        self._offers[offer_id] = decided
        self._active_by_order.pop(lease.order_id, None)
        self._commands[command_id] = decided
        return decided

    def expire(self, *, offer_id: str, now: datetime) -> OfferLease:
        lease = self._offers[offer_id]
        if lease.status is not OfferStatus.ACTIVE:
            return lease
        if now < lease.expires_at:
            raise ValueError("OFFER_STILL_ACTIVE")
        expired = replace(lease, status=OfferStatus.EXPIRED, decision_at=now)
        self._offers[offer_id] = expired
        self._active_by_order.pop(lease.order_id, None)
        return expired
