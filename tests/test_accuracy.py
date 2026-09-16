from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.accuracy import (
    AccuracyObservation,
    AccuracyPolicy,
    AccuracyReport,
    AccuracyTask,
    ArkaonAccuracyEvaluator,
    BranchAccuracy,
    PredictionDisposition,
    detect_error_drift,
    evaluate_national_accuracy,
)

NOW = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)


def policy(**changes):
    values = {
        "task": AccuracyTask.TRAVEL_TIME,
        "min_samples": 4,
        "max_mae": 3,
        "max_absolute_bias": 2,
        "max_p90_absolute_error": 5,
        "min_interval_coverage_bps": 7_500,
        "max_segment_mae_ratio_bps": 30_000,
        "min_baseline_improvement_bps": 1_000,
        "max_interval_width": 10,
        "max_observation_age": timedelta(days=30),
        "min_segments": 2,
        "min_samples_per_segment": 2,
    }
    values.update(changes)
    return AccuracyPolicy(**values)


def observation(
    number,
    *,
    predicted,
    actual,
    baseline,
    segment,
    low=None,
    high=None,
    observed_at=NOW,
):
    return AccuracyObservation(
        observation_id=f"obs-{number}",
        model_version="eta-v1",
        predicted=predicted,
        actual=actual,
        interval_low=predicted - 3 if low is None else low,
        interval_high=predicted + 3 if high is None else high,
        baseline_predicted=baseline,
        segment=segment,
        predicted_at=observed_at - timedelta(minutes=20),
        observed_at=observed_at,
    )


def passing_observations():
    return (
        observation(1, predicted=20, actual=21, baseline=27, segment="branch-sejong"),
        observation(2, predicted=18, actual=20, baseline=25, segment="branch-sejong"),
        observation(3, predicted=30, actual=29, baseline=35, segment="branch-busan"),
        observation(4, predicted=28, actual=30, baseline=36, segment="branch-busan"),
    )


def passing_report():
    return ArkaonAccuracyEvaluator().evaluate(
        policy=policy(),
        model_version="eta-v1",
        observations=passing_observations(),
        evaluated_at=NOW + timedelta(days=1),
    )


def test_accuracy_uses_out_of_sample_error_bias_tail_coverage_and_baseline() -> None:
    report = passing_report()

    assert report.passed
    assert report.sample_count == 4
    assert report.mae == 1
    assert report.p90_absolute_error == 2
    assert report.interval_coverage_bps == 10_000
    assert report.baseline_improvement_bps > 1_000


def test_duplicate_observation_is_not_double_counted_or_rebound() -> None:
    evaluator = ArkaonAccuracyEvaluator()
    observations = passing_observations()
    report = evaluator.evaluate(
        policy=policy(),
        model_version="eta-v1",
        observations=observations + (observations[0],),
        evaluated_at=NOW + timedelta(days=1),
    )

    assert report.sample_count == 4
    assert report.duplicate_observations == 1
    with pytest.raises(ValueError, match="OBSERVATION_IDEMPOTENCY_CONFLICT"):
        evaluator.evaluate(
            policy=policy(),
            model_version="eta-v1",
            observations=observations + (replace(observations[0], actual=999),),
            evaluated_at=NOW + timedelta(days=1),
        )


def test_sparse_or_unbalanced_branch_data_cannot_pass() -> None:
    evaluator = ArkaonAccuracyEvaluator()
    sparse = tuple(
        replace(item, segment="branch-sejong")
        for item in passing_observations()
    )
    report = evaluator.evaluate(
        policy=policy(),
        model_version="eta-v1",
        observations=sparse,
        evaluated_at=NOW,
    )

    assert not report.passed
    assert "INSUFFICIENT_SEGMENT_COVERAGE" in report.failure_codes


