from datetime import UTC, datetime

import pytest

from narang_rider.dispatch import DispatchCandidate, FairDispatchPolicy
from narang_rider.ledger import Ledger, settlement_from_quote
from narang_rider.money import Money
from narang_rider.pricing import (
    DeliveryFacts,
    MerchantOrderEconomics,
    PricingPolicy,
    quote_delivery,
)


def policy(**changes) -> PricingPolicy:
    values = {
        "policy_id": "sejong-v1",
        "effective_from": datetime(2026, 9, 16, tzinfo=UTC),
        "base_pay_won": 3_000,
        "distance_unit_m": 500,
        "distance_unit_pay_won": 300,
        "included_distance_m": 1_000,
        "wait_free_minutes": 5,
        "wait_minute_pay_won": 150,
        "return_unit_m": 500,
        "return_unit_pay_won": 200,
        "bundle_increment_won": 1_800,
        "platform_cost_won": 300,
        "safety_fund_won": 100,
        "regional_fund_won": 100,
        "merchant_delivery_cap_won": 3_000,
    }
    values.update(changes)
    return PricingPolicy(**values)


def quote(**fact_changes):
    facts = {
        "distance_m": 2_000,
        "expected_active_minutes": 20,
        "expected_wait_minutes": 10,
        "expected_return_distance_m": 1_000,
        "expected_return_minutes": 10,
        "bundled_orders": 1,
    }
    facts.update(fact_changes)
    bundled_orders = facts["bundled_orders"]
    return quote_delivery(
        policy(),
        DeliveryFacts(**facts),
        customer_delivery_fee=Money(3_500 * bundled_orders),
        expected_direct_cost=Money(800),
    )


def test_public_quote_pays_distance_wait_and_return_and_balances() -> None:
    result = quote()

    assert result.rider_pay == Money(4_750)
    assert result.merchant_delivery_share == Money(1_750)
    assert result.expected_net_per_order == Money(3_950)
    assert result.expected_net_hourly == Money(5_925)
    assert result.funding_total == result.use_total
    assert "WAIT_PAY" in result.explanation_codes
    assert "RETURN_DISTANCE_PAY" in result.explanation_codes


def test_boundary_units_round_up_without_distance_gaps() -> None:
    at_boundary = quote(distance_m=1_500, expected_wait_minutes=5, expected_return_distance_m=500)
    over_boundary = quote(distance_m=1_501, expected_wait_minutes=6, expected_return_distance_m=501)

    assert at_boundary.rider_pay == Money(3_500)
    assert over_boundary.rider_pay == Money(4_150)


def test_bundle_increment_is_visible_and_net_is_per_order() -> None:
    single = quote()
    bundle = quote(bundled_orders=2)

    assert bundle.rider_pay - single.rider_pay == Money(1_800)
    assert bundle.expected_net_per_order == Money((bundle.rider_pay.won - 800) // 2)


def test_merchant_cost_cap_fails_closed() -> None:
    with pytest.raises(ValueError, match="MERCHANT_DELIVERY_CAP_EXCEEDED"):
        quote_delivery(
            policy(merchant_delivery_cap_won=1_000),
            DeliveryFacts(4_000, 30, 20, 3_000, 10),
            customer_delivery_fee=Money(1_000),
            expected_direct_cost=Money(1_000),
        )


def test_zero_duration_quote_is_rejected() -> None:
    facts = DeliveryFacts(1, 0, 0, 0)
    with pytest.raises(ValueError, match="POSITIVE_EXPECTED_TIME_REQUIRED"):
        quote_delivery(
            policy(), facts, customer_delivery_fee=Money(3_500), expected_direct_cost=Money(0)
        )


def test_longest_available_eligible_rider_is_selected_with_receipt() -> None:
    result = FairDispatchPolicy("fifo-v1").select(
        (
            DispatchCandidate("rider-new", True, 200, 100),
            DispatchCandidate("rider-old", True, 100, 500),
            DispatchCandidate("rider-unsafe", True, 50, 50, safety_eligible=False),
        ),
        quote(),
    )

    assert result.selected_rider_id == "rider-old"
    assert result.candidate_ids == ("rider-new", "rider-old", "rider-unsafe")
    assert result.explanation_codes == (
        "ELIGIBLE",
        "SAFE_TO_OFFER",
        "LONGEST_AVAILABLE_FIRST",
    )


@pytest.mark.parametrize(
    "feature",
    (
        "declined_offer_count",
        "accepted_offer_rate",
        "past_safety_stop",
        "insurance_claim_history",
        "nationality",
        "ai_worker_rank",
        "unknown_score",
    ),
)
def test_abusive_discriminatory_or_ai_ranking_feature_fails_closed(feature: str) -> None:
    with pytest.raises(ValueError, match="PROHIBITED_OR_UNKNOWN"):
        FairDispatchPolicy("fifo-v1").select(
            (DispatchCandidate("rider", True, 100, 100),),
            quote(),
            requested_features=(feature,),
        )


def test_decline_history_cannot_change_fair_selection() -> None:
    candidates = (
        DispatchCandidate("rider-a", True, 100, 1_000),
        DispatchCandidate("rider-b", True, 200, 100),
    )
    dispatch = FairDispatchPolicy("fifo-v1")

    before = dispatch.select(candidates, quote())
    after = dispatch.select(candidates, quote())

    assert before == after
    assert "declined_offer_count" not in before.used_features


def test_duplicate_candidate_and_no_safe_candidate_fail_closed() -> None:
    dispatch = FairDispatchPolicy("fifo-v1")
    with pytest.raises(ValueError, match="DUPLICATE"):
        dispatch.select(
            (
                DispatchCandidate("same", True, 1, 1),
                DispatchCandidate("same", True, 2, 2),
            ),
            quote(),
        )
    with pytest.raises(ValueError, match="NO_ELIGIBLE"):
        dispatch.select(
            (DispatchCandidate("unsafe", True, 1, 1, safety_eligible=False),), quote()
        )


def test_settlement_ledger_is_balanced_append_only_and_idempotent() -> None:
    ledger = Ledger()
    transaction = settlement_from_quote(
        transaction_id="settle-order-1", order_id="order-1", quote=quote()
    )
    ledger.record(transaction)

    assert sum(entry.debit.won for entry in transaction.entries) == sum(
        entry.credit.won for entry in transaction.entries
    )
    assert ledger.transactions() == (transaction,)
    with pytest.raises(ValueError, match="DUPLICATE_LEDGER_TRANSACTION"):
        ledger.record(transaction)


def test_merchant_order_profit_is_explicit_and_can_reveal_a_loss() -> None:
    economics = MerchantOrderEconomics(
        order_revenue=Money(25_000),
        food_and_labor_cost=Money(16_000),
        packaging_cost=Money(700),
        payment_cost=Money(500),
        platform_cost=Money(300),
        delivery_share=quote().merchant_delivery_share,
        discount_share=Money(1_000),
        refund_cost=Money(0),
    )
    loss = MerchantOrderEconomics(
        order_revenue=Money(10_000),
        food_and_labor_cost=Money(11_000),
        packaging_cost=Money(0),
        payment_cost=Money(0),
        platform_cost=Money(0),
        delivery_share=Money(0),
        discount_share=Money(0),
        refund_cost=Money(0),
    )

    assert economics.contribution_profit == Money(4_750)
    assert loss.contribution_profit == Money(-1_000)
