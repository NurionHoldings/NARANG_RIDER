import pytest

from narang_rider.cancellation import (
    CancelActor,
    CancellationPolicy,
    CancelStage,
    cancel,
)

POLICY = CancellationPolicy(500, 1500)


def test_arrival_cancel_pays_rider_and_charges_customer():
    result = cancel(
        order_id="o",
        actor=CancelActor.CUSTOMER,
        stage=CancelStage.ARRIVED,
        policy=POLICY,
        food_handed_over=False,
    )
    assert result.rider_pay_won == 1500
    assert result.customer_charge_won == 1500
    assert not result.rider_penalty_allowed


def test_merchant_cancel_funds_rider():
    result = cancel(
        order_id="o",
        actor=CancelActor.MERCHANT,
        stage=CancelStage.ASSIGNED,
        policy=POLICY,
        food_handed_over=False,
    )
    assert result.merchant_charge_won == 500


def test_post_pickup_requires_human_review_and_blocks_redispatch():
    result = cancel(
        order_id="o",
        actor=CancelActor.PLATFORM,
        stage=CancelStage.PICKED_UP,
        policy=POLICY,
        food_handed_over=True,
    )
    assert result.human_review_required
    assert not result.redispatch_allowed


def test_negative_policy_fails():
    with pytest.raises(ValueError, match="INVALID_CANCEL_POLICY"):
        cancel(
            order_id="o",
            actor=CancelActor.CUSTOMER,
            stage=CancelStage.ASSIGNED,
            policy=CancellationPolicy(-1, 0),
            food_handed_over=False,
        )
