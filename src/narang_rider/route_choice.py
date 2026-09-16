"""Provider-neutral route choice with rider authority and pay protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256


class RouteObjective(StrEnum):
    SAFEST = "safest"
    BALANCED = "balanced"
    LOWEST_COST = "lowest_cost"
    SHORTEST = "shortest"


class Verification(StrEnum):
    VERIFIED = "verified"
    UNKNOWN = "unknown"


class RouteRejected(ValueError):
    pass


@dataclass(frozen=True)
class HazardAdvisory:
    category: str
    severity: int
    official_source_url: str
    source_digest: str
    observed_at: datetime
    expires_at: datetime

    def valid(self, now: datetime) -> bool:
        return (
            self.official_source_url.startswith("https://")
            and bool(self.source_digest)
            and 0 <= self.severity <= 5
            and self.observed_at <= now < self.expires_at
        )


@dataclass(frozen=True)
class RouteLeg:
    order_ref: str
    public_leg_ref: str
    distance_m: int
    duration_s: int
    toll_won: int
    ferry: bool
    restriction_summary: str
    motorcycle_capability: Verification
    uncertainty_s: int


@dataclass(frozen=True)
class RouteAlternative:
    route_id: str
    provider_id: str
    provider_quote_ref: str
    quoted_at: datetime
    expires_at: datetime
    legs: tuple[RouteLeg, ...]
    hazards: tuple[HazardAdvisory, ...] = ()
    return_to_zone_m: int = 0
    return_to_zone_s: int = 0

    @property
    def distance_m(self) -> int:
        return sum(leg.distance_m for leg in self.legs)

    @property
    def duration_s(self) -> int:
        return sum(leg.duration_s for leg in self.legs)

    @property
    def toll_won(self) -> int:
        return sum(leg.toll_won for leg in self.legs)


@dataclass(frozen=True)
class RiderCostInputs:
    won_per_km: int
    battery_or_fuel_label: str


@dataclass(frozen=True)
class AcceptedPay:
    accepted_won: int
    included_distance_m: int
    included_wait_s: int


@dataclass(frozen=True)
class RouteExplanation:
    route_id: str
    objective: RouteObjective
    reasons: tuple[str, ...]
    uncertainty_s: int
    estimated_rider_cost_won: int
    supplemental_review_won: int
    accepted_pay_floor_won: int
    rider_must_choose: bool = True
    tracking_allowed: bool = False
    deviation_is_misconduct: bool = False


@dataclass(frozen=True)
class OfflineInstruction:
    session_token: str
    expires_at: datetime
    stores_address_coordinates_or_tiles: bool = False
    auto_select_route: bool = False


class RouteChoiceService:
    MAX_QUOTE_AGE = timedelta(minutes=5)
    MAX_DETOUR_RATIO = 0.35

    def evaluate(
        self,
        alternatives: tuple[RouteAlternative, ...],
        objective: RouteObjective,
        costs: RiderCostInputs,
        accepted: AcceptedPay,
        now: datetime,
    ) -> tuple[RouteExplanation, ...]:
        if not alternatives or costs.won_per_km < 0 or accepted.accepted_won < 0:
            raise RouteRejected("invalid route economics")
        valid = tuple(self._validate(route, now) for route in alternatives)
        ranked = sorted(valid, key=lambda route: self._score(route, objective))
        return tuple(self._explain(route, objective, costs, accepted) for route in ranked)

    def _validate(self, route: RouteAlternative, now: datetime) -> RouteAlternative:
        if not route.provider_id or not route.provider_quote_ref or not route.legs:
            raise RouteRejected("provider provenance and legs are required")
        if now >= route.expires_at or now - route.quoted_at > self.MAX_QUOTE_AGE:
            raise RouteRejected("stale route quote")
        if route.return_to_zone_m < 0 or route.return_to_zone_s < 0:
            raise RouteRejected("negative return estimate")
        refs: set[str] = set()
        shortest = min(leg.distance_m for leg in route.legs)
        for leg in route.legs:
            if (
                leg.distance_m <= 0
                or leg.duration_s <= 0
                or leg.toll_won < 0
                or leg.uncertainty_s < 0
                or not leg.order_ref
                or not leg.public_leg_ref
                or leg.order_ref in refs
            ):
                raise RouteRejected("invalid or cross-order leg")
            refs.add(leg.order_ref)
            if leg.motorcycle_capability is not Verification.VERIFIED:
                raise RouteRejected("motorcycle legality/suitability is unverified")
            if len(route.legs) > 1 and leg.distance_m > shortest * (1 + self.MAX_DETOUR_RATIO):
                raise RouteRejected("bundle detour cap exceeded")
        if any(not hazard.valid(now) for hazard in route.hazards):
            raise RouteRejected("hazard source is fake, stale, or malformed")
        return route

    @staticmethod
    def _score(route: RouteAlternative, objective: RouteObjective) -> tuple[int, int]:
        hazard = sum(item.severity for item in route.hazards)
        uncertainty = sum(leg.uncertainty_s for leg in route.legs)
        if objective is RouteObjective.SAFEST:
            return hazard * 1000 + uncertainty, route.duration_s
        if objective is RouteObjective.LOWEST_COST:
            return route.toll_won, route.distance_m
        if objective is RouteObjective.SHORTEST:
            return route.duration_s, route.distance_m
        return route.duration_s + uncertainty + hazard * 300, route.toll_won

    @staticmethod
    def _explain(
        route: RouteAlternative,
        objective: RouteObjective,
        costs: RiderCostInputs,
        accepted: AcceptedPay,
    ) -> RouteExplanation:
        total_m = route.distance_m + route.return_to_zone_m
        rider_cost = (total_m * costs.won_per_km + 999) // 1000 + route.toll_won
        extra_m = max(0, total_m - accepted.included_distance_m)
        extra_s = max(0, route.duration_s + route.return_to_zone_s - accepted.included_wait_s)
        supplemental = (extra_m * costs.won_per_km + 999) // 1000 + (extra_s // 60) * 100
        reasons = (
            f"선택 기준: {objective.value}",
            f"거리 {route.distance_m}m·예상 {route.duration_s}초·통행료 {route.toll_won}원",
            f"복귀 예상 {route.return_to_zone_m}m·{route.return_to_zone_s}초",
            f"{costs.battery_or_fuel_label} 입력 기준 예상비용 {rider_cost}원",
        )
        return RouteExplanation(
            route.route_id,
            objective,
            reasons,
            sum(leg.uncertainty_s for leg in route.legs),
            rider_cost,
            supplemental,
            accepted.accepted_won,
        )

    @staticmethod
    def offline_instruction(session_id: str, now: datetime) -> OfflineInstruction:
        if not session_id or any(char in session_id for char in "\r\n\x00"):
            raise RouteRejected("invalid session")
        token = sha256(f"offline|{session_id}|{now.isoformat()}".encode()).hexdigest()
        return OfflineInstruction(token, now.astimezone(UTC) + timedelta(minutes=10))
