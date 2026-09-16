from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .evidence import (
    DeliveryEvidenceBundle,
    EvidenceReceipt,
    EvidenceStage,
    MerchantPackagingEvidence,
)
from .ledger import Ledger, LedgerTransaction, settlement_from_quote
from .lifecycle import (
    OfferLease,
    OfferStatus,
    OrderRepository,
    OrderState,
    QuoteSnapshot,
)
from .pricing import PublicQuote


class AssignmentStatus(StrEnum):
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    SETTLED = "SETTLED"


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    order_id: str
    rider_id: str
    offer_id: str
    quote_id: str
    status: AssignmentStatus
    assigned_at: datetime
    picked_up_at: datetime | None = None
    delivered_at: datetime | None = None
    evidence_id: str | None = None
    settlement_transaction_id: str | None = None


class DeliveryExecutionService:
    """Connect accepted offers to delivery proof and append-only settlement."""

    def __init__(
        self,
        *,
        orders: OrderRepository,
        evidence: DeliveryEvidenceBundle,
        ledger: Ledger,
    ) -> None:
        self._orders = orders
        self._evidence = evidence
        self._ledger = ledger
        self._assignments: dict[str, Assignment] = {}
        self._assignment_by_order: dict[str, str] = {}
        self._commands: dict[str, Assignment] = {}

    def confirm_assignment(
        self,
        *,
        command_id: str,
        assignment_id: str,
        offer: OfferLease,
        quote: QuoteSnapshot,
        now: datetime,
    ) -> Assignment:
        prior = self._commands.get(command_id)
        if prior is not None:
            if prior.assignment_id != assignment_id:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return prior
        if offer.status is not OfferStatus.ACCEPTED:
            raise ValueError("ACCEPTED_OFFER_REQUIRED")
        if offer.order_id != quote.order_id or offer.quote_id != quote.quote_id:
            raise ValueError("OFFER_QUOTE_BINDING_MISMATCH")
        order = self._orders.get(offer.order_id)
        if order.state is not OrderState.OFFERING:
            raise ValueError("ORDER_NOT_OFFERING")
        if assignment_id in self._assignments or order.order_id in self._assignment_by_order:
            raise ValueError("DUPLICATE_ASSIGNMENT")
        self._orders.transition(
            order_id=order.order_id,
            event_id=f"assignment:{assignment_id}",
            to_state=OrderState.ASSIGNED,
            occurred_at=now,
            reason_code="OFFER_ACCEPTED",
            expected_version=order.version,
        )
        assignment = Assignment(
            assignment_id=assignment_id,
            order_id=order.order_id,
            rider_id=offer.rider_id,
            offer_id=offer.offer_id,
            quote_id=quote.quote_id,
            status=AssignmentStatus.ASSIGNED,
            assigned_at=now,
        )
        self._assignments[assignment_id] = assignment
        self._assignment_by_order[order.order_id] = assignment_id
        self._commands[command_id] = assignment
        return assignment

    def mark_picked_up(
        self,
        *,
        command_id: str,
        assignment_id: str,
        rider_id: str,
        now: datetime,
    ) -> Assignment:
        prior = self._commands.get(command_id)
        if prior is not None:
            if prior.assignment_id != assignment_id:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return prior
        assignment = self._bound_assignment(assignment_id, rider_id)
        if assignment.status is not AssignmentStatus.ASSIGNED:
            raise ValueError("ASSIGNMENT_NOT_ASSIGNED")
        order = self._orders.get(assignment.order_id)
        updated_order = self._orders.transition(
            order_id=order.order_id,
            event_id=f"pickup:{assignment_id}",
            to_state=OrderState.PICKED_UP,
            occurred_at=now,
            reason_code="RIDER_CONFIRMED_PICKUP",
            expected_version=order.version,
        )
        if updated_order.state is not OrderState.PICKED_UP:
            raise ValueError("PICKUP_STATE_FAILURE")
        updated = replace(
            assignment,
            status=AssignmentStatus.PICKED_UP,
            picked_up_at=now,
        )
        self._assignments[assignment_id] = updated
        self._commands[command_id] = updated
        return updated

    def complete_delivery(
        self,
        *,
        command_id: str,
        assignment_id: str,
        rider_id: str,
        evidence: EvidenceReceipt,
        packaging: MerchantPackagingEvidence,
        now: datetime,
    ) -> Assignment:
        prior = self._commands.get(command_id)
        if prior is not None:
            if prior.assignment_id != assignment_id:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return prior
        assignment = self._bound_assignment(assignment_id, rider_id)
        if assignment.status is not AssignmentStatus.PICKED_UP:
            raise ValueError("ASSIGNMENT_NOT_PICKED_UP")
        if (
            evidence.order_id != assignment.order_id
            or evidence.assignment_id != assignment.assignment_id
            or evidence.rider_id != assignment.rider_id
        ):
            raise ValueError("DELIVERY_EVIDENCE_BINDING_MISMATCH")
        if evidence.stage is not EvidenceStage.DELIVERY_LOCATION:
            raise ValueError("DELIVERY_LOCATION_EVIDENCE_REQUIRED")
        if not evidence.customer_notice_sent:
            raise ValueError("CUSTOMER_EVIDENCE_NOTICE_REQUIRED")
        if not self._evidence.verify_receipt_signature(evidence):
            raise ValueError("INVALID_EVIDENCE_SIGNATURE")
        if packaging.order_id != assignment.order_id:
            raise ValueError("PACKAGING_ORDER_MISMATCH")
        if packaging.seal_number != evidence.seal_number:
            raise ValueError("PACKAGING_SEAL_MISMATCH")
        order = self._orders.get(assignment.order_id)
        self._orders.transition(
            order_id=order.order_id,
            event_id=f"delivery:{assignment_id}",
            to_state=OrderState.DELIVERED,
            occurred_at=now,
            reason_code="VERIFIED_DELIVERY_EVIDENCE",
            expected_version=order.version,
        )
        updated = replace(
            assignment,
            status=AssignmentStatus.DELIVERED,
            delivered_at=now,
            evidence_id=evidence.evidence_id,
        )
        self._assignments[assignment_id] = updated
        self._commands[command_id] = updated
        return updated

    def settle(
        self,
        *,
        command_id: str,
        assignment_id: str,
        rider_id: str,
        quote_snapshot: QuoteSnapshot,
        public_quote: PublicQuote,
        transaction_id: str,
        now: datetime,
    ) -> tuple[Assignment, LedgerTransaction]:
        assignment = self._bound_assignment(assignment_id, rider_id)
        if assignment.status is AssignmentStatus.SETTLED:
            if (
                assignment.settlement_transaction_id != transaction_id
                or command_id not in self._commands
            ):
                raise ValueError("SETTLEMENT_IDEMPOTENCY_CONFLICT")
            transaction = next(
                tx for tx in self._ledger.transactions() if tx.transaction_id == transaction_id
            )
            return assignment, transaction
        if assignment.status is not AssignmentStatus.DELIVERED:
            raise ValueError("DELIVERY_REQUIRED_BEFORE_SETTLEMENT")
        self._verify_quote(assignment, quote_snapshot, public_quote)
        order = self._orders.get(assignment.order_id)
        transaction = settlement_from_quote(
            transaction_id=transaction_id,
            order_id=assignment.order_id,
            quote=public_quote,
        )
        self._ledger.record(transaction)
        self._orders.transition(
            order_id=order.order_id,
            event_id=f"settlement:{transaction_id}",
            to_state=OrderState.SETTLED,
            occurred_at=now,
            reason_code="BALANCED_LEDGER_RECORDED",
            expected_version=order.version,
        )
        updated = replace(
            assignment,
            status=AssignmentStatus.SETTLED,
            settlement_transaction_id=transaction_id,
        )
        self._assignments[assignment_id] = updated
        self._commands[command_id] = updated
        return updated, transaction

    def assignment(self, assignment_id: str) -> Assignment:
        try:
            return self._assignments[assignment_id]
        except KeyError as exc:
            raise ValueError("ASSIGNMENT_NOT_FOUND") from exc

    def _bound_assignment(self, assignment_id: str, rider_id: str) -> Assignment:
        assignment = self.assignment(assignment_id)
        if assignment.rider_id != rider_id:
            raise ValueError("ASSIGNMENT_RIDER_MISMATCH")
        return assignment

    @staticmethod
    def _verify_quote(
        assignment: Assignment,
        snapshot: QuoteSnapshot,
        quote: PublicQuote,
    ) -> None:
        if snapshot.order_id != assignment.order_id or snapshot.quote_id != assignment.quote_id:
            raise ValueError("SETTLEMENT_QUOTE_BINDING_MISMATCH")
        expected = (
            snapshot.policy_id,
            snapshot.rider_pay_won,
            snapshot.merchant_delivery_share_won,
            snapshot.customer_delivery_fee_won,
            snapshot.expected_net_per_order_won,
            snapshot.expected_net_hourly_won,
            snapshot.explanation_codes,
        )
        supplied = (
            quote.policy_id,
            quote.rider_pay.won,
            quote.merchant_delivery_share.won,
            quote.customer_delivery_fee.won,
            quote.expected_net_per_order.won,
            quote.expected_net_hourly.won,
            quote.explanation_codes,
        )
        if supplied != expected:
            raise ValueError("SETTLEMENT_QUOTE_MISMATCH")
