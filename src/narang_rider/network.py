from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class BranchType(StrEnum):
    HEADQUARTERS = "HEADQUARTERS"
    REGIONAL_BRANCH = "REGIONAL_BRANCH"
    LOCAL_HUB = "LOCAL_HUB"


class BranchStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True)
class ServiceArea:
    area_code: str
    province_code: str
    municipality_code: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.area_code, self.province_code, self.municipality_code)
        ):
            raise ValueError("SERVICE_AREA_IDENTITY_REQUIRED")


@dataclass(frozen=True)
class Branch:
    branch_id: str
    branch_type: BranchType
    name: str
    parent_branch_id: str | None
    status: BranchStatus
    service_areas: tuple[ServiceArea, ...]
    policy_version: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.branch_id.strip() or not self.name.strip() or self.created_at.tzinfo is None:
            raise ValueError("BRANCH_IDENTITY_REQUIRED")
        if self.policy_version < 1:
            raise ValueError("BRANCH_POLICY_VERSION_REQUIRED")
        if self.branch_type is BranchType.HEADQUARTERS and self.parent_branch_id is not None:
            raise ValueError("HEADQUARTERS_CANNOT_HAVE_PARENT")
        if self.branch_type is not BranchType.HEADQUARTERS and not self.parent_branch_id:
            raise ValueError("NON_HEADQUARTERS_PARENT_REQUIRED")


class BranchRegistry:
    def __init__(self) -> None:
        self._branches: dict[str, Branch] = {}
        self._area_owner: dict[str, str] = {}

    def register(self, branch: Branch) -> Branch:
        if branch.branch_id in self._branches:
            raise ValueError("DUPLICATE_BRANCH")
        if branch.parent_branch_id is not None:
            parent = self.get(branch.parent_branch_id)
            if branch.branch_type is BranchType.REGIONAL_BRANCH:
                if parent.branch_type is not BranchType.HEADQUARTERS:
                    raise ValueError("REGIONAL_BRANCH_REQUIRES_HEADQUARTERS")
            elif parent.branch_type is not BranchType.REGIONAL_BRANCH:
                raise ValueError("LOCAL_HUB_REQUIRES_REGIONAL_BRANCH")
        for area in branch.service_areas:
            if area.area_code in self._area_owner:
                raise ValueError("OVERLAPPING_SERVICE_AREA")
        self._branches[branch.branch_id] = branch
        for area in branch.service_areas:
            self._area_owner[area.area_code] = branch.branch_id
        return branch

    def activate(self, *, branch_id: str, expected_policy_version: int) -> Branch:
        branch = self.get(branch_id)
        if branch.policy_version != expected_policy_version:
            raise ValueError("BRANCH_POLICY_VERSION_CONFLICT")
        if branch.status is BranchStatus.SUSPENDED:
            raise ValueError("SUSPENDED_BRANCH_REQUIRES_HUMAN_REINSTATEMENT")
        activated = replace(branch, status=BranchStatus.ACTIVE)
        self._branches[branch_id] = activated
        return activated

    def get(self, branch_id: str) -> Branch:
        try:
            return self._branches[branch_id]
        except KeyError as exc:
            raise ValueError("BRANCH_NOT_FOUND") from exc

    def branch_for_area(self, area_code: str) -> Branch:
        try:
            return self.get(self._area_owner[area_code])
        except KeyError as exc:
            raise ValueError("UNSERVED_AREA") from exc

    def descendants(self, branch_id: str) -> tuple[str, ...]:
        descendants: list[str] = []
        pending = [branch_id]
        while pending:
            parent = pending.pop()
            children = sorted(
                branch.branch_id
                for branch in self._branches.values()
                if branch.parent_branch_id == parent
            )
            descendants.extend(children)
            pending.extend(children)
        return tuple(descendants)


class PrincipalRole(StrEnum):
    HQ_AUDITOR = "HQ_AUDITOR"
    BRANCH_ADMIN = "BRANCH_ADMIN"
    HUB_OPERATOR = "HUB_OPERATOR"
    ARKAON_ANALYST = "ARKAON_ANALYST"


@dataclass(frozen=True)
class ScopedPrincipal:
    principal_id: str
    role: PrincipalRole
    branch_id: str
    active: bool = True


class ResourceKind(StrEnum):
    AGGREGATE_METRIC = "AGGREGATE_METRIC"
    ORDER = "ORDER"
    RIDER_PROFILE = "RIDER_PROFILE"
    MERCHANT_PROFILE = "MERCHANT_PROFILE"
    SETTLEMENT = "SETTLEMENT"


