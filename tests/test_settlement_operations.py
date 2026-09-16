from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.settlement_operations import (
    DeductionCode,
    InstructionStatus,
    ProviderPayoutEvent,
    SettlementErrorCode,
    SettlementOperations,
    SettlementPeriod,
    SettlementRejected,
    StatementLine,
    StatementOwner,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


class Verifier:
    def verify(self, event: ProviderPayoutEvent) -> bool:
        return event.signature == "valid-signature"


def service() -> SettlementOperations:
    return SettlementOperations(callback_verifier=Verifier())


def period() -> SettlementPeriod:
    return SettlementPeriod("2026-09-a", "sejong", NOW - timedelta(days=8), NOW)


def line(*, gross: int = 10_000, deduction: int = 1_000) -> StatementLine:
    return StatementLine(
        "line-1", "order-1", "ledger-1", "quote-1", gross, deduction,
        DeductionCode.WITHHOLDING_TAX if deduction else None, "cancel-1",
    )


def statement(ops: SettlementOperations, *, lines: tuple[StatementLine, ...] | None = None):
    return ops.close_statement(
        period=period(), statement_id="statement-1", owner_type=StatementOwner.RIDER,
        owner_id="rider-1", lines=lines or (line(),),
        dispute_deadline=NOW + timedelta(days=7), destination_vault_ref="vault:bank:1",
        now=NOW,
    )


def instruction(ops: SettlementOperations):
    statement(ops)
    return ops.request_instruction(
        instruction_id="payout-1", statement_id="statement-1", statement_version=1,
        branch_id="sejong", requested_by="requester", now=NOW,
    )


def test_statement_is_immutable_balanced_and_has_full_provenance() -> None:
    ops = service()
    value = statement(ops)
    assert (value.gross_won, value.deductions_won, value.net_won) == (10_000, 1_000, 9_000)
    assert value.lines[0].cancellation_id == "cancel-1"
    assert len(value.content_hash) == 64
    assert len(ops.audit) == len(ops.outbox) == 1
    assert statement(ops) == value


def test_forged_deduction_and_negative_net_are_rejected() -> None:
    with pytest.raises(SettlementRejected) as forged:
        StatementLine("l", "o", "tx", "q", 100, 1, None)
    assert forged.value.code is SettlementErrorCode.INVALID_DEDUCTION
    ops = service()
    with pytest.raises(SettlementRejected) as negative:
        statement(ops, lines=(line(gross=100, deduction=200),))
    assert negative.value.code is SettlementErrorCode.LEGAL_FLOOR_VIOLATION


def test_owner_and_branch_idor_are_denied() -> None:
    ops = service()
    statement(ops)
    for branch_id, owner_id in (("daejeon", "rider-1"), ("sejong", "rider-2")):
        with pytest.raises(SettlementRejected) as denied:
            ops.get_statement(statement_id="statement-1", branch_id=branch_id, owner_id=owner_id)
        assert denied.value.code is SettlementErrorCode.ACCESS_DENIED


def test_dispute_holds_only_disputed_amount_not_full_statement() -> None:
    ops = service()
    statement(ops)
    ops.dispute(
        dispute_id="d-1", statement_id="statement-1", branch_id="sejong",
        owner_id="rider-1", line_id="line-1", disputed_won=2_000, now=NOW,
    )
    payout = ops.request_instruction(
        instruction_id="payout-1", statement_id="statement-1", statement_version=1,
        branch_id="sejong", requested_by="requester", now=NOW,
    )
    assert payout.amount_won == 7_000


def test_stale_duplicate_and_dual_control_abuse_are_blocked() -> None:
    ops = service()
    statement(ops)
    with pytest.raises(SettlementRejected) as stale:
        ops.request_instruction(
            instruction_id="x", statement_id="statement-1", statement_version=0,
            branch_id="sejong", requested_by="requester", now=NOW,
        )
    assert stale.value.code is SettlementErrorCode.STALE_STATEMENT
    value = instruction(ops)
    assert ops.request_instruction(
        instruction_id="payout-1", statement_id="statement-1", statement_version=1,
        branch_id="sejong", requested_by="requester", now=NOW,
    ) == value
    with pytest.raises(SettlementRejected) as self_approve:
        ops.approve(instruction_id="payout-1", branch_id="sejong", approver_id="requester")
    assert self_approve.value.code is SettlementErrorCode.DUAL_CONTROL_REQUIRED
    ops.approve(instruction_id="payout-1", branch_id="sejong", approver_id="approver-1")
    ready = ops.approve(instruction_id="payout-1", branch_id="sejong", approver_id="approver-2")
    assert ready.status is InstructionStatus.READY_FOR_PROVIDER
    with pytest.raises(SettlementRejected) as separated:
        ops.execute_instruction(
            instruction_id="payout-1", branch_id="sejong", executor_id="approver-1"
        )
    assert separated.value.code is SettlementErrorCode.DUTY_SEPARATION_REQUIRED


def test_callback_success_replay_and_mismatch_go_to_human_review_without_clawback() -> None:
    ops = service()
    instruction(ops)
    ops.approve(instruction_id="payout-1", branch_id="sejong", approver_id="a1")
    ops.approve(instruction_id="payout-1", branch_id="sejong", approver_id="a2")
    ops.execute_instruction(instruction_id="payout-1", branch_id="sejong", executor_id="exec")
    event = ProviderPayoutEvent(
        "event-1", "payout-1", 1, 9_000, "vault:bank:1", "provider-1", NOW,
        "valid-signature",
    )
    assert ops.reconcile(event=event, branch_id="sejong").status is InstructionStatus.CONFIRMED
    assert ops.reconcile(event=event, branch_id="sejong").status is InstructionStatus.CONFIRMED
    mismatch = ProviderPayoutEvent(
        "event-2", "payout-1", 2, 8_999, "vault:bank:attacker", "provider-2", NOW,
        "valid-signature",
    )
    reviewed = ops.reconcile(event=mismatch, branch_id="sejong")
    assert reviewed.status is InstructionStatus.HUMAN_REVIEW
    assert reviewed.amount_won == 9_000


def test_arkaon_cannot_reduce_hold_deny_or_claw_back() -> None:
    ops = service()
    for action in ("REDUCE", "HOLD", "DENY", "CLAWBACK"):
        with pytest.raises(SettlementRejected) as forbidden:
            ops.arkaon_action(action=action)
        assert forbidden.value.code is SettlementErrorCode.AI_AUTHORITY_FORBIDDEN
    assert ops.arkaon_action(action="EXPLAIN") is None
