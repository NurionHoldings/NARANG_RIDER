from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class AccuracyTask(StrEnum):
    DEMAND_FORECAST = "DEMAND_FORECAST"
    PREPARATION_TIME = "PREPARATION_TIME"
    TRAVEL_TIME = "TRAVEL_TIME"
    RIDER_NET_EARNINGS = "RIDER_NET_EARNINGS"
    MERCHANT_MARGIN = "MERCHANT_MARGIN"
    SETTLEMENT_ANOMALY = "SETTLEMENT_ANOMALY"


class PredictionDisposition(StrEnum):
    MODEL = "MODEL"
    PUBLIC_RULE_FALLBACK = "PUBLIC_RULE_FALLBACK"
    HUMAN_REVIEW = "HUMAN_REVIEW"


@dataclass(frozen=True)
class AccuracyPolicy:
    task: AccuracyTask
    min_samples: int
    max_mae: int
    max_absolute_bias: int
    max_p90_absolute_error: int
    min_interval_coverage_bps: int
    max_segment_mae_ratio_bps: int
    min_baseline_improvement_bps: int
    max_interval_width: int
    max_observation_age: timedelta
    min_segments: int
    min_samples_per_segment: int
    exactness_required: bool = False

    def __post_init__(self) -> None:
        if (
            self.min_samples < 1
            or self.min_segments < 1
            or self.min_samples_per_segment < 1
            or min(
                self.max_mae,
                self.max_absolute_bias,
                self.max_p90_absolute_error,
                self.max_interval_width,
            )
            < 0
            or not 0 <= self.min_interval_coverage_bps <= 10_000
            or self.max_segment_mae_ratio_bps < 10_000
            or self.min_baseline_improvement_bps < 0
            or self.max_observation_age <= timedelta(0)
        ):
            raise ValueError("INVALID_ACCURACY_POLICY")
        if self.task in {
            AccuracyTask.RIDER_NET_EARNINGS,
            AccuracyTask.MERCHANT_MARGIN,
        } and not self.exactness_required:
            raise ValueError("FINANCIAL_CALCULATION_MUST_BE_EXACT")


