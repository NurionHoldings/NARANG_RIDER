from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .accuracy import AccuracyReport, NationalAccuracyVerdict
from .arkaon import ArkaonCapability, ArkaonProfile
from .network import BranchRegistry, BranchStatus


class RolloutStage(StrEnum):
    REGISTERED = "REGISTERED"
    SHADOW = "SHADOW"
    PILOT = "PILOT"
    REGIONAL = "REGIONAL"
    NATIONAL = "NATIONAL"
    PAUSED = "PAUSED"
    ROLLED_BACK = "ROLLED_BACK"


class OutputAuthority(StrEnum):
    ADVISORY = "ADVISORY"
    EXPLANATION = "EXPLANATION"
    FORECAST = "FORECAST"


@dataclass(frozen=True)
class ArkaonRelease:
    release_id: str
    profile_id: str
    capability: ArkaonCapability
    artifact_digest: str
    fallback_policy_id: str
    output_authority: OutputAuthority
    created_at: datetime
    stage: RolloutStage = RolloutStage.REGISTERED
    paused_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                value.strip()
                for value in (
                    self.release_id,
                    self.profile_id,
                    self.artifact_digest,
                    self.fallback_policy_id,
                )
            )
            or len(self.artifact_digest) != 64
            or self.created_at.tzinfo is None
        ):
            raise ValueError("VALID_ARKAON_RELEASE_REQUIRED")


@dataclass(frozen=True)
class BranchDeployment:
    release_id: str
    branch_id: str
    stage: RolloutStage
    fallback_policy_id: str
    started_at: datetime
    accuracy_report: AccuracyReport | None = None
    ethernian_reviewer: str | None = None
    branch_approver: str | None = None
    rollback_reason: str | None = None


