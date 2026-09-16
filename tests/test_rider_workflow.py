from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.merchant_operations import ActorSource
from narang_rider.persistence import InMemoryPersistence, RecordKind, canonical_payload_digest
from narang_rider.rider_workflow import (
    RIDER_ROUTE_MANIFEST,
    DeliveryStatus,
    FairOfferView,
    RiderAvailability,
    RiderCapability,
    RiderCommandRejected,
    RiderContext,
    RiderErrorCode,
    RiderOfferStatus,
    RiderWorkflowService,
)

NOW = datetime(2026, 9, 16, 3, tzinfo=UTC)
CAPABILITIES = frozenset(RiderCapability)


def context(
    rider_id: str = "rider-1",
    branch_id: str = "branch-sejong",
    source: ActorSource = ActorSource.HUMAN,
) -> RiderContext:
    return RiderContext("actor-1", rider_id, branch_id, CAPABILITIES, source)


def offer(offer_id: str = "offer-1", rider_id: str = "rider-1") -> FairOfferView:
    return FairOfferView(
        offer_id,
        "order-1",
        rider_id,
        "branch-sejong",
        6_000,
        1_500,
        4_500,
        1,
        ("FIFO_AVAILABLE_SINCE", "SERVICE_AREA_MATCH"),
        f"receipt-{offer_id}",
        NOW + timedelta(minutes=2),
        "SEJONG-JIPHYEON",
        "SEJONG-DAEPYEONG",
        "vault:route/order-1",
    )


def reject(code, function, *args, **kwargs) -> None:
    with pytest.raises(RiderCommandRejected) as raised:
        function(*args, **kwargs)
    assert raised.value.code is code


def seeded_service():
    store = InMemoryPersistence()
    service = RiderWorkflowService(store, store)
    service.publish_offer(context(), offer(), "publish-1")
    return service, store


def accepted_service():
    service, store = seeded_service()
    receipt = service.respond_offer(
        context(),
        offer_id="offer-1",
        accept=True,
        now=NOW,
        expected_version=1,
        idempotency_key="accept-1",
    )
    return service, store, receipt.record_id


def seed_order(store: InMemoryPersistence) -> None:
    payload = {
        "branch_id": "branch-sejong",
        "merchant_id": "merchant-1",
        "state": "SUBMITTED",
        "double_packaging": "false",
        "seal_number": None,
    }
    unit = store.begin(
        branch_id="branch-sejong",
        idempotency_key="seed-order",
        payload_digest=canonical_payload_digest(payload),
    )
    unit.put(RecordKind.ORDER, "order-1", payload, expected_version=0)
    unit.commit()


@pytest.mark.parametrize("status", list(RiderAvailability))
def test_availability_has_no_penalty_or_decline_retaliation(status) -> None:
    store = InMemoryPersistence()
    service = RiderWorkflowService(store, store)
    receipt = service.set_availability(
        context(),
        status=status,
        expected_version=0,
        idempotency_key=status.value,
        safety_reason_code="WEATHER_RISK" if status is RiderAvailability.SAFETY_STOP else None,
    )
    record = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", receipt.record_id)
    assert record is not None
    assert record.payload["penalty_allowed"] is False
    assert record.payload["decline_history_used"] is False


def test_offer_exposes_public_pay_net_fifo_reasons_and_minimum_route() -> None:
    _, store = seeded_service()
    record = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", "rider-offer:offer-1")
    assert record is not None
    assert record.payload["rider_pay_won"] == 6_000
    assert record.payload["estimated_net_won"] == 4_500
    assert record.payload["queue_position"] == 1
    assert record.payload["decline_history_used"] is False
    assert record.payload["route_ref"].startswith("vault:")


def test_raw_gps_or_address_shaped_route_is_rejected() -> None:
    values = offer().__dict__ | {"route_ref": "37.123,127.456"}
    with pytest.raises(ValueError):
        FairOfferView(**values)
    values = offer().__dict__ | {"coarse_pickup_zone": "37.1,127.1"}
    with pytest.raises(ValueError):
        FairOfferView(**values)


def test_decline_and_timeout_never_authorize_penalty() -> None:
    service, store = seeded_service()
    service.respond_offer(
        context(),
        offer_id="offer-1",
        accept=False,
        now=NOW,
        expected_version=1,
        idempotency_key="decline-1",
    )
    record = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", "rider-offer:offer-1")
    assert record is not None
    assert record.payload["status"] == RiderOfferStatus.DECLINED
    assert record.payload["decline_penalty_allowed"] is False
    assert record.payload["retaliation_allowed"] is False

    service.publish_offer(context(), offer("offer-2"), "publish-2")
    result = service.respond_offer(
        context(),
        offer_id="offer-2",
        accept=False,
        now=NOW + timedelta(minutes=3),
        expected_version=1,
        idempotency_key="timeout-2",
    )
    assert result.status == RiderOfferStatus.TIMED_OUT


def test_stale_lease_and_forged_assignment_fail_closed() -> None:
    service, _ = seeded_service()
    reject(
        RiderErrorCode.STALE_LEASE,
        service.respond_offer,
        context(),
        offer_id="offer-1",
        accept=True,
        now=NOW + timedelta(minutes=3),
        expected_version=1,
        idempotency_key="late",
    )
    reject(
        RiderErrorCode.NOT_FOUND,
        service.progress,
        context(),
        assignment_id="forged-assignment",
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="forged",
    )


