from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class ArkaonCapability(StrEnum):
    DEMAND_FORECAST = "DEMAND_FORECAST"
    PREPARATION_TIME_ESTIMATE = "PREPARATION_TIME_ESTIMATE"
    TRAVEL_TIME_ESTIMATE = "TRAVEL_TIME_ESTIMATE"
    RIDER_NET_EARNINGS_EXPLANATION = "RIDER_NET_EARNINGS_EXPLANATION"
    MERCHANT_MARGIN_ANALYSIS = "MERCHANT_MARGIN_ANALYSIS"
    SETTLEMENT_ANOMALY_AUDIT = "SETTLEMENT_ANOMALY_AUDIT"
    OPERATIONS_RECOMMENDATION = "OPERATIONS_RECOMMENDATION"
    CODE_CHANGE_PROPOSAL = "CODE_CHANGE_PROPOSAL"
    TEST_PLAN_PROPOSAL = "TEST_PLAN_PROPOSAL"


class ForbiddenAuthority(StrEnum):
    DISPATCH_EXCLUSION = "DISPATCH_EXCLUSION"
    PAY_REDUCTION = "PAY_REDUCTION"
    ACCOUNT_SUSPENSION = "ACCOUNT_SUSPENSION"
    INSURANCE_PRICE_DIFFERENTIATION = "INSURANCE_PRICE_DIFFERENTIATION"
    AUTOMATIC_COMPLAINT_REJECTION = "AUTOMATIC_COMPLAINT_REJECTION"
    AUTOMATIC_LIABILITY_DECISION = "AUTOMATIC_LIABILITY_DECISION"
    RETALIATORY_DISPATCH = "RETALIATORY_DISPATCH"
    DIRECT_MERGE = "DIRECT_MERGE"
    DIRECT_DEPLOYMENT = "DIRECT_DEPLOYMENT"
    SECRET_ACCESS = "SECRET_ACCESS"


@dataclass(frozen=True)
class ArkaonProfile:
    profile_id: str
    domain: str
    version: int
    capabilities: frozenset[ArkaonCapability]
    forbidden_authorities: frozenset[ForbiddenAuthority]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.domain != "NARANG_RIDER":
            raise ValueError("ARKAON_DOMAIN_MISMATCH")
        if self.version < 1 or self.created_at.tzinfo is None:
            raise ValueError("VERSIONED_ARKAON_PROFILE_REQUIRED")
        if self.forbidden_authorities != frozenset(ForbiddenAuthority):
            raise ValueError("ALL_FORBIDDEN_AUTHORITIES_MUST_BE_LOCKED")


@dataclass(frozen=True)
class EthernianDirective:
    directive_id: str
    profile_id: str
    objective: str
    capability: ArkaonCapability
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    signature: str


