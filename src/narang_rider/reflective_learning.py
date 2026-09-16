"""Governed ARKAON reflective learning without self-modification."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256


class ReflectionRejected(ValueError):
    pass


class ReflectionStage(StrEnum):
    OBSERVED = "OBSERVED"
    SELF_GAP_IDENTIFIED = "SELF_GAP_IDENTIFIED"
    HYPOTHESIS_PROPOSED = "HYPOTHESIS_PROPOSED"
    SYNTHETIC_SHADOWED = "SYNTHETIC_SHADOWED"
    ETERNIAN_REVIEWED = "ETERNIAN_REVIEWED"
    OPERATOR_DECIDED = "OPERATOR_DECIDED"
    LESSON_RECORDED = "LESSON_RECORDED"


class Decision(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    DEFER = "DEFER"


@dataclass(frozen=True)
class ExternalObservation:
    observation_id: str
    source_evidence_digest: str
    source_feature: str
    problem_solved: str
    observed_method: str
    official_verified: bool
    copy_prohibited: bool = True


@dataclass(frozen=True)
class SelfAssessment:
    platform_id: str
    current_capability: str
    strength: str
    gap: str
    root_cause: str
    user_impact: str
    policy_conflicts: tuple[str, ...]


@dataclass(frozen=True)
class Metric:
    name: str
    baseline: float
    candidate: float
    higher_is_better: bool

    @property
    def improved(self) -> bool:
        return self.candidate > self.baseline if self.higher_is_better else self.candidate < self.baseline


@dataclass(frozen=True)
class ReflectionCase:
    case_id: str
    observation: ExternalObservation
    assessment: SelfAssessment | None
    hypothesis: str | None
    synthetic_test_plan: tuple[str, ...]
    metrics: tuple[Metric, ...]
    stage: ReflectionStage
    eternian_review_digest: str | None = None
    operator_decision: Decision | None = None
    operator_decision_digest: str | None = None
    lesson_id: str | None = None
    self_weight_change_allowed: bool = False
    automatic_operational_application: bool = False


@dataclass(frozen=True)
class ReflectiveLesson:
    lesson_id: str
    case_id: str
    platform_scope: str
    outcome: Decision
    principle: str
    reusable_pattern: str
    failure_or_rejection_reason: str
    evidence_digest: str
    recorded_at: datetime
    contains_operational_data: bool = False


@dataclass(frozen=True)
class ReflectionEvent:
    sequence: int
    case_id: str
    action: str
    evidence_digest: str
    previous_digest: str
    event_digest: str


class ArkaonReflectiveLearning:
    """Accumulates reviewed lessons, never model weights or automatic authority."""

    def __init__(self) -> None:
        self._cases: dict[str, ReflectionCase] = {}
        self._lessons: dict[str, ReflectiveLesson] = {}
        self.events: list[ReflectionEvent] = []

    def observe(self, case_id: str, observation: ExternalObservation) -> ReflectionCase:
        if (
            case_id in self._cases
            or not case_id
            or not observation.official_verified
            or not observation.copy_prohibited
            or not self._digest_ok(observation.source_evidence_digest)
            or not all((observation.source_feature, observation.problem_solved, observation.observed_method))
        ):
            raise ReflectionRejected("verified non-copy observation required")
        value = ReflectionCase(
            case_id, observation, None, None, (), (), ReflectionStage.OBSERVED
        )
        return self._save(value, "OBSERVED", observation.source_evidence_digest)

    def assess_self(self, case_id: str, assessment: SelfAssessment) -> ReflectionCase:
        value = self._require(case_id, ReflectionStage.OBSERVED)
        if (
            not all((assessment.platform_id, assessment.current_capability, assessment.strength,
                     assessment.gap, assessment.root_cause, assessment.user_impact))
            or assessment.platform_id != "NARANG_RIDER"
        ):
            raise ReflectionRejected("complete NARANG_RIDER self-assessment required")
        updated = replace(value, assessment=assessment, stage=ReflectionStage.SELF_GAP_IDENTIFIED)
        return self._save(updated, "SELF_GAP_IDENTIFIED", self._digest(assessment.__dict__))

    def propose_hypothesis(
        self, case_id: str, *, hypothesis: str, synthetic_test_plan: tuple[str, ...]
    ) -> ReflectionCase:
        value = self._require(case_id, ReflectionStage.SELF_GAP_IDENTIFIED)
        if not hypothesis.strip() or not synthetic_test_plan or any(not item for item in synthetic_test_plan):
            raise ReflectionRejected("testable bounded hypothesis required")
        updated = replace(
            value, hypothesis=hypothesis, synthetic_test_plan=synthetic_test_plan,
            stage=ReflectionStage.HYPOTHESIS_PROPOSED,
        )
        return self._save(updated, "HYPOTHESIS_PROPOSED", self._digest((hypothesis, synthetic_test_plan)))

    def record_shadow(self, case_id: str, metrics: tuple[Metric, ...]) -> ReflectionCase:
        value = self._require(case_id, ReflectionStage.HYPOTHESIS_PROPOSED)
        if not metrics or len({metric.name for metric in metrics}) != len(metrics):
            raise ReflectionRejected("unique baseline and candidate metrics required")
        updated = replace(value, metrics=metrics, stage=ReflectionStage.SYNTHETIC_SHADOWED)
        return self._save(updated, "SYNTHETIC_SHADOWED", self._digest([item.__dict__ for item in metrics]))

    def eternian_review(self, case_id: str, *, review_digest: str) -> ReflectionCase:
        value = self._require(case_id, ReflectionStage.SYNTHETIC_SHADOWED)
        if not self._digest_ok(review_digest):
            raise ReflectionRejected("Ethernian review digest required")
        updated = replace(
            value, eternian_review_digest=review_digest, stage=ReflectionStage.ETERNIAN_REVIEWED
        )
        return self._save(updated, "ETERNIAN_REVIEWED", review_digest)

    def operator_decide(
        self, case_id: str, *, decision: Decision, decision_digest: str
    ) -> ReflectionCase:
        value = self._require(case_id, ReflectionStage.ETERNIAN_REVIEWED)
        if not self._digest_ok(decision_digest):
            raise ReflectionRejected("operator decision digest required")
        updated = replace(
            value, operator_decision=decision, operator_decision_digest=decision_digest,
            stage=ReflectionStage.OPERATOR_DECIDED,
        )
        return self._save(updated, "OPERATOR_DECIDED", decision_digest)

    def record_lesson(
        self,
        case_id: str,
        *,
        lesson_id: str,
        principle: str,
        reusable_pattern: str,
        failure_or_rejection_reason: str,
        now: datetime,
    ) -> ReflectiveLesson:
        value = self._require(case_id, ReflectionStage.OPERATOR_DECIDED)
        if (
            lesson_id in self._lessons or not lesson_id or not principle.strip()
            or not reusable_pattern.strip() or now.tzinfo is None
            or value.operator_decision in {Decision.REJECT, Decision.DEFER}
            and not failure_or_rejection_reason.strip()
        ):
            raise ReflectionRejected("complete immutable lesson required")
        evidence = self._digest(
            (case_id, value.operator_decision.value, principle, reusable_pattern,
             failure_or_rejection_reason, now.isoformat())
        )
        lesson = ReflectiveLesson(
            lesson_id, case_id, "NARANG_RIDER", value.operator_decision,
            principle, reusable_pattern, failure_or_rejection_reason, evidence, now,
        )
        self._lessons[lesson_id] = lesson
        updated = replace(value, lesson_id=lesson_id, stage=ReflectionStage.LESSON_RECORDED)
        self._save(updated, "LESSON_RECORDED", evidence)
        return lesson

    def lessons(self) -> tuple[ReflectiveLesson, ...]:
        return tuple(self._lessons.values())

    def delete_failure_or_self_modify(self) -> None:
        raise ReflectionRejected("failure deletion, self-weight change and self-approval are forbidden")

    def _require(self, case_id: str, stage: ReflectionStage) -> ReflectionCase:
        value = self._cases[case_id]
        if value.stage is not stage:
            raise ReflectionRejected("invalid reflection stage")
        return value

    def _save(self, value: ReflectionCase, action: str, evidence: str) -> ReflectionCase:
        self._cases[value.case_id] = value
        previous = self.events[-1].event_digest if self.events else "0" * 64
        sequence = len(self.events) + 1
        event_digest = self._digest((sequence, value.case_id, action, evidence, previous))
        self.events.append(ReflectionEvent(sequence, value.case_id, action, evidence, previous, event_digest))
        return value

    @staticmethod
    def _digest(value: object) -> str:
        return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _digest_ok(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
