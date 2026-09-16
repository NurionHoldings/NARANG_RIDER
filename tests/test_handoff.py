from pathlib import Path

from narang_rider.handoff import (
    RUNBOOKS,
    DrillAction,
    IncidentSeverity,
    OnCallRole,
    ScenarioKind,
    TabletopEngine,
    verify_handoff_references,
)


def approve(actor: str, role: OnCallRole, branch: str = "sejong-local") -> DrillAction:
    return DrillAction(actor, role, branch, "APPROVE_RECOVERY")


def test_inventory_has_complete_executable_runbooks_and_fresh_references() -> None:
    assert set(RUNBOOKS) == set(ScenarioKind)
    assert all(item.detection_signals and item.contain_actions and item.forbidden_actions
               and item.approval_roles and item.evidence_refs and item.recovery_criteria
               and item.postmortem_fields for item in RUNBOOKS.values())
    assert any(item.severity is IncidentSeverity.SEV1 for item in RUNBOOKS.values())
    assert verify_handoff_references(Path(".")) == ()


def test_financial_recovery_requires_separate_humans_and_never_claws_back() -> None:
    engine = TabletopEngine()
    valid = engine.evaluate(
        scenario=ScenarioKind.SETTLEMENT_PAYOUT, incident_branch_id="sejong-local",
        actions=(approve("finance-a", OnCallRole.FINANCE_REVIEWER),
                 approve("operator-b", OnCallRole.OPERATOR_APPROVER)),
    )
    assert valid.passed
    same_person = engine.evaluate(
        scenario=ScenarioKind.SETTLEMENT_PAYOUT, incident_branch_id="sejong-local",
        actions=(approve("one", OnCallRole.FINANCE_REVIEWER),
                 approve("one", OnCallRole.OPERATOR_APPROVER)),
    )
    assert "SELF_OR_DUPLICATE_APPROVAL" in same_person.violations
    clawback = engine.evaluate(
        scenario=ScenarioKind.SETTLEMENT_PAYOUT, incident_branch_id="sejong-local",
        actions=(DrillAction("finance", OnCallRole.FINANCE_REVIEWER,
                             "sejong-local", "RIDER_CLAWBACK"),),
    )
    assert "AUTOMATIC_RIDER_CLAWBACK" in clawback.violations


def test_branch_scope_ai_restart_and_fallback_privacy_fail_closed() -> None:
    engine = TabletopEngine()
    scoped = engine.evaluate(
        scenario=ScenarioKind.EVIDENCE_MEDIA, incident_branch_id="sejong-local",
        actions=(DrillAction("local", OnCallRole.LOCAL_OPERATOR, "daejeon-local", "CONTAIN"),),
    )
    assert "BRANCH_SCOPE_VIOLATION" in scoped.violations
    ai = engine.evaluate(
        scenario=ScenarioKind.ROLLBACK, incident_branch_id="sejong-local",
        actions=(DrillAction("arkaon", OnCallRole.ARKAON_ADVISOR,
                             "sejong-local", "RESTART"),),
    )
    assert "AI_UNILATERAL_AUTHORITY" in ai.violations
    fallback = engine.evaluate(
        scenario=ScenarioKind.MAP_NOTIFICATION, incident_branch_id="sejong-local",
        actions=(DrillAction("regional", OnCallRole.REGIONAL_OPERATOR, "sejong",
                             "MINIMAL_FALLBACK_NOTICE", frozenset({"address"})),),
        regional_prefix="sejong",
    )
    assert "FALLBACK_PRIVACY_VIOLATION" in fallback.violations


def test_every_drill_with_actions_requires_declared_approval_roles() -> None:
    engine = TabletopEngine()
    for scenario in ScenarioKind:
        result = engine.evaluate(
            scenario=scenario, incident_branch_id="sejong-local",
            actions=(DrillAction("local", OnCallRole.LOCAL_OPERATOR,
                                 "sejong-local", "CONTAIN"),),
            regional_prefix="sejong",
        )
        assert "APPROVALS_INCOMPLETE" in result.violations
