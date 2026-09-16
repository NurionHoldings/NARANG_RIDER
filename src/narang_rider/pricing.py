from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .money import Money


@dataclass(frozen=True)
class DeliveryFacts:
    distance_m: int
    expected_active_minutes: int
    expected_wait_minutes: int
    expected_return_distance_m: int
    expected_return_minutes: int = 0
    bundled_orders: int = 1
    safety_surcharge_won: int = 0

    def __post_init__(self) -> None:
        values = (
            self.distance_m,
            self.expected_active_minutes,
            self.expected_wait_minutes,
            self.expected_return_distance_m,
            self.expected_return_minutes,
            self.safety_surcharge_won,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("DELIVERY_FACTS_MUST_BE_INTEGERS")
        if any(value < 0 for value in values) or self.bundled_orders < 1:
            raise ValueError("DELIVERY_FACTS_OUT_OF_RANGE")


@dataclass(frozen=True)
class PricingPolicy:
    policy_id: str
    effective_from: datetime
    base_pay_won: int
    distance_unit_m: int
    distance_unit_pay_won: int
    included_distance_m: int
    wait_free_minutes: int
    wait_minute_pay_won: int
    return_unit_m: int
    return_unit_pay_won: int
    bundle_increment_won: int
    platform_cost_won: int
    safety_fund_won: int
    regional_fund_won: int
    merchant_delivery_cap_won: int

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or self.effective_from.tzinfo is None:
            raise ValueError("VERSIONED_POLICY_REQUIRED")
        integer_values = tuple(
            value
            for name, value in vars(self).items()
            if name.endswith(("_won", "_m", "_minutes"))
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise TypeError("PRICING_POLICY_VALUES_MUST_BE_INTEGERS")
        if any(value < 0 for value in integer_values):
            raise ValueError("PRICING_POLICY_VALUES_MUST_NOT_BE_NEGATIVE")
        if self.distance_unit_m == 0 or self.return_unit_m == 0:
            raise ValueError("PRICING_UNIT_MUST_BE_POSITIVE")


@dataclass(frozen=True)
class PublicQuote:
    policy_id: str
    rider_pay: Money
    customer_delivery_fee: Money
    merchant_delivery_share: Money
    platform_cost: Money
    safety_fund: Money
    regional_fund: Money
    expected_direct_cost: Money
    expected_total_minutes: int
    expected_net_per_order: Money
    expected_net_hourly: Money
    explanation_codes: tuple[str, ...]

    @property
    def funding_total(self) -> Money:
        return self.customer_delivery_fee + self.merchant_delivery_share

    @property
    def use_total(self) -> Money:
        return self.rider_pay + self.platform_cost + self.safety_fund + self.regional_fund

    def __post_init__(self) -> None:
        if self.expected_total_minutes <= 0:
            raise ValueError("POSITIVE_EXPECTED_TIME_REQUIRED")
        if self.funding_total != self.use_total:
            raise ValueError("QUOTE_DOES_NOT_BALANCE")
        if self.merchant_delivery_share.won < 0:
            raise ValueError("NEGATIVE_MERCHANT_SHARE")


def _ceiling_units(value: int, unit: int) -> int:
    return (value + unit - 1) // unit


def quote_delivery(
    policy: PricingPolicy,
    facts: DeliveryFacts,
    *,
    customer_delivery_fee: Money,
    expected_direct_cost: Money,
) -> PublicQuote:
    if min(customer_delivery_fee.won, expected_direct_cost.won) < 0:
        raise ValueError("QUOTE_INPUT_MUST_NOT_BE_NEGATIVE")
    total_minutes = (
        facts.expected_active_minutes
        + facts.expected_wait_minutes
        + facts.expected_return_minutes
    )
    if total_minutes <= 0:
        raise ValueError("POSITIVE_EXPECTED_TIME_REQUIRED")
    paid_distance = max(0, facts.distance_m - policy.included_distance_m)
    distance_pay = _ceiling_units(paid_distance, policy.distance_unit_m) * policy.distance_unit_pay_won
    paid_wait = max(0, facts.expected_wait_minutes - policy.wait_free_minutes)
    wait_pay = paid_wait * policy.wait_minute_pay_won
    return_pay = (
        _ceiling_units(facts.expected_return_distance_m, policy.return_unit_m)
        * policy.return_unit_pay_won
    )


    bundle_pay = (facts.bundled_orders - 1) * policy.bundle_increment_won
    rider_pay = Money(
        policy.base_pay_won
        + distance_pay
        + wait_pay
        + return_pay
        + bundle_pay
        + facts.safety_surcharge_won
    )
    fixed_uses = Money(policy.platform_cost_won + policy.safety_fund_won + policy.regional_fund_won)
    merchant_share = rider_pay + fixed_uses - customer_delivery_fee
    if merchant_share.won < 0:
        raise ValueError("CUSTOMER_FEE_EXCEEDS_PUBLIC_USES")
    if merchant_share.won > policy.merchant_delivery_cap_won:
        raise ValueError("MERCHANT_DELIVERY_CAP_EXCEEDED")
    net = rider_pay - expected_direct_cost
    return PublicQuote(
        policy_id=policy.policy_id,
        rider_pay=rider_pay,
        customer_delivery_fee=customer_delivery_fee,
        merchant_delivery_share=merchant_share,
        platform_cost=Money(policy.platform_cost_won),
        safety_fund=Money(policy.safety_fund_won),
        regional_fund=Money(policy.regional_fund_won),
        expected_direct_cost=expected_direct_cost,
        expected_total_minutes=total_minutes,
        expected_net_per_order=Money(net.won // facts.bundled_orders),
        expected_net_hourly=Money(net.won * 60 // total_minutes),
        explanation_codes=(
            "BASE_PAY",
            "DISTANCE_PAY",
            "WAIT_PAY",
            "RETURN_DISTANCE_PAY",
            "BUNDLE_INCREMENT",
            "SAFETY_SURCHARGE",
        ),
    )


@dataclass(frozen=True)
class MerchantOrderEconomics:
    order_revenue: Money
    food_and_labor_cost: Money
    packaging_cost: Money
    payment_cost: Money
    platform_cost: Money
    delivery_share: Money
    discount_share: Money
    refund_cost: Money

    def __post_init__(self) -> None:
        if any(value.won < 0 for value in vars(self).values()):
            raise ValueError("MERCHANT_ECONOMIC_INPUT_MUST_NOT_BE_NEGATIVE")

    @property
    def contribution_profit(self) -> Money:
        costs = sum(
            value.won for name, value in vars(self).items() if name != "order_revenue"
        )
        return Money(self.order_revenue.won - costs)
