from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narang_rider.persistence import (
    InMemoryPersistence,
    LedgerIntegrityViolation,
    RecordKind,
    canonical_payload_digest,
)
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


class CallbackVerifier:
    def verify(self, event: ProviderPayoutEvent) -> bool:
        return event.signature == "valid"


def settlement() -> SettlementOperations:
    operations = SettlementOperations(callback_verifier=CallbackVerifier())
    operations.close_statement(
        period=SettlementPeriod("period", "branch", NOW - timedelta(days=1), NOW),
        statement_id="statement",
        owner_type=StatementOwner.RIDER,
        owner_id="rider",
        lines=(StatementLine(
            "line", "order", "ledger", "quote", 10_000, 1_000,
            DeductionCode.WITHHOLDING_TAX,
        ),),
        dispute_deadline=NOW + timedelta(days=1),
        destination_vault_ref="vault://bank/destination",
        now=NOW,
    )
    operations.request_instruction(
        instruction_id="instruction", statement_id="statement", statement_version=1,
        branch_id="branch", requested_by="requester", now=NOW,
    )
    return operations


def callback(*, event_id: str = "event", signature: str = "valid") -> ProviderPayoutEvent:
    return ProviderPayoutEvent(
        event_id, "instruction", 1, 9_000, "vault://bank/destination",
        "provider-reference", NOW, signature,
    )


def test_callback_cannot_confirm_before_dual_control_and_execution() -> None:
    operations = settlement()
    with pytest.raises(SettlementRejected) as rejected:
        operations.reconcile(event=callback(), branch_id="branch")
    assert rejected.value.code is SettlementErrorCode.DUTY_SEPARATION_REQUIRED


def test_invalid_callback_cannot_poison_later_valid_event_id() -> None:
    operations = settlement()
    operations.approve(instruction_id="instruction", branch_id="branch", approver_id="a1")
    operations.approve(instruction_id="instruction", branch_id="branch", approver_id="a2")
    operations.execute_instruction(
        instruction_id="instruction", branch_id="branch", executor_id="executor"
    )
    assert operations.reconcile(
        event=callback(signature="invalid"), branch_id="branch"
    ).status is InstructionStatus.HUMAN_REVIEW

    # A fresh service models the retry after human review has reset the instruction.
    retry = settlement()
    retry.approve(instruction_id="instruction", branch_id="branch", approver_id="a1")
    retry.approve(instruction_id="instruction", branch_id="branch", approver_id="a2")
    retry.execute_instruction(
        instruction_id="instruction", branch_id="branch", executor_id="executor"
    )
    assert retry.reconcile(event=callback(), branch_id="branch").status is InstructionStatus.CONFIRMED


def test_cumulative_disputes_cannot_exceed_one_line() -> None:
    operations = settlement()
    operations.dispute(
        dispute_id="d1", statement_id="statement", branch_id="branch", owner_id="rider",
        line_id="line", disputed_won=8_000, now=NOW,
    )
    with pytest.raises(ValueError, match="DISPUTE_EXCEEDS_LINE"):
        operations.dispute(
            dispute_id="d2", statement_id="statement", branch_id="branch",
            owner_id="rider", line_id="line", disputed_won=2_000, now=NOW,
        )


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"account_code": "EXPENSE", "amount_won": 1}],
        [
            {"account_code": "EXPENSE", "amount_won": 100},
            {"account_code": "PAYABLE", "amount_won": -99},
        ],
        [
            {"account_code": "EXPENSE", "amount_won": True},
            {"account_code": "PAYABLE", "amount_won": -1},
        ],
    ],
)
def test_adapter_rejects_unbalanced_or_malformed_ledger(entries: list[dict]) -> None:
    store = InMemoryPersistence()
    unit = store.begin(
        branch_id="branch", idempotency_key="key",
        payload_digest=canonical_payload_digest({"operation": "ledger"}),
    )
    with pytest.raises(LedgerIntegrityViolation):
        unit.put(
            RecordKind.LEDGER_TRANSACTION,
            "transaction",
            {"branch_id": "branch", "entries": entries},
            expected_version=0,
        )


def test_postgres_contract_is_append_only_balanced_and_dlq_safe() -> None:
    schema = Path("migrations/0001_postgres_persistence.sql").read_text()
    adapter = Path("src/narang_rider/postgres.py").read_text()
    assert "CREATE CONSTRAINT TRIGGER balanced_ledger_transaction" in schema
    assert schema.count("reject_append_only_mutation") >= 5
    assert "dead_lettered_at IS NULL" in adapter
    assert "NOT EXISTS (" in adapter and "prior.delivered_at IS NULL" in adapter
