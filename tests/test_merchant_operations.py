from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.application import RiderCallRoute
from narang_rider.cancellation import CancellationPolicy, CancelStage
from narang_rider.merchant_operations import (
    MERCHANT_ROUTE_MANIFEST,
    ActorSource,
    DoublePackaging,
    MerchantCapability,
    MerchantCommandRejected,
    MerchantContext,
    MerchantErrorCode,
    MerchantOrderService,
    MerchantOrderState,
    PublicQuoteSnapshot,
)
from narang_rider.persistence import InMemoryPersistence, RecordKind

NOW = datetime(2026, 9, 16, 1, tzinfo=UTC)
CAPABILITIES = frozenset(MerchantCapability)


def context(
    *,
    merchant_id: str = "merchant-1",
    branch_id: str = "branch-sejong",
    source: ActorSource = ActorSource.HUMAN,
) -> MerchantContext:
    return MerchantContext("actor-1", merchant_id, branch_id, CAPABILITIES, source)


def quote(order_id: str = "order-1") -> PublicQuoteSnapshot:
    return PublicQuoteSnapshot(
        "quote-1",
        order_id,
        "branch-sejong",
        rider_pay_won=4_000,
        merchant_share_won=1_000,
        customer_fee_won=3_000,
        policy_id="pricing-2026-09",
        valid_until=NOW + timedelta(minutes=10),
    )


def service_with_draft():
    persistence = InMemoryPersistence()
    service = MerchantOrderService(persistence, persistence)
    service.create_draft(
        context(), order_id="order-1", idempotency_key="draft-1", prep_minutes_advisory=15
    )
    return service, persistence


def quoted_service():
    service, persistence = service_with_draft()
    service.confirm_quote(
        context(),
        quote=quote(),
        presented_amounts=(4_000, 1_000, 3_000),
        expected_version=1,
        idempotency_key="quote-1",
        now=NOW,
    )
    return service, persistence


def assert_code(expected: MerchantErrorCode, function, *args, **kwargs) -> None:
    with pytest.raises(MerchantCommandRejected) as raised:
        function(*args, **kwargs)
    assert raised.value.code is expected


def test_draft_and_immutable_public_quote_are_atomic() -> None:
    service, persistence = service_with_draft()
    receipt = service.confirm_quote(
        context(),
        quote=quote(),
        presented_amounts=(4_000, 1_000, 3_000),
        expected_version=1,
        idempotency_key="quote-1",
        now=NOW,
    )
    assert receipt.state is MerchantOrderState.QUOTED
    stored = persistence.get(RecordKind.PARTNER_EVENT, "branch-sejong", "quote:quote-1")
    assert stored is not None
    assert stored.payload["rider_pay_won"] == 4_000
    assert stored.payload["merchant_share_won"] == 1_000
    assert stored.payload["customer_fee_won"] == 3_000
    assert stored.payload["policy_id"] == "pricing-2026-09"
    outbox = persistence.get(RecordKind.OUTBOX_MESSAGE, "branch-sejong", "merchant-order:order-1:2")
    assert outbox is not None


def test_client_cannot_tamper_with_any_quote_amount() -> None:
    service, persistence = service_with_draft()
    assert_code(
        MerchantErrorCode.PRICE_TAMPERED,
        service.confirm_quote,
        context(),
        quote=quote(),
        presented_amounts=(3_000, 1_000, 3_000),
        expected_version=1,
        idempotency_key="tampered",
        now=NOW,
    )
    order = persistence.get(RecordKind.ORDER, "branch-sejong", "order-1")
    assert order is not None and order.version == 1


@pytest.mark.parametrize(
    ("version", "now", "code"),
    [
        (0, NOW, MerchantErrorCode.STALE_VERSION),
        (1, NOW + timedelta(minutes=11), MerchantErrorCode.STALE_QUOTE),
    ],
)
def test_stale_version_and_quote_fail_closed(version, now, code) -> None:
    service, _ = service_with_draft()
    assert_code(
        code,
        service.confirm_quote,
        context(),
        quote=quote(),
        presented_amounts=(4_000, 1_000, 3_000),
        expected_version=version,
        idempotency_key=f"stale-{version}",
        now=now,
    )


def test_cross_merchant_and_branch_idor_are_hidden_or_denied() -> None:
    service, _ = service_with_draft()
    assert_code(
        MerchantErrorCode.MERCHANT_SCOPE_MISMATCH,
        service.status,
        context(merchant_id="merchant-2"),
        "order-1",
    )
    assert_code(
        MerchantErrorCode.NOT_FOUND,
        service.status,
        context(branch_id="branch-busan"),
        "order-1",
    )


def test_submit_is_idempotent_and_global_call_lock_blocks_other_route() -> None:
    service, persistence = quoted_service()
    first = service.submit(
        context(),
        order_id="order-1",
        quote_id="quote-1",
        rider_call_route=RiderCallRoute.MERCHANT_DIRECT,
        expected_version=2,
        idempotency_key="submit-1",
    )
    replay = service.submit(
        context(),
        order_id="order-1",
        quote_id="quote-1",
        rider_call_route=RiderCallRoute.MERCHANT_DIRECT,
        expected_version=2,
        idempotency_key="submit-1",
    )
    assert first.state is MerchantOrderState.SUBMITTED
    assert replay.replayed is True
    call = persistence.get(RecordKind.RIDER_CALL, "branch-sejong", "order-1")
    assert call is not None and call.payload["route"] == "merchant_direct"
    assert_code(
        MerchantErrorCode.STALE_VERSION,
        service.submit,
        context(),
        order_id="order-1",
        quote_id="quote-1",
        rider_call_route=RiderCallRoute.RIDER_COMPANY,
        expected_version=2,
        idempotency_key="submit-2",
    )


