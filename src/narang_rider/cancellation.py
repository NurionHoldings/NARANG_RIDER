from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CancelActor(StrEnum):
    CUSTOMER = "CUSTOMER"
    MERCHANT = "MERCHANT"
    RIDER = "RIDER"
    PLATFORM = "PLATFORM"


class CancelStage(StrEnum):
    BEFORE_ASSIGNMENT = "BEFORE_ASSIGNMENT"
    ASSIGNED = "ASSIGNED"
    ARRIVED = "ARRIVED"
    PICKED_UP = "PICKED_UP"


@dataclass(frozen=True)
class CancellationPolicy:
    assigned_pay_won: int
    arrived_pay_won: int


@dataclass(frozen=True)
class CancellationResult:
    order_id: str
    rider_pay_won: int
    customer_charge_won: int
    merchant_charge_won: int
    redispatch_allowed: bool
    human_review_required: bool
    rider_penalty_allowed: bool = False


def cancel(
    *,
    order_id: str,
    actor: CancelActor,
    stage: CancelStage,
    policy: CancellationPolicy,
    food_handed_over: bool,
) -> CancellationResult:
    if not order_id.strip():
        raise ValueError("ORDER_ID_REQUIRED")
    if min(policy.assigned_pay_won, policy.arrived_pay_won) < 0:
        raise ValueError("INVALID_CANCEL_POLICY")

    rider_pay_won = 0
    if stage is CancelStage.ASSIGNED:
        rider_pay_won = policy.assigned_pay_won
    elif stage is CancelStage.ARRIVED:
        rider_pay_won = policy.arrived_pay_won

    if stage is CancelStage.PICKED_UP or food_handed_over:
        return CancellationResult(
            order_id=order_id,
            rider_pay_won=rider_pay_won,
            customer_charge_won=0,
            merchant_charge_won=0,
            redispatch_allowed=False,
            human_review_required=True,
        )

    return CancellationResult(
        order_id=order_id,
        rider_pay_won=rider_pay_won,
        customer_charge_won=rider_pay_won if actor is CancelActor.CUSTOMER else 0,
        merchant_charge_won=rider_pay_won if actor is CancelActor.MERCHANT else 0,
        redispatch_allowed=True,
        human_review_required=False,
    )