def test_financial_tasks_require_exact_integer_results() -> None:
    with pytest.raises(ValueError, match="FINANCIAL_CALCULATION_MUST_BE_EXACT"):
        policy(task=AccuracyTask.RIDER_NET_EARNINGS)
    exact_policy = policy(
        task=AccuracyTask.RIDER_NET_EARNINGS,
        exactness_required=True,
        max_mae=0,
        max_absolute_bias=0,
        max_p90_absolute_error=0,
    )
    inaccurate = tuple(
        replace(item, predicted=item.actual + 1)
        for item in passing_observations()
    )
    report = ArkaonAccuracyEvaluator().evaluate(
        policy=exact_policy,
        model_version="eta-v1",
        observations=inaccurate,
        evaluated_at=NOW,
    )

    assert not report.passed
    assert "FINANCIAL_EXACTNESS_FAILURE" in report.failure_codes


def test_unreliable_or_wide_prediction_uses_safe_fallback() -> None:
    evaluator = ArkaonAccuracyEvaluator()
    report = passing_report()

    assert evaluator.disposition(
        policy=policy(),
        report=report,
        interval_low=10,
        interval_high=30,
        input_observed_at=NOW,
        now=NOW,
        safety_critical=False,
    ) is PredictionDisposition.PUBLIC_RULE_FALLBACK
    assert evaluator.disposition(
        policy=policy(),
        report=replace(report, passed=False),
        interval_low=10,
        interval_high=15,
        input_observed_at=NOW,
        now=NOW,
        safety_critical=True,
    ) is PredictionDisposition.HUMAN_REVIEW


def test_drift_detects_material_error_deterioration() -> None:
    reference = passing_observations()
    current = tuple(
        replace(
            item,
            predicted=item.actual + 5,
            interval_low=item.actual + 3,
            interval_high=item.actual + 7,
            observation_id=f"current-{index}",
        )
        for index, item in enumerate(reference)
    )
    report = detect_error_drift(
        reference=reference,
        current=current,
        max_deterioration_bps=5_000,
    )

    assert report.drifted
    assert report.current_mae > report.reference_mae


def branch_report(branch_id, *, mae=2, passed=True):
    return BranchAccuracy(
        branch_id=branch_id,
        report=AccuracyReport(
            task=AccuracyTask.TRAVEL_TIME,
            model_version="eta-v1",
            sample_count=100,
            mae=mae,
            signed_bias=0,
            p90_absolute_error=4,
            interval_coverage_bps=9_000,
            worst_segment_mae_ratio_bps=10_000,
            baseline_improvement_bps=2_000,
            duplicate_observations=0,
            passed=passed,
            failure_codes=() if passed else ("MAE_THRESHOLD_EXCEEDED",),
        ),
    )


def test_national_rollout_requires_every_branch_to_pass() -> None:
    required = ("seoul", "busan", "sejong")
    missing = evaluate_national_accuracy(
        required_branch_ids=required,
        branch_reports=(branch_report("seoul"), branch_report("busan")),
        max_cross_branch_mae_ratio_bps=20_000,
    )
    failed = evaluate_national_accuracy(
        required_branch_ids=required,
        branch_reports=(
            branch_report("seoul"),
            branch_report("busan", passed=False),
            branch_report("sejong"),
        ),
        max_cross_branch_mae_ratio_bps=20_000,
    )
    passed = evaluate_national_accuracy(
        required_branch_ids=required,
        branch_reports=(
            branch_report("seoul", mae=2),
            branch_report("busan", mae=3),
            branch_report("sejong", mae=2),
        ),
        max_cross_branch_mae_ratio_bps=20_000,
    )

    assert missing.missing_branch_ids == ("sejong",)
    assert not missing.passed
    assert failed.failed_branch_ids == ("busan",)
    assert not failed.passed
    assert passed.passed


def test_cross_branch_accuracy_gap_blocks_national_activation() -> None:
    verdict = evaluate_national_accuracy(
        required_branch_ids=("branch-a", "branch-b"),
        branch_reports=(
            branch_report("branch-a", mae=1),
            branch_report("branch-b", mae=4),
        ),
        max_cross_branch_mae_ratio_bps=20_000,
    )

    assert verdict.cross_branch_mae_ratio_bps == 40_000
    assert not verdict.passed
