from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.geospatial import (
    GEOSPATIAL_ROUTE_MANIFEST,
    CoarseLocationMetadata,
    EtaAdvice,
    GeoBoundaryRejected,
    GeoErrorCode,
    LiveLocationService,
    ProviderRouteResult,
    QuoteSource,
    RoutePolicy,
    RouteQuoteService,
    TrackingStopReason,
    allocate_bundle_savings,
    assess_route_deviation,
)
from narang_rider.money import Money

NOW = datetime(2026, 9, 16, 3, 0, tzinfo=UTC)
KEY = b"route-quote-integrity-key-32-bytes-minimum"


class Resolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def provider_token(self, vault_ref: str, *, branch_id: str) -> str:
        self.references.append(vault_ref)
        return f"token:{branch_id}:{len(self.references)}"


class Provider:
    def __init__(self, result: ProviderRouteResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def route(self, pickup_token: str, dropoff_token: str, *, request_id: str):
        self.calls += 1
        assert pickup_token.startswith("token:")
        assert dropoff_token.startswith("token:")
        assert request_id
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def service(result: ProviderRouteResult | Exception, *, threshold: int = 3):
    resolver = Resolver()
    provider = Provider(result)
    return (
        RouteQuoteService(
            resolver,
            provider,
            policy=RoutePolicy(failure_threshold=threshold),
            integrity_key=KEY,
        ),
        resolver,
        provider,
    )


def valid_result(**overrides: object) -> ProviderRouteResult:
    values = {
        "distance_m": 6_300,
        "duration_seconds": 1_320,
        "toll_won": 900,
        "calculated_at": NOW,
        "provider_route_id": "provider-secret-route-id",
    }
    values.update(overrides)
    return ProviderRouteResult(**values)  # type: ignore[arg-type]


def quote_with(svc: RouteQuoteService):
    return svc.quote(
        order_id="ord-1",
        branch_id="branch-a",
        pickup_address_vault_ref="vault://address/pickup",
        dropoff_address_vault_ref="vault://address/dropoff",
        now=NOW,
    )


def test_provider_route_is_private_integrity_bound_and_pricing_ready() -> None:
    svc, resolver, _ = service(valid_result())
    quote = quote_with(svc)

    assert quote.source is QuoteSource.PROVIDER
    assert quote.distance_m == 6_300
    assert quote.provider_route_id_hash != "provider-secret-route-id"
    assert "vault://" not in repr(quote)
    assert resolver.references == ["vault://address/pickup", "vault://address/dropoff"]
    svc.verify(quote, branch_id="branch-a", order_id="ord-1", now=NOW)

    facts = svc.delivery_facts(
        quote, wait_minutes=8, return_distance_m=2_000, return_minutes=7, bundled_orders=2
    )
    assert facts.expected_active_minutes == 22
    assert facts.expected_wait_minutes == 8
    assert facts.expected_return_distance_m == 2_000


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"distance_m": -1}, GeoErrorCode.INVALID_PROVIDER_RESULT),
        ({"distance_m": 999_999_999}, GeoErrorCode.INVALID_PROVIDER_RESULT),
        ({"duration_seconds": -1}, GeoErrorCode.INVALID_PROVIDER_RESULT),
        ({"toll_won": -1}, GeoErrorCode.INVALID_PROVIDER_RESULT),
        ({"calculated_at": NOW - timedelta(hours=1)}, GeoErrorCode.QUOTE_STALE),
    ],
)
def test_forged_or_stale_provider_facts_fail_to_conservative_fallback(change, code) -> None:
    svc, _, _ = service(valid_result(**change))
    quote = quote_with(svc)
    assert quote.source is QuoteSource.CONSERVATIVE_FALLBACK
    assert quote.distance_m == 12_000
    assert code.value in {"INVALID_PROVIDER_RESULT", "QUOTE_STALE"}


def test_timeout_opens_circuit_and_uses_conservative_public_price() -> None:
    svc, _, provider = service(TimeoutError(), threshold=2)
    assert quote_with(svc).source is QuoteSource.CONSERVATIVE_FALLBACK
    assert quote_with(svc).source is QuoteSource.CONSERVATIVE_FALLBACK
    assert quote_with(svc).source is QuoteSource.CONSERVATIVE_FALLBACK
    assert provider.calls == 2


def test_raw_addresses_are_rejected_before_provider_and_never_persisted() -> None:
    svc, _, provider = service(valid_result())
    with pytest.raises(GeoBoundaryRejected) as caught:
        svc.quote(
            order_id="ord-1",
            branch_id="branch-a",
            pickup_address_vault_ref="서울시 실제 주소",
            dropoff_address_vault_ref="vault://address/dropoff",
            now=NOW,
        )
    assert caught.value.code is GeoErrorCode.PRECISE_LOCATION_FORBIDDEN
    assert provider.calls == 0


def test_quote_tamper_expiry_cross_order_and_branch_are_blocked() -> None:
    svc, _, _ = service(valid_result())
    quote = quote_with(svc)
    with pytest.raises(GeoBoundaryRejected) as tampered:
        svc.verify(replace(quote, distance_m=1), branch_id="branch-a", order_id="ord-1", now=NOW)
    assert tampered.value.code is GeoErrorCode.QUOTE_INTEGRITY_FAILED
    with pytest.raises(GeoBoundaryRejected) as wrong_order:
        svc.verify(quote, branch_id="branch-a", order_id="ord-2", now=NOW)
    assert wrong_order.value.code is GeoErrorCode.BRANCH_SCOPE_MISMATCH
    with pytest.raises(GeoBoundaryRejected):
        svc.verify(quote, branch_id="branch-b", order_id="ord-1", now=NOW)
    with pytest.raises(GeoBoundaryRejected) as stale:
        svc.verify(quote, branch_id="branch-a", order_id="ord-1", now=NOW + timedelta(hours=1))
    assert stale.value.code is GeoErrorCode.QUOTE_STALE


