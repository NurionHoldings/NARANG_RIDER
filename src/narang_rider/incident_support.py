"""Rider incident and insurance support with human, regulated decision boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

SAFE_REF = re.compile(r"^[A-Za-z0-9._:/-]{3,160}$")


class IncidentKind(StrEnum):
    EMERGENCY = "EMERGENCY"
    ACCIDENT = "ACCIDENT"
    INJURY = "INJURY"
    VEHICLE = "VEHICLE"
    PROPERTY = "PROPERTY"


class IncidentState(StrEnum):
    REPORTED = "REPORTED"
    TRIAGED = "TRIAGED"
    HUMAN_ASSIGNED = "HUMAN_ASSIGNED"
    SUBMITTED = "SUBMITTED"
    RESOLVED = "RESOLVED"
    APPEALED = "APPEALED"


class IncidentRole(StrEnum):
    RIDER = "RIDER"
    BRANCH_SAFETY = "BRANCH_SAFETY"
    INSURANCE_SPECIALIST = "INSURANCE_SPECIALIST"
    APPEAL_REVIEWER = "APPEAL_REVIEWER"


class ArkaonIncidentAction(StrEnum):
    SUMMARIZE = "SUMMARIZE"
    CHECK_MISSING_DOCUMENTS = "CHECK_MISSING_DOCUMENTS"
    DETERMINE_FAULT = "DETERMINE_FAULT"
    DENY_COVERAGE = "DENY_COVERAGE"
    PRICE_PREMIUM = "PRICE_PREMIUM"
    SUSPEND_ACCOUNT = "SUSPEND_ACCOUNT"
    CLAW_BACK_PAY = "CLAW_BACK_PAY"
    AFFECT_DISPATCH = "AFFECT_DISPATCH"


class IncidentErrorCode(StrEnum):
    FORBIDDEN = "FORBIDDEN"
    BRANCH_SCOPE = "BRANCH_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    RAW_SENSITIVE_DATA = "RAW_SENSITIVE_DATA"
    COVERAGE_FORGED = "COVERAGE_FORGED"
    DUPLICATE_INCIDENT = "DUPLICATE_INCIDENT"
    EVIDENCE_REUSED = "EVIDENCE_REUSED"
    ARKAON_AUTHORITY_DENIED = "ARKAON_AUTHORITY_DENIED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


class IncidentRejected(RuntimeError):
    def __init__(self, code: IncidentErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class IncidentPrincipal:
    actor_id: str
    branch_id: str
    role: IncidentRole


@dataclass(frozen=True)
class CoverageSnapshot:
    snapshot_id: str
    assignment_id: str
    rider_id: str
    branch_id: str
    provider_reference: str
    product_reference: str
    captured_at: datetime


@dataclass(frozen=True)
class IncidentReportCommand:
    assignment_id: str
    kind: IncidentKind
    idempotency_key: str
    coarse_zone: str
    narrative_vault_ref: str
    medical_vault_ref: str | None = None


@dataclass(frozen=True)
class IncidentCase:
    case_id: str
    branch_id: str
    rider_id: str
    assignment_id: str
    kind: IncidentKind
    state: IncidentState
    coarse_zone: str
    narrative_vault_ref: str
    medical_vault_ref: str | None
    coverage_snapshot_id: str
    assigned_human_id: str | None = None
    external_case_ref: str | None = None
    resolution_ref: str | None = None
    correction_ref: str | None = None
    appeal_reason_vault_ref: str | None = None
    version: int = 1


@dataclass(frozen=True)
class SafetyStopReceipt:
    rider_id: str
    dispatch_blocked: bool = True
    decline_penalty_allowed: bool = False
    retaliation_signal_allowed: bool = False


@dataclass(frozen=True)
class EarningsProtection:
    undisputed_earnings_payable: bool = True
    automatic_hold_allowed: bool = False
    automatic_clawback_allowed: bool = False


class SafetyStopProvider(Protocol):
    def stop(self, rider_id: str, branch_id: str, reason_ref: str) -> SafetyStopReceipt: ...


class HandoffProvider(Protocol):
    def handoff(self, channel: str, case: IncidentCase) -> str: ...


class EvidenceGrantProvider(Protocol):
    def issue(self, case_id: str, rider_id: str, purpose: str) -> str: ...


class IncidentService:
    """In-memory domain service; storage adapters persist identical immutable records."""

    def __init__(
        self,
        safety_stop: SafetyStopProvider,
        evidence_grants: EvidenceGrantProvider,
        handoff: HandoffProvider,
    ) -> None:
        self._safety_stop = safety_stop
        self._evidence_grants = evidence_grants
        self._handoff = handoff
        self._coverage: dict[str, CoverageSnapshot] = {}
        self._cases: dict[str, IncidentCase] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._incident_key: dict[tuple[str, str, IncidentKind], str] = {}
        self._used_grants: set[str] = set()
        self.audit: list[dict[str, str]] = []

    def capture_coverage(self, principal: IncidentPrincipal, snapshot: CoverageSnapshot) -> None:
        if principal.role not in {IncidentRole.BRANCH_SAFETY, IncidentRole.INSURANCE_SPECIALIST}:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        self._check_branch(principal, snapshot.branch_id)
        if snapshot.assignment_id in self._coverage or snapshot.captured_at.tzinfo is None:
            raise IncidentRejected(IncidentErrorCode.COVERAGE_FORGED)
        for value in (
            snapshot.snapshot_id,
            snapshot.assignment_id,
            snapshot.rider_id,
            snapshot.provider_reference,
            snapshot.product_reference,
        ):
            self._safe_ref(value)
        self._coverage[snapshot.assignment_id] = snapshot
        self._audit("COVERAGE_CAPTURED", snapshot.snapshot_id, principal, sensitive=False)

    def report(
        self, principal: IncidentPrincipal, command: IncidentReportCommand
    ) -> tuple[IncidentCase, SafetyStopReceipt, EarningsProtection]:
        if principal.role is not IncidentRole.RIDER:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        for value in (
            command.assignment_id,
            command.idempotency_key,
            command.coarse_zone,
            command.narrative_vault_ref,
        ):
            self._safe_ref(value)
        if command.medical_vault_ref is not None:
            self._vault_ref(command.medical_vault_ref)
        self._vault_ref(command.narrative_vault_ref)
        snapshot = self._coverage.get(command.assignment_id)
        if snapshot is None or snapshot.rider_id != principal.actor_id:
            raise IncidentRejected(IncidentErrorCode.COVERAGE_FORGED)
        self._check_branch(principal, snapshot.branch_id)
        payload = self._digest(command)
        idem_key = (principal.actor_id, command.idempotency_key)
        previous = self._idempotency.get(idem_key)
        if previous:
            if previous[0] != payload:
                raise IncidentRejected(IncidentErrorCode.DUPLICATE_INCIDENT)
            case = self._cases[previous[1]]
            return case, SafetyStopReceipt(principal.actor_id), EarningsProtection()
        natural = (principal.actor_id, command.assignment_id, command.kind)
        if natural in self._incident_key:
            raise IncidentRejected(IncidentErrorCode.DUPLICATE_INCIDENT)
        case_id = f"inc_{uuid.uuid4().hex}"
        case = IncidentCase(
            case_id,
            principal.branch_id,
            principal.actor_id,
            command.assignment_id,
            command.kind,
            IncidentState.REPORTED,
            command.coarse_zone,
            command.narrative_vault_ref,
            command.medical_vault_ref,
            snapshot.snapshot_id,
        )
        receipt = self._safety_stop.stop(principal.actor_id, principal.branch_id, case_id)
        if not receipt.dispatch_blocked or receipt.decline_penalty_allowed:
            raise IncidentRejected(IncidentErrorCode.HUMAN_DECISION_REQUIRED)
        self._cases[case_id] = case
        self._incident_key[natural] = case_id
        self._idempotency[idem_key] = (payload, case_id)
        self._audit("INCIDENT_REPORTED_SAFETY_STOP", case_id, principal, sensitive=False)
        return case, receipt, EarningsProtection()

    def get(self, principal: IncidentPrincipal, case_id: str) -> IncidentCase:
        case = self._cases.get(case_id)
        if case is None:
            raise IncidentRejected(IncidentErrorCode.NOT_FOUND)
        self._check_branch(principal, case.branch_id)
        if principal.role is IncidentRole.RIDER and principal.actor_id != case.rider_id:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        if case.medical_vault_ref and principal.role not in {
            IncidentRole.RIDER,
            IncidentRole.INSURANCE_SPECIALIST,
        }:
            case = replace(case, medical_vault_ref=None)
        self._audit("INCIDENT_VIEWED", case_id, principal, sensitive=bool(case.medical_vault_ref))
        return case

    def triage(self, principal: IncidentPrincipal, case_id: str) -> IncidentCase:
        if principal.role is not IncidentRole.BRANCH_SAFETY:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        case = self.get(principal, case_id)
        if case.state is not IncidentState.REPORTED:
            raise IncidentRejected(IncidentErrorCode.INVALID_STATE)
        return self._save(
            replace(case, state=IncidentState.TRIAGED, version=case.version + 1),
            principal,
            "TRIAGED",
        )

    def assign_human(
        self, principal: IncidentPrincipal, case_id: str, assignee_id: str
    ) -> IncidentCase:
        if principal.role is not IncidentRole.BRANCH_SAFETY:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        case = self.get(principal, case_id)
        if case.state is not IncidentState.TRIAGED:
            raise IncidentRejected(IncidentErrorCode.INVALID_STATE)
        self._safe_ref(assignee_id)
        return self._save(
            replace(
                case,
                state=IncidentState.HUMAN_ASSIGNED,
                assigned_human_id=assignee_id,
                version=case.version + 1,
            ),
            principal,
            "HUMAN_ASSIGNED",
        )

    def evidence_grant(self, principal: IncidentPrincipal, case_id: str, purpose: str) -> str:
        case = self.get(principal, case_id)
        if principal.role is not IncidentRole.RIDER or principal.actor_id != case.rider_id:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        if purpose not in {"INCIDENT_SCENE", "DAMAGE", "DOCUMENT"}:
            raise IncidentRejected(IncidentErrorCode.INVALID_REFERENCE)
        grant = self._evidence_grants.issue(case_id, principal.actor_id, purpose)
        if grant in self._used_grants:
            raise IncidentRejected(IncidentErrorCode.EVIDENCE_REUSED)
        self._used_grants.add(grant)
        return grant

    def submit(self, principal: IncidentPrincipal, case_id: str, channel: str) -> IncidentCase:
        if principal.role is not IncidentRole.INSURANCE_SPECIALIST:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        case = self.get(principal, case_id)
        if case.state is not IncidentState.HUMAN_ASSIGNED or not case.assigned_human_id:
            raise IncidentRejected(IncidentErrorCode.INVALID_STATE)
        if channel not in {"INSURER", "WORKERS_COMPENSATION", "EMERGENCY_CONTACT"}:
            raise IncidentRejected(IncidentErrorCode.INVALID_REFERENCE)
        external_ref = self._handoff.handoff(channel, case)
        self._safe_ref(external_ref)
        return self._save(
            replace(
                case,
                state=IncidentState.SUBMITTED,
                external_case_ref=external_ref,
                version=case.version + 1,
            ),
            principal,
            "EXTERNAL_HANDOFF",
        )

    def resolve(
        self, principal: IncidentPrincipal, case_id: str, resolution_ref: str
    ) -> IncidentCase:
        if principal.role is not IncidentRole.INSURANCE_SPECIALIST:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        case = self.get(principal, case_id)
        if case.state not in {IncidentState.SUBMITTED, IncidentState.APPEALED}:
            raise IncidentRejected(IncidentErrorCode.INVALID_STATE)
        self._safe_ref(resolution_ref)
        return self._save(
            replace(
                case,
                state=IncidentState.RESOLVED,
                resolution_ref=resolution_ref,
                version=case.version + 1,
            ),
            principal,
            "HUMAN_RESOLVED",
        )

    def appeal(
        self, principal: IncidentPrincipal, case_id: str, reason_vault_ref: str
    ) -> IncidentCase:
        case = self.get(principal, case_id)
        if principal.role is not IncidentRole.RIDER or principal.actor_id != case.rider_id:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        if case.state is not IncidentState.RESOLVED:
            raise IncidentRejected(IncidentErrorCode.INVALID_STATE)
        self._vault_ref(reason_vault_ref)
        return self._save(
            replace(
                case,
                state=IncidentState.APPEALED,
                appeal_reason_vault_ref=reason_vault_ref,
                version=case.version + 1,
            ),
            principal,
            "RIDER_APPEALED",
        )

    def correct(
        self, principal: IncidentPrincipal, case_id: str, correction_ref: str
    ) -> IncidentCase:
        if principal.role not in {IncidentRole.INSURANCE_SPECIALIST, IncidentRole.APPEAL_REVIEWER}:
            raise IncidentRejected(IncidentErrorCode.FORBIDDEN)
        case = self.get(principal, case_id)
        self._safe_ref(correction_ref)
        return self._save(
            replace(case, correction_ref=correction_ref, version=case.version + 1),
            principal,
            "CASE_CORRECTED",
        )

    def arkaon(
        self, principal: IncidentPrincipal, case_id: str, action: ArkaonIncidentAction
    ) -> dict[str, object]:
        case = self.get(principal, case_id)
        if action not in {
            ArkaonIncidentAction.SUMMARIZE,
            ArkaonIncidentAction.CHECK_MISSING_DOCUMENTS,
        }:
            raise IncidentRejected(IncidentErrorCode.ARKAON_AUTHORITY_DENIED)
        return {
            "case_id": case.case_id,
            "advisory_only": True,
            "action": action.value,
            "human_review_required": True,
        }

    def _save(self, case: IncidentCase, principal: IncidentPrincipal, action: str) -> IncidentCase:
        self._cases[case.case_id] = case
        self._audit(action, case.case_id, principal, sensitive=False)
        return case

    def _check_branch(self, principal: IncidentPrincipal, branch_id: str) -> None:
        if principal.branch_id != branch_id:
            raise IncidentRejected(IncidentErrorCode.BRANCH_SCOPE)

    @staticmethod
    def _safe_ref(value: str) -> None:
        if not isinstance(value, str) or not SAFE_REF.fullmatch(value):
            raise IncidentRejected(IncidentErrorCode.INVALID_REFERENCE)

    @classmethod
    def _vault_ref(cls, value: str) -> None:
        if not isinstance(value, str) or not value.startswith("vault://"):
            raise IncidentRejected(IncidentErrorCode.RAW_SENSITIVE_DATA)
        cls._safe_ref(value)

    @staticmethod
    def _digest(command: IncidentReportCommand) -> str:
        data = {
            "assignment_id": command.assignment_id,
            "kind": command.kind.value,
            "coarse_zone": command.coarse_zone,
            "narrative_vault_ref": command.narrative_vault_ref,
            "medical_vault_ref": command.medical_vault_ref,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def _audit(
        self, action: str, subject: str, principal: IncidentPrincipal, *, sensitive: bool
    ) -> None:
        self.audit.append(
            {
                "action": action,
                "subject": subject,
                "actor": principal.actor_id,
                "branch": principal.branch_id,
                "sensitive_access": str(sensitive).lower(),
                "at": datetime.now(UTC).isoformat(),
            }
        )
