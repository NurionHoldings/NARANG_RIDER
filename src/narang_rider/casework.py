"""Fair, branch-scoped support and dispute casework contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar


class CaseErrorCode(StrEnum):
    FORBIDDEN = "CASE_FORBIDDEN"
    NOT_FOUND = "CASE_NOT_FOUND"
    INVALID_TRANSITION = "CASE_INVALID_TRANSITION"
    VERSION_CONFLICT = "CASE_VERSION_CONFLICT"
    INVALID_REFERENCE = "CASE_INVALID_REFERENCE"
    CONFLICT_OF_INTEREST = "CASE_CONFLICT_OF_INTEREST"
    HUMAN_DECISION_REQUIRED = "CASE_HUMAN_DECISION_REQUIRED"
    DUPLICATE_ACTION = "CASE_DUPLICATE_ACTION"
    SLA_POLICY_VIOLATION = "CASE_SLA_POLICY_VIOLATION"
    RESOURCE_LIMIT = "CASE_RESOURCE_LIMIT"


class CaseRejected(ValueError):
    def __init__(self, code: CaseErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class CaseType(StrEnum):
    DELIVERY = "delivery"
    DAMAGE = "damage"
    REFUND = "refund"
    PAYMENT = "payment"
    SETTLEMENT = "settlement"
    DISPATCH = "dispatch"
    SAFETY = "safety"
    PRIVACY = "privacy"
    INSURANCE = "insurance"
    PARTNER = "partner"


class CaseSeverity(StrEnum):
    STANDARD = "standard"
    URGENT = "urgent"
    CRITICAL = "critical"


class CaseState(StrEnum):
    OPEN = "open"
    TRIAGE = "triage"
    ASSIGNED = "assigned"
    WAITING_PARTY = "waiting_party"
    REVIEW = "review"
    RESOLVED = "resolved"
    APPEALED = "appealed"
    CLOSED = "closed"


class ParticipantRole(StrEnum):
    CUSTOMER = "customer"
    MERCHANT = "merchant"
    RIDER = "rider"
    PARTNER = "partner"
    SUPPORT = "support"
    PRIVACY_OFFICER = "privacy_officer"


@dataclass(frozen=True)
class CasePrincipal:
    principal_id: str
    role: ParticipantRole
    branch_id: str
    representative_for_ref: str | None = None


@dataclass(frozen=True)
class SlaPolicy:
    standard_hours: int = 72
    urgent_hours: int = 24
    critical_hours: int = 4

    def deadline(self, severity: CaseSeverity, opened_at: datetime) -> datetime:
        hours = {
            CaseSeverity.STANDARD: self.standard_hours,
            CaseSeverity.URGENT: self.urgent_hours,
            CaseSeverity.CRITICAL: self.critical_hours,
        }[severity]
        if hours <= 0:
            raise CaseRejected(CaseErrorCode.SLA_POLICY_VIOLATION)
        return opened_at + timedelta(hours=hours)


@dataclass(frozen=True)
class CaseMessage:
    sequence: int
    author_id: str
    author_role: ParticipantRole
    body_ref: str
    created_at: datetime
    internal: bool = False

    def __post_init__(self) -> None:
        prefix = "vault://internal-note/" if self.internal else "vault://case-message/"
        if not self.body_ref.startswith(prefix):
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)


@dataclass(frozen=True)
class CaseEvidence:
    evidence_id: str
    submitted_by: str
    subject_ids: frozenset[str]
    sanitized_ref: str
    receipt_digest: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if not self.sanitized_ref.startswith("vault://sanitized-evidence/"):
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        if len(self.receipt_digest) < 16 or not self.subject_ids:
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)


@dataclass(frozen=True)
class CaseEvent:
    sequence: int
    kind: str
    actor_id: str
    at: datetime
    detail_ref: str | None = None


@dataclass
class SupportCase:
    case_id: str
    branch_id: str
    case_type: CaseType
    severity: CaseSeverity
    owner_id: str
    participant_ids: frozenset[str]
    resource_ref: str
    opened_at: datetime
    sla_due_at: datetime
    state: CaseState = CaseState.OPEN
    assigned_agent_id: str | None = None
    version: int = 1
    messages: list[CaseMessage] = field(default_factory=list)
    evidence: list[CaseEvidence] = field(default_factory=list)
    events: list[CaseEvent] = field(default_factory=list)
    human_rationale_ref: str | None = None
    rights_notice_ref: str | None = None
    resolver_id: str | None = None
    appeal_reviewer_id: str | None = None


@dataclass(frozen=True)
class ParticipantCaseView:
    case_id: str
    case_type: CaseType
    severity: CaseSeverity
    state: CaseState
    sla_due_at: datetime
    participant_aliases: tuple[str, ...]
    message_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    rights_notice_ref: str | None


@dataclass(frozen=True)
class AgentWorkload:
    agent_id: str
    branch_id: str
    active_cases: int
    specialist_types: frozenset[CaseType]
    unavailable: bool = False


@dataclass(frozen=True)
class ActionInstruction:
    instruction_id: str
    case_id: str
    kind: str
    amount_minor: int
    currency: str
    beneficiary_ref: str
    requires_dual_control: bool = True
    executed: bool = False


@dataclass(frozen=True)
class ArkaonCaseAdvice:
    summary_ref: str
    suggested_type: CaseType
    missing_items: tuple[str, ...]
    repeated_or_linked_priority: bool = False
    authority: str = "advisory_only"


class CaseworkService:
    MAX_PARTICIPANTS = 16
    MAX_MESSAGES_PER_CASE = 500
    MAX_EVIDENCE_PER_CASE = 100
    TRANSITIONS: ClassVar[dict[CaseState, set[CaseState]]] = {
        CaseState.OPEN: {CaseState.TRIAGE},
        CaseState.TRIAGE: {CaseState.ASSIGNED},
        CaseState.ASSIGNED: {CaseState.WAITING_PARTY, CaseState.REVIEW},
        CaseState.WAITING_PARTY: {CaseState.REVIEW, CaseState.ASSIGNED},
        CaseState.REVIEW: {CaseState.RESOLVED, CaseState.WAITING_PARTY},
        CaseState.RESOLVED: {CaseState.APPEALED, CaseState.CLOSED},
        CaseState.APPEALED: {CaseState.REVIEW, CaseState.CLOSED},
    }

    def __init__(self, sla_policy: SlaPolicy | None = None) -> None:
        self.sla_policy = sla_policy or SlaPolicy()
        self.cases: dict[str, SupportCase] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self.instructions: dict[str, ActionInstruction] = {}
        self.outbox: list[Mapping[str, str]] = []

    @staticmethod
    def _valid_resource_ref(value: str) -> bool:
        return value.startswith("resource://") and len(value) > 16

    def open_case(
        self,
        *,
        case_id: str,
        principal: CasePrincipal,
        case_type: CaseType,
        severity: CaseSeverity,
        participant_ids: Iterable[str],
        resource_ref: str,
        idempotency_key: str,
        now: datetime,
    ) -> SupportCase:
        participant_values: list[str] = []
        for participant_id in participant_ids:
            if len(participant_values) >= self.MAX_PARTICIPANTS:
                raise CaseRejected(CaseErrorCode.RESOURCE_LIMIT)
            participant_values.append(participant_id)
        participants = frozenset(participant_values)
        if not participants:
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        if principal.principal_id not in participants and (
            not principal.representative_for_ref
            or not principal.representative_for_ref.startswith("vault://representative/")
        ):
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        if not self._valid_resource_ref(resource_ref):
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        key = (principal.principal_id, idempotency_key)
        if key in self._idempotency:
            return self.cases[self._idempotency[key]]
        case = SupportCase(
            case_id=case_id,
            branch_id=principal.branch_id,
            case_type=case_type,
            severity=severity,
            owner_id=principal.principal_id,
            participant_ids=participants,
            resource_ref=resource_ref,
            opened_at=now,
            sla_due_at=self.sla_policy.deadline(severity, now),
        )
        case.events.append(CaseEvent(1, "case_opened", principal.principal_id, now))
        self.cases[case_id] = case
        self._idempotency[key] = case_id
        self._notify(case, "case_opened")
        return case

    def get(self, case_id: str, principal: CasePrincipal) -> SupportCase:
        case = self.cases.get(case_id)
        if case is None:
            raise CaseRejected(CaseErrorCode.NOT_FOUND)
        participant = principal.principal_id in case.participant_ids
        staff = principal.role in {ParticipantRole.SUPPORT, ParticipantRole.PRIVACY_OFFICER}
        if principal.branch_id != case.branch_id or not (participant or staff):
            raise CaseRejected(CaseErrorCode.NOT_FOUND)
        return case

    def participant_view(self, case_id: str, principal: CasePrincipal) -> ParticipantCaseView:
        case = self.get(case_id, principal)
        participant = principal.principal_id in case.participant_ids
        if not participant:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        aliases = tuple(
            "본인" if item == principal.principal_id else f"참여자-{index + 1}"
            for index, item in enumerate(sorted(case.participant_ids))
        )
        return ParticipantCaseView(
            case.case_id,
            case.case_type,
            case.severity,
            case.state,
            case.sla_due_at,
            aliases,
            tuple(m.body_ref for m in case.messages if not m.internal),
            tuple(e.sanitized_ref for e in case.evidence if principal.principal_id in e.subject_ids),
            case.rights_notice_ref,
        )

    def append_message(
        self,
        case_id: str,
        message: CaseMessage,
        *,
        principal: CasePrincipal,
        expected_version: int,
    ) -> SupportCase:
        case = self.get(case_id, principal)
        self._check_version(case, expected_version)
        if message.author_id != principal.principal_id or message.sequence != len(case.messages) + 1:
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        if len(case.messages) >= self.MAX_MESSAGES_PER_CASE:
            raise CaseRejected(CaseErrorCode.RESOURCE_LIMIT)
        if message.internal and principal.role not in {
            ParticipantRole.SUPPORT,
            ParticipantRole.PRIVACY_OFFICER,
        }:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        case.messages.append(message)
        self._bump(case, principal.principal_id, "message_appended", message.created_at)
        self._notify(case, "case_message")
        return case

    def append_evidence(
        self,
        case_id: str,
        evidence: CaseEvidence,
        *,
        principal: CasePrincipal,
        expected_version: int,
    ) -> SupportCase:
        case = self.get(case_id, principal)
        self._check_version(case, expected_version)
        if evidence.submitted_by != principal.principal_id:
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        if len(case.evidence) >= self.MAX_EVIDENCE_PER_CASE:
            raise CaseRejected(CaseErrorCode.RESOURCE_LIMIT)
        if not evidence.subject_ids.issubset(case.participant_ids):
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        if any(item.evidence_id == evidence.evidence_id for item in case.evidence):
            raise CaseRejected(CaseErrorCode.DUPLICATE_ACTION)
        case.evidence.append(evidence)
        self._bump(case, principal.principal_id, "evidence_appended", evidence.captured_at)
        return case

    def assign(
        self,
        case_id: str,
        agents: Iterable[AgentWorkload],
        *,
        actor: CasePrincipal,
        expected_version: int,
        now: datetime,
    ) -> SupportCase:
        case = self.get(case_id, actor)
        if actor.role is not ParticipantRole.SUPPORT:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        self._check_version(case, expected_version)
        eligible = [
            item
            for item in agents
            if item.branch_id == case.branch_id
            and not item.unavailable
            and item.agent_id not in case.participant_ids
            and (not item.specialist_types or case.case_type in item.specialist_types)
        ]
        if not eligible:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        chosen = min(eligible, key=lambda item: (item.active_cases, item.agent_id))
        case.assigned_agent_id = chosen.agent_id
        if case.state is CaseState.OPEN:
            self._transition(case, CaseState.TRIAGE, actor.principal_id, now)
        self._transition(case, CaseState.ASSIGNED, actor.principal_id, now)
        return case

    def transition(
        self,
        case_id: str,
        target: CaseState,
        *,
        actor: CasePrincipal,
        expected_version: int,
        now: datetime,
        rationale_ref: str | None = None,
        rights_notice_ref: str | None = None,
        arkaon_initiated: bool = False,
    ) -> SupportCase:
        case = self.get(case_id, actor)
        self._check_version(case, expected_version)
        if actor.role is not ParticipantRole.SUPPORT:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        if case.assigned_agent_id and actor.principal_id != case.assigned_agent_id:
            raise CaseRejected(CaseErrorCode.CONFLICT_OF_INTEREST)
        if arkaon_initiated and target in {CaseState.RESOLVED, CaseState.CLOSED}:
            raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
        if target in {CaseState.RESOLVED, CaseState.CLOSED}:
            if not (rationale_ref or "").startswith("vault://human-rationale/"):
                raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
            if not (rights_notice_ref or "").startswith("notice://rights/"):
                raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
            case.human_rationale_ref = rationale_ref
            case.rights_notice_ref = rights_notice_ref
            case.resolver_id = actor.principal_id
        self._transition(case, target, actor.principal_id, now)
        self._notify(case, f"case_{target.value}")
        return case

    def appeal(
        self,
        case_id: str,
        *,
        principal: CasePrincipal,
        appeal_ref: str,
        expected_version: int,
        now: datetime,
    ) -> SupportCase:
        case = self.get(case_id, principal)
        self._check_version(case, expected_version)
        if principal.principal_id not in case.participant_ids or not appeal_ref.startswith(
            "vault://appeal/"
        ):
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        self._transition(case, CaseState.APPEALED, principal.principal_id, now, appeal_ref)
        self._notify(case, "case_appealed")
        return case

    def assign_appeal_reviewer(
        self, case_id: str, reviewer: AgentWorkload, *, actor: CasePrincipal, now: datetime
    ) -> SupportCase:
        case = self.get(case_id, actor)
        if case.state is not CaseState.APPEALED or reviewer.agent_id in {
            case.assigned_agent_id,
            case.resolver_id,
            *case.participant_ids,
        }:
            raise CaseRejected(CaseErrorCode.CONFLICT_OF_INTEREST)
        if reviewer.branch_id != case.branch_id or reviewer.unavailable:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        case.appeal_reviewer_id = reviewer.agent_id
        self._transition(case, CaseState.REVIEW, actor.principal_id, now)
        return case

    def issue_action_instruction(
        self,
        case_id: str,
        *,
        actor: CasePrincipal,
        kind: str,
        amount_minor: int,
        currency: str,
        beneficiary_ref: str,
        idempotency_key: str,
        fault_final: bool = False,
    ) -> ActionInstruction:
        self.get(case_id, actor)
        if actor.role is not ParticipantRole.SUPPORT or amount_minor < 0:
            raise CaseRejected(CaseErrorCode.FORBIDDEN)
        if kind == "rider_pay_clawback" and not fault_final:
            raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
        instruction_id = hashlib.sha256(f"{case_id}:{idempotency_key}".encode()).hexdigest()
        if instruction_id in self.instructions:
            return self.instructions[instruction_id]
        if kind not in {"provisional_credit", "refund_review", "payment_adjustment", "rider_pay_clawback"}:
            raise CaseRejected(CaseErrorCode.INVALID_REFERENCE)
        instruction = ActionInstruction(
            instruction_id,
            case_id,
            kind,
            amount_minor,
            currency,
            beneficiary_ref,
        )
        self.instructions[instruction_id] = instruction
        self.outbox.append({"kind": "dual_control_financial_instruction", "id": instruction_id})
        return instruction

    @staticmethod
    def review_evidence(
        before: CaseEvidence, after: CaseEvidence, *, human_reviewer_id: str
    ) -> Mapping[str, str]:
        if not human_reviewer_id or before.evidence_id == after.evidence_id:
            raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
        return {
            "disposition": "human_review_required",
            "before_ref": before.sanitized_ref,
            "after_ref": after.sanitized_ref,
            "photo_or_ai_is_not_determinative": "true",
        }

    @staticmethod
    def accept_arkaon_advice(advice: ArkaonCaseAdvice) -> ArkaonCaseAdvice:
        if advice.authority != "advisory_only":
            raise CaseRejected(CaseErrorCode.HUMAN_DECISION_REQUIRED)
        return advice

    @staticmethod
    def _check_version(case: SupportCase, expected: int) -> None:
        if case.version != expected:
            raise CaseRejected(CaseErrorCode.VERSION_CONFLICT)

    def _transition(
        self,
        case: SupportCase,
        target: CaseState,
        actor_id: str,
        now: datetime,
        detail_ref: str | None = None,
    ) -> None:
        if target not in self.TRANSITIONS.get(case.state, set()):
            raise CaseRejected(CaseErrorCode.INVALID_TRANSITION)
        case.state = target
        self._bump(case, actor_id, f"case_{target.value}", now, detail_ref)

    @staticmethod
    def _bump(
        case: SupportCase,
        actor_id: str,
        kind: str,
        now: datetime,
        detail_ref: str | None = None,
    ) -> None:
        case.version += 1
        case.events.append(CaseEvent(len(case.events) + 1, kind, actor_id, now, detail_ref))

    def _notify(self, case: SupportCase, event: str) -> None:
        self.outbox.append({"kind": "case_notification", "case_id": case.case_id, "event": event})


CASEWORK_ROUTE_MANIFEST = {
    "POST /api/v1/support/cases": "authenticated participant or verified representative; idempotent",
    "GET /api/v1/support/cases": "owner inbox; branch scoped",
    "GET /api/v1/support/cases/{case_id}": "masked participant view or scoped support",
    "POST /api/v1/support/cases/{case_id}/messages": "append-only; optimistic version",
    "POST /api/v1/support/cases/{case_id}/evidence": "sanitized receipt refs only",
    "POST /api/v1/support/cases/{case_id}/appeals": "participant; independent reviewer",
    "POST /api/v1/support-ops/cases/{case_id}/assign": "branch support; fair workload",
    "POST /api/v1/support-ops/cases/{case_id}/transition": "human rationale and rights notice",
    "POST /api/v1/support-ops/cases/{case_id}/actions": "instruction only; dual control",
}
