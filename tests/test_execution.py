from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.evidence import (
    DELIVERY_EVIDENCE_PURPOSE,
    DeliveryEvidenceBundle,
    DoublePackaging,
    EvidenceStage,
    UploadInspection,
)
from narang_rider.execution import AssignmentStatus, DeliveryExecutionService
from narang_rider.ledger import Ledger
from narang_rider.lifecycle import (
    OfferLeaseService,
    OrderRepository,
    OrderState,
    QuoteRepository,
)
from narang_rider.money import Money
from narang_rider.pricing import DeliveryFacts, PricingPolicy, quote_delivery

NOW = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)


def quote():
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


def inspection():
    return UploadInspection(
        sha256="a" * 64,
        mime_type="image/jpeg",
        size_bytes=100_000,
        exif_removed=True,
        malware_clean=True,
        privacy_mask_applied=True,
        original_discarded_after_masking=True,
        fixed_guide_frame_used=True,
        realtime_prohibited_content_scan_passed=True,
        depicts_package_at_designated_location=True,
        captured_in_app_camera=True,
        gallery_upload=False,
        server_nonce_visible=True,
        burst_frame_sha256=("a" * 64, "b" * 64),
        frame_consistency_digest="c" * 64,
        capture_purpose=DELIVERY_EVIDENCE_PURPOSE,
        exterior_appears_normal=True,
        seal_appears_intact=True,
    )


def setup_flow():
    orders = OrderRepository()
    order = orders.create(order_id="order-1", event_id="create", occurred_at=NOW)
    order = orders.transition(
        order_id=order.order_id,
        event_id="quoted",
        to_state=OrderState.QUOTED,
        occurred_at=NOW,
        reason_code="QUOTE_SAVED",
        expected_version=order.version,
    )
    order = orders.transition(
        order_id=order.order_id,
        event_id="offering",
        to_state=OrderState.OFFERING,
        occurred_at=NOW,
        reason_code="DISPATCH_STARTED",
        expected_version=order.version,
    )
    quotes = QuoteRepository()
    snapshot = quotes.save(
        quote_id="quote-1",
        order_id=order.order_id,
        quote=quote(),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    offers = OfferLeaseService()
    _, token = offers.issue(
        offer_id="offer-1",
        order=order,
        rider_id="rider-1",
        quote=snapshot,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    offer = offers.decide(
        command_id="accept",
        offer_id="offer-1",
        rider_id="rider-1",
        token=token,
        accept=True,
        now=NOW + timedelta(seconds=1),
    )
    evidence = DeliveryEvidenceBundle(
        grant_ttl=timedelta(minutes=5),
        normal_retention=timedelta(days=7),
        signing_key=b"k" * 32,
    )
    ledger = Ledger()
    service = DeliveryExecutionService(orders=orders, evidence=evidence, ledger=ledger)
    assignment = service.confirm_assignment(
        command_id="assign",
        assignment_id="assignment-1",
        offer=offer,
        quote=snapshot,
        now=NOW + timedelta(seconds=2),
    )
    return service, orders, evidence, ledger, snapshot, assignment


def pickup_and_evidence(service, evidence, assignment):
    assignment = service.mark_picked_up(
        command_id="pickup",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        now=NOW + timedelta(minutes=1),
    )
    packaging = evidence.record_merchant_packaging(
        order_id=assignment.order_id,
        high_risk_liquid_order=True,
        double_packaging=DoublePackaging.TRUE,
        seal_number="seal-1",
        packed_at=NOW,
    )
    grant, token = evidence.issue_grant(
        order_id=assignment.order_id,
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        now=NOW + timedelta(minutes=2),
    )
    receipt = evidence.record_photo(
        grant_id=grant.grant_id,
        token=token,
        order_id=assignment.order_id,
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        stage=EvidenceStage.DELIVERY_LOCATION,
        inspection=inspection(),
        now=NOW + timedelta(minutes=3),
        customer_notice_sent=True,
        approximate_delivery_zone="grid:sejong-1",
        seal_number="seal-1",
    )
    return assignment, packaging, receipt


def test_accepted_offer_becomes_one_bound_assignment_idempotently() -> None:
    service, orders, _, _, snapshot, assignment = setup_flow()

    replay = service.confirm_assignment(
        command_id="assign",
        assignment_id=assignment.assignment_id,
        offer=None,
        quote=snapshot,
        now=NOW + timedelta(seconds=3),
    )

    assert replay == assignment
    assert orders.get(assignment.order_id).state is OrderState.ASSIGNED


def test_wrong_rider_cannot_pick_up_or_complete_assignment() -> None:
    service, _, evidence, _, _, assignment = setup_flow()

    with pytest.raises(ValueError, match="ASSIGNMENT_RIDER_MISMATCH"):
        service.mark_picked_up(
            command_id="pickup-wrong",
            assignment_id=assignment.assignment_id,
            rider_id="attacker",
            now=NOW + timedelta(minutes=1),
        )
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)
    with pytest.raises(ValueError, match="ASSIGNMENT_RIDER_MISMATCH"):
        service.complete_delivery(
            command_id="complete-wrong",
            assignment_id=assignment.assignment_id,
            rider_id="attacker",
            evidence=receipt,
            packaging=packaging,
            now=NOW + timedelta(minutes=4),
        )


def test_delivery_requires_matching_signed_proof_and_packaging_seal() -> None:
    service, _, evidence, _, _, assignment = setup_flow()
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)

    with pytest.raises(ValueError, match="PACKAGING_SEAL_MISMATCH"):
        service.complete_delivery(
            command_id="complete-bad-seal",
            assignment_id=assignment.assignment_id,
            rider_id=assignment.rider_id,
            evidence=receipt,
            packaging=replace(packaging, seal_number="swapped"),
            now=NOW + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_SIGNATURE"):
        service.complete_delivery(
            command_id="complete-forged",
            assignment_id=assignment.assignment_id,
            rider_id=assignment.rider_id,
            evidence=replace(receipt, server_signature="0" * 64),
            packaging=packaging,
            now=NOW + timedelta(minutes=4),
        )


def test_delivery_completion_is_bound_and_idempotent() -> None:
    service, orders, evidence, _, _, assignment = setup_flow()
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)
    completed = service.complete_delivery(
        command_id="complete",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        evidence=receipt,
        packaging=packaging,
        now=NOW + timedelta(minutes=4),
    )
    replay = service.complete_delivery(
        command_id="complete",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        evidence=replace(receipt, order_id="ignored-on-safe-retry"),
        packaging=packaging,
        now=NOW + timedelta(minutes=5),
    )

    assert completed == replay
    assert completed.status is AssignmentStatus.DELIVERED
    assert orders.get(assignment.order_id).state is OrderState.DELIVERED


