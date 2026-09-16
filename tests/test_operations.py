from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.network import (
    Branch,
    BranchRegistry,
    BranchStatus,
    BranchType,
    ServiceArea,
)
from narang_rider.operations import (
    AvailabilityStatus,
    IntakeOrder,
    Merchant,
    OrderChannel,
    OrderIntakeService,
    RiderAvailability,
    RiderAvailabilityService,
)

NOW = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)


def branches():
    value = BranchRegistry()
    value.register(
        Branch("hq", BranchType.HEADQUARTERS, "본사", None, BranchStatus.ACTIVE, (), 1, NOW)
    )
    value.register(
        Branch(
            "central", BranchType.REGIONAL_BRANCH, "중부", "hq", BranchStatus.ACTIVE, (), 1, NOW
        )
    )
    value.register(
        Branch(
            "sejong-hub",
            BranchType.LOCAL_HUB,
            "세종",
            "central",
            BranchStatus.ACTIVE,
            (ServiceArea("SJ-1", "SJ", "SJ"),),
            1,
            NOW,
        )
    )
    value.register(
        Branch(
            "seoul-hub",
            BranchType.LOCAL_HUB,
            "서울",
            "central",
            BranchStatus.ACTIVE,
            (ServiceArea("SEOUL-1", "SEOUL", "SEOUL"),),
            1,
            NOW,
        )
    )
    return value


def order(**changes):
    values = {
        "order_id": "order-1",
        "merchant_id": "merchant-1",
        "servicing_branch_id": "sejong-hub",
        "channel": OrderChannel.PARTNER_API,
        "external_order_id": "partner-order-1",
        "order_amount_won": 25_000,
        "customer_contact_ref": "vault:contact:1",
        "delivery_address_ref": "vault:address:1",
        "service_area_code": "SJ-1",
        "received_at": NOW,
        "payload_digest": "a" * 64,
    }
    values.update(changes)
    return IntakeOrder(**values)


def intake():
    service = OrderIntakeService(branches=branches())
    service.register_merchant(Merchant("merchant-1", "sejong-hub", True))
    return service


def test_multi_channel_order_is_idempotent_within_channel() -> None:
    service = intake()
    first = service.ingest(order())
    replay = service.ingest(replace(order(), order_id="retry-generated-id"))

    assert replay == first
    assert replay.order_id == "order-1"


def test_same_external_id_can_exist_on_distinct_channels() -> None:
    service = intake()
    partner = service.ingest(order())
    direct = service.ingest(
        order(
            order_id="order-2",
            channel=OrderChannel.DIRECT,
            payload_digest="b" * 64,
        )
    )

    assert partner.order_id != direct.order_id


def test_channel_order_replay_cannot_change_payload() -> None:
    service = intake()
    service.ingest(order())
    with pytest.raises(ValueError, match="CHANNEL_ORDER_IDEMPOTENCY_CONFLICT"):
        service.ingest(replace(order(), payload_digest="f" * 64))


def test_service_area_routes_to_one_accountable_branch() -> None:
    service = intake()
    with pytest.raises(ValueError, match="SERVICE_AREA_BRANCH_MISMATCH"):
        service.ingest(order(servicing_branch_id="seoul-hub"))


@pytest.mark.parametrize(
    "changes,error",
    (
        ({"customer_contact_ref": "010-1234-5678"}, "RAW_CUSTOMER_CONTACT_FORBIDDEN"),
        ({"delivery_address_ref": "서울시 실제주소"}, "RAW_DELIVERY_ADDRESS_FORBIDDEN"),
    ),
)
def test_raw_customer_data_is_rejected(changes, error) -> None:
    with pytest.raises(ValueError, match=error):
        order(**changes)


def rider(rider_id, *, branch_id="sejong-hub", since=NOW):
    return RiderAvailability(
        rider_id=rider_id,
        branch_id=branch_id,
        status=AvailabilityStatus.AVAILABLE,
        available_since=since,
        vehicle_type="motorcycle",
        insurance_ref=f"vault:insurance:{rider_id}",
        updated_at=NOW,
    )


def test_available_riders_are_branch_scoped_and_fifo_ordered() -> None:
    service = RiderAvailabilityService(branches=branches())
    service.register(rider("rider-new", since=NOW))
    service.register(rider("rider-old", since=NOW - timedelta(minutes=5)))
    service.register(rider("rider-seoul", branch_id="seoul-hub"))

    eligible = service.eligible_riders(branch_id="sejong-hub")
    assert [item.rider_id for item in eligible] == ["rider-old", "rider-new"]


def test_safety_stop_removes_rider_without_penalty() -> None:
    service = RiderAvailabilityService(branches=branches())
    service.register(rider("rider-1"))
    stopped = service.update(
        rider_id="rider-1",
        status=AvailabilityStatus.SAFETY_STOP,
        now=NOW + timedelta(minutes=1),
        safety_stop_reason="extreme wind",
    )

    assert stopped.penalty_allowed is False
    assert service.eligible_riders(branch_id="sejong-hub") == ()
    resumed = service.update(
        rider_id="rider-1",
        status=AvailabilityStatus.AVAILABLE,
        now=NOW + timedelta(minutes=30),
    )
    assert resumed.available_since == NOW + timedelta(minutes=30)


def test_safety_stop_requires_reason_and_cannot_enable_penalty() -> None:
    with pytest.raises(ValueError, match="SAFETY_STOP_REASON_REQUIRED"):
        replace(
            rider("rider-1"),
            status=AvailabilityStatus.SAFETY_STOP,
            available_since=None,
        )
    with pytest.raises(ValueError, match="AVAILABILITY_CANNOT_AUTHORIZE_PENALTY"):
        replace(rider("rider-1"), penalty_allowed=True)
