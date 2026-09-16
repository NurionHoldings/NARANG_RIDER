"""Fail-closed map-provider failover, quota, and cost-budget planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256


class ResilienceRejected(ValueError):
    pass


class PlanStatus(StrEnum):
    PREFERRED_AVAILABLE = "preferred_available"
    RIDER_CHOICE_REQUIRED = "rider_choice_required"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProviderBudgetPolicy:
    provider_id: str
    priority: int
    capabilities: frozenset[str]
    monthly_budget_won: int
    daily_quota: int
    protected_reserve_requests: int
    estimated_request_cost_won: int
    circuit_failure_threshold: int
    circuit_cooldown_seconds: int
    evidence_expires_at: datetime
    certified: bool
    motorcycle_verified: bool
    synthetic_only: bool = True

    def validate(self) -> None:
        if not self.provider_id or self.priority < 0:
            raise ResilienceRejected("valid provider identity and priority required")
        numeric = (
            self.monthly_budget_won,
            self.daily_quota,
            self.protected_reserve_requests,
            self.estimated_request_cost_won,
            self.circuit_failure_threshold,
            self.circuit_cooldown_seconds,
        )
        if any(value < 0 for value in numeric):
            raise ResilienceRejected("negative budget or resilience value")
        if (
            self.daily_quota <= self.protected_reserve_requests
            or self.circuit_failure_threshold < 1
            or self.circuit_cooldown_seconds < 1
        ):
            raise ResilienceRejected("unsafe quota reserve or circuit threshold")
        if not self.synthetic_only:
            raise ResilienceRejected("live provider policy is forbidden before external approval")


@dataclass
class ProviderUsage:
    requests_today: int = 0
    spend_this_month_won: int = 0
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    reservations: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderPlan:
    requested_provider_id: str
    selected_provider_id: str | None
    capability: str
    status: PlanStatus
    reason: str
    estimated_cost_won: int
    remaining_quota_after_reservation: int | None
    remaining_budget_after_reservation_won: int | None
    idempotency_digest: str
    provider_call_allowed: bool = False
    automatic_app_switch_allowed: bool = False
    automatic_pay_change_allowed: bool = False
    automatic_rider_penalty_allowed: bool = False
    production_activation_allowed: bool = False

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.map-provider-resilience-plan.v1", **self.__dict__}


class MapProviderResiliencePlanner:
    def __init__(self, policies: tuple[ProviderBudgetPolicy, ...]) -> None:
        if not policies or len({item.provider_id for item in policies}) != len(policies):
            raise ResilienceRejected("unique provider policies required")
        for item in policies:
            item.validate()
        self._policies = {item.provider_id: item for item in policies}
        self._usage = {item.provider_id: ProviderUsage() for item in policies}

    def plan(
        self,
        *,
        requested_provider_id: str,
        capability: str,
        idempotency_key: str,
        now: datetime,
        allow_protected_reserve: bool = False,
    ) -> ProviderPlan:
        if not capability or not idempotency_key:
            raise ResilienceRejected("capability and idempotency key required")
        digest = sha256(
            f"{requested_provider_id}|{capability}|{idempotency_key}".encode()
        ).hexdigest()
        requested = self._policies.get(requested_provider_id)
        if requested is None:
            return self._blocked(requested_provider_id, capability, digest, "unknown_provider")

        preferred_reason = self._ineligible_reason(
            requested, capability, now, allow_protected_reserve
        )
        if preferred_reason is None:
            return self._reserve(
                requested,
                requested_provider_id,
                capability,
                digest,
                PlanStatus.PREFERRED_AVAILABLE,
                "preferred_provider_within_budget",
            )

        candidates = tuple(
            item
            for item in sorted(self._policies.values(), key=lambda policy: policy.priority)
            if item.provider_id != requested_provider_id
            and self._ineligible_reason(item, capability, now, allow_protected_reserve) is None
        )
        if not candidates:
            return self._blocked(
                requested_provider_id,
                capability,
                digest,
                f"preferred_{preferred_reason}_no_certified_fallback",
            )
        fallback = candidates[0]
        plan = self._reserve(
            fallback,
            requested_provider_id,
            capability,
            digest,
            PlanStatus.RIDER_CHOICE_REQUIRED,
            f"preferred_{preferred_reason}_fallback_requires_explicit_choice",
        )
        return plan

    def record_result(
        self, provider_id: str, *, succeeded: bool, now: datetime
    ) -> None:
        policy = self._policies.get(provider_id)
        if policy is None:
            raise ResilienceRejected("unknown provider result")
        usage = self._usage[provider_id]
        if succeeded:
            usage.consecutive_failures = 0
            usage.circuit_open_until = None
            return
        usage.consecutive_failures += 1
        if usage.consecutive_failures >= policy.circuit_failure_threshold:
            usage.circuit_open_until = now + timedelta(
                seconds=policy.circuit_cooldown_seconds
            )

    def usage(self, provider_id: str) -> ProviderUsage:
        try:
            return self._usage[provider_id]
        except KeyError as exc:
            raise ResilienceRejected("unknown provider") from exc

    def _ineligible_reason(
        self,
        policy: ProviderBudgetPolicy,
        capability: str,
        now: datetime,
        allow_protected_reserve: bool,
    ) -> str | None:
        usage = self._usage[policy.provider_id]
        if not policy.certified or now >= policy.evidence_expires_at:
            return "uncertified_or_stale"
        if capability not in policy.capabilities:
            return "capability_missing"
        if capability == "motorcycle_route" and not policy.motorcycle_verified:
            return "motorcycle_unverified"
        if usage.circuit_open_until is not None and usage.circuit_open_until > now:
            return "circuit_open"
        usable_quota = policy.daily_quota
        if not allow_protected_reserve:
            usable_quota -= policy.protected_reserve_requests
        if usage.requests_today >= usable_quota:
            return "quota_exhausted"
        if (
            usage.spend_this_month_won + policy.estimated_request_cost_won
            > policy.monthly_budget_won
        ):
            return "budget_exhausted"
        return None

    def _reserve(
        self,
        policy: ProviderBudgetPolicy,
        requested_provider_id: str,
        capability: str,
        digest: str,
        status: PlanStatus,
        reason: str,
    ) -> ProviderPlan:
        usage = self._usage[policy.provider_id]
        if digest not in usage.reservations:
            usage.reservations[digest] = policy.estimated_request_cost_won
            usage.requests_today += 1
            usage.spend_this_month_won += policy.estimated_request_cost_won
        return ProviderPlan(
            requested_provider_id=requested_provider_id,
            selected_provider_id=policy.provider_id,
            capability=capability,
            status=status,
            reason=reason,
            estimated_cost_won=policy.estimated_request_cost_won,
            remaining_quota_after_reservation=policy.daily_quota - usage.requests_today,
            remaining_budget_after_reservation_won=(
                policy.monthly_budget_won - usage.spend_this_month_won
            ),
            idempotency_digest=digest,
        )

    @staticmethod
    def _blocked(
        requested_provider_id: str,
        capability: str,
        digest: str,
        reason: str,
    ) -> ProviderPlan:
        return ProviderPlan(
            requested_provider_id=requested_provider_id,
            selected_provider_id=None,
            capability=capability,
            status=PlanStatus.BLOCKED,
            reason=reason,
            estimated_cost_won=0,
            remaining_quota_after_reservation=None,
            remaining_budget_after_reservation_won=None,
            idempotency_digest=digest,
        )
