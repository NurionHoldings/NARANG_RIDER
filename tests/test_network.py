from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.network import (
    Branch,
    BranchLedgerAllocation,
    BranchRegistry,
    BranchScopeAuthorizer,
    BranchStatus,
    BranchType,
    CorridorRegistry,
    InterBranchCorridor,
    PrincipalRole,
    ResourceKind,
    ScopedPrincipal,
    ServiceArea,
)

NOW = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


def branch(
    branch_id,
    branch_type,
    parent=None,
    *,
    areas=(),
    status=BranchStatus.ACTIVE,
):
    return Branch(
        branch_id=branch_id,
        branch_type=branch_type,
        name=branch_id,
        parent_branch_id=parent,
        status=status,
        service_areas=tuple(
            ServiceArea(code, code.split("-")[0], code) for code in areas
        ),
        policy_version=1,
        created_at=NOW,
    )


def national_registry():
    registry = BranchRegistry()
    registry.register(branch("hq", BranchType.HEADQUARTERS))
    registry.register(branch("central", BranchType.REGIONAL_BRANCH, "hq"))
    registry.register(branch("southeast", BranchType.REGIONAL_BRANCH, "hq"))
    registry.register(
        branch("sejong-hub", BranchType.LOCAL_HUB, "central", areas=("SJ-JIPHYEON",))
    )
    registry.register(
        branch("busan-hub", BranchType.LOCAL_HUB, "southeast", areas=("BS-HAEUNDAE",))
    )
    return registry


def test_branch_hierarchy_requires_hq_regional_and_local_order() -> None:
    registry = BranchRegistry()
    registry.register(branch("hq", BranchType.HEADQUARTERS))
    with pytest.raises(ValueError, match="LOCAL_HUB_REQUIRES_REGIONAL_BRANCH"):
        registry.register(branch("invalid-hub", BranchType.LOCAL_HUB, "hq"))
    registry.register(branch("region", BranchType.REGIONAL_BRANCH, "hq"))
    registry.register(branch("hub", BranchType.LOCAL_HUB, "region"))
    with pytest.raises(ValueError, match="REGIONAL_BRANCH_REQUIRES_HEADQUARTERS"):
        registry.register(branch("invalid-region", BranchType.REGIONAL_BRANCH, "hub"))


def test_service_area_has_one_accountable_branch() -> None:
    registry = national_registry()

    assert registry.branch_for_area("SJ-JIPHYEON").branch_id == "sejong-hub"
    with pytest.raises(ValueError, match="OVERLAPPING_SERVICE_AREA"):
        registry.register(
            branch(
                "duplicate-hub",
                BranchType.LOCAL_HUB,
                "central",
                areas=("SJ-JIPHYEON",),
            )
        )


def test_branch_operator_cannot_cross_read_or_write_other_branch() -> None:
    registry = national_registry()
    authorizer = BranchScopeAuthorizer(registry=registry)
    sejong = ScopedPrincipal("operator-sejong", PrincipalRole.HUB_OPERATOR, "sejong-hub")

    authorizer.authorize_read(
        principal=sejong,
        resource_branch_id="sejong-hub",
        resource_kind=ResourceKind.ORDER,
        contains_personal_data=True,
    )
    with pytest.raises(ValueError, match="CROSS_BRANCH_ACCESS_DENIED"):
        authorizer.authorize_read(
            principal=sejong,
            resource_branch_id="busan-hub",
            resource_kind=ResourceKind.ORDER,
            contains_personal_data=True,
        )
    with pytest.raises(ValueError, match="CROSS_BRANCH_WRITE_DENIED"):
        authorizer.authorize_write(
            principal=sejong,
            resource_branch_id="busan-hub",
        )


def test_regional_admin_can_manage_descendant_but_not_peer_region() -> None:
    registry = national_registry()
    authorizer = BranchScopeAuthorizer(registry=registry)
    central = ScopedPrincipal("admin-central", PrincipalRole.BRANCH_ADMIN, "central")

    authorizer.authorize_read(
        principal=central,
        resource_branch_id="sejong-hub",
        resource_kind=ResourceKind.SETTLEMENT,
        contains_personal_data=False,
    )
    with pytest.raises(ValueError, match="CROSS_BRANCH_ACCESS_DENIED"):
        authorizer.authorize_read(
            principal=central,
            resource_branch_id="busan-hub",
            resource_kind=ResourceKind.SETTLEMENT,
            contains_personal_data=False,
        )


