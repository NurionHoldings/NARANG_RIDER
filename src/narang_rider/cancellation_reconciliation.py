from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from .ledger import Ledger, LedgerAccount, LedgerEntry, LedgerTransaction
from .money import ZERO, Money


class ReconciliationDisposition(StrEnum):
    MATCHED = "MATCHED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True)
class CancellationFeeAllocation:
    customer_won: int
    merchant_won: int
    platform_won: int
    rider_compensation_won: int
    platform_fee_won: int = 0

    def __post_init__(self) -> None:
        values = (
            self.customer_won,
            self.merchant_won,
            self.platform_won,
            self.rider_compensation_won,
            self.platform_fee_won,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("CANCELLATION_AMOUNT_MUST_BE_INTEGER_WON")
        if min(values) < 0:
            raise ValueError("NEGATIVE_CANCELLATION_AMOUNT")
        if self.customer_won + self.merchant_won + self.platform_won != self.total_won:
            raise ValueError("UNBALANCED_CANCELLATION_ALLOCATION")

    @property
    def total_won(self) -> int:
        return self.rider_compensation_won + self.platform_fee_won


@dataclass(frozen=True)
class PartnerFeeReport:
    event_id: str
    partner_id: str
    branch_id: str
    order_id: str
    sequence: int
    reported_total_won: int

    def __post_init__(self) -> None:
        identities = (self.event_id, self.partner_id, self.branch_id, self.order_id)
        if any(not value.strip() for value in identities):
            raise ValueError("PARTNER_FEE_REPORT_IDENTITY_REQUIRED")
        if self.sequence < 1:
            raise ValueError("INVALID_PARTNER_FEE_SEQUENCE")
        if self.reported_total_won < 0:
            raise ValueError("NEGATIVE_PARTNER_REPORTED_AMOUNT")


@dataclass(frozen=True)
class CancellationAuditReceipt:
    transaction_id: str
    event_id: str
    partner_id: str
    branch_id: str
    order_id: str
    sequence: int
    canonical_total_won: int
    reported_total_won: int
    rider_compensation_won: int
    disposition: ReconciliationDisposition
    automatic_rider_clawback_allowed: bool
    receipt_hash: str


class CancellationFeeReconciliationService:
    """Record canonical pay and make partner mismatches review-only signals."""

    def __init__(self, *, ledger: Ledger, branch_id: str, max_partner_charge_won: int) -> None:
        if not branch_id.strip():
            raise ValueError("BRANCH_ID_REQUIRED")
        if max_partner_charge_won < 0:
            raise ValueError("INVALID_MAX_PARTNER_CHARGE")
        self._ledger = ledger
        self._branch_id = branch_id
        self._max_partner_charge_won = max_partner_charge_won
        self._receipts: dict[str, CancellationAuditReceipt] = {}
        self._reports: dict[str, PartnerFeeReport] = {}
        self._last_sequence: dict[tuple[str, str], int] = {}

    def reconcile(
        self,
        *,
        report: PartnerFeeReport,
        allocation: CancellationFeeAllocation,
        transaction_id: str,
        policy_id: str,
    ) -> CancellationAuditReceipt:
        if report.branch_id != self._branch_id:
            raise PermissionError("BRANCH_SCOPE_MISMATCH")
        if report.reported_total_won > self._max_partner_charge_won:
            raise ValueError("PARTNER_CHARGE_LIMIT_EXCEEDED")

        existing_report = self._reports.get(report.event_id)
        if existing_report is not None:
            if existing_report != report:
                raise ValueError("CONFLICTING_PARTNER_EVENT_REPLAY")
            return self._receipts[report.event_id]

        stream = (report.partner_id, report.order_id)
        if report.sequence <= self._last_sequence.get(stream, 0):
            raise ValueError("OUT_OF_ORDER_PARTNER_FEE_REPORT")

        disposition = (
            ReconciliationDisposition.MATCHED
            if report.reported_total_won == allocation.total_won
            else ReconciliationDisposition.HUMAN_REVIEW
        )
        self._ledger.record(
            _cancellation_transaction(
                transaction_id=transaction_id,
                branch_id=report.branch_id,
                order_id=report.order_id,
                policy_id=policy_id,
                allocation=allocation,
            )
        )

        receipt = CancellationAuditReceipt(
            transaction_id=transaction_id,
            event_id=report.event_id,
            partner_id=report.partner_id,
            branch_id=report.branch_id,
            order_id=report.order_id,
            sequence=report.sequence,
            canonical_total_won=allocation.total_won,
            reported_total_won=report.reported_total_won,
            rider_compensation_won=allocation.rider_compensation_won,
            disposition=disposition,
            automatic_rider_clawback_allowed=False,
            receipt_hash=_receipt_hash(report, allocation, transaction_id, disposition),
        )
        self._reports[report.event_id] = report
        self._receipts[report.event_id] = receipt
        self._last_sequence[stream] = report.sequence
        return receipt

    def receipts(self) -> tuple[CancellationAuditReceipt, ...]:
        return tuple(self._receipts.values())


def _cancellation_transaction(
    *,
    transaction_id: str,
    branch_id: str,
    order_id: str,
    policy_id: str,
    allocation: CancellationFeeAllocation,
) -> LedgerTransaction:
    entries: list[LedgerEntry] = []
    payer_accounts = (
        (LedgerAccount.CUSTOMER_RECEIVABLE, allocation.customer_won),
        (LedgerAccount.MERCHANT_RECEIVABLE, allocation.merchant_won),
        (LedgerAccount.PLATFORM_CANCELLATION_EXPENSE, allocation.platform_won),
    )
    entries.extend(
        LedgerEntry(account, Money(amount), ZERO)
        for account, amount in payer_accounts
        if amount > 0
    )
    if allocation.rider_compensation_won:
        entries.append(
            LedgerEntry(LedgerAccount.RIDER_PAYABLE, ZERO, Money(allocation.rider_compensation_won))
        )
    if allocation.platform_fee_won:
        entries.append(
            LedgerEntry(LedgerAccount.PLATFORM_REVENUE, ZERO, Money(allocation.platform_fee_won))
        )
    if not entries:
        raise ValueError("ZERO_CANCELLATION_TRANSACTION")
    return LedgerTransaction(
        transaction_id=transaction_id,
        order_id=order_id,
        quote_policy_id=f"{policy_id}@{branch_id}",
        entries=tuple(entries),
    )


def _receipt_hash(
    report: PartnerFeeReport,
    allocation: CancellationFeeAllocation,
    transaction_id: str,
    disposition: ReconciliationDisposition,
) -> str:
    payload = "|".join(
        (
            transaction_id,
            report.event_id,
            report.partner_id,
            report.branch_id,
            report.order_id,
            str(report.sequence),
            str(allocation.total_won),
            str(report.reported_total_won),
            disposition.value,
            "NO_RIDER_AUTO_CLAWBACK",
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()
