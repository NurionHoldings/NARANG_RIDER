"""Fail-closed monitoring for approved map-provider documentation snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlsplit


class DocumentMonitorRejected(ValueError):
    pass


class ChangeSeverity(StrEnum):
    NONE = "none"
    ROUTINE = "routine"
    BREAKING = "breaking"
    SECURITY = "security"


class KnowledgeState(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    HUMAN_REVIEW = "human_review"
    BLOCKED = "blocked"


SENSITIVE_FIELDS = frozenset(
    {
        "scheme",
        "host",
        "path",
        "query_parameters",
        "api_key_policy",
        "data_retention",
        "subprocessors",
        "motorcycle_support",
    }
)


@dataclass(frozen=True)
class OfficialDocumentSnapshot:
    evidence_id: str
    provider_id: str
    official_url: str
    title: str
    accessed_at: datetime
    expires_at: datetime
    content_sha256: str
    extracted_fields: frozenset[str]
    source_authority_verified: bool
    raw_document_retained: bool = False
    contains_prompt_injection: bool = False

    def validate(self) -> None:
        parsed = urlsplit(self.official_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise DocumentMonitorRejected("official HTTPS source required")
        if not self.evidence_id or not self.provider_id or not self.title:
            raise DocumentMonitorRejected("evidence identity required")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256
        ):
            raise DocumentMonitorRejected("lowercase SHA-256 required")
        if self.expires_at <= self.accessed_at:
            raise DocumentMonitorRejected("expiry must follow access time")
        if self.raw_document_retained:
            raise DocumentMonitorRejected("raw provider document retention is forbidden")


@dataclass(frozen=True)
class DocumentChangeReport:
    provider_id: str
    previous_evidence_id: str
    candidate_evidence_id: str
    severity: ChangeSeverity
    knowledge_state: KnowledgeState
    added_fields: tuple[str, ...]
    removed_fields: tuple[str, ...]
    requires_contract_tests: bool
    requires_ethernian_review: bool
    production_activation_allowed: bool
    report_digest: str

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.map-document-change.v1", **self.__dict__}


class MapDocumentMonitor:
    def compare(
        self,
        *,
        previous: OfficialDocumentSnapshot,
        candidate: OfficialDocumentSnapshot,
        now: datetime,
    ) -> DocumentChangeReport:
        previous.validate()
        candidate.validate()
        if previous.provider_id != candidate.provider_id:
            raise DocumentMonitorRejected("cross-provider comparison forbidden")
        if candidate.accessed_at < previous.accessed_at:
            raise DocumentMonitorRejected("snapshot chronology regression")

        added = tuple(sorted(candidate.extracted_fields - previous.extracted_fields))
        removed = tuple(sorted(previous.extracted_fields - candidate.extracted_fields))
        changed = previous.content_sha256 != candidate.content_sha256
        sensitive_change = bool((set(added) | set(removed)) & SENSITIVE_FIELDS)

        if candidate.contains_prompt_injection or not candidate.source_authority_verified:
            severity = ChangeSeverity.SECURITY
            state = KnowledgeState.BLOCKED
        elif now >= candidate.expires_at:
            severity = ChangeSeverity.BREAKING if changed else ChangeSeverity.NONE
            state = KnowledgeState.STALE
        elif sensitive_change or removed:
            severity = ChangeSeverity.BREAKING
            state = KnowledgeState.HUMAN_REVIEW
        elif changed:
            severity = ChangeSeverity.ROUTINE
            state = KnowledgeState.HUMAN_REVIEW
        else:
            severity = ChangeSeverity.NONE
            state = KnowledgeState.CURRENT

        requires_tests = changed or bool(added or removed)
        requires_review = state is not KnowledgeState.CURRENT
        canonical = "|".join(
            (
                previous.provider_id,
                previous.evidence_id,
                candidate.evidence_id,
                severity.value,
                state.value,
                ",".join(added),
                ",".join(removed),
                str(requires_tests),
                str(requires_review),
            )
        )
        return DocumentChangeReport(
            provider_id=previous.provider_id,
            previous_evidence_id=previous.evidence_id,
            candidate_evidence_id=candidate.evidence_id,
            severity=severity,
            knowledge_state=state,
            added_fields=added,
            removed_fields=removed,
            requires_contract_tests=requires_tests,
            requires_ethernian_review=requires_review,
            production_activation_allowed=False,
            report_digest=sha256(canonical.encode()).hexdigest(),
        )


def safe_refresh_proposal(report: DocumentChangeReport) -> dict[str, object]:
    """Produce a review proposal, never an automatic registry or runtime update."""
    return {
        "provider_id": report.provider_id,
        "candidate_evidence_id": report.candidate_evidence_id,
        "knowledge_state": report.knowledge_state.value,
        "report_digest": report.report_digest,
        "next_action": "human_review" if report.requires_ethernian_review else "no_change",
        "registry_write_allowed": False,
        "runtime_change_allowed": False,
        "model_learning_allowed": False,
    }
