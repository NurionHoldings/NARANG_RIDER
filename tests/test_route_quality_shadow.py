from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.route_quality_shadow import (
    RouteQualityShadowEvaluator,
    ShadowEvaluationRejected,
    ShadowObservation,
    ShadowStatus,
    ShadowThresholds,
)

NOW = datetime(2026, 9, 16, 14, tzinfo=UTC)


def observation(**changes):
    values = {
        "provider_id": "synthetic-map",
        "route_class": "urban",
        "predicted_eta_s": 600,
        "observed_eta_s": 660,
        "predicted_distance_m": 3000,
        "observed_distance_m": 3150,
        "quoted_at": NOW,
        "completed_at": NOW + timedelta(minutes=11),
        "motorcycle_verified": True,
    }
    values.update(changes)
    return ShadowObservation(**values)


def cohort(count=5, **changes):
    return tuple(observation(**changes) for _ in range(count))


def evaluate(items=None, baseline=None, **kwargs):
    return RouteQualityShadowEvaluator().evaluate(
        provider_id=kwargs.get("provider_id", "synthetic-map"),
        route_class=kwargs.get("route_class", "urban"),
        observations=cohort() if items is None else items,
        baseline_eta_mape_bps=baseline,
    )


def test_accurate_synthetic_cohort_passes_only_for_human_review():
    report = evaluate()
    assert report.status is ShadowStatus.PASS_FOR_HUMAN_REVIEW
    assert report.eta_mape_bps == 909
    assert report.distance_mape_bps == 476
    assert report.eta_p90_error_s == 60
    assert not report.production_activation_allowed
    assert not report.dispatch_decision_allowed
    assert not report.pay_change_allowed
    assert not report.rider_penalty_allowed
    assert report.as_ci_artifact()["schema"] == "narang.route-quality-shadow.v1"


def test_insufficient_sample_never_passes():
    report = evaluate(cohort(4))
    assert report.status is ShadowStatus.INSUFFICIENT_DATA
    assert report.reasons == ("minimum_sample_not_met",)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"predicted_eta_s": 1000, "observed_eta_s": 600}, "eta_mape_exceeded"),
        (
            {"predicted_distance_m": 5000, "observed_distance_m": 3000},
            "distance_mape_exceeded",
        ),
        ({"predicted_eta_s": 1200, "observed_eta_s": 700}, "eta_p90_exceeded"),
    ],
)
def test_threshold_failures_are_blocked(changes, reason):
    report = evaluate(cohort(**changes))
    assert report.status is ShadowStatus.BLOCKED
    assert reason in report.reasons


def test_baseline_regression_is_blocked():
    thresholds = ShadowThresholds(maximum_regression_bps=100)
    report = RouteQualityShadowEvaluator(thresholds).evaluate(
        provider_id="synthetic-map",
        route_class="urban",
        observations=cohort(),
        baseline_eta_mape_bps=700,
    )
    assert report.regression_bps == 209
    assert "baseline_regression_exceeded" in report.reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "other"},
        {"route_class": "rural"},
        {"synthetic": False},
        {"contains_raw_location": True},
        {"contains_rider_or_order_id": True},
        {"motorcycle_verified": False},
    ],
)
def test_mixed_real_private_or_unverified_observations_are_rejected(changes):
    with pytest.raises(ShadowEvaluationRejected):
        evaluate((observation(**changes),))


@pytest.mark.parametrize(
    "changes",
    [
        {"predicted_eta_s": 0},
        {"observed_eta_s": -1},
        {"predicted_distance_m": 0},
        {"observed_distance_m": -1},
        {"completed_at": NOW - timedelta(seconds=1)},
        {"completed_at": NOW + timedelta(hours=5)},
        {"predicted_eta_s": 7000, "observed_eta_s": 600},
        {"predicted_distance_m": 40000, "observed_distance_m": 3000},
    ],
)
def test_invalid_time_value_and_implausible_ratio_are_rejected(changes):
    with pytest.raises(ShadowEvaluationRejected):
        evaluate((observation(**changes),))


def test_invalid_scope_and_baseline_are_rejected():
    with pytest.raises(ShadowEvaluationRejected):
        evaluate(provider_id="")
    with pytest.raises(ShadowEvaluationRejected):
        evaluate(route_class="unknown")
    with pytest.raises(ShadowEvaluationRejected):
        evaluate(baseline=-1)


def test_empty_cohort_is_insufficient_not_success():
    report = evaluate(())
    assert report.status is ShadowStatus.INSUFFICIENT_DATA
    assert report.sample_count == 0


def test_report_digest_is_deterministic_and_contains_no_trip_identity():
    first = evaluate()
    second = evaluate()
    assert first.report_digest == second.report_digest
    serialized = repr(first)
    assert "rider-" not in serialized
    assert "order-" not in serialized
    assert "latitude" not in serialized
