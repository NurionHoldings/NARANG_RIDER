from concurrent.futures import ThreadPoolExecutor

import pytest

from narang_rider.application import (
    CommandCapability,
    CommandContext,
    IntakeCommandRejected,
    IntakeErrorCode,
    OrderIntakeApplicationService,
    OrderIntakeCommand,
    RiderCallRoute,
)
from narang_rider.persistence import InMemoryPersistence, RecordKind

BRANCH = "branch-sejong-01"


def context(
    *,
    branch_id: str = BRANCH,
    capabilities: frozenset[CommandCapability] | None = None,
) -> CommandContext:
    return CommandContext(
        actor_id="merchant-operator-1",
        branch_id=branch_id,
        capabilities=(
            capabilities
            if capabilities is not None
            else frozenset({CommandCapability.INGEST_ORDER, CommandCapability.CALL_RIDER})
        ),
    )


def command(
    *,
    source_order_id: str = "external-1",
    idempotency_key: str = "request-1",
    branch_id: str = BRANCH,
    route: RiderCallRoute = RiderCallRoute.MERCHANT_DIRECT,
    total_won: int = 18_000,
    attributes: dict[str, object] | None = None,
) -> OrderIntakeCommand:
    return OrderIntakeCommand(
        source_system="dosirak.store",
        source_order_id=source_order_id,
        idempotency_key=idempotency_key,
        branch_id=branch_id,
        merchant_id="merchant-1",
        total_won=total_won,
        delivery_address_vault_ref="vault://pii/address/1",
        recipient_phone_vault_ref="vault://pii/phone/1",
        rider_call_route=route,
        attributes=attributes,
    )


def test_canonical_intake_commits_order_event_call_and_ordered_outbox() -> None:
    store = InMemoryPersistence()
    receipt = OrderIntakeApplicationService(store).ingest(context(), command())

    assert receipt.order_id.startswith("ord_")
    assert receipt.status_sequence == 1
    assert receipt.persistence_receipt.versions == (1, 1, 1, 1)
    order = store.get(RecordKind.ORDER, BRANCH, receipt.order_id)
    rider_call = store.get(RecordKind.RIDER_CALL, BRANCH, receipt.order_id)
    outbox = store.get(RecordKind.OUTBOX_MESSAGE, BRANCH, f"order-status:{receipt.order_id}:1")
    assert order.payload["state"] == "RECEIVED"
    assert rider_call.payload["route"] == "merchant_direct"
    assert outbox.payload["sequence"] == 1
    assert outbox.payload["status"] == "RECEIVED"


def test_exact_retry_is_idempotent_and_creates_no_duplicates() -> None:
    store = InMemoryPersistence()
    service = OrderIntakeApplicationService(store)

    first = service.ingest(context(), command())
    replay = service.ingest(context(), command())

    assert replay.order_id == first.order_id
    assert replay.persistence_receipt.replayed is True
    assert store.get(RecordKind.ORDER, BRANCH, first.order_id).version == 1
    assert store.get(RecordKind.RIDER_CALL, BRANCH, first.order_id).version == 1


def test_same_idempotency_key_with_changed_payload_fails_closed() -> None:
    store = InMemoryPersistence()
    service = OrderIntakeApplicationService(store)
    service.ingest(context(), command(total_won=18_000))

    with pytest.raises(IntakeCommandRejected) as caught:
        service.ingest(context(), command(total_won=99_000))

    assert caught.value.code is IntakeErrorCode.IDEMPOTENCY_CONFLICT


def test_same_source_order_through_company_route_cannot_create_second_call() -> None:
    store = InMemoryPersistence()
    service = OrderIntakeApplicationService(store)
    first = service.ingest(context(), command(route=RiderCallRoute.MERCHANT_DIRECT))

    with pytest.raises(IntakeCommandRejected) as caught:
        service.ingest(
            context(),
            command(idempotency_key="request-2", route=RiderCallRoute.RIDER_COMPANY),
        )

    assert caught.value.code is IntakeErrorCode.CONCURRENT_CONFLICT
    rider_call = store.get(RecordKind.RIDER_CALL, BRANCH, first.order_id)
    assert rider_call.payload["route"] == "merchant_direct"
    assert rider_call.version == 1


