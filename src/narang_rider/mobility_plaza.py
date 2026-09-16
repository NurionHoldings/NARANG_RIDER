"""Fail-closed governance for the synthetic rider mobility plaza."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit


class PlazaRejected(ValueError):
    pass


class PlazaVertical(StrEnum):
    INSTALLMENT_FINANCE = "installment_finance"
    INSURANCE = "insurance"
    MOTORCYCLE = "motorcycle"
    RIDING_GEAR = "riding_gear"
    MAINTENANCE = "maintenance"
    RENTAL = "rental"


class PartnerStage(StrEnum):
    DRAFT = "draft"
    EVIDENCE_REVIEW = "evidence_review"
    CONTRACT_TEST = "contract_test"
    ETHERNIAN_REVIEW = "ethernian_review"
    OPERATOR_DECISION = "operator_decision"
    SANDBOX_ENABLED = "sandbox_enabled"
    SUSPENDED = "suspended"
    OFFBOARDED = "offboarded"


class ListingStage(StrEnum):
    DRAFT = "draft"
    HUMAN_REVIEW = "human_review"
    SANDBOX_VISIBLE = "sandbox_visible"
    WITHDRAWN = "withdrawn"


class MonitoringVerdict(StrEnum):
    PASS_FOR_HUMAN_REVIEW = "pass_for_human_review"
    HUMAN_REVIEW = "human_review"
    BLOCKED = "blocked"


REGULATED_VERTICALS = frozenset({PlazaVertical.INSTALLMENT_FINANCE, PlazaVertical.INSURANCE})
FORBIDDEN_ARKAON_ACTIONS = frozenset(
    {
        "APPROVE_CREDIT",
        "PRICE_CREDIT",
        "UNDERWRITE_INSURANCE",
        "CONCLUDE_SUITABILITY",
        "BIND_CONTRACT",
        "MOVE_MONEY",
        "AUTO_SANCTION",
        "AUTO_RANK_BY_COMMISSION",
        "MERGE_PLATFORM_PERSONAL_DATA",
        "CONTACT_EXTERNAL_PARTNER",
        "ACTIVATE_PRODUCTION",
    }
)


def _digest_ok(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


@dataclass(frozen=True)
class AuthorityEvidence:
    evidence_id: str
    source_url: str
    title: str
    authority: str
    accessed_at: datetime
    expires_at: datetime
    content_digest: str
    legal_interpretation_approved: bool = False

    def valid(self, now: datetime) -> bool:
        parsed = urlsplit(self.source_url)
        return (
            parsed.scheme == "https"
            and bool(parsed.netloc)
            and self.authority in {"LAW_GO_KR", "FSC", "FSS", "FTC", "PIPC", "KISA"}
            and self.accessed_at.tzinfo is not None
            and self.accessed_at <= now < self.expires_at
            and _digest_ok(self.content_digest)
        )


@dataclass(frozen=True)
class PartnerApplication:
    partner_id: str
    legal_name: str
    verticals: frozenset[PlazaVertical]
    license_evidence_ref: str | None
    contract_digest: str
    disclosure_digest: str
    sandbox_endpoint: str
    synthetic_only: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.sandbox_endpoint)
        if not self.partner_id or not self.legal_name or not self.verticals:
            raise PlazaRejected("complete partner identity and vertical scope required")
        if not _digest_ok(self.contract_digest) or not _digest_ok(self.disclosure_digest):
            raise PlazaRejected("partner contract and disclosure digests required")
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".invalid"):
            raise PlazaRejected("synthetic .invalid sandbox endpoint required")
        if not self.synthetic_only:
            raise PlazaRejected("operational partner onboarding is forbidden")
        if self.verticals & REGULATED_VERTICALS and not self.license_evidence_ref:
            raise PlazaRejected("regulated partner license evidence required")


@dataclass(frozen=True)
class PartnerRecord:
    application: PartnerApplication
    stage: PartnerStage = PartnerStage.DRAFT
    evidence_digest: str | None = None
    contract_test_digest: str | None = None
    ethernian_review_ref: str | None = None
    operator_decision_ref: str | None = None
    reason: str | None = None


class PartnerGovernance:
    """Controls onboarding without granting ARKAON approval or activation authority."""

    def __init__(self) -> None:
        self._records: dict[str, PartnerRecord] = {}

    def register(self, application: PartnerApplication) -> PartnerRecord:
        if application.partner_id in self._records:
            raise PlazaRejected("duplicate partner")
        record = PartnerRecord(application)
        self._records[application.partner_id] = record
        return record

    def submit_evidence(self, partner_id: str, evidence_digest: str) -> PartnerRecord:
        record = self._require(partner_id, PartnerStage.DRAFT)
        if not _digest_ok(evidence_digest):
            raise PlazaRejected("valid evidence digest required")
        return self._store(replace(record, stage=PartnerStage.EVIDENCE_REVIEW, evidence_digest=evidence_digest))

    def accept_contract_test(self, partner_id: str, report_digest: str) -> PartnerRecord:
        record = self._require(partner_id, PartnerStage.EVIDENCE_REVIEW)
        if not _digest_ok(report_digest):
            raise PlazaRejected("passing contract test digest required")
        return self._store(replace(record, stage=PartnerStage.CONTRACT_TEST, contract_test_digest=report_digest))

    def ethernian_review(self, partner_id: str, review_ref: str) -> PartnerRecord:
        record = self._require(partner_id, PartnerStage.CONTRACT_TEST)
        if not review_ref.strip():
            raise PlazaRejected("ethernian review reference required")
        return self._store(replace(record, stage=PartnerStage.ETHERNIAN_REVIEW, ethernian_review_ref=review_ref))

    def operator_decide(self, partner_id: str, decision_ref: str) -> PartnerRecord:
        record = self._require(partner_id, PartnerStage.ETHERNIAN_REVIEW)
        if not decision_ref.strip() or decision_ref == record.ethernian_review_ref:
            raise PlazaRejected("separate operator decision required")
        return self._store(replace(record, stage=PartnerStage.OPERATOR_DECISION, operator_decision_ref=decision_ref))

    def enable_sandbox(self, partner_id: str) -> PartnerRecord:
        record = self._require(partner_id, PartnerStage.OPERATOR_DECISION)
        return self._store(replace(record, stage=PartnerStage.SANDBOX_ENABLED))

    def suspend(self, partner_id: str, reason: str) -> PartnerRecord:
        record = self.get(partner_id)
        if record.stage not in {PartnerStage.SANDBOX_ENABLED, PartnerStage.OPERATOR_DECISION} or not reason.strip():
            raise PlazaRejected("suspendable stage and reason required")
        return self._store(replace(record, stage=PartnerStage.SUSPENDED, reason=reason))

    def offboard(self, partner_id: str, operator_ref: str) -> PartnerRecord:
        record = self.get(partner_id)
        if record.stage is not PartnerStage.SUSPENDED or not operator_ref.strip():
            raise PlazaRejected("operator-confirmed suspended partner required")
        return self._store(replace(record, stage=PartnerStage.OFFBOARDED))

    def get(self, partner_id: str) -> PartnerRecord:
        try:
            return self._records[partner_id]
        except KeyError as exc:
            raise PlazaRejected("partner not found") from exc

    def _require(self, partner_id: str, stage: PartnerStage) -> PartnerRecord:
        record = self.get(partner_id)
        if record.stage is not stage:
            raise PlazaRejected(f"expected partner stage {stage.value}")
        return record

    def _store(self, record: PartnerRecord) -> PartnerRecord:
        self._records[record.application.partner_id] = record
        return record


@dataclass(frozen=True)
class PlazaListing:
    listing_id: str
    partner_id: str
    vertical: PlazaVertical
    title: str
    terms_digest: str
    evidence_refs: tuple[str, ...]
    commission_bps: int
    sponsored: bool
    stage: ListingStage = ListingStage.DRAFT
    external_contract_only: bool = True
    real_application_allowed: bool = False
    production_visible: bool = False

    def __post_init__(self) -> None:
        if not self.listing_id or not self.partner_id or not self.title.strip():
            raise PlazaRejected("complete listing identity required")
        if not _digest_ok(self.terms_digest) or not self.evidence_refs:
            raise PlazaRejected("listing terms and evidence required")
        if not 0 <= self.commission_bps <= 10_000:
            raise PlazaRejected("commission basis points out of range")
        if not self.external_contract_only or self.real_application_allowed or self.production_visible:
            raise PlazaRejected("only non-activating external-contract sandbox listings are allowed")


@dataclass(frozen=True)
class ComparisonFactor:
    name: str
    normalized_value: int
    weight: int

    def __post_init__(self) -> None:
        if not self.name or not 0 <= self.normalized_value <= 100 or not 0 <= self.weight <= 100:
            raise PlazaRejected("bounded comparison factor required")


@dataclass(frozen=True)
class ComparisonCandidate:
    listing: PlazaListing
    factors: tuple[ComparisonFactor, ...]


@dataclass(frozen=True)
class ComparisonResult:
    listing_id: str
    score: int
    sponsored: bool
    commission_bps: int
    explanation: tuple[str, ...]
    external_contract_only: bool = True


def compare_listings(candidates: tuple[ComparisonCandidate, ...]) -> tuple[ComparisonResult, ...]:
    if not candidates:
        raise PlazaRejected("comparison candidates required")
    results: list[ComparisonResult] = []
    for candidate in candidates:
        if candidate.listing.stage is not ListingStage.SANDBOX_VISIBLE or not candidate.factors:
            raise PlazaRejected("only reviewed sandbox listings may be compared")
        total_weight = sum(factor.weight for factor in candidate.factors)
        if total_weight != 100:
            raise PlazaRejected("comparison factor weights must total 100")
        score = sum(factor.normalized_value * factor.weight for factor in candidate.factors) // 100
        results.append(
            ComparisonResult(
                candidate.listing.listing_id,
                score,
                candidate.listing.sponsored,
                candidate.listing.commission_bps,
                tuple(f"{factor.name}:{factor.normalized_value}:{factor.weight}" for factor in candidate.factors),
            )
        )
    return tuple(sorted(results, key=lambda item: (-item.score, item.listing_id)))


@dataclass(frozen=True)
class DataBoundaryRequest:
    source_platform: str
    target_platform: str
    categories: frozenset[str]
    purpose: str
    consent_ref: str | None = None

    def validate(self) -> None:
        prohibited = {"identity", "order", "location", "settlement", "credit", "insurance"}
        if self.source_platform != self.target_platform and self.categories & prohibited:
            raise PlazaRejected("cross-platform personal or regulated data mixing forbidden")
        if self.categories and not self.purpose.strip():
            raise PlazaRejected("specific processing purpose required")


@dataclass(frozen=True)
class ArkaonObservation:
    observation_id: str
    partner_id: str
    listing_id: str | None
    code: str
    severity: str
    evidence_digest: str
    created_at: datetime
    proposed_action: str = "HUMAN_REVIEW"

    def __post_init__(self) -> None:
        if self.proposed_action in FORBIDDEN_ARKAON_ACTIONS:
            raise PlazaRejected("ARKAON forbidden authority")
        if self.proposed_action not in {"HUMAN_REVIEW", "REQUEST_EVIDENCE", "PROPOSE_SUSPENSION"}:
            raise PlazaRejected("unsupported ARKAON observation action")
        if self.severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} or not _digest_ok(self.evidence_digest):
            raise PlazaRejected("bounded severity and evidence digest required")
        if self.created_at.tzinfo is None:
            raise PlazaRejected("timezone-aware observation required")


@dataclass(frozen=True)
class MonitoringSnapshot:
    partner_id: str
    listing_id: str | None
    policy_digest: str
    source_digests: tuple[str, ...]
    contract_test_digest: str
    observations: tuple[ArkaonObservation, ...]
    generated_at: datetime
    verdict: MonitoringVerdict
    blockers: tuple[str, ...]
    snapshot_digest: str
    requires_human_review: bool = True
    automatic_enforcement_allowed: bool = False
    production_change_allowed: bool = False

    def as_ci_artifact(self) -> dict[str, Any]:
        return {
            "schema": "narang.arkaon.mobility-plaza-monitor.v1",
            **self.__dict__,
            "verdict": self.verdict.value,
            "observations": [observation.__dict__ for observation in self.observations],
        }


class ArkaonPlazaSupervisor:
    """Observes evidence and drift from design onward; never executes outcomes."""

    def assess(
        self,
        *,
        partner: PartnerRecord,
        listing: PlazaListing | None,
        authorities: tuple[AuthorityEvidence, ...],
        policy_digest: str,
        contract_test_digest: str,
        observations: tuple[ArkaonObservation, ...],
        now: datetime,
    ) -> MonitoringSnapshot:
        blockers: list[str] = []
        if partner.stage is not PartnerStage.SANDBOX_ENABLED:
            blockers.append("partner_not_sandbox_enabled")
        if listing is not None and (
            listing.partner_id != partner.application.partner_id
            or listing.stage is not ListingStage.SANDBOX_VISIBLE
        ):
            blockers.append("listing_not_reviewed_or_partner_mismatch")
        if not _digest_ok(policy_digest) or not _digest_ok(contract_test_digest):
            blockers.append("invalid_policy_or_contract_test_digest")
        if not authorities:
            blockers.append("authority_evidence_missing")
        for evidence in authorities:
            if not evidence.valid(now):
                blockers.append(f"authority_invalid:{evidence.evidence_id}")
            if not evidence.legal_interpretation_approved:
                blockers.append(f"legal_review_pending:{evidence.evidence_id}")
        if any(item.severity == "CRITICAL" for item in observations):
            blockers.append("critical_observation")
        verdict = (
            MonitoringVerdict.BLOCKED
            if any(item.startswith(("authority_invalid", "critical_")) for item in blockers)
            else MonitoringVerdict.HUMAN_REVIEW
            if blockers or observations
            else MonitoringVerdict.PASS_FOR_HUMAN_REVIEW
        )
        canonical = {
            "partner": partner.application.partner_id,
            "listing": listing.listing_id if listing else None,
            "partner_contract": partner.application.contract_digest,
            "partner_disclosure": partner.application.disclosure_digest,
            "listing_terms": listing.terms_digest if listing else None,
            "policy": policy_digest,
            "sources": sorted(item.content_digest for item in authorities),
            "contract": contract_test_digest,
            "observations": sorted((item.observation_id, item.evidence_digest) for item in observations),
            "blockers": blockers,
            "verdict": verdict.value,
            "generated_at": now.isoformat(),
        }
        snapshot_digest = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return MonitoringSnapshot(
            partner.application.partner_id,
            listing.listing_id if listing else None,
            policy_digest,
            tuple(sorted(item.content_digest for item in authorities)),
            contract_test_digest,
            observations,
            now,
            verdict,
            tuple(blockers),
            snapshot_digest,
        )


@dataclass
class ListingGovernance:
    listings: dict[str, PlazaListing] = field(default_factory=dict)

    def register(self, listing: PlazaListing, partner: PartnerRecord) -> PlazaListing:
        if listing.listing_id in self.listings:
            raise PlazaRejected("duplicate listing")
        if partner.stage is not PartnerStage.SANDBOX_ENABLED or listing.partner_id != partner.application.partner_id:
            raise PlazaRejected("sandbox-enabled matching partner required")
        if listing.vertical not in partner.application.verticals:
            raise PlazaRejected("listing outside partner vertical scope")
        self.listings[listing.listing_id] = listing
        return listing

    def submit_for_review(self, listing_id: str) -> PlazaListing:
        return self._transition(listing_id, ListingStage.DRAFT, ListingStage.HUMAN_REVIEW)

    def expose_in_sandbox(self, listing_id: str, operator_ref: str) -> PlazaListing:
        if not operator_ref.strip():
            raise PlazaRejected("operator reference required")
        return self._transition(listing_id, ListingStage.HUMAN_REVIEW, ListingStage.SANDBOX_VISIBLE)

    def withdraw(self, listing_id: str, reason: str) -> PlazaListing:
        current = self._get(listing_id)
        if current.stage is ListingStage.WITHDRAWN or not reason.strip():
            raise PlazaRejected("active listing and reason required")
        updated = replace(current, stage=ListingStage.WITHDRAWN)
        self.listings[listing_id] = updated
        return updated

    def _transition(self, listing_id: str, expected: ListingStage, target: ListingStage) -> PlazaListing:
        current = self._get(listing_id)
        if current.stage is not expected:
            raise PlazaRejected(f"expected listing stage {expected.value}")
        updated = replace(current, stage=target)
        self.listings[listing_id] = updated
        return updated

    def _get(self, listing_id: str) -> PlazaListing:
        try:
            return self.listings[listing_id]
        except KeyError as exc:
            raise PlazaRejected("listing not found") from exc
