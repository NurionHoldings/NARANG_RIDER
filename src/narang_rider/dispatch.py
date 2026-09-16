from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .pricing import PublicQuote


class DecisionSource(StrEnum):
    PUBLIC_RULE = "PUBLIC_RULE"
    HUMAN_REVIEW = "HUMAN_REVIEW"


PROHIBITED_DECISION_FEATURES = frozenset(
    {
        "accepted_offer_rate",
        "declined_offer_count",
        "past_safety_stop",
        "insurance_claim_history",
        "sex",
        "age",
        "nationality",
        "disability",
        "precise_home_location",
        "ai_risk_score",
        "ai_worker_rank",
    }
)


@dataclass(frozen=True)
class DispatchCandidate:
    rider_id: str
    eligible: bool
    available_since_epoch_ms: int
    distance_to_pickup_m: int
    safety_eligible: bool = True

    def __post_init__(self) -> None:
        if not self.rider_id.strip() or min(
            self.available_since_epoch_ms, self.distance_to_pickup_m
        ) < 0:
            raise ValueError("INVALID_DISPATCH_CANDIDATE")


@dataclass(frozen=True)
class DispatchReceipt:
    selected_rider_id: str
    candidate_ids: tuple[str, ...]
    policy_id: str
    quote_policy_id: str
    decision_source: DecisionSource
    used_features: tuple[str, ...]
    explanation_codes: tuple[str, ...]


class FairDispatchPolicy:
    """Reproducible FIFO selection; AI predictions may inform quotes, never worker rank."""

    ALLOWED_FEATURES = frozenset(
        {"eligibility", "safety_eligibility", "available_since", "distance_to_pickup"}
    )
    MAX_CANDIDATES_PER_OFFER = 5_000

    def __init__(self, policy_id: str) -> None:
        if not policy_id.strip():
            raise ValueError("DISPATCH_POLICY_ID_REQUIRED")
        self.policy_id = policy_id

    def select(
        self,
        candidates: tuple[DispatchCandidate, ...],
        quote: PublicQuote,
        *,
        requested_features: tuple[str, ...] = (),
    ) -> DispatchReceipt:
        if len(candidates) > self.MAX_CANDIDATES_PER_OFFER:
            raise ValueError("DISPATCH_CANDIDATE_LIMIT_EXCEEDED")
        features = set(requested_features) or set(self.ALLOWED_FEATURES)
        prohibited = features & PROHIBITED_DECISION_FEATURES
        unknown = features - self.ALLOWED_FEATURES
        if prohibited or unknown:
            raise ValueError("PROHIBITED_OR_UNKNOWN_DISPATCH_FEATURE")
        identities = [candidate.rider_id for candidate in candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("DUPLICATE_DISPATCH_CANDIDATE")
        eligible = tuple(
            candidate
            for candidate in candidates
            if candidate.eligible and candidate.safety_eligible
        )
        if not eligible:
            raise ValueError("NO_ELIGIBLE_RIDER")
        selected = min(
            eligible,
            key=lambda item: (
                item.available_since_epoch_ms,
                item.distance_to_pickup_m,
                item.rider_id,
            ),
        )
        return DispatchReceipt(
            selected_rider_id=selected.rider_id,
            candidate_ids=tuple(sorted(identities)),
            policy_id=self.policy_id,
            quote_policy_id=quote.policy_id,
            decision_source=DecisionSource.PUBLIC_RULE,
            used_features=tuple(sorted(features)),
            explanation_codes=("ELIGIBLE", "SAFE_TO_OFFER", "LONGEST_AVAILABLE_FIRST"),
        )
