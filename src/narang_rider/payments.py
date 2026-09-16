from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .execution import Assignment, AssignmentStatus
from .ledger import LedgerAccount, LedgerTransaction
from .lifecycle import QuoteSnapshot


class PaymentEventType(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"


class PaymentState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True)
class ProviderPaymentEvent:
    provider_event_id: str
    payment_id: str
    order_id: str
    event_type: PaymentEventType
    amount_won: int
    occurred_at: datetime
    signature: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.provider_event_id, self.payment_id, self.order_id, self.signature)
        ):
            raise ValueError("PAYMENT_EVENT_IDENTITY_REQUIRED")
        if self.occurred_at.tzinfo is None or self.amount_won < 0:
            raise ValueError("INVALID_PAYMENT_EVENT")


@dataclass(frozen=True)
class PaymentRecord:
    payment_id: str
    order_id: str
    state: PaymentState
    captured_won: int
    refunded_won: int
    last_event_at: datetime


class PaymentWebhookService:
    """Authenticated inbox for payment-provider events."""

    def __init__(self, *, signing_secret: bytes, clock_skew: timedelta) -> None:
        if len(signing_secret) < 32 or clock_skew <= timedelta(0):
            raise ValueError("PAYMENT_WEBHOOK_SECURITY_CONFIG_REQUIRED")
        self._secret = signing_secret
        self._clock_skew = clock_skew
        self._events: dict[str, tuple[str, PaymentRecord]] = {}
        self._payments: dict[str, PaymentRecord] = {}
        self._payment_by_order: dict[str, str] = {}

    def sign(
        self,
        *,
        provider_event_id: str,
        payment_id: str,
        order_id: str,
        event_type: PaymentEventType,
        amount_won: int,
        occurred_at: datetime,
    ) -> str:
        return hmac.new(
            self._secret,
            self._canonical(
                provider_event_id,
                payment_id,
                order_id,
                event_type,
                amount_won,
                occurred_at,
            ),
            hashlib.sha256,
        ).hexdigest()

    def ingest(
        self,
        *,
        event: ProviderPaymentEvent,
        quote: QuoteSnapshot,
        received_at: datetime,
    ) -> PaymentRecord:
        if received_at.tzinfo is None:
            raise ValueError("AWARE_RECEIVED_AT_REQUIRED")
        fingerprint = hashlib.sha256(
            self._canonical(
                event.provider_event_id,
                event.payment_id,
                event.order_id,
                event.event_type,
                event.amount_won,
                event.occurred_at,
            )
        ).hexdigest()
        prior_event = self._events.get(event.provider_event_id)
        if prior_event is not None:
            if prior_event[0] != fingerprint:
                raise ValueError("PAYMENT_EVENT_IDEMPOTENCY_CONFLICT")
            return prior_event[1]
        expected_signature = self.sign(
            provider_event_id=event.provider_event_id,
            payment_id=event.payment_id,
            order_id=event.order_id,
            event_type=event.event_type,
            amount_won=event.amount_won,
            occurred_at=event.occurred_at,
        )
        if not hmac.compare_digest(expected_signature, event.signature):
            raise ValueError("INVALID_PAYMENT_SIGNATURE")
        if abs(received_at - event.occurred_at) > self._clock_skew:
            raise ValueError("STALE_PAYMENT_EVENT")
        if event.order_id != quote.order_id:
            raise ValueError("PAYMENT_ORDER_MISMATCH")
        bound_payment = self._payment_by_order.get(event.order_id)
        if bound_payment is not None and bound_payment != event.payment_id:
            raise ValueError("ORDER_PAYMENT_REBINDING_FORBIDDEN")
        current = self._payments.get(event.payment_id)
        record = self._apply(current=current, event=event, quote=quote)
        self._payments[event.payment_id] = record
        self._payment_by_order[event.order_id] = event.payment_id
        self._events[event.provider_event_id] = (fingerprint, record)
        return record

    @staticmethod
    def _apply(
        *,
        current: PaymentRecord | None,
        event: ProviderPaymentEvent,
        quote: QuoteSnapshot,
    ) -> PaymentRecord:
        expected_customer_charge = quote.customer_delivery_fee_won
        if (
            event.event_type in {PaymentEventType.AUTHORIZED, PaymentEventType.CAPTURED}
            and event.amount_won != expected_customer_charge
        ):
            raise ValueError("PAYMENT_AMOUNT_MISMATCH")
        if event.event_type is PaymentEventType.AUTHORIZED:
            if current is not None:
                raise ValueError("PAYMENT_EVENT_OUT_OF_ORDER")
            return PaymentRecord(
                event.payment_id,
                event.order_id,
                PaymentState.AUTHORIZED,
                0,
                0,
                event.occurred_at,
            )
        if event.event_type is PaymentEventType.CAPTURED:
            if current is None or current.state is not PaymentState.AUTHORIZED:
                raise ValueError("PAYMENT_EVENT_OUT_OF_ORDER")
            return replace(
                current,
                state=PaymentState.CAPTURED,
                captured_won=event.amount_won,
                last_event_at=event.occurred_at,
            )
        if current is None or current.state is not PaymentState.CAPTURED:
            raise ValueError("PAYMENT_EVENT_OUT_OF_ORDER")
        if event.amount_won <= 0 or event.amount_won > current.captured_won:
            raise ValueError("INVALID_REFUND_AMOUNT")
        return replace(
            current,
            state=PaymentState.REFUNDED,
            refunded_won=event.amount_won,
            last_event_at=event.occurred_at,
        )

    @staticmethod
    def _canonical(
        provider_event_id: str,
        payment_id: str,
        order_id: str,
        event_type: PaymentEventType,
        amount_won: int,
        occurred_at: datetime,
    ) -> bytes:
        return "\x1f".join(
            (
                provider_event_id,
                payment_id,
                order_id,
                event_type.value,
                str(amount_won),
                occurred_at.isoformat(),
            )
        ).encode()


