from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .network import BranchRegistry, BranchStatus, BranchType


@dataclass(frozen=True)
class NationalOperatingPolicy:
    policy_id: str
    version: int
    minimum_base_pay_won: int
    maximum_evidence_retention: timedelta
    maximum_settlement_delay_hours: int
    forbidden_ai_authorities: frozenset[str]
    effective_from: datetime

    def __post_init__(self) -> None:
        if (
            not self.policy_id.strip()
            or self.version < 1
            or self.minimum_base_pay_won < 0
            or self.maximum_evidence_retention <= timedelta(0)
            or self.maximum_settlement_delay_hours < 0
            or self.effective_from.tzinfo is None
            or not self.forbidden_ai_authorities
        ):
            raise ValueError("VALID_NATIONAL_POLICY_REQUIRED")


@dataclass(frozen=True)
class BranchPolicyOverride:
    override_id: str
    branch_id: str
    national_policy_id: str
    national_policy_version: int
    base_pay_won: int
    evidence_retention: timedelta
    settlement_delay_hours: int
    requested_by: str
    approved_by_hq: str
    approved_by_branch: str
    effective_from: datetime

    def __post_init__(self) -> None:
        if self.approved_by_hq == self.approved_by_branch:
            raise ValueError("POLICY_OVERRIDE_DUAL_APPROVAL_REQUIRED")


@dataclass(frozen=True)
class EffectiveBranchPolicy:
    branch_id: str
    source_policy_id: str
    source_policy_version: int
    base_pay_won: int
    evidence_retention: timedelta
    settlement_delay_hours: int
    forbidden_ai_authorities: frozenset[str]
    override_id: str | None


class BranchPolicyResolver:
    def __init__(
        self,
        *,
        branches: BranchRegistry,
        national_policy: NationalOperatingPolicy,
    ) -> None:
        self._branches = branches
        self._national = national_policy
        self._overrides: dict[str, BranchPolicyOverride] = {}

    def register_override(self, value: BranchPolicyOverride) -> BranchPolicyOverride:
        branch = self._branches.get(value.branch_id)
        if branch.branch_type is BranchType.HEADQUARTERS:
            raise ValueError("HEADQUARTERS_USES_NATIONAL_POLICY")
        if value.national_policy_id != self._national.policy_id:
            raise ValueError("NATIONAL_POLICY_MISMATCH")
        if value.national_policy_version != self._national.version:
            raise ValueError("STALE_NATIONAL_POLICY_VERSION")
        if value.base_pay_won < self._national.minimum_base_pay_won:
            raise ValueError("BRANCH_CANNOT_LOWER_MINIMUM_PAY")
        if value.evidence_retention > self._national.maximum_evidence_retention:
            raise ValueError("BRANCH_CANNOT_EXTEND_EVIDENCE_RETENTION")
        if value.settlement_delay_hours > self._national.maximum_settlement_delay_hours:
            raise ValueError("BRANCH_CANNOT_DELAY_SETTLEMENT")
        if value.branch_id in self._overrides:
            raise ValueError("BRANCH_POLICY_OVERRIDE_APPEND_ONLY")
        self._overrides[value.branch_id] = value
        return value

    def resolve(self, branch_id: str) -> EffectiveBranchPolicy:
        self._branches.get(branch_id)
        override = self._overrides.get(branch_id)
        if override is None:
            return EffectiveBranchPolicy(
                branch_id=branch_id,
                source_policy_id=self._national.policy_id,
                source_policy_version=self._national.version,
                base_pay_won=self._national.minimum_base_pay_won,
                evidence_retention=self._national.maximum_evidence_retention,
                settlement_delay_hours=self._national.maximum_settlement_delay_hours,
                forbidden_ai_authorities=self._national.forbidden_ai_authorities,
                override_id=None,
            )
        return EffectiveBranchPolicy(
            branch_id=branch_id,
            source_policy_id=self._national.policy_id,
            source_policy_version=self._national.version,
            base_pay_won=override.base_pay_won,
            evidence_retention=override.evidence_retention,
            settlement_delay_hours=override.settlement_delay_hours,
            forbidden_ai_authorities=self._national.forbidden_ai_authorities,
            override_id=override.override_id,
        )