class BranchScopeAuthorizer:
    def __init__(self, *, registry: BranchRegistry) -> None:
        self._registry = registry

    def authorize_read(
        self,
        *,
        principal: ScopedPrincipal,
        resource_branch_id: str,
        resource_kind: ResourceKind,
        contains_personal_data: bool,
    ) -> None:
        if not principal.active:
            raise ValueError("INACTIVE_PRINCIPAL")
        self._registry.get(principal.branch_id)
        self._registry.get(resource_branch_id)
        if principal.role is PrincipalRole.ARKAON_ANALYST:
            if resource_kind is not ResourceKind.AGGREGATE_METRIC or contains_personal_data:
                raise ValueError("ARKAON_AGGREGATE_ONLY")
            return
        if principal.role is PrincipalRole.HQ_AUDITOR:
            if contains_personal_data:
                raise ValueError("HQ_PERSONAL_DATA_REQUIRES_CASE_GRANT")
            return
        permitted = {principal.branch_id, *self._registry.descendants(principal.branch_id)}
        if resource_branch_id not in permitted:
            raise ValueError("CROSS_BRANCH_ACCESS_DENIED")

    def authorize_write(
        self,
        *,
        principal: ScopedPrincipal,
        resource_branch_id: str,
    ) -> None:
        if not principal.active:
            raise ValueError("INACTIVE_PRINCIPAL")
        if principal.role in {PrincipalRole.HQ_AUDITOR, PrincipalRole.ARKAON_ANALYST}:
            raise ValueError("READ_ONLY_PRINCIPAL")
        if principal.branch_id != resource_branch_id:
            raise ValueError("CROSS_BRANCH_WRITE_DENIED")


@dataclass(frozen=True)
class InterBranchCorridor:
    corridor_id: str
    origin_branch_id: str
    destination_branch_id: str
    allowed_area_codes: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    approved_by_origin: str
    approved_by_destination: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if (
            self.origin_branch_id == self.destination_branch_id
            or self.valid_from.tzinfo is None
            or self.valid_until <= self.valid_from
            or not self.allowed_area_codes
            or self.approved_by_origin == self.approved_by_destination
        ):
            raise ValueError("INVALID_INTER_BRANCH_CORRIDOR")


class CorridorRegistry:
    def __init__(self, *, branches: BranchRegistry) -> None:
        self._branches = branches
        self._corridors: dict[str, InterBranchCorridor] = {}

    def register(self, corridor: InterBranchCorridor) -> InterBranchCorridor:
        if corridor.corridor_id in self._corridors:
            raise ValueError("DUPLICATE_CORRIDOR")
        origin = self._branches.get(corridor.origin_branch_id)
        destination = self._branches.get(corridor.destination_branch_id)
        if origin.status is not BranchStatus.ACTIVE or destination.status is not BranchStatus.ACTIVE:
            raise ValueError("ACTIVE_BRANCHES_REQUIRED")
        self._corridors[corridor.corridor_id] = corridor
        return corridor

    def authorize_delivery(
        self,
        *,
        corridor_id: str,
        origin_branch_id: str,
        destination_branch_id: str,
        destination_area_code: str,
        now: datetime,
    ) -> None:
        try:
            corridor = self._corridors[corridor_id]
        except KeyError as exc:
            raise ValueError("CORRIDOR_NOT_FOUND") from exc
        if (
            not corridor.enabled
            or corridor.origin_branch_id != origin_branch_id
            or corridor.destination_branch_id != destination_branch_id
            or destination_area_code not in corridor.allowed_area_codes
            or not corridor.valid_from <= now < corridor.valid_until
        ):
            raise ValueError("INTER_BRANCH_DELIVERY_NOT_AUTHORIZED")


@dataclass(frozen=True)
class BranchLedgerAllocation:
    allocation_id: str
    order_id: str
    servicing_branch_id: str
    regional_branch_id: str
    headquarters_id: str
    servicing_branch_won: int
    regional_support_won: int
    headquarters_shared_cost_won: int

    def __post_init__(self) -> None:
        amounts = (
            self.servicing_branch_won,
            self.regional_support_won,
            self.headquarters_shared_cost_won,
        )
        if any(isinstance(amount, bool) or not isinstance(amount, int) for amount in amounts):
            raise TypeError("BRANCH_ALLOCATION_MUST_USE_INTEGER_WON")
        if any(amount < 0 for amount in amounts):
            raise ValueError("NEGATIVE_BRANCH_ALLOCATION")

    @property
    def total_won(self) -> int:
        return (
            self.servicing_branch_won
            + self.regional_support_won
            + self.headquarters_shared_cost_won
        )
