from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.lifecycle import (
    OfferLeaseService,
    OfferStatus,
    OrderRepository,
    OrderState,
    QuoteRepository,
)
from narang_rider.money import Money
from narang_rider.pricing import DeliveryFacts, PricingPolicy, quote_delivery


NOW = datetime(2026, 9, 16, 1, 0, tzinfo=UTC)


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
        DeliveryFacts(
            distance_m=2_000,
            expected_active_minutes=20,
            expected_wait_minutes=10,
            expected_return_distance_m=1_000,
            expected_return_minutes=10,
            bundled_orders=1,
        ),
        customer_delivery_fee=Money(3_500),
        expected_direct_cost=Money(800),
    )


def offering_order():
    orders = OrderRepository()
    order = orders.create(order_id="order-1", event_id="evt-create", occurred_at=NOW)
    order = orders.transition(
        order_id=order.order_id,
        event_id="evt-quote",
        to_state=OrderState.QUOTED,
        occurred_at=NOW,
        reason_code="QUOTE_SAVED",
        expected_version=order.version,
    )
    order = orders.transition(
        order_id=order.order_id,
        event_id="evt-offering",
        to_state=OrderState.OFFERING,
        occurred_at=NOW,
        reason_code="DISPATCH_STARTED",
        expected_version=order.version,
    )
    return orders, order


def saved_quote():
    quotes = QuoteRepository()
    snapshot = quotes.save(
        quote_id="quote-1",
        order_id="order-1",
        quote=public_quote(),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    return quotes, snapshot


def test_order_state_machine_is_append_only_and_optimistically_locked() -> None:
    orders, order = offering_order()

    assert order.state is OrderState.OFFERING
    assert [event.to_state for event in orders.events(order.order_id)] == [
        OrderState.CREATED,
        OrderState.QUOTED,
        OrderState.OFFERING,
    ]
    with pytest.raises(ValueError, match="ORDER_VERSION_CONFLICT"):
        orders.transition(
            order_id=order.order_id,
            event_id="evt-stale",
            to_state=OrderState.ASSIGNED,
            occurred_at=NOW,
            reason_code="OFFER_ACCEPTED",
            expected_version=1,
        )


def test_illegal_transition_fails_closed() -> None:
    orders = OrderRepository()
    order = orders.create(order_id="order-1", event_id="evt-create", occurred_at=NOW)

    with pytest.raises(ValueError, match="ILLEGAL_ORDER_TRANSITION"):
        orders.transition(
            order_id=order.order_id,
            event_id="evt-skip",
            to_state=OrderState.DELIVERED,
            occurred_at=NOW,
            reason_code="SKIP",
            expected_version=order.version,
        )


def test_event_idempotency_cannot_be_rebound() -> None:
    orders, order = offering_order()
    accepted = orders.transition(
        order_id=order.order_id,
        event_id="evt-accept",
        to_state=OrderState.ASSIGNED,
        occurred_at=NOW,
        reason_code="OFFER_ACCEPTED",
        expected_version=order.version,
    )
    replay = orders.transition(
        order_id=order.order_id,
        event_id="evt-accept",
        to_state=OrderState.ASSIGNED,
        occurred_at=NOW + timedelta(seconds=1),
        reason_code="OFFER_ACCEPTED_RETRY",
        expected_version=order.version,
    )

    assert replay == accepted
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        orders.transition(
            order_id="other-order",
            event_id="evt-accept",
            to_state=OrderState.CANCELLED,
            occurred_at=NOW,
            reason_code="REBIND",
            expected_version=1,
        )


def test_quote_snapshot_is_immutable_idempotent_and_expires() -> None:
    quotes, first = saved_quote()
    replay = quotes.save(
        quote_id="quote-1",
        order_id="order-1",
        quote=public_quote(),
        created_at=NOW + timedelta(seconds=30),
        expires_at=NOW + timedelta(minutes=15),
    )

    assert replay == first
    assert first.rider_pay_won == public_quote().rider_pay.won
    assert first.digest
    with pytest.raises(ValueError, match="QUOTE_IDEMPOTENCY_CONFLICT"):
        quotes.save(
            quote_id="quote-1",
            order_id="order-1",
            quote=public_quote(),
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=16),
        )


