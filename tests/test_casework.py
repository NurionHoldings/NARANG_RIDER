from datetime import UTC, datetime

import pytest

from narang_rider.casework import (
    CASEWORK_ROUTE_MANIFEST,
    ActionInstruction,
    AgentWorkload,
    ArkaonCaseAdvice,
    CaseErrorCode,
    CaseEvidence,
    CaseMessage,
    CasePrincipal,
    CaseRejected,
    CaseSeverity,
    CaseState,
    CaseType,
    CaseworkService,
    ParticipantRole,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def person(who="customer-1", role=ParticipantRole.CUSTOMER, branch="sejong"):
    return CasePrincipal(who, role, branch)


def staff(who="agent-1", branch="sejong"):
    return person(who, ParticipantRole.SUPPORT, branch)


def opened(case_type=CaseType.DELIVERY):
    service = CaseworkService()
    case = service.open_case(case_id="case-1", principal=person(), case_type=case_type,
        severity=CaseSeverity.STANDARD, participant_ids=["customer-1", "rider-1"],
        resource_ref="resource://order/order-123", idempotency_key="open-1", now=NOW)
    return service, case


def assigned():
    service, case = opened()
    service.assign(case.case_id, [AgentWorkload("agent-1", "sejong", 2, frozenset())],
                   actor=staff("dispatcher"), expected_version=1, now=NOW)
    return service, case


def evidence(eid, submitter="customer-1", subjects=frozenset({"customer-1"})):
    return CaseEvidence(eid, submitter, subjects, f"vault://sanitized-evidence/{eid}-12345",
                        "0123456789abcdef", NOW)


def test_all_case_types_and_routes_exist():
    assert len(CaseType) == 10
    assert any("/appeals" in route for route in CASEWORK_ROUTE_MANIFEST)
    assert any("support-ops" in route for route in CASEWORK_ROUTE_MANIFEST)


def test_open_is_owned_branch_scoped_and_idempotent():
    service, case = opened()
    replay = service.open_case(case_id="other", principal=person(), case_type=CaseType.DAMAGE,
        severity=CaseSeverity.CRITICAL, participant_ids=["customer-1"],
        resource_ref="resource://order/other-123", idempotency_key="open-1", now=NOW)
    assert replay is case
    with pytest.raises(CaseRejected) as error:
        service.get(case.case_id, person("customer-1", branch="busan"))
    assert error.value.code is CaseErrorCode.NOT_FOUND


def test_forged_representative_is_blocked():
    service = CaseworkService()
    with pytest.raises(CaseRejected):
        service.open_case(case_id="x", principal=person("attacker"), case_type=CaseType.REFUND,
            severity=CaseSeverity.STANDARD, participant_ids=["victim"],
            resource_ref="resource://order/order-123", idempotency_key="x", now=NOW)


def test_messages_are_append_only_versioned_and_internal_notes_never_leak():
    service, case = opened()
    service.append_message(case.case_id,
        CaseMessage(1, "customer-1", ParticipantRole.CUSTOMER,
                    "vault://case-message/message-123", NOW),
        principal=person(), expected_version=1)
    service.append_message(case.case_id,
        CaseMessage(2, "agent-1", ParticipantRole.SUPPORT,
                    "vault://internal-note/note-12345", NOW, True),
        principal=staff(), expected_version=2)
    view = service.participant_view(case.case_id, person())
    assert view.message_refs == ("vault://case-message/message-123",)
    with pytest.raises(CaseRejected) as error:
        service.append_message(case.case_id,
            CaseMessage(3, "customer-1", ParticipantRole.CUSTOMER,
                        "vault://case-message/next-123", NOW),
            principal=person(), expected_version=1)
    assert error.value.code is CaseErrorCode.VERSION_CONFLICT


def test_participant_view_masks_other_party_and_cross_party_evidence():
    service, case = opened()
    service.append_evidence(case.case_id, evidence("ev1"), principal=person(), expected_version=1)
    rider = person("rider-1", ParticipantRole.RIDER)
    view = service.participant_view(case.case_id, rider)
    assert "customer-1" not in view.participant_aliases
    assert view.evidence_refs == ()


def test_forged_or_duplicate_evidence_is_rejected():
    service, case = opened()
    with pytest.raises(CaseRejected):
        service.append_evidence(case.case_id, evidence("ev1", submitter="attacker"),
                                principal=person(), expected_version=1)
    service.append_evidence(case.case_id, evidence("ev1"), principal=person(), expected_version=1)
    with pytest.raises(CaseRejected) as error:
        service.append_evidence(case.case_id, evidence("ev1"), principal=person(), expected_version=2)
    assert error.value.code is CaseErrorCode.DUPLICATE_ACTION


def test_assignment_uses_lowest_workload_without_performance_ranking():
    service, case = opened()
    service.assign(case.case_id, [
        AgentWorkload("agent-b", "sejong", 1, frozenset()),
        AgentWorkload("agent-a", "sejong", 1, frozenset()),
        AgentWorkload("agent-z", "sejong", 0, frozenset(), unavailable=True)],
        actor=staff("dispatcher"), expected_version=1, now=NOW)
    assert case.assigned_agent_id == "agent-a"


def test_premature_close_ai_resolution_and_missing_rights_notice_fail():
    service, case = assigned()
    service.transition(case.case_id, CaseState.REVIEW, actor=staff(),
                       expected_version=case.version, now=NOW)
    with pytest.raises(CaseRejected) as error:
        service.transition(case.case_id, CaseState.RESOLVED, actor=staff(),
            expected_version=case.version, now=NOW, rationale_ref="vault://human-rationale/r1",
            rights_notice_ref="notice://rights/v1", arkaon_initiated=True)
    assert error.value.code is CaseErrorCode.HUMAN_DECISION_REQUIRED
    with pytest.raises(CaseRejected):
        service.transition(case.case_id, CaseState.RESOLVED, actor=staff(),
            expected_version=case.version, now=NOW, rationale_ref="vault://human-rationale/r1")


def test_human_resolution_and_independent_appeal_review():
    service, case = assigned()
    service.transition(case.case_id, CaseState.REVIEW, actor=staff(),
                       expected_version=case.version, now=NOW)
    service.transition(case.case_id, CaseState.RESOLVED, actor=staff(),
        expected_version=case.version, now=NOW, rationale_ref="vault://human-rationale/reason-1",
        rights_notice_ref="notice://rights/v1")
    service.appeal(case.case_id, principal=person(), appeal_ref="vault://appeal/appeal-123",
                   expected_version=case.version, now=NOW)
    with pytest.raises(CaseRejected) as error:
        service.assign_appeal_reviewer(case.case_id,
            AgentWorkload("agent-1", "sejong", 1, frozenset()), actor=staff("dispatcher"), now=NOW)
    assert error.value.code is CaseErrorCode.CONFLICT_OF_INTEREST


def test_financial_actions_are_unexecuted_dual_control_and_idempotent():
    service, case = opened(CaseType.REFUND)
    first = service.issue_action_instruction(case.case_id, actor=staff(), kind="provisional_credit",
        amount_minor=5000, currency="KRW", beneficiary_ref="vault://beneficiary/ref-1",
        idempotency_key="credit-1")
    replay = service.issue_action_instruction(case.case_id, actor=staff(), kind="provisional_credit",
        amount_minor=5000, currency="KRW", beneficiary_ref="vault://beneficiary/ref-1",
        idempotency_key="credit-1")
    assert first is replay and first.requires_dual_control and not first.executed


def test_rider_pay_clawback_waits_for_final_fault():
    service, case = opened(CaseType.DAMAGE)
    with pytest.raises(CaseRejected) as error:
        service.issue_action_instruction(case.case_id, actor=staff(), kind="rider_pay_clawback",
            amount_minor=1000, currency="KRW", beneficiary_ref="vault://rider/r1",
            idempotency_key="clawback-1")
    assert error.value.code is CaseErrorCode.HUMAN_DECISION_REQUIRED


def test_photo_comparison_never_decides_and_repeated_signal_is_priority_only():
    service, _ = opened()
    result = service.review_evidence(evidence("before"), evidence("after"), human_reviewer_id="human")
    assert result["disposition"] == "human_review_required"
    advice = service.accept_arkaon_advice(ArkaonCaseAdvice(
        "vault://summary/1", CaseType.DAMAGE, ("seal_status",), True))
    assert advice.repeated_or_linked_priority and advice.authority == "advisory_only"


def test_arkaon_elevated_authority_is_rejected():
    with pytest.raises(CaseRejected) as error:
        CaseworkService.accept_arkaon_advice(ArkaonCaseAdvice(
            "vault://summary/1", CaseType.REFUND, (), authority="resolve"))
    assert error.value.code is CaseErrorCode.HUMAN_DECISION_REQUIRED


def test_action_instruction_type_documents_no_direct_payment():
    fields = ActionInstruction.__dataclass_fields__
    assert fields["executed"].default is False
