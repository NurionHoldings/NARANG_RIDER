"""Privacy-preserving rider navigation handoff and ARKAON map capability.

Provider-specific syntax is configuration accepted only after certification against
current official documentation.  This module never persists destinations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class MapCapability(StrEnum):
    GEOCODE = "geocode"
    REVERSE_GEOCODE = "reverse_geocode"
    ROUTE = "route"
    NAVIGATION_HANDOFF = "navigation_handoff"
    TRAFFIC = "traffic"
    MOTORCYCLE_ROUTE = "motorcycle_route"


class KnowledgeState(StrEnum):
    VERIFIED = "verified"
    PROVISIONAL = "provisional"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class OfficialSource:
    title: str
    url: str
    accessed_on: str
    provenance_digest: str

    @classmethod
    def create(cls, title: str, url: str, accessed_on: str) -> OfficialSource:
        digest = sha256(f"{title}\n{url}\n{accessed_on}".encode()).hexdigest()
        return cls(title, url, accessed_on, digest)


@dataclass(frozen=True)
class ProviderKnowledge:
    provider_id: str
    version: str
    jurisdiction: str
    capabilities: frozenset[MapCapability]
    state: KnowledgeState
    official_sources: tuple[OfficialSource, ...]
    last_verified: datetime
    expires_at: datetime
    confidence: float
    attribution: str | None
    key_restrictions: str
    terms_review: str
    motorcycle_support_verified: bool = False

    def usable(self, capability: MapCapability, now: datetime) -> bool:
        if self.state is not KnowledgeState.VERIFIED or now >= self.expires_at:
            return False
        if capability is MapCapability.MOTORCYCLE_ROUTE:
            return self.motorcycle_support_verified and capability in self.capabilities
        return capability in self.capabilities


class KnowledgeRejected(ValueError):
    pass


@dataclass
class MapProviderKnowledgeRegistry:
    records: dict[str, ProviderKnowledge] = field(default_factory=dict)

    def register(self, record: ProviderKnowledge) -> None:
        if not record.official_sources or not 0 <= record.confidence <= 1:
            raise KnowledgeRejected("official provenance and valid confidence are required")
        if any(not source.url.startswith("https://") for source in record.official_sources):
            raise KnowledgeRejected("official sources must use HTTPS")
        self.records[record.provider_id] = record

    def require(
        self, provider_id: str, capability: MapCapability, now: datetime
    ) -> ProviderKnowledge:
        record = self.records.get(provider_id)
        if record is None or not record.usable(capability, now):
            raise KnowledgeRejected("provider knowledge is missing, stale, or unverified")
        return record

    def search(self, term: str, now: datetime) -> tuple[ProviderKnowledge, ...]:
        needle = term.casefold().strip()
        if not needle:
            return ()
        return tuple(
            record
            for record in self.records.values()
            if record.state is KnowledgeState.VERIFIED
            and now < record.expires_at
            and (
                needle in record.provider_id.casefold()
                or any(needle in capability.value for capability in record.capabilities)
            )
        )


@dataclass(frozen=True)
class MapIntegrationProposal:
    provider_id: str
    capability: MapCapability
    checklist: tuple[str, ...]
    scaffold: str
    provenance: tuple[str, ...]
    production_activation_allowed: bool = False


class MapIntegrationAssistant:
    """ARKAON read/propose boundary; never activates production integrations."""

    FORBIDDEN = frozenset(
        {
            "dispatch_exclusion",
            "pay_change",
            "penalty",
            "insurance_decision",
            "worker_surveillance",
            "route_deviation_misconduct",
        }
    )

    def __init__(self, registry: MapProviderKnowledgeRegistry) -> None:
        self.registry = registry

    def compare(self, capability: MapCapability, now: datetime) -> tuple[str, ...]:
        return tuple(
            sorted(
                record.provider_id
                for record in self.registry.records.values()
                if record.usable(capability, now)
            )
        )

    def propose(
        self, provider_id: str, capability: MapCapability, now: datetime
    ) -> MapIntegrationProposal:
        record = self.registry.require(provider_id, capability, now)
        return MapIntegrationProposal(
            provider_id=provider_id,
            capability=capability,
            checklist=(
                "signed Ethernian directive",
                "offline contract and abuse tests",
                "operator promotion approval",
                "canary and rollback evidence",
            ),
            scaffold="ProviderAdapter(validate, build_ephemeral_handoff, availability)",
            provenance=tuple(source.provenance_digest for source in record.official_sources),
        )


class SearchProvider(Protocol):
    def search_official(self, provider_id: str) -> tuple[OfficialSource, ...]: ...


@dataclass(frozen=True)
class KnowledgeUpdateCandidate:
    provider_id: str
    previous_version: str
    proposed_sources: tuple[OfficialSource, ...]
    diff_digest: str
    human_review_required: bool = True
    canary_required: bool = True
    rollback_required: bool = True


def propose_knowledge_update(
    provider_id: str,
    registry: MapProviderKnowledgeRegistry,
    search_provider: SearchProvider,
) -> KnowledgeUpdateCandidate:
    previous = registry.records[provider_id]
    sources = search_provider.search_official(provider_id)
    if not sources:
        raise KnowledgeRejected("official retrieval produced no evidence")
    digest = sha256(
        ("|".join(source.provenance_digest for source in sources)).encode()
    ).hexdigest()
    return KnowledgeUpdateCandidate(provider_id, previous.version, sources, digest)


@dataclass(frozen=True)
class RiderNavigationContext:
    branch_id: str
    rider_id: str
    order_id: str
    provider_id: str
    destination_token: str
    consented: bool
    session_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class CertifiedHandoffConfig:
    provider_id: str
    scheme: str
    host: str
    path: str
    destination_parameter: str
    allowed_parameters: frozenset[str]
    certification_digest: str

    def __post_init__(self) -> None:
        if self.scheme not in {"https", "geo"}:
            raise ValueError("uncertified scheme")
        if self.scheme == "https" and (not self.host or self.host.endswith(".invalid")):
            raise ValueError("HTTPS host must be explicitly certified")
        if not self.path.startswith("/") or ".." in self.path:
            raise ValueError("invalid certified path")
        if self.destination_parameter not in self.allowed_parameters:
            raise ValueError("destination parameter must be allowlisted")


@dataclass(frozen=True)
class NavigationHandoff:
    launch_id: str
    external_url: str
    expires_at: datetime
    disclosure: str
    fallback_label: str = "시스템 지도에서 열기"


class NavigationRejected(ValueError):
    pass


class EphemeralDestinationResolver(Protocol):
    def resolve_once(
        self, token: str, *, order_id: str, rider_id: str, branch_id: str
    ) -> str: ...


class RiderNavigationHandoffService:
    DISCLOSURE = (
        "선택한 외부 지도 앱으로 이동합니다. 경로는 참고용이며 교통법규와 현장 안전을 우선하세요. "
        "나랑라이더는 외부 앱 사용 중 백그라운드 위치추적을 시작하지 않습니다."
    )

    def __init__(
        self,
        registry: MapProviderKnowledgeRegistry,
        configs: dict[str, CertifiedHandoffConfig],
        resolver: EphemeralDestinationResolver,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.registry = registry
        self.configs = configs
        self.resolver = resolver
        self.clock = clock
        self._launches: dict[str, NavigationHandoff] = {}

    def launch(self, context: RiderNavigationContext, idempotency_key: str) -> NavigationHandoff:
        now = self.clock()
        if not context.consented:
            raise NavigationRejected("explicit rider choice and consent are required")
        if now >= context.expires_at or context.expires_at - context.issued_at > timedelta(minutes=15):
            raise NavigationRejected("navigation session expired or exceeds TTL")
        if not all(
            value and all(char not in value for char in "\r\n\x00")
            for value in (
                context.branch_id,
                context.rider_id,
                context.order_id,
                context.session_id,
                idempotency_key,
            )
        ):
            raise NavigationRejected("invalid navigation scope")
        self.registry.require(context.provider_id, MapCapability.NAVIGATION_HANDOFF, now)
        config = self.configs.get(context.provider_id)
        if config is None:
            raise NavigationRejected("provider adapter is not certified")
        launch_id = sha256(
            f"{context.branch_id}|{context.rider_id}|{context.order_id}|"
            f"{context.session_id}|{idempotency_key}".encode()
        ).hexdigest()
        if launch_id in self._launches:
            return self._launches[launch_id]
        destination = self.resolver.resolve_once(
            context.destination_token,
            order_id=context.order_id,
            rider_id=context.rider_id,
            branch_id=context.branch_id,
        )
        if not destination or any(c in destination for c in "\r\n\x00"):
            raise NavigationRejected("destination token resolution failed")
        query = urlencode({config.destination_parameter: destination}, safe="")
        url = urlunsplit((config.scheme, config.host, config.path, query, ""))
        self._validate_url(url, config)
        handoff = NavigationHandoff(launch_id, url, context.expires_at, self.DISCLOSURE)
        self._launches[launch_id] = handoff
        return handoff

    @staticmethod
    def _validate_url(url: str, config: CertifiedHandoffConfig) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != config.scheme
            or parsed.netloc != config.host
            or parsed.path != config.path
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise NavigationRejected("handoff target is outside certified allowlist")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if not query or any(key not in config.allowed_parameters for key, _ in query):
            raise NavigationRejected("handoff query is outside certified allowlist")


def privacy_safe_launch_log(handoff: NavigationHandoff, provider_id: str) -> dict[str, str]:
    """No destination, address, coordinate, order, rider, or URL is logged."""
    return {"event": "navigation_handoff", "provider": provider_id, "launch_id": handoff.launch_id}
