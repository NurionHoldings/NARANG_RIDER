"""Fail-closed ARKAON pre-deployment inspection and remediation reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256


class ReadinessRejected(ValueError):
    pass


class FindingSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFO = "INFO"


class ReadinessVerdict(StrEnum):
    BLOCKED = "BLOCKED"
    READY_FOR_OPERATOR_REVIEW = "READY_FOR_OPERATOR_REVIEW"


def _digest_ok(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _commit_sha_ok(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def canonical_digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class IntegrationCandidate:
    candidate_id: str
    source_head: str
    base_main: str
    included_prs: tuple[int, ...]
    source_tree_digest: str
    synthetic_only: bool = True

    def __post_init__(self) -> None:
        if (
            not self.candidate_id
            or not _commit_sha_ok(self.source_head)
            or not _commit_sha_ok(self.base_main)
            or not _digest_ok(self.source_tree_digest)
            or not self.included_prs
            or len(set(self.included_prs)) != len(self.included_prs)
            or tuple(sorted(self.included_prs)) != self.included_prs
            or not self.synthetic_only
        ):
            raise ReadinessRejected("immutable synthetic integration candidate required")


@dataclass(frozen=True)
class CheckEvidence:
    check_id: str
    category: str
    status: str
    evidence_digest: str
    observed_at: datetime
    expires_at: datetime
    commit_sha: str
    synthetic: bool = True

    def __post_init__(self) -> None:
        if (
            not self.check_id
            or self.status not in {"PASS", "FAIL", "MISSING", "STALE"}
            or not _digest_ok(self.evidence_digest)
            or not _commit_sha_ok(self.commit_sha)
            or self.observed_at.tzinfo is None
            or not self.observed_at < self.expires_at
            or not self.synthetic
        ):
            raise ReadinessRejected("valid bounded synthetic check evidence required")


@dataclass(frozen=True)
class ReadinessFinding:
    finding_id: str
    category: str
    severity: FindingSeverity
    title: str
    owner_role: str
    remediation: str
    evidence_digest: str
    arkaon_may_fix: bool
    operator_decision_required: bool


@dataclass(frozen=True)
class PredeploymentReport:
    report_id: str
    candidate_id: str
    candidate_head: str
    generated_at: datetime
    verdict: ReadinessVerdict
    device_test_ready: bool
    merge_recommended: bool
    deploy_allowed: bool
    findings: tuple[ReadinessFinding, ...]
    report_digest: str


class ArkaonPredeploymentInspector:
    """Reports gaps but cannot waive, sign, merge, deploy, or self-approve them."""

    REQUIRED_CHECKS = frozenset(
        {
            "SOURCE_LINEAGE",
            "UNIT_REGRESSION",
            "STATIC_SECURITY",
            "MIGRATION_RECOVERY",
            "SYNTHETIC_CONTRACT",
            "DEVICE_TEST_PLAN",
            "SIGNED_TEST_BUILD",
            "NONPROD_CREDENTIALS",
            "ROLLBACK_DRILL",
            "PRIVACY_REVIEW",
            "LEGAL_REVIEW",
            "OPERATOR_APPROVAL",
        }
    )
    DEVICE_REQUIRED = frozenset(
        {"DEVICE_TEST_PLAN", "SIGNED_TEST_BUILD", "NONPROD_CREDENTIALS", "ROLLBACK_DRILL"}
    )
    HUMAN_ONLY = frozenset({"PRIVACY_REVIEW", "LEGAL_REVIEW", "OPERATOR_APPROVAL"})

    def inspect(
        self,
        *,
        report_id: str,
        candidate: IntegrationCandidate,
        evidence: tuple[CheckEvidence, ...],
        now: datetime,
    ) -> PredeploymentReport:
        if not report_id or now.tzinfo is None:
            raise ReadinessRejected("report identity and timezone-aware generation time required")
        by_id = {item.check_id: item for item in evidence}
        if len(by_id) != len(evidence):
            raise ReadinessRejected("duplicate readiness check evidence")
        unknown = set(by_id) - self.REQUIRED_CHECKS
        if unknown:
            raise ReadinessRejected("unknown checks cannot influence readiness")

        findings: list[ReadinessFinding] = []
        passing: set[str] = set()
        for check_id in sorted(self.REQUIRED_CHECKS):
            item = by_id.get(check_id)
            status = "MISSING" if item is None else item.status
            if item is not None and item.commit_sha != candidate.source_head:
                status = "FAIL"
            if item is not None and now >= item.expires_at:
                status = "STALE"
            if status == "PASS":
                passing.add(check_id)
                continue
            human_only = check_id in self.HUMAN_ONLY
            findings.append(
                ReadinessFinding(
                    finding_id=f"finding:{check_id.lower()}",
                    category=item.category if item else self._category(check_id),
                    severity=FindingSeverity.BLOCKER,
                    title=f"{check_id}: {status}",
                    owner_role=self._owner(check_id),
                    remediation=self._remediation(check_id, status),
                    evidence_digest=item.evidence_digest if item else "0" * 64,
                    arkaon_may_fix=not human_only,
                    operator_decision_required=human_only or check_id == "SIGNED_TEST_BUILD",
                )
            )

        device_ready = self.DEVICE_REQUIRED <= passing
        verdict = (
            ReadinessVerdict.READY_FOR_OPERATOR_REVIEW
            if self.REQUIRED_CHECKS <= passing
            else ReadinessVerdict.BLOCKED
        )
        serializable_findings = [
            {
                **finding.__dict__,
                "severity": finding.severity.value,
            }
            for finding in findings
        ]
        digest = canonical_digest(
            {
                "report_id": report_id,
                "candidate_id": candidate.candidate_id,
                "candidate_head": candidate.source_head,
                "generated_at": now.isoformat(),
                "verdict": verdict.value,
                "device_test_ready": device_ready,
                "findings": serializable_findings,
            }
        )
        return PredeploymentReport(
            report_id,
            candidate.candidate_id,
            candidate.source_head,
            now,
            verdict,
            device_ready,
            False,
            False,
            tuple(findings),
            digest,
        )

    def accept_resolution(self, *_args: object, **_kwargs: object) -> None:
        raise ReadinessRejected("ARKAON cannot accept its own remediation")

    def authorize_merge_or_deploy(self) -> None:
        raise ReadinessRejected("ARKAON cannot authorize merge or deployment")

    @staticmethod
    def _category(check_id: str) -> str:
        if check_id in {"DEVICE_TEST_PLAN", "SIGNED_TEST_BUILD", "NONPROD_CREDENTIALS"}:
            return "DEVICE_TEST"
        if check_id in {"PRIVACY_REVIEW", "LEGAL_REVIEW", "OPERATOR_APPROVAL"}:
            return "EXTERNAL_APPROVAL"
        return "ENGINEERING"

    @staticmethod
    def _owner(check_id: str) -> str:
        return {
            "PRIVACY_REVIEW": "PRIVACY_REVIEWER",
            "LEGAL_REVIEW": "LEGAL_REVIEWER",
            "OPERATOR_APPROVAL": "OPERATOR",
            "SIGNED_TEST_BUILD": "RELEASE_ENGINEER",
            "NONPROD_CREDENTIALS": "SECURITY_OPERATOR",
            "DEVICE_TEST_PLAN": "DEVICE_TEST_LEAD",
        }.get(check_id, "ENGINEERING_LEAD")

    @staticmethod
    def _remediation(check_id: str, status: str) -> str:
        return f"{check_id} 증거를 동일 후보 커밋으로 생성·독립검토하고 상태 {status}를 해소한다."
