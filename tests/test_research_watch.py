from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.research_watch import (
    ArkaonResearchWatch,
    ResearchCandidate,
    ResearchRejected,
    ResearchSource,
    ResearchStage,
    StartupTrigger,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def watch():
    return ArkaonResearchWatch(frozenset({"docs.example.invalid"}))


def candidate():
    source = ResearchSource(
        "source-1", "https://docs.example.invalid/release", "공식 기능 발표", "합성 공급자",
        NOW - timedelta(days=1), NOW, NOW + timedelta(days=14), digest("content"), True,
    )
    return ResearchCandidate(
        "candidate-1", "DELIVERY", source, "합성 신규 기능", "기존 후보와 다름",
        "사용자 대기 감소", ("개인정보 경계",), ("합성 계약시험",),
    )


def test_privacy_safe_startup_trigger_does_not_collect_machine_identity():
    value = watch()
    value.accept_startup_trigger(StartupTrigger("trigger-1", NOW, "WINDOWS_TASK_SCHEDULER"))
    with pytest.raises(ResearchRejected):
        value.accept_startup_trigger(
            StartupTrigger("trigger-2", NOW, "WINDOWS_TASK_SCHEDULER", True)
        )


def test_official_https_allowlist_and_untrusted_quarantine_are_required():
    value = watch()
    assert value.quarantine(candidate()).stage is ResearchStage.QUARANTINED
    bad = replace(
        candidate(),
        candidate_id="bad",
        source=replace(candidate().source, url="https://unverified.example/release"),
    )
    with pytest.raises(ResearchRejected, match="official-source"):
        value.quarantine(bad)


def test_evidence_then_eternian_review_precedes_improvement_proposal():
    value = watch()
    value.quarantine(candidate())
    assert value.verify_evidence("candidate-1", now=NOW).stage is ResearchStage.EVIDENCE_VERIFIED
    assert value.request_eternian_review("candidate-1").stage is ResearchStage.ETERNIAN_REVIEW
    proposed = value.record_eternian_review(
        "candidate-1", review_digest=digest("eternian-review"), accepted=True
    )
    assert proposed.stage is ResearchStage.IMPROVEMENT_PROPOSED
    assert proposed.proposal_digest
    assert not proposed.automatic_learning and not proposed.production_change_allowed


def test_rejected_review_never_creates_proposal():
    value = watch()
    value.quarantine(candidate())
    value.verify_evidence("candidate-1", now=NOW)
    value.request_eternian_review("candidate-1")
    rejected = value.record_eternian_review(
        "candidate-1", review_digest=digest("reject"), accepted=False
    )
    assert rejected.stage is ResearchStage.REJECTED
    assert rejected.proposal_digest is None


def test_stale_source_and_automatic_learning_fail_closed():
    value = watch()
    value.quarantine(candidate())
    with pytest.raises(ResearchRejected, match="fresh"):
        value.verify_evidence("candidate-1", now=NOW + timedelta(days=15))
    with pytest.raises(ResearchRejected, match="automatic learning"):
        value.apply_or_learn()
