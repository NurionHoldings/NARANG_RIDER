from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.execution import Assignment, AssignmentStatus
from narang_rider.ledger import settlement_from_quote
from narang_rider.lifecycle import QuoteRepository
from narang_rider.money import Money
from narang_rider.payments import (
    PaymentEventType,
    PaymentState,
    PaymentWebhookService,
    PayoutDestinationRegistry,
    PayoutInstructionService,
    ProviderPaymentEvent,
)
from narang_rider.pricing import DeliveryFacts, PricingPolicy, quote_delivery

NOW = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)


def public_quote():
    policy = PricingPolicy(
        policy_id="sejong-v1",
        effective_from=NOW,
        base_pay_won=3_000,
        distance_unit_m=500,
        distance_unit_pay_won=300,
        included_distance_m=1_000,
        wait_free_minutes=5,
        wait_minute_pay_won=150,
        return_unit_m=500,
        return_unit_pay_won=200,
        bundle_increment_won=1_800,
        platform_cost_won=300,
        safety_fund_won=100,
        regional_fund_won=100,
        merchant_delivery_cap_won=3_000,
    )
    return quote_delivery(
        policy,
        DeliveryFacts(2_000, 20, 10, 1_000, 10),
        customer_delivery_fee=Money(3_500),
        expected_direct_cost=Money(800),
    )


