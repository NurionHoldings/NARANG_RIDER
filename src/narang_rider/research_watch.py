"""Official-source research intake with mandatory Ethernian and operator review."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlsplit


class ResearchRejected(ValueError):
    pass


class ResearchStage(StrEnum):
    QUARANTINED = "QUARANTINED"
    EVIDENCE_VERIFIED = "EVIDENCE_VERIFIED"
    ETERNIAN_REVIEW = "ETERNIAN_REVIEW"
    IMPROVEMENT_PROPOSED = "IMPROVEMENT_PROPOSED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ResearchSource:
    source_id: str
    url: str
    title: str
    publisher: str
    published_at: datetime
    retrieved_at: datetime
    expires_at: datetime
    content_digest: str
    official: bool
    untrusted_input: bool = True


@dataclass(frozen=True)
class ResearchCandidate:
    candidate_id: str
    domain: str
    source: ResearchSource
    feature_summary: str
    novelty: str
    user_value: str
    risks: tuple[str, ...]
    synthetic_test_plan: tuple[str, ...]
    stage: ResearchStage = ResearchStage.QUARANTINED
    eternian_review_digest: str | None = None
    proposal_digest: str | None = None
    automatic_learning: bool = False
    production_change_allowed: bool = False


@dataclass(frozen=True)
class StartupTrigger:
    trigger_id: str
    observed_at: datetime
    source: str
    machine_identifier_collected: bool = False


class ArkaonResearchWatch:
    DOMAINS = frozenset(
        {
            "DELIVERY",
            "RIDER",
            "MOBILITY",
            "MOTORCYCLE_FINANCE",
            "USED_MARKET",
            "INSURANCE_COMPARISON",
            "ELECTRONIC_CONTRACT",
        }
    )

    def __init__(self, official_hosts: frozenset[str]) -> None:
        if not official_hosts or any("." not in host for host in official_hosts):
            raise ResearchRejected("explicit official host registry required")
        self.official_hosts = official_hosts
        self._candidates: dict[str, ResearchCandidate] = {}
        self._triggers: set[str] = set()

    def accept_startup_trigger(self, trigger: StartupTrigger) -> None:
        if (
            not trigger.trigger_id
            or trigger.observed_at.tzinfo is None
            or trigger.source not in {"WINDOWS_TASK_SCHEDULER", "SCHEDULED_AUTOMATION", "MANUAL"}
            or trigger.machine_identifier_collected
            or trigger.trigger_id in self._triggers
        ):
            raise ResearchRejected("privacy-safe unique startup trigger required")
        self._triggers.add(trigger.trigger_id)

    def quarantine(self, candidate: ResearchCandidate) -> ResearchCandidate:
        source = candidate.source
        host = urlsplit(source.url).hostname
        if (
            candidate.candidate_id in self._candidates
            or candidate.domain not in self.DOMAINS
            or not source.official
            or not source.untrusted_input
            or host not in self.official_hosts
            or urlsplit(source.url).scheme != "https"
            or source.published_at.tzinfo is None
            or source.retrieved_at.tzinfo is None
            or not source.retrieved_at < source.expires_at
            or not self._digest_ok(source.content_digest)
            or not candidate.feature_summary.strip()
            or not candidate.risks
            or not candidate.synthetic_test_plan
            or candidate.automatic_learning
            or candidate.production_change_allowed
        ):
            raise ResearchRejected("complete official-source quarantined candidate required")
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def verify_evidence(self, candidate_id: str, *, now: datetime) -> ResearchCandidate:
        candidate = self._require(candidate_id, ResearchStage.QUARANTINED)
        if now.tzinfo is None or now >= candidate.source.expires_at:
            raise ResearchRejected("fresh source evidence required")
        return self._save(replace(candidate, stage=ResearchStage.EVIDENCE_VERIFIED))

    def request_eternian_review(self, candidate_id: str) -> ResearchCandidate:
        candidate = self._require(candidate_id, ResearchStage.EVIDENCE_VERIFIED)
        return self._save(replace(candidate, stage=ResearchStage.ETERNIAN_REVIEW))

    def record_eternian_review(
        self, candidate_id: str, *, review_digest: str, accepted: bool
    ) -> ResearchCandidate:
        candidate = self._require(candidate_id, ResearchStage.ETERNIAN_REVIEW)
        if not self._digest_ok(review_digest):
            raise ResearchRejected("independent Ethernian review digest required")
        stage = ResearchStage.IMPROVEMENT_PROPOSED if accepted else ResearchStage.REJECTED
        proposal_digest = (
            sha256(
                (
                    f"{candidate.candidate_id}|{candidate.source.content_digest}|"
                    f"{review_digest}|{candidate.feature_summary}"
                ).encode()
            ).hexdigest()
            if accepted
            else None
        )
        return self._save(
            replace(
                candidate,
                stage=stage,
                eternian_review_digest=review_digest,
                proposal_digest=proposal_digest,
            )
        )

    def apply_or_learn(self) -> None:
        raise ResearchRejected("automatic learning and operational application are forbidden")

    def get(self, candidate_id: str) -> ResearchCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise ResearchRejected("unknown candidate") from exc

    def _require(self, candidate_id: str, stage: ResearchStage) -> ResearchCandidate:
        candidate = self.get(candidate_id)
        if candidate.stage is not stage:
            raise ResearchRejected("invalid research stage")
        return candidate

    def _save(self, candidate: ResearchCandidate) -> ResearchCandidate:
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    @staticmethod
    def _digest_ok(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