@dataclass(frozen=True)
class PayoutDestination:
    rider_id: str
    version: int
    vault_reference: str
    requested_by: str
    requested_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_by is not None


class PayoutDestinationRegistry:
    def __init__(self) -> None:
        self._destinations: dict[str, PayoutDestination] = {}

    def request_change(
        self,
        *,
        rider_id: str,
        vault_reference: str,
        requested_by: str,
        now: datetime,
    ) -> PayoutDestination:
        if now.tzinfo is None or not all(
            value.strip() for value in (rider_id, vault_reference, requested_by)
        ):
            raise ValueError("PAYOUT_DESTINATION_IDENTITY_REQUIRED")
        if not vault_reference.startswith("vault:"):
            raise ValueError("RAW_BANK_ACCOUNT_STORAGE_FORBIDDEN")
        current = self._destinations.get(rider_id)
        destination = PayoutDestination(
            rider_id=rider_id,
            version=1 if current is None else current.version + 1,
            vault_reference=vault_reference,
            requested_by=requested_by,
            requested_at=now,
        )
        self._destinations[rider_id] = destination
        return destination

    def approve(
        self,
        *,
        rider_id: str,
        version: int,
        approved_by: str,
        now: datetime,
    ) -> PayoutDestination:
        destination = self.get(rider_id)
        if destination.version != version:
            raise ValueError("PAYOUT_DESTINATION_VERSION_CONFLICT")
        if destination.requested_by == approved_by:
            raise ValueError("PAYOUT_DESTINATION_DUAL_CONTROL_REQUIRED")
        if destination.approved:
            return destination
        approved = replace(destination, approved_by=approved_by, approved_at=now)
        self._destinations[rider_id] = approved
        return approved

    def get(self, rider_id: str) -> PayoutDestination:
        try:
            return self._destinations[rider_id]
        except KeyError as exc:
            raise ValueError("PAYOUT_DESTINATION_NOT_FOUND") from exc


@dataclass(frozen=True)
class PayoutInstruction:
    payout_id: str
    order_id: str
    assignment_id: str
    rider_id: str
    amount_won: int
    ledger_transaction_id: str
    destination_version: int
    vault_reference: str
    created_at: datetime
    status: str = "PENDING_PROVIDER_SUBMISSION"


class PayoutInstructionService:
    """Creates instructions only; an external adapter performs the transfer."""

    def __init__(self, *, destinations: PayoutDestinationRegistry) -> None:
        self._destinations = destinations
        self._payouts: dict[str, PayoutInstruction] = {}
        self._by_ledger_transaction: dict[str, str] = {}

    def create(
        self,
        *,
        payout_id: str,
        assignment: Assignment,
        quote: QuoteSnapshot,
        ledger_transaction: LedgerTransaction,
        now: datetime,
    ) -> PayoutInstruction:
        existing = self._payouts.get(payout_id)
        if existing is not None:
            if existing.ledger_transaction_id != ledger_transaction.transaction_id:
                raise ValueError("PAYOUT_IDEMPOTENCY_CONFLICT")
            return existing
        if assignment.status is not AssignmentStatus.SETTLED:
            raise ValueError("SETTLED_ASSIGNMENT_REQUIRED")
        if (
            assignment.order_id != quote.order_id
            or assignment.quote_id != quote.quote_id
            or ledger_transaction.order_id != assignment.order_id
            or assignment.settlement_transaction_id != ledger_transaction.transaction_id
        ):
            raise ValueError("PAYOUT_BINDING_MISMATCH")
        if ledger_transaction.transaction_id in self._by_ledger_transaction:
            raise ValueError("DUPLICATE_PAYOUT_FOR_LEDGER_TRANSACTION")
        rider_credit = sum(
            entry.credit.won
            for entry in ledger_transaction.entries
            if entry.account is LedgerAccount.RIDER_PAYABLE
        )
        if rider_credit != quote.rider_pay_won:
            raise ValueError("PAYOUT_AMOUNT_MISMATCH")
        destination = self._destinations.get(assignment.rider_id)
        if not destination.approved:
            raise ValueError("APPROVED_PAYOUT_DESTINATION_REQUIRED")
        instruction = PayoutInstruction(
            payout_id=payout_id,
            order_id=assignment.order_id,
            assignment_id=assignment.assignment_id,
            rider_id=assignment.rider_id,
            amount_won=rider_credit,
            ledger_transaction_id=ledger_transaction.transaction_id,
            destination_version=destination.version,
            vault_reference=destination.vault_reference,
            created_at=now,
        )
        self._payouts[payout_id] = instruction
        self._by_ledger_transaction[ledger_transaction.transaction_id] = payout_id
        return instruction
