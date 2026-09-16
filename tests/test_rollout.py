from dataclasses import replace
from datetime import UTC, datetime

import pytest

from narang_rider.accuracy import (
    AccuracyReport,
    AccuracyTask,
    NationalAccuracyVerdict,
)
from narang_rider.arkaon import (
    ArkaonCapability,
    ArkaonProfile,
    ForbiddenAuthority,
)
from narang_rider.network import (
    Branch,
    BranchRegistry,
    BranchStatus,
    BranchType,
    ServiceArea,
)
from narang_rider.rollout import (
    ArkaonRelease,
    ArkaonRolloutController,
    OutputAuthority,
    RolloutStage,
)

NOW = datetime(2026, 9, 16, 7, 0, tzinfo=UTC)


def branches():
    registry = BranchRegistry()
    registry.register(
        Branch("hq", BranchType.HEADQUARTERS, "본사", None, BranchStatus.ACTIVE, (), 1, NOW)
    )
    registry.register(
        Branch(
            "central",
            BranchType.REGIONAL_BRANCH,
            "중부광역",
            "hq",
            BranchStatus.ACTIVE,
            (),
            1,
            NOW,
        )
    )
    for branch_id, area in (("seoul-hub", "SEOUL-1"), ("sejong-hub", "SEJONG-1")):
        registry.register(
            Branch(
                branch_id,
                BranchType.LOCAL_HUB,
                branch_id,
                "central",
                BranchStatus.ACTIVE,
                (ServiceArea(area, area.split("-")[0], area),),
                1,
                NOW,
            )
        )
    return registry


def profile():
    return ArkaonProfile(
        profile_id="nara-arkaon-v1",
        domain="NARANG_RIDER",
        version=1,
        capabilities=frozenset({ArkaonCapability.TRAVEL_TIME_ESTIMATE}),
        forbidden_authorities=frozenset(ForbiddenAuthority),
        created_at=NOW,
    )


def release():
    return ArkaonRelease(
        release_id="eta-release-1",
        profile_id="nara-arkaon-v1",
        capability=ArkaonCapability.TRAVEL_TIME_ESTIMATE,
        artifact_digest="a" * 64,
        fallback_policy_id="public-eta-v1",
        output_authority=OutputAuthority.FORECAST,
        created_at=NOW,
    )


def report(*, passed=True, branch_error=2):
    return AccuracyReport(
        task=AccuracyTask.TRAVEL_TIME,
        model_version="eta-v1",
        sample_count=200,
        mae=branch_error,
        signed_bias=0,
        p90_absolute_error=4,
        interval_coverage_bps=9_000,
        worst_segment_mae_ratio_bps=12_000,
        baseline_improvement_bps=2_000,
        duplicate_observations=0,
        passed=passed,
        failure_codes=() if passed else ("MAE_THRESHOLD_EXCEEDED",),
    )


def controller():
    value = ArkaonRolloutController(branches=branches(), profile=profile())
    value.register(release())
    return value


def pilot(value, branch_id):
    value.start_shadow(
        release_id="eta-release-1",
        branch_id=branch_id,
        now=NOW,
    )
    return value.promote_pilot(
        release_id="eta-release-1",
        branch_id=branch_id,
        report=report(),
        ethernian_reviewer="ethernian",
        branch_approver=f"{branch_id}-manager",
    )


def test_release_starts_in_shadow_with_public_fallback() -> None:
    value = controller()
    deployment = value.start_shadow(
        release_id="eta-release-1",
        branch_id="sejong-hub",
        now=NOW,
    )

    assert deployment.stage is RolloutStage.SHADOW
    assert deployment.fallback_policy_id == "public-eta-v1"
    assert value.get_release("eta-release-1").stage is RolloutStage.SHADOW


def test_failed_accuracy_or_same_person_approval_blocks_pilot() -> None:
    value = controller()
    value.start_shadow(
        release_id="eta-release-1",
        branch_id="sejong-hub",
        now=NOW,
    )
    with pytest.raises(ValueError, match="BRANCH_ACCURACY_GATE_FAILED"):
        value.promote_pilot(
            release_id="eta-release-1",
            branch_id="sejong-hub",
            report=report(passed=False),
            ethernian_reviewer="ethernian",
            branch_approver="branch-manager",
        )
    with pytest.raises(ValueError, match="INDEPENDENT_ROLLOUT_APPROVAL_REQUIRED"):
        value.promote_pilot(
            release_id="eta-release-1",
            branch_id="sejong-hub",
            report=report(),
            ethernian_reviewer="same-person",
            branch_approver="same-person",
        )


def test_regional_promotion_requires_every_selected_hub_pilot() -> None:
    value = controller()
    pilot(value, "sejong-hub")

    with pytest.raises(ValueError, match="BRANCH_DEPLOYMENT_NOT_FOUND"):
        value.promote_regional(
            release_id="eta-release-1",
            regional_branch_id="central",
            required_local_hub_ids=("sejong-hub", "seoul-hub"),
        )
    pilot(value, "seoul-hub")
    promoted = value.promote_regional(
        release_id="eta-release-1",
        regional_branch_id="central",
        required_local_hub_ids=("sejong-hub", "seoul-hub"),
    )
    assert promoted.stage is RolloutStage.REGIONAL


def test_national_promotion_requires_national_verdict_and_independent_approval() -> None:
    value = controller()
    pilot(value, "sejong-hub")
    pilot(value, "seoul-hub")
    value.promote_regional(
        release_id="eta-release-1",
        regional_branch_id="central",
        required_local_hub_ids=("sejong-hub", "seoul-hub"),
    )
    failed = NationalAccuracyVerdict(
        required_branch_ids=("sejong-hub", "seoul-hub"),
        evaluated_branch_ids=("sejong-hub", "seoul-hub"),
        missing_branch_ids=(),
        failed_branch_ids=("seoul-hub",),
        cross_branch_mae_ratio_bps=10_000,
        passed=False,
    )
    with pytest.raises(ValueError, match="NATIONAL_ACCURACY_GATE_FAILED"):
        value.promote_national(
            release_id="eta-release-1",
            verdict=failed,
            ethernian_reviewer="ethernian",
            operator_approver="operator-choi",
        )
    passed = replace(failed, failed_branch_ids=(), passed=True)
    national = value.promote_national(
        release_id="eta-release-1",
        verdict=passed,
        ethernian_reviewer="ethernian",
        operator_approver="operator-choi",
    )
    assert national.stage is RolloutStage.NATIONAL


def test_kill_switch_pauses_release_and_every_branch() -> None:
    value = controller()
    pilot(value, "sejong-hub")
    pilot(value, "seoul-hub")
    paused = value.kill_switch(
        release_id="eta-release-1",
        reason="coverage fell below the approved threshold",
    )

    assert paused.stage is RolloutStage.PAUSED
    assert value.get_deployment(
        "eta-release-1", "sejong-hub"
    ).stage is RolloutStage.PAUSED
    assert value.get_deployment(
        "eta-release-1", "seoul-hub"
    ).fallback_policy_id == "public-eta-v1"


def test_one_branch_can_rollback_without_disabling_other_branch() -> None:
    value = controller()
    pilot(value, "sejong-hub")
    pilot(value, "seoul-hub")
    rolled_back = value.rollback_branch(
        release_id="eta-release-1",
        branch_id="sejong-hub",
        reason="local drift",
    )

    assert rolled_back.stage is RolloutStage.ROLLED_BACK
    assert value.get_deployment(
        "eta-release-1", "seoul-hub"
    ).stage is RolloutStage.PILOT
