"""Fail-closed external provider evidence intake."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlparse


class ProviderKind(StrEnum):
    AI_BAEBI = "ai_baebi"
    DOSIRAK_STORE = "dosirak_store"
    ORDER_NETWORK = "pos_platform_agency"
    MAP = "map_geocoder_routing"
    NOTIFICATION = "push_sms_email"
    INSURANCE = "insurer_workers_comp"
    PAYMENT = "payment_payout"
    SECURITY_MEDIA = "vault_media_malware"


class IntakeStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUARANTINED = "QUARANTINED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    CERTIFIED = "CERTIFIED"


ALLOWED_MEDIA_TYPES = frozenset(
    {"application/json", "application/yaml", "text/markdown", "text/plain", "application/pdf"}
)
REQUIRED_APPROVERS = frozenset({"ETHERNian", "OPERATOR"})
SECRET_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|client[_-]?secret|password|private[_-]?key)\s*[:=]\s*\S+"
)
PII_PATTERN = re.compile(r"(?:01[016789]-?\d{3,4}-?\d{4}|\d{6}-?[1-4]\d{6})")


class IntakeRejected(ValueError):
    """Evidence cannot safely advance."""


@dataclass(frozen=True, slots=True)
class EvidenceSubmission:
    evidence_id: str
    provider_kind: ProviderKind
    provider_identity_ref: str
    source_url: str
    document_sha256: str
    issued_at: datetime
    expires_at: datetime
    environment: str
    media_type: str
    malware_scan_ref: str
    schema_version: str
    schema_compatible: bool
    contains_embedded_secrets: bool = False
    contains_personal_data: bool = False
    status: IntakeStatus = IntakeStatus.RECEIVED
    verifier_refs: tuple[str, ...] = ()
    block_reasons: tuple[str, ...] = ()
    harness_passed: bool = False
    approval_roles: frozenset[str] = frozenset()

    def canonical_digest(self) -> str:
        payload = (
            f"{self.evidence_id}|{self.provider_kind.value}|"
            f"{self.provider_identity_ref}|{self.source_url}|{self.document_sha256}|"
            f"{self.issued_at.isoformat()}|{self.expires_at.isoformat()}|"
            f"{self.environment}|{self.media_type}|{self.schema_version}"
        )
        return sha256(payload.encode()).hexdigest()


class EvidenceIntake:
    """State machine which treats every upload as untrusted."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._records: dict[str, EvidenceSubmission] = {}

    def receive(self, item: EvidenceSubmission) -> EvidenceSubmission:
        if item.evidence_id in self._records:
            if self._records[item.evidence_id].canonical_digest() != item.canonical_digest():
                raise IntakeRejected("evidence id reuse with different metadata")
            return self._records[item.evidence_id]
        self._validate_envelope(item)
        received = replace(item, status=IntakeStatus.RECEIVED)
        self._records[item.evidence_id] = received
        return received

    def quarantine(self, evidence_id: str, *, scan_passed: bool) -> EvidenceSubmission:
        item = self._expect(evidence_id, IntakeStatus.RECEIVED)
        if not scan_passed:
            blocked = replace(
                item, status=IntakeStatus.BLOCKED, block_reasons=("malware scan failed",)
            )
            self._records[evidence_id] = blocked
            return blocked
        quarantined = replace(item, status=IntakeStatus.QUARANTINED)
        self._records[evidence_id] = quarantined
        return quarantined

    def verify(
        self,
        evidence_id: str,
        *,
        verifier_refs: tuple[str, ...],
        official_source_confirmed: bool,
    ) -> EvidenceSubmission:
        item = self._expect(evidence_id, IntakeStatus.QUARANTINED)
        reasons = []
        if not official_source_confirmed:
            reasons.append("official source not confirmed")
        if not item.schema_compatible:
            reasons.append("schema incompatible")
        if not verifier_refs:
            reasons.append("independent verification missing")
        status = IntakeStatus.BLOCKED if reasons else IntakeStatus.VERIFIED
        checked = replace(
            item,
            status=status,
            verifier_refs=verifier_refs,
            block_reasons=tuple(reasons),
        )
        self._records[evidence_id] = checked
        return checked

    def certify(
        self,
        evidence_id: str,
        *,
        harness_passed: bool,
        approval_roles: frozenset[str],
    ) -> EvidenceSubmission:
        item = self._expect(evidence_id, IntakeStatus.VERIFIED)
        if not harness_passed:
            raise IntakeRejected("existing sandbox certification harness must pass")
        if not REQUIRED_APPROVERS.issubset(approval_roles):
            raise IntakeRejected("ETHERNian and operator approval are both required")
        certified = replace(
            item,
            status=IntakeStatus.CERTIFIED,
            harness_passed=True,
            approval_roles=approval_roles,
        )
        self._records[evidence_id] = certified
        return certified

    def get(self, evidence_id: str) -> EvidenceSubmission:
        return self._records[evidence_id]

    def _expect(self, evidence_id: str, expected: IntakeStatus) -> EvidenceSubmission:
        item = self._records[evidence_id]
        if item.status is not expected:
            raise IntakeRejected(f"expected {expected}, got {item.status}")
        return item

    def _validate_envelope(self, item: EvidenceSubmission) -> None:
        if not item.provider_identity_ref.startswith("provider-registry://"):
            raise IntakeRejected("provider identity must be a registry reference")
        parsed = urlparse(item.source_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise IntakeRejected("source URL must be an official credential-free HTTPS URL")
        if not re.fullmatch(r"[0-9a-f]{64}", item.document_sha256):
            raise IntakeRejected("document digest must be lowercase SHA-256")
        if item.environment != "sandbox":
            raise IntakeRejected("only sandbox evidence is accepted")
        if item.media_type not in ALLOWED_MEDIA_TYPES:
            raise IntakeRejected("file type is not allowed")
        if not item.malware_scan_ref.startswith("malware-scan://"):
            raise IntakeRejected("malware scan reference is required")
        if item.issued_at.tzinfo is None or item.expires_at.tzinfo is None:
            raise IntakeRejected("timestamps must be timezone-aware")
        if not item.issued_at <= self.now < item.expires_at:
            raise IntakeRejected("evidence is not currently valid")
        if item.contains_embedded_secrets or item.contains_personal_data:
            raise IntakeRejected("embedded secrets or personal data are forbidden")


def inspect_text_for_forbidden_data(text: str) -> tuple[str, ...]:
    """Preflight only; never replaces malware and DLP scanning."""
    findings = []
    if SECRET_PATTERN.search(text):
        findings.append("possible secret")
    if PII_PATTERN.search(text):
        findings.append("possible personal data")
    return tuple(findings)


COMMON_REQUEST_FIELDS = (
    "official API/OpenAPI/event schema and version",
    "sandbox URLs and account approval process",
    "authentication, webhook signature and key rotation",
    "IP allow-list and mTLS requirements",
    "rate limits, SLA, errors, idempotency, versioning and deprecation",
    "data fields, purpose, retention, cross-border transfer and subprocessors",
    "webhook replay protection and ordering guarantees",
    "security and incident contacts",
    "sandbox/production pricing and governing legal documents",
)


def operator_checklist(kind: ProviderKind) -> dict[str, object]:
    return {
        "provider_kind": kind.value,
        "required_materials": COMMON_REQUEST_FIELDS,
        "secure_delivery": (
            "Never send production secrets or personal data via email/repository. "
            "Use the approved secure channel and record vault references only."
        ),
        "certification_gate": (
            "Sandbox only; quarantine and verify; existing harness PASS; "
            "ETHERNian plus operator approval. Upload alone never establishes trust."
        ),
        "release_status": "BLOCKED",
    }
