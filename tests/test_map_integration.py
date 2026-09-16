from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.map_integration import (
    CertifiedHandoffConfig,
    KnowledgeRejected,
    KnowledgeState,
    MapCapability,
    MapIntegrationAssistant,
    MapProviderKnowledgeRegistry,
    NavigationRejected,
    OfficialSource,
    ProviderKnowledge,
    RiderNavigationContext,
    RiderNavigationHandoffService,
    privacy_safe_launch_log,
)


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def knowledge(
    *,
    state: KnowledgeState = KnowledgeState.VERIFIED,
    expires_at: datetime = NOW + timedelta(days=30),
    motorcycle: bool = False,
) -> ProviderKnowledge:
    capabilities = {
        MapCapability.GEOCODE,
        MapCapability.ROUTE,
        MapCapability.NAVIGATION_HANDOFF,
    }
    if motorcycle:
        capabilities.add(MapCapability.MOTORCYCLE_ROUTE)
    return ProviderKnowledge(
        provider_id="certified-map",
        version="2026-09-16.1",
        jurisdiction="KR",
        capabilities=frozenset(capabilities),
        state=state,
        official_sources=(
            OfficialSource.create(
                "Official navigation documentation",
                "https://docs.example.com/navigation",
                "2026-09-16",
            ),
        ),
        last_verified=NOW,
        expires_at=expires_at,
        confidence=0.95,
        attribution="Provider attribution required",
        key_restrictions="server keys must not be embedded in clients",
        terms_review="independent review required before production",
        motorcycle_support_verified=motorcycle,
    )


class Resolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_once(
        self, token: str, *, order_id: str, rider_id: str, branch_id: str
    ) -> str:
        assert (token, order_id, rider_id, branch_id) == (
            "vault:destination:one-time",
            "order-1",
            "rider-1",
            "branch-1",
        )
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("destination was resolved more than once")
        return "37.1,127.1"


def registry(record: ProviderKnowledge | None = None) -> MapProviderKnowledgeRegistry:
    result = MapProviderKnowledgeRegistry()
    result.register(record or knowledge())
    return result


def context(**changes: object) -> RiderNavigationContext:
    values = {
        "branch_id": "branch-1",
        "rider_id": "rider-1",
        "order_id": "order-1",
        "provider_id": "certified-map",
        "destination_token": "vault:destination:one-time",
        "consented": True,
        "session_id": "session-1",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(changes)
    return RiderNavigationContext(**values)


def service(record: ProviderKnowledge | None = None) -> tuple[RiderNavigationHandoffService, Resolver]:
    resolver = Resolver()
    config = CertifiedHandoffConfig(
        provider_id="certified-map",
        scheme="https",
        host="maps.example.com",
        path="/navigate",
        destination_parameter="destination",
        allowed_parameters=frozenset({"destination"}),
        certification_digest="sha256:certified-fixture",
    )
    result = RiderNavigationHandoffService(
        registry(record),
        {"certified-map": config},
        resolver,
        clock=lambda: NOW,
    )
    return result, resolver


def test_arkaon_search_compare_and_proposal_are_curated_and_non_activating() -> None:
    assistant = MapIntegrationAssistant(registry())
    assert assistant.compare(MapCapability.NAVIGATION_HANDOFF, NOW) == ("certified-map",)
    proposal = assistant.propose(
        "certified-map", MapCapability.NAVIGATION_HANDOFF, NOW
    )
    assert proposal.production_activation_allowed is False
    assert "operator promotion approval" in proposal.checklist
    assert "route_deviation_misconduct" in assistant.FORBIDDEN


def test_stale_or_provisional_knowledge_fails_closed() -> None:
    for record in (
        knowledge(expires_at=NOW),
        knowledge(state=KnowledgeState.PROVISIONAL),
        knowledge(state=KnowledgeState.BLOCKED),
    ):
        with pytest.raises(KnowledgeRejected):
            registry(record).require(
                "certified-map", MapCapability.NAVIGATION_HANDOFF, NOW
            )


def test_motorcycle_route_is_never_inferred() -> None:
    with pytest.raises(KnowledgeRejected):
        registry().require("certified-map", MapCapability.MOTORCYCLE_ROUTE, NOW)
    assert registry(knowledge(motorcycle=True)).require(
        "certified-map", MapCapability.MOTORCYCLE_ROUTE, NOW
    )


def test_explicit_consent_ephemeral_resolution_and_idempotent_launch() -> None:
    handoff_service, resolver = service()
    first = handoff_service.launch(context(), "tap-1")
    second = handoff_service.launch(context(), "tap-1")
    assert first == second
    assert resolver.calls == 1
    assert first.external_url == (
        "https://maps.example.com/navigate?destination=37.1%2C127.1"
    )
    assert "백그라운드 위치추적을 시작하지 않습니다" in first.disclosure


def test_privacy_safe_log_excludes_location_identity_and_url() -> None:
    handoff_service, _ = service()
    handoff = handoff_service.launch(context(), "tap-1")
    event = privacy_safe_launch_log(handoff, "certified-map")
    serialized = repr(event)
    assert "37.1" not in serialized
    assert "order-1" not in serialized
    assert "rider-1" not in serialized
    assert "external_url" not in event


@pytest.mark.parametrize(
    "changes",
    [
        {"consented": False},
        {"expires_at": NOW},
        {"expires_at": NOW + timedelta(minutes=16)},
        {"branch_id": "branch-1\nhttps://evil.example"},
    ],
)
def test_invalid_consent_ttl_and_scope_are_rejected(changes: dict[str, object]) -> None:
    handoff_service, _ = service()
    with pytest.raises(NavigationRejected):
        handoff_service.launch(context(**changes), "tap-1")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scheme": "javascript", "host": "", "path": "/open"},
        {"scheme": "https", "host": "maps.example.invalid", "path": "/open"},
        {"scheme": "https", "host": "maps.example.com", "path": "/../open"},
    ],
)
def test_uncertified_targets_and_open_redirect_shapes_are_rejected(
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        CertifiedHandoffConfig(
            provider_id="unsafe",
            destination_parameter="destination",
            allowed_parameters=frozenset({"destination"}),
            certification_digest="fixture",
            **kwargs,
        )


def test_unavailable_or_uncertified_provider_is_rejected() -> None:
    handoff_service, _ = service()
    with pytest.raises(KnowledgeRejected):
        handoff_service.launch(context(provider_id="missing"), "tap-1")
