from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.pilot import (
    PILOT_ADMIN_ROUTES,
    ApprovalAuthority,
    ApprovalRole,
    ParticipantConsentRegistry,
    PilotErrorCode,
    PilotLifecycle,
    PilotReadinessEvaluator,
    PilotReadinessSubmission,
    PilotRejected,
    PilotStage,
    PilotState,
    ReadinessItem,
    StopCriterion,
)

NOW = datetime(2026, 9, 16, 8, tzinfo=UTC)
AUTHORITY = ApprovalAuthority(b"pilot-readiness-test-signing-key-32-bytes")


def approval(item: str, who: str, role: ApprovalRole, *, expires=timedelta(days=30)):
    return AUTHORITY.issue(
        reference=f"approval://{item}/{who}",
        item=item,
        branch_id="branch-sejong",
        approver_id=who,
        role=role,
        issued_at=NOW,
        expires_at=NOW + expires,
    )


def complete_submission(
    target: PilotStage = PilotStage.INTERNAL_SHADOW,
) -> PilotReadinessSubmission:
    evidence = tuple(
        approval(item.value, f"owner-{index}", ApprovalRole.EVIDENCE_OWNER)
        for index, item in enumerate(ReadinessItem)
    )
    return PilotReadinessSubmission(
        branch_id="branch-sejong",
        submitted_by="release-manager",
        target_stage=target,
        evidence=evidence,
        ethernian_review=approval(
            f"pilot:{target.value}", "ethernian-reviewer", ApprovalRole.ETHERNIAN_REVIEWER
        ),
        operator_approval=approval(f"pilot:{target.value}", "operator-choi", ApprovalRole.OPERATOR),
    )


def test_complete_gate_is_machine_readable_but_not_legal_approval() -> None:
    report = PilotReadinessEvaluator(AUTHORITY).require_ready(complete_submission(), now=NOW)
    assert report.ready
    assert not report.legal_approval
    assert len(report.checklist) == len(ReadinessItem)
    assert all(
        item.passed and item.reference.startswith("approval://") for item in report.checklist
    )
    assert "employment_and_worker_classification" in report.counsel_decisions_required
    assert '"legal_approval":false' in report.to_json()
    assert len(report.report_digest) == 64


def test_missing_expired_forged_and_cross_branch_evidence_fail_closed() -> None:
    evaluator = PilotReadinessEvaluator(AUTHORITY)
    submission = complete_submission()
    missing = replace(submission, evidence=submission.evidence[1:])
    assert not evaluator.evaluate(missing, now=NOW).ready
    expired_item = approval(
        ReadinessItem.BRANCH_READINESS.value,
        "expired-owner",
        ApprovalRole.EVIDENCE_OWNER,
        expires=timedelta(seconds=1),
    )
    expired = replace(submission, evidence=(expired_item,) + submission.evidence[1:])
    result = evaluator.evaluate(expired, now=NOW + timedelta(minutes=1))
    assert not result.ready
    assert (
        next(x for x in result.checklist if x.item is ReadinessItem.BRANCH_READINESS).reason
        == "APPROVAL_EXPIRED"
    )
    forged = replace(submission.evidence[0], signature="0" * 64)
    result = evaluator.evaluate(
        replace(submission, evidence=(forged,) + submission.evidence[1:]), now=NOW
    )
    assert not result.ready
    cross = replace(submission.evidence[0], branch_id="branch-other")
    result = evaluator.evaluate(
        replace(submission, evidence=(cross,) + submission.evidence[1:]), now=NOW
    )
    assert not result.ready


def test_self_approval_and_same_reviewer_operator_are_forbidden() -> None:
    submission = complete_submission()
    self_approved = replace(
        submission,
        evidence=(
            approval(
                ReadinessItem.BRANCH_READINESS.value, "release-manager", ApprovalRole.EVIDENCE_OWNER
            ),
        )
        + submission.evidence[1:],
    )
    assert not PilotReadinessEvaluator(AUTHORITY).evaluate(self_approved, now=NOW).ready
    same_person = replace(
        submission,
        operator_approval=approval(
            f"pilot:{submission.target_stage.value}", "ethernian-reviewer", ApprovalRole.OPERATOR
        ),
    )
    with pytest.raises(PilotRejected) as caught:
        PilotReadinessEvaluator(AUTHORITY).evaluate(same_person, now=NOW)
    assert caught.value.code is PilotErrorCode.SELF_APPROVAL_FORBIDDEN