def test_concurrent_duplicate_requests_publish_exactly_one_bundle() -> None:
    store = InMemoryPersistence()
    service = OrderIntakeApplicationService(store)

    def ingest(index: int) -> str:
        try:
            receipt = service.ingest(context(), command(idempotency_key=f"request-{index}"))
            return receipt.order_id
        except IntakeCommandRejected as error:
            return error.code.value

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(ingest, range(8)))

    committed = [value for value in outcomes if value.startswith("ord_")]
    assert len(committed) == 1
    assert outcomes.count(IntakeErrorCode.CONCURRENT_CONFLICT.value) == 7
    order_id = committed[0]
    assert store.get(RecordKind.ORDER, BRANCH, order_id).version == 1
    assert store.get(RecordKind.RIDER_CALL, BRANCH, order_id).version == 1


def test_crash_rolls_back_entire_intake_and_retry_succeeds() -> None:
    store = InMemoryPersistence()
    store.arm_crash_before_publish()
    service = OrderIntakeApplicationService(store)
    intake = command()

    with pytest.raises(IntakeCommandRejected) as caught:
        service.ingest(context(), intake)

    assert caught.value.code is IntakeErrorCode.RETRYABLE_PERSISTENCE_FAILURE
    order_id = service._canonical_order_id(intake)
    assert store.get(RecordKind.ORDER, BRANCH, order_id) is None
    assert store.get(RecordKind.RIDER_CALL, BRANCH, order_id) is None
    assert service.ingest(context(), intake).persistence_receipt.replayed is False


def test_cross_branch_command_is_denied_before_storage() -> None:
    store = InMemoryPersistence()
    with pytest.raises(IntakeCommandRejected) as caught:
        OrderIntakeApplicationService(store).ingest(
            context(branch_id="branch-busan-01"), command(branch_id=BRANCH)
        )

    assert caught.value.code is IntakeErrorCode.BRANCH_SCOPE_MISMATCH


@pytest.mark.parametrize(
    "capabilities",
    [
        frozenset(),
        frozenset({CommandCapability.INGEST_ORDER}),
        frozenset({CommandCapability.CALL_RIDER}),
    ],
)
def test_missing_capability_is_denied(capabilities) -> None:
    store = InMemoryPersistence()
    with pytest.raises(IntakeCommandRejected) as caught:
        OrderIntakeApplicationService(store).ingest(context(capabilities=capabilities), command())

    assert caught.value.code is IntakeErrorCode.AUTHORIZATION_DENIED


@pytest.mark.parametrize(
    "field,value",
    [
        ("delivery_address_vault_ref", "세종시 어느로 1"),
        ("recipient_phone_vault_ref", "010-0000-0000"),
    ],
)
def test_raw_pii_in_reference_fields_is_rejected_during_validation(field, value) -> None:
    values = {
        "source_system": "dosirak.store",
        "source_order_id": "external-1",
        "idempotency_key": "request-1",
        "branch_id": BRANCH,
        "merchant_id": "merchant-1",
        "total_won": 18_000,
        "delivery_address_vault_ref": "vault://pii/address/1",
        "recipient_phone_vault_ref": "vault://pii/phone/1",
        "rider_call_route": RiderCallRoute.MERCHANT_DIRECT,
    }
    values[field] = value
    with pytest.raises(ValueError, match="vault reference"):
        OrderIntakeCommand(**values)


def test_raw_pii_hidden_in_attributes_maps_to_stable_policy_error() -> None:
    store = InMemoryPersistence()
    with pytest.raises(IntakeCommandRejected) as caught:
        OrderIntakeApplicationService(store).ingest(
            context(), command(attributes={"customer": {"phone": "010-0000-0000"}})
        )

    assert caught.value.code is IntakeErrorCode.PII_POLICY_VIOLATION


def test_negative_order_amount_is_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        command(total_won=-1)