def test_settlement_requires_delivery_and_exact_persisted_quote() -> None:
    service, _, evidence, ledger, snapshot, assignment = setup_flow()
    with pytest.raises(ValueError, match="DELIVERY_REQUIRED_BEFORE_SETTLEMENT"):
        service.settle(
            command_id="settle-early",
            assignment_id=assignment.assignment_id,
            rider_id=assignment.rider_id,
            quote_snapshot=snapshot,
            public_quote=quote(),
            transaction_id="tx-early",
            now=NOW,
        )
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)
    completed = service.complete_delivery(
        command_id="complete",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        evidence=receipt,
        packaging=packaging,
        now=NOW + timedelta(minutes=4),
    )
    altered = replace(quote(), expected_net_hourly=Money(1))
    with pytest.raises(ValueError, match="SETTLEMENT_QUOTE_MISMATCH"):
        service.settle(
            command_id="settle-altered",
            assignment_id=completed.assignment_id,
            rider_id=completed.rider_id,
            quote_snapshot=snapshot,
            public_quote=altered,
            transaction_id="tx-altered",
            now=NOW + timedelta(minutes=5),
        )
    assert ledger.transactions() == ()


def test_balanced_settlement_is_recorded_once_after_verified_delivery() -> None:
    service, orders, evidence, ledger, snapshot, assignment = setup_flow()
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)
    assignment = service.complete_delivery(
        command_id="complete",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        evidence=receipt,
        packaging=packaging,
        now=NOW + timedelta(minutes=4),
    )
    settled, transaction = service.settle(
        command_id="settle",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        quote_snapshot=snapshot,
        public_quote=quote(),
        transaction_id="settlement-order-1",
        now=NOW + timedelta(minutes=5),
    )
    replay, same_transaction = service.settle(
        command_id="settle",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        quote_snapshot=snapshot,
        public_quote=quote(),
        transaction_id="settlement-order-1",
        now=NOW + timedelta(minutes=6),
    )

    assert settled == replay
    assert transaction == same_transaction
    assert settled.status is AssignmentStatus.SETTLED
    assert len(ledger.transactions()) == 1
    assert orders.get(assignment.order_id).state is OrderState.SETTLED
    assert sum(entry.debit.won for entry in transaction.entries) == sum(
        entry.credit.won for entry in transaction.entries
    )


def test_settlement_replay_cannot_change_transaction_identity() -> None:
    service, _, evidence, _, snapshot, assignment = setup_flow()
    assignment, packaging, receipt = pickup_and_evidence(service, evidence, assignment)
    assignment = service.complete_delivery(
        command_id="complete",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        evidence=receipt,
        packaging=packaging,
        now=NOW + timedelta(minutes=4),
    )
    service.settle(
        command_id="settle",
        assignment_id=assignment.assignment_id,
        rider_id=assignment.rider_id,
        quote_snapshot=snapshot,
        public_quote=quote(),
        transaction_id="settlement-order-1",
        now=NOW + timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="SETTLEMENT_IDEMPOTENCY_CONFLICT"):
        service.settle(
            command_id="settle",
            assignment_id=assignment.assignment_id,
            rider_id=assignment.rider_id,
            quote_snapshot=snapshot,
            public_quote=quote(),
            transaction_id="settlement-swapped",
            now=NOW + timedelta(minutes=6),
        )