def test_cross_rider_and_branch_idor_are_rejected() -> None:
    service, _, assignment_id = accepted_service()
    reject(
        RiderErrorCode.OWNERSHIP_MISMATCH,
        service.progress,
        context(rider_id="rider-2"),
        assignment_id=assignment_id,
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="idor-rider",
    )
    reject(
        RiderErrorCode.NOT_FOUND,
        service.progress,
        context(branch_id="branch-busan"),
        assignment_id=assignment_id,
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="idor-branch",
    )


def test_second_rider_cannot_accept_same_order_assignment() -> None:
    service, store, assignment_id = accepted_service()
    second = offer("offer-2", "rider-2")
    service.publish_offer(context(rider_id="rider-2"), second, "publish-2")
    reject(
        RiderErrorCode.STALE_VERSION,
        service.respond_offer,
        context(rider_id="rider-2"),
        offer_id="offer-2",
        accept=True,
        now=NOW,
        expected_version=1,
        idempotency_key="accept-2",
    )
    assignment = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", assignment_id)
    assert assignment is not None and assignment.payload["rider_id"] == "rider-1"


def test_status_skip_is_rejected_and_pickup_does_not_transfer_packaging_blame() -> None:
    service, store, assignment_id = accepted_service()
    seed_order(store)
    reject(
        RiderErrorCode.STATE_TRANSITION_DENIED,
        service.progress,
        context(),
        assignment_id=assignment_id,
        target=DeliveryStatus.PICKED_UP,
        expected_version=1,
        idempotency_key="skip",
    )
    service.progress(
        context(),
        assignment_id=assignment_id,
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="arrive",
    )
    service.progress(
        context(),
        assignment_id=assignment_id,
        target=DeliveryStatus.PICKED_UP,
        expected_version=2,
        idempotency_key="pickup",
    )
    assignment = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", assignment_id)
    assert assignment is not None
    assert assignment.payload["double_packaging"] == "false"
    assert assignment.payload["merchant_packaging_incomplete_is_rider_fault"] is False


def picked_up_service():
    service, store, assignment_id = accepted_service()
    seed_order(store)
    service.progress(
        context(),
        assignment_id=assignment_id,
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="arrive",
    )
    service.progress(
        context(),
        assignment_id=assignment_id,
        target=DeliveryStatus.PICKED_UP,
        expected_version=2,
        idempotency_key="pickup",
    )
    return service, store, assignment_id


def test_nonce_is_one_time_and_completion_creates_append_only_earning() -> None:
    service, store, assignment_id = picked_up_service()
    grant = service.issue_proof_grant(
        context(),
        assignment_id=assignment_id,
        grant_id="grant-1",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="grant-1",
    )
    service.complete_delivery(
        context(),
        assignment_id=assignment_id,
        grant_id=grant.grant_id,
        nonce=grant.nonce,
        evidence_receipt_ref="evidence:receipt-1",
        now=NOW,
        expected_assignment_version=3,
        idempotency_key="deliver-1",
    )
    earning = store.get(RecordKind.LEDGER_TRANSACTION, "branch-sejong", f"earning:{assignment_id}")
    assert earning is not None
    assert earning.payload["append_only"] is True
    assert earning.payload["estimated_net_won"] == 4_500
    reject(
        RiderErrorCode.STALE_VERSION,
        service.complete_delivery,
        context(),
        assignment_id=assignment_id,
        grant_id=grant.grant_id,
        nonce=grant.nonce,
        evidence_receipt_ref="evidence:receipt-2",
        now=NOW,
        expected_assignment_version=3,
        idempotency_key="nonce-reuse",
    )


def test_wrong_nonce_is_rejected_without_consuming_grant() -> None:
    service, store, assignment_id = picked_up_service()
    service.issue_proof_grant(
        context(),
        assignment_id=assignment_id,
        grant_id="grant-1",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="grant-1",
    )
    reject(
        RiderErrorCode.INVALID_REQUEST,
        service.complete_delivery,
        context(),
        assignment_id=assignment_id,
        grant_id="grant-1",
        nonce="forged",
        evidence_receipt_ref="evidence:receipt-1",
        now=NOW,
        expected_assignment_version=3,
        idempotency_key="bad-nonce",
    )
    grant = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", "proof-grant:grant-1")
    assert grant is not None and grant.payload["consumed"] is False


def test_arkaon_is_eta_advisory_only_and_cannot_execute() -> None:
    service, _, assignment_id = accepted_service()
    advisory = service.eta_advisory(context(source=ActorSource.ARKAON), assignment_id, 18)
    assert advisory["may_exclude"] is False
    assert advisory["may_penalize"] is False
    assert advisory["may_suspend"] is False
    assert advisory["may_alter_pay"] is False
    assert advisory["may_alter_insurance"] is False
    reject(
        RiderErrorCode.FORBIDDEN_AI_AUTHORITY,
        service.progress,
        context(source=ActorSource.ARKAON),
        assignment_id=assignment_id,
        target=DeliveryStatus.ARRIVED,
        expected_version=1,
        idempotency_key="ai-progress",
    )


def test_earnings_preview_and_route_manifest_contract() -> None:
    service, _, assignment_id = accepted_service()
    assert service.earnings_preview(context(), assignment_id) == {
        "gross_won": 6_000,
        "estimated_cost_won": 1_500,
        "estimated_net_won": 4_500,
    }
    assert len({route.route_id for route in RIDER_ROUTE_MANIFEST}) == 7
    assert all(route.idempotent for route in RIDER_ROUTE_MANIFEST if route.method != "GET")
