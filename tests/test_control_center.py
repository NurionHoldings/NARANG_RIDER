from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.control_center import (
    AggregateSnapshot,
    BranchLevel,
    BranchNode,
    CommandTarget,
    ControlError,
    ControlRejected,
    Corridor,
    EffectivePolicy,
    Impact,
    NationalControlCenter,
    NationalPolicy,
    OperatorCommand,
    ReadinessChecklist,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def center() -> NationalControlCenter:
    value = NationalControlCenter(national=NationalPolicy(
        4_000, 5, 7, 30, frozenset({"PAY_CUT", "ACCOUNT_SUSPEND", "DISPATCH_DENY"})
    ))
    value.register_branch(BranchNode("hq", BranchLevel.HQ, None, frozenset({"KR"}), True))
    value.register_branch(BranchNode("central", BranchLevel.REGIONAL, "hq", frozenset({"C-1"}), True))
    value.register_branch(BranchNode("sejong", BranchLevel.LOCAL, "central", frozenset({"S-1"})))
    value.register_branch(BranchNode("daejeon", BranchLevel.LOCAL, "central", frozenset({"D-1"})))
    return value


def test_readiness_hierarchy_and_scope_idor() -> None:
    c = center()
    with pytest.raises(ControlRejected) as incomplete:
        c.activate(ReadinessChecklist("sejong", True, True, True, False, True, True), actor_id="hq-op")
    assert incomplete.value.code is ControlError.READINESS_INCOMPLETE
    assert c.activate(ReadinessChecklist("sejong", True, True, True, True, True, True), actor_id="hq-op").active
    c.authorize(actor_branch_id="central", target_branch_id="sejong")
    with pytest.raises(ControlRejected) as denied:
        c.authorize(actor_branch_id="sejong", target_branch_id="daejeon")
    assert denied.value.code is ControlError.ACCESS_DENIED


def test_aggregate_dashboard_blocks_reidentification_and_individual_ranking() -> None:
    c = center()
    small = AggregateSnapshot("sejong", 4, 2, 2, 10, 1, 0, 3, 5_000)
    with pytest.raises(ControlRejected) as hidden:
        c.dashboard(actor_branch_id="hq", snapshot=small)
    assert hidden.value.code is ControlError.SMALL_COHORT_FORBIDDEN
    visible = AggregateSnapshot("sejong", 5, 3, 2, 10, 1, 0, 3, 5_000)
    assert c.dashboard(actor_branch_id="central", snapshot=visible) == visible
    assert not hasattr(visible, "rider_ranking")


def policy(**changes: object) -> EffectivePolicy:
    values = {
        "policy_id": "p2", "branch_id": "sejong", "version": 2, "effective_at": NOW,
        "minimum_rider_pay_won": 4_500, "minimum_cohort": 6,
        "settlement_days_max": 5, "evidence_retention_days_max": 20,
        "ai_forbidden_powers": frozenset({"PAY_CUT", "ACCOUNT_SUSPEND", "DISPATCH_DENY"}),
    }
    values.update(changes)
    return EffectivePolicy(**values)


@pytest.mark.parametrize("changes", [
    {"minimum_rider_pay_won": 3_999}, {"minimum_cohort": 4},
    {"settlement_days_max": 8}, {"evidence_retention_days_max": 31},
    {"ai_forbidden_powers": frozenset({"PAY_CUT"})},
])
def test_branch_policy_cannot_weaken_national_floor(changes: dict[str, object]) -> None:
    c = center()
    with pytest.raises(ControlRejected) as weak:
        c.set_policy(value=policy(**changes), expected_version=1, actor_id="policy-op")
    assert weak.value.code is ControlError.POLICY_WEAKENING_FORBIDDEN


def test_effective_dated_policy_stale_version_and_rollback_history() -> None:
    c = center()
    assert c.set_policy(value=policy(), expected_version=1, actor_id="op").version == 2
    with pytest.raises(ControlRejected) as stale:
        c.set_policy(value=policy(policy_id="stale", version=3), expected_version=1, actor_id="op")
    assert stale.value.code is ControlError.STALE_VERSION
    rollback = policy(policy_id="rollback-national", version=3, effective_at=NOW + timedelta(days=1))
    c.set_policy(value=rollback, expected_version=2, actor_id="op")
    assert c.policy_at("sejong", NOW).policy_id == "p2"
    assert c.policy_at("sejong", NOW + timedelta(days=2)).policy_id == "rollback-national"


def command(**changes: object) -> OperatorCommand:
    values = {
        "command_id": "cmd-1", "branch_id": "sejong", "target": CommandTarget.OUTBOX,
        "impact": Impact.FINANCIAL, "reason": "provider mismatch", "ticket_ref": "INC-100",
        "requested_by": "requester", "expected_branch_version": 1, "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return OperatorCommand(**values)


def test_operator_command_dual_control_expiry_and_overbroad_abuse() -> None:
    c = center()
    c.request_command(command())
    with pytest.raises(ControlRejected) as self_approve:
        c.approve_command("cmd-1", approver_id="requester")
    assert self_approve.value.code is ControlError.DUAL_APPROVAL_REQUIRED
    assert not c.approve_command("cmd-1", approver_id="reviewer-1").executed
    assert c.approve_command("cmd-1", approver_id="reviewer-2").executed
    assert c.expire_emergency(now=NOW + timedelta(hours=2)) == ("cmd-1",)
    assert c.audit[-1].action == "EMERGENCY_EXPIRED_REVIEW_REQUIRED"
    with pytest.raises(ControlRejected) as broad:
        c.request_command(command(command_id="cmd-2", expires_at=NOW + timedelta(days=1)))
    assert broad.value.code is ControlError.OVERBROAD_COMMAND


def test_corridor_requires_both_branches_and_rejects_raw_pii() -> None:
    c = center()
    c.add_corridor(Corridor("corridor-1", "sejong", "daejeon", frozenset({"LINK-1"})))
    c.approve_corridor("corridor-1", branch_id="sejong")
    with pytest.raises(ControlRejected) as one_side:
        c.transfer("corridor-1", from_branch_id="sejong", to_branch_id="daejeon",
                   zone_id="LINK-1", payload={"order_count": 1})
    assert one_side.value.code is ControlError.CORRIDOR_APPROVAL_REQUIRED
    c.approve_corridor("corridor-1", branch_id="daejeon")
    with pytest.raises(ControlRejected) as pii:
        c.transfer("corridor-1", from_branch_id="sejong", to_branch_id="daejeon",
                   zone_id="LINK-1", payload={"address": "forbidden"})
    assert pii.value.code is ControlError.RAW_PII_FORBIDDEN
    c.transfer("corridor-1", from_branch_id="sejong", to_branch_id="daejeon",
               zone_id="LINK-1", payload={"order_ref": "opaque:1"})


def test_arkaon_is_aggregate_advisory_only_and_events_are_atomic_pairs() -> None:
    c = center()
    c.arkaon("AGGREGATE_FORECAST")
    c.arkaon("RECOMMEND")
    with pytest.raises(ControlRejected) as forbidden:
        c.arkaon("PAUSE_DISPATCH")
    assert forbidden.value.code is ControlError.AI_AUTHORITY_FORBIDDEN
    c.activate(ReadinessChecklist("sejong", True, True, True, True, True, True), actor_id="op")
    assert c.audit == c.outbox
