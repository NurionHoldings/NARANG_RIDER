from datetime import UTC, datetime

import pytest

from narang_rider.ledger import LedgerAccount, LedgerEntry, LedgerTransaction
from narang_rider.money import Money
from narang_rider.pricing import DeliveryFacts, PricingPolicy


def test_money_rejects_float_and_boolean() -> None:
    with pytest.raises(TypeError, match="INTEGER_WON"):
        Money(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="INTEGER_WON"):
        Money(True)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ("distance_m", "expected_wait_minutes"))
def test_delivery_facts_reject_negative_values(field: str) -> None:
    values = {
        "distance_m": 1,
        "expected_active_minutes": 1,
        "expected_wait_minutes": 0,
        "expected_return_distance_m": 0,
    }
    values[field] = -1
    with pytest.raises(ValueError, match="OUT_OF_RANGE"):
        DeliveryFacts(**values)


def test_pricing_policy_requires_timezone_and_positive_units() -> None:
    with pytest.raises(ValueError, match="VERSIONED_POLICY_REQUIRED"):
        PricingPolicy(
            "v1",
            datetime(2026, 9, 16),  # noqa: DTZ001
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
        )
    with pytest.raises(ValueError, match="PRICING_UNIT_MUST_BE_POSITIVE"):
        PricingPolicy(
            "v1",
            datetime(2026, 9, 16, tzinfo=UTC),
            1,
            0,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
            1,
        )


def test_unbalanced_or_ambiguous_ledger_entry_is_rejected() -> None:
    with pytest.raises(ValueError, match="EXACTLY_ONE"):
        LedgerEntry(LedgerAccount.PLATFORM_REVENUE, Money(10), Money(10))
    with pytest.raises(ValueError, match="UNBALANCED"):
        LedgerTransaction(
            "tx",
            "order",
            "policy",
            (LedgerEntry(LedgerAccount.CUSTOMER_RECEIVABLE, Money(10), Money(0)),),
        )
