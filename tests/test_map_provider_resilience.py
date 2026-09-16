from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.map_provider_resilience import (
    MapProviderResiliencePlanner,
    PlanStatus,
    ProviderBudgetPolicy,
    ResilienceRejected,
)

NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)


def policy(provider_id="primary", **changes):
    values = {
        "provider_id": provider_id,
        "priority": 1 if provider_id == "primary" else 2,
        "capabilities": frozenset({"route", "motorcycle_route"}),
        "monthly_budget_won": 1000,
        "daily_quota": 10,
        "protected_reserve_requests": 2,
        "estimated_request_cost_won": 10,
        "circuit_failure_threshold": 2,
        "circuit_cooldown_seconds": 60,
        "evidence_expires_at": NOW + timedelta(days=30),
        "certified": True,
        "motorcycle_verified": True,
    }
    values.update(changes)
    return ProviderBudgetPolicy(**values)


def planner(primary=None, fallback=None):
    return MapProviderResiliencePlanner(
        (primary or policy(), fallback or policy("fallback"))
    )


def plan(service, **changes):
    values = {
        "requested_provider_id": "primary",
        "capability": "route",
        "idempotency_key": "request-1",
        "now": NOW,
    }
    values.update(changes)
    return service.plan(**values)


def test_preferred_provider_reserves_budget_idempotently_but_cannot_call():
    service = planner()
    first = plan(service)
    second = plan(service)
    assert first.status is PlanStatus.PREFERRED_AVAILABLE
    assert first == second
    assert service.usage("primary").requests_today == 1
    assert service.usage("primary").spend_this_month_won == 10
    assert not first.provider_call_allowed
    assert not first.production_activation_allowed
    assert first.as_ci_artifact()["schema"] == "narang.map-provider-resilience-plan.v1"


def test_open_circuit_proposes_fallback_but_requires_rider_choice():
    service = planner()
    service.record_result("primary", succeeded=False, now=NOW)
    service.record_result("primary", succeeded=False, now=NOW)
    result = plan(service)
    assert result.status is PlanStatus.RIDER_CHOICE_REQUIRED
    assert result.selected_provider_id == "fallback"
    assert "circuit_open" in result.reason
    assert not result.automatic_app_switch_allowed
    assert service.usage("fallback").requests_today == 1


def test_success_resets_failure_and_circuit_state():
    service = planner()
    service.record_result("primary", succeeded=False, now=NOW)
    service.record_result("primary", succeeded=False, now=NOW)
    service.record_result("primary", succeeded=True, now=NOW)
    result = plan(service)
    assert result.status is PlanStatus.PREFERRED_AVAILABLE
    assert service.usage("primary").consecutive_failures == 0


def test_quota_preserves_emergency_reserve_by_default():
    service = planner(
        primary=policy(daily_quota=3, protected_reserve_requests=1),
        fallback=policy(
            "fallback", daily_quota=2, protected_reserve_requests=1
        ),
    )
    plan(service, idempotency_key="one")
    plan(service, idempotency_key="two")
    result = plan(service, idempotency_key="three")
    assert result.status is PlanStatus.RIDER_CHOICE_REQUIRED
    assert result.selected_provider_id == "fallback"
    blocked = plan(service, idempotency_key="four")
    assert blocked.status is PlanStatus.BLOCKED
    assert "quota_exhausted" in blocked.reason


def test_protected_reserve_requires_explicit_policy_input():
    service = planner(
        primary=policy(daily_quota=2, protected_reserve_requests=1),
        fallback=policy(
            "fallback",
            daily_quota=2,
            protected_reserve_requests=1,
            certified=False,
        ),
    )
    plan(service, idempotency_key="one")
    blocked = plan(service, idempotency_key="two")
    assert blocked.status is PlanStatus.BLOCKED
    reserved = plan(
        service,
        idempotency_key="reserve",
        allow_protected_reserve=True,
    )
    assert reserved.status is PlanStatus.PREFERRED_AVAILABLE


def test_monthly_budget_is_hard_cap():
    service = planner(
        primary=policy(monthly_budget_won=10),
        fallback=policy("fallback", monthly_budget_won=0),
    )
    plan(service, idempotency_key="one")
    result = plan(service, idempotency_key="two")
    assert result.status is PlanStatus.BLOCKED
    assert "budget_exhausted" in result.reason


@pytest.mark.parametrize(
    ("primary_changes", "reason"),
    [
        ({"certified": False}, "uncertified_or_stale"),
        ({"evidence_expires_at": NOW}, "uncertified_or_stale"),
        ({"capabilities": frozenset({"geocode"})}, "capability_missing"),
        ({"motorcycle_verified": False}, "motorcycle_unverified"),
    ],
)
def test_ineligible_primary_never_receives_reservation(primary_changes, reason):
    capability = (
        "motorcycle_route"
        if "motorcycle_verified" in primary_changes
        else "route"
    )
    service = planner(primary=policy(**primary_changes))
    result = plan(service, capability=capability)
    assert result.status is PlanStatus.RIDER_CHOICE_REQUIRED
    assert reason in result.reason
    assert service.usage("primary").requests_today == 0


def test_motorcycle_request_never_degrades_to_unverified_fallback():
    service = planner(
        primary=policy(certified=False),
        fallback=policy("fallback", motorcycle_verified=False),
    )
    result = plan(service, capability="motorcycle_route")
    assert result.status is PlanStatus.BLOCKED
    assert result.selected_provider_id is None


def test_failures_are_isolated_per_provider():
    service = planner()
    service.record_result("primary", succeeded=False, now=NOW)
    service.record_result("primary", succeeded=False, now=NOW)
    assert service.usage("fallback").consecutive_failures == 0
    assert service.usage("fallback").circuit_open_until is None


def test_circuit_cooldown_allows_planning_again():
    service = planner()
    service.record_result("primary", succeeded=False, now=NOW)
    service.record_result("primary", succeeded=False, now=NOW)
    result = plan(service, now=NOW + timedelta(seconds=61))
    assert result.status is PlanStatus.PREFERRED_AVAILABLE


@pytest.mark.parametrize(
    "changes",
    [
        {"monthly_budget_won": -1},
        {"daily_quota": 1, "protected_reserve_requests": 1},
        {"estimated_request_cost_won": -1},
        {"circuit_failure_threshold": 0},
        {"circuit_cooldown_seconds": 0},
        {"synthetic_only": False},
    ],
)
def test_unsafe_provider_policy_is_rejected(changes):
    with pytest.raises(ResilienceRejected):
        MapProviderResiliencePlanner((policy(**changes),))


def test_duplicate_empty_and_unknown_inputs_fail_closed():
    with pytest.raises(ResilienceRejected):
        MapProviderResiliencePlanner((policy(), policy()))
    service = planner()
    with pytest.raises(ResilienceRejected):
        plan(service, capability="")
    with pytest.raises(ResilienceRejected):
        plan(service, idempotency_key="")
    unknown = plan(service, requested_provider_id="unknown")
    assert unknown.status is PlanStatus.BLOCKED
    with pytest.raises(ResilienceRejected):
        service.record_result("unknown", succeeded=False, now=NOW)


def test_plan_never_changes_pay_or_penalizes_rider():
    result = plan(planner())
    assert not result.automatic_pay_change_allowed
    assert not result.automatic_rider_penalty_allowed