def test_only_one_active_offer_exists_per_order() -> None:
    _, order = offering_order()
    _, quote = saved_quote()
    service = OfferLeaseService()
    service.issue(
        offer_id="offer-1",
        order=order,
        rider_id="rider-1",
        quote=quote,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ValueError, match="ACTIVE_OFFER_EXISTS"):
        service.issue(
            offer_id="offer-2",
            order=order,
            rider_id="rider-2",
            quote=quote,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )


def test_offer_acceptance_is_bound_authenticated_and_idempotent() -> None:
    _, order = offering_order()
    _, quote = saved_quote()
    service = OfferLeaseService()
    _, token = service.issue(
        offer_id="offer-1",
        order=order,
        rider_id="rider-1",
        quote=quote,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ValueError, match="OFFER_RIDER_MISMATCH"):
        service.decide(
            command_id="cmd-wrong-rider",
            offer_id="offer-1",
            rider_id="rider-2",
            token=token,
            accept=True,
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="INVALID_OFFER_TOKEN"):
        service.decide(
            command_id="cmd-wrong-token",
            offer_id="offer-1",
            rider_id="rider-1",
            token="wrong",
            accept=True,
            now=NOW + timedelta(seconds=1),
        )

    accepted = service.decide(
        command_id="cmd-accept",
        offer_id="offer-1",
        rider_id="rider-1",
        token=token,
        accept=True,
        now=NOW + timedelta(seconds=2),
    )
    replay = service.decide(
        command_id="cmd-accept",
        offer_id="offer-1",
        rider_id="rider-1",
        token=token,
        accept=True,
        now=NOW + timedelta(seconds=3),
    )

    assert accepted.status is OfferStatus.ACCEPTED
    assert replay == accepted


def test_late_acceptance_expires_and_releases_order() -> None:
    _, order = offering_order()
    _, quote = saved_quote()
    service = OfferLeaseService()
    _, token = service.issue(
        offer_id="offer-1",
        order=order,
        rider_id="rider-1",
        quote=quote,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
    )

    expired = service.decide(
        command_id="cmd-late",
        offer_id="offer-1",
        rider_id="rider-1",
        token=token,
        accept=True,
        now=NOW + timedelta(seconds=10),
    )

    assert expired.status is OfferStatus.EXPIRED
    replacement, _ = service.issue(
        offer_id="offer-2",
        order=order,
        rider_id="rider-2",
        quote=quote,
        issued_at=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(seconds=30),
    )
    assert replacement.status is OfferStatus.ACTIVE


def test_offer_cannot_outlive_quote_or_use_another_orders_quote() -> None:
    _, order = offering_order()
    _, quote = saved_quote()
    service = OfferLeaseService()

    with pytest.raises(ValueError, match="INVALID_OFFER_EXPIRY"):
        service.issue(
            offer_id="offer-long",
            order=order,
            rider_id="rider-1",
            quote=quote,
            issued_at=NOW,
            expires_at=quote.expires_at + timedelta(seconds=1),
        )

    foreign = QuoteRepository().save(
        quote_id="quote-other",
        order_id="other-order",
        quote=public_quote(),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    with pytest.raises(ValueError, match="QUOTE_ORDER_MISMATCH"):
        service.issue(
            offer_id="offer-foreign",
            order=order,
            rider_id="rider-1",
            quote=foreign,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )


def test_decline_is_neutral_and_does_not_modify_order_state() -> None:
    _, order = offering_order()
    _, quote = saved_quote()
    service = OfferLeaseService()
    _, token = service.issue(
        offer_id="offer-1",
        order=order,
        rider_id="rider-1",
        quote=quote,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    declined = service.decide(
        command_id="cmd-decline",
        offer_id="offer-1",
        rider_id="rider-1",
        token=token,
        accept=False,
        now=NOW + timedelta(seconds=1),
    )

    assert declined.status is OfferStatus.DECLINED
    assert order.state is OrderState.OFFERING
    assert not hasattr(declined, "penalty")
    assert not hasattr(declined, "acceptance_rate")