def test_arkaon_is_limited_to_non_personal_aggregate_metrics() -> None:
    registry = national_registry()
    authorizer = BranchScopeAuthorizer(registry=registry)
    arkaon = ScopedPrincipal("nara-arkaon", PrincipalRole.ARKAON_ANALYST, "hq")

    authorizer.authorize_read(
        principal=arkaon,
        resource_branch_id="busan-hub",
        resource_kind=ResourceKind.AGGREGATE_METRIC,
        contains_personal_data=False,
    )
    with pytest.raises(ValueError, match="ARKAON_AGGREGATE_ONLY"):
        authorizer.authorize_read(
            principal=arkaon,
            resource_branch_id="busan-hub",
            resource_kind=ResourceKind.RIDER_PROFILE,
            contains_personal_data=True,
        )
    with pytest.raises(ValueError, match="READ_ONLY_PRINCIPAL"):
        authorizer.authorize_write(
            principal=arkaon,
            resource_branch_id="hq",
        )


def test_hq_audit_does_not_imply_unrestricted_personal_data_access() -> None:
    registry = national_registry()
    authorizer = BranchScopeAuthorizer(registry=registry)
    auditor = ScopedPrincipal("auditor", PrincipalRole.HQ_AUDITOR, "hq")

    authorizer.authorize_read(
        principal=auditor,
        resource_branch_id="sejong-hub",
        resource_kind=ResourceKind.SETTLEMENT,
        contains_personal_data=False,
    )
    with pytest.raises(ValueError, match="HQ_PERSONAL_DATA_REQUIRES_CASE_GRANT"):
        authorizer.authorize_read(
            principal=auditor,
            resource_branch_id="sejong-hub",
            resource_kind=ResourceKind.RIDER_PROFILE,
            contains_personal_data=True,
        )


def test_cross_branch_delivery_requires_two_sided_time_limited_corridor() -> None:
    branches = national_registry()
    corridors = CorridorRegistry(branches=branches)
    corridor = corridors.register(
        InterBranchCorridor(
            corridor_id="corridor-1",
            origin_branch_id="sejong-hub",
            destination_branch_id="busan-hub",
            allowed_area_codes=("BS-HAEUNDAE",),
            valid_from=NOW,
            valid_until=NOW + timedelta(days=30),
            approved_by_origin="sejong-manager",
            approved_by_destination="busan-manager",
        )
    )

    corridors.authorize_delivery(
        corridor_id=corridor.corridor_id,
        origin_branch_id="sejong-hub",
        destination_branch_id="busan-hub",
        destination_area_code="BS-HAEUNDAE",
        now=NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="INTER_BRANCH_DELIVERY_NOT_AUTHORIZED"):
        corridors.authorize_delivery(
            corridor_id=corridor.corridor_id,
            origin_branch_id="sejong-hub",
            destination_branch_id="busan-hub",
            destination_area_code="BS-NOT-APPROVED",
            now=NOW + timedelta(days=1),
        )


def test_corridor_cannot_be_self_approved_by_one_actor() -> None:
    with pytest.raises(ValueError, match="INVALID_INTER_BRANCH_CORRIDOR"):
        InterBranchCorridor(
            corridor_id="unsafe",
            origin_branch_id="sejong-hub",
            destination_branch_id="busan-hub",
            allowed_area_codes=("BS-HAEUNDAE",),
            valid_from=NOW,
            valid_until=NOW + timedelta(days=1),
            approved_by_origin="same-actor",
            approved_by_destination="same-actor",
        )


def test_branch_allocation_uses_integer_won_and_balances_explicitly() -> None:
    allocation = BranchLedgerAllocation(
        allocation_id="allocation-1",
        order_id="order-1",
        servicing_branch_id="sejong-hub",
        regional_branch_id="central",
        headquarters_id="hq",
        servicing_branch_won=700,
        regional_support_won=200,
        headquarters_shared_cost_won=100,
    )

    assert allocation.total_won == 1_000
    with pytest.raises(TypeError, match="BRANCH_ALLOCATION_MUST_USE_INTEGER_WON"):
        BranchLedgerAllocation(
            allocation_id="bad",
            order_id="order-1",
            servicing_branch_id="sejong-hub",
            regional_branch_id="central",
            headquarters_id="hq",
            servicing_branch_won=700.5,
            regional_support_won=200,
            headquarters_shared_cost_won=100,
        )