class ReadinessItem(StrEnum):
    LEGAL_AUTHORITY = "LEGAL_AUTHORITY"
    INSURANCE_COVERAGE = "INSURANCE_COVERAGE"
    RIDER_SUPPORT = "RIDER_SUPPORT"
    MERCHANT_SUPPORT = "MERCHANT_SUPPORT"
    SETTLEMENT_ACCOUNT = "SETTLEMENT_ACCOUNT"
    PRIVACY_OFFICER = "PRIVACY_OFFICER"
    INCIDENT_CONTACT = "INCIDENT_CONTACT"
    POLICY_ACCEPTANCE = "POLICY_ACCEPTANCE"


@dataclass(frozen=True)
class ReadinessEvidence:
    item: ReadinessItem
    evidence_reference: str
    verified_by: str
    verified_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not self.evidence_reference.startswith("vault:")
            or not self.verified_by.strip()
            or self.verified_at.tzinfo is None
        ):
            raise ValueError("VERIFIED_VAULT_EVIDENCE_REQUIRED")
        if self.expires_at is not None and self.expires_at <= self.verified_at:
            raise ValueError("INVALID_READINESS_EXPIRY")


@dataclass(frozen=True)
class BranchReadiness:
    branch_id: str
    passed_items: tuple[ReadinessItem, ...]
    missing_items: tuple[ReadinessItem, ...]
    expired_items: tuple[ReadinessItem, ...]
    ready: bool


class BranchOnboardingService:
    def __init__(self, *, branches: BranchRegistry) -> None:
        self._branches = branches
        self._evidence: dict[tuple[str, ReadinessItem], ReadinessEvidence] = {}

    def record(
        self,
        *,
        branch_id: str,
        evidence: ReadinessEvidence,
    ) -> ReadinessEvidence:
        branch = self._branches.get(branch_id)
        if branch.status is not BranchStatus.PROVISIONING:
            raise ValueError("PROVISIONING_BRANCH_REQUIRED")
        key = (branch_id, evidence.item)
        if key in self._evidence:
            raise ValueError("READINESS_EVIDENCE_APPEND_ONLY")
        self._evidence[key] = evidence
        return evidence

    def evaluate(self, *, branch_id: str, now: datetime) -> BranchReadiness:
        self._branches.get(branch_id)
        passed: list[ReadinessItem] = []
        expired: list[ReadinessItem] = []
        for item in ReadinessItem:
            evidence = self._evidence.get((branch_id, item))
            if evidence is None:
                continue
            if evidence.expires_at is not None and now >= evidence.expires_at:
                expired.append(item)
            else:
                passed.append(item)
        missing = [item for item in ReadinessItem if item not in passed and item not in expired]
        return BranchReadiness(
            branch_id=branch_id,
            passed_items=tuple(sorted(passed, key=lambda item: item.value)),
            missing_items=tuple(sorted(missing, key=lambda item: item.value)),
            expired_items=tuple(sorted(expired, key=lambda item: item.value)),
            ready=not missing and not expired,
        )

    def activate(
        self,
        *,
        branch_id: str,
        now: datetime,
        expected_policy_version: int,
        hq_approver: str,
        branch_approver: str,
    ):
        if hq_approver == branch_approver:
            raise ValueError("BRANCH_ACTIVATION_DUAL_APPROVAL_REQUIRED")
        readiness = self.evaluate(branch_id=branch_id, now=now)
        if not readiness.ready:
            raise ValueError("BRANCH_NOT_READY")
        return self._branches.activate(
            branch_id=branch_id,
            expected_policy_version=expected_policy_version,
        )
