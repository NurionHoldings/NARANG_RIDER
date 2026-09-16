"""Independent professional decision intake without legal auto-approval."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class Discipline(StrEnum):
    COUNSEL_PRIVACY = "counsel_privacy"
    LABOR_INSURANCE = "labor_insurance"
    ACCOUNTING_TAX = "accounting_tax"
    ELECTRONIC_FINANCE = "electronic_finance"
    LOCATION = "location"


class DecisionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    CONDITIONAL = "CONDITIONAL"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


MANDATORY_DISCIPLINES = frozenset(Discipline)
OPAQUE_REF = re.compile(r"signed-approval://[A-Za-z0-9._/-]{12,200}")


class ProfessionalReviewRejected(ValueError):
    """A professional review claim failed a governance boundary."""


@dataclass(frozen=True, slots=True)
class ProfessionalDecision:
    decision_id: str
    discipline: Discipline
    reviewer_identity_ref: str
    signed_approval_ref: str
    scope: str
    system_version: str
    effective_at: datetime
    expires_at: datetime
    status: DecisionStatus
    conditions: tuple[str, ...] = ()
    conflict_refs: tuple[str, ...] = ()
    withdrawn_at: datetime | None = None
    withdrawal_ref: str | None = None


@dataclass(frozen=True, slots=True)
class OperatorAcceptance:
    operator_identity_ref: str
    signed_acceptance_ref: str
    scope: str
    system_version: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class ProfessionalReadiness:
    legal_approval: bool
    release_status: str
    missing_disciplines: tuple[Discipline, ...]
    conditional_disciplines: tuple[Discipline, ...]
    rejected_disciplines: tuple[Discipline, ...]
    reasons: tuple[str, ...]


class ProfessionalDecisionRegistry:
    """Fail-closed registry; decisions remain opinions, not automated legal conclusions."""

    def __init__(self, *, system_version: str, scope: str, now: datetime | None = None) -> None:
        self.system_version = system_version
        self.scope = scope
        self.now = now or datetime.now(UTC)
        self._decisions: dict[Discipline, ProfessionalDecision] = {}

    def record(self, decision: ProfessionalDecision) -> ProfessionalDecision:
        self._validate(decision)
        existing = self._decisions.get(decision.discipline)
        if existing and existing.decision_id == decision.decision_id and existing != decision:
            raise ProfessionalReviewRejected("decision id reuse with changed content")
        self._decisions[decision.discipline] = decision
        return decision

    def withdraw(
        self, discipline: Discipline, *, withdrawn_at: datetime, withdrawal_ref: str
    ) -> ProfessionalDecision:
        current = self._decisions[discipline]
        if not OPAQUE_REF.fullmatch(withdrawal_ref):
            raise ProfessionalReviewRejected("signed opaque withdrawal reference required")
        withdrawn = replace(
            current,
            status=DecisionStatus.WITHDRAWN,
            withdrawn_at=withdrawn_at,
            withdrawal_ref=withdrawal_ref,
        )
        self._decisions[discipline] = withdrawn
        return withdrawn

    def readiness(self, acceptance: OperatorAcceptance | None) -> ProfessionalReadiness:
        missing = tuple(sorted(MANDATORY_DISCIPLINES - self._decisions.keys(), key=str))
        conditional = tuple(
            sorted(
                (
                    discipline
                    for discipline, decision in self._decisions.items()
                    if decision.status is DecisionStatus.CONDITIONAL
                ),
                key=str,
            )
        )
        rejected = tuple(
            sorted(
                (
                    discipline
                    for discipline, decision in self._decisions.items()
                    if decision.status in {DecisionStatus.REJECTED, DecisionStatus.WITHDRAWN}
                ),
                key=str,
            )
        )
        reasons = []
        if missing:
            reasons.append("mandatory independent disciplines are missing")
        if conditional:
            reasons.append("conditional professional decisions remain open")
        if rejected:
            reasons.append("rejected or withdrawn professional decisions exist")
        if any(decision.expires_at <= self.now for decision in self._decisions.values()):
            reasons.append("a professional decision is expired")
        if any(decision.conditions for decision in self._decisions.values()):
            reasons.append("unresolved professional conditions exist")
        reviewers = [decision.reviewer_identity_ref for decision in self._decisions.values()]
        if len(reviewers) != len(set(reviewers)):
            reasons.append("disciplines are not independently reviewed")
        if acceptance is None:
            reasons.append("operator acceptance is missing")
        else:
            if acceptance.scope != self.scope or acceptance.system_version != self.system_version:
                reasons.append("operator acceptance scope or version mismatch")
            if not OPAQUE_REF.fullmatch(acceptance.signed_acceptance_ref):
                reasons.append("operator signed acceptance reference is invalid")
            if acceptance.operator_identity_ref in reviewers:
                reasons.append("operator cannot self-review a professional discipline")
        approved = not reasons and len(self._decisions) == len(MANDATORY_DISCIPLINES)
        return ProfessionalReadiness(
            legal_approval=approved,
            release_status="BLOCKED",
            missing_disciplines=missing,
            conditional_disciplines=conditional,
            rejected_disciplines=rejected,
            reasons=tuple(reasons),
        )

    def _validate(self, decision: ProfessionalDecision) -> None:
        if not decision.reviewer_identity_ref.startswith("professional-registry://"):
            raise ProfessionalReviewRejected("professional registry reference required")
        if not OPAQUE_REF.fullmatch(decision.signed_approval_ref):
            raise ProfessionalReviewRejected("signed opaque approval reference required")
        if decision.scope != self.scope or decision.system_version != self.system_version:
            raise ProfessionalReviewRejected("decision scope or system version mismatch")
        if decision.effective_at.tzinfo is None or decision.expires_at.tzinfo is None:
            raise ProfessionalReviewRejected("timestamps must be timezone-aware")
        if not decision.effective_at <= self.now < decision.expires_at:
            raise ProfessionalReviewRejected("decision is not currently effective")
        if decision.status is DecisionStatus.ACCEPTED and decision.conditions:
            raise ProfessionalReviewRejected("accepted decision cannot retain conditions")
        if decision.status is DecisionStatus.CONDITIONAL and not decision.conditions:
            raise ProfessionalReviewRejected("conditional decision requires conditions")
        if decision.conflict_refs:
            raise ProfessionalReviewRejected("unresolved reviewer conflict")
