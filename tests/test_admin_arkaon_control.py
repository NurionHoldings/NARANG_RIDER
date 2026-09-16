from datetime import UTC, datetime
from hashlib import sha256

import pytest

from narang_rider.admin_arkaon_control import (
    AdminArkaonControl,
    AdminArkaonRejected,
    PartialChangeInstruction,
    ProposalStage,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)
HEAD = "73e0713b16305b0d7120fd20d20efa19ffb9e114"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def setup_control():
    control = AdminArkaonControl()
    control.start_session("session-1", operator_id="operator:choi", now=NOW)
    instruction = control.request_partial_change(
        PartialChangeInstruction(
            "instruction-1",
            "session-1",
            "operator:choi",
            "관리자 화면 문구 부분 수정",
            ("frontend/src/admin-arkaon-console.ts", "frontend/tests/admin-arkaon-console.test.mjs"),
            "승인 버튼 설명을 명확하게 한다",
            HEAD,
            NOW,
        )
    )
    return control, instruction


def proposal(control):
    return control.propose(
        proposal_id="proposal-1",
        instruction_id="instruction-1",
        source_head=HEAD,
        changed_paths=("frontend/src/admin-arkaon-console.ts",),
        summary="승인 전 미리보기 설명 보완",
        patch_digest=digest("patch"),
        test_plan=("frontend unit", "accessibility"),
        risk_notes=("운영반영 아님",),
        now=NOW,
    )


def test_operator_and_arkaon_can_converse_with_hash_chain():
    control, _ = setup_control()
    first = control.post_message(
        "session-1", message_id="m1", actor_type="OPERATOR", actor_ref="operator:choi",
        text="이 화면만 부분 수정해줘", now=NOW,
    )
    second = control.post_message(
        "session-1", message_id="m2", actor_type="ARKAON", actor_ref="arkaon",
        text="수정 범위와 시험안을 제시합니다.", now=NOW,
    )
    assert second.previous_digest == first.message_digest


def test_sensitive_message_and_replay_are_rejected():
    control, _ = setup_control()
    control.post_message(
        "session-1", message_id="m1", actor_type="OPERATOR", actor_ref="operator:choi",
        text="합성 지시", now=NOW,
    )
    with pytest.raises(AdminArkaonRejected, match="replay"):
        control.post_message(
            "session-1", message_id="m1", actor_type="OPERATOR", actor_ref="operator:choi",
            text="중복", now=NOW,
        )
    with pytest.raises(AdminArkaonRejected, match="safe"):
        control.post_message(
            "session-1", message_id="m2", actor_type="OPERATOR", actor_ref="operator:choi",
            text="900101-1234567", now=NOW,
        )


def test_proposal_must_stay_inside_operator_requested_paths_and_head():
    control, _ = setup_control()
    with pytest.raises(AdminArkaonRejected, match="bounded"):
        control.propose(
            proposal_id="bad", instruction_id="instruction-1", source_head=HEAD,
            changed_paths=("src/narang_rider/payments.py",), summary="범위 초과",
            patch_digest=digest("patch"), test_plan=("test",), risk_notes=("risk",), now=NOW,
        )


def test_every_mutation_refreshes_guidance_version_and_digest():
    control, _ = setup_control()
    value = proposal(control)
    assert value.guidance_version == 1 and value.guidance_digest
    value = control.preview("proposal-1", expected_version=1, now=NOW)
    assert value.guidance_version == 2
    value = control.approve(
        "proposal-1", operator_id="operator:choi", approval_digest=digest("approval"),
        expected_version=2, now=NOW,
    )
    assert value.guidance_version == 3
    value = control.apply_to_review_branch(
        "proposal-1", applied_evidence_digest=digest("applied"), expected_version=3, now=NOW
    )
    assert value.stage is ProposalStage.REVIEW_BRANCH_APPLIED
    assert value.guidance_version == 4
    assert [item.guidance_version for item in control.guidance("proposal-1")] == [1, 2, 3, 4]


def test_arkaon_cannot_self_approve_or_use_stale_version():
    control, _ = setup_control()
    proposal(control)
    control.preview("proposal-1", expected_version=1, now=NOW)
    with pytest.raises(AdminArkaonRejected, match="operator approval"):
        control.approve(
            "proposal-1", operator_id="ARKAON", approval_digest=digest("approval"),
            expected_version=2, now=NOW,
        )
    with pytest.raises(AdminArkaonRejected, match="stale"):
        control.preview("proposal-1", expected_version=1, now=NOW)


@pytest.mark.parametrize("path", [".env", "secrets/key", "../outside", "/etc/passwd"])
def test_secret_or_outside_paths_are_forbidden(path):
    control = AdminArkaonControl()
    control.start_session("s", operator_id="op", now=NOW)
    with pytest.raises(AdminArkaonRejected, match="allowlisted"):
        control.request_partial_change(
            PartialChangeInstruction("i", "s", "op", "scope", (path,), "change", HEAD, NOW)
        )


def test_review_branch_apply_does_not_authorize_production_merge_or_deploy():
    control, _ = setup_control()
    proposal(control)
    control.preview("proposal-1", expected_version=1, now=NOW)
    control.approve(
        "proposal-1", operator_id="operator:choi", approval_digest=digest("approval"),
        expected_version=2, now=NOW,
    )
    control.apply_to_review_branch(
        "proposal-1", applied_evidence_digest=digest("applied"), expected_version=3, now=NOW
    )
    with pytest.raises(AdminArkaonRejected, match="production"):
        control.production_mutation()
