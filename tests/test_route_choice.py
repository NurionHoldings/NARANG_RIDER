from datetime import UTC, datetime, timedelta

import pytest
from narang_rider.route_choice import (
    AcceptedPay,
    HazardAdvisory,
    RiderCostInputs,
    RouteAlternative,
    RouteChoiceService,
    RouteLeg,
    RouteObjective,
    RouteRejected,
    Verification,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def leg(order="order-a", distance=1000, motorcycle=Verification.VERIFIED, toll=0):
    return RouteLeg(order, "leg-public", distance, 600, toll, False, "없음", motorcycle, 60)


def route(**changes):
    values = {
        "route_id": "route-a",
        "provider_id": "provider-a",
        "provider_quote_ref": "opaque-ref",
        "quoted_at": NOW,
        "expires_at": NOW + timedelta(minutes=3),
        "legs": (leg(),),
        "return_to_zone_m": 500,
        "return_to_zone_s": 300,
    }
    values.update(changes)
    return RouteAlternative(**values)


def evaluate(value):
    return RouteChoiceService().evaluate(
        (value,),
        RouteObjective.BALANCED,
        RiderCostInputs(120, "전기/연료"),
        AcceptedPay(5000, 1000, 600),
        NOW,
    )


def test_transparent_cost_return_and_supplement_never_cut_accepted_pay():
    result = evaluate(route())[0]
    assert result.estimated_rider_cost_won == 180
    assert result.supplemental_review_won > 0
    assert result.accepted_pay_floor_won == 5000
    assert result.rider_must_choose and not result.tracking_allowed
    assert not result.deviation_is_misconduct


@pytest.mark.parametrize(
    "bad",
    [
        route(expires_at=NOW),
        route(quoted_at=NOW - timedelta(minutes=6)),
        route(legs=(leg(toll=-1),)),
        route(legs=(leg(motorcycle=Verification.UNKNOWN),)),
        route(provider_quote_ref=""),
    ],
)
def test_rejects_stale_tampered_negative_or_unverified_routes(bad):
    with pytest.raises(RouteRejected):
        evaluate(bad)


def test_rejects_fake_hazard_and_hidden_bundle_detour():
    fake = HazardAdvisory("폭우", 4, "http://fake", "", NOW, NOW + timedelta(hours=1))
    with pytest.raises(RouteRejected):
        evaluate(route(hazards=(fake,)))
    with pytest.raises(RouteRejected):
        evaluate(route(legs=(leg("a", 1000), leg("b", 1500))))


def test_rider_objective_controls_ranking_and_provider_disagreement_is_visible():
    cheap = route(route_id="cheap", provider_id="one", legs=(leg(toll=0),))
    fast = route(
        route_id="fast",
        provider_id="two",
        legs=(
            RouteLeg("order-a", "public", 800, 300, 100, False, "없음", Verification.VERIFIED, 30),
        ),
    )
    service = RouteChoiceService()
    args = (RiderCostInputs(100, "연료"), AcceptedPay(4000, 800, 300), NOW)
    assert service.evaluate((cheap, fast), RouteObjective.LOWEST_COST, *args)[0].route_id == "cheap"
    assert service.evaluate((cheap, fast), RouteObjective.SHORTEST, *args)[0].route_id == "fast"


def test_offline_token_contains_no_precision_and_never_auto_selects():
    item = RouteChoiceService.offline_instruction("session-safe", NOW)
    assert "session-safe" not in item.session_token
    assert not item.stores_address_coordinates_or_tiles
    assert not item.auto_select_route
