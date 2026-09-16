"""Fail-closed regional pilot readiness, consent and containment controls."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum


class PilotStage(StrEnum):
    SYNTHETIC = "synthetic"
    INTERNAL_SHADOW = "internal_shadow"
    CLOSED_SANDBOX = "closed_sandbox"
    LIMITED_BRANCH_PILOT = "limited_branch_pilot"


class ApprovalRole(StrEnum):
    ETHERNIAN_REVIEWER = "ethernian_reviewer"
    OPERATOR = "operator"
    EVIDENCE_OWNER = "evidence_owner"
    RESTART_REVIEWER = "restart_reviewer"


class ReadinessItem(StrEnum):
    BRANCH_READINESS = "branch_readiness"
    SIGNED_POLICIES = "signed_policies"
    TRAINED_HUMAN_REVIEWERS = "trained_human_reviewers"
    SUPPORT_ON_CALL = "support_on_call"
    INSURANCE_WORKERS_COMP = "insurance_workers_comp"
    PRIVACY_IMPACT_RETENTION = "privacy_impact_retention"
    SETTLEMENT_ESCROW_FINOPS = "settlement_escrow_finops"
    PARTNER_SANDBOX_CERTIFICATION = "partner_sandbox_certification"
    MAP_NOTIFICATION_PROVIDER_SANDBOX = "map_notification_provider_sandbox"
    BACKUP_RESTORE = "backup_restore"
    CAPACITY_REPORT = "capacity_report"
    ACCESSIBILITY_SECURITY = "accessibility_security"
    INCIDENT_TABLETOP = "incident_tabletop"
    ROLLBACK = "rollback"


class StopCriterion(StrEnum):
    SAFETY_EVENT = "safety_event"
    PAY_FLOOR_BREACH = "pay_floor_breach"
    LEDGER_MISMATCH = "ledger_mismatch"
    PRIVACY_INCIDENT = "privacy_incident"
    AUTH_TENANT_ESCAPE = "auth_tenant_escape"
    DELIVERY_FAILURE_THRESHOLD = "delivery_failure_threshold"


class PilotErrorCode(StrEnum):
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_FORGED = "APPROVAL_FORGED"
    APPROVAL_MISSING = "APPROVAL_MISSING"
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    GATE_NOT_READY = "GATE_NOT_READY"
    REAL_DATA_FORBIDDEN = "REAL_DATA_FORBIDDEN"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    STAGE_BYPASS_FORBIDDEN = "STAGE_BYPASS_FORBIDDEN"
    UNSAFE_RESTART_FORBIDDEN = "UNSAFE_RESTART_FORBIDDEN"


class PilotRejected(RuntimeError):
    def __init__(self, code: PilotErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OpaqueApproval:
    reference: str
    item: str
    branch_id: str
    approver_id_hash: str
    role: ApprovalRole
    issued_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        if not self.reference.startswith("approval://"):
            raise ValueError("OPAQUE_APPROVAL_REFERENCE_REQUIRED")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("TIMEZONE_REQUIRED")
        if self.expires_at <= self.issued_at:
            raise ValueError("APPROVAL_EXPIRY_REQUIRED")


def _approval_payload(approval: OpaqueApproval) -> bytes:
    fields = (
        approval.reference,
        approval.item,
        approval.branch_id,
        approval.approver_id_hash,
        approval.role.value,
        approval.issued_at.isoformat(),
        approval.expires_at.isoformat(),
    )
    return "|".join(fields).encode()


class ApprovalAuthority:
    """Testable signature boundary; production keys belong in a managed signer."""

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("APPROVAL_SIGNING_KEY_TOO_SHORT")
        self._key = signing_key

    def issue(
        self,
        *,
        reference: str,
        item: str,
        branch_id: str,
        approver_id: str,
        role: ApprovalRole,
        issued_at: datetime,
        expires_at: datetime,
    ) -> OpaqueApproval:
        unsigned = OpaqueApproval(
            reference,
            item,
            branch_id,
            hashlib.sha256(approver_id.encode()).hexdigest(),
            role,
            issued_at,
            expires_at,
            "",
        )
        signature = hmac.new(self._key, _approval_payload(unsigned), hashlib.sha256).hexdigest()
        return replace(unsigned, signature=signature)

    def verify(self, approval: OpaqueApproval, *, now: datetime, branch_id: str) -> None:
        if approval.branch_id != branch_id:
            raise PilotRejected(PilotErrorCode.BRANCH_SCOPE_MISMATCH, "approval branch mismatch")
        if now > approval.expires_at:
            raise PilotRejected(PilotErrorCode.APPROVAL_EXPIRED, "approval expired")
        expected = hmac.new(self._key, _approval_payload(approval), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, approval.signature):
            raise PilotRejected(PilotErrorCode.APPROVAL_FORGED, "approval signature invalid")


@dataclass(frozen=True)
class PilotReadinessSubmission:
    branch_id: str
    submitted_by: str
    target_stage: PilotStage
    evidence: tuple[OpaqueApproval, ...]
    ethernian_review: OpaqueApproval
    operator_approval: OpaqueApproval
    contains_real_data: bool = False


@dataclass(frozen=True)
class ChecklistResult:
    item: ReadinessItem
    passed: bool
    reference: str | None
    reason: str


@dataclass(frozen=True)
class PilotReadinessReport:
    schema_version: str
    branch_id: str
    target_stage: PilotStage
    evaluated_at: datetime
    checklist: tuple[ChecklistResult, ...]
    independent_review_passed: bool
    operator_approval_passed: bool
    ready: bool
    legal_approval: bool
    counsel_decisions_required: tuple[str, ...]
    report_digest: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, sort_keys=True, separators=(",", ":"))


class PilotReadinessEvaluator:
    def __init__(self, authority: ApprovalAuthority) -> None:
        self._authority = authority

    def evaluate(
        self, submission: PilotReadinessSubmission, *, now: datetime
    ) -> PilotReadinessReport:
        if (
            submission.contains_real_data
            and submission.target_stage is not PilotStage.LIMITED_BRANCH_PILOT
        ):
            raise PilotRejected(
                PilotErrorCode.REAL_DATA_FORBIDDEN,
                "real data is forbidden before an approved pilot",
            )
        actor_hash = hashlib.sha256(submission.submitted_by.encode()).hexdigest()
        approvals: dict[str, OpaqueApproval] = {}
        invalid: dict[str, str] = {}
        for approval in submission.evidence:
            try:
                self._authority.verify(approval, now=now, branch_id=submission.branch_id)
                if approval.approver_id_hash == actor_hash:
                    raise PilotRejected(
                        PilotErrorCode.SELF_APPROVAL_FORBIDDEN, "submitter cannot approve evidence"
                    )
                approvals[approval.item] = approval
            except PilotRejected as error:
                invalid[approval.item] = error.code.value
        checklist = tuple(
            ChecklistResult(
                item,
                item.value in approvals,
                approvals[item.value].reference if item.value in approvals else None,
                "verified" if item.value in approvals else invalid.get(item.value, "missing"),
            )
            for item in ReadinessItem
        )
        review_ok = self._verify_gate_approval(
            submission.ethernian_review,
            ApprovalRole.ETHERNIAN_REVIEWER,
            submission,
            actor_hash,
            now,
        )
        operator_ok = self._verify_gate_approval(
            submission.operator_approval,
            ApprovalRole.OPERATOR,
            submission,
            actor_hash,
            now,
        )
        if (
            submission.ethernian_review.approver_id_hash
            == submission.operator_approval.approver_id_hash
        ):
            raise PilotRejected(
                PilotErrorCode.SELF_APPROVAL_FORBIDDEN,
                "independent reviewer and operator must be different people",
            )
        ready = all(item.passed for item in checklist) and review_ok and operator_ok
        unsigned = {
            "branch_id": submission.branch_id,
            "target_stage": submission.target_stage.value,
            "evaluated_at": now.isoformat(),
            "checklist": [asdict(item) for item in checklist],
            "ready": ready,
        }
        digest = hashlib.sha256(
            json.dumps(unsigned, default=str, sort_keys=True).encode()
        ).hexdigest()
        return PilotReadinessReport(
            "narang.pilot-readiness.v1",
            submission.branch_id,
            submission.target_stage,
            now,
            checklist,
            review_ok,
            operator_ok,
            ready,
            False,
            (
                "employment_and_worker_classification",
                "insurance_and_workers_comp_scope",
                "location_service_registration_and_consent",
                "privacy_impact_retention_and_overseas_transfer",
                "escrow_payment_and_settlement_regulatory_scope",
                "consumer_refund_terms_and_evidence_use",
            ),
            digest,
        )

    def require_ready(
        self, submission: PilotReadinessSubmission, *, now: datetime
    ) -> PilotReadinessReport:
        report = self.evaluate(submission, now=now)
        if not report.ready:
            raise PilotRejected(PilotErrorCode.GATE_NOT_READY, "pilot readiness gate failed")
        return report

    def _verify_gate_approval(
        self,
        approval: OpaqueApproval,
        role: ApprovalRole,
        submission: PilotReadinessSubmission,
        actor_hash: str,
        now: datetime,
    ) -> bool:
        self._authority.verify(approval, now=now, branch_id=submission.branch_id)
        if approval.item != f"pilot:{submission.target_stage.value}" or approval.role is not role:
            raise PilotRejected(PilotErrorCode.APPROVAL_FORGED, "approval scope or role mismatch")
        if approval.approver_id_hash == actor_hash:
            raise PilotRejected(PilotErrorCode.SELF_APPROVAL_FORBIDDEN, "self approval forbidden")
        return True


@dataclass(frozen=True)
class PilotState:
    branch_id: str
    stage: PilotStage
    contained: bool = False
    stop_reasons: frozenset[StopCriterion] = frozenset()


class PilotLifecycle:
    _ORDER = tuple(PilotStage)

    def advance(self, state: PilotState, report: PilotReadinessReport) -> PilotState:
        if state.contained:
            raise PilotRejected(
                PilotErrorCode.UNSAFE_RESTART_FORBIDDEN, "contained pilot cannot advance"
            )
        if report.branch_id != state.branch_id or not report.ready:
            raise PilotRejected(PilotErrorCode.GATE_NOT_READY, "matching ready report required")
        index = self._ORDER.index(state.stage)
        if index + 1 >= len(self._ORDER) or report.target_stage is not self._ORDER[index + 1]:
            raise PilotRejected(
                PilotErrorCode.STAGE_BYPASS_FORBIDDEN, "stages must advance one at a time"
            )
        return replace(state, stage=report.target_stage)

    def contain(self, state: PilotState, criterion: StopCriterion) -> PilotState:
        return replace(state, contained=True, stop_reasons=state.stop_reasons | {criterion})

    def restart(
        self,
        state: PilotState,
        *,
        resolved: frozenset[StopCriterion],
        reviewer: OpaqueApproval,
        operator: OpaqueApproval,
        authority: ApprovalAuthority,
        now: datetime,
    ) -> PilotState:
        if not state.contained or resolved != state.stop_reasons:
            raise PilotRejected(
                PilotErrorCode.UNSAFE_RESTART_FORBIDDEN, "all stop causes must be resolved"
            )
        for approval, role in (
            (reviewer, ApprovalRole.RESTART_REVIEWER),
            (operator, ApprovalRole.OPERATOR),
        ):
            authority.verify(approval, now=now, branch_id=state.branch_id)
            if approval.item != f"restart:{state.stage.value}" or approval.role is not role:
                raise PilotRejected(
                    PilotErrorCode.UNSAFE_RESTART_FORBIDDEN, "restart approval invalid"
                )
        if reviewer.approver_id_hash == operator.approver_id_hash:
            raise PilotRejected(PilotErrorCode.SELF_APPROVAL_FORBIDDEN, "restart needs two people")
        return replace(state, contained=False, stop_reasons=frozenset())


@dataclass(frozen=True)
class ParticipantEnrollment:
    participant_id_hash: str
    branch_id: str
    consented_at: datetime
    consent_reference: str
    active: bool = True
    withdrawn_at: datetime | None = None
    retaliation_allowed: bool = False


class ParticipantConsentRegistry:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ParticipantEnrollment] = {}

    def enroll(
        self,
        participant_id: str,
        branch_id: str,
        *,
        consent: bool,
        consent_reference: str,
        now: datetime,
    ) -> ParticipantEnrollment:
        if not consent or not consent_reference.startswith("consent://"):
            raise PilotRejected(PilotErrorCode.CONSENT_REQUIRED, "explicit consent required")
        identity = hashlib.sha256(participant_id.encode()).hexdigest()
        record = ParticipantEnrollment(identity, branch_id, now, consent_reference)
        self._records[(branch_id, identity)] = record
        return record

    def withdraw(
        self, participant_id: str, branch_id: str, *, now: datetime
    ) -> ParticipantEnrollment:
        identity = hashlib.sha256(participant_id.encode()).hexdigest()
        record = self._records[(branch_id, identity)]
        withdrawn = replace(record, active=False, withdrawn_at=now, retaliation_allowed=False)
        self._records[(branch_id, identity)] = withdrawn
        return withdrawn


@dataclass(frozen=True)
class PilotAdminRoute:
    method: str
    path: str
    request_dto: str | None
    response_dto: str


PILOT_ADMIN_ROUTES = (
    PilotAdminRoute(
        "POST",
        "/api/v1/admin/branches/{branch_id}/pilot/readiness",
        "PilotReadinessV1",
        "PilotReadinessReportV1",
    ),
    PilotAdminRoute(
        "GET", "/api/v1/admin/branches/{branch_id}/pilot/checklist", None, "PilotChecklistV1"
    ),
    PilotAdminRoute(
        "POST", "/api/v1/admin/branches/{branch_id}/pilot/contain", "PilotContainV1", "PilotStateV1"
    ),
    PilotAdminRoute(
        "POST", "/api/v1/admin/branches/{branch_id}/pilot/restart", "PilotRestartV1", "PilotStateV1"
    ),
)