def snapshot():
    return QuoteRepository().save(
        quote_id="quote-1",
        order_id="order-1",
        quote=public_quote(),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def webhook():
    return PaymentWebhookService(signing_secret=b"p" * 32, clock_skew=timedelta(minutes=5))


def event(
    service,
    *,
    event_id,
    event_type,
    amount=3_500,
    occurred_at=NOW,
    payment_id="payment-1",
    order_id="order-1",
):
    signature = service.sign(
        provider_event_id=event_id,
        payment_id=payment_id,
        order_id=order_id,
        event_type=event_type,
        amount_won=amount,
        occurred_at=occurred_at,
    )
    return ProviderPaymentEvent(
        provider_event_id=event_id,
        payment_id=payment_id,
        order_id=order_id,
        event_type=event_type,
        amount_won=amount,
        occurred_at=occurred_at,
        signature=signature,
    )


def authorize(service):
    return service.ingest(
        event=event(service, event_id="evt-authorize", event_type=PaymentEventType.AUTHORIZED),
        quote=snapshot(),
        received_at=NOW,
    )


def test_signed_authorize_capture_and_refund_sequence() -> None:
    service = webhook()
    authorized = authorize(service)
    captured = service.ingest(
        event=event(
            service,
            event_id="evt-capture",
            event_type=PaymentEventType.CAPTURED,
            occurred_at=NOW + timedelta(seconds=1),
        ),
        quote=snapshot(),
        received_at=NOW + timedelta(seconds=1),
    )
    refunded = service.ingest(
        event=event(
            service,
            event_id="evt-refund",
            event_type=PaymentEventType.REFUNDED,
            amount=1_000,
            occurred_at=NOW + timedelta(seconds=2),
        ),
        quote=snapshot(),
        received_at=NOW + timedelta(seconds=2),
    )

    assert authorized.state is PaymentState.AUTHORIZED
    assert captured.state is PaymentState.CAPTURED
    assert captured.captured_won == 3_500
    assert refunded.state is PaymentState.REFUNDED
    assert refunded.refunded_won == 1_000


def test_forged_stale_and_wrong_amount_events_fail_closed() -> None:
    service = webhook()
    forged = replace(
        event(service, event_id="evt-forged", event_type=PaymentEventType.AUTHORIZED),
        signature="0" * 64,
    )
    with pytest.raises(ValueError, match="INVALID_PAYMENT_SIGNATURE"):
        service.ingest(event=forged, quote=snapshot(), received_at=NOW)
    stale = event(
        service,
        event_id="evt-stale",
        event_type=PaymentEventType.AUTHORIZED,
        occurred_at=NOW - timedelta(minutes=6),
    )
    with pytest.raises(ValueError, match="STALE_PAYMENT_EVENT"):
        service.ingest(event=stale, quote=snapshot(), received_at=NOW)
    wrong_amount = event(
        service,
        event_id="evt-amount",
        event_type=PaymentEventType.AUTHORIZED,
        amount=3_499,
    )
    with pytest.raises(ValueError, match="PAYMENT_AMOUNT_MISMATCH"):
        service.ingest(event=wrong_amount, quote=snapshot(), received_at=NOW)


def test_duplicate_event_is_idempotent_but_cannot_be_rebound() -> None:
    service = webhook()
    first_event = event(
        service,
        event_id="evt-authorize",
        event_type=PaymentEventType.AUTHORIZED,
    )
    first = service.ingest(event=first_event, quote=snapshot(), received_at=NOW)
    replay = service.ingest(
        event=first_event,
        quote=snapshot(),
        received_at=NOW + timedelta(seconds=1),
    )

    assert replay == first
    rebound = event(
        service,
        event_id="evt-authorize",
        event_type=PaymentEventType.AUTHORIZED,
        payment_id="payment-attacker",
    )
    with pytest.raises(ValueError, match="PAYMENT_EVENT_IDEMPOTENCY_CONFLICT"):
        service.ingest(event=rebound, quote=snapshot(), received_at=NOW)


def test_capture_before_authorization_and_payment_rebinding_are_rejected() -> None:
    service = webhook()
    capture = event(service, event_id="evt-capture", event_type=PaymentEventType.CAPTURED)
    with pytest.raises(ValueError, match="PAYMENT_EVENT_OUT_OF_ORDER"):
        service.ingest(event=capture, quote=snapshot(), received_at=NOW)
    authorize(service)
    second_payment = event(
        service,
        event_id="evt-other-payment",
        event_type=PaymentEventType.AUTHORIZED,
        payment_id="payment-2",
    )
    with pytest.raises(ValueError, match="ORDER_PAYMENT_REBINDING_FORBIDDEN"):
        service.ingest(event=second_payment, quote=snapshot(), received_at=NOW)


def settled_assignment():
    return Assignment(
        assignment_id="assignment-1",
        order_id="order-1",
        rider_id="rider-1",
        offer_id="offer-1",
        quote_id="quote-1",
        status=AssignmentStatus.SETTLED,
        assigned_at=NOW,
        picked_up_at=NOW + timedelta(minutes=1),
        delivered_at=NOW + timedelta(minutes=10),
        evidence_id="evidence-1",
        settlement_transaction_id="settlement-1",
    )


def approved_destinations():
    registry = PayoutDestinationRegistry()
    pending = registry.request_change(
        rider_id="rider-1",
        vault_reference="vault:bank-account-token-1",
        requested_by="operator-a",
        now=NOW,
    )
    registry.approve(
        rider_id="rider-1",
        version=pending.version,
        approved_by="operator-b",
        now=NOW + timedelta(seconds=1),
    )
    return registry


def test_payout_destination_requires_vault_and_dual_control() -> None:
    registry = PayoutDestinationRegistry()
    with pytest.raises(ValueError, match="RAW_BANK_ACCOUNT_STORAGE_FORBIDDEN"):
        registry.request_change(
            rider_id="rider-1",
            vault_reference="110-123-456789",
            requested_by="operator-a",
            now=NOW,
        )
    pending = registry.request_change(
        rider_id="rider-1",
        vault_reference="vault:bank-account-token-1",
        requested_by="operator-a",
        now=NOW,
    )
    with pytest.raises(ValueError, match="PAYOUT_DESTINATION_DUAL_CONTROL_REQUIRED"):
        registry.approve(
            rider_id="rider-1",
            version=pending.version,
            approved_by="operator-a",
            now=NOW,
        )


def test_payout_requires_settled_binding_and_exact_rider_payable() -> None:
    quote = public_quote()
    quote_snapshot = snapshot()
    transaction = settlement_from_quote(
        transaction_id="settlement-1",
        order_id="order-1",
        quote=quote,
    )
    service = PayoutInstructionService(destinations=approved_destinations())
    assignment = settled_assignment()
    payout = service.create(
        payout_id="payout-1",
        assignment=assignment,
        quote=quote_snapshot,
        ledger_transaction=transaction,
        now=NOW + timedelta(minutes=11),
    )
    replay = service.create(
        payout_id="payout-1",
        assignment=assignment,
        quote=quote_snapshot,
        ledger_transaction=transaction,
        now=NOW + timedelta(minutes=12),
    )

    assert payout == replay
    assert payout.amount_won == quote_snapshot.rider_pay_won
    assert payout.vault_reference.startswith("vault:")


def test_payout_blocks_unsettled_duplicate_and_swapped_ledger() -> None:
    quote_snapshot = snapshot()
    transaction = settlement_from_quote(
        transaction_id="settlement-1",
        order_id="order-1",
        quote=public_quote(),
    )
    service = PayoutInstructionService(destinations=approved_destinations())
    assignment = settled_assignment()
    with pytest.raises(ValueError, match="SETTLED_ASSIGNMENT_REQUIRED"):
        service.create(
            payout_id="payout-early",
            assignment=replace(assignment, status=AssignmentStatus.DELIVERED),
            quote=quote_snapshot,
            ledger_transaction=transaction,
            now=NOW,
        )
    service.create(
        payout_id="payout-1",
        assignment=assignment,
        quote=quote_snapshot,
        ledger_transaction=transaction,
        now=NOW,
    )
    with pytest.raises(ValueError, match="DUPLICATE_PAYOUT_FOR_LEDGER_TRANSACTION"):
        service.create(
            payout_id="payout-2",
            assignment=assignment,
            quote=quote_snapshot,
            ledger_transaction=transaction,
            now=NOW + timedelta(seconds=1),
        )
    swapped = replace(transaction, order_id="other-order")
    with pytest.raises(ValueError, match="PAYOUT_BINDING_MISMATCH"):
        PayoutInstructionService(destinations=approved_destinations()).create(
            payout_id="payout-swapped",
            assignment=assignment,
            quote=quote_snapshot,
            ledger_transaction=swapped,
            now=NOW,
        )
