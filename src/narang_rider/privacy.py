"""Privacy lifecycle and data-subject rights orchestration.

This module stores references and minimized metadata only.  Vault operations are
represented as outbox intents; it never reads or deletes production personal data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar


class PrivacyErrorCode(StrEnum):
    FORBIDDEN = "PRIVACY_FORBIDDEN"
    INVALID_IDENTITY_PROOF = "PRIVACY_IDENTITY_PROOF_INVALID"
    INVALID_TRANSITION = "PRIVACY_INVALID_TRANSITION"
    DUPLICATE = "PRIVACY_DUPLICATE_REQUEST"
    MIXED_SUBJECT = "PRIVACY_MIXED_SUBJECT_DATA"
    HOLD_INVALID = "PRIVACY_LEGAL_HOLD_INVALID"
    AI_AUTHORITY_FORBIDDEN = "PRIVACY_AI_AUTHORITY_FORBIDDEN"
    BULK_EXPORT_FORBIDDEN = "PRIVACY_BULK_EXPORT_FORBIDDEN"


class PrivacyRejected(ValueError):
    def __init__(self, code: PrivacyErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class DataCategory(StrEnum):
    ACCOUNT = "account"
    CONTACT = "contact"
    LOCATION = "location"
    EVIDENCE = "evidence"
    SUPPORT = "support"
    FINANCIAL = "financial"
    AUDIT = "audit"


@dataclass(frozen=True)
class DataPolicy:
    category: DataCategory
    purpose: str
    legal_basis_ref: str
    retention_days: int
    vault_required: bool = True
    deletion_mode: str = "vault_tombstone_and_metadata_anonymize"

    def __post_init__(self) -> None:
        if not self.purpose or not self.legal_basis_ref or self.retention_days <= 0:
            raise ValueError("invalid data policy")


class PolicyRegistry:
    def __init__(self, policies: Iterable[DataPolicy]) -> None:
        values = tuple(policies)
        self._policies = {item.category: item for item in values}
        if set(self._policies) != set(DataCategory):
            raise ValueError("all privacy categories require an explicit policy")

    def get(self, category: DataCategory) -> DataPolicy:
        return self._policies[category]


@dataclass(frozen=True)
class ConsentRecord:
    consent_id: str
    subject_id: str
    purpose: str
    notice_version: str
    granted_at: datetime
    withdrawn_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None

    def withdraw(self, *, at: datetime) -> ConsentRecord:
        if self.withdrawn_at is not None:
            return self
        return ConsentRecord(**{**self.__dict__, "withdrawn_at": at})


class RightType(StrEnum):
    ACCESS = "access"
    CORRECTION = "correction"
    DELETION = "deletion"
    RESTRICTION = "restriction"
    EXPORT = "export"
    OBJECTION = "objection"


class RequestState(StrEnum):
    RECEIVED = "received"
    IDENTITY_VERIFIED = "identity_verified"
    IN_REVIEW = "in_review"
    ACTION_PENDING = "action_pending"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    HUMAN_DENIED = "human_denied"


@dataclass(frozen=True)
class PrivacyPrincipal:
    principal_id: str
    subject_id: str | None
    role: str
    branch_id: str | None
    privacy_officer: bool = False


@dataclass
class RightsRequest:
    request_id: str
    subject_id: str
    branch_id: str
    right: RightType
    identity_proof_ref: str
    created_at: datetime
    due_at: datetime
    state: RequestState = RequestState.RECEIVED
    decision_reason_ref: str | None = None
    _idempotency_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class SubjectDataRef:
    record_id: str
    subject_id: str
    branch_id: str
    category: DataCategory
    vault_ref: str | None
    minimized: Mapping[str, str]
    collected_at: datetime

    def __post_init__(self) -> None:
        forbidden = {"name", "phone", "address", "latitude", "longitude", "email"}
        if forbidden.intersection(key.lower() for key in self.minimized):
            raise ValueError("raw PII is forbidden in application metadata")
        if self.category not in {DataCategory.FINANCIAL, DataCategory.AUDIT} and not self.vault_ref:
            raise ValueError("raw data must remain behind a vault reference")


@dataclass(frozen=True)
class ExportManifest:
    request_id: str
    subject_id: str
    record_refs: tuple[str, ...]
    minimized_records: tuple[Mapping[str, str], ...]
    digest: str


@dataclass(frozen=True)
class LegalHold:
    hold_id: str
    subject_id: str
    record_ids: frozenset[str]
    reason_ref: str
    expires_at: datetime
    approvers: tuple[str, str]

    def active(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass(frozen=True)
class LifecycleIntent:
    intent_id: str
    kind: str
    request_id: str
    record_id: str
    vault_ref: str | None


@dataclass(frozen=True)
class SweepCandidate:
    record_id: str
    branch_id: str
    expires_at: datetime
    blocked_by_hold: bool


class SweepPhase(StrEnum):
    DRY_RUN = "dry_run"
    REVIEWED = "reviewed"
    EXECUTED = "executed"


@dataclass
class RetentionSweep:
    sweep_id: str
    candidates: tuple[SweepCandidate, ...]
    phase: SweepPhase = SweepPhase.DRY_RUN
    reviewer_id: str | None = None
    executed_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VendorTransferApproval:
    vendor_id: str
    subprocessor_register_ref: str
    transfer_country: str | None
    transfer_assessment_ref: str | None
    approval_ref: str
    expires_at: datetime


@dataclass(frozen=True)
class BreachCase:
    case_id: str
    detected_at: datetime
    assessment_due_at: datetime
    evidence_refs: tuple[str, ...]
    status: str = "human_assessment_required"
    legal_conclusion: str | None = None


class PrivacyLifecycleService:
    """In-memory contract model for a transactional persistence adapter."""

    TRANSITIONS: ClassVar[dict[RequestState, set[RequestState]]] = {
        RequestState.RECEIVED: {RequestState.IDENTITY_VERIFIED},
        RequestState.IDENTITY_VERIFIED: {RequestState.IN_REVIEW},
        RequestState.IN_REVIEW: {
            RequestState.ACTION_PENDING,
            RequestState.PARTIALLY_COMPLETED,
            RequestState.HUMAN_DENIED,
        },
        RequestState.ACTION_PENDING: {
            RequestState.COMPLETED,
            RequestState.PARTIALLY_COMPLETED,
        },
    }

    def __init__(self, policy: PolicyRegistry, *, response_days: int = 30) -> None:
        if response_days <= 0:
            raise ValueError("response deadline must be positive")
        self.policy = policy
        self.response_days = response_days
        self.requests: dict[str, RightsRequest] = {}
        self._dedupe: dict[tuple[str, str], str] = {}
        self.intents: dict[str, LifecycleIntent] = {}
        self.audit: list[Mapping[str, str]] = []

    @staticmethod
    def _valid_ref(value: str, prefix: str) -> bool:
        return value.startswith(prefix) and len(value) > len(prefix) + 4

    def submit(
        self,
        *,
        principal: PrivacyPrincipal,
        request_id: str,
        branch_id: str,
        right: RightType,
        identity_proof_ref: str,
        idempotency_key: str,
        now: datetime,
    ) -> RightsRequest:
        if not principal.subject_id or principal.subject_id != principal.principal_id:
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)
        if principal.branch_id != branch_id:
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)
        if not self._valid_ref(identity_proof_ref, "vault://identity/"):
            raise PrivacyRejected(PrivacyErrorCode.INVALID_IDENTITY_PROOF)
        key = (principal.subject_id, idempotency_key)
        if key in self._dedupe:
            return self.requests[self._dedupe[key]]
        request = RightsRequest(
            request_id=request_id,
            subject_id=principal.subject_id,
            branch_id=branch_id,
            right=right,
            identity_proof_ref=identity_proof_ref,
            created_at=now,
            due_at=now + timedelta(days=self.response_days),
            _idempotency_key=idempotency_key,
        )
        self.requests[request_id] = request
        self._dedupe[key] = request_id
        self.audit.append({"event": "privacy_request_received", "request_id": request_id})
        return request

    def transition(
        self,
        request_id: str,
        target: RequestState,
        *,
        actor: PrivacyPrincipal,
        reason_ref: str | None = None,
        arkaon_initiated: bool = False,
    ) -> RightsRequest:
        request = self.requests[request_id]
        if arkaon_initiated and target in {RequestState.HUMAN_DENIED, RequestState.PARTIALLY_COMPLETED}:
            raise PrivacyRejected(PrivacyErrorCode.AI_AUTHORITY_FORBIDDEN)
        self._authorize_officer(request, actor)
        if target not in self.TRANSITIONS.get(request.state, set()):
            raise PrivacyRejected(PrivacyErrorCode.INVALID_TRANSITION)
        if target is RequestState.HUMAN_DENIED and not self._valid_ref(reason_ref or "", "policy://"):
            raise PrivacyRejected(PrivacyErrorCode.INVALID_TRANSITION)
        request.state = target
        request.decision_reason_ref = reason_ref
        self.audit.append({"event": f"privacy_request_{target.value}", "request_id": request_id})
        return request

    @staticmethod
    def _authorize_officer(request: RightsRequest, actor: PrivacyPrincipal) -> None:
        if not actor.privacy_officer or actor.branch_id != request.branch_id:
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)

    def export(
        self,
        request_id: str,
        records: Iterable[SubjectDataRef],
        *,
        actor: PrivacyPrincipal,
        signing_key: bytes,
    ) -> ExportManifest:
        request = self.requests[request_id]
        self._authorize_officer(request, actor)
        if request.right not in {RightType.ACCESS, RightType.EXPORT}:
            raise PrivacyRejected(PrivacyErrorCode.INVALID_TRANSITION)
        selected = tuple(records)
        if any(r.subject_id != request.subject_id or r.branch_id != request.branch_id for r in selected):
            raise PrivacyRejected(PrivacyErrorCode.MIXED_SUBJECT)
        if len(selected) > 1000:
            raise PrivacyRejected(PrivacyErrorCode.BULK_EXPORT_FORBIDDEN)
        payload = {
            "request_id": request_id,
            "subject_id": request.subject_id,
            "record_refs": [r.record_id for r in selected],
            "records": [dict(sorted(r.minimized.items())) for r in selected],
        }
        digest = hmac.new(
            signing_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return ExportManifest(
            request_id=request_id,
            subject_id=request.subject_id,
            record_refs=tuple(payload["record_refs"]),
            minimized_records=tuple(payload["records"]),
            digest=digest,
        )

    def deletion_intents(
        self,
        request_id: str,
        records: Iterable[SubjectDataRef],
        holds: Iterable[LegalHold],
        *,
        actor: PrivacyPrincipal,
        now: datetime,
    ) -> tuple[LifecycleIntent, ...]:
        request = self.requests[request_id]
        self._authorize_officer(request, actor)
        if request.right is not RightType.DELETION:
            raise PrivacyRejected(PrivacyErrorCode.INVALID_TRANSITION)
        active_holds = {rid for hold in holds if hold.subject_id == request.subject_id and hold.active(now) for rid in hold.record_ids}
        result: list[LifecycleIntent] = []
        for record in records:
            if record.subject_id != request.subject_id or record.branch_id != request.branch_id:
                raise PrivacyRejected(PrivacyErrorCode.MIXED_SUBJECT)
            if record.record_id in active_holds:
                continue
            policy = self.policy.get(record.category)
            kind = "pseudonymize_and_unlink" if record.category in {DataCategory.FINANCIAL, DataCategory.AUDIT} else policy.deletion_mode
            intent_id = f"{request_id}:{record.record_id}:{kind}"
            self.intents.setdefault(intent_id, LifecycleIntent(intent_id, kind, request_id, record.record_id, record.vault_ref))
            result.append(self.intents[intent_id])
        return tuple(result)

    @staticmethod
    def create_hold(
        *, hold_id: str, subject_id: str, record_ids: Iterable[str], reason_ref: str,
        expires_at: datetime, approvers: tuple[str, str], now: datetime,
    ) -> LegalHold:
        ids = frozenset(record_ids)
        if (not ids or len(ids) > 500 or not reason_ref.startswith("legal://") or expires_at <= now
                or approvers[0] == approvers[1] or not all(approvers)):
            raise PrivacyRejected(PrivacyErrorCode.HOLD_INVALID)
        return LegalHold(hold_id, subject_id, ids, reason_ref, expires_at, approvers)

    def dry_run_sweep(
        self, sweep_id: str, records: Iterable[SubjectDataRef], holds: Iterable[LegalHold], *, now: datetime,
    ) -> RetentionSweep:
        active = {rid for h in holds if h.active(now) for rid in h.record_ids}
        candidates = tuple(
            SweepCandidate(r.record_id, r.branch_id, r.collected_at + timedelta(days=self.policy.get(r.category).retention_days), r.record_id in active)
            for r in records if r.collected_at + timedelta(days=self.policy.get(r.category).retention_days) <= now
        )
        return RetentionSweep(sweep_id, candidates)

    def review_sweep(self, sweep: RetentionSweep, *, reviewer_id: str) -> RetentionSweep:
        if sweep.phase is SweepPhase.EXECUTED:
            return sweep
        if not reviewer_id:
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)
        sweep.phase, sweep.reviewer_id = SweepPhase.REVIEWED, reviewer_id
        return sweep

    def execute_sweep(self, sweep: RetentionSweep) -> RetentionSweep:
        if sweep.phase is SweepPhase.EXECUTED:
            return sweep
        if sweep.phase is not SweepPhase.REVIEWED:
            raise PrivacyRejected(PrivacyErrorCode.INVALID_TRANSITION)
        sweep.executed_record_ids = tuple(c.record_id for c in sweep.candidates if not c.blocked_by_hold)
        sweep.phase = SweepPhase.EXECUTED
        self.audit.append({"event": "retention_sweep_executed", "sweep_id": sweep.sweep_id})
        return sweep

    @staticmethod
    def validate_vendor(approval: VendorTransferApproval, *, now: datetime) -> None:
        if approval.expires_at <= now or not approval.subprocessor_register_ref.startswith("register://"):
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)
        if approval.transfer_country and not (approval.transfer_assessment_ref or "").startswith("assessment://"):
            raise PrivacyRejected(PrivacyErrorCode.FORBIDDEN)

    @staticmethod
    def open_breach_case(case_id: str, evidence_refs: Iterable[str], *, now: datetime, assessment_hours: int = 24) -> BreachCase:
        refs = tuple(evidence_refs)
        if not refs or assessment_hours <= 0:
            raise ValueError("breach evidence and clock required")
        return BreachCase(case_id, now, now + timedelta(hours=assessment_hours), refs)


PRIVACY_ROUTE_MANIFEST = {
    "POST /api/v1/privacy/requests": "authenticated subject; Idempotency-Key required",
    "GET /api/v1/privacy/requests/{request_id}": "owner or scoped privacy officer",
    "POST /api/v1/privacy/requests/{request_id}/verify": "scoped privacy officer",
    "POST /api/v1/privacy/requests/{request_id}/actions": "scoped privacy officer",
    "GET /api/v1/privacy/requests/{request_id}/export": "owner; single-request signed manifest",
    "POST /api/v1/privacy/consents/{consent_id}/withdraw": "owner; idempotent",
    "GET /api/v1/privacy/policies": "authenticated subject; public metadata only",
    "POST /api/v1/privacy-ops/holds": "privacy officer; dual approval",
    "POST /api/v1/privacy-ops/sweeps": "privacy officer; dry-run only",
    "POST /api/v1/privacy-ops/sweeps/{sweep_id}/execute": "different reviewer; reviewed only",
}


def default_policy_registry() -> PolicyRegistry:
    days = {DataCategory.ACCOUNT: 365, DataCategory.CONTACT: 90, DataCategory.LOCATION: 7,
            DataCategory.EVIDENCE: 30, DataCategory.SUPPORT: 365, DataCategory.FINANCIAL: 1825,
            DataCategory.AUDIT: 1825}
    return PolicyRegistry(DataPolicy(c, f"purpose://{c.value}", f"legal-basis://{c.value}", d) for c, d in days.items())