class ArkaonRolloutController:
    """Staged branch rollout with explicit review, fallback, and kill switch."""

    def __init__(self, *, branches: BranchRegistry, profile: ArkaonProfile) -> None:
        self._branches = branches
        self._profile = profile
        self._releases: dict[str, ArkaonRelease] = {}
        self._deployments: dict[tuple[str, str], BranchDeployment] = {}

    def register(self, release: ArkaonRelease) -> ArkaonRelease:
        if release.release_id in self._releases:
            raise ValueError("DUPLICATE_ARKAON_RELEASE")
        if release.profile_id != self._profile.profile_id:
            raise ValueError("RELEASE_PROFILE_MISMATCH")
        if release.capability not in self._profile.capabilities:
            raise ValueError("RELEASE_CAPABILITY_NOT_GRANTED")
        self._releases[release.release_id] = release
        return release

    def start_shadow(
        self,
        *,
        release_id: str,
        branch_id: str,
        now: datetime,
    ) -> BranchDeployment:
        release = self.get_release(release_id)
        branch = self._branches.get(branch_id)
        if branch.status is not BranchStatus.ACTIVE:
            raise ValueError("ACTIVE_BRANCH_REQUIRED")
        key = (release_id, branch_id)
        if key in self._deployments:
            raise ValueError("DUPLICATE_BRANCH_DEPLOYMENT")
        deployment = BranchDeployment(
            release_id=release_id,
            branch_id=branch_id,
            stage=RolloutStage.SHADOW,
            fallback_policy_id=release.fallback_policy_id,
            started_at=now,
        )
        self._deployments[key] = deployment
        if release.stage is RolloutStage.REGISTERED:
            self._releases[release_id] = replace(release, stage=RolloutStage.SHADOW)
        return deployment

    def promote_pilot(
        self,
        *,
        release_id: str,
        branch_id: str,
        report: AccuracyReport,
        ethernian_reviewer: str,
        branch_approver: str,
    ) -> BranchDeployment:
        deployment = self.get_deployment(release_id, branch_id)
        if deployment.stage is not RolloutStage.SHADOW:
            raise ValueError("SHADOW_STAGE_REQUIRED")
        if not report.passed:
            raise ValueError("BRANCH_ACCURACY_GATE_FAILED")
        if ethernian_reviewer == branch_approver:
            raise ValueError("INDEPENDENT_ROLLOUT_APPROVAL_REQUIRED")
        promoted = replace(
            deployment,
            stage=RolloutStage.PILOT,
            accuracy_report=report,
            ethernian_reviewer=ethernian_reviewer,
            branch_approver=branch_approver,
        )
        self._deployments[(release_id, branch_id)] = promoted
        self._releases[release_id] = replace(
            self.get_release(release_id),
            stage=RolloutStage.PILOT,
        )
        return promoted

    def promote_regional(
        self,
        *,
        release_id: str,
        regional_branch_id: str,
        required_local_hub_ids: tuple[str, ...],
    ) -> ArkaonRelease:
        descendants = set(self._branches.descendants(regional_branch_id))
        required = set(required_local_hub_ids)
        if not required or not required.issubset(descendants):
            raise ValueError("INVALID_REGIONAL_ROLLOUT_SCOPE")
        deployments = [
            self.get_deployment(release_id, branch_id)
            for branch_id in sorted(required)
        ]
        if any(
            item.stage is not RolloutStage.PILOT
            or item.accuracy_report is None
            or not item.accuracy_report.passed
            for item in deployments
        ):
            raise ValueError("ALL_LOCAL_HUB_PILOTS_MUST_PASS")
        release = replace(self.get_release(release_id), stage=RolloutStage.REGIONAL)
        self._releases[release_id] = release
        return release

    def promote_national(
        self,
        *,
        release_id: str,
        verdict: NationalAccuracyVerdict,
        ethernian_reviewer: str,
        operator_approver: str,
    ) -> ArkaonRelease:
        release = self.get_release(release_id)
        if release.stage is not RolloutStage.REGIONAL:
            raise ValueError("REGIONAL_STAGE_REQUIRED")
        if not verdict.passed:
            raise ValueError("NATIONAL_ACCURACY_GATE_FAILED")
        if ethernian_reviewer == operator_approver:
            raise ValueError("INDEPENDENT_NATIONAL_APPROVAL_REQUIRED")
        for branch_id in verdict.required_branch_ids:
            deployment = self.get_deployment(release_id, branch_id)
            if (
                deployment.stage is not RolloutStage.PILOT
                or deployment.accuracy_report is None
                or not deployment.accuracy_report.passed
            ):
                raise ValueError("REQUIRED_BRANCH_NOT_PILOT_VERIFIED")
        national = replace(release, stage=RolloutStage.NATIONAL)
        self._releases[release_id] = national
        return national

    def kill_switch(self, *, release_id: str, reason: str) -> ArkaonRelease:
        if not reason.strip():
            raise ValueError("KILL_SWITCH_REASON_REQUIRED")
        release = replace(
            self.get_release(release_id),
            stage=RolloutStage.PAUSED,
            paused_reason=reason,
        )
        self._releases[release_id] = release
        for key, deployment in tuple(self._deployments.items()):
            if key[0] == release_id and deployment.stage is not RolloutStage.ROLLED_BACK:
                self._deployments[key] = replace(
                    deployment,
                    stage=RolloutStage.PAUSED,
                    rollback_reason=reason,
                )
        return release

    def rollback_branch(
        self,
        *,
        release_id: str,
        branch_id: str,
        reason: str,
    ) -> BranchDeployment:
        if not reason.strip():
            raise ValueError("ROLLBACK_REASON_REQUIRED")
        deployment = replace(
            self.get_deployment(release_id, branch_id),
            stage=RolloutStage.ROLLED_BACK,
            rollback_reason=reason,
        )
        self._deployments[(release_id, branch_id)] = deployment
        return deployment

    def get_release(self, release_id: str) -> ArkaonRelease:
        try:
            return self._releases[release_id]
        except KeyError as exc:
            raise ValueError("ARKAON_RELEASE_NOT_FOUND") from exc

    def get_deployment(self, release_id: str, branch_id: str) -> BranchDeployment:
        try:
            return self._deployments[(release_id, branch_id)]
        except KeyError as exc:
            raise ValueError("BRANCH_DEPLOYMENT_NOT_FOUND") from exc
