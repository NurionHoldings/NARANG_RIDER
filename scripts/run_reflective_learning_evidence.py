from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.reflective_learning import (
    ArkaonReflectiveLearning,
    Decision,
    ExternalObservation,
    Metric,
    SelfAssessment,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "arkaon-reflective-learning-evidence.json"


def digest(value: str) -> str: return sha256(value.encode()).hexdigest()


def normalize(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, Enum): return value.value
    if isinstance(value, dict): return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [normalize(item) for item in value]
    return value


def main() -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    service = ArkaonReflectiveLearning()
    service.observe("case-1", ExternalObservation(
        "obs-1", digest("official"), "단계 안내", "진행 혼란", "상태와 다음 행동 표시", True))
    service.assess_self("case-1", SelfAssessment(
        "NARANG_RIDER", "완료 안내", "권리 고지", "다음 단계 불명확", "상태모델 부족",
        "중복 제출", ("자동승인 금지",)))
    service.propose_hypothesis("case-1", hypothesis="단계 안내가 중복을 줄인다",
        synthetic_test_plan=("합성 사용성", "접근성"))
    service.record_shadow("case-1", (Metric("task_success", 0.7, 0.9, True),))
    service.eternian_review("case-1", review_digest=digest("eternian"))
    service.operator_decide("case-1", decision=Decision.ACCEPT, decision_digest=digest("operator"))
    lesson = service.record_lesson("case-1", lesson_id="lesson-1",
        principle="진행상태와 다음 행동을 함께 제시", reusable_pattern="단계·행동·권리 패턴",
        failure_or_rejection_reason="", now=now)
    payload = normalize({"lesson": asdict(lesson), "events": [asdict(item) for item in service.events],
        "self_weight_change_allowed": False, "automatic_operational_application": False})
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")


if __name__ == "__main__": main()