@pytest.mark.parametrize("packaging", list(DoublePackaging))
def test_all_packaging_states_preserve_merchant_responsibility(packaging) -> None:
    service, persistence = service_with_draft()
    service.record_packaging(
        context(),
        order_id="order-1",
        double_packaging=packaging,
        seal_number="SEAL-2026_01",
        completed_at=NOW,
        expected_version=1,
        idempotency_key=f"pack-{packaging.value}",
    )
    order = persistence.get(RecordKind.ORDER, "branch-sejong", "order-1")
    assert order is not None
    assert order.payload["double_packaging"] == packaging.value
    assert order.payload["packaging_responsibility"] == "MERCHANT"
    assert order.payload["rider_fault_inferred"] is False


def test_seal_number_rejects_pii_shaped_or_unsafe_text() -> None:
    service, _ = service_with_draft()
    assert_code(
        MerchantErrorCode.INVALID_REQUEST,
        service.record_packaging,
        context(),
        order_id="order-1",
        double_packaging=DoublePackaging.TRUE,
        seal_number="customer@example.com",
        completed_at=NOW,
        expected_version=1,
        idempotency_key="pack-unsafe",
    )


def test_post_pickup_cancel_routes_to_human_without_rider_clawback() -> None:
    service, persistence = quoted_service()
    receipt = service.cancel_order(
        context(),
        order_id="order-1",
        stage=CancelStage.PICKED_UP,
        food_handed_over=True,
        policy=CancellationPolicy(1_000, 2_000),
        expected_version=2,
        idempotency_key="cancel-1",
    )
    assert receipt.state is MerchantOrderState.CANCEL_REVIEW
    assert receipt.result.human_review_required is True
    assert receipt.result.rider_penalty_allowed is False
    event = persistence.get(RecordKind.PARTNER_EVENT, "branch-sejong", "cancel:order-1:3")
    assert event is not None
    assert event.payload["automatic_rider_clawback_allowed"] is False


@pytest.mark.parametrize("action", ["confirm", "submit", "cancel"])
def test_arkaon_is_advisory_only_for_consequential_commands(action) -> None:
    service, _ = quoted_service() if action != "confirm" else service_with_draft()
    ai = context(source=ActorSource.ARKAON)
    if action == "confirm":
        call = lambda: service.confirm_quote(
            ai,
            quote=quote(),
            presented_amounts=(4_000, 1_000, 3_000),
            expected_version=1,
            idempotency_key="ai-confirm",
            now=NOW,
        )
    elif action == "submit":
        call = lambda: service.submit(
            ai,
            order_id="order-1",
            quote_id="quote-1",
            rider_call_route=RiderCallRoute.MERCHANT_DIRECT,
            expected_version=2,
            idempotency_key="ai-submit",
        )
    else:
        call = lambda: service.cancel_order(
            ai,
            order_id="order-1",
            stage=CancelStage.ASSIGNED,
            food_handed_over=False,
            policy=CancellationPolicy(1_000, 2_000),
            expected_version=2,
            idempotency_key="ai-cancel",
        )
    assert_code(MerchantErrorCode.FORBIDDEN_AI_AUTHORITY, call)


def test_arkaon_may_only_add_a_prep_time_advisory_to_draft() -> None:
    persistence = InMemoryPersistence()
    service = MerchantOrderService(persistence, persistence)
    service.create_draft(
        context(source=ActorSource.ARKAON),
        order_id="order-ai",
        idempotency_key="ai-advisory",
        prep_minutes_advisory=22,
    )
    order = persistence.get(RecordKind.ORDER, "branch-sejong", "order-ai")
    assert order is not None
    assert order.payload["prep_minutes_advisory"] == 22
    assert "penalty" not in order.payload
    assert "price_cut" not in order.payload


def test_command_capability_and_idempotency_payload_are_enforced() -> None:
    persistence = InMemoryPersistence()
    service = MerchantOrderService(persistence, persistence)
    denied = MerchantContext("actor", "merchant-1", "branch-sejong", frozenset())
    assert_code(
        MerchantErrorCode.AUTHORIZATION_DENIED,
        service.create_draft,
        denied,
        order_id="order-1",
        idempotency_key="same-key",
    )
    service.create_draft(context(), order_id="order-1", idempotency_key="same-key")
    assert_code(
        MerchantErrorCode.IDEMPOTENCY_CONFLICT,
        service.create_draft,
        context(),
        order_id="order-2",
        idempotency_key="same-key",
    )


def test_route_manifest_is_stable_and_all_mutations_are_idempotent() -> None:
    assert len({route.route_id for route in MERCHANT_ROUTE_MANIFEST}) == 6
    assert len({(route.method, route.path_template) for route in MERCHANT_ROUTE_MANIFEST}) == 6
    assert all(route.idempotent for route in MERCHANT_ROUTE_MANIFEST if route.method != "GET")
    assert {route.request_dto for route in MERCHANT_ROUTE_MANIFEST} >= {
        "MerchantDraftV1",
        "QuoteConfirmationV1",
        "OrderSubmissionV1",
        "PackagingEvidenceV1",
        "MerchantCancellationV1",
    }
