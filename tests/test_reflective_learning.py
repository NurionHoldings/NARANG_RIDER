from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise

import pytest

from narang_rider.reflective_learning import (
    ArkaonReflectiveLearning,
    Decision,
    ExternalObservation,
    Metric,
    ReflectionRejected,
    ReflectionStage,
    SelfAssessment,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def observation():
    return ExternalObservation(
        "obs-1", digest("official-evidence"), "단계형 진행 안내", "사용자 진행 혼란",
        "단계별 상태와 다음 행동 표시", True,
    )


def assessment():
    return SelfAssessment(
        "NARANG_RIDER", "단일 완료 메시지", "권리 고지가 명확함", "다음 단계가 불명확함",
        "진행상태 모델 부족", "문의와 중복 제출 가능", ("자동승인 금지",),
    )


def shadowed():
    value = ArkaonReflectiveLearning()
    value.observe("case-1", observation())
    value.assess_self("case-1", assessment())
    value.propose_hypothesis(
        "case-1", hypothesis="단계 표시가 중복 제출을 줄인다",
        synthetic_test_plan=("합성 사용성 시험", "접근성 시험"),
    )
    value.record_shadow("case-1", (Metric("task_success", 0.7, 0.9, True),))
    return value


def test_observation_requires_verified_non_copy_evidence():
    value = ArkaonReflectiveLearning()
    with pytest.raises(ReflectionRejected, match="non-copy"):
        value.observe("bad", ExternalObservation(**{**observation().__dict__, "copy_prohibited": False}))


def test_reflection_names_strength_gap_root_cause_and_user_impact():
    value = ArkaonReflectiveLearning()
    value.observe("case-1", observation())
    result = value.assess_self("case-1", assessment())
    assert result.stage is ReflectionStage.SELF_GAP_IDENTIFIED
    assert result.assessment.strength and result.assessment.root_cause


def test_hypothesis_must_be_shadowed_before_review_and_decision():
    value = shadowed()
    assert value.events[-1].action == "SYNTHETIC_SHADOWED"
    reviewed = value.eternian_review("case-1", review_digest=digest("review"))
    assert reviewed.stage is ReflectionStage.ETERNIAN_REVIEWED
    decided = value.operator_decide(
        "case-1", decision=Decision.ACCEPT, decision_digest=digest("operator")
    )
    assert decided.stage is ReflectionStage.OPERATOR_DECIDED


@pytest.mark.parametrize("decision", [Decision.REJECT, Decision.DEFER])
def test_rejected_and_deferred_outcomes_require_reason_and_become_lessons(decision):
    value = shadowed()
    value.eternian_review("case-1", review_digest=digest("review"))
    value.operator_decide("case-1", decision=decision, decision_digest=digest("decision"))
    with pytest.raises(ReflectionRejected, match="lesson"):
        value.record_lesson(
            "case-1", lesson_id="lesson-1", principle="검증된 원칙",
            reusable_pattern="합성 패턴", failure_or_rejection_reason="", now=NOW,
        )
    lesson = value.record_lesson(
        "case-1", lesson_id="lesson-1", principle="검증 없는 도입을 피한다",
        reusable_pattern="공식근거와 shadow를 먼저 수행", failure_or_rejection_reason="효과 부족", now=NOW,
    )
    assert lesson.outcome is decision
    assert lesson.failure_or_rejection_reason


def test_accepted_lesson_contains_no_operational_data_or_self_modification():
    value = shadowed()
    value.eternian_review("case-1", review_digest=digest("review"))
    value.operator_decide("case-1", decision=Decision.ACCEPT, decision_digest=digest("decision"))
    lesson = value.record_lesson(
        "case-1", lesson_id="lesson-1", principle="진행상태를 명시한다",
        reusable_pattern="상태·다음행동·권리를 함께 제시", failure_or_rejection_reason="", now=NOW,
    )
    assert not lesson.contains_operational_data
    assert value.lessons() == (lesson,)
    with pytest.raises(ReflectionRejected, match="self-weight"):
        value.delete_failure_or_self_modify()


def test_reflection_events_are_append_only_digest_chain():
    value = shadowed()
    for previous, current in pairwise(value.events):
        assert current.previous_digest == previous.event_digest
    assert [item.sequence for item in value.events] == list(range(1, len(value.events) + 1))