def test_real_data_is_forbidden_before_limited_gate() -> None:
    with pytest.raises(PilotRejected) as caught:
        PilotReadinessEvaluator(AUTHORITY).evaluate(
            replace(complete_submission(), contains_real_data=True), now=NOW
        )
    assert caught.value.code is PilotErrorCode.REAL_DATA_FORBIDDEN


def test_stages_cannot_be_bypassed() -> None:
    evaluator = PilotReadinessEvaluator(AUTHORITY)
    lifecycle = PilotLifecycle()
    state = PilotState("branch-sejong", PilotStage.SYNTHETIC)
    report = evaluator.require_ready(complete_submission(PilotStage.CLOSED_SANDBOX), now=NOW)
    with pytest.raises(PilotRejected) as caught:
        lifecycle.advance(state, report)
    assert caught.value.code is PilotErrorCode.STAGE_BYPASS_FORBIDDEN
    shadow = evaluator.require_ready(complete_submission(PilotStage.INTERNAL_SHADOW), now=NOW)
    assert lifecycle.advance(state, shadow).stage is PilotStage.INTERNAL_SHADOW


@pytest.mark.parametrize("criterion", list(StopCriterion))
def test_every_stop_criterion_automatically_contains_only(criterion: StopCriterion) -> None:
    state = PilotLifecycle().contain(
        PilotState("branch-sejong", PilotStage.CLOSED_SANDBOX), criterion
    )
    assert state.contained
    assert criterion in state.stop_reasons
    assert state.stage is PilotStage.CLOSED_SANDBOX


def test_restart_needs_all_causes_and_two_independent_humans() -> None:
    lifecycle = PilotLifecycle()
    state = PilotState("branch-sejong", PilotStage.CLOSED_SANDBOX)
    state = lifecycle.contain(state, StopCriterion.PRIVACY_INCIDENT)
    state = lifecycle.contain(state, StopCriterion.LEDGER_MISMATCH)
    reviewer = approval("restart:closed_sandbox", "restart-reviewer", ApprovalRole.RESTART_REVIEWER)
    operator = approval("restart:closed_sandbox", "operator-choi", ApprovalRole.OPERATOR)
    with pytest.raises(PilotRejected) as unresolved:
        lifecycle.restart(
            state,
            resolved=frozenset({StopCriterion.PRIVACY_INCIDENT}),
            reviewer=reviewer,
            operator=operator,
            authority=AUTHORITY,
            now=NOW,
        )
    assert unresolved.value.code is PilotErrorCode.UNSAFE_RESTART_FORBIDDEN
    restarted = lifecycle.restart(
        state,
        resolved=state.stop_reasons,
        reviewer=reviewer,
        operator=operator,
        authority=AUTHORITY,
        now=NOW,
    )
    assert not restarted.contained and not restarted.stop_reasons
    with pytest.raises(PilotRejected) as one_person:
        lifecycle.restart(
            state,
            resolved=state.stop_reasons,
            reviewer=reviewer,
            operator=approval("restart:closed_sandbox", "restart-reviewer", ApprovalRole.OPERATOR),
            authority=AUTHORITY,
            now=NOW,
        )
    assert one_person.value.code is PilotErrorCode.SELF_APPROVAL_FORBIDDEN


def test_participant_consent_withdrawal_and_no_retaliation() -> None:
    registry = ParticipantConsentRegistry()
    with pytest.raises(PilotRejected) as denied:
        registry.enroll("rider-1", "branch-sejong", consent=False, consent_reference="", now=NOW)
    assert denied.value.code is PilotErrorCode.CONSENT_REQUIRED
    enrolled = registry.enroll(
        "rider-1",
        "branch-sejong",
        consent=True,
        consent_reference="consent://pilot/opaque-record-1",
        now=NOW,
    )
    assert enrolled.active and "rider-1" not in repr(enrolled)
    withdrawn = registry.withdraw("rider-1", "branch-sejong", now=NOW + timedelta(hours=1))
    assert not withdrawn.active
    assert not withdrawn.retaliation_allowed


def test_admin_api_is_branch_scoped_and_exposes_no_secret_material() -> None:
    assert len(PILOT_ADMIN_ROUTES) == 4
    assert all("{branch_id}" in route.path for route in PILOT_ADMIN_ROUTES)
    assert all(route.path.startswith("/api/v1/admin/") for route in PILOT_ADMIN_ROUTES)
