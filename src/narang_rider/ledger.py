from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .money import Money
from .pricing import PublicQuote


class LedgerAccount(StrEnum):
    CUSTOMER_RECEIVABLE = "CUSTOMER_RECEIVABLE"
    MERCHANT_RECEIVABLE = "MERCHANT_RECEIVABLE"
    RIDER_PAYABLE = "RIDER_PAYABLE"
    PLATFORM_REVENUE = "PLATFORM_REVENUE"
    PLATFORM_CANCELLATION_EXPENSE = "PLATFORM_CANCELLATION_EXPENSE"
    SAFETY_FUND_PAYABLE = "SAFETY_FUND_PAYABLE"
    REGIONAL_FUND_PAYABLE = "REGIONAL_FUND_PAYABLE"


@dataclass(frozen=True)
class LedgerEntry:
    account: LedgerAccount
    debit: Money
    credit: Money

    def __post_init__(self) -> None:
        if min(self.debit.won, self.credit.won) < 0:
            raise ValueError("NEGATIVE_LEDGER_AMOUNT")
        if (self.debit.won == 0) == (self.credit.won == 0):
            raise ValueError("EXACTLY_ONE_LEDGER_SIDE_REQUIRED")


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    order_id: str
    quote_policy_id: str
    entries: tuple[LedgerEntry, ...]

    def __post_init__(self) -> None:
        if not self.transaction_id.strip() or not self.order_id.strip() or not self.entries:
            raise ValueError("LEDGER_TRANSACTION_IDENTITY_REQUIRED")
        debits = sum(entry.debit.won for entry in self.entries)
        credits = sum(entry.credit.won for entry in self.entries)
        if debits != credits:
            raise ValueError("UNBALANCED_LEDGER_TRANSACTION")


class Ledger:
    """Append-only in-memory boundary; persistence adapters must enforce the same IDs."""

    def __init__(self) -> None:
        self._transactions: dict[str, LedgerTransaction] = {}

    def record(self, transaction: LedgerTransaction) -> None:
        if transaction.transaction_id in self._transactions:
            raise ValueError("DUPLICATE_LEDGER_TRANSACTION")
        self._transactions[transaction.transaction_id] = transaction

    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return tuple(self._transactions.values())


def settlement_from_quote(
    *, transaction_id: str, order_id: str, quote: PublicQuote
) -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id=transaction_id,
        order_id=order_id,
        quote_policy_id=quote.policy_id,
        entries=(
            LedgerEntry(LedgerAccount.CUSTOMER_RECEIVABLE, quote.customer_delivery_fee, Money(0)),
            LedgerEntry(LedgerAccount.MERCHANT_RECEIVABLE, quote.merchant_delivery_share, Money(0)),
            LedgerEntry(LedgerAccount.RIDER_PAYABLE, Money(0), quote.rider_pay),
            LedgerEntry(LedgerAccount.PLATFORM_REVENUE, Money(0), quote.platform_cost),
            LedgerEntry(LedgerAccount.SAFETY_FUND_PAYABLE, Money(0), quote.safety_fund),
            LedgerEntry(LedgerAccount.REGIONAL_FUND_PAYABLE, Money(0), quote.regional_fund),
        ),
    )
