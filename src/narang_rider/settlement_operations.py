"""Branch-scoped settlement statements and payout instruction operations.

This module deliberately creates provider instructions, never money transfers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SettlementErrorCode(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"
    INVALID_DEDUCTION = "INVALID_DEDUCTION"
    LEGAL_FLOOR_VIOLATION = "LEGAL_FLOOR_VIOLATION"
    STALE_STATEMENT = "STALE_STATEMENT"
    DUAL_CONTROL_REQUIRED = "DUAL_CONTROL_REQUIRED"
    DUTY_SEPARATION_REQUIRED = "DUTY_SEPARATION_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    AI_AUTHORITY_FORBIDDEN = "AI_AUTHORITY_FORBIDDEN"


class SettlementRejected(RuntimeError):
    def __init__(self, code: SettlementErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class StatementOwner(StrEnum):
    RIDER = "RIDER"
    MERCHANT = "MERCHANT"


class DeductionCode(StrEnum):
    WITHHOLDING_TAX = "WITHHOLDING_TAX"
    COURT_ORDER = "COURT_ORDER"
    VOLUNTARY_INSURANCE = "VOLUNTARY_INSURANCE"


class InstructionStatus(StrEnum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY_FOR_PROVIDER = "READY_FOR_PROVIDER"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True)
class SettlementPeriod:
    period_id: str
    branch_id: str
    starts_at: datetime
    cutoff_at: datetime

    def __post_init__(self) -> None:
        if not self.period_id or not self.branch_id or self.starts_at.tzinfo is None:
            raise ValueError("VALID_PERIOD_REQUIRED")
        if self.cutoff_at.tzinfo is None or self.cutoff_at <= self.starts_at:
            raise ValueError("VALID_CUTOFF_REQUIRED")


@dataclass(frozen=True)
class StatementLine:
    line_id: str
    order_id: str
    ledger_transaction_id: str
    quote_id: str
    gross_won: int
    deduction_won: int = 0
    deduction_code: DeductionCode | None = None
    cancellation_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.line_id, self.order_id, self.ledger_transaction_id, self.quote_id)):
            raise ValueError("LINE_PROVENANCE_REQUIRED")
        if self.gross_won < 0 or self.deduction_won < 0:
            raise ValueError("NEGATIVE_AMOUNT_FORBIDDEN")
        if (self.deduction_won > 0) != (self.deduction_code is not None):
            raise SettlementRejected(SettlementErrorCode.INVALID_DEDUCTION)


@dataclass(frozen=True)
class SettlementStatement:
    statement_id: str
    version: int
    period_id: str
    branch_id: str
    owner_type: StatementOwner
    owner_id: str
    lines: tuple[StatementLine, ...]
    gross_won: int
    deductions_won: int
    net_won: int
    dispute_deadline: datetime
    destination_vault_ref: str
    content_hash: str


@dataclass(frozen=True)
class Dispute:
    dispute_id: str
    statement_id: str
    line_id: str
    disputed_won: int
    opened_by: str
    opened_at: datetime


@dataclass(frozen=True)
class PayoutInstructionRecord:
    instruction_id: str
    statement_id: str
    statement_version: int
    branch_id: str
    amount_won: int
    destination_vault_ref: str
    requested_by: str
    created_at: datetime
    approvals: tuple[str, ...] = ()
    executor_id: str | None = None
    status: InstructionStatus = InstructionStatus.AWAITING_APPROVAL
    provider_reference: str | None = None


@dataclass(frozen=True)
class ProviderPayoutEvent:
    event_id: str
    instruction_id: str
    sequence: int
    amount_won: int
    destination_vault_ref: str
    provider_reference: str
    occurred_at: datetime
    signature: str


class ProviderCallbackVerifier(Protocol):
    def verify(self, event: ProviderPayoutEvent) -> bool: ...


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    subject_id: str
    actor_id: str


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    branch_id: str
    event_type: str
    subject_id: str


SETTLEMENT_ROUTE_MANIFEST = (
    ("GET", "/api/v1/settlements/statements/{statement_id}", "SettlementStatementV1"),
    ("POST", "/api/v1/settlements/statements/{statement_id}/disputes", "DisputeV1"),
    ("POST", "/api/v1/settlements/statements/{statement_id}/payout-instructions", "PayoutInstructionV1"),
    ("POST", "/api/v1/payout-instructions/{instruction_id}/approvals", "PayoutApprovalV1"),
    ("GET", "/api/v1/payout-instructions/{instruction_id}", "PayoutInstructionStatusV1"),
)


class SettlementOperations:
    def __init__(self, *, callback_verifier: ProviderCallbackVerifier, legal_floor_won: int = 0) -> None:
        if legal_floor_won < 0:
            raise ValueError("VALID_LEGAL_FLOOR_REQUIRED")
        self._verifier = callback_verifier
        self._floor = legal_floor_won
        self._statements: dict[str, SettlementStatement] = {}
        self._disputes: dict[str, Dispute] = {}
        self._instructions: dict[str, PayoutInstructionRecord] = {}
        self._events: dict[str, str] = {}
        self._last_sequence: dict[str, int] = {}
        self.audit: list[AuditEvent] = []
        self.outbox: list[OutboxEvent] = []

    def close_statement(
        self, *, period: SettlementPeriod, statement_id: str, owner_type: StatementOwner,
        owner_id: str, lines: tuple[StatementLine, ...], dispute_deadline: datetime,
        destination_vault_ref: str, now: datetime,
    ) -> SettlementStatement:
        if now < period.cutoff_at or dispute_deadline <= now:
            raise ValueError("PERIOD_CUTOFF_OR_DISPUTE_WINDOW_INVALID")
        if not destination_vault_ref.startswith("vault:") or not lines:
            raise ValueError("VAULT_DESTINATION_AND_LINES_REQUIRED")
        gross = sum(line.gross_won for line in lines)
        deductions = sum(line.deduction_won for line in lines)
        net = gross - deductions
        if net < self._floor or net < 0:
            raise SettlementRejected(SettlementErrorCode.LEGAL_FLOOR_VIOLATION)
        canonical = json.dumps(
            [[line.line_id, line.order_id, line.ledger_transaction_id, line.quote_id,
              line.cancellation_id, line.gross_won, line.deduction_won,
              line.deduction_code.value if line.deduction_code else None] for line in lines],
            separators=(",", ":"), sort_keys=True,
        )
        statement = SettlementStatement(
            statement_id, 1, period.period_id, period.branch_id, owner_type, owner_id, lines,
            gross, deductions, net, dispute_deadline, destination_vault_ref,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
        if statement_id in self._statements:
            if self._statements[statement_id] != statement:
                raise SettlementRejected(SettlementErrorCode.IDEMPOTENCY_CONFLICT)
            return self._statements[statement_id]
        self._statements[statement_id] = statement
        self._emit(period.branch_id, "STATEMENT_CLOSED", statement_id, "system")
        return statement

    def get_statement(self, *, statement_id: str, branch_id: str, owner_id: str) -> SettlementStatement:
        statement = self._statements.get(statement_id)
        if statement is None or statement.branch_id != branch_id or statement.owner_id != owner_id:
            raise SettlementRejected(SettlementErrorCode.ACCESS_DENIED)
        return statement

    def dispute(self, *, dispute_id: str, statement_id: str, branch_id: str, owner_id: str,
                line_id: str, disputed_won: int, now: datetime) -> Dispute:
        statement = self.get_statement(statement_id=statement_id, branch_id=branch_id, owner_id=owner_id)
        if now > statement.dispute_deadline or disputed_won <= 0:
            raise ValueError("INVALID_DISPUTE")
        line = next((item for item in statement.lines if item.line_id == line_id), None)
        if line is None or disputed_won > line.gross_won - line.deduction_won:
            raise ValueError("DISPUTE_EXCEEDS_LINE")
        dispute = Dispute(dispute_id, statement_id, line_id, disputed_won, owner_id, now)
        existing = self._disputes.get(dispute_id)
        if existing and existing != dispute:
            raise SettlementRejected(SettlementErrorCode.IDEMPOTENCY_CONFLICT)
        if not existing:
            self._disputes[dispute_id] = dispute
            self._emit(branch_id, "DISPUTE_OPENED", dispute_id, owner_id)
        return existing or dispute

    def request_instruction(self, *, instruction_id: str, statement_id: str, statement_version: int,
                            branch_id: str, requested_by: str, now: datetime) -> PayoutInstructionRecord:
        statement = self._statements.get(statement_id)
        if statement is None or statement.branch_id != branch_id:
            raise SettlementRejected(SettlementErrorCode.ACCESS_DENIED)
        if statement.version != statement_version:
            raise SettlementRejected(SettlementErrorCode.STALE_STATEMENT)
        held = sum(d.disputed_won for d in self._disputes.values() if d.statement_id == statement_id)
        payable = statement.net_won - held
        if payable < 0:
            raise SettlementRejected(SettlementErrorCode.LEGAL_FLOOR_VIOLATION)
        record = PayoutInstructionRecord(
            instruction_id, statement_id, statement_version, branch_id, payable,
            statement.destination_vault_ref, requested_by, now,
        )
        existing = self._instructions.get(instruction_id)
        if existing:
            if existing != record:
                raise SettlementRejected(SettlementErrorCode.IDEMPOTENCY_CONFLICT)
            return existing
        self._instructions[instruction_id] = record
        self._emit(branch_id, "PAYOUT_INSTRUCTION_REQUESTED", instruction_id, requested_by)
        return record

    def approve(self, *, instruction_id: str, branch_id: str, approver_id: str) -> PayoutInstructionRecord:
        current = self._instruction(instruction_id, branch_id)
        if approver_id == current.requested_by:
            raise SettlementRejected(SettlementErrorCode.DUAL_CONTROL_REQUIRED)
        if approver_id in current.approvals:
            return current
        approvals = current.approvals + (approver_id,)
        updated = replace(current, approvals=approvals, status=(
            InstructionStatus.READY_FOR_PROVIDER if len(approvals) >= 2
            else InstructionStatus.AWAITING_APPROVAL
        ))
        self._instructions[instruction_id] = updated
        self._emit(branch_id, "PAYOUT_INSTRUCTION_APPROVED", instruction_id, approver_id)
        return updated

    def execute_instruction(self, *, instruction_id: str, branch_id: str, executor_id: str) -> PayoutInstructionRecord:
        current = self._instruction(instruction_id, branch_id)
        if current.status is not InstructionStatus.READY_FOR_PROVIDER:
            raise SettlementRejected(SettlementErrorCode.DUAL_CONTROL_REQUIRED)
        if executor_id == current.requested_by or executor_id in current.approvals:
            raise SettlementRejected(SettlementErrorCode.DUTY_SEPARATION_REQUIRED)
        updated = replace(current, executor_id=executor_id, status=InstructionStatus.SUBMITTED)
        self._instructions[instruction_id] = updated
        self._emit(branch_id, "PAYOUT_INSTRUCTION_SUBMIT_READY", instruction_id, executor_id)
        return updated

    def reconcile(self, *, event: ProviderPayoutEvent, branch_id: str) -> PayoutInstructionRecord:
        current = self._instruction(event.instruction_id, branch_id)
        fingerprint = hashlib.sha256(repr(event).encode()).hexdigest()
        if event.event_id in self._events:
            if self._events[event.event_id] != fingerprint:
                return self._review(current, "PROVIDER_REPLAY_CONFLICT")
            return current
        self._events[event.event_id] = fingerprint
        last = self._last_sequence.get(event.instruction_id, 0)
        if (not self._verifier.verify(event) or event.sequence != last + 1
                or event.amount_won != current.amount_won
                or event.destination_vault_ref != current.destination_vault_ref):
            return self._review(current, "PROVIDER_CALLBACK_MISMATCH")
        self._last_sequence[event.instruction_id] = event.sequence
        updated = replace(current, status=InstructionStatus.CONFIRMED,
                          provider_reference=event.provider_reference)
        self._instructions[event.instruction_id] = updated
        self._emit(branch_id, "PAYOUT_CONFIRMED", event.instruction_id, "provider")
        return updated

    def arkaon_action(self, *, action: str) -> None:
        if action in {"REDUCE", "HOLD", "DENY", "CLAWBACK"}:
            raise SettlementRejected(SettlementErrorCode.AI_AUTHORITY_FORBIDDEN)

    def _instruction(self, instruction_id: str, branch_id: str) -> PayoutInstructionRecord:
        value = self._instructions.get(instruction_id)
        if value is None or value.branch_id != branch_id:
            raise SettlementRejected(SettlementErrorCode.ACCESS_DENIED)
        return value

    def _review(self, current: PayoutInstructionRecord, reason: str) -> PayoutInstructionRecord:
        updated = replace(current, status=InstructionStatus.HUMAN_REVIEW)
        self._instructions[current.instruction_id] = updated
        self._emit(current.branch_id, reason, current.instruction_id, "provider")
        return updated

    def _emit(self, branch_id: str, event_type: str, subject_id: str, actor_id: str) -> None:
        sequence = len(self.audit) + 1
        self.audit.append(AuditEvent(sequence, event_type, subject_id, actor_id))
        self.outbox.append(OutboxEvent(f"settlement-{sequence}", branch_id, event_type, subject_id))