def test_coarse_metadata_bounds_precision_and_retention() -> None:
    coarse = CoarseLocationMetadata("branch-a", "ord-1", "wydm3", 5, NOW, NOW + timedelta(hours=2))
    assert coarse.precision == 5
    with pytest.raises(GeoBoundaryRejected):
        CoarseLocationMetadata("branch-a", "ord-1", "wydm36", 6, NOW, NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        CoarseLocationMetadata("branch-a", "ord-1", "wydm3", 5, NOW, NOW + timedelta(days=2))


def test_bundle_savings_are_balanced_and_transparent() -> None:
    allocation = allocate_bundle_savings(
        standalone_total=Money(10_000),
        bundled_total=Money(7_000),
        rider_basis_points=5_000,
        merchant_basis_points=2_000,
    )
    assert allocation.savings == Money(3_000)
    assert allocation.rider_share == Money(1_500)
    assert allocation.merchant_share == Money(600)
    assert allocation.customer_share == Money(900)
    assert (
        allocation.rider_share + allocation.merchant_share + allocation.customer_share
        == allocation.savings
    )


def test_arkaon_eta_is_calibrated_advisory_and_cannot_become_control() -> None:
    advice = EtaAdvice(23, 7, 2.4)
    assert advice.advisory_only
    assert "DISPATCH_EXCLUSION" in advice.forbidden_uses
    assert "PAY_CUT" in advice.forbidden_uses
    with pytest.raises(ValueError):
        EtaAdvice(23, 7, 2.4, advisory_only=False)


def test_route_deviation_is_never_misconduct_evidence() -> None:
    assessment = assess_route_deviation(planned_distance_m=1_000, observed_distance_m=4_000)
    assert assessment["review_hint"] is True
    assert assessment["misconduct_evidence"] is False
    assert assessment["automatic_penalty_allowed"] is False


def test_live_location_requires_consent_is_coarse_and_stops_after_delivery() -> None:
    svc = LiveLocationService()
    with pytest.raises(GeoBoundaryRejected) as denied:
        svc.start(
            branch_id="branch-a",
            order_id="ord-1",
            rider_id="rider-1",
            consent=False,
            now=NOW,
            ttl=timedelta(hours=1),
        )
    assert denied.value.code is GeoErrorCode.CONSENT_REQUIRED
    session = svc.start(
        branch_id="branch-a",
        order_id="ord-1",
        rider_id="rider-1",
        consent=True,
        now=NOW,
        ttl=timedelta(hours=1),
    )
    progress = svc.customer_progress(
        session.session_id,
        branch_id="branch-a",
        order_id="ord-1",
        latitude=36.5045,
        longitude=127.2495,
        now=NOW,
    )
    assert progress["precision"] == "coarse"
    assert "latitude" not in progress and "longitude" not in progress
    assert "rider-1" not in repr(progress)
    svc.stop(
        session.session_id,
        branch_id="branch-a",
        order_id="ord-1",
        reason=TrackingStopReason.DELIVERY_COMPLETED,
    )
    with pytest.raises(GeoBoundaryRejected) as closed:
        svc.customer_progress(
            session.session_id,
            branch_id="branch-a",
            order_id="ord-1",
            latitude=36.5,
            longitude=127.2,
            now=NOW,
        )
    assert closed.value.code is GeoErrorCode.LOCATION_SESSION_CLOSED


def test_tracking_cross_scope_safety_stop_and_ttl_are_enforced() -> None:
    svc = LiveLocationService()
    session = svc.start(
        branch_id="branch-a",
        order_id="ord-1",
        rider_id="rider-1",
        consent=True,
        now=NOW,
        ttl=timedelta(minutes=30),
    )
    with pytest.raises(GeoBoundaryRejected) as cross_scope:
        svc.customer_progress(
            session.session_id,
            branch_id="branch-b",
            order_id="ord-1",
            latitude=0,
            longitude=0,
            now=NOW,
        )
    assert cross_scope.value.code is GeoErrorCode.BRANCH_SCOPE_MISMATCH
    svc.stop(
        session.session_id,
        branch_id="branch-a",
        order_id="ord-1",
        reason=TrackingStopReason.SAFETY_STOP,
    )
    with pytest.raises(GeoBoundaryRejected):
        svc.customer_progress(
            session.session_id,
            branch_id="branch-a",
            order_id="ord-1",
            latitude=0,
            longitude=0,
            now=NOW,
        )
    expiring = svc.start(
        branch_id="branch-a",
        order_id="ord-2",
        rider_id="rider-1",
        consent=True,
        now=NOW,
        ttl=timedelta(minutes=1),
    )
    with pytest.raises(GeoBoundaryRejected) as expired:
        svc.customer_progress(
            expiring.session_id,
            branch_id="branch-a",
            order_id="ord-2",
            latitude=0,
            longitude=0,
            now=NOW + timedelta(minutes=2),
        )
    assert expired.value.code is GeoErrorCode.LOCATION_SESSION_CLOSED


def test_map_free_route_manifest_has_safe_dtos() -> None:
    assert {route.route_id for route in GEOSPATIAL_ROUTE_MANIFEST} == {
        "route_quote",
        "location_start",
        "customer_progress",
        "location_stop",
    }
    assert all(route.path.startswith("/api/v1/orders/") for route in GEOSPATIAL_ROUTE_MANIFEST)
