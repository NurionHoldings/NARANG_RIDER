"""Privacy-minimized offline Shadow evaluation for route quality and ETA."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256


class ShadowEvaluationRejected(ValueError):
    pass


class ShadowStatus(StrEnum):
    PASS_FOR_HUMAN_REVIEW = "pass_for_human_review"
    INSUFFICIENT_DATA = "insufficient_data"
    BLOCKED = "blocked"


ALLOWED_ROUTE_CLASSES = frozenset({"urban", "suburban", "rural"})


@dataclass(frozen=True)
class ShadowObservation:
    provider_id: str
    route_class: str
    predicted_eta_s: int
    observed_eta_s: int
    predicted_distance_m: int
    observed_distance_m: int
    quoted_at: datetime
    completed_at: datetime
    motorcycle_verified: bool
    synthetic: bool = True
    contains_raw_location: bool = False
    contains_rider_or_order_id: bool = False


@dataclass(frozen=True)
class ShadowThresholds:
    minimum_samples: int = 5
    maximum_eta_mape_bps: int = 2000
    maximum_distance_mape_bps: int = 1500
    maximum_eta_p90_error_s: int = 300
    maximum_regression_bps: int = 500


@dataclass(frozen=True)
class RouteQualityShadowReport:
    provider_id: str
    route_class: str
    sample_count: int
    eta_mape_bps: int
    distance_mape_bps: int
    eta_p90_error_s: int
    baseline_eta_mape_bps: int | None
    regression_bps: int
    status: ShadowStatus
    reasons: tuple[str, ...]
    report_digest: str
    dispatch_decision_allowed: bool = False
    pay_change_allowed: bool = False
    rider_penalty_allowed: bool = False
    production_activation_allowed: bool = False

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.route-quality-shadow.v1", **self.__dict__}


class RouteQualityShadowEvaluator:
    MAX_OBSERVATION_DURATION = timedelta(hours=4)
    MAX_RATIO = 10

    def __init__(self, thresholds: ShadowThresholds | None = None) -> None:
        self.thresholds = thresholds or ShadowThresholds()

    def evaluate(
        self,
        *,
        provider_id: str,
        route_class: str,
        observations: tuple[ShadowObservation, ...],
        baseline_eta_mape_bps: int | None = None,
    ) -> RouteQualityShadowReport:
        if not provider_id or route_class not in ALLOWED_ROUTE_CLASSES:
            raise ShadowEvaluationRejected("valid provider and route class required")
        if baseline_eta_mape_bps is not None and baseline_eta_mape_bps < 0:
            raise ShadowEvaluationRejected("baseline metric cannot be negative")
        for item in observations:
            self._validate_observation(item, provider_id, route_class)

        count = len(observations)
        if count:
            eta_errors = tuple(
                abs(item.predicted_eta_s - item.observed_eta_s) for item in observations
            )
            eta_mape = sum(
                abs(item.predicted_eta_s - item.observed_eta_s) * 10_000
                // item.observed_eta_s
                for item in observations
            ) // count
            distance_mape = sum(
                abs(item.predicted_distance_m - item.observed_distance_m) * 10_000
                // item.observed_distance_m
                for item in observations
            ) // count
            p90 = self._percentile_90(eta_errors)
        else:
            eta_mape = distance_mape = p90 = 0

        regression = (
            max(0, eta_mape - baseline_eta_mape_bps)
            if baseline_eta_mape_bps is not None
            else 0
        )
        reasons: list[str] = []
        t = self.thresholds
        if count < t.minimum_samples:
            status = ShadowStatus.INSUFFICIENT_DATA
            reasons.append("minimum_sample_not_met")
        else:
            if eta_mape > t.maximum_eta_mape_bps:
                reasons.append("eta_mape_exceeded")
            if distance_mape > t.maximum_distance_mape_bps:
                reasons.append("distance_mape_exceeded")
            if p90 > t.maximum_eta_p90_error_s:
                reasons.append("eta_p90_exceeded")
            if regression > t.maximum_regression_bps:
                reasons.append("baseline_regression_exceeded")
            status = ShadowStatus.BLOCKED if reasons else ShadowStatus.PASS_FOR_HUMAN_REVIEW

        parts = (
            provider_id,
            route_class,
            str(count),
            str(eta_mape),
            str(distance_mape),
            str(p90),
            str(baseline_eta_mape_bps),
            str(regression),
            status.value,
            ",".join(reasons),
        )
        return RouteQualityShadowReport(
            provider_id=provider_id,
            route_class=route_class,
            sample_count=count,
            eta_mape_bps=eta_mape,
            distance_mape_bps=distance_mape,
            eta_p90_error_s=p90,
            baseline_eta_mape_bps=baseline_eta_mape_bps,
            regression_bps=regression,
            status=status,
            reasons=tuple(reasons),
            report_digest=sha256("|".join(parts).encode()).hexdigest(),
        )

    def _validate_observation(
        self, item: ShadowObservation, provider_id: str, route_class: str
    ) -> None:
        if item.provider_id != provider_id or item.route_class != route_class:
            raise ShadowEvaluationRejected("cross-provider or cross-cohort mixing forbidden")
        if not item.synthetic:
            raise ShadowEvaluationRejected("only synthetic Shadow observations are allowed")
        if item.contains_raw_location or item.contains_rider_or_order_id:
            raise ShadowEvaluationRejected("location and identity data are forbidden")
        if not item.motorcycle_verified:
            raise ShadowEvaluationRejected("motorcycle suitability must be verified")
        values = (
            item.predicted_eta_s,
            item.observed_eta_s,
            item.predicted_distance_m,
            item.observed_distance_m,
        )
        if any(value <= 0 for value in values):
            raise ShadowEvaluationRejected("positive ETA and distance values required")
        if item.completed_at < item.quoted_at:
            raise ShadowEvaluationRejected("completion cannot precede quote")
        if item.completed_at - item.quoted_at > self.MAX_OBSERVATION_DURATION:
            raise ShadowEvaluationRejected("observation duration exceeds four hours")
        if (
            max(item.predicted_eta_s, item.observed_eta_s)
            > min(item.predicted_eta_s, item.observed_eta_s) * self.MAX_RATIO
            or max(item.predicted_distance_m, item.observed_distance_m)
            > min(item.predicted_distance_m, item.observed_distance_m) * self.MAX_RATIO
        ):
            raise ShadowEvaluationRejected("implausible ratio requires source review")

    @staticmethod
    def _percentile_90(values: tuple[int, ...]) -> int:
        ordered = sorted(values)
        rank = max(1, (len(ordered) * 90 + 99) // 100)
        return ordered[rank - 1]
