import pytest

from narang_rider.cancellation_reconciliation import (
    CancellationFeeAllocation,
    CancellationFeeReconciliationService,
    PartnerFeeReport,
    ReconciliationDisposition,
)
from narang_rider.ledger import Ledger, LedgerAccount


def allocation() -> CancellationFeeAllocation:
    return CancellationFeeAllocation(
        customer_won=1000,
        merchant_won=500,
        platform_won=300,
        rider_compensation_won=1500,
        platform_fee_won=300,
    )


def report(*, event_id: str = "evt-1", sequence: int = 1, amount: int = 1800):
    return PartnerFeeReport(
        event_id=event_id,
        partner_id="ai-baebi",
        branch_id="sejong-1",
        order_id="order-1",
        sequence=sequence,
        reported_total_won=amount,
    )


def service(ledger: Ledger | None = None) -> CancellationFeeReconciliationService:
    return CancellationFeeReconciliationService(
        ledger=ledger or Ledger(), branch_id="sejong-1", max_partner_charge_won=10_000
    )


def reconcile(target: CancellationFeeReconciliationService, partner_report=None):
    return target.reconcile(
        report=partner_report or report(),
        allocation=allocation(),
        transaction_id="cancel:order-1",
        policy_id="cancel-v1",
    )


def test_balanced_cancellation_is_appended_with_audit_receipt():
    ledger = Ledger()
    receipt = reconcile(service(ledger))

    transaction = ledger.transactions()[0]
    assert sum(entry.debit.won for entry in transaction.entries) == 1800
    assert sum(entry.credit.won for entry in transaction.entries) == 1800
    assert transaction.quote_policy_id == "cancel-v1@sejong-1"
    assert any(
        entry.account is LedgerAccount.RIDER_PAYABLE and entry.credit.won == 1500
        for entry in transaction.entries
    )
    assert receipt.disposition is ReconciliationDisposition.MATCHED
    assert len(receipt.receipt_hash) == 64
    assert receipt.automatic_rider_clawback_allowed is False


def test_exact_replay_is_idempotent_without_duplicate_ledger_entry():
    ledger = Ledger()
    target = service(ledger)
    first = reconcile(target)
    second = reconcile(target)

    assert second == first
    assert len(ledger.transactions()) == 1


def test_amount_mismatch_routes_to_human_review_without_rider_clawback():
    receipt = reconcile(service(), report(amount=1700))

    assert receipt.disposition is ReconciliationDisposition.HUMAN_REVIEW
    assert receipt.rider_compensation_won == 1500
    assert receipt.automatic_rider_clawback_allowed is False


def test_conflicting_replay_and_out_of_order_sequence_fail_closed():
    target = service()
    reconcile(target)
    with pytest.raises(ValueError, match="CONFLICTING_PARTNER_EVENT_REPLAY"):
        reconcile(target, report(amount=1700))
    with pytest.raises(ValueError, match="OUT_OF_ORDER_PARTNER_FEE_REPORT"):
        reconcile(target, report(event_id="evt-old", sequence=1))


def test_branch_scope_negative_and_excess_charges_are_rejected():
    target = service()
    wrong_branch = PartnerFeeReport("evt", "partner", "busan-1", "order", 1, 100)
    with pytest.raises(PermissionError, match="BRANCH_SCOPE_MISMATCH"):
        reconcile(target, wrong_branch)
    with pytest.raises(ValueError, match="NEGATIVE_PARTNER_REPORTED_AMOUNT"):
        report(amount=-1)
    with pytest.raises(ValueError, match="PARTNER_CHARGE_LIMIT_EXCEEDED"):
        reconcile(target, report(amount=10_001))


def test_unbalanced_or_negative_canonical_allocation_is_rejected():
    with pytest.raises(ValueError, match="UNBALANCED_CANCELLATION_ALLOCATION"):
        CancellationFeeAllocation(100, 0, 0, 50, 0)
    with pytest.raises(ValueError, match="NEGATIVE_CANCELLATION_AMOUNT"):
        CancellationFeeAllocation(-1, 0, 1, 0, 0)


def test_duplicate_transaction_id_cannot_pay_rider_twice():
    ledger = Ledger()
    first = service(ledger)
    second = service(ledger)
    reconcile(first)
    with pytest.raises(ValueError, match="DUPLICATE_LEDGER_TRANSACTION"):
        reconcile(second, report(event_id="evt-other"))