class EthernianDirectiveAuthority:
    def __init__(self, *, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("DIRECTIVE_SIGNING_KEY_TOO_SHORT")
        self._key = signing_key

    def issue(
        self,
        *,
        directive_id: str,
        profile: ArkaonProfile,
        objective: str,
        capability: ArkaonCapability,
        allowed_paths: tuple[str, ...],
        acceptance_criteria: tuple[str, ...],
        issued_at: datetime,
        expires_at: datetime,
    ) -> EthernianDirective:
        if capability not in profile.capabilities:
            raise ValueError("CAPABILITY_NOT_GRANTED")
        if expires_at <= issued_at or issued_at.tzinfo is None:
            raise ValueError("DIRECTIVE_EXPIRY_REQUIRED")
        if not objective.strip() or not acceptance_criteria:
            raise ValueError("DIRECTIVE_OBJECTIVE_AND_CRITERIA_REQUIRED")
        normalized_paths = self._validate_paths(allowed_paths)
        signature = self._sign(
            directive_id,
            profile.profile_id,
            objective,
            capability.value,
            ",".join(normalized_paths),
            "\x1e".join(acceptance_criteria),
            issued_at.isoformat(),
            expires_at.isoformat(),
        )
        return EthernianDirective(
            directive_id=directive_id,
            profile_id=profile.profile_id,
            objective=objective,
            capability=capability,
            allowed_paths=normalized_paths,
            acceptance_criteria=acceptance_criteria,
            issued_at=issued_at,
            expires_at=expires_at,
            signature=signature,
        )

    def verify(
        self,
        *,
        directive: EthernianDirective,
        profile: ArkaonProfile,
        now: datetime,
    ) -> None:
        if directive.profile_id != profile.profile_id:
            raise ValueError("DIRECTIVE_PROFILE_MISMATCH")
        if directive.capability not in profile.capabilities:
            raise ValueError("CAPABILITY_NOT_GRANTED")
        if now.tzinfo is None or now >= directive.expires_at:
            raise ValueError("DIRECTIVE_EXPIRED")
        expected = self._sign(
            directive.directive_id,
            directive.profile_id,
            directive.objective,
            directive.capability.value,
            ",".join(directive.allowed_paths),
            "\x1e".join(directive.acceptance_criteria),
            directive.issued_at.isoformat(),
            directive.expires_at.isoformat(),
        )
        if not hmac.compare_digest(expected, directive.signature):
            raise ValueError("INVALID_DIRECTIVE_SIGNATURE")
        self._validate_paths(directive.allowed_paths)

    @staticmethod
    def _validate_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
        if not paths:
            raise ValueError("ALLOWED_PATHS_REQUIRED")
        allowed_roots = ("src/", "tests/", "docs/")
        forbidden_fragments = ("..", ".env", "secret", ".git/", ".github/workflows/")
        normalized = tuple(sorted(set(paths)))
        if any(
            not path.startswith(allowed_roots)
            or path.startswith("/")
            or any(fragment in path.lower() for fragment in forbidden_fragments)
            for path in normalized
        ):
            raise ValueError("UNSAFE_DEVELOPMENT_PATH")
        return normalized

    def _sign(self, *values: str) -> str:
        return hmac.new(
            self._key,
            "\x1f".join(values).encode(),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True)
class CodeChangeProposal:
    proposal_id: str
    directive_id: str
    profile_id: str
    touched_paths: tuple[str, ...]
    change_digest: str
    summary: str
    test_commands: tuple[str, ...]
    created_at: datetime
    status: str = "AWAITING_ETHERNIAN_REVIEW"
    may_merge_or_deploy: bool = False


class ArkaonDevelopmentCoordinator:
    """Allows bounded proposals, never repository writes, merge, deployment, or secrets."""

    def __init__(
        self,
        *,
        profile: ArkaonProfile,
        authority: EthernianDirectiveAuthority,
    ) -> None:
        self._profile = profile
        self._authority = authority
        self._proposals: dict[str, CodeChangeProposal] = {}

    def propose_change(
        self,
        *,
        proposal_id: str,
        directive: EthernianDirective,
        touched_paths: tuple[str, ...],
        change_digest: str,
        summary: str,
        test_commands: tuple[str, ...],
        now: datetime,
    ) -> CodeChangeProposal:
        existing = self._proposals.get(proposal_id)
        if existing is not None:
            if existing.change_digest != change_digest:
                raise ValueError("PROPOSAL_IDEMPOTENCY_CONFLICT")
            return existing
        self._authority.verify(directive=directive, profile=self._profile, now=now)
        if directive.capability is not ArkaonCapability.CODE_CHANGE_PROPOSAL:
            raise ValueError("CODE_CHANGE_CAPABILITY_REQUIRED")
        paths = tuple(sorted(set(touched_paths)))
        if not paths or any(path not in directive.allowed_paths for path in paths):
            raise ValueError("PROPOSAL_OUTSIDE_DIRECTIVE_SCOPE")
        if len(change_digest) != 64 or any(character not in "0123456789abcdef" for character in change_digest):
            raise ValueError("VALID_CHANGE_DIGEST_REQUIRED")
        allowed_tests = ("pytest", "ruff check", "git diff --check")
        if not test_commands or any(
            not command.startswith(allowed_tests) for command in test_commands
        ):
            raise ValueError("UNSAFE_TEST_COMMAND")
        proposal = CodeChangeProposal(
            proposal_id=proposal_id,
            directive_id=directive.directive_id,
            profile_id=self._profile.profile_id,
            touched_paths=paths,
            change_digest=change_digest,
            summary=summary,
            test_commands=test_commands,
            created_at=now,
        )
        self._proposals[proposal_id] = proposal
        return proposal

    @staticmethod
    def assert_authority_allowed(requested_authority: str) -> None:
        try:
            forbidden = ForbiddenAuthority(requested_authority)
        except ValueError:
            return
        raise ValueError(f"ARKAON_FORBIDDEN_AUTHORITY:{forbidden.value}")


class CandidateStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ETHERNIAN_APPROVED = "ETHERNIAN_APPROVED"
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    profile_id: str
    capability: ArkaonCapability
    artifact_digest: str
    evidence_refs: tuple[str, ...]
    evaluation_score: int
    privacy_passed: bool
    fairness_passed: bool
    security_passed: bool
    created_at: datetime
    status: CandidateStatus = CandidateStatus.PROPOSED
    ethernian_reviewer: str | None = None
    operator_approver: str | None = None
    rejection_reason: str | None = None


class ArkaonEvolutionRegistry:
    """Promotes evaluated assets through independent review and operator approval."""

    def __init__(self) -> None:
        self._candidates: dict[str, EvolutionCandidate] = {}
        self._active_by_capability: dict[ArkaonCapability, str] = {}

    def register(self, candidate: EvolutionCandidate) -> EvolutionCandidate:
        if candidate.candidate_id in self._candidates:
            raise ValueError("DUPLICATE_EVOLUTION_CANDIDATE")
        if (
            candidate.evaluation_score < 0
            or candidate.evaluation_score > 100
            or not candidate.evidence_refs
            or len(candidate.artifact_digest) != 64
        ):
            raise ValueError("INVALID_EVOLUTION_EVIDENCE")
        self._candidates[candidate.candidate_id] = candidate
        return candidate

    def ethernian_review(
        self,
        *,
        candidate_id: str,
        reviewer: str,
        approve: bool,
        rejection_reason: str | None = None,
    ) -> EvolutionCandidate:
        candidate = self.get(candidate_id)
        if candidate.status is not CandidateStatus.PROPOSED:
            raise ValueError("INVALID_EVOLUTION_REVIEW_STATE")
        gates_passed = (
            candidate.evaluation_score >= 80
            and candidate.privacy_passed
            and candidate.fairness_passed
            and candidate.security_passed
        )
        if approve and not gates_passed:
            raise ValueError("EVOLUTION_GATES_NOT_PASSED")
        if not approve and not rejection_reason:
            raise ValueError("REJECTION_REASON_REQUIRED")
        reviewed = replace(
            candidate,
            status=(
                CandidateStatus.ETHERNIAN_APPROVED if approve else CandidateStatus.REJECTED
            ),
            ethernian_reviewer=reviewer,
            rejection_reason=rejection_reason,
        )
        self._candidates[candidate_id] = reviewed
        return reviewed

    def operator_approve(
        self,
        *,
        candidate_id: str,
        operator: str,
    ) -> EvolutionCandidate:
        candidate = self.get(candidate_id)
        if candidate.status is not CandidateStatus.ETHERNIAN_APPROVED:
            raise ValueError("ETHERNIAN_APPROVAL_REQUIRED")
        approved = replace(
            candidate,
            status=CandidateStatus.OPERATOR_APPROVED,
            operator_approver=operator,
        )
        self._candidates[candidate_id] = approved
        return approved

    def activate(self, *, candidate_id: str) -> EvolutionCandidate:
        candidate = self.get(candidate_id)
        if candidate.status is not CandidateStatus.OPERATOR_APPROVED:
            raise ValueError("OPERATOR_APPROVAL_REQUIRED")
        active = replace(candidate, status=CandidateStatus.ACTIVE)
        self._candidates[candidate_id] = active
        self._active_by_capability[candidate.capability] = candidate_id
        return active

    def rollback(self, *, candidate_id: str, reason: str) -> EvolutionCandidate:
        candidate = self.get(candidate_id)
        if candidate.status is not CandidateStatus.ACTIVE or not reason.strip():
            raise ValueError("ACTIVE_CANDIDATE_AND_REASON_REQUIRED")
        rolled_back = replace(
            candidate,
            status=CandidateStatus.ROLLED_BACK,
            rejection_reason=reason,
        )
        self._candidates[candidate_id] = rolled_back
        if self._active_by_capability.get(candidate.capability) == candidate_id:
            self._active_by_capability.pop(candidate.capability)
        return rolled_back

    def get(self, candidate_id: str) -> EvolutionCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise ValueError("EVOLUTION_CANDIDATE_NOT_FOUND") from exc