@dataclass(frozen=True)
class AccuracyObservation:
    observation_id: str
    model_version: str
    predicted: int
    actual: int
    interval_low: int
    interval_high: int
    baseline_predicted: int
    segment: str
    predicted_at: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        integer_values = (
            self.predicted,
            self.actual,
            self.interval_low,
            self.interval_high,
            self.baseline_predicted,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise TypeError("ACCURACY_VALUES_MUST_BE_INTEGERS")
        if not all(
            value.strip() for value in (self.observation_id, self.model_version, self.segment)
        ):
            raise ValueError("ACCURACY_OBSERVATION_IDENTITY_REQUIRED")
        if (
            self.predicted_at.tzinfo is None
            or self.observed_at.tzinfo is None
            or self.observed_at < self.predicted_at
            or self.interval_low > self.predicted
            or self.interval_high < self.predicted
        ):
            raise ValueError("INVALID_ACCURACY_OBSERVATION")

    @property
    def error(self) -> int:
        return self.predicted - self.actual

    @property
    def absolute_error(self) -> int:
        return abs(self.error)

    @property
    def baseline_absolute_error(self) -> int:
        return abs(self.baseline_predicted - self.actual)

    @property
    def covered(self) -> bool:
        return self.interval_low <= self.actual <= self.interval_high


@dataclass(frozen=True)
class AccuracyReport:
    task: AccuracyTask
    model_version: str
    sample_count: int
    mae: int
    signed_bias: int
    p90_absolute_error: int
    interval_coverage_bps: int
    worst_segment_mae_ratio_bps: int
    baseline_improvement_bps: int
    duplicate_observations: int
    passed: bool
    failure_codes: tuple[str, ...]


class ArkaonAccuracyEvaluator:
    def evaluate(
        self,
        *,
        policy: AccuracyPolicy,
        model_version: str,
        observations: tuple[AccuracyObservation, ...],
        evaluated_at: datetime,
    ) -> AccuracyReport:
        if evaluated_at.tzinfo is None:
            raise ValueError("AWARE_EVALUATION_TIME_REQUIRED")
        selected = tuple(item for item in observations if item.model_version == model_version)
        unique: dict[str, AccuracyObservation] = {}
        duplicate_count = 0
        for item in selected:
            existing = unique.get(item.observation_id)
            if existing is not None:
                if existing != item:
                    raise ValueError("OBSERVATION_IDEMPOTENCY_CONFLICT")
                duplicate_count += 1
                continue
            unique[item.observation_id] = item
        samples = tuple(sorted(unique.values(), key=lambda item: item.predicted_at))
        failures: list[str] = []
        if len(samples) < policy.min_samples:
            failures.append("INSUFFICIENT_OUT_OF_SAMPLE_DATA")
        if not samples:
            return AccuracyReport(
                task=policy.task,
                model_version=model_version,
                sample_count=0,
                mae=0,
                signed_bias=0,
                p90_absolute_error=0,
                interval_coverage_bps=0,
                worst_segment_mae_ratio_bps=0,
                baseline_improvement_bps=0,
                duplicate_observations=duplicate_count,
                passed=False,
                failure_codes=tuple(failures),
            )
        if any(evaluated_at - item.observed_at > policy.max_observation_age for item in samples):
            failures.append("STALE_EVALUATION_DATA")
        absolute_errors = sorted(item.absolute_error for item in samples)
        mae = sum(absolute_errors) // len(samples)
        signed_bias = sum(item.error for item in samples) // len(samples)
        p90_index = max(0, (9 * len(samples) + 9) // 10 - 1)
        p90 = absolute_errors[p90_index]
        coverage_bps = sum(item.covered for item in samples) * 10_000 // len(samples)
        segment_errors: dict[str, list[int]] = {}
        for item in samples:
            segment_errors.setdefault(item.segment, []).append(item.absolute_error)
        if len(segment_errors) < policy.min_segments:
            failures.append("INSUFFICIENT_SEGMENT_COVERAGE")
        if any(
            len(errors) < policy.min_samples_per_segment
            for errors in segment_errors.values()
        ):
            failures.append("INSUFFICIENT_SEGMENT_SAMPLES")
        segment_maes = [
            sum(errors) // len(errors)
            for errors in segment_errors.values()
            if errors
        ]
        best_segment = min(segment_maes)
        worst_segment = max(segment_maes)
        segment_ratio_bps = (
            10_000
            if worst_segment == 0
            else worst_segment * 10_000 // max(1, best_segment)
        )
        baseline_error = sum(item.baseline_absolute_error for item in samples)
        model_error = sum(absolute_errors)
        improvement_bps = (
            0
            if baseline_error == 0
            else (baseline_error - model_error) * 10_000 // baseline_error
        )
        if mae > policy.max_mae:
            failures.append("MAE_THRESHOLD_EXCEEDED")
        if abs(signed_bias) > policy.max_absolute_bias:
            failures.append("BIAS_THRESHOLD_EXCEEDED")
        if p90 > policy.max_p90_absolute_error:
            failures.append("TAIL_ERROR_THRESHOLD_EXCEEDED")
        if coverage_bps < policy.min_interval_coverage_bps:
            failures.append("INTERVAL_UNDERCOVERAGE")
        if segment_ratio_bps > policy.max_segment_mae_ratio_bps:
            failures.append("SEGMENT_ACCURACY_GAP")
        if improvement_bps < policy.min_baseline_improvement_bps:
            failures.append("BASELINE_NOT_BEATEN")
        if policy.exactness_required and any(item.absolute_error for item in samples):
            failures.append("FINANCIAL_EXACTNESS_FAILURE")
        return AccuracyReport(
            task=policy.task,
            model_version=model_version,
            sample_count=len(samples),
            mae=mae,
            signed_bias=signed_bias,
            p90_absolute_error=p90,
            interval_coverage_bps=coverage_bps,
            worst_segment_mae_ratio_bps=segment_ratio_bps,
            baseline_improvement_bps=improvement_bps,
            duplicate_observations=duplicate_count,
            passed=not failures,
            failure_codes=tuple(sorted(set(failures))),
        )

    @staticmethod
    def disposition(
        *,
        policy: AccuracyPolicy,
        report: AccuracyReport,
        interval_low: int,
        interval_high: int,
        input_observed_at: datetime,
        now: datetime,
        safety_critical: bool,
    ) -> PredictionDisposition:
        if now.tzinfo is None or input_observed_at.tzinfo is None:
            raise ValueError("AWARE_PREDICTION_TIME_REQUIRED")
        unreliable = (
            not report.passed
            or interval_high < interval_low
            or interval_high - interval_low > policy.max_interval_width
            or now - input_observed_at > policy.max_observation_age
        )
        if unreliable and safety_critical:
            return PredictionDisposition.HUMAN_REVIEW
        if unreliable:
            return PredictionDisposition.PUBLIC_RULE_FALLBACK
        return PredictionDisposition.MODEL


@dataclass(frozen=True)
class DriftReport:
    reference_mae: int
    current_mae: int
    deterioration_bps: int
    drifted: bool


@dataclass(frozen=True)
class BranchAccuracy:
    branch_id: str
    report: AccuracyReport


@dataclass(frozen=True)
class NationalAccuracyVerdict:
    required_branch_ids: tuple[str, ...]
    evaluated_branch_ids: tuple[str, ...]
    missing_branch_ids: tuple[str, ...]
    failed_branch_ids: tuple[str, ...]
    cross_branch_mae_ratio_bps: int
    passed: bool


def evaluate_national_accuracy(
    *,
    required_branch_ids: tuple[str, ...],
    branch_reports: tuple[BranchAccuracy, ...],
    max_cross_branch_mae_ratio_bps: int,
) -> NationalAccuracyVerdict:
    required = tuple(sorted(set(required_branch_ids)))
    if not required or max_cross_branch_mae_ratio_bps < 10_000:
        raise ValueError("VALID_NATIONAL_ACCURACY_SCOPE_REQUIRED")
    by_branch: dict[str, AccuracyReport] = {}
    for item in branch_reports:
        if item.branch_id in by_branch:
            raise ValueError("DUPLICATE_BRANCH_ACCURACY_REPORT")
        by_branch[item.branch_id] = item.report
    missing = tuple(branch_id for branch_id in required if branch_id not in by_branch)
    failed = tuple(
        branch_id
        for branch_id in required
        if branch_id in by_branch and not by_branch[branch_id].passed
    )
    maes = [by_branch[branch_id].mae for branch_id in required if branch_id in by_branch]
    if not maes:
        ratio = 0
    elif max(maes) == 0:
        ratio = 10_000
    else:
        ratio = max(maes) * 10_000 // max(1, min(maes))
    passed = (
        not missing
        and not failed
        and ratio <= max_cross_branch_mae_ratio_bps
    )
    return NationalAccuracyVerdict(
        required_branch_ids=required,
        evaluated_branch_ids=tuple(sorted(by_branch)),
        missing_branch_ids=missing,
        failed_branch_ids=failed,
        cross_branch_mae_ratio_bps=ratio,
        passed=passed,
    )


def detect_error_drift(
    *,
    reference: tuple[AccuracyObservation, ...],
    current: tuple[AccuracyObservation, ...],
    max_deterioration_bps: int,
) -> DriftReport:
    if not reference or not current or max_deterioration_bps < 0:
        raise ValueError("VALID_DRIFT_WINDOWS_REQUIRED")
    reference_mae = sum(item.absolute_error for item in reference) // len(reference)
    current_mae = sum(item.absolute_error for item in current) // len(current)
    deterioration = (
        0
        if reference_mae == 0 and current_mae == 0
        else 10_000
        if reference_mae == 0
        else (current_mae - reference_mae) * 10_000 // reference_mae
    )
    return DriftReport(
        reference_mae=reference_mae,
        current_mae=current_mae,
        deterioration_bps=deterioration,
        drifted=deterioration > max_deterioration_bps,
    )
