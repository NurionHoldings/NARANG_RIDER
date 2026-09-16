# ruff: noqa
import pytest
from narang_rider.cancellation import *
P=CancellationPolicy(500,1500)
def test_arrival_cancel_pays_rider_and_charges_customer():
 r=cancel(order_id="o",actor=CancelActor.CUSTOMER,stage=CancelStage.ARRIVED,policy=P,food_handed_over=False)
 assert r.rider_pay_won==1500 and r.customer_charge_won==1500 and not r.rider_penalty_allowed
def test_merchant_cancel_funds_rider():
 r=cancel(order_id="o",actor=CancelActor.MERCHANT,stage=CancelStage.ASSIGNED,policy=P,food_handed_over=False)
 assert r.merchant_charge_won==500
def test_post_pickup_requires_human_review_and_blocks_redispatch():
 r=cancel(order_id="o",actor=CancelActor.PLATFORM,stage=CancelStage.PICKED_UP,policy=P,food_handed_over=True)
 assert r.human_review_required and not r.redispatch_allowed
def test_negative_policy_fails():
 with pytest.raises(ValueError): cancel(order_id="o",actor=CancelActor.CUSTOMER,stage=CancelStage.ASSIGNED,policy=CancellationPolicy(-1,0),food_handed_over=False)
