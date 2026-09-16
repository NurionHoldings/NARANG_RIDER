"""Privacy-preserving routing, ETA and live-progress boundaries.

Precise coordinates and addresses exist only inside injected adapters.  Domain
objects can retain opaque provider tokens, short-lived quote facts and coarse
cells, but never raw addresses or GPS coordinates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .money import Money
from .pricing import DeliveryFacts


class GeoErrorCode(StrEnum):
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    INVALID_PROVIDER_RESULT = "INVALID_PROVIDER_RESULT"
    LOCATION_SESSION_CLOSED = "LOCATION_SESSION_CLOSED"
    PRECISE_LOCATION_FORBIDDEN = "PRECISE_LOCATION_FORBIDDEN"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    QUOTE_INTEGRITY_FAILED = "QUOTE_INTEGRITY_FAILED"
    QUOTE_STALE = "QUOTE_STALE"


class GeoBoundaryRejected(RuntimeError):
    def __init__(self, code: GeoErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class QuoteSource(StrEnum):
    PROVIDER = "provider"
    CONSERVATIVE_FALLBACK = "conservative_fallback"


class TrackingStopReason(StrEnum):
    DELIVERY_COMPLETED = "delivery_completed"
    SAFETY_STOP = "safety_stop"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    EXPIRED = "expired"


class AddressTokenResolver(Protocol):
    """Resolves a vault reference outside the domain into an opaque token."""

    def provider_token(self, vault_ref: str, *, branch_id: str) -> str: ...


class RoutingProvider(Protocol):
    """Adapter owns address/GPS handling and must return aggregate route facts."""

    def route(
        self, pickup_token: str, dropoff_token: str, *, request_id: str
    ) -> ProviderRouteResult: ...


@dataclass(frozen=True)
class ProviderRouteResult:
    distance_m: int
    duration_seconds: int
    toll_won: int
    calculated_at: datetime
    provider_route_id: str


@dataclass(frozen=True)
class RoutePolicy:
    max_age: timedelta = timedelta(minutes=5)
    max_distance_m: int = 200_000
    max_duration_seconds: int = 28_800
    fallback_distance_m: int = 12_000
    fallback_duration_seconds: int = 3_600
    fallback_toll_won: int = 0
    failure_threshold: int = 3
    circuit_open_for: timedelta = timedelta(minutes=2)
    quote_ttl: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if (
            min(
                self.max_distance_m,
                self.max_duration_seconds,
                self.fallback_distance_m,
                self.fallback_duration_seconds,
                self.failure_threshold,
            )
            <= 0
        ):
            raise ValueError("ROUTE_POLICY_MUST_BE_POSITIVE")


@dataclass(frozen=True)
class RouteQuote:
    quote_id: str
    order_id: str
    branch_id: str
    distance_m: int
    duration_seconds: int
    toll_won: int
    calculated_at: datetime
    expires_at: datetime
    source: QuoteSource
    provider_route_id_hash: str
    integrity_hash: str
    explanation_codes: tuple[str, ...]

    def public_view(self) -> Mapping[str, object]:
        return {
            "quote_id": self.quote_id,
            "distance_m": self.distance_m,
            "duration_minutes": math.ceil(self.duration_seconds / 60),
            "toll_won": self.toll_won,
            "source": self.source.value,
            "expires_at": self.expires_at.isoformat(),
            "explanation_codes": self.explanation_codes,
        }


@dataclass(frozen=True)
class CoarseLocationMetadata:
    """The only location shape allowed in durable stores."""

    branch_id: str
    order_id: str
    coarse_cell: str
    precision: int
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not (3 <= self.precision <= 5):
            raise GeoBoundaryRejected(
                GeoErrorCode.PRECISE_LOCATION_FORBIDDEN, "coarse precision must be 3..5"
            )
        if len(self.coarse_cell) != self.precision or not self.coarse_cell.isalnum():
            raise GeoBoundaryRejected(
                GeoErrorCode.PRECISE_LOCATION_FORBIDDEN, "invalid coarse cell"
            )
        if self.expires_at <= self.observed_at or self.expires_at - self.observed_at > timedelta(
            hours=24
        ):
            raise ValueError("COARSE_LOCATION_RETENTION_EXCEEDED")


def _quote_payload(quote: RouteQuote) -> bytes:
    payload = {
        "branch_id": quote.branch_id,
        "calculated_at": quote.calculated_at.isoformat(),
        "distance_m": quote.distance_m,
        "duration_seconds": quote.duration_seconds,
        "expires_at": quote.expires_at.isoformat(),
        "order_id": quote.order_id,
        "provider_route_id_hash": quote.provider_route_id_hash,
        "quote_id": quote.quote_id,
        "source": quote.source.value,
        "toll_won": quote.toll_won,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class RouteQuoteService:
    def __init__(
        self,
        resolver: AddressTokenResolver,
        provider: RoutingProvider,
        *,
        policy: RoutePolicy,
        integrity_key: bytes,
    ) -> None:
        if len(integrity_key) < 32:
            raise ValueError("INTEGRITY_KEY_TOO_SHORT")
        self._resolver = resolver
        self._provider = provider
        self._policy = policy
        self._key = integrity_key
        self._failures = 0
        self._circuit_until: datetime | None = None

    def quote(
        self,
        *,
        order_id: str,
        branch_id: str,
        pickup_address_vault_ref: str,
        dropoff_address_vault_ref: str,
        now: datetime,
    ) -> RouteQuote:
        for ref in (pickup_address_vault_ref, dropoff_address_vault_ref):
            if not ref.startswith("vault://"):
                raise GeoBoundaryRejected(
                    GeoErrorCode.PRECISE_LOCATION_FORBIDDEN, "address must be a vault reference"
                )
        request_id = hashlib.sha256(
            f"{branch_id}:{order_id}:{now.isoformat()}".encode()
        ).hexdigest()[:24]
        result: ProviderRouteResult | None = None
        if self._circuit_until is None or now >= self._circuit_until:
            try:
                pickup = self._resolver.provider_token(
                    pickup_address_vault_ref, branch_id=branch_id
                )
                dropoff = self._resolver.provider_token(
                    dropoff_address_vault_ref, branch_id=branch_id
                )
                result = self._provider.route(pickup, dropoff, request_id=request_id)
                self._validate_result(result, now)
                self._failures = 0
                self._circuit_until = None
            except (TimeoutError, ConnectionError, GeoBoundaryRejected, ValueError):
                result = None
                self._failures += 1
                if self._failures >= self._policy.failure_threshold:
                    self._circuit_until = now + self._policy.circuit_open_for
        if result is None:
            return self._build(
                order_id,
                branch_id,
                self._policy.fallback_distance_m,
                self._policy.fallback_duration_seconds,
                self._policy.fallback_toll_won,
                now,
                QuoteSource.CONSERVATIVE_FALLBACK,
                "fallback",
                ("PROVIDER_UNAVAILABLE", "CONSERVATIVE_PUBLIC_PRICE"),
            )
        return self._build(
            order_id,
            branch_id,
            result.distance_m,
            result.duration_seconds,
            result.toll_won,
            result.calculated_at,
            QuoteSource.PROVIDER,
            result.provider_route_id,
            ("PROVIDER_ROUTE", "PUBLIC_DISTANCE_DURATION_TOLL"),
        )

    def _validate_result(self, result: ProviderRouteResult, now: datetime) -> None:
        if result.calculated_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("TIMEZONE_REQUIRED")
        if (
            result.calculated_at > now + timedelta(seconds=30)
            or now - result.calculated_at > self._policy.max_age
        ):
            raise GeoBoundaryRejected(GeoErrorCode.QUOTE_STALE, "provider quote is stale")
        if not (0 < result.distance_m <= self._policy.max_distance_m):
            raise GeoBoundaryRejected(GeoErrorCode.INVALID_PROVIDER_RESULT, "invalid distance")
        if not (0 < result.duration_seconds <= self._policy.max_duration_seconds):
            raise GeoBoundaryRejected(GeoErrorCode.INVALID_PROVIDER_RESULT, "invalid duration")
        if result.toll_won < 0 or result.toll_won > 1_000_000 or not result.provider_route_id:
            raise GeoBoundaryRejected(
                GeoErrorCode.INVALID_PROVIDER_RESULT, "invalid toll or route id"
            )

    def _build(
        self,
        order_id: str,
        branch_id: str,
        distance_m: int,
        duration_seconds: int,
        toll_won: int,
        calculated_at: datetime,
        source: QuoteSource,
        provider_route_id: str,
        explanations: tuple[str, ...],
    ) -> RouteQuote:
        quote_id = (
            "route_"
            + hashlib.sha256(
                f"{branch_id}:{order_id}:{calculated_at.isoformat()}:{distance_m}:{duration_seconds}".encode()
            ).hexdigest()[:24]
        )
        quote = RouteQuote(
            quote_id=quote_id,
            order_id=order_id,
            branch_id=branch_id,
            distance_m=distance_m,
            duration_seconds=duration_seconds,
            toll_won=toll_won,
            calculated_at=calculated_at,
            expires_at=calculated_at + self._policy.quote_ttl,
            source=source,
            provider_route_id_hash=hashlib.sha256(provider_route_id.encode()).hexdigest(),
            integrity_hash="",
            explanation_codes=explanations,
        )
        return replace(
            quote,
            integrity_hash=hmac.new(self._key, _quote_payload(quote), hashlib.sha256).hexdigest(),
        )

    def verify(self, quote: RouteQuote, *, branch_id: str, order_id: str, now: datetime) -> None:
        if quote.branch_id != branch_id or quote.order_id != order_id:
            raise GeoBoundaryRejected(GeoErrorCode.BRANCH_SCOPE_MISMATCH, "quote scope mismatch")
        if now > quote.expires_at:
            raise GeoBoundaryRejected(GeoErrorCode.QUOTE_STALE, "route quote expired")
        unsigned = replace(quote, integrity_hash="")
        expected = hmac.new(self._key, _quote_payload(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, quote.integrity_hash):
            raise GeoBoundaryRejected(GeoErrorCode.QUOTE_INTEGRITY_FAILED, "route quote changed")

    @staticmethod
    def delivery_facts(
        quote: RouteQuote,
        *,
        wait_minutes: int,
        return_distance_m: int,
        return_minutes: int,
        bundled_orders: int = 1,
    ) -> DeliveryFacts:
        return DeliveryFacts(
            distance_m=quote.distance_m,
            expected_active_minutes=math.ceil(quote.duration_seconds / 60),
            expected_wait_minutes=wait_minutes,
            expected_return_distance_m=return_distance_m,
            expected_return_minutes=return_minutes,
            bundled_orders=bundled_orders,
        )


@dataclass(frozen=True)
class BundleSavingsAllocation:
    standalone_total: Money
    bundled_total: Money
    savings: Money
    rider_share: Money
    merchant_share: Money
    customer_share: Money
    explanation_codes: tuple[str, ...]


def allocate_bundle_savings(
    *,
    standalone_total: Money,
    bundled_total: Money,
    rider_basis_points: int,
    merchant_basis_points: int,
) -> BundleSavingsAllocation:
    if min(standalone_total.won, bundled_total.won) < 0 or bundled_total.won > standalone_total.won:
        raise ValueError("INVALID_BUNDLE_TOTAL")
    if (
        min(rider_basis_points, merchant_basis_points) < 0
        or rider_basis_points + merchant_basis_points > 10_000
    ):
        raise ValueError("INVALID_SAVINGS_ALLOCATION")
    savings = standalone_total - bundled_total
    rider = Money(savings.won * rider_basis_points // 10_000)
    merchant = Money(savings.won * merchant_basis_points // 10_000)
    customer = savings - rider - merchant
    return BundleSavingsAllocation(
        standalone_total,
        bundled_total,
        savings,
        rider,
        merchant,
        customer,
        ("STANDALONE_COMPARISON", "RIDER_SHARE", "MERCHANT_SHARE", "CUSTOMER_SHARE"),
    )


@dataclass(frozen=True)
class EtaAdvice:
    predicted_minutes: int
    uncertainty_minutes: int
    calibration_error_minutes: float
    advisory_only: bool = True
    allowed_uses: tuple[str, ...] = (
        "CUSTOMER_RANGE",
        "KITCHEN_COORDINATION",
        "OPERATIONS_PLANNING",
    )
    forbidden_uses: tuple[str, ...] = (
        "DISPATCH_EXCLUSION",
        "PAY_CUT",
        "PENALTY",
        "MISCONDUCT_FINDING",
    )

    def __post_init__(self) -> None:
        if (
            self.predicted_minutes <= 0
            or self.uncertainty_minutes <= 0
            or self.calibration_error_minutes < 0
        ):
            raise ValueError("INVALID_ETA_ADVICE")
        if not self.advisory_only:
            raise ValueError("ARKAON_ETA_MUST_BE_ADVISORY")


def assess_route_deviation(
    *, planned_distance_m: int, observed_distance_m: int
) -> Mapping[str, object]:
    if min(planned_distance_m, observed_distance_m) < 0:
        raise ValueError("INVALID_DISTANCE")
    return {
        "review_hint": observed_distance_m > planned_distance_m * 3 // 2,
        "misconduct_evidence": False,
        "automatic_penalty_allowed": False,
        "explanation": "route deviation may reflect safety, traffic, or provider error",
    }


@dataclass(frozen=True)
class LiveLocationSession:
    session_id: str
    branch_id: str
    order_id: str
    rider_id_hash: str
    consented_at: datetime
    expires_at: datetime
    active: bool = True
    stop_reason: TrackingStopReason | None = None


class LiveLocationService:
    """Consumes precise fixes ephemerally and emits only coarse progress."""

    def __init__(self, *, max_ttl: timedelta = timedelta(hours=4)) -> None:
        self._max_ttl = max_ttl
        self._sessions: dict[str, LiveLocationSession] = {}

    def start(
        self,
        *,
        branch_id: str,
        order_id: str,
        rider_id: str,
        consent: bool,
        now: datetime,
        ttl: timedelta,
    ) -> LiveLocationSession:
        if not consent:
            raise GeoBoundaryRejected(
                GeoErrorCode.CONSENT_REQUIRED, "live location requires consent"
            )
        if ttl <= timedelta(0) or ttl > self._max_ttl:
            raise ValueError("INVALID_LOCATION_TTL")
        session_id = (
            "loc_"
            + hashlib.sha256(
                f"{branch_id}:{order_id}:{rider_id}:{now.isoformat()}".encode()
            ).hexdigest()[:24]
        )
        session = LiveLocationSession(
            session_id,
            branch_id,
            order_id,
            hashlib.sha256(rider_id.encode()).hexdigest(),
            now,
            now + ttl,
        )
        self._sessions[session_id] = session
        return session

    def customer_progress(
        self,
        session_id: str,
        *,
        branch_id: str,
        order_id: str,
        latitude: float,
        longitude: float,
        now: datetime,
    ) -> Mapping[str, object]:
        session = self._require_active(session_id, branch_id, order_id, now)
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("INVALID_COORDINATE")
        # Coordinates are used in local variables only, then discarded. No log/store hook exists.
        zone_lat = round(latitude, 2)
        zone_lon = round(longitude, 2)
        zone = hashlib.sha256(f"{zone_lat:.2f}:{zone_lon:.2f}".encode()).hexdigest()[:8]
        return {
            "order_id": session.order_id,
            "progress_zone": zone,
            "precision": "coarse",
            "rider_identity_disclosed": False,
            "precise_coordinates_disclosed": False,
        }

    def stop(
        self,
        session_id: str,
        *,
        branch_id: str,
        order_id: str,
        reason: TrackingStopReason,
    ) -> LiveLocationSession:
        session = self._sessions.get(session_id)
        if session is None or session.branch_id != branch_id or session.order_id != order_id:
            raise GeoBoundaryRejected(GeoErrorCode.BRANCH_SCOPE_MISMATCH, "session scope mismatch")
        closed = replace(session, active=False, stop_reason=reason)
        self._sessions[session_id] = closed
        return closed

    def _require_active(
        self, session_id: str, branch_id: str, order_id: str, now: datetime
    ) -> LiveLocationSession:
        session = self._sessions.get(session_id)
        if session is None or session.branch_id != branch_id or session.order_id != order_id:
            raise GeoBoundaryRejected(GeoErrorCode.BRANCH_SCOPE_MISMATCH, "session scope mismatch")
        if not session.active or now >= session.expires_at:
            if session.active:
                self._sessions[session_id] = replace(
                    session, active=False, stop_reason=TrackingStopReason.EXPIRED
                )
            raise GeoBoundaryRejected(GeoErrorCode.LOCATION_SESSION_CLOSED, "tracking is closed")
        return session


@dataclass(frozen=True)
class GeospatialRouteContract:
    route_id: str
    method: str
    path: str
    request_dto: str | None
    response_dto: str


GEOSPATIAL_ROUTE_MANIFEST = (
    GeospatialRouteContract(
        "route_quote",
        "POST",
        "/api/v1/orders/{order_id}/route-quotes",
        "RouteQuoteRequestV1",
        "RouteQuoteV1",
    ),
    GeospatialRouteContract(
        "location_start",
        "POST",
        "/api/v1/orders/{order_id}/location-sessions",
        "LocationConsentV1",
        "LocationSessionV1",
    ),
    GeospatialRouteContract(
        "customer_progress", "GET", "/api/v1/orders/{order_id}/progress", None, "CoarseProgressV1"
    ),
    GeospatialRouteContract(
        "location_stop",
        "POST",
        "/api/v1/orders/{order_id}/location-sessions/{session_id}/stop",
        "LocationStopV1",
        "LocationSessionV1",
    ),
)
