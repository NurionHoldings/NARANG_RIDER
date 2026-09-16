"""Release-candidate evidence aggregation with fail-closed launch gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReleaseVerdict(StrEnum):
    BLOCKED = "BLOCKED"
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"


class EvidenceStatus(StrEnum):
    PASS = "pass"
    MISSING = "missing"
    EXPIRED = "expired"
    FAIL = "fail"


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    category: str
    digest: str | None
    status: EvidenceStatus
    expires_at: datetime | None = None
    reference: str | None = None

    def valid(self, now: datetime) -> bool:
        return (
            self.status is EvidenceStatus.PASS
            and bool(self.digest)
            and (self.expires_at is None or now < self.expires_at)
        )


@dataclass(frozen=True)
class ReleaseRequirement:
    requirement_id: str
    title: str
    domain_refs: tuple[str, ...]
    api_refs: tuple[str, ...]
    db_refs: tuple[str, ...]
    frontend_refs: tuple[str, ...]
    test_refs: tuple[str, ...]
    threat_controls: tuple[str, ...]

    def missing_layers(self) -> tuple[str, ...]:
        layers = {
            "domain": self.domain_refs,
            "api": self.api_refs,
            "db": self.db_refs,
            "frontend": self.frontend_refs,
            "tests": self.test_refs,
            "threat_controls": self.threat_controls,
        }
        return tuple(name for name, refs in layers.items() if not refs)


@dataclass(frozen=True)
class TraceabilityReport:
    requirement_count: int
    orphan_refs: tuple[str, ...]
    missing_coverage: Mapping[str, tuple[str, ...]]
    digest: str

    @property
    def complete(self) -> bool:
        return not self.orphan_refs and not self.missing_coverage


class TraceabilityVerifier:
    LAYERS = ("domain_refs", "api_refs", "db_refs", "frontend_refs", "test_refs", "threat_controls")

    @staticmethod
    def verify(
        requirements: Iterable[ReleaseRequirement], *, known_refs: Iterable[str]
    ) -> TraceabilityReport:
        values = tuple(requirements)
        known = frozenset(known_refs)
        missing = {item.requirement_id: item.missing_layers() for item in values if item.missing_layers()}
        used = {
            ref
            for item in values
            for layer in TraceabilityVerifier.LAYERS
            for ref in getattr(item, layer)
        }
        orphan = tuple(sorted(used - known))
        payload = {
            "requirements": [item.requirement_id for item in values],
            "orphan_refs": orphan,
            "missing_coverage": missing,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return TraceabilityReport(len(values), orphan, missing, digest)


@dataclass(frozen=True)
class ReleaseCandidate:
    version: str
    commit_sha: str
    traceability_digest: str
    evidence_digest: str
    verdict: ReleaseVerdict
    blockers: tuple[str, ...]
    limitations: tuple[str, ...]
    compliance_claim: str = "NOT_A_COMPLIANCE_OR_CERTIFICATION_CLAIM"


class ReleaseCandidateEvaluator:
    REQUIRED_EVIDENCE = frozenset(
        {
            "ci_jobs",
            "migrations",
            "sbom",
            "synthetic_e2e",
            "postgres_live",
            "accessibility_contract",
            "capacity_simulation",
            "partner_official_api",
            "partner_sandbox_credentials",
            "map_provider_sandbox",
            "notification_provider_sandbox",
            "insurance_provider_sandbox",
            "legal_counsel_approval",
            "privacy_officer_approval",
            "operator_approval",
            "main_merge",
            "production_infrastructure",
            "production_secret_provisioning",
            "field_pilot",
        }
    )

    def evaluate(
        self,
        *,
        version: str,
        commit_sha: str,
        traceability: TraceabilityReport,
        evidence: Iterable[EvidenceItem],
        now: datetime,
    ) -> ReleaseCandidate:
        supplied = {item.category: item for item in evidence}
        blockers = []
        if not traceability.complete:
            blockers.append("traceability_incomplete")
        for category in sorted(self.REQUIRED_EVIDENCE):
            item = supplied.get(category)
            if item is None or not item.valid(now):
                blockers.append(category)
        canonical = [
            {
                "category": item.category,
                "digest": item.digest,
                "status": item.status.value,
                "reference": item.reference,
            }
            for item in sorted(supplied.values(), key=lambda value: value.category)
        ]
        evidence_digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        verdict = (
            ReleaseVerdict.READY_FOR_OPERATOR_REVIEW if not blockers else ReleaseVerdict.BLOCKED
        )
        limitations = (
            "synthetic_and_contract_tests_do_not_replace_field_validation",
            "provisional_partner_profiles_are_not_official_certification",
            "legal_matrix_is_advisory_and_requires_professional_approval",
            "no_production_credentials_or_personal_data_were_used",
        )
        return ReleaseCandidate(
            version,
            commit_sha,
            traceability.digest,
            evidence_digest,
            verdict,
            tuple(blockers),
            limitations,
        )


@dataclass(frozen=True)
class FreezeException:
    exception_id: str
    reason: str
    risk_ref: str
    rollback_ref: str
    ethernian_approver: str
    operator_approver: str
    expires_at: datetime

    def valid(self, now: datetime) -> bool:
        return (
            bool(self.reason)
            and self.risk_ref.startswith("risk://")
            and self.rollback_ref.startswith("rollback://")
            and self.ethernian_approver.startswith("ethernian:")
            and self.operator_approver.startswith("operator:")
            and self.ethernian_approver != self.operator_approver
            and now < self.expires_at
        )


@dataclass(frozen=True)
class ChangeFreezePolicy:
    release_version: str
    frozen_commit_sha: str
    allowed_paths: frozenset[str] = frozenset(
        {"docs/known-limitations.md", "release/release-candidate-report.json"}
    )

    def authorize(
        self,
        changed_paths: Iterable[str],
        *,
        exception: FreezeException | None,
        now: datetime,
    ) -> bool:
        changes = frozenset(changed_paths)
        return changes.issubset(self.allowed_paths) or bool(exception and exception.valid(now))


FINAL_SMOKE_CHECKLIST = (
    "config_fail_closed",
    "database_migration_and_rls",
    "oidc_jwks_rotation",
    "merchant_order_to_rider_delivery",
    "masked_customer_evidence",
    "balanced_ledger_and_payout_instruction",
    "outbox_retry_dlq_and_human_review",
    "branch_pause_and_dual_control_resume",
    "backup_restore_rpo_rto",
    "privacy_rights_and_support_appeal",
    "rollback_rehearsal",
)
